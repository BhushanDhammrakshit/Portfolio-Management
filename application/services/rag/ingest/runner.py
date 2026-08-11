"""Daily ingestion orchestrator.

Pulls news + filings for a list of symbols, embeds them, writes to store.
Idempotent: re-running on the same day skips already-stored items by RowKey.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .. import embeddings, store
from ..symbols import canonicalize, display_name, search_terms
from . import nse_filings, rss_fetcher

log = logging.getLogger(__name__)


def _user_held_symbols() -> List[str]:
    """Distinct canonical symbols from all users' portfolios."""
    try:
        from application.services.azure_table import stocks_table_client
        seen = set()
        for s in stocks_table_client.list_entities():
            sym = s.get("Symbol") or s.get("StockName") or ""
            if sym:
                seen.add(canonicalize(sym))
        return sorted(s for s in seen if s)
    except Exception as e:
        log.warning("[rag.runner] could not enumerate user holdings: %s", e)
        return []


def run_for_symbols(symbols: List[str], *, embed_new: bool = True) -> Dict[str, dict]:
    """Ingest news + filings for the given symbols. Returns per-symbol stats."""
    if not store.is_ready():
        log.warning("[rag.runner] store not ready, aborting")
        return {}

    canon = [canonicalize(s) for s in symbols if s]
    canon = sorted({s for s in canon if s})
    if not canon:
        return {}

    log.info("[rag.runner] starting ingest for %d symbols", len(canon))

    # 1) One pass over RSS for all symbols
    sym_terms = {sym: search_terms(sym) for sym in canon}
    rss_buckets = rss_fetcher.fetch_for_symbols(sym_terms)

    stats: Dict[str, dict] = {}
    embed_can = embed_new and embeddings.is_configured()

    for sym in canon:
        added = 0
        skipped = 0
        items = list(rss_buckets.get(sym, []))

        # 2) NSE corporate filings (per-symbol, may fail silently)
        try:
            items.extend(nse_filings.fetch_announcements(display_name(sym)))
        except Exception as e:
            log.debug("[rag.runner] NSE skipped for %s: %s", sym, e)

        if not items:
            stats[sym] = {"added": 0, "skipped": 0, "embedded": 0}
            store.record_ingest(sym, 0)
            continue

        # 3) Build row keys; skip already-stored
        new_items = []
        for it in items:
            rk = store.make_row_key(it["published_at"], it["source"], it["title"])
            it["_row_key"] = rk
            if store.doc_exists(sym, rk):
                skipped += 1
                continue
            new_items.append(it)

        # 4) Embed (batched) — optional
        embedded = 0
        embed_map: Dict[str, list] = {}
        if embed_can and new_items:
            BATCH = 16
            for i in range(0, len(new_items), BATCH):
                chunk = new_items[i:i + BATCH]
                vecs = embeddings.embed([c["content"] for c in chunk])
                if not vecs or len(vecs) != len(chunk):
                    continue
                for it, v in zip(chunk, vecs):
                    embed_map[it["_row_key"]] = v
                    embedded += 1

        # 5) Persist
        for it in new_items:
            store.upsert_doc(
                symbol=sym,
                row_key=it["_row_key"],
                title=it["title"],
                content=it["content"],
                url=it["url"],
                source=it["source"],
                doc_type=it["doc_type"],
                published_at=it["published_at"],
                embedding=embed_map.get(it["_row_key"]),
            )
            added += 1

        store.record_ingest(sym, added)
        stats[sym] = {"added": added, "skipped": skipped, "embedded": embedded}
        log.info("[rag.runner] %s: +%d new, %d dup, %d embedded",
                 sym, added, skipped, embedded)

    return stats


def run_daily(extra_symbols: Optional[List[str]] = None) -> Dict[str, dict]:
    """Daily job entry point. Ingests every symbol any user holds + extras."""
    syms = _user_held_symbols()
    if extra_symbols:
        syms = sorted(set(syms) | {canonicalize(s) for s in extra_symbols if s})
    if not syms:
        log.info("[rag.runner] no symbols to ingest")
        return {}
    return run_for_symbols(syms)
