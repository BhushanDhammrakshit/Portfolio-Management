"""Monthly AI-usage recap email for Pro/Elite users.

Runs once a month (see the scheduler wired in application/__init__.py),
on the 1st, summarizing the just-completed month's AI usage. Each
calendar month notifies at most once, tracked via ``UsageSummarySentMonth``
on the user's Azure Table entity. Non-essential: honors the
``MarketingEmailsOptOut`` flag and includes an unsubscribe link.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from azure.data.tables import UpdateMode

from application.services.azure_table import user_table_client
from application.services import plans, token_limiter, email_prefs

log = logging.getLogger(__name__)


def _prev_month_label() -> str:
    first_of_this_month = date.today().replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    return last_month_end.strftime("%Y-%m")


def send_monthly_usage_summaries() -> dict:
    """Send the monthly recap to Pro/Elite users, at most once per calendar month.

    Returns {"checked": n, "sent": n, "errors": n, "skipped": n}.
    """
    from application.services import email_service, email_templates

    stats = {"checked": 0, "sent": 0, "errors": 0, "skipped": 0}
    month_label = _prev_month_label()
    try:
        users = list(user_table_client.query_entities(
            query_filter="PartitionKey eq 'user'"))
    except Exception as e:
        log.warning("usage_summary: could not list users: %s", e)
        return stats

    for user in users:
        stats["checked"] += 1
        try:
            plan_id = (user.get("Plan") or "free").lower()
            if plan_id not in ("pro", "elite"):
                continue
            if email_prefs.is_marketing_opted_out(user):
                stats["skipped"] += 1
                continue
            if user.get("UsageSummarySentMonth") == month_label:
                stats["skipped"] += 1
                continue

            email = user.get("Email")
            if not email:
                continue
            uid = user.get("RowKey", "")
            name = user.get("UserName", "")
            plan = plans.get_plan(plan_id)
            limits = plan.get("limits", {})
            ai_used = plans.get_usage(uid).get("ai_single", 0)
            tokens_used, tokens_limit = token_limiter.get_usage(email)
            # Elite has no token cap — show a generous reference ceiling instead of crashing.
            tokens_limit_display = tokens_limit if tokens_limit is not None else max(tokens_used * 2, 100_000)

            ok, info = email_service.send_email(
                to=email,
                subject=email_templates.TEMPLATES["usage_summary"]["subject"],
                html=email_templates.usage_summary_html(
                    name, plan.get("name", plan_id.title()),
                    ai_used=ai_used, ai_limit=limits.get("ai_single") or 1,
                    tokens_used=tokens_used, tokens_limit=tokens_limit_display,
                    unsubscribe_url=email_prefs.unsubscribe_url(uid),
                ),
            )
            if ok:
                user["UsageSummarySentMonth"] = month_label
                try:
                    user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
                except Exception as ue:
                    log.warning("usage_summary: persist failed: %s", ue)
                stats["sent"] += 1
            else:
                stats["errors"] += 1
                log.warning("usage_summary: send failed for %s: %s", email, info)
        except Exception as e:
            stats["errors"] += 1
            log.warning("usage_summary: error processing user: %s", e)

    return stats
