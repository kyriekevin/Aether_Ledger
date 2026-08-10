from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pricing  # noqa: E402
import update_pricing  # noqa: E402


class PricingTableTests(unittest.TestCase):
    def test_sonnet_introductory_rate_changes_on_its_effective_date(self) -> None:
        before = pricing.active_rate("claude-sonnet-5", date(2026, 8, 31))
        after = pricing.active_rate("claude-sonnet-5", date(2026, 9, 1))
        self.assertEqual((before["input"], before["output"]), (2.0, 10.0))
        self.assertEqual((after["input"], after["output"]), (3.0, 15.0))

    def test_unknown_and_explicitly_unpriced_models_cost_zero(self) -> None:
        tokens = {"inputTokens": 1_000_000}
        self.assertEqual(
            pricing.standard_cost("gpt-5.3-codex-spark", date(2026, 8, 10), tokens),
            0.0,
        )
        self.assertEqual(
            pricing.standard_cost("some-new-model", date(2026, 8, 10), tokens), 0.0
        )

    def test_ccusage_config_pins_official_prices_and_zeroes_spark(self) -> None:
        config = pricing.ccusage_config(date(2026, 8, 10))
        overrides = config["defaults"]["pricingOverrides"]
        self.assertEqual(overrides["claude-opus-5"]["inputCostPerToken"], 0.000005)
        self.assertEqual(overrides["gpt-5.3-codex-spark"]["inputCostPerToken"], 0.0)
        self.assertEqual(config["codex"]["defaults"]["speed"], "auto")

    def test_ccusage_fast_premium_is_kept_but_zero_falls_back_to_standard(self) -> None:
        tokens = {"inputTokens": 1_000_000}
        day = date(2026, 8, 10)
        self.assertEqual(
            pricing.official_cost_from_ccusage(
                "gpt-5.5", day, day, tokens, 12.5
            ),
            12.5,
        )
        self.assertEqual(
            pricing.official_cost_from_ccusage(
                "gpt-5.5", day, day, tokens, 0.0
            ),
            5.0,
        )


class OfficialPageParserTests(unittest.TestCase):
    def test_anthropic_model_id_maps_to_official_display_name(self) -> None:
        self.assertEqual(
            update_pricing.inferred_official_name(
                "claude-haiku-4-5-20251001", "anthropic"
            ),
            "Claude Haiku 4.5",
        )
        self.assertEqual(
            update_pricing.inferred_official_name("claude-opus-5", "anthropic"),
            "Claude Opus 5",
        )

    def test_anthropic_parser_selects_date_effective_sonnet_rate(self) -> None:
        source = """
| Model | Base Input Tokens | 5m Cache Writes | 1h Cache Writes | Cache Hits & Refreshes | Output Tokens |
| --- | --- | --- | --- | --- | --- |
| Claude Sonnet 5 [through August 31, 2026](x) | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.20 / MTok | $10 / MTok |
| Claude Sonnet 5 starting September 1, 2026 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.30 / MTok | $15 / MTok |
"""
        august = update_pricing.parse_anthropic(source, date(2026, 8, 10))
        september = update_pricing.parse_anthropic(source, date(2026, 9, 1))
        self.assertEqual(august["Claude Sonnet 5"]["input"], 2.0)
        self.assertEqual(september["Claude Sonnet 5"]["input"], 3.0)

    def test_openai_parser_reads_standard_long_and_fast_rates(self) -> None:
        source = """
### Standard pricing data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-5.6-sol | $5.00 | $0.50 | $6.25 | $30.00 | $10.00 | $1.00 | $12.50 | $45.00 |
### Batch pricing data
### Fast pricing data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-5.6-sol | $10.00 | $1.00 | $12.50 | $60.00 | $20.00 | $2.00 | $25.00 | $90.00 |
## Multimodal models
"""
        rate = update_pricing.parse_openai(source, date(2026, 8, 10))["gpt-5.6-sol"]
        self.assertEqual(rate["longContextThreshold"], 272000)
        self.assertEqual(rate["longOutput"], 45.0)
        self.assertEqual(rate["fastMultiplier"], 2.0)

    def test_deepseek_parser_reads_first_party_snapshot(self) -> None:
        source = """
| Model | Input / M tokens | Output / M tokens | Cache Hit / M tokens |
| --- | --- | --- | --- |
| deepseek-v4-pro | $0.435 | $0.87 | $0.003625 |
"""
        rate = update_pricing.parse_deepseek(source, date(2026, 8, 10))[
            "deepseek-v4-pro"
        ]
        self.assertEqual(rate["input"], 0.435)
        self.assertEqual(rate["cacheRead"], 0.003625)

    def test_checked_in_pricing_is_valid_json(self) -> None:
        self.assertEqual(json.loads(pricing.PRICING_PATH.read_text())["schemaVersion"], 1)


if __name__ == "__main__":
    unittest.main()
