"""Entry point for the Fyers WebSocket worker.

Run this in a process that's **separate** from gunicorn — never inside a
web worker (otherwise every gunicorn worker opens its own socket).

Local dev:
    python scripts/run_fyers_ws.py

Azure deployment options:
  * **Continuous WebJob** — same App Service plan, no extra cost.
    Drop ``scripts/run_fyers_ws.py`` + ``run.cmd`` + ``settings.job``
    (provided alongside) into ``App_Data/jobs/continuous/fyers_ws/``.
  * Separate Azure Container App / Container Instance for true
    isolation when you outgrow the WebJob model.

Reads the same env vars as the Flask app, so secrets only live in one
place (App Service Configuration).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

# Make ``application`` importable when the script is launched from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env so local runs match the Flask app's behaviour.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

# Mark this process as the WS worker before any config-dependent import.
os.environ.setdefault("FYERS_WS_ENABLED", "1")

from application import config  # noqa: E402
from application.services import cache as shared_cache  # noqa: E402
from application.services.ws_ticker import FyersWSManager  # noqa: E402

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(
    level=os.getenv("FYERS_WS_LOG_LEVEL", "INFO").upper(),
    format=LOG_FORMAT,
)
log = logging.getLogger("fyers_ws")


def main() -> int:
    if not config.FYERS_APP_ID or not config.FYERS_ACCESS_TOKEN:
        log.error("FYERS_APP_ID / FYERS_ACCESS_TOKEN are not set; aborting.")
        return 2
    if not shared_cache.is_redis_enabled():
        log.warning(
            "Redis is not configured. The WS worker can still run but the "
            "Flask app won't see any ticks (in-process cache is per-process). "
            "Set REDIS_URL to enable cross-process delivery."
        )

    mgr = FyersWSManager(
        access_token=config.FYERS_ACCESS_TOKEN,
        mode=os.getenv("FYERS_WS_MODE", "lite"),
        tick_ttl=config.FYERS_WS_TICK_TTL,
        max_symbols=config.FYERS_WS_MAX_SYMBOLS_PER_CONN,
    )

    def _on_signal(signum, _frame):
        log.info("Received signal %d, shutting down", signum)
        mgr.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except Exception:  # noqa: BLE001 — Windows may not support SIGTERM
            pass

    mgr.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
