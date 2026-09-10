#!/usr/bin/env -S uv run --script
"""Build a self-contained review artifact; never writes generated ledger stores."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from dashboard_story import statistics_assignment
from render_dashboard import aggregate_daily, render_svg


def read_model(root: Path) -> dict:
    result = {}
    for role in ('work', 'personal'):
        path = root / 'data' / role / 'statistics.json'
        statistics = json.loads(path.read_text()) if path.exists() else {}
        sources = statistics.get('sources', {})
        # Include every published source in the coverage intersection. Unready
        # sources must not silently disappear and make partial usage look whole.
        valid_sets = []
        for source in sources.values():
            start = source['metrics']['modelEffort']['effectiveFrom']
            valid_sets.append({d for d, row in source['days'].items()
                               if start and d >= start and 'modelEffort' in row['validMetrics']
                               and source['status'] == 'ok'})
        common = sorted(set.intersection(*valid_sets)) if valid_sets else []
        days = {}
        for day in common:
            rows = {}
            for source, payload in sources.items():
                harness = source.removesuffix('-multica')
                for row in payload['days'][day]['combinations']:
                    key = (harness, row['model'], row['effort'])
                    rows[key] = rows.get(key, 0) + row['totalTokens']
            days[day] = [dict(harness=h, model=m, effort=e, tokens=n)
                         for (h, m, e), n in rows.items() if n]
        activity_path = root / 'data' / role / 'issue-activity.json'
        activity = json.loads(activity_path.read_text()) if activity_path.exists() else None
        assignment = statistics_assignment(root)
        result[role] = dict(days=days, sources=list(sources), activity=activity,
            assignment=[r for r in (assignment or {}).get('configurations', [])
                        if r['role'] == role and sum(r['issues'].values())],
            assignmentDate=(statistics.get('multica') or {}).get('assignment', {}).get('asOf')
                if (statistics.get('multica') or {}).get('assignment') else None)
    return result


def build(root: Path, output: Path):
    totals = aggregate_daily(root)
    last = max(totals)
    heatmap = render_svg(totals, last)
    template = (root / 'scripts' / 'readme_preview.html').read_text()
    payload = json.dumps(read_model(root), ensure_ascii=True).replace('<', '\\u003c')
    text = template.replace('__LEDGER_DATA__', payload).replace('__HEATMAP__', heatmap)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.root, args.output)
