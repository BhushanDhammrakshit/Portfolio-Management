"""Market data abstraction layer.

All routes / services should call functions from this module instead of
talking to ``yfinance`` (or any vendor SDK) directly. The active provider
is selected by the ``MARKET_DATA_PROVIDER`` env var; an optional fallback
provider (``MARKET_DATA_FALLBACK``) is used transparently when the primary
returns no data or doesn't implement a call.

Supported providers
-------------------
- ``dhan``     — DhanHQ v2 REST API. Licensed exchange data; recommended
                 for production. Requires ``DHAN_CLIENT_ID`` and
                 ``DHAN_ACCESS_TOKEN``.
- ``yfinance`` — Yahoo Finance via the ``yfinance`` package. Free but
                 unlicensed; suitable for development.

Public surface
--------------
``get_history(symbol, days, interval)``       OHLCV DataFrame for one symbol
``download_history(symbols, start, end, interval)`` dict[symbol -> DataFrame]
``get_quote(symbol)``                         Normalized quote dict
``get_quotes(symbols)``                       dict[symbol -> quote] (batched)
``get_info(symbol)``                          Sector / market-cap / display name
``search(query)``                             Symbol search results
``provider_name()``                           Active primary provider id
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

from application import config
from application.services import quote_cache
from application.services.providers import (
    dhan_provider,
    fyers_provider,
    yfinance_provider,
)

log = logging.getLogger(__name__)

_PROVIDERS = {
    "dhan": dhan_provider,
    "fyers": fyers_provider,
    "yfinance": yfinance_provider,
}


def _quote_cache_enabled() -> bool:
    """Use the split-provider cache when at least one broker is configured.

    The cache itself splits load between Dhan and Fyers (whichever are
    available) and shields upstream from concurrent users via a 15 s TTL
    plus single-flight locking.
    """
    have_dhan = bool(config.DHAN_CLIENT_ID and config.DHAN_ACCESS_TOKEN)
    have_fyers = bool(config.FYERS_APP_ID and config.FYERS_ACCESS_TOKEN)
    return have_dhan or have_fyers


def _primary():
    name = (config.MARKET_DATA_PROVIDER or "yfinance").lower()
    p = _PROVIDERS.get(name)
    if p is None:
        log.warning("Unknown MARKET_DATA_PROVIDER=%r — using yfinance", name)
        return yfinance_provider
    return p


def _fallback():
    name = (config.MARKET_DATA_FALLBACK or "").lower()
    if name in ("", "none", "off"):
        return None
    if name == config.MARKET_DATA_PROVIDER.lower():
        return None  # avoid infinite "fallback to self"
    return _PROVIDERS.get(name)


def provider_name() -> str:
    return (config.MARKET_DATA_PROVIDER or "yfinance").lower()


# ── Helpers ─────────────────────────────────────────────────────────────

def _try(primary_call, fallback_call, *, empty_check):
    """Run ``primary_call``; if its result is empty, retry with fallback."""
    try:
        result = primary_call()
    except Exception as e:
        log.warning("primary provider failed: %s", e)
        result = None
    if not empty_check(result):
        return result
    fb = _fallback()
    if fb is None:
        return result
    try:
        fb_call = fallback_call(fb)
        return fb_call
    except Exception as e:
        log.warning("fallback provider failed: %s", e)
        return result


def _df_empty(df) -> bool:
    return df is None or (isinstance(df, pd.DataFrame) and df.empty)


def _none_or_empty(v) -> bool:
    return v is None or v == {} or v == []


# ── Public API ──────────────────────────────────────────────────────────

def get_history(symbol: str, days: int = 30, interval: str = "1d") -> pd.DataFrame:
    primary = _primary()
    return _try(
        lambda: primary.get_history(symbol, days=days, interval=interval),
        lambda fb: fb.get_history(symbol, days=days, interval=interval),
        empty_check=_df_empty,
    )


def download_history(symbols: Iterable[str], start, end,
                     interval: str = "1d") -> dict[str, pd.DataFrame]:
    primary = _primary()
    syms = list(symbols)
    try:
        result = primary.download_history(syms, start, end, interval=interval)
    except Exception as e:
        log.warning("download_history primary failed: %s", e)
        result = {}

    # Fill in any symbols the primary couldn't provide using the fallback.
    fb = _fallback()
    missing = [s for s in syms if _df_empty(result.get(s))]
    if missing and fb is not None:
        try:
            fb_data = fb.download_history(missing, start, end, interval=interval)
            for s, df in fb_data.items():
                if not _df_empty(df):
                    result[s] = df
        except Exception as e:
            log.warning("download_history fallback failed: %s", e)
    return result


def get_quote(symbol: str) -> Optional[dict]:
    if _quote_cache_enabled():
        q = quote_cache.get_quote(symbol)
        if q:
            return q
        # Cache miss across both brokers — try the configured fallback
        # (typically yfinance) so the caller still gets *something*.
        fb = _fallback()
        if fb is not None:
            try:
                return fb.get_quote(symbol)
            except Exception as e:
                log.warning("get_quote fallback failed: %s", e)
        return None

    primary = _primary()
    return _try(
        lambda: primary.get_quote(symbol),
        lambda fb: fb.get_quote(symbol),
        empty_check=_none_or_empty,
    )


def get_quotes(symbols: Iterable[str]) -> dict[str, dict]:
    syms = list(symbols)
    if _quote_cache_enabled():
        result = quote_cache.get_quotes(syms)
        # Anything neither Dhan nor Fyers returned — fill from fallback.
        missing = [s for s in syms if not result.get(s)]
        fb = _fallback()
        if missing and fb is not None:
            try:
                fb_data = fb.get_quotes(missing) or {}
                for s, q in fb_data.items():
                    if q:
                        result[s] = q
            except Exception as e:
                log.warning("get_quotes fallback failed: %s", e)
        return result

    primary = _primary()
    try:
        result = primary.get_quotes(syms)
    except Exception as e:
        log.warning("get_quotes primary failed: %s", e)
        result = {}

    missing = [s for s in syms if s not in result or not result.get(s)]
    fb = _fallback()
    if missing and fb is not None:
        try:
            fb_data = fb.get_quotes(missing)
            for s, q in fb_data.items():
                if q:
                    result[s] = q
        except Exception as e:
            log.warning("get_quotes fallback failed: %s", e)
    return result


def get_info(symbol: str) -> Optional[dict]:
    """Get name/sector/industry/market-cap.

    Dhan does not expose sector or market cap, so when the primary is Dhan
    we always merge in yfinance metadata when available.
    """
    primary = _primary()
    out = None
    try:
        out = primary.get_info(symbol)
    except Exception as e:
        log.warning("get_info primary failed: %s", e)

    needs_meta = (
        out is None
        or not out.get("sector")
        or out.get("market_cap") in (None, 0)
    )
    if needs_meta:
        fb = _fallback()
        if fb is not None:
            try:
                fb_info = fb.get_info(symbol)
                if fb_info:
                    if out is None:
                        out = fb_info
                    else:
                        for k, v in fb_info.items():
                            if v and not out.get(k):
                                out[k] = v
            except Exception as e:
                log.warning("get_info fallback failed: %s", e)
    return out


def search(query: str) -> list[dict]:
    primary = _primary()
    try:
        results = primary.search(query) or []
    except Exception as e:
        log.warning("search primary failed: %s", e)
        results = []
    if results:
        return results
    fb = _fallback()
    if fb is None:
        return []
    try:
        return fb.search(query) or []
    except Exception as e:
        log.warning("search fallback failed: %s", e)
        return []
