"""Gap-up / gap-down signal history.

Persists the daily gap-outlook prediction shown on the Options Analytics
page and self-evaluates each prediction against the next trading day's
actual open so users can see whether the signal worked.

Storage
-------
Azure Table ``GapSignals`` (configurable via ``GAP_SIGNALS_TABLE``).
    PartitionKey = symbol      (e.g. "NIFTY")
    RowKey       = signal_date (YYYY-MM-DD IST)

Lifecycle
---------
1. ``record(...)``     — called whenever the option-chain payload is
   refreshed during/after the closing window. Upserts the *latest*
   signal for the day; the last write wins, which means the signal
   captured at end-of-day is what gets evaluated.
2. ``evaluate_pending(...)`` — runs at most once per process per hour
   (cheap guard so we don't re-query history on every request). For
   every PENDING row whose date < today, fetches the next-trading-day
   open via market_data and writes the outcome.
3. ``recent(...)``     — list helper for the UI / API.

All public functions swallow Azure / network errors and return safe
defaults so a Tables outage never breaks the option-chain page.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# Only persist a prediction once we have at least this much confidence.
_MIN_RECORD_CONFIDENCE = 0.20

# Threshold (in %) below which the actual move is considered FLAT.
_FLAT_TOLERANCE_PCT = 0.15

# Throttle for evaluate_pending — at most one full sweep per process per hour.
_EVAL_INTERVAL_SECONDS = 3600

_TABLE_NAME = os.getenv("GAP_SIGNALS_TABLE", "GapSignals")

# Yahoo symbols used to fetch the actual open the day after a signal.
_HISTORY_SYMBOL = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
}


def register_history_symbols(mapping: dict) -> None:
    """Register ``{partition_key: yahoo_ticker}`` pairs so next-day-open
    evaluation can resolve the right Yahoo symbol for non-index signals
    (e.g. individual F&O stocks stored under their own partition keys).

    Idempotent; safe to call repeatedly from other services on import.
    """
    try:
        for k, v in (mapping or {}).items():
            if k and v:
                _HISTORY_SYMBOL[str(k)] = str(v)
    except Exception as e:  # noqa: BLE001
        log.debug("gap_history.register_history_symbols: %s", e)


# ── Lazy table client ─────────────────────────────────────────────────

_table_client = None
_table_init_failed = False
_table_lock = threading.Lock()


def _get_table_client():
    """Return an Azure Table client for GapSignals, or None on any error.

    Lazily created on first use so importing this module never raises
    even when Azure config is missing.
    """
    global _table_client, _table_init_failed
    if _table_client is not None or _table_init_failed:
        return _table_client
    with _table_lock:
        if _table_client is not None or _table_init_failed:
            return _table_client
        try:
            from azure.data.tables import TableServiceClient  # type: ignore
            from application.config import AZURE_TABLE_CONN_STR
            if not AZURE_TABLE_CONN_STR:
                log.info("gap_history: AZURE_TABLE_CONN_STR not set; disabled")
                _table_init_failed = True
                return None
            svc = TableServiceClient.from_connection_string(AZURE_TABLE_CONN_STR)
            try:
                svc.create_table_if_not_exists(table_name=_TABLE_NAME)
            except Exception as e:
                log.debug("gap_history: create_table_if_not_exists: %s", e)
            _table_client = svc.get_table_client(table_name=_TABLE_NAME)
            return _table_client
        except Exception as e:
            log.warning("gap_history: table init failed: %s", e)
            _table_init_failed = True
            return None


# ── Helpers ───────────────────────────────────────────────────────────

def _today_ist() -> _dt.date:
    return _dt.datetime.now(_IST).date()


def _is_post_close_or_after_hours() -> bool:
    """Only allow recording between 13:30 and 15:30 IST.

    Post-close and pre-open writes are disabled so a stale refresh
    cannot overwrite the locked decision or corrupt PrevClose.
    """
    now = _dt.datetime.now(_IST)
    mins = now.hour * 60 + now.minute
    market_close = 15 * 60 + 30
    # Only record during the active forecast window: 13:30 → 15:30.
    return (13 * 60 + 30) <= mins <= market_close


# ── Public: record (one upsert per render) ────────────────────────────

def record(
    symbol: str,
    *,
    label: str,
    raw_score: float,
    gap_score: float,
    confidence: float,
    spot: Optional[float],
    expected_gap_points: Optional[float] = None,
    expected_gap_pct: Optional[float] = None,
    expected_gap_points_low: Optional[float] = None,
    expected_gap_points_high: Optional[float] = None,
    probability: Optional[float] = None,
    summary: str = "",
) -> None:
    """Upsert today's prediction for ``symbol``.

    No-op if confidence is below the noise threshold, if the option-chain
    is in the early-session (pre-13:30) phase, or if Azure isn't available.
    """
    try:
        if not symbol or label in (None, "", "TOO EARLY"):
            return
        if (confidence or 0) < _MIN_RECORD_CONFIDENCE:
            return
        if not _is_post_close_or_after_hours():
            return

        client = _get_table_client()
        if client is None:
            return

        sig_date = _today_ist().isoformat()
        # Don't overwrite a row that already has an evaluated outcome —
        # that would lose history. (Shouldn't happen since eval runs
        # only for date < today, but defence in depth.)
        try:
            existing = client.get_entity(partition_key=symbol, row_key=sig_date)
            if existing.get("Outcome") not in (None, "", "PENDING"):
                return
        except Exception:
            existing = None

        entity = {
            "PartitionKey": symbol,
            "RowKey": sig_date,
            "PredictedLabel": label,                       # GAP UP / GAP DOWN / FLAT
            "PredictedScore": float(gap_score or 0),
            "RawScore": float(raw_score or 0),
            "Confidence": float(confidence or 0),
            "PrevClose": float(spot) if spot is not None else None,
            "PredictedGapPoints": (
                int(round(float(expected_gap_points)))
                if expected_gap_points is not None else None
            ),
            "PredictedGapPct": (
                round(float(expected_gap_pct), 3)
                if expected_gap_pct is not None else None
            ),
            "PredictedGapPointsLow": (
                int(round(float(expected_gap_points_low)))
                if expected_gap_points_low is not None else None
            ),
            "PredictedGapPointsHigh": (
                int(round(float(expected_gap_points_high)))
                if expected_gap_points_high is not None else None
            ),
            "Probability": (
                round(float(probability), 1)
                if probability is not None else None
            ),
            "CapturedAt": _dt.datetime.now(_IST).isoformat(timespec="seconds"),
            "Summary": (summary or "")[:512],
            "Outcome": "PENDING",
            "ActualOpen": None,
            "ActualGapPct": None,
            "ActualGapPoints": None,
            "ActualDirection": None,
            "EvaluatedAt": None,
        }
        client.upsert_entity(entity=entity, mode="merge")
    except Exception as e:
        log.debug("gap_history.record(%s): %s", symbol, e)


# ── Public: evaluate pending rows against next-day actual open ────────

_last_eval_at: dict[str, float] = {}


def evaluate_pending(symbol: str, *, force: bool = False) -> int:
    """For every PENDING signal older than today, fill in the actual
    next-trading-day open and outcome. Returns number of rows updated.

    Throttled to once per process per ``_EVAL_INTERVAL_SECONDS``.
    """
    try:
        now = time.time()
        if not force and (now - _last_eval_at.get(symbol, 0)) < _EVAL_INTERVAL_SECONDS:
            return 0

        client = _get_table_client()
        if client is None:
            return 0

        _last_eval_at[symbol] = now
        today = _today_ist()

        try:
            rows = list(client.query_entities(
                query_filter=f"PartitionKey eq '{symbol}' and Outcome eq 'PENDING'"
            ))
        except Exception as e:
            log.debug("gap_history: query pending failed: %s", e)
            return 0

        rows = [r for r in rows if (r.get("RowKey") or "") < today.isoformat()]
        if not rows:
            return 0

        # Fetch enough history to cover the oldest pending row.
        try:
            oldest = min(_dt.date.fromisoformat(r["RowKey"]) for r in rows)
        except Exception:
            oldest = today - _dt.timedelta(days=30)
        days_needed = max(15, (today - oldest).days + 5)

        history = _load_history(symbol, days=days_needed)
        if history is None or history.empty:
            return 0

        updated = 0
        for r in rows:
            try:
                sig_date = _dt.date.fromisoformat(r["RowKey"])
                next_open, next_date = _next_session_open(history, sig_date)
                if next_open is None:
                    continue  # next session not yet available

                prev_close = r.get("PrevClose")
                # Fall back to the close of the signal day from history.
                if prev_close in (None, 0):
                    prev_close = _close_for_date(history, sig_date)
                if not prev_close:
                    continue

                # Prefer the official close from Yahoo history over the
                # live-captured spot (eliminates cross-source price bias).
                hist_close = _close_for_date(history, sig_date)
                ref_close = hist_close if hist_close else float(prev_close)

                gap_pct = (next_open - ref_close) / ref_close * 100.0
                gap_points = float(next_open) - ref_close
                if gap_pct >= _FLAT_TOLERANCE_PCT:
                    actual_dir = "GAP UP"
                elif gap_pct <= -_FLAT_TOLERANCE_PCT:
                    actual_dir = "GAP DOWN"
                else:
                    actual_dir = "FLAT"

                predicted = (r.get("PredictedLabel") or "").upper()
                if predicted in ("GAP UP", "GAP DOWN"):
                    # Direction-match grading: HIT if predicted direction
                    # matches actual direction OR actual is within dead-band.
                    if predicted == actual_dir:
                        outcome = "HIT"
                    elif actual_dir == "FLAT":
                        # Actual gap is tiny; predicted direction not wrong,
                        # just not big enough — soft miss, not hard failure.
                        outcome = "NEAR"
                    else:
                        outcome = "MISS"
                else:
                    # Predicted FLAT (no clear bias) — count flat as correct.
                    outcome = "FLAT_OK" if actual_dir == "FLAT" else "FLAT_MISS"

                update = {
                    "PartitionKey": symbol,
                    "RowKey": r["RowKey"],
                    "ActualOpen": float(next_open),
                    "ActualGapPct": round(gap_pct, 3),
                    "ActualGapPoints": round(gap_points, 2),
                    "ActualDirection": actual_dir,
                    "ActualDate": next_date.isoformat(),
                    "Outcome": outcome,
                    "EvaluatedAt": _dt.datetime.now(_IST).isoformat(timespec="seconds"),
                }
                client.upsert_entity(entity=update, mode="merge")
                updated += 1
            except Exception as e:
                log.debug("gap_history.eval row %s: %s", r.get("RowKey"), e)
        if updated:
            log.info("gap_history: evaluated %d pending signal(s) for %s",
                     updated, symbol)
        return updated
    except Exception as e:
        log.debug("gap_history.evaluate_pending(%s): %s", symbol, e)
        return 0


def _load_history(symbol: str, days: int = 30):
    """Daily OHLC history for ``symbol`` via the market_data abstraction."""
    try:
        from application.services import market_data
        yf_sym = _HISTORY_SYMBOL.get(symbol, symbol)
        # fresh=True bypasses the daily OHLC cache (which never holds today's
        # row and tolerates up to 4 days of staleness) so grading sees the
        # latest closed session — and today's open — without lag.
        df = market_data.get_history(yf_sym, days=days, interval="1d", fresh=True)
        if df is None or df.empty:
            return None
        # Normalise index to date for easy lookup.
        if "Date" in df.columns:
            df = df.set_index("Date")
        df = df.sort_index()
        return df
    except Exception as e:
        log.debug("gap_history: history fetch failed: %s", e)
        return None


def _next_session_open(history, sig_date: _dt.date):
    """Return (open_price, session_date) for the first session strictly
    after ``sig_date``, or (None, None) if not yet available."""
    try:
        for ts, row in history.iterrows():
            try:
                d = ts.date() if hasattr(ts, "date") else _dt.date.fromisoformat(str(ts)[:10])
            except Exception:
                continue
            if d > sig_date:
                op = row.get("Open")
                if op is not None and not _is_nan(op):
                    return float(op), d
        return None, None
    except Exception:
        return None, None


def _close_for_date(history, sig_date: _dt.date):
    try:
        for ts, row in history.iterrows():
            try:
                d = ts.date() if hasattr(ts, "date") else _dt.date.fromisoformat(str(ts)[:10])
            except Exception:
                continue
            if d == sig_date:
                cl = row.get("Close")
                if cl is not None and not _is_nan(cl):
                    return float(cl)
        return None
    except Exception:
        return None


def _is_nan(v) -> bool:
    try:
        return v != v  # NaN check without importing math/pandas
    except Exception:
        return False


# ── Public: read recent rows for the UI ───────────────────────────────

def recent(symbol: str, limit: int = 14) -> list[dict]:
    """Return up to ``limit`` most recent signals for ``symbol`` (newest first)."""
    try:
        client = _get_table_client()
        if client is None:
            return []
        rows = list(client.query_entities(
            query_filter=f"PartitionKey eq '{symbol}'"
        ))
        rows.sort(key=lambda r: r.get("RowKey") or "", reverse=True)
        rows = rows[:max(1, int(limit))]

        # Reuse the same points-estimation logic used in option_chain so
        # historical rows (created before points fields existed) still show
        # comparable predicted points in the UI.
        try:
            from application.services.option_chain import _estimate_gap_points  # type: ignore
        except Exception:
            _estimate_gap_points = None

        out = []
        for r in rows:
            predicted = (r.get("PredictedLabel") or "").upper()
            predicted_pts = r.get("PredictedGapPoints")
            predicted_pct = r.get("PredictedGapPct")
            predicted_lo = r.get("PredictedGapPointsLow")
            predicted_hi = r.get("PredictedGapPointsHigh")
            prev_close = r.get("PrevClose")

            if (
                _estimate_gap_points is not None
                and prev_close not in (None, 0)
                and predicted_pts in (None, "")
            ):
                try:
                    est = _estimate_gap_points(
                        spot=float(prev_close),
                        label=predicted,
                        gap_score=float(r.get("PredictedScore") or 0),
                        confidence=float(r.get("Confidence") or 0),
                        vix_change_pct=0.0,
                    )
                    predicted_pts = est.get("expected_gap_points")
                    predicted_pct = est.get("expected_gap_pct")
                    predicted_lo = est.get("expected_gap_points_low")
                    predicted_hi = est.get("expected_gap_points_high")
                except Exception:
                    pass

            actual_open = r.get("ActualOpen")
            actual_points = r.get("ActualGapPoints")
            if actual_points in (None, "") and actual_open not in (None, "") and prev_close not in (None, "", 0):
                try:
                    actual_points = round(float(actual_open) - float(prev_close), 2)
                except Exception:
                    actual_points = None

            points_error = None
            if predicted_pts not in (None, "") and actual_points not in (None, ""):
                try:
                    points_error = round(float(actual_points) - float(predicted_pts), 2)
                except Exception:
                    points_error = None

            out.append({
                "date": r.get("RowKey"),
                "predicted": r.get("PredictedLabel"),
                "predicted_score": r.get("PredictedScore"),
                "confidence": r.get("Confidence"),
                "prev_close": prev_close,
                "predicted_gap_points": predicted_pts,
                "predicted_gap_pct": predicted_pct,
                "predicted_gap_points_low": predicted_lo,
                "predicted_gap_points_high": predicted_hi,
                "probability": r.get("Probability"),
                "captured_at": r.get("CapturedAt"),
                "actual_open": actual_open,
                "actual_gap_pct": r.get("ActualGapPct"),
                "actual_gap_points": actual_points,
                "points_error": points_error,
                "actual_direction": r.get("ActualDirection"),
                "actual_date": r.get("ActualDate"),
                "outcome": r.get("Outcome") or "PENDING",
                "summary": r.get("Summary"),
            })
        return out
    except Exception as e:
        log.debug("gap_history.recent(%s): %s", symbol, e)
        return []


def stats(symbol: str, lookback: int = 60) -> dict:
    """Hit-rate stats over the last ``lookback`` directional signals."""
    items = recent(symbol, limit=lookback)
    directional = [i for i in items if (i.get("predicted") or "").upper() in ("GAP UP", "GAP DOWN")
                                       and i.get("outcome") in ("HIT", "MISS")]
    hits = sum(1 for i in directional if i["outcome"] == "HIT")
    total = len(directional)
    return {
        "total_signals": len(items),
        "directional_evaluated": total,
        "hits": hits,
        "misses": total - hits,
        "hit_rate_pct": round(hits / total * 100.0, 1) if total else None,
        "pending": sum(1 for i in items if i.get("outcome") == "PENDING"),
    }
