"""Referral program — invite friends, earn credits.

How it works:
1. Every user gets a unique referral code (generated on first access).
2. New users can sign up with a referral code (via URL param or form field).
3. When the referred user makes their first paid purchase (Pro/Elite),
   the referrer earns a credit (₹80 for Pro, ₹200 for Elite).
4. Credits are held in "pending" status for 14 days (cooling period).
5. After 14 days they become "credited" and reduce the referrer's next bill.

Data is stored in Azure Table Storage (table: Referrals).
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from application import config

log = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────
_TABLE_NAME = os.getenv("REFERRAL_TABLE", "Referrals")
_COOLDOWN_DAYS = 14
_MAX_REFERRALS_PER_MONTH = 50

REWARDS = {
    "pro": 80,      # ₹80 for Pro referral
    "elite": 200,   # ₹200 for Elite referral
}


# ── Azure Table client ──────────────────────────────────────────────────
_table_client = None


def _get_table():
    global _table_client
    if _table_client is not None:
        return _table_client
    if not config.AZURE_TABLE_CONN_STR:
        return None
    try:
        from azure.data.tables import TableServiceClient
        svc = TableServiceClient.from_connection_string(conn_str=config.AZURE_TABLE_CONN_STR)
        try:
            svc.create_table_if_not_exists(table_name=_TABLE_NAME)
        except Exception:
            pass
        _table_client = svc.get_table_client(table_name=_TABLE_NAME)
        return _table_client
    except Exception as e:
        log.warning("referral: table init failed: %s", e)
        return None


# ── Referral code generation ────────────────────────────────────────────

def generate_code(user_id: str, name: str = "") -> str:
    """Generate a deterministic, short referral code for a user.

    Format: FC-<NAME_PREFIX>-<4CHARS>  e.g. FC-BHUSHAN-7X3K
    Deterministic so calling multiple times returns the same code.
    """
    # Take first 6 chars of name (uppercase, alphanumeric only)
    prefix = "".join(c for c in (name or "USER").upper() if c.isalnum())[:6]
    # Hash user_id to get a stable 4-char suffix
    h = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:4].upper()
    return f"FC-{prefix}-{h}"


def get_or_create_code(user_entity: dict) -> str:
    """Get the user's referral code, generating and persisting if needed."""
    code = user_entity.get("ReferralCode")
    if code:
        return code
    # Generate and persist
    user_id = user_entity.get("RowKey", "")
    name = user_entity.get("UserName", "")
    code = generate_code(user_id, name)
    try:
        from azure.data.tables import UpdateMode
        from application.services.azure_table import user_table_client
        user_entity["ReferralCode"] = code
        user_table_client.update_entity(entity=user_entity, mode=UpdateMode.MERGE)
    except Exception as e:
        log.warning("referral: could not persist code: %s", e)
    return code


# ── Referral linking (on signup) ────────────────────────────────────────

def resolve_referrer(code: str) -> Optional[dict]:
    """Look up the user who owns this referral code. Returns user entity or None."""
    if not code:
        return None
    code = code.strip().upper()
    try:
        from application.services.azure_table import user_table_client
        # Query by ReferralCode field
        safe_code = code.replace("'", "''")
        results = list(user_table_client.query_entities(
            query_filter=f"ReferralCode eq '{safe_code}'"))
        return results[0] if results else None
    except Exception as e:
        log.warning("referral: resolve_referrer failed: %s", e)
        return None


def link_referral(referrer_user_id: str, referred_user_id: str,
                  referred_email: str, referral_code: str) -> bool:
    """Record that a new user signed up via a referral code.

    Called during signup. The credit is only created later when the
    referred user makes a purchase.
    """
    table = _get_table()
    if not table:
        return False
    if referrer_user_id == referred_user_id:
        return False  # self-referral
    try:
        entity = {
            "PartitionKey": referrer_user_id,
            "RowKey": referred_user_id,
            "ReferralCode": referral_code,
            "ReferredEmail": referred_email,
            "Status": "linked",  # linked → pending → credited → paid | voided
            "CreditAmount": 0,
            "PlanPurchased": "",
            "PurchaseDate": "",
            "CreditDate": "",
            "PaidDate": "",
            "LinkedDate": date.today().isoformat(),
        }
        table.upsert_entity(entity=entity)
        return True
    except Exception as e:
        log.warning("referral: link_referral failed: %s", e)
        return False


# ── Credit creation (on purchase) ───────────────────────────────────────

def create_credit(referred_user_id: str, plan_id: str) -> Optional[int]:
    """Called when a referred user makes their first paid purchase.

    Finds the referral link and sets the credit to "pending" with a
    14-day cooldown. Returns the credit amount or None if no referral.
    """
    table = _get_table()
    if not table:
        return None
    plan_id = (plan_id or "").lower()
    amount = REWARDS.get(plan_id)
    if not amount:
        return None

    # Find the referral record where this user is the referred party
    try:
        # RowKey = referred_user_id, scan all partitions
        results = list(table.query_entities(
            query_filter=f"RowKey eq '{referred_user_id}' and Status eq 'linked'"))
        if not results:
            return None
        ref = results[0]
    except Exception as e:
        log.warning("referral: create_credit query failed: %s", e)
        return None

    # Check monthly cap for the referrer
    referrer_id = ref["PartitionKey"]
    this_month = date.today().strftime("%Y-%m")
    try:
        month_refs = list(table.query_entities(
            query_filter=f"PartitionKey eq '{referrer_id}' and Status ne 'linked'"))
        month_count = sum(1 for r in month_refs
                         if (r.get("PurchaseDate") or "").startswith(this_month))
        if month_count >= _MAX_REFERRALS_PER_MONTH:
            log.info("referral: monthly cap reached for %s", referrer_id)
            return None
    except Exception:
        pass

    # Update the referral to pending
    try:
        from azure.data.tables import UpdateMode
        ref["Status"] = "pending"
        ref["PlanPurchased"] = plan_id
        ref["CreditAmount"] = amount
        ref["PurchaseDate"] = date.today().isoformat()
        ref["CreditDate"] = (date.today() + timedelta(days=_COOLDOWN_DAYS)).isoformat()
        table.update_entity(entity=ref, mode=UpdateMode.REPLACE)
        log.info("referral: credit ₹%d pending for referrer %s (referred: %s, plan: %s)",
                 amount, referrer_id, referred_user_id, plan_id)
        return amount
    except Exception as e:
        log.warning("referral: create_credit update failed: %s", e)
        return None


# ── Credit maturation (daily job) ───────────────────────────────────────

def mature_pending_credits() -> int:
    """Move credits past their cooldown from 'pending' to 'credited'.

    Called daily by the precompute scheduler. Returns count of matured.
    """
    table = _get_table()
    if not table:
        return 0
    today = date.today().isoformat()
    matured = 0
    try:
        from azure.data.tables import UpdateMode
        pending = list(table.query_entities(
            query_filter="Status eq 'pending'"))
        for ref in pending:
            credit_date = ref.get("CreditDate", "")
            if credit_date and credit_date <= today:
                ref["Status"] = "credited"
                table.update_entity(entity=ref, mode=UpdateMode.REPLACE)
                matured += 1
                log.info("referral: matured ₹%s for %s",
                         ref.get("CreditAmount"), ref["PartitionKey"])
    except Exception as e:
        log.warning("referral: mature_pending_credits failed: %s", e)
    return matured


# ── Read helpers (for UI) ───────────────────────────────────────────────

def get_referral_stats(user_id: str) -> dict:
    """Get referral stats for a user (for the billing/profile page)."""
    table = _get_table()
    if not table:
        return {"code": "", "total_earned": 0, "pending": 0, "credited": 0,
                "referrals": [], "count": 0}

    # Get referral code from user entity
    code = ""
    try:
        from application.services.azure_table import user_table_client
        users = list(user_table_client.query_entities(
            query_filter=f"RowKey eq '{user_id}'"))
        if users:
            code = get_or_create_code(users[0])
    except Exception:
        pass

    # Get all referrals by this user
    referrals = []
    total_earned = 0
    pending = 0
    credited = 0
    try:
        results = list(table.query_entities(
            query_filter=f"PartitionKey eq '{user_id}'"))
        for r in results:
            status = r.get("Status", "linked")
            amount = int(r.get("CreditAmount") or 0)
            item = {
                "email": _mask_email(r.get("ReferredEmail", "")),
                "plan": r.get("PlanPurchased", ""),
                "amount": amount,
                "status": status,
                "purchase_date": r.get("PurchaseDate", ""),
                "credit_date": r.get("CreditDate", ""),
            }
            referrals.append(item)
            if status == "pending":
                pending += amount
            elif status in ("credited", "paid"):
                credited += amount
                total_earned += amount
    except Exception as e:
        log.warning("referral: get_referral_stats failed: %s", e)

    return {
        "code": code,
        "total_earned": total_earned,
        "pending": pending,
        "credited": credited,
        "referrals": referrals,
        "count": len(referrals),
    }


def get_available_credit(user_id: str) -> int:
    """Get total available (matured) credit for a user that hasn't been used."""
    table = _get_table()
    if not table:
        return 0
    try:
        results = list(table.query_entities(
            query_filter=f"PartitionKey eq '{user_id}' and Status eq 'credited'"))
        return sum(int(r.get("CreditAmount") or 0) for r in results)
    except Exception:
        return 0


def use_credit(user_id: str, amount: int) -> int:
    """Consume up to ``amount`` from available credits. Returns amount consumed."""
    table = _get_table()
    if not table or amount <= 0:
        return 0
    try:
        from azure.data.tables import UpdateMode
        results = list(table.query_entities(
            query_filter=f"PartitionKey eq '{user_id}' and Status eq 'credited'"))
        # Sort oldest first
        results.sort(key=lambda r: r.get("CreditDate", ""))
        consumed = 0
        for ref in results:
            if consumed >= amount:
                break
            credit = int(ref.get("CreditAmount") or 0)
            ref["Status"] = "paid"
            ref["PaidDate"] = date.today().isoformat()
            table.update_entity(entity=ref, mode=UpdateMode.REPLACE)
            consumed += credit
        return min(consumed, amount)
    except Exception as e:
        log.warning("referral: use_credit failed: %s", e)
        return 0


def _mask_email(email: str) -> str:
    """Mask email for privacy: b***n@gmail.com"""
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"
