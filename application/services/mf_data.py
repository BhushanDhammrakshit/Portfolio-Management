"""Mutual fund data service.

Public-data sources (no paid feed required):
  • MFAPI.in        — full Indian MF universe, daily NAV history, scheme meta
                      https://api.mfapi.in/mf, https://api.mfapi.in/mf/<code>
  • AMFI NAVAll.txt — official daily NAV file (used as fallback / latest NAV)

Stock-level holdings are NOT available from these free sources. We ship a
small seed file ``_mf_holdings.json`` containing top holdings for a curated
set of popular funds and expose helpers to extend it (manual / CSV upload).
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Iterable, Optional

import requests

from application.services import cache as shared_cache

# ── Constants ────────────────────────────────────────────────────────────
_MFAPI_LIST = "https://api.mfapi.in/mf"
_MFAPI_SCHEME = "https://api.mfapi.in/mf/{code}"
_AMFI_NAV_ALL = "https://www.amfiindia.com/spages/NAVAll.txt"
_USER_AGENT = "HM2-PortfolioManager/1.0"
_HTTP_TIMEOUT = 12

# Cache TTLs (seconds)
_TTL_SCHEME_LIST = 6 * 3600        # 6h — universe rarely changes
_TTL_SCHEME_DETAIL = 6 * 3600      # 6h — NAV history file
_TTL_LATEST_NAV = 15 * 60          # 15m — for live portfolio valuation

# Local files
_HOLDINGS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "_mf_holdings.json",
)
_HOLDINGS_LOCK = threading.Lock()


# ── HTTP helper ──────────────────────────────────────────────────────────
def _http_json(url: str) -> Optional[Any]:
    try:
        r = requests.get(url, timeout=_HTTP_TIMEOUT,
                         headers={"User-Agent": _USER_AGENT})
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[mf_data] GET {url[:80]} failed: {e}")
    return None


def _http_text(url: str) -> Optional[str]:
    try:
        r = requests.get(url, timeout=_HTTP_TIMEOUT,
                         headers={"User-Agent": _USER_AGENT})
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"[mf_data] GET {url[:80]} failed: {e}")
    return None


# ── Scheme universe / search ─────────────────────────────────────────────
def list_schemes(force: bool = False) -> list[dict]:
    """Return full scheme universe: [{schemeCode, schemeName}, ...]."""
    key = "mf:schemes:all"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return cached
    data = _http_json(_MFAPI_LIST) or []
    if isinstance(data, list) and data:
        shared_cache.jset(key, data, ttl=_TTL_SCHEME_LIST)
    return data


def search_schemes(query: str, limit: int = 25) -> list[dict]:
    """Case-insensitive substring search across scheme names."""
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    for s in list_schemes():
        name = (s.get("schemeName") or "").lower()
        if q in name:
            out.append(s)
            if len(out) >= limit:
                break
    return out


# ── Scheme detail (NAV history + metadata) ───────────────────────────────
def get_scheme(scheme_code: int | str, force: bool = False) -> dict:
    """Fetch scheme metadata + full NAV history.

    Response shape (from MFAPI.in):
        {"meta": {fund_house, scheme_type, scheme_category,
                  scheme_code, scheme_name, isin_growth, isin_div_reinvestment},
         "data": [{date: "DD-MM-YYYY", nav: "123.45"}, ...]}  # newest first
    """
    code = str(scheme_code).strip()
    key = f"mf:scheme:{code}"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return cached
    data = _http_json(_MFAPI_SCHEME.format(code=code)) or {}
    if data.get("meta"):
        shared_cache.jset(key, data, ttl=_TTL_SCHEME_DETAIL)
    return data


def latest_nav(scheme_code: int | str) -> Optional[float]:
    """Latest NAV (cached separately with short TTL)."""
    code = str(scheme_code).strip()
    key = f"mf:nav:{code}"
    cached = shared_cache.jget(key)
    if cached is not None:
        try:
            return float(cached)
        except Exception:
            pass
    scheme = get_scheme(code)
    nav_list = scheme.get("data") or []
    if not nav_list:
        return None
    try:
        nav = float(nav_list[0].get("nav"))
        shared_cache.jset(key, nav, ttl=_TTL_LATEST_NAV)
        return nav
    except Exception:
        return None


# ── Returns / performance ────────────────────────────────────────────────
def _parse_dmy(s: str):
    try:
        return datetime.strptime(s, "%d-%m-%Y").date()
    except Exception:
        return None


def returns_summary(scheme_code: int | str) -> dict:
    """Compute trailing returns (1M / 3M / 6M / 1Y / 3Y / 5Y) + CAGR."""
    scheme = get_scheme(scheme_code)
    series = scheme.get("data") or []
    if not series:
        return {}
    # Newest first → build {date: nav} sorted descending
    parsed = []
    for row in series:
        d = _parse_dmy(row.get("date") or "")
        try:
            nav = float(row.get("nav"))
        except Exception:
            continue
        if d and nav > 0:
            parsed.append((d, nav))
    if not parsed:
        return {}
    parsed.sort(key=lambda x: x[0], reverse=True)
    latest_date, latest = parsed[0]

    def _nav_at(days_back: int) -> Optional[float]:
        target = latest_date.toordinal() - days_back
        # Find the closest entry on/before target (parsed is newest-first)
        for d, nav in parsed:
            if d.toordinal() <= target:
                return nav
        return None

    def _ret(days_back: int, annualize: bool) -> Optional[float]:
        past = _nav_at(days_back)
        if not past or past <= 0:
            return None
        total = (latest / past) - 1.0
        if not annualize:
            return round(total * 100, 2)
        years = days_back / 365.25
        if years <= 0:
            return None
        cagr = (latest / past) ** (1 / years) - 1.0
        return round(cagr * 100, 2)

    return {
        "latest_nav": round(latest, 4),
        "latest_date": latest_date.isoformat(),
        "r_1m": _ret(30, False),
        "r_3m": _ret(91, False),
        "r_6m": _ret(182, False),
        "r_1y": _ret(365, True),
        "r_3y": _ret(365 * 3, True),
        "r_5y": _ret(365 * 5, True),
        "since_inception_years": round(
            (latest_date.toordinal() - parsed[-1][0].toordinal()) / 365.25, 2),
    }


def nav_history(scheme_code: int | str, days: int = 365) -> list[dict]:
    """Compact NAV history for charting: [{date, nav}, ...] oldest-first."""
    scheme = get_scheme(scheme_code)
    series = scheme.get("data") or []
    out = []
    for row in series[:max(1, days)]:
        d = _parse_dmy(row.get("date") or "")
        try:
            nav = float(row.get("nav"))
        except Exception:
            continue
        if d:
            out.append({"date": d.isoformat(), "nav": nav})
    out.reverse()  # oldest first for charts
    return out


# ── Risk / advanced analytics ────────────────────────────────────────────
def _parsed_series(scheme_code) -> list[tuple]:
    """Return [(date, nav), …] newest-first, validated."""
    scheme = get_scheme(scheme_code)
    out = []
    for row in scheme.get("data") or []:
        d = _parse_dmy(row.get("date") or "")
        try:
            nav = float(row.get("nav"))
        except Exception:
            continue
        if d and nav > 0:
            out.append((d, nav))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def scheme_analytics(scheme_code: int | str,
                     risk_free_rate: float = 0.065) -> dict:
    """Compute risk/return analytics from full NAV history.

    Returns volatility, drawdown, Sharpe/Sortino, win ratio, calendar-year
    returns and monthly heat-strip — all computed locally from MFAPI data
    (no extra HTTP).
    """
    parsed = _parsed_series(scheme_code)
    if len(parsed) < 30:
        return {}
    parsed_old_first = list(reversed(parsed))  # ascending
    dates = [d for d, _ in parsed_old_first]
    navs = [n for _, n in parsed_old_first]
    latest_date, latest = parsed[0]

    # Daily log-returns
    import math
    rets = []
    for i in range(1, len(navs)):
        if navs[i-1] > 0 and navs[i] > 0:
            rets.append(math.log(navs[i] / navs[i-1]))
    if not rets:
        return {}

    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    std = math.sqrt(var)
    ann_vol = std * math.sqrt(252) * 100
    ann_ret = (math.exp(mean * 252) - 1) * 100
    sharpe = ((ann_ret / 100) - risk_free_rate) / (ann_vol / 100) if ann_vol else None
    down = [r for r in rets if r < 0]
    dstd = math.sqrt(sum(r * r for r in down) / len(down)) if down else 0
    sortino = ((ann_ret / 100) - risk_free_rate) / (dstd * math.sqrt(252)) if dstd else None

    # Max drawdown (on NAV)
    peak = navs[0]
    max_dd = 0.0
    for n in navs:
        if n > peak:
            peak = n
        dd = (n / peak) - 1.0
        if dd < max_dd:
            max_dd = dd

    # Monthly returns by (year, month) — use last NAV of each month
    monthly: dict[tuple, float] = {}
    last_per_month: dict[tuple, tuple] = {}
    for d, n in parsed_old_first:
        last_per_month[(d.year, d.month)] = (d, n)
    months_sorted = sorted(last_per_month.keys())
    prev = None
    for k in months_sorted:
        _, n = last_per_month[k]
        if prev is not None:
            p = last_per_month[prev][1]
            if p > 0:
                monthly[k] = round((n / p - 1) * 100, 2)
        prev = k
    wins = sum(1 for v in monthly.values() if v > 0)
    win_pct = round(wins / len(monthly) * 100, 1) if monthly else None
    best_month = max(monthly.values()) if monthly else None
    worst_month = min(monthly.values()) if monthly else None

    # Calendar-year returns (last available NAV of year vs last of prev year)
    last_per_year: dict[int, tuple] = {}
    for d, n in parsed_old_first:
        last_per_year[d.year] = (d, n)
    yearly = {}
    years_sorted = sorted(last_per_year.keys())
    for i in range(1, len(years_sorted)):
        py, cy = years_sorted[i - 1], years_sorted[i]
        p = last_per_year[py][1]; c = last_per_year[cy][1]
        if p > 0:
            yearly[cy] = round((c / p - 1) * 100, 2)
    # YTD
    ytd = None
    cur_year_end_prev = last_per_year.get(latest_date.year - 1)
    if cur_year_end_prev and cur_year_end_prev[1] > 0:
        ytd = round((latest / cur_year_end_prev[1] - 1) * 100, 2)

    best_year_val = max(yearly.values()) if yearly else None
    worst_year_val = min(yearly.values()) if yearly else None
    best_year = max(yearly, key=yearly.get) if yearly else None
    worst_year = min(yearly, key=yearly.get) if yearly else None

    # Rolling 12-month returns (median + min)
    rolling_1y = []
    # Build index of date→nav for quick lookup
    nav_by_ord = {d.toordinal(): n for d, n in parsed_old_first}
    ord_sorted = sorted(nav_by_ord.keys())
    for o in ord_sorted:
        past = o - 365
        # find closest <= past
        # binary-ish: linear is fine for ~5k pts
        cand = None
        for x in ord_sorted:
            if x <= past:
                cand = x
            else:
                break
        if cand and nav_by_ord[cand] > 0:
            rolling_1y.append((nav_by_ord[o] / nav_by_ord[cand] - 1) * 100)
    if rolling_1y:
        rolling_1y.sort()
        mid = rolling_1y[len(rolling_1y) // 2]
        rolling_summary = {
            "median": round(mid, 2),
            "min": round(rolling_1y[0], 2),
            "max": round(rolling_1y[-1], 2),
            "neg_pct": round(sum(1 for r in rolling_1y if r < 0) / len(rolling_1y) * 100, 1),
        }
    else:
        rolling_summary = None

    # Simple risk verdict from vol + drawdown
    av = ann_vol or 0
    risk_label = ("Very High" if av >= 25 else "High" if av >= 18
                  else "Moderate" if av >= 12 else "Low to Moderate" if av >= 7
                  else "Low")

    return {
        "annual_volatility_pct": round(ann_vol, 2),
        "annual_return_pct": round(ann_ret, 2),
        "sharpe_ratio": round(sharpe, 2) if sharpe is not None else None,
        "sortino_ratio": round(sortino, 2) if sortino is not None else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "win_ratio_pct": win_pct,
        "best_month_pct": best_month,
        "worst_month_pct": worst_month,
        "best_year": {"year": best_year, "return_pct": best_year_val} if best_year else None,
        "worst_year": {"year": worst_year, "return_pct": worst_year_val} if worst_year else None,
        "ytd_pct": ytd,
        "yearly_returns": [{"year": y, "return_pct": yearly[y]} for y in sorted(yearly)],
        "monthly_heatmap": [{"year": y, "month": m, "return_pct": v}
                            for (y, m), v in sorted(monthly.items())][-60:],
        "rolling_1y": rolling_summary,
        "risk_label": risk_label,
        "data_points": len(navs),
        "history_years": round((latest_date.toordinal() - dates[0].toordinal()) / 365.25, 2),
        "risk_free_rate_pct": round(risk_free_rate * 100, 2),
    }


# ── Holdings store (seed file + user-managed) ────────────────────────────
_SEED_HOLDINGS: dict[str, dict] = {
    # scheme_code: {fund_name, asof, top: [{symbol, weight_pct, sector}]}
    # A small curated seed so the Compare tab works out-of-the-box.
    # Weights are approximate and meant for demo/illustration; users can
    # override via /api/mutual-funds/holdings/upload.
    "120503": {
        "fund_name": "HDFC Top 100 Fund - Regular Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "HDFCBANK", "weight_pct": 9.8, "sector": "Financials"},
            {"symbol": "ICICIBANK", "weight_pct": 8.2, "sector": "Financials"},
            {"symbol": "RELIANCE", "weight_pct": 7.6, "sector": "Energy"},
            {"symbol": "INFY", "weight_pct": 5.4, "sector": "IT"},
            {"symbol": "LT", "weight_pct": 4.1, "sector": "Industrials"},
            {"symbol": "TCS", "weight_pct": 3.6, "sector": "IT"},
            {"symbol": "AXISBANK", "weight_pct": 3.4, "sector": "Financials"},
            {"symbol": "BHARTIARTL", "weight_pct": 3.2, "sector": "Telecom"},
            {"symbol": "SBIN", "weight_pct": 2.9, "sector": "Financials"},
            {"symbol": "ITC", "weight_pct": 2.7, "sector": "FMCG"},
        ],
    },
    "118989": {
        "fund_name": "SBI Bluechip Fund - Regular Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "HDFCBANK", "weight_pct": 9.2, "sector": "Financials"},
            {"symbol": "ICICIBANK", "weight_pct": 7.5, "sector": "Financials"},
            {"symbol": "RELIANCE", "weight_pct": 6.8, "sector": "Energy"},
            {"symbol": "INFY", "weight_pct": 5.1, "sector": "IT"},
            {"symbol": "LT", "weight_pct": 4.6, "sector": "Industrials"},
            {"symbol": "BHARTIARTL", "weight_pct": 4.2, "sector": "Telecom"},
            {"symbol": "TCS", "weight_pct": 3.8, "sector": "IT"},
            {"symbol": "KOTAKBANK", "weight_pct": 3.5, "sector": "Financials"},
            {"symbol": "MARUTI", "weight_pct": 2.8, "sector": "Auto"},
            {"symbol": "HINDUNILVR", "weight_pct": 2.6, "sector": "FMCG"},
        ],
    },
    "119551": {
        "fund_name": "Axis Bluechip Fund - Regular Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "ICICIBANK", "weight_pct": 9.6, "sector": "Financials"},
            {"symbol": "HDFCBANK", "weight_pct": 8.9, "sector": "Financials"},
            {"symbol": "BAJFINANCE", "weight_pct": 5.4, "sector": "Financials"},
            {"symbol": "INFY", "weight_pct": 5.0, "sector": "IT"},
            {"symbol": "TCS", "weight_pct": 4.7, "sector": "IT"},
            {"symbol": "AVENUE_SUPERMARTS", "weight_pct": 4.1, "sector": "Retail"},
            {"symbol": "BHARTIARTL", "weight_pct": 3.9, "sector": "Telecom"},
            {"symbol": "TITAN", "weight_pct": 3.5, "sector": "Consumer"},
            {"symbol": "PIDILITIND", "weight_pct": 3.0, "sector": "Materials"},
            {"symbol": "ASIANPAINT", "weight_pct": 2.7, "sector": "Materials"},
        ],
    },
    "120716": {
        "fund_name": "Mirae Asset Large Cap Fund - Regular Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "HDFCBANK", "weight_pct": 9.7, "sector": "Financials"},
            {"symbol": "ICICIBANK", "weight_pct": 8.4, "sector": "Financials"},
            {"symbol": "RELIANCE", "weight_pct": 7.9, "sector": "Energy"},
            {"symbol": "INFY", "weight_pct": 5.8, "sector": "IT"},
            {"symbol": "TCS", "weight_pct": 4.2, "sector": "IT"},
            {"symbol": "AXISBANK", "weight_pct": 3.6, "sector": "Financials"},
            {"symbol": "LT", "weight_pct": 3.3, "sector": "Industrials"},
            {"symbol": "BHARTIARTL", "weight_pct": 3.1, "sector": "Telecom"},
            {"symbol": "KOTAKBANK", "weight_pct": 2.9, "sector": "Financials"},
            {"symbol": "SBIN", "weight_pct": 2.7, "sector": "Financials"},
        ],
    },
    "120465": {
        "fund_name": "Parag Parikh Flexi Cap Fund - Regular Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "HDFCBANK", "weight_pct": 8.1, "sector": "Financials"},
            {"symbol": "BAJAJ_HOLDINGS", "weight_pct": 6.4, "sector": "Financials"},
            {"symbol": "ITC", "weight_pct": 5.6, "sector": "FMCG"},
            {"symbol": "POWERGRID", "weight_pct": 5.1, "sector": "Utilities"},
            {"symbol": "ICICIBANK", "weight_pct": 4.9, "sector": "Financials"},
            {"symbol": "MARUTI", "weight_pct": 4.7, "sector": "Auto"},
            {"symbol": "COALINDIA", "weight_pct": 3.8, "sector": "Energy"},
            {"symbol": "INFY", "weight_pct": 3.5, "sector": "IT"},
            {"symbol": "HCLTECH", "weight_pct": 2.9, "sector": "IT"},
            {"symbol": "ZYDUSLIFE", "weight_pct": 2.5, "sector": "Pharma"},
        ],
    },
    # ── Direct plans of the same / requested funds ───────────────────────
    "122639": {
        "fund_name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "HDFCBANK", "weight_pct": 8.2, "sector": "Financials"},
            {"symbol": "BAJAJ_HOLDINGS", "weight_pct": 6.5, "sector": "Financials"},
            {"symbol": "ITC", "weight_pct": 5.7, "sector": "FMCG"},
            {"symbol": "POWERGRID", "weight_pct": 5.2, "sector": "Utilities"},
            {"symbol": "ICICIBANK", "weight_pct": 5.0, "sector": "Financials"},
            {"symbol": "MARUTI", "weight_pct": 4.7, "sector": "Auto"},
            {"symbol": "COALINDIA", "weight_pct": 3.8, "sector": "Energy"},
            {"symbol": "INFY", "weight_pct": 3.5, "sector": "IT"},
            {"symbol": "HCLTECH", "weight_pct": 2.9, "sector": "IT"},
            {"symbol": "ZYDUSLIFE", "weight_pct": 2.5, "sector": "Pharma"},
        ],
    },
    "101762": {
        "fund_name": "HDFC Flexi Cap Fund - Growth Plan",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "ICICIBANK", "weight_pct": 9.6, "sector": "Financials"},
            {"symbol": "HDFCBANK", "weight_pct": 9.1, "sector": "Financials"},
            {"symbol": "AXISBANK", "weight_pct": 6.4, "sector": "Financials"},
            {"symbol": "SBIN", "weight_pct": 5.5, "sector": "Financials"},
            {"symbol": "INFY", "weight_pct": 4.8, "sector": "IT"},
            {"symbol": "LT", "weight_pct": 4.2, "sector": "Industrials"},
            {"symbol": "BHARTIARTL", "weight_pct": 3.9, "sector": "Telecom"},
            {"symbol": "MARUTI", "weight_pct": 3.4, "sector": "Auto"},
            {"symbol": "HCLTECH", "weight_pct": 2.8, "sector": "IT"},
            {"symbol": "NTPC", "weight_pct": 2.6, "sector": "Utilities"},
        ],
    },
    "118834": {
        "fund_name": "HDFC Flexi Cap Fund - Direct Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "ICICIBANK", "weight_pct": 9.6, "sector": "Financials"},
            {"symbol": "HDFCBANK", "weight_pct": 9.1, "sector": "Financials"},
            {"symbol": "AXISBANK", "weight_pct": 6.4, "sector": "Financials"},
            {"symbol": "SBIN", "weight_pct": 5.5, "sector": "Financials"},
            {"symbol": "INFY", "weight_pct": 4.8, "sector": "IT"},
            {"symbol": "LT", "weight_pct": 4.2, "sector": "Industrials"},
            {"symbol": "BHARTIARTL", "weight_pct": 3.9, "sector": "Telecom"},
            {"symbol": "MARUTI", "weight_pct": 3.4, "sector": "Auto"},
            {"symbol": "HCLTECH", "weight_pct": 2.8, "sector": "IT"},
            {"symbol": "NTPC", "weight_pct": 2.6, "sector": "Utilities"},
        ],
    },
    "125354": {
        "fund_name": "Axis Bluechip Fund - Direct Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "ICICIBANK", "weight_pct": 9.7, "sector": "Financials"},
            {"symbol": "HDFCBANK", "weight_pct": 9.0, "sector": "Financials"},
            {"symbol": "BAJFINANCE", "weight_pct": 5.5, "sector": "Financials"},
            {"symbol": "INFY", "weight_pct": 5.1, "sector": "IT"},
            {"symbol": "TCS", "weight_pct": 4.8, "sector": "IT"},
            {"symbol": "AVENUE_SUPERMARTS", "weight_pct": 4.1, "sector": "Retail"},
            {"symbol": "BHARTIARTL", "weight_pct": 3.9, "sector": "Telecom"},
            {"symbol": "TITAN", "weight_pct": 3.6, "sector": "Consumer"},
            {"symbol": "PIDILITIND", "weight_pct": 3.0, "sector": "Materials"},
            {"symbol": "ASIANPAINT", "weight_pct": 2.7, "sector": "Materials"},
        ],
    },
    "118825": {
        "fund_name": "ICICI Prudential Bluechip Fund - Direct Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "ICICIBANK", "weight_pct": 9.4, "sector": "Financials"},
            {"symbol": "RELIANCE", "weight_pct": 8.8, "sector": "Energy"},
            {"symbol": "HDFCBANK", "weight_pct": 7.9, "sector": "Financials"},
            {"symbol": "LT", "weight_pct": 5.6, "sector": "Industrials"},
            {"symbol": "INFY", "weight_pct": 4.9, "sector": "IT"},
            {"symbol": "BHARTIARTL", "weight_pct": 4.6, "sector": "Telecom"},
            {"symbol": "AXISBANK", "weight_pct": 4.2, "sector": "Financials"},
            {"symbol": "MARUTI", "weight_pct": 3.5, "sector": "Auto"},
            {"symbol": "ULTRACEMCO", "weight_pct": 3.1, "sector": "Materials"},
            {"symbol": "ITC", "weight_pct": 2.9, "sector": "FMCG"},
        ],
    },
    "120586": {
        "fund_name": "Mirae Asset Large Cap Fund - Direct Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "HDFCBANK", "weight_pct": 9.7, "sector": "Financials"},
            {"symbol": "ICICIBANK", "weight_pct": 8.4, "sector": "Financials"},
            {"symbol": "RELIANCE", "weight_pct": 7.9, "sector": "Energy"},
            {"symbol": "INFY", "weight_pct": 5.8, "sector": "IT"},
            {"symbol": "TCS", "weight_pct": 4.2, "sector": "IT"},
            {"symbol": "AXISBANK", "weight_pct": 3.6, "sector": "Financials"},
            {"symbol": "LT", "weight_pct": 3.3, "sector": "Industrials"},
            {"symbol": "BHARTIARTL", "weight_pct": 3.1, "sector": "Telecom"},
            {"symbol": "KOTAKBANK", "weight_pct": 2.9, "sector": "Financials"},
            {"symbol": "SBIN", "weight_pct": 2.7, "sector": "Financials"},
        ],
    },
    "120505": {
        "fund_name": "Axis Long Term Equity Fund (ELSS) - Direct Plan - Growth",
        "asof": "2026-04-30",
        "top": [
            {"symbol": "ICICIBANK", "weight_pct": 8.9, "sector": "Financials"},
            {"symbol": "HDFCBANK", "weight_pct": 8.2, "sector": "Financials"},
            {"symbol": "BAJFINANCE", "weight_pct": 5.1, "sector": "Financials"},
            {"symbol": "INFY", "weight_pct": 4.8, "sector": "IT"},
            {"symbol": "TCS", "weight_pct": 4.4, "sector": "IT"},
            {"symbol": "TITAN", "weight_pct": 3.7, "sector": "Consumer"},
            {"symbol": "AVENUE_SUPERMARTS", "weight_pct": 3.5, "sector": "Retail"},
            {"symbol": "BHARTIARTL", "weight_pct": 3.2, "sector": "Telecom"},
            {"symbol": "PIDILITIND", "weight_pct": 2.8, "sector": "Materials"},
            {"symbol": "DIVISLAB", "weight_pct": 2.5, "sector": "Pharma"},
        ],
    },
}


def _load_holdings_store() -> dict:
    with _HOLDINGS_LOCK:
        if os.path.exists(_HOLDINGS_FILE):
            try:
                with open(_HOLDINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        # Merge seed for any missing codes
                        for k, v in _SEED_HOLDINGS.items():
                            data.setdefault(k, v)
                        return data
            except Exception:
                pass
        return dict(_SEED_HOLDINGS)


def _save_holdings_store(data: dict) -> None:
    with _HOLDINGS_LOCK:
        try:
            with open(_HOLDINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            print(f"[mf_data] save holdings failed: {e}")


def get_holdings(scheme_code: int | str) -> Optional[dict]:
    """Return top holdings for a scheme, or None if we don't have data."""
    code = str(scheme_code).strip()
    store = _load_holdings_store()
    return store.get(code)


def known_holdings_codes() -> list[str]:
    return sorted(_load_holdings_store().keys())


def set_holdings(scheme_code: int | str, fund_name: str,
                 holdings: list[dict], asof: Optional[str] = None) -> dict:
    """Persist or replace holdings for one scheme."""
    code = str(scheme_code).strip()
    cleaned = []
    for h in holdings:
        sym = (h.get("symbol") or "").strip().upper()
        if not sym:
            continue
        try:
            w = float(h.get("weight_pct") or 0)
        except Exception:
            w = 0.0
        cleaned.append({
            "symbol": sym,
            "weight_pct": round(w, 3),
            "sector": (h.get("sector") or "").strip() or "Other",
        })
    cleaned.sort(key=lambda x: x["weight_pct"], reverse=True)
    entry = {
        "fund_name": fund_name or f"Scheme {code}",
        "asof": asof or datetime.utcnow().date().isoformat(),
        "top": cleaned,
    }
    store = _load_holdings_store()
    store[code] = entry
    _save_holdings_store(store)
    return entry


def parse_holdings_csv(text: str) -> list[dict]:
    """Parse user-uploaded CSV with columns: symbol, weight_pct, [sector]."""
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        for r in reader:
            # Normalise header casing
            low = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in r.items()}
            sym = low.get("symbol") or low.get("ticker") or low.get("scrip")
            wt = low.get("weight_pct") or low.get("weight") or low.get("%")
            sec = low.get("sector") or low.get("industry") or ""
            if not sym:
                continue
            try:
                w = float(str(wt).replace("%", "").strip() or 0)
            except Exception:
                w = 0.0
            rows.append({"symbol": sym, "weight_pct": w, "sector": sec})
    except Exception as e:
        print(f"[mf_data] CSV parse error: {e}")
    return rows


# ── Comparison / overlap ─────────────────────────────────────────────────
def compare_funds(scheme_codes: list[str | int]) -> dict:
    """Compare up to 5 funds: returns + overlap + sector mix.

    Returns shape:
      {
        "funds": [{code, name, category, fund_house, returns, top_n}],
        "overlap_matrix": [[100, 42.5, ...], ...],
        "common_stocks": [{symbol, weights: [w1,w2,...], avg_weight}],
        "sector_mix": [{fund_code, sectors: {Financials: 45.2, ...}}],
        "missing_holdings": [code, ...]
      }
    """
    codes = [str(c).strip() for c in scheme_codes if str(c).strip()][:5]
    funds_out = []
    all_holdings: dict[str, dict[str, float]] = {}  # code → {sym: weight}
    sectors_out = []
    missing = []

    for code in codes:
        scheme = get_scheme(code)
        meta = scheme.get("meta") or {}
        rets = returns_summary(code)
        holdings = get_holdings(code)
        if not holdings:
            missing.append(code)
            sym_w: dict[str, float] = {}
            top = []
        else:
            top = holdings.get("top") or []
            sym_w = {h["symbol"]: float(h.get("weight_pct") or 0)
                     for h in top}
        all_holdings[code] = sym_w

        # Sector mix
        sec_mix: dict[str, float] = {}
        for h in top:
            sec = h.get("sector") or "Other"
            sec_mix[sec] = round(sec_mix.get(sec, 0) + float(h.get("weight_pct") or 0), 2)
        sectors_out.append({"fund_code": code, "sectors": sec_mix})

        funds_out.append({
            "code": code,
            "name": meta.get("scheme_name") or (holdings or {}).get("fund_name") or f"Scheme {code}",
            "category": meta.get("scheme_category", ""),
            "fund_house": meta.get("fund_house", ""),
            "scheme_type": meta.get("scheme_type", ""),
            "returns": rets,
            "top": top[:10],
            "holdings_asof": (holdings or {}).get("asof"),
        })

    # Pairwise overlap = sum of min(weight_i, weight_j) across common symbols
    n = len(codes)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        wi = all_holdings[codes[i]]
        for j in range(n):
            wj = all_holdings[codes[j]]
            if not wi or not wj:
                matrix[i][j] = 0.0
                continue
            if i == j:
                matrix[i][j] = round(sum(wi.values()), 2)
                continue
            common = set(wi.keys()) & set(wj.keys())
            overlap = sum(min(wi[s], wj[s]) for s in common)
            matrix[i][j] = round(overlap, 2)

    # Common stocks across ALL listed funds (intersection)
    common_syms: set[str] = set()
    if all_holdings:
        first = True
        for c in codes:
            syms = set(all_holdings[c].keys())
            if not syms:
                continue
            common_syms = syms if first else (common_syms & syms)
            first = False
    common_stocks = []
    for sym in sorted(common_syms):
        weights = [all_holdings[c].get(sym, 0.0) for c in codes]
        common_stocks.append({
            "symbol": sym,
            "weights": [round(w, 2) for w in weights],
            "avg_weight": round(sum(weights) / max(1, len([w for w in weights if w > 0])), 2),
        })
    common_stocks.sort(key=lambda x: x["avg_weight"], reverse=True)

    # Pairwise jaccard-style similarity score (0–1) for quick "are they the same?" verdict
    similarity_score = None
    if n == 2 and all_holdings[codes[0]] and all_holdings[codes[1]]:
        a = set(all_holdings[codes[0]].keys())
        b = set(all_holdings[codes[1]].keys())
        union = a | b
        jac = len(a & b) / max(1, len(union))
        weighted = matrix[0][1] / 100.0  # weighted overlap %
        similarity_score = round((jac * 0.4 + weighted * 0.6), 3)

    return {
        "funds": funds_out,
        "overlap_matrix": matrix,
        "common_stocks": common_stocks,
        "sector_mix": sectors_out,
        "missing_holdings": missing,
        "similarity_score": similarity_score,
    }
