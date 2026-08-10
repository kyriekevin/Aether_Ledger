from __future__ import annotations

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from install_launchd import (  # noqa: E402
    LABEL,
    LEGACY_LABEL,
    migrate_legacy_node_name,
    reload_agent,
    remove_legacy_agent,
    render_template,
)


class InstallLaunchdTests(unittest.TestCase):
    def test_rendered_plist_uses_current_paths_without_placeholders(self) -> None:
        home = Path("/tmp/example & home")
        repo = Path("/tmp/example & repo")
        rendered = render_template(home, repo)
        parsed = plistlib.loads(rendered.encode())

        self.assertNotIn("__HOME__", rendered)
        self.assertNotIn("__REPO_DIR__", rendered)
        self.assertEqual(parsed["EnvironmentVariables"]["HOME"], str(home))
        self.assertEqual(
            parsed["ProgramArguments"][-1],
            str(repo / "scripts" / "sync_usage.py"),
        )
        self.assertNotIn("StartInterval", parsed)
        self.assertEqual(
            parsed["StartCalendarInterval"],
            [{"Minute": 0}, {"Minute": 15}, {"Minute": 30}, {"Minute": 45}],
        )

    def test_reload_replaces_loaded_agent_with_rendered_plist(self) -> None:
        destination = Path("/tmp/com.example.agent.plist")
        with patch("install_launchd.subprocess.run") as run:
            bootstrap = run.return_value
            self.assertIs(reload_agent(destination, 501), bootstrap)

        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["launchctl", "bootout", f"gui/501/{LABEL}"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["launchctl", "bootstrap", "gui/501", str(destination)],
        )
        for call in run.call_args_list:
            self.assertEqual(
                call.kwargs,
                {"capture_output": True, "text": True, "check": False},
            )

    def test_removes_and_unloads_legacy_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("legacy")

            with patch("install_launchd.subprocess.run") as run:
                self.assertTrue(remove_legacy_agent(home, 501))

            self.assertFalse(legacy.exists())
            run.assert_called_once_with(
                ["launchctl", "bootout", "gui/501", str(legacy)],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_migrates_approved_legacy_node_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / ".config" / "feishu-claude-usage" / "machine_name"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("personal\n")

            self.assertEqual(migrate_legacy_node_name(home), "personal")
            self.assertEqual(
                (home / ".config" / "token-activity" / "node_name").read_text(),
                "personal\n",
            )


if __name__ == "__main__":
    unittest.main()
