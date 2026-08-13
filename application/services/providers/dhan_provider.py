"""DhanHQ v2 REST market-data provider.

Endpoints used (https://api.dhan.co/v2):
  POST /charts/historical    daily candles
  POST /charts/intraday      1/5/15/25/60-min candles
  POST /marketfeed/quote     full quote (LTP + OHLC + volume) for up to 1000
                             instruments per request

Auth: ``access-token`` + ``client-id`` headers.

Symbol convention used by callers is Yahoo-style (``RELIANCE.NS``,
``^NSEI``). Conversion to Dhan ``security_id`` happens here via
:mod:`application.services.dhan_symbols`.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from typing import Iterable, Optional

import pandas as pd
import requests

from application import config
from application.services.dhan_symbols import (
    SymbolNotFoundError,
    resolve,
)
from application.services.http_session import get_session

_HTTP = get_session("dhan", pool_connections=10, pool_maxsize=20, retries=2)

log = logging.getLogger(__name__)

_BASE = "https://api.dhan.co/v2"
_TIMEOUT = 15

# Map our generic interval strings -> Dhan's chart intervals.
_INTRADAY_INTERVALS = {
    "1m": "1", "5m": "5", "15m": "15", "25m": "25", "60m": "60", "1h": "60",
}


class DhanError(RuntimeError):
    pass


# ── Throttle ────────────────────────────────────────────────────────────
# Dhan caps the chart endpoints at 5 req/s and /marketfeed/quote at 1 req/s.
# Callers that fan out across a thread pool (scanners) would otherwise blow
# straight through those caps, so gate every request on a shared per-endpoint
# minimum interval.
_MIN_INTERVAL = {"/marketfeed/quote": 1.0}
_DEFAULT_MIN_INTERVAL = 0.2  # 5 req/s

_throttle_lock = threading.Lock()
_last_request_at: dict[str, float] = {}


def _throttle(path: str) -> None:
    interval = _MIN_INTERVAL.get(path, _DEFAULT_MIN_INTERVAL)
    with _throttle_lock:
        now = time.monotonic()
        nxt = max(now, _last_request_at.get(path, 0.0) + interval)
        _last_request_at[path] = nxt
        wait = nxt - now
    if wait > 0:
        time.sleep(wait)


def _headers() -> dict:
    if not config.DHAN_ACCESS_TOKEN or not config.DHAN_CLIENT_ID:
        raise DhanError(
            "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set in environment "
            "to use the Dhan market-data provider."
        )
    return {
        "access-token": config.DHAN_ACCESS_TOKEN,
        "client-id": config.DHAN_CLIENT_ID,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post(path: str, payload: dict, _retried: bool = False) -> dict:
    url = f"{_BASE}{path}"
    _throttle(path)
    r = _HTTP.post(url, json=payload, headers=_headers(), timeout=_TIMEOUT)
    if r.status_code == 429:
        # Simple retry-once on rate limit.
        time.sleep(1.1)
        _throttle(path)
        r = _HTTP.post(url, json=payload, headers=_headers(), timeout=_TIMEOUT)
    # Auto-refresh on 401 (token expired daily) — single retry.
    if r.status_code == 401 and not _retried:
        try:
            from application.services.providers import dhan_auth
            if dhan_auth.refresh_access_token():
                return _post(path, payload, _retried=True)
        except Exception:
            pass
    if r.status_code >= 400:
        raise DhanError(f"Dhan {path} HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json() or {}
    except ValueError as e:
        raise DhanError(f"Dhan {path} bad JSON: {e}") from e


def _instrument_kind(segment: str) -> str:
    """Dhan ``instrument`` field expected by chart endpoints."""
    if segment.startswith("IDX"):
        return "INDEX"
    return "EQUITY"


def _candles_to_df(payload: dict) -> pd.DataFrame:
    """Convert a Dhan candles response to an OHLCV DataFrame indexed by time."""
    if not payload:
        return pd.DataFrame()
    o = payload.get("open") or []
    h = payload.get("high") or []
    low = payload.get("low") or []
    c = payload.get("close") or []
    v = payload.get("volume") or []
    ts = payload.get("timestamp") or []
    if not c or not ts:
        return pd.DataFrame()
    # Dhan timestamps are epoch seconds (IST market data, UTC epoch).
    idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
    df = pd.DataFrame({
        "Open": o, "High": h, "Low": low, "Close": c, "Volume": v,
    }, index=idx)
    df.index.name = "Date"
    return df


# ── Public API ──────────────────────────────────────────────────────────

def get_history(symbol: str, days: int = 30, interval: str = "1d") -> pd.DataFrame:
    """Daily or intraday OHLCV history for a single symbol.

    Returns an empty DataFrame on lookup failure so callers can degrade
    gracefully (matching yfinance's behaviour for missing symbols).
    """
    try:
        segment, sec_id, _name = resolve(symbol)
    except SymbolNotFoundError as e:
        log.warning("dhan.get_history: %s", e)
        return pd.DataFrame()

    today = _dt.date.today()
    # Pad lookback to cover weekends/holidays so caller gets ``days`` trading bars.
    pad = max(int(days * 1.6), days + 10)
    from_date = (today - _dt.timedelta(days=pad)).isoformat()
    to_date = today.isoformat()

    instrument = _instrument_kind(segment)
    if interval == "1d":
        body = {
            "securityId": sec_id,
            "exchangeSegment": segment,
            "instrument": instrument,
            "expiryCode": 0,
            "fromDate": from_date,
            "toDate": to_date,
        }
        path = "/charts/historical"
    else:
        dhan_int = _INTRADAY_INTERVALS.get(interval)
        if not dhan_int:
            raise DhanError(f"Unsupported interval: {interval}")
        body = {
            "securityId": sec_id,
            "exchangeSegment": segment,
            "instrument": instrument,
            "interval": dhan_int,
            "fromDate": from_date,
            "toDate": to_date,
        }
        path = "/charts/intraday"

    try:
        data = _post(path, body)
    except DhanError as e:
        log.warning("dhan.get_history(%s): %s", symbol, e)
        return pd.DataFrame()
    return _candles_to_df(data)


def download_history(symbols: Iterable[str], start, end,
                     interval: str = "1d") -> dict[str, pd.DataFrame]:
    """Download daily history for many symbols. Returns dict keyed by input symbol."""
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

    out: dict[str, pd.DataFrame] = {}
    # Dhan doesn't offer multi-symbol historical in one call — loop.
    # Throttle gently to stay under the 5-req/sec history cap.
    for i, sym in enumerate(symbols):
        try:
            segment, sec_id, _ = resolve(sym)
        except SymbolNotFoundError:
            out[sym] = pd.DataFrame()
            continue
        body = {
            "securityId": sec_id,
            "exchangeSegment": segment,
            "instrument": _instrument_kind(segment),
            "expiryCode": 0,
            "fromDate": start_d.isoformat(),
            "toDate": end_d.isoformat(),
        }
        try:
            data = _post("/charts/historical", body)
            out[sym] = _candles_to_df(data)
        except DhanError as e:
            log.warning("dhan.download_history(%s): %s", sym, e)
            out[sym] = pd.DataFrame()
        if (i + 1) % 5 == 0:
            time.sleep(1.0)
    return out


def _quote_batch(symbols: list[str]) -> dict[str, dict]:
    """Call /marketfeed/quote for a batch of symbols. Returns {symbol: raw_quote}."""
    grouped: dict[str, list[str]] = {}
    sym_by_id: dict[tuple[str, str], str] = {}
    for sym in symbols:
        try:
            segment, sec_id, _ = resolve(sym)
        except SymbolNotFoundError:
            continue
        grouped.setdefault(segment, []).append(sec_id)
        sym_by_id[(segment, sec_id)] = sym

    if not grouped:
        return {}

    payload = {seg: [int(s) for s in ids] for seg, ids in grouped.items()}
    try:
        resp = _post("/marketfeed/quote", payload)
    except DhanError as e:
        log.warning("dhan._quote_batch: %s", e)
        return {}

    out: dict[str, dict] = {}
    data = resp.get("data") or {}
    for seg, by_id in data.items():
        if not isinstance(by_id, dict):
            continue
        for sid, q in by_id.items():
            sym = sym_by_id.get((seg, str(sid)))
            if sym:
                out[sym] = q
    return out


def get_quote(symbol: str) -> Optional[dict]:
    """Return a normalized quote dict for one symbol."""
    raw_map = _quote_batch([symbol])
    raw = raw_map.get(symbol)
    if not raw:
        return None
    return _normalize_quote(symbol, raw)


def get_quotes(symbols: Iterable[str]) -> dict[str, dict]:
    """Batched normalized quotes — preferred when scanning many symbols."""
    syms = list(symbols)
    out: dict[str, dict] = {}
    # Dhan caps at 1000 instruments / request.
    for i in range(0, len(syms), 900):
        chunk = syms[i:i + 900]
        raw_map = _quote_batch(chunk)
        for sym, raw in raw_map.items():
            norm = _normalize_quote(sym, raw)
            if norm:
                out[sym] = norm
        time.sleep(1.0)  # 1 req/sec cap on quote endpoint
    return out


def _normalize_quote(symbol: str, raw: dict) -> Optional[dict]:
    try:
        ohlc = raw.get("ohlc") or {}
        ltp = raw.get("last_price") or raw.get("LTP") or ohlc.get("close") or 0
        # Per Dhan's docs, ohlc.close is TODAY's closing price (not
        # yesterday's) — it equals ltp once the market closes, which made
        # change/change_pct collapse to 0 after hours. Dhan already gives us
        # ``net_change`` = "absolute change in LTP from previous day closing
        # price", so derive change/prev_close from that instead.
        net_change = raw.get("net_change")
        if net_change not in (None, ""):
            change = float(net_change)
            prev_close = float(ltp) - change if ltp else 0.0
        else:
            # Fallback for payloads without net_change (e.g. some F&O rows).
            prev_close = ohlc.get("close") or ltp
            if not prev_close:
                prev_close = ltp
            change = float(ltp) - float(prev_close) if prev_close else 0.0
        change_pct = (change / float(prev_close) * 100.0) if prev_close else 0.0

        try:
            _seg, _sid, name = resolve(symbol)
        except SymbolNotFoundError:
            name = symbol

        return {
            "symbol": symbol,
            "name": name,
            "price": float(ltp) if ltp else 0.0,
            "prev_close": float(prev_close) if prev_close else 0.0,
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "day_open": float(ohlc.get("open") or 0),
            "day_high": float(ohlc.get("high") or 0),
            "day_low": float(ohlc.get("low") or 0),
            "volume": int(raw.get("volume") or 0),
            "market_cap": None,  # Dhan does not expose market cap
            "sector": "",        # Dhan does not expose sector
        }
    except Exception as e:
        log.warning("dhan._normalize_quote(%s): %s", symbol, e)
        return None


def get_info(symbol: str) -> Optional[dict]:
    """Lightweight info — Dhan doesn't expose sector / industry / market cap.

    We resolve the display name from the scrip master and return a minimal
    payload. Callers needing richer metadata should let the dispatcher fall
    back to yfinance.
    """
    try:
        segment, _sid, name = resolve(symbol)
    except SymbolNotFoundError:
        return None
    return {
        "symbol": symbol,
        "name": name,
        "exchange": "NSE" if segment == "NSE_EQ" else ("BSE" if segment == "BSE_EQ" else segment),
        "sector": "",
        "industry": "",
        "currency": "INR",
        "market_cap": None,
    }


def search(query: str) -> list[dict]:
    """Search the cached scrip master by symbol/name prefix."""
    from application.services.dhan_symbols import _load_master, _nse_eq, _bse_eq

    q = (query or "").strip().upper()
    if len(q) < 2:
        return []
    _load_master()

    out: list[dict] = []
    seen = set()
    for src, exch in ((_nse_eq, "NSE"), (_bse_eq, "BSE")):
        for sym, (_seg, _sid, name) in src.items():
            if sym.startswith(q) or q in name.upper():
                key = (sym, exch)
                if key in seen:
                    continue
                seen.add(key)
                yahoo_sym = f"{sym}.NS" if exch == "NSE" else f"{sym}.BO"
                out.append({
                    "symbol": yahoo_sym,
                    "name": name or sym,
                    "exchange": exch,
                    "type": "EQUITY",
                })
                if len(out) >= 20:
                    return out
    return out
