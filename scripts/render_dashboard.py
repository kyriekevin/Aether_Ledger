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
DEFAULT_TOPOLOGY_HISTORY_OUTPUT = REPO_ROOT / "assets" / "token-topology-history.svg"
DEFAULT_ALLOCATION_OUTPUT = REPO_ROOT / "assets" / "compute-allocation.svg"
DEFAULT_ALLOCATION_HISTORY_OUTPUT = REPO_ROOT / "assets" / "compute-allocation-history.svg"
DEFAULT_EFFICIENCY_OUTPUT = REPO_ROOT / "assets" / "compute-efficiency.svg"
DEFAULT_EFFICIENCY_HISTORY_OUTPUT = REPO_ROOT / "assets" / "compute-efficiency-history.svg"
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
EFFORT_SHORT_LABELS = {
    "none": "none",
    "low": "low",
    "medium": "med",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
SPEED_ORDER = ("standard", "fast")
COMPONENT_ORDER = (
    "inputTokens",
    "outputTokens",
    "cacheCreationTokens",
    "cacheReadTokens",
)
COMPONENT_LABELS = {
    "inputTokens": "input",
    "outputTokens": "output",
    "cacheCreationTokens": "cache write",
    "cacheReadTokens": "cache read",
}
LEVEL_CLASSES = tuple(f"heatmap-level-{level}" for level in range(5))


def _theme_style_lines(
    *, topology: bool = False, allocation: bool = False
) -> tuple[str, ...]:
    light_levels = (
        (
            "    .topology-label-0 { fill: #6c6f85; }",
            "    .topology-label-1, .topology-label-2 { fill: #4c4f69; }",
            "    .topology-label-3, .topology-label-4 { fill: #eff1f5; }",
            "    .topology-label-agent-claude-3,",
            "    .topology-label-agent-claude-4 { fill: #4c4f69; }",
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
            "      .topology-label-3, .topology-label-4 { fill: #1e1e2e; }",
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
        *((
        "    .series-0 { fill: #1e66f5; }",
        "    .series-1 { fill: #fe640b; }",
        "    .series-2 { fill: #40a02b; }",
        "    .series-3 { fill: #8839ef; }",
        "    .series-other { fill: #9ca0b0; }",
        "    .signal-level-1 { fill: #179299; fill-opacity: 0.22; }",
        "    .signal-level-2 { fill: #179299; fill-opacity: 0.45; }",
        "    .signal-level-3 { fill: #179299; fill-opacity: 0.68; }",
        "    .signal-level-4 { fill: #179299; }",
        ) if allocation else ()),
    ) if allocation or topology else ()
    light_components = (
        "    .component-input { fill: #40a02b; }",
        "    .component-output { fill: #df8e1d; }",
        "    .component-cache-write { fill: #179299; }",
        "    .component-cache-read { fill: #7287fd; }",
    ) if allocation else ()
    dark_agents = (
        "      .agent-claude { fill: #fab387; }",
        "      .agent-codex { fill: #89b4fa; }",
        "      .agent-traex { fill: #cba6f7; }",
        "      .agent-legacy { fill: #a6adc8; }",
        *((
        "      .series-0 { fill: #89b4fa; }",
        "      .series-1 { fill: #fab387; }",
        "      .series-2 { fill: #a6e3a1; }",
        "      .series-3 { fill: #cba6f7; }",
        "      .series-other { fill: #7f849c; }",
        "      .signal-level-1 { fill: #94e2d5; fill-opacity: 0.22; }",
        "      .signal-level-2 { fill: #94e2d5; fill-opacity: 0.45; }",
        "      .signal-level-3 { fill: #94e2d5; fill-opacity: 0.68; }",
        "      .signal-level-4 { fill: #94e2d5; }",
        ) if allocation else ()),
    ) if allocation or topology else ()
    dark_components = (
        "      .component-input { fill: #a6e3a1; }",
        "      .component-output { fill: #f9e2af; }",
        "      .component-cache-write { fill: #94e2d5; }",
        "      .component-cache-read { fill: #b4befe; }",
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
        *light_components,
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
        *dark_components,
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
    window_starts: tuple[date, ...]
    weekly_topology: tuple[dict[tuple[str, str], int], ...]


@dataclass(frozen=True)
class AllocationTotals:
    as_of: date
    recent_start: date
    agent_tokens: dict[str, int]
    model_tokens: dict[tuple[str, str], int]
    trend_starts: tuple[date, ...]
    weekly_model_tokens: tuple[dict[tuple[str, str], int], ...]
    weekly_model_observed: tuple[set[str], ...]
    weekly_components: tuple[dict[tuple[str, str], int], ...]
    weekly_component_observed: tuple[set[str], ...]
    weekly_efforts: tuple[dict[tuple[str, str], int], ...]
    weekly_reasoning: tuple[dict[str, int], ...]
    weekly_effort_observed: tuple[set[str], ...]
    weekly_speeds: tuple[dict[tuple[str, str], int], ...]
    weekly_speed_observed: tuple[set[str], ...]
    weekly_quota: tuple[dict[str, float], ...]
    weekly_quota_observed: tuple[set[str], ...]
    efforts: dict[str, dict[str, dict[str, int]]]
    speeds: dict[str, dict[str, dict[str, int]]]
    components: dict[str, dict[str, int]]
    quota_windows: dict[str, dict[int, float]]
    quota_limit_days: dict[str, int]


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
    trend_start = as_of - timedelta(days=83)
    window_starts = tuple(
        trend_start + timedelta(days=index * 7) for index in range(12)
    )
    recent_roles = {role: 0 for role in TOPOLOGY_ROLE_ORDER}
    recent_agents = {agent: 0 for agent in TOPOLOGY_AGENT_ORDER}
    recent_topology = {
        (role, agent): 0
        for role in TOPOLOGY_ROLE_ORDER
        for agent in TOPOLOGY_AGENT_ORDER
    }
    weekly_topology = tuple(defaultdict(int) for _ in window_starts)

    for record in load_usage_records(root):
        role_bucket = ROLE_BUCKETS[record.role]
        agent_bucket = AGENT_BUCKETS[record.agent]
        if trend_start <= record.day <= as_of:
            index = (record.day - trend_start).days // 7
            weekly_topology[index][(role_bucket, agent_bucket)] += record.tokens
        if recent_start <= record.day <= as_of:
            recent_roles[role_bucket] += record.tokens
            recent_agents[agent_bucket] += record.tokens
            recent_topology[(role_bucket, agent_bucket)] += record.tokens

    return TopologyTotals(
        as_of=as_of,
        recent_start=recent_start,
        recent_roles=recent_roles,
        recent_agents=recent_agents,
        recent_topology=recent_topology,
        window_starts=window_starts,
        weekly_topology=tuple(dict(window) for window in weekly_topology),
    )


def aggregate_allocation(root: Path, as_of: date) -> AllocationTotals:
    """Aggregate the trailing 30-day harness, model, and routing dimensions."""
    recent_start = as_of - timedelta(days=29)
    trend_start = as_of - timedelta(days=83)
    trend_starts = tuple(
        trend_start + timedelta(days=index * 7) for index in range(12)
    )
    agent_tokens = {agent: 0 for agent in ALLOCATION_AGENT_ORDER}
    model_tokens: defaultdict[tuple[str, str], int] = defaultdict(int)
    weekly_model_tokens = tuple(defaultdict(int) for _ in trend_starts)
    weekly_model_observed = tuple(set() for _ in trend_starts)
    weekly_components = tuple(defaultdict(int) for _ in trend_starts)
    weekly_component_observed = tuple(set() for _ in trend_starts)
    weekly_efforts = tuple(defaultdict(int) for _ in trend_starts)
    weekly_reasoning = tuple(defaultdict(int) for _ in trend_starts)
    weekly_effort_observed = tuple(set() for _ in trend_starts)
    weekly_speeds = tuple(defaultdict(int) for _ in trend_starts)
    weekly_speed_observed = tuple(set() for _ in trend_starts)
    weekly_quota = tuple(defaultdict(float) for _ in trend_starts)
    weekly_quota_observed = tuple(set() for _ in trend_starts)
    efforts = {
        agent: {
            effort: {"turns": 0, "totalTokens": 0, "reasoningOutputTokens": 0}
            for effort in EFFORT_ORDER
        }
        for agent in ALLOCATION_AGENT_ORDER
    }
    speeds = {
        agent: {
            speed: {"turns": 0, "totalTokens": 0}
            for speed in SPEED_ORDER
        }
        for agent in ALLOCATION_AGENT_ORDER
    }
    components = {
        agent: {key: 0 for key in COMPONENT_ORDER}
        for agent in ALLOCATION_AGENT_ORDER
    }
    quota_windows: dict[str, dict[int, float]] = {
        agent: {} for agent in ALLOCATION_AGENT_ORDER
    }
    quota_limit_days: dict[str, set[date]] = {
        agent: set() for agent in ALLOCATION_AGENT_ORDER
    }
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
            if not isinstance(entry, dict):
                continue
            tokens = entry.get("totalTokens", 0)
            if trend_start <= day <= as_of:
                index = (day - trend_start).days // 7
                trend_models = entry.get("models", {})
                if isinstance(trend_models, dict) and trend_models:
                    weekly_model_observed[index].add(agent)
                    component_observed = False
                    for model, payload in trend_models.items():
                        if not isinstance(model, str) or not isinstance(payload, dict):
                            continue
                        weekly_model_tokens[index][(agent, model)] += max(
                            0, int(payload.get("totalTokens", 0))
                        )
                        for key in COMPONENT_ORDER:
                            value = payload.get(key)
                            if isinstance(value, (int, float)) and not isinstance(value, bool):
                                weekly_components[index][(agent, key)] += max(0, int(value))
                                component_observed = True
                    if component_observed:
                        weekly_component_observed[index].add(agent)
                trend_routing = entry.get("routing", {})
                if isinstance(trend_routing, dict):
                    trend_efforts = trend_routing.get("efforts", {})
                    if isinstance(trend_efforts, dict):
                        effort_observed = False
                        for label, payload in trend_efforts.items():
                            if label not in EFFORT_ORDER or not isinstance(payload, dict):
                                continue
                            effort_tokens = payload.get("totalTokens")
                            reasoning_tokens = payload.get("reasoningOutputTokens")
                            if isinstance(effort_tokens, int) and not isinstance(
                                effort_tokens, bool
                            ):
                                weekly_efforts[index][(agent, label)] += max(
                                    0, effort_tokens
                                )
                                effort_observed = True
                            if isinstance(reasoning_tokens, int) and not isinstance(
                                reasoning_tokens, bool
                            ):
                                weekly_reasoning[index][agent] += max(
                                    0, reasoning_tokens
                                )
                        if effort_observed:
                            weekly_effort_observed[index].add(agent)
                    trend_speeds = trend_routing.get("speeds", {})
                    if isinstance(trend_speeds, dict):
                        speed_observed = False
                        for label, payload in trend_speeds.items():
                            if label not in SPEED_ORDER or not isinstance(payload, dict):
                                continue
                            speed_tokens = payload.get("totalTokens")
                            if isinstance(speed_tokens, int) and not isinstance(
                                speed_tokens, bool
                            ):
                                weekly_speeds[index][(agent, label)] += max(
                                    0, speed_tokens
                                )
                                speed_observed = True
                        if speed_observed:
                            weekly_speed_observed[index].add(agent)
                trend_quota = entry.get("quota", {})
                if isinstance(trend_quota, dict):
                    for percent in trend_quota.get("windows", {}).values():
                        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                            weekly_quota[index][agent] = max(
                                float(percent), weekly_quota[index][agent]
                            )
                            weekly_quota_observed[index].add(agent)
            if not recent_start <= day <= as_of:
                continue
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
                    for key in COMPONENT_ORDER:
                        value = payload.get(key)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            components[agent][key] += max(0, int(value))
            routing = entry.get("routing", {})
            if isinstance(routing, dict):
                for label, payload in routing.get("efforts", {}).items():
                    if label not in efforts[agent] or not isinstance(payload, dict):
                        continue
                    for key in efforts[agent][label]:
                        value = payload.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            efforts[agent][label][key] += max(0, value)
                for label, payload in routing.get("speeds", {}).items():
                    if label not in speeds[agent] or not isinstance(payload, dict):
                        continue
                    for key in speeds[agent][label]:
                        value = payload.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            speeds[agent][label][key] += max(0, value)
            quota = entry.get("quota", {})
            if isinstance(quota, dict):
                for raw_minutes, percent in quota.get("windows", {}).items():
                    try:
                        minutes = int(raw_minutes)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                        quota_windows[agent][minutes] = max(
                            float(percent), quota_windows[agent].get(minutes, 0.0)
                        )
                if quota.get("limitReached") is True:
                    quota_limit_days[agent].add(day)
    return AllocationTotals(
        as_of=as_of,
        recent_start=recent_start,
        agent_tokens=agent_tokens,
        model_tokens=dict(model_tokens),
        trend_starts=trend_starts,
        weekly_model_tokens=tuple(dict(window) for window in weekly_model_tokens),
        weekly_model_observed=weekly_model_observed,
        weekly_components=tuple(dict(window) for window in weekly_components),
        weekly_component_observed=weekly_component_observed,
        weekly_efforts=tuple(dict(window) for window in weekly_efforts),
        weekly_reasoning=tuple(dict(window) for window in weekly_reasoning),
        weekly_effort_observed=weekly_effort_observed,
        weekly_speeds=tuple(dict(window) for window in weekly_speeds),
        weekly_speed_observed=weekly_speed_observed,
        weekly_quota=tuple(dict(window) for window in weekly_quota),
        weekly_quota_observed=weekly_quota_observed,
        efforts=efforts,
        speeds=speeds,
        components=components,
        quota_windows=quota_windows,
        quota_limit_days={
            agent: len(days) for agent, days in quota_limit_days.items()
        },
    )


def _compact_number(value: int) -> str:
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            scaled = value / divisor
            digits = 0 if scaled >= 100 else 1
            rendered = f"{scaled:.{digits}f}"
            if digits:
                rendered = rendered.rstrip("0").rstrip(".")
            return rendered + suffix
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
        'hue is harness · intensity is harness share within each environment</text>',
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
                else f"agent-{agent} topology-level-{level}"
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
                    f'  <text class="topology-label-{level} '
                    f'topology-label-agent-{agent}-{level}" '
                    f'x="{x + cell_width / 2:.1f}" '
                    f'y="{y + 32}" text-anchor="middle" '
                    'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                    f'font-size="13" font-weight="600">{escape(value_text)}</text>',
                )
            )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def render_topology_history_svg(topology: TopologyTotals) -> str:
    """Render weekly absolute topology as environment-level stacked bars."""
    agents = tuple(
        agent
        for agent in TOPOLOGY_AGENT_ORDER
        if any(
            window.get((role, agent), 0) > 0
            for window in topology.weekly_topology
            for role in TOPOLOGY_ROLE_ORDER
        )
    )
    title = f"AI compute topology history through {topology.as_of.isoformat()}"
    height = 340
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        '  <desc id="desc">Twelve weekly absolute token stacks by harness within '
        'Work, Personal, and Development.</desc>',
        *_theme_style_lines(allocation=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Topology history</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{topology.window_starts[0].isoformat()}–{topology.as_of.isoformat()} · '
        '12 weekly absolute stacks · each environment uses its own scale</text>',
    ]
    legend_x = 650
    for index, agent in enumerate(agents):
        x = legend_x + index * 120
        lines.extend((
            f'  <circle class="agent-{agent}" cx="{x}" cy="88" r="5"/>',
            f'  <text class="dashboard-secondary" x="{x + 12}" y="92" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="11">{escape(AGENT_LABELS[agent])}</text>',
        ))

    panel_y, panel_w, panel_h, panel_gap = 106, 372, 210, 16
    plot_h, bar_w, bar_gap = 118, 20, 8
    for column, role in enumerate(TOPOLOGY_ROLE_ORDER):
        x = 16 + column * (panel_w + panel_gap)
        plot_x = x + 18
        plot_top = panel_y + 50
        baseline = plot_top + plot_h
        weekly_totals = [
            sum(window.get((role, agent), 0) for agent in agents)
            for window in topology.weekly_topology
        ]
        maximum = max(weekly_totals, default=0)
        lines.extend((
            f'  <rect class="dashboard-panel" x="{x}" y="{panel_y}" '
            f'width="{panel_w}" height="{panel_h}" rx="16"/>',
            f'  <text class="dashboard-primary" x="{x + 18}" y="{panel_y + 30}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="15" font-weight="600">{escape(ROLE_LABELS[role])}</text>',
            f'  <text class="dashboard-muted" x="{x + panel_w - 18}" y="{panel_y + 30}" '
            'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="11">latest {_compact_number(weekly_totals[-1])} · '
            f'peak {_compact_number(maximum)}</text>',
            f'  <line class="dashboard-border" x1="{plot_x}" y1="{baseline}" '
            f'x2="{plot_x + 328}" y2="{baseline}" stroke-width="1"/>',
        ))
        for week_index, window in enumerate(topology.weekly_topology):
            bx = plot_x + week_index * (bar_w + bar_gap)
            cursor = float(baseline)
            total = weekly_totals[week_index]
            for agent in agents:
                value = window.get((role, agent), 0)
                segment_h = plot_h * _share(value, maximum)
                if segment_h <= 0:
                    continue
                cursor -= segment_h
                lines.extend((
                    f'  <rect class="agent-{agent}" x="{bx}" y="{cursor:.1f}" '
                    f'width="{bar_w}" height="{segment_h:.1f}" data-role="{role}" '
                    f'data-agent="{agent}" data-week="{topology.window_starts[week_index]}">',
                    f'    <title>{escape(ROLE_LABELS[role])} · '
                    f'{topology.window_starts[week_index].isoformat()} · '
                    f'{escape(AGENT_LABELS[agent])}: {_compact_number(value)} of '
                    f'{_compact_number(total)}</title>',
                    '  </rect>',
                ))
        lines.extend((
            f'  <text class="dashboard-muted" x="{plot_x}" y="{panel_y + 193}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{topology.window_starts[0].strftime("%b %-d")}</text>',
            f'  <text class="dashboard-muted" x="{plot_x + 328}" y="{panel_y + 193}" '
            'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{topology.window_starts[-1].strftime("%b %-d")}</text>',
        ))
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


def _routing_signal_lines(
    allocation: AllocationTotals, agent: str
) -> tuple[tuple[str, str], ...]:
    effort_buckets = allocation.efforts[agent]
    effort_tokens = sum(item["totalTokens"] for item in effort_buckets.values())
    if agent == "claude":
        effort_text = "not exposed by Claude logs"
        reasoning_text = "not exposed separately"
    elif effort_tokens:
        effort_text = " · ".join(
            f"{EFFORT_SHORT_LABELS[name]} "
            f"{_percent(effort_buckets[name]['totalTokens'], effort_tokens)}"
            for name in EFFORT_ORDER
            if effort_buckets[name]["totalTokens"]
        )
        reasoning = sum(
            item["reasoningOutputTokens"] for item in effort_buckets.values()
        )
        reasoning_text = (
            f"{_compact_number(reasoning)} · {_percent(reasoning, effort_tokens)} of routed"
        )
    else:
        effort_text = "awaiting compatible session telemetry"
        reasoning_text = "awaiting compatible session telemetry"

    speed_buckets = allocation.speeds[agent]
    speed_tokens = sum(item["totalTokens"] for item in speed_buckets.values())
    speed_turns = sum(item["turns"] for item in speed_buckets.values())
    if speed_tokens:
        fast = speed_buckets["fast"]["totalTokens"]
        speed_text = f"{_percent(fast, speed_tokens)} fast · {speed_turns} turns"
    else:
        speed_text = "awaiting session telemetry"

    if agent == "claude":
        quota_text = "not exposed by Claude logs"
    elif allocation.quota_windows[agent]:
        minutes, percent = max(
            allocation.quota_windows[agent].items(),
            key=lambda item: (item[1], item[0]),
        )
        hits = allocation.quota_limit_days[agent]
        hit_text = f" · {hits} hit day(s)" if hits else ""
        quota_text = f"{percent:.0f}% peak · {_window_label(minutes)}{hit_text}"
    else:
        quota_text = "awaiting compatible session telemetry"

    return (
        ("Effort", effort_text),
        ("Reasoning", reasoning_text),
        ("Speed", speed_text),
        ("Quota", quota_text),
    )


def render_allocation_svg(allocation: AllocationTotals) -> str:
    """Render the current trailing-30-day per-harness model allocation."""
    total = sum(allocation.agent_tokens.values())
    title = f"Recent AI compute allocation through {allocation.as_of.isoformat()}"
    height = 350
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        f'  <desc id="desc">Harness and model allocation for {_compact_number(total)} '
        'tokens in the trailing 30 days.</desc>',
        *_theme_style_lines(allocation=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Model allocation</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{allocation.recent_start.isoformat()}–{allocation.as_of.isoformat()} · '
        'current 30-day model mix by harness</text>',
    ]

    panel_y, panel_h, panel_w, panel_gap = 92, 230, 372, 16
    for column, agent in enumerate(("claude", "codex", "traex")):
        x = 16 + column * (panel_w + panel_gap)
        agent_total = allocation.agent_tokens[agent]
        current_models = {
            model: tokens
            for (bucket, model), tokens in allocation.model_tokens.items()
            if bucket == agent and tokens > 0
        }
        current_model_total = sum(current_models.values())
        models = sorted(
            current_models,
            key=lambda model: (-current_models[model], model),
        )
        displayed = models[:4]
        lines.extend((
            f'  <rect class="dashboard-panel" x="{x}" y="{panel_y}" '
            f'width="{panel_w}" height="{panel_h}" rx="16"/>',
            f'  <circle class="agent-{agent}" cx="{x + 22}" cy="{panel_y + 27}" r="6"/>',
            f'  <text class="dashboard-primary" x="{x + 36}" y="{panel_y + 33}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="17" font-weight="600">{escape(AGENT_LABELS[agent])}</text>',
            f'  <text class="dashboard-muted" x="{x + panel_w - 18}" y="{panel_y + 33}" '
            'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="11">{_compact_number(agent_total)} · '
            f'{escape(_percent(agent_total, total))}</text>',
            f'  <text class="dashboard-muted" x="{x + 18}" y="{panel_y + 61}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="11">MODEL</text>',
            f'  <text class="dashboard-muted" x="{x + panel_w - 18}" y="{panel_y + 61}" '
            'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="10">30D SHARE</text>',
        ))
        if not displayed:
            lines.append(
                f'  <text class="dashboard-muted" x="{x + 18}" y="{panel_y + 91}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                'font-size="13">No model detail observed</text>'
            )
        else:
            for row, model in enumerate(displayed):
                y = panel_y + 86 + row * 36
                tokens = current_models[model]
                label = model if len(model) <= 27 else model[:26] + "…"
                share = _share(tokens, current_model_total)
                lines.extend((
                    f'  <text class="dashboard-secondary" x="{x + 18}" y="{y}" '
                    'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                    f'font-size="12">{escape(label)}</text>',
                    f'  <text class="dashboard-muted" x="{x + panel_w - 18}" y="{y}" '
                    'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                    f'font-size="11">{escape(_percent(tokens, current_model_total))}</text>',
                    f'  <rect class="dashboard-border" x="{x + 18}" y="{y + 8}" '
                    'width="336" height="8" rx="4" fill="none" stroke-width="1"/>',
                    f'  <rect class="agent-{agent}" x="{x + 18}" y="{y + 8}" '
                    f'width="{336 * share:.1f}" height="8" rx="4" fill-opacity="0.82" '
                    f'data-agent="{agent}" data-model="{escape(model)}" data-tokens="{tokens}">',
                    f'    <title>{escape(model)}: {_compact_number(tokens)} · '
                    f'{escape(_percent(tokens, current_model_total))}</title>',
                    '  </rect>',
                ))
            if len(models) > 4:
                lines.append(
                    f'  <text class="dashboard-muted" x="{x + 18}" y="{panel_y + 218}" '
                    'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                    f'font-size="11">+{len(models) - 4} more observed models</text>'
                )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def render_allocation_history_svg(allocation: AllocationTotals) -> str:
    """Render weekly absolute model stacks within each primary harness."""
    title = f"AI model allocation history through {allocation.as_of.isoformat()}"
    height = 370
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        '  <desc id="desc">Twelve weekly absolute model-token stacks within Claude, '
        'Codex, and TRAE, with missing model coverage left blank.</desc>',
        *_theme_style_lines(allocation=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Model allocation history</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{allocation.trend_starts[0].isoformat()}–{allocation.as_of.isoformat()} · '
        '12 weekly absolute stacks · Top 3 models + Other · blank = unavailable · per-harness scale</text>',
    ]

    panel_y, panel_w, panel_h, panel_gap = 92, 372, 250, 16
    plot_h, bar_w, bar_gap = 112, 20, 8
    for column, agent in enumerate(("claude", "codex", "traex")):
        x = 16 + column * (panel_w + panel_gap)
        totals_by_model: defaultdict[str, int] = defaultdict(int)
        all_models: set[str] = set()
        for window in allocation.weekly_model_tokens:
            for (bucket, model), tokens in window.items():
                if bucket != agent:
                    continue
                totals_by_model[model] += tokens
                all_models.add(model)
        top_models = sorted(
            all_models,
            key=lambda model: (-totals_by_model[model], model),
        )[:3]
        has_other = len(all_models) > len(top_models)
        series = [*top_models]
        if has_other:
            series.append("__other__")
        weekly_totals = [
            sum(
                tokens
                for (bucket, _model), tokens in window.items()
                if bucket == agent
            )
            for window in allocation.weekly_model_tokens
        ]
        maximum = max(weekly_totals, default=0)
        coverage = sum(
            agent in observed for observed in allocation.weekly_model_observed
        )
        lines.extend((
            f'  <rect class="dashboard-panel" x="{x}" y="{panel_y}" '
            f'width="{panel_w}" height="{panel_h}" rx="16"/>',
            f'  <circle class="agent-{agent}" cx="{x + 22}" cy="{panel_y + 25}" r="5"/>',
            f'  <text class="dashboard-primary" x="{x + 35}" y="{panel_y + 30}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="15" font-weight="600">{escape(AGENT_LABELS[agent])}</text>',
            f'  <text class="dashboard-muted" x="{x + panel_w - 18}" y="{panel_y + 30}" '
            'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="11">{coverage}/12 weeks observed</text>',
        ))
        for series_index, model in enumerate(series):
            legend_x = x + 18 + (series_index % 2) * 174
            legend_y = panel_y + 56 + (series_index // 2) * 20
            css_class = "series-other" if model == "__other__" else f"series-{series_index}"
            label = "Other" if model == "__other__" else model
            if len(label) > 20:
                label = label[:19] + "…"
            lines.extend((
                f'  <rect class="{css_class}" x="{legend_x}" y="{legend_y - 9}" '
                'width="9" height="9" rx="2"/>',
                f'  <text class="dashboard-secondary" x="{legend_x + 14}" y="{legend_y}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="10">{escape(label)}</text>',
            ))
        plot_x = x + 18
        plot_top = panel_y + 102
        baseline = plot_top + plot_h
        lines.append(
            f'  <line class="dashboard-border" x1="{plot_x}" y1="{baseline}" '
            f'x2="{plot_x + 328}" y2="{baseline}" stroke-width="1"/>'
        )
        for week_index, window in enumerate(allocation.weekly_model_tokens):
            if agent not in allocation.weekly_model_observed[week_index]:
                continue
            bx = plot_x + week_index * (bar_w + bar_gap)
            cursor = float(baseline)
            total = weekly_totals[week_index]
            for series_index, model in enumerate(series):
                if model == "__other__":
                    value = sum(
                        tokens
                        for (bucket, candidate), tokens in window.items()
                        if bucket == agent and candidate not in top_models
                    )
                    css_class = "series-other"
                    data_model = "other"
                    label = "Other"
                else:
                    value = window.get((agent, model), 0)
                    css_class = f"series-{series_index}"
                    data_model = model
                    label = model
                segment_h = plot_h * _share(value, maximum)
                if segment_h <= 0:
                    continue
                cursor -= segment_h
                lines.extend((
                    f'  <rect class="{css_class}" x="{bx}" y="{cursor:.1f}" '
                    f'width="{bar_w}" height="{segment_h:.1f}" data-agent="{agent}" '
                    f'data-model="{escape(data_model)}" '
                    f'data-week="{allocation.trend_starts[week_index]}">',
                    f'    <title>{escape(AGENT_LABELS[agent])} · '
                    f'{allocation.trend_starts[week_index].isoformat()} · '
                    f'{escape(label)}: {_compact_number(value)} of '
                    f'{_compact_number(total)}</title>',
                    '  </rect>',
                ))
        lines.extend((
            f'  <text class="dashboard-muted" x="{plot_x}" y="{panel_y + 235}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{allocation.trend_starts[0].strftime("%b %-d")}</text>',
            f'  <text class="dashboard-muted" x="{plot_x + 328}" y="{panel_y + 235}" '
            'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{allocation.trend_starts[-1].strftime("%b %-d")}</text>',
        ))
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _component_css_name(key: str) -> str:
    return {
        "inputTokens": "input",
        "outputTokens": "output",
        "cacheCreationTokens": "cache-write",
        "cacheReadTokens": "cache-read",
    }[key]


def render_efficiency_svg(allocation: AllocationTotals) -> str:
    """Render per-harness token flow and observable routing telemetry."""
    title = f"Recent AI token efficiency through {allocation.as_of.isoformat()}"
    height = 470
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        '  <desc id="desc">Input, output, cache, effort, reasoning, speed, and quota '
        'signals within each harness for the trailing 30 days.</desc>',
        *_theme_style_lines(allocation=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Token efficiency</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{allocation.recent_start.isoformat()}–{allocation.as_of.isoformat()} · '
        'input/output/cache flow and observable routing signals by harness</text>',
    ]

    panel_w, panel_gap = 372, 16
    flow_y = 92
    for column, agent in enumerate(("claude", "codex", "traex")):
        x = 16 + column * (panel_w + panel_gap)
        agent_total = allocation.agent_tokens[agent]
        component_values = allocation.components[agent]
        component_total = sum(component_values.values())
        lines.extend((
            f'  <rect class="dashboard-panel" x="{x}" y="{flow_y}" '
            f'width="{panel_w}" height="142" rx="16"/>',
            f'  <circle class="agent-{agent}" cx="{x + 22}" cy="{flow_y + 25}" r="5"/>',
            f'  <text class="dashboard-primary" x="{x + 35}" y="{flow_y + 30}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="14" font-weight="600">{escape(AGENT_LABELS[agent])} token flow</text>',
        ))
        if component_total:
            lines.append(
                f'  <text class="dashboard-muted" x="{x + panel_w - 18}" '
                f'y="{flow_y + 30}" text-anchor="end" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="11">{escape(_percent(component_total, agent_total))} coverage</text>'
            )
            bar_x, bar_y, bar_width, bar_height = x + 18, flow_y + 48, 336, 16
            lines.append(
                f'  <rect class="dashboard-border" x="{bar_x}" y="{bar_y}" '
                f'width="{bar_width}" height="{bar_height}" rx="5" fill="none" stroke-width="1"/>'
            )
            cursor = float(bar_x)
            for index, key in enumerate(COMPONENT_ORDER):
                value = component_values[key]
                width = bar_width * _share(value, component_total)
                if index == len(COMPONENT_ORDER) - 1:
                    width = bar_x + bar_width - cursor
                css_name = _component_css_name(key)
                lines.extend((
                    f'  <rect class="component-{css_name}" x="{cursor:.1f}" y="{bar_y}" '
                    f'width="{max(0.0, width):.1f}" height="{bar_height}" rx="5" '
                    f'data-agent="{agent}" data-component="{key}" data-tokens="{value}">',
                    f'    <title>{escape(AGENT_LABELS[agent])} {escape(COMPONENT_LABELS[key])}: '
                    f'{_compact_number(value)} ({escape(_percent(value, component_total))})</title>',
                    '  </rect>',
                ))
                cursor += width
            for index, key in enumerate(COMPONENT_ORDER):
                legend_x = x + 18 + (index % 2) * 168
                legend_y = flow_y + 91 + (index // 2) * 24
                css_name = _component_css_name(key)
                lines.extend((
                    f'  <circle class="component-{css_name}" cx="{legend_x + 5}" '
                    f'cy="{legend_y - 4}" r="4"/>',
                    f'  <text class="dashboard-secondary" x="{legend_x + 15}" y="{legend_y}" '
                    'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                    f'font-size="11">{escape(COMPONENT_LABELS[key])} '
                    f'{escape(_percent(component_values[key], component_total))}</text>',
                ))
        else:
            lines.extend((
                f'  <text class="dashboard-primary" x="{x + 18}" y="{flow_y + 72}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                'font-size="13">Awaiting component detail</text>',
                f'  <text class="dashboard-muted" x="{x + 18}" y="{flow_y + 97}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                'font-size="11">input · output · cache write · cache read</text>',
            ))

    routing_y = 278
    lines.append(
        f'  <text class="dashboard-primary" x="16" y="{routing_y - 14}" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="16" font-weight="600">Observed routing signals</text>'
    )
    for column, agent in enumerate(("claude", "codex", "traex")):
        x = 16 + column * (panel_w + panel_gap)
        lines.extend((
            f'  <rect class="dashboard-panel" x="{x}" y="{routing_y}" '
            f'width="{panel_w}" height="166" rx="16" data-agent="{agent}"/>',
            f'  <circle class="agent-{agent}" cx="{x + 22}" cy="{routing_y + 25}" r="5"/>',
            f'  <text class="dashboard-primary" x="{x + 35}" y="{routing_y + 30}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="14" font-weight="600">{escape(AGENT_LABELS[agent])}</text>',
        ))
        for row, (label, value) in enumerate(_routing_signal_lines(allocation, agent)):
            y = routing_y + 58 + row * 26
            lines.extend((
                f'  <text class="dashboard-muted" x="{x + 18}" y="{y}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="11">{escape(label)}</text>',
                f'  <text class="dashboard-secondary" x="{x + 92}" y="{y}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="11">{escape(value)}</text>',
            ))
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _signal_level(value: float) -> int:
    """Map an observed 0..1 signal to a visible four-level intensity."""
    return min(4, max(1, int(value * 4) + 1))


def render_efficiency_history_svg(allocation: AllocationTotals) -> str:
    """Render weekly token-flow composition and routing signal history."""
    title = f"AI token efficiency history through {allocation.as_of.isoformat()}"
    height = 430
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        '  <desc id="desc">Twelve weekly token-component compositions and observed '
        'effort, reasoning, Fast, and quota signals within each harness.</desc>',
        *_theme_style_lines(allocation=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Efficiency history</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{allocation.trend_starts[0].isoformat()}–{allocation.as_of.isoformat()} · '
        'weekly token-flow composition + routing intensity · darker = higher · gray = unavailable</text>',
    ]

    legend_x = 600
    for index, key in enumerate(COMPONENT_ORDER):
        x = legend_x + index * 138
        css_name = _component_css_name(key)
        lines.extend((
            f'  <rect class="component-{css_name}" x="{x}" y="80" '
            'width="9" height="9" rx="2"/>',
            f'  <text class="dashboard-secondary" x="{x + 14}" y="89" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{escape(COMPONENT_LABELS[key])}</text>',
        ))

    panel_y, panel_w, panel_h, panel_gap = 106, 372, 300, 16
    grid_offset, cell_w, cell_gap = 90, 18, 4
    effort_weights = {name: index for index, name in enumerate(EFFORT_ORDER)}
    for column, agent in enumerate(("claude", "codex", "traex")):
        x = 16 + column * (panel_w + panel_gap)
        grid_x = x + grid_offset
        lines.extend((
            f'  <rect class="dashboard-panel" x="{x}" y="{panel_y}" '
            f'width="{panel_w}" height="{panel_h}" rx="16"/>',
            f'  <circle class="agent-{agent}" cx="{x + 22}" cy="{panel_y + 25}" r="5"/>',
            f'  <text class="dashboard-primary" x="{x + 35}" y="{panel_y + 30}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="15" font-weight="600">{escape(AGENT_LABELS[agent])}</text>',
        ))

        flow_top, flow_h = panel_y + 50, 68
        lines.append(
            f'  <text class="dashboard-muted" x="{x + 18}" y="{flow_top + 38}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="11">Token flow</text>'
        )
        for week_index, window in enumerate(allocation.weekly_components):
            bx = grid_x + week_index * (cell_w + cell_gap)
            if agent not in allocation.weekly_component_observed[week_index]:
                lines.append(
                    f'  <rect class="heatmap-level-0" x="{bx}" y="{flow_top}" '
                    f'width="{cell_w}" height="{flow_h}" rx="3" data-agent="{agent}" '
                    f'data-week="{allocation.trend_starts[week_index]}" '
                    'data-signal="components"><title>component detail unavailable</title></rect>'
                )
                continue
            values = {key: window.get((agent, key), 0) for key in COMPONENT_ORDER}
            total = sum(values.values())
            cursor = float(flow_top + flow_h)
            for key in COMPONENT_ORDER:
                value = values[key]
                segment_h = flow_h * _share(value, total)
                if segment_h <= 0:
                    continue
                cursor -= segment_h
                css_name = _component_css_name(key)
                lines.extend((
                    f'  <rect class="component-{css_name}" x="{bx}" y="{cursor:.1f}" '
                    f'width="{cell_w}" height="{segment_h:.1f}" data-agent="{agent}" '
                    f'data-component="{key}" data-week="{allocation.trend_starts[week_index]}">',
                    f'    <title>{allocation.trend_starts[week_index].isoformat()} · '
                    f'{escape(COMPONENT_LABELS[key])}: {escape(_percent(value, total))} '
                    f'of {_compact_number(total)}</title>',
                    '  </rect>',
                ))
        lines.extend((
            f'  <text class="dashboard-muted" x="{grid_x}" y="{flow_top + 84}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="9">{allocation.trend_starts[0].strftime("%b %-d")}</text>',
            f'  <text class="dashboard-muted" x="{grid_x + 260}" y="{flow_top + 84}" '
            'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="9">{allocation.trend_starts[-1].strftime("%b %-d")}</text>',
        ))

        signal_rows = ("Effort", "Reasoning", "Fast", "Quota")
        signal_top = panel_y + 152
        for row, label in enumerate(signal_rows):
            y = signal_top + row * 28
            lines.append(
                f'  <text class="dashboard-muted" x="{x + 18}" y="{y + 14}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                f'font-size="10">{label}</text>'
            )
            for week_index in range(12):
                if label == "Effort":
                    observed = agent in allocation.weekly_effort_observed[week_index]
                    buckets = allocation.weekly_efforts[week_index]
                    denominator = sum(
                        buckets.get((agent, effort), 0) for effort in EFFORT_ORDER
                    )
                    weighted = sum(
                        effort_weights[effort] * buckets.get((agent, effort), 0)
                        for effort in EFFORT_ORDER
                    )
                    value = _share(weighted, denominator * 5)
                    detail = f"weighted effort {value * 5:.1f}/5"
                elif label == "Reasoning":
                    observed = agent in allocation.weekly_effort_observed[week_index]
                    buckets = allocation.weekly_efforts[week_index]
                    denominator = sum(
                        buckets.get((agent, effort), 0) for effort in EFFORT_ORDER
                    )
                    reasoning = allocation.weekly_reasoning[week_index].get(agent, 0)
                    value = _share(reasoning, denominator)
                    detail = f"reasoning {_percent(reasoning, denominator)} of routed"
                elif label == "Fast":
                    observed = agent in allocation.weekly_speed_observed[week_index]
                    buckets = allocation.weekly_speeds[week_index]
                    denominator = sum(
                        buckets.get((agent, speed), 0) for speed in SPEED_ORDER
                    )
                    fast = buckets.get((agent, "fast"), 0)
                    value = _share(fast, denominator)
                    detail = f"Fast {_percent(fast, denominator)}"
                else:
                    observed = agent in allocation.weekly_quota_observed[week_index]
                    percent = allocation.weekly_quota[week_index].get(agent, 0.0)
                    value = max(0.0, min(1.0, percent / 100))
                    detail = f"quota peak {percent:.0f}%"
                bx = grid_x + week_index * (cell_w + cell_gap)
                css_class = (
                    f"signal-level-{_signal_level(value)}"
                    if observed
                    else "heatmap-level-0"
                )
                lines.extend((
                    f'  <rect class="{css_class}" x="{bx}" y="{y}" '
                    f'width="{cell_w}" height="18" rx="3" data-agent="{agent}" '
                    f'data-signal="{label.lower()}" '
                    f'data-week="{allocation.trend_starts[week_index]}">',
                    f'    <title>{allocation.trend_starts[week_index].isoformat()} · '
                    f'{escape(detail) if observed else "unavailable"}</title>',
                    '  </rect>',
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


def generate_topology_history(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_topology_history_svg(aggregate_topology(root, as_of))
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


def generate_allocation_history(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_allocation_history_svg(aggregate_allocation(root, as_of))
    return _update_output(output, expected, check=check)


def generate_efficiency(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_efficiency_svg(aggregate_allocation(root, as_of))
    return _update_output(output, expected, check=check)


def generate_efficiency_history(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_efficiency_history_svg(aggregate_allocation(root, as_of))
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
        "--topology-history-output",
        type=Path,
        default=DEFAULT_TOPOLOGY_HISTORY_OUTPUT,
    )
    parser.add_argument(
        "--allocation-output",
        type=Path,
        default=DEFAULT_ALLOCATION_OUTPUT,
    )
    parser.add_argument(
        "--allocation-history-output",
        type=Path,
        default=DEFAULT_ALLOCATION_HISTORY_OUTPUT,
    )
    parser.add_argument(
        "--efficiency-output",
        type=Path,
        default=DEFAULT_EFFICIENCY_OUTPUT,
    )
    parser.add_argument(
        "--efficiency-history-output",
        type=Path,
        default=DEFAULT_EFFICIENCY_HISTORY_OUTPUT,
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
            args.topology_history_output,
            generate_topology_history(
                args.root,
                args.topology_history_output,
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
        (
            args.allocation_history_output,
            generate_allocation_history(
                args.root,
                args.allocation_history_output,
                args.as_of,
                check=args.check,
            ),
        ),
        (
            args.efficiency_output,
            generate_efficiency(
                args.root,
                args.efficiency_output,
                args.as_of,
                check=args.check,
            ),
        ),
        (
            args.efficiency_history_output,
            generate_efficiency_history(
                args.root,
                args.efficiency_history_output,
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
