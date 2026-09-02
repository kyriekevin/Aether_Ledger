# Operations

[English](operations.md) | [简体中文](operations_zh-CN.md)

## Sources and retention

Each writer reads local usage through `ccusage daily --json --by-agent` and writes its exact
per-agent breakdowns into Claude, Codex, and OpenCode stores. Model names are retained as detail,
not used to infer the invoking agent: a model reached through a Claude Code router such as
cc-switch therefore remains Claude usage under its real model name. Codex `image_gen` PNGs are
counted separately by local file mtime because they do not appear as LLM token events.

TRAE CLI (traex) remains a separate collection path. It is a Codex fork that writes the same
`rollout-*.jsonl` session format under
`~/.trae/cli` instead of `~/.codex`. `ccusage` has no `trae` agent, but its `codex` reader honours
`CODEX_HOME`, so each writer runs a second, read-only `ccusage codex daily` with
`CODEX_HOME=~/.trae/cli` and records the result in a separate `traex.json` store. That invocation
never touches the real `~/.codex` tree, so Codex usage and the Codex CLI itself are unaffected.
TRAE CLI logs capitalised model names (`GPT-5.5`, `Gemini-3-Flash-Preview`), so before collection,
the writer mirrors the traex
sessions into a temporary `CODEX_HOME` with only the `"model"` field normalised; the real
`~/.trae/cli` tree is never written. Normalisation does two things: it lowercases the name so
it matches the shared table, and it resolves TRAE
CLI's opaque Claude aliases (`openrouter-1o`/`2o`/`3o`, including `__max` variants) to the real
Anthropic Opus slugs they front (`claude-opus-4-6`/`4-7`/`4-8`). It also collapses TRAE's
short Gemini config names (`gemini-3.1-pro`, `gemini-3-flash`) onto the official preview slugs
recorded in its own model metadata, so old and new sessions share one cumulative model bucket.

DeepSeek Harness (dsh) is read from its own logs, because ccusage has no `dsh` agent to borrow the
way traex borrows the Codex reader. A dsh harness home holds one append-only JSONL event log per
session at `<root>/<project>/<session>/session.jsonl[.zstd]`, and the writer reads three of its
event types: `request/header` and `request/context` name the model serving the calls that follow,
and `assistant/message` carries one model call's token accounting. dsh reports disjoint counts —
`inputTokens` excludes cached input, which arrives separately as cache reads and writes — so the
four components add up to the billed total, the same convention the ccusage-fed stores use. Normal
runs land in `dsh.json`, priced at the official standard tier; dsh records no priority tier for a
multiplier to apply to. Effort comes from the header's `reasoningEffort`: dsh's `off` is recorded
as this repository's `none`, and its `minimal`, which has no counterpart here, records no effort
bucket rather than being folded into `low`.

Session logs are normally Zstandard-compressed, as a concatenation of one independently decodable
frame per durable batch. The writer decodes them with Python's own `compression.zstd` (3.14+) or,
failing that, a local `zstd` binary. Under the pinned 3.11 it is always the `zstd` binary; the
in-process branch is there for when the pin moves, and both return the same prefix for the same
artifact. Whatever decoded is kept, and the run reports how many logs did
not decode to the end. Keeping a partial read is safe: frames are append-only and every run
recomputes the day from the whole artifact, so a torn read is superseded by the completed one rather
than added to it.

A live session's unfinished last frame is the ordinary cause of a partial read, and the writer does
not claim to tell it apart from a damaged frame. It cannot: zstd reports single-byte corruption
under seven different messages, one of them the same `premature end` a live tail produces, and frame
boundaries cannot be found by scanning for the frame magic because those four bytes also occur
inside compressed payloads. So the count is reported without a claimed cause, and a count that stays
positive across runs with no dsh session running is the signal that a log is genuinely damaged.

Decoding stops at the first frame that does not decode and does not resume past it. For the ordinary
cause nothing follows the torn frame to lose. A frame damaged mid-file is the case that costs
something: the batches after it are not read until the artifact is repaired or rotated away. The
per-day high-water merge holds the days already recorded steady in the meantime, so the ledger
stalls rather than drops. That is the accepted price of not guessing at frame boundaries — an
earlier revision did guess, and the guess was what made valid frames undecodable. On a machine with neither decoder the
run reports how many logs it could not read at all and leaves the cumulative store intact, exactly
as a failed ccusage fetch does.

Multica is an orchestrator rather than a harness: it drives Claude Code, Codex, TRAE CLI, and dsh
in its own workspaces, and that work belongs to those CLIs' stores. Its Claude and TRAE runs
already write to `~/.claude/projects` and `~/.trae/cli/sessions`, so they are collected with no
special handling. Codex and dsh are the exceptions. Multica gives each Codex task a private `CODEX_HOME` whose
`sessions` is a symlink into a shared `~/.codex/multica-sessions` tree, which is a sibling of
`~/.codex/sessions` rather than a child, so ccusage's default scan never sees it. The writer reads
that tree with the same Codex reader through a temporary `CODEX_HOME` holding one symlink, and
writes the result to a store of its own, `codex-multica.json`. Multica also relocates dsh logs to
`~/.multica/profiles/<profile>/dsh-sessions`; the writer discovers those profile roots and records
them in `dsh-multica.json`.

Because those tokens already reach the ledger through the harnesses, the Multica API is never asked
for token totals — that would count the same work twice, from two measurements that do not agree
(for 2026-08-31 the API reported 21.63M against 21.50M parsed from the local rollouts). What only
Multica knows is the shape of the work it dispatched, so `data/multica.json` records exactly that:
per day, per public role, per agent, how many runs finished, how they ended, and how long they took.
The audit rejects a `usage` section in that file, which is what re-introducing the double count would
look like.

The store sits at the data root rather than under a node label because one API answers for every
runtime at once, and a single configured machine collects it so that the one-writer-per-file rule
still holds. Collection is opt-in: `~/.config/token-activity/multica_runtime_roles.json` must map
each runtime's operator-chosen custom name to `work`, `personal`, or `devbox`. A machine without
that file collects nothing rather than guessing, and a runtime whose provider this repository does
not recognise is reported on stderr rather than skipped silently — a renamed provider string would
otherwise read exactly like that provider having done no work.

Runs are dated by when they started, in Shanghai time, and only terminal runs are counted: a run
still in flight has no duration and would be recounted under a different status next time. Each run
and issue is counted once per collection: issue pages overlap while the workspace is being written
to, and one run can surface under two issues. A duplicate would not break the arithmetic — it adds to
`total` and to one outcome together — so nothing downstream would notice, and the merge would make
the inflated total the permanent high-water mark. Days
merge by keeping the fuller observation, for the same reason the token stores keep a high-water mark
— a finished run's day, status and duration never change again, so a smaller number means the fetch
saw less than the store already knows, which is what happens once the workspace prunes old issues.

The comparison is over the whole counter bundle rather than each counter separately. Maxing counters
independently would let `completed` come from one fetch and `failed` from another, and those two
never described the same runs: prune two completed runs, land two failed ones, and the per-counter
maxima claim two runs with four outcomes — a day that never happened, which the audit then rejects.
Ties on total are broken by the longer duration, because a terminal run can be reported before its
finish time is set and would otherwise keep a duration of zero for good. Keeping the observation with
the larger total stays internally consistent, at the cost of recording
the larger single observation rather than a sum when a day is pruned and refilled. That undercounts,
which is the honest direction: the two fetches overlap by an unknown amount, so adding them would
invent runs rather than miss them.

The CLI answers for one profile and one workspace at a time, and a profile holding no issues returns
an empty list rather than an error, so pointing the collector at the wrong one looks identical to an
idle day. Set `MULTICA_PROFILE` (and `MULTICA_WORKSPACE_ID`, which the CLI reads itself) when the
work is not in the CLI's default profile; `--profile` is a global flag and is rejected after the
subcommand, so the collector puts it in front. A fetch that finds runtimes but no terminal runs says
so on stderr rather than writing an empty day quietly. The two servers also disagree on one provider
string — TRAE CLI is `traecli` on one and `traex` on the other — so both map to the `traex` agent.

A separate store rather than a bigger Codex or dsh day, because the cumulative merge keeps the larger of
the stored and incoming observations per day. That high-water rule is what protects history from
session rotation, and it only holds while each stored number describes one fixed source. Add the
two trees together first and max() is comparing sums whose composition can change: on a day whose
Multica rollouts have aged out while the standard tree kept growing past the old combined total,
the larger sum wins and the pruned tree's share is gone, with nothing to signal it and no later run
able to restore it. One store per tree keeps every high-water mark meaning what it says, and
`AGENT_BUCKETS` folds `codex-multica` under `codex` and `dsh-multica` under `dsh` at render time, so
no chart shows either as a harness of its own. It also isolates a failed Multica read: an empty fetch merges nothing, where a
summed one would have rewritten the Codex day from a partial view under `--reconcile-since`.

Unlike the traex path, this one keeps ccusage's own cost for a row whose every
model has an unchanged official rate, because ccusage still knows which of those calls ran on the
priority tier or crossed a long-context threshold and a day's totals cannot say.

Token prices come only from `config/official-pricing.json`. The sync invokes ccusage with
`--offline` and a generated override file, so neither LiteLLM's live table nor models.dev can alter
stored amounts. ccusage still supplies its request-level Codex Fast/standard and long-context
classification; the repository supplies the rates. Models absent from the checked-in table, and
models such as `gpt-5.3-codex-spark` with no public official API token price, contribute tokens but
zero cost and record `costSource: "unpriced"`. If a later table update supplies a rate effective
for that date, the next sync may replace that provisional amount even when the token count is
unchanged. This is an API-equivalent estimate, not a subscription invoice.

Refresh the table from first-party vendor sources with:

```sh
uv run --script scripts/update_pricing.py
uv run --script scripts/update_pricing.py --apply --effective-from YYYY-MM-DD
```

The first command is read-only and also discovers model names already present in all durable and
trail stores. Review its `CHANGE`, `UNPRICED`, and `UNSUPPORTED` lines before applying. The updater
fails closed when an official page cannot be fetched or parsed; it never falls back to a reseller.
`effectiveFrom` is the first repository date to which that captured rate applies, not a claim about
the model's public release date. Initial entries use each model's first observed date.
DeepSeek, Google Gemini, Kimi, and MiniMax are read directly from their canonical API pricing
pages. The registered Gemini models use the standard text/image rate within TRAE's advertised
200K context cap; audio and Google cache-storage charges are not present in the captured token
buckets. DeepSeek daily aggregates do not retain request timestamps, so a peak/off-peak table uses
the peak, undiscounted rate as a conservative API-equivalent estimate. OpenAI models with a separate
long-context tier are accepted only when the pinned ccusage version is listed as knowing that
model's request-tier boundary; otherwise the updater reports `UNSUPPORTED` instead of silently
using ccusage's generic 200K fallback.

The daily rollover runs `update_pricing.py --require-observed-prices` only after main was pushed,
completed usage branches were deleted, and today's branch was created. An observed model from a
supported public provider without an active reviewed rate makes the rollover workflow visibly
fail, but cannot strand or discard the usage snapshot. Internal and deliberately unsupported
families are advisory only. Adding a rate remains a reviewed pull request because its effective
date controls historical backfill; the scheduled audit never edits the table itself. Kimi and
Gemini parsers discover model IDs from their official family pages, so a future Kimi K3 or Gemini
variant is surfaced after its first recorded use rather than requiring a hard-coded model list.

`main` is protected by the required GitHub Actions check named `checks`; a red or missing check
blocks a pull-request merge and a direct human push. Daily rollover is the sole direct writer. It
validates the completed snapshots, publishes the exact candidate SHA on a temporary branch, and
uses its `checks: write` permission to attach a successful `checks` run to that SHA before pushing
it to `main`. It then preserves the existing failure-safe order: advance `main`, delete completed
usage branches, remove the temporary candidate branch, and create today's usage branch. Thus the
same gate covers human changes without disabling the automated close.

Local Claude/Codex session logs rotate. Once a daily observation reaches this repository,
`sync_usage.py` preserves the highest observed token total for that machine, agent, and date.
Cost follows the winning token observation. Once a date has an official-priced observation, the
same token high-water mark is immutable; use `--reconcile-since` for an intentional historical
correction. A provisional `unpriced` observation is the exception and can be back-filled once.
Legacy dates with no model breakdown keep their existing amount because they cannot be reconstructed
without guessing a model.

A new machine can only recover dates still present in its local logs.

## Prerequisites

- macOS with Homebrew
- `uv`
- `ccusage` 20.0.19 or newer (`--by-agent`, pricing overrides, and recorded Fast tier support)
- Git and authenticated push access to this repository
- Authenticated GitHub CLI (`gh`) on the `work` and `personal` writers for rollover recovery
- Claude Code, Codex, OpenCode, TRAE CLI (traex), or DeepSeek Harness (dsh) local usage logs

Install the command-line dependencies:

```sh
brew install uv ccusage gh
```

The scripts are dependency-free single files carrying their own `requires-python = ">=3.11"`, so
there is no project file or lockfile to resolve. That floor alone left the interpreter open: `uv`
picks the newest installed version that satisfies it, which differs between a developer machine, CI,
and the rollover runner, and neither workflow passes a `python-version`. `.python-version` pins all
of them to 3.11 — the declared floor, so what runs is what the scripts claim to support. Every
`make` target goes through `uv` so the pin actually covers them.

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

The bundled launchd agent runs at minutes 0, 15, 30, and 45 of every hour. Keep the checkout used
for development off the active `usage/YYYY-MM-DD` branch, update `main`, then install:

```sh
git switch main
git pull --ff-only
uv run --script scripts/install_launchd.py
```

The installer creates `~/.cache/aether-ledger/writer` as a linked, launchd-only Git worktree and
renders that path into the agent. If today's usage branch already exists, the writer checks it out;
otherwise it starts detached from `main` and lets the normal rollover guard create the branch.
The source checkout must not still have today's usage branch checked out, because Git permits one
worktree per local branch. Use `--writer-worktree PATH` to choose a different location.

The writer pins the code inherited when the daily branch was created. Changes merged into `main`
during the day are never merged into the active usage branch. When the writer switches to the next
daily branch it ends that tick before reading usage, so the following invocation starts from the
new code snapshot. This keeps scheduled commits data-only and isolates development branch switches
and dirty files from launchd. Linked worktrees still share Git metadata: a concurrent fetch, pull,
or rebase in another worktree can make one tick defer, and the next scheduled run retries it.
Refs are shared too: the writer fast-forwards the local `main` ref after each fetch, but Git will
not move a branch a worktree has checked out, so a source checkout parked on `main` keeps that ref
and is the only thing that advances it, by its own `git pull`.
Re-running the installer reuses the registered writer worktree and reloads the agent.

The installer migrates an existing approved `machine_name`, then unloads and removes the legacy
`com.kyriekevin.cc-cx-usage-data` agent. It atomically installs and reloads the current agent so
schedule changes take effect immediately. This prevents old and new schedules from running together.

Logs:

- `~/Library/Logs/aether-ledger/sync.log`
- `~/Library/Logs/aether-ledger/sync.err.log`

Each invocation writes start and finish heartbeat lines to the standard-output log.

### Concurrent Git access

The writer is not the only process running Git in this checkout: `compact_trails.py` does too, as
do a human's terminal, editor, and any linked worktree — the last three under no lock at all. The
downstream Feishu signature pusher used to pull this working tree as well, and its launchd
`WatchPaths` watch the very data files the writer produces, so a write wakes it up mid-sync; it
was the largest single contributor to the race below and now runs no Git at all (see below). Git
has no cross-process lock for a working tree: concurrent fetches truncate and rewrite `.git/FETCH_HEAD` under each other, which
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
4. Re-run the public-data audit, the dashboard freshness check, the commit-identity audit, and the
   whitespace check over everything about to land.
5. Push `main` and delete only the successfully published day branches.
6. Create today's branch from the updated `main`.

Step 4 is the only gate these commits get. The push is authenticated with `GITHUB_TOKEN`, and a
push made with that token does not start another workflow run, so `verify.yml` never sees them. It is
also the last point at which a bad merge is still recoverable, because step 5 deletes the day
branches immediately. A failure there leaves `main` untouched and the day branches intact, so the
next scheduled pass retries.

Step 3 renders without `--as-of` on purpose. The renderer derives the day from the data, and step 4
verifies the result the same way, so the two cannot disagree. Pinning the calendar day instead
would render a day with no activity as the newest column while the check expected the last active
one; that mismatch would fail every retry, strand the day branch, and stop today's branch from
being created — halting the sync entirely, since writers refuse to open today while an older day
branch survives. An idle day now simply produces no snapshot commit.

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

## Continuous integration

`.github/workflows/verify.yml` runs the hand-off checklist on every pull request, on every push to
`main`, and on manual dispatch. It holds `contents: read` only, and supersedes stale pull-request
runs while grouping every other run by commit, so those never cancel or queue behind each other.

It does not see the daily rollover. That push is authenticated with `GITHUB_TOKEN`, which does not
start another workflow run, which is why the rollover validates before it publishes. In practice
CI covers pull requests and the merge commit each one produces on `main`.

| Check | Catches |
| --- | --- |
| `unittest discover -s tests` | producer, renderer, and rollover regressions |
| `audit_public.py` | identity and filesystem-path leaks, out-of-schema store fields |
| `render_dashboard.py --check` | committed SVGs that no longer match the committed data |
| `py_compile scripts/*.py` | syntax errors in scripts no test imports |
| `git diff --check` | trailing whitespace and stray conflict markers |
| `audit_public.py --history` | new commits carrying a personal email address |

The last two run over the incoming commit range rather than the working tree, which is empty on a
fresh checkout. On a pull request the range runs from the base commit to the branch tip, not to
`HEAD`: `HEAD` is GitHub's synthetic merge commit, which is authored with the account's primary
email and discarded after the run, so auditing it would report a leak on every pull request. On a
push the range starts at the previous tip. Where no base exists — a branch's first push, a
force-push over a discarded tip, a manual dispatch — both range checks are skipped and say so,
rather than falling back to a single commit and reporting coverage that is not there.

The commit-identity audit is scoped to the incoming range on purpose: it must block new leaks
without failing on commits that predate the check.

Usage commits never open a pull request and land on `usage/YYYY-MM-DD`, so limiting the push
trigger to `main` keeps the 15-minute sync out of CI without needing a path filter.

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
values, such as the Feishu signature pusher, reads the writer's own worktree —
`~/.cache/aether-ledger/writer`, or wherever `--writer-worktree` put it — because that is the
only checkout following today's usage branch. The source checkout is not a substitute: the
installer requires it to give up today's branch before the writer worktree can exist, so it sits
on `main` or on whatever its owner is working on, and today has no data there. A separate clone
pinned to `main` will be up to one day behind by design.

## Dashboard

`scripts/render_dashboard.py` scans `data/` for canonical files named `claude.json`, `codex.json`,
`codex-multica.json`, `dsh-multica.json`, `opencode.json`, `traex.json`, or `dsh.json`. Files such as `codex_by_repo.json` are deliberately
excluded.

The activity SVG contains:

- latest completed snapshot, month, lifetime, and peak tokens with API-equivalent cost;
- active days, defined as calendar days with a positive aggregate token total;
- a trailing 53-week daily heatmap.

The topology SVG crosses public environment roles with agents active in the trailing 30 days.
Each harness keeps the same hue used by the history view, while intensity within a row shows its
share of that environment. OpenCode-launched history is retained as `Legacy`. The recent window
ends on the same completed
snapshot as the activity SVG. Trail workers and persistent `devbox` stores are combined as
`Development`; opaque trail node IDs never enter the asset. The underlying stores remain separate
for collection and operations.

Topology and allocation each pair the trailing-30-day snapshot with a separate eight-week history
asset. Both histories use the same adjacent weekly buckets and explicitly split them into the
previous four weeks and the latest four weeks. Topology history uses absolute weekly stacks within
Work, Personal, and Development, so bar height preserves each environment's total while color
shows harness substitution. Allocation history uses absolute Top 3 model + Other stacks within
each harness. Missing model coverage stays blank or gray rather than being plotted as zero.

The README therefore reads as activity, then current/history pairs for topology, allocation, and
runtime. The runtime snapshot uses lengths and exact values for effort, Fast, and the latest day's
seven-day quota peak. Its history uses smaller weekly effort stacks, a Fast trajectory, and weekly
seven-day quota peak bars. Effort covers every harness; Fast and quota are Codex-only, so a single
quota series is centred on each week rather than paired against an empty slot. Color identifies a harness or effort category while geometry shows
magnitude, matching the visual grammar of the other history views.

Claude assistant events expose effort and, on supported models, `thinking_tokens`; Fast is not
selectable in the observed Claude setup, so its standard-only speed field is not collected or
shown. Claude logs expose no Codex-style quota fields. Claude does report quota, but only to `statusLine.command`, and reading that stream means wrapping the status line the user already runs -- a wrapper that cannot be made fully transparent, so no Claude quota is collected or charted. Codex exposes effort, reasoning, speed, and
quota. TRAE is an internally provided CLI, not a model vendor or an intrinsically cheap substitute;
its model mix is shown literally. Because compatible TRAE builds use the Codex rollout format, the
collector also accepts their effort, speed, reasoning, and quota events when present. Missing
historical telemetry stays explicitly unavailable instead of being inferred from total tokens or
cost. Reasoning intensity is interpreted only within one harness, never across vendors.

All seven dashboard SVGs use `prefers-color-scheme` with Catppuccin Latte and Mocha colors across
GitHub's light and dark themes.

Only the rollover workflow commits the shared SVGs. Individual machine writers commit only their
own data directory, avoiding generated-asset conflicts when machines push concurrently.

Intensity levels use distribution quartiles rather than a linear scale, so ordinary days remain
visible when trail workloads create very large peaks. The published SVGs contain aggregate token
counts, model names in the allocation view, and API-equivalent cost in the activity view. They do
not include machine identity, paths, prompts, sessions, or repository-level data.

## Public-data boundary

Committed data is limited to date-keyed aggregate usage under `data/`, using the public durable
roles `work`, `personal`, and `devbox`, or opaque IDs for ephemeral workers. The producer does not
persist working directories, repository names, prompts, session identifiers, usernames, or hostnames.
Repository-level exports such as `codex_by_repo.json` are ignored and forbidden by the public-data
audit.

Before publishing or changing a data producer, run:

```sh
uv run --script scripts/audit_public.py
```

The daily workflow runs the same audit before merging usage branches, and CI runs it on every
pull request.

Commit metadata is public too. Keep the checkout's identity on a no-reply address:

```sh
git config user.email "<id>+<username>@users.noreply.github.com"
```

A local setting is not sufficient on its own. GitHub rewrites the author of a squash merge made
from the web UI to the account's primary email address, so **Settings → Emails → Keep my email
address private** must also be enabled, otherwise every merged pull request republishes it. Audit
a range with:

```sh
uv run --script scripts/audit_public.py --history origin/main..HEAD
```

## Store schemas

Claude entries contain daily token and raw API-equivalent cost totals, and a per-model token
breakdown:

```json
{
  "2026-04-07": {
    "totalTokens": 8946720,
    "totalCost": 0.44,
    "costSource": "official",
    "models": {
      "claude-opus-5": {
        "totalTokens": 8946720,
        "inputTokens": 100,
        "outputTokens": 20,
        "cacheCreationTokens": 2000,
        "cacheReadTokens": 8944600
      }
    }
  }
}
```

The detailed `models` map is written going forward. Days whose sessions `ccusage` has already
rotated away keep their existing totals-only shape and legacy cost.

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

New observations preserve the four token components under each model for every harness that
ccusage can break down. Codex session events also contribute aggregate routing and quota telemetry;
Claude assistant events contribute effort and thinking telemetry; compatible TRAE session events
contribute the same routing fields as Codex when present:

```json
{
  "routing": {
    "efforts": {
      "low": {
        "calls": 12,
        "totalTokens": 840000,
        "reasoningCalls": 12,
        "reasoningOutputTokens": 42000
      }
    },
    "speeds": {
      "fast": {"calls": 3, "totalTokens": 210000}
    }
  },
  "quota": {
    "windows": {"300": 64.0, "10080": 37.0},
    "limitReached": false
  }
}
```

`calls` counts model invocations observed in session telemetry; it is not a count of user turns.
`reasoningCalls` counts invocations where the harness exposed a reasoning or thinking-token field,
including an explicit zero, so per-call trends do not turn missing telemetry into zero.
Routing token totals come from that session stream and are not coverage estimates for the
independently collected canonical daily total. Window keys are anonymous durations in minutes.
Message and session identifiers are used only for in-memory deduplication and are never written.
Historical totals remain valid but do not gain component or routing detail after their source logs
rotate. Legacy entries may retain the former `turns` field with the same model-call meaning.

OpenCode has the same date-keyed shape and may include per-model totals. Its agent attribution also
comes directly from the `--by-agent` breakdown rather than from the model family.

dsh (`dsh.json` and `dsh-multica.json`) uses the same date-keyed shape, with `routing.efforts` when
the session headers named a reasoning level this repository renders. Both stores represent
DeepSeek Harness and are combined in every dashboard; the split only preserves their independent
source-tree high-water marks.

traex (`traex.json`) uses the same date-keyed shape as Codex. It represents the internal TRAE CLI,
while its recorded model names describe the actual capacity supplied behind that harness. Fast
pricing is not assumed, so registered models use the official standard rate; unknown models
contribute tokens with zero cost. Codex-compatible routing fields are collected when a TRAE build
emits them and otherwise remain unavailable.

## Trail compaction

Run `scripts/compact_trails.py` on one writer only. Pods whose newest data is older than seven days
are added into `data/trail/rollup` and removed in the same commit. Every ephemeral pod directory
represents a distinct worker, so token, model-component, and routing counters fold additively;
quota windows retain their maximum observed pressure.

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
