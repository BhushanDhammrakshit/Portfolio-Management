"""Shared, connection-pooled HTTP session with retries.

All provider/network code should fetch a session via ``get_session(name)``
instead of using the bare ``requests`` module. Reusing a Session reuses
the underlying TCP/TLS connection across requests, which removes the
~100–300ms handshake from every upstream call and dramatically improves
throughput when many users hit the app concurrently.

Per-host sessions are cached, so unrelated providers don't fight over
the same connection pool.
"""
from __future__ import annotations

import threading
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

try:
    # urllib3 ships with requests; Retry lives there.
    from urllib3.util.retry import Retry  # type: ignore
except Exception:  # pragma: no cover
    Retry = None  # type: ignore[assignment]


_DEFAULT_POOL = 20
_DEFAULT_MAX_POOL = 50

_sessions: dict[str, requests.Session] = {}
_lock = threading.Lock()


def _build_session(
    pool_connections: int,
    pool_maxsize: int,
    retries: int,
    backoff: float,
    user_agent: Optional[str],
) -> requests.Session:
    s = requests.Session()
    retry = None
    if Retry is not None and retries > 0:
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=backoff,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "HEAD", "PUT", "DELETE"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=retry if retry is not None else 0,
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    if user_agent:
        s.headers["User-Agent"] = user_agent
    return s


def get_session(
    name: str = "default",
    *,
    pool_connections: int = _DEFAULT_POOL,
    pool_maxsize: int = _DEFAULT_MAX_POOL,
    retries: int = 2,
    backoff: float = 0.4,
    user_agent: Optional[str] = None,
) -> requests.Session:
    """Return a process-wide cached Session for the given logical ``name``.

    ``name`` is just a cache key — use one per upstream (``"dhan"``,
    ``"fyers"``, ``"yfinance"``) so each gets its own connection pool.
    """
    s = _sessions.get(name)
    if s is not None:
        return s
    with _lock:
        s = _sessions.get(name)
        if s is None:
            s = _build_session(
                pool_connections=pool_connections,
                pool_maxsize=pool_maxsize,
                retries=retries,
                backoff=backoff,
                user_agent=user_agent,
            )
            _sessions[name] = s
        return s


def close_all() -> None:
    """Close every cached session (useful in tests/teardown)."""
    with _lock:
        for s in _sessions.values():
            try:
                s.close()
            except Exception:
                pass
        _sessions.clear()
