from __future__ import annotations

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from render_dashboard import (  # noqa: E402
    AllocationTotals,
    DailyTotals,
    _compact_number,
    aggregate_allocation,
    aggregate_daily,
    aggregate_topology,
    generate,
    generate_allocation,
    generate_allocation_history,
    generate_runtime_history,
    generate_runtime_profile,
    generate_topology,
    generate_topology_history,
    render_allocation_svg,
    render_allocation_history_svg,
    render_runtime_history_svg,
    render_runtime_profile_svg,
    render_svg,
    render_topology_svg,
    render_topology_history_svg,
)


class AggregateDailyTests(unittest.TestCase):
    def test_compact_number_keeps_significant_trailing_zeroes(self) -> None:
        self.assertEqual(_compact_number(130_000_000), "130M")
        self.assertEqual(_compact_number(1_200_000_000), "1.2B")

    def test_sums_only_canonical_agent_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data" / "work").mkdir(parents=True)
            (root / "data" / "personal").mkdir()
            (root / "data" / "work" / "claude.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 100, "totalCost": 1.25}}),
                encoding="utf-8",
            )
            (root / "data" / "personal" / "codex.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 250, "totalCost": 2.5}}),
                encoding="utf-8",
            )
            (root / "data" / "personal" / "codex_by_repo.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 999_999}}), encoding="utf-8"
            )

            self.assertEqual(
                aggregate_daily(root),
                {date(2026, 8, 1): DailyTotals(tokens=350, cost=3.75)},
            )

    def test_rejects_malformed_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "work" / "claude.json"
            path.parent.mkdir(parents=True)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "date-keyed object"):
                aggregate_daily(Path(directory))


class AggregateTopologyTests(unittest.TestCase):
    def test_keeps_public_role_agent_and_recent_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "data" / "work"
            personal = root / "data" / "personal"
            devbox = root / "data" / "devbox"
            trail = root / "data" / "trail" / "node-opaque"
            work.mkdir(parents=True)
            personal.mkdir()
            devbox.mkdir()
            trail.mkdir(parents=True)
            (work / "claude.json").write_text(
                json.dumps(
                    {
                        "2026-07-01": {"totalTokens": 100},
                        "2026-08-01": {"totalTokens": 50},
                    }
                ),
                encoding="utf-8",
            )
            (personal / "codex.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 250}}),
                encoding="utf-8",
            )
            (personal / "opencode.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 25}}),
                encoding="utf-8",
            )
            (trail / "codex.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 400}}),
                encoding="utf-8",
            )
            (devbox / "codex.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 60}}),
                encoding="utf-8",
            )

            totals = aggregate_topology(root, date(2026, 8, 1))

            self.assertEqual(totals.recent_start, date(2026, 7, 3))
            self.assertEqual(totals.recent_roles["work"], 50)
            self.assertEqual(totals.recent_agents["claude"], 50)
            self.assertEqual(totals.recent_agents["codex"], 710)
            self.assertEqual(totals.recent_agents["legacy"], 25)
            self.assertEqual(totals.recent_topology[("work", "claude")], 50)
            self.assertEqual(totals.recent_topology[("personal", "codex")], 250)
            self.assertEqual(totals.recent_topology[("personal", "legacy")], 25)
            self.assertEqual(totals.recent_topology[("development", "codex")], 460)
            self.assertEqual(totals.window_starts[0], date(2026, 6, 7))
            self.assertEqual(totals.weekly_topology[3][("work", "claude")], 100)
            self.assertEqual(
                totals.weekly_topology[7][("development", "codex")], 460
            )


class AggregateAllocationTests(unittest.TestCase):
    def test_keeps_only_privacy_safe_recent_routing_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "personal" / "codex.json"
            store.parent.mkdir(parents=True)
            store.write_text(json.dumps({
                "2026-07-01": {
                    "totalTokens": 999,
                    "models": {"prior-example": {
                        "totalTokens": 999,
                        "inputTokens": 99,
                        "outputTokens": 100,
                        "cacheCreationTokens": 100,
                        "cacheReadTokens": 700,
                    }},
                    "routing": {
                        "efforts": {"medium": {
                            "turns": 3, "totalTokens": 999,
                            "reasoningCalls": 3,
                            "reasoningOutputTokens": 30,
                        }},
                        "speeds": {"standard": {"turns": 3, "totalTokens": 999}},
                    },
                    "quota": {"windows": {"300": 40.0}, "limitReached": False},
                },
                "2026-08-01": {
                    "totalTokens": 100,
                    "models": {
                        "gpt-example": {
                            "totalTokens": 100,
                            "inputTokens": 10,
                            "outputTokens": 5,
                            "cacheCreationTokens": 15,
                            "cacheReadTokens": 70,
                        }
                    },
                    "routing": {
                        "efforts": {
                            "low": {
                                "turns": 2, "totalTokens": 100,
                                "reasoningCalls": 2,
                                "reasoningOutputTokens": 4,
                            }
                        },
                        "speeds": {"fast": {"turns": 2, "totalTokens": 100}},
                    },
                    "quota": {
                        "windows": {"300": 85.0, "10080": 64.0},
                        "limitReached": True,
                    },
                },
            }), encoding="utf-8")
            (store.parent / "claude.json").write_text(json.dumps({
                "2026-08-01": {
                    "totalTokens": 40,
                    "models": {"claude-example": {
                        "totalTokens": 40,
                        "inputTokens": 4,
                        "outputTokens": 6,
                        "cacheCreationTokens": 10,
                        "cacheReadTokens": 20,
                    }},
                    "routing": {
                        "efforts": {"xhigh": {
                            "turns": 1, "totalTokens": 40,
                            "reasoningCalls": 1,
                            "reasoningOutputTokens": 12,
                        }},
                        "speeds": {"standard": {"turns": 1, "totalTokens": 40}},
                    },
                }
            }), encoding="utf-8")
            (store.parent / "traex.json").write_text(json.dumps({
                "2026-08-01": {
                    "totalTokens": 60,
                    "models": {"internal-model": {
                        "totalTokens": 60,
                        "inputTokens": 5,
                        "outputTokens": 5,
                        "cacheCreationTokens": 0,
                        "cacheReadTokens": 50,
                    }},
                    "routing": {
                        "efforts": {"medium": {
                            "turns": 1, "totalTokens": 60,
                            "reasoningCalls": 1,
                            "reasoningOutputTokens": 3,
                        }}
                    },
                }
            }), encoding="utf-8")

            totals = aggregate_allocation(root, date(2026, 8, 1))

            self.assertEqual(totals.agent_tokens["codex"], 100)
            self.assertEqual(totals.model_tokens[("codex", "gpt-example")], 100)
            self.assertEqual(totals.efforts["codex"]["low"]["calls"], 2)
            self.assertEqual(totals.efforts["codex"]["low"]["reasoningCalls"], 2)
            self.assertEqual(totals.speeds["codex"]["fast"]["totalTokens"], 100)
            self.assertEqual(totals.quota_windows["codex"], {300: 85.0, 10080: 64.0})
            self.assertEqual(totals.latest_quota_day["codex"], date(2026, 8, 1))
            self.assertEqual(
                totals.latest_quota_windows["codex"], {300: 85.0, 10080: 64.0}
            )
            self.assertIsNone(totals.latest_quota_day["claude"])
            self.assertEqual(totals.latest_quota_windows["claude"], {})
            self.assertEqual(totals.quota_observed_days["codex"], 1)
            self.assertEqual(totals.quota_pressure_days["codex"], 1)
            self.assertEqual(totals.quota_limit_days["codex"], 1)
            self.assertEqual(totals.efforts["claude"]["xhigh"]["calls"], 1)
            self.assertEqual(totals.speeds["claude"]["standard"]["calls"], 1)
            self.assertEqual(totals.efforts["traex"]["medium"]["calls"], 1)
            self.assertEqual(len(totals.trend_starts), 8)
            self.assertEqual(totals.trend_starts[0], date(2026, 6, 7))
            self.assertEqual(
                totals.weekly_model_tokens[3][("codex", "prior-example")], 999
            )
            self.assertEqual(
                totals.weekly_model_tokens[7][("codex", "gpt-example")], 100
            )
            self.assertIn("codex", totals.weekly_model_observed[3])
            self.assertIn("traex", totals.weekly_model_observed[7])
            self.assertEqual(totals.weekly_effort_calls[3][("codex", "medium")], 3)
            self.assertEqual(totals.weekly_reasoning_calls[3]["codex"], 3)
            self.assertEqual(totals.weekly_reasoning[3]["codex"], 30)
            self.assertEqual(totals.weekly_speed_calls[7][("codex", "fast")], 2)
            self.assertEqual(totals.weekly_quota_observed_days[7]["codex"], 1)
            self.assertEqual(totals.weekly_quota_pressure_days[7]["codex"], 1)
            self.assertEqual(totals.weekly_quota_7d_peak[7]["codex"], 64.0)


class DashboardTests(unittest.TestCase):
    def test_svg_contains_accessible_stats_and_daily_cells(self) -> None:
        totals = {
            date(2026, 7, 31): DailyTotals(tokens=1_500_000, cost=4.25),
            date(2026, 8, 1): DailyTotals(tokens=2_000_000, cost=7.5),
        }
        svg = render_svg(totals, date(2026, 8, 1))
        self.assertIn("<title id=\"title\">", svg)
        for label in ("Latest · Aug 1", "Month · Aug", "Lifetime", "Peak", "Active days"):
            self.assertIn(label, svg)
        self.assertNotIn(">Today</text>", svg)
        self.assertIn("$11.75", svg)
        self.assertIn("2 days", svg)
        self.assertIn('data-date="2026-08-01"', svg)
        self.assertIn('data-tokens="2000000"', svg)
        for weekday in ("Mon", "Wed", "Fri"):
            self.assertIn(f">{weekday}</text>", svg)
        self.assertNotIn("Daily · Asia/Shanghai", svg)
        self.assertNotIn("streak", svg.lower())
        self.assertIn("@media (prefers-color-scheme: dark)", svg)
        self.assertIn('class="dashboard-background"', svg)
        self.assertIn('class="dashboard-border"', svg)
        for color in ("#eff1f5", "#ccd0da", "#179299", "#1e1e2e", "#313244", "#94e2d5"):
            self.assertIn(color, svg)
        for level in range(5):
            self.assertIn(f'class="heatmap-level-{level}"', svg)

    def test_generate_defaults_to_latest_recorded_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "work" / "codex.json"
            store.parent.mkdir(parents=True)
            store.write_text(
                json.dumps({"2026-07-31": {"totalTokens": 100}}), encoding="utf-8"
            )
            output = root / "assets" / "dashboard.svg"

            self.assertTrue(generate(root, output))
            self.assertIn("through 2026-07-31", output.read_text(encoding="utf-8"))
            self.assertFalse(generate(root, output, check=True))

    def test_topology_svg_exposes_cross_dimension_shares_without_node_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "trail" / "node-private" / "codex.json"
            store.parent.mkdir(parents=True)
            store.write_text(
                json.dumps({"2026-08-01": {"totalTokens": 2_000_000}}),
                encoding="utf-8",
            )
            (store.parent / "traex.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 1}}),
                encoding="utf-8",
            )
            totals = aggregate_topology(root, date(2026, 8, 1))
            svg = render_topology_svg(totals)
            history_svg = render_topology_history_svg(totals)

            ET.fromstring(svg)
            ET.fromstring(history_svg)
            self.assertIn("Recent compute topology", svg)
            self.assertIn("Development", svg)
            self.assertIn("100.0% · 2M", svg)
            self.assertIn("&lt;0.1%", svg)
            self.assertNotIn("node-private", svg)
            self.assertIn("@media (prefers-color-scheme: dark)", svg)
            self.assertIn('class="dashboard-background"', svg)
            self.assertIn('data-role="development"', svg)
            self.assertIn('data-agent="codex"', svg)
            self.assertIn("hue is harness", svg)
            self.assertNotIn("prior 30d", svg)
            self.assertNotRegex(svg, r"[+-]?\d+(?:\.\d+)?pp\b")
            for role in ("work", "personal", "development"):
                self.assertIn(f'class="topology-{role}"', svg)
                self.assertIn(f".topology-{role}", svg)
            for level in range(1, 5):
                self.assertIn(f".topology-level-{level}", svg)
            for color in ("#fe640b", "#1e66f5", "#8839ef", "#fab387", "#89b4fa", "#cba6f7"):
                self.assertIn(color, svg)
            self.assertIn(".heatmap-level-0", svg)
            self.assertIn(".topology-label-0 { fill: #6c6f85; }", svg)
            self.assertIn(".topology-label-0 { fill: #a6adc8; }", svg)
            self.assertIn(".topology-label-agent-claude-4", svg)
            self.assertNotIn(".heatmap-label-0", svg)
            self.assertIn("Topology history", history_svg)
            self.assertIn("previous 4 weeks vs latest 4 weeks", history_svg)
            self.assertIn("PREVIOUS 4 WEEKS", history_svg)
            self.assertIn("LATEST 4 WEEKS", history_svg)
            self.assertIn('data-role="development"', history_svg)
            self.assertIn('data-agent="codex"', history_svg)
            self.assertIn('data-week="2026-07-26"', history_svg)

    def test_topology_reports_an_empty_recent_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "work" / "claude.json"
            store.parent.mkdir(parents=True)
            store.write_text(
                json.dumps({"2026-06-01": {"totalTokens": 500}}), encoding="utf-8"
            )
            totals = aggregate_topology(root, date(2026, 8, 1))

            topology = render_topology_svg(totals)

            ET.fromstring(topology)
            self.assertIn("No recent activity", topology)
            self.assertNotIn('data-agent="', topology)

    def test_generate_topology_defaults_to_latest_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "work" / "claude.json"
            store.parent.mkdir(parents=True)
            store.write_text(
                json.dumps({"2026-07-31": {"totalTokens": 100}}),
                encoding="utf-8",
            )
            output = root / "assets" / "topology.svg"

            self.assertTrue(generate_topology(root, output))
            self.assertIn("through 2026-07-31", output.read_text(encoding="utf-8"))
            self.assertFalse(generate_topology(root, output, check=True))

            history_output = root / "assets" / "topology-history.svg"
            self.assertTrue(generate_topology_history(root, history_output))
            self.assertIn(
                "through 2026-07-31",
                history_output.read_text(encoding="utf-8"),
            )
            self.assertFalse(
                generate_topology_history(root, history_output, check=True)
            )

    def test_allocation_and_runtime_views_split_models_from_routing(self) -> None:
        allocation = AllocationTotals(
            as_of=date(2026, 8, 1),
            recent_start=date(2026, 7, 3),
            agent_tokens={
                "claude": 100, "codex": 200, "traex": 50, "legacy": 0,
            },
            model_tokens={
                ("claude", "claude-opus-example"): 100,
                ("codex", "gpt-example"): 200,
                ("traex", "cheap-example"): 50,
            },
            trend_starts=tuple(
                date(2026, 6, 7) + timedelta(days=index * 7)
                for index in range(8)
            ),
            weekly_model_tokens=tuple(
                {
                    ("claude", "claude-opus-example"): (index + 1) * 10,
                    ("codex", "gpt-example"): (index + 1) * 20,
                    ("traex", "cheap-example"): (index + 1) * 5,
                }
                for index in range(8)
            ),
            weekly_model_observed=tuple(
                {"claude", "codex", "traex"} for _ in range(8)
            ),
            weekly_effort_calls=tuple(
                {
                    ("claude", "xhigh"): (index + 1) * 8,
                    ("codex", "low"): (index + 1) * 10,
                    ("codex", "medium"): (index + 1) * 5,
                    ("traex", "medium"): (index + 1) * 6,
                }
                for index in range(8)
            ),
            weekly_reasoning_calls=tuple(
                {"claude": index + 1, "codex": index + 1, "traex": index + 1}
                for index in range(8)
            ),
            weekly_reasoning=tuple(
                {
                    "claude": (index + 1) * 20,
                    "codex": (index + 1) * 10,
                    "traex": (index + 1) * 5,
                }
                for index in range(8)
            ),
            weekly_effort_observed=tuple(
                {"claude", "codex", "traex"} for _ in range(8)
            ),
            weekly_speed_calls=tuple(
                {
                    ("claude", "standard"): (index + 1) * 10,
                    ("codex", "standard"): (index + 1) * 5,
                    ("codex", "fast"): (index + 1) * 5,
                }
                for index in range(8)
            ),
            weekly_speed_observed=tuple(
                {"claude", "codex"} for _ in range(8)
            ),
            weekly_quota_observed_days=tuple(
                {"codex": 7, "traex": 7}
                for _ in range(8)
            ),
            weekly_quota_pressure_days=tuple(
                {"codex": index % 7, "traex": (index + 1) % 7}
                for index in range(8)
            ),
            weekly_quota_7d_peak=tuple(
                {"claude": 10.0 + index, "codex": 20.0 + index}
                for index in range(8)
            ),  # the Claude series is deliberate: the renderer must drop it
            weekly_quota_observed=tuple(
                {"codex", "traex"} for _ in range(8)
            ),
            efforts={
                agent: {
                    effort: {
                        "calls": 0,
                        "totalTokens": 0,
                        "reasoningCalls": 0,
                        "reasoningOutputTokens": 0,
                    }
                    for effort in ("none", "low", "medium", "high", "xhigh", "max")
                }
                for agent in ("claude", "codex", "traex", "legacy")
            },
            speeds={
                agent: {
                    speed: {"calls": 0, "totalTokens": 0}
                    for speed in ("standard", "fast")
                }
                for agent in ("claude", "codex", "traex", "legacy")
            },
            quota_windows={
                agent: {} for agent in ("claude", "codex", "traex", "legacy")
            },
            latest_quota_day={
                agent: None for agent in ("claude", "codex", "traex", "legacy")
            },
            latest_quota_windows={
                agent: {} for agent in ("claude", "codex", "traex", "legacy")
            },
            quota_observed_days={
                agent: 0 for agent in ("claude", "codex", "traex", "legacy")
            },
            quota_pressure_days={
                agent: 0 for agent in ("claude", "codex", "traex", "legacy")
            },
            quota_limit_days={
                agent: 0 for agent in ("claude", "codex", "traex", "legacy")
            },
        )
        allocation.efforts["claude"]["xhigh"].update(
            calls=2,
            totalTokens=100,
            reasoningCalls=2,
            reasoningOutputTokens=100,
        )
        allocation.efforts["codex"]["low"].update(
            calls=3,
            totalTokens=120,
            reasoningCalls=3,
            reasoningOutputTokens=30,
        )
        allocation.efforts["codex"]["medium"].update(
            calls=1,
            totalTokens=80,
            reasoningCalls=1,
            reasoningOutputTokens=20,
        )
        allocation.efforts["traex"]["medium"].update(
            calls=2,
            totalTokens=80,
            reasoningCalls=2,
            reasoningOutputTokens=10,
        )
        allocation.speeds["claude"]["standard"].update(calls=2, totalTokens=100)
        allocation.speeds["codex"]["standard"].update(calls=1, totalTokens=100)
        allocation.speeds["codex"]["fast"].update(calls=1, totalTokens=100)
        allocation.quota_windows["codex"][300] = 68.0
        allocation.latest_quota_day["codex"] = date(2026, 8, 1)
        allocation.latest_quota_windows["codex"] = {300: 68.0, 10080: 64.0}
        allocation.quota_observed_days["codex"] = 10
        allocation.quota_pressure_days["codex"] = 3
        allocation.quota_limit_days["codex"] = 1

        allocation_svg = render_allocation_svg(allocation)
        allocation_history_svg = render_allocation_history_svg(allocation)
        profile_svg = render_runtime_profile_svg(allocation)
        history_svg = render_runtime_history_svg(allocation)

        ET.fromstring(allocation_svg)
        ET.fromstring(allocation_history_svg)
        ET.fromstring(profile_svg)
        ET.fromstring(history_svg)
        self.assertIn("Model allocation", allocation_svg)
        self.assertIn("current 30-day model mix", allocation_svg)
        self.assertIn("30D SHARE", allocation_svg)
        self.assertIn("claude-opus-example", allocation_svg)
        self.assertIn("cheap-example", allocation_svg)
        self.assertIn('data-model="gpt-example"', allocation_svg)
        self.assertNotIn("12-WEEK", allocation_svg)
        self.assertNotIn("prior 30d", allocation_svg)
        self.assertNotRegex(allocation_svg, r"[+-]?\d+(?:\.\d+)?pp\b")
        self.assertNotIn("Observed routing signals", allocation_svg)
        self.assertNotIn("cache read", allocation_svg)
        self.assertIn("Model allocation history", allocation_history_svg)
        self.assertIn("Top 3 models + Other", allocation_history_svg)
        self.assertIn("previous 4 weeks vs latest 4 weeks", allocation_history_svg)
        self.assertIn("PREVIOUS 4 WEEKS", allocation_history_svg)
        self.assertIn("LATEST 4 WEEKS", allocation_history_svg)
        self.assertIn('data-model="gpt-example"', allocation_history_svg)
        self.assertIn('data-week="2026-07-26"', allocation_history_svg)
        self.assertIn("Runtime profile", profile_svg)
        self.assertIn("low 75.0%", profile_svg)
        self.assertIn("50.0% fast", profile_svg)
        self.assertIn("7-day quota peak", profile_svg)
        self.assertIn("7d 64% · Aug 1", profile_svg)
        self.assertIn('data-effort="xhigh"', profile_svg)
        self.assertNotIn("Reasoning", profile_svg)
        self.assertNotIn("Thinking", profile_svg)
        self.assertNotIn("8-WEEK", profile_svg)
        self.assertIn("Runtime history", history_svg)
        self.assertIn("PREVIOUS 4 WEEKS", history_svg)
        self.assertIn('class="line-codex"', history_svg)
        self.assertIn("7-day quota peak", history_svg)
        self.assertIn("7d peak 20%", history_svg)
        # Quota is Codex-only, so a stale Claude series is charted nowhere and
        # claims no legend slot -- and the single Codex bar sits on the week's
        # centre instead of hanging off the side of an empty pair.
        self.assertNotIn("7d peak 10%", history_svg)
        self.assertNotIn('<circle class="agent-claude" cx="42" cy="456"', history_svg)
        self.assertIn('<circle class="agent-codex" cx="42" cy="456"', history_svg)
        self.assertIn('<rect class="agent-codex" x="221.0"', history_svg)
        self.assertIn('data-week="2026-07-26"', history_svg)
        self.assertNotIn("Reasoning", history_svg)
        self.assertNotIn("Thinking", history_svg)
        self.assertNotIn('<rect class="signal-level-', history_svg)
        for svg in (profile_svg, history_svg):
            self.assertNotIn("cache read", svg)
            self.assertNotIn("not exposed", svg)
            self.assertNotIn("Token efficiency", svg)
            self.assertNotIn("Token flow", svg)
        for svg in (
            allocation_svg,
            allocation_history_svg,
            profile_svg,
            history_svg,
        ):
            self.assertNotIn("private-repo", svg)
            self.assertNotIn("session-id", svg)
            self.assertIn('@media (prefers-color-scheme: dark)', svg)
            self.assertIn('data-agent="traex"', svg)

    def test_generate_allocation_defaults_to_latest_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "work" / "claude.json"
            store.parent.mkdir(parents=True)
            store.write_text(
                json.dumps({"2026-07-31": {"totalTokens": 100}}),
                encoding="utf-8",
            )
            output = root / "assets" / "allocation.svg"

            self.assertTrue(generate_allocation(root, output))
            self.assertIn("through 2026-07-31", output.read_text(encoding="utf-8"))
            self.assertFalse(generate_allocation(root, output, check=True))

            allocation_history_output = root / "assets" / "allocation-history.svg"
            self.assertTrue(
                generate_allocation_history(root, allocation_history_output)
            )
            self.assertIn(
                "through 2026-07-31",
                allocation_history_output.read_text(encoding="utf-8"),
            )
            self.assertFalse(
                generate_allocation_history(
                    root, allocation_history_output, check=True
                )
            )

            profile_output = root / "assets" / "runtime-profile.svg"
            self.assertTrue(generate_runtime_profile(root, profile_output))
            self.assertIn(
                "through 2026-07-31",
                profile_output.read_text(encoding="utf-8"),
            )
            self.assertFalse(
                generate_runtime_profile(root, profile_output, check=True)
            )

            history_output = root / "assets" / "runtime-history.svg"
            self.assertTrue(generate_runtime_history(root, history_output))
            self.assertIn(
                "through 2026-07-31",
                history_output.read_text(encoding="utf-8"),
            )
            self.assertFalse(
                generate_runtime_history(root, history_output, check=True)
            )

    def test_runtime_profile_omits_fast_without_speed_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "work" / "codex.json"
            store.parent.mkdir(parents=True)
            store.write_text(json.dumps({
                "2026-08-01": {
                    "totalTokens": 100,
                    "models": {"gpt-example": {"totalTokens": 100}},
                }
            }), encoding="utf-8")

            svg = render_runtime_profile_svg(
                aggregate_allocation(root, date(2026, 8, 1))
            )

            self.assertNotIn(">Fast</text>", svg)
            self.assertNotIn("0.0% fast", svg)


if __name__ == "__main__":
    unittest.main()
