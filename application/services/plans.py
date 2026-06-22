"""Subscription plans, per-user limits, and monthly usage tracking.

Plan state is stored on the user entity in the Azure ``USER_INFO_TABLE``
(columns: ``Plan``, ``PlanExpiresOn``). Usage counters are stored in a small
JSON file alongside ``_token_usage.json`` so they survive process restarts and
auto-reset every calendar month.

Tiers (May 2026 launch pricing):
    free   – 15 holdings, 3 single-stock AI / month, 0 bulk AI, 1 broker
    pro    – unlimited holdings, 100 single + 10 bulk AI / month, multi-broker
    elite  – unlimited everything (fair-use 500 single + 50 bulk / month)
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta
from functools import wraps
from typing import Optional

from flask import flash, redirect, session, url_for, jsonify, request

# ── Plan catalogue ──────────────────────────────────────────────────────

PLANS = {
    "free": {
        "id": "free",
        "name": "Free",
        "tagline": "Track your portfolio, free forever",
        "price_monthly": 0,
        "price_annual": 0,
        "limits": {
            "holdings": 15,
            "ai_single": 3,
            "ai_bulk": 0,
            "ai_chat_daily": 5,
            "ai_tokens_monthly": 10_000,
            "fundamentals_ai": 0,
            "brokers": 1,
        },
        "features": [
            "Portfolio tracking & P/L (up to 15 holdings)",
            "Manual entry, CSV import, 1 broker sync",
            "Sector heatmap & market pulse",
            "Mutual fund tracker & advisor",
            "Tender calendar",
            "Algo Helper (3 AI analyses / month)",
        ],
        "missing": [
            "Intraday tools (ORB, RVOL heatmap)",
            "Swing tools (breakouts, RS, patterns, 52W high)",
            "Fundamentals lookup",
            "AI Idea of the Day",
            "Options analytics, volume alerts, advanced metrics",
        ],
    },
    "pro": {
        "id": "pro",
        "name": "Pro",
        "tagline": "For active retail investors",
        "price_monthly": 399,
        "price_annual": 3499,
        "highlight": True,
        "limits": {
            "holdings": None,
            "ai_single": 100,
            "ai_bulk": 10,
            "ai_chat_daily": 50,
            "ai_tokens_monthly": 100_000,
            "fundamentals_ai": 100,
            "brokers": 5,
        },
        "features": [
            "Everything in Free, plus:",
            "Intraday tools — ORB scanner & RVOL heatmap",
            "Intraday scanner — Swing 10\u201315% picks",
            "Swing tools — breakouts, relative strength, chart patterns, near 52-week high",
            "Fundamentals — lookup any stock",
            "AI Assistant — 100,000 tokens / month",
            "AI & Tools — Idea of the Day",
            "Unlimited holdings & multi-broker sync",
        ],
        "missing": [
            "Options analytics (Elite)",
            "Volume alerts (Elite)",
            "Fundamentals Top Picks (Elite)",
            "Advanced metrics: Sharpe, beta, drawdown (Elite)",
            "Unlimited AI tokens (Elite)",
        ],
    },
    "elite": {
        "id": "elite",
        "name": "Elite",
        "tagline": "Pro tools + unlimited AI",
        "price_monthly": 999,
        "price_annual": 8999,
        "limits": {
            "holdings": None,
            "ai_single": 500,
            "ai_bulk": 50,
            "ai_chat_daily": 500,
            "ai_tokens_monthly": None,  # unlimited (fair-use)
            "fundamentals_ai": 500,
            "brokers": None,
        },
        "features": [
            "Everything in Pro, plus:",
            "Options analytics (NIFTY option chain + OI)",
            "Volume alerts (volume shockers in real time)",
            "Fundamentals — Top Picks (curated buy ideas)",
            "AI Assistant — unlimited tokens (fair-use)",
            "Advanced metrics — Sharpe, beta, drawdown & more",
        ],
        "missing": [],
    },
}

PLAN_RANK = {"free": 0, "pro": 1, "elite": 2}


# ── Coupons / pricing ─────────────────────────────────────────────────

# Coupon rules are intentionally data-driven so non-code users can edit one
# place. Values are in INR unless noted.
COUPONS = {
    "WELCOME10": {
        "type": "percent",      # percent | flat
        "value": 10,
        "active": True,
        "plans": ["pro", "elite"],
        "cycles": ["monthly", "annual"],
    },
    "PRO50": {
        "type": "flat",
        "value": 50,
        "active": True,
        "plans": ["pro"],
        "cycles": ["monthly"],
    },
    "ELITE200": {
        "type": "flat",
        "value": 200,
        "active": True,
        "plans": ["elite"],
        "cycles": ["monthly"],
    },
}


def plan_amount_inr(plan_id: str, cycle: str) -> int:
    """Return base amount in INR for the selected plan/cycle."""
    p = get_plan(plan_id)
    c = (cycle or "monthly").lower()
    if p["id"] == "free":
        return 0
    return int(p.get("price_annual") if c == "annual" else p.get("price_monthly") or 0)


def apply_coupon(plan_id: str, cycle: str, coupon_code: str, base_amount_inr: int) -> dict:
    """Validate and apply coupon. Returns pricing payload with discount."""
    code = (coupon_code or "").strip().upper()
    out = {
        "valid": False,
        "code": code,
        "discount_inr": 0,
        "final_amount_inr": int(base_amount_inr or 0),
        "message": "",
    }
    if not code:
        out["message"] = "No coupon applied."
        return out

    rule = COUPONS.get(code)
    if not rule or not rule.get("active"):
        out["message"] = "Invalid or inactive coupon code."
        return out

    if plan_id not in set(rule.get("plans") or []):
        out["message"] = "Coupon is not valid for this plan."
        return out

    c = (cycle or "monthly").lower()
    if c not in set(rule.get("cycles") or []):
        out["message"] = "Coupon is not valid for this billing cycle."
        return out

    amt = max(0, int(base_amount_inr or 0))
    if amt == 0:
        out["message"] = "Coupon is not applicable for free plan."
        return out

    if rule.get("type") == "percent":
        discount = int(round(amt * (float(rule.get("value") or 0) / 100.0)))
    else:
        discount = int(rule.get("value") or 0)
    discount = max(0, min(amt, discount))

    out.update({
        "valid": True,
        "discount_inr": discount,
        "final_amount_inr": max(0, amt - discount),
        "message": f"Coupon {code} applied successfully.",
    })
    return out


def get_plan(plan_id: Optional[str]) -> dict:
    return PLANS.get((plan_id or "free").lower(), PLANS["free"])


# ── User plan helpers (read/write Azure Table) ──────────────────────────

def _is_active(expires_on: Optional[str]) -> bool:
    if not expires_on:
        return False
    try:
        return datetime.fromisoformat(expires_on).date() >= date.today()
    except (TypeError, ValueError):
        return False


def get_user_plan(user_entity: Optional[dict]) -> dict:
    """Return the active plan dict for a user entity (or Free if expired/none)."""
    if not user_entity:
        return PLANS["free"]
    plan_id = (user_entity.get("Plan") or "free").lower()
    expires = user_entity.get("PlanExpiresOn")
    if plan_id != "free" and not _is_active(expires):
        return PLANS["free"]
    return get_plan(plan_id)


def set_user_plan(user_entity: dict, plan_id: str, months: int = 1) -> dict:
    """Update plan + expiry on the entity in-memory. Caller must persist."""
    from azure.data.tables import UpdateMode
    from application.services.azure_table import user_table_client

    plan_id = (plan_id or "free").lower()
    if plan_id not in PLANS:
        plan_id = "free"
    user_entity["Plan"] = plan_id
    if plan_id == "free":
        user_entity["PlanExpiresOn"] = ""
    else:
        new_expiry = date.today() + timedelta(days=30 * max(1, months))
        user_entity["PlanExpiresOn"] = new_expiry.isoformat()
    try:
        user_table_client.update_entity(entity=user_entity, mode=UpdateMode.MERGE)
    except Exception as e:
        print(f"[plans] could not persist plan: {e}")
    return user_entity


# ── Monthly usage tracking (per-user) ───────────────────────────────────

_LOCK = threading.Lock()
_USAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "_usage.json",
)


def _this_month() -> str:
    return date.today().strftime("%Y-%m")


def _today() -> str:
    return date.today().isoformat()


def _load() -> dict:
    try:
        with open(_USAGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    try:
        with open(_USAGE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _entry_for(data: dict, user_id: str) -> dict:
    month = _this_month()
    e = data.get(user_id)
    if not e or e.get("month") != month:
        e = {"month": month, "ai_single": 0, "ai_bulk": 0, "_chat_day": "", "ai_chat_daily": 0}
        data[user_id] = e
    # Reset daily counters when day rolls over
    if e.get("_chat_day") != _today():
        e["_chat_day"] = _today()
        e["ai_chat_daily"] = 0
    return e


def get_usage(user_id: str) -> dict:
    """Return current month's usage counters for a user."""
    if not user_id:
        return {"ai_single": 0, "ai_bulk": 0, "ai_chat_daily": 0}
    with _LOCK:
        data = _load()
        e = _entry_for(data, user_id)
        _save(data)
        return {
            "ai_single": int(e.get("ai_single", 0)),
            "ai_bulk": int(e.get("ai_bulk", 0)),
            "ai_chat_daily": int(e.get("ai_chat_daily", 0)),
        }


def increment_usage(user_id: str, key: str, n: int = 1) -> int:
    """Increment a counter and return the new value."""
    if not user_id or n <= 0:
        return 0
    with _LOCK:
        data = _load()
        e = _entry_for(data, user_id)
        e[key] = int(e.get(key, 0)) + int(n)
        _save(data)
        return int(e[key])


def remaining(user_id: str, plan: dict, key: str) -> Optional[int]:
    """Remaining quota for a key. ``None`` means unlimited."""
    limit = plan.get("limits", {}).get(key)
    if limit is None:
        return None
    used = get_usage(user_id).get(key, 0)
    return max(0, limit - used)


def can_use(user_id: str, plan: dict, key: str, n: int = 1) -> bool:
    rem = remaining(user_id, plan, key)
    return rem is None or rem >= n


# ── Flask helpers ───────────────────────────────────────────────────────

def current_user_entity():
    """Fetch the logged-in user's entity (or None). Cached on session for one
    request via Flask ``g`` is overkill — we just hit Azure, it's already used
    by /settings on every load."""
    if "email" not in session:
        return None
    try:
        from application.services.azure_table import user_table_client
        users = list(user_table_client.query_entities(
            query_filter=f"Email eq '{session['email']}'"))
        return users[0] if users else None
    except Exception as e:
        print(f"[plans] current_user_entity failed: {e}")
        return None


def current_plan() -> dict:
    return get_user_plan(current_user_entity())


def user_entity_by_id(user_id: str):
    """Fetch user entity by RowKey/user_id."""
    if not user_id:
        return None
    try:
        from application.services.azure_table import user_table_client
        users = list(user_table_client.query_entities(
            query_filter=f"RowKey eq '{user_id}'"
        ))
        return users[0] if users else None
    except Exception as e:
        print(f"[plans] user_entity_by_id failed: {e}")
        return None


def requires_plan(min_plan: str = "pro"):
    """Decorator: bounce users to /billing if their plan is below ``min_plan``.

    Also enforces auth: if the user has no session, returns 401 (or redirects
    to login for non-API routes) instead of treating them as a Free user.
    """
    min_rank = PLAN_RANK.get(min_plan, 1)

    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "email" not in session:
                if request.is_json or request.path.startswith("/api/"):
                    return jsonify({"error": "auth"}), 401
                return redirect(url_for("logIn"))
            plan = current_plan()
            if PLAN_RANK.get(plan["id"], 0) < min_rank:
                if request.is_json or request.path.startswith("/api/"):
                    return jsonify({
                        "error": "upgrade_required",
                        "message": f"This feature requires the {min_plan.title()} plan.",
                        "current_plan": plan["id"],
                        "required_plan": min_plan,
                        "upgrade_url": url_for("billing.billing_page"),
                    }), 402
                flash(f"This feature requires the {min_plan.title()} plan.", "warning")
                return redirect(url_for("billing.billing_page"))
            return view(*args, **kwargs)
        return wrapped
    return deco


def requires_quota(key: str, n: int = 1):
    """Decorator: ensure user has ``n`` units of quota for ``key`` left this period.

    Does NOT auto-increment — view function should call ``increment_usage``
    on success.
    """
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            uid = session.get("user_id", "")
            plan = current_plan()
            if not can_use(uid, plan, key, n):
                limit = plan.get("limits", {}).get(key)
                if request.is_json or request.path.startswith("/api/"):
                    return jsonify({
                        "error": "quota_exceeded",
                        "message": f"You've used your monthly {key} quota ({limit}). Upgrade for more.",
                        "current_plan": plan["id"],
                        "upgrade_url": url_for("billing.billing_page"),
                    }), 402
                flash(f"You've used your monthly quota for this feature. Upgrade for more.", "warning")
                return redirect(url_for("billing.billing_page"))
            return view(*args, **kwargs)
        return wrapped
    return deco
