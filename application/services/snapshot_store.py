"""Persistent tab-snapshot store backed by Azure Table Storage.

Why this exists
---------------
Heavy "non-live" analytics tabs (chart patterns, sector rotation, near-52w
high, breakouts, swing scanner, F&O gap forecast, …) re-fetch a lot of
upstream data (yfinance / NSE / brokers) every time they are opened. That is
slow, gets rate-limited on cloud IPs, and burns the Redis command budget.

This module persists the *last computed result* of each such tab in Azure
Table Storage so it can be served again and again without recomputing, until
someone explicitly refreshes it. The behaviour, per the product requirement:

* **Refresh clicked** → rebuild live, overwrite the stored snapshot, return it.
* **Market closed** (after 15:30 IST, weekends, NSE holidays) → always serve
  the stored snapshot; only build live if no snapshot exists yet (then store).
* **Market open**:
    * ``live=True`` tabs  → always rebuild live (and keep the snapshot fresh).
    * ``live=False`` tabs → serve the stored snapshot if present, else build
      once and store it.

A short Redis read-through cache sits in front of Table Storage so repeated
opens within a minute don't hit the table on every request.

The snapshots are **global** (one row per tab, shared by all users).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Any, Callable, Optional

from application.config import AZURE_TABLE_CONN_STR
from application.services import cache as shared_cache

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# ── Configuration ───────────────────────────────────────────────────────
_TABLE_NAME = os.getenv("SNAPSHOT_TABLE", "TabSnapshots")
_PARTITION = "SNAP"
# Azure Table string properties cap at 32K UTF-16 chars; chunk well under it.
_CHUNK_CHARS = 30_000
# Short read-through cache so we don't query the table on every open.
_READ_CACHE_TTL = 120

# Market session (IST). Trading window 09:15 – 15:30.
_OPEN_MIN = 9 * 60 + 15
_CLOSE_MIN = 15 * 60 + 30

# NSE trading holidays. Weekends are handled separately. This list should be
# refreshed yearly; it can also be overridden/extended via the NSE_HOLIDAYS
# env var (comma-separated YYYY-MM-DD). Dates here are full-day market closes.
_DEFAULT_HOLIDAYS = {
    # 2025
    "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14",
    "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27", "2025-10-02",
    "2025-10-21", "2025-10-22", "2025-11-05", "2025-12-25",
    # 2026 (update when NSE publishes the official calendar)
    "2026-01-26", "2026-02-15", "2026-03-04", "2026-03-21", "2026-04-01",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-08-15", "2026-10-02",
    "2026-10-20", "2026-11-09", "2026-12-25",
}


def _holidays() -> set[str]:
    extra = os.getenv("NSE_HOLIDAYS", "")
    out = set(_DEFAULT_HOLIDAYS)
    for d in extra.split(","):
        d = d.strip()
        if d:
            out.add(d)
    return out


def _now_ist() -> _dt.datetime:
    return _dt.datetime.now(_IST)


def is_market_open(now: Optional[_dt.datetime] = None) -> bool:
    """True only during NSE cash-session hours on a trading day (IST)."""
    now = now or _now_ist()
    if now.weekday() >= 5:                       # Sat / Sun
        return False
    if now.strftime("%Y-%m-%d") in _holidays():  # NSE holiday
        return False
    minutes = now.hour * 60 + now.minute
    return _OPEN_MIN <= minutes <= _CLOSE_MIN


# ── Azure Table client (safe fallback when unconfigured) ────────────────
_table_client = None
if AZURE_TABLE_CONN_STR:
    try:
        from azure.data.tables import TableServiceClient

        _svc = TableServiceClient.from_connection_string(conn_str=AZURE_TABLE_CONN_STR)
        try:
            _svc.create_table_if_not_exists(table_name=_TABLE_NAME)
        except Exception as e:  # noqa: BLE001
            log.debug("snapshot_store: create table skipped: %s", e)
        _table_client = _svc.get_table_client(table_name=_TABLE_NAME)
        log.info("snapshot_store: Azure Table '%s' ready", _TABLE_NAME)
    except Exception as e:  # noqa: BLE001
        log.warning("snapshot_store: Azure Tables init failed (%s); snapshots disabled", e)
        _table_client = None
else:
    log.info("snapshot_store: AZURE_TABLE_CONN_STR not set; snapshots disabled")


def enabled() -> bool:
    return _table_client is not None


def _row_key(key: str) -> str:
    # RowKey may not contain / \ # ? or control chars.
    safe = key.replace("/", "_").replace("\\", "_").replace("#", "_").replace("?", "_")
    return safe[:512]


def _read_cache_key(key: str) -> str:
    return f"snap:{key}"


# ── Persistence primitives ──────────────────────────────────────────────
def put(key: str, payload: Any, as_of: Optional[str] = None) -> None:
    """Persist ``payload`` (JSON-serialisable) as the snapshot for ``key``."""
    as_of = as_of or _now_ist().strftime("%d %b %Y, %I:%M %p IST")
    # Refresh the fast read-through cache regardless of table availability.
    try:
        shared_cache.jset(_read_cache_key(key),
                          {"payload": payload, "as_of": as_of},
                          ttl=_READ_CACHE_TTL)
    except Exception:
        pass
    if _table_client is None:
        return
    try:
        blob = json.dumps(payload, default=str)
        # Azure Table entities cap at ~1 MB total. Leave headroom; if the
        # payload is too large we keep only the Redis read-through copy.
        if len(blob.encode("utf-8")) > 900_000:
            log.warning("snapshot_store.put(%s) skipped: payload too large (%d bytes)",
                        key, len(blob))
            return
        chunks = [blob[i:i + _CHUNK_CHARS] for i in range(0, len(blob), _CHUNK_CHARS)] or [""]
        entity: dict[str, Any] = {
            "PartitionKey": _PARTITION,
            "RowKey": _row_key(key),
            "AsOf": as_of,
            "Built": _now_ist().isoformat(),
            "Chunks": len(chunks),
        }
        for i, ch in enumerate(chunks):
            entity[f"Data{i}"] = ch
        _table_client.upsert_entity(entity=entity)
    except Exception as e:  # noqa: BLE001
        log.warning("snapshot_store.put(%s) failed: %s", key, e)


def get(key: str) -> Optional[dict]:
    """Return ``{"payload":…, "as_of":…}`` for ``key`` or None if absent."""
    cached = None
    try:
        cached = shared_cache.jget(_read_cache_key(key))
    except Exception:
        cached = None
    if isinstance(cached, dict) and "payload" in cached:
        return cached
    if _table_client is None:
        return None
    try:
        ent = _table_client.get_entity(partition_key=_PARTITION, row_key=_row_key(key))
    except Exception:
        return None
    try:
        n = int(ent.get("Chunks") or 0)
        blob = "".join(ent.get(f"Data{i}", "") or "" for i in range(n))
        payload = json.loads(blob) if blob else None
        if payload is None:
            return None
        result = {"payload": payload, "as_of": ent.get("AsOf")}
        try:
            shared_cache.jset(_read_cache_key(key), result, ttl=_READ_CACHE_TTL)
        except Exception:
            pass
        return result
    except Exception as e:  # noqa: BLE001
        log.warning("snapshot_store.get(%s) decode failed: %s", key, e)
        return None


def _tag(payload: Any, *, snapshot: bool, as_of: Optional[str], source: str) -> Any:
    """Attach snapshot metadata to a dict payload (non-dicts pass through)."""
    if isinstance(payload, dict):
        out = dict(payload)
        out["snapshot"] = snapshot
        out["snapshot_source"] = source
        if as_of:
            out["snapshot_as_of"] = as_of
        return out
    return payload


# ── Public orchestrator ─────────────────────────────────────────────────
def serve_or_refresh(
    key: str,
    builder: Callable[[], Any],
    *,
    live: bool = False,
    force: bool = False,
) -> Any:
    """Serve ``key`` from storage or rebuild via ``builder`` per the rules.

    ``builder`` must compute and return the fresh payload (no caching of its
    own). ``live`` marks tabs that must always rebuild during market hours.
    """
    market_open = is_market_open()

    def _is_storable(payload: Any) -> bool:
        # Never persist error/empty results — keep the last good snapshot.
        if isinstance(payload, dict) and payload.get("error"):
            return False
        return True

    def _build_and_store(source: str) -> Any:
        payload = builder()
        if _is_storable(payload):
            try:
                put(key, payload)
            except Exception:
                pass
        return _tag(payload, snapshot=False, as_of=None, source=source)

    # 1. Explicit refresh always rebuilds live and overwrites the snapshot.
    if force:
        return _build_and_store("refresh")

    # 2. Market closed → serve stored snapshot; build once only if missing.
    if not market_open:
        snap = get(key)
        if snap is not None:
            return _tag(snap["payload"], snapshot=True,
                        as_of=snap.get("as_of"), source="stored")
        return _build_and_store("live")

    # 3. Market open + live tab → always rebuild (keep snapshot fresh too).
    if live:
        return _build_and_store("live")

    # 4. Market open + non-live tab → stored snapshot wins until refreshed.
    snap = get(key)
    if snap is not None:
        return _tag(snap["payload"], snapshot=True,
                    as_of=snap.get("as_of"), source="stored")
    return _build_and_store("live")
