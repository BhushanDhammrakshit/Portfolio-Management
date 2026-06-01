"""yfinance market-data provider — wraps yfinance behind the common interface.

Used as the default provider when Dhan credentials aren't configured, and
as the metadata fallback (sector / market cap / search) for Dhan, which
doesn't expose those fields.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Iterable, Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# yfinance period strings keyed by approximate days.
def _period_for_days(days: int) -> str:
    if days <= 5:
        return "5d"
    if days <= 30:
        return "1mo"
    if days <= 90:
        return "3mo"
    if days <= 180:
        return "6mo"
    if days <= 365:
        return "1y"
    if days <= 730:
        return "2y"
    return "5y"


def _interval_for(interval: str) -> str:
    # yfinance accepts "1m","5m","15m","30m","60m","1h","1d","1wk","1mo"
    return interval


def get_history(symbol: str, days: int = 30, interval: str = "1d") -> pd.DataFrame:
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol)
        period = _period_for_days(days)
        df = ticker.history(period=period, interval=_interval_for(interval))
        if df is None:
            return pd.DataFrame()
        return df
    except Exception as e:
        log.warning("yfinance.get_history(%s): %s", symbol, e)
        return pd.DataFrame()


def download_history(symbols: Iterable[str], start, end,
                     interval: str = "1d") -> dict[str, pd.DataFrame]:
    import yfinance as yf
    syms = list(symbols)
    if not syms:
        return {}
    if isinstance(start, _dt.date):
        start = start.isoformat()
    if isinstance(end, _dt.date):
        end = end.isoformat()
    try:
        data = yf.download(syms, start=start, end=end, interval=interval,
                           progress=False, auto_adjust=True, group_by="ticker",
                           threads=True)
    except Exception as e:
        log.warning("yfinance.download_history: %s", e)
        return {s: pd.DataFrame() for s in syms}

    out: dict[str, pd.DataFrame] = {}
    if data is None or data.empty:
        return {s: pd.DataFrame() for s in syms}

    if len(syms) == 1:
        out[syms[0]] = data
        return out

    # group_by="ticker" gives a MultiIndex (symbol, field) on columns.
    if hasattr(data.columns, "levels"):
        for sym in syms:
            try:
                out[sym] = data[sym].dropna(how="all")
            except KeyError:
                out[sym] = pd.DataFrame()
    else:
        out[syms[0]] = data
    return out


def _live_price(ticker) -> Optional[float]:
    fast = getattr(ticker, "fast_info", None)
    if fast:
        for k in ("last_price", "lastPrice", "regular_market_price"):
            try:
                v = fast.get(k) if hasattr(fast, "get") else getattr(fast, k, None)
            except Exception:
                v = None
            if v:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    try:
        hist = ticker.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


def get_quote(symbol: str) -> Optional[dict]:
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol)
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            info = {}

        price = info.get("regularMarketPrice") or _live_price(ticker)
        prev_close = (info.get("regularMarketPreviousClose")
                      or info.get("previousClose"))
        if price is None and prev_close is None:
            return None
        if price is None:
            price = prev_close
        if prev_close is None:
            prev_close = price
        change = float(price) - float(prev_close) if prev_close else 0.0
        change_pct = (change / float(prev_close) * 100.0) if prev_close else 0.0

        return {
            "symbol": symbol,
            "name": info.get("shortName") or info.get("longName") or symbol,
            "price": float(price),
            "prev_close": float(prev_close),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "day_open": float(info.get("regularMarketOpen") or 0),
            "day_high": float(info.get("regularMarketDayHigh") or 0),
            "day_low": float(info.get("regularMarketDayLow") or 0),
            "volume": int(info.get("regularMarketVolume") or 0),
            "market_cap": info.get("marketCap"),
            "sector": info.get("sector") or "",
        }
    except Exception as e:
        log.warning("yfinance.get_quote(%s): %s", symbol, e)
        return None


def get_quotes(symbols: Iterable[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sym in symbols:
        q = get_quote(sym)
        if q:
            out[sym] = q
    return out


def get_info(symbol: str) -> Optional[dict]:
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol)
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            info = {}
        return {
            "symbol": symbol,
            "name": info.get("shortName") or info.get("longName") or symbol,
            "exchange": info.get("exchange") or "",
            "sector": info.get("sector") or "",
            "industry": info.get("industry") or "",
            "currency": info.get("currency") or "",
            "market_cap": info.get("marketCap"),
        }
    except Exception as e:
        log.warning("yfinance.get_info(%s): %s", symbol, e)
        return None


_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
}


def search(query: str) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    url = "https://query2.finance.yahoo.com/v1/finance/search"
    try:
        r = requests.get(
            url,
            params={"q": q, "quotesCount": 10, "newsCount": 0,
                    "lang": "en-IN", "region": "IN"},
            headers=_SEARCH_HEADERS, timeout=6,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except (requests.RequestException, ValueError):
        return []

    results = []
    for q_ in data.get("quotes", []) or []:
        sym = q_.get("symbol")
        if not sym:
            continue
        results.append({
            "symbol": sym,
            "name": q_.get("shortname") or q_.get("longname") or sym,
            "exchange": q_.get("exchDisp") or q_.get("exchange") or "",
            "type": q_.get("typeDisp") or q_.get("quoteType") or "",
        })
    results.sort(key=lambda r_: 0 if r_["symbol"].endswith((".NS", ".BO")) else 1)
    return results[:10]
