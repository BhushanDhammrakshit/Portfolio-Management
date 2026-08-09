"""Investing tool routes."""
from __future__ import annotations

import traceback

from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for

from application.services import investing_tools
from application.services.azure_table import stocks_table_client
from azure.core.exceptions import ResourceNotFoundError

investing_tools_api = Blueprint("investing_tools_api", __name__)

from application.services.event_tracker import track_feature


def _auth_ok():
    return "email" in session


def _force_flag():
    return (request.args.get("refresh") == "1") or \
           ((request.get_json(silent=True) or {}).get("refresh") is True)


def _normalize_symbol(s: str) -> str:
    s = (s or "").strip().upper()
    if not s:
        return ""
    if s.startswith("^"):
        return s
    if "." in s:
        return s
    if ":" in s:
        exch, rest = s.split(":", 1)
        base = rest.split("-", 1)[0]
        return f"{base}.{'BO' if exch == 'BSE' else 'NS'}"
    return s + ".NS"


def _user_holdings():
    uid = session.get("user_id")
    if not uid:
        return []
    try:
        items = list(stocks_table_client.query_entities(query_filter=f"UserId eq '{uid}'"))
    except ResourceNotFoundError:
        return []
    except Exception:
        return []
    out = []
    for s in items:
        def _v(x):
            return x.value if hasattr(x, "value") else x
        out.append({
            "symbol": _v(s.get("Symbol")) or _v(s.get("StockName")),
            "quantity": _v(s.get("Quantity")),
            "purchase_price": _v(s.get("PurchasePrice")),
            "current_price": _v(s.get("CurrentPrice")),
        })
    return [h for h in out if h["symbol"]]


@investing_tools_api.route("/investing-tools")
@track_feature("investing_tools")
def investing_tools_page():
    if not _auth_ok():
        return redirect(url_for("logIn"))
    return render_template(
        "investingTools.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="Investing Tools",
    )


@investing_tools_api.route("/api/investing-tools/screener", methods=["GET", "POST"])
def api_screener():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    strategy = (request.args.get("strategy")
                or (request.get_json(silent=True) or {}).get("strategy")
                or "magic_formula")
    try:
        return jsonify(investing_tools.screener(strategy=strategy, force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/dcf", methods=["POST"])
def api_dcf():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    symbol = _normalize_symbol(body.get("symbol") or "")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(investing_tools.dcf_value(
            symbol,
            growth_pct=float(body.get("growth_pct", 12)),
            terminal_pct=float(body.get("terminal_pct", 4)),
            discount_pct=float(body.get("discount_pct", 11)),
            years=int(body.get("years", 10)),
        ))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/peers", methods=["GET"])
def api_peers():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    symbol = _normalize_symbol(request.args.get("symbol") or "")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(investing_tools.peer_comparison(symbol))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/earnings", methods=["GET", "POST"])
def api_earnings():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    try:
        return jsonify(investing_tools.earnings_calendar(force=_force_flag()))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/shareholding", methods=["GET"])
def api_shareholding():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    symbol = _normalize_symbol(request.args.get("symbol") or "")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(investing_tools.shareholding_pattern(symbol))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/corporate-actions", methods=["POST"])
def api_corp_actions():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols")
    if not symbols:
        symbols = [h["symbol"] for h in _user_holdings()]
    symbols = [_normalize_symbol(s) for s in symbols if s]
    if not symbols:
        return jsonify({"stocks": [], "scan_time": "—"})
    try:
        return jsonify(investing_tools.corporate_actions(symbols[:50]))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/rag-qa", methods=["POST"])
def api_rag_qa():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    symbol = _normalize_symbol(body.get("symbol") or "")
    question = (body.get("question") or "").strip()
    if not symbol or not question:
        return jsonify({"error": "symbol and question required"}), 400
    try:
        return jsonify(investing_tools.annual_report_qa(symbol, question))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/concall-sentiment", methods=["GET"])
def api_concall():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    symbol = _normalize_symbol(request.args.get("symbol") or "")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(investing_tools.concall_sentiment(symbol))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/moat", methods=["GET"])
def api_moat():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    symbol = _normalize_symbol(request.args.get("symbol") or "")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(investing_tools.moat_score(symbol))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/moat-scan", methods=["GET"])
def api_moat_scan():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    limit = request.args.get("limit", default=10, type=int) or 10
    force = (request.args.get("refresh") or "") == "1"
    try:
        return jsonify(investing_tools.scan_moat_scores(limit, force=force))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/portfolio-health", methods=["GET", "POST"])
def api_portfolio_health():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    holdings = body.get("holdings") or _user_holdings()
    try:
        return jsonify(investing_tools.portfolio_health(holdings))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/insider", methods=["GET"])
def api_insider():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    symbol = _normalize_symbol(request.args.get("symbol") or "")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(investing_tools.insider_transactions(symbol))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500


@investing_tools_api.route("/api/investing-tools/sip", methods=["POST"])
def api_sip():
    if not _auth_ok():
        return jsonify({"error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(investing_tools.sip_simulator(
            monthly_amount=float(body.get("monthly_amount", 10000)),
            expected_return_pct=float(body.get("expected_return_pct", 12)),
            years=int(body.get("years", 10)),
            step_up_pct=float(body.get("step_up_pct", 0)),
        ))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "failed", "detail": str(e)}), 500
