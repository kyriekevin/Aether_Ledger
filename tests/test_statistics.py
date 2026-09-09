from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import statistics_readers as readers
from statistics_store import Journal, export_source, summarize, estimate
from statistics_schema import validate
from statistics_multica import collect_runs, export_runs
from usage_schema import SHANGHAI

MODELS = {"model-a", "model-b", "unknown"}
PRICING = {"models": {"model-a": {"rates": [{"effectiveFrom": "2026-01-01", "input": 1, "output": 2, "cacheRead": .1, "cacheWrite": 1}]}}}


def fact(day="2026-09-10", tokens=100, effort="high", model="model-a", identity="call", session="session", basis="counter"):
    reading = readers.Reading(present=True)
    reading.add("codex", session, identity, day + "T10:00:00+08:00", model, effort, "standard",
                {"inputTokens": tokens - 20, "outputTokens": 10, "cacheReadTokens": 10, "cacheCreationTokens": 0}, basis=basis)
    return reading


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, events):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return p

    def token(self, total, at, cumulative):
        return {"type": "event_msg", "timestamp": at, "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": total - 10, "cached_input_tokens": 20, "output_tokens": 10, "total_tokens": total},
            "total_token_usage": {"total_tokens": cumulative}}}}

    def test_codex_replays_changes_and_unknown_effort(self):
        first = self.token(100, "2026-09-10T01:00:00Z", 100)
        second = self.token(200, "2026-09-10T02:00:00Z", 300)
        replay = self.token(100, "2026-09-10T03:00:00Z", 100)
        self.write("session.jsonl", [
            {"type": "session_meta", "payload": {"id": "private-session"}},
            {"type": "turn_context", "payload": {"model": "model-a", "effort": "low"}}, first,
            {"type": "turn_context", "payload": {"model": "model-b"}}, second, replay])
        result = readers.codex([self.root])
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.diagnostics["ambiguousCounter"], 1)
        self.assertEqual(len(result.observations), 2)
        summary = summarize(result.observations.values(), MODELS, PRICING)
        self.assertEqual(summary["totals"]["totalTokens"], 300)
        self.assertEqual(summary["totals"]["calls"], 2)
        self.assertEqual(summary["coverage"]["jointCalls"], 1)
        self.assertEqual({(c["model"], c["effort"]) for c in summary["combinations"]}, {("model-a", "low"), ("model-b", "unknown")})

    def test_native_responses_replace_scoped_legacy_counters_without_double_counting(self):
        legacy = self.token(100, "2026-09-10T01:00:01Z", 900)
        usage = legacy["payload"]["info"]["last_token_usage"]
        native = {"type": "token_usage_record", "timestamp": "2026-09-10T01:00:00Z", "ordinal": 2,
                  "payload": {"thread_id": "session", "session_id": "session", "response_id": "response",
                              "usage": usage, "thread_token_usage": {"total_tokens": 900}, "turn_token_usage": {"total_tokens": 100}}}
        reset = self.token(100, "2026-09-10T01:00:02Z", 100)
        adjustment = copy.deepcopy(reset)
        adjustment["timestamp"] = "2026-09-10T01:00:03Z"
        adjustment["payload"]["info"]["last_token_usage"] = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "total_tokens": 50}
        self.write("session.jsonl", [{"type": "session_meta", "payload": {"id": "session"}},
                   {"type": "turn_context", "payload": {"model": "model-a", "effort": "high"}}, native, legacy, reset, adjustment, native])
        result = readers.codex([self.root])
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(sum(f["totalTokens"] for f in result.observations.values()), 100)

    def test_quota_does_not_require_token_or_effort_observation(self):
        self.write("quota.jsonl", [{"timestamp": "2026-09-10T01:00:00Z", "payload": {"type": "token_count", "info": None,
            "rate_limits": {"secondary": {"window_minutes": 10080, "used_percent": 81}}}}])
        result = readers.codex([self.root])
        self.assertEqual(result.quotas, {"2026-09-10": {"10080": 81}})
        self.assertFalse(result.observations)

    def test_invalid_token_components_cannot_pass_as_complete(self):
        event = self.token(100, "2026-09-10T01:00:00Z", 100)
        event["payload"]["info"]["last_token_usage"]["total_tokens"] = 200
        self.write("bad.jsonl", [event])
        result = readers.codex([self.root])
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.diagnostics["inconsistentTokens"], 1)
        self.assertFalse(result.observations)

    def test_claude_stream_message_moves_as_one_bundle(self):
        a = {"type": "assistant", "timestamp": "2026-09-10T01:00:00Z", "sessionId": "private-session",
             "message": {"id": "private-message", "model": "model-a", "usage": {"input_tokens": 30, "output_tokens": 5}}}
        b = copy.deepcopy(a)
        b["effort"] = "high"
        b["message"]["model"] = "model-b"
        b["message"]["usage"]["output_tokens"] = 15
        self.write("session.jsonl", [a, b, a])
        result = readers.claude(self.root)
        self.assertEqual(len(result.observations), 1)
        value = next(iter(result.observations.values()))
        self.assertEqual((value["model"], value["effort"], value["totalTokens"]), ("model-b", "high", 45))
        self.assertNotIn("private", json.dumps(summarize(result.observations.values(), MODELS, PRICING)))

    def test_claude_copied_message_without_session_field_still_counts_once(self):
        event = {"type": "assistant", "timestamp": "2026-09-10T01:00:00Z", "effort": "high",
                 "message": {"id": "message-id", "model": "model-a", "usage": {"input_tokens": 30, "output_tokens": 5}}}
        self.write("original.jsonl", [event])
        self.write("copy.jsonl", [event])
        self.assertEqual(len(readers.claude(self.root).observations), 1)

    def test_dsh_deduplicates_copies_and_keeps_effort(self):
        events = [{"type": "session", "id": "private-session"},
                  {"type": "request/header", "data": {"header": {"config": {"model": "model-a", "reasoningEffort": "off"}}}},
                  {"type": "assistant/message", "time": 1789005600000, "seq": 3,
                   "data": {"turn": 1, "step": 1, "usage": {"inputTokens": 30, "outputTokens": 10, "cacheReadTokens": 60}}}]
        self.write("one/session.jsonl", events)
        self.write("two/session.jsonl", events)
        result = readers.dsh([self.root])
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(next(iter(result.observations.values()))["effort"], "none")


class JournalFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.j = Journal(Path(self.tmp.name) / "journal.sqlite3")
        self.addCleanup(self.j.close)

    def export(self, now, ledger):
        return export_source(self.j, "codex", ledger, now, MODELS, PRICING)


class JournalTests(JournalFixture):
    def test_repeated_reads_rotation_and_reclassification_conserve(self):
        now = date(2026, 9, 10)
        original = fact()
        self.j.ingest("codex", original, now)
        self.j.ingest("codex", original, now)
        self.j.ingest("codex", readers.Reading(present=True), now)
        self.assertEqual(len(self.j.facts()), 1)
        # A deliberate correction moves the existing fact, not both buckets.
        self.j.ingest("codex", fact(tokens=80, effort="low", model="model-b"), now, reconcile=True)
        result = self.export(now, {"2026-09-10": {"totalTokens": 80}})
        day = result["days"]["2026-09-10"]
        self.assertEqual(day["totals"]["totalTokens"], 80)
        self.assertEqual(len(day["combinations"]), 1)
        self.assertEqual(day["combinations"][0]["model"], "model-b")
        self.assertEqual(day["combinations"][0]["effort"], "low")

    def test_first_full_day_activates_only_after_closed_day_reconciliation(self):
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 9))
        self.j.ingest("codex", fact(), date(2026, 9, 10))
        ledger = {"2026-09-10": {"totalTokens": 100}}
        same_day = self.export(date(2026, 9, 10), ledger)
        self.assertIsNone(same_day["metrics"]["modelEffort"]["effectiveFrom"])
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 11))
        result = self.export(date(2026, 9, 11), ledger)
        self.assertEqual(result["metrics"]["modelEffort"]["effectiveFrom"], "2026-09-10")
        self.assertEqual(result["days"]["2026-09-10"]["validMetrics"], ["usage", "modelEffort", "speed", "cost"])
        validate({"schemaVersion": 1, "role": "work", "sources": {"codex": result}, "multica": None}, MODELS)

    def test_unknown_effort_and_mismatch_never_activate_joint_metric(self):
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 9))
        self.j.ingest("codex", fact(effort=None), date(2026, 9, 10))
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 11))
        result = self.export(date(2026, 9, 11), {"2026-09-10": {"totalTokens": 200}})
        self.assertIsNone(result["metrics"]["usage"]["effectiveFrom"])
        self.assertEqual(result["days"]["2026-09-10"]["reconciliation"]["state"], "mismatch")
        result = self.export(date(2026, 9, 11), {"2026-09-10": {"totalTokens": 100}})
        self.assertEqual(result["metrics"]["usage"]["effectiveFrom"], "2026-09-10")
        self.assertIsNone(result["metrics"]["modelEffort"]["effectiveFrom"])

    def test_native_identity_stream_can_activate_while_retaining_legacy_difference(self):
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 9))
        self.j.ingest("codex", fact(basis="response"), date(2026, 9, 10))
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 11))
        result = self.export(date(2026, 9, 11), {"2026-09-10": {"totalTokens": 200}})
        observed = result["days"]["2026-09-10"]
        self.assertEqual(observed["totals"]["totalTokens"], 100)
        self.assertEqual(observed["reconciliation"]["state"], "mismatch")
        self.assertEqual(observed["totals"]["identifiedCalls"], 1)
        self.assertEqual(result["metrics"]["usage"]["effectiveFrom"], "2026-09-10")
        validate({"schemaVersion": 1, "role": "work", "sources": {"codex": result}, "multica": None}, MODELS)

    def test_unavailable_source_does_not_start_its_validity_clock(self):
        self.j.ingest("codex", readers.Reading(), date(2026, 9, 9))
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 12))
        result = self.export(date(2026, 9, 12), {})
        self.assertEqual(result["collectionStarted"], "2026-09-09")
        self.assertEqual(result["metrics"]["usage"]["eligibleFrom"], "2026-09-13")

    def test_source_overlap_does_not_duplicate_usage(self):
        self.j.ingest("codex", fact(), date(2026, 9, 10))
        meta = self.j.ingest("codex-multica", fact(), date(2026, 9, 10))
        self.assertEqual(meta["status"], "partial")
        self.assertEqual(meta["diagnostics"]["sourceOverlap"], 1)
        self.assertEqual(len(self.j.facts()), 1)

    def test_private_fields_and_false_denominators_are_rejected(self):
        self.j.ingest("codex", fact(), date(2026, 9, 10))
        source = self.export(date(2026, 9, 10), {})
        baseline = {"schemaVersion": 1, "role": "work", "sources": {"codex": source}, "multica": None}
        validate(baseline, MODELS)
        mutations = [lambda s: s.update(session="private"),
                     lambda s: s["sources"]["codex"]["days"]["2026-09-10"]["coverage"].update(jointCalls=99),
                     lambda s: s["sources"]["codex"]["days"]["2026-09-10"]["combinations"][0].update(model="private-model"),
                     lambda s: s["sources"]["codex"]["metrics"]["usage"].update(effectiveFrom="2026-09-09")]
        for mutate in mutations:
            candidate = copy.deepcopy(baseline)
            mutate(candidate)
            with self.assertRaises(ValueError):
                validate(candidate, MODELS)

    def test_long_context_and_fast_are_priced_per_observation(self):
        f = next(iter(fact(tokens=300_100).observations.values()))
        f["speed"] = "fast"
        pricing = {"models": {"model-a": {"rates": [{"effectiveFrom": "2026-01-01", "input": 1, "output": 2, "cacheRead": .1, "cacheWrite": 1,
                   "longContextThreshold": 272000, "longInput": 2, "longOutput": 3, "longCacheRead": .2, "longCacheWrite": 2, "fastMultiplier": 2}]}}}
        self.assertAlmostEqual(estimate(f, pricing), (300080 * 2 + 10 * 3 + 10 * .2) / 1e6 * 2)
        f["speed"] = "unknown"
        self.assertIsNone(estimate(f, pricing))

    def test_run_duration_has_a_separate_activation_gate(self):
        run = {"key": "run", "harness": "codex", "session": None, "day": "2026-09-10",
               "started": "2026-09-10T09:00:00+08:00", "ended": None, "status": "completed"}
        meta = {"collectionStarted": "2026-09-09", "successDays": ["2026-09-09", "2026-09-10", "2026-09-11"],
                "lastAttempt": "2026-09-11", "status": "ok"}
        with self.j.db:
            self.j.save_meta("multica-runs", meta)
            self.j.db.execute("INSERT INTO runs VALUES (?,?)", (run["key"], json.dumps(run)))
        result = export_runs(self.j, date(2026, 9, 11))
        self.assertEqual(result["metrics"]["runs"]["effectiveFrom"], "2026-09-10")
        self.assertIsNone(result["metrics"]["runDuration"]["effectiveFrom"])
        self.assertNotIn("runDuration", result["days"]["2026-09-10"]["validMetrics"])
        run["ended"] = "2026-09-10T10:00:00+08:00"
        with self.j.db:
            self.j.db.execute("UPDATE runs SET body=? WHERE key=?", (json.dumps(run), run["key"]))
        result = export_runs(self.j, date(2026, 9, 11))
        self.assertEqual(result["metrics"]["runDuration"]["effectiveFrom"], "2026-09-10")
        self.assertEqual(result["days"]["2026-09-10"]["durationSeconds"], 3600)

    def test_exact_run_link_excludes_overlapping_runs(self):
        self.j.ingest("codex", fact(), date(2026, 9, 10))
        source = next(iter(fact().observations.values()))
        run = {"key": "private-run", "harness": "codex", "session": source["session"], "day": "2026-09-10",
               "started": "2026-09-10T09:00:00+08:00", "ended": "2026-09-10T11:00:00+08:00", "status": "completed"}
        meta = {"collectionStarted": "2026-09-09", "successDays": ["2026-09-10", "2026-09-11"], "lastAttempt": "2026-09-11", "status": "ok", "effectiveFrom": None}
        with self.j.db:
            self.j.save_meta("multica-runs", meta)
            self.j.db.execute("INSERT INTO runs VALUES (?,?)", (run["key"], json.dumps(run)))
        result = export_runs(self.j, date(2026, 9, 11))
        self.assertEqual(result["days"]["2026-09-10"]["linkedTokens"], 100)
        self.assertIsNone(result["metrics"]["runUsage"]["effectiveFrom"])
        run["key"] = "other-run"
        with self.j.db:
            self.j.db.execute("INSERT INTO runs VALUES (?,?)", (run["key"], json.dumps(run)))
        result = export_runs(self.j, date(2026, 9, 11))
        self.assertEqual(result["days"]["2026-09-10"]["linkedTokens"], 0)
        validate({"schemaVersion": 1, "role": "work", "sources": {}, "multica": result}, MODELS)

class VersionAndCollectionTests(JournalFixture):
    def test_common_dates_respect_each_metric_and_missing_days(self):
        from statistics_store import common_valid_days
        one = {"status": "ok", "metrics": {"usage": {"effectiveFrom": "2026-09-10"}},
               "days": {"2026-09-10": {"validMetrics": ["usage"]}, "2026-09-11": {"validMetrics": []}, "2026-09-12": {"validMetrics": ["usage"]}}}
        two = {"status": "ok", "metrics": {"modelEffort": {"effectiveFrom": "2026-09-11"}},
               "days": {"2026-09-11": {"validMetrics": ["modelEffort"]}, "2026-09-12": {"validMetrics": ["modelEffort"]}}}
        self.assertEqual(common_valid_days([(one, "usage"), (two, "modelEffort")]), ["2026-09-12"])
        two["status"] = "failed"
        self.assertEqual(common_valid_days([(one, "usage"), (two, "modelEffort")]), [])

    def test_metric_upgrade_does_not_restart_unaffected_metrics(self):
        import statistics_store
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 9))
        self.j.ingest("codex", fact(), date(2026, 9, 10))
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 11))
        ledger = {"2026-09-10": {"totalTokens": 100}}
        self.export(date(2026, 9, 11), ledger)
        with patch.dict(statistics_store.METRIC_VERSIONS, {"modelEffort": 2}):
            self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 12))
            result = self.export(date(2026, 9, 12), ledger)
        self.assertEqual(result["metrics"]["usage"]["effectiveFrom"], "2026-09-10")
        self.assertEqual(result["metrics"]["modelEffort"], {"version": 2, "eligibleFrom": "2026-09-13", "effectiveFrom": None})

    def test_verified_empty_day_differs_from_failed_day(self):
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 9))
        self.j.ingest("codex", fact(), date(2026, 9, 10))
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 11))
        ledger = {"2026-09-10": {"totalTokens": 100}}
        self.export(date(2026, 9, 11), ledger)
        self.j.ingest("codex", readers.Reading(present=True), date(2026, 9, 12))
        result = self.export(date(2026, 9, 12), ledger)
        empty = result["days"]["2026-09-11"]
        self.assertEqual(empty["reconciliation"]["state"], "empty")
        self.assertIn("usage", empty["validMetrics"])
        validate({"schemaVersion": 1, "role": "work", "sources": {"codex": result}, "multica": None}, MODELS)
        self.j.ingest("codex", readers.Reading(failed=True), date(2026, 9, 13))
        result = self.export(date(2026, 9, 13), ledger)
        self.assertEqual(result["days"]["2026-09-12"]["collection"], "unverified")
        self.assertEqual(result["lastSuccess"], "2026-09-12")

    def test_multica_role_isolation_deduplication_and_failure_atomicity(self):
        run = {"id": "private-run", "runtime_id": "work-runtime", "started_at": "2026-09-10T01:00:00Z", "completed_at": "2026-09-10T01:10:00Z", "status": "completed", "result": {"session_id": "private-session", "output": "private-output"}}
        def read(args):
            if args[:2] == ["runtime", "list"]:
                return [{"id": "work-runtime", "provider": "codex", "custom_name": "work-device"}, {"id": "personal-runtime", "provider": "codex", "custom_name": "personal-device"}]
            if args[:2] == ["issue", "list"]:
                return {"issues": [{"id": "private-issue"}, {"id": "private-issue"}], "has_more": False}
            return [run, run, dict(run, id="personal-run", runtime_id="personal-runtime")]
        now = datetime(2026, 9, 10, 18, tzinfo=SHANGHAI)
        collect_runs(self.j, "work", {"work-device": "work", "personal-device": "personal"}, now, read)
        result = export_runs(self.j, now.date())
        self.assertEqual(result["days"]["2026-09-10"]["total"], 1)
        self.assertEqual(result["days"]["2026-09-10"]["durationSeconds"], 600)
        self.assertNotIn("private", json.dumps(result))
        def failed(args):
            raise RuntimeError("private details")
        collect_runs(self.j, "work", {}, datetime(2026, 9, 11, 18, tzinfo=SHANGHAI), failed)
        result = export_runs(self.j, date(2026, 9, 11))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["days"]["2026-09-10"]["total"], 1)
        self.assertEqual(result["lastSuccess"], "2026-09-10")


if __name__ == "__main__":
    unittest.main()
