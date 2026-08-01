#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Render the repository's aggregate token dashboard as static SVG."""

from __future__ import annotations

import argparse
import bisect
import json
import os
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "assets" / "token-activity.svg"
AGENT_FILES = frozenset({"claude.json", "codex.json", "opencode.json"})
IGNORED_PARTS = frozenset({".git", ".venv", "__pycache__"})
SHANGHAI = ZoneInfo("Asia/Shanghai")

# Neutral empty cells plus a high-contrast cyan ramp. The larger luminance
# steps stay distinguishable when GitHub scales the SVG down in the README.
PALETTE = ("#272b3a", "#164e63", "#0e7490", "#06b6d4", "#67e8f9")
WIDTH = 1180
CARD_X = 16
CARD_WIDTH = WIDTH - CARD_X * 2
GRID_LEFT = 34
GRID_TOP = 198
CELL_SIZE = 15
CELL_GAP = 4
CELL_STEP = CELL_SIZE + CELL_GAP
WEEKS = 53


def discover_agent_files(root: Path) -> tuple[Path, ...]:
    """Find only canonical per-agent stores, excluding caches and Git internals."""
    paths = []
    for path in Path(root).rglob("*.json"):
        if path.name not in AGENT_FILES or any(part in IGNORED_PARTS for part in path.parts):
            continue
        paths.append(path)
    return tuple(sorted(paths))


def aggregate_daily(root: Path) -> dict[date, int]:
    """Sum daily token totals across every durable machine, trail pod, and rollup."""
    totals: defaultdict[date, int] = defaultdict(int)
    for path in discover_agent_files(root):
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc
        if not isinstance(store, dict):
            raise ValueError(f"expected a date-keyed object in {path}")
        for raw_day, entry in store.items():
            if not isinstance(entry, dict):
                continue
            try:
                day = date.fromisoformat(raw_day)
            except (TypeError, ValueError):
                continue
            tokens = entry.get("totalTokens", 0)
            if isinstance(tokens, bool) or not isinstance(tokens, (int, float)):
                continue
            totals[day] += max(0, int(tokens))
    return dict(sorted(totals.items()))


def streaks(totals: dict[date, int], as_of: date) -> tuple[int, int]:
    active = sorted(day for day, tokens in totals.items() if tokens > 0 and day <= as_of)
    if not active:
        return 0, 0

    longest = run = 1
    for previous, current in zip(active, active[1:]):
        run = run + 1 if current == previous + timedelta(days=1) else 1
        longest = max(longest, run)

    active_set = set(active)
    anchor = as_of if as_of in active_set else as_of - timedelta(days=1)
    current = 0
    while anchor in active_set:
        current += 1
        anchor -= timedelta(days=1)
    return current, longest


def _compact_number(value: int) -> str:
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            scaled = value / divisor
            digits = 0 if scaled >= 100 else 1
            return f"{scaled:.{digits}f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def _grid_bounds(as_of: date) -> tuple[date, date]:
    grid_end = as_of + timedelta(days=6 - as_of.weekday())
    return grid_end - timedelta(days=WEEKS * 7 - 1), grid_end


def _thresholds(values: list[int]) -> tuple[int, int, int]:
    if not values:
        return 0, 0, 0
    ordered = sorted(values)
    return tuple(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))] for q in (.25, .5, .75))


def render_svg(totals: dict[date, int], as_of: date) -> str:
    positive = {day: value for day, value in totals.items() if value > 0 and day <= as_of}
    lifetime = sum(positive.values())
    peak_day, peak_tokens = max(positive.items(), key=lambda item: item[1], default=(None, 0))
    current_streak, longest_streak = streaks(totals, as_of)
    grid_start, grid_end = _grid_bounds(as_of)
    visible_values = [value for day, value in positive.items() if grid_start <= day <= grid_end]
    thresholds = _thresholds(visible_values)

    stats = (
        (_compact_number(lifetime), "Lifetime tokens"),
        (_compact_number(peak_tokens), "Peak tokens"),
        (str(len(positive)), "Active days"),
        (f"{current_streak} days", "Current streak"),
        (f"{longest_streak} days", "Longest streak"),
    )
    height = 390
    title = f"Token activity through {as_of.isoformat()}"
    peak_text = peak_day.isoformat() if peak_day else "no activity"
    lines = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
            f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">'
        ),
        f"  <title id=\"title\">{escape(title)}</title>",
        (
            f'  <desc id="desc">{_compact_number(lifetime)} lifetime tokens across '
            f'{len(positive)} active days; peak {_compact_number(peak_tokens)} on {peak_text}.</desc>'
        ),
        f'  <rect width="{WIDTH}" height="{height}" rx="22" fill="#1d1e2c"/>',
        (
            f'  <rect x="{CARD_X}" y="18" width="{CARD_WIDTH}" height="92" rx="18" '
            'fill="none" stroke="#303246" stroke-width="2"/>'
        ),
    ]

    stat_width = CARD_WIDTH / len(stats)
    for index, (value, label) in enumerate(stats):
        center = CARD_X + stat_width * index + stat_width / 2
        if index:
            divider = CARD_X + stat_width * index
            lines.append(
                f'  <line x1="{divider:.1f}" y1="36" x2="{divider:.1f}" y2="92" '
                'stroke="#303246" stroke-width="1"/>'
            )
        lines.extend(
            (
                f'  <text x="{center:.1f}" y="57" text-anchor="middle" fill="#d9ddf3" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="24" font-weight="500">{escape(value)}</text>',
                f'  <text x="{center:.1f}" y="86" text-anchor="middle" fill="#9699b0" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="17">{escape(label)}</text>',
            )
        )

    lines.extend(
        (
            '  <text x="16" y="154" fill="#d9ddf3" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="22" font-weight="500">Token activity</text>',
            f'  <text x="{WIDTH - 18}" y="154" text-anchor="end" fill="#9699b0" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="15">Daily · Asia/Shanghai · through {as_of.isoformat()}</text>',
        )
    )

    seen_months: set[tuple[int, int]] = set()
    for week in range(WEEKS):
        week_start = grid_start + timedelta(days=week * 7)
        candidates = [week_start + timedelta(days=offset) for offset in range(7)]
        month_day = next((day for day in candidates if day.day <= 7), None)
        if month_day is None or (month_day.year, month_day.month) in seen_months:
            continue
        seen_months.add((month_day.year, month_day.month))
        x = GRID_LEFT + week * CELL_STEP
        lines.append(
            f'  <text x="{x}" y="184" fill="#85889f" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="13">{month_day.strftime("%b")}</text>'
        )

    current = grid_start
    while current <= grid_end:
        if current <= as_of:
            week = (current - grid_start).days // 7
            weekday = current.weekday()
            x = GRID_LEFT + week * CELL_STEP
            y = GRID_TOP + weekday * CELL_STEP
            tokens = positive.get(current, 0)
            level = 0 if tokens <= 0 else 1 + bisect.bisect_left(thresholds, tokens)
            level = min(level, 4)
            label = f"{current.isoformat()}: {_compact_number(tokens)} tokens"
            lines.extend(
                (
                    f'  <rect x="{x}" y="{y}" width="{CELL_SIZE}" height="{CELL_SIZE}" '
                    f'rx="4" fill="{PALETTE[level]}" data-date="{current.isoformat()}" '
                    f'data-tokens="{tokens}" data-level="{level}">',
                    f"    <title>{escape(label)}</title>",
                    "  </rect>",
                )
            )
        current += timedelta(days=1)

    footer_y = GRID_TOP + 7 * CELL_STEP + 27
    lines.append(
        f'  <text x="{GRID_LEFT}" y="{footer_y}" fill="#85889f" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="12">Less</text>'
    )
    legend_x = GRID_LEFT + 34
    for level, color in enumerate(PALETTE):
        x = legend_x + level * CELL_STEP
        lines.append(
            f'  <rect x="{x}" y="{footer_y - 12}" width="{CELL_SIZE}" height="{CELL_SIZE}" '
            f'rx="4" fill="{color}"/>'
        )
    lines.append(
        f'  <text x="{legend_x + len(PALETTE) * CELL_STEP + 2}" y="{footer_y}" '
        'fill="#85889f" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="12">More</text>'
    )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def generate(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    totals = aggregate_daily(root)
    if as_of is None:
        as_of = max(
            (day for day, tokens in totals.items() if tokens > 0),
            default=datetime.now(SHANGHAI).date(),
        )
    expected = render_svg(totals, as_of)
    try:
        actual = output.read_text(encoding="utf-8")
    except FileNotFoundError:
        actual = None
    changed = actual != expected
    if changed and not check:
        _atomic_write(output, expected)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--check", action="store_true", help="fail if the SVG is stale")
    args = parser.parse_args()
    changed = generate(args.root, args.output, args.as_of, check=args.check)
    if args.check and changed:
        print(f"stale dashboard: {args.output}")
        return 1
    if changed:
        print(f"rendered {args.output}")
    else:
        print(f"dashboard is current: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
