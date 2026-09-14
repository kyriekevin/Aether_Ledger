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
| `opencode.json` | ccusage OpenCode logs |
| `traex.json` | ccusage Codex parser over the TRAE CLI home, with normalized model names |
| `dsh.json` | Local DSH JSONL and compressed logs |
| `dsh-multica.json` | DSH logs from the bound Multica profile |

Each source retains a separate cumulative store to avoid mixing its high-water mark with another
source. Multica Codex discovery deduplicates copied rollouts before ccusage reads them. DSH
messages are deduplicated by session and step. Removing this discovery would omit token usage.

Private source settings live in `~/.config/token-activity/multica.json`; use
[`config/multica.example.json`](../config/multica.example.json) as a template. `dshProfile`
selects the DSH profile and `taskWorkspacesRoot` locates task-private Codex logs. Existing
`profile` and `workspaceId` keys are accepted for configuration compatibility; no task API is called.
DSH keeps its profile binding in `~/.config/token-activity/multica_dsh_profile`; changing it
requires deliberate reconciliation so two cumulative sources are not mixed.

## Setup

Python is pinned to 3.11. On macOS, install dependencies and verify the ccusage runtime:

```sh
brew install uv ccusage gh rust zstd
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

The ccusage runtime must pass synthetic pricing probes below, at, and above the 272K request
boundary, including cached input and Fast mode. The installer builds pinned upstream revision
`98a1b6a88292ef00153508874a33685a81eac1e6` in a local cache. A compatible system runner is also
accepted. Verify on every writing device before upgrading its writer: a failed probe stops all
ccusage-backed collection while preserving stored data. Ordinary shell `ccusage` remains unchanged.

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
collection failures abort reconciliation before writes, but empty sources and partial DSH reads
can still omit data. Absent dates retain their values; present dates can be lowered. The option
affects all collected stores and cannot reconstruct missing logs.

A failed push keeps a local commit for retry; dirty files block day switching. A failed rollover
keeps completed branches. Malformed canonical JSON stops heatmap generation. Check the installed
writer's branch, revision, source summaries and stderr before retrying.

The repository is public. Never commit prompts, session exports, repository names, hostnames,
usernames, absolute user paths, session identifiers or private journals. Public-data validators
remain in place for all retained snapshots.
