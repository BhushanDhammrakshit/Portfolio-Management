"""Build a citation-ready RAG context block for a given symbol.

Strategy:
    1. Pull recent docs from the store (last 90d, newest first)
    2. If embeddings + a query are available, rank by cosine similarity
       to the query embedding; otherwise return newest-first
    3. Format top-N as a numbered SOURCES block with publication dates
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from . import embeddings, store
from .symbols import canonicalize, display_name

log = logging.getLogger(__name__)


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _age_label(published_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
    except Exception:
        return "unknown date"
    now = datetime.now(timezone.utc)
    delta = now - dt
    if delta.days >= 1:
        return f"{delta.days}d ago"
    hrs = int(delta.total_seconds() // 3600)
    if hrs >= 1:
        return f"{hrs}h ago"
    return "just now"


def retrieve(symbol: str, *, query: Optional[str] = None,
             top_k: int = 6, days_back: int = 90) -> List[dict]:
    """Return top-k relevant docs for a symbol (sorted by relevance)."""
    docs = store.list_docs(symbol, days_back=days_back, limit=80)
    if not docs:
        return []

    # If no query or no embeddings configured, return newest-first
    if not query or not embeddings.is_configured():
        return docs[:top_k]

    qvec = embeddings.embed_one(query)
    if not qvec:
        return docs[:top_k]

    embed_map = store.get_embeddings(symbol, [d["RowKey"] for d in docs])
    if not embed_map:
        return docs[:top_k]

    scored: List[Tuple[float, dict]] = []
    for d in docs:
        v = embed_map.get(d["RowKey"])
        score = _cosine(qvec, v) if v else 0.0
        # Recency boost: ~0.05 per recent week
        try:
            age_days = (datetime.now(timezone.utc) -
                        datetime.fromisoformat(
                            d.get("PublishedAt", "").replace("Z", "+00:00"))
                        ).days
            recency = max(0.0, 0.10 - 0.005 * age_days)
        except Exception:
            recency = 0.0
        scored.append((score + recency, d))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:top_k]]


def build_context(symbol: str, *, query: Optional[str] = None,
                  top_k: int = 6) -> Tuple[str, List[dict]]:
    """Return (prompt_block, sources_list) for the given symbol.

    sources_list shape (for UI rendering):
        [{"n": 1, "title": ..., "source": ..., "url": ..., "age": "2d ago",
          "published_at": "2026-05-08T..."}, ...]
    """
    canon = canonicalize(symbol)
    docs = retrieve(canon, query=query, top_k=top_k)

    if not docs:
        block = (
            "SOURCES:\n"
            f"(No recent news or filings indexed for {display_name(canon)}. "
            "Base your answer on general knowledge but say so explicitly.)"
        )
        return block, []

    lines = ["SOURCES:"]
    sources = []
    for i, d in enumerate(docs, start=1):
        title = d.get("Title", "").strip()
        content = (d.get("Content", "") or "")[:1200]
        src = d.get("Source", "")
        url = d.get("Url", "")
        pub = d.get("PublishedAt", "")
        age = _age_label(pub)
        lines.append(f"\n[Source {i}] {src} \u2014 {age}")
        lines.append(f"Title: {title}")
        lines.append(f"Excerpt: {content}")
        sources.append({
            "n": i,
            "title": title,
            "source": src,
            "url": url,
            "age": age,
            "published_at": pub,
            "doc_type": d.get("DocType", "news"),
        })

    return "\n".join(lines), sources
