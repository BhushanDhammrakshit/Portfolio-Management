"""Global Markets — major-country index heatmap with regional sentiment.

Uses yfinance directly (via the project's `yfinance_provider`) since the
broker providers only cover Indian symbols. Cached for ~1 minute so the
"LIVE" session indicator flips on/off close to real time.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from urllib.parse import quote

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from application.services import cache as shared_cache
from application.services import snapshot_store
from application.services.http_session import get_session
from application.services.providers import yfinance_provider as yfp

global_markets_api = Blueprint("global_markets_api", __name__)
log = logging.getLogger(__name__)

# Per-ticker cash-equity session windows (Mon-Fri local).
# Tuples: (timezone, open HH:MM, close HH:MM). Lunch breaks are
# subsumed into a single open window — close enough for a "is the
# market currently active" indicator. Public holidays are not handled.
_SESSIONS: dict[str, tuple[str, str, str]] = {
    # Asia-Pacific
    "^NSEI":     ("Asia/Kolkata",     "09:15", "15:30"),
    "^BSESN":    ("Asia/Kolkata",     "09:15", "15:30"),
    "^N225":     ("Asia/Tokyo",       "09:00", "15:00"),
    "000001.SS": ("Asia/Shanghai",    "09:30", "15:00"),
    "^HSI":      ("Asia/Hong_Kong",   "09:30", "16:00"),
    "^KS11":     ("Asia/Seoul",       "09:00", "15:30"),
    "^TWII":     ("Asia/Taipei",      "09:00", "13:30"),
    "^STI":      ("Asia/Singapore",   "09:00", "17:00"),
    "^AXJO":     ("Australia/Sydney", "10:00", "16:00"),
    # Europe
    "^FTSE":     ("Europe/London",    "08:00", "16:30"),
    "^GDAXI":    ("Europe/Berlin",    "09:00", "17:30"),
    "^FCHI":     ("Europe/Paris",     "09:00", "17:30"),
    "^IBEX":     ("Europe/Madrid",    "09:00", "17:30"),
    "^SSMI":     ("Europe/Zurich",    "09:00", "17:30"),
    "^STOXX50E": ("Europe/Berlin",    "09:00", "17:30"),
    # Americas
    "^GSPC":     ("America/New_York", "09:30", "16:00"),
    "^IXIC":     ("America/New_York", "09:30", "16:00"),
    "^DJI":      ("America/New_York", "09:30", "16:00"),
    "^GSPTSE":   ("America/Toronto",  "09:30", "16:00"),
    "^BVSP":     ("America/Sao_Paulo","10:00", "17:00"),
    "^MXX":      ("America/Mexico_City","08:30","15:00"),
}

_INDIAN_INDEX_TICKERS = {
    "^NSEI", "^NSEBANK", "^CNX100", "^CNX200", "^CRSLDX", "^CNXFIN",
    "^CNXIT", "^CNXAUTO", "^CNXPHARMA", "^CNXFMCG", "^CNXMETAL",
    "^CNXREALTY", "^CNXENERGY", "^CNXINFRA", "^CNXPSUBANK",
    "^CNXMEDIA", "^NSMIDCP", "^CNXMDCP", "^CNXSC", "^NSEMDCP50",
    "^INDIAVIX", "^BSESN",
}


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _market_status(ticker: str) -> dict:
    """Return current open/closed info for `ticker` based on local clock."""
    sess = _SESSIONS.get(ticker)
    if not sess and ticker in _INDIAN_INDEX_TICKERS:
        sess = ("Asia/Kolkata", "09:15", "15:30")
    if not sess:
        return {"is_open": False, "session": None}
    tz_name, open_s, close_s = sess
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        return {"is_open": False, "session": f"{open_s}-{close_s} {tz_name}"}
    open_t = _parse_hhmm(open_s)
    close_t = _parse_hhmm(close_s)
    is_weekday = now.weekday() < 5  # Mon=0..Fri=4
    is_in_window = open_t <= now.time() <= close_t
    return {
        "is_open": bool(is_weekday and is_in_window),
        "session": f"{open_s}–{close_s} {tz_name.split('/')[-1].replace('_', ' ')}",
    }


# (region, country, flag, ticker, index_name)
INDICES: list[dict] = [
    # Asia-Pacific
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^NSEI",    "index": "Nifty 50"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNX100",  "index": "Nifty 100"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNX200",  "index": "Nifty 200"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CRSLDX",  "index": "Nifty 500"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^NSEBANK", "index": "Nifty Bank"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXFIN",  "index": "Nifty Financial Services"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXIT",   "index": "Nifty IT"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXAUTO", "index": "Nifty Auto"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXPHARMA", "index": "Nifty Pharma"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXFMCG", "index": "Nifty FMCG"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXMETAL", "index": "Nifty Metal"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXREALTY", "index": "Nifty Realty"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXENERGY", "index": "Nifty Energy"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXINFRA", "index": "Nifty Infrastructure"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXPSUBANK", "index": "Nifty PSU Bank"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXMEDIA", "index": "Nifty Media"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^NSMIDCP", "index": "Nifty Next 50"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXMDCP",  "index": "Nifty Midcap 100"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^NSEMDCP50", "index": "Nifty Midcap 50"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^CNXSC",   "index": "Nifty Smallcap 100"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^INDIAVIX", "index": "India VIX"},
    {"region": "Asia-Pacific", "country": "India",        "flag": "🇮🇳", "ticker": "^BSESN",   "index": "Sensex"},
    {"region": "Asia-Pacific", "country": "Japan",        "flag": "🇯🇵", "ticker": "^N225",    "index": "Nikkei 225"},
    {"region": "Asia-Pacific", "country": "China",        "flag": "🇨🇳", "ticker": "000001.SS","index": "Shanghai Composite"},
    {"region": "Asia-Pacific", "country": "Hong Kong",    "flag": "🇭🇰", "ticker": "^HSI",     "index": "Hang Seng"},
    {"region": "Asia-Pacific", "country": "South Korea",  "flag": "🇰🇷", "ticker": "^KS11",    "index": "KOSPI"},
    {"region": "Asia-Pacific", "country": "Taiwan",       "flag": "🇹🇼", "ticker": "^TWII",    "index": "Taiwan Weighted"},
    {"region": "Asia-Pacific", "country": "Singapore",    "flag": "🇸🇬", "ticker": "^STI",     "index": "Straits Times"},
    {"region": "Asia-Pacific", "country": "Australia",    "flag": "🇦🇺", "ticker": "^AXJO",    "index": "ASX 200"},

    # Europe
    {"region": "Europe",       "country": "United Kingdom","flag": "🇬🇧", "ticker": "^FTSE",    "index": "FTSE 100"},
    {"region": "Europe",       "country": "Germany",      "flag": "🇩🇪", "ticker": "^GDAXI",   "index": "DAX"},
    {"region": "Europe",       "country": "France",       "flag": "🇫🇷", "ticker": "^FCHI",    "index": "CAC 40"},
    {"region": "Europe",       "country": "Spain",        "flag": "🇪🇸", "ticker": "^IBEX",    "index": "IBEX 35"},
    {"region": "Europe",       "country": "Switzerland",  "flag": "🇨🇭", "ticker": "^SSMI",    "index": "SMI"},
    {"region": "Europe",       "country": "Eurozone",     "flag": "🇪🇺", "ticker": "^STOXX50E","index": "EURO STOXX 50"},

    # Americas
    {"region": "Americas",     "country": "United States","flag": "🇺🇸", "ticker": "^GSPC",    "index": "S&P 500"},
    {"region": "Americas",     "country": "United States","flag": "🇺🇸", "ticker": "^IXIC",    "index": "Nasdaq Composite"},
    {"region": "Americas",     "country": "United States","flag": "🇺🇸", "ticker": "^DJI",     "index": "Dow Jones"},
    {"region": "Americas",     "country": "Canada",       "flag": "🇨🇦", "ticker": "^GSPTSE",  "index": "TSX Composite"},
    {"region": "Americas",     "country": "Brazil",       "flag": "🇧🇷", "ticker": "^BVSP",    "index": "Bovespa"},
    {"region": "Americas",     "country": "Mexico",       "flag": "🇲🇽", "ticker": "^MXX",     "index": "IPC"},
]

NSE_INDEX_NAMES = {
    "^NSEI": "NIFTY 50",
    "^CNX100": "NIFTY 100",
    "^CNX200": "NIFTY 200",
    "^CRSLDX": "NIFTY 500",
    "^NSEBANK": "NIFTY BANK",
    "^CNXFIN": "NIFTY FIN SERVICE",
    "^CNXIT": "NIFTY IT",
    "^CNXAUTO": "NIFTY AUTO",
    "^CNXPHARMA": "NIFTY PHARMA",
    "^CNXFMCG": "NIFTY FMCG",
    "^CNXMETAL": "NIFTY METAL",
    "^CNXREALTY": "NIFTY REALTY",
    "^CNXENERGY": "NIFTY ENERGY",
    "^CNXINFRA": "NIFTY INFRA",
    "^CNXPSUBANK": "NIFTY PSU BANK",
    "^CNXMEDIA": "NIFTY MEDIA",
    "^NSMIDCP": "NIFTY NEXT 50",
    "^CNXMDCP": "NIFTY MIDCAP 100",
    "^NSEMDCP50": "NIFTY MIDCAP 50",
    "^CNXSC": "NIFTY SMLCAP 100",
}

CACHE_KEY = "globalmarkets:v2"
CACHE_TTL = 60  # 1 min — keeps LIVE indicator close to real-time
_CONSTITUENTS_TTL = 15 * 60
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/live-market-indices",
}
_NSE_HTTP = get_session("nse-index-constituents", user_agent=_NSE_HEADERS["User-Agent"])
_BSE_HEADERS = {
    "User-Agent": _NSE_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/indices/IndexConstituent.html",
}
_BSE_HTTP = get_session("bse-index-constituents", user_agent=_BSE_HEADERS["User-Agent"])
_BSE_INDEX_CODES = {"^BSESN": 16}


def _index_by_ticker(ticker: str) -> dict | None:
    return next(
        (item for item in INDICES if item["country"] == "India" and item["ticker"] == ticker),
        None,
    )


def _fetch_index_constituents(ticker: str) -> dict:
    cache_key = "index:constituents:v1:" + ticker
    cached = shared_cache.jget(cache_key)
    if isinstance(cached, dict) and cached.get("constituents"):
        return cached

    if ticker in _BSE_INDEX_CODES:
        result = _fetch_bse_constituents(ticker)
        try:
            shared_cache.jset(cache_key, result, ttl=_CONSTITUENTS_TTL)
        except Exception as exc:
            log.warning("Could not cache BSE constituent data for %s: %s", ticker, exc)
        return result

    index_name = NSE_INDEX_NAMES.get(ticker)
    if not index_name:
        raise ValueError("This index does not publish stock constituents.")

    try:
        _NSE_HTTP.headers.update(_NSE_HEADERS)
        _NSE_HTTP.get("https://www.nseindia.com/", timeout=8)
        response = _NSE_HTTP.get(
            "https://www.nseindia.com/api/equity-stockIndices?index=" + quote(index_name),
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.warning("NSE constituents request failed for %s: %s", index_name, exc)
        raise RuntimeError("NSE constituent data is temporarily unavailable.") from exc

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("NSE returned an unexpected constituent-data response.")

    constituents = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9&-]{1,30}", symbol):
            continue
        if symbol.replace(" ", "") == index_name.replace(" ", ""):
            continue
        constituents.append({
            "symbol": symbol,
            "exchange": "NSE",
            "name": row.get("companyName") or symbol,
            "weight": _safe_float(row.get("weightage")),
            "price": _safe_float(row.get("lastPrice")),
            "change": _safe_float(row.get("change")),
            "change_pct": _safe_float(row.get("pChange")),
            "volume": _safe_float(row.get("totalTradedVolume")),
            "day_high": _safe_float(row.get("dayHigh")),
            "day_low": _safe_float(row.get("dayLow")),
            "year_high": _safe_float(row.get("yearHigh")),
            "year_low": _safe_float(row.get("yearLow")),
        })

    if not constituents:
        raise RuntimeError("NSE returned no constituent rows for this index.")
    constituents.sort(key=lambda item: item["weight"] if item["weight"] is not None else -1, reverse=True)
    result = {
        "index": index_name,
        "source": "NSE India",
        "as_of": payload.get("timestamp") if isinstance(payload, dict) else None,
        "constituents": constituents,
    }
    try:
        shared_cache.jset(cache_key, result, ttl=_CONSTITUENTS_TTL)
    except Exception as exc:
        log.warning("Could not cache NSE constituent data for %s: %s", index_name, exc)
    return result


def _bse_row_value(row: dict, *names: str):
    fields = {
        re.sub(r"[^a-z0-9]", "", str(key).lower()): value
        for key, value in row.items()
    }
    for name in names:
        value = fields.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if value not in (None, ""):
            return value
    return None


def _fetch_bse_constituents(ticker: str) -> dict:
    index_name = _index_by_ticker(ticker)["index"]
    try:
        _BSE_HTTP.headers.update(_BSE_HEADERS)
        _BSE_HTTP.get("https://www.bseindia.com/", timeout=8)
        response = _BSE_HTTP.get(
            "https://api.bseindia.com/BseIndiaAPI/api/IndexConstituent/w"
            f"?Indx_Code={_BSE_INDEX_CODES[ticker]}",
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.warning("BSE constituents request failed for %s: %s", index_name, exc)
        raise RuntimeError("BSE constituent data is temporarily unavailable.") from exc

    rows = payload if isinstance(payload, list) else None
    if rows is None and isinstance(payload, dict):
        rows = next(
            (value for value in payload.values() if isinstance(value, list)),
            None,
        )
    if not isinstance(rows, list):
        raise RuntimeError("BSE returned an unexpected constituent-data response.")

    constituents = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(_bse_row_value(row, "scrip_cd", "scrip_code", "security_code", "scripCode") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]{1,12}", code, re.IGNORECASE):
            continue
        constituents.append({
            "symbol": code,
            "exchange": "BSE",
            "name": _bse_row_value(row, "scrip_name", "company_name", "security_name", "scripName") or code,
            "weight": _safe_float(_bse_row_value(row, "weightage", "weight", "index_weight")),
            "price": _safe_float(_bse_row_value(row, "last_price", "last", "ltp", "current_value", "price", "close")),
            "change": _safe_float(_bse_row_value(row, "change", "net_change", "change_value")),
            "change_pct": _safe_float(_bse_row_value(row, "pchange", "change_percent", "per_change")),
            "volume": _safe_float(_bse_row_value(row, "total_traded_volume", "total_volume", "volume")),
            "day_high": _safe_float(_bse_row_value(row, "day_high", "high")),
            "day_low": _safe_float(_bse_row_value(row, "day_low", "low")),
            "year_high": _safe_float(_bse_row_value(row, "year_high", "52_week_high")),
            "year_low": _safe_float(_bse_row_value(row, "year_low", "52_week_low")),
        })
    if not constituents:
        raise RuntimeError("BSE returned no constituent rows for this index.")
    constituents.sort(key=lambda item: item["weight"] if item["weight"] is not None else -1, reverse=True)
    return {
        "index": index_name,
        "source": "BSE India",
        "as_of": None,
        "constituents": constituents,
    }


def _safe_float(value):
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", "").removesuffix("%")
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


@global_markets_api.route("/index")
def index_detail_page():
    if "email" not in session:
        return redirect(url_for("logIn"))
    ticker = (request.args.get("ticker") or "").strip().upper()
    item = _index_by_ticker(ticker)
    if item is None:
        return render_template("error.html", code=404, message="Unknown index"), 404
    return render_template(
        "indexDetail.html",
        title=item["index"],
        index_ticker=ticker,
        index_name=item["index"],
        country=item["country"],
    )


@global_markets_api.route("/api/index/details")
def index_details():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    ticker = (request.args.get("ticker") or "").strip().upper()
    item = _index_by_ticker(ticker)
    if item is None:
        return jsonify({"error": "unknown index"}), 404
    quote_data = _fetch_quote(item)
    if quote_data is None:
        return jsonify({"error": "Index quote is currently unavailable."}), 502
    return jsonify({
        "index": item["index"],
        "ticker": ticker,
        "price": quote_data["price"],
        "prev_close": quote_data["prev_close"],
        "change": quote_data["change"],
        "change_pct": quote_data["change_pct"],
        "is_open": quote_data["is_open"],
        "session": quote_data["session"],
        "source": "Yahoo Finance",
        "updated_at": int(time.time()),
        "has_constituents": ticker in NSE_INDEX_NAMES or ticker in _BSE_INDEX_CODES,
    })


@global_markets_api.route("/api/index/history")
def index_history():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    ticker = (request.args.get("ticker") or "").strip().upper()
    if _index_by_ticker(ticker) is None:
        return jsonify({"error": "unknown index"}), 404
    chart_range = (request.args.get("range") or "1D").upper()
    ranges = {
        "1D": (1, "5m"),
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
        history = yfp.get_history(ticker, days=days, interval=interval)
        if history is None or history.empty:
            return jsonify({"ticker": ticker, "range": chart_range, "points": [], "available": False})
        latest_day = history.index[-1].strftime("%Y-%m-%d") if chart_range == "1D" else None
        points = []
        for timestamp, row in history.iterrows():
            if latest_day and timestamp.strftime("%Y-%m-%d") != latest_day:
                continue
            point = {
                "time": timestamp.isoformat(),
                "open": _safe_float(row.get("Open")),
                "high": _safe_float(row.get("High")),
                "low": _safe_float(row.get("Low")),
                "close": _safe_float(row.get("Close")),
                "volume": _safe_float(row.get("Volume")),
            }
            if point["close"] is not None:
                points.append(point)
        if not points:
            return jsonify({"ticker": ticker, "range": chart_range, "points": [], "available": False})
        first = points[0]["close"]
        last = points[-1]["close"]
        return jsonify({
            "ticker": ticker,
            "range": chart_range,
            "points": points,
            "available": True,
            "summary": {
                "change": round(last - first, 2),
                "change_pct": round((last - first) / first * 100, 2) if first else None,
                "low": min(point["low"] if point["low"] is not None else point["close"] for point in points),
                "high": max(point["high"] if point["high"] is not None else point["close"] for point in points),
                "volume": sum(point["volume"] or 0 for point in points),
            },
        })
    except Exception:
        log.exception("Index history failed for %s", ticker)
        return jsonify({"error": "Index history is temporarily unavailable."}), 502


@global_markets_api.route("/api/index/constituents")
def index_constituents():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    ticker = (request.args.get("ticker") or "").strip().upper()
    if _index_by_ticker(ticker) is None:
        return jsonify({"error": "unknown index"}), 404
    if ticker not in NSE_INDEX_NAMES and ticker not in _BSE_INDEX_CODES:
        return jsonify({"error": "Constituent weights are not available for this index from the configured exchange feed."}), 404
    try:
        return jsonify(_fetch_index_constituents(ticker))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502


def _fetch_quote(item: dict) -> dict | None:
    try:
        q = yfp.get_quote(item["ticker"])
    except Exception:
        return None
    if not q or q.get("price") is None:
        return None
    status = _market_status(item["ticker"])
    return {
        **item,
        "overview_only": item["country"] == "India" and item["ticker"] not in ("^NSEI", "^BSESN"),
        "price": float(q.get("price") or 0),
        "prev_close": float(q.get("prev_close") or 0),
        "change": float(q.get("change") or 0),
        "change_pct": float(q.get("change_pct") or 0),
        "is_open": status["is_open"],
        "session": status["session"],
    }


def _classify_sentiment(avg_pct: float, breadth_pct: float) -> dict:
    """Map an average change-pct + breadth into a friendly sentiment label.

    Also exposes a coarse `state` (BULLISH / BEARISH / NEUTRAL) for the UI
    badge, so users can see the market's mood at a glance.
    """
    if avg_pct >= 0.75 and breadth_pct >= 65:
        label, tone, emoji = "Strongly Bullish", "bull", "🔥"
    elif avg_pct >= 0.25:
        label, tone, emoji = "Risk-On", "bull", "🟢"
    elif avg_pct <= -0.75 and breadth_pct <= 35:
        label, tone, emoji = "Strongly Bearish", "bear", "🛑"
    elif avg_pct <= -0.25:
        label, tone, emoji = "Risk-Off", "bear", "🔴"
    else:
        label, tone, emoji = "Mixed / Neutral", "neutral", "⚖️"

    if tone == "bull":
        state = "BULLISH"
    elif tone == "bear":
        state = "BEARISH"
    else:
        state = "NEUTRAL"

    return {"label": label, "tone": tone, "emoji": emoji, "state": state,
            "avg_pct": round(avg_pct, 2), "breadth_pct": round(breadth_pct, 1)}


def _build_idea(items: list[dict], regions: dict, global_sent: dict) -> str:
    """Heuristic 'today's idea' callout for Indian traders."""
    if not items:
        return "Markets are quiet — wait for the open before placing fresh swing trades."

    # Best/worst region
    region_avg = {r: round(sum(x["change_pct"] for x in v) / len(v), 2)
                  for r, v in regions.items() if v}
    best_region = max(region_avg, key=region_avg.get) if region_avg else None
    worst_region = min(region_avg, key=region_avg.get) if region_avg else None

    # US is the gap-driver for India next morning
    us_items = [x for x in items if x["country"] == "United States"]
    us_avg = round(sum(x["change_pct"] for x in us_items) / len(us_items), 2) if us_items else 0.0

    in_items = [x for x in items if x["country"] == "India"]
    in_avg = round(sum(x["change_pct"] for x in in_items) / len(in_items), 2) if in_items else None

    parts = []
    parts.append(f"Global sentiment is **{global_sent['label']}** ({global_sent['avg_pct']:+.2f}% avg, "
                 f"{global_sent['breadth_pct']:.0f}% of indices green).")
    if best_region and worst_region and best_region != worst_region:
        parts.append(f"{best_region} is leading ({region_avg[best_region]:+.2f}%) "
                     f"while {worst_region} lags ({region_avg[worst_region]:+.2f}%).")
    if us_items:
        if us_avg >= 0.4:
            parts.append(f"US closed strong ({us_avg:+.2f}%) — expect a positive gap on Indian indices; "
                         f"prefer fading green opens in over-extended large-caps.")
        elif us_avg <= -0.4:
            parts.append(f"US sold off ({us_avg:+.2f}%) — Indian indices likely to gap down; "
                         f"watch defensives (FMCG, IT, Pharma) for relative strength.")
        else:
            parts.append(f"US closed flat ({us_avg:+.2f}%); Indian open should follow domestic cues.")
    if in_avg is not None:
        if in_avg > 0 and us_avg < 0:
            parts.append("India is outperforming a weak US — a sign of domestic flows; "
                         "stay long quality, avoid chasing.")
        elif in_avg < 0 and us_avg > 0:
            parts.append("India is underperforming a strong US — caution on swing longs; "
                         "wait for breadth to confirm.")
    return " ".join(parts)


def _build_payload() -> dict:
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fallback_item, q in zip(INDICES, ex.map(_fetch_quote, INDICES)):
            if q:
                items.append({**q, "data_available": True})
            else:
                status = _market_status(fallback_item["ticker"])
                items.append({
                    **fallback_item,
                    "overview_only": (
                        fallback_item["country"] == "India"
                        and fallback_item["ticker"] not in ("^NSEI", "^BSESN")
                    ),
                    "price": None,
                    "prev_close": None,
                    "change": None,
                    "change_pct": None,
                    "is_open": status["is_open"],
                    "session": status["session"],
                    "data_available": False,
                })

    available_items = [item for item in items if item["data_available"]]
    primary_items = [item for item in available_items if not item.get("overview_only")] or available_items
    avg_pct = (
        sum(x["change_pct"] for x in primary_items) / len(primary_items)
        if primary_items else 0.0
    )
    up = sum(1 for x in primary_items if x["change_pct"] > 0.05)
    down = sum(1 for x in primary_items if x["change_pct"] < -0.05)
    flat = sum(1 for x in primary_items if abs(x["change_pct"]) <= 0.05)
    breadth_pct = (up / len(primary_items)) * 100.0 if primary_items else 50.0
    sentiment = _classify_sentiment(avg_pct, breadth_pct)

    regions: dict[str, list[dict]] = {}
    for it in items:
        regions.setdefault(it["region"], []).append(it)
    primary_regions: dict[str, list[dict]] = {}
    for it in primary_items:
        primary_regions.setdefault(it["region"], []).append(it)
    region_blocks = []
    for r, lst in regions.items():
        sentiment_items = primary_regions.get(r) or [
            item for item in lst if item["data_available"]
        ]
        r_avg = (
            sum(x["change_pct"] for x in sentiment_items) / len(sentiment_items)
            if sentiment_items else None
        )
        r_up = sum(1 for x in sentiment_items if x["change_pct"] > 0.05)
        r_open = sum(1 for x in sentiment_items if x.get("is_open"))
        r_tone = "bull" if r_avg is not None and r_avg > 0.15 else (
            "bear" if r_avg is not None and r_avg < -0.15 else "neutral"
        )
        r_state = "BULLISH" if r_tone == "bull" else ("BEARISH" if r_tone == "bear" else "NEUTRAL")
        region_blocks.append({
            "name": r,
            "items": sorted(
                lst,
                key=lambda z: (
                    z["change_pct"] is not None,
                    z["change_pct"] if z["change_pct"] is not None else float("-inf"),
                ),
                reverse=True,
            ),
            "avg_pct": round(r_avg, 2) if r_avg is not None else None,
            "breadth_pct": round((r_up / len(sentiment_items)) * 100.0, 1) if sentiment_items else None,
            "tone": r_tone,
            "state": r_state,
            "open_count": r_open,
        })
    # Stable region order
    order = {"Asia-Pacific": 0, "Europe": 1, "Americas": 2}
    region_blocks.sort(key=lambda b: order.get(b["name"], 99))

    top_gainer = max(primary_items, key=lambda x: x["change_pct"]) if primary_items else None
    top_loser = min(primary_items, key=lambda x: x["change_pct"]) if primary_items else None

    idea = _build_idea(primary_items, primary_regions, sentiment)

    return {
        "ts": int(time.time()),
        "sentiment": sentiment,
        "regions": region_blocks,
        "stats": {
            "total": len(primary_items),
            "up": up,
            "down": down,
            "flat": flat,
            "open_count": sum(1 for x in primary_items if x.get("is_open")),
            "top_gainer": top_gainer,
            "top_loser": top_loser,
        },
        "idea": idea,
    }


def _load(force: bool = False) -> dict:
    if not force:
        cached = shared_cache.jget(CACHE_KEY)
        if cached:
            return cached
    with shared_cache.lock("globalmarkets:build", ttl=20, blocking=True, wait=4.0) as got:
        cached = shared_cache.jget(CACHE_KEY)
        if cached and not force:
            return cached
        if not got:
            return cached or _build_payload()
        data = _build_payload()
        shared_cache.jset(CACHE_KEY, data, ttl=CACHE_TTL)
        return data


@global_markets_api.route("/global-markets")
def global_markets_page():
    if "email" not in session:
        return redirect(url_for("logIn"))
    return render_template(
        "globalMarkets.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="Global Markets",
    )


@global_markets_api.route("/api/global-markets")
def global_markets_data():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    force = request.args.get("refresh") == "1"
    snapshot_only = request.args.get("snapshot") == "1"

    if snapshot_only and not force:
        data = snapshot_store.serve_snapshot("live:global_markets")
        if data is None:
            return jsonify({"snapshot_missing": True})
        return jsonify(data)

    payload = _load(force=force)
    try:
        snapshot_store.put("live:global_markets", payload)
    except Exception:
        pass
    return jsonify(payload)


_GLOBAL_ASSETS = {
    "commodities": [
        {"ticker": "GC=F", "name": "Gold", "country": "Metals", "precision": 2, "unit": "USD", "weight": 40},
        {"ticker": "CL=F", "name": "WTI Crude Oil", "country": "Energy", "precision": 2, "unit": "USD", "weight": 35},
        {"ticker": "SI=F", "name": "Silver", "country": "Metals", "precision": 2, "unit": "USD", "weight": 25},
        {"ticker": "NG=F", "name": "Natural Gas", "country": "Energy", "precision": 3, "unit": "USD", "weight": 20},
        {"ticker": "HG=F", "name": "Copper", "country": "Metals", "precision": 3, "unit": "USD", "weight": 18},
        {"ticker": "ZC=F", "name": "Corn", "country": "Agriculture", "precision": 2, "unit": "USD", "weight": 12},
        {"ticker": "ZW=F", "name": "Wheat", "country": "Agriculture", "precision": 2, "unit": "USD", "weight": 10},
    ],
    "forex": [
        {"ticker": "EURUSD=X", "name": "EUR / USD", "country": "Majors", "precision": 4, "unit": "", "weight": 40},
        {"ticker": "USDJPY=X", "name": "USD / JPY", "country": "Majors", "precision": 3, "unit": "", "weight": 35},
        {"ticker": "GBPUSD=X", "name": "GBP / USD", "country": "Majors", "precision": 4, "unit": "", "weight": 30},
        {"ticker": "USDINR=X", "name": "USD / INR", "country": "Asia", "precision": 2, "unit": "", "weight": 24},
        {"ticker": "AUDUSD=X", "name": "AUD / USD", "country": "Majors", "precision": 4, "unit": "", "weight": 20},
        {"ticker": "USDCNY=X", "name": "USD / CNY", "country": "Asia", "precision": 4, "unit": "", "weight": 18},
        {"ticker": "USDCAD=X", "name": "USD / CAD", "country": "Majors", "precision": 4, "unit": "", "weight": 16},
    ],
    "crypto": [
        {"ticker": "BTC-USD", "name": "Bitcoin", "country": "Crypto", "precision": 0, "unit": "USD", "weight": 50},
        {"ticker": "ETH-USD", "name": "Ethereum", "country": "Crypto", "precision": 2, "unit": "USD", "weight": 25},
        {"ticker": "BNB-USD", "name": "BNB", "country": "Crypto", "precision": 2, "unit": "USD", "weight": 8},
        {"ticker": "SOL-USD", "name": "Solana", "country": "Crypto", "precision": 2, "unit": "USD", "weight": 7},
        {"ticker": "XRP-USD", "name": "XRP", "country": "Crypto", "precision": 4, "unit": "USD", "weight": 5},
        {"ticker": "ADA-USD", "name": "Cardano", "country": "Crypto", "precision": 4, "unit": "USD", "weight": 3},
    ],
    "bonds": [
        {"ticker": "^TNX", "name": "US 10-Year Treasury", "country": "United States", "precision": 3, "unit": "%", "weight": 40},
        {"ticker": "^IRX", "name": "US 13-Week Treasury Bill", "country": "United States", "precision": 3, "unit": "%", "weight": 25},
        {"ticker": "^FVX", "name": "US 5-Year Treasury", "country": "United States", "precision": 3, "unit": "%", "weight": 20},
        {"ticker": "^TYX", "name": "US 30-Year Treasury", "country": "United States", "precision": 3, "unit": "%", "weight": 15},
    ],
}
_ASSET_BY_TICKER = {
    item["ticker"]: item
    for asset_items in _GLOBAL_ASSETS.values()
    for item in asset_items
}
_WORLD_BANK_HTTP = get_session("global-markets-world-bank")
_MACRO_COUNTRIES = [
    ("USA", "United States"),
    ("CHN", "China"),
    ("JPN", "Japan"),
    ("IND", "India"),
    ("DEU", "Germany"),
]
_MACRO_INDICATORS = [
    ("FR.INR.LEND", "Lending interest rate", "%"),
    ("FP.CPI.TOTL.ZG", "Inflation", "%"),
    ("NY.GDP.MKTP.KD.ZG", "GDP growth", "%"),
    ("SL.UEM.TOTL.ZS", "Unemployment", "%"),
]


def _fetch_asset_quote(item: dict) -> dict:
    try:
        quote_data = yfp.get_quote(item["ticker"])
    except Exception as exc:
        log.warning("Global asset quote failed for %s: %s", item["ticker"], exc)
        quote_data = None
    status = {
        "crypto": "24/7",
        "forex": "24h weekdays",
        "commodities": "Market hours vary",
        "bonds": "Market hours vary",
    }
    return {
        **item,
        "index": item["name"],
        "region": item["country"],
        "price": _safe_float((quote_data or {}).get("price")),
        "prev_close": _safe_float((quote_data or {}).get("prev_close")),
        "change_pct": _safe_float((quote_data or {}).get("change_pct")),
        "is_open": item.get("asset") == "crypto",
        "status": status.get(item.get("asset"), "Market hours vary"),
        "session": None,
    }


@global_markets_api.route("/api/global-markets/assets")
def global_markets_assets():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    asset = (request.args.get("asset") or "").strip().lower()
    if asset not in _GLOBAL_ASSETS:
        return jsonify({"error": "unsupported asset class"}), 400
    cache_key = f"globalmarkets:asset:{asset}:v1"
    cached = shared_cache.jget(cache_key)
    if isinstance(cached, dict) and time.time() - cached.get("ts", 0) < 60:
        return jsonify(cached)
    items = [dict(item, asset=asset) for item in _GLOBAL_ASSETS[asset]]
    with ThreadPoolExecutor(max_workers=8) as executor:
        quotes = list(executor.map(_fetch_asset_quote, items))
    result = {
        "asset": asset,
        "items": quotes,
        "source": "Yahoo Finance via yfinance; free indicative quotes, availability and licensing vary.",
        "ts": int(time.time()),
    }
    try:
        shared_cache.jset(cache_key, result, ttl=60)
    except Exception as exc:
        log.warning("Could not cache global %s quotes: %s", asset, exc)
    return jsonify(result)


def _fetch_market_history(ticker: str) -> dict:
    cache_key = f"globalmarkets:history:v1:{ticker}"
    cached = shared_cache.jget(cache_key)
    if isinstance(cached, dict) and cached.get("points"):
        return cached
    try:
        history = yfp.get_history(ticker, days=30, interval="1d")
        if history is None or history.empty:
            return {"ticker": ticker, "points": []}
        points = []
        for timestamp, row in history.iterrows():
            close = _safe_float(row.get("Close"))
            if close is not None:
                points.append({"time": timestamp.strftime("%Y-%m-%d"), "close": close})
        result = {"ticker": ticker, "points": points[-30:]}
        try:
            shared_cache.jset(cache_key, result, ttl=300)
        except Exception as exc:
            log.warning("Could not cache global market history for %s: %s", ticker, exc)
        return result
    except Exception as exc:
        log.warning("Global market history failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "points": []}


@global_markets_api.route("/api/global-markets/history")
def global_markets_history():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    requested = [ticker.strip().upper() for ticker in (request.args.get("tickers") or "").split(",") if ticker.strip()]
    if not requested or len(requested) > 30 or any(
        ticker not in _ASSET_BY_TICKER and _index_by_ticker(ticker) is None
        for ticker in requested
    ):
        return jsonify({"error": "invalid ticker list"}), 400
    tickers = list(dict.fromkeys(requested))
    with ThreadPoolExecutor(max_workers=6) as executor:
        items = list(executor.map(_fetch_market_history, tickers))
    return jsonify({"items": items, "source": "Yahoo Finance via yfinance", "ts": int(time.time())})


def _fetch_world_bank_indicator(indicator_id: str) -> dict[str, dict]:
    countries = ";".join(code for code, _ in _MACRO_COUNTRIES)
    url = (
        "https://api.worldbank.org/v2/country/"
        f"{countries}/indicator/{indicator_id}?format=json&per_page=500"
    )
    response = _WORLD_BANK_HTTP.get(url, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        raise RuntimeError(f"World Bank returned an unexpected response for {indicator_id}.")
    latest = {}
    for row in payload[1]:
        if not isinstance(row, dict):
            continue
        code = (row.get("countryiso3code") or "").upper()
        value = _safe_float(row.get("value"))
        year = str(row.get("date") or "")
        if value is not None and (code not in latest or year > latest[code]["year"]):
            latest[code] = {"value": value, "year": year}
    return latest


@global_markets_api.route("/api/global-markets/macro")
def global_markets_macro():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    cache_key = "globalmarkets:macro:v1"
    cached = shared_cache.jget(cache_key)
    if isinstance(cached, dict) and cached.get("indicators"):
        return jsonify(cached)

    indicators = []
    failures = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_fetch_world_bank_indicator, indicator_id): (indicator_id, name, unit)
            for indicator_id, name, unit in _MACRO_INDICATORS
        }
        for future, (indicator_id, name, unit) in futures.items():
            try:
                values = future.result()
            except Exception as exc:
                failures += 1
                log.warning("World Bank indicator %s unavailable: %s", indicator_id, exc)
                values = {}
            indicators.append({
                "id": indicator_id,
                "name": name,
                "unit": unit,
                "countries": [
                    {
                        "code": code,
                        "name": country,
                        "value": values.get(code, {}).get("value"),
                        "year": values.get(code, {}).get("year"),
                    }
                    for code, country in _MACRO_COUNTRIES
                ],
            })
    if failures == len(_MACRO_INDICATORS):
        return jsonify({"error": "World Bank macro data is temporarily unavailable."}), 502
    result = {
        "source": "Source: World Bank Open Data · annual/latest available observation",
        "indicators": indicators,
        "ts": int(time.time()),
    }
    try:
        shared_cache.jset(cache_key, result, ttl=21600)
    except Exception as exc:
        log.warning("Could not cache World Bank macro data: %s", exc)
    return jsonify(result)
