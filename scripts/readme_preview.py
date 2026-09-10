#!/usr/bin/env -S uv run --script
"""Generate static README review copies, optionally with marked fictional data."""
from __future__ import annotations
import argparse
from datetime import date, timedelta
from pathlib import Path
import shutil
from readme_data import read_model
from readme_dashboard import panels


def example_model():
    """Fictional layout fixture. Never written to data/ or used by production."""
    result={}
    configs=[('codex','gpt-6-astra','low',110),('codex','gpt-6-astra','medium',80),
             ('claude','claude-opus-4-6','xhigh',44),('traex','gpt-5.5','medium',36),
             ('dsh','deepseek-v4-pro','high',12)]
    for role,scale in [('work',1),('personal',.37)]:
        days={};events={}
        for i in range(28):
            d=(date(2026,8,1)+timedelta(days=i)).isoformat()
            days[d]=[dict(harness=h,model=m,effort=e,tokens=round(n*100000*scale*(.4+((i*7+j*3)%11)/10)))
                     for j,(h,m,e,n) in enumerate(configs)]
            human=round((5+(i*3)%17)*scale)
            events[d]=dict(valid=True,humanComments=human,humanTriggeredRuns=human+(8 if i%5==0 else 1),
                           triggeringHumanComments=human,reviewReturns=3 if i%6==0 else 0,reopens=1 if i%10==0 else 0)
        groups={g:dict(zip(['zero','oneTwo','threeFive','sixTen','elevenPlus'],ns))
                for g,ns in [('standalone',[6,9,7,3,2]),('parent',[1,2,2,3,4]),('child',[9,7,2,1,0])]}
        result[role]=dict(days=days,sources=['codex','claude','traex','dsh'],
            assignment=[dict(harness=h,model=m,effort=e,issues={'done':round((20-i*3)*scale)}) for i,(h,m,e,n) in enumerate(configs)],
            assignmentDate='2026-08-28',activity=dict(status='ok',lastSuccess='2026-08-29',
            metrics={'activity':{'effectiveFrom':'2026-08-01'}},days=events,
            current={'available':True,'humanCommentDistribution':groups}))
    return result


def build(root, output, example=False):
    # Preview copies must not overwrite the checkout or its public assets.
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError('preview output must be outside the repository')
    assets=output/'assets';assets.mkdir(parents=True,exist_ok=True)
    for name,svg in panels(example_model() if example else read_model(root)):
        if example:
            mark='布局示例 · 虚构数据' if name.endswith('-zh.svg') else 'LAYOUT EXAMPLE · FICTIONAL DATA'
            svg=svg.replace('</svg>',f'<text x="480" y="15" text-anchor="middle" font-size="10" fill="#df8e1d">{mark}</text>\n</svg>')
        (assets/name).write_text(svg)
    shutil.copyfile(root/'assets/token-activity.svg',assets/'token-activity.svg')
    for name,notice in [('README.md','Layout example: new panels use fictional data; the heatmap uses real history.'),
                        ('README_zh-CN.md','布局示例：新面板使用虚构数据；热力图使用真实历史。')]:
        text=(root/name).read_text()
        if example:
            text='> **'+notice+'**\n\n'+text
        (output/name).write_text(text)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--example',action='store_true')
    args=parser.parse_args()
    build(args.root,args.output,args.example)
