"""NSE corporate-announcements fetcher (best-effort, free, public endpoint).

NSE blocks unidentified clients; we send a browser-like User-Agent and
warm a session cookie. Failures are non-fatal — RSS news still flows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import requests

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}


def _session() -> Optional[requests.Session]:
    try:
        s = requests.Session()
        s.headers.update(_HEADERS)
        # Warm cookies
        s.get("https://www.nseindia.com/", timeout=10)
        return s
    except Exception as e:
        log.warning("[rag.nse] cookie warmup failed: %s", e)
        return None


def fetch_announcements(symbol: str, *, days_back: int = 30) -> List[dict]:
    """Return recent corporate announcements for an NSE symbol.

    Symbol must be the bare ticker (no '.NS').
    """
    sess = _session()
    if sess is None:
        return []

    bare = symbol.replace(".NS", "").replace(".BO", "")
    url = ("https://www.nseindia.com/api/corporate-announcements"
           f"?index=equities&symbol={bare}")
    try:
        r = sess.get(url, timeout=15)
    except Exception as e:
        log.warning("[rag.nse] %s request failed: %s", bare, e)
        return []

    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []

    rows = data if isinstance(data, list) else (data.get("data") or [])
    out = []
    for row in rows[:50]:
        try:
            subject = (row.get("desc") or row.get("subject") or row.get("title") or "").strip()
            details = (row.get("attchmntText") or row.get("description") or "").strip()
            if not subject:
                continue
            ts = row.get("an_dt") or row.get("date") or row.get("sm_indexName")
            try:
                published = datetime.strptime(ts, "%d-%b-%Y %H:%M:%S")
                published = published.replace(tzinfo=timezone.utc)
            except Exception:
                published = datetime.now(timezone.utc)
            attachment = row.get("attchmntFile") or row.get("attachmentFile") or ""
            out.append({
                "title": subject[:500],
                "content": (subject + "\n\n" + details)[:5000],
                "url": attachment if attachment.startswith("http") else "",
                "source": "NSE Filings",
                "doc_type": "filing",
                "published_at": published,
            })
        except Exception:
            continue
    return out
