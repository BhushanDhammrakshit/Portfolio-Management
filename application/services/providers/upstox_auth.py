"""Upstox v2 access-token management.

Two ways to obtain a token:

1. **OAuth code exchange** (reliable, fully documented) — the user visits
   :func:`authorization_url`, logs in once, Upstox redirects to
   ``/callback/upstox?code=...`` and :func:`exchange_code` swaps that code
   for an access token. Use this if the automated flow ever breaks.

2. **Headless TOTP login** (best-effort) — :func:`refresh_access_token`
   drives Upstox's private ``service.upstox.com`` login endpoints with the
   mobile / TOTP / PIN from the environment so the daily token can refresh
   without a browser. These endpoints are undocumented and may change; any
   failure is logged with an actionable message and the caller degrades to
   Fyers/yfinance.

Upstox access tokens expire daily at 03:30 IST.

Required env (see config.UPSTOX_*):
    UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI   (OAuth)
    UPSTOX_MOBILE, UPSTOX_TOTP_SECRET, UPSTOX_PIN            (headless TOTP)
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
from urllib.parse import urlencode

import requests

from application import config

log = logging.getLogger(__name__)

_OAUTH_BASE = "https://api.upstox.com/v2/login/authorization"
_TIMEOUT = 20

# Headless refresh is expensive + rate-limited upstream; never run it more
# than once every few minutes regardless of how many 401s arrive.
_REFRESH_MIN_INTERVAL = 180
_refresh_lock = threading.Lock()
_last_refresh_at = 0.0


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


# ── OAuth (documented) ─────────────────────────────────────────────────

def authorization_url(state: str = "auto") -> str:
    """Build the Upstox consent URL the user opens once to authorise."""
    params = {
        "response_type": "code",
        "client_id": config.UPSTOX_API_KEY,
        "redirect_uri": config.UPSTOX_REDIRECT_URI,
        "state": state,
    }
    return f"{_OAUTH_BASE}/dialog?{urlencode(params)}"


def exchange_code(code: str) -> Optional[str]:
    """Swap an OAuth ``code`` (from /callback/upstox) for an access token.
    Persists the token and returns it, or None on failure.
    """
    if not code:
        return None
    if not (config.UPSTOX_API_KEY and config.UPSTOX_API_SECRET
            and config.UPSTOX_REDIRECT_URI):
        log.warning("upstox_auth.exchange_code: UPSTOX_API_KEY/SECRET/REDIRECT_URI "
                    "not fully configured")
        return None
    try:
        r = requests.post(
            f"{_OAUTH_BASE}/token",
            headers={
                "accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "code": code,
                "client_id": config.UPSTOX_API_KEY,
                "client_secret": config.UPSTOX_API_SECRET,
                "redirect_uri": config.UPSTOX_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=_TIMEOUT,
        )
        body = r.json()
        token = body.get("access_token")
        if not token:
            log.warning("upstox_auth.exchange_code: no access_token: %s",
                        str(body)[:300])
            return None
        _store_token(token)
        log.info("upstox_auth.exchange_code: token acquired")
        return token
    except (requests.RequestException, ValueError) as e:
        log.warning("upstox_auth.exchange_code: %s", e)
        return None


# ── Headless TOTP login (best-effort) ──────────────────────────────────

def _missing_login_creds() -> Optional[str]:
    needed = {
        "UPSTOX_API_KEY":     config.UPSTOX_API_KEY,
        "UPSTOX_REDIRECT_URI": config.UPSTOX_REDIRECT_URI,
        "UPSTOX_MOBILE":      config.UPSTOX_MOBILE,
        "UPSTOX_TOTP_SECRET": config.UPSTOX_TOTP_SECRET,
        "UPSTOX_PIN":         config.UPSTOX_PIN,
    }
    missing = [k for k, v in needed.items() if not v]
    return ", ".join(missing) if missing else None


def _headless_login() -> Optional[str]:
    """Drive a headless browser through Upstox's OAuth login (mobile →
    TOTP → PIN → consent), capture the redirected ``code``, and exchange
    it for an access token. Returns the token or None.

    Upstox has no documented headless token API, so we automate the same
    browser flow a user would do. Selenium is imported lazily: if it (or a
    Chromium binary) isn't available, we log an actionable message and
    return None so the app degrades to Fyers.
    """
    code = _selenium_get_code()
    if not code:
        return None
    return exchange_code(code)


def _build_driver():
    """Create a headless Chromium WebDriver. Honours UPSTOX_CHROME_BINARY
    for the browser path; Selenium Manager (bundled with Selenium ≥4.6)
    resolves the matching driver automatically.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,1024")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    binary = os.getenv("UPSTOX_CHROME_BINARY", "")
    if binary:
        opts.binary_location = binary

    driver_path = os.getenv("UPSTOX_CHROMEDRIVER", "")
    if driver_path:
        from selenium.webdriver.chrome.service import Service
        return webdriver.Chrome(service=Service(driver_path), options=opts)
    return webdriver.Chrome(options=opts)


def _selenium_get_code() -> Optional[str]:
    """Run the browser login flow and return the OAuth ``code`` (or None)."""
    try:
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        log.warning("upstox_auth: selenium not installed — run "
                    "`pip install selenium`, or authorise manually at "
                    "/broker/upstox/connect.")
        return None

    mobile = str(config.UPSTOX_MOBILE)
    totp_secret = config.UPSTOX_TOTP_SECRET
    pin = str(config.UPSTOX_PIN)
    redirect_host = config.UPSTOX_REDIRECT_URI.split("//", 1)[-1].split("/", 1)[0]

    driver = None
    try:
        try:
            driver = _build_driver()
        except Exception as e:  # noqa: BLE001  (WebDriverException et al.)
            log.warning("upstox_auth: could not start Chromium (%s). Install "
                        "Chrome/Chromium or set UPSTOX_CHROME_BINARY; "
                        "meanwhile authorise at /broker/upstox/connect.", e)
            return None

        wait = WebDriverWait(driver, 30)
        driver.get(authorization_url())

        # ── Step 1: mobile number ──────────────────────────────────────
        mobile_box = _find(wait, By, EC, [
            (By.ID, "mobileNum"),
            (By.CSS_SELECTOR, "input[type='tel']"),
            (By.CSS_SELECTOR, "input[name='mobileNumber']"),
        ])
        if not mobile_box:
            log.warning("upstox_auth: mobile field not found on login page")
            return None
        mobile_box.clear()
        mobile_box.send_keys(mobile)
        _click(driver, By, [
            (By.ID, "getOtp"),
            (By.XPATH, "//button[contains(., 'Get OTP') or contains(., 'Continue')]"),
        ])

        # ── Step 2: TOTP ───────────────────────────────────────────────
        # Generate the code only when we're well inside a fresh 30s window
        # so it can't expire while the page submits it.
        otp_box = _find(wait, By, EC, [
            (By.ID, "otpNum"),
            (By.CSS_SELECTOR, "input[name='otp']"),
            (By.CSS_SELECTOR, "input[type='text']"),
        ])
        if not otp_box:
            log.warning("upstox_auth: TOTP field not found")
            return None
        while int(time.time()) % 30 > 5:
            time.sleep(0.3)
        otp_box.clear()
        otp_box.send_keys(_generate_totp(totp_secret))
        _click(driver, By, [
            (By.ID, "continueBtn"),
            (By.XPATH, "//button[contains(., 'Continue')]"),
        ])

        # ── Step 3: PIN ────────────────────────────────────────────────
        pin_box = _find(wait, By, EC, [
            (By.ID, "pinCode"),
            (By.CSS_SELECTOR, "input[type='password']"),
            (By.CSS_SELECTOR, "input[name='pin']"),
        ])
        if not pin_box:
            log.warning("upstox_auth: PIN field not found")
            return None
        pin_box.clear()
        pin_box.send_keys(pin)
        _click(driver, By, [
            (By.ID, "pinContinueBtn"),
            (By.XPATH, "//button[contains(., 'Continue')]"),
        ])

        # ── Step 4: wait for the redirect carrying ?code=... ───────────
        try:
            WebDriverWait(driver, 30).until(
                lambda d: redirect_host in d.current_url and "code=" in d.current_url
            )
        except TimeoutException:
            log.warning("upstox_auth: did not reach redirect with code "
                        "(url=%s). The login UI may have changed or a consent "
                        "screen is blocking; authorise at /broker/upstox/connect.",
                        (driver.current_url or "")[:120])
            return None

        url = driver.current_url
        return url.split("code=", 1)[1].split("&", 1)[0]

    except WebDriverException as e:
        log.warning("upstox_auth._selenium_get_code: %s", e)
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass


def _find(wait, By, EC, locators):
    """Return the first present element among ``locators`` (a list of
    ``(by, value)`` tuples), or None if none appear within the wait.
    """
    for by, value in locators:
        try:
            return wait.until(EC.presence_of_element_located((by, value)))
        except Exception:  # noqa: BLE001
            continue
    return None


def _click(driver, By, locators) -> bool:
    """Click the first clickable element among ``locators``. Returns True
    on success. Pressing Enter on the focused field is a fallback handled
    by the caller's flow if no button matches.
    """
    for by, value in locators:
        try:
            el = driver.find_element(by, value)
            el.click()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ── Public refresh entry ───────────────────────────────────────────────

def refresh_access_token() -> Optional[str]:
    """Refresh the Upstox access token via the headless TOTP login.

    Throttled to at most once per few minutes. Returns the new token or
    None (in which case the app keeps using Fyers).
    """
    global _last_refresh_at
    missing = _missing_login_creds()
    if missing:
        log.warning("upstox_auth.refresh: missing env for auto-login: %s. "
                    "Authorise once at /broker/upstox/connect instead.", missing)
        return None

    with _refresh_lock:
        if time.time() - _last_refresh_at < _REFRESH_MIN_INTERVAL:
            return None
        _last_refresh_at = time.time()
        token = _headless_login()
        if token:
            log.info("upstox_auth.refresh: token refreshed")
        return token


def load_token_from_redis() -> bool:
    """Populate the runtime token from Redis at startup. Returns True if a
    token was loaded.
    """
    try:
        from application.services import cache as cache_mod
        payload = cache_mod.jget("upstox:token")
    except Exception as e:  # noqa: BLE001
        log.debug("upstox_auth.load_token_from_redis: %s", e)
        return False
    if isinstance(payload, dict) and payload.get("token"):
        tok = payload["token"]
        config.set_upstox_token(tok)
        config.UPSTOX_ACCESS_TOKEN = tok
        os.environ["UPSTOX_ACCESS_TOKEN"] = tok
        log.info("upstox_auth: loaded cached token from Redis")
        return True
    return False


# ── Internal persistence ───────────────────────────────────────────────

def _store_token(token: str) -> None:
    config.set_upstox_token(token)
    config.UPSTOX_ACCESS_TOKEN = token
    os.environ["UPSTOX_ACCESS_TOKEN"] = token
    _persist_to_redis(token)
    _persist_token_to_env_file("UPSTOX_ACCESS_TOKEN", token)


def _persist_to_redis(token: str) -> None:
    try:
        from application.services import cache as cache_mod
        # Tokens die at 03:30 IST; 12h TTL is a safe upper bound.
        cache_mod.jset("upstox:token",
                       {"token": token, "ts": int(time.time())},
                       ttl=12 * 3600)
    except Exception as e:  # noqa: BLE001
        log.debug("upstox_auth._persist_to_redis: %s", e)


def _persist_token_to_env_file(env_key: str, token: str) -> None:
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.normpath(os.path.join(here, "..", "..", "..", ".env"))
        if not os.path.exists(env_path):
            return
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
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
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    except OSError as e:
        log.warning("upstox_auth: could not persist %s to .env: %s", env_key, e)
