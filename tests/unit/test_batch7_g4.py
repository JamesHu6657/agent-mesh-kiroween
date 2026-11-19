"""Focused unit regression tests for batch7 g4 findings."""

import asyncio
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.cli.render import render_text
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.clock import now_iso_utc, parse_utc
from mesh.core.db import init_db, transaction
from mesh.core.ids import generate_id
from mesh.core.leases import get_lease_ttl_s
from mesh.core.settings import (
    get_agent_ack_timeout_s,
    get_default_settings,
    is_testing_mode,
    set_agent_ack_timeout_s,
    validate_ack_timeout,
    validate_setting,
)
from mesh.daemon import merge as merge_mod
from mesh.daemon import scheduler as scheduler_mod
from mesh.daemon.handlers.base import Effect, HandlerContext
from mesh.daemon.handlers.dep import handle_dep_add, handle_dep_remove
from mesh.daemon.handlers.project import (
    _prepare_repo_project,
    handle_intake_submit,
    handle_project_update,
)
from mesh.daemon.handlers.run import (
    _prune_skip_reason,
    _remove_worktree_and_branch,
    handle_run_release,
    handle_workspace_delete,
)
from mesh.daemon.server import DaemonServer, _lookup_live_idempotency
from mesh.errors import MeshError


def _setup_home_and_db(tmp: str):
    home = os.path.join(tmp, "home")
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(home, "workspaces"), mode=0o700, exist_ok=True)
    conn = init_db(os.path.join(home, "mesh.db"))
    with transaction(conn):
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', '2026-01-01T00:00:00.000Z');"
        )
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) "
            "VALUES ('agt_lead', 'agent', 'lead1', '2026-01-01T00:00:00.000Z');"
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at) "
            "VALUES ('agt_lead', 'none', 'inbox', 1, '[\"intake\"]', 1, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');"
        )
        conn.execute(
            "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
            "VALUES ('prj_1', 'P1', 'G1', 'active', 'usr_1', 'user', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');"
        )
        conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('la_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', '2026-01-01T00:00:00.000Z');"
        )
    return home, conn


def _user_ctx(conn: sqlite3.Connection, home: str, cmd: str, args: dict, *, dry_run: bool = False, testing_mode: bool = False) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal={"id": "usr_1", "kind": "user", "name": "user"},
        actor_kind="user",
        via="direct",
        request_id="req_test",
        home=home,
        cmd=cmd,
        args=args,
        dry_run=dry_run,
        testing_mode=testing_mode,
    )


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)


class TestBatch7G4Unit(unittest.TestCase):
    """Focused unit tests for batch7 g4 findings."""

    def test_f1_remove_worktree_and_branch_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                integ_dir = os.path.join(home, "workspaces", "prj_1", "integration")
                tasks_dir = os.path.join(home, "workspaces", "prj_1", "tasks")
                os.makedirs(integ_dir)
                os.makedirs(tasks_dir)
                with open(os.path.join(integ_dir, "keep.txt"), "w") as f:
                    f.write("precious\n")

                ws_path = os.path.join(tasks_dir, "tsk_1")
                os.symlink(integ_dir, ws_path)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_1', 'prj_1', 'T1', 'S1', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_1', 'usr_1', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');",
                        (ws_path,),
                    )
                task_row = conn.execute("SELECT * FROM tasks WHERE id = 'tsk_1';").fetchone()
                ctx = _user_ctx(conn, home, "workspace.prune", {})
                with mock.patch("mesh.core.gitops.run_git") as m_git:
                    reason = _remove_worktree_and_branch(ctx, task_row, integ_dir)
                self.assertEqual(reason, "outside_workspaces_root")
                m_git.assert_not_called()
                self.assertTrue(os.path.islink(ws_path))
                self.assertTrue(os.path.exists(os.path.join(integ_dir, "keep.txt")))

                # Also verify symlinked tasks/ parent dir is rejected
                os.unlink(ws_path)
                shutil.rmtree(tasks_dir)
                os.symlink(integ_dir, tasks_dir)
                reason2 = _remove_worktree_and_branch(ctx, task_row, integ_dir)
                self.assertEqual(reason2, "outside_workspaces_root")
            finally:
                conn.close()

    def test_f2_abort_and_verify_cleans_only_new_untracked_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            outside = os.path.join(tmp, "outside")
            os.makedirs(repo)
            os.makedirs(outside)
            with open(os.path.join(outside, "keep.txt"), "w") as f:
                f.write("keep\n")

            _git(repo, "init", "-b", "main")
            with open(os.path.join(repo, "f.txt"), "w") as f:
                f.write("base\n")
            _git(repo, "add", "f.txt")
            _git(repo, "commit", "-m", "base")
            pre_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

            _git(repo, "checkout", "-b", "feat")
            with open(os.path.join(repo, "f.txt"), "w") as f:
                f.write("feat\n")
            _git(repo, "commit", "-am", "feat")

            _git(repo, "checkout", "main")
            with open(os.path.join(repo, "f.txt"), "w") as f:
                f.write("main\n")
            _git(repo, "commit", "-am", "main")
            pre_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

            # Trigger conflicting merge
            cp = subprocess.run(["git", "merge", "--no-commit", "--no-ff", "feat"], cwd=repo, capture_output=True)
            self.assertNotEqual(cp.returncode, 0)

            pre_untracked = set(merge_mod._untracked_paths(repo))
            self.assertEqual(pre_untracked, set())

            # Simulate test_command creating new untracked files and a symlink to outside
            with open(os.path.join(repo, ".coverage"), "w") as f:
                f.write("cov")
            os.makedirs(os.path.join(repo, "reports"))
            with open(os.path.join(repo, "reports", "junit.xml"), "w") as f:
                f.write("<xml/>")
            os.symlink(outside, os.path.join(repo, "reports", "escape_link"))

            res = merge_mod._abort_and_verify(repo, pre_head, pre_untracked=pre_untracked)
            self.assertTrue(res["restored"], res)
            self.assertFalse(os.path.exists(os.path.join(repo, ".coverage")))
            self.assertFalse(os.path.exists(os.path.join(repo, "reports")))
            self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")))

            # Pre-existing untracked files are preserved across _cleanup_new_untracked
            with open(os.path.join(repo, "preexisting.txt"), "w") as f:
                f.write("stay")
            pre2 = set(merge_mod._untracked_paths(repo))
            with open(os.path.join(repo, "new_artifact.log"), "w") as f:
                f.write("gone")
            merge_mod._cleanup_new_untracked(repo, pre2)
            self.assertTrue(os.path.exists(os.path.join(repo, "preexisting.txt")))
            self.assertFalse(os.path.exists(os.path.join(repo, "new_artifact.log")))

    def test_f5_prune_merge_lease_non_tx_delete_and_dry_run_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                ws_dir = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_1")
                os.makedirs(ws_dir)
                with open(os.path.join(ws_dir, "file.txt"), "w") as f:
                    f.write("hello\n")

                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_1', 'prj_1', 'T1', 'S1', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_1', 'usr_1', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');",
                        (ws_dir,),
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, boot_id, run_dir, argv_redacted_json, started_at) "
                        "VALUES ('run_merge', 'merge_test', 'tsk_1', 'prj_1', 'agt_lead', 1, 1, 'none', 'running', 'boot_1', ?, '[]', '2026-01-01T00:00:00.000Z');",
                        (os.path.join(home, "runs", "run_merge"),),
                    )
                    conn.execute(
                        "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
                        "VALUES ('merge:prj_1', 'run_merge', 'meshd', 'boot_1', 1, '2026-01-01T00:00:00.000Z', '2099-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');"
                    )
                trow = conn.execute("SELECT * FROM tasks WHERE id = 'tsk_1';").fetchone()
                ctx_prune = _user_ctx(conn, home, "workspace.prune", {})
                self.assertEqual(_prune_skip_reason(ctx_prune, trow), "merge_in_progress")
                with transaction(conn):
                    conn.execute("DELETE FROM leases WHERE key = 'merge:prj_1';")

                # Dry-run workspace.delete returns populated transitions & events without touching disk
                ctx_dry = _user_ctx(conn, home, "workspace.delete", {"task_id": "tsk_1"}, dry_run=True)
                res_dry = handle_workspace_delete(ctx_dry)
                self.assertEqual(len(res_dry.transitions), 1)
                self.assertEqual(res_dry.transitions[0]["action"], "delete")
                self.assertEqual(res_dry.events, ["workspace.deleted"])
                self.assertTrue(os.path.isdir(ws_dir))

                # Real workspace.delete through handle_client deletes off-TX and off-loop;
                # validation failure preserves disk and delete OSError commits no audit event.
                srv = DaemonServer(home, testing_mode=True)
                srv.db_conn = conn
                real_rmtree = shutil.rmtree
                in_tx_during_rmtree = []

                def _spy_rmtree(path, *a, **kw):
                    in_tx_during_rmtree.append(conn.in_transaction)
                    return real_rmtree(path, *a, **kw)

                async def _drive_delete(task_id_arg: str) -> dict:
                    reader_s = asyncio.StreamReader()
                    req = {
                        "v": 1,
                        "cmd": "workspace.delete",
                        "args": {"task_id": task_id_arg},
                        "request_id": f"req_{task_id_arg}",
                    }
                    reader_s.feed_data((json.dumps(req) + "\n").encode("utf-8"))
                    reader_s.feed_eof()
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
                         mock.patch.object(srv, "authenticate_and_authorize", return_value=({"id": "usr_1", "kind": "user", "name": "user"}, "user", "direct", None)):
                        await srv.handle_client(reader_s, _W())
                    return json.loads(buf.decode("utf-8").splitlines()[0])

                with mock.patch("shutil.rmtree", side_effect=_spy_rmtree):
                    resp_del = asyncio.run(_drive_delete("tsk_1"))
                self.assertTrue(resp_del["ok"], resp_del)
                self.assertTrue(resp_del["data"]["deleted"])
                self.assertEqual(in_tx_during_rmtree, [False])
                self.assertFalse(os.path.exists(ws_dir))

                ws_dir2 = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_2")
                os.makedirs(ws_dir2)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_2', 'prj_1', 'T2', 'S2', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_2', 'usr_1', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');",
                        (ws_dir2,),
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, boot_id, run_dir, argv_redacted_json, started_at) "
                        "VALUES ('run_t2', 'task', 'tsk_2', 'prj_1', 'agt_lead', 1, 1, 'none', 'running', 'boot_1', ?, '[]', '2026-01-01T00:00:00.000Z');",
                        (os.path.join(home, "runs", "run_t2"),),
                    )
                    conn.execute(
                        "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
                        "VALUES ('task:tsk_2', 'run_t2', 'meshd', 'boot_1', 1, '2026-01-01T00:00:00.000Z', '2099-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');"
                    )
                resp_lease = asyncio.run(_drive_delete("tsk_2"))
                self.assertFalse(resp_lease["ok"], resp_lease)
                self.assertTrue(os.path.isdir(ws_dir2))
                with transaction(conn):
                    conn.execute("DELETE FROM leases WHERE key = 'task:tsk_2';")

                with mock.patch("shutil.rmtree", side_effect=OSError("disk_boom")):
                    resp_err = asyncio.run(_drive_delete("tsk_2"))
                self.assertFalse(resp_err["ok"], resp_err)
                self.assertTrue(os.path.isdir(ws_dir2))
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = 'tsk_2';"
                    ).fetchone()[0],
                    0,
                )
                self.assertFalse(conn.in_transaction)

                # Dry-run run.release populates transitions, events, and seen_release_forced effect
                with transaction(conn):
                    conn.execute(
                        "UPDATE tasks SET status = 'cancelling' WHERE id = 'tsk_1';"
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, boot_id, run_dir, argv_redacted_json, started_at) "
                        "VALUES ('run_1', 'task', 'tsk_1', 'prj_1', 'agt_lead', 1, 1, 'none', 'running', 'boot_1', ?, '[]', '2026-01-01T00:00:00.000Z');",
                        (os.path.join(home, "runs", "run_1"),),
                    )
                ctx_rel = _user_ctx(conn, home, "run.release", {"run_id": "run_1", "force_seen_unusable": True}, dry_run=True)
                with mock.patch("mesh.daemon.supervisor.seen_held", return_value=True):
                    res_rel = handle_run_release(ctx_rel)
                self.assertEqual([t["entity"] for t in res_rel.transitions], ["run", "task"])
                self.assertEqual(res_rel.transitions[0]["to"], "lost")
                self.assertEqual(res_rel.transitions[1]["to"], "cancelled")
                self.assertIn("run.seen_release_forced", res_rel.events)
                self.assertEqual([e.kind for e in ctx_rel.effects], ["seen_release_forced"])
            finally:
                conn.close()

    def test_f6_workspaces_root_and_repo_overlap_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                repo = os.path.join(tmp, "external_repo")
                os.makedirs(repo)
                with transaction(conn):
                    conn.execute("UPDATE projects SET repo_path = ? WHERE id = 'prj_1';", (repo,))

                # validate_setting rejects $MESH_HOME, subdirs != workspaces, parents, and registered repos
                for bad in (
                    home,
                    os.path.join(home, "runs"),
                    os.path.dirname(home),
                    repo,
                    os.path.dirname(repo),
                    os.path.join(repo, "subdir"),
                ):
                    with self.subTest(bad=bad):
                        os.makedirs(bad, exist_ok=True)
                        with self.assertRaises(MeshError) as cm:
                            validate_setting("workspaces.root", bad, home, db_conn=conn)
                        self.assertEqual(cm.exception.code, c.INVALID_SETTING)
                        self.assertIn("overlap", cm.exception.message.lower())

                # Default $MESH_HOME/workspaces and disjoint dir are accepted
                self.assertEqual(
                    validate_setting("workspaces.root", os.path.join(home, "workspaces"), home, db_conn=conn),
                    os.path.realpath(os.path.join(home, "workspaces")),
                )
                disjoint = os.path.join(tmp, "other_ws")
                os.makedirs(disjoint, mode=0o700)
                self.assertEqual(
                    validate_setting("workspaces.root", disjoint, home, db_conn=conn),
                    os.path.realpath(disjoint),
                )

                # _prepare_repo_project rejects repos overlapping $MESH_HOME or workspaces.root
                for bad_repo in (
                    home,
                    os.path.join(home, "subrepo"),
                    os.path.join(home, "workspaces"),
                    os.path.join(home, "workspaces", "nested"),
                    os.path.dirname(home),
                ):
                    with self.subTest(bad_repo=bad_repo):
                        os.makedirs(bad_repo, exist_ok=True)
                        ctx = _user_ctx(
                            conn,
                            home,
                            "intake.submit",
                            {"repo": bad_repo, "base_branch": "main", "test_command": "none"},
                        )
                        with self.assertRaises(MeshError) as cm:
                            _prepare_repo_project(ctx)
                        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
                        self.assertEqual(cm.exception.details.get("param"), "repo")
            finally:
                conn.close()

    def test_f7_testing_mode_explicit_overrides_os_environ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                # Seed sub-floor production settings in DB
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES "
                        "('lease.ttl_s', '1', '2026-01-01T00:00:00.000Z', 'usr_1'), "
                        "('approval.ttl_seconds', '1', '2026-01-01T00:00:00.000Z', 'usr_1'), "
                        "('inbox.ack_timeout_s', '1', '2026-01-01T00:00:00.000Z', 'usr_1') "
                        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json;"
                    )
                    set_agent_ack_timeout_s(conn, "agt_lead", 1, "2026-01-01T00:00:00.000Z", "usr_1")

                # MESH_TESTING=1 in env must NOT leak into testing_mode=False
                with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                    self.assertFalse(is_testing_mode(False))
                    self.assertEqual(get_default_settings(home, testing_mode=False)["adapter.evidence_mode"], "warn")
                    with self.assertRaises(MeshError):
                        validate_setting("lease.ttl_s", 1, home, testing_mode=False)
                    with self.assertRaises(MeshError):
                        validate_setting("lead.wake_backoff_s", [1, 1, 1], home, testing_mode=False)
                    with self.assertRaises(MeshError):
                        validate_ack_timeout(1, testing_mode=False)

                    # Read paths clamp DB-seeded sub-floor values when testing_mode=False
                    self.assertEqual(get_lease_ttl_s(conn, testing_mode=False), 10)
                    self.assertEqual(scheduler_mod.get_inbox_ack_timeout_s(conn, testing_mode=False), 10)
                    self.assertEqual(get_agent_ack_timeout_s(conn, "agt_lead", testing_mode=False), 10)
                    with transaction(conn):
                        create_or_get_pending_approval(
                            conn, "delete", "op.test", "agt_lead", {"k": "v"}, "summary", testing_mode=False
                        )
                    arow = conn.execute(
                        "SELECT requested_at, expires_at FROM approvals WHERE op_type = 'op.test';"
                    ).fetchone()
                    ttl = (parse_utc(arow["expires_at"]) - parse_utc(arow["requested_at"])).total_seconds()
                    self.assertGreaterEqual(ttl, 60)

                # Live DaemonServer(home, testing_mode=False) rejects testing settings & 1s ack_timeout even with MESH_TESTING=1
                async def _check_server_dispatch() -> None:
                    srv = DaemonServer(home, testing_mode=False)
                    srv.db_conn = conn

                    async def _call(req: dict) -> dict:
                        reader_s = asyncio.StreamReader()
                        reader_s.feed_data((json.dumps(req) + "\n").encode("utf-8"))
                        reader_s.feed_eof()
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
                             mock.patch.object(srv, "authenticate_and_authorize", return_value=({"id": "usr_1", "kind": "user", "name": "user"}, "user", "direct", None)):
                            await srv.handle_client(reader_s, _W())
                        return json.loads(buf.decode("utf-8").splitlines()[0])

                    with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                        resp = await _call({"v": 1, "cmd": "config.set", "args": {"key": "lease.ttl_s", "value": 1}, "request_id": "req_f7_1"})
                        self.assertFalse(resp["ok"], resp)
                        self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)

                        resp_list = await _call({"v": 1, "cmd": "config.list", "args": {}, "request_id": "req_f7_2"})
                        self.assertTrue(resp_list["ok"], resp_list)
                        settings_map = {it["key"]: it["value"] for it in resp_list["data"]["items"]}
                        self.assertEqual(settings_map["adapter.evidence_mode"], "warn")

                        resp_agt = await _call({
                            "v": 1,
                            "cmd": "agent.create",
                            "args": {"name": "a1s", "adapter": "none", "delivery": "inbox", "ack_timeout": "1s"},
                            "request_id": "req_f7_3",
                        })
                        self.assertFalse(resp_agt["ok"], resp_agt)
                        self.assertEqual(resp_agt["error"]["code"], c.INVALID_ARGUMENT)

                asyncio.run(_check_server_dispatch())

                # Conversely, explicit testing_mode=True overrides missing MESH_TESTING
                env_no_testing = {k: v for k, v in os.environ.items() if k != "MESH_TESTING"}
                with mock.patch.dict(os.environ, env_no_testing, clear=True):
                    self.assertTrue(is_testing_mode(True))
                    self.assertEqual(get_default_settings(home, testing_mode=True)["adapter.evidence_mode"], "strict")
                    self.assertEqual(validate_setting("lease.ttl_s", 1, home, testing_mode=True), 1)
                    self.assertEqual(validate_ack_timeout(1, testing_mode=True), 1)
                    self.assertEqual(get_lease_ttl_s(conn, testing_mode=True), 1)
                    self.assertEqual(scheduler_mod.get_inbox_ack_timeout_s(conn, testing_mode=True), 1)
                    self.assertEqual(get_agent_ack_timeout_s(conn, "agt_lead", testing_mode=True), 1)
                    with transaction(conn):
                        create_or_get_pending_approval(
                            conn, "delete", "op.test2", "agt_lead", {"k": "v2"}, "summary2", testing_mode=True
                        )
                    arow2 = conn.execute(
                        "SELECT requested_at, expires_at FROM approvals WHERE op_type = 'op.test2';"
                    ).fetchone()
                    ttl2 = (parse_utc(arow2["expires_at"]) - parse_utc(arow2["requested_at"])).total_seconds()
                    self.assertLessEqual(ttl2, 5)
            finally:
                conn.close()

    def test_f9_sweep_expired_idempotency_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now_ts = 1_800_000_000.0
                old_ts = now_ts - c.IDEMPOTENCY_TTL_S - 60
                fresh_ts = now_ts - 60
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO idempotency (principal_id, key, cmd, request_hash, response_json, exit_code, created_at) "
                        "VALUES ('usr_1', 'k_old', 'task.create', 'h1', '{\"ok\":true}', 0, ?), "
                        "('usr_1', 'k_new', 'task.create', 'h2', '{\"ok\":true}', 0, ?);",
                        (now_iso_utc(old_ts), now_iso_utc(fresh_ts)),
                    )
                self.assertIsNone(_lookup_live_idempotency(conn, "usr_1", "k_old", now_ts=now_ts))
                self.assertIsNotNone(_lookup_live_idempotency(conn, "usr_1", "k_new", now_ts=now_ts))

                pruned = scheduler_mod.sweep_expired_idempotency(conn, home, now_ts=now_ts)
                self.assertEqual(pruned, ["idempotency:1"])
                rows = [r["key"] for r in conn.execute("SELECT key FROM idempotency;").fetchall()]
                self.assertEqual(rows, ["k_new"])
            finally:
                conn.close()

    def test_f10_dep_noop_and_whitespace_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                with transaction(conn):
                    for tid in ("tsk_a", "tsk_b"):
                        conn.execute(
                            "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at) "
                            "VALUES (?, 'prj_1', 'T', 'S', 'general', 'assigned', 'dev', 600, 3, 'usr_1', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');",
                            (tid,),
                        )

                # First dep.add emits dep.added
                with transaction(conn):
                    res1 = handle_dep_add(_user_ctx(conn, home, "dep.add", {"task_id": "tsk_b", "on": "tsk_a"}))
                self.assertEqual(res1.events, ["dep.added"])

                # Duplicate dep.add emits no event and records no row in events
                before_cnt = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'dep.added';").fetchone()[0]
                with transaction(conn):
                    res2 = handle_dep_add(_user_ctx(conn, home, "dep.add", {"task_id": "tsk_b", "on": "tsk_a"}))
                after_cnt = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'dep.added';").fetchone()[0]
                self.assertEqual(res2.events, [])
                self.assertEqual(before_cnt, after_cnt)

                # dep.remove on nonexistent --on raises NOT_FOUND
                with self.assertRaises(MeshError) as cm:
                    handle_dep_remove(_user_ctx(conn, home, "dep.remove", {"task_id": "tsk_b", "on": "tsk_missing"}))
                self.assertEqual(cm.exception.code, c.NOT_FOUND)

                # First dep.remove emits dep.removed; second emits zero events
                with transaction(conn):
                    rem1 = handle_dep_remove(_user_ctx(conn, home, "dep.remove", {"task_id": "tsk_b", "on": "tsk_a"}))
                self.assertEqual(rem1.events, ["dep.removed"])
                rem_cnt_1 = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'dep.removed';").fetchone()[0]
                with transaction(conn):
                    rem2 = handle_dep_remove(_user_ctx(conn, home, "dep.remove", {"task_id": "tsk_b", "on": "tsk_a"}))
                rem_cnt_2 = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'dep.removed';").fetchone()[0]
                self.assertEqual(rem2.events, [])
                self.assertEqual(rem_cnt_1, rem_cnt_2)

                # Whitespace rejection in intake.submit (followup + new) and project.update
                for args in (
                    {"project_id": "prj_1", "message": "   \n\t  "},
                    {"title": "   ", "goal": "G", "criteria": ["c1"]},
                    {"title": "T", "goal": " \t ", "criteria": ["c1"]},
                    {"title": "T", "goal": "G", "criteria": ["c1", "   "]},
                ):
                    with self.assertRaises(MeshError) as cm:
                        handle_intake_submit(_user_ctx(conn, home, "intake.submit", args))
                    self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)

                for uargs, expected_msg in (
                    ({"project_id": "prj_1", "goal": "  \n "}, "Field 'goal' must be a non-empty string"),
                    ({"project_id": "prj_1", "criterion": ["ok", "   "]}, "Each acceptance criterion must be a non-empty string"),
                    ({"project_id": "prj_1", "criteria": ["ok", "   "]}, "Each acceptance criterion must be a non-empty string"),
                ):
                    with self.assertRaises(MeshError) as cm:
                        handle_project_update(_user_ctx(conn, home, "project.update", uargs))
                    self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
                    self.assertIn(expected_msg, cm.exception.message)
            finally:
                conn.close()

    def test_f11_render_text_full_fields_for_approvals_workspaces_and_tokens(self) -> None:
        long_cmd = "mesh approval approve apr_1234567890 --challenge abcdef --response deadbeef1234567890"
        apr_text = render_text({
            "items": [{
                "id": "apr_1234567890",
                "status": "pending",
                "category": "delete",
                "summary": "Delete a very long workspace path without table truncation",
                "expires_at": "2026-11-16T00:00:00.000Z",
                "decision_channel": "direct",
                "approve": {"command": long_cmd},
            }]
        })
        self.assertIn(long_cmd, apr_text)
        self.assertIn("2026-11-16T00:00:00.000Z", apr_text)

        long_path = "/tmp/mh-very-long-home/workspaces/prj_abcdef123456/tasks/tsk_abcdef123456"
        ws_text = render_text({
            "root": "/tmp/mh-very-long-home/workspaces",
            "workspaces": [{
                "task_id": "tsk_abcdef123456",
                "project_id": "prj_abcdef123456",
                "status": "merged",
                "path": long_path,
                "branch": "mesh/prj_abcdef123456/tsk_abcdef123456",
            }],
        })
        self.assertIn(long_path, ws_text)
        self.assertIn("mesh/prj_abcdef123456/tsk_abcdef123456", ws_text)

        tok_text = render_text({
            "items": [{
                "id": "cred_12345",
                "lookup": "L00KUP1234",
                "kind": "agent_token",
                "created_at": "2026-01-01T00:00:00.000Z",
                "expires_at": "2027-01-01T00:00:00.000Z",
                "revoked_at": "2026-02-01T00:00:00.000Z",
            }]
        })
        self.assertIn("LOOKUP", tok_text)
        self.assertIn("L00KUP1234", tok_text)
        self.assertIn("EXPIRES_AT", tok_text)
        self.assertIn("REVOKED_AT", tok_text)


if __name__ == "__main__":
    unittest.main()
