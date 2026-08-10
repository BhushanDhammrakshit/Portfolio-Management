"""One-time re-engagement email for inactive users.

Fires once when a user has been inactive (no login) for 7+ days, then
will not fire again for the same user within a 30-day cooldown window —
tracked via ``ReEngagementSentOn`` on the user entity. Non-essential:
honors the ``MarketingEmailsOptOut`` flag and includes an unsubscribe link.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from azure.data.tables import UpdateMode

from application.services.azure_table import user_table_client
from application.services import email_prefs

log = logging.getLogger(__name__)

_MIN_INACTIVE_DAYS = 7
_COOLDOWN_DAYS = 30


def _days_since(iso_str: str) -> int | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).replace(tzinfo=None)
        return (datetime.utcnow() - dt).days
    except (TypeError, ValueError):
        return None


def _on_cooldown(user: dict) -> bool:
    last_sent = user.get("ReEngagementSentOn") or ""
    if not last_sent:
        return False
    try:
        sent_date = datetime.fromisoformat(last_sent).date()
        return (date.today() - sent_date).days < _COOLDOWN_DAYS
    except (TypeError, ValueError):
        return False


def send_re_engagement_emails() -> dict:
    """Scan users and send a one-time re-engagement nudge to the inactive ones.

    Returns {"checked": n, "sent": n, "errors": n, "skipped": n}.
    """
    from application.services import email_service, email_templates

    stats = {"checked": 0, "sent": 0, "errors": 0, "skipped": 0}
    try:
        users = list(user_table_client.query_entities(
            query_filter="PartitionKey eq 'user'"))
    except Exception as e:
        log.warning("re_engagement: could not list users: %s", e)
        return stats

    for user in users:
        stats["checked"] += 1
        try:
            if not user.get("EmailVerified"):
                continue
            if email_prefs.is_marketing_opted_out(user):
                stats["skipped"] += 1
                continue

            days_inactive = _days_since(user.get("LastLoginOn") or "")
            if days_inactive is None or days_inactive < _MIN_INACTIVE_DAYS:
                continue
            if _on_cooldown(user):
                stats["skipped"] += 1
                continue

            email = user.get("Email")
            if not email:
                continue
            name = user.get("UserName", "")
            uid = user.get("RowKey", "")

            ok, info = email_service.send_email(
                to=email,
                subject=email_templates.TEMPLATES["re_engagement"]["subject"],
                html=email_templates.re_engagement_html(
                    name, days_inactive, email_prefs.unsubscribe_url(uid)),
            )
            if ok:
                user["ReEngagementSentOn"] = date.today().isoformat()
                try:
                    user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
                except Exception as ue:
                    log.warning("re_engagement: persist failed: %s", ue)
                stats["sent"] += 1
            else:
                stats["errors"] += 1
                log.warning("re_engagement: send failed for %s: %s", email, info)
        except Exception as e:
            stats["errors"] += 1
            log.warning("re_engagement: error processing user: %s", e)

    return stats
