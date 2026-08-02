# 运维说明

[English](operations.md) | [简体中文](operations.zh-CN.md)

## 数据来源与留存

每台写入设备通过 `ccusage daily --json` 读取本地用量，并按模型明细归类到 Claude、
Codex 与 OpenCode 数据文件。Codex 的 `image_gen` PNG 会按本地文件修改时间单独计数，
因为它们不会作为 LLM token 事件出现。

本地 Claude/Codex 会话日志会轮转。每日观测一旦进入本仓库，`sync_usage.py` 会为每台
设备、每个 agent 和每个日期保留观测到的最高 token 总量。成本跟随胜出的 token 观测，
从而避免价格表刷新后继续保留过时的高水位价格。

新设备只能恢复其本地日志中仍然存在的日期。

## 前置条件

- 安装 Homebrew 的 macOS
- `uv`
- `ccusage`
- Git，以及本仓库的已认证推送权限
- `work` 与 `personal` 写入设备上已认证的 GitHub CLI（`gh`），用于 rollover 恢复
- Claude Code、Codex 或 OpenCode 的本地用量日志

安装命令行依赖：

```sh
brew install uv ccusage gh
```

## 设备身份

长期设备使用三个有意公开的角色标签之一：`work`、`personal` 或 `devbox`。为当前
checkout 配置对应标签：

```sh
mkdir -p ~/.config/token-activity
printf 'personal\n' > ~/.config/token-activity/node_name
```

临时 trail worker 设置 `CC_USAGE_TRAIL=1`，也可以将其设为稳定的 worker ID。生产脚本
只会保存经 SHA-256 派生的 `data/trail/node-<digest>` 路径，不会保存原始值。

## 定时同步

仓库附带的 launchd agent 每 15 分钟运行一次。为当前 checkout 渲染模板：

```sh
uv run --script scripts/install_launchd.py
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.kyriekevin.aether-ledger.plist
launchctl kickstart -k "gui/$(id -u)/com.kyriekevin.aether-ledger"
```

安装器会迁移已有且获准的 `machine_name`，随后卸载并删除旧的
`com.kyriekevin.cc-cx-usage-data` agent，避免新旧定时任务同时运行。

日志位置：

- `~/Library/Logs/aether-ledger/sync.log`
- `~/Library/Logs/aether-ledger/sync.err.log`

## 每日分支生命周期

写入设备使用 Asia/Shanghai 自然日对应的 `usage/YYYY-MM-DD`。多台设备可以推送到同一
分支，因为它们分别拥有独立目录；生产脚本会在推送竞争时通过 rebase 重试。

`.github/workflows/daily-rollover.yml` 会在午夜后运行，并在 30 分钟后进行一次幂等重试：

1. 找出所有早于今天的 `usage/YYYY-MM-DD` 分支。
2. 按日期顺序将每个完整日期 squash 到 `main`。
3. 重新生成活动面板，并为每个日期创建一个快照提交。
4. 推送 `main`，仅删除已经成功发布的日期分支。
5. 从更新后的 `main` 创建当天分支。

如果 GitHub Actions 延迟，写入设备不会在仍存在旧 usage 分支时创建当天分支，而是
正常退出并在下一次定时同步时追赶。这样可以避免当天分支基于缺少前一日最终数据的
`main` 创建。

workflow 也支持手动触发。并发组会阻止 rollover 重叠运行；每次扫描全部旧日期分支，
因此第二次定时触发和手动恢复都是幂等且安全的。

具有定时同步的 `work` 与 `personal` 写入设备同时充当 GitHub scheduler 之外的
watchdog。00:50 宽限期后，它们会在正常的 15 分钟同步中检查是否仍存在旧 usage 分支；
如果存在，就使用本机已认证的 `gh` 会话 dispatch rollover workflow。workflow 的并发锁
保证两台设备同时发起恢复也是安全的。手动或 workload 触发的 `devbox` 与临时 `trail`
写入设备不承担 watchdog。恢复请求失败时会在同步日志中产生明确错误，并在下一次定时
同步时重试。

## 提交约定

人工改动使用 Conventional Commit，并带聚焦的 scope，例如
`docs(readme): explain the activity ledger`。自动写入使用
`chore(data): sync node-<digest> usage`；`main` 上的每日 squash 提交为
`chore(data): finalize YYYY-MM-DD snapshot`。Trail 压缩同样使用 `chore(data)` scope。
本地与 Kubernetes 写入脚本会强制使用公开安全的
`Aether Ledger <noreply@github.com>` 身份创建自动提交，因此不依赖、也不会暴露宿主机的
Git 身份。rollover workflow 则使用 GitHub Actions bot 身份。

## 下游消费者

`main` 只包含已经结束的每日快照。需要日内数据的消费者（例如飞书签名推送器）应读取
与 `sync_usage.py` 相同的 checkout；该 checkout 会跟随当天 usage 分支。单独固定在
`main` 的 clone 按设计最多会落后一天。

## 活动面板

`scripts/render_dashboard.py` 在 `data/` 中扫描名为 `claude.json`、`codex.json` 或
`opencode.json` 的规范文件，并明确排除 `codex_by_repo.json` 等文件。

静态 SVG 包含：

- 最近一次完整快照、本月、累计和峰值的 token 与 API 等价成本；
- Active days，即聚合 token 总量大于零的自然日数量；
- 最近 53 周的每日 token 热力图。

只有 rollover workflow 会提交共享 SVG。各设备写入脚本只提交自己的数据目录，从而
避免多台设备并发推送时发生生成文件冲突。

颜色强度按分布四分位数计算，而不是线性缩放，因此 trail workload 产生巨大峰值时，
普通日期仍然可见。SVG 只包含聚合 token 和 API 等价成本，不包含模型、设备、路径、
提示词或仓库级数据。

## 公开数据边界

提交数据仅限 `data/` 下按日期聚合的用量，使用公开长期角色 `work`、`personal`、
`devbox`，或临时 worker 的不透明 ID。生产脚本不会收集工作目录、仓库名称、提示词、
会话标识、用户名或主机名。公开数据审计会忽略并禁止 `codex_by_repo.json` 等仓库级导出。

发布或修改数据生产脚本前运行：

```sh
uv run --script scripts/audit_public.py
```

每日 workflow 在合并 usage 分支前也会执行同一审计。

## 数据结构

Claude 条目包含每日 token 与原始 API 等价成本：

```json
{
  "2026-04-07": {
    "totalTokens": 8946720,
    "totalCost": 0.44
  }
}
```

Codex 条目还可以包含按模型拆分的 token 和图片计数：

```json
{
  "2026-04-20": {
    "totalTokens": 248347,
    "totalCost": 0.6163031,
    "models": {
      "gpt-5.3-codex": {"totalTokens": 248347}
    },
    "imageCount": 0
  }
}
```

OpenCode 使用相同的按日期结构，也可以包含每个模型的汇总。由于当前 `ccusage` 的模型
明细不标识调用它的 agent，因此归类基于模型家族。

## Trail 压缩

只在一台写入设备上运行 `scripts/compact_trails.py`。最新数据早于七天前的 pod 会被累加
到 `data/trail/rollup`，并在同一个提交中删除。每个临时 pod 目录代表独立 worker，
因此 fold 使用加法聚合。

始终先预览：

```sh
uv run --script scripts/compact_trails.py --dry-run
```

## 恢复

- 数据推送失败时，本地提交会保留；下一次同步会在切换日期前重试。
- 工作区不干净时，自动日期切换会停止，不会把改动带入另一天。
- rollover 失败时会保留源分支，也不会基于过期的 `main` 创建当天分支。
- 规范 JSON 损坏时，面板生成会失败，不会静默发布不完整聚合。
