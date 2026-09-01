#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Check official vendor pages and update config/official-pricing.json.

The default mode is read-only. Use ``--apply --effective-from YYYY-MM-DD`` to
record a new model or a changed price after reviewing the printed candidates.
Unknown models stay explicitly unpriced (zero) until an official row exists.
``--require-observed-prices`` is the daily close audit: it exits nonzero when an
observed model from a supported public provider has no active reviewed rate.
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
from html.parser import HTMLParser
from pathlib import Path

from pricing import (
    PRICING_PATH,
    active_rate,
    ccusage_long_context_supported,
    load_pricing,
)

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
            # Anthropic cancelled this scheduled rate on August 10 and made the
            # introductory Sonnet 5 price permanent.
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

    specialized = markdown.split("Specialized models", 1)[1]
    for marker in ("\nFast mode\n", "### Fast pricing data"):
        specialized = specialized.split(marker, 1)[0]
    for row in _rows(specialized):
        if len(row) == 5 and row[0] == "Codex" and row[1].startswith("gpt-"):
            values = [_money(cell) for cell in row[2:]]
            if any(value is None for value in values):
                continue
            candidates[row[1]] = {
                "input": values[0], "cacheRead": values[1],
                "cacheWrite": values[0], "output": values[2],
            }

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


class _HtmlTableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def parse_deepseek(page: str, _as_of: date) -> dict[str, dict]:
    parser = _HtmlTableRows()
    parser.feed(page)
    model_row = next(
        (row for row in parser.rows if row and row[0] == "MODEL"), None
    )
    if model_row is None or len(model_row) < 2:
        return {}
    models = model_row[1:]

    def prices(label: str) -> list[float] | None:
        row = next(
            (row for row in parser.rows if any(label in cell.upper() for cell in row)),
            None,
        )
        if row is None or len(row) < len(models):
            return None
        values = [_money(cell) for cell in row[-len(models):]]
        return None if any(value is None for value in values) else values

    cache_read = prices("CACHE HIT")
    input_prices = prices("CACHE MISS")
    output_prices = prices("OUTPUT TOKENS")
    if cache_read is None or input_prices is None or output_prices is None:
        return {}
    return {
        model: {
            "input": input_prices[index], "output": output_prices[index],
            "cacheWrite": input_prices[index], "cacheRead": cache_read[index],
        }
        for index, model in enumerate(models)
    }


def parse_kimi(page: str, _as_of: date) -> dict[str, dict]:
    """Read Kimi ``DocTable`` rows serialized into the official HTML pages."""
    candidates: dict[str, dict] = {}
    rows = re.compile(
        r"rows:\[\[\`(kimi-[^`]+)\`,\`1M tokens\`,(.*?)\]\]\}\)",
        re.DOTALL,
    )
    prices = re.compile(
        r"children:\[\`\$\`,\`([0-9]+(?:\.[0-9]+)?)\`\]"
    )
    for model, row in rows.findall(page):
        values = prices.findall(row)
        if len(values) < 3:
            continue
        cache_hit, cache_miss, output = values[:3]
        cache_read = float(cache_hit)
        input_price = float(cache_miss)
        output_price = float(output)
        candidates[model] = {
            "input": input_price, "output": output_price,
            "cacheWrite": input_price, "cacheRead": cache_read,
        }
    return candidates


def parse_minimax(page: str, _as_of: date) -> dict[str, dict]:
    parser = _HtmlTableRows()
    parser.feed(page)
    candidates: dict[str, dict] = {}
    for row in parser.rows:
        if len(row) != 5 or not row[0].startswith("MiniMax-M"):
            continue
        values = [_money(cell) for cell in row[1:]]
        if any(value is None for value in values):
            continue
        candidates[row[0].lower()] = {
            "input": values[0], "output": values[1],
            "cacheRead": values[2], "cacheWrite": values[3],
        }
    return candidates


def parse_google(page: str, _as_of: date) -> dict[str, dict]:
    """Read the Standard table for the Gemini models exposed by TRAE.

    TRAE caps these models at 200K context, so Gemini 3.1 Pro never enters the
    official >200K tier. Audio-specific prices are likewise outside TRAE CLI's
    text/image session accounting; the first dollar value is the applicable one.
    """
    candidates: dict[str, dict] = {}
    markers = list(re.finditer(r'<h2 id="(gemini-[^"]+)"', page))
    for index, marker in enumerate(markers):
        model = marker.group(1)
        end = markers[index + 1].start() if index + 1 < len(markers) else len(page)
        section = page[marker.end():end]
        parser = _HtmlTableRows()
        parser.feed(section)

        def value(label: str) -> float | None:
            row = next(
                (item for item in parser.rows if item and item[0].startswith(label)),
                None,
            )
            return None if row is None else _money(row[-1])

        input_price = value("Input price")
        output_price = value("Output price")
        cache_read = value("Context caching price")
        if None in (input_price, output_price, cache_read):
            continue
        candidates[model] = {
            "input": input_price, "output": output_price,
            "cacheWrite": input_price, "cacheRead": cache_read,
        }
    return candidates


PARSERS = {
    "anthropic": parse_anthropic,
    "openai": parse_openai,
    "deepseek": parse_deepseek,
    "google": parse_google,
    "kimi": parse_kimi,
    "minimax": parse_minimax,
}


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Aether-Ledger/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return html.unescape(response.read().decode("utf-8"))


def fetch_provider_text(provider: str, source: dict[str, str]) -> str:
    """Fetch one provider source, expanding Kimi's model-price index."""
    page = fetch_text(source["fetchUrl"])
    if provider != "kimi":
        return page
    links = sorted(set(re.findall(
        r"\((https://platform\.kimi\.ai/docs/pricing/chat-k[^)]+?\.md)\)",
        page,
    )))
    if not links:
        return page
    return "\n".join(fetch_text(link.removesuffix(".md")) for link in links)


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
    if model.startswith("gemini-"):
        return "google"
    if model.startswith("kimi-"):
        return "kimi"
    if model.startswith("minimax-"):
        return "minimax"
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
    parser.add_argument(
        "--require-observed-prices", action="store_true",
        help="exit nonzero if an observed supported-provider model has no active rate",
    )
    args = parser.parse_args(argv)
    if args.apply and args.effective_from is None:
        parser.error("--apply requires --effective-from YYYY-MM-DD")
    if args.apply and args.require_observed_prices:
        parser.error("--apply and --require-observed-prices are mutually exclusive")

    pricing = load_pricing()
    candidates = {}
    for provider, source in pricing["sources"].items():
        parsed = PARSERS[provider](
            fetch_provider_text(provider, source), args.as_of
        )
        if not parsed:
            raise RuntimeError(f"no prices parsed from {provider}'s official source")
        candidates[provider] = parsed
    changed = False
    missing_observed_price = False
    observed = observed_models()
    for model in sorted(observed | set(pricing["models"])):
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
        if candidate is not None and not ccusage_long_context_supported(
            model, candidate, pricing
        ):
            print(
                f"UNSUPPORTED {model}: long-context pricing is not verified "
                "in the pinned ccusage version"
            )
            continue
        current = active_rate(model, args.as_of, pricing)
        if (
            args.require_observed_prices
            and model in observed
            and current is None
            and not (entry and entry.get("unpricedReason"))
        ):
            missing_observed_price = True
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
    return 1 if missing_observed_price else 0


if __name__ == "__main__":
    raise SystemExit(main())
