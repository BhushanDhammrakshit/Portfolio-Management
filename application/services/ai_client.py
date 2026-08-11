"""Shared OpenAI / Azure OpenAI client.

Detects whether ``OPENAI_ENDPOINT`` is an Azure OpenAI URL and switches
auth header and payload shape accordingly. Returns (text, error).
"""
from __future__ import annotations

import requests

from application.config import OPENAI_API_KEY, OPENAI_ENDPOINT


def is_configured() -> bool:
    return bool(OPENAI_API_KEY and OPENAI_ENDPOINT)


def _is_azure() -> bool:
    return bool(OPENAI_ENDPOINT and "openai.azure.com" in OPENAI_ENDPOINT)


def chat(messages, *, temperature: float = 0.6, max_tokens: int = 900,
         timeout: int = 45):
    """Call the configured AI backend. Returns ``(content, error)``.

    ``content`` is the assistant text on success, otherwise ``None`` and
    ``error`` carries a short human-readable reason.
    """
    if not is_configured():
        return None, "AI service not configured."

    if _is_azure():
        headers = {
            "Content-Type": "application/json",
            "api-key": OPENAI_API_KEY,
        }
        payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    else:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        }
        payload = {
            "model": "gpt-4",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    try:
        r = requests.post(OPENAI_ENDPOINT, headers=headers, json=payload,
                          timeout=timeout)
    except requests.RequestException as e:
        return None, f"AI service unreachable: {e}"

    if r.status_code != 200:
        snippet = (r.text or "")[:300].replace("\n", " ")
        return None, f"AI service error {r.status_code}: {snippet}"

    try:
        data = r.json()
        return data["choices"][0]["message"]["content"], None
    except (KeyError, IndexError, ValueError):
        return None, "Unexpected AI response shape."


def chat_with_usage(messages, *, temperature: float = 0.6, max_tokens: int = 900,
                    timeout: int = 45):
    """Like ``chat`` but also returns token usage.

    Returns ``(content, error, usage)`` where ``usage`` is a dict with keys
    ``prompt_tokens``, ``completion_tokens``, ``total_tokens`` (zeros if the
    backend does not report usage).
    """
    if not is_configured():
        return None, "AI service not configured.", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    if _is_azure():
        headers = {"Content-Type": "application/json", "api-key": OPENAI_API_KEY}
        payload = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    else:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
        payload = {"model": "gpt-4", "messages": messages, "temperature": temperature, "max_tokens": max_tokens}

    try:
        r = requests.post(OPENAI_ENDPOINT, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        return None, f"AI service unreachable: {e}", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    if r.status_code != 200:
        snippet = (r.text or "")[:300].replace("\n", " ")
        return None, f"AI service error {r.status_code}: {snippet}", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return content, None, {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    except (KeyError, IndexError, ValueError):
        return None, "Unexpected AI response shape.", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
