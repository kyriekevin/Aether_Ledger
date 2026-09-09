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
> Public by design. The ledger keeps anonymized aggregates only — no prompts, sessions,
> repository names, hostnames, usernames, or working directories are ever recorded.

Work and personal tasks become issues in Multica. I dispatch them to agents configured as
**harness × model × effort**. This ledger tracks those combinations and the compute they use.

## Activity

![Token activity](assets/token-activity.svg)

Token use includes cache reads. Costs are API-equivalent estimates, not subscription bills.

## Issue allocation

![Issue allocation grouped by harness](assets/agent-dispatch.svg)

Current assignments from each machine, grouped by harness and model × effort. Only assigned
combinations appear. Work and Personal contribute their own rows; snapshot dates are shown separately.

## Harness × Model × Effort

![Verified model and effort usage](assets/model-matrix.svg)

Only verified days after each metric takes effect are included. Solid bars represent Work;
light bars represent Personal. The chart lists its covered sources and uses their common valid days.
A previous-period comparison appears after 56 consecutive valid days; until then, only observed
usage is shown. Missing collection is never treated as zero. The heatmap keeps its full history.

[Model history, effort, Fast, quota, and metric definitions](docs/dashboard-details.md)

## Documentation

> **Before upgrading a writer:** install and verify the [compatible ccusage runtime](docs/operations.md#ccusage-runtime-upgrade) on every writing device. Otherwise, all ccusage-backed collection stops, not only Astra. Existing data is retained.

| Guide | Covers |
|---|---|
| [Operations](docs/operations.md) | Setup, machine identity, branch lifecycle, schemas, dashboards, and recovery |
| [Repository guidance](AGENTS.md) | The contribution and hand-off contract |

## License

MIT — see [LICENSE](LICENSE).
