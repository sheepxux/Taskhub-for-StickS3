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
        self.assertFalse(th.is_human_question(""))


class StatusMachines(unittest.TestCase):
    def test_claude_status_running_via_process(self):
        m = {"latest_event_ms": th.now_ms(), "active_turn": True}
        self.assertEqual(th.claude_status(m, None, process_running=True), "running")

    def test_claude_status_waiting_wins(self):
        m = {"latest_event_ms": th.now_ms(), "active_turn": True, "waiting_for_user": True}
        self.assertEqual(th.claude_status(m, None, process_running=True), "waiting")

    def test_claude_status_idle_when_stale_and_dead(self):
        old = th.now_ms() - 10 * 24 * 3600 * 1000  # 10 days ago
        m = {"latest_event_ms": old, "active_turn": False}
        self.assertEqual(th.claude_status(m, None, process_running=False), "idle")

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

    def test_assistant_question_is_waiting(self):
        path = write_jsonl([
            {"type": "user", "timestamp": iso_now(-5000), "message": {"role": "user", "content": "help"}},
            {"type": "assistant", "timestamp": iso_now(), "message": {
                "role": "assistant", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Should I proceed with deleting the old files?"}],
            }},
        ])
        try:
            scan = th._scan_claude_transcript(path)
            self.assertTrue(scan["waiting_for_user"])
            self.assertFalse(scan["active_turn"])
        finally:
            os.remove(path)

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
            self.assertFalse(scan["waiting_for_user"])
        finally:
            os.remove(path)
            th._CODEX_SESSION_CACHE.pop(path, None)

    def test_completed_turn_then_question_is_waiting(self):
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
