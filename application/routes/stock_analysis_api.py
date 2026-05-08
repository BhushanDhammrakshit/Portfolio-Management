"""Per-stock AI analysis endpoint."""
from flask import Blueprint, request, jsonify, session

from application.services.ai_client import chat as ai_chat, is_configured

stock_analysis_api = Blueprint("stock_analysis_api", __name__)

STOCK_SYSTEM_PROMPT = (
    "You are a senior equity analyst covering Indian markets. Given a single "
    "stock holding, provide a concise outlook covering: business overview, "
    "recent performance, sector context, key risks, and a short verdict. "
    "Use markdown with bold section headings. Always remind the user this is "
    "informational, not a recommendation to buy or sell."
)


def _snapshot(stock: dict) -> str:
    name = stock.get("StockName", "this stock")
    qty = float(stock.get("Quantity") or 0)
    pp = float(stock.get("PurchasePrice") or 0)
    cp = float(stock.get("CurrentPrice") or 0) or pp
    invested = qty * pp
    current = qty * cp
    pl = current - invested
    pl_pct = (pl / invested * 100) if invested else 0
    sector = stock.get("Sector", "N/A")
    return (
        f"**{name}** ({sector})\n\n"
        f"- Quantity: {int(qty)}\n"
        f"- Buy price: \u20b9{pp:.2f}\n"
        f"- Current price: \u20b9{cp:.2f}\n"
        f"- Invested: \u20b9{invested:.2f}\n"
        f"- Value: \u20b9{current:.2f}\n"
        f"- P/L: \u20b9{pl:.2f} ({pl_pct:.2f}%)"
    )


@stock_analysis_api.route("/api/stock_analysis", methods=["POST"])
def stock_analysis():
    if "email" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    stock = data.get("stock") or {}
    if not stock.get("StockName"):
        return jsonify({"error": "Missing stock"}), 400

    if not is_configured():
        return jsonify({"analysis": _snapshot(stock) +
                        "\n\n_AI service not configured._"})

    prompt = (
        f"Stock holding details:\n"
        f"- Name: {stock.get('StockName')}\n"
        f"- Sector: {stock.get('Sector', 'N/A')}\n"
        f"- Exchange: {stock.get('Exchange', 'N/A')}\n"
        f"- Quantity held: {stock.get('Quantity', 0)}\n"
        f"- Purchase price: {stock.get('PurchasePrice', 0)}\n"
        f"- Current price: {stock.get('CurrentPrice', 0)}\n"
        f"- Purchase date: {stock.get('PurchaseDate', 'N/A')}\n\n"
        "Provide a focused outlook in 4 sections: **Business**, **Recent "
        "Performance**, **Sector & Risks**, **Verdict**. Keep it under 250 words."
    )
    content, err = ai_chat(
        [
            {"role": "system", "content": STOCK_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5, max_tokens=600,
    )
    if err:
        return jsonify({"analysis": _snapshot(stock) +
                        f"\n\n_Live AI unavailable: {err}_"})
    return jsonify({"analysis": content})
