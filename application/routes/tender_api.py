"""Market news + sentiment classification."""
import datetime
import json
import re

import requests
from bs4 import BeautifulSoup
from flask import (Blueprint, render_template, session, redirect, url_for,
                   request, jsonify)

from application.constants import SYSTEM_PROMPT
from application.services.ai_client import chat as ai_chat, is_configured

# In-memory cache
_stock_summary_cache = {"data": None, "timestamp": None}
CACHE_EXPIRY_SECONDS = 3600  # 1 hour

tender_api = Blueprint("tendor_api", __name__)


def get_cached_stock_summary():
    now = datetime.datetime.utcnow()
    ts = _stock_summary_cache["timestamp"]
    if (_stock_summary_cache["data"] is not None and ts is not None
            and (now - ts).total_seconds() < CACHE_EXPIRY_SECONDS):
        return _stock_summary_cache["data"]
    return None


def set_cached_stock_summary(data):
    _stock_summary_cache["data"] = data
    _stock_summary_cache["timestamp"] = datetime.datetime.utcnow()


def _fetch_news_text() -> str:
    """Try multiple sources, return concatenated headline text."""
    sources = [
        ("https://economictimes.indiatimes.com/markets/stocks/news",
         {"User-Agent": "Mozilla/5.0"}),
        ("https://www.moneycontrol.com/news/business/markets/",
         {"User-Agent": "Mozilla/5.0"}),
        ("https://www.business-standard.com/markets/news",
         {"User-Agent": "Mozilla/5.0"}),
    ]
    chunks = []
    for url, headers in sources:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            # Collect headlines from common tags
            heads = []
            for tag in soup.find_all(["h1", "h2", "h3", "a"], limit=400):
                txt = (tag.get_text() or "").strip()
                if 25 <= len(txt) <= 220 and not txt.lower().startswith("http"):
                    heads.append(txt)
            if heads:
                chunks.append("Source: " + url + "\n" + "\n".join(heads[:80]))
        except requests.RequestException:
            continue
    return "\n\n".join(chunks)[:8000]


def fetch_and_parse_stock_news():
    text_content = _fetch_news_text()
    if not text_content:
        return []

    if not is_configured():
        return []

    content, err = ai_chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text_content},
        ],
        temperature=0.5, max_tokens=1500, timeout=60,
    )
    if err or not content:
        print(f"[tenders] AI error: {err}")
        return []

    # Try to extract JSON array
    parsed_data = None
    try:
        parsed_data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\[\s*\{.*\}\s*\]", content, re.S)
        if m:
            try:
                parsed_data = json.loads(m.group(0))
            except Exception:
                parsed_data = None
    if not isinstance(parsed_data, list):
        return []

    rows = []
    for item in parsed_data:
        if not isinstance(item, dict):
            continue
        stock = item.get("stock") or item.get("Stock") or item.get("name")
        sentiment = (item.get("classification") or item.get("sentiment")
                     or "neutral")
        reason = (item.get("reason") or item.get("summary")
                  or item.get("description") or "")
        if stock and reason:
            rows.append({
                "stock": str(stock).strip(),
                "sentiment": str(sentiment).strip().lower(),
                "summary": str(reason).strip(),
            })
    return rows


def fetch_stock_news_summary(force=False):
    if not force:
        cached = get_cached_stock_summary()
        if cached is not None:
            return cached
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
    last_updated = _stock_summary_cache.get("timestamp")
    return render_template(
        "tenders.html",
        stocks=stock_data or [],
        ai_configured=is_configured(),
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="Market News",
        last_updated=last_updated.strftime("%d %b %Y, %H:%M UTC")
        if last_updated else None,
    )


@tender_api.route("/api/tenders/refresh", methods=["POST"])
def refresh_news():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    data = fetch_stock_news_summary(force=True)
    return jsonify({"stocks": data, "count": len(data)})
