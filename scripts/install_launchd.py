#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Install a path-expanded launchd agent for a dedicated writer worktree."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.kyriekevin.aether-ledger"
LEGACY_LABEL = "com.kyriekevin.cc-cx-usage-data"
DURABLE_NODES = frozenset({"work", "personal", "devbox"})
TEMPLATE = REPO_ROOT / "launchd" / f"{LABEL}.plist.template"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MULTICA_CONFIG_KEYS = {
    "profile": "MULTICA_PROFILE",
    "workspaceId": "MULTICA_WORKSPACE_ID",
    "dshProfile": "MULTICA_DSH_PROFILE",
}


def load_multica_environment(home: Path) -> dict[str, str]:
    """Load private Multica inputs without exposing their values in output."""
    path = home / ".config" / "token-activity" / "multica.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path}: {type(error).__name__}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    unknown = set(payload).difference(MULTICA_CONFIG_KEYS)
    if unknown:
        raise ValueError(f"{path} has unknown keys: {', '.join(sorted(unknown))}")
    environment = {}
    for key, variable in MULTICA_CONFIG_KEYS.items():
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path} field {key!r} must be a non-empty string")
        environment[variable] = value.strip()
    return environment


def render_template(
    home: Path, repo_root: Path, environment: dict[str, str] | None = None
) -> str:
    content = TEMPLATE.read_text(encoding="utf-8")
    extra_environment = "\n".join(
        f"    <key>{escape(key)}</key>\n    <string>{escape(value)}</string>"
        for key, value in sorted((environment or {}).items())
    )
    return (
        content.replace("__HOME__", escape(str(home)))
        .replace("__REPO_DIR__", escape(str(repo_root)))
        .replace("__EXTRA_ENVIRONMENT__", extra_environment)
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git_common_dir(repo_root: Path) -> Path | None:
    result = _git(repo_root, "rev-parse", "--git-common-dir")
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = repo_root / common
    return common.resolve()


def ensure_writer_worktree(writer: Path, repo_root: Path, today: date) -> Path:
    """Create or reuse the launchd-only checkout for today's data branch."""
    writer = writer.expanduser().resolve()
    repo_root = repo_root.resolve()
    if writer.exists():
        top = _git(writer, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != writer:
            raise RuntimeError(f"writer path exists but is not a Git worktree: {writer}")
        if _git_common_dir(writer) != _git_common_dir(repo_root):
            raise RuntimeError(f"writer path belongs to a different Git repository: {writer}")
        return writer

    branch = f"usage/{today.isoformat()}"
    current = _git(repo_root, "branch", "--show-current")
    if current.returncode != 0:
        raise RuntimeError(f"cannot inspect source checkout: {current.stderr.strip()}")
    if current.stdout.strip() == branch:
        raise RuntimeError(
            f"switch the source checkout off {branch} before creating its writer worktree"
        )

    # A cache cleanup can remove the directory while Git still records the exact
    # linked worktree. Remove only that stale registration before recreating it;
    # an unregistered path simply makes this best-effort command return nonzero.
    _git(repo_root, "worktree", "remove", "--force", str(writer))
    writer.parent.mkdir(parents=True, exist_ok=True)
    local = _git(repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    remote = _git(
        repo_root, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"
    )
    if local.returncode == 0:
        command = ["worktree", "add", str(writer), branch]
    elif remote.returncode == 0:
        command = ["worktree", "add", "--track", "-b", branch, str(writer), f"origin/{branch}"]
    else:
        command = ["worktree", "add", "--detach", str(writer), "main"]
    created = _git(repo_root, *command)
    if created.returncode != 0:
        raise RuntimeError(f"cannot create writer worktree: {created.stderr.strip()}")
    return writer


def remove_legacy_agent(home: Path, uid: int) -> bool:
    """Unload and remove the pre-Aether launchd agent when it exists."""
    legacy = home / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist"
    if not legacy.exists():
        return False
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(legacy)],
        capture_output=True,
        text=True,
        check=False,
    )
    legacy.unlink()
    return True


def reload_agent(destination: Path, uid: int) -> subprocess.CompletedProcess[str]:
    """Replace any loaded copy with the freshly rendered launchd agent."""
    domain = f"gui/{uid}"
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return subprocess.run(
        ["launchctl", "bootstrap", domain, str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )


def migrate_legacy_node_name(home: Path) -> str | None:
    """Copy an approved legacy role label into the Aether Ledger config."""
    destination = home / ".config" / "token-activity" / "node_name"
    if destination.exists():
        return None
    legacy = home / ".config" / "feishu-claude-usage" / "machine_name"
    if not legacy.exists():
        return None
    node_name = legacy.read_text(encoding="utf-8").strip().lower()
    if node_name not in DURABLE_NODES:
        return None
    atomic_write(destination, f"{node_name}\n")
    return node_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the scheduled usage writer in a dedicated Git worktree."
    )
    parser.add_argument(
        "--writer-worktree",
        type=Path,
        help="launchd-only checkout (default: ~/.cache/aether-ledger/writer)",
    )
    args = parser.parse_args(argv)
    home = Path.home().resolve()
    try:
        multica_environment = load_multica_environment(home)
    except ValueError as error:
        print(f"local configuration is invalid: {error}", file=sys.stderr)
        return 1
    writer_path = args.writer_worktree or home / ".cache" / "aether-ledger" / "writer"
    existed = writer_path.expanduser().exists()
    try:
        writer_root = ensure_writer_worktree(
            writer_path, REPO_ROOT, datetime.now(SHANGHAI).date()
        )
    except RuntimeError as error:
        print(f"writer worktree setup failed: {error}", file=sys.stderr)
        return 1
    print(f"{'reusing' if existed else 'created'} writer worktree {writer_root}")
    migrated = migrate_legacy_node_name(home)
    if migrated:
        print(f"migrated durable node role {migrated!r}")
    if remove_legacy_agent(home, os.getuid()):
        print(f"removed legacy launchd agent {LEGACY_LABEL}")
    destination = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    (home / "Library" / "Logs" / "aether-ledger").mkdir(parents=True, exist_ok=True)
    atomic_write(
        destination, render_template(home, writer_root, multica_environment)
    )
    print(f"installed {destination}")
    loaded = reload_agent(destination, os.getuid())
    if loaded.returncode != 0:
        print(f"launchctl bootstrap failed: {loaded.stderr.strip()}")
        return loaded.returncode
    print(f"reloaded launchd agent {LABEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
