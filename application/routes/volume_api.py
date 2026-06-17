"""Volume Shockers – compare today's volume to 10-day average."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, render_template, jsonify, session, redirect, url_for

from application.services import market_data

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

volume_api = Blueprint("volume_api", __name__)

# ── Stock universe (unique symbols across indices) ───────────────────────
_NIFTY50 = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HCLTECH.NS", "LT.NS", "ITC.NS",
    "BHARTIARTL.NS", "ASIANPAINT.NS", "BAJFINANCE.NS", "HINDUNILVR.NS",
    "MARUTI.NS", "TITAN.NS", "ULTRACEMCO.NS", "SUNPHARMA.NS", "WIPRO.NS",
    "BAJAJ-AUTO.NS", "BAJAJFINSV.NS", "CIPLA.NS", "COALINDIA.NS", "DRREDDY.NS",
    "EICHERMOT.NS", "GRASIM.NS", "HEROMOTOCO.NS", "HINDALCO.NS", "JSWSTEEL.NS",
    "M&M.NS", "NESTLEIND.NS", "NTPC.NS", "ONGC.NS", "POWERGRID.NS",
    "SBILIFE.NS", "SHRIRAMFIN.NS", "TATACONSUM.NS", "TATAMOTORS.NS",
    "TATASTEEL.NS", "TECHM.NS", "TRENT.NS", "ADANIENT.NS", "ADANIPORTS.NS",
    "APOLLOHOSP.NS", "BPCL.NS", "BRITANNIA.NS", "DIVISLAB.NS", "HDFCLIFE.NS",
    "INDUSINDBK.NS",
]

_BANKNIFTY = [
    "HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "SBIN.NS", "AXISBANK.NS",
    "BANKBARODA.NS", "PNB.NS", "IDFCFIRSTB.NS", "FEDERALBNK.NS", "AUBANK.NS",
    "BANDHANBNK.NS", "CANBK.NS",
]

_FINNIFTY = [
    "BAJFINANCE.NS", "BAJAJFINSV.NS", "HDFCBANK.NS", "ICICIBANK.NS",
    "KOTAKBANK.NS", "SBIN.NS", "AXISBANK.NS", "SBILIFE.NS", "HDFCLIFE.NS",
    "CHOLAFIN.NS", "SHRIRAMFIN.NS", "MUTHOOTFIN.NS", "PFC.NS", "RECLTD.NS",
]

_SENSEX = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "HINDUNILVR.NS", "ITC.NS", "BHARTIARTL.NS", "SBIN.NS", "KOTAKBANK.NS",
    "LT.NS", "BAJFINANCE.NS", "HCLTECH.NS", "MARUTI.NS", "TITAN.NS",
    "SUNPHARMA.NS", "TATAMOTORS.NS", "AXISBANK.NS", "NTPC.NS", "WIPRO.NS",
    "ULTRACEMCO.NS", "ASIANPAINT.NS", "NESTLEIND.NS", "BAJAJ-AUTO.NS",
    "POWERGRID.NS", "JSWSTEEL.NS", "M&M.NS", "TATASTEEL.NS", "TECHM.NS",
    "INDUSINDBK.NS",
]

_NIFTY200_EXTRA = [
    "ADANIGREEN.NS", "ADANIPOWER.NS", "AMBUJACEM.NS", "ACC.NS",
    "AUROPHARMA.NS", "BIOCON.NS", "BOSCHLTD.NS",
    "COLPAL.NS", "DABUR.NS", "DLF.NS", "GODREJCP.NS", "GODREJPROP.NS",
    "HAVELLS.NS", "IPCALAB.NS", "JINDALSTEL.NS", "LUPIN.NS",
    "MOTHERSON.NS", "MPHASIS.NS", "NAUKRI.NS", "NMDC.NS",
    "OBEROIRLTY.NS", "PAGEIND.NS", "PERSISTENT.NS", "PIDILITIND.NS",
    "PIIND.NS", "SAIL.NS", "SIEMENS.NS", "SRF.NS", "TORNTPHARM.NS",
    "TVSMOTOR.NS", "UPL.NS", "VEDL.NS", "VOLTAS.NS", "ZOMATO.NS",
    "COFORGE.NS", "DMART.NS", "HAL.NS", "IRCTC.NS", "LODHA.NS",
    "MAXHEALTH.NS", "POLICYBZR.NS", "PRESTIGE.NS", "TATAPOWER.NS",
    "UNIONBANK.NS", "YESBANK.NS",
]

# Remaining NSE F&O-enabled stocks (not already covered by NIFTY 50 /
# BANK NIFTY / FINNIFTY / SENSEX / NIFTY 200 extras above). Together this
# brings the scan universe to ~200 names — i.e. the full set of stocks on
# which options trading is permitted by NSE. Reviewed against the NSE F&O
# eligibility list; refresh quarterly as NSE adds/removes underlyings.
_FNO_ONLY = [
    "ABB.NS", "ABBOTINDIA.NS", "ABCAPITAL.NS", "ABFRL.NS", "ALKEM.NS",
    "APLAPOLLO.NS", "ASTRAL.NS", "ATUL.NS", "AARTIIND.NS", "BALKRISIND.NS",
    "BALRAMCHIN.NS", "BATAINDIA.NS", "BEL.NS", "BERGEPAINT.NS",
    "BHARATFORG.NS", "BHEL.NS", "BSOFT.NS", "CANFINHOME.NS", "CGPOWER.NS",
    "CHAMBLFERT.NS", "CONCOR.NS", "COROMANDEL.NS", "CROMPTON.NS", "CUB.NS",
    "CUMMINSIND.NS", "DALBHARAT.NS", "DEEPAKNTR.NS", "DELHIVERY.NS",
    "DIXON.NS", "ESCORTS.NS", "EXIDEIND.NS", "GAIL.NS", "GLENMARK.NS",
    "GMRAIRPORT.NS", "GNFC.NS", "GRANULES.NS", "GUJGASLTD.NS",
    "HDFCAMC.NS", "HINDCOPPER.NS", "HINDPETRO.NS", "ICICIGI.NS",
    "ICICIPRULI.NS", "IDEA.NS", "IEX.NS", "IGL.NS", "INDHOTEL.NS",
    "INDIACEM.NS", "INDIAMART.NS", "INDIGO.NS", "IOC.NS", "IRB.NS",
    "JKCEMENT.NS", "JSL.NS", "JUBLFOOD.NS", "LALPATHLAB.NS",
    "LAURUSLABS.NS", "LICHSGFIN.NS", "LICI.NS", "LTF.NS", "LTIM.NS",
    "LTTS.NS", "M&MFIN.NS", "MANAPPURAM.NS", "MARICO.NS", "MCX.NS",
    "METROPOLIS.NS", "MFSL.NS", "MGL.NS", "MRF.NS", "NATIONALUM.NS",
    "NAVINFLUOR.NS", "OFSS.NS", "OIL.NS", "PEL.NS", "PETRONET.NS",
    "POLYCAB.NS", "PVRINOX.NS", "RAMCOCEM.NS", "RBLBANK.NS", "SBICARD.NS",
    "SHREECEM.NS", "SUNTV.NS", "SYNGENE.NS", "TATACHEM.NS", "TATACOMM.NS",
    "TATAELXSI.NS", "TIINDIA.NS", "TORNTPOWER.NS", "UBL.NS", "VBL.NS",
    "ZEEL.NS",
]

# Build symbol → index membership
def _build_universe():
    sym_index = {}
    for sym in _NIFTY50:
        sym_index.setdefault(sym, []).append("NIFTY 50")
    for sym in _BANKNIFTY:
        sym_index.setdefault(sym, []).append("BANK NIFTY")
    for sym in _FINNIFTY:
        sym_index.setdefault(sym, []).append("FINNIFTY")
    for sym in _SENSEX:
        sym_index.setdefault(sym, []).append("SENSEX")
    for sym in _NIFTY200_EXTRA:
        sym_index.setdefault(sym, []).append("NIFTY 200")
    for sym in _FNO_ONLY:
        sym_index.setdefault(sym, []).append("F&O")
    # NIFTY 50 stocks are also in NIFTY 200
    for sym in _NIFTY50:
        if "NIFTY 200" not in sym_index.get(sym, []):
            sym_index[sym].append("NIFTY 200")
    return sym_index

SYMBOL_INDEX = _build_universe()
ALL_SYMBOLS = list(SYMBOL_INDEX.keys())

# Live data cache: short TTL so the UI can poll every 15s without hammering Fyers.
_CACHE = {"data": None, "ts": 0}
_CACHE_TTL = 12  # seconds — slightly under the UI poll interval (15s)

# Heavy per-symbol baseline (10-day avg vol + name). These don't change
# intraday, so we compute once and reuse for ~30 min. Each live refresh then
# only batch-fetches today's quote (price/change/today_vol).
_BASELINE_CACHE: dict[str, dict] = {}
_BASELINE_TTL = 30 * 60  # 30 minutes


def _build_baseline(symbol):
    """Heavy: 10-day avg volume + display name. Cached for 30 min."""
    try:
        hist = market_data.get_history(symbol, days=15, interval="1d")
        if hist is None or len(hist) < 2:
            return None
        hist = hist.dropna(subset=["Close", "Volume"])
        if len(hist) < 2:
            return None
        prev_vols = hist["Volume"].iloc[-11:-1] if len(hist) >= 11 else hist["Volume"].iloc[:-1]
        avg_10d = int(prev_vols.mean()) if len(prev_vols) > 0 else 0
        prev_close = float(hist["Close"].iloc[-2])

        info = market_data.get_info(symbol) or {}
        short = symbol.replace(".NS", "")
        return {
            "name": info.get("name") or short,
            "avg_10d_vol": avg_10d,
            "prev_close": prev_close,
        }
    except Exception as e:
        print(f"[volume baseline] {symbol}: {e}")
        return None


def _get_baseline(symbol):
    now = time.time()
    cached = _BASELINE_CACHE.get(symbol)
    if cached and now - cached["ts"] < _BASELINE_TTL:
        return cached["data"]
    data = _build_baseline(symbol)
    if data:
        _BASELINE_CACHE[symbol] = {"data": data, "ts": now}
    return data


def _signal_for(ratio, change_pct):
    if ratio >= 3 and change_pct >= 1.5:
        return "STRONG BUY", "Heavy buying volume with strong price gain"
    if ratio >= 2 and change_pct >= 0.5:
        return "BUY", "Above-average volume with positive price action"
    if ratio >= 3 and change_pct <= -1.5:
        return "STRONG SELL", "Heavy selling volume with sharp price drop"
    if ratio >= 2 and change_pct <= -0.5:
        return "SELL", "Above-average volume with negative price action"
    if ratio >= 1.5 and change_pct >= 0:
        return "WATCH BUY", "Mild volume uptick with green close"
    if ratio >= 1.5 and change_pct < 0:
        return "WATCH SELL", "Mild volume uptick with red close"
    return "HOLD", "Volume below average — no conviction"


def _load(force=False):
    """Light path: 1 batched ``get_quotes`` call for all ~120 symbols.

    Heavy per-symbol baselines (10-day avg vol, name) are warmed in parallel on
    the very first call only and reused for 30 minutes.
    """
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["ts"] < _CACHE_TTL):
        return _CACHE["data"]

    # Warm baselines that have expired (only the first scan does the bulk work)
    missing = [s for s in ALL_SYMBOLS
               if not _BASELINE_CACHE.get(s)
               or (now - _BASELINE_CACHE[s]["ts"]) >= _BASELINE_TTL]
    if missing:
        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(_get_baseline, missing))

    # Cheap path: batched live quotes
    try:
        quotes = market_data.get_quotes(ALL_SYMBOLS) or {}
    except Exception as e:
        print(f"[volume] get_quotes failed: {e}")
        quotes = {}

    stocks = []
    for sym in ALL_SYMBOLS:
        base = _get_baseline(sym)
        if not base:
            continue
        q = quotes.get(sym) or {}
        price = q.get("price")
        if not price:
            continue
        today_vol = int(q.get("volume") or 0)
        change_pct = q.get("change_pct")
        if change_pct is None:
            prev_close = base.get("prev_close") or price
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0
        avg_10d = base["avg_10d_vol"]
        ratio = round(today_vol / avg_10d, 2) if avg_10d else 0.0
        signal, reason = _signal_for(ratio, change_pct)

        short = sym.replace(".NS", "")
        stocks.append({
            "symbol": short,
            "name": base["name"],
            "price": round(float(price), 2),
            "change_pct": float(change_pct),
            "today_vol": today_vol,
            "avg_10d_vol": avg_10d,
            "vol_ratio": ratio,
            "signal": signal,
            "signal_reason": reason,
            "indices": SYMBOL_INDEX.get(sym, []),
        })

    stocks.sort(key=lambda s: s["vol_ratio"], reverse=True)
    data = {"stocks": stocks, "total": len(stocks), "ts": int(now)}
    _CACHE["data"] = data
    _CACHE["ts"] = now
    return data


@volume_api.route("/api/volume/scan")
def volume_scan():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    from flask import request
    force = request.args.get("force") == "1"
    return jsonify(_load(force=force))


@volume_api.route("/volume")
def volume_page():
    if "email" not in session:
        return redirect(url_for("logIn"))
    return render_template("volume.html",
                           name=session.get("name", "User"),
                           email=session.get("email", ""),
                           title="Volume Alerts")
