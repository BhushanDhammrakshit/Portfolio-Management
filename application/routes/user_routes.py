"""Legacy user profile API kept for backward compatibility."""
from flask import Blueprint, jsonify, request, session, redirect, url_for

from application.services.azure_table import (get_user_by_credentials,
                                              get_user_stocks_by_row_key)
from application.services import precompute

user_blueprint = Blueprint("user", __name__)


@user_blueprint.route("/api/portfolio")
def api_portfolio():
    """Fast read-only endpoint: returns the user's precomputed portfolio
    payload from Redis. Frontend polls this every few seconds.

    Cold path: if there's nothing cached yet (first hit after login or
    after an invalidation), compute synchronously once so the first
    response isn't empty.
    """
    if "user_id" not in session:
        return jsonify({"error": "auth"}), 401
    uid = session["user_id"]
    data = precompute.get_user_portfolio(uid)
    if data is None:
        data = precompute.refresh_user(uid) or {"portfolio": [], "summary": {}}
    return jsonify(data)


@user_blueprint.route("/user/profile", methods=["GET", "POST"])
def get_user_profile_and_stocks():
    """JSON profile endpoint. POST with email+password OR GET with active session."""
    try:
        if request.method == "POST":
            email = request.form.get("email")
            password = request.form.get("password")
            user_entity = get_user_by_credentials(email, password)
            if not user_entity:
                return jsonify({"error": "Invalid credentials"}), 401
        else:
            if "email" not in session:
                return redirect(url_for("logIn"))
            from application.services.azure_table import user_table_client
            users = list(user_table_client.query_entities(
                query_filter=f"Email eq '{session['email']}'"))
            user_entity = users[0] if users else None
            if not user_entity:
                return jsonify({"error": "User not found"}), 404

        user_stocks = get_user_stocks_by_row_key(user_entity.get("RowKey"))
        stocks = []
        for stock in user_stocks:
            stocks.append({
                "StockName": stock.get("StockName"),
                "Quantity": stock.get("Quantity"),
                "PurchasePrice": stock.get("PurchasePrice"),
                "PurchaseDate": stock.get("PurchaseDate"),
                "Exchange": stock.get("Exchange"),
                "Sector": stock.get("Sector"),
            })
        return jsonify({
            "user": {
                "name": user_entity.get("UserName"),
                "email": user_entity.get("Email"),
                "phone": user_entity.get("ContactNo"),
                "gender": user_entity.get("Gender"),
                "location": user_entity.get("Location"),
            },
            "stocks": stocks,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Feedback / Contact ──────────────────────────────────────────────────
_FEEDBACK_TO = "financecandleservice@gmail.com"
_FEEDBACK_COOLDOWN = 60


@user_blueprint.route("/api/feedback", methods=["POST"])
def submit_feedback():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401

    from application.services import cache as shared_cache
    from application.services import email_service

    data = request.get_json(silent=True) or {}
    category = (data.get("category") or "general").strip()[:50]
    message = (data.get("message") or "").strip()
    if not message or len(message) < 5:
        return jsonify({"error": "Message too short."}), 400
    if len(message) > 5000:
        return jsonify({"error": "Message too long (max 5000 chars)."}), 400

    cooldown_key = f"feedback:cooldown:{session['email']}"
    if shared_cache.jget(cooldown_key):
        return jsonify({"error": "Please wait a minute before sending again."}), 429
    shared_cache.jset(cooldown_key, 1, ttl=_FEEDBACK_COOLDOWN)

    user_name = session.get("name", "Unknown")
    user_email = session.get("email", "unknown")
    subject = f"[FinanceCandle Feedback] {category} \u2014 from {user_name}"

    import datetime as _dt
    from application.services import email_templates

    sent_at = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30))) \
        .strftime("%d %b %Y, %I:%M %p IST")
    html = email_templates.admin_feedback_html(
        user_name, user_email, category, message, sent_at)

    ok, info = email_service.send_email(
        to=_FEEDBACK_TO,
        subject=subject,
        html=html,
        text=f"[{category}] Feedback from {user_name} <{user_email}> at {sent_at}\n\n{message}",
    )
    if ok:
        return jsonify({"ok": True, "message": "Feedback sent! Thank you."})
    return jsonify({"error": "Failed to send. Please try again later."}), 500
