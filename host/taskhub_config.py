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
TASK_HUB_VERSION = "2.2.0"

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

# Device pairing (first-run flow for M5Burner firmware). An unpaired StickS3
# finds the Host over UDP discovery, shows a short code on its screen and POSTs
# it to /pair; the token is only handed out after that exact code is approved
# on the Mac (loopback-only /pair/approve). Pending requests expire quickly.
PAIR_PENDING_TTL_MS = int(os.environ.get("TASK_HUB_PAIR_PENDING_TTL_MS", "300000"))
PAIR_APPROVED_TTL_MS = int(os.environ.get("TASK_HUB_PAIR_APPROVED_TTL_MS", "120000"))
PAIR_MAX_PENDING = int(os.environ.get("TASK_HUB_PAIR_MAX_PENDING", "8"))

MAX_TASKS = int(os.environ.get("TASK_HUB_MAX_TASKS", "40"))
ACTIVE_MINUTES = int(os.environ.get("TASK_HUB_ACTIVE_MINUTES", "1440"))
TASK_CACHE_MS = int(os.environ.get("TASK_HUB_CACHE_MS", "3000"))
TASK_STICK_STALE_CACHE_MS = int(os.environ.get("TASK_HUB_STICK_STALE_CACHE_MS", "1800000"))
TASK_BACKGROUND_REFRESH_MIN_MS = int(os.environ.get("TASK_HUB_BACKGROUND_REFRESH_MIN_MS", "5000"))
# How long a request may wait for an in-flight cache refresh when the cache is
# still empty (Host just started). Kept under the StickS3's 8s HTTP timeout so
# a cold Host answers with `syncing: true` instead of letting the Stick time
# out; the Stick then re-fetches a moment later.
TASK_COLD_START_WAIT_MS = int(os.environ.get("TASK_HUB_COLD_START_WAIT_MS", "6000"))
TRANSCRIPT_CACHE_MAX = int(os.environ.get("TASK_HUB_TRANSCRIPT_CACHE_MAX", "200"))
ADAPTER_MAX_WORKERS = max(1, int(os.environ.get("TASK_HUB_ADAPTER_MAX_WORKERS", "8")))

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
CLAUDE_MAX_TRANSCRIPTS = int(os.environ.get("TASK_HUB_CLAUDE_MAX_TRANSCRIPTS", "12"))
CLAUDE_TERMINAL_STOP_REASONS = {"end_turn", "stop_sequence", "max_tokens"}
# Tools whose unanswered tool_use blocks the turn on user input. ExitPlanMode
# is Claude Code's plan-approval prompt (2.x): the agent stops and waits for
# the user to approve the plan, which is a WAIT on the device, not a RUN.
CLAUDE_HUMAN_INPUT_TOOLS = {"AskUserQuestion", "ExitPlanMode"}

CODEX_HUMAN_INPUT_FUNCTIONS = {"request_user_input"}
OPENCLAW_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_OPENCLAW_RUNNING_STALE_MS", "1800000"))
OPENCLAW_FAILED_TTL_MS = int(os.environ.get("TASK_HUB_OPENCLAW_FAILED_TTL_MS", "600000"))
OPENCLAW_CLI_FALLBACK = os.environ.get("TASK_HUB_OPENCLAW_CLI_FALLBACK", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
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
# Cursor IDE agent sessions, read from Cursor's global composer index plus the
# per-project agent transcripts under ~/.cursor/projects.
CURSOR_MAX_SESSIONS = int(os.environ.get("TASK_HUB_CURSOR_MAX_SESSIONS", "6"))
# Transcript lines carry no timestamps, so RUN freshness rides on file mtime /
# composer lastUpdatedAt. Cursor flushes transcripts lazily during a long turn,
# so keep this generous (like Codex); the Cursor-process gate already ends RUN
# immediately when the app quits.
CURSOR_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CURSOR_RUNNING_STALE_MS", "900000"))
CURSOR_DONE_WINDOW_MS = int(os.environ.get("TASK_HUB_CURSOR_DONE_WINDOW_MS", "300000"))
CURSOR_FAILED_TTL_MS = int(os.environ.get("TASK_HUB_CURSOR_FAILED_TTL_MS", "600000"))
# Cursor sets hasBlockingPendingActions while tool calls are merely in flight,
# not only when an approval is pending, so a WAIT signal counts only after the
# composer's checkpoint activity has stalled for this long. Checkpoints update
# continuously during generation and stall when the agent is actually blocked.
CURSOR_WAIT_CONFIRM_MS = int(os.environ.get("TASK_HUB_CURSOR_WAIT_CONFIRM_MS", "120000"))
# Cursor sets hasBlockingPendingActions while tools are merely executing, not
# only when an approval is pending. Only report WAIT once composer activity
# (lastUpdatedAt/checkpointAt) has stalled for this long — a generating agent
# keeps bumping those, a blocked approval does not.
CURSOR_WAIT_CONFIRM_MS = int(os.environ.get("TASK_HUB_CURSOR_WAIT_CONFIRM_MS", "180000"))
# Transcripts flush lazily (often only at turn end). If the composer index is
# newer than the transcript by more than this, there is an unflushed turn in
# progress and the transcript's end-of-file verdict is outdated.
CURSOR_TRANSCRIPT_LAG_MS = int(os.environ.get("TASK_HUB_CURSOR_TRANSCRIPT_LAG_MS", "15000"))
CURSOR_GLOBAL_STORAGE_DB = os.environ.get(
    "TASK_HUB_CURSOR_GLOBAL_STORAGE_DB",
    "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
)
CURSOR_PROJECTS_ROOT = os.environ.get("TASK_HUB_CURSOR_PROJECTS_ROOT", "~/.cursor/projects")
CURSOR_HUMAN_INPUT_TOOLS = {"AskQuestion"}
WORKBUDDY_MAX_SESSIONS = int(os.environ.get("TASK_HUB_WORKBUDDY_MAX_SESSIONS", "6"))
WORKBUDDY_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_WORKBUDDY_RUNNING_STALE_MS", "600000"))
WORKBUDDY_DONE_WINDOW_MS = int(os.environ.get("TASK_HUB_WORKBUDDY_DONE_WINDOW_MS", "300000"))
# Cline / Roo Code / Kilo Code (VS Code-family extensions). Every task rewrites
# its ui_messages.json on each message, so file mtime is a reliable freshness
# signal and an unanswered `ask` message is an exact WAIT.
CLINE_MAX_TASKS = int(os.environ.get("TASK_HUB_CLINE_MAX_TASKS", "6"))
CLINE_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CLINE_RUNNING_STALE_MS", "900000"))
CLINE_DONE_WINDOW_MS = int(os.environ.get("TASK_HUB_CLINE_DONE_WINDOW_MS", "300000"))
CLINE_FAILED_TTL_MS = int(os.environ.get("TASK_HUB_CLINE_FAILED_TTL_MS", "600000"))
# Terminal coding agents (Gemini CLI / Qwen Code / GitHub Copilot CLI). Their
# transcripts carry per-message timestamps; RUN needs a live CLI process and a
# fresh turn, or (if the process is not visible to ps) a very recent event.
CLI_AGENT_MAX_SESSIONS = int(os.environ.get("TASK_HUB_CLI_AGENT_MAX_SESSIONS", "6"))
CLI_AGENT_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CLI_AGENT_RUNNING_STALE_MS", "900000"))
CLI_AGENT_NO_PROCESS_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CLI_AGENT_NO_PROCESS_RUNNING_STALE_MS", "120000"))
CLI_AGENT_DONE_WINDOW_MS = int(os.environ.get("TASK_HUB_CLI_AGENT_DONE_WINDOW_MS", "300000"))
CLI_AGENT_FAILED_TTL_MS = int(os.environ.get("TASK_HUB_CLI_AGENT_FAILED_TTL_MS", "600000"))
WEB_AI_ACTIVITY_STALE_MS = int(
    os.environ.get("TASK_HUB_WEB_AI_ACTIVITY_STALE_MS", str(ACTIVE_MINUTES * 60 * 1000))
)
KIMI_RENDERER_RUN_CPU = float(os.environ.get("TASK_HUB_KIMI_RENDERER_RUN_CPU", "8.0"))
KIMI_RUNNING_HOLD_MS = int(os.environ.get("TASK_HUB_KIMI_RUNNING_HOLD_MS", "15000"))
KIMI_LOCAL_RUNNING_STALE_MS = int(
    os.environ.get("TASK_HUB_KIMI_LOCAL_RUNNING_STALE_MS", "21600000")
)
