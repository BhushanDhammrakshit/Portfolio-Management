"""Fundamentals page + APIs.

Endpoints
---------
GET  /fundamentals
    Renders the page (two tabs: portfolio fundamentals + lookup).

GET  /api/fundamentals/<symbol>
    Cached fundamentals + scorer output for one symbol. No quota — free.

POST /api/fundamentals/portfolio
    Body: {"symbols": [...]}   (optional; defaults to user's holdings)
    Returns a list of scored summaries for the user's portfolio.

POST /api/fundamentals/ai
    Body: {"symbol": "..."}
    AI thesis grounded in fundamentals + RAG. Quota-gated on ``fundamentals_ai``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from flask import Blueprint, jsonify, render_template, request, session

from application.services import cache as shared_cache, plans
from application.services.ai_client import chat as ai_chat, is_configured
from application.services.fundamentals_scorer import analyse
from application.services.providers.fundamentals_provider import get_fundamentals
from application.services.rag import retriever as rag_retriever

log = logging.getLogger(__name__)

fundamentals_api = Blueprint("fundamentals_api", __name__)

# Global cache for the AI thesis output (per Yahoo symbol). Shared across
# users — once one user has paid the LLM cost for a stock, subsequent
# viewers get the same thesis for free (no quota charge) until it goes
# stale. ``refresh=true`` in the request body forces a re-run.
_AI_CACHE_PREFIX = "fundamentals:ai:v2:"
_AI_CACHE_TTL = 24 * 60 * 60  # 24 hours


# ── helpers ────────────────────────────────────────────────────────────

def _yahoo_symbol(raw: str, default_exchange: str = "NSE") -> str:
    """Normalise a user-supplied symbol to Yahoo format (RELIANCE → RELIANCE.NS)."""
    if not raw:
        return ""
    s = raw.strip().upper()
    if "." in s:
        return s
    if ":" in s:  # NSE:RELIANCE-EQ
        exch, rest = s.split(":", 1)
        base = rest.split("-", 1)[0]
        return f"{base}.{'BO' if exch == 'BSE' else 'NS'}"
    suffix = "BO" if default_exchange.upper() == "BSE" else "NS"
    return f"{s}.{suffix}"


def _summarise(payload: dict, scored: dict, holding: Optional[dict] = None) -> dict:
    """Compact dict used by the portfolio table and the lookup card."""
    m = payload.get("metrics") or {}
    val = m.get("valuation") or {}
    prof = m.get("profitability") or {}
    growth = m.get("growth") or {}
    health = m.get("financial_health") or {}
    own = m.get("ownership") or {}
    inst_sent = own.get("institutional_sentiment") or {}
    out = {
        "symbol": payload.get("symbol"),
        "name": payload.get("name"),
        "sector": payload.get("sector"),
        "cmp": payload.get("cmp"),
        "available": payload.get("available"),
        "pe": val.get("pe_trailing"),
        "pb": val.get("pb"),
        "roe": prof.get("roe_pct"),
        "de": health.get("de"),
        "revenue_yoy": growth.get("revenue_yoy_pct"),
        "score": scored["score"]["composite"],
        "verdict": scored["score"]["verdict"],
        "piotroski": scored["score"]["piotroski"],
        "pillars": scored["score"]["pillars"],
        "target_mean": scored["targets"].get("mean"),
        "expected_return_1y_pct": scored["targets"].get("expected_return_1y_pct"),
        "red_flags": scored["red_flags"],
        "inst_holding_pct": own.get("institutional_holding_pct"),
        "inst_flag": inst_sent.get("flag"),
        "inst_signals": inst_sent.get("signals") or [],
    }
    if holding:
        out["holding"] = {
            "quantity": holding.get("Quantity"),
            "purchase_price": holding.get("PurchasePrice"),
            "current_price": holding.get("CurrentPrice"),
        }
    return out


def _parse_ai_json(text: str):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _ensure_fundamentals_quota_key(plan: dict) -> str:
    """Free plans haven't been seeded with a fundamentals key yet — fall back
    to using ``ai_single`` quota so existing users aren't blocked."""
    limits = plan.get("limits", {}) or {}
    if "fundamentals_ai" in limits:
        return "fundamentals_ai"
    return "ai_single"


# ── system prompt for AI thesis ────────────────────────────────────────

FUNDAMENTALS_SYSTEM_PROMPT = (
    "You are a senior equity research analyst specialising in Indian listed "
    "companies. You receive a structured fundamentals snapshot plus optional "
    "recent news. Produce a JSON object (no markdown fences, no prose outside "
    "the JSON) with these exact keys:\n"
    "  thesis           (markdown — 3-4 short paragraphs: Business, "
    "Recent Performance, Why these numbers matter, Verdict)\n"
    "  bull_case        (one paragraph describing the bull scenario)\n"
    "  bear_case        (one paragraph describing the bear scenario)\n"
    "  base_case_target (number — 12-month target price in INR)\n"
    "  bull_case_target (number — INR)\n"
    "  bear_case_target (number — INR)\n"
    "  key_drivers      (array of 3-5 short bullets)\n"
    "  key_risks        (array of 3-5 short bullets)\n"
    "  time_horizon     (Short-term | Medium-term | Long-term)\n"
    "  confidence       (High | Medium | Low)\n"
    "End the thesis with a one-liner that this is informational, not "
    "investment advice."
)


# ── page route ─────────────────────────────────────────────────────────

@fundamentals_api.route("/fundamentals")
def fundamentals_page():
    if "email" not in session:
        from flask import redirect, url_for
        return redirect(url_for("logIn"))
    # Lazy import — route.py owns _fetch_user_stocks but we don't want
    # a circular import.
    from application.routes.route import _fetch_user_stocks
    stocks = _fetch_user_stocks(session["user_id"])
    return render_template(
        "fundamentals.html",
        title="Fundamentals",
        name=session.get("name", ""),
        email=session.get("email", ""),
        stocks=stocks,
    )


# ── per-symbol fundamentals (no quota) ─────────────────────────────────

@fundamentals_api.route("/api/fundamentals/<path:symbol>", methods=["GET"])
def fundamentals_one(symbol: str):
    if "email" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    sym = _yahoo_symbol(symbol)
    if not sym:
        return jsonify({"error": "Missing symbol"}), 400
    force = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    payload = get_fundamentals(sym, force=force)
    scored = analyse(payload)
    return jsonify({
        "fundamentals": payload,
        **scored,
        "summary": _summarise(payload, scored),
    })


# ── portfolio fundamentals (bulk, no quota) ────────────────────────────

@fundamentals_api.route("/api/fundamentals/portfolio", methods=["POST", "GET"])
def fundamentals_portfolio():
    if "email" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    from application.routes.route import _fetch_user_stocks
    holdings = _fetch_user_stocks(session["user_id"])

    body = request.get_json(silent=True) or {}
    sym_filter = {s.upper() for s in (body.get("symbols") or [])}

    results = []
    for h in holdings:
        sym = _yahoo_symbol(h.get("Symbol") or h.get("StockName") or "")
        if not sym:
            continue
        if sym_filter and sym.upper() not in sym_filter:
            continue
        try:
            payload = get_fundamentals(sym)
            scored = analyse(payload)
            results.append(_summarise(payload, scored, holding=h))
        except Exception as e:
            log.warning("[fundamentals] portfolio item %s failed: %s", sym, e)

    # Portfolio aggregate score = weighted by invested value
    total_invested = 0.0
    weighted_score = 0.0
    for r in results:
        h = r.get("holding") or {}
        invested = float(h.get("quantity") or 0) * float(h.get("purchase_price") or 0)
        if invested > 0 and r.get("score") is not None:
            total_invested += invested
            weighted_score += invested * r["score"]
    aggregate = round(weighted_score / total_invested) if total_invested else None

    return jsonify({
        "items": results,
        "aggregate_score": aggregate,
        "count": len(results),
    })


# ── screener (top picks across curated universe) ───────────────────────

_SCREENER_CACHE_PREFIX = "fundamentals:screener:v1:"
_SCREENER_TTL = 6 * 60 * 60  # 6 hours


@fundamentals_api.route("/api/fundamentals/screener", methods=["GET"])
def fundamentals_screener():
    """Scan a curated NSE universe and return stocks scoring at/above ``min_score``.

    Query params
    ------------
    min_score : int (default 80) — composite-score threshold.
    refresh   : "1" forces a re-scan, otherwise a cached scan (≤6h old) is used.
    """
    if "email" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    try:
        min_score = max(0, min(100, int(request.args.get("min_score", 80))))
    except ValueError:
        min_score = 80
    force = request.args.get("refresh") == "1"

    # Lazy import to avoid the heavier volume_api dependency on cold paths
    from application.services import cache as _cache
    from application.routes.volume_api import ALL_SYMBOLS

    cache_key = f"{_SCREENER_CACHE_PREFIX}{min_score}"
    if not force:
        cached = _cache.jget(cache_key)
        if cached and isinstance(cached, dict):
            return jsonify({**cached, "cached": True})

    items: list[dict] = []
    errors = 0
    for sym in ALL_SYMBOLS:
        try:
            payload = get_fundamentals(sym)
            if not payload.get("available"):
                continue
            scored = analyse(payload)
            composite = (scored.get("score") or {}).get("composite")
            if composite is None or composite < min_score:
                continue
            items.append(_summarise(payload, scored))
        except Exception as e:
            errors += 1
            log.warning("[screener] %s failed: %s", sym, e)

    items.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    result = {
        "items": items,
        "count": len(items),
        "universe_size": len(ALL_SYMBOLS),
        "min_score": min_score,
        "errors": errors,
        "cached": False,
    }
    try:
        _cache.jset(cache_key, result, _SCREENER_TTL)
    except Exception:
        pass
    return jsonify(result)


# ── AI thesis (quota-gated) ────────────────────────────────────────────

@fundamentals_api.route("/api/fundamentals/ai", methods=["POST"])
def fundamentals_ai():
    if "email" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol") or ""
    force = bool(data.get("refresh"))
    sym = _yahoo_symbol(symbol)
    if not sym:
        return jsonify({"error": "Missing symbol"}), 400

    # Always recompute fundamentals/score (fundamentals provider has its own
    # 7-day cache); but the expensive LLM call is cached globally so a
    # symbol that has been analysed by anyone in the last 24h is served
    # from cache without burning the requester's quota — including users
    # who have already exhausted their monthly AI quota.
    payload = get_fundamentals(sym)
    scored = analyse(payload)

    ai_cache_key = _AI_CACHE_PREFIX + sym.upper()
    cached_ai = None if force else shared_cache.jget(ai_cache_key)
    if isinstance(cached_ai, dict):
        return jsonify({
            "fundamentals": payload,
            **scored,
            **cached_ai,
            "cached": True,
            "from_cache": True,
        })

    # No cache hit (or refresh requested) — we must actually call the LLM,
    # so now we enforce the per-user quota.
    uid = session.get("user_id", "")
    plan = plans.current_plan()
    quota_key = _ensure_fundamentals_quota_key(plan)
    if not plans.can_use(uid, plan, quota_key):
        limit = plan.get("limits", {}).get(quota_key)
        return jsonify({
            "error": "quota_exceeded",
            "message": f"You've used your monthly limit of {limit} AI analyses. Upgrade for more.",
            "current_plan": plan["id"],
            "upgrade_url": "/billing",
        }), 402

    # Trim history arrays before shipping to the LLM (token-savvy).
    trimmed = {
        "symbol": payload.get("symbol"),
        "name": payload.get("name"),
        "sector": payload.get("sector"),
        "industry": payload.get("industry"),
        "cmp": payload.get("cmp"),
        "currency": payload.get("currency"),
        "metrics": {
            k: v for k, v in (payload.get("metrics") or {}).items()
            if k != "history"
        },
        "score": scored["score"],
        "model_targets": scored["targets"],
        "red_flags": scored["red_flags"],
    }

    if not is_configured():
        plans.increment_usage(uid, quota_key)
        return jsonify({
            "ai_available": False,
            "fundamentals": payload,
            **scored,
            "thesis": (
                f"**Verdict**\n\n{scored['score']['verdict']} "
                f"(composite score {scored['score']['composite']}/100). "
                "AI thesis is currently unavailable; the scorecard above "
                "summarises the rules-based read of the company's fundamentals."
            ),
        })

    # RAG context (best-effort)
    rag_block, rag_sources = "", []
    try:
        rag_query = f"{payload.get('name') or sym} earnings results guidance " \
                    f"{payload.get('sector') or ''}"
        rag_block, rag_sources = rag_retriever.build_context(sym, query=rag_query, top_k=4)
    except Exception as e:
        log.warning("[fundamentals_ai] RAG failed: %s", e)

    prompt = (
        "Fundamentals snapshot (JSON):\n"
        f"{json.dumps(trimmed, default=str)}\n\n"
    )
    if rag_block:
        prompt += rag_block + "\n\n"
    prompt += (
        "Use the snapshot as the primary input. Cross-check the model targets "
        "and adjust your base/bull/bear targets if recent news warrants. "
        "Return the JSON object as specified."
    )

    content, err = ai_chat(
        [
            {"role": "system", "content": FUNDAMENTALS_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3, max_tokens=1100,
    )

    parsed = _parse_ai_json(content) if content else None
    plans.increment_usage(uid, quota_key)

    if not parsed:
        # Don't cache a failed/unparseable LLM response — let the next caller retry.
        return jsonify({
            "ai_available": bool(content),
            "fundamentals": payload,
            **scored,
            "thesis": content or f"_AI unavailable: {err or 'no response'}_",
            "sources_used": rag_sources,
            "cached": False,
            "from_cache": False,
        })

    ai_block = {
        "ai_available": True,
        "thesis": str(parsed.get("thesis") or ""),
        "bull_case": str(parsed.get("bull_case") or ""),
        "bear_case": str(parsed.get("bear_case") or ""),
        "ai_targets": {
            "base": parsed.get("base_case_target"),
            "bull": parsed.get("bull_case_target"),
            "bear": parsed.get("bear_case_target"),
        },
        "key_drivers": parsed.get("key_drivers") or [],
        "key_risks": parsed.get("key_risks") or [],
        "time_horizon": parsed.get("time_horizon"),
        "confidence": parsed.get("confidence"),
        "sources_used": rag_sources,
    }
    try:
        shared_cache.jset(ai_cache_key, ai_block, ttl=_AI_CACHE_TTL)
    except Exception as e:
        log.warning("[fundamentals_ai] cache write failed: %s", e)

    return jsonify({
        "fundamentals": payload,
        **scored,
        **ai_block,
        "cached": False,
        "from_cache": False,
    })
