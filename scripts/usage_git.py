"""Writer identity, checkout locking, and daily Git branch lifecycle."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

import usage_schema


CONFIG_DIR = Path.home() / ".config" / "token-activity"

NODE_NAME_FILE = CONFIG_DIR / "node_name"

DURABLE_NODES = frozenset({"work", "personal", "devbox"})

ROLLOVER_WATCHDOG_NODES = frozenset({"work", "personal"})

NODE_ID_RE = re.compile(r"node-[0-9a-f]{12}")

TRAIL_ENV = "CC_USAGE_TRAIL"

TRAIL_ID_FILE = CONFIG_DIR / "trail_id"


def _opaque_node_id(raw: str) -> str:
    return f"node-{hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:12]}"


def _minted_trail_node_id() -> str:
    """This worker's folder name, minted once and then reused.

    Deriving it from the hostname looked stable and was not: a box whose hostname
    moves while its HOME persists comes back as a brand new node and re-uploads
    every day it had already reported. One machine accumulated eight identities
    that way, and compact_trails.py folds dead pods additively, so the same days
    reached the signature up to six times over.

    A minted ID cannot drift, and it still expresses what data/trail is for: a
    genuinely fresh pod has a fresh HOME, finds no file, and mints its own. To
    adopt an existing folder — after a hostname change, or when moving a worker —
    write that node-<digest> name into TRAIL_ID_FILE before the next run.

    Only the ABSENCE of the file may mint — not an unusable value in it. A file
    that exists but does not parse is a half-written or damaged identity, and
    reminting over it would orphan the history it points at, the failure this
    whole function exists to prevent, so that case stops and asks for a human.
    Emptiness is one of those values, not a second kind of absence: minting
    publishes the ID by linking a fully written temp file into place, so an empty
    file is never something this code produced, and treating it as absent means
    minting, failing to link over the file that is already there, and reading the
    same emptiness again.

    Linking is what makes minting safe to race: it is atomic and fails if another
    process got there first, so concurrent first runs converge on one ID and no
    reader can observe a partial one. Losing that race re-enters this function
    exactly once, because by then the file exists and must either parse or stop.
    """
    exists = True
    try:
        node_id = TRAIL_ID_FILE.read_text().strip()
    except FileNotFoundError:
        exists, node_id = False, ""
    except OSError as e:
        sys.exit(f"cannot read {TRAIL_ID_FILE}: {e}")
    if NODE_ID_RE.fullmatch(node_id):
        return node_id
    if exists:
        sys.exit(
            f"{TRAIL_ID_FILE} holds {node_id!r}, which is not a node-<12 hex digits> "
            f"name. Refusing to mint a replacement: this worker would start a second "
            f"folder and re-upload history it has already reported. Write the correct "
            f"existing name into the file, or delete it to start a genuinely new node."
        )
    TRAIL_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    minted = f"node-{secrets.token_hex(6)}"
    fd, tmp = tempfile.mkstemp(dir=TRAIL_ID_FILE.parent, prefix=".trail_id.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(minted + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, TRAIL_ID_FILE)
        except FileExistsError:
            # Another process minted between our read and our link. Its ID is as
            # good as ours and it is the one on disk, so adopt it.
            return _minted_trail_node_id()
    finally:
        os.unlink(tmp)
    return minted


def resolve_machine() -> str:
    """This machine's data-repo folder name.

    Ephemeral workers get an opaque nested name under data/trail/, so hostnames
    and job identifiers never enter tracked paths. Durable machines use one of
    three intentionally public role labels.
    """
    trail = os.environ.get(TRAIL_ENV, "").strip()
    if trail:
        if trail.lower() in ("1", "true", "yes"):
            return f"data/trail/{_minted_trail_node_id()}"
        return f"data/trail/{_opaque_node_id(trail)}"
    if not NODE_NAME_FILE.exists():
        sys.exit(
            f"missing {NODE_NAME_FILE}: write one of {sorted(DURABLE_NODES)}, "
            f"or set {TRAIL_ENV}=1 on an ephemeral worker"
        )
    node_name = NODE_NAME_FILE.read_text().strip().lower()
    if node_name not in DURABLE_NODES:
        sys.exit(f"invalid {NODE_NAME_FILE}: expected one of {sorted(DURABLE_NODES)}")
    return f"data/{node_name}"

GIT_TIMEOUT_SECONDS = 30

GIT_LOCK_PATH = Path.home() / ".cache" / "aether-ledger" / "git.lock"

GIT_LOCK_WAIT_SECONDS = 60

AUTOMATION_GIT_NAME = "Aether Ledger"

AUTOMATION_GIT_EMAIL = "noreply@github.com"

DAILY_BRANCH_PREFIX = "usage/"


@contextmanager
def repo_git_lock(wait_seconds: float) -> Iterator[bool]:
    """Hold the checkout-wide Git lock, yielding whether it was acquired.

    An advisory flock is enough because every process that runs Git here is ours.
    The lock is released by the kernel on exit, so a crashed run cannot strand it.

    Only contention (BlockingIOError) is retried. Any other OSError means locking
    itself is broken here — an unsupported filesystem, a bad descriptor — and is
    raised rather than silently reported as a busy peer.

    Not reentrant: a nested acquire flocks a second descriptor against this
    process's own lock, which conflicts even in one process. It would yield False
    after the wait rather than hang, but the caller would then skip work for no
    reason. Take the lock once, at the top of a run — see compact_trails.main().
    """
    GIT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(GIT_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.5)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _git(
    args: list[str],
    *,
    automation_identity: bool = False,
) -> subprocess.CompletedProcess:
    env = None
    if automation_identity:
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": AUTOMATION_GIT_NAME,
            "GIT_AUTHOR_EMAIL": AUTOMATION_GIT_EMAIL,
            "GIT_COMMITTER_NAME": AUTOMATION_GIT_NAME,
            "GIT_COMMITTER_EMAIL": AUTOMATION_GIT_EMAIL,
        })
    try:
        return subprocess.run(
            ["git", *args], cwd=usage_schema.DATA_REPO_DIR,
            capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        # Synthesize a non-zero return so callers can treat it as any other
        # transient git failure and keep moving.
        return subprocess.CompletedProcess(
            args=e.cmd, returncode=124,
            stdout=e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=f"git timed out after {GIT_TIMEOUT_SECONDS}s",
        )


def git_commit(message: str) -> subprocess.CompletedProcess:
    """Create an automated commit with a public-safe, machine-independent identity."""
    return _git(["commit", "-m", message], automation_identity=True)


def git_catch_up(*, fetch: bool = True) -> bool:
    """Best-effort catch-up onto the current branch's upstream. True if we caught up.

    Pass fetch=False when the caller already fetched in this run: each fetch is a
    network round trip taken while holding the checkout-wide lock, and every extra
    one widens the window in which the signature pusher gives up waiting and
    publishes from a tree it could not refresh.

    Deliberately not `git pull --rebase`. Pull rebases onto FETCH_HEAD, and that is
    one file shared by every Git process in the checkout, truncated and rewritten in
    full by each fetch. A fetch landing in another process while pull parses it
    leaves more than one merge head in there and the whole run dies with "Cannot
    rebase onto multiple branches", losing the catch-up. The shared advisory lock
    serializes the scheduled writers but cannot cover a terminal, an editor, or a
    second worktree, all of which share this same file.

    Fetching and then rebasing onto the remote-tracking ref reads refs/remotes/
    instead, which Git updates one ref at a time under a lockfile. A concurrent
    fetch can then only ever land us on a different valid commit, never on a
    half-written view of several branches at once.

    A failure here is logged and the run continues against local state, exactly as
    git pull did before; nothing here unwinds a rebase, because --abort cannot tell
    our rebase from a human's and every writer only ever touches its own
    data/<machine>/ subtree, so there is nothing to conflict on.
    """
    if fetch:
        fetched = _git(["fetch", "--prune", "origin"])
        if fetched.returncode != 0:
            print(f"git fetch failed (continuing with local state): {fetched.stderr.strip()}", file=sys.stderr)
            return False
    upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if upstream.returncode != 0:
        print("current branch has no upstream (continuing with local state)", file=sys.stderr)
        return False
    r = _git(["rebase", "--autostash", upstream.stdout.strip()])
    if r.returncode != 0:
        print(f"git rebase failed (continuing with local state): {r.stderr.strip()}\n"
              f"  If it stopped part-way, {usage_schema.DATA_REPO_DIR} stays parked until a human "
              f"runs `git rebase --continue` or `--abort` there. Either one hands back "
              f"whatever --autostash set aside; it is held in the rebase state, so "
              f"`git stash list` will look empty and is not where to go looking.",
              file=sys.stderr)
        return False
    return True


def _branch_is_ahead() -> bool:
    """True if HEAD is strictly ahead of its upstream — a previous run committed
    but failed to push, so this run should retry the push.
    """
    r = _git(["rev-list", "--count", "@{u}..HEAD"])
    return r.returncode == 0 and r.stdout.strip() not in ("", "0")


def _try_push() -> bool:
    """Push HEAD; on failure rebase onto upstream and retry once.

    Returns True if the remote now matches local HEAD, False otherwise.
    """
    if _git(["push"]).returncode == 0:
        return True
    if not git_catch_up():
        return False
    retry = _git(["push"])
    if retry.returncode != 0:
        print(f"git push retry failed: {retry.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _ref_exists(ref: str) -> bool:
    return _git(["show-ref", "--verify", "--quiet", ref]).returncode == 0


def _current_branch() -> str | None:
    result = _git(["branch", "--show-current"])
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _main_is_checked_out_elsewhere() -> bool:
    """True if another worktree holds main, so this one must not move that ref.

    Only ever asked once `_current_branch()` has said this worktree is not on
    main, which makes any listed holder of main some other worktree.

    Git refuses to move a branch a worktree has checked out, because that would
    leave its index and files describing a commit its branch no longer points
    at. A developer checkout parked on main is the normal state here, not a
    fault: refusing is Git working as intended, so the run reads the refusal
    ahead of time and leaves main to whoever holds it. Nothing downstream
    suffers — every decision this writer makes reads origin/main, and the local
    ref is a courtesy for the human checkout, which pulls for itself.

    An unreadable listing answers yes, because the two mistakes do not cost the
    same. Guessing "held" when it is free skips one courtesy update nothing
    reads. Guessing "free" when it is held brings back the fatal this exists to
    remove, and does it in the worst case there is: a listing that failed by
    timing out has already spent 30 of the 30 seconds the signature pusher will
    wait for the lock this run is holding, and a `branch -f` behind it can spend
    30 more.
    """
    listing = _git(["worktree", "list", "--porcelain"])
    if listing.returncode != 0:
        print(f"cannot list worktrees; leaving local main alone: {listing.stderr.strip()}",
              file=sys.stderr)
        return True
    return any(line.strip() == "branch refs/heads/main" for line in listing.stdout.splitlines())


def _sync_local_main() -> None:
    """Fast-forward the local main ref without overwriting local-only work."""
    remote_ref = "refs/remotes/origin/main"
    local_ref = "refs/heads/main"
    if not _ref_exists(remote_ref):
        return
    if not _ref_exists(local_ref):
        created = _git(["branch", "--track", "main", "origin/main"])
        if created.returncode != 0:
            print(f"cannot create local main: {created.stderr.strip()}", file=sys.stderr)
        return
    ancestor = _git(["merge-base", "--is-ancestor", "main", "origin/main"])
    if ancestor.returncode != 0:
        print("local main has commits not in origin/main; leaving it unchanged", file=sys.stderr)
        return
    if _current_branch() == "main":
        updated = _git(["merge", "--ff-only", "origin/main"])
    elif _main_is_checked_out_elsewhere():
        return
    else:
        updated = _git(["branch", "-f", "main", "origin/main"])
    if updated.returncode != 0:
        print(f"cannot fast-forward local main: {updated.stderr.strip()}", file=sys.stderr)


def _completed_local_daily_branches(before: date) -> list[tuple[date, str]]:
    """List local dated usage branches whose remote branch has disappeared."""
    result = _git([
        "for-each-ref",
        "--format=%(refname:strip=2)",
        "refs/heads/usage/",
    ])
    if result.returncode != 0:
        return []
    completed: list[tuple[date, str]] = []
    for branch in result.stdout.splitlines():
        name = branch.removeprefix(DAILY_BRANCH_PREFIX)
        try:
            branch_date = date.fromisoformat(name)
        except ValueError:
            continue
        remote_ref = f"refs/remotes/origin/{branch}"
        if branch_date < before and not _ref_exists(remote_ref):
            completed.append((branch_date, branch))
    return sorted(completed)


def _cleanup_completed_local_branches(before: date) -> None:
    """Delete only local usage branches that hold nothing of their own.

    Daily branches are squash-merged, so Git cannot use ordinary ancestry to prove
    they were merged. Two independent checks stand in for it:

    1. the branch's `data/` matches the day's finalized snapshot on main, so every
       observation it carries has been published; and
    2. the branch introduced no change outside `data/` *relative to where it forked
       from main*, so it is not hiding local-only work.

    Check 2 deliberately compares against the fork point rather than the snapshot.
    Comparing whole trees against the snapshot would resurrect every code and docs
    commit main merged after the fork — routine while the repo is still moving —
    and pin the branch forever behind a difference it never introduced.

    Both checks read final trees, not commit history, so a branch whose commits
    cancel out (a change and its revert, an empty commit) reads as holding nothing
    and is deleted. `branch -D` drops that branch's reflog along with the ref, so
    those commits keep only whatever other references still reach them — HEAD's
    reflog, if the branch was ever checked out here — and become prunable once
    none do. That is the accepted trade: a squash-merged branch leaves no
    ancestry to test instead.
    """
    current = _current_branch()
    for branch_date, branch in _completed_local_daily_branches(before):
        if branch == current:
            continue
        subject = f"chore(data): finalize {branch_date.isoformat()} snapshot"
        snapshot = _git([
            "log",
            "-1",
            "--format=%H",
            "--fixed-strings",
            f"--grep={subject}",
            "origin/main",
        ])
        snapshot_commit = snapshot.stdout.strip()
        if snapshot.returncode != 0 or not snapshot_commit:
            continue
        published = _git(["diff", "--quiet", snapshot_commit, branch, "--", "data"])
        # `git diff --quiet` exits 1 for differences; anything above that is a
        # real Git failure and must not be reported as unpublished data.
        if published.returncode > 1:
            print(f"cannot compare local {branch} with its finalized snapshot "
                  f"({published.stderr.strip()}); keeping it", file=sys.stderr)
            continue
        if published.returncode == 1:
            print(f"local {branch} holds data missing from its finalized snapshot; keeping it",
                  file=sys.stderr)
            continue
        fork = _git(["merge-base", "origin/main", branch])
        fork_point = fork.stdout.strip()
        if fork.returncode != 0 or not fork_point:
            print(f"cannot locate where local {branch} forked from main; keeping it",
                  file=sys.stderr)
            continue
        own_work = _git([
            "diff",
            "--quiet",
            fork_point,
            branch,
            "--",
            ".",
            ":(exclude)data",
        ])
        if own_work.returncode > 1:
            print(f"cannot compare local {branch} with its fork point "
                  f"({own_work.stderr.strip()}); keeping it", file=sys.stderr)
            continue
        if own_work.returncode == 1:
            print(f"local {branch} carries its own changes outside data/; keeping it",
                  file=sys.stderr)
            continue
        deleted = _git(["branch", "-D", branch])
        if deleted.returncode != 0:
            print(f"cannot delete completed local {branch}: {deleted.stderr.strip()}", file=sys.stderr)


def _pending_daily_branches(before: date) -> list[str]:
    """Return remote usage date branches older than *before*, oldest first."""
    result = _git([
        "for-each-ref",
        "--format=%(refname:strip=4)",
        "refs/remotes/origin/usage/",
    ])
    if result.returncode != 0:
        return []
    pending: list[tuple[date, str]] = []
    for name in result.stdout.splitlines():
        try:
            branch_date = date.fromisoformat(name)
        except ValueError:
            continue
        if branch_date < before:
            pending.append((branch_date, f"{DAILY_BRANCH_PREFIX}{name}"))
    return [name for _, name in sorted(pending)]


def _request_rollover_recovery(pending: list[str]) -> bool:
    """Ask GitHub to run rollover when the external writer detects it was missed."""
    try:
        result = subprocess.run(
            ["gh", "workflow", "run", "daily-rollover.yml", "--ref", "main"],
            cwd=usage_schema.DATA_REPO_DIR,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"cannot request rollover recovery for {pending}: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            f"cannot request rollover recovery for {pending}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    print(f"requested rollover recovery for {', '.join(pending)}", file=sys.stderr)
    return True


def prepare_daily_branch(today: date, *, recover_missed_rollover: bool = False) -> bool:
    """Switch to today's shared branch, creating it only from a settled main.

    The writer must never create today's branch while yesterday still exists:
    that would fork from main before yesterday's final data was merged. If the
    rollover job is delayed, this run simply defers; cumulative local logs make
    the next 15-minute run catch up without losing usage.
    """
    if _branch_is_ahead() and not _try_push():
        print("current branch is still ahead of origin; deferring branch rollover",
              file=sys.stderr)
        return False
    dirty = _git(["status", "--porcelain"])
    if dirty.returncode != 0 or dirty.stdout.strip():
        print("working tree is dirty before daily branch switch; deferring sync",
              file=sys.stderr)
        return False

    fetched = _git(["fetch", "--prune", "origin"])
    if fetched.returncode != 0:
        print(f"git fetch failed; deferring sync: {fetched.stderr.strip()}", file=sys.stderr)
        return False
    _sync_local_main()

    branch = f"{DAILY_BRANCH_PREFIX}{today.isoformat()}"
    remote_ref = f"refs/remotes/origin/{branch}"
    local_ref = f"refs/heads/{branch}"
    if _ref_exists(remote_ref):
        if _current_branch() != branch:
            if _ref_exists(local_ref):
                switched = _git(["switch", branch])
            else:
                switched = _git(["switch", "--track", "-c", branch, f"origin/{branch}"])
            if switched.returncode != 0:
                print(f"cannot switch to {branch}: {switched.stderr.strip()}", file=sys.stderr)
                return False
        tracking = _git(["branch", "--set-upstream-to", f"origin/{branch}", branch])
        if tracking.returncode != 0:
            print(f"cannot track origin/{branch}: {tracking.stderr.strip()}", file=sys.stderr)
            return False
        _cleanup_completed_local_branches(today)
        return True

    pending = _pending_daily_branches(today)
    if pending:
        if recover_missed_rollover:
            _request_rollover_recovery(pending)
        print(f"{branch} is not ready; waiting for {', '.join(pending)} to roll into main",
              file=sys.stderr)
        return False

    # Bootstrap or recover a missed schedule only after every earlier day
    # disappeared, which means origin/main is the safe base for the new day.
    if _current_branch() != branch:
        if _ref_exists(local_ref):
            switched = _git(["switch", branch])
        else:
            switched = _git(["switch", "-c", branch, "origin/main"])
        if switched.returncode != 0:
            print(f"cannot create {branch}: {switched.stderr.strip()}", file=sys.stderr)
            return False
    pushed = _git(["push", "-u", "origin", branch])
    if pushed.returncode == 0:
        _cleanup_completed_local_branches(today)
        return True

    # Another machine may have won the create race. Track and rebase onto it.
    race_refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    if _git(["fetch", "origin", race_refspec]).returncode == 0:
        tracked = _git(["branch", "--set-upstream-to", f"origin/{branch}", branch])
        if tracked.returncode != 0:
            # Without tracking, this run can still commit but nothing can push it,
            # and the failure would surface later as an unrelated push error.
            print(f"cannot track origin/{branch} after the create race: "
                  f"{tracked.stderr.strip()}", file=sys.stderr)
            return False
        # The refspec above already updated the one ref we care about, so rebase
        # straight onto it rather than going back through FETCH_HEAD.
        rebased = _git(["rebase", "--autostash", f"origin/{branch}"])
        if rebased.returncode == 0:
            _cleanup_completed_local_branches(today)
            return True
        # Report why the rebase failed before the push error below, which by then
        # describes a race we already know we lost rather than what went wrong here.
        print(f"lost the {branch} create race and could not rebase onto the winner: "
              f"{rebased.stderr.strip()}", file=sys.stderr)
    print(f"cannot publish {branch}: {pushed.stderr.strip()}", file=sys.stderr)
    return False


def git_push(machine: str) -> None:
    """Best-effort commit + push of this machine's subdirectory.

    Order matters: if a prior run committed locally but failed to push, the
    branch is ahead of upstream with nothing new to stage. Retrying the push
    *before* the staged-diff check ensures that commit eventually reaches the
    remote on a later tick even if no new ccusage data shows up in between.
    """
    if _branch_is_ahead():
        _try_push()  # republish stranded commit; continue regardless of result

    paths = [machine + "/"]
    # The Multica aggregate sits outside every machine directory because it
    # describes the workspace rather than one machine; stage it only when its
    # designated writer is the one committing.
    if Path(machine).name == usage_schema.MULTICA_TASK_WRITER and (
        usage_schema.DATA_REPO_DIR / usage_schema.MULTICA_TASK_STORE
    ).exists():
        paths.append(usage_schema.MULTICA_TASK_STORE)
    add = _git(["add", *paths])
    if add.returncode != 0:
        print(f"git add failed: {add.stderr.strip()}", file=sys.stderr)
        return
    if _git(["diff", "--cached", "--quiet"]).returncode == 0:
        return  # nothing new to commit
    msg = usage_commit_message(machine)
    commit = git_commit(msg)
    if commit.returncode != 0:
        print(f"git commit failed: {commit.stderr.strip()}", file=sys.stderr)
        return
    _try_push()


def usage_commit_message(machine: str) -> str:
    """Return the stable Conventional Commit subject used by data writers."""
    return f"chore(data): sync {Path(machine).name} usage"
