"""Dhan instrument-master loader.

Dhan's REST API is keyed by numeric ``security_id`` + ``exchange_segment``
(e.g. ``NSE_EQ``). The application uses Yahoo-style tickers like
``RELIANCE.NS`` / ``^NSEI`` everywhere. This module bridges the two.

The instrument master (~24 MB CSV) is downloaded once from Dhan's CDN and
cached on disk; only NSE-EQ, BSE-EQ and the common indices are loaded
into memory.
"""
from __future__ import annotations

import csv
import os
import time
import threading
from typing import Optional

import requests

# ── Public types ─────────────────────────────────────────────────────────
# (segment, security_id, display_name)


_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)))
_CACHE_FILE = os.path.join(_CACHE_DIR, "_dhan_scrip_master.csv")
_CACHE_TTL_SECONDS = 24 * 60 * 60  # refresh master once a day

# Hard-coded common indices (Dhan ships these in the master too, but
# resolving by name is brittle — easier to pin the well-known ones).
_INDEX_MAP = {
    "^NSEI":     ("IDX_I", "13", "NIFTY 50"),
    "NIFTY":     ("IDX_I", "13", "NIFTY 50"),
    "NIFTY50":   ("IDX_I", "13", "NIFTY 50"),
    "^NSEBANK":  ("IDX_I", "25", "NIFTY BANK"),
    "BANKNIFTY": ("IDX_I", "25", "NIFTY BANK"),
    "FINNIFTY":  ("IDX_I", "27", "NIFTY FIN SERVICE"),
    "^BSESN":    ("IDX_I", "51", "SENSEX"),
    "SENSEX":    ("IDX_I", "51", "SENSEX"),
}

_lock = threading.Lock()
_loaded = False
_nse_eq: dict[str, tuple[str, str, str]] = {}  # symbol -> (segment, sec_id, name)
_bse_eq: dict[str, tuple[str, str, str]] = {}
# Reverse lookup: (segment, sec_id) -> symbol (for batch quote responses)
_by_id: dict[tuple[str, str], str] = {}


class SymbolNotFoundError(LookupError):
    """Raised when a symbol cannot be mapped to a Dhan security_id."""


def _download_master() -> None:
    """Download the master CSV to disk if missing / stale."""
    try:
        if os.path.exists(_CACHE_FILE):
            age = time.time() - os.path.getmtime(_CACHE_FILE)
            if age < _CACHE_TTL_SECONDS:
                return
        r = requests.get(_MASTER_URL, timeout=60, stream=True)
        r.raise_for_status()
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)
        os.replace(tmp, _CACHE_FILE)
    except Exception as e:
        # If we already have a stale copy, fall back to it instead of failing.
        if not os.path.exists(_CACHE_FILE):
            raise RuntimeError(f"Could not download Dhan scrip master: {e}") from e


def _load_master() -> None:
    """Parse the CSV into NSE/BSE EQ dicts. Idempotent + thread-safe."""
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _download_master()

        # Index lookups by id
        for sym, (seg, sid, name) in _INDEX_MAP.items():
            _by_id[(seg, sid)] = sym

        with open(_CACHE_FILE, "r", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                instrument = (row.get("SEM_INSTRUMENT_NAME") or "").strip().upper()
                if instrument != "EQUITY":
                    continue
                exch = (row.get("SEM_EXM_EXCH_ID") or "").strip().upper()
                series = (row.get("SEM_SERIES") or "").strip().upper()
                # NSE: only EQ series (regular cash). BSE: A/B/T groups all OK.
                if exch == "NSE" and series and series != "EQ":
                    continue
                trading_sym = (row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
                sec_id = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
                name = (row.get("SM_SYMBOL_NAME") or row.get("SEM_CUSTOM_SYMBOL")
                        or trading_sym).strip()
                if not trading_sym or not sec_id:
                    continue
                if exch == "NSE":
                    _nse_eq[trading_sym] = ("NSE_EQ", sec_id, name)
                    _by_id[("NSE_EQ", sec_id)] = trading_sym + ".NS"
                elif exch == "BSE":
                    _bse_eq[trading_sym] = ("BSE_EQ", sec_id, name)
                    _by_id[("BSE_EQ", sec_id)] = trading_sym + ".BO"

        _loaded = True


def _normalize(symbol: str) -> tuple[str, Optional[str]]:
    """Strip exchange suffix; return (clean_symbol, preferred_segment_or_None)."""
    s = (symbol or "").strip().upper()
    if not s:
        return s, None
    if s.endswith(".NS"):
        return s[:-3], "NSE_EQ"
    if s.endswith(".BO"):
        return s[:-3], "BSE_EQ"
    return s, None


def resolve(symbol: str) -> tuple[str, str, str]:
    """Resolve a Yahoo-style symbol to ``(segment, security_id, display_name)``.

    Raises :class:`SymbolNotFoundError` if not found.
    """
    if not symbol:
        raise SymbolNotFoundError("empty symbol")

    clean, preferred = _normalize(symbol)

    # Check index map first (cheap, no master load needed).
    if clean in _INDEX_MAP:
        return _INDEX_MAP[clean]
    if symbol.strip().upper() in _INDEX_MAP:
        return _INDEX_MAP[symbol.strip().upper()]

    _load_master()

    if preferred == "BSE_EQ":
        hit = _bse_eq.get(clean) or _nse_eq.get(clean)
    else:
        hit = _nse_eq.get(clean) or _bse_eq.get(clean)
    if hit:
        return hit

    raise SymbolNotFoundError(f"Symbol not found in Dhan master: {symbol}")


def reverse_lookup(segment: str, security_id: str) -> Optional[str]:
    """Return the original Yahoo-style symbol for a (segment, sec_id) pair."""
    if not _loaded:
        _load_master()
    return _by_id.get((segment, str(security_id)))
