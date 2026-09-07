from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ccusage_runtime


class RuntimeVerificationTests(unittest.TestCase):
    def output(self, costs):
        return subprocess.CompletedProcess([], 0, json.dumps({"daily": [
            {"date": f"2026-09-{i:02d}", "costUSD": cost}
            for i, cost in enumerate(costs, 1)
        ]}))

    def test_rejects_a_build_with_the_200k_fallback(self):
        with patch.object(ccusage_runtime.subprocess, "run", return_value=self.output(
            [2.27502, 3.715, 3.71502, 7.43004]
        )):
            with self.assertRaisesRegex(ValueError, "272K/Fast"):
                ccusage_runtime.verify("ccusage")

    def test_accepts_correct_boundary_cache_and_fast_prices(self):
        with patch.object(ccusage_runtime.subprocess, "run", return_value=self.output(
            [1.15001, 1.87, 3.71502, 7.43004]
        )):
            ccusage_runtime.verify("ccusage")

    def test_empty_report_is_not_a_successful_probe(self):
        with patch.object(ccusage_runtime.subprocess, "run", return_value=self.output([])):
            with self.assertRaises(ValueError):
                ccusage_runtime.verify("ccusage")

    def test_pinned_source_matches_pricing_table(self):
        table = json.loads((Path(__file__).resolve().parents[1]
                            / "config/official-pricing.json").read_text())
        self.assertEqual(table["ccusage"]["verifiedRevision"], ccusage_runtime.REVISION)
