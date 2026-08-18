# 运维说明

[English](operations.md) | [简体中文](operations_zh-CN.md)

## 数据来源与留存

每台写入设备通过 `ccusage daily --json --by-agent` 读取本地用量，并把准确的 agent 明细
分别写入 Claude、Codex 与 OpenCode 数据文件。模型名只作为明细保留，不再用于推断调用端：
因此 Claude Code 经 cc-switch 等路由器调用的模型仍归入 Claude，同时保留真实模型名。
Codex 的 `image_gen` PNG 会按本地文件修改时间单独计数，因为它们不会作为 LLM token
事件出现。

TRAE CLI（traex）继续使用独立采集路径。它是 Codex 的一个分支，会以相同的
`rollout-*.jsonl` 会话格式写入
`~/.trae/cli`（而非 `~/.codex`）。`ccusage` 没有 `trae` agent，但其 `codex` 读取器认
`CODEX_HOME`，因此每台写入设备会额外跑一次只读的 `ccusage codex daily`，结果记入独立的
`traex.json`，且完全不触碰真实的 `~/.codex` 树。TRAE CLI 记录的模型名带大写
（`GPT-5.5`、`Gemini-3-Flash-Preview`），因此采集前会
先把 traex 会话镜像到一个临时 `CODEX_HOME`，仅对其中的 `"model"` 字段做归一化，真实的
`~/.trae/cli` 树不会被写入。归一化做两件事：转小写以匹配共享价格表；并把 TRAE CLI
的匿名 Claude 别名（`openrouter-1o`/`2o`/`3o`，含
`__max` 变体）解析为其背后的真实 Anthropic Opus slug（`claude-opus-4-6`/`4-7`/`4-8`），使其也能
定价。此外还会把 TRAE 的 Gemini 短配置名（`gemini-3.1-pro`、`gemini-3-flash`）合并到其模型
元数据记录的官方 preview slug，使新旧会话进入同一个累计模型桶。

Token 价格只来自 `config/official-pricing.json`。同步调用 ccusage 时强制使用 `--offline`，并注入
由该表生成的 override，因此 LiteLLM 在线表和 models.dev 都不能再改变已存金额。ccusage 仍负责
逐请求识别 Codex Fast/standard 与长上下文，仓库负责提供费率。未进入表的模型，以及
`gpt-5.3-codex-spark` 这类没有公开官方 API token 价的模型，只计 token、金额按 0。这是 API
等价估算，不是订阅账单；此时写入 `costSource: "unpriced"`。如果后续价格表加入对该日期生效的
官方价格，下一轮同步即使 token 未增长，也可以替换这笔暂记金额。

从厂商第一方来源刷新价格：

```sh
uv run --script scripts/update_pricing.py
uv run --script scripts/update_pricing.py --apply --effective-from YYYY-MM-DD
```

第一条命令只读，同时会发现所有长期节点和 trail 数据中已有的模型名。应用前检查输出中的
`CHANGE`、`UNPRICED` 与 `UNSUPPORTED`。官方页面抓取或解析失败时脚本直接失败，不会回退到代理商。
`effectiveFrom` 表示该次抓取的价格从仓库哪一天开始适用，不代表模型公开发布日期。初始条目使用
各模型在仓库中首次出现的日期。
DeepSeek、Google Gemini、Kimi 和 MiniMax 均直接读取各自 canonical API 定价页。已登记的 Gemini
模型在 TRAE 标示的 200K 上下文上限内使用标准文本/图片价格；采集到的 token 桶不包含音频和
Google 缓存存储时长费用。对于有独立长上下文档位的 OpenAI 模型，只有仓库确认
当前固定 ccusage 版本认识该模型的请求级切档阈值时才允许加入；否则输出 `UNSUPPORTED`，不会静默
套用 ccusage 的通用 200K fallback。

每日 rollover 只有在 main 已推送、完成的 usage 分支已删除、当天分支已创建后，才运行
`update_pricing.py --require-observed-prices`。如果已观测到的公开 provider 模型没有生效中的已审核
费率，本次 rollover workflow 会明确标红，但不会阻塞或丢弃 usage 快照；内部及明确不支持的模型族
只做提示。新增费率仍必须走人工审核 PR，因为生效日期决定历史补价范围；定时审计本身绝不写价格表。
Kimi 与 Gemini 解析器会从各自官方家族页面发现模型 ID，因此未来 Kimi K3 或 Gemini 新变体在首次
产生记录后会自动暴露，不再依赖人工维护硬编码模型名单。

`main` 要求 GitHub Actions 中名为 `checks` 的检查通过；检查失败或缺失都会阻止 PR 合并和人工
直推。daily rollover 是唯一的直写者：它先审计完成日快照，把同一个候选 SHA 发布到临时分支，再
用 `checks: write` 为该 SHA 写入成功的 `checks`，之后才推进 `main`。随后仍保持原有的故障安全
顺序：推进 `main`、删除已完成 usage 分支、删除临时候选分支、创建当天 usage 分支。这样人工改动
与自动收口共用同一门禁，同时不会把自动 writer 锁死。

本地 Claude/Codex 会话日志会轮转。每日观测一旦进入本仓库，`sync_usage.py` 会为每台
设备、每个 agent 和每个日期保留观测到的最高 token 总量。成本跟随胜出的 token 观测；某天一旦
有官方计价结果，同一 token 高水位下金额不再改变。需要有意修正历史时使用 `--reconcile-since`。
`unpriced` 暂记是例外，拿到官方价格后允许补价。没有模型明细的旧 Claude 日期无法可靠复算，
因此保留原金额，不猜模型。

新设备只能恢复其本地日志中仍然存在的日期。

## 前置条件

- 安装 Homebrew 的 macOS
- `uv`
- `ccusage` 20.0.19 或更高版本（需支持 `--by-agent`、价格 override 与已记录 Fast tier）
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
只会保存不透明的 `data/trail/node-<12 位十六进制>` 路径，不会保存原始值：稳定 worker ID
以其 SHA-256 摘要落盘，而 `CC_USAGE_TRAIL=1` 会随机铸造一次 ID 并保存在
`~/.config/token-activity/trail_id`。

这个文件就是该 worker 的身份。它以前由主机名派生，而主机名会漂移：`$HOME` 持久但主机名
变化的 worker 会以全新节点身份回来，把已经上报过的日子再传一遍；而折叠死亡 pod 是相加的，
于是那些天被翻倍。

**升级一台已经在某个 `node-…` 目录下有数据的 worker 时，请先做这一步**，否则它下次同步会
铸造新 ID 并把原目录变成孤儿：

```sh
mkdir -p ~/.config/token-activity
printf 'node-0123456789ab\n' > ~/.config/token-activity/trail_id   # 填它已有的目录名
```

全新 worker 不需要任何操作 —— 找不到文件就自己铸造。文件存在但格式不对时会直接中止，
而不是覆盖掉一个正在使用的身份。

## 定时同步

仓库附带的 launchd agent 会在每小时的 0、15、30、45 分运行。先让日常开发 checkout
离开当前 `usage/YYYY-MM-DD` 分支并更新 `main`，再安装：

```sh
git switch main
git pull --ff-only
uv run --script scripts/install_launchd.py
```

安装器会创建 linked、仅供 launchd 使用的 Git worktree：
`~/.cache/aether-ledger/writer`，并把该路径写入 agent。当天 usage 分支已存在时，writer
直接检出它；尚不存在时则从 `main` detached 启动，由原有 rollover guard 决定何时建分支。
源 checkout 不能仍占用当天 usage 分支，因为同一个本地分支只能被一个 worktree 检出。
可用 `--writer-worktree PATH` 改写默认位置。

writer 固定使用每日分支创建时继承的代码，白天新合入 `main` 的修改不会 merge 进当天 usage
分支。writer 切到次日分支后会在读取用量前结束本轮，下一次调用再从新代码快照启动。这样定时
提交仍然只包含数据，开发目录的切分支和 dirty files 也不会影响 launchd。linked worktree 仍
共享 Git 元数据；其他 worktree 并发执行 fetch、pull 或 rebase 时，某一轮仍可能 defer，下一轮
定时任务会自动重试。重复运行安装器会复用已注册的 writer worktree 并重新加载 agent。

安装器会迁移已有且获准的 `machine_name`，随后卸载并删除旧的
`com.kyriekevin.cc-cx-usage-data` agent。它会原子写入并重新加载当前 agent，让调度变更
立即生效，同时避免新旧定时任务一起运行。

日志位置：

- `~/Library/Logs/aether-ledger/sync.log`
- `~/Library/Logs/aether-ledger/sync.err.log`

每次运行都会在标准输出日志中写入开始和结束心跳。

### Git 并发访问

在这个 checkout 里跑 Git 的不止写入脚本一个进程：还有 `compact_trails.py`，以及人手
开的终端、编辑器和 linked worktree——后三者不受下面这把锁约束。下游的飞书签名推送器
曾经也会 pull 同一个工作区，而它的 launchd `WatchPaths` 监听的正是写入脚本产出的那几
个数据文件，所以写入动作本身就会把它叫醒，二者必然重叠；它贡献了下面这个竞态里最大
的一份，现在已经完全不跑 Git 了（见下）。Git 对一个工作区没有跨进程锁：并发 fetch 会互相
截断重写 `.git/FETCH_HEAD`，表现为 `fatal: Cannot rebase onto multiple branches.`；
远端 ref 更新会丢失 compare-and-swap（`cannot lock ref ... is at X but expected Y`）；
并发 fetch 还可能更新当前检出分支的 ref，随后 `pull` 会在另一个进程眼皮底下试图快进
工作树。

因此所有在本 checkout 里执行 Git 的进程都要获取同一把建议锁
`~/.cache/aether-ledger/git.lock`：

- 写入脚本（`sync_usage.py`）最多等待 60 秒，拿不到就跳过本轮。本地 store 是累积的，
  只要后续有一轮能跑起来，跳过就不丢数据。它在整个运行期间持锁（包括 `ccusage`），
  所以那次调用必须自带超时——无限挂起会把锁永久攥住，饿死这个 checkout 里的其他进程。
- `compact_trails.py` 同样获取这把锁，`--dry-run` 也不例外，因为它在汇报前也会 pull。
  它是人工触发的，所以拿不到锁就直接报错退出，不重试。
- 签名推送器完全不跑 Git，只读写入脚本上一轮落盘的内容，最多滞后一个写入周期。它以前
  会在这里 fetch + rebase，但这毫无收益（它没有自己的提交需要重放），却让它成了上面那个
  FETCH_HEAD 竞态最大的来源。它仍然最多等待 30 秒并在读取数据文件的整个过程中持锁：
  写入脚本是逐个替换各 agent 的 JSON 文件的，不持锁读取可能读到新旧混合的一份——只有
  持锁才能真正杜绝这一点。等不到锁时它会直接读，并校验读取前后所有 store 的 mtime
  没有变动，不满足就重试几次。这个校验弱于锁：它能发现"读取期间正在写"的写入方，但
  发现不了"停在自己两次文件替换之间"的写入方，那种情况下某个 agent 的 store 会比另一个
  新一代。之所以不整轮跳过：持续竞争下签名可能永远不更新，而求和值上一代的偏差下一轮
  就会自行纠正。

以后新增任何读写这个 checkout 的进程，都必须获取同一把锁。

## 每日分支生命周期

写入设备使用 Asia/Shanghai 自然日对应的 `usage/YYYY-MM-DD`。多台设备可以推送到同一
分支，因为它们分别拥有独立目录；生产脚本会在推送竞争时通过 rebase 重试。

`.github/workflows/daily-rollover.yml` 会在午夜后运行，并在 30 分钟后进行一次幂等重试：

1. 找出所有早于今天的 `usage/YYYY-MM-DD` 分支。
2. 按日期顺序将每个完整日期 squash 到 `main`。
3. 重新生成活动面板，并为每个日期创建一个快照提交。
4. 对即将落地的全部内容重新执行公开数据审计、活动面板时效检查、提交身份审计与空白字符检查。
5. 推送 `main`，仅删除已经成功发布的日期分支。
6. 从更新后的 `main` 创建当天分支。

第 4 步是这些提交唯一的关卡。该推送使用 `GITHUB_TOKEN` 认证，而用该 token 推送不会再触发新的
workflow run，因此 `verify.yml` 根本看不到它们。这里同时也是坏合并仍可挽回的最后时机，因为第 5 步会
立刻删除日期分支。该步失败时 `main` 保持不变、日期分支完整保留，下一次定时任务会重试。

第 3 步特意不带 `--as-of`：渲染器从数据推导日期，第 4 步也以同样方式校验，两者不可能不一致。若在
此固定日历日，则无活动的一天会被渲染成最新一列，而检查期望的是最后一个有活动的日期；这个偏差会
让每次重试都失败，日期分支被永久滞留，当天分支也永远建不出来——由于写入设备在旧日期分支尚存时
拒绝创建当天分支，整条同步链路会因此停摆。现在空闲的一天只是不产生快照提交。

如果功能 PR 意外带入了活跃 usage 分支中较早的生成数据快照，squash 可能会在两边实际
属于同一条累计历史时仍报告冲突。只有当每个冲突路径都是规范生成数据文件，并且能够证明
`main` 的版本曾原样出现在 usage 分支分叉后的历史中时，rollover 才采用 usage 分支的最终
版本。其他数据冲突、仅在分叉前匹配的旧版本或生成数据范围以外的冲突仍会安全失败，等待
人工检查。

如果 GitHub Actions 延迟，写入设备不会在仍存在旧 usage 分支时创建当天分支，而是
正常退出并在下一次定时同步时追赶。这样可以避免当天分支基于缺少前一日最终数据的
`main` 创建。

每次成功 fetch 后，写入设备还会在不存在本地独立提交时快进本地 `main`。已完成的远端
usage 分支消失后，只有当本地分支"没有任何属于它自己的东西"时才会删除本地副本，需要
同时满足两个条件：它的 `data/` 与 `origin/main` 上当日的日结快照一致；并且相对于它从
`main` 分叉出去的那个提交，它没有引入 `data/` 之外的任何改动。第二个条件特意与分叉点
比较，而不是与日结快照比较——如果拿整棵树去比快照，那么分叉之后才合入 `main` 的代码或
文档提交（仓库尚在演进期这很常见）都会被算成差异，把分支永久钉住。数据未发布、在
`data/` 之外留下净改动、或被其他 worktree 检出的分支都会保留，并各自打印对应日志；
比较本身失败（Git 报错）时同样保留，并与"存在差异"分开报告。

两个条件比的都是最终文件树而非提交历史，因为 squash 合并根本没留下可判定的祖先关系。
所以如果某个分支自己的提交互相抵消了（改了又还原、空提交），它会被判定为"没有自己的
东西"而删除。`git branch -D` 会连同该分支的 reflog 一起删掉，那些提交此后只剩下其他
引用还能够到它们（比如 HEAD 的 reflog——前提是这个分支曾在本地被检出过）；一旦没有
任何引用指向它们，就会在 Git 清理（gc/prune）时被回收。

workflow 也支持手动触发。并发组会阻止 rollover 重叠运行；每次扫描全部旧日期分支，
因此第二次定时触发和手动恢复都是幂等且安全的。

具有定时同步的 `work` 与 `personal` 写入设备同时充当 GitHub scheduler 之外的
watchdog。00:50 宽限期后，它们会在正常的 15 分钟同步中检查是否仍存在旧 usage 分支；
如果存在，就使用本机已认证的 `gh` 会话 dispatch rollover workflow。workflow 的并发锁
保证两台设备同时发起恢复也是安全的。手动或 workload 触发的 `devbox` 与临时 `trail`
写入设备不承担 watchdog。恢复请求失败时会在同步日志中产生明确错误，并在下一次定时
同步时重试。

## 持续集成

`.github/workflows/verify.yml` 在每个 PR、每次推送 `main` 以及手动触发时执行交付前检查清单，权限仅为
`contents: read`。PR 上的过期任务会被新提交取消；其余任务按 commit 分组，因此彼此既不取消也不排队。

它看不到每日 rollover：那次推送用 `GITHUB_TOKEN` 认证，不会再触发新的 workflow run，这正是
rollover 必须在推送前自行校验的原因。实际上 CI 覆盖的是 PR 本身，以及每个 PR 在 `main` 上产生的
合并提交。

| 检查 | 拦截 |
| --- | --- |
| `unittest discover -s tests` | 生产脚本、渲染器与 rollover 回归 |
| `audit_public.py` | 身份与路径泄露、超出 schema 的字段 |
| `render_dashboard.py --check` | 与已提交数据不匹配的 SVG |
| `py_compile scripts/*.py` | 无测试覆盖脚本中的语法错误 |
| `git diff --check` | 行尾空白与残留冲突标记 |
| `audit_public.py --history` | 携带个人邮箱的新提交 |

后两项针对本次提交区间执行，而不是工作区——全新 checkout 的工作区是干净的。PR 上的区间是 base
提交到分支 tip，而不是到 `HEAD`：`HEAD` 是 GitHub 合成的 merge commit，其作者是账号主邮箱且用完
即弃，审计它会导致每个 PR 都报泄露。push 上的区间从上一个 tip 开始。当不存在可用 base 时——分支
首次推送、force-push 覆盖了原 tip、手动触发——两项区间检查会跳过并明确说明，而不是退化成只看一个
提交、给出并不存在的覆盖假象。

提交身份审计特意限定区间：它要拦住新的泄露，同时不因早于该检查的历史提交而失败。

usage 提交不会开 PR，只落在 `usage/YYYY-MM-DD` 上，因此把 push 触发限制在 `main` 就足以让
15 分钟一次的同步不进入 CI，无需额外路径过滤。

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

`scripts/render_dashboard.py` 在 `data/` 中扫描名为 `claude.json`、`codex.json`、
`opencode.json` 或 `traex.json` 的规范文件，并明确排除 `codex_by_repo.json` 等文件。

活动 SVG 包含：

- 最近一次完整快照、本月、累计和峰值的 token 与 API 等价成本；
- Active days，即聚合 token 总量大于零的自然日数量；
- 最近 53 周的每日 token 热力图。

拓扑 SVG 交叉展示公开环境角色与最近 30 天活跃的 agent；每个 harness 沿用历史图中的同一
色相，行内颜色强度表达它在该环境中的占比；
历史上经 OpenCode 启动的用量归入 `Legacy`。
最近 30 天窗口与活动 SVG 使用同一个已完成快照作为截止日期。图中将常驻 `devbox` 与
按需 GPU `trail`
合并为 `Development`，但底层数据仍分开保存以服务采集与运维；不透明 trail node ID
不会进入生成资源。

拓扑和分配都将最近 30 日截面与独立的 8 周历史图配对。两张历史图使用相同的连续周桶，并
明确分成前 4 周与近 4 周。拓扑历史在 Work、Personal、Development 内使用绝对周度堆叠，
柱高保留环境总量，颜色展示 harness 替换；分配历史在每个 harness 内使用绝对量的 Top 3 模型
+ Other 堆叠。缺少模型覆盖的周保持为空白或灰色，不会被画成零。

因此 README 的阅读顺序是活动，然后依次查看拓扑、分配和运行的当前/历史配对：先看算力何时
发生、在哪里运行及如何变化，再看选择了哪些模型、组合如何迁移，最后看各 harness 的 effort
与路由选择如何变化。

Claude assistant 事件提供 effort，支持的模型还会提供 `thinking_tokens`。当前 Claude 环境不能
选择 Fast，因此不采集、不展示只有 standard 的速度字段；Claude 日志也没有 Codex 式额度字段。
Codex 提供 effort、reasoning、速度与额度。TRAE 是司内提供的 CLI，并非模型厂商，也不天然等于
低价平替；图中只如实展示其背后的模型组合。兼容版本的 TRAE 使用 Codex rollout 格式，因此
采集器也会在日志确实提供时读取 effort、速度、reasoning 与额度聚合。缺失的历史遥测明确显示
为不可用，不会从 token 总量或金额反推。Reasoning 强度只在各 harness 内部解释，不跨厂商比较。

七张 dashboard SVG 都通过 `prefers-color-scheme` 使用 Catppuccin Latte 与 Mocha 配色，
并适配 GitHub 的浅色与深色主题。

只有 rollover workflow 会提交共享 SVG。各设备写入脚本只提交自己的数据目录，从而
避免多台设备并发推送时发生生成文件冲突。

颜色强度按分布四分位数计算，而不是线性缩放，因此 trail workload 产生巨大峰值时，
普通日期仍然可见。发布的 SVG 包含聚合 token、分配视图中的模型名称，以及活动视图中的
API 等价成本；它们不包含设备身份、路径、提示词、会话或仓库级数据。

## 公开数据边界

提交数据仅限 `data/` 下按日期聚合的用量，使用公开长期角色 `work`、`personal`、
`devbox`，或临时 worker 的不透明 ID。生产脚本不会持久化工作目录、仓库名称、提示词、
会话标识、用户名或主机名。公开数据审计会忽略并禁止 `codex_by_repo.json` 等仓库级导出。

发布或修改数据生产脚本前运行：

```sh
uv run --script scripts/audit_public.py
```

每日 workflow 在合并 usage 分支前也会执行同一审计，CI 也会在每个 PR 上执行。

提交元数据同样是公开的。请将本地身份固定为 no-reply 邮箱：

```sh
git config user.email "<id>+<username>@users.noreply.github.com"
```

仅设置本地配置并不够。从网页端 squash 合并时，GitHub 会把作者改写为账号的主邮箱，因此还必须启用
**Settings → Emails → Keep my email address private**，否则每次合并 PR 都会重新公开该邮箱。
审计某个区间：

```sh
uv run --script scripts/audit_public.py --history origin/main..HEAD
```

## 数据结构

Claude 条目包含每日 token、原始 API 等价成本，以及按模型拆分的 token：

```json
{
  "2026-04-07": {
    "totalTokens": 8946720,
    "totalCost": 0.44,
    "costSource": "official",
    "models": {
      "claude-opus-5": {
        "totalTokens": 8946720,
        "inputTokens": 100,
        "outputTokens": 20,
        "cacheCreationTokens": 2000,
        "cacheReadTokens": 8944600
      }
    }
  }
}
```

详细 `models` 字段自此向后写入；`ccusage` 已轮转掉会话的历史日期保留原有的「仅总量」结构和旧金额。

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

只要 ccusage 能拆分，新观测就会为每个 harness 的每个模型保留四类 token。Codex session
事件还会贡献匿名的路由与额度遥测；Claude assistant 事件贡献 effort 与 thinking 遥测；兼容的
TRAE session 事件在确实提供时贡献与 Codex 相同的路由字段：

```json
{
  "routing": {
    "efforts": {
      "low": {
        "calls": 12,
        "totalTokens": 840000,
        "reasoningCalls": 12,
        "reasoningOutputTokens": 42000
      }
    },
    "speeds": {
      "fast": {"calls": 3, "totalTokens": 210000}
    }
  },
  "quota": {
    "windows": {"300": 64.0, "10080": 37.0},
    "limitReached": false
  }
}
```

`calls` 统计 session 遥测中观测到的模型调用，并不等于用户对话轮次。`reasoningCalls` 统计
harness 明确给出 reasoning 或 thinking token 字段的调用，包括字段明确为零的情况，避免把
缺失遥测算成零。Routing token 来自
session 事件流，不能当作独立采集的每日主账覆盖率。额度窗口 key 是匿名的分钟数。
Message 与 session 标识只在内存中用于去重，绝不写入仓库。历史总量仍然有效，但源日志
轮转后不会补出 component 或 routing 明细。旧条目可能仍保留原来的 `turns` 字段，其含义
同样是模型调用次数。

OpenCode 使用相同的按日期结构，也可以包含每个模型的汇总；其调用端归属同样直接来自
`--by-agent` 明细，而不是模型家族。

traex（`traex.json`）使用与 Codex 相同的按日期结构，代表司内的 TRAE CLI；其中记录的模型名
才描述该 harness 背后实际提供的能力。价格不会假设 Fast tier，已登记模型按官方 standard
价格计算，未知模型只贡献 token、金额按 0。若某个 TRAE 版本输出 Codex-compatible 路由字段，
采集器会保存其匿名聚合，否则保持不可用。

## Trail 压缩

只在一台写入设备上运行 `scripts/compact_trails.py`。最新数据早于七天前的 pod 会被累加
到 `data/trail/rollup`，并在同一个提交中删除。每个临时 pod 目录代表独立 worker，
因此 token、模型 component 和 routing 计数使用加法聚合；额度窗口保留观测到的最高压力。

始终先预览：

```sh
uv run --script scripts/compact_trails.py --dry-run
```

## 恢复

- 数据推送失败时，本地提交会保留；下一次同步会在切换日期前重试。
- 工作区不干净时，自动日期切换会停止，不会把改动带入另一天。
- rollover 失败时会保留源分支，也不会基于过期的 `main` 创建当天分支。
- 规范 JSON 损坏时，面板生成会失败，不会静默发布不完整聚合。
