"""Stock Screener — page route and cached screening universe.

The screener is intentionally built on top of the data the platform already
computes for its scanners:

* ``swing_scanner.scan()`` supplies the liquidity-checked NIFTY universe with
  price, volume-multiple and technical factors (cached by the scan scheduler).
* ``fundamentals_provider.get_fundamentals()`` adds valuation, profitability
  and sector information (cached for a week per symbol).

Rows are joined once and cached, so filtering happens in the browser and the
sliders/accordions respond without a round-trip. A cold fundamentals fetch is
best-effort: rows still render with technical fields when fundamentals are
missing, and the payload reports how much coverage was available.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from application.services import cache as shared_cache
from application.services.event_tracker import track_feature

screener_api = Blueprint("screener_api", __name__)

log = logging.getLogger(__name__)

_CACHE_KEY = "screener:rows:v1"
_CACHE_TTL = 30 * 60          # complete payload, 30 minutes
_PARTIAL_TTL = 3 * 60         # retry quickly when fundamentals were missing
_FUNDAMENTALS_DEADLINE = 15   # seconds before we respond with partial rows
_MAX_WORKERS = 8

_SYMBOL_RE = re.compile(r"[^A-Z0-9._&-]")


@screener_api.route("/stock-screener")
@track_feature("stock_screener")
def stock_screener_page():
    if "email" not in session:
        return redirect(url_for("logIn"))
    return render_template(
        "stockScreener.html",
        title="Stock Screener",
        name=session.get("name", "User"),
        email=session.get("email", ""),
    )


def _short_symbol(symbol: str) -> str:
    return (symbol or "").replace(".NS", "").replace(".BO", "")


def _sector_lookup() -> dict:
    """Best-effort symbol → sector map from the heatmap taxonomy.

    Fundamentals carry the authoritative sector, but a cold fundamentals cache
    would otherwise leave the sector filter empty. The heatmap map is static
    and free, so it lets every row land in a sector immediately.
    """
    try:
        from application.routes.heatmap import SECTOR_STOCKS

        lookup: dict[str, str] = {}
        for sector, symbols in SECTOR_STOCKS.items():
            for symbol in symbols:
                lookup.setdefault(_short_symbol(symbol).upper(), sector)
        return lookup
    except Exception:  # noqa: BLE001
        return {}


def _as_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _fundamentals_bits(symbol: str) -> dict:
    """Return the subset of fundamentals used by the screener filters."""
    from application.services.investing_tools import _flatten_fundamentals

    flat = _flatten_fundamentals(symbol) or {}
    return {
        "sector": flat.get("sector") or "Unclassified",
        "pe": _as_float(flat.get("pe")),
        "pb": _as_float(flat.get("pb")),
        "roe": _as_float(flat.get("roe")),
        "de": _as_float(flat.get("de")),
        "rev_growth_3y": _as_float(flat.get("rev_growth_3y")),
        "div_yield": _as_float(flat.get("div_yield")),
        "market_cap": _as_float(flat.get("market_cap")),
    }


def _row_from_stock(stock: dict, fundamentals: dict, sector_fallback: str = "") -> dict:
    price = _as_float(stock.get("price"))
    sma50 = _as_float(stock.get("sma50"))
    sma200 = _as_float(stock.get("sma200"))
    sector = fundamentals.get("sector") or sector_fallback or "Unclassified"
    return {
        "symbol": _short_symbol(stock.get("symbol")),
        "name": stock.get("name") or _short_symbol(stock.get("symbol")),
        "sector": sector,
        "price": price,
        "vol_multiple": _as_float(stock.get("vol_thrust")),
        "turnover_cr": _as_float(stock.get("turnover_cr")),
        "ret_1m": _as_float(stock.get("ret_1m")),
        "ret_3m": _as_float(stock.get("ret_3m")),
        "ret_6m": _as_float(stock.get("ret_6m")),
        "pct_from_52h": _as_float(stock.get("pct_from_52h")),
        "pct_from_52l": _as_float(stock.get("pct_from_52l")),
        "adr_pct": _as_float(stock.get("adr_pct")),
        "atr_ratio": _as_float(stock.get("atr_ratio")),
        "above_sma50": bool(price is not None and sma50 is not None and price > sma50),
        "above_sma200": bool(price is not None and sma200 is not None and price > sma200),
        "pe": fundamentals.get("pe"),
        "pb": fundamentals.get("pb"),
        "roe": fundamentals.get("roe"),
        "de": fundamentals.get("de"),
        "rev_growth_3y": fundamentals.get("rev_growth_3y"),
        "div_yield": fundamentals.get("div_yield"),
        "market_cap": fundamentals.get("market_cap"),
    }


def _build_rows() -> dict:
    from application.services import swing_scanner

    scan = swing_scanner.scan() or {}
    stocks = scan.get("stocks") or []

    fundamentals_by_symbol: dict[str, dict] = {}
    if stocks:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            futures = {
                executor.submit(_fundamentals_bits, stock.get("symbol")): stock.get("symbol")
                for stock in stocks
                if stock.get("symbol")
            }
            try:
                from concurrent.futures import as_completed

                for future in as_completed(futures, timeout=_FUNDAMENTALS_DEADLINE):
                    symbol = futures[future]
                    try:
                        fundamentals_by_symbol[symbol] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        log.debug("screener fundamentals skipped for %s: %s", symbol, exc)
            except FuturesTimeoutError:
                log.warning(
                    "screener: fundamentals deadline reached (%ss); returning partial rows",
                    _FUNDAMENTALS_DEADLINE,
                )

    sector_map = _sector_lookup()
    rows = []
    for stock in stocks:
        symbol = stock.get("symbol") or ""
        rows.append(
            _row_from_stock(
                stock,
                fundamentals_by_symbol.get(symbol, {}),
                sector_map.get(_short_symbol(symbol).upper(), ""),
            )
        )
    rows.sort(
        key=lambda row: (
            row.get("market_cap") or 0,
            row.get("turnover_cr") or 0,
            row.get("symbol") or "",
        ),
        reverse=True,
    )

    # Sector list is derived from what we actually resolved, so the filter
    # never offers a sector that would return zero rows.
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["sector"]] = counts.get(row["sector"], 0) + 1

    covered = sum(1 for row in rows if row.get("pe") is not None or row.get("roe") is not None)
    universe = scan.get("universe_size") or len(stocks)
    return {
        "rows": rows,
        "sectors": [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "count": len(rows),
        "universe_size": universe,
        "fundamentals_covered": covered,
        "fundamentals_partial": covered < len(rows),
        "scanned_at": scan.get("scan_time") or "",
        "as_of": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30)))
        .strftime("%d %b %Y, %I:%M %p IST"),
    }


@screener_api.route("/api/stock-screener")
def stock_screener_data():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401

    force = request.args.get("refresh") == "1"
    if not force:
        cached = shared_cache.jget(_CACHE_KEY)
        if isinstance(cached, dict) and cached.get("rows") is not None:
            return jsonify({**cached, "cached": True})

    try:
        payload = _build_rows()
    except Exception as exc:  # noqa: BLE001
        log.exception("screener: build failed")
        return jsonify({
            "error": "screener_unavailable",
            "detail": str(exc)[:200],
            "rows": [],
            "sectors": [],
            "count": 0,
        }), 200

    ttl = _PARTIAL_TTL if payload.get("fundamentals_partial") else _CACHE_TTL
    try:
        shared_cache.jset(_CACHE_KEY, payload, ttl=ttl)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({**payload, "cached": False})
