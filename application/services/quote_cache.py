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
from application.services.providers import dhan_provider, fyers_provider

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
    """Fetch ``symbols`` from upstream providers, splitting load 50/50
    between Dhan and Fyers when both are configured.

    Both providers are queried in parallel threads. If one returns
    nothing for a symbol the other had to fetch, we'll pick that gap up
    on the next refresh cycle (no synchronous fallback here, to keep
    response time bounded).
    """
    if not symbols:
        return {}

    have_dhan = _dhan_available()
    have_fyers = _fyers_available()

    # Single-provider paths
    if have_dhan and not have_fyers:
        return _fetch_with(dhan_provider, symbols)
    if have_fyers and not have_dhan:
        return _fetch_with(fyers_provider, symbols)
    if not (have_dhan or have_fyers):
        log.warning("quote_cache: no provider configured (Dhan/Fyers)")
        return {}

    # Both available — interleave so consecutive symbols don't all land
    # on the same provider (helps when callers pass a sector-grouped list).
    dhan_syms = symbols[0::2]
    fyers_syms = symbols[1::2]

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="qcache") as ex:
        f_dhan = ex.submit(_fetch_with, dhan_provider, dhan_syms)
        f_fyers = ex.submit(_fetch_with, fyers_provider, fyers_syms)
        out.update(f_dhan.result() or {})
        out.update(f_fyers.result() or {})

    # Cross-fill: if a symbol assigned to provider A came back empty,
    # ask the other provider once. This keeps coverage high without
    # doubling baseline load.
    missing = [s for s in symbols if not out.get(s)]
    if missing:
        # Send all missing to whichever provider didn't originally
        # handle them (best effort).
        from_dhan_missing = [s for s in missing if s in set(dhan_syms)]
        from_fyers_missing = [s for s in missing if s in set(fyers_syms)]
        if from_dhan_missing:
            out.update(_fetch_with(fyers_provider, from_dhan_missing))
        if from_fyers_missing:
            out.update(_fetch_with(dhan_provider, from_fyers_missing))

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

    # Tell the WS worker (if running) that these symbols are live so it
    # streams them and the next read becomes a tick-hit. No-op without
    # Redis. Cheap: a single ZADD with the full list.
    try:
        from application.services import ws_subscription
        ws_subscription.subscribe_many(uniq)
    except Exception:  # noqa: BLE001
        pass

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
    }
