"""Stock search & info endpoints (Yahoo Finance backed)."""
import requests
import yfinance as yf
from flask import Blueprint, jsonify, request, session

stock_lookup_api = Blueprint("stock_lookup_api", __name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
}


@stock_lookup_api.route("/api/stock/search")
def stock_search():
    """Return Yahoo Finance search suggestions for the given query."""
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})

    url = "https://query2.finance.yahoo.com/v1/finance/search"
    try:
        r = requests.get(
            url,
            params={"q": q, "quotesCount": 10, "newsCount": 0, "lang": "en-IN", "region": "IN"},
            headers=_HEADERS, timeout=6,
        )
        if r.status_code != 200:
            return jsonify({"results": []})
        data = r.json()
    except (requests.RequestException, ValueError):
        return jsonify({"results": []})

    results = []
    for q_ in data.get("quotes", []) or []:
        sym = q_.get("symbol")
        if not sym:
            continue
        # Prefer NSE / BSE listings for Indian stocks
        results.append({
            "symbol": sym,
            "name": q_.get("shortname") or q_.get("longname") or sym,
            "exchange": q_.get("exchDisp") or q_.get("exchange") or "",
            "type": q_.get("typeDisp") or q_.get("quoteType") or "",
        })

    # Indian first
    results.sort(key=lambda r_: 0 if r_["symbol"].endswith((".NS", ".BO")) else 1)
    return jsonify({"results": results[:10]})


def _live_price(ticker):
    info = getattr(ticker, "fast_info", None)
    if info:
        for k in ("last_price", "lastPrice", "regular_market_price"):
            v = info.get(k) if hasattr(info, "get") else None
            if v:
                return float(v)
    try:
        hist = ticker.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


@stock_lookup_api.route("/api/stock/info")
def stock_info():
    """Return live price + sector + display name for a symbol."""
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400

    try:
        ticker = yf.Ticker(symbol)
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            info = {}
        price = _live_price(ticker)
        return jsonify({
            "symbol": symbol,
            "name": info.get("shortName") or info.get("longName") or symbol,
            "sector": info.get("sector") or "",
            "industry": info.get("industry") or "",
            "exchange": info.get("exchange") or "",
            "currency": info.get("currency") or "",
            "price": price,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
