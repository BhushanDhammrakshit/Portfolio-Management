"""Sector Scope – treemap heatmap backed by the market_data abstraction."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, render_template, jsonify, session, redirect, url_for, request

from application.services import market_data

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

heatmap_bp = Blueprint("heatmap", __name__)

# ── sector-wise stock universe ──────────────────────────────────────────
SECTOR_STOCKS = {
    "BANK": [
        "HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "AXISBANK.NS",
        "AUBANK.NS", "INDUSINDBK.NS", "IDFCFIRSTB.NS",
    ],
    "PSU BANK": [
        "SBIN.NS", "BANKBARODA.NS", "PNB.NS", "CANBK.NS",
        "UNIONBANK.NS", "INDIANB.NS",
    ],
    "IT": [
        "TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS",
        "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "KPITTECH.NS",
    ],
    "FMCG": [
        "HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS",
        "DABUR.NS", "COLPAL.NS", "TATACONSUM.NS",
    ],
    "PHARMA": [
        "SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS",
        "LUPIN.NS", "AUROPHARMA.NS", "BIOCON.NS", "TORNTPHARM.NS",
    ],
    "AUTO": [
        "MARUTI.NS", "M&M.NS", "TATAMOTORS.NS", "BAJAJ-AUTO.NS",
        "EICHERMOT.NS", "HEROMOTOCO.NS", "ASHOKLEY.NS", "TVSMOTOR.NS",
    ],
    "ENERGY": [
        "RELIANCE.NS", "NTPC.NS", "POWERGRID.NS", "ONGC.NS",
        "BPCL.NS", "IOC.NS", "COALINDIA.NS",
    ],
    "METAL": [
        "TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "VEDL.NS",
        "SAIL.NS", "NMDC.NS", "JINDALSTEL.NS",
    ],
    "REALTY": [
        "DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS",
        "LODHA.NS", "NBCC.NS",
    ],
    "CEMENT": [
        "ULTRACEMCO.NS", "SHREECEM.NS", "AMBUJACEM.NS",
        "DALBHARAT.NS", "ACC.NS",
    ],
    "FIN SERVICE": [
        "BAJFINANCE.NS", "BAJAJFINSV.NS", "SBILIFE.NS",
        "HDFCLIFE.NS", "CHOLAFIN.NS", "SHRIRAMFIN.NS",
    ],
    "CONSUMER": [
        "TITAN.NS", "TRENT.NS", "ASIANPAINT.NS", "PIDILITIND.NS",
    ],
    "INFRA": [
        "LT.NS", "ADANIENT.NS", "ADANIPORTS.NS", "BHARTIARTL.NS",
    ],
}

# Approximate market-cap weights (₹ '000 Cr) used when yfinance doesn't return marketCap
_DEFAULT_MCAP = {
    "RELIANCE": 18_00_000, "TCS": 14_00_000, "HDFCBANK": 13_00_000,
    "ICICIBANK": 9_00_000, "BHARTIARTL": 8_50_000, "INFY": 7_00_000,
    "ITC": 6_00_000, "HINDUNILVR": 5_80_000, "SBIN": 5_50_000,
    "LT": 5_00_000, "BAJFINANCE": 4_80_000, "KOTAKBANK": 4_00_000,
    "MARUTI": 3_80_000, "HCLTECH": 3_70_000, "SUNPHARMA": 3_60_000,
    "TITAN": 3_40_000, "NTPC": 3_30_000, "TATAMOTORS": 3_00_000,
    "ADANIENT": 2_90_000, "AXISBANK": 2_80_000, "M&M": 2_70_000,
    "ULTRACEMCO": 2_50_000, "POWERGRID": 2_40_000, "BAJAJFINSV": 2_30_000,
    "WIPRO": 2_20_000, "ONGC": 2_10_000, "JSWSTEEL": 2_00_000,
    "TATASTEEL": 1_90_000, "NESTLEIND": 1_80_000, "TECHM": 1_70_000,
    "DRREDDY": 1_10_000, "CIPLA": 1_05_000, "DIVISLAB": 1_00_000,
    "COALINDIA": 95_000, "TRENT": 90_000, "BRITANNIA": 85_000,
}

# Live data cache: short TTL so /heatmap-data can be polled every 10s.
_CACHE = {"data": None, "ts": 0}
_CACHE_TTL = 8  # seconds — slightly less than the UI poll interval (10s)

# Per-symbol metadata cache (name, sector, market-cap). These don't change
# intraday so we cache them for 30 min and only refresh prices/changes live.
_META_CACHE: dict[str, dict] = {}
_META_TTL = 30 * 60  # 30 minutes


def _get_meta(symbol):
    """Cached name + market-cap for one symbol (slow per-symbol call)."""
    now = time.time()
    cached = _META_CACHE.get(symbol)
    if cached and now - cached["ts"] < _META_TTL:
        return cached["data"]
    try:
        info = market_data.get_info(symbol) or {}
    except Exception:
        info = {}
    short = symbol.replace(".NS", "")
    mcap = info.get("market_cap") or 0
    if not mcap:
        mcap = _DEFAULT_MCAP.get(short, 50_000) * 1e7
    meta = {
        "name": (info.get("name") or short).upper(),
        "mcap": int(mcap),
    }
    _META_CACHE[symbol] = {"data": meta, "ts": now}
    return meta


def _load(force=False):
    """Build the heatmap payload.

    Hot path: 1 batched call to ``get_quotes`` for all symbols (cheap on Fyers,
    ~2 requests for 80 symbols). Slow per-symbol metadata is cached for 30 min.
    """
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["ts"] < _CACHE_TTL):
        return _CACHE["data"]

    # Flatten unique symbols
    all_symbols = []
    sym_sector = {}
    for sector, symbols in SECTOR_STOCKS.items():
        for sym in symbols:
            if sym not in sym_sector:
                all_symbols.append(sym)
                sym_sector[sym] = sector

    # Batched live quotes (one call per 50 symbols on Fyers)
    try:
        quotes = market_data.get_quotes(all_symbols) or {}
    except Exception as e:
        print(f"[heatmap] get_quotes failed: {e}")
        quotes = {}

    # Warm metadata cache in parallel (only the symbols whose meta has expired)
    missing_meta = [s for s in all_symbols
                    if not _META_CACHE.get(s)
                    or (now - _META_CACHE[s]["ts"]) >= _META_TTL]
    if missing_meta:
        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(_get_meta, missing_meta))

    sectors = {}
    for sym in all_symbols:
        q = quotes.get(sym) or {}
        meta = _get_meta(sym)
        price = q.get("price")
        change = q.get("change_pct")
        if not price:
            continue
        short = sym.replace(".NS", "")
        result = {
            "name": meta["name"],
            "symbol": short,
            "price": round(float(price), 2),
            "change": round(float(change or 0), 2),
            "mcap": meta["mcap"],
        }
        sec = sym_sector[sym]
        if sec not in sectors:
            sectors[sec] = {"name": sec, "stocks": [], "totalMcap": 0}
        sectors[sec]["stocks"].append(result)
        sectors[sec]["totalMcap"] += result["mcap"]

    data = {"sectors": list(sectors.values()), "ts": int(now)}
    _CACHE["data"] = data
    _CACHE["ts"] = now
    return data


@heatmap_bp.route("/heatmap-data")
def heatmap_data():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    force = request.args.get("force_refresh") == "1"
    return jsonify(_load(force=force))


@heatmap_bp.route("/heatmap")
def heatmap():
    if "email" not in session:
        return redirect(url_for("logIn"))
    return render_template("heatmap.html",
                           name=session.get("name", "User"),
                           email=session.get("email", ""),
                           title="Sector Heatmap")
