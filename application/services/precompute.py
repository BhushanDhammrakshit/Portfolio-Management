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

import datetime as _dt
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from application import config
from application.services import cache

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _within_market_window() -> bool:
    """True roughly during NSE hours (Mon-Fri 09:00 - 15:35 IST).

    Window starts before the 09:15 open so pre-open snapshots get captured
    and extends past 15:30 to let the gap-outlook 15:15 lock + last-15-min
    surge override finalise.
    """
    now = _dt.datetime.now(_IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 <= mins <= 15 * 60 + 35

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


def refresh_option_chain() -> None:
    """Rebuild the NIFTY option-chain snapshot in the background.

    Runs every ``OPTION_CHAIN_REFRESH_SECONDS`` during market hours so OI
    deltas, the intraday series, and the gap-outlook decision lock keep
    accumulating even when no user has the page open.
    """
    if not _within_market_window():
        return
    if not cache.try_become_leader(ttl=max(config.OPTION_CHAIN_REFRESH_SECONDS * 3, 60)):
        return
    with cache.lock("refresh:option_chain", ttl=45) as got:
        if not got:
            return
        try:
            from application.services import option_chain as oc_service
            oc_service.get_nifty_option_chain(force_refresh=True)
        except Exception as e:  # noqa: BLE001
            log.warning("precompute.refresh_option_chain failed: %s", e)


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


# ── Overnight scanner warmer ───────────────────────────────────────────
# Runs once a morning before users wake up. Forces every swing scan to
# recompute and writes results to Redis under the keys the live scanners
# read — so the first user request of the day is a cache hit and pays
# zero broker quota. Also walks the scan universe through ohlc_cache to
# seed Azure Table with today's closing candle (which the live scanners
# can't persist because they only see the cached path).

def refresh_swing_scans() -> None:
    """Recompute every swing scan and warm the OHLC table. Runs once per
    morning under leader election so only one gunicorn worker does it."""
    if not cache.try_become_leader(ttl=30 * 60):
        return
    with cache.lock("refresh:swing_scans", ttl=30 * 60) as got:
        if not got:
            return
        t0 = time.time()
        # 1. Warm OHLC table for the scan universe so the scan itself is
        #    served from cache. Done in parallel but capped to keep us
        #    well under the per-app Fyers throughput.
        universe = []
        try:
            from application.services import ohlc_cache, swing_scanner
            universe = list(getattr(swing_scanner, "UNIVERSE", []))
            if universe:
                with ThreadPoolExecutor(max_workers=4,
                                        thread_name_prefix="ohlc-warm") as ex:
                    list(ex.map(lambda s: _safe(ohlc_cache.warm, s, 365),
                                universe))
                log.info("precompute.refresh_swing_scans: warmed %d OHLC rows",
                         len(universe))
        except Exception as e:  # noqa: BLE001
            log.warning("precompute.refresh_swing_scans: OHLC warm failed: %s", e)

        # 1b. Warm every distinct symbol held in a user portfolio so the
        #     advanced-analytics dashboard reads real history from cache even
        #     when the live provider is unavailable during the day. Covers
        #     obscure / BSE holdings that aren't in the scan universe.
        try:
            from application.services import ohlc_cache
            from application.services.azure_table import stocks_table_client
            held = set()
            for r in stocks_table_client.list_entities():
                sym = (r.get("Symbol") or "").strip()
                if sym:
                    held.add(sym)
            held.add("^NSEI")  # benchmark used by the analytics dashboard
            extra = [s for s in held if s not in set(universe)]
            if extra:
                with ThreadPoolExecutor(max_workers=4,
                                        thread_name_prefix="ohlc-warm-port") as ex:
                    list(ex.map(lambda s: _safe(ohlc_cache.warm, s, 400), extra))
                log.info("precompute.refresh_swing_scans: warmed %d portfolio "
                         "symbols", len(extra))
        except Exception as e:  # noqa: BLE001
            log.warning("precompute.refresh_swing_scans: portfolio OHLC warm "
                        "failed: %s", e)

        # 2. Run every scan with force=True. Each one writes its own
        #    Redis payload, so live requests just `jget` it.
        try:
            from application.services import swing_tools, swing_scanner
            _safe(swing_tools.breakout_consolidation, True)
            _safe(swing_tools.relative_strength, True)
            _safe(swing_tools.chart_patterns, True)
            _safe(swing_tools.sector_leaders, True)
            _safe(swing_tools.sector_rotation, True)
            # Master setup scorer (swing_scanner._CACHE_KEY).
            scan = getattr(swing_scanner, "scan_universe", None) \
                or getattr(swing_scanner, "scan_setups", None) \
                or getattr(swing_scanner, "scan", None)
            if callable(scan):
                _safe(scan)
        except Exception as e:  # noqa: BLE001
            log.warning("precompute.refresh_swing_scans: scan run failed: %s", e)

        log.info("precompute.refresh_swing_scans: done in %.1fs",
                 time.time() - t0)


def refresh_ohlc_eod() -> None:
    """Re-warm the OHLC table after the 15:30 close so today's candle is
    persisted for the next trading day's scans."""
    if not cache.try_become_leader(ttl=30 * 60):
        return
    with cache.lock("refresh:ohlc_eod", ttl=30 * 60) as got:
        if not got:
            return
        try:
            from application.services import ohlc_cache, swing_scanner
            universe = list(getattr(swing_scanner, "UNIVERSE", []))
            if not universe:
                return
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=4,
                                    thread_name_prefix="ohlc-eod") as ex:
                list(ex.map(lambda s: _safe(ohlc_cache.warm, s, 30), universe))
            log.info("precompute.refresh_ohlc_eod: %d symbols in %.1fs",
                     len(universe), time.time() - t0)
        except Exception as e:  # noqa: BLE001
            log.warning("precompute.refresh_ohlc_eod failed: %s", e)


# ── Daily Fyers token refresh (TOTP, multi-app) ──────────────────────────
# Fyers access tokens expire at midnight IST. This job runs one TOTP
# login + N auth-code exchanges so all configured apps have fresh tokens
# before the 09:15 open. Leader election ensures only one gunicorn worker
# performs the login.

def refresh_fyers_tokens() -> None:
    """Refresh every Fyers app's daily access token via the headless TOTP
    flow. Triggered by the scheduler 07:30 IST Mon-Fri, and on startup
    when any configured app's pool token is missing.
    """
    if not cache.try_become_leader(ttl=10 * 60):
        return
    with cache.lock("refresh:fyers_tokens", ttl=10 * 60) as got:
        if not got:
            return
        try:
            from application.services.providers import fyers_auth
            tokens = fyers_auth.refresh_all_tokens()
            ok = sum(1 for v in tokens.values() if v)
            log.info("precompute.refresh_fyers_tokens: %d/%d apps refreshed",
                     ok, len(tokens))
        except Exception as e:  # noqa: BLE001
            log.warning("precompute.refresh_fyers_tokens failed: %s", e)


# ── Daily Upstox token refresh (TOTP) ────────────────────────────────────
# Upstox access tokens expire at 03:30 IST. This job runs the headless
# TOTP login so the token is fresh before the open. Leader election keeps
# only one worker performing the login.

def refresh_upstox_token() -> None:
    """Refresh the Upstox daily access token via the headless TOTP flow.
    Triggered by the scheduler before the open, and on startup when no
    usable token is cached.
    """
    if not cache.try_become_leader(ttl=10 * 60):
        return
    with cache.lock("refresh:upstox_token", ttl=10 * 60) as got:
        if not got:
            return
        try:
            from application.services.providers import upstox_auth
            tok = upstox_auth.refresh_access_token()
            log.info("precompute.refresh_upstox_token: %s",
                     "ok" if tok else "no token (degrading to Fyers)")
        except Exception as e:  # noqa: BLE001
            log.warning("precompute.refresh_upstox_token failed: %s", e)


def refresh_dhan_token() -> None:
    """Refresh the Dhan daily access token via TOTP.

    Tries renew first (lightweight, extends by 24h); falls back to full
    TOTP-based refresh if the current token is already expired.
    """
    if not cache.try_become_leader(ttl=10 * 60):
        return
    with cache.lock("refresh:dhan_token", ttl=5 * 60) as got:
        if not got:
            return
        try:
            from application.services.providers import dhan_auth
            tok = dhan_auth.renew_access_token()
            log.info("precompute.refresh_dhan_token: %s",
                     "ok" if tok else "failed (degrading to yfinance)")
        except Exception as e:  # noqa: BLE001
            log.warning("precompute.refresh_dhan_token failed: %s", e)


def mature_referral_credits() -> None:
    """Move referral credits past the 14-day cooldown to 'credited' status."""
    if not cache.try_become_leader(ttl=5 * 60):
        return
    try:
        from application.services import referral
        n = referral.mature_pending_credits()
        if n:
            log.info("precompute.mature_referral_credits: %d matured", n)
    except Exception as e:  # noqa: BLE001
        log.warning("precompute.mature_referral_credits failed: %s", e)



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
        sched.add_job(
            refresh_option_chain,
            "interval",
            seconds=config.OPTION_CHAIN_REFRESH_SECONDS,
            id="precompute_option_chain",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Overnight scanner warmer — cron, Mon-Fri at the configured time.
        if getattr(config, "SWING_PRECOMPUTE_ENABLED", True):
            sched.add_job(
                refresh_swing_scans,
                "cron",
                day_of_week="mon-fri",
                hour=getattr(config, "SWING_PRECOMPUTE_HOUR_IST", 8),
                minute=getattr(config, "SWING_PRECOMPUTE_MINUTE_IST", 0),
                id="precompute_swing_scans",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60 * 60,  # if app restarted late, still run within 1h
            )
            # End-of-day OHLC top-up: 16:00 IST captures today's close.
            sched.add_job(
                refresh_ohlc_eod,
                "cron",
                day_of_week="mon-fri",
                hour=16,
                minute=0,
                id="precompute_ohlc_eod",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60 * 60,
            )

        # Daily Fyers token refresh — well before the 09:15 market open.
        # Only schedule when the four personal creds are present; otherwise
        # there's nothing the job could do.
        if all(getattr(config, k, "") for k in (
            "FYERS_FY_ID", "FYERS_PIN", "FYERS_TOTP_SECRET", "FYERS_REDIRECT_URI",
        )):
            sched.add_job(
                refresh_fyers_tokens,
                "cron",
                day_of_week="mon-sun",
                hour=int(getattr(config, "FYERS_TOKEN_REFRESH_HOUR_IST", 7)),
                minute=int(getattr(config, "FYERS_TOKEN_REFRESH_MINUTE_IST", 30)),
                id="precompute_fyers_tokens",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60 * 60,
            )
            # Startup hydration: pull cached tokens from Redis right now;
            # if any app still has no usable token, kick off an immediate
            # one-shot refresh so the first request doesn't 401.
            try:
                from application.services.providers import fyers_auth
                fyers_auth.load_tokens_from_redis()
                pool = config.fyers_app_pool()
                creds = config.fyers_app_credentials()
                if creds and len(pool) < len(creds):
                    log.info(
                        "precompute: %d/%d Fyers tokens missing — scheduling "
                        "startup refresh", len(creds) - len(pool), len(creds),
                    )
                    sched.add_job(
                        refresh_fyers_tokens,
                        "date",
                        run_date=_dt.datetime.now(_IST) + _dt.timedelta(seconds=15),
                        id="precompute_fyers_tokens_startup",
                        replace_existing=True,
                    )
            except Exception as e:  # noqa: BLE001
                log.debug("precompute: token bootstrap check failed: %s", e)

        # Daily Upstox token refresh — before the 09:15 open. Only schedule
        # when the headless-login creds are present.
        if all(getattr(config, k, "") for k in (
            "UPSTOX_API_KEY", "UPSTOX_REDIRECT_URI", "UPSTOX_MOBILE",
            "UPSTOX_TOTP_SECRET", "UPSTOX_PIN",
        )):
            sched.add_job(
                refresh_upstox_token,
                "cron",
                day_of_week="mon-fri",
                hour=int(getattr(config, "UPSTOX_TOKEN_REFRESH_HOUR_IST", 7)),
                minute=int(getattr(config, "UPSTOX_TOKEN_REFRESH_MINUTE_IST", 15)),
                id="precompute_upstox_token",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60 * 60,
            )
            # Startup hydration: load any cached token from Redis; if none
            # is usable, kick off a one-shot refresh.
            try:
                from application.services.providers import upstox_auth
                upstox_auth.load_token_from_redis()
                if not config.upstox_access_token():
                    log.info("precompute: no Upstox token — scheduling startup "
                             "refresh")
                    sched.add_job(
                        refresh_upstox_token,
                        "date",
                        run_date=_dt.datetime.now(_IST) + _dt.timedelta(seconds=20),
                        id="precompute_upstox_token_startup",
                        replace_existing=True,
                    )
            except Exception as e:  # noqa: BLE001
                log.debug("precompute: upstox token bootstrap check failed: %s", e)

        # Daily Dhan token refresh — 07:00 IST, before market open.
        # Dhan tokens expire every 24h; this renews (or full-refreshes via
        # TOTP) so the app never loses data access.
        if all(getattr(config, k, "") for k in (
            "DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET",
        )):
            sched.add_job(
                refresh_dhan_token,
                "cron",
                day_of_week="mon-sun",
                hour=7,
                minute=0,
                id="precompute_dhan_token",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60 * 60,
            )
            # Startup: load cached token from Redis; if expired, refresh now.
            try:
                from application.services.providers import dhan_auth
                if not dhan_auth.load_token_from_redis():
                    log.info("precompute: no Dhan token in Redis — scheduling "
                             "startup refresh")
                    sched.add_job(
                        refresh_dhan_token,
                        "date",
                        run_date=_dt.datetime.now(_IST) + _dt.timedelta(seconds=10),
                        id="precompute_dhan_token_startup",
                        replace_existing=True,
                    )
            except Exception as e:  # noqa: BLE001
                log.debug("precompute: dhan token bootstrap failed: %s", e)

        # Daily referral credit maturation — 08:00 IST.
        sched.add_job(
            mature_referral_credits,
            "cron",
            day_of_week="mon-sun",
            hour=8,
            minute=0,
            id="precompute_referral_mature",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60 * 60,
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
