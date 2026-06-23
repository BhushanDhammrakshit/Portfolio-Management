"""Admin endpoints for the RAG layer.

All endpoints require login and an admin email check (controlled by the
ADMIN_EMAILS env var, comma-separated). Safe defaults: if no admin emails
are set, only the very first registered user is treated as admin.
"""
from __future__ import annotations

import logging
import os
from functools import wraps

from flask import Blueprint, jsonify, request, session

from application.services.rag import retriever, store
from application.services.rag.ingest import runner

log = logging.getLogger(__name__)

rag_admin_bp = Blueprint("rag_admin", __name__, url_prefix="/admin/rag")

_ADMIN_EMAILS = {
    e.strip().lower()
    for e in (os.getenv("ADMIN_EMAILS") or "").split(",")
    if e.strip()
}


def _is_admin(email: str) -> bool:
    if not email:
        return False
    if not _ADMIN_EMAILS:
        # No allow-list configured: only allow if the user is logged in.
        # (You should set ADMIN_EMAILS in .env for production.)
        return True
    return email.lower() in _ADMIN_EMAILS


def _require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "email" not in session:
            return jsonify({"error": "auth required"}), 401
        if not _is_admin(session.get("email", "")):
            return jsonify({"error": "admin required"}), 403
        return fn(*args, **kwargs)
    return wrapper


@rag_admin_bp.get("/status")
@_require_admin
def status():
    return jsonify(store.stats())


@rag_admin_bp.post("/ingest")
@_require_admin
def ingest():
    """Trigger ingestion. Body: {symbols: ["RELIANCE.NS", ...]} or empty for all."""
    data = request.get_json(silent=True) or {}
    syms = data.get("symbols") or []
    if syms:
        stats = runner.run_for_symbols(syms)
    else:
        stats = runner.run_daily()
    return jsonify({"ok": True, "stats": stats})


@rag_admin_bp.get("/preview")
@_require_admin
def preview():
    """Preview the RAG context for a symbol. ?symbol=RELIANCE.NS&q=earnings"""
    sym = request.args.get("symbol", "").strip()
    q = request.args.get("q", "").strip() or None
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    block, sources = retriever.build_context(sym, query=q)
    return jsonify({"symbol": sym, "block": block, "sources": sources})


@rag_admin_bp.post("/cleanup")
@_require_admin
def cleanup():
    days = int(request.args.get("days", "90"))
    n = store.cleanup_old(days=days)
    return jsonify({"ok": True, "deleted": n})
