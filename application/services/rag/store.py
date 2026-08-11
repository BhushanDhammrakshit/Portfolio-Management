"""Azure Tables-backed RAG document + embedding store.

Schema:
    RagDocs   PartitionKey=symbol  RowKey=<isodate>_<source>_<hash8>
              Title, Content (<=30KB), Url, Source, DocType, PublishedAt, IngestedAt
    RagEmbed  PartitionKey=symbol  RowKey=same as RagDocs RowKey
              EmbeddingJson, Model, Dim
    RagMeta   PartitionKey="_global"  RowKey=last_ingest_<symbol>
              Timestamp, DocsAdded
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableServiceClient, UpdateMode

from application.config import (AZURE_TABLE_CONN_STR, RAG_DOCS_TABLE,
                                RAG_EMBED_TABLE, RAG_META_TABLE)
from .symbols import safe_partition_key

log = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 30000  # leave headroom under 64KB property limit
_GLOBAL_PK = "_global"

# ── Lazy table client init ──────────────────────────────────────────────
_docs_client = None
_embed_client = None
_meta_client = None
_init_attempted = False


def _init():
    global _docs_client, _embed_client, _meta_client, _init_attempted
    if _init_attempted:
        return
    _init_attempted = True
    if not AZURE_TABLE_CONN_STR:
        log.warning("[rag.store] AZURE_TABLE_CONN_STR missing; RAG store disabled.")
        return
    try:
        svc = TableServiceClient.from_connection_string(conn_str=AZURE_TABLE_CONN_STR)
        for name in (RAG_DOCS_TABLE, RAG_EMBED_TABLE, RAG_META_TABLE):
            try:
                svc.create_table_if_not_exists(table_name=name)
            except ResourceExistsError:
                pass
            except Exception as e:
                log.warning("[rag.store] create %s failed: %s", name, e)
        _docs_client = svc.get_table_client(table_name=RAG_DOCS_TABLE)
        _embed_client = svc.get_table_client(table_name=RAG_EMBED_TABLE)
        _meta_client = svc.get_table_client(table_name=RAG_META_TABLE)
        log.info("[rag.store] tables ready: %s, %s, %s",
                 RAG_DOCS_TABLE, RAG_EMBED_TABLE, RAG_META_TABLE)
    except Exception as e:
        log.warning("[rag.store] init failed: %s", e)


def is_ready() -> bool:
    _init()
    return _docs_client is not None


# ── Helpers ─────────────────────────────────────────────────────────────

def _hash8(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:8]


def make_row_key(published_at: datetime, source: str, title: str) -> str:
    """Stable, sortable RowKey: '20260510_173000_mint_a3f9b2c1'."""
    ts = published_at.strftime("%Y%m%d_%H%M%S")
    src = (source or "unk").lower().replace(" ", "")[:12]
    return f"{ts}_{src}_{_hash8(title or '')}"


def doc_exists(symbol: str, row_key: str) -> bool:
    _init()
    if _docs_client is None:
        return False
    try:
        _docs_client.get_entity(safe_partition_key(symbol), row_key)
        return True
    except Exception:
        return False


def upsert_doc(*, symbol: str, row_key: str, title: str, content: str,
               url: str, source: str, doc_type: str,
               published_at: datetime, embedding: Optional[List[float]] = None,
               sentiment: Optional[float] = None) -> None:
    """Insert or replace a document + its embedding (if provided)."""
    _init()
    if _docs_client is None:
        return

    pk = safe_partition_key(symbol)
    now = datetime.now(timezone.utc).isoformat()
    pub_iso = published_at.astimezone(timezone.utc).isoformat() if published_at else now

    doc_entity = {
        "PartitionKey": pk,
        "RowKey": row_key,
        "Title": (title or "")[:1000],
        "Content": (content or "")[:_MAX_CONTENT_CHARS],
        "Url": (url or "")[:1000],
        "Source": (source or "")[:64],
        "DocType": (doc_type or "news")[:32],
        "PublishedAt": pub_iso,
        "IngestedAt": now,
    }
    if sentiment is not None:
        doc_entity["Sentiment"] = float(sentiment)

    try:
        _docs_client.upsert_entity(doc_entity, mode=UpdateMode.REPLACE)
    except Exception as e:
        log.warning("[rag.store] upsert doc failed (%s/%s): %s", pk, row_key, e)
        return

    if embedding:
        embed_entity = {
            "PartitionKey": pk,
            "RowKey": row_key,
            "EmbeddingJson": json.dumps(embedding, separators=(",", ":")),
            "Model": "text-embedding-3-small",
            "Dim": len(embedding),
        }
        try:
            _embed_client.upsert_entity(embed_entity, mode=UpdateMode.REPLACE)
        except Exception as e:
            log.warning("[rag.store] upsert embed failed (%s/%s): %s",
                        pk, row_key, e)


def list_docs(symbol: str, *, days_back: int = 90, limit: int = 200) -> List[dict]:
    """Fetch docs for a symbol within the lookback window. Newest first."""
    _init()
    if _docs_client is None:
        return []
    pk = safe_partition_key(symbol)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    try:
        flt = f"PartitionKey eq '{pk}' and PublishedAt ge '{cutoff}'"
        rows = list(_docs_client.query_entities(query_filter=flt))
    except Exception as e:
        log.warning("[rag.store] list_docs %s failed: %s", pk, e)
        return []
    rows.sort(key=lambda r: r.get("PublishedAt", ""), reverse=True)
    return [dict(r) for r in rows[:limit]]


def get_embeddings(symbol: str, row_keys: Iterable[str]) -> dict:
    """Bulk-fetch embeddings for the given row keys. Returns {row_key: vector}."""
    _init()
    if _embed_client is None:
        return {}
    pk = safe_partition_key(symbol)
    out = {}
    for rk in row_keys:
        try:
            ent = _embed_client.get_entity(pk, rk)
            blob = ent.get("EmbeddingJson")
            if blob:
                out[rk] = json.loads(blob)
        except Exception:
            continue
    return out


def record_ingest(symbol: str, docs_added: int) -> None:
    _init()
    if _meta_client is None:
        return
    try:
        _meta_client.upsert_entity({
            "PartitionKey": _GLOBAL_PK,
            "RowKey": f"last_ingest_{safe_partition_key(symbol)}",
            "Timestamp": datetime.now(timezone.utc).isoformat(),
            "DocsAdded": int(docs_added),
        }, mode=UpdateMode.REPLACE)
    except Exception as e:
        log.warning("[rag.store] record_ingest failed: %s", e)


def last_ingest(symbol: str) -> Optional[dict]:
    _init()
    if _meta_client is None:
        return None
    try:
        ent = _meta_client.get_entity(
            _GLOBAL_PK, f"last_ingest_{safe_partition_key(symbol)}")
        return dict(ent)
    except Exception:
        return None


def cleanup_old(days: int = 90) -> int:
    """Delete RagDocs + RagEmbed rows older than `days`. Returns count."""
    _init()
    if _docs_client is None:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    deleted = 0
    try:
        flt = f"PublishedAt lt '{cutoff}'"
        for ent in _docs_client.query_entities(query_filter=flt):
            pk, rk = ent["PartitionKey"], ent["RowKey"]
            try:
                _docs_client.delete_entity(pk, rk)
                if _embed_client is not None:
                    try:
                        _embed_client.delete_entity(pk, rk)
                    except Exception:
                        pass
                deleted += 1
            except Exception:
                continue
    except Exception as e:
        log.warning("[rag.store] cleanup_old failed: %s", e)
    return deleted


def stats() -> dict:
    """Quick health snapshot for an admin endpoint."""
    _init()
    out = {"ready": is_ready(), "tables": {
        "docs": RAG_DOCS_TABLE, "embed": RAG_EMBED_TABLE, "meta": RAG_META_TABLE,
    }}
    if _meta_client is not None:
        try:
            out["recent_ingests"] = sorted(
                ({
                    "symbol": e["RowKey"].replace("last_ingest_", ""),
                    "ts": e.get("Timestamp"),
                    "docs": e.get("DocsAdded"),
                } for e in _meta_client.query_entities(
                    query_filter=f"PartitionKey eq '{_GLOBAL_PK}'")),
                key=lambda r: r.get("ts") or "", reverse=True)[:20]
        except Exception:
            out["recent_ingests"] = []
    return out
