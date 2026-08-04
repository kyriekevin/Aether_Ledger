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

Exit status: 0 means nothing was left undone. 1 means "read stderr", which covers
both an outright failure AND a partial success — pods that report identical token
counts are quarantined rather than folded, so a run can commit and push a real
fold and still exit 1 because those pods need a human. Anything consuming this
status must treat 1 as "work remains", not as "the fold did not happen".
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from render_dashboard import SHANGHAI
from sync_usage import (
    GIT_LOCK_PATH,
    GIT_LOCK_WAIT_SECONDS,
    git_commit,
    prepare_daily_branch,
    repo_git_lock,
)

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


def _identical_sources(pods: list[Path]) -> list[tuple[str, str, str, str, int]]:
    """Pods that report the same day with byte-identical token counts.

    The additive fold below takes each pod's data as distinct, which held only while
    a pod ID was unique per worker. It was not: deriving the ID from the hostname
    gave one machine a fresh identity every time its hostname moved, its persistent
    ~/.codex history was re-uploaded under each, and folding them multiplied those
    days — one reached the signature six times over before this was caught.

    Two pods agreeing to the token on a day did not do the same work twice; they are
    one worker seen twice. Adding them is the single thing compaction must never do,
    and it cannot be undone from the rollup afterwards, so refuse the whole run.

    The token count is part of the key, not something compared against one remembered
    pod: three pods where only the last two agree are still two copies of one worker,
    and keying on (file, date) alone would have compared each of them to the first
    and reported nothing.
    """
    by_count: dict[tuple[str, str, int], list[str]] = {}
    for pod in sorted(pods):
        for fname in AGENT_FILES:
            for d, entry in _read_store(pod / fname).items():
                tokens = entry.get("totalTokens", 0)
                if not tokens:
                    continue
                by_count.setdefault((fname, d, tokens), []).append(pod.name)
    return [
        (fname, d, names[0], other, tokens)
        for (fname, d, tokens), names in sorted(by_count.items())
        if len(names) > 1
        for other in names[1:]
    ]


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

    # This is the third Git user of the shared checkout, after the 15-minute sync
    # writer and the signature pusher, and it is the one a human starts by hand at
    # an arbitrary moment. It must take the same lock or it reintroduces exactly
    # the FETCH_HEAD and working-tree races that lock exists to prevent. Even a
    # dry-run pulls before reporting, so it is no exception.
    with repo_git_lock(GIT_LOCK_WAIT_SECONDS) as acquired:
        if not acquired:
            print(
                f"another process has held {GIT_LOCK_PATH} for "
                f"{GIT_LOCK_WAIT_SECONDS}s; try compaction again later",
                file=sys.stderr,
            )
            return 0
        return _compact(dry=dry)


def _compact(*, dry: bool) -> int:
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

    # Check every pod, not only the dying ones: a live pod repeating a dead pod's
    # days is the same duplication seen from the other side.
    clashes = _identical_sources(list(_iter_trail_pods()))
    if clashes:
        # Quarantine the pods involved rather than the whole run. Folding them is
        # what must never happen — it multiplies the total and the rollup cannot
        # be un-added afterwards — but pods with no part in the clash are ordinary
        # dead pods, and refusing those too would let data/trail grow without
        # bound until someone notices, which is how this went unseen for weeks.
        suspect = {name for _, _, a, b, _ in clashes for name in (a, b)}
        print(
            f"{len(suspect)} pod(s) report identical token counts, so they are one "
            f"worker under several IDs and adding them would multiply the total. "
            f"Leaving them in place; collapse data/trail by hand, then re-run.",
            file=sys.stderr,
        )
        for fname, d, a, b, tokens in clashes[:10]:
            print(f"  {fname} {d}: {a} and {b} both report {tokens:,} tokens",
                  file=sys.stderr)
        if len(clashes) > 10:
            print(f"  ... and {len(clashes) - 10} more", file=sys.stderr)
        dead = [(pod, newest) for pod, newest in dead if pod.name not in suspect]
        if not dead:
            print("every dead pod is implicated; nothing safe to fold", file=sys.stderr)
            return 1

    rollups = {f: _load_committed_rollup(f) for f in AGENT_FILES}
    for pod, newest in dead:
        _fold_pod_into(rollups, pod)
        print(f"fold {pod.relative_to(DATA_REPO_DIR)} (newest={newest})")

    if dry:
        for f in AGENT_FILES:
            print(f"  rollup/{f}: {len(rollups[f])} day(s) after fold")
        print(f"DRY RUN: would remove {len(dead)} pod folder(s), no writes made")
        return 1 if clashes else 0

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
    if git_commit(msg).returncode != 0:
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
    # The fold succeeded, but quarantined pods still need a human, and a silent
    # success would let them sit there indefinitely.
    return 1 if clashes else 0


if __name__ == "__main__":
    sys.exit(main())
