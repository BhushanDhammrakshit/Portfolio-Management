"""NIFTY option-chain fetcher + OI-shift trend indicator.

Primary source : Fyers API v3 (`/data/options-chain-v3`) — works from
                 any server because it uses the user's broker token.
Fallback       : public NSE endpoint (works from Indian residential IPs;
                 commonly blocked from cloud / non-IN datacentre IPs).

Strategy (user-defined):
    * OI shifting from CE → PE (Δ Put-OI ≫ Δ Call-OI) → BEARISH (crash risk)
    * OI shifting from PE → CE (Δ Call-OI ≫ Δ Put-OI) → BULLISH (rally)
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from application import config
from application.services import cache as shared_cache

log = logging.getLogger(__name__)

_TIMEOUT = 12

_CACHE_KEY = "optionchain:nifty:v2"
_CACHE_TTL = 60

_PREV_KEY = "optionchain:nifty:prev:v2"
_PREV_TTL = 30 * 60

# Stale fallback: last known-good payload, served when both upstream
# providers fail. TTL is generous so we can still degrade gracefully
# overnight; the UI flags any stale serve so the trader knows.
_STALE_KEY = "optionchain:nifty:stale:v2"
_STALE_TTL = 12 * 3600
_STALE_STORE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "_optionchain_last.json",
)
_STALE_STORE_LOCK = threading.Lock()
# Beyond this age, we still serve the snapshot but mark it as "very stale"
# so the UI can warn more aggressively.
_STALE_HARD_AGE_SEC = 6 * 3600

# Per-day intraday OI series — one point per minute, kept until end of day.
_SERIES_KEY_FMT = "optionchain:nifty:series:{date}"
_SERIES_TTL = 16 * 3600   # keep one session; auto-expires overnight
_SERIES_MAX_POINTS = 400  # ~375 minute-samples in a session + buffer
_SERIES_STORE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "_optionchain_oi_series.json",
)
_SERIES_STORE_LOCK = threading.Lock()

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _load_series_from_store(day_key: str) -> List[Dict[str, Any]]:
    """Read intraday OI series for ``day_key`` from durable backend store.

    This survives app restarts and fills chart history even when cache has
    been evicted or the process is recycled.
    """
    with _SERIES_STORE_LOCK:
        try:
            if not os.path.exists(_SERIES_STORE_FILE):
                return []
            with open(_SERIES_STORE_FILE, "r", encoding="utf-8") as f:
                blob = json.load(f)
            rows = blob.get(day_key) if isinstance(blob, dict) else None
            if not isinstance(rows, list):
                return []
            return rows[-_SERIES_MAX_POINTS:]
        except Exception:
            return []


def _save_series_to_store(day_key: str, rows: List[Dict[str, Any]]) -> None:
    """Persist intraday OI series to disk atomically.

    Keeps only a few recent sessions to bound file size.
    """
    with _SERIES_STORE_LOCK:
        try:
            blob: Dict[str, Any] = {}
            if os.path.exists(_SERIES_STORE_FILE):
                try:
                    with open(_SERIES_STORE_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        blob = data
                except Exception:
                    blob = {}

            blob[day_key] = rows[-_SERIES_MAX_POINTS:]
            # Keep only last 5 date buckets (YYYYMMDD lexical sort works).
            keys = sorted(k for k in blob.keys() if isinstance(k, str) and len(k) == 8)
            for old_k in keys[:-5]:
                blob.pop(old_k, None)

            os.makedirs(os.path.dirname(_SERIES_STORE_FILE), exist_ok=True)
            tmp = _SERIES_STORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=True, separators=(",", ":"))
            os.replace(tmp, _SERIES_STORE_FILE)
        except Exception:
            pass


def _load_stale_snapshot() -> Optional[Dict[str, Any]]:
    """Read the last-good payload snapshot from disk.

    Survives process restarts and Redis evictions; this is the last line
    of defense when both providers are down on a cold-booted worker.
    """
    with _STALE_STORE_LOCK:
        try:
            if not os.path.exists(_STALE_STORE_FILE):
                return None
            with open(_STALE_STORE_FILE, "r", encoding="utf-8") as f:
                blob = json.load(f)
            if isinstance(blob, dict) and blob.get("payload"):
                return blob
        except Exception:
            return None
    return None


def _save_stale_snapshot(payload: Dict[str, Any]) -> None:
    """Persist the last-good payload atomically to disk."""
    with _STALE_STORE_LOCK:
        try:
            blob = {
                "saved_at": time.time(),
                "payload": payload,
            }
            os.makedirs(os.path.dirname(_STALE_STORE_FILE), exist_ok=True)
            tmp = _STALE_STORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=True, separators=(",", ":"))
            os.replace(tmp, _STALE_STORE_FILE)
        except Exception:
            pass


def _build_stale_response(blob: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Wrap a stale payload with degradation metadata for the UI."""
    payload = dict(blob.get("payload") or {})
    saved_at = float(blob.get("saved_at") or 0)
    age_sec = max(0, int(time.time() - saved_at)) if saved_at else None
    payload["stale"] = True
    payload["stale_reason"] = reason or "Upstream feed unavailable."
    payload["stale_age_sec"] = age_sec
    payload["stale_saved_at"] = int(saved_at) if saved_at else None
    if age_sec is not None and age_sec > _STALE_HARD_AGE_SEC:
        payload["stale_severity"] = "high"
    else:
        payload["stale_severity"] = "warn"
    return payload


# ── Public API ─────────────────────────────────────────────────────────

def get_nifty_option_chain(force_refresh: bool = False) -> Dict[str, Any]:
    if not force_refresh:
        cached = shared_cache.jget(_CACHE_KEY)
        if isinstance(cached, dict):
            # If cached payload missed/evicted the series, recover it from
            # cache-by-day or durable backend store so chart history survives.
            ser = cached.get("series")
            if not isinstance(ser, list) or not ser:
                now_ist = _dt.datetime.now(_IST)
                day_key = now_ist.strftime("%Y%m%d")
                series_key = _SERIES_KEY_FMT.format(date=day_key)
                series = shared_cache.jget(series_key)
                if not isinstance(series, list) or not series:
                    series = _load_series_from_store(day_key)
                    if series:
                        shared_cache.jset(series_key, series, ttl=_SERIES_TTL)
                if isinstance(series, list) and series:
                    cached = {**cached, "series": series}
            return {**cached, "cached": True}

    payload, src_error = _fetch_payload()
    if payload is None:
        # ── Failsafe ──
        # Both providers failed. Serve the last-known-good snapshot from
        # the stale cache (Redis first, disk second) so the UI keeps
        # working in a clearly-flagged degraded mode.
        stale_blob = shared_cache.jget(_STALE_KEY)
        if not (isinstance(stale_blob, dict) and stale_blob.get("payload")):
            stale_blob = _load_stale_snapshot()
            if stale_blob:
                # Re-warm Redis so subsequent requests don't hit disk.
                try:
                    shared_cache.jset(_STALE_KEY, stale_blob, ttl=_STALE_TTL)
                except Exception:
                    pass
        if isinstance(stale_blob, dict) and stale_blob.get("payload"):
            log.warning(
                "option_chain: upstream failed (%s); serving stale snapshot",
                src_error,
            )
            return _build_stale_response(stale_blob, src_error or "")
        return {
            "error": "Failed to fetch option chain.",
            "detail": src_error or "All upstream sources failed.",
        }

    prev = shared_cache.jget(_PREV_KEY)
    payload["indicator"] = _compute_indicator(
        payload, prev if isinstance(prev, dict) else None,
    )
    # VIX is now an explicit input to the gap model (multi-factor scoring).
    payload["vix"] = _fetch_india_vix()
    payload["gap_outlook"] = _compute_gap_outlook(
        payload, prev if isinstance(prev, dict) else None,
    )

    # Persist today's gap signal + back-fill outcomes for past PENDING
    # signals. Both calls are best-effort and never raise.
    try:
        from application.services import gap_history
        go = payload["gap_outlook"] or {}
        gap_history.record(
            "NIFTY",
            label=go.get("label") or "",
            raw_score=float(go.get("raw_score") or 0),
            gap_score=float(go.get("gap_score") or 0),
            confidence=float(go.get("confidence") or 0),
            spot=payload.get("spot"),
            expected_gap_points=go.get("expected_gap_points"),
            expected_gap_pct=go.get("expected_gap_pct"),
            expected_gap_points_low=go.get("expected_gap_points_low"),
            expected_gap_points_high=go.get("expected_gap_points_high"),
            summary=go.get("summary") or "",
        )
        gap_history.evaluate_pending("NIFTY")
    except Exception as _gh_err:  # noqa: BLE001
        log.debug("gap_history hook: %s", _gh_err)
    _annotate_rows_with_buildup(payload.get("rows") or [])
    payload["pcr_prev"] = (prev or {}).get("pcr") if isinstance(prev, dict) else None

    # New panels: OI-based support/resistance and
    # unusual-options-activity ("block deals") tracker.
    payload["sr_levels"] = _compute_sr_levels(
        payload.get("rows") or [], payload.get("spot") or 0,
    )
    payload["block_deals"] = _compute_block_deals(
        payload.get("rows") or [], payload.get("spot") or 0,
    )

    # Option Buying vs Selling environment verdict (uses ATM premium
    # responsiveness, IV vs VIX, and VIX regime + trend).
    payload["strategy"] = _compute_strategy_verdict(payload)

    # Market Regime Meter — sideways vs volatile classification blended from
    # VIX, ATM straddle, gamma pinning, OI wall range, and IV skew.
    try:
        from application.services import regime_meter
        payload["regime"] = regime_meter.compute_regime(payload)
    except Exception as _rm_err:  # noqa: BLE001
        log.debug("regime_meter failed: %s", _rm_err)
        payload["regime"] = None

    try:
        shared_cache.jset(_CACHE_KEY, payload, ttl=_CACHE_TTL)
        prev_age = float((prev or {}).get("_taken_at") or 0) if isinstance(prev, dict) else 0
        if (time.time() - prev_age) > 180:
            snapshot = {
                "pcr": payload.get("pcr"),
                "spot": payload.get("spot"),
                "ce_oi": payload.get("totals", {}).get("ce_oi"),
                "pe_oi": payload.get("totals", {}).get("pe_oi"),
                "ce_oi_chg": payload.get("totals", {}).get("ce_oi_chg"),
                "pe_oi_chg": payload.get("totals", {}).get("pe_oi_chg"),
                "_taken_at": time.time(),
            }
            shared_cache.jset(_PREV_KEY, snapshot, ttl=_PREV_TTL)
    except Exception:
        pass

    # Mirror the fresh payload to the stale-fallback store (Redis + disk).
    # Used to gracefully degrade when both providers fail.
    try:
        stale_blob = {"saved_at": time.time(), "payload": payload}
        shared_cache.jset(_STALE_KEY, stale_blob, ttl=_STALE_TTL)
        _save_stale_snapshot(payload)
    except Exception:
        pass

    # Append minute-bucketed point to today's intraday series.
    try:
        now_ist = _dt.datetime.now(_IST)
        day_key = now_ist.strftime("%Y%m%d")
        series_key = _SERIES_KEY_FMT.format(date=day_key)
        series = shared_cache.jget(series_key)
        if not isinstance(series, list):
            series = _load_series_from_store(day_key)
            if series:
                shared_cache.jset(series_key, series, ttl=_SERIES_TTL)
        minute_bucket = now_ist.strftime("%H:%M")
        ce_oi_v = (payload.get("totals") or {}).get("ce_oi")
        pe_oi_v = (payload.get("totals") or {}).get("pe_oi")
        if ce_oi_v is not None and pe_oi_v is not None:
            point = {
                "t": minute_bucket,
                "ce_oi": ce_oi_v,
                "pe_oi": pe_oi_v,
                "pcr": payload.get("pcr"),
                "spot": payload.get("spot"),
            }
            if series and series[-1].get("t") == minute_bucket:
                series[-1] = point  # replace within same minute
            else:
                series.append(point)
            if len(series) > _SERIES_MAX_POINTS:
                series = series[-_SERIES_MAX_POINTS:]
            shared_cache.jset(series_key, series, ttl=_SERIES_TTL)
            _save_series_to_store(day_key, series)
        payload["series"] = series
    except Exception:
        payload["series"] = []

    payload["cached"] = False
    return payload


# ── Source dispatch ────────────────────────────────────────────────────

def _fetch_payload() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    errors: List[str] = []

    if config.FYERS_APP_ID and config.FYERS_ACCESS_TOKEN:
        try:
            data = _fetch_via_fyers()
            if data:
                return data, None
        except Exception as e:  # noqa: BLE001
            errors.append(f"fyers: {e}")
            log.warning("option_chain.fyers failed: %s", e)

    try:
        data = _fetch_via_nse()
        if data:
            return data, None
    except Exception as e:  # noqa: BLE001
        errors.append(f"nse: {e}")
        log.warning("option_chain.nse failed: %s", e)

    return None, "; ".join(errors) if errors else None


# ── Fyers source ───────────────────────────────────────────────────────

_FY_BASE = "https://api-t1.fyers.in"
_FY_PATH = "/data/options-chain-v3"


def _fy_headers() -> Dict[str, str]:
    return {
        "Authorization": f"{config.FYERS_APP_ID}:{config.FYERS_ACCESS_TOKEN}",
        "Accept": "application/json",
    }


def _fetch_via_fyers(strikecount: int = 12, _retried: bool = False) -> Optional[Dict[str, Any]]:
    params = {
        "symbol": "NSE:NIFTY50-INDEX",
        "strikecount": strikecount,
        "timestamp": "",
    }
    r = requests.get(
        _FY_BASE + _FY_PATH,
        params=params, headers=_fy_headers(), timeout=_TIMEOUT,
    )
    # Auto-refresh on 401 (token expired daily at midnight IST). Single retry.
    if r.status_code == 401 or (
        r.status_code >= 400 and "could not authenticate" in r.text.lower()
    ):
        if not _retried:
            try:
                from application.services.providers import fyers_provider
                if fyers_provider._try_refresh_token():
                    return _fetch_via_fyers(strikecount, _retried=True)
            except Exception as e:
                log.warning("option_chain.fyers refresh failed: %s", e)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    body = r.json() or {}
    if body.get("s") and body.get("s") != "ok":
        raise RuntimeError(f"upstream error: {str(body)[:200]}")
    data = body.get("data") or {}
    chain: List[Dict[str, Any]] = data.get("optionsChain") or []
    if not chain:
        return None

    spot = 0.0
    for it in chain:
        if not it.get("option_type"):
            spot = _num(it.get("ltp"))
            break
    if not spot:
        spot = _num(data.get("index_value"))

    by_strike: Dict[float, Dict[str, Any]] = {}
    for it in chain:
        opt_type = (it.get("option_type") or "").upper()
        if opt_type not in ("CE", "PE"):
            continue
        strike = it.get("strike_price")
        if strike in (None, 0):
            continue
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        leg = {
            "oi": _num(it.get("oi")),
            "oi_chg": _num(it.get("oich")) or _num(it.get("oichng")),
            "iv": _num(it.get("iv")),
            "ltp": _num(it.get("ltp")),
            "vol": _num(it.get("volume")),
            "chg": _num(it.get("ltpch")),
            "pct_chg": _num(it.get("ltpchp")),
        }
        bucket = by_strike.setdefault(strike_f, {"strike": strike_f})
        bucket["ce" if opt_type == "CE" else "pe"] = leg

    rows: List[Dict[str, Any]] = []
    for strike_f in sorted(by_strike):
        row = by_strike[strike_f]
        row.setdefault("ce", _empty_leg())
        row.setdefault("pe", _empty_leg())
        rows.append(row)

    exp_list = data.get("expiryData") or []
    expiry_label = ""
    if exp_list:
        first = exp_list[0]
        expiry_label = first.get("date") or _fmt_expiry(first.get("expiry"))
    expiries = [e.get("date") or _fmt_expiry(e.get("expiry")) for e in exp_list]

    totals = _totals(rows)
    pcr = (totals["pe_oi"] / totals["ce_oi"]) if totals["ce_oi"] else 0.0
    return {
        "symbol": "NIFTY",
        "spot": round(spot, 2),
        "expiry": expiry_label,
        "expiries": expiries,
        "rows": rows,
        "totals": totals,
        "pcr": round(pcr, 3),
        "as_of": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST"),
        "source": "fyers",
    }


def _fmt_expiry(ts: Any) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(ts), _IST).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return ""


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _empty_leg() -> Dict[str, float]:
    return {"oi": 0.0, "oi_chg": 0.0, "iv": 0.0, "ltp": 0.0,
            "vol": 0.0, "chg": 0.0, "pct_chg": 0.0}


# ── NSE source (fallback) ──────────────────────────────────────────────

_NSE_BASE = "https://www.nseindia.com"
_NSE_HOME = f"{_NSE_BASE}/option-chain"
_NSE_API = f"{_NSE_BASE}/api/option-chain-indices"

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": _NSE_HOME,
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

_session_lock = threading.Lock()
_nse_session: Optional[requests.Session] = None
_nse_session_age: float = 0.0
_NSE_SESSION_TTL = 15 * 60

_NSE_BREADTH_CACHE_KEY = "optionchain:nse:breadth_movers:v1"
_NSE_BREADTH_CACHE_TTL = 45

_NSE_CORPREF_CACHE_KEY = "optionchain:nse:corp_ref:v1"
_NSE_CORPREF_CACHE_TTL = 5 * 60


def _get_nse_session() -> requests.Session:
    global _nse_session, _nse_session_age
    with _session_lock:
        if _nse_session is None or (time.time() - _nse_session_age) > _NSE_SESSION_TTL:
            s = requests.Session()
            s.headers.update(_NSE_HEADERS)
            try:
                s.get(_NSE_BASE + "/", timeout=_TIMEOUT)
                s.get(_NSE_HOME, timeout=_TIMEOUT)
            except requests.RequestException as e:
                log.warning("option_chain.nse cookie warm-up failed: %s", e)
            _nse_session = s
            _nse_session_age = time.time()
        return _nse_session


def _fetch_via_nse(retries: int = 1) -> Optional[Dict[str, Any]]:
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            s = _get_nse_session()
            r = s.get(_NSE_API, params={"symbol": "NIFTY"}, timeout=_TIMEOUT)
            if r.status_code in (401, 403):
                _invalidate_nse_session()
                last_err = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return _normalize_nse(r.json())
        except (requests.RequestException, ValueError) as e:
            last_err = e
            _invalidate_nse_session()
            time.sleep(0.4 * (attempt + 1))
    if last_err:
        raise last_err
    return None


def _nse_get_json(path: str, params: Optional[Dict[str, Any]] = None,
                  retries: int = 1) -> Any:
    """GET JSON from NSE with cookie-warmed session + retry on auth failures."""
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            s = _get_nse_session()
            r = s.get(_NSE_BASE + path, params=params or {}, timeout=_TIMEOUT)
            if r.status_code in (401, 403):
                _invalidate_nse_session()
                last_err = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            _invalidate_nse_session()
            time.sleep(0.35 * (attempt + 1))
    if last_err:
        raise last_err
    return None


def _invalidate_nse_session() -> None:
    global _nse_session, _nse_session_age
    with _session_lock:
        _nse_session = None
        _nse_session_age = 0.0


def _normalize_nse(raw: Dict[str, Any], strikes_each_side: int = 10) -> Dict[str, Any]:
    records = raw.get("records") or {}
    expiries: List[str] = records.get("expiryDates") or []
    nearest_expiry = expiries[0] if expiries else ""
    spot = float(records.get("underlyingValue") or 0)

    data = records.get("data") or []
    rows_all: List[Dict[str, Any]] = []
    for item in data:
        if item.get("expiryDate") != nearest_expiry:
            continue
        strike = item.get("strikePrice")
        if strike is None:
            continue
        rows_all.append({
            "strike": float(strike),
            "ce": _nse_leg(item.get("CE") or {}),
            "pe": _nse_leg(item.get("PE") or {}),
        })
    rows_all.sort(key=lambda r: r["strike"])
    rows = _atm_window(rows_all, spot, strikes_each_side)
    totals = _totals(rows)
    pcr = (totals["pe_oi"] / totals["ce_oi"]) if totals["ce_oi"] else 0.0
    return {
        "symbol": "NIFTY",
        "spot": round(spot, 2),
        "expiry": nearest_expiry,
        "expiries": expiries,
        "rows": rows,
        "totals": totals,
        "pcr": round(pcr, 3),
        "as_of": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST"),
        "source": "nseindia.com",
    }


def _nse_leg(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "oi": _num(d.get("openInterest")),
        "oi_chg": _num(d.get("changeinOpenInterest")),
        "iv": _num(d.get("impliedVolatility")),
        "ltp": _num(d.get("lastPrice")),
        "vol": _num(d.get("totalTradedVolume")),
        "chg": _num(d.get("change")),
        "pct_chg": _num(d.get("pChange")),
    }


def _atm_window(rows: List[Dict[str, Any]], spot: float,
                each_side: int) -> List[Dict[str, Any]]:
    if not rows:
        return rows
    idx = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot))
    lo = max(0, idx - each_side)
    hi = min(len(rows), idx + each_side + 1)
    return rows[lo:hi]


def _totals(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    ce_oi = sum(r["ce"]["oi"] for r in rows)
    pe_oi = sum(r["pe"]["oi"] for r in rows)
    ce_oi_chg = sum(r["ce"]["oi_chg"] for r in rows)
    pe_oi_chg = sum(r["pe"]["oi_chg"] for r in rows)
    ce_vol = sum(r["ce"]["vol"] for r in rows)
    pe_vol = sum(r["pe"]["vol"] for r in rows)
    return {
        "ce_oi": round(ce_oi),
        "pe_oi": round(pe_oi),
        "ce_oi_chg": round(ce_oi_chg),
        "pe_oi_chg": round(pe_oi_chg),
        "ce_vol": round(ce_vol),
        "pe_vol": round(pe_vol),
    }


# ── OI-shift indicator ─────────────────────────────────────────────────

_BEARISH_TH = 0.20
_BULLISH_TH = -0.20

# Build-up classification (price + OI matrix). Weights are calibrated on
# the assumption that **option writers are the smart-money side**
# (institutions/prop desks) and **option buyers are typically retail**.
# So writer-driven build-ups (Short Build-up on CE, Short Put Build-up on
# PE) carry the strongest signal, and retail-buyer build-ups (Long
# Build-up on CE, Long Put Build-up on PE) get a small bullish/bearish
# read with some contrarian discount.
#
# Convention: positive weight = bullish for the underlying.
#
# Calls (CE):
#   price↓ + OI↑  → Short Build-up   → -1.2  (institutions writing → resistance)
#   price↑ + OI↓  → Short Covering   → +1.1  (institutions giving up bearish bet)
#   price↑ + OI↑  → Long Build-up    → +0.4  (retail chasing — weak / can be exhaustion)
#   price↓ + OI↓  → Long Unwinding   → -0.4  (retail exiting — weak bearish)
#
# Puts (PE) — mirrored, since put price moves inversely to the index:
#   price↓ + OI↑  → Short Put Build-up → +1.2 (institutions writing puts → support)
#   price↑ + OI↓  → Put Short Covering → -1.1 (institutional put writers worried)
#   price↑ + OI↑  → Long Put Build-up  → -0.4 (retail buying puts — weak bearish)
#   price↓ + OI↓  → Put Long Unwinding → +0.4 (put longs exiting — weak bullish)

_BUILDUP_CE = {
    ("up", "up"):     ("Long Build-up",   +0.4),
    ("down", "up"):   ("Short Build-up",  -1.2),
    ("up", "down"):   ("Short Covering",  +1.1),
    ("down", "down"): ("Long Unwinding",  -0.4),
}
_BUILDUP_PE = {
    ("up", "up"):     ("Long Put Build-up",   -0.4),
    ("down", "up"):   ("Short Put Build-up",  +1.2),
    ("up", "down"):   ("Put Short Covering",  -1.1),
    ("down", "down"): ("Put Long Unwinding",  +0.4),
}

# Minimum |price change| / |OI change| to count a strike (filters noise).
_MIN_PRICE_CHG = 0.01
_MIN_OI_CHG    = 1.0


def _dir(x: float, eps: float) -> Optional[str]:
    if x > eps:
        return "up"
    if x < -eps:
        return "down"
    return None


def _classify_strike(leg: Dict[str, Any], leg_type: str) -> Optional[Dict[str, Any]]:
    price_chg = float(leg.get("chg") or 0)
    oi_chg = float(leg.get("oi_chg") or 0)
    pd = _dir(price_chg, _MIN_PRICE_CHG)
    od = _dir(oi_chg, _MIN_OI_CHG)
    if pd is None or od is None:
        return None
    table = _BUILDUP_CE if leg_type == "ce" else _BUILDUP_PE
    entry = table.get((pd, od))
    if entry is None:
        return None
    name, weight = entry
    return {
        "name": name,
        "weight": weight,
        "oi_chg": oi_chg,
        "price_chg": price_chg,
    }


def _compute_buildup(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-strike build-up classifications into a weighted score.

    Returns: {
        score: float in [-1, +1] (positive = bullish for underlying),
        ce_counts / pe_counts: {build-up name: int},
        ce_weight / pe_weight: signed weighted sum,
        top_ce / top_pe: strongest single strike contribution per side.
    }
    """
    ce_w_total = 0.0
    pe_w_total = 0.0
    ce_mag = 0.0
    pe_mag = 0.0
    ce_counts: Dict[str, int] = {}
    pe_counts: Dict[str, int] = {}
    top_ce: Optional[Dict[str, Any]] = None
    top_pe: Optional[Dict[str, Any]] = None

    for r in rows:
        strike = r.get("strike")
        for leg_type in ("ce", "pe"):
            cls = _classify_strike(r.get(leg_type) or {}, leg_type)
            if cls is None:
                continue
            mag = abs(cls["oi_chg"])
            contrib = cls["weight"] * mag
            if leg_type == "ce":
                ce_w_total += contrib
                ce_mag += mag
                ce_counts[cls["name"]] = ce_counts.get(cls["name"], 0) + 1
                if top_ce is None or abs(contrib) > abs(top_ce["contrib"]):
                    top_ce = {"strike": strike, "name": cls["name"],
                              "contrib": contrib, "oi_chg": cls["oi_chg"],
                              "price_chg": cls["price_chg"]}
            else:
                pe_w_total += contrib
                pe_mag += mag
                pe_counts[cls["name"]] = pe_counts.get(cls["name"], 0) + 1
                if top_pe is None or abs(contrib) > abs(top_pe["contrib"]):
                    top_pe = {"strike": strike, "name": cls["name"],
                              "contrib": contrib, "oi_chg": cls["oi_chg"],
                              "price_chg": cls["price_chg"]}

    total_mag = ce_mag + pe_mag
    score = ((ce_w_total + pe_w_total) / total_mag) if total_mag else 0.0
    # Clamp into [-1, +1].
    score = max(-1.0, min(1.0, score))
    return {
        "score": round(score, 3),
        "ce_counts": ce_counts,
        "pe_counts": pe_counts,
        "ce_weight": round(ce_w_total, 1),
        "pe_weight": round(pe_w_total, 1),
        "top_ce": top_ce,
        "top_pe": top_pe,
    }


def _compute_indicator(payload: Dict[str, Any],
                       prev: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    totals = payload.get("totals") or {}
    rows = payload.get("rows") or []
    ce_chg = float(totals.get("ce_oi_chg") or 0)
    pe_chg = float(totals.get("pe_oi_chg") or 0)
    denom = abs(ce_chg) + abs(pe_chg)
    # Original CE→PE shift score (positive = bearish).
    intraday_score = ((pe_chg - ce_chg) / denom) if denom else 0.0

    # PCR drift (positive = bearish).
    pcr_now = float(payload.get("pcr") or 0)
    pcr_prev = float(prev.get("pcr") or 0) if prev else 0.0
    pcr_delta = pcr_now - pcr_prev if pcr_prev else 0.0
    pcr_contrib = max(-0.5, min(0.5, pcr_delta * 1.5))

    # Per-strike build-up score (positive = bullish; flip sign so all three
    # contributors share the same "positive = bearish" convention).
    buildup = _compute_buildup(rows)
    buildup_score = -float(buildup["score"])  # flip to match convention

    # Weighted combination — build-up gets the largest weight because it
    # uses *price + OI* together to identify writer-driven flow (the
    # smart-money side). OI shift alone is the next strongest signal
    # because rising Call OI (writers selling calls) and rising Put OI
    # (writers selling puts) already encode the institutional view.
    shift_score = (
        0.55 * buildup_score
        + 0.35 * intraday_score
        + 0.10 * pcr_contrib
    )
    shift_score = max(-1.0, min(1.0, shift_score))

    if shift_score >= _BEARISH_TH:
        label = "BEARISH"
        summary = (
            "Institutions are aggressively writing Calls / unwinding Put "
            "writes — smart-money is positioned for a fall."
        )
    elif shift_score <= _BULLISH_TH:
        label = "BULLISH"
        summary = (
            "Institutions are aggressively writing Puts / covering Call "
            "shorts — smart-money is positioned for an upmove."
        )
    else:
        label = "NEUTRAL"
        summary = (
            "Mixed institutional flow — Call writers and Put writers are "
            "both active without a clear winner."
        )

    reasons: List[str] = []
    # 1) Build-up commentary
    top_ce = buildup.get("top_ce")
    top_pe = buildup.get("top_pe")
    if top_ce:
        reasons.append(
            f"Call {top_ce['strike']:g}: {top_ce['name']} "
            f"(Δ OI {_fmt_int(top_ce['oi_chg'])}, "
            f"Δ price {top_ce['price_chg']:+.2f})"
        )
    if top_pe:
        reasons.append(
            f"Put {top_pe['strike']:g}: {top_pe['name']} "
            f"(Δ OI {_fmt_int(top_pe['oi_chg'])}, "
            f"Δ price {top_pe['price_chg']:+.2f})"
        )
    # 2) OI shift commentary
    reasons.append(
        f"Δ Put OI: {_fmt_int(pe_chg)}  vs  Δ Call OI: {_fmt_int(ce_chg)} "
        f"(intraday change vs prev close)"
    )
    # 3) PCR commentary
    reasons.append(
        f"PCR (PE/CE OI) = {pcr_now:.2f}"
        + (f"  ↑ from {pcr_prev:.2f}" if pcr_prev and pcr_now > pcr_prev
           else (f"  ↓ from {pcr_prev:.2f}" if pcr_prev and pcr_now < pcr_prev else ""))
    )
    max_ce_strike = _max_oi_strike(rows, "ce")
    max_pe_strike = _max_oi_strike(rows, "pe")
    if max_ce_strike is not None:
        reasons.append(f"Highest Call OI strike (resistance): {max_ce_strike:g}")
    if max_pe_strike is not None:
        reasons.append(f"Highest Put OI strike (support): {max_pe_strike:g}")

    return {
        "label": label,
        "shift_score": round(shift_score, 3),
        "intraday_score": round(intraday_score, 3),
        "buildup_score": round(-buildup_score, 3),  # report in bullish-positive form
        "pcr_delta": round(pcr_delta, 3),
        "summary": summary,
        "reasons": reasons,
        "max_ce_strike": max_ce_strike,
        "max_pe_strike": max_pe_strike,
        "buildup": buildup,
    }


def _max_oi_strike(rows: List[Dict[str, Any]], leg: str) -> Optional[float]:
    if not rows:
        return None
    best = max(rows, key=lambda r: (r.get(leg) or {}).get("oi", 0))
    oi = (best.get(leg) or {}).get("oi", 0)
    return best["strike"] if oi else None


def _annotate_rows_with_buildup(rows: List[Dict[str, Any]]) -> None:
    """Tag each leg with its build-up label so the UI can colour-code rows."""
    for r in rows:
        for leg_type in ("ce", "pe"):
            leg = r.get(leg_type)
            if not isinstance(leg, dict):
                continue
            cls = _classify_strike(leg, leg_type)
            if cls is None:
                leg["buildup"] = None
                leg["buildup_bias"] = 0
            else:
                leg["buildup"] = cls["name"]
                # Sign reflects bullish-positive sentiment for the underlying.
                leg["buildup_bias"] = 1 if cls["weight"] > 0 else -1


def _fmt_int(n: float) -> str:
    sign = "+" if n > 0 else ("" if n == 0 else "-")
    return f"{sign}{abs(int(n)):,}"


# ── Gap-up / Gap-down outlook ──────────────────────────────────────────
#
# Idea (user-defined):
#   Near market close, the OI being *added* tells you which side the
#   writers (institutions) are positioning for the *next* session.
#       * Fresh Call OI added in the closing window  → writers expect
#         price NOT to rise → gap DOWN open likely.
#       * Fresh Put OI added in the closing window   → writers expect
#         price NOT to fall → gap UP open likely.
#
# We measure "fresh OI" as the change between the previous cached
# snapshot (~3 min old) and now. We weight the signal by how close we
# are to the close — before 13:30 IST the forecast confidence is ~0
# and ramps linearly to full confidence at 15:00 IST.

_GAP_UP_TH = 0.25
_GAP_DOWN_TH = -0.25

# ── Decision-lock window (per user spec) ──
# The forecast must be *decided* by 15:15 IST and held stable through the
# close, unless a sudden big OI surge in the last 15 minutes (15:15–15:30)
# is strong enough to flip the bias.
_LOCK_HOUR = 15
_LOCK_MIN = 15                  # 15:15 IST — lock decision
_CLOSE_MIN_OF_DAY = 15 * 60 + 30  # 15:30 IST — market close
_LOCK_MIN_OF_DAY = _LOCK_HOUR * 60 + _LOCK_MIN
_GAP_LOCK_KEY_FMT = "optionchain:gap_lock:{date}"
_GAP_LOCK_TTL = 8 * 3600        # carries through post-close window
# A late-session surge qualifies as "big" only if the net OI built between
# 15:15 and now is at least 5 lakh contracts AND at least 50 % of the
# imbalance that produced the locked decision.
_SURGE_MIN_NET = 500_000
_SURGE_REL_FRAC = 0.5

# Multi-factor weights for production-quality gap prediction.
# All factor scores are normalized into [-1, +1] where +1 implies GAP UP.
_GAP_W_OI = 0.55
_GAP_W_TREND = 0.20
_GAP_W_VIX = 0.15
_GAP_W_PCR = 0.10


def _label_from_raw(raw: float) -> str:
    if raw >= _GAP_UP_TH:
        return "GAP UP"
    if raw <= _GAP_DOWN_TH:
        return "GAP DOWN"
    return "FLAT"


def _estimate_gap_points(
    spot: float,
    label: str,
    gap_score: float,
    confidence: float,
    vix_change_pct: float = 0.0,
) -> Dict[str, Any]:
    """Estimate expected NIFTY opening gap in points and percent.

    Uses signal strength, confidence, and VIX shock to scale magnitude.
    Direction is taken from the final gap label.
    """
    direction = 1 if label == "GAP UP" else (-1 if label == "GAP DOWN" else 0)
    if spot <= 0 or direction == 0:
        return {
            "expected_gap_points": 0,
            "expected_gap_pct": 0.0,
            "expected_gap_points_low": 0,
            "expected_gap_points_high": 0,
        }

    strength = max(0.0, min(1.0, abs(float(gap_score))))
    conf = max(0.0, min(1.0, float(confidence)))
    # Base move envelope: about 0.12% (weak) to 0.60% (strong).
    base_pct = 0.12 + (0.48 * strength)
    conf_adj = 0.65 + (0.35 * conf)
    # Rising VIX usually widens opening gaps; clamp to avoid blowups.
    vix_adj = max(0.80, min(1.25, 1.0 + (float(vix_change_pct) / 12.0)))

    pct_abs = base_pct * conf_adj * vix_adj
    pct = direction * pct_abs
    pts_abs = (spot * pct_abs) / 100.0
    pts = direction * pts_abs

    lo = max(5.0, pts_abs * 0.70)
    hi = max(8.0, pts_abs * 1.30)
    return {
        "expected_gap_points": int(round(pts)),
        "expected_gap_pct": round(pct, 2),
        "expected_gap_points_low": int(round(direction * lo)),
        "expected_gap_points_high": int(round(direction * hi)),
    }


def _session_weight_now() -> Tuple[float, str, bool]:
    """Return (confidence_weight ∈ [0,1], phase_label, force_use_total).

    `force_use_total` is True for post-close window — the day's full
    intraday OI change is the best read on tomorrow's open until the next
    session starts.

    Forecast is *active* between 13:30 and 15:15 IST. After 15:15 the call
    is locked (see :func:`_compute_gap_outlook`); the weight returned here
    still ramps to 1.0 so the locked snapshot reflects full confidence.
    """
    now = _dt.datetime.now(_IST)
    mins = now.hour * 60 + now.minute
    market_open = 9 * 60 + 15
    market_close = 15 * 60 + 30
    # Post-close (same calendar day, after 15:30) → use full intraday total.
    if mins > market_close:
        return 0.9, "Post-close — forecast for next open", True
    # Pre-open (before 09:15) → still showing yesterday's close-based view.
    if mins < market_open:
        return 0.7, "Pre-open — based on previous close", True
    # Pre 13:30 → too early to forecast gap.
    start = 13 * 60 + 30   # 13:30
    full = _LOCK_MIN_OF_DAY  # 15:15 → full confidence (decision-lock time)
    if mins < start:
        return 0.0, "Intraday (forecast inactive until 13:30 IST)", False
    if mins >= full:
        return 1.0, "Decision window closing — locking call at 15:15", False
    # Linear ramp 13:30 → 15:15.
    w = (mins - start) / (full - start)
    return round(w, 2), "Late session — building confidence", False


def _compute_gap_outlook(payload: Dict[str, Any],
                         prev: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    totals = payload.get("totals") or {}
    ce_oi = float(totals.get("ce_oi") or 0)
    pe_oi = float(totals.get("pe_oi") or 0)
    ce_chg_total = float(totals.get("ce_oi_chg") or 0)
    pe_chg_total = float(totals.get("pe_oi_chg") or 0)

    weight, phase, force_total = _session_weight_now()

    # Recent (last ~3 min) OI build — preferred signal during the session.
    # After hours / pre-open, use the full intraday change instead.
    if (not force_total) and prev and prev.get("ce_oi") is not None and prev.get("pe_oi") is not None:
        ce_recent = ce_oi - float(prev["ce_oi"])
        pe_recent = pe_oi - float(prev["pe_oi"])
        source = "recent"
    else:
        ce_recent = ce_chg_total
        pe_recent = pe_chg_total
        if not force_total:
            weight *= 0.6  # no baseline → softer confidence intra-session
        source = "intraday"

    def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, x))

    # ── Factor 1: fresh OI flow (core signal) ───────────────────────
    denom = abs(ce_recent) + abs(pe_recent)
    oi_raw = ((pe_recent - ce_recent) / denom) if denom else 0.0

    # ── Factor 2: spot trend drift vs previous snapshot (~3 min) ─────
    spot_now = float(payload.get("spot") or 0)
    prev_spot = float(prev.get("spot") or 0) if isinstance(prev, dict) else 0.0
    if spot_now > 0 and prev_spot > 0:
        trend_pct = ((spot_now - prev_spot) / prev_spot) * 100.0
        # 0.30% move in ~3 min maps close to full conviction.
        trend_raw = _clamp(trend_pct / 0.30)
    else:
        trend_pct = 0.0
        trend_raw = 0.0

    # ── Factor 3: volatility shock (VIX) ─────────────────────────────
    # Rising VIX is usually risk-off (bearish), so the sign is flipped.
    vix = payload.get("vix") if isinstance(payload.get("vix"), dict) else None
    vix_chg_pct = float((vix or {}).get("change_pct") or 0.0)
    vix_raw = _clamp(-vix_chg_pct / 2.5) if vix else 0.0

    # ── Factor 4: PCR drift ───────────────────────────────────────────
    pcr_now = float(payload.get("pcr") or 0)
    pcr_prev = float(prev.get("pcr") or 0) if isinstance(prev, dict) else 0.0
    pcr_delta = (pcr_now - pcr_prev) if pcr_prev else 0.0
    pcr_raw = _clamp(pcr_delta / 0.12) if pcr_prev else 0.0

    # Compose available factors with normalized coverage so missing factors
    # do not skew direction but reduce confidence.
    factor_specs = [
        ("oi", oi_raw, _GAP_W_OI, True),
        ("trend", trend_raw, _GAP_W_TREND, (spot_now > 0 and prev_spot > 0)),
        ("vix", vix_raw, _GAP_W_VIX, bool(vix)),
        ("pcr", pcr_raw, _GAP_W_PCR, bool(pcr_prev)),
    ]
    total_w = sum(w for _, _, w, _ in factor_specs)
    active_w = sum(w for _, _, w, ok in factor_specs if ok)
    if active_w > 0:
        composite_raw = sum(raw_i * w for _, raw_i, w, ok in factor_specs if ok) / active_w
    else:
        composite_raw = oi_raw
        active_w = _GAP_W_OI

    coverage = active_w / total_w if total_w else 1.0
    # raw > 0  → GAP UP bias, raw < 0 → GAP DOWN bias.
    gap_score = _clamp(composite_raw * weight)
    confidence = round(max(0.0, min(1.0, weight * coverage)), 2)

    label = _label_from_raw(gap_score)
    if label == "GAP UP":
        summary = (
            "Composite close signal is bullish — put-side flow, trend, and "
            "risk tone favour a gap-up open."
        )
    elif label == "GAP DOWN":
        summary = (
            "Composite close signal is bearish — call-side flow, trend, and "
            "risk tone favour a gap-down open."
        )
    else:
        if weight < 0.2:
            label = "TOO EARLY"
            summary = (
                "Not enough late-session data yet — gap forecast activates "
                "after 13:30 IST."
            )
        else:
            summary = (
                "Call and put writing are roughly balanced into the close "
                "— no clear gap bias."
            )

    contribs = {
        name: round((raw_i * w) / (active_w if active_w else 1.0), 3)
        for name, raw_i, w, ok in factor_specs if ok
    }
    quality_flags: List[str] = []
    if source != "recent":
        quality_flags.append("no_recent_baseline")
    if not vix:
        quality_flags.append("vix_unavailable")
    if not (spot_now > 0 and prev_spot > 0):
        quality_flags.append("trend_unavailable")
    if not pcr_prev:
        quality_flags.append("pcr_baseline_unavailable")

    top_factors = sorted(
        contribs.items(), key=lambda kv: abs(kv[1]), reverse=True
    )[:2]
    if top_factors and label in ("GAP UP", "GAP DOWN"):
        leads = ", ".join(f"{k}={v:+.2f}" for k, v in top_factors)
        summary = f"{summary} Lead factors: {leads}."

    projected = _estimate_gap_points(
        spot=spot_now,
        label=label,
        gap_score=gap_score,
        confidence=confidence,
        vix_change_pct=vix_chg_pct,
    )

    live = {
        "label": label,
        "gap_score": round(gap_score, 3),
        "raw_score": round(composite_raw, 3),
        "confidence": confidence,
        "phase": phase,
        "source": source,
        "summary": summary,
        "model": "multifactor_v1",
        "coverage": round(coverage, 2),
        "factors": {
            "oi_raw": round(oi_raw, 3),
            "trend_raw": round(trend_raw, 3),
            "trend_pct": round(trend_pct, 3),
            "vix_raw": round(vix_raw, 3),
            "vix_change_pct": round(vix_chg_pct, 2),
            "pcr_raw": round(pcr_raw, 3),
            "pcr_delta": round(pcr_delta, 3),
            "contrib": contribs,
            "quality_flags": quality_flags,
        },
        "ce_recent": int(ce_recent),
        "pe_recent": int(pe_recent),
        "ce_chg_total": int(ce_chg_total),
        "pe_chg_total": int(pe_chg_total),
        **projected,
        "locked": False,
        "overridden": False,
    }

    # ── Decision-lock semantics (post 15:15 IST) ──
    # Before 15:15 the live signal is free to evolve. At/after 15:15 we
    # snapshot the call and hold it through the close & post-close window.
    # Between 15:15 and 15:30 we will *only* override the locked call if a
    # sudden big OI surge in those final minutes is large enough to flip
    # the bias.
    now = _dt.datetime.now(_IST)
    mins_of_day = now.hour * 60 + now.minute
    if mins_of_day < _LOCK_MIN_OF_DAY:
        return live

    lock_key = _GAP_LOCK_KEY_FMT.format(date=now.strftime("%Y%m%d"))
    lock = shared_cache.jget(lock_key)
    if not isinstance(lock, dict) or not lock.get("label"):
        lock = {
            "label": live["label"],
            "gap_score": live["gap_score"],
            "raw_score": live["raw_score"],
            "summary": live["summary"],
            "expected_gap_points": live.get("expected_gap_points", 0),
            "expected_gap_pct": live.get("expected_gap_pct", 0.0),
            "expected_gap_points_low": live.get("expected_gap_points_low", 0),
            "expected_gap_points_high": live.get("expected_gap_points_high", 0),
            "ce_chg_total": int(ce_chg_total),
            "pe_chg_total": int(pe_chg_total),
            "locked_at": now.strftime("%H:%M IST"),
        }
        shared_cache.jset(lock_key, lock, ttl=_GAP_LOCK_TTL)

    result = {
        **live,
        "label": lock["label"],
        "gap_score": lock["gap_score"],
        "raw_score": lock.get("raw_score", live["raw_score"]),
        "summary": lock["summary"],
        "expected_gap_points": lock.get("expected_gap_points", live.get("expected_gap_points", 0)),
        "expected_gap_pct": lock.get("expected_gap_pct", live.get("expected_gap_pct", 0.0)),
        "expected_gap_points_low": lock.get("expected_gap_points_low", live.get("expected_gap_points_low", 0)),
        "expected_gap_points_high": lock.get("expected_gap_points_high", live.get("expected_gap_points_high", 0)),
        "phase": (
            f"Locked at {lock['locked_at']} — final call for tomorrow's open"
            if mins_of_day > _CLOSE_MIN_OF_DAY
            else f"Locked at {lock['locked_at']} — monitoring last-15-min surge"
        ),
        "locked": True,
        "locked_at": lock["locked_at"],
    }

    # During 15:15 → 15:30 only, look for a *big* late surge that would
    # change the locked decision.
    if mins_of_day <= _CLOSE_MIN_OF_DAY:
        delta_ce = ce_chg_total - float(lock["ce_chg_total"])
        delta_pe = pe_chg_total - float(lock["pe_chg_total"])
        delta_net = delta_pe - delta_ce            # >0 → puts heavy → GAP UP
        locked_net = float(lock["pe_chg_total"]) - float(lock["ce_chg_total"])
        threshold = max(_SURGE_MIN_NET, _SURGE_REL_FRAC * abs(locked_net))

        denom_d = abs(delta_ce) + abs(delta_pe)
        delta_raw = ((delta_pe - delta_ce) / denom_d) if denom_d else 0.0
        candidate = _label_from_raw(delta_raw)

        if candidate != lock["label"] and abs(delta_net) >= threshold:
            override_summary = (
                f"Late surge in the last 15 minutes — net OI build of "
                f"{_fmt_int(delta_net)} flipped the bias from "
                f"{lock['label']} to {candidate}."
            )
            new_score = round(max(-1.0, min(1.0, delta_raw)), 3)
            override_projected = _estimate_gap_points(
                spot=spot_now,
                label=candidate,
                gap_score=new_score,
                confidence=confidence,
                vix_change_pct=vix_chg_pct,
            )
            result.update({
                "label": candidate,
                "gap_score": new_score,
                "raw_score": round(delta_raw, 3),
                "summary": override_summary,
                "phase": "Overridden by last-15-min OI surge",
                "overridden": True,
                "override_delta_ce": int(delta_ce),
                "override_delta_pe": int(delta_pe),
                "override_net": int(delta_net),
                "override_threshold": int(threshold),
                **override_projected,
            })
            # Persist the override so subsequent calls stay consistent.
            shared_cache.jset(lock_key, {
                **lock,
                "label": candidate,
                "gap_score": new_score,
                "raw_score": round(delta_raw, 3),
                "summary": override_summary,
                "expected_gap_points": result.get("expected_gap_points", 0),
                "expected_gap_pct": result.get("expected_gap_pct", 0.0),
                "expected_gap_points_low": result.get("expected_gap_points_low", 0),
                "expected_gap_points_high": result.get("expected_gap_points_high", 0),
                "ce_chg_total": int(ce_chg_total),
                "pe_chg_total": int(pe_chg_total),
            }, ttl=_GAP_LOCK_TTL)

    return result


# ── India VIX widget ───────────────────────────────────────────────────
#
# VIX = NSE's 30-day implied-volatility index for NIFTY. Levels:
#   < 12   → "Calm / complacent" (low fear, often pre-trend start)
#   12-18  → "Normal"
#   18-25  → "Elevated" (start of trouble)
#   > 25   → "High fear" (often local bottoms)

_VIX_CACHE_KEY = "optionchain:vix:v1"
_VIX_CACHE_TTL = 30  # 30s — VIX moves slowly intraday


def _vix_regime(value: float) -> Tuple[str, str]:
    if value < 12:
        return "calm", "Complacent — low implied volatility"
    if value < 18:
        return "normal", "Normal volatility regime"
    if value < 25:
        return "elevated", "Elevated volatility — caution"
    return "high", "High fear — extreme volatility"


def _fetch_india_vix() -> Optional[Dict[str, Any]]:
    """Fetch live India VIX via Fyers /data/quotes; cache for 30 s.

    Returns None if both Fyers and NSE sources fail."""
    cached = shared_cache.jget(_VIX_CACHE_KEY)
    if isinstance(cached, dict) and cached.get("value"):
        return cached

    value = chg = pct = 0.0
    ok = False

    # Try Fyers first.
    if config.FYERS_APP_ID and config.FYERS_ACCESS_TOKEN:
        try:
            r = requests.get(
                _FY_BASE + "/data/quotes",
                params={"symbols": "NSE:INDIAVIX-INDEX"},
                headers=_fy_headers(),
                timeout=_TIMEOUT,
            )
            if r.status_code < 400:
                body = r.json() or {}
                d = (body.get("d") or [{}])[0]
                v = d.get("v") or {}
                value = _num(v.get("lp") or v.get("ltp"))
                chg = _num(v.get("ch"))
                pct = _num(v.get("chp"))
                if value:
                    ok = True
        except Exception as e:  # noqa: BLE001
            log.debug("vix.fyers failed: %s", e)

    # Fallback to NSE indices endpoint (cookie-warmed session).
    if not ok:
        try:
            body = _nse_get_json("/api/allIndices", retries=1) or {}
            for item in (body.get("data") or []):
                if (item.get("indexSymbol") or item.get("index") or "").upper() in ("INDIA VIX", "INDIAVIX"):
                    value = _num(item.get("last"))
                    chg = _num(item.get("variation") or item.get("change"))
                    pct = _num(item.get("percentChange") or item.get("pChange"))
                    ok = bool(value)
                    break
        except Exception as e:  # noqa: BLE001
            log.debug("vix.nse failed: %s", e)

    if not ok:
        return None

    level, regime_label = _vix_regime(value)
    out = {
        "value": round(value, 2),
        "change": round(chg, 2),
        "change_pct": round(pct, 2),
        "level": level,
        "regime_label": regime_label,
        "as_of": _dt.datetime.now(_IST).strftime("%I:%M %p IST"),
    }
    try:
        shared_cache.jset(_VIX_CACHE_KEY, out, ttl=_VIX_CACHE_TTL)
    except Exception:
        pass
    return out


# ── OI-based Support / Resistance levels ───────────────────────────────
#
# Classic options-trader rule:
#   Strikes with the highest Put OI act as SUPPORT (put writers defending
#   the level — they don't want price to fall there).
#   Strikes with the highest Call OI act as RESISTANCE (call writers
#   defending — they don't want price to rise through it).

def _compute_sr_levels(rows: List[Dict[str, Any]], spot: float,
                       top_n: int = 3) -> Dict[str, Any]:
    if not rows:
        return {"support": [], "resistance": [],
                "strongest_support": None, "strongest_resistance": None,
                "range_pct": 0.0}

    # Resistance: highest Call OI strikes ABOVE spot (or nearest).
    ce_sorted = sorted(
        rows,
        key=lambda r: (r.get("ce") or {}).get("oi", 0),
        reverse=True,
    )
    pe_sorted = sorted(
        rows,
        key=lambda r: (r.get("pe") or {}).get("oi", 0),
        reverse=True,
    )

    def _pack(rows_in: List[Dict[str, Any]], side: str) -> List[Dict[str, Any]]:
        out = []
        for r in rows_in[:top_n]:
            leg = r.get(side) or {}
            oi = float(leg.get("oi") or 0)
            if oi <= 0:
                continue
            strike = float(r.get("strike") or 0)
            out.append({
                "strike": strike,
                "oi": int(oi),
                "oi_chg": int(leg.get("oi_chg") or 0),
                "dist_pct": round(((strike - spot) / spot * 100.0) if spot else 0.0, 2),
            })
        return out

    resistance = _pack(ce_sorted, "ce")
    support = _pack(pe_sorted, "pe")

    strongest_resistance = resistance[0]["strike"] if resistance else None
    strongest_support = support[0]["strike"] if support else None
    range_pct = 0.0
    if strongest_resistance and strongest_support and spot:
        range_pct = round(
            (strongest_resistance - strongest_support) / spot * 100.0, 2
        )

    return {
        "support": support,
        "resistance": resistance,
        "strongest_support": strongest_support,
        "strongest_resistance": strongest_resistance,
        "range_pct": range_pct,
    }


# ── Block-deal / Unusual-Options-Activity tracker ──────────────────────
#
# True F&O block deals aren't published live by NSE. We approximate
# institutional / large-trade activity by scanning the option chain for
# strikes where TRADED VOLUME is large relative to OPEN INTEREST — that
# means a big position was opened or rotated today. We also compute the
# rupee notional (volume × LTP × NIFTY lot size = 75) and infer the
# direction from same-day price + OI change.

_NIFTY_LOT_SIZE = 75
_BLOCK_MIN_NOTIONAL_CR = 5.0   # ignore strikes below ₹5 Cr notional
_BLOCK_MIN_VOL = 7_500         # absolute volume floor (in shares) ≈ 100 lots


def _infer_block_direction(leg_type: str, price_chg: float,
                           oi_chg: float) -> Tuple[str, str]:
    """Return (direction, reason) — direction is "BUY" or "WRITE"."""
    # Strong signal: OI rose materially → fresh position.
    if oi_chg > 0 and price_chg > 0:
        # Price up + OI up → buyers aggressive
        if leg_type == "ce":
            return "BUY", "Aggressive Call buying (price↑, OI↑)"
        return "BUY", "Aggressive Put buying (price↑, OI↑)"
    if oi_chg > 0 and price_chg < 0:
        # Price down + OI up → writers aggressive (smart money)
        if leg_type == "ce":
            return "WRITE", "Call writers active (price↓, OI↑) — bearish"
        return "WRITE", "Put writers active (price↓, OI↑) — bullish"
    if oi_chg < 0 and price_chg > 0:
        if leg_type == "ce":
            return "COVER", "Short covering (price↑, OI↓) — bullish"
        return "COVER", "Put short covering (price↑, OI↓) — bearish"
    if oi_chg < 0 and price_chg < 0:
        if leg_type == "ce":
            return "UNWIND", "Long unwinding (price↓, OI↓)"
        return "UNWIND", "Put long unwinding (price↓, OI↓)"
    return "MIXED", "Heavy volume, unclear direction"


# ── Option Buying vs Selling environment ──────────────────────────────
#
# A live verdict telling the user whether the *current* market favors
# being an option BUYER (long premium / theta-negative) or an option
# SELLER (short premium / theta-positive). Combines four signals:
#
#   1. ATM premium responsiveness — for the day's spot move, how much
#      did the ATM straddle (CE + PE) actually move vs the expected
#      move? An over-shooting straddle means vol is expanding → buyer
#      reward. Under-shooting / contracting straddle = theta + vol
#      crush dominate → seller reward.
#   2. India VIX level — cheap IV favors buyers; rich IV favors sellers.
#   3. India VIX intraday trend — rising VIX = long-vol pays;
#      falling VIX = short-vol pays.
#   4. ATM IV vs VIX — when ATM IV is materially richer than VIX, the
#      expiry is over-priced for the regime → seller edge.
#
# All four are normalised to [-1, +1] (positive = buyers, negative =
# sellers), weighted, and combined into a single score and label.

_STRAT_W_RESP = 0.40
_STRAT_W_VIX_LVL = 0.25
_STRAT_W_VIX_TREND = 0.20
_STRAT_W_IV_GAP = 0.15

_STRAT_BUY_TH = 0.18
_STRAT_STRONG_BUY_TH = 0.45
_STRAT_SELL_TH = -0.18
_STRAT_STRONG_SELL_TH = -0.45


def _strat_label(score: float) -> str:
    if score >= _STRAT_STRONG_BUY_TH:
        return "STRONG BUY OPTIONS"
    if score >= _STRAT_BUY_TH:
        return "FAVOR OPTION BUYING"
    if score <= _STRAT_STRONG_SELL_TH:
        return "STRONG SELL OPTIONS"
    if score <= _STRAT_SELL_TH:
        return "FAVOR OPTION SELLING"
    return "NEUTRAL"


def _strat_short_label(label: str) -> str:
    if "STRONG BUY" in label:
        return "STRONG BUY"
    if "BUYING" in label:
        return "BUY"
    if "STRONG SELL" in label:
        return "STRONG SELL"
    if "SELLING" in label:
        return "SELL"
    return "NEUTRAL"


def _atm_row(rows: List[Dict[str, Any]], spot: float) -> Optional[Dict[str, Any]]:
    if not rows or not spot:
        return None
    return min(rows, key=lambda r: abs(float(r.get("strike") or 0) - float(spot)))


def _spot_intraday_change_pct() -> Optional[float]:
    """NIFTY's intraday % change vs previous close via market_data."""
    try:
        from application.services import market_data
        quotes = market_data.get_quotes(["^NSEI"]) or {}
        q = quotes.get("^NSEI") or {}
        cp = q.get("change_pct")
        return float(cp) if cp is not None else None
    except Exception as e:
        log.debug("strategy._spot_intraday_change_pct: %s", e)
        return None


def _compute_strategy_verdict(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether the current regime favors option BUYING or SELLING."""
    rows = payload.get("rows") or []
    spot = float(payload.get("spot") or 0)
    vix = payload.get("vix") or {}
    vix_value = float(vix.get("value") or 0)
    vix_pct = float(vix.get("change_pct") or 0)

    atm = _atm_row(rows, spot)
    if not atm or not spot:
        return {
            "available": False,
            "label": "NEUTRAL",
            "short_label": "NEUTRAL",
            "score": 0.0,
            "confidence": 0.0,
            "summary": "Insufficient data for strategy verdict.",
            "reasons": [],
        }

    atm_strike = float(atm.get("strike") or 0)
    ce = atm.get("ce") or {}
    pe = atm.get("pe") or {}
    ce_ltp = float(ce.get("ltp") or 0)
    pe_ltp = float(pe.get("ltp") or 0)
    ce_chg = float(ce.get("chg") or 0)
    pe_chg = float(pe.get("chg") or 0)
    ce_iv = float(ce.get("iv") or 0)
    pe_iv = float(pe.get("iv") or 0)

    straddle = ce_ltp + pe_ltp
    straddle_prev = (ce_ltp - ce_chg) + (pe_ltp - pe_chg)
    straddle_chg = straddle - straddle_prev
    straddle_chg_pct = (straddle_chg / straddle_prev * 100.0) if straddle_prev > 0 else 0.0
    atm_iv = (ce_iv + pe_iv) / 2.0 if (ce_iv or pe_iv) else 0.0

    spot_pct = _spot_intraday_change_pct()
    spot_pts = (spot * (spot_pct or 0.0) / 100.0) if spot_pct is not None else 0.0

    reasons: List[str] = []

    # (1) Premium responsiveness — straddle expansion vs expectation.
    resp_score = 0.0
    resp_available = False
    expected_chg_pct = 0.0
    if spot_pct is not None and straddle_prev > 0:
        expected_chg_pct = abs(spot_pct) * 0.6
        actual_abs = abs(straddle_chg_pct)
        ratio = (actual_abs - expected_chg_pct) / max(0.5, expected_chg_pct + 0.5)
        sign = 1.0 if straddle_chg_pct >= 0 else -1.0
        resp_score = max(-1.0, min(1.0, ratio * sign))
        resp_available = True
        verb = ("expanding" if straddle_chg_pct >= 0.2 else
                "contracting" if straddle_chg_pct <= -0.2 else "flat")
        reasons.append(
            f"ATM {int(atm_strike)} straddle Rs.{straddle:.0f} "
            f"({straddle_chg_pct:+.2f}%) — premiums {verb} vs "
            f"expected {expected_chg_pct:.2f}% on {spot_pct:+.2f}% spot move."
        )
    elif straddle > 0:
        reasons.append(
            f"ATM {int(atm_strike)} straddle Rs.{straddle:.0f} — "
            "spot intraday change unavailable; VIX-only assessment."
        )

    # (2) VIX level — cheap = buy, rich = sell.
    vix_lvl_score = 0.0
    vix_lvl_available = False
    if vix_value > 0:
        vix_lvl_available = True
        if vix_value <= 10:
            vix_lvl_score = 1.0
        elif vix_value >= 28:
            vix_lvl_score = -1.0
        else:
            vix_lvl_score = round(1.0 - 2.0 * (vix_value - 10) / 18.0, 3)
        regime, _ = _vix_regime(vix_value)
        reasons.append(
            f"India VIX {vix_value:.2f} ({regime}) — "
            + ("cheap IV favors buyers." if vix_lvl_score > 0.15 else
               "rich IV favors sellers." if vix_lvl_score < -0.15 else
               "balanced IV regime.")
        )

    # (3) VIX intraday trend — rising = buy, falling = sell.
    vix_trend_score = 0.0
    vix_trend_available = False
    if vix_value > 0:
        vix_trend_available = True
        vix_trend_score = max(-1.0, min(1.0, vix_pct / 5.0))
        if abs(vix_pct) >= 1.5:
            reasons.append(
                f"VIX {'rising' if vix_pct > 0 else 'falling'} "
                f"{vix_pct:+.2f}% intraday — vol "
                + ("expanding (buyer tailwind)." if vix_pct > 0
                   else "compressing (seller tailwind).")
            )

    # (4) ATM IV vs VIX — over-priced expiry → seller edge.
    iv_gap_score = 0.0
    iv_gap_available = False
    if atm_iv > 0 and vix_value > 0:
        iv_gap_available = True
        diff_pct = ((atm_iv - vix_value) / vix_value) * 100.0
        iv_gap_score = max(-1.0, min(1.0, -diff_pct / 20.0))
        if abs(diff_pct) >= 8:
            reasons.append(
                f"ATM IV {atm_iv:.1f} vs VIX {vix_value:.1f} "
                f"({diff_pct:+.1f}% gap) — "
                + ("strikes expensive vs regime; sellers favored."
                   if diff_pct > 0 else
                   "strikes cheap vs regime; buyers favored.")
            )

    # Weighted aggregate.
    weights = {
        "responsiveness": (_STRAT_W_RESP, resp_score, resp_available),
        "vix_level":      (_STRAT_W_VIX_LVL, vix_lvl_score, vix_lvl_available),
        "vix_trend":      (_STRAT_W_VIX_TREND, vix_trend_score, vix_trend_available),
        "iv_gap":         (_STRAT_W_IV_GAP, iv_gap_score, iv_gap_available),
    }
    num = 0.0
    den = 0.0
    for _, (w, s, ok) in weights.items():
        if not ok:
            continue
        num += w * s
        den += w
    score = (num / den) if den > 0 else 0.0
    score = max(-1.0, min(1.0, round(score, 3)))

    available_n = sum(1 for _, _, ok in weights.values() if ok)
    coverage = available_n / len(weights)
    confidence = round(min(1.0, abs(score) * 0.7 + coverage * 0.3), 2)

    label = _strat_label(score)
    short_label = _strat_short_label(label)

    summary = (
        f"{label.title()} — "
        + ("buying ATM/OTM gives positive vega + gamma exposure."
           if score > 0.18 else
           "writing strangles / iron condors collects rich premium decay."
           if score < -0.18 else
           "edge is small — consider non-directional spreads or wait.")
    )

    return {
        "available": True,
        "label": label,
        "short_label": short_label,
        "score": score,
        "confidence": confidence,
        "summary": summary,
        "reasons": reasons,
        "atm": {
            "strike": int(atm_strike),
            "ce_ltp": round(ce_ltp, 2),
            "pe_ltp": round(pe_ltp, 2),
            "straddle": round(straddle, 2),
            "straddle_chg_pct": round(straddle_chg_pct, 2),
            "atm_iv": round(atm_iv, 2),
        },
        "spot": {
            "value": round(spot, 2),
            "change_pct": round(spot_pct, 3) if spot_pct is not None else None,
            "change_pts": round(spot_pts, 1) if spot_pct is not None else None,
        },
        "vix": {
            "value": round(vix_value, 2),
            "change_pct": round(vix_pct, 2),
        },
        "components": {
            "responsiveness": {
                "score": round(resp_score, 3),
                "available": resp_available,
                "expected_pct": round(expected_chg_pct, 2),
                "actual_pct": round(straddle_chg_pct, 2),
                "weight": _STRAT_W_RESP,
            },
            "vix_level": {
                "score": round(vix_lvl_score, 3),
                "available": vix_lvl_available,
                "weight": _STRAT_W_VIX_LVL,
            },
            "vix_trend": {
                "score": round(vix_trend_score, 3),
                "available": vix_trend_available,
                "weight": _STRAT_W_VIX_TREND,
            },
            "iv_gap": {
                "score": round(iv_gap_score, 3),
                "available": iv_gap_available,
                "weight": _STRAT_W_IV_GAP,
            },
        },
    }


def _compute_block_deals(rows: List[Dict[str, Any]], spot: float,
                         max_deals: int = 10) -> Dict[str, Any]:
    if not rows:
        return {"deals": [], "lot_size": _NIFTY_LOT_SIZE,
                "total_notional_cr": 0.0, "count": 0}

    candidates: List[Dict[str, Any]] = []
    for r in rows:
        strike = float(r.get("strike") or 0)
        for leg_type in ("ce", "pe"):
            leg = r.get(leg_type) or {}
            vol = float(leg.get("vol") or 0)         # already in shares (Fyers convention)
            oi = float(leg.get("oi") or 0)           # in contracts (lots)
            ltp = float(leg.get("ltp") or 0)
            if vol < _BLOCK_MIN_VOL or ltp <= 0:
                continue
            notional_cr = (vol * ltp) / 1e7          # ₹ Cr (vol already share-units)
            if notional_cr < _BLOCK_MIN_NOTIONAL_CR:
                continue
            # OI is in contracts → convert to shares for an apples-to-apples ratio.
            oi_shares = oi * _NIFTY_LOT_SIZE
            vol_oi_ratio = (vol / oi_shares) if oi_shares else (vol / _NIFTY_LOT_SIZE)
            direction, reason = _infer_block_direction(
                leg_type,
                float(leg.get("chg") or 0),
                float(leg.get("oi_chg") or 0),
            )
            candidates.append({
                "type": leg_type.upper(),          # "CE" / "PE"
                "strike": strike,
                "ltp": round(ltp, 2),
                "vol": int(vol),
                "oi": int(oi),
                "oi_chg": int(leg.get("oi_chg") or 0),
                "price_chg": round(float(leg.get("chg") or 0), 2),
                "vol_oi_ratio": round(vol_oi_ratio, 2),
                "notional_cr": round(notional_cr, 2),
                "direction": direction,
                "reason": reason,
                "dist_pct": round(((strike - spot) / spot * 100.0) if spot else 0.0, 2),
            })

    # Rank by ₹ notional (largest first) — the "block-trade" view.
    candidates.sort(key=lambda d: d["notional_cr"], reverse=True)
    deals = candidates[:max_deals]
    total_notional = round(sum(d["notional_cr"] for d in candidates), 2)
    now_ist = _dt.datetime.now(_IST)
    as_of = now_ist.strftime("%d-%b-%Y %H:%M:%S IST")
    # Stamp every deal with the snapshot time so the UI can show it per row.
    for d in deals:
        d["as_of"] = as_of
        d["date"] = now_ist.strftime("%d-%b-%Y")
        d["time"] = now_ist.strftime("%H:%M:%S")
    return {
        "deals": deals,
        "lot_size": _NIFTY_LOT_SIZE,
        "total_notional_cr": total_notional,
        "count": len(candidates),
        "as_of": as_of,
        "date": now_ist.strftime("%d-%b-%Y"),
        "time": now_ist.strftime("%H:%M:%S"),
    }


# ── NSE Market Breadth + Movers ───────────────────────────────────────

def _fetch_market_breadth_movers(top_n: int = 8) -> Optional[Dict[str, Any]]:
    cached = shared_cache.jget(_NSE_BREADTH_CACHE_KEY)
    if isinstance(cached, dict):
        return cached

    try:
        all_idx = _nse_get_json("/api/allIndices", retries=1) or {}
        g_obj = _nse_get_json("/api/live-analysis-variations", params={"index": "gainers"}, retries=1) or {}
        l_obj = _nse_get_json("/api/live-analysis-variations", params={"index": "loosers"}, retries=1) or {}
    except Exception as e:  # noqa: BLE001
        log.debug("breadth_movers.nse failed: %s", e)
        return None

    def _to_int(v: Any) -> int:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    def _pack_movers(items: Any) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for it in (items or [])[:top_n]:
            pct = _num(it.get("net_price") or it.get("pChange") or it.get("per"))
            out.append({
                "symbol": str(it.get("symbol") or "").upper(),
                "ltp": round(_num(it.get("ltp") or it.get("ltP")), 2),
                "pct_chg": round(pct, 2),
                "chg": round(_num(it.get("change") or it.get("net_change")), 2),
                "volume": _to_int(it.get("trade_quantity") or it.get("trdVolM") or 0),
                "turnover": round(_num(it.get("turnover") or 0), 2),
            })
        return [x for x in out if x["symbol"]]

    gains = _pack_movers(((g_obj.get("NIFTY") or {}).get("data") or []))
    losses = _pack_movers(((l_obj.get("NIFTY") or {}).get("data") or []))

    nifty_row = None
    for item in (all_idx.get("data") or []):
        nm = (item.get("indexSymbol") or item.get("index") or "").upper()
        if nm == "NIFTY 50":
            nifty_row = item
            break

    out = {
        "advances": _to_int(all_idx.get("advances") or 0),
        "declines": _to_int(all_idx.get("declines") or 0),
        "unchanged": _to_int(all_idx.get("unchanged") or 0),
        "timestamp": all_idx.get("timestamp") or _dt.datetime.now(_IST).strftime("%I:%M %p IST"),
        "nifty": {
            "last": round(_num((nifty_row or {}).get("last")), 2),
            "chg": round(_num((nifty_row or {}).get("variation") or (nifty_row or {}).get("change")), 2),
            "pct_chg": round(_num((nifty_row or {}).get("percentChange") or (nifty_row or {}).get("pChange")), 2),
        },
        "top_gainers": gains,
        "top_losers": losses,
    }
    try:
        shared_cache.jset(_NSE_BREADTH_CACHE_KEY, out, ttl=_NSE_BREADTH_CACHE_TTL)
    except Exception:
        pass
    return out


# ── NSE Corporate + Reference data ────────────────────────────────────

def _fetch_corporate_reference(top_n: int = 8) -> Optional[Dict[str, Any]]:
    cached = shared_cache.jget(_NSE_CORPREF_CACHE_KEY)
    if isinstance(cached, dict):
        return cached

    try:
        anns = _nse_get_json("/api/corporate-announcements", params={"index": "equities"}, retries=1) or []
        acts = _nse_get_json("/api/corporates-corporateActions", params={"index": "equities"}, retries=1) or []
        bms = _nse_get_json("/api/corporate-board-meetings", params={"index": "equities"}, retries=1) or []
    except Exception as e:  # noqa: BLE001
        log.debug("corp_ref.nse failed: %s", e)
        return None

    def _pack_ann(it: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": str(it.get("symbol") or "").upper(),
            "company": it.get("sm_name") or it.get("company") or "",
            "title": it.get("desc") or "",
            "when": it.get("an_dt") or it.get("dt") or "",
            "url": it.get("attchmntFile") or it.get("attchmntText") or "",
        }

    def _pack_act(it: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": str(it.get("symbol") or "").upper(),
            "series": it.get("series") or "",
            "subject": it.get("subject") or "",
            "ex_date": it.get("exDate") or it.get("faceValDate") or "",
            "record_date": it.get("recordDate") or "",
        }

    def _pack_bm(it: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": str(it.get("bm_symbol") or it.get("symbol") or "").upper(),
            "date": it.get("bm_date") or "",
            "purpose": it.get("bm_purpose") or "",
            "desc": it.get("bm_desc") or "",
        }

    out = {
        "announcements": [_pack_ann(x) for x in anns[:top_n] if isinstance(x, dict)],
        "corporate_actions": [_pack_act(x) for x in acts[:top_n] if isinstance(x, dict)],
        "board_meetings": [_pack_bm(x) for x in bms[:top_n] if isinstance(x, dict)],
        "timestamp": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST"),
    }
    try:
        shared_cache.jset(_NSE_CORPREF_CACHE_KEY, out, ttl=_NSE_CORPREF_CACHE_TTL)
    except Exception:
        pass
    return out
