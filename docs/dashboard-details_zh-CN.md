# 看板详情

[English](dashboard-details.md) · [README](../README_zh-CN.md)

README 围绕 issue、agent 配置和实际算力用量展开，保留活动热力图。新图分别生成中英文版本，
说明放在 README 正文，不再塞进图中。

## Issue 分派快照

`uv run --script scripts/multica_dispatch.py` 读取已配置的 Multica 工作区，生成
`data/multica-dispatch.json`，沿用本地采集配置和 runtime 角色映射。这是手动刷新命令，没有
新增定时采集。随后运行 `uv run --script scripts/render_dashboard.py` 生成图表；`--check`
会检查全部四张新图。

采集器在内存中关联当前 issue 的 assignee、agent 和 runtime，公开结果只保留用途、harness、
规范化模型名、effort、agent 数量和 issue 状态计数。分页重复的 issue 只计一次。Agent、runtime
和 issue 的 ID、名称、提示词、项目信息都不写入快照。严格的公开字段白名单校验全部输出，
并验证总 issue 数等于各组合计数加上未分派/未映射的数量。

图表按 harness 分组，只展示已分派 issue 的配置；跨用途的相同 model/effort 配置合并为一行。
零 issue 配置、零数量状态和为零的未分派计数在图上隐藏，原始聚合仍保留。
这些是当前配置，不是历史执行分组；已归档 agent 仍保留在来源快照中。
所有可见 issue 都计入，不限日期或状态；类别缺失时只回退到可识别的内置状态（含已归档），
仍无法识别的保留为未知。一个 issue 只按当前
assignee 计一次，重试和重新分派不另计。快照仅覆盖配置的工作区，目前有工作环境，没有个人环境。

模型规范化移除已知的 `opencode-go/` 供应商前缀和 `[1m]` 上下文后缀，并沿用账本模型别名。
TRAEX 的 effort 可能编码在模型查询参数中；查询参数与显式 effort 冲突时标为未知，
未填写 effort 时标为默认。只保留价格目录与明确列出的未定价公开模型 ID，未识别的模型或 effort
均标为未知。这张图展示配置，不证明某个模型实际执行了过去的请求；service tier 和上下文大小
不作为这张图的维度。

## 模型分配与前后期变化

保留的 `model-matrix*.svg` 文件名现在对应分组横条，不再绘制稀疏矩阵。每个 harness 只出现一次，
只展示前 28 天或近 28 天有记录的组合。模型按近期用量、前期用量依次排序；仅在前期出现的模型
仍会保留，避免隐藏已经退出的用量。

每行有两条对齐的时期横条，同一 harness 内全部模型的前后期共用线性比例尺。
不同 harness 分别缩放，组标题列出总量供绝对量比较；Issue 横条则在分派图内共用一个比例尺。近期期末取最后一个 token 为正的日期，
也可通过 `--as-of` 指定；前期为紧接此前的 28 天。“新增”表示前期没有记录到 token，不是模型
发布时间。变化百分比比较已记录用量；缺失记录不能证明没有活动。原始日志仍含 model/effort
联合观测的部分可以重新解析，只有每日分项汇总的部分无法还原关联。

两张工作流图与活动热力图、诊断图共用 `scripts/dashboard_theme.py` 中的主题。Harness 颜色跨图
保持一致，实色为工作，浅色为个人。分派图若出现 devbox，使用更浅的同色条，并在来源覆盖标签和
条形提示中保留其身份。Issue 状态计数改为简短文字汇总，不再引入第二套颜色。

用量对比覆盖工作和个人，包含手动与 Multica 执行。Devbox/trail 历史保留在原有活动图和诊断图中，
不追溯改成工作用途。Codex/DSH 的 Multica 存储合回原 harness，OpenCode 归 Legacy。
缺失的模型归因仍明确列为“未归因”。

对比包含手动和 Multica 执行。现有存储将 model 与 effort 分开汇总，不能生成二者联合的 token
分布，也不能计算 issue 成本。历史 run 缺少不可变的配置快照，不会用当前 agent 设置反推历史
用量归因。因此配置日期与用量日期分别展示。Token 衡量资源，不衡量质量或生产力。

## 渲染

`scripts/dashboard_story.py` 计算分组时期对比并渲染两种图表的中英文版本；`scripts/render_dashboard.py`
管理标准文件发现、日期、原子写入和更新检查。每日 rollover 一并暂存全部生成图。
本次看板改版不改变 token 存储或原有采集计划。

## 诊断图

以下图表沿用原有的 30 天/八周窗口，具体口径见[运维文档](operations_zh-CN.md#活动面板)。
它们的时间窗口与 README 总览不同。

### 活动历史

![活动](../assets/token-activity.svg)

### 环境与 harness 历史

旧环境视图仍将 devbox/trail 合为 Development。

![拓扑](../assets/token-topology.svg)
![拓扑历史](../assets/token-topology-history.svg)

### 模型分配

![模型分配](../assets/compute-allocation.svg)
![模型分配历史](../assets/compute-allocation-history.svg)

### 运行配置

![运行配置](../assets/runtime-profile.svg)
![运行历史](../assets/runtime-history.svg)
