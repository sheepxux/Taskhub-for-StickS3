import json
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
