"""Static SVG panels for GitHub README; no script, controls or external assets."""
from __future__ import annotations
from collections import Counter
from datetime import date, timedelta
from html import escape
from dashboard_theme import _theme_style_lines
from readme_data import read_model

COLORS = {'codex': '#1e66f5', 'claude': '#fe640b', 'traex': '#8839ef', 'dsh': '#ea76cb', 'legacy': '#6c6f85'}
NAMES = {'codex': 'Codex', 'claude': 'Claude Code', 'traex': 'TRAEX', 'dsh': 'DSH', 'legacy': 'Legacy'}


def number(n):
    if n is None:
        return '—'
    for bound, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if n >= bound:
            return f'{n / bound:.1f}{suffix}'
    return str(round(n, 2))


class SVG:
    def __init__(self, height, title):
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{height}" viewBox="0 0 960 {height}" role="img" aria-labelledby="title">',
                      f'<title id="title">{escape(title)}</title>', *_theme_style_lines(allocation=True),
                      '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}</style>',
                      f'<rect width="960" height="{height}" rx="16" class="dashboard-background"/>']

    def text(self, x, y, value, size=14, muted=False, anchor='start'):
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" class="dashboard-{"muted" if muted else "primary"}">{escape(str(value))}</text>')

    def rect(self, x, y, w, h, color=None, title=''):
        if w <= 0 or h <= 0:
            return
        paint = f'fill="{color}"' if color else 'class="dashboard-panel"'
        self.parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" rx="3" {paint}><title>{escape(title)}</title></rect>')

    def line(self, x1, y, x2):
        self.parts.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" class="dashboard-border"/>')

    def empty(self, y, message):
        self.rect(28, y, 904, 68)
        self.text(480, y+40, message, muted=True, anchor='middle')

    def finish(self):
        return '\n'.join([*self.parts, '</svg>'])+'\n'


def window(days):
    if not days:
        return []
    end = date.fromisoformat(max(days))
    return [(end-timedelta(days=27-i)).isoformat() for i in range(28)]


def compute(data):
    days = window(data['days'])
    counts = Counter()
    for d in days:
        for row in data['days'].get(d, []):
            counts[row['harness'], row['model'], row['effort']] += row['tokens']
    return days, counts


def activity(data):
    a = data.get('activity') or {}
    start = a.get('metrics', {}).get('activity', {}).get('effectiveFrom')
    valid = {d: r for d, r in a.get('days', {}).items()
             if a.get('status') == 'ok' and start and d >= start and r['valid']}
    return a, window(valid), valid


def daily(svg, y, days, series):
    """Series are (label, color, {date: number|None}); gaps are never zero bars."""
    if not days:
        return
    top, bottom = y+12, y+130
    maximum = max(1, *(sum(values.get(d) or 0 for _, _, values in series) for d in days))
    step = 850/len(days)
    for j in range(3):
        yy = bottom-j*(bottom-top)/2
        svg.line(64, yy, 918)
        svg.text(54, yy+4, number(maximum*j/2), 11, True, 'end')
    for i, d in enumerate(days):
        x, yy = 64+step*(i+.2), bottom
        if all(values.get(d) is None for _, _, values in series):
            svg.text(x+step*.3, bottom-3, '—', 10, True, 'middle')
        else:
            for label, color, values in series:
                n = values.get(d) or 0
                h = (bottom-top)*n/maximum
                yy -= h
                svg.rect(x, yy, step*.6, h, color, f'{d} · {label}: {n}')
        if i in (0, 7, 14, 21, 27):
            svg.text(x+step*.3, bottom+21, d[5:], 11, True, 'middle')
    x = 64
    for label, color, _ in series:
        svg.rect(x, bottom+39, 8, 8, color)
        svg.text(x+14, bottom+47, label, 12, True)
        x += max(160, len(label)*7+35)


def render_execution(data, role, lang):
    t = lambda zh, en: zh if lang == 'zh' else en
    days, combos = compute(data)
    assigned = Counter()
    for r in data['assignment']:
        assigned[r['harness']] += sum(r['issues'].values())
    assigned = +assigned
    assign_h = max(95, len(assigned)*35+38)
    mix_h = max(90, len(combos)*39+46)
    svg = SVG(212+assign_h+mix_h, t('工作' if role=='work' else '个人', role.title())+' · Execution')
    svg.text(28, 38, t('Issue 分配', 'Issue assignment'), 20)
    svg.text(932, 37, data.get('assignmentDate') or t('暂无快照','No snapshot'), 12, True, 'end')
    if not assigned:
        svg.empty(56, t('等待当前分配快照', 'Awaiting a current assignment snapshot'))
    else:
        maximum = max(assigned.values())
        for i, (h, n) in enumerate(assigned.most_common()):
            y=74+i*35
            svg.text(28, y, NAMES.get(h,h))
            svg.rect(260,y-10,590,7)
            svg.rect(260,y-10,590*n/maximum,7,COLORS.get(h))
            svg.text(930,y,n,14,False,'end')
    y=62+assign_h
    svg.line(28,y,932)
    svg.text(28,y+37,'Harness × Model × Effort',20)
    svg.text(932,y+37,number(sum(combos.values()))+' tokens' if days else '—',20,False,'end')
    if not days:
        svg.empty(y+58,t('等待 Model × Effort 的有效统计','Awaiting verified model × effort statistics'))
    elif not combos:
        svg.empty(y+58,t('所选有效日期内无 Token 用量','No token usage on selected verified dates'))
    else:
        maximum=max(combos.values())
        for i, ((h,m,e),n) in enumerate(combos.most_common()):
            yy=y+80+i*39
            # Full public configuration labels are kept intact, never truncated.
            svg.text(28,yy,NAMES.get(h,h),13)
            svg.text(140,yy,m,12)
            svg.text(420,yy,e,12,True)
            svg.rect(510,yy-10,326,7)
            svg.rect(510,yy-10,326*n/maximum,7,COLORS.get(h))
            svg.text(932,yy,number(n),14,False,'end')
    footer=y+mix_h+74
    svg.text(28,footer, t('有效日','Verified days')+f": {sum(d in data['days'] for d in days)}/28",12,True)
    svg.text(932,footer,f'{days[0]} — {days[-1]}' if days else t('缺失日期不补零','Missing dates are not zero'),12,True,'end')
    return svg.finish()


def render_process(data, role, lang):
    t=lambda zh,en:zh if lang=='zh' else en
    a, days, valid=activity(data)
    current=a.get('current',{})
    svg=SVG(590 if days or current.get('available') else 156, role.title()+' · Issue activity')
    svg.text(28,38,t('人工评论 · 每日','Human comments · daily'),20)
    svg.text(932,37,f'{days[0]} — {days[-1]}' if days else t('暂无有效日期','No verified dates'),12,True,'end')
    if days:
        daily(svg,60,days,[(t('人工评论','Human comments'),COLORS['codex'],{d:r['humanComments'] for d,r in valid.items()})])
    else:
        svg.empty(60,t('等待 Issue 活动的有效统计','Awaiting verified issue activity'))
    if not days and not current.get('available'):
        return svg.finish()
    svg.line(28,269,932)
    svg.text(28,302,t('每个 Issue 的累计人工评论','Lifetime human comments per current issue'),19)
    svg.text(932,302,a.get('lastSuccess') or '—',12,True,'end')
    if not current.get('available'):
        svg.empty(327,t('当前快照不可用','Current snapshot unavailable'))
    else:
        groups=current['humanCommentDistribution']
        maximum=max(1,*(n for group in groups.values() for n in group.values()))
        for j,(group,label) in enumerate([('standalone',t('独立任务','Standalone')),('parent',t('父任务','Parent')),('child',t('子任务','Child'))]):
            x=28+j*310
            svg.text(x,339,label,15)
            for i,(key,label) in enumerate(zip(['zero','oneTwo','threeFive','sixTen','elevenPlus'],['0','1–2','3–5','6–10','11+'])):
                yy=373+i*32;n=groups[group][key]
                svg.text(x,yy,label,12,True)
                svg.rect(x+49,yy-9,175,7)
                svg.rect(x+49,yy-9,175*n/maximum,7,COLORS['codex'])
                svg.text(x+265,yy,n,13,False,'end')
    svg.text(28,559,t('横条：Issue 数 · 三组共用比例尺','Bars: issue counts · shared scale across groups'),12,True)
    return svg.finish()


def render_trends(data, role, lang):
    t=lambda zh,en:zh if lang=='zh' else en
    cd,combos=compute(data);a,ad,valid=activity(data)
    if not cd and not ad:
        svg=SVG(150,role.title()+' · Trends')
        svg.text(28,38,t('用量与任务流程趋势','Usage and task-process trends'),20)
        svg.empty(56,t('有效统计尚未发布','Verified statistics are not yet published'))
        return svg.finish()
    svg=SVG(845,role.title()+' · Trends')
    svg.text(28,38,t('Token · 按 Harness','Tokens · by harness'),20)
    svg.text(932,38,f'{cd[0]} — {cd[-1]}' if cd else '—',12,True,'end')
    series=[]
    for h in COLORS:
        if any(k[0]==h for k in combos):
            series.append((NAMES[h],COLORS[h],{d:sum(r['tokens'] for r in data['days'][d] if r['harness']==h) for d in cd if d in data['days']}))
    if cd:
        daily(svg,56,cd,series or [('Token',COLORS['codex'],{d:0 for d in cd if d in data['days']})])
    else:
        svg.empty(56,t('暂无有效日期','No verified dates'))
    svg.line(28,260,932)
    svg.text(28,299,t('每条触发评论对应的执行数','Runs per triggering human comment'),20)
    svg.text(932,299,f'{ad[0]} — {ad[-1]}' if ad else '—',12,True,'end')
    if ad:
        daily(svg,317,ad,[(t('执行 / 触发评论','Runs / triggering comments'),COLORS['traex'],{d:r['humanTriggeredRuns']/r['triggeringHumanComments'] if r['triggeringHumanComments'] else None for d,r in valid.items()})])
    else:
        svg.empty(317,t('暂无有效日期','No verified dates'))
    svg.line(28,522,932)
    svg.text(28,561,t('流程回流 · 每日','Workflow returns · daily'),20)
    svg.text(932,561,f'{ad[0]} — {ad[-1]}' if ad else '—',12,True,'end')
    if ad:
        daily(svg,578,ad,[(t('Review → 进行中','Review → in progress'),COLORS['claude'],{d:r['reviewReturns'] for d,r in valid.items()}),(t('完成后重开','Reopened after done'),COLORS['traex'],{d:r['reopens'] for d,r in valid.items()})])
    else:
        svg.empty(578,t('暂无有效日期','No verified dates'))
    svg.text(28,817,t('—：缺失或无分母 · 各图独立比例尺','—: missing or no denominator · independent scales'),12,True)
    return svg.finish()


def panels(model):
    for role,data in model.items():
        for lang in ('en','zh'):
            for name,render in [('execution',render_execution),('process',render_process),('trends',render_trends)]:
                yield f'readme-{role}-{name}-{lang}.svg',render(data,role,lang)


def generate(root, check=False):
    from render_dashboard import _update_output
    return [(root/'assets'/name,_update_output(root/'assets'/name,svg,check=check)) for name,svg in panels(read_model(root))]
