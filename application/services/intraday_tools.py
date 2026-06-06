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

def _basis_for_index(name: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Spot + simple cost-of-carry fair value for the current-month future.

    Without futures-symbol resolution we approximate the fair value as
    ``spot * (1 + r * days_to_expiry / 365)`` with r = 6.5% (current
    Indian risk-free), and treat any *real* premium/discount visible
    via the daily change/intraday move as the live basis indicator.
    Since we don't always have a futures quote, we surface the spot,
    today's change, and the theoretical fair value so the user can
    compare against their broker's live futures LTP.
    """
    daily = market_data.get_history(symbol, days=5, interval="1d")
    if daily is None or daily.empty:
        return None
    last = _safe_float(daily["Close"].iloc[-1])
    prev = (_safe_float(daily["Close"].iloc[-2])
            if len(daily) >= 2 else last)
    if last <= 0:
        return None
    change_pct = ((last - prev) / prev * 100.0) if prev else 0

    # Days to last Thursday of current month (NSE F&O expiry)
    today = _now_ist().date()
    # Find last Thursday of current month
    if today.month == 12:
        next_month = _dt.date(today.year + 1, 1, 1)
    else:
        next_month = _dt.date(today.year, today.month + 1, 1)
    last_day = next_month - _dt.timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7  # Thursday = 3
    last_thu = last_day - _dt.timedelta(days=offset)
    if last_thu < today:
        # Roll to next month
        if next_month.month == 12:
            nm2 = _dt.date(next_month.year + 1, 1, 1)
        else:
            nm2 = _dt.date(next_month.year, next_month.month + 1, 1)
        last_day = nm2 - _dt.timedelta(days=1)
        offset = (last_day.weekday() - 3) % 7
        last_thu = last_day - _dt.timedelta(days=offset)
    dte = max(1, (last_thu - today).days)

    r = 0.065
    fair_value = last * (1 + r * dte / 365.0)

    return {
        "name": name,
        "symbol": symbol,
        "spot": round(last, 2),
        "change_pct": round(change_pct, 2),
        "fair_value": round(fair_value, 2),
        "days_to_expiry": dte,
        "expiry": last_thu.isoformat(),
    }


def index_basis(force: bool = False) -> Dict[str, Any]:
    key = "intra:basis:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    rows = []
    for name, sym in INDICES.items():
        r = _basis_for_index(name, sym)
        if r:
            rows.append(r)

    payload = {
        "indices": rows,
        "note": ("Fair value = Spot Ã-- (1 + 6.5% Ã-- DTE/365). Compare with your "
                 "broker's live futures LTP to compute live premium/discount."),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=60)
    except Exception:
        pass
    return payload


# -----------------------------------------------------------------------
# VWAP Deviation Scanner
# -----------------------------------------------------------------------

def _vwap_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        df = market_data.get_history(symbol, days=2, interval="5m")
        if df is None or df.empty or len(df) < 6:
            return None
        df = df.copy()
        if hasattr(df.index, "date"):
            today = df.index.max().date()
            today_df = df[df.index.date == today]
        else:
            today_df = df.tail(75)
        if today_df.empty:
            today_df = df.tail(75)
        typical = (today_df["High"] + today_df["Low"] + today_df["Close"]) / 3.0
        vol = today_df["Volume"].astype(float)
        cum_v = vol.cumsum()
        if cum_v.iloc[-1] <= 0:
            return None
        vwap = (typical * vol).cumsum() / cum_v.replace(0, np.nan)
        last = today_df.iloc[-1]
        v = _safe_float(vwap.iloc[-1])
        ltp = _safe_float(last["Close"])
        if v <= 0 or ltp <= 0:
            return None
        dev = (ltp - v) / v * 100.0
        # Rising volume = last 3 bars avg vs prior 12 bars
        recent_v = vol.tail(3).mean()
        base_v = vol.iloc[-15:-3].mean() if len(vol) > 15 else vol.mean()
        vol_ratio = recent_v / base_v if base_v > 0 else 1.0
        side = "long" if dev > 0 else "short"
        signal = "trend-continuation" if vol_ratio > 1.3 else "mean-reversion"
        return {
            "symbol": symbol, "name": _display_name(symbol),
            "ltp": round(ltp, 2), "vwap": round(v, 2),
            "deviation_pct": round(dev, 2),
            "abs_deviation": round(abs(dev), 2),
            "volume_ratio": round(vol_ratio, 2),
            "side": side, "signal": signal,
        }
    except Exception:
        return None


def vwap_deviation_scan(min_abs_dev: float = 2.0, force: bool = False) -> Dict[str, Any]:
    key = f"intraday:vwap:{min_abs_dev}"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}
    results = _parallel(_vwap_for_symbol, UNIVERSE, max_workers=10)
    results = [r for r in results if r and r["abs_deviation"] >= min_abs_dev]
    results.sort(key=lambda r: r["abs_deviation"], reverse=True)
    payload = {
        "stocks": results,
        "long_count": sum(1 for r in results if r["side"] == "long"),
        "short_count": sum(1 for r in results if r["side"] == "short"),
        "min_dev": min_abs_dev,
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=60)
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
        df = market_data.get_history(symbol, days=2, interval="15m")
        if df is None or df.empty or len(df) < 4:
            return None
        df = df.copy()
        if hasattr(df.index, "date"):
            today = df.index.max().date()
            tdf = df[df.index.date == today]
            ydf = df[df.index.date < today]
        else:
            tdf = df.tail(25); ydf = df.iloc[:-25]
        if tdf.empty:
            return None
        open_today = _safe_float(tdf.iloc[0]["Open"])
        ltp = _safe_float(tdf.iloc[-1]["Close"])
        prev_close = _safe_float(ydf.iloc[-1]["Close"]) if not ydf.empty else open_today
        day_chg = (ltp - prev_close) / prev_close * 100 if prev_close else 0.0
        # Momentum delta: last 4 bars (1 hour) move
        recent = tdf.tail(5)
        mom_pct = ((recent.iloc[-1]["Close"] - recent.iloc[0]["Close"]) /
                   recent.iloc[0]["Close"] * 100) if len(recent) >= 2 else 0.0
        return {
            "name": name, "symbol": symbol,
            "ltp": round(ltp, 2),
            "day_change_pct": round(day_chg, 2),
            "momentum_1h_pct": round(_safe_float(mom_pct), 2),
        }
    except Exception:
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
