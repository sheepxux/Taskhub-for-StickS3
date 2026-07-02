import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import task_hub as th  # noqa: E402


class DiagnosticsHelpers(unittest.TestCase):
    def test_task_summary_normalizes_status_aliases(self):
        tasks = [
            {"status": "in_progress"},
            {"status": "needs_input"},
            {"status": "completed"},
            {"status": "offline"},
            {"status": "error", "needs_attention": True},
        ]

        summary = th.diagnostic_task_summary(tasks)

        self.assertEqual(summary["count"], 5)
        self.assertEqual(summary["active"], 3)
        self.assertEqual(summary["attention"], 2)
        self.assertEqual(summary["statuses"]["running"], 1)
        self.assertEqual(summary["statuses"]["waiting"], 1)
        self.assertEqual(summary["statuses"]["done"], 1)
        self.assertEqual(summary["statuses"]["idle"], 1)
        self.assertEqual(summary["statuses"]["failed"], 1)

    def test_display_path_redacts_home_directory(self):
        home = os.path.expanduser("~")
        self.assertEqual(th.display_path(os.path.join(home, "Library", "TaskHub")), "~/Library/TaskHub")
        self.assertEqual(th.display_path(home), "~")
        self.assertEqual(th.display_path("/tmp/taskhub"), "/tmp/taskhub")

    def test_diagnostics_snapshot_omits_token_and_task_titles(self):
        class FakeAdapter:
            source = "Fake"

            def list_tasks(self):
                return [
                    th.task(
                        task_id="fake-1",
                        source="Fake",
                        title="secret user task title",
                        status="running",
                    )
                ]

        old_voice = th.taskhub_voice
        th.taskhub_voice = None
        try:
            hub = th.Hub(token="super-secret-token", http_port=5577, discovery_port=5578, bind="127.0.0.1")
            hub.adapters = [FakeAdapter()]
            hub.peer_manager.snapshot = lambda refresh=False: {  # type: ignore[method-assign]
                "enabled": False,
                "discovery_count": 0,
                "discovery_duration_ms": 0,
                "peers": [],
            }

            snapshot = hub.diagnostics_snapshot(refresh_peers=False)
            encoded = json.dumps(snapshot, ensure_ascii=False)

            self.assertIn('"token_length": 18', encoded)
            self.assertNotIn("super-secret-token", encoded)
            self.assertNotIn("secret user task title", encoded)
            self.assertFalse(snapshot["auth"]["default_token_active"])
            self.assertEqual(snapshot["adapters"][0]["count"], 1)
            self.assertEqual(snapshot["adapters"][0]["statuses"], {"running": 1})
        finally:
            th.taskhub_voice = old_voice

    def test_stick_fast_path_returns_stale_cache_while_refreshing(self):
        class FakeAdapter:
            source = "Fake"

            def __init__(self):
                self.title = "old task"
                self.delay = 0.0

            def list_tasks(self):
                if self.delay:
                    time.sleep(self.delay)
                return [
                    th.task(
                        task_id="fake-1",
                        source="Fake",
                        title=self.title,
                        status="running",
                    )
                ]

        hub = th.Hub(token="test-token", http_port=5577, discovery_port=5578, bind="127.0.0.1")
        adapter = FakeAdapter()
        hub.adapters = [adapter]
        hub.peer_manager.remote_tasks = lambda: []  # type: ignore[method-assign]

        rows = hub.list_tasks(include_remote=True)
        self.assertEqual(rows[0]["title"], "old task")

        adapter.title = "new task"
        adapter.delay = 0.2
        with hub.cache_lock:
            hub.cache_at = th.now_ms() - th.TASK_CACHE_MS - 1

        started = time.monotonic()
        rows = hub.list_tasks(include_remote=True, allow_stale=True, stale_ms=60000)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(rows[0]["title"], "old task")

        deadline = time.monotonic() + 2.0
        refreshed_title = ""
        while time.monotonic() < deadline:
            with hub.cache_lock:
                refreshed_title = hub.cache[0]["title"] if hub.cache else ""
            if refreshed_title == "new task":
                break
            time.sleep(0.05)
        self.assertEqual(refreshed_title, "new task")


if __name__ == "__main__":
    unittest.main()
