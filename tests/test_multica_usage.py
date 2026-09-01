from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import audit_public  # noqa: E402
import multica_usage  # noqa: E402
import squash_usage_branch  # noqa: E402
import sync_usage  # noqa: E402


RUNTIME_ID = "runtime-private-id"
ISSUE_ID = "issue-private-id"
# Split the same way audit_public.py splits its own pattern, so this fixture can
# carry a realistic absolute path without the public-data audit flagging the test
# file that exists to prove such paths never reach the store.
PRIVATE_WORK_DIR = "/" + "Users/someone/secret-project"


def _identity_bearing_fixture(
    *, provider: str = "codex", runs: list[dict] | None = None
):
    """A fake Multica API whose every response carries identity.

    The point of the fixture is that the real API answers look like this: names,
    hostnames, absolute paths, prompts and raw output. A test that fed the
    collector clean data could not tell whether the reduction is what keeps them
    out of the store.
    """
    default_runs = [{
        "id": "task-private-id",
        "runtime_id": RUNTIME_ID,
        "status": "completed",
        "started_at": "2026-08-31T01:00:00Z",
        "completed_at": "2026-08-31T01:02:30Z",
        "usage": [{"input_tokens": 100}],
        "trigger_summary": "private prompt text",
        "work_dir": PRIVATE_WORK_DIR,
        "author_email": "someone@example.com",
    }]

    def fake_run(args: list[str]) -> object:
        if args == ["runtime", "list"]:
            return [{
                "id": RUNTIME_ID,
                "custom_name": "laptop-alias",
                "provider": provider,
                "device_info": "private-host.local",
            }]
        if args[:2] == ["issue", "list"]:
            return {
                "issues": [{"id": ISSUE_ID, "title": "private prompt"}],
                "has_more": False,
            }
        if args == ["issue", "runs", ISSUE_ID]:
            return default_runs if runs is None else runs
        raise AssertionError(f"unexpected command: {args}")

    return fake_run


class TaskCollectionTests(unittest.TestCase):
    def test_reduces_terminal_runs_to_counters_and_keeps_no_identity(self) -> None:
        snapshot = multica_usage.collect_snapshot(
            {"laptop-alias": "work"}, run_json=_identity_bearing_fixture()
        )
        self.assertEqual(
            snapshot,
            {"2026-08-31": {"tasks": {"work": {"codex": {
                "total": 1, "completed": 1, "failed": 0, "cancelled": 0,
                "durationSeconds": 150,
            }}}}},
        )
        serialised = json.dumps(snapshot)
        for secret in (
            RUNTIME_ID, ISSUE_ID, "task-private-id", "laptop-alias",
            "private-host.local", "private prompt", "secret-project",
            "someone@example.com",
        ):
            self.assertNotIn(secret, serialised)

    def test_records_no_tokens_or_cost(self) -> None:
        """Multica's tokens arrive through the harnesses it drives.

        Counting them here as well would double them, against a measurement that
        does not even agree with the local one.
        """
        snapshot = multica_usage.collect_snapshot(
            {"laptop-alias": "work"}, run_json=_identity_bearing_fixture()
        )
        counters = snapshot["2026-08-31"]["tasks"]["work"]["codex"]
        self.assertEqual(set(counters), set(multica_usage.COUNTER_FIELDS))
        serialised = json.dumps(snapshot)
        for forbidden in ("token", "Token", "cost", "Cost", "usage"):
            self.assertNotIn(forbidden, serialised)

    def test_a_run_still_in_flight_is_not_counted(self) -> None:
        """It has no duration yet, and its status is not the one it will end on."""
        snapshot = multica_usage.collect_snapshot(
            {"laptop-alias": "work"},
            run_json=_identity_bearing_fixture(runs=[{
                "runtime_id": RUNTIME_ID,
                "status": "running",
                "started_at": "2026-08-31T01:00:00Z",
            }]),
        )
        self.assertEqual(snapshot, {})

    def test_every_provider_either_server_reports_is_mapped(self) -> None:
        """The two Multica servers disagree on one provider string.

        `multica runtime list` returns `traecli` on one and `traex` on the other,
        for the same TRAE CLI. Mapping only one silently drops every runtime on
        the other server, which is indistinguishable from it having done no work.
        These are the values both servers actually return.
        """
        self.assertEqual(
            multica_usage.PROVIDER_AGENTS,
            {
                "claude": "claude", "codex": "codex", "dsh": "dsh",
                "traecli": "traex", "traex": "traex",
            },
        )
        for provider, agent in multica_usage.PROVIDER_AGENTS.items():
            with self.subTest(provider=provider):
                snapshot = multica_usage.collect_snapshot(
                    {"laptop-alias": "work"},
                    run_json=_identity_bearing_fixture(provider=provider),
                )
                self.assertIn(agent, snapshot["2026-08-31"]["tasks"]["work"])

    def test_an_unmapped_provider_is_reported_rather_than_dropped(self) -> None:
        """A renamed provider string must not read as "did no work"."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(ValueError):
            multica_usage.collect_snapshot(
                {"laptop-alias": "work"},
                run_json=_identity_bearing_fixture(provider="something-new"),
            )
        self.assertIn("something-new", stderr.getvalue())


class SnapshotMergeTests(unittest.TestCase):
    """A finished run's day, status and duration never change again."""

    def test_a_pruned_fetch_cannot_shrink_a_recorded_day(self) -> None:
        stored = {"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 9, "completed": 8, "failed": 1, "cancelled": 0,
            "durationSeconds": 900,
        }}}}}
        pruned = {"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 2, "completed": 2, "failed": 0, "cancelled": 0,
            "durationSeconds": 200,
        }}}}}
        merged = multica_usage.merge_snapshot(stored, pruned)
        self.assertEqual(merged, stored)

    def test_new_days_and_new_agents_are_added(self) -> None:
        stored = {"2026-08-30": {"tasks": {"work": {"codex": {
            "total": 1, "completed": 1, "failed": 0, "cancelled": 0,
            "durationSeconds": 10,
        }}}}}
        merged = multica_usage.merge_snapshot(stored, {"2026-08-31": {"tasks": {
            "work": {"dsh": {
                "total": 3, "completed": 3, "failed": 0, "cancelled": 0,
                "durationSeconds": 30,
            }}
        }}})
        self.assertEqual(sorted(merged), ["2026-08-30", "2026-08-31"])
        self.assertEqual(merged["2026-08-31"]["tasks"]["work"]["dsh"]["total"], 3)
        self.assertEqual(merged["2026-08-30"]["tasks"]["work"]["codex"]["total"], 1)

    def test_counters_never_mix_two_observations(self) -> None:
        """Per-counter maxima would invent a day that never happened.

        Two completed runs are pruned and two failed ones land on the same day.
        Maxing each counter on its own keeps completed=2 from the first fetch and
        failed=2 from the second, against a total of 2 — four outcomes in a
        two-run day, which the public-data audit rejects outright. The merge has
        to choose one whole observation.
        """
        first = {"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 2, "completed": 2, "failed": 0, "cancelled": 0,
            "durationSeconds": 20,
        }}}}}
        replaced = {"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 2, "completed": 0, "failed": 2, "cancelled": 0,
            "durationSeconds": 30,
        }}}}}
        counters = (
            multica_usage.merge_snapshot(first, replaced)
        )["2026-08-31"]["tasks"]["work"]["codex"]
        self.assertEqual(
            counters["total"],
            counters["completed"] + counters["failed"] + counters["cancelled"],
        )

    def test_the_merged_result_always_passes_the_public_audit(self) -> None:
        """The invariant the audit enforces has to survive merging, not just fetching."""
        stored = {"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 3, "completed": 3, "failed": 0, "cancelled": 0,
            "durationSeconds": 30,
        }}}}}
        for incoming in (
            {"total": 3, "completed": 0, "failed": 3, "cancelled": 0, "durationSeconds": 9},
            {"total": 1, "completed": 0, "failed": 0, "cancelled": 1, "durationSeconds": 1},
            {"total": 7, "completed": 2, "failed": 4, "cancelled": 1, "durationSeconds": 70},
        ):
            with self.subTest(incoming=incoming):
                merged = multica_usage.merge_snapshot(
                    stored, {"2026-08-31": {"tasks": {"work": {"codex": incoming}}}}
                )
                issues: list[str] = []
                audit_public._validate_multica_schema(
                    merged, Path("data/multica.json"), issues
                )
                self.assertEqual(issues, [])

    def test_a_tied_total_still_accepts_a_corrected_duration(self) -> None:
        """A terminal run can be reported before its finish time is set.

        It is counted with a duration of zero. The next fetch sees the same run
        with `completed_at`, so the totals tie — ranking on total alone would
        keep the zero for good.
        """
        first = {"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 1, "completed": 1, "failed": 0, "cancelled": 0,
            "durationSeconds": 0,
        }}}}}
        corrected = {"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 1, "completed": 1, "failed": 0, "cancelled": 0,
            "durationSeconds": 250,
        }}}}}
        merged = multica_usage.merge_snapshot(first, corrected)
        self.assertEqual(
            merged["2026-08-31"]["tasks"]["work"]["codex"]["durationSeconds"], 250
        )

    def test_merging_does_not_mutate_the_stored_aggregate(self) -> None:
        stored = {"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 1, "completed": 1, "failed": 0, "cancelled": 0,
            "durationSeconds": 10,
        }}}}}
        before = json.dumps(stored, sort_keys=True)
        multica_usage.merge_snapshot(stored, {"2026-08-31": {"tasks": {"work": {
            "codex": {"total": 5, "completed": 5, "failed": 0, "cancelled": 0,
                      "durationSeconds": 50},
        }}}})
        self.assertEqual(json.dumps(stored, sort_keys=True), before)

    def test_write_is_a_merge_not_an_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "multica.json"
            multica_usage.write_snapshot({"2026-08-31": {"tasks": {"work": {
                "codex": {"total": 4, "completed": 4, "failed": 0,
                          "cancelled": 0, "durationSeconds": 400},
            }}}}, path)
            multica_usage.write_snapshot({"2026-08-31": {"tasks": {"work": {
                "codex": {"total": 1, "completed": 1, "failed": 0,
                          "cancelled": 0, "durationSeconds": 100},
            }}}}, path)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["2026-08-31"]["tasks"]["work"]["codex"]["total"], 4)


class DeduplicationTests(unittest.TestCase):
    """The API can hand back the same run more than once."""

    RUN = {
        "id": "run-1", "runtime_id": "rt", "status": "completed",
        "started_at": "2026-08-31T01:00:00Z",
        "completed_at": "2026-08-31T01:01:00Z",
    }

    def _collect(self, pages: list[dict], runs_for: dict) -> dict:
        def api(args: list[str]) -> object:
            if args[:2] == ["issue", "list"]:
                offset = int(args[args.index("--offset") + 1])
                index = offset // multica_usage.ISSUE_PAGE_SIZE
                return pages[index] if index < len(pages) else {
                    "issues": [], "has_more": False,
                }
            if args[:2] == ["issue", "runs"]:
                return runs_for[args[2]]
            raise AssertionError(f"unexpected command: {args}")

        return multica_usage.collect_tasks({"rt": ("work", "codex")}, run_json=api)

    def test_one_run_under_two_issues_counts_once(self) -> None:
        """Otherwise the merge makes the transient double count permanent."""
        days = self._collect(
            [{"issues": [{"id": "i1"}, {"id": "i2"}], "has_more": False}],
            {"i1": [self.RUN], "i2": [self.RUN]},
        )
        counters = days["2026-08-31"]["tasks"]["work"]["codex"]
        self.assertEqual(counters["total"], 1)
        self.assertEqual(counters["durationSeconds"], 60)

    def test_an_issue_repeated_across_pages_counts_once(self) -> None:
        """Pages overlap when the workspace is written to mid-collection."""
        days = self._collect(
            [
                {"issues": [{"id": "i1"}], "has_more": True},
                {"issues": [{"id": "i1"}], "has_more": False},
            ],
            {"i1": [self.RUN]},
        )
        self.assertEqual(days["2026-08-31"]["tasks"]["work"]["codex"]["total"], 1)


class MalformedPayloadTests(unittest.TestCase):
    """A shape the API should not send must not strand the token stores.

    The collector runs after every token store has merged and before the push, so
    an exception escaping it costs a day of token data on every machine.
    """

    def test_an_unhashable_provider_does_not_raise(self) -> None:
        index = multica_usage._runtime_index(
            [{"id": "r", "custom_name": "n", "provider": ["codex"]}], {"n": "work"}
        )
        self.assertEqual(index, {})

    def test_an_unhashable_custom_name_does_not_raise(self) -> None:
        index = multica_usage._runtime_index(
            [{"id": "r", "custom_name": {"a": 1}, "provider": "codex"}], {"n": "work"}
        )
        self.assertEqual(index, {})

    def test_a_non_string_role_is_rejected_not_crashed_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "roles.json"
            config.write_text(json.dumps({"n": ["work"]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                multica_usage.load_runtime_roles(config)


class ConfigTests(unittest.TestCase):
    def test_collection_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.json"
            store = Path(tmp) / "multica.json"
            self.assertFalse(multica_usage.collect_if_configured(missing, store))
            self.assertFalse(store.exists())

    def test_a_role_outside_the_public_set_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "roles.json"
            config.write_text(json.dumps({"laptop-alias": "laptop"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                multica_usage.load_runtime_roles(config)


class StoreRegistrationTests(unittest.TestCase):
    def test_the_task_store_is_not_an_agent_token_store(self) -> None:
        """It holds no tokens, so it must not join the per-machine store list.

        Everything in AGENT_STORES is summed into the ledger's totals; this one
        would contribute nothing and be looked for under every node label.
        """
        self.assertNotIn("multica", sync_usage.AGENT_STORES)
        self.assertNotIn("multica.json", set(sync_usage.AGENT_STORES.values()))

    def test_the_audit_and_the_writer_name_the_same_file(self) -> None:
        """audit_public runs standalone and repeats the path as a literal."""
        self.assertEqual(audit_public.MULTICA_TASK_STORE, sync_usage.MULTICA_TASK_STORE)

    def test_the_squash_rule_covers_the_task_store(self) -> None:
        """Without this the daily squash treats it as a hand-edited file."""
        self.assertIsNotNone(
            squash_usage_branch.GENERATED_STORE.fullmatch(sync_usage.MULTICA_TASK_STORE)
        )


class AuditTests(unittest.TestCase):
    def _issues(self, payload: object) -> list[str]:
        issues: list[str] = []
        audit_public._validate_multica_schema(payload, Path("data/multica.json"), issues)
        return issues

    def test_a_clean_aggregate_passes(self) -> None:
        self.assertEqual(self._issues({"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 2, "completed": 1, "failed": 1, "cancelled": 0,
            "durationSeconds": 12,
        }}}}}), [])

    def test_a_usage_section_is_rejected(self) -> None:
        """The guard against re-introducing the double count.

        Multica's tokens reach the ledger through codex-multica.json and the
        other harnesses' own trees. A `usage` key here means they are being
        counted a second time.
        """
        issues = self._issues({"2026-08-31": {
            "tasks": {},
            "usage": {"work": {"codex": {"totalTokens": 1}}},
        }})
        self.assertTrue(any("usage" in issue for issue in issues))

    def test_an_unknown_field_is_rejected_rather_than_ignored(self) -> None:
        issues = self._issues({"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 1, "completed": 1, "failed": 0, "cancelled": 0,
            "durationSeconds": 1, "workDir": PRIVATE_WORK_DIR,
        }}}}})
        self.assertTrue(any("workDir" in issue for issue in issues))

    def test_outcomes_must_account_for_every_counted_task(self) -> None:
        issues = self._issues({"2026-08-31": {"tasks": {"work": {"codex": {
            "total": 5, "completed": 1, "failed": 1, "cancelled": 0,
            "durationSeconds": 1,
        }}}}})
        self.assertTrue(any("terminal outcomes" in issue for issue in issues))

    def test_a_role_outside_the_public_labels_is_rejected(self) -> None:
        issues = self._issues({"2026-08-31": {"tasks": {"someone-laptop": {}}}})
        self.assertTrue(any("role" in issue for issue in issues))

    def test_an_agent_the_collector_cannot_emit_is_rejected(self) -> None:
        """The allow-list is the collector's output, not the token-store list.

        `opencode` is a token store but not something Multica dispatches, and
        `codex-multica` is a store name rather than an agent. Accepting either
        would pass a file the collector could not have written.
        """
        for agent in ("opencode", "codex-multica"):
            with self.subTest(agent=agent):
                issues = self._issues({"2026-08-31": {"tasks": {"work": {agent: {
                    "total": 1, "completed": 1, "failed": 0, "cancelled": 0,
                    "durationSeconds": 1,
                }}}}})
                self.assertTrue(any("agent" in issue for issue in issues))

    def test_the_audit_allows_exactly_what_the_collector_emits(self) -> None:
        self.assertEqual(
            audit_public.MULTICA_TASK_AGENTS,
            set(multica_usage.PROVIDER_AGENTS.values()),
        )

    def test_a_bundle_missing_a_counter_is_rejected(self) -> None:
        """Without every counter present the arithmetic check has nothing to compare."""
        for payload in (
            {"total": 1},
            {"completed": 1, "failed": 0, "cancelled": 0, "durationSeconds": 1},
        ):
            with self.subTest(payload=payload):
                issues = self._issues(
                    {"2026-08-31": {"tasks": {"work": {"codex": payload}}}}
                )
                self.assertTrue(issues)


class SyncIntegrationTests(unittest.TestCase):
    """The collector is an enrichment; it must never cost a day of token data.

    These read _sync's syntax tree rather than running it: _sync fetches from
    ccusage, walks session trees and pushes to git, so exercising it end to end
    would test the mocks. What matters is structural and is checked as such.
    """

    @staticmethod
    def _sync_tree() -> ast.AST:
        return ast.parse(textwrap.dedent(inspect.getsource(sync_usage._sync)))

    @staticmethod
    def _calls(node: ast.AST, name: str) -> list[ast.Call]:
        return [
            call for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == name
        ]

    def test_the_collector_call_catches_everything(self) -> None:
        """An enumerated list was tried and was wrong.

        A JSON payload with a list where a string belongs raises TypeError out of
        a dict lookup, which was not in the list, and that escaping _sync skips
        git_push — so the token stores merged just before it never get committed.
        """
        guards = [
            node for node in ast.walk(self._sync_tree())
            if isinstance(node, ast.Try) and self._calls(node.body[0], "collect_if_configured")
        ]
        self.assertEqual(len(guards), 1, "collector call is not wrapped exactly once")
        handlers = guards[0].handlers
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0].type, ast.Name)
        self.assertEqual(handlers[0].type.id, "Exception")

    def test_the_push_is_not_inside_the_collector_guard(self) -> None:
        """git_push has to be reachable whether or not the collector raised."""
        tree = self._sync_tree()
        guarded = {
            id(call)
            for node in ast.walk(tree)
            if isinstance(node, ast.Try) and self._calls(node.body[0], "collect_if_configured")
            for call in self._calls(node, "git_push")
        }
        pushes = self._calls(tree, "git_push")
        self.assertTrue(pushes, "_sync no longer pushes")
        self.assertFalse(guarded, "git_push moved inside the collector's try block")

    def test_both_shared_file_touchpoints_are_writer_guarded(self) -> None:
        """One writer per file is what makes the high-water merge trustworthy."""
        self.assertEqual(sync_usage.MULTICA_TASK_WRITER, "work")
        module = ast.parse(inspect.getsource(sync_usage))
        guarded_calls, guarded_appends = 0, 0
        for node in ast.walk(module):
            if not isinstance(node, ast.If):
                continue
            if "MULTICA_TASK_WRITER" not in ast.dump(node.test):
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            guarded_calls += "collect_if_configured" in body
            # The collector guard also names the store (it passes store_path), so
            # the staging site is identified by the append itself.
            guarded_appends += "attr='append'" in body and "MULTICA_TASK_STORE" in body
        self.assertEqual(guarded_calls, 1, "the collector runs outside the writer check")
        self.assertEqual(guarded_appends, 1, "the store is staged outside the writer check")


if __name__ == "__main__":
    unittest.main()
