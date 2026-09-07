from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import usage_telemetry


class RepeatedUsageTests(unittest.TestCase):
    def read(self, events, since=date(2026, 9, 7)):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "rollout-example.jsonl").write_text("\n".join(map(json.dumps, events)))
            return usage_telemetry.collect_codex_routing_since(since, root)

    def events(self):
        usage = {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 100,
                 "reasoning_output_tokens": 50, "total_tokens": 1100}
        return [
            {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "high"}},
            {"type": "event_msg", "payload": {"type": "thread_settings_applied",
             "thread_settings": {"service_tier": "priority"}}},
            {"timestamp": "2026-09-07T01:00:00Z", "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage},
                "rate_limits": {"secondary": {"window_minutes": 10080, "used_percent": 10}}}},
        ]

    def test_repeated_request_is_counted_once_but_new_quota_is_kept(self):
        events = self.events()
        duplicate = copy.deepcopy(events[-1])
        duplicate["payload"]["rate_limits"]["secondary"]["used_percent"] = 20
        events.append(duplicate)
        day = self.read(events)["2026-09-07"]
        effort = day["routing"]["efforts"]["high"]
        self.assertEqual(effort, {"calls": 1, "totalTokens": 1100,
                                  "reasoningCalls": 1, "reasoningOutputTokens": 50})
        self.assertEqual(day["routing"]["speeds"]["fast"]["calls"], 1)
        self.assertEqual(day["quota"]["windows"]["10080"], 20)

    def test_distinct_requests_with_equal_last_usage_still_count_twice(self):
        events = self.events()
        second = copy.deepcopy(events[-1])
        second["payload"]["info"]["total_token_usage"] = {"total_tokens": 2200}
        events.append(second)
        self.assertEqual(self.read(events)["2026-09-07"]["routing"]["efforts"]["high"]["calls"], 2)

    def test_repeat_across_since_boundary_does_not_create_a_new_call(self):
        events = self.events()
        events[-1]["timestamp"] = "2026-09-06T15:59:00Z"
        duplicate = copy.deepcopy(events[-1])
        duplicate["timestamp"] = "2026-09-06T16:01:00Z"
        events.append(duplicate)
        day = self.read(events)["2026-09-07"]
        self.assertNotIn("routing", day)
        self.assertEqual(day["quota"]["windows"]["10080"], 10)
