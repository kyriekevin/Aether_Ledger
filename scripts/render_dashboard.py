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
DEFAULT_RUNTIME_PROFILE_OUTPUT = REPO_ROOT / "assets" / "runtime-profile.svg"
DEFAULT_RUNTIME_HISTORY_OUTPUT = REPO_ROOT / "assets" / "runtime-history.svg"
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
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
SPEED_ORDER = ("standard", "fast")
LEVEL_CLASSES = tuple(f"heatmap-level-{level}" for level in range(5))


def _theme_style_lines(
    *, topology: bool = False, allocation: bool = False, runtime: bool = False
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
    light_efforts = (
        "    .effort-none { fill: #9ca0b0; }",
        "    .effort-low { fill: #40a02b; }",
        "    .effort-medium { fill: #1e66f5; }",
        "    .effort-high { fill: #df8e1d; }",
        "    .effort-xhigh { fill: #8839ef; }",
        "    .effort-max { fill: #d20f39; }",
        "    .line-codex { fill: none; stroke: #1e66f5; }",
    ) if runtime else ()
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
    dark_efforts = (
        "      .effort-none { fill: #7f849c; }",
        "      .effort-low { fill: #a6e3a1; }",
        "      .effort-medium { fill: #89b4fa; }",
        "      .effort-high { fill: #f9e2af; }",
        "      .effort-xhigh { fill: #cba6f7; }",
        "      .effort-max { fill: #f38ba8; }",
        "      .line-codex { fill: none; stroke: #89b4fa; }",
    ) if runtime else ()
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
        *light_efforts,
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
        *dark_efforts,
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
HISTORY_WEEKS = 8
HISTORY_PERIOD_WEEKS = 4


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
    weekly_effort_calls: tuple[dict[tuple[str, str], int], ...]
    weekly_reasoning_calls: tuple[dict[str, int], ...]
    weekly_reasoning: tuple[dict[str, int], ...]
    weekly_effort_observed: tuple[set[str], ...]
    weekly_speed_calls: tuple[dict[tuple[str, str], int], ...]
    weekly_speed_observed: tuple[set[str], ...]
    weekly_quota_observed_days: tuple[dict[str, int], ...]
    weekly_quota_pressure_days: tuple[dict[str, int], ...]
    weekly_quota_7d_peak: tuple[dict[str, float], ...]
    weekly_quota_observed: tuple[set[str], ...]
    efforts: dict[str, dict[str, dict[str, int]]]
    speeds: dict[str, dict[str, dict[str, int]]]
    quota_windows: dict[str, dict[int, float]]
    latest_quota_day: dict[str, date | None]
    latest_quota_windows: dict[str, dict[int, float]]
    quota_observed_days: dict[str, int]
    quota_pressure_days: dict[str, int]
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
    trend_start = as_of - timedelta(days=HISTORY_WEEKS * 7 - 1)
    window_starts = tuple(
        trend_start + timedelta(days=index * 7) for index in range(HISTORY_WEEKS)
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


def _routing_calls(payload: dict) -> int:
    values = [
        value
        for key in ("calls", "turns")
        if isinstance((value := payload.get(key)), int)
        and not isinstance(value, bool)
    ]
    return max(0, *values)


def aggregate_allocation(root: Path, as_of: date) -> AllocationTotals:
    """Aggregate the trailing 30-day harness, model, and routing dimensions."""
    recent_start = as_of - timedelta(days=29)
    trend_start = as_of - timedelta(days=HISTORY_WEEKS * 7 - 1)
    trend_starts = tuple(
        trend_start + timedelta(days=index * 7) for index in range(HISTORY_WEEKS)
    )
    agent_tokens = {agent: 0 for agent in ALLOCATION_AGENT_ORDER}
    model_tokens: defaultdict[tuple[str, str], int] = defaultdict(int)
    weekly_model_tokens = tuple(defaultdict(int) for _ in trend_starts)
    weekly_model_observed = tuple(set() for _ in trend_starts)
    weekly_effort_calls = tuple(defaultdict(int) for _ in trend_starts)
    weekly_reasoning_calls = tuple(defaultdict(int) for _ in trend_starts)
    weekly_reasoning = tuple(defaultdict(int) for _ in trend_starts)
    weekly_effort_observed = tuple(set() for _ in trend_starts)
    weekly_speed_calls = tuple(defaultdict(int) for _ in trend_starts)
    weekly_speed_observed = tuple(set() for _ in trend_starts)
    weekly_quota_observed_dates = tuple(defaultdict(set) for _ in trend_starts)
    weekly_quota_pressure_dates = tuple(defaultdict(set) for _ in trend_starts)
    weekly_quota_7d_peak = tuple(defaultdict(float) for _ in trend_starts)
    weekly_quota_observed = tuple(set() for _ in trend_starts)
    efforts = {
        agent: {
            effort: {
                "calls": 0,
                "totalTokens": 0,
                "reasoningCalls": 0,
                "reasoningOutputTokens": 0,
            }
            for effort in EFFORT_ORDER
        }
        for agent in ALLOCATION_AGENT_ORDER
    }
    speeds = {
        agent: {
            speed: {"calls": 0, "totalTokens": 0}
            for speed in SPEED_ORDER
        }
        for agent in ALLOCATION_AGENT_ORDER
    }
    quota_windows: dict[str, dict[int, float]] = {
        agent: {} for agent in ALLOCATION_AGENT_ORDER
    }
    latest_quota_day: dict[str, date | None] = {
        agent: None for agent in ALLOCATION_AGENT_ORDER
    }
    latest_quota_windows: dict[str, dict[int, float]] = {
        agent: {} for agent in ALLOCATION_AGENT_ORDER
    }
    quota_limit_days: dict[str, set[date]] = {
        agent: set() for agent in ALLOCATION_AGENT_ORDER
    }
    quota_observed_days: dict[str, set[date]] = {
        agent: set() for agent in ALLOCATION_AGENT_ORDER
    }
    quota_pressure_days: dict[str, set[date]] = {
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
                    for model, payload in trend_models.items():
                        if not isinstance(model, str) or not isinstance(payload, dict):
                            continue
                        weekly_model_tokens[index][(agent, model)] += max(
                            0, int(payload.get("totalTokens", 0))
                        )
                trend_routing = entry.get("routing", {})
                if isinstance(trend_routing, dict):
                    trend_efforts = trend_routing.get("efforts", {})
                    if isinstance(trend_efforts, dict):
                        effort_observed = False
                        for label, payload in trend_efforts.items():
                            if label not in EFFORT_ORDER or not isinstance(payload, dict):
                                continue
                            effort_calls = _routing_calls(payload)
                            reasoning_calls = payload.get("reasoningCalls")
                            reasoning_tokens = payload.get("reasoningOutputTokens")
                            if effort_calls:
                                weekly_effort_calls[index][(agent, label)] += effort_calls
                                effort_observed = True
                            if isinstance(reasoning_calls, int) and not isinstance(
                                reasoning_calls, bool
                            ):
                                weekly_reasoning_calls[index][agent] += max(
                                    0, reasoning_calls
                                )
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
                            speed_calls = _routing_calls(payload)
                            if speed_calls:
                                weekly_speed_calls[index][(agent, label)] += speed_calls
                                speed_observed = True
                        if speed_observed:
                            weekly_speed_observed[index].add(agent)
                trend_quota = entry.get("quota", {})
                if isinstance(trend_quota, dict):
                    quota_observed = False
                    quota_pressured = False
                    for raw_minutes, percent in trend_quota.get("windows", {}).items():
                        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                            quota_observed = True
                            quota_pressured = quota_pressured or percent >= 80
                            try:
                                minutes = int(raw_minutes)
                            except (TypeError, ValueError):
                                continue
                            if minutes == 10080:
                                weekly_quota_7d_peak[index][agent] = max(
                                    float(percent), weekly_quota_7d_peak[index][agent]
                                )
                    if quota_observed:
                        weekly_quota_observed_dates[index][agent].add(day)
                        weekly_quota_observed[index].add(agent)
                    if quota_pressured:
                        weekly_quota_pressure_dates[index][agent].add(day)
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
            routing = entry.get("routing", {})
            if isinstance(routing, dict):
                for label, payload in routing.get("efforts", {}).items():
                    if label not in efforts[agent] or not isinstance(payload, dict):
                        continue
                    for key in efforts[agent][label]:
                        value = (
                            _routing_calls(payload)
                            if key == "calls"
                            else payload.get(key)
                        )
                        if isinstance(value, int) and not isinstance(value, bool):
                            efforts[agent][label][key] += max(0, value)
                for label, payload in routing.get("speeds", {}).items():
                    if label not in speeds[agent] or not isinstance(payload, dict):
                        continue
                    for key in speeds[agent][label]:
                        value = (
                            _routing_calls(payload)
                            if key == "calls"
                            else payload.get(key)
                        )
                        if isinstance(value, int) and not isinstance(value, bool):
                            speeds[agent][label][key] += max(0, value)
            quota = entry.get("quota", {})
            if isinstance(quota, dict):
                quota_observed = False
                quota_pressured = False
                for raw_minutes, percent in quota.get("windows", {}).items():
                    try:
                        minutes = int(raw_minutes)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                        quota_observed = True
                        quota_pressured = quota_pressured or percent >= 80
                        quota_windows[agent][minutes] = max(
                            float(percent), quota_windows[agent].get(minutes, 0.0)
                        )
                if quota_observed:
                    quota_observed_days[agent].add(day)
                    if latest_quota_day[agent] is None or day > latest_quota_day[agent]:
                        latest_quota_day[agent] = day
                        latest_quota_windows[agent] = {}
                    if day == latest_quota_day[agent]:
                        for raw_minutes, percent in quota.get("windows", {}).items():
                            try:
                                minutes = int(raw_minutes)
                            except (TypeError, ValueError):
                                continue
                            if isinstance(percent, (int, float)) and not isinstance(
                                percent, bool
                            ):
                                latest_quota_windows[agent][minutes] = max(
                                    float(percent),
                                    latest_quota_windows[agent].get(minutes, 0.0),
                                )
                if quota_pressured:
                    quota_pressure_days[agent].add(day)
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
        weekly_effort_calls=tuple(dict(window) for window in weekly_effort_calls),
        weekly_reasoning_calls=tuple(dict(window) for window in weekly_reasoning_calls),
        weekly_reasoning=tuple(dict(window) for window in weekly_reasoning),
        weekly_effort_observed=weekly_effort_observed,
        weekly_speed_calls=tuple(dict(window) for window in weekly_speed_calls),
        weekly_speed_observed=weekly_speed_observed,
        weekly_quota_observed_days=tuple(
            {agent: len(days) for agent, days in window.items()}
            for window in weekly_quota_observed_dates
        ),
        weekly_quota_pressure_days=tuple(
            {agent: len(days) for agent, days in window.items()}
            for window in weekly_quota_pressure_dates
        ),
        weekly_quota_7d_peak=tuple(
            dict(window) for window in weekly_quota_7d_peak
        ),
        weekly_quota_observed=weekly_quota_observed,
        efforts=efforts,
        speeds=speeds,
        quota_windows=quota_windows,
        latest_quota_day=latest_quota_day,
        latest_quota_windows=latest_quota_windows,
        quota_observed_days={
            agent: len(days) for agent, days in quota_observed_days.items()
        },
        quota_pressure_days={
            agent: len(days) for agent, days in quota_pressure_days.items()
        },
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
    height = 356
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        '  <desc id="desc">Eight weekly absolute token stacks by harness within '
        'Work, Personal, and Development, split into previous and latest four-week periods.</desc>',
        *_theme_style_lines(allocation=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Topology history</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{topology.window_starts[0].isoformat()}–{topology.as_of.isoformat()} · '
        '8 weekly stacks · previous 4 weeks vs latest 4 weeks · per-environment scale</text>',
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

    panel_y, panel_w, panel_h, panel_gap = 106, 372, 226, 16
    plot_h, bar_w, bar_gap = 112, 30, 11
    for column, role in enumerate(TOPOLOGY_ROLE_ORDER):
        x = 16 + column * (panel_w + panel_gap)
        plot_x = x + 18
        plot_top = panel_y + 72
        baseline = plot_top + plot_h
        divider_x = plot_x + HISTORY_PERIOD_WEEKS * (bar_w + bar_gap) - bar_gap / 2
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
            f'  <text class="dashboard-muted" x="{plot_x + 76}" y="{panel_y + 61}" '
            'text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="9">PREVIOUS 4 WEEKS</text>',
            f'  <text class="dashboard-muted" x="{plot_x + 246}" y="{panel_y + 61}" '
            'text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="9">LATEST 4 WEEKS</text>',
            f'  <line class="dashboard-border" x1="{divider_x:.1f}" y1="{plot_top - 5}" '
            f'x2="{divider_x:.1f}" y2="{baseline + 4}" stroke-width="1" stroke-dasharray="2 3"/>',
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
            f'  <text class="dashboard-muted" x="{plot_x}" y="{panel_y + 212}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{topology.window_starts[0].strftime("%b %-d")}</text>',
            f'  <text class="dashboard-muted" x="{plot_x + 328}" y="{panel_y + 212}" '
            'text-anchor="end" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{topology.window_starts[-1].strftime("%b %-d")}</text>',
        ))
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _routing_signal_lines(
    allocation: AllocationTotals, agent: str
) -> tuple[tuple[str, str], ...]:
    lines: list[tuple[str, str]] = []
    effort_buckets = allocation.efforts[agent]
    effort_calls = sum(item["calls"] for item in effort_buckets.values())
    if effort_calls:
        lines.append(("Effort", " · ".join(
            f"{EFFORT_SHORT_LABELS[name]} "
            f"{100 * effort_buckets[name]['calls'] / effort_calls:.0f}%"
            for name in EFFORT_ORDER
            if effort_buckets[name]["calls"]
        )))
        lines.append(("Effort calls", f"{_compact_number(effort_calls)} calls"))
        reasoning_calls = sum(
            item["reasoningCalls"] for item in effort_buckets.values()
        )
        reasoning = sum(
            item["reasoningOutputTokens"] for item in effort_buckets.values()
        )
        reasoning_label = "Thinking" if agent == "claude" else "Reasoning"
        if reasoning_calls:
            lines.append((
                reasoning_label,
                f"{_compact_number(round(reasoning / reasoning_calls))}/call",
            ))
        elif reasoning:
            lines.append((reasoning_label, f"{_compact_number(reasoning)} tokens"))

    speed_buckets = allocation.speeds[agent]
    speed_calls = sum(item["calls"] for item in speed_buckets.values())
    if agent != "claude" and speed_calls:
        fast_calls = speed_buckets["fast"]["calls"]
        lines.append((
            "Speed",
            f"{_percent(fast_calls, speed_calls)} fast · "
            f"{_compact_number(speed_calls)} calls",
        ))

    observed_days = allocation.quota_observed_days[agent]
    if observed_days:
        pressure_days = allocation.quota_pressure_days[agent]
        hits = allocation.quota_limit_days[agent]
        hit_label = "limit day" if hits == 1 else "limit days"
        lines.append((
            "Quota",
            f"{pressure_days}/{observed_days} days ≥80% · {hits} {hit_label}",
        ))

    return tuple(lines)


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
        '  <desc id="desc">Eight weekly absolute model-token stacks within Claude, '
        'Codex, and TRAE, split into previous and latest four-week periods, with missing '
        'model coverage left blank.</desc>',
        *_theme_style_lines(allocation=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Model allocation history</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{allocation.trend_starts[0].isoformat()}–{allocation.as_of.isoformat()} · '
        '8 weekly stacks · previous 4 weeks vs latest 4 weeks · Top 3 models + Other · blank = unavailable</text>',
    ]

    panel_y, panel_w, panel_h, panel_gap = 92, 372, 250, 16
    plot_h, bar_w, bar_gap = 112, 30, 11
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
            f'font-size="11">{coverage}/{HISTORY_WEEKS} weeks observed</text>',
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
        divider_x = plot_x + HISTORY_PERIOD_WEEKS * (bar_w + bar_gap) - bar_gap / 2
        lines.extend((
            f'  <text class="dashboard-muted" x="{plot_x + 76}" y="{plot_top - 8}" '
            'text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="9">PREVIOUS 4 WEEKS</text>',
            f'  <text class="dashboard-muted" x="{plot_x + 241}" y="{plot_top - 8}" '
            'text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="9">LATEST 4 WEEKS</text>',
            f'  <line class="dashboard-border" x1="{plot_x}" y1="{baseline}" '
            f'x2="{plot_x + 328}" y2="{baseline}" stroke-width="1"/>',
            f'  <line class="dashboard-border" x1="{divider_x:.1f}" y1="{plot_top - 5}" '
            f'x2="{divider_x:.1f}" y2="{baseline + 4}" stroke-width="1" stroke-dasharray="2 3"/>',
        ))
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


def _effort_mix_text(values: dict[str, int]) -> str:
    total = sum(values.values())
    if not total:
        return "unavailable"
    return " · ".join(
        f"{EFFORT_SHORT_LABELS[effort]} {_percent(values[effort], total)}"
        for effort in EFFORT_ORDER
        if values[effort]
    )


def _trajectory(
    values: list[float | None],
    left: float,
    right: float,
    top: float,
    height: float,
    domain: tuple[float, float] | None = None,
) -> tuple[str, list[tuple[float, float] | None], float, float]:
    observed = [value for value in values if value is not None]
    if domain is None:
        low = min(observed, default=0.0)
        high = max(observed, default=1.0)
        if high <= low:
            padding = max(1.0, abs(high) * 0.1)
            low -= padding
            high += padding
    else:
        low, high = domain
    step = (right - left) / max(1, len(values) - 1)
    points: list[tuple[float, float] | None] = []
    commands: list[str] = []
    continuing = False
    for index, value in enumerate(values):
        if value is None:
            points.append(None)
            continuing = False
            continue
        x = left + index * step
        y = top + height * (1 - (value - low) / (high - low))
        points.append((x, y))
        commands.append(f"{'L' if continuing else 'M'} {x:.1f} {y:.1f}")
        continuing = True
    return " ".join(commands), points, low, high


def render_runtime_profile_svg(allocation: AllocationTotals) -> str:
    """Render the current effort, Fast share, and latest seven-day quota."""
    title = f"Runtime profile through {allocation.as_of.isoformat()}"
    height = 420
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        '  <desc id="desc">Current 30-day effort mix, Codex Fast share, and latest '
        'observed seven-day quota percentages.</desc>',
        *_theme_style_lines(allocation=True, runtime=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Runtime profile</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{allocation.recent_start.isoformat()}–{allocation.as_of.isoformat()} · '
        'observed model-call routing</text>',
        '  <text class="dashboard-primary" x="34" y="104" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="14" font-weight="600">Effort mix</text>',
        '  <line class="dashboard-border" x1="34" y1="114" x2="1146" y2="114" '
        'stroke-width="1"/>',
    ]

    legend_x = 620
    for index, effort in enumerate(EFFORT_ORDER):
        x = legend_x + index * 88
        lines.extend((
            f'  <rect class="effort-{effort}" x="{x}" y="94" '
            'width="8" height="8" rx="2"/>',
            f'  <text class="dashboard-muted" x="{x + 12}" y="102" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="9">{escape(EFFORT_SHORT_LABELS[effort])}</text>',
        ))

    bar_x, bar_w = 170, 760
    for row, agent in enumerate(("claude", "codex", "traex")):
        y = 138 + row * 58
        buckets = allocation.efforts[agent]
        values = {effort: buckets[effort]["calls"] for effort in EFFORT_ORDER}
        total = sum(values.values())
        lines.extend((
            f'  <circle class="agent-{agent}" cx="42" cy="{y + 9}" r="4"/>',
            f'  <text class="dashboard-secondary" x="54" y="{y + 13}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="12">{escape(AGENT_LABELS[agent])}</text>',
        ))
        if not total:
            lines.append(
                f'  <text class="dashboard-muted" x="{bar_x}" y="{y + 13}" '
                'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
                'font-size="11">awaiting backfill</text>'
            )
            continue
        cursor = float(bar_x)
        for effort in EFFORT_ORDER:
            calls = values[effort]
            width = bar_w * _share(calls, total)
            if width < 0.1:
                continue
            lines.extend((
                f'  <rect class="effort-{effort}" x="{cursor:.1f}" y="{y}" '
                f'width="{width:.1f}" height="18" data-agent="{agent}" '
                f'data-effort="{effort}">',
                f'    <title>{escape(AGENT_LABELS[agent])} · {escape(effort)} '
                f'{escape(_percent(calls, total))} · {calls} calls</title>',
                '  </rect>',
            ))
            cursor += width
        lines.extend((
            f'  <text class="dashboard-muted" x="{bar_x}" y="{y + 38}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{escape(_effort_mix_text(values))}</text>',
            f'  <text class="dashboard-muted" x="1120" y="{y + 13}" text-anchor="end" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{_compact_number(total)} calls</text>',
        ))

    lines.append(
        '  <line class="dashboard-border" x1="34" y1="314" x2="1146" y2="314" '
        'stroke-width="1"/>'
    )
    codex_speeds = allocation.speeds["codex"]
    speed_total = sum(item["calls"] for item in codex_speeds.values())
    fast_calls = codex_speeds["fast"]["calls"]
    quota_agents = [
        agent
        for agent in ("claude", "codex")
        if 10080 in allocation.latest_quota_windows[agent]
    ]
    if speed_total:
        fast_share = _share(fast_calls, speed_total)
        lines.extend((
            '  <text class="dashboard-primary" x="34" y="340" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="14" font-weight="600">Fast</text>',
            '  <circle class="agent-codex" cx="42" cy="372" r="4"/>',
            '  <text class="dashboard-secondary" x="54" y="376" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="11">Codex</text>',
            '  <rect class="heatmap-level-0" x="110" y="363" width="300" height="14" rx="4"/>',
            f'  <rect class="agent-codex" x="110" y="363" width="{300 * fast_share:.1f}" '
            'height="14" rx="4"/>',
            f'  <text class="dashboard-primary" x="430" y="376" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="12" font-weight="600">{escape(_percent(fast_calls, speed_total))} fast</text>',
        ))
    if speed_total and quota_agents:
        lines.append(
            '  <line class="dashboard-border" x1="570" y1="330" x2="570" '
            'y2="400" stroke-width="1"/>'
        )
    if quota_agents:
        lines.append(
            '  <text class="dashboard-primary" x="600" y="340" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            'font-size="14" font-weight="600">Latest-day 7-day peak</text>'
        )
    for row, agent in enumerate(quota_agents):
        percent = allocation.latest_quota_windows[agent][10080]
        day = allocation.latest_quota_day[agent]
        suffix = f" · {day.strftime('%b %-d')}" if day is not None else ""
        y = 354 + row * 28
        lines.extend((
            f'  <circle class="agent-{agent}" cx="608" cy="{y + 7}" r="4"/>',
            f'  <text class="dashboard-secondary" x="620" y="{y + 11}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10">{escape(AGENT_LABELS[agent])}</text>',
            f'  <rect class="heatmap-level-0" x="700" y="{y}" width="290" height="14" rx="4"/>',
            f'  <rect class="agent-{agent}" x="700" y="{y}" '
            f'width="{2.9 * percent:.1f}" height="14" rx="4"/>',
            f'  <text class="dashboard-primary" x="1120" y="{y + 11}" text-anchor="end" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="10" font-weight="600">7d {percent:.0f}%{escape(suffix)}</text>',
        ))
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def render_runtime_history_svg(allocation: AllocationTotals) -> str:
    """Render weekly effort, Fast share, and seven-day quota history."""
    title = f"Runtime history through {allocation.as_of.isoformat()}"
    height = 520
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        '  <desc id="desc">Eight weekly effort distributions, Codex Fast trajectory, '
        'and seven-day quota peak bars.</desc>',
        *_theme_style_lines(allocation=True, runtime=True),
        f'  <rect class="dashboard-background" width="{WIDTH}" height="{height}" rx="22"/>',
        '  <text class="dashboard-primary" x="16" y="42" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="24" font-weight="600">Runtime history</text>',
        f'  <text class="dashboard-muted" x="16" y="66" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13">'
        f'{allocation.trend_starts[0].isoformat()}–{allocation.as_of.isoformat()} · '
        'previous four weeks vs latest four weeks</text>',
    ]
    plot_left, plot_right = 230.0, 1060.0
    step = (plot_right - plot_left) / (HISTORY_WEEKS - 1)
    divider_x = plot_left + step * 3.5
    lines.extend((
        f'  <text class="dashboard-muted" x="{plot_left + step * 1.5:.1f}" y="88" '
        'text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="9">PREVIOUS 4 WEEKS</text>',
        f'  <text class="dashboard-muted" x="{plot_left + step * 5.5:.1f}" y="88" '
        'text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="9">LATEST 4 WEEKS</text>',
        f'  <line class="dashboard-border" x1="{divider_x:.1f}" y1="78" '
        f'x2="{divider_x:.1f}" y2="490" stroke-width="1" stroke-dasharray="2 3"/>',
        '  <text class="dashboard-primary" x="34" y="112" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="14" font-weight="600">Effort mix</text>',
    ))
    legend_x = 620
    for index, effort in enumerate(EFFORT_ORDER):
        x = legend_x + index * 88
        lines.extend((
            f'  <rect class="effort-{effort}" x="{x}" y="102" '
            'width="8" height="8" rx="2"/>',
            f'  <text class="dashboard-muted" x="{x + 12}" y="110" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="9">{escape(EFFORT_SHORT_LABELS[effort])}</text>',
        ))

    effort_rows = {"claude": 128, "codex": 182, "traex": 236}
    cell_w, bar_h = 44, 28
    for agent, row_y in effort_rows.items():
        lines.extend((
            f'  <circle class="agent-{agent}" cx="42" cy="{row_y + 14}" r="4"/>',
            f'  <text class="dashboard-secondary" x="54" y="{row_y + 18}" '
            'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
            f'font-size="11">{escape(AGENT_LABELS[agent])}</text>',
        ))
        for week_index, weekly in enumerate(allocation.weekly_effort_calls):
            values = {effort: weekly.get((agent, effort), 0) for effort in EFFORT_ORDER}
            total = sum(values.values())
            if not total:
                continue
            x = plot_left + week_index * step - cell_w / 2
            cursor_y = float(row_y + bar_h)
            for effort in EFFORT_ORDER:
                calls = values[effort]
                segment_h = bar_h * _share(calls, total)
                if segment_h < 0.1:
                    continue
                cursor_y -= segment_h
                lines.extend((
                    f'  <rect class="effort-{effort}" x="{x:.1f}" y="{cursor_y:.1f}" '
                    f'width="{cell_w}" height="{segment_h:.1f}" data-agent="{agent}" '
                    f'data-effort="{effort}" data-week="{allocation.trend_starts[week_index]}">',
                    f'    <title>{allocation.trend_starts[week_index].isoformat()} · '
                    f'{escape(AGENT_LABELS[agent])} · {escape(effort)} '
                    f'{escape(_percent(calls, total))} · {calls} calls</title>',
                    '  </rect>',
                ))

    lines.extend((
        '  <line class="dashboard-border" x1="34" y1="292" x2="1146" y2="292" '
        'stroke-width="1"/>',
        '  <text class="dashboard-primary" x="34" y="320" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="14" font-weight="600">Fast share</text>',
    ))
    fast_values: list[float | None] = []
    for weekly in allocation.weekly_speed_calls:
        total = sum(weekly.get(("codex", speed), 0) for speed in SPEED_ORDER)
        fast_values.append(
            _share(weekly.get(("codex", "fast"), 0), total) if total else None
        )
    path, points, _, _ = _trajectory(
        fast_values, plot_left, plot_right, 334, 44, domain=(0.0, 1.0)
    )
    lines.extend((
        '  <circle class="agent-codex" cx="42" cy="356" r="4"/>',
        '  <text class="dashboard-secondary" x="54" y="360" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="11">Codex</text>',
        '  <text class="dashboard-muted" x="205" y="343" text-anchor="end" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="9">100%</text>',
        '  <text class="dashboard-muted" x="205" y="380" text-anchor="end" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="9">0%</text>',
        f'  <line class="dashboard-border" x1="{plot_left}" y1="378" '
        f'x2="{plot_right}" y2="378" stroke-width="1"/>',
    ))
    if path:
        lines.append(
            f'  <path class="line-codex" d="{path}" stroke-width="2.5" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )
    for week_index, point in enumerate(points):
        if point is None or fast_values[week_index] is None:
            continue
        x, y = point
        lines.extend((
            f'  <circle class="agent-codex" cx="{x:.1f}" cy="{y:.1f}" r="4">',
            f'    <title>{allocation.trend_starts[week_index].isoformat()} · '
            f'Fast {_percent(round(1000 * fast_values[week_index]), 1000)}</title>',
            '  </circle>',
        ))

    lines.extend((
        '  <line class="dashboard-border" x1="34" y1="402" x2="1146" y2="402" '
        'stroke-width="1"/>',
        '  <text class="dashboard-primary" x="34" y="430" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="14" font-weight="600">7-day quota peak</text>',
        '  <circle class="agent-claude" cx="42" cy="456" r="4"/>',
        '  <text class="dashboard-secondary" x="54" y="460" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="10">Claude</text>',
        '  <circle class="agent-codex" cx="118" cy="456" r="4"/>',
        '  <text class="dashboard-secondary" x="130" y="460" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="10">Codex</text>',
        '  <text class="dashboard-muted" x="205" y="440" text-anchor="end" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="9">100%</text>',
        '  <text class="dashboard-muted" x="205" y="480" text-anchor="end" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        'font-size="9">0%</text>',
        f'  <line class="dashboard-border" x1="{plot_left}" y1="478" '
        f'x2="{plot_right}" y2="478" stroke-width="1"/>',
    ))
    quota_bar_w = 18
    for week_index, weekly in enumerate(allocation.weekly_quota_7d_peak):
        center = plot_left + week_index * step
        for offset, agent in ((-12, "claude"), (12, "codex")):
            percent = weekly.get(agent)
            if percent is None:
                continue
            bar_height = 40 * max(0.0, min(100.0, percent)) / 100
            x = center + offset - quota_bar_w / 2
            y = 478 - bar_height
            lines.extend((
                f'  <rect class="agent-{agent}" x="{x:.1f}" y="{y:.1f}" '
                f'width="{quota_bar_w}" height="{bar_height:.1f}" '
                f'data-agent="{agent}" data-week="{allocation.trend_starts[week_index]}">',
                f'    <title>{allocation.trend_starts[week_index].isoformat()} · '
                f'{escape(AGENT_LABELS[agent])} · 7d peak {percent:.0f}%</title>',
                '  </rect>',
            ))
    lines.extend((
        f'  <text class="dashboard-muted" x="{plot_left}" y="505" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        f'font-size="9">{allocation.trend_starts[0].strftime("%b %-d")}</text>',
        f'  <text class="dashboard-muted" x="{plot_right}" y="505" text-anchor="end" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" '
        f'font-size="9">{allocation.trend_starts[-1].strftime("%b %-d")}</text>',
        '</svg>',
    ))
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


def generate_runtime_profile(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_runtime_profile_svg(aggregate_allocation(root, as_of))
    return _update_output(output, expected, check=check)


def generate_runtime_history(
    root: Path,
    output: Path,
    as_of: date | None = None,
    *,
    check: bool = False,
) -> bool:
    if as_of is None:
        as_of = _latest_activity_day(aggregate_daily(root))
    expected = render_runtime_history_svg(aggregate_allocation(root, as_of))
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
        "--runtime-profile-output",
        type=Path,
        default=DEFAULT_RUNTIME_PROFILE_OUTPUT,
    )
    parser.add_argument(
        "--runtime-history-output",
        type=Path,
        default=DEFAULT_RUNTIME_HISTORY_OUTPUT,
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
            args.runtime_profile_output,
            generate_runtime_profile(
                args.root,
                args.runtime_profile_output,
                args.as_of,
                check=args.check,
            ),
        ),
        (
            args.runtime_history_output,
            generate_runtime_history(
                args.root,
                args.runtime_history_output,
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
