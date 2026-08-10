"""Repository-owned official API-equivalent pricing helpers."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
PRICING_PATH = REPO_ROOT / "config" / "official-pricing.json"
PER_MILLION = 1_000_000.0


def load_pricing(path: Path = PRICING_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schemaVersion") != 1 or data.get("unit") != "perMillionTokens":
        raise ValueError(f"unsupported pricing schema in {path}")
    return data


def active_rate(model: str, day: date, pricing: dict | None = None) -> dict | None:
    pricing = pricing or load_pricing()
    model_entry = pricing["models"].get(model)
    if model_entry is None:
        return None
    eligible = [
        rate for rate in model_entry.get("rates", [])
        if date.fromisoformat(rate["effectiveFrom"]) <= day
    ]
    return max(eligible, key=lambda rate: rate["effectiveFrom"], default=None)


def model_is_registered(model: str, pricing: dict | None = None) -> bool:
    pricing = pricing or load_pricing()
    return model in pricing["models"]


def token_breakdown(raw: dict) -> dict[str, int]:
    return {
        "inputTokens": int(raw.get("inputTokens", 0)),
        "outputTokens": int(raw.get("outputTokens", 0)),
        "cacheCreationTokens": int(raw.get("cacheCreationTokens", 0)),
        "cacheReadTokens": int(raw.get("cacheReadTokens", 0)),
    }


def standard_cost(model: str, day: date, tokens: dict, pricing: dict | None = None) -> float:
    """Price an aggregate at the official standard tier; unknown models are free."""
    rate = active_rate(model, day, pricing)
    if rate is None:
        return 0.0
    return (
        tokens.get("inputTokens", 0) * rate["input"]
        + tokens.get("outputTokens", 0) * rate["output"]
        + tokens.get("cacheCreationTokens", 0) * rate["cacheWrite"]
        + tokens.get("cacheReadTokens", 0) * rate["cacheRead"]
    ) / PER_MILLION


def official_cost_from_ccusage(
    model: str,
    usage_day: date,
    config_day: date,
    tokens: dict,
    ccusage_cost: float,
    pricing: dict | None = None,
) -> float:
    """Keep ccusage's request-aware Fast/long-context result when rates match.

    ccusage receives the repository's rates for ``config_day`` and retains the
    per-request information that its aggregate JSON omits. If a model changed
    price between the usage date and config date, fall back to our date-aware
    standard calculation. Current known changes (Sonnet 5) do not support Fast.
    """
    pricing = pricing or load_pricing()
    usage_rate = active_rate(model, usage_day, pricing)
    config_rate = active_rate(model, config_day, pricing)
    if usage_rate is None:
        return 0.0
    if usage_rate == config_rate:
        # The pinned override should make this nonzero. Falling back to the
        # standard calculation makes a malformed/partial ccusage result harmless,
        # while a larger value retains its per-request Fast/long-context premium.
        return max(
            float(ccusage_cost),
            standard_cost(model, usage_day, tokens, pricing),
        )
    return standard_cost(model, usage_day, tokens, pricing)


def _override(rate: dict | None) -> dict:
    if rate is None:
        return {
            "inputCostPerToken": 0.0,
            "outputCostPerToken": 0.0,
            "cacheCreationInputTokenCost": 0.0,
            "cacheReadInputTokenCost": 0.0,
        }
    out = {
        "inputCostPerToken": rate["input"] / PER_MILLION,
        "outputCostPerToken": rate["output"] / PER_MILLION,
        "cacheCreationInputTokenCost": rate["cacheWrite"] / PER_MILLION,
        "cacheReadInputTokenCost": rate["cacheRead"] / PER_MILLION,
    }
    optional = {
        "longInput": "inputCostPerTokenAbove200kTokens",
        "longOutput": "outputCostPerTokenAbove200kTokens",
        "longCacheWrite": "cacheCreationInputTokenCostAbove200kTokens",
        "longCacheRead": "cacheReadInputTokenCostAbove200kTokens",
    }
    for source, destination in optional.items():
        if source in rate:
            out[destination] = rate[source] / PER_MILLION
    if "fastMultiplier" in rate:
        out["fastMultiplier"] = rate["fastMultiplier"]
    return out


def ccusage_config(day: date, pricing: dict | None = None) -> dict:
    pricing = pricing or load_pricing()
    return {
        "$schema": "https://ccusage.com/config-schema.json",
        "defaults": {
            "pricingOverrides": {
                model: _override(active_rate(model, day, pricing))
                for model in sorted(pricing["models"])
            }
        },
        "codex": {"defaults": {"speed": "auto"}},
    }


@contextmanager
def ccusage_config_file(day: date) -> Iterator[Path]:
    fd, raw_path = tempfile.mkstemp(prefix="aether-ccusage-", suffix=".json")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ccusage_config(day), fh, sort_keys=True)
        yield path
    finally:
        path.unlink(missing_ok=True)
