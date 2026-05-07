"""Heatmap data endpoint backed by yfinance with simple in-memory caching."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf
from flask import Blueprint, render_template, jsonify, session, redirect, url_for, request

# Silence yfinance's noisy "possibly delisted" warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

heatmap_bp = Blueprint("heatmap", __name__)

NIFTY_50 = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HCLTECH.NS", "LT.NS", "ITC.NS",
    "BHARTIARTL.NS", "ASIANPAINT.NS", "BAJFINANCE.NS", "HINDUNILVR.NS",
    "MARUTI.NS", "TITAN.NS", "ULTRACEMCO.NS", "SUNPHARMA.NS", "WIPRO.NS",
    "BAJAJ-AUTO.NS", "BAJAJFINSV.NS", "CIPLA.NS", "COALINDIA.NS", "DRREDDY.NS",
    "EICHERMOT.NS", "GRASIM.NS", "HEROMOTOCO.NS", "HINDALCO.NS", "JIOFIN.NS",
    "JSWSTEEL.NS", "M&M.NS", "NESTLEIND.NS", "NTPC.NS", "ONGC.NS",
    "POWERGRID.NS", "SBILIFE.NS", "SHRIRAMFIN.NS", "TATACONSUM.NS",
    "TATAMOTORS.NS", "TATASTEEL.NS", "TECHM.NS", "TRENT.NS",
    "ADANIENT.NS", "ADANIPORTS.NS", "APOLLOHOSP.NS", "BPCL.NS", "BRITANNIA.NS",
    "DIVISLAB.NS", "HDFCLIFE.NS", "INDUSINDBK.NS", "LTIMINDTREE.NS", "UPL.NS",
]

BANK_NIFTY = [
    "HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "SBIN.NS", "AXISBANK.NS",
    "BANKBARODA.NS", "PNB.NS", "IDFCFIRSTB.NS", "FEDERALBNK.NS", "AUBANK.NS",
]

_CACHE = {"data": None, "ts": 0}
_CACHE_TTL = 5 * 60  # 5 minutes


def _fetch_one(symbol):
    try:
        ticker = yf.Ticker(symbol)
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            info = {}

        price = info.get("regularMarketPrice")
        change = info.get("regularMarketChangePercent")
        if price is None or change is None:
            hist = ticker.history(period="2d")
            if len(hist) >= 2:
                price = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
                change = round((price - prev) / prev * 100, 2) if prev else 0.0
            elif len(hist) == 1:
                price = float(hist["Close"].iloc[-1])
                change = 0.0
            else:
                return None
        return {
            "name": info.get("shortName") or info.get("longName") or symbol.replace(".NS", ""),
            "symbol": symbol,
            "price": round(float(price), 2),
            "change": round(float(change), 2),
            "sector": info.get("sector") or "Other",
            "logo": info.get("logo_url") or info.get("logoUrl"),
        }
    except Exception as e:
        print(f"[heatmap] {symbol}: {e}")
        return None


def get_stock_data(symbols):
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_fetch_one, symbols))
    return [r for r in results if r]


def _load(force=False):
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["ts"] < _CACHE_TTL):
        return _CACHE["data"]
    data = {
        "nifty": get_stock_data(NIFTY_50),
        "banknifty": get_stock_data(BANK_NIFTY),
    }
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
                           title="Heatmap")
