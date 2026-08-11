"""F&O single-stock next-day gap forecast.

Mirrors the NIFTY "Gap Forecast" idea (see ``option_chain._compute_gap_outlook``)
but applies it to individual NSE F&O underlyings, and ranks the most liquid
~50 names by conviction.

Two tiers (hybrid by design):

1. **List / ranking** — fast. Reuses the live NSE OI-spurts feed already
   wired up in :mod:`oi_buildup` (one network call returns OI + price change
   for the entire F&O universe). For each curated stock we derive a next-day
   directional score from the joint OI-buildup + intraday momentum signal.

2. **Detail** — heavier, on demand. Pulls the stock's own option chain from
   Fyers (``/data/options-chain-v3`` with the stock's underlying symbol) and
   scores the close-window OI flow the same way the index model does
   (put-writing → gap-up bias, call-writing → gap-down bias), enriched with
   PCR and OI-based support/resistance.

Every forecast is persisted via :mod:`gap_history` (partitioned under
``FNO:<symbol>``) so each prediction self-evaluates against the next trading
day's actual open — exactly like the NIFTY signal-accuracy table.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, List, Optional, Tuple

from application import config
from application.services import cache as shared_cache

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

_LIST_CACHE_KEY = "fno_gap:list:v1"
_LIST_CACHE_TTL = 60
_DETAIL_CACHE_KEY_FMT = "fno_gap:detail:{sym}:v1"
_DETAIL_CACHE_TTL = 90

# Direction thresholds (raw score in [-1, +1]).
_GAP_UP_TH = 0.20
_GAP_DOWN_TH = -0.20

# Factor weights for the fast (OI-spurts) list model.
_W_OI = 0.60
_W_MOM = 0.40

# Conviction multiplier per OI-buildup bucket. Sign encodes next-day bias.
#   long buildup    (price↑ OI↑) → fresh longs, bullish continuation
#   short covering  (price↑ OI↓) → shorts exiting, bullish but softer
#   short buildup   (price↓ OI↑) → fresh shorts, bearish continuation
#   long unwinding  (price↓ OI↓) → longs exiting, bearish but softer
_BUILDUP_BIAS = {
    "long_buildup":   1.0,
    "short_covering": 0.6,
    "short_buildup": -1.0,
    "long_unwinding": -0.6,
}
_BUILDUP_LABEL = {
    "long_buildup":   "Long Buildup",
    "short_covering": "Short Covering",
    "short_buildup":  "Short Buildup",
    "long_unwinding": "Long Unwinding",
}


# ── Curated universe: top ~50 most liquid NSE F&O stocks ───────────────
# (symbol, display name). Yahoo / Fyers symbols are derived below.
_UNIVERSE_RAW: List[Tuple[str, str]] = [
    ("RELIANCE", "Reliance Industries"),
    ("HDFCBANK", "HDFC Bank"),
    ("ICICIBANK", "ICICI Bank"),
    ("SBIN", "State Bank of India"),
    ("INFY", "Infosys"),
    ("TCS", "Tata Consultancy Svcs"),
    ("AXISBANK", "Axis Bank"),
    ("KOTAKBANK", "Kotak Mahindra Bank"),
    ("BHARTIARTL", "Bharti Airtel"),
    ("ITC", "ITC"),
    ("LT", "Larsen & Toubro"),
    ("HINDUNILVR", "Hindustan Unilever"),
    ("BAJFINANCE", "Bajaj Finance"),
    ("MARUTI", "Maruti Suzuki"),
    ("TATAMOTORS", "Tata Motors"),
    ("TATASTEEL", "Tata Steel"),
    ("SUNPHARMA", "Sun Pharma"),
    ("WIPRO", "Wipro"),
    ("HCLTECH", "HCL Technologies"),
    ("ADANIENT", "Adani Enterprises"),
    ("ADANIPORTS", "Adani Ports"),
    ("ASIANPAINT", "Asian Paints"),
    ("TITAN", "Titan Company"),
    ("ULTRACEMCO", "UltraTech Cement"),
    ("BAJAJFINSV", "Bajaj Finserv"),
    ("NTPC", "NTPC"),
    ("POWERGRID", "Power Grid"),
    ("ONGC", "ONGC"),
    ("COALINDIA", "Coal India"),
    ("JSWSTEEL", "JSW Steel"),
    ("HINDALCO", "Hindalco"),
    ("GRASIM", "Grasim Industries"),
    ("M&M", "Mahindra & Mahindra"),
    ("BAJAJ-AUTO", "Bajaj Auto"),
    ("EICHERMOT", "Eicher Motors"),
    ("HEROMOTOCO", "Hero MotoCorp"),
    ("DRREDDY", "Dr Reddy's Labs"),
    ("CIPLA", "Cipla"),
    ("BRITANNIA", "Britannia"),
    ("NESTLEIND", "Nestle India"),
    ("TECHM", "Tech Mahindra"),
    ("INDUSINDBK", "IndusInd Bank"),
    ("SBILIFE", "SBI Life Insurance"),
    ("HDFCLIFE", "HDFC Life"),
    ("BPCL", "BPCL"),
    ("DLF", "DLF"),
    ("VEDL", "Vedanta"),
    ("TATACONSUM", "Tata Consumer"),
    ("APOLLOHOSP", "Apollo Hospitals"),
    ("DIVISLAB", "Divi's Laboratories"),
]

# Yahoo tickers that don't translate via a plain ``.NS`` suffix.
_YAHOO_OVERRIDES = {
    "M&M": "M&M.NS",
}


def _yahoo_ticker(symbol: str) -> str:
    return _YAHOO_OVERRIDES.get(symbol, f"{symbol}.NS")


def _fyers_symbol(symbol: str) -> str:
    return f"NSE:{symbol}-EQ"


def _partition_key(symbol: str) -> str:
    return f"FNO:{symbol}"


# Public catalogue: {symbol: {name, yahoo, fyers, pkey}}.
UNIVERSE: Dict[str, Dict[str, str]] = {
    sym: {
        "name": name,
        "yahoo": _yahoo_ticker(sym),
        "fyers": _fyers_symbol(sym),
        "pkey": _partition_key(sym),
    }
    for sym, name in _UNIVERSE_RAW
}

# Register Yahoo tickers so gap_history can resolve next-day opens for the
# per-stock partition keys when it evaluates pending predictions.
try:
    from application.services import gap_history as _gh
    _gh.register_history_symbols(
        {meta["pkey"]: meta["yahoo"] for meta in UNIVERSE.values()}
    )
except Exception as _e:  # noqa: BLE001
    log.debug("fno_gap_forecast: history-symbol registration failed: %s", _e)


# ── Shared helpers ─────────────────────────────────────────────────────

def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _label_from_score(score: float) -> str:
    if score >= _GAP_UP_TH:
        return "GAP UP"
    if score <= _GAP_DOWN_TH:
        return "GAP DOWN"
    return "FLAT"


def _session_weight() -> Tuple[float, str, bool]:
    """Confidence weight ∈ [0,1], phase label, and whether we're in a
    full-session read (post-close / pre-open). Mirrors the index model:
    the next-day gap call is only meaningful late in the session."""
    now = _dt.datetime.now(_IST)
    mins = now.hour * 60 + now.minute
    market_open = 9 * 60 + 15
    market_close = 15 * 60 + 30
    if mins > market_close:
        return 0.9, "Post-close — forecast for next open", True
    if mins < market_open:
        return 0.7, "Pre-open — based on previous close", True
    start = 13 * 60 + 30          # 13:30 IST
    full = 15 * 60 + 15           # 15:15 IST → full confidence
    if mins < start:
        return 0.0, "Intraday — forecast activates after 13:30 IST", False
    if mins >= full:
        return 1.0, "Decision window — call locking by 15:15", False
    return round((mins - start) / (full - start), 2), "Late session — building confidence", False


def _probability(gap_score: float, confidence: float, label: str) -> int:
    """Map signal strength + confidence to a 50–95% directional probability."""
    if label == "FLAT":
        # Lower band: probability the move stays roughly flat.
        return int(round(_clamp(50 + 30 * (1 - abs(gap_score)) * confidence, 40, 75)))
    base = 50 + 45 * abs(gap_score) * (0.55 + 0.45 * confidence)
    return int(round(max(50.0, min(95.0, base))))


def _estimate_points(spot: float, label: str, gap_score: float, confidence: float) -> Dict[str, Any]:
    """Reuse the index magnitude estimator for a consistent gap envelope."""
    try:
        from application.services.option_chain import _estimate_gap_points
        return _estimate_gap_points(
            spot=spot, label=label, gap_score=gap_score,
            confidence=confidence, vix_change_pct=0.0,
        )
    except Exception:
        return {
            "expected_gap_points": 0, "expected_gap_pct": 0.0,
            "expected_gap_points_low": 0, "expected_gap_points_high": 0,
        }


def _persist(symbol: str, meta: Dict[str, str], spot: float, outlook: Dict[str, Any]) -> None:
    """Best-effort: record today's prediction for next-day self-evaluation."""
    try:
        from application.services import gap_history
        gap_history.record(
            meta["pkey"],
            label=outlook.get("label") or "",
            raw_score=float(outlook.get("raw_score") or 0),
            gap_score=float(outlook.get("gap_score") or 0),
            confidence=float(outlook.get("confidence") or 0),
            spot=spot,
            expected_gap_points=outlook.get("expected_gap_points"),
            expected_gap_pct=outlook.get("expected_gap_pct"),
            expected_gap_points_low=outlook.get("expected_gap_points_low"),
            expected_gap_points_high=outlook.get("expected_gap_points_high"),
            probability=outlook.get("probability"),
            summary=outlook.get("summary") or "",
        )
    except Exception as e:  # noqa: BLE001
        log.debug("fno_gap_forecast._persist(%s): %s", symbol, e)


# ── Tier 1: fast ranked list (OI-spurts feed) ──────────────────────────

def _score_from_buildup(price_chg_pct: float, oi_chg_pct: float,
                        weight: float) -> Dict[str, Any]:
    """Score a single stock from its intraday OI-buildup + momentum."""
    from application.services.oi_buildup import _classify

    bucket = _classify(price_chg_pct, oi_chg_pct)
    bias = _BUILDUP_BIAS.get(bucket, 0.0)

    # Factor 1: OI-buildup flow — conviction scaled by OI-change magnitude.
    oi_mag = _clamp(abs(oi_chg_pct) / 20.0, 0.0, 1.0)   # 20% OI swing → full
    oi_factor = bias * oi_mag

    # Factor 2: intraday momentum into the close.
    mom_factor = _clamp(price_chg_pct / 3.0)            # 3% move → full

    raw = _clamp(_W_OI * oi_factor + _W_MOM * mom_factor)
    gap_score = _clamp(raw * weight)
    label = _label_from_score(gap_score)
    confidence = round(weight * (0.55 + 0.45 * oi_mag), 2)
    return {
        "bucket": bucket,
        "buildup": _BUILDUP_LABEL.get(bucket, bucket),
        "raw_score": round(raw, 3),
        "gap_score": round(gap_score, 3),
        "label": label,
        "confidence": confidence,
    }


def _summarize(name: str, label: str, buildup: str,
               price_chg_pct: float, oi_chg_pct: float) -> str:
    if label == "GAP UP":
        lead = f"{name}: {buildup.lower()} into the close favours a gap-up open."
    elif label == "GAP DOWN":
        lead = f"{name}: {buildup.lower()} into the close favours a gap-down open."
    else:
        lead = f"{name}: positioning is balanced — no clear gap bias."
    return (f"{lead} Price {price_chg_pct:+.2f}%, OI {oi_chg_pct:+.2f}%.")


def get_forecast_list(force_refresh: bool = False) -> Dict[str, Any]:
    """Ranked next-day gap forecast for the curated F&O universe."""
    from application.services import snapshot_store
    return snapshot_store.serve_or_refresh(
        _LIST_CACHE_KEY, lambda: _build_forecast_list(force_refresh),
        live=False, force=force_refresh)


def _build_forecast_list(force_refresh: bool = False) -> Dict[str, Any]:
    from application.services import oi_buildup as oi_svc

    weight, phase, _full = _session_weight()
    data = oi_svc.oi_buildup(force=force_refresh)

    # Flatten all buckets into one {symbol: row} map.
    by_symbol: Dict[str, Dict[str, Any]] = {}
    for rows in (data.get("buckets") or {}).values():
        for r in rows or []:
            sym = (r.get("symbol") or "").strip().upper()
            if sym:
                by_symbol[sym] = r

    items: List[Dict[str, Any]] = []
    for sym, meta in UNIVERSE.items():
        row = by_symbol.get(sym)
        if not row:
            continue
        price_chg = float(row.get("price_chg_pct") or 0.0)
        oi_chg_pct = float(row.get("oi_chg_pct") or 0.0)
        ltp = float(row.get("ltp") or 0.0)

        scored = _score_from_buildup(price_chg, oi_chg_pct, weight)
        pts = _estimate_points(ltp, scored["label"],
                               scored["gap_score"], scored["confidence"])
        probability = _probability(scored["gap_score"],
                                   scored["confidence"], scored["label"])
        summary = _summarize(meta["name"], scored["label"], scored["buildup"],
                             price_chg, oi_chg_pct)

        item = {
            "symbol": sym,
            "name": meta["name"],
            "ltp": round(ltp, 2),
            "price_chg_pct": round(price_chg, 2),
            "oi_chg_pct": round(oi_chg_pct, 2),
            "oi_chg": int(row.get("oi_chg") or 0),
            "buildup": scored["buildup"],
            "bucket": scored["bucket"],
            "label": scored["label"],
            "raw_score": scored["raw_score"],
            "gap_score": scored["gap_score"],
            "confidence": scored["confidence"],
            "probability": probability,
            "summary": summary,
            **pts,
        }
        items.append(item)
        _persist(sym, meta, ltp, item)

    # Rank by conviction = |score| × confidence, strongest calls first.
    items.sort(key=lambda x: abs(x["gap_score"]) * (x["confidence"] or 0),
               reverse=True)

    payload = {
        "items": items,
        "count": len(items),
        "phase": phase,
        "weight": weight,
        "as_of": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST"),
        "source": data.get("source") or "nseindia.com",
        "stale": bool(data.get("stale")),
        "cached": False,
    }
    return payload


# ── Tier 2: per-stock option-chain detail (Fyers) ──────────────────────

def _fetch_stock_chain(fyers_symbol: str, strikecount: int = 15) -> Optional[Dict[str, Any]]:
    """Fetch a single stock's option chain from Fyers and normalise it the
    same way ``option_chain._fetch_via_fyers`` does for NIFTY."""
    import requests
    from application.services.option_chain import (
        _FY_BASE, _FY_PATH, _fy_headers, _num, _empty_leg, _totals,
    )

    if not (config.FYERS_APP_ID and config.FYERS_ACCESS_TOKEN):
        return None

    params = {"symbol": fyers_symbol, "strikecount": strikecount, "timestamp": ""}
    r = requests.get(_FY_BASE + _FY_PATH, params=params,
                     headers=_fy_headers(), timeout=12)
    if r.status_code == 401 or (
        r.status_code >= 400 and "could not authenticate" in r.text.lower()
    ):
        try:
            from application.services.providers import fyers_provider
            if fyers_provider._try_refresh_token():
                r = requests.get(_FY_BASE + _FY_PATH, params=params,
                                 headers=_fy_headers(), timeout=12)
        except Exception as e:
            log.warning("fno_gap_forecast: fyers refresh failed: %s", e)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")

    body = r.json() or {}
    if body.get("s") and body.get("s") != "ok":
        raise RuntimeError(f"upstream error: {str(body)[:160]}")
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
        }
        bucket = by_strike.setdefault(strike_f, {"strike": strike_f})
        bucket["ce" if opt_type == "CE" else "pe"] = leg

    rows: List[Dict[str, Any]] = []
    for strike_f in sorted(by_strike):
        row = by_strike[strike_f]
        row.setdefault("ce", _empty_leg())
        row.setdefault("pe", _empty_leg())
        rows.append(row)

    totals = _totals(rows)
    pcr = (totals["pe_oi"] / totals["ce_oi"]) if totals["ce_oi"] else 0.0

    exp_list = data.get("expiryData") or []
    expiry_label = ""
    if exp_list:
        expiry_label = exp_list[0].get("date") or ""

    return {
        "spot": round(spot, 2),
        "rows": rows,
        "totals": totals,
        "pcr": round(pcr, 3),
        "expiry": expiry_label,
    }


def _score_from_chain(chain: Dict[str, Any], price_chg_pct: float,
                      weight: float) -> Dict[str, Any]:
    """Score next-day gap from the stock's own option-chain close-window
    OI flow (put-writing → bullish, call-writing → bearish), PCR and
    intraday momentum."""
    totals = chain.get("totals") or {}
    ce_chg = float(totals.get("ce_oi_chg") or 0.0)
    pe_chg = float(totals.get("pe_oi_chg") or 0.0)

    # Factor 1: net OI writing flow.
    denom = abs(ce_chg) + abs(pe_chg)
    oi_raw = ((pe_chg - ce_chg) / denom) if denom else 0.0

    # Factor 2: PCR tilt (PCR > 1 → put-heavy → supportive/bullish).
    pcr = float(chain.get("pcr") or 0.0)
    pcr_raw = _clamp((pcr - 1.0) / 0.6) if pcr else 0.0

    # Factor 3: intraday momentum.
    mom_raw = _clamp(price_chg_pct / 3.0)

    raw = _clamp(0.6 * oi_raw + 0.2 * pcr_raw + 0.2 * mom_raw)
    gap_score = _clamp(raw * weight)
    label = _label_from_score(gap_score)
    confidence = round(weight * (0.6 + 0.4 * min(1.0, denom / 1e6 if denom else 0.0)), 2)
    confidence = max(0.0, min(1.0, confidence))
    return {
        "raw_score": round(raw, 3),
        "gap_score": round(gap_score, 3),
        "label": label,
        "confidence": confidence,
        "oi_raw": round(oi_raw, 3),
        "pcr_raw": round(pcr_raw, 3),
        "mom_raw": round(mom_raw, 3),
        "ce_oi_chg": int(ce_chg),
        "pe_oi_chg": int(pe_chg),
    }


def _key_levels(chain: Dict[str, Any]) -> Dict[str, Any]:
    """OI-based support (max PE OI) and resistance (max CE OI)."""
    rows = chain.get("rows") or []
    support = resistance = None
    max_pe = max_ce = -1.0
    for r in rows:
        pe_oi = float((r.get("pe") or {}).get("oi") or 0)
        ce_oi = float((r.get("ce") or {}).get("oi") or 0)
        if pe_oi > max_pe:
            max_pe, support = pe_oi, r.get("strike")
        if ce_oi > max_ce:
            max_ce, resistance = ce_oi, r.get("strike")
    return {"support": support, "resistance": resistance}


def get_forecast_detail(symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
    """Deep per-stock forecast from the option chain + history/accuracy."""
    symbol = (symbol or "").strip().upper()
    meta = UNIVERSE.get(symbol)
    if not meta:
        return {"error": "unknown_symbol",
                "detail": f"{symbol} is not in the F&O universe."}

    cache_key = _DETAIL_CACHE_KEY_FMT.format(sym=symbol)
    if not force_refresh:
        cached = shared_cache.jget(cache_key)
        if isinstance(cached, dict):
            cached = {**cached, "cached": True}
            cached["history"] = _history_block(meta["pkey"])
            return cached

    weight, phase, _full = _session_weight()

    # Intraday price change from the (cheap) OI-spurts feed if available.
    price_chg_pct = 0.0
    try:
        lst = get_forecast_list(force_refresh=False)
        for it in lst.get("items") or []:
            if it.get("symbol") == symbol:
                price_chg_pct = float(it.get("price_chg_pct") or 0.0)
                break
    except Exception:
        pass

    # Try Dhan first (stable), then Fyers fallback.
    chain = None
    try:
        from application.services.option_chain import _fetch_stock_chain_dhan
        chain = _fetch_stock_chain_dhan(symbol)
    except Exception as e:  # noqa: BLE001
        log.debug("fno_gap_forecast.detail(%s) dhan chain: %s", symbol, e)
    if not chain:
        try:
            chain = _fetch_stock_chain(meta["fyers"])
        except Exception as e:  # noqa: BLE001
            log.warning("fno_gap_forecast.detail(%s) chain failed: %s", symbol, e)
            chain = None

    if not chain:
        # Market closed or chain unavailable — serve the last cached detail
        # (which has the probability from the last session) + fresh history.
        cached = shared_cache.jget(cache_key)
        if isinstance(cached, dict) and cached.get("label"):
            cached["cached"] = True
            cached["history"] = _history_block(meta["pkey"])
            return cached
        return {
            "error": "chain_unavailable",
            "detail": ("Option chain for this stock is unavailable right now "
                       "(broker feed off-hours or token expired)."),
            "symbol": symbol,
            "name": meta["name"],
            "history": _history_block(meta["pkey"]),
        }

    scored = _score_from_chain(chain, price_chg_pct, weight)
    spot = float(chain.get("spot") or 0.0)
    pts = _estimate_points(spot, scored["label"],
                           scored["gap_score"], scored["confidence"])
    probability = _probability(scored["gap_score"],
                               scored["confidence"], scored["label"])
    levels = _key_levels(chain)

    if scored["label"] == "GAP UP":
        summary = (f"{meta['name']}: put-writers dominate the closing OI flow "
                   "— bias favours a gap-up open.")
    elif scored["label"] == "GAP DOWN":
        summary = (f"{meta['name']}: call-writers dominate the closing OI flow "
                   "— bias favours a gap-down open.")
    else:
        summary = (f"{meta['name']}: option writing is balanced into the close "
                   "— no clear gap bias.")

    outlook = {
        "label": scored["label"],
        "raw_score": scored["raw_score"],
        "gap_score": scored["gap_score"],
        "confidence": scored["confidence"],
        "probability": probability,
        "summary": summary,
        **pts,
    }
    _persist(symbol, meta, spot, outlook)

    payload = {
        "symbol": symbol,
        "name": meta["name"],
        "spot": spot,
        "expiry": chain.get("expiry"),
        "pcr": chain.get("pcr"),
        "phase": phase,
        "weight": weight,
        "price_chg_pct": round(price_chg_pct, 2),
        "support": levels.get("support"),
        "resistance": levels.get("resistance"),
        "totals": chain.get("totals"),
        "factors": {
            "oi_raw": scored["oi_raw"],
            "pcr_raw": scored["pcr_raw"],
            "mom_raw": scored["mom_raw"],
            "ce_oi_chg": scored["ce_oi_chg"],
            "pe_oi_chg": scored["pe_oi_chg"],
        },
        "as_of": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST"),
        "model": "fno_chain_v1",
        "cached": False,
        **outlook,
    }
    try:
        shared_cache.jset(cache_key, payload, ttl=_DETAIL_CACHE_TTL)
    except Exception:
        pass
    payload["history"] = _history_block(meta["pkey"])
    return payload


def _history_block(pkey: str) -> Dict[str, Any]:
    """Recent persisted predictions + hit-rate stats for a partition key."""
    try:
        from application.services import gap_history
        try:
            gap_history.evaluate_pending(pkey)
        except Exception:
            pass
        return {
            "items": gap_history.recent(pkey, limit=20),
            "stats": gap_history.stats(pkey, lookback=60),
        }
    except Exception as e:  # noqa: BLE001
        log.debug("fno_gap_forecast._history_block(%s): %s", pkey, e)
        return {"items": [], "stats": {}}


# ── Accuracy / summary dashboard ───────────────────────────────────────

_SUMMARY_CACHE_KEY = "fno_gap:summary:v2"
_SUMMARY_CACHE_TTL = 300

# Probability buckets used to test whether higher model probability really
# translates into a higher next-day hit rate.
_PROB_BUCKETS = [(50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]


def _prob_bucket_index(prob: float) -> int:
    """Index of the probability bucket a value falls into (-1 if out of range)."""
    for i, (lo, hi) in enumerate(_PROB_BUCKETS):
        # Last bucket is inclusive of 100.
        if prob >= lo and (prob < hi or (i == len(_PROB_BUCKETS) - 1 and prob <= 100)):
            return i
    return -1


def _stats_from_items(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """Hit/miss/pending tally from a list of ``gap_history.recent`` rows."""
    hits = misses = pending = directional = 0
    for it in items:
        outcome = (it.get("outcome") or "PENDING").upper()
        predicted = (it.get("predicted") or "").upper()
        if outcome == "PENDING":
            pending += 1
        if predicted in ("GAP UP", "GAP DOWN") and outcome in ("HIT", "MISS"):
            directional += 1
            if outcome == "HIT":
                hits += 1
            else:
                misses += 1
    return {
        "hits": hits, "misses": misses, "pending": pending,
        "directional": directional, "signals": len(items),
    }


def get_accuracy_summary(force_refresh: bool = False) -> Dict[str, Any]:
    """Aggregate signal-accuracy across the whole F&O universe.

    Returns overall hit/miss tallies, a per-stock leaderboard, a probability
    vs hit-rate breakdown, and a flat feed of *graded* forecasts (pending ones
    are intentionally excluded — they live on the forecast page).
    """
    if not force_refresh:
        cached = shared_cache.jget(_SUMMARY_CACHE_KEY)
        if isinstance(cached, dict):
            return {**cached, "cached": True}

    from application.services import gap_history

    tot_hits = tot_misses = tot_pending = tot_directional = tot_signals = 0
    per_stock: List[Dict[str, Any]] = []
    feed: List[Dict[str, Any]] = []
    # Probability-bucket accumulators: evaluated/hits per bucket.
    bucket_eval = [0] * len(_PROB_BUCKETS)
    bucket_hits = [0] * len(_PROB_BUCKETS)

    _GRADED = ("HIT", "MISS", "FLAT_OK", "FLAT_MISS")

    for sym, meta in UNIVERSE.items():
        pkey = meta["pkey"]
        # Grade any pending predictions whose next-session open is now
        # available. A forced refresh bypasses the 1/hour/symbol throttle so
        # the user can complete grading on demand.
        try:
            gap_history.evaluate_pending(pkey, force=force_refresh)
        except Exception:
            pass
        try:
            items = gap_history.recent(pkey, limit=90)
        except Exception:
            items = []
        if not items:
            continue

        st = _stats_from_items(items)
        tot_hits += st["hits"]
        tot_misses += st["misses"]
        tot_pending += st["pending"]
        tot_directional += st["directional"]
        tot_signals += st["signals"]

        # Probability bucketing + graded-only feed (skip pending rows).
        for it in items:
            outcome = (it.get("outcome") or "PENDING").upper()
            predicted = (it.get("predicted") or "").upper()
            prob = it.get("probability")
            if (predicted in ("GAP UP", "GAP DOWN")
                    and outcome in ("HIT", "MISS")
                    and prob is not None):
                bi = _prob_bucket_index(float(prob))
                if bi >= 0:
                    bucket_eval[bi] += 1
                    if outcome == "HIT":
                        bucket_hits[bi] += 1
            if outcome in _GRADED:
                feed.append({"symbol": sym, "name": meta["name"], **it})

        latest = items[0]
        per_stock.append({
            "symbol": sym,
            "name": meta["name"],
            "hits": st["hits"],
            "misses": st["misses"],
            "pending": st["pending"],
            "evaluated": st["directional"],
            "signals": st["signals"],
            "hit_rate_pct": (round(st["hits"] / st["directional"] * 100.0, 1)
                             if st["directional"] else None),
            "latest_date": latest.get("date"),
            "latest_label": latest.get("predicted"),
            "latest_probability": latest.get("probability"),
            "latest_outcome": latest.get("outcome") or "PENDING",
        })

    overall_hit_rate = (round(tot_hits / tot_directional * 100.0, 1)
                        if tot_directional else None)

    # Probability vs hit-rate buckets.
    prob_buckets = []
    for i, (lo, hi) in enumerate(_PROB_BUCKETS):
        ev = bucket_eval[i]
        hi_ = bucket_hits[i]
        prob_buckets.append({
            "label": f"{lo}\u2013{hi}%",
            "lo": lo,
            "hi": hi,
            "evaluated": ev,
            "hits": hi_,
            "misses": ev - hi_,
            "hit_rate_pct": (round(hi_ / ev * 100.0, 1) if ev else None),
        })

    # Leaderboard: best hit-rate first; stocks with no graded calls sink down.
    per_stock.sort(
        key=lambda x: (x["hit_rate_pct"] if x["hit_rate_pct"] is not None else -1.0,
                       x["evaluated"]),
        reverse=True,
    )
    # Global feed: newest graded predictions first.
    feed.sort(key=lambda x: (x.get("date") or "", x.get("symbol") or ""),
              reverse=True)

    payload = {
        "totals": {
            "tracked_stocks": len(per_stock),
            "universe": len(UNIVERSE),
            "signals": tot_signals,
            "evaluated": tot_directional,
            "hits": tot_hits,
            "misses": tot_misses,
            "pending": tot_pending,
            "hit_rate_pct": overall_hit_rate,
        },
        "probability_buckets": prob_buckets,
        "stocks": per_stock,
        "forecasts": feed[:150],
        "as_of": _dt.datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST"),
        "cached": False,
    }
    try:
        shared_cache.jset(_SUMMARY_CACHE_KEY, payload, ttl=_SUMMARY_CACHE_TTL)
    except Exception:
        pass
    return payload
