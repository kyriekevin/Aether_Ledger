# Operations

[简体中文](operations_zh-CN.md)

## Collection scope

The ledger collects daily tokens and API-equivalent cost from local session logs. It does not
query Multica tasks, issues, agents or comments, and does not collect effort or quota telemetry.
Model names and input/output/cache components remain necessary for pricing and reconciliation.

| Store | Source |
| --- | --- |
| `claude.json` | ccusage Claude logs, including Multica/Trae-launched Claude sessions in the same log tree |
| `codex.json` | ccusage ordinary Codex logs |
| `codex-multica.json` | ccusage over discovered shared and task-private Multica Codex session trees |
| `traex.json` | ccusage Codex parser over the TRAE CLI home, with normalized model names |

Each source retains a separate cumulative store to avoid mixing its high-water mark with another
source. Multica Codex discovery deduplicates copied rollouts before ccusage reads them. Removing
this discovery would omit token usage. DSH and OpenCode are no longer collected, and their stores
have been removed.

Private source settings live in `~/.config/token-activity/multica.json`; use
[`config/multica.example.json`](../config/multica.example.json) as a template.
`taskWorkspacesRoot` locates task-private Codex logs. Existing `profile` and `workspaceId` keys are
accepted for configuration compatibility; no task API is called. A leftover `dshProfile` key is
ignored.

## Setup

Python is pinned to 3.11. On macOS, install dependencies and verify the ccusage runtime:

```sh
brew install uv ccusage gh rust
uv run python scripts/ccusage_runtime.py --install
uv run python scripts/ccusage_runtime.py --check
mkdir -p ~/.config/token-activity
printf 'personal\n' > ~/.config/token-activity/node_name
make install
make health
```

Use the appropriate public role: `work`, `personal`, or `devbox`. Git must have authenticated push
access; the `work` and `personal` writers also need authenticated `gh` for rollover recovery.

<a id="ccusage-runtime-upgrade"></a>

### ccusage runtime

| Item | Current value |
| --- | --- |
| Version | `20.0.35+7a063a0` (v20.0.35 plus 16 commits, 2026-10-18) |
| Upstream revision | `7a063a09103184391dede2d6285b32a75903d328` |
| Installed binary | `~/.cache/aether-ledger/ccusage/<revision>/ccusage` |
| Long-context models verified | `ccusage.longContextModels` in `config/official-pricing.json` |

The revision is pinned in `scripts/ccusage_runtime.py` (`REVISION`) and recorded as
`ccusage.verifiedRevision` in the pricing table; a test keeps the two equal. The installer builds it
with `cargo` in a local cache. It is the last upstream revision whose models.dev snapshot lives in
the source tree, so long-context boundaries are fixed by the revision rather than fetched at build
time.

ccusage takes each model's long-context boundary from that embedded snapshot; pricing overrides
cannot set it, and a model the snapshot lacks falls back to 200K. `--check` and `--install`
therefore probe every model in `longContextModels` below, at, and above the 272K request boundary,
including cached input and Fast mode. A compatible system runner is accepted only if it passes the
same probes. A failed probe stops all ccusage-backed collection while preserving stored data.
Ordinary shell `ccusage` remains unchanged.

To upgrade:

1. Pick an upstream revision whose models.dev snapshot gives each new long-context model its
   official boundary.
2. Update `REVISION` and `VERSION` in `scripts/ccusage_runtime.py`, `verifiedRevision` and
   `minimumVersion` in the pricing table, this table, and the Chinese copy.
3. Add the models to `longContextModels`, then run `--install` and `--check`. An
   `update_pricing.py` model reported as `UNSUPPORTED` for long-context pricing is not covered yet.
4. Run `--install` on every writing device before the new code reaches its writer. The binary
   cache is keyed by revision, so a writer that picks up the new revision without that binary
   fails its probe and stops ccusage-backed collection until the install runs.

## Scheduled sync

The launchd agent runs at minutes 0, 15, 30, and 45. `make install` creates or reuses a dedicated
linked worktree at `~/.cache/aether-ledger/writer`, renders its path and private environment into
the agent, and reloads it. Use `--writer-worktree PATH` with `scripts/install_launchd.py` to override
the location. Development branch changes do not change the writer's code.

The writer takes a Git lock, catches up its daily branch, reads each source, merges cumulative
stores, and publishes data-only commits. A source failure preserves that source's data, lets other
sources publish, and returns 1. An empty source is not necessarily a failure: it may be idle,
unconfigured, or missing logs. Inspect `latest_day` and `today_tokens` as well as the status.

Code reaches the writer through the next daily branch. Switching days ends the current tick;
the following tick runs the new code. A merge to `main` does not update today's running writer.
Old `--include-statistics` and `--include-multica-tasks` arguments are accepted as no-ops so existing
schedules keep collecting tokens. Reinstall to remove them; `make health` reports stale arguments.

Logs are `~/Library/Logs/aether-ledger/sync.log` and `sync.err.log`. Health checks inspect binaries,
configuration, the installed script path, and source discovery. They do not prove a successful
pricing probe or remote publication.

### Manual sync

A terminal does not inherit launchd's private source settings. To run with the installed command,
writer path and environment:

```sh
uv run python - <<'PYTHON'
import plistlib
import subprocess
from pathlib import Path

plist = Path.home() / "Library/LaunchAgents/com.kyriekevin.aether-ledger.plist"
with plist.open("rb") as stream:
    agent = plistlib.load(stream)
command = agent["ProgramArguments"]
script = next(Path(arg) for arg in command if Path(arg).name == "sync_usage.py")
result = subprocess.run(command, cwd=script.parent.parent, env=agent["EnvironmentVariables"])
raise SystemExit(result.returncode)
PYTHON
```

This writes and attempts publication. `--no-push` still writes local stores, but skips branch
switching, commits and pushes. It is not a dry run. `--help`, `make health`, logs and `git status`
are suitable for read-only inspection. `--reconcile-since YYYY-MM-DD` accepts lower observations
from that date and is reserved for intentional corrections.

### Linux and ephemeral workers

launchd, `make install`, and `make health` are macOS-only. On Linux, `make health` reports the
missing launchd agent, and a trail writer also fails its `node_name` check; both are expected.
Install uv, Rust, and Git push access, then build and check the ccusage runtime as in
[Setup](#setup). `gh` is needed only by `work` and `personal` writers.

Ephemeral workers write as a trail (`CC_USAGE_TRAIL=1`). If the home directory may not survive a
worker change, keep a copy of `~/.config/token-activity/trail_id` elsewhere and restore it before
the first sync; see [Trail workers and recovery](#trail-workers-and-recovery).

Create the writer worktree from today's branch, or from `main` when that branch does not exist yet:

```sh
day="usage/$(TZ=Asia/Shanghai date +%F)"
git fetch origin
git worktree add --track -b "$day" ~/.cache/aether-ledger/writer "origin/$day" \
  || git worktree add --detach ~/.cache/aether-ledger/writer origin/main
```

Run the writer in a loop that matches the launchd schedule. The lock keeps one loop per worker, so
the starter is safe to call repeatedly:

```sh
#!/usr/bin/env bash
export TZ=Asia/Shanghai CC_USAGE_TRAIL=1
LOG_FILE="${LOG_FILE:-$HOME/.local/state/aether-ledger/sync.log}"
mkdir -p "$(dirname "$LOG_FILE")" ~/.cache/aether-ledger
exec 9>~/.cache/aether-ledger/loop.lock
flock -n 9 || exit 0
cd ~/.cache/aether-ledger/writer || exit 1
while true; do
  timeout 1200 uv run --script scripts/sync_usage.py >> "$LOG_FILE" 2>&1
  sleep $(( 900 - $(date +%s) % 900 + 5 ))
done
```

Start it detached, for example with `setsid nohup bash loop.sh >/dev/null 2>&1 &`. The loop must
see the same `CLAUDE_CONFIG_DIR` and `CODEX_HOME` as the agents it measures, because ccusage reads
those variables to find their logs; otherwise those sources come back empty rather than failing.
Set `GIT_SSH_COMMAND` too if pushes use a dedicated key. A worker that is reclaimed kills the loop,
so call the starter again from something that runs on each new worker, such as a shell profile or
an agent session-start hook.

## Pricing and stores

Stores are date-keyed JSON objects under `data/{work,personal,devbox}/` or `data/trail/`.
Each day contains `totalTokens`, `totalCost`, and model-level token components where available.
Pricing provenance marks incomplete or unpriced observations. Only the seven canonical token
store names contribute to the heatmap; other retained data is still audited.

Rates come from `config/official-pricing.json`. ccusage runs offline with generated overrides,
while retaining request-level Fast and long-context classification for correct prices. Missing
official prices contribute tokens with provisional zero cost (`costSource: "unpriced"`). A later
applicable rate can repair that amount even if tokens are unchanged. Cost is an API-equivalent
estimate, not a subscription bill.

```sh
uv run --script scripts/update_pricing.py
uv run --script scripts/update_pricing.py --apply --effective-from YYYY-MM-DD
```

Normal cumulative merging protects stored observations against missing or rotated logs. Keep the
model components, source separation, price provenance, and reconciliation behavior when changing
collectors. Never hand-edit generated stores.

## Branches and verification

High-frequency commits go to `usage/YYYY-MM-DD`, using Asia/Shanghai dates. The daily workflow
squash-merges completed days, regenerates `assets/token-activity.svg`, validates and pushes `main`,
then deletes completed branches and creates today's branch. Preserve this ordering: a failed
publication must leave recoverable source branches. Writers can request missed-rollover recovery.

Human changes go through pull requests with Conventional Commit subjects and no-reply commit
emails. Run `make verify` before handoff: tests, public-data audit, heatmap freshness, Python
compilation, and whitespace checks. CI also audits the whole incoming commit range and author emails.
Keep GitHub's email privacy enabled for squash merges. Only the daily workflow pushes to `main`.

Regenerate the heatmap with `uv run --script scripts/render_dashboard.py`. It sums all canonical
stores, uses the latest active day as its endpoint, and displays daily activity plus token/cost
summaries. `--check` verifies freshness without writing.

For intraday consumers, read the dedicated writer's canonical stores or the current remote daily
branch. `main` contains completed days. Compare with the actual remote tip to verify publication;
a local upstream ref, exit 0, or completed source read alone is insufficient evidence.

## Trail workers and recovery

Ephemeral workers use `CC_USAGE_TRAIL=1` to mint a persistent opaque ID, or supply a stable worker
ID that is hashed. The local `~/.config/token-activity/trail_id` must survive restarts. Preserve an
existing identity when migrating a worker; reminting can upload the same usage under a second name.
Invalid identity files stop collection rather than silently creating another identity.

Preview `uv run --script scripts/compact_trails.py --dry-run` before compaction. Run compaction
on one writer only: inactive worker stores older than seven days are added into `data/trail/rollup`
and removed in the same commit.

Before reconciliation, preserve a copy of the stores and confirm source completeness. Reported
collection failures abort reconciliation before writes, but empty sources
can still omit data. Absent dates retain their values; present dates can be lowered. The option
affects all collected stores and cannot reconstruct missing logs.

A failed push keeps a local commit for retry; dirty files block day switching. A failed rollover
keeps completed branches. Malformed canonical JSON stops heatmap generation. Check the installed
writer's branch, revision, source summaries and stderr before retrying.

The repository is public. Never commit prompts, session exports, repository names, hostnames,
usernames, absolute user paths, session identifiers or private journals. Public-data validators
remain in place for all retained snapshots.
