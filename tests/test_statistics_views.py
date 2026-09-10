import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from dashboard_story import statistics_story, statistics_assignment, render_model_matrix


class StatisticsViewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, role, sources=None, multica=None):
        path = self.root / 'data' / role / 'statistics.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'sources': sources or {}, 'multica': multica}))

    def source(self, days, start='2026-09-10', effort='high'):
        return {'lastAttempt': '2026-09-13', 'metrics': {'modelEffort': {'effectiveFrom': start}},
                'days': {d: {'validMetrics': ['modelEffort'], 'combinations': [
                    {'model': 'model-a', 'effort': effort, 'speed': 'standard', 'totalTokens': 10},
                    {'model': 'model-a', 'effort': effort, 'speed': 'fast', 'totalTokens': 5}]} for d in days}}

    def test_joint_effective_days_intersect_and_no_legacy_fallback(self):
        self.write('work', {'codex': self.source(['2026-09-09', '2026-09-10', '2026-09-11']),
                            'codex-multica': self.source(['2026-09-10', '2026-09-11'], effort='low')})
        self.write('personal', {'codex': self.source(['2026-09-11', '2026-09-12'])})
        story = statistics_story(self.root)
        self.assertEqual(story.valid_days, ('2026-09-11',))
        self.assertEqual(story.contexts['work'].models['codex'], {'model-a · high': 15, 'model-a · low': 15})
        self.assertEqual(story.contexts['personal'].current, 15)
        self.assertFalse(story.comparable)
        self.assertNotIn('New</text>', render_model_matrix(story))
        self.write('work', {'codex': self.source(['2026-09-11'], start=None)})
        self.write('personal')
        legacy = self.root / 'data/work/codex.json'
        legacy.write_text('{"2026-09-11":{"totalTokens":999}}')
        self.assertIn('Waiting for verified', render_model_matrix(statistics_story(self.root)))

    def test_comparison_requires_every_day_and_filters_invalid_rows(self):
        end = date(2026, 9, 12)
        days = [(end - timedelta(days=n)).isoformat() for n in range(56)]
        source = self.source(days, start=min(days))
        self.write('work', {'codex': source})
        self.assertTrue(statistics_story(self.root).comparable)
        source['days'][days[30]]['validMetrics'] = []
        self.write('work', {'codex': source})
        story = statistics_story(self.root)
        self.assertFalse(story.comparable)
        self.assertEqual(story.contexts['work'].previous, 0)

    def test_overlapping_workspace_assignments_count_each_role_once(self):
        rows = [{'role': role, 'harness': 'codex', 'model': 'model-a', 'effort': 'high',
                 'issues': {'done': count}} for role, count in [('work', 3), ('personal', 2)]]
        shared = {'status': 'ok', 'assignmentStatus': 'ok', 'assignment': {
            'asOf': '2026-09-12', 'coveredRoles': ['work', 'personal'], 'configurations': rows,
            'unassignedIssues': 8}}
        self.write('work', multica=shared)
        self.write('personal', multica=shared)
        result = statistics_assignment(self.root)
        self.assertEqual(len(result['configurations']), 2)
        self.assertEqual(sum(r['issues']['done'] for r in result['configurations']), 5)
        shared['status'] = 'failed'
        self.write('personal', multica=shared)
        self.assertEqual(statistics_assignment(self.root)['coveredRoles'], ['work'])
        self.write('work')
        (self.root / 'data/multica-dispatch.json').write_text(json.dumps(shared['assignment']))
        self.assertIsNone(statistics_assignment(self.root))

    def test_verified_zero_usage_and_long_model_effort_stay_distinct(self):
        source = self.source(['2026-09-11'])
        row = source['days']['2026-09-11']['combinations'][0]
        row.update(model='nvidia/nemotron-3-ultra-550b-a55b:free', effort='xhigh')
        self.write('work', {'codex': source})
        svg = render_model_matrix(statistics_story(self.root))
        self.assertIn('… · xhigh', svg)
        for row in source['days']['2026-09-11']['combinations']:
            row['totalTokens'] = 0
        self.write('work', {'codex': source})
        self.assertIn('No usage in the verified period', render_model_matrix(statistics_story(self.root)))
