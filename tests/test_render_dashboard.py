from __future__ import annotations

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from render_dashboard import (  # noqa: E402
    AllocationTotals,
    DailyTotals,
    aggregate_allocation,
    aggregate_topology,
    aggregate_daily,
    generate,
    generate_allocation,
    generate_topology,
    render_allocation_svg,
    render_svg,
    render_topology_svg,
)


class AggregateDailyTests(unittest.TestCase):
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


class AggregateAllocationTests(unittest.TestCase):
    def test_keeps_only_privacy_safe_recent_routing_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "personal" / "codex.json"
            store.parent.mkdir(parents=True)
            store.write_text(json.dumps({
                "2026-07-01": {"totalTokens": 999},
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
                                "reasoningOutputTokens": 4,
                            }
                        },
                        "speeds": {"fast": {"turns": 2, "totalTokens": 100}},
                    },
                    "quota": {
                        "windows": {"300": 65.0}, "limitReached": True,
                    },
                },
            }), encoding="utf-8")

            totals = aggregate_allocation(root, date(2026, 8, 1))

            self.assertEqual(totals.agent_tokens["codex"], 100)
            self.assertEqual(totals.model_tokens[("codex", "gpt-example")], 100)
            self.assertEqual(totals.efforts["low"]["turns"], 2)
            self.assertEqual(totals.speeds["fast"]["totalTokens"], 100)
            self.assertEqual(totals.components["cacheReadTokens"], 70)
            self.assertEqual(totals.quota_windows, {300: 65.0})
            self.assertEqual(totals.quota_limit_days, 1)


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

            ET.fromstring(svg)
            self.assertIn("Recent compute topology", svg)
            self.assertIn("Development", svg)
            self.assertIn("100.0% · 2M", svg)
            self.assertIn("&lt;0.1%", svg)
            self.assertNotIn("node-private", svg)
            self.assertIn("@media (prefers-color-scheme: dark)", svg)
            self.assertIn('class="dashboard-background"', svg)
            self.assertIn('data-role="development"', svg)
            self.assertIn('data-agent="codex"', svg)
            self.assertIn("hue is environment", svg)
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
            self.assertNotIn(".heatmap-label-0", svg)

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

    def test_allocation_svg_exposes_model_routing_and_efficiency_without_identity(self) -> None:
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
            efforts={
                "none": {"turns": 0, "totalTokens": 0, "reasoningOutputTokens": 0},
                "low": {"turns": 3, "totalTokens": 120, "reasoningOutputTokens": 10},
                "medium": {"turns": 1, "totalTokens": 80, "reasoningOutputTokens": 20},
                "high": {"turns": 0, "totalTokens": 0, "reasoningOutputTokens": 0},
                "xhigh": {"turns": 0, "totalTokens": 0, "reasoningOutputTokens": 0},
                "max": {"turns": 0, "totalTokens": 0, "reasoningOutputTokens": 0},
            },
            speeds={
                "standard": {"turns": 3, "totalTokens": 250},
                "fast": {"turns": 1, "totalTokens": 100},
            },
            components={
                "inputTokens": 20,
                "outputTokens": 10,
                "cacheCreationTokens": 20,
                "cacheReadTokens": 300,
            },
            quota_windows={300: 68.0},
            quota_limit_days=0,
        )

        svg = render_allocation_svg(allocation)

        ET.fromstring(svg)
        self.assertIn("Compute allocation", svg)
        self.assertIn("claude-opus-example", svg)
        self.assertIn("cheap-example", svg)
        self.assertIn("Codex effort", svg)
        self.assertIn("low 60.0%", svg)
        self.assertIn("28.6% fast", svg)
        self.assertIn("85.7%", svg)
        self.assertIn("68% peak", svg)
        self.assertNotIn("private-repo", svg)
        self.assertNotIn("session-id", svg)
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

if __name__ == "__main__":
    unittest.main()
