<h1 align="center">Aether Ledger</h1>

<p align="center">
  <strong>Nightglass Protocol</strong> 的 AI 算力账本 ——
  每天自动更新的 coding agent token 消耗记录，只存匿名聚合数据。
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
> 这个仓库从第一天起就按公开来设计。账本里只有匿名的聚合数字——提示词、会话、仓库名、
> 主机名、用户名、工作目录，一概不记。

Aether Ledger 记录我用 Claude Code、Codex、TRAE CLI 花了多少 token，并把用量画成图。
早期通过 OpenCode 跑的用量也在账本里，统一记在 Legacy harness 一栏。花掉的 token
不是经验值，烧得多不代表我变强了；它是把想法做成结果要付的成本，学习、生活、工作
都算在内。

## 用量总览

![Aether Ledger 用量面板](assets/token-activity.svg)

金额按记录到的模型用量折算成 API 等价成本，不是真实的订阅账单。Active days 统计的是
token 总量不为零的自然日。

## 算力拓扑

![Aether Ledger 近期算力拓扑](assets/token-topology.svg)

拓扑图画的是最近 30 天里，哪些 agent 在为哪个环境干活。`Development` 一栏把常驻
开发机和按需拉起的 GPU trail worker 合在一起算。

## 账本

能公开的聚合数据都在 [`data/`](data/) 下，按长期角色分组（`personal`、`work`、
`devbox`），临时的 `trail` 节点匿名单列。

## 工作原理

```text
Claude Code · Codex · TRAE CLI
        │  定时同步
        ▼
usage/YYYY-MM-DD ── 当天聚合提交持续写入
        │  Asia/Shanghai 跨日后触发 rollover
        ▼
main ── squash merge 当天数据 + 重新渲染面板
        │
        └─→ 删除已完成分支，创建当天新分支
```

当天的用量只写进日期分支；`main` 只收 rollover workflow 合进来的完整一天。人工改动
一律走 PR，合并前过 `make verify`。

## 文档导航

| 指南 | 内容 |
|---|---|
| [运维文档](docs/operations_zh-CN.md) | 安装、机器身份、分支生命周期、数据结构、面板与恢复 |
| [仓库约定](AGENTS.md) | 提交与交付的约定 |

## License

MIT —— 见 [LICENSE](LICENSE)。
