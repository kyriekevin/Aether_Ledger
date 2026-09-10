from __future__ import annotations
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from dashboard_story import aggregate_story, dispatch_groups, model_groups, render_dispatch, render_model_matrix
from render_dashboard import AGENT_BUCKETS, discover_agent_files, generate_matrix


class WorkflowStoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, relative, data):
        path = self.root / "data" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def story(self):
        return aggregate_story(self.root, discover_agent_files(self.root), date(2026, 9, 6), AGENT_BUCKETS)

    def test_window_boundaries_aliases_and_legacy_contexts(self):
        self.write("work/codex.json", {d: {"totalTokens": n} for d, n in [
            ("2026-07-12", 1000), ("2026-07-13", 2), ("2026-08-09", 3),
            ("2026-08-10", 5), ("2026-09-06", 7), ("2026-09-07", 2000)]})
        self.write("work/codex-multica.json", {"2026-09-06": {"totalTokens": 11}})
        self.write("personal/dsh.json", {"2026-09-06": {"totalTokens": 13}})
        self.write("devbox/codex.json", {"2026-09-06": {"totalTokens": 17}})
        self.write("trail/node-000000000001/claude.json", {"2026-09-06": {"totalTokens": 19}})
        self.write("work/codex_by_repo.json", {"2026-09-06": {"totalTokens": 9999}})
        story = self.story()
        work = story.contexts["work"]
        self.assertEqual(work.previous, 5)
        self.assertEqual(work.current, 23)
        self.assertEqual(work.harnesses, {"codex": 23})
        self.assertEqual(work.weekly, [2, 0, 0, 3, 5, 0, 0, 18])
        self.assertEqual(story.contexts["personal"].current, 13)
        self.assertEqual(story.historical_context_tokens, 36)

    def test_retains_all_models_and_explicit_missing_attribution(self):
        self.write("work/claude.json", {"2026-09-05": {"totalTokens": 100,
            "models": {"model-a": {"totalTokens": 60}, "model-b": {"totalTokens": 30}}}})
        self.write("personal/codex.json", {"2026-09-05": {"totalTokens": 20,
            "models": {"model-a": {"totalTokens": 20}}}})
        story = self.story()
        self.assertEqual(story.contexts["work"].models["claude"]["Unattributed"], 10)
        self.assertEqual(story.contexts["personal"].models["codex"]["model-a"], 20)
        self.assertEqual(story.contexts["work"].weekly_models["model-a"][-1], 60)
        for locale in ["en", "zh"]:
            svg = render_model_matrix(story, locale)
            ET.fromstring(svg)
            self.assertIn("model-a", svg)
            self.assertIn("model-b", svg)
            self.assertIn("Unattributed" if locale == "en" else "未归因", svg)

    def test_empty_data_is_renderable_and_localized(self):
        for locale in ["en", "zh"]:
            ET.fromstring(render_model_matrix(self.story(), locale))
            ET.fromstring(render_dispatch(None, locale))
        self.assertIn("暂无记录", render_dispatch(None, "zh"))

    def test_grouped_usage_preserves_previous_only_models_and_harness_attribution(self):
        self.write("work/codex.json", {"2026-08-01": {"totalTokens": 50, "models": {"retired": {"totalTokens": 50}}}})
        self.write("personal/dsh.json", {"2026-09-05": {"totalTokens": 30, "models": {"current": {"totalTokens": 30}}}})
        story = self.story()
        self.assertEqual(story.contexts["work"].previous_models["codex"]["retired"], 50)
        self.assertEqual(model_groups(story), [("codex", ["retired"]), ("dsh", ["current"])])
        doc = ET.fromstring(render_model_matrix(story))
        labels = [node.text for node in doc.findall(".//{http://www.w3.org/2000/svg}text")]
        self.assertEqual(labels.count("Codex"), 1)
        self.assertEqual(labels.count("DSH"), 1)
        self.assertNotIn("Claude Code", labels)
        self.assertIn("-100%", labels)
        self.assertIn("New", labels)

    def test_dispatch_merges_harness_and_roles_and_hides_zero_counts(self):
        def row(role, model, count):
            return {"role": role, "harness": "codex", "model": model, "effort": "high",
                    "agents": 1, "archivedAgents": 0, "issues": {"done": count}}
        snapshot = {"asOf": "2026-09-09", "coveredRoles": ["work", "personal"], "unassignedIssues": 0,
                    "configurations": [row("work", "shared", 3), row("personal", "shared", 2), row("work", "unused", 0)]}
        self.assertEqual(dispatch_groups(snapshot), {"codex": {("shared", "high"): {"work": 3, "personal": 2}}})
        svg = render_dispatch(snapshot)
        labels = [node.text for node in ET.fromstring(svg).findall(".//{http://www.w3.org/2000/svg}text")]
        self.assertEqual(labels.count("Codex"), 1)
        self.assertEqual(labels.count("shared"), 1)
        self.assertNotIn("unused", svg)
        self.assertNotIn("Unassigned / unmapped", svg)
        self.assertIn("5", labels)
        snapshot["unassignedIssues"] = 2
        self.assertIn("Unassigned / unmapped: 2", render_dispatch(snapshot))

    def test_generation_freshness_and_locale_are_checked(self):
        self.write("work/codex.json", {"2026-09-06": {"totalTokens": 10}})
        output = self.root / "assets" / "model-matrix.svg"
        self.assertTrue(generate_matrix(self.root, output, date(2026, 9, 6)))
        self.assertFalse(generate_matrix(self.root, output, date(2026, 9, 6), check=True))
        before = output.read_text()
        self.assertFalse(generate_matrix(self.root, output, date(2026, 9, 7), check=True))
        self.assertTrue(generate_matrix(self.root, output, date(2026, 9, 6), check=True, locale="zh"))
        self.assertEqual(output.read_text(), before)
