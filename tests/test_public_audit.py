from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from audit_public import _validate_store_schema, _walk_json  # noqa: E402
import sync_usage  # noqa: E402
from sync_usage import NODE_ID_RE, _opaque_node_id  # noqa: E402


class PublicAuditTests(unittest.TestCase):
    def test_rejects_session_path_keys_at_any_depth(self) -> None:
        issues: list[str] = []
        _walk_json(
            {"repos": {"example": {"cwd": "/private/project"}}, "source": "archive"},
            Path("node-a1b2c3d4e5f6/codex.json"),
            issues,
        )
        self.assertEqual(len(issues), 2)
        self.assertTrue(any("'cwd'" in issue for issue in issues))
        self.assertTrue(any("'source'" in issue for issue in issues))

    def test_node_ids_are_opaque_and_stable(self) -> None:
        first = _opaque_node_id("worker-hostname")
        self.assertEqual(first, _opaque_node_id("worker-hostname"))
        self.assertRegex(first, NODE_ID_RE)
        self.assertNotIn("worker", first)

    def test_store_schema_rejects_unapproved_metadata(self) -> None:
        issues: list[str] = []
        _validate_store_schema(
            {
                "2026-08-01": {
                    "totalTokens": 10,
                    "prompt": "private",
                    "sessionId": "session",
                    "hostname": "laptop",
                    "repository": "private-repo",
                }
            },
            Path("data/personal/codex.json"),
            issues,
        )
        self.assertEqual(len(issues), 4)
        for key in ("prompt", "sessionId", "hostname", "repository"):
            self.assertTrue(any(repr(key) in issue for issue in issues))

    def test_store_schema_accepts_public_pricing_provenance_and_token_buckets(self) -> None:
        issues: list[str] = []
        _validate_store_schema(
            {
                "2026-08-01": {
                    "totalTokens": 10,
                    "totalCost": 0.1,
                    "costSource": "official",
                    "models": {
                        "gpt-example": {
                            "totalTokens": 10,
                            "inputTokens": 4,
                            "outputTokens": 1,
                            "cacheCreationTokens": 2,
                            "cacheReadTokens": 3,
                        }
                    },
                }
            },
            Path("data/personal/codex.json"),
            issues,
        )
        self.assertEqual(issues, [])

    def test_store_schema_accepts_an_explicit_unpriced_cost(self) -> None:
        issues: list[str] = []
        _validate_store_schema(
            {
                "2026-08-01": {
                    "totalTokens": 10,
                    "totalCost": 0.0,
                    "costSource": "unpriced",
                }
            },
            Path("data/personal/codex.json"),
            issues,
        )
        self.assertEqual(issues, [])

    def test_trail_machine_path_never_contains_raw_identity(self) -> None:
        raw = "worker-hostname-with-job-id"
        previous = os.environ.get(sync_usage.TRAIL_ENV)
        try:
            os.environ[sync_usage.TRAIL_ENV] = raw
            resolved = sync_usage.resolve_machine()
        finally:
            if previous is None:
                os.environ.pop(sync_usage.TRAIL_ENV, None)
            else:
                os.environ[sync_usage.TRAIL_ENV] = previous

        self.assertRegex(resolved, r"^data/trail/node-[0-9a-f]{12}$")
        self.assertNotIn(raw, resolved)

    def test_durable_machine_uses_approved_public_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            node_name = Path(directory) / "node_name"
            node_name.write_text("personal\n")
            with (
                patch.object(sync_usage, "NODE_NAME_FILE", node_name),
                patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(sync_usage.resolve_machine(), "data/personal")


if __name__ == "__main__":
    unittest.main()
