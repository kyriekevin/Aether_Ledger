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
import math
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "assets" / "token-activity.svg"
DEFAULT_COMPOSITION_OUTPUT = REPO_ROOT / "assets" / "token-composition.svg"
DEFAULT_TOPOLOGY_OUTPUT = REPO_ROOT / "assets" / "token-topology.svg"
AGENT_FILES = frozenset({"claude.json", "codex.json", "opencode.json", "traex.json"})
IGNORED_PARTS = frozenset({".git", ".venv", "__pycache__"})
SHANGHAI = ZoneInfo("Asia/Shanghai")

ROLE_ORDER = ("work", "personal", "devbox", "trail")
COMPOSITION_ROLE_ORDER = ("work", "personal", "development")
AGENT_ORDER = ("claude", "codex", "opencode", "traex")
COMPOSITION_AGENT_ORDER = ("claude", "codex", "traex", "legacy")
ROLE_LABELS = {"work": "Work", "personal": "Personal", "development": "Development"}
ROLE_BUCKETS = {
    "work": "work",
    "personal": "personal",
    "devbox": "development",
    "trail": "development",
}
AGENT_LABELS = {"claude": "Claude", "codex": "Codex", "traex": "TRAE", "legacy": "Legacy"}
AGENT_BUCKETS = {"claude": "claude", "codex": "codex", "opencode": "legacy", "traex": "traex"}
ROLE_COLORS = {
    "work": "#22d3ee",
    "personal": "#38bdf8",
    "development": "#64748b",
}
AGENT_COLORS = {
    "claude": "#60a5fa",
    "codex": "#22d3ee",
    "traex": "#818cf8",
    "legacy": "#64748b",
}

# Neutral empty cells plus a high-contrast cyan ramp. The larger luminance
# steps stay distinguishable when GitHub scales the SVG down in the README.
PALETTE = ("#272b3a", "#164e63", "#0e7490", "#06b6d4", "#67e8f9")
WIDTH = 1180
CARD_X = 16
CARD_WIDTH = WIDTH - CARD_X * 2
GRID_LEFT = 58
GRID_TOP = 218
CELL_SIZE = 16
CELL_GAP = 4
CELL_STEP = CELL_SIZE + CELL_GAP
WEEKS = 53


@dataclass(frozen=True)
class DailyTotals:
    tokens: int = 0
    cost: float = 0.0


@dataclass(frozen=True)
class UsageRecord:
    day: date
    role: str
    agent: str
    tokens: int
    cost: float


@dataclass(frozen=True)
class CompositionTotals:
    as_of: date
    recent_start: date
    lifetime_roles: dict[str, int]
    recent_roles: dict[str, int]
    lifetime_agents: dict[str, int]
    recent_agents: dict[str, int]
    recent_topology: dict[tuple[str, str], int]


def discover_agent_files(root: Path) -> tuple[Path, ...]:
    """Find only canonical per-agent stores, excluding caches and Git internals."""
    paths = []
    for path in (Path(root) / "data").rglob("*.json"):
        if path.name not in AGENT_FILES or any(part in IGNORED_PARTS for part in path.parts):
            continue
        paths.append(path)
    return tuple(sorted(paths))


def load_usage_records(root: Path) -> tuple[UsageRecord, ...]:
    """Load public role, agent, day, token, and cost dimensions from canonical stores."""
    root = Path(root)
    data_root = root / "data"
    records: list[UsageRecord] = []
    for path in discover_agent_files(root):
        relative = path.relative_to(data_root)
        role = relative.parts[0]
        agent = path.stem
        if role not in ROLE_ORDER:
            raise ValueError(f"unexpected public role in {path}")
        if agent not in AGENT_ORDER:
            raise ValueError(f"unexpected canonical agent in {path}")
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
                tokens = 0
            cost = entry.get("totalCost", 0.0)
            if isinstance(cost, bool) or not isinstance(cost, (int, float)):
                cost = 0.0
            records.append(
                UsageRecord(
                    day=day,
                    role=role,
                    agent=agent,
                    tokens=max(0, int(tokens)),
                    cost=max(0.0, float(cost)),
                )
            )
    return tuple(records)


def aggregate_daily(root: Path) -> dict[date, DailyTotals]:
    """Sum daily tokens and API-equivalent cost across canonical stores."""
    tokens_by_day: defaultdict[date, int] = defaultdict(int)
    cost_by_day: defaultdict[date, float] = defaultdict(float)
    for record in load_usage_records(root):
        tokens_by_day[record.day] += record.tokens
        cost_by_day[record.day] += record.cost
    return {
        day: DailyTotals(tokens=tokens_by_day[day], cost=cost_by_day[day])
        for day in sorted(tokens_by_day.keys() | cost_by_day.keys())
    }


def aggregate_composition(root: Path, as_of: date) -> CompositionTotals:
    """Aggregate lifetime comparison and recent role-by-agent topology."""
    recent_start = as_of - timedelta(days=29)
    lifetime_roles = {role: 0 for role in COMPOSITION_ROLE_ORDER}
    recent_roles = {role: 0 for role in COMPOSITION_ROLE_ORDER}
    lifetime_agents = {agent: 0 for agent in COMPOSITION_AGENT_ORDER}
    recent_agents = {agent: 0 for agent in COMPOSITION_AGENT_ORDER}
    recent_topology = {
        (role, agent): 0
        for role in COMPOSITION_ROLE_ORDER
        for agent in COMPOSITION_AGENT_ORDER
    }

    for record in load_usage_records(root):
        if record.day > as_of:
            continue
        role_bucket = ROLE_BUCKETS[record.role]
        lifetime_roles[role_bucket] += record.tokens
        agent_bucket = AGENT_BUCKETS[record.agent]
        lifetime_agents[agent_bucket] += record.tokens
        if record.day >= recent_start:
            recent_roles[role_bucket] += record.tokens
            recent_agents[agent_bucket] += record.tokens
            recent_topology[(role_bucket, agent_bucket)] += record.tokens

    return CompositionTotals(
        as_of=as_of,
        recent_start=recent_start,
        lifetime_roles=lifetime_roles,
        recent_roles=recent_roles,
        lifetime_agents=lifetime_agents,
        recent_agents=recent_agents,
        recent_topology=recent_topology,
    )


def _compact_number(value: int) -> str:
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            scaled = value / divisor
            digits = 0 if scaled >= 100 else 1
            return f"{scaled:.{digits}f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def _compact_cost(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"${value / 1_000:.1f}K".replace(".0K", "K")
    if value >= 100:
        return f"${value:.0f}"
    return f"${value:.2f}"


def _grid_bounds(as_of: date) -> tuple[date, date]:
    grid_end = as_of + timedelta(days=6 - as_of.weekday())
    return grid_end - timedelta(days=WEEKS * 7 - 1), grid_end


def _thresholds(values: list[int]) -> tuple[int, int, int]:
    if not values:
        return 0, 0, 0
    ordered = sorted(values)
    return tuple(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))] for q in (.25, .5, .75))


def _sum_period(values: list[DailyTotals]) -> DailyTotals:
    return DailyTotals(
        tokens=sum(item.tokens for item in values),
        cost=sum(item.cost for item in values),
    )


def render_svg(totals: dict[date, DailyTotals], as_of: date) -> str:
    recorded = {day: value for day, value in totals.items() if day <= as_of}
    positive = {day: value for day, value in recorded.items() if value.tokens > 0}
    lifetime = _sum_period(list(recorded.values()))
    latest = recorded.get(as_of, DailyTotals())
    month = _sum_period(
        [
            value
            for day, value in recorded.items()
            if (day.year, day.month) == (as_of.year, as_of.month)
        ]
    )
    peak_day, peak = max(
        positive.items(),
        key=lambda item: item[1].tokens,
        default=(None, DailyTotals()),
    )
    active_days = len(positive)
    grid_start, grid_end = _grid_bounds(as_of)
    visible_values = [
        value.tokens for day, value in positive.items() if grid_start <= day <= grid_end
    ]
    thresholds = _thresholds(visible_values)

    stats = (
        (latest, f"Latest · {as_of.strftime('%b %-d')}"),
        (month, f"Month · {as_of.strftime('%b')}"),
        (lifetime, "Lifetime"),
        (peak, f"Peak · {peak_day.strftime('%b %-d') if peak_day else '—'}"),
    )
    height = 410
    title = f"AI compute activity through {as_of.isoformat()}"
    peak_text = peak_day.isoformat() if peak_day else "no activity"
    lines = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
            f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">'
        ),
        f"  <title id=\"title\">{escape(title)}</title>",
        (
            f'  <desc id="desc">{_compact_cost(lifetime.cost)} API-equivalent lifetime cost and '
            f'{_compact_number(lifetime.tokens)} lifetime tokens across {active_days} active days; '
            f'peak {_compact_number(peak.tokens)} tokens on {peak_text}.</desc>'
        ),
        f'  <rect width="{WIDTH}" height="{height}" rx="22" fill="#1d1e2c"/>',
        (
            f'  <rect x="{CARD_X}" y="18" width="{CARD_WIDTH}" height="112" rx="18" '
            'fill="none" stroke="#303246" stroke-width="2"/>'
        ),
    ]

    stat_width = CARD_WIDTH / 5
    for index, (value, label) in enumerate(stats):
        center = CARD_X + stat_width * index + stat_width / 2
        if index:
            divider = CARD_X + stat_width * index
            lines.append(
                f'  <line x1="{divider:.1f}" y1="36" x2="{divider:.1f}" y2="112" '
                'stroke="#303246" stroke-width="1"/>'
            )
        lines.extend(
            (
                f'  <text x="{center:.1f}" y="52" text-anchor="middle" fill="#67e8f9" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="22" font-weight="600">{escape(_compact_number(value.tokens))} tokens</text>',
                f'  <text x="{center:.1f}" y="78" text-anchor="middle" fill="#d9ddf3" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="16" font-weight="500">{escape(_compact_cost(value.cost))}</text>',
                f'  <text x="{center:.1f}" y="106" text-anchor="middle" fill="#9699b0" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="14">{escape(label)}</text>',
            )
        )

    active_center = CARD_X + stat_width * 4 + stat_width / 2
    divider = CARD_X + stat_width * 4
    lines.extend(
        (
            f'  <line x1="{divider:.1f}" y1="36" x2="{divider:.1f}" y2="112" '
            'stroke="#303246" stroke-width="1"/>',
            f'  <text x="{active_center:.1f}" y="66" text-anchor="middle" fill="#d9ddf3" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="25" font-weight="600">{active_days} days</text>',
            f'  <text x="{active_center:.1f}" y="106" text-anchor="middle" fill="#9699b0" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="14">Active days</text>',
        )
    )

    lines.extend(
        (
            '  <text x="16" y="174" fill="#d9ddf3" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="22" font-weight="500">Compute activity</text>',
        )
    )

    for weekday, label in ((0, "Mon"), (2, "Wed"), (4, "Fri")):
        y = GRID_TOP + weekday * CELL_STEP + 13
        lines.append(
            f'  <text x="16" y="{y}" fill="#85889f" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="12">{label}</text>'
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
            f'  <text x="{x}" y="204" fill="#85889f" '
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
            tokens = positive.get(current, DailyTotals()).tokens
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


def _share(value: int, total: int) -> float:
    return value / total if total > 0 else 0.0


def _percent(value: int, total: int) -> str:
    share = _share(value, total) * 100
    if 0 < share < 0.1:
        return "<0.1%"
    return f"{share:.1f}%"


def _render_composition_bar(
    lines: list[str],
    *,
    bar_id: str,
    y: int,
    row_label: str,
    values: dict[str, int],
    order: tuple[str, ...],
    labels: dict[str, str],
    colors: dict[str, str],
    height: int = 32,
    emphasize: bool = True,
) -> None:
    bar_x = 170
    bar_width = WIDTH - bar_x - 32
    bar_height = height
    total = sum(values.values())
    label_color = "#d9ddf3" if emphasize else "#85889f"
    lines.extend(
        (
            f'  <text x="32" y="{y + height / 2 + 5:.1f}" '
            f'fill="{label_color}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="15" font-weight="500">{escape(row_label)}</text>',
            f'  <rect x="{bar_x}" y="{y}" width="{bar_width}" height="{bar_height}" '
            'rx="8" fill="#272b3a"/>',
            f'  <clipPath id="{bar_id}"><rect x="{bar_x}" y="{y}" width="{bar_width}" '
            f'height="{bar_height}" rx="8"/></clipPath>',
        )
    )
    cursor = float(bar_x)
    for index, category in enumerate(order):
        value = values.get(category, 0)
        width = bar_width * _share(value, total)
        if index == len(order) - 1 and value:
            width = bar_x + bar_width - cursor
        if width <= 0:
            continue
        share_text = _percent(value, total)
        escaped_share = escape(share_text, quote=True)
        lines.extend(
            (
                f'  <rect x="{cursor:.2f}" y="{y}" width="{width:.2f}" height="{bar_height}" '
                f'fill="{colors[category]}" clip-path="url(#{bar_id})" '
                f'data-category="{category}" data-tokens="{value}" data-share="{escaped_share}">',
                f'    <title>{escape(labels[category])}: {_compact_number(value)} tokens '
                f'({escaped_share})</title>',
                "  </rect>",
            )
        )
        if emphasize and width >= 112:
            lines.append(
                f'  <text x="{cursor + width / 2:.2f}" y="{y + 21}" text-anchor="middle" '
                'fill="#111827" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="13" font-weight="700">{escape(labels[category])} {escaped_share}</text>'
            )
        cursor += width


def _render_composition_legend(
    lines: list[str],
    *,
    y: int,
    order: tuple[str, ...],
    labels: dict[str, str],
    colors: dict[str, str],
    lifetime: dict[str, int],
    recent: dict[str, int],
) -> None:
    lifetime_total = sum(lifetime.values())
    recent_total = sum(recent.values())
    item_width = (WIDTH - 218) / len(order)
    for index, category in enumerate(order):
        x = 186 + index * item_width
        comparison = (
            f"{labels[category]} · {_percent(lifetime.get(category, 0), lifetime_total)}"
            f" → {_percent(recent.get(category, 0), recent_total)}"
        )
        lines.extend(
            (
                f'  <rect x="{x:.2f}" y="{y - 11}" width="12" height="12" rx="3" '
                f'fill="{colors[category]}"/>',
                f'  <text x="{x + 20:.2f}" y="{y}" fill="#b9bdd2" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="13">{escape(comparison)}</text>',
            )
        )


def render_composition_svg(composition: CompositionTotals) -> str:
    """Render lifetime-to-recent composition shifts as static SVG."""
    lifetime_total = sum(composition.lifetime_roles.values())
    recent_total = sum(composition.recent_roles.values())
    dominant_role = max(
        COMPOSITION_ROLE_ORDER, key=lambda role: composition.recent_roles[role]
    )
    dominant_agent = max(
        COMPOSITION_AGENT_ORDER, key=lambda agent: composition.recent_agents[agent]
    )
    title = f"AI compute composition through {composition.as_of.isoformat()}"
    description = (
        f"{_compact_number(lifetime_total)} lifetime tokens and {_compact_number(recent_total)} "
        f"tokens in the trailing 30 days; recent activity is led by {ROLE_LABELS[dominant_role]} "
        f"and {AGENT_LABELS[dominant_agent]}."
    )
    height = 500
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        f'  <desc id="desc">{escape(description)}</desc>',
        f'  <rect width="{WIDTH}" height="{height}" rx="22" fill="#1d1e2c"/>',
        '  <text x="16" y="46" fill="#d9ddf3" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="24" font-weight="600">Compute composition</text>',
        f'  <text x="16" y="72" fill="#85889f" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">Through {composition.as_of.isoformat()} · lifetime baseline vs {composition.recent_start.isoformat()}–{composition.as_of.isoformat()}</text>',
        '  <rect x="16" y="94" width="1148" height="178" rx="16" fill="#222536"/>',
        '  <text x="32" y="124" fill="#d9ddf3" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="18" font-weight="600">Environment</text>',
        '  <text x="1138" y="124" text-anchor="end" fill="#85889f" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="12">RECENT EMPHASIZED</text>',
    ]
    _render_composition_bar(
        lines, bar_id="role-recent", y=138, row_label="Recent 30d",
        values=composition.recent_roles, order=COMPOSITION_ROLE_ORDER, labels=ROLE_LABELS,
        colors=ROLE_COLORS, height=34, emphasize=True,
    )
    _render_composition_bar(
        lines, bar_id="role-lifetime", y=184, row_label="Lifetime",
        values=composition.lifetime_roles, order=COMPOSITION_ROLE_ORDER, labels=ROLE_LABELS,
        colors=ROLE_COLORS, height=18, emphasize=False,
    )
    _render_composition_legend(
        lines, y=242, order=COMPOSITION_ROLE_ORDER, labels=ROLE_LABELS, colors=ROLE_COLORS,
        lifetime=composition.lifetime_roles, recent=composition.recent_roles,
    )
    lines.extend((
        '  <rect x="16" y="288" width="1148" height="178" rx="16" fill="#222536"/>',
        '  <text x="32" y="318" fill="#d9ddf3" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="18" font-weight="600">Agent</text>',
        '  <text x="1138" y="318" text-anchor="end" fill="#85889f" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="12">OPENCODE INCLUDED IN LEGACY</text>',
    ))
    _render_composition_bar(
        lines, bar_id="agent-recent", y=332, row_label="Recent 30d",
        values=composition.recent_agents, order=COMPOSITION_AGENT_ORDER,
        labels=AGENT_LABELS, colors=AGENT_COLORS, height=34, emphasize=True,
    )
    _render_composition_bar(
        lines, bar_id="agent-lifetime", y=378, row_label="Lifetime",
        values=composition.lifetime_agents, order=COMPOSITION_AGENT_ORDER,
        labels=AGENT_LABELS, colors=AGENT_COLORS, height=18, emphasize=False,
    )
    _render_composition_legend(
        lines, y=436, order=COMPOSITION_AGENT_ORDER, labels=AGENT_LABELS,
        colors=AGENT_COLORS, lifetime=composition.lifetime_agents,
        recent=composition.recent_agents,
    )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def render_topology_svg(composition: CompositionTotals) -> str:
    """Render the recent environment-by-agent topology as a separate SVG."""
    active_agents = tuple(
        agent for agent in COMPOSITION_AGENT_ORDER if composition.recent_agents[agent] > 0
    ) or ("codex",)
    recent_total = sum(composition.recent_roles.values())
    title = f"Recent AI compute topology through {composition.as_of.isoformat()}"
    height = 350
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        f'  <desc id="desc">Environment by active agent shares for {_compact_number(recent_total)} tokens in the trailing 30 days.</desc>',
        f'  <rect width="{WIDTH}" height="{height}" rx="22" fill="#1d1e2c"/>',
        '  <text x="16" y="46" fill="#d9ddf3" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="24" font-weight="600">Recent compute topology</text>',
        f'  <text x="16" y="72" fill="#85889f" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">{composition.recent_start.isoformat()}–{composition.as_of.isoformat()} · active agents only · intensity is share within each environment</text>',
    ]
    matrix_x = 260
    matrix_right = 1164
    cell_gap = 10
    cell_width = (matrix_right - matrix_x - cell_gap * (len(active_agents) - 1)) / len(active_agents)
    matrix_y = 130
    row_step = 66
    for column, agent in enumerate(active_agents):
        center = matrix_x + column * (cell_width + cell_gap) + cell_width / 2
        lines.append(
            f'  <text x="{center:.1f}" y="112" text-anchor="middle" fill="#b9bdd2" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="14" font-weight="600">{escape(AGENT_LABELS[agent])}</text>'
        )
    for row, role in enumerate(COMPOSITION_ROLE_ORDER):
        y = matrix_y + row * row_step
        role_total = sum(composition.recent_topology[(role, agent)] for agent in active_agents)
        lines.extend((
            f'  <text x="32" y="{y + 23}" fill="#d9ddf3" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="15" font-weight="600">{escape(ROLE_LABELS[role])}</text>',
            f'  <text x="32" y="{y + 43}" fill="#85889f" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="12">{_compact_number(role_total)} tokens</text>',
        ))
        for column, agent in enumerate(active_agents):
            value = composition.recent_topology[(role, agent)]
            share = _share(value, role_total)
            x = matrix_x + column * (cell_width + cell_gap)
            fill = "#22d3ee" if value else "#272b3a"
            opacity = 0.16 + 0.78 * math.sqrt(share) if value else 1.0
            value_text = f"{_percent(value, role_total)} · {_compact_number(value)}" if value else "—"
            lines.extend((
                f'  <rect x="{x:.1f}" y="{y}" width="{cell_width:.1f}" height="52" rx="10" fill="{fill}" fill-opacity="{opacity:.3f}" data-role="{role}" data-agent="{agent}" data-tokens="{value}">',
                f'    <title>{escape(ROLE_LABELS[role])} × {escape(AGENT_LABELS[agent])}: {_compact_number(value)} tokens ({escape(_percent(value, role_total))} of environment)</title>',
                "  </rect>",
                f'  <text x="{x + cell_width / 2:.1f}" y="{y + 32}" text-anchor="middle" fill="#eef2ff" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="14" font-weight="600">{escape(value_text)}</text>',
            ))
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


def _update_output(path: Path, expected: str, *, check: bool) -> bool:
    try:
        actual = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        actual = None
    changed = actual != expected
    if changed and not check:
        _atomic_write(path, expected)
    return changed


def _latest_activity_day(totals: dict[date, DailyTotals]) -> date:
    return max(
        (day for day, daily in totals.items() if daily.tokens > 0),
        default=datetime.now(SHANGHAI).date(),
    )


def generate(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    totals = aggregate_daily(root)
    if as_of is None:
        as_of = _latest_activity_day(totals)
    expected = render_svg(totals, as_of)
    return _update_output(output, expected, check=check)


def generate_composition(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_composition_svg(aggregate_composition(root, as_of))
    return _update_output(output, expected, check=check)


def generate_topology(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_topology_svg(aggregate_composition(root, as_of))
    return _update_output(output, expected, check=check)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--composition-output",
        type=Path,
        default=DEFAULT_COMPOSITION_OUTPUT,
    )
    parser.add_argument("--topology-output", type=Path, default=DEFAULT_TOPOLOGY_OUTPUT)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--check", action="store_true", help="fail if any SVG is stale")
    args = parser.parse_args()
    outputs = (
        (args.output, generate(args.root, args.output, args.as_of, check=args.check)),
        (
            args.composition_output,
            generate_composition(
                args.root,
                args.composition_output,
                args.as_of,
                check=args.check,
            ),
        ),
        (
            args.topology_output,
            generate_topology(args.root, args.topology_output, args.as_of, check=args.check),
        ),
    )
    for output, changed in outputs:
        if args.check and changed:
            print(f"stale dashboard: {output}")
        elif changed:
            print(f"rendered {output}")
        else:
            print(f"dashboard is current: {output}")
    return 1 if args.check and any(changed for _, changed in outputs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
