from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claude_statusline_proxy as proxy  # noqa: E402
import install_claude_statusline as installer  # noqa: E402


class ClaudeStatuslineCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cache = self.root / "quota.json"
        self.tz = ZoneInfo("Asia/Shanghai")

    def payload(self, five: object, seven: object) -> str:
        return json.dumps({
            "cwd": "/private/project",
            "transcript_path": "/private/session.jsonl",
            "rate_limits": {
                "five_hour": {"used_percentage": five, "resets_at": 123},
                "seven_day": {"used_percentage": seven, "resets_at": 456},
            },
        })

    def test_keeps_daily_high_waters_without_identity_or_reset_times(self) -> None:
        day = datetime(2026, 8, 18, 10, 0, tzinfo=self.tz)
        self.assertTrue(proxy.capture_rate_limits(self.payload(40, 20), self.cache, day))
        self.assertTrue(proxy.capture_rate_limits(self.payload(30, 25), self.cache, day))
        tomorrow = datetime(2026, 8, 19, 1, 0, tzinfo=self.tz)
        self.assertTrue(
            proxy.capture_rate_limits(self.payload(5, 26), self.cache, tomorrow)
        )

        stored = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["days"]["2026-08-18"]["windows"],
            {"300": 40.0, "10080": 25.0},
        )
        self.assertEqual(
            stored["days"]["2026-08-19"]["windows"],
            {"300": 5.0, "10080": 26.0},
        )
        serialized = json.dumps(stored)
        for forbidden in ("private", "transcript", "resets_at", "cwd"):
            self.assertNotIn(forbidden, serialized)

    def test_missing_or_malformed_rate_limits_do_not_create_cache(self) -> None:
        self.assertFalse(proxy.capture_rate_limits("not json", self.cache))
        self.assertFalse(proxy.capture_rate_limits("{}", self.cache))
        self.assertFalse(
            proxy.capture_rate_limits(self.payload("unknown", None), self.cache)
        )
        self.assertFalse(self.cache.exists())

    def test_main_forwards_original_stdin_even_when_capture_fails(self) -> None:
        raw = '{"rate_limits":null}'
        completed = type("Completed", (), {"returncode": 7})()
        with patch.object(proxy.sys, "stdin", io.StringIO(raw)), \
                patch.object(proxy, "capture_rate_limits", side_effect=OSError), \
                patch.object(proxy, "_original_command", return_value="hud"), \
                patch.object(proxy.subprocess, "run", return_value=completed) as run:
            self.assertEqual(proxy.main(), 7)
        self.assertEqual(run.call_args.args[0], ["/bin/bash", "-c", "hud"])
        self.assertEqual(run.call_args.kwargs["input"], raw)


class ClaudeStatuslineInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.settings = self.root / "settings.json"
        self.install_dir = self.root / "install"
        self.config = self.root / "config.json"
        self.settings.write_text(json.dumps({
            "permissions": {"allow": []},
            "statusLine": {"type": "command", "command": "existing hud"},
        }), encoding="utf-8")

    def install(self, *, dry_run: bool = False) -> bool:
        return installer.install(
            self.settings,
            self.install_dir,
            self.config,
            "/usr/bin/python3",
            dry_run=dry_run,
        )

    def test_install_is_idempotent_and_uninstall_restores_command(self) -> None:
        self.assertTrue(self.install())
        installed_settings = json.loads(self.settings.read_text(encoding="utf-8"))
        installed_command = installed_settings["statusLine"]["command"]
        self.assertIn("claude_statusline_proxy.py", installed_command)
        self.assertTrue((self.install_dir / "claude_statusline_proxy.py").exists())
        self.assertEqual(
            json.loads(self.config.read_text(encoding="utf-8"))["originalCommand"],
            "existing hud",
        )
        self.assertFalse(self.install())
        self.assertTrue(installer.uninstall(self.settings, self.config))
        restored = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(restored["statusLine"]["command"], "existing hud")

    def test_dry_run_writes_nothing(self) -> None:
        before = self.settings.read_text(encoding="utf-8")
        self.assertTrue(self.install(dry_run=True))
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)
        self.assertFalse(self.install_dir.exists())
        self.assertFalse(self.config.exists())

    def test_uninstall_refuses_to_overwrite_a_later_user_change(self) -> None:
        self.install()
        payload = json.loads(self.settings.read_text(encoding="utf-8"))
        payload["statusLine"]["command"] = "new user hud"
        self.settings.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed after installation"):
            installer.uninstall(self.settings, self.config)


if __name__ == "__main__":
    unittest.main()
