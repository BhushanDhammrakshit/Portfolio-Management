"""Append-only user event log stored in Azure Table Storage.

Every meaningful user action (login, feature view, plan change, holding
add, etc.) is recorded as a row in the UserEvents table. The admin
Activity dashboard reads these events for KPIs, funnels, and per-user
timelines.

RowKey uses inverted ticks so the *latest* events sort first within each
user's partition — Azure Tables sorts RowKeys lexicographically in
ascending order, so a smaller string = earlier in scan results.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

from flask import request, session

_MAX_TICKS = 9_999_999_999_999  # 13-digit ceiling (~2286 CE)

# Populated by azure_table.py after table init.
_events_client = None


def _client():
    global _events_client
    if _events_client is not None:
        return _events_client
    try:
        from application.services.azure_table import events_table_client
        _events_client = events_table_client
    except Exception:
        _events_client = None
    return _events_client


def track_event(
    user_id: str,
    event: str,
    meta: Optional[dict] = None,
    plan: Optional[str] = None,
) -> None:
    """Fire-and-forget: log one event row. Never raises."""
    if not user_id or not event:
        return
    client = _client()
    if client is None:
        return
    try:
        now_ms = int(time.time() * 1000)
        inverted = str(_MAX_TICKS - now_ms)
        entity = {
            "PartitionKey": user_id,
            "RowKey": f"{inverted}-{event}",
            "Event": event,
            "Plan": plan or (session.get("plan") if session else "") or "",
            "Meta": json.dumps(meta or {}, default=str),
            "EventTime": datetime.now(timezone.utc).isoformat(),
        }
        client.upsert_entity(entity)
    except Exception as e:
        print(f"[event_tracker] track_event failed: {e}")


def track_feature(feature_name: str):
    """Decorator for Flask route functions — logs a feature_viewed event."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            uid = session.get("user_id") if session else None
            if uid:
                track_event(uid, f"feature_viewed:{feature_name}")
            return result
        return wrapper
    return decorator


def track_api(feature_name: str):
    """Decorator for API endpoints — logs on successful (2xx) responses."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            uid = session.get("user_id") if session else None
            if uid:
                status = getattr(result, "status_code", 200) if hasattr(result, "status_code") else 200
                if isinstance(result, tuple):
                    status = result[1] if len(result) > 1 else 200
                if 200 <= status < 300:
                    track_event(uid, f"api_call:{feature_name}")
            return result
        return wrapper
    return decorator


# ── Client-side event ingestion ──────────────────────────────────────

def register_client_track_route(app):
    """Register POST /api/track for client-side JS event logging."""
    from flask import jsonify as _jsonify

    @app.route("/api/track", methods=["POST"])
    def _client_track():
        uid = session.get("user_id")
        if not uid:
            return _jsonify({"ok": False}), 401
        data = request.get_json(silent=True) or {}
        event = (data.get("event") or "").strip()
        if not event or len(event) > 100:
            return _jsonify({"ok": False}), 400
        meta = data.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        # Whitelist meta keys to prevent payload abuse
        safe_meta = {k: str(v)[:200] for k, v in list(meta.items())[:10]}
        track_event(uid, event, safe_meta)
        return _jsonify({"ok": True}), 200


# ── Query helpers for admin dashboard ────────────────────────────────

def get_user_events(user_id: str, limit: int = 50) -> list[dict]:
    """Recent events for one user (latest first thanks to inverted RowKey)."""
    client = _client()
    if not client or not user_id:
        return []
    try:
        rows = client.query_entities(
            query_filter=f"PartitionKey eq '{user_id}'",
            results_per_page=limit,
        )
        out = []
        for r in rows:
            out.append({
                "event": r.get("Event", ""),
                "time": r.get("EventTime", ""),
                "plan": r.get("Plan", ""),
                "meta": r.get("Meta", "{}"),
            })
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        print(f"[event_tracker] get_user_events failed: {e}")
        return []
def _parse_meta(raw: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def compute_time_by_feature(events: list[dict], users: Optional[list[dict]] = None) -> list[dict]:
    """Per-feature: distinct users, total minutes, avg minutes/user, and a
    per-user breakdown (name/email + minutes) so admins can see exactly
    who is spending time on each tool."""
    seconds_by_feature: dict[str, int] = {}
    user_seconds: dict[str, dict[str, int]] = {}  # feature -> user_id -> seconds
    for e in events:
        if e["event"] != "time_spent":
            continue
        meta = _parse_meta(e.get("meta"))
        feature = meta.get("feature") or "unknown"
        try:
            seconds = int(float(meta.get("seconds") or 0))
        except (TypeError, ValueError):
            continue
        if seconds <= 0:
            continue
        uid = e["user_id"]
        seconds_by_feature[feature] = seconds_by_feature.get(feature, 0) + seconds
        by_user = user_seconds.setdefault(feature, {})
        by_user[uid] = by_user.get(uid, 0) + seconds

    user_lookup = {u.get("id"): u for u in (users or []) if u.get("id")}

    out = []
    for feature, total_secs in seconds_by_feature.items():
        by_user = user_seconds.get(feature, {})
        n_users = len(by_user)
        ranked = sorted(by_user.items(), key=lambda kv: kv[1], reverse=True)
        top_users = []
        for uid, secs in ranked:
            u = user_lookup.get(uid, {})
            top_users.append({
                "user_id": uid,
                "name": u.get("name") or "",
                "email": u.get("email") or "",
                "minutes": round(secs / 60, 1),
            })
        out.append({
            "feature": feature,
            "users": n_users,
            "total_minutes": round(total_secs / 60, 1),
            "avg_minutes_per_user": round(total_secs / 60 / max(n_users, 1), 1),
            "top_users": top_users,
        })
    out.sort(key=lambda x: x["total_minutes"], reverse=True)
    return out


def get_all_events_since(hours: int = 24, max_rows: int = 5000) -> list[dict]:
    """All events across all users within the last N hours.

    Uses an EventTime filter so Azure does a full table scan (no
    cross-partition index). Acceptable for admin-only dashboards with
    moderate event volume; for large scale, precompute rollups instead.
    """
    client = _client()
    if not client:
        return []
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
        cutoff_inverted = str(_MAX_TICKS - int(cutoff * 1000))
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = client.query_entities(
            query_filter=f"EventTime ge '{cutoff_iso}'",
        )
        out = []
        for r in rows:
            out.append({
                "user_id": r.get("PartitionKey", ""),
                "event": r.get("Event", ""),
                "time": r.get("EventTime", ""),
                "plan": r.get("Plan", ""),
                "meta": r.get("Meta", "{}"),
            })
            if len(out) >= max_rows:
                break
        return out
    except Exception as e:
        print(f"[event_tracker] get_all_events_since failed: {e}")
        return []


def compute_activity_stats(users: list[dict]) -> dict:
    """Compute feature adoption, funnel, and engagement stats.

    Reads events from the last 30 days and aggregates them.
    Returns a dict ready for the admin Activity tab JSON response.
    """
    events_30d = get_all_events_since(hours=30 * 24, max_rows=50_000)
    events_24h = [e for e in events_30d
                  if e["time"] >= (datetime.now(timezone.utc)
                                   - timedelta(hours=24)).isoformat()]
    events_7d = [e for e in events_30d
                 if e["time"] >= (datetime.now(timezone.utc)
                                  - timedelta(days=7)).isoformat()]

    # DAU / WAU / MAU (distinct users)
    dau = len({e["user_id"] for e in events_24h})
    wau = len({e["user_id"] for e in events_7d})
    mau = len({e["user_id"] for e in events_30d})

    # Feature adoption (count distinct users per feature)
    feature_users: dict[str, set] = {}
    feature_total: dict[str, int] = {}
    for e in events_30d:
        ev = e["event"]
        if ev.startswith("feature_viewed:"):
            feat = ev.split(":", 1)[1]
            feature_users.setdefault(feat, set()).add(e["user_id"])
            feature_total[feat] = feature_total.get(feat, 0) + 1

    adoption = []
    for feat in sorted(feature_users, key=lambda f: len(feature_users[f]), reverse=True):
        adoption.append({
            "feature": feat,
            "users": len(feature_users[feat]),
            "hits": feature_total.get(feat, 0),
            "pct": round(len(feature_users[feat]) / max(mau, 1) * 100, 1),
        })

    # Funnel — count users at each stage (from user entities, not events)
    total = len(users)
    verified = sum(1 for u in users if u.get("email_verified"))
    has_holding = 0
    has_ai = 0
    upgraded = 0
    user_ids_set = {u.get("id") for u in users}

    # Users who triggered specific events ever
    holding_users = set()
    ai_users = set()
    upgrade_users = set()
    for e in events_30d:
        if e["event"] == "holding_added":
            holding_users.add(e["user_id"])
        elif e["event"] in ("ai_query_run", "api_call:ai_assistant"):
            ai_users.add(e["user_id"])
        elif e["event"] == "plan_upgraded":
            upgrade_users.add(e["user_id"])
    has_holding = len(holding_users & user_ids_set)
    has_ai = len(ai_users & user_ids_set)
    upgraded = len(upgrade_users & user_ids_set)

    funnel = [
        {"step": "Signed up", "count": total, "pct": 100},
        {"step": "Email verified", "count": verified,
         "pct": round(verified / max(total, 1) * 100, 1)},
        {"step": "First holding", "count": has_holding,
         "pct": round(has_holding / max(total, 1) * 100, 1)},
        {"step": "First AI query", "count": has_ai,
         "pct": round(has_ai / max(total, 1) * 100, 1)},
        {"step": "Upgraded", "count": upgraded,
         "pct": round(upgraded / max(total, 1) * 100, 1)},
    ]

    # Event type breakdown (top 15 events)
    event_counts: dict[str, int] = {}
    for e in events_30d:
        event_counts[e["event"]] = event_counts.get(e["event"], 0) + 1
    top_events = sorted(event_counts.items(), key=lambda x: x[1], reverse=True)[:15]

    return {
        "dau": dau,
        "wau": wau,
        "mau": mau,
        "feature_adoption": adoption,
        "funnel": funnel,
        "top_events": [{"event": ev, "count": c} for ev, c in top_events],
        "total_events_30d": len(events_30d),
        "time_by_feature": compute_time_by_feature(events_30d, users),
    }
