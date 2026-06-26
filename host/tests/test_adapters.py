"""
Regression tests for the accuracy-critical parsing in task_hub.py.

These lock in the behaviour the adapters already get right (status derivation,
WAIT detection, case-insensitive process matching, token accounting) so a
future refactor or an upstream format tweak can't silently break it — exactly
the class of bug that the `/Claude.app/` vs `/claude.app/` casing issue was.

Pure stdlib (unittest), no third-party deps, so CI needs nothing installed.
Run:  python3 -m unittest discover -s host/tests
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import task_hub as th  # noqa: E402


def iso_now(offset_ms: int = 0) -> str:
    """A fresh ISO-8601 timestamp (UTC), shifted by offset_ms. WAIT detection
    requires the assistant message to be strictly later than the user message,
    so fixtures pass a negative offset to earlier events to order them
    deterministically (avoids same-millisecond flakiness)."""
    return (datetime.now(timezone.utc) + timedelta(milliseconds=offset_ms)).isoformat()


def write_jsonl(lines):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")
    return path


def write_codex_state_db(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute(
            """
            create table threads (
              id text,
              rollout_path text,
              title text,
              cwd text,
              updated_at_ms integer,
              tokens_used integer,
              model text,
              reasoning_effort text,
              archived integer,
              source text,
              thread_source text,
              preview text
            )
            """
        )
        con.executemany(
            """
            insert into threads (
              id, rollout_path, title, cwd, updated_at_ms, tokens_used, model,
              reasoning_effort, archived, source, thread_source, preview
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        con.commit()
    finally:
        con.close()


class PureHelpers(unittest.TestCase):
    def test_ci_contains_is_case_insensitive(self):
        # The exact bug we fixed: app path casing differs by launcher.
        self.assertTrue(th.ci_contains("/Claude.app/", "/foo/claude.app/bar"))
        self.assertTrue(th.ci_contains("Codex.app", "x/CODEX.APP/y"))
        self.assertFalse(th.ci_contains("codex", "/Applications/Other.app/"))

    def test_normalize_status_buckets(self):
        for raw in ("queued", "waiting", "needs_input", "pending"):
            self.assertEqual(th.normalize_status(raw), "waiting")
        for raw in ("running", "active", "in_progress", "syncing"):
            self.assertEqual(th.normalize_status(raw), "running")
        for raw in ("failed", "error", "timed_out", "lost"):
            self.assertEqual(th.normalize_status(raw), "failed")
        for raw in ("succeeded", "done", "completed"):
            self.assertEqual(th.normalize_status(raw), "done")

    def test_is_human_question(self):
        self.assertTrue(th.is_human_question("Should I proceed with the migration?"))
        self.assertTrue(th.is_human_question("Please confirm before I delete these files."))
        self.assertFalse(th.is_human_question("Running tests now."))
        self.assertFalse(th.is_human_question("I went ahead and finished the task."))
        self.assertFalse(th.is_human_question(""))

    def test_bounded_cache_evicts_lru(self):
        cache = th.OrderedDict()
        original_max = th.TRANSCRIPT_CACHE_MAX
        th.TRANSCRIPT_CACHE_MAX = 2
        try:
            th.bounded_cache_set(cache, "a", {"_key": (1, 1)})
            th.bounded_cache_set(cache, "b", {"_key": (1, 1)})
            self.assertIsNotNone(th.bounded_cache_get(cache, "a", (1, 1)))
            th.bounded_cache_set(cache, "c", {"_key": (1, 1)})
            self.assertIn("a", cache)
            self.assertIn("c", cache)
            self.assertNotIn("b", cache)
        finally:
            th.TRANSCRIPT_CACHE_MAX = original_max


class StatusMachines(unittest.TestCase):
    def test_claude_status_running_via_process(self):
        m = {"latest_event_ms": th.now_ms(), "active_turn": True}
        self.assertEqual(th.claude_status(m, None, process_running=True), "running")

    def test_claude_status_running_when_process_has_no_transcript_yet(self):
        self.assertEqual(th.claude_status({}, None, process_running=True), "running")

    def test_claude_status_waiting_wins(self):
        m = {"latest_event_ms": th.now_ms(), "active_turn": True, "waiting_for_user": True}
        self.assertEqual(th.claude_status(m, None, process_running=True), "waiting")

    def test_claude_status_idle_when_stale_and_dead(self):
        old = th.now_ms() - 10 * 24 * 3600 * 1000  # 10 days ago
        m = {"latest_event_ms": old, "active_turn": False}
        self.assertEqual(th.claude_status(m, None, process_running=False), "idle")

    def test_claude_status_done_in_window_after_terminal_turn(self):
        now = th.now_ms()
        m = {
            "latest_event_ms": now,
            "active_turn": False,
            "latest_terminal_ms": now,
            "latest_stop_reason": "end_turn",
        }
        self.assertEqual(th.claude_status(m, None, process_running=False), "done")

    def test_claude_status_recent_after_done_window_elapses(self):
        past = th.now_ms() - th.CLAUDE_DONE_WINDOW_MS - 1000
        m = {
            "latest_event_ms": past,
            "active_turn": False,
            "latest_terminal_ms": past,
            "latest_stop_reason": "end_turn",
        }
        self.assertEqual(th.claude_status(m, None, process_running=False), "recent")

    def test_claude_turn_state(self):
        self.assertEqual(th.claude_turn_state({"waiting_for_user": True}), "wait")
        self.assertEqual(th.claude_turn_state({"active_turn": True, "latest_stop_reason": "tool_use"}), "tool")
        self.assertEqual(th.claude_turn_state({"active_turn": True}), "active")
        self.assertEqual(th.claude_turn_state({"latest_stop_reason": "end_turn"}), "done")

    def test_codex_status_waiting_and_running(self):
        now = th.now_ms()
        self.assertEqual(th.codex_status({"waiting_for_user": True, "latest_event_ms": now}), "waiting")
        self.assertEqual(
            th.codex_status({"active_turn": True, "latest_event_ms": now}, app_is_running=True),
            "running",
        )


class ClaudeTranscriptScan(unittest.TestCase):
    def test_claude_app_fallback_when_no_code_session_metadata(self):
        old_home = os.environ.get("HOME")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HOME"] = tmp
            try:
                commands = ["/Applications/Claude.app/Contents/MacOS/Claude"]
                tasks = th.ClaudeAdapter().list_tasks(commands=commands)
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]["id"], "claude-app")
                self.assertEqual(tasks[0]["status"], "idle")
                self.assertEqual(tasks[0]["detail"]["metadata_source"], "desktop-app-fallback")
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home

    def test_tool_use_turn_is_active(self):
        path = write_jsonl([
            {"type": "user", "timestamp": iso_now(-5000), "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "timestamp": iso_now(), "message": {
                "role": "assistant", "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }},
        ])
        try:
            scan = th._scan_claude_transcript(path)
            self.assertTrue(scan["active_turn"])
            self.assertFalse(scan["waiting_for_user"])
            self.assertEqual(scan["latest_stop_reason"], "tool_use")
        finally:
            os.remove(path)

    def test_text_question_at_turn_end_is_done_not_waiting(self):
        # A turn that ends with question-like prose (but no AskUserQuestion tool)
        # is a completed turn, not a WAIT — WAIT is reserved for explicit asks.
        path = write_jsonl([
            {"type": "user", "timestamp": iso_now(-5000), "message": {"role": "user", "content": "help"}},
            {"type": "assistant", "timestamp": iso_now(), "message": {
                "role": "assistant", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Should I proceed with deleting the old files?"}],
            }},
        ])
        try:
            scan = th._scan_claude_transcript(path)
            self.assertFalse(scan["waiting_for_user"])
            self.assertFalse(scan["active_turn"])
        finally:
            os.remove(path)

    def test_pending_ask_user_question_is_waiting(self):
        # An unanswered AskUserQuestion tool call (no later terminal) is a WAIT.
        path = write_jsonl([
            {"type": "user", "timestamp": iso_now(-5000), "message": {"role": "user", "content": "help"}},
            {"type": "assistant", "timestamp": iso_now(), "message": {
                "role": "assistant", "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "AskUserQuestion", "input": {}}],
            }},
        ])
        try:
            scan = th._scan_claude_transcript(path)
            self.assertTrue(scan["waiting_for_user"])
        finally:
            os.remove(path)
            th._CLAUDE_TRANSCRIPT_CACHE.pop(path, None)

    def test_stale_human_input_tool_is_cleared_by_later_end_turn(self):
        path = write_jsonl([
            {"type": "user", "timestamp": iso_now(-9000), "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "timestamp": iso_now(-8000), "message": {
                "role": "assistant", "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "AskUserQuestion", "input": {}}],
            }},
            {"type": "assistant", "timestamp": iso_now(), "message": {
                "role": "assistant", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "All done. Tests pass."}],
            }},
        ])
        try:
            scan = th._scan_claude_transcript(path)
            self.assertFalse(scan["active_turn"])
            self.assertFalse(scan["waiting_for_user"])
            self.assertEqual(scan["latest_stop_reason"], "end_turn")
        finally:
            os.remove(path)
            th._CLAUDE_TRANSCRIPT_CACHE.pop(path, None)

    def test_end_turn_statement_is_done_not_waiting(self):
        path = write_jsonl([
            {"type": "user", "timestamp": iso_now(), "message": {"role": "user", "content": "fix it"}},
            {"type": "assistant", "timestamp": iso_now(), "message": {
                "role": "assistant", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "All done. Tests pass."}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }},
        ])
        try:
            scan = th._scan_claude_transcript(path)
            self.assertFalse(scan["active_turn"])
            self.assertFalse(scan["waiting_for_user"])
            self.assertEqual(scan["total_tokens"], 150)
        finally:
            os.remove(path)

    def test_scan_is_memoised(self):
        path = write_jsonl([
            {"type": "assistant", "timestamp": iso_now(), "message": {"role": "assistant", "stop_reason": "end_turn", "content": "ok"}},
        ])
        try:
            a = th._scan_claude_transcript(path)
            b = th._scan_claude_transcript(path)
            self.assertIs(a, b)  # same object → served from cache, no re-parse
        finally:
            os.remove(path)
            th._CLAUDE_TRANSCRIPT_CACHE.pop(path, None)

    def test_local_agent_transcript_root_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "local-agent-session"
            root = os.path.join(tmp, ".claude", "projects", "project")
            os.makedirs(root)
            path = os.path.join(root, f"{session_id}.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "assistant",
                    "timestamp": iso_now(),
                    "message": {
                        "role": "assistant",
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "name": "Bash"}],
                        "usage": {"input_tokens": 7, "output_tokens": 3},
                    },
                }) + "\n")
            try:
                metrics = th.claude_session_metrics(session_id, transcript_roots=[os.path.join(tmp, ".claude", "projects")])
                self.assertTrue(metrics["transcript_found"])
                self.assertTrue(metrics["active_turn"])
                self.assertEqual(metrics["usage"]["total_tokens"], 10)
            finally:
                th._CLAUDE_TRANSCRIPT_CACHE.pop(path, None)


class CodexSessionScan(unittest.TestCase):
    def test_open_turn_is_active(self):
        path = write_jsonl([
            {"type": "session_meta", "timestamp": iso_now(-5000), "payload": {"id": "s1", "cwd": "/x"}},
            {"type": "event_msg", "timestamp": iso_now(), "payload": {"type": "task_started", "turn_id": "t1"}},
        ])
        try:
            scan = th._scan_codex_session(path)
            self.assertEqual(scan["latest_turn_id"], "t1")
            self.assertNotEqual(scan["latest_turn_id"], scan["latest_completed_turn_id"])
            self.assertTrue(scan["active_turn"])
            self.assertFalse(scan["waiting_for_user"])
        finally:
            os.remove(path)
            th._CODEX_SESSION_CACHE.pop(path, None)

    def test_completed_turn_then_text_question_is_not_waiting(self):
        # Prose question after a completed turn is DONE, not WAIT (no explicit ask).
        path = write_jsonl([
            {"type": "session_meta", "timestamp": iso_now(-9000), "payload": {"id": "s2", "cwd": "/x"}},
            {"type": "event_msg", "timestamp": iso_now(-8000), "payload": {"type": "user_message", "message": "go"}},
            {"type": "event_msg", "timestamp": iso_now(-7000), "payload": {"type": "task_started", "turn_id": "t1"}},
            {"type": "event_msg", "timestamp": iso_now(-6000), "payload": {"type": "task_complete", "turn_id": "t1"}},
            {"type": "event_msg", "timestamp": iso_now(), "payload": {"type": "agent_message", "message": "Should I run the deploy now?"}},
        ])
        try:
            scan = th._scan_codex_session(path)
            self.assertEqual(scan["latest_turn_id"], scan["latest_completed_turn_id"])  # not active
            self.assertFalse(scan["active_turn"])
            self.assertFalse(scan["waiting_for_user"])
        finally:
            os.remove(path)
            th._CODEX_SESSION_CACHE.pop(path, None)

    def test_pending_request_user_input_is_waiting(self):
        # An explicit request_user_input function call (unanswered) is a WAIT.
        path = write_jsonl([
            {"type": "session_meta", "timestamp": iso_now(-9000), "payload": {"id": "s4", "cwd": "/x"}},
            {"type": "event_msg", "timestamp": iso_now(-8000), "payload": {"type": "user_message", "message": "go"}},
            {"type": "response_item", "timestamp": iso_now(), "payload": {"type": "function_call", "name": "request_user_input"}},
        ])
        try:
            scan = th._scan_codex_session(path)
            self.assertTrue(scan["waiting_for_user"])
        finally:
            os.remove(path)
            th._CODEX_SESSION_CACHE.pop(path, None)

    def test_completed_statement_is_not_waiting(self):
        path = write_jsonl([
            {"type": "session_meta", "timestamp": iso_now(-9000), "payload": {"id": "s3", "cwd": "/x"}},
            {"type": "event_msg", "timestamp": iso_now(-8000), "payload": {"type": "user_message", "message": "go"}},
            {"type": "event_msg", "timestamp": iso_now(-7000), "payload": {"type": "task_started", "turn_id": "t1"}},
            {"type": "event_msg", "timestamp": iso_now(-6000), "payload": {"type": "task_complete", "turn_id": "t1"}},
            {"type": "event_msg", "timestamp": iso_now(), "payload": {"type": "agent_message", "message": "Deploy finished successfully."}},
        ])
        try:
            scan = th._scan_codex_session(path)
            self.assertFalse(scan["waiting_for_user"])
        finally:
            os.remove(path)
            th._CODEX_SESSION_CACHE.pop(path, None)

    def test_completed_at_seconds_marks_done(self):
        completed_sec = th.now_ms() // 1000
        path = write_jsonl([
            {"type": "session_meta", "timestamp": iso_now(-9000), "payload": {"id": "s5", "cwd": "/x"}},
            {"type": "event_msg", "timestamp": iso_now(-7000), "payload": {"type": "task_started", "turn_id": "t1"}},
            {
                "type": "event_msg",
                "timestamp": iso_now(),
                "payload": {"type": "task_complete", "turn_id": "t1", "completed_at": completed_sec},
            },
        ])
        try:
            scan = th._scan_codex_session(path)
            self.assertEqual(scan["latest_turn_id"], scan["latest_completed_turn_id"])
            self.assertGreater(scan["latest_completed_ms"], 10_000_000_000)
            self.assertEqual(th.codex_status(scan, app_is_running=True), "done")
        finally:
            os.remove(path)
            th._CODEX_SESSION_CACHE.pop(path, None)

    def test_file_mtime_does_not_make_stale_session_recent(self):
        stale_ms = th.now_ms() - (th.ACTIVE_MINUTES + 60) * 60 * 1000
        stale_iso = datetime.fromtimestamp(stale_ms / 1000, timezone.utc).isoformat()
        path = write_jsonl([
            {"type": "session_meta", "timestamp": stale_iso, "payload": {"id": "s6", "cwd": "/x"}},
            {"type": "event_msg", "timestamp": stale_iso, "payload": {"type": "task_started", "turn_id": "t1"}},
            {
                "type": "event_msg",
                "timestamp": stale_iso,
                "payload": {"type": "task_complete", "turn_id": "t1", "completed_at": stale_ms // 1000},
            },
        ])
        try:
            os.utime(path, None)
            scan = th._scan_codex_session(path)
            self.assertEqual(scan["latest_event_ms"], th.iso_to_ms(stale_iso))
            self.assertGreater(scan["file_mtime_ms"], scan["latest_event_ms"])
            self.assertEqual(th.codex_status(scan, app_is_running=True), "idle")
        finally:
            os.remove(path)
            th._CODEX_SESSION_CACHE.pop(path, None)

    def test_adapter_preserves_record_updated_time(self):
        old_ms = th.now_ms() - 90 * 60 * 1000
        original = th.codex_records
        try:
            th.codex_records = lambda: [{
                "id": "s7",
                "cwd": "/x",
                "folder": "x",
                "title": "Old Codex task",
                "updated_ms": old_ms,
                "latest_event_ms": old_ms,
                "usage": {},
            }]
            tasks = th.CodexAdapter().list_tasks(commands=[])
            self.assertEqual(tasks[0]["updated_ms"], old_ms)
            self.assertGreaterEqual(tasks[0]["age_sec"], 90 * 60)
            self.assertLessEqual(tasks[0]["age_sec"], 90 * 60 + 2)
        finally:
            th.codex_records = original

    def test_state_records_read_root_and_nested_codex_dbs(self):
        old_home = os.environ.get("HOME")
        now = th.now_ms()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.environ["HOME"] = tmp
                write_codex_state_db(
                    os.path.join(tmp, ".codex", "state_5.sqlite"),
                    [("root-thread", "", "Root DB thread", "/tmp/root", now, 10, "gpt-test", "", 0, "", "user", "")],
                )
                write_codex_state_db(
                    os.path.join(tmp, ".codex", "sqlite", "state_5.sqlite"),
                    [("nested-thread", "", "Nested DB thread", "/tmp/nested", now - 1000, 5, "gpt-test", "", 0, "", "user", "")],
                )
                records = th.codex_state_records(max_threads=4)
                ids = [item["id"] for item in records]
                self.assertIn("root-thread", ids)
                self.assertIn("nested-thread", ids)
                self.assertEqual(records[0]["id"], "root-thread")
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home


class OpenClawSessions(unittest.TestCase):
    def test_stale_failed_sessions_are_hidden(self):
        now = th.now_ms()
        old_ms = now - th.OPENCLAW_FAILED_TTL_MS - 1000
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = os.path.join(tmp, "agents", "main", "sessions")
            os.makedirs(sessions_dir)
            with open(os.path.join(sessions_dir, "sessions.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "old-failed": {
                            "sessionId": "old",
                            "status": "failed",
                            "updatedAt": old_ms,
                            "endedAt": old_ms,
                            "model": "old-model",
                        },
                        "fresh-failed": {
                            "sessionId": "fresh",
                            "status": "failed",
                            "updatedAt": now,
                            "endedAt": now,
                            "model": "fresh-model",
                        },
                        "heartbeat-failed": {
                            "sessionId": "heartbeat",
                            "status": "failed",
                            "updatedAt": now,
                            "endedAt": now,
                            "lastTo": "heartbeat",
                            "model": "heartbeat-model",
                        },
                        "heartbeat-running": {
                            "sessionId": "heartbeat-running",
                            "status": "running",
                            "updatedAt": now,
                            "lastTo": "heartbeat",
                            "model": "heartbeat-running-model",
                        },
                        "agent:main:main": {
                            "sessionId": "idle-direct",
                            "status": "running",
                            "updatedAt": now,
                            "chatType": "direct",
                            "model": "idle-direct-model",
                        },
                    },
                    fh,
                )
            adapter = th.OpenClawAdapter()
            adapter.state_dir = tmp
            tasks = adapter._list_sessions()
            titles = [item["title"] for item in tasks]
            self.assertTrue(any("fresh-model" in title for title in titles))
            self.assertFalse(any("old-model" in title for title in titles))
            self.assertFalse(any("heartbeat-model" in title for title in titles))
            self.assertFalse(any("heartbeat-running-model" in title for title in titles))
            self.assertFalse(any("idle-direct-model" in title for title in titles))


class ExternalIngest(unittest.TestCase):
    def test_accepts_single_task_and_lists_it(self):
        ext = th.ExternalTaskAdapter()
        ok, _, n = ext.ingest({"source": "Gemini", "title": "Refactor pricing page", "status": "running"})
        self.assertTrue(ok)
        self.assertEqual(n, 1)
        tasks = ext.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["source"], "Gemini")
        self.assertEqual(tasks[0]["title"], "Refactor pricing page")
        self.assertEqual(tasks[0]["status"], "running")
        self.assertTrue(tasks[0]["id"].startswith("ext-"))

    def test_accepts_batch(self):
        ext = th.ExternalTaskAdapter()
        ok, _, n = ext.ingest({"tasks": [
            {"source": "Lovable", "title": "Build landing page"},
            {"source": "Perplexity", "title": "Research competitors", "status": "waiting"},
        ]})
        self.assertTrue(ok)
        self.assertEqual(n, 2)
        self.assertEqual(len(ext.list_tasks()), 2)

    def test_rejects_missing_fields(self):
        ext = th.ExternalTaskAdapter()
        ok, _, n = ext.ingest({"source": "Gemini"})           # no title
        self.assertTrue(ok)        # call succeeds…
        self.assertEqual(n, 0)     # …but nothing accepted
        self.assertEqual(len(ext.list_tasks()), 0)

    def test_rejects_garbage_payload(self):
        ext = th.ExternalTaskAdapter()
        ok, _, n = ext.ingest("not a task")
        self.assertFalse(ok)

    def test_refresh_updates_same_id(self):
        ext = th.ExternalTaskAdapter()
        ext.ingest({"id": "ext-x", "source": "Gemini", "title": "First", "status": "running"})
        ext.ingest({"id": "ext-x", "source": "Gemini", "title": "Second", "status": "done"})
        tasks = ext.list_tasks()
        self.assertEqual(len(tasks), 1)            # same id → replaced, not duplicated
        self.assertEqual(tasks[0]["title"], "Second")
        self.assertEqual(tasks[0]["status"], "done")

    def test_waiting_sets_attention(self):
        ext = th.ExternalTaskAdapter()
        ext.ingest({"source": "Lovable", "title": "Need API key", "status": "waiting"})
        self.assertTrue(ext.list_tasks()[0]["needs_attention"])

    def test_expired_tasks_drop_out(self):
        ext = th.ExternalTaskAdapter()
        ext.ingest({"source": "Gemini", "title": "Quick task", "ttl_sec": 1})
        # Force expiry by rewinding the stored expiry timestamp into the past.
        for entry in ext._store.values():
            entry["expires_ms"] = th.now_ms() - 1
        self.assertEqual(len(ext.list_tasks()), 0)

    def test_url_becomes_open_action(self):
        ext = th.ExternalTaskAdapter()
        ext.ingest({"source": "Lovable", "title": "Open project", "url": "https://lovable.dev/p/123"})
        self.assertEqual(ext.list_tasks()[0]["_open"], {"type": "url", "target": "https://lovable.dev/p/123"})


if __name__ == "__main__":
    unittest.main()
