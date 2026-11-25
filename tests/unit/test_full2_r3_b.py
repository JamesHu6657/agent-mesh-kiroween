"""Regression tests for full2-r3.md Group B findings (P1-2, P1-3, P1-5, P2-2, P2-3)."""

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import mesh.constants as c
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.repos import is_registered_worktree, setup_integration
from mesh.core.workspaces import get_workspaces_root, ws_delete_reservation_key
from mesh.daemon import merge
from mesh.daemon.handlers.base import Effect, HandlerContext
from mesh.daemon.server import DaemonServer
from mesh.errors import MeshError
from tests.gitfixture import GitFixture
from tests.helpers import fake_peer_creds

# merge.run_test_step scans real /proc for leftovers.
MESH_PROC_LOCK = "shared"


def _setup_home_and_db(tmp: str) -> tuple[str, sqlite3.Connection]:
    home = os.path.join(tmp, "home")
    for sub in ("logs", "run", "runs", "merges", "workspaces"):
        os.makedirs(os.path.join(home, sub), mode=0o700, exist_ok=True)
    conn = init_db(os.path.join(home, "mesh.db"))
    now = now_iso_utc()
    with transaction(conn):
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) VALUES ('agt_lead', 'none', 'inbox', 1, '[]', 2, 'edit', ?, ?);",
            (now, now),
        )
    return home, conn


async def _send_req(srv: DaemonServer, payload: dict) -> dict:
    sock_a, sock_b = socket.socketpair()
    reader_s, writer_s = await asyncio.open_connection(sock=sock_b)
    reader_c, writer_c = await asyncio.open_connection(sock=sock_a)
    try:
        with mock.patch(
            "mesh.daemon.server.get_peer_credentials",
            return_value=fake_peer_creds(),
        ), mock.patch("mesh.daemon.server.classify_peer_run_session", return_value=None), mock.patch.object(
            srv, "refresh_run_caches"
        ), mock.patch.object(
            srv,
            "authenticate_and_authorize",
            new=mock.AsyncMock(return_value=({"id": "usr_1", "kind": "user", "name": "user"}, "user", "direct", None)),
        ):
            writer_c.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer_c.drain()
            await srv.handle_client(reader_s, writer_s)
            line = await reader_c.readline()
    finally:
        writer_c.close()
        writer_s.close()
    return json.loads(line.decode("utf-8"))


class _RealWorktreeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[key]
        os.environ.update(self.fixture.git_env_vars())

        self.tmp = self.fixture.base
        self.repo = self.fixture.make_k0("R3B")
        self.base = self.fixture.head(self.repo, "main")
        self.home, self.conn = _setup_home_and_db(self.tmp)
        self.addCleanup(self.conn.close)
        self.ws_root = get_workspaces_root(self.conn, self.home)
        info = setup_integration(self.repo, "prj_1", self.base, self.ws_root)
        self.integ = info["integration_path"]
        self.integ_branch = info["integration_branch"]
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
                "base_commit, created_by, created_via, version, created_at, updated_at) "
                "VALUES ('prj_1', 'P1', 'goal', 'active', ?, 'main', ?, ?, 'usr_1', 'direct', 1, ?, ?);",
                (self.repo, self.integ_branch, self.base, now, now),
            )
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
                (now,),
            )

    def _make_task_branch(self, task_id: str, files: dict[str, str], *, base: str | None = None) -> tuple[str, str]:
        branch = f"mesh/prj_1/{task_id}"
        wt = os.path.join(self.ws_root, "prj_1", "tasks", task_id)
        os.makedirs(os.path.dirname(wt), mode=0o700, exist_ok=True)
        self.fixture.git(self.repo, "worktree", "add", "-b", branch, wt, base or self.base)
        for rel, content in files.items():
            full = os.path.join(wt, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            self.fixture.git(wt, "add", rel)
        self.fixture.git(wt, "commit", "-q", "-m", f"commit {task_id}")
        tip = self.fixture.head(wt)
        return wt, tip

    def _plan(
        self,
        run_id: str,
        task_id: str,
        wt: str,
        tip: str,
        *,
        script: str | None = None,
        branch: str | None = None,
    ) -> dict:
        run_dir = os.path.join(self.home, "merges", run_id)
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        plan = {
            "run_id": run_id,
            "merge_id": f"mrg_{run_id}",
            "task_id": task_id,
            "project_id": "prj_1",
            "title": task_id,
            "repo": self.repo,
            "ws_root": self.ws_root,
            "integration": self.integ,
            "integration_branch": self.integ_branch,
            "worktree": wt,
            "branch": branch or f"mesh/prj_1/{task_id}",
            "expected_head": tip,
            "pre_head": self.fixture.head(self.integ),
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir,
        }
        if script is not None:
            plan["test_argv"] = [sys.executable, "-c", script]
        return plan


class TestP1_2MergeHeadIntegrity(_RealWorktreeFixture):
    """P1-2: tampering .git/MERGE_HEAD during test_command must be rejected and aborted."""

    def test_appended_sha_in_merge_head_rejected_and_aborted(self) -> None:
        wt_ok, tip_ok = self._make_task_branch("tsk_ok", {"ok.txt": "ok\n"})
        _wt_evil, tip_evil = self._make_task_branch("tsk_evil", {"evil.txt": "evil\n"})
        pre = self.fixture.head(self.integ)
        script = (
            "import os, subprocess\n"
            "mh = subprocess.check_output(['git', 'rev-parse', '--git-path', 'MERGE_HEAD'], text=True).strip()\n"
            "with open(mh, 'a', encoding='utf-8') as f:\n"
            f"    f.write({tip_evil!r} + '\\n')\n"
        )
        out = merge.run_merge_pipeline(self._plan("run_p12a", "tsk_ok", wt_ok, tip_ok, script=script))
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "integration_dirty"), out)
        self.assertIn(".git/MERGE_HEAD", out["details"].get("dirty_files", []), out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertEqual(self.fixture.head(self.integ), pre)
        self.assertFalse(self.fixture.has_merge_head(self.integ))

    def test_replaced_sha_in_merge_head_rejected_and_aborted(self) -> None:
        wt_ok, tip_ok = self._make_task_branch("tsk_ok2", {"ok2.txt": "ok2\n"})
        _wt_evil, tip_evil = self._make_task_branch("tsk_evil2", {"evil2.txt": "evil2\n"})
        pre = self.fixture.head(self.integ)
        script = (
            "import subprocess\n"
            "mh = subprocess.check_output(['git', 'rev-parse', '--git-path', 'MERGE_HEAD'], text=True).strip()\n"
            "with open(mh, 'w', encoding='utf-8') as f:\n"
            f"    f.write({tip_evil!r} + '\\n')\n"
        )
        out = merge.run_merge_pipeline(self._plan("run_p12b", "tsk_ok2", wt_ok, tip_ok, script=script))
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "integration_dirty"), out)
        self.assertIn(".git/MERGE_HEAD", out["details"].get("dirty_files", []), out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertEqual(self.fixture.head(self.integ), pre)
        self.assertFalse(self.fixture.has_merge_head(self.integ))

    def test_noop_corrupt_symlink_merge_head_rejected_and_cleaned(self) -> None:
        wt_n, tip_n = self._make_task_branch("tsk_noop", {"noop.txt": "noop\n"})
        self.fixture.git(self.integ, "merge", "--no-ff", "-q", "-m", "already", tip_n)
        pre = self.fixture.head(self.integ)
        script = (
            "import os, subprocess\n"
            "mh = subprocess.check_output(['git', 'rev-parse', '--git-path', 'MERGE_HEAD'], text=True).strip()\n"
            "os.symlink('/nonexistent/target', mh)\n"
        )
        out = merge.run_merge_pipeline(self._plan("run_p12c", "tsk_noop", wt_n, tip_n, script=script))
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "integration_dirty"), out)
        self.assertIn(".git/MERGE_HEAD", out["details"].get("dirty_files", []), out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertEqual(self.fixture.head(self.integ), pre)
        mh_path = self.fixture.git(self.integ, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip()
        mh_abs = mh_path if os.path.isabs(mh_path) else os.path.join(self.integ, mh_path)
        self.assertFalse(os.path.lexists(mh_abs))


class TestP1_3IntegrationWorktreeIsolation(_RealWorktreeFixture):
    """P1-3: integration worktree must pass resolve_trusted_path and is_registered_worktree."""

    def test_failing_test_rewriting_integ_dot_git_never_clobbers_main_repo(self) -> None:
        wt, tip = self._make_task_branch("tsk_p13a", {"a.txt": "a\n"})
        main_head_before = self.fixture.head(self.repo, "main")
        main_sym_before = self.fixture.git(self.repo, "rev-parse", "--symbolic-full-name", "HEAD").stdout.strip()
        self.assertEqual(main_sym_before, "refs/heads/main")
        repo_git_dir = os.path.join(self.repo, ".git")
        script = (
            "import os, sys\n"
            "if os.path.isfile('.git'):\n"
            "    os.unlink('.git')\n"
            "with open('.git', 'w', encoding='utf-8') as f:\n"
            f"    f.write('gitdir: ' + {repo_git_dir!r} + '\\n')\n"
            "sys.exit(1)\n"
        )
        out = merge.run_merge_pipeline(self._plan("run_p13a", "tsk_p13a", wt, tip, script=script))
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "tests_failed"), out)
        self.assertIs(out["details"].get("restored"), False, out)
        self.assertIs(out["details"].get("foreign_repository"), True, out)
        # Main repository HEAD, symref, and index must stay completely intact!
        self.assertEqual(
            self.fixture.git(self.repo, "rev-parse", "--symbolic-full-name", "HEAD").stdout.strip(),
            "refs/heads/main",
        )
        self.assertEqual(self.fixture.head(self.repo, "HEAD"), main_head_before)
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_passing_test_rewriting_integ_dot_git_rejected_without_touching_repo(self) -> None:
        wt, tip = self._make_task_branch("tsk_p13b", {"b.txt": "b\n"})
        main_head_before = self.fixture.head(self.repo, "main")
        repo_git_dir = os.path.join(self.repo, ".git")
        script = (
            "import os\n"
            "if os.path.isfile('.git'):\n"
            "    os.unlink('.git')\n"
            "with open('.git', 'w', encoding='utf-8') as f:\n"
            f"    f.write('gitdir: ' + {repo_git_dir!r} + '\\n')\n"
        )
        out = merge.run_merge_pipeline(self._plan("run_p13b", "tsk_p13b", wt, tip, script=script))
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "integration_dirty"), out)
        self.assertIs(out["details"].get("foreign_repository"), True, out)
        self.assertEqual(
            self.fixture.git(self.repo, "rev-parse", "--symbolic-full-name", "HEAD").stdout.strip(),
            "refs/heads/main",
        )
        self.assertEqual(self.fixture.head(self.repo, "HEAD"), main_head_before)

    def test_symlinked_integ_rejected_in_pipeline_and_recovery(self) -> None:
        wt, tip = self._make_task_branch("tsk_p13c", {"c.txt": "c\n"})
        plan = self._plan("run_p13c", "tsk_p13c", wt, tip)
        outside = os.path.join(self.tmp, "outside_victim")
        os.makedirs(os.path.join(outside, "ro"), mode=0o555)
        self.addCleanup(lambda: os.path.exists(outside) and os.chmod(os.path.join(outside, "ro"), 0o755))
        shutil.rmtree(self.integ)
        os.symlink(outside, self.integ)

        out = merge.run_merge_pipeline(plan)
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "integration_dirty"), out)
        self.assertIs(out["details"].get("foreign_repository"), True, out)
        if sys.platform != "win32":
            self.assertEqual(os.stat(os.path.join(outside, "ro")).st_mode & 0o777, 0o555)

        tok = merge._PROBE_EXPECTED.set((self.repo, self.ws_root))
        try:
            self.assertIsNone(merge.probe_integration_head(self.integ))
        finally:
            merge._PROBE_EXPECTED.reset(tok)

        rec = merge.decide_recovery(
            self.integ,
            "tsk_p13c",
            "mesh/prj_1/tsk_p13c",
            self.base,
            repo=self.repo,
            ws_root=self.ws_root,
        )
        self.assertEqual((rec["outcome"], rec["reason"]), ("failed", "interrupted"), rec)
        self.assertIs(rec["details"].get("foreign_repository"), True, rec)
        self.assertIs(rec["details"].get("restored"), False, rec)
        if sys.platform != "win32":
            self.assertEqual(os.stat(os.path.join(outside, "ro")).st_mode & 0o777, 0o555)


class TestP1_5MergeCancelAndReleaseSync(_RealWorktreeFixture):
    """P1-5: run_task_merge cancellation after TX1 and synchronous reconcile on run.release --force-merge-held."""

    def test_run_task_merge_cancel_after_tx1_still_runs_shielded_pipeline(self) -> None:
        wt, tip = self._make_task_branch("tsk_p15a", {"f.txt": "f\n"})
        now = now_iso_utc()
        deliv = json.dumps({"deliverable": {"summary": "done", "git": {"head": tip}}})
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, deliverable_json, created_by, created_at, updated_at) "
                "VALUES ('tsk_p15a', 'prj_1', 'T15a', 'S', 'code', 'accepted', 'edit', 600, 3, ?, ?, ?, 'usr_1', ?, ?);",
                (wt, "mesh/prj_1/tsk_p15a", deliv, now, now),
            )

        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        srv.log_error = lambda _m: None
        notify_entered = asyncio.Event()
        notify_release = asyncio.Event()

        async def _blocking_notify() -> None:
            notify_entered.set()
            await notify_release.wait()

        srv.notify_waiters = _blocking_notify  # type: ignore[method-assign]
        ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_p15a",
            home=self.home,
            cmd="task.merge",
            args={"task_id": "tsk_p15a"},
            testing_mode=True,
        )

        async def _run() -> None:
            nonlocal notify_entered, notify_release
            notify_entered = asyncio.Event()
            notify_release = asyncio.Event()
            req_task = asyncio.create_task(merge.run_task_merge(srv, ctx))
            await asyncio.wait_for(notify_entered.wait(), timeout=5.0)
            req_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await req_task
            notify_release.set()
            if srv._merge_jobs:
                await asyncio.wait_for(asyncio.gather(*list(srv._merge_jobs)), timeout=15.0)

        asyncio.run(_run())
        row = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_p15a';").fetchone()
        self.assertEqual(row["status"], "merged")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_1';").fetchone())

    def test_release_merge_held_awaits_reconcile_before_response_and_holds_lease(self) -> None:
        wt, tip = self._make_task_branch("tsk_p15b", {"g.txt": "g\n"})
        now = now_iso_utc()
        run_id = "run_held_p15b"
        mrg_id = "mrg_held_p15b"
        run_dir = os.path.join(self.home, "merges", mrg_id)
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, merge_json, created_by, created_at, updated_at) "
                "VALUES ('tsk_p15b', 'prj_1', 'T15b', 'S', 'code', 'merging', 'edit', 600, 3, ?, ?, ?, 'usr_1', ?, ?);",
                (
                    wt,
                    "mesh/prj_1/tsk_p15b",
                    json.dumps({"mrg": mrg_id, "run_id": run_id, "pre_head": self.base, "started_at": now}),
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, "
                "boot_id, run_dir, argv_redacted_json, started_at) "
                "VALUES (?, 'merge_test', 'tsk_p15b', 'prj_1', 1, 0, 'mesh', 'running', 'boot_test', ?, '[]', ?);",
                (run_id, run_dir, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('merge:prj_1', ?, 'meshd', 'boot_test', 0, ?, '2099-01-01T00:00:00Z', ?);",
                (run_id, now, now),
            )

        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        srv.log_error = lambda _m: None

        entered = threading.Event()
        release = threading.Event()

        def _slow_decide(_integ, _tid, _branch, _pre_head, run_dir=None):
            entered.set()
            release.wait(timeout=5.0)
            return {"outcome": "failed", "reason": "interrupted", "pre_head": self.base, "details": {"restored": True}}

        async def _run() -> dict:
            with mock.patch.object(merge, "decide_recovery", side_effect=_slow_decide):
                req_task = asyncio.create_task(
                    _send_req(
                        srv,
                        {
                            "v": 1,
                            "cmd": "run.release",
                            "auth": {"type": "user"},
                            "args": {"run_id": run_id, "force_merge_held": True},
                        },
                    )
                )
                deadline = time.monotonic() + 5.0
                while not entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set(), "decide_recovery did not start")
                # While reconciliation is still running, the response must NOT have returned and lease stays held.
                self.assertFalse(req_task.done(), "run.release returned before reconcile finished")
                lease_while = self.conn.execute(
                    "SELECT run_id FROM leases WHERE key = 'merge:prj_1';"
                ).fetchone()
                self.assertIsNotNone(lease_while, "merge lease released prematurely while reconcile running")
                release.set()
                return await asyncio.wait_for(req_task, timeout=10.0)

        resp = asyncio.run(_run())
        self.assertTrue(resp["ok"], resp)
        # Immediately on receiving the response (no polling!), task must already be decided and lease gone.
        t_row = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_p15b';").fetchone()
        self.assertEqual(t_row["status"], "merge_failed")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_1';").fetchone())
        # A subsequent merge in the same project can proceed immediately without waiting.
        wt_next, _tip_next = self._make_task_branch("tsk_p15b_next", {"n.txt": "n\n"})
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES ('tsk_p15b_next', 'prj_1', 'Next', 'S', 'code', 'accepted', 'edit', 600, 3, ?, "
                "'mesh/prj_1/tsk_p15b_next', 'usr_1', ?, ?);",
                (wt_next, now, now),
            )
        ctx_next = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_next",
            home=self.home,
            cmd="task.merge",
            args={"task_id": "tsk_p15b_next"},
            testing_mode=True,
        )
        merge.check_merge_request(ctx_next, "tsk_p15b_next")

    def test_release_merge_held_retries_transient_lock_and_releases_lease_on_error(self) -> None:
        now = now_iso_utc()
        run_id = "run_held_p15c"
        run_dir = os.path.join(self.home, "merges", "mrg_held_p15c")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "branch, merge_json, created_by, created_at, updated_at) "
                "VALUES ('tsk_p15c', 'prj_1', 'T15c', 'S', 'code', 'merging', 'edit', 600, 3, "
                "'mesh/prj_1/tsk_p15c', ?, 'usr_1', ?, ?);",
                (json.dumps({"mrg": "mrg_held_p15c", "run_id": run_id, "pre_head": self.base, "started_at": now}), now, now),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, "
                "boot_id, run_dir, argv_redacted_json, started_at) "
                "VALUES (?, 'merge_test', 'tsk_p15c', 'prj_1', 1, 0, 'mesh', 'running', 'boot_test', ?, '[]', ?);",
                (run_id, run_dir, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('merge:prj_1', ?, 'meshd', 'boot_test', 0, ?, '2099-01-01T00:00:00Z', ?);",
                (run_id, now, now),
            )

        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        srv.log_error = lambda _m: None

        calls = 0
        orig_recover = merge.recover_merging_task_async

        async def _flaky(conn, home, task):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return await orig_recover(conn, home, task)

        with mock.patch.object(merge, "recover_merging_task_async", side_effect=_flaky):
            resp = asyncio.run(
                _send_req(
                    srv,
                    {
                        "v": 1,
                        "cmd": "run.release",
                        "auth": {"type": "user"},
                        "args": {"run_id": run_id, "force_merge_held": True},
                    },
                )
            )
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(calls, 2)
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_p15c';").fetchone()["status"], "merge_failed")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_1';").fetchone())

    def test_release_merge_held_releases_lease_and_errors_on_persistent_reconcile_failure(self) -> None:
        now = now_iso_utc()
        run_id = "run_held_p15d"
        run_dir = os.path.join(self.home, "merges", "mrg_held_p15d")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "branch, merge_json, created_by, created_at, updated_at) "
                "VALUES ('tsk_p15d', 'prj_1', 'T15d', 'S', 'code', 'merging', 'edit', 600, 3, "
                "'mesh/prj_1/tsk_p15d', ?, 'usr_1', ?, ?);",
                (json.dumps({"mrg": "mrg_held_p15d", "run_id": run_id, "pre_head": self.base, "started_at": now}), now, now),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, "
                "boot_id, run_dir, argv_redacted_json, started_at) "
                "VALUES (?, 'merge_test', 'tsk_p15d', 'prj_1', 1, 0, 'mesh', 'running', 'boot_test', ?, '[]', ?);",
                (run_id, run_dir, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('merge:prj_1', ?, 'meshd', 'boot_test', 0, ?, '2099-01-01T00:00:00Z', ?);",
                (run_id, now, now),
            )

        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        srv.log_error = lambda _m: None

        with mock.patch.object(merge, "recover_merging_task_async", side_effect=RuntimeError("persistent failure")):
            resp = asyncio.run(
                _send_req(
                    srv,
                    {
                        "v": 1,
                        "cmd": "run.release",
                        "auth": {"type": "user"},
                        "args": {"run_id": run_id, "force_merge_held": True},
                    },
                )
            )
        self.assertFalse(resp["ok"], resp)
        self.assertEqual(resp["exit_code"], c.EXIT_INTERNAL)
        # Lease must still be cleaned up in finally so I8 is not violated on the closed run.
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_1';").fetchone())


class TestP2_2RestoreSymlinkReplacedByDir(unittest.TestCase):
    """P2-2: _restore_single_tracked_path must not unlink restored symlinks on old_mode=='000000'."""

    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[key]
        os.environ.update(self.fixture.git_env_vars())
        self.repo = self.fixture.make_k0("P22")

    def test_tracked_symlink_replaced_by_dir_restored_without_pre_index(self) -> None:
        with open(os.path.join(self.repo, "target.txt"), "w", encoding="utf-8") as f:
            f.write("hello\n")
        os.symlink("target.txt", os.path.join(self.repo, "doclink"))
        self.fixture.git(self.repo, "add", "target.txt", "doclink")
        self.fixture.git(self.repo, "commit", "-q", "-m", "add symlink")
        pre_head = self.fixture.head(self.repo)

        # Simulate replacing tracked symlink `doclink` with a directory and staging a child
        # so `git diff --raw -z pre_head` emits `D doclink` followed by `A doclink/sub.txt`.
        os.unlink(os.path.join(self.repo, "doclink"))
        os.makedirs(os.path.join(self.repo, "doclink"))
        with open(os.path.join(self.repo, "doclink", "sub.txt"), "w", encoding="utf-8") as f:
            f.write("sub\n")
        self.fixture.git(self.repo, "add", "doclink/sub.txt")

        merge._restore_tracked_from_ref(self.repo, pre_head)
        link_path = os.path.join(self.repo, "doclink")
        self.assertTrue(os.path.islink(link_path), "doclink symlink was unlinked during restore!")
        self.assertEqual(os.readlink(link_path), "target.txt")

    def test_old_mode_zero_never_follows_or_unlinks_symlink_parent(self) -> None:
        outside = tempfile.mkdtemp(prefix="mh-p22-out-")
        self.addCleanup(shutil.rmtree, outside, True)
        victim = os.path.join(outside, "keep.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("keep\n")
        os.symlink(outside, os.path.join(self.repo, "symdir"))
        merge._restore_single_tracked_path(
            self.repo,
            os.path.realpath(self.repo),
            None,
            "symdir/keep.txt",
            "000000",
        )
        self.assertTrue(os.path.islink(os.path.join(self.repo, "symdir")))
        self.assertTrue(os.path.isfile(victim))


class TestP2_3CheckMergeRequestMutualExclusion(_RealWorktreeFixture):
    """P2-3: check_merge_request blocks project-wide ws reservations and sibling-project repo merges."""

    def test_blocks_when_other_task_in_same_project_has_ws_delete_reservation(self) -> None:
        now = now_iso_utc()
        wt_b, _tip_b = self._make_task_branch("tsk_b", {"b.txt": "b\n"})
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES ('tsk_a', 'prj_1', 'A', 'S', 'code', 'accepted', 'edit', 600, 3, '/tmp/a', "
                "'mesh/prj_1/tsk_a', 'usr_1', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES ('tsk_b', 'prj_1', 'B', 'S', 'code', 'accepted', 'edit', 600, 3, ?, "
                "'mesh/prj_1/tsk_b', 'usr_1', ?, ?);",
                (wt_b, now, now),
            )
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?);",
                (
                    ws_delete_reservation_key("tsk_a"),
                    json.dumps({"kind": "delete", "task_id": "tsk_a", "project_id": "prj_1"}),
                ),
            )

        ctx = SimpleNamespace(conn=self.conn, principal={"id": "usr_1"}, actor_kind="user", via="direct")
        with self.assertRaises(MeshError) as cm:
            merge.check_merge_request(ctx, "tsk_b")
        self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.CONCURRENCY_LIMIT, c.EXIT_BUSY))

    def test_blocks_when_sibling_project_on_same_repo_is_merging(self) -> None:
        now = now_iso_utc()
        setup_integration(self.repo, "prj_2", self.base, self.ws_root)
        wt_2 = os.path.join(self.ws_root, "prj_2", "tasks", "tsk_2")
        os.makedirs(os.path.dirname(wt_2), mode=0o700, exist_ok=True)
        self.fixture.git(self.repo, "worktree", "add", "-b", "mesh/prj_2/tsk_2", wt_2, self.base)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
                "base_commit, created_by, created_via, version, created_at, updated_at) "
                "VALUES ('prj_2', 'P2', 'goal', 'active', ?, 'main', 'mesh/prj_2/integration', ?, 'usr_1', 'direct', 1, ?, ?);",
                (self.repo, self.base, now, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES ('tsk_1', 'prj_1', 'T1', 'S', 'code', 'merging', 'edit', 600, 3, '/tmp/1', "
                "'mesh/prj_1/tsk_1', 'usr_1', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, "
                "boot_id, run_dir, argv_redacted_json, started_at) "
                "VALUES ('run_sib', 'merge_test', 'tsk_1', 'prj_1', 1, 0, 'mesh', 'running', 'boot_1', '/tmp/m', '[]', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('merge:prj_1', 'run_sib', 'meshd', 'boot_1', 0, ?, '2099-01-01T00:00:00Z', ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES ('tsk_2', 'prj_2', 'T2', 'S', 'code', 'accepted', 'edit', 600, 3, ?, "
                "'mesh/prj_2/tsk_2', 'usr_1', ?, ?);",
                (wt_2, now, now),
            )

        ctx = SimpleNamespace(conn=self.conn, principal={"id": "usr_1"}, actor_kind="user", via="direct")
        with self.assertRaises(MeshError) as cm:
            merge.check_merge_request(ctx, "tsk_2")
        self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.CONCURRENCY_LIMIT, c.EXIT_BUSY))
        self.assertEqual(cm.exception.details.get("merge_project_id"), "prj_1")


if __name__ == "__main__":
    unittest.main()
