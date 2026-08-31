#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Fail when tracked content contains identity or filesystem-path leaks."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_FILES = frozenset({"claude.json", "codex.json", "opencode.json", "traex.json"})
DURABLE_NODES = frozenset({"work", "personal", "devbox"})
OPAQUE_NODE = re.compile(r"node-[0-9a-f]{12}")
FORBIDDEN_TEXT = (
    re.compile("/" + r"Users/[^/\s]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"),
    re.compile(r"@bytedance\.com\b", re.IGNORECASE),
)
FORBIDDEN_JSON_KEYS = frozenset({"cwd", "source"})
STORE_ENTRY_KEYS = frozenset({
    "totalTokens", "totalCost", "costSource", "models", "imageCount",
    "routing", "quota",
})
MODEL_ENTRY_KEYS = frozenset({
    "totalTokens", "inputTokens", "outputTokens",
    "cacheCreationTokens", "cacheReadTokens",
})
ROUTING_DIMENSIONS = {
    "efforts": frozenset({"none", "low", "medium", "high", "xhigh", "max"}),
    "speeds": frozenset({"standard", "fast"}),
}
ROUTING_BUCKET_KEYS = frozenset(
    {"calls", "turns", "totalTokens", "reasoningCalls", "reasoningOutputTokens"}
)
QUOTA_KEYS = frozenset({"windows", "limitReached"})
MULTICA_DAY_KEYS = frozenset({"usage", "tasks"})
MULTICA_TASK_KEYS = frozenset({
    "total", "completed", "failed", "cancelled", "withUsage", "durationSeconds",
})


def tracked_files(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return tuple(root / item.decode() for item in result.stdout.split(b"\0") if item)


def _walk_json(value: object, path: Path, issues: list[str]) -> None:
    if isinstance(value, dict):
        leaked = FORBIDDEN_JSON_KEYS.intersection(value)
        for key in sorted(leaked):
            issues.append(f"{path}: forbidden JSON key {key!r}")
        for child in value.values():
            _walk_json(child, path, issues)
    elif isinstance(value, list):
        for child in value:
            _walk_json(child, path, issues)


def _validate_store_schema(value: object, path: Path, issues: list[str]) -> None:
    """Reject every canonical-store field that is not explicitly public-safe."""
    if not isinstance(value, dict):
        issues.append(f"{path}: usage store must be a date-keyed object")
        return
    for day, entry in value.items():
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError):
            issues.append(f"{path}: invalid date key {day!r}")
            continue
        if not isinstance(entry, dict):
            issues.append(f"{path}: {day} entry must be an object")
            continue
        unknown = set(entry).difference(STORE_ENTRY_KEYS)
        for key in sorted(unknown):
            issues.append(f"{path}: {day} field {key!r} is not in the public schema")
        for key in ("totalTokens", "imageCount"):
            metric = entry.get(key)
            if metric is not None and (
                not isinstance(metric, int) or isinstance(metric, bool) or metric < 0
            ):
                issues.append(f"{path}: {day} {key} must be a non-negative integer")
        cost = entry.get("totalCost")
        if cost is not None and (
            not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0
        ):
            issues.append(f"{path}: {day} totalCost must be a non-negative number")
        source = entry.get("costSource")
        if source is not None and source not in {"official", "unpriced"}:
            issues.append(f"{path}: {day} costSource must be 'official' or 'unpriced'")
        models = entry.get("models", {})
        if not isinstance(models, dict):
            issues.append(f"{path}: {day} models must be an object")
            models = {}
        for model, payload in models.items():
            if not isinstance(model, str) or not isinstance(payload, dict):
                issues.append(f"{path}: {day} has an invalid model entry")
                continue
            unknown_model = set(payload).difference(MODEL_ENTRY_KEYS)
            for key in sorted(unknown_model):
                issues.append(
                    f"{path}: {day} model {model!r} field {key!r} "
                    "is not in the public schema"
                )
            for key in MODEL_ENTRY_KEYS:
                tokens = payload.get(key)
                if tokens is not None and (
                    not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0
                ):
                    issues.append(
                        f"{path}: {day} model {model!r} {key} must be "
                        "a non-negative integer"
                    )
        routing = entry.get("routing")
        if routing is not None:
            if not isinstance(routing, dict):
                issues.append(f"{path}: {day} routing must be an object")
            else:
                for dimension, buckets in routing.items():
                    allowed = ROUTING_DIMENSIONS.get(dimension)
                    if allowed is None:
                        issues.append(
                            f"{path}: {day} routing dimension {dimension!r} is not public"
                        )
                        continue
                    if not isinstance(buckets, dict):
                        issues.append(
                            f"{path}: {day} routing {dimension} must be an object"
                        )
                        continue
                    for label, bucket in buckets.items():
                        if label not in allowed or not isinstance(bucket, dict):
                            issues.append(
                                f"{path}: {day} has invalid routing bucket "
                                f"{dimension}.{label}"
                            )
                            continue
                        for key in sorted(set(bucket).difference(ROUTING_BUCKET_KEYS)):
                            issues.append(
                                f"{path}: {day} routing {dimension}.{label} field "
                                f"{key!r} is not public"
                            )
                        for key in ROUTING_BUCKET_KEYS:
                            metric = bucket.get(key)
                            if metric is not None and (
                                not isinstance(metric, int)
                                or isinstance(metric, bool)
                                or metric < 0
                            ):
                                issues.append(
                                    f"{path}: {day} routing {dimension}.{label} "
                                    f"{key} must be a non-negative integer"
                                )
        quota = entry.get("quota")
        if quota is not None:
            if not isinstance(quota, dict):
                issues.append(f"{path}: {day} quota must be an object")
            else:
                for key in sorted(set(quota).difference(QUOTA_KEYS)):
                    issues.append(f"{path}: {day} quota field {key!r} is not public")
                windows = quota.get("windows", {})
                if not isinstance(windows, dict):
                    issues.append(f"{path}: {day} quota windows must be an object")
                else:
                    for minutes, percent in windows.items():
                        if not isinstance(minutes, str) or not minutes.isdigit():
                            issues.append(
                                f"{path}: {day} quota window {minutes!r} is invalid"
                            )
                        if (
                            not isinstance(percent, (int, float))
                            or isinstance(percent, bool)
                            or not 0 <= percent <= 100
                        ):
                            issues.append(
                                f"{path}: {day} quota window {minutes!r} must be 0..100"
                            )
                reached = quota.get("limitReached")
                if reached is not None and not isinstance(reached, bool):
                    issues.append(f"{path}: {day} quota limitReached must be boolean")


def _validate_multica_schema(value: object, path: Path, issues: list[str]) -> None:
    """Validate the ID-free Multica source aggregate."""
    if not isinstance(value, dict):
        issues.append(f"{path}: Multica aggregate must be a date-keyed object")
        return
    for day, entry in value.items():
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError):
            issues.append(f"{path}: invalid date key {day!r}")
            continue
        if not isinstance(entry, dict):
            issues.append(f"{path}: {day} entry must be an object")
            continue
        for key in sorted(set(entry).difference(MULTICA_DAY_KEYS)):
            issues.append(f"{path}: {day} field {key!r} is not in the public schema")
        for section in ("usage", "tasks"):
            roles = entry.get(section, {})
            if not isinstance(roles, dict):
                issues.append(f"{path}: {day} {section} must be an object")
                continue
            for role, agents in roles.items():
                if role not in DURABLE_NODES or not isinstance(agents, dict):
                    issues.append(f"{path}: {day} has invalid {section} role {role!r}")
                    continue
                for agent, payload in agents.items():
                    if agent not in {name.removesuffix(".json") for name in AGENT_FILES}:
                        issues.append(f"{path}: {day} has invalid {section} agent {agent!r}")
                        continue
                    if not isinstance(payload, dict):
                        issues.append(f"{path}: {day} {section}.{role}.{agent} must be an object")
                        continue
                    if section == "usage":
                        _validate_store_schema({day: payload}, path, issues)
                    else:
                        for key in sorted(set(payload).difference(MULTICA_TASK_KEYS)):
                            issues.append(
                                f"{path}: {day} task field {role}.{agent}.{key} is not public"
                            )
                        for key in MULTICA_TASK_KEYS:
                            metric = payload.get(key)
                            if metric is not None and (
                                not isinstance(metric, int)
                                or isinstance(metric, bool)
                                or metric < 0
                            ):
                                issues.append(
                                    f"{path}: {day} tasks.{role}.{agent}.{key} "
                                    "must be a non-negative integer"
                                )
                        total = payload.get("total")
                        outcomes = sum(
                            payload.get(key, 0)
                            for key in ("completed", "failed", "cancelled")
                            if isinstance(payload.get(key, 0), int)
                            and not isinstance(payload.get(key, 0), bool)
                        )
                        if isinstance(total, int) and not isinstance(total, bool):
                            if total != outcomes:
                                issues.append(
                                    f"{path}: {day} tasks.{role}.{agent}.total "
                                    "must equal terminal outcomes"
                                )
                            with_usage = payload.get("withUsage")
                            if (
                                isinstance(with_usage, int)
                                and not isinstance(with_usage, bool)
                                and with_usage > total
                            ):
                                issues.append(
                                    f"{path}: {day} tasks.{role}.{agent}.withUsage "
                                    "cannot exceed total"
                                )


def audit_tree(root: Path) -> list[str]:
    issues: list[str] = []
    for path in tracked_files(root):
        parsed: object = None
        parsed_ok = False
        # `git ls-files --cached` includes index entries staged for deletion.
        if not path.exists():
            continue
        relative = path.relative_to(root)
        if relative.name == "codex_by_repo.json":
            issues.append(f"{relative}: repository-level session export must not be tracked")
        if path.suffix == ".json":
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                issues.append(f"{relative}: unreadable JSON: {exc}")
            else:
                parsed_ok = True
                _walk_json(parsed, relative, issues)
        if path.name in AGENT_FILES:
            parts = relative.parts
            valid_node = (
                len(parts) == 3
                and parts[0] == "data"
                and parts[1] in DURABLE_NODES
            ) or (
                len(parts) == 4
                and parts[:2] == ("data", "trail")
                and (OPAQUE_NODE.fullmatch(parts[2]) is not None or parts[2] == "rollup")
            )
            if not valid_node:
                issues.append(f"{relative}: usage store is not under an approved node label")
            if parsed_ok:
                _validate_store_schema(parsed, relative, issues)
        elif relative == Path("data/multica.json") and parsed_ok:
            _validate_multica_schema(parsed, relative, issues)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in FORBIDDEN_TEXT:
            if pattern.search(text):
                issues.append(f"{relative}: matches forbidden identity/path pattern {pattern.pattern!r}")
    return issues


def audit_history(root: Path, ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "log", ref, "--format=%aE%n%cE"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    emails = {line.strip().lower() for line in result.stdout.splitlines() if line.strip()}
    return [
        f"{ref}: public history exposes commit email {email!r}"
        for email in sorted(emails)
        if not email.endswith("@users.noreply.github.com") and email != "noreply@github.com"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--history", metavar="REF", help="also audit commit emails on REF")
    args = parser.parse_args()
    issues = audit_tree(args.root)
    if args.history:
        issues.extend(audit_history(args.root, args.history))
    if issues:
        for issue in issues:
            print(issue)
        return 1
    print("public-data audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
