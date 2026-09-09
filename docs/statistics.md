# Statistics contract

[简体中文](statistics_zh-CN.md) · [Operations](operations.md)

The new statistics path is opt-in. It does not rewrite the legacy token stores or switch any
chart. Work and Personal collect independently on their own machines. There is no hard-coded
activation date and no automatic backfill of pre-installation statistics.

## Data and counting

- `statistics_readers.py` extracts minimal measurement facts in memory. No prompt, code,
  tool arguments, response text, repository name, or working directory is retained.
- A private SQLite journal lives at `~/.cache/aether-ledger/statistics-v1/<role>.sqlite3`, with
  mode 0600. It holds hashed deduplication/session/run references, raw and normalized model
  labels, timestamps, configuration observations, and token components. Do not commit it.
- `data/<role>/statistics.json` contains only public aggregates, versions, source status,
  coverage, and reconciliation results. Its exact schema is checked by the public audit.
- `calls` means positive-token, deduplicated usage observations. It is not user turns, chat
  count, or all attempted API requests. Calls without effort still count in an Unknown bucket.
- Modern Codex logs provide `token_usage_record` with response IDs. Once that stream begins
  in a log, subsequent `token_count` compatibility reports are excluded from usage counting;
  they may switch cumulative scope or describe compaction adjustments. Quota observations
  still come from those reports. A legacy-only prefix/file uses distinct cumulative counters;
  ambiguous resets/replays mark the read partial. Downgrading a native-stream writer back to
  legacy-only output in the same log requires a new source/version boundary.
- Claude streaming updates retain one complete usage/configuration bundle per message.
  DSH uses session + turn + step identity. Copied logs are deduplicated. Cross-source overlap
  is reported rather than counted twice. The journal retains facts after source logs rotate.
- Model, effort, speed and token components stay together. Corrections move a whole fact
  between aggregate buckets; no independent maxima are combined. Codex cached input/write
  components are subtracted from inclusive input before disjoint components are summed.
- Models allowed for publication are listed in `config/statistics-models.json`, independently
  of prices. Unlisted values remain private and publish as Unknown. Configuration observed
  in execution logs is not a provider-side attestation of the model that served a request.
  A recorded route such as `codex-auto-review` keeps that label; its underlying model and price
  are not guessed.

## Checks and validity

Every public combination sum must equal its daily total, and each token total must equal its
component sum. Coverage counts must derive from those same combinations. `identifiedCalls`
counts observations with response IDs or stable DSH step identities; legacy counter-only
observations do not have that guarantee.

The comparison with the legacy ledger is retained as `matched`, `mismatch`, `unknown`, or
`empty`. The old ledger is a comparison, not the gold standard for new native instrumentation.
A closed day with a fully identified native stream can become valid even when the old ledger
differs; the difference remains visible. Counter-only/mixed streams require a matching legacy
total. Do not silently combine old and new totals or label a disagreement as repaired history.

`modelEffort` additionally requires complete joint configuration coverage. `speed` requires
complete observed speed coverage; missing tier is not Standard. `cost` requires every observed
call to be priced. Estimates apply the date-effective rate, per-request long-context threshold,
and Fast multiplier. A missing rate or an unknown tier with a possible multiplier is unpriced;
`estimatedCost` only covers `pricedCalls`. Quota is the maximum observed percentage by window
within this source, not an account-wide balance or something to sum across sources.

Source states are `ok`, `partial`, `failed`, and `unavailable`; diagnostics contain only enum
names and counts. Last attempt and last successful collection are separate. An already-valid
metric may report a verified empty day after successful scans on that day and the following
day. A failed or missing scan does not create a zero. Current-day data remains open.

Each source and metric has its own version, `eligibleFrom`, and `effectiveFrom`:

1. The first successful scan starts observation. The following calendar day is the earliest
   eligible complete day, in Asia/Shanghai.
2. A metric becomes effective only after a qualifying day has closed, successful scans cover
   that date and the next date, and its checks pass. Its effective date is that qualifying day,
   not the later validation date. No positive observation means no activation proof yet.
3. Change an affected entry in `METRIC_VERSIONS` (or `RUN_METRIC_VERSIONS` for runs) when correcting that metric's definition or
   attribution algorithm. Its eligibility restarts after the first successful scan of the new
   version. Other metrics retain their dates. Old version metadata remains in the private journal.
4. Consumers must check `validMetrics` on each day as well as `effectiveFrom`; later gaps and
   corrections can invalidate individual days. `common_valid_days` intersects the requirements
   for a multi-metric view without filling holes or borrowing legacy history.

## Multica

API reads run at most once an hour per machine, after token publication. Each machine selects
only runs whose configured runtime role matches its own. Run IDs are deduplicated privately;
status and duration updates replace the prior run bundle. Missing old API records are retained
because absence cannot prove deletion. A failed fetch never publishes a partial run snapshot.
Runs are dated by start, or creation while queued. Duration is summed execution time, not user
waiting time; missing/invalid completion timestamps have a separate coverage count.
`runDuration` has its own activation date and requires known duration for every run in the
qualifying cohort; run counts can become valid earlier.

Current issue assignments are refreshed separately using cached pages from the same API pass.
Their `coveredRoles` describe the workspace snapshot, not the collector machine. Filter by role
when presenting them, and never sum overlapping workspace snapshots or daily snapshots as
unique task counts. Assignment failure does not discard successful run collection.

Session IDs and run start/end intervals can link an observed call only when exactly one run
matches. Overlapping/ambiguous runs remain unlinked. `linkedCalls`/`linkedTokens` describe that
observed subset, not a complete run bill. Current agent settings are never applied to past runs.
The available run API has no immutable configuration/usage-total snapshot, and the tested
statistics-export endpoint denied access. Therefore `runConfiguration` and `runUsage` remain
unactivated. Future activation requires an independent source/verification contract.

Queued runs may move from creation day to start day. Publication checks retained run totals before fetching and after aggregation, rather than requiring each day to grow. Successful zero-run days are exported; they become valid only after a positive day activates the metric and both the day and next day were scanned successfully. Missing scan days remain absent.

## Rollout and recovery

After the change has landed through a PR, install on **each** durable machine:

```sh
uv run --script scripts/install_launchd.py --statistics
```

This adds `--include-statistics` to that machine's scheduled sync. Work installation does not
activate Personal. Reinstalling preserves this option; `--no-statistics` explicitly disables it
without deleting the journal. Existing private Multica profile/workspace/runtime-role configuration is
reused; none of it belongs in the repository. The new statistics output is published separately
after the legacy token publication, so failures cannot strand otherwise successful token data.

Before enabling production, run a read-only-source preview:

```sh
uv run --script scripts/collect_statistics.py --role work --preview --no-multica
```

The preview uses temporary journal/output paths and cannot set production effective dates.
Omit `--no-multica` to test the configured workspace read. On Personal use `--role personal`.

Back up the private journal when moving a durable role to another machine. If a public snapshot
exists but its journal is missing/empty, collection refuses to overwrite it. A restored journal
that would regress published call/token totals also fails closed. Non-preview standalone runs
require today's usage branch and take the shared writer lock. Restore the current journal
first. `--reconcile` on the standalone collector deliberately accepts corrected lower fact
bundles; it is a manual option and is never passed by the scheduler. It does not erase missing
rotated observations. An older collector cannot downgrade a journal's metric versions.

No tool/plugin/skill usage, user-turn count, chat duration, or task-quality metric is added here.
