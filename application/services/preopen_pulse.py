"""Pre-Open Market Pulse — composite verdict for the day's bias.

NSE pre-open auction runs 09:00 → 09:08 IST. During that window the
exchange publishes an *indicative* opening price for each stock based on
the demand–supply imbalance of overnight orders. That single window is
the most reliable signal of whether the day will lean bullish or bearish.

This module assembles a multi-factor verdict from:

    1. NIFTY indicative open vs previous close          (weight 35 %)
    2. NIFTY-50 advance / decline + mcap-weighted move  (weight 30 %)
    3. India VIX change                                 (weight 15 %)
    4. Yesterday's locked OI-gap forecast               (weight 20 %)

The output is a single label (STRONG BULLISH / BULLISH / NEUTRAL /
BEARISH / STRONG BEARISH) plus a numeric confidence and a human-readable
narrative — designed for direct rendering in the Options Analytics page.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from application.services import cache as shared_cache
from application.services import market_data
from application.services import gap_history

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# NIFTY-50 universe (Yahoo-style; works across all providers).
# Order roughly tracks index weight so the "top contributors" visualisation
# reads naturally; mcap weights are kept in _MCAP_WEIGHT below.
NIFTY_50 = [
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
    "BHARTIARTL.NS", "ITC.NS", "LT.NS", "SBIN.NS", "AXISBANK.NS",
    "KOTAKBANK.NS", "HINDUNILVR.NS", "BAJFINANCE.NS", "M&M.NS", "MARUTI.NS",
    "SUNPHARMA.NS", "TITAN.NS", "HCLTECH.NS", "ULTRACEMCO.NS", "ASIANPAINT.NS",
    "NTPC.NS", "POWERGRID.NS", "ADANIENT.NS", "ADANIPORTS.NS", "ONGC.NS",
    "WIPRO.NS", "TATAMOTORS.NS", "TATASTEEL.NS", "JSWSTEEL.NS", "COALINDIA.NS",
    "BAJAJFINSV.NS", "BAJAJ-AUTO.NS", "NESTLEIND.NS", "TECHM.NS", "GRASIM.NS",
    "HINDALCO.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS", "EICHERMOT.NS",
    "BRITANNIA.NS", "HEROMOTOCO.NS", "INDUSINDBK.NS", "TATACONSUM.NS", "BPCL.NS",
    "APOLLOHOSP.NS", "SHRIRAMFIN.NS", "SBILIFE.NS", "HDFCLIFE.NS", "LTIM.NS",
]

# Approximate mcap weights for top names — used to weight advance/decline.
# Stocks not in this map fall back to a small default weight.
_MCAP_WEIGHT: Dict[str, float] = {
    "RELIANCE.NS": 9.5, "HDFCBANK.NS": 12.5, "ICICIBANK.NS": 8.5,
    "INFY.NS": 5.5, "TCS.NS": 4.0, "BHARTIARTL.NS": 4.5, "ITC.NS": 3.8,
    "LT.NS": 3.7, "SBIN.NS": 3.0, "AXISBANK.NS": 3.0, "KOTAKBANK.NS": 2.5,
    "HINDUNILVR.NS": 2.4, "BAJFINANCE.NS": 2.2, "M&M.NS": 2.1, "MARUTI.NS": 2.0,
    "SUNPHARMA.NS": 1.9, "TITAN.NS": 1.6, "HCLTECH.NS": 1.5, "ULTRACEMCO.NS": 1.2,
    "ASIANPAINT.NS": 1.1, "NTPC.NS": 1.5, "POWERGRID.NS": 1.3, "ADANIENT.NS": 1.0,
    "ADANIPORTS.NS": 0.9, "ONGC.NS": 1.0, "WIPRO.NS": 1.0, "TATAMOTORS.NS": 1.4,
    "TATASTEEL.NS": 1.0, "JSWSTEEL.NS": 0.9, "COALINDIA.NS": 1.0,
    "BAJAJFINSV.NS": 1.4, "BAJAJ-AUTO.NS": 0.8, "NESTLEIND.NS": 0.9,
    "TECHM.NS": 0.7, "GRASIM.NS": 0.7, "HINDALCO.NS": 0.8, "DRREDDY.NS": 0.7,
    "CIPLA.NS": 0.7, "DIVISLAB.NS": 0.5, "EICHERMOT.NS": 0.6,
    "BRITANNIA.NS": 0.5, "HEROMOTOCO.NS": 0.5, "INDUSINDBK.NS": 0.7,
    "TATACONSUM.NS": 0.5, "BPCL.NS": 0.6, "APOLLOHOSP.NS": 0.7,
    "SHRIRAMFIN.NS": 0.6, "SBILIFE.NS": 0.5, "HDFCLIFE.NS": 0.7, "LTIM.NS": 0.5,
}
_DEFAULT_WEIGHT = 0.4

# Sum of all mapped weights (so we can report each stock's share as a %
# of the universe we actually track, not a raw mcap number).
_TOTAL_WEIGHT = sum(_MCAP_WEIGHT.get(s, _DEFAULT_WEIGHT) for s in NIFTY_50)

NIFTY_INDEX = "^NSEI"
INDIAVIX = "^INDIAVIX"

# Cache keys
_LIVE_KEY = "preopen:pulse:live"          # short TTL during pre-open
_FROZEN_KEY_FMT = "preopen:pulse:frozen:{date}"  # 09:08 snapshot per day

_LIVE_TTL = 20                # 20 s during pre-open (matches series cadence)
_OFFHOURS_TTL = 5 * 60        # 5 min outside pre-open
_FROZEN_TTL = 24 * 3600       # one day

# Per-day price series for the pre-open window (08:55 → 09:30), one point
# every 20 seconds.
_SERIES_KEY_FMT = "preopen:series:{date}"
_SERIES_TTL = 12 * 3600
_SERIES_BUCKET_SECONDS = 20
_SERIES_MAX_POINTS = 120  # 35 min × 3 buckets/min + buffer
_SERIES_STORE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "_preopen_series.json",
)
_SERIES_STORE_LOCK = threading.Lock()


# ── Time-window helpers ────────────────────────────────────────────────

def _now_ist() -> _dt.datetime:
    return _dt.datetime.now(_IST)


def _in_series_window(now: _dt.datetime) -> bool:
    """Record price points only between 08:55 and 09:30 IST on weekdays."""
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (8 * 60 + 55) <= mins <= (9 * 60 + 30)


def _load_series_from_store(day_key: str) -> List[Dict[str, Any]]:
    """Read pre-open price series for ``day_key`` from durable backend store.

    Survives app restarts so the chart history fills in even when cache
    has been evicted or the process recycled.
    """
    with _SERIES_STORE_LOCK:
        try:
            if not os.path.exists(_SERIES_STORE_FILE):
                return []
            with open(_SERIES_STORE_FILE, "r", encoding="utf-8") as f:
                blob = json.load(f)
            rows = blob.get(day_key) if isinstance(blob, dict) else None
            if not isinstance(rows, list):
                return []
            return rows[-_SERIES_MAX_POINTS:]
        except Exception:
            return []


def _save_series_to_store(day_key: str, rows: List[Dict[str, Any]]) -> None:
    with _SERIES_STORE_LOCK:
        try:
            blob: Dict[str, Any] = {}
            if os.path.exists(_SERIES_STORE_FILE):
                try:
                    with open(_SERIES_STORE_FILE, "r", encoding="utf-8") as f:
                        blob = json.load(f) or {}
                        if not isinstance(blob, dict):
                            blob = {}
                except Exception:
                    blob = {}
            blob[day_key] = rows[-_SERIES_MAX_POINTS:]
            # Keep only the last ~10 sessions on disk.
            keys = sorted(blob.keys())
            for old_k in keys[:-10]:
                blob.pop(old_k, None)
            os.makedirs(os.path.dirname(_SERIES_STORE_FILE), exist_ok=True)
            tmp = _SERIES_STORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=True, separators=(",", ":"))
            os.replace(tmp, _SERIES_STORE_FILE)
        except Exception:
            pass


def _phase(now: _dt.datetime) -> Tuple[str, bool, str]:
    """Return (phase_id, is_live, human_label).

    Phases:
        before        — before 08:45 IST (still showing yesterday's signals)
        warmup        — 08:45 → 09:00 IST (pre-open hasn't started yet)
        preopen       — 09:00 → 09:08 IST (auction window — real prediction)
        frozen        — 09:08 → 09:15 IST (final indicative open locked)
        live_session  — 09:15 → 15:30 IST (compare to actual open)
        after         — after 15:30 IST (today's verdict + outcome)
        weekend       — Sat / Sun
    """
    if now.weekday() >= 5:
        return "weekend", False, "Weekend — markets closed"
    mins = now.hour * 60 + now.minute
    if mins < 8 * 60 + 45:
        return "before", False, "Pre-open opens at 09:00 IST"
    if mins < 9 * 60:
        return "warmup", False, "Awaiting pre-open auction (09:00 IST)"
    if mins < 9 * 60 + 8:
        return "preopen", True, "Pre-open auction live — building signal"
    if mins < 9 * 60 + 15:
        return "frozen", False, "Pre-open auction closed — bias locked"
    if mins < 15 * 60 + 30:
        return "live_session", False, "Market open — comparing actual vs verdict"
    return "after", False, "Post-close — today's verdict & outcome"


# ── Signal computation ─────────────────────────────────────────────────

def _signal_nifty_indicative(quote: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Signal 1: NIFTY indicative open vs previous close.

    During pre-open (09:00–09:08) the exchange's published spot is the
    indicative match price. After 09:15 it becomes the live LTP.
    """
    if not quote:
        return {"score": 0.0, "available": False, "value": None,
                "change_pct": None, "prev_close": None}
    spot = float(quote.get("price") or 0)
    prev = float(quote.get("prev_close") or 0)
    if spot <= 0 or prev <= 0:
        return {"score": 0.0, "available": False, "value": spot or None,
                "change_pct": None, "prev_close": prev or None}
    chg_pct = ((spot - prev) / prev) * 100.0
    # Map ±0.6 % onto ±1.0 score; clamp.
    score = max(-1.0, min(1.0, chg_pct / 0.6))
    return {
        "score": round(score, 3),
        "available": True,
        "value": round(spot, 2),
        "change": round(spot - prev, 2),
        "change_pct": round(chg_pct, 3),
        "prev_close": round(prev, 2),
    }


def _signal_advance_decline(quotes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Signal 2: Mcap-weighted advance/decline of NIFTY-50 components."""
    advances = declines = unchanged = 0
    weighted_sum = 0.0
    weight_total = 0.0
    contributors: List[Dict[str, Any]] = []

    for sym in NIFTY_50:
        q = quotes.get(sym) or {}
        price = float(q.get("price") or 0)
        prev = float(q.get("prev_close") or 0)
        if price <= 0 or prev <= 0:
            continue
        chg_pct = ((price - prev) / prev) * 100.0
        w = _MCAP_WEIGHT.get(sym, _DEFAULT_WEIGHT)
        weighted_sum += chg_pct * w
        weight_total += w
        if chg_pct > 0.05:
            advances += 1
        elif chg_pct < -0.05:
            declines += 1
        else:
            unchanged += 1
        contributors.append({
            "symbol": sym.replace(".NS", ""),
            "name": q.get("name") or sym.replace(".NS", ""),
            "change_pct": round(chg_pct, 2),
            "contribution": round(chg_pct * w, 3),
            "weight_pct": round((w / _TOTAL_WEIGHT) * 100.0, 2) if _TOTAL_WEIGHT > 0 else 0.0,
        })

    counted = advances + declines + unchanged
    if not counted or weight_total <= 0:
        return {"score": 0.0, "available": False, "advances": 0, "declines": 0,
                "unchanged": 0, "weighted_pct": 0.0, "top": [], "bottom": []}

    weighted_pct = weighted_sum / weight_total
    # Map ±0.4 % weighted move onto ±1.0 score.
    score = max(-1.0, min(1.0, weighted_pct / 0.4))

    contributors.sort(key=lambda x: x["contribution"], reverse=True)
    top = contributors[:5]
    bottom = list(reversed(contributors[-5:]))

    return {
        "score": round(score, 3),
        "available": True,
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "counted": counted,
        "weighted_pct": round(weighted_pct, 3),
        "top": top,
        "bottom": bottom,
    }


def _signal_vix(quote: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Signal 3: India VIX change.

    Falling VIX → bullish (risk appetite up). Rising VIX → bearish.
    Score is *inverted* sign of change.
    """
    if not quote:
        return {"score": 0.0, "available": False}
    val = float(quote.get("price") or 0)
    prev = float(quote.get("prev_close") or 0)
    if val <= 0 or prev <= 0:
        return {"score": 0.0, "available": False, "value": val or None,
                "change_pct": None}
    chg_pct = ((val - prev) / prev) * 100.0
    # ±5 % VIX move → ±1.0 score (negated).
    score = max(-1.0, min(1.0, -chg_pct / 5.0))
    return {
        "score": round(score, 3),
        "available": True,
        "value": round(val, 2),
        "change": round(val - prev, 2),
        "change_pct": round(chg_pct, 2),
    }


def _signal_oi_carry() -> Dict[str, Any]:
    """Signal 4: yesterday's locked OI-gap forecast.

    Uses :mod:`gap_history` — the previous session's 15:15 IST locked
    decision. Score = predicted_score (already in [-1, +1]).
    """
    try:
        items = gap_history.recent("NIFTY", limit=2)
    except Exception as e:
        log.debug("oi_carry: gap_history.recent failed: %s", e)
        return {"score": 0.0, "available": False}

    if not items:
        return {"score": 0.0, "available": False}

    # Find the most recent dated entry (excluding today if user is
    # viewing post-close — we want *yesterday's* call for the open).
    today_key = _now_ist().strftime("%Y-%m-%d")
    pick = None
    for it in items:
        if (it.get("date") or "") == today_key:
            continue
        pick = it
        break
    if pick is None:
        pick = items[0]

    label = (pick.get("predicted") or "").upper()
    score = float(pick.get("predicted_score") or 0)
    conf = float(pick.get("confidence") or 0)
    if not label or label in ("FLAT", "TOO EARLY"):
        # Flat call still informative but score = 0.
        return {
            "score": 0.0, "available": True, "label": label or "FLAT",
            "confidence": round(conf, 2), "date": pick.get("date"),
            "predicted_gap_pct": pick.get("predicted_gap_pct"),
        }
    return {
        "score": round(max(-1.0, min(1.0, score)), 3),
        "available": True,
        "label": label,
        "confidence": round(conf, 2),
        "date": pick.get("date"),
        "predicted_gap_pct": pick.get("predicted_gap_pct"),
        "predicted_gap_points": pick.get("predicted_gap_points"),
    }


# ── Verdict assembly ───────────────────────────────────────────────────

_W_NIFTY = 0.35
_W_AD = 0.30
_W_VIX = 0.15
_W_OI = 0.20

_BULLISH_TH = 0.20
_STRONG_TH = 0.55


def _label_for(score: float) -> str:
    if score >= _STRONG_TH:
        return "STRONG BULLISH"
    if score >= _BULLISH_TH:
        return "BULLISH"
    if score <= -_STRONG_TH:
        return "STRONG BEARISH"
    if score <= -_BULLISH_TH:
        return "BEARISH"
    return "NEUTRAL"


def _confidence(score: float, signals: Dict[str, Dict[str, Any]]) -> float:
    """Confidence rises with |score| AND with the number of available signals."""
    available = sum(1 for s in signals.values() if s.get("available"))
    coverage = available / max(1, len(signals))
    base = abs(score) * 0.7 + coverage * 0.3
    # Bonus when signals agree directionally.
    pos = sum(1 for s in signals.values() if s.get("available") and (s.get("score") or 0) > 0.05)
    neg = sum(1 for s in signals.values() if s.get("available") and (s.get("score") or 0) < -0.05)
    if available >= 3 and (pos == 0 or neg == 0):
        base = min(1.0, base + 0.10)
    return round(min(1.0, max(0.0, base)), 2)


def _narrative(label: str, signals: Dict[str, Dict[str, Any]]) -> str:
    parts: List[str] = []
    nifty = signals.get("nifty") or {}
    ad = signals.get("ad") or {}
    vix = signals.get("vix") or {}
    oi = signals.get("oi") or {}

    if nifty.get("available"):
        c = nifty.get("change_pct") or 0
        sign = "+" if c >= 0 else ""
        parts.append(f"NIFTY indicative {sign}{c:.2f}%")
    if ad.get("available"):
        a, d = ad.get("advances", 0), ad.get("declines", 0)
        wp = ad.get("weighted_pct") or 0
        sign = "+" if wp >= 0 else ""
        parts.append(f"{a}/{d} A/D (mcap-wtd {sign}{wp:.2f}%)")
    if vix.get("available"):
        c = vix.get("change_pct") or 0
        sign = "+" if c >= 0 else ""
        parts.append(f"VIX {sign}{c:.2f}%")
    if oi.get("available"):
        lbl = oi.get("label") or "FLAT"
        parts.append(f"yesterday's OI lock: {lbl}")

    if not parts:
        return "Signals not available yet — waiting for pre-open data."
    return f"{label.title()} bias from " + " · ".join(parts) + "."


def _compute_pulse() -> Dict[str, Any]:
    now = _now_ist()
    phase_id, is_live, phase_label = _phase(now)

    # ── Data fetch (parallel) ──────────────────────────────────────────
    syms = NIFTY_50 + [NIFTY_INDEX, INDIAVIX]
    quotes: Dict[str, Dict[str, Any]] = {}
    try:
        quotes = market_data.get_quotes(syms) or {}
    except Exception as e:
        log.warning("preopen: get_quotes failed: %s", e)
        quotes = {}

    sig_nifty = _signal_nifty_indicative(quotes.get(NIFTY_INDEX))
    sig_ad = _signal_advance_decline(quotes)
    sig_vix = _signal_vix(quotes.get(INDIAVIX))
    sig_oi = _signal_oi_carry()

    # ── Weighted score (only across available signals) ─────────────────
    weights = {
        "nifty": _W_NIFTY, "ad": _W_AD, "vix": _W_VIX, "oi": _W_OI,
    }
    signals = {"nifty": sig_nifty, "ad": sig_ad, "vix": sig_vix, "oi": sig_oi}

    num = 0.0
    den = 0.0
    for name, sig in signals.items():
        if not sig.get("available"):
            continue
        num += float(sig.get("score") or 0) * weights[name]
        den += weights[name]
    score = num / den if den > 0 else 0.0
    score = max(-1.0, min(1.0, round(score, 3)))

    label = _label_for(score)
    conf = _confidence(score, signals)
    summary = _narrative(label, signals)

    payload: Dict[str, Any] = {
        "as_of": now.strftime("%H:%M:%S IST"),
        "date": now.strftime("%Y-%m-%d"),
        "phase": phase_id,
        "phase_label": phase_label,
        "is_live": is_live,
        "verdict": {
            "label": label,
            "score": score,
            "confidence": conf,
            "summary": summary,
        },
        "signals": {
            "nifty": sig_nifty,
            "ad": sig_ad,
            "vix": sig_vix,
            "oi": sig_oi,
        },
        "weights": weights,
    }

    # ── Per-minute price series (08:55 → 09:30 IST) ────────────────────
    # Recovers history from disk on first call so the chart fills even
    # after a process restart. Appends one point per minute.
    day_key = now.strftime("%Y%m%d")
    series_key = _SERIES_KEY_FMT.format(date=day_key)
    series = shared_cache.jget(series_key)
    if not isinstance(series, list) or not series:
        series = _load_series_from_store(day_key)
        if series:
            shared_cache.jset(series_key, series, ttl=_SERIES_TTL)
    series = list(series or [])

    if _in_series_window(now) and sig_nifty.get("available"):
        bucket_sec = (now.second // _SERIES_BUCKET_SECONDS) * _SERIES_BUCKET_SECONDS
        bucket = f"{now.hour:02d}:{now.minute:02d}:{bucket_sec:02d}"
        prev_close = sig_nifty.get("prev_close")
        spot = sig_nifty.get("value")
        chg_pct = sig_nifty.get("change_pct")
        if spot is not None:
            point = {
                "t": bucket,
                "spot": spot,
                "prev_close": prev_close,
                "change_pct": chg_pct,
                "phase": phase_id,
            }
            if series and series[-1].get("t") == bucket:
                series[-1] = point
            else:
                series.append(point)
            if len(series) > _SERIES_MAX_POINTS:
                series = series[-_SERIES_MAX_POINTS:]
            shared_cache.jset(series_key, series, ttl=_SERIES_TTL)
            _save_series_to_store(day_key, series)

    payload["series"] = series

    # ── Outcome reconciliation (post-09:15) ────────────────────────────
    # If we have a frozen verdict from 09:08+ and we're now past 09:15,
    # surface hit/miss against the actual NIFTY open.
    if phase_id in ("live_session", "after"):
        frozen = shared_cache.jget(_FROZEN_KEY_FMT.format(date=payload["date"]))
        if isinstance(frozen, dict):
            payload["frozen_verdict"] = frozen.get("verdict")
            payload["frozen_at"] = frozen.get("as_of")
            actual_pct = sig_nifty.get("change_pct")
            if actual_pct is not None and frozen.get("verdict"):
                fl = (frozen["verdict"].get("label") or "").upper()
                bullish_call = "BULLISH" in fl
                bearish_call = "BEARISH" in fl
                hit = (
                    (bullish_call and actual_pct >= 0.05) or
                    (bearish_call and actual_pct <= -0.05)
                )
                if fl == "NEUTRAL":
                    outcome = "FLAT_OK" if abs(actual_pct) < 0.15 else "FLAT_MISS"
                else:
                    outcome = "HIT" if hit else "MISS"
                payload["outcome"] = {
                    "status": outcome,
                    "actual_change_pct": round(actual_pct, 3),
                }

    return payload


# ── Public API ─────────────────────────────────────────────────────────

def get_preopen_pulse(force_refresh: bool = False) -> Dict[str, Any]:
    """Return the current pre-open pulse payload.

    Caching strategy:
        * During the live pre-open window (09:00–09:08) we recompute every
          ~30 s so the panel feels live.
        * Outside that window we cache for several minutes — the inputs
          don't move much.
        * At 09:08 we *freeze* the verdict to a per-day key so the locked
          prediction stays stable regardless of later refreshes.
    """
    now = _now_ist()
    phase_id, is_live, _ = _phase(now)

    if not force_refresh:
        cached = shared_cache.jget(_LIVE_KEY)
        if isinstance(cached, dict) and cached.get("phase") == phase_id:
            return cached

    with shared_cache.lock("preopen:build", ttl=15, blocking=True, wait=4.0) as got:
        if not force_refresh:
            cached = shared_cache.jget(_LIVE_KEY)
            if isinstance(cached, dict) and cached.get("phase") == phase_id:
                return cached
        if not got:
            return shared_cache.jget(_LIVE_KEY) or {
                "as_of": now.strftime("%H:%M:%S IST"),
                "phase": phase_id,
                "phase_label": "Computing…",
                "verdict": {"label": "NEUTRAL", "score": 0.0,
                            "confidence": 0.0, "summary": "Waiting for data."},
                "signals": {},
            }
        data = _compute_pulse()

        # Freeze final verdict at 09:08 (first time we enter "frozen").
        if phase_id == "frozen":
            frozen_key = _FROZEN_KEY_FMT.format(date=data["date"])
            existing = shared_cache.jget(frozen_key)
            if not isinstance(existing, dict):
                shared_cache.jset(frozen_key, {
                    "verdict": data["verdict"],
                    "signals": data["signals"],
                    "as_of": data["as_of"],
                }, ttl=_FROZEN_TTL)

        ttl = _LIVE_TTL if is_live else _OFFHOURS_TTL
        shared_cache.jset(_LIVE_KEY, data, ttl=ttl)
        return data
