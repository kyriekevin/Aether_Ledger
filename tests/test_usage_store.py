from __future__ import annotations

import io
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


class SharedStoreCoverageTests(unittest.TestCase):
    def test_trail_compaction_covers_traex(self) -> None:
        self.assertIn("traex.json", compact_trails.AGENT_FILES)

    def test_trail_compaction_preserves_allocation_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollups = {"codex.json": {}}
            for index, (tokens, quota) in enumerate(((100, 40.0), (200, 75.0))):
                pod = root / f"pod-{index}"
                pod.mkdir()
                (pod / "codex.json").write_text(json.dumps({
                    "2026-08-01": {
                        "totalTokens": tokens,
                        "totalCost": 1.0,
                        "models": {
                            "gpt-example": {
                                "totalTokens": tokens,
                                "inputTokens": tokens // 2,
                                "cacheReadTokens": tokens // 2,
                            }
                        },
                        "routing": {
                            "efforts": {
                                "low": {
                                    "turns": 1,
                                    "totalTokens": tokens,
                                    "reasoningCalls": 1,
                                    "reasoningOutputTokens": 5,
                                }
                            }
                        },
                        "quota": {
                            "windows": {"300": quota},
                            "limitReached": index == 1,
                        },
                    }
                }))
                with patch.object(compact_trails, "AGENT_FILES", ("codex.json",)):
                    compact_trails._fold_pod_into(rollups, pod)

            day = rollups["codex.json"]["2026-08-01"]
            self.assertEqual(day["totalTokens"], 300)
            self.assertEqual(day["models"]["gpt-example"]["inputTokens"], 150)
            self.assertEqual(day["routing"]["efforts"]["low"]["calls"], 2)
            self.assertEqual(
                day["routing"]["efforts"]["low"]["reasoningCalls"], 2
            )
            self.assertEqual(
                day["routing"]["efforts"]["low"]["reasoningOutputTokens"], 10
            )
            self.assertEqual(day["quota"]["windows"], {"300": 75.0})
            self.assertTrue(day["quota"]["limitReached"])


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

    def test_same_official_tokens_never_reprice_completed_history(self) -> None:
        self.write_store({
            "2026-08-04": {
                "totalTokens": 100, "totalCost": 4.0, "costSource": "official"
            }
        })
        sync_usage.merge_with_cumulative([{
            "date": "2026-08-04", "totalTokens": 100, "totalCost": 1.5,
            "costSource": "official",
        }], self.store)
        self.assertEqual(self.read_store()["2026-08-04"]["totalCost"], 4.0)

    def test_same_tokens_backfill_when_unpriced_becomes_official(self) -> None:
        self.write_store({
            "2026-08-04": {
                "totalTokens": 100, "totalCost": 0.0, "costSource": "unpriced"
            }
        })
        sync_usage.merge_with_cumulative([{
            "date": "2026-08-04", "totalTokens": 100, "totalCost": 5.0,
            "costSource": "official",
        }], self.store)
        self.assertEqual(
            self.read_store()["2026-08-04"],
            {"totalTokens": 100, "totalCost": 5.0, "costSource": "official"},
        )

    def test_same_tokens_never_downgrade_official_to_unpriced(self) -> None:
        self.write_store({
            "2026-08-04": {
                "totalTokens": 100, "totalCost": 5.0, "costSource": "official"
            }
        })
        sync_usage.merge_with_cumulative([{
            "date": "2026-08-04", "totalTokens": 100, "totalCost": 0.0,
            "costSource": "unpriced",
        }], self.store)
        self.assertEqual(
            self.read_store()["2026-08-04"],
            {"totalTokens": 100, "totalCost": 5.0, "costSource": "official"},
        )

    def test_unpriced_zero_replaces_a_legacy_proxy_price(self) -> None:
        self.write_store({"2026-08-04": {"totalTokens": 100, "totalCost": 9.0}})
        sync_usage.merge_with_cumulative([{
            "date": "2026-08-04", "totalTokens": 100, "totalCost": 0.0,
            "costSource": "unpriced", "costTrusted": True,
        }], self.store)
        self.assertEqual(
            self.read_store()["2026-08-04"],
            {"totalTokens": 100, "totalCost": 0.0, "costSource": "unpriced"},
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

    def test_each_model_keeps_its_high_water_when_the_fresh_total_wins(self) -> None:
        self.write_store({
            "2026-07-09": {
                "totalTokens": 95_406_538,
                "totalCost": 159.02,
                "models": {
                    "claude-fable-5": {"totalTokens": 91_163_717},
                    "claude-opus-4-8": {"totalTokens": 3_362_925},
                    "claude-sonnet-5": {"totalTokens": 879_896},
                },
            }
        })
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-07-09",
                "totalTokens": 95_406_538,
                "totalCost": 159.02,
                "models": {
                    "claude-fable-5": {"totalTokens": 57_210_072},
                    "claude-opus-4-8": {"totalTokens": 2_734_337},
                    "claude-sonnet-5": {"totalTokens": 592_667},
                },
            }],
            self.store,
        )
        self.assertEqual(
            self.read_store()["2026-07-09"]["models"],
            {
                "claude-fable-5": {"totalTokens": 91_163_717},
                "claude-opus-4-8": {"totalTokens": 3_362_925},
                "claude-sonnet-5": {"totalTokens": 879_896},
            },
        )

    def test_model_high_waters_keep_missing_models_and_accept_growth(self) -> None:
        self.write_store({
            "2026-08-04": {
                "totalTokens": 100,
                "totalCost": 1.0,
                "models": {
                    "old-model": {"totalTokens": 80},
                    "growing-model": {"totalTokens": 20},
                },
            }
        })
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-04",
                "totalTokens": 120,
                "totalCost": 1.2,
                "models": {
                    "growing-model": {"totalTokens": 30},
                    "new-model": {"totalTokens": 10},
                },
            }],
            self.store,
        )
        self.assertEqual(
            self.read_store()["2026-08-04"]["models"],
            {
                "old-model": {"totalTokens": 80},
                "growing-model": {"totalTokens": 30},
                "new-model": {"totalTokens": 10},
            },
        )

    def test_daily_total_covers_the_merged_model_high_waters(self) -> None:
        self.write_store({
            "2026-08-04": {
                "totalTokens": 100,
                "totalCost": 1.0,
                "models": {"model-a": {"totalTokens": 100}},
            }
        })
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-04",
                "totalTokens": 110,
                "totalCost": 1.1,
                "models": {
                    "model-a": {"totalTokens": 90},
                    "model-b": {"totalTokens": 20},
                },
            }],
            self.store,
        )
        day = self.read_store()["2026-08-04"]
        self.assertEqual(day["totalTokens"], 120)
        self.assertEqual(
            sum(model["totalTokens"] for model in day["models"].values()),
            day["totalTokens"],
        )

    def test_model_keys_are_canonicalized_before_taking_high_waters(self) -> None:
        models = {
            "GPT-5.4": {"totalTokens": 45_400},
            "GPT-5.5": {"totalTokens": 1_486_376},
            "Gemini-3-Flash-Preview": {"totalTokens": 425_379},
        }
        self.write_store({
            "2026-08-07": {
                "totalTokens": 1_957_155,
                "totalCost": 0.0,
                "models": models,
            }
        })
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-07",
                "totalTokens": 1_957_155,
                "totalCost": 0.0,
                "models": {name.lower(): value for name, value in models.items()},
            }],
            self.store,
        )
        day = self.read_store()["2026-08-07"]
        self.assertEqual(day["totalTokens"], 1_957_155)
        self.assertEqual(
            day["models"],
            {
                "gpt-5.4": {"totalTokens": 45_400},
                "gpt-5.5": {"totalTokens": 1_486_376},
                "gemini-3-flash-preview": {"totalTokens": 425_379},
            },
        )

    def test_model_token_components_survive_the_cumulative_merge(self) -> None:
        self.write_store({
            "2026-08-04": {
                "totalTokens": 100,
                "totalCost": 1.0,
                "models": {"gpt-5.5": {"totalTokens": 100}},
            }
        })
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-04",
                "totalTokens": 100,
                "totalCost": 1.0,
                "models": {
                    "gpt-5.5": {
                        "totalTokens": 100,
                        "inputTokens": 10,
                        "outputTokens": 5,
                        "cacheCreationTokens": 15,
                        "cacheReadTokens": 70,
                    }
                },
            }],
            self.store,
        )
        self.assertEqual(
            self.read_store()["2026-08-04"]["models"]["gpt-5.5"],
            {
                "totalTokens": 100,
                "inputTokens": 10,
                "outputTokens": 5,
                "cacheCreationTokens": 15,
                "cacheReadTokens": 70,
            },
        )
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-04",
                "totalTokens": 110,
                "totalCost": 1.1,
                "models": {"gpt-5.5": {"totalTokens": 110}},
            }],
            self.store,
        )
        self.assertEqual(
            self.read_store()["2026-08-04"]["models"]["gpt-5.5"],
            {
                "totalTokens": 110,
                "inputTokens": 10,
                "outputTokens": 5,
                "cacheCreationTokens": 15,
                "cacheReadTokens": 70,
            },
        )

    def test_routing_and_quota_keep_independent_high_waters(self) -> None:
        self.write_store({
            "2026-08-04": {
                "totalTokens": 100,
                "totalCost": 1.0,
                "routing": {
                    "efforts": {
                        "low": {
                            "turns": 2,
                            "totalTokens": 100,
                            "reasoningCalls": 2,
                            "reasoningOutputTokens": 20,
                        }
                    }
                },
                "quota": {
                    "windows": {"300": 75.0}, "limitReached": False,
                },
            }
        })
        sync_usage.merge_with_cumulative(
            [{
                "date": "2026-08-04",
                "totalTokens": 90,
                "totalCost": 0.9,
                "routing": {
                    "efforts": {
                        "low": {
                            "calls": 3, "totalTokens": 100, "reasoningCalls": 1,
                        },
                        "high": {
                            "calls": 1, "totalTokens": 40,
                            "reasoningCalls": 1,
                            "reasoningOutputTokens": 12,
                        },
                    },
                    "speeds": {"fast": {"calls": 1, "totalTokens": 40}},
                },
                "quota": {
                    "windows": {"300": 40.0, "10080": 20.0},
                    "limitReached": True,
                },
                "tokensObserved": False,
            }],
            self.store,
        )
        day = self.read_store()["2026-08-04"]
        self.assertEqual(day["routing"]["efforts"]["low"]["totalTokens"], 100)
        self.assertEqual(day["routing"]["efforts"]["low"]["calls"], 3)
        self.assertNotIn("turns", day["routing"]["efforts"]["low"])
        self.assertEqual(day["routing"]["efforts"]["low"]["reasoningCalls"], 2)
        self.assertEqual(
            day["routing"]["efforts"]["low"]["reasoningOutputTokens"], 20
        )
        self.assertEqual(day["routing"]["efforts"]["high"]["totalTokens"], 40)
        self.assertEqual(day["routing"]["speeds"]["fast"]["calls"], 1)
        self.assertEqual(day["quota"]["windows"], {"300": 75.0, "10080": 20.0})
        self.assertTrue(day["quota"]["limitReached"])


class RoutingTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write_jsonl(self, relative: str, events: list[dict]) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        return path

    def test_codex_events_become_effort_speed_and_quota_buckets(self) -> None:
        self.write_jsonl("2026/08/15/rollout.jsonl", [
            {
                "type": "event_msg", "timestamp": "2026-08-15T01:00:00Z",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {
                        "reasoning_effort": "low", "service_tier": "priority",
                    },
                },
            },
            {
                "type": "event_msg", "timestamp": "2026-08-15T01:01:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {
                        "total_tokens": 100, "reasoning_output_tokens": 20,
                    }},
                    "rate_limits": {
                        "primary": {"window_minutes": 300, "used_percent": 40},
                        "secondary": {
                            "window_minutes": 10080, "used_percent": 12,
                        },
                        "rate_limit_reached_type": None,
                    },
                },
            },
            {
                "type": "event_msg", "timestamp": "2026-08-15T02:00:00Z",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {
                        "reasoning_effort": "medium", "service_tier": "default",
                    },
                },
            },
            {
                "type": "event_msg", "timestamp": "2026-08-15T02:01:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {
                        "total_tokens": 200, "reasoning_output_tokens": 50,
                    }},
                    "rate_limits": {
                        "primary": {"window_minutes": 300, "used_percent": 80},
                        "rate_limit_reached_type": "primary",
                    },
                },
            },
        ])
        telemetry = sync_usage.collect_codex_routing_since(
            date(2026, 8, 15), self.root
        )["2026-08-15"]
        self.assertEqual(
            telemetry["routing"]["efforts"],
            {
                "low": {
                    "calls": 1, "totalTokens": 100,
                    "reasoningCalls": 1,
                    "reasoningOutputTokens": 20,
                },
                "medium": {
                    "calls": 1, "totalTokens": 200,
                    "reasoningCalls": 1,
                    "reasoningOutputTokens": 50,
                },
            },
        )
        self.assertEqual(telemetry["routing"]["speeds"]["fast"]["totalTokens"], 100)
        self.assertEqual(
            telemetry["routing"]["speeds"]["standard"]["totalTokens"], 200
        )
        self.assertEqual(telemetry["quota"]["windows"], {"300": 80.0, "10080": 12.0})
        self.assertTrue(telemetry["quota"]["limitReached"])
        self.assertNotIn("session", json.dumps(telemetry).lower())

    def test_claude_stream_updates_are_deduplicated_before_routing_totals(self) -> None:
        self.write_jsonl("project/session.jsonl", [
            {
                "type": "assistant", "timestamp": "2026-08-15T01:00:00Z",
                "effort": "low",
                "message": {
                    "id": "private-message-a",
                    "usage": {
                        "speed": "standard", "input_tokens": 10,
                        "output_tokens_details": {"thinking_tokens": 2},
                    },
                },
            },
            {
                "type": "assistant", "timestamp": "2026-08-15T01:00:01Z",
                "effort": "low",
                "message": {
                    "id": "private-message-a",
                    "usage": {
                        "speed": "standard", "input_tokens": 20,
                        "cache_read_input_tokens": 10,
                        "output_tokens_details": {"thinking_tokens": 4},
                    },
                },
            },
            {
                "type": "assistant", "timestamp": "2026-08-15T02:00:00Z",
                "effort": "xhigh",
                "message": {
                    "id": "private-message-b",
                    "usage": {
                        "speed": "standard", "output_tokens": 20,
                        "output_tokens_details": {"thinking_tokens": 8},
                    },
                },
            },
        ])
        telemetry = sync_usage.collect_claude_routing_since(
            date(2026, 8, 15), self.root
        )["2026-08-15"]["routing"]
        self.assertEqual(telemetry["efforts"]["low"], {
            "calls": 1, "totalTokens": 30,
            "reasoningCalls": 1, "reasoningOutputTokens": 4,
        })
        self.assertEqual(telemetry["efforts"]["xhigh"], {
            "calls": 1, "totalTokens": 20,
            "reasoningCalls": 1, "reasoningOutputTokens": 8,
        })
        self.assertNotIn("speeds", telemetry)
        self.assertNotIn("private-message", json.dumps(telemetry))


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


class OfficialPricingFetchTests(unittest.TestCase):
    """fetch_daily_since accepts only repository-owned official model prices."""

    def fetch(self, agent_breakdowns: dict[str, list[dict]]) -> dict[str, dict]:
        agents = [
            {"agent": agent, "modelBreakdowns": breakdowns}
            for agent, breakdowns in agent_breakdowns.items()
        ]
        payload = json.dumps(
            {"daily": [{
                "period": "2026-08-04",
                "agents": agents,
                "modelBreakdowns": [
                    model for breakdowns in agent_breakdowns.values()
                    for model in breakdowns
                ],
            }]}
        )
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return completed

        with patch.object(sync_usage.subprocess, "run", side_effect=fake_run), \
                patch.object(sync_usage, "count_codex_image_files_per_day",
                             return_value={}):
            cc, cx, op = sync_usage.fetch_daily_since(date(2026, 1, 1))
        self.captured = captured
        return {
            "claude": cc[0] if cc else None,
            "codex": cx[0] if cx else None,
            "opencode": op[0] if op else None,
        }

    def test_a_zero_ccusage_cost_falls_back_to_the_official_standard_rate(self) -> None:
        out = self.fetch({
            "claude": [
                {"modelName": "claude-opus-5", "inputTokens": 100, "cost": 0.0},
                {"modelName": "claude-sonnet-5", "inputTokens": 50, "cost": 1.03},
            ],
            "codex": [
                {"modelName": "gpt-5.5", "inputTokens": 10, "cost": 2.0},
            ],
        })
        self.assertTrue(out["claude"]["costTrusted"])
        self.assertAlmostEqual(out["claude"]["totalCost"], 1.0305)
        self.assertTrue(out["codex"]["costTrusted"])

    def test_a_fully_priced_day_is_trusted(self) -> None:
        out = self.fetch({
            "claude": [
                {"modelName": "claude-opus-5", "inputTokens": 100, "cost": 4.0},
            ],
            "codex": [
                {"modelName": "gpt-5.5", "inputTokens": 10, "cost": 2.0},
            ],
        })
        self.assertTrue(out["claude"]["costTrusted"])
        self.assertTrue(out["codex"]["costTrusted"])

    def test_a_model_upstream_never_prices_does_not_taint_its_day(self) -> None:
        out = self.fetch({"codex": [
            {"modelName": "codex-auto-review", "inputTokens": 500, "cost": 0.0},
            {"modelName": "gpt-5.5", "inputTokens": 10, "cost": 2.0},
        ]})
        self.assertTrue(out["codex"]["costTrusted"])
        self.assertEqual(out["codex"]["costSource"], "unpriced")

    def test_a_model_with_no_usage_does_not_taint_its_day(self) -> None:
        out = self.fetch({"claude": [
            {"modelName": "claude-opus-5", "inputTokens": 0, "cost": 0.0},
            {"modelName": "claude-sonnet-5", "inputTokens": 50, "cost": 1.5},
        ]})
        self.assertTrue(out["claude"]["costTrusted"])

    def test_claude_now_carries_a_per_model_breakdown(self) -> None:
        # Claude was the one agent that stored only totals; it now mirrors codex
        # and opencode with a per-model token map.
        out = self.fetch({"claude": [
            {"modelName": "claude-opus-5", "inputTokens": 100, "cost": 4.0},
            {"modelName": "claude-sonnet-5", "outputTokens": 50, "cost": 1.5},
        ]})
        self.assertEqual(
            out["claude"]["models"],
            {
                "claude-opus-5": {
                    "totalTokens": 100, "inputTokens": 100, "outputTokens": 0,
                    "cacheCreationTokens": 0, "cacheReadTokens": 0,
                },
                "claude-sonnet-5": {
                    "totalTokens": 50, "inputTokens": 0, "outputTokens": 50,
                    "cacheCreationTokens": 0, "cacheReadTokens": 0,
                },
            },
        )

    def test_routed_models_stay_with_the_invoking_agent(self) -> None:
        out = self.fetch({
            "claude": [
                {"modelName": "agnes-2.0-flash", "inputTokens": 100, "cost": 0.0},
                {"modelName": "gpt-5.5", "outputTokens": 20, "cost": 0.4},
            ],
            "codex": [
                {"modelName": "gpt-5.5", "inputTokens": 30, "cost": 0.6},
            ],
        })
        self.assertEqual(out["claude"]["totalTokens"], 120)
        self.assertEqual(out["claude"]["models"]["agnes-2.0-flash"]["totalTokens"], 100)
        self.assertEqual(out["claude"]["models"]["gpt-5.5"]["totalTokens"], 20)
        self.assertEqual(out["codex"]["totalTokens"], 30)
        self.assertEqual(out["codex"]["models"]["gpt-5.5"]["totalTokens"], 30)

    def test_requests_exact_agent_breakdowns(self) -> None:
        self.fetch({})
        self.assertIn("--by-agent", self.captured["cmd"])
        self.assertIn("--offline", self.captured["cmd"])
        self.assertIn("--config", self.captured["cmd"])


class TraexFetchTests(unittest.TestCase):
    """fetch_codex_home_daily reads traex's Codex-format sessions via CODEX_HOME.

    The `codex daily` schema differs from the unified `daily`: dates arrive under
    `date`, rows carry per-model tokens plus a single row-level `costUSD`, and
    there is no per-model cost. Every model is a Codex-family model, so the whole
    row is one bucket with no agent classification.
    """

    def fetch(self, daily: list[dict]) -> list[dict]:
        payload = json.dumps({"daily": daily})
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return completed

        with patch.object(sync_usage.subprocess, "run", side_effect=fake_run):
            out = sync_usage.fetch_codex_home_daily(
                date(2026, 1, 1), Path("/some/trae/home")
            )
        self.captured = captured
        return out

    def test_it_points_ccusage_at_the_given_codex_home(self) -> None:
        self.fetch([])
        self.assertEqual(self.captured["env"]["CODEX_HOME"], "/some/trae/home")
        # Uses the codex subcommand, not the unified daily.
        self.assertEqual(self.captured["cmd"][:3], ["ccusage", "codex", "daily"])

    def test_a_priced_day_is_trusted(self) -> None:
        out = self.fetch([{
            "date": "2026-08-07", "totalTokens": 1000, "costUSD": 999.0,
            "models": {"GPT-5.5": {"totalTokens": 1000, "inputTokens": 1000}},
        }])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["totalTokens"], 1000)
        self.assertEqual(out[0]["totalCost"], 0.005)
        self.assertTrue(out[0]["costTrusted"])
        self.assertEqual(out[0]["models"]["GPT-5.5"]["inputTokens"], 1000)

    def test_alias_without_component_tokens_is_explicitly_zero(self) -> None:
        # The alias resolves to Claude, but a total-only model bucket cannot be
        # split into billable input/output components, so its official cost is 0.
        out = self.fetch([{
            "date": "2026-08-07", "totalTokens": 4000, "costUSD": 0,
            "models": {"openrouter-3o": {"totalTokens": 4000}},
        }])
        self.assertEqual(out[0]["totalTokens"], 4000)
        self.assertTrue(out[0]["costTrusted"])
        self.assertEqual(out[0]["totalCost"], 0.0)

    def test_a_mixed_day_is_summed_from_official_per_model_costs(self) -> None:
        # Ignore ccusage's unsplittable row cost and sum the detailed model buckets.
        # The GPT input is priced; the total-only alias contributes explicit zero.
        out = self.fetch([{
            "date": "2026-08-07", "totalTokens": 5000, "costUSD": 2.5,
            "models": {
                "gpt-5.5": {"totalTokens": 1000, "inputTokens": 1000},
                "openrouter-3o": {"totalTokens": 4000},
            },
        }])
        self.assertEqual(out[0]["totalTokens"], 5000)
        self.assertEqual(out[0]["totalCost"], 0.005)
        self.assertTrue(out[0]["costTrusted"])
        self.assertEqual(out[0]["costSource"], "unpriced")

    def test_public_traex_models_use_their_official_component_rates(self) -> None:
        out = self.fetch([{
            "date": "2026-08-11", "totalTokens": 3_000_000, "costUSD": 999.0,
            "models": {
                "kimi-k2.5": {"totalTokens": 1_000_000, "inputTokens": 1_000_000},
                "minimax-m2.7": {
                    "totalTokens": 1_000_000, "outputTokens": 1_000_000,
                },
                "gemini-3-flash": {
                    "totalTokens": 1_000_000, "cacheReadTokens": 1_000_000,
                },
            },
        }])
        self.assertAlmostEqual(out[0]["totalCost"], 1.85)
        self.assertEqual(out[0]["costSource"], "official")

    def test_an_empty_home_yields_no_entries(self) -> None:
        self.assertEqual(self.fetch([]), [])

    def test_unmapped_opaque_slugs_are_logged(self) -> None:
        # Seed/Doubao/Qwen have no registered official price and bill zero. The
        # slug is logged so a new family gets noticed instead of disappearing.
        with patch.object(sync_usage.sys, "stderr", io.StringIO()) as err:
            out = self.fetch([{
                "date": "2026-08-07", "totalTokens": 3000, "costUSD": 1.0,
                "models": {
                    "gpt-5.5": {"totalTokens": 1000},
                    "seed-1.6": {"totalTokens": 2000},
                },
            }])
        self.assertTrue(out[0]["costTrusted"])
        self.assertIn("seed-1.6", err.getvalue())
        self.assertNotIn("gpt-5.5", err.getvalue())


class LowercasedCodexHomeTests(unittest.TestCase):
    """The mirror that lets ccusage price TRAE CLI's capitalised model names.

    ccusage's price lookup is case-sensitive against lowercase slugs, but TRAE CLI
    logs `GPT-5.5`, `Gemini-3-Flash-Preview`, etc. The mirror rewrites only the
    `"model"` field to lowercase, never touching the real source tree.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.sessions = self.home / "sessions" / "2026" / "08" / "07"
        self.sessions.mkdir(parents=True)

    def write(self, name: str, text: str) -> Path:
        path = self.sessions / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_only_the_model_field_is_lowercased(self) -> None:
        original = (
            '{"model":"GPT-5.5","text":"Keep This CASED","payload":"Mixed-Case"}\n'
            '{"model":"Gemini-3-Flash-Preview"}\n'
        )
        src = self.write("rollout-a.jsonl", original)
        with sync_usage._lowercased_codex_home(self.home) as mirror:
            mirrored = (mirror / "sessions" / "2026" / "08" / "07" / "rollout-a.jsonl").read_text()
        # Model names dropped to lowercase...
        self.assertIn('"model":"gpt-5.5"', mirrored)
        self.assertIn('"model":"gemini-3-flash-preview"', mirrored)
        # ...but nothing else changed, and the real source file is untouched.
        self.assertIn('"text":"Keep This CASED"', mirrored)
        self.assertIn('"payload":"Mixed-Case"', mirrored)
        self.assertEqual(src.read_text(encoding="utf-8"), original)

    def test_normalise_model_lowercases_and_resolves_aliases(self) -> None:
        # Real names only need casing; opaque aliases resolve to real Opus slugs.
        self.assertEqual(sync_usage._normalise_model("GPT-5.5"), "gpt-5.5")
        self.assertEqual(sync_usage._normalise_model("openrouter-1o"), "claude-opus-4-6")
        self.assertEqual(sync_usage._normalise_model("openrouter-2o"), "claude-opus-4-7")
        self.assertEqual(sync_usage._normalise_model("openrouter-3o"), "claude-opus-4-8")
        self.assertEqual(
            sync_usage._normalise_model("OpenRouter-3o__max"), "claude-opus-4-8"
        )
        # An unmapped opaque slug is only lowercased, staying unpriced.
        self.assertEqual(sync_usage._normalise_model("Seed-1.6"), "seed-1.6")

    def test_normalise_model_collapses_traex_gemini_display_aliases(self) -> None:
        self.assertEqual(
            sync_usage._normalise_model("gemini-3.1-pro"),
            "gemini-3.1-pro-preview",
        )
        self.assertEqual(
            sync_usage._normalise_model("Gemini-3-Flash"),
            "gemini-3-flash-preview",
        )

    def test_opaque_claude_aliases_resolve_to_real_opus_slugs(self) -> None:
        # openrouter-1o/2o/3o front Anthropic Opus 4.6/4.7/4.8; suffixed variants
        # (__max) and capitalisation must resolve to the same real slug so ccusage
        # can price them.
        original = (
            '{"model":"openrouter-1o"}\n'
            '{"model":"openrouter-2o"}\n'
            '{"model":"openrouter-3o"}\n'
            '{"model":"openrouter-3o__max"}\n'
            '{"model":"OpenRouter-3o"}\n'
        )
        self.write("rollout-alias.jsonl", original)
        with sync_usage._lowercased_codex_home(self.home) as mirror:
            mirrored = (
                mirror / "sessions" / "2026" / "08" / "07" / "rollout-alias.jsonl"
            ).read_text()
        self.assertIn('"model":"claude-opus-4-6"', mirrored)
        self.assertIn('"model":"claude-opus-4-7"', mirrored)
        # 3o, its __max variant, and the capitalised form all collapse to 4-8.
        self.assertEqual(mirrored.count('"model":"claude-opus-4-8"'), 3)
        # No unresolved alias slug survives into what ccusage reads.
        self.assertNotIn("openrouter", mirrored)

    def test_a_source_without_sessions_yields_an_empty_mirror(self) -> None:
        empty = Path(self._tmp.name) / "no-sessions-here"
        empty.mkdir()
        with sync_usage._lowercased_codex_home(empty) as mirror:
            self.assertFalse((mirror / "sessions").exists())

    def test_lowercase_flag_routes_through_a_mirror(self) -> None:
        # With lowercase_models set, ccusage must be pointed at a tempdir mirror,
        # not the real home passed in.
        self.write("rollout-b.jsonl", '{"model":"GPT-5.5"}\n')
        payload = json.dumps({"daily": []})
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["home"] = kwargs.get("env", {}).get("CODEX_HOME")
            return completed

        with patch.object(sync_usage.subprocess, "run", side_effect=fake_run):
            sync_usage.fetch_codex_home_daily(
                date(2026, 1, 1), self.home, lowercase_models=True
            )
        self.assertIsNotNone(seen["home"])
        self.assertNotEqual(seen["home"], str(self.home))


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
        self.assertEqual(
            self.read()["2026-07-25"]["models"],
            {"gpt-5.5": {"totalTokens": 722_000_000}},
        )

    def test_a_day_the_fetch_dropped_keeps_its_stored_value(self) -> None:
        sync_usage.merge_with_cumulative(
            [{"date": "2026-07-26", "totalTokens": 100, "totalCost": 1.0}],
            self.store,
            reconcile_since=date(2026, 7, 18),
        )
        self.assertEqual(self.read()["2026-07-25"]["totalTokens"], 722_000_000)


if __name__ == "__main__":
    unittest.main()
