"""F&O single-stock next-day gap forecast — page + JSON endpoints."""
from __future__ import annotations

import traceback

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from application.services import fno_gap_forecast as fno_svc
from application.services.plans import requires_plan

fno_gap_api = Blueprint("fno_gap_api", __name__)


def _auth_ok() -> bool:
    return "email" in session


def _force_flag() -> bool:
    return request.args.get("refresh") == "1"


@fno_gap_api.route("/fno-gap-forecast")
def fno_gap_page():
    if not _auth_ok():
        return redirect(url_for("logIn"))
    return render_template(
        "fnoGapForecast.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="F&O Gap Forecast",
    )


@fno_gap_api.route("/fno-gap-forecast/summary")
def fno_gap_summary_page():
    if not _auth_ok():
        return redirect(url_for("logIn"))
    return render_template(
        "fnoGapSummary.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="F&O Forecast Summary",
    )


@fno_gap_api.route("/api/fno-gap-forecast/list")
@requires_plan("elite")
def api_fno_list():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(fno_svc.get_forecast_list(force_refresh=_force_flag()))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"error": "list_failed", "detail": str(e)}), 500


@fno_gap_api.route("/api/fno-gap-forecast/detail")
@requires_plan("elite")
def api_fno_detail():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "missing_symbol"}), 400
    try:
        data = fno_svc.get_forecast_detail(symbol, force_refresh=_force_flag())
        return jsonify(data), 200
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"error": "detail_failed", "detail": str(e)}), 500


@fno_gap_api.route("/api/fno-gap-forecast/summary")
@requires_plan("elite")
def api_fno_summary():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(fno_svc.get_accuracy_summary(force_refresh=_force_flag()))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"error": "summary_failed", "detail": str(e)}), 500
