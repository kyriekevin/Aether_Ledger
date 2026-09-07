"""Read optional effort, speed, and quota observations from harness logs."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterable

import usage_schema
import usage_sources


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _add_routing_bucket(
    daily: dict[str, dict],
    day: date,
    dimension: str,
    label: str,
    tokens: int,
    *,
    reasoning_tokens: int = 0,
    reasoning_observed: bool = False,
) -> None:
    routing = daily.setdefault(day.isoformat(), {}).setdefault("routing", {})
    bucket = routing.setdefault(dimension, {}).setdefault(
        label, {"calls": 0, "totalTokens": 0}
    )
    bucket["calls"] += 1
    bucket["totalTokens"] += tokens
    if reasoning_observed:
        bucket["reasoningCalls"] = bucket.get("reasoningCalls", 0) + 1
    if reasoning_tokens:
        bucket["reasoningOutputTokens"] = (
            bucket.get("reasoningOutputTokens", 0) + reasoning_tokens
        )


def collect_codex_routing_since(
    since: date, sessions: Path | Iterable[Path] = usage_sources.CODEX_SESSION_DIR
) -> dict[str, dict]:
    """Aggregate privacy-safe Codex routing and quota telemetry from session trees.

    Session files expose model effort, service tier, per-call token usage, and
    rate-limit snapshots. Only enum buckets, counters, and window percentages
    leave this function; paths, prompts, turn IDs, and session IDs never do.

    Duplicate rollout identities across Multica's shared and task-private trees
    are processed once, matching the token reader.
    """
    daily: dict[str, dict] = {}
    sessions_dirs = [sessions] if isinstance(sessions, Path) else list(sessions)
    for _, path in usage_sources._codex_session_files(sessions_dirs):
        effort: str | None = None
        speed: str | None = None
        previous_total: dict | None = None
        try:
            stream = path.open(encoding="utf-8")
        except OSError:
            continue
        with stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # A live JSONL may end with a line that is still being written.
                    continue
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type")
                if event.get("type") == "turn_context":
                    raw_effort = payload.get("effort")
                    effort = raw_effort if raw_effort in usage_schema.EFFORT_LEVELS else None
                elif payload_type == "thread_settings_applied":
                    settings = payload.get("thread_settings")
                    if isinstance(settings, dict):
                        raw_effort = settings.get("reasoning_effort")
                        effort = raw_effort if raw_effort in usage_schema.EFFORT_LEVELS else effort
                        speed = usage_schema._normalise_speed(settings.get("service_tier")) or speed
                if payload_type != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                # Quota updates can repeat the previous request's usage. Count
                # that request once, while still observing its newer quota.
                cumulative = info.get("total_token_usage")
                repeated = isinstance(cumulative, dict) and cumulative == previous_total
                if isinstance(cumulative, dict):
                    previous_total = cumulative
                day = usage_schema._event_day(event.get("timestamp"))
                if day is None or day < since:
                    continue
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict):
                    continue
                tokens = usage_schema._token_value(usage.get("total_tokens"))
                if tokens <= 0:
                    continue
                raw_reasoning = usage.get("reasoning_output_tokens")
                reasoning_observed = (
                    isinstance(raw_reasoning, (int, float))
                    and not isinstance(raw_reasoning, bool)
                )
                reasoning = usage_schema._token_value(raw_reasoning)
                if effort is not None and not repeated:
                    _add_routing_bucket(
                        daily, day, "efforts", effort, tokens,
                        reasoning_tokens=reasoning,
                        reasoning_observed=reasoning_observed,
                    )
                if speed is not None and not repeated:
                    _add_routing_bucket(daily, day, "speeds", speed, tokens)

                rate_limits = payload.get("rate_limits")
                if not isinstance(rate_limits, dict):
                    continue
                quota = daily.setdefault(day.isoformat(), {}).setdefault(
                    "quota", {"windows": {}, "limitReached": False}
                )
                for name in ("primary", "secondary"):
                    window = rate_limits.get(name)
                    if not isinstance(window, dict):
                        continue
                    minutes = usage_schema._token_value(window.get("window_minutes"))
                    percent = window.get("used_percent")
                    if not minutes or isinstance(percent, bool) or not isinstance(
                        percent, (int, float)
                    ):
                        continue
                    key = str(minutes)
                    observed_percent = min(100.0, max(0.0, float(percent)))
                    quota["windows"][key] = max(
                        observed_percent, quota["windows"].get(key, 0.0)
                    )
                quota["limitReached"] = bool(
                    quota["limitReached"]
                    or rate_limits.get("rate_limit_reached_type")
                    or rate_limits.get("spend_control_reached")
                )
    return daily


def collect_claude_routing_since(
    since: date, projects_dir: Path = CLAUDE_PROJECTS_DIR
) -> dict[str, dict]:
    """Aggregate Claude effort and thinking tokens without retaining identity."""
    if not projects_dir.is_dir():
        return {}
    # Claude may append the same assistant message several times while streaming.
    # Keep only its largest observed usage. IDs are dedupe keys in memory only.
    messages: dict[str, tuple[date, str, int, int, bool]] = {}
    for path in projects_dir.rglob("*.jsonl"):
        try:
            stream = path.open(encoding="utf-8")
        except OSError:
            continue
        with stream:
            for line_number, line in enumerate(stream):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "assistant":
                    continue
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                raw_effort = event.get("effort")
                effort = raw_effort if raw_effort in usage_schema.EFFORT_LEVELS else None
                day = usage_schema._event_day(event.get("timestamp"))
                if effort is None or day is None or day < since:
                    continue
                tokens = sum(
                    usage_schema._token_value(usage.get(key))
                    for key in (
                        "input_tokens", "output_tokens",
                        "cache_creation_input_tokens", "cache_read_input_tokens",
                    )
                )
                if tokens <= 0:
                    continue
                output_details = usage.get("output_tokens_details")
                raw_reasoning = (
                    output_details.get("thinking_tokens")
                    if isinstance(output_details, dict)
                    else None
                )
                reasoning_observed = (
                    isinstance(raw_reasoning, (int, float))
                    and not isinstance(raw_reasoning, bool)
                )
                reasoning = usage_schema._token_value(raw_reasoning)
                raw_id = message.get("id") or event.get("uuid")
                dedupe = str(raw_id) if raw_id else f"{path}:{line_number}"
                previous = messages.get(dedupe)
                if previous is None or (tokens, reasoning) >= (previous[2], previous[3]):
                    messages[dedupe] = (
                        day, effort, tokens, reasoning, reasoning_observed
                    )
    daily: dict[str, dict] = {}
    for day, effort, tokens, reasoning, reasoning_observed in messages.values():
        _add_routing_bucket(
            daily, day, "efforts", effort, tokens,
            reasoning_tokens=reasoning,
            reasoning_observed=reasoning_observed,
        )
    return daily


def _attach_telemetry(entries: list[dict], telemetry: dict[str, dict]) -> None:
    """Attach aggregate telemetry, adding non-authoritative stubs when needed."""
    by_day = {entry["date"]: entry for entry in entries}
    for day, payload in telemetry.items():
        entry = by_day.get(day)
        if entry is None:
            entry = {
                "date": day, "totalTokens": 0, "totalCost": 0.0,
                "models": {}, "tokensObserved": False,
            }
            entries.append(entry)
            by_day[day] = entry
        entry.update(payload)
