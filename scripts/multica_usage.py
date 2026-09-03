"""Collect privacy-safe Multica task aggregates.

Multica is an orchestrator, not a harness: it dispatches work to Claude Code,
Codex, TRAE CLI and dsh, and those CLIs write the session logs the token stores
are built from. The tokens are therefore already counted — Multica's relocated
Codex and dsh trees arrive in ``codex-multica.json`` and ``dsh-multica.json``,
while Claude and TRAE write to their harnesses' default trees. Asking the
Multica API for token totals as well would count the same
work twice, from two measurements that do not even agree (for 2026-08-31 the API
reported 21.63M against 21.50M parsed from the local rollouts).

What only Multica knows is the shape of the work it dispatched: how many tasks
ran, whether they finished, and how long they took. That is what this module
collects, and it records no tokens or cost at all.

The API's runtime, issue and task records carry identity — real names, emails,
absolute working directories, raw prompts and raw agent output. None of it is
persisted. Every response is reduced in memory to per-day counters keyed by a
public machine role and a canonical agent name, and only that reduction reaches
``data/multica.json``.

Collection is opt-in: a local config file must map each runtime's custom name to
one of the repository's public roles, so a machine that has not been configured
collects nothing rather than guessing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from render_dashboard import SHANGHAI


CONFIG_FILE = Path.home() / ".config" / "token-activity" / "multica_runtime_roles.json"
STORE_FILE = Path(__file__).resolve().parents[1] / "data" / "multica.json"
PUBLIC_ROLES = frozenset({"work", "personal", "devbox"})

# Multica's `provider` values, mapped to this repository's agent names, read off
# `multica runtime list` on both servers rather than assumed. TRAE CLI is the one
# that differs: it reports `traecli` on one and `traex` on the other, so both map
# to the same agent. Getting this wrong is invisible in the data — an unmapped
# provider's runtimes are simply absent, which looks exactly like that provider
# having done no work, so _runtime_index reports whatever it could not map.
PROVIDER_AGENTS = {
    "claude": "claude",
    "codex": "codex",
    "dsh": "dsh",
    "traecli": "traex",
    "traex": "traex",
}

# A run only counts once it has stopped: a run still in flight has no duration
# and would be recounted with a different status on the next collection.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
COUNTER_FIELDS = ("total", "completed", "failed", "cancelled", "durationSeconds")

COMMAND_TIMEOUT_SECONDS = 30
ISSUE_PAGE_SIZE = 100
APP_MULTICA_BIN = Path(
    "/Applications/Multica.app/Contents/Resources/app.asar.unpacked/resources/bin/multica"
)
MULTICA_BIN = os.environ.get(
    "MULTICA_BIN", str(APP_MULTICA_BIN) if APP_MULTICA_BIN.exists() else "multica"
)

# The CLI keeps one config, daemon state and workspace per profile, and answers
# for the selected profile only. Left unset, that is whichever profile the CLI
# defaults to — which is not necessarily the one holding the work. `--profile` is
# a global flag and is rejected after the subcommand, so it goes in front.
# MULTICA_WORKSPACE_ID needs no handling here; the CLI reads it from the
# environment itself.
MULTICA_PROFILE = os.environ.get("MULTICA_PROFILE")


def _run_json(args: list[str]) -> object:
    prefix = ["--profile", MULTICA_PROFILE] if MULTICA_PROFILE else []
    result = subprocess.run(
        [MULTICA_BIN, *prefix, *args, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        # CalledProcessError's default string drops captured stderr, which left
        # scheduled failures as an exit code with no actionable explanation.
        # Name only the command family here: `issue runs` is followed by a
        # private issue ID, and that identifier must not reach the log.
        command = " ".join(args[:2])
        detail = " ".join((result.stderr or result.stdout).split())
        if not detail:
            detail = "no diagnostic output"
        raise RuntimeError(
            f"multica {command} failed with exit status {result.returncode}: "
            f"{detail[:1000]}"
        )
    return json.loads(result.stdout)


def load_runtime_roles(path: Path = CONFIG_FILE) -> dict[str, str]:
    """Load custom runtime name -> public role.

    The config names runtimes by their operator-chosen custom name rather than
    their API id, so the mapping file itself holds nothing that identifies the
    workspace, and an id appearing in an API response can never introduce a role
    that the operator did not write down.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path} must be a non-empty JSON object")
    roles: dict[str, str] = {}
    for custom_name, role in raw.items():
        if not isinstance(custom_name, str) or not custom_name.strip():
            raise ValueError(f"{path} has an invalid runtime custom name")
        if not isinstance(role, str) or role not in PUBLIC_ROLES:
            raise ValueError(f"{path}: {custom_name!r} must map to {sorted(PUBLIC_ROLES)}")
        roles[custom_name] = role
    return roles


def _hashable(value: object) -> object:
    """A value safe to use as a dict key.

    Every lookup here is keyed by something the API supplied, and JSON can put a
    list or a dict where a string was expected. `dict.get` raises TypeError on
    those rather than missing, and that exception escaping the collector would
    strand the token stores merged just before it. Anything unhashable is turned
    into a sentinel that simply will not match.
    """
    return value if isinstance(value, (str, int, float, bool, type(None))) else object()


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
    """Return runtime id -> (public role, agent), held in memory only.

    A runtime is skipped when the operator has not mapped its name or when its
    provider is one this repository does not know. The second case is reported:
    silently dropping a provider looks exactly like that provider having done no
    work, which is how a renamed provider string would go unnoticed.
    """
    index: dict[str, tuple[str, str]] = {}
    if not isinstance(runtimes, list):
        raise ValueError("multica runtime list returned a non-list payload")
    unknown_providers: set[str] = set()
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            continue
        runtime_id = runtime.get("id")
        role = roles.get(_hashable(runtime.get("custom_name")))
        provider = runtime.get("provider")
        agent = PROVIDER_AGENTS.get(_hashable(provider))
        if role and agent is None and isinstance(provider, str):
            unknown_providers.add(provider)
        if role and agent and isinstance(runtime_id, str):
            index[runtime_id] = (role, agent)
    if unknown_providers:
        print(
            "Multica providers not collected (unknown to this repository): "
            + ", ".join(sorted(unknown_providers)),
            file=sys.stderr,
        )
    return index


def _task_bucket(days: dict, raw_day: str, role: str, agent: str) -> dict:
    return (
        days.setdefault(raw_day, {})
        .setdefault("tasks", {})
        .setdefault(role, {})
        .setdefault(agent, {field: 0 for field in COUNTER_FIELDS})
    )


def collect_tasks(
    runtime_index: dict[str, tuple[str, str]],
    *,
    run_json: Callable[[list[str]], object] = _run_json,
) -> dict:
    """Reduce every terminal run to per-day, per-role, per-agent counters.

    Runs are dated by when they started, in Shanghai time, so a task belongs to
    the day the operator dispatched it rather than the day it happened to finish.
    """
    days: dict = {}
    # A run must be counted once even if it is handed back more than once: issue
    # pages can overlap while the workspace is being written to, and one run can
    # surface under more than one issue. Nothing downstream would notice — a
    # duplicate increments `total` and one outcome together, so the arithmetic
    # invariant still holds — and the merge keeps the largest total it ever saw,
    # which would make a transient double count permanent.
    counted_runs: set[str] = set()
    seen_issues: set[str] = set()
    offset = 0
    while True:
        payload = run_json(
            ["issue", "list", "--limit", str(ISSUE_PAGE_SIZE), "--offset", str(offset)]
        )
        if isinstance(payload, dict):
            issues = payload.get("issues")
            has_more = payload.get("has_more") is True
        else:
            # Older CLI versions returned the page array directly.
            issues = payload
            has_more = isinstance(issues, list) and len(issues) == ISSUE_PAGE_SIZE
        if not isinstance(issues, list):
            raise ValueError("multica issue list returned an invalid page payload")
        for issue in issues:
            if not isinstance(issue, dict) or not isinstance(issue.get("id"), str):
                continue
            if issue["id"] in seen_issues:
                continue
            seen_issues.add(issue["id"])
            runs = run_json(["issue", "runs", issue["id"]])
            if not isinstance(runs, list):
                raise ValueError("multica issue runs returned a non-list payload")
            for run in runs:
                if not isinstance(run, dict):
                    continue
                status = run.get("status")
                if status not in TERMINAL_STATUSES:
                    continue
                run_id = run.get("id")
                if isinstance(run_id, str):
                    if run_id in counted_runs:
                        continue
                    counted_runs.add(run_id)
                runtime = runtime_index.get(_hashable(run.get("runtime_id")))
                started = _timestamp(run.get("started_at"))
                if runtime is None or started is None:
                    continue
                role, agent = runtime
                bucket = _task_bucket(days, started.date().isoformat(), role, agent)
                bucket["total"] += 1
                bucket[status] += 1
                completed = _timestamp(run.get("completed_at"))
                if completed is not None:
                    bucket["durationSeconds"] += max(
                        0, int((completed - started).total_seconds())
                    )
        if not has_more:
            break
        offset += ISSUE_PAGE_SIZE
    return days


def collect_snapshot(
    roles: dict[str, str],
    *,
    run_json: Callable[[list[str]], object] = _run_json,
) -> dict:
    """Fetch Multica and return only the public daily aggregate."""
    runtime_index = _runtime_index(run_json(["runtime", "list"]), roles)
    if not runtime_index:
        raise ValueError("no Multica runtimes matched the configured custom names")
    days = collect_tasks(runtime_index, run_json=run_json)
    if not days:
        # Runtimes exist but nothing was counted. Usually that just means no task
        # has finished yet, but it is also what querying the wrong workspace looks
        # like: the CLI answers for one profile at a time, and a profile with no
        # issues returns an empty list rather than an error. Say so, so that a
        # misdirected collector does not read as an idle one.
        print(
            "Multica returned no terminal runs for the configured runtimes; "
            "if that is unexpected, check the profile and workspace the CLI is "
            "pointed at (MULTICA_PROFILE, MULTICA_WORKSPACE_ID)",
            file=sys.stderr,
        )
    return {raw_day: days[raw_day] for raw_day in sorted(days)}


def merge_snapshot(existing: dict, snapshot: dict) -> dict:
    """Merge a fetch into the stored aggregate, keeping the fuller observation.

    A finished run's day, status and duration never change again, so a past day's
    counters only grow as more runs land on it — a smaller number means the fetch
    saw less than the store already knows, not that work was undone. That is what
    a workspace pruning old issues looks like, and a plain overwrite would quietly
    erase those days.

    The comparison is whole-bundle, not per counter. Maxing each counter
    separately lets `completed` come from one fetch and `failed` from another, and
    those two never described the same set of runs: prune two completed runs, land
    two failed ones, and the per-counter maxima say two total against four
    outcomes — a day that never happened, and one the public-data audit rejects.
    Choosing the observation with the larger total keeps the bundle internally
    consistent by construction.

    So a day whose runs were pruned and then replaced records the larger single
    observation rather than the sum. That undercounts, which is the honest
    failure: the two fetches overlap by an unknown amount, and adding them would
    invent runs instead of missing them.
    """
    merged = {day: json.loads(json.dumps(entry)) for day, entry in existing.items()}
    for day, entry in snapshot.items():
        target = merged.setdefault(day, {}).setdefault("tasks", {})
        for role, agents in entry.get("tasks", {}).items():
            for agent, counters in agents.items():
                stored = target.setdefault(role, {}).get(agent)
                if stored is None or _observation_rank(counters) > _observation_rank(stored):
                    target[role][agent] = {
                        field: counters.get(field, 0) for field in COUNTER_FIELDS
                    }
    return merged


def _observation_rank(counters: dict) -> tuple[int, int]:
    """How much an observation knows: more runs first, then more duration.

    Duration is the tie-breaker because a terminal run can be reported before its
    `completed_at` is set, which counts the run with a duration of zero. The next
    fetch sees the same run with its finish time, so the totals tie and only the
    duration improves — ranking on total alone would keep the zero forever.
    Comparing whole observations, never counter by counter, is what stops
    `completed` and `failed` being taken from fetches that saw different runs.
    """
    return (counters.get("total", 0), counters.get("durationSeconds", 0))


def write_snapshot(snapshot: dict, path: Path = STORE_FILE) -> None:
    """Merge the fetch into the cumulative aggregate and replace the file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must be a date-keyed object")
        existing = loaded
    merged = merge_snapshot(existing, snapshot)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
