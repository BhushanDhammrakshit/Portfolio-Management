"""Fyers API v3 market-data provider.

Endpoints (https://api-t1.fyers.in):
  GET /data/quotes      ?symbols=NSE:RELIANCE-EQ,NSE:SBIN-EQ   (<=50 syms)
  GET /data/history     ?symbol=...&resolution=...&date_format=1
                        &range_from=YYYY-MM-DD&range_to=YYYY-MM-DD&cont_flag=1

Auth: ``Authorization: <APP_ID>:<ACCESS_TOKEN>`` header.

Symbol mapping (Yahoo style → Fyers):
  RELIANCE.NS    → NSE:RELIANCE-EQ
  RELIANCE.BO    → BSE:RELIANCE-A
  ^NSEI / NIFTY  → NSE:NIFTY50-INDEX
  ^NSEBANK / BN  → NSE:NIFTYBANK-INDEX
  FINNIFTY       → NSE:FINNIFTY-INDEX
  ^BSESN/SENSEX  → BSE:SENSEX-INDEX
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Iterable, Optional

import pandas as pd
import requests

from application import config

log = logging.getLogger(__name__)

_BASE = "https://api-t1.fyers.in"
_TIMEOUT = 15

# Resolutions accepted by Fyers /data/history: 1,2,3,5,10,15,20,30,45,60,
# 120,240,D,W,M
_INTERVAL_MAP = {
    "1m": "1", "2m": "2", "3m": "3", "5m": "5", "10m": "10",
    "15m": "15", "20m": "20", "30m": "30", "45m": "45",
    "60m": "60", "1h": "60", "2h": "120", "4h": "240",
    "1d": "D", "1wk": "W", "1mo": "M",
}

_INDEX_MAP = {
    "^NSEI":     "NSE:NIFTY50-INDEX",
    "NIFTY":     "NSE:NIFTY50-INDEX",
    "NIFTY50":   "NSE:NIFTY50-INDEX",
    "^NSEBANK":  "NSE:NIFTYBANK-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY":  "NSE:FINNIFTY-INDEX",
    "^BSESN":    "BSE:SENSEX-INDEX",
    "SENSEX":    "BSE:SENSEX-INDEX",
}


class FyersError(RuntimeError):
    pass


# ── Circuit breaker ─────────────────────────────────────────────────────
# When the Fyers token is invalid (401) or we keep hitting Cloudflare's
# 1015 rate limit (429), we disable the provider for a cool-off window so
# the dispatcher silently falls back to yfinance instead of stalling
# every request with another retry against a known-bad upstream.
_DISABLED_UNTIL = 0.0  # epoch seconds; 0 = enabled
_DISABLED_REASON = ""
_AUTH_FAIL_COOLDOWN = 30 * 60      # 30 min after a 401
_RATE_LIMIT_COOLDOWN = 60          # 1 min after a 429
_LAST_REQUEST_AT = 0.0
_MIN_REQUEST_INTERVAL = 0.25       # ≤4 req/s globally; well under Fyers limits


def _is_disabled() -> bool:
    return time.time() < _DISABLED_UNTIL


def _disable_for(seconds: float, reason: str) -> None:
    global _DISABLED_UNTIL, _DISABLED_REASON
    _DISABLED_UNTIL = time.time() + seconds
    _DISABLED_REASON = reason
    log.warning("Fyers provider disabled for %ds: %s", int(seconds), reason)


def _throttle() -> None:
    global _LAST_REQUEST_AT
    delta = time.time() - _LAST_REQUEST_AT
    if delta < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - delta)
    _LAST_REQUEST_AT = time.time()


def _headers() -> dict:
    if not config.FYERS_APP_ID or not config.FYERS_ACCESS_TOKEN:
        raise FyersError(
            "FYERS_APP_ID and FYERS_ACCESS_TOKEN must be set in environment "
            "to use the Fyers market-data provider."
        )
    return {
        "Authorization": f"{config.FYERS_APP_ID}:{config.FYERS_ACCESS_TOKEN}",
        "Accept": "application/json",
    }


def _get(path: str, params: dict, _retried: bool = False) -> dict:
    if _is_disabled():
        raise FyersError(f"Fyers temporarily disabled: {_DISABLED_REASON}")
    _throttle()
    url = f"{_BASE}{path}"
    r = requests.get(url, params=params, headers=_headers(), timeout=_TIMEOUT)
    if r.status_code == 429:
        _disable_for(_RATE_LIMIT_COOLDOWN, "rate limited (HTTP 429)")
        raise FyersError(f"Fyers {path} HTTP 429: {r.text[:200]}")
    if r.status_code == 401 or (
        r.status_code >= 400 and "could not authenticate" in r.text.lower()
    ):
        # Try once to auto-refresh the token, then retry the call.
        if not _retried and _try_refresh_token():
            return _get(path, params, _retried=True)
        _disable_for(_AUTH_FAIL_COOLDOWN, "auth failed (HTTP 401)")
        raise FyersError(f"Fyers {path} HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise FyersError(f"Fyers {path} HTTP {r.status_code}: {r.text[:300]}")
    try:
        body = r.json() or {}
    except ValueError as e:
        raise FyersError(f"Fyers {path} bad JSON: {e}") from e
    if body.get("s") and body.get("s") != "ok":
        msg = str(body)
        if "could not authenticate" in msg.lower() or body.get("code") == -16:
            if not _retried and _try_refresh_token():
                return _get(path, params, _retried=True)
            _disable_for(_AUTH_FAIL_COOLDOWN, "auth failed (body code -16)")
        raise FyersError(f"Fyers {path} error: {msg[:300]}")
    return body


# ── Token auto-refresh ─────────────────────────────────────────────────
# Throttle refresh attempts so a flood of 401s can't hammer the login
# endpoint. We try at most once every 5 minutes.
_LAST_REFRESH_AT = 0.0
_REFRESH_COOLDOWN = 300  # seconds


def _try_refresh_token() -> bool:
    """Attempt automated token refresh via fyers_auth. Returns True on
    success. Throttled to one attempt per 5 minutes regardless of caller.
    """
    global _LAST_REFRESH_AT
    if time.time() - _LAST_REFRESH_AT < _REFRESH_COOLDOWN:
        return False
    _LAST_REFRESH_AT = time.time()
    try:
        from application.services.providers import fyers_auth
        new_token = fyers_auth.refresh_access_token()
        return bool(new_token)
    except Exception as e:
        log.warning("fyers._try_refresh_token: %s", e)
        return False


# ── Symbol conversion ──────────────────────────────────────────────────

def _to_fyers(symbol: str) -> Optional[str]:
    """Yahoo-style symbol → Fyers symbol. Returns None if unmappable."""
    if not symbol:
        return None
    s = symbol.strip().upper()

    if s in _INDEX_MAP:
        return _INDEX_MAP[s]

    if s.endswith(".NS"):
        base = s[:-3]
        return f"NSE:{base}-EQ"
    if s.endswith(".BO"):
        base = s[:-3]
        # BSE Fyers symbols append the trading group (A/B/T/...). The most
        # common is "A". This is a best-effort guess; BSE is rarely used
        # in this app and the dispatcher will fall back to yfinance.
        return f"BSE:{base}-A"
    if s.startswith(("NSE:", "BSE:", "MCX:")):
        return s

    # Bare symbol, default to NSE EQ
    return f"NSE:{s}-EQ"


def _from_fyers(fy_symbol: str) -> str:
    """Fyers symbol → Yahoo-style symbol (for response normalization)."""
    if not fy_symbol:
        return fy_symbol
    s = fy_symbol.upper()
    # Reverse index map first
    for yahoo, fy in _INDEX_MAP.items():
        if fy == s:
            return yahoo
    if s.startswith("NSE:") and s.endswith("-EQ"):
        return s[4:-3] + ".NS"
    if s.startswith("BSE:") and "-" in s[4:]:
        return s[4:].split("-", 1)[0] + ".BO"
    return fy_symbol


# ── Public API ─────────────────────────────────────────────────────────

def _candles_to_df(candles) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    rows = []
    idx = []
    for c in candles:
        # [epoch_seconds, open, high, low, close, volume]
        if len(c) < 6:
            continue
        idx.append(c[0])
        rows.append([c[1], c[2], c[3], c[4], c[5]])
    if not rows:
        return pd.DataFrame()
    ts = pd.to_datetime(idx, unit="s", utc=True).tz_convert("Asia/Kolkata")
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=ts)
    df.index.name = "Date"
    return df


def get_history(symbol: str, days: int = 30, interval: str = "1d") -> pd.DataFrame:
    fy = _to_fyers(symbol)
    if not fy:
        return pd.DataFrame()

    resolution = _INTERVAL_MAP.get(interval)
    if resolution is None:
        raise FyersError(f"Unsupported interval: {interval}")

    today = _dt.date.today()
    pad = max(int(days * 1.6), days + 10)
    range_from = (today - _dt.timedelta(days=pad)).isoformat()
    range_to = today.isoformat()

    params = {
        "symbol": fy,
        "resolution": resolution,
        "date_format": "1",
        "range_from": range_from,
        "range_to": range_to,
        "cont_flag": "1",
    }
    try:
        body = _get("/data/history", params)
    except FyersError as e:
        log.warning("fyers.get_history(%s): %s", symbol, e)
        return pd.DataFrame()
    return _candles_to_df(body.get("candles") or [])


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

    resolution = _INTERVAL_MAP.get(interval) or "D"
    out: dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(symbols):
        fy = _to_fyers(sym)
        if not fy:
            out[sym] = pd.DataFrame()
            continue
        params = {
            "symbol": fy,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start_d.isoformat(),
            "range_to": end_d.isoformat(),
            "cont_flag": "1",
        }
        try:
            body = _get("/data/history", params)
            out[sym] = _candles_to_df(body.get("candles") or [])
        except FyersError as e:
            log.warning("fyers.download_history(%s): %s", sym, e)
            out[sym] = pd.DataFrame()
        # Fyers history rate limit ~ 10 req/sec; be conservative.
        if (i + 1) % 8 == 0:
            time.sleep(1.0)
    return out


def _quote_batch(symbols: list[str]) -> dict[str, dict]:
    fy_syms = []
    fy_to_yahoo: dict[str, str] = {}
    for sym in symbols:
        fy = _to_fyers(sym)
        if fy:
            fy_syms.append(fy)
            fy_to_yahoo[fy] = sym
    if not fy_syms:
        return {}

    out: dict[str, dict] = {}
    # Fyers caps quotes at 50 symbols per call.
    for i in range(0, len(fy_syms), 50):
        chunk = fy_syms[i:i + 50]
        try:
            body = _get("/data/quotes", {"symbols": ",".join(chunk)})
        except FyersError as e:
            log.warning("fyers._quote_batch: %s", e)
            continue
        for entry in body.get("d") or []:
            if entry.get("s") != "ok":
                continue
            n = entry.get("n")
            v = entry.get("v") or {}
            yahoo_sym = fy_to_yahoo.get(n) or _from_fyers(n)
            out[yahoo_sym] = v
        time.sleep(0.4)  # polite throttle
    return out


def _normalize_quote(symbol: str, v: dict) -> Optional[dict]:
    try:
        ltp = v.get("lp") or v.get("last_price") or 0
        prev_close = v.get("prev_close_price") or v.get("prev_close") or ltp
        change = v.get("ch")
        change_pct = v.get("chp")
        if change is None and prev_close:
            change = float(ltp) - float(prev_close)
        if change_pct is None and prev_close:
            change_pct = (float(change) / float(prev_close) * 100.0) if prev_close else 0.0

        return {
            "symbol": symbol,
            "name": v.get("short_name") or v.get("symbol") or symbol,
            "price": float(ltp) if ltp else 0.0,
            "prev_close": float(prev_close) if prev_close else 0.0,
            "change": round(float(change or 0), 2),
            "change_pct": round(float(change_pct or 0), 2),
            "day_open": float(v.get("open_price") or 0),
            "day_high": float(v.get("high_price") or 0),
            "day_low": float(v.get("low_price") or 0),
            "volume": int(v.get("volume") or 0),
            "market_cap": None,   # Fyers does not expose market cap
            "sector": "",         # Fyers does not expose sector
        }
    except Exception as e:
        log.warning("fyers._normalize_quote(%s): %s", symbol, e)
        return None


def get_quote(symbol: str) -> Optional[dict]:
    raw_map = _quote_batch([symbol])
    raw = raw_map.get(symbol)
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
    """Fyers doesn't expose sector / industry / market-cap.

    We return name + exchange via a single quote call; the dispatcher will
    merge in sector/market-cap from the fallback provider.
    """
    fy = _to_fyers(symbol)
    if not fy:
        return None
    raw_map = _quote_batch([symbol])
    raw = raw_map.get(symbol) or {}
    name = raw.get("short_name") or raw.get("symbol") or symbol
    exchange = "NSE" if fy.startswith("NSE:") else ("BSE" if fy.startswith("BSE:") else fy.split(":")[0])
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
    """Fyers has no public search endpoint usable without auth context.

    Return empty so the dispatcher falls back to yfinance for search.
    """
    return []
