"""
Minimal Anthropic Messages API client over stdlib urllib (no extra deps).

Reads credentials from the environment so it works both in a managed
OpenClaw/Claude-Code host and with a plain user-supplied key:
  ANTHROPIC_API_KEY   required to make a live call
  ANTHROPIC_BASE_URL  optional, default https://api.anthropic.com
  ANTHROPIC_MODEL     optional, default claude-sonnet-4-5

call_claude() returns the assistant text, or raises LLMUnavailable if no
key is configured or the request fails — callers fall back to a heuristic.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")


class LLMUnavailable(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def call_claude(prompt: str, *, system: str | None = None, max_tokens: int = 1024,
                model: str | None = None, timeout: float = 60.0) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMUnavailable("ANTHROPIC_API_KEY not set")

    body = {
        "model": model or DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    req = urllib.request.Request(
        f"{BASE_URL}/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise LLMUnavailable(f"HTTP {e.code}: {detail[:300]}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMUnavailable(str(e)) from e

    parts = [b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise LLMUnavailable("empty completion")
    return text
