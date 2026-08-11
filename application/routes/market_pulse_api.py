"""Market Pulse — NSE-sourced market breadth, movers and corporate data."""
from flask import Blueprint, jsonify, render_template, request, redirect, session, url_for

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
