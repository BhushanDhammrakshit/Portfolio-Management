"""Background precompute jobs.

Goal
----
Move all expensive work (provider calls, P&L math, analytics aggregates)
OFF the request path. Background jobs build payloads and dump them into
Redis; Flask routes only ``jget`` the precomputed JSON and return it.

This file deliberately ships small, generic builders. Specific
calculations (sector breakdown, drawdown, risk metrics, …) should be
factored out of the existing route handlers and called from
``_build_user_payload`` below. The scheduler glue stays the same.

Leader election
---------------
With multiple gunicorn workers we MUST run these jobs in only one place,
otherwise every worker hammers the upstream providers. The scheduler
takes a Redis lock (``scheduler:leader``) and renews it; only the worker
that holds the lock executes jobs. Without Redis the app assumes single-
worker dev and runs them locally.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from application import config
from application.services import cache

log = logging.getLogger(__name__)

# Redis key names (without the global CACHE_KEY_PREFIX, which jset adds).
K_HEATMAP = "heatmap:nifty50"
K_QUOTE = "quote:{symbol}"
K_USER_PORTFOLIO = "user:{uid}:portfolio"
K_USER_ANALYTICS = "user:{uid}:analytics"


# ── Market-wide refresh ────────────────────────────────────────────────

def refresh_market() -> None:
    """Rebuild the shared heatmap payload and store it in Redis."""
    if not cache.try_become_leader(ttl=max(config.MARKET_REFRESH_SECONDS * 3, 30)):
        return
    with cache.lock("refresh:market", ttl=30) as got:
        if not got:
            return
        try:
            # Import lazily so this module stays import-safe in tests.
            from application.routes import heatmap as heatmap_mod
            data = heatmap_mod._build_heatmap_payload()  # pure builder
            if data:
                cache.jset(K_HEATMAP, data, ttl=config.HEATMAP_CACHE_TTL * 4)
        except Exception as e:  # noqa: BLE001
            log.warning("precompute.refresh_market failed: %s", e)


# ── Per-user refresh ───────────────────────────────────────────────────

def _build_user_payload(user_id: str) -> dict | None:
    """Compute everything a logged-in user's pages need, in one go.

    Returns a dict like::

        {
          "portfolio": [...],
          "summary": {"invested": .., "current": .., "pnl": .., "pnl_pct": ..},
          "ts": 1700000000,
        }

    All upstream calls go through the shared quote cache, so multiple
    users holding the same stock only cost one provider hit.
    """
    try:
        from application.services.azure_table import stocks_table_client
        from application.services import market_data
    except Exception as e:  # noqa: BLE001
        log.warning("precompute: imports failed: %s", e)
        return None

    # 1. Holdings
    try:
        rows = list(stocks_table_client.query_entities(
            query_filter=f"UserId eq '{user_id}'"))
    except Exception as e:  # noqa: BLE001
        log.warning("precompute: holdings fetch failed for %s: %s", user_id, e)
        return None

    if not rows:
        return {"portfolio": [], "summary": {}, "ts": int(time.time())}

    # 2. Live prices (batched, cache-friendly)
    symbols = [r.get("StockName") for r in rows if r.get("StockName")]
    try:
        quotes = market_data.get_quotes(symbols) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("precompute: quotes failed for %s: %s", user_id, e)
        quotes = {}

    # 3. Build per-position rows + summary
    portfolio: list[dict] = []
    invested = 0.0
    current = 0.0
    sector_totals: dict[str, dict[str, float]] = {}
    top_gainer = None
    top_loser = None

    for r in rows:
        sym = r.get("StockName") or ""
        try:
            qty = float(r.get("Quantity") or 0)
            buy = float(r.get("PurchasePrice") or 0)
        except (TypeError, ValueError):
            qty, buy = 0.0, 0.0
        q = quotes.get(sym) or {}
        ltp = float(q.get("price") or buy or 0)
        pnl = (ltp - buy) * qty
        pnl_pct = ((ltp - buy) / buy * 100.0) if buy else 0.0
        position_value = ltp * qty
        sector = r.get("Sector") or "Other"

        row = {
            "symbol": sym,
            "qty": qty,
            "buy": buy,
            "ltp": round(ltp, 2),
            "value": round(position_value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "sector": sector,
        }
        portfolio.append(row)
        invested += buy * qty
        current += position_value

        # Sector aggregation
        bucket = sector_totals.setdefault(
            sector, {"invested": 0.0, "current": 0.0, "positions": 0})
        bucket["invested"] += buy * qty
        bucket["current"] += position_value
        bucket["positions"] += 1

        # Top mover tracking
        if qty > 0 and buy > 0:
            if top_gainer is None or pnl_pct > top_gainer["pnl_pct"]:
                top_gainer = row
            if top_loser is None or pnl_pct < top_loser["pnl_pct"]:
                top_loser = row

    pnl_total = current - invested
    summary = {
        "invested": round(invested, 2),
        "current": round(current, 2),
        "pnl": round(pnl_total, 2),
        "pnl_pct": round((pnl_total / invested * 100.0) if invested else 0.0, 2),
        "positions": len(portfolio),
        "top_gainer": top_gainer,
        "top_loser": top_loser,
    }

    # Sector breakdown — list form so the frontend can render directly.
    sector_breakdown = []
    for name, b in sector_totals.items():
        inv = b["invested"]
        cur = b["current"]
        sector_breakdown.append({
            "sector": name,
            "invested": round(inv, 2),
            "current": round(cur, 2),
            "pnl": round(cur - inv, 2),
            "pnl_pct": round(((cur - inv) / inv * 100.0) if inv else 0.0, 2),
            "weight_pct": round((cur / current * 100.0) if current else 0.0, 2),
            "positions": b["positions"],
        })
    sector_breakdown.sort(key=lambda x: x["current"], reverse=True)

    return {
        "portfolio": portfolio,
        "summary": summary,
        "sectors": sector_breakdown,
        "ts": int(time.time()),
    }


def refresh_user(user_id: str, force: bool = False) -> dict | None:
    """Recompute one user's payload and store it. Single-flight via a
    per-user Redis lock, so concurrent route + scheduler triggers don't
    duplicate work.
    """
    with cache.lock(f"refresh:user:{user_id}", ttl=30) as got:
        if not got and not force:
            # Someone else is computing — return whatever's there (may be None).
            return cache.jget(K_USER_PORTFOLIO.format(uid=user_id))
        payload = _build_user_payload(user_id)
        if payload is not None:
            cache.jset(
                K_USER_PORTFOLIO.format(uid=user_id),
                payload,
                ttl=max(config.USER_CACHE_TTL * 4, 60),
            )
        return payload


def refresh_active_users() -> None:
    """Refresh all users seen in the active window. Runs from APScheduler."""
    if not cache.try_become_leader(ttl=max(config.USER_REFRESH_SECONDS * 3, 60)):
        return
    uids = cache.active_user_ids()
    if not uids:
        return
    # Cap parallelism — we don't want to swamp Azure Tables or the providers.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="precompute") as ex:
        list(ex.map(lambda u: _safe(refresh_user, u), uids))


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("precompute.%s(%s) failed: %s", fn.__name__, args, e)
        return None


# ── Public read helpers (used by routes) ───────────────────────────────

def get_user_portfolio(user_id: str) -> dict | None:
    """Read-only fast path used by API routes."""
    return cache.jget(K_USER_PORTFOLIO.format(uid=user_id))


def get_heatmap() -> dict | None:
    return cache.jget(K_HEATMAP)


def invalidate_user(user_id: str) -> None:
    """Call this from any mutating endpoint (add/remove stock) so the
    next read recomputes immediately instead of returning stale data.
    """
    cache.jdelete(
        K_USER_PORTFOLIO.format(uid=user_id),
        K_USER_ANALYTICS.format(uid=user_id),
    )


# ── Scheduler wiring ───────────────────────────────────────────────────

_scheduler = None


def start_scheduler() -> None:
    """Idempotently start the APScheduler that drives the refreshers.

    Called from ``application/__init__.py``. Safe to call multiple times.
    """
    global _scheduler
    if _scheduler is not None:
        return
    if not config.ENABLE_PRECOMPUTE:
        log.info("precompute: disabled via ENABLE_PRECOMPUTE=0")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler(timezone="Asia/Kolkata", daemon=True)
        sched.add_job(
            refresh_market,
            "interval",
            seconds=config.MARKET_REFRESH_SECONDS,
            id="precompute_market",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        sched.add_job(
            refresh_active_users,
            "interval",
            seconds=config.USER_REFRESH_SECONDS,
            id="precompute_users",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        sched.start()
        _scheduler = sched
        log.info(
            "precompute: scheduler started (market=%ss, users=%ss, redis=%s)",
            config.MARKET_REFRESH_SECONDS, config.USER_REFRESH_SECONDS,
            cache.is_redis_enabled(),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("precompute: scheduler failed to start: %s", e)
