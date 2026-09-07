"""Discover and deduplicate local session sources without reading prompts into output."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

MULTICA_DSH_PROFILE_FILE = Path.home() / ".config" / "token-activity" / "multica_dsh_profile"

CODEX_SESSION_DIR = Path.home() / ".codex" / "sessions"

MULTICA_CODEX_SESSION_DIR = Path.home() / ".codex" / "multica-sessions"

MULTICA_TASK_WORKSPACES_ROOT = Path(
    os.environ.get("MULTICA_TASK_WORKSPACES_ROOT", "").strip()
) if os.environ.get("MULTICA_TASK_WORKSPACES_ROOT", "").strip() else None

DSH_HOME = Path(os.environ.get("DSH_HOME", "").strip() or Path.home() / ".dsh")

MULTICA_HOME = Path(
    os.environ.get("MULTICA_HOME", "").strip() or Path.home() / ".multica"
)

MULTICA_DSH_PROFILE = os.environ.get("MULTICA_DSH_PROFILE", "").strip() or None


def dsh_session_roots(home: Path = DSH_HOME) -> tuple[Path, ...]:
    """The session tree under one dsh harness home, or nothing when absent.

    A tuple rather than a single path so a caller reads the same shape whether or
    not the home exists. See DSH_HOME for why only the default tree is read.
    """
    default = home / "sessions"
    return (default,) if default.is_dir() else ()


def multica_dsh_session_roots(
    home: Path = MULTICA_HOME,
    profile: str | None = MULTICA_DSH_PROFILE,
    binding_file: Path = MULTICA_DSH_PROFILE_FILE,
) -> tuple[Path, ...]:
    """The one Multica profile tree assigned to dsh-multica.json."""
    profiles = home / "profiles"
    bound = _read_multica_dsh_profile(binding_file)
    if profile is not None and bound is not None and profile != bound:
        raise ValueError(
            f"dsh-multica.json is bound to Multica profile {bound!r}; refusing "
            f"to switch it to MULTICA_DSH_PROFILE={profile!r}"
        )
    selected_profile = bound or profile
    if selected_profile is not None:
        _validate_multica_dsh_profile(selected_profile)
        selected = profiles / selected_profile / "dsh-sessions"
        if not selected.is_dir():
            raise ValueError(
                f"bound Multica DSH profile {selected_profile!r} has no "
                "dsh-sessions tree; keeping the stored high-water"
            )
        if bound is None:
            persisted = _bind_multica_dsh_profile(binding_file, selected_profile)
            if persisted != selected_profile:
                raise ValueError(
                    f"dsh-multica.json was concurrently bound to {persisted!r}; "
                    f"refusing to collect {selected_profile!r}"
                )
        return (selected,)
    if not profiles.is_dir():
        return ()
    candidates = tuple(sorted(
        path for path in profiles.glob("*/dsh-sessions") if path.is_dir()
    ))
    if len(candidates) > 1:
        raise ValueError(
            "multiple Multica profiles have dsh-sessions trees; set "
            "MULTICA_DSH_PROFILE so dsh-multica.json keeps one fixed source"
        )
    if not candidates:
        return ()
    selected_profile = candidates[0].parent.name
    persisted = _bind_multica_dsh_profile(binding_file, selected_profile)
    if persisted != selected_profile:
        raise ValueError(
            f"dsh-multica.json was concurrently bound to {persisted!r}; "
            f"refusing to collect {selected_profile!r}"
        )
    return candidates


def _validate_multica_dsh_profile(profile: str) -> None:
    if Path(profile).name != profile or profile in {"", ".", ".."}:
        raise ValueError("Multica DSH profile must be one profile directory name")


def _read_multica_dsh_profile(path: Path) -> str | None:
    try:
        profile = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    _validate_multica_dsh_profile(profile)
    return profile


def _bind_multica_dsh_profile(path: Path, profile: str) -> str:
    """Atomically bind dsh-multica.json to one local Multica profile."""
    _validate_multica_dsh_profile(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(profile + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            persisted = _read_multica_dsh_profile(path)
            if persisted is None:  # pragma: no cover - link says it exists
                raise OSError(f"cannot read concurrently created {path}")
            return persisted
    finally:
        os.unlink(tmp)
    return profile


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
        def unreadable(error: OSError) -> None:
            raise OSError("cannot read configured Multica workspace tree") from error

        visited: set[tuple[int, int]] = set()
        for directory, children, _ in os.walk(
            workspaces_root, onerror=unreadable, followlinks=True,
        ):
            home = Path(directory)
            if home.name == "codex-home":
                candidates.extend(home / name for name in ("sessions", "archived_sessions"))
                children[:] = []
            else:
                # Links may point outside the workspace or back to an ancestor.
                # Traverse each physical directory once. Check the harness marker
                # first so an earlier alias cannot hide a named codex-home.
                stat = home.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity in visited:
                    children[:] = []
                    continue
                visited.add(identity)
                children[:] = sorted(
                    name for name in children
                    if name not in {".git", "node_modules", ".venv", "__pycache__"}
                )

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
