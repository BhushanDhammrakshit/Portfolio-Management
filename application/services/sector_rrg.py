"""Advanced Sector Rotation — Relative Rotation Graph (RRG) service.

Computes JdK-style RS-Ratio and RS-Momentum for each sector index relative to
a chosen benchmark, plus a historical trail of N periods so the front-end can
draw rotation snakes through the four quadrants (Leading / Weakening /
Lagging / Improving).

Public entry point: `sector_rrg(benchmark, interval, periods, force=False)`.
"""
from __future__ import annotations

import datetime as _dt
import math
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from application.services import cache as shared_cache, market_data
from application.services import snapshot_store
from application.services.intraday_tools import SECTOR_INDICES, _now_ist


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

BENCHMARKS: Dict[str, str] = {
    "NIFTY 50":        "^NSEI",
    "NIFTY 500":       "^CRSLDX",
    "NIFTY BANK":      "^NSEBANK",
    "NIFTY MIDCAP 100":"NIFTY_MIDCAP_100.NS",
    "NIFTY NEXT 50":   "^NSMIDCP",
}

# Stable, distinguishable colour palette (10 entries — one per sector).
PALETTE: List[str] = [
    "#4f46e5", "#10b981", "#f59e0b", "#ef4444", "#06b6d4",
    "#8b5cf6", "#ec4899", "#65a30d", "#0ea5e9", "#f97316",
]

# RRG calculation constants
_SMA_WIN = 14   # window for relative-strength normalisation
_MOM_LAG = 5    # ROC lag for momentum derivative
_MIN_HIST_DAILY = 90    # days of daily bars to fetch
_MIN_HIST_WEEKLY = 365  # days of daily bars to fetch when resampling to weekly

_INTERVALS = {"1d", "1wk"}
_MAX_PERIODS = 60


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except Exception:
        return None


def _classify_quadrant(rs_ratio: float, rs_momentum: float) -> str:
    if rs_ratio >= 100 and rs_momentum >= 100:
        return "leading"
    if rs_ratio >= 100 and rs_momentum < 100:
        return "weakening"
    if rs_ratio < 100 and rs_momentum < 100:
        return "lagging"
    return "improving"


def _heading_deg(dx: float, dy: float) -> Optional[float]:
    if dx == 0 and dy == 0:
        return None
    # 0° = East (+X), 90° = North (+Y). Front-end converts to SVG y-down.
    return round(math.degrees(math.atan2(dy, dx)), 1)


def _fetch_close(symbol: str, days: int) -> Optional[pd.Series]:
    try:
        df = market_data.get_history(symbol, days=days, interval="1d")
        if df is None or df.empty or "Close" not in df.columns:
            return None
        s = pd.to_numeric(df["Close"], errors="coerce").dropna()
        if s.empty:
            return None
        # Ensure tz-naive sorted datetime index
        s = s.copy()
        try:
            if getattr(s.index, "tz", None) is not None:
                s.index = s.index.tz_localize(None)
        except Exception:
            pass
        s = s.sort_index()
        return s
    except Exception:
        return None


def _resample(series: pd.Series, interval: str) -> pd.Series:
    if interval == "1wk":
        return series.resample("W-FRI").last().dropna()
    return series


def _compute_rs(sector_close: pd.Series, bench_close: pd.Series) -> Optional[pd.DataFrame]:
    """Compute RS-Ratio and RS-Momentum series aligned on shared dates."""
    df = pd.concat([sector_close, bench_close], axis=1, join="inner").dropna()
    if df.shape[0] < (_SMA_WIN + _MOM_LAG + 2):
        return None
    df.columns = ["sec", "bench"]
    relative = df["sec"] / df["bench"]
    sma = relative.rolling(_SMA_WIN).mean()
    std = relative.rolling(_SMA_WIN).std()
    # JdK-style z-score scaled, centred at 100
    rs_ratio = 100.0 + 10.0 * (relative - sma) / std.replace(0, np.nan)
    # Momentum = smoothed ROC of rs_ratio, also centred at 100
    mom_raw = rs_ratio - rs_ratio.shift(_MOM_LAG)
    mom_sma = mom_raw.rolling(_SMA_WIN).mean()
    mom_std = mom_raw.rolling(_SMA_WIN).std()
    rs_momentum = 100.0 + 10.0 * (mom_raw - mom_sma) / mom_std.replace(0, np.nan)
    out = pd.DataFrame({"rs_ratio": rs_ratio, "rs_momentum": rs_momentum}).dropna()
    if out.empty:
        return None
    return out


def _trail_payload(series_df: pd.DataFrame, periods: int) -> List[Dict[str, Any]]:
    tail = series_df.tail(periods)
    out: List[Dict[str, Any]] = []
    for ts, row in tail.iterrows():
        rr = _safe_float(row["rs_ratio"])
        mm = _safe_float(row["rs_momentum"])
        if rr is None or mm is None:
            continue
        out.append({
            "t": ts.strftime("%Y-%m-%d"),
            "rs_ratio": round(rr, 2),
            "rs_momentum": round(mm, 2),
            "quadrant": _classify_quadrant(rr, mm),
        })
    return out


def _sector_payload(
    name: str,
    symbol: str,
    color: str,
    bench_close: pd.Series,
    interval: str,
    periods: int,
) -> Optional[Dict[str, Any]]:
    days = _MIN_HIST_WEEKLY if interval == "1wk" else _MIN_HIST_DAILY
    sec_close = _fetch_close(symbol, days=days)
    if sec_close is None:
        return None
    sec_r = _resample(sec_close, interval)
    bench_r = _resample(bench_close, interval)
    rs = _compute_rs(sec_r, bench_r)
    if rs is None:
        return None
    trail = _trail_payload(rs, periods)
    if len(trail) < 2:
        return None
    cur = trail[-1]
    prev = trail[-2]
    dx = round(cur["rs_ratio"] - prev["rs_ratio"], 3)
    dy = round(cur["rs_momentum"] - prev["rs_momentum"], 3)
    magnitude = round(math.sqrt(dx * dx + dy * dy), 3)
    return {
        "name": name,
        "symbol": symbol,
        "color": color,
        "current": {
            "rs_ratio": cur["rs_ratio"],
            "rs_momentum": cur["rs_momentum"],
            "quadrant": cur["quadrant"],
            "as_of": cur["t"],
        },
        "previous": {
            "rs_ratio": prev["rs_ratio"],
            "rs_momentum": prev["rs_momentum"],
            "quadrant": prev["quadrant"],
            "as_of": prev["t"],
        },
        "direction": {
            "dx": dx,
            "dy": dy,
            "magnitude": magnitude,
            "heading_deg": _heading_deg(dx, dy),
        },
        "trail": trail,
    }


def _analytics(sectors: List[Dict[str, Any]]) -> Dict[str, Any]:
    dist = {"leading": 0, "weakening": 0, "lagging": 0, "improving": 0}
    for s in sectors:
        q = s["current"]["quadrant"]
        if q in dist:
            dist[q] += 1
    if not sectors:
        return {"distribution": dist, "strongest": None, "weakest": None,
                "fastest_improving": None, "fastest_weakening": None}

    def _score(s):
        c = s["current"]
        # Distance from origin (100,100) in NE direction is "strength"
        return (c["rs_ratio"] - 100) + (c["rs_momentum"] - 100)

    strongest = max(sectors, key=_score)
    weakest = min(sectors, key=_score)

    def _improve(s):
        d = s["direction"]
        # Movement towards NE quadrant
        return d["dx"] + d["dy"]

    fastest_improving = max(sectors, key=_improve)
    fastest_weakening = min(sectors, key=_improve)

    def _slim(s):
        return {
            "name": s["name"], "color": s["color"],
            "rs_ratio": s["current"]["rs_ratio"],
            "rs_momentum": s["current"]["rs_momentum"],
            "quadrant": s["current"]["quadrant"],
            "dx": s["direction"]["dx"], "dy": s["direction"]["dy"],
        }

    return {
        "distribution": dist,
        "strongest": _slim(strongest),
        "weakest": _slim(weakest),
        "fastest_improving": _slim(fastest_improving),
        "fastest_weakening": _slim(fastest_weakening),
    }


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def sector_rrg(benchmark: str = "NIFTY 50",
               interval: str = "1d",
               periods: int = 10,
               force: bool = False) -> Dict[str, Any]:
    bench_name = benchmark if benchmark in BENCHMARKS else "NIFTY 50"
    bench_symbol = BENCHMARKS[bench_name]
    interval = interval if interval in _INTERVALS else "1d"
    try:
        periods = int(periods)
    except Exception:
        periods = 10
    periods = max(2, min(_MAX_PERIODS, periods))

    cache_key = f"intraday:sector_rrg:v1:{bench_name}:{interval}:{periods}"
    return snapshot_store.serve_or_refresh(
        cache_key,
        lambda: _build_sector_rrg(bench_name, bench_symbol, interval, periods),
        live=False, force=force)


def _build_sector_rrg(bench_name: str, bench_symbol: str,
                      interval: str, periods: int) -> Dict[str, Any]:
    days = _MIN_HIST_WEEKLY if interval == "1wk" else _MIN_HIST_DAILY
    bench_close = _fetch_close(bench_symbol, days=days)
    if bench_close is None or bench_close.empty:
        return {
            "error": "benchmark_unavailable",
            "benchmark": {"name": bench_name, "symbol": bench_symbol},
            "interval": interval,
            "periods": periods,
            "sectors": [],
            "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
            "cached": False,
        }

    items = list(SECTOR_INDICES.items())

    sectors: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {
            ex.submit(_sector_payload, name, sym, PALETTE[i % len(PALETTE)],
                      bench_close, interval, periods): name
            for i, (name, sym) in enumerate(items)
        }
        for fut in as_completed(future_map):
            try:
                payload = fut.result()
                if payload:
                    sectors.append(payload)
            except Exception:
                traceback.print_exc()
                continue

    sectors.sort(key=lambda s: items.index((s["name"], s["symbol"]))
                 if (s["name"], s["symbol"]) in items else 999)

    timestamps: List[str] = []
    if sectors:
        # All sectors share the same date ladder once resampled — take longest.
        longest = max(sectors, key=lambda s: len(s["trail"]))["trail"]
        timestamps = [p["t"] for p in longest]

    payload = {
        "benchmark": {"name": bench_name, "symbol": bench_symbol},
        "interval": interval,
        "periods": periods,
        "timestamps": timestamps,
        "sectors": sectors,
        "analytics": _analytics(sectors),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }

    return payload


def list_benchmarks() -> List[Dict[str, str]]:
    return [{"name": n, "symbol": s} for n, s in BENCHMARKS.items()]
