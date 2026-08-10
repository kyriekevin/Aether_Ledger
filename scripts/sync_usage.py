#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Canonical per-machine data producer for Aether Ledger.

Reads this machine's local ccusage usage data, persists daily totals into
`data/<machine>/claude.json`, `data/<machine>/codex.json`,
`data/<machine>/opencode.json`, and `data/<machine>/traex.json`
under this repo, and commits + pushes the result.

Every machine that contributes data runs this script (or its launchd wrapper).
Sync-only machines only need to clone the data repo — they do NOT need the
feishu-claude-usage code repo. That repo's update_signature.py is the Feishu
pusher only; it reads from here but never writes here.

Machine identity:
  - Durable machines read ~/.config/token-activity/node_name. The allowed
    public role labels are `work`, `personal`, and `devbox`.
  - Ephemeral workers set CC_USAGE_TRAIL=1 and mint an opaque ID once, kept in
    ~/.config/token-activity/trail_id, so nothing about the host enters Git paths.
    Each worker owns one folder and concurrent workers sum instead of clobbering
    via the per-folder max() rotation guard. compact_trails.py later folds expired
    workers into data/trail/rollup and prunes them.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from pricing import (
    active_rate,
    ccusage_config_file,
    load_pricing,
    official_cost_from_ccusage,
    standard_cost,
    token_breakdown,
)
from render_dashboard import SHANGHAI

# Data repo root: two levels up from this script (repo/scripts/sync_usage.py)
DATA_REPO_DIR = Path(__file__).resolve().parents[1]

CONFIG_DIR = Path.home() / ".config" / "token-activity"
NODE_NAME_FILE = CONFIG_DIR / "node_name"
DURABLE_NODES = frozenset({"work", "personal", "devbox"})
ROLLOVER_WATCHDOG_NODES = frozenset({"work", "personal"})
NODE_ID_RE = re.compile(r"node-[0-9a-f]{12}")

# Set on ephemeral workers. "1"/"true"/"yes" mints and reuses a local ID; any other
# value is the raw worker identity. Only its SHA-256 digest enters the repo.
TRAIL_ENV = "CC_USAGE_TRAIL"
# Where the minted ID lives. Its whole job is to outlive a hostname change.
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

# ccusage v20.0.15+ 的 `daily --by-agent` 一次读取所有 agent CLI（claude / codex /
# opencode / ...），并在每日行的 `agents` 中保留准确的调用端归属。模型名只作为
# 明细保留，不能用于推断 agent：Claude Code 经代理或 cc-switch 路由后可能记录
# 任意模型族。各 agent 仍写入独立 store，使 session rotation 下的高水位彼此隔离。
CCUSAGE_CMD = ["ccusage", "daily", "--json", "--by-agent"]

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

# TRAE CLI (traex) is a Codex fork: it writes the same rollout-*.jsonl session
# format, only under ~/.trae/cli instead of ~/.codex. ccusage has no `trae` agent,
# but its `codex` reader honours CODEX_HOME, so pointing it at ~/.trae/cli reads
# traex's sessions with zero effect on the real ~/.codex tree. This is a separate,
# READ-ONLY invocation kept fully apart from the Codex path so the two never mix:
# traex lands in its own data/<machine>/traex.json store.
#
# Token counts come back accurate. Cost uses the repository-owned official table;
# unknown models deliberately contribute zero. Two naming mismatches are fixed by
# normalising the
# `"model"` field in a throwaway mirror before ccusage reads it (see
# _lowercased_codex_home / _normalise_model):
#   1. Case. The lookup is case-sensitive against lowercase slugs, but TRAE CLI
#      logs names capitalised (GPT-5.5, Gemini-3-Flash-Preview, DeepSeek-V4-Pro),
#      so every real-name model priced to nothing until lowercased.
#   2. Opaque Claude aliases. TRAE CLI fronts Anthropic models under anonymised
#      slugs (openrouter-1o/2o/3o, incl. __max variants) that carry no vendor
#      root, so ccusage's substring match never finds them. _ALIAS_PREFIXES maps
#      each to the real Opus slug it stands for.
# What stays unpriced: any model absent from config/official-pricing.json, including
# vendor-internal slugs whose real model and official rate are unknown. They bill
# zero until scripts/update_pricing.py can resolve an official row.
TRAEX_CODEX_HOME = Path.home() / ".trae" / "cli"
# ccusage `codex daily` (unlike the unified `daily`) reports per-model tokens but
# only a row-level costUSD, and dates under key `date`, not `period`.
CCUSAGE_CODEX_CMD = ["ccusage", "codex", "daily", "--json"]

# Session lines record the model as `"model":"<name>"`. We rewrite only that field
# when mirroring, leaving every other byte untouched.
_SESSION_MODEL_RE = re.compile(r'("model"\s*:\s*")([^"]+)(")')

# Anonymised TRAE CLI aliases → the real Anthropic slug each fronts. Matched by
# prefix so suffixed variants (openrouter-3o__max) resolve to the same model, and
# so a rewrite lands whatever tier string follows. Families are disjoint, order
# does not matter.
_ALIAS_PREFIXES = (
    ("openrouter-3o", "claude-opus-4-8"),
    ("openrouter-2o", "claude-opus-4-7"),
    ("openrouter-1o", "claude-opus-4-6"),
)

# Vendor-internal slugs we knowingly leave unpriced because no official rate is
# pinned. Listed so a new one shows up in the log rather than billing zero unseen.
# A leftover `openrouter-` here means our alias map missed a variant.
_KNOWN_UNPRICED_PREFIXES = ("openrouter-", "seed-", "doubao-", "qwen-")


def _normalise_model(name: str) -> str:
    """Lowercase a session model name and resolve known opaque aliases.

    Lowercasing alone lets ccusage's case-sensitive lookup price every real-name
    model (GPT-5.x, Gemini, DeepSeek). Aliases in _ALIAS_PREFIXES carry no vendor
    root for the substring match to catch, so they are additionally rewritten to
    the real slug they front before the (already lowercased) name is returned.
    """
    lowered = name.lower()
    for prefix, real in _ALIAS_PREFIXES:
        if lowered.startswith(prefix):
            return real
    return lowered


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------

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


def _breakdown_tokens(m: dict) -> int:
    """Total tokens on one modelBreakdowns entry; ccusage gives no rolled-up field."""
    return (
        m.get("inputTokens", 0)
        + m.get("outputTokens", 0)
        + m.get("cacheCreationTokens", 0)
        + m.get("cacheReadTokens", 0)
    )


def fetch_daily_since(since: date) -> tuple[list[dict], list[dict], list[dict]]:
    """一次 ccusage daily --by-agent，拆出 (cc_daily, cx_daily, op_daily)。

    v20+ schema:
      - 日期字段 `period`(v18 是 `date`)
      - 每日行 `agents[]` 按实际调用端给出独立的 modelBreakdowns；模型名称
        不参与 agent 归类，因此 Claude Code 路由到非 Claude 模型也不会漏记。
      - 每个 modelBreakdowns[] 给到 per-model cost + 四类 tokens,但没有
        per-model totalTokens,需要把四类相加。

    Codex 桶额外带:
      - filesystem-derived imageCount (ccusage 无法捕获 gpt-image-2 计费)
      - per-model {totalTokens} 映射,供下游 debug
      - raw API-equiv cost,/fast 乘数和 image_gen 价格留给 pusher 在展示侧应用。

    其余 ccusage 支持的 agent 不属于本仓库的三个 store，保持忽略。
    """
    config_day = datetime.now(SHANGHAI).date()
    pricing = load_pricing()
    with ccusage_config_file(config_day) as config_path:
        out = subprocess.run(
            CCUSAGE_CMD + [
                "--offline", "--config", str(config_path),
                "--since", since.strftime("%Y%m%d"),
            ],
            capture_output=True, text=True, check=True,
            timeout=CCUSAGE_TIMEOUT_SECONDS,
        )
    raw = json.loads(out.stdout).get("daily", [])
    fs_image_counts = count_codex_image_files_per_day()

    cc_daily: list[dict] = []
    cx_daily: list[dict] = []
    op_daily: list[dict] = []
    seen_codex_dates: set[str] = set()
    destinations = {"claude": cc_daily, "codex": cx_daily, "opencode": op_daily}
    for row in raw:
        d = row["period"]
        for agent_row in row.get("agents", []):
            agent = agent_row.get("agent")
            destination = destinations.get(agent)
            if destination is None:
                continue
            tokens = 0
            cost = 0.0
            models: dict[str, dict] = {}
            fully_priced = True
            usage_day = date.fromisoformat(d)
            for m in agent_row.get("modelBreakdowns", []):
                model_tokens = _breakdown_tokens(m)
                breakdown = token_breakdown(m)
                model_name = m["modelName"]
                tokens += model_tokens
                if model_tokens and active_rate(model_name, usage_day, pricing) is None:
                    fully_priced = False
                cost += official_cost_from_ccusage(
                    model_name, usage_day, config_day, breakdown,
                    m.get("cost", 0.0), pricing,
                )
                models[model_name] = {"totalTokens": model_tokens, **breakdown}
            if not (tokens or cost or models):
                continue
            entry = {
                "date": d, "totalTokens": tokens, "totalCost": cost,
                "models": models, "costTrusted": True,
                "costSource": "official" if fully_priced else "unpriced",
            }
            if agent == "codex":
                entry["imageCount"] = fs_image_counts.get(d, 0)
                seen_codex_dates.add(d)
            destination.append(entry)

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
            # Says nothing about that day's tokens — ccusage no longer has the
            # session at all. Only imageCount is real, so reconciliation, which
            # otherwise lets a fetch overwrite history downward, must not read
            # these zeros as "the day turned out to be empty".
            "tokensObserved": False,
        })

    return cc_daily, cx_daily, op_daily


@contextmanager
def _lowercased_codex_home(source: Path) -> Iterator[Path]:
    """Yield a throwaway CODEX_HOME whose session model names are normalised.

    ccusage reads the session JSONLs itself and prices them internally, so we
    cannot fix a model name after the fact — the only lever we have is the bytes
    ccusage reads. This mirrors `source/sessions` into a tempdir, rewriting just
    the `"model":"…"` field on every line via _normalise_model (lowercase +
    alias resolution), and hands back the tempdir root for use as CODEX_HOME. The
    real `source` tree is never written.

    A source with no sessions (a machine that never ran traex) yields an empty
    mirror, which ccusage reports as no usage — the same as pointing it straight at
    the empty tree. The tempdir is removed on exit regardless.
    """
    sessions = source / "sessions"
    with tempfile.TemporaryDirectory(prefix="traex-lc-") as tmp:
        mirror_root = Path(tmp)
        if sessions.is_dir():
            mirror_sessions = mirror_root / "sessions"
            for src in sessions.rglob("*.jsonl"):
                dest = mirror_sessions / src.relative_to(sessions)
                dest.parent.mkdir(parents=True, exist_ok=True)
                text = src.read_text(encoding="utf-8")
                dest.write_text(
                    _SESSION_MODEL_RE.sub(
                        lambda m: m.group(1) + _normalise_model(m.group(2)) + m.group(3),
                        text,
                    ),
                    encoding="utf-8",
                )
        yield mirror_root


def fetch_codex_home_daily(
    since: date, codex_home: Path, *, lowercase_models: bool = False
) -> list[dict]:
    """`ccusage codex daily` against an alternate CODEX_HOME → per-day entries.

    Used for traex, whose sessions live under ~/.trae/cli in the very format the
    Codex reader expects. The invocation is read-only and touches nothing under
    the real ~/.codex; we only override CODEX_HOME for this one child process.

    `lowercase_models` runs ccusage against a normalised mirror of the sessions
    (see _lowercased_codex_home) so TRAE CLI's capitalised model names match
    ccusage's case-sensitive lowercase price keys and its opaque Claude aliases
    (openrouter-*) resolve to real Opus slugs. Without it every real-name model
    (GPT-5.x, Gemini, DeepSeek) and every alias prices to nothing.

    The `codex daily` schema differs from the unified `daily` fetch_daily_since
    parses: dates arrive under `date` (not `period`), each row carries per-model
    {totalTokens, …} plus a single row-level `costUSD` (there is no per-model
    cost), and every model here is already a Codex-family model, so no agent
    classification is needed — the whole row is one bucket.

    ccusage's row-level costUSD is ignored here because it cannot separate known
    official models from unknown aliases on mixed days. The per-model token buckets
    are priced directly from config/official-pricing.json; traex currently records
    no Fast tier, so this path uses the official standard rate. Unknown models are
    deliberately zero and logged when they match a known internal-slug family.
    """
    if lowercase_models:
        with _lowercased_codex_home(codex_home) as mirror:
            return fetch_codex_home_daily(since, mirror)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    config_day = datetime.now(SHANGHAI).date()
    pricing = load_pricing()
    with ccusage_config_file(config_day) as config_path:
        out = subprocess.run(
            CCUSAGE_CODEX_CMD + [
                "--offline", "--config", str(config_path),
                "--since", since.strftime("%Y%m%d"),
            ],
            capture_output=True, text=True, check=True,
            timeout=CCUSAGE_TIMEOUT_SECONDS, env=env,
        )
    raw = json.loads(out.stdout).get("daily", [])
    daily: list[dict] = []
    for row in raw:
        d = row["date"]
        tokens = row.get("totalTokens", 0)
        cost = 0.0
        models = {}
        fully_priced = True
        usage_day = date.fromisoformat(d)
        for name, raw_model in row.get("models", {}).items():
            breakdown = token_breakdown(raw_model)
            model_tokens = raw_model.get("totalTokens", sum(breakdown.values()))
            models[name] = {"totalTokens": model_tokens, **breakdown}
            canonical = _normalise_model(name)
            if model_tokens and (
                active_rate(canonical, usage_day, pricing) is None
                or sum(breakdown.values()) != model_tokens
            ):
                fully_priced = False
            cost += standard_cost(canonical, usage_day, breakdown, pricing)
        # Tokens are accurate regardless of whether the official table knows the
        # model. Unknown shares remain visible in models and contribute zero cost.
        if tokens or models:
            daily.append({
                "date": d, "totalTokens": tokens, "totalCost": cost,
                "models": models, "costTrusted": True,
                "costSource": "official" if fully_priced else "unpriced",
            })
    # Surface slugs we knowingly leave unpriced (Seed/Doubao/Qwen) so a new one
    # gets noticed rather than silently billing zero. A leftover openrouter-* here
    # means _ALIAS_PREFIXES missed a variant and needs a new entry. Model names in
    # `daily` are already normalised on the mirror path, so this only fires for
    # slugs the normaliser did not resolve.
    unpriced_days: dict[str, int] = {}
    for entry in daily:
        for name in entry["models"]:
            if name.startswith(_KNOWN_UNPRICED_PREFIXES):
                unpriced_days[name] = unpriced_days.get(name, 0) + 1
    if unpriced_days:
        print(
            "traex left unpriced (no official price entry, counted at zero): "
            + ", ".join(f"{name} ({days}d)" for name, days in sorted(unpriced_days.items())),
            file=sys.stderr,
        )
    return daily


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _canonical_model_totals(models: dict) -> dict[str, dict]:
    """Canonicalize one observation before comparing it with another."""
    canonical: dict[str, dict] = {}
    for name, payload in models.items():
        model = _normalise_model(name)
        tokens = (
            payload.get("totalTokens", 0) if isinstance(payload, dict) else payload
        )
        bucket = canonical.setdefault(model, {"totalTokens": 0})
        bucket["totalTokens"] += tokens
    return canonical


def merge_with_cumulative(
    daily: list[dict], store_path: Path, *, reconcile_since: date | None = None
) -> list[dict]:
    """Upsert daily entries into a local store; return merged list sorted by date.

    Why: ccusage / @ccusage/codex both read session JSONLs that get rotated by
    their host CLIs (cleanupPeriodDays etc). Without persisting locally, the
    cumulative total silently shrinks each day.

    `reconcile_since` lifts that high-water rule for dates on or after it, so the
    fetch wins even when it counts fewer tokens. See main() for when to reach for
    it — never on the scheduled path.
    """
    store: dict[str, dict] = {}
    if store_path.exists():
        store = json.loads(store_path.read_text())
    for entry in daily:
        prev = store.get(entry["date"], {"totalTokens": 0, "totalCost": 0.0})
        # An entry that never observed tokens (the image-only stub built for a day
        # whose session ccusage has already rotated away) carries zeros that mean
        # "unknown", not "none". Reconciliation is the one path that would write
        # them over real stored usage, so it never applies to those entries.
        reconciling = (
            reconcile_since is not None
            and entry.get("tokensObserved", True)
            and date.fromisoformat(entry["date"]) >= reconcile_since
        )
        # Keep whichever observation saw the MOST tokens, and carry ITS cost.
        # max() on TOKENS alone is what guards against ccusage's window shrinking
        # when the host CLIs rotate session JSONLs (cleanupPeriodDays) — deleted
        # usage must not vanish from the cumulative total. Cost must FOLLOW tokens,
        # not be max()'d independently. `>=` lets legacy, unmarked observations
        # refresh; official-to-official same-token observations are frozen below.
        # Only a genuine token regression (rotation) freezes the old pair here.
        take_entry = reconciling or entry["totalTokens"] >= prev["totalTokens"]
        if take_entry:
            merged = {"totalTokens": entry["totalTokens"], "totalCost": entry["totalCost"]}
            cost_source = entry.get("costSource")
        else:
            merged = {"totalTokens": prev["totalTokens"], "totalCost": prev["totalCost"]}
            cost_source = prev.get("costSource")
        # Once the stored observation uses this repository's official table, an
        # unchanged token high-water mark is immutable. A later incomplete price
        # lookup must not downgrade it to `unpriced`; --reconcile-since remains
        # the explicit operator path for intentional historical corrections.
        if (
            not reconciling
            and entry["totalTokens"] == prev["totalTokens"]
            and prev.get("costSource") == "official"
        ):
            merged["totalCost"] = prev["totalCost"]
            cost_source = "official"
        # Legacy callers may still provide the old trust marker. Preserve their
        # last known positive cost when an incomplete price lookup returns zero or
        # a known partial sum. Repository-owned pricing marks known rates
        # `official` and intentionally records missing rates as `unpriced` zero.
        if not reconciling and prev["totalCost"] and entry.get("costSource") is None and (
            not entry.get("costTrusted", True) or not merged["totalCost"]
        ):
            merged["totalCost"] = prev["totalCost"]
            cost_source = prev.get("costSource")
        # Carry per-model token breakdown when present (claude/codex/opencode all
        # supply it now). Each model needs the same high-water protection as the
        # row total: session rotation can shrink one model even while another
        # grows enough for the fresh row total to win.
        # While reconciling the fetch is authoritative even when it breaks the day
        # down into nothing, so an emptied map must not leave the old one behind
        # describing totals that no longer exist.
        if reconciling and "models" in entry:
            merged["models"] = _canonical_model_totals(entry["models"])
        elif entry.get("models"):
            merged_models = _canonical_model_totals(prev.get("models", {}))
            current_models = _canonical_model_totals(entry["models"])
            for model, current in current_models.items():
                previous = merged_models.get(model)
                current_tokens = current["totalTokens"]
                previous_tokens = previous["totalTokens"] if previous else 0
                if current_tokens >= previous_tokens:
                    merged_models[model] = current
            merged["models"] = merged_models
        elif "models" in prev:
            merged["models"] = _canonical_model_totals(prev["models"])
        if not reconciling and merged.get("models"):
            # Historical stores may lack some model detail, so the breakdown can
            # remain smaller than the row total. It must never exceed that total:
            # component high waters are a stronger lower bound when different
            # models rotate out and grow between observations.
            model_sum = sum(
                model["totalTokens"] for model in merged["models"].values()
            )
            merged["totalTokens"] = max(merged["totalTokens"], model_sum)
        if cost_source:
            merged["costSource"] = cost_source
        # imageCount: max() like other monotonic fields, so user/system cleanup
        # of ~/.codex/generated_images after imageCount was recorded doesn't lose data.
        new_img = entry.get("imageCount", 0)
        prev_img = prev.get("imageCount", 0)
        if new_img or prev_img:
            merged["imageCount"] = max(new_img, prev_img)
        store[entry["date"]] = merged
    if reconcile_since is not None:
        # Reconciliation can only correct days the fetch still returns. A day it
        # dropped entirely is either usage the upgrade folded away or a session
        # that simply rotated out of ccusage's window, and nothing in the fetch
        # tells those apart — so keep the stored value, which is the safe reading,
        # and name the days so the operator can judge them.
        observed = {e["date"] for e in daily if e.get("tokensObserved", True)}
        stale = sorted(
            d for d, v in store.items()
            if date.fromisoformat(d) >= reconcile_since
            and d not in observed
            and v.get("totalTokens", 0)
        )
        if stale:
            print(
                f"{store_path.name}: {len(stale)} day(s) on or after {reconcile_since} "
                f"were not in this fetch and keep their stored value: "
                f"{', '.join(stale)}",
                file=sys.stderr,
            )
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
              f"  If it stopped part-way, {DATA_REPO_DIR} stays parked until a human "
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
    parser.add_argument(
        "--reconcile-since", metavar="YYYY-MM-DD", type=date.fromisoformat,
        help="accept LOWER token counts from this date on (run by hand after a "
             "ccusage upgrade changes how usage is counted; never on a schedule)",
    )
    args = parser.parse_args()

    print("sync run started", flush=True)
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
            print("sync run finished status=lock-busy", flush=True)
            return 0
        status = _sync(machine, no_push=args.no_push,
                       reconcile_since=args.reconcile_since)
        print(f"sync run finished exit={status}", flush=True)
        return status


def _sync(machine: str, *, no_push: bool, reconcile_since: date | None = None) -> int:
    now = datetime.now(SHANGHAI)
    today = now.date()
    recovery_grace_elapsed = (now.hour, now.minute) >= (0, 50)
    recover_missed_rollover = (
        Path(machine).name in ROLLOVER_WATCHDOG_NODES and recovery_grace_elapsed
    )
    if not no_push:
        starting_branch = _current_branch()
        if not prepare_daily_branch(
            today,
            recover_missed_rollover=recover_missed_rollover,
        ):
            return 0
        if _current_branch() != starting_branch:
            print(
                "daily branch changed; deferring usage fetch until the next run",
                file=sys.stderr,
            )
            return 0

    machine_dir = DATA_REPO_DIR / machine
    cc_path = machine_dir / "claude.json"
    codex_path = machine_dir / "codex.json"
    opencode_path = machine_dir / "opencode.json"
    traex_path = machine_dir / "traex.json"
    machine_dir.mkdir(parents=True, exist_ok=True)

    # Rebase onto the other machines' commits before writing, so the push at the
    # end is a fast-forward. prepare_daily_branch fetched moments ago on this same
    # path, so this only needs the rebase half; when it was skipped there is no
    # push to keep clear of, and staying on local refs saves a round trip under
    # the lock.
    #
    # Falling behind is survivable: a catch-up failure here is logged and the run
    # continues against local state, same as every other caller of git_catch_up.
    if not no_push:
        git_catch_up(fetch=False)

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
    # traex (TRAE CLI) is read from its own CODEX_HOME in a separate invocation, so
    # its failure is isolated: an empty fetch merges [] and keeps the cached store
    # via max(), exactly like the Codex path above. A machine that never ran traex
    # simply gets an empty tree here and writes nothing.
    try:
        tx_daily = fetch_codex_home_daily(
            EPOCH, TRAEX_CODEX_HOME, lowercase_models=True
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
    ) as e:
        print(f"traex ccusage fetch failed, keeping cached store: {e}", file=sys.stderr)
        tx_daily = []
    # Reconciling rewrites history downward, so it must not run against an empty
    # read. Unknown-model zeroes are intentional under the official table.
    if reconcile_since is not None:
        # Entries that never observed tokens do not count as a read: a fetch made
        # only of image stubs looks non-empty and knows nothing about usage.
        if not [e for e in (*cc_daily, *cx_daily, *op_daily, *tx_daily)
                if e.get("tokensObserved", True)]:
            print("nothing fetched; refusing to reconcile against an empty read",
                  file=sys.stderr)
            return 1
        print(f"reconciling {reconcile_since} onward: the fetch wins even where it "
              "counts fewer tokens", file=sys.stderr)

    merge_with_cumulative(cc_daily, cc_path, reconcile_since=reconcile_since)
    merge_with_cumulative(cx_daily, codex_path, reconcile_since=reconcile_since)
    merge_with_cumulative(op_daily, opencode_path, reconcile_since=reconcile_since)
    merge_with_cumulative(tx_daily, traex_path, reconcile_since=reconcile_since)

    if not no_push:
        git_push(machine)

    return 0


if __name__ == "__main__":
    sys.exit(main())
