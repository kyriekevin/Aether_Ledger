#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Refresh an anonymous snapshot of current agents and assigned issues.

No historical run is attributed using today's agent configuration. All IDs are
used for in-memory joins only; models must belong to the public pricing catalog.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import multica_usage
from usage_schema import EFFORT_LEVELS, SHANGHAI, _normalise_model

ROOT = Path(__file__).resolve().parents[1]
STATES = ("backlog", "todo", "in_progress", "done", "cancelled", "archived", "unknown")
EFFORTS = EFFORT_LEVELS | {"default", "unknown"}


def public_models(root: Path = ROOT) -> set[str]:
    return {"minimax-m3", "mimo-v2.5-pro", "glm-5.2", "kimi-k3", "qwen3.7-max"} | set(json.loads((root / "config" / "official-pricing.json").read_text())["models"]) | {"default", "unknown"}


def configuration(agent: dict, allowed: set[str]) -> tuple[str, str]:
    raw = agent.get("model") or ""
    base, _, query = raw.partition("?")
    if base.startswith("opencode-go/"):
        base = base.removeprefix("opencode-go/")
    base = base.removesuffix("[1m]")
    model = _normalise_model(base) if base else "default"
    model = model if model in allowed else "unknown"
    embedded = parse_qs(query).get("model_reasoning_effort", [])
    explicit = agent.get("thinking_level") or ""
    # Conflicting selections have no known effective value; never guess precedence.
    selected = {v for v in [explicit, *embedded] if v}
    effort = next(iter(selected)) if len(selected) == 1 else ("default" if not selected else "unknown")
    return model, effort if effort in EFFORTS else "unknown"


def issue_state(issue: dict) -> str:
    if issue.get("status") == "archived":
        return "archived"
    category = issue.get("status_category")
    if category in STATES:
        return category
    raw = issue.get("status")
    return raw if raw in STATES else "unknown"


def collect_dispatch(roles: dict[str, str], *, run_json=multica_usage._run_json,
                     allowed_models: set[str] | None = None, as_of: str | None = None) -> dict:
    allowed = public_models() if allowed_models is None else allowed_models
    runtimes = multica_usage._runtime_index(run_json(["runtime", "list"]), roles)
    if not runtimes:
        raise ValueError("no configured runtime coverage")
    agents = run_json(["agent", "list", "--include-archived"])
    if not isinstance(agents, list):
        raise ValueError("invalid agents response")
    groups: dict[tuple, Counter] = {}
    agent_groups = {}
    skipped_agents = 0
    for agent in agents:
        if not isinstance(agent, dict) or not isinstance(agent.get("id"), str):
            raise ValueError("invalid agent record")
        if agent["id"] in agent_groups:
            continue
        runtime = runtimes.get(agent.get("runtime_id"))
        if runtime is None:
            skipped_agents += 1
            continue
        model, effort = configuration(agent, allowed)
        key = (*runtime, model, effort)
        agent_groups[agent["id"]] = key
        group = groups.setdefault(key, Counter())
        group["archivedAgents" if agent.get("archived_at") else "agents"] += 1
    seen = set()
    unassigned = 0
    offset = 0
    while True:
        payload = run_json(["issue", "list", "--limit", "100", "--offset", str(offset)])
        if not isinstance(payload, dict) or not isinstance(payload.get("issues"), list):
            raise ValueError("invalid issue page")
        for issue in payload["issues"]:
            identifier = issue.get("id")
            if not isinstance(identifier, str):
                raise ValueError("invalid issue identity")
            if identifier in seen:
                continue
            seen.add(identifier)
            key = agent_groups.get(issue.get("assignee_id")) if issue.get("assignee_type") == "agent" else None
            if key is None:
                unassigned += 1
                continue
            groups[key][issue_state(issue)] += 1
        if not payload.get("has_more"):
            break
        if not payload["issues"]:
            raise ValueError("empty issue page with has_more")
        offset += 100
    return {
        "asOf": as_of or datetime.now(SHANGHAI).date().isoformat(),
        "coveredRoles": sorted({role for role, _ in runtimes.values()}),
        "unmappedAgents": skipped_agents,
        "unassignedIssues": unassigned,
        "totalIssues": len(seen),
        "configurations": [dict(zip(("role", "harness", "model", "effort"), key),
                                agents=values["agents"], archivedAgents=values["archivedAgents"],
                                issues={state: values[state] for state in STATES})
                           for key, values in sorted(groups.items())],
    }


def validate_snapshot(value: object, models: set[str]) -> None:
    """An exact allow-list: arbitrary API strings must never become public fields."""
    def count(v):
        return type(v) is int and v >= 0

    if not isinstance(value, dict) or set(value) != {"asOf", "coveredRoles", "unmappedAgents", "unassignedIssues", "totalIssues", "configurations"}:
        raise ValueError("invalid dispatch snapshot fields")
    datetime.strptime(value["asOf"], "%Y-%m-%d")
    roles = value["coveredRoles"]
    if not isinstance(roles, list) or not roles or any(r not in multica_usage.PUBLIC_ROLES for r in roles) or len(set(roles)) != len(roles):
        raise ValueError("invalid dispatch roles")
    if not all(count(value[k]) for k in ("unmappedAgents", "unassignedIssues", "totalIssues")) or not isinstance(value["configurations"], list):
        raise ValueError("invalid dispatch counts")
    total = value["unassignedIssues"]
    seen = set()
    for row in value["configurations"]:
        if not isinstance(row, dict) or set(row) != {"role", "harness", "model", "effort", "agents", "archivedAgents", "issues"}:
            raise ValueError("invalid configuration fields")
        if row["role"] not in roles or row["harness"] not in {"codex", "claude", "traex", "dsh"} or row["model"] not in models or row["effort"] not in EFFORTS:
            raise ValueError("non-public configuration")
        key = tuple(row[k] for k in ("role", "harness", "model", "effort"))
        if key in seen or not count(row["agents"]) or not count(row["archivedAgents"]):
            raise ValueError("duplicate configuration or invalid agent count")
        seen.add(key)
        if not isinstance(row["issues"], dict) or set(row["issues"]) != set(STATES) or not all(count(n) for n in row["issues"].values()):
            raise ValueError("invalid issue counts")
        total += sum(row["issues"].values())
    if total != value["totalIssues"]:
        raise ValueError("issue conservation failed")


def main() -> int:
    from install_launchd import load_multica_environment
    from render_dashboard import _atomic_write

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "multica-dispatch.json")
    args = parser.parse_args()
    for key, value in load_multica_environment(Path.home()).items():
        os.environ.setdefault(key, value)
    multica_usage.MULTICA_PROFILE = os.environ.get("MULTICA_PROFILE")
    snapshot = collect_dispatch(multica_usage.load_runtime_roles())
    validate_snapshot(snapshot, public_models())
    _atomic_write(args.output, json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    print(f"refreshed dispatch snapshot: {len(snapshot['configurations'])} combinations, {snapshot['totalIssues']} issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
