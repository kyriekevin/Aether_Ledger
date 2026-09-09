# Dashboard details

[简体中文](dashboard-details_zh-CN.md) · [README](../README.md)

## Sources and dates

The new charts read only `data/{work,personal}/statistics.json`. They never fall back to legacy
marginal token stores or the manually exported shared assignment snapshot. Collection and
activation rules are in [Statistics](statistics.md).

Assignment uses `multica.assignment` only when collection and assignment status are both `ok`.
Each file contributes only its own role's rows, preventing overlapping workspace snapshots from
being counted twice. Dates and covered roles are displayed. Workspace-wide unassigned counts
cannot be attributed to a role and are omitted. Identical harness/model/effort rows are combined;
zero-assignment configurations are hidden. This is current assignment, not historical execution.

Model usage includes only sources with an effective `modelEffort` metric. The chart explicitly
lists those sources; pending sources are excluded, not represented as zero. Dates are intersected
across the included sources and require `validMetrics` plus the effective date. Codex and DSH
Multica sources merge into their harness. Rows preserve model × effort; speed variants sum within
the same combination. No historical agent configuration is inferred.

The end date defaults to the day before the latest source collection attempt, or explicit `--as-of`.
The latest window spans 28 calendar days. Only its verified days contribute, with their count shown.
Two adjacent 28-day periods and change percentages appear only when all 56 days are verified.
Missing days never become zero or produce a misleading change percentage. With no observed
verified combination, the chart displays a waiting state. The original heatmap is unchanged.

## Rendering

Run `uv run --script scripts/render_dashboard.py`; `--check` verifies generated assets.
Both charts share the existing theme: harness hue, solid Work bars, lighter Personal bars.
Usage bars scale within a harness; issue bars share one scale. Bilingual explanations remain
in Markdown. Daily rollover publishes the generated assets. No collector or activation changes
are part of this PR. Tokens measure resources, not task quality or productivity.

## Diagnostic charts

These retain their existing 30-day/eight-week windows and metric definitions in
[Operations](operations.md#dashboard). Their time windows differ from the README overview.

### Activity history

![Activity](../assets/token-activity.svg)

### Environment and harness history

The older environment view includes devbox/trail as Development.

![Topology](../assets/token-topology.svg)
![Topology history](../assets/token-topology-history.svg)

### Model allocation

![Allocation](../assets/compute-allocation.svg)
![Allocation history](../assets/compute-allocation-history.svg)

### Runtime configuration

![Runtime](../assets/runtime-profile.svg)
![Runtime history](../assets/runtime-history.svg)
