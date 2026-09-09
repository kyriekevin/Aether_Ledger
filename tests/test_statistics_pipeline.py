from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_public
import collect_statistics
import statistics_readers as readers
from statistics_store import Journal
from statistics_multica import collect_runs
from usage_schema import SHANGHAI


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.machine = self.root / "data/work"
        self.machine.mkdir(parents=True)
        self.cache = self.root / "private"

    def test_preview_style_collection_publishes_no_identity_and_preserves_token_store(self):
        store = self.machine / "codex.json"
        store.write_text('{"2026-09-10":{"totalTokens":100,"totalCost":0}}')
        before = store.read_bytes()
        reading = readers.Reading(present=True)
        reading.add("codex", "private-session", "private-response", "2026-09-10T01:00:00Z", "gpt-5.5", "high", "standard",
                    {"inputTokens": 80, "outputTokens": 10, "cacheReadTokens": 10, "cacheCreationTokens": 0})
        with patch.object(readers, "codex", side_effect=[reading, readers.Reading(), readers.Reading()]), \
             patch.object(readers, "claude", return_value=readers.Reading()), \
             patch.object(readers, "dsh", return_value=readers.Reading()), \
             patch.object(collect_statistics.usage_sources, "multica_codex_session_roots", return_value=[]), \
             patch.object(collect_statistics.usage_sources, "multica_dsh_session_roots", return_value=[]):
            result = collect_statistics.collect(self.machine, cache_root=self.cache, now=datetime(2026, 9, 10, 18, tzinfo=SHANGHAI), with_multica=False)
        self.assertEqual(before, store.read_bytes())
        self.assertNotIn("private-session", json.dumps(result))
        self.assertNotIn("private-response", json.dumps(result))
        self.assertEqual(result["sources"]["codex"]["days"]["2026-09-10"]["totals"]["calls"], 1)
        self.assertIsNone(result["sources"]["codex"]["metrics"]["modelEffort"]["effectiveFrom"])
        self.assertEqual((self.cache / "work.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_profile_discovery_does_not_bind_production_config(self):
        home = self.root / "multica"
        sessions = home / "profiles/example/dsh-sessions"
        sessions.mkdir(parents=True)
        binding = self.root / "real-binding"
        with patch.object(collect_statistics.usage_sources, "MULTICA_DSH_PROFILE_FILE", binding), \
             patch.object(collect_statistics.usage_sources, "MULTICA_HOME", home), \
             patch.dict(os.environ, {"MULTICA_DSH_PROFILE": "example"}):
            roots = collect_statistics.multica_dsh_roots_readonly()
        self.assertEqual(roots, (sessions,))
        self.assertFalse(binding.exists())

    def test_missing_journal_cannot_overwrite_published_statistics(self):
        published = self.machine / "statistics.json"
        published.write_text('{}')
        with self.assertRaises(collect_statistics.MissingStatisticsJournal):
            collect_statistics.collect(self.machine, cache_root=self.cache, with_multica=False)
        self.assertEqual(published.read_text(), '{}')

    def test_empty_journal_cannot_overwrite_published_statistics(self):
        published = self.machine / "statistics.json"
        published.write_text('{}')
        journal = Journal(self.cache / "work.sqlite3")
        journal.close()
        with self.assertRaises(collect_statistics.MissingStatisticsJournal):
            collect_statistics.collect(self.machine, cache_root=self.cache, with_multica=False)
        self.assertEqual(published.read_text(), '{}')

    def test_public_audit_rejects_private_journal_and_invalid_statistics(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "config").mkdir()
        (self.root / "config/statistics-models.json").write_text('{"version":1,"models":[]}')
        (self.machine / "statistics.json").write_text('{"session_id":"private"}')
        (self.root / "journal.sqlite3").write_bytes(b'SQLite format 3\x00')
        errors = audit_public.audit_tree(self.root)
        self.assertTrue(any("invalid public statistics" in e for e in errors))
        self.assertTrue(any("private measurement journals" in e for e in errors))

    def test_cross_day_run_migration_publishes_and_lost_journal_still_fails(self):
        run = {"id": "run-1", "runtime_id": "runtime", "status": "queued",
               "created_at": "2026-09-10T09:00:00+08:00"}
        def api(args):
            if args[:2] == ["runtime", "list"]:
                return [{"id": "runtime", "provider": "codex", "custom_name": "work-device"}]
            if args[:2] == ["issue", "list"]:
                return {"issues": [{"id": "issue"}], "has_more": False}
            if args[:2] == ["agent", "list"]:
                return []
            return [run]
        def collect_api(journal, role, roles, now, **kwargs):
            collect_runs(journal, role, {"work-device": "work"}, now, api, **kwargs)
        config = self.root / "multica.json"
        config.write_text('{}')
        with ExitStack() as stack:
            for name in ("codex", "claude", "dsh"):
                stack.enter_context(patch.object(readers, name, return_value=readers.Reading()))
            stack.enter_context(patch.object(collect_statistics, "multica_dsh_roots_readonly", return_value=[]))
            stack.enter_context(patch.object(collect_statistics.usage_sources, "multica_codex_session_roots", return_value=[]))
            stack.enter_context(patch.object(collect_statistics.usage_sources, "dsh_session_roots", return_value=[]))
            stack.enter_context(patch.object(collect_statistics.multica_usage, "CONFIG_FILE", config))
            stack.enter_context(patch.object(collect_statistics.multica_usage, "load_runtime_roles", return_value={}))
            stack.enter_context(patch.object(collect_statistics, "collect_runs", side_effect=collect_api))
            def collect(day):
                return collect_statistics.collect(self.machine, cache_root=self.cache,
                    now=datetime(2026, 9, day, 18, tzinfo=SHANGHAI))
            first = collect(10)
            self.assertEqual(first["multica"]["days"]["2026-09-10"]["total"], 1)
            run.update(status="running", started_at="2026-09-11T09:00:00+08:00")
            second = collect(11)
            self.assertEqual(second["multica"]["days"]["2026-09-10"]["total"], 0)
            self.assertEqual(second["multica"]["days"]["2026-09-11"]["total"], 1)
            collect(12)  # Publication remains healthy on the next scan.
            published = (self.machine / "statistics.json").read_bytes()
            journal = Journal(self.cache / "work.sqlite3")
            with journal.db:
                journal.db.execute("DELETE FROM runs")
            journal.close()
            with self.assertRaises(collect_statistics.StatisticsRegression):
                collect(13)  # API still has a run; fetching must not mask journal loss.
            self.assertEqual((self.machine / "statistics.json").read_bytes(), published)
