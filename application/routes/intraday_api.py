"""Intraday trading signals using RSI + MACD + EMA crossover strategy."""
import datetime
import traceback

import yfinance as yf
import pandas as pd
import numpy as np
from flask import Blueprint, jsonify, session, render_template, redirect, url_for

intraday_api = Blueprint("intraday_api", __name__)

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
        ticker = yf.Ticker(symbol)
        # Get 5-day 15-min data for intraday analysis
        df = ticker.history(period="5d", interval="15m")
        if df.empty or len(df) < 30:
            return None

        # Also get daily data for longer-term context
        daily = ticker.history(period="3mo")
        if daily.empty or len(daily) < 30:
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
        title="Intraday Signals",
    )


@intraday_api.route("/api/intraday/scan", methods=["POST"])
def scan_stocks():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401

    results = []
    for symbol in INTRADAY_WATCHLIST:
        data = _analyze_stock(symbol)
        if data:
            results.append(data)

    results.sort(key=lambda x: x["score"], reverse=True)
    scan_time = datetime.datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    return jsonify({
        "stocks": results,
        "scan_time": scan_time,
        "buy_count": len([r for r in results if "BUY" in r["signal"]]),
        "sell_count": len([r for r in results if "SELL" in r["signal"]]),
        "hold_count": len([r for r in results if r["signal"] == "HOLD"]),
    })
