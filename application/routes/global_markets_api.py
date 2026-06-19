"""Global Markets — major-country index heatmap with regional sentiment.

Uses yfinance directly (via the project's `yfinance_provider`) since the
broker providers only cover Indian symbols. Cached for ~1 minute so the
"LIVE" session indicator flips on/off close to real time.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from application.services import cache as shared_cache
from application.services.providers import yfinance_provider as yfp

global_markets_api = Blueprint("global_markets_api", __name__)

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


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _market_status(ticker: str) -> dict:
    """Return current open/closed info for `ticker` based on local clock."""
    sess = _SESSIONS.get(ticker)
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

CACHE_KEY = "globalmarkets:v1"
CACHE_TTL = 60  # 1 min — keeps LIVE indicator close to real-time


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
        for q in ex.map(_fetch_quote, INDICES):
            if q:
                items.append(q)

    if not items:
        return {"sectors": [], "regions": [], "ts": int(time.time()),
                "sentiment": _classify_sentiment(0.0, 50.0),
                "idea": "Live data is briefly unavailable — try again in a moment.",
                "stats": {"total": 0, "up": 0, "down": 0, "flat": 0,
                          "top_gainer": None, "top_loser": None}}

    avg_pct = sum(x["change_pct"] for x in items) / len(items)
    up = sum(1 for x in items if x["change_pct"] > 0.05)
    down = sum(1 for x in items if x["change_pct"] < -0.05)
    flat = len(items) - up - down
    breadth_pct = (up / len(items)) * 100.0
    sentiment = _classify_sentiment(avg_pct, breadth_pct)

    regions: dict[str, list[dict]] = {}
    for it in items:
        regions.setdefault(it["region"], []).append(it)
    region_blocks = []
    for r, lst in regions.items():
        r_avg = sum(x["change_pct"] for x in lst) / len(lst)
        r_up = sum(1 for x in lst if x["change_pct"] > 0.05)
        r_open = sum(1 for x in lst if x.get("is_open"))
        r_tone = "bull" if r_avg > 0.15 else ("bear" if r_avg < -0.15 else "neutral")
        r_state = "BULLISH" if r_tone == "bull" else ("BEARISH" if r_tone == "bear" else "NEUTRAL")
        region_blocks.append({
            "name": r,
            "items": sorted(lst, key=lambda z: -z["change_pct"]),
            "avg_pct": round(r_avg, 2),
            "breadth_pct": round((r_up / len(lst)) * 100.0, 1),
            "tone": r_tone,
            "state": r_state,
            "open_count": r_open,
        })
    # Stable region order
    order = {"Asia-Pacific": 0, "Europe": 1, "Americas": 2}
    region_blocks.sort(key=lambda b: order.get(b["name"], 99))

    top_gainer = max(items, key=lambda x: x["change_pct"])
    top_loser = min(items, key=lambda x: x["change_pct"])

    idea = _build_idea(items, regions, sentiment)

    return {
        "ts": int(time.time()),
        "sentiment": sentiment,
        "regions": region_blocks,
        "stats": {
            "total": len(items),
            "up": up,
            "down": down,
            "flat": flat,
            "open_count": sum(1 for x in items if x.get("is_open")),
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
    return jsonify(_load(force=force))
