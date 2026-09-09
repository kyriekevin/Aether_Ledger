from __future__ import annotations
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from multica_dispatch import collect_dispatch, configuration, issue_state, validate_snapshot
from dashboard_story import render_dispatch


class DispatchTests(unittest.TestCase):
    models = {"gpt-5.5", "claude-opus-5", "claude-opus-4-8", "deepseek-v4-pro", "default", "unknown"}

    def test_embedded_effort_vendor_prefix_and_context_suffix(self):
        for raw, effort, expected in [
            ("gpt-5.5?model_reasoning_effort=medium", "", ("gpt-5.5", "medium")),
            ("openrouter-3o?model_reasoning_effort=xhigh", "", ("claude-opus-4-8", "xhigh")),
            ("opencode-go/DeepSeek-V4-Pro", "high", ("deepseek-v4-pro", "high")),
            ("claude-opus-5[1m]", "xhigh", ("claude-opus-5", "xhigh")),
            ("gpt-5.5?model_reasoning_effort=high", "low", ("gpt-5.5", "unknown")),
            ("private-custom-model", "private-effort", ("unknown", "unknown")),
            ("", "", ("default", "default")),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(configuration({"model": raw, "thinking_level": effort}, self.models), expected)

    def test_archive_status_survives_missing_category(self):
        self.assertEqual(issue_state({"status": "archived", "status_category": None}), "archived")
        self.assertEqual(issue_state({"status": "custom-status", "status_category": "done"}), "done")
        self.assertEqual(issue_state({"status": "custom-status"}), "unknown")

    def snapshot(self):
        issue = {"id": "private-issue", "assignee_type": "agent", "assignee_id": "private-agent", "status_category": "done", "title": "private-title"}
        def read(args):
            if args[:2] == ["runtime", "list"]:
                return [{"id": "private-runtime", "provider": "codex", "custom_name": "local-runtime"}]
            if args[:2] == ["agent", "list"]:
                agent = {"id": "private-agent", "runtime_id": "private-runtime", "model": "gpt-5.5", "thinking_level": "medium", "instructions": "private-prompt"}
                return [agent, agent, dict(agent, id="private-archived", archived_at="2026-08-01")]
            if args[-1] == "0":
                return {"issues": [issue], "has_more": True}
            return {"issues": [issue, dict(issue, id="unassigned-issue", assignee_type="user")], "has_more": False}
        return collect_dispatch({"local-runtime": "work"}, run_json=read, allowed_models=self.models, as_of="2026-09-09")

    def test_deduplication_current_assignment_and_no_private_fields(self):
        snapshot = self.snapshot()
        validate_snapshot(snapshot, self.models)
        self.assertEqual(snapshot["totalIssues"], 2)
        self.assertEqual(snapshot["unassignedIssues"], 1)
        self.assertEqual(snapshot["coveredRoles"], ["work"])
        row = snapshot["configurations"][0]
        self.assertEqual(row["agents"], 1)
        self.assertEqual(row["archivedAgents"], 1)
        self.assertEqual(row["issues"]["done"], 1)
        self.assertNotIn("private", json.dumps(snapshot))
        self.assertIn("Work", render_dispatch(snapshot))

    def test_public_schema_rejects_identity_and_nonconserving_counts(self):
        baseline = self.snapshot()
        mutations = [lambda s: s.update(prompt="secret"),
                     lambda s: s["configurations"][0].update(model="private-name"),
                     lambda s: s["configurations"][0].update(agent_id="secret"),
                     lambda s: s["configurations"][0]["issues"].update(done=3),
                     lambda s: s["configurations"].append(copy.deepcopy(s["configurations"][0])),
                     lambda s: s.update(totalIssues=True)]
        for mutate in mutations:
            snapshot = copy.deepcopy(baseline)
            mutate(snapshot)
            with self.assertRaises(ValueError):
                validate_snapshot(snapshot, self.models)
