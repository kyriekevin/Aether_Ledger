"""README narrative: context, execution choices, and observed Multica runs.

Collection stores remain independent. This module only computes a read model and
renders it; the canonical file selection and harness aliases come from the caller.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape
from pathlib import Path

from usage_schema import _token_value
from dashboard_theme import _theme_style_lines

CONTEXTS = ("work", "personal")
HARNESSES = ("codex", "claude", "traex", "dsh", "legacy")
LABELS = {"codex": "Codex", "claude": "Claude Code", "traex": "TRAEX", "dsh": "DSH", "legacy": "Legacy"}


@dataclass
class Context:
    weekly: list[int] = field(default_factory=lambda: [0] * 8)
    harnesses: Counter = field(default_factory=Counter)
    models: dict[str, Counter] = field(default_factory=lambda: {h: Counter() for h in HARNESSES})
    previous_models: dict[str, Counter] = field(default_factory=lambda: {h: Counter() for h in HARNESSES})
    cache_read: int = 0
    model_coverage: int = 0
    weekly_models: dict[str, list[int]] = field(default_factory=dict)
    latest: date | None = None

    @property
    def current(self) -> int:
        return sum(self.weekly[4:])

    @property
    def previous(self) -> int:
        return sum(self.weekly[:4])


@dataclass
class Story:
    as_of: date
    contexts: dict[str, Context]
    historical_context_tokens: int = 0


def aggregate_story(root: Path, paths: tuple[Path, ...], as_of: date, aliases: dict[str, str]) -> Story:
    """Keep 56 recorded days; compare adjacent 28-day windows, never infer tasks."""
    story = Story(as_of, {role: Context() for role in CONTEXTS})
    start = as_of - timedelta(days=55)
    recent = as_of - timedelta(days=27)
    for path in paths:
        role = path.relative_to(root / "data").parts[0]
        harness = aliases[path.stem]
        for raw_day, entry in json.loads(path.read_text()).items():
            day = date.fromisoformat(raw_day)
            if day > as_of:
                continue
            tokens = _token_value(entry.get("totalTokens", 0))
            if role not in CONTEXTS:
                if day >= recent:
                    story.historical_context_tokens += tokens
                continue
            context = story.contexts[role]
            if tokens:
                context.latest = max(context.latest or day, day)
            if day < start:
                continue
            context.weekly[(day - start).days // 7] += tokens
            weekly_covered = 0
            for name, model in entry.get("models", {}).items():
                amount = _token_value(model.get("totalTokens", 0))
                weekly_covered += amount
                context.weekly_models.setdefault(name, [0] * 8)[(day - start).days // 7] += amount
            context.weekly_models.setdefault("Unattributed", [0] * 8)[(day - start).days // 7] += max(0, tokens - weekly_covered)
            if day < recent:
                for name, model in entry.get("models", {}).items():
                    context.previous_models[harness][name] += _token_value(model.get("totalTokens", 0))
                context.previous_models[harness]["Unattributed"] += max(0, tokens - weekly_covered)
                continue
            context.harnesses[harness] += tokens
            models = entry.get("models", {})
            covered = 0
            cache = 0
            for name, model in models.items():
                amount = _token_value(model.get("totalTokens", 0))
                context.models[harness][name] += amount
                covered += amount
                cache += _token_value(model.get("cacheReadTokens", 0))
            # Coverage is measured against this entry, never borrowed from another day.
            context.model_coverage += min(tokens, covered)
            context.cache_read += min(tokens, cache)
            context.models[harness]["Unattributed"] += max(0, tokens - covered)
    return story


def number(value: int) -> str:
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= threshold:
            return f"{value / threshold:.1f}{suffix}"
    return str(value)


def percent(value: int, total: int) -> str:
    if not total:
        return "—"
    if 0 < value / total < .01:
        return "<1%"
    if value < total and value / total > .99:
        return ">99%"
    return f"{value / total:.0%}"


WORDS = {
    "en": {"cache": "cache", "matrix": "Harness × Model", "work": "Work", "personal": "Personal", "total": "Tokens", "trend": "8 weeks", "model": "Model", "effort": "Effort", "agents": "Agents", "issues": "Issues", "status": "Issue status", "dispatch": "Issue → Agent", "combos": "combinations", "unknown": "Unknown", "default": "Default", "Unattributed": "Unattributed", "backlog": "Backlog", "todo": "To do", "in_progress": "In progress", "done": "Done", "cancelled": "Cancelled", "empty": "No records", "unassigned": "Unassigned / unmapped", "archived": "Archived", "coverage": "Coverage", "history": "Legacy contexts", "not_collected": "not collected", "snapshot": "Configuration snapshot", "recent": "28 days", "scale": "Cell scale", "row_scale": "Trend: per-row scale"},
    "zh": {"cache": "缓存", "matrix": "Harness × Model", "work": "工作", "personal": "个人", "total": "Token", "trend": "8 周趋势", "model": "模型", "effort": "Effort", "agents": "Agent", "issues": "Issue", "status": "Issue 状态", "dispatch": "Issue → Agent", "combos": "种组合", "unknown": "未知", "default": "默认", "Unattributed": "未归因", "backlog": "待规划", "todo": "待办", "in_progress": "进行中", "done": "已完成", "cancelled": "已取消", "empty": "暂无记录", "unassigned": "未分派 / 未映射", "archived": "已归档", "coverage": "覆盖", "history": "历史环境", "not_collected": "未采集", "snapshot": "配置快照", "recent": "28 天", "scale": "单元格比例尺", "row_scale": "趋势按行缩放"},
}
STATE_ORDER = ("backlog", "todo", "in_progress", "done", "cancelled", "archived", "unknown")


# One visual vocabulary: harness hue, purpose opacity, and linear horizontal bars.
PURPOSE_OPACITY = {"work": 1, "personal": .35, "devbox": .15}
for locale, additions in {
    "en": {"matrix": "Model allocation & change", "previous": "Previous 28 days", "recent": "Latest 28 days", "change": "Change", "purpose": "Work: solid · Personal: light", "assigned": "Assigned", "scale": "Within each harness: one scale for both periods", "new": "New", "shown": "assigned combinations"},
    "zh": {"matrix": "模型分配与变化", "previous": "前 28 天", "recent": "近 28 天", "change": "变化", "purpose": "工作：实色 · 个人：浅色", "assigned": "已分派", "scale": "各 harness 组内，前后期共用比例尺", "new": "新增", "shown": "种已分派组合"},
}.items():
    WORDS[locale].update(additions)


class Canvas:
    def __init__(self, height, title):
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1180" height="{height}" viewBox="0 0 1180 {height}" role="img" aria-labelledby="title">',
                      f'<title id="title">{escape(title)}</title>',
                      *_theme_style_lines(allocation=True),
                      '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}</style>',
                      f'<rect width="1180" height="{height}" rx="22" class="dashboard-background"/>',
                      '<g transform="translate(40,0)">']

    def text(self, x, y, value, *, size=16, muted=False, anchor="start", bold=False, cls=None):
        color = cls or ("dashboard-muted" if muted else "dashboard-primary")
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" class="{color}" font-weight="{600 if bold else 400}">{escape(str(value))}</text>')

    def rect(self, x, y, w, h, *, cls="dashboard-panel", opacity=1, title=None):
        if w <= 0 or h <= 0:
            return
        self.parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" rx="2" class="{cls}" opacity="{opacity}">' + (f'<title>{escape(title)}</title>' if title else '') + '</rect>')

    def line(self, y):
        self.parts.append(f'<line x1="28" y1="{y}" x2="1072" y2="{y}" class="dashboard-border"/>')

    def group(self, y, harness, summary):
        self.line(y)
        self.rect(28, y + 15, 4, 18, cls=f"agent-{harness}")
        self.text(43, y + 30, LABELS[harness], size=18, bold=True, cls=f"agent-{harness}")
        self.text(1072, y + 30, summary, size=15, muted=True, anchor="end")

    def bar(self, x, y, width, maximum, harness, values, t, title):
        for role, value in values.items():
            segment = width * value / maximum if maximum else 0
            self.rect(x, y, segment, 8, cls=f"agent-{harness}", opacity=PURPOSE_OPACITY[role],
                      title=f'{title} · {t.get(role, role)}: {value:,}')
            x += segment

    def finish(self):
        return "\n".join(self.parts + ['</g>', '</svg>']) + "\n"


def model_groups(story: Story) -> list[tuple[str, list[str]]]:
    """Sparse observed combinations, ordered by current then previous use."""
    groups = []
    for h in HARNESSES:
        recent, previous = Counter(), Counter()
        for c in story.contexts.values():
            recent.update(c.models[h])
            previous.update(c.previous_models[h])
        names = sorted((name for name in recent.keys() | previous.keys() if recent[name] or previous[name]),
                       key=lambda name: (-recent[name], -previous[name], name))
        if names:
            groups.append((h, names))
    return groups


def render_model_matrix(story: Story, locale: str = "en") -> str:
    """Keep the existing asset/API name; render grouped period comparisons."""
    t = WORDS[locale]
    groups = model_groups(story)
    height = 180 + 48 * len(groups) + 42 * sum(len(names) for _, names in groups)
    svg = Canvas(height, t["matrix"])
    svg.text(28, 39, t["matrix"], size=24, bold=True)
    svg.text(1072, 37, str(story.as_of), anchor="end", muted=True)
    svg.text(28, 73, t["purpose"], muted=True)
    svg.text(1072, 73, f'{t["recent"]}: {number(sum(c.current for c in story.contexts.values()))} tokens', anchor="end", muted=True)
    svg.text(44, 112, t["model"], muted=True)
    svg.text(390, 112, t["previous"], muted=True)
    svg.text(705, 112, t["recent"], muted=True)
    svg.text(1072, 112, t["change"], muted=True, anchor="end")
    y = 126
    for h, names in groups:
        maximum = max(sum(getattr(c, attr)[h][name] for c in story.contexts.values())
                      for name in names for attr in ("previous_models", "models"))
        before = sum(sum(c.previous_models[h].values()) for c in story.contexts.values())
        after = sum(c.harnesses[h] for c in story.contexts.values())
        svg.group(y, h, f'{number(before)} → {number(after)} tokens')
        y += 48
        for name in names:
            label = t.get(name, name)
            svg.text(44, y + 19, label if len(label) <= 34 else label[:31] + "…", size=15)
            totals = []
            for x, attr in ((390, "previous_models"), (705, "models")):
                values = {role: getattr(c, attr)[h][name] for role, c in story.contexts.items()}
                total = sum(values.values())
                totals.append(total)
                svg.text(x + 240, y + 18, number(total) if total else "—", anchor="end", size=15, muted=not total)
                svg.bar(x, y + 25, 240, maximum, h, values, t, label)
            before, after = totals
            delta = f'{(after / before - 1) * 100:+.0f}%' if before else t["new"]
            svg.text(1072, y + 19, delta, anchor="end", size=14, muted=True)
            y += 42
    if not groups:
        svg.text(44, y + 19, t["empty"], muted=True)
    svg.text(28, height - 17, t["scale"], size=13, muted=True)
    return svg.finish()


def dispatch_groups(snapshot: dict | None) -> dict[str, dict[tuple[str, str], dict[str, int]]]:
    grouped = {}
    for row in snapshot["configurations"] if snapshot else []:
        count = sum(row["issues"].values())
        if not count:
            continue
        by_model = grouped.setdefault(row["harness"], {})
        values = by_model.setdefault((row["model"], row["effort"]), {})
        values[row["role"]] = values.get(row["role"], 0) + count
    return grouped


def render_dispatch(snapshot: dict | None, locale: str = "en") -> str:
    t = WORDS[locale]
    groups = dispatch_groups(snapshot)
    row_count = sum(len(rows) for rows in groups.values())
    totals = Counter()
    for row in snapshot["configurations"] if snapshot else []:
        totals.update(row["issues"])
    status_labels = [f'{t[state]} {n}' for state in STATE_ORDER if (n := totals[state])]
    unassigned = snapshot["unassignedIssues"] if snapshot else 0
    height = 190 + len(groups) * 48 + max(1, row_count) * 42 + (24 if unassigned else 0)
    svg = Canvas(height, t["dispatch"])
    svg.text(28, 39, t["dispatch"], size=24, bold=True)
    svg.text(1072, 37, f'{t["snapshot"]} · {snapshot["asOf"] if snapshot else "—"}', anchor="end", muted=True)
    assigned = sum(sum(values.values()) for rows in groups.values() for values in rows.values())
    svg.text(28, 73, f'{assigned} issues · {row_count} {t["shown"]}', size=18)
    svg.text(1072, 73, ' / '.join(t.get(r, r) for r in snapshot["coveredRoles"]) if snapshot else t["empty"], anchor="end", muted=True)
    svg.text(44, 112, t["model"], muted=True)
    svg.text(390, 112, t["effort"], muted=True)
    svg.text(1072, 112, t["issues"], anchor="end", muted=True)
    maximum = max((sum(values.values()) for rows in groups.values() for values in rows.values()), default=0)
    y = 126
    for h in HARNESSES:
        if h not in groups:
            continue
        rows = groups[h]
        svg.group(y, h, f'{sum(sum(v.values()) for v in rows.values())} issues')
        y += 48
        for (model, effort), values in sorted(rows.items(), key=lambda item: (-sum(item[1].values()), item[0])):
            svg.text(44, y + 19, t.get(model, model), size=15)
            svg.text(390, y + 19, t.get(effort, effort), size=15, muted=True)
            svg.bar(575, y + 13, 385, maximum, h, values, t, f'{model} · {effort}')
            svg.text(1072, y + 19, sum(values.values()), anchor="end", size=15, bold=True)
            y += 42
    if not groups:
        svg.text(44, y + 19, t["empty"], muted=True)
    if status_labels:
        svg.text(28, height - 24 - (24 if unassigned else 0), ' · '.join(status_labels), size=14, muted=True)
    if unassigned:
        svg.text(28, height - 24, f'{t["unassigned"]}: {unassigned}', size=14, muted=True)
    return svg.finish()
