from __future__ import annotations

import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_usage  # noqa: E402


def result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class DailyBranchTests(unittest.TestCase):
    def test_usage_commit_subject_is_conventional_and_stable(self) -> None:
        self.assertEqual(
            sync_usage.usage_commit_message("node-a1b2c3d4e5f6"),
            "chore(data): sync node-a1b2c3d4e5f6 usage",
        )

    def test_rollover_uses_conventional_snapshot_subject(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "daily-rollover.yml"
        ).read_text()
        self.assertIn('git commit -m "chore(data): finalize $day snapshot"', workflow)

    def test_rollover_audits_squashed_data_before_commit(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "daily-rollover.yml"
        ).read_text()
        merge = workflow.index('git merge --squash "origin/$branch"')
        audit = workflow.index("uv run --script scripts/audit_public.py", merge)
        commit = workflow.index('git commit -m "chore(data): finalize $day snapshot"')
        self.assertLess(merge, audit)
        self.assertLess(audit, commit)

    @patch.object(sync_usage, "_branch_is_ahead", return_value=False)
    @patch.object(sync_usage, "_current_branch", return_value="main")
    def test_waits_while_any_older_branch_still_exists(self, _current, _ahead) -> None:
        with (
            patch.object(sync_usage, "_ref_exists", return_value=False),
            patch.object(
                sync_usage,
                "_pending_daily_branches",
                return_value=["usage/2026-07-30"],
            ),
            patch.object(sync_usage, "_git", side_effect=[result(), result()]) as git,
        ):
            self.assertFalse(sync_usage.prepare_daily_branch(date(2026, 8, 1)))
        self.assertEqual(git.call_count, 2)  # clean-status check and fetch only

    def test_lists_every_prior_date_branch_in_order(self) -> None:
        refs = "2026-07-31\nnot-a-date\n2026-07-29\n2026-08-01\n"
        with patch.object(sync_usage, "_git", return_value=result(stdout=refs)):
            self.assertEqual(
                sync_usage._pending_daily_branches(date(2026, 8, 1)),
                ["usage/2026-07-29", "usage/2026-07-31"],
            )

    @patch.object(sync_usage, "_branch_is_ahead", return_value=False)
    @patch.object(sync_usage, "_current_branch", return_value="main")
    def test_tracks_an_existing_today_branch(self, _current, _ahead) -> None:
        refs = {
            "refs/remotes/origin/usage/2026-08-01": True,
            "refs/heads/usage/2026-08-01": False,
        }
        with (
            patch.object(sync_usage, "_ref_exists", side_effect=lambda ref: refs.get(ref, False)),
            patch.object(
                sync_usage,
                "_git",
                side_effect=[result(), result(), result(), result()],
            ) as git,
        ):
            self.assertTrue(sync_usage.prepare_daily_branch(date(2026, 8, 1)))
        commands = [call.args[0] for call in git.call_args_list]
        self.assertIn(
            ["switch", "--track", "-c", "usage/2026-08-01", "origin/usage/2026-08-01"],
            commands,
        )
        self.assertIn(
            ["branch", "--set-upstream-to", "origin/usage/2026-08-01", "usage/2026-08-01"],
            commands,
        )

    @patch.object(sync_usage, "_branch_is_ahead", return_value=False)
    @patch.object(sync_usage, "_current_branch", return_value="main")
    def test_bootstraps_today_only_after_yesterday_is_gone(self, _current, _ahead) -> None:
        with (
            patch.object(sync_usage, "_ref_exists", return_value=False),
            patch.object(sync_usage, "_pending_daily_branches", return_value=[]),
            patch.object(
                sync_usage,
                "_git",
                side_effect=[result(), result(), result(), result()],
            ) as git,
        ):
            self.assertTrue(sync_usage.prepare_daily_branch(date(2026, 8, 1)))
        commands = [call.args[0] for call in git.call_args_list]
        self.assertIn(["switch", "-c", "usage/2026-08-01", "origin/main"], commands)
        self.assertIn(["push", "-u", "origin", "usage/2026-08-01"], commands)


if __name__ == "__main__":
    unittest.main()
