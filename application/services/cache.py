"""Shared cache layer (Redis when configured, in-process fallback otherwise).

Why this exists
---------------
HM2 has multiple gunicorn workers and may scale to multiple dynos. Each
worker previously kept its own in-process dicts (heatmap, quotes, tender
summary, …) which meant N× upstream traffic and inconsistent views for
different users hitting different workers.

This module gives the rest of the app a single, simple API:

    from application.services.cache import jget, jset, jdelete, lock, mark_active

    jset("heatmap:nifty50", payload, ttl=15)
    data = jget("heatmap:nifty50")

When ``REDIS_URL`` is set, all operations go through Redis (shared across
workers and dynos). When it is unset, we fall back to a process-local
TTL dict so dev / single-worker setups still work with zero config.

Flask-Caching is also initialised here so route decorators
(``@cache.cached(...)``) can be used for read-mostly endpoints.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Optional

from application import config

log = logging.getLogger(__name__)


def _safe_url(url: str) -> str:
    # Avoid logging credentials embedded in REDIS_URL.
    try:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        return f"{scheme}://{rest}"
    except Exception:
        return "<redis>"


# ── Redis client (optional) ────────────────────────────────────────────
redis_client = None  # type: ignore[assignment]
_redis_enabled = False

if config.REDIS_URL:
    try:
        import redis  # type: ignore

        redis_client = redis.Redis.from_url(
            config.REDIS_URL,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            health_check_interval=30,
        )
        # Probe the connection once at import time; if it fails we fall
        # back to the local cache without crashing the app.
        redis_client.ping()
        _redis_enabled = True
        log.info("cache: Redis enabled at %s", _safe_url(config.REDIS_URL))
    except Exception as e:  # noqa: BLE001
        log.warning("cache: Redis unavailable (%s); using in-process fallback", e)
        redis_client = None
        _redis_enabled = False


def is_redis_enabled() -> bool:
    return _redis_enabled and not _budget_exhausted()


def _prefix(key: str) -> str:
    return f"{config.CACHE_KEY_PREFIX}{key}"


def _can_use_redis(cost: int = 1) -> bool:
    """Gate used by every Redis call-site. Charges ``cost`` commands
    against the daily budget and returns True if the caller may proceed.
    """
    if not _redis_enabled or redis_client is None:
        return False
    return _budget_charge(cost)


# ── Daily command-budget guard ─────────────────────────────────────────
# Upstash free tier caps us at 10k commands per UTC day. We track an
# in-process counter and periodically reconcile it with a shared Redis
# counter so all gunicorn workers / dynos share the same view. When the
# budget is exhausted we silently route everything through the
# in-process fallback until the next UTC midnight.

_BUDGET_KEY = "__budget"  # gets CACHE_KEY_PREFIX prepended
_budget_lock = threading.Lock()
_budget_local_count = 0       # ops since last sync (this worker)
_budget_global_count = 0      # last known global count (post-sync)
_budget_day = ""              # YYYY-MM-DD UTC for current window
_budget_exhausted_flag = False


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _budget_seconds_until_reset() -> int:
    # Seconds until next UTC midnight + small buffer.
    now = time.time()
    secs_today = int(now) % 86400
    return max(60, 86400 - secs_today + 30)


def _budget_reset_if_new_day() -> None:
    global _budget_local_count, _budget_global_count, _budget_day, _budget_exhausted_flag
    today = _utc_day()
    if today != _budget_day:
        _budget_day = today
        _budget_local_count = 0
        _budget_global_count = 0
        _budget_exhausted_flag = False


def _budget_exhausted() -> bool:
    limit = config.REDIS_DAILY_COMMAND_LIMIT
    if limit <= 0:
        return False
    with _budget_lock:
        _budget_reset_if_new_day()
        return _budget_exhausted_flag


def _budget_charge(n: int = 1) -> bool:
    """Charge ``n`` commands against the daily budget.

    Returns True if the caller may proceed with the Redis op(s), False
    if the budget is exhausted and the caller should use the local
    fallback instead.

    Every ``REDIS_BUDGET_SYNC_EVERY`` charges we sync the accumulated
    local count into a shared Redis counter (one extra command) so all
    workers see the same total.
    """
    if not _redis_enabled or redis_client is None:
        return False
    limit = config.REDIS_DAILY_COMMAND_LIMIT
    if limit <= 0:
        return True

    global _budget_local_count, _budget_global_count, _budget_exhausted_flag
    with _budget_lock:
        _budget_reset_if_new_day()
        if _budget_exhausted_flag:
            return False
        _budget_local_count += n
        # Use the latest snapshot of (global + local) as an estimate.
        if (_budget_global_count + _budget_local_count) >= limit:
            _budget_exhausted_flag = True
            log.warning(
                "cache: daily Redis budget (%s) reached — falling back to "
                "in-process cache until UTC midnight", limit,
            )
            return False
        # Time to sync?
        if _budget_local_count < config.REDIS_BUDGET_SYNC_EVERY:
            return True
        delta = _budget_local_count
        _budget_local_count = 0

    # Outside the lock: do the sync (one extra command, +1 to delta).
    try:
        key = _prefix(_BUDGET_KEY) + ":" + _utc_day()
        new_total = redis_client.incrby(key, delta + 1)  # +1 for this INCRBY
        # Make the counter expire shortly after the day rolls over so we
        # don't leak keys forever.
        redis_client.expire(key, _budget_seconds_until_reset())
        with _budget_lock:
            _budget_global_count = int(new_total)
            if _budget_global_count >= limit:
                _budget_exhausted_flag = True
                log.warning(
                    "cache: daily Redis budget (%s) reached at global=%s — "
                    "falling back to in-process cache until UTC midnight",
                    limit, _budget_global_count,
                )
                return False
    except Exception as e:  # noqa: BLE001
        log.debug("cache._budget_charge sync error: %s", e)
    return True


def budget_stats() -> dict:
    with _budget_lock:
        _budget_reset_if_new_day()
        return {
            "day_utc": _budget_day,
            "limit": config.REDIS_DAILY_COMMAND_LIMIT,
            "global_count": _budget_global_count,
            "local_count": _budget_local_count,
            "exhausted": _budget_exhausted_flag,
        }


# ── Flask-Caching (optional) ───────────────────────────────────────────
# Used for ``@cache.cached`` / ``@cache.memoize`` on routes. When Redis
# is not configured we use SimpleCache (per-process), which is still
# useful in dev.
try:
    from flask_caching import Cache  # type: ignore

    if _redis_enabled:
        cache = Cache(config={
            "CACHE_TYPE": "RedisCache",
            "CACHE_REDIS_URL": config.REDIS_URL,
            "CACHE_DEFAULT_TIMEOUT": config.CACHE_DEFAULT_TIMEOUT,
            "CACHE_KEY_PREFIX": config.CACHE_KEY_PREFIX,
        })
    else:
        cache = Cache(config={
            "CACHE_TYPE": "SimpleCache",
            "CACHE_DEFAULT_TIMEOUT": config.CACHE_DEFAULT_TIMEOUT,
        })
except Exception as e:  # noqa: BLE001
    log.warning("cache: Flask-Caching unavailable (%s); decorators are no-ops", e)
    cache = None  # type: ignore[assignment]


# ── In-process fallback store ──────────────────────────────────────────
_local: dict[str, tuple[float, Any]] = {}
_local_lock = threading.Lock()


# ── JSON get / set ─────────────────────────────────────────────────────

def jset(key: str, value: Any, ttl: int) -> None:
    """Store ``value`` (any JSON-serialisable Python object) under ``key``
    with a TTL in seconds. Silent on errors — caching must never break
    the request path.
    """
    try:
        if _can_use_redis(1):
            redis_client.set(_prefix(key), json.dumps(value, default=str), ex=ttl)
            return
    except Exception as e:  # noqa: BLE001
        log.debug("cache.jset(%s) redis error: %s", key, e)
    # Fallback / on-error path
    with _local_lock:
        _local[key] = (time.time() + ttl, value)


def jget(key: str) -> Optional[Any]:
    try:
        if _can_use_redis(1):
            raw = redis_client.get(_prefix(key))
            return json.loads(raw) if raw else None
    except Exception as e:  # noqa: BLE001
        log.debug("cache.jget(%s) redis error: %s", key, e)
    with _local_lock:
        entry = _local.get(key)
        if not entry:
            return None
        exp, val = entry
        if exp < time.time():
            _local.pop(key, None)
            return None
        return val


def jdelete(*keys: str) -> None:
    if not keys:
        return
    try:
        if _can_use_redis(1):
            redis_client.delete(*(_prefix(k) for k in keys))
    except Exception as e:  # noqa: BLE001
        log.debug("cache.jdelete redis error: %s", e)
    with _local_lock:
        for k in keys:
            _local.pop(k, None)


def jget_many(keys: Iterable[str]) -> dict[str, Any]:
    keys = list(keys)
    if not keys:
        return {}
    out: dict[str, Any] = {}
    try:
        if _can_use_redis(1):
            raws = redis_client.mget(_prefix(k) for k in keys)
            for k, raw in zip(keys, raws):
                if raw:
                    try:
                        out[k] = json.loads(raw)
                    except Exception:
                        pass
            return out
    except Exception as e:  # noqa: BLE001
        log.debug("cache.jget_many redis error: %s", e)
    now = time.time()
    with _local_lock:
        for k in keys:
            entry = _local.get(k)
            if entry and entry[0] >= now:
                out[k] = entry[1]
    return out


# ── Distributed lock (SET NX EX) ───────────────────────────────────────

@contextmanager
def lock(name: str, ttl: int = 30, blocking: bool = False, wait: float = 0.0):
    """Best-effort distributed lock.

    Usage:
        with lock("refresh:user:42", ttl=30) as got:
            if not got: return         # someone else is doing it
            ...do the work...

    Falls back to a process-local lock if Redis is disabled.
    """
    token = uuid.uuid4().hex
    acquired = False
    key = _prefix(f"lock:{name}")

    deadline = time.time() + wait
    while True:
        try:
            if _can_use_redis(1):
                acquired = bool(redis_client.set(key, token, nx=True, ex=ttl))
            else:
                with _local_lock:
                    entry = _local.get(f"__lock__:{name}")
                    if not entry or entry[0] < time.time():
                        _local[f"__lock__:{name}"] = (time.time() + ttl, token)
                        acquired = True
        except Exception as e:  # noqa: BLE001
            log.debug("cache.lock(%s) error: %s", name, e)
            acquired = False

        if acquired or not blocking or time.time() >= deadline:
            break
        time.sleep(0.05)

    try:
        yield acquired
    finally:
        if not acquired:
            return
        try:
            if _can_use_redis(1):
                # Only delete if we still own the lock (token match).
                redis_client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] "
                    "then return redis.call('del', KEYS[1]) else return 0 end",
                    1, key, token,
                )
            else:
                with _local_lock:
                    entry = _local.get(f"__lock__:{name}")
                    if entry and entry[1] == token:
                        _local.pop(f"__lock__:{name}", None)
        except Exception as e:  # noqa: BLE001
            log.debug("cache.lock release(%s) error: %s", name, e)


# ── Active-user tracking ───────────────────────────────────────────────
# A simple sorted set ``active_users`` (score = last_seen_epoch) so the
# precompute scheduler knows which users to refresh.

_ACTIVE_KEY = "active_users"


def mark_active(user_id: str) -> None:
    if not user_id:
        return
    try:
        ts = time.time()
        if _can_use_redis(2):
            redis_client.zadd(_prefix(_ACTIVE_KEY), {user_id: ts})
            # Trim very old entries opportunistically.
            cutoff = ts - max(config.USER_ACTIVE_WINDOW_SECONDS * 4, 3600)
            redis_client.zremrangebyscore(_prefix(_ACTIVE_KEY), "-inf", cutoff)
            return
    except Exception as e:  # noqa: BLE001
        log.debug("cache.mark_active redis error: %s", e)
    with _local_lock:
        _local[f"active:{user_id}"] = (time.time() + config.USER_ACTIVE_WINDOW_SECONDS * 4, time.time())


def active_user_ids(window_seconds: Optional[int] = None) -> list[str]:
    window = window_seconds or config.USER_ACTIVE_WINDOW_SECONDS
    cutoff = time.time() - window
    try:
        if _can_use_redis(1):
            return [
                uid for uid in redis_client.zrangebyscore(
                    _prefix(_ACTIVE_KEY), cutoff, "+inf")
                if uid
            ]
    except Exception as e:  # noqa: BLE001
        log.debug("cache.active_user_ids redis error: %s", e)
    out: list[str] = []
    now = time.time()
    with _local_lock:
        for k, (exp, ts) in list(_local.items()):
            if k.startswith("active:") and exp >= now and ts >= cutoff:
                out.append(k.split(":", 1)[1])
    return out


# ── Leader election (for the scheduler) ────────────────────────────────
# So that with N gunicorn workers, only ONE actually runs the background
# refresh jobs. The leader renews its lease periodically; if it dies any
# other worker picks it up within ``ttl`` seconds.

_LEADER_KEY = "scheduler:leader"
_leader_token = uuid.uuid4().hex


def try_become_leader(ttl: int = 30) -> bool:
    """Return True if this process is (now) the scheduler leader."""
    try:
        if _can_use_redis(3):
            # NX acquire, or renew if we already hold it.
            got = redis_client.set(_prefix(_LEADER_KEY), _leader_token, nx=True, ex=ttl)
            if got:
                return True
            current = redis_client.get(_prefix(_LEADER_KEY))
            if current == _leader_token:
                redis_client.expire(_prefix(_LEADER_KEY), ttl)
                return True
            return False
    except Exception as e:  # noqa: BLE001
        log.debug("cache.try_become_leader error: %s", e)
    # Without Redis there's no cross-worker coordination — assume leader
    # (caller controls this via ENABLE_PRECOMPUTE in single-worker dev).
    return True


# ── Stats ──────────────────────────────────────────────────────────────

def stats() -> dict:
    info = {
        "redis": _redis_enabled,
        "redis_active": is_redis_enabled(),
        "local_size": 0,
        "budget": budget_stats(),
    }
    with _local_lock:
        info["local_size"] = len(_local)
    # Don't charge the budget for the introspection dbsize call.
    if _redis_enabled and redis_client is not None:
        try:
            info["redis_keys"] = redis_client.dbsize()
        except Exception:
            pass
    return info
