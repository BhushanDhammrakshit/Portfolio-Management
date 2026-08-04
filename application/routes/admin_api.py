"""Admin portal — users, subscriptions, usage, system health."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Blueprint, jsonify, render_template, request, session)

from application.services.azure_table import user_table_client
from application.services import plans

admin_bp = Blueprint("admin_portal", __name__)

_ADMIN_EMAILS = {
    e.strip().lower()
    for e in (os.getenv("ADMIN_EMAILS") or "").split(",")
    if e.strip()
}


def _is_admin(email: str) -> bool:
    if not email:
        return False
    if not _ADMIN_EMAILS:
        return True
    return email.lower() in _ADMIN_EMAILS


def _require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "email" not in session:
            return jsonify({"error": "auth required"}), 401
        if not _is_admin(session.get("email", "")):
            return jsonify({"error": "admin required"}), 403
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


def _days_since(iso_str: str):
    """Days since an ISO timestamp, or None if unparseable/blank."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).replace(tzinfo=None)
        return (datetime.utcnow() - dt).days
    except (TypeError, ValueError):
        return None


# ── Page ────────────────────────────────────────────────────────────────

@admin_bp.route("/admin")
@_require_admin
def admin_page():
    return render_template(
        "admin.html",
        name=session.get("name"), email=session.get("email"),
        title="Admin Portal",
    )


# ── Users API ───────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/users")
@_require_admin
def list_users():
    try:
        users_raw = list(user_table_client.query_entities(
            query_filter="PartitionKey eq 'user'"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    now = date.today()
    users = []
    for u in users_raw:
        plan_id = (u.get("Plan") or "free").lower()
        expires = u.get("PlanExpiresOn") or ""
        active = False
        if plan_id != "free" and expires:
            try:
                active = datetime.fromisoformat(expires).date() >= now
            except (TypeError, ValueError):
                pass
        if plan_id != "free" and not active:
            plan_id = "free"

        last_login = u.get("LastLoginOn") or ""
        days_since_login = _days_since(last_login)

        users.append({
            "id": u.get("RowKey", ""),
            "name": u.get("UserName", ""),
            "email": u.get("Email", ""),
            "phone": u.get("ContactNo", ""),
            "gender": u.get("Gender", ""),
            "location": u.get("Location", ""),
            "plan": plan_id,
            "plan_expires": expires,
            "email_verified": bool(u.get("EmailVerified")),
            "terms_accepted": bool(u.get("TermsAccepted")),
            "trial_used": bool(u.get("TrialUsed")),
            "on_trial": plans.is_on_trial(u),
            "trial_days": plans.trial_days_remaining(u),
            "persona": u.get("Persona", ""),
            "referral_code": u.get("ReferralCode", ""),
            "created": u.get("TermsAcceptedOn") or u.get("Timestamp", ""),
            "last_login": last_login,
            "active_7d": days_since_login is not None and days_since_login <= 7,
            "active_30d": days_since_login is not None and days_since_login <= 30,
        })
    return jsonify({"users": users})


@admin_bp.route("/api/admin/users/<user_id>/plan", methods=["POST"])
@_require_admin
def change_user_plan(user_id):
    data = request.get_json(silent=True) or {}
    new_plan = (data.get("plan") or "").lower()
    months = int(data.get("months") or 1)
    if new_plan not in plans.PLANS:
        return jsonify({"error": "Invalid plan"}), 400

    try:
        results = list(user_table_client.query_entities(
            query_filter=f"RowKey eq '{user_id}'"))
        if not results:
            return jsonify({"error": "User not found"}), 404
        user = results[0]
        plans.set_user_plan(user, new_plan, months)
        return jsonify({"ok": True, "plan": new_plan})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/users/<user_id>/verify-email", methods=["POST"])
@_require_admin
def admin_verify_email(user_id):
    from azure.data.tables import UpdateMode
    try:
        results = list(user_table_client.query_entities(
            query_filter=f"RowKey eq '{user_id}'"))
        if not results:
            return jsonify({"error": "User not found"}), 404
        user = results[0]
        user["EmailVerified"] = True
        user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/users/<user_id>/delete", methods=["DELETE"])
@_require_admin
def admin_delete_user(user_id):
    try:
        results = list(user_table_client.query_entities(
            query_filter=f"RowKey eq '{user_id}'"))
        if not results:
            return jsonify({"error": "User not found"}), 404
        user = results[0]
        user_table_client.delete_entity(
            partition_key=user["PartitionKey"], row_key=user["RowKey"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Stats summary ───────────────────────────────────────────────────────

@admin_bp.route("/api/admin/stats")
@_require_admin
def admin_stats():
    try:
        users_raw = list(user_table_client.query_entities(
            query_filter="PartitionKey eq 'user'"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    now = date.today()
    total = len(users_raw)
    verified = 0
    by_plan = {"free": 0, "pro": 0, "elite": 0}
    on_trial = 0
    trial_expiring = 0
    today_signups = 0
    week_signups = 0
    active_today = 0
    active_7d = 0
    active_30d = 0

    for u in users_raw:
        if u.get("EmailVerified"):
            verified += 1

        days_since_login = _days_since(u.get("LastLoginOn") or "")
        if days_since_login is not None:
            if days_since_login <= 0:
                active_today += 1
            if days_since_login <= 7:
                active_7d += 1
            if days_since_login <= 30:
                active_30d += 1

        plan_id = (u.get("Plan") or "free").lower()
        expires = u.get("PlanExpiresOn") or ""
        active = False
        if plan_id != "free" and expires:
            try:
                exp_date = datetime.fromisoformat(expires).date()
                active = exp_date >= now
                if active and plans.is_on_trial(u):
                    on_trial += 1
                    if (exp_date - now).days <= 2:
                        trial_expiring += 1
            except (TypeError, ValueError):
                pass
        if plan_id != "free" and not active:
            plan_id = "free"
        by_plan[plan_id] = by_plan.get(plan_id, 0) + 1

        created = u.get("TermsAcceptedOn") or ""
        if created:
            try:
                cd = datetime.fromisoformat(created.replace("Z", "+00:00")).date()
                if cd == now:
                    today_signups += 1
                if (now - cd).days <= 7:
                    week_signups += 1
            except (TypeError, ValueError):
                pass

    return jsonify({
        "total_users": total,
        "verified": verified,
        "unverified": total - verified,
        "by_plan": by_plan,
        "on_trial": on_trial,
        "trial_expiring_soon": trial_expiring,
        "signups_today": today_signups,
        "signups_this_week": week_signups,
        "active_today": active_today,
        "active_7d": active_7d,
        "active_30d": active_30d,
    })
