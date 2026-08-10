"""Mutual Funds feature routes."""
from __future__ import annotations

import traceback

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from application.services import mf_advisor, mf_data, mf_portfolio, plans
from application.services.azure_table import stocks_table_client
from application.services import ai_client

mutual_funds_api = Blueprint("mutual_funds_api", __name__)

from application.services.event_tracker import track_feature


def _auth_ok() -> bool:
    return "email" in session and bool(session.get("user_id"))


def _err(msg: str, status: int = 400, **extra):
    body = {"error": msg, **extra}
    return jsonify(body), status


# ── Page ────────────────────────────────────────────────────────────────
@mutual_funds_api.route("/mutual-funds")
@track_feature("mutual_funds")
def mutual_funds_page():
    if "email" not in session:
        return redirect(url_for("logIn"))
    return render_template(
        "mutualFunds.html",
        name=session.get("name", "User"),
        email=session.get("email", ""),
        title="Mutual Funds",
    )


# ── Discovery: search, scheme detail, returns, NAV history ──────────────
@mutual_funds_api.route("/api/mutual-funds/search")
def api_search():
    if not _auth_ok():
        return _err("auth", 401)
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 25) or 25), 100)
    try:
        return jsonify({"query": q, "results": mf_data.search_schemes(q, limit)})
    except Exception as e:
        traceback.print_exc()
        return _err("search_failed", 500, detail=str(e))


@mutual_funds_api.route("/api/mutual-funds/scheme/<code>")
def api_scheme(code):
    if not _auth_ok():
        return _err("auth", 401)
    try:
        scheme = mf_data.get_scheme(code)
        if not scheme.get("meta"):
            return _err("not_found", 404)
        return jsonify({
            "meta": scheme.get("meta"),
            "returns": mf_data.returns_summary(code),
            "analytics": mf_data.scheme_analytics(code),
            "nav_history": mf_data.nav_history(code, days=int(request.args.get("days", 365))),
            "holdings": mf_data.get_holdings(code),
        })
    except Exception as e:
        traceback.print_exc()
        return _err("failed", 500, detail=str(e))


@mutual_funds_api.route("/api/mutual-funds/nav/<code>")
def api_nav(code):
    if not _auth_ok():
        return _err("auth", 401)
    return jsonify({"code": code, "nav": mf_data.latest_nav(code)})


# ── Portfolio CRUD ──────────────────────────────────────────────────────
@mutual_funds_api.route("/api/mutual-funds/portfolio")
def api_portfolio():
    if not _auth_ok():
        return _err("auth", 401)
    try:
        return jsonify(mf_portfolio.portfolio_summary(session["user_id"]))
    except Exception as e:
        traceback.print_exc()
        return _err("portfolio_failed", 500, detail=str(e))


@mutual_funds_api.route("/api/mutual-funds/holding", methods=["POST"])
def api_add_holding():
    if not _auth_ok():
        return _err("auth", 401)
    body = request.get_json(silent=True) or {}
    try:
        h = mf_portfolio.add_holding(
            user_id=session["user_id"],
            scheme_code=str(body.get("scheme_code") or "").strip(),
            units=float(body.get("units") or 0),
            nav_at_purchase=float(body.get("nav_at_purchase") or 0),
            purchase_date=str(body.get("purchase_date") or "").strip(),
            sip_monthly=float(body.get("sip_monthly") or 0),
            folio_number=str(body.get("folio_number") or "").strip(),
            scheme_name=str(body.get("scheme_name") or "").strip() or None,
        )
        return jsonify({"ok": True, "holding": h})
    except ValueError as e:
        return _err(str(e), 400)
    except Exception as e:
        traceback.print_exc()
        return _err("add_failed", 500, detail=str(e))


@mutual_funds_api.route("/api/mutual-funds/holding/<hid>", methods=["PUT", "DELETE"])
def api_holding_modify(hid):
    if not _auth_ok():
        return _err("auth", 401)
    if request.method == "DELETE":
        try:
            ok = mf_portfolio.delete_holding(session["user_id"], hid)
        except Exception as e:
            traceback.print_exc()
            return _err("delete_failed", 500, detail=str(e))
        if not ok:
            return _err("not_found", 404)
        return jsonify({"ok": True})
    body = request.get_json(silent=True) or {}
    try:
        updated = mf_portfolio.update_holding(session["user_id"], hid, **body)
    except Exception as e:
        traceback.print_exc()
        return _err("update_failed", 500, detail=str(e))
    if not updated:
        return _err("not_found", 404)
    return jsonify({"ok": True, "holding": updated})


# ── Compare / overlap ──────────────────────────────────────────────────
@mutual_funds_api.route("/api/mutual-funds/compare", methods=["POST"])
def api_compare():
    if not _auth_ok():
        return _err("auth", 401)
    body = request.get_json(silent=True) or {}
    codes = body.get("scheme_codes") or []
    if not isinstance(codes, list) or len(codes) < 2:
        return _err("provide at least 2 scheme_codes", 400)
    try:
        return jsonify(mf_data.compare_funds(codes))
    except Exception as e:
        traceback.print_exc()
        return _err("compare_failed", 500, detail=str(e))


@mutual_funds_api.route("/api/mutual-funds/stock-overlap")
def api_stock_overlap():
    """Find stocks held both directly + inside the user's MFs."""
    if not _auth_ok():
        return _err("auth", 401)
    try:
        return jsonify(mf_portfolio.stock_overlap_with_portfolio(
            session["user_id"], stocks_table_client))
    except Exception as e:
        traceback.print_exc()
        return _err("overlap_failed", 500, detail=str(e))


# ── Holdings management (for funds we don't have seeded data on) ────────
@mutual_funds_api.route("/api/mutual-funds/holdings/known")
def api_known_holdings():
    if not _auth_ok():
        return _err("auth", 401)
    return jsonify({"codes": mf_data.known_holdings_codes()})


@mutual_funds_api.route("/api/mutual-funds/holdings/upload", methods=["POST"])
def api_upload_holdings():
    """Accept a CSV upload of holdings for a single scheme.

    Form fields: scheme_code, file (CSV with symbol,weight_pct[,sector])
    OR JSON: { scheme_code, holdings: [{symbol,weight_pct,sector}] }
    """
    if not _auth_ok():
        return _err("auth", 401)
    if request.is_json:
        body = request.get_json(silent=True) or {}
        code = str(body.get("scheme_code") or "").strip()
        rows = body.get("holdings") or []
        asof = body.get("asof")
    else:
        code = (request.form.get("scheme_code") or "").strip()
        asof = request.form.get("asof")
        f = request.files.get("file")
        text = f.read().decode("utf-8", errors="ignore") if f else ""
        rows = mf_data.parse_holdings_csv(text)
    if not code or not rows:
        return _err("scheme_code and holdings required", 400)
    scheme = mf_data.get_scheme(code)
    name = (scheme.get("meta") or {}).get("scheme_name", "")
    try:
        entry = mf_data.set_holdings(code, name, rows, asof=asof)
        return jsonify({"ok": True, "saved": entry})
    except Exception as e:
        traceback.print_exc()
        return _err("save_failed", 500, detail=str(e))


# ── Calculators ─────────────────────────────────────────────────────────
@mutual_funds_api.route("/api/mutual-funds/calc/sip", methods=["POST"])
def api_calc_sip():
    if not _auth_ok():
        return _err("auth", 401)
    b = request.get_json(silent=True) or {}
    try:
        return jsonify(mf_portfolio.sip_future_value(
            monthly=float(b.get("monthly") or 0),
            years=float(b.get("years") or 0),
            annual_return_pct=float(b.get("annual_return_pct") or 12),
            step_up_pct=float(b.get("step_up_pct") or 0),
        ))
    except Exception as e:
        return _err("calc_failed", 400, detail=str(e))


@mutual_funds_api.route("/api/mutual-funds/calc/lumpsum", methods=["POST"])
def api_calc_lumpsum():
    if not _auth_ok():
        return _err("auth", 401)
    b = request.get_json(silent=True) or {}
    try:
        return jsonify(mf_portfolio.lumpsum_future_value(
            amount=float(b.get("amount") or 0),
            years=float(b.get("years") or 0),
            annual_return_pct=float(b.get("annual_return_pct") or 12),
        ))
    except Exception as e:
        return _err("calc_failed", 400, detail=str(e))


@mutual_funds_api.route("/api/mutual-funds/calc/goal", methods=["POST"])
def api_calc_goal():
    if not _auth_ok():
        return _err("auth", 401)
    b = request.get_json(silent=True) or {}
    try:
        return jsonify(mf_portfolio.goal_sip(
            target_amount=float(b.get("target") or 0),
            years=float(b.get("years") or 0),
            annual_return_pct=float(b.get("annual_return_pct") or 12),
        ))
    except Exception as e:
        return _err("calc_failed", 400, detail=str(e))


# ── AI advisory ─────────────────────────────────────────────────────────
@mutual_funds_api.route("/api/mutual-funds/ai/analyze", methods=["POST"])
def api_ai_analyze():
    if not _auth_ok():
        return _err("auth", 401)
    uid = session["user_id"]
    summary = mf_portfolio.portfolio_summary(uid)
    if not summary.get("holdings"):
        return jsonify({"analysis": None,
                        "message": "Add at least one MF holding from the Portfolio tab before running AI analysis."})
    try:
        overlap = mf_portfolio.stock_overlap_with_portfolio(
            uid, stocks_table_client, holdings=summary.get("holdings"))
    except Exception:
        traceback.print_exc()
        overlap = {"overlaps": []}
    result, err = mf_advisor.analyze_portfolio(summary, overlap)
    if err:
        return _err("ai_failed", 500, detail=err)
    return jsonify({"analysis": result, "overlap_count": len(overlap.get("overlaps", []))})


@mutual_funds_api.route("/api/mutual-funds/ai/goal", methods=["POST"])
def api_ai_goal():
    if not _auth_ok():
        return _err("auth", 401)
    b = request.get_json(silent=True) or {}
    result, err = mf_advisor.recommend_for_goal(
        goal=str(b.get("goal") or "wealth creation"),
        horizon_years=float(b.get("years") or 10),
        risk=str(b.get("risk") or "moderate"),
        monthly_amount=float(b.get("monthly") or 5000),
    )
    if err:
        return _err("ai_failed", 500, detail=err)
    return jsonify({"recommendation": result})


@mutual_funds_api.route("/api/mutual-funds/ai/explain/<code>", methods=["POST"])
def api_ai_explain(code):
    """Short AI narrative for a single scheme using its returns + analytics."""
    if not _auth_ok():
        return _err("auth", 401)
    try:
        scheme = mf_data.get_scheme(code)
        meta = scheme.get("meta") or {}
        if not meta:
            return _err("not_found", 404)
        ret = mf_data.returns_summary(code)
        ana = mf_data.scheme_analytics(code)
        hd = mf_data.get_holdings(code) or {}
        top_holdings = [h.get("symbol") for h in (hd.get("top") or [])[:8]]
        ctx = {
            "name": meta.get("scheme_name"),
            "category": meta.get("scheme_category"),
            "fund_house": meta.get("fund_house"),
            "returns": ret,
            "analytics": ana,
            "top_holdings": top_holdings,
        }
        import json as _json
        msgs = [
            {"role": "system", "content":
                "You are an Indian mutual-fund analyst. Reply with STRICT JSON only "
                "(no prose, no ``` fences) of shape: "
                "{verdict, suitability:[strings], pros:[strings], cons:[strings], "
                "ideal_for, holding_period_years, key_risk}. Keep each bullet under 18 words. "
                "Do NOT give personalised financial advice; describe characteristics only."},
            {"role": "user", "content":
                "Analyse this scheme and respond in JSON:\n" + _json.dumps(ctx, default=str)},
        ]
        text, err = ai_client.chat(msgs, temperature=0.4, max_tokens=600, timeout=45)
        if err or not text:
            return _err("ai_failed", 500, detail=err or "empty")
        t = text.strip()
        if t.startswith("```"):
            t = t.split("```", 2)[1]
            if t.lower().startswith("json"):
                t = t[4:]
            t = t.strip("` \n")
        try:
            parsed = _json.loads(t)
        except Exception:
            parsed = {"verdict": text.strip()}
        return jsonify({"analysis": parsed})
    except Exception as e:
        traceback.print_exc()
        return _err("failed", 500, detail=str(e))
