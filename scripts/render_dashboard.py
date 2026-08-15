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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "assets" / "token-activity.svg"
DEFAULT_TOPOLOGY_OUTPUT = REPO_ROOT / "assets" / "token-topology.svg"
DEFAULT_ALLOCATION_OUTPUT = REPO_ROOT / "assets" / "compute-allocation.svg"
AGENT_FILES = frozenset({"claude.json", "codex.json", "opencode.json", "traex.json"})
IGNORED_PARTS = frozenset({".git", ".venv", "__pycache__"})
SHANGHAI = ZoneInfo("Asia/Shanghai")

ROLE_ORDER = ("work", "personal", "devbox", "trail")
TOPOLOGY_ROLE_ORDER = ("work", "personal", "development")
AGENT_ORDER = ("claude", "codex", "opencode", "traex")
TOPOLOGY_AGENT_ORDER = ("claude", "codex", "traex", "legacy")
ROLE_LABELS = {"work": "Work", "personal": "Personal", "development": "Development"}
ROLE_BUCKETS = {
    "work": "work",
    "personal": "personal",
    "devbox": "development",
    "trail": "development",
}
AGENT_LABELS = {"claude": "Claude", "codex": "Codex", "traex": "TRAE", "legacy": "Legacy"}
AGENT_BUCKETS = {"claude": "claude", "codex": "codex", "opencode": "legacy", "traex": "traex"}
ALLOCATION_AGENT_ORDER = ("claude", "codex", "traex", "legacy")
EFFORT_ORDER = ("none", "low", "medium", "high", "xhigh", "max")
SPEED_ORDER = ("standard", "fast")
LEVEL_CLASSES = tuple(f"heatmap-level-{level}" for level in range(5))


def _theme_style_lines(
    *, topology: bool = False, allocation: bool = False
) -> tuple[str, ...]:
    light_levels = (
        (
            "    .topology-label-0 { fill: #6c6f85; }",
            "    .topology-label-1, .topology-label-2 { fill: #4c4f69; }",
            "    .topology-label-3, .topology-label-4 { fill: #eff1f5; }",
            "    .topology-label-work-3, .topology-label-work-4 { fill: #4c4f69; }",
            "    .topology-work { fill: #fe640b; }",
            "    .topology-personal { fill: #1e66f5; }",
            "    .topology-development { fill: #8839ef; }",
            "    .topology-level-1 { fill-opacity: 0.22; }",
            "    .topology-level-2 { fill-opacity: 0.45; }",
            "    .topology-level-3 { fill-opacity: 0.68; }",
            "    .topology-level-4 { fill-opacity: 1; }",
            "    .heatmap-level-0 { fill: #ccd0da; }",
        )
        if topology
        else (
            "    .heatmap-level-0 { fill: #ccd0da; }",
            "    .heatmap-level-1 { fill: #179299; fill-opacity: 0.25; }",
            "    .heatmap-level-2 { fill: #179299; fill-opacity: 0.5; }",
            "    .heatmap-level-3 { fill: #179299; fill-opacity: 0.75; }",
            "    .heatmap-level-4 { fill: #179299; }",
        )
    )
    dark_levels = (
        (
            "      .topology-label-0 { fill: #a6adc8; }",
            "      .topology-label-1, .topology-label-2 { fill: #cdd6f4; }",
            "      .topology-label-3, .topology-label-4,",
            "      .topology-label-work-3, .topology-label-work-4 { fill: #1e1e2e; }",
            "      .topology-work { fill: #fab387; }",
            "      .topology-personal { fill: #89b4fa; }",
            "      .topology-development { fill: #cba6f7; }",
            "      .heatmap-level-0 { fill: #313244; }",
        )
        if topology
        else (
            "      .heatmap-level-0 { fill: #313244; }",
            "      .heatmap-level-1 { fill: #94e2d5; fill-opacity: 0.25; }",
            "      .heatmap-level-2 { fill: #94e2d5; fill-opacity: 0.5; }",
            "      .heatmap-level-3 { fill: #94e2d5; fill-opacity: 0.75; }",
            "      .heatmap-level-4 { fill: #94e2d5; }",
        )
    )
    light_agents = (
        "    .agent-claude { fill: #fe640b; }",
        "    .agent-codex { fill: #1e66f5; }",
        "    .agent-traex { fill: #8839ef; }",
        "    .agent-legacy { fill: #6c6f85; }",
    ) if allocation else ()
    dark_agents = (
        "      .agent-claude { fill: #fab387; }",
        "      .agent-codex { fill: #89b4fa; }",
        "      .agent-traex { fill: #cba6f7; }",
        "      .agent-legacy { fill: #a6adc8; }",
    ) if allocation else ()
    return (
        "  <style>",
        "    .dashboard-background { fill: #eff1f5; }",
        "    .dashboard-panel { fill: #e6e9ef; }",
        "    .dashboard-primary { fill: #4c4f69; }",
        "    .dashboard-secondary { fill: #5c5f77; }",
        "    .dashboard-muted { fill: #6c6f85; }",
        "    .dashboard-accent { fill: #179299; }",
        "    .dashboard-border { stroke: #ccd0da; }",
        *light_agents,
        *light_levels,
        "    @media (prefers-color-scheme: dark) {",
        "      .dashboard-background { fill: #1e1e2e; }",
        "      .dashboard-panel { fill: #181825; }",
        "      .dashboard-primary { fill: #cdd6f4; }",
        "      .dashboard-secondary { fill: #bac2de; }",
        "      .dashboard-muted { fill: #a6adc8; }",
        "      .dashboard-accent { fill: #94e2d5; }",
        "      .dashboard-border { stroke: #313244; }",
        *dark_agents,
        *dark_levels,
        "    }",
        "  </style>",
    )
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
class TopologyTotals:
    as_of: date
    recent_start: date
    recent_roles: dict[str, int]
    recent_agents: dict[str, int]
    recent_topology: dict[tuple[str, str], int]


@dataclass(frozen=True)
class AllocationTotals:
    as_of: date
    recent_start: date
    agent_tokens: dict[str, int]
    model_tokens: dict[tuple[str, str], int]
    efforts: dict[str, dict[str, int]]
    speeds: dict[str, dict[str, int]]
    components: dict[str, int]
    quota_windows: dict[int, float]
    quota_limit_days: int


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


def aggregate_topology(root: Path, as_of: date) -> TopologyTotals:
    """Aggregate recent token usage by public environment and agent."""
    recent_start = as_of - timedelta(days=29)
    recent_roles = {role: 0 for role in TOPOLOGY_ROLE_ORDER}
    recent_agents = {agent: 0 for agent in TOPOLOGY_AGENT_ORDER}
    recent_topology = {
        (role, agent): 0
        for role in TOPOLOGY_ROLE_ORDER
        for agent in TOPOLOGY_AGENT_ORDER
    }

    for record in load_usage_records(root):
        if not recent_start <= record.day <= as_of:
            continue
        role_bucket = ROLE_BUCKETS[record.role]
        agent_bucket = AGENT_BUCKETS[record.agent]
        recent_roles[role_bucket] += record.tokens
        recent_agents[agent_bucket] += record.tokens
        recent_topology[(role_bucket, agent_bucket)] += record.tokens

    return TopologyTotals(
        as_of=as_of,
        recent_start=recent_start,
        recent_roles=recent_roles,
        recent_agents=recent_agents,
        recent_topology=recent_topology,
    )


def aggregate_allocation(root: Path, as_of: date) -> AllocationTotals:
    """Aggregate the trailing 30-day harness, model, and routing dimensions."""
    recent_start = as_of - timedelta(days=29)
    agent_tokens = {agent: 0 for agent in ALLOCATION_AGENT_ORDER}
    model_tokens: defaultdict[tuple[str, str], int] = defaultdict(int)
    efforts = {
        effort: {"turns": 0, "totalTokens": 0, "reasoningOutputTokens": 0}
        for effort in EFFORT_ORDER
    }
    speeds = {
        speed: {"turns": 0, "totalTokens": 0}
        for speed in SPEED_ORDER
    }
    components = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheCreationTokens": 0,
        "cacheReadTokens": 0,
    }
    quota_windows: dict[int, float] = {}
    quota_limit_days: set[date] = set()
    for path in discover_agent_files(root):
        raw_agent = path.stem
        agent = AGENT_BUCKETS[raw_agent]
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc
        if not isinstance(store, dict):
            raise ValueError(f"expected a date-keyed object in {path}")
        for raw_day, entry in store.items():
            try:
                day = date.fromisoformat(raw_day)
            except (TypeError, ValueError):
                continue
            if not recent_start <= day <= as_of or not isinstance(entry, dict):
                continue
            tokens = entry.get("totalTokens", 0)
            if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
                agent_tokens[agent] += max(0, int(tokens))
            models = entry.get("models", {})
            if isinstance(models, dict):
                for model, payload in models.items():
                    if not isinstance(model, str) or not isinstance(payload, dict):
                        continue
                    model_tokens[(agent, model)] += max(
                        0, int(payload.get("totalTokens", 0))
                    )
                    for key in components:
                        value = payload.get(key)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            components[key] += max(0, int(value))
            routing = entry.get("routing", {})
            if isinstance(routing, dict):
                for label, payload in routing.get("efforts", {}).items():
                    if label not in efforts or not isinstance(payload, dict):
                        continue
                    for key in efforts[label]:
                        value = payload.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            efforts[label][key] += max(0, value)
                for label, payload in routing.get("speeds", {}).items():
                    if label not in speeds or not isinstance(payload, dict):
                        continue
                    for key in speeds[label]:
                        value = payload.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            speeds[label][key] += max(0, value)
            quota = entry.get("quota", {})
            if isinstance(quota, dict):
                for raw_minutes, percent in quota.get("windows", {}).items():
                    try:
                        minutes = int(raw_minutes)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                        quota_windows[minutes] = max(
                            float(percent), quota_windows.get(minutes, 0.0)
                        )
                if quota.get("limitReached") is True:
                    quota_limit_days.add(day)
    return AllocationTotals(
        as_of=as_of,
        recent_start=recent_start,
        agent_tokens=agent_tokens,
        model_tokens=dict(model_tokens),
        efforts=efforts,
        speeds=speeds,
        components=components,
        quota_windows=quota_windows,
        quota_limit_days=len(quota_limit_days),
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
        *_theme_style_lines(),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        (
            f'  <rect class="dashboard-border" x="{CARD_X}" y="18" '
            f'width="{CARD_WIDTH}" height="112" rx="18" fill="none" stroke-width="2"/>'
        ),
    ]

    stat_width = CARD_WIDTH / 5
    for index, (value, label) in enumerate(stats):
        center = CARD_X + stat_width * index + stat_width / 2
        if index:
            divider = CARD_X + stat_width * index
            lines.append(
                f'  <line class="dashboard-border" x1="{divider:.1f}" y1="36" '
                f'x2="{divider:.1f}" y2="112" stroke-width="1"/>'
            )
        lines.extend(
            (
                f'  <text class="dashboard-accent" x="{center:.1f}" y="52" text-anchor="middle" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="22" font-weight="600">{escape(_compact_number(value.tokens))} tokens</text>',
                f'  <text class="dashboard-primary" x="{center:.1f}" y="78" text-anchor="middle" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="16" font-weight="500">{escape(_compact_cost(value.cost))}</text>',
                f'  <text class="dashboard-muted" x="{center:.1f}" y="106" text-anchor="middle" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="14">{escape(label)}</text>',
            )
        )

    active_center = CARD_X + stat_width * 4 + stat_width / 2
    divider = CARD_X + stat_width * 4
    lines.extend(
        (
            f'  <line class="dashboard-border" x1="{divider:.1f}" y1="36" '
            f'x2="{divider:.1f}" y2="112" stroke-width="1"/>',
            f'  <text class="dashboard-primary" x="{active_center:.1f}" y="66" text-anchor="middle" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="25" font-weight="600">{active_days} days</text>',
            f'  <text class="dashboard-muted" x="{active_center:.1f}" y="106" text-anchor="middle" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="14">Active days</text>',
        )
    )

    lines.extend(
        (
            '  <text class="dashboard-primary" x="16" y="174" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="22" font-weight="500">Compute activity</text>',
        )
    )

    for weekday, label in ((0, "Mon"), (2, "Wed"), (4, "Fri")):
        y = GRID_TOP + weekday * CELL_STEP + 13
        lines.append(
            f'  <text class="dashboard-muted" x="16" y="{y}" '
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
            f'  <text class="dashboard-muted" x="{x}" y="204" '
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
                    f'  <rect class="{LEVEL_CLASSES[level]}" x="{x}" y="{y}" '
                    f'width="{CELL_SIZE}" height="{CELL_SIZE}" rx="4" data-date="{current.isoformat()}" '
                    f'data-tokens="{tokens}" data-level="{level}">',
                    f"    <title>{escape(label)}</title>",
                    "  </rect>",
                )
            )
        current += timedelta(days=1)

    footer_y = GRID_TOP + 7 * CELL_STEP + 27
    lines.append(
        f'  <text class="dashboard-muted" x="{GRID_LEFT}" y="{footer_y}" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="12">Less</text>'
    )
    legend_x = GRID_LEFT + 34
    for level, level_class in enumerate(LEVEL_CLASSES):
        x = legend_x + level * CELL_STEP
        lines.append(
            f'  <rect class="{level_class}" x="{x}" y="{footer_y - 12}" '
            f'width="{CELL_SIZE}" height="{CELL_SIZE}" rx="4"/>'
        )
    lines.append(
        f'  <text class="dashboard-muted" '
        f'x="{legend_x + len(LEVEL_CLASSES) * CELL_STEP + 2}" y="{footer_y}" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
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



def _topology_level(share: float) -> int:
    if share <= 0:
        return 0
    return min(4, max(1, int(share * 4) + 1))


def render_topology_svg(topology: TopologyTotals) -> str:
    """Render recent environment-by-agent shares as an adaptive heatmap."""
    active_agents = tuple(
        agent for agent in TOPOLOGY_AGENT_ORDER if topology.recent_agents[agent] > 0
    )
    recent_total = sum(topology.recent_roles.values())
    title = f"Recent AI compute topology through {topology.as_of.isoformat()}"
    height = 350
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        f'  <desc id="desc">Environment by active agent shares for '
        f'{_compact_number(recent_total)} tokens in the trailing 30 days.</desc>',
        *_theme_style_lines(topology=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="46" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Recent compute topology</text>',
        f'  <text class="dashboard-muted" x="16" y="72" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{topology.recent_start.isoformat()}–{topology.as_of.isoformat()} · active agents only · '
        'hue is environment · intensity is agent share within each row</text>',
    ]
    if not active_agents:
        lines.extend(
            (
                '  <rect class="dashboard-panel" x="16" y="104" width="1148" '
                'height="214" rx="16"/>',
                '  <text class="dashboard-primary" x="590" y="198" text-anchor="middle" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                'font-size="20" font-weight="600">No recent activity</text>',
                '  <text class="dashboard-muted" x="590" y="228" text-anchor="middle" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                'font-size="13">No agents recorded token usage in this 30-day window.</text>',
                "</svg>",
            )
        )
        return "\n".join(lines) + "\n"

    matrix_x = 260
    matrix_right = 1164
    cell_gap = 10
    cell_width = (
        matrix_right - matrix_x - cell_gap * (len(active_agents) - 1)
    ) / len(active_agents)
    matrix_y = 130
    row_step = 66
    for column, agent in enumerate(active_agents):
        center = matrix_x + column * (cell_width + cell_gap) + cell_width / 2
        lines.append(
            f'  <text class="dashboard-secondary" x="{center:.1f}" y="112" '
            'text-anchor="middle" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="14" font-weight="600">{escape(AGENT_LABELS[agent])}</text>'
        )
    for row, role in enumerate(TOPOLOGY_ROLE_ORDER):
        y = matrix_y + row * row_step
        role_total = sum(topology.recent_topology[(role, agent)] for agent in active_agents)
        lines.extend(
            (
                f'  <circle class="topology-{role}" cx="36" cy="{y + 18}" r="5"/>',
                f'  <text class="dashboard-primary" x="50" y="{y + 23}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="15" font-weight="600">{escape(ROLE_LABELS[role])}</text>',
                f'  <text class="dashboard-muted" x="50" y="{y + 43}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="12">{_compact_number(role_total)} tokens</text>',
            )
        )
        for column, agent in enumerate(active_agents):
            value = topology.recent_topology[(role, agent)]
            share = _share(value, role_total)
            level = _topology_level(share)
            cell_class = (
                "heatmap-level-0"
                if level == 0
                else f"topology-{role} topology-level-{level}"
            )
            x = matrix_x + column * (cell_width + cell_gap)
            value_text = (
                f"{_percent(value, role_total)} · {_compact_number(value)}" if value else "—"
            )
            lines.extend(
                (
                    f'  <rect class="{cell_class}" x="{x:.1f}" y="{y}" '
                    f'width="{cell_width:.1f}" height="52" rx="10" data-role="{role}" '
                    f'data-agent="{agent}" data-tokens="{value}" data-level="{level}">',
                    f'    <title>{escape(ROLE_LABELS[role])} × '
                    f'{escape(AGENT_LABELS[agent])}: {_compact_number(value)} tokens '
                    f'({escape(_percent(value, role_total))} of environment)</title>',
                    "  </rect>",
                    f'  <text class="topology-label-{level} topology-label-{role}-{level}" '
                    f'x="{x + cell_width / 2:.1f}" '
                    f'y="{y + 32}" text-anchor="middle" '
                    'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                    f'font-size="14" font-weight="600">{escape(value_text)}</text>',
                )
            )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _window_label(minutes: int) -> str:
    if minutes % (7 * 24 * 60) == 0:
        return f"{minutes // (7 * 24 * 60)}w window"
    if minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)}d window"
    if minutes % 60 == 0:
        return f"{minutes // 60}h window"
    return f"{minutes}m window"


def render_allocation_svg(allocation: AllocationTotals) -> str:
    """Render recent harness/model allocation plus observed routing efficiency."""
    total = sum(allocation.agent_tokens.values())
    title = f"Recent AI compute allocation through {allocation.as_of.isoformat()}"
    height = 560
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        f'  <desc id="desc">Harness and model allocation for {_compact_number(total)} '
        'tokens in the trailing 30 days, with privacy-safe effort, speed, cache, and '
        'quota telemetry where observed.</desc>',
        *_theme_style_lines(allocation=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Compute allocation</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{allocation.recent_start.isoformat()}–{allocation.as_of.isoformat()} · '
        'routing telemetry is aggregate-only; no projects or sessions</text>',
    ]

    bar_x, bar_y, bar_width, bar_height = 16, 88, 1148, 34
    lines.append(
        f'  <rect class="dashboard-panel" x="{bar_x}" y="{bar_y}" '
        f'width="{bar_width}" height="{bar_height}" rx="10"/>'
    )
    cursor = float(bar_x)
    active_agents = [
        agent for agent in ALLOCATION_AGENT_ORDER if allocation.agent_tokens[agent] > 0
    ]
    for index, agent in enumerate(active_agents):
        value = allocation.agent_tokens[agent]
        width = bar_width * _share(value, total)
        if index == len(active_agents) - 1:
            width = bar_x + bar_width - cursor
        lines.extend((
            f'  <rect class="agent-{agent}" x="{cursor:.1f}" y="{bar_y}" '
            f'width="{max(0.0, width):.1f}" height="{bar_height}" rx="10" '
            f'data-agent="{agent}" data-tokens="{value}">',
            f'    <title>{escape(AGENT_LABELS[agent])}: {_compact_number(value)} tokens '
            f'({escape(_percent(value, total))})</title>',
            '  </rect>',
        ))
        cursor += width

    legend_x = 18
    for agent in active_agents:
        value = allocation.agent_tokens[agent]
        lines.extend((
            f'  <circle class="agent-{agent}" cx="{legend_x + 5}" cy="148" r="5"/>',
            f'  <text class="dashboard-secondary" x="{legend_x + 16}" y="153" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="13">{escape(AGENT_LABELS[agent])} {escape(_percent(value, total))} · '
            f'{_compact_number(value)}</text>',
        ))
        legend_x += 250

    panel_y, panel_h, panel_w, panel_gap = 178, 220, 372, 16
    for column, agent in enumerate(("claude", "codex", "traex")):
        x = 16 + column * (panel_w + panel_gap)
        agent_total = allocation.agent_tokens[agent]
        observed = sorted(
            (
                (model, tokens)
                for (bucket, model), tokens in allocation.model_tokens.items()
                if bucket == agent and tokens > 0
            ),
            key=lambda item: (-item[1], item[0]),
        )
        observed_total = sum(tokens for _, tokens in observed)
        lines.extend((
            f'  <rect class="dashboard-panel" x="{x}" y="{panel_y}" '
            f'width="{panel_w}" height="{panel_h}" rx="16"/>',
            f'  <circle class="agent-{agent}" cx="{x + 22}" cy="{panel_y + 27}" r="6"/>',
            f'  <text class="dashboard-primary" x="{x + 36}" y="{panel_y + 33}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="17" font-weight="600">{escape(AGENT_LABELS[agent])}</text>',
            f'  <text class="dashboard-muted" x="{x + panel_w - 18}" y="{panel_y + 33}" '
            'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="12">{_compact_number(agent_total)} tokens</text>',
        ))
        if not observed:
            lines.append(
                f'  <text class="dashboard-muted" x="{x + 18}" y="{panel_y + 82}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                'font-size="13">No model detail observed</text>'
            )
            continue
        for row, (model, tokens) in enumerate(observed[:4]):
            y = panel_y + 66 + row * 37
            bar_width_value = 112 * _share(tokens, observed_total)
            label = model if len(model) <= 27 else model[:26] + "…"
            lines.extend((
                f'  <text class="dashboard-secondary" x="{x + 18}" y="{y}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="12">{escape(label)}</text>',
                f'  <rect class="dashboard-border" x="{x + 214}" y="{y - 11}" '
                'width="112" height="9" rx="4" fill="none" stroke-width="1"/>',
                f'  <rect class="agent-{agent}" x="{x + 214}" y="{y - 11}" '
                f'width="{bar_width_value:.1f}" height="9" rx="4" '
                f'data-agent="{agent}" data-model="{escape(model)}" data-tokens="{tokens}">',
                f'    <title>{escape(model)}: {_compact_number(tokens)} tokens '
                f'({escape(_percent(tokens, observed_total))} of observed '
                f'{escape(AGENT_LABELS[agent])} model tokens)</title>',
                '  </rect>',
                f'  <text class="dashboard-muted" x="{x + panel_w - 18}" y="{y}" '
                'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="11">{escape(_percent(tokens, observed_total))}</text>',
            ))
        if len(observed) > 4:
            lines.append(
                f'  <text class="dashboard-muted" x="{x + 18}" y="{panel_y + 206}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="11">+{len(observed) - 4} more observed models</text>'
            )

    metric_y, metric_h, metric_gap = 420, 116, 12
    metric_w = (1148 - metric_gap * 3) / 4
    metric_specs: list[tuple[str, str, str]] = []
    effort_tokens = sum(item["totalTokens"] for item in allocation.efforts.values())
    effort_turns = sum(item["turns"] for item in allocation.efforts.values())
    if effort_tokens:
        effort_summary = " · ".join(
            f"{name} {_percent(allocation.efforts[name]['totalTokens'], effort_tokens)}"
            for name in EFFORT_ORDER if allocation.efforts[name]["totalTokens"]
        )
        metric_specs.append(("Codex effort", f"{effort_turns} turns", effort_summary))
    else:
        metric_specs.append(("Codex effort", "Awaiting telemetry", "low · medium · high"))

    speed_tokens = sum(item["totalTokens"] for item in allocation.speeds.values())
    speed_turns = sum(item["turns"] for item in allocation.speeds.values())
    if speed_tokens:
        fast = allocation.speeds["fast"]["totalTokens"]
        metric_specs.append(("Speed tier", f"{_percent(fast, speed_tokens)} fast", f"{speed_turns} observed turns"))
    else:
        metric_specs.append(("Speed tier", "Awaiting telemetry", "standard · fast"))

    component_total = sum(allocation.components.values())
    if component_total:
        cache_read = allocation.components["cacheReadTokens"]
        metric_specs.append((
            "Cache reuse", _percent(cache_read, component_total),
            f"of {_compact_number(component_total)} component tokens",
        ))
    else:
        metric_specs.append(("Cache reuse", "Awaiting telemetry", "read share of component tokens"))

    if allocation.quota_windows:
        minutes, percent = max(
            allocation.quota_windows.items(), key=lambda item: (item[1], item[0])
        )
        limit_text = (
            f"{allocation.quota_limit_days} limit-hit day(s)"
            if allocation.quota_limit_days else "no limit-hit days observed"
        )
        metric_specs.append(("Codex quota", f"{percent:.0f}% peak", f"{_window_label(minutes)} · {limit_text}"))
    else:
        metric_specs.append(("Codex quota", "Awaiting telemetry", "peak observed window pressure"))

    for index, (label, value, detail) in enumerate(metric_specs):
        x = 16 + index * (metric_w + metric_gap)
        lines.extend((
            f'  <rect class="dashboard-panel" x="{x:.1f}" y="{metric_y}" '
            f'width="{metric_w:.1f}" height="{metric_h}" rx="14"/>',
            f'  <text class="dashboard-muted" x="{x + 16:.1f}" y="{metric_y + 27}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="12">{escape(label)}</text>',
            f'  <text class="dashboard-primary" x="{x + 16:.1f}" y="{metric_y + 61}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="18" font-weight="600">{escape(value)}</text>',
            f'  <text class="dashboard-muted" x="{x + 16:.1f}" y="{metric_y + 88}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="11">{escape(detail)}</text>',
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


def generate_topology(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_topology_svg(aggregate_topology(root, as_of))
    return _update_output(output, expected, check=check)


def generate_allocation(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_allocation_svg(aggregate_allocation(root, as_of))
    return _update_output(output, expected, check=check)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--topology-output",
        type=Path,
        default=DEFAULT_TOPOLOGY_OUTPUT,
    )
    parser.add_argument(
        "--allocation-output",
        type=Path,
        default=DEFAULT_ALLOCATION_OUTPUT,
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--check", action="store_true", help="fail if any SVG is stale")
    args = parser.parse_args()
    outputs = (
        (args.output, generate(args.root, args.output, args.as_of, check=args.check)),
        (
            args.topology_output,
            generate_topology(
                args.root,
                args.topology_output,
                args.as_of,
                check=args.check,
            ),
        ),
        (
            args.allocation_output,
            generate_allocation(
                args.root,
                args.allocation_output,
                args.as_of,
                check=args.check,
            ),
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
