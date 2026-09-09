"""Shared light/dark palette for activity, workflow, and diagnostic charts."""

def _theme_style_lines(
    *, topology: bool = False, allocation: bool = False, runtime: bool = False
) -> tuple[str, ...]:
    light_levels = (
        (
            "    .topology-label-0 { fill: #6c6f85; }",
            "    .topology-label-1, .topology-label-2 { fill: #4c4f69; }",
            "    .topology-label-3, .topology-label-4 { fill: #eff1f5; }",
            "    .topology-label-agent-claude-3,",
            "    .topology-label-agent-claude-4 { fill: #4c4f69; }",
            "    .topology-work { fill: #fe640b; }",
            "    .topology-personal { fill: #1e66f5; }",
            "    .topology-development { fill: #8839ef; }",
            "    .topology-level-1 { fill-opacity: 0.22; }",
            "    .topology-level-2 { fill-opacity: 0.45; }",
            "    .topology-level-3 { fill-opacity: 0.68; }",
            "    .topology-level-4 { fill-opacity: 1; }",
            "    .heatmap-level-0 { fill: #ccd0da; }",
        )
        if topology
        else (
            "    .heatmap-level-0 { fill: #ccd0da; }",
            "    .heatmap-level-1 { fill: #179299; fill-opacity: 0.25; }",
            "    .heatmap-level-2 { fill: #179299; fill-opacity: 0.5; }",
            "    .heatmap-level-3 { fill: #179299; fill-opacity: 0.75; }",
            "    .heatmap-level-4 { fill: #179299; }",
        )
    )
    dark_levels = (
        (
            "      .topology-label-0 { fill: #a6adc8; }",
            "      .topology-label-1, .topology-label-2 { fill: #cdd6f4; }",
            "      .topology-label-3, .topology-label-4 { fill: #1e1e2e; }",
            "      .topology-work { fill: #fab387; }",
            "      .topology-personal { fill: #89b4fa; }",
            "      .topology-development { fill: #cba6f7; }",
            "      .heatmap-level-0 { fill: #313244; }",
        )
        if topology
        else (
            "      .heatmap-level-0 { fill: #313244; }",
            "      .heatmap-level-1 { fill: #94e2d5; fill-opacity: 0.25; }",
            "      .heatmap-level-2 { fill: #94e2d5; fill-opacity: 0.5; }",
            "      .heatmap-level-3 { fill: #94e2d5; fill-opacity: 0.75; }",
            "      .heatmap-level-4 { fill: #94e2d5; }",
        )
    )
    light_agents = (
        "    .agent-claude { fill: #fe640b; }",
        "    .agent-codex { fill: #1e66f5; }",
        "    .agent-traex { fill: #8839ef; }",
        "    .agent-dsh { fill: #ea76cb; }",
        "    .agent-legacy { fill: #6c6f85; }",
        *((
        "    .series-0 { fill: #1e66f5; }",
        "    .series-1 { fill: #fe640b; }",
        "    .series-2 { fill: #40a02b; }",
        "    .series-3 { fill: #8839ef; }",
        "    .series-other { fill: #9ca0b0; }",
        "    .signal-level-1 { fill: #179299; fill-opacity: 0.22; }",
        "    .signal-level-2 { fill: #179299; fill-opacity: 0.45; }",
        "    .signal-level-3 { fill: #179299; fill-opacity: 0.68; }",
        "    .signal-level-4 { fill: #179299; }",
        ) if allocation else ()),
    ) if allocation or topology else ()
    light_efforts = (
        "    .effort-none { fill: #9ca0b0; }",
        "    .effort-low { fill: #40a02b; }",
        "    .effort-medium { fill: #1e66f5; }",
        "    .effort-high { fill: #df8e1d; }",
        "    .effort-xhigh { fill: #8839ef; }",
        "    .effort-max { fill: #d20f39; }",
        "    .line-codex { fill: none; stroke: #1e66f5; }",
    ) if runtime else ()
    light_components = (
        "    .component-input { fill: #40a02b; }",
        "    .component-output { fill: #df8e1d; }",
        "    .component-cache-write { fill: #179299; }",
        "    .component-cache-read { fill: #7287fd; }",
    ) if allocation else ()
    dark_agents = (
        "      .agent-claude { fill: #fab387; }",
        "      .agent-codex { fill: #89b4fa; }",
        "      .agent-traex { fill: #cba6f7; }",
        "      .agent-dsh { fill: #f5c2e7; }",
        "      .agent-legacy { fill: #a6adc8; }",
        *((
        "      .series-0 { fill: #89b4fa; }",
        "      .series-1 { fill: #fab387; }",
        "      .series-2 { fill: #a6e3a1; }",
        "      .series-3 { fill: #cba6f7; }",
        "      .series-other { fill: #7f849c; }",
        "      .signal-level-1 { fill: #94e2d5; fill-opacity: 0.22; }",
        "      .signal-level-2 { fill: #94e2d5; fill-opacity: 0.45; }",
        "      .signal-level-3 { fill: #94e2d5; fill-opacity: 0.68; }",
        "      .signal-level-4 { fill: #94e2d5; }",
        ) if allocation else ()),
    ) if allocation or topology else ()
    dark_efforts = (
        "      .effort-none { fill: #7f849c; }",
        "      .effort-low { fill: #a6e3a1; }",
        "      .effort-medium { fill: #89b4fa; }",
        "      .effort-high { fill: #f9e2af; }",
        "      .effort-xhigh { fill: #cba6f7; }",
        "      .effort-max { fill: #f38ba8; }",
        "      .line-codex { fill: none; stroke: #89b4fa; }",
    ) if runtime else ()
    dark_components = (
        "      .component-input { fill: #a6e3a1; }",
        "      .component-output { fill: #f9e2af; }",
        "      .component-cache-write { fill: #94e2d5; }",
        "      .component-cache-read { fill: #b4befe; }",
    ) if allocation else ()
    return (
        "  <style>",
        "    .dashboard-background { fill: #eff1f5; }",
        "    .dashboard-panel { fill: #e6e9ef; }",
        "    .dashboard-primary { fill: #4c4f69; }",
        "    .dashboard-secondary { fill: #5c5f77; }",
        "    .dashboard-muted { fill: #6c6f85; }",
        "    .dashboard-accent { fill: #179299; }",
        "    .dashboard-border { stroke: #ccd0da; }",
        *light_agents,
        *light_efforts,
        *light_components,
        *light_levels,
        "    @media (prefers-color-scheme: dark) {",
        "      .dashboard-background { fill: #1e1e2e; }",
        "      .dashboard-panel { fill: #181825; }",
        "      .dashboard-primary { fill: #cdd6f4; }",
        "      .dashboard-secondary { fill: #bac2de; }",
        "      .dashboard-muted { fill: #a6adc8; }",
        "      .dashboard-accent { fill: #94e2d5; }",
        "      .dashboard-border { stroke: #313244; }",
        *dark_agents,
        *dark_efforts,
        *dark_components,
        *dark_levels,
        "    }",
        "  </style>",
    )
