<h1 align="center">Aether Ledger</h1>

<p align="center">
  记录我如何把工作交给 coding agents。<br>
  <sub><strong>Nightglass Protocol</strong> 的一部分。</sub>
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
> 仓库只发布匿名聚合数据，不记录提示词、issue 标题、session 标识、仓库名称、主机名、
> 用户名或工作目录。

现在我的 agent 工作大多从 Multica 开始。Issue 用来保存工作和上下文；每次 run 记录由哪个
harness 执行、如何结束、运行了多久。以前这些信息散落在不同终端和历史记录里，现在可以在
同一个地方查看和管理。

Aether Ledger 把两本账放在一起：

- **工作账**来自 Multica 的 issues 和 runs；
- **算力账**来自 Claude Code、Codex、TRAE CLI 与 DSH 的本地 session 日志。

两本账来自同一套工作流，但不做逐任务关联。账本不会猜某个 issue 消耗了多少 token。

## 下发的工作

![Multica 工作概况](assets/work-overview.svg)

这张图从任务下发后开始记录：issue-days、已经结束的 runs、执行结果、运行时长，以及各 harness
承担了多少次执行。一个 issue 当天产生过 terminal run，就记作一个 issue-day；每天先去重，再汇总
到展示周期。公开数据只有计数，issue 内容仍留在 Multica 内。

## 执行方式

![Harness 与模型矩阵](assets/harness-model.svg)

矩阵展示最近 30 天里，各 harness 实际调用了哪些模型。Claude Code、Codex 和 TRAE 是日常执行
路径；DSH 也可以由 Multica 下发，但我主要用它理解和试验 harness。OpenCode Go 一类服务让 DSH
可以快速接入更多模型，它们是模型入口，不是另一种 harness。

Effort、reasoning、速度和额度仍有价值，但它们是模型调用的附属信息，不再单独构成一级分类。

## 复盘

![八周工作与算力复盘](assets/work-review.svg)

两行数据共用一条周度时间轴。上面是 Multica 记录的 terminal runs，下面是本地 harness 日志里的
token。放在一起可以观察工作与执行方式如何变化，但不把两种数据解释成逐任务归因。

## 算力足迹

![Aether Ledger 算力活动](assets/token-activity.svg)

Token 和 API 等价成本仍然保留，但它们只描述资源消耗，不评价产出或能力。金额根据模型用量估算，
不是实际订阅账单。

## 工作原理

```text
Issue ── Multica ──→ run ──→ Claude Code / Codex / TRAE / DSH
  │                               │
  └─ 每日工作聚合                  └─ 本地 session 聚合
                 │                │
                 └──── usage/YYYY-MM-DD
                              │  Asia/Shanghai 跨日后 rollover
                              ▼
                            main ── 重新生成公开面板
```

任务可以在本机执行，也可以从工作 MacBook 通过 SSH 到远程 devbox。Devbox 是执行位置，不再被描述成
一种独立的工作类型。直接运行各 harness 仍会进入算力账，但 Multica 已经成为组织和下发工作的主要入口。

## 账本

公开聚合数据保存在 [`data/`](data/) 下。高频更新写入当天的 `usage/YYYY-MM-DD` 分支；完整日期由
rollover workflow 合入 `main`。人工改动通过 PR 交付，并以 `make verify` 作为基本门槛。

## 文档

| 指南 | 内容 |
|---|---|
| [运维文档](docs/operations_zh-CN.md) | 采集、数据结构、分支生命周期、面板与恢复 |
| [仓库约定](AGENTS.md) | 贡献与交付规则 |

## License

MIT —— 见 [LICENSE](LICENSE)。
