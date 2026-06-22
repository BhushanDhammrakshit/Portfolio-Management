"""Shared subscription set: which symbols the WS worker should stream.

The Flask app and the WebSocket worker live in different processes. They
coordinate via Redis:

    SADD ws:subscribe RELIANCE.NS TCS.NS ...

The Flask side calls :func:`subscribe` / :func:`subscribe_many` from
routes whenever a user opens a page that needs live quotes for a set of
symbols. The WS worker reads the set every few seconds with
:func:`active_symbols` and diffs against its current subscription.

Symbols are kept in a sorted set scored by ``last-seen epoch``; a tiny
sweeper drops anything that hasn't been re-added for
``SUBSCRIPTION_TTL`` seconds, so an idle user doesn't keep us paying for
quotes forever.

Falls back to a no-op when Redis isn't configured (the WS worker simply
won't get any symbols and will idle — which is the correct behaviour in
a dev environment without Redis).
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

from application.services import cache as shared_cache

log = logging.getLogger(__name__)

_KEY = "ws:subscribe"
SUBSCRIPTION_TTL = 30 * 60  # symbol auto-evicts 30 min after last touch


def _redis():
    """Return the raw redis client or None if not configured.
    The shared cache exposes the underlying client via ``_redis`` after
    ``init_app``; we tolerate either name."""
    r = getattr(shared_cache, "_redis", None) or getattr(shared_cache, "redis", None)
    return r if r and shared_cache.is_redis_enabled() else None


def subscribe(symbol: str) -> None:
    subscribe_many([symbol])


def subscribe_many(symbols: Iterable[str]) -> int:
    """Mark ``symbols`` as needing live quotes. Returns count touched."""
    syms = [s for s in (s.strip() for s in symbols if s) if s]
    if not syms:
        return 0
    r = _redis()
    if r is None:
        return 0
    now = time.time()
    try:
        # ZADD with current timestamp as score so we can evict by age.
        mapping = {s: now for s in syms}
        r.zadd(_KEY, mapping)
        # Best-effort TTL refresh on the key itself, in case Redis is
        # configured with maxmemory-policy=allkeys-lru.
        r.expire(_KEY, SUBSCRIPTION_TTL * 4)
        return len(syms)
    except Exception as e:  # noqa: BLE001
        log.debug("ws_subscription.subscribe_many failed: %s", e)
        return 0


def active_symbols() -> list[str]:
    """Return the currently-active symbol set. Called by the WS worker."""
    r = _redis()
    if r is None:
        return []
    now = time.time()
    try:
        # First sweep stale entries; then read what's left.
        r.zremrangebyscore(_KEY, min=0, max=now - SUBSCRIPTION_TTL)
        raw = r.zrange(_KEY, 0, -1)
        return [s.decode() if isinstance(s, (bytes, bytearray)) else str(s)
                for s in (raw or [])]
    except Exception as e:  # noqa: BLE001
        log.debug("ws_subscription.active_symbols failed: %s", e)
        return []


def unsubscribe(symbol: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.zrem(_KEY, symbol)
    except Exception as e:  # noqa: BLE001
        log.debug("ws_subscription.unsubscribe failed: %s", e)
