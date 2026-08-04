#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Canonical per-machine data producer for Aether Ledger.

Reads this machine's local ccusage usage data, persists daily totals into
`data/<machine>/claude.json`, `data/<machine>/codex.json`, and
`data/<machine>/opencode.json`
under this repo, and commits + pushes the result.

Every machine that contributes data runs this script (or its launchd wrapper).
Sync-only machines only need to clone the data repo — they do NOT need the
feishu-claude-usage code repo. That repo's update_signature.py is the Feishu
pusher only; it reads from here but never writes here.

Machine identity:
  - Durable machines read ~/.config/token-activity/node_name. The allowed
    public role labels are `work`, `personal`, and `devbox`.
  - Ephemeral workers set CC_USAGE_TRAIL=1. Only a SHA-256-derived opaque ID is
    stored below data/trail/, so the hostname never appears in Git paths. Each worker
    owns one folder and concurrent workers sum instead of clobbering via the
    per-folder max() rotation guard. compact_trails.py later folds expired
    workers into data/trail/rollup and prunes them.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from render_dashboard import SHANGHAI

# Data repo root: two levels up from this script (repo/scripts/sync_usage.py)
DATA_REPO_DIR = Path(__file__).resolve().parents[1]

CONFIG_DIR = Path.home() / ".config" / "token-activity"
NODE_NAME_FILE = CONFIG_DIR / "node_name"
DURABLE_NODES = frozenset({"work", "personal", "devbox"})
ROLLOVER_WATCHDOG_NODES = frozenset({"work", "personal"})
NODE_ID_RE = re.compile(r"node-[0-9a-f]{12}")

# Set on ephemeral workers. "1"/"true"/"yes" uses the hostname; any other
# value is the raw worker identity. Only its SHA-256 digest enters the repo.
TRAIL_ENV = "CC_USAGE_TRAIL"


def _opaque_node_id(raw: str) -> str:
    return f"node-{hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:12]}"


def resolve_machine() -> str:
    """This machine's data-repo folder name.

    Ephemeral workers get a digest-only nested name under data/trail/, so hostnames
    and job identifiers never enter tracked paths. Durable machines use one of
    three intentionally public role labels.
    """
    trail = os.environ.get(TRAIL_ENV, "").strip()
    if trail:
        raw = socket.gethostname() if trail.lower() in ("1", "true", "yes") else trail
        return f"data/trail/{_opaque_node_id(raw)}"
    if not NODE_NAME_FILE.exists():
        sys.exit(
            f"missing {NODE_NAME_FILE}: write one of {sorted(DURABLE_NODES)}, "
            f"or set {TRAIL_ENV}=1 on an ephemeral worker"
        )
    node_name = NODE_NAME_FILE.read_text().strip().lower()
    if node_name not in DURABLE_NODES:
        sys.exit(f"invalid {NODE_NAME_FILE}: expected one of {sorted(DURABLE_NODES)}")
    return f"data/{node_name}"

# ccusage v20+ 的 `daily` 已合并所有 agent CLI（claude / codex / copilot / ...）
# 到一次调用。我们仍按 modelName 前缀把行内 modelBreakdowns 拆回 cc / cx 两个
# 本地 store，原因是 session rotation（>30d 清理）下，单文件 max() 会被部分
# rotation 拖低；分桶 max() 则各自独立守住历史峰值。
CCUSAGE_CMD = ["ccusage", "daily", "--json"]

# ccusage walks every session JSONL, so it is the slowest step here — and it runs
# while this process holds the checkout-wide Git lock. An unbounded hang would
# strand that lock for as long as launchd keeps the job alive, starving every
# other Git user in the checkout. Bound it and let the run fall back to the
# cached stores instead.
CCUSAGE_TIMEOUT_SECONDS = 180

# Earliest possible date for cumulative totals
EPOCH = date(2024, 1, 1)

# Git network ops hang forever on flaky DNS / TLS / proxy. 30s is enough for a
# small repo over any sane link.
GIT_TIMEOUT_SECONDS = 30

# This writer is not the only scheduled process running Git inside the checkout:
# the downstream Feishu signature pusher pulls the same working tree, and its
# launchd WatchPaths fire on the very data files written below — so it wakes up
# *while* this run is still mid-sync. Git has no cross-process lock for a working
# tree: every fetch truncates and rewrites .git/FETCH_HEAD, so two concurrent
# fetches leave two merge heads behind ("Cannot rebase onto multiple branches"),
# remote-ref updates lose their compare-and-swap ("cannot lock ref ... is at X
# but expected Y"), and a fetch can move the checked-out branch under the other
# process. One advisory lock file, shared by every Git user of this checkout,
# serializes them. Readers take it non-blocking and skip their pull instead of
# queueing; this writer waits, because skipping means losing an observation.
GIT_LOCK_PATH = Path.home() / ".cache" / "aether-ledger" / "git.lock"
GIT_LOCK_WAIT_SECONDS = 60

# Automated commits must be safe for public history regardless of the writer's
# global/local Git configuration (or an inherited GIT_AUTHOR_* environment).
AUTOMATION_GIT_NAME = "Aether Ledger"
AUTOMATION_GIT_EMAIL = "noreply@github.com"

# High-frequency writes accumulate on one date branch. The daily rollover
# workflow squash-merges it into main, so main grows by one commit per day.
DAILY_BRANCH_PREFIX = "usage/"

# Codex built-in image_gen (gpt-image-2): ccusage doesn't capture these because
# the session log records them as tool invocations, not LLM token_count events.
# We count PNGs by mtime as a proxy for billable image generations.
CODEX_IMAGE_GEN_DIR = Path.home() / ".codex" / "generated_images"


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------

def _classify_agent(model_name: str, row_agents: list[str] | None = None) -> str | None:
    """ccusage 模型名 → 来源 agent CLI。

    启发式:claude-* → Claude Code; gpt-* / codex-* → Codex CLI。
    ccusage daily 当前不给每个 model breakdown 的 invoking agent,只有当天出现过的
    agents 列表。所以若 opencode 调用了 claude/gpt 家族模型,仍会落进对应家族桶；
    opencode 桶只兜住其余模型族,避免这部分 usage 被静默漏掉。Feishu 签名只显示
    合计值,这个近似已经足够把此前未统计的 opencode usage 补进来。
    """
    if model_name.startswith("claude-"):
        return "claude"
    if model_name.startswith("gpt-") or model_name.startswith("codex-"):
        return "codex"
    if row_agents and "opencode" in row_agents:
        return "opencode"
    return None


def count_codex_image_files_per_day() -> dict[str, int]:
    """Count Codex-generated PNG files per local date (by mtime).

    Why: ccusage's codex daily JSON only captures LLM token_count events. The
    built-in image_gen tool (gpt-image-2) doesn't surface as a model, so its
    cost is silently dropped. PNGs on disk are 1:1 with billable generations.
    """
    counts: dict[str, int] = {}
    if not CODEX_IMAGE_GEN_DIR.exists():
        return counts
    for path in CODEX_IMAGE_GEN_DIR.rglob("*.png"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        d = datetime.fromtimestamp(mtime).date().isoformat()
        counts[d] = counts.get(d, 0) + 1
    return counts


def fetch_daily_since(since: date) -> tuple[list[dict], list[dict], list[dict]]:
    """一次 ccusage daily,按可辨识来源拆出 (cc_daily, cx_daily, op_daily)。

    v20+ schema:
      - 日期字段 `period`(v18 是 `date`)
      - 每行 modelBreakdowns[] 给到 per-model cost + 四类 tokens,但没有
        per-model totalTokens,需要把四类相加。
      - 行级 totalCost / totalTokens 是该日所有 agent 的合计,不能直接用。

    Codex 桶额外带:
      - filesystem-derived imageCount (ccusage 无法捕获 gpt-image-2 计费)
      - per-model {totalTokens} 映射,供下游 debug
      - raw API-equiv cost,/fast 乘数和 image_gen 价格留给 pusher 在展示侧应用。

    OpenCode/ttadk 桶只补非 claude/gpt/codex 家族的模型。因为 ccusage 暂无
    per-breakdown agent 字段,无法把使用 claude/gpt 家族模型的 opencode 调用从
    Claude Code / Codex CLI 中再精确拆出来。
    """
    out = subprocess.run(
        CCUSAGE_CMD + ["--since", since.strftime("%Y%m%d")],
        capture_output=True, text=True, check=True,
        timeout=CCUSAGE_TIMEOUT_SECONDS,
    )
    raw = json.loads(out.stdout).get("daily", [])
    fs_image_counts = count_codex_image_files_per_day()

    cc_daily: list[dict] = []
    cx_daily: list[dict] = []
    op_daily: list[dict] = []
    seen_codex_dates: set[str] = set()
    for row in raw:
        d = row["period"]
        row_agents = row.get("metadata", {}).get("agents", [])
        cc_tokens = 0
        cc_cost = 0.0
        cx_tokens = 0
        cx_cost = 0.0
        cx_models: dict[str, dict] = {}
        op_tokens = 0
        op_cost = 0.0
        op_models: dict[str, dict] = {}
        for m in row.get("modelBreakdowns", []):
            tokens = (
                m.get("inputTokens", 0)
                + m.get("outputTokens", 0)
                + m.get("cacheCreationTokens", 0)
                + m.get("cacheReadTokens", 0)
            )
            cost = m.get("cost", 0.0)
            agent = _classify_agent(m["modelName"], row_agents)
            if agent == "claude":
                cc_tokens += tokens
                cc_cost += cost
            elif agent == "codex":
                cx_tokens += tokens
                cx_cost += cost
                cx_models[m["modelName"]] = {"totalTokens": tokens}
            elif agent == "opencode":
                op_tokens += tokens
                op_cost += cost
                op_models[m["modelName"]] = {"totalTokens": tokens}
        if cc_tokens or cc_cost:
            cc_daily.append({
                "date": d, "totalTokens": cc_tokens, "totalCost": cc_cost,
            })
        if cx_tokens or cx_cost or cx_models:
            cx_daily.append({
                "date": d, "totalTokens": cx_tokens, "totalCost": cx_cost,
                "models": cx_models,
                "imageCount": fs_image_counts.get(d, 0),
            })
            seen_codex_dates.add(d)
        if op_tokens or op_cost or op_models:
            op_daily.append({
                "date": d, "totalTokens": op_tokens, "totalCost": op_cost,
                "models": op_models,
            })

    # Edge case: PNG 存在但 ccusage 那天的 session 已被 rotate — 补一个 stub 让
    # imageCount 仍能进入 cumulative store。
    for d, cnt in fs_image_counts.items():
        if d in seen_codex_dates:
            continue
        try:
            if date.fromisoformat(d) < since:
                continue
        except ValueError:
            continue
        cx_daily.append({
            "date": d, "totalTokens": 0, "totalCost": 0.0,
            "models": {}, "imageCount": cnt,
        })

    return cc_daily, cx_daily, op_daily


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def merge_with_cumulative(daily: list[dict], store_path: Path) -> list[dict]:
    """Upsert daily entries into a local store; return merged list sorted by date.

    Why: ccusage / @ccusage/codex both read session JSONLs that get rotated by
    their host CLIs (cleanupPeriodDays etc). Without persisting locally, the
    cumulative total silently shrinks each day.
    """
    store: dict[str, dict] = {}
    if store_path.exists():
        store = json.loads(store_path.read_text())
    for entry in daily:
        prev = store.get(entry["date"], {"totalTokens": 0, "totalCost": 0.0})
        # Keep whichever observation saw the MOST tokens, and carry ITS cost.
        # max() on TOKENS alone is what guards against ccusage's window shrinking
        # when the host CLIs rotate session JSONLs (cleanupPeriodDays) — deleted
        # usage must not vanish from the cumulative total. Cost must FOLLOW tokens,
        # not be max()'d independently: when the same tokens are merely repriced by
        # ccusage's price table (e.g. a vendor cut), we want the fresh recalculated
        # cost, not a stale high-water price. `>=` lets a same-token reprice flow
        # through; only a genuine token regression (rotation) freezes the old pair.
        if entry["totalTokens"] >= prev["totalTokens"]:
            merged = {"totalTokens": entry["totalTokens"], "totalCost": entry["totalCost"]}
        else:
            merged = {"totalTokens": prev["totalTokens"], "totalCost": prev["totalCost"]}
        # Carry per-model token breakdown when present (codex side only;
        # cc entries don't have this). Fresh fetch wins over stale stored copy.
        if entry.get("models"):
            merged["models"] = entry["models"]
        elif "models" in prev:
            merged["models"] = prev["models"]
        # imageCount: max() like other monotonic fields, so user/system cleanup
        # of ~/.codex/generated_images after imageCount was recorded doesn't lose data.
        new_img = entry.get("imageCount", 0)
        prev_img = prev.get("imageCount", 0)
        if new_img or prev_img:
            merged["imageCount"] = max(new_img, prev_img)
        store[entry["date"]] = merged
    _atomic_write_json(store_path, store)
    return [{"date": d, **v} for d, v in sorted(store.items())]


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via temp file + os.replace so a crash mid-write can't leave
    invalid JSON that blocks future runs.
    """
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
# Git helpers
# ---------------------------------------------------------------------------

@contextmanager
def repo_git_lock(wait_seconds: float) -> Iterator[bool]:
    """Hold the checkout-wide Git lock, yielding whether it was acquired.

    An advisory flock is enough because every process that runs Git here is ours.
    The lock is released by the kernel on exit, so a crashed run cannot strand it.

    Only contention (BlockingIOError) is retried. Any other OSError means locking
    itself is broken here — an unsupported filesystem, a bad descriptor — and is
    raised rather than silently reported as a busy peer.
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
            ["git", *args], cwd=DATA_REPO_DIR,
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


def git_pull() -> None:
    """Best-effort pull. --autostash is free insurance against unexpected dirt."""
    r = _git(["pull", "--rebase", "--autostash"])
    if r.returncode != 0:
        print(f"git pull failed (continuing with local state): {r.stderr.strip()}",
              file=sys.stderr)


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
    rebase = _git(["pull", "--rebase", "--autostash"])
    if rebase.returncode != 0:
        print(f"git push race-retry rebase failed: {rebase.stderr.strip()}", file=sys.stderr)
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
    and is deleted with its history reachable only through the reflog. That is the
    accepted trade: squash-merged branches leave no ancestry to test instead.
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
            cwd=DATA_REPO_DIR,
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
        _git(["branch", "--set-upstream-to", f"origin/{branch}", branch])
        rebased = _git(["pull", "--rebase", "--autostash"])
        if rebased.returncode == 0:
            _cleanup_completed_local_branches(today)
            return True
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

    add = _git(["add", machine + "/"])
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync this machine's ccusage data into the data repo and push."
    )
    parser.add_argument(
        "--no-push", action="store_true",
        help="update local data without switching branches, committing, or pushing",
    )
    args = parser.parse_args()

    machine = resolve_machine()

    # Held across the whole run — ccusage included — because the data files this
    # writes are what wake the pusher up. Releasing early would just hand it the
    # window we are still committing in.
    with repo_git_lock(GIT_LOCK_WAIT_SECONDS) as acquired:
        if not acquired:
            print(
                f"another process has held {GIT_LOCK_PATH} for "
                f"{GIT_LOCK_WAIT_SECONDS}s; skipping this run",
                file=sys.stderr,
            )
            return 0
        return _sync(machine, no_push=args.no_push)


def _sync(machine: str, *, no_push: bool) -> int:
    now = datetime.now(SHANGHAI)
    today = now.date()
    recovery_grace_elapsed = (now.hour, now.minute) >= (0, 50)
    recover_missed_rollover = (
        Path(machine).name in ROLLOVER_WATCHDOG_NODES and recovery_grace_elapsed
    )
    if not no_push and not prepare_daily_branch(
        today,
        recover_missed_rollover=recover_missed_rollover,
    ):
        return 0

    machine_dir = DATA_REPO_DIR / machine
    cc_path = machine_dir / "claude.json"
    codex_path = machine_dir / "codex.json"
    opencode_path = machine_dir / "opencode.json"
    machine_dir.mkdir(parents=True, exist_ok=True)

    # Pull first so we rebase cleanly onto the other machine's commits.
    git_pull()

    # Update only THIS machine's per-agent files from its own local ccusage state.
    try:
        cc_daily, cx_daily, op_daily = fetch_daily_since(EPOCH)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
    ) as e:
        # ccusage fetch failing (binary missing, malformed output, hung run) must
        # not blank out either store — merging [] keeps existing rows via max().
        print(f"ccusage fetch failed, keeping cached stores: {e}", file=sys.stderr)
        cc_daily, cx_daily, op_daily = [], [], []
    merge_with_cumulative(cc_daily, cc_path)
    merge_with_cumulative(cx_daily, codex_path)
    merge_with_cumulative(op_daily, opencode_path)

    if not no_push:
        git_push(machine)

    return 0


if __name__ == "__main__":
    sys.exit(main())
