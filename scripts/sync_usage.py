#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Collect local usage, merge cumulative stores, and publish the daily branch."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import multica_usage
import collect_statistics
import usage_ccusage
import usage_dsh
import usage_git
import usage_schema
import usage_sources
import usage_store
import usage_telemetry


READ_ERRORS = (OSError, ValueError, subprocess.SubprocessError, KeyError, TypeError)


def _read(name: str, fetch: Callable, failures: set[str], empty=None):
    """Isolate one source and report failure without logging private command data."""
    try:
        return fetch()
    except READ_ERRORS as error:
        failures.add(name)
        print(f"{name}: status=failed error={type(error).__name__}; keeping stored data",
              file=sys.stderr)
        return [] if empty is None else empty


def collect_usage(since: date) -> tuple[dict[str, list[dict]], set[str]]:
    """Read all sources once; each result retains its own cumulative store."""
    failures: set[str] = set()
    claude, codex, opencode = _read(
        "ccusage", lambda: usage_ccusage.fetch_daily_since(since), failures,
        empty=([], [], []),
    )
    multica_roots = _read(
        "codex-multica", usage_sources.multica_codex_session_roots, failures,
    )
    if "codex-multica" not in failures:
        summary = _read("codex-multica discovery", lambda: usage_sources.codex_source_summary(multica_roots),
                        failures, empty="status=failed")
        print(f"codex-multica discovery: {summary}")
    observations = {
        "claude": claude, "codex": codex, "opencode": opencode,
        "codex-multica": _read(
            "codex-multica",
            lambda: usage_ccusage.fetch_multica_codex_daily(since, multica_roots),
            failures,
        ) if "codex-multica" not in failures else [],
        "traex": _read(
            "traex", lambda: usage_ccusage.fetch_codex_home_daily(
                since, usage_ccusage.TRAEX_CODEX_HOME, lowercase_models=True,
            ), failures,
        ),
        "dsh": _read("dsh", lambda: usage_dsh.collect_dsh_daily_since(since), failures),
        "dsh-multica": _read(
            "dsh-multica", lambda: usage_dsh.collect_dsh_daily_since(
                since, usage_sources.multica_dsh_session_roots(),
            ), failures,
        ),
    }
    # Optional dimensions cannot discard a successfully read token observation.
    telemetry = {
        "claude": lambda: usage_telemetry.collect_claude_routing_since(
            since, usage_telemetry.CLAUDE_PROJECTS_DIR,
        ),
        "codex": lambda: usage_telemetry.collect_codex_routing_since(
            since, [usage_sources.CODEX_SESSION_DIR,
                    usage_sources.CODEX_SESSION_DIR.parent / "archived_sessions"],
        ),
        "codex-multica": lambda: usage_telemetry.collect_codex_routing_since(since, multica_roots),
        "traex": lambda: usage_telemetry.collect_codex_routing_since(
            since, usage_ccusage.TRAEX_CODEX_HOME / "sessions",
        ),
    }
    for name, fetch in telemetry.items():
        if observations[name]:
            details = _read(name + " telemetry", fetch, failures, empty={})
            usage_telemetry._attach_telemetry(observations[name], details)
    today = datetime.now(usage_schema.SHANGHAI).date().isoformat()
    for name, rows in observations.items():
        failed = name in failures or ("ccusage" in failures and name in {"claude", "codex", "opencode"})
        state = "failed" if failed else "ok" if rows else "empty"
        tokens = sum(row["totalTokens"] for row in rows if row["date"] == today)
        latest = max((row["date"] for row in rows), default="none")
        print(f"{name}: status={state} days={len(rows)} latest_day={latest} today_tokens={tokens}")
    return observations, failures


def _sync(
    machine: str, *, no_push: bool, reconcile_since: date | None = None,
    include_multica_tasks: bool = False,
    include_statistics: bool = False,
) -> int:
    now = datetime.now(usage_schema.SHANGHAI)
    if not no_push:
        starting_branch = usage_git._current_branch()
        recover = Path(machine).name in usage_git.ROLLOVER_WATCHDOG_NODES and (now.hour, now.minute) >= (0, 50)
        if not usage_git.prepare_daily_branch(now.date(), recover_missed_rollover=recover):
            return 0
        if usage_git._current_branch() != starting_branch:
            print("daily branch changed; deferring usage fetch until the next run", file=sys.stderr)
            return 0
        usage_git.git_catch_up(fetch=False)

    observations, failures = collect_usage(usage_schema.EPOCH)
    if reconcile_since is not None:
        if failures:
            print("incomplete collection; refusing to reconcile", file=sys.stderr)
            return 1
        if not any(row.get("tokensObserved", True) for rows in observations.values() for row in rows):
            print("nothing fetched; refusing to reconcile against an empty read", file=sys.stderr)
            return 1

    machine_dir = usage_schema.DATA_REPO_DIR / machine
    for name, filename in usage_schema.AGENT_STORES.items():
        usage_store.merge_with_cumulative(
            observations[name], machine_dir / filename, reconcile_since=reconcile_since,
        )
    if not no_push:
        usage_git.git_push(machine)

    # The new statistics journal is opt-in and independently published. A new
    # parser/API failure must not block the legacy token publication above.
    if include_statistics:
        try:
            statistics = collect_statistics.collect(machine_dir)
            if any(s["status"] in {"partial", "failed"} for s in statistics["sources"].values()):
                failures.add("statistics")
            if statistics["multica"] and (statistics["multica"]["status"] == "failed" or statistics["multica"]["assignmentStatus"] == "failed"):
                failures.add("statistics multica")
            if not no_push:
                usage_git.git_push(machine)
        except Exception as error:
            failures.add("statistics")
            print(f"statistics: status=failed error={type(error).__name__}", file=sys.stderr)

    # Workspace-wide task metadata is optional and runs AFTER publishing tokens.
    # Existing task history stays readable when this opt-in is not used.
    if include_multica_tasks and Path(machine).name == usage_schema.MULTICA_TASK_WRITER:
        try:
            changed = multica_usage.collect_if_configured(
                store_path=usage_schema.DATA_REPO_DIR / usage_schema.MULTICA_TASK_STORE,
            )
            if changed and not no_push:
                usage_git.git_push(machine)
        except Exception as error:
            failures.add("multica tasks")
            print(f"multica tasks: status=failed error={type(error).__name__}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-push", action="store_true",
                        help="update local data without switching branches, committing, or pushing")
    parser.add_argument("--reconcile-since", metavar="YYYY-MM-DD", type=date.fromisoformat,
                        help="accept lower counts from this date; manual use only")
    parser.add_argument("--include-statistics", action="store_true",
                        help="collect versioned statistics and per-machine Multica runs after tokens publish")
    parser.add_argument("--include-multica-tasks", action="store_true",
                        help="also refresh optional workspace task metadata after publishing tokens")
    args = parser.parse_args()
    print("sync run started", flush=True)
    machine = usage_git.resolve_machine()
    with usage_git.repo_git_lock(usage_git.GIT_LOCK_WAIT_SECONDS) as acquired:
        if not acquired:
            print("sync run finished status=lock-busy", flush=True)
            return 0
        status = _sync(machine, no_push=args.no_push, reconcile_since=args.reconcile_since,
                       include_multica_tasks=args.include_multica_tasks,
                       include_statistics=args.include_statistics)
    print(f"sync run finished exit={status}", flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
