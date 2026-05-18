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
import time
from typing import Optional

import requests

from application import config

log = logging.getLogger(__name__)

_VAGATOR_BASE = "https://api-t2.fyers.in/vagator/v2"
_API_BASE = "https://api-t1.fyers.in/api/v3"
_TIMEOUT = 20


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


def _missing_creds() -> Optional[str]:
    needed = {
        "FYERS_APP_ID":       config.FYERS_APP_ID,
        "FYERS_SECRET_KEY":   config.FYERS_SECRET_KEY,
        "FYERS_FY_ID":        os.getenv("FYERS_FY_ID", ""),
        "FYERS_PIN":          os.getenv("FYERS_PIN", ""),
        "FYERS_TOTP_SECRET":  os.getenv("FYERS_TOTP_SECRET", ""),
        "FYERS_REDIRECT_URI": os.getenv("FYERS_REDIRECT_URI", ""),
    }
    missing = [k for k, v in needed.items() if not v]
    return ", ".join(missing) if missing else None


# ── Public API ─────────────────────────────────────────────────────────

def refresh_access_token() -> Optional[str]:
    """Run the full automated login. On success, returns the new access
    token AND mutates ``config.FYERS_ACCESS_TOKEN`` so subsequent calls
    pick it up. Returns None (and logs a warning) on any failure.
    """
    missing = _missing_creds()
    if missing:
        log.warning("fyers_auth: cannot auto-refresh token, missing env vars: %s", missing)
        return None

    fy_id = os.environ["FYERS_FY_ID"]
    pin = os.environ["FYERS_PIN"]
    totp_secret = os.environ["FYERS_TOTP_SECRET"]
    redirect_uri = os.environ["FYERS_REDIRECT_URI"]
    app_id = config.FYERS_APP_ID
    secret_key = config.FYERS_SECRET_KEY

    sess = requests.Session()
    sess.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    })

    try:
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
        # fresh 30s window so it can't expire mid-flight; if Fyers still
        # rejects it (clock drift), wait for the next window and retry once.
        request_key2 = None
        last_body = None
        for attempt in range(2):
            # Wait until we're at most 5 seconds into the current TOTP
            # window — gives the request ~25s of validity to reach Fyers.
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
            # Don't burn the second attempt on the same now-expired code —
            # let the current TOTP window roll over first.
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
        vagator_token = body["data"]["access_token"]

        # 4. Exchange for auth_code
        sess.headers["Authorization"] = f"Bearer {vagator_token}"
        r = sess.post(
            f"{_API_BASE}/token",
            data=json.dumps({
                "fyers_id":     fy_id,
                "app_id":       app_id.split("-")[0],   # Fyers wants the bare id here
                "redirect_uri": redirect_uri,
                "appType":      app_id.split("-")[-1] if "-" in app_id else "100",
                "code_challenge": "",
                "state":        "auto",
                "scope":        "",
                "nonce":        "",
                "response_type": "code",
                "create_cookie": True,
            }),
            timeout=_TIMEOUT,
        )
        body = r.json()
        url = body.get("Url") or body.get("url") or ""
        if "auth_code=" not in url:
            log.warning("fyers_auth: /token did not return auth_code: %s", body)
            return None
        auth_code = url.split("auth_code=", 1)[1].split("&", 1)[0]

        # 5. Validate auth_code → app access_token
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
            log.warning("fyers_auth: validate-authcode failed: %s", body)
            return None

        # Mutate runtime config so subsequent _headers() calls use the new token.
        config.FYERS_ACCESS_TOKEN = token
        os.environ["FYERS_ACCESS_TOKEN"] = token
        _persist_token_to_env(token)
        log.info("fyers_auth: refreshed access token successfully")
        return token

    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("fyers_auth: unexpected error during refresh: %s", e)
        return None


def _persist_token_to_env(token: str) -> None:
    """Best-effort: write the refreshed token back to .env so it survives
    a Flask restart. Silent no-op if .env can't be found or written.
    """
    try:
        # Project root is two levels up from application/services/providers/.
        here = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.normpath(os.path.join(here, "..", "..", "..", ".env"))
        if not os.path.exists(env_path):
            return
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        found = False
        for i, line in enumerate(lines):
            if line.lstrip().startswith("FYERS_ACCESS_TOKEN="):
                lines[i] = f"FYERS_ACCESS_TOKEN={token}\n"
                found = True
                break
        if not found:
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(f"FYERS_ACCESS_TOKEN={token}\n")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    except OSError as e:
        log.warning("fyers_auth: could not persist token to .env: %s", e)
