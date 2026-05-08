"""Per-stock AI analysis endpoint — returns structured KPIs + summary."""
import json
import re

from flask import Blueprint, request, jsonify, session

from application.services.ai_client import chat as ai_chat, is_configured

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
        "summary": str(payload.get("summary") or fallback["summary"]),
        "source": "ai",
    }


@stock_analysis_api.route("/api/stock_analysis", methods=["POST"])
def stock_analysis():
    if "email" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    stock = data.get("stock") or {}
    if not stock.get("StockName"):
        return jsonify({"error": "Missing stock"}), 400

    if not is_configured():
        return jsonify(_rule_based_kpis(stock))

    prompt = (
        f"Stock holding details:\n"
        f"- Name: {stock.get('StockName')}\n"
        f"- Sector: {stock.get('Sector', 'N/A')}\n"
        f"- Exchange: {stock.get('Exchange', 'N/A')}\n"
        f"- Quantity held: {stock.get('Quantity', 0)}\n"
        f"- Purchase price: {stock.get('PurchasePrice', 0)}\n"
        f"- Current price: {stock.get('CurrentPrice', 0)}\n"
        f"- Purchase date: {stock.get('PurchaseDate', 'N/A')}\n\n"
        "Return the JSON object as specified."
    )
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
        return jsonify(result)

    payload = _parse_ai_json(content)
    if not payload or not isinstance(payload, dict):
        result = _rule_based_kpis(stock)
        result["summary"] = content
        result["source"] = "ai-text"
        return jsonify(result)

    return jsonify(_normalize(payload, stock))
