"""AI portfolio insight endpoint."""
from flask import Blueprint, request, jsonify, session

from application.services.ai_client import chat as ai_chat, is_configured

ai_portfolio_api = Blueprint("ai_portfolio_api", __name__)

PORTFOLIO_SYSTEM_PROMPT = (
    "You are an experienced financial advisor specializing in Indian equity markets. "
    "Analyze portfolios for diversification, sector allocation, risk concentration, "
    "and answer user questions in clear, friendly language. "
    "Use markdown headings, bullet lists, and short paragraphs. "
    "Provide actionable, specific suggestions but ALWAYS remind the user this is "
    "informational and not a recommendation to buy or sell."
)


def _summarize_portfolio(stocks):
    if not stocks:
        return "User has not added any stocks yet."
    lines = []
    total_invested = 0.0
    total_current = 0.0
    for s in stocks:
        try:
            qty = float(s.get("Quantity") or 0)
            pp = float(s.get("PurchasePrice") or 0)
            cp = float(s.get("CurrentPrice") or 0) or pp
        except (TypeError, ValueError):
            qty = pp = cp = 0
        invested = qty * pp
        current = qty * cp
        total_invested += invested
        total_current += current
        lines.append(
            f"- {s.get('StockName', '?')} ({s.get('Sector', 'Other')}) | "
            f"Qty: {int(qty)} | Buy: \u20b9{pp:.2f} | Now: \u20b9{cp:.2f} | "
            f"Invested: \u20b9{invested:.2f} | Value: \u20b9{current:.2f}"
        )
    pl = total_current - total_invested
    pl_pct = (pl / total_invested * 100) if total_invested else 0
    summary = "\n".join(lines)
    summary += (
        f"\n\nTotals \u2014 Invested: \u20b9{total_invested:.2f}, "
        f"Current Value: \u20b9{total_current:.2f}, "
        f"P/L: \u20b9{pl:.2f} ({pl_pct:.2f}%)"
    )
    return summary


@ai_portfolio_api.route("/api/ai_portfolio_insight", methods=["POST"])
def ai_portfolio_insight():
    if "email" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    stocks = data.get("stocks") or []
    user_query = (data.get("query") or "").strip()

    if not stocks and not user_query:
        return jsonify({"error": "No portfolio or question provided."}), 400

    if not is_configured():
        return jsonify({
            "insight": (
                "**AI service not configured.**\n\n"
                "Set `OPENAI_API_KEY` and `OPENAI_ENDPOINT` to enable insights.\n\n"
                + _summarize_portfolio(stocks)
            )
        })

    portfolio_text = _summarize_portfolio(stocks)
    user_block = (
        f"User question: {user_query}\n\n" if user_query else
        "User has not asked a specific question. Provide a concise overall analysis."
    )
    prompt = (
        f"Portfolio holdings:\n{portfolio_text}\n\n"
        f"{user_block}"
        "Please respond with:\n"
        "1. **Snapshot** \u2013 overall posture in 1\u20132 sentences.\n"
        "2. **Diversification & Risk** \u2013 sector and concentration view.\n"
        "3. **Suggestions** \u2013 3 short, actionable points.\n"
        "4. If the user asked a question, answer it clearly at the end."
    )

    content, err = ai_chat(
        [
            {"role": "system", "content": PORTFOLIO_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.6, max_tokens=900,
    )
    if err:
        return jsonify({
            "insight": (
                "**Live AI is unavailable right now.** Quick summary while we retry:\n\n"
                + _summarize_portfolio(stocks)
                + f"\n\n_Detail:_ {err}"
            )
        })
    return jsonify({"insight": content})
