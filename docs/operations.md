# Operations

[English](operations.md) | [简体中文](operations.zh-CN.md)

## Sources and retention

Each writer reads local usage through `ccusage daily --json` and classifies model breakdowns into
Claude, Codex, and OpenCode stores. Codex `image_gen` PNGs are counted separately by local file
mtime because they do not appear as LLM token events.

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
- Claude Code, Codex, or OpenCode local usage logs

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
stores only a SHA-256-derived `data/trail/node-<digest>` path, never the raw value.

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

If GitHub Actions is delayed, a writer will not create today while any older usage branch still
exists. It exits cleanly and catches up on the next scheduled run. This prevents today's branch
from being based on a `main` that lacks an earlier day's final data.

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

## Downstream consumers

`main` intentionally contains only finalized daily snapshots. A consumer that needs intraday
values, such as the Feishu signature pusher, should read the same checkout used by
`sync_usage.py`; that checkout follows today's usage branch. A separate clone pinned to `main`
will be up to one day behind by design.

## Dashboard

`scripts/render_dashboard.py` scans `data/` for canonical files named `claude.json`, `codex.json`,
or `opencode.json`. Files such as `codex_by_repo.json` are deliberately excluded.

The static SVG contains:

- latest completed snapshot, month, lifetime, and peak tokens with API-equivalent cost;
- active days, defined as calendar days with a positive aggregate token total;
- a trailing 53-week daily heatmap.

Only the rollover workflow commits the shared SVG. Individual machine writers commit only their
own data directory, avoiding generated-asset conflicts when machines push concurrently.

Intensity levels use distribution quartiles rather than a linear scale, so ordinary days remain
visible when trail workloads create very large peaks. The SVG contains aggregate token counts and
API-equivalent cost, but does not include model, machine, path, prompt, or repository-level data.

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

Claude entries contain daily token and raw API-equivalent cost totals:

```json
{
  "2026-04-07": {
    "totalTokens": 8946720,
    "totalCost": 0.44
  }
}
```

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
