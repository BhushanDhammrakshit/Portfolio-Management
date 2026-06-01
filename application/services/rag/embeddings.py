"""Azure OpenAI embedding wrapper. Graceful no-op when not configured."""
from __future__ import annotations

import logging
from typing import List, Optional

import requests

from application.config import OPENAI_API_KEY, OPENAI_EMBED_ENDPOINT

log = logging.getLogger(__name__)

# text-embedding-3-small is 1536 dims by default
EMBED_DIM = 1536


def is_configured() -> bool:
    return bool(OPENAI_API_KEY and OPENAI_EMBED_ENDPOINT)


def _is_azure() -> bool:
    return bool(OPENAI_EMBED_ENDPOINT and "openai.azure.com" in OPENAI_EMBED_ENDPOINT)


def embed(texts: List[str], *, timeout: int = 30) -> Optional[List[List[float]]]:
    """Return embeddings for a batch of texts, or None on any failure.

    Each input is truncated to ~8000 chars (~2000 tokens) for safety.
    """
    if not is_configured() or not texts:
        return None

    cleaned = [(t or "")[:8000] for t in texts]

    if _is_azure():
        headers = {"Content-Type": "application/json", "api-key": OPENAI_API_KEY}
        payload = {"input": cleaned}
    else:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        }
        payload = {"input": cleaned, "model": "text-embedding-3-small"}

    try:
        r = requests.post(OPENAI_EMBED_ENDPOINT, headers=headers,
                          json=payload, timeout=timeout)
    except requests.RequestException as e:
        log.warning("[rag.embeddings] request failed: %s", e)
        return None

    if r.status_code != 200:
        log.warning("[rag.embeddings] HTTP %s: %s",
                    r.status_code, (r.text or "")[:200])
        return None

    try:
        data = r.json().get("data") or []
        return [d.get("embedding") for d in data if d.get("embedding")]
    except Exception as e:
        log.warning("[rag.embeddings] parse error: %s", e)
        return None


def embed_one(text: str) -> Optional[List[float]]:
    out = embed([text])
    return out[0] if out else None
