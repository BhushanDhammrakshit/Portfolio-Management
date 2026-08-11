"""Intraday tool-suite — ORB, RVOL, gappers, pivots, momentum bursts, index basis.

All functions are pure (no Flask dependencies) and return JSON-serialisable
dicts. Routes in ``intraday_tools_api.py`` simply jsonify the results.

Caching uses the shared cache module (Redis when configured, in-process
fallback otherwise). TTLs are short (30s-2min) because intraday data is
real-time-sensitive.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from application.services import cache as shared_cache, market_data

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# Universe — NIFTY 100 large-caps + selected mid-caps (reuse swing universe)
try:
    from application.services.swing_scanner import UNIVERSE as _SWING_UNIVERSE
    UNIVERSE: List[str] = list(_SWING_UNIVERSE)
except Exception:
    UNIVERSE = [
        "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
        "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "KOTAKBANK.NS", "LT.NS",
        "HINDUNILVR.NS", "AXISBANK.NS", "BAJFINANCE.NS", "MARUTI.NS",
        "SUNPHARMA.NS", "TITAN.NS", "ULTRACEMCO.NS", "WIPRO.NS",
        "HCLTECH.NS", "ADANIENT.NS", "TATAMOTORS.NS", "TATASTEEL.NS",
    ]

INDICES = {
    "NIFTY 50": "^NSEI",
    "BANK NIFTY": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "FIN NIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
}


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _now_ist() -> _dt.datetime:
    return _dt.datetime.now(_IST)


def _display_name(symbol: str) -> str:
    return symbol.replace(".NS", "").replace(".BO", "").replace("^", "")


def _parallel(fn, items, max_workers=8):
    out = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fn, it): it for it in items}
        for fut in as_completed(futures):
            try:
                r = fut.result()
                if r is not None:
                    out.append(r)
            except Exception as e:
                log.debug("parallel error on %r: %s", futures[fut], e)
    return out


# ─────────────────────────────────────────────────────────────────────────
# 1. ORB — Opening Range Breakout (15 / 30 min)
# ─────────────────────────────────────────────────────────────────────────

def _orb_for_symbol(symbol: str, orb_minutes: int = 15) -> Optional[Dict[str, Any]]:
    df = market_data.get_history(symbol, days=2, interval="5m")
    if df is None or df.empty or len(df) < 6:
        return None

    # Filter to today's date in IST
    df = df.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC", nonexistent="shift_forward",
                                        ambiguous="NaT").tz_convert(_IST)
    else:
        df.index = df.index.tz_convert(_IST)

    today = _now_ist().date()
    today_df = df[df.index.date == today]
    if today_df.empty:
        # market may not have opened yet — use latest available trading day
        latest_date = df.index.date[-1]
        today_df = df[df.index.date == latest_date]
        if today_df.empty:
            return None

    bars_in_orb = max(1, orb_minutes // 5)
    orb_bars = today_df.iloc[:bars_in_orb]
    rest = today_df.iloc[bars_in_orb:]

    orb_high = _safe_float(orb_bars["High"].max())
    orb_low = _safe_float(orb_bars["Low"].min())
    orb_vol = _safe_float(orb_bars["Volume"].sum())
    if orb_high <= 0 or orb_low <= 0:
        return None

    last_price = _safe_float(today_df["Close"].iloc[-1])
    day_high = _safe_float(today_df["High"].max())
    day_low = _safe_float(today_df["Low"].min())
    day_vol = _safe_float(today_df["Volume"].sum())

    # Breakout status
    status = "inside"
    if last_price > orb_high:
        status = "breakout_up"
    elif last_price < orb_low:
        status = "breakout_down"

    # Volume confirmation — total day vol vs 10-day avg
    daily = market_data.get_history(symbol, days=20, interval="1d")
    avg_vol = (_safe_float(daily["Volume"].tail(10).mean())
               if daily is not None and not daily.empty else 0.0)
    vol_ratio = (day_vol / avg_vol) if avg_vol > 0 else 0.0

    prev_close = (_safe_float(daily["Close"].iloc[-2])
                  if daily is not None and len(daily) >= 2 else last_price)
    change_pct = ((last_price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

    # Score: stronger breakout if outside range + volume confirmation
    score = 0
    if status == "breakout_up":
        pct_above = (last_price - orb_high) / orb_high * 100.0
        score = min(100, int(pct_above * 20 + min(40, vol_ratio * 20)))
    elif status == "breakout_down":
        pct_below = (orb_low - last_price) / orb_low * 100.0
        score = -min(100, int(pct_below * 20 + min(40, vol_ratio * 20)))

    return {
        "symbol": symbol,
        "name": _display_name(symbol),
        "orb_high": round(orb_high, 2),
        "orb_low": round(orb_low, 2),
        "orb_range_pct": round((orb_high - orb_low) / orb_low * 100.0, 2) if orb_low else 0,
        "price": round(last_price, 2),
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "change_pct": round(change_pct, 2),
        "vol_ratio": round(vol_ratio, 2),
        "status": status,
        "score": score,
        "orb_minutes": orb_minutes,
    }


def orb_scan(orb_minutes: int = 15, force: bool = False) -> Dict[str, Any]:
    key = f"intra:orb:{orb_minutes}"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    results = _parallel(lambda s: _orb_for_symbol(s, orb_minutes), UNIVERSE, max_workers=10)
    # Sort: breakouts up first (by score desc), then breakouts down (by score asc), then inside
    def _rank(r):
        if r["status"] == "breakout_up":
            return (0, -r["score"])
        if r["status"] == "breakout_down":
            return (1, r["score"])
        return (2, 0)
    results.sort(key=_rank)

    payload = {
        "stocks": results,
        "orb_minutes": orb_minutes,
        "breakout_up": sum(1 for r in results if r["status"] == "breakout_up"),
        "breakout_down": sum(1 for r in results if r["status"] == "breakout_down"),
        "inside": sum(1 for r in results if r["status"] == "inside"),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=90)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 2. RVOL — Relative Volume Heatmap
# ─────────────────────────────────────────────────────────────────────────

def _rvol_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """RVOL = today's cumulative volume / 20-day avg volume at same time-of-day."""
    daily = market_data.get_history(symbol, days=30, interval="1d")
    if daily is None or daily.empty or len(daily) < 5:
        return None

    avg_vol_20d = _safe_float(daily["Volume"].tail(20).mean())
    if avg_vol_20d <= 0:
        return None

    # Approximate "expected volume to this point in day" by scaling 20-day avg
    # by elapsed fraction of trading session (9:15 - 15:30 IST = 375 min).
    now = _now_ist()
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now < market_open:
        elapsed_pct = 1.0  # treat as full prev day
    elif now > market_close:
        elapsed_pct = 1.0
    else:
        elapsed_pct = max(0.05, (now - market_open).total_seconds() / 375.0 / 60.0)
    expected_vol = avg_vol_20d * elapsed_pct

    quote = market_data.get_quote(symbol)
    if not quote:
        return None
    today_vol = _safe_float(quote.get("volume"))
    if today_vol <= 0:
        return None

    rvol = today_vol / expected_vol if expected_vol > 0 else 0.0
    rvol_full = today_vol / avg_vol_20d if avg_vol_20d > 0 else 0.0

    cmp_price = _safe_float(quote.get("ltp"))
    change_pct = _safe_float(quote.get("change_pct"))

    # Tier
    if rvol >= 3:
        tier = "extreme"
    elif rvol >= 2:
        tier = "high"
    elif rvol >= 1.3:
        tier = "elevated"
    else:
        tier = "normal"

    return {
        "symbol": symbol,
        "name": _display_name(symbol),
        "price": round(cmp_price, 2),
        "change_pct": round(change_pct, 2),
        "rvol": round(rvol, 2),
        "rvol_full_day": round(rvol_full, 2),
        "tier": tier,
        "today_vol": int(today_vol),
        "avg_vol_20d": int(avg_vol_20d),
    }


def rvol_heatmap(force: bool = False) -> Dict[str, Any]:
    key = "intra:rvol:heatmap"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    results = _parallel(_rvol_for_symbol, UNIVERSE, max_workers=10)
    results.sort(key=lambda r: r["rvol"], reverse=True)

    payload = {
        "stocks": results,
        "extreme": sum(1 for r in results if r["tier"] == "extreme"),
        "high": sum(1 for r in results if r["tier"] == "high"),
        "elevated": sum(1 for r in results if r["tier"] == "elevated"),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=45)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 3. Pre-market gappers + gap-fill tracker
# ─────────────────────────────────────────────────────────────────────────

def _gap_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    daily = market_data.get_history(symbol, days=10, interval="1d")
    if daily is None or daily.empty or len(daily) < 3:
        return None

    today_open = _safe_float(daily["Open"].iloc[-1])
    prev_close = _safe_float(daily["Close"].iloc[-2])
    today_high = _safe_float(daily["High"].iloc[-1])
    today_low = _safe_float(daily["Low"].iloc[-1])
    last_price = _safe_float(daily["Close"].iloc[-1])

    if prev_close <= 0 or today_open <= 0:
        return None
    gap_pct = (today_open - prev_close) / prev_close * 100.0
    if abs(gap_pct) < 0.5:  # ignore tiny gaps
        return None

    # Gap-fill: did intraday price retrace back through prev close?
    if gap_pct > 0:
        filled = today_low <= prev_close
        gap_remaining_pct = max(0.0, (last_price - prev_close) / prev_close * 100.0)
    else:
        filled = today_high >= prev_close
        gap_remaining_pct = min(0.0, (last_price - prev_close) / prev_close * 100.0)

    direction = "gap_up" if gap_pct > 0 else "gap_down"

    return {
        "symbol": symbol,
        "name": _display_name(symbol),
        "prev_close": round(prev_close, 2),
        "open": round(today_open, 2),
        "price": round(last_price, 2),
        "high": round(today_high, 2),
        "low": round(today_low, 2),
        "gap_pct": round(gap_pct, 2),
        "direction": direction,
        "filled": bool(filled),
        "gap_remaining_pct": round(gap_remaining_pct, 2),
    }


def gappers_and_gap_fill(force: bool = False) -> Dict[str, Any]:
    key = "intra:gappers:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    results = _parallel(_gap_for_symbol, UNIVERSE, max_workers=10)
    results.sort(key=lambda r: abs(r["gap_pct"]), reverse=True)

    payload = {
        "stocks": results,
        "gap_up": sum(1 for r in results if r["direction"] == "gap_up"),
        "gap_down": sum(1 for r in results if r["direction"] == "gap_down"),
        "filled": sum(1 for r in results if r["filled"]),
        "open_gaps": sum(1 for r in results if not r["filled"]),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=120)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 4. Pivot levels + confluence
# ─────────────────────────────────────────────────────────────────────────

def pivot_levels(symbol: str) -> Optional[Dict[str, Any]]:
    """Classic / Fibonacci / Camarilla pivots + confluence detection."""
    daily = market_data.get_history(symbol, days=10, interval="1d")
    if daily is None or daily.empty or len(daily) < 2:
        return None

    # Use previous day's H/L/C
    prev = daily.iloc[-2]
    high = _safe_float(prev["High"])
    low = _safe_float(prev["Low"])
    close = _safe_float(prev["Close"])
    if high <= 0 or low <= 0:
        return None

    last_price = _safe_float(daily["Close"].iloc[-1])

    # Classic
    pp = (high + low + close) / 3
    r1 = 2 * pp - low
    s1 = 2 * pp - high
    r2 = pp + (high - low)
    s2 = pp - (high - low)
    r3 = high + 2 * (pp - low)
    s3 = low - 2 * (high - pp)

    # Fibonacci
    rng = high - low
    fib_r1 = pp + 0.382 * rng
    fib_r2 = pp + 0.618 * rng
    fib_r3 = pp + 1.000 * rng
    fib_s1 = pp - 0.382 * rng
    fib_s2 = pp - 0.618 * rng
    fib_s3 = pp - 1.000 * rng

    # Camarilla
    cam_r1 = close + rng * 1.1 / 12
    cam_r2 = close + rng * 1.1 / 6
    cam_r3 = close + rng * 1.1 / 4
    cam_r4 = close + rng * 1.1 / 2
    cam_s1 = close - rng * 1.1 / 12
    cam_s2 = close - rng * 1.1 / 6
    cam_s3 = close - rng * 1.1 / 4
    cam_s4 = close - rng * 1.1 / 2

    levels = [
        ("Classic PP", pp), ("Classic R1", r1), ("Classic R2", r2), ("Classic R3", r3),
        ("Classic S1", s1), ("Classic S2", s2), ("Classic S3", s3),
        ("Fib R1 (38.2%)", fib_r1), ("Fib R2 (61.8%)", fib_r2), ("Fib R3 (100%)", fib_r3),
        ("Fib S1 (38.2%)", fib_s1), ("Fib S2 (61.8%)", fib_s2), ("Fib S3 (100%)", fib_s3),
        ("Cam R3", cam_r3), ("Cam R4", cam_r4),
        ("Cam S3", cam_s3), ("Cam S4", cam_s4),
    ]

    # Confluence detection — group levels within 0.3% of each other
    sorted_levels = sorted(levels, key=lambda x: x[1])
    confluences: List[Dict[str, Any]] = []
    cluster = [sorted_levels[0]]
    for name, val in sorted_levels[1:]:
        if val and cluster[-1][1] and abs(val - cluster[-1][1]) / cluster[-1][1] < 0.003:
            cluster.append((name, val))
        else:
            if len(cluster) >= 2:
                avg = sum(v for _, v in cluster) / len(cluster)
                confluences.append({
                    "level": round(avg, 2),
                    "members": [n for n, _ in cluster],
                    "strength": len(cluster),
                    "distance_pct": round((avg - last_price) / last_price * 100.0, 2)
                                    if last_price else 0,
                })
            cluster = [(name, val)]
    if len(cluster) >= 2:
        avg = sum(v for _, v in cluster) / len(cluster)
        confluences.append({
            "level": round(avg, 2),
            "members": [n for n, _ in cluster],
            "strength": len(cluster),
            "distance_pct": round((avg - last_price) / last_price * 100.0, 2)
                            if last_price else 0,
        })
    confluences.sort(key=lambda c: c["strength"], reverse=True)

    def _fmt(levels_list):
        return [{"name": n, "value": round(v, 2),
                 "distance_pct": round((v - last_price) / last_price * 100.0, 2)
                                 if last_price else 0}
                for n, v in levels_list]

    return {
        "symbol": symbol,
        "name": _display_name(symbol),
        "price": round(last_price, 2),
        "prev_high": round(high, 2),
        "prev_low": round(low, 2),
        "prev_close": round(close, 2),
        "classic": _fmt([
            ("PP", pp), ("R1", r1), ("R2", r2), ("R3", r3),
            ("S1", s1), ("S2", s2), ("S3", s3),
        ]),
        "fibonacci": _fmt([
            ("R3", fib_r3), ("R2", fib_r2), ("R1", fib_r1),
            ("PP", pp),
            ("S1", fib_s1), ("S2", fib_s2), ("S3", fib_s3),
        ]),
        "camarilla": _fmt([
            ("R4", cam_r4), ("R3", cam_r3), ("R2", cam_r2), ("R1", cam_r1),
            ("S1", cam_s1), ("S2", cam_s2), ("S3", cam_s3), ("S4", cam_s4),
        ]),
        "confluences": confluences[:6],
    }


# ─────────────────────────────────────────────────────────────────────────
# 5. Momentum burst scanner
# ─────────────────────────────────────────────────────────────────────────

def _momentum_for_symbol(symbol: str, lookback_min: int = 30) -> Optional[Dict[str, Any]]:
    df = market_data.get_history(symbol, days=2, interval="5m")
    if df is None or df.empty or len(df) < 10:
        return None

    bars = max(2, lookback_min // 5)
    recent = df.tail(bars + 1)
    if len(recent) < 2:
        return None

    start_price = _safe_float(recent["Close"].iloc[0])
    end_price = _safe_float(recent["Close"].iloc[-1])
    burst_pct = ((end_price - start_price) / start_price * 100.0) if start_price else 0.0

    if abs(burst_pct) < 0.5:
        return None  # filter noise

    # Volume of burst period vs typical 5-min bar volume of last 5 trading days
    burst_vol = _safe_float(recent["Volume"].tail(bars).sum())
    avg_5m_vol = _safe_float(df["Volume"].tail(75).mean())  # ~75 bars = ~1 day
    expected = avg_5m_vol * bars
    vol_thrust = burst_vol / expected if expected > 0 else 0

    # Daily context
    daily = market_data.get_history(symbol, days=5, interval="1d")
    prev_close = (_safe_float(daily["Close"].iloc[-2])
                  if daily is not None and len(daily) >= 2 else start_price)
    day_change_pct = ((end_price - prev_close) / prev_close * 100.0) if prev_close else 0

    direction = "up" if burst_pct > 0 else "down"
    score = round(burst_pct * (1 + min(2, vol_thrust)), 2)

    return {
        "symbol": symbol,
        "name": _display_name(symbol),
        "price": round(end_price, 2),
        "burst_pct": round(burst_pct, 2),
        "vol_thrust": round(vol_thrust, 2),
        "day_change_pct": round(day_change_pct, 2),
        "direction": direction,
        "score": score,
        "lookback_min": lookback_min,
    }


def momentum_burst(lookback_min: int = 30, force: bool = False) -> Dict[str, Any]:
    key = f"intra:momentum:{lookback_min}"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    results = _parallel(lambda s: _momentum_for_symbol(s, lookback_min),
                        UNIVERSE, max_workers=10)
    results.sort(key=lambda r: abs(r["score"]), reverse=True)

    payload = {
        "stocks": results,
        "up": sum(1 for r in results if r["direction"] == "up"),
        "down": sum(1 for r in results if r["direction"] == "down"),
        "lookback_min": lookback_min,
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=45)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 6. Index basis monitor (spot vs estimated fair value of futures)
# ─────────────────────────────────────────────────────────────────────────

def _basis_expiry_dte(today: _dt.date) -> tuple[_dt.date, int]:
    """Return the current-month NSE F&O expiry (last Thursday) and DTE."""
    if today.month == 12:
        next_month = _dt.date(today.year + 1, 1, 1)
    else:
        next_month = _dt.date(today.year, today.month + 1, 1)
    last_day = next_month - _dt.timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7  # Thursday = 3
    last_thu = last_day - _dt.timedelta(days=offset)
    if last_thu < today:
        if next_month.month == 12:
            nm2 = _dt.date(next_month.year + 1, 1, 1)
        else:
            nm2 = _dt.date(next_month.year, next_month.month + 1, 1)
        last_day = nm2 - _dt.timedelta(days=1)
        offset = (last_day.weekday() - 3) % 7
        last_thu = last_day - _dt.timedelta(days=offset)
    return last_thu, max(1, (last_thu - today).days)


# (snapshot symbol, display name, yfinance spot symbol)
_BASIS_INDEX_MAP: List[tuple[str, str, str]] = [
    ("NIFTY",      "NIFTY 50",          "^NSEI"),
    ("BANKNIFTY",  "BANK NIFTY",        "^NSEBANK"),
    ("FINNIFTY",   "FIN NIFTY",         "NIFTY_FIN_SERVICE.NS"),
    ("MIDCPNIFTY", "NIFTY MIDCAP",      "^NSEMDCP50"),
    ("SENSEX",     "SENSEX",            "^BSESN"),
]


def _parse_nse_date(s) -> Optional[_dt.date]:
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            return _dt.datetime.strptime(str(s), fmt).date()
        except ValueError:
            continue
    return None


def _fetch_index_futures_rows() -> List[Dict[str, Any]]:
    """Pull FUTIDX rows from NSE's live derivatives snapshot."""
    from application.services.option_chain import _nse_get_json
    try:
        raw = _nse_get_json("/api/snapshot-derivatives-equity",
                            {"index": "futures"}, retries=2)
    except Exception as e:
        log.warning("index_basis: NSE snapshot fetch failed: %s", e)
        return []
    if not isinstance(raw, dict):
        return []
    data = raw.get("data") or []
    return [c for c in data if (c.get("instrument") or "").upper() == "FUTIDX"]


def _pick_near_month_fut(rows: List[Dict[str, Any]], snap_sym: str,
                         today: _dt.date) -> Optional[Dict[str, Any]]:
    cands = [r for r in rows
             if (r.get("symbol") or "").upper() == snap_sym.upper()]
    parsed: List[tuple[_dt.date, Dict[str, Any]]] = []
    for r in cands:
        d = _parse_nse_date(r.get("expiryDate"))
        if d and d >= today:
            parsed.append((d, r))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])
    return parsed[0][1]


def _f(v) -> Optional[float]:
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _spot_quote(spot_sym: str) -> Optional[Dict[str, Any]]:
    """Live spot price + day-open + prev close, with daily-history fallback."""
    out: Dict[str, Any] = {}
    try:
        q = market_data.get_quote(spot_sym) or {}
        out["price"] = _f(q.get("price"))
        out["change_pct"] = _f(q.get("change_pct"))
    except Exception:
        pass
    if not out.get("price"):
        try:
            daily = market_data.get_history(spot_sym, days=5, interval="1d")
            if daily is not None and not daily.empty:
                out["price"] = _safe_float(daily["Close"].iloc[-1])
                if len(daily) >= 2:
                    prev = _safe_float(daily["Close"].iloc[-2])
                    if prev:
                        out["change_pct"] = (out["price"] - prev) / prev * 100
        except Exception:
            return None
    return out if out.get("price") else None


def _basis_row(snap_sym: str, name: str, spot_sym: str,
               fut_rows: List[Dict[str, Any]],
               today: _dt.date) -> Optional[Dict[str, Any]]:
    fut = _pick_near_month_fut(fut_rows, snap_sym, today)
    spot = _spot_quote(spot_sym)
    if not spot or not spot.get("price"):
        return None

    spot_ltp = spot["price"]
    spot_chg = spot.get("change_pct")

    fut_ltp = _f(fut.get("lastPrice")) if fut else None
    fut_chg = _f(fut.get("pChange")) if fut else None
    fut_open = _f(fut.get("openPrice")) if fut else None
    expiry_str = (fut or {}).get("expiryDate") or ""
    expiry_date = _parse_nse_date(expiry_str) or _basis_expiry_dte(today)[0]
    dte = max(1, (expiry_date - today).days)

    # Cost-of-carry fair value: Spot * (1 + r * dte/365), r = 6.5% (RBI repo).
    r = 0.065
    fair_value = spot_ltp * (1 + r * dte / 365.0)

    basis = (fut_ltp - spot_ltp) if fut_ltp is not None else None
    basis_pct = (basis / spot_ltp * 100) if (basis is not None and spot_ltp) else None
    prem_vs_fair = (fut_ltp - fair_value) if fut_ltp is not None else None

    # Intraday basis-shift: open-basis vs current basis (proxy for "flip").
    open_basis = None
    if fut_open is not None:
        open_basis = fut_open - spot_ltp  # spot-open is rarely available; use ltp as
                                          # neutral baseline so sign comparison is still valid.
    basis_shift = (basis - open_basis) if (basis is not None and open_basis is not None) else None
    flipped = bool(basis is not None and open_basis is not None
                   and ((basis >= 0) != (open_basis >= 0)))

    if basis is None:
        state = "no_future"
    elif basis_pct is None:
        state = "flat"
    elif basis_pct > 0.05:
        state = "premium"
    elif basis_pct < -0.05:
        state = "discount"
    else:
        state = "at_par"

    return {
        "name": name,
        "snap_symbol": snap_sym,
        "spot_symbol": spot_sym,
        "spot": round(spot_ltp, 2),
        "spot_change_pct": round(spot_chg, 2) if spot_chg is not None else None,
        "future": round(fut_ltp, 2) if fut_ltp is not None else None,
        "future_change_pct": round(fut_chg, 2) if fut_chg is not None else None,
        "fair_value": round(fair_value, 2),
        "basis": round(basis, 2) if basis is not None else None,
        "basis_pct": round(basis_pct, 3) if basis_pct is not None else None,
        "premium_vs_fair": round(prem_vs_fair, 2) if prem_vs_fair is not None else None,
        "basis_shift": round(basis_shift, 2) if basis_shift is not None else None,
        "flipped": flipped,
        "state": state,
        "days_to_expiry": dte,
        "expiry": expiry_date.isoformat(),
    }


def _basis_fii_net() -> Optional[float]:
    """Today's FII cash-market net (₹ Cr). None if unavailable."""
    try:
        from application.services.swing_tools import fii_dii_overlay
        d = fii_dii_overlay()
        return d.get("fii_net_cr")
    except Exception as e:
        log.debug("index_basis: fii overlay unavailable: %s", e)
        return None


def _basis_verdict(row: Dict[str, Any], fii_net: Optional[float]) -> str:
    state = row.get("state")
    if state in ("no_future", "flat"):
        return "Awaiting live futures data"
    if row.get("flipped"):
        return "Basis flip — watch for intraday reversal"
    if state == "premium":
        if fii_net is not None and fii_net > 0:
            return "Bullish institutional flow confirmed"
        if fii_net is not None and fii_net < 0:
            return "Premium but FII selling — divergence"
        return "Bullish futures positioning"
    if state == "discount":
        if fii_net is not None and fii_net < 0:
            return "Bearish institutional flow confirmed"
        if fii_net is not None and fii_net > 0:
            return "Discount but FII buying — divergence"
        return "Bearish futures positioning"
    return "Neutral — no clear positioning"


def index_basis(force: bool = False) -> Dict[str, Any]:
    key = "intra:basis:v2"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    today = _now_ist().date()
    fut_rows = _fetch_index_futures_rows()
    fii_net = _basis_fii_net()

    rows: List[Dict[str, Any]] = []
    for snap_sym, name, spot_sym in _BASIS_INDEX_MAP:
        r = _basis_row(snap_sym, name, spot_sym, fut_rows, today)
        if r is None:
            continue
        r["verdict"] = _basis_verdict(r, fii_net)
        rows.append(r)

    payload = {
        "indices": rows,
        "fii_net_cr": fii_net,
        "fii_sentiment": ("buying" if (fii_net or 0) > 0
                          else "selling" if (fii_net or 0) < 0
                          else "flat"),
        "note": ("Basis = Future − Spot. Premium vs Fair = Future − Spot×(1+6.5%×DTE/365). "
                 "FII Net is today's cash-market flow (₹ Cr) from NSE."),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=45)
    except Exception:
        pass
    return payload


# -----------------------------------------------------------------------
# Sector Rotation Ticker
# -----------------------------------------------------------------------

SECTOR_INDICES = {
    "NIFTY 50": "^NSEI",
    "BANK NIFTY": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "NIFTY AUTO": "^CNXAUTO",
    "NIFTY PHARMA": "^CNXPHARMA",
    "NIFTY FMCG": "^CNXFMCG",
    "NIFTY METAL": "^CNXMETAL",
    "NIFTY REALTY": "^CNXREALTY",
    "NIFTY ENERGY": "^CNXENERGY",
    "NIFTY FIN SERVICE": "NIFTY_FIN_SERVICE.NS",
}


def _sector_rot_one(name: str, symbol: str) -> Optional[Dict[str, Any]]:
    try:
        # Try 15m intraday first; fall back to daily if unavailable
        df = market_data.get_history(symbol, days=2, interval="15m")
        if df is None or df.empty or len(df) < 4:
            df = market_data.get_history(symbol, days=5, interval="1d")
        if df is None or df.empty or len(df) < 2:
            return None
        df = df.copy()

        # Determine if we have intraday (datetime index) or daily data
        is_intraday = hasattr(df.index, 'hour') and df.index[-1].hour > 0

        if is_intraday and hasattr(df.index, "date"):
            today = df.index.max().date()
            tdf = df[df.index.date == today]
            ydf = df[df.index.date < today]
            if tdf.empty:
                # Market not open yet — use last available day
                today = df.index.max().date()
                tdf = df[df.index.date == today]
                ydf = df[df.index.date < today]
        else:
            # Daily candles fallback
            tdf = df.tail(1)
            ydf = df.iloc[:-1]

        if tdf.empty:
            return None
        open_today = _safe_float(tdf.iloc[0]["Open"])
        ltp = _safe_float(tdf.iloc[-1]["Close"])
        prev_close = _safe_float(ydf.iloc[-1]["Close"]) if not ydf.empty else open_today
        day_chg = (ltp - prev_close) / prev_close * 100 if prev_close else 0.0
        # Momentum delta: last 4 bars (1 hour) move for intraday, or day change for daily
        if is_intraday and len(tdf) >= 2:
            recent = tdf.tail(5)
            mom_pct = ((recent.iloc[-1]["Close"] - recent.iloc[0]["Close"]) /
                       recent.iloc[0]["Close"] * 100)
        else:
            mom_pct = day_chg
        return {
            "name": name, "symbol": symbol,
            "ltp": round(ltp, 2),
            "day_change_pct": round(day_chg, 2),
            "momentum_1h_pct": round(_safe_float(mom_pct), 2),
        }
    except Exception as e:
        log.warning("sector_rot_one failed for %s (%s): %s", name, symbol, e)
        return None


def sector_rotation(force: bool = False) -> Dict[str, Any]:
    key = "intraday:sector_rot:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}
    rows = _parallel(lambda kv: _sector_rot_one(kv[0], kv[1]),
                     list(SECTOR_INDICES.items()), max_workers=8)
    rows = [r for r in rows if r]
    rows.sort(key=lambda r: r["day_change_pct"], reverse=True)
    if rows:
        leader = rows[0]; laggard = rows[-1]
        rotation_spread = leader["day_change_pct"] - laggard["day_change_pct"]
    else:
        leader = laggard = None; rotation_spread = 0
    payload = {
        "sectors": rows,
        "leader": leader, "laggard": laggard,
        "rotation_spread_pct": round(rotation_spread, 2),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=90)
    except Exception:
        pass
    return payload


# -----------------------------------------------------------------------
# Live News / Tweet Sentiment Tagger
# -----------------------------------------------------------------------

_POS_WORDS = {"surge", "jump", "gain", "rally", "beat", "upgrade", "buy", "bullish",
              "strong", "growth", "profit", "record", "high", "outperform", "expand",
              "win", "approval", "launch", "partnership", "raise"}
_NEG_WORDS = {"plunge", "fall", "drop", "miss", "downgrade", "sell", "bearish",
              "weak", "loss", "decline", "low", "underperform", "cut", "concern",
              "probe", "fraud", "fine", "delay", "warning", "lawsuit", "default"}


def _sentiment_text(text: str) -> int:
    t = (text or "").lower()
    pos = sum(1 for w in _POS_WORDS if w in t)
    neg = sum(1 for w in _NEG_WORDS if w in t)
    return pos - neg


def _news_sent_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        from application.services.rag import retriever as _ret
        docs = _ret.retrieve(symbol, top_k=5, days_back=2)
        if not docs:
            return None
        scores = []
        latest_title = ""
        latest_age = ""
        latest_url = ""
        for d in docs:
            txt = (d.get("Title") or "") + " " + (d.get("Snippet") or d.get("Summary") or "")
            s = _sentiment_text(txt)
            scores.append(s)
            if not latest_title:
                latest_title = d.get("Title", "")[:140]
                latest_age = d.get("PublishedAt", "")[:10]
                latest_url = d.get("Url", "")
        if not scores:
            return None
        agg = sum(scores) / len(scores)
        verdict = "bullish" if agg > 0.4 else "bearish" if agg < -0.4 else "neutral"
        return {
            "symbol": symbol, "name": _display_name(symbol),
            "news_count": len(docs),
            "sentiment_score": round(agg, 2),
            "verdict": verdict,
            "headline": latest_title or "--",
            "published": latest_age or "--",
            "url": latest_url or "",
        }
    except Exception:
        return None


def news_sentiment(force: bool = False) -> Dict[str, Any]:
    key = "intraday:news_sent:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}
    # Sample top ~40 from universe to keep it light
    sample = UNIVERSE[:40]
    results = _parallel(_news_sent_for_symbol, sample, max_workers=10)
    results = [r for r in results if r]
    # Sort: most positive then most negative for visibility
    results.sort(key=lambda r: r["sentiment_score"], reverse=True)
    payload = {
        "stocks": results,
        "bullish": sum(1 for r in results if r["verdict"] == "bullish"),
        "bearish": sum(1 for r in results if r["verdict"] == "bearish"),
        "neutral": sum(1 for r in results if r["verdict"] == "neutral"),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=180)
    except Exception:
        pass
    return payload
