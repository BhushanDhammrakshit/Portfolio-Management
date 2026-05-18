"""Fundamentals fetcher backed by yfinance.

Pulls valuation, profitability, growth, financial-health and ownership metrics
for a single ticker and caches the result for a week. yfinance can be slow and
flaky for Indian symbols, so every field is wrapped in a defensive helper and
missing data is returned as ``None`` (never raised) so the caller can still
score what it has.

Public API
----------
``get_fundamentals(symbol: str, *, force: bool = False) -> dict``
    Returns the unified fundamentals dict described in the docstring of
    :func:`_empty_payload`. Uses the workspace's ``cache`` module with a
    7-day TTL, keyed by the Yahoo symbol.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from application.services import cache

log = logging.getLogger(__name__)

CACHE_TTL = 7 * 24 * 3600  # 7 days
CACHE_PREFIX = "fundamentals:v2:"


# ── helpers ────────────────────────────────────────────────────────────

def _num(v: Any) -> Optional[float]:
    """Coerce any value to a finite float; otherwise None."""
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _pct(v: Any) -> Optional[float]:
    """Convert a 0..1 ratio to a percent; pass-through if it's already a %."""
    f = _num(v)
    if f is None:
        return None
    # yfinance returns most ratios as decimals (e.g. 0.183 for 18.3%).
    # If the absolute value is unusually large (>3) we assume the source
    # already gave a percent.
    return round(f * 100, 2) if abs(f) <= 3 else round(f, 2)


def _row(df, candidates):
    """Return the first matching row from a yfinance DataFrame as a list of
    floats (newest first). Returns [] if not found / empty."""
    if df is None or df.empty:
        return []
    for c in candidates:
        if c in df.index:
            try:
                vals = [_num(x) for x in df.loc[c].tolist()]
                return [v for v in vals if v is not None]
            except Exception:
                continue
    return []


def _cagr(series, years: int) -> Optional[float]:
    """CAGR from a newest-first series across ``years`` periods."""
    if not series or len(series) <= years:
        return None
    start, end = series[years], series[0]
    if start is None or end is None or start <= 0:
        return None
    try:
        return round(((end / start) ** (1 / years) - 1) * 100, 2)
    except Exception:
        return None


def _yoy_pct(series) -> Optional[float]:
    if not series or len(series) < 2:
        return None
    prev, curr = series[1], series[0]
    if prev is None or curr is None or prev == 0:
        return None
    return round((curr - prev) / abs(prev) * 100, 2)


def _institutional_sentiment(own: dict) -> dict:
    """Compose an FII/DII (institutional) bullish/bearish flag from the
    available ownership signals.

    Direct FII vs DII split is not available via free APIs (it lives in
    quarterly XBRL shareholding patterns on NSE/BSE). This proxy combines:

    * ``institutional_holding_pct``  — current level of institutional ownership
    * ``insider_net_activity_pct``   — net insider buys over the last 6 months
    * ``analyst_rec_mean``           — broker consensus (1=Strong Buy … 5=Sell)
    * ``insider_holding_pct``        — promoter/insider stake ("skin in game")

    Aggregated score (-2..+2) maps to ``bullish`` / ``neutral`` / ``bearish``.
    """
    score = 0.0
    signals: list[str] = []
    level: Optional[str] = None

    inst = own.get("institutional_holding_pct")
    if inst is not None:
        if inst >= 40:
            level = "high"
            score += 1
            signals.append(f"Heavy institutional ownership ({inst:.1f}%)")
        elif inst >= 20:
            level = "moderate"
            signals.append(f"Moderate institutional ownership ({inst:.1f}%)")
        elif inst >= 5:
            level = "low"
            score -= 0.5
            signals.append(f"Light institutional ownership ({inst:.1f}%)")
        else:
            level = "low"
            score -= 1
            signals.append(f"Very low institutional interest ({inst:.1f}%)")

    insider_act = own.get("insider_net_activity_pct")
    if insider_act is not None:
        if insider_act > 0.5:
            score += 1
            signals.append(f"Net insider buying ({insider_act:+.2f}% of float, 6M)")
        elif insider_act > 0:
            score += 0.5
            signals.append(f"Mild insider buying ({insider_act:+.2f}%, 6M)")
        elif insider_act < -0.5:
            score -= 1
            signals.append(f"Net insider selling ({insider_act:+.2f}% of float, 6M)")
        elif insider_act < 0:
            score -= 0.5
            signals.append(f"Mild insider selling ({insider_act:+.2f}%, 6M)")

    rec = own.get("analyst_rec_mean")
    n = own.get("analyst_rec_count")
    if rec is not None and (n is None or n >= 3):
        if rec <= 2.0:
            score += 1
            signals.append(f"Analyst consensus: Buy (mean {rec:.1f}/5)")
        elif rec <= 2.5:
            score += 0.5
            signals.append(f"Analyst consensus: Outperform (mean {rec:.1f}/5)")
        elif rec >= 3.5:
            score -= 1
            signals.append(f"Analyst consensus: Underperform (mean {rec:.1f}/5)")
        else:
            signals.append(f"Analyst consensus: Hold (mean {rec:.1f}/5)")

    insider_hold = own.get("insider_holding_pct")
    if insider_hold is not None and insider_hold >= 40:
        score += 0.5
        signals.append(f"Strong promoter/insider stake ({insider_hold:.1f}%)")

    if not signals:
        flag = "unknown"
    elif score >= 1.5:
        flag = "bullish"
    elif score <= -1.0:
        flag = "bearish"
    else:
        flag = "neutral"

    return {
        "flag": flag,
        "level": level,
        "score": round(score, 2),
        "signals": signals,
    }


def _empty_payload(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "name": None,
        "sector": None,
        "industry": None,
        "exchange": None,
        "cmp": None,
        "currency": "INR",
        "as_of": datetime.now(timezone.utc).date().isoformat(),
        "available": False,
        "metrics": {
            "valuation": {
                "pe_trailing": None, "pe_forward": None, "pb": None, "ps": None,
                "ev_ebitda": None, "peg": None, "div_yield_pct": None,
                "market_cap": None, "enterprise_value": None,
            },
            "profitability": {
                "roe_pct": None, "roa_pct": None, "roce_pct": None,
                "gross_margin_pct": None, "operating_margin_pct": None,
                "net_margin_pct": None, "ebitda_margin_pct": None,
            },
            "growth": {
                "revenue_cagr_3y_pct": None, "eps_cagr_3y_pct": None,
                "revenue_yoy_pct": None, "eps_yoy_pct": None,
                "earnings_qoq_pct": None,
            },
            "financial_health": {
                "de": None, "interest_coverage": None, "current_ratio": None,
                "fcf": None, "fcf_yield_pct": None, "fcf_positive_years": None,
                "total_debt": None,
            },
            "ownership": {
                "promoter_holding_pct": None,
                "insider_holding_pct": None,
                "institutional_holding_pct": None,
                "insider_net_activity_pct": None,
                "analyst_rec_mean": None,
                "analyst_rec_count": None,
                "institutional_sentiment": {
                    "flag": "unknown",       # bullish | neutral | bearish | unknown
                    "level": None,            # high | moderate | low
                    "score": 0,                # -2..+2 aggregate
                    "signals": [],             # list of human-readable strings
                },
            },
            "per_share": {
                "eps_ttm": None, "book_value": None, "dividend_ttm": None,
            },
            "history": {
                "net_income_series": [], "ocf_series": [],
                "revenue_series": [], "eps_series": [],
            },
        },
        "source": "none",
        "errors": [],
    }


# ── core fetch ─────────────────────────────────────────────────────────

def _fetch_yfinance(symbol: str) -> dict:
    """Fetch raw fundamentals from yfinance. Best-effort: any field may be None."""
    import yfinance as yf  # local import — heavy

    payload = _empty_payload(symbol)
    errors = payload["errors"]

    try:
        t = yf.Ticker(symbol)
    except Exception as e:
        errors.append(f"ticker_init: {e}")
        return payload

    info: dict = {}
    try:
        info = dict(t.info or {})
    except Exception as e:
        errors.append(f"info: {e}")

    # ── meta ──
    payload["name"] = info.get("longName") or info.get("shortName")
    payload["sector"] = info.get("sector")
    payload["industry"] = info.get("industry")
    payload["exchange"] = info.get("exchange")
    payload["cmp"] = _num(info.get("currentPrice") or info.get("regularMarketPrice"))
    payload["currency"] = info.get("currency") or "INR"

    val = payload["metrics"]["valuation"]
    val["pe_trailing"] = _num(info.get("trailingPE"))
    val["pe_forward"] = _num(info.get("forwardPE"))
    val["pb"] = _num(info.get("priceToBook"))
    val["ps"] = _num(info.get("priceToSalesTrailing12Months"))
    val["ev_ebitda"] = _num(info.get("enterpriseToEbitda"))
    val["peg"] = _num(info.get("pegRatio") or info.get("trailingPegRatio"))
    val["div_yield_pct"] = _pct(info.get("dividendYield"))
    val["market_cap"] = _num(info.get("marketCap"))
    val["enterprise_value"] = _num(info.get("enterpriseValue"))

    prof = payload["metrics"]["profitability"]
    prof["roe_pct"] = _pct(info.get("returnOnEquity"))
    prof["roa_pct"] = _pct(info.get("returnOnAssets"))
    prof["gross_margin_pct"] = _pct(info.get("grossMargins"))
    prof["operating_margin_pct"] = _pct(info.get("operatingMargins"))
    prof["net_margin_pct"] = _pct(info.get("profitMargins"))
    prof["ebitda_margin_pct"] = _pct(info.get("ebitdaMargins"))

    health = payload["metrics"]["financial_health"]
    health["de"] = _num(info.get("debtToEquity"))
    # yfinance reports D/E as percentage (e.g. 42 = 0.42x). Normalise.
    if health["de"] is not None and health["de"] > 5:
        health["de"] = round(health["de"] / 100, 2)
    health["current_ratio"] = _num(info.get("currentRatio"))
    health["total_debt"] = _num(info.get("totalDebt"))
    fcf = _num(info.get("freeCashflow"))
    health["fcf"] = fcf
    mc = val["market_cap"]
    if fcf is not None and mc and mc > 0:
        health["fcf_yield_pct"] = round(fcf / mc * 100, 2)

    own = payload["metrics"]["ownership"]
    own["insider_holding_pct"] = _pct(info.get("heldPercentInsiders"))
    own["institutional_holding_pct"] = _pct(info.get("heldPercentInstitutions"))
    own["insider_net_activity_pct"] = _pct(info.get("netSharePurchaseActivity"))
    own["analyst_rec_mean"] = _num(info.get("recommendationMean"))
    own["analyst_rec_count"] = _num(info.get("numberOfAnalystOpinions"))
    own["institutional_sentiment"] = _institutional_sentiment(own)

    ps = payload["metrics"]["per_share"]
    ps["eps_ttm"] = _num(info.get("trailingEps"))
    ps["book_value"] = _num(info.get("bookValue"))
    ps["dividend_ttm"] = _num(info.get("dividendRate"))

    # ── historical statements (best-effort) ──
    fin = None
    bs = None
    cf = None
    try:
        fin = t.financials
    except Exception as e:
        errors.append(f"financials: {e}")
    try:
        bs = t.balance_sheet
    except Exception as e:
        errors.append(f"balance_sheet: {e}")
    try:
        cf = t.cashflow
    except Exception as e:
        errors.append(f"cashflow: {e}")

    revenue = _row(fin, ["Total Revenue", "TotalRevenue", "Revenue"])
    net_income = _row(fin, ["Net Income", "NetIncome", "Net Income Common Stockholders"])
    ebit = _row(fin, ["EBIT", "Operating Income", "OperatingIncome"])
    interest = _row(fin, ["Interest Expense", "InterestExpense"])
    ocf = _row(cf, ["Total Cash From Operating Activities",
                    "Operating Cash Flow", "CashFlowFromContinuingOperatingActivities"])

    payload["metrics"]["history"] = {
        "revenue_series": revenue,
        "net_income_series": net_income,
        "ocf_series": ocf,
        "eps_series": [],  # filled below if we can derive
    }

    g = payload["metrics"]["growth"]
    g["revenue_cagr_3y_pct"] = _cagr(revenue, 3)
    g["revenue_yoy_pct"] = _yoy_pct(revenue)
    g["eps_yoy_pct"] = _pct(info.get("earningsGrowth"))
    g["earnings_qoq_pct"] = _pct(info.get("earningsQuarterlyGrowth"))

    # Interest coverage (EBIT / Interest)
    if ebit and interest and interest[0]:
        try:
            ic = ebit[0] / abs(interest[0])
            health["interest_coverage"] = round(ic, 2)
        except Exception:
            pass

    # FCF positive years from cashflow series
    capex = _row(cf, ["Capital Expenditures", "CapitalExpenditures"])
    if ocf and capex and len(capex) == len(ocf):
        try:
            fcfs = [o + c for o, c in zip(ocf, capex)]  # capex is negative in yf
            health["fcf_positive_years"] = sum(1 for v in fcfs if v and v > 0)
        except Exception:
            pass
    elif ocf:
        health["fcf_positive_years"] = sum(1 for v in ocf if v and v > 0)

    # ROCE = EBIT / (Total Assets - Current Liabilities), best-effort
    total_assets = _row(bs, ["Total Assets", "TotalAssets"])
    cur_liab = _row(bs, ["Total Current Liabilities", "CurrentLiabilities"])
    if ebit and total_assets and cur_liab:
        try:
            capital_employed = total_assets[0] - cur_liab[0]
            if capital_employed > 0:
                prof["roce_pct"] = round(ebit[0] / capital_employed * 100, 2)
        except Exception:
            pass

    payload["available"] = any([
        val["pe_trailing"], val["pb"], prof["roe_pct"], net_income, revenue,
    ])
    payload["source"] = "yfinance"
    return payload


# ── public API ─────────────────────────────────────────────────────────

def get_fundamentals(symbol: str, *, force: bool = False) -> dict:
    """Return cached fundamentals for ``symbol`` (Yahoo-format e.g. ``RELIANCE.NS``).

    Cached for 7 days. Pass ``force=True`` to bypass cache.
    """
    if not symbol:
        return _empty_payload("")
    key = CACHE_PREFIX + symbol.upper()
    if not force:
        cached = cache.jget(key)
        if cached:
            return cached
    try:
        data = _fetch_yfinance(symbol)
    except Exception as e:
        log.exception("[fundamentals] fetch failed for %s: %s", symbol, e)
        data = _empty_payload(symbol)
        data["errors"].append(f"unhandled: {e}")
    # Cache even partially-empty results so we don't hammer yfinance.
    try:
        cache.jset(key, data, ttl=CACHE_TTL)
    except Exception as e:
        log.warning("[fundamentals] cache set failed: %s", e)
    return data


def invalidate(symbol: str) -> None:
    try:
        cache.jdelete(CACHE_PREFIX + symbol.upper())
    except Exception:
        pass
