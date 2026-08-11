<h1 align="center">Aether Ledger</h1>

<p align="center">
  The AI compute ledger of the <strong>Nightglass Protocol</strong> —
  an anonymized, continuously updated record of how my coding agents spend tokens.
</p>

<p align="center">
  English · <a href="README_zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/kyriekevin/Aether_Ledger/actions/workflows/verify.yml"><img alt="Verify" src="https://github.com/kyriekevin/Aether_Ledger/actions/workflows/verify.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/kyriekevin/Aether_Ledger?style=flat-square"></a>
  <img alt="Managed by uv" src="https://img.shields.io/badge/managed%20by-uv-261230?style=flat-square">
</p>

> [!IMPORTANT]
> Usage is pushed to this public repository automatically, so only aggregate numbers are ever
> collected — which day, which tool, how many tokens. Prompts, sessions, repository names,
> hostnames, usernames, working directories: nothing that could reconstruct what I was actually
> working on.

Aether Ledger records how many tokens my coding agents — Claude Code, Codex, and TRAE CLI —
burn each day, rendered into two charts: one for totals and trend, one for which tool carries
each context. Historical OpenCode-launched usage remains in the ledger as a legacy bucket.

## Activity

![Aether Ledger activity dashboard](assets/token-activity.svg)

Costs are API-equivalent estimates based on the captured model usage, not an actual subscription
bill. Active days count every calendar day with a positive token total.

## By context

![Aether Ledger 30-day usage by context](assets/token-topology.svg)

This chart splits the last 30 days by context: rows are contexts — Work, Personal, and
Development (persistent devboxes plus on-demand GPU workers) — columns are tools, and each
cell is that tool's share of the row.

## Ledger

The public-safe aggregates live under [`data/`](data/), grouped by durable role (`personal`,
`work`, and `devbox`) plus anonymized ephemeral `trail` workers.

## How it works

```text
Claude Code · Codex · TRAE CLI
        │  scheduled sync
        ▼
usage/YYYY-MM-DD ── intraday aggregate commits
        │  daily rollover, after Asia/Shanghai midnight
        ▼
main ── squash-merged day + regenerated dashboards
        │
        └─→ completed branch deleted, today's branch created
```

Usage lands throughout the day on the dated branch; `main` only receives completed days from the
rollover workflow. Human changes go through pull requests gated by `make verify`.

## Documentation

| Guide | Covers |
|---|---|
| [Operations](docs/operations.md) | Setup, machine identity, branch lifecycle, schemas, dashboards, and recovery |
| [Repository guidance](AGENTS.md) | The contribution and hand-off contract |

## License

MIT — see [LICENSE](LICENSE).
