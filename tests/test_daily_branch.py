from __future__ import annotations

import fcntl
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
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
    def test_keeps_completed_local_branch_when_the_comparison_itself_fails(
        self,
        _current,
        _completed,
    ) -> None:
        """Exit codes above 1 are Git failures, not differences, and say so."""
        with patch.object(
            sync_usage,
            "_git",
            side_effect=[
                result(stdout="snapshot\n"),
                result(returncode=128, stderr="fatal: bad object"),
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
    def test_keeps_completed_local_branch_when_the_fork_comparison_fails(
        self,
        _current,
        _completed,
    ) -> None:
        """The second comparison must fail closed too, not only the first."""
        with patch.object(
            sync_usage,
            "_git",
            side_effect=[
                result(stdout="snapshot\n"),
                result(),  # data matches
                result(stdout="forkpoint\n"),
                result(returncode=128, stderr="fatal: bad revision"),
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


class RealRepositoryCleanupTests(unittest.TestCase):
    """Drive the deletion rule against a real repository instead of mocked git.

    The mocked tests above pin the command shape; these pin the actual Git
    semantics the rule depends on — merge-base against a squash-merged branch,
    and pathspec-scoped diffs — which no side_effect sequence can prove.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        # Ignore the developer's own Git configuration: a global template dir,
        # hooks path or alias must not decide whether these tests pass. The
        # patch covers sync_usage._git too, which inherits os.environ.
        env = patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        )
        env.start()
        self.addCleanup(env.stop)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        patcher = patch.object(sync_usage, "DATA_REPO_DIR", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args: str) -> str:
        r = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"git {' '.join(args)} failed: {r.stderr}")
        return r.stdout.strip()

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, subject: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)

    def branches(self) -> list[str]:
        return self.git("branch", "--format=%(refname:short)").splitlines()

    def build_day_branch_that_main_outran(self) -> None:
        """A finished day branch, plus a code commit main took on after the fork."""
        self.write("data/work/claude.json", '{"2026-08-01": 1}\n')
        self.write("scripts/sync_usage.py", "# v1\n")
        self.commit("chore(data): finalize 2026-08-01 snapshot")

        self.git("branch", "usage/2026-08-02")
        self.git("switch", "-q", "usage/2026-08-02")
        self.write("data/work/claude.json", '{"2026-08-02": 2}\n')
        self.commit("chore(data): sync work usage")

        self.git("switch", "-q", "main")
        self.write("scripts/sync_usage.py", "# v2, merged while the day was open\n")
        self.commit("feat(automation): land a PR mid-day (#3)")
        self.write("data/work/claude.json", '{"2026-08-02": 2}\n')  # the squash
        self.commit("chore(data): finalize 2026-08-02 snapshot")
        self.git("update-ref", "refs/remotes/origin/main", "main")

    def test_deletes_completed_branch_after_main_moved_on(self) -> None:
        self.build_day_branch_that_main_outran()
        snapshot = self.git("rev-parse", "main")
        whole_tree = subprocess.run(
            ["git", "diff", "--quiet", snapshot, "usage/2026-08-02"],
            cwd=self.repo,
        )
        # Guards the scenario itself: whole trees differ, so the rule this
        # replaced would have kept the branch here forever.
        self.assertEqual(whole_tree.returncode, 1)

        sync_usage._cleanup_completed_local_branches(date(2026, 8, 3))
        self.assertNotIn("usage/2026-08-02", self.branches())

    def test_keeps_completed_branch_holding_its_own_code(self) -> None:
        self.build_day_branch_that_main_outran()
        self.git("switch", "-q", "usage/2026-08-02")
        self.write("scripts/hotfix.py", "# never reached main\n")
        self.commit("fix: something only this branch has")
        self.git("switch", "-q", "main")

        sync_usage._cleanup_completed_local_branches(date(2026, 8, 3))
        self.assertIn("usage/2026-08-02", self.branches())

    def test_keeps_completed_branch_holding_unpublished_data(self) -> None:
        self.build_day_branch_that_main_outran()
        self.git("switch", "-q", "usage/2026-08-02")
        self.write("data/work/claude.json", '{"2026-08-02": 3}\n')
        self.commit("chore(data): sync work usage")
        self.git("switch", "-q", "main")

        sync_usage._cleanup_completed_local_branches(date(2026, 8, 3))
        self.assertIn("usage/2026-08-02", self.branches())


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


class GitCatchUpTests(unittest.TestCase):
    """Catching up must not depend on FETCH_HEAD.

    FETCH_HEAD is one file shared by every Git process in the checkout and
    rewritten in full by each fetch, so `git pull --rebase` can read a view with
    several merge heads and die. The advisory lock serializes the scheduled
    writers but cannot cover a terminal or a second worktree.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        env = patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        )
        env.start()
        self.addCleanup(env.stop)

        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)],
                       check=True)
        self.repo = root / "repo"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.repo)],
                       check=True)
        for k, v in (("user.name", "Test"), ("user.email", "test@example.invalid"),
                     ("commit.gpgsign", "false")):
            self.git("config", k, v)
        self.write("seed", "0\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.git("push", "-q", "-u", "origin", "main")

        patcher = patch.object(sync_usage, "DATA_REPO_DIR", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)
        if check:
            self.assertEqual(r.returncode, 0, f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def write(self, rel: str, text: str) -> None:
        (self.repo / rel).write_text(text)

    def push_from_elsewhere(self, text: str) -> None:
        """Another machine advances the branch behind our back."""
        other = self.repo.parent / "other"
        if not other.exists():
            subprocess.run(["git", "clone", "-q", str(self.remote), str(other)],
                           check=True)
            for k, v in (("user.name", "Other"), ("user.email", "o@example.invalid"),
                         ("commit.gpgsign", "false")):
                subprocess.run(["git", "config", k, v], cwd=other, check=True)
        (other / "seed").write_text(text)
        for args in (["add", "-A"], ["commit", "-q", "-m", "elsewhere"], ["push", "-q"]):
            subprocess.run(["git", *args], cwd=other, check=True)

    def test_it_catches_up_with_the_upstream(self) -> None:
        self.push_from_elsewhere("1\n")
        self.assertTrue(sync_usage.git_catch_up())
        self.assertEqual((self.repo / "seed").read_text(), "1\n")

    def test_it_replays_local_commits_on_top(self) -> None:
        self.write("mine", "local\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "mine")
        self.push_from_elsewhere("1\n")
        self.assertTrue(sync_usage.git_catch_up())
        self.assertEqual((self.repo / "seed").read_text(), "1\n")
        self.assertEqual((self.repo / "mine").read_text(), "local\n")

    def test_it_rebases_onto_the_tracking_ref_and_never_pulls(self) -> None:
        """The regression guard: going back to `git pull` reopens the race."""
        calls: list[list[str]] = []
        real = sync_usage._git

        def record(args, **kw):
            calls.append(args)
            return real(args, **kw)

        with patch.object(sync_usage, "_git", record):
            sync_usage.git_catch_up()
        verbs = [c[0] for c in calls]
        self.assertNotIn("pull", verbs)
        rebase = next(c for c in calls if c[0] == "rebase")
        self.assertIn("origin/main", rebase)

    def test_no_upstream_is_reported_not_crashed(self) -> None:
        self.git("checkout", "-q", "-b", "orphan")
        self.assertFalse(sync_usage.git_catch_up())

    def test_uncommitted_work_survives_the_catch_up(self) -> None:
        """--autostash must give the dirt back; the writer's tree is live data."""
        self.push_from_elsewhere("1\n")
        self.write("seed", "0\nuncommitted\n")
        self.assertTrue(sync_usage.git_catch_up())
        self.assertIn("uncommitted", (self.repo / "seed").read_text())

    def test_a_failed_rebase_parks_uncommitted_work_recoverably(self) -> None:
        """Not lost, just not in the tree — and the failure says where it went.

        The catch-up used to run `rebase --abort` here, which handed the
        autostash back. That was dropped: --abort cannot tell our rebase from a
        human's, so it could unwind someone else's conflict resolution. A failed
        rebase now stays parked until a human finishes or aborts it.

        Where the edit goes is counter-intuitive and worth pinning: an autostash
        held by a live rebase lives in the rebase state, NOT in `refs/stash`, so
        `git stash list` prints nothing and pointing a human there would send
        them to an empty list while their work sits somewhere else.
        """
        self.write("mine", "local\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "mine")
        self.write("seed", "conflicting\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "conflict")
        self.push_from_elsewhere("theirs\n")
        self.write("mine", "still editing\n")

        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(sync_usage.git_catch_up())

        self.assertEqual((self.repo / "mine").read_text(), "local\n",
                         "autostash took the edit out of the tree")
        self.assertFalse(self.git("stash", "list"),
                         "a live rebase's autostash is not a stash-list entry")
        self.assertIn("git rebase --continue", err.getvalue(),
                      "the log has to name the recovery that actually works")

        # And that recovery really does hand the edit back.
        self.git("rebase", "--abort")
        self.assertEqual((self.repo / "mine").read_text(), "still editing\n")

    def test_it_fetches_once(self) -> None:
        fetches = self.count_verbs("fetch")
        self.assertEqual(fetches, 1)

    def test_a_caller_that_just_fetched_can_skip_the_round_trip(self) -> None:
        """Each fetch is a network round trip taken while holding the shared lock."""
        self.assertEqual(self.count_verbs("fetch", fetch=False), 0)

    def count_verbs(self, verb: str, **kw) -> int:
        seen: list[str] = []
        real = sync_usage._git

        def record(args, **inner):
            seen.append(args[0])
            return real(args, **inner)

        with patch.object(sync_usage, "_git", record):
            sync_usage.git_catch_up(**kw)
        return seen.count(verb)

    def test_a_failure_before_the_rebase_prescribes_no_rebase_recovery(self) -> None:
        """Nothing started, so nothing is parked; sending a human to `git rebase
        --abort` here points them at a checkout that is merely behind.
        """
        self.git("checkout", "-q", "-b", "orphan")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(sync_usage.git_catch_up())
        printed = err.getvalue()
        self.assertIn("no upstream", printed)
        self.assertNotIn("rebase --abort", printed)
        self.assertNotIn("rebase --continue", printed)


if __name__ == "__main__":
    unittest.main()
