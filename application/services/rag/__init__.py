"""Retrieval-Augmented Generation (RAG) layer for stock analysis.

Free-tier stack:
    - Document store : Azure Tables (RagDocs, RagEmbed, RagMeta)
    - Embeddings     : Azure OpenAI text-embedding-3-small (optional)
    - News source    : free RSS feeds (Mint, Moneycontrol, BS, ET)
    - Filings        : NSE corporate-announcements public JSON
    - Vector search  : numpy cosine similarity (post-filter by symbol)

Public surface:
    - retriever.build_context(symbol)          -> (text_block, sources_list)
    - ingest.runner.run_for_symbols(symbols)   -> dict of stats
    - store.cleanup_old(days)                  -> int rows deleted
"""
from . import embeddings, retriever, store, symbols  # noqa: F401
