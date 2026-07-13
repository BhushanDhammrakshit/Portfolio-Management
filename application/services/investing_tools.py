"""Investing tool-suite — screeners, DCF, peers, earnings, shareholding,
corporate actions, RAG Q&A, concall sentiment, moat score, portfolio health,
insider tracker, SIP simulator.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from application.services import cache as shared_cache, market_data
from application.services.providers.fundamentals_provider import get_fundamentals
from application.services.fundamentals_scorer import analyse, _piotroski

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


try:
    from application.services.swing_scanner import UNIVERSE as _SWING_UNIVERSE
    SCREENER_UNIVERSE: List[str] = list(_SWING_UNIVERSE)
except Exception:
    SCREENER_UNIVERSE = []


def _safe_float(v, default=0.0):
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
    return symbol.replace(".NS", "").replace(".BO", "")


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


def _flatten_fundamentals(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        p = get_fundamentals(symbol)
    except Exception:
        return None
    if not p or not p.get("available"):
        return None
    m = p.get("metrics", {}) or {}
    val = m.get("valuation", {}) or {}
    prof = m.get("profitability", {}) or {}
    grow = m.get("growth", {}) or {}
    health = m.get("financial_health", {}) or {}
    own = m.get("ownership", {}) or {}
    return {
        "symbol": symbol,
        "name": p.get("name") or _display_name(symbol),
        "sector": p.get("sector"),
        "cmp": p.get("cmp"),
        "pe": val.get("pe_trailing"),
        "pb": val.get("pb"),
        "ev_ebitda": val.get("ev_ebitda"),
        "div_yield": val.get("div_yield_pct"),
        "market_cap": val.get("market_cap"),
        "roe": prof.get("roe_pct"),
        "roce": prof.get("roce_pct"),
        "op_margin": prof.get("operating_margin_pct"),
        "rev_growth_3y": grow.get("revenue_cagr_3y_pct"),
        "eps_growth_3y": grow.get("eps_cagr_3y_pct"),
        "de": health.get("de"),
        "interest_cov": health.get("interest_coverage"),
        "fcf": health.get("fcf"),
        "promoter_pct": own.get("promoter_holding_pct"),
        "_raw": p,
    }


# ─────────────────────────────────────────────────────────────────────────
# 1. Quant screeners
# ─────────────────────────────────────────────────────────────────────────

def _earnings_yield(d):
    pe = d.get("pe")
    if pe and pe > 0:
        return 1.0 / pe * 100.0
    return None


def screener(strategy: str = "magic_formula", force: bool = False) -> Dict[str, Any]:
    """
    Strategies:
      - magic_formula: rank by (ROCE high + earnings yield high)  [Greenblatt]
      - piotroski: F-score >= 7
      - quality: ROE > 15, ROCE > 15, D/E < 0.5
      - value: PE < 20, PB < 3, Div yield > 1%
      - growth: Rev CAGR 3y > 15%, EPS CAGR 3y > 15%
    """
    key = f"invest:screen:{strategy}"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    rows = _parallel(_flatten_fundamentals, SCREENER_UNIVERSE, max_workers=6)

    matches: List[Dict[str, Any]] = []
    if strategy == "magic_formula":
        scored = []
        for r in rows:
            roce = r.get("roce"); pe = r.get("pe")
            if not roce or not pe or pe <= 0:
                continue
            ey = 100.0 / pe
            scored.append((r, roce, ey))
        # Rank: highest ROCE rank + highest earnings-yield rank (lower combined = better)
        n = len(scored)
        roce_rank = {id(t[0]): rk for rk, t in enumerate(sorted(scored, key=lambda x: -x[1]))}
        ey_rank = {id(t[0]): rk for rk, t in enumerate(sorted(scored, key=lambda x: -x[2]))}
        for r, roce, ey in scored:
            combined = roce_rank[id(r)] + ey_rank[id(r)]
            matches.append({**{k: v for k, v in r.items() if k != "_raw"},
                            "earnings_yield_pct": round(ey, 2),
                            "magic_score": int(2 * n - combined)})
        matches.sort(key=lambda r: r["magic_score"], reverse=True)
    elif strategy == "piotroski":
        for r in rows:
            raw = r.get("_raw")
            score = _piotroski(raw) if raw else None
            if score is not None and score >= 7:
                matches.append({**{k: v for k, v in r.items() if k != "_raw"},
                                "piotroski_f": score})
        matches.sort(key=lambda r: r["piotroski_f"], reverse=True)
    elif strategy == "quality":
        for r in rows:
            roe = r.get("roe"); roce = r.get("roce"); de = r.get("de")
            if roe and roce and (de is not None):
                if roe > 15 and roce > 15 and de < 0.5:
                    matches.append({k: v for k, v in r.items() if k != "_raw"})
        matches.sort(key=lambda r: (r.get("roce") or 0), reverse=True)
    elif strategy == "value":
        for r in rows:
            pe = r.get("pe"); pb = r.get("pb"); dy = r.get("div_yield") or 0
            if pe and pb:
                if 0 < pe < 20 and 0 < pb < 3 and dy > 1:
                    matches.append({k: v for k, v in r.items() if k != "_raw"})
        matches.sort(key=lambda r: r.get("pe") or 99)
    elif strategy == "growth":
        for r in rows:
            rg = r.get("rev_growth_3y"); eg = r.get("eps_growth_3y")
            if rg and eg and rg > 15 and eg > 15:
                matches.append({k: v for k, v in r.items() if k != "_raw"})
        matches.sort(key=lambda r: ((r.get("eps_growth_3y") or 0)
                                    + (r.get("rev_growth_3y") or 0)), reverse=True)
    else:
        return {"error": "unknown strategy", "stocks": []}

    payload = {
        "strategy": strategy,
        "stocks": matches[:50],
        "total_screened": len(rows),
        "matches": len(matches),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=60 * 60)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 2. DCF calculator
# ─────────────────────────────────────────────────────────────────────────

def dcf_value(symbol: str, growth_pct: float = 12.0,
              terminal_pct: float = 4.0, discount_pct: float = 11.0,
              years: int = 10) -> Dict[str, Any]:
    # DCF is a pure function of the (daily-stable) fundamentals + the
    # assumption set, so cache the computed valuation in Redis keyed by the
    # symbol and rounded assumptions.
    _key = "invtools:dcf:v1:{}:{}:{}:{}:{}".format(
        symbol.upper(), round(float(growth_pct), 2), round(float(terminal_pct), 2),
        round(float(discount_pct), 2), int(years))
    try:
        cached = shared_cache.jget(_key)
        if isinstance(cached, dict):
            return cached
    except Exception:
        pass
    out = _dcf_value_compute(symbol, growth_pct, terminal_pct, discount_pct, years)
    # Only cache successful valuations — errors are often transient (missing
    # fundamentals right after a token expiry), so don't pin them.
    if isinstance(out, dict) and not out.get("error"):
        try:
            shared_cache.jset(_key, out, ttl=60 * 60)  # 1 hour
        except Exception:
            pass
    return out


def _dcf_value_compute(symbol: str, growth_pct: float = 12.0,
                       terminal_pct: float = 4.0, discount_pct: float = 11.0,
                       years: int = 10) -> Dict[str, Any]:
    p = _flatten_fundamentals(symbol)
    if not p:
        return {"error": "fundamentals unavailable", "symbol": symbol}

    raw = p["_raw"]
    metrics = raw.get("metrics", {}) or {}
    health = metrics.get("financial_health", {}) or {}
    fcf = _safe_float(health.get("fcf"))
    mcap = _safe_float(metrics.get("valuation", {}).get("market_cap"))
    cmp = _safe_float(p.get("cmp"))

    if fcf <= 0:
        return {"error": "FCF is zero or negative — DCF not applicable",
                "symbol": symbol, "cmp": cmp}

    g = growth_pct / 100.0
    tg = terminal_pct / 100.0
    d = discount_pct / 100.0
    if d <= tg:
        return {"error": "Discount rate must exceed terminal growth", "symbol": symbol}

    # Project FCFs
    proj = []
    fcf_yr = fcf
    pv_sum = 0.0
    for t in range(1, years + 1):
        fcf_yr = fcf_yr * (1 + g)
        pv = fcf_yr / ((1 + d) ** t)
        pv_sum += pv
        proj.append({"year": t, "fcf": round(fcf_yr, 0), "pv": round(pv, 0)})

    # Terminal value (Gordon growth)
    terminal_fcf = fcf_yr * (1 + tg)
    tv = terminal_fcf / (d - tg)
    pv_tv = tv / ((1 + d) ** years)

    intrinsic_ev = pv_sum + pv_tv
    upside_pct = ((intrinsic_ev - mcap) / mcap * 100.0) if mcap > 0 else None
    fair_price = (cmp * intrinsic_ev / mcap) if (mcap > 0 and cmp > 0) else None

    return {
        "symbol": symbol,
        "name": p["name"],
        "cmp": cmp,
        "market_cap": mcap,
        "current_fcf": round(fcf, 0),
        "projections": proj,
        "pv_explicit": round(pv_sum, 0),
        "pv_terminal": round(pv_tv, 0),
        "intrinsic_ev": round(intrinsic_ev, 0),
        "fair_price": round(fair_price, 2) if fair_price is not None else None,
        "upside_pct": round(upside_pct, 1) if upside_pct is not None else None,
        "assumptions": {
            "growth_pct": growth_pct, "terminal_pct": terminal_pct,
            "discount_pct": discount_pct, "years": years,
        },
    }


# ─────────────────────────────────────────────────────────────────────────
# 3. Peer comparison
# ─────────────────────────────────────────────────────────────────────────

# Quick sector → peer map (uses heatmap sectors)
try:
    from application.routes.heatmap import SECTOR_STOCKS as _SECTOR_MAP
except Exception:
    _SECTOR_MAP = {}


def _peers_for(symbol: str) -> List[str]:
    for stocks in _SECTOR_MAP.values():
        if symbol in stocks:
            return [s for s in stocks if s != symbol]
    return []


def peer_comparison(symbol: str) -> Dict[str, Any]:
    target = _flatten_fundamentals(symbol)
    if not target:
        return {"error": "fundamentals unavailable", "symbol": symbol}

    peers = _peers_for(symbol)
    if not peers:
        # Fallback: use stocks in same sector from screener universe
        sector = target.get("sector")
        if sector:
            peer_rows = _parallel(_flatten_fundamentals, SCREENER_UNIVERSE[:60], max_workers=6)
            peers_data = [r for r in peer_rows if r.get("sector") == sector and r["symbol"] != symbol]
        else:
            peers_data = []
    else:
        peers_data = _parallel(_flatten_fundamentals, peers, max_workers=6)

    cols = ["pe", "pb", "ev_ebitda", "roe", "roce", "op_margin",
            "rev_growth_3y", "eps_growth_3y", "de", "div_yield"]

    def _slim(r):
        return {k: r.get(k) for k in ["symbol", "name", "cmp", "market_cap"] + cols}

    # Compute averages
    avgs = {}
    for c in cols:
        vals = [r.get(c) for r in peers_data if isinstance(r.get(c), (int, float))]
        avgs[c] = round(sum(vals) / len(vals), 2) if vals else None

    return {
        "target": _slim(target),
        "peers": [_slim(r) for r in peers_data][:10],
        "averages": avgs,
        "sector": target.get("sector"),
    }


# ─────────────────────────────────────────────────────────────────────────
# 4. Earnings calendar
# ─────────────────────────────────────────────────────────────────────────

def earnings_calendar(force: bool = False) -> Dict[str, Any]:
    """Upcoming earnings using yfinance Ticker.calendar where available.

    For each large-cap, look at next earnings date if within next 30 days.
    """
    key = "invest:earnings_cal:v1"
    if not force:
        cached = shared_cache.jget(key)
        if cached:
            return {**cached, "cached": True}

    def _earnings_for(symbol):
        try:
            import yfinance as yf
            t = yf.Ticker(symbol)
            cal = t.calendar
            if cal is None:
                return None
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date") or cal.get("earnings_date")
                if isinstance(ed, list) and ed:
                    ed = ed[0]
            else:
                # DataFrame
                try:
                    ed = cal.loc["Earnings Date"].iloc[0]
                except Exception:
                    return None
            if not ed:
                return None
            try:
                d = ed.date() if hasattr(ed, "date") else _dt.date.fromisoformat(str(ed)[:10])
            except Exception:
                return None
            today = _now_ist().date()
            delta = (d - today).days
            if delta < -2 or delta > 60:
                return None
            quote = market_data.get_quote(symbol) or {}
            return {
                "symbol": symbol,
                "name": _display_name(symbol),
                "earnings_date": d.isoformat(),
                "days_until": delta,
                "price": _safe_float(quote.get("ltp")),
                "change_pct": _safe_float(quote.get("change_pct")),
            }
        except Exception:
            return None

    results = _parallel(_earnings_for, SCREENER_UNIVERSE, max_workers=6)
    results.sort(key=lambda r: r["days_until"])

    payload = {
        "events": results,
        "this_week": sum(1 for r in results if 0 <= r["days_until"] <= 7),
        "next_30d": sum(1 for r in results if r["days_until"] <= 30),
        "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(key, payload, ttl=6 * 60 * 60)
    except Exception:
        pass
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 5. Shareholding pattern (latest available via yfinance)
# ─────────────────────────────────────────────────────────────────────────

def shareholding_pattern(symbol: str) -> Dict[str, Any]:
    p = _flatten_fundamentals(symbol)
    if not p:
        return {"error": "data unavailable", "symbol": symbol}
    raw = p["_raw"]
    own = (raw.get("metrics", {}) or {}).get("ownership", {}) or {}
    return {
        "symbol": symbol,
        "name": p["name"],
        "promoter_pct": own.get("promoter_holding_pct"),
        "institutional_pct": own.get("institutional_holding_pct"),
        "insider_pct": own.get("insider_holding_pct"),
        "insider_net_activity_pct": own.get("insider_net_activity_pct"),
        "institutional_sentiment": own.get("institutional_sentiment") or {},
        "as_of": raw.get("as_of"),
    }


# ─────────────────────────────────────────────────────────────────────────
# 6. Corporate actions feed (yfinance dividends/splits)
# ─────────────────────────────────────────────────────────────────────────

def corporate_actions(symbols: List[str]) -> Dict[str, Any]:
    """Pull dividends and splits for given symbols (last 2 years)."""
    out: List[Dict[str, Any]] = []
    cutoff = _now_ist().date() - _dt.timedelta(days=730)

    def _ca(symbol):
        try:
            import yfinance as yf
            t = yf.Ticker(symbol)
            div = t.dividends
            spl = t.splits
            events = []
            if div is not None and len(div) > 0:
                for ts, v in div.items():
                    d = ts.date() if hasattr(ts, "date") else None
                    if d and d >= cutoff:
                        events.append({"date": d.isoformat(), "type": "Dividend",
                                       "value": round(float(v), 2)})
            if spl is not None and len(spl) > 0:
                for ts, v in spl.items():
                    d = ts.date() if hasattr(ts, "date") else None
                    if d and d >= cutoff:
                        events.append({"date": d.isoformat(), "type": "Split",
                                       "value": float(v)})
            if not events:
                return None
            events.sort(key=lambda e: e["date"], reverse=True)
            return {"symbol": symbol, "name": _display_name(symbol), "events": events}
        except Exception:
            return None

    results = _parallel(_ca, symbols, max_workers=6)
    return {"stocks": results,
            "scan_time": _now_ist().strftime("%d %b %Y, %I:%M %p IST")}


# ─────────────────────────────────────────────────────────────────────────
# 7. Annual report / RAG Q&A
# ─────────────────────────────────────────────────────────────────────────

def annual_report_qa(symbol: str, question: str, k: int = 5) -> Dict[str, Any]:
    try:
        from application.services.rag import retriever as rag_retriever
        from application.services.ai_client import chat as ai_chat, is_configured
    except Exception as e:
        return {"error": f"RAG unavailable: {e}"}

    try:
        contexts = rag_retriever.retrieve(symbol, question, k=k) or []
    except Exception as e:
        return {"error": f"retrieve failed: {e}"}

    if not contexts:
        return {"symbol": symbol, "question": question,
                "answer": "No relevant context found in the RAG store for this stock yet. "
                          "Filings may not have been ingested.",
                "contexts": [], "model_used": False}

    if not is_configured():
        # Return raw context only
        return {"symbol": symbol, "question": question,
                "answer": "AI is not configured; returning relevant context excerpts.",
                "contexts": contexts, "model_used": False}

    context_text = "\n\n---\n\n".join(
        [f"[{i+1}] {c.get('text','')[:1200]}" for i, c in enumerate(contexts)]
    )
    prompt = (
        f"You are a senior equity analyst. Answer the question about {symbol} "
        f"using ONLY the context excerpts below. Cite the excerpt numbers. "
        f"If the context does not contain the answer, say so honestly.\n\n"
        f"Question: {question}\n\nContext:\n{context_text}"
    )
    try:
        answer = ai_chat(prompt, max_tokens=600)
    except Exception as e:
        return {"error": f"AI call failed: {e}", "contexts": contexts}
    return {"symbol": symbol, "question": question, "answer": answer,
            "contexts": contexts, "model_used": True}


# ─────────────────────────────────────────────────────────────────────────
# 8. Concall sentiment (simplified — uses RAG concall docs if available)
# ─────────────────────────────────────────────────────────────────────────

def concall_sentiment(symbol: str) -> Dict[str, Any]:
    try:
        from application.services.rag import retriever as rag_retriever
    except Exception as e:
        return {"error": f"RAG unavailable: {e}"}

    queries = [
        "management outlook guidance growth",
        "capex expansion plan",
        "demand environment headwinds challenges",
        "margins pricing competitive",
    ]
    notes = []
    pos_kw = ("strong", "growth", "robust", "expansion", "record", "improved",
              "favorable", "momentum", "guidance raised", "ahead of plan")
    neg_kw = ("weak", "decline", "headwind", "challenging", "pressure", "slowdown",
              "delay", "loss", "decrease", "guidance lowered", "miss")

    score = 0
    samples = []
    for q in queries:
        try:
            ctxs = rag_retriever.retrieve(symbol, q, k=2) or []
        except Exception:
            continue
        for c in ctxs:
            text = (c.get("text") or "").lower()
            p = sum(1 for k in pos_kw if k in text)
            n = sum(1 for k in neg_kw if k in text)
            score += p - n
            if p or n:
                samples.append({"query": q, "snippet": (c.get("text") or "")[:300],
                                "pos": p, "neg": n})

    if score >= 5:
        verdict = "bullish"
    elif score >= 2:
        verdict = "mildly bullish"
    elif score <= -5:
        verdict = "bearish"
    elif score <= -2:
        verdict = "mildly bearish"
    else:
        verdict = "neutral / mixed"

    return {
        "symbol": symbol,
        "score": score,
        "verdict": verdict,
        "samples": samples[:6],
        "note": "Heuristic keyword sentiment over recent filings / news. "
                "Returns 'neutral' if insufficient data in RAG store.",
    }


# ─────────────────────────────────────────────────────────────────────────
# 9. Moat / Quality composite
# ─────────────────────────────────────────────────────────────────────────

def moat_score(symbol: str) -> Dict[str, Any]:
    p = _flatten_fundamentals(symbol)
    if not p:
        return {"error": "fundamentals unavailable", "symbol": symbol}
    raw = p["_raw"]
    metrics = raw.get("metrics", {}) or {}
    hist = metrics.get("history", {}) or {}

    roe = p.get("roe") or 0
    roce = p.get("roce") or 0
    op_margin = p.get("op_margin") or 0
    de = p.get("de"); de = de if de is not None else 1.0
    fcf = p.get("fcf") or 0

    # ROCE consistency: stdev / mean of ROCE if history available; otherwise use level
    # Use net income series sign consistency as a proxy
    ni = hist.get("net_income_series") or []
    fcf_positive_years = (metrics.get("financial_health", {}) or {}).get("fcf_positive_years") or 0

    pillars = {
        "profitability": min(100, int((roe / 25 + roce / 25) * 50)),
        "margin_quality": min(100, int(op_margin * 3)),
        "leverage_safety": int(max(0, 100 - de * 50)),
        "cash_generation": min(100, int(fcf_positive_years * 12)) if fcf_positive_years
                            else (60 if fcf > 0 else 20),
        "earnings_consistency": min(100, int(sum(1 for x in ni if x and x > 0)
                                             / max(1, len(ni)) * 100)) if ni else 50,
    }
    composite = round(sum(pillars.values()) / len(pillars))

    if composite >= 75:
        verdict = "Wide moat"
    elif composite >= 60:
        verdict = "Narrow moat"
    elif composite >= 45:
        verdict = "Some advantages"
    else:
        verdict = "No discernible moat"

    return {
        "symbol": symbol,
        "name": p["name"],
        "composite": composite,
        "verdict": verdict,
        "pillars": pillars,
        "metrics_snapshot": {
            "roe": roe, "roce": roce, "op_margin": op_margin, "de": de,
            "fcf_positive_years": fcf_positive_years,
        },
    }


def scan_moat_scores(limit: int = 10, force: bool = False) -> Dict[str, Any]:
    """Scan the full universe, compute moat/quality scores and return the
    top `limit` stocks ranked by composite score."""
    limit = max(5, min(int(limit or 10), 50))
    key = "invest:moat:scan:v1"
    payload = None if force else shared_cache.jget(key)
    if not payload:
        rows = _parallel(moat_score, SCREENER_UNIVERSE, max_workers=6)
        scored = [r for r in rows if r and not r.get("error")
                  and isinstance(r.get("composite"), (int, float))]
        scored.sort(key=lambda r: r.get("composite", 0), reverse=True)
        payload = {
            "stocks": scored,
            "total_scanned": len(scored),
            "as_of": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
        }
        shared_cache.jset(key, payload, ttl=60 * 60)
    top = payload.get("stocks", [])[:limit]
    return {
        "stocks": top,
        "limit": limit,
        "total_scanned": payload.get("total_scanned", len(top)),
        "as_of": payload.get("as_of"),
    }


# ─────────────────────────────────────────────────────────────────────────
# 10. Portfolio health check
# ─────────────────────────────────────────────────────────────────────────

def portfolio_health(holdings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """holdings: [{symbol, quantity, purchase_price, current_price?}, ...]
    Returns sector concentration, valuation/quality scoring, recommendations.
    """
    if not holdings:
        return {"error": "No holdings provided"}

    enriched = []
    sector_value: Dict[str, float] = {}
    total_value = 0.0
    total_invested = 0.0

    for h in holdings:
        symbol = h.get("symbol") or h.get("Symbol")
        qty = _safe_float(h.get("quantity") or h.get("Quantity"))
        purchase = _safe_float(h.get("purchase_price") or h.get("PurchasePrice"))
        cur = _safe_float(h.get("current_price") or h.get("CurrentPrice"))
        if not symbol or qty <= 0:
            continue
        if cur <= 0:
            q = market_data.get_quote(symbol) or {}
            cur = _safe_float(q.get("ltp"))
        f = _flatten_fundamentals(symbol) or {}
        sector = f.get("sector") or "Other"
        value = qty * cur
        invested = qty * purchase
        sector_value[sector] = sector_value.get(sector, 0) + value
        total_value += value
        total_invested += invested
        scored = None
        try:
            scored = analyse(f.get("_raw") or {}) if f.get("_raw") else None
        except Exception:
            scored = None
        enriched.append({
            "symbol": symbol,
            "name": f.get("name") or _display_name(symbol),
            "sector": sector,
            "qty": qty,
            "purchase": purchase,
            "cmp": cur,
            "value": round(value, 0),
            "pnl": round(value - invested, 0),
            "pnl_pct": round((value - invested) / invested * 100.0, 2) if invested > 0 else 0,
            "pe": f.get("pe"),
            "roe": f.get("roe"),
            "score": (scored or {}).get("score", {}).get("composite") if scored else None,
            "verdict": (scored or {}).get("score", {}).get("verdict") if scored else None,
        })

    # Sector concentration
    sectors = [{"sector": s, "value": round(v, 0),
                "pct": round(v / total_value * 100.0, 2) if total_value else 0}
               for s, v in sector_value.items()]
    sectors.sort(key=lambda r: r["value"], reverse=True)

    # Concentration risk
    concentration = sectors[0]["pct"] if sectors else 0
    flags: List[str] = []
    if concentration > 40:
        flags.append(f"High sector concentration: {sectors[0]['sector']} = {concentration:.0f}%")
    if total_value > 0 and len(enriched) < 5:
        flags.append("Portfolio under-diversified (less than 5 positions)")
    # Weak holdings
    weak = [r for r in enriched if r.get("score") and r["score"] < 40]
    if weak:
        flags.append(f"{len(weak)} holding(s) with poor fundamentals score")

    avg_pe = ([r["pe"] for r in enriched if r["pe"]] or [None])
    weighted_pe = None
    if avg_pe[0] is not None:
        wsum = sum((r["pe"] or 0) * r["value"] for r in enriched if r["pe"])
        vsum = sum(r["value"] for r in enriched if r["pe"])
        weighted_pe = round(wsum / vsum, 1) if vsum else None

    return {
        "total_value": round(total_value, 0),
        "total_invested": round(total_invested, 0),
        "total_pnl": round(total_value - total_invested, 0),
        "total_pnl_pct": round((total_value - total_invested) / total_invested * 100.0, 2)
                         if total_invested > 0 else 0,
        "holdings": enriched,
        "sector_breakdown": sectors,
        "weighted_pe": weighted_pe,
        "flags": flags,
        "as_of": _now_ist().strftime("%d %b %Y, %I:%M %p IST"),
    }


# ─────────────────────────────────────────────────────────────────────────
# 11. Insider transactions (best-effort via yfinance)
# ─────────────────────────────────────────────────────────────────────────

def insider_transactions(symbol: str) -> Dict[str, Any]:
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        # yfinance exposes some insider data for US tickers; for NSE it's
        # usually empty. We surface what we have and a helpful note.
        rows = []
        for attr in ("insider_transactions", "insider_purchases", "insider_roster_holders"):
            df = getattr(t, attr, None)
            if df is None or len(df) == 0:
                continue
            for _, r in df.head(20).iterrows():
                rows.append({k: (str(r[k]) if r[k] is not None else None) for k in r.index})
        return {"symbol": symbol, "rows": rows[:30],
                "note": "NSE insider data is patchy via free APIs. For full SEBI filings, "
                        "subscribe to a paid feed or scrape NSE corporate-announcements."}
    except Exception as e:
        return {"symbol": symbol, "rows": [], "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────
# 12. SIP / wealth simulator
# ─────────────────────────────────────────────────────────────────────────

def sip_simulator(monthly_amount: float = 10000.0,
                  expected_return_pct: float = 12.0,
                  years: int = 10,
                  step_up_pct: float = 0.0) -> Dict[str, Any]:
    if monthly_amount <= 0 or years <= 0:
        return {"error": "Invalid parameters"}
    r_monthly = (1 + expected_return_pct / 100.0) ** (1 / 12) - 1
    yearly: List[Dict[str, Any]] = []
    corpus = 0.0
    invested = 0.0
    contribution = monthly_amount
    for y in range(1, years + 1):
        for _ in range(12):
            corpus = corpus * (1 + r_monthly) + contribution
            invested += contribution
        yearly.append({
            "year": y,
            "monthly_contribution": round(contribution, 0),
            "invested_so_far": round(invested, 0),
            "corpus": round(corpus, 0),
            "gain": round(corpus - invested, 0),
        })
        contribution *= (1 + step_up_pct / 100.0)
    return {
        "monthly_amount": monthly_amount,
        "expected_return_pct": expected_return_pct,
        "step_up_pct": step_up_pct,
        "years": years,
        "final_corpus": round(corpus, 0),
        "total_invested": round(invested, 0),
        "total_gain": round(corpus - invested, 0),
        "yearly": yearly,
    }
