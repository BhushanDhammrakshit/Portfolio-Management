"""Market Regime Meter — classifies the option market as SIDEWAYS / NEUTRAL / VOLATILE.

Composite score (0–100) blended from five option-derived signals:

  1. India VIX level                      (weight 30%)
  2. ATM straddle implied daily move %    (weight 25%)
  3. Gamma pinning (peak gamma-OI strike) (weight 20%)
  4. OI wall range (S/R cage width)       (weight 15%)
  5. IV skew (OTM-put IV vs ATM IV)       (weight 10%)

Higher score → volatility-expansion / trending regime.
Lower score  → range-bound / pinning regime.

Bands:
    0–35   → SIDEWAYS  (favour theta strategies)
    35–65  → NEUTRAL   (defined-risk spreads)
    65–100 → VOLATILE  (option-buying / debit spreads)

Designed for NIFTY weekly chains. Falls back to neutral sub-scores when an
input is missing so the meter degrades gracefully instead of raising.
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Dict, List, Optional, Tuple

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


# ── Helpers ─────────────────────────────────────────────────────────────

def _expiry_to_date(expiry: str) -> Optional[_dt.date]:
    if not expiry:
        return None
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(expiry, fmt).date()
        except ValueError:
            continue
    return None


def _days_to_expiry(expiry: str) -> float:
    d = _expiry_to_date(expiry)
    if not d:
        return 1.0
    today = _dt.datetime.now(_IST).date()
    # Min 0.5 day so we never divide by zero / sqrt(0) on expiry-day calls.
    return max(0.5, float((d - today).days) + 0.5)


def _norm(value: float, breakpoints: List[Tuple[float, float]]) -> float:
    """Piecewise-linear interpolation from `value` onto a 0–100 score.

    Anchors below the lowest input clamp to the lowest output; anchors above
    the highest input clamp to the highest output.
    """
    bps = sorted(breakpoints)
    if value <= bps[0][0]:
        return float(bps[0][1])
    if value >= bps[-1][0]:
        return float(bps[-1][1])
    for (x0, y0), (x1, y1) in zip(bps, bps[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return float(y0)
            t = (value - x0) / (x1 - x0)
            return float(y0 + t * (y1 - y0))
    return 50.0


def _atm_row(rows: List[Dict[str, Any]], spot: float) -> Optional[Dict[str, Any]]:
    if not rows or spot <= 0:
        return None
    return min(rows, key=lambda r: abs(float(r.get("strike") or 0) - spot))


def _avg_atm_iv(rows: List[Dict[str, Any]], spot: float, window: int = 1) -> Optional[float]:
    """Mean CE+PE IV across ATM ± `window` strikes (percent units)."""
    if not rows or spot <= 0:
        return None
    sorted_rows = sorted(rows, key=lambda r: float(r.get("strike") or 0))
    n = len(sorted_rows)
    atm_idx = min(range(n), key=lambda i: abs(float(sorted_rows[i].get("strike") or 0) - spot))
    lo = max(0, atm_idx - window)
    hi = min(n - 1, atm_idx + window)
    ivs: List[float] = []
    for r in sorted_rows[lo:hi + 1]:
        for leg in ("ce", "pe"):
            v = (r.get(leg) or {}).get("iv")
            if v and v > 0:
                ivs.append(float(v))
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def _bs_gamma(spot: float, strike: float, t_years: float, sigma: float) -> float:
    """Black–Scholes gamma. `sigma` is annualised volatility in decimal form."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return 0.0
    try:
        d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t_years) / (sigma * math.sqrt(t_years))
        return math.exp(-0.5 * d1 * d1) / (spot * sigma * math.sqrt(2.0 * math.pi * t_years))
    except (ValueError, ZeroDivisionError):
        return 0.0


# ── Sub-components (each returns score 0–100 + a UI dict) ──────────────

def _component_vix(vix: Optional[Dict[str, Any]]) -> Tuple[float, Dict[str, Any]]:
    val = float((vix or {}).get("value") or 0.0)
    if val <= 0:
        return 50.0, {
            "name": "India VIX",
            "value": "—",
            "score": 50,
            "note": "VIX unavailable — assumed neutral",
        }
    score = _norm(val, [
        (8.0, 5), (12.0, 25), (15.0, 40), (18.0, 60),
        (22.0, 78), (27.0, 90), (35.0, 98),
    ])
    if val < 12:
        note = "Complacent — implied vol below long-term floor"
    elif val < 18:
        note = "Normal volatility regime"
    elif val < 25:
        note = "Elevated — caution"
    else:
        note = "High — extreme fear / volatility"
    return score, {
        "name": "India VIX",
        "value": f"{val:.2f}",
        "score": round(score),
        "note": note,
    }


def _component_straddle(rows: List[Dict[str, Any]], spot: float,
                        expiry: str) -> Tuple[float, Dict[str, Any]]:
    atm = _atm_row(rows, spot)
    if not atm or spot <= 0:
        return 50.0, {"name": "ATM Straddle", "value": "—", "score": 50,
                      "note": "ATM strike unavailable"}
    ce_ltp = float(((atm.get("ce") or {}).get("ltp") or 0))
    pe_ltp = float(((atm.get("pe") or {}).get("ltp") or 0))
    straddle = ce_ltp + pe_ltp
    if straddle <= 0:
        return 50.0, {"name": "ATM Straddle", "value": "—", "score": 50,
                      "note": "ATM premiums unavailable"}
    dte = _days_to_expiry(expiry)
    # Daily expected move % of spot (scale straddle by sqrt(time) per BSM).
    daily_move_pct = (straddle / spot) * 100.0 / max(1.0, math.sqrt(dte))
    score = _norm(daily_move_pct, [
        (0.20, 5), (0.40, 25), (0.55, 40), (0.75, 60), (1.00, 78), (1.50, 92),
    ])
    return score, {
        "name": "ATM Straddle",
        "value": f"±{daily_move_pct:.2f}%/day",
        "score": round(score),
        "note": f"Straddle ≈ {straddle:.0f} • {dte:.1f}d to expiry",
    }


def _component_gamma_pin(rows: List[Dict[str, Any]], spot: float,
                          expiry: str,
                          vix_value: float = 0.0) -> Tuple[float, Dict[str, Any]]:
    if not rows or spot <= 0:
        return 50.0, {"name": "Gamma Pinning", "value": "—", "score": 50,
                      "note": "Chain rows unavailable"}
    dte_years = _days_to_expiry(expiry) / 365.0
    vix_sigma = (vix_value / 100.0) if vix_value > 0 else 0.0
    per_strike: List[Tuple[float, float]] = []  # (strike, gamma_weighted_oi)
    used_proxy = False
    for r in rows:
        strike = float(r.get("strike") or 0)
        if strike <= 0:
            continue
        ce = r.get("ce") or {}
        pe = r.get("pe") or {}
        ivs = [float(v) for v in (ce.get("iv"), pe.get("iv")) if v and float(v) > 0]
        if ivs:
            sigma = (sum(ivs) / len(ivs)) / 100.0  # IV is in percent
        elif vix_sigma > 0:
            sigma = vix_sigma  # provider didn't return per-strike IV; VIX proxy.
            used_proxy = True
        else:
            continue
        gamma = _bs_gamma(spot, strike, dte_years, sigma)
        if gamma <= 0:
            continue
        oi_total = float(ce.get("oi") or 0) + float(pe.get("oi") or 0)
        if oi_total <= 0:
            continue
        per_strike.append((strike, gamma * oi_total))
    if not per_strike:
        return 50.0, {"name": "Gamma Pinning", "value": "—", "score": 50,
                      "note": "IV unavailable — pinning indeterminate"}
    total = sum(g for _, g in per_strike)
    peak_strike, peak_gamma = max(per_strike, key=lambda x: x[1])
    dist_pct = abs(peak_strike - spot) / spot * 100.0
    concentration = peak_gamma / total if total > 0 else 0.0
    # Tight pin = peak near spot + high concentration on one strike → SIDEWAYS.
    dist_score = _norm(dist_pct, [(0.0, 0), (0.3, 20), (0.7, 40), (1.5, 65), (3.0, 90)])
    conc_score = _norm(concentration, [(0.10, 90), (0.18, 65), (0.28, 35), (0.40, 10)])
    score = 0.55 * dist_score + 0.45 * conc_score
    note = f"Concentration {concentration * 100:.0f}% on peak strike"
    if used_proxy:
        note += " (VIX proxy)"
    return score, {
        "name": "Gamma Pinning",
        "value": f"peak @ {int(peak_strike)} ({dist_pct:.2f}% off)",
        "score": round(score),
        "note": note,
    }


def _component_oi_walls(sr_levels: Dict[str, Any],
                         spot: float) -> Tuple[float, Dict[str, Any]]:
    sup = (sr_levels or {}).get("support") or []
    res = (sr_levels or {}).get("resistance") or []
    if not sup or not res or spot <= 0:
        return 50.0, {"name": "OI Wall Range", "value": "—", "score": 50,
                      "note": "S/R levels unavailable"}
    sup0 = float(sup[0].get("strike") or 0)
    res0 = float(res[0].get("strike") or 0)
    if sup0 <= 0 or res0 <= 0 or sup0 >= res0:
        return 50.0, {"name": "OI Wall Range", "value": "—", "score": 50,
                      "note": "OI walls inverted or missing"}
    range_pct = (res0 - sup0) / spot * 100.0
    score = _norm(range_pct, [(0.6, 5), (1.2, 22), (2.0, 45), (3.5, 70), (5.5, 92)])
    return score, {
        "name": "OI Wall Range",
        "value": f"{int(sup0)}–{int(res0)} ({range_pct:.2f}%)",
        "score": round(score),
        "note": "Tight cage → pinning; wide walls → expansion",
    }


def _component_skew(rows: List[Dict[str, Any]],
                     spot: float) -> Tuple[float, Dict[str, Any]]:
    if not rows or spot <= 0:
        return 50.0, {"name": "IV Skew", "value": "—", "score": 50,
                      "note": "Chain rows unavailable"}
    sorted_rows = sorted(rows, key=lambda r: float(r.get("strike") or 0))
    atm_iv = _avg_atm_iv(sorted_rows, spot, window=1)
    if not atm_iv:
        return 50.0, {"name": "IV Skew", "value": "—", "score": 50,
                      "note": "IV unavailable"}
    otm_put_ivs: List[float] = []
    for r in sorted_rows:
        strike = float(r.get("strike") or 0)
        if strike <= 0:
            continue
        dist = (strike - spot) / spot
        pe_iv = float(((r.get("pe") or {}).get("iv") or 0))
        if -0.04 <= dist <= -0.015 and pe_iv > 0:
            otm_put_ivs.append(pe_iv)
    if not otm_put_ivs:
        return 50.0, {"name": "IV Skew", "value": "—", "score": 50,
                      "note": "Not enough OTM-put strikes"}
    otm_put_iv = sum(otm_put_ivs) / len(otm_put_ivs)
    put_skew = otm_put_iv - atm_iv  # positive ⇒ fear / vol bid
    score = _norm(put_skew, [(-1.0, 15), (0.0, 35), (1.0, 55), (2.5, 75), (5.0, 92)])
    return score, {
        "name": "IV Skew",
        "value": f"put skew {put_skew:+.2f}",
        "score": round(score),
        "note": f"ATM IV {atm_iv:.1f}% • OTM-put IV {otm_put_iv:.1f}%",
    }


# ── Public entry point ─────────────────────────────────────────────────

_WEIGHTS = {
    "vix": 0.30,
    "straddle": 0.25,
    "gamma": 0.20,
    "walls": 0.15,
    "skew": 0.10,
}


def compute_regime(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the market regime meter from an option-chain payload.

    Inputs read off `payload`:
        rows, spot, expiry, vix, sr_levels

    Returns a dict with `score` (0–100), `label`, `band`, `summary`,
    `components` (list of sub-score dicts with name/value/score/weight/note)
    and `as_of`.
    """
    rows = payload.get("rows") or []
    spot = float(payload.get("spot") or 0)
    expiry = payload.get("expiry") or ""
    vix = payload.get("vix") or {}
    sr = payload.get("sr_levels") or {}

    vix_s, vix_c = _component_vix(vix)
    str_s, str_c = _component_straddle(rows, spot, expiry)
    gam_s, gam_c = _component_gamma_pin(rows, spot, expiry,
                                        vix_value=float(vix.get("value") or 0.0))
    wal_s, wal_c = _component_oi_walls(sr, spot)
    skw_s, skw_c = _component_skew(rows, spot)

    composite = (
        vix_s * _WEIGHTS["vix"]
        + str_s * _WEIGHTS["straddle"]
        + gam_s * _WEIGHTS["gamma"]
        + wal_s * _WEIGHTS["walls"]
        + skw_s * _WEIGHTS["skew"]
    )
    score = round(composite, 1)

    if score < 35:
        band, label = "sideways", "SIDEWAYS"
        summary = (
            f"Range-bound regime ({score:.0f}/100). Premiums price a tight day — "
            "favour theta strategies (short straddle/strangle, iron condor)."
        )
    elif score < 65:
        band, label = "neutral", "NEUTRAL"
        summary = (
            f"Mixed signals ({score:.0f}/100). Directional bias unclear — keep "
            "size light and use defined-risk spreads."
        )
    else:
        band, label = "volatile", "VOLATILE"
        summary = (
            f"Volatility-expansion regime ({score:.0f}/100). Expect wider ranges — "
            "favour option-buying / debit spreads, avoid naked short premium."
        )

    return {
        "score": score,
        "label": label,
        "band": band,
        "summary": summary,
        "components": [
            {**vix_c, "weight": int(_WEIGHTS["vix"] * 100)},
            {**str_c, "weight": int(_WEIGHTS["straddle"] * 100)},
            {**gam_c, "weight": int(_WEIGHTS["gamma"] * 100)},
            {**wal_c, "weight": int(_WEIGHTS["walls"] * 100)},
            {**skw_c, "weight": int(_WEIGHTS["skew"] * 100)},
        ],
        "as_of": _dt.datetime.now(_IST).strftime("%I:%M %p IST"),
    }
