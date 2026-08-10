"""Per-stock AI analysis endpoint — returns structured KPIs + summary."""
import json
import logging
import re

from flask import Blueprint, request, jsonify, session

from application.services.ai_client import chat as ai_chat, is_configured
from application.services.rag import retriever as rag_retriever
from application.services.event_tracker import track_event

log = logging.getLogger(__name__)

stock_analysis_api = Blueprint("stock_analysis_api", __name__)

STOCK_SYSTEM_PROMPT = (
    "You are a senior equity analyst covering Indian markets. For a given "
    "stock holding, output a JSON object (no prose, no markdown fences) with "
    "the following exact keys:\n"
    "  action          (one of: STRONG BUY, BUY, ACCUMULATE, HOLD, REDUCE, BOOK PROFIT, SELL, STRONG SELL)\n"
    "  confidence      (High | Medium | Low)\n"
    "  horizon         (Short-term | Medium-term | Long-term)\n"
    "  risk            (Low | Medium | High)\n"
    "  target_price    (number in INR — 12-month target)\n"
    "  stop_loss       (number in INR)\n"
    "  upside_pct      (number, percent change from current to target)\n"
    "  key_drivers     (array of 3-5 short bullet strings)\n"
    "  key_risks       (array of 3-5 short bullet strings)\n"
    "  what_to_do      (one short imperative sentence telling the user the next action)\n"
    "  summary         (markdown string with sections: **Business**, **Recent Performance**, **Sector & Risks**, **Verdict**)\n"
    "Return ONLY the JSON object. End the summary by mentioning that this is informational, not investment advice."
)


def _compute_holding_metrics(stock: dict):
    qty = float(stock.get("Quantity") or 0)
    pp = float(stock.get("PurchasePrice") or 0)
    cp = float(stock.get("CurrentPrice") or 0) or pp
    invested = qty * pp
    current = qty * cp
    pl = current - invested
    pl_pct = (pl / invested * 100) if invested else 0
    return qty, pp, cp, invested, current, pl, pl_pct


def _rule_based_kpis(stock: dict) -> dict:
    """Deterministic fallback when AI is unavailable. Bases action on P/L%."""
    qty, pp, cp, invested, current, pl, pl_pct = _compute_holding_metrics(stock)
    name = stock.get("StockName", "Stock")
    sector = stock.get("Sector", "N/A")

    if pl_pct >= 30:
        action, what = "BOOK PROFIT", f"Consider booking partial profits — you are up {pl_pct:.1f}%."
        target, stop = round(cp * 1.08, 2), round(cp * 0.92, 2)
        risk, conf, horizon = "Medium", "Medium", "Short-term"
    elif pl_pct >= 12:
        action, what = "HOLD", f"Holding strong — +{pl_pct:.1f}%. Trail your stop-loss to protect gains."
        target, stop = round(cp * 1.12, 2), round(pp * 1.02, 2)
        risk, conf, horizon = "Low", "Medium", "Medium-term"
    elif pl_pct >= -5:
        action, what = "HOLD", "Position is roughly at cost. Wait for a clear breakout above resistance."
        target, stop = round(cp * 1.10, 2), round(cp * 0.93, 2)
        risk, conf, horizon = "Medium", "Low", "Medium-term"
    elif pl_pct >= -15:
        action, what = "ACCUMULATE", f"Stock is down {abs(pl_pct):.1f}%. Consider averaging only if fundamentals are intact."
        target, stop = round(pp * 1.05, 2), round(cp * 0.90, 2)
        risk, conf, horizon = "Medium", "Low", "Medium-term"
    else:
        action, what = "REDUCE", f"Heavy loss of {abs(pl_pct):.1f}%. Cut exposure if breakdown continues below stop-loss."
        target, stop = round(cp * 1.05, 2), round(cp * 0.92, 2)
        risk, conf, horizon = "High", "Low", "Short-term"

    upside = ((target - cp) / cp * 100) if cp else 0
    summary = (
        f"**Business**\n\n{name} operates in the {sector} sector. AI analysis is currently unavailable; "
        "this snapshot is generated from your holding metrics only.\n\n"
        f"**Recent Performance**\n\nYou are at {('+' if pl_pct >= 0 else '')}{pl_pct:.2f}% "
        f"({('+' if pl >= 0 else '')}₹{pl:,.0f}). Current price ₹{cp:,.2f} vs buy ₹{pp:,.2f}.\n\n"
        f"**Sector & Risks**\n\nReview {sector} sector trends and any company-specific news "
        "before acting on this rule-based suggestion.\n\n"
        f"**Verdict**\n\n{what}\n\n_This is informational, not investment advice._"
    )

    return {
        "action": action,
        "confidence": conf,
        "horizon": horizon,
        "risk": risk,
        "target_price": target,
        "stop_loss": stop,
        "upside_pct": round(upside, 2),
        "key_drivers": [
            f"Current P/L: {('+' if pl_pct >= 0 else '')}{pl_pct:.2f}%",
            f"Position size: ₹{current:,.0f}",
            f"Sector: {sector}",
        ],
        "key_risks": [
            "Market-wide volatility",
            "Sector rotation",
            "Stock-specific news flow",
        ],
        "what_to_do": what,
        "summary": summary,
        "source": "rule-based",
    }


def _parse_ai_json(text: str):
    """Best-effort extraction of a JSON object from an AI response."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _normalize(payload: dict, stock: dict) -> dict:
    """Ensure the AI payload has all expected fields with sensible defaults."""
    qty, pp, cp, invested, current, pl, pl_pct = _compute_holding_metrics(stock)
    fallback = _rule_based_kpis(stock)

    def _coerce_num(v, default):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return default

    action = str(payload.get("action") or fallback["action"]).upper().strip()
    target = _coerce_num(payload.get("target_price"), fallback["target_price"])
    stop = _coerce_num(payload.get("stop_loss"), fallback["stop_loss"])
    upside = payload.get("upside_pct")
    if upside is None and cp:
        upside = (target - cp) / cp * 100
    upside = _coerce_num(upside, fallback["upside_pct"])

    drivers = payload.get("key_drivers") or []
    if not isinstance(drivers, list):
        drivers = [str(drivers)]
    risks = payload.get("key_risks") or []
    if not isinstance(risks, list):
        risks = [str(risks)]

    return {
        "action": action,
        "confidence": str(payload.get("confidence") or fallback["confidence"]),
        "horizon": str(payload.get("horizon") or fallback["horizon"]),
        "risk": str(payload.get("risk") or fallback["risk"]),
        "target_price": target,
        "stop_loss": stop,
        "upside_pct": upside,
        "key_drivers": [str(d).strip() for d in drivers if str(d).strip()][:6],
        "key_risks": [str(r).strip() for r in risks if str(r).strip()][:6],
        "what_to_do": str(payload.get("what_to_do") or fallback["what_to_do"]),
        "summary": _coerce_summary_text(payload.get("summary"), fallback["summary"]),
        "source": "ai",
    }


def _coerce_summary_text(raw, default_text: str) -> str:
    """Return a clean prose summary, even when the AI dumped JSON into the field.

    Some models echo their entire structured response (action, confidence, …)
    into the `summary` field as JSON or as a nested dict. The page already
    renders those structured fields as KPI tiles, so we extract just the
    narrative portion and drop the rest.
    """
    if raw is None or raw == "":
        return default_text
    # Already a structured dict? extract narrative directly.
    if isinstance(raw, dict):
        return _narrative_from_dict(raw) or default_text
    s = str(raw).strip()
    # Strip ```json fences the model may have wrapped around the JSON.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    # Try parsing as a full JSON object first.
    if s.startswith("{") and s.endswith("}"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return _narrative_from_dict(parsed) or default_text
        except json.JSONDecodeError:
            pass
    # Mixed content: a JSON blob followed/preceded by prose. Drop the blob,
    # keep only the prose portion.
    if "{" in s and "}" in s:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            blob = m.group(0)
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    nested = _narrative_from_dict(parsed)
                    if nested:
                        return nested
            except json.JSONDecodeError:
                pass
            # No usable JSON — strip the brace block and return the rest.
            cleaned = (s.replace(blob, "")).strip()
            if cleaned:
                return cleaned
    return s


def _narrative_from_dict(d: dict) -> str:
    """Pull a readable paragraph out of a structured AI dict."""
    if isinstance(d.get("summary"), str) and d["summary"].strip():
        return d["summary"].strip()
    # Last-ditch: stitch the narrative-ish fields together as prose so the
    # user sees something useful instead of raw JSON.
    parts = []
    if d.get("what_to_do"):
        parts.append(f"**Next action:** {d['what_to_do']}")
    drivers = d.get("key_drivers") or []
    if isinstance(drivers, list) and drivers:
        parts.append("**Key drivers:** " + "; ".join(str(x) for x in drivers))
    risks = d.get("key_risks") or []
    if isinstance(risks, list) and risks:
        parts.append("**Key risks:** " + "; ".join(str(x) for x in risks))
    return "\n\n".join(parts)


@stock_analysis_api.route("/api/stock_analysis", methods=["POST"])
def stock_analysis():
    if "email" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    # Plan / quota gate
    from application.services import plans
    uid = session.get("user_id", "")
    plan = plans.current_plan()
    if not plans.can_use(uid, plan, "ai_single"):
        limit = plan.get("limits", {}).get("ai_single")
        return jsonify({
            "error": "quota_exceeded",
            "message": f"You've used your monthly limit of {limit} AI analyses. Upgrade for more.",
            "current_plan": plan["id"],
            "upgrade_url": "/billing",
        }), 402

    data = request.get_json(silent=True) or {}
    stock = data.get("stock") or {}
    if not stock.get("StockName"):
        return jsonify({"error": "Missing stock"}), 400

    if not is_configured():
        plans.increment_usage(uid, "ai_single")
        return jsonify(_rule_based_kpis(stock))

    # ── RAG: retrieve recent news/filings for grounding (best-effort) ──
    rag_block, rag_sources = "", []
    try:
        symbol_for_rag = stock.get("Symbol") or stock.get("StockName", "")
        rag_query = (
            f"{stock.get('StockName', '')} latest news earnings outlook "
            f"{stock.get('Sector', '')}"
        )
        rag_block, rag_sources = rag_retriever.build_context(
            symbol_for_rag, query=rag_query, top_k=5)
    except Exception as e:
        log.warning("[stock_analysis] RAG build_context failed: %s", e)

    prompt = (
        f"Stock holding details:\n"
        f"- Name: {stock.get('StockName')}\n"
        f"- Sector: {stock.get('Sector', 'N/A')}\n"
        f"- Exchange: {stock.get('Exchange', 'N/A')}\n"
        f"- Quantity held: {stock.get('Quantity', 0)}\n"
        f"- Purchase price: {stock.get('PurchasePrice', 0)}\n"
        f"- Current price: {stock.get('CurrentPrice', 0)}\n"
        f"- Purchase date: {stock.get('PurchaseDate', 'N/A')}\n\n"
    )
    if rag_block:
        prompt += rag_block + "\n\n"
    prompt += "Return the JSON object as specified."
    content, err = ai_chat(
        [
            {"role": "system", "content": STOCK_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3, max_tokens=900,
    )
    if err or not content:
        result = _rule_based_kpis(stock)
        result["summary"] += f"\n\n_Live AI unavailable: {err or 'no response'}_"
        result["sources_used"] = rag_sources
        return jsonify(result)

    payload = _parse_ai_json(content)
    if not payload or not isinstance(payload, dict):
        result = _rule_based_kpis(stock)
        result["summary"] = content
        result["source"] = "ai-text"
        result["sources_used"] = rag_sources
        plans.increment_usage(uid, "ai_single")
        return jsonify(result)

    plans.increment_usage(uid, "ai_single")
    out = _normalize(payload, stock)
    out["sources_used"] = rag_sources
    track_event(uid, "ai_query_run", {"symbol": stock.get("Symbol", "")})
    return jsonify(out)


@stock_analysis_api.route("/api/stock_analysis/bulk", methods=["POST"])
def stock_analysis_bulk():
    """Analyze multiple stocks sequentially and return a list of results."""
    if "email" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    # Plan / quota gate — bulk requires Pro+ and consumes 1 ai_bulk run
    from application.services import plans
    uid = session.get("user_id", "")
    plan = plans.current_plan()
    if plans.PLAN_RANK.get(plan["id"], 0) < plans.PLAN_RANK["pro"]:
        return jsonify({
            "error": "upgrade_required",
            "message": "Bulk \"Analyze all stocks\" is a Pro feature. Upgrade to unlock.",
            "current_plan": plan["id"],
            "upgrade_url": "/billing",
        }), 402
    if not plans.can_use(uid, plan, "ai_bulk"):
        limit = plan.get("limits", {}).get("ai_bulk")
        return jsonify({
            "error": "quota_exceeded",
            "message": f"You've used your monthly limit of {limit} bulk runs. Upgrade for more.",
            "current_plan": plan["id"],
            "upgrade_url": "/billing",
        }), 402

    data = request.get_json(silent=True) or {}
    stock_list = data.get("stocks") or []
    if not stock_list:
        return jsonify({"error": "No stocks provided"}), 400

    # Cap to 30 stocks max to avoid timeout
    stock_list = stock_list[:30]
    results = []

    for stock in stock_list:
        if not stock.get("StockName"):
            continue

        if not is_configured():
            results.append({
                "stock_name": stock.get("StockName"),
                "symbol": stock.get("Symbol", ""),
                **_rule_based_kpis(stock)
            })
            continue

        # RAG context (best-effort, per-stock)
        rag_block, rag_sources = "", []
        try:
            symbol_for_rag = stock.get("Symbol") or stock.get("StockName", "")
            rag_query = (
                f"{stock.get('StockName', '')} latest news earnings outlook "
                f"{stock.get('Sector', '')}"
            )
            rag_block, rag_sources = rag_retriever.build_context(
                symbol_for_rag, query=rag_query, top_k=4)
        except Exception as e:
            log.warning("[stock_analysis.bulk] RAG failed for %s: %s",
                        stock.get("StockName"), e)

        prompt = (
            f"Stock holding details:\n"
            f"- Name: {stock.get('StockName')}\n"
            f"- Sector: {stock.get('Sector', 'N/A')}\n"
            f"- Exchange: {stock.get('Exchange', 'N/A')}\n"
            f"- Quantity held: {stock.get('Quantity', 0)}\n"
            f"- Purchase price: {stock.get('PurchasePrice', 0)}\n"
            f"- Current price: {stock.get('CurrentPrice', 0)}\n"
            f"- Purchase date: {stock.get('PurchaseDate', 'N/A')}\n\n"
        )
        if rag_block:
            prompt += rag_block + "\n\n"
        prompt += "Return the JSON object as specified."
        content, err = ai_chat(
            [
                {"role": "system", "content": STOCK_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3, max_tokens=900,
        )
        if err or not content:
            result = _rule_based_kpis(stock)
            result["summary"] += f"\n\n_Live AI unavailable: {err or 'no response'}_"
        else:
            payload = _parse_ai_json(content)
            if not payload or not isinstance(payload, dict):
                result = _rule_based_kpis(stock)
                result["summary"] = content
                result["source"] = "ai-text"
            else:
                result = _normalize(payload, stock)

        result["stock_name"] = stock.get("StockName")
        result["symbol"] = stock.get("Symbol", "")
        result["sources_used"] = rag_sources
        results.append(result)

    plans.increment_usage(uid, "ai_bulk")
    return jsonify({"results": results})
