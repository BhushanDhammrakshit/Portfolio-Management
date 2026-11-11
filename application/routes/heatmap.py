"""Sector Scope – treemap heatmap backed by the market_data abstraction."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, render_template, jsonify, session, redirect, url_for, request

from application import config
from application.services import market_data
from application.services import cache as shared_cache
from application.services import snapshot_store

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

heatmap_bp = Blueprint("heatmap", __name__)

# ── sector-wise stock universe ──────────────────────────────────────────
SECTOR_STOCKS = {
    "BANK": [
        "HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "AXISBANK.NS",
        "AUBANK.NS", "INDUSINDBK.NS", "IDFCFIRSTB.NS", "FEDERALBNK.NS",
        "YESBANK.NS", "BANDHANBNK.NS",
    ],
    "PSU BANK": [
        "SBIN.NS", "BANKBARODA.NS", "PNB.NS", "CANBK.NS",
        "UNIONBANK.NS", "INDIANB.NS", "MAHABANK.NS", "CENTRALBK.NS",
        "UCOBANK.NS", "BANKINDIA.NS",
    ],
    "IT": [
        "TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS",
        "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "KPITTECH.NS",
        "LTIM.NS",
    ],
    "FMCG": [
        "HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS",
        "DABUR.NS", "COLPAL.NS", "TATACONSUM.NS", "MARICO.NS",
        "GODREJCP.NS", "EMAMILTD.NS",
    ],
    "PHARMA": [
        "SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS",
        "LUPIN.NS", "AUROPHARMA.NS", "BIOCON.NS", "TORNTPHARM.NS",
        "ZYDUSLIFE.NS", "MANKIND.NS",
    ],
    "AUTO": [
        "MARUTI.NS", "M&M.NS", "TATAMOTORS.NS", "BAJAJ-AUTO.NS",
        "EICHERMOT.NS", "HEROMOTOCO.NS", "ASHOKLEY.NS", "TVSMOTOR.NS",
        "BOSCHLTD.NS", "MOTHERSON.NS",
    ],
    "ENERGY": [
        "RELIANCE.NS", "NTPC.NS", "POWERGRID.NS", "ONGC.NS",
        "BPCL.NS", "IOC.NS", "COALINDIA.NS", "TATAPOWER.NS",
        "GAIL.NS", "ADANIGREEN.NS",
    ],
    "METAL": [
        "TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "VEDL.NS",
        "SAIL.NS", "NMDC.NS", "JINDALSTEL.NS", "HINDZINC.NS",
        "NATIONALUM.NS", "HINDCOPPER.NS",
    ],
    "REALTY": [
        "DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS",
        "LODHA.NS", "NBCC.NS", "BRIGADE.NS", "SOBHA.NS",
        "SIGNATURE.NS", "MAHLIFE.NS",
    ],
    "CEMENT": [
        "ULTRACEMCO.NS", "SHREECEM.NS", "AMBUJACEM.NS",
        "DALBHARAT.NS", "ACC.NS", "GRASIM.NS", "JKLAKSHMI.NS",
        "RAMCOCEM.NS", "BIRLACORPN.NS", "HEIDELBERG.NS",
    ],
    "FIN SERVICE": [
        "BAJFINANCE.NS", "BAJAJFINSV.NS", "SBILIFE.NS",
        "HDFCLIFE.NS", "CHOLAFIN.NS", "SHRIRAMFIN.NS",
        "ICICIPRULI.NS", "MAXFIN.NS", "HDFCAMC.NS", "PFC.NS",
    ],
    "CONSUMER": [
        "TITAN.NS", "TRENT.NS", "ASIANPAINT.NS", "PIDILITIND.NS",
        "HAVELLS.NS", "DIXON.NS", "VOLTAS.NS", "CROMPTON.NS",
        "KALYANKJIL.NS", "DMART.NS",
    ],
    "INFRA": [
        "LT.NS", "ADANIENT.NS", "ADANIPORTS.NS", "BHARTIARTL.NS",
        "IRB.NS", "GMRINFRA.NS", "CONCOR.NS", "RVNL.NS",
        "IRFC.NS", "BHEL.NS",
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

# Cache keys (the shared cache adds the global CACHE_KEY_PREFIX).
_HEATMAP_KEY = "heatmap:sector-universe:v2"
_META_KEY = "heatmap:meta:{symbol}"


def _get_meta(symbol):
    """Cached name + market-cap for one symbol (slow per-symbol call).

    Stored in shared cache so all workers share the same metadata and
    we only call ``get_info`` once per ``META_CACHE_TTL`` per symbol.
    """
    key = _META_KEY.format(symbol=symbol)
    cached = shared_cache.jget(key)
    if cached:
        return cached
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
    shared_cache.jset(key, meta, ttl=config.META_CACHE_TTL)
    return meta


def _build_heatmap_payload():
    """Pure builder: hits providers and assembles the heatmap payload.

    Called by the precompute scheduler (writes to Redis) and by the
    cold-path of the route handler (single-flighted via a Redis lock).
    """
    now = time.time()

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

    # Warm metadata cache in parallel for any symbols missing it.
    missing_meta = [s for s in all_symbols if shared_cache.jget(_META_KEY.format(symbol=s)) is None]
    if missing_meta:
        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(_get_meta, missing_meta))

    sectors = {
        sector: {"name": sector, "stocks": [], "unavailable": [], "totalMcap": 0}
        for sector in SECTOR_STOCKS
    }
    for sym in all_symbols:
        q = quotes.get(sym) or {}
        meta = _get_meta(sym)
        price = q.get("price")
        change = q.get("change_pct")
        sec = sym_sector[sym]
        short = sym.replace(".NS", "")
        if not price:
            sectors[sec]["unavailable"].append({
                "name": meta["name"],
                "symbol": short,
                "price": None,
                "change": None,
                "mcap": meta["mcap"],
            })
            continue
        result = {
            "name": meta["name"],
            "symbol": short,
            "price": round(float(price), 2),
            "change": round(float(change or 0), 2),
            "mcap": meta["mcap"],
        }
        sectors[sec]["stocks"].append(result)
        sectors[sec]["totalMcap"] += result["mcap"]

    return {
        "sectors": [sector for sector in sectors.values() if sector["stocks"]],
        "ts": int(now),
    }


def _load(force=False):
    """Return the heatmap payload, preferring the precomputed copy in Redis.

    Hot path (99% of requests): one Redis GET — no provider calls.
    Cold path (cache empty): single-flight the build via a distributed
    lock so a thundering herd doesn't fan out to upstream.

    Outside market hours (incl. the pre-open dead zone before 9:15 IST),
    the broker feed reports ``net_change: 0`` for most symbols since it
    hasn't started publishing today's session data yet. Rebuilding then
    would show an all-flat heatmap, so a rebuild falls back to the last
    snapshot captured while the market was actually live (snapshot_store).
    """
    if not force:
        cached = shared_cache.jget(_HEATMAP_KEY)
        if cached:
            return cached

    with shared_cache.lock("heatmap:build", ttl=20, blocking=True, wait=5.0) as got:
        # Re-check after acquiring (another worker may have just filled it).
        cached = shared_cache.jget(_HEATMAP_KEY)
        if cached and not force:
            return cached
        if not got:
            # Couldn't get the lock and still no data — degrade rather than hang.
            return cached or {"sectors": [], "ts": int(time.time())}
        data = snapshot_store.serve_or_refresh(
            _HEATMAP_KEY, _build_heatmap_payload, live=True, force=force)
        shared_cache.jset(_HEATMAP_KEY, data, ttl=max(config.HEATMAP_CACHE_TTL * 4, 60))
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
