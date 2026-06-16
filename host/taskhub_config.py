"""Runtime configuration for TaskHub Host.

Kept separate from task_hub.py so adapter/server code is not mixed with
environment parsing and deployment defaults.
"""

from __future__ import annotations

import hashlib
import os
import socket


DEFAULT_PORT = int(os.environ.get("TASK_HUB_PORT", "5577"))
DEFAULT_BIND = os.environ.get("TASK_HUB_BIND", "127.0.0.1")
DEFAULT_TOKEN = os.environ.get("TASK_HUB_TOKEN", "dev-token")
DEFAULT_DISCOVERY_PORT = int(os.environ.get("TASK_HUB_DISCOVERY_PORT", "5578"))
TASK_HUB_VERSION = "2.0.0"

DEVICE_NAME = os.environ.get("TASK_HUB_DEVICE_NAME") or socket.gethostname().split(".")[0] or "TaskHub"
DEVICE_ID = os.environ.get("TASK_HUB_DEVICE_ID") or (
    "host-" + hashlib.sha1(f"{socket.gethostname()}:{os.path.expanduser('~')}".encode("utf-8", "ignore")).hexdigest()[:12]
)

PEER_ENABLED = os.environ.get("TASK_HUB_ENABLE_PEERS", "1").lower() not in {"0", "false", "no", "off"}
PEER_DISCOVERY_MS = int(os.environ.get("TASK_HUB_PEER_DISCOVERY_MS", "15000"))
PEER_CACHE_MS = int(os.environ.get("TASK_HUB_PEER_CACHE_MS", "5000"))
PEER_DISCOVERY_TIMEOUT_MS = int(os.environ.get("TASK_HUB_PEER_DISCOVERY_TIMEOUT_MS", "350"))
PEER_HTTP_TIMEOUT_MS = int(os.environ.get("TASK_HUB_PEER_HTTP_TIMEOUT_MS", "1200"))
PEER_MAX = int(os.environ.get("TASK_HUB_PEER_MAX", "8"))

MAX_TASKS = int(os.environ.get("TASK_HUB_MAX_TASKS", "40"))
ACTIVE_MINUTES = int(os.environ.get("TASK_HUB_ACTIVE_MINUTES", "1440"))
TASK_CACHE_MS = int(os.environ.get("TASK_HUB_CACHE_MS", "3000"))
TRANSCRIPT_CACHE_MAX = int(os.environ.get("TASK_HUB_TRANSCRIPT_CACHE_MAX", "200"))

CODEX_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CODEX_RUNNING_STALE_MS", "900000"))
CODEX_DONE_WINDOW_MS = int(os.environ.get("TASK_HUB_CODEX_DONE_WINDOW_MS", "300000"))
CODEX_MAX_THREADS = int(os.environ.get("TASK_HUB_CODEX_MAX_THREADS", "6"))
QUESTION_WAITING_STALE_MS = int(os.environ.get("TASK_HUB_QUESTION_WAITING_STALE_MS", "3600000"))

# 90s default (was 15min): with process detection now reliable, the time-based
# fallback only needs to cover the brief gap between a turn finishing and the
# JSONL flushing its terminal stop_reason. Long-running turns stay marked as
# running via process detection, so this short window doesn't false-negative.
CLAUDE_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CLAUDE_RUNNING_STALE_MS", "90000"))
# After a turn ends with a terminal stop_reason, report the session as DONE
# (green) for this window before it settles to "recent". This gives a completed
# turn a distinct just-finished state for the StickS3's green DONE row and
# on-device DONE chime.
CLAUDE_DONE_WINDOW_MS = int(os.environ.get("TASK_HUB_CLAUDE_DONE_WINDOW_MS", "300000"))
CLAUDE_TERMINAL_STOP_REASONS = {"end_turn", "stop_sequence", "max_tokens"}
CLAUDE_HUMAN_INPUT_TOOLS = {"AskUserQuestion"}

CODEX_HUMAN_INPUT_FUNCTIONS = {"request_user_input"}
OPENCLAW_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_OPENCLAW_RUNNING_STALE_MS", "1800000"))
MANUS_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_MANUS_RUNNING_STALE_MS", "900000"))
MANUS_MAX_SESSIONS = int(os.environ.get("TASK_HUB_MANUS_MAX_SESSIONS", "3"))
MANUS_TERMINAL_STATUS_CODES = {5, 7}
PERPLEXITY_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_PERPLEXITY_RUNNING_STALE_MS", "30000"))
PERPLEXITY_COMPUTER_RUNNING_STALE_MS = int(
    os.environ.get("TASK_HUB_PERPLEXITY_COMPUTER_RUNNING_STALE_MS", "3600000")
)
GEMINI_ACTIVITY_STALE_MS = int(os.environ.get("TASK_HUB_GEMINI_ACTIVITY_STALE_MS", str(ACTIVE_MINUTES * 60 * 1000)))
GEMINI_BROWSER_POLL_MS = int(os.environ.get("TASK_HUB_GEMINI_BROWSER_POLL_MS", "10000"))
LOVABLE_BROWSER_POLL_MS = int(os.environ.get("TASK_HUB_LOVABLE_BROWSER_POLL_MS", "10000"))
LOVABLE_ACTIVITY_STALE_MS = int(os.environ.get("TASK_HUB_LOVABLE_ACTIVITY_STALE_MS", str(ACTIVE_MINUTES * 60 * 1000)))
LOVABLE_RENDERER_RUN_CPU = float(os.environ.get("TASK_HUB_LOVABLE_RENDERER_RUN_CPU", "8.0"))
LOVABLE_DOMAINS = tuple(
    part.strip().lower()
    for part in os.environ.get("TASK_HUB_LOVABLE_DOMAINS", "lovable.dev").split(",")
    if part.strip()
)
LOVABLE_MAX_TABS = int(os.environ.get("TASK_HUB_LOVABLE_MAX_TABS", "3"))
