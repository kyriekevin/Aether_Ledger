#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Squash a completed usage branch, resolving proven stale-store conflicts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


GENERATED_STORE = re.compile(
    r"^data/(?:work|personal|devbox)/"
    r"(?:claude|codex|codex-multica|traex|statistics|issue-activity)\.json$"
    # The Multica task aggregate is machine-independent, so it sits at the data
    # root rather than under a node label — but it is regenerated the same way
    # and squashes under the same rule.
    r"|^data/multica\.json$"
)
# Stores of sources that are no longer collected. A day branch cut before their
# removal can still touch them, so a squash deletes them instead of refusing.
RETIRED_STORE = re.compile(
    r"^data/(?:work|personal|devbox)/(?:dsh|dsh-multica|opencode)\.json$"
)


def git(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=False,
        text=True,
        capture_output=capture,
    )


def blob_at(revision: str, path: str) -> str | None:
    result = git("rev-parse", f"{revision}:{path}", capture=True)
    return result.stdout.strip() if result.returncode == 0 else None


def branch_contains_blob_after(branch: str, fork: str, path: str, blob: str) -> bool:
    history = git(
        "log",
        "--first-parent",
        "--format=%H",
        f"{fork}..{branch}",
        "--",
        path,
        capture=True,
    )
    if history.returncode != 0:
        return False
    return any(blob_at(commit, path) == blob for commit in history.stdout.splitlines())


def drop_retired_stores() -> bool:
    listed = git("ls-files", "--", "data", capture=True)
    if listed.returncode != 0:
        return False
    for path in listed.stdout.splitlines():
        if RETIRED_STORE.fullmatch(path):
            if git("rm", "-q", "-f", "--", path).returncode != 0:
                return False
            print(f"removed retired store from {path}")
    return True


def squash(branch: str) -> bool:
    merged = git("merge", "--squash", branch)
    if merged.returncode == 0:
        if drop_retired_stores():
            return True
        git("reset", "--merge")
        return False

    conflicts = git("diff", "--name-only", "--diff-filter=U", capture=True)
    paths = conflicts.stdout.splitlines() if conflicts.returncode == 0 else []
    if not paths:
        print(f"cannot squash {branch}: merge failed without resolvable conflicts", file=sys.stderr)
        return False

    fork = git("merge-base", "HEAD", branch, capture=True)
    fork_point = fork.stdout.strip() if fork.returncode == 0 else ""
    unsafe: list[str] = []
    for path in paths:
        if RETIRED_STORE.fullmatch(path):
            continue
        main_blob = blob_at("HEAD", path)
        if (
            not GENERATED_STORE.fullmatch(path)
            or main_blob is None
            or not fork_point
            or not branch_contains_blob_after(branch, fork_point, path, main_blob)
        ):
            unsafe.append(path)

    if unsafe:
        print(
            "refusing to resolve conflicts that are not proven stale generated stores: "
            + ", ".join(unsafe),
            file=sys.stderr,
        )
        git("reset", "--merge")
        return False

    for path in paths:
        if RETIRED_STORE.fullmatch(path):
            if git("rm", "-q", "-f", "--", path).returncode != 0:
                git("reset", "--merge")
                return False
            print(f"resolved retired-store conflict from {branch}: {path}")
            continue
        if git("checkout", "--theirs", "--", path).returncode != 0:
            git("reset", "--merge")
            return False
        if git("add", "--", path).returncode != 0:
            git("reset", "--merge")
            return False
        print(f"resolved stale generated-store conflict from {branch}: {path}")
    if not drop_retired_stores():
        git("reset", "--merge")
        return False
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <branch>", file=sys.stderr)
        return 2
    return 0 if squash(sys.argv[1]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
