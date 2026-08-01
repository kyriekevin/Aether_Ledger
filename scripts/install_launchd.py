#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Install a path-expanded launchd agent for the current checkout."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

REPO_ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.kyriekevin.aether-ledger"
LEGACY_LABEL = "com.kyriekevin.cc-cx-usage-data"
DURABLE_NODES = frozenset({"work", "personal", "devbox"})
TEMPLATE = REPO_ROOT / "launchd" / f"{LABEL}.plist.template"


def render_template(home: Path, repo_root: Path) -> str:
    content = TEMPLATE.read_text(encoding="utf-8")
    return content.replace("__HOME__", escape(str(home))).replace(
        "__REPO_DIR__", escape(str(repo_root))
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


def main() -> int:
    home = Path.home().resolve()
    migrated = migrate_legacy_node_name(home)
    if migrated:
        print(f"migrated durable node role {migrated!r}")
    if remove_legacy_agent(home, os.getuid()):
        print(f"removed legacy launchd agent {LEGACY_LABEL}")
    destination = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    (home / "Library" / "Logs" / "aether-ledger").mkdir(parents=True, exist_ok=True)
    atomic_write(destination, render_template(home, REPO_ROOT))
    print(f"installed {destination}")
    print(f'activate with: launchctl bootstrap "gui/$(id -u)" "{destination}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
