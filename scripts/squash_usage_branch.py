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
    r"(?:claude|codex|codex-multica|opencode|traex|dsh)\.json$"
    r"|^data/trail/[^/]+/"
    r"(?:claude|codex|codex-multica|opencode|traex|dsh)\.json$"
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


def squash(branch: str) -> bool:
    merged = git("merge", "--squash", branch)
    if merged.returncode == 0:
        return True

    conflicts = git("diff", "--name-only", "--diff-filter=U", capture=True)
    paths = conflicts.stdout.splitlines() if conflicts.returncode == 0 else []
    if not paths:
        print(f"cannot squash {branch}: merge failed without resolvable conflicts", file=sys.stderr)
        return False

    fork = git("merge-base", "HEAD", branch, capture=True)
    fork_point = fork.stdout.strip() if fork.returncode == 0 else ""
    unsafe: list[str] = []
    for path in paths:
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
        if git("checkout", "--theirs", "--", path).returncode != 0:
            git("reset", "--merge")
            return False
        if git("add", "--", path).returncode != 0:
            git("reset", "--merge")
            return False
        print(f"resolved stale generated-store conflict from {branch}: {path}")
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <branch>", file=sys.stderr)
        return 2
    return 0 if squash(sys.argv[1]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
