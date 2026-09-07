from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import usage_sources


class SourceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_discovers_harness_structure_independent_of_task_names_and_depth(self):
        shared = self.root / "shared"
        workspace = self.root / "workspaces"
        homes = [workspace / "project" / "ticket-22" / "codex-home",
                 workspace / "profile" / "group" / "renamed" / "codex-home"]
        expected = []
        for home in homes:
            for tree in ["sessions", "archived_sessions"]:
                path = home / tree
                path.mkdir(parents=True)
                (path / "rollout-same.jsonl").write_text("{}\n")
                expected.append(path)
        # A cached copy inside a discovered home is not another source.
        nested = homes[0] / "cache" / "copied" / "codex-home" / "sessions"
        nested.mkdir(parents=True)
        roots = usage_sources.multica_codex_session_roots(shared, workspace)
        self.assertCountEqual(roots, expected)
        self.assertEqual(len(usage_sources._codex_session_files(roots)), 1)

    def test_missing_explicit_root_is_a_failure_not_an_idle_day(self):
        with self.assertRaisesRegex(ValueError, "configured Multica workspace root is missing"):
            usage_sources.multica_codex_session_roots(self.root / "shared", self.root / "missing")

    def test_unconfigured_source_can_be_absent(self):
        self.assertEqual(usage_sources.multica_codex_session_roots(self.root / "shared", None), [])

    def test_summary_reports_empty_and_live_sources_without_private_names(self):
        self.assertIn("status=empty", usage_sources.codex_source_summary([]))
        sessions = self.root / "private-project"
        sessions.mkdir()
        (sessions / "private-session.jsonl").write_text("{}\n")
        result = usage_sources.codex_source_summary([sessions])
        self.assertIn("files=1", result)
        self.assertNotIn("private", result)
        self.assertNotIn(str(self.root), result)
