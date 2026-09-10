# Issue activity collection

[简体中文](issue-activity_zh-CN.md)

The existing `install_launchd.py --statistics` option also enables the independent issue activity collector after this change is deployed. Each machine uses its own configured Multica profile and role. This branch does not install or enable production collection. Chat remains unsupported; no Chat totals are inferred.

The collector runs at most hourly after token publication, independently of usage-statistics success. It reads issue pages, full comments (including resolved threads), runs, and activity-only timelines. Nonzero CLI exits and truncation warnings reject the scan. A failed scan preserves the prior event journal, publishes failure state and no valid days, and does not prevent token publication. The current snapshot is unavailable rather than zero on failure.

## Private records

A separate role-local SQLite journal, `issue-activity-<role>.sqlite3`, lives in the existing statistics cache, with mode 0600. It holds hashed issue/comment/run/event/parent references, timestamps, actor categories, normalized states and run-to-comment links. Titles, descriptions, comment bodies, names, raw identifiers and prompts are never stored. Run start/end timestamps are retained privately for later comparable task-window analysis; no such cohort score is published yet.

Issue scope uses the current bound runtime, then assignee runtime, then a unique known role among its runs. Ambiguous or unmapped issues are excluded. Scan/scope counts are published. Events of an included issue include all its runs, even if it used several harnesses. Cross-role transfers cannot yet establish disjoint historical ownership: do not sum roles into a workspace-wide unique-issue total. Parent/child structure is structural, not an AI-generated task classification.

Records are deduplicated by hashed stable identity. Updates replace whole records. Conflicting duplicates reject the scan. Records missing from later API responses remain retained, because absence cannot prove deletion. Current issue distributions include only issues seen in the successful scan, but count their retained comment history. Back up the journal; a missing or regressed journal cannot overwrite published statistics. No automatic reset/reconcile is provided.

## Public aggregates

Only `data/<role>/issue-activity.json` is published:

- Daily human/agent/system/unknown comments, replies and created runs.
- Human-triggered runs and distinct triggering human comments, both attributed to the comment creation day. Late executions can revise that day's counts. Their ratio is execution amplification, not human effort or waste.
- Status transitions, review-to-progress returns and done-to-active reopenings. These are workflow events, not semantic rejection or quality judgments.
- Current issue/status counts and retained human-comment histograms, separately for parent, child and standalone issues. A node with both parent and children is classified as parent; the groups are disjoint. These are cumulative distributions, not matched-age cohorts.
- Retained record counts, scope counts, unresolved link/unknown-event coverage, scan status, and versioned activation dates.

No per-issue rows, labels, stable public hashes, content, task costs, Chat totals, or AI-derived health score are exported. The exact public schema is audited in CI.

## Dates and recovery

Initial history is retained privately. Daily output starts at collectionStarted and does not backfill earlier dates. Eligibility starts on the day after the first successful complete scan. A closed day becomes valid only when it and the next day both have successful scans, the current scan is successful, and there are no unresolved parent/trigger links or unknown comment/status events in retained records. Effective dates are recorded on validation, never prefilled. Successful zero-event days can be valid; missing scan days remain absent or invalid. `valid` means covered under this collection contract, not immutable or guaranteed complete beyond server-visible history.

This collector has its own metric version, independent of token/model statistics. Increment it when its counting or attribution contract changes. Existing rates and calendars remain unchanged. Future analysis must still separate task ages, parent/child roles and incomplete tasks before drawing conclusions about friction.
