"""Razorpay integration helpers (orders + signature verification + webhook dedupe)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from typing import Any, Optional

import requests

from application import config

_API_BASE = "https://api.razorpay.com/v1"
_TIMEOUT = 12
_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "_billing_events.json",
)
_LOCK = threading.Lock()


def is_enabled() -> bool:
    return bool(config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET)


def public_key_id() -> str:
    return config.RAZORPAY_KEY_ID


def _auth_header() -> dict:
    token = f"{config.RAZORPAY_KEY_ID}:{config.RAZORPAY_KEY_SECRET}".encode("utf-8")
    return {
        "Authorization": "Basic " + base64.b64encode(token).decode("ascii"),
        "Content-Type": "application/json",
    }


def create_order(amount_paise: int, receipt: str, notes: Optional[dict[str, Any]] = None) -> dict:
    if not is_enabled():
        raise RuntimeError("Razorpay keys are not configured")
    payload = {
        "amount": int(amount_paise),
        "currency": config.RAZORPAY_CURRENCY or "INR",
        "receipt": receipt[:40],
        "notes": notes or {},
    }
    r = requests.post(
        _API_BASE + "/orders",
        headers=_auth_header(),
        data=json.dumps(payload),
        timeout=_TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Razorpay order create failed: HTTP {r.status_code} {r.text[:200]}")
    body = r.json() or {}
    if not body.get("id"):
        raise RuntimeError("Razorpay order create failed: missing id")
    return body


def fetch_payment(payment_id: str) -> dict:
    if not is_enabled():
        raise RuntimeError("Razorpay keys are not configured")
    r = requests.get(_API_BASE + f"/payments/{payment_id}", headers=_auth_header(), timeout=_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"Razorpay payment fetch failed: HTTP {r.status_code} {r.text[:200]}")
    return r.json() or {}


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    if not config.RAZORPAY_KEY_SECRET:
        return False
    msg = f"{order_id}|{payment_id}".encode("utf-8")
    digest = hmac.new(
        config.RAZORPAY_KEY_SECRET.encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, (signature or "").strip())


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    if not config.RAZORPAY_WEBHOOK_SECRET:
        return False
    digest = hmac.new(
        config.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, (signature or "").strip())


def _load_store() -> dict:
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_store(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
        tmp = _STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, separators=(",", ":"))
        os.replace(tmp, _STORE_PATH)
    except Exception:
        pass


def remember_order(order_id: str, meta: dict[str, Any]) -> None:
    with _LOCK:
        data = _load_store()
        orders = data.setdefault("orders", {})
        orders[order_id] = {
            **(meta or {}),
            "ts": int(time.time()),
        }
        # keep latest 500 orders
        if len(orders) > 500:
            for k, _ in sorted(orders.items(), key=lambda kv: kv[1].get("ts", 0))[:-500]:
                orders.pop(k, None)
        _save_store(data)


def recall_order(order_id: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        data = _load_store()
        return (data.get("orders") or {}).get(order_id)


def mark_event_processed(event_id: str) -> bool:
    """Return True when marking new event, False if already processed."""
    if not event_id:
        return False
    with _LOCK:
        data = _load_store()
        events = data.setdefault("events", {})
        if event_id in events:
            return False
        events[event_id] = int(time.time())
        # keep latest 5000 webhook IDs
        if len(events) > 5000:
            for k, _ in sorted(events.items(), key=lambda kv: kv[1])[:-5000]:
                events.pop(k, None)
        _save_store(data)
        return True
