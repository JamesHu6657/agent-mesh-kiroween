"""Regression unit tests for PR #45 review findings (P1-1, P1-2, P2-1, P2-2, P2-3, P3-1)."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.auth import generate_run_token
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.leases import get_lease_ttl_s
from mesh.core.proc import get_proc_stat, is_tgid_alive
from mesh.core.settings import validate_setting
from mesh.core.token_roots import remember_protected_path
from mesh.daemon import merge, recovery, scheduler, supervisor, terminate
from mesh.daemon.handlers.approval import (
    handle_approval_approve,
    handle_approval_cancel,
    handle_approval_check,
    handle_approval_consume,
    handle_approval_reject,
)
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.project import _prepare_repo_project, handle_project_update
from mesh.daemon.handlers.run import handle_workspace_prune
from mesh.daemon.server import DaemonServer
from mesh.errors import MeshError
from tests.gitfixture import GitFixture
from tests.unit import test_merge as tm

# Calls merge.run_merge_pipeline and supervisor.watch_launch_runs scanning real /proc.
MESH_PROC_LOCK = "shared"


def _user_ctx(
    conn: sqlite3.Connection,
    home: str,
    cmd: str,
    args: dict,
    dry_run: bool = False,
    testing_mode: bool = True,
    k_user: bytes = None,
) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal={"id": "usr_1", "kind": "user", "name": "user"},
        actor_kind="user",
        via="direct",
        request_id="req_r1",
        home=home,
        cmd=cmd,
        args=args,
        dry_run=dry_run,
        testing_mode=testing_mode,
        k_user=k_user,
    )


def _setup_home_and_db(tmp: str):
    home = os.path.join(tmp, "home")
    os.makedirs(home, mode=0o700, exist_ok=True)
    for sub in ("logs", "run", "runs", "workspaces"):
        os.makedirs(os.path.join(home, sub), mode=0o700, exist_ok=True)
    conn = init_db(os.path.join(home, "mesh.db"))
    conn.row_factory = sqlite3.Row
    now = now_iso_utc()
    with transaction(conn):
        conn.execute(
            "INSERT INTO boots (id, pid, started_at, crashed) VALUES ('boot_cur', ?, ?, 0);",
            (os.getpid(), now),
        )
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, default_profile, max_unapproved_profile, "
            "default_model, allowed_models_json, max_concurrency, created_at, updated_at) "
            "VALUES ('agt_lead', 'fake', 'launch', 1, 'edit', 'dev', 'model-a', '[\"model-a\"]', 2, ?, ?);",
            (now, now),
        )
    return home, conn


def _wait_until(pred, timeout: float = 5.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


class TestP1_1RepoWorkspaceOverlapAndSymlinkDefaultRoot(unittest.TestCase):
    """P1-1: historical workspaces.root / protected paths & symlinked default workspaces dir."""

    def test_prepare_repo_project_blocks_historical_and_protected_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr45-p11-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            fixture = GitFixture()
            self.addCleanup(fixture.cleanup)
            try:
                old_ws = os.path.join(tmp, "ws_old")
                new_ws = os.path.join(tmp, "ws_new")
                os.makedirs(old_ws, mode=0o700)
                os.makedirs(new_ws, mode=0o700)

                # Historical task worktree repo inside old_ws, then workspaces.root switches to new_ws.
                hist_task_repo = os.path.join(old_ws, "prj_1", "tasks", "tsk_old")
                os.makedirs(os.path.dirname(hist_task_repo), mode=0o700, exist_ok=True)
                shutil.copytree(fixture.make_k0("hist"), hist_task_repo)

                with transaction(conn):
                    remember_protected_path(conn, old_ws)
                    record_event(
                        conn=conn,
                        type_="config.updated",
                        actor_id="usr_1",
                        actor_kind="user",
                        via="direct",
                        payload={"key": "workspaces.root", "value": old_ws},
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                        "VALUES ('workspaces.root', ?, ?, 'usr_1');",
                        (json.dumps(new_ws), now_iso_utc()),
                    )

                for bad_repo in (hist_task_repo, old_ws):
                    with self.subTest(bad_repo=bad_repo):
                        if bad_repo == old_ws:
                            # Ensure old_ws itself isn't rejected for missing repo before overlap check,
                            # or check parent overlap with a git repo rooted at old_ws.
                            git_dir_src = os.path.join(hist_task_repo, ".git")
                            if not os.path.exists(os.path.join(old_ws, ".git")):
                                shutil.copytree(git_dir_src, os.path.join(old_ws, ".git"))
                        ctx = _user_ctx(conn, home, "project.create", {
                            "repo": bad_repo,
                            "base_branch": "main",
                            "test_command": "none",
                        })
                        with self.assertRaises(MeshError) as cm:
                            _prepare_repo_project(ctx)
                        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            finally:
                conn.close()

    def test_validate_setting_rejects_symlinked_default_and_symlink_val(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr45-sym-") as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home, mode=0o700)
            ws_link = os.path.join(home, "workspaces")
            # Point $MESH_HOME/workspaces at $MESH_HOME itself!
            os.symlink(home, ws_link)
            with self.assertRaises(MeshError) as cm:
                validate_setting("workspaces.root", home, home=home)
            self.assertEqual(cm.exception.code, c.INVALID_SETTING)

            # Also reject a symlink val pointing to a valid external 0700 directory.
            real_target = os.path.join(tmp, "real_ws")
            os.makedirs(real_target, mode=0o700)
            val_link = os.path.join(tmp, "link_ws")
            os.symlink(real_target, val_link)
            with self.assertRaises(MeshError) as cm2:
                validate_setting("workspaces.root", val_link, home=home)
            self.assertEqual(cm2.exception.code, c.INVALID_SETTING)

            # Also reject configuring the symlinked default workspaces path itself.
            outside_target = os.path.join(tmp, "outside_ws")
            os.makedirs(outside_target, mode=0o700)
            os.unlink(ws_link)
            os.symlink(outside_target, ws_link)
            with self.assertRaises(MeshError) as cm3:
                validate_setting("workspaces.root", ws_link, home=home)
            self.assertEqual(cm3.exception.code, c.INVALID_SETTING)


class TestP1_2MergeCleanupAndCrashRecovery(tm.TaskBranchCase):
    """P1-2: crash recovery untracked cleanup, 0o500 read-only artifact cleanup, pre-commit guard."""

    def test_decide_recovery_with_merge_head_cleans_untracked_and_readonly_dirs(self) -> None:
        self.fixture.git(self.repo, "merge", "-q", "--no-ff", "--no-commit", self.BRANCH)
        self.assertTrue(self.fixture.has_merge_head(self.repo))

        with open(os.path.join(self.repo, ".coverage"), "w", encoding="utf-8") as f:
            f.write("cov\n")
        os.makedirs(os.path.join(self.repo, "reports"), mode=0o755, exist_ok=True)
        with open(os.path.join(self.repo, "reports", "junit.xml"), "w", encoding="utf-8") as f:
            f.write("<testsuite/>\n")
        ro_dir = os.path.join(self.repo, "ro_cache")
        os.makedirs(ro_dir, mode=0o755)
        with open(os.path.join(ro_dir, "cached.bin"), "wb") as f:
            f.write(b"x")
        os.chmod(ro_dir, 0o500)
        self.addCleanup(lambda: os.path.isdir(ro_dir) and os.chmod(ro_dir, 0o700))

        out = merge.decide_recovery(self.repo, self.TASK, self.BRANCH, self.base)
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "interrupted"))
        self.assertIs(out["details"]["restored"], True, out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".coverage")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "reports")))
        self.assertFalse(os.path.exists(ro_dir))
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_merge_pipeline_persists_pre_untracked_cleans_ro_and_recovers_only_new(self) -> None:
        pre_keep = os.path.join(self.repo, "preexisting.txt")
        with open(pre_keep, "w", encoding="utf-8") as f:
            f.write("keep me\n")
        run_dir = os.path.join(self.fixture.base, "run_crash")
        os.makedirs(run_dir, mode=0o700)

        # Simulate pre_untracked snapshot persisted and test creating artifact before crash.
        self.fixture.git(self.repo, "merge", "-q", "--no-ff", "--no-commit", self.BRANCH)
        with open(os.path.join(run_dir, "pre_untracked.json"), "w", encoding="utf-8") as f:
            json.dump(["preexisting.txt"], f)
        with open(os.path.join(self.repo, "artifact.tmp"), "w", encoding="utf-8") as f:
            f.write("temp\n")

        out = merge.decide_recovery(self.repo, self.TASK, self.BRANCH, self.base, run_dir=run_dir)
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "interrupted"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "artifact.tmp")))
        self.assertTrue(os.path.exists(pre_keep))

    def test_merge_pipeline_cleans_readonly_test_dir_before_commit_and_aborts_if_still_dirty(self) -> None:
        # 1. Online path where test_command creates 0o500 cache dir and exits 0 -> must clean & stay clean!
        script = (
            "import os\n"
            "d = os.path.join(os.getcwd(), 'ro_cache')\n"
            "os.makedirs(d, mode=0o755, exist_ok=True)\n"
            "with open(os.path.join(d, 'pkg.a'), 'w') as f:\n"
            "    f.write('cache')\n"
            "os.chmod(d, 0o500)\n"
        )
        run_dir1 = os.path.join(self.fixture.base, "run_ok")
        os.makedirs(run_dir1, mode=0o700)
        plan1 = {
            "run_id": "run_ok",
            "task_id": self.TASK,
            "title": "t",
            "integration": self.repo,
            "worktree": self.repo,
            "branch": self.BRANCH,
            "pre_head": self.base,
            "test_command": "custom",
            "test_argv": [sys.executable, "-c", script],
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir1,
        }
        out1 = merge.run_merge_pipeline(plan1)
        self.assertEqual(out1["outcome"], "merged", out1)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ro_cache")))
        self.assertTrue(self.fixture.is_clean(self.repo))

        # 2. If cleanup still leaves integ dirty after passing tests, pipeline aborts rather than committing dirty!
        self.fixture.git(self.repo, "checkout", "-b", "mesh/prj_x/tsk_dirty", self.base)
        with open(os.path.join(self.repo, "second.txt"), "w", encoding="utf-8") as f:
            f.write("second\n")
        self.fixture.git(self.repo, "add", "second.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "second")
        self.fixture.git(self.repo, "checkout", "-q", "main")
        head_before = self.fixture.head(self.repo)

        run_dir2 = os.path.join(self.fixture.base, "run_dirty")
        os.makedirs(run_dir2, mode=0o700)
        plan2 = {
            "run_id": "run_dirty",
            "task_id": "tsk_dirty",
            "title": "dirty",
            "integration": self.repo,
            "worktree": self.repo,
            "branch": "mesh/prj_x/tsk_dirty",
            "pre_head": head_before,
            "test_command": "custom",
            "test_argv": [sys.executable, "-c", "open('stubborn.tmp', 'w').write('dirty')"],
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir2,
        }
        try:
            with mock.patch.object(merge, "_safe_unlink_untracked", side_effect=OSError("cant_unlink")):
                out2 = merge.run_merge_pipeline(plan2)
            self.assertEqual(out2["outcome"], "failed", out2)
            self.assertFalse(self.fixture.has_merge_head(self.repo))
            self.assertEqual(self.fixture.head(self.repo), head_before)
        finally:
            stub = os.path.join(self.repo, "stubborn.tmp")
            if os.path.exists(stub):
                os.unlink(stub)


class TestP2_1WorkspaceDeleteFailureAndParentSymlinks(unittest.TestCase):
    """P2-1: workspace delete must clean 0o500 dirs, never fake deleted:true/audit on error, recheck parents."""

    def _drive_client(self, srv: DaemonServer, cmd: str, args: dict, idem_key: str = "") -> dict:
        async def _call() -> dict:
            reader = asyncio.StreamReader()
            req = {"v": 1, "cmd": cmd, "args": args, "request_id": "req_p21"}
            if idem_key:
                req["idempotency_key"] = idem_key
            reader.feed_data((json.dumps(req) + "\n").encode("utf-8"))
            reader.feed_eof()
            buf = bytearray()

            class _W:
                def get_extra_info(self, *_a):
                    return None
                def write(self, data: bytes) -> None:
                    buf.extend(data)
                async def drain(self) -> None:
                    return None
                def close(self) -> None:
                    pass
                async def wait_closed(self) -> None:
                    pass

            with mock.patch("mesh.daemon.server.get_peer_credentials", return_value=(os.getpid(), os.getuid(), os.getgid())), \
                 mock.patch.object(srv, "authenticate_and_authorize",
                                   return_value=({"id": "usr_1", "kind": "user", "name": "user"}, "user", "direct", None)):
                await srv.handle_client(reader, _W())
            return json.loads(buf.decode("utf-8").splitlines()[0])

        return asyncio.run(_call())

    def test_workspace_delete_cleans_ro_subdir_and_fails_honestly_on_oserror(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr45-p21-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                ws1 = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_ro")
                ro = os.path.join(ws1, "readonly_dir")
                os.makedirs(ro, mode=0o755)
                with open(os.path.join(ro, "artifact.txt"), "w") as f:
                    f.write("data\n")
                os.chmod(ro, 0o500)
                self.addCleanup(lambda: os.path.isdir(ro) and os.chmod(ro, 0o700))

                ws2 = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_err")
                os.makedirs(ws2, mode=0o755)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                        "VALUES ('prj_1', 'P', 'G', 'active', 'usr_1', 'direct', ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_ro', 'prj_1', 'RO', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_ro', 'usr_1', ?, ?);",
                        (ws1, now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_err', 'prj_1', 'ERR', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_err', 'usr_1', ?, ?);",
                        (ws2, now, now),
                    )

                srv = DaemonServer(home, testing_mode=True)
                srv.db_conn = conn

                # 1. 0o500 child dir should be removed cleanly.
                resp_ro = self._drive_client(srv, "workspace.delete", {"task_id": "tsk_ro"})
                self.assertTrue(resp_ro["ok"], resp_ro)
                self.assertTrue(resp_ro["data"]["deleted"])
                self.assertFalse(os.path.exists(ws1))

                # 2. If underlying removal genuinely fails, do NOT return deleted:true, audit workspace.deleted, or cache idem!
                with mock.patch("shutil.rmtree", side_effect=PermissionError(13, "denied", ws2)):
                    resp_err = self._drive_client(srv, "workspace.delete", {"task_id": "tsk_err"}, idem_key="k-fail")
                self.assertFalse(resp_err["ok"], resp_err)
                self.assertTrue(os.path.isdir(ws2))
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = 'tsk_err';"
                ).fetchone()[0]
                self.assertEqual(cnt, 0)
                idem = conn.execute(
                    "SELECT COUNT(*) FROM idempotency WHERE key = 'k-fail';"
                ).fetchone()[0]
                self.assertEqual(idem, 0)

                # Retry without patched failure now succeeds!
                resp_ok = self._drive_client(srv, "workspace.delete", {"task_id": "tsk_err"}, idem_key="k-fail")
                self.assertTrue(resp_ok["ok"], resp_ok)
                self.assertFalse(os.path.exists(ws2))
            finally:
                conn.close()

    def test_workspace_delete_rechecks_parent_symlinks_and_prune_off_loop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr45-p21par-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                tasks_dir = os.path.join(home, "workspaces", "prj_1", "tasks")
                ws_real = os.path.join(tasks_dir, "tsk_par")
                os.makedirs(ws_real, mode=0o700)
                outside = os.path.join(tmp, "outside_tasks")
                os.makedirs(outside, mode=0o700)
                with open(os.path.join(outside, "precious.txt"), "w") as f:
                    f.write("keep")

                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                        "VALUES ('prj_1', 'P', 'G', 'active', 'usr_1', 'direct', ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_par', 'prj_1', 'Par', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_par', 'usr_1', ?, ?);",
                        (ws_real, now, now),
                    )

                srv = DaemonServer(home, testing_mode=True)
                srv.db_conn = conn

                # Simulate TOCTOU swap right before actual rmtree: swap `tasks/` to a symlink pointing outside!
                real_rmtree = shutil.rmtree

                def _swap_then_rmtree(path, *a, **kw):
                    # If parent symlink recheck ran before rmtree, this mock won't even be reached;
                    # if it IS reached, that proves the TOCTOU check missed the symlink!
                    return real_rmtree(path, *a, **kw)

                # Even simpler: check directly if `tasks_dir` was replaced before the delete step runs
                # by swapping `tasks_dir` inside the worker helper right after _task_workspace_match passes.
                from mesh.daemon.handlers import run as run_handlers
                orig_match = run_handlers._task_workspace_match

                def _match_then_swap(*a, **kw):
                    res = orig_match(*a, **kw)
                    if res is not None and os.path.isdir(tasks_dir) and not os.path.islink(tasks_dir):
                        target_inside = os.path.join(outside, "tsk_par")
                        os.makedirs(target_inside, exist_ok=True)
                        shutil.rmtree(tasks_dir)
                        os.symlink(outside, tasks_dir)
                    return res

                with mock.patch.object(run_handlers, "_task_workspace_match", side_effect=_match_then_swap), \
                     mock.patch("shutil.rmtree", side_effect=_swap_then_rmtree):
                    resp = self._drive_client(srv, "workspace.delete", {"task_id": "tsk_par"})
                self.assertFalse(resp["ok"], resp)
                self.assertTrue(os.path.exists(os.path.join(outside, "precious.txt")))
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted';").fetchone()[0],
                    0,
                )

                # Verify workspace.prune runs execute_workspace_prune_plan off the event loop via asyncio.to_thread.
                observed_threads = []

                async def _check_prune_thread() -> None:
                    import threading
                    main_tid = threading.get_ident()
                    real_exec = getattr(run_handlers, "execute_workspace_prune_plan", None)
                    if real_exec is None:
                        observed_threads.append(main_tid)
                        return

                    def _spy_exec(plan):
                        observed_threads.append(threading.get_ident())
                        return real_exec(plan)

                    with mock.patch.object(run_handlers, "execute_workspace_prune_plan", side_effect=_spy_exec):
                        reader = asyncio.StreamReader()
                        req = {"v": 1, "cmd": "workspace.prune", "args": {}, "request_id": "req_prn"}
                        reader.feed_data((json.dumps(req) + "\n").encode("utf-8"))
                        reader.feed_eof()
                        buf = bytearray()

                        class _W:
                            def get_extra_info(self, *_a):
                                return None
                            def write(self, data: bytes) -> None:
                                buf.extend(data)
                            async def drain(self) -> None:
                                return None
                            def close(self) -> None:
                                pass
                            async def wait_closed(self) -> None:
                                pass

                        with mock.patch("mesh.daemon.server.get_peer_credentials", return_value=(os.getpid(), os.getuid(), os.getgid())), \
                             mock.patch.object(srv, "authenticate_and_authorize",
                                               return_value=({"id": "usr_1", "kind": "user", "name": "user"}, "user", "direct", None)):
                            await srv.handle_client(reader, _W())
                    self.assertTrue(observed_threads)
                    self.assertNotEqual(observed_threads[0], main_tid)

                asyncio.run(_check_prune_thread())
            finally:
                conn.close()


class TestP2_2TerminationLoopTestingModePropagation(unittest.TestCase):
    """P2-2: 200 ms _termination_loop / advance_terminations must pass server.testing_mode."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-pr45-p22-")
        self.addCleanup(shutil.rmtree, self.home, True)
        for sub in ("logs", "run", "runs", "workspaces"):
            os.makedirs(os.path.join(self.home, sub), mode=0o700, exist_ok=True)
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self._reset()
        self.addCleanup(self._reset)

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO boots (id, pid, started_at, crashed) VALUES ('boot_cur', ?, ?, 0);",
                (os.getpid(), now),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, default_profile, max_unapproved_profile, "
                "default_model, allowed_models_json, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_lead', 'droid', 'launch', 1, 'edit', 'dev', 'model-a', '[\"model-a\"]', 2, ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P', 'G', 'active', 'usr_1', 'direct', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
                (now,),
            )

    def _reset(self) -> None:
        supervisor._evict_seen(set())
        supervisor._CLEAR.clear()
        supervisor._SPAWN_ABORTS.clear()
        supervisor._WRAPPERS.clear()
        for rid in list(terminate.active()):
            terminate.forget(rid)

    def _run_loop_until_closed(self, srv: DaemonServer, run_id: str) -> None:
        async def _drive() -> None:
            srv.stop_event = asyncio.Event()
            task = asyncio.create_task(srv._termination_loop())
            try:
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    row = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()
                    if row is not None and row["status"] not in ("starting", "running"):
                        break
                    await asyncio.sleep(0.03)
            finally:
                srv.stop_event.set()
                await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(_drive())

    def test_termination_loop_testing_mode_false_ignores_env_mesh_testing_1(self) -> None:
        run_id = "run_lt_false"
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir, mode=0o700)
        now = now_iso_utc()
        # Dead wrapper pid (reuse a finished child pid) + exit.json with task.create timeout=1
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=5)
        with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as f:
            json.dump({"exit_code": 0, "signal": None, "ended_at": now}, f)
        with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd": "task.create", "args": {"title": "SubFloor", "spec": "S", "timeout": 1}}]\n```\n')
        _, lookup, tok_hash = generate_run_token()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at, lead_assignment_id) "
                "VALUES (?, 'lead_turn', 'prj_1', 'agt_lead', 0, 1, 'droid', 'running', 'boot_cur', ?, ?, 1, ?, ?, ?, 'lead_1');",
                (run_id, dead.pid, dead.pid, run_dir, now, now_iso_utc(time.time() + 300)),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('leadturn:prj_1', ?, 'meshd', 'boot_cur', 'agt_lead', 1, ?, ?, ?);",
                (run_id, now, now_iso_utc(time.time() + 300), now),
            )
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES ('cred_f', 'agt_lead', 'run_token', ?, ?, ?, ?, ?);",
                (lookup, tok_hash, json.dumps({"role": "lead", "project_id": "prj_1", "lead_assignment_id": "lead_1", "run_id": run_id}),
                 now, now_iso_utc(time.time() + 300)),
            )

        with (
            mock.patch.dict(os.environ, {"MESH_TESTING": "1"}),
            mock.patch.object(terminate, "run_targets", side_effect=lambda *a, **k: terminate.ScanResult()),
        ):
            # First scan under testing_mode=False records pending exit check.
            self.assertEqual(supervisor.watch_launch_runs(self.conn, self.home, testing_mode=False), [])
            self.assertTrue(supervisor.pending_exit_checks())
            srv = DaemonServer(self.home, testing_mode=False)
            srv.db_conn = self.conn
            self._run_loop_until_closed(srv, run_id)

        run_row = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertEqual(run_row["status"], "exited")
        # In testing_mode=False, timeout=1 must be rejected (< 60s floor), even though MESH_TESTING=1 in env!
        subfloor = self.conn.execute("SELECT timeout_s FROM tasks WHERE title = 'SubFloor';").fetchone()
        self.assertIsNone(subfloor, f"Task SubFloor with timeout=1 should be rejected when testing_mode=False: {dict(subfloor) if subfloor else None}")
        ev = self.conn.execute(
            "SELECT payload_json FROM events WHERE run_id = ? AND type = 'lead.action_result';",
            (run_id,),
        ).fetchone()
        self.assertIsNotNone(ev)
        self.assertFalse(json.loads(ev["payload_json"])["ok"], ev["payload_json"])

    def test_termination_loop_testing_mode_true_ignores_absent_env_mesh_testing(self) -> None:
        run_id = "run_lt_true"
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir, mode=0o700)
        now = now_iso_utc()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=5)
        with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as f:
            json.dump({"exit_code": 0, "signal": None, "ended_at": now}, f)
        # fake adapter parses JSON {"session_id": "..."} from stdout.log!
        with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            json.dump({"session_id": "sess_123", "final_text": "ok"}, f)
        _, lookup, tok_hash = generate_run_token()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at, lead_assignment_id) "
                "VALUES (?, 'lead_turn', 'prj_1', 'agt_lead', 0, 1, 'fake', 'running', 'boot_cur', ?, ?, 1, ?, ?, ?, 'lead_1');",
                (run_id, dead.pid, dead.pid, run_dir, now, now_iso_utc(time.time() + 300)),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('leadturn:prj_1', ?, 'meshd', 'boot_cur', 'agt_lead', 1, ?, ?, ?);",
                (run_id, now, now_iso_utc(time.time() + 300), now),
            )
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES ('cred_t', 'agt_lead', 'run_token', ?, ?, ?, ?, ?);",
                (lookup, tok_hash, json.dumps({"role": "lead", "project_id": "prj_1", "lead_assignment_id": "lead_1", "run_id": run_id}),
                 now, now_iso_utc(time.time() + 300)),
            )

        clean_env = dict(os.environ)
        clean_env.pop("MESH_TESTING", None)
        with (
            mock.patch.dict(os.environ, clean_env, clear=True),
            mock.patch.object(terminate, "run_targets", side_effect=lambda *a, **k: terminate.ScanResult()),
        ):
            self.assertEqual(supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True), [])
            self.assertTrue(supervisor.pending_exit_checks())
            srv = DaemonServer(self.home, testing_mode=True)
            srv.db_conn = self.conn
            self._run_loop_until_closed(srv, run_id)

        run_row = self.conn.execute("SELECT status, session_id FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertEqual(run_row["status"], "exited")
        self.assertEqual(run_row["session_id"], "sess_123")
        asg_row = self.conn.execute("SELECT session_id FROM lead_assignments WHERE id = 'lead_1';").fetchone()
        self.assertEqual(asg_row["session_id"], "sess_123")

    def test_termination_loop_finish_run_propagates_testing_mode_to_vendor_nonzero_failure(self) -> None:
        """G5 + PR45 r1: 200ms loop -> _finish_run -> _vendor_nonzero_failure honors server.testing_mode."""
        def _stage_task_run(suffix: str, *, testing_mode: bool, env_patch: dict, clear_env: bool = False) -> dict:
            self._reset()
            task_id = f"tsk_nz_{suffix}"
            run_id = f"run_nz_{suffix}"
            run_dir = os.path.join(self.home, "runs", run_id)
            os.makedirs(run_dir, mode=0o700, exist_ok=True)
            now = now_iso_utc()
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait(timeout=5)
            with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as f:
                json.dump({"exit_code": 2, "signal": None, "ended_at": now}, f)
            with open(os.path.join(run_dir, "stderr.log"), "w", encoding="utf-8") as f:
                f.write("simulated vendor auth failure\n")
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, model, "
                    "profile, timeout_s, max_attempts, attempt, fence_seq, created_by, created_at, updated_at) "
                    "VALUES (?, 'prj_1', ?, 'S', 'general', 'running', 'agt_lead', 'model-a', "
                    "'edit', 600, 3, 1, 1, 'usr_1', ?, ?);",
                    (task_id, f"Task {suffix}", now, now),
                )
                self.conn.execute(
                    "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                    "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at) "
                    "VALUES (?, 'task', ?, 'prj_1', 'agt_lead', 1, 1, 'fake', 'running', 'boot_cur', ?, ?, 1, ?, ?, ?);",
                    (run_id, task_id, dead.pid, dead.pid, run_dir, now, now_iso_utc(time.time() + 300)),
                )
                self.conn.execute(
                    "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                    "VALUES (?, ?, 'meshd', 'boot_cur', 'agt_lead', 1, ?, ?, ?);",
                    (f"task:{task_id}", run_id, now, now_iso_utc(time.time() + 300), now),
                )

            seen_stderr: list = []

            def _fake_parse_stderr(_self, text: str) -> dict:
                seen_stderr.append(text)
                return {"reason": "vendor_auth", "details": {"vendor_code": 401}}

            with (
                mock.patch.dict(os.environ, env_patch, clear=clear_env),
                mock.patch.object(terminate, "run_targets", side_effect=lambda *a, **k: terminate.ScanResult()),
                mock.patch("mesh.adapters.fake.FakeAdapter.parse_stderr_error", create=True, new=_fake_parse_stderr),
            ):
                self.assertEqual(supervisor.watch_launch_runs(self.conn, self.home, testing_mode=testing_mode), [])
                self.assertTrue(supervisor.pending_exit_checks())
                srv = DaemonServer(self.home, testing_mode=testing_mode)
                srv.db_conn = self.conn
                self._run_loop_until_closed(srv, run_id)

            run_row = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()
            task_row = self.conn.execute("SELECT status, failure_json FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            self.assertEqual(run_row["status"], "exited")
            self.assertEqual(task_row["status"], "failed")
            return {"failure": json.loads(task_row["failure_json"]), "seen_stderr": seen_stderr}

        # testing_mode=False ignores MESH_TESTING=1 -> fake adapter disabled -> fallback exit_nonzero
        res_false = _stage_task_run("false", testing_mode=False, env_patch={"MESH_TESTING": "1"})
        self.assertEqual(res_false["seen_stderr"], [])
        self.assertEqual(res_false["failure"]["reason"], "exit_nonzero")

        # testing_mode=True ignores absent MESH_TESTING -> fake adapter enabled -> parsed vendor_auth
        clean_env = dict(os.environ)
        clean_env.pop("MESH_TESTING", None)
        res_true = _stage_task_run("true", testing_mode=True, env_patch=clean_env, clear_env=True)
        self.assertEqual(len(res_true["seen_stderr"]), 1)
        self.assertEqual(res_true["failure"]["reason"], "vendor_auth")
        self.assertEqual(res_true["failure"]["details"].get("vendor_code"), 401)


class TestP2_3AndP3_1ApprovalsAndProjectUpdate(unittest.TestCase):
    """P2-3: terminal approvals stay non-expired past expires_at; P3-1: project.update plural criteria."""

    def test_approval_check_terminal_statuses_not_expired_past_ttl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr45-p23-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                k_user = b"\x01" * 32
                past = now_iso_utc(time.time() - 3600)
                with transaction(conn):
                    apr_rej = create_or_get_pending_approval(
                        conn=conn, category="deploy", op="custom.op", requested_by="agt_lead",
                        params={"x": 1}, summary="Reject me", testing_mode=True,
                    )
                    apr_can = create_or_get_pending_approval(
                        conn=conn, category="deploy", op="custom.op", requested_by="agt_lead",
                        params={"x": 2}, summary="Cancel me", testing_mode=True,
                    )
                    apr_con = create_or_get_pending_approval(
                        conn=conn, category="deploy", op="custom.op", requested_by="agt_lead",
                        params={"x": 3}, summary="Consume me", testing_mode=True,
                    )
                    apr_pend = create_or_get_pending_approval(
                        conn=conn, category="deploy", op="custom.op", requested_by="agt_lead",
                        params={"x": 4}, summary="Expire me", testing_mode=True,
                    )
                    handle_approval_reject(_user_ctx(conn, home, "approval.reject", {
                        "approval_id": apr_rej["approval_id"], "op_hash": apr_rej["op_hash"],
                    }, k_user=k_user))
                    handle_approval_cancel(_user_ctx(conn, home, "approval.cancel", {
                        "approval_id": apr_can["approval_id"],
                    }, k_user=k_user))
                    handle_approval_approve(_user_ctx(conn, home, "approval.approve", {
                        "approval_id": apr_con["approval_id"], "op_hash": apr_con["op_hash"],
                    }, k_user=k_user))
                    handle_approval_consume(HandlerContext(
                        conn=conn,
                        principal={"id": "agt_lead", "kind": "agent", "name": "lead1"},
                        actor_kind="agent",
                        via="token",
                        request_id="req_r1",
                        home=home,
                        cmd="approval.consume",
                        args={"approval_id": apr_con["approval_id"], "op_hash": apr_con["op_hash"]},
                        dry_run=False,
                        testing_mode=True,
                    ))
                    conn.execute("UPDATE approvals SET expires_at = ?;", (past,))

                expired = scheduler.sweep_expired_approvals(conn, home)
                self.assertEqual(expired, [apr_pend["approval_id"]])

                for apr, expected_status in (
                    (apr_rej, "rejected"),
                    (apr_can, "cancelled"),
                    (apr_con, "consumed"),
                ):
                    with self.subTest(expected_status=expected_status):
                        res = handle_approval_check(_user_ctx(conn, home, "approval.check", {
                            "approval_id": apr["approval_id"], "op_hash": apr["op_hash"],
                        }))
                        self.assertEqual(res.data["status"], expected_status)

                with self.assertRaises(MeshError) as cm:
                    handle_approval_check(_user_ctx(conn, home, "approval.check", {
                        "approval_id": apr_pend["approval_id"], "op_hash": apr_pend["op_hash"],
                    }))
                self.assertEqual(cm.exception.code, c.APPROVAL_EXPIRED)
            finally:
                conn.close()

    def test_project_update_accepts_and_validates_plural_criteria(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr45-p31-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                        "VALUES ('prj_1', 'P', 'OldGoal', 'active', 'usr_1', 'direct', ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO acceptance_criteria (id, project_id, ordinal, text, created_at) "
                        "VALUES ('crit_0', 'prj_1', 1, 'C0', ?);",
                        (now,),
                    )

                with self.assertRaises(MeshError) as cm1:
                    handle_project_update(_user_ctx(conn, home, "project.update", {
                        "project_id": "prj_1", "goal": "Updated", "criteria": ["   "],
                    }))
                self.assertEqual(cm1.exception.code, c.INVALID_ARGUMENT)
                self.assertIn("Each acceptance criterion", cm1.exception.message)

                with self.assertRaises(MeshError) as cm2:
                    handle_project_update(_user_ctx(conn, home, "project.update", {
                        "project_id": "prj_1", "criteria": ["ok", "   "],
                    }))
                self.assertEqual(cm2.exception.code, c.INVALID_ARGUMENT)
                self.assertIn("Each acceptance criterion", cm2.exception.message)

                with transaction(conn):
                    res = handle_project_update(_user_ctx(conn, home, "project.update", {
                        "project_id": "prj_1", "criteria": ["NewC1", "NewC2"],
                    }))
                self.assertEqual(res.data["id"], "prj_1")
                crits = [
                    r["text"]
                    for r in conn.execute(
                        "SELECT text FROM acceptance_criteria WHERE project_id = 'prj_1' ORDER BY ordinal ASC;"
                    ).fetchall()
                ]
                self.assertEqual(crits, ["C0", "NewC1", "NewC2"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
