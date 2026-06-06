"""Swing tool-suite — breakouts, relative strength, chart patterns, sector
leaders, options-confirmed signals, FII/DII overlay.
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


# Sector mapping (reuse heatmap's structure)
try:
    from application.routes.heatmap import SECTOR_STOCKS as _SECTOR_MAP
    SECTOR_STOCKS: Dict[str, List[str]] = dict(_SECTOR_MAP)
except Exception:
    SECTOR_STOCKS = {}

try:
    from application.services.swing_scanner import UNIVERSE as _SWING_UNIVERSE
    UNIVERSE: List[str] = list(_SWING_UNIVERSE)
except Exception:
    UNIVERSE = []

NIFTY_SYMBOL = "^NSEI"


def _safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _now_ist():
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


def _get_daily(symbol: str, days: int = 200):
    return market_data.get_history(symbol, days=days, interval="1d")


# ─────────────────────────────────────────────────────────────────────────
# 1. Breakout from consolidation (NR7 / Inside Bar / VCP-style)
# ─────────────────────────────────────────────────────────────────────────

def _breakout_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    df = _get_daily(symbol, 120)
    if df is None or df.empty or len(df) < 50:
        return None

    closes = df["Close"]
    highs = df["High"]
    lows = df["Low"]
    vols = df["Volume"]

    last_close = _safe_float(closes.iloc[-1])
    last_high = _safe_float(highs.iloc[-1])
    last_low = _safe_float(lows.iloc[-1])
    last_vol = _safe_float(vols.iloc[-1])

    # Daily ranges
    ranges = (highs - lows).tail(30)
    if len(ranges) < 7:
        return None

    # NR7 — today's range is smallest of last 7 days
    last_range = _safe_float(ranges.iloc[-1])
    nr7 = bool(last_range == ranges.tail(7).min())

    # Inside bar — today inside yesterday
    inside_bar = bool(last_high <= _safe_float(highs.iloc[-2])
                      and last_low >= _safe_float(lows.iloc[-2]))

    # Bollinger Band width squeeze (lowest 20% of last 60 days)
    sma20 = closes.rolling(20).mean()
    std20 = closes.rolling(20).std()
    bb_width = ((sma20 + 2 * std20) - (sma20 - 2 * std20)) / sma20
    bb_width_now = _safe_float(bb_width.iloc[-1])
    bb_width_60 = bb_width.tail(60).dropna()
    if len(bb_width_60) < 10:
        return None
    bb_pct = (bb_width_60 <= bb_width_now).mean()
    squeeze = bool(bb_pct <= 0.20)

    # Consolidation: last N days within ±5% range
    last_20 = closes.tail(20)
    range_pct_20 = (last_20.max() - last_20.min()) / last_20.mean() * 100.0
    consolidating = bool(range_pct_20 < 8.0)

    # Breakout above 20-day high on volume?
    high_20 = _safe_float(highs.tail(21).iloc[:-1].max())
    avg_vol_20 = _safe_float(vols.tail(20).mean())
    vol_ratio = (last_vol / avg_vol_20) if avg_vol_20 > 0 else 0
    breakout = bool(last_close > high_20 * 1.001)

    # Need at least one consolidation signal
    if not (nr7 or inside_bar or squeeze or consolidating or breakout):
        return None

    setup = []
    if breakout: setup.append("20-day high breakout")
    if squeeze: setup.append("BB squeeze (bottom 20%)")
    if consolidating: setup.append(f"20-day range only {range_pct_20:.1f}%")
    if nr7: setup.append("NR7 (tightest in 7 days)")
    if inside_bar: setup.append("Inside bar")

    score = 0
    if breakout and vol_ratio > 1.5: score += 40
    elif breakout: score += 25
    if squeeze: score += 25
    if consolidating: score += 15
    if nr7: score += 10
    if inside_bar: score += 10

    return {
        "symbol": symbol,
        "name": _display_name(symbol),
        "price": round(last_close, 2),
        "range_20d_pct": round(range_pct_20, 2),
        "bb_width_pct": round(bb_pct * 100, 1),
        "vol_ratio": round(vol_ratio, 2),
        "breakout": breakout,
        "squeeze": squeeze,
        "consolidating": consolidating,
        "nr7": nr7,
        "inside_bar": inside_bar,
        "high_20d": round(high_20, 2),
        "setup": setup,
        "score": int(score),
    }


def breakout_consolidation(force: bool = False) -> Dict[str, Any]:
    key = "swing:breakout:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}
    results = _parallel(_breakout_for_symbol, UNIVERSE, max_workers=8)
    results.sort(key=lambda r: r["score"], reverse=True)
    payload = {
        "stocks": results,
        "active_breakouts": sum(1 for r in results if r["breakout"]),
        "squeezes": sum(1 for r in results if r["squeeze"]),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=600)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 2. Relative Strength vs NIFTY (Mansfield)
# ─────────────────────────────────────────────────────────────────────────

def _rs_for_symbol(symbol: str, nifty_close: pd.Series) -> Optional[Dict[str, Any]]:
    df = _get_daily(symbol, 200)
    if df is None or df.empty or len(df) < 130:
        return None
    closes = df["Close"]
    last = _safe_float(closes.iloc[-1])

    # Returns over 4/12/26 weeks (~20/60/130 trading days)
    def _ret(lb):
        if len(closes) < lb + 1:
            return 0.0
        return (_safe_float(closes.iloc[-1]) / _safe_float(closes.iloc[-lb - 1]) - 1) * 100.0

    def _nret(lb):
        if len(nifty_close) < lb + 1:
            return 0.0
        return (_safe_float(nifty_close.iloc[-1]) / _safe_float(nifty_close.iloc[-lb - 1]) - 1) * 100.0

    r4 = _ret(20); r12 = _ret(60); r26 = _ret(130)
    n4 = _nret(20); n12 = _nret(60); n26 = _nret(130)

    rs4 = r4 - n4
    rs12 = r12 - n12
    rs26 = r26 - n26

    # Mansfield RS = (price / nifty) / 52w-MA(price/nifty) − 1, in pct
    ratio = (closes / nifty_close.reindex(closes.index).ffill())
    ratio_ma = ratio.rolling(52 * 5).mean()
    if len(ratio_ma.dropna()) > 0:
        mansfield = (_safe_float(ratio.iloc[-1]) / _safe_float(ratio_ma.iloc[-1]) - 1) * 100.0
    else:
        mansfield = 0.0

    # Composite RS rating (0-100 scale)
    rs_rating = max(0, min(100, int(50 + (rs4 * 0.3 + rs12 * 0.4 + rs26 * 0.3))))

    return {
        "symbol": symbol,
        "name": _display_name(symbol),
        "price": round(last, 2),
        "ret_4w_pct": round(r4, 2),
        "ret_12w_pct": round(r12, 2),
        "ret_26w_pct": round(r26, 2),
        "rs_4w_pct": round(rs4, 2),
        "rs_12w_pct": round(rs12, 2),
        "rs_26w_pct": round(rs26, 2),
        "mansfield_rs": round(mansfield, 2),
        "rs_rating": rs_rating,
    }


def relative_strength(force: bool = False) -> Dict[str, Any]:
    key = "swing:rs:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    nifty = _get_daily(NIFTY_SYMBOL, 200)
    if nifty is None or nifty.empty:
        return {"stocks": [], "error": "NIFTY data unavailable"}

    nifty_close = nifty["Close"]
    results = _parallel(lambda s: _rs_for_symbol(s, nifty_close), UNIVERSE, max_workers=8)
    results.sort(key=lambda r: r["rs_rating"], reverse=True)

    payload = {
        "stocks": results,
        "leaders": sum(1 for r in results if r["rs_rating"] >= 70),
        "laggards": sum(1 for r in results if r["rs_rating"] <= 30),
        "nifty_ret_4w": round(_safe_float((nifty_close.iloc[-1] / nifty_close.iloc[-21] - 1) * 100)
                              if len(nifty_close) >= 22 else 0, 2),
        "nifty_ret_12w": round(_safe_float((nifty_close.iloc[-1] / nifty_close.iloc[-61] - 1) * 100)
                               if len(nifty_close) >= 62 else 0, 2),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=15 * 60)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 3. Chart pattern detection (rule-based, lightweight)
# ─────────────────────────────────────────────────────────────────────────

def _detect_double_bottom(highs, lows, closes) -> Optional[Dict[str, Any]]:
    # Look for two lows within ±3% over last 60 days, with a higher mid
    last_n = 60
    if len(lows) < last_n:
        return None
    L = lows.tail(last_n).values
    H = highs.tail(last_n).values
    idx_min1 = int(np.argmin(L[:last_n // 2]))
    idx_min2 = int(last_n // 2 + np.argmin(L[last_n // 2:]))
    v1, v2 = L[idx_min1], L[idx_min2]
    if v1 <= 0 or v2 <= 0:
        return None
    if abs(v1 - v2) / v1 > 0.03:
        return None
    peak_between = H[idx_min1:idx_min2].max() if idx_min2 > idx_min1 + 2 else 0
    if peak_between < v1 * 1.04:
        return None
    last_close = closes.iloc[-1]
    confirmed = bool(last_close > peak_between)
    return {"pattern": "Double Bottom", "confirmed": confirmed,
            "neckline": round(float(peak_between), 2),
            "support": round(float(min(v1, v2)), 2)}


def _detect_ascending_triangle(highs, lows, closes) -> Optional[Dict[str, Any]]:
    last_n = 40
    if len(highs) < last_n:
        return None
    H = highs.tail(last_n).values
    L = lows.tail(last_n).values
    # Flat resistance: top 5 highs within 2% of each other
    top5 = np.sort(H)[-5:]
    if (top5.max() - top5.min()) / top5.mean() > 0.02:
        return None
    # Rising support: linear regression slope of lows > 0
    x = np.arange(len(L))
    slope, _ = np.polyfit(x, L, 1)
    if slope <= 0:
        return None
    resistance = float(top5.mean())
    confirmed = bool(closes.iloc[-1] > resistance * 1.005)
    return {"pattern": "Ascending Triangle", "confirmed": confirmed,
            "resistance": round(resistance, 2),
            "slope": round(float(slope), 4)}


def _detect_bull_flag(highs, lows, closes) -> Optional[Dict[str, Any]]:
    if len(closes) < 30:
        return None
    # Strong run: 20→10 days ago up >10%
    if closes.iloc[-21] <= 0:
        return None
    pole = (closes.iloc[-11] / closes.iloc[-21] - 1) * 100.0
    if pole < 8:
        return None
    # Recent 10 days: pullback or tight consolidation (≤5%)
    recent = closes.tail(10)
    pullback = (recent.max() - recent.min()) / recent.mean() * 100.0
    if pullback > 7:
        return None
    confirmed = bool(closes.iloc[-1] >= recent.max())
    return {"pattern": "Bull Flag", "confirmed": confirmed,
            "pole_gain_pct": round(float(pole), 2),
            "flag_range_pct": round(float(pullback), 2)}


def _detect_cup_handle(highs, lows, closes) -> Optional[Dict[str, Any]]:
    if len(closes) < 80:
        return None
    seg = closes.tail(80).reset_index(drop=True)
    # Rough cup: start ≈ end, deepest dip in middle ~15-30% below
    start, end = seg.iloc[:5].mean(), seg.iloc[-15:-5].mean()
    bottom = seg.iloc[20:60].min()
    if start <= 0 or bottom <= 0:
        return None
    if abs(start - end) / start > 0.05:
        return None
    depth = (start - bottom) / start * 100.0
    if not (12 <= depth <= 35):
        return None
    # Handle: last 10 bars pulled back 3-12% from end
    handle_low = seg.iloc[-10:].min()
    handle_pullback = (end - handle_low) / end * 100.0
    if not (3 <= handle_pullback <= 12):
        return None
    confirmed = bool(closes.iloc[-1] > end)
    return {"pattern": "Cup & Handle", "confirmed": confirmed,
            "cup_depth_pct": round(float(depth), 2),
            "handle_pullback_pct": round(float(handle_pullback), 2)}


def _patterns_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    df = _get_daily(symbol, 120)
    if df is None or df.empty or len(df) < 60:
        return None
    found = []
    for fn in (_detect_double_bottom, _detect_ascending_triangle,
               _detect_bull_flag, _detect_cup_handle):
        try:
            r = fn(df["High"], df["Low"], df["Close"])
            if r:
                found.append(r)
        except Exception as e:
            log.debug("pattern detect error %s on %s: %s", fn.__name__, symbol, e)
    if not found:
        return None
    last_close = _safe_float(df["Close"].iloc[-1])
    return {
        "symbol": symbol,
        "name": _display_name(symbol),
        "price": round(last_close, 2),
        "patterns": found,
        "confirmed_count": sum(1 for p in found if p.get("confirmed")),
    }


def chart_patterns(force: bool = False) -> Dict[str, Any]:
    key = "swing:patterns:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}
    results = _parallel(_patterns_for_symbol, UNIVERSE, max_workers=8)
    results.sort(key=lambda r: (r["confirmed_count"], len(r["patterns"])), reverse=True)
    payload = {
        "stocks": results,
        "total": len(results),
        "confirmed": sum(1 for r in results if r["confirmed_count"] > 0),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=15 * 60)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 4. Sector leaders — top RS per sector
# ─────────────────────────────────────────────────────────────────────────

def sector_leaders(force: bool = False) -> Dict[str, Any]:
    key = "swing:sector_leaders:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    nifty = _get_daily(NIFTY_SYMBOL, 200)
    if nifty is None or nifty.empty:
        return {"sectors": [], "error": "NIFTY data unavailable"}
    nifty_close = nifty["Close"]

    sector_results: List[Dict[str, Any]] = []
    for sector, stocks in SECTOR_STOCKS.items():
        scored = _parallel(lambda s: _rs_for_symbol(s, nifty_close), stocks, max_workers=6)
        scored.sort(key=lambda r: r["rs_rating"], reverse=True)
        if not scored:
            continue
        # Sector aggregate: avg 12w return of constituents
        avg_12w = sum(s["ret_12w_pct"] for s in scored) / len(scored)
        nifty_12w = (_safe_float((nifty_close.iloc[-1] / nifty_close.iloc[-61] - 1) * 100)
                     if len(nifty_close) >= 62 else 0)
        sector_results.append({
            "sector": sector,
            "avg_ret_12w_pct": round(avg_12w, 2),
            "sector_rs_vs_nifty": round(avg_12w - nifty_12w, 2),
            "leaders": scored[:3],
            "stock_count": len(scored),
        })

    sector_results.sort(key=lambda r: r["sector_rs_vs_nifty"], reverse=True)
    payload = {
        "sectors": sector_results,
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=20 * 60)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 5. Options-confirmed swing signals (uses NIFTY option chain PCR + buildup)
# ─────────────────────────────────────────────────────────────────────────

def options_confirmed_swing(force: bool = False) -> Dict[str, Any]:
    """Cross-reference swing breakouts with NIFTY option chain bias.

    Logic:
      - Pull NIFTY option chain (PCR, max-pain, buildup).
      - Determine market bias (bullish if PCR > 1.1 & long-buildup dominant).
      - Filter swing-breakout candidates aligned with the bias.
    """
    key = "swing:opt_confirmed:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    try:
        from application.services import option_chain as oc
        oc_data = oc.get_nifty_option_chain()
    except Exception as e:
        log.warning("option chain fetch failed: %s", e)
        oc_data = None

    pcr = None
    bias = "neutral"
    max_pain = None
    if oc_data:
        ind = oc_data.get("indicator") or {}
        pcr = ind.get("pcr") or oc_data.get("pcr")
        max_pain = ind.get("max_pain") or oc_data.get("max_pain")
        # Sentiment
        if isinstance(pcr, (int, float)):
            if pcr > 1.20:
                bias = "bullish"
            elif pcr < 0.80:
                bias = "bearish"

    breakouts = breakout_consolidation(force=False).get("stocks", [])
    rs = relative_strength(force=False).get("stocks", [])
    rs_lookup = {r["symbol"]: r for r in rs}

    aligned: List[Dict[str, Any]] = []
    for b in breakouts[:50]:
        rsd = rs_lookup.get(b["symbol"])
        if not rsd:
            continue
        rs_rating = rsd["rs_rating"]
        # Aligned setups: breakout + strong RS in bullish env, or weak RS in bearish
        if bias == "bullish" and b["breakout"] and rs_rating >= 60:
            verdict = "Long candidate"
        elif bias == "bearish" and rs_rating <= 40:
            verdict = "Short candidate"
        elif bias == "neutral" and b["score"] >= 50 and rs_rating >= 65:
            verdict = "Long candidate (RS-driven)"
        else:
            continue
        aligned.append({
            **b,
            "rs_rating": rs_rating,
            "rs_4w_pct": rsd["rs_4w_pct"],
            "verdict": verdict,
        })

    aligned.sort(key=lambda r: r["score"] + r["rs_rating"], reverse=True)
    payload = {
        "stocks": aligned,
        "nifty_bias": bias,
        "pcr": pcr,
        "max_pain": max_pain,
        "options_data_available": bool(oc_data),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=10 * 60)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 6. FII/DII overlay
# ─────────────────────────────────────────────────────────────────────────

def fii_dii_overlay(force: bool = False) -> Dict[str, Any]:
    """Fetch FII / DII cash-market flows (last 15 sessions).

    Free public source: NSE archives. We attempt the official endpoint and
    gracefully fall back to a 'data unavailable' state if blocked.
    """
    key = "swing:fii_dii:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    import requests

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121"),
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.nseindia.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }

    rows: List[Dict[str, Any]] = []
    error = None
    try:
        sess = requests.Session()
        # Warm up cookies
        sess.get("https://www.nseindia.com/", headers=headers, timeout=4)
        url = "https://www.nseindia.com/api/fiidiiTradeReact"
        r = sess.get(url, headers=headers, timeout=6)
        if r.status_code == 200:
            data = r.json()
            # API returns latest session only (FII + DII line each). For
            # multi-session history we'd need the historical CSV; here we
            # surface today's data and compute net buy/sell.
            for entry in data:
                rows.append({
                    "category": entry.get("category"),
                    "date": entry.get("date"),
                    "buy_value": _safe_float(entry.get("buyValue")),
                    "sell_value": _safe_float(entry.get("sellValue")),
                    "net_value": _safe_float(entry.get("netValue")),
                })
        else:
            error = f"NSE returned HTTP {r.status_code}"
    except Exception as e:
        error = f"Fetch failed: {e}"

    # Determine sentiment
    fii_net = next((r["net_value"] for r in rows
                    if r["category"] and "FII" in r["category"].upper()), None)
    dii_net = next((r["net_value"] for r in rows
                    if r["category"] and "DII" in r["category"].upper()), None)

    sentiment = "neutral"
    if fii_net is not None and dii_net is not None:
        if fii_net > 500 and dii_net > 500:
            sentiment = "very_bullish"
        elif fii_net > 0 or dii_net > 0:
            sentiment = "bullish"
        elif fii_net < -500 and dii_net < -500:
            sentiment = "very_bearish"
        elif fii_net < 0 and dii_net < 0:
            sentiment = "bearish"

    payload = {
        "rows": rows,
        "fii_net_cr": round(fii_net, 2) if fii_net is not None else None,
        "dii_net_cr": round(dii_net, 2) if dii_net is not None else None,
        "sentiment": sentiment,
        "error": error,
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=15 * 60)
    except Exception:
        pass
    return payload


# -----------------------------------------------------------------------
# Multi-Timeframe Trend Alignment (Daily + Weekly EMA stack)
# -----------------------------------------------------------------------

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _mtf_one(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        df = market_data.get_history(symbol, days=320, interval="1d")
        if df is None or df.empty or len(df) < 220:
            return None
        close = df["Close"].astype(float)
        e20 = _ema(close, 20); e50 = _ema(close, 50); e200 = _ema(close, 200)
        d20, d50, d200 = _safe_float(e20.iloc[-1]), _safe_float(e50.iloc[-1]), _safe_float(e200.iloc[-1])
        # Weekly stack
        try:
            wk = close.resample("W-FRI").last().dropna()
        except Exception:
            wk = close.iloc[::5]
        if len(wk) < 60:
            return None
        we20 = _ema(wk, 20); we50 = _ema(wk, 50)
        w20, w50 = _safe_float(we20.iloc[-1]), _safe_float(we50.iloc[-1])
        ltp = _safe_float(close.iloc[-1])
        if not all([d20, d50, d200, w20, w50, ltp]):
            return None
        daily_up = ltp > d20 > d50 > d200
        daily_dn = ltp < d20 < d50 < d200
        weekly_up = w20 > w50
        weekly_dn = w20 < w50
        if daily_up and weekly_up:
            verdict = "strong-bull"; score = 100
        elif daily_dn and weekly_dn:
            verdict = "strong-bear"; score = -100
        elif daily_up and not weekly_dn:
            verdict = "bull"; score = 60
        elif daily_dn and not weekly_up:
            verdict = "bear"; score = -60
        else:
            verdict = "mixed"; score = 0
        # % above 200DMA
        dist_200 = (ltp - d200) / d200 * 100
        return {
            "symbol": symbol, "name": _display_name(symbol),
            "ltp": round(ltp, 2),
            "ema20": round(d20, 2), "ema50": round(d50, 2), "ema200": round(d200, 2),
            "weekly_ema20": round(w20, 2), "weekly_ema50": round(w50, 2),
            "distance_200dma_pct": round(dist_200, 2),
            "verdict": verdict, "score": score,
        }
    except Exception:
        return None


def mtf_alignment(force: bool = False) -> Dict[str, Any]:
    key = "swing:mtf:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}
    rows = _parallel(_mtf_one, UNIVERSE, max_workers=8)
    rows = [r for r in rows if r]
    rows.sort(key=lambda r: r["score"], reverse=True)
    bulls = [r for r in rows if r["score"] >= 60]
    bears = [r for r in rows if r["score"] <= -60]
    payload = {
        "stocks": rows, "bulls": bulls[:30], "bears": bears[:20],
        "strong_bulls": sum(1 for r in rows if r["verdict"] == "strong-bull"),
        "strong_bears": sum(1 for r in rows if r["verdict"] == "strong-bear"),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=900)
    except Exception:
        pass
    return payload


# -----------------------------------------------------------------------
# 52-Week High Proximity Scanner
# -----------------------------------------------------------------------

def _52wh_one(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        df = market_data.get_history(symbol, days=260, interval="1d")
        if df is None or df.empty or len(df) < 200:
            return None
        close = df["Close"].astype(float); high = df["High"].astype(float)
        hh52 = _safe_float(high.max())
        ltp = _safe_float(close.iloc[-1])
        if hh52 <= 0 or ltp <= 0:
            return None
        proximity = (hh52 - ltp) / hh52 * 100
        # Base tightness: stdev of last 25 days / mean
        recent = close.tail(25)
        tightness = (recent.std() / recent.mean() * 100) if recent.mean() > 0 else 99
        # Volume contraction: last 10 vs prior 30
        vol = df["Volume"].astype(float)
        vol_recent = vol.tail(10).mean()
        vol_base = vol.iloc[-40:-10].mean()
        vol_ratio = float(vol_recent / vol_base) if vol_base > 0 else 1.0
        tightness = float(tightness)
        constructive = bool(tightness < 4.5 and vol_ratio < 1.1)
        return {
            "symbol": symbol, "name": _display_name(symbol),
            "ltp": round(ltp, 2),
            "high_52w": round(hh52, 2),
            "proximity_pct": round(float(proximity), 2),
            "base_tightness_pct": round(tightness, 2),
            "volume_ratio": round(vol_ratio, 2),
            "constructive_base": constructive,
        }
    except Exception:
        return None


def near_52wh(max_proximity: float = 5.0, force: bool = False) -> Dict[str, Any]:
    key = f"swing:52wh:{max_proximity}"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}
    rows = _parallel(_52wh_one, UNIVERSE, max_workers=8)
    rows = [r for r in rows if r and r["proximity_pct"] <= max_proximity]
    rows.sort(key=lambda r: r["proximity_pct"])
    payload = {
        "stocks": rows,
        "constructive": sum(1 for r in rows if r["constructive_base"]),
        "max_proximity": max_proximity,
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=900)
    except Exception:
        pass
    return payload


# -----------------------------------------------------------------------
# Volume Dry-Up + Pocket Pivot
# -----------------------------------------------------------------------

def _pocket_pivot_one(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        df = market_data.get_history(symbol, days=80, interval="1d")
        if df is None or df.empty or len(df) < 55:
            return None
        close = df["Close"].astype(float); vol = df["Volume"].astype(float)
        last_close = _safe_float(close.iloc[-1]); prev_close = _safe_float(close.iloc[-2])
        if last_close <= 0 or prev_close <= 0:
            return None
        # Today must be an up-day
        if last_close <= prev_close:
            return None
        # Pocket pivot: today's volume > largest DOWN-day volume of past 10 sessions
        prior = df.iloc[-11:-1].copy()
        prior["chg"] = prior["Close"].astype(float).diff()
        down_vols = prior[prior["chg"] < 0]["Volume"].astype(float)
        max_down_vol = down_vols.max() if not down_vols.empty else 0
        today_vol = _safe_float(vol.iloc[-1])
        if today_vol <= max_down_vol or max_down_vol <= 0:
            return None
        # Volume dry-up context: avg vol last 10 < avg vol prior 30
        v_recent = vol.iloc[-11:-1].mean()
        v_base = vol.iloc[-41:-11].mean() if len(vol) >= 41 else vol.mean()
        if v_base <= 0 or v_recent >= v_base * 0.95:
            return None
        dryup_ratio = v_recent / v_base
        # On a base (above 50DMA but not too extended)
        ema50 = _ema(close, 50).iloc[-1]
        if last_close < ema50:
            return None
        ext_pct = (last_close - ema50) / ema50 * 100
        return {
            "symbol": symbol, "name": _display_name(symbol),
            "ltp": round(last_close, 2),
            "day_change_pct": round((last_close - prev_close) / prev_close * 100, 2),
            "today_volume": int(today_vol),
            "max_down_vol_10d": int(max_down_vol),
            "vol_dryup_ratio": round(dryup_ratio, 2),
            "extension_pct": round(ext_pct, 2),
        }
    except Exception:
        return None


def pocket_pivot(force: bool = False) -> Dict[str, Any]:
    key = "swing:pocket_pivot:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}
    rows = _parallel(_pocket_pivot_one, UNIVERSE, max_workers=8)
    rows = [r for r in rows if r]
    rows.sort(key=lambda r: r["day_change_pct"], reverse=True)
    payload = {
        "stocks": rows,
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=900)
    except Exception:
        pass
    return payload


# -----------------------------------------------------------------------
# Swing Backtest Sandbox: EMA-cross + RSI + Volume confirmation
# -----------------------------------------------------------------------

def _rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0); dn = -delta.clip(upper=0)
    rs = up.ewm(alpha=1/n, adjust=False).mean() / dn.ewm(alpha=1/n, adjust=False).mean().replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def swing_backtest(symbol: str,
                   ema_fast: int = 20,
                   ema_slow: int = 50,
                   rsi_threshold: float = 55.0,
                   vol_multiplier: float = 1.2,
                   years: int = 3) -> Dict[str, Any]:
    days = max(120, years * 252)
    df = market_data.get_history(symbol, days=days, interval="1d")
    if df is None or df.empty or len(df) < max(ema_slow + 30, 80):
        return {"error": "insufficient data", "symbol": symbol}
    df = df.copy()
    close = df["Close"].astype(float); vol = df["Volume"].astype(float)
    df["ema_f"] = _ema(close, ema_fast); df["ema_s"] = _ema(close, ema_slow)
    df["rsi"] = _rsi(close)
    df["vol_ma"] = vol.rolling(20).mean()
    df = df.dropna()
    if df.empty:
        return {"error": "no signals", "symbol": symbol}

    in_pos = False; entry_price = 0.0; entry_date = None
    trades = []
    equity = 100000.0; equity_curve = []
    bh_start_price = float(df["Close"].iloc[0])
    for i in range(1, len(df)):
        row = df.iloc[i]; prev = df.iloc[i-1]
        bullish_cross = prev["ema_f"] <= prev["ema_s"] and row["ema_f"] > row["ema_s"]
        bearish_cross = prev["ema_f"] >= prev["ema_s"] and row["ema_f"] < row["ema_s"]
        vol_ok = row["Volume"] >= row["vol_ma"] * vol_multiplier
        rsi_ok = row["rsi"] >= rsi_threshold
        date_str = row.name.strftime("%Y-%m-%d") if hasattr(row.name, "strftime") else str(row.name)
        price = float(row["Close"])
        if not in_pos and bullish_cross and rsi_ok and vol_ok:
            in_pos = True; entry_price = price; entry_date = date_str
        elif in_pos and (bearish_cross or row["rsi"] < 45):
            ret_pct = (price - entry_price) / entry_price * 100
            equity *= (1 + ret_pct / 100)
            trades.append({
                "entry_date": entry_date, "exit_date": date_str,
                "entry": round(entry_price, 2), "exit": round(price, 2),
                "return_pct": round(ret_pct, 2),
                "days_held": (pd.to_datetime(date_str) - pd.to_datetime(entry_date)).days,
            })
            in_pos = False; entry_price = 0; entry_date = None
        equity_curve.append({"date": date_str, "equity": round(equity, 2)})

    if not trades:
        return {"symbol": symbol, "error": "no trades generated with given parameters",
                "params": {"ema_fast": ema_fast, "ema_slow": ema_slow,
                           "rsi_threshold": rsi_threshold, "vol_multiplier": vol_multiplier}}

    wins = [t for t in trades if t["return_pct"] > 0]
    losses = [t for t in trades if t["return_pct"] <= 0]
    total_ret = (equity - 100000) / 100000 * 100
    bh_ret = (float(df["Close"].iloc[-1]) - bh_start_price) / bh_start_price * 100
    avg_win = sum(t["return_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["return_pct"] for t in losses) / len(losses) if losses else 0
    return {
        "symbol": symbol, "name": _display_name(symbol),
        "params": {"ema_fast": ema_fast, "ema_slow": ema_slow,
                   "rsi_threshold": rsi_threshold, "vol_multiplier": vol_multiplier,
                   "years": years},
        "trades": trades[-50:],
        "total_trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(abs((avg_win * len(wins)) / (avg_loss * len(losses))), 2) if losses and avg_loss != 0 else None,
        "total_return_pct": round(total_ret, 2),
        "buy_hold_return_pct": round(bh_ret, 2),
        "outperformance_pct": round(total_ret - bh_ret, 2),
        "final_equity": round(equity, 2),
        "equity_curve": equity_curve[-200:],
    }
