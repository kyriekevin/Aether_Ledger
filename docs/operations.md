# Operations

[English](operations.md) | [简体中文](operations.zh-CN.md)

## Sources and retention

Each writer reads local usage through `ccusage daily --json` and classifies model breakdowns into
Claude, Codex, and OpenCode stores. Codex `image_gen` PNGs are counted separately by local file
mtime because they do not appear as LLM token events.

TRAE CLI (traex) is a Codex fork that writes the same `rollout-*.jsonl` session format under
`~/.trae/cli` instead of `~/.codex`. `ccusage` has no `trae` agent, but its `codex` reader honours
`CODEX_HOME`, so each writer runs a second, read-only `ccusage codex daily` with
`CODEX_HOME=~/.trae/cli` and records the result in a separate `traex.json` store. That invocation
never touches the real `~/.codex` tree, so Codex usage and the Codex CLI itself are unaffected.
`ccusage`'s price lookup is case-sensitive against lowercase slugs, but TRAE CLI logs capitalised
model names (`GPT-5.5`, `Gemini-3-Flash-Preview`), so before pricing, the writer mirrors the traex
sessions into a temporary `CODEX_HOME` with only the `"model"` field normalised; the real
`~/.trae/cli` tree is never written. Normalisation does two things: it lowercases the name so
`ccusage` can price traex's real-name models (GPT-5.x, Gemini, DeepSeek, …), and it resolves TRAE
CLI's opaque Claude aliases (`openrouter-1o`/`2o`/`3o`, including `__max` variants) to the real
Anthropic Opus slugs they front (`claude-opus-4-6`/`4-7`/`4-8`) so those price too. Other
vendor-internal slugs we have not mapped (Seed/Doubao/Qwen) remain absent from the price table and
bill free, so a traex day's recorded cost is a real but low-side figure, short by their unpriced
tokens; the writer logs each such slug so a new unpriced family gets noticed instead of billing zero
unseen. A day whose cost is exactly zero (nothing priced) is held
untrusted, so a stored cost is never overwritten downward; traex is excluded from
`--reconcile-since` for the same reason.

Local Claude/Codex session logs rotate. Once a daily observation reaches this repository,
`sync_usage.py` preserves the highest observed token total for that machine, agent, and date.
Cost follows the winning token observation so a pricing-table refresh does not keep a stale
high-water price.

A new machine can only recover dates still present in its local logs.

## Prerequisites

- macOS with Homebrew
- `uv`
- `ccusage`
- Git and authenticated push access to this repository
- Authenticated GitHub CLI (`gh`) on the `work` and `personal` writers for rollover recovery
- Claude Code, Codex, OpenCode, or TRAE CLI (traex) local usage logs

Install the command-line dependencies:

```sh
brew install uv ccusage gh
```

## Machine identity

Durable machines use one of three intentionally public role labels: `work`, `personal`, or
`devbox`. Configure the label that describes the checkout:

```sh
mkdir -p ~/.config/token-activity
printf 'personal\n' > ~/.config/token-activity/node_name
```

Ephemeral trail workers set `CC_USAGE_TRAIL=1`, or set it to a stable worker ID. The producer
stores only an opaque `data/trail/node-<12 hex digits>` path, never the raw value: a stable
worker ID is stored as its SHA-256 digest, and `CC_USAGE_TRAIL=1` mints a random ID once and
keeps it in `~/.config/token-activity/trail_id`.

That file is the worker's identity. It used to be derived from the hostname, which drifts: a
worker whose hostname moves while its `$HOME` persists comes back as a new node and re-uploads
every day it had already reported, and compaction folds pods additively, so those days multiply.

**Upgrading a worker that already has data under a `node-…` folder, do this first**, or its
next sync mints a fresh ID and orphans that folder:

```sh
mkdir -p ~/.config/token-activity
printf 'node-0123456789ab\n' > ~/.config/token-activity/trail_id   # its existing folder name
```

A genuinely new worker needs nothing — it finds no file and mints its own. A file that exists
but does not parse stops the run rather than reminting over an identity in use.

## Scheduled sync

The bundled launchd agent runs every 15 minutes. Render its template for the current checkout:

```sh
uv run --script scripts/install_launchd.py
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.kyriekevin.aether-ledger.plist
launchctl kickstart -k "gui/$(id -u)/com.kyriekevin.aether-ledger"
```

The installer migrates an existing approved `machine_name`, then unloads and removes the legacy
`com.kyriekevin.cc-cx-usage-data` agent. This prevents old and new schedules from running together.

Logs:

- `~/Library/Logs/aether-ledger/sync.log`
- `~/Library/Logs/aether-ledger/sync.err.log`

### Concurrent Git access

The writer is not the only scheduled process running Git in this checkout. The downstream Feishu
signature pusher pulls the same working tree, and its launchd `WatchPaths` watch the very data
files the writer produces, so a write wakes it up mid-sync. Git has no cross-process lock for a
working tree: concurrent fetches truncate and rewrite `.git/FETCH_HEAD` under each other, which
surfaces as `fatal: Cannot rebase onto multiple branches.`; remote-ref updates lose their
compare-and-swap (`cannot lock ref ... is at X but expected Y`); and a concurrent fetch can update
the checked-out branch's ref, after which `pull` tries to fast-forward the working tree under the
other process.

Every process that runs Git in this checkout therefore takes one advisory lock file,
`~/.cache/aether-ledger/git.lock`:

- The writer (`sync_usage.py`) waits up to 60 seconds and skips the run if the lock never frees.
  Local stores are cumulative, so a skipped tick loses nothing as long as a later one runs. It
  holds the lock for its whole run, `ccusage` included, which is why that call has its own timeout:
  an unbounded hang would strand the lock and starve every other Git user here.
- `compact_trails.py` takes the same lock, in both normal and `--dry-run` mode, because it pulls
  before reporting. It is hand-started, so it reports and exits rather than retrying.
- The signature pusher runs no Git at all — it reads whatever the writer last left on disk, at
  most one writer cycle behind. It used to fetch and rebase here, which bought nothing (it has no
  commits of its own to replay) and made it the single largest source of the FETCH_HEAD race
  above. It still waits up to 30 seconds for the lock and holds it across its reads, because the
  writer replaces the per-agent JSON files one at a time and an unlocked read can mix
  generations. Only the lock guarantees that. If it never frees, the pusher
  reads anyway, checking that no store's mtime moved across the read and retrying a few times —
  weaker than the lock, since it catches a writer running *during* the read but not one paused
  between two of its own file swaps. Skipping the run instead would let sustained contention
  freeze the signature indefinitely, and one cycle of drift in a summed total self-corrects.

Any new process added to this checkout must take the same lock.

## Daily branch lifecycle

Writers use `usage/YYYY-MM-DD`, based on the Asia/Shanghai calendar day. Multiple machines can
push the same branch because they own distinct directories and the producer retries a push race
with a rebase.

The `.github/workflows/daily-rollover.yml` workflow runs shortly after midnight and makes one
idempotent retry 30 minutes later:

1. Find every `usage/YYYY-MM-DD` branch older than today.
2. Squash each completed day into `main` in date order.
3. Regenerate the dashboard and create one snapshot commit per day.
4. Push `main` and delete only the successfully published day branches.
5. Create today's branch from the updated `main`.

If a feature PR accidentally carried an earlier generated-store snapshot from the active usage
branch, the squash can report a conflict even though the usage branch already contains that exact
`main` blob in its post-fork history. The rollover accepts the usage branch's final version only
when it can prove that relationship for every conflict and every path is a canonical generated
store. Any unrelated data conflict, pre-fork match, or conflict outside those stores still fails
closed for human review.

If GitHub Actions is delayed, a writer will not create today while any older usage branch still
exists. It exits cleanly and catches up on the next scheduled run. This prevents today's branch
from being based on a `main` that lacks an earlier day's final data.

After each successful fetch, writers also fast-forward the local `main` ref when it has no
local-only commits. Once a completed remote usage branch has disappeared, the writer deletes its
local counterpart only when the branch holds nothing of its own: its `data/` must match the day's
finalized snapshot on `origin/main`, and it must introduce no change outside `data/` relative to
the commit where it forked from `main`. The second check compares against the fork point on
purpose. Comparing whole trees against the snapshot would count every code or docs commit that
reached `main` after the fork — routine while the repo is still moving — as a local difference and
pin the branch forever. Branches with unpublished data, with a net change of their own outside
`data/`, or checked out by another worktree are kept, each with its own log line; so is any branch
whose comparison fails outright, which is reported separately from a real difference.

Both checks read final trees, not commit history, because a squash-merged branch leaves no
ancestry to test. A branch whose own commits cancel out — a change and its revert, an empty commit
— therefore reads as holding nothing and is deleted. `git branch -D` drops that branch's reflog
along with the ref, so those commits keep only whatever other references still reach them — HEAD's
reflog, if the branch was ever checked out here — and become prunable once none do.

The workflow is also manually dispatchable. Its concurrency group prevents overlapping rollover
runs, and scanning all older date branches makes both the retry and manual recovery safe after a
missed schedule.

The scheduled `work` and `personal` writers are external watchdogs for the GitHub scheduler.
After a 00:50 grace period, their normal 15-minute sync checks for an older usage branch. If one
still exists, they use the locally authenticated `gh` session to dispatch the rollover workflow.
The workflow concurrency lock makes simultaneous recovery requests safe. `devbox` and ephemeral
`trail` writers do not act as watchdogs because they are manually or workload triggered. A failed
recovery request produces an explicit sync-log error and is retried on the next scheduled tick.

## Commit convention

Human changes use Conventional Commit subjects with a focused scope, for example
`docs(readme): explain the activity ledger`. Automated writers use
`chore(data): sync node-<digest> usage`; the daily squash commit on `main` is
`chore(data): finalize YYYY-MM-DD snapshot`. Trail compaction also uses the `chore(data)` scope.
Local and Kubernetes writers force the public-safe identity
`Aether Ledger <noreply@github.com>` for automated commits, so they do not depend on or expose
the host's Git identity. The rollover workflow uses the GitHub Actions bot identity.

## Downstream consumers

`main` intentionally contains only finalized daily snapshots. A consumer that needs intraday
values, such as the Feishu signature pusher, should read the same checkout used by
`sync_usage.py`; that checkout follows today's usage branch. A separate clone pinned to `main`
will be up to one day behind by design.

## Dashboard

`scripts/render_dashboard.py` scans `data/` for canonical files named `claude.json`, `codex.json`,
`opencode.json`, or `traex.json`. Files such as `codex_by_repo.json` are deliberately excluded.

The activity SVG contains:

- latest completed snapshot, month, lifetime, and peak tokens with API-equivalent cost;
- active days, defined as calendar days with a positive aggregate token total;
- a trailing 53-week daily heatmap.

The topology SVG crosses public environment roles with agents active in the trailing 30 days.
Each environment has its own restrained hue, while intensity within that row shows an agent's
share of the environment. OpenCode-launched history is retained as `Legacy`. The recent window
ends on the same completed
snapshot as the activity SVG. Trail workers and persistent `devbox` stores are combined as
`Development`; opaque trail node IDs never enter the asset. The underlying stores remain separate
for collection and operations.

Both dashboard SVGs use `prefers-color-scheme` with Catppuccin Latte and Mocha colors across
GitHub's light and dark themes.

Only the rollover workflow commits the shared SVGs. Individual machine writers commit only their
own data directory, avoiding generated-asset conflicts when machines push concurrently.

Intensity levels use distribution quartiles rather than a linear scale, so ordinary days remain
visible when trail workloads create very large peaks. The published SVGs contain aggregate token
counts and, in the activity view, API-equivalent cost; they do not include model, machine, path,
prompt, or repository-level data.

## Public-data boundary

Committed data is limited to date-keyed aggregate usage under `data/`, using the public durable
roles `work`, `personal`, and `devbox`, or opaque IDs for ephemeral workers. The producer does not
collect working directories, repository names, prompts, session identifiers, usernames, or hostnames.
Repository-level exports such as `codex_by_repo.json` are ignored and forbidden by the public-data
audit.

Before publishing or changing a data producer, run:

```sh
uv run --script scripts/audit_public.py
```

The daily workflow runs the same audit before merging usage branches.

## Store schemas

Claude entries contain daily token and raw API-equivalent cost totals, and a per-model token
breakdown:

```json
{
  "2026-04-07": {
    "totalTokens": 8946720,
    "totalCost": 0.44,
    "models": {
      "claude-opus-5": {"totalTokens": 8946720}
    }
  }
}
```

The `models` map is written going forward; days whose sessions `ccusage` has already rotated away
keep their existing totals-only shape.

Codex entries may also contain model token breakdowns and an image counter:

```json
{
  "2026-04-20": {
    "totalTokens": 248347,
    "totalCost": 0.6163031,
    "models": {
      "gpt-5.3-codex": {"totalTokens": 248347}
    },
    "imageCount": 0
  }
}
```

OpenCode has the same date-keyed shape and may include per-model totals. Classification is based
on model family because current `ccusage` model breakdowns do not identify the invoking agent.

traex (`traex.json`) uses the same date-keyed shape as Codex, with per-model token breakdowns. Its
`totalCost` is a real but low-side figure: model names are normalised before pricing (lowercased,
and `openrouter-*` Claude aliases resolved to real Opus slugs) so `ccusage` can price them, while
unmapped vendor-internal slugs (Seed/Doubao/Qwen) contribute tokens with no cost.

## Trail compaction

Run `scripts/compact_trails.py` on one writer only. Pods whose newest data is older than seven days
are added into `data/trail/rollup` and removed in the same commit. The fold is additive because every
ephemeral pod directory represents a distinct worker.

Always preview first:

```sh
uv run --script scripts/compact_trails.py --dry-run
```

## Recovery

- A failed data push leaves a clean local commit; the next sync retries it before switching days.
- A dirty worktree blocks automatic day switching rather than carrying edits onto another date.
- A failed rollover keeps the source branch and does not create today from stale `main`.
- A malformed canonical JSON store makes dashboard generation fail instead of silently publishing
  an incomplete aggregate.
