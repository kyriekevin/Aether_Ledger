# README candidate preview

[简体中文](readme-preview_zh-CN.md)

Generate a self-contained local review artifact:

```sh
uv run python scripts/readme_preview.py --output /tmp/aether-readme-preview.html
```

Open the HTML in a browser. It defaults to real public aggregates. The optional **Layout example** uses fictional data for execution and task-process panels; the full-history heatmap always uses the real ledger. No collection, installation, publishing or ledger mutation occurs.

The preview separates Work and Personal and lets reviewers switch composition/trend, time window, task-process metric and structural task group. English and Chinese share the same data. Execution totals use the intersection of valid model/effort dates across all published sources for that role. The date window ends at the latest available valid date, which is shown explicitly. Missing days stay missing. Issue activity has its own valid-date window; assignment and comment distributions are current snapshots, unaffected by the period control.

This is an interaction prototype for README review. GitHub README images do not run these controls. Static defaults and expandable alternatives will be selected after visual review. Chat share, per-issue tokens and human interaction turns remain omitted because reliable public aggregates are unavailable. No health score is inferred.
