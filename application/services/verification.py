"""Email-verification OTP service.

Generates 6-digit one-time codes, stores them in Azure Tables with TTL,
enforces resend cooldown + max attempts, marks user as verified on success.

Storage: table EMAIL_VERIFICATION_TABLE
    PartitionKey = email (lowercased)
    RowKey       = 'current'  (one active record per email)
    Properties   :
        CodeHash         : sha256 of the 6-digit code (never store plaintext)
        ExpiresAt        : ISO timestamp
        AttemptsLeft     : int (decrements on wrong code)
        LastSentAt       : ISO timestamp (for cooldown)
        UserId           : RowKey of the user entity in user_table
        Verified         : bool

Public API:
    - start(email, user_id, name) -> (ok, info)   send a fresh OTP
    - resend(email)               -> (ok, info)   subject to cooldown
    - verify(email, code)         -> (ok, info)   on success: marks user verified
    - is_user_verified(user_entity) -> bool
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode

from application.config import (AZURE_TABLE_CONN_STR,
                                EMAIL_VERIFICATION_TABLE,
                                OTP_MAX_ATTEMPTS,
                                OTP_RESEND_COOLDOWN_SECONDS, OTP_TTL_MINUTES)
from application.services import email_service

log = logging.getLogger(__name__)

_ROW_KEY = "current"
_table_client = None
_init_attempted = False


def _init():
    global _table_client, _init_attempted
    if _init_attempted:
        return
    _init_attempted = True
    if not AZURE_TABLE_CONN_STR:
        log.warning("[verification] AZURE_TABLE_CONN_STR missing")
        return
    try:
        svc = TableServiceClient.from_connection_string(conn_str=AZURE_TABLE_CONN_STR)
        try:
            svc.create_table_if_not_exists(table_name=EMAIL_VERIFICATION_TABLE)
        except ResourceExistsError:
            pass
        _table_client = svc.get_table_client(table_name=EMAIL_VERIFICATION_TABLE)
    except Exception as e:
        log.warning("[verification] init failed: %s", e)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _generate_code() -> str:
    """Return a cryptographically-random 6-digit numeric code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _get(email: str) -> Optional[dict]:
    _init()
    if _table_client is None:
        return None
    try:
        return dict(_table_client.get_entity(_normalize_email(email), _ROW_KEY))
    except ResourceNotFoundError:
        return None
    except Exception as e:
        log.warning("[verification] get %s: %s", email, e)
        return None


def _put(entity: dict) -> bool:
    _init()
    if _table_client is None:
        return False
    try:
        _table_client.upsert_entity(entity, mode=UpdateMode.REPLACE)
        return True
    except Exception as e:
        log.warning("[verification] put %s: %s", entity.get("PartitionKey"), e)
        return False


def _delete(email: str) -> None:
    _init()
    if _table_client is None:
        return
    try:
        _table_client.delete_entity(_normalize_email(email), _ROW_KEY)
    except Exception:
        pass


# ── Public API ──────────────────────────────────────────────────────────

def start(email: str, user_id: str, name: str = "") -> Tuple[bool, str]:
    """Generate a fresh OTP, persist it, and email it. Returns (ok, info)."""
    norm = _normalize_email(email)
    if not norm:
        return False, "missing email"

    code = _generate_code()
    now = _now()
    entity = {
        "PartitionKey": norm,
        "RowKey": _ROW_KEY,
        "CodeHash": _hash_code(code),
        "ExpiresAt": (now + timedelta(minutes=OTP_TTL_MINUTES)).isoformat(),
        "AttemptsLeft": OTP_MAX_ATTEMPTS,
        "LastSentAt": now.isoformat(),
        "UserId": user_id or "",
        "Verified": False,
    }
    if not _put(entity):
        return False, "could not store verification record"

    ok, info = email_service.send_otp(
        to=norm, name=name, code=code, ttl_minutes=OTP_TTL_MINUTES)
    if not ok:
        log.warning("[verification] start: send failed for %s: %s", norm, info)
        return False, f"could not send email: {info}"
    log.info("[verification] sent OTP to %s (%s)", norm, info)
    return True, "sent"


def resend(email: str) -> Tuple[bool, str]:
    """Re-send a fresh OTP, subject to cooldown. Returns (ok, info)."""
    norm = _normalize_email(email)
    rec = _get(norm)
    if not rec:
        return False, "no pending verification — please sign up again"

    # Cooldown check
    try:
        last_sent = datetime.fromisoformat(rec["LastSentAt"].replace("Z", "+00:00"))
    except Exception:
        last_sent = _now() - timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS + 1)
    elapsed = (_now() - last_sent).total_seconds()
    if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
        wait = int(OTP_RESEND_COOLDOWN_SECONDS - elapsed)
        return False, f"please wait {wait}s before requesting another code"

    return start(norm, rec.get("UserId", ""), name=norm)


def verify(email: str, code: str) -> Tuple[bool, str]:
    """Validate a submitted OTP. On success, marks user entity as verified."""
    norm = _normalize_email(email)
    code = (code or "").strip().replace(" ", "")
    if not code or len(code) != 6 or not code.isdigit():
        return False, "enter the 6-digit code"

    rec = _get(norm)
    if not rec:
        return False, "no verification in progress for this email"

    # Expiry
    try:
        expires = datetime.fromisoformat(rec["ExpiresAt"].replace("Z", "+00:00"))
    except Exception:
        expires = _now() - timedelta(minutes=1)
    if _now() >= expires:
        _delete(norm)
        return False, "code expired — request a new one"

    # Attempts
    attempts_left = int(rec.get("AttemptsLeft", 0))
    if attempts_left <= 0:
        _delete(norm)
        return False, "too many wrong attempts — request a new code"

    # Compare
    if _hash_code(code) != rec.get("CodeHash"):
        rec["AttemptsLeft"] = attempts_left - 1
        _put(rec)
        if rec["AttemptsLeft"] <= 0:
            return False, "incorrect code — too many attempts, request a new one"
        return False, f"incorrect code — {rec['AttemptsLeft']} attempt(s) left"

    # Success: mark user entity verified, then drop the record
    user_id = rec.get("UserId", "")
    _mark_user_verified(user_id, norm)
    _delete(norm)
    return True, "verified"


def _mark_user_verified(user_id: str, email: str) -> None:
    """Update the user entity in the user_info table."""
    try:
        from application.services.azure_table import user_table_client
        # Find by RowKey first; fall back to email query
        if user_id:
            try:
                ent = user_table_client.get_entity("user", user_id)
                ent["EmailVerified"] = True
                ent["EmailVerifiedAt"] = _now().isoformat()
                user_table_client.upsert_entity(ent, mode=UpdateMode.MERGE)
                return
            except Exception:
                pass
        rows = list(user_table_client.query_entities(
            query_filter=f"Email eq '{email}'"))
        if rows:
            ent = dict(rows[0])
            ent["EmailVerified"] = True
            ent["EmailVerifiedAt"] = _now().isoformat()
            user_table_client.upsert_entity(ent, mode=UpdateMode.MERGE)
    except Exception as e:
        log.warning("[verification] mark verified failed: %s", e)


def is_user_verified(user_entity: dict) -> bool:
    """Backward-compatible verified check.

    Users created BEFORE this feature shipped won't have an EmailVerified
    field at all — they're treated as verified to avoid locking them out.
    Users created AFTER this code is in place will always have the field
    set (False initially, True after verification).
    """
    if user_entity is None:
        return False
    if "EmailVerified" not in user_entity:
        return True  # legacy account
    val = user_entity.get("EmailVerified")
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "yes")
