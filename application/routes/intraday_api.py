"""Intraday trading signals using RSI + MACD + EMA crossover strategy."""
import datetime
import math
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
from flask import Blueprint, jsonify, request, session, render_template, redirect, url_for

from application.services import cache as shared_cache, market_data
from application.services import swing_scanner
from application.services import snapshot_store
from application.services.plans import requires_plan

intraday_api = Blueprint("intraday_api", __name__)

# Shared global cache for the intraday scan result. First user to hit /scan
# triggers a fresh run; subsequent users in the next 5 minutes get the cached
# result. ``?refresh=1`` forces a fresh scan (also re-seeds the cache).
_SCAN_CACHE_PREFIX = "intraday:scan:v2"
_SCAN_CACHE_TTL = 300  # 5 minutes

# Single source of truth for the scannable universe — same list the swing
# scanner and the intraday tool-suite use (large-caps first, then mid-cap
# movers), so a tier of N is always "the N most liquid names".
INTRADAY_UNIVERSE = list(swing_scanner.UNIVERSE)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# ── Scan sizing ─────────────────────────────────────────────────────────
# Each stock costs 2 provider calls: 15-min intraday history (always live)
# and 90-day daily history (served from the Azure OHLC cache after the first
# run of the day). Wall-clock time is therefore bounded by the provider's
# request-rate cap, not by CPU, so the estimate below is derived from the
# per-provider throttle that the provider modules actually enforce.
_CALLS_PER_STOCK = 2
_PROVIDER_RPS = {
    "dhan": 5.0,        # 5 req/s cap on the chart endpoints
    "fyers": 4.0,       # _MIN_REQUEST_INTERVAL = 0.25s per app
    "upstox": 25.0,     # 25 req/s / 250 per min
    "yfinance": 8.0,    # no published cap; conservative
}
_SCAN_MAX_WORKERS = 8
_TIER_COUNTS = (25, 50, 100, len(INTRADAY_UNIVERSE))
_TIER_META = {
    25: ("Quick", "Top 25 large-caps"),
    50: ("Balanced", "NIFTY 50 large-caps"),
    100: ("Wide", "Large-caps + mid-cap movers"),
}
_DEFAULT_SCAN_COUNT = 50


def _estimate_seconds(count: int) -> int:
    rps = _PROVIDER_RPS.get(market_data.provider_name(), 5.0)
    return max(5, int(math.ceil(count * _CALLS_PER_STOCK / rps)) + 3)


def scan_tiers() -> list:
    """Selectable universe sizes with a per-provider time estimate."""
    out, seen = [], set()
    for raw in _TIER_COUNTS:
        count = min(raw, len(INTRADAY_UNIVERSE))
        if count in seen:
            continue
        seen.add(count)
        label, desc = _TIER_META.get(raw, ("Full universe", "Every tracked stock"))
        out.append({
            "count": count,
            "label": label,
            "description": desc,
            "est_seconds": _estimate_seconds(count),
        })
    return out


def _resolve_count(raw) -> int:
    """Clamp a requested stock count to the nearest offered tier."""
    allowed = [t["count"] for t in scan_tiers()]
    try:
        want = int(raw)
    except (TypeError, ValueError):
        want = _DEFAULT_SCAN_COUNT
    return min(allowed, key=lambda c: abs(c - want))


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


def _compute_atr(df, period=14) -> float:
    """Average true range on whatever bars ``df`` holds (here: 15-min)."""
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.tail(period).mean())
    if not atr or math.isnan(atr) or atr <= 0:
        atr = float(df["Close"].iloc[-1]) * 0.005  # ~0.5% fallback
    return atr


def _build_plan(is_long: bool, entry: float, stop_candidate: float,
                target_candidate: float, atr: float) -> dict:
    """Turn a raw entry/stop/target idea into a plan with a sane min R:R."""
    stop_dist = (entry - stop_candidate) if is_long else (stop_candidate - entry)
    if stop_dist <= 0:
        stop_dist = max(atr, entry * 0.003)
        stop_candidate = entry - stop_dist if is_long else entry + stop_dist

    target_dist = (target_candidate - entry) if is_long else (entry - target_candidate)
    if target_dist < 1.2 * stop_dist:
        target_dist = 2 * stop_dist
        target_candidate = entry + target_dist if is_long else entry - target_dist

    return {
        "entry": round(entry, 2),
        "stop": round(stop_candidate, 2),
        "target": round(target_candidate, 2),
        "stop_pct": round(stop_dist / entry * 100, 2) if entry else 0,
        "target_pct": round(target_dist / entry * 100, 2) if entry else 0,
        "risk_reward": round(target_dist / stop_dist, 2) if stop_dist else 0,
    }


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
        # Two independent vote pools, tallied separately so a "STRONG" score
        # that's actually trend signals cancelling reversion signals (or
        # vice versa) can be told apart from genuine agreement.
        trend_score = 0
        reversion_score = 0
        reasons = []

        # RSI (reversion pool)
        if rsi_val < 30:
            reversion_score += 2
            reasons.append("RSI oversold (<30) — potential bounce")
        elif rsi_val < 40:
            reversion_score += 1
            reasons.append("RSI approaching oversold")
        elif rsi_val > 70:
            reversion_score -= 2
            reasons.append("RSI overbought (>70) — potential pullback")
        elif rsi_val > 60:
            reversion_score -= 1
            reasons.append("RSI approaching overbought")

        # MACD (trend pool)
        if macd_cross == "bullish" and hist_val > 0:
            trend_score += 2
            reasons.append("MACD bullish crossover with rising histogram")
        elif macd_cross == "bullish":
            trend_score += 1
            reasons.append("MACD above signal line")
        elif macd_cross == "bearish" and hist_val < 0:
            trend_score -= 2
            reasons.append("MACD bearish crossover with falling histogram")
        else:
            trend_score -= 1
            reasons.append("MACD below signal line")

        # EMA crossover (trend pool)
        if ema_cross == "bullish":
            trend_score += 1
            reasons.append("EMA 9 above EMA 21 (short-term uptrend)")
        else:
            trend_score -= 1
            reasons.append("EMA 9 below EMA 21 (short-term downtrend)")

        # VWAP (trend pool)
        if above_vwap:
            trend_score += 1
            reasons.append("Price above VWAP (institutional buying)")
        else:
            trend_score -= 1
            reasons.append("Price below VWAP (institutional selling)")

        # Volume
        if vol_ratio > 1.5:
            reasons.append(f"Volume {vol_ratio:.1f}x above average (strong momentum)")

        # Bollinger (reversion pool)
        if last_price <= bb_lower:
            reversion_score += 1
            reasons.append("Price at lower Bollinger Band — potential reversal up")
        elif last_price >= bb_upper:
            reversion_score -= 1
            reasons.append("Price at upper Bollinger Band — potential reversal down")

        score = trend_score + reversion_score

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

        # Setup classification: which vote pool actually drove the score.
        # "mixed" = trend and reversion pools disagree (opposite signs) —
        # the total can still look "STRONG" while the thesis is incoherent,
        # so no trade plan is offered for those.
        if trend_score == 0 and reversion_score == 0:
            setup_type = "neutral"
        elif trend_score * reversion_score < 0:
            setup_type = "mixed"
        elif abs(trend_score) >= abs(reversion_score):
            setup_type = "trend"
        else:
            setup_type = "reversion"

        # --- Trade plan (entry/stop/target/R:R) ---
        atr = _compute_atr(df)
        plan = None
        direction = "long" if score > 0 else ("short" if score < 0 else None)
        if direction and setup_type in ("trend", "reversion"):
            is_long = direction == "long"
            if setup_type == "trend":
                entry = last_price
                stop_candidate = day_low if is_long else day_high
                target_candidate = bb_upper if is_long else bb_lower
            else:  # reversion
                entry = last_price
                buf = 0.25 * atr
                stop_candidate = (bb_lower - buf) if is_long else (bb_upper + buf)
                target_candidate = vwap_val
            plan = _build_plan(is_long, entry, stop_candidate, target_candidate, atr)
            plan["direction"] = direction

        name = symbol.replace(".NS", "").replace(".BO", "")

        # Initial candle payload — reuse the 15-min frame already fetched
        # above (one less provider call per stock). The frontend can request
        # smaller (5m) or larger (1h) timeframes on-demand via
        # /api/intraday/candles.
        tail = df.tail(80)
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
            "setup_type": setup_type,
            "plan": plan,
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
        scan_tiers=scan_tiers(),
        default_scan_count=_resolve_count(_DEFAULT_SCAN_COUNT),
        universe_size=len(INTRADAY_UNIVERSE),
        swing_tiers=list(swing_scanner.TIER_COUNTS),
    )


@intraday_api.route("/api/intraday/scan", methods=["GET", "POST"])
def scan_stocks():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401

    body = request.get_json(silent=True) or {}
    force = (request.args.get("refresh") == "1") or (body.get("refresh") is True)
    snapshot_only = request.args.get("snapshot") == "1"
    count = _resolve_count(request.args.get("count", body.get("count")))
    cache_key = f"{_SCAN_CACHE_PREFIX}:{count}"
    snapshot_key = f"live:intraday_scan:{count}"

    if snapshot_only and not force:
        data = snapshot_store.serve_snapshot(snapshot_key)
        if data is None:
            return jsonify({"snapshot_missing": True})
        return jsonify(data)

    if not force:
        cached = shared_cache.jget(cache_key)
        if isinstance(cached, dict) and cached.get("stocks") is not None:
            return jsonify({**cached, "cached": True})

    symbols = INTRADAY_UNIVERSE[:count]
    started = datetime.datetime.now(IST)
    results = []
    with ThreadPoolExecutor(max_workers=_SCAN_MAX_WORKERS) as ex:
        futures = [ex.submit(_analyze_stock, s) for s in symbols]
        for fut in as_completed(futures):
            try:
                data = fut.result()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                continue
            if data:
                results.append(data)

    results.sort(key=lambda x: x["score"], reverse=True)
    finished = datetime.datetime.now(IST)

    payload = {
        "stocks": results,
        "scan_time": finished.strftime("%d %b %Y, %I:%M %p IST"),
        "requested": count,
        "elapsed_seconds": round((finished - started).total_seconds(), 1),
        "buy_count": len([r for r in results if "BUY" in r["signal"]]),
        "sell_count": len([r for r in results if "SELL" in r["signal"]]),
        "hold_count": len([r for r in results if r["signal"] == "HOLD"]),
        "cached": False,
    }
    try:
        shared_cache.jset(cache_key, payload, ttl=_SCAN_CACHE_TTL)
    except Exception:
        pass
    try:
        snapshot_store.put(snapshot_key, payload)
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
    body = request.get_json(silent=True) or {}
    force = (request.args.get("refresh") == "1") or (body.get("refresh") is True)
    raw_count = request.args.get("count", body.get("count"))
    count = None
    if raw_count:
        try:
            want = int(raw_count)
            count = min((c for c in swing_scanner.TIER_COUNTS if c >= want),
                        default=swing_scanner.TIER_COUNTS[-1])
        except (TypeError, ValueError):
            count = None
    try:
        payload = swing_scanner.scan(count=count, force_refresh=force)
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
    # Candle data for a given (symbol, timeframe) is identical for every user,
    # so cache the shaped payload in Redis. Intraday timeframes use a short
    # TTL; daily bars are stable for much longer.
    _ttl = 60 if tf in ("5m", "15m") else (300 if tf == "1h" else 15 * 60)
    _key = f"candles:{symbol}:{tf}"
    try:
        _cached = shared_cache.jget(_key)
        if isinstance(_cached, dict):
            return jsonify(_cached)
    except Exception:
        pass
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
    payload = {"ok": True, "symbol": symbol, "tf": tf, "candles": out}
    if out:
        try:
            shared_cache.jset(_key, payload, ttl=_ttl)
        except Exception:
            pass
    return jsonify(payload)
