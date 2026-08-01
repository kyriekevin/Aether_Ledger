from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from render_dashboard import aggregate_daily, generate, render_svg, streaks  # noqa: E402


class AggregateDailyTests(unittest.TestCase):
    def test_sums_only_canonical_agent_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work").mkdir()
            (root / "personal").mkdir()
            (root / "work" / "claude.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 100}}), encoding="utf-8"
            )
            (root / "personal" / "codex.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 250}}), encoding="utf-8"
            )
            (root / "personal" / "codex_by_repo.json").write_text(
                json.dumps({"2026-08-01": {"totalTokens": 999_999}}), encoding="utf-8"
            )

            self.assertEqual(aggregate_daily(root), {date(2026, 8, 1): 350})

    def test_rejects_malformed_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "work" / "claude.json"
            path.parent.mkdir()
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "date-keyed object"):
                aggregate_daily(Path(directory))


class DashboardTests(unittest.TestCase):
    def test_current_and_longest_streaks(self) -> None:
        totals = {
            date(2026, 7, 27): 1,
            date(2026, 7, 28): 1,
            date(2026, 7, 30): 1,
            date(2026, 7, 31): 1,
        }
        self.assertEqual(streaks(totals, date(2026, 8, 1)), (2, 2))
        self.assertEqual(streaks(totals, date(2026, 8, 2)), (0, 2))

    def test_svg_contains_accessible_stats_and_daily_cells(self) -> None:
        totals = {
            date(2026, 7, 31): 1_500_000,
            date(2026, 8, 1): 2_000_000,
        }
        svg = render_svg(totals, date(2026, 8, 1))
        self.assertIn("<title id=\"title\">", svg)
        self.assertIn("Lifetime tokens", svg)
        self.assertIn('data-date="2026-08-01"', svg)
        self.assertIn('data-tokens="2000000"', svg)
        self.assertIn("Current streak", svg)

    def test_generate_defaults_to_latest_recorded_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "work" / "codex.json"
            store.parent.mkdir()
            store.write_text(
                json.dumps({"2026-07-31": {"totalTokens": 100}}), encoding="utf-8"
            )
            output = root / "assets" / "dashboard.svg"

            self.assertTrue(generate(root, output))
            self.assertIn("through 2026-07-31", output.read_text(encoding="utf-8"))
            self.assertFalse(generate(root, output, check=True))


if __name__ == "__main__":
    unittest.main()
