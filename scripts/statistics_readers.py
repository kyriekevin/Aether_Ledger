"""Read identity-bearing measurement facts in memory; never publish this output.

Each adapter emits observed usage records, not user turns. Missing configuration
is explicit. Session identities only serve deduplication and exact run linking.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import usage_schema as schema
import usage_sources

COMPONENTS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens")


def digest(*values) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def timestamp(raw) -> str | None:
    try:
        value = datetime.fromtimestamp(raw / 1000, schema.SHANGHAI) if isinstance(raw, (int, float)) else datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return None
        return value.astimezone(schema.SHANGHAI).isoformat()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def model_name(raw) -> str:
    return schema._normalise_model(raw.strip()) if isinstance(raw, str) and raw.strip() else "unknown"


def effort_name(raw) -> str:
    return raw if isinstance(raw, str) and raw in schema.EFFORT_LEVELS else "unknown"


@dataclass
class Reading:
    observations: dict[str, dict] = field(default_factory=dict)
    diagnostics: Counter = field(default_factory=Counter)
    present: bool = False
    failed: bool = False
    since: str | None = None
    quotas: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def status(self):
        if self.failed:
            return "failed"
        if not self.present:
            return "unavailable"
        return "partial" if any(self.diagnostics.values()) else "ok"

    def add(self, harness, session, identity, at, model, effort, speed, components,
            *, reported_total=None, rank=0, basis="response", global_identity=False):
        at = timestamp(at)
        if at is None:
            self.diagnostics["invalidTimestamp"] += 1
            return
        if self.since and at[:10] < self.since:
            return
        if any(type(v) is not int or v < 0 for v in components.values()):
            self.diagnostics["invalidTokens"] += 1
            return
        if set(components) != set(COMPONENTS):
            self.diagnostics["missingComponents"] += 1
            return
        total = sum(components.values())
        if reported_total is not None and reported_total != total:
            self.diagnostics["inconsistentTokens"] += 1
            return
        if not total:
            return
        key = digest(harness, identity) if global_identity else digest(harness, session, identity)
        value = dict(key=key, session=digest(harness, session), at=at, day=at[:10],
                     model=model_name(model), rawModel=model if isinstance(model, str) else None, effort=effort_name(effort),
                     speed=speed if speed in schema.SPEED_LEVELS else "unknown",
                     totalTokens=total, basis=basis, **components, rank=[rank if type(rank) in (int, float) else 0, total])
        previous = self.observations.get(key)
        if previous is None or value["rank"] >= previous["rank"]:
            self.observations[key] = value


def json_events(path: Path, reading: Reading):
    try:
        if reading.since and datetime.fromtimestamp(path.stat().st_mtime, schema.SHANGHAI).date().isoformat() < reading.since:
            return
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    reading.diagnostics["invalidJson"] += 1
                    continue
                if isinstance(event, dict):
                    yield event
    except (OSError, UnicodeError):
        reading.diagnostics["unreadable"] += 1


def codex(roots: Iterable[Path], harness="codex", since=None) -> Reading:
    roots = list(roots)
    reading = Reading(present=any(p.is_dir() for p in roots), since=since)

    def emit(session, identity, at, model, effort, speed, usage, rank=0):
        stamp = timestamp(at)
        if reading.since and stamp and stamp[:10] < reading.since:
            return
        if any(type(usage.get(k)) is not int for k in ("input_tokens", "output_tokens", "cached_input_tokens")) or type(usage.get("cache_write_input_tokens", 0)) is not int:
            reading.diagnostics["missingComponents"] += 1
            return
        write = usage.get("cache_write_input_tokens", 0)
        parts = dict(inputTokens=usage["input_tokens"] - usage["cached_input_tokens"] - write,
                     outputTokens=usage["output_tokens"], cacheReadTokens=usage["cached_input_tokens"], cacheCreationTokens=write)
        reading.add(harness, session, identity, at, model, effort, speed, parts,
                    reported_total=usage.get("total_tokens"), rank=rank,
                    basis="response" if identity[0] == "response" else "counter",
                    global_identity=identity[0] == "response")

    for _, path in usage_sources._codex_session_files(roots):
        session = path.stem
        session_known = False
        model = effort = speed = None
        legacy = []
        first_native = None
        for event in json_events(path, reading):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = event.get("type")
            if kind == "session_meta" and isinstance(payload.get("id"), str):
                session = payload["id"]
                session_known = True
            if kind == "turn_context":
                model = payload.get("model")
                effort = payload.get("effort") or payload.get("reasoning_effort")
                speed = schema._normalise_speed(payload.get("service_tier")) or speed
            if payload.get("type") == "thread_settings_applied":
                settings = payload.get("thread_settings", {})
                if isinstance(settings, dict):
                    model = settings.get("model", model)
                    effort = settings.get("reasoning_effort", effort)
                    if "service_tier" in settings:
                        speed = schema._normalise_speed(settings["service_tier"])
            if kind == "token_usage_record":
                usage = payload.get("usage")
                response = payload.get("response_id")
                at = timestamp(event.get("timestamp"))
                if not isinstance(usage, dict) or not isinstance(response, str) or not response:
                    reading.diagnostics["missingIdentity"] += 1
                    continue
                owner = payload.get("thread_id") or payload.get("session_id") or session
                if session_known and owner != session:
                    reading.diagnostics["foreignThread"] += 1
                    continue
                if at:
                    first_native = min(first_native or at, at)
                emit(owner, ["response", response], event.get("timestamp"), model, effort, speed, usage, event.get("ordinal", 0))
            if payload.get("type") != "token_count":
                continue
            limits = payload.get("rate_limits")
            at = timestamp(event.get("timestamp"))
            if isinstance(limits, dict) and at:
                for name in ("primary", "secondary"):
                    window = limits.get(name)
                    if not isinstance(window, dict):
                        continue
                    minutes, used = window.get("window_minutes"), window.get("used_percent")
                    if type(minutes) is int and minutes > 0 and type(used) in (int, float) and 0 <= used <= 100:
                        windows = reading.quotas.setdefault(at[:10], {})
                        windows[str(minutes)] = max(windows.get(str(minutes), 0), used)
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("last_token_usage"), dict):
                legacy.append((session, event.get("timestamp"), info.get("model") or model, effort, speed,
                               info["last_token_usage"], info.get("total_token_usage")))
        seen, previous_identity, previous_counter = set(), None, None
        for session, at, model, effort, speed, usage, cumulative in legacy:
            stamp = timestamp(at)
            # Native records are the authoritative per-response stream. Once it
            # starts, token_count is compatibility telemetry: its cumulative
            # scope may switch between thread/turn totals or report compaction
            # adjustments with no usage components. Never add it a second time.
            # A pre-upgrade legacy prefix is retained; a downgrade back to a
            # legacy-only writer must use a new source/version boundary.
            if first_native and stamp and stamp >= first_native:
                continue
            in_window = not reading.since or (stamp and stamp[:10] >= reading.since)
            identity = digest(cumulative) if isinstance(cumulative, dict) else digest(at, usage)
            counter = cumulative.get("total_tokens") if isinstance(cumulative, dict) else None
            if in_window and ((identity in seen and identity != previous_identity) or
                              (type(counter) is int and type(previous_counter) is int and counter < previous_counter)):
                reading.diagnostics["ambiguousCounter"] += 1
            previous_identity, previous_counter = identity, counter
            if identity in seen:
                continue
            seen.add(identity)
            emit(session, ["legacy", identity], at, model, effort, speed, usage)
    return reading


def claude(root: Path, since=None) -> Reading:
    reading = Reading(present=root.is_dir(), since=since)
    if not reading.present:
        return reading
    for path in sorted(root.rglob("*.jsonl")):
        for event in json_events(path, reading):
            if event.get("type") != "assistant":
                continue
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            at = timestamp(event.get("timestamp"))
            if reading.since and at and at[:10] < reading.since:
                continue
            u = message["usage"]
            identity = message.get("id") or event.get("uuid")
            if not isinstance(identity, str):
                reading.diagnostics["missingIdentity"] += 1
                continue
            if any(type(u.get(k)) is not int for k in ("input_tokens", "output_tokens")):
                reading.diagnostics["missingComponents"] += 1
                continue
            parts = dict(inputTokens=u["input_tokens"], outputTokens=u["output_tokens"],
                         cacheReadTokens=u.get("cache_read_input_tokens", 0),
                         cacheCreationTokens=u.get("cache_creation_input_tokens", 0))
            # A streamed message is one observation; choose its greatest complete
            # usage bundle. Model and effort move with that bundle, not by maxima.
            reading.add("claude", event.get("sessionId") or path.stem, identity, event.get("timestamp"),
                        message.get("model"), event.get("effort"), None, parts, global_identity=True)
    return reading


def dsh(roots: Iterable[Path], since=None) -> Reading:
    from usage_dsh import _dsh_session_logs, _read_dsh_session_log, _normalise_dsh_effort
    roots = list(roots)
    reading = Reading(present=any(p.is_dir() for p in roots), since=since)
    for path in _dsh_session_logs(roots):
        if reading.since and datetime.fromtimestamp(path.stat().st_mtime, schema.SHANGHAI).date().isoformat() < reading.since:
            continue
        text, complete = _read_dsh_session_log(path)
        if text is None:
            reading.diagnostics["unreadable"] += 1
            continue
        if not complete:
            reading.diagnostics["incompleteLog"] += 1
        session = str(path.parent)
        model = effort = None
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                reading.diagnostics["invalidJson"] += 1
                continue
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            if event.get("type") == "session":
                session = event.get("id") or session
            if not isinstance(data, dict):
                continue
            if event.get("type") == "request/header":
                header = data.get("header", {})
                config = header.get("config", {}) if isinstance(header, dict) else {}
                if isinstance(config, dict):
                    model, effort = config.get("model"), config.get("reasoningEffort")
            if event.get("type") == "request/context":
                model = data.get("model", model)
                effort = data.get("reasoningEffort", effort)
            if event.get("type") != "assistant/message" or not isinstance(data.get("usage"), dict):
                continue
            at = timestamp(event.get("time"))
            if reading.since and at and at[:10] < reading.since:
                continue
            u = data["usage"]
            if any(type(data.get(k)) is not int or data[k] < 0 for k in ("turn", "step")):
                reading.diagnostics["missingIdentity"] += 1
                continue
            parts = dict(inputTokens=u.get("inputTokens"), outputTokens=u.get("outputTokens"),
                         cacheReadTokens=u.get("cacheReadTokens", 0), cacheCreationTokens=u.get("cacheWriteTokens", 0))
            reading.add("dsh", session, [data["turn"], data["step"]], event.get("time"),
                        model, _normalise_dsh_effort(effort), None, parts, rank=event.get("seq", 0), basis="step")
    return reading
