"""Automated Fyers v3 access-token generation using TOTP.

Fyers access tokens expire daily at midnight IST. This module performs the
full headless login flow so the app can refresh its own token whenever it
gets a 401 from /data/* endpoints — no manual browser dance required.

Required env vars (in addition to FYERS_APP_ID / FYERS_SECRET_KEY):
    FYERS_FY_ID         Trading login id (e.g. "XB16305")
    FYERS_PIN           4-digit trading PIN
    FYERS_TOTP_SECRET   Base32 TOTP secret from the Fyers 2FA setup screen
    FYERS_REDIRECT_URI  Same redirect URI registered on the Fyers app

When all of those are set, ``refresh_access_token()`` returns a fresh token
and updates ``application.config.FYERS_ACCESS_TOKEN`` in-place. Returns
``None`` (and logs a warning) if any required value is missing or any step
of the flow fails.

Flow (Fyers v3 vagator endpoints):
  1. /vagator/v2/send_login_otp_v2   → request_key
  2. /vagator/v2/verify_otp          → request_key (with TOTP)
  3. /vagator/v2/verify_pin_v2       → access_token (vagator)
  4. /api/v3/token                   → auth_code
  5. /api/v3/validate-authcode       → app access_token
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import tempfile
import threading
import time
from typing import Optional

import requests

from application import config

log = logging.getLogger(__name__)

_VAGATOR_BASE = "https://api-t2.fyers.in/vagator/v2"
_API_BASE = "https://api-t1.fyers.in/api/v3"
_TIMEOUT = 20

# Serialises concurrent .env writers within this process. Combined with the
# atomic temp-file + os.replace below, this prevents the read-modify-write
# race that previously truncated .env down to only the token lines.
_ENV_WRITE_LOCK = threading.Lock()


# ── TOTP (RFC 6238) ────────────────────────────────────────────────────

def _generate_totp(secret: str, time_step: int = 30, digits: int = 6) -> str:
    """Generate a TOTP code from a base32 secret. No external deps."""
    key = base64.b32decode(secret.replace(" ", "").upper(), casefold=True)
    counter = int(time.time() // time_step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (
        (digest[offset] & 0x7F) << 24
        | (digest[offset + 1] & 0xFF) << 16
        | (digest[offset + 2] & 0xFF) << 8
        | (digest[offset + 3] & 0xFF)
    )
    return str(code % (10 ** digits)).zfill(digits)


# ── Helpers ────────────────────────────────────────────────────────────

def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _missing_login_creds() -> Optional[str]:
    """Personal credentials needed for the TOTP login (one-per-account)."""
    needed = {
        "FYERS_FY_ID":       os.getenv("FYERS_FY_ID", ""),
        "FYERS_PIN":         os.getenv("FYERS_PIN", ""),
        "FYERS_TOTP_SECRET": os.getenv("FYERS_TOTP_SECRET", ""),
    }
    missing = [k for k, v in needed.items() if not v]
    return ", ".join(missing) if missing else None


def _env_key_for(app_id: str, suffix: str) -> str:
    """Map an app_id back to its slot env var name (e.g. FYERS_ACCESS_TOKEN_3)."""
    if app_id == config.FYERS_APP_ID:
        return f"FYERS_{suffix}"
    for n in (2, 3, 4, 5):
        if app_id == getattr(config, f"FYERS_APP_ID_{n}", ""):
            return f"FYERS_{suffix}_{n}"
    return f"FYERS_{suffix}"


# ── TOTP login + auth-code exchange ────────────────────────────────────

def _perform_totp_login(sess: "requests.Session",
                        fy_id: str, pin: str, totp_secret: str) -> Optional[str]:
    """Steps 1-3 of the Fyers headless login. Returns the vagator access
    token (a session bearer, NOT an app access token) or None on failure.
    """
    # 1. Send login OTP (returns request_key)
    r = sess.post(
        f"{_VAGATOR_BASE}/send_login_otp_v2",
        data=json.dumps({"fy_id": _b64(fy_id), "app_id": "2"}),
        timeout=_TIMEOUT,
    )
    body = r.json()
    if r.status_code >= 400 or body.get("s") != "ok":
        log.warning("fyers_auth: send_login_otp_v2 failed: %s", body)
        return None
    request_key = body["request_key"]

    # 2. Verify TOTP. Generate the code only when we're well inside a
    # fresh 30s window so it can't expire mid-flight; retry once on the
    # next window if Fyers rejects it (clock drift).
    request_key2 = None
    last_body = None
    for _ in range(2):
        while int(time.time()) % 30 > 5:
            time.sleep(0.3)
        totp = _generate_totp(totp_secret)
        r = sess.post(
            f"{_VAGATOR_BASE}/verify_otp",
            data=json.dumps({"request_key": request_key, "otp": totp}),
            timeout=_TIMEOUT,
        )
        body = r.json()
        last_body = body
        if body.get("s") == "ok" and body.get("request_key"):
            request_key2 = body["request_key"]
            break
        time.sleep(31)
    if not request_key2:
        log.warning("fyers_auth: verify_otp failed: %s", last_body)
        return None

    # 3. Verify PIN (returns vagator access_token)
    r = sess.post(
        f"{_VAGATOR_BASE}/verify_pin_v2",
        data=json.dumps({
            "request_key": request_key2,
            "identity_type": "pin",
            "identifier": _b64(pin),
        }),
        timeout=_TIMEOUT,
    )
    body = r.json()
    if r.status_code >= 400 or body.get("s") != "ok":
        log.warning("fyers_auth: verify_pin_v2 failed: %s", body)
        return None
    return body["data"]["access_token"]


def _exchange_for_app(sess: "requests.Session",
                      fy_id: str, app_id: str, secret_key: str,
                      redirect_uri: str) -> Optional[str]:
    """Steps 4-5: convert a vagator-authenticated session into a per-app
    access token. ``sess`` must already have ``Authorization: Bearer <vagator>``
    set. Returns the app's access token (None on failure).
    """
    base_payload = {
        "fyers_id":      fy_id,
        "app_id":        app_id.split("-")[0],  # bare id
        "redirect_uri":  redirect_uri,
        "appType":       app_id.split("-")[-1] if "-" in app_id else "100",
        "code_challenge": "",
        "state":         "auto",
        "scope":         "",
        "nonce":         "",
        "response_type": "code",
        "create_cookie": True,
    }

    # 4a. /api/v3/token -> Url with auth_code (already-consented apps)
    #                    OR consent prompt (first-time apps)
    r = sess.post(
        f"{_API_BASE}/token",
        data=json.dumps(base_payload),
        timeout=_TIMEOUT,
    )
    body = r.json()
    url = body.get("Url") or body.get("url") or ""

    # 4b. Auto-accept the first-time consent prompt. Fyers returns
    # `s=ok` + `data.auth=<consent JWT>` and no Url when the user has
    # never authorised this app — the same screen they'd see in a
    # browser. Re-POSTing with `auth=<that JWT>` is equivalent to
    # clicking "Allow" and yields the real auth_code.
    if "auth_code=" not in url and body.get("s") == "ok":
        consent_jwt = (body.get("data") or {}).get("auth")
        if consent_jwt:
            r = sess.post(
                f"{_API_BASE}/token",
                data=json.dumps(dict(base_payload, auth=consent_jwt)),
                timeout=_TIMEOUT,
            )
            body = r.json()
            url = body.get("Url") or body.get("url") or ""

    if "auth_code=" not in url:
        log.warning("fyers_auth: /token gave no auth_code for app %s: %s",
                    app_id[:8], body)
        return None
    auth_code = url.split("auth_code=", 1)[1].split("&", 1)[0]

    # 5. /api/v3/validate-authcode -> app access_token
    app_id_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()
    r = sess.post(
        f"{_API_BASE}/validate-authcode",
        data=json.dumps({
            "grant_type": "authorization_code",
            "appIdHash":  app_id_hash,
            "code":       auth_code,
        }),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    body = r.json()
    token = body.get("access_token")
    if not token:
        log.warning("fyers_auth: validate-authcode failed for app %s: %s",
                    app_id[:8], body)
        return None
    return token


# ── Public API ─────────────────────────────────────────────────────────

def refresh_all_tokens() -> dict[str, Optional[str]]:
    """One TOTP login + N app token exchanges.

    Persists every refreshed token to:
      * ``config._runtime_tokens`` (in-process, picked up by fyers_app_pool)
      * The legacy module attr (e.g. ``config.FYERS_ACCESS_TOKEN_3``)
      * ``os.environ`` (so subprocesses inherit the fresh value)
      * Redis under ``fyers:token:{app_id}`` (TTL 22h, shared across workers)
      * ``.env`` on disk (local convenience, no-op when file is absent)

    Returns ``{app_id: token_or_None}``. An empty dict means we couldn't
    even attempt the login (missing creds / no apps configured).
    """
    missing = _missing_login_creds()
    if missing:
        log.warning("fyers_auth.refresh_all: missing env vars: %s", missing)
        return {}

    apps = config.fyers_app_credentials()
    if not apps:
        log.warning(
            "fyers_auth.refresh_all: no apps with both APP_ID and SECRET_KEY "
            "configured (set FYERS_SECRET_KEY[_2..5] for each app you want "
            "auto-refreshed)."
        )
        return {}

    fy_id = os.environ["FYERS_FY_ID"]
    pin = os.environ["FYERS_PIN"]
    totp_secret = os.environ["FYERS_TOTP_SECRET"]

    sess = requests.Session()
    sess.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    })

    try:
        vagator_token = _perform_totp_login(sess, fy_id, pin, totp_secret)
        if not vagator_token:
            return {a: None for a, _, _ in apps}
        sess.headers["Authorization"] = f"Bearer {vagator_token}"

        out: dict[str, Optional[str]] = {}
        for app_id, secret_key, redirect_uri in apps:
            if not redirect_uri:
                log.warning("fyers_auth.refresh_all: app %s has no redirect URI "
                            "(set FYERS_REDIRECT_URI or per-app override); skipping.",
                            app_id[:8])
                out[app_id] = None
                continue
            try:
                tok = _exchange_for_app(sess, fy_id, app_id, secret_key, redirect_uri)
            except (requests.RequestException, ValueError, KeyError) as e:
                log.warning("fyers_auth.refresh_all: app %s exchange error: %s",
                            app_id[:8], e)
                tok = None
            out[app_id] = tok
            if tok:
                _store_token(app_id, tok)

        ok = sum(1 for v in out.values() if v)
        log.info("fyers_auth.refresh_all: %d/%d apps refreshed", ok, len(apps))
        return out

    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("fyers_auth.refresh_all: unexpected error: %s", e)
        return {a: None for a, _, _ in apps}


def refresh_access_token() -> Optional[str]:
    """Back-compat single-token entry. Runs the multi-app refresh and
    returns the primary app's new token (or None).
    """
    tokens = refresh_all_tokens()
    return tokens.get(config.FYERS_APP_ID)


def load_tokens_from_redis() -> int:
    """Populate ``config._runtime_tokens`` from Redis-persisted tokens.
    Returns the number of tokens loaded. Safe to call at startup.
    """
    loaded = 0
    try:
        from application.services import cache as cache_mod
    except Exception as e:  # noqa: BLE001
        log.debug("fyers_auth.load_tokens_from_redis: cache import failed: %s", e)
        return 0
    for app_id, _secret, _redir in config.fyers_app_credentials():
        try:
            payload = cache_mod.jget(f"fyers:token:{app_id}")
        except Exception as e:  # noqa: BLE001
            log.debug("fyers_auth.load_tokens_from_redis(%s): %s", app_id[:8], e)
            continue
        if isinstance(payload, dict) and payload.get("token"):
            tok = payload["token"]
            config.set_runtime_token(app_id, tok)
            # Mirror to legacy module attr + env so older code paths still see it.
            setattr(config, _env_key_for(app_id, "ACCESS_TOKEN"), tok)
            os.environ[_env_key_for(app_id, "ACCESS_TOKEN")] = tok
            loaded += 1
    if loaded:
        log.info("fyers_auth: loaded %d cached token(s) from Redis", loaded)
    return loaded


# ── Internal persistence ───────────────────────────────────────────────

def _store_token(app_id: str, token: str) -> None:
    """Persist ``token`` for ``app_id`` everywhere callers might look."""
    env_key = _env_key_for(app_id, "ACCESS_TOKEN")
    config.set_runtime_token(app_id, token)
    setattr(config, env_key, token)
    os.environ[env_key] = token
    _persist_to_redis(app_id, token)
    _persist_token_to_env_file(env_key, token)


def _persist_to_redis(app_id: str, token: str) -> None:
    try:
        from application.services import cache as cache_mod
        cache_mod.jset(
            f"fyers:token:{app_id}",
            {"token": token, "ts": int(time.time())},
            ttl=22 * 3600,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("fyers_auth._persist_to_redis(%s): %s", app_id[:8], e)


def _persist_token_to_env_file(env_key: str, token: str) -> None:
    """Best-effort: write the refreshed token back to .env so it survives
    a Flask restart in local dev. Silent no-op when .env isn't writable.

    The update is serialised by a process-wide lock and written atomically
    (temp file + os.replace) so concurrent refreshes — multiple workers, the
    WS subprocess, or a scheduler — can never observe a half-truncated file
    and clobber the other keys. Only the matching key line is touched; every
    other line is preserved verbatim.
    """
    try:
        # Project root is three levels up from application/services/providers/.
        here = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.normpath(os.path.join(here, "..", "..", "..", ".env"))
        with _ENV_WRITE_LOCK:
            if not os.path.exists(env_path):
                return
            with open(env_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()

            # Guard against ever wiping a populated file with an empty/partial
            # snapshot: if the read came back empty but the file has bytes,
            # bail rather than overwrite.
            if not lines and os.path.getsize(env_path) > 0:
                log.warning("fyers_auth: skipped .env write for %s (empty read "
                            "of non-empty file)", env_key)
                return

            found = False
            for i, line in enumerate(lines):
                if line.lstrip().startswith(f"{env_key}="):
                    lines[i] = f"{env_key}={token}\n"
                    found = True
                    break
            if not found:
                if lines and not lines[-1].endswith("\n"):
                    lines.append("\n")
                lines.append(f"{env_key}={token}\n")

            # Atomic replace: write a temp file in the same directory, flush to
            # disk, then rename over the target. Readers see either the old or
            # the new full file — never an empty/truncated one.
            dir_name = os.path.dirname(env_path)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".env.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.writelines(lines)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_path, env_path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
    except OSError as e:
        log.warning("fyers_auth: could not persist %s to .env: %s", env_key, e)
