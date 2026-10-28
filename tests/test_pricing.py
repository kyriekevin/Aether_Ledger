from __future__ import annotations

import html
import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pricing  # noqa: E402
import update_pricing  # noqa: E402


class PricingTableTests(unittest.TestCase):
    def test_sonnet_introductory_rate_remains_active_after_august(self) -> None:
        before = pricing.active_rate("claude-sonnet-5", date(2026, 8, 31))
        after = pricing.active_rate("claude-sonnet-5", date(2026, 9, 1))
        self.assertEqual((before["input"], before["output"]), (2.0, 10.0))
        self.assertEqual((after["input"], after["output"]), (2.0, 10.0))

    def test_openai_price_refresh_uses_source_dates(self) -> None:
        old_sol = pricing.active_rate("gpt-5.6-sol", date(2026, 8, 20))
        new_sol = pricing.active_rate("gpt-5.6-sol", date(2026, 8, 21))
        self.assertEqual((old_sol["input"], old_sol["output"]), (5.0, 30.0))
        self.assertEqual((new_sol["input"], new_sol["output"]), (4.0, 20.0))
        self.assertEqual(new_sol["fastMultiplier"], 2.0)

        self.assertIsNone(
            pricing.active_rate("gpt-5.6-terra", date(2026, 8, 14))
        )
        terra = pricing.active_rate("gpt-5.6-terra", date(2026, 8, 15))
        self.assertEqual((terra["input"], terra["output"]), (2.0, 12.0))

    def test_deepseek_peak_rates_start_on_the_shanghai_effective_day(self) -> None:
        old_flash = pricing.active_rate("deepseek-v4-flash", date(2026, 8, 16))
        new_flash = pricing.active_rate("deepseek-v4-flash", date(2026, 8, 17))
        self.assertEqual((old_flash["input"], old_flash["output"]), (0.14, 0.28))
        self.assertEqual((new_flash["input"], new_flash["output"]), (0.44, 1.32))

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

    def test_public_traex_models_have_official_rates(self) -> None:
        day = date(2026, 8, 11)
        expected = {
            "gpt-5.2": (1.75, 14.0, 0.175),
            "gemini-3-flash-preview": (0.5, 3.0, 0.05),
            "gemini-3.1-pro-preview": (2.0, 12.0, 0.2),
            "kimi-k2.5": (0.6, 3.0, 0.1),
            "kimi-k2.6": (0.95, 4.0, 0.16),
            "minimax-m2.7": (0.3, 1.2, 0.06),
        }
        for model, rates in expected.items():
            with self.subTest(model=model):
                rate = pricing.active_rate(model, day)
                self.assertIsNotNone(rate)
                self.assertEqual(
                    (rate["input"], rate["output"], rate["cacheRead"]), rates
                )

    def test_ccusage_config_rejects_an_unverified_long_context_model(self) -> None:
        table = {
            "schemaVersion": 1,
            "unit": "perMillionTokens",
            "ccusage": {"longContextModels": []},
            "models": {
                "gpt-new": {
                    "rates": [{
                        "effectiveFrom": "2026-08-10",
                        "input": 1.0, "output": 2.0,
                        "cacheWrite": 1.0, "cacheRead": 0.1,
                        "longContextThreshold": 272000,
                        "longInput": 2.0, "longOutput": 3.0,
                    }]
                }
            },
        }
        with self.assertRaisesRegex(ValueError, "long-context pricing is not verified"):
            pricing.ccusage_config(date(2026, 8, 10), table)

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

    def test_anthropic_parser_marks_opus_fast_mode(self) -> None:
        source = """
| Model | Base Input Tokens | 5m Cache Writes | 1h Cache Writes | Cache Hits & Refreshes | Output Tokens |
| --- | --- | --- | --- | --- | --- |
| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok<sup>2</sup> | $20 / MTok |
| Claude Sonnet 5.5 | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.10 / MTok<sup>2</sup> | $10 / MTok |
"""
        rates = update_pricing.parse_anthropic(source, date(2026, 10, 28))
        self.assertEqual(rates["Claude Opus 5.5"], {
            "input": 4.0, "cacheWrite": 5.0, "cacheRead": 0.2, "output": 20.0,
            "fastMultiplier": 2.0,
        })
        self.assertNotIn("fastMultiplier", rates["Claude Sonnet 5.5"])

    def test_anthropic_parser_ignores_cancelled_sonnet_rate(self) -> None:
        source = """
| Model | Base Input Tokens | 5m Cache Writes | 1h Cache Writes | Cache Hits & Refreshes | Output Tokens |
| --- | --- | --- | --- | --- | --- |
| Claude Sonnet 5 | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.20 / MTok | $10 / MTok |
| Claude Sonnet 5 starting September 1, 2026 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.30 / MTok | $15 / MTok |
"""
        august = update_pricing.parse_anthropic(source, date(2026, 8, 10))
        september = update_pricing.parse_anthropic(source, date(2026, 9, 1))
        self.assertEqual(august["Claude Sonnet 5"]["input"], 2.0)
        self.assertEqual(september["Claude Sonnet 5"]["input"], 2.0)

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
### Grouped Pricing Table data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-5.6-sol | $5.00 | $0.50 | $6.25 | $30.00 | $10.00 | $1.00 | $12.50 | $45.00 |
Specialized models
Standard
Fast mode
"""
        rate = update_pricing.parse_openai(source, date(2026, 8, 10))["gpt-5.6-sol"]
        self.assertEqual(rate["longContextThreshold"], 272000)
        self.assertEqual(rate["longOutput"], 45.0)
        self.assertEqual(rate["fastMultiplier"], 2.0)

    def test_openai_parser_reads_columns_by_header(self) -> None:
        source = """
### Standard pricing data
| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6-sol | $2.00 | $10.00 | $0.20 | $2.50 | $5.00 | $4.00 | $15.00 | $0.40 | $5.00 | $10.00 |
| gpt-5.5 (<272K context length) | $5.00 | $30.00 | $0.50 | - | - | $10.00 | $45.00 | $1.00 | - | - |

| Model | Input | Output | Cached input |
| --- | --- | --- | --- |
| gpt-5.4-mini | $0.75 | $4.50 | $0.075 |
### Batch pricing data
| Model | Input | Output | Cached input |
| --- | --- | --- | --- |
| gpt-5.4-mini | $0.375 | $2.25 | $0.0375 |
### Fast pricing data
| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6-sol | $4.00 | $20.00 | $0.40 | $5.00 | $10.00 | $8.00 | $30.00 | $0.80 | $10.00 | $20.00 |
| gpt-5.5 (<272K context length) | $12.50 | $75.00 | $1.25 | - | - | - | - | - | - | - |
### Ultrafast pricing data
Specialized models
Standard
### Grouped Pricing Table data
| Category | Model | Input | Output | Cached input |
| --- | --- | --- | --- | --- |
| ChatGPT | chat-latest | $5.00 | $30.00 | $0.50 |
| Codex | gpt-5.3-codex | $1.75 | $14.00 | $0.175 |
Fast
### Grouped Pricing Table data
| Category | Model | Input | Output | Cached input |
| --- | --- | --- | --- | --- |
| Codex | gpt-5.3-codex | $3.50 | $28.00 | $0.35 |
"""
        rates = update_pricing.parse_openai(source, date(2026, 10, 28))
        self.assertEqual(rates["gpt-6-sol"], {
            "input": 2.0, "output": 10.0, "cacheRead": 0.2, "cacheWrite": 2.5,
            "longContextThreshold": 272000, "longInput": 4.0, "longOutput": 15.0,
            "longCacheRead": 0.4, "longCacheWrite": 5.0, "fastMultiplier": 2.0,
        })
        self.assertEqual(rates["gpt-5.5"]["cacheWrite"], 5.0)
        self.assertEqual(rates["gpt-5.5"]["fastMultiplier"], 2.5)
        self.assertEqual(rates["gpt-5.4-mini"], {
            "input": 0.75, "output": 4.5, "cacheRead": 0.075, "cacheWrite": 0.75,
        })
        self.assertEqual(rates["gpt-5.3-codex"], {
            "input": 1.75, "output": 14.0, "cacheRead": 0.175, "cacheWrite": 1.75,
            "fastMultiplier": 2.0,
        })
        self.assertNotIn("chat-latest", rates)

    def test_openai_parser_reads_every_specialized_codex_row(self) -> None:
        source = """
### Standard pricing data
### Batch pricing data
Specialized models
Standard
| Category | Model | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| Codex | gpt-first-codex | $1.00 | $0.10 | $2.00 |
| Codex | gpt-second-codex | $3.00 | $0.30 | $4.00 |
Fast mode
| Category | Model | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| Codex | gpt-first-codex | $2.00 | $0.20 | $4.00 |
## Multimodal models
"""
        rates = update_pricing.parse_openai(source, date(2026, 8, 10))
        self.assertEqual(
            sorted(rates), ["gpt-first-codex", "gpt-second-codex"]
        )
        self.assertEqual(rates["gpt-first-codex"]["input"], 1.0)

    def test_deepseek_parser_reads_canonical_html_table(self) -> None:
        source = """
<table><tbody>
<tr><td>MODEL</td><td>deepseek-v4-flash</td><td>deepseek-v4-pro</td></tr>
<tr><td rowspan="3">PRICING</td><td>1M INPUT TOKENS (CACHE HIT)</td><td>$0.0028</td><td>$0.003625</td></tr>
<tr><td>1M INPUT TOKENS (CACHE MISS)</td><td>$0.14</td><td>$0.435</td></tr>
<tr><td>1M OUTPUT TOKENS</td><td>$0.28</td><td>$0.87</td></tr>
</tbody></table>
"""
        rate = update_pricing.parse_deepseek(source, date(2026, 8, 10))[
            "deepseek-v4-pro"
        ]
        self.assertEqual(rate["input"], 0.435)
        self.assertEqual(rate["cacheRead"], 0.003625)

    def test_deepseek_parser_uses_peak_rate_for_tiered_table(self) -> None:
        source = """
<table><tbody>
<tr><td>MODEL</td><td>deepseek-v4-flash</td><td>deepseek-v4-pro</td></tr>
<tr><td>1M INPUT TOKENS (CACHE HIT)</td><td>OFF-PEAK</td><td>$0.007</td><td>$0.022</td></tr>
<tr><td>PEAK</td><td>$0.014</td><td>$0.044</td></tr>
<tr><td>1M INPUT TOKENS (CACHE MISS)</td><td>OFF-PEAK</td><td>$0.22</td><td>$0.66</td></tr>
<tr><td>PEAK</td><td>$0.44</td><td>$1.32</td></tr>
<tr><td>1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.66</td><td>$1.98</td></tr>
<tr><td>PEAK</td><td>$1.32</td><td>$3.96</td></tr>
</tbody></table>
"""
        rates = update_pricing.parse_deepseek(source, date(2026, 8, 17))
        self.assertEqual(rates["deepseek-v4-flash"]["input"], 0.44)
        self.assertEqual(rates["deepseek-v4-pro"]["output"], 3.96)

    def test_kimi_parser_reads_doc_table_rows_by_column(self) -> None:
        source = r"""
columns:[{title:`Model`},{title:`Input`},{title:`Output`},{title:`Cached Input`},{title:`5m write`},{title:`1h write`}],rows:[[_jsx(_components.a,{href:`/guide/kimi-k3-quickstart`,children:`kimi-k3`}),`$3.00 / MTok`,`$15.00 / MTok`,`$0.30 / MTok`,`$3.00 / MTok`,`$6.00 / MTok`],[_jsx(_components.a,{href:`/guide/kimi-next`,children:`kimi-next`}),`$1.00 / MTok`,`$5.00 / MTok`,`$0.10 / MTok`,`$1.25 / MTok`,`$2.00 / MTok`]]})
columns:[{title:`Model`},{title:`Input`},{title:`Output`},{title:`Cached Input`}],rows:[[_jsx(_components.a,{href:`/guide/kimi-k2-6-quickstart`,children:`kimi-k2.6`}),`$0.95 / MTok`,`$4.00 / MTok`,`$0.16 / MTok`]]})
"""
        rates = update_pricing.parse_kimi(source, date(2026, 10, 28))
        self.assertEqual(
            rates["kimi-k3"],
            {"input": 3.0, "output": 15.0, "cacheWrite": 3.0, "cacheRead": 0.3},
        )
        # The 5m write column, not the input price, sets cacheWrite.
        self.assertEqual(rates["kimi-next"]["cacheWrite"], 1.25)
        self.assertEqual(
            rates["kimi-k2.6"],
            {"input": 0.95, "output": 4.0, "cacheWrite": 0.95, "cacheRead": 0.16},
        )

    def test_minimax_parser_reads_standard_tab_billed_prices(self) -> None:
        source = """
<table><tr><th>Model</th><th>Input$ / M tokens</th><th>Output$ / M tokens</th><th>Prompt caching Read$ / M tokens</th></tr>
<tr><td>MiniMax-M3.1-Flash</td><td>$0.15</td><td>$0.60</td><td>$0.015</td></tr></table>
<table><tr><th>Model</th><th>Input$ / M tokens</th><th>Output$ / M tokens</th><th>Prompt caching Read$ / M tokens</th></tr>
<tr><td>MiniMax-M3<br/>&le; 512k input tokens</td><td><del>$0.60</del> $0.30</td><td><del>$2.40</del> $1.20</td><td><del>$0.12</del> $0.06</td></tr>
<tr><td>MiniMax-M3<br/>&gt; 512k input tokens</td><td><del>$1.20</del> $0.60</td><td><del>$4.80</del> $2.40</td><td><del>$0.24</del> $0.12</td></tr></table>
<table><tr><th>Model</th><th>Input$ / M tokens</th><th>Output$ / M tokens</th><th>Prompt caching Read$ / M tokens</th></tr>
<tr><td>MiniMax-M3<br/>&le; 512k input tokens</td><td><del>$0.90</del> $0.45</td><td><del>$3.60</del> $1.80</td><td><del>$0.18</del> $0.09</td></tr></table>
<table><tr><th>Model / API</th><th>Spec</th><th>Output Videoper second</th><th>Input Videoper second</th></tr>
<tr><td>MiniMax-H3-Regeneration</td><td>768P</td><td>$0.05</td><td>$0.05</td></tr></table>
"""
        rates = update_pricing.parse_minimax(
            html.unescape(source), date(2026, 10, 28)
        )
        self.assertEqual(sorted(rates), ["minimax-m3", "minimax-m3.1-flash"])
        self.assertEqual(
            rates["minimax-m3.1-flash"],
            {"input": 0.15, "output": 0.6, "cacheRead": 0.015, "cacheWrite": 0.15},
        )
        m3 = rates["minimax-m3"]
        self.assertEqual((m3["input"], m3["output"], m3["cacheRead"]), (0.3, 1.2, 0.06))
        self.assertEqual(m3["longContextThreshold"], 512000)
        self.assertEqual((m3["longInput"], m3["longOutput"]), (0.6, 2.4))

    def test_google_parser_reads_traex_models_standard_tiers(self) -> None:
        source = """
<h2 id="gemini-3.1-pro-preview">Gemini 3.1 Pro Preview</h2>
<h3>Standard</h3><table><tbody>
<tr><td>Input price</td><td>Not available</td><td>$2.00, prompts &lt;= 200k tokens<br>$4.00, prompts &gt; 200k tokens</td></tr>
<tr><td>Output price</td><td>Not available</td><td>$12.00, prompts &lt;= 200k tokens<br>$18.00, prompts &gt; 200k</td></tr>
<tr><td>Context caching price</td><td>Not available</td><td>$0.20, prompts &lt;= 200k tokens<br>$0.40, prompts &gt; 200k</td></tr>
</tbody></table>
<h2 id="gemini-3-flash-preview">Gemini 3 Flash Preview</h2>
<h3>Standard</h3><table><tbody>
<tr><td>Input price</td><td>Free</td><td>$0.50 (text / image / video)<br>$1.00 (audio)</td></tr>
<tr><td>Output price</td><td>Free</td><td>$3.00</td></tr>
<tr><td>Context caching price</td><td>Free</td><td>$0.05</td></tr>
</tbody></table>
<h2 id="gemini-future-flash">Gemini Future Flash</h2>
<h3>Standard</h3><table><tbody>
<tr><td>Input price</td><td>Free</td><td>$0.75</td></tr>
<tr><td>Output price</td><td>Free</td><td>$4.00</td></tr>
<tr><td>Context caching price</td><td>Free</td><td>$0.075</td></tr>
</tbody></table>
"""
        rates = update_pricing.parse_google(source, date(2026, 8, 11))
        self.assertEqual(rates["gemini-3.1-pro-preview"]["input"], 2.0)
        self.assertEqual(rates["gemini-3.1-pro-preview"]["output"], 12.0)
        self.assertEqual(rates["gemini-3-flash-preview"]["cacheRead"], 0.05)
        self.assertEqual(rates["gemini-future-flash"]["input"], 0.75)

    def test_daily_audit_fails_for_an_observed_public_model_without_a_rate(self) -> None:
        table = {
            "schemaVersion": 1,
            "unit": "perMillionTokens",
            "sources": {"kimi": {"fetchUrl": "https://example.invalid"}},
            "models": {},
        }
        with (
            patch.object(update_pricing, "load_pricing", return_value=table),
            patch.object(update_pricing, "fetch_text", return_value="pricing"),
            patch.object(update_pricing, "observed_models", return_value={"kimi-next"}),
            patch.dict(
                update_pricing.PARSERS,
                {"kimi": lambda _source, _day: {
                    "kimi-next": {
                        "input": 1.0, "output": 2.0,
                        "cacheWrite": 1.0, "cacheRead": 0.1,
                    }
                }},
            ),
        ):
            status = update_pricing.main([
                "--require-observed-prices", "--as-of", "2026-08-11",
            ])
        self.assertEqual(status, 1)

    def test_daily_audit_fails_for_an_observed_unverified_long_context_model(self) -> None:
        table = {
            "schemaVersion": 1,
            "unit": "perMillionTokens",
            "ccusage": {"longContextModels": []},
            "sources": {"minimax": {"fetchUrl": "https://example.invalid"}},
            "models": {},
        }
        with (
            patch.object(update_pricing, "load_pricing", return_value=table),
            patch.object(update_pricing, "fetch_text", return_value="pricing"),
            patch.object(update_pricing, "observed_models", return_value={"minimax-next"}),
            patch.object(
                update_pricing, "ccusage_long_context_supported", return_value=False
            ),
            patch.dict(
                update_pricing.PARSERS,
                {"minimax": lambda _source, _day: {
                    "minimax-next": {
                        "input": 1.0, "output": 2.0,
                        "cacheWrite": 1.0, "cacheRead": 0.1,
                        "longContextThreshold": 512000,
                        "longInput": 2.0, "longOutput": 4.0,
                        "longCacheWrite": 2.0, "longCacheRead": 0.2,
                    }
                }},
            ),
        ):
            status = update_pricing.main([
                "--require-observed-prices", "--as-of", "2026-10-28",
            ])
        self.assertEqual(status, 1)

    def test_checked_in_pricing_is_valid_json(self) -> None:
        table = json.loads(pricing.PRICING_PATH.read_text())
        self.assertEqual(table["schemaVersion"], 1)
        self.assertEqual(
            table["sources"]["deepseek"]["fetchUrl"],
            "https://api-docs.deepseek.com/quick_start/pricing/",
        )


if __name__ == "__main__":
    unittest.main()
