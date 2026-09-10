from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import multica_usage
import sync_usage
import usage_ccusage
import usage_dsh
import usage_git
import usage_schema
import usage_sources
import usage_telemetry


def row(tokens):
    return [{"date": "2026-09-07", "totalTokens": tokens, "totalCost": 0,
             "models": {"gpt-5.5": {"totalTokens": tokens}}, "costSource": "unpriced"}]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.activity = self.stack.enter_context(patch.object(sync_usage.issue_activity, "collect", return_value=None))
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(usage_schema, "DATA_REPO_DIR", self.root))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.unified = self.stack.enter_context(patch.object(
            usage_ccusage, "fetch_daily_since", return_value=(row(11), row(22), row(33))))
        self.multica = self.stack.enter_context(patch.object(
            usage_ccusage, "fetch_multica_codex_daily", return_value=row(44)))
        self.stack.enter_context(patch.object(usage_sources, "multica_codex_session_roots", return_value=[]))
        self.stack.enter_context(patch.object(usage_sources, "multica_dsh_session_roots", return_value=[]))
        self.stack.enter_context(patch.object(usage_ccusage, "fetch_codex_home_daily", return_value=row(55)))
        self.stack.enter_context(patch.object(
            usage_dsh, "collect_dsh_daily_since", side_effect=lambda since, roots=None: row(66 if roots is None else 77)))
        self.telemetry = self.stack.enter_context(patch.object(
            usage_telemetry, "collect_codex_routing_since", return_value={}))
        self.stack.enter_context(patch.object(usage_telemetry, "collect_claude_routing_since", return_value={}))
        self.tasks = self.stack.enter_context(patch.object(multica_usage, "collect_if_configured", return_value=True))
        self.stack.enter_context(patch.object(usage_git, "_current_branch", return_value="usage/2026-09-07"))
        self.stack.enter_context(patch.object(usage_git, "prepare_daily_branch", return_value=True))
        self.stack.enter_context(patch.object(usage_git, "git_catch_up", return_value=True))
        self.push = self.stack.enter_context(patch.object(usage_git, "git_push"))

    def stored(self, name):
        path = self.root / "data/work" / usage_schema.AGENT_STORES[name]
        return json.loads(path.read_text())

    def test_statistics_failure_happens_after_token_publication(self):
        order = []
        self.push.side_effect = lambda _: order.append("tokens")
        def fail(*args, **kwargs):
            order.append("statistics")
            raise ValueError("private details")
        with patch.object(sync_usage.collect_statistics, "collect", side_effect=fail), redirect_stderr(io.StringIO()) as errors:
            result = sync_usage._sync("data/work", no_push=False, include_statistics=True)
        self.assertEqual(result, 1)
        self.assertEqual(order, ["tokens", "statistics"])
        self.assertNotIn("private details", errors.getvalue())
        self.assertEqual(self.stored("codex")["2026-09-07"]["totalTokens"], 22)

    def test_personal_statistics_use_personal_directory(self):
        with patch.object(sync_usage.collect_statistics, "collect", return_value={"sources": {}, "multica": None}) as collect:
            result = sync_usage._sync("data/personal", no_push=False, include_statistics=True)
        self.assertEqual(result, 0)
        collect.assert_called_once_with(self.root / "data/personal")
        self.assertEqual(self.push.call_count, 2)

    def test_activity_failure_does_not_block_token_or_statistics_publication(self):
        self.activity.side_effect = ValueError("private issue content")
        with patch.object(sync_usage.collect_statistics, "collect", return_value={"sources": {}, "multica": None}), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(sync_usage._sync("data/work", no_push=False, include_statistics=True), 1)
        self.assertEqual(self.push.call_count, 2)
        self.assertNotIn("private issue content", errors.getvalue())

    def test_each_reader_reaches_its_own_store(self):
        self.assertEqual(sync_usage._sync("data/work", no_push=True), 0)
        for name, tokens in {"claude": 11, "codex": 22, "opencode": 33,
                             "codex-multica": 44, "traex": 55, "dsh": 66, "dsh-multica": 77}.items():
            self.assertEqual(self.stored(name)["2026-09-07"]["totalTokens"], tokens)
        self.tasks.assert_not_called()
        self.push.assert_not_called()

    def test_failed_source_keeps_history_and_other_sources_publish(self):
        sync_usage._sync("data/work", no_push=True)
        self.multica.side_effect = OSError("private path must not enter diagnostics")
        self.unified.return_value = (row(12), row(23), row(34))
        self.assertEqual(sync_usage._sync("data/work", no_push=False), 1)
        self.assertEqual(self.stored("codex-multica")["2026-09-07"]["totalTokens"], 44)
        self.assertEqual(self.stored("codex")["2026-09-07"]["totalTokens"], 23)
        self.push.assert_called_once_with("data/work")

    def test_optional_telemetry_cannot_discard_tokens(self):
        self.telemetry.side_effect = ValueError("bad event")
        self.assertEqual(sync_usage._sync("data/work", no_push=False), 1)
        self.assertEqual(self.stored("codex")["2026-09-07"]["totalTokens"], 22)
        self.push.assert_called_once()

    def test_failed_read_refuses_reconciliation_before_writing(self):
        sync_usage._sync("data/work", no_push=True)
        before = self.stored("codex")
        self.multica.side_effect = OSError()
        self.unified.return_value = (row(1), row(2), row(3))
        self.assertEqual(sync_usage._sync("data/work", no_push=True,
                                         reconcile_since=date(2026, 9, 7)), 1)
        self.assertEqual(self.stored("codex"), before)

    def test_tokens_publish_before_an_opted_in_task_failure(self):
        order = []
        self.push.side_effect = lambda _: order.append("push")
        def fail(**kwargs):
            order.append("task")
            raise TypeError("malformed API data")
        self.tasks.side_effect = fail
        self.assertEqual(sync_usage._sync("data/work", no_push=False, include_multica_tasks=True), 1)
        self.assertEqual(order, ["push", "task"])
        self.assertEqual(self.stored("codex")["2026-09-07"]["totalTokens"], 22)

    def test_opted_in_task_refresh_publishes_separately(self):
        self.assertEqual(sync_usage._sync("data/work", no_push=False, include_multica_tasks=True), 0)
        self.assertEqual(self.push.call_count, 2)
        self.tasks.assert_called_once_with(store_path=self.root / "data/multica.json")

    def test_non_writer_never_fetches_shared_tasks(self):
        self.assertEqual(sync_usage._sync("data/personal", no_push=True, include_multica_tasks=True), 0)
        self.tasks.assert_not_called()

    def test_failure_diagnostics_do_not_expose_paths_or_commands(self):
        output = io.StringIO()
        self.multica.side_effect = ValueError("secret-session-name")
        with redirect_stderr(output):
            sync_usage.collect_usage(date(2026, 9, 7))
        self.assertIn("codex-multica: status=failed", output.getvalue())
        self.assertNotIn("secret-session-name", output.getvalue())
