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