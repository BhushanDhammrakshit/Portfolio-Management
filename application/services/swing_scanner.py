"""Modern momentum swing-trade scanner.

Strategy: **Qullamaggie + Minervini Momentum Breakout** — built from the
publicly-documented playbooks of two traders whose methods *empirically*
outperform standard retail indicators in current markets:

    * Kristjan "Qullamaggie" Kullamägi  (turned ~$5k into $100M+ 2011-2020)
    * Mark Minervini  (US Investing Champion 1997, 2021)

Why RSI/MACD/EMA/VWAP underperform in 2024-2026 markets
-------------------------------------------------------
Those indicators are *coincident* (they describe what already happened)
and are mathematically smoothed versions of price — every retail trader
runs them, so any edge has been competed away. They generate constant
crossovers that fire in chop and provide no information about whether
*institutions* are actually accumulating.

What still has an edge (out-of-sample, peer-reviewed):
------------------------------------------------------
1.  **Cross-sectional momentum** (Jegadeesh & Titman 1993; Asness 1997;
    Moskowitz 2012) — 3-12 month relative-strength leaders continue to
    outperform. The single strongest factor in 40+ years of data.
2.  **Volatility Contraction → Expansion** — institutions accumulate
    quietly (volume + range dry up) before pushing price out. The
    breakout from a tight base on volume thrust is the *announcement*
    that accumulation has finished.
3.  **Episodic catalyst gaps** — Pradeep Bonde's "Stockbee Episodic
    Pivots". A 4%+ gap up on volume after earnings/news re-rates the
    stock; PEAD (post-earnings-announcement drift) is one of the few
    factor anomalies still alive.

The scanner combines these into a single 0-100 score. Targets 10-15% in
1-2 weeks (≈ ATR × 6-10), with a stop at the consolidation low.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from application.services import cache as shared_cache, market_data

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

_CACHE_KEY = "swing:scan:v1"
_CACHE_TTL = 15 * 60  # 15 min — swing setups don't change minute-to-minute

# ── Universe ────────────────────────────────────────────────────────────
#
# NIFTY 100 large-caps (where institutional flow is real) + a curated
# mid-cap "movers" list (where 10-15% in 2 weeks actually happens).
# Mid-caps are essential — large-caps rarely produce >10% moves in 10
# trading days unless there's a sector catalyst.

_LARGE_CAP = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
    "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "KOTAKBANK.NS", "LT.NS",
    "HINDUNILVR.NS", "AXISBANK.NS", "BAJFINANCE.NS", "MARUTI.NS",
    "SUNPHARMA.NS", "TITAN.NS", "ULTRACEMCO.NS", "WIPRO.NS",
    "HCLTECH.NS", "ADANIENT.NS", "TATAMOTORS.NS", "TATASTEEL.NS",
    "NTPC.NS", "POWERGRID.NS", "ONGC.NS", "COALINDIA.NS",
    "JSWSTEEL.NS", "M&M.NS", "BAJAJFINSV.NS", "TECHM.NS",
    "ASIANPAINT.NS", "NESTLEIND.NS", "HDFCLIFE.NS", "SBILIFE.NS",
    "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS", "EICHERMOT.NS",
    "GRASIM.NS", "HEROMOTOCO.NS", "BAJAJ-AUTO.NS", "BPCL.NS",
    "BRITANNIA.NS", "TATACONSUM.NS", "APOLLOHOSP.NS", "INDUSINDBK.NS",
    "HINDALCO.NS", "ADANIPORTS.NS", "PIDILITIND.NS", "SHRIRAMFIN.NS",
]

_MID_CAP_MOVERS = [
    # Capital goods / Defence — top sectors in current cycle
    "BEL.NS", "HAL.NS", "BHEL.NS", "SIEMENS.NS", "ABB.NS",
    "CUMMINSIND.NS", "BHARATFORG.NS", "TIINDIA.NS",
    # Power / Infra
    "ADANIPOWER.NS", "TATAPOWER.NS", "JSWENERGY.NS", "NHPC.NS",
    "POWERINDIA.NS", "GMRINFRA.NS", "IRB.NS", "RVNL.NS",
    "RAILTEL.NS", "IRFC.NS", "IRCON.NS",
    # Mid-cap tech / new-age
    "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "LTIM.NS",
    "POLICYBZR.NS", "PAYTM.NS", "ZOMATO.NS", "NYKAA.NS",
    # Pharma / chemicals
    "LAURUSLABS.NS", "GLENMARK.NS", "AUROPHARMA.NS", "BIOCON.NS",
    "DEEPAKNTR.NS", "ATUL.NS", "PIIND.NS", "SRF.NS",
    # Banks / NBFC mid-caps
    "FEDERALBNK.NS", "IDFCFIRSTB.NS", "BANDHANBNK.NS", "AUBANK.NS",
    "CHOLAFIN.NS", "MUTHOOTFIN.NS", "LICHSGFIN.NS",
    # Consumer / retail
    "TRENT.NS", "DMART.NS", "VOLTAS.NS", "DIXON.NS", "HAVELLS.NS",
    "POLYCAB.NS", "CROMPTON.NS",
    # Metals / mining
    "VEDL.NS", "JINDALSTEL.NS", "SAIL.NS", "NMDC.NS",
    # Real estate
    "DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS",
    # PSU
    "GAIL.NS", "IOC.NS", "HINDPETRO.NS", "PFC.NS", "RECLTD.NS",
]

UNIVERSE = _LARGE_CAP + _MID_CAP_MOVERS


# ── Factor computation ─────────────────────────────────────────────────

def _safe_pct(num: float, den: float) -> float:
    return (num / den * 100.0) if den else 0.0


def _compute_factors(symbol: str, bench_df: Optional[pd.DataFrame] = None
                     ) -> Optional[Dict[str, Any]]:
    """Run all swing factors on one symbol; return None if data insufficient."""
    # Fyers caps daily-bar requests at 365 days; the provider pads ×1.6, so
    # we request ~220 days (≈ 352 padded ≈ 240 trading days — plenty for
    # SMA200 + 22-day slope check + 252-day 52W high).
    df = market_data.get_history(symbol, days=220, interval="1d")
    if df is None or df.empty or len(df) < 200:
        return None

    df = df.dropna(subset=["Close", "High", "Low"]).copy()
    if len(df) < 200:
        return None

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    vol = df["Volume"].fillna(0)

    last = float(close.iloc[-1])
    if last <= 0 or np.isnan(last):
        return None

    # 1. Trend-template moving averages -----------------------------------
    sma50 = close.rolling(50).mean()
    sma150 = close.rolling(150).mean()
    sma200 = close.rolling(200).mean()
    sma50_v = float(sma50.iloc[-1])
    sma150_v = float(sma150.iloc[-1])
    sma200_v = float(sma200.iloc[-1])

    # SMA200 rising for at least 22 trading days (≈ 1 month)
    sma200_rising = bool(sma200.iloc[-1] > sma200.iloc[-22]) if len(sma200) > 22 else False

    # 52-week high / low
    high52 = float(high.rolling(252).max().iloc[-1]) if len(high) >= 252 else float(high.max())
    low52 = float(low.rolling(252).min().iloc[-1]) if len(low) >= 252 else float(low.min())
    pct_from_52h = _safe_pct(last - high52, high52)   # negative number
    pct_from_52l = _safe_pct(last - low52, low52)     # positive number

    # 2. Relative-strength scores -----------------------------------------
    def _ret(n: int) -> float:
        if len(close) <= n:
            return 0.0
        return _safe_pct(close.iloc[-1] - close.iloc[-n - 1], close.iloc[-n - 1])

    ret_1m = _ret(21)
    ret_3m = _ret(63)
    ret_6m = _ret(126)

    # vs benchmark (NIFTY)
    rs_3m_excess = ret_3m
    if bench_df is not None and len(bench_df) > 63:
        b_close = bench_df["Close"]
        bench_3m = _safe_pct(b_close.iloc[-1] - b_close.iloc[-64], b_close.iloc[-64])
        rs_3m_excess = ret_3m - bench_3m

    # 3. ADR% (Average Daily Range) — Qullamaggie key filter --------------
    daily_ranges = (high - low) / close.replace(0, np.nan)
    adr_pct = float(daily_ranges.tail(20).mean() * 100.0)

    # 4. ATR + volatility contraction -------------------------------------
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr5 = float(tr.tail(5).mean())
    atr20 = float(tr.tail(20).mean())
    atr_ratio = (atr5 / atr20) if atr20 else 1.0  # < 0.85 = contracting

    # Range tightness of last 10 bars vs price
    range10 = float(high.tail(10).max() - low.tail(10).min())
    range10_pct = _safe_pct(range10, last)  # <10% = very tight base

    # 5. Volume profile ---------------------------------------------------
    vol5 = float(vol.tail(5).mean())
    vol20 = float(vol.tail(20).mean())
    vol50 = float(vol.tail(50).mean())
    vol_dryup = (vol5 / vol20) if vol20 else 1.0   # <0.85 = dry-up
    vol_thrust = (float(vol.iloc[-1]) / vol50) if vol50 else 1.0   # >1.5 = thrust

    # 6. Liquidity (turnover, ₹ crore) ------------------------------------
    turnover_cr = float((close * vol).tail(20).mean() / 1e7)

    # 7. Breakout / pivot trigger -----------------------------------------
    pivot_high_20 = float(high.iloc[-22:-2].max()) if len(high) > 22 else last
    closed_above_pivot = last > pivot_high_20
    day_low = float(low.iloc[-1])
    day_high = float(high.iloc[-1])
    day_close_pos = _safe_pct(last - day_low, max(day_high - day_low, 1e-9))
    range_expansion = (day_high - day_low) > (
        float(high.iloc[-2] - low.iloc[-2]) if len(high) > 1 else 0
    )

    # 8. Episodic-pivot detection (gap > 4% in last 60 days) -------------
    open_ = df["Open"]
    if len(open_) >= 60 and len(close) >= 60:
        prev = close.shift().iloc[-60:]
        gap_series = (open_.iloc[-60:] - prev) / prev.replace(0, pd.NA) * 100.0
        gap_series = gap_series.dropna()
        max_gap_60d = float(gap_series.max()) if not gap_series.empty else 0.0
    else:
        max_gap_60d = 0.0

    return {
        "symbol": symbol,
        "name": symbol.replace(".NS", "").replace(".BO", ""),
        "price": round(last, 2),
        "sma50": round(sma50_v, 2),
        "sma150": round(sma150_v, 2),
        "sma200": round(sma200_v, 2),
        "sma200_rising": sma200_rising,
        "high52": round(high52, 2),
        "low52": round(low52, 2),
        "pct_from_52h": round(pct_from_52h, 1),
        "pct_from_52l": round(pct_from_52l, 1),
        "ret_1m": round(ret_1m, 1),
        "ret_3m": round(ret_3m, 1),
        "ret_6m": round(ret_6m, 1),
        "rs_3m_excess": round(rs_3m_excess, 1),
        "adr_pct": round(adr_pct, 2),
        "atr_ratio": round(atr_ratio, 2),
        "range10_pct": round(range10_pct, 1),
        "vol_dryup": round(vol_dryup, 2),
        "vol_thrust": round(vol_thrust, 2),
        "turnover_cr": round(turnover_cr, 1),
        "pivot_high_20": round(pivot_high_20, 2),
        "closed_above_pivot": closed_above_pivot,
        "day_close_pos": round(day_close_pos, 0),
        "range_expansion": range_expansion,
        "max_gap_60d": round(max_gap_60d, 1),
    }


# ── Scoring ────────────────────────────────────────────────────────────

def _score_setup(f: Dict[str, Any]) -> Dict[str, Any]:
    """Convert factor bundle → 0-100 setup score + reason trail."""
    score = 0
    reasons: List[str] = []
    rejects: List[str] = []

    # === Hard filters (Minervini Trend Template) ===
    # Stage-2 uptrend test — without these, no setup is viable.
    trend_ok = (
        f["price"] > f["sma50"] > f["sma150"] > f["sma200"]
        and f["sma200_rising"]
        and f["pct_from_52h"] >= -25.0
        and f["pct_from_52l"] >= 30.0
    )
    if trend_ok:
        score += 25
        reasons.append("✓ Stage-2 uptrend (price > 50 > 150 > 200 SMA, 200 rising)")
    else:
        rejects.append("Failed trend template (not in Stage-2 uptrend)")

    # Liquidity gate
    if f["turnover_cr"] < 5:
        rejects.append(f"Illiquid (₹{f['turnover_cr']:.1f} Cr/day avg)")

    # === Momentum (Jegadeesh-Titman factor) ===
    if f["ret_3m"] >= 20 and f["rs_3m_excess"] >= 10:
        score += 20
        reasons.append(f"Strong relative-strength leader (+{f['ret_3m']:.0f}% 3M, {f['rs_3m_excess']:+.0f}% vs NIFTY)")
    elif f["ret_3m"] >= 10 and f["rs_3m_excess"] >= 5:
        score += 12
        reasons.append(f"RS leader (+{f['ret_3m']:.0f}% 3M, {f['rs_3m_excess']:+.0f}% vs NIFTY)")
    elif f["rs_3m_excess"] > 0:
        score += 5
        reasons.append(f"Mildly outperforming index ({f['rs_3m_excess']:+.0f}%)")
    else:
        rejects.append(f"Underperforming index ({f['rs_3m_excess']:+.0f}% 3M)")

    # === ADR (the move-magnitude filter) ===
    if f["adr_pct"] >= 4.5:
        score += 12
        reasons.append(f"High ADR {f['adr_pct']:.1f}% — capable of 10-15% in 2 weeks")
    elif f["adr_pct"] >= 3.0:
        score += 6
        reasons.append(f"Adequate ADR {f['adr_pct']:.1f}%")
    else:
        rejects.append(f"Low ADR {f['adr_pct']:.1f}% — too slow for swing")

    # === Volatility contraction (Qullamaggie / VCP) ===
    if f["atr_ratio"] < 0.75 and f["range10_pct"] < 8:
        score += 15
        reasons.append(f"Tight VCP (ATR ratio {f['atr_ratio']:.2f}, 10-day range {f['range10_pct']:.1f}%)")
    elif f["atr_ratio"] < 0.85 and f["range10_pct"] < 12:
        score += 8
        reasons.append(f"Contracting volatility (ATR ratio {f['atr_ratio']:.2f})")

    if f["vol_dryup"] < 0.80:
        score += 5
        reasons.append(f"Volume dry-up in base ({f['vol_dryup']:.2f}× vs 20-day)")

    # === Breakout trigger (the entry signal) ===
    if f["closed_above_pivot"] and f["vol_thrust"] >= 1.5 and f["day_close_pos"] >= 70:
        score += 18
        reasons.append(f"BREAKOUT today — closed above {f['pivot_high_20']:.2f} on {f['vol_thrust']:.1f}× volume")
    elif f["closed_above_pivot"] and f["vol_thrust"] >= 1.2:
        score += 12
        reasons.append(f"Pivot breakout on rising volume ({f['vol_thrust']:.1f}×)")
    elif f["pct_from_52h"] >= -3:
        score += 6
        reasons.append(f"Pressing 52-week high (within {abs(f['pct_from_52h']):.1f}%)")

    # === Episodic-pivot bonus (PEAD / catalyst gap) ===
    if f["max_gap_60d"] >= 8:
        score += 8
        reasons.append(f"Catalyst gap {f['max_gap_60d']:.1f}% in last 60 days — episodic pivot")
    elif f["max_gap_60d"] >= 4:
        score += 4
        reasons.append(f"Recent gap {f['max_gap_60d']:.1f}% — possible catalyst")

    # Range expansion today (institutions stepping in)
    if f["range_expansion"] and f["day_close_pos"] >= 70:
        score += 3
        reasons.append("Range expansion + close in upper 30% (institutional buying)")

    score = max(0, min(100, score))

    # Setup grade
    if not trend_ok or f["turnover_cr"] < 5:
        grade = "AVOID"
    elif score >= 75:
        grade = "A+ SETUP"
    elif score >= 60:
        grade = "A SETUP"
    elif score >= 45:
        grade = "B SETUP"
    elif score >= 30:
        grade = "WATCH"
    else:
        grade = "AVOID"

    # Trade plan -------------------------------------------------------
    entry = f["price"] if f["closed_above_pivot"] else max(f["pivot_high_20"] * 1.001, f["price"])
    # ATR proxy for stop (1× ADR below entry, capped at -7%)
    stop_pct = max(min(f["adr_pct"], 7.0), 2.0)
    stop = round(entry * (1 - stop_pct / 100), 2)
    # Target: 2-week move ≈ ADR × √10 trading days × 0.55 efficiency, cap at +20%
    target_pct = min(f["adr_pct"] * math.sqrt(10) * 0.55, 20.0)
    target = round(entry * (1 + target_pct / 100), 2)
    rr = round(target_pct / stop_pct, 2) if stop_pct else 0
    return {
        "score": score,
        "grade": grade,
        "reasons": reasons,
        "rejects": rejects,
        "entry": round(entry, 2),
        "stop": stop,
        "target": target,
        "stop_pct": round(stop_pct, 1),
        "target_pct": round(target_pct, 1),
        "risk_reward": rr,
    }


# ── Public scan ────────────────────────────────────────────────────────

def scan(force_refresh: bool = False) -> Dict[str, Any]:
    if not force_refresh:
        cached = shared_cache.jget(_CACHE_KEY)
        if isinstance(cached, dict) and cached.get("stocks") is not None:
            return {**cached, "cached": True}

    # Benchmark (NIFTY) for relative-strength comparison.
    try:
        bench = market_data.get_history("^NSEI", days=200, interval="1d")
    except Exception:
        bench = None

    results: List[Dict[str, Any]] = []
    for sym in UNIVERSE:
        try:
            f = _compute_factors(sym, bench)
            if not f:
                continue
            scoring = _score_setup(f)
            results.append({**f, **scoring})
        except Exception as e:  # noqa: BLE001
            log.debug("swing.scan %s skipped: %s", sym, e)

    # Sort: highest score first, then highest ADR (move potential)
    results.sort(key=lambda x: (x["score"], x["adr_pct"]), reverse=True)

    scan_time = _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")
    grade_counts: Dict[str, int] = {}
    for r in results:
        grade_counts[r["grade"]] = grade_counts.get(r["grade"], 0) + 1

    payload = {
        "stocks": results,
        "scan_time": scan_time,
        "universe_size": len(UNIVERSE),
        "scanned": len(results),
        "grade_counts": grade_counts,
        "strategy": "Qullamaggie + Minervini Momentum Breakout",
        "cached": False,
    }
    try:
        shared_cache.jset(_CACHE_KEY, payload, ttl=_CACHE_TTL)
    except Exception:
        pass
    return payload
