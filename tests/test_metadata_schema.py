"""Retained snapshots remain auditable after their collectors are removed."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from metadata_schema import ACTIVITY_STATES, validate_activity, validate_snapshot
from statistics_schema import RUN_METRIC_VERSIONS, validate


class MetadataSchemaTests(unittest.TestCase):
    def test_statistics_assignment_uses_standalone_validator(self):
        assignment = {"asOf": "2026-09-12", "coveredRoles": ["work"], "unmappedAgents": 0,
                      "unassignedIssues": 0, "totalIssues": 0, "configurations": []}
        runs = {"collectionStarted": "2026-09-10", "eligibleFrom": "2026-09-11",
                "lastAttempt": "2026-09-12", "lastSuccess": "2026-09-12", "status": "ok",
                "metrics": {name: {"version": version, "eligibleFrom": "2026-09-11", "effectiveFrom": None}
                            for name, version in RUN_METRIC_VERSIONS.items()},
                "days": {}, "assignment": assignment, "assignmentStatus": "ok"}
        value = {"schemaVersion": 1, "role": "work", "sources": {}, "multica": runs}
        validate(value, {"unknown"})
        invalid = copy.deepcopy(value)
        invalid["multica"]["assignment"]["configurations"] = [{"prompt": "private"}]
        with self.assertRaises(ValueError):
            validate(invalid, {"unknown"})
        assignment["totalIssues"] = 1
        with self.assertRaises(ValueError):
            validate_snapshot(assignment, {"unknown"})

    def test_activity_still_rejects_private_fields_and_inconsistent_totals(self):
        value = {
            "schemaVersion": 1, "role": "work", "collectionStarted": "2026-09-10",
            "lastAttempt": "2026-09-12", "lastSuccess": "2026-09-12", "status": "ok",
            "metrics": {"activity": {"version": 1, "eligibleFrom": "2026-09-11", "effectiveFrom": None}},
            "coverage": {"missingParents": 0, "missingTriggers": 0, "unknownEvents": 0},
            "retained": {"issues": 0, "comments": 0, "runs": 0, "activities": 0},
            "scope": {"scannedIssues": 0, "scopedIssues": 0},
            "current": {"available": True, "issues": 0, "statuses": dict.fromkeys(ACTIVITY_STATES, 0),
                        "humanCommentDistribution": {
                            name: dict.fromkeys(("zero", "oneTwo", "threeFive", "sixTen", "elevenPlus"), 0)
                            for name in ("parent", "child", "standalone")}},
            "days": {},
        }
        validate_activity(value)
        invalid = copy.deepcopy(value)
        invalid["current"]["prompt"] = "private"
        with self.assertRaises(ValueError):
            validate_activity(invalid)
        value["current"]["issues"] = 1
        with self.assertRaises(ValueError):
            validate_activity(value)
