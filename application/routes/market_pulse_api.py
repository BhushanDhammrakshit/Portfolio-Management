"""Market Pulse — NSE-sourced market breadth, movers and corporate data."""
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, render_template, request, redirect, session, url_for

from application.services import cache as shared_cache
from application.services import market_data
from application.services import option_chain as oc_service
from application.services import snapshot_store

market_pulse_api = Blueprint("market_pulse_api", __name__)


@market_pulse_api.route("/market-pulse")
def market_pulse_page():
    if "email" not in session:
        return redirect(url_for("logIn"))
    return render_template(
        "marketPulse.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="Market Pulse",
    )


@market_pulse_api.route("/api/market-pulse")
def market_pulse_data():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    force = request.args.get("refresh") == "1"
    snapshot_only = request.args.get("snapshot") == "1"

    if snapshot_only and not force:
        data = snapshot_store.serve_snapshot("live:market_pulse")
        if data is None:
            return jsonify({"snapshot_missing": True})
        return jsonify(data)

    if force:
        from application.services import cache as shared_cache
        shared_cache.jdelete("optionchain:nse:breadth_movers:v1")
        shared_cache.jdelete("optionchain:nse:corp_ref:v1")
    breadth = oc_service._fetch_market_breadth_movers()
    corp = oc_service._fetch_corporate_reference()
    payload = {
        "market_breadth_movers": breadth or {},
        "corporate_reference": corp or {},
    }
    try:
        snapshot_store.put("live:market_pulse", payload)
    except Exception:
        pass
    return jsonify(payload)


_SPARK_RE = re.compile(r"^[A-Z0-9^][A-Z0-9._&-]{0,23}$")
_SPARK_TTL = 5 * 60


def _spark_for(symbol: str) -> tuple[str, dict]:
    """Build the 22-session trend and volume multiple for one mover."""
    yahoo = symbol if symbol.startswith("^") or "." in symbol else symbol + ".NS"
    try:
        hist = market_data.get_history(yahoo, days=30, interval="1d")
        if hist is None or len(hist) < 2:
            return symbol, {}
        hist = hist.dropna(subset=["Close", "Volume"])
        closes = [round(float(value), 2) for value in hist["Close"].tail(22)]
        volumes = hist["Volume"]
        latest = float(volumes.iloc[-1])
        baseline = volumes.iloc[-21:-1] if len(volumes) >= 21 else volumes.iloc[:-1]
        average = float(baseline.mean()) if len(baseline) else 0.0
        multiple = round(latest / average, 2) if average else None
        return symbol, {"spark": closes, "volume_multiple": multiple}
    except Exception:  # noqa: BLE001
        return symbol, {}


@market_pulse_api.route("/api/market-pulse/sparklines")
def market_pulse_sparklines():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    raw = (request.args.get("symbols") or "")[:600]
    symbols = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if symbol and _SPARK_RE.match(symbol) and symbol not in symbols:
            symbols.append(symbol)
    symbols = symbols[:24]
    if not symbols:
        return jsonify({"symbols": {}})

    cache_key = "marketpulse:sparks:v1:" + hashlib.md5(
        ",".join(sorted(symbols)).encode("utf-8")
    ).hexdigest()
    cached = shared_cache.jget(cache_key)
    if isinstance(cached, dict):
        return jsonify({**cached, "cached": True})

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        for symbol, payload in executor.map(_spark_for, symbols):
            if payload:
                out[symbol] = payload
    result = {"symbols": out, "cached": False}
    try:
        shared_cache.jset(cache_key, result, ttl=_SPARK_TTL)
    except Exception:  # noqa: BLE001
        pass
    return jsonify(result)
