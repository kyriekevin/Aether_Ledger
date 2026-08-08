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
            trail = root / "data" / "trail" / "node-opaque"
            work.mkdir(parents=True)
            personal.mkdir()
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
            (trail / "codex.json").write_text(
                json.dumps({"2026-08-02": {"totalTokens": 400}}),
                encoding="utf-8",
            )

            totals = aggregate_composition(root, date(2026, 8, 1))

            self.assertEqual(totals.recent_start, date(2026, 7, 3))
            self.assertEqual(totals.lifetime_roles["work"], 150)
            self.assertEqual(totals.recent_roles["work"], 50)
            self.assertEqual(totals.lifetime_roles["personal"], 250)
            self.assertEqual(totals.lifetime_roles["trail"], 0)
            self.assertEqual(totals.lifetime_agents["claude"], 150)
            self.assertEqual(totals.lifetime_agents["codex"], 250)
            self.assertEqual(totals.topology[("work", "claude")], 150)
            self.assertEqual(totals.topology[("personal", "codex")], 250)


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
            self.assertIn("Environment × Agent topology", svg)
            self.assertIn("Recent 30d", svg)
            self.assertIn('data-role="trail"', svg)
            self.assertIn('data-agent="codex"', svg)
            self.assertIn("Trail 100.0%", svg)
            self.assertIn("&lt;0.1%", svg)
            self.assertNotIn("node-private", svg)

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
