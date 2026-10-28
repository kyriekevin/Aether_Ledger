"""Discover and deduplicate local session sources without reading prompts into output."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

CODEX_SESSION_DIR = Path.home() / ".codex" / "sessions"

MULTICA_CODEX_SESSION_DIR = Path.home() / ".codex" / "multica-sessions"

MULTICA_TASK_WORKSPACES_ROOT = Path(
    os.environ.get("MULTICA_TASK_WORKSPACES_ROOT", "").strip()
) if os.environ.get("MULTICA_TASK_WORKSPACES_ROOT", "").strip() else None


def _workspace_codex_homes(root: Path) -> list[Path]:
    """Resolve home boundaries independently of directory alias traversal order.

    Scan each physical directory once, recording ordinary directory edges and
    named home edges separately. Named edges are terminal. An alias can expose
    a home's contents during this scan, but declarations in those contents are
    accepted only if their parent remains reachable outside valid home boundaries.
    """
    if root.name == "codex-home" or root.resolve().name == "codex-home":
        return [root]

    def identity(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_dev, stat.st_ino

    directories: dict[tuple[int, int], set[tuple[int, int]]] = {}
    declarations: list[tuple[tuple[int, int], tuple[int, int], Path]] = []
    canonical_homes: set[tuple[int, int]] = set()
    pending = [root]
    try:
        root_id = identity(root)
        while pending:
            directory = pending.pop()
            parent_id = identity(directory)
            if parent_id in directories:
                continue
            directories[parent_id] = set()
            if directory.resolve().name == "codex-home":
                canonical_homes.add(parent_id)
            with os.scandir(directory) as entries:
                children = sorted(
                    Path(entry.path) for entry in entries
                    if entry.name not in {".git", "node_modules", ".venv", "__pycache__"}
                    and entry.is_dir(follow_symlinks=True)
                )
            for child in children:
                child_id = identity(child)
                if child.name == "codex-home":
                    declarations.append((parent_id, child_id, child))
                else:
                    directories[parent_id].add(child_id)
                    pending.append(child)
    except OSError as error:
        raise OSError("cannot read configured Multica workspace tree") from error

    # Discarded cache declarations must stop influencing the next reachability
    # pass. Re-evaluate until the set of valid homes stabilizes. Mutually
    # contradictory alias declarations fail visibly rather than guessing.
    boundaries = frozenset(home_id for _, home_id, _ in declarations)
    attempted: set[frozenset[tuple[int, int]]] = set()
    while boundaries not in attempted:
        attempted.add(boundaries)
        reachable: set[tuple[int, int]] = set()
        expanded: set[tuple[int, int]] = set()
        pending_ids = [root_id]
        while pending_ids:
            directory_id = pending_ids.pop()
            if directory_id in reachable:
                continue
            reachable.add(directory_id)
            if directory_id != root_id and directory_id in boundaries | canonical_homes:
                continue
            expanded.add(directory_id)
            pending_ids.extend(directories.get(directory_id, ()))
        homes = [
            (home_id, path) for parent_id, home_id, path in declarations
            if parent_id in expanded or (parent_id == home_id and parent_id in reachable)
        ]
        updated = frozenset(home_id for home_id, _ in homes)
        if updated == boundaries:
            # Reachability starts at the configured root even if it is a home.
            # Once confirmed as such, its contents cannot supply more homes.
            return [path for home_id, path in homes if root_id not in updated or home_id == root_id]
        boundaries = updated
    raise ValueError("ambiguous cyclic Multica harness-home aliases")


def multica_codex_session_roots(
    shared: Path = MULTICA_CODEX_SESSION_DIR,
    workspaces_root: Path | None = MULTICA_TASK_WORKSPACES_ROOT,
) -> list[Path]:
    """Return every local Multica Codex session tree, once.

    Discover the harness's ``codex-home`` marker below the configured workspace
    root. Task and project names are opaque; neither their prefix nor their
    nesting depth is part of the accounting contract. Stop at each harness home
    so its caches, skills and session contents are not searched for more homes.
    """
    candidates = [shared]
    if workspaces_root is not None:
        if not workspaces_root.is_dir():
            raise ValueError("configured Multica workspace root is missing")
        homes = _workspace_codex_homes(workspaces_root)
        for home in homes:
            candidates.extend(home / name for name in ("sessions", "archived_sessions"))

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        try:
            identity = candidate.resolve()
        except OSError:
            continue
        if identity not in seen:
            seen.add(identity)
            roots.append(candidate)
    return sorted(roots)


def _codex_session_files(sessions_dirs: Iterable[Path]) -> list[tuple[Path, Path]]:
    """Choose one copy of each rollout across session trees.

    Shared rollouts carry profile/agent/issue path prefixes that task-private
    copies do not, so their relative paths are not comparable. The rollout
    filename contains its globally unique session ID and is stable across both
    layouts. On a collision the largest copy is preferred because a live JSONL
    grows by appending events.
    """
    selected: dict[str, tuple[Path, Path, int]] = {}
    for sessions_dir in sessions_dirs:
        if not sessions_dir.is_dir():
            continue
        for path in sessions_dir.rglob("*.jsonl"):
            relative = path.relative_to(sessions_dir)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            previous = selected.get(path.name)
            if previous is None or size > previous[2]:
                selected[path.name] = (relative, path, size)
    return sorted((relative, path) for relative, path, _ in selected.values())


def codex_source_summary(roots: Iterable[Path]) -> str:
    """Report coverage without exposing directory or session identities."""
    roots = list(roots)
    files = _codex_session_files(roots)
    newest = max((path.stat().st_mtime for _, path in files), default=None)
    modified = datetime.fromtimestamp(newest, timezone.utc).isoformat() if newest else "none"
    state = "ok" if files else "empty"
    return f"status={state} roots={len(roots)} files={len(files)} latest_mtime={modified}"


@contextmanager
def _codex_home_over(sessions: Path | Iterable[Path]) -> Iterator[Path]:
    """Yield a throwaway CODEX_HOME over one or more session trees.

    ccusage's Codex reader only ever looks at `$CODEX_HOME/sessions`, while
    Multica's rollouts live outside the ordinary Codex tree. The source trees
    remain read-only; only selected rollout bytes are copied into the temporary
    merged view.

    Duplicate rollout paths are copied only once. ccusage deliberately ignores
    symlinked files, so the temporary merge must contain regular files. A missing
    source yields a home with no ``sessions`` directory and therefore no usage.
    """
    with tempfile.TemporaryDirectory(prefix="codex-alt-") as tmp:
        root = Path(tmp)
        sessions_dirs = [sessions] if isinstance(sessions, Path) else list(sessions)
        files = _codex_session_files(sessions_dirs)
        for relative, source in files:
            destination = root / "sessions" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        yield root
