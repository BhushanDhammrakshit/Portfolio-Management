"""Subscription / billing routes with Razorpay integration + coupons."""
from flask import (Blueprint, render_template, session, redirect, url_for,
                   request, flash, jsonify)

from application.services import plans
from application.services import razorpay_gateway as rz
from application.services.event_tracker import track_event

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
        razorpay_enabled=rz.is_enabled(),
        razorpay_key_id=rz.public_key_id(),
    )


@billing_bp.route("/billing/upgrade", methods=["POST"])
@_login_required
def upgrade():
    """Legacy fallback endpoint.

    For paid plans, users should use Razorpay checkout via /api/billing/checkout.
    Keep this route only for free-plan switch and backwards compatibility.
    """
    plan_id = (request.form.get("plan") or "").lower()
    cycle = (request.form.get("cycle") or "monthly").lower()
    if plan_id not in plans.PLANS:
        flash("Unknown plan.", "danger")
        return redirect(url_for("billing.billing_page"))

    if plan_id != "free":
        flash("Please complete payment via Razorpay checkout.", "warning")
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
        track_event(session["user_id"], "plan_upgraded", {
            "from": (plans.get_user_plan(user) or {}).get("id", "free"),
            "to": plan_id, "trigger": "legacy_form",
        })
        flash(f"Welcome to {plans.PLANS[plan_id]['name']}! Your plan is active for "
              f"{months} month{'s' if months > 1 else ''}.", "success")
    return redirect(url_for("billing.billing_page"))


@billing_bp.route("/billing/cancel", methods=["POST"])
@_login_required
def cancel():
    user = plans.current_user_entity()
    old_plan = (plans.get_user_plan(user) or {}).get("id", "free") if user else "free"
    if user:
        plans.set_user_plan(user, "free")
    track_event(session.get("user_id", ""), "plan_cancelled", {"from": old_plan})
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


@billing_bp.route("/api/billing/referral")
@_login_required
def referral_stats_json():
    """Referral program stats for the current user."""
    from application.services import referral
    stats = referral.get_referral_stats(session["user_id"])
    # Build shareable link
    stats["link"] = f"{request.host_url.rstrip('/')}/signup?ref={stats.get('code', '')}"
    return jsonify(stats)


@billing_bp.route("/api/billing/coupon/validate", methods=["POST"])
@_login_required
def coupon_validate():
    data = request.get_json(silent=True) or {}
    plan_id = (data.get("plan") or "").lower()
    cycle = (data.get("cycle") or "monthly").lower()
    code = (data.get("coupon") or "").strip()

    if plan_id not in plans.PLANS:
        return jsonify({"ok": False, "error": "Unknown plan."}), 400

    base_inr = plans.plan_amount_inr(plan_id, cycle)
    coupon = plans.apply_coupon(plan_id, cycle, code, base_inr)
    return jsonify({
        "ok": coupon.get("valid", False),
        "coupon": coupon,
        "base_amount_inr": base_inr,
    })


@billing_bp.route("/api/billing/checkout", methods=["POST"])
@_login_required
def checkout_create():
    data = request.get_json(silent=True) or {}
    plan_id = (data.get("plan") or "").lower()
    cycle = (data.get("cycle") or "monthly").lower()
    coupon_code = (data.get("coupon") or "").strip()

    if plan_id not in plans.PLANS or plan_id == "free":
        return jsonify({"ok": False, "error": "Please select a paid plan."}), 400

    if cycle not in ("monthly", "annual"):
        cycle = "monthly"

    if not rz.is_enabled():
        return jsonify({
            "ok": False,
            "error": "Payments are not configured yet. Add Razorpay keys to enable checkout.",
        }), 503

    base_inr = plans.plan_amount_inr(plan_id, cycle)
    coupon = plans.apply_coupon(plan_id, cycle, coupon_code, base_inr)
    if coupon_code and not coupon.get("valid"):
        return jsonify({"ok": False, "error": coupon.get("message") or "Invalid coupon."}), 400

    final_inr = int(coupon.get("final_amount_inr") or base_inr)
    if final_inr <= 0:
        return jsonify({"ok": False, "error": "Final amount must be greater than zero."}), 400

    amount_paise = final_inr * 100
    uid = str(session.get("user_id") or "")
    receipt = f"hm2-{uid[:8]}-{plan_id[:3]}-{cycle[:1]}-{int(__import__('time').time())}"
    notes = {
        "user_id": uid,
        "email": session.get("email") or "",
        "plan": plan_id,
        "cycle": cycle,
        "coupon": (coupon.get("code") or ""),
        "base_inr": str(base_inr),
        "discount_inr": str(coupon.get("discount_inr") or 0),
        "final_inr": str(final_inr),
    }

    try:
        order = rz.create_order(amount_paise=amount_paise, receipt=receipt, notes=notes)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    rz.remember_order(order.get("id"), notes)
    return jsonify({
        "ok": True,
        "key_id": rz.public_key_id(),
        "order_id": order.get("id"),
        "amount": order.get("amount"),
        "currency": order.get("currency") or "INR",
        "plan_name": plans.get_plan(plan_id).get("name"),
        "cycle": cycle,
        "coupon": coupon,
        "prefill": {
            "name": session.get("name") or "",
            "email": session.get("email") or "",
        },
    })


def _activate_paid_plan_from_meta(meta: dict) -> bool:
    uid = str(meta.get("user_id") or "")
    user = plans.user_entity_by_id(uid) if uid else plans.current_user_entity()
    if not user:
        return False
    plan_id = (meta.get("plan") or "").lower()
    cycle = (meta.get("cycle") or "monthly").lower()
    if plan_id not in plans.PLANS or plan_id == "free":
        return False
    months = 12 if cycle == "annual" else 1
    plans.set_user_plan(user, plan_id, months=months)

    track_event(uid, "plan_upgraded", {
        "to": plan_id, "cycle": cycle, "trigger": "razorpay",
    })

    # Confirmation email — best-effort, never blocks plan activation.
    try:
        from application.services import email_service, email_templates
        email = user.get("Email")
        if email:
            plan_name = plans.get_plan(plan_id).get("name", plan_id.title())
            email_service.send_email(
                to=email,
                subject=email_templates.TEMPLATES["renewal_success"]["subject"],
                html=email_templates.renewal_success_html(
                    user.get("UserName", ""), plan_name, user.get("PlanExpiresOn", "")),
            )
    except Exception as e:
        print(f"[billing] renewal confirmation email failed: {e}")

    # Award referral credit to whoever referred this user.
    try:
        from application.services import referral
        referral.create_credit(referred_user_id=uid, plan_id=plan_id)
    except Exception as e:
        print(f"[referral] create_credit on payment failed: {e}")

    return True


@billing_bp.route("/api/billing/verify", methods=["POST"])
@_login_required
def verify_checkout():
    data = request.get_json(silent=True) or {}
    order_id = (data.get("razorpay_order_id") or "").strip()
    payment_id = (data.get("razorpay_payment_id") or "").strip()
    signature = (data.get("razorpay_signature") or "").strip()

    if not order_id or not payment_id or not signature:
        return jsonify({"ok": False, "error": "Missing payment verification fields."}), 400

    if not rz.verify_checkout_signature(order_id, payment_id, signature):
        return jsonify({"ok": False, "error": "Signature verification failed."}), 400

    try:
        payment = rz.fetch_payment(payment_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    status = (payment.get("status") or "").lower()
    if status not in ("captured", "authorized"):
        return jsonify({"ok": False, "error": f"Payment status is {status or 'unknown'}."}), 400

    meta = rz.recall_order(order_id) or {}
    if not meta:
        # Fallback: allow explicit plan/cycle from client only when signed
        # checkout verification already passed.
        meta = {
            "plan": (data.get("plan") or "").lower(),
            "cycle": (data.get("cycle") or "monthly").lower(),
        }

    if not _activate_paid_plan_from_meta(meta):
        return jsonify({"ok": False, "error": "Could not activate your plan."}), 500

    return jsonify({"ok": True, "message": "Payment verified and plan activated."})


@billing_bp.route("/billing/webhook", methods=["POST"])
def billing_webhook():
    sig = request.headers.get("X-Razorpay-Signature", "")
    raw = request.get_data() or b""
    if not rz.verify_webhook_signature(raw, sig):
        return jsonify({"ok": False, "error": "invalid signature"}), 400

    payload = request.get_json(silent=True) or {}
    event_id = str(payload.get("id") or "")
    if event_id and not rz.mark_event_processed(event_id):
        return jsonify({"ok": True, "status": "duplicate"})

    event = (payload.get("event") or "").lower()
    if event not in ("payment.captured", "order.paid"):
        return jsonify({"ok": True, "status": "ignored"})

    order_id = None
    try:
        entity = (((payload.get("payload") or {}).get("payment") or {}).get("entity") or {})
        order_id = entity.get("order_id")
    except Exception:
        order_id = None

    meta = rz.recall_order(order_id or "") or {}
    if not meta:
        return jsonify({"ok": True, "status": "no_meta"})

    ok = _activate_paid_plan_from_meta(meta)
    return jsonify({"ok": True, "status": "activated" if ok else "activate_failed"})
