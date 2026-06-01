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
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple


Task = Dict[str, Any]

DEFAULT_PORT = int(os.environ.get("TASK_HUB_PORT", "5577"))
DEFAULT_BIND = os.environ.get("TASK_HUB_BIND", "0.0.0.0")
DEFAULT_TOKEN = os.environ.get("TASK_HUB_TOKEN", "dev-token")
DEFAULT_DISCOVERY_PORT = int(os.environ.get("TASK_HUB_DISCOVERY_PORT", "5578"))
MAX_TASKS = int(os.environ.get("TASK_HUB_MAX_TASKS", "40"))
ACTIVE_MINUTES = int(os.environ.get("TASK_HUB_ACTIVE_MINUTES", "1440"))
TASK_CACHE_MS = int(os.environ.get("TASK_HUB_CACHE_MS", "3000"))
CODEX_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CODEX_RUNNING_STALE_MS", "900000"))
CLAUDE_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_CLAUDE_RUNNING_STALE_MS", str(CODEX_RUNNING_STALE_MS)))
CLAUDE_TERMINAL_STOP_REASONS = {"end_turn", "stop_sequence", "max_tokens"}
OPENCLAW_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_OPENCLAW_RUNNING_STALE_MS", "1800000"))
MANUS_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_MANUS_RUNNING_STALE_MS", "900000"))
MANUS_MAX_SESSIONS = int(os.environ.get("TASK_HUB_MANUS_MAX_SESSIONS", "3"))
MANUS_TERMINAL_STATUS_CODES = {5, 7}
PERPLEXITY_RUNNING_STALE_MS = int(os.environ.get("TASK_HUB_PERPLEXITY_RUNNING_STALE_MS", "30000"))


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone().isoformat(timespec="seconds")


def stable_id(prefix: str, raw: str) -> str:
    digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def run(args: List[str], timeout: float = 4.0) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
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


def claude_session_metrics(cli_session_id: str, completed_turns: Any = None) -> Dict[str, Any]:
    path = claude_transcript_path(cli_session_id)
    turns = safe_int(completed_turns)
    if not path:
        return {"usage": build_usage(turns=turns), "turns": turns}

    fields = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    seen_requests = set()
    seen_terminal_requests = set()
    request_count = 0
    model = ""
    terminal_turns = 0
    latest_event_ms = 0
    latest_turn_event_ms = 0
    latest_user_ms = 0
    latest_assistant_ms = 0
    latest_terminal_ms = 0
    latest_stop_reason = ""
    latest_request_id = ""
    latest_turn_event_type = ""

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
        return {"usage": build_usage(turns=turns), "turns": turns}

    active_turn = False
    if latest_turn_event_ms:
        if latest_turn_event_type == "assistant":
            active_turn = latest_stop_reason not in CLAUDE_TERMINAL_STOP_REASONS
        else:
            active_turn = latest_turn_event_ms > latest_terminal_ms

    total = total_tokens_from_usage(fields, list(fields.keys()))
    usage = build_usage(
        total_tokens=total,
        turns=turns or terminal_turns or request_count,
        fields={
            "source": "claude-transcript",
            "model": model,
            "requests": request_count,
            **fields,
        },
    )
    return {
        "usage": usage,
        "turns": turns or terminal_turns or request_count,
        "requests": request_count,
        "transcript_found": True,
        "latest_event_ms": latest_turn_event_ms or latest_event_ms,
        "latest_user_ms": latest_user_ms,
        "latest_assistant_ms": latest_assistant_ms,
        "latest_terminal_ms": latest_terminal_ms,
        "latest_stop_reason": latest_stop_reason,
        "latest_request_id": latest_request_id,
        "active_turn": active_turn,
    }


def claude_usage(cli_session_id: str, completed_turns: Any = None) -> Dict[str, Any]:
    return claude_session_metrics(cli_session_id, completed_turns).get("usage") or {}


def claude_status(metrics: Dict[str, Any], updated_ms: Optional[int], process_running: bool = False) -> str:
    latest = safe_int(metrics.get("latest_event_ms") or updated_ms)
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


def codex_usage_records(max_files: int = 80) -> List[Dict[str, Any]]:
    root = os.path.expanduser("~/.codex/sessions")
    index = codex_session_index()
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    files = sorted(files, key=os.path.getmtime, reverse=True)[:max_files]
    records: List[Dict[str, Any]] = []

    for path in files:
        session_id = ""
        cwd = ""
        updated_ms = int(os.path.getmtime(path) * 1000)
        token_usage: Dict[str, Any] = {}
        rate_percent: Optional[float] = None
        turns = 0
        latest_turn_id = ""
        latest_turn_ms = 0
        latest_completed_turn_id = ""
        latest_completed_ms = 0
        latest_event_ms = updated_ms

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
                    elif payload_type == "task_started" or item_type == "turn_context" or payload_type == "turn_context":
                        latest_turn_id = str(payload.get("turn_id") or item.get("turn_id") or latest_turn_id)
                        latest_turn_ms = safe_ms(payload.get("started_at")) or event_ms or latest_turn_ms
                        updated_ms = latest_turn_ms or event_ms or updated_ms
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
            continue

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
                "active_turn": bool(latest_turn_id and latest_turn_id != latest_completed_turn_id),
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
        for line in commands:
            if "/Claude.app/" in line and "/claude " in line:
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
                        "turn_state": claude_turn_state(metrics),
                        "latest_stop_reason": metrics.get("latest_stop_reason"),
                        "latest_event_at": ms_to_iso(safe_int(metrics.get("latest_event_ms"))),
                        "transcript_found": bool(metrics.get("transcript_found")),
                    },
                    usage=usage,
                    open_action={"type": "app", "target": "com.anthropic.claudefordesktop"},
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
                            "turn_state": claude_turn_state(metrics),
                            "latest_stop_reason": metrics.get("latest_stop_reason"),
                            "latest_event_at": ms_to_iso(safe_int(metrics.get("latest_event_ms"))),
                            "transcript_found": bool(metrics.get("transcript_found")),
                        },
                        usage=metrics.get("usage") or {},
                        open_action={"type": "app", "target": "com.anthropic.claudefordesktop"},
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
        app_server_count = sum("Codex.app/Contents/Resources/codex app-server" in line for line in commands)
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
                    },
                    usage=usage,
                    open_action={"type": "app", "target": "com.openai.codex"},
                )
            )

        for line in commands:
            if "/Codex.app/Contents/Resources/node " not in line or "--session-id" not in line:
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
                    },
                    usage=usage,
                    open_action={"type": "app", "target": "com.openai.codex"},
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
                    },
                    usage=usage,
                    open_action={"type": "app", "target": "com.openai.codex"},
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
        self.pref_path = os.path.expanduser(
            "~/Library/Containers/ai.perplexity.mac/Data/Library/Preferences/ai.perplexity.mac.plist"
        )
        self.cache_db = os.path.expanduser(
            "~/Library/Containers/ai.perplexity.mac/Data/Library/Caches/ai.perplexity.mac/Cache.db"
        )
        self.http_storage = os.path.expanduser(
            "~/Library/Containers/ai.perplexity.mac/Data/Library/HTTPStorages/ai.perplexity.mac/httpstorages.sqlite"
        )
        self._last_signature = ""
        self._last_change_ms = 0

    def list_tasks(self, commands: Optional[List[str]] = None) -> List[Task]:
        commands = commands or ps_commands()
        running = app_running(commands, self.bundle_fragment, self.binary_name)
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
        return [
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
        http_mtime = self._path_mtime_ms(self.http_storage)
        if http_mtime:
            state["http_mtime_ms"] = http_mtime
        state["status_basis"] = "local preferences + WebKit cache mtimes" if state else "app process only"
        return state

    def _read_preferences(self) -> Dict[str, Any]:
        if not os.path.exists(self.pref_path):
            return self._read_preferences_with_defaults()
        path = self.pref_path
        temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
        try:
            temp_dir = tempfile.TemporaryDirectory(prefix="sticks3-pplx-pref-")
            copy_path = os.path.join(temp_dir.name, "preferences.plist")
            shutil.copy2(self.pref_path, copy_path)
            path = copy_path
        except Exception:
            path = self.pref_path
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
            "prefs_mtime_ms": self._path_mtime_ms(self.pref_path),
            "local_query_count": safe_int(data.get("ThreadViewModel.localQueryCount")),
            "local_deep_research_query_count": safe_int(data.get("localDeepResearchQueryCount")),
            "local_query_count_for_prompt": safe_int(data.get("localQueryCountForPrompt")),
        }

    def _read_preferences_with_defaults(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"prefs_mtime_ms": self._path_mtime_ms(self.pref_path)}
        fields = {
            "ThreadViewModel.localQueryCount": "local_query_count",
            "localDeepResearchQueryCount": "local_deep_research_query_count",
            "localQueryCountForPrompt": "local_query_count_for_prompt",
        }
        for key, target in fields.items():
            code, stdout, _ = run(["/usr/bin/defaults", "read", "ai.perplexity.mac", key], timeout=1)
            if code == 0 and stdout.strip():
                out[target] = safe_int(stdout.strip())
        return out

    def _read_cache_hint(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        cache_mtime = self._path_mtime_ms(self.cache_db)
        if cache_mtime:
            out["cache_mtime_ms"] = cache_mtime
        if not os.path.exists(self.cache_db):
            return out
        try:
            row = self._latest_cache_row(self.cache_db)
        except Exception:
            row = self._latest_cache_row_from_copy()
        if not row:
            return out
        endpoint = self._sanitize_endpoint(str(row[0] or ""))
        if endpoint:
            out["latest_endpoint"] = endpoint
        latest_ms = self._parse_cache_time(row[1])
        if latest_ms:
            out["latest_cache_ms"] = latest_ms
        return out

    def _latest_cache_row(self, path: str) -> Optional[Tuple[Any, Any]]:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute(
                """
                select request_key, time_stamp
                from cfurl_cache_response
                where request_key like '%perplexity%' or request_key like '%pplx%'
                order by time_stamp desc
                limit 1
                """
            ).fetchone()
        finally:
            con.close()

    def _latest_cache_row_from_copy(self) -> Optional[Tuple[Any, Any]]:
        with tempfile.TemporaryDirectory(prefix="sticks3-pplx-cache-") as tmp:
            copy_path = os.path.join(tmp, "Cache.db")
            try:
                shutil.copy2(self.cache_db, copy_path)
                for suffix in ("-wal", "-shm"):
                    src = f"{self.cache_db}{suffix}"
                    if os.path.exists(src):
                        shutil.copy2(src, f"{copy_path}{suffix}")
            except OSError:
                return None
            try:
                return self._latest_cache_row(copy_path)
            except Exception:
                return None

    def _path_mtime_ms(self, path: str) -> int:
        mtimes = []
        for item in [path, f"{path}-wal", f"{path}-shm"]:
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
    @media (max-width: 640px) {{
      .top {{ align-items: flex-start; flex-direction: column; }}
      dl {{ grid-template-columns: 1fr; gap: 4px 0; }}
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


class Hub:
    def __init__(self) -> None:
        self.adapters = [
            OpenClawAdapter(),
            ClaudeAdapter(),
            CodexAdapter(),
            ManusAdapter(),
            PerplexityAdapter(),
        ]
        self.cache: List[Task] = []
        self.cache_at = 0
        self.detail_base_url = "http://127.0.0.1:5577"

    def list_tasks(self) -> List[Task]:
        if now_ms() - self.cache_at < TASK_CACHE_MS and self.cache:
            return self.cache

        commands = ps_commands()
        tasks: List[Task] = []
        for adapter in self.adapters:
            try:
                if isinstance(adapter, (ClaudeAdapter, CodexAdapter, ManusAdapter, PerplexityAdapter, AppAdapter)):
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
            dedup[item["id"]] = item
        self.cache = sorted(dedup.values(), key=sort_key)[:MAX_TASKS]
        self.cache_at = now_ms()
        return self.cache

    def find_task(self, task_id: str) -> Optional[Task]:
        for item in self.list_tasks():
            if item.get("id") == task_id:
                return item
        return None

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
        return False, "task not found"


def open_action(action: Dict[str, str]) -> Tuple[bool, str]:
    kind = action.get("type")
    target = action.get("target")
    if not kind or not target:
        return False, "no open action"
    try:
        if kind == "url":
            subprocess.Popen(["open", target])
        elif kind == "app":
            subprocess.Popen(["open", "-b", target])
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
                    "ip": local_ip_hint(),
                    "discovery_port": DEFAULT_DISCOVERY_PORT,
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
        tasks = self.hub.list_tasks()
        active = sum(1 for t in tasks if t.get("status") in {"running", "waiting", "failed"})
        attention = sum(1 for t in tasks if t.get("needs_attention"))
        if fmt == "stick":
            payload = {
                "ok": True,
                "ts": now_ms(),
                "poll_sec": 300,
                "count": len(tasks),
                "active": active,
                "attention": attention,
                "tasks": [compact_task(t) for t in tasks[: max(1, min(limit, 12))]],
            }
        else:
            payload = {
                "ok": True,
                "generated_at": ms_to_iso(now_ms()),
                "ip": local_ip_hint(),
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

    hub = Hub()
    hub.detail_base_url = f"http://127.0.0.1:{args.port}"
    Handler.hub = hub
    Handler.token = args.token
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    discovery = start_discovery(args.bind, args.discovery_port, args.port, args.token)
    print(f"Task Hub listening on http://{args.bind}:{args.port}")
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
