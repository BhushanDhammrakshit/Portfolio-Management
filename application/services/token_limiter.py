"""Per-user monthly token limiter for the Algo Helper / AI Assistant.

The token cap is plan-aware:

    free   →  10,000 tokens / month
    pro    → 100,000 tokens / month
    elite  → unlimited (fair-use)

Counters reset on the 1st of every calendar month. Counts are persisted
to a JSON file so they survive process restarts within the same month.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

# Legacy daily fallback (used only when plan lookup fails).
DAILY_LIMIT = 10_000

_LOCK = threading.Lock()
_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "_token_usage.json",
)


def _this_month() -> str:
    return date.today().strftime("%Y-%m")


def _load() -> Dict[str, dict]:
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: Dict[str, dict]) -> None:
    try:
        with open(_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _entry_for(data: Dict[str, dict], user: str) -> dict:
    month = _this_month()
    e = data.get(user)
    # Reset on month roll-over. Also migrates legacy daily entries (which
    # had a `date` field) by treating them as expired.
    if not e or e.get("month") != month:
        e = {"month": month, "tokens": 0}
        data[user] = e
    return e


def _plan_limit(user: str) -> Optional[int]:
    """Look up the user's monthly AI-token cap. ``None`` means unlimited."""
    if not user:
        return DAILY_LIMIT
    try:
        from application.services import plans
        from application.services.azure_table import user_table_client
        rows = list(user_table_client.query_entities(
            query_filter=f"Email eq '{user}'"
        ))
        entity = rows[0] if rows else None
        plan = plans.get_user_plan(entity)
        return plan.get("limits", {}).get("ai_tokens_monthly", DAILY_LIMIT)
    except Exception:
        return DAILY_LIMIT


def get_usage(user: str) -> Tuple[int, Optional[int]]:
    """Return ``(used, limit_or_None)`` for the user this month."""
    if not user:
        return 0, DAILY_LIMIT
    with _LOCK:
        data = _load()
        e = _entry_for(data, user)
        return int(e.get("tokens", 0)), _plan_limit(user)


def remaining(user: str) -> Optional[int]:
    """Tokens left this month. ``None`` means unlimited."""
    used, limit = get_usage(user)
    if limit is None:
        return None
    return max(0, limit - used)


def can_consume(user: str, estimated: int = 1) -> bool:
    """Quick check (does not reserve)."""
    rem = remaining(user)
    return rem is None or rem >= estimated


def add_usage(user: str, tokens: int) -> Tuple[int, Optional[int]]:
    """Add tokens to this month's count. Returns updated ``(used, limit)``."""
    if not user or tokens <= 0:
        return get_usage(user)
    with _LOCK:
        data = _load()
        e = _entry_for(data, user)
        e["tokens"] = int(e.get("tokens", 0)) + int(tokens)
        _save(data)
        used, limit = int(e["tokens"]), _plan_limit(user)
    _maybe_send_usage_alert(user, used, limit)
    return used, limit


def _maybe_send_usage_alert(user: str, used: int, limit: Optional[int]) -> None:
    """Fire an 80%/100% quota email once per calendar month per threshold.

    Unlimited plans (``limit is None``) never alert. Best-effort — any
    failure here must never break token accounting for the caller.
    """
    if not user or not limit:
        return
    pct = int(used / limit * 100)
    if pct < 80:
        return
    threshold = 100 if pct >= 100 else 80
    field = f"UsageWarn{threshold}Month"
    month = _this_month()
    try:
        from azure.data.tables import UpdateMode
        from application.services import email_service, email_templates, plans
        from application.services.azure_table import user_table_client

        rows = list(user_table_client.query_entities(query_filter=f"Email eq '{user}'"))
        entity = rows[0] if rows else None
        if not entity or entity.get(field) == month:
            return  # already alerted for this threshold this month

        email = entity.get("Email")
        if not email:
            return
        name = entity.get("UserName", "")
        plan_id = (entity.get("Plan") or "free").lower()
        plan_name = plans.get_plan(plan_id).get("name", plan_id.title())
        subject = ("Your monthly AI quota is exhausted" if threshold >= 100
                  else f"You've used {threshold}% of your AI quota")

        ok, info = email_service.send_email(
            to=email, subject=subject,
            html=email_templates.usage_limit_warning_html(name, plan_name, threshold),
        )
        if ok:
            entity[field] = month
            user_table_client.update_entity(entity=entity, mode=UpdateMode.MERGE)
        else:
            log.warning("token_limiter: usage alert send failed for %s: %s", email, info)
    except Exception as e:
        log.warning("token_limiter: usage alert error for %s: %s", user, e)
