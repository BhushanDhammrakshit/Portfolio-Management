"""Trial-ending reminder emails.

Sends a reminder email to users whose Elite trial expires in 3 days or 1 day.
Each threshold is sent at most once per user, tracked via TrialReminder3d / TrialReminder1d
fields on the user entity.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from azure.data.tables import UpdateMode

from application.config import APP_BASE_URL, APP_NAME
from application.services import plans
from application.services.azure_table import user_table_client

log = logging.getLogger(__name__)

_THRESHOLDS = [3, 1]  # days before expiry


def _days_until_expiry(expires_on: str) -> int | None:
    if not expires_on:
        return None
    try:
        exp = datetime.fromisoformat(expires_on).date()
        return (exp - date.today()).days
    except (TypeError, ValueError):
        return None


def _reminder_html(name: str, plan_name: str, days_left: int) -> str:
    safe_name = (name or "there").split("@")[0][:60]
    billing_url = f"{APP_BASE_URL}/billing"
    urgency = "tomorrow" if days_left <= 1 else f"in {days_left} days"
    return f"""\
<!doctype html>
<html><body style="margin:0;padding:0;background:#f6f9fc;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937">
  <div style="max-width:600px;margin:0 auto;padding:24px">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#f59e0b 0%,#d97706 100%);padding:28px;color:#fff;text-align:center">
        <div style="font-size:12px;opacity:.85;letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px">Trial ending soon</div>
        <div style="font-size:22px;font-weight:800;line-height:1.3">Your {plan_name} trial ends {urgency}</div>
      </div>
      <div style="padding:28px">
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Just a heads-up &mdash; your <strong>{plan_name}</strong> trial expires {urgency}.
          After that, your account will move to the Free plan and you'll lose access to
          advanced tools, unlimited AI analysis, and multi-broker sync.
        </p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 22px">
          Upgrade now to keep everything you've been using &mdash; no interruption, no data loss.
        </p>
        <div style="text-align:center;margin:24px 0">
          <a href="{billing_url}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;text-decoration:none;font-weight:700;font-size:15px;padding:14px 36px;border-radius:10px;box-shadow:0 4px 14px rgba(99,102,241,.35)">Upgrade Now &#8594;</a>
        </div>
        <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;line-height:1.5">
          &#128274; Your data is safe regardless. Upgrade anytime to restore full access.
        </p>
      </div>
      <div style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:16px 28px;text-align:center">
        <p style="margin:0;font-size:11px;color:#d1d5db;line-height:1.5">
          &copy; {APP_NAME} &middot; You received this because your free trial is ending soon &middot;
          <a href="{billing_url}" style="color:#6366f1;text-decoration:none">Manage subscription</a>
        </p>
      </div>
    </div>
  </div>
</body></html>"""


def send_trial_ending_reminders() -> dict:
    """Scan users on trial and send reminders at 3-day and 1-day thresholds.

    Returns {"checked": n, "sent": n, "errors": n}.
    """
    from application.services import email_service

    stats = {"checked": 0, "sent": 0, "errors": 0}
    try:
        users = list(user_table_client.query_entities(
            query_filter="PartitionKey eq 'user'"))
    except Exception as e:
        log.warning("trial_reminder: could not list users: %s", e)
        return stats

    for user in users:
        stats["checked"] += 1
        try:
            if not plans.is_on_trial(user):
                continue

            expires_on = user.get("PlanExpiresOn") or ""
            days_left = _days_until_expiry(expires_on)
            if days_left is None:
                continue

            for threshold in _THRESHOLDS:
                if days_left > threshold:
                    continue
                if days_left < 0:
                    continue

                field = f"TrialReminder{threshold}d"
                if user.get(field) == expires_on:
                    continue  # already sent for this expiry cycle

                email = user.get("Email")
                if not email:
                    break
                name = user.get("UserName", "")
                plan_id = (user.get("Plan") or "elite").lower()
                plan_name = plans.get_plan(plan_id).get("name", plan_id.title())

                ok, info = email_service.send_email(
                    to=email,
                    subject=f"Your {plan_name} trial ends {'tomorrow' if days_left <= 1 else f'in {days_left} days'} \u2014 upgrade now",
                    html=_reminder_html(name, plan_name, days_left),
                )
                if ok:
                    user[field] = expires_on
                    try:
                        user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
                    except Exception as ue:
                        log.warning("trial_reminder: persist failed: %s", ue)
                    stats["sent"] += 1
                else:
                    stats["errors"] += 1
                    log.warning("trial_reminder: send failed for %s: %s", email, info)
                break  # only send the most urgent threshold per user
        except Exception as e:
            stats["errors"] += 1
            log.warning("trial_reminder: error processing user: %s", e)

    return stats
