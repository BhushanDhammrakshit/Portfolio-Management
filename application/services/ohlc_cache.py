"""Persistent daily OHLC cache backed by Azure Table Storage.

Daily candles are immutable once the trading day closes, so we persist
them in an Azure Table and only ask the broker for what we don't already
have. The first scan of the day repopulates today's row; every
subsequent call hits the table and pays zero broker quota.

PartitionKey:  normalized symbol (e.g. ``RELIANCE.NS`` → ``RELIANCE.NS``;
               ``^NSEI`` → ``IDX_NSEI`` because ``^`` is awkward in URLs).
RowKey:        ``YYYYMMDD`` — fixed-width, lexicographically sortable.
Columns:       Open, High, Low, Close, Volume.

Only ``interval == "1d"`` is cached. Intraday / minute data is volatile
and not worth persisting at this scale.

Today's row is NEVER persisted — it can change every minute during
trading hours. After 15:35 IST it becomes stable; the scheduled
overnight precompute job re-fetches and writes it once.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import pandas as pd

from application import config

log = logging.getLogger(__name__)

# ── Lazy Azure Table client ────────────────────────────────────────────
_table_client = None
_table_lock = threading.Lock()


def _ensure_client():
    """Create the ``OhlcDaily`` table client on first use. Returns None
    when Azure isn't configured so callers fall back to direct fetch."""
    global _table_client
    if _table_client is not None:
        return _table_client if _table_client != "missing" else None
    with _table_lock:
        if _table_client is not None:
            return _table_client if _table_client != "missing" else None
        conn = getattr(config, "AZURE_TABLE_CONN_STR", "")
        table_name = getattr(config, "OHLC_TABLE", "OhlcDaily")
        if not conn or not table_name:
            _table_client = "missing"
            return None
        try:
            from azure.data.tables import TableServiceClient  # type: ignore
            svc = TableServiceClient.from_connection_string(conn_str=conn)
            try:
                svc.create_table_if_not_exists(table_name=table_name)
            except Exception as e:  # noqa: BLE001
                log.debug("[ohlc] create table skipped: %s", e)
            _table_client = svc.get_table_client(table_name=table_name)
            return _table_client
        except Exception as e:  # noqa: BLE001
            log.warning("[ohlc] table client init failed, cache disabled: %s", e)
            _table_client = "missing"
            return None


# ── Negative-result cache (in-process) ─────────────────────────────────
# When a symbol returns empty from the provider (delisted / typo), avoid
# re-asking for `OHLC_NEGATIVE_TTL` seconds.
_neg_cache: dict[str, float] = {}
_neg_lock = threading.Lock()


def _is_neg_cached(symbol: str) -> bool:
    with _neg_lock:
        exp = _neg_cache.get(symbol, 0.0)
        if exp > time.time():
            return True
        if exp:
            _neg_cache.pop(symbol, None)
        return False


def _mark_neg(symbol: str) -> None:
    ttl = getattr(config, "OHLC_NEGATIVE_TTL", 900)
    with _neg_lock:
        _neg_cache[symbol] = time.time() + ttl


# ── Helpers ───────────────────────────────────────────────────────────
def _ist_today() -> date:
    # IST is UTC+5:30; no DST so a fixed offset is fine.
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()


def _market_closed_today() -> bool:
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    if now_ist.weekday() >= 5:  # Sat/Sun
        return True
    # NSE close 15:30; allow 5 min for the broker to finalise the last bar.
    return now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 35)


def _expected_latest_business_day() -> date:
    """Most recent date for which a daily close *should* exist."""
    d = _ist_today()
    if not _market_closed_today():
        # Trading still in progress (or pre-open) → we expect previous biz day.
        d = d - timedelta(days=1)
    # Skip back over weekends. Holidays we can't know without a calendar;
    # the staleness tolerance below covers them.
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _normalize_symbol(symbol: str) -> str:
    """Make a symbol safe for an Azure Table PartitionKey.

    Forbidden chars in keys: ``/`` ``\\`` ``#`` ``?`` and control chars.
    ``.`` is permitted and we keep it so ``RELIANCE.NS`` round-trips.
    """
    s = (symbol or "").strip()
    if not s:
        return s
    if s.startswith("^"):
        s = "IDX_" + s[1:]
    return s.replace("/", "_").replace("\\", "_").replace("#", "_").replace("?", "_")


def _date_to_rowkey(d: date) -> str:
    return d.strftime("%Y%m%d")


def _rowkey_to_date(rk: str) -> Optional[date]:
    try:
        return datetime.strptime(rk, "%Y%m%d").date()
    except Exception:  # noqa: BLE001
        return None


# ── Read ──────────────────────────────────────────────────────────────
def _load(symbol: str, start: date, end: date) -> Optional[pd.DataFrame]:
    """Return a DataFrame of cached candles in ``[start, end]`` (inclusive),
    or None when the cache is unavailable. Empty DataFrame ≠ unavailable —
    it just means we haven't cached this symbol yet."""
    client = _ensure_client()
    if client is None:
        return None
    pk = _normalize_symbol(symbol)
    rk_lo = _date_to_rowkey(start)
    rk_hi = _date_to_rowkey(end)
    qf = (f"PartitionKey eq '{pk}' and RowKey ge '{rk_lo}' "
          f"and RowKey le '{rk_hi}'")
    rows = []
    try:
        for ent in client.query_entities(query_filter=qf):
            d = _rowkey_to_date(ent.get("RowKey", ""))
            if d is None:
                continue
            rows.append({
                "Date": pd.Timestamp(d),
                "Open": float(ent.get("Open", 0.0) or 0.0),
                "High": float(ent.get("High", 0.0) or 0.0),
                "Low": float(ent.get("Low", 0.0) or 0.0),
                "Close": float(ent.get("Close", 0.0) or 0.0),
                "Volume": float(ent.get("Volume", 0.0) or 0.0),
            })
    except Exception as e:  # noqa: BLE001
        log.warning("[ohlc] read failed for %s: %s", symbol, e)
        return None
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame(rows).sort_values("Date").set_index("Date")
    return df[["Open", "High", "Low", "Close", "Volume"]]


# ── Write ─────────────────────────────────────────────────────────────
def _save(symbol: str, df: pd.DataFrame) -> int:
    """Upsert every closed row (i.e. excluding today) into the table.
    Returns count of rows written."""
    if df is None or df.empty:
        return 0
    client = _ensure_client()
    if client is None:
        return 0
    pk = _normalize_symbol(symbol)
    today = _ist_today()

    # Some provider DataFrames have a DatetimeIndex, others have a "Date" col.
    if "Date" in df.columns and not isinstance(df.index, pd.DatetimeIndex):
        idx = pd.to_datetime(df["Date"])
    else:
        try:
            idx = pd.to_datetime(df.index)
        except Exception:  # noqa: BLE001
            log.debug("[ohlc] %s: cannot interpret index, skip save", symbol)
            return 0

    entities = []
    for ts, row in zip(idx, df.itertuples(index=False)):
        d = ts.date() if hasattr(ts, "date") else ts
        if not isinstance(d, date):
            continue
        if d >= today:
            continue  # never persist today's (or future) row
        try:
            entity = {
                "PartitionKey": pk,
                "RowKey": _date_to_rowkey(d),
                "Open": float(getattr(row, "Open", 0.0) or 0.0),
                "High": float(getattr(row, "High", 0.0) or 0.0),
                "Low": float(getattr(row, "Low", 0.0) or 0.0),
                "Close": float(getattr(row, "Close", 0.0) or 0.0),
                "Volume": float(getattr(row, "Volume", 0.0) or 0.0),
            }
        except (TypeError, ValueError):
            continue
        entities.append(entity)

    if not entities:
        return 0

    written = 0
    # Azure Table batch: max 100 entities, all same PartitionKey, all same op.
    for i in range(0, len(entities), 100):
        chunk = entities[i:i + 100]
        try:
            client.submit_transaction([("upsert", e) for e in chunk])
            written += len(chunk)
        except Exception as e:  # noqa: BLE001
            # Fall back to one-by-one so a single bad row doesn't kill the batch.
            log.debug("[ohlc] batch upsert failed for %s, retrying singles: %s",
                      symbol, e)
            for e_ in chunk:
                try:
                    client.upsert_entity(entity=e_)
                    written += 1
                except Exception as ee:  # noqa: BLE001
                    log.debug("[ohlc] single upsert failed: %s", ee)
    return written


# ── Public API ────────────────────────────────────────────────────────
def get_history_cached(
    symbol: str,
    days: int,
    interval: str,
    fetch_fn: Callable[[], Optional[pd.DataFrame]],
) -> Optional[pd.DataFrame]:
    """Return up to ``days`` of daily candles for ``symbol``, served from
    Azure Table whenever fresh enough. Falls back transparently to
    ``fetch_fn()`` (the underlying provider call) when:
      • caching is disabled / Azure isn't configured
      • the requested interval isn't ``1d``
      • the table doesn't yet have enough fresh data
    Fresh fetches are persisted back to the table for next time.
    """
    if interval not in ("1d", "D", "day", "daily"):
        return fetch_fn()
    if not getattr(config, "OHLC_CACHE_ENABLED", True):
        return fetch_fn()

    today = _ist_today()
    pad = max(7, int(days * 0.4))
    start = today - timedelta(days=int(days) + pad)
    cached = _load(symbol, start, today)

    expected_latest = _expected_latest_business_day()

    def _cache_is_usable(c: Optional[pd.DataFrame]) -> bool:
        if c is None or c.empty:
            return False
        latest = c.index.max()
        latest_d = latest.date() if hasattr(latest, "date") else latest
        # Allow up to 4 calendar days of staleness to absorb holidays.
        return latest_d >= (expected_latest - timedelta(days=4))

    if _cache_is_usable(cached):
        # Window slice: caller asked for `days` calendar days back.
        cutoff = today - timedelta(days=int(days))
        out = cached[cached.index >= pd.Timestamp(cutoff)]
        return out if not out.empty else cached

    # Cache miss or stale → fetch live, persist, return.
    if _is_neg_cached(symbol):
        return cached  # may be empty; better than another wasted call

    df = fetch_fn()
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        _mark_neg(symbol)
        return df

    try:
        _save(symbol, df)
    except Exception as e:  # noqa: BLE001
        log.debug("[ohlc] save failed for %s: %s", symbol, e)
    return df


def warm(symbol: str, days: int = 365) -> int:
    """Force a fetch + persist for ``symbol``. Used by the overnight
    precompute job to seed/refresh the scanner universe and every user's
    portfolio holdings.

    Uses the explicit start/end ``download_history`` path (not the per-symbol
    ``get_history``): Fyers' ``get_history`` inflates the requested window by
    ~1.6x and then trips the broker's 366-day cap for a full year, whereas
    ``download_history`` passes exact dates and both the primary and the
    configured fallback are tried transparently — so a single expired broker
    token doesn't leave the cache empty for the whole trading day."""
    from application.services import market_data
    end = datetime.utcnow().date()
    # Stay safely under the broker's 366-day range limit.
    start = end - timedelta(days=min(int(days), 360))
    df = None
    try:
        res = market_data.download_history([symbol], start, end, interval="1d")
        df = res.get(symbol) if isinstance(res, dict) else None
    except Exception as e:  # noqa: BLE001
        log.debug("[ohlc] warm download failed for %s: %s", symbol, e)
    if df is None or df.empty:
        _mark_neg(symbol)
        return 0
    return _save(symbol, df)


def load_cached(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Return whatever daily candles we already have for ``symbol``,
    ignoring staleness. Used as a last resort by the analytics dashboard so
    it can still render from history captured on an earlier trading day even
    when every live source (expired broker token, IP-blocked yfinance) fails
    right now. Returns None when caching is unavailable."""
    today = _ist_today()
    start = today - timedelta(days=int(days) + 10)
    return _load(symbol, start, today)
