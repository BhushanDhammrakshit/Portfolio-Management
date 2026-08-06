"""Plan-expired notification emails.

Runs once daily (see the scheduler wired in ``application/__init__.py``).
For every user whose paid plan (Pro/Elite, including an expired Elite
trial) has lapsed, sends a one-time "you're back on Free" email with a
renew CTA. Each expiry cycle notifies at most once — tracked via
``PlanExpiredNotifiedFor`` on the user's Azure Table entity (keyed to the
specific ``PlanExpiresOn`` value so a later renewal + re-expiry re-fires).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from azure.data.tables import UpdateMode

from application.config import APP_BASE_URL, APP_NAME
from application.services import plans
from application.services.azure_table import user_table_client

log = logging.getLogger(__name__)


def _is_expired(expires_on: str) -> bool:
    if not expires_on:
        return False
    try:
        return datetime.fromisoformat(expires_on).date() < date.today()
    except (TypeError, ValueError):
        return False


def _mark_notified(user_entity: dict, expires_on: str) -> None:
    user_entity["PlanExpiredNotifiedFor"] = expires_on
    try:
        user_table_client.update_entity(entity=user_entity, mode=UpdateMode.MERGE)
    except Exception as e:
        log.warning("plan_expiry: could not persist PlanExpiredNotifiedFor: %s", e)


def _expired_html(name: str, plan_name: str, expires_on: str) -> str:
    safe_name = (name or "there").split("@")[0][:60]
    billing_url = f"{APP_BASE_URL}/billing"
    return f"""\
<!doctype html>
<html><body style="margin:0;padding:0;background:#f6f9fc;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937">
  <div style="max-width:600px;margin:0 auto;padding:24px">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%);padding:32px 28px;color:#fff;text-align:center">
        <div style="font-size:12px;opacity:.85;letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px">Don't lose your edge</div>
        <div style="font-size:24px;font-weight:800;line-height:1.3">You were making smarter trades.<br>Don't stop now.</div>
      </div>
      <div style="padding:28px 28px 8px">
        <p style="font-size:15px;margin:0 0 16px">Hi <strong>{safe_name}</strong>,</p>
        <p style="font-size:14px;line-height:1.65;color:#374151;margin:0 0 16px">
          Over the last 7 days, your <strong>{plan_name}</strong> trial gave you an edge most
          retail investors never get &mdash; AI-powered analysis, real-time F&amp;O gap
          forecasts, and multi-broker sync all working together.
        </p>
        <div style="background:#fef2f2;border-left:4px solid #dc2626;border-radius:8px;padding:14px 18px;margin:0 0 22px">
          <div style="font-size:14px;font-weight:700;color:#991b1b;margin:0 0 2px">&#9888; Your {plan_name} access ended on {expires_on}</div>
          <div style="font-size:13px;color:#7f1d1d">Your account has moved to the Free plan.</div>
        </div>
        <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6366f1;margin:0 0 12px">What you're missing now</div>
        <table style="width:100%;border-collapse:collapse;margin:0 0 24px;font-size:13px">
          <tr style="background:#f8fafc">
            <td style="padding:10px 14px;border:1px solid #e5e7eb;font-weight:600;color:#374151">Feature</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;color:#dc2626;font-weight:600">Free</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;color:#059669;font-weight:600">{plan_name} &#10022;</td>
          </tr>
          <tr>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">AI stock analysis</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;color:#9ca3af">3 / day</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;font-weight:700;color:#059669">Unlimited</td>
          </tr>
          <tr style="background:#f8fafc">
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Portfolio holdings</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;color:#9ca3af">10</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;font-weight:700;color:#059669">Unlimited</td>
          </tr>
          <tr>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Broker sync</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;color:#9ca3af">1 broker</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;font-weight:700;color:#059669">All brokers</td>
          </tr>
          <tr style="background:#f8fafc">
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Swing &amp; F&amp;O tools</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;color:#dc2626">&#10005;</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;color:#059669;font-weight:700">&#10003; Full access</td>
          </tr>
          <tr>
            <td style="padding:10px 14px;border:1px solid #e5e7eb">Sector rotation maps</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;color:#dc2626">&#10005;</td>
            <td style="padding:10px 14px;border:1px solid #e5e7eb;text-align:center;color:#059669;font-weight:700">&#10003; Full access</td>
          </tr>
        </table>
        <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6366f1;margin:0 0 12px">Less than one bad trade</div>
        <table style="width:100%;border-collapse:separate;border-spacing:12px 0;margin:0 0 24px">
          <tr>
            <td style="width:50%;background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:18px;text-align:center;vertical-align:top">
              <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.06em">Monthly</div>
              <div style="font-size:28px;font-weight:800;color:#1f2937;margin:6px 0 2px">&#8377;499</div>
              <div style="font-size:12px;color:#9ca3af">per month &middot; cancel anytime</div>
            </td>
            <td style="width:50%;background:linear-gradient(135deg,#eef2ff,#e0e7ff);border:2px solid #6366f1;border-radius:10px;padding:18px;text-align:center;vertical-align:top">
              <div style="font-size:10px;font-weight:700;color:#fff;background:#6366f1;border-radius:20px;padding:2px 10px;display:inline-block;margin-bottom:6px">BEST VALUE</div>
              <div style="font-size:28px;font-weight:800;color:#1f2937;margin:2px 0 2px">&#8377;3,999</div>
              <div style="font-size:12px;color:#4f46e5;font-weight:600">per year &middot; save 33%</div>
            </td>
          </tr>
        </table>
        <div style="text-align:center;margin:28px 0">
          <a href="{billing_url}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;text-decoration:none;font-weight:700;font-size:15px;padding:14px 36px;border-radius:10px;box-shadow:0 4px 14px rgba(99,102,241,.35)">Restore My {plan_name} Access &#8594;</a>
        </div>
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:14px 18px;margin:0 0 22px;text-align:center">
          <div style="font-size:14px;color:#166534;font-weight:600;margin:0 0 4px">&#128737; Our promise</div>
          <div style="font-size:13px;color:#15803d;line-height:1.5">If {plan_name} doesn't improve your investment decisions within 30 days,<br>write to us and we'll make it right. No questions asked.</div>
        </div>
        <div style="text-align:center;font-size:12px;color:#9ca3af;margin:0 0 12px;line-height:1.6">
          &#128274; Your portfolio, watchlist, and settings are safe and untouched.<br>Upgrade anytime to pick up right where you left off.
        </div>
      </div>
      <div style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:18px 28px;text-align:center">
        <p style="margin:0 0 6px;font-size:12px;color:#9ca3af;line-height:1.5">
          <strong>P.S.</strong> &mdash; {plan_name} includes <em>everything</em> you used during your
          trial: unlimited AI analysis, all broker integrations, swing tools,
          F&amp;O forecasts, and sector rotation maps. No feature gates. No upsells.
        </p>
        <p style="margin:0;font-size:11px;color:#d1d5db;line-height:1.5">
          &copy; {APP_NAME} &middot; You received this because your free trial ended &middot;
          <a href="{billing_url}" style="color:#6366f1;text-decoration:none">Manage subscription</a>
        </p>
      </div>
    </div>
  </div>
</body></html>"""


def send_plan_expired_emails() -> dict:
    """Scan all users and email those whose paid plan just lapsed.

    Returns a small stats dict: {"checked": n, "sent": n, "errors": n}.
    """
    from application.services import email_service

    stats = {"checked": 0, "sent": 0, "errors": 0}
    try:
        users = list(user_table_client.query_entities(
            query_filter="PartitionKey eq 'user'"))
    except Exception as e:
        log.warning("plan_expiry: could not list users: %s", e)
        return stats

    for user in users:
        stats["checked"] += 1
        try:
            plan_id = (user.get("Plan") or "free").lower()
            expires_on = user.get("PlanExpiresOn") or ""
            if plan_id == "free" or not expires_on:
                continue
            if not _is_expired(expires_on):
                continue
            if user.get("PlanExpiredNotifiedFor") == expires_on:
                continue  # already notified for this expiry cycle

            email = user.get("Email")
            if not email:
                continue
            name = user.get("UserName", "")
            plan_name = plans.get_plan(plan_id).get("name", plan_id.title())
            ok, info = email_service.send_email(
                to=email,
                subject=f"You were making smarter trades \u2014 don\u2019t stop now",
                html=_expired_html(name, plan_name, expires_on),
            )
            if ok:
                _mark_notified(user, expires_on)
                stats["sent"] += 1
            else:
                stats["errors"] += 1
                log.warning("plan_expiry: send failed for %s: %s", email, info)
        except Exception as e:
            stats["errors"] += 1
            log.warning("plan_expiry: error processing user: %s", e)

    return stats
