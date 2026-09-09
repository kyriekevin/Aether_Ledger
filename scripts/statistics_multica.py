"""Collect per-machine Multica runs and exact links to observed session usage.

No current agent configuration is projected onto a past run. The run API lacks
an immutable requested-configuration/usage-total snapshot; those metrics stay
unactivated even when some session observations can be linked exactly.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

import multica_usage
from multica_dispatch import collect_dispatch
from statistics_readers import digest, timestamp
from statistics_store import observe_metric_versions

RUN_METRIC_VERSIONS = {"runs": 1, "runDuration": 1, "runConfiguration": 1, "runUsage": 1}

STATUSES = ("completed", "failed", "cancelled", "running", "queued", "unknown")
RUN_COUNTS = ("total", *STATUSES, "durationSeconds", "durationKnownRuns", "linkedRuns", "linkedCalls", "linkedTokens")


def collect_runs(journal, role, roles, now, run_json=multica_usage._run_json, public_models=None):
    meta = journal.metadata("multica-runs") or {"collectionStarted": now.date().isoformat(), "successDays": [], "metrics": {}}
    observe_metric_versions(meta, RUN_METRIC_VERSIONS, "pending", now.date(), {"runs", "runDuration"})
    # Low-frequency API work does not slow every token tick. Failures preserve
    # the last run journal, and a later attempt retries independently of Git.
    prior = timestamp(meta.get("lastAttemptAt"))
    if prior and (now - datetime.fromisoformat(prior)).total_seconds() < 3600:
        return
    if not meta["successDays"]:
        meta["collectionStarted"] = now.date().isoformat()
    meta.update(lastAttemptAt=now.isoformat(), lastAttempt=now.date().isoformat())
    try:
        runtime_payload = run_json(["runtime", "list"])
        index = multica_usage._runtime_index(runtime_payload, roles)
        index = {k: v for k, v in index.items() if v[0] == role}
        if not index:
            meta["status"] = "unavailable"
        else:
            seen_issues, seen_runs = set(), set()
            runs = []
            pages = {}
            offset = 0
            while True:
                page = run_json(["issue", "list", "--limit", "100", "--offset", str(offset)])
                pages[offset] = page
                if not isinstance(page, dict) or not isinstance(page.get("issues"), list):
                    raise ValueError("invalid issue page")
                for issue in page["issues"]:
                    identifier = issue["id"]
                    if identifier in seen_issues:
                        continue
                    seen_issues.add(identifier)
                    payload = run_json(["issue", "runs", identifier])
                    if not isinstance(payload, list):
                        raise ValueError("invalid run response")
                    for run in payload:
                        run_id = run["id"]
                        if not isinstance(run_id, str):
                            raise ValueError("invalid run identity")
                        if run_id in seen_runs:
                            continue
                        seen_runs.add(run_id)
                        runtime = index.get(run.get("runtime_id"))
                        if runtime is None:
                            continue
                        started = timestamp(run.get("started_at"))
                        ended = timestamp(run.get("completed_at"))
                        created = timestamp(run.get("created_at"))
                        at = started or created
                        if at is None or at[:10] < meta["collectionStarted"] or at[:10] > now.date().isoformat():
                            continue
                        result = run.get("result")
                        session = result.get("session_id") if isinstance(result, dict) else None
                        status = run.get("status")
                        runs.append(dict(key=digest("run", run_id), harness=runtime[1], day=at[:10],
                                         started=started, ended=ended,
                                         status=status if status in STATUSES else "unknown",
                                         session=digest(runtime[1], session) if isinstance(session, str) and session else None))
                if not page.get("has_more"):
                    break
                if not page["issues"]:
                    raise ValueError("empty issue page")
                offset += 100
            def cached_read(args):
                if args == ["runtime", "list"]:
                    return runtime_payload
                if args[:2] == ["issue", "list"]:
                    return pages[int(args[-1])]
                return run_json(args)
            try:
                meta["assignment"] = collect_dispatch(roles, run_json=cached_read,
                    allowed_models=public_models | {"default"} if public_models is not None else None,
                    as_of=now.date().isoformat())
                meta["assignmentStatus"] = "ok"
            except Exception:
                meta["assignmentStatus"] = "failed"
            # Commit a fully fetched snapshot atomically. Missing old runs are
            # retained, because remote retention cannot establish deletion.
            with journal.db:
                for run in runs:
                    journal.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)", (run["key"], json.dumps(run)))
            meta["status"] = "ok"
            meta["successDays"] = sorted(set(meta["successDays"]) | {now.date().isoformat()})
    except Exception:
        meta["status"] = "failed"
    observe_metric_versions(meta, RUN_METRIC_VERSIONS, meta["status"], now.date(), {"runs", "runDuration"})
    with journal.db:
        journal.save_meta("multica-runs", meta)


def export_runs(journal, today):
    from datetime import datetime
    meta = journal.metadata("multica-runs")
    if meta is None:
        return None
    runs = [json.loads(r[0]) for r in journal.db.execute("SELECT body FROM runs")]
    by_session = defaultdict(list)
    for run in runs:
        if run["session"] and run["started"] and run["ended"] and run["status"] in multica_usage.TERMINAL_STATUSES:
            if run["started"] <= run["ended"]:
                by_session[run["session"]].append(run)
    linked = defaultdict(list)
    for fact in journal.facts():
        candidates = [r for r in by_session[fact["session"]] if r["started"] <= fact["at"] <= r["ended"]]
        if len(candidates) == 1:
            linked[candidates[0]["key"]].append(fact)
    # Successful scans provide evidence of zero activity. Unobserved dates
    # remain absent; validMetrics additionally requires next-day closure.
    days = {day: {key: 0 for key in RUN_COUNTS} for day in meta["successDays"]
            if meta["collectionStarted"] <= day <= min(today.isoformat(), meta["lastAttempt"])}
    for run in runs:
        if run["day"] > today.isoformat():
            continue
        bucket = days.setdefault(run["day"], {key: 0 for key in RUN_COUNTS})
        bucket["total"] += 1
        bucket[run["status"]] += 1
        if run["status"] in multica_usage.TERMINAL_STATUSES and run["started"] and run["ended"] and run["started"] <= run["ended"]:
            bucket["durationKnownRuns"] += 1
            bucket["durationSeconds"] += int((datetime.fromisoformat(run["ended"]) - datetime.fromisoformat(run["started"])).total_seconds())
        facts = linked[run["key"]]
        bucket["linkedRuns"] += bool(facts)
        bucket["linkedCalls"] += len(facts)
        bucket["linkedTokens"] += sum(f["totalTokens"] for f in facts)
    eligible = (date.fromisoformat(meta["collectionStarted"]) + timedelta(days=1)).isoformat()
    if "metrics" not in meta:
        first = min(meta["successDays"], default=meta["collectionStarted"])
        observe_metric_versions(meta, RUN_METRIC_VERSIONS, "ok" if meta["successDays"] else "pending",
                                date.fromisoformat(first), {"runs", "runDuration"})
    for day, values in sorted(days.items()):
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        closed = meta["status"] == "ok" and day < today.isoformat() and day in meta["successDays"] and next_day in meta["successDays"]
        checks = {"runs": not values["unknown"], "runDuration": values["durationKnownRuns"] == values["total"],
                  "runConfiguration": False, "runUsage": False}
        values["validMetrics"] = []
        for name, definition in meta["metrics"].items():
            start = definition["eligibleFrom"]
            observed = values["total"] > 0 or (definition["effectiveFrom"] is not None and day >= definition["effectiveFrom"])
            if closed and start is not None and day >= start and checks[name] and observed:
                values["validMetrics"].append(name)
                if definition["effectiveFrom"] is None:
                    definition["effectiveFrom"] = day
    with journal.db:
        journal.save_meta("multica-runs", meta)
    return {"collectionStarted": meta["collectionStarted"], "eligibleFrom": eligible,
            "lastAttempt": meta["lastAttempt"], "lastSuccess": max(meta["successDays"], default=None), "status": meta["status"],
            "assignment": meta.get("assignment"), "assignmentStatus": meta.get("assignmentStatus", "unavailable"),
            "metrics": meta["metrics"], "days": days}
