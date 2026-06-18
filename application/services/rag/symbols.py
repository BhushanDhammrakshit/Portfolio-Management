"""Symbol normalization + alias resolution.

Maps user-friendly names like 'Reliance' or 'TCS' to canonical NSE symbols
('RELIANCE.NS', 'TCS.NS') used as Azure Table PartitionKey.
"""
from __future__ import annotations

import re
from typing import Optional

# Hand-curated alias map for common Indian tickers.
# Extend over time; missing aliases fall back to the resolver below.
_ALIASES = {
    "reliance": "RELIANCE.NS",
    "ril": "RELIANCE.NS",
    "tcs": "TCS.NS",
    "infosys": "INFY.NS",
    "infy": "INFY.NS",
    "wipro": "WIPRO.NS",
    "hdfc bank": "HDFCBANK.NS",
    "hdfc": "HDFCBANK.NS",
    "icici": "ICICIBANK.NS",
    "icici bank": "ICICIBANK.NS",
    "sbi": "SBIN.NS",
    "state bank": "SBIN.NS",
    "kotak": "KOTAKBANK.NS",
    "axis": "AXISBANK.NS",
    "axis bank": "AXISBANK.NS",
    "itc": "ITC.NS",
    "hul": "HINDUNILVR.NS",
    "hindustan unilever": "HINDUNILVR.NS",
    "bharti": "BHARTIARTL.NS",
    "airtel": "BHARTIARTL.NS",
    "lt": "LT.NS",
    "l&t": "LT.NS",
    "larsen": "LT.NS",
    "tata motors": "TATAMOTORS.NS",
    "tata steel": "TATASTEEL.NS",
    "tata power": "TATAPOWER.NS",
    "tcs": "TCS.NS",
    "maruti": "MARUTI.NS",
    "m&m": "M&M.NS",
    "mahindra": "M&M.NS",
    "ongc": "ONGC.NS",
    "ntpc": "NTPC.NS",
    "coal india": "COALINDIA.NS",
    "powergrid": "POWERGRID.NS",
    "adani enterprises": "ADANIENT.NS",
    "adani green": "ADANIGREEN.NS",
    "adani ports": "ADANIPORTS.NS",
    "asian paints": "ASIANPAINT.NS",
    "nestle": "NESTLEIND.NS",
    "britannia": "BRITANNIA.NS",
    "sun pharma": "SUNPHARMA.NS",
    "dr reddy": "DRREDDY.NS",
    "cipla": "CIPLA.NS",
    "bajaj finance": "BAJFINANCE.NS",
    "bajaj finserv": "BAJAJFINSV.NS",
    "bajaj auto": "BAJAJ-AUTO.NS",
    "hero": "HEROMOTOCO.NS",
    "hero motocorp": "HEROMOTOCO.NS",
    "eicher": "EICHERMOT.NS",
    "jsw steel": "JSWSTEEL.NS",
    "ultratech": "ULTRACEMCO.NS",
    "grasim": "GRASIM.NS",
    "ambuja": "AMBUJACEM.NS",
    "shriram finance": "SHRIRAMFIN.NS",
    "tech mahindra": "TECHM.NS",
    "hcl tech": "HCLTECH.NS",
    "hcl": "HCLTECH.NS",
    "lti mindtree": "LTIM.NS",
    "ltimindtree": "LTIM.NS",
    "indusind": "INDUSINDBK.NS",
    "divis": "DIVISLAB.NS",
    "apollo hospitals": "APOLLOHOSP.NS",
    "titan": "TITAN.NS",
    "trent": "TRENT.NS",
    "zomato": "ZOMATO.NS",
    "paytm": "PAYTM.NS",
    "nykaa": "NYKAA.NS",
    "policybazaar": "POLICYBZR.NS",
    "irctc": "IRCTC.NS",
    "lic": "LICI.NS",
}


def canonicalize(symbol: str) -> str:
    """Return the canonical PartitionKey-safe form of a symbol.

    Examples:
        'reliance'    -> 'RELIANCE.NS'
        'TCS'         -> 'TCS.NS'
        'TCS.NS'      -> 'TCS.NS'
        'NSE:INFY-EQ' -> 'INFY.NS'
    """
    if not symbol:
        return ""
    s = symbol.strip()

    # Alias hit (case-insensitive)
    low = s.lower()
    if low in _ALIASES:
        return _ALIASES[low]

    # Already canonical
    if s.endswith(".NS") or s.endswith(".BO"):
        return s.upper()

    # Fyers-style "NSE:RELIANCE-EQ"
    m = re.match(r"^(?:NSE|BSE):([A-Z0-9&\-]+?)(?:-EQ)?$", s, re.I)
    if m:
        ex = ".NS" if s.upper().startswith("NSE") else ".BO"
        return m.group(1).upper() + ex

    # Bare ticker assumed NSE
    if re.match(r"^[A-Z0-9&\-]+$", s, re.I):
        return s.upper() + ".NS"

    return s


def display_name(symbol: str) -> str:
    """Strip exchange suffix for display: 'RELIANCE.NS' -> 'RELIANCE'."""
    s = canonicalize(symbol)
    return s.split(".")[0] if s else ""


def safe_partition_key(symbol: str) -> str:
    """Azure Table PartitionKey-safe form. Replaces forbidden chars."""
    s = canonicalize(symbol) or "_unknown"
    # Forbidden: / \ # ?  + control chars. We only need to handle '/' and '\'.
    return re.sub(r"[\/\\#\?]", "_", s)


def search_terms(symbol: str) -> list:
    """Generate query terms for matching news articles to a symbol.

    Returns the canonical name plus any aliases pointing at it.
    """
    canon = canonicalize(symbol)
    base = display_name(canon)
    terms = {base, base.lower()}
    for alias, target in _ALIASES.items():
        if target == canon:
            terms.add(alias)
    # Drop very short ambiguous tokens
    return sorted(t for t in terms if len(t) >= 3)
