"""Stock search & info endpoints — backed by the market_data abstraction."""
from flask import Blueprint, jsonify, request, session

from application.services import market_data

stock_lookup_api = Blueprint("stock_lookup_api", __name__)


@stock_lookup_api.route("/api/stock/search")
def stock_search():
    """Return symbol search suggestions for the given query."""
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    return jsonify({"results": market_data.search(q)})


@stock_lookup_api.route("/api/stock/info")
def stock_info():
    """Return live price + sector + display name for a symbol."""
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400

    try:
        info = market_data.get_info(symbol) or {}
        quote = market_data.get_quote(symbol) or {}
        return jsonify({
            "symbol": symbol,
            "name": info.get("name") or quote.get("name") or symbol,
            "sector": info.get("sector") or "",
            "industry": info.get("industry") or "",
            "exchange": info.get("exchange") or "",
            "currency": info.get("currency") or "INR",
            "price": quote.get("price"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
