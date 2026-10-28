# 运维说明

[English](operations.md)

## 采集范围

账本从本地会话日志采集每天的 token 和 API 等价成本，不查询 Multica 任务、issue、agent 或评论，也不采集 effort 和 quota。定价和校正仍需要模型名称及输入、输出、缓存 token 明细。

| 数据文件 | 来源 |
| --- | --- |
| `claude.json` | ccusage 读取 Claude 日志，包括写入同一日志目录的 Multica、Trae 启动的 Claude 会话 |
| `codex.json` | ccusage 读取普通 Codex 日志 |
| `codex-multica.json` | 发现 Multica 共享及任务私有的 Codex 日志目录后，交给 ccusage 读取 |
| `traex.json` | ccusage 的 Codex 解析器读取 TRAE CLI 目录，并统一模型名称 |

每个来源保留独立累计文件，避免混用累计上限。Multica Codex 日志发现会先对复制的 rollout 去重，再交给 ccusage，删除这些发现逻辑会漏算用量。DSH 和 OpenCode 已不再采集，对应数据文件已移除。

私有来源配置位于 `~/.config/token-activity/multica.json`，模板见 [`config/multica.example.json`](../config/multica.example.json)。`taskWorkspacesRoot` 指向任务私有 Codex 日志的上级目录。为兼容已有配置，仍接受 `profile` 和 `workspaceId`，但不会调用任务 API。残留的 `dshProfile` 会被忽略。

## 安装

Python 固定为 3.11。在 macOS 安装依赖并校验 ccusage：

```sh
brew install uv ccusage gh rust
uv run python scripts/ccusage_runtime.py --install
uv run python scripts/ccusage_runtime.py --check
mkdir -p ~/.config/token-activity
printf 'personal\n' > ~/.config/token-activity/node_name
make install
make health
```

按设备用途选择公开标签 `work`、`personal` 或 `devbox`。Git 需有推送权限；`work` 和 `personal` 采集端还需要已登录的 `gh`，用于恢复错过的每日归档。

<a id="ccusage-runtime-upgrade"></a>

### ccusage 运行时

| 项目 | 当前值 |
| --- | --- |
| 版本 | `20.0.35+7a063a0`（v20.0.35 之后 16 个提交，2026-10-18） |
| 上游提交 | `7a063a09103184391dede2d6285b32a75903d328` |
| 安装位置 | `~/.cache/aether-ledger/ccusage/<revision>/ccusage` |
| 已校验长上下文模型 | `config/official-pricing.json` 中的 `ccusage.longContextModels` |

固定版本写在 `scripts/ccusage_runtime.py` 的 `REVISION`，并记录为定价表的 `ccusage.verifiedRevision`，测试保证两者一致。安装器在本地缓存中用 `cargo` 构建该版本。它是上游最后一个把 models.dev 快照放在源码树里的版本，长上下文边界由版本本身决定，不会在构建时下载。

ccusage 从内嵌快照读取每个模型的长上下文边界，价格覆盖文件无法设置该边界；快照中没有的模型会退回 200K。因此 `--check` 和 `--install` 会对 `longContextModels` 中的每个模型，分别在 272K 请求边界以下、恰好等于及以上做合成定价检查，覆盖缓存输入和 Fast 模式。系统自带的 ccusage 只有通过同样检查才会被使用。检查失败会停止所有依赖 ccusage 的采集，但保留已有数据。终端里的普通 `ccusage` 命令不受影响。

升级步骤：

1. 选择一个上游版本，其 models.dev 快照为每个新增长上下文模型提供官方边界。
2. 更新 `scripts/ccusage_runtime.py` 中的 `REVISION` 和 `VERSION`、定价表中的 `verifiedRevision` 和 `minimumVersion`，以及本表和英文文档。
3. 把模型加入 `longContextModels`，再运行 `--install` 和 `--check`。`update_pricing.py` 报告长上下文定价 `UNSUPPORTED` 的模型尚未覆盖。
4. 新代码进入采集端之前，在每台采集设备运行 `--install`。二进制缓存按版本区分，采集端换到新版本却没有对应二进制时，检查会失败，所有依赖 ccusage 的采集会停止，直到完成安装。

## 定时同步

launchd 在每小时第 0、15、30、45 分钟运行。`make install` 创建或复用 `~/.cache/aether-ledger/writer` 独立 worktree，将路径和私有环境变量写入定时配置，再重新加载。可通过 `scripts/install_launchd.py --writer-worktree PATH` 指定位置。开发目录切换分支不会改变定时采集代码。

采集端先获取 Git 锁、同步当日分支，再读取各来源、合并累计文件并提交数据。一处来源失败会保留该来源数据，其余来源继续发布，最终返回 1。空来源可能表示没有活动、没有配置或日志缺失，不一定是错误；应同时检查 `latest_day` 和 `today_tokens`。

新代码随下一个日期分支生效。切换日期后本轮立即结束，下一轮才执行新代码。合并到 `main` 不会更新当日正在运行的采集端。旧的 `--include-statistics` 和 `--include-multica-tasks` 参数仍可传入，但不再执行额外采集，以免已有定时配置中断 token 同步。重新安装会移除这些参数，`make health` 会提示旧配置。

日志位于 `~/Library/Logs/aether-ledger/sync.log` 和 `sync.err.log`。健康检查覆盖依赖、配置、已安装脚本路径和来源发现，但不证明定价检查通过或数据已经推送。

### 手动同步

终端不会继承 launchd 的私有来源配置。使用已安装的命令、工作目录和环境运行：

```sh
uv run python - <<'PYTHON'
import plistlib
import subprocess
from pathlib import Path

plist = Path.home() / "Library/LaunchAgents/com.kyriekevin.aether-ledger.plist"
with plist.open("rb") as stream:
    agent = plistlib.load(stream)
command = agent["ProgramArguments"]
script = next(Path(arg) for arg in command if Path(arg).name == "sync_usage.py")
result = subprocess.run(command, cwd=script.parent.parent, env=agent["EnvironmentVariables"])
raise SystemExit(result.returncode)
PYTHON
```

该命令会写入并尝试推送。`--no-push` 仍会写本地累计文件，只跳过分支切换、提交和推送，不是预演。只读检查可使用 `--help`、`make health`、日志和 `git status`。`--reconcile-since YYYY-MM-DD` 允许从指定日期起接受更低的观测值，仅用于主动校正。

### Linux 与临时节点

launchd、`make install` 和 `make health` 只适用于 macOS。在 Linux 上，`make health` 会报告缺少 launchd 定时任务；临时节点还会报告 `node_name` 检查失败，两者都属正常。安装 uv、Rust，并确保 Git 有推送权限，再按[安装](#安装)一节构建并检查 ccusage。只有 `work` 和 `personal` 采集端需要 `gh`。

临时节点以临时身份采集（`CC_USAGE_TRAIL=1`）。如果更换节点后主目录可能丢失，把 `~/.config/token-activity/trail_id` 另存一份，并在第一次同步前恢复，详见[临时节点与恢复](#临时节点与恢复)。

从当日分支创建采集 worktree；当日分支尚不存在时，从 `main` 创建：

```sh
day="usage/$(TZ=Asia/Shanghai date +%F)"
git fetch origin
git worktree add --track -b "$day" ~/.cache/aether-ledger/writer "origin/$day" \
  || git worktree add --detach ~/.cache/aether-ledger/writer origin/main
```

用与 launchd 相同节奏的循环运行采集。锁保证每个节点只有一个循环，启动脚本可以重复调用：

```sh
#!/usr/bin/env bash
export TZ=Asia/Shanghai CC_USAGE_TRAIL=1
LOG_FILE="${LOG_FILE:-$HOME/.local/state/aether-ledger/sync.log}"
mkdir -p "$(dirname "$LOG_FILE")" ~/.cache/aether-ledger
exec 9>~/.cache/aether-ledger/loop.lock
flock -n 9 || exit 0
cd ~/.cache/aether-ledger/writer || exit 1
while true; do
  timeout 1200 uv run --script scripts/sync_usage.py >> "$LOG_FILE" 2>&1
  sleep $(( 900 - $(date +%s) % 900 + 5 ))
done
```

以脱离终端的方式启动，例如 `setsid nohup bash loop.sh >/dev/null 2>&1 &`。循环必须使用与被统计 agent 相同的 `CLAUDE_CONFIG_DIR` 和 `CODEX_HOME`，ccusage 依靠这两个变量找到日志；否则这些来源会返回空，而不是报错。如果推送使用专用密钥，还需设置 `GIT_SSH_COMMAND`。节点被回收时循环会被终止，因此需要在每个新节点上重新调用启动脚本，例如放在 shell profile 或 agent 的会话启动 hook 中。

## 定价与数据

数据位于 `data/{work,personal,devbox}/` 或 `data/trail/`，JSON 以日期为键。每天保存 `totalTokens`、`totalCost`，以及来源可提供的模型 token 明细；定价来源标记不完整或尚未定价的观测。只有上述七类 token 文件计入热力图，其余保留数据仍接受公开数据审计。

价格来自 `config/official-pricing.json`。ccusage 离线运行，使用生成的价格覆盖文件，同时保留按请求识别 Fast 和长上下文的能力，以正确计费。缺少官方价格的模型仍计入 token，暂记零成本并标注 `costSource: "unpriced"`。后续补入适用于该日期的价格后，即使 token 不变也可修正成本。这里统计的是 API 等价成本，不是订阅账单。

```sh
uv run --script scripts/update_pricing.py
uv run --script scripts/update_pricing.py --apply --effective-from YYYY-MM-DD
```

正常累计合并可防止日志缺失或轮转导致已有观测减少。修改采集器时需保留模型明细、来源隔离、定价来源和校正行为。不要手改生成的数据文件。

## 分支与验证

高频提交进入按 Asia/Shanghai 日期命名的 `usage/YYYY-MM-DD`。每日工作流 squash 合并已结束日期、重新生成 `assets/token-activity.svg`、校验并推送 `main`，然后删除已完成分支、创建当日分支。必须保留该顺序，让发布失败时仍有来源分支可恢复。采集端可请求补跑错过的每日归档。

人工修改通过 PR 提交，使用 Conventional Commit 标题和 no-reply 邮箱。交付前运行 `make verify`，覆盖测试、公开数据审计、热力图新鲜度、Python 编译和空白检查。CI 还检查整个传入提交范围及作者邮箱。GitHub squash 合并也需启用邮箱隐私。只有每日工作流直接推送 `main`。

用 `uv run --script scripts/render_dashboard.py` 重新生成热力图。它汇总所有标准 token 文件，以最新有活动的日期为终点，展示每日活动及 token、cost 汇总。`--check` 只校验，不写文件。

需要日内数据的下游应读取独立采集 worktree 的标准文件，或远端当日分支；`main` 只包含已结束日期。确认发布时需对比实际远端分支，不能只凭本地 upstream 引用、退出码 0 或来源读取完成。

## 临时节点与恢复

临时节点使用 `CC_USAGE_TRAIL=1` 生成持久匿名 ID，也可提供稳定的节点 ID，采集器会对其哈希。本地 `~/.config/token-activity/trail_id` 应跨重启保留。迁移节点时沿用已有身份，重新生成可能让同一份用量以第二个名字上传。身份文件无效时停止采集，不会静默生成新身份。

压缩前先运行 `uv run --script scripts/compact_trails.py --dry-run`。仅在一个采集端执行压缩：最新数据距今超过七天的非活跃节点会累计到 `data/trail/rollup`，并在同一个提交里移除原节点目录。

校正前先保存累计文件副本，确认来源完整。明确的采集失败会在写入前中止校正，但空来源仍可能缺数据。读取中没有出现的日期保留原值，出现的日期则可能被调低。该选项影响所有采集文件，也无法重建缺失日志。

推送失败会留下本地提交供下次重试；脏文件阻止日期切换；每日归档失败会保留已结束分支；标准 JSON 损坏会阻止热力图生成。重试前检查已安装采集端的分支、代码版本、来源摘要及错误日志。

仓库公开发布。不得提交提示词、原始会话导出、仓库名、主机名、用户名、用户绝对路径、会话标识或私有日志数据库。所有保留快照继续接受公开数据校验。
