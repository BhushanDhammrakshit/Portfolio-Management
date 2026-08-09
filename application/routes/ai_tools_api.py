"""Cross-cutting tools routes: Watchlist, Alerts, Strategy Builder, Idea of Day."""
from __future__ import annotations

import traceback

from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for

from application.services import ai_tools
from application.services.plans import requires_plan

ai_tools_api = Blueprint("ai_tools_api", __name__)

from application.services.event_tracker import track_feature


def _auth_ok():
    return "email" in session


def _email():
    return session.get("email", "")


@ai_tools_api.route("/ai-tools")
@track_feature("ai_tools")
def ai_tools_page():
    if not _auth_ok():
        return redirect(url_for("logIn"))
    return render_template(
        "aiTools.html",
        name=session.get("name", "User"),
        email=_email(),
        title="AI & Tools",
    )


# ───── Watchlist ─────
@ai_tools_api.route("/api/ai-tools/watchlist", methods=["GET"])
def api_watchlist_get():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(ai_tools.watchlist_quotes(_email()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@ai_tools_api.route("/api/ai-tools/watchlist", methods=["POST"])
def api_watchlist_add():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        data = request.get_json(force=True) or {}
        return jsonify(ai_tools.add_to_watchlist(
            _email(),
            symbol=data.get("symbol", ""),
            style=data.get("style", ""),
            note=data.get("note", ""),
        ))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@ai_tools_api.route("/api/ai-tools/watchlist/<item_id>", methods=["DELETE"])
def api_watchlist_delete(item_id):
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(ai_tools.remove_from_watchlist(_email(), item_id))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


# ───── Alerts ─────
@ai_tools_api.route("/api/ai-tools/alerts", methods=["GET"])
def api_alerts_get():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(ai_tools.evaluate_alerts(_email()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@ai_tools_api.route("/api/ai-tools/alerts", methods=["POST"])
def api_alerts_add():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        data = request.get_json(force=True) or {}
        return jsonify(ai_tools.add_alert(
            _email(),
            symbol=data.get("symbol", ""),
            alert_type=data.get("alert_type", ""),
            threshold=float(data.get("threshold", 0)),
            note=data.get("note", ""),
        ))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@ai_tools_api.route("/api/ai-tools/alerts/<item_id>", methods=["DELETE"])
def api_alerts_delete(item_id):
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(ai_tools.remove_alert(_email(), item_id))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@ai_tools_api.route("/api/ai-tools/alerts/<item_id>/ack", methods=["POST"])
def api_alerts_ack(item_id):
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(ai_tools.acknowledge_alert(_email(), item_id))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


# ───── Strategy Builder ─────
@ai_tools_api.route("/api/ai-tools/strategy", methods=["POST"])
def api_strategy_run():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        data = request.get_json(force=True) or {}
        rules = data.get("rules") or []
        if not isinstance(rules, list):
            return jsonify({"error": "rules must be a list"}), 400
        return jsonify(ai_tools.run_strategy(rules))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


# ───── Idea of the Day ─────
@ai_tools_api.route("/api/ai-tools/idea-of-day", methods=["GET", "POST"])
@requires_plan("pro")
def api_idea():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        style = (request.args.get("style") or "swing").lower()
        force = (request.args.get("refresh") == "1") or \
                ((request.get_json(silent=True) or {}).get("refresh") is True)
        return jsonify(ai_tools.idea_of_the_day(style=style, force=force))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500
