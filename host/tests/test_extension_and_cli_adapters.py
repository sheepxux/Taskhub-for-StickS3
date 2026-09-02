"""
Fixture-driven tests for the Cline-family, Gemini CLI / Qwen Code and GitHub
Copilot CLI adapters. The fixtures mirror each tool's documented on-disk
transcript shape; they pin down the RUN / WAIT / DONE / FAIL derivation and the
"no live process -> nothing can be running" downgrade.

Run:  python3 -m unittest discover -s host/tests
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import task_hub as th  # noqa: E402


def iso(offset_ms: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(milliseconds=offset_ms)).isoformat()


def ms(offset_ms: int = 0) -> int:
    return th.now_ms() + offset_ms


# ---------------------------------------------------------------------------
# Cline / Roo Code / Kilo Code
# ---------------------------------------------------------------------------
class ClineFixture:
    def __init__(self, tmp: str, host_fragment: str = "/Visual Studio Code.app/"):
        self.store_dir = os.path.join(tmp, "globalStorage", "saoudrizwan.claude-dev")
        os.makedirs(os.path.join(self.store_dir, "tasks"), exist_ok=True)
        self.host_fragment = host_fragment
        self.adapter = th.ClineAdapter(
            stores=[
                {
                    "source": "Cline",
                    "host": "VS Code",
                    "store_dir": self.store_dir,
                    "bundle_fragment": host_fragment,
                    "app_name": "Visual Studio Code",
                }
            ]
        )

    def write_task(self, task_id: str, messages, history=None) -> str:
        task_dir = os.path.join(self.store_dir, "tasks", task_id)
        os.makedirs(task_dir, exist_ok=True)
        path = os.path.join(task_dir, "ui_messages.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(messages, fh)
        th._CLINE_TASK_CACHE.pop(path, None)
        if history is not None:
            state_dir = os.path.join(self.store_dir, "state")
            os.makedirs(state_dir, exist_ok=True)
            with open(os.path.join(state_dir, "taskHistory.json"), "w", encoding="utf-8") as fh:
                json.dump(history, fh)
        return path

    def host_commands(self):
        return [f"4242 S   Wed Sep  2 18:00:00 2026 /Applications{self.host_fragment}Contents/MacOS/Electron --type=extensionHost"]


def cline_task_prompt(ts: int, text: str = "Refactor the parser"):
    return {"ts": ts, "type": "say", "say": "task", "text": text}


def cline_api_req(ts: int, tokens_in=1200, tokens_out=300, cost=0.0123):
    return {
        "ts": ts,
        "type": "say",
        "say": "api_req_started",
        "text": json.dumps({"tokensIn": tokens_in, "tokensOut": tokens_out, "cacheWrites": 0, "cacheReads": 800, "cost": cost}),
    }


class ClineAdapterTests(unittest.TestCase):
    def test_unanswered_tool_ask_is_waiting_with_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = ClineFixture(tmp)
            fx.write_task(
                "1725000000001",
                [
                    cline_task_prompt(ms(-60000)),
                    cline_api_req(ms(-50000)),
                    {"ts": ms(-40000), "type": "say", "say": "text", "text": "I will edit main.py"},
                    {"ts": ms(-30000), "type": "ask", "ask": "tool", "text": json.dumps({"tool": "editedExistingFile", "path": "main.py"})},
                ],
                history=[{"id": "1725000000001", "task": "Refactor the parser", "cwdOnTaskInitialization": "/Users/me/proj"}],
            )
            rows = fx.adapter.list_tasks(fx.host_commands())
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["status"], "waiting")
            self.assertTrue(row["needs_attention"])
            self.assertEqual(row["source"], "Cline")
            self.assertEqual(row["title"], "Refactor the parser")
            self.assertIn("VS Code", row["subtitle"])
            self.assertIn("proj", row["subtitle"])
            self.assertEqual(row["detail"]["last_message"], "ask:tool")
            self.assertEqual(row["usage"]["turns"], 1)
            self.assertEqual(row["usage"]["total_tokens"], 1200 + 300 + 800)
            self.assertEqual(row["_open"], {"type": "app-name", "target": "Visual Studio Code"})

    def test_partial_ask_is_still_streaming_not_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = ClineFixture(tmp)
            fx.write_task(
                "t2",
                [
                    cline_task_prompt(ms(-20000)),
                    cline_api_req(ms(-10000)),
                    {"ts": ms(-1000), "type": "ask", "ask": "followup", "text": "Which fi", "partial": True},
                ],
            )
            row = fx.adapter.list_tasks(fx.host_commands())[0]
            self.assertEqual(row["status"], "running")
            self.assertFalse(row["needs_attention"])

    def test_api_request_in_flight_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = ClineFixture(tmp)
            fx.write_task("t3", [cline_task_prompt(ms(-5000)), cline_api_req(ms(-2000))])
            row = fx.adapter.list_tasks(fx.host_commands())[0]
            self.assertEqual(row["status"], "running")
            self.assertTrue(row["detail"]["active_turn"])

    def test_completion_result_is_done_then_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = ClineFixture(tmp)
            fx.write_task(
                "t4",
                [
                    cline_task_prompt(ms(-30000)),
                    cline_api_req(ms(-20000)),
                    {"ts": ms(-10000), "type": "say", "say": "completion_result", "text": "All done."},
                    {"ts": ms(-9000), "type": "ask", "ask": "completion_result", "text": ""},
                ],
            )
            row = fx.adapter.list_tasks(fx.host_commands())[0]
            self.assertEqual(row["status"], "done")
            self.assertFalse(row["needs_attention"])

            old = th.CLINE_DONE_WINDOW_MS
            th.CLINE_DONE_WINDOW_MS = 1
            try:
                row = fx.adapter.list_tasks(fx.host_commands())[0]
                self.assertEqual(row["status"], "recent")
            finally:
                th.CLINE_DONE_WINDOW_MS = old

    def test_api_failure_and_error_say_are_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = ClineFixture(tmp)
            fx.write_task(
                "t5",
                [cline_task_prompt(ms(-8000)), cline_api_req(ms(-6000)), {"ts": ms(-4000), "type": "ask", "ask": "api_req_failed", "text": "429 rate limit"}],
            )
            fx.write_task(
                "t6",
                [cline_task_prompt(ms(-7000)), {"ts": ms(-3000), "type": "say", "say": "error", "text": "boom"}],
            )
            statuses = {row["detail"]["task_id"]: row["status"] for row in fx.adapter.list_tasks(fx.host_commands())}
            self.assertEqual(statuses, {"t5": "failed", "t6": "failed"})

    def test_resume_task_after_host_restart_is_parked_not_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = ClineFixture(tmp)
            fx.write_task(
                "t7",
                [cline_task_prompt(ms(-60000)), {"ts": ms(-30000), "type": "ask", "ask": "resume_task", "text": ""}],
            )
            row = fx.adapter.list_tasks(fx.host_commands())[0]
            self.assertEqual(row["status"], "recent")
            self.assertFalse(row["needs_attention"])

    def test_host_app_not_running_downgrades_wait_and_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = ClineFixture(tmp)
            fx.write_task("t8", [cline_task_prompt(ms(-5000)), {"ts": ms(-1000), "type": "ask", "ask": "command", "text": "rm -rf build"}])
            fx.write_task("t9", [cline_task_prompt(ms(-5000)), cline_api_req(ms(-1000))])
            rows = fx.adapter.list_tasks(["1 S Wed Sep 2 18:00:00 2026 /usr/sbin/cfprefsd"])
            self.assertEqual({row["status"] for row in rows}, {"recent"})
            self.assertFalse(any(row["needs_attention"] for row in rows))

    def test_stale_tasks_are_skipped_without_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = ClineFixture(tmp)
            path = fx.write_task("old", [cline_task_prompt(ms(-90 * 86400000)), {"ts": ms(-90 * 86400000), "type": "ask", "ask": "tool", "text": "{}"}])
            stale = time.time() - 90 * 86400
            os.utime(path, (stale, stale))
            self.assertEqual(fx.adapter.list_tasks(fx.host_commands()), [])
            self.assertNotIn(path, th._CLINE_TASK_CACHE)

    def test_default_stores_cover_every_host_extension_pair(self):
        stores = th.ClineAdapter._default_stores()
        self.assertEqual(len(stores), len(th.CLINE_HOSTS) * len(th.CLINE_EXTENSIONS))
        self.assertTrue(any("Cursor/User/globalStorage/rooveterinaryinc.roo-cline" in s["store_dir"] for s in stores))


# ---------------------------------------------------------------------------
# Gemini CLI / Qwen Code
# ---------------------------------------------------------------------------
class GeminiCliFixture:
    def __init__(self, tmp: str, source: str = "Gemini CLI", binaries=("gemini",)):
        self.root = os.path.join(tmp, ".gemini")
        self.project_path = "/Users/me/proj"
        self.project_hash = th.hashlib.sha256(self.project_path.encode("utf-8")).hexdigest()
        os.makedirs(os.path.join(self.root, "tmp", self.project_hash, "chats"), exist_ok=True)
        with open(os.path.join(self.root, "projects.json"), "w", encoding="utf-8") as fh:
            json.dump({"projects": {self.project_path: "proj"}}, fh)
        self.adapter = th.GeminiCliAdapter(source=source, root=self.root, binaries=binaries, exclude_process_fragments=("Gemini.app",))

    def write_session(self, name: str, messages, session_id: str = "sess-1") -> str:
        path = os.path.join(self.root, "tmp", self.project_hash, "chats", f"session-{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "sessionId": session_id,
                    "projectHash": self.project_hash,
                    "startTime": iso(-120000),
                    "lastUpdated": max((m.get("timestamp") for m in messages), default=iso()),
                    "messages": messages,
                },
                fh,
            )
        th._CLI_AGENT_SESSION_CACHE.pop(path, None)
        return path


GEMINI_PROC = ["8811 S+  Wed Sep  2 18:00:00 2026 node /Users/me/.local/npm/bin/gemini"]
NO_PROC = ["1 S Wed Sep 2 18:00:00 2026 /sbin/launchd", "77 S Wed Sep 2 18:00:00 2026 /Applications/Gemini.app/Contents/MacOS/Gemini"]


class GeminiCliAdapterTests(unittest.TestCase):
    def test_user_message_last_with_live_process_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = GeminiCliFixture(tmp)
            fx.write_session("a", [{"id": "1", "timestamp": iso(-30000), "type": "user", "content": "Add tests for utils"}])
            row = fx.adapter.list_tasks(GEMINI_PROC)[0]
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["title"], "Add tests for utils")
            self.assertEqual(row["source"], "Gemini CLI")
            self.assertEqual(row["detail"]["cwd"], "/Users/me/proj")
            self.assertIn("proj", row["subtitle"])
            self.assertTrue(row["id"].startswith("gemini-cli-"))

    def test_gemini_reply_last_is_done_with_token_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = GeminiCliFixture(tmp)
            fx.write_session(
                "b",
                [
                    {"id": "1", "timestamp": iso(-30000), "type": "user", "content": "Explain main.py"},
                    {"id": "2", "timestamp": iso(-10000), "type": "gemini", "content": "main.py does...", "tokens": {"input": 900, "output": 250, "total": 1150}},
                ],
            )
            row = fx.adapter.list_tasks(GEMINI_PROC)[0]
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["usage"]["total_tokens"], 1150)
            self.assertEqual(row["usage"]["turns"], 1)

    def test_tool_awaiting_approval_is_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = GeminiCliFixture(tmp)
            fx.write_session(
                "c",
                [
                    {"id": "1", "timestamp": iso(-30000), "type": "user", "content": "Delete the build dir"},
                    {
                        "id": "2",
                        "timestamp": iso(-5000),
                        "type": "gemini",
                        "content": "",
                        "toolCalls": [{"id": "t1", "name": "run_shell_command", "status": "awaiting_approval", "args": {"command": "rm -rf build"}}],
                    },
                ],
            )
            row = fx.adapter.list_tasks(GEMINI_PROC)[0]
            self.assertEqual(row["status"], "waiting")
            self.assertTrue(row["needs_attention"])

    def test_executing_tool_is_running_and_error_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = GeminiCliFixture(tmp)
            fx.write_session(
                "d",
                [
                    {"id": "1", "timestamp": iso(-30000), "type": "user", "content": "Run the suite"},
                    {"id": "2", "timestamp": iso(-5000), "type": "gemini", "content": "", "toolCalls": [{"id": "t1", "name": "run_shell_command", "status": "executing"}]},
                ],
                session_id="sess-d",
            )
            fx.write_session(
                "e",
                [
                    {"id": "1", "timestamp": iso(-30000), "type": "user", "content": "Something"},
                    {"id": "2", "timestamp": iso(-4000), "type": "error", "content": "API quota exceeded"},
                ],
                session_id="sess-e",
            )
            statuses = {row["detail"]["session_id"]: row["status"] for row in fx.adapter.list_tasks(GEMINI_PROC)}
            self.assertEqual(statuses, {"sess-d": "running", "sess-e": "failed"})

    def test_no_live_process_only_counts_very_recent_turns_as_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = GeminiCliFixture(tmp)
            fx.write_session("f", [{"id": "1", "timestamp": iso(-5000), "type": "user", "content": "fresh"}], session_id="fresh")
            fx.write_session(
                "g",
                [{"id": "1", "timestamp": iso(-th.CLI_AGENT_NO_PROCESS_RUNNING_STALE_MS - 60000), "type": "user", "content": "stale"}],
                session_id="stale",
            )
            statuses = {row["detail"]["session_id"]: row["status"] for row in fx.adapter.list_tasks(NO_PROC)}
            self.assertEqual(statuses["fresh"], "running")
            self.assertEqual(statuses["stale"], "recent")

    def test_qwen_code_variant_uses_its_own_root_and_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = GeminiCliFixture(tmp, source="Qwen Code", binaries=("qwen",))
            fx.write_session("q", [{"id": "1", "timestamp": iso(-3000), "type": "user", "content": "port to rust"}])
            rows = fx.adapter.list_tasks(["9 S Wed Sep 2 18:00:00 2026 node /opt/homebrew/bin/qwen"])
            self.assertEqual(rows[0]["source"], "Qwen Code")
            self.assertEqual(rows[0]["status"], "running")
            self.assertTrue(rows[0]["id"].startswith("qwen-code-"))

    def test_cli_process_matching_ignores_desktop_app_and_partial_words(self):
        self.assertTrue(th.cli_process_running(GEMINI_PROC, ("gemini",), ("Gemini.app",)))
        self.assertFalse(th.cli_process_running(NO_PROC, ("gemini",), ("Gemini.app",)))
        self.assertFalse(th.cli_process_running(["5 S x node /x/geminiscope"], ("gemini",)))
        self.assertTrue(th.cli_process_running(["5 S x /usr/local/bin/gemini --yolo"], ("gemini",)))


# ---------------------------------------------------------------------------
# GitHub Copilot CLI
# ---------------------------------------------------------------------------
class CopilotFixture:
    def __init__(self, tmp: str):
        self.root = os.path.join(tmp, ".copilot")
        self.adapter = th.CopilotCliAdapter(root=self.root)

    def write_session(self, session_id: str, events, workspace: str = "") -> str:
        session_dir = os.path.join(self.root, "session-state", session_id)
        os.makedirs(session_dir, exist_ok=True)
        path = os.path.join(session_dir, "events.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
        if workspace:
            with open(os.path.join(session_dir, "workspace.yaml"), "w", encoding="utf-8") as fh:
                fh.write(workspace)
        th._CLI_AGENT_SESSION_CACHE.pop(path, None)
        return path


COPILOT_PROC = ["4400 S+  Wed Sep  2 18:00:00 2026 node /Users/me/.local/npm/bin/copilot"]
VSCODE_COPILOT_ONLY = ["4401 S  Wed Sep  2 18:00:00 2026 /Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper.app/Contents/MacOS/Code Helper --extensionId github.copilot-chat"]


def ev(kind: str, offset_ms: int, data=None):
    return {"type": kind, "timestamp": iso(offset_ms), "data": data or {}}


class CopilotCliAdapterTests(unittest.TestCase):
    def test_turn_in_progress_is_running_with_cwd_from_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = CopilotFixture(tmp)
            fx.write_session(
                "s1",
                [
                    ev("session.start", -60000, {"sessionId": "s1"}),
                    ev("user.message", -30000, {"content": "Fix the failing CI job"}),
                    ev("assistant.turn_start", -29000),
                    ev("tool.execution_start", -20000, {"toolName": "bash"}),
                ],
                workspace="id: s1\ncwd: /Users/me/ci-repo\nsummary: Fix CI\n",
            )
            row = fx.adapter.list_tasks(COPILOT_PROC)[0]
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["title"], "Fix CI")
            self.assertEqual(row["detail"]["cwd"], "/Users/me/ci-repo")
            self.assertIn("ci-repo", row["subtitle"])
            self.assertEqual(row["source"], "Copilot CLI")

    def test_turn_end_is_done_and_shutdown_is_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = CopilotFixture(tmp)
            fx.write_session(
                "done",
                [ev("user.message", -30000, {"content": "hi"}), ev("assistant.message", -20000, {"content": "hello"}), ev("assistant.turn_end", -19000)],
            )
            fx.write_session(
                "closed",
                [ev("user.message", -30000, {"content": "hi"}), ev("assistant.turn_end", -20000), ev("session.shutdown", -10000)],
            )
            statuses = {row["detail"]["session_id"]: row["status"] for row in fx.adapter.list_tasks(COPILOT_PROC)}
            self.assertEqual(statuses, {"done": "done", "closed": "recent"})

    def test_pending_permission_event_is_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = CopilotFixture(tmp)
            fx.write_session(
                "perm",
                [ev("user.message", -30000, {"content": "deploy"}), ev("permission.requested", -5000, {"tool": "bash"})],
            )
            row = fx.adapter.list_tasks(COPILOT_PROC)[0]
            self.assertEqual(row["status"], "waiting")
            self.assertTrue(row["needs_attention"])

    def test_error_event_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = CopilotFixture(tmp)
            fx.write_session("err", [ev("user.message", -30000, {"content": "x"}), ev("session.error", -5000, {"message": "401"})])
            self.assertEqual(fx.adapter.list_tasks(COPILOT_PROC)[0]["status"], "failed")

    def test_vscode_copilot_extension_is_not_the_cli_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = CopilotFixture(tmp)
            fx.write_session(
                "old-turn",
                [ev("user.message", -th.CLI_AGENT_NO_PROCESS_RUNNING_STALE_MS - 30000, {"content": "x"})],
            )
            row = fx.adapter.list_tasks(VSCODE_COPILOT_ONLY)[0]
            self.assertEqual(row["status"], "recent")
            self.assertFalse(row["detail"]["process_running"])

    def test_missing_root_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(th.CopilotCliAdapter(root=os.path.join(tmp, "nope")).list_tasks(COPILOT_PROC), [])


if __name__ == "__main__":
    unittest.main()
