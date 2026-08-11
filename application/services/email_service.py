"""Pluggable transactional email service.

Providers:
    - brevo   : Brevo (formerly Sendinblue) REST API. Free 300/day forever.
    - console : Print to terminal. Used as a safe fallback when no provider
                is configured. Also used during local dev to test flows
                without burning real email quota.

Public surface:
    - send_email(to, subject, html, text=None, ...) -> (ok: bool, info: str)
    - send_otp(to, name, code, ttl_minutes=10)      -> (ok: bool, info: str)
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import requests

from application.config import (APP_NAME, BREVO_API_KEY, EMAIL_FROM,
                                EMAIL_FROM_NAME, EMAIL_PROVIDER,
                                EMAIL_REPLY_TO)

log = logging.getLogger(__name__)

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def is_configured() -> bool:
    """True if the active provider has the credentials it needs."""
    if EMAIL_PROVIDER == "brevo":
        return bool(BREVO_API_KEY and EMAIL_FROM)
    return True  # console always works


def provider_label() -> str:
    return EMAIL_PROVIDER


# ── Provider implementations ────────────────────────────────────────────

def _send_via_console(*, to: str, subject: str, html: str,
                      text: Optional[str]) -> Tuple[bool, str]:
    print("=" * 60)
    print(f"[email:console] TO     : {to}")
    print(f"[email:console] FROM   : {EMAIL_FROM_NAME} <{EMAIL_FROM or 'no-from-set'}>")
    print(f"[email:console] SUBJECT: {subject}")
    print("-" * 60)
    print(text or "(html body — see HTML)")
    print("=" * 60)
    return True, "console"


def _send_via_brevo(*, to: str, subject: str, html: str,
                    text: Optional[str]) -> Tuple[bool, str]:
    if not BREVO_API_KEY:
        return False, "BREVO_API_KEY not set"
    if not EMAIL_FROM:
        return False, "EMAIL_FROM not set"

    payload = {
        "sender": {"name": EMAIL_FROM_NAME, "email": EMAIL_FROM},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
    }
    if text:
        payload["textContent"] = text
    if EMAIL_REPLY_TO:
        payload["replyTo"] = {"email": EMAIL_REPLY_TO}

    try:
        r = requests.post(_BREVO_URL, json=payload, timeout=15, headers={
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": BREVO_API_KEY,
        })
    except requests.RequestException as e:
        log.warning("[email:brevo] network error: %s", e)
        return False, f"network error: {e}"

    if r.status_code in (200, 201, 202):
        try:
            mid = r.json().get("messageId", "")
        except Exception:
            mid = ""
        return True, f"brevo ok ({mid})"

    snippet = (r.text or "")[:300].replace("\n", " ")
    log.warning("[email:brevo] HTTP %s: %s", r.status_code, snippet)
    return False, f"brevo HTTP {r.status_code}: {snippet}"


# ── Public API ──────────────────────────────────────────────────────────

def send_email(*, to: str, subject: str, html: str,
               text: Optional[str] = None) -> Tuple[bool, str]:
    """Send a single transactional email. Returns (ok, info)."""
    if not to:
        return False, "no recipient"

    provider = EMAIL_PROVIDER
    if provider == "brevo":
        return _send_via_brevo(to=to, subject=subject, html=html, text=text)
    return _send_via_console(to=to, subject=subject, html=html, text=text)


# ── OTP email template ──────────────────────────────────────────────────

def _otp_html(name: str, code: str, ttl_minutes: int) -> str:
    safe_name = (name or "there").split("@")[0][:60]
    return f"""\
<!doctype html>
<html><body style="margin:0;padding:0;background:#f6f9fc;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937">
  <div style="max-width:540px;margin:0 auto;padding:24px">
    <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#1e40af 0%,#2563eb 100%);padding:24px 28px;color:#fff">
        <div style="font-size:13px;opacity:.85;letter-spacing:.08em;text-transform:uppercase">Verify your email</div>
        <div style="font-size:22px;font-weight:800;margin-top:4px">{APP_NAME}</div>
      </div>
      <div style="padding:28px">
        <p style="margin:0 0 14px;font-size:15px;line-height:1.55">Hi <strong>{safe_name}</strong>,</p>
        <p style="margin:0 0 18px;font-size:15px;line-height:1.55">
          Use the one-time code below to verify your email address and finish
          setting up your account. This code expires in <strong>{ttl_minutes} minutes</strong>.
        </p>
        <div style="text-align:center;margin:24px 0">
          <div style="display:inline-block;padding:14px 26px;background:#f3f4f6;border:1px dashed #9ca3af;border-radius:10px;font-family:Consolas,Menlo,monospace;font-size:30px;font-weight:800;letter-spacing:.5em;color:#111827">
            {code}
          </div>
        </div>
        <div style="background:#fef9c3;border:1px solid #fde047;border-radius:8px;padding:10px 14px;font-size:13px;color:#713f12;margin-bottom:18px">
          <strong>Can't see this email?</strong> Please check your <strong>Spam</strong> /
          <strong>Junk</strong> / <strong>Promotions</strong> folder. If you find it there,
          mark it as "Not spam" so future codes land in your inbox.
        </div>
        <p style="margin:0 0 14px;font-size:13px;color:#6b7280;line-height:1.55">
          If you didn't sign up, you can safely ignore this email \u2014 someone may
          have entered your address by mistake.
        </p>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:22px 0">
        <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.55">
          \u00a9 {APP_NAME}. This is an automated message, please don't reply.
        </p>
      </div>
    </div>
  </div>
</body></html>"""


def _otp_text(name: str, code: str, ttl_minutes: int) -> str:
    safe_name = (name or "there").split("@")[0][:60]
    return (
        f"Hi {safe_name},\n\n"
        f"Your {APP_NAME} verification code is: {code}\n\n"
        f"This code expires in {ttl_minutes} minutes.\n\n"
        "Can't see this email? Please check your Spam / Junk / Promotions folder.\n"
        "If you find it there, mark it as 'Not spam' so future codes land in your inbox.\n\n"
        "If you didn't sign up, you can safely ignore this email.\n"
    )


def send_otp(*, to: str, name: str, code: str,
             ttl_minutes: int = 10) -> Tuple[bool, str]:
    """High-level: send a verification OTP to the given user."""
    subject = f"Your {APP_NAME} verification code: {code}"
    html = _otp_html(name, code, ttl_minutes)
    text = _otp_text(name, code, ttl_minutes)
    return send_email(to=to, subject=subject, html=html, text=text)
