"""Advanced portfolio analytics API — Bloomberg-inspired metrics."""
import math
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, session
import numpy as np

from application.services.plans import requires_plan

advanced_analytics_api = Blueprint("advanced_analytics_api", __name__)

# Per-user analytics cache TTL. Long enough to absorb repeated dashboard
# opens / tab switches, short enough that intraday price moves refresh.
_ANALYTICS_TTL = 15 * 60  # 15 minutes


def _holdings_signature(stocks) -> str:
    """Stable short hash of the holdings that changes on any edit."""
    import hashlib

    parts = []
    for s in sorted(stocks, key=lambda x: (x.get("Symbol") or x.get("StockName") or "")):
        parts.append("{}|{}|{}|{}".format(
            (s.get("Symbol") or s.get("StockName") or "").strip().upper(),
            s.get("Quantity") or 0,
            s.get("PurchasePrice") or 0,
            s.get("CurrentPrice") or 0,
        ))
    raw = ";".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]



def _login_required_api(f):
    from functools import wraps
    from flask import redirect, url_for
    @wraps(f)
    def wrap(*args, **kwargs):
        if "email" not in session or "user_id" not in session:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrap


def _fetch_stocks():
    from application.services.azure_table import stocks_table_client
    from azure.core.exceptions import ResourceNotFoundError
    try:
        items = list(stocks_table_client.query_entities(
            query_filter=f"UserId eq '{session['user_id']}'"))
    except (ResourceNotFoundError, Exception):
        return []
    cleaned = []
    for raw in items:
        s = dict(raw)
        try:
            s["Quantity"] = int(s.get("Quantity") or 0)
        except (TypeError, ValueError):
            s["Quantity"] = 0
        for k in ("PurchasePrice", "CurrentPrice"):
            try:
                s[k] = float(s.get(k) or 0)
            except (TypeError, ValueError):
                s[k] = 0.0
        cleaned.append(s)
    return cleaned


def _load_close_series(all_symbols, days=400):
    """Return ``{symbol: Close Series}`` for daily candles, cache-first.

    Each symbol is read through :func:`market_data.get_history`, which is
    backed by the persistent Azure Table OHLC cache — so a symbol seeded on
    an earlier trading day keeps working even when the live provider (Fyers
    token / yfinance) is unavailable right now. If both the cache-refresh and
    the live fetch come back empty, we fall back to *any* rows still sitting
    in the cache (ignoring staleness) so the dashboard renders real numbers
    instead of the all-zero placeholder.
    """
    from application.services import market_data, ohlc_cache

    close_cols = {}
    for sym in all_symbols:
        if not sym:
            continue
        series = None
        try:
            df = market_data.get_history(sym, days=days, interval="1d")
            if df is not None and not df.empty and "Close" in df.columns:
                series = df["Close"]
        except Exception as e:  # noqa: BLE001
            print(f"[advanced_analytics] get_history failed for {sym}: {e}")

        # Last resort: whatever we ever cached for this symbol.
        if series is None or series.empty:
            try:
                cdf = ohlc_cache.load_cached(sym, days)
                if cdf is not None and not cdf.empty and "Close" in cdf.columns:
                    series = cdf["Close"]
            except Exception:
                pass

        if series is None or series.empty:
            continue
        series = series.copy()
        # Strip tz so symbols from different providers align on dates.
        if getattr(series.index, "tz", None) is not None:
            series.index = series.index.tz_localize(None)
        close_cols[sym] = series
    return close_cols


@advanced_analytics_api.route("/api/advanced-analytics", methods=["GET"])
@requires_plan("elite")
@_login_required_api
def get_advanced_analytics():
    """Compute advanced portfolio metrics using historical data."""
    import pandas as pd

    stocks = _fetch_stocks()
    if not stocks:
        return jsonify({"error": "no_stocks", "message": "Add stocks to see analytics."})

    # Per-user cache. The key embeds a signature of the holdings so any
    # portfolio edit (add / update / delete) produces a fresh key
    # automatically, while a short TTL bounds how stale the market-derived
    # metrics can get. Repeated dashboard opens within the window are served
    # straight from Redis instead of re-downloading 400 days of history.
    from flask import request as _req
    from application.services import cache as shared_cache

    _force = _req.args.get("refresh") == "1"
    _sig = _holdings_signature(stocks)
    _cache_key = f"analytics:{session['user_id']}:{_sig}"
    if not _force:
        try:
            _cached = shared_cache.jget(_cache_key)
            if isinstance(_cached, dict):
                return jsonify({**_cached, "cached": True})
        except Exception:
            pass

    # Gather symbols and weights
    symbols = []
    weights = []
    stock_data = []
    total_value = 0

    for s in stocks:
        cp = s.get("CurrentPrice") or s.get("PurchasePrice") or 0
        value = s["Quantity"] * cp
        total_value += value
        stock_data.append({
            "name": s.get("StockName", ""),
            "symbol": (s.get("Symbol") or "").strip(),
            "sector": s.get("Sector", "Other"),
            "quantity": s["Quantity"],
            "purchase_price": s["PurchasePrice"],
            "current_price": cp,
            "purchase_date": s.get("PurchaseDate"),
            "invested": s["Quantity"] * s["PurchasePrice"],
            "value": value,
        })

    if total_value == 0:
        return jsonify({"error": "no_value"})

    for sd in stock_data:
        sd["weight"] = sd["value"] / total_value
        if sd["symbol"]:
            symbols.append(sd["symbol"])
            weights.append(sd["weight"])

    # Fetch 1-year historical data
    benchmark_symbol = "^NSEI"  # NIFTY 50

    try:
        all_symbols = symbols + [benchmark_symbol]
        # Cache-first: read daily closes through the persistent Azure Table
        # OHLC cache instead of a live batch download. This keeps the
        # dashboard working on the deployed server even when the live sources
        # are down for the moment — an expired Fyers token or yfinance being
        # IP-blocked from the datacenter — as long as the nightly warm job (or
        # any earlier request) has seeded the cache.
        close_cols = _load_close_series(all_symbols, days=365)
        if not close_cols:
            return jsonify(_fallback_metrics(stock_data, total_value))
        close = pd.DataFrame(close_cols).sort_index()
    except Exception as e:
        print(f"[advanced_analytics] history load error: {e}")
        return jsonify(_fallback_metrics(stock_data, total_value))

    # Daily returns
    try:
        # A global dropna() would trim every symbol down to the *shortest*
        # available history, so a single newly-listed stock (or a symbol with
        # sparse data) could collapse the common window below the threshold and
        # force the all-zero fallback for the whole portfolio. Instead, drop
        # symbols that don't have enough history, keep the benchmark, and
        # forward-fill small holiday/alignment gaps before computing returns.
        port_cols = [c for c in close.columns if c != benchmark_symbol]
        max_hist = max((close[c].notna().sum() for c in port_cols), default=0)
        # Prefer ~3 months of data, but relax for young portfolios so we don't
        # needlessly fall back when the best available history is still short.
        min_points = min(60, max(20, int(max_hist * 0.5))) if max_hist else 60
        keep = [c for c in port_cols if close[c].notna().sum() >= min_points]
        if benchmark_symbol in close.columns and close[benchmark_symbol].notna().sum() >= 20:
            keep.append(benchmark_symbol)
        if keep:
            close = close[keep]
        # Fill interior gaps (holidays / provider misalignment) without
        # fabricating leading history; leading NaNs are trimmed by dropna below.
        close = close.ffill()
        returns_df = close.pct_change().dropna()
        if returns_df.empty or len(returns_df) < 10:
            return jsonify(_fallback_metrics(stock_data, total_value))
    except Exception:
        return jsonify(_fallback_metrics(stock_data, total_value))

    # Portfolio daily returns (weighted)
    port_returns = None
    valid_symbols = [s for s in symbols if s in returns_df.columns]
    valid_weights = []
    for s in valid_symbols:
        idx = symbols.index(s)
        valid_weights.append(weights[idx])

    if valid_symbols and valid_weights:
        w_sum = sum(valid_weights)
        norm_weights = [w / w_sum for w in valid_weights] if w_sum > 0 else valid_weights
        port_returns = (returns_df[valid_symbols] * norm_weights).sum(axis=1)
    else:
        return jsonify(_fallback_metrics(stock_data, total_value))

    # Benchmark returns
    bench_returns = returns_df[benchmark_symbol] if benchmark_symbol in returns_df.columns else None

    # === Compute Metrics ===
    trading_days = 252
    rf_rate = 0.065  # India 10Y govt bond ~6.5%
    rf_daily = rf_rate / trading_days

    # Annualized return
    total_port_return = (1 + port_returns).prod() - 1
    n_days = len(port_returns)
    ann_return = ((1 + total_port_return) ** (trading_days / n_days) - 1) if n_days > 0 else 0

    # Volatility
    daily_vol = port_returns.std()
    ann_volatility = daily_vol * math.sqrt(trading_days)

    # Sharpe Ratio
    excess_daily = port_returns - rf_daily
    sharpe = (excess_daily.mean() / port_returns.std() * math.sqrt(trading_days)) if port_returns.std() > 0 else 0

    # Sortino Ratio (downside deviation)
    downside = port_returns[port_returns < 0]
    downside_dev = downside.std() * math.sqrt(trading_days) if len(downside) > 0 else 1
    sortino = ((ann_return - rf_rate) / downside_dev) if downside_dev > 0 else 0

    # Max Drawdown
    cum_returns = (1 + port_returns).cumprod()
    rolling_max = cum_returns.cummax()
    drawdowns = (cum_returns - rolling_max) / rolling_max
    max_drawdown = drawdowns.min()

    # Beta & Alpha (vs NIFTY 50)
    beta = 0
    alpha = 0
    correlation = 0
    bench_ann_return = 0
    if bench_returns is not None and len(bench_returns) > 10:
        cov_matrix = np.cov(port_returns.values, bench_returns.values)
        beta = cov_matrix[0, 1] / cov_matrix[1, 1] if cov_matrix[1, 1] != 0 else 0
        bench_total = (1 + bench_returns).prod() - 1
        bench_ann_return = ((1 + bench_total) ** (trading_days / n_days) - 1) if n_days > 0 else 0
        alpha = ann_return - (rf_rate + beta * (bench_ann_return - rf_rate))
        correlation = np.corrcoef(port_returns.values, bench_returns.values)[0, 1]

    # Treynor Ratio
    treynor = ((ann_return - rf_rate) / beta) if beta != 0 else 0

    # Information Ratio
    info_ratio = 0
    if bench_returns is not None:
        active_returns = port_returns - bench_returns
        tracking_error = active_returns.std() * math.sqrt(trading_days)
        info_ratio = (active_returns.mean() * trading_days / tracking_error) if tracking_error > 0 else 0

    # Value at Risk (parametric, 95% & 99%)
    var_95 = float(np.percentile(port_returns, 5)) * total_value
    var_99 = float(np.percentile(port_returns, 1)) * total_value

    # Conditional VaR (Expected Shortfall)
    cvar_95 = float(port_returns[port_returns <= np.percentile(port_returns, 5)].mean()) * total_value
    cvar_99 = float(port_returns[port_returns <= np.percentile(port_returns, 1)].mean()) * total_value

    # Calmar Ratio
    calmar = (ann_return / abs(max_drawdown)) if max_drawdown != 0 else 0

    # Concentration (HHI - Herfindahl-Hirschman Index)
    hhi = sum(sd["weight"] ** 2 for sd in stock_data)
    effective_n = 1 / hhi if hhi > 0 else len(stock_data)

    # Sector concentration
    sector_weights = {}
    for sd in stock_data:
        sec = sd["sector"] or "Other"
        sector_weights[sec] = sector_weights.get(sec, 0) + sd["weight"]

    # Individual stock metrics
    stock_metrics = []
    for sd in stock_data:
        sym = sd["symbol"]
        gain = sd["value"] - sd["invested"]
        gain_pct = (gain / sd["invested"] * 100) if sd["invested"] > 0 else 0
        contrib = gain / total_value * 100 if total_value > 0 else 0

        sm = {
            "name": sd["name"],
            "symbol": sym,
            "sector": sd["sector"],
            "weight": round(sd["weight"] * 100, 2),
            "invested": round(sd["invested"], 2),
            "value": round(sd["value"], 2),
            "gain": round(gain, 2),
            "gain_pct": round(gain_pct, 2),
            "contribution": round(contrib, 2),
        }

        # Per-stock volatility & beta
        if sym in returns_df.columns:
            stock_vol = returns_df[sym].std() * math.sqrt(trading_days)
            sm["volatility"] = round(stock_vol * 100, 2)
            if bench_returns is not None and len(bench_returns) > 10:
                scov = np.cov(returns_df[sym].values, bench_returns.values)
                sm["beta"] = round(scov[0, 1] / scov[1, 1], 3) if scov[1, 1] != 0 else 0
            else:
                sm["beta"] = 0
            # RSI (14-day)
            if sym in close.columns:
                sm["rsi"] = _calc_rsi(close[sym])
            # 50/200 DMA position
            if sym in close.columns and len(close[sym].dropna()) >= 200:
                last = close[sym].dropna().iloc[-1]
                ma50 = close[sym].dropna().iloc[-50:].mean()
                ma200 = close[sym].dropna().iloc[-200:].mean()
                sm["above_50dma"] = bool(last > ma50)
                sm["above_200dma"] = bool(last > ma200)
                sm["dma_50"] = round(float(ma50), 2)
                sm["dma_200"] = round(float(ma200), 2)
                sm["last_close"] = round(float(last), 2)
        else:
            sm["volatility"] = 0
            sm["beta"] = 0

        stock_metrics.append(sm)

    # Cumulative return series (for chart)
    cum_series = []
    bench_series = []
    dates = []
    try:
        for i, (dt, val) in enumerate(cum_returns.items()):
            if i % 5 == 0 or i == len(cum_returns) - 1:  # sample every 5 days
                dates.append(dt.strftime("%Y-%m-%d"))
                cum_series.append(round((val - 1) * 100, 2))
        if bench_returns is not None:
            bench_cum = (1 + bench_returns).cumprod()
            for i, (dt, val) in enumerate(bench_cum.items()):
                if i % 5 == 0 or i == len(bench_cum) - 1:
                    bench_series.append(round((val - 1) * 100, 2))
    except Exception:
        pass

    # Drawdown series
    dd_series = []
    try:
        for i, (dt, val) in enumerate(drawdowns.items()):
            if i % 5 == 0 or i == len(drawdowns) - 1:
                dd_series.append(round(val * 100, 2))
    except Exception:
        pass

    # Monthly returns heatmap
    monthly_returns = _calc_monthly_returns(port_returns)

    result = {
        "summary": {
            "total_value": round(total_value, 2),
            "total_invested": round(sum(sd["invested"] for sd in stock_data), 2),
            "total_gain": round(total_value - sum(sd["invested"] for sd in stock_data), 2),
            "holdings_count": len(stock_data),
        },
        "performance": {
            "annualized_return": round(ann_return * 100, 2),
            "benchmark_return": round(bench_ann_return * 100, 2),
            "total_return": round(total_port_return * 100, 2),
            "volatility": round(ann_volatility * 100, 2),
            "max_drawdown": round(max_drawdown * 100, 2),
        },
        "risk_ratios": {
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
            "treynor": round(treynor, 3),
            "calmar": round(calmar, 3),
            "information_ratio": round(info_ratio, 3),
            "beta": round(beta, 3),
            "alpha": round(alpha * 100, 2),
            "correlation": round(correlation, 3),
        },
        "risk_metrics": {
            "var_95": round(var_95, 2),
            "var_99": round(var_99, 2),
            "cvar_95": round(cvar_95, 2),
            "cvar_99": round(cvar_99, 2),
            "daily_volatility": round(daily_vol * 100, 4),
            "ann_volatility": round(ann_volatility * 100, 2),
        },
        "diversification": {
            "hhi": round(hhi, 4),
            "effective_n": round(effective_n, 1),
            "sector_weights": {k: round(v * 100, 2) for k, v in sorted(sector_weights.items(), key=lambda x: -x[1])},
            "concentration_top3": round(sum(sorted([sd["weight"] for sd in stock_data], reverse=True)[:3]) * 100, 2),
        },
        "stocks": sorted(stock_metrics, key=lambda x: -x["value"]),
        "charts": {
            "dates": dates,
            "portfolio_cum": cum_series,
            "benchmark_cum": bench_series,
            "drawdown": dd_series,
            "monthly_returns": monthly_returns,
        },
    }
    result["insights"] = _build_insights(
        stock_data, total_value,
        result["performance"], result["risk_ratios"],
        result["risk_metrics"], result["diversification"],
        monthly_returns,
    )
    try:
        shared_cache.jset(_cache_key, result, ttl=_ANALYTICS_TTL)
    except Exception:
        pass
    return jsonify(result)


def _calc_rsi(series, period=14):
    """Calculate RSI-14."""
    try:
        delta = series.diff().dropna()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=period).mean().iloc[-1]
        avg_loss = loss.rolling(window=period).mean().iloc[-1]
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)
    except Exception:
        return None


def _calc_monthly_returns(port_returns):
    """Group daily returns into monthly returns for heatmap."""
    try:
        monthly = {}
        for dt, ret in port_returns.items():
            key = dt.strftime("%Y-%m")
            if key not in monthly:
                monthly[key] = 1
            monthly[key] *= (1 + ret)
        return {k: round((v - 1) * 100, 2) for k, v in sorted(monthly.items())}
    except Exception:
        return {}


def _fallback_metrics(stock_data, total_value):
    """Return basic metrics when historical data is unavailable."""
    total_invested = sum(sd["invested"] for sd in stock_data)
    total_gain = total_value - total_invested
    gain_pct = (total_gain / total_invested * 100) if total_invested > 0 else 0

    sector_weights = {}
    for sd in stock_data:
        sec = sd["sector"] or "Other"
        sector_weights[sec] = sector_weights.get(sec, 0) + sd["weight"]

    hhi = sum(sd["weight"] ** 2 for sd in stock_data)

    stocks = []
    for sd in stock_data:
        gain = sd["value"] - sd["invested"]
        stocks.append({
            "name": sd["name"], "symbol": sd["symbol"], "sector": sd["sector"],
            "weight": round(sd["weight"] * 100, 2),
            "invested": round(sd["invested"], 2), "value": round(sd["value"], 2),
            "gain": round(gain, 2),
            "gain_pct": round((gain / sd["invested"] * 100) if sd["invested"] > 0 else 0, 2),
            "contribution": round(gain / total_value * 100, 2) if total_value > 0 else 0,
            "volatility": 0, "beta": 0,
        })

    result = {
        "summary": {
            "total_value": round(total_value, 2),
            "total_invested": round(total_invested, 2),
            "total_gain": round(total_gain, 2),
            "holdings_count": len(stock_data),
        },
        "performance": {
            "annualized_return": round(gain_pct, 2),
            "benchmark_return": 0,
            "total_return": round(gain_pct, 2),
            "volatility": 0,
            "max_drawdown": 0,
        },
        "risk_ratios": {"sharpe": 0, "sortino": 0, "treynor": 0, "calmar": 0,
                        "information_ratio": 0, "beta": 0, "alpha": 0, "correlation": 0},
        "risk_metrics": {"var_95": 0, "var_99": 0, "cvar_95": 0, "cvar_99": 0,
                         "daily_volatility": 0, "ann_volatility": 0},
        "diversification": {
            "hhi": round(hhi, 4),
            "effective_n": round(1 / hhi if hhi > 0 else len(stock_data), 1),
            "sector_weights": {k: round(v * 100, 2) for k, v in sorted(sector_weights.items(), key=lambda x: -x[1])},
            "concentration_top3": round(sum(sorted([sd["weight"] for sd in stock_data], reverse=True)[:3]) * 100, 2),
        },
        "stocks": sorted(stocks, key=lambda x: -x["value"]),
        "charts": {"dates": [], "portfolio_cum": [], "benchmark_cum": [], "drawdown": [], "monthly_returns": {}},
        "fallback": True,
    }
    result["insights"] = _build_insights(
        stock_data, total_value,
        result["performance"], result["risk_ratios"],
        result["risk_metrics"], result["diversification"],
        {},
    )
    return result


# ── Plain-English insights builder ──────────────────────────────────────────

def _parse_date(s):
    """Best-effort parse of various human/ISO date formats."""
    if not s:
        return None
    s = str(s)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d",
                "%d %b %Y", "%d %B %Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except (ValueError, TypeError):
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _build_insights(stock_data, total_value, perf, ratios, rm, div, monthly_returns):
    """Translate the numeric metrics into a layperson-friendly summary."""
    inv = sum(sd.get("invested", 0) for sd in stock_data)
    ann_ret = perf.get("annualized_return", 0)
    bench_ret = perf.get("benchmark_return", 0)
    vol = perf.get("volatility", 0)
    max_dd = perf.get("max_drawdown", 0)
    sharpe = ratios.get("sharpe", 0)
    sortino = ratios.get("sortino", 0)
    beta = ratios.get("beta", 0)
    alpha = ratios.get("alpha", 0)
    hhi = div.get("hhi", 0)
    var95 = abs(rm.get("var_95", 0))

    sorted_by_weight = sorted(stock_data, key=lambda x: -x.get("weight", 0))

    # 1. Health score — equal-weighted across 4 pillars (0..25 each).
    sharpe_pt = 25 * _clamp(sharpe / 2.0, 0, 1)
    dd_pt     = 25 * (1 - _clamp(abs(max_dd) / 40.0, 0, 1))
    div_pt    = 25 * (1 - _clamp(hhi / 0.5, 0, 1))
    alpha_pt  = 25 * _clamp((alpha + 5) / 15.0, 0, 1)
    score = int(round(sharpe_pt + dd_pt + div_pt + alpha_pt))
    if   score >= 85: label = "Excellent"
    elif score >= 70: label = "Good"
    elif score >= 55: label = "Fair"
    elif score >= 40: label = "Below average"
    else:             label = "Needs attention"

    # 2. Status chips
    ret_diff = ann_ret - bench_ret
    if   ret_diff >= 2:  ret_chip  = {"text": "Beating market",   "tone": "good"}
    elif ret_diff > -2:  ret_chip  = {"text": "Matching market",  "tone": "neutral"}
    else:                ret_chip  = {"text": "Lagging market",   "tone": "bad"}

    if   beta < 0.9:     risk_chip = {"text": "Lower risk than market", "tone": "good"}
    elif beta <= 1.1:    risk_chip = {"text": "Market-level risk",      "tone": "neutral"}
    elif beta <= 1.5:    risk_chip = {"text": "Higher risk than market","tone": "warn"}
    else:                risk_chip = {"text": "Much higher risk",       "tone": "bad"}

    if   hhi < 0.15:     div_chip  = {"text": "Well diversified",         "tone": "good"}
    elif hhi < 0.25:     div_chip  = {"text": "Moderately concentrated",  "tone": "warn"}
    else:                div_chip  = {"text": "Highly concentrated",      "tone": "bad"}

    # 3. Verdict
    if score >= 70 and ret_diff >= 0:
        verdict = "Strong, well-rounded portfolio — you're being rewarded for the risk you're taking."
    elif beta > 1.3 and hhi > 0.25:
        verdict = "High-risk, concentrated portfolio — a single bad stock can hurt the whole portfolio."
    elif ret_diff < -3:
        verdict = "Underperforming the market — review your picks against a simple index fund."
    elif abs(max_dd) > 25:
        verdict = "Recovering from heavy losses — be ready for similar dips in the future."
    elif score >= 55:
        verdict = "Decent portfolio with room to improve — focus on diversification and reducing concentration."
    else:
        verdict = "Average portfolio — diversify across more stocks and sectors to reduce risk."

    # 4. Action suggestions (max 3)
    actions = []
    if sorted_by_weight:
        top = sorted_by_weight[0]
        top_w = top.get("weight", 0) * 100
        if top_w > 25:
            actions.append(f"Trim {top['name']} — it's {top_w:.1f}% of your portfolio (target <15%).")
    sector_top = max(div.get("sector_weights", {}).items(),
                     key=lambda kv: kv[1], default=(None, 0))
    if sector_top[0] and sector_top[1] > 40:
        actions.append(f"Reduce exposure to {sector_top[0]} — it's {sector_top[1]:.1f}% of your portfolio.")
    laggards = [s for s in stock_data
                if s.get("invested", 0) > 0
                and (s["value"] - s["invested"]) / s["invested"] * 100 < -10
                and s.get("weight", 0) * 100 > 5]
    if laggards:
        laggards.sort(key=lambda s: (s["value"] - s["invested"]) / max(s["invested"], 1))
        s = laggards[0]
        loss_pct = (s["value"] - s["invested"]) / s["invested"] * 100
        actions.append(
            f"Review {s['name']} — down {loss_pct:.1f}% and still "
            f"{s.get('weight', 0) * 100:.1f}% of portfolio."
        )
    if len(div.get("sector_weights", {})) < 4:
        actions.append("Add stocks from new sectors — you currently span fewer than 4 sectors.")
    actions = actions[:3]

    # 5. Plain-English translations keyed by metric.
    top_w_pct = sorted_by_weight[0].get("weight", 0) * 100 if sorted_by_weight else 0
    plain = {
        "sharpe":  (f"You earn ₹{sharpe:.2f} of return for every ₹1 of risk taken."
                    if sharpe > 0 else
                    "Returns aren't compensating you for the risk you're taking."),
        "sortino": ("Strong protection against losses."         if sortino >= 1.5
                    else "Moderate protection against losses." if sortino >= 0.5
                    else "Returns aren't keeping up with downside risk."),
        "beta":    (f"{(beta - 1) * 100:+.0f}% more sensitive than the market — "
                    f"if NIFTY drops 10%, expect about {abs(beta * 10):.1f}% drop."
                    if beta else "Moves independently of the market."),
        "alpha":   (f"Beating NIFTY by {alpha:.1f}% per year after adjusting for risk."
                    if alpha > 0 else
                    f"Underperforming NIFTY by {abs(alpha):.1f}% per year after adjusting for risk."),
        "max_dd":  (f"Worst dip so far: lost about ₹{abs(max_dd) * total_value / 100:,.0f} "
                    f"before recovering."),
        "vol":     f"Typical year swings between about ±{vol:.0f}%.",
        "var95":   f"On a bad day (1 in 20), you could lose around ₹{var95:,.0f} or more.",
        "hhi":     (f"Top holding is {top_w_pct:.0f}% of your portfolio."
                    if sorted_by_weight else "—"),
    }

    # 6. Money comparisons (1-year estimates).
    fd_rate = 0.07
    vs_fd    = (ann_ret / 100 - fd_rate) * inv
    vs_nifty = (ret_diff / 100) * inv

    # 7. Monthly stats
    monthly_stats = None
    if monthly_returns:
        months_sorted = sorted(monthly_returns.items())
        vals = [v for _, v in months_sorted]
        green = sum(1 for v in vals if v > 0)
        streak = best_streak = 0
        for v in vals:
            if v > 0:
                streak += 1
                best_streak = max(best_streak, streak)
            else:
                streak = 0
        best  = max(months_sorted, key=lambda kv: kv[1])
        worst = min(months_sorted, key=lambda kv: kv[1])
        monthly_stats = {
            "best":           {"month": best[0],  "return": round(best[1], 2)},
            "worst":          {"month": worst[0], "return": round(worst[1], 2)},
            "months_green":   green,
            "months_total":   len(vals),
            "win_rate":       round(green / len(vals) * 100, 1) if vals else 0,
            "longest_streak": best_streak,
            "avg_monthly":    round(sum(vals) / len(vals), 2) if vals else 0,
        }

    # 8. Risk in everyday terms
    stress_pct = (-20 * beta) if beta else -20  # market crash of 20%
    risk_words = {
        "stress_market_crash_pct":   round(stress_pct, 1),
        "stress_market_crash_value": round(stress_pct / 100 * total_value, 0),
        "top_holding_name":          sorted_by_weight[0]["name"] if sorted_by_weight else None,
        "top_holding_to_zero_pct":   round(top_w_pct, 1),
        "top_holding_to_zero_value": round(sorted_by_weight[0]["value"], 0) if sorted_by_weight else 0,
    }

    # 9. Tax estimate — FY 2025-26: STCG 20%, LTCG 12.5% above ₹1.25L exemption.
    today = datetime.now()
    stcg = 0.0
    ltcg = 0.0
    soon_lt = []
    have_dates = False
    for s in stock_data:
        pd_ = _parse_date(s.get("purchase_date"))
        if not pd_:
            continue
        have_dates = True
        days_held = (today - pd_).days
        g = s["value"] - s["invested"]
        if days_held < 365:
            stcg += g
            if days_held >= 300 and g > 0:
                soon_lt.append({
                    "name":         s["name"],
                    "days_to_lt":   365 - days_held,
                    "est_savings":  round(g * (0.20 - 0.125), 0),
                })
        else:
            ltcg += g
    tax_summary = None
    if have_dates:
        ltcg_exempt = 125000
        ltcg_taxable = max(0, ltcg - ltcg_exempt)
        est_tax = max(0, stcg) * 0.20 + ltcg_taxable * 0.125
        tax_summary = {
            "stcg_gain":              round(stcg, 0),
            "ltcg_gain":              round(ltcg, 0),
            "ltcg_exempt":            ltcg_exempt,
            "est_tax_if_sold_today":  round(est_tax, 0),
            "soon_long_term":         sorted(soon_lt, key=lambda x: x["days_to_lt"])[:5],
        }

    # 10. Projection — clamp growth between 0% and 30%.
    r = _clamp(ann_ret / 100, 0.0, 0.30)
    projection = {
        "rate_pct":  round(r * 100, 1),
        "years_5":   round(total_value * (1 + r) ** 5,  0),
        "years_10":  round(total_value * (1 + r) ** 10, 0),
    }

    return {
        "health_score":  score,
        "health_label":  label,
        "chips": [
            {"label": "Return",          **ret_chip},
            {"label": "Risk",            **risk_chip},
            {"label": "Diversification", **div_chip},
        ],
        "verdict":       verdict,
        "actions":       actions,
        "plain":         plain,
        "money": {
            "vs_fd_rupees":    round(vs_fd, 0),
            "vs_nifty_rupees": round(vs_nifty, 0),
            "fd_rate_pct":     round(fd_rate * 100, 1),
        },
        "monthly_stats": monthly_stats,
        "risk_words":    risk_words,
        "tax":           tax_summary,
        "projection":    projection,
    }
