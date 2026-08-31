"""Collect privacy-safe Multica task and token aggregates.

The Multica API exposes identity-bearing runtime, issue, and task records.  This
module keeps those values in memory only and writes one date-keyed aggregate to
``data/multica.json``.  Collection is opt-in: every runtime custom name must be
mapped to one of the repository's public machine roles in a local config file.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from pricing import active_rate, load_pricing, standard_cost
from render_dashboard import SHANGHAI


CONFIG_FILE = Path.home() / ".config" / "token-activity" / "multica_runtime_roles.json"
STORE_FILE = Path(__file__).resolve().parents[1] / "data" / "multica.json"
PUBLIC_ROLES = frozenset({"work", "personal", "devbox"})
PROVIDER_AGENTS = {"claude": "claude", "codex": "codex", "traecli": "traex"}
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
COMMAND_TIMEOUT_SECONDS = 30
APP_MULTICA_BIN = Path(
    "/Applications/Multica.app/Contents/Resources/app.asar.unpacked/resources/bin/multica"
)
MULTICA_BIN = os.environ.get(
    "MULTICA_BIN", str(APP_MULTICA_BIN) if APP_MULTICA_BIN.exists() else "multica"
)


def _run_json(args: list[str]) -> object:
    result = subprocess.run(
        [MULTICA_BIN, *args, "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def load_runtime_roles(path: Path = CONFIG_FILE) -> dict[str, str]:
    """Load custom runtime name -> public role without accepting raw IDs."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path} must be a non-empty JSON object")
    roles: dict[str, str] = {}
    for custom_name, role in raw.items():
        if not isinstance(custom_name, str) or not custom_name.strip():
            raise ValueError(f"{path} has an invalid runtime custom name")
        if role not in PUBLIC_ROLES:
            raise ValueError(f"{path}: {custom_name!r} must map to {sorted(PUBLIC_ROLES)}")
        roles[custom_name] = role
    return roles


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(SHANGHAI) if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)


def _runtime_index(
    runtimes: object, roles: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """Return runtime ID -> (public role, canonical agent), in memory only."""
    index: dict[str, tuple[str, str]] = {}
    if not isinstance(runtimes, list):
        raise ValueError("multica runtime list returned a non-list payload")
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            continue
        role = roles.get(runtime.get("custom_name"))
        agent = PROVIDER_AGENTS.get(runtime.get("provider"))
        runtime_id = runtime.get("id")
        if role and agent and isinstance(runtime_id, str):
            index[runtime_id] = (role, agent)
    return index


def _usage_bucket(days: dict, raw_day: str, role: str, agent: str) -> dict:
    return (
        days.setdefault(raw_day, {})
        .setdefault("usage", {})
        .setdefault(role, {})
        .setdefault(
            agent,
            {
                "totalTokens": 0,
                "totalCost": 0.0,
                "costSource": "official",
                "models": {},
            },
        )
    )


def _collect_usage(
    days: dict,
    runtime_index: dict[str, tuple[str, str]],
    *,
    lookback_days: int,
    run_json: Callable[[list[str]], object],
) -> None:
    pricing = load_pricing()
    for runtime_id, (role, agent) in runtime_index.items():
        rows = run_json(["runtime", "usage", runtime_id, "--days", str(lookback_days)])
        if not isinstance(rows, list):
            raise ValueError("multica runtime usage returned a non-list payload")
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_day = row.get("date")
            model = row.get("model")
            if not isinstance(raw_day, str) or not isinstance(model, str):
                continue
            try:
                usage_day = date.fromisoformat(raw_day)
            except ValueError:
                continue
            breakdown = {
                "inputTokens": _nonnegative_int(row.get("input_tokens")),
                "outputTokens": _nonnegative_int(row.get("output_tokens")),
                "cacheCreationTokens": _nonnegative_int(row.get("cache_write_tokens")),
                "cacheReadTokens": _nonnegative_int(row.get("cache_read_tokens")),
            }
            total = sum(breakdown.values())
            if not total:
                continue
            bucket = _usage_bucket(days, raw_day, role, agent)
            model_bucket = bucket["models"].setdefault(
                model,
                {
                    "totalTokens": 0,
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheCreationTokens": 0,
                    "cacheReadTokens": 0,
                },
            )
            for key, value in breakdown.items():
                model_bucket[key] += value
            model_bucket["totalTokens"] += total
            bucket["totalTokens"] += total
            bucket["totalCost"] += standard_cost(model, usage_day, breakdown, pricing)
            if active_rate(model, usage_day, pricing) is None:
                bucket["costSource"] = "unpriced"


def _task_bucket(days: dict, raw_day: str, role: str, agent: str) -> dict:
    return (
        days.setdefault(raw_day, {})
        .setdefault("tasks", {})
        .setdefault(role, {})
        .setdefault(
            agent,
            {
                "total": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "withUsage": 0,
                "durationSeconds": 0,
            },
        )
    )


def _collect_tasks(
    days: dict,
    runtime_index: dict[str, tuple[str, str]],
    *,
    run_json: Callable[[list[str]], object],
) -> None:
    offset = 0
    while True:
        payload = run_json(
            ["issue", "list", "--limit", "100", "--offset", str(offset)]
        )
        if isinstance(payload, dict):
            issues = payload.get("issues")
            has_more = payload.get("has_more") is True
        else:
            # Retain compatibility with older CLI versions that returned the
            # page array directly.
            issues = payload
            has_more = isinstance(issues, list) and len(issues) == 100
        if not isinstance(issues, list):
            raise ValueError("multica issue list returned an invalid page payload")
        for issue in issues:
            if not isinstance(issue, dict) or not isinstance(issue.get("id"), str):
                continue
            runs = run_json(["issue", "runs", issue["id"]])
            if not isinstance(runs, list):
                raise ValueError("multica issue runs returned a non-list payload")
            for run in runs:
                if not isinstance(run, dict) or run.get("status") not in TERMINAL_STATUSES:
                    continue
                runtime = runtime_index.get(run.get("runtime_id"))
                started = _timestamp(run.get("started_at"))
                if runtime is None or started is None:
                    continue
                role, agent = runtime
                status = run["status"]
                bucket = _task_bucket(days, started.date().isoformat(), role, agent)
                bucket["total"] += 1
                bucket[status] += 1
                usage = run.get("usage")
                if isinstance(usage, list) and usage:
                    bucket["withUsage"] += 1
                completed = _timestamp(run.get("completed_at"))
                if completed is not None:
                    bucket["durationSeconds"] += max(0, int((completed - started).total_seconds()))
        if not has_more:
            break
        offset += 100


def collect_snapshot(
    roles: dict[str, str],
    *,
    lookback_days: int = 365,
    run_json: Callable[[list[str]], object] = _run_json,
) -> dict:
    """Fetch Multica and return only public daily aggregates."""
    if not 1 <= lookback_days <= 365:
        raise ValueError("lookback_days must be between 1 and 365")
    runtime_index = _runtime_index(run_json(["runtime", "list"]), roles)
    if not runtime_index:
        raise ValueError("no Multica runtimes matched the configured custom names")
    days: dict = {}
    _collect_usage(days, runtime_index, lookback_days=lookback_days, run_json=run_json)
    _collect_tasks(days, runtime_index, run_json=run_json)
    return {raw_day: days[raw_day] for raw_day in sorted(days)}


def write_snapshot(snapshot: dict, path: Path = STORE_FILE) -> None:
    """Atomically merge the fetched window into the cumulative aggregate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must be a date-keyed object")
        existing = loaded
    existing.update(snapshot)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def collect_if_configured(
    config_path: Path = CONFIG_FILE,
    store_path: Path = STORE_FILE,
) -> bool:
    """Collect only when the operator has explicitly mapped runtime names."""
    if not config_path.exists():
        return False
    write_snapshot(collect_snapshot(load_runtime_roles(config_path)), store_path)
    return True
