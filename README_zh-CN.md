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
> 用量是脚本自动推到这个公开仓库的，所以采集的只有聚合数字——哪天、哪个工具、多少
> token，仅此而已。提示词、会话内容、仓库名、主机名、用户名、工作目录，这些能还原出
> 我具体在干什么的信息一概不采。

Aether Ledger 记录我的 coding agent——Claude Code、Codex、TRAE CLI——每天烧掉多少
token，并画成两张图：一张看总量和趋势，一张看每个场景各靠哪个工具。早期通过 OpenCode
跑的用量也在账本里，统一记在 Legacy 一栏。

## 用量总览

![Aether Ledger 用量面板](assets/token-activity.svg)

金额按记录到的模型用量折算成 API 等价成本，不是真实的订阅账单。Active days 统计的是
token 总量不为零的自然日。

## 分场景

![Aether Ledger 近 30 天分场景用量](assets/token-topology.svg)

这张图按场景拆最近 30 天的用量：行是场景——Work、Personal、Development（常驻开发机
加按需拉起的 GPU 节点）——列是工具，格子里是该工具在这个场景里占的份额。

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
