from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

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

    def test_a_partly_priced_day_is_not_protected(self) -> None:
        """Documents the limit of the guard rather than a behaviour worth having.

        In the real failure only the models newer than ccusage's bundled snapshot
        lose their price; the older ones still cost something, so the day's sum stays
        truthy and understates. The guard fires on exactly 0 and cannot see this, and
        the next complete fetch restores it.
        """
        self.write_store({"2026-08-04": {"totalTokens": 900, "totalCost": 40.0}})
        sync_usage.merge_with_cumulative(
            [{"date": "2026-08-04", "totalTokens": 900, "totalCost": 4.0}], self.store
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 900, "totalCost": 4.0}
        )
        sync_usage.merge_with_cumulative(
            [{"date": "2026-08-04", "totalTokens": 900, "totalCost": 41.5}], self.store
        )
        self.assertEqual(
            self.read_store()["2026-08-04"], {"totalTokens": 900, "totalCost": 41.5}
        )


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


if __name__ == "__main__":
    unittest.main()
