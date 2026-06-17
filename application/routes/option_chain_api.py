"""Option-chain page + JSON endpoint (NIFTY only)."""
from flask import Blueprint, jsonify, render_template, request, redirect, session, url_for

from application.services import option_chain as oc_service
from application.services import gap_history

option_chain_api = Blueprint("option_chain_api", __name__)


@option_chain_api.route("/option-chain")
def option_chain_page():
    if "email" not in session:
        return redirect(url_for("logIn"))
    return render_template(
        "optionChain.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="Options Analytics",
    )


@option_chain_api.route("/api/option-chain/nifty")
def nifty_option_chain():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    force = request.args.get("refresh") == "1"
    data = oc_service.get_nifty_option_chain(force_refresh=force)
    if "error" in data:
        # 200 so the UI can render the detail message instead of generic
        # "failed to fetch". Detail field carries the upstream reason.
        return jsonify(data), 200
    return jsonify(data)


@option_chain_api.route("/api/option-chain/gap-history")
def nifty_gap_history():
    """Return persisted gap-up/gap-down signals + next-day outcomes."""
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    try:
        limit = max(1, min(60, int(request.args.get("limit") or 14)))
    except (TypeError, ValueError):
        limit = 14
    # Opportunistic re-evaluation — throttled internally to 1/hour/process.
    try:
        gap_history.evaluate_pending("NIFTY")
    except Exception:
        pass
    return jsonify({
        "symbol": "NIFTY",
        "items": gap_history.recent("NIFTY", limit=limit),
        "stats": gap_history.stats("NIFTY", lookback=60),
    })
