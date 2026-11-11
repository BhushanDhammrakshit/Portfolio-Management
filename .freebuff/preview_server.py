"""Local preview server for Phase 4 UI verification.

Boots the real Flask app and injects a demo session so the authenticated pages
can be opened in a browser without going through login/OTP.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("RAG_ENABLE_SCHEDULER", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import session  # noqa: E402

from application import app  # noqa: E402
from application.services import plans  # noqa: E402

# Preview as an Elite user (default) so the gated analytics pages render fully.
_PREVIEW_PLAN = os.environ.get("PREVIEW_PLAN", "elite")
plans.current_plan = lambda: plans.PLANS.get(_PREVIEW_PLAN, plans.PLANS["elite"])


@app.before_request
def _preview_session():
    if not session.get("email"):
        session["email"] = "preview@example.com"
        session["name"] = "Preview"
        session["user_id"] = "preview-user"
        session["persona"] = "trader"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PREVIEW_PORT", "5055")),
            debug=False, use_reloader=False)
