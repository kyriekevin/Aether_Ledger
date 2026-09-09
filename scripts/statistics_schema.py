"""Exact public schema and conservation checks for statistics.json."""
from __future__ import annotations

import math
from datetime import date, timedelta

from statistics_readers import COMPONENTS
from statistics_store import COUNTS, METRICS, SOURCES
from statistics_multica import RUN_COUNTS, RUN_METRIC_VERSIONS, STATUSES
from usage_schema import EFFORT_LEVELS, SPEED_LEVELS

DIAGNOSTICS = {"invalidTimestamp", "invalidTokens", "missingComponents", "inconsistentTokens", "invalidJson", "unreadable", "incompleteLog", "missingIdentity", "sourceOverlap", "collectionFailed", "ambiguousCounter", "foreignThread"}


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("unexpected public statistics fields")


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid statistics count")


def day(value, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("invalid statistics date")


def metrics(value, names, earliest, latest):
    fields(value, names)
    for definition in value.values():
        fields(definition, {"version", "eligibleFrom", "effectiveFrom"})
        if type(definition["version"]) is not int or definition["version"] < 1:
            raise ValueError("unknown metric version")
        eligible = definition["eligibleFrom"]
        day(eligible, nullable=True)
        if eligible is not None and eligible < earliest:
            raise ValueError("premature eligibility")
        start = definition["effectiveFrom"]
        day(start, nullable=True)
        if start is not None and (eligible is None or not eligible <= start < latest):
            raise ValueError("premature metric activation")


def bundle(value, extra=()):
    fields(value, {*COUNTS, "estimatedCost", *extra})
    for key in COUNTS:
        count(value[key])
    cost = value["estimatedCost"]
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        raise ValueError("invalid estimated cost")
    if value["totalTokens"] != sum(value[k] for k in COMPONENTS) or value["pricedCalls"] > value["calls"] or value["identifiedCalls"] > value["calls"]:
        raise ValueError("token conservation failed")


def validate(value, models):
    fields(value, {"schemaVersion", "role", "sources", "multica"})
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1 or value["role"] not in {"work", "personal", "devbox"}:
        raise ValueError("invalid statistics scope")
    if not isinstance(value["sources"], dict) or not set(value["sources"]) <= set(SOURCES):
        raise ValueError("unknown statistics source")
    for source in value["sources"].values():
        fields(source, {"collectionStarted", "eligibleFrom", "lastAttempt", "lastSuccess", "status", "diagnostics", "metrics", "days"})
        for key in ("collectionStarted", "eligibleFrom", "lastAttempt"):
            day(source[key])
        day(source["lastSuccess"], nullable=True)
        if source["lastSuccess"] is not None and not source["collectionStarted"] <= source["lastSuccess"] <= source["lastAttempt"]:
            raise ValueError("invalid source freshness")
        if source["eligibleFrom"] != (date.fromisoformat(source["collectionStarted"]) + timedelta(days=1)).isoformat() or source["lastAttempt"] < source["collectionStarted"]:
            raise ValueError("invalid observation window")
        if source["status"] not in {"ok", "partial", "failed", "unavailable"}:
            raise ValueError("unknown collection state")
        if not isinstance(source["diagnostics"], dict) or not set(source["diagnostics"]) <= DIAGNOSTICS:
            raise ValueError("non-public diagnostics")
        for n in source["diagnostics"].values():
            count(n)
        metrics(source["metrics"], METRICS, source["eligibleFrom"], source["lastAttempt"])
        if not isinstance(source["days"], dict):
            raise ValueError("invalid daily statistics")
        for key, row in source["days"].items():
            day(key)
            if not source["collectionStarted"] <= key <= source["lastAttempt"]:
                raise ValueError("out-of-window statistics")
            fields(row, {"totals", "combinations", "coverage", "reconciliation", "collection", "validMetrics", "quotaPeaks"})
            bundle(row["totals"])
            if not isinstance(row["combinations"], list):
                raise ValueError("invalid combinations")
            identities = set()
            for combination in row["combinations"]:
                bundle(combination, {"model", "effort", "speed"})
                identity = tuple(combination[k] for k in ("model", "effort", "speed"))
                if identity in identities or identity[0] not in models or identity[1] not in EFFORT_LEVELS | {"unknown"} or identity[2] not in SPEED_LEVELS | {"unknown"}:
                    raise ValueError("invalid joint configuration")
                identities.add(identity)
            for metric in (*COUNTS, "estimatedCost"):
                if not math.isclose(row["totals"][metric], sum(c[metric] for c in row["combinations"]), rel_tol=1e-12, abs_tol=1e-8):
                    raise ValueError("joint aggregation does not conserve totals")
            fields(row["coverage"], {"modelCalls", "effortCalls", "jointCalls", "speedCalls"})
            predicates = {"modelCalls": lambda c: c["model"] != "unknown", "effortCalls": lambda c: c["effort"] != "unknown",
                          "jointCalls": lambda c: c["model"] != "unknown" and c["effort"] != "unknown", "speedCalls": lambda c: c["speed"] != "unknown"}
            for name, predicate in predicates.items():
                count(row["coverage"][name])
                if row["coverage"][name] != sum(c["calls"] for c in row["combinations"] if predicate(c)):
                    raise ValueError("invalid coverage denominator")
            fields(row["reconciliation"], {"ledgerTokens", "state"})
            expected = row["reconciliation"]["ledgerTokens"]
            if expected is not None:
                count(expected)
            state = "unknown" if expected is None else "matched" if expected == row["totals"]["totalTokens"] else "mismatch"
            if row["reconciliation"]["state"] == "empty":
                if expected is not None or row["totals"]["totalTokens"] or row["totals"]["calls"] or row["collection"] != "complete" or source["status"] != "ok":
                    raise ValueError("unverified empty day")
                state = "empty"
            if row["reconciliation"]["state"] != state:
                raise ValueError("invalid reconciliation result")
            if row["collection"] not in {"complete", "open", "unverified"}:
                raise ValueError("invalid daily collection state")
            valid = row["validMetrics"]
            if not isinstance(valid, list) or not set(valid) <= set(METRICS) or len(set(valid)) != len(valid):
                raise ValueError("invalid metric validity")
            if valid and (source["status"] != "ok" or row["collection"] != "complete" or key < source["eligibleFrom"] or key >= source["lastAttempt"]):
                raise ValueError("unverified date marked valid")
            if set(valid) - {"quota"} and state not in {"matched", "empty"} and row["totals"]["identifiedCalls"] != row["totals"]["calls"]:
                raise ValueError("unreconciled usage marked valid")
            if valid and row["totals"]["calls"] == 0 and set(valid) != {"quota"} and state != "empty":
                raise ValueError("unobserved metric declared ready")
            for metric in valid:
                eligible = source["metrics"][metric]["eligibleFrom"]
                if eligible is None or key < eligible:
                    raise ValueError("metric used before its version started")
            if "modelEffort" in valid and row["coverage"]["jointCalls"] != row["totals"]["calls"]:
                raise ValueError("incomplete joint coverage")
            if "speed" in valid and row["coverage"]["speedCalls"] != row["totals"]["calls"]:
                raise ValueError("incomplete speed coverage")
            if "cost" in valid and row["totals"]["pricedCalls"] != row["totals"]["calls"]:
                raise ValueError("incomplete pricing coverage")
            if "quota" in valid and not row["quotaPeaks"]:
                raise ValueError("unobserved quota marked valid")
            if not isinstance(row["quotaPeaks"], dict):
                raise ValueError("invalid quota observations")
            for minutes, percent in row["quotaPeaks"].items():
                if not isinstance(minutes, str) or not minutes.isdigit() or int(minutes) <= 0 or type(percent) not in (int, float) or not 0 <= percent <= 100:
                    raise ValueError("invalid quota peak")
    runs = value["multica"]
    if runs is not None:
        fields(runs, {"collectionStarted", "eligibleFrom", "lastAttempt", "lastSuccess", "status", "metrics", "days", "assignment", "assignmentStatus"})
        for key in ("collectionStarted", "eligibleFrom", "lastAttempt"):
            day(runs[key])
        day(runs["lastSuccess"], nullable=True)
        if runs["lastSuccess"] is not None and not runs["collectionStarted"] <= runs["lastSuccess"] <= runs["lastAttempt"]:
            raise ValueError("invalid run freshness")
        if runs["status"] not in {"ok", "failed", "unavailable"} or runs["eligibleFrom"] <= runs["collectionStarted"]:
            raise ValueError("invalid run collection")
        if runs["assignmentStatus"] not in {"ok", "failed", "unavailable"}:
            raise ValueError("invalid assignment status")
        if runs["assignment"] is not None:
            from multica_dispatch import validate_snapshot
            validate_snapshot(runs["assignment"], models | {"default"})
            if runs["assignment"]["asOf"] > runs["lastAttempt"]:
                raise ValueError("future assignment snapshot")
        elif runs["assignmentStatus"] == "ok":
            raise ValueError("missing assignment snapshot")
        metrics(runs["metrics"], RUN_METRIC_VERSIONS, runs["eligibleFrom"], runs["lastAttempt"])
        if any(runs["metrics"][k]["effectiveFrom"] is not None for k in ("runConfiguration", "runUsage")):
            raise ValueError("run configuration/usage lacks independent verification")
        if not isinstance(runs["days"], dict):
            raise ValueError("invalid run days")
        for key, row in runs["days"].items():
            day(key)
            if not runs["collectionStarted"] <= key <= runs["lastAttempt"]:
                raise ValueError("invalid run date")
            fields(row, {*RUN_COUNTS, "validMetrics"})
            for name in RUN_COUNTS:
                count(row[name])
            if not isinstance(row["validMetrics"], list) or not set(row["validMetrics"]) <= {"runs", "runDuration"}:
                raise ValueError("unverified run metric")
            for name in row["validMetrics"]:
                start = runs["metrics"][name]["eligibleFrom"]
                if runs["status"] != "ok" or start is None or not start <= key < runs["lastAttempt"]:
                    raise ValueError("run metric used before activation")
            if "runDuration" in row["validMetrics"] and row["durationKnownRuns"] != row["total"]:
                raise ValueError("incomplete run duration")
            if row["total"] != sum(row[k] for k in STATUSES) or row["durationKnownRuns"] > sum(row[k] for k in ("completed", "failed", "cancelled")) or row["linkedRuns"] > row["durationKnownRuns"]:
                raise ValueError("run conservation failed")
