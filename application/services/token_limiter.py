"""Per-user daily token limiter for the Algo Helper chatbot.

Tracks how many AI tokens each user has consumed today and resets at
local midnight. Counts are persisted to a JSON file so they survive
process restarts within the same day.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date
from typing import Dict, Tuple

DAILY_LIMIT = 10_000

_LOCK = threading.Lock()
_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "_token_usage.json",
)


def _today() -> str:
    return date.today().isoformat()


def _load() -> Dict[str, dict]:
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: Dict[str, dict]) -> None:
    try:
        with open(_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _entry_for(data: Dict[str, dict], user: str) -> dict:
    today = _today()
    e = data.get(user)
    if not e or e.get("date") != today:
        e = {"date": today, "tokens": 0}
        data[user] = e
    return e


def get_usage(user: str) -> Tuple[int, int]:
    """Return (used, limit) for the user today."""
    if not user:
        return 0, DAILY_LIMIT
    with _LOCK:
        data = _load()
        e = _entry_for(data, user)
        return int(e.get("tokens", 0)), DAILY_LIMIT


def remaining(user: str) -> int:
    used, limit = get_usage(user)
    return max(0, limit - used)


def can_consume(user: str, estimated: int = 1) -> bool:
    """Quick check (does not reserve)."""
    return remaining(user) >= estimated


def add_usage(user: str, tokens: int) -> Tuple[int, int]:
    """Add tokens to today's count. Returns updated (used, limit)."""
    if not user or tokens <= 0:
        return get_usage(user)
    with _LOCK:
        data = _load()
        e = _entry_for(data, user)
        e["tokens"] = int(e.get("tokens", 0)) + int(tokens)
        _save(data)
        return int(e["tokens"]), DAILY_LIMIT
