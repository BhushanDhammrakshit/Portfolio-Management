"""Intraday trading signals using RSI + MACD + EMA crossover strategy."""
import datetime
import traceback

import pandas as pd
import numpy as np
from flask import Blueprint, jsonify, request, session, render_template, redirect, url_for

from application.services import cache as shared_cache, market_data
from application.services import swing_scanner
from application.services.plans import requires_plan

intraday_api = Blueprint("intraday_api", __name__)

# Shared global cache for the intraday scan result. First user to hit /scan
# triggers a fresh run; subsequent users in the next 5 minutes get the cached
# result. ``?refresh=1`` forces a fresh scan (also re-seeds the cache).
_SCAN_CACHE_KEY = "intraday:scan:v1"
_SCAN_CACHE_TTL = 300  # 5 minutes

# Popular Indian stocks for intraday scanning
INTRADAY_WATCHLIST = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
    "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "KOTAKBANK.NS", "LT.NS",
    "HINDUNILVR.NS", "AXISBANK.NS", "BAJFINANCE.NS", "MARUTI.NS",
    "SUNPHARMA.NS", "TITAN.NS", "ULTRACEMCO.NS", "WIPRO.NS",
    "HCLTECH.NS", "ADANIENT.NS", "TATAMOTORS.NS", "TATASTEEL.NS",
    "NTPC.NS", "POWERGRID.NS", "ONGC.NS", "COALINDIA.NS",
    "JSWSTEEL.NS", "M&M.NS", "BAJAJFINSV.NS", "TECHM.NS",
]

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _compute_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _compute_vwap(df):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    cumvol = df["Volume"].cumsum()
    cumtp = (tp * df["Volume"]).cumsum()
    return cumtp / cumvol.replace(0, np.nan)


def _analyze_stock(symbol):
    """Fetch recent data and compute intraday signals."""
    try:
        # Get 5-day 15-min data for intraday analysis
        df = market_data.get_history(symbol, days=5, interval="15m")
        if df is None or df.empty or len(df) < 30:
            return None

        # Also get daily data for longer-term context
        daily = market_data.get_history(symbol, days=90, interval="1d")
        if daily is None or daily.empty or len(daily) < 30:
            return None

        close = df["Close"]
        last_price = float(close.iloc[-1])
        prev_close = float(daily["Close"].iloc[-2]) if len(daily) >= 2 else last_price

        # RSI (14-period on 15min bars)
        rsi = _compute_rsi(close, 14)
        rsi_val = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

        # MACD
        macd_line, signal_line, histogram = _compute_macd(close)
        macd_val = float(macd_line.iloc[-1]) if not pd.isna(macd_line.iloc[-1]) else 0
        signal_val = float(signal_line.iloc[-1]) if not pd.isna(signal_line.iloc[-1]) else 0
        hist_val = float(histogram.iloc[-1]) if not pd.isna(histogram.iloc[-1]) else 0
        macd_cross = "bullish" if macd_val > signal_val else "bearish"

        # EMA 9 & 21
        ema9 = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
        ema21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
        ema_cross = "bullish" if ema9 > ema21 else "bearish"

        # VWAP
        vwap = _compute_vwap(df)
        vwap_val = float(vwap.iloc[-1]) if not pd.isna(vwap.iloc[-1]) else last_price
        above_vwap = last_price > vwap_val

        # Daily EMA 20 / 50
        ema20d = float(daily["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
        ema50d = float(daily["Close"].ewm(span=50, adjust=False).mean().iloc[-1])

        # Bollinger Bands (20, 2)
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_upper = float((sma20 + 2 * std20).iloc[-1]) if not pd.isna(sma20.iloc[-1]) else last_price
        bb_lower = float((sma20 - 2 * std20).iloc[-1]) if not pd.isna(sma20.iloc[-1]) else last_price

        # Volume analysis
        avg_vol = float(df["Volume"].tail(20).mean())
        curr_vol = float(df["Volume"].iloc[-1])
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1.0

        # Day change
        day_change = last_price - prev_close
        day_change_pct = (day_change / prev_close * 100) if prev_close > 0 else 0

        # Day high/low from today's intraday data
        today = df.index[-1].date()
        today_data = df[df.index.date == today]
        day_high = float(today_data["High"].max()) if not today_data.empty else last_price
        day_low = float(today_data["Low"].min()) if not today_data.empty else last_price

        # --- Signal logic ---
        # Score: +ve = bullish, -ve = bearish
        score = 0
        reasons = []

        # RSI
        if rsi_val < 30:
            score += 2
            reasons.append("RSI oversold (<30) — potential bounce")
        elif rsi_val < 40:
            score += 1
            reasons.append("RSI approaching oversold")
        elif rsi_val > 70:
            score -= 2
            reasons.append("RSI overbought (>70) — potential pullback")
        elif rsi_val > 60:
            score -= 1
            reasons.append("RSI approaching overbought")

        # MACD
        if macd_cross == "bullish" and hist_val > 0:
            score += 2
            reasons.append("MACD bullish crossover with rising histogram")
        elif macd_cross == "bullish":
            score += 1
            reasons.append("MACD above signal line")
        elif macd_cross == "bearish" and hist_val < 0:
            score -= 2
            reasons.append("MACD bearish crossover with falling histogram")
        else:
            score -= 1
            reasons.append("MACD below signal line")

        # EMA crossover
        if ema_cross == "bullish":
            score += 1
            reasons.append("EMA 9 above EMA 21 (short-term uptrend)")
        else:
            score -= 1
            reasons.append("EMA 9 below EMA 21 (short-term downtrend)")

        # VWAP
        if above_vwap:
            score += 1
            reasons.append("Price above VWAP (institutional buying)")
        else:
            score -= 1
            reasons.append("Price below VWAP (institutional selling)")

        # Volume
        if vol_ratio > 1.5:
            reasons.append(f"Volume {vol_ratio:.1f}x above average (strong momentum)")

        # Bollinger
        if last_price <= bb_lower:
            score += 1
            reasons.append("Price at lower Bollinger Band — potential reversal up")
        elif last_price >= bb_upper:
            score -= 1
            reasons.append("Price at upper Bollinger Band — potential reversal down")

        # Determine signal
        if score >= 3:
            signal = "STRONG BUY"
        elif score >= 1:
            signal = "BUY"
        elif score <= -3:
            signal = "STRONG SELL"
        elif score <= -1:
            signal = "SELL"
        else:
            signal = "HOLD"

        name = symbol.replace(".NS", "").replace(".BO", "")

        # Initial candle payload — last 2 trading days of 15-min bars (≈ 50 bars).
        # The frontend can request smaller (5m) or larger (1h) timeframes
        # on-demand via /api/intraday/candles.
        try:
            ch = market_data.get_history(symbol, days=2, interval="15m")
        except Exception:
            ch = df  # fallback to whatever we already loaded
        if ch is None or ch.empty:
            ch = df
        tail = ch.tail(80)
        candles = []
        for ts, row in tail.iterrows():
            try:
                candles.append({
                    "t": int(ts.timestamp() * 1000) if hasattr(ts, "timestamp") else 0,
                    "o": round(float(row["Open"]), 2),
                    "h": round(float(row["High"]), 2),
                    "l": round(float(row["Low"]), 2),
                    "c": round(float(row["Close"]), 2),
                })
            except (ValueError, KeyError):
                continue

        return {
            "symbol": symbol,
            "name": name,
            "price": round(last_price, 2),
            "prev_close": round(prev_close, 2),
            "day_change": round(day_change, 2),
            "day_change_pct": round(day_change_pct, 2),
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "rsi": round(rsi_val, 1),
            "macd": round(macd_val, 4),
            "macd_signal": round(signal_val, 4),
            "macd_hist": round(hist_val, 4),
            "macd_cross": macd_cross,
            "ema9": round(ema9, 2),
            "ema21": round(ema21, 2),
            "ema_cross": ema_cross,
            "vwap": round(vwap_val, 2),
            "above_vwap": above_vwap,
            "ema20d": round(ema20d, 2),
            "ema50d": round(ema50d, 2),
            "bb_upper": round(bb_upper, 2),
            "bb_lower": round(bb_lower, 2),
            "vol_ratio": round(vol_ratio, 2),
            "signal": signal,
            "score": score,
            "reasons": reasons,
            "candles": candles,
        }
    except Exception as e:
        print(f"[intraday] {symbol} error: {e}")
        traceback.print_exc()
        return None


@intraday_api.route("/intraday")
def intraday_page():
    if "email" not in session:
        return redirect(url_for("logIn"))
    return render_template(
        "intraday.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="Intraday Scanner",
    )


@intraday_api.route("/api/intraday/scan", methods=["GET", "POST"])
def scan_stocks():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401

    force = (request.args.get("refresh") == "1") or \
            ((request.get_json(silent=True) or {}).get("refresh") is True)

    if not force:
        cached = shared_cache.jget(_SCAN_CACHE_KEY)
        if isinstance(cached, dict) and cached.get("stocks") is not None:
            return jsonify({**cached, "cached": True})

    results = []
    for symbol in INTRADAY_WATCHLIST:
        data = _analyze_stock(symbol)
        if data:
            results.append(data)

    results.sort(key=lambda x: x["score"], reverse=True)
    scan_time = datetime.datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    payload = {
        "stocks": results,
        "scan_time": scan_time,
        "buy_count": len([r for r in results if "BUY" in r["signal"]]),
        "sell_count": len([r for r in results if "SELL" in r["signal"]]),
        "hold_count": len([r for r in results if r["signal"] == "HOLD"]),
        "cached": False,
    }
    try:
        shared_cache.jset(_SCAN_CACHE_KEY, payload, ttl=_SCAN_CACHE_TTL)
    except Exception:
        pass
    return jsonify(payload)


@intraday_api.route("/api/intraday/swing-scan", methods=["GET", "POST"])
@requires_plan("pro")
def swing_scan():
    """Qullamaggie + Minervini momentum-breakout scanner.

    Targets 10-15% upside in 1-2 weeks via:
      • Stage-2 trend template (Minervini)
      • 3-month relative-strength leadership (Jegadeesh-Titman)
      • Volatility contraction → expansion (Qullamaggie VCP)
      • Volume thrust on pivot breakout
      • Episodic catalyst gaps (Stockbee PEAD)
    """
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    force = (request.args.get("refresh") == "1") or \
            ((request.get_json(silent=True) or {}).get("refresh") is True)
    try:
        payload = swing_scanner.scan(force_refresh=force)
        return jsonify(payload)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"error": "scan failed", "detail": str(e)}), 500


# Allowed (interval, days) combos. yfinance limits 1m/5m to ~7 days history,
# but we cap aggressively to keep payloads small and respect Fyers rate limits.
_TF_CONFIG = {
    "5m": {"days": 5, "interval": "5m", "max_bars": 200},
    "15m": {"days": 5, "interval": "15m", "max_bars": 150},
    "1h": {"days": 30, "interval": "60m", "max_bars": 200},
    "1d": {"days": 180, "interval": "1d", "max_bars": 200},
}


@intraday_api.route("/api/intraday/candles")
def intraday_candles():
    """On-demand OHLC fetch for the detail-modal chart.
    Query: ?symbol=RELIANCE.NS&tf=5m
    Returns: { ok, symbol, tf, candles: [{t,o,h,l,c}, ...] }
    """
    if "email" not in session:
        return jsonify({"ok": False, "error": "auth"}), 401
    from flask import request as _req
    raw_symbol = (_req.args.get("symbol") or "").strip()
    tf = (_req.args.get("tf") or "15m").strip()
    if not raw_symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400
    # Normalize: bare names like "BRITANNIA" → "BRITANNIA.NS" so yfinance
    # (the fallback when Fyers is unavailable) can resolve them.
    s = raw_symbol.upper()
    if "." not in s and ":" not in s and not s.startswith("^"):
        symbol = s + ".NS"
    else:
        symbol = s
    cfg = _TF_CONFIG.get(tf)
    if not cfg:
        return jsonify({"ok": False, "error": "invalid tf"}), 400
    try:
        df = market_data.get_history(symbol, days=cfg["days"], interval=cfg["interval"])
    except Exception as e:
        return jsonify({"ok": False, "error": f"fetch failed: {e}"}), 502
    if df is None or df.empty:
        return jsonify({"ok": False, "error": "no data"}), 502
    tail = df.tail(cfg["max_bars"])
    out = []
    for ts, row in tail.iterrows():
        try:
            out.append({
                "t": int(ts.timestamp() * 1000) if hasattr(ts, "timestamp") else 0,
                "o": round(float(row["Open"]), 2),
                "h": round(float(row["High"]), 2),
                "l": round(float(row["Low"]), 2),
                "c": round(float(row["Close"]), 2),
            })
        except (ValueError, KeyError):
            continue
    return jsonify({"ok": True, "symbol": symbol, "tf": tf, "candles": out})
