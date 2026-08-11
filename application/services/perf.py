"""Performance / observability middleware.

Provides:

- ``register(app)`` — wire it all up from ``application/__init__.py``.
- Per-request timing log (``method path → status (ms)``) with a slow-
  request warning above ``SLOW_REQUEST_MS``.
- Automatic ``Cache-Control`` + weak ``ETag`` on GET responses for
  ``/api/...`` endpoints (browsers + any CDN in front get free caching,
  and revalidation returns ``304`` so we don't even serialise the body).
- Per-path TTL overrides via :data:`API_CACHE_RULES` so heatmap / quote
  endpoints can be cached longer than user-specific ones.

Nothing here mutates payloads — it only adds headers and times requests.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Iterable

from flask import Flask, Response, g, request

log = logging.getLogger("hm2.perf")

SLOW_REQUEST_MS = 800  # log a warning above this

# Per-route Cache-Control tuning.
# Each entry: (pattern, max_age_seconds, stale_while_revalidate_seconds, public)
#   - ``public`` is True for shared/anonymous data, False for per-user.
# First matching rule wins; default applied if nothing matches.
API_CACHE_RULES: list[tuple[re.Pattern, int, int, bool]] = [
    (re.compile(r"^/api/_health"),               5,   30,  True),
    (re.compile(r"^/api/stock/search"),          60,  600, True),
    (re.compile(r"^/api/stock/info"),            60,  300, True),
    (re.compile(r"^/api/heatmap"),               15,  120, True),
    (re.compile(r"^/api/market[_-]pulse"),       15,  120, True),
    (re.compile(r"^/api/option[_-]chain"),       30,  300, True),
    (re.compile(r"^/api/fundamentals"),          300, 1800,True),
    (re.compile(r"^/api/intraday"),              15,  120, True),
    (re.compile(r"^/api/volume"),                30,  300, True),
    (re.compile(r"^/api/swing"),                 60,  600, True),
    (re.compile(r"^/api/investing"),             60,  600, True),
    (re.compile(r"^/api/mutual[_-]?funds"),      300, 3600,True),
    # Per-user — short, private, still SWR so the UI is instant.
    (re.compile(r"^/api/portfolio"),             10,  60,  False),
    (re.compile(r"^/api/user"),                  10,  60,  False),
    (re.compile(r"^/api/ai"),                    0,   0,   False),  # no-cache (LLM)
]
# Default for any unmatched /api/* GET.
API_CACHE_DEFAULT = (10, 60, False)

# Paths we never timestamp / never apply ETag to.
_SKIP_TIMING = ("/static/", "/favicon")


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "-"


def _cache_rule_for(path: str) -> tuple[int, int, bool]:
    for pat, ma, swr, public in API_CACHE_RULES:
        if pat.search(path):
            return ma, swr, public
    return API_CACHE_DEFAULT


def _should_apply_cache_headers(resp: Response) -> bool:
    if request.method != "GET":
        return False
    if not request.path.startswith("/api/"):
        return False
    if resp.status_code >= 400:
        return False
    # Don't touch streaming responses.
    if resp.direct_passthrough:
        return False
    # Respect explicit caller-set Cache-Control.
    if resp.headers.get("Cache-Control"):
        return False
    return True


def _weak_etag(body: bytes) -> str:
    return 'W/"' + hashlib.md5(body).hexdigest() + '"'


def register(app: Flask) -> None:
    """Install the perf middleware on ``app``. Idempotent."""
    if getattr(app, "_hm2_perf_installed", False):
        return
    app._hm2_perf_installed = True  # type: ignore[attr-defined]

    @app.before_request
    def _start_timer():
        g._perf_start = time.perf_counter()

    @app.after_request
    def _after(resp: Response):
        # ── 1. Cache-Control + ETag (API GETs only) ──────────────
        try:
            if _should_apply_cache_headers(resp):
                ma, swr, public = _cache_rule_for(request.path)
                if ma <= 0:
                    resp.headers["Cache-Control"] = "no-store"
                else:
                    scope = "public" if public else "private"
                    resp.headers["Cache-Control"] = (
                        f"{scope}, max-age={ma}, "
                        f"stale-while-revalidate={swr}"
                    )
                    resp.headers.setdefault("Vary", "Cookie, Accept-Encoding")
                # ETag — enables 304 Not Modified on revalidation.
                if resp.status_code == 200 and not resp.headers.get("ETag"):
                    try:
                        body = resp.get_data()
                        if body:
                            etag = _weak_etag(body)
                            resp.headers["ETag"] = etag
                            inm = request.headers.get("If-None-Match", "")
                            if inm and etag in [t.strip() for t in inm.split(",")]:
                                resp.status_code = 304
                                resp.set_data(b"")
                                resp.headers.pop("Content-Length", None)
                    except Exception:
                        pass
        except Exception as e:  # noqa: BLE001
            log.debug("perf: cache headers failed: %s", e)

        # ── 2. Server-Timing + request log ───────────────────────
        try:
            start = getattr(g, "_perf_start", None)
            if start is None or request.path.startswith(_SKIP_TIMING):
                return resp
            dur_ms = (time.perf_counter() - start) * 1000.0
            resp.headers["Server-Timing"] = f"app;dur={dur_ms:.1f}"
            level = logging.WARNING if dur_ms >= SLOW_REQUEST_MS else logging.INFO
            log.log(
                level,
                "%s %s → %d %.1fms ip=%s",
                request.method, request.full_path.rstrip("?"),
                resp.status_code, dur_ms, _client_ip(),
            )
        except Exception:
            pass
        return resp


# ── Optional helper for routes that want to declare their own policy ──

def cache_for(resp: Response, seconds: int, *, swr: int = 0, public: bool = True) -> Response:
    """Set a custom Cache-Control on an outgoing response.

    Call from inside a route to override the default rule, e.g.::

        @app.route('/api/something')
        def something():
            return perf.cache_for(jsonify(payload), 300, swr=600)
    """
    if seconds <= 0:
        resp.headers["Cache-Control"] = "no-store"
        return resp
    scope = "public" if public else "private"
    parts = [scope, f"max-age={seconds}"]
    if swr > 0:
        parts.append(f"stale-while-revalidate={swr}")
    resp.headers["Cache-Control"] = ", ".join(parts)
    return resp


def install_compression(app: Flask) -> None:
    """Enable gzip/br compression. No-op if Flask-Compress isn't installed."""
    try:
        from flask_compress import Compress  # type: ignore
    except Exception as e:  # noqa: BLE001
        log.info("perf: Flask-Compress unavailable (%s) — responses uncompressed", e)
        return
    if getattr(app, "_hm2_compress_installed", False):
        return
    app.config.setdefault("COMPRESS_MIMETYPES", [
        "text/html", "text/css", "text/xml", "text/plain",
        "application/json", "application/javascript",
        "application/xml", "image/svg+xml",
    ])
    app.config.setdefault("COMPRESS_LEVEL", 6)
    app.config.setdefault("COMPRESS_MIN_SIZE", 512)
    Compress(app)
    app._hm2_compress_installed = True  # type: ignore[attr-defined]


def install(app: Flask) -> None:
    """One-call setup: compression + timing + cache headers."""
    install_compression(app)
    register(app)
