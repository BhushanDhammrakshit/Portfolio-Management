"""Score a fundamentals payload + derive 1-year price targets.

All inputs come from :mod:`fundamentals_provider`. Every function is pure and
returns None for any metric it can't compute, so the UI can still render a
partial scorecard.

Three deliverables:

* ``compute_scores(payload)`` — pillar scores (Valuation / Profitability /
  Growth / Safety), composite 0..100, verdict label, Piotroski F-Score,
  and a list of red flags.

* ``compute_targets(payload)`` — 1-year price targets via DCF, P/E re-rating
  and Graham number; mean target + expected 1-year return.

* ``analyse(payload)`` — convenience: returns ``{score, targets, red_flags}``.
"""
from __future__ import annotations

from typing import Any, Optional


# ── sector hints for valuation scoring (fallback medians for the Indian
# market when peer data isn't readily available) ──────────────────────
SECTOR_PE_MEDIAN = {
    "Technology": 26, "Information Technology": 26, "Communication Services": 22,
    "Financial Services": 18, "Finance": 18, "Banks": 14,
    "Consumer Cyclical": 30, "Consumer Defensive": 38, "FMCG": 45,
    "Healthcare": 30, "Industrials": 25, "Energy": 12, "Utilities": 18,
    "Basic Materials": 16, "Real Estate": 28, "Auto": 22,
}
DEFAULT_PE_MEDIAN = 22


# ── helpers ────────────────────────────────────────────────────────────

def _clip(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def _bucket(value: Optional[float], thresholds, *, higher_better: bool = True) -> int:
    """Map a value to 0/25/50/75/100 across four threshold breakpoints.

    ``thresholds`` is ascending. With ``higher_better=True``::

        value < t0 → 0
        t0..t1     → 25
        t1..t2     → 50
        t2..t3     → 75
        ≥ t3       → 100
    """
    if value is None:
        return 50  # neutral when missing
    score_steps = [0, 25, 50, 75, 100]
    if higher_better:
        for i, t in enumerate(thresholds):
            if value < t:
                return score_steps[i]
        return 100
    # lower better — invert
    for i, t in enumerate(thresholds):
        if value < t:
            return score_steps[-(i + 1)]
    return 0


def _peer_pe(sector: Optional[str]) -> float:
    if not sector:
        return DEFAULT_PE_MEDIAN
    return SECTOR_PE_MEDIAN.get(sector, DEFAULT_PE_MEDIAN)


# ── pillar scoring ─────────────────────────────────────────────────────

def _score_valuation(m: dict, sector: Optional[str]) -> int:
    v = m.get("valuation") or {}
    peer = _peer_pe(sector)
    parts = []

    pe = v.get("pe_trailing")
    if pe is not None and pe > 0:
        # cheap = pe well below peer, expensive = pe well above
        parts.append(_bucket(peer / pe, [0.6, 0.9, 1.1, 1.4], higher_better=True))

    peg = v.get("peg")
    if peg is not None and peg > 0:
        parts.append(_bucket(peg, [0.8, 1.2, 1.8, 2.5], higher_better=False))

    pb = v.get("pb")
    if pb is not None and pb > 0:
        parts.append(_bucket(pb, [1.5, 3, 5, 8], higher_better=False))

    ev_eb = v.get("ev_ebitda")
    if ev_eb is not None and ev_eb > 0:
        parts.append(_bucket(ev_eb, [8, 14, 20, 30], higher_better=False))

    dy = v.get("div_yield_pct")
    if dy is not None:
        parts.append(_bucket(dy, [0.5, 1.5, 3, 5], higher_better=True))

    return round(sum(parts) / len(parts)) if parts else 50


def _score_profitability(m: dict) -> int:
    p = m.get("profitability") or {}
    parts = []
    for key, thr in [
        ("roe_pct", [5, 12, 18, 25]),
        ("roce_pct", [8, 14, 20, 28]),
        ("roa_pct", [2, 5, 9, 14]),
        ("net_margin_pct", [3, 8, 15, 25]),
        ("operating_margin_pct", [5, 12, 20, 30]),
    ]:
        if p.get(key) is not None:
            parts.append(_bucket(p[key], thr, higher_better=True))
    return round(sum(parts) / len(parts)) if parts else 50


def _score_growth(m: dict) -> int:
    g = m.get("growth") or {}
    parts = []
    for key, thr in [
        ("revenue_cagr_3y_pct", [3, 8, 15, 25]),
        ("eps_cagr_3y_pct", [3, 10, 18, 30]),
        ("revenue_yoy_pct", [0, 8, 15, 25]),
        ("eps_yoy_pct", [0, 10, 20, 35]),
        ("earnings_qoq_pct", [0, 10, 25, 50]),
    ]:
        if g.get(key) is not None:
            parts.append(_bucket(g[key], thr, higher_better=True))
    return round(sum(parts) / len(parts)) if parts else 50


def _score_safety(m: dict) -> int:
    h = m.get("financial_health") or {}
    parts = []
    if h.get("de") is not None:
        parts.append(_bucket(h["de"], [0.3, 0.7, 1.2, 2.0], higher_better=False))
    if h.get("interest_coverage") is not None:
        parts.append(_bucket(h["interest_coverage"], [2, 4, 8, 15], higher_better=True))
    if h.get("current_ratio") is not None:
        parts.append(_bucket(h["current_ratio"], [1.0, 1.3, 1.8, 2.5], higher_better=True))
    if h.get("fcf_yield_pct") is not None:
        parts.append(_bucket(h["fcf_yield_pct"], [0, 3, 6, 10], higher_better=True))
    if h.get("fcf_positive_years") is not None:
        parts.append(_bucket(h["fcf_positive_years"], [1, 2, 3, 4], higher_better=True))
    return round(sum(parts) / len(parts)) if parts else 50


def _piotroski(payload: dict) -> Optional[int]:
    """Classic 9-point F-Score. Returns None if too few inputs are available."""
    m = payload.get("metrics") or {}
    hist = m.get("history") or {}
    ni = hist.get("net_income_series") or []
    ocf = hist.get("ocf_series") or []
    rev = hist.get("revenue_series") or []
    prof = m.get("profitability") or {}
    health = m.get("financial_health") or {}

    score = 0
    checks = 0

    if ni:
        checks += 1
        if ni[0] > 0:
            score += 1
    if prof.get("roa_pct") is not None:
        checks += 1
        if prof["roa_pct"] > 0:
            score += 1
    if ocf:
        checks += 1
        if ocf[0] > 0:
            score += 1
    if ocf and ni:
        checks += 1
        if ocf[0] > ni[0]:
            score += 1
    if health.get("current_ratio") is not None:
        checks += 1
        if health["current_ratio"] >= 1:
            score += 1
    if rev and len(rev) >= 2:
        checks += 1
        if rev[0] >= rev[1]:
            score += 1
    if ni and len(ni) >= 2:
        checks += 1
        if ni[0] >= ni[1]:
            score += 1
    if health.get("de") is not None:
        checks += 1
        if health["de"] <= 1:
            score += 1
    if prof.get("gross_margin_pct") is not None:
        checks += 1
        if prof["gross_margin_pct"] >= 20:
            score += 1

    if checks < 5:
        return None
    # Scale partial-data score to the standard 0..9 range
    return round(score * 9 / checks)


def _verdict(composite: int) -> str:
    # NOTE: We deliberately use market-sentiment language (bullish / bearish)
    # rather than actionable calls (buy / sell). This is an educational
    # fundamentals score, not investment advice or a SEBI-regulated
    # buy/sell recommendation.
    if composite >= 80:
        return "Strongly Bullish"
    if composite >= 65:
        return "Bullish"
    if composite >= 45:
        return "Neutral"
    if composite >= 30:
        return "Cautious"
    return "Bearish"


def _red_flags(payload: dict) -> list:
    flags = []
    m = payload.get("metrics") or {}
    h = m.get("financial_health") or {}
    p = m.get("profitability") or {}
    g = m.get("growth") or {}
    v = m.get("valuation") or {}
    hist = m.get("history") or {}

    if h.get("de") is not None and h["de"] > 2:
        flags.append(f"High leverage (D/E {h['de']:.2f})")
    if h.get("interest_coverage") is not None and h["interest_coverage"] < 2:
        flags.append(f"Weak interest coverage ({h['interest_coverage']:.1f}x)")
    if h.get("current_ratio") is not None and h["current_ratio"] < 1:
        flags.append(f"Liquidity strain (current ratio {h['current_ratio']:.2f})")
    if p.get("roe_pct") is not None and p["roe_pct"] < 5:
        flags.append(f"Low ROE ({p['roe_pct']:.1f}%)")
    if p.get("net_margin_pct") is not None and p["net_margin_pct"] < 0:
        flags.append("Loss-making (negative net margin)")
    if g.get("revenue_yoy_pct") is not None and g["revenue_yoy_pct"] < -5:
        flags.append(f"Revenue declining YoY ({g['revenue_yoy_pct']:.1f}%)")
    if v.get("pe_trailing") is not None and v["pe_trailing"] > 80:
        flags.append(f"Stretched valuation (P/E {v['pe_trailing']:.0f})")
    ni = hist.get("net_income_series") or []
    if ni and len(ni) >= 2 and ni[0] < ni[1] * 0.7:
        flags.append("Earnings dropped >30% YoY")
    return flags


def compute_scores(payload: dict) -> dict:
    m = payload.get("metrics") or {}
    sector = payload.get("sector")
    v = _score_valuation(m, sector)
    p = _score_profitability(m)
    g = _score_growth(m)
    s = _score_safety(m)
    composite = round(0.25 * v + 0.25 * p + 0.25 * g + 0.25 * s)
    return {
        "pillars": {"valuation": v, "profitability": p, "growth": g, "safety": s},
        "composite": int(_clip(composite)),
        "verdict": _verdict(composite),
        "piotroski": _piotroski(payload),
    }


# ── target price models ────────────────────────────────────────────────

def _target_dcf(payload: dict) -> Optional[float]:
    """Two-stage FCF DCF. Very rough — for direction, not precision."""
    m = payload.get("metrics") or {}
    h = m.get("financial_health") or {}
    v = m.get("valuation") or {}
    fcf = h.get("fcf")
    g = m.get("growth") or {}
    growth = g.get("revenue_cagr_3y_pct") or g.get("revenue_yoy_pct") or 8
    # cap growth assumption to keep DCF sane
    growth = max(min(growth, 25), 0) / 100
    discount = 0.12
    terminal_g = 0.04
    if not fcf or fcf <= 0:
        return None

    # 5-year explicit forecast
    npv = 0.0
    for year in range(1, 6):
        proj = fcf * ((1 + growth) ** year)
        npv += proj / ((1 + discount) ** year)
    # Terminal value (Gordon growth)
    terminal_fcf = fcf * ((1 + growth) ** 5) * (1 + terminal_g)
    terminal_val = terminal_fcf / (discount - terminal_g)
    npv += terminal_val / ((1 + discount) ** 5)

    mc = v.get("market_cap")
    cmp = payload.get("cmp")
    if not mc or not cmp or mc <= 0:
        return None
    shares = mc / cmp
    if shares <= 0:
        return None
    return round(npv / shares, 2)


def _target_pe(payload: dict) -> Optional[float]:
    m = payload.get("metrics") or {}
    ps = m.get("per_share") or {}
    eps = ps.get("eps_ttm")
    if eps is None or eps <= 0:
        return None
    peer = _peer_pe(payload.get("sector"))
    growth = (m.get("growth") or {}).get("eps_yoy_pct")
    if growth is not None:
        # forward EPS estimate (cap growth at 30%)
        eps = eps * (1 + max(min(growth, 30), -20) / 100)
    return round(eps * peer, 2)


def _target_graham(payload: dict) -> Optional[float]:
    ps = (payload.get("metrics") or {}).get("per_share") or {}
    eps = ps.get("eps_ttm")
    bvps = ps.get("book_value")
    if eps is None or bvps is None or eps <= 0 or bvps <= 0:
        return None
    try:
        return round((22.5 * eps * bvps) ** 0.5, 2)
    except Exception:
        return None


def compute_targets(payload: dict) -> dict:
    cmp = payload.get("cmp")
    dcf = _target_dcf(payload)
    pe_t = _target_pe(payload)
    graham = _target_graham(payload)
    candidates = [x for x in (dcf, pe_t, graham) if x and x > 0]
    if not candidates:
        return {
            "dcf": dcf, "pe_based": pe_t, "graham": graham,
            "mean": None, "expected_return_1y_pct": None,
            "band_low": None, "band_high": None,
        }
    mean = round(sum(candidates) / len(candidates), 2)
    band_low = round(min(candidates), 2)
    band_high = round(max(candidates), 2)
    exp_ret = None
    if cmp and cmp > 0:
        exp_ret = round((mean - cmp) / cmp * 100, 2)
    return {
        "dcf": dcf, "pe_based": pe_t, "graham": graham,
        "mean": mean,
        "expected_return_1y_pct": exp_ret,
        "band_low": band_low, "band_high": band_high,
    }


def analyse(payload: dict) -> dict:
    return {
        "score": compute_scores(payload),
        "targets": compute_targets(payload),
        "red_flags": _red_flags(payload),
    }
