"""Fyers WebSocket ticker — runs in a dedicated worker process.

This module is imported by ``scripts/run_fyers_ws.py`` (and by the Azure
WebJob deployment) — **never** by the Flask app. Importing it inside a
gunicorn worker would create one WS connection per worker, defeating the
purpose.

Responsibilities
----------------
1. Open a single Fyers WebSocket (Lite mode by default) and stay
   connected with exponential-backoff reconnect.
2. Every ``POLL_INTERVAL`` seconds, read the desired symbol set from
   Redis (:mod:`application.services.ws_subscription`) and reconcile —
   subscribe new symbols, unsubscribe departed ones.
3. On every tick, write a normalised quote dict to Redis under
   ``quote:{symbol}`` with a TTL of ``FYERS_WS_TICK_TTL`` seconds, using
   the same key format the existing :mod:`quote_cache` consumer reads.
   Tick-driven keys overwrite REST-fetched ones; consumers don't need
   to know where the data came from.
4. On HTTP 401 (token expired), re-read the token from settings_store
   and reconnect.

The Flask quote pipeline (``quote_cache.get_quotes``) already handles
the fallback path: if a key is missing from Redis it goes to the broker
REST API. So if the WS process is down or hasn't subscribed a symbol
yet, the app gracefully degrades to REST instead of failing.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable, Optional

from application import config
from application.services import cache as shared_cache
from application.services import ws_subscription
from application.services.providers.fyers_provider import _from_fyers, _to_fyers

log = logging.getLogger(__name__)

POLL_INTERVAL = 5.0       # seconds between subscription-reconciliation passes
RECONNECT_BASE = 2.0      # initial reconnect backoff
RECONNECT_MAX = 60.0      # cap


class FyersWSManager:
    """Owns one Fyers WebSocket connection and reconciles subscriptions."""

    def __init__(self,
                 access_token: Optional[str] = None,
                 mode: str = "lite",
                 tick_ttl: Optional[int] = None,
                 max_symbols: Optional[int] = None):
        self.access_token = access_token or config.FYERS_ACCESS_TOKEN
        self.app_id = config.FYERS_APP_ID
        self.mode = mode  # "lite" (LTP only) or "full" (full depth)
        self.tick_ttl = tick_ttl or config.FYERS_WS_TICK_TTL
        self.max_symbols = max_symbols or config.FYERS_WS_MAX_SYMBOLS_PER_CONN
        self._ws = None
        self._subscribed: set[str] = set()      # Fyers-style symbols
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._reconnect_delay = RECONNECT_BASE

    # ── Public lifecycle ─────────────────────────────────────────────
    def run_forever(self) -> None:
        """Blocking event loop. Run from the WebJob entry script."""
        log.info("FyersWSManager starting (mode=%s, max_symbols=%d)",
                 self.mode, self.max_symbols)
        while not self._stop.is_set():
            try:
                self._connect()
                self._loop()
            except Exception as e:  # noqa: BLE001
                log.exception("FyersWS loop crashed: %s", e)
            if self._stop.is_set():
                break
            delay = min(self._reconnect_delay, RECONNECT_MAX)
            log.warning("FyersWS reconnecting in %.1fs", delay)
            self._stop.wait(delay)
            self._reconnect_delay = min(delay * 2, RECONNECT_MAX)

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close_connection()
            except Exception:  # noqa: BLE001
                pass

    # ── Connection ───────────────────────────────────────────────────
    def _connect(self) -> None:
        """Open the WebSocket. Imports the SDK lazily so this module is
        import-safe even when ``fyers-apiv3`` isn't installed (e.g. on
        the Flask side)."""
        from fyers_apiv3.FyersWebsocket import data_ws  # type: ignore

        access = f"{self.app_id}:{self.access_token}"
        self._ws = data_ws.FyersDataSocket(
            access_token=access,
            log_path="",
            litemode=(self.mode == "lite"),
            write_to_file=False,
            reconnect=False,                # we own reconnect logic
            on_connect=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )
        self._ws.connect()
        # On a fresh connection we lose all subscriptions. Forget them so
        # _reconcile() will re-subscribe from scratch.
        with self._lock:
            self._subscribed.clear()
        self._reconnect_delay = RECONNECT_BASE
        log.info("FyersWS connected")

    # ── Main reconciliation loop ─────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._reconcile()
            except Exception as e:  # noqa: BLE001
                log.warning("FyersWS reconcile failed: %s", e)
            self._stop.wait(POLL_INTERVAL)

    def _reconcile(self) -> None:
        desired_yahoo = ws_subscription.active_symbols()
        # Convert + clamp to per-connection cap.
        desired: list[str] = []
        for sym in desired_yahoo:
            fy = _to_fyers(sym)
            if fy:
                desired.append(fy)
            if len(desired) >= self.max_symbols:
                break
        desired_set = set(desired)
        with self._lock:
            to_add = list(desired_set - self._subscribed)
            to_remove = list(self._subscribed - desired_set)
        if to_add:
            self._subscribe(to_add)
        if to_remove:
            self._unsubscribe(to_remove)

    def _subscribe(self, fy_symbols: Iterable[str]) -> None:
        syms = list(fy_symbols)
        if not syms:
            return
        # Fyers SDK accepts a list of symbols + a data_type.
        data_type = "SymbolUpdate"  # full updates; SDK ignores if litemode
        try:
            self._ws.subscribe(symbols=syms, data_type=data_type)
            with self._lock:
                self._subscribed.update(syms)
            log.info("FyersWS subscribed +%d (total=%d)",
                     len(syms), len(self._subscribed))
        except Exception as e:  # noqa: BLE001
            log.warning("FyersWS subscribe failed: %s", e)

    def _unsubscribe(self, fy_symbols: Iterable[str]) -> None:
        syms = list(fy_symbols)
        if not syms:
            return
        try:
            self._ws.unsubscribe(symbols=syms)
            with self._lock:
                self._subscribed.difference_update(syms)
            log.info("FyersWS unsubscribed -%d (total=%d)",
                     len(syms), len(self._subscribed))
        except Exception as e:  # noqa: BLE001
            log.warning("FyersWS unsubscribe failed: %s", e)

    # ── Callbacks ────────────────────────────────────────────────────
    def _on_open(self) -> None:
        log.info("FyersWS on_open")
        # Force one immediate reconciliation so we don't wait POLL_INTERVAL
        # for the first symbols.
        try:
            self._reconcile()
        except Exception as e:  # noqa: BLE001
            log.debug("FyersWS initial reconcile: %s", e)

    def _on_close(self, *args, **kwargs) -> None:
        log.warning("FyersWS on_close %s %s", args, kwargs)
        # Returning from _loop triggers reconnect in run_forever.

    def _on_error(self, msg) -> None:
        log.warning("FyersWS on_error: %s", msg)
        text = str(msg).lower()
        if "401" in text or "could not authenticate" in text or "invalid token" in text:
            # Token died — try to reload from settings_store, then bounce.
            self._reload_token()

    def _on_message(self, msg) -> None:
        """Normalise a tick and write it to Redis under ``quote:{symbol}``."""
        if not isinstance(msg, dict):
            return
        fy_sym = msg.get("symbol") or msg.get("ticker") or msg.get("symb")
        if not fy_sym:
            return
        yahoo = _from_fyers(fy_sym)
        ltp = msg.get("ltp") or msg.get("last_price") or msg.get("lp")
        if ltp is None:
            return
        try:
            ltp_f = float(ltp)
        except (TypeError, ValueError):
            return
        # Fyers fields vary by mode; pull what's there.
        quote = {
            "symbol": yahoo,
            "price": ltp_f,
            "open": _num(msg.get("open_price") or msg.get("o")),
            "high": _num(msg.get("high_price") or msg.get("h")),
            "low": _num(msg.get("low_price") or msg.get("l")),
            "prev_close": _num(msg.get("prev_close_price") or msg.get("pc")),
            "volume": _num(msg.get("vol_traded_today") or msg.get("v")),
            "change": _num(msg.get("ch")),
            "change_pct": _num(msg.get("chp")),
            "ts": msg.get("exch_feed_time") or msg.get("ts") or int(time.time()),
            "src": "ws",
        }
        try:
            shared_cache.jset(f"quote:{yahoo}", quote, ttl=self.tick_ttl)
        except Exception as e:  # noqa: BLE001
            log.debug("FyersWS jset failed for %s: %s", yahoo, e)

    # ── Token refresh ────────────────────────────────────────────────
    def _reload_token(self) -> None:
        """Re-read the latest token from settings storage and bounce the
        connection. We try fyers_setup's helpers if available; otherwise
        we just trust the env var."""
        try:
            from application.services.providers import fyers_provider
            if hasattr(fyers_provider, "_try_refresh_token"):
                if fyers_provider._try_refresh_token():  # noqa: SLF001
                    self.access_token = config.FYERS_ACCESS_TOKEN
                    log.info("FyersWS reloaded access token")
        except Exception as e:  # noqa: BLE001
            log.warning("FyersWS token refresh failed: %s", e)
        # Force the outer reconnect.
        try:
            if self._ws is not None:
                self._ws.close_connection()
        except Exception:  # noqa: BLE001
            pass


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
