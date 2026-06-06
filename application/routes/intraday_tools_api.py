"""Intraday tool routes — page + 6 JSON endpoints."""
from __future__ import annotations

import traceback

from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for

from application.services import intraday_tools

intraday_tools_api = Blueprint("intraday_tools_api", __name__)


def _auth_ok():
    return "email" in session


@intraday_tools_api.route("/intraday-tools")
def intraday_tools_page():
    if not _auth_ok():
        return redirect(url_for("logIn"))
    return render_template(
        "intradayTools.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="Intraday Tools",
    )


def _force_flag():
    return (request.args.get("refresh") == "1") or \
           ((request.get_json(silent=True) or {}).get("refresh") is True)


@intraday_tools_api.route("/api/intraday-tools/orb", methods=["GET", "POST"])
def api_orb():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        minutes = int(request.args.get("minutes", 15))
        if minutes not in (15, 30, 60):
            minutes = 15
        return jsonify(intraday_tools.orb_scan(orb_minutes=minutes, force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "orb_failed", "detail": str(e)}), 500


@intraday_tools_api.route("/api/intraday-tools/rvol", methods=["GET", "POST"])
def api_rvol():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(intraday_tools.rvol_heatmap(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "rvol_failed", "detail": str(e)}), 500


@intraday_tools_api.route("/api/intraday-tools/gappers", methods=["GET", "POST"])
def api_gappers():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(intraday_tools.gappers_and_gap_fill(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "gappers_failed", "detail": str(e)}), 500


@intraday_tools_api.route("/api/intraday-tools/pivots", methods=["GET"])
def api_pivots():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    if "." not in symbol and not symbol.startswith("^"):
        symbol = symbol + ".NS"
    try:
        data = intraday_tools.pivot_levels(symbol)
        if not data:
            return jsonify({"error": "data unavailable for symbol", "symbol": symbol}), 404
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "pivots_failed", "detail": str(e)}), 500


@intraday_tools_api.route("/api/intraday-tools/momentum", methods=["GET", "POST"])
def api_momentum():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        lb = int(request.args.get("lookback", 30))
        if lb not in (15, 30, 60, 120):
            lb = 30
        return jsonify(intraday_tools.momentum_burst(lookback_min=lb, force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "momentum_failed", "detail": str(e)}), 500


@intraday_tools_api.route("/api/intraday-tools/basis", methods=["GET", "POST"])
def api_basis():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(intraday_tools.index_basis(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "basis_failed", "detail": str(e)}), 500


@intraday_tools_api.route("/api/intraday-tools/vwap", methods=["GET", "POST"])
def api_vwap():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        mn = float(request.args.get("min_dev", 2.0))
        return jsonify(intraday_tools.vwap_deviation_scan(min_abs_dev=mn, force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "vwap_failed", "detail": str(e)}), 500


@intraday_tools_api.route("/api/intraday-tools/sector-rotation", methods=["GET", "POST"])
def api_sector_rotation():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(intraday_tools.sector_rotation(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "sector_rotation_failed", "detail": str(e)}), 500


@intraday_tools_api.route("/api/intraday-tools/news-sentiment", methods=["GET", "POST"])
def api_news_sentiment():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(intraday_tools.news_sentiment(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "news_sentiment_failed", "detail": str(e)}), 500
