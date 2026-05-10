"""Advanced portfolio analytics API — Bloomberg-inspired metrics."""
import math
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, session
import numpy as np

advanced_analytics_api = Blueprint("advanced_analytics_api", __name__)


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


@advanced_analytics_api.route("/api/advanced-analytics", methods=["GET"])
@_login_required_api
def get_advanced_analytics():
    """Compute advanced portfolio metrics using historical data."""
    import pandas as pd
    from application.services import market_data

    stocks = _fetch_stocks()
    if not stocks:
        return jsonify({"error": "no_stocks", "message": "Add stocks to see analytics."})

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
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    benchmark_symbol = "^NSEI"  # NIFTY 50

    try:
        all_symbols = symbols + [benchmark_symbol]
        per_sym = market_data.download_history(
            all_symbols, start_date, end_date, interval="1d"
        )
        # Build a single Close DataFrame keyed by symbol.
        close_cols = {}
        for sym, df in per_sym.items():
            if df is None or df.empty or "Close" not in df.columns:
                continue
            s = df["Close"].copy()
            # Strip tz so symbols from different providers align on dates.
            if getattr(s.index, "tz", None) is not None:
                s.index = s.index.tz_localize(None)
            close_cols[sym] = s
        if not close_cols:
            return jsonify(_fallback_metrics(stock_data, total_value))
        close = pd.DataFrame(close_cols).sort_index()
    except Exception as e:
        print(f"[advanced_analytics] history download error: {e}")
        return jsonify(_fallback_metrics(stock_data, total_value))

    # Daily returns
    try:
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

    return {
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
