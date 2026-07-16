"""Automated Dhan access-token refresh using TOTP.

Dhan access tokens expire every 24 hours. This module generates a fresh
token programmatically using the TOTP secret, eliminating manual regeneration.

Endpoint:
    POST https://auth.dhan.co/app/generateAccessToken
         ?dhanClientId=<id>&pin=<pin>&totp=<6-digit-code>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import struct
import threading
import time
from typing import Optional

import requests

from application import config

log = logging.getLogger(__name__)

_AUTH_BASE = "https://auth.dhan.co"
_API_BASE = "https://api.dhan.co/v2"
_TIMEOUT = 15
_WRITE_LOCK = threading.Lock()
_LAST_REFRESH_AT = 0.0
_REFRESH_COOLDOWN = 300


def _generate_totp(secret: str, time_step: int = 30, digits: int = 6) -> str:
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


def refresh_access_token() -> Optional[str]:
    """Generate a fresh 24h Dhan access token using TOTP."""
    global _LAST_REFRESH_AT
    if time.time() - _LAST_REFRESH_AT < _REFRESH_COOLDOWN:
        return None
    _LAST_REFRESH_AT = time.time()

    client_id = config.DHAN_CLIENT_ID
    pin = os.getenv("DHAN_PIN", "")
    totp_secret = os.getenv("DHAN_TOTP_SECRET", "")
    if not (client_id and pin and totp_secret):
        log.warning("dhan_auth: missing DHAN_CLIENT_ID/DHAN_PIN/DHAN_TOTP_SECRET")
        return None

    # Wait for fresh TOTP window
    for _ in range(3):
        if int(time.time()) % 30 < 20:
            break
        time.sleep(1)

    totp_code = _generate_totp(totp_secret)
    try:
        r = requests.post(
            f"{_AUTH_BASE}/app/generateAccessToken",
            params={"dhanClientId": client_id, "pin": pin, "totp": totp_code},
            timeout=_TIMEOUT,
        )
        if r.status_code >= 400:
            log.warning("dhan_auth: HTTP %d: %s", r.status_code, r.text[:200])
            return None
        body = r.json() or {}
        token = body.get("accessToken")
        if not token:
            log.warning("dhan_auth: no accessToken: %s", str(body)[:200])
            return None
    except Exception as e:
        log.warning("dhan_auth: request failed: %s", e)
        return None

    _store_token(token)
    log.info("dhan_auth: refreshed (expires: %s)", body.get("expiryTime", "?"))
    return token


def renew_access_token() -> Optional[str]:
    """Extend current token by 24h. Falls back to full refresh if expired."""
    if not config.DHAN_ACCESS_TOKEN:
        return refresh_access_token()
    try:
        r = requests.get(
            f"{_API_BASE}/RenewToken",
            headers={"access-token": config.DHAN_ACCESS_TOKEN,
                     "dhanClientId": config.DHAN_CLIENT_ID},
            timeout=_TIMEOUT,
        )
        if r.status_code >= 400:
            return refresh_access_token()
        token = (r.json() or {}).get("accessToken")
        if not token:
            return refresh_access_token()
        _store_token(token)
        log.info("dhan_auth: renewed")
        return token
    except Exception:
        return refresh_access_token()


def _store_token(token: str) -> None:
    """Persist the fresh token to config, Redis, and os.environ."""
    with _WRITE_LOCK:
        config.DHAN_ACCESS_TOKEN = token
        os.environ["DHAN_ACCESS_TOKEN"] = token
        try:
            from application.services import cache as cache_mod
            cache_mod.jset("dhan:access_token", token, ttl=25 * 3600)
        except Exception:
            pass


def load_token_from_redis() -> bool:
    """Load a previously-refreshed token from Redis into config.

    Call at app startup so fresh workers pick up a token another worker
    already refreshed. Returns True if a token was loaded.
    """
    try:
        from application.services import cache as cache_mod
        token = cache_mod.jget("dhan:access_token")
        if token and isinstance(token, str) and len(token) > 50:
            config.DHAN_ACCESS_TOKEN = token
            os.environ["DHAN_ACCESS_TOKEN"] = token
            return True
    except Exception:
        pass
    return False
