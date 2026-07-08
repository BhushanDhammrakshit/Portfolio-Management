"""F&O Open-Interest buildup classifier.

Pulls the live NSE OI-spurts feed (per-underlying aggregated OI), joins with
batched yfinance previous closes to derive intraday price change %, and
classifies each F&O stock into one of four standard buildup buckets based on
the joint sign of intraday price change and intraday OI change:

    price ↑ + OI ↑  → Long Buildup       (fresh longs adding positions)
    price ↓ + OI ↑  → Short Buildup      (fresh shorts adding positions)
    price ↑ + OI ↓  → Short Covering     (shorts closing positions)
    price ↓ + OI ↓  → Long Unwinding     (longs closing positions)

Source: NSE ``/api/live-analysis-oi-spurts-underlyings`` — the same feed that
powers NSE's own "Change in Open Interest" page. Cookie-warmed session is
re-used from ``option_chain`` to avoid duplicating the auth dance.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, List, Optional

from application.services import cache as shared_cache

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
_CACHE_KEY = "intra:oi_buildup:v1"
_CACHE_TTL = 60  # seconds — endpoint itself refreshes ~every minute

# Durable "last known good" snapshot. Used after-hours / on NSE errors so the
# UI can still show meaningful data instead of an empty grid.
_LAST_GOOD_KEY = "intra:oi_buildup:last_good:v1"
_LAST_GOOD_TTL = 7 * 24 * 60 * 60  # 7 days

# OI-spurts feed gives per-underlying aggregated OI in one response.
_NSE_OI_PATH = "/api/live-analysis-oi-spurts-underlyings"

# Cached batch of previous-day closes for all F&O underlyings, scoped to the
# trading date. Refreshed once per day to derive intraday price change %.
_PREV_CLOSE_KEY_FMT = "intra:oi_buildup:prev_close:{date}"
_PREV_CLOSE_TTL = 14 * 3600  # outlives any single trading session

# Indices and pseudo-indices that appear in the feed but aren't stocks.
_INDEX_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYNXT50", "NIFTYIT", "NIFTYINFRA",
}

# Yahoo's NSE ticker mapping for symbols that don't translate via simple `.NS`.
_YAHOO_OVERRIDES = {
    "M&M": "M%26M.NS",
    "GVT&D": "GVT%26D.NS",
}


def _to_float(v) -> Optional[float]:
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _to_int(v) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _parse_expiry(s: str) -> Optional[_dt.date]:
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _classify(price_chg_pct: float, oi_chg_pct: float) -> str:
    if price_chg_pct >= 0 and oi_chg_pct >= 0:
        return "long_buildup"
    if price_chg_pct < 0 and oi_chg_pct >= 0:
        return "short_buildup"
    if price_chg_pct >= 0 and oi_chg_pct < 0:
        return "short_covering"
    return "long_unwinding"


def _yahoo_ticker(symbol: str) -> str:
    return _YAHOO_OVERRIDES.get(symbol, f"{symbol}.NS")


def _fetch_prev_closes(symbols: List[str]) -> Dict[str, float]:
    """Batch-fetch prev-day closes for the supplied F&O symbols.

    Cached for the full trading day (per symbol) so we issue only one yfinance
    multi-download per session. Returns ``{symbol: prev_close}``.
    """
    today = _dt.datetime.now(_IST).date()
    key = _PREV_CLOSE_KEY_FMT.format(date=today.strftime("%Y%m%d"))
    try:
        cached = shared_cache.jget(key) or {}
    except Exception:
        cached = {}

    missing = [s for s in symbols if s not in cached]
    if not missing:
        return cached

    from application.services import market_data  # local import (heavy module)

    ticker_to_sym: Dict[str, str] = {_yahoo_ticker(s): s for s in missing}
    start = today - _dt.timedelta(days=8)
    end = today + _dt.timedelta(days=1)
    try:
        frames = market_data.download_history(
            list(ticker_to_sym.keys()), start, end, interval="1d"
        ) or {}
    except Exception as e:
        log.warning("oi_buildup: prev_close batch fetch failed: %s", e)
        frames = {}

    for ticker, df in frames.items():
        sym = ticker_to_sym.get(ticker)
        if not sym or df is None or getattr(df, "empty", True):
            continue
        try:
            closes = df["Close"].dropna()
            if closes.empty:
                continue
            # During market hours yfinance may include today's live bar; use the
            # most recent bar dated strictly before today as previous close.
            prev = None
            for ts, val in zip(closes.index[::-1], closes.values[::-1]):
                ts_date = ts.date() if hasattr(ts, "date") else None
                if ts_date is None or ts_date < today:
                    prev = float(val)
                    break
            if prev is None:
                # No earlier bar available — fall back to the last close we got.
                prev = float(closes.iloc[-1])
            if prev > 0:
                cached[sym] = prev
        except Exception as e:
            log.debug("oi_buildup: prev_close parse failed for %s: %s", sym, e)

    try:
        shared_cache.jset(key, cached, ttl=_PREV_CLOSE_TTL)
    except Exception:
        pass
    return cached


def _row_from_underlying(c: Dict[str, Any], prev_closes: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """Map an OI-spurts-underlyings row → our normalised row."""
    symbol = (c.get("symbol") or "").strip().upper()
    if not symbol or symbol in _INDEX_SYMBOLS:
        return None

    ltp = _to_float(c.get("underlyingValue"))
    if ltp is None or ltp <= 0:
        return None

    prev = prev_closes.get(symbol)
    if not prev or prev <= 0:
        return None
    p_chg_pct = (ltp - prev) / prev * 100.0

    oi = _to_int(c.get("latestOI"))
    oi_chg = _to_int(c.get("changeInOI"))
    oi_chg_pct = _to_float(c.get("avgInOI"))
    if oi is None or oi_chg_pct is None:
        return None

    return {
        "symbol": symbol,
        "expiry": "",
        "ltp": round(ltp, 2),
        "price_chg_pct": round(p_chg_pct, 2),
        "oi": oi,
        "oi_chg": oi_chg if oi_chg is not None else 0,
        "oi_chg_pct": round(oi_chg_pct, 2),
        "volume": _to_int(c.get("volume")) or 0,
    }


def _fetch_snapshot() -> List[Dict[str, Any]]:
    # Imported lazily so ``option_chain`` doesn't load at module import time.
    from application.services.option_chain import _nse_get_json
    raw = _nse_get_json(_NSE_OI_PATH, None, retries=2)
    if not isinstance(raw, dict):
        return []
    data = raw.get("data") or []
    return [c for c in data if isinstance(c, dict)]


def _stale_payload(reason: str) -> Optional[Dict[str, Any]]:
    """Return the most recent successful snapshot, tagged as stale."""
    try:
        last = shared_cache.jget(_LAST_GOOD_KEY)
    except Exception:
        last = None
    if not last:
        return None
    out = {**last, "cached": True, "stale": True, "stale_reason": reason}
    out["stale_since"] = last.get("scan_time") or ""
    return out


def oi_buildup(force: bool = False) -> Dict[str, Any]:
    if not force:
        cached = shared_cache.jget(_CACHE_KEY)
        if cached:
            return {**cached, "cached": True}

    try:
        contracts = _fetch_snapshot()
    except Exception as e:
        log.warning("oi_buildup: NSE snapshot fetch failed: %s", e)
        stale = _stale_payload(f"fetch_failed: {e}")
        if stale is not None:
            return stale
        return {
            "error": "fetch_failed",
            "detail": str(e),
            "scan_time": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST"),
            "buckets": _empty_buckets(),
            "counts": {k: 0 for k in _empty_buckets()},
        }

    # Collect candidate stock symbols (skip indices) so we batch yfinance once.
    candidate_syms = [
        (c.get("symbol") or "").strip().upper()
        for c in contracts
    ]
    candidate_syms = [s for s in candidate_syms if s and s not in _INDEX_SYMBOLS]
    prev_closes = _fetch_prev_closes(candidate_syms)

    rows: List[Dict[str, Any]] = []
    for c in contracts:
        r = _row_from_underlying(c, prev_closes)
        if r is not None:
            rows.append(r)

    buckets: Dict[str, List[Dict[str, Any]]] = {
        "long_buildup": [],
        "short_buildup": [],
        "short_covering": [],
        "long_unwinding": [],
    }
    for r in rows:
        bucket = _classify(r["price_chg_pct"], r["oi_chg_pct"])
        buckets[bucket].append(r)

    # Sort each bucket by absolute OI-change %, biggest moves first.
    for k in buckets:
        buckets[k].sort(key=lambda x: abs(x["oi_chg_pct"]), reverse=True)

    total = sum(len(v) for v in buckets.values())

    # Live fetch returned nothing usable (markets closed, NSE quirk, parser
    # mismatch). Fall back to the last known-good snapshot if we have one.
    if total == 0:
        stale = _stale_payload("empty_live_result")
        if stale is not None:
            return stale

    payload = {
        "buckets": buckets,
        "counts": {k: len(v) for k, v in buckets.items()},
        "total": total,
        "scan_time": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST"),
        "source": "nseindia.com (live-analysis-oi-spurts-underlyings)",
        "cached": False,
        "stale": False,
    }
    try:
        shared_cache.jset(_CACHE_KEY, payload, ttl=_CACHE_TTL)
    except Exception:
        pass
    # Persist a durable copy so off-hours / failure modes still have data.
    if total > 0:
        try:
            shared_cache.jset(_LAST_GOOD_KEY, payload, ttl=_LAST_GOOD_TTL)
        except Exception:
            pass
    return payload


def _empty_buckets() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "long_buildup": [],
        "short_buildup": [],
        "short_covering": [],
        "long_unwinding": [],
    }


# ── NIFTY futures buildup (for index gap forecast) ────────────────────

_NIFTY_FUT_CACHE_KEY = "oi_buildup:nifty_futures:v1"
_NIFTY_FUT_CACHE_TTL = 90  # seconds


def nifty_futures_buildup(force: bool = False) -> Optional[Dict[str, Any]]:
    """Return NIFTY futures OI buildup classification.

    Extracts NIFTY from the same NSE OI-spurts feed that stocks use,
    but without the index filter.  Returns::

        {"symbol": "NIFTY", "price_chg_pct": ..., "oi_chg_pct": ...,
         "bucket": "long_buildup"|..., "bias": float in [-1, +1]}

    or ``None`` if data is unavailable.
    """
    if not force:
        cached = shared_cache.jget(_NIFTY_FUT_CACHE_KEY)
        if cached:
            return cached

    try:
        snapshot = _fetch_snapshot()
    except Exception as e:
        log.debug("nifty_futures_buildup: fetch failed: %s", e)
        return None

    for c in snapshot:
        sym = (c.get("symbol") or "").strip().upper()
        if sym != "NIFTY":
            continue

        ltp = _to_float(c.get("underlyingValue"))
        prev_close = _to_float(c.get("prev_close") or c.get("previousClose"))
        oi = _to_int(c.get("openInterest") or c.get("latestOI"))
        prev_oi = _to_int(c.get("prevOI") or c.get("previousOI"))

        # Derive price change %
        if ltp and prev_close and prev_close > 0:
            price_chg_pct = (ltp - prev_close) / prev_close * 100.0
        else:
            price_chg_pct = _to_float(c.get("pChange")) or 0.0

        # Derive OI change %
        if oi and prev_oi and prev_oi > 0:
            oi_chg_pct = (oi - prev_oi) / prev_oi * 100.0
        else:
            oi_chg_pct = _to_float(c.get("oiChange") or c.get("pchangeinOpenInterest")) or 0.0

        bucket = _classify(price_chg_pct, oi_chg_pct)

        # Bias: same mapping as fno_gap_forecast
        _BIAS = {
            "long_buildup": 1.0,
            "short_covering": 0.6,
            "short_buildup": -1.0,
            "long_unwinding": -0.6,
        }
        # Scale by OI magnitude (bigger OI swing → stronger signal)
        oi_mag = min(abs(oi_chg_pct) / 15.0, 1.0)
        bias = _BIAS.get(bucket, 0.0) * oi_mag

        result = {
            "symbol": "NIFTY",
            "price_chg_pct": round(price_chg_pct, 2),
            "oi_chg_pct": round(oi_chg_pct, 2),
            "bucket": bucket,
            "bias": round(bias, 3),
        }
        try:
            shared_cache.jset(_NIFTY_FUT_CACHE_KEY, result, ttl=_NIFTY_FUT_CACHE_TTL)
        except Exception:
            pass
        return result

    return None
