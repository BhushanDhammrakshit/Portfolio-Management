"""Swing tool routes."""
from __future__ import annotations

import traceback

from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for

from application.services import swing_tools
from application.services.plans import requires_plan
from application.services.event_tracker import track_feature

swing_tools_api = Blueprint("swing_tools_api", __name__)


def _auth_ok():
    return "email" in session


@swing_tools_api.route("/swing-tools")
@track_feature("swing_tools")
def swing_tools_page():
    if not _auth_ok():
        return redirect(url_for("logIn"))
    return render_template(
        "swingTools.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="Swing Tools",
    )


def _force_flag():
    return (request.args.get("refresh") == "1") or \
           ((request.get_json(silent=True) or {}).get("refresh") is True)


@swing_tools_api.route("/api/swing-tools/breakouts", methods=["GET", "POST"])
@requires_plan("pro")
def api_breakouts():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(swing_tools.breakout_consolidation(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/relative-strength", methods=["GET", "POST"])
@requires_plan("pro")
def api_rs():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(swing_tools.relative_strength(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/patterns", methods=["GET", "POST"])
@requires_plan("pro")
def api_patterns():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(swing_tools.chart_patterns(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/sector-leaders", methods=["GET", "POST"])
def api_sector_leaders():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(swing_tools.sector_leaders(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/sector-rotation", methods=["GET", "POST"])
@requires_plan("pro")
def api_sector_rotation():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        body = request.get_json(silent=True) or {}
        tail = body.get("tail") or request.args.get("tail") or 7
        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = 7
        return jsonify(swing_tools.sector_rotation(force=_force_flag(), tail=tail))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/rrg")
def rrg_page():
    if not _auth_ok():
        return redirect(url_for("logIn"))
    return render_template(
        "rrgRotation.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="RRG Rotation",
    )


@swing_tools_api.route("/api/rrg/rotation", methods=["GET", "POST"])
@requires_plan("pro")
def api_rrg_rotation():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        body = request.get_json(silent=True) or {}
        benchmark = body.get("benchmark") or request.args.get("benchmark") or "nifty"
        timeframe = body.get("timeframe") or request.args.get("timeframe") or "weekly"
        tail = body.get("tail") or request.args.get("tail") or 7
        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = 7
        return jsonify(swing_tools.rrg_rotation(
            benchmark=benchmark, timeframe=timeframe, tail=tail, force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/options-confirmed", methods=["GET", "POST"])
def api_options_confirmed():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(swing_tools.options_confirmed_swing(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/fii-dii", methods=["GET", "POST"])
def api_fii_dii():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(swing_tools.fii_dii_overlay(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/mtf-alignment", methods=["GET", "POST"])
def api_mtf():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(swing_tools.mtf_alignment(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/near-52wh", methods=["GET", "POST"])
@requires_plan("pro")
def api_52wh():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        mp = float(request.args.get("max_proximity", 5.0))
        return jsonify(swing_tools.near_52wh(max_proximity=mp, force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/pocket-pivot", methods=["GET", "POST"])
def api_pocket_pivot():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(swing_tools.pocket_pivot(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@swing_tools_api.route("/api/swing-tools/backtest", methods=["POST"])
def api_backtest():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        data = request.get_json(force=True) or {}
        sym = (data.get("symbol") or "").strip().upper()
        if not sym:
            return jsonify({"error": "symbol required"}), 400
        if "." not in sym and not sym.startswith("^"):
            sym += ".NS"
        return jsonify(swing_tools.swing_backtest(
            symbol=sym,
            ema_fast=int(data.get("ema_fast", 20)),
            ema_slow=int(data.get("ema_slow", 50)),
            rsi_threshold=float(data.get("rsi_threshold", 55)),
            vol_multiplier=float(data.get("vol_multiplier", 1.2)),
            years=int(data.get("years", 3)),
        ))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "backtest_failed", "detail": str(e)}), 500
