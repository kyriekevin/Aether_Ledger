# README 静态预览

[English](readme-preview.md)

候选版只使用 Markdown、静态 SVG 图片和原生 `<details>` 折叠区。默认展开工作数据，
补充工作趋势和个人数据收在折叠区。没有脚本筛选、按钮或动态图表，中英文布局对应。

运行 `uv run python scripts/render_dashboard.py` 重新生成真实图表，`make verify`
会检查图片是否最新。正式渲染器只读取仓库公开汇总。

生成独立的视觉评审副本：

```sh
uv run python scripts/readme_preview.py --output /tmp/aether-readme-static
uv run python scripts/readme_preview.py --output /tmp/aether-readme-example --example
```

每份输出包含中英文 README 和引用图片。示例版在每张新图及文档开头明确标注虚构数据，
完整历史热力图仍为真实数据。输出不能位于仓库内，示例数据不会进入账本。
用支持原生 HTML 的 Markdown 渲染器查看折叠效果，或用兼容 GitHub 的渲染器预览。

执行用量取每个角色所有已发布来源共同有效的 Model × Effort 日期。28 天窗口截至最近可用的
有效日，缺失日期保持缺失。Issue 活动使用独立的生效日期；分配与评论分布是当前快照。
三类任务的横条共用比例尺，但任务年龄与职责不同。评论数、执行放大与流程回流是观察指标，
不代表质量分数。
