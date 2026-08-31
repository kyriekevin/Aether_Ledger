from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import multica_usage  # noqa: E402


class MulticaUsageTests(unittest.TestCase):
    def test_collects_usage_and_terminal_tasks_without_identity_fields(self) -> None:
        runtime_id = "runtime-private-id"
        issue_id = "issue-private-id"

        def fake_run(args: list[str]) -> object:
            if args == ["runtime", "list"]:
                return [{
                    "id": runtime_id,
                    "custom_name": "laptop-alias",
                    "provider": "codex",
                    "device_info": "private-host",
                }]
            if args[:3] == ["runtime", "usage", runtime_id]:
                return [{
                    "date": "2026-08-31",
                    "model": "gpt-5.6-sol",
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": 300,
                    "cache_write_tokens": 5,
                }]
            if args == ["issue", "list", "--limit", "100", "--offset", "0"]:
                return {
                    "issues": [{"id": issue_id, "title": "private prompt"}],
                    "has_more": False,
                }
            if args == ["issue", "runs", issue_id]:
                return [{
                    "id": "task-private-id",
                    "runtime_id": runtime_id,
                    "status": "completed",
                    "started_at": "2026-08-31T01:00:00Z",
                    "completed_at": "2026-08-31T01:02:30Z",
                    "usage": [{"input_tokens": 100}],
                    "trigger_summary": "private prompt",
                    "work_dir": "/private/path",
                }]
            self.fail(f"unexpected command: {args}")

        snapshot = multica_usage.collect_snapshot(
            {"laptop-alias": "work"}, run_json=fake_run
        )
        day = snapshot["2026-08-31"]
        usage = day["usage"]["work"]["codex"]
        self.assertEqual(usage["totalTokens"], 425)
        self.assertEqual(usage["models"]["gpt-5.6-sol"]["cacheReadTokens"], 300)
        self.assertGreater(usage["totalCost"], 0)
        self.assertEqual(day["tasks"]["work"]["codex"], {
            "total": 1,
            "completed": 1,
            "failed": 0,
            "cancelled": 0,
            "withUsage": 1,
            "durationSeconds": 150,
        })
        encoded = json.dumps(snapshot)
        for private in (runtime_id, issue_id, "task-private-id", "private prompt", "/private/path"):
            self.assertNotIn(private, encoded)

    def test_ignores_unmapped_runtimes_and_running_tasks(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str]) -> object:
            calls.append(args)
            if args == ["runtime", "list"]:
                return [{"id": "hidden", "custom_name": "other", "provider": "codex"}]
            if args[:2] == ["issue", "list"]:
                return []
            self.fail(f"unmapped runtime should not be queried: {args}")

        with self.assertRaisesRegex(ValueError, "no Multica runtimes matched"):
            multica_usage.collect_snapshot({"mapped": "work"}, run_json=fake_run)
        self.assertFalse(any(args[:2] == ["runtime", "usage"] for args in calls))

    def test_runtime_role_config_is_explicit_and_public(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            path.write_text(json.dumps({"laptop-alias": "work", "box": "devbox"}))
            self.assertEqual(
                multica_usage.load_runtime_roles(path),
                {"laptop-alias": "work", "box": "devbox"},
            )
            path.write_text(json.dumps({"laptop-alias": "secret-host"}))
            with self.assertRaisesRegex(ValueError, "must map"):
                multica_usage.load_runtime_roles(path)

    def test_write_snapshot_is_date_keyed_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "multica.json"
            snapshot = {"2026-08-31": {"usage": {}, "tasks": {}}}
            multica_usage.write_snapshot(snapshot, path)
            self.assertEqual(json.loads(path.read_text()), snapshot)
            multica_usage.write_snapshot(
                {"2026-09-01": {"usage": {}, "tasks": {}}}, path
            )
            self.assertEqual(
                set(json.loads(path.read_text())), {"2026-08-31", "2026-09-01"}
            )


if __name__ == "__main__":
    unittest.main()
