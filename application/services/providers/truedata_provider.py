"""TrueData market-data provider — REST historical + derived quotes.

Why this exists
---------------
Broker APIs (Fyers / Dhan) authenticate with daily-expiring tokens and a
TOTP login that the broker flags/blocks when driven from a datacenter. That
is not a production-grade data source. TrueData is a *licensed market-data
vendor*: a stable username/password, a REST history feed that works from any
cloud IP, and no daily token dance.

This provider wraps TrueData's official ``truedata`` package (``TD_hist``)
behind the common provider interface used by :mod:`market_data`:

    get_history(symbol, days, interval)      -> OHLCV DataFrame (yfinance-style)
    download_history(symbols, start, end)    -> dict[symbol -> DataFrame]
    get_quote(symbol)                        -> normalized quote dict
    get_quotes(symbols)                      -> dict[symbol -> quote]
    get_info(symbol)                         -> None  (fundamentals via yfinance)
    search(query)                            -> []    (search via yfinance)

Only history + quotes are served here; company metadata / search stay on the
existing yfinance + snapshot_store path (they are quarterly / static data
that TrueData does not provide).

Live streaming (real-time ticks, option-chain) uses the separate ``TD_live``
websocket and is handled elsewhere — this module is REST-only so it stays
stateless and safe to call from every Flask worker.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

import pandas as pd

from application import config

log = logging.getLogger(__name__)

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# ── Interval mapping (app interval → TrueData bar_size) ─────────────────
_BAR_SIZE = {
    "1m": "1 min", "2m": "2 mins", "3m": "3 mins", "5m": "5 mins",
    "10m": "10 mins", "15m": "15 mins", "30m": "30 mins",
    "60m": "60 mins", "1h": "60 mins",
    "1d": "eod", "D": "eod", "day": "eod", "daily": "eod",
    "1wk": "week", "1w": "week", "1mo": "month",
}

# ── Index / symbol mapping (Yahoo-style → TrueData) ─────────────────────
_INDEX_MAP = {
    "^NSEI":     "NIFTY 50",
    "NIFTY":     "NIFTY 50",
    "NIFTY50":   "NIFTY 50",
    "^NSEBANK":  "NIFTY BANK",
    "BANKNIFTY": "NIFTY BANK",
    "^NSEBANKNIFTY": "NIFTY BANK",
    "FINNIFTY":  "NIFTY FIN SERVICE",
    "^CNXFIN":   "NIFTY FIN SERVICE",
    "^BSESN":    "SENSEX",
    "SENSEX":    "SENSEX",
    "^INDIAVIX": "INDIA VIX",
    "INDIAVIX":  "INDIA VIX",
}


def _to_td(symbol: str) -> Optional[str]:
    """Convert a Yahoo-style symbol to a TrueData symbol.

    RELIANCE.NS -> RELIANCE     ^NSEI -> NIFTY 50     NIFTY-I stays as-is.
    Returns None for symbols we can't map (caller then falls back).
    """
    if not symbol:
        return None
    s = symbol.strip().upper()
    if s in _INDEX_MAP:
        return _INDEX_MAP[s]
    # Continuous futures / already-native TrueData symbols pass through.
    if s.endswith("-I") or s.endswith("-II") or s.endswith("-III"):
        return s
    # BSE symbols aren't served here; let the caller fall back to yfinance.
    if s.endswith(".BO"):
        return None
    if s.endswith(".NS"):
        s = s[:-3]
    if s.startswith("^"):
        # Unknown index we don't have a mapping for.
        return None
    return s


# ── TD_hist singleton (per process) ─────────────────────────────────────
_td_lock = threading.Lock()
_td_hist = None            # the TD_hist instance
_td_unavailable = False    # set True after a hard init failure (missing creds/pkg)


def _client():
    """Return a cached TD_hist client, creating it on first use.

    Returns None when credentials are missing or the ``truedata`` package
    isn't installed — callers must handle None and let the dispatcher fall
    back to yfinance.
    """
    global _td_hist, _td_unavailable
    if _td_hist is not None:
        return _td_hist
    if _td_unavailable:
        return None
    user = getattr(config, "TRUEDATA_USERNAME", "") or ""
    pwd = getattr(config, "TRUEDATA_PASSWORD", "") or ""
    if not (user and pwd):
        _td_unavailable = True
        log.warning("truedata: TRUEDATA_USERNAME / TRUEDATA_PASSWORD not set")
        return None
    with _td_lock:
        if _td_hist is not None:
            return _td_hist
        try:
            from truedata import TD_hist  # type: ignore
            import logging as _logging

            client = TD_hist(user, pwd, log_level=_logging.WARNING)
            # TD_hist doesn't raise on a bad login — it just logs and leaves
            # the REST datasource without an access token. Validate that a
            # token was actually issued; otherwise treat as unavailable so
            # the dispatcher falls back to yfinance instead of crashing on
            # every call.
            ds = getattr(client, "historical_datasource", None)
            token = getattr(ds, "access_token", None) if ds else None
            if not token:
                _td_unavailable = True
                log.warning(
                    "truedata: login failed for %s (bad credentials or "
                    "subscription not active); using fallback", user)
                _td_hist = None
                return None
            _td_hist = client
            log.info("truedata: historical client connected as %s", user)
        except Exception as e:  # noqa: BLE001
            _td_unavailable = True
            log.warning("truedata: client init failed (%s); using fallback", e)
            _td_hist = None
    return _td_hist


def _reset_client():
    """Drop the cached client so the next call re-authenticates."""
    global _td_hist, _td_unavailable
    with _td_lock:
        _td_hist = None
        _td_unavailable = False


# ── DataFrame normalisation ─────────────────────────────────────────────
_COL_ALIASES = {
    "time": "Datetime", "timestamp": "Datetime", "date": "Datetime",
    "datetime": "Datetime",
    "open": "Open", "o": "Open",
    "high": "High", "h": "High",
    "low": "Low", "l": "Low",
    "close": "Close", "c": "Close",
    "volume": "Volume", "v": "Volume", "ttq": "Volume",
    "oi": "OI",
}


def _normalize(df: "pd.DataFrame") -> "pd.DataFrame":
    """Coerce a TrueData history frame to the yfinance-style layout the app
    expects: a DatetimeIndex plus ``Open/High/Low/Close/Volume`` columns.
    """
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    ren = {c: _COL_ALIASES[c] for c in out.columns if c in _COL_ALIASES}
    out = out.rename(columns=ren)
    if "Datetime" in out.columns:
        try:
            out["Datetime"] = pd.to_datetime(out["Datetime"])
            out = out.set_index("Datetime")
        except Exception:
            pass
    elif not isinstance(out.index, pd.DatetimeIndex):
        try:
            out.index = pd.to_datetime(out.index)
        except Exception:
            return pd.DataFrame()
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in out.columns:
            out[col] = 0.0
    try:
        out = out.sort_index()
    except Exception:
        pass
    return out


# ── History ─────────────────────────────────────────────────────────────

def get_history(symbol: str, days: int = 30, interval: str = "1d") -> "pd.DataFrame":
    td = _client()
    if td is None:
        return pd.DataFrame()
    sym = _to_td(symbol)
    if not sym:
        return pd.DataFrame()
    bar_size = _BAR_SIZE.get(interval, "eod")
    duration = _duration_for(days, bar_size)
    try:
        df = td.get_historic_data(sym, duration=duration, bar_size=bar_size)
    except Exception as e:  # noqa: BLE001
        log.warning("truedata.get_history(%s): %s", symbol, e)
        _maybe_reset(e)
        return pd.DataFrame()
    return _normalize(df)


def _duration_for(days: int, bar_size: str) -> str:
    """TrueData duration string. Intraday bar sizes are capped to a sane
    window so requests stay small; daily/weekly/monthly scale in days."""
    days = max(1, int(days))
    if bar_size in ("eod", "week", "month"):
        # EOD history: express in days (TrueData accepts 'N D').
        return f"{min(days, 2000)} D"
    # Intraday: cap the lookback so payloads stay light.
    return f"{min(days, 60)} D"


def download_history(symbols: Iterable[str], start, end,
                     interval: str = "1d") -> dict[str, "pd.DataFrame"]:
    td = _client()
    syms = list(symbols)
    if td is None or not syms:
        return {s: pd.DataFrame() for s in syms}
    bar_size = _BAR_SIZE.get(interval, "eod")
    start_dt = _as_dt(start)
    end_dt = _as_dt(end) or _dt.datetime.now(_IST).replace(tzinfo=None)

    def _one(sym: str) -> tuple[str, "pd.DataFrame"]:
        td_sym = _to_td(sym)
        if not td_sym:
            return sym, pd.DataFrame()
        try:
            df = td.get_historic_data(
                td_sym, start_time=start_dt, end_time=end_dt, bar_size=bar_size)
            return sym, _normalize(df)
        except Exception as e:  # noqa: BLE001
            log.warning("truedata.download_history(%s): %s", sym, e)
            _maybe_reset(e)
            return sym, pd.DataFrame()

    out: dict[str, "pd.DataFrame"] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for sym, df in ex.map(_one, syms):
            out[sym] = df
    return out


def _as_dt(v):
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day)
    try:
        return pd.to_datetime(v).to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None


# ── Quotes ──────────────────────────────────────────────────────────────

def get_quote(symbol: str) -> Optional[dict]:
    """Derive a live-ish quote from the latest 1-min bar + prior EOD close."""
    td = _client()
    if td is None:
        return None
    sym = _to_td(symbol)
    if not sym:
        return None

    # 1) Intraday 1-min bars for today → LTP + day OHLC + volume.
    intra = pd.DataFrame()
    try:
        intra = _normalize(td.get_historic_data(sym, duration="1 D", bar_size="1 min"))
    except Exception as e:  # noqa: BLE001
        log.debug("truedata.get_quote intraday(%s): %s", symbol, e)
        _maybe_reset(e)

    # 2) Daily EOD bars → previous close (last completed prior session).
    eod = pd.DataFrame()
    try:
        eod = _normalize(td.get_historic_data(sym, duration="7 D", bar_size="eod"))
    except Exception as e:  # noqa: BLE001
        log.debug("truedata.get_quote eod(%s): %s", symbol, e)

    price = None
    day_open = day_high = day_low = volume = 0.0
    if not intra.empty:
        price = float(intra["Close"].iloc[-1])
        day_open = float(intra["Open"].iloc[0])
        day_high = float(intra["High"].max())
        day_low = float(intra["Low"].min())
        try:
            volume = float(intra["Volume"].sum())
        except Exception:
            volume = 0.0

    prev_close = None
    if not eod.empty:
        closes = eod["Close"].astype(float)
        if price is None:
            price = float(closes.iloc[-1])
            # No intraday: treat the last two EOD bars as today/prev.
            prev_close = float(closes.iloc[-2]) if len(closes) > 1 else price
        else:
            # Intraday present: prev close is the last EOD bar that isn't today.
            today = _dt.datetime.now(_IST).date()
            prior = closes[eod.index.date < today] if hasattr(eod.index, "date") else closes
            prev_close = float(prior.iloc[-1]) if len(prior) else float(closes.iloc[-1])

    if price is None:
        return None
    if prev_close is None:
        prev_close = price
    change = price - prev_close
    change_pct = (change / prev_close * 100.0) if prev_close else 0.0

    return {
        "symbol": symbol,
        "name": symbol.replace(".NS", "").replace(".BO", ""),
        "price": round(float(price), 2),
        "prev_close": round(float(prev_close), 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "day_open": round(day_open, 2),
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "volume": int(volume or 0),
        "market_cap": None,
        "sector": "",
    }


def get_quotes(symbols: Iterable[str]) -> dict[str, dict]:
    syms = list(symbols)
    if not syms or _client() is None:
        return {}
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = ex.map(lambda s: (s, get_quote(s)), syms)
    for sym, q in results:
        if q:
            out[sym] = q
    return out


# ── Metadata / search: not served here (yfinance handles these) ─────────

def get_info(symbol: str) -> Optional[dict]:  # noqa: D401 - interface stub
    return None


def search(query: str) -> list[dict]:  # noqa: D401 - interface stub
    return []


# ── Error handling helper ───────────────────────────────────────────────

def _maybe_reset(exc: Exception) -> None:
    """Re-auth on the next call if the error looks like an auth/session drop."""
    msg = str(exc).lower()
    if any(k in msg for k in ("auth", "login", "token", "unauthorized", "session")):
        _reset_client()
