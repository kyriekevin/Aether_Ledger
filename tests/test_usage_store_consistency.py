from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import usage_store


class CostCoverageTests(unittest.TestCase):
    def test_combined_model_history_does_not_claim_a_fully_priced_observation(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "codex.json"
            for tokens, cost, models in [
                (100, 1.0, {"model-a": {"totalTokens": 100}}),
                (120, 2.4, {"model-b": {"totalTokens": 120}}),
            ]:
                usage_store.merge_with_cumulative([{
                    "date": "2026-09-07", "totalTokens": tokens, "totalCost": cost,
                    "models": models, "costSource": "official",
                }], path)
            stored = json.loads(path.read_text())["2026-09-07"]
            self.assertEqual(stored["totalTokens"], 220)
            self.assertEqual(stored["totalCost"], 2.4)
            self.assertEqual(stored["costSource"], "unpriced")
            # A later complete read can repair the cost at the same token total.
            usage_store.merge_with_cumulative([{
                "date": "2026-09-07", "totalTokens": 220, "totalCost": 3.4,
                "models": stored["models"], "costSource": "official",
            }], path)
            self.assertEqual(json.loads(path.read_text())["2026-09-07"]["totalCost"], 3.4)
            self.assertEqual(json.loads(path.read_text())["2026-09-07"]["costSource"], "official")
