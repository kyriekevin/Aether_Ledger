"""Pure public-data validators for retained metadata; no collection or API access."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from usage_schema import EFFORT_LEVELS

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROLES = {"work", "personal", "devbox"}
DISPATCH_STATES = ("backlog", "todo", "in_progress", "done", "cancelled", "archived", "unknown")
EFFORTS = EFFORT_LEVELS | {"default", "unknown"}
ACTIVITY_VERSION = {"activity": 1}
ACTIVITY_STATES = {"backlog", "todo", "in_progress", "in_review", "blocked", "done", "cancelled", "archived", "unknown"}
ACTIVITY_COUNTS = {"humanComments", "agentComments", "systemComments", "unknownComments", "replies", "runsCreated", "humanTriggeredRuns", "triggeringHumanComments", "reviewReturns", "reopens", "statusChanges"}

def allowed_models(root=ROOT):
    return set(json.loads((root / "config/statistics-models.json").read_text())["models"]) | {"unknown"}

def public_models(root: Path = ROOT) -> set[str]:
    return {"minimax-m3", "mimo-v2.5-pro", "glm-5.2", "kimi-k3", "qwen3.7-max"} | set(json.loads((root / "config" / "official-pricing.json").read_text())["models"]) | {"default", "unknown"}

def validate_activity(value):
    from statistics_schema import fields, count, day, metrics
    fields(value,{'schemaVersion','role','collectionStarted','lastAttempt','lastSuccess','status','metrics','coverage','retained','scope','current','days'})
    if type(value['schemaVersion']) is not int or value['schemaVersion']!=1 or value['role'] not in PUBLIC_ROLES or value['status'] not in {'ok','failed'}:raise ValueError('invalid activity schema')
    for k in ['collectionStarted','lastAttempt']:day(value[k])
    day(value['lastSuccess'],nullable=True)
    if value['collectionStarted']>value['lastAttempt'] or value['lastSuccess'] is not None and not value['collectionStarted']<=value['lastSuccess']<=value['lastAttempt']:raise ValueError('invalid dates')
    earliest=(datetime.fromisoformat(value['collectionStarted'])+timedelta(days=1)).date().isoformat()
    metrics(value['metrics'],ACTIVITY_VERSION,earliest,value['lastAttempt'])
    for name,keys in [('coverage',{'missingParents','missingTriggers','unknownEvents'}),('retained',{'issues','comments','runs','activities'}),('scope',{'scannedIssues','scopedIssues'})]:
        fields(value[name],keys)
        for n in value[name].values():count(n)
    if value['scope']['scopedIssues']>value['scope']['scannedIssues']:raise ValueError('invalid scope')
    c=value['current'];fields(c,{'available','issues','statuses','humanCommentDistribution'});count(c['issues'])
    if type(c['available']) is not bool or c['available']!=(value['status']=='ok'):raise ValueError('invalid freshness')
    fields(c['statuses'],ACTIVITY_STATES)
    for n in c['statuses'].values():count(n)
    if sum(c['statuses'].values())!=c['issues']:raise ValueError('invalid status totals')
    fields(c['humanCommentDistribution'],{'parent','child','standalone'});total=0
    for row in c['humanCommentDistribution'].values():
        fields(row,{'zero','oneTwo','threeFive','sixTen','elevenPlus'})
        for n in row.values():count(n);total+=n
    if total!=c['issues'] or c['issues']>value['retained']['issues'] or not c['available'] and total:raise ValueError('invalid distribution')
    if not isinstance(value['days'],dict):raise ValueError('invalid days')
    for d,row in value['days'].items():
        day(d);fields(row,ACTIVITY_COUNTS|{'valid'})
        for k in ACTIVITY_COUNTS:count(row[k])
        if not value['collectionStarted']<=d<=value['lastAttempt'] or type(row['valid']) is not bool:raise ValueError('invalid event day')
        start=value['metrics']['activity']['effectiveFrom']
        if row['valid'] and (value['status']!='ok' or start is None or not start<=d<value['lastAttempt'] or row['unknownComments'] or any(value['coverage'].values())):raise ValueError('invalid valid day')
        if row['triggeringHumanComments']>row['humanComments'] or row['triggeringHumanComments']>row['humanTriggeredRuns'] or row['reviewReturns']+row['reopens']>row['statusChanges']:raise ValueError('invalid event totals')

def validate_snapshot(value: object, models: set[str]) -> None:
    """An exact allow-list: arbitrary API strings must never become public fields."""
    def count(v):
        return type(v) is int and v >= 0

    if not isinstance(value, dict) or set(value) != {"asOf", "coveredRoles", "unmappedAgents", "unassignedIssues", "totalIssues", "configurations"}:
        raise ValueError("invalid dispatch snapshot fields")
    datetime.strptime(value["asOf"], "%Y-%m-%d")
    roles = value["coveredRoles"]
    if not isinstance(roles, list) or not roles or any(r not in PUBLIC_ROLES for r in roles) or len(set(roles)) != len(roles):
        raise ValueError("invalid dispatch roles")
    if not all(count(value[k]) for k in ("unmappedAgents", "unassignedIssues", "totalIssues")) or not isinstance(value["configurations"], list):
        raise ValueError("invalid dispatch counts")
    total = value["unassignedIssues"]
    seen = set()
    for row in value["configurations"]:
        if not isinstance(row, dict) or set(row) != {"role", "harness", "model", "effort", "agents", "archivedAgents", "issues"}:
            raise ValueError("invalid configuration fields")
        if row["role"] not in roles or row["harness"] not in {"codex", "claude", "traex", "dsh"} or row["model"] not in models or row["effort"] not in EFFORTS:
            raise ValueError("non-public configuration")
        key = tuple(row[k] for k in ("role", "harness", "model", "effort"))
        if key in seen or not count(row["agents"]) or not count(row["archivedAgents"]):
            raise ValueError("duplicate configuration or invalid agent count")
        seen.add(key)
        if not isinstance(row["issues"], dict) or set(row["issues"]) != set(DISPATCH_STATES) or not all(count(n) for n in row["issues"].values()):
            raise ValueError("invalid issue counts")
        total += sum(row["issues"].values())
    if total != value["totalIssues"]:
        raise ValueError("issue conservation failed")
