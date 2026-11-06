"""Stock search & info endpoints — backed by the market_data abstraction."""
import logging
import re

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from application.services import market_data

stock_lookup_api = Blueprint("stock_lookup_api", __name__)
log = logging.getLogger(__name__)


def _normalise_symbol(raw: str) -> str:
    symbol = (raw or "").strip().upper()
    if ":" in symbol:
        exchange, symbol = symbol.split(":", 1)
        if exchange not in ("NSE", "BSE"):
            return ""
        symbol = symbol.split("-", 1)[0]
        symbol += ".BO" if exchange == "BSE" else ".NS"
    elif "." not in symbol:
        symbol += ".NS"
    if not re.fullmatch(r"[A-Z0-9&-]+\.(?:NS|BO)", symbol):
        return ""
    return symbol


def _number(value):
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


@stock_lookup_api.route("/stock/<path:symbol>")
def stock_detail_page(symbol):
    if "email" not in session:
        return redirect(url_for("logIn"))
    normalized = _normalise_symbol(symbol)
    if not normalized:
        return render_template("error.html", code=404, message="Invalid stock symbol"), 404
    return render_template(
        "stockDetail.html",
        title="Stock Details",
        stock_symbol=normalized.rsplit(".", 1)[0],
        exchange="BSE" if normalized.endswith(".BO") else "NSE",
    )


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


@stock_lookup_api.route("/api/stock/history")
def stock_history():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    symbol = _normalise_symbol(request.args.get("symbol") or "")
    if not symbol:
        return jsonify({"error": "invalid symbol"}), 400

    chart_range = (request.args.get("range") or "1D").upper()
    ranges = {
        "1D": (5, "5m"),
        "1W": (7, "15m"),
        "1M": (30, "1d"),
        "3M": (90, "1d"),
        "6M": (180, "1d"),
        "1Y": (365, "1d"),
        "5Y": (1825, "1d"),
    }
    if chart_range not in ranges:
        return jsonify({"error": "unsupported range"}), 400

    days, interval = ranges[chart_range]
    try:
        history = market_data.get_history(symbol, days=days, interval=interval)
        if history is None or history.empty:
            return jsonify({
                "symbol": symbol,
                "range": chart_range,
                "points": [],
                "available": False,
            })

        rows = []
        latest_day = None
        if chart_range == "1D":
            latest_day = history.index[-1].strftime("%Y-%m-%d")
        for timestamp, row in history.iterrows():
            if latest_day and timestamp.strftime("%Y-%m-%d") != latest_day:
                continue
            point = {
                "time": timestamp.isoformat(),
                "open": _number(row.get("Open")),
                "high": _number(row.get("High")),
                "low": _number(row.get("Low")),
                "close": _number(row.get("Close")),
                "volume": _number(row.get("Volume")),
            }
            if point["close"] is not None:
                rows.append(point)

        if not rows:
            return jsonify({
                "symbol": symbol,
                "range": chart_range,
                "points": [],
                "available": False,
            })

        first = rows[0]["close"]
        last = rows[-1]["close"]
        return jsonify({
            "symbol": symbol,
            "range": chart_range,
            "points": rows,
            "available": True,
            "summary": {
                "change": round(last - first, 2),
                "change_pct": round((last - first) / first * 100, 2) if first else None,
                "low": round(min((item["low"] if item["low"] is not None else item["close"]) for item in rows), 2),
                "high": round(max((item["high"] if item["high"] is not None else item["close"]) for item in rows), 2),
                "volume": sum(item["volume"] or 0 for item in rows),
            },
        })
    except Exception as e:
        log.exception("Stock history failed for %s", symbol)
        return jsonify({"error": "history unavailable", "detail": str(e)}), 500


@stock_lookup_api.route("/api/stock/updates")
def stock_updates():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    symbol = _normalise_symbol(request.args.get("symbol") or "")
    if not symbol:
        return jsonify({"error": "invalid symbol"}), 400

    try:
        from application.services.investing_tools import corporate_actions
        from application.services.rag import retriever as rag_retriever
        from application.services.rag.ingest import nse_filings

        docs = rag_retriever.retrieve(symbol, top_k=40, days_back=180)
        filings = nse_filings.fetch_announcements(symbol.rsplit(".", 1)[0], days_back=90)

        def serialise(item):
            published = item.get("published_at") or item.get("PublishedAt")
            return {
                "title": item.get("title") or item.get("Title") or "Company update",
                "content": item.get("content") or item.get("Content") or "",
                "url": item.get("url") or item.get("Url") or "",
                "source": item.get("source") or item.get("Source") or "Company update",
                "type": item.get("doc_type") or item.get("DocType") or "news",
                "published_at": published.isoformat() if hasattr(published, "isoformat") else published,
            }

        feed_items = [serialise(item) for item in docs]
        announcement_items = [
            serialise(item) for item in docs
            if (item.get("doc_type") or item.get("DocType")) == "filing"
        ]
        announcement_items.extend(serialise(item) for item in filings)
        action_data = corporate_actions([symbol])
        actions = []
        for stock in action_data.get("stocks") or []:
            for action in stock.get("events") or []:
                actions.append({
                    **action,
                    "symbol": stock.get("symbol") or symbol,
                    "name": stock.get("name") or symbol.rsplit(".", 1)[0],
                })

        return jsonify({
            "symbol": symbol,
            "news": [item for item in feed_items if item["type"] == "news"],
            "announcements": announcement_items,
            "actions": actions,
            "feeds": feed_items,
        })
    except Exception as e:
        log.exception("Stock updates failed for %s", symbol)
        return jsonify({"error": "updates unavailable", "detail": str(e)}), 500


@stock_lookup_api.route("/api/stock/oi")
def stock_open_interest():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    symbol = _normalise_symbol(request.args.get("symbol") or "")
    if not symbol:
        return jsonify({"error": "invalid symbol"}), 400

    bare_symbol = symbol.rsplit(".", 1)[0]
    try:
        from application.services.option_chain import _fetch_stock_chain_dhan

        chain = _fetch_stock_chain_dhan(bare_symbol)
        if not chain:
            from application.services.fno_gap_forecast import UNIVERSE, _fetch_stock_chain

            metadata = UNIVERSE.get(bare_symbol)
            if metadata:
                chain = _fetch_stock_chain(metadata["fyers"])

        if not chain:
            return jsonify({
                "symbol": symbol,
                "available": False,
                "message": "No stock option chain is available. The stock may not have listed derivatives, or a broker data feed may be unavailable.",
            })

        rows = chain.get("rows") or []
        support = max(
            (row for row in rows if (row.get("pe") or {}).get("oi", 0)),
            key=lambda row: (row.get("pe") or {}).get("oi", 0),
            default={},
        )
        resistance = max(
            (row for row in rows if (row.get("ce") or {}).get("oi", 0)),
            key=lambda row: (row.get("ce") or {}).get("oi", 0),
            default={},
        )
        return jsonify({
            "symbol": symbol,
            "available": True,
            "spot": chain.get("spot"),
            "expiry": chain.get("expiry"),
            "pcr": chain.get("pcr"),
            "totals": chain.get("totals") or {},
            "support": support.get("strike"),
            "resistance": resistance.get("strike"),
            "rows": rows,
        })
    except Exception as e:
        log.exception("Stock OI analysis failed for %s", symbol)
        return jsonify({"error": "open interest unavailable", "detail": str(e)}), 500
