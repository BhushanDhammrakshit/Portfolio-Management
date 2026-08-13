"""NIFTY 500 constituent list.

Loaded once from the NSE-published CSV (``ind_nifty500list.csv``) checked
into the repo root, and converted to Yahoo-style ``SYMBOL.NS`` tickers so it
plugs straight into ``market_data`` / the swing scanner's universe.
"""
from __future__ import annotations

import csv
import logging
import os
import threading
from typing import List, Optional

log = logging.getLogger(__name__)

# repo_root/application/services/nifty500.py -> repo_root/ind_nifty500list.csv
_CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "ind_nifty500list.csv",
)

_lock = threading.Lock()
_symbols: Optional[List[str]] = None


def _load() -> List[str]:
    global _symbols
    if _symbols is not None:
        return _symbols
    with _lock:
        if _symbols is not None:
            return _symbols
        out: List[str] = []
        try:
            with open(_CSV_PATH, "r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    sym = (row.get("Symbol") or "").strip().upper()
                    series = (row.get("Series") or "").strip().upper()
                    if sym and series in ("EQ", ""):
                        out.append(f"{sym}.NS")
        except Exception as e:  # noqa: BLE001
            log.warning("nifty500: could not load %s: %s", _CSV_PATH, e)
        _symbols = out
    return _symbols


def symbols() -> List[str]:
    """All NIFTY 500 constituents as Yahoo-style ``SYMBOL.NS`` tickers."""
    return list(_load())
