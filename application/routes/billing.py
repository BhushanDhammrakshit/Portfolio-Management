"""Subscription / billing routes.

For now this uses a *mock* checkout — clicking "Upgrade" instantly switches the
user's plan and sets a 30-day expiry. Replace ``mock_checkout`` with Razorpay
``orders.create`` + a webhook handler when payment integration is added.
"""
from flask import (Blueprint, render_template, session, redirect, url_for,
                   request, flash, jsonify)

from application.services import plans

billing_bp = Blueprint("billing", __name__)


def _login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "email" not in session or "user_id" not in session:
            return redirect(url_for("logIn"))
        return view(*args, **kwargs)
    return wrapped


@billing_bp.route("/billing")
@_login_required
def billing_page():
    user = plans.current_user_entity() or {}
    plan = plans.get_user_plan(user)
    usage = plans.get_usage(session["user_id"])

    # Usage rows for the meter
    limits = plan.get("limits", {})
    meters = []
    for key, label in [
        ("ai_single", "Single-stock AI analyses"),
        ("ai_bulk", "Bulk AI analyses"),
        ("ai_chat_daily", "AI Assistant chats today"),
    ]:
        used = usage.get(key, 0)
        limit = limits.get(key)
        pct = 0 if limit is None or limit == 0 else min(100, int(used / limit * 100))
        meters.append({
            "key": key,
            "label": label,
            "used": used,
            "limit": limit,
            "limit_display": "Unlimited" if limit is None else str(limit),
            "pct": pct,
            "danger": pct >= 90,
        })

    return render_template(
        "billing.html",
        title="Plans & Billing",
        name=session.get("name", ""),
        email=session.get("email", ""),
        plans=plans.PLANS,
        plan_order=["free", "pro", "elite"],
        current_plan=plan,
        plan_expires=user.get("PlanExpiresOn") or None,
        meters=meters,
    )


@billing_bp.route("/billing/upgrade", methods=["POST"])
@_login_required
def upgrade():
    """Mock upgrade: instantly switches plan. Replace with Razorpay flow."""
    plan_id = (request.form.get("plan") or "").lower()
    cycle = (request.form.get("cycle") or "monthly").lower()
    if plan_id not in plans.PLANS:
        flash("Unknown plan.", "danger")
        return redirect(url_for("billing.billing_page"))

    user = plans.current_user_entity()
    if not user:
        flash("Could not load your account.", "danger")
        return redirect(url_for("billing.billing_page"))

    months = 12 if cycle == "annual" else 1
    plans.set_user_plan(user, plan_id, months=months)

    if plan_id == "free":
        flash("You've been moved to the Free plan.", "info")
    else:
        flash(f"Welcome to {plans.PLANS[plan_id]['name']}! Your plan is active for "
              f"{months} month{'s' if months > 1 else ''}.", "success")
    return redirect(url_for("billing.billing_page"))


@billing_bp.route("/billing/cancel", methods=["POST"])
@_login_required
def cancel():
    user = plans.current_user_entity()
    if user:
        plans.set_user_plan(user, "free")
    flash("Your subscription has been cancelled. You're now on the Free plan.", "info")
    return redirect(url_for("billing.billing_page"))


# ── JSON endpoint used by other pages to show "X of Y used" ─────────────

@billing_bp.route("/api/billing/usage")
@_login_required
def usage_json():
    user = plans.current_user_entity()
    plan = plans.get_user_plan(user)
    usage = plans.get_usage(session["user_id"])
    return jsonify({
        "plan": plan["id"],
        "plan_name": plan["name"],
        "limits": plan.get("limits", {}),
        "usage": usage,
    })
