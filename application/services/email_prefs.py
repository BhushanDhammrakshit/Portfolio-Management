"""Marketing-email opt-out (unsubscribe) helpers.

Non-essential mail (re-engagement, monthly usage summary, future digests)
must check ``is_marketing_opted_out`` before sending. Transactional mail
(OTP, usage-limit alerts, broker-sync failure, plan activation, welcome)
is account-critical and ignores this flag.
"""
from __future__ import annotations

import logging

from itsdangerous import URLSafeSerializer, BadSignature

from application.config import APP_BASE_URL
from application.services.azure_table import user_table_client

log = logging.getLogger(__name__)

_SALT = "email-unsubscribe"


def _serializer() -> URLSafeSerializer:
    from flask import current_app
    return URLSafeSerializer(current_app.secret_key, salt=_SALT)


def unsubscribe_url(user_id: str) -> str:
    """Signed link that opts a user out of non-essential mail — no login needed."""
    token = _serializer().dumps(user_id)
    return f"{APP_BASE_URL}/email/unsubscribe?token={token}"


def user_id_from_token(token: str) -> str | None:
    try:
        return _serializer().loads(token)
    except BadSignature:
        return None


def is_marketing_opted_out(user: dict) -> bool:
    return bool(user.get("MarketingEmailsOptOut"))


def set_opted_out(user_id: str) -> bool:
    from azure.data.tables import UpdateMode
    try:
        results = list(user_table_client.query_entities(
            query_filter=f"PartitionKey eq 'user' and RowKey eq '{user_id}'"))
        if not results:
            return False
        user = results[0]
        user["MarketingEmailsOptOut"] = True
        user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
        return True
    except Exception as e:
        log.warning("email_prefs: could not persist opt-out for %s: %s", user_id, e)
        return False
