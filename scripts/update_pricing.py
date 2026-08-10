#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Check official vendor pages and update config/official-pricing.json.

The default mode is read-only. Use ``--apply --effective-from YYYY-MM-DD`` to
record a new model or a changed price after reviewing the printed candidates.
Unknown models stay explicitly unpriced (zero) until an official row exists.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

from pricing import PRICING_PATH, active_rate, load_pricing

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
AGENT_FILES = frozenset({"claude.json", "codex.json", "opencode.json", "traex.json"})


def _money(value: str) -> float | None:
    match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", value)
    return float(match.group(1)) if match else None


def _rows(markdown: str) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in markdown.splitlines()
        if line.lstrip().startswith("|") and "---" not in line
    ]


def parse_anthropic(markdown: str, as_of: date) -> dict[str, dict]:
    candidates: dict[str, dict] = {}
    for row in _rows(markdown):
        if len(row) != 6 or not row[0].startswith("Claude "):
            continue
        display = re.sub(r"\s*\[.*", "", row[0]).strip()
        if display == "Claude Sonnet 5 starting September 1, 2026":
            display = "Claude Sonnet 5"
            if as_of < date(2026, 9, 1):
                continue
        elif display == "Claude Sonnet 5" and as_of >= date(2026, 9, 1):
            continue
        values = [_money(cell) for cell in row[1:]]
        if any(value is None for value in values):
            continue
        candidates[display] = {
            "input": values[0], "cacheWrite": values[1],
            "cacheRead": values[3], "output": values[4],
        }
    for name in ("Claude Opus 5", "Claude Opus 4.8"):
        if name in candidates:
            candidates[name]["fastMultiplier"] = 2.0
    return candidates


def parse_openai(markdown: str, _as_of: date) -> dict[str, dict]:
    candidates: dict[str, dict] = {}
    standard = markdown.split("### Standard pricing data", 1)[-1].split(
        "### Batch pricing data", 1
    )[0]
    for row in _rows(standard):
        if len(row) != 9 or not row[0].startswith("gpt-"):
            continue
        model = row[0].split(" ", 1)[0]
        values = [_money(cell) for cell in row[1:]]
        if values[0] is None or values[3] is None:
            continue
        rate = {
            "input": values[0], "cacheRead": values[1] or values[0],
            "cacheWrite": values[2] or values[0], "output": values[3],
        }
        if values[4] is not None:
            rate.update({
                "longContextThreshold": 272000,
                "longInput": values[4], "longCacheRead": values[5] or values[4],
                "longCacheWrite": values[6] or values[4], "longOutput": values[7],
            })
        candidates[model] = rate

    specialized = markdown.split("## Specialized models", 1)[-1]
    for row in _rows(specialized):
        if len(row) == 5 and row[0] == "Codex" and row[1].startswith("gpt-"):
            candidates[row[1]] = {
                "input": _money(row[2]), "cacheRead": _money(row[3]),
                "cacheWrite": _money(row[2]), "output": _money(row[4]),
            }
            break

    fast = markdown.split("### Fast pricing data", 1)[-1].split(
        "## Multimodal models", 1
    )[0]
    fast_input: dict[str, float] = {}
    for row in _rows(fast):
        if len(row) == 9 and row[0].startswith("gpt-"):
            value = _money(row[1])
            if value is not None:
                fast_input[row[0].split(" ", 1)[0]] = value
    for model, rate in candidates.items():
        if model in fast_input and rate["input"]:
            rate["fastMultiplier"] = fast_input[model] / rate["input"]
    if "gpt-5.3-codex" in candidates:
        candidates["gpt-5.3-codex"]["fastMultiplier"] = 2.0
    return candidates


def parse_deepseek(markdown: str, _as_of: date) -> dict[str, dict]:
    candidates: dict[str, dict] = {}
    for row in _rows(markdown):
        model = row[0].strip("`")
        if len(row) != 4 or not model.startswith("deepseek-"):
            continue
        values = [_money(cell) for cell in row[1:]]
        if any(value is None for value in values):
            continue
        candidates[model] = {
            "input": values[0], "output": values[1],
            "cacheWrite": values[0], "cacheRead": values[2],
        }
    return candidates


PARSERS = {
    "anthropic": parse_anthropic,
    "openai": parse_openai,
    "deepseek": parse_deepseek,
}


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Aether-Ledger/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return html.unescape(response.read().decode("utf-8"))


def observed_models() -> set[str]:
    models: set[str] = set()
    for path in DATA_DIR.rglob("*.json"):
        if path.name not in AGENT_FILES:
            continue
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in store.values():
            if isinstance(entry, dict) and isinstance(entry.get("models"), dict):
                models.update(entry["models"])
    return models


def infer_provider(model: str) -> str | None:
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("gpt-"):
        return "openai"
    if model.startswith("deepseek-"):
        return "deepseek"
    return None


def inferred_official_name(model: str, provider: str) -> str:
    if provider != "anthropic":
        return model
    parts = model.split("-")
    if len(parts) >= 2 and len(parts[-1]) == 8 and parts[-1].isdigit():
        parts.pop()
    if len(parts) >= 4 and parts[-2].isdigit() and parts[-1].isdigit():
        parts[-2:] = [f"{parts[-2]}.{parts[-1]}"]
    return " ".join(part.capitalize() for part in parts)


def _atomic_write(path: Path, data: dict) -> None:
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(raw, path)
    except Exception:
        Path(raw).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write reviewed changes")
    parser.add_argument("--effective-from", type=date.fromisoformat)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)
    if args.apply and args.effective_from is None:
        parser.error("--apply requires --effective-from YYYY-MM-DD")

    pricing = load_pricing()
    candidates = {}
    for provider, source in pricing["sources"].items():
        parsed = PARSERS[provider](fetch_text(source["fetchUrl"]), args.as_of)
        if not parsed:
            raise RuntimeError(f"no prices parsed from {provider}'s official source")
        candidates[provider] = parsed
    changed = False
    for model in sorted(observed_models() | set(pricing["models"])):
        entry = pricing["models"].get(model)
        provider = entry.get("provider") if entry else infer_provider(model)
        if provider is None:
            print(f"UNSUPPORTED {model}: no official provider mapping")
            continue
        official_name = (
            entry.get("officialName") if entry
            else inferred_official_name(model, provider)
        )
        candidate = candidates[provider].get(official_name)
        current = active_rate(model, args.as_of, pricing)
        comparable = None if current is None else {
            key: value for key, value in current.items() if key != "effectiveFrom"
        }
        if candidate is None:
            print(f"UNPRICED {model}: absent from {provider}'s official price table")
            if entry is None:
                if args.apply:
                    pricing["models"][model] = {
                        "provider": provider, "officialName": official_name,
                        "rates": [], "unpricedReason": "No public official API token price",
                    }
                    changed = True
            continue
        if comparable == candidate:
            print(f"OK {model}")
            continue
        print(f"CHANGE {model}: {json.dumps(candidate, sort_keys=True)}")
        if args.apply:
            if entry is None:
                entry = pricing["models"].setdefault(model, {
                    "provider": provider, "officialName": official_name, "rates": [],
                })
            rate = {"effectiveFrom": args.effective_from.isoformat(), **candidate}
            entry.pop("unpricedReason", None)
            rates = entry.setdefault("rates", [])
            rates[:] = [
                old for old in rates
                if old["effectiveFrom"] != args.effective_from.isoformat()
            ]
            rates.append(rate)
            entry["rates"].sort(key=lambda item: item["effectiveFrom"])
            changed = True
    if changed:
        _atomic_write(PRICING_PATH, pricing)
        print(f"updated {PRICING_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
