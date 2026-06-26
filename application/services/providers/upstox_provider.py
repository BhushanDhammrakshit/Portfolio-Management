"""Upstox API (v2/v3) market-data provider.

Endpoints used:
  GET /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}   daily + intraday history
  GET /v3/historical-candle/intraday/{key}/{unit}/{interval}      current-day candles
  GET /v2/market-quote/quotes?instrument_key=...                  full quotes (<=500)

Auth: ``Authorization: Bearer <ACCESS_TOKEN>`` header. The token is read
from ``config.upstox_access_token()`` (runtime value wins over env), and
is refreshed daily by ``upstox_auth``.

Symbol convention used by callers is Yahoo-style (``RELIANCE.NS``,
``^NSEI``). Conversion to Upstox ``instrument_key`` happens here via
:mod:`application.services.upstox_symbols`.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from typing import Iterable, Optional
from urllib.parse import quote as _urlquote

import pandas as pd

from application import config
from application.services.http_session import get_session
from application.services.upstox_symbols import (
    SymbolNotFoundError,
    from_quote_key,
    resolve,
)

log = logging.getLogger(__name__)

_HTTP = get_session("upstox", pool_connections=10, pool_maxsize=20, retries=2)

_BASE = "https://api.upstox.com"
_TIMEOUT = 15

# Map our generic interval strings -> Upstox (unit, interval) pairs.
# Upstox v3 units: minutes, hours, days, weeks, months.
_INTERVAL_MAP = {
    "1m": ("minutes", "1"), "2m": ("minutes", "2"), "3m": ("minutes", "3"),
    "5m": ("minutes", "5"), "10m": ("minutes", "10"), "15m": ("minutes", "15"),
    "30m": ("minutes", "30"), "60m": ("hours", "1"), "1h": ("hours", "1"),
    "2h": ("hours", "2"), "4h": ("hours", "4"),
    "1d": ("days", "1"), "1wk": ("weeks", "1"), "1mo": ("months", "1"),
}

# Intervals that should hit the intraday endpoint for the current day.
_INTRADAY_UNITS = {"minutes", "hours"}


class UpstoxError(RuntimeError):
    pass


# ── Circuit breaker + throttle ─────────────────────────────────────────
# A single Upstox access token / app. On 401 (bad/expired token) we
# cool off for 30 min; on 429 (rate limit) for 1 min, so the dispatcher
# silently falls back to Fyers/yfinance instead of stalling.
_AUTH_FAIL_COOLDOWN = 30 * 60
_RATE_LIMIT_COOLDOWN = 60
_MIN_REQUEST_INTERVAL = 0.04        # ≤25 req/s (Upstox allows 25/s, 250/min)

_state_lock = threading.Lock()
_disabled_until = 0.0
_last_request_at = 0.0
_disabled_reason = ""


def _is_disabled() -> bool:
    with _state_lock:
        return _disabled_until > time.time()


def _disable(seconds: float, reason: str) -> None:
    global _disabled_until, _disabled_reason
    with _state_lock:
        _disabled_until = time.time() + seconds
        _disabled_reason = reason
    log.warning("Upstox disabled for %ds: %s", int(seconds), reason)


def _throttle() -> None:
    global _last_request_at
    with _state_lock:
        delta = time.time() - _last_request_at
        wait = _MIN_REQUEST_INTERVAL - delta
    if wait > 0:
        time.sleep(wait)
    with _state_lock:
        _last_request_at = time.time()


def _headers() -> dict:
    token = config.upstox_access_token()
    if not token:
        raise UpstoxError(
            "UPSTOX_ACCESS_TOKEN not set. Connect via /broker/upstox/connect "
            "or configure the TOTP auto-login (UPSTOX_TOTP_SECRET/MOBILE/PIN)."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _try_refresh_token() -> bool:
    """Best-effort automated token refresh via upstox_auth. Throttled
    internally there. Clears the auth cooldown on success.
    """
    global _disabled_until
    try:
        from application.services.providers import upstox_auth
        tok = upstox_auth.refresh_access_token()
        if tok:
            with _state_lock:
                if "auth" in (_disabled_reason or ""):
                    _disabled_until = 0.0
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("upstox._try_refresh_token: %s", e)
    return False


def _get(path: str, params: Optional[dict] = None, _retried: bool = False) -> dict:
    if _is_disabled():
        raise UpstoxError(f"Upstox temporarily disabled: {_disabled_reason}")
    _throttle()
    url = f"{_BASE}{path}"
    r = _HTTP.get(url, params=params or {}, headers=_headers(), timeout=_TIMEOUT)
    if r.status_code == 429:
        _disable(_RATE_LIMIT_COOLDOWN, "rate limited (HTTP 429)")
        raise UpstoxError(f"Upstox {path} HTTP 429: {r.text[:200]}")
    if r.status_code in (401, 403):
        if not _retried and _try_refresh_token():
            return _get(path, params, _retried=True)
        _disable(_AUTH_FAIL_COOLDOWN, "auth failed (HTTP 401)")
        raise UpstoxError(f"Upstox {path} HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise UpstoxError(f"Upstox {path} HTTP {r.status_code}: {r.text[:300]}")
    try:
        body = r.json() or {}
    except ValueError as e:
        raise UpstoxError(f"Upstox {path} bad JSON: {e}") from e
    if body.get("status") and body.get("status") != "success":
        raise UpstoxError(f"Upstox {path} error: {str(body)[:300]}")
    return body


# ── History ────────────────────────────────────────────────────────────

def _candles_to_df(candles) -> pd.DataFrame:
    """Upstox candle = [ts_iso, open, high, low, close, volume, oi]."""
    if not candles:
        return pd.DataFrame()
    rows, idx = [], []
    for c in candles:
        if len(c) < 6:
            continue
        idx.append(c[0])
        rows.append([c[1], c[2], c[3], c[4], c[5]])
    if not rows:
        return pd.DataFrame()
    ts = pd.to_datetime(idx, utc=True, errors="coerce").tz_convert("Asia/Kolkata")
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=ts)
    df.index.name = "Date"
    # Upstox returns newest-first; normalise to ascending like other providers.
    df = df[~df.index.isna()].sort_index()
    return df


def get_history(symbol: str, days: int = 30, interval: str = "1d") -> pd.DataFrame:
    try:
        key, _name = resolve(symbol)
    except SymbolNotFoundError as e:
        log.debug("upstox.get_history: %s", e)
        return pd.DataFrame()

    pair = _INTERVAL_MAP.get(interval)
    if pair is None:
        raise UpstoxError(f"Unsupported interval: {interval}")
    unit, step = pair

    enc_key = _urlquote(key, safe="")
    today = _dt.date.today()
    pad = max(int(days * 1.6), days + 10)
    from_date = (today - _dt.timedelta(days=pad)).isoformat()
    to_date = today.isoformat()

    frames: list[pd.DataFrame] = []
    # Historical (excludes the current trading day).
    try:
        body = _get(
            f"/v3/historical-candle/{enc_key}/{unit}/{step}/{to_date}/{from_date}"
        )
        frames.append(_candles_to_df((body.get("data") or {}).get("candles") or []))
    except UpstoxError as e:
        log.warning("upstox.get_history(%s) hist: %s", symbol, e)

    # Current-day candles for intraday resolutions (history API omits today).
    if unit in _INTRADAY_UNITS:
        try:
            body = _get(
                f"/v3/historical-candle/intraday/{enc_key}/{unit}/{step}"
            )
            frames.append(_candles_to_df((body.get("data") or {}).get("candles") or []))
        except UpstoxError as e:
            log.debug("upstox.get_history(%s) intraday: %s", symbol, e)

    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def download_history(symbols: Iterable[str], start, end,
                     interval: str = "1d") -> dict[str, pd.DataFrame]:
    if isinstance(start, str):
        start_d = _dt.date.fromisoformat(start)
    elif hasattr(start, "date"):
        start_d = start.date()
    else:
        start_d = start
    if isinstance(end, str):
        end_d = _dt.date.fromisoformat(end)
    elif hasattr(end, "date"):
        end_d = end.date()
    else:
        end_d = end

    pair = _INTERVAL_MAP.get(interval) or ("days", "1")
    unit, step = pair
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            key, _name = resolve(sym)
        except SymbolNotFoundError:
            out[sym] = pd.DataFrame()
            continue
        enc_key = _urlquote(key, safe="")
        try:
            body = _get(
                f"/v3/historical-candle/{enc_key}/{unit}/{step}/"
                f"{end_d.isoformat()}/{start_d.isoformat()}"
            )
            out[sym] = _candles_to_df((body.get("data") or {}).get("candles") or [])
        except UpstoxError as e:
            log.warning("upstox.download_history(%s): %s", sym, e)
            out[sym] = pd.DataFrame()
    return out


# ── Quotes ─────────────────────────────────────────────────────────────

def _normalize_quote(symbol: str, v: dict) -> Optional[dict]:
    try:
        ltp = v.get("last_price") or 0
        ohlc = v.get("ohlc") or {}
        net_change = v.get("net_change")
        prev_close = None
        if net_change is not None and ltp:
            prev_close = float(ltp) - float(net_change)
        if not prev_close:
            prev_close = float(ohlc.get("close") or ltp or 0)
        change = float(net_change) if net_change is not None else (
            float(ltp) - prev_close if prev_close else 0.0)
        change_pct = (change / prev_close * 100.0) if prev_close else 0.0
        return {
            "symbol": symbol,
            "name": v.get("symbol") or symbol,
            "price": float(ltp) if ltp else 0.0,
            "prev_close": round(float(prev_close), 2) if prev_close else 0.0,
            "change": round(float(change), 2),
            "change_pct": round(float(change_pct), 2),
            "day_open": float(ohlc.get("open") or 0),
            "day_high": float(ohlc.get("high") or 0),
            "day_low": float(ohlc.get("low") or 0),
            "volume": int(v.get("volume") or 0),
            "market_cap": None,     # Upstox does not expose market cap
            "sector": "",           # Upstox does not expose sector
        }
    except Exception as e:  # noqa: BLE001
        log.warning("upstox._normalize_quote(%s): %s", symbol, e)
        return None


def _quote_batch(symbols: list[str]) -> dict[str, dict]:
    keys: list[str] = []
    key_to_yahoo: dict[str, str] = {}
    for sym in symbols:
        try:
            key, _name = resolve(sym)
        except SymbolNotFoundError:
            continue
        keys.append(key)
        key_to_yahoo[key] = sym
    if not keys:
        return {}

    out: dict[str, dict] = {}
    # Upstox caps quotes at 500 instruments per call.
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        try:
            body = _get("/v2/market-quote/quotes",
                        {"instrument_key": ",".join(chunk)})
        except UpstoxError as e:
            log.warning("upstox._quote_batch: %s", e)
            continue
        for resp_key, v in (body.get("data") or {}).items():
            if not isinstance(v, dict):
                continue
            # Prefer the instrument_token echoed back; fall back to the
            # response key ("NSE_EQ:SYMBOL").
            token = v.get("instrument_token") or ""
            yahoo = key_to_yahoo.get(token) or from_quote_key(resp_key)
            if yahoo:
                out[yahoo] = v
    return out


def get_quote(symbol: str) -> Optional[dict]:
    raw = _quote_batch([symbol]).get(symbol)
    if not raw:
        return None
    return _normalize_quote(symbol, raw)


def get_quotes(symbols: Iterable[str]) -> dict[str, dict]:
    raw_map = _quote_batch(list(symbols))
    out: dict[str, dict] = {}
    for sym, raw in raw_map.items():
        norm = _normalize_quote(sym, raw)
        if norm:
            out[sym] = norm
    return out


def get_info(symbol: str) -> Optional[dict]:
    """Upstox doesn't expose sector / industry / market-cap; return the
    name + exchange so the dispatcher can merge metadata from yfinance.
    """
    try:
        key, name = resolve(symbol)
    except SymbolNotFoundError:
        return None
    exchange = "NSE" if key.startswith("NSE_") else key.split("_", 1)[0]
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "sector": "",
        "industry": "",
        "currency": "INR",
        "market_cap": None,
    }


def search(query: str) -> list[dict]:
    """No public Upstox search endpoint — let the dispatcher fall back."""
    return []
