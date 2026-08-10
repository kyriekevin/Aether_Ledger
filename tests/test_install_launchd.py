from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from install_launchd import (  # noqa: E402
    LABEL,
    LEGACY_LABEL,
    ensure_writer_worktree,
    migrate_legacy_node_name,
    reload_agent,
    remove_legacy_agent,
    render_template,
)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


class InstallLaunchdTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        repo = root / "source"
        git(root, "init", "-b", "main", str(repo))
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.com")
        (repo / "README.md").write_text("base\n")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "base")
        return repo

    def test_creates_writer_on_the_existing_daily_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.make_repo(root)
            git(repo, "branch", "usage/2026-08-10")
            writer = root / "writer"

            self.assertEqual(
                ensure_writer_worktree(writer, repo, date(2026, 8, 10)),
                writer.resolve(),
            )

            self.assertEqual(git(repo, "branch", "--show-current").stdout.strip(), "main")
            self.assertEqual(
                git(writer, "branch", "--show-current").stdout.strip(),
                "usage/2026-08-10",
            )
            self.assertEqual(
                ensure_writer_worktree(writer, repo, date(2026, 8, 10)),
                writer.resolve(),
            )

    def test_creates_detached_writer_from_main_before_the_daily_branch_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.make_repo(root)
            writer = root / "writer"

            ensure_writer_worktree(writer, repo, date(2026, 8, 10))

            self.assertEqual(git(writer, "branch", "--show-current").stdout.strip(), "")
            self.assertEqual(
                git(writer, "rev-parse", "HEAD").stdout,
                git(repo, "rev-parse", "main").stdout,
            )

    def test_refuses_to_steal_the_daily_branch_from_the_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.make_repo(root)
            git(repo, "switch", "-c", "usage/2026-08-10")

            with self.assertRaisesRegex(RuntimeError, "switch the source checkout off"):
                ensure_writer_worktree(root / "writer", repo, date(2026, 8, 10))

    def test_refuses_an_unrelated_repository_at_the_writer_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.make_repo(root / "source-parent")
            unrelated = self.make_repo(root / "writer-parent")

            with self.assertRaisesRegex(RuntimeError, "different Git repository"):
                ensure_writer_worktree(unrelated, repo, date(2026, 8, 10))

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
