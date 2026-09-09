# 看板详情

[English](dashboard-details.md) · [README](../README_zh-CN.md)

## 数据来源与日期

新图只读取 `data/{work,personal}/statistics.json`，不会回退到旧 token 分项汇总或手动导出的
共享分派快照。采集和生效规则见[统计口径](statistics_zh-CN.md)。

Issue 图只使用采集状态和分派状态都为 `ok` 的 `multica.assignment`。每份文件只贡献自己的
角色记录，避免两台机器的工作区快照重叠时重复计数。日期和覆盖角色分别显示。工作区级未分派
计数无法归属角色，因此不展示。相同 harness/model/effort 合并，零分派配置隐藏。
这张图展示当前分派，不代表历史执行。

模型用量只纳入 `modelEffort` 已生效的来源，图中列出这些来源；未生效来源不纳入，也不当成零。
取纳入来源的共同有效日期，每天都要求 `validMetrics` 和生效日期满足条件。Codex、DSH 的
Multica 来源合回各自 harness；保留 model × effort 组合，同组合的 speed 分项相加。
不反推历史 agent 配置。

截止日期默认取最近一次来源采集日期的前一天，也可用 `--as-of` 指定。近期窗口为 28 个自然日，
只累计其中的有效日并显示天数。全部 56 天有效后，才显示连续两个 28 天窗口及变化百分比。
漏采不补零，也不据此计算变化。没有已验证的用量组合时显示等待状态。原有热力图不变。

## 渲染

运行 `uv run --script scripts/render_dashboard.py`；`--check` 检查生成资产。
两张图共用原主题：harness 颜色一致，工作用实色，个人用浅色。用量横条在 harness 内共用比例尺，
Issue 横条全图共用比例尺。中英文说明放在 Markdown 中。每日 rollover 发布生成资产。
本 PR 不改采集器或生效规则。Token 衡量资源，不衡量任务质量或生产力。

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
