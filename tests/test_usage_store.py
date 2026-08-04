from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import compact_trails  # noqa: E402
import sync_usage  # noqa: E402


class MergeWithCumulativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "claude.json"

    def write_store(self, data: dict) -> None:
        self.store.write_text(json.dumps(data))

    def read_store(self) -> dict:
        return json.loads(self.store.read_text())

    def test_growing_tokens_carry_their_fresh_cost(self) -> None:
        self.write_store({"2026-08-04": {"totalTokens": 100, "totalCost": 1.0}})
        sync_usage.merge_with_cumulative(
            [{"date": "2026-08-04", "totalTokens": 250, "totalCost": 2.5}], self.store
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 250, "totalCost": 2.5}
        )

    def test_same_tokens_repriced_lower_flow_through(self) -> None:
        self.write_store({"2026-08-04": {"totalTokens": 100, "totalCost": 4.0}})
        sync_usage.merge_with_cumulative(
            [{"date": "2026-08-04", "totalTokens": 100, "totalCost": 1.5}], self.store
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 100, "totalCost": 1.5}
        )

    def test_token_regression_freezes_the_stored_pair(self) -> None:
        self.write_store({"2026-08-04": {"totalTokens": 100, "totalCost": 4.0}})
        sync_usage.merge_with_cumulative(
            [{"date": "2026-08-04", "totalTokens": 40, "totalCost": 1.6}], self.store
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 100, "totalCost": 4.0}
        )

    def test_unpriced_fetch_keeps_the_last_known_cost(self) -> None:
        """Every model on the day lost its price, so the whole day comes back free."""
        self.write_store(
            {
                "2026-07-06": {"totalTokens": 900, "totalCost": 30.0},
                "2026-08-04": {"totalTokens": 100, "totalCost": 4.0},
            }
        )
        sync_usage.merge_with_cumulative(
            [
                {"date": "2026-07-06", "totalTokens": 900, "totalCost": 0.0},
                {"date": "2026-08-04", "totalTokens": 250, "totalCost": 0.0},
            ],
            self.store,
        )
        store = self.read_store()
        # History must not be repriced to nothing by one offline run...
        self.assertEqual(store["2026-07-06"], {"totalTokens": 900, "totalCost": 30.0})
        # ...and today keeps counting tokens while its cost holds the last known value.
        self.assertEqual(store["2026-08-04"], {"totalTokens": 250, "totalCost": 4.0})

    def test_next_priced_fetch_overwrites_the_held_cost(self) -> None:
        self.write_store({"2026-08-04": {"totalTokens": 250, "totalCost": 4.0}})
        sync_usage.merge_with_cumulative(
            [{"date": "2026-08-04", "totalTokens": 300, "totalCost": 9.5}], self.store
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 300, "totalCost": 9.5}
        )

    def test_day_first_seen_unpriced_is_recorded_and_recovers(self) -> None:
        sync_usage.merge_with_cumulative(
            [{"date": "2026-08-04", "totalTokens": 250, "totalCost": 0.0}], self.store
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 250, "totalCost": 0.0}
        )
        sync_usage.merge_with_cumulative(
            [{"date": "2026-08-04", "totalTokens": 250, "totalCost": 7.25}], self.store
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 250, "totalCost": 7.25}
        )

    def test_a_new_model_still_back_fills_after_growing_unpriced(self) -> None:
        """The whole life of a model upstream has not priced yet.

        It is recorded free, keeps accruing tokens across several unpriced runs, and
        must take the first real price it is offered. Nothing in the guard may pin
        the day at 0 — that back-fill is the reason cost follows tokens at all.
        """
        for tokens in (250, 400, 900):
            sync_usage.merge_with_cumulative(
                [{"date": "2026-08-04", "totalTokens": tokens, "totalCost": 0.0}],
                self.store,
            )
            self.assertEqual(self.read_store()["2026-08-04"]["totalCost"], 0.0)
        sync_usage.merge_with_cumulative(
            [{"date": "2026-08-04", "totalTokens": 900, "totalCost": 12.5}], self.store
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 900, "totalCost": 12.5}
        )

    def test_a_partly_priced_day_keeps_its_cost(self) -> None:
        """The shape the value test alone cannot see.

        Only the models newer than ccusage's bundled snapshot lose their price, so
        the day's sum stays truthy and merely understates — $1.03 against a stored
        $69.47 on the same day, in the run that prompted this. The fetch marks the
        day untrusted; tokens still advance.
        """
        self.write_store({"2026-08-04": {"totalTokens": 900, "totalCost": 69.47}})
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-04", "totalTokens": 1200, "totalCost": 1.03,
                "costTrusted": False,
            }],
            self.store,
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 1200, "totalCost": 69.47}
        )
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-04", "totalTokens": 1200, "totalCost": 74.5,
                "costTrusted": True,
            }],
            self.store,
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 1200, "totalCost": 74.5}
        )

    def test_an_untrusted_day_with_no_stored_cost_still_records(self) -> None:
        """Nothing to preserve, so the marking must not pin the day at nothing."""
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-04", "totalTokens": 900, "totalCost": 1.03,
                "costTrusted": False,
            }],
            self.store,
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 900, "totalCost": 1.03}
        )

    def test_the_marking_never_reaches_the_store(self) -> None:
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-04", "totalTokens": 900, "totalCost": 2.0,
                "costTrusted": True,
            }],
            self.store,
        )
        self.assertNotIn("costTrusted", self.read_store()["2026-08-04"])


class ReconcileTests(unittest.TestCase):
    """The high-water rule is right on the schedule and wrong after an upgrade."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "codex.json"
        self.store.write_text(json.dumps(
            {
                "2026-07-10": {"totalTokens": 500, "totalCost": 10.0},
                "2026-07-25": {"totalTokens": 900, "totalCost": 20.0},
            }
        ))
        self.corrected = [
            {"date": "2026-07-10", "totalTokens": 400, "totalCost": 8.0},
            {"date": "2026-07-25", "totalTokens": 300, "totalCost": 6.0},
        ]

    def test_lower_counts_are_refused_by_default(self) -> None:
        sync_usage.merge_with_cumulative(self.corrected, self.store)
        store = json.loads(self.store.read_text())
        self.assertEqual(store["2026-07-10"], {"totalTokens": 500, "totalCost": 10.0})
        self.assertEqual(store["2026-07-25"], {"totalTokens": 900, "totalCost": 20.0})

    def test_reconciling_takes_them_from_the_given_date_on(self) -> None:
        sync_usage.merge_with_cumulative(
            self.corrected, self.store, reconcile_since=date(2026, 7, 25)
        )
        store = json.loads(self.store.read_text())
        # Before the window the high-water rule still stands...
        self.assertEqual(store["2026-07-10"], {"totalTokens": 500, "totalCost": 10.0})
        # ...and inside it the correction lands whole, cost with tokens.
        self.assertEqual(store["2026-07-25"], {"totalTokens": 300, "totalCost": 6.0})

    def test_reconciling_does_not_re_apply_the_unpriced_guard(self) -> None:
        """Inside the window the fetch is authoritative, and was checked upstream."""
        sync_usage.merge_with_cumulative(
            [{"date": "2026-07-25", "totalTokens": 300, "totalCost": 0.0}],
            self.store,
            reconcile_since=date(2026, 7, 25),
        )
        self.assertEqual(
            json.loads(self.store.read_text())["2026-07-25"],
            {"totalTokens": 300, "totalCost": 0.0},
        )


class TrailIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.id_file = Path(self._tmp.name) / "trail_id"
        p = patch.object(sync_usage, "TRAIL_ID_FILE", self.id_file)
        p.start()
        self.addCleanup(p.stop)

    def resolve(self) -> str:
        with patch.dict(sync_usage.os.environ, {sync_usage.TRAIL_ENV: "1"}):
            return sync_usage.resolve_machine()

    def test_the_identity_is_minted_once_and_reused(self) -> None:
        first = self.resolve()
        self.assertTrue(sync_usage.NODE_ID_RE.fullmatch(first.rsplit("/", 1)[-1]))
        self.assertEqual(first, self.resolve())
        self.assertEqual(first.rsplit("/", 1)[-1], self.id_file.read_text().strip())

    def test_nothing_about_the_host_is_consulted(self) -> None:
        """The hostname dependency is what let one machine become eight nodes."""
        source = Path(sync_usage.__file__).read_text()
        self.assertNotIn("gethostname", source)

    def test_a_fresh_worker_mints_its_own(self) -> None:
        first = self.resolve()
        self.id_file.unlink()
        self.assertNotEqual(first, self.resolve())

    def test_an_existing_folder_can_be_adopted(self) -> None:
        self.id_file.write_text("node-f4c49af3ff34\n")
        self.assertEqual(self.resolve(), "data/trail/node-f4c49af3ff34")

    def test_a_corrupt_file_stops_the_run_rather_than_reminting(self) -> None:
        """Reminting over a damaged identity orphans the folder it points at."""
        self.id_file.write_text("../../etc\n")
        with self.assertRaises(SystemExit):
            self.resolve()
        self.assertEqual(self.id_file.read_text(), "../../etc\n")

    def test_a_half_written_file_stops_the_run(self) -> None:
        self.id_file.write_text("node-f4c49a")
        with self.assertRaises(SystemExit):
            self.resolve()

    def test_an_empty_file_stops_the_run(self) -> None:
        """Emptiness is an unusable value, not a second kind of absence.

        Treating it as absent means minting, failing to link over the file that
        is already there, and reading the same emptiness again — forever.
        """
        self.id_file.write_text("")
        with self.assertRaises(SystemExit):
            self.resolve()

    def test_a_whitespace_only_file_stops_the_run(self) -> None:
        self.id_file.write_text("   \n")
        with self.assertRaises(SystemExit):
            self.resolve()

    def test_an_id_minted_concurrently_is_adopted(self) -> None:
        """Two first runs must converge, not split the history across two folders."""
        real = sync_usage.os.link
        def link_after_someone_else_won(src, dst):
            Path(dst).write_text("node-aaaabbbbcccc\n")
            return real(src, dst)
        with patch.object(sync_usage.os, "link", link_after_someone_else_won):
            self.assertEqual(self.resolve(), "data/trail/node-aaaabbbbcccc")

    def test_a_partial_id_is_never_visible_to_a_reader(self) -> None:
        """The file appears only once fully written, so a racing reader can't see half."""
        seen = []
        real = sync_usage.os.link
        def record_then_link(src, dst):
            seen.append(Path(src).read_text())
            return real(src, dst)
        with patch.object(sync_usage.os, "link", record_then_link):
            minted = self.resolve().rsplit("/", 1)[-1]
        self.assertEqual(seen, [minted + "\n"])

    def test_an_explicit_identity_still_hashes(self) -> None:
        with patch.dict(sync_usage.os.environ, {sync_usage.TRAIL_ENV: "worker-7"}):
            machine = sync_usage.resolve_machine()
        self.assertEqual(machine, f"data/trail/{sync_usage._opaque_node_id('worker-7')}")
        self.assertFalse(self.id_file.exists())


class UnpricedModelsTests(unittest.TestCase):
    def test_reports_models_that_billed_tokens_for_free(self) -> None:
        raw = [
            {"period": "2026-08-03", "modelBreakdowns": [
                {"modelName": "claude-opus-5", "inputTokens": 10, "cost": 0.0},
                {"modelName": "gpt-5.5", "inputTokens": 10, "cost": 2.0},
            ]},
            {"period": "2026-08-04", "modelBreakdowns": [
                {"modelName": "claude-opus-5", "cacheReadTokens": 90, "cost": 0.0},
            ]},
        ]
        self.assertEqual(sync_usage.unpriced_models(raw), {"claude-opus-5": 2})

    def test_ignores_models_upstream_never_priced(self) -> None:
        raw = [{"period": "2026-08-04", "modelBreakdowns": [
            {"modelName": "codex-auto-review", "inputTokens": 500, "cost": 0.0},
        ]}]
        self.assertEqual(sync_usage.unpriced_models(raw), {})

    def test_ignores_a_model_that_simply_had_no_usage(self) -> None:
        raw = [{"period": "2026-08-04", "modelBreakdowns": [
            {"modelName": "claude-opus-5", "inputTokens": 0, "cost": 0.0},
        ]}]
        self.assertEqual(sync_usage.unpriced_models(raw), {})

    def test_a_fully_priced_run_is_silent(self) -> None:
        raw = [{"period": "2026-08-04", "modelBreakdowns": [
            {"modelName": "gpt-5.5", "inputTokens": 10, "outputTokens": 5, "cost": 1.5},
        ]}]
        self.assertEqual(sync_usage.unpriced_models(raw), {})


class CostTrustedMarkingTests(unittest.TestCase):
    """fetch_daily_since decides, per day and per agent, whether pricing was complete."""

    def fetch(self, breakdowns: list[dict]) -> dict[str, dict]:
        payload = json.dumps(
            {"daily": [{"period": "2026-08-04", "modelBreakdowns": breakdowns}]}
        )
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
        with patch.object(sync_usage.subprocess, "run", return_value=completed), \
                patch.object(sync_usage, "count_codex_image_files_per_day",
                             return_value={}):
            cc, cx, op = sync_usage.fetch_daily_since(date(2026, 1, 1))
        return {
            "claude": cc[0] if cc else None,
            "codex": cx[0] if cx else None,
            "opencode": op[0] if op else None,
        }

    def test_one_unpriced_model_taints_only_its_own_agent(self) -> None:
        out = self.fetch([
            {"modelName": "claude-opus-5", "inputTokens": 100, "cost": 0.0},
            {"modelName": "claude-sonnet-5", "inputTokens": 50, "cost": 1.03},
            {"modelName": "gpt-5.5", "inputTokens": 10, "cost": 2.0},
        ])
        # The Claude sum is short by whatever opus-5 should have cost...
        self.assertFalse(out["claude"]["costTrusted"])
        self.assertEqual(out["claude"]["totalCost"], 1.03)
        # ...while Codex priced everything it saw and stays usable.
        self.assertTrue(out["codex"]["costTrusted"])

    def test_a_fully_priced_day_is_trusted(self) -> None:
        out = self.fetch([
            {"modelName": "claude-opus-5", "inputTokens": 100, "cost": 4.0},
            {"modelName": "gpt-5.5", "inputTokens": 10, "cost": 2.0},
        ])
        self.assertTrue(out["claude"]["costTrusted"])
        self.assertTrue(out["codex"]["costTrusted"])

    def test_a_model_upstream_never_prices_does_not_taint_its_day(self) -> None:
        out = self.fetch([
            {"modelName": "codex-auto-review", "inputTokens": 500, "cost": 0.0},
            {"modelName": "gpt-5.5", "inputTokens": 10, "cost": 2.0},
        ])
        self.assertTrue(out["codex"]["costTrusted"])

    def test_a_model_with_no_usage_does_not_taint_its_day(self) -> None:
        out = self.fetch([
            {"modelName": "claude-opus-5", "inputTokens": 0, "cost": 0.0},
            {"modelName": "claude-sonnet-5", "inputTokens": 50, "cost": 1.5},
        ])
        self.assertTrue(out["claude"]["costTrusted"])


class IdenticalSourceTests(unittest.TestCase):
    """The guard that stands between a duplicated worker and an additive fold."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.trail = Path(self._tmp.name)

    def pod(self, name: str, days: dict[str, int]) -> Path:
        d = self.trail / name
        d.mkdir()
        (d / "codex.json").write_text(
            json.dumps({k: {"totalTokens": v, "totalCost": 1.0} for k, v in days.items()})
        )
        return d

    def clashes(self) -> list[tuple[str, str, str, str, int]]:
        return compact_trails._identical_sources(sorted(self.trail.iterdir()))

    def test_distinct_pods_do_not_clash(self) -> None:
        self.pod("node-aaaaaaaaaaaa", {"2026-08-01": 100})
        self.pod("node-bbbbbbbbbbbb", {"2026-08-01": 200})
        self.assertEqual(self.clashes(), [])

    def test_two_pods_repeating_a_day_clash(self) -> None:
        self.pod("node-aaaaaaaaaaaa", {"2026-08-01": 100})
        self.pod("node-bbbbbbbbbbbb", {"2026-08-01": 100})
        self.assertEqual(
            self.clashes(),
            [("codex.json", "2026-08-01", "node-aaaaaaaaaaaa", "node-bbbbbbbbbbbb", 100)],
        )

    def test_a_clash_between_later_pods_is_not_hidden_by_the_first(self) -> None:
        """Keying on (file, date) alone compared b and c only against a, and missed them."""
        self.pod("node-aaaaaaaaaaaa", {"2026-08-01": 999})
        self.pod("node-bbbbbbbbbbbb", {"2026-08-01": 100})
        self.pod("node-cccccccccccc", {"2026-08-01": 100})
        self.assertEqual(
            self.clashes(),
            [("codex.json", "2026-08-01", "node-bbbbbbbbbbbb", "node-cccccccccccc", 100)],
        )

    def test_every_copy_beyond_the_first_is_reported(self) -> None:
        for n in ("node-aaaaaaaaaaaa", "node-bbbbbbbbbbbb", "node-cccccccccccc"):
            self.pod(n, {"2026-08-01": 100})
        self.assertEqual(
            [c[3] for c in self.clashes()],
            ["node-bbbbbbbbbbbb", "node-cccccccccccc"],
        )

    def test_a_day_both_pods_recorded_as_zero_is_not_a_clash(self) -> None:
        self.pod("node-aaaaaaaaaaaa", {"2026-08-01": 0})
        self.pod("node-bbbbbbbbbbbb", {"2026-08-01": 0})
        self.assertEqual(self.clashes(), [])


class ReconcileStubTests(unittest.TestCase):
    """--reconcile-since rewrites history downward; only real readings may drive it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "codex.json"
        self.store.write_text(json.dumps({
            "2026-07-25": {"totalTokens": 722_000_000, "totalCost": 90.0,
                           "models": {"gpt-5.5": 722_000_000}},
        }))

    def read(self) -> dict:
        return json.loads(self.store.read_text())

    def test_an_image_stub_cannot_zero_a_reconciled_day(self) -> None:
        """ccusage rotated the session away; the stub knows images, not tokens."""
        sync_usage.merge_with_cumulative(
            [{"date": "2026-07-25", "totalTokens": 0, "totalCost": 0.0,
              "models": {}, "imageCount": 3, "tokensObserved": False}],
            self.store,
            reconcile_since=date(2026, 7, 18),
        )
        day = self.read()["2026-07-25"]
        self.assertEqual(day["totalTokens"], 722_000_000)
        self.assertEqual(day["imageCount"], 3)

    def test_a_real_reading_still_corrects_downward(self) -> None:
        sync_usage.merge_with_cumulative(
            [{"date": "2026-07-25", "totalTokens": 223_000_000, "totalCost": 30.0}],
            self.store,
            reconcile_since=date(2026, 7, 18),
        )
        self.assertEqual(self.read()["2026-07-25"]["totalTokens"], 223_000_000)

    def test_reconciling_lets_an_emptied_breakdown_win(self) -> None:
        sync_usage.merge_with_cumulative(
            [{"date": "2026-07-25", "totalTokens": 223_000_000, "totalCost": 30.0,
              "models": {}}],
            self.store,
            reconcile_since=date(2026, 7, 18),
        )
        self.assertEqual(self.read()["2026-07-25"]["models"], {})

    def test_a_normal_merge_still_keeps_a_stale_breakdown(self) -> None:
        sync_usage.merge_with_cumulative(
            [{"date": "2026-07-25", "totalTokens": 800_000_000, "totalCost": 99.0,
              "models": {}}],
            self.store,
        )
        self.assertEqual(self.read()["2026-07-25"]["models"], {"gpt-5.5": 722_000_000})

    def test_a_day_the_fetch_dropped_keeps_its_stored_value(self) -> None:
        sync_usage.merge_with_cumulative(
            [{"date": "2026-07-26", "totalTokens": 100, "totalCost": 1.0}],
            self.store,
            reconcile_since=date(2026, 7, 18),
        )
        self.assertEqual(self.read()["2026-07-25"]["totalTokens"], 722_000_000)


if __name__ == "__main__":
    unittest.main()
