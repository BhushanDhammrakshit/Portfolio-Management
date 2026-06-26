"""Server-side quote cache + split-provider fetcher.

Why this exists
---------------
Every Flask worker used to call Fyers (or Dhan) directly on every request.
With N users hitting the heatmap or portfolio page, that multiplied
upstream traffic and tripped Cloudflare's 1015 / HTTP 429 rate limit.

This module sits in front of the market-data providers and gives us:

1. **TTL cache (15s)** — concurrent requests for the same symbol within
   the window return the cached quote; upstream is hit at most once per
   symbol per ``QUOTE_TTL_SECONDS`` seconds.

2. **Single-flight** — when many requests arrive simultaneously and the
   cache is cold/stale, only one of them performs the upstream fetch;
   the rest wait on a lock and reuse the result.

3. **Provider split** — missing symbols are divided ~50/50 between Dhan
   and Fyers and fetched in parallel. This halves the per-provider
   request rate, so neither broker's rate-limit window fills up.

If only one provider is configured, the entire batch goes to that one
(no behavioural change vs. the old single-provider path, except we now
benefit from the cache).
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from application import config
from application.services import cache as shared_cache
from application.services.providers import (
    dhan_provider,
    fyers_provider,
    upstox_provider,
)

log = logging.getLogger(__name__)

# How long a cached quote is considered fresh. Pulled from config so the
# TTL is consistent with the precompute scheduler.
QUOTE_TTL_SECONDS = float(getattr(config, "QUOTE_CACHE_TTL", 15))

# Redis key for a single symbol's quote (the shared cache adds the
# global CACHE_KEY_PREFIX). Storing one key per symbol (not a hash)
# keeps per-key TTLs simple and matches the shared-cache helpers.
_QUOTE_KEY = "quote:{symbol}"


# ── Provider availability ──────────────────────────────────────────────

def _dhan_available() -> bool:
    return bool(config.DHAN_CLIENT_ID and config.DHAN_ACCESS_TOKEN)


def _fyers_available() -> bool:
    return bool(config.FYERS_APP_ID and config.FYERS_ACCESS_TOKEN)


def _upstox_available() -> bool:
    return bool(config.UPSTOX_API_KEY and config.upstox_access_token())


def _active_providers() -> list:
    """Providers currently configured, in load-balancing order."""
    out = []
    if _dhan_available():
        out.append(dhan_provider)
    if _fyers_available():
        out.append(fyers_provider)
    if _upstox_available():
        out.append(upstox_provider)
    return out


# ── Cache helpers (Redis-backed via shared_cache) ──────────────────────

def _now() -> float:
    return time.time()


def _read_fresh(symbols: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Return (fresh_hits, stale_or_missing).

    Reads happen in a single ``MGET`` against Redis (or the in-process
    fallback). Anything Redis returns is considered fresh — TTL is
    enforced by Redis itself via the ``EX`` argument used in ``_store``.
    """
    keys = [_QUOTE_KEY.format(symbol=s) for s in symbols]
    got = shared_cache.jget_many(keys)
    fresh: dict[str, dict] = {}
    stale: list[str] = []
    for sym, key in zip(symbols, keys):
        val = got.get(key)
        if val:
            fresh[sym] = val
        else:
            stale.append(sym)
    return fresh, stale


def _store(quotes: dict[str, dict]) -> None:
    if not quotes:
        return
    ttl = int(QUOTE_TTL_SECONDS)
    for sym, q in quotes.items():
        if q:
            shared_cache.jset(_QUOTE_KEY.format(symbol=sym), q, ttl=ttl)


# ── Split fetch ────────────────────────────────────────────────────────

def _fetch_with(provider, symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    try:
        return provider.get_quotes(symbols) or {}
    except Exception as e:
        log.warning("quote_cache: %s.get_quotes failed: %s",
                    getattr(provider, "__name__", provider), e)
        return {}


def _split_fetch(symbols: list[str]) -> dict[str, dict]:
    """Fetch ``symbols`` from upstream, splitting load evenly across every
    configured provider (Dhan / Fyers / Upstox) in parallel.

    Spreading each batch across N providers means each one handles ~1/N of
    the symbols, so no single broker's rate-limit window fills up and the
    overall round-trip is bounded by the slowest 1/N slice rather than the
    whole batch — this is the main latency win when several brokers are
    connected. Symbols a provider couldn't serve are cross-filled once by
    the others.
    """
    if not symbols:
        return {}

    providers = _active_providers()
    if not providers:
        log.warning("quote_cache: no provider configured (Dhan/Fyers/Upstox)")
        return {}

    # Single-provider path — no threading overhead.
    if len(providers) == 1:
        return _fetch_with(providers[0], symbols)

    # Round-robin assignment so consecutive symbols don't all land on the
    # same provider (helps when callers pass a sector-grouped list).
    n = len(providers)
    buckets: list[list[str]] = [[] for _ in range(n)]
    for i, sym in enumerate(symbols):
        buckets[i % n].append(sym)

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="qcache") as ex:
        futures = [ex.submit(_fetch_with, providers[i], buckets[i])
                   for i in range(n)]
        for fut in futures:
            out.update(fut.result() or {})

    # Cross-fill: any symbol that came back empty is retried once on the
    # other providers (round-robin), keeping coverage high without
    # doubling baseline load.
    missing = [s for s in symbols if not out.get(s)]
    if missing:
        owner = {}
        for i in range(n):
            for s in buckets[i]:
                owner[s] = i
        retry_buckets: list[list[str]] = [[] for _ in range(n)]
        for j, s in enumerate(missing):
            # Pick a provider other than the one that originally owned it.
            orig = owner.get(s, 0)
            target = (orig + 1 + j) % n
            if target == orig:
                target = (target + 1) % n
            retry_buckets[target].append(s)
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="qcache-cf") as ex:
            futures = [ex.submit(_fetch_with, providers[i], retry_buckets[i])
                       for i in range(n) if retry_buckets[i]]
            for fut in futures:
                out.update(fut.result() or {})

    return out


# ── Single-flight wrapper (distributed via Redis lock) ─────────────────

def _fetch_coalesced(symbols: list[str]) -> dict[str, dict]:
    """Fetch ``symbols`` with single-flight per symbol *across workers*.

    Strategy: try to claim a short-lived per-symbol Redis lock
    (``SET NX EX``). The thread that wins fetches upstream and writes
    the result to Redis. Threads that lose the race briefly poll the
    cache until the winner publishes the result (or the lock expires).
    """
    if not symbols:
        return {}

    to_fetch: list[str] = []
    waits: list[str] = []
    owned_locks: list[object] = []  # context managers we need to exit

    # First pass: try to acquire lock for each missing symbol.
    for s in symbols:
        cm = shared_cache.lock(f"quote:fetch:{s}", ttl=10)
        got = cm.__enter__()
        if got:
            owned_locks.append((s, cm))
            to_fetch.append(s)
        else:
            # Someone else is fetching — release our (unowned) CM and wait.
            cm.__exit__(None, None, None)
            waits.append(s)

    result: dict[str, dict] = {}
    try:
        if to_fetch:
            fetched = _split_fetch(to_fetch)
            _store(fetched)
            result.update(fetched)
    finally:
        # Release every lock we own so waiters can proceed.
        for _s, cm in owned_locks:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    # Pick up results other workers fetched. Short, bounded poll on
    # Redis — much simpler than threading.Event coordination and
    # naturally works across processes / dynos.
    if waits:
        deadline = _now() + 10.0
        pending = list(waits)
        while pending and _now() < deadline:
            got = shared_cache.jget_many(_QUOTE_KEY.format(symbol=s) for s in pending)
            still: list[str] = []
            for s in pending:
                v = got.get(_QUOTE_KEY.format(symbol=s))
                if v:
                    result[s] = v
                else:
                    still.append(s)
            if not still:
                break
            pending = still
            time.sleep(0.1)

    return result


# ── Public API ─────────────────────────────────────────────────────────

def get_quotes(symbols: Iterable[str]) -> dict[str, dict]:
    syms = [s for s in symbols if s]
    if not syms:
        return {}
    # De-dupe while preserving order.
    seen = set()
    uniq: list[str] = []
    for s in syms:
        if s not in seen:
            seen.add(s)
            uniq.append(s)

    fresh, stale = _read_fresh(uniq)
    if stale:
        fetched = _fetch_coalesced(stale)
        fresh.update(fetched)
    return fresh


def get_quote(symbol: str) -> Optional[dict]:
    res = get_quotes([symbol])
    return res.get(symbol)


def invalidate(symbols: Optional[Iterable[str]] = None) -> None:
    """Drop cache entries (a subset, or all). Useful for tests / admin."""
    if symbols is None:
        # Without an explicit list we can't safely SCAN+DEL in Redis from
        # here. Callers wanting a full flush should use the shared_cache
        # admin tools. We at least clear the local fallback.
        return
    keys = [_QUOTE_KEY.format(symbol=s) for s in symbols if s]
    if keys:
        shared_cache.jdelete(*keys)


def stats() -> dict:
    """Lightweight introspection for /healthz-style endpoints."""
    return {
        "backend": "redis" if shared_cache.is_redis_enabled() else "local",
        "ttl_seconds": QUOTE_TTL_SECONDS,
        "dhan_enabled": _dhan_available(),
        "fyers_enabled": _fyers_available(),
        "upstox_enabled": _upstox_available(),
    }
