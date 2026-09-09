"""Private deduplication journal and versioned, anonymous public statistics.

The journal retains measurement facts when session logs rotate. A fact's complete
bundle is replaced on correction; dimensions are never independently max-merged.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from pricing import active_rate
from statistics_readers import COMPONENTS

METRIC_VERSIONS = {"usage": 1, "modelEffort": 1, "speed": 1, "cost": 1, "quota": 1}
METRICS = tuple(METRIC_VERSIONS)
SOURCES = {"codex": "codex", "codex-multica": "codex", "claude": "claude", "traex": "traex", "dsh": "dsh", "dsh-multica": "dsh"}
COUNTS = ("calls", "identifiedCalls", "totalTokens", *COMPONENTS, "pricedCalls")


def observe_metric_versions(meta, versions, status, today, enabled=None):
    definitions = meta.setdefault("metrics", {})
    enabled = set(versions) if enabled is None else set(enabled)
    for name, version in versions.items():
        definition = definitions.get(name)
        if definition is not None and definition["version"] > version:
            raise ValueError("statistics collector is older than the journal")
        if definition is None or definition["version"] != version:
            if definition is not None:
                meta.setdefault("retiredVersions", {}).setdefault(name, []).append(definition)
            definition = {"version": version, "eligibleFrom": None, "effectiveFrom": None}
            definitions[name] = definition
        if status == "ok" and name in enabled and definition["eligibleFrom"] is None:
            definition["eligibleFrom"] = (today + timedelta(days=1)).isoformat()


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS facts (key TEXT PRIMARY KEY, source TEXT NOT NULL, day TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (source TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (key TEXT PRIMARY KEY, body TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def metadata(self, source):
        row = self.db.execute("SELECT body FROM metadata WHERE source=?", (source,)).fetchone()
        return json.loads(row[0]) if row else None

    def ingest(self, source, reading, today: date, *, reconcile=False):
        if source not in SOURCES:
            raise ValueError("unknown statistics source")
        meta = self.metadata(source) or {"collectionStarted": today.isoformat(), "successDays": [], "metrics": {}, "quota": {}}
        diagnostics = dict(reading.diagnostics)
        with self.db:
            for fact in reading.observations.values():
                if fact["day"] < meta["collectionStarted"] or fact["day"] > today.isoformat():
                    continue
                previous = self.db.execute("SELECT source,body FROM facts WHERE key=?", (fact["key"],)).fetchone()
                if previous and previous[0] != source:
                    diagnostics["sourceOverlap"] = diagnostics.get("sourceOverlap", 0) + 1
                    continue
                if previous and not reconcile and json.loads(previous[1])["rank"] > fact["rank"]:
                    continue
                self.db.execute("INSERT OR REPLACE INTO facts VALUES (?,?,?,?)", (fact["key"], source, fact["day"], json.dumps(fact)))
            status = reading.status
            if any(diagnostics.values()) and status == "ok":
                status = "partial"
            meta.update(lastAttempt=today.isoformat(), status=status, diagnostics=diagnostics)
            if status == "ok":
                meta["successDays"] = sorted(set(meta["successDays"]) | {today.isoformat()})
            observe_metric_versions(meta, METRIC_VERSIONS, status, today)
            for day, windows in reading.quotas.items():
                if not meta["collectionStarted"] <= day <= today.isoformat():
                    continue
                bucket = meta["quota"].setdefault(day, {})
                for minutes, percent in windows.items():
                    bucket[minutes] = max(bucket.get(minutes, 0), percent)
            self.save_meta(source, meta)
        return meta

    def save_meta(self, source, meta):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (source, json.dumps(meta)))

    def facts(self, source=None):
        query = "SELECT body FROM facts" + (" WHERE source=?" if source else "")
        args = (source,) if source else ()
        return [json.loads(row[0]) for row in self.db.execute(query, args)]


def estimate(fact, pricing):
    rate = active_rate(fact["model"], date.fromisoformat(fact["day"]), pricing)
    if rate is None or ("fastMultiplier" in rate and fact["speed"] == "unknown"):
        return None
    context = fact["inputTokens"] + fact["cacheReadTokens"] + fact["cacheCreationTokens"]
    long = context > rate.get("longContextThreshold", float("inf"))
    fields = ("longInput", "longOutput", "longCacheRead", "longCacheWrite") if long else ("input", "output", "cacheRead", "cacheWrite")
    if any(k not in rate for k in fields):
        return None
    amount = sum(fact[k] * rate[r] for k, r in zip(COMPONENTS, fields)) / 1e6
    return amount * (rate.get("fastMultiplier", 1) if fact["speed"] == "fast" else 1)


def summarize(facts, public_models, pricing):
    rows = {}
    for fact in facts:
        model = fact["model"] if fact["model"] in public_models else "unknown"
        key = (model, fact["effort"], fact["speed"])
        bucket = rows.setdefault(key, {k: 0 for k in COUNTS} | {"estimatedCost": 0.0})
        bucket["calls"] += 1
        bucket["identifiedCalls"] += fact["basis"] in {"response", "step"}
        for k in ("totalTokens", *COMPONENTS):
            bucket[k] += fact[k]
        cost = estimate(fact, pricing)
        if cost is not None:
            bucket["pricedCalls"] += 1
            bucket["estimatedCost"] += cost
    combinations = [dict(zip(("model", "effort", "speed"), key), **values) for key, values in sorted(rows.items())]
    totals = {k: sum(row[k] for row in combinations) for k in (*COUNTS, "estimatedCost")}
    coverage = {
        "modelCalls": sum(r["calls"] for r in combinations if r["model"] != "unknown"),
        "effortCalls": sum(r["calls"] for r in combinations if r["effort"] != "unknown"),
        "jointCalls": sum(r["calls"] for r in combinations if r["model"] != "unknown" and r["effort"] != "unknown"),
        "speedCalls": sum(r["calls"] for r in combinations if r["speed"] != "unknown"),
    }
    return dict(totals=totals, combinations=combinations, coverage=coverage)


def export_source(journal, source, ledger, today, public_models, pricing):
    meta = journal.metadata(source)
    if not meta:
        raise ValueError("source has not been observed")
    by_day = defaultdict(list)
    for fact in journal.facts(source):
        by_day[fact["day"]].append(fact)
    days = {}
    day = date.fromisoformat(meta["collectionStarted"])
    eligible = day + timedelta(days=1)
    while day <= today:
        key = day.isoformat()
        row = summarize(by_day[key], public_models, pricing)
        # Compare to the durable canonical store, never a potentially truncated
        # fresh scan. Missing canonical day is unknown, not an asserted zero.
        expected = ledger.get(key, {}).get("totalTokens")
        observed = row["totals"]["totalTokens"]
        matched = expected is not None and expected == observed
        closed = day < today and key in meta["successDays"] and (day + timedelta(days=1)).isoformat() in meta["successDays"]
        empty = closed and expected is None and observed == 0 and meta["status"] == "ok"
        row["reconciliation"] = {"ledgerTokens": expected, "state": "empty" if empty else "matched" if matched else "unknown" if expected is None else "mismatch"}
        row["collection"] = "complete" if closed else "open" if day == today else "unverified"
        calls = row["totals"]["calls"]
        trusted = matched or (calls > 0 and row["totals"]["identifiedCalls"] == calls)
        ready = {"usage": trusted and calls > 0,
                 "modelEffort": trusted and calls > 0 and row["coverage"]["jointCalls"] == calls,
                 "speed": trusted and calls > 0 and row["coverage"]["speedCalls"] == calls,
                 "cost": trusted and calls > 0 and row["totals"]["pricedCalls"] == calls,
                 "quota": bool(meta["quota"].get(key))}
        if empty:
            for metric in ("usage", "modelEffort", "speed", "cost"):
                ready[metric] = meta["metrics"][metric]["effectiveFrom"] is not None
        row["validMetrics"] = [m for m in METRICS if closed and meta["status"] == "ok" and meta["metrics"][m]["eligibleFrom"] is not None
                               and key >= meta["metrics"][m]["eligibleFrom"] and ready[m]]
        row["quotaPeaks"] = meta["quota"].get(key, {})
        for metric in row["validMetrics"]:
            if meta["metrics"][metric]["effectiveFrom"] is None:
                meta["metrics"][metric]["effectiveFrom"] = key
        days[key] = row
        day += timedelta(days=1)
    with journal.db:
        journal.save_meta(source, meta)
    return {
        "collectionStarted": meta["collectionStarted"], "eligibleFrom": eligible.isoformat(),
        "lastAttempt": meta["lastAttempt"], "lastSuccess": max(meta["successDays"], default=None), "status": meta["status"], "diagnostics": meta["diagnostics"],
        "metrics": meta["metrics"],
        "days": days,
    }


def common_valid_days(requirements):
    """Intersect metric-specific valid dates; never fill gaps with legacy data."""
    common = None
    for source, metric in requirements:
        definition = source["metrics"][metric]
        start = definition["effectiveFrom"]
        days = {day for day, row in source["days"].items()
                if start is not None and day >= start and metric in row["validMetrics"]}
        if source["status"] != "ok":
            days = set()
        common = days if common is None else common & days
    return sorted(common or ())
