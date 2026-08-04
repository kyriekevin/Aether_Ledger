from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_usage  # noqa: E402


def result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class DailyBranchTests(unittest.TestCase):
    @patch.object(sync_usage.subprocess, "run", return_value=result())
    def test_automated_commit_overrides_host_git_identity(self, run) -> None:
        with patch.dict(
            sync_usage.os.environ,
            {
                "GIT_AUTHOR_NAME": "Private Author",
                "GIT_AUTHOR_EMAIL": "author@company.example",
                "GIT_COMMITTER_NAME": "Private Committer",
                "GIT_COMMITTER_EMAIL": "committer@company.example",
            },
        ):
            sync_usage.git_commit("chore(data): sync personal usage")

        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_AUTHOR_NAME"], "Aether Ledger")
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], "noreply@github.com")
        self.assertEqual(env["GIT_COMMITTER_NAME"], "Aether Ledger")
        self.assertEqual(env["GIT_COMMITTER_EMAIL"], "noreply@github.com")

    def test_only_scheduled_durable_writers_are_rollover_watchdogs(self) -> None:
        self.assertEqual(sync_usage.ROLLOVER_WATCHDOG_NODES, {"work", "personal"})
        self.assertNotIn("devbox", sync_usage.ROLLOVER_WATCHDOG_NODES)

    def test_usage_commit_subject_is_conventional_and_stable(self) -> None:
        self.assertEqual(
            sync_usage.usage_commit_message("data/trail/node-a1b2c3d4e5f6"),
            "chore(data): sync node-a1b2c3d4e5f6 usage",
        )
        self.assertEqual(
            sync_usage.usage_commit_message("data/personal"),
            "chore(data): sync personal usage",
        )

    def test_rollover_uses_conventional_snapshot_subject(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "daily-rollover.yml"
        ).read_text()
        self.assertIn('git commit -m "chore(data): finalize $day snapshot"', workflow)

    def test_rollover_has_an_idempotent_retry_schedule(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "daily-rollover.yml"
        ).read_text()
        self.assertIn('cron: "11,41 0 * * *"', workflow)
        self.assertIn('timezone: "Asia/Shanghai"', workflow)

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
            patch.object(sync_usage, "_sync_local_main"),
            patch.object(sync_usage, "_cleanup_completed_local_branches"),
            patch.object(
                sync_usage,
                "_pending_daily_branches",
                return_value=["usage/2026-07-30"],
            ),
            patch.object(sync_usage, "_git", side_effect=[result(), result()]) as git,
        ):
            self.assertFalse(sync_usage.prepare_daily_branch(date(2026, 8, 1)))
        self.assertEqual(git.call_count, 2)  # clean-status check and fetch only

    @patch.object(sync_usage, "_request_rollover_recovery", return_value=True)
    @patch.object(sync_usage, "_branch_is_ahead", return_value=False)
    @patch.object(sync_usage, "_current_branch", return_value="main")
    def test_external_writer_recovers_a_missed_rollover(
        self,
        _current,
        _ahead,
        request_recovery,
    ) -> None:
        pending = ["usage/2026-07-31"]
        with (
            patch.object(sync_usage, "_ref_exists", return_value=False),
            patch.object(sync_usage, "_sync_local_main"),
            patch.object(sync_usage, "_cleanup_completed_local_branches"),
            patch.object(sync_usage, "_pending_daily_branches", return_value=pending),
            patch.object(sync_usage, "_git", side_effect=[result(), result()]),
        ):
            self.assertFalse(
                sync_usage.prepare_daily_branch(
                    date(2026, 8, 1),
                    recover_missed_rollover=True,
                )
            )
        request_recovery.assert_called_once_with(pending)

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
            patch.object(sync_usage, "_sync_local_main"),
            patch.object(sync_usage, "_cleanup_completed_local_branches"),
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
            patch.object(sync_usage, "_sync_local_main"),
            patch.object(sync_usage, "_cleanup_completed_local_branches"),
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

    @patch.object(sync_usage, "_current_branch", return_value="usage/2026-08-03")
    @patch.object(sync_usage, "_ref_exists", return_value=True)
    def test_fast_forwards_main_ref_while_usage_is_checked_out(self, _exists, _current) -> None:
        with patch.object(
            sync_usage,
            "_git",
            side_effect=[result(), result()],
        ) as git:
            sync_usage._sync_local_main()
        self.assertEqual(
            [call.args[0] for call in git.call_args_list],
            [
                ["merge-base", "--is-ancestor", "main", "origin/main"],
                ["branch", "-f", "main", "origin/main"],
            ],
        )

    @patch.object(sync_usage, "_current_branch", return_value="usage/2026-08-03")
    @patch.object(sync_usage, "_ref_exists", return_value=True)
    def test_keeps_diverged_local_main(self, _exists, _current) -> None:
        with patch.object(sync_usage, "_git", return_value=result(returncode=1)) as git:
            sync_usage._sync_local_main()
        git.assert_called_once_with(["merge-base", "--is-ancestor", "main", "origin/main"])

    @patch.object(
        sync_usage,
        "_completed_local_daily_branches",
        return_value=[(date(2026, 8, 2), "usage/2026-08-02")],
    )
    @patch.object(sync_usage, "_current_branch", return_value="usage/2026-08-03")
    def test_deletes_completed_local_branch_matching_snapshot(self, _current, _completed) -> None:
        with patch.object(
            sync_usage,
            "_git",
            side_effect=[
                result(stdout="snapshot\n"),  # locate the finalized snapshot
                result(),  # data matches that snapshot
                result(stdout="forkpoint\n"),  # locate the fork point
                result(),  # nothing of its own outside data/
                result(),  # delete
            ],
        ) as git:
            sync_usage._cleanup_completed_local_branches(date(2026, 8, 3))
        commands = [call.args[0] for call in git.call_args_list]
        self.assertIn(
            ["diff", "--quiet", "snapshot", "usage/2026-08-02", "--", "data"],
            commands,
        )
        self.assertIn(
            [
                "diff",
                "--quiet",
                "forkpoint",
                "usage/2026-08-02",
                "--",
                ".",
                ":(exclude)data",
            ],
            commands,
        )
        self.assertIn(["branch", "-D", "usage/2026-08-02"], commands)

    @patch.object(
        sync_usage,
        "_completed_local_daily_branches",
        return_value=[(date(2026, 8, 2), "usage/2026-08-02")],
    )
    @patch.object(sync_usage, "_current_branch", return_value="usage/2026-08-03")
    def test_deletes_completed_local_branch_after_main_moved_on(
        self,
        _current,
        _completed,
    ) -> None:
        """A code or docs commit merged into main after the fork must not pin the branch."""
        with patch.object(
            sync_usage,
            "_git",
            side_effect=[
                result(stdout="snapshot\n"),
                result(),  # data still matches the snapshot
                result(stdout="forkpoint\n"),
                result(),  # branch itself never touched anything outside data/
                result(),
            ],
        ) as git:
            sync_usage._cleanup_completed_local_branches(date(2026, 8, 3))
        self.assertIn(
            ["branch", "-D", "usage/2026-08-02"],
            [call.args[0] for call in git.call_args_list],
        )

    @patch.object(
        sync_usage,
        "_completed_local_daily_branches",
        return_value=[(date(2026, 8, 2), "usage/2026-08-02")],
    )
    @patch.object(sync_usage, "_current_branch", return_value="usage/2026-08-03")
    def test_keeps_completed_local_branch_carrying_its_own_code(
        self,
        _current,
        _completed,
    ) -> None:
        with patch.object(
            sync_usage,
            "_git",
            side_effect=[
                result(stdout="snapshot\n"),
                result(),  # data is published
                result(stdout="forkpoint\n"),
                result(returncode=1),  # but the branch changed something outside data/
            ],
        ) as git:
            sync_usage._cleanup_completed_local_branches(date(2026, 8, 3))
        self.assertNotIn(
            ["branch", "-D", "usage/2026-08-02"],
            [call.args[0] for call in git.call_args_list],
        )

    @patch.object(
        sync_usage,
        "_completed_local_daily_branches",
        return_value=[(date(2026, 8, 2), "usage/2026-08-02")],
    )
    @patch.object(sync_usage, "_current_branch", return_value="usage/2026-08-03")
    def test_keeps_completed_local_branch_with_unfinalized_data(self, _current, _completed) -> None:
        with patch.object(
            sync_usage,
            "_git",
            side_effect=[result(stdout="snapshot\n"), result(returncode=1)],
        ) as git:
            sync_usage._cleanup_completed_local_branches(date(2026, 8, 3))
        self.assertNotIn(
            ["branch", "-D", "usage/2026-08-02"],
            [call.args[0] for call in git.call_args_list],
        )


class GitLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(
            sync_usage, "GIT_LOCK_PATH", Path(self._tmp.name) / "nested" / "git.lock"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_acquires_and_releases_the_shared_lock(self) -> None:
        with sync_usage.repo_git_lock(0) as acquired:
            self.assertTrue(acquired)
        # A second run must find it free again.
        with sync_usage.repo_git_lock(0) as acquired:
            self.assertTrue(acquired)

    def test_reports_failure_when_another_process_holds_the_lock(self) -> None:
        sync_usage.GIT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(sync_usage.GIT_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sync_usage.repo_git_lock(wait_seconds=0) as acquired:
            self.assertFalse(acquired)

    @patch.object(sync_usage, "resolve_machine", return_value="data/work")
    def test_run_is_skipped_rather_than_racing_a_lock_holder(self, _machine) -> None:
        with (
            patch.object(sync_usage, "_sync") as sync,
            patch.object(sync_usage.sys, "argv", ["sync_usage.py"]),
        ):
            sync_usage.GIT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(sync_usage.GIT_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
            self.addCleanup(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sync_usage, "GIT_LOCK_WAIT_SECONDS", 0):
                self.assertEqual(sync_usage.main(), 0)
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
