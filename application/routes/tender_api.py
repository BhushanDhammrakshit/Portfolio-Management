"""Market news + sentiment classification (rule-based, no LLM)."""
import datetime
import re

import requests
from bs4 import BeautifulSoup
from flask import (Blueprint, render_template, session, redirect, url_for,
                   request, jsonify)

from application.services import cache as shared_cache

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def is_configured() -> bool:
    """Kept for template compatibility — the rule-based engine is always available."""
    return True


# ── Stock dictionary: alias variants → canonical name ──────────────────────
_STOCK_ALIASES = {
    "RELIANCE": ["reliance industries", "reliance ind", "ril ", "reliance"],
    "TCS": ["tata consultancy", "tcs"],
    "INFOSYS": ["infosys", "infy"],
    "HDFC BANK": ["hdfc bank", "hdfcbank"],
    "ICICI BANK": ["icici bank", "icicibank"],
    "SBI": ["state bank of india", "sbi ", " sbi"],
    "AXIS BANK": ["axis bank"],
    "KOTAK BANK": ["kotak mahindra", "kotak bank"],
    "BAJAJ FINANCE": ["bajaj finance", "bajfinance"],
    "BAJAJ FINSERV": ["bajaj finserv"],
    "BHARTI AIRTEL": ["bharti airtel", "airtel"],
    "ITC": ["itc ltd", "itc "],
    "HUL": ["hindustan unilever", "hul "],
    "L&T": ["larsen", "l&t"],
    "MARUTI": ["maruti suzuki", "maruti"],
    "TATA MOTORS": ["tata motors"],
    "TATA STEEL": ["tata steel"],
    "JSW STEEL": ["jsw steel"],
    "WIPRO": ["wipro"],
    "HCL TECH": ["hcl tech", "hcltech"],
    "TECH MAHINDRA": ["tech mahindra"],
    "ADANI ENTERPRISES": ["adani enterprises", "adani ent"],
    "ADANI PORTS": ["adani ports"],
    "ADANI GREEN": ["adani green"],
    "ADANI POWER": ["adani power"],
    "ASIAN PAINTS": ["asian paints"],
    "TITAN": ["titan company", "titan "],
    "SUN PHARMA": ["sun pharma"],
    "DR REDDY": ["dr reddy", "dr. reddy"],
    "CIPLA": ["cipla"],
    "DIVI'S LAB": ["divi's lab", "divis lab"],
    "ULTRATECH CEMENT": ["ultratech"],
    "AMBUJA CEMENT": ["ambuja"],
    "POWER GRID": ["power grid", "powergrid"],
    "NTPC": ["ntpc"],
    "ONGC": ["ongc"],
    "COAL INDIA": ["coal india"],
    "INDIAN OIL": ["indian oil", "ioc "],
    "BPCL": ["bpcl", "bharat petroleum"],
    "PNB": ["punjab national bank", "pnb "],
    "BANK OF BARODA": ["bank of baroda", "bankbaroda"],
    "CANARA BANK": ["canara bank", "canbk"],
    "INDUSIND BANK": ["indusind"],
    "IDFC FIRST BANK": ["idfc first"],
    "M&M": ["mahindra & mahindra", "mahindra and mahindra", "m&m"],
    "EICHER MOTORS": ["eicher"],
    "BAJAJ AUTO": ["bajaj auto"],
    "HERO MOTOCORP": ["hero motocorp"],
    "TVS MOTOR": ["tvs motor"],
    "ASHOK LEYLAND": ["ashok leyland"],
    "NESTLE": ["nestle"],
    "BRITANNIA": ["britannia"],
    "DABUR": ["dabur"],
    "GODREJ": ["godrej"],
    "DLF": ["dlf "],
    "ZOMATO": ["zomato"],
    "PAYTM": ["paytm"],
    "NYKAA": ["nykaa"],
    "HINDALCO": ["hindalco"],
    "VEDANTA": ["vedanta", "vedl"],
    "GAIL": ["gail "],
    "IRCTC": ["irctc"],
    "TATA CONSUMER": ["tata consumer"],
    "BIOCON": ["biocon"],
    "LUPIN": ["lupin"],
    "AUROBINDO PHARMA": ["aurobindo"],
    "TORRENT PHARMA": ["torrent pharma"],
    "NIFTY": ["nifty 50", "nifty50", "nifty"],
    "SENSEX": ["sensex", "bse"],
    "BANK NIFTY": ["bank nifty", "banknifty"],
}

# ── Sentiment lexicons ─────────────────────────────────────
BULLISH_WORDS = {
    "surge", "surged", "surges", "soar", "soars", "soared", "rally", "rallies",
    "rallied", "jump", "jumps", "jumped", "rise", "rises", "rose", "gain",
    "gains", "gained", "climb", "climbs", "climbed", "advance", "advances",
    "advanced", "boom", "booms", "spike", "spikes", "spiked", "outperform",
    "outperforms", "outperformed", "beat", "beats", "exceed", "exceeds",
    "exceeded", "buy", "upgrade", "upgrades", "upgraded", "bullish",
    "positive", "strong", "robust", "growth", "profit", "profits", "raise",
    "raised", "raises", "expand", "expands", "expansion", "acquisition",
    "acquire", "acquires", "bagged", "secures", "secured", "boost", "boosts",
    "milestone", "breakthrough", "approval", "approved", "upbeat", "dividend",
    "buyback", "rebound", "rebounds", "recovery", "recovers", "win", "wins",
    "won", "hike", "hiked", "hikes", "high", "highs", "top", "tops",
}

BEARISH_WORDS = {
    "fall", "falls", "fell", "drop", "drops", "dropped", "plunge", "plunges",
    "plunged", "crash", "crashes", "crashed", "slump", "slumps", "slumped",
    "tumble", "tumbles", "tumbled", "decline", "declines", "declined", "loss",
    "losses", "lose", "lost", "slip", "slips", "slipped", "underperform",
    "underperforms", "miss", "misses", "missed", "downgrade", "downgrades",
    "downgraded", "sell", "bearish", "negative", "weak", "weakness", "concern",
    "concerns", "warning", "warns", "cut", "cuts", "slashed", "slashes",
    "slash", "low", "lows", "fraud", "probe", "investigation", "raid",
    "penalty", "fine", "lawsuit", "downturn", "recession", "crisis",
    "default", "downside", "layoff", "layoffs", "shutdown", "scandal",
    "downbeat", "hit", "hits", "sink", "sinks", "sank",
}

MULTI_BULL = ("record high", "all-time high", "all time high", "52-week high",
              "52 week high", "earnings beat", "order win", "high demand",
              "positive outlook", "guidance raised")
MULTI_BEAR = ("record low", "52-week low", "52 week low", "all-time low",
              "all time low", "loss-making", "profit warning",
              "guidance cut", "warning issued")
NEGATION = {"not", "no", "never", "without", "avoid", "denies", "denied", "deny"}

# High-impact catalysts that historically move price sharply on the day of
# announcement. Presence of any of these earns the headline a "high conviction"
# tag. Single tokens are matched against the tokenized words; phrases against
# the raw lowercased text.
HIGH_IMPACT_BULL_TOKENS = {
    "buyback", "bonus", "merger", "acquisition", "acquires", "acquired",
    "approval", "approved", "upgrade", "upgraded", "raised",
}
HIGH_IMPACT_BEAR_TOKENS = {
    "fraud", "probe", "raid", "scandal", "default", "downgrade", "downgraded",
    "ban", "banned", "lawsuit", "penalty", "shutdown", "layoff", "layoffs",
    "resigns", "resignation", "investigation",
}
HIGH_IMPACT_BULL_PHRASES = (
    "record high", "all-time high", "52-week high", "earnings beat",
    "order win", "guidance raised", "stake buy", "block deal buy",
    "dividend declared", "buyback announced", "bagged order",
)
HIGH_IMPACT_BEAR_PHRASES = (
    "record low", "52-week low", "profit warning", "guidance cut",
    "block deal sell", "stake sale", "ceo resigns", "cfo resigns",
    "auditor resigns", "sebi probe", "income tax raid", "ed raid",
)

# Shared cache key for the rule-based stock summary payload.
# 5-minute global cache: the first user triggers a scrape+classify; every
# other user gets the cached payload until it goes stale, then the next
# request re-scrapes once.
_STOCK_SUMMARY_KEY = "tender:stock_summary"
CACHE_EXPIRY_SECONDS = 300  # 5 minutes

tender_api = Blueprint("tendor_api", __name__)


def get_cached_stock_summary():
    """Return (rows, timestamp) from the shared cache; both may be None."""
    payload = shared_cache.jget(_STOCK_SUMMARY_KEY)
    if not isinstance(payload, dict):
        return None, None
    return payload.get("rows"), payload.get("timestamp")


def set_cached_stock_summary(data):
    payload = {
        "rows": data,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    shared_cache.jset(_STOCK_SUMMARY_KEY, payload, ttl=CACHE_EXPIRY_SECONDS)


# Regex patterns for extracting time mentions near a headline
_REL_TIME_RE = re.compile(
    r"\b(\d+\s*(?:min|mins|minute|minutes|hr|hrs|hour|hours|day|days|week|weeks|month|months|year|years)\s*ago)\b"
    r"|\b(just\s+now|moments\s+ago|today|yesterday)\b",
    re.IGNORECASE,
)
# ISO-style date inside text (e.g. "May 8, 2026" or "08 May 2026")
_ABS_DATE_RE = re.compile(
    r"\b(?:\d{1,2}\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{1,2}(?:,?\s*\d{4})?(?:\s*[•·,]\s*\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)?)?\b"
)


def _parse_time_attr(value: str):
    """Parse an ISO-ish datetime string and return a relative-time string in IST."""
    if not value:
        return None
    value = value.strip()
    fmts = (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    )
    dt = None
    for f in fmts:
        try:
            dt = datetime.datetime.strptime(value, f)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    dt_ist = dt.astimezone(IST)
    now = datetime.datetime.now(IST)
    delta = now - dt_ist
    secs = int(delta.total_seconds())
    if secs < 0:
        return dt_ist.strftime("%d %b %Y, %I:%M %p IST")
    if secs < 60:
        return "Just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} hr ago"
    if secs < 604800:
        return f"{secs // 86400} day{'s' if secs // 86400 > 1 else ''} ago"
    return dt_ist.strftime("%d %b %Y")


def _extract_time_near(tag) -> str:
    """Walk up the DOM to find a publication time near the headline tag."""
    node = tag
    for _ in range(4):  # check up to 4 ancestor levels
        if node is None:
            break
        # Look for <time> elements with datetime attribute
        time_el = node.find("time") if hasattr(node, "find") else None
        if time_el:
            iso = time_el.get("datetime") or time_el.get("data-time") or ""
            parsed = _parse_time_attr(iso)
            if parsed:
                return parsed
            txt = (time_el.get_text() or "").strip()
            if txt:
                return re.sub(r"\s+", " ", txt)[:40]
        # Look for elements with class hints suggesting time
        if hasattr(node, "find_all"):
            for cand in node.find_all(
                attrs={"class": re.compile(r"(time|date|publish|posted)", re.I)},
                limit=3,
            ):
                txt = (cand.get_text() or "").strip()
                if txt and len(txt) < 60:
                    return re.sub(r"\s+", " ", txt)[:40]
        # Look for relative-time or date patterns in nearby text
        nearby_text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""
        if nearby_text and len(nearby_text) < 600:
            m = _REL_TIME_RE.search(nearby_text)
            if m:
                return (m.group(1) or m.group(2)).strip().capitalize()
            m = _ABS_DATE_RE.search(nearby_text)
            if m:
                return m.group(0).strip()
        node = node.parent if hasattr(node, "parent") else None
    return ""


def _fetch_news_headlines() -> list:
    """Fetch headlines from multiple sources. Returns list of (text, time_str) tuples."""
    sources = [
        ("https://economictimes.indiatimes.com/markets/stocks/news",
         {"User-Agent": "Mozilla/5.0"}),
        ("https://www.moneycontrol.com/news/business/markets/",
         {"User-Agent": "Mozilla/5.0"}),
        ("https://www.business-standard.com/markets/news",
         {"User-Agent": "Mozilla/5.0"}),
        ("https://www.livemint.com/market/stock-market-news",
         {"User-Agent": "Mozilla/5.0"}),
    ]
    all_heads = []
    seen = set()
    for url, headers in sources:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup.find_all(["h1", "h2", "h3", "a"], limit=400):
                txt = (tag.get_text() or "").strip()
                txt = re.sub(r"\s+", " ", txt)
                if 25 <= len(txt) <= 240 and not txt.lower().startswith("http"):
                    if txt not in seen:
                        seen.add(txt)
                        time_str = _extract_time_near(tag)
                        all_heads.append((txt, time_str))
        except requests.RequestException:
            continue
    return all_heads


def _detect_stock(headline_lower: str):
    """Return canonical stock name for the first alias matched in headline, else None."""
    for canonical, aliases in _STOCK_ALIASES.items():
        for a in aliases:
            if a in headline_lower:
                return canonical
    return None


def _classify_sentiment(headline_lower: str):
    """Score a headline and return a dict:
        { sentiment: 'bullish'|'bearish'|'neutral',
          score: int (overall conviction strength, higher = stronger),
          conviction: 'high'|'normal',
          catalysts: list[str] (matched high-impact triggers, for tooltip) }
    """
    bull = 0
    bear = 0
    catalysts = []

    # Multi-word phrases first (weighted x2)
    for phrase in MULTI_BULL:
        if phrase in headline_lower:
            bull += 2
    for phrase in MULTI_BEAR:
        if phrase in headline_lower:
            bear += 2

    # Tokenize and score with simple negation lookback
    tokens = re.findall(r"[a-z][a-z'&-]*", headline_lower)
    for i, tok in enumerate(tokens):
        prev = tokens[i - 1] if i > 0 else ""
        prev2 = tokens[i - 2] if i > 1 else ""
        negated = prev in NEGATION or prev2 in NEGATION
        if tok in BULLISH_WORDS:
            bear += 1 if negated else 0
            bull += 0 if negated else 1
        elif tok in BEARISH_WORDS:
            bull += 1 if negated else 0
            bear += 0 if negated else 1

    # High-impact catalyst detection
    high_bull = False
    high_bear = False
    for phrase in HIGH_IMPACT_BULL_PHRASES:
        if phrase in headline_lower:
            high_bull = True
            catalysts.append(phrase)
    for phrase in HIGH_IMPACT_BEAR_PHRASES:
        if phrase in headline_lower:
            high_bear = True
            catalysts.append(phrase)
    token_set = set(tokens)
    for t in HIGH_IMPACT_BULL_TOKENS & token_set:
        high_bull = True
        catalysts.append(t)
    for t in HIGH_IMPACT_BEAR_TOKENS & token_set:
        high_bear = True
        catalysts.append(t)

    if bull > bear and bull >= 1:
        sentiment = "bullish"
        score = bull - bear
    elif bear > bull and bear >= 1:
        sentiment = "bearish"
        score = bear - bull
    else:
        sentiment = "neutral"
        score = 0

    # Conviction rules:
    #  • Any high-impact catalyst that aligns with sentiment → high
    #  • Net score >= 3 (e.g. multi-word phrase + supporting word) → high
    aligned_catalyst = (sentiment == "bullish" and high_bull) or \
                       (sentiment == "bearish" and high_bear)
    conviction = "high" if (aligned_catalyst or score >= 3) else "normal"
    if sentiment == "neutral":
        conviction = "normal"

    return {
        "sentiment": sentiment,
        "score": score,
        "conviction": conviction,
        "catalysts": list(dict.fromkeys(catalysts))[:3],
    }


def fetch_and_parse_stock_news():
    """Scrape news sources and classify each headline by stock + sentiment."""
    headlines = _fetch_news_headlines()
    if not headlines:
        return []

    fallback_time = datetime.datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    seen = set()  # de-dupe by (stock, summary[:60])
    rows = []

    for headline, pub_time in headlines:
        text = headline.strip()
        if len(text) < 25:
            continue
        lower = text.lower()
        stock = _detect_stock(lower)
        if not stock:
            continue
        cls = _classify_sentiment(lower)
        key = (stock, lower[:60])
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "stock": stock,
            "sentiment": cls["sentiment"],
            "score": cls["score"],
            "conviction": cls["conviction"],
            "catalysts": cls["catalysts"],
            "summary": text,
            "time": pub_time or fallback_time,
        })
        if len(rows) >= 80:
            break

    # Sort: high-conviction first, then by score desc, then keep insertion order
    rows.sort(key=lambda r: (
        0 if r["conviction"] == "high" else 1,
        -r.get("score", 0),
    ))
    return rows


def fetch_stock_news_summary(force=False):
    if not force:
        cached_rows, _ = get_cached_stock_summary()
        if cached_rows is not None:
            return cached_rows
    try:
        rows = fetch_and_parse_stock_news()
    except Exception as e:
        print(f"[tenders] error: {e}")
        rows = []
    set_cached_stock_summary(rows)
    return rows


@tender_api.route("/tenders")
def show_stock_news():
    if "email" not in session:
        return redirect(url_for("logIn"))
    force = request.args.get("refresh") == "1"
    stock_data = fetch_stock_news_summary(force=force)
    _, last_updated_iso = get_cached_stock_summary()
    last_updated_str = None
    if last_updated_iso:
        try:
            dt = datetime.datetime.fromisoformat(last_updated_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            last_updated_str = dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
        except (ValueError, TypeError):
            last_updated_str = None
    return render_template(
        "tenders.html",
        stocks=stock_data or [],
        ai_configured=is_configured(),
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="News & Trends",
        last_updated=last_updated_str,
    )


@tender_api.route("/api/tenders/refresh", methods=["POST"])
def refresh_news():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    data = fetch_stock_news_summary(force=True)
    return jsonify({"stocks": data, "count": len(data)})
