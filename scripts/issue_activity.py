"""Private issue-event journal and anonymous activity aggregates; no content storage."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import multica_usage as api
from statistics_readers import digest, timestamp
from statistics_store import observe_metric_versions
from usage_schema import SHANGHAI
from usage_store import _atomic_write_json

VERSION = {"activity": 1}
STATES = {"backlog", "todo", "in_progress", "in_review", "blocked", "done", "cancelled", "archived", "unknown"}
COUNTS = {"humanComments", "agentComments", "systemComments", "unknownComments", "replies", "runsCreated", "humanTriggeredRuns", "triggeringHumanComments", "reviewReturns", "reopens", "statusChanges"}


def read(args):
    prefix = ["--profile", api.MULTICA_PROFILE] if api.MULTICA_PROFILE else []
    result = subprocess.run([api.MULTICA_BIN, *prefix, *args, "--output", "json"],
                            capture_output=True, text=True, timeout=45)
    if result.returncode or "truncat" in result.stderr.lower() or "next thread cursor" in result.stderr.lower():
        raise ValueError("incomplete activity read")
    return json.loads(result.stdout)


def records(value):
    if not isinstance(value, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"] for r in value):
        raise ValueError("invalid record list")
    seen = {}
    for r in value:
        if r['id'] in seen and seen[r['id']] != r:
            raise ValueError("conflicting duplicate")
        seen[r['id']] = r
    return list(seen.values())


def at(value):
    result = timestamp(value)
    if result is None:
        raise ValueError("invalid event time")
    return result


def ref(kind, value):
    return digest(kind, value) if isinstance(value, str) and value else None


def collect(machine_dir, *, cache_root=None, now=None, run_json=read, roles=None):
    """Caller holds the usage writer lock. Independent of token collection failures."""
    if roles is None and not api.CONFIG_FILE.exists():
        return None
    role = machine_dir.name
    if role not in api.PUBLIC_ROLES:
        raise ValueError("invalid role")
    now = now or datetime.now(SHANGHAI)
    today = now.date().isoformat()
    cache_root = cache_root or Path.home() / '.cache/aether-ledger/statistics-v1'
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = cache_root / f'issue-activity-{role}.sqlite3'
    public = machine_dir / 'issue-activity.json'
    if public.exists() and not path.exists():
        raise ValueError("restore private activity journal")
    db = sqlite3.connect(path)
    os.chmod(path, 0o600)
    try:
        db.executescript('CREATE TABLE IF NOT EXISTS events (kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id)); CREATE TABLE IF NOT EXISTS meta (body TEXT);')
        saved = db.execute('SELECT body FROM meta').fetchone()
        if public.exists() and not saved:
            raise ValueError("restore private activity metadata")
        meta = json.loads(saved[0]) if saved else {'collectionStarted': today, 'successDays': [], 'metrics': {}}
        if saved and (now - datetime.fromisoformat(meta['lastAttemptAt'])).total_seconds() < 3600:
            return json.loads(public.read_text()) if public.exists() else None
        if public.exists():
            prior = json.loads(public.read_text())
            for kind, field in [('issue','issues'),('comment','comments'),('run','runs'),('activity','activities')]:
                if db.execute('SELECT COUNT(*) FROM events WHERE kind=?',(kind,)).fetchone()[0] < prior['retained'][field]:
                    raise ValueError("activity journal behind publication")
        meta.update(lastAttemptAt=now.isoformat(), lastAttempt=today, status='failed')
        observe_metric_versions(meta, VERSION, 'pending', now.date())
        try:
            mapping = api._runtime_index(run_json(['runtime','list']), roles if roles is not None else api.load_runtime_roles())
            agents = {a['id']: mapping.get(a.get('runtime_id')) for a in records(run_json(['agent','list','--include-archived']))}
            issues = []; offset = 0
            while True:
                page = run_json(['issue','list','--limit','100','--offset',str(offset)])
                rows = records(page['issues']); issues.extend(rows)
                if not page.get('has_more'): break
                if not rows: raise ValueError("empty continuation")
                offset += 100
            issues = records(issues)
            batch = []; scoped = 0
            for issue in issues:
                runs = records(run_json(['issue','runs',issue['id']]))
                owner = mapping.get(issue.get('bound_runtime_id')) or agents.get(issue.get('assignee_id'))
                if owner is None:
                    owners = {mapping[r['runtime_id']][0] for r in runs if r.get('runtime_id') in mapping}
                    owner = (next(iter(owners)), '') if len(owners)==1 else None
                if owner is None or owner[0] != role: continue
                scoped += 1
                ikey = ref('issue',issue['id'])
                state = issue.get('status_category') or issue.get('status')
                batch.append(('issue',ikey,dict(created=at(issue['created_at']), parent=ref('issue',issue.get('parent_issue_id')), status=state if state in STATES else 'unknown', seen=today)))
                comments = records(run_json(['issue','comment','list',issue['id'],'--full']))
                for c in comments:
                    author = c.get('author_type')
                    batch.append(('comment',ref('comment',c['id']),dict(issue=ikey, at=at(c['created_at']), author=author if author in {'member','agent','system'} else 'unknown', parent=ref('comment',c.get('parent_id')), run=ref('run',c.get('source_task_id')))))
                for r in runs:
                    batch.append(('run',ref('run',r['id']),dict(issue=ikey, at=at(r['created_at']), started=timestamp(r.get('started_at')), ended=timestamp(r.get('completed_at')), trigger=ref('comment',r.get('trigger_comment_id')), status=r.get('status') if r.get('status') in {'completed','failed','cancelled','running','queued'} else 'unknown')))
                for event in records(run_json(['issue','timeline',issue['id'],'--activity-only'])):
                    if event.get('action') != 'status_changed': continue
                    details = event.get('details') or {}
                    before, after = details.get('from'), details.get('to')
                    batch.append(('activity',ref('activity',event['id']),dict(issue=ikey, at=at(event['created_at']), before=before if before in STATES else 'unknown', after=after if after in STATES else 'unknown')))
            if not any(v[0]==role for v in mapping.values()): raise ValueError("no role coverage")
            if any(v.get('at',v.get('created',''))[:10] > today for _,_,v in batch): raise ValueError("future event")
            with db:
                for kind,key,value in batch:
                    db.execute('INSERT OR REPLACE INTO events VALUES (?,?,?)',(kind,key,json.dumps(value)))
            meta.update(status='ok', scannedIssues=len(issues), scopedIssues=scoped, currentIssues=[key for kind,key,_ in batch if kind=='issue'])
            meta['successDays'] = sorted(set(meta['successDays']) | {today})
        except Exception:
            meta['status'] = 'failed'
        observe_metric_versions(meta, VERSION, meta['status'], now.date())
        result = export(db, meta, role, today)
        validate(result)
        with db:
            db.execute('DELETE FROM meta'); db.execute('INSERT INTO meta VALUES (?)',(json.dumps(meta),))
        _atomic_write_json(public,result)
        return result
    finally:
        db.close()


def export(db, meta, role, today):
    data = {kind: {} for kind in ['issue','comment','run','activity']}
    for kind,key,body in db.execute('SELECT kind,id,body FROM events'):
        data[kind][key] = json.loads(body)
    coverage = {
        'missingParents': sum(bool(c['parent']) and c['parent'] not in data['comment'] for c in data['comment'].values()),
        'missingTriggers': sum(bool(r['trigger']) and r['trigger'] not in data['comment'] for r in data['run'].values()),
        'unknownEvents': sum(c['author']=='unknown' for c in data['comment'].values()) + sum(e['before']=='unknown' or e['after']=='unknown' for e in data['activity'].values()),
    }
    days = {}; start = meta['metrics']['activity']['eligibleFrom']
    def bucket(at):
        day = at[:10]
        if day < meta['collectionStarted'] or day > today: return None
        return days.setdefault(day,{key:0 for key in COUNTS})
    # No false zeros on failed or missing scans.
    for d in meta['successDays']: bucket(d)
    human = Counter(); triggering = set()
    for key,c in data['comment'].items():
        if c['author']=='member': human[c['issue']]+=1
        b=bucket(c['at'])
        if b is not None:
            b[{'member':'humanComments','agent':'agentComments','system':'systemComments','unknown':'unknownComments'}[c['author']]]+=1
            b['replies']+=bool(c['parent'])
    for r in data['run'].values():
        b=bucket(r['at'])
        if b is not None:b['runsCreated']+=1
        c=data['comment'].get(r['trigger'])
        if c and c['author']=='member':
            b=bucket(c['at'])
            if b is not None:b['humanTriggeredRuns']+=1
            triggering.add(r['trigger'])
    for key in triggering:
        b=bucket(data['comment'][key]['at'])
        if b is not None:b['triggeringHumanComments']+=1
    for e in data['activity'].values():
        b=bucket(e['at'])
        if b is None:continue
        b['statusChanges']+=1
        b['reviewReturns']+=e['before']=='in_review' and e['after']=='in_progress'
        b['reopens']+=e['before']=='done' and e['after'] in {'todo','in_progress','in_review','blocked'}
    definition=meta['metrics']['activity']
    for d,b in sorted(days.items()):
        next_day=(datetime.fromisoformat(d)+timedelta(days=1)).date().isoformat()
        valid=meta['status']=='ok' and start is not None and d>=start and d<today and d in meta['successDays'] and next_day in meta['successDays'] and b['unknownComments']==0 and not any(coverage.values())
        b['valid']=valid
        if valid and definition['effectiveFrom'] is None:definition['effectiveFrom']=d
    distribution={group:{k:0 for k in ['zero','oneTwo','threeFive','sixTen','elevenPlus']} for group in ['parent','child','standalone']}
    parents={i['parent'] for i in data['issue'].values() if i['parent']}
    current={k:v for k,v in data['issue'].items() if k in set(meta.get('currentIssues',[]))} if meta['status']=='ok' else {}
    for key,i in current.items():
        n=human[key];group='parent' if key in parents else ('child' if i['parent'] else 'standalone')
        label='zero' if n==0 else 'oneTwo' if n<=2 else 'threeFive' if n<=5 else 'sixTen' if n<=10 else 'elevenPlus'
        distribution[group][label]+=1
    return dict(schemaVersion=1,role=role,collectionStarted=meta['collectionStarted'],lastAttempt=today,lastSuccess=max(meta['successDays'],default=None),status=meta['status'],metrics=meta['metrics'],
                coverage=coverage,retained=dict(zip(['issues','comments','runs','activities'],[len(data[k]) for k in ['issue','comment','run','activity']])),
                scope={'scannedIssues':meta.get('scannedIssues',0),'scopedIssues':meta.get('scopedIssues',0)},
                current={'available':meta['status']=='ok','issues':len(current),'statuses':{state:sum(i['status']==state for i in current.values()) for state in STATES},'humanCommentDistribution':distribution},days=days)


def validate(value):
    from statistics_schema import fields, count, day, metrics
    fields(value,{'schemaVersion','role','collectionStarted','lastAttempt','lastSuccess','status','metrics','coverage','retained','scope','current','days'})
    if type(value['schemaVersion']) is not int or value['schemaVersion']!=1 or value['role'] not in api.PUBLIC_ROLES or value['status'] not in {'ok','failed'}:raise ValueError('invalid activity schema')
    for k in ['collectionStarted','lastAttempt']:day(value[k])
    day(value['lastSuccess'],nullable=True)
    if value['collectionStarted']>value['lastAttempt'] or value['lastSuccess'] is not None and not value['collectionStarted']<=value['lastSuccess']<=value['lastAttempt']:raise ValueError('invalid dates')
    earliest=(datetime.fromisoformat(value['collectionStarted'])+timedelta(days=1)).date().isoformat()
    metrics(value['metrics'],VERSION,earliest,value['lastAttempt'])
    for name,keys in [('coverage',{'missingParents','missingTriggers','unknownEvents'}),('retained',{'issues','comments','runs','activities'}),('scope',{'scannedIssues','scopedIssues'})]:
        fields(value[name],keys)
        for n in value[name].values():count(n)
    if value['scope']['scopedIssues']>value['scope']['scannedIssues']:raise ValueError('invalid scope')
    c=value['current'];fields(c,{'available','issues','statuses','humanCommentDistribution'});count(c['issues'])
    if type(c['available']) is not bool or c['available']!=(value['status']=='ok'):raise ValueError('invalid freshness')
    fields(c['statuses'],STATES)
    for n in c['statuses'].values():count(n)
    if sum(c['statuses'].values())!=c['issues']:raise ValueError('invalid status totals')
    fields(c['humanCommentDistribution'],{'parent','child','standalone'});total=0
    for row in c['humanCommentDistribution'].values():
        fields(row,{'zero','oneTwo','threeFive','sixTen','elevenPlus'})
        for n in row.values():count(n);total+=n
    if total!=c['issues'] or c['issues']>value['retained']['issues'] or not c['available'] and total:raise ValueError('invalid distribution')
    if not isinstance(value['days'],dict):raise ValueError('invalid days')
    for d,row in value['days'].items():
        day(d);fields(row,COUNTS|{'valid'})
        for k in COUNTS:count(row[k])
        if not value['collectionStarted']<=d<=value['lastAttempt'] or type(row['valid']) is not bool:raise ValueError('invalid event day')
        start=value['metrics']['activity']['effectiveFrom']
        if row['valid'] and (value['status']!='ok' or start is None or not start<=d<value['lastAttempt'] or row['unknownComments'] or any(value['coverage'].values())):raise ValueError('invalid valid day')
        if row['triggeringHumanComments']>row['humanComments'] or row['triggeringHumanComments']>row['humanTriggeredRuns'] or row['reviewReturns']+row['reopens']>row['statusChanges']:raise ValueError('invalid event totals')
