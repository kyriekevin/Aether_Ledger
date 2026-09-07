from __future__ import annotations

import json
import subprocess
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

    def test_follows_linked_profiles_tasks_and_harness_homes(self):
        for linked_component in ("profile", "task", "codex-home"):
            with self.subTest(linked_component=linked_component):
                case = self.root / linked_component
                workspace = case / "workspaces"
                workspace.mkdir(parents=True)
                target = case / "external"
                remaining = {
                    "profile": "task-demo/codex-home",
                    "task": "codex-home",
                    "codex-home": ".",
                }[linked_component]
                home = target / remaining
                for tree in ("sessions", "archived_sessions"):
                    (home / tree).mkdir(parents=True)
                    (home / tree / f"rollout-{tree}.jsonl").write_text("{}\n")
                link = workspace / {"profile": "profile-demo", "task": "task-demo",
                                    "codex-home": "codex-home"}[linked_component]
                link.symlink_to(target, target_is_directory=True)
                roots = usage_sources.multica_codex_session_roots(case / "absent-shared", workspace)
                self.assertEqual({path.resolve() for path in roots},
                                 {(home / tree).resolve() for tree in ("sessions", "archived_sessions")})
                self.assertEqual(len(usage_sources._codex_session_files(roots)), 2)

    def test_link_cycles_terminate_and_aliases_do_not_duplicate_sources(self):
        workspace = self.root / "workspaces"
        home = workspace / "task-real" / "codex-home"
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "rollout-example.jsonl").write_text("{}\n")
        (workspace / "task-alias").symlink_to(home.parent, target_is_directory=True)
        (workspace / "task-real" / "back-to-workspaces").symlink_to(workspace, target_is_directory=True)
        # A separate process makes nontermination a bounded test failure.
        program = (
            "import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "import usage_sources as s; "
            "roots=s.multica_codex_session_roots(Path(sys.argv[2])/'absent', Path(sys.argv[2])); "
            "print(json.dumps([len(roots), len(s._codex_session_files(roots))]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", program, str(Path(usage_sources.__file__).parent), str(workspace)],
            capture_output=True, text=True, check=True, timeout=5,
        )
        self.assertEqual(json.loads(result.stdout), [1, 1])

    def test_earlier_alias_does_not_hide_a_named_harness_home(self):
        workspace = self.root / "workspaces"
        workspace.mkdir()
        target = self.root / "external-home"
        (target / "sessions").mkdir(parents=True)
        (workspace / "a-alias").symlink_to(target, target_is_directory=True)
        (workspace / "codex-home").symlink_to(target, target_is_directory=True)
        roots = usage_sources.multica_codex_session_roots(self.root / "shared", workspace)
        self.assertEqual([path.resolve() for path in roots], [(target / "sessions").resolve()])

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
