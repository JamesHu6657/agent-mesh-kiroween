"""Regression tests for the Devin full review of main e20a5d2 (P2-1, P2-2, P2-3)."""

import asyncio
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import repos
from mesh.core.clock import now_iso_utc
from mesh.core.db import transaction
from mesh.daemon import recovery
from mesh.daemon.handlers import project as project_handlers
from mesh.daemon.handlers.base import Effect, HandlerResult
from mesh.daemon.server import DaemonServer
from mesh.errors import MeshError
from tests.unit.test_full2_r3_b import _send_req
from tests.unit.test_full2_r3_c import _GitEnvMixin, _create_approved_approval, _ctx, _setup_home_and_repo


def _old_git_path(tmp: str) -> str:
    """PATH with a git shim that acts like git < 2.36: `worktree list ... -z` exits 129,
    everything else runs the real git."""
    real = shutil.which("git")
    shim_dir = os.path.join(tmp, "oldgit")
    os.makedirs(shim_dir)
    shim = os.path.join(shim_dir, "git")
    with open(shim, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/sh\nwt=0; ls=0; z=0\n"
            'for a in "$@"; do case "$a" in worktree) wt=1;; list) ls=1;; -z) z=1;; esac; done\n'
            'if [ "$wt$ls$z" = 111 ]; then echo "error: unknown switch \\`z\'" >&2; exit 129; fi\n'
            f'exec "{real}" "$@"\n'
        )
    os.chmod(shim, stat.S_IRWXU)
    return shim_dir + os.pathsep + os.environ.get("PATH", "")


class TestP2_3WorktreeListFallback(unittest.TestCase):
    """P2-3: git < 2.36 rejects `worktree list --porcelain -z` with rc 129."""


    def test_rc129_falls_back_to_newline_porcelain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-dp2-p23-", dir="/tmp") as tmp:
            live = os.path.join(tmp, "base wt ")
            os.makedirs(live)
            gone = os.path.join(tmp, "gone")
            out = (
                f"worktree {tmp}/repo\nHEAD {'a' * 40}\nbranch refs/heads/other\n\n"
                f"worktree {gone}\nHEAD {'b' * 40}\nbranch refs/heads/main\nprunable gitdir file points to non-existent location\n\n"
                f"worktree {live}\nHEAD {'c' * 40}\nbranch refs/heads/main\n\n"
                f"worktree {tmp}/det\nHEAD {'d' * 40}\ndetached\n\n"
            )
            calls = []

            def fake_git(step, args, cwd, **kw):  # type: ignore[no-untyped-def]
                calls.append(list(args))
                if "-z" in args:
                    return subprocess.CompletedProcess(args, 129, stdout="", stderr="error: unknown switch `z'\n")
                return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

            with mock.patch.object(repos, "_git", side_effect=fake_git), \
                mock.patch.object(repos, "_verify_legacy_worktree_paths"):
                self.assertEqual(repos._worktree_for_branch(tmp, "main"), live)
                self.assertEqual(repos._worktree_for_branch(tmp, "other"), f"{tmp}/repo")
                self.assertIsNone(repos._worktree_for_branch(tmp, "nope"))
            self.assertEqual(calls[0], ["worktree", "list", "--porcelain", "-z"])
            self.assertEqual(calls[1], ["worktree", "list", "--porcelain"])

    def test_ambiguous_newline_paths_fail_closed(self) -> None:
        h = "a" * 40
        outs = (
            f"worktree /w/base\n\nx\nHEAD {h}\nbranch refs/heads/main\n\n",
            f"worktree /w/base\nx\nHEAD {h}\nbranch refs/heads/main\n\n",
            f"worktree /w/base\nprunable\nHEAD {h}\nbranch refs/heads/main\n\n",
            f"worktree /w/a\nbranch refs/heads/main\nHEAD {h}\nbranch refs/heads/other\n\n",
            f"worktree /w/a\nHEAD {h}\nbranch refs/heads/main\nlocked why\nmore\n\n",
        )
        for out in outs:
            def fake_git(step, args, cwd, _out=out, **kw):  # type: ignore[no-untyped-def]
                if "-z" in args:
                    return subprocess.CompletedProcess(args, 129, stdout="", stderr="unknown switch\n")
                return subprocess.CompletedProcess(args, 0, stdout=_out, stderr="")

            with mock.patch.object(repos, "_git", side_effect=fake_git):
                with self.assertRaises(MeshError) as cm:
                    repos._worktree_for_branch("/w", "main")
            self.assertEqual(cm.exception.details.get("reason"), "worktree_list_ambiguous", out)

    def test_unparseable_record_fails_closed_with_neutral_hint(self) -> None:
        """Round 2 P3: a broken worktree (only `HEAD 000…`, no branch/detached line)."""
        out = f"worktree /w/base\nHEAD {'a' * 40}\nbranch refs/heads/main\n\nworktree /w/broken\nHEAD {'0' * 40}\n\n"

        def fake_git(step, args, cwd, **kw):  # type: ignore[no-untyped-def]
            if "-z" in args:
                return subprocess.CompletedProcess(args, 129, stdout="", stderr="unknown switch\n")
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        with mock.patch.object(repos, "_git", side_effect=fake_git):
            with self.assertRaises(MeshError) as cm:
                repos._worktree_for_branch("/w", "other")
        self.assertEqual(cm.exception.details.get("reason"), "worktree_list_ambiguous")
        msg = cm.exception.message
        self.assertNotIn("newline?", msg)
        for part in ("unparseable worktree record", "newline", "broken worktree",
                     "git worktree prune", "git worktree repair"):
            self.assertIn(part, msg)

    def test_legacy_bare_and_locked_records_parse(self) -> None:
        h = "a" * 40
        out = (f"worktree /w/bare.git\nbare\n\n"
               f"worktree /w/b\nHEAD {h}\nbranch refs/heads/main\nlocked\n\n")

        def fake_git(step, args, cwd, **kw):  # type: ignore[no-untyped-def]
            if "-z" in args:
                return subprocess.CompletedProcess(args, 129, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        with mock.patch.object(repos, "_git", side_effect=fake_git), \
                mock.patch.object(repos, "_verify_legacy_worktree_paths"):
            self.assertEqual(repos._worktree_for_branch("/w", "main"), "/w/b")

    def test_other_git_failure_still_raises(self) -> None:
        def fake_git(step, args, cwd, **kw):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: not a git repository\n")

        with mock.patch.object(repos, "_git", side_effect=fake_git):
            with self.assertRaises(MeshError):
                repos._worktree_for_branch("/nonexistent", "main")

    def test_real_git_lists_checked_out_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-dp2-p23r-", dir="/tmp") as tmp:
            repo = os.path.join(tmp, "r")
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True, env=env)
            subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@e",
                            "commit", "-q", "--allow-empty", "-m", "i"], check=True, env=env)
            self.assertEqual(os.path.realpath(repos._worktree_for_branch(repo, "main")), os.path.realpath(repo))


class TestA1LegacyWorktreeForgery(unittest.TestCase):
    """Round 3 A1: without -z a newline in a worktree path can forge a whole record, so every
    listed worktree must exist and share this repository's common dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mh-dp2-a1-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        root = os.path.realpath(self._tmp.name)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")

        def git(cwd, *args):  # type: ignore[no-untyped-def]
            subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

        self.repo, self.other, self.wt = (os.path.join(root, n) for n in ("repo", "other", "wt"))
        for d in (self.repo, self.other):
            os.makedirs(d)
            git(d, "init", "-q", "-b", "main")
            git(d, "commit", "-q", "--allow-empty", "-m", "init")
        git(self.repo, "worktree", "add", "-q", "-b", "feature", self.wt)
        self.missing = os.path.join(root, "missing")
        self.h = "a" * 40
        self._git = git
        self._root = root

    def _legacy(self, out):  # type: ignore[no-untyped-def]
        real = repos._git

        def fake_git(step, args, cwd, **kw):  # type: ignore[no-untyped-def]
            if args[:2] == ["worktree", "list"]:
                if "-z" in args:
                    return subprocess.CompletedProcess(args, 129, stdout="", stderr="unknown switch\n")
                return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
            return real(step, args, cwd, **kw)

        return mock.patch.object(repos, "_git", side_effect=fake_git)

    def _out(self, *records):  # type: ignore[no-untyped-def]
        return "".join(f"worktree {p}\nHEAD {self.h}\nbranch refs/heads/{b}\n\n" for p, b in records)

    def test_forged_record_in_another_repo_is_refused(self) -> None:
        with self._legacy(self._out((self.repo, "other"), (self.other, "main"))):
            with self.assertRaises(MeshError) as cm:
                repos._worktree_for_branch(self.repo, "main")
        self.assertEqual(cm.exception.details.get("reason"), "worktree_list_ambiguous")
        self.assertEqual(cm.exception.details.get("check"), "not a registered worktree of this repository")
        self.assertEqual(cm.exception.code, c.WORKTREE_DIRTY)

    def test_forged_record_to_a_missing_dir_is_refused(self) -> None:
        with self._legacy(self._out((self.repo, "other"), (self.missing, "main"))):
            with self.assertRaises(MeshError) as cm:
                repos._worktree_for_branch(self.repo, "main")
        self.assertEqual(cm.exception.details.get("reason"), "worktree_list_ambiguous")
        self.assertEqual(cm.exception.details.get("check"), "not a directory")

    def test_non_repo_dir_is_refused(self) -> None:
        plain = os.path.join(os.path.dirname(self.repo), "plain")
        os.makedirs(plain)
        with self._legacy(self._out((self.repo, "main"), (plain, "x"))):
            with self.assertRaises(MeshError) as cm:
                repos._worktree_for_branch(self.repo, "main")
        self.assertEqual(cm.exception.details.get("reason"), "worktree_list_ambiguous")

    def test_forged_record_after_two_blank_lines_is_refused(self) -> None:
        """Review r2 P2-1: an extra blank line must not hide a forged record from the checks."""
        h = self.h
        out = (f"worktree {self.repo}\nHEAD {h}\nbranch refs/heads/main\n\n"
               f"worktree {self.repo}\nHEAD {h}\ndetached\n\n\n"
               f"worktree {self.other}\nHEAD {h}\nbranch refs/heads/integ\n\n\n"
               f"worktree {self.wt}\nHEAD {h}\nbranch refs/heads/feature\n\n")
        with self._legacy(out):
            for branch in ("integ", "main", "feature", "nope"):
                with self.assertRaises(MeshError) as cm:
                    repos._worktree_for_branch(self.repo, branch)
                self.assertEqual(cm.exception.details.get("reason"), "worktree_list_ambiguous", branch)

    def test_real_newline_path_with_two_blank_lines_is_refused_under_old_git(self) -> None:
        """Review r2 P2-1 with real git behind a PATH shim that rejects `worktree list -z`."""
        h = self.h
        with mock.patch.dict(os.environ, {"PATH": _old_git_path(self._root)}):
            self.assertEqual(repos._worktree_for_branch(self.repo, "feature"), self.wt)
            for blank, name in (("\n\n", "one"), ("\n\n\n", "two")):
                evil = (f"{self.repo}\nHEAD {h}\ndetached{blank}worktree {self.other}\nHEAD {h}\n"
                        f"branch refs/heads/integ{blank}worktree {self.wt}-{name}")
                self._git(self.repo, "worktree", "add", "-q", "-b", f"evil-{name}", evil)
                with self.assertRaises(MeshError, msg=name) as cm:
                    repos._worktree_for_branch(self.repo, "integ")
                self.assertEqual(cm.exception.details.get("reason"), "worktree_list_ambiguous", name)
                self._git(self.repo, "worktree", "remove", "--force", evil)
            self.assertEqual(repos._worktree_for_branch(self.repo, "feature"), self.wt)

    def test_registration_checks_stay_within_the_git_budget_under_old_git(self) -> None:
        """Review r2 P3-3: the per-worktree git call of the fallback gets the time left, not 30 s."""
        real = repos.run_git
        seen = []

        def recording(args, cwd=None, timeout_s=30.0, **kw):  # type: ignore[no-untyped-def]
            seen.append((list(args), timeout_s))
            return real(args, cwd=cwd, timeout_s=timeout_s, **kw)

        with mock.patch.dict(os.environ, {"PATH": _old_git_path(self._root)}), \
                mock.patch.object(repos, "run_git", side_effect=recording), repos.git_budget(5.0):
            self.assertEqual(repos._worktree_for_branch(self.repo, "feature"), self.wt)
        self.assertEqual([a for a, _ in seen].count(["rev-parse", "--git-common-dir"]), 2, seen)
        self.assertTrue(all(t <= 5.0 for _, t in seen), seen)

    def test_registration_check_does_not_start_once_the_git_budget_is_spent(self) -> None:
        """Review r2 P3-3: like `_git`, no registration check starts after the deadline."""
        out = self._out((self.repo, "main"), (self.wt, "feature"))

        def fake_git(step, args, cwd, **kw):  # type: ignore[no-untyped-def]
            if args[:2] == ["worktree", "list"]:
                return subprocess.CompletedProcess(args, 129 if "-z" in args else 0,
                                                   stdout="" if "-z" in args else out, stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=".git\n", stderr="")

        with mock.patch.object(repos, "_git", side_effect=fake_git), \
                mock.patch.object(repos, "is_registered_worktree", return_value=True) as registered, \
                repos.git_budget(0):
            with self.assertRaises(MeshError) as cm:
                repos._worktree_for_branch(self.repo, "feature")
        self.assertEqual(cm.exception.details.get("reason"), "git_timeout")
        self.assertEqual(cm.exception.details.get("worktree"), self.wt)
        registered.assert_not_called()

    def test_real_worktrees_still_pass(self) -> None:
        with self._legacy(self._out((self.repo, "main"), (self.wt, "feature"))):
            self.assertEqual(repos._worktree_for_branch(self.repo, "feature"), self.wt)
            self.assertEqual(repos._worktree_for_branch(self.repo, "main"), self.repo)
            self.assertIsNone(repos._worktree_for_branch(self.repo, "nope"))


class TestR2P32BaseMovedMessage(unittest.TestCase):
    """Review r2 P3-2: a base_moved error that may follow a write must not say "nothing was changed"."""

    OLD, NEW, OTHER = "a" * 40, "b" * 40, "c" * 40

    def _err(self, actual: str, after_write: bool) -> MeshError:
        cp = subprocess.CompletedProcess([], 0, stdout=actual + "\n", stderr="")
        with mock.patch.object(repos, "_git", return_value=cp):
            with self.assertRaises(MeshError) as cm:
                repos._raise_if_base_moved("/r", "main", self.OLD, self.NEW, after_write=after_write)
        return cm.exception

    def test_after_write_message_says_the_delivery_may_have_been_written(self) -> None:
        for actual, after_write in ((self.NEW, True), (self.OTHER, True), (self.NEW, False)):
            exc = self._err(actual, after_write)
            self.assertTrue(exc.details.get("after_write"), (actual, after_write))
            self.assertNotIn("nothing was changed", exc.message)
            self.assertIn("may have been written", exc.message)
            self.assertIn("`mesh project deliver`", exc.message)

    def test_no_write_message_still_says_nothing_was_changed(self) -> None:
        exc = self._err(self.OTHER, False)
        self.assertNotIn("after_write", exc.details)
        self.assertIn("nothing was changed", exc.message)


class TestP2_3DoctorGitVersion(unittest.TestCase):
    def _checks(self, version_line: str):
        from mesh.cli.commands import doctor_env

        with tempfile.TemporaryDirectory(prefix="mh-dp2-doc-", dir="/tmp") as tmp:
            fake = os.path.join(tmp, "git")
            with open(fake, "w", encoding="utf-8") as f:
                f.write(f"#!/bin/sh\necho '{version_line}'\n")
            os.chmod(fake, stat.S_IRWXU)
            with mock.patch.dict(os.environ, {"PATH": tmp + os.pathsep + os.environ.get("PATH", "")}):
                return doctor_env.environment_checks(tmp, False)

    def test_old_git_warns(self) -> None:
        checks = self._checks("git version 2.34.1")
        gv = [x for x in checks if x["name"] == "git_version"]
        self.assertEqual(len(gv), 1, checks)
        self.assertFalse(gv[0]["ok"])
        self.assertEqual(gv[0]["level"], "warning")
        self.assertIn("2.36", gv[0]["hint"])
        self.assertTrue([x for x in checks if x["name"] == "git"][0]["ok"])

    def test_git_older_than_the_fallback_floor_is_called_unsupported(self) -> None:
        """Review r2 P3-4: below 2.31 the fallback is not enough, and doctor says why."""
        for line in ("git version 2.30.2", "git version 2.25.1", "git version 1.8.3.1"):
            (gv,) = [x for x in self._checks(line) if x["name"] == "git_version"]
            self.assertFalse(gv["ok"], line)
            self.assertEqual(gv["level"], "warning")
            self.assertIn("older than 2.31", gv["detail"], line)
            for part in ("not supported", "git worktree prune", "core.fsmonitor", "2.36"):
                self.assertIn(part, gv["hint"], line)
        for line in ("git version 2.31.0", "git version 2.35.9"):
            (gv,) = [x for x in self._checks(line) if x["name"] == "git_version"]
            self.assertFalse(gv["ok"], line)
            self.assertNotIn("older than", gv["detail"], line)
            self.assertNotIn("not supported", gv["hint"], line)
            self.assertIn("fallback", gv["hint"], line)

    def test_readme_states_the_oldest_git_the_fallback_covers(self) -> None:
        """Review r2 P3-4: README must not promise that any older git works."""
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as f:
            text = " ".join(f.read().split())
        self.assertFalse("older git works through a fallback" in text, "README promises any older git")
        self.assertTrue("git 2.31 to 2.35 works through a fallback" in text, "README lacks the fallback range")
        self.assertTrue("git older than 2.31 is not supported" in text, "README lacks the git floor")

    def test_new_git_ok(self) -> None:
        for line in ("git version 2.36.0", "git version 2.47.3", "git version 3.0.1"):
            gv = [x for x in self._checks(line) if x["name"] == "git_version"]
            self.assertEqual(len(gv), 1)
            self.assertTrue(gv[0]["ok"], line)


class TestP2_2PostCommitReconcileFailure(unittest.TestCase, _GitEnvMixin):
    """P2-2: a committed keyed command whose post-commit reconcile fails answers rc 1
    reconcile_failed (round-3 P1-5 contract), and its idempotent replay returns that same
    error instead of the ok envelope stored before COMMIT."""

    def setUp(self) -> None:
        self._init_git_env()
        self._tmp = tempfile.TemporaryDirectory(prefix="mh-dp2-p22-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.fixture.allow_root(self._tmp.name)
        self.home, self.conn, self.repo, _base, _integ = _setup_home_and_repo(self._tmp.name, self.fixture)
        self.addCleanup(self.conn.close)
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "created_by, created_at, updated_at) "
                "VALUES ('tsk_rf', 'prj_1', 'T', 'S', 'code', 'merging', 'edit', 600, 3, 'usr_1', ?, ?);",
                (now, now),
            )

    def test_reconcile_failure_replay_matches_live_error(self) -> None:
        calls = []

        def fake_release(ctx):  # type: ignore[no-untyped-def]
            calls.append(ctx.request_id)
            ctx.effects.append(Effect(kind="reconcile_merging_task", params={"task_id": "tsk_rf"}))
            return HandlerResult(data={"released": True})

        async def boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("reconcile exploded")

        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        srv.log_error = lambda _m: None
        req = {"v": 1, "cmd": "run.release", "args": {"run_id": "run_x"}, "idempotency_key": "k-p22"}

        async def _run():
            with mock.patch("mesh.daemon.server.handle_run_release", side_effect=fake_release), \
                    mock.patch("mesh.daemon.merge.recover_merging_task_async", side_effect=boom):
                live = await _send_req(srv, dict(req))
                replay = await _send_req(srv, dict(req))
            return live, replay

        live, replay = asyncio.run(_run())
        self.assertEqual(len(calls), 1, "the replay must not run the command again")
        self.assertFalse(live["ok"], live)
        self.assertEqual(live["exit_code"], c.EXIT_INTERNAL)
        self.assertEqual(live["error"]["details"].get("reason"), "reconcile_failed")
        self.assertTrue(replay["idempotent_replay"], replay)
        self.assertEqual(replay["ok"], live["ok"], replay)
        self.assertEqual(replay["exit_code"], live["exit_code"])
        self.assertEqual(replay["error"], live["error"])
        for env in (live, replay):
            self.assertIs(env["error"]["details"].get("committed"), True, env)
            self.assertIn("do not retry with a new key", env["error"]["details"].get("hint", ""), env)


    def _srv(self):
        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        srv.logged = []
        srv.log_error = srv.logged.append
        return srv

    def _run(self, srv, reqs, reconcile):
        def fake_release(ctx):  # type: ignore[no-untyped-def]
            ctx.effects.append(Effect(kind="reconcile_merging_task", params={"task_id": "tsk_rf"}))
            return HandlerResult(data={"released": True})

        async def go():
            with mock.patch("mesh.daemon.server.handle_run_release", side_effect=fake_release), \
                    mock.patch("mesh.daemon.merge.recover_merging_task_async", side_effect=reconcile):
                return [await _send_req(srv, dict(r)) for r in reqs]

        return asyncio.run(go())

    def test_reconcile_mesh_error_passes_through_to_live_and_replay(self) -> None:
        async def conflict(*_a, **_k):  # type: ignore[no-untyped-def]
            raise MeshError(code=c.VERSION_MISMATCH, exit_code=c.EXIT_CONFLICT, message="moved",
                            details={"reason": "base_moved"})

        req = {"v": 1, "cmd": "run.release", "args": {"run_id": "run_x"}, "idempotency_key": "k-p22-me"}
        live, replay = self._run(self._srv(), [req, req], conflict)
        self.assertEqual((live["exit_code"], live["error"]["code"]), (c.EXIT_CONFLICT, c.VERSION_MISMATCH))
        self.assertEqual(live["error"]["details"], {"reason": "base_moved"})
        self.assertTrue(replay["idempotent_replay"], replay)
        self.assertEqual((replay["exit_code"], replay["error"]), (live["exit_code"], live["error"]))

    def test_rewrite_failure_still_returns_the_live_error(self) -> None:
        import mesh.daemon.server as server_mod

        real = server_mod._idempotency_record

        def failing(cmd, envelope):  # type: ignore[no-untyped-def]
            if not envelope.get("ok", True):
                raise RuntimeError("disk full")
            return real(cmd, envelope)

        async def boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("reconcile exploded")

        srv = self._srv()
        req = {"v": 1, "cmd": "run.release", "args": {"run_id": "run_x"}, "idempotency_key": "k-p22-rw"}
        with mock.patch("mesh.daemon.server._idempotency_record", side_effect=failing):
            (live,) = self._run(srv, [req], boom)
        self.assertEqual(live["exit_code"], c.EXIT_INTERNAL)
        self.assertEqual(live["error"]["details"].get("reason"), "reconcile_failed")
        self.assertIs(live["error"]["details"].get("committed"), True)
        self.assertTrue(any("not updated after a reconcile failure" in m for m in srv.logged), srv.logged)

    def test_no_key_does_not_rewrite(self) -> None:
        async def boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("reconcile exploded")

        srv = self._srv()
        req = {"v": 1, "cmd": "run.release", "args": {"run_id": "run_x"}}
        with mock.patch.object(srv, "_rewrite_idempotency_response") as rw:
            (live,) = self._run(srv, [req], boom)
        rw.assert_not_called()
        self.assertEqual(live["error"]["details"].get("reason"), "reconcile_failed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM idempotency;").fetchone()[0], 0)


class TestP2_1DeliverPostWriteBaseMove(unittest.TestCase, _GitEnvMixin):
    """P2-1: the ff merge wrote the base, then someone advanced it before the post-ff read.

    The mismatch must keep the deliver intent so settle/recovery records the delivery and
    consumes the approval; a pre-write mismatch still leaves no intent (test_full2_r3_c)."""

    def setUp(self) -> None:
        self._init_git_env()
        self._tmp = tempfile.TemporaryDirectory(prefix="mh-dp2-p21-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.fixture.allow_root(self._tmp.name)
        self.home, self.conn, self.repo, self.base_sha, self.integ = _setup_home_and_repo(
            self._tmp.name, self.fixture
        )
        self.addCleanup(self.conn.close)

    def _commit(self, cwd: str, fname: str, content: str) -> str:
        with open(os.path.join(cwd, fname), "w", encoding="utf-8") as f:
            f.write(content)
        self.fixture.git(cwd, "add", fname)
        self.fixture.git(cwd, "commit", "-q", "-m", f"add {fname}", identity=True)
        return self.fixture.head(cwd)

    def _approval(self) -> str:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('approval.policy.deliver_to_base', '\"direct_or_relayed\"', 'now', 'usr_1');"
            )
        return _create_approved_approval(
            self.conn, self.home, category="deliver_to_base", op="project.deliver",
            requested_by="agt_lead", project_id="prj_1", target_id="prj_1",
            params={"project_id": "prj_1"},
        )

    def _post_write_mismatch(self, apr_id: str) -> str:
        """Deliver whose ff write lands, then the base moves on; returns the delivered sha."""
        new_sha = self._commit(self.integ["integration_path"], "feat3.txt", "f\n")
        real = repos.run_git
        state = {}

        def wrapped(args, cwd=None, **kw):  # type: ignore[no-untyped-def]
            cp = real(args, cwd=cwd, **kw)
            if args[:2] == ["merge", "--ff-only"] and cp.returncode == 0 and "after" not in state:
                state["after"] = self._commit(self.repo, "external3.txt", "e\n")
            return cp

        ctx = _ctx(self.conn, self.home, "project.deliver", {"project_id": "prj_1"},
                   actor_kind="agent", via="token", approval=apr_id)
        with mock.patch("mesh.core.repos.run_git", side_effect=wrapped):
            with self.assertRaises(MeshError):
                with transaction(self.conn):
                    project_handlers.handle_project_deliver(ctx)
        self.assertTrue(os.path.lexists(repos.deliver_intent_path(self.home, "prj_1")))
        return new_sha

    def _delivered_events(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'project.delivered';").fetchone()[0]

    def _approval_status(self, apr_id: str) -> str:
        return self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_id,)).fetchone()["status"]

    def test_keyed_retry_through_handle_client_replays_recovered(self) -> None:
        new_sha = self._commit(self.integ["integration_path"], "feat4.txt", "f\n")
        real = repos.run_git
        state = {}

        def wrapped(args, cwd=None, **kw):  # type: ignore[no-untyped-def]
            cp = real(args, cwd=cwd, **kw)
            if args[:2] == ["merge", "--ff-only"] and cp.returncode == 0 and "after" not in state:
                state["after"] = self._commit(self.repo, "external4.txt", "e\n")
            return cp

        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        srv.log_error = lambda _m: None
        req = {"v": 1, "cmd": "project.deliver", "args": {"project_id": "prj_1"}, "idempotency_key": "k-p21"}

        async def go():
            with mock.patch("mesh.core.repos.run_git", side_effect=wrapped):
                live = await _send_req(srv, dict(req))
            return live, await _send_req(srv, dict(req))

        live, retry = asyncio.run(go())
        self.assertEqual((live["exit_code"], live["error"]["code"]), (c.EXIT_CONFLICT, c.VERSION_MISMATCH))
        self.assertTrue(retry["ok"], retry)
        self.assertTrue(retry["idempotent_replay"], retry)
        self.assertEqual(retry["data"]["method"], "recovered")
        self.assertEqual(retry["data"]["new"], new_sha)
        self.assertEqual(self._delivered_events(), 1)
        self.assertFalse(os.path.lexists(repos.deliver_intent_path(self.home, "prj_1")))

    def test_settle_with_base_back_at_old_drops_intent_without_delivery(self) -> None:
        apr_id = self._approval()
        self._post_write_mismatch(apr_id)
        self.fixture.git(self.repo, "reset", "-q", "--hard", self.base_sha)
        recovery.settle_deliver_intent(self.conn, self.home, "prj_1")
        self.assertFalse(os.path.lexists(repos.deliver_intent_path(self.home, "prj_1")))
        self.assertEqual(self._delivered_events(), 0)
        self.assertEqual(self._approval_status(apr_id), "approved")

    def test_settle_with_base_rewritten_without_new_records_nothing(self) -> None:
        apr_id = self._approval()
        new_sha = self._post_write_mismatch(apr_id)
        self.fixture.git(self.repo, "reset", "-q", "--hard", self.base_sha)
        other = self._commit(self.repo, "rewrite.txt", "r\n")
        anc = subprocess.run(["git", "merge-base", "--is-ancestor", new_sha, other], cwd=self.repo,
                             capture_output=True)
        self.assertEqual(anc.returncode, 1)
        recovery.settle_deliver_intent(self.conn, self.home, "prj_1")
        self.assertEqual(self._delivered_events(), 0)
        self.assertEqual(self._approval_status(apr_id), "approved")

    def test_ambiguous_worktree_list_leaves_no_intent_and_keeps_approval(self) -> None:
        """P2-3 at the handler: worktree_list_ambiguous fails before the intent is written."""
        apr_id = self._approval()
        self._commit(self.integ["integration_path"], "feat5.txt", "f\n")
        real = repos._git

        def fake_git(step, args, cwd, **kw):  # type: ignore[no-untyped-def]
            if args[:2] == ["worktree", "list"]:
                if "-z" in args:
                    return subprocess.CompletedProcess(args, 129, stdout="", stderr="unknown switch\n")
                return subprocess.CompletedProcess(args, 0, stdout="worktree /w\nHEAD " + "0" * 40 + "\n\n",
                                                   stderr="")
            return real(step, args, cwd, **kw)

        ctx = _ctx(self.conn, self.home, "project.deliver", {"project_id": "prj_1"},
                   actor_kind="agent", via="token", approval=apr_id)
        with mock.patch.object(repos, "_git", side_effect=fake_git):
            with self.assertRaises(MeshError) as cm:
                with transaction(self.conn):
                    project_handlers.handle_project_deliver(ctx)
        self.assertEqual(cm.exception.details.get("reason"), "worktree_list_ambiguous")
        self.assertFalse(os.path.lexists(repos.deliver_intent_path(self.home, "prj_1")))
        self.assertEqual(self._approval_status(apr_id), "approved")
        self.assertEqual(self._delivered_events(), 0)

    def test_post_ff_base_move_keeps_intent_and_settle_records_delivery(self) -> None:
        new_sha = self._commit(self.integ["integration_path"], "feat.txt", "f\n")
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('approval.policy.deliver_to_base', '\"direct_or_relayed\"', 'now', 'usr_1');"
            )
        apr_id = _create_approved_approval(
            self.conn, self.home, category="deliver_to_base", op="project.deliver",
            requested_by="agt_lead", project_id="prj_1", target_id="prj_1",
            params={"project_id": "prj_1"},
        )
        real = repos.run_git
        state = {}

        def wrapped(args, cwd=None, **kw):  # type: ignore[no-untyped-def]
            cp = real(args, cwd=cwd, **kw)
            if args[:2] == ["merge", "--ff-only"] and cp.returncode == 0 and "after" not in state:
                state["after"] = self._commit(self.repo, "external.txt", "external\n")
            return cp

        intent_path = repos.deliver_intent_path(self.home, "prj_1")
        ctx = _ctx(self.conn, self.home, "project.deliver", {"project_id": "prj_1"},
                   actor_kind="agent", via="token", approval=apr_id)
        with mock.patch("mesh.core.repos.run_git", side_effect=wrapped):
            with self.assertRaises(MeshError) as cm:
                with transaction(self.conn):
                    project_handlers.handle_project_deliver(ctx)
        self.assertEqual(cm.exception.code, c.VERSION_MISMATCH)
        self.assertEqual(cm.exception.details.get("reason"), "base_moved")
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), state["after"])
        self.assertTrue(os.path.lexists(intent_path), "post-write mismatch dropped the deliver intent")
        self.assertTrue(cm.exception.details.get("after_write"))

        recovery.settle_deliver_intent(self.conn, self.home, "prj_1")
        self.assertFalse(os.path.lexists(intent_path))
        self.assertEqual(
            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_id,)).fetchone()["status"],
            "consumed",
        )
        ev = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'project.delivered';").fetchone()[0]
        self.assertEqual(ev, 1)
        self.assertNotEqual(new_sha, state["after"])


    def test_merge_nonzero_after_ref_moved_keeps_intent_and_settle_records_delivery(self) -> None:
        """Round 2 P3: merge --ff-only updated the ref, then exited non-zero (e.g. a signal)."""
        new_sha = self._commit(self.integ["integration_path"], "feat2.txt", "f\n")
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('approval.policy.deliver_to_base', '\"direct_or_relayed\"', 'now', 'usr_1');"
            )
        apr_id = _create_approved_approval(
            self.conn, self.home, category="deliver_to_base", op="project.deliver",
            requested_by="agt_lead", project_id="prj_1", target_id="prj_1",
            params={"project_id": "prj_1"},
        )
        real = repos.run_git

        def wrapped(args, cwd=None, **kw):  # type: ignore[no-untyped-def]
            cp = real(args, cwd=cwd, **kw)
            if args[:2] == ["merge", "--ff-only"] and cp.returncode == 0:
                return subprocess.CompletedProcess(cp.args, 143, stdout=cp.stdout, stderr="killed\n")
            return cp

        intent_path = repos.deliver_intent_path(self.home, "prj_1")
        ctx = _ctx(self.conn, self.home, "project.deliver", {"project_id": "prj_1"},
                   actor_kind="agent", via="token", approval=apr_id)
        with mock.patch("mesh.core.repos.run_git", side_effect=wrapped):
            with self.assertRaises(MeshError) as cm:
                with transaction(self.conn):
                    project_handlers.handle_project_deliver(ctx)
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), new_sha)
        self.assertEqual((cm.exception.code, cm.exception.details.get("reason")), (c.VERSION_MISMATCH, "base_moved"))
        self.assertTrue(os.path.lexists(intent_path), "merge rc!=0 after the write dropped the deliver intent")
        self.assertTrue(cm.exception.details.get("after_write"))
        self.assertNotIn("nothing was changed", cm.exception.message)
        recovery.settle_deliver_intent(self.conn, self.home, "prj_1")
        self.assertFalse(os.path.lexists(intent_path))
        self.assertEqual(
            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_id,)).fetchone()["status"],
            "consumed",
        )
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'project.delivered';").fetchone()[0], 1)

    def _deliver_update_ref(self, apr_id: str, after_update_ref):  # type: ignore[no-untyped-def]
        """Deliver through the update-ref CAS (base not checked out); returns the MeshError."""
        self.fixture.git(self.repo, "checkout", "-q", "--detach", self.base_sha)
        real = repos.run_git

        def wrapped(args, cwd=None, **kw):  # type: ignore[no-untyped-def]
            if args[:1] == ["update-ref"]:
                return after_update_ref(lambda: real(args, cwd=cwd, **kw))
            return real(args, cwd=cwd, **kw)

        ctx = _ctx(self.conn, self.home, "project.deliver", {"project_id": "prj_1"},
                   actor_kind="agent", via="token", approval=apr_id)
        with mock.patch("mesh.core.repos.run_git", side_effect=wrapped):
            with self.assertRaises(MeshError) as cm:
                with transaction(self.conn):
                    project_handlers.handle_project_deliver(ctx)
        return cm.exception

    def test_update_ref_nonzero_after_ref_moved_keeps_intent_and_settle_records_delivery(self) -> None:
        """Review r2 P3-1: update-ref committed the ref, then exited non-zero (e.g. a signal)."""
        apr_id = self._approval()
        new_sha = self._commit(self.integ["integration_path"], "feat6.txt", "f\n")

        def killed(run):  # type: ignore[no-untyped-def]
            cp = run()
            self.assertEqual(cp.returncode, 0, cp.stderr)
            return subprocess.CompletedProcess(cp.args, -15, stdout="", stderr="")

        exc = self._deliver_update_ref(apr_id, killed)
        intent_path = repos.deliver_intent_path(self.home, "prj_1")
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), new_sha)
        self.assertEqual((exc.code, exc.details.get("reason")), (c.VERSION_MISMATCH, "base_moved"))
        self.assertTrue(exc.details.get("after_write"))
        self.assertTrue(os.path.lexists(intent_path), "update-ref rc!=0 after the write dropped the deliver intent")
        self.assertNotIn("nothing was changed", exc.message)
        recovery.settle_deliver_intent(self.conn, self.home, "prj_1")
        self.assertFalse(os.path.lexists(intent_path))
        self.assertEqual(self._approval_status(apr_id), "consumed")
        self.assertEqual(self._delivered_events(), 1)

    def test_update_ref_cas_lost_to_another_commit_still_leaves_no_intent(self) -> None:
        """Review r2 P3-1 guard: a CAS lost to someone else's commit wrote nothing."""
        apr_id = self._approval()
        self._commit(self.integ["integration_path"], "feat7.txt", "f\n")
        state = {}

        def raced(run):  # type: ignore[no-untyped-def]
            self.fixture.git(self.repo, "checkout", "-q", "main")
            state["other"] = self._commit(self.repo, "external7.txt", "e\n")
            self.fixture.git(self.repo, "checkout", "-q", "--detach")
            cp = run()
            self.assertNotEqual(cp.returncode, 0)
            return cp

        exc = self._deliver_update_ref(apr_id, raced)
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), state["other"])
        self.assertEqual((exc.code, exc.details.get("reason")), (c.VERSION_MISMATCH, "base_moved"))
        self.assertFalse(exc.details.get("after_write"))
        self.assertFalse(os.path.lexists(repos.deliver_intent_path(self.home, "prj_1")))
        self.assertEqual(self._approval_status(apr_id), "approved")
        self.assertEqual(self._delivered_events(), 0)


if __name__ == "__main__":
    unittest.main()
