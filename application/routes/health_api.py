"""Lightweight health + cache stats endpoints.

GET /api/_health           liveness probe (cheap, no auth)
GET /api/_health/cache     cache backend + budget + quote-cache stats
                           (requires logged-in session)
"""
from __future__ import annotations

import time

from flask import Blueprint, jsonify, session

from application.services import cache as shared_cache
from application.services import quote_cache

health_api = Blueprint("health_api", __name__)

_START = time.time()


@health_api.route("/api/_health")
def health():
    return jsonify({
        "ok": True,
        "uptime_s": int(time.time() - _START),
        "cache_backend": "redis" if shared_cache.is_redis_enabled() else "local",
    })


@health_api.route("/api/_health/cache")
def health_cache():
    if "email" not in session:
        return jsonify({"error": "auth"}), 401
    try:
        budget = shared_cache.budget_stats()
    except Exception as e:  # noqa: BLE001
        budget = {"error": str(e)}
    return jsonify({
        "backend": "redis" if shared_cache.is_redis_enabled() else "local",
        "budget": budget,
        "quote_cache": quote_cache.stats(),
        "uptime_s": int(time.time() - _START),
    })
