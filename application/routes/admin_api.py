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


# ── Activity / behaviour analytics ─────────────────────────────────────

@admin_bp.route("/api/admin/activity")
@_require_admin
def activity_stats():
    from application.services import event_tracker
    try:
        users_raw = list(user_table_client.query_entities(
            query_filter="PartitionKey eq 'user'"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    users = []
    for u in users_raw:
        users.append({
            "id": u.get("RowKey", ""),
            "email_verified": bool(u.get("EmailVerified")),
        })
    try:
        stats = event_tracker.compute_activity_stats(users)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(stats)


@admin_bp.route("/api/admin/users/<user_id>/events")
@_require_admin
def user_events(user_id):
    from application.services import event_tracker
    limit = min(int(request.args.get("limit", 50)), 200)
    events = event_tracker.get_user_events(user_id, limit=limit)
    return jsonify({"events": events})


@admin_bp.route("/api/admin/resolve-user")
@_require_admin
def resolve_user():
    """Look up a user by email/name/phone substring and return their ID."""
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"error": "missing q"}), 400
    try:
        users_raw = list(user_table_client.query_entities(
            query_filter="PartitionKey eq 'user'"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    best = None
    for u in users_raw:
        email = str(u.get("Email") or "").strip().lower()
        name = str(u.get("UserName") or "").strip().lower()
        phone = str(u.get("ContactNo") or "").strip().lower()
        row_key = str(u.get("RowKey") or "")
        if q == email or q == row_key:
            # Exact match wins immediately.
            return jsonify({"user_id": row_key, "label": u.get("UserName") or email})
        if best is None and (q in email or q in name or q in phone):
            best = {"user_id": row_key, "label": u.get("UserName") or email}
    if best:
        return jsonify(best)
    return jsonify({"user_id": None})


# ── Trial reminder emails ───────────────────────────────────────────────

@admin_bp.route("/api/admin/trial-reminders/run", methods=["POST"])
@_require_admin
def run_trial_reminders():
    from application.services import trial_reminder
    stats = trial_reminder.send_trial_ending_reminders()
    return jsonify({"ok": True, "stats": stats})


# ── Plan-expired notification emails ─────────────────────────────────────

@admin_bp.route("/api/admin/plan-expiry/run", methods=["POST"])
@_require_admin
def run_plan_expiry():
    from application.services import plan_expiry_notifier
    stats = plan_expiry_notifier.send_plan_expired_emails()
    return jsonify({"ok": True, "stats": stats})


# ── Free-plan upgrade nudge emails ───────────────────────────────────────

@admin_bp.route("/api/admin/free-plan-nudge/run", methods=["POST"])
@_require_admin
def run_free_plan_nudge():
    from application.services import free_plan_nudge
    stats = free_plan_nudge.send_free_plan_nudges()
    return jsonify({"ok": True, "stats": stats})


# ── Email templates — list, preview, manual send ─────────────────────

@admin_bp.route("/api/admin/email-templates")
@_require_admin
def list_email_templates():
    from application.services.email_templates import TEMPLATES
    return jsonify({"templates": list(TEMPLATES.values())})


@admin_bp.route("/api/admin/email-templates/<key>/preview")
@_require_admin
def preview_email_template(key):
    from application.services.email_templates import TEMPLATES, preview_html
    if key not in TEMPLATES:
        return jsonify({"error": "Unknown template"}), 404
    html = preview_html(key)
    if html is None:
        return jsonify({"error": "Preview not available"}), 404
    return jsonify({"key": key, "subject": TEMPLATES[key]["subject"], "html": html})


@admin_bp.route("/api/admin/email-templates/<key>/send", methods=["POST"])
@_require_admin
def send_email_template(key):
    """Send a specific email template to a manually selected list of users."""
    from application.services import email_service
    from application.services import email_templates as et

    if key not in et.TEMPLATES:
        return jsonify({"error": "Unknown template"}), 404
    if et.TEMPLATES[key].get("admin_only"):
        return jsonify({"error": "This is an internal admin notification and can't be sent to users."}), 400

    data = request.get_json(silent=True) or {}
    user_ids = data.get("user_ids") or []
    if not user_ids or not isinstance(user_ids, list):
        return jsonify({"error": "Provide a non-empty 'user_ids' array"}), 400

    tpl = et.TEMPLATES[key]
    stats = {"sent": 0, "errors": 0, "skipped": 0, "details": []}

    for uid in user_ids:
        uid = (uid or "").strip()
        if not uid:
            stats["skipped"] += 1
            continue
        try:
            results = list(user_table_client.query_entities(
                query_filter=f"PartitionKey eq 'user' and RowKey eq '{uid}'"))
            if not results:
                stats["skipped"] += 1
                stats["details"].append({"id": uid, "status": "not found"})
                continue
            user = results[0]
            email = user.get("Email", "")
            if not email:
                stats["skipped"] += 1
                stats["details"].append({"id": uid, "status": "no email"})
                continue

            name = user.get("UserName", "")
            plan_id = (user.get("Plan") or "free").lower()
            plan_name = plans.get_plan(plan_id).get("name", plan_id.title())
            persona = user.get("Persona", "")

            html = _render_template_for_user(key, name, plan_name, persona, user)
            if not html:
                stats["skipped"] += 1
                stats["details"].append({"id": uid, "email": email, "status": "no renderer"})
                continue

            ok, info = email_service.send_email(
                to=email, subject=tpl["subject"], html=html,
            )
            if ok:
                stats["sent"] += 1
                stats["details"].append({"id": uid, "email": email, "status": "sent"})
            else:
                stats["errors"] += 1
                stats["details"].append({"id": uid, "email": email, "status": f"failed: {info}"})
        except Exception as e:
            stats["errors"] += 1
            stats["details"].append({"id": uid, "status": str(e)})

    return jsonify({"ok": True, "stats": stats})


def _render_template_for_user(key: str, name: str, plan_name: str,
                              persona: str, user: dict) -> str | None:
    from application.services import email_templates as et
    renderers = {
        "welcome": lambda: et.welcome_html(name, persona),
        "feature_discovery": lambda: et.feature_discovery_html(name, plan_name),
        "winback": lambda: et.winback_html(name, plan_name),
        "re_engagement": lambda: et.re_engagement_html(name),
        "renewal_success": lambda: et.renewal_success_html(
            name, plan_name, user.get("PlanExpiresOn", "")),
        "weekly_swing": lambda: et.weekly_swing_html(name),
        "weekly_intraday": lambda: et.weekly_intraday_html(name),
        "weekly_investor": lambda: et.weekly_investor_html(name),
        "usage_summary": lambda: et.usage_summary_html(name, plan_name),
        "usage_limit_warning": lambda: et.usage_limit_warning_html(name, plan_name),
        "broker_sync_failure": lambda: et.broker_sync_failure_html(name),
        "referral_reward": lambda: et.referral_reward_html(name),
    }
    fn = renderers.get(key)
    return fn() if fn else None


# ── Send expiry mail to selected users ───────────────────────────────────

@admin_bp.route("/api/admin/send-expiry-mail", methods=["POST"])
@_require_admin
def send_expiry_mail_to_selected():
    """Send the plan-expired persuasion email to a manually selected list of users."""
    from application.services import email_service
    from application.services.plan_expiry_notifier import _expired_html

    data = request.get_json(silent=True) or {}
    user_ids = data.get("user_ids") or []
    if not user_ids or not isinstance(user_ids, list):
        return jsonify({"error": "Provide a non-empty 'user_ids' array"}), 400

    stats = {"sent": 0, "errors": 0, "skipped": 0, "details": []}

    for uid in user_ids:
        uid = (uid or "").strip()
        if not uid:
            stats["skipped"] += 1
            continue
        try:
            results = list(user_table_client.query_entities(
                query_filter=f"PartitionKey eq 'user' and RowKey eq '{uid}'"))
            if not results:
                stats["skipped"] += 1
                stats["details"].append({"id": uid, "status": "not found"})
                continue
            user = results[0]
            email = user.get("Email", "")
            if not email:
                stats["skipped"] += 1
                stats["details"].append({"id": uid, "status": "no email"})
                continue
            name = user.get("UserName", "")
            plan_id = (user.get("Plan") or "free").lower()
            plan_name = plans.get_plan(plan_id).get("name", plan_id.title())
            expires_on = user.get("PlanExpiresOn") or "N/A"

            ok, info = email_service.send_email(
                to=email,
                subject="You were making smarter trades \u2014 don\u2019t stop now",
                html=_expired_html(name, plan_name, expires_on),
            )
            if ok:
                stats["sent"] += 1
                stats["details"].append({"id": uid, "email": email, "status": "sent"})
            else:
                stats["errors"] += 1
                stats["details"].append({"id": uid, "email": email, "status": f"failed: {info}"})
        except Exception as e:
            stats["errors"] += 1
            stats["details"].append({"id": uid, "status": str(e)})

    return jsonify({"ok": True, "stats": stats})
