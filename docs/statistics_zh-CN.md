# 统计契约

[English](statistics.md) · [运维文档](operations_zh-CN.md)

新统计需要显式启用，不改写旧 token 存储，也不切换图表。Work 和 Personal 各自在自己的机器上
采集。代码不预填生效日期，也不自动补齐启用之前的新统计。

## 数据与计数

- `statistics_readers.py` 在内存中提取最小计量记录，不保留提示词、代码、工具参数、响应正文、
  仓库名称或工作目录。
- 私有 SQLite 日志位于 `~/.cache/aether-ledger/statistics-v1/<role>.sqlite3`，权限为 0600。
  其中保存哈希化的去重/session/run 关联、原始及规范化模型名、时间、配置观测和 token 构成。
  不得提交到仓库。
- `data/<role>/statistics.json` 只保存公开聚合、版本、来源状态、覆盖和对账结果，公开审计会
  按严格 schema 检查。
- `calls` 指有正 token 计量的去重调用观测，不是用户 turns、聊天数或全部尝试过的 API 请求。
  缺少 effort 的调用仍然计数，归入 Unknown。
- 新版 Codex 日志有带 response ID 的 `token_usage_record`。一个日志开始使用这条记录流后，
  后续 `token_count` 兼容报告不再计入用量；后者可能切换累计范围，或记录压缩调整量。
  额度观测仍从兼容报告读取。仅有旧格式的文件或前缀按不同累计计数去重；无法区分重置与重放时
  标记为 partial。原生记录流启用后若降级回旧格式，必须建立新的来源/版本边界。
- Claude 的流式更新按 message 保留一份完整用量和配置；DSH 使用 session + turn + step 去重。
  复制日志不会重复累计，跨来源重复会报告异常。源日志轮转后，私有日志仍保留已采事实。
- Model、effort、speed 和 token 构成一起保存。修正时移动整条事实，不分别取各桶最大值。
  Codex 的 input 包含缓存部分，先扣除缓存读取/写入，再求不重叠的构成总和。
- 可公开模型名由 `config/statistics-models.json` 管理，与价格表分离。未列入的名称留在私有日志，
  公开为 Unknown。执行日志中的配置观测不等于服务端对实际模型身份的证明。
  `codex-auto-review` 这类已记录路由保留原标签，不猜测底层模型或价格。

## 校验与有效期

公开组合之和必须等于每日总量，token 构成之和必须等于 token 总量，覆盖计数也必须来自同一组
组合。`identifiedCalls` 统计有 response ID 或稳定 DSH step 标识的观测；旧累计计数没有同样的保证。

与旧账本的比较保留为 `matched`、`mismatch`、`unknown` 或 `empty`。旧账本是对照，不是新原生
计量的黄金标准：全部具有稳定标识的原生记录，在完整日期通过校验后，即使与旧账不同也可以生效，
差异仍会展示。旧计数器或混合记录必须与旧账总量一致。不得默默混用两个版本的总量，也不能把差异
解释成历史已经修好。

`modelEffort` 还要求联合配置覆盖完整；`speed` 要求 speed 观测覆盖完整，缺失不能当 Standard。
`cost` 要求全部调用可定价，逐次使用当日有效价格、长上下文阈值和 Fast 倍率。缺价格，或可能存在
倍率但 tier 未知时，记为未定价；`estimatedCost` 只覆盖 `pricedCalls`。额度是该来源各窗口的已观测
峰值，不是账户余额，也不能跨来源相加。

来源状态为 `ok`、`partial`、`failed`、`unavailable`，诊断只保留枚举名称和数量。最近尝试和最近
成功采集分别记录。指标生效后，若当天及次日均成功扫描，可确认一个没有已记录活动的空日；失败或
漏采不产生零值。当天数据始终为 open。

每个来源、每项指标都有独立的版本、`eligibleFrom` 和 `effectiveFrom`：

1. 首次成功扫描开始观测，下一自然日才是最早可完整统计的日期，统一使用 Asia/Shanghai。
2. 日期结束、当天及次日都有成功扫描，且该指标的校验通过后，才设置生效日期。生效日取通过校验
   的统计日，不是随后确认的日期。尚无正用量观测时，不提前宣告指标已验证。
3. 修正某项指标的定义或归因算法时，递增 `METRIC_VERSIONS`（run 使用 `RUN_METRIC_VERSIONS`）中对应版本。新版本在首次成功扫描后
   重新确定最早可用日，其余指标保留原生效日期。旧版本元数据保留在私有日志中。
4. 使用数据时既要检查 `effectiveFrom`，也要检查每天的 `validMetrics`。后续缺口或修正可能使
   某些日期失效。`common_valid_days` 对组合视图取共同有效日期，不填洞，也不借用旧历史补满窗口。

## Multica

每台机器最多每小时读取一次 API，发生在 token 发布之后。只计入 runtime 角色与本机匹配的 run。
Run ID 在私有日志去重，状态和时长更新替换原记录。API 不再返回某条旧记录时保留原数据，不能仅凭
缺失断言删除。抓取失败不会发布半份 run 快照。Run 按开始日期归属，尚在排队时按创建日期归属。
时长为累计执行时间，不是用户等待时间；缺失或无效的结束时间单独计覆盖。
`runDuration` 有独立生效日，要求该批 run 的时长全部可知，运行次数可以更早生效。

当前 issue 分派复用同轮 API 分页单独刷新。其 `coveredRoles` 描述工作区快照，不代表采集机器。
展示时按用途过滤，不能把重叠工作区快照或每日快照相加当去重任务数。分派刷新失败不会丢弃已成功
读取的运行记录。

仅当 session ID 与运行起止时间唯一匹配时，才关联调用；重叠或模糊匹配保持未关联。
`linkedCalls`/`linkedTokens` 描述已关联的观测子集，不是完整 run 账单。当前 agent 配置不会套用到
过去的 run。现有 run API 没有不可变的配置/用量总数快照，实际测试的统计导出接口也拒绝访问，
因此 `runConfiguration` 和 `runUsage` 保持未生效。以后启用它们，需要独立的数据与验证契约。

## 启用与恢复

改动通过 PR 合入后，在每台长期使用的机器上分别执行：

```sh
uv run --script scripts/install_launchd.py --statistics
```

这会给该机器的定时同步增加 `--include-statistics`。Work 安装不会使 Personal 生效。
重新安装时保留该设置；只有显式传入 `--no-statistics` 才停用，私有日志不会被删除。
沿用既有的私有 Multica profile、workspace 和 runtime 角色配置，不把这些配置写入仓库。
旧 token 先发布，新统计随后单独发布；新解析器或 API 失败不会阻塞已成功的 token 数据。

正式启用前可做只读来源的预演：

```sh
uv run --script scripts/collect_statistics.py --role work --preview --no-multica
```

预演使用临时私有日志和输出目录，不会设置正式生效日期。去掉 `--no-multica` 可测试已配置工作区
的读取；个人机器改用 `--role personal`。

迁移机器时备份对应私有日志。如果公开快照已经存在，但私有日志缺失或为空，采集会拒绝覆盖。
恢复的私有日志若导致已发布调用/token 总量回退，也会停止发布；应先恢复最新日志。
独立命令的正式运行必须位于当天 usage 写入分支，并获取共享写入锁。
独立采集命令的 `--reconcile` 可明确接受较小的修正事实，仅限手动操作，定时任务不会传入。
它不会抹掉已轮转、此次未出现的旧观测。旧版采集器也不能降低私有日志中的指标版本。

本轮不新增工具/plugin/skill 使用量、用户 turns、会话时长或任务质量指标。
