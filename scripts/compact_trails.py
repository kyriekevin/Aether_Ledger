#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Fold dead trail pods into data/trail/rollup, then prune their folders.

Ephemeral workers each sync into a unique opaque nested folder
`data/trail/node-<digest>/` (see sync_usage.py,
CC_USAGE_TRAIL). Because k8s allocates a fresh pod every run, those folders
would otherwise pile up forever and slow every clone/pull.

Run this on the SINGLE writer machine only (single writer ⇒ the additive fold is
race-free). It folds any trail pod whose newest data date is older than
TRAIL_TTL_DAYS into the cumulative `data/trail/rollup/{claude,codex,opencode}.json`
(ADDITIVE — each dead pod's data is final and distinct), then `git rm`s the pod
folders in the SAME commit. The pusher (update_signature.py) sums data/trail/rollup
and any still-live pods, so the cumulative total is preserved exactly while the
repo stays bounded to (live pods + rollup).

Idempotent by construction: the rollup baseline is read from the committed tree
(git show HEAD), and a pod is removed in the same commit it is folded into the
rollup. A crashed run (no commit) recomputes the identical rollup next time.
After a hard crash mid-run, `git checkout -- .` in the data repo before re-running
restores a clean committed baseline.

Usage:
    uv run --script scripts/compact_trails.py            # fold + prune + push
    uv run --script scripts/compact_trails.py --dry-run  # report only, no writes
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from render_dashboard import SHANGHAI
from sync_usage import prepare_daily_branch

# Data repo root: two levels up from this script (repo/scripts/compact_trails.py)
DATA_REPO_DIR = Path(__file__).resolve().parents[1]
TRAIL_DIR = DATA_REPO_DIR / "data" / "trail"
ROLLUP_DIR = TRAIL_DIR / "rollup"
AGENT_FILES = ("claude.json", "codex.json", "opencode.json")

# A pod with no data newer than this many days is "dead" (its k8s box is gone).
TRAIL_TTL_DAYS = 7

GIT_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Git / IO helpers
# ---------------------------------------------------------------------------

def _git(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], cwd=DATA_REPO_DIR,
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(
            args=e.cmd, returncode=124, stdout="",
            stderr=f"git timed out after {GIT_TIMEOUT_SECONDS}s",
        )


def _branch_is_ahead() -> bool:
    """True if HEAD is ahead of upstream — a prior run committed but failed to
    push, so the fold is stranded locally and must be retried before anything else."""
    r = _git(["rev-list", "--count", "@{u}..HEAD"])
    return r.returncode == 0 and r.stdout.strip() not in ("", "0")


def _push_with_retry() -> bool:
    """Push HEAD; on failure rebase onto upstream once and retry. True on success."""
    if _git(["push"]).returncode == 0:
        return True
    if _git(["pull", "--rebase", "--autostash"]).returncode != 0:
        return False
    return _git(["push"]).returncode == 0


def _abort_clean() -> None:
    """Restore a clean committed tree after a mid-transaction git failure.

    reset unstages, checkout restores tracked trail files (incl. pod folders that
    `git rm` removed), and clean drops any newly-written-but-untracked rollup
    files. The next run then recomputes the fold from the committed baseline.
    """
    _git(["reset", "-q", "HEAD"])
    _git(["checkout", "-q", "--", "data/trail"])
    _git(["clean", "-fdq", "data/trail/rollup"])


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Trail pod discovery + folding
# ---------------------------------------------------------------------------

def _iter_trail_pods():
    """Yield each live trail pod folder under data/trail/ (excluding the rollup)."""
    if not TRAIL_DIR.exists():
        return
    for p in sorted(TRAIL_DIR.iterdir()):
        if p.is_dir() and p.name != "rollup" and not p.name.startswith("."):
            yield p


class CorruptPod(Exception):
    """A pod has an existing-but-unreadable agent json. Such a pod is skipped
    (left in place) rather than treated as empty — otherwise a recoverable but
    momentarily-corrupt store would be silently pruned and its data lost."""


def _read_store(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise CorruptPod(f"{path}: {e}") from e


def _newest_data_date(pod_dir: Path) -> date | None:
    newest: date | None = None
    for fname in AGENT_FILES:
        for d in _read_store(pod_dir / fname):
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                continue
            if newest is None or dd > newest:
                newest = dd
    return newest


def _load_committed_rollup(fname: str) -> dict:
    """Read the rollup from the committed tree, ignoring any dirty working copy.

    Reading HEAD (not the working file) keeps the fold idempotent even if a
    previous run crashed after writing the rollup but before committing.
    """
    r = _git(["show", f"HEAD:data/trail/rollup/{fname}"])
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def _fold_pod_into(rollups: dict[str, dict], pod_dir: Path) -> None:
    """Add a dead pod's per-day {totalTokens, totalCost, imageCount} additively.

    Per-model `models` breakdowns are intentionally dropped — they are debug-only
    and don't aggregate meaningfully across pods. The pusher only consumes
    totalTokens / totalCost / imageCount (imageCount stays raw; the pusher applies
    image_gen pricing at display time, same as for live pods).
    """
    for fname in AGENT_FILES:
        store = _read_store(pod_dir / fname)
        roll = rollups[fname]
        for d, entry in store.items():
            agg = roll.setdefault(d, {"totalTokens": 0, "totalCost": 0.0})
            agg["totalTokens"] += entry.get("totalTokens", 0)
            agg["totalCost"] += entry.get("totalCost", 0.0)
            img = entry.get("imageCount", 0)
            if img or "imageCount" in agg:
                agg["imageCount"] = agg.get("imageCount", 0) + img


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    dry = "--dry-run" in sys.argv[1:]

    # Compaction writes are ordinary usage updates and belong on today's branch.
    # A dry-run remains completely local and does not switch branches.
    if not dry and not prepare_daily_branch(datetime.now(SHANGHAI).date()):
        print("today's usage branch is not ready; deferring compaction", file=sys.stderr)
        return 0

    # Pull so we see the latest pod pushes and other machines' commits.
    pull = _git(["pull", "--rebase", "--autostash"])
    if pull.returncode != 0:
        print(f"git pull failed (continuing with local state): {pull.stderr.strip()}",
              file=sys.stderr)

    # A prior run may have committed a fold but failed to push it. Republish that
    # stranded commit first, before the early-returns below skip it forever.
    if not dry and _branch_is_ahead():
        if not _push_with_retry():
            print("stranded compaction commit still un-pushed; retrying next run",
                  file=sys.stderr)

    if not TRAIL_DIR.exists():
        print("no data/trail/ namespace; nothing to compact")
        return 0

    cutoff = date.today() - timedelta(days=TRAIL_TTL_DAYS)
    dead: list[tuple[Path, date | None]] = []
    for pod in _iter_trail_pods():
        try:
            newest = _newest_data_date(pod)
        except CorruptPod as e:
            # Unreadable data: leave the pod untouched for manual inspection.
            print(f"skip pod with unreadable json (left in place): {e}", file=sys.stderr)
            continue
        # newest is None only when every store parses but is empty ({} / absent)
        # → nothing to lose, safe to prune (a still-live pod just recreates it).
        if newest is None or newest < cutoff:
            dead.append((pod, newest))

    if not dead:
        print(f"no trail pods older than {TRAIL_TTL_DAYS}d; nothing to compact")
        return 0

    rollups = {f: _load_committed_rollup(f) for f in AGENT_FILES}
    for pod, newest in dead:
        _fold_pod_into(rollups, pod)
        print(f"fold {pod.relative_to(DATA_REPO_DIR)} (newest={newest})")

    if dry:
        for f in AGENT_FILES:
            print(f"  rollup/{f}: {len(rollups[f])} day(s) after fold")
        print(f"DRY RUN: would remove {len(dead)} pod folder(s), no writes made")
        return 0

    for f in AGENT_FILES:
        if rollups[f]:  # don't create an empty rollup file for an unused agent
            _atomic_write_json(ROLLUP_DIR / f, rollups[f])

    # Stage the rollup update AND the pod deletions, then commit them together.
    # Any git failure here aborts and restores a clean committed tree, so a dead
    # pod is never committed-to-rollup without also being removed (no re-fold /
    # double-count on the next run).
    for pod, _ in dead:
        rm = _git(["rm", "-r", "--quiet", str(pod.relative_to(DATA_REPO_DIR))])
        if rm.returncode != 0:
            print(f"git rm failed for {pod.name}: {rm.stderr.strip()}; aborting compaction",
                  file=sys.stderr)
            _abort_clean()
            return 1

    if _git(["add", "data/trail/rollup"]).returncode != 0:
        print("git add data/trail/rollup failed; aborting compaction", file=sys.stderr)
        _abort_clean()
        return 1

    if _git(["diff", "--cached", "--quiet"]).returncode == 0:
        # Nothing staged means the rollup didn't change AND no pod was removed —
        # inconsistent with having dead pods. Bail clean rather than commit junk.
        print("nothing staged after compaction; aborting", file=sys.stderr)
        _abort_clean()
        return 1

    msg = f"chore(data): compact {len(dead)} trail node(s) into rollup"
    if _git(["commit", "-m", msg]).returncode != 0:
        print("git commit failed; aborting compaction", file=sys.stderr)
        _abort_clean()
        return 1

    if not _push_with_retry():
        # Commit is safely local and consistent; _branch_is_ahead() will retry
        # the push on the next run. Don't abort — the fold is durable in HEAD.
        print("git push failed after one rebase-retry; commit is local, "
              "will retry next run", file=sys.stderr)
        return 1
    print(f"compacted {len(dead)} pod(s) → data/trail/rollup; pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
