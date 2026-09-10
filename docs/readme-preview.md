# Static README preview

[简体中文](readme-preview_zh-CN.md)

The candidate uses only Markdown, static SVG images and native `<details>` sections. Work is
expanded by default; supplementary Work trends and Personal data are collapsed. There are no
scripted filters, buttons or dynamic charts. English and Chinese have equivalent layouts.

Regenerate the real assets with `uv run python scripts/render_dashboard.py`. `make verify`
checks their freshness. The production renderer only reads public repository aggregates.

For a separate visual review copy:

```sh
uv run python scripts/readme_preview.py --output /tmp/aether-readme-static
uv run python scripts/readme_preview.py --output /tmp/aether-readme-example --example
```

Each output contains both READMEs and their images. The example puts an explicit fictional-data
label on every new chart and on the document; the full-history heatmap remains real. It cannot
write inside the checkout. No preview data enters the ledger. Render the Markdown with raw HTML
enabled to inspect the native disclosure sections, or view it in a GitHub-compatible renderer.

Execution uses common verified model/effort dates across all published sources for each role.
Each 28-day window ends at the latest available valid date; gaps remain missing. Issue activity
uses its own effective dates. Assignment and comment distributions are current snapshots.
The three task groups share a bar scale but have unmatched ages and responsibilities. Comment
counts, execution amplification and workflow returns are observations, not quality scores.
