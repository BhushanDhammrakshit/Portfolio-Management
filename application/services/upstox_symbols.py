"""Upstox instrument-master loader.

Upstox's REST API is keyed by ``instrument_key`` strings like
``NSE_EQ|INE002A01018`` (ISIN-based) for equities and
``NSE_INDEX|Nifty 50`` for indices. The application uses Yahoo-style
tickers (``RELIANCE.NS`` / ``^NSEI``) everywhere, so this module bridges
the two.

The NSE instrument master (~few MB, gzipped JSON) is downloaded once from
Upstox's CDN and cached on disk; only NSE equities + the common indices
are loaded into memory.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)))
_CACHE_FILE = os.path.join(_CACHE_DIR, "_upstox_nse_instruments.json")
_CACHE_TTL_SECONDS = 24 * 60 * 60  # refresh the master once a day

# Well-known indices — pinned because resolving them by name from the
# master is brittle. Keys are Yahoo-style aliases used across the app.
_INDEX_MAP = {
    "^NSEI":      ("NSE_INDEX|Nifty 50", "NIFTY 50"),
    "NIFTY":      ("NSE_INDEX|Nifty 50", "NIFTY 50"),
    "NIFTY50":    ("NSE_INDEX|Nifty 50", "NIFTY 50"),
    "^NSEBANK":   ("NSE_INDEX|Nifty Bank", "NIFTY BANK"),
    "BANKNIFTY":  ("NSE_INDEX|Nifty Bank", "NIFTY BANK"),
    "FINNIFTY":   ("NSE_INDEX|Nifty Fin Service", "NIFTY FIN SERVICE"),
    "^CNXIT":     ("NSE_INDEX|Nifty IT", "NIFTY IT"),
    "^CNXAUTO":   ("NSE_INDEX|Nifty Auto", "NIFTY AUTO"),
    "^CNXPHARMA": ("NSE_INDEX|Nifty Pharma", "NIFTY PHARMA"),
    "^CNXFMCG":   ("NSE_INDEX|Nifty FMCG", "NIFTY FMCG"),
    "^CNXMETAL":  ("NSE_INDEX|Nifty Metal", "NIFTY METAL"),
    "^CNXREALTY": ("NSE_INDEX|Nifty Realty", "NIFTY REALTY"),
    "^CNXENERGY": ("NSE_INDEX|Nifty Energy", "NIFTY ENERGY"),
    "INDIAVIX":   ("NSE_INDEX|India VIX", "INDIA VIX"),
    "^INDIAVIX":  ("NSE_INDEX|India VIX", "INDIA VIX"),
}

_lock = threading.Lock()
_loaded = False
_nse_eq: dict[str, tuple[str, str]] = {}     # symbol -> (instrument_key, name)
_by_key: dict[str, str] = {}                  # instrument_key -> yahoo symbol
_by_quote_key: dict[str, str] = {}            # "NSE_EQ:SYMBOL" -> yahoo symbol


class SymbolNotFoundError(LookupError):
    """Raised when a symbol cannot be mapped to an Upstox instrument key."""


def _download_master() -> Optional[list]:
    """Download + parse the gzipped NSE master. Returns the JSON list, or
    None if the network failed and no cached copy exists.
    """
    try:
        if os.path.exists(_CACHE_FILE):
            age = time.time() - os.path.getmtime(_CACHE_FILE)
            if age < _CACHE_TTL_SECONDS:
                with open(_CACHE_FILE, "r", encoding="utf-8") as fh:
                    return json.load(fh)
        r = requests.get(_MASTER_URL, timeout=60)
        r.raise_for_status()
        data = json.loads(gzip.decompress(r.content).decode("utf-8"))
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _CACHE_FILE)
        return data
    except Exception as e:  # noqa: BLE001
        if os.path.exists(_CACHE_FILE):
            try:
                with open(_CACHE_FILE, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:  # noqa: BLE001
                pass
        log.warning("upstox_symbols: could not download instrument master: %s", e)
        return None


def _load_master() -> None:
    """Parse the master into the NSE-EQ lookup dicts. Idempotent + safe."""
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return

        # Index lookups first (always available even if the download fails).
        for sym, (key, _name) in _INDEX_MAP.items():
            _by_key[key] = _index_alias(sym)

        data = _download_master()
        if data:
            for row in data:
                try:
                    seg = (row.get("segment") or "").upper()
                    itype = (row.get("instrument_type") or "").upper()
                    if seg != "NSE_EQ" or itype != "EQ":
                        continue
                    tsym = (row.get("trading_symbol") or "").strip().upper()
                    key = (row.get("instrument_key") or "").strip()
                    name = (row.get("name") or tsym).strip()
                    if not tsym or not key:
                        continue
                    _nse_eq[tsym] = (key, name)
                    _by_key[key] = tsym + ".NS"
                    _by_quote_key[f"NSE_EQ:{tsym}"] = tsym + ".NS"
                except Exception:  # noqa: BLE001
                    continue
        _loaded = True


def _index_alias(sym: str) -> str:
    """Canonical Yahoo-style alias used when mapping an index key back."""
    canonical = {
        "NIFTY": "^NSEI", "NIFTY50": "^NSEI",
        "BANKNIFTY": "^NSEBANK", "^INDIAVIX": "INDIAVIX",
    }
    return canonical.get(sym, sym)


# ── Public API ──────────────────────────────────────────────────────────

def resolve(symbol: str) -> tuple[str, str]:
    """Yahoo-style symbol → (instrument_key, display_name).

    Raises SymbolNotFoundError if the symbol cannot be mapped.
    """
    if not symbol:
        raise SymbolNotFoundError("empty symbol")
    s = symbol.strip().upper()

    if s in _INDEX_MAP:
        key, name = _INDEX_MAP[s]
        return key, name

    _load_master()

    if s in _INDEX_MAP:
        key, name = _INDEX_MAP[s]
        return key, name

    base = s
    if base.endswith(".NS"):
        base = base[:-3]
    elif base.endswith(".BO"):
        # BSE not loaded; let the caller fall back.
        raise SymbolNotFoundError(f"BSE symbol not supported by Upstox loader: {symbol}")

    hit = _nse_eq.get(base)
    if hit:
        return hit[0], hit[1]
    raise SymbolNotFoundError(f"Upstox instrument key not found for {symbol}")


def from_key(instrument_key: str) -> Optional[str]:
    """Upstox instrument_key → Yahoo-style symbol (best effort)."""
    if not instrument_key:
        return None
    if not _loaded:
        _load_master()
    return _by_key.get(instrument_key)


def from_quote_key(quote_key: str) -> Optional[str]:
    """Map a full-quote response key (``NSE_EQ:RELIANCE`` /
    ``NSE_INDEX:Nifty 50``) back to a Yahoo-style symbol.
    """
    if not quote_key:
        return None
    if not _loaded:
        _load_master()
    hit = _by_quote_key.get(quote_key)
    if hit:
        return hit
    # Index responses come back as "NSE_INDEX:Nifty 50".
    if quote_key.startswith("NSE_INDEX:"):
        name = quote_key.split(":", 1)[1].strip().lower()
        for alias, (key, _n) in _INDEX_MAP.items():
            if key.split("|", 1)[1].lower() == name:
                return _index_alias(alias)
    # Fallback: "NSE_EQ:SYMBOL" → SYMBOL.NS
    if quote_key.startswith("NSE_EQ:"):
        return quote_key.split(":", 1)[1].strip().upper() + ".NS"
    return None
