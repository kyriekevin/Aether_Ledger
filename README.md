# Aether Ledger

[English](README.md) | [简体中文](README_zh-CN.md)

> The AI compute ledger of the **Nightglass Protocol**.

Aether Ledger records how I use Claude Code, Codex, and TRAE CLI—and makes that activity visible.
Historical OpenCode-launched usage remains in the ledger as a legacy harness bucket.
Tokens are not skill points; they are the compute resources I spend while turning ideas into
useful work.

## Activity

![Aether Ledger activity dashboard](assets/token-activity.svg)

Costs are API-equivalent estimates based on the captured model usage, not an actual subscription
bill. Active days count every calendar day with a positive token total.

## Composition

![Aether Ledger compute composition](assets/token-composition.svg)

Composition highlights recent shifts against the lifetime baseline. `Development` combines
persistent devboxes with on-demand GPU trail workers.

## Ledger

The public-safe aggregates live under [`data/`](data/), grouped by durable role (`personal`,
`work`, and `devbox`) plus anonymized ephemeral `trail` workers. No prompts, sessions, repository
names, hostnames, usernames, or working directories are recorded.

## Automation

Updates land throughout the day on `usage/YYYY-MM-DD`. After midnight in Asia/Shanghai, the
rollover workflow squash-merges the completed day into `main`, refreshes the dashboard, and opens
the new day's branch. See [Operations](docs/operations.md) for setup, schemas, and recovery.
