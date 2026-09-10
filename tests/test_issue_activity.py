import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import issue_activity as activity
from usage_schema import SHANGHAI

class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.machine=self.root/'work';self.cache=self.root/'private'
        self.comments=[dict(id='c',created_at='2026-09-10T08:00:00+08:00',author_type='member',content='PRIVATE PROMPT',parent_id=None),
                       dict(id='a',created_at='2026-09-10T09:00:00+08:00',author_type='agent',content='PRIVATE ANSWER',parent_id='c',source_task_id='r')]
        self.runs=[dict(id='r',runtime_id='runtime',created_at='2026-09-10T08:00:00+08:00',status='completed',trigger_comment_id='c')]
        self.fail=False
    def read(self,args):
        if args[:2]==['runtime','list']:return [dict(id='runtime',provider='codex',custom_name='device')]
        if args[:2]==['agent','list']:return [dict(id='agent',runtime_id='runtime')]
        if args[:2]==['issue','list']:return dict(issues=[dict(id='issue',created_at='2026-09-09T08:00:00+08:00',assignee_id='agent',status='in_review',title='PRIVATE TITLE')],has_more=False)
        if args[:2]==['issue','runs']:return self.runs+self.runs
        if args[:2]==['issue','comment']:return self.comments+self.comments
        if self.fail:raise ValueError('private failure')
        return [dict(id='event',created_at='2026-09-10T09:00:00+08:00',action='status_changed',details={'from':'in_review','to':'in_progress','private':'SECRET'})]
    def collect(self,day):
        return activity.collect(self.machine,cache_root=self.cache,now=datetime(2026,9,day,18,tzinfo=SHANGHAI),roles={'device':'work'},run_json=self.read)
    def test_dedup_links_privacy_and_activation(self):
        first=self.collect(9)
        self.assertEqual(first['status'],'failed') # future records cannot activate
        self.assertIsNone(first['metrics']['activity']['eligibleFrom'])
        result=self.collect(10)
        self.assertEqual(result['retained'],dict(issues=1,comments=2,runs=1,activities=1))
        self.assertEqual(result['metrics']['activity']['eligibleFrom'],'2026-09-11')
        self.collect(11);result=self.collect(12)
        self.assertEqual(result['metrics']['activity']['effectiveFrom'],'2026-09-11')
        self.assertTrue(result['days']['2026-09-11']['valid'])
        self.assertFalse(result['days']['2026-09-10']['valid'])
        self.assertEqual(result['days']['2026-09-10']['humanTriggeredRuns'],1)
        self.assertEqual(result['days']['2026-09-10']['reviewReturns'],1)
        self.assertNotIn('PRIVATE',json.dumps(result))
        db=sqlite3.connect(self.cache/'issue-activity-work.sqlite3')
        bodies=' '.join(r[0] for r in db.execute('SELECT body FROM events'));db.close()
        self.assertNotIn('PRIVATE',bodies);self.assertNotIn('SECRET',bodies)
    def test_failure_preserves_batch_and_invalidates_days(self):
        self.collect(10);self.comments.append(dict(id='new',author_type='member',created_at='2026-09-11T09:00:00+08:00'))
        self.fail=True;result=self.collect(11)
        self.assertEqual(result['retained']['comments'],2)
        self.assertFalse(result['current']['available'])
        self.assertTrue(all(not d['valid'] for d in result['days'].values()))
    def test_multiple_runs_per_comment_and_missing_day(self):
        self.collect(10);self.runs.append(dict(self.runs[0],id='r2'))
        result=self.collect(12)
        self.assertEqual(result['days']['2026-09-10']['humanTriggeredRuns'],2)
        self.assertEqual(result['days']['2026-09-10']['triggeringHumanComments'],1)
        self.assertNotIn('2026-09-11',result['days'])
    def test_role_isolation(self):
        result=activity.collect(self.root/'personal',cache_root=self.cache,now=datetime(2026,9,10,18,tzinfo=SHANGHAI),roles={'device':'personal'},run_json=self.read)
        self.assertEqual(result['role'],'personal')
        self.assertFalse((self.root/'work'/'issue-activity.json').exists())
    def test_missing_journal_fails_closed(self):
        self.collect(10);(self.cache/'issue-activity-work.sqlite3').unlink()
        with self.assertRaises(ValueError):self.collect(11)
    def test_exact_public_schema(self):
        result=self.collect(10)
        for mutation in [lambda v:v.update(title='PRIVATE'),lambda v:v['current'].update(id='PRIVATE'),lambda v:v['days']['2026-09-10'].update(humanComments=-1)]:
            candidate=copy.deepcopy(result);mutation(candidate)
            with self.assertRaises(ValueError):activity.validate(candidate)
    def test_truncation_warning_fails_closed(self):
        class Response:
            returncode=0;stdout='[]';stderr='Warning: history truncated'
        with patch.object(activity.subprocess,'run',return_value=Response()):
            with self.assertRaises(ValueError):activity.read(['issue','timeline','synthetic'])
    def test_missing_links_are_observed_but_not_validated(self):
        self.comments[1]['parent_id']='not-returned'
        self.collect(10);self.collect(11);result=self.collect(12)
        self.assertEqual(result['coverage']['missingParents'],1)
        self.assertIsNone(result['metrics']['activity']['effectiveFrom'])
        self.assertTrue(all(not r['valid'] for r in result['days'].values()))
    def test_unknown_role_is_excluded_and_coverage_reported(self):
        original=self.read
        def read(args):
            result=original(args)
            if args[:2]==['agent','list']:return []
            if args[:2]==['issue','runs']:return [dict(self.runs[0],runtime_id='unmapped')]
            return result
        result=activity.collect(self.machine,cache_root=self.cache,now=datetime(2026,9,10,18,tzinfo=SHANGHAI),roles={'device':'work'},run_json=read)
        self.assertEqual(result['scope'],{'scannedIssues':1,'scopedIssues':0})
        self.assertEqual(result['current']['issues'],0)
    def test_hourly_cooldown_and_later_missing_records_retained(self):
        result=self.collect(10)
        with patch.object(activity,'records',side_effect=AssertionError('should not fetch')):
            self.assertEqual(self.collect(10),result)
        self.comments=[];self.runs=[]
        result=self.collect(11)
        self.assertEqual(result['retained']['comments'],2)
        self.assertEqual(result['retained']['runs'],1)

    def test_current_scope_refreshes_within_same_day(self):
        self.collect(10)
        original=self.read
        def read(args):
            if args[:2]==['issue','list']:return {'issues':[], 'has_more':False}
            return original(args)
        result=activity.collect(self.machine,cache_root=self.cache,now=datetime(2026,9,10,20,tzinfo=SHANGHAI),roles={'device':'work'},run_json=read)
        self.assertEqual(result['current']['issues'],0)
        self.assertEqual(result['retained']['issues'],1)
