#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Check the installed Aether Ledger writer without changing local state."""

from __future__ import annotations

import json
import plistlib
import shutil
import sys
from pathlib import Path

from install_launchd import LABEL, load_multica_environment
from multica_usage import load_runtime_roles

REQUIRED_BINARIES = ("uv", "ccusage", "zstd")
MULTICA_ENVIRONMENT = frozenset({
    "MULTICA_PROFILE", "MULTICA_WORKSPACE_ID", "MULTICA_DSH_PROFILE",
})


def installation_issues(home: Path = Path.home()) -> list[str]:
    issues = []
    for binary in REQUIRED_BINARIES:
        if shutil.which(binary) is None:
            issues.append(f"required command is missing: {binary}")

    node_name = home / ".config" / "token-activity" / "node_name"
    try:
        role = node_name.read_text(encoding="utf-8").strip()
    except OSError:
        role = ""
    if role not in {"work", "personal", "devbox"}:
        issues.append("node_name is missing or invalid")

    try:
        expected_environment = load_multica_environment(home)
    except ValueError:
        expected_environment = {}
        issues.append("multica.json is invalid")

    roles_path = home / ".config" / "token-activity" / "multica_runtime_roles.json"
    if expected_environment and not roles_path.exists():
        issues.append("multica_runtime_roles.json is missing")
    elif roles_path.exists():
        try:
            load_runtime_roles(roles_path)
        except (OSError, json.JSONDecodeError, ValueError):
            issues.append("multica_runtime_roles.json is invalid")

    expected_dsh_profile = expected_environment.get("MULTICA_DSH_PROFILE")
    binding_path = home / ".config" / "token-activity" / "multica_dsh_profile"
    if expected_dsh_profile is not None and binding_path.exists():
        try:
            bound_dsh_profile = binding_path.read_text(encoding="utf-8").strip()
        except OSError:
            bound_dsh_profile = ""
        if bound_dsh_profile != expected_dsh_profile:
            issues.append("Multica DSH source binding differs from multica.json")

    plist_path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    try:
        with plist_path.open("rb") as stream:
            plist = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException):
        issues.append("launchd agent is missing or invalid")
        return issues

    environment = plist.get("EnvironmentVariables", {})
    for variable in MULTICA_ENVIRONMENT:
        if environment.get(variable) != expected_environment.get(variable):
            issues.append(f"launchd environment differs from multica.json: {variable}")
    arguments = plist.get("ProgramArguments", [])
    script = Path(arguments[-1]) if arguments else None
    if script is None or script.name != "sync_usage.py" or not script.is_file():
        issues.append("launchd writer script is missing")
    return issues


def main() -> int:
    issues = installation_issues()
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}", file=sys.stderr)
        return 1
    print("Aether Ledger installation is healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
