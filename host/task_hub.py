#!/usr/bin/env python3
"""
Local AI Task Hub for StickS3.

The hub keeps the StickS3 simple: it exposes a compact HTTP API, collects task
state from local adapters, and handles "open this task" actions on the Mac.
It intentionally reads only local metadata and process/window state.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import html
import json
import os
import plistlib
import re
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple


Task = Dict[str, Any]

DEFAULT_PORT = int(os.environ.get("TASK_HUB_PORT", "5577"))
DEFAULT_BIND = os.environ.get("TASK_HUB_BIND", "0.0.0.0")
DEFAULT_TOKEN = os.environ.get("TASK_HUB_TOKEN", "dev-token")
DEFAULT_DISCOVERY_PORT = int(os.environ.get("TASK_HUB_DISCOVERY_PORT", "5578"))
TASK_HUB_VERSION = "1.1.1"
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
CODEX_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CODEX_RUNNING_STALE_MS", "900000"))
QUESTION_WAITING_STALE_MS = int(os.environ.get("TASK_HUB_QUESTION_WAITING_STALE_MS", "3600000"))
# 90s default (was 15min): with process detection now reliable, the time-based
# fallback only needs to cover the brief gap between a turn finishing and the
# JSONL flushing its terminal stop_reason. Long-running turns stay marked as
# running via process detection, so this short window doesn't false-negative.
CLAUDE_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CLAUDE_RUNNING_STALE_MS", "90000"))
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

# Memoise the expensive Claude transcript scans. Keyed by JSONL path, value is
# the scan result tagged with (mtime, size); a cache hit means the file hasn't
# changed since we last walked it, so we return the prior result instantly
# instead of re-reading the entire transcript on every /tasks request. Hot for
# users with many idle sessions on disk.
_CLAUDE_TRANSCRIPT_CACHE: Dict[str, Dict[str, Any]] = {}
# Same idea for Codex session rollouts; codex_usage_records() walks up to 80
# *.jsonl files per /tasks request, so memoising makes idle sessions free.
_CODEX_SESSION_CACHE: Dict[str, Dict[str, Any]] = {}


def ci_contains(needle: str, hay: str) -> bool:
    """Case-insensitive substring check. macOS process listings mix
    `/Claude.app/` (Desktop) and `/claude.app/` (Claude Code binary) in the
    same path, so case-sensitive matching silently misses half the time."""
    return needle.lower() in hay.lower()


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone().isoformat(timespec="seconds")


def stable_id(prefix: str, raw: str) -> str:
    digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def short_device_label(name: str) -> str:
    label = re.sub(r"\.local$", "", str(name or ""), flags=re.IGNORECASE).strip()
    label = label.replace("MacBook-Pro", "MBP").replace("MacBook", "MB")
    label = label.replace("Mac-Studio", "Studio").replace("Mac-mini", "Mini")
    label = re.sub(r"[^A-Za-z0-9_-]+", "", label) or "Hub"
    return label[:10]


def attach_device(task_obj: Task, *, device_id: str, device_name: str, origin: str = "local") -> Task:
    task_obj["device_id"] = device_id
    task_obj["device_name"] = device_name
    task_obj["device_label"] = short_device_label(device_name)
    task_obj["origin"] = origin
    return task_obj


def run(args: List[str], timeout: float = 4.0) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        return 1, "", str(exc)


def run_osascript(script: str, timeout: float = 4.0) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(["osascript"], input=script, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        return 1, "", str(exc)


def run_json(args: List[str], timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    code, out, _ = run(args, timeout=timeout)
    if code != 0 or not out.strip():
        return None
    try:
        value = json.loads(out)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def node_command() -> Optional[str]:
    configured = os.environ.get("TASK_HUB_NODE")
    candidates = [
        configured,
        shutil.which("node"),
        os.path.expanduser("~/.local/node/bin/node"),
        os.path.expanduser("~/.local/node-v22.22.1-darwin-arm64/bin/node"),
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
        "/usr/bin/node",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def task(
    *,
    task_id: str,
    source: str,
    title: str,
    status: str,
    updated_ms: Optional[int] = None,
    subtitle: str = "",
    detail: Optional[Dict[str, Any]] = None,
    usage: Optional[Dict[str, Any]] = None,
    open_action: Optional[Dict[str, str]] = None,
    needs_attention: bool = False,
) -> Task:
    updated = updated_ms or now_ms()
    return {
        "id": task_id,
        "source": source,
        "title": title or source,
        "status": normalize_status(status),
        "updated_at": ms_to_iso(updated),
        "updated_ms": updated,
        "age_sec": max(0, int((now_ms() - updated) / 1000)),
        "subtitle": subtitle,
        "detail": detail or {},
        "usage": usage or {},
        "needs_attention": bool(needs_attention),
        "_open": open_action or {},
    }


def normalize_status(status: str) -> str:
    s = (status or "unknown").lower()
    if s in {"queued", "waiting", "needs_input", "needs-attention", "pending"}:
        return "waiting"
    if s in {"running", "active", "in_progress", "syncing"}:
        return "running"
    if s in {"failed", "error", "timed_out", "lost"}:
        return "failed"
    if s in {"succeeded", "success", "done", "completed"}:
        return "done"
    if s in {"idle", "not_running", "offline"}:
        return "idle"
    if s in {"recent"}:
        return "recent"
    return "unknown"


def sort_key(t: Task) -> Tuple[int, int]:
    rank = {
        "waiting": 0,
        "failed": 1,
        "running": 2,
        "recent": 3,
        "unknown": 4,
        "done": 5,
        "idle": 9,
    }.get(t.get("status"), 8)
    if t.get("needs_attention"):
        rank = -1
    return rank, -int(t.get("updated_ms") or 0)


def local_ip_hint() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def ps_commands() -> List[str]:
    code, out, _ = run(["ps", "-axo", "pid=,stat=,lstart=,command="], timeout=3)
    return out.splitlines() if code == 0 else []


def app_running(commands: Iterable[str], bundle_path_fragment: str, binary_name: str) -> bool:
    needle = bundle_path_fragment.lower()
    binary = f"/MacOS/{binary_name}".lower()
    for line in commands:
        low = line.lower()
        if needle in low and binary in low:
            return True
    return False


def process_name_running(name: str) -> bool:
    code, _, _ = run(["pgrep", "-x", name], timeout=0.5)
    return code == 0


def window_titles(app_name: str) -> List[str]:
    script = (
        f'tell application "System Events" to '
        f'if exists process "{app_name}" then get name of every window of process "{app_name}"'
    )
    code, out, _ = run(["osascript", "-e", script], timeout=2)
    if code != 0:
        return []
    return [part.strip() for part in out.replace("\n", ",").split(",") if part.strip()]


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def iso_to_ms(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def short_count(value: int) -> str:
    if value >= 10_000_000:
        return f"{round(value / 1_000_000)}M"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{round(value / 1_000)}k"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def folder_label(path: str) -> str:
    return os.path.basename(path.rstrip("/")) if path else ""


def join_parts(parts: Iterable[Any], sep: str = " · ") -> str:
    return sep.join(str(part) for part in parts if part not in (None, ""))


def extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "")
                if item_type in {"text", "output_text", "message"} and isinstance(item.get("text"), str):
                    parts.append(str(item.get("text")))
                elif item_type in {"text", "output_text", "message"} and isinstance(item.get("content"), str):
                    parts.append(str(item.get("content")))
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "message", "content"):
            if isinstance(value.get(key), str):
                return str(value.get(key))
    return ""


def tool_use_names(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    names: List[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def is_human_question(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact or len(compact) > 1200:
        return False
    tail = compact[-320:].lower()
    prompt_markers = [
        "please confirm",
        "please approve",
        "confirm before",
        "confirm this",
        "approve this",
        "permission",
        "approval",
        "should i",
        "would you like",
        "do you want me",
        "which option",
        "choose one",
        "please choose",
        "please provide",
        "please reply",
        "how should i",
        "how would you like",
        "what would you prefer",
        "which would you prefer",
        "shall i",
        "go ahead",
        "continue?",
        "waiting for your",
        "need your confirmation",
        "need your approval",
        "requires your",
        "请确认",
        "需要你确认",
        "需要您确认",
        "需要你批准",
        "需要您批准",
        "等你确认",
        "等您确认",
        "等你回复",
        "等您回复",
        "要我现在",
        "要我继续",
        "要不要我",
        "是否继续",
        "选哪个",
        "哪个方案",
        "哪种方案",
        "请选择",
        "选择一个",
        "请提供",
        "请回复",
        "请告诉我",
        "等你选择",
        "等您选择",
        "你希望我",
        "您希望我",
        "你要我",
        "您要我",
        "可以继续吗",
        "继续吗",
        "现在开始吗",
    ]
    return any(marker in tail for marker in prompt_markers)


def is_waiting_for_human(latest_assistant_ms: int, latest_user_ms: int, latest_text: str) -> bool:
    if not latest_assistant_ms or latest_assistant_ms <= latest_user_ms:
        return False
    if now_ms() - latest_assistant_ms > QUESTION_WAITING_STALE_MS:
        return False
    return is_human_question(latest_text)


def is_unanswered_human_input_request(request_ms: int, latest_user_ms: int) -> bool:
    if not request_ms or request_ms <= latest_user_ms:
        return False
    return now_ms() - request_ms <= QUESTION_WAITING_STALE_MS


def build_usage(
    *,
    total_tokens: int = 0,
    turns: int = 0,
    rate_percent: Optional[float] = None,
    fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    parts: List[str] = []
    if total_tokens > 0:
        parts.append(f"{short_count(total_tokens)} tok")
    if turns > 0:
        parts.append(f"{turns} turns")
    if rate_percent is not None:
        rate_text = "<1" if 0 < rate_percent < 1 else f"{rate_percent:.0f}"
        parts.append(f"rl {rate_text}%")
    if not parts:
        return {}

    usage = dict(fields or {})
    usage.update(
        {
            "label": " · ".join(parts[:3]),
            "total_tokens": total_tokens,
            "turns": turns,
        }
    )
    if rate_percent is not None:
        usage["rate_percent"] = rate_percent
    return usage


def compact_usage_label(usage: Dict[str, Any]) -> str:
    total = safe_int(usage.get("total_tokens"))
    turns = safe_int(usage.get("turns"))
    rate_percent = safe_float(usage.get("rate_percent"))
    parts: List[str] = []
    if total > 0:
        parts.append(f"{short_count(total)} tok")
    if turns > 0:
        parts.append(f"{turns}t")
    if rate_percent is not None and rate_percent >= 1:
        parts.append(f"{rate_percent:.0f}%")
    if not parts and usage.get("label"):
        return str(usage.get("label"))
    return " ".join(parts)


def total_tokens_from_usage(usage: Dict[str, Any], fields: List[str]) -> int:
    if safe_int(usage.get("total_tokens")) > 0:
        return safe_int(usage.get("total_tokens"))
    return sum(safe_int(usage.get(field)) for field in fields)


def claude_transcript_path(cli_session_id: str) -> str:
    if not cli_session_id:
        return ""
    pattern = os.path.expanduser(f"~/.claude/projects/**/{cli_session_id}.jsonl")
    paths = glob.glob(pattern, recursive=True)
    if not paths:
        return ""
    return max(paths, key=os.path.getmtime)


def _scan_claude_transcript(path: str) -> Optional[Dict[str, Any]]:
    """Walk a Claude Code JSONL transcript once and return the accumulated
    state needed for status + usage. Memoised by (path, mtime, size) so that
    on the next /tasks request, an unchanged file returns in O(1) instead of
    re-reading every line — critical when the user has hundreds of historical
    sessions on disk."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cache_key = (int(st.st_mtime * 1000), int(st.st_size))
    cached = _CLAUDE_TRANSCRIPT_CACHE.get(path)
    if cached and cached.get("_key") == cache_key:
        return cached

    fields = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    seen_requests: set = set()
    seen_terminal_requests: set = set()
    request_count = 0
    model = ""
    terminal_turns = 0
    latest_event_ms = 0
    latest_turn_event_ms = 0
    latest_user_ms = 0
    latest_assistant_ms = 0
    latest_terminal_ms = 0
    latest_human_input_request_ms = 0
    latest_non_human_tool_use_ms = 0
    latest_stop_reason = ""
    latest_request_id = ""
    latest_turn_event_type = ""
    latest_assistant_text_ms = 0
    latest_assistant_text = ""

    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = item.get("message")
                message = message if isinstance(message, dict) else {}
                event_ms = iso_to_ms(item.get("timestamp"))
                item_type = str(item.get("type") or "")
                role = str(message.get("role") or item.get("role") or "")
                stop_reason = str(message.get("stop_reason") or item.get("stop_reason") or "")

                if event_ms:
                    latest_event_ms = max(latest_event_ms, event_ms)
                    if item_type in {"user", "assistant"} or role in {"user", "assistant"}:
                        latest_turn_event_ms = event_ms
                        latest_turn_event_type = item_type or role
                        if role == "user" or item_type == "user":
                            latest_user_ms = event_ms
                        if role == "assistant" or item_type == "assistant":
                            latest_assistant_ms = event_ms
                            latest_stop_reason = stop_reason
                            latest_request_id = str(item.get("requestId") or message.get("id") or item.get("uuid") or "")
                            content = message.get("content")
                            tool_names = tool_use_names(content)
                            if any(name in CLAUDE_HUMAN_INPUT_TOOLS for name in tool_names):
                                latest_human_input_request_ms = event_ms
                            elif tool_names:
                                latest_non_human_tool_use_ms = event_ms
                            text = extract_text(content)
                            if text:
                                latest_assistant_text_ms = event_ms
                                latest_assistant_text = text
                            if stop_reason in CLAUDE_TERMINAL_STOP_REASONS:
                                latest_terminal_ms = event_ms
                                terminal_key = latest_request_id or str(item.get("uuid") or event_ms)
                                if terminal_key not in seen_terminal_requests:
                                    seen_terminal_requests.add(terminal_key)
                                    terminal_turns += 1

                raw_usage = message.get("usage")
                if isinstance(raw_usage, dict):
                    request_id = str(item.get("requestId") or message.get("id") or item.get("uuid") or "")
                    if request_id and request_id in seen_requests:
                        continue
                    if request_id:
                        seen_requests.add(request_id)
                    request_count += 1
                    if message.get("model"):
                        model = str(message.get("model"))
                    for field in fields:
                        fields[field] += safe_int(raw_usage.get(field))
    except OSError:
        return None

    active_turn = False
    if latest_turn_event_ms:
        if latest_turn_event_type == "assistant":
            active_turn = latest_stop_reason not in CLAUDE_TERMINAL_STOP_REASONS
        else:
            active_turn = latest_turn_event_ms > latest_terminal_ms

    scan = {
        "_key": cache_key,
        "fields": fields,
        "total_tokens": total_tokens_from_usage(fields, list(fields.keys())),
        "model": model,
        "request_count": request_count,
        "terminal_turns": terminal_turns,
        "latest_event_ms": latest_turn_event_ms or latest_event_ms,
        "latest_user_ms": latest_user_ms,
        "latest_assistant_ms": latest_assistant_ms,
        "latest_assistant_text_ms": latest_assistant_text_ms,
        "latest_terminal_ms": latest_terminal_ms,
        "latest_human_input_request_ms": latest_human_input_request_ms,
        "latest_non_human_tool_use_ms": latest_non_human_tool_use_ms,
        "latest_stop_reason": latest_stop_reason,
        "latest_request_id": latest_request_id,
        "active_turn": active_turn,
        "waiting_for_user": (
            is_unanswered_human_input_request(latest_human_input_request_ms, latest_user_ms)
            or (
                latest_non_human_tool_use_ms < latest_assistant_text_ms
                and is_waiting_for_human(latest_assistant_text_ms, latest_user_ms, latest_assistant_text)
            )
        ),
    }
    _CLAUDE_TRANSCRIPT_CACHE[path] = scan
    return scan


def claude_session_metrics(cli_session_id: str, completed_turns: Any = None) -> Dict[str, Any]:
    path = claude_transcript_path(cli_session_id)
    turns = safe_int(completed_turns)
    if not path:
        return {"usage": build_usage(turns=turns), "turns": turns}
    scan = _scan_claude_transcript(path)
    if scan is None:
        return {"usage": build_usage(turns=turns), "turns": turns}

    effective_turns = turns or scan["terminal_turns"] or scan["request_count"]
    usage = build_usage(
        total_tokens=scan["total_tokens"],
        turns=effective_turns,
        fields={
            "source": "claude-transcript",
            "model": scan["model"],
            "requests": scan["request_count"],
            **scan["fields"],
        },
    )
    return {
        "usage": usage,
        "turns": effective_turns,
        "requests": scan["request_count"],
        "transcript_found": True,
        "latest_event_ms": scan["latest_event_ms"],
        "latest_user_ms": scan["latest_user_ms"],
        "latest_assistant_ms": scan["latest_assistant_ms"],
        "latest_terminal_ms": scan["latest_terminal_ms"],
        "latest_human_input_request_ms": scan["latest_human_input_request_ms"],
        "latest_stop_reason": scan["latest_stop_reason"],
        "latest_request_id": scan["latest_request_id"],
        "active_turn": scan["active_turn"],
        "waiting_for_user": scan.get("waiting_for_user", False),
    }


def claude_usage(cli_session_id: str, completed_turns: Any = None) -> Dict[str, Any]:
    return claude_session_metrics(cli_session_id, completed_turns).get("usage") or {}


def claude_status(metrics: Dict[str, Any], updated_ms: Optional[int], process_running: bool = False) -> str:
    latest = safe_int(metrics.get("latest_event_ms") or updated_ms)
    if metrics.get("waiting_for_user"):
        return "waiting"
    if metrics.get("active_turn"):
        if process_running:
            return "running"
        if latest and now_ms() - latest < CLAUDE_RUNNING_STALE_MS:
            return "running"
    if latest and now_ms() - latest < ACTIVE_MINUTES * 60 * 1000:
        return "recent"
    return "recent" if process_running else "idle"


def claude_turn_state(metrics: Dict[str, Any]) -> str:
    stop_reason = str(metrics.get("latest_stop_reason") or "")
    if metrics.get("waiting_for_user"):
        return "wait"
    if metrics.get("active_turn"):
        if stop_reason == "tool_use":
            return "tool"
        return "active"
    if stop_reason == "max_tokens":
        return "limit"
    if stop_reason in CLAUDE_TERMINAL_STOP_REASONS:
        return "done"
    return ""


def claude_subtitle(metrics: Dict[str, Any], cwd: str, fallback: str = "Claude Code") -> str:
    parts: List[Any] = []
    folder = folder_label(cwd)
    if folder:
        parts.append(folder)
    turns = safe_int(metrics.get("turns"))
    if turns:
        parts.append(f"t{turns}")
    state = claude_turn_state(metrics)
    if state:
        parts.append(state)
    if not parts:
        parts.append(fallback)
    return join_parts(parts, " · ")


def codex_session_index() -> Dict[str, Dict[str, Any]]:
    path = os.path.expanduser("~/.codex/session_index.jsonl")
    records: Dict[str, Dict[str, Any]] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = str(item.get("id") or "")
                if not session_id:
                    continue
                records[session_id] = {
                    "title": str(item.get("thread_name") or ""),
                    "updated_ms": iso_to_ms(item.get("updated_at")),
                }
    except OSError:
        pass
    return records


def _scan_codex_session(path: str) -> Optional[Dict[str, Any]]:
    """Walk a single codex rollout JSONL once; memoise the result by
    (path, mtime, size). The hot path on a /tasks request becomes O(1) for
    every session the user hasn't touched since the previous scan."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cache_key = (int(st.st_mtime * 1000), int(st.st_size))
    cached = _CODEX_SESSION_CACHE.get(path)
    if cached and cached.get("_key") == cache_key:
        return cached

    session_id = ""
    cwd = ""
    updated_ms = int(st.st_mtime * 1000)
    token_usage: Dict[str, Any] = {}
    rate_percent: Optional[float] = None
    turns = 0
    latest_turn_id = ""
    latest_turn_ms = 0
    latest_completed_turn_id = ""
    latest_completed_ms = 0
    latest_event_ms = updated_ms
    latest_user_ms = 0
    latest_agent_message_ms = 0
    latest_agent_message_text = ""
    latest_tool_call_ms = 0
    latest_human_input_request_ms = 0

    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                item_type = str(item.get("type") or "")
                payload_type = str(payload.get("type") or "")
                event_ms = iso_to_ms(item.get("timestamp"))
                if event_ms:
                    latest_event_ms = max(latest_event_ms, event_ms)
                if item_type == "session_meta":
                    session_id = str(payload.get("id") or session_id)
                    cwd = str(payload.get("cwd") or cwd)
                    updated_ms = iso_to_ms(payload.get("timestamp")) or updated_ms
                elif payload_type == "user_message":
                    latest_user_ms = event_ms or latest_user_ms
                elif payload_type == "task_started" or item_type == "turn_context" or payload_type == "turn_context":
                    latest_turn_id = str(payload.get("turn_id") or item.get("turn_id") or latest_turn_id)
                    latest_turn_ms = safe_ms(payload.get("started_at")) or event_ms or latest_turn_ms
                    updated_ms = latest_turn_ms or event_ms or updated_ms
                elif payload_type == "agent_message":
                    latest_agent_message_ms = event_ms or latest_agent_message_ms
                    latest_agent_message_text = str(payload.get("message") or latest_agent_message_text)
                    updated_ms = event_ms or updated_ms
                elif item_type == "response_item" and payload_type == "message":
                    role = str(payload.get("role") or "")
                    if role == "assistant":
                        text = extract_text(payload.get("content"))
                        if text:
                            latest_agent_message_ms = event_ms or latest_agent_message_ms
                            latest_agent_message_text = text
                            updated_ms = event_ms or updated_ms
                    elif role == "user":
                        latest_user_ms = event_ms or latest_user_ms
                elif item_type == "response_item" and payload_type in {
                    "function_call",
                    "custom_tool_call",
                    "local_shell_call",
                    "mcp_tool_call",
                    "tool_search_call",
                    "web_search_call",
                    "image_generation_call",
                }:
                    name = str(payload.get("name") or "")
                    if name in CODEX_HUMAN_INPUT_FUNCTIONS:
                        latest_human_input_request_ms = event_ms or latest_human_input_request_ms
                    latest_tool_call_ms = event_ms or latest_tool_call_ms
                elif payload_type == "token_count":
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    raw_usage = info.get("total_token_usage")
                    if isinstance(raw_usage, dict):
                        token_usage = raw_usage
                    limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
                    primary = limits.get("primary") if isinstance(limits.get("primary"), dict) else {}
                    rate_percent = safe_float(primary.get("used_percent"))
                    updated_ms = iso_to_ms(item.get("timestamp")) or updated_ms
                elif payload_type in {"task_complete", "turn_aborted"}:
                    if payload_type == "task_complete":
                        turns += 1
                    latest_completed_turn_id = str(payload.get("turn_id") or latest_completed_turn_id)
                    latest_completed_ms = safe_ms(payload.get("completed_at")) or latest_completed_ms
                    updated_ms = latest_completed_ms or updated_ms
    except OSError:
        return None

    scan = {
        "_key": cache_key,
        "session_id": session_id,
        "cwd": cwd,
        "updated_ms": updated_ms,
        "token_usage": token_usage,
        "rate_percent": rate_percent,
        "turns": turns,
        "latest_turn_id": latest_turn_id,
        "latest_turn_ms": latest_turn_ms,
        "latest_completed_turn_id": latest_completed_turn_id,
        "latest_completed_ms": latest_completed_ms,
        "latest_event_ms": latest_event_ms,
        "latest_user_ms": latest_user_ms,
        "latest_agent_message_ms": latest_agent_message_ms,
        "latest_tool_call_ms": latest_tool_call_ms,
        "latest_human_input_request_ms": latest_human_input_request_ms,
        "waiting_for_user": (
            is_unanswered_human_input_request(latest_human_input_request_ms, latest_user_ms)
            or (
                latest_tool_call_ms < latest_agent_message_ms
                and is_waiting_for_human(latest_agent_message_ms, latest_user_ms, latest_agent_message_text)
            )
        ),
    }
    _CODEX_SESSION_CACHE[path] = scan
    return scan


def codex_usage_records(max_files: int = 80) -> List[Dict[str, Any]]:
    root = os.path.expanduser("~/.codex/sessions")
    index = codex_session_index()
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    files = sorted(files, key=os.path.getmtime, reverse=True)[:max_files]
    records: List[Dict[str, Any]] = []

    for path in files:
        scan = _scan_codex_session(path)
        if scan is None:
            continue
        session_id = scan["session_id"]
        cwd = scan["cwd"]
        updated_ms = scan["updated_ms"]
        token_usage = scan["token_usage"]
        rate_percent = scan["rate_percent"]
        turns = scan["turns"]
        latest_turn_id = scan["latest_turn_id"]
        latest_turn_ms = scan["latest_turn_ms"]
        latest_completed_turn_id = scan["latest_completed_turn_id"]
        latest_completed_ms = scan["latest_completed_ms"]
        latest_event_ms = scan["latest_event_ms"]
        latest_human_input_request_ms = scan["latest_human_input_request_ms"]
        waiting_for_user = bool(scan.get("waiting_for_user"))

        indexed = index.get(session_id) or {}
        title = str(indexed.get("title") or "")
        indexed_updated = safe_int(indexed.get("updated_ms"))
        if indexed_updated > updated_ms:
            updated_ms = indexed_updated

        usage: Dict[str, Any] = {}
        if token_usage:
            total = total_tokens_from_usage(
                token_usage,
                ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"],
            )
            usage = build_usage(
                total_tokens=total,
                turns=turns,
                rate_percent=rate_percent,
                fields={
                    "source": "codex-session",
                    **token_usage,
                },
            )

        if not session_id and not cwd and not usage:
            continue
        records.append(
            {
                "id": session_id,
                "cwd": cwd,
                "folder": folder_label(cwd),
                "title": title,
                "updated_ms": updated_ms,
                "latest_event_ms": latest_event_ms,
                "latest_turn_id": latest_turn_id,
                "latest_turn_ms": latest_turn_ms,
                "latest_completed_turn_id": latest_completed_turn_id,
                "latest_completed_ms": latest_completed_ms,
                "latest_human_input_request_ms": latest_human_input_request_ms,
                "active_turn": bool(latest_turn_id and latest_turn_id != latest_completed_turn_id),
                "waiting_for_user": waiting_for_user,
                "path": path,
                "usage": usage,
            }
        )

    return records


def latest_codex_record(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not records:
        return {}
    return max(records, key=lambda item: int(item.get("updated_ms") or 0))


def latest_codex_usage(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    return latest_codex_record(records).get("usage") or {}


def codex_record_for_cwd(records: List[Dict[str, Any]], cwd: str) -> Dict[str, Any]:
    if not cwd:
        return {}
    matches = [item for item in records if item.get("cwd") == cwd]
    if not matches:
        return {}
    return max(matches, key=lambda item: int(item.get("updated_ms") or 0))


def codex_usage_for_cwd(records: List[Dict[str, Any]], cwd: str) -> Dict[str, Any]:
    return codex_record_for_cwd(records, cwd).get("usage") or {}


def codex_title(record: Dict[str, Any], fallback: str = "Codex") -> str:
    title = str(record.get("title") or "").strip()
    if title:
        return title
    folder = str(record.get("folder") or "").strip()
    if folder:
        return f"Codex · {folder}"
    return fallback


def codex_subtitle(record: Dict[str, Any], fallback: str = "") -> str:
    folder = str(record.get("folder") or "").strip()
    cwd = str(record.get("cwd") or "").strip()
    if folder:
        return folder
    if cwd:
        return cwd
    return fallback


def codex_status(record: Dict[str, Any], app_is_running: bool = False) -> str:
    latest = safe_int(record.get("latest_event_ms") or record.get("updated_ms"))
    if record.get("waiting_for_user"):
        return "waiting"
    active_turn = bool(record.get("active_turn"))
    if active_turn and latest and now_ms() - latest < CODEX_RUNNING_STALE_MS:
        return "running"
    if latest and now_ms() - latest < ACTIVE_MINUTES * 60 * 1000:
        return "recent"
    return "idle" if app_is_running else "idle"


class OpenClawAdapter:
    source = "OpenClaw"

    def __init__(self) -> None:
        candidates = [
            os.environ.get("OPENCLAW_BIN") or "",
            os.path.expanduser("~/.local/npm/bin/openclaw"),
            shutil.which("openclaw") or "",
        ]
        self.bin = next((path for path in candidates if path and os.path.exists(path)), "")
        self.state_dir = os.path.expanduser(os.environ.get("OPENCLAW_STATE_DIR", "~/.openclaw"))

    def available(self) -> bool:
        return bool((self.bin and os.path.exists(self.bin)) or os.path.isdir(self.state_dir))

    def list_tasks(self) -> List[Task]:
        if not self.available():
            return []

        tasks: List[Task] = []
        tasks.extend(self._list_task_runs())
        tasks.extend(self._list_sessions())

        if not tasks and self.bin and os.path.exists(self.bin):
            tasks.extend(self._list_cli_fallback())

        return tasks

    def _list_task_runs(self) -> List[Task]:
        db_path = os.path.join(self.state_dir, "tasks", "runs.sqlite")
        if not os.path.exists(db_path):
            return []

        rows: List[Dict[str, Any]] = []
        try:
            conn = sqlite3.connect(db_path, timeout=0.4)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            query = """
                SELECT
                  task_id, runtime, task_kind, source_id, requester_session_key,
                  owner_key, scope_kind, child_session_key, parent_flow_id,
                  parent_task_id, agent_id, run_id, label, status,
                  delivery_status, notify_policy, created_at, started_at,
                  ended_at, last_event_at, cleanup_after, terminal_outcome
                FROM task_runs
                ORDER BY COALESCE(last_event_at, ended_at, started_at, created_at) DESC
                LIMIT 50
            """
            rows = [dict(row) for row in conn.execute(query)]
            conn.close()
        except (OSError, sqlite3.Error):
            return []

        out: List[Task] = []
        cutoff = now_ms() - ACTIVE_MINUTES * 60 * 1000
        for item in rows:
            updated = safe_ms(
                item.get("last_event_at")
                or item.get("ended_at")
                or item.get("started_at")
                or item.get("created_at")
            )
            raw_status = str(item.get("status") or "")
            status = self._status_from_openclaw(raw_status, updated, ended_ms=safe_ms(item.get("ended_at")))
            if status not in {"running", "waiting", "failed"} and updated and updated < cutoff:
                continue
            raw_id = str(item.get("task_id") or item.get("run_id") or item)
            title = str(item.get("label") or item.get("task_kind") or item.get("runtime") or "OpenClaw task")
            subtitle = join_parts(
                [item.get("agent_id"), item.get("runtime"), item.get("delivery_status")],
                " · ",
            )
            out.append(
                task(
                    task_id=stable_id("openclaw-task", raw_id),
                    source=self.source,
                    title=title,
                    status=status,
                    updated_ms=updated,
                    subtitle=subtitle,
                    detail={
                        "task_id": item.get("task_id"),
                        "run_id": item.get("run_id"),
                        "runtime": item.get("runtime"),
                        "task_kind": item.get("task_kind"),
                        "status": raw_status,
                        "delivery_status": item.get("delivery_status"),
                        "notify_policy": item.get("notify_policy"),
                        "agent_id": item.get("agent_id"),
                        "owner_key": item.get("owner_key"),
                        "requester_session_key": item.get("requester_session_key"),
                        "started_at": ms_to_iso(safe_ms(item.get("started_at"))),
                        "ended_at": ms_to_iso(safe_ms(item.get("ended_at"))),
                        "last_event_at": ms_to_iso(safe_ms(item.get("last_event_at"))),
                        "terminal_outcome": item.get("terminal_outcome"),
                    },
                    open_action={"type": "url", "target": "http://127.0.0.1:18789/tasks"},
                    needs_attention=status in {"waiting", "failed"},
                )
            )
        return out

    def _list_sessions(self) -> List[Task]:
        pattern = os.path.join(self.state_dir, "agents", "*", "sessions", "sessions.json")
        paths = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p), reverse=True)
        cutoff = now_ms() - ACTIVE_MINUTES * 60 * 1000
        out: List[Task] = []

        for path in paths:
            agent_id = self._agent_id_from_session_store(path)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    store = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(store, dict):
                continue

            for key, entry in store.items():
                if not isinstance(entry, dict):
                    continue
                updated = safe_ms(entry.get("updatedAt") or entry.get("endedAt") or entry.get("startedAt"))
                raw_status = str(entry.get("status") or "")
                status = self._status_from_openclaw(
                    raw_status,
                    updated,
                    ended_ms=safe_ms(entry.get("endedAt")),
                    aborted=bool(entry.get("abortedLastRun")),
                )
                if status not in {"running", "waiting", "failed"} and updated and updated < cutoff:
                    continue

                session_key = str(key)
                session_id = str(entry.get("sessionId") or session_key)
                model = str(entry.get("model") or "session")
                effective_agent = agent_id or self._agent_from_session_key(session_key) or str(entry.get("agentId") or "main")
                title = f"{effective_agent} · {model}"
                usage = self._usage_from_session(entry)
                subtitle_parts = [effective_agent, entry.get("chatType")]
                if entry.get("lastTo"):
                    subtitle_parts.append(entry.get("lastTo"))
                subtitle = join_parts(subtitle_parts, " · ")
                out.append(
                    task(
                        task_id=stable_id("openclaw-session", session_key),
                        source=self.source,
                        title=title,
                        status=status,
                        updated_ms=updated,
                        subtitle=subtitle,
                        detail={
                            "key": session_key,
                            "session_id": session_id,
                            "agent_id": effective_agent,
                            "status": raw_status,
                            "model_provider": entry.get("modelProvider"),
                            "model": entry.get("model"),
                            "chat_type": entry.get("chatType"),
                            "last_to": entry.get("lastTo"),
                            "started_at": ms_to_iso(safe_ms(entry.get("startedAt"))),
                            "ended_at": ms_to_iso(safe_ms(entry.get("endedAt"))),
                            "runtime_ms": entry.get("runtimeMs"),
                            "aborted_last_run": bool(entry.get("abortedLastRun")),
                            "total_tokens_fresh": entry.get("totalTokensFresh"),
                        },
                        usage=usage,
                        open_action={"type": "url", "target": "http://127.0.0.1:18789/sessions"},
                        needs_attention=status in {"waiting", "failed"},
                    )
                )

        return sorted(out, key=lambda item: int(item.get("updated_ms") or 0), reverse=True)[:30]

    def _list_cli_fallback(self) -> List[Task]:
        out: List[Task] = []

        task_data = run_json([self.bin, "tasks", "list", "--json"], timeout=5)
        for item in (task_data or {}).get("tasks", []) or []:
            raw_id = str(item.get("taskId") or item.get("id") or item.get("runId") or item)
            status = normalize_status(str(item.get("status") or "unknown"))
            title = str(item.get("title") or item.get("name") or item.get("kind") or "OpenClaw task")
            updated = safe_ms(item.get("updatedAt") or item.get("createdAt"))
            out.append(
                task(
                    task_id=stable_id("openclaw-task", raw_id),
                    source=self.source,
                    title=title,
                    status=status,
                    updated_ms=updated,
                    subtitle=str(item.get("kind") or item.get("runtime") or ""),
                    detail=item if isinstance(item, dict) else {},
                    open_action={"type": "url", "target": "http://127.0.0.1:18789/tasks"},
                    needs_attention=status in {"waiting", "failed"},
                )
            )

        session_data = run_json(
            [
                self.bin,
                "sessions",
                "--all-agents",
                "--active",
                str(ACTIVE_MINUTES),
                "--limit",
                "30",
                "--json",
            ],
            timeout=6,
        )
        for item in (session_data or {}).get("sessions", []) or []:
            key = str(item.get("key") or item.get("sessionId") or item)
            age_ms = int(item.get("ageMs") or 10**9)
            status = "running" if age_ms < OPENCLAW_RUNNING_STALE_MS else "recent"
            agent = item.get("agentId") or "main"
            model = item.get("model") or "session"
            tokens = item.get("totalTokens")
            subtitle = f"{agent}"
            if tokens:
                subtitle += f" · {int(tokens) // 1000}k tokens"
            title = f"{agent} · {model}"
            out.append(
                task(
                    task_id=stable_id("openclaw-session", key),
                    source=self.source,
                    title=title,
                    status=status,
                    updated_ms=safe_ms(item.get("updatedAt")),
                    subtitle=subtitle,
                    detail={
                        "key": key,
                        "session_id": item.get("sessionId"),
                        "model_provider": item.get("modelProvider"),
                        "kind": item.get("kind"),
                    },
                    open_action={"type": "url", "target": "http://127.0.0.1:18789/sessions"},
                )
            )

        return out

    def _status_from_openclaw(
        self,
        raw_status: str,
        updated_ms: Optional[int],
        *,
        ended_ms: Optional[int] = None,
        aborted: bool = False,
    ) -> str:
        status = normalize_status(raw_status)
        latest = safe_int(updated_ms or ended_ms)
        if aborted and latest and now_ms() - latest < ACTIVE_MINUTES * 60 * 1000:
            return "failed"
        if status == "running":
            if not latest or now_ms() - latest < OPENCLAW_RUNNING_STALE_MS:
                return "running"
            return "recent"
        if status in {"waiting", "failed"}:
            return status
        if latest and now_ms() - latest < ACTIVE_MINUTES * 60 * 1000:
            return "recent"
        return "idle"

    def _usage_from_session(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        fields = {
            "source": "openclaw-session",
            "input_tokens": safe_int(entry.get("inputTokens")),
            "output_tokens": safe_int(entry.get("outputTokens")),
            "cache_read_tokens": safe_int(entry.get("cacheRead")),
            "cache_write_tokens": safe_int(entry.get("cacheWrite")),
            "context_tokens": safe_int(entry.get("contextTokens")),
            "estimated_cost_usd": entry.get("estimatedCostUsd"),
        }
        return build_usage(total_tokens=safe_int(entry.get("totalTokens")), fields=fields)

    def _agent_id_from_session_store(self, path: str) -> str:
        parts = path.split(os.sep)
        try:
            idx = parts.index("agents")
            return parts[idx + 1]
        except (ValueError, IndexError):
            return ""

    def _agent_from_session_key(self, key: str) -> str:
        match = re.match(r"agent:([^:]+):", key)
        return match.group(1) if match else ""


class ClaudeAdapter:
    source = "Claude"

    def list_tasks(self, commands: Optional[List[str]] = None) -> List[Task]:
        commands = commands or ps_commands()
        running_resumes = set()
        # Detect any Claude Code process by --resume flag. Case-insensitive
        # because the active binary lives under `claude.app/` (lowercase) while
        # only the launcher wrapper sits in `Claude.app/`; the old anchored
        # match silently dropped the bare claude.app process (≈half the time).
        claude_re = re.compile(r"\bclaude\b", re.IGNORECASE)
        for line in commands:
            if claude_re.search(line) and "--resume" in line:
                running_resumes.update(re.findall(r"--resume\s+([^\s]+)", line))

        roots = [
            os.path.expanduser("~/Library/Application Support/Claude/claude-code-sessions"),
            os.path.expanduser("~/Library/Application Support/Claude/local-agent-mode-sessions"),
        ]
        files: List[str] = []
        for root in roots:
            if os.path.isdir(root):
                files.extend(glob.glob(os.path.join(root, "**", "local_*.json"), recursive=True))
        files = sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)[:100]

        seen = set()
        tasks: List[Task] = []
        cutoff = now_ms() - ACTIVE_MINUTES * 60 * 1000
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    item = json.load(fh)
            except Exception:
                continue
            session_id = str(item.get("sessionId") or "")
            cli_session_id = str(item.get("cliSessionId") or "")
            key = session_id or cli_session_id or path
            if key in seen:
                continue
            seen.add(key)
            updated = safe_ms(item.get("lastActivityAt") or item.get("lastFocusedAt") or item.get("createdAt"))
            is_running = bool({session_id, cli_session_id} & running_resumes)
            # Skip the expensive JSONL scan for sessions metadata says are older
            # than the active window AND whose process isn't running. This is
            # what keeps a user with hundreds of archived sessions snappy — we
            # only ever open the few transcripts that could still be relevant.
            if not is_running and updated and updated < cutoff:
                continue
            metrics = claude_session_metrics(cli_session_id, item.get("completedTurns"))
            transcript_updated = safe_int(metrics.get("latest_event_ms"))
            if transcript_updated and (not updated or transcript_updated > updated):
                updated = transcript_updated
            status = claude_status(metrics, updated, process_running=is_running)
            if status != "running" and not is_running and updated and updated < cutoff:
                continue
            if item.get("isArchived") and status != "running" and not is_running:
                continue
            cwd = str(item.get("cwd") or item.get("originCwd") or "")
            title = str(item.get("title") or os.path.basename(cwd) or "Claude Code session")
            subtitle = claude_subtitle(metrics, cwd)
            usage = metrics.get("usage") or {}
            tasks.append(
                task(
                    task_id=stable_id("claude", key),
                    source=self.source,
                    title=title,
                    status=status,
                    updated_ms=updated,
                    subtitle=subtitle,
                    detail={
                        "session_id": session_id,
                        "cli_session_id": cli_session_id,
                        "cwd": cwd,
                        "model": item.get("model"),
                        "effort": item.get("effort"),
                        "completed_turns": item.get("completedTurns"),
                        "process_running": is_running,
                        "active_turn": bool(metrics.get("active_turn")),
                        "waiting_for_user": bool(metrics.get("waiting_for_user")),
                        "turn_state": claude_turn_state(metrics),
                        "latest_stop_reason": metrics.get("latest_stop_reason"),
                        "latest_event_at": ms_to_iso(safe_int(metrics.get("latest_event_ms"))),
                        "transcript_found": bool(metrics.get("transcript_found")),
                    },
                    usage=usage,
                    open_action={"type": "app", "target": "com.anthropic.claudefordesktop"},
                    needs_attention=status == "waiting",
                )
            )

        known_ids = set()
        for item in tasks:
            detail = item.get("detail") or {}
            for value in (detail.get("session_id"), detail.get("cli_session_id")):
                if value:
                    known_ids.add(str(value))
        for resume_id in sorted(running_resumes):
            if resume_id and resume_id not in known_ids:
                metrics = claude_session_metrics(resume_id)
                status = claude_status(metrics, safe_int(metrics.get("latest_event_ms")), process_running=True)
                tasks.append(
                    task(
                        task_id=stable_id("claude", resume_id),
                        source=self.source,
                        title=f"Claude Code {resume_id[:8]}",
                        status=status,
                        subtitle=claude_subtitle(metrics, "", "running process"),
                        detail={
                            "session_id": resume_id,
                            "process_running": True,
                            "active_turn": bool(metrics.get("active_turn")),
                            "waiting_for_user": bool(metrics.get("waiting_for_user")),
                            "turn_state": claude_turn_state(metrics),
                            "latest_stop_reason": metrics.get("latest_stop_reason"),
                            "latest_event_at": ms_to_iso(safe_int(metrics.get("latest_event_ms"))),
                            "transcript_found": bool(metrics.get("transcript_found")),
                        },
                        usage=metrics.get("usage") or {},
                        open_action={"type": "app", "target": "com.anthropic.claudefordesktop"},
                        needs_attention=status == "waiting",
                    )
                )

        return tasks


class CodexAdapter:
    source = "Codex"

    def list_tasks(self, commands: Optional[List[str]] = None) -> List[Task]:
        commands = commands or ps_commands()
        tasks: List[Task] = []
        usage_records = codex_usage_records()
        app_is_running = app_running(commands, "/Applications/Codex.app", "Codex")
        # Case-insensitive: Codex CLI installs may render the path with mixed
        # case depending on launcher; same lesson learned from the Claude fix.
        app_server_count = sum(ci_contains("Codex.app/Contents/Resources/codex app-server", line) for line in commands)
        titles = window_titles("Codex") if app_is_running else []

        if app_is_running:
            record = latest_codex_record(usage_records)
            window_title = titles[0] if titles and titles[0] != "Codex" else "Codex desktop"
            title = codex_title(record, window_title)
            status = codex_status(record, app_is_running=True)
            usage = record.get("usage") or {}
            subtitle = codex_subtitle(record, f"{app_server_count} app-server process(es)")
            tasks.append(
                task(
                    task_id="codex-app",
                    source=self.source,
                    title=title,
                    status=status,
                    subtitle=subtitle,
                    detail={
                        "session_id": record.get("id"),
                        "cwd": record.get("cwd"),
                        "folder": record.get("folder"),
                        "usage_source": usage.get("source"),
                        "app_server_count": app_server_count,
                        "waiting_for_user": bool(record.get("waiting_for_user")),
                    },
                    usage=usage,
                    open_action={"type": "app", "target": "com.openai.codex"},
                    needs_attention=status == "waiting",
                )
            )

        for line in commands:
            if not ci_contains("/Codex.app/Contents/Resources/node ", line) or "--session-id" not in line:
                continue
            session = first_match(r"--session-id\s+([^\s]+)", line)
            cwd = first_match(r"--working-dir\s+(.+?)(?:\s--|$)", line)
            if not session:
                continue
            record = codex_record_for_cwd(usage_records, cwd)
            project = folder_label(cwd) or session[:8]
            title = codex_title(record, f"Codex · {project}")
            subtitle = codex_subtitle(record, project)
            status = codex_status(record, app_is_running=True)
            usage = record.get("usage") or {}
            tasks.append(
                task(
                    task_id=stable_id("codex-kernel", session),
                    source=self.source,
                    title=title,
                    status=status,
                    subtitle=subtitle or "active kernel",
                    detail={
                        "session_id": session,
                        "codex_session_id": record.get("id"),
                        "cwd": cwd or record.get("cwd"),
                        "folder": record.get("folder") or project,
                        "usage_source": usage.get("source"),
                        "waiting_for_user": bool(record.get("waiting_for_user")),
                    },
                    usage=usage,
                    open_action={"type": "app", "target": "com.openai.codex"},
                    needs_attention=status == "waiting",
                )
            )

        if not tasks:
            record = latest_codex_record(usage_records)
            usage = record.get("usage") or {}
            tasks.append(
                task(
                    task_id="codex-idle",
                    source=self.source,
                    title=codex_title(record, "Codex"),
                    status="idle",
                    subtitle=codex_subtitle(record, "not running"),
                    detail={
                        "session_id": record.get("id"),
                        "cwd": record.get("cwd"),
                        "folder": record.get("folder"),
                        "waiting_for_user": bool(record.get("waiting_for_user")),
                    },
                    usage=usage,
                    open_action={"type": "app", "target": "com.openai.codex"},
                    needs_attention=bool(record.get("waiting_for_user")),
                )
            )
        return tasks


MANUS_LEVEL_HELPER = r"""
const fs = require("fs");
const path = require("path");

function loadClassicLevel(moduleDirs) {
  for (const dir of moduleDirs) {
    try {
      const resolved = require.resolve("classic-level", { paths: [dir] });
      return require(resolved).ClassicLevel;
    } catch (_) {}
  }
  throw new Error("classic-level module not found");
}

function utf16be(buffer) {
  const even = buffer.length % 2 ? buffer.subarray(0, buffer.length - 1) : buffer;
  return Buffer.from(even).swap16().toString("utf16le").replace(/[\u0000-\u001f]/g, " ");
}

function getValue(chunk, key) {
  const stringMatch = chunk.match(new RegExp(`"${key}"\\s*:\\s*"([^"\\\\]{0,240})"`));
  if (stringMatch) return stringMatch[1];
  const numberMatch = chunk.match(new RegExp(`"${key}"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)`));
  if (numberMatch) return Number(numberMatch[1]);
  const boolMatch = chunk.match(new RegExp(`"${key}"\\s*:\\s*(true|false)`));
  if (boolMatch) return boolMatch[1] === "true";
  return null;
}

function timestampAfter(chunk, key) {
  const pos = chunk.indexOf(`"${key}"`);
  if (pos < 0) return 0;
  const part = chunk.slice(pos, pos + 260);
  const seconds = part.match(/"seconds"\s*:\s*(\d+)/);
  if (!seconds) return 0;
  const nanos = part.match(/"nanos"\s*:\s*(\d+)/);
  return Number(seconds[1]) * 1000 + (nanos ? Math.floor(Number(nanos[1]) / 1000000) : 0);
}

function extractText(chunk, key) {
  const startNeedle = `"${key}":"`;
  const start = chunk.indexOf(startNeedle);
  if (start < 0) return "";
  const valueStart = start + startNeedle.length;
  const delimiters = [
    `","uid"`,
    `","icon"`,
    `","lastDisplayMessage"`,
    `","unreadMessageCount"`,
    `","userId"`,
    `","status"`,
  ];
  let end = -1;
  for (const delimiter of delimiters) {
    const idx = chunk.indexOf(delimiter, valueStart);
    if (idx >= 0 && (end < 0 || idx < end)) end = idx;
  }
  if (end < 0) return "";
  let value = chunk.slice(valueStart, end).replace(/\s+/g, " ").trim();
  value = value.replace(/[^\p{L}\p{N}\p{Script=Han}\s.,:;!?()/_-]/gu, "").trim();
  if (value.includes("icon:")) return "";
  if (value.length > 96) value = value.slice(0, 96).trim();
  const asciiLetters = (value.match(/[A-Za-z0-9]/g) || []).length;
  const han = (value.match(/\p{Script=Han}/gu) || []).length;
  return asciiLetters >= 3 || han >= 2 ? value : "";
}

function usageFromChunk(chunk) {
  const pos = chunk.indexOf('"usage"');
  if (pos < 0) return {};
  const part = chunk.slice(pos, pos + 360);
  const number = (key) => {
    const m = part.match(new RegExp(`"${key}"\\s*:\\s*(\\d+)`));
    return m ? Number(m[1]) : 0;
  };
  return {
    commandsRun: number("commandsRun"),
    filesCreated: number("filesCreated"),
    apiCalled: number("apiCalled"),
    cumulativeRuntimeMs: number("cumulativeRuntimeMs"),
  };
}

async function main() {
  const dbPath = process.argv[1];
  const moduleDirs = JSON.parse(process.argv[2] || "[]");
  const ClassicLevel = loadClassicLevel(moduleDirs);
  const db = new ClassicLevel(dbPath, { keyEncoding: "buffer", valueEncoding: "buffer" });
  await db.open();
  const rowsByUid = new Map();
  let taskFinished = null;
  let lastActiveMs = 0;

  for await (const [keyBuffer, value] of db.iterator()) {
    const key = Buffer.from(keyBuffer).toString("utf8");
    if (key.includes("task_finished")) {
      const raw = Buffer.from(value).toString("utf8").replace(/[\u0000-\u001f]/g, "").trim();
      taskFinished = raw === "true" ? true : raw === "false" ? false : taskFinished;
    }
    if (key.includes("last_active_time")) {
      const raw = Buffer.from(value).toString("utf8").replace(/[\u0000-\u001f"]/g, "").trim();
      const parsed = Number(raw);
      if (Number.isFinite(parsed)) lastActiveMs = parsed;
    }
    if (!key.includes("sessions_detail")) continue;

    const text = utf16be(value);
    const chunks = text
      .split('{"$typeName":"session.v1.AgentSession"')
      .slice(1)
      .map((part) => '{"$typeName":"session.v1.AgentSession"' + part);
    for (const chunk of chunks) {
      const uid = getValue(chunk, "uid");
      if (!uid) continue;
      const usage = usageFromChunk(chunk);
      const row = {
        uid,
        statusCode: getValue(chunk, "status"),
        agentTaskMode: getValue(chunk, "agentTaskMode"),
        isFavorite: getValue(chunk, "isFavorite"),
        unreadMessageCount: getValue(chunk, "unreadMessageCount"),
        newMessageCount: getValue(chunk, "newMessageCount"),
        scheduledTask: getValue(chunk, "scheduledTask"),
        costedCredits: getValue(chunk, "costedCredits"),
        title: extractText(chunk, "displayTitle") || extractText(chunk, "title"),
        displayTitle: extractText(chunk, "displayTitle"),
        createdMs: timestampAfter(chunk, "createdAt"),
        updatedMs: timestampAfter(chunk, "updatedAt"),
        lastMessageMs: timestampAfter(chunk, "lastMessageTime"),
        lastReadMs: timestampAfter(chunk, "lastReadAt"),
        ...usage,
      };
      const existing = rowsByUid.get(uid);
      const rowFreshness = Math.max(row.updatedMs || 0, row.lastMessageMs || 0, row.createdMs || 0);
      const existingFreshness = existing
        ? Math.max(existing.updatedMs || 0, existing.lastMessageMs || 0, existing.createdMs || 0)
        : 0;
      if (!existing || rowFreshness >= existingFreshness) rowsByUid.set(uid, row);
    }
  }
  await db.close();
  console.log(JSON.stringify({ ok: true, taskFinished, lastActiveMs, sessions: [...rowsByUid.values()] }));
}

main().catch((error) => {
  console.log(JSON.stringify({ ok: false, error: String(error && error.message || error) }));
  process.exit(0);
});
"""


class ManusAdapter:
    source = "Manus"

    def __init__(self) -> None:
        self.app_name = "Manus"
        self.bundle_id = "im.manus.desktop"
        self.bundle_fragment = "/Applications/Manus.app"
        self.binary_name = "Manus"
        self.leveldb_dir = os.path.expanduser("~/Library/Application Support/Manus/Local Storage/leveldb")

    def list_tasks(self, commands: Optional[List[str]] = None) -> List[Task]:
        commands = commands or ps_commands()
        running = app_running(commands, self.bundle_fragment, self.binary_name)
        data = self._read_local_sessions()
        sessions = data.get("sessions") if isinstance(data.get("sessions"), list) else []
        if not sessions:
            return [self._fallback_task(running)]

        task_finished = data.get("taskFinished")
        rows: List[Task] = []
        cutoff = now_ms() - ACTIVE_MINUTES * 60 * 1000
        for item in sessions:
            if not isinstance(item, dict):
                continue
            updated = max(
                safe_int(item.get("updatedMs")),
                safe_int(item.get("lastMessageMs")),
                safe_int(item.get("createdMs")),
            )
            status = self._status(item, task_finished)
            if status not in {"running", "waiting", "failed", "done"} and updated and updated < cutoff:
                continue
            title = str(item.get("title") or "").strip() or f"Manus · {str(item.get('uid') or '')[:8]}"
            subtitle = join_parts(
                [
                    self._status_label(item.get("statusCode")),
                    self._runtime_label(safe_int(item.get("cumulativeRuntimeMs"))),
                ],
                " · ",
            )
            usage = self._usage(item)
            uid = str(item.get("uid") or title)
            rows.append(
                task(
                    task_id=stable_id("manus-session", uid),
                    source=self.source,
                    title=title,
                    status=status,
                    updated_ms=updated,
                    subtitle=subtitle or ("app open" if running else "not running"),
                    detail={
                        "uid": uid,
                        "status_code": item.get("statusCode"),
                        "status_label": self._status_label(item.get("statusCode")),
                        "agent_task_mode": item.get("agentTaskMode"),
                        "task_finished": task_finished,
                        "last_active_at": ms_to_iso(safe_int(data.get("lastActiveMs"))),
                        "created_at": ms_to_iso(safe_int(item.get("createdMs"))),
                        "updated_at": ms_to_iso(safe_int(item.get("updatedMs"))),
                        "last_message_at": ms_to_iso(safe_int(item.get("lastMessageMs"))),
                        "commands_run": item.get("commandsRun"),
                        "files_created": item.get("filesCreated"),
                        "costed_credits": item.get("costedCredits"),
                        "app_running": running,
                    },
                    usage=usage,
                    open_action={"type": "app", "target": self.bundle_id},
                    needs_attention=status in {"waiting", "failed"},
                )
            )

        if rows:
            return sorted(rows, key=sort_key)[:MANUS_MAX_SESSIONS]
        return [self._fallback_task(running)]

    def _fallback_task(self, running: bool) -> Task:
        return task(
            task_id="manus-app",
            source=self.source,
            title="Manus",
            status="idle",
            subtitle="app open" if running else "not running",
            open_action={"type": "app", "target": self.bundle_id},
        )

    def _read_local_sessions(self) -> Dict[str, Any]:
        if not os.path.isdir(self.leveldb_dir):
            return {}
        module_dirs = self._node_module_dirs()
        node = node_command()
        if not module_dirs or not node:
            return {}
        with tempfile.TemporaryDirectory(prefix="sticks3-manus-") as tmp:
            copy_path = os.path.join(tmp, "leveldb")
            try:
                shutil.copytree(self.leveldb_dir, copy_path)
            except OSError:
                return {}
            code, out, _ = run(
                [node, "-e", MANUS_LEVEL_HELPER, copy_path, json.dumps(module_dirs)],
                timeout=2.5,
            )
        if code != 0 or not out.strip():
            return {}
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) and parsed.get("ok") else {}

    def _node_module_dirs(self) -> List[str]:
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_modules"),
            os.path.expanduser("~/Library/Application Support/StickS3TaskHub/node_modules"),
            "/tmp/sticks3-manus-level/node_modules",
        ]
        return [path for path in candidates if os.path.isdir(path)]

    def _status(self, item: Dict[str, Any], task_finished: Any) -> str:
        latest = max(
            safe_int(item.get("updatedMs")),
            safe_int(item.get("lastMessageMs")),
            safe_int(item.get("createdMs")),
        )
        status_code = safe_int(item.get("statusCode"))
        if task_finished is False and latest and now_ms() - latest < MANUS_RUNNING_STALE_MS:
            return "running"
        if status_code in MANUS_TERMINAL_STATUS_CODES:
            return "done"
        if status_code and status_code not in MANUS_TERMINAL_STATUS_CODES:
            if latest and now_ms() - latest < MANUS_RUNNING_STALE_MS:
                return "running"
            return "recent"
        if latest and now_ms() - latest < ACTIVE_MINUTES * 60 * 1000:
            return "recent"
        return "idle"

    def _status_label(self, status_code: Any) -> str:
        code = safe_int(status_code)
        labels = {
            5: "done",
            7: "done",
        }
        return labels.get(code, f"status {code}" if code else "")

    def _runtime_label(self, runtime_ms: int) -> str:
        if runtime_ms <= 0:
            return ""
        minutes = max(1, round(runtime_ms / 60000))
        if minutes < 60:
            return f"{minutes}m runtime"
        return f"{minutes // 60}h {minutes % 60}m runtime"

    def _usage(self, item: Dict[str, Any]) -> Dict[str, Any]:
        commands = safe_int(item.get("commandsRun"))
        files = safe_int(item.get("filesCreated"))
        runtime = safe_int(item.get("cumulativeRuntimeMs"))
        parts: List[str] = []
        if commands:
            parts.append(f"{commands} cmd{'s' if commands != 1 else ''}")
        if files:
            parts.append(f"{files} file{'s' if files != 1 else ''}")
        if runtime:
            parts.append(self._runtime_label(runtime))
        if not parts:
            return {}
        return {
            "label": " · ".join(parts[:3]),
            "commands_run": commands,
            "files_created": files,
            "runtime_ms": runtime,
            "costed_credits": item.get("costedCredits"),
            "source": "manus-local-storage",
        }


class PerplexityAdapter:
    source = "Perplexity"

    def __init__(self) -> None:
        self.app_name = "Perplexity"
        self.bundle_id = "ai.perplexity.macv3"
        self.bundle_fragment = "/Applications/Perplexity.app"
        self.binary_name = "Perplexity"
        self.daemon_bundle_fragment = "Perplexity Helper.app"
        self.daemon_binary_name = "perplexityd"
        self.pref_paths = [
            os.path.expanduser("~/Library/Preferences/ai.perplexity.macv3.plist"),
            os.path.expanduser(
                "~/Library/Containers/ai.perplexity.mac/Data/Library/Preferences/ai.perplexity.mac.plist"
            ),
        ]
        self.cache_dbs = [
            os.path.expanduser("~/Library/Caches/ai.perplexity.macv3/Cache.db"),
            os.path.expanduser(
                "~/Library/Containers/ai.perplexity.mac/Data/Library/Caches/ai.perplexity.mac/Cache.db"
            ),
        ]
        self.computer_cache_dbs = [
            os.path.expanduser("~/Library/Caches/ai.perplexity.macv3/Cache.db"),
            os.path.expanduser("~/Library/Caches/ai.perplexity.macv3.perplexityd/Cache.db"),
        ]
        self.http_storages = [
            os.path.expanduser("~/Library/HTTPStorages/ai.perplexity.macv3/httpstorages.sqlite"),
            os.path.expanduser(
                "~/Library/Containers/ai.perplexity.mac/Data/Library/HTTPStorages/ai.perplexity.mac/httpstorages.sqlite"
            ),
        ]
        self.computer_paths = [
            os.path.expanduser("~/Library/Caches/ai.perplexity.macv3.perplexityd/Cache.db"),
            os.path.expanduser("~/Library/HTTPStorages/ai.perplexity.macv3.perplexityd/httpstorages.sqlite"),
            os.path.expanduser("~/Library/Caches/ai.perplexity.macv3/fsCachedData"),
            os.path.expanduser("~/Library/Caches/ai.perplexity.macv3/tool-output-cache"),
            os.path.expanduser("~/Library/Caches/ai.perplexity.macv3.perplexityd/fsCachedData"),
        ]
        self.indexed_db_glob = os.path.expanduser(
            "~/Library/WebKit/ai.perplexity.macv3/WebsiteData/Default/*/*/IndexedDB/*/IndexedDB.sqlite3"
        )
        self._last_signature = ""
        self._last_change_ms = 0
        self._last_computer_signature = ""
        self._last_computer_change_ms = 0

    def list_tasks(self, commands: Optional[List[str]] = None) -> List[Task]:
        commands = commands or ps_commands()
        running = app_running(commands, self.bundle_fragment, self.binary_name)
        computer_running = self._daemon_running(commands)
        titles = window_titles(self.app_name) if running else []
        state = self._read_local_state()
        signal_ms = max(
            safe_int(state.get("prefs_mtime_ms")),
            safe_int(state.get("cache_mtime_ms")),
            safe_int(state.get("http_mtime_ms")),
            safe_int(state.get("latest_cache_ms")),
        )
        signature = json.dumps(
            {
                "query": state.get("local_query_count"),
                "deep": state.get("local_deep_research_query_count"),
                "prompt": state.get("local_query_count_for_prompt"),
                "cache": state.get("cache_mtime_ms"),
                "http": state.get("http_mtime_ms"),
            },
            sort_keys=True,
        )
        now = now_ms()
        if self._last_signature and signature != self._last_signature:
            self._last_change_ms = now
        self._last_signature = signature

        status = self._status(running, signal_ms)
        title = titles[0] if titles and titles[0] != self.app_name else self.source
        usage = self._usage(state)
        subtitle = self._subtitle(running, state)
        tasks = [
            task(
                task_id="perplexity-app",
                source=self.source,
                title=title,
                status=status,
                updated_ms=signal_ms if signal_ms else None,
                subtitle=subtitle,
                detail={
                    "app_running": running,
                    "local_query_count": state.get("local_query_count"),
                    "local_deep_research_query_count": state.get("local_deep_research_query_count"),
                    "local_query_count_for_prompt": state.get("local_query_count_for_prompt"),
                    "latest_endpoint": state.get("latest_endpoint"),
                    "last_local_signal_at": ms_to_iso(signal_ms),
                    "last_change_at": ms_to_iso(self._last_change_ms),
                    "status_basis": state.get("status_basis"),
                },
                usage=usage,
                open_action={"type": "app", "target": self.bundle_id},
            )
        ]
        computer = self._computer_state(computer_running)
        if computer.get("available"):
            tasks.append(
                task(
                    task_id="perplexity-computer",
                    source=self.source,
                    title=str(computer.get("title") or "Perplexity Computer"),
                    status=self._computer_status(
                        computer_running,
                        safe_int(computer.get("signal_ms")),
                        str(computer.get("thread_status") or ""),
                    ),
                    updated_ms=safe_int(computer.get("signal_ms")) or None,
                    subtitle=self._computer_subtitle(computer_running, computer),
                    detail={
                        "daemon_running": computer_running,
                        "last_local_signal_at": ms_to_iso(safe_int(computer.get("signal_ms"))),
                        "last_change_at": ms_to_iso(self._last_computer_change_ms),
                        "status_basis": computer.get("status_basis"),
                        "latest_endpoint": computer.get("latest_endpoint"),
                        "thread_id": computer.get("thread_id"),
                        "thread_status": computer.get("thread_status"),
                        "title_basis": computer.get("title_basis"),
                    },
                    usage={"label": computer.get("usage_label", "")} if computer.get("usage_label") else {},
                    open_action={"type": "app", "target": self.bundle_id},
                )
            )
        return tasks

    def _status(self, running: bool, signal_ms: int) -> str:
        now = now_ms()
        if running and self._last_change_ms and now - self._last_change_ms < PERPLEXITY_RUNNING_STALE_MS:
            return "running"
        if signal_ms and now - signal_ms < ACTIVE_MINUTES * 60 * 1000:
            return "recent"
        return "idle"

    def _subtitle(self, running: bool, state: Dict[str, Any]) -> str:
        parts = ["app open" if running else "not running"]
        endpoint = state.get("latest_endpoint")
        if endpoint:
            parts.append(str(endpoint))
        elif any(state.get(key) for key in ("prefs_mtime_ms", "cache_mtime_ms", "http_mtime_ms")):
            parts.append("local activity")
        return join_parts(parts, " · ")

    def _daemon_running(self, commands: Iterable[str]) -> bool:
        return any(
            ci_contains(self.daemon_bundle_fragment, line) and ci_contains(f"/MacOS/{self.daemon_binary_name}", line)
            for line in commands
        )

    def _computer_state(self, daemon_running: bool) -> Dict[str, Any]:
        latest_path, signal_ms = self._latest_path_mtime(self.computer_paths)
        indexeddb = self._computer_indexeddb_hint()
        indexeddb_ms = safe_int(indexeddb.get("updated_ms"))
        if indexeddb_ms > signal_ms:
            latest_path = str(indexeddb.get("path") or latest_path)
            signal_ms = indexeddb_ms
        latest_endpoint = ""
        for path in self.computer_cache_dbs:
            row = self._safe_latest_cache_row(path)
            if not row:
                continue
            endpoint = self._sanitize_endpoint(str(row[0] or ""))
            if endpoint:
                latest_endpoint = endpoint
                break
        signature = json.dumps(
            {
                "daemon": daemon_running,
                "signal": signal_ms,
                "path": latest_path,
                "endpoint": latest_endpoint,
                "title": indexeddb.get("title"),
                "thread": indexeddb.get("thread_id"),
                "thread_status": indexeddb.get("thread_status"),
            },
            sort_keys=True,
        )
        now = now_ms()
        if self._last_computer_signature and signature != self._last_computer_signature:
            self._last_computer_change_ms = now
        self._last_computer_signature = signature

        return {
            "available": daemon_running or bool(signal_ms) or bool(indexeddb.get("title")),
            "signal_ms": signal_ms,
            "latest_path": latest_path,
            "latest_endpoint": latest_endpoint,
            "title": indexeddb.get("title"),
            "thread_id": indexeddb.get("thread_id"),
            "thread_status": indexeddb.get("thread_status"),
            "title_basis": indexeddb.get("title_basis"),
            "status_basis": (
                "Perplexity macv3 daemon/cache activity + IndexedDB task cache"
                if indexeddb
                else "Perplexity macv3 daemon/cache activity"
            ),
            "usage_label": "computer daemon" if daemon_running else "",
        }

    def _computer_status(self, daemon_running: bool, signal_ms: int, thread_status: str = "") -> str:
        mapped = self._map_computer_thread_status(thread_status)
        if mapped:
            return mapped
        now = now_ms()
        if (
            daemon_running
            and self._last_computer_change_ms
            and now - self._last_computer_change_ms < PERPLEXITY_COMPUTER_RUNNING_STALE_MS
        ):
            return "running"
        if daemon_running and signal_ms and now - signal_ms < PERPLEXITY_COMPUTER_RUNNING_STALE_MS:
            return "running"
        if signal_ms and now - signal_ms < ACTIVE_MINUTES * 60 * 1000:
            return "recent"
        return "idle"

    def _map_computer_thread_status(self, status: str) -> str:
        normalized = (status or "").strip().lower().replace("-", "_")
        if normalized in {"completed", "complete", "done", "succeeded", "success"}:
            return "done"
        if normalized in {"failed", "error", "errored", "timed_out", "timeout", "cancelled", "canceled"}:
            return "failed"
        if normalized in {"active", "running", "in_progress", "processing", "generating", "streaming"}:
            return "running"
        if normalized in {"queued", "pending", "waiting"}:
            return "waiting"
        return ""

    def _computer_subtitle(self, daemon_running: bool, state: Dict[str, Any]) -> str:
        parts = ["daemon" if daemon_running else "daemon idle"]
        if state.get("signal_ms"):
            parts.append("local activity")
        if state.get("thread_status"):
            parts.append(str(state.get("thread_status")))
        return join_parts(parts, " · ")

    def _computer_indexeddb_hint(self) -> Dict[str, Any]:
        candidates: List[Dict[str, Any]] = []
        paths = sorted(glob.glob(self.indexed_db_glob), key=self._sqlite_content_mtime_ms, reverse=True)[:6]
        for path in paths:
            rows = self._safe_indexeddb_rows(path)
            if not rows:
                continue
            db_mtime = self._sqlite_content_mtime_ms(path)
            for _, key, value in rows:
                candidate = self._computer_candidate_from_idb_record(path, db_mtime, key, value)
                if candidate:
                    candidates.append(candidate)
        if not candidates:
            return {}
        candidates.sort(key=lambda item: (safe_int(item.get("score")), safe_int(item.get("updated_ms"))), reverse=True)
        winner = dict(candidates[0])
        for candidate in candidates[1:]:
            if candidate.get("title") != winner.get("title"):
                continue
            if not winner.get("thread_id") and candidate.get("thread_id"):
                winner["thread_id"] = candidate.get("thread_id")
            if not winner.get("thread_status") and candidate.get("thread_status"):
                winner["thread_status"] = candidate.get("thread_status")
        winner.pop("score", None)
        return winner

    def _safe_indexeddb_rows(self, path: str) -> List[Tuple[Any, Any, Any]]:
        if not os.path.exists(path):
            return []
        try:
            return self._indexeddb_rows(path)
        except Exception:
            with tempfile.TemporaryDirectory(prefix="sticks3-pplx-idb-") as tmp:
                copy_path = os.path.join(tmp, "IndexedDB.sqlite3")
                try:
                    shutil.copy2(path, copy_path)
                    for suffix in ("-wal", "-shm"):
                        src = f"{path}{suffix}"
                        if os.path.exists(src):
                            shutil.copy2(src, f"{copy_path}{suffix}")
                except OSError:
                    return []
                try:
                    return self._indexeddb_rows(copy_path)
                except Exception:
                    return []

    def _indexeddb_rows(self, path: str) -> List[Tuple[Any, Any, Any]]:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.text_factory = bytes
        try:
            return con.execute("select recordID, key, value from Records").fetchall()
        finally:
            con.close()

    def _computer_candidate_from_idb_record(
        self, path: str, updated_ms: int, key: Any, value: Any
    ) -> Dict[str, Any]:
        key_text = " ".join(item[1] for item in self._idb_positioned_strings(key)).lower()
        if not key_text or "/rest/tasks/shortcuts" in key_text:
            return {}
        interesting = (
            "thread_metadata" in key_text
            or "all_results" in key_text
            or "/rest/thread/list_recent" in key_text
            or "/computer" in key_text
            or "/rest/tasks" in key_text
        )
        if not interesting:
            return {}
        fields = self._idb_fields(value)
        title = (
            self._clean_task_title(fields.get("thread_title"))
            or self._clean_task_title(fields.get("title"))
            or self._clean_task_title(fields.get("task_name"))
            or self._clean_task_title(fields.get("task_description"))
        )
        if not title:
            return {}
        score = 0
        if "thread_metadata" in key_text:
            score += 90
        if "all_results" in key_text:
            score += 80
        if "/rest/thread/list_recent" in key_text:
            score += 70
        if "/computer" in key_text or "/rest/tasks" in key_text:
            score += 20
        if fields.get("thread_status"):
            score += 10
        if fields.get("thread_title"):
            score += 5
        return {
            "title": title,
            "thread_id": fields.get("backend_uuid") or fields.get("uuid") or "",
            "thread_status": fields.get("thread_status") or "",
            "updated_ms": updated_ms,
            "path": path,
            "title_basis": "Perplexity IndexedDB task cache",
            "score": score,
        }

    def _idb_fields(self, value: Any) -> Dict[str, str]:
        items = self._idb_positioned_strings(value)
        names = {
            "backend_uuid",
            "thread_status",
            "thread_status_summary",
            "thread_status_summary_enum",
            "thread_title",
            "thread_url_slug",
            "title",
            "task_name",
            "task_description",
            "todo_task_status",
            "uuid",
        }
        fields: Dict[str, str] = {}
        for idx, (_, text, _) in enumerate(items):
            if text not in names or text in fields:
                continue
            if text in {"backend_uuid", "uuid"}:
                for _, candidate, _ in items[idx + 1 : idx + 12]:
                    cleaned = self._clean_idb_value(candidate)
                    if re.fullmatch(
                        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                        cleaned,
                    ):
                        fields[text] = cleaned
                        break
                continue
            if text in {"title", "thread_title", "task_name", "task_description"}:
                best_title = ""
                for _, candidate, _ in items[idx + 1 : idx + 12]:
                    cleaned = self._clean_idb_value(candidate)
                    if not cleaned or cleaned in names:
                        continue
                    title = self._clean_task_title(cleaned)
                    if title and len(title) > len(best_title):
                        best_title = title
                if best_title:
                    fields[text] = best_title
                continue
            for _, candidate, _ in items[idx + 1 : idx + 10]:
                cleaned = self._clean_idb_value(candidate)
                if not cleaned or cleaned in names:
                    continue
                if text == "thread_status":
                    if self._map_computer_thread_status(cleaned):
                        fields[text] = cleaned
                        break
                    continue
                if text in {"thread_status_summary", "thread_status_summary_enum", "todo_task_status"}:
                    continue
                fields[text] = cleaned
                break
        return fields

    def _idb_positioned_strings(self, value: Any) -> List[Tuple[int, str, str]]:
        data = self._ensure_bytes(value)
        found: List[Tuple[int, str, str]] = []
        for match in re.finditer(rb"[\x20-\x7e]{3,}", data):
            text = match.group().decode("utf-8", "ignore")
            if text:
                found.append((match.start(), text, "ascii"))
        for start in (0, 1):
            idx = start
            while idx + 1 < len(data):
                chars: List[str] = []
                end = idx
                while end + 1 < len(data):
                    codepoint = data[end] | (data[end + 1] << 8)
                    if codepoint in (9, 10, 13) or (codepoint >= 32 and codepoint not in (0xFFFE, 0xFFFF)):
                        char = chr(codepoint)
                        if char.isprintable() and char != "\x00":
                            chars.append(char)
                            end += 2
                            continue
                    break
                if len(chars) >= 3:
                    text = "".join(chars)
                    if any(char.isalnum() for char in text):
                        found.append((idx, text, "utf16"))
                    idx = max(end, idx + 2)
                else:
                    idx += 2
        found.sort(key=lambda item: (item[0], 0 if item[2] == "ascii" else 1))
        out: List[Tuple[int, str, str]] = []
        seen = set()
        for pos, text, encoding in found:
            cleaned = self._clean_idb_value(text)
            if not cleaned:
                continue
            key = (pos, cleaned)
            if key in seen:
                continue
            seen.add(key)
            out.append((pos, cleaned, encoding))
        return out

    def _ensure_bytes(self, value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8", "ignore")
        try:
            return bytes(value)
        except Exception:
            return str(value).encode("utf-8", "ignore")

    def _clean_idb_value(self, value: Any) -> str:
        text = str(value or "").strip().strip("\x00")
        if not text or self._has_idb_noise_marker(text):
            return ""
        packed = self._decode_packed_ascii(text)
        if packed:
            text = packed
        if not text or self._looks_like_idb_noise(text):
            return ""
        return html.unescape(text)

    def _clean_task_title(self, value: Any) -> str:
        text = self._clean_idb_value(value)
        if not text or len(text) < 2 or len(text) > 120:
            return ""
        if text.lower() in {
            "answer_preview",
            "items",
            "link",
            "query_source",
            "related_queries",
            "updated_at",
        }:
            return ""
        if text.startswith(("http://", "https://", "/rest/", "pplx-query-cache-")):
            return ""
        if text in {
            "completed",
            "failed",
            "incomplete",
            "pending",
            "running",
            "success",
        }:
            return ""
        return text

    def _looks_like_idb_noise(self, text: str) -> bool:
        if "\ufffd" in text:
            return True
        if self._has_idb_noise_marker(text):
            return True
        return False

    def _has_idb_noise_marker(self, text: str) -> bool:
        markers = {"耀", "璀", "疀", "犀", "榀", "憀", "沀", "熀", "∀", "ⴀ", "㐀", "㌀", "㤀"}
        return any(marker in text for marker in markers)

    def _decode_packed_ascii(self, text: str) -> str:
        decoded: List[str] = []
        useful_bytes = 0
        skipped_bytes = 0
        for char in text:
            codepoint = ord(char)
            if codepoint <= 0xFF:
                return ""
            if codepoint > 0xFFFF:
                return ""
            for byte in (codepoint >> 8, codepoint & 0xFF):
                if 32 <= byte <= 126:
                    decoded.append(chr(byte))
                    useful_bytes += 1
                elif byte in (0, 0x80):
                    skipped_bytes += 1
                else:
                    return ""
        result = "".join(decoded).strip()
        if useful_bytes >= 3 and useful_bytes >= skipped_bytes and re.search(r"[A-Za-z0-9]", result):
            return result
        return ""

    def _usage(self, state: Dict[str, Any]) -> Dict[str, Any]:
        query_count = safe_int(state.get("local_query_count"))
        deep_count = safe_int(state.get("local_deep_research_query_count"))
        prompt_count = safe_int(state.get("local_query_count_for_prompt"))
        parts: List[str] = []
        if query_count:
            parts.append(f"{query_count} q")
        if deep_count:
            parts.append(f"{deep_count} deep")
        if prompt_count:
            parts.append(f"{prompt_count} prompt")
        if not parts:
            return {}
        return {
            "label": " · ".join(parts[:3]),
            "local_query_count": query_count,
            "local_deep_research_query_count": deep_count,
            "local_query_count_for_prompt": prompt_count,
            "source": "perplexity-local-preferences",
        }

    def _read_local_state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {}
        prefs = self._read_preferences()
        state.update(prefs)
        cache = self._read_cache_hint()
        state.update(cache)
        http_mtime = self._latest_mtime_ms(self.http_storages)
        if http_mtime:
            state["http_mtime_ms"] = http_mtime
        state["status_basis"] = "local preferences + WebKit cache mtimes" if state else "app process only"
        return state

    def _read_preferences(self) -> Dict[str, Any]:
        pref_path = next((path for path in self.pref_paths if os.path.exists(path)), self.pref_paths[0])
        if not os.path.exists(pref_path):
            return self._read_preferences_with_defaults()
        path = pref_path
        temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
        try:
            temp_dir = tempfile.TemporaryDirectory(prefix="sticks3-pplx-pref-")
            copy_path = os.path.join(temp_dir.name, "preferences.plist")
            shutil.copy2(pref_path, copy_path)
            path = copy_path
        except Exception:
            path = pref_path
        try:
            with open(path, "rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            if temp_dir:
                temp_dir.cleanup()
            return self._read_preferences_with_defaults()
        if temp_dir:
            temp_dir.cleanup()
        if not isinstance(data, dict):
            return self._read_preferences_with_defaults()
        return {
            "prefs_mtime_ms": self._path_mtime_ms(pref_path),
            "local_query_count": safe_int(data.get("ThreadViewModel.localQueryCount")),
            "local_deep_research_query_count": safe_int(data.get("localDeepResearchQueryCount")),
            "local_query_count_for_prompt": safe_int(data.get("localQueryCountForPrompt")),
        }

    def _read_preferences_with_defaults(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"prefs_mtime_ms": self._latest_mtime_ms(self.pref_paths)}
        fields = {
            "ThreadViewModel.localQueryCount": "local_query_count",
            "localDeepResearchQueryCount": "local_deep_research_query_count",
            "localQueryCountForPrompt": "local_query_count_for_prompt",
        }
        for domain in ("ai.perplexity.macv3", "ai.perplexity.mac"):
            for key, target in fields.items():
                if target in out:
                    continue
                code, stdout, _ = run(["/usr/bin/defaults", "read", domain, key], timeout=1)
                if code == 0 and stdout.strip():
                    out[target] = safe_int(stdout.strip())
        return out

    def _read_cache_hint(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        cache_mtime = self._latest_mtime_ms(self.cache_dbs)
        if cache_mtime:
            out["cache_mtime_ms"] = cache_mtime
        row = None
        for path in self.cache_dbs:
            row = self._safe_latest_cache_row(path)
            if row:
                break
        if not row:
            return out
        endpoint = self._sanitize_endpoint(str(row[0] or ""))
        if endpoint:
            out["latest_endpoint"] = endpoint
        latest_ms = self._parse_cache_time(row[1])
        if latest_ms:
            out["latest_cache_ms"] = latest_ms
        return out

    def _safe_latest_cache_row(self, path: str) -> Optional[Tuple[Any, Any]]:
        if not os.path.exists(path):
            return None
        try:
            return self._latest_cache_row(path)
        except Exception:
            return self._latest_cache_row_from_copy(path)

    def _latest_cache_row(self, path: str) -> Optional[Tuple[Any, Any]]:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute(
                """
                select request_key, time_stamp
                from cfurl_cache_response
                where (request_key like '%perplexity%' or request_key like '%pplx%')
                  and request_key not like '%favicon%'
                  and request_key not like '%gstatic.com%'
                  and request_key not like '%r2cdn.perplexity.ai/source%'
                  and request_key not like '%frontend-cdn.perplexity.ai/_agi_assets%'
                  and request_key not like '%cloudfront.net/thumbnails%'
                order by time_stamp desc
                limit 1
                """
            ).fetchone()
        finally:
            con.close()

    def _latest_cache_row_from_copy(self, path: str) -> Optional[Tuple[Any, Any]]:
        with tempfile.TemporaryDirectory(prefix="sticks3-pplx-cache-") as tmp:
            copy_path = os.path.join(tmp, "Cache.db")
            try:
                shutil.copy2(path, copy_path)
                for suffix in ("-wal", "-shm"):
                    src = f"{path}{suffix}"
                    if os.path.exists(src):
                        shutil.copy2(src, f"{copy_path}{suffix}")
            except OSError:
                return None
            try:
                return self._latest_cache_row(copy_path)
            except Exception:
                return None

    def _latest_mtime_ms(self, paths: Iterable[str]) -> int:
        return max((self._path_mtime_ms(path) for path in paths), default=0)

    def _sqlite_content_mtime_ms(self, path: str) -> int:
        mtimes = []
        for item in (path, f"{path}-wal"):
            try:
                mtimes.append(int(os.path.getmtime(item) * 1000))
            except OSError:
                continue
        return max(mtimes) if mtimes else 0

    def _latest_path_mtime(self, paths: Iterable[str]) -> Tuple[str, int]:
        latest_path = ""
        latest_ms = 0
        for path in paths:
            mtime = self._path_mtime_ms(path)
            if mtime > latest_ms:
                latest_path = path
                latest_ms = mtime
        return latest_path, latest_ms

    def _path_mtime_ms(self, path: str) -> int:
        mtimes = []
        if os.path.isdir(path):
            try:
                for name in os.listdir(path):
                    item = os.path.join(path, name)
                    if os.path.isfile(item):
                        mtimes.append(int(os.path.getmtime(item) * 1000))
            except OSError:
                pass
        else:
            for item in [path, f"{path}-wal"]:
                try:
                    mtimes.append(int(os.path.getmtime(item) * 1000))
                except OSError:
                    continue
        return max(mtimes) if mtimes else 0

    def _parse_cache_time(self, value: Any) -> int:
        if not value:
            return 0
        if isinstance(value, (int, float)):
            return safe_ms(value) or 0
        text = str(value).strip()
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0
        return int(time.mktime(dt.timetuple()) * 1000)

    def _sanitize_endpoint(self, url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.netloc:
            return ""
        path = re.sub(r"[0-9a-fA-F]{8,}", "<id>", parsed.path)
        path = re.sub(r"/[A-Za-z0-9_-]{12,}", "/<id>", path)
        return f"{parsed.netloc.lower()}{path}"[:80]


class AppAdapter:
    def __init__(self, source: str, app_name: str, bundle_id: str, bundle_fragment: str, binary_name: str) -> None:
        self.source = source
        self.app_name = app_name
        self.bundle_id = bundle_id
        self.bundle_fragment = bundle_fragment
        self.binary_name = binary_name

    def list_tasks(self, commands: Optional[List[str]] = None) -> List[Task]:
        commands = commands or ps_commands()
        running = app_running(commands, self.bundle_fragment, self.binary_name)
        titles = window_titles(self.app_name) if running else []
        title = titles[0] if titles else self.source
        return [
            task(
                task_id=f"{self.source.lower()}-app",
                source=self.source,
                title=title,
                status="idle",
                subtitle="app open" if running else "not running",
                open_action={"type": "app", "target": self.bundle_id},
            )
        ]


GEMINI_SAFARI_SCRIPT = r'''
set AppleScript's text item delimiters to " | "
set sep to ASCII character 9
set tabTitle to ""
set tabUrl to ""
set frontTitle to ""
set frontUrl to ""
tell application "Safari"
  if not running then return ""
  try
    set frontTitle to name of current tab of front window
    set frontUrl to URL of current tab of front window
  on error
    return ""
  end try
  set out to "FRONT" & sep & frontTitle & sep & frontUrl & linefeed
  try
    repeat with w in windows
      repeat with t in tabs of w
        set out to out & "TAB" & sep & (name of t as text) & sep & (URL of t as text) & linefeed
      end repeat
    end repeat
  end try
end tell
if frontUrl does not contain "gemini.google.com" and frontUrl does not contain "bard.google.com" then return out

tell application "System Events"
  try
    tell process "Safari"
      set webArea to UI element 1 of scroll area 1 of group 1 of group 1 of tab group 1 of splitter group 1 of window 1
      set mainGroup to group 2 of webArea
      set out to out & "MAIN" & sep & (name of every UI element of mainGroup as text) & linefeed
      try
        set out to out & "NAV" & sep & (name of every UI element of group 2 of mainGroup as text) & linefeed
      end try
      try
        set out to out & "PROMPT" & sep & (name of every UI element of group 3 of mainGroup as text) & linefeed
      end try
      try
        set out to out & "PROMPTV" & sep & (value of every UI element of group 3 of mainGroup as text) & linefeed
      end try
    end tell
  end try
end tell
return out
'''

GEMINI_CHROMIUM_BROWSERS = [
    ("Google Chrome", "Chrome"),
    ("Arc", "Arc"),
    ("Microsoft Edge", "Edge"),
    ("Brave Browser", "Brave"),
    ("Chromium", "Chromium"),
]


class GeminiAdapter:
    source = "Gemini"

    def __init__(self) -> None:
        self.app_name = "Gemini"
        self.bundle_id = "com.google.GeminiMacOS"
        self.bundle_fragment = "/Applications/Gemini.app"
        self.binary_name = "Gemini"
        self.launcher_fragment = "GeminiAppLauncher.app/Contents/MacOS/GeminiAppLauncher"
        self.activity_paths = [
            os.path.expanduser("~/Library/Application Support/com.google.GeminiMacOS/Data/minichat-settings.store"),
            os.path.expanduser("~/Library/Application Support/com.google.GeminiMacOS/Data/minichat-settings.store-wal"),
        ]
        self.activity_globs = [
            os.path.expanduser("~/Library/Caches/com.google.GeminiMacOS/Logs/diagnostic*.log"),
            os.path.expanduser("~/Library/Caches/com.google.GeminiMacOS/Logs/launcher*.log"),
            os.path.expanduser("~/Library/Caches/com.google.GeminiMacOS/PerformanceLogs/*.json"),
        ]
        self._browser_cache: Dict[str, Any] = {}
        self._browser_cache_at = 0

    def list_tasks(self, commands: Optional[List[str]] = None) -> List[Task]:
        commands = commands or ps_commands()
        running = self._running(commands)
        signal_ms, signal_path = self._latest_activity()
        browser = self._browser_state()
        title = self.source
        status = self._status(signal_ms)
        subtitle = self._subtitle(running, signal_ms)
        updated_ms = signal_ms or now_ms()
        open_action = {"type": "app", "target": self.bundle_id}
        status_basis = "Gemini app process + local settings/log/cache mtimes"

        if browser.get("present"):
            title = str(browser.get("title") or title or self.source)
            status = str(browser.get("status") or status)
            updated_ms = safe_int(browser.get("updated_ms")) or updated_ms
            subtitle = self._browser_subtitle(browser, running)
            status_basis = str(browser.get("status_basis") or status_basis)
            if browser.get("url"):
                open_action = {"type": "url", "target": str(browser.get("url"))}

        return [
            task(
                task_id="gemini-app",
                source=self.source,
                title=title,
                status=status,
                updated_ms=updated_ms,
                subtitle=subtitle,
                detail={
                    "running": running,
                    "signal_ms": signal_ms,
                    "signal_path": signal_path,
                    "browser": browser.get("browser"),
                    "browser_url": browser.get("url"),
                    "browser_frontmost": browser.get("frontmost"),
                    "browser_title_basis": browser.get("title_basis"),
                    "status_basis": status_basis,
                },
                open_action=open_action,
            )
        ]

    def _running(self, commands: Iterable[str]) -> bool:
        for line in commands:
            if not ci_contains(self.bundle_fragment, line):
                continue
            if ci_contains(f"/MacOS/{self.binary_name}", line) or ci_contains(self.launcher_fragment, line):
                return True
        return False

    def _status(self, signal_ms: int) -> str:
        if signal_ms and now_ms() - signal_ms < GEMINI_ACTIVITY_STALE_MS:
            return "recent"
        return "idle"

    def _subtitle(self, running: bool, signal_ms: int) -> str:
        parts = ["app open" if running else "not running"]
        if signal_ms:
            parts.append("local activity")
        return join_parts(parts, " · ")

    def _browser_subtitle(self, browser: Dict[str, Any], app_running_flag: bool) -> str:
        parts = [str(browser.get("browser") or "Safari"), "web" if browser.get("frontmost") else "tab"]
        if browser.get("running_signal"):
            parts.append("generating")
        elif browser.get("title_basis"):
            parts.append("visible task")
        elif app_running_flag:
            parts.append("app open")
        return join_parts(parts, " · ")

    def _browser_state(self) -> Dict[str, Any]:
        now = now_ms()
        if self._browser_cache and now - self._browser_cache_at < GEMINI_BROWSER_POLL_MS:
            return self._browser_cache

        states: List[Dict[str, Any]] = []
        code, out, _ = run_osascript(GEMINI_SAFARI_SCRIPT, timeout=2.0)
        if code == 0 and out.strip():
            state = self._parse_safari_state(out)
            if state:
                states.append(state)

        states.extend(self._chromium_states())
        state = self._best_browser_state(states)
        self._browser_cache = state
        self._browser_cache_at = now
        return state

    def _chromium_states(self) -> List[Dict[str, Any]]:
        states: List[Dict[str, Any]] = []
        for app_name, browser_name in GEMINI_CHROMIUM_BROWSERS:
            if not process_name_running(app_name):
                continue
            script = self._chromium_script(app_name)
            code, out, _ = run_osascript(script, timeout=1.0)
            if code != 0 or not out.strip():
                continue
            state = self._parse_chromium_state(out, browser_name)
            if state:
                states.append(state)
        return states

    def _chromium_script(self, app_name: str) -> str:
        return f'''
set sep to ASCII character 9
tell application "System Events"
  if not (exists process "{app_name}") then return ""
end tell
tell application "{app_name}"
  set out to ""
  try
    set out to out & "FRONT" & sep & (title of active tab of front window as text) & sep & (URL of active tab of front window as text) & linefeed
  end try
  try
    repeat with w in windows
      repeat with t in tabs of w
        set out to out & "TAB" & sep & (title of t as text) & sep & (URL of t as text) & linefeed
      end repeat
    end repeat
  end try
  return out
end tell
'''

    def _best_browser_state(self, states: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not states:
            return {}
        for state in states:
            if state.get("title_basis"):
                return state
        for state in states:
            if state.get("frontmost"):
                return state
        return states[0]

    def _parse_safari_state(self, out: str) -> Dict[str, Any]:
        front_is_gemini = False
        tab_title = ""
        tab_url = ""
        labels: List[str] = []
        for raw_line in out.splitlines():
            parts = raw_line.split("\t", 2)
            if not parts:
                continue
            key = parts[0]
            if key == "FRONT" and len(parts) >= 3:
                front_title = parts[1].strip()
                front_url = parts[2].strip()
                front_is_gemini = self._is_gemini_url(front_url)
                if front_is_gemini:
                    tab_title = front_title
                    tab_url = front_url
            elif key == "TAB" and len(parts) >= 3:
                candidate_title = parts[1].strip()
                candidate_url = parts[2].strip()
                if not tab_url and self._is_gemini_url(candidate_url):
                    tab_title = candidate_title
                    tab_url = candidate_url
            elif key in {"MAIN", "NAV", "PROMPT", "PROMPTV"} and len(parts) >= 2:
                labels.extend(self._split_apple_list(parts[1]))

        if not tab_url or not self._is_gemini_url(tab_url):
            return {}

        title, title_basis = self._browser_title(tab_title, labels)
        running_signal = self._browser_running_signal(labels)
        return {
            "present": True,
            "browser": "Safari",
            "url": tab_url,
            "title": title or "Gemini",
            "title_basis": title_basis,
            "status": "running" if running_signal else "recent",
            "updated_ms": now_ms(),
            "running_signal": running_signal,
            "frontmost": front_is_gemini,
            "status_basis": (
                "Safari Gemini Web accessibility headings" if front_is_gemini else "Safari Gemini Web tab"
            ),
        }

    def _parse_chromium_state(self, out: str, browser_name: str) -> Dict[str, Any]:
        front_is_gemini = False
        tab_title = ""
        tab_url = ""
        for raw_line in out.splitlines():
            parts = raw_line.split("\t", 2)
            if len(parts) < 3:
                continue
            key, candidate_title, candidate_url = parts[0], parts[1].strip(), parts[2].strip()
            if key == "FRONT":
                front_is_gemini = self._is_gemini_url(candidate_url)
                if front_is_gemini:
                    tab_title = candidate_title
                    tab_url = candidate_url
            elif key == "TAB" and not tab_url and self._is_gemini_url(candidate_url):
                tab_title = candidate_title
                tab_url = candidate_url

        if not tab_url:
            return {}

        title, title_basis = self._browser_title(tab_title, [])
        return {
            "present": True,
            "browser": browser_name,
            "url": tab_url,
            "title": title or "Gemini",
            "title_basis": title_basis,
            "status": "recent",
            "updated_ms": now_ms(),
            "running_signal": False,
            "frontmost": front_is_gemini,
            "status_basis": f"{browser_name} Gemini Web tab",
        }

    def _split_apple_list(self, value: str) -> List[str]:
        return [
            item.strip()
            for item in value.split(" | ")
            if item.strip() and item.strip().lower() != "missing value"
        ]

    def _is_gemini_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        return host.endswith("gemini.google.com") or host.endswith("bard.google.com")

    def _browser_title(self, tab_title: str, labels: List[str]) -> Tuple[str, str]:
        for label in labels:
            if self._is_useful_browser_title(label):
                return label, "safari-accessibility"
        if self._is_useful_browser_title(tab_title):
            return tab_title, "safari-tab-title"
        return "Gemini", ""

    def _is_useful_browser_title(self, value: str) -> bool:
        text = re.sub(r"\s+", " ", value or "").strip()
        if not text:
            return False
        generic = {
            "gemini",
            "google gemini",
            "conversation with gemini",
            "hi chester, what's the move?",
            "ready when you are",
            "welcome to gemini",
            "new chat",
            "side navigation",
            "main actions menu",
            "additional actions menu",
            "close sidebar",
            "toggle recents",
            "temporary chat",
            "enter a prompt for gemini",
        }
        return text.lower() not in generic and len(text) <= 96

    def _browser_running_signal(self, labels: List[str]) -> bool:
        joined = " ".join(labels).lower()
        markers = [
            "stop generating",
            "stop response",
            "stop responding",
            "cancel response",
            "cancel generation",
        ]
        return any(marker in joined for marker in markers)

    def _latest_activity(self) -> Tuple[int, str]:
        candidates = list(self.activity_paths)
        for pattern in self.activity_globs:
            candidates.extend(glob.glob(pattern))

        best_ms = 0
        best_path = ""
        for path in candidates:
            try:
                if not os.path.isfile(path):
                    continue
                mtime_ms = int(os.path.getmtime(path) * 1000)
            except OSError:
                continue
            if mtime_ms > best_ms:
                best_ms = mtime_ms
                best_path = path
        return best_ms, best_path


class LovableAdapter:
    source = "Lovable"

    def __init__(self) -> None:
        self.app_name = "Lovable"
        self.bundle_id = "dev.lovable.build"
        self.bundle_fragment = "/Applications/Lovable.app"
        self.binary_name = "Lovable"
        self.app_support_dir = os.path.expanduser("~/Library/Application Support/lovable-desktop")
        self.app_activity_globs = [
            os.path.join(self.app_support_dir, "Local Storage/leveldb/*"),
            os.path.join(self.app_support_dir, "IndexedDB/https_lovable.dev_0.indexeddb.leveldb/*"),
            os.path.join(self.app_support_dir, "Session Storage/*"),
            os.path.join(self.app_support_dir, "Cookies"),
            os.path.join(self.app_support_dir, "Network Persistent State"),
            os.path.join(self.app_support_dir, "sentry/session.json"),
        ]
        self._browser_cache: List[Dict[str, Any]] = []
        self._browser_cache_at = 0
        self._last_app_signature = ""
        self._last_app_change_ms = 0

    def list_tasks(self, commands: Optional[List[str]] = None) -> List[Task]:
        commands = commands or ps_commands()
        states = self._browser_states()
        tasks: List[Task] = []
        app_state = self._app_state(commands)
        if app_state.get("available"):
            tasks.append(self._app_task(app_state))
        for index, state in enumerate(states[:LOVABLE_MAX_TABS]):
            url = str(state.get("url") or "https://lovable.dev/")
            title = str(state.get("title") or "Lovable")
            running_signal = bool(state.get("running_signal"))
            status = "running" if running_signal else "recent"
            subtitle = self._subtitle(state)
            tasks.append(
                task(
                    task_id=stable_id("lovable", url),
                    source=self.source,
                    title=title,
                    status=status,
                    updated_ms=safe_int(state.get("updated_ms")) or now_ms(),
                    subtitle=subtitle,
                    detail={
                        "browser": state.get("browser"),
                        "browser_url": url,
                        "browser_frontmost": state.get("frontmost"),
                        "title_basis": state.get("title_basis"),
                        "status_basis": state.get("status_basis"),
                        "running_signal": running_signal,
                        "tab_index": index + 1,
                    },
                    open_action={"type": "url", "target": url},
                )
            )
        return tasks

    def debug_snapshot(self, commands: Optional[List[str]] = None) -> Dict[str, Any]:
        commands = commands or ps_commands()
        app = self._app_state(commands)
        browsers = []
        for state in self._browser_states():
            browsers.append(
                {
                    "browser": state.get("browser"),
                    "url": state.get("url"),
                    "title": state.get("title"),
                    "title_basis": state.get("title_basis"),
                    "frontmost": state.get("frontmost"),
                    "running_signal": state.get("running_signal"),
                    "status_basis": state.get("status_basis"),
                }
            )
        return {
            "source": self.source,
            "bundle_id": self.bundle_id,
            "app_support_dir": self.app_support_dir,
            "renderer_cpu_threshold": LOVABLE_RENDERER_RUN_CPU,
            "browser_poll_ms": LOVABLE_BROWSER_POLL_MS,
            "max_tabs": LOVABLE_MAX_TABS,
            "app": app,
            "browsers": browsers,
        }

    def _app_state(self, commands: List[str]) -> Dict[str, Any]:
        running = app_running(commands, self.bundle_fragment, self.binary_name)
        window_names = window_titles(self.app_name) if running else []
        labels = self._accessibility_labels(self.app_name) if running else []
        signal_path, signal_ms = self._latest_app_activity()
        renderer_cpu = self._renderer_cpu_percent() if running else 0.0
        signature = json.dumps(
            {
                "running": running,
                "signal_ms": signal_ms,
                "signal_path": signal_path,
                "window": window_names[:2],
            },
            sort_keys=True,
        )
        current_ms = now_ms()
        if self._last_app_signature and signature != self._last_app_signature:
            self._last_app_change_ms = current_ms
        self._last_app_signature = signature
        running_signal = self._running_signal(labels, " ".join(window_names))
        cpu_running_signal = renderer_cpu >= LOVABLE_RENDERER_RUN_CPU
        title, title_basis = self._browser_title(window_names[0] if window_names else "", labels, "")
        if title == "Lovable" and title_basis == "":
            title_basis = "app-window" if window_names else ""
        return {
            "available": running or bool(signal_ms),
            "running": running,
            "running_signal": running_signal,
            "cpu_running_signal": cpu_running_signal,
            "renderer_cpu_percent": renderer_cpu,
            "title": title,
            "title_basis": title_basis,
            "window_names": window_names,
            "signal_ms": signal_ms,
            "signal_path": signal_path,
            "last_change_ms": self._last_app_change_ms,
            "status": self._app_status(running, running_signal, cpu_running_signal, signal_ms),
        }

    def _app_task(self, state: Dict[str, Any]) -> Task:
        running = bool(state.get("running"))
        signal_ms = safe_int(state.get("signal_ms"))
        status = str(state.get("status") or "idle")
        subtitle_parts = ["app open" if running else "app idle"]
        if state.get("running_signal") or state.get("cpu_running_signal"):
            subtitle_parts.append("generating")
        elif signal_ms:
            subtitle_parts.append("local activity")
        return task(
            task_id="lovable-app",
            source=self.source,
            title=str(state.get("title") or "Lovable"),
            status=status,
            updated_ms=signal_ms or None,
            subtitle=join_parts(subtitle_parts, " · "),
            detail={
                "app_running": running,
                "bundle_id": self.bundle_id,
                "last_local_signal_at": ms_to_iso(signal_ms),
                "last_change_at": ms_to_iso(safe_int(state.get("last_change_ms"))),
                "signal_path": state.get("signal_path"),
                "renderer_cpu_percent": round(safe_float(state.get("renderer_cpu_percent")), 1),
                "renderer_cpu_threshold": LOVABLE_RENDERER_RUN_CPU,
                "running_signal": bool(state.get("running_signal")),
                "cpu_running_signal": bool(state.get("cpu_running_signal")),
                "title_basis": state.get("title_basis"),
                "window_names": state.get("window_names"),
                "status_basis": (
                    "Lovable.app accessibility generation label"
                    if state.get("running_signal")
                    else "Lovable.app renderer CPU"
                    if state.get("cpu_running_signal")
                    else "Lovable.app process + local storage/cache mtimes"
                ),
            },
            open_action={"type": "app", "target": self.bundle_id},
        )

    def _app_status(self, running: bool, running_signal: bool, cpu_running_signal: bool, signal_ms: int) -> str:
        current_ms = now_ms()
        if running_signal or cpu_running_signal:
            return "running"
        if running:
            return "recent"
        if signal_ms and current_ms - signal_ms < LOVABLE_ACTIVITY_STALE_MS:
            return "recent"
        return "idle"

    def _renderer_cpu_percent(self) -> float:
        code, out, _ = run(["ps", "-axo", "pcpu=,command="], timeout=1.0)
        if code != 0:
            return 0.0
        max_cpu = 0.0
        for line in out.splitlines():
            if "Lovable Helper (Renderer).app/Contents/MacOS/Lovable Helper (Renderer)" not in line:
                continue
            if "lovable-desktop" not in line:
                continue
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            try:
                cpu = float(parts[0])
            except ValueError:
                continue
            max_cpu = max(max_cpu, cpu)
        return max_cpu

    def _latest_app_activity(self) -> Tuple[str, int]:
        best_path = ""
        best_ms = 0
        for pattern in self.app_activity_globs:
            for path in glob.glob(pattern):
                try:
                    if not os.path.isfile(path):
                        continue
                    mtime_ms = int(os.path.getmtime(path) * 1000)
                except OSError:
                    continue
                if mtime_ms > best_ms:
                    best_ms = mtime_ms
                    best_path = path
        return best_path, best_ms

    def _subtitle(self, state: Dict[str, Any]) -> str:
        parts = [str(state.get("browser") or "Browser")]
        parts.append("web" if state.get("frontmost") else "tab")
        if state.get("running_signal"):
            parts.append("generating")
        elif state.get("title_basis"):
            parts.append("project")
        return join_parts(parts, " · ")

    def _browser_states(self) -> List[Dict[str, Any]]:
        now = now_ms()
        if self._browser_cache_at and now - self._browser_cache_at < LOVABLE_BROWSER_POLL_MS:
            return self._browser_cache

        code, out, _ = run_osascript(self._browser_script(), timeout=2.5)
        if code != 0 or not out.strip():
            self._browser_cache = []
            self._browser_cache_at = now
            return []

        states_by_url: Dict[str, Dict[str, Any]] = {}
        front_process = ""
        for raw_line in out.splitlines():
            parts = raw_line.split("\t", 4)
            if len(parts) < 5:
                continue
            kind, process_name, browser_name, tab_title, url = parts
            url = url.strip()
            if not self._is_lovable_url(url):
                continue
            frontmost = kind == "FRONT"
            if frontmost:
                front_process = process_name
            state = {
                "present": True,
                "browser": browser_name.strip() or process_name.strip() or "Browser",
                "process": process_name.strip(),
                "url": url,
                "tab_title": tab_title.strip(),
                "frontmost": frontmost,
                "updated_ms": now,
                "running_signal": False,
                "status_basis": f"{browser_name.strip() or process_name.strip()} Lovable tab",
            }
            existing = states_by_url.get(url)
            if not existing or frontmost:
                states_by_url[url] = state

        states = list(states_by_url.values())
        front_state = next((state for state in states if state.get("frontmost")), None)
        labels: List[str] = []
        if front_state and front_process:
            labels = self._accessibility_labels(front_process)

        for state in states:
            state_labels = labels if state.get("frontmost") else []
            title, basis = self._browser_title(str(state.get("tab_title") or ""), state_labels, str(state.get("url") or ""))
            state["title"] = title
            state["title_basis"] = basis
            if state.get("frontmost"):
                state["running_signal"] = self._running_signal(state_labels, str(state.get("tab_title") or ""))
                if state_labels:
                    state["status_basis"] = f"{state.get('browser')} Lovable accessibility labels"

        states.sort(
            key=lambda item: (
                0 if item.get("frontmost") else 1,
                0 if item.get("title_basis") else 1,
                str(item.get("title") or ""),
            )
        )
        self._browser_cache = states
        self._browser_cache_at = now
        return states

    def _browser_script(self) -> str:
        script = [
            'set sep to ASCII character 9',
            'set out to ""',
            'tell application "System Events" to set runningNames to name of every process',
            'if runningNames contains "Safari" then',
            '  tell application "Safari"',
            '    try',
            '      set out to out & "FRONT" & sep & "Safari" & sep & "Safari" & sep & (name of current tab of front window as text) & sep & (URL of current tab of front window as text) & linefeed',
            '    end try',
            '    try',
            '      repeat with w in windows',
            '        repeat with t in tabs of w',
            '          set out to out & "TAB" & sep & "Safari" & sep & "Safari" & sep & (name of t as text) & sep & (URL of t as text) & linefeed',
            '        end repeat',
            '      end repeat',
            '    end try',
            '  end tell',
            'end if',
        ]
        for app_name, browser_name in GEMINI_CHROMIUM_BROWSERS:
            script.extend(
                [
                    f'if runningNames contains "{app_name}" then',
                    f'  tell application "{app_name}"',
                    '    try',
                    f'      set out to out & "FRONT" & sep & "{app_name}" & sep & "{browser_name}" & sep & (title of active tab of front window as text) & sep & (URL of active tab of front window as text) & linefeed',
                    '    end try',
                    '    try',
                    '      repeat with w in windows',
                    '        repeat with t in tabs of w',
                    f'          set out to out & "TAB" & sep & "{app_name}" & sep & "{browser_name}" & sep & (title of t as text) & sep & (URL of t as text) & linefeed',
                    '        end repeat',
                    '      end repeat',
                    '    end try',
                    '  end tell',
                    'end if',
                ]
            )
        script.append("return out")
        return "\n".join(script)

    def _accessibility_labels(self, process_name: str) -> List[str]:
        if not process_name:
            return []
        safe_process = process_name.replace('"', '\\"')
        script = f'''
set AppleScript's text item delimiters to " | "
tell application "System Events"
  if not (exists process "{safe_process}") then return ""
  tell process "{safe_process}"
    set labels to {{}}
    try
      set uiItems to entire contents of front window
      repeat with el in uiItems
        try
          set n to name of el
          if n is not missing value and n is not "" then set end of labels to (n as text)
        end try
        if (count of labels) > 160 then exit repeat
      end repeat
    end try
    return labels as text
  end tell
end tell
'''
        code, out, _ = run_osascript(script, timeout=1.5)
        if code != 0 or not out.strip():
            return []
        return [
            item.strip()
            for item in out.split(" | ")
            if item.strip() and item.strip().lower() != "missing value"
        ]

    def _is_lovable_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower().split(":", 1)[0]
        if not host:
            return False
        return any(host == domain or host.endswith(f".{domain}") for domain in LOVABLE_DOMAINS)

    def _browser_title(self, tab_title: str, labels: List[str], url: str) -> Tuple[str, str]:
        cleaned_tab = self._clean_title(tab_title)
        if self._is_useful_title(cleaned_tab):
            return cleaned_tab, "tab-title"
        for label in labels:
            cleaned = self._clean_title(label)
            if self._is_useful_title(cleaned):
                return cleaned, "accessibility"
        path_label = self._url_path_label(url)
        if path_label:
            return path_label, "url-path"
        return "Lovable", ""

    def _clean_title(self, value: str) -> str:
        text = re.sub(r"\s+", " ", value or "").strip()
        text = re.sub(r"\s+[-–—|]\s+Lovable.*$", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^Lovable\s+[-–—|]\s+", "", text, flags=re.IGNORECASE)
        return text.strip()

    def _is_useful_title(self, value: str) -> bool:
        text = re.sub(r"\s+", " ", value or "").strip()
        if not text or len(text) > 96:
            return False
        low = text.lower()
        generic = {
            "lovable",
            "lovable.dev",
            "new project",
            "projects",
            "dashboard",
            "sign in",
            "log in",
            "chat",
            "preview",
            "build",
            "share",
            "deploy",
            "settings",
            "templates",
        }
        if low in generic:
            return False
        if low.startswith(("http://", "https://")):
            return False
        if "ai app builder" in low or "build apps" in low:
            return False
        return True

    def _url_path_label(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return ""
        if len(parts) >= 2 and parts[0] in {"projects", "project"}:
            return f"Lovable · {parts[1][:8]}"
        if parts[0] not in {"login", "signin", "auth"}:
            return f"Lovable · {parts[0][:18]}"
        return ""

    def _running_signal(self, labels: List[str], tab_title: str) -> bool:
        joined = " ".join([tab_title, *labels]).lower()
        markers = [
            "stop generating",
            "stop generation",
            "cancel generation",
            "cancel request",
            "generating",
            "applying changes",
            "making changes",
            "working on",
            "building your app",
            "thinking",
            "deploying",
        ]
        return any(marker in joined for marker in markers)


def first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def safe_ms(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n < 10_000_000_000:
        n *= 1000
    return int(n)


def public_task(t: Task) -> Task:
    clean = dict(t)
    clean.pop("_open", None)
    return clean


def compact_task(t: Task) -> Dict[str, Any]:
    status = normalize_status(str(t.get("status")))
    usage = t.get("usage") if isinstance(t.get("usage"), dict) else {}
    code = {
        "running": "run",
        "waiting": "wait",
        "failed": "fail",
        "done": "done",
        "recent": "rec",
        "idle": "idle",
        "unknown": "unk",
    }.get(status, "unk")
    return {
        "id": t.get("id", "")[:48],
        "s": t.get("source", "")[:18],
        "t": t.get("title", "")[:96],
        "st": code,
        "a": 1 if t.get("needs_attention") else 0,
        "u": int(t.get("age_sec") or 0),
        "sub": t.get("subtitle", "")[:96],
        "us": compact_usage_label(usage)[:32],
        "d": t.get("device_label", "")[:10],
    }


def html_page(title: str, body: str) -> bytes:
    safe_title = html.escape(title)
    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
    }}
    body {{
      margin: 0;
      background: Canvas;
      color: CanvasText;
    }}
    main {{
      max-width: 880px;
      margin: 0 auto;
      padding: 28px 20px 56px;
    }}
    .top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      border-bottom: 1px solid color-mix(in srgb, CanvasText 14%, transparent);
      padding-bottom: 16px;
      margin-bottom: 22px;
    }}
    h1 {{
      font-size: 26px;
      line-height: 1.2;
      margin: 0;
      letter-spacing: 0;
    }}
    h2 {{
      font-size: 15px;
      margin: 28px 0 10px;
      color: color-mix(in srgb, CanvasText 70%, transparent);
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .pill {{
      border: 1px solid color-mix(in srgb, CanvasText 18%, transparent);
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 13px;
      color: color-mix(in srgb, CanvasText 78%, transparent);
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 20px;
    }}
    a.button {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 7px;
      background: #0a84ff;
      color: white;
      text-decoration: none;
      font-size: 14px;
      font-weight: 600;
    }}
    a.secondary {{
      background: color-mix(in srgb, CanvasText 10%, transparent);
      color: CanvasText;
    }}
    dl {{
      display: grid;
      grid-template-columns: minmax(120px, 180px) 1fr;
      gap: 10px 14px;
      margin: 0;
    }}
    dt {{
      color: color-mix(in srgb, CanvasText 58%, transparent);
    }}
    dd {{
      margin: 0;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    pre {{
      overflow: auto;
      padding: 14px;
      border-radius: 8px;
      background: color-mix(in srgb, CanvasText 8%, transparent);
      font-size: 12px;
      line-height: 1.45;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 14px;
      font-size: 13px;
    }}
    th, td {{
      padding: 8px 6px;
      border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: color-mix(in srgb, CanvasText 62%, transparent);
      font-weight: 600;
    }}
    @media (max-width: 640px) {{
      .top {{ align-items: flex-start; flex-direction: column; }}
      dl {{ grid-template-columns: 1fr; gap: 4px 0; }}
      table {{ font-size: 12px; }}
    }}
  </style>
</head>
<body>
<main>{body}</main>
</body>
</html>"""
    return doc.encode("utf-8")


def kv_rows(values: Dict[str, Any]) -> str:
    rows = []
    for key, value in values.items():
        if value in (None, "", {}, []):
            continue
        rows.append(f"<dt>{html.escape(str(key))}</dt><dd>{html.escape(str(value))}</dd>")
    return "<dl>" + "".join(rows) + "</dl>"


def render_task_detail(t: Task) -> bytes:
    detail = t.get("detail") if isinstance(t.get("detail"), dict) else {}
    usage = t.get("usage") if isinstance(t.get("usage"), dict) else {}
    task_id = str(t.get("id") or "")
    encoded_id = urllib.parse.quote(task_id, safe="")
    title = str(t.get("title") or "Task")
    body = f"""
<section class="top">
  <div>
    <h1>{html.escape(title)}</h1>
    <div class="meta">
      <span class="pill">{html.escape(str(t.get("source") or "Unknown"))}</span>
      <span class="pill">{html.escape(str(t.get("status") or "unknown"))}</span>
      <span class="pill">{html.escape(str(t.get("updated_at") or ""))}</span>
    </div>
  </div>
  <a class="button secondary" href="/tasks">All tasks</a>
</section>
<div class="actions">
  <a class="button" href="/tasks/{encoded_id}/open-native">Open original app</a>
  <a class="button secondary" href="/tasks/{encoded_id}.json">JSON</a>
</div>
<h2>Task</h2>
{kv_rows({
    "ID": task_id,
    "Title": title,
    "Source": t.get("source"),
    "Status": t.get("status"),
    "Subtitle": t.get("subtitle"),
    "Age": f"{int(t.get('age_sec') or 0)} sec",
    "Needs attention": "yes" if t.get("needs_attention") else "no",
})}
<h2>Usage</h2>
{kv_rows({
    "Summary": usage.get("label") or compact_usage_label(usage),
    "Total tokens": usage.get("total_tokens"),
    "Turns": usage.get("turns"),
    "Rate limit": usage.get("rate_percent"),
    "Model": usage.get("model"),
})}
<h2>Local context</h2>
{kv_rows(detail)}
<h2>Raw</h2>
<pre>{html.escape(json.dumps(public_task(t), ensure_ascii=False, indent=2, default=str))}</pre>
"""
    return html_page(title, body)


def render_task_list(tasks: List[Task]) -> bytes:
    items = []
    for t in tasks:
        task_id = urllib.parse.quote(str(t.get("id") or ""), safe="")
        usage = t.get("usage") if isinstance(t.get("usage"), dict) else {}
        meta = " / ".join(
            part
            for part in [
                str(t.get("source") or ""),
                str(t.get("status") or ""),
                compact_usage_label(usage),
            ]
            if part
        )
        items.append(
            f'<p><a href="/tasks/{task_id}">{html.escape(str(t.get("title") or "Task"))}</a>'
            f'<br><span class="pill">{html.escape(meta)}</span></p>'
        )
    body = f"""
<section class="top">
  <div>
    <h1>AI Tasks</h1>
    <div class="meta"><span class="pill">{len(tasks)} tasks</span></div>
  </div>
</section>
{''.join(items)}
"""
    return html_page("AI Tasks", body)


def render_peers_page(snapshot: Dict[str, Any]) -> bytes:
    peers = snapshot.get("peers") if isinstance(snapshot.get("peers"), list) else []
    rows = []
    for peer in peers:
        if not isinstance(peer, dict):
            continue
        status = str(peer.get("status") or "")
        status_color = {
            "ok": "#16a34a",
            "error": "#dc2626",
            "discovered": "#d97706",
        }.get(status, "currentColor")
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(str(peer.get('label') or peer.get('name') or peer.get('id') or 'Peer'))}</strong>"
            f"<br><span class=\"pill\">{html.escape(str(peer.get('id') or ''))}</span></td>"
            f"<td><span style=\"color:{status_color}\">{html.escape(status)}</span>"
            f"<br>{html.escape(str(peer.get('last_error') or ''))}</td>"
            f"<td>{html.escape(str(peer.get('host') or ''))}:{html.escape(str(peer.get('port') or ''))}"
            f"<br>{html.escape(str(peer.get('url') or ''))}</td>"
            f"<td>{safe_int(peer.get('task_count'))} tasks"
            f"<br>{safe_int(peer.get('active'))} active / {safe_int(peer.get('attention'))} alert</td>"
            f"<td>{safe_int(peer.get('last_fetch_ms'))} ms"
            f"<br>{html.escape(str(peer.get('last_success_at') or peer.get('last_fetch_at') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5">No peers discovered yet.</td></tr>')

    body = f"""
<section class="top">
  <div>
    <h1>TaskHub Peers</h1>
    <div class="meta">
      <span class="pill">{html.escape(str(snapshot.get("device_label") or snapshot.get("device_name") or ""))}</span>
      <span class="pill">{'enabled' if snapshot.get('enabled') else 'disabled'}</span>
      <span class="pill">{len(peers)} peer(s)</span>
    </div>
  </div>
  <div class="actions">
    <a class="button secondary" href="/tasks">Tasks</a>
    <a class="button secondary" href="/peers.json">JSON</a>
  </div>
</section>
<h2>Discovery</h2>
{kv_rows({
    "Version": snapshot.get("version"),
    "Device": f"{snapshot.get('device_name')} ({snapshot.get('device_id')})",
    "Discovery port": snapshot.get("discovery_port"),
    "Last discovery": snapshot.get("discovery_at"),
    "Discovery duration": f"{safe_int(snapshot.get('discovery_duration_ms'))} ms",
    "Discovery error": snapshot.get("discovery_error"),
    "Remote task cache age": f"{safe_int(snapshot.get('cache_age_ms'))} ms" if snapshot.get("cache_age_ms") is not None else "",
})}
<h2>Peers</h2>
<table>
  <thead><tr><th>Device</th><th>Status</th><th>Endpoint</th><th>Tasks</th><th>Last Fetch</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""
    return html_page("TaskHub Peers", body)


class PeerManager:
    def __init__(self, token: str, http_port: int, discovery_port: int) -> None:
        self.token = token
        self.http_port = http_port
        self.discovery_port = discovery_port
        self.peers: Dict[str, Dict[str, Any]] = {}
        self.tasks_cache: List[Task] = []
        self.tasks_cache_at = 0
        self.discovery_at = 0
        self.discovery_error = ""
        self.discovery_count = 0
        self.discovery_duration_ms = 0

    def remote_tasks(self) -> List[Task]:
        if not PEER_ENABLED:
            return []
        current_ms = now_ms()
        if self.tasks_cache and current_ms - self.tasks_cache_at < PEER_CACHE_MS:
            return self.tasks_cache
        if current_ms - self.discovery_at > PEER_DISCOVERY_MS:
            self.discover()
        tasks: List[Task] = []
        for peer in list(self.peers.values())[:PEER_MAX]:
            started = now_ms()
            fetched, error = self._fetch_peer_tasks_result(peer)
            peer["last_fetch_at"] = now_ms()
            peer["last_fetch_ms"] = max(0, now_ms() - started)
            if not error:
                peer["last_seen_ms"] = current_ms
                peer["last_success_at"] = now_ms()
                peer["last_error"] = ""
                peer["task_count"] = len(fetched)
                peer["active"] = sum(1 for t in fetched if t.get("status") in {"running", "waiting", "failed"})
                peer["attention"] = sum(1 for t in fetched if t.get("needs_attention") or t.get("status") == "waiting")
                tasks.extend(fetched)
            else:
                peer["last_error"] = error
                peer["last_error_at"] = now_ms()
        self.tasks_cache = sorted(tasks, key=sort_key)
        self.tasks_cache_at = current_ms
        return self.tasks_cache

    def discover(self) -> None:
        started = now_ms()
        self.discovery_at = started
        self.discovery_error = ""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(PEER_DISCOVERY_TIMEOUT_MS / 1000)
        except OSError as exc:
            self.discovery_error = str(exc)
            return
        try:
            payload = {
                "type": "sticks3.discover",
                "device": DEVICE_ID,
                "device_name": DEVICE_NAME,
                "token": self.token,
                "want": "taskhub-peers",
            }
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            sock.sendto(body, ("255.255.255.255", self.discovery_port))
            deadline = time.time() + PEER_DISCOVERY_TIMEOUT_MS / 1000
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                except OSError:
                    break
                self._record_discovery(data, addr)
        finally:
            sock.close()
            self.discovery_duration_ms = max(0, now_ms() - started)
            self.discovery_count = len(self.peers)

    def _record_discovery(self, data: bytes, addr: Tuple[str, int]) -> None:
        try:
            payload = json.loads(data.decode("utf-8", "ignore"))
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("type") != "sticks3.hub":
            return
        peer_id = str(payload.get("device_id") or "")
        if not peer_id or peer_id == DEVICE_ID:
            return
        port = safe_int(payload.get("port")) or DEFAULT_PORT
        host = str(payload.get("host") or addr[0])
        if port == self.http_port and host in {local_ip_hint(), "127.0.0.1", "::1"}:
            return
        url = str(payload.get("url") or f"http://{host}:{port}").rstrip("/")
        self.peers[peer_id] = {
            "id": peer_id,
            "name": str(payload.get("device_name") or peer_id),
            "label": str(payload.get("device_label") or short_device_label(str(payload.get("device_name") or peer_id))),
            "host": host,
            "port": port,
            "url": url,
            "version": payload.get("version"),
            "discovered_at": now_ms(),
            "last_discovery_from": addr[0],
        }

    def _fetch_peer_tasks(self, peer: Dict[str, Any]) -> List[Task]:
        tasks, _ = self._fetch_peer_tasks_result(peer)
        return tasks

    def _fetch_peer_tasks_result(self, peer: Dict[str, Any]) -> Tuple[List[Task], str]:
        url = f"{str(peer.get('url') or '').rstrip('/')}/tasks?scope=local&limit={MAX_TASKS}"
        if not url.startswith(("http://", "https://")):
            return [], "invalid peer url"
        request = urllib.request.Request(url, headers={"X-Device-Token": self.token})
        try:
            with urllib.request.urlopen(request, timeout=PEER_HTTP_TIMEOUT_MS / 1000) as response:
                status_code = getattr(response, "status", 200)
                payload = json.loads(response.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            return [], f"http {exc.code}"
        except (OSError, urllib.error.URLError) as exc:
            return [], str(exc)
        except json.JSONDecodeError as exc:
            return [], f"bad json: {exc}"
        if status_code >= 400:
            return [], f"http {status_code}"
        if not isinstance(payload, dict) or not payload.get("ok"):
            return [], str(payload.get("error") if isinstance(payload, dict) else "bad response")
        raw_tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        peer_id = str(peer.get("id") or "")
        peer_name = str(peer.get("name") or peer_id)
        out: List[Task] = []
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                continue
            original_id = str(raw.get("id") or "")
            if not original_id:
                continue
            item = dict(raw)
            detail = dict(item.get("detail") if isinstance(item.get("detail"), dict) else {})
            detail.update(
                {
                    "remote_task_id": original_id,
                    "remote_hub_url": peer.get("url"),
                    "remote_device_id": peer_id,
                    "remote_device_name": peer_name,
                }
            )
            item["detail"] = detail
            item["id"] = stable_id("remote", f"{peer_id}:{original_id}")
            item["origin_id"] = original_id
            attach_device(item, device_id=peer_id, device_name=peer_name, origin="remote")
            item["_open"] = {
                "type": "remote",
                "url": str(peer.get("url") or ""),
                "task_id": original_id,
                "token": self.token,
            }
            out.append(item)
        return out, ""

    def snapshot(self, refresh: bool = False) -> Dict[str, Any]:
        if refresh or (PEER_ENABLED and now_ms() - self.discovery_at > PEER_DISCOVERY_MS):
            self.discover()
        peers = []
        for peer in sorted(self.peers.values(), key=lambda item: str(item.get("name") or item.get("id") or "")):
            last_error = str(peer.get("last_error") or "")
            peers.append(
                {
                    "id": peer.get("id"),
                    "name": peer.get("name"),
                    "label": peer.get("label"),
                    "host": peer.get("host"),
                    "port": peer.get("port"),
                    "url": peer.get("url"),
                    "version": peer.get("version"),
                    "status": "error" if last_error else ("ok" if peer.get("last_success_at") else "discovered"),
                    "task_count": safe_int(peer.get("task_count")),
                    "active": safe_int(peer.get("active")),
                    "attention": safe_int(peer.get("attention")),
                    "last_fetch_ms": safe_int(peer.get("last_fetch_ms")),
                    "discovered_at": ms_to_iso(safe_int(peer.get("discovered_at"))),
                    "last_fetch_at": ms_to_iso(safe_int(peer.get("last_fetch_at"))),
                    "last_success_at": ms_to_iso(safe_int(peer.get("last_success_at"))),
                    "last_error_at": ms_to_iso(safe_int(peer.get("last_error_at"))),
                    "last_error": last_error,
                }
            )
        return {
            "enabled": PEER_ENABLED,
            "version": TASK_HUB_VERSION,
            "device_id": DEVICE_ID,
            "device_name": DEVICE_NAME,
            "device_label": short_device_label(DEVICE_NAME),
            "discovery_port": self.discovery_port,
            "discovery_at": ms_to_iso(self.discovery_at),
            "discovery_count": self.discovery_count,
            "discovery_duration_ms": self.discovery_duration_ms,
            "discovery_error": self.discovery_error,
            "cache_age_ms": max(0, now_ms() - self.tasks_cache_at) if self.tasks_cache_at else None,
            "peers": peers,
        }


class Hub:
    def __init__(self, token: str = DEFAULT_TOKEN, http_port: int = DEFAULT_PORT, discovery_port: int = DEFAULT_DISCOVERY_PORT) -> None:
        self.adapters = [
            OpenClawAdapter(),
            ClaudeAdapter(),
            CodexAdapter(),
            ManusAdapter(),
            PerplexityAdapter(),
            GeminiAdapter(),
            LovableAdapter(),
        ]
        self.cache: List[Task] = []
        self.cache_at = 0
        self.detail_base_url = "http://127.0.0.1:5577"
        self.peer_manager = PeerManager(token, http_port, discovery_port)

    def list_tasks(self, include_remote: bool = True) -> List[Task]:
        if include_remote and now_ms() - self.cache_at < TASK_CACHE_MS and self.cache:
            return self.cache

        commands = ps_commands()
        tasks: List[Task] = []
        for adapter in self.adapters:
            try:
                if isinstance(adapter, (ClaudeAdapter, CodexAdapter, ManusAdapter, PerplexityAdapter, GeminiAdapter, LovableAdapter, AppAdapter)):
                    tasks.extend(adapter.list_tasks(commands))
                else:
                    tasks.extend(adapter.list_tasks())
            except Exception as exc:
                tasks.append(
                    task(
                        task_id=stable_id("adapter-error", adapter.source),
                        source=adapter.source,
                        title=f"{adapter.source} adapter error",
                        status="failed",
                        subtitle=str(exc)[:96],
                        needs_attention=True,
                    )
                )

        dedup: Dict[str, Task] = {}
        for item in tasks:
            attach_device(item, device_id=DEVICE_ID, device_name=DEVICE_NAME, origin="local")
            dedup[item["id"]] = item
        local_tasks = sorted(dedup.values(), key=sort_key)
        if not include_remote:
            return local_tasks[:MAX_TASKS]

        for item in self.peer_manager.remote_tasks():
            dedup[item["id"]] = item
        self.cache = sorted(dedup.values(), key=sort_key)[:MAX_TASKS]
        self.cache_at = now_ms()
        return self.cache

    def find_task(self, task_id: str) -> Optional[Task]:
        for item in self.list_tasks():
            if item.get("id") == task_id:
                return item
        return None

    def peers_snapshot(self, refresh: bool = False) -> Dict[str, Any]:
        return self.peer_manager.snapshot(refresh=refresh)

    def lovable_debug_snapshot(self) -> Dict[str, Any]:
        for adapter in self.adapters:
            if isinstance(adapter, LovableAdapter):
                return adapter.debug_snapshot(ps_commands())
        return {"source": "Lovable", "error": "adapter not registered"}

    def open_task(self, task_id: str) -> Tuple[bool, str]:
        return self.open_native_task(task_id)

    def open_native_task(self, task_id: str) -> Tuple[bool, str]:
        tasks = self.list_tasks()
        for item in tasks:
            if item.get("id") == task_id:
                return open_action(item.get("_open") or {})

        # Prefix fallback for stale task IDs held by a sleeping StickS3.
        if task_id.startswith("claude-"):
            return open_action({"type": "app", "target": "com.anthropic.claudefordesktop"})
        if task_id.startswith("codex-"):
            return open_action({"type": "app", "target": "com.openai.codex"})
        if task_id.startswith("openclaw-"):
            return open_action({"type": "url", "target": "http://127.0.0.1:18789/"})
        if task_id.startswith("manus-"):
            return open_action({"type": "app", "target": "im.manus.desktop"})
        if task_id.startswith("perplexity-"):
            return open_action({"type": "app", "target": "ai.perplexity.macv3"})
        if task_id.startswith("gemini-"):
            return open_action({"type": "app", "target": "com.google.GeminiMacOS"})
        if task_id == "lovable-app":
            return open_action({"type": "app", "target": "dev.lovable.build"})
        if task_id.startswith("lovable-"):
            return open_action({"type": "url", "target": "https://lovable.dev/"})
        return False, "task not found"


def open_action(action: Dict[str, str]) -> Tuple[bool, str]:
    kind = action.get("type")
    target = action.get("target")
    if not kind:
        return False, "no open action"
    if kind in {"url", "app"} and not target:
        return False, "no open target"
    try:
        if kind == "url":
            subprocess.Popen(["open", target])
        elif kind == "app":
            subprocess.Popen(["open", "-b", target])
        elif kind == "remote":
            task_id = action.get("task_id") or ""
            base_url = (action.get("url") or "").rstrip("/")
            token = action.get("token") or DEFAULT_TOKEN
            if not task_id or not base_url:
                return False, "remote action missing task_id or url"
            encoded = urllib.parse.quote(task_id, safe="")
            req = urllib.request.Request(
                f"{base_url}/tasks/{encoded}/open-native",
                method="POST",
                headers={"X-Device-Token": token},
            )
            with urllib.request.urlopen(req, timeout=PEER_HTTP_TIMEOUT_MS / 1000) as response:
                body = response.read().decode("utf-8", "ignore")
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and not payload.get("ok", True):
                    return False, str(payload.get("message") or payload.get("error") or "remote open failed")
            except json.JSONDecodeError:
                pass
        else:
            return False, f"unsupported open action: {kind}"
        return True, "opened"
    except Exception as exc:
        return False, str(exc)


class Handler(BaseHTTPRequestHandler):
    hub: Hub
    token: str

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def is_loopback(self) -> bool:
        host = self.client_address[0]
        return host == "::1" or host.startswith("127.")

    def authorized(self) -> bool:
        if not self.token:
            return True
        supplied = self.headers.get("X-Device-Token") or ""
        return supplied == self.token

    def send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def task_allowed(self) -> bool:
        return self.is_loopback() or self.authorized()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "status": "live",
                    "version": TASK_HUB_VERSION,
                    "ip": local_ip_hint(),
                    "discovery_port": DEFAULT_DISCOVERY_PORT,
                    "device_id": DEVICE_ID,
                    "device_name": DEVICE_NAME,
                    "device_label": short_device_label(DEVICE_NAME),
                },
            )
            return

        json_match = re.fullmatch(r"/tasks/([^/]+)\.json", parsed.path)
        detail_match = re.fullmatch(r"/tasks/([^/]+)", parsed.path)
        native_match = re.fullmatch(r"/tasks/([^/]+)/open-native", parsed.path)

        if native_match:
            if not self.task_allowed():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            task_id = urllib.parse.unquote(native_match.group(1))
            ok, message = self.hub.open_native_task(task_id)
            body = html_page(
                "Open original app",
                f"""
<section class="top">
  <div><h1>{html.escape("Opened" if ok else "Open failed")}</h1></div>
  <a class="button secondary" href="/tasks/{urllib.parse.quote(task_id, safe='')}">Back</a>
</section>
<p>{html.escape(message)}</p>
""",
            )
            self.send_html(200 if ok else 404, body)
            return

        if json_match or detail_match:
            if not self.task_allowed():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            task_id = urllib.parse.unquote((json_match or detail_match).group(1))
            item = self.hub.find_task(task_id)
            if not item:
                self.send_json(404, {"ok": False, "error": "task not found", "task_id": task_id})
                return
            if json_match:
                self.send_json(200, {"ok": True, "task": public_task(item)})
            else:
                self.send_html(200, render_task_detail(item))
            return

        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/debug/lovable":
            if not self.task_allowed():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self.send_json(200, {"ok": True, "debug": self.hub.lovable_debug_snapshot()})
            return

        if parsed.path in {"/peers", "/peers.json"}:
            if not self.task_allowed():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            snapshot = self.hub.peers_snapshot(refresh=(qs.get("refresh") or ["0"])[0] in {"1", "true", "yes"})
            if parsed.path == "/peers.json":
                self.send_json(200, {"ok": True, **snapshot})
            else:
                self.send_html(200, render_peers_page(snapshot))
            return

        if parsed.path == "/tasks" and self.is_loopback() and (
            not parsed.query or "text/html" in (self.headers.get("Accept") or "")
        ):
            self.send_html(200, render_task_list(self.hub.list_tasks()))
            return

        if parsed.path != "/tasks":
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        if not self.authorized():
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return

        limit = int((qs.get("limit") or ["8"])[0])
        fmt = (qs.get("format") or ["full"])[0]
        scope = (qs.get("scope") or ["all"])[0].lower()
        include_remote = scope not in {"local", "self"}
        tasks = self.hub.list_tasks(include_remote=include_remote)
        active = sum(1 for t in tasks if t.get("status") in {"running", "waiting", "failed"})
        attention = sum(1 for t in tasks if t.get("needs_attention") or t.get("status") == "waiting")
        if fmt == "stick":
            payload = {
                "ok": True,
                "ts": now_ms(),
                "poll_sec": 300,
                "count": len(tasks),
                "active": active,
                "attention": attention,
                "device": short_device_label(DEVICE_NAME),
                "tasks": [compact_task(t) for t in tasks[: max(1, min(limit, 12))]],
            }
        else:
            payload = {
                "ok": True,
                "generated_at": ms_to_iso(now_ms()),
                "ip": local_ip_hint(),
                "device_id": DEVICE_ID,
                "device_name": DEVICE_NAME,
                "device_label": short_device_label(DEVICE_NAME),
                "scope": "all" if include_remote else "local",
                "count": len(tasks),
                "active": active,
                "attention": attention,
                "tasks": [public_task(t) for t in tasks],
            }
        self.send_json(200, payload)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        match = re.fullmatch(r"/tasks/([^/]+)/(open|open-native)", parsed.path)
        if not match:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        if not self.authorized():
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        task_id = urllib.parse.unquote(match.group(1))
        ok, message = (
            self.hub.open_native_task(task_id) if match.group(2) == "open-native" else self.hub.open_task(task_id)
        )
        self.send_json(200 if ok else 404, {"ok": ok, "message": message, "task_id": task_id})


class DiscoveryServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True


class DiscoveryHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        try:
            payload = json.loads(data.decode("utf-8", "ignore"))
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("type") != "sticks3.discover":
            return

        token = getattr(self.server, "token", "")
        if token and payload.get("token") != token:
            return

        http_port = int(getattr(self.server, "http_port", DEFAULT_PORT))
        host = local_ip_hint()
        response = {
            "ok": True,
            "type": "sticks3.hub",
            "version": TASK_HUB_VERSION,
            "device_id": DEVICE_ID,
            "device_name": DEVICE_NAME,
            "device_label": short_device_label(DEVICE_NAME),
            "host": host,
            "port": http_port,
            "url": f"http://{host}:{http_port}",
            "ts": now_ms(),
        }
        body = json.dumps(response, separators=(",", ":")).encode("utf-8")
        sock.sendto(body, self.client_address)


def start_discovery(bind: str, discovery_port: int, http_port: int, token: str) -> Optional[DiscoveryServer]:
    bind_host = "0.0.0.0" if bind in {"", "0.0.0.0", "::"} else bind
    try:
        server = DiscoveryServer((bind_host, discovery_port), DiscoveryHandler)
    except OSError as exc:
        print(f"Discovery disabled on UDP {bind_host}:{discovery_port}: {exc}")
        return None
    server.token = token
    server.http_port = http_port
    thread = threading.Thread(target=server.serve_forever, name="task-hub-discovery", daemon=True)
    thread.start()
    print(f"Discovery listening on udp://{bind_host}:{discovery_port}")
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Local AI Task Hub for StickS3")
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT)
    args = parser.parse_args()

    hub = Hub(token=args.token, http_port=args.port, discovery_port=args.discovery_port)
    hub.detail_base_url = f"http://127.0.0.1:{args.port}"
    Handler.hub = hub
    Handler.token = args.token
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    discovery = start_discovery(args.bind, args.discovery_port, args.port, args.token)
    print(f"Task Hub listening on http://{args.bind}:{args.port}")
    print(f"Device: {DEVICE_NAME} ({DEVICE_ID})")
    print(f"Peer aggregation: {'enabled' if PEER_ENABLED else 'disabled'}")
    print(f"LAN hint: http://{local_ip_hint()}:{args.port}/tasks?format=stick")
    print("Set TASK_HUB_TOKEN or pass --token to change the device token.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Task Hub")
    finally:
        if discovery:
            discovery.shutdown()
            discovery.server_close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
