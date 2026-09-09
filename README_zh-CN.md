<h1 align="center">Aether Ledger</h1>

<p align="center">
  <strong>Nightglass Protocol</strong> 的 AI 算力账本 ——
  持续更新、完全匿名化的 coding agent token 消耗记录。
</p>

<p align="center">
  <a href="README.md">English</a> · 简体中文
</p>

<p align="center">
  <a href="https://github.com/kyriekevin/Aether_Ledger/actions/workflows/verify.yml"><img alt="Verify" src="https://github.com/kyriekevin/Aether_Ledger/actions/workflows/verify.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/kyriekevin/Aether_Ledger?style=flat-square"></a>
  <img alt="Managed by uv" src="https://img.shields.io/badge/managed%20by-uv-261230?style=flat-square">
</p>

> [!IMPORTANT]
> 本仓库以公开为前提设计。账本只保存匿名化聚合数据——绝不记录提示词、会话、仓库名称、
> 主机名、用户名或工作目录。

工作和个人任务在 Multica 中成为 issue，再派发给按 **harness × model × effort** 配置的 agent。
这份账本记录这些组合，以及实际消耗的算力。

## 活动

![Token 活动](assets/token-activity.svg)

Token 包含缓存读取；金额是 API 等价成本估算，并非订阅账单。

## Issue 分派

![按 harness 分组的 Issue 分派](assets/agent-dispatch-zh.svg)

读取每台机器的当前分派记录，按 harness 和 model × effort 分组，只展示已有分派的组合。
工作和个人各自提供自己的记录，快照日期分别显示。

## Harness × Model × Effort

![已验证的模型与 effort 用量](assets/model-matrix-zh.svg)

只使用指标生效后的有效日期。实色代表工作，浅色代表个人；图中列出已覆盖来源，取这些来源的
共同有效日期。连续积累 56 个有效日后才显示前后期对比，此前只显示已观测用量。
漏采不会当成零值，热力图继续保留完整历史。

[模型历史、effort、Fast、额度与指标口径](docs/dashboard-details_zh-CN.md)

## 文档导航

> **更新写入设备前：**每台设备都必须安装并验证[兼容的 ccusage 运行器](docs/operations_zh-CN.md#ccusage-runtime-upgrade)。否则所有依赖 ccusage 的新增采集都会停止，不仅是 Astra；已有数据会保留。

| 指南 | 内容 |
|---|---|
| [运维文档](docs/operations_zh-CN.md) | 安装、机器身份、分支生命周期、数据结构、面板与恢复 |
| [仓库约定](AGENTS.md) | 贡献与交付约定 |

## License

MIT —— 见 [LICENSE](LICENSE)。
