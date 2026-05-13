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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from application import config
from application.services.providers import dhan_provider, fyers_provider

log = logging.getLogger(__name__)

# How long a cached quote is considered fresh. 15s matches the user's
# acceptable refresh interval and is comfortably below intraday tick
# usefulness.
QUOTE_TTL_SECONDS = 15.0

# Per-symbol cache: symbol -> (epoch_seconds_fetched, quote_dict)
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# Single-flight: ensures only one thread fetches a given symbol at a time.
# Other threads wanting the same symbol wait on the same Event.
_inflight: dict[str, threading.Event] = {}
_inflight_lock = threading.Lock()


# ── Provider availability ──────────────────────────────────────────────

def _dhan_available() -> bool:
    return bool(config.DHAN_CLIENT_ID and config.DHAN_ACCESS_TOKEN)


def _fyers_available() -> bool:
    return bool(config.FYERS_APP_ID and config.FYERS_ACCESS_TOKEN)


# ── Cache helpers ──────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _read_fresh(symbols: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Return (fresh_hits, stale_or_missing). Holds the cache lock briefly."""
    fresh: dict[str, dict] = {}
    stale: list[str] = []
    cutoff = _now() - QUOTE_TTL_SECONDS
    with _cache_lock:
        for s in symbols:
            entry = _cache.get(s)
            if entry and entry[0] >= cutoff:
                fresh[s] = entry[1]
            else:
                stale.append(s)
    return fresh, stale


def _store(quotes: dict[str, dict]) -> None:
    if not quotes:
        return
    ts = _now()
    with _cache_lock:
        for s, q in quotes.items():
            if q:
                _cache[s] = (ts, q)


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


# ── Single-flight wrapper ──────────────────────────────────────────────

def _fetch_coalesced(symbols: list[str]) -> dict[str, dict]:
    """Fetch ``symbols`` with single-flight per symbol. Concurrent callers
    asking for overlapping symbols share one upstream round-trip.
    """
    if not symbols:
        return {}

    # Decide who fetches what. For each symbol either:
    #   - we own it (set up an Event and fetch), or
    #   - someone else is fetching it (we'll wait on their Event).
    to_fetch: list[str] = []
    waits: list[tuple[str, threading.Event]] = []
    own_events: dict[str, threading.Event] = {}

    with _inflight_lock:
        for s in symbols:
            ev = _inflight.get(s)
            if ev is None:
                ev = threading.Event()
                _inflight[s] = ev
                own_events[s] = ev
                to_fetch.append(s)
            else:
                waits.append((s, ev))

    result: dict[str, dict] = {}
    try:
        if to_fetch:
            fetched = _split_fetch(to_fetch)
            _store(fetched)
            result.update(fetched)
    finally:
        # Release every event we own, regardless of fetch outcome, so
        # waiters don't deadlock on a failed upstream call.
        with _inflight_lock:
            for s, ev in own_events.items():
                _inflight.pop(s, None)
                ev.set()

    # Wait for symbols other threads were already fetching, then read
    # their results from the cache.
    if waits:
        # Bound the wait so a hung upstream can't stall us forever.
        deadline = _now() + 10.0
        for s, ev in waits:
            remaining = max(0.0, deadline - _now())
            ev.wait(timeout=remaining)
        with _cache_lock:
            for s, _ in waits:
                entry = _cache.get(s)
                if entry:
                    result[s] = entry[1]

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
    """Drop cache entries (all, or a subset). Useful for tests / admin."""
    with _cache_lock:
        if symbols is None:
            _cache.clear()
        else:
            for s in symbols:
                _cache.pop(s, None)


def stats() -> dict:
    """Lightweight introspection for /healthz-style endpoints."""
    with _cache_lock:
        size = len(_cache)
        if _cache:
            ages = [_now() - ts for ts, _ in _cache.values()]
            avg_age = sum(ages) / len(ages)
        else:
            avg_age = 0.0
    return {
        "size": size,
        "avg_age_seconds": round(avg_age, 2),
        "ttl_seconds": QUOTE_TTL_SECONDS,
        "dhan_enabled": _dhan_available(),
        "fyers_enabled": _fyers_available(),
    }
