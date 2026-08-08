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
    DailyTotals,
    aggregate_composition,
    aggregate_daily,
    generate,
    generate_composition,
    render_composition_svg,
    render_svg,
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


class AggregateCompositionTests(unittest.TestCase):
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

            totals = aggregate_composition(root, date(2026, 8, 1))

            self.assertEqual(totals.recent_start, date(2026, 7, 3))
            self.assertEqual(totals.lifetime_roles["work"], 150)
            self.assertEqual(totals.recent_roles["work"], 50)
            self.assertEqual(totals.lifetime_roles["personal"], 275)
            self.assertEqual(totals.lifetime_roles["development"], 460)
            self.assertEqual(totals.lifetime_agents["claude"], 150)
            self.assertEqual(totals.lifetime_agents["codex"], 710)
            self.assertEqual(totals.lifetime_agents["legacy"], 25)
            self.assertEqual(totals.recent_agents["claude"], 50)
            self.assertEqual(totals.recent_agents["codex"], 710)
            self.assertEqual(totals.recent_agents["legacy"], 25)


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
        for color in ("#f6f8fa", "#e8ecf1", "#1d1e2c", "#34384a", "#a5f3fc"):
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

    def test_composition_svg_exposes_aggregates_without_node_ids(self) -> None:
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
            totals = aggregate_composition(root, date(2026, 8, 1))
            svg = render_composition_svg(totals)

            ET.fromstring(svg)
            self.assertIn("Compute composition", svg)
            self.assertIn("Recent 30d", svg)
            self.assertIn("Development 100.0%", svg)
            self.assertIn("&lt;0.1%", svg)
            self.assertIn("OPENCODE INCLUDED IN LEGACY", svg)
            self.assertNotIn("node-private", svg)
            self.assertIn("@media (prefers-color-scheme: dark)", svg)
            self.assertIn('class="dashboard-background"', svg)
            self.assertIn('class="dashboard-panel"', svg)
            self.assertIn('class="dashboard-track"', svg)

    def test_composition_reports_an_empty_recent_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "work" / "claude.json"
            store.parent.mkdir(parents=True)
            store.write_text(
                json.dumps({"2026-06-01": {"totalTokens": 500}}), encoding="utf-8"
            )
            totals = aggregate_composition(root, date(2026, 8, 1))

            composition = render_composition_svg(totals)

            ET.fromstring(composition)
            self.assertIn("500 lifetime tokens", composition)
            self.assertIn("no token activity in the trailing 30 days", composition)
            self.assertNotIn("recent activity is led by", composition)

    def test_generate_composition_defaults_to_latest_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data" / "work" / "claude.json"
            store.parent.mkdir(parents=True)
            store.write_text(
                json.dumps({"2026-07-31": {"totalTokens": 100}}),
                encoding="utf-8",
            )
            output = root / "assets" / "composition.svg"

            self.assertTrue(generate_composition(root, output))
            self.assertIn("through 2026-07-31", output.read_text(encoding="utf-8"))
            self.assertFalse(generate_composition(root, output, check=True))

if __name__ == "__main__":
    unittest.main()
