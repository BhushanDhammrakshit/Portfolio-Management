"""Fetch RSS feeds, normalize entries, filter by target symbols."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List

try:
    import feedparser  # type: ignore
except ImportError:  # pragma: no cover - optional dep
    feedparser = None  # type: ignore[assignment]

from .rss_sources import RSS_FEEDS

log = logging.getLogger(__name__)

# Strip HTML tags for plain-text content
_HTML_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _HTML_RE.sub(" ", s).replace("&nbsp;", " ").strip()


def _published_dt(entry) -> datetime:
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        v = getattr(entry, k, None) or entry.get(k) if isinstance(entry, dict) else None
        if v:
            try:
                return datetime(*v[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _matches_terms(text: str, terms: Iterable[str]) -> bool:
    """Case-insensitive whole-word match for any of the given terms."""
    if not text:
        return False
    low = text.lower()
    for t in terms:
        t = t.strip().lower()
        if not t:
            continue
        # Whole-word boundary; tolerate '&' and '-' inside ticker
        pat = r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])"
        if re.search(pat, low):
            return True
    return False


def fetch_all_entries(timeout: int = 20) -> List[dict]:
    """Pull every RSS entry from all feeds. Each item is a normalized dict."""
    if feedparser is None:
        log.warning("[rag.rss] feedparser not installed; skipping RSS ingest")
        return []
    items = []
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed["url"])
        except Exception as e:
            log.warning("[rag.rss] %s parse failed: %s", feed["name"], e)
            continue
        for e in parsed.entries:
            try:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                summary = _strip_html(e.get("summary") or e.get("description") or "")
                content = title + "\n\n" + summary
                items.append({
                    "title": title,
                    "content": content,
                    "url": e.get("link") or "",
                    "source": feed["source"],
                    "doc_type": "news",
                    "published_at": _published_dt(e),
                })
            except Exception:
                continue
    log.info("[rag.rss] fetched %d entries from %d feeds", len(items), len(RSS_FEEDS))
    return items


def filter_for_symbol(items: List[dict], terms: Iterable[str]) -> List[dict]:
    """Return only items whose title or summary mentions any term."""
    out = []
    for it in items:
        if _matches_terms(it["title"], terms) or _matches_terms(it["content"], terms):
            out.append(it)
    return out


def fetch_for_symbols(symbol_terms: Dict[str, List[str]]) -> Dict[str, List[dict]]:
    """One pass over all feeds, then bucket items per symbol.

    symbol_terms: {canonical_symbol: [search_terms, ...]}
    Returns:     {canonical_symbol: [item, ...]}
    """
    all_items = fetch_all_entries()
    out: Dict[str, List[dict]] = {sym: [] for sym in symbol_terms}
    for sym, terms in symbol_terms.items():
        out[sym] = filter_for_symbol(all_items, terms)
    return out
