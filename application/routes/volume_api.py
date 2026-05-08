"""Volume Shockers – compare today's volume to 10-day average."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf
from flask import Blueprint, render_template, jsonify, session, redirect, url_for

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
    "AUROPHARMA.NS", "BIOCON.NS", "BOSCHLTD.NS", "CADILAHC.NS",
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
    # NIFTY 50 stocks are also in NIFTY 200
    for sym in _NIFTY50:
        if "NIFTY 200" not in sym_index.get(sym, []):
            sym_index[sym].append("NIFTY 200")
    return sym_index

SYMBOL_INDEX = _build_universe()
ALL_SYMBOLS = list(SYMBOL_INDEX.keys())

_CACHE = {"data": None, "ts": 0}
_CACHE_TTL = 45  # 45 seconds — supports live refresh


def _fetch_volume(symbol):
    """Fetch today's volume and 10-day average volume for a symbol."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="15d")
        if hist is None or len(hist) < 2:
            return None

        # Drop rows with NaN Close (incomplete bars after-hours / pre-market)
        hist = hist.dropna(subset=["Close", "Volume"])
        if len(hist) < 2:
            return None

        today_vol = int(hist["Volume"].iloc[-1])
        # Previous 10 trading days (excluding today)
        prev_vols = hist["Volume"].iloc[-11:-1] if len(hist) >= 11 else hist["Volume"].iloc[:-1]
        avg_10d = int(prev_vols.mean()) if len(prev_vols) > 0 else 0

        if avg_10d == 0:
            ratio = 0
        else:
            ratio = round(today_vol / avg_10d, 2)

        price = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else price
        # Guard against NaN values that would corrupt JSON output
        import math
        if math.isnan(price) or math.isnan(prev_close):
            return None
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0

        # ── Signal: combine volume surge + price direction ─────────────
        # High volume + rising price → accumulation (BUY)
        # High volume + falling price → distribution (SELL)
        # Low volume → no clear signal (HOLD)
        if ratio >= 3 and change_pct >= 1.5:
            signal, reason = "STRONG BUY", "Heavy buying volume with strong price gain"
        elif ratio >= 2 and change_pct >= 0.5:
            signal, reason = "BUY", "Above-average volume with positive price action"
        elif ratio >= 3 and change_pct <= -1.5:
            signal, reason = "STRONG SELL", "Heavy selling volume with sharp price drop"
        elif ratio >= 2 and change_pct <= -0.5:
            signal, reason = "SELL", "Above-average volume with negative price action"
        elif ratio >= 1.5 and change_pct >= 0:
            signal, reason = "WATCH BUY", "Mild volume uptick with green close"
        elif ratio >= 1.5 and change_pct < 0:
            signal, reason = "WATCH SELL", "Mild volume uptick with red close"
        else:
            signal, reason = "HOLD", "Volume below average — no conviction"

        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            pass

        short = symbol.replace(".NS", "")
        return {
            "symbol": short,
            "name": (info.get("shortName") or info.get("longName") or short),
            "price": round(price, 2),
            "change_pct": change_pct,
            "today_vol": today_vol,
            "avg_10d_vol": avg_10d,
            "vol_ratio": ratio,
            "signal": signal,
            "signal_reason": reason,
            "indices": SYMBOL_INDEX.get(symbol, []),
        }
    except Exception as e:
        print(f"[volume] {symbol}: {e}")
        return None


def _load(force=False):
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["ts"] < _CACHE_TTL):
        return _CACHE["data"]

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(_fetch_volume, ALL_SYMBOLS))

    stocks = [r for r in results if r is not None]
    stocks.sort(key=lambda s: s["vol_ratio"], reverse=True)

    data = {"stocks": stocks, "total": len(stocks)}
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
