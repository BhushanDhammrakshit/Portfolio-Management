"""Free-plan upgrade nudge emails.

Sends a periodic upgrade nudge to users who have been on the Free plan for
at least 7 days and haven't received a nudge in the last 14 days.
Tracked via FreeNudgeSentOn field on the user entity.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from azure.data.tables import UpdateMode

from application.config import APP_BASE_URL, APP_NAME
from application.services import plans
from application.services.azure_table import user_table_client

log = logging.getLogger(__name__)

_COOLDOWN_DAYS = 14
_MIN_AGE_DAYS = 7  # don't nudge brand-new signups


def _account_age_days(user: dict) -> int | None:
    created = user.get("TermsAcceptedOn") or ""
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00")).date()
        return (date.today() - dt).days
    except (TypeError, ValueError):
        return None


def _nudge_on_cooldown(user: dict) -> bool:
    last_sent = user.get("FreeNudgeSentOn") or ""
    if not last_sent:
        return False
    try:
        sent_date = datetime.fromisoformat(last_sent).date()
        return (date.today() - sent_date).days < _COOLDOWN_DAYS
    except (TypeError, ValueError):
        return False


def _nudge_html(name: str) -> str:
    safe_name = (name or "there").split("@")[0][:60]
    billing_url = f"{APP_BASE_URL}/billing"
    return f"""\
<!doctype html>
<html><body style="margin:0;padding:0;background:#f6f9fc;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937">
  <div style="max-width:600px;margin:0 auto;padding:24px">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#0ea5e9 0%,#0284c7 100%);padding:28px;color:#fff;text-align:center">
        <div style="font-size:12px;opacity:.85;letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px">You're missing out</div>
        <div style="font-size:22px;font-weight:800;line-height:1.3">Unlock the full power of {APP_NAME}</div>
      </div>
      <div style="padding:28px">
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          You've been using {APP_NAME} on the Free plan &mdash; great for getting started!
          But there's so much more waiting for you:
        </p>
        <ul style="font-size:14px;line-height:1.8;color:#374151;padding-left:20px;margin:0 0 20px">
          <li><strong>Unlimited AI stock analysis</strong> (Free gets 3/month)</li>
          <li><strong>Swing &amp; intraday tools</strong> &mdash; breakouts, ORB scanner, RVOL heatmap</li>
          <li><strong>F&amp;O gap forecasts</strong> &amp; options analytics</li>
          <li><strong>Multi-broker sync</strong> &mdash; Fyers, Dhan, Upstox all in one view</li>
          <li><strong>Unlimited portfolio holdings</strong> (Free caps at 15)</li>
        </ul>
        <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;padding:14px 18px;margin:0 0 22px;text-align:center">
          <div style="font-size:14px;color:#0369a1;font-weight:600">Pro starts at just &#8377;399/month</div>
          <div style="font-size:12px;color:#0c4a6e;margin-top:4px">Less than one bad trade. Cancel anytime.</div>
        </div>
        <div style="text-align:center;margin:24px 0">
          <a href="{billing_url}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;text-decoration:none;font-weight:700;font-size:15px;padding:14px 36px;border-radius:10px;box-shadow:0 4px 14px rgba(99,102,241,.35)">See Plans &amp; Upgrade &#8594;</a>
        </div>
        <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;line-height:1.5">
          &#128274; Your portfolio data stays safe on any plan.
        </p>
      </div>
      <div style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:16px 28px;text-align:center">
        <p style="margin:0;font-size:11px;color:#d1d5db;line-height:1.5">
          &copy; {APP_NAME} &middot; You received this as a free-plan member &middot;
          <a href="{billing_url}" style="color:#6366f1;text-decoration:none">Manage subscription</a>
        </p>
      </div>
    </div>
  </div>
</body></html>"""


def send_free_plan_nudges() -> dict:
    """Send upgrade nudge to eligible free-plan users.

    Returns {"checked": n, "sent": n, "errors": n, "skipped": n}.
    """
    from application.services import email_service

    stats = {"checked": 0, "sent": 0, "errors": 0, "skipped": 0}
    try:
        users = list(user_table_client.query_entities(
            query_filter="PartitionKey eq 'user'"))
    except Exception as e:
        log.warning("free_nudge: could not list users: %s", e)
        return stats

    for user in users:
        stats["checked"] += 1
        try:
            plan_id = (user.get("Plan") or "free").lower()
            if plan_id != "free":
                continue
            if not user.get("EmailVerified"):
                continue

            age = _account_age_days(user)
            if age is None or age < _MIN_AGE_DAYS:
                continue
            if _nudge_on_cooldown(user):
                stats["skipped"] += 1
                continue

            email = user.get("Email")
            if not email:
                continue
            name = user.get("UserName", "")

            ok, info = email_service.send_email(
                to=email,
                subject=f"You're missing out \u2014 unlock the full {APP_NAME}",
                html=_nudge_html(name),
            )
            if ok:
                user["FreeNudgeSentOn"] = date.today().isoformat()
                try:
                    user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
                except Exception as ue:
                    log.warning("free_nudge: persist failed: %s", ue)
                stats["sent"] += 1
            else:
                stats["errors"] += 1
                log.warning("free_nudge: send failed for %s: %s", email, info)
        except Exception as e:
            stats["errors"] += 1
            log.warning("free_nudge: error processing user: %s", e)

    return stats
