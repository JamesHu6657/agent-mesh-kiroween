"""Unit regression tests for PR #45 round-2 review findings (P1-1, P1-2, P2, P3)."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.clock import now_iso_utc, parse_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.invariants import check_i7_event_hash_chain, check_i8_cancelling_and_leases
from mesh.core.settings import validate_setting
from mesh.daemon import lead_turns, merge, recovery, scheduler, supervisor
from mesh.daemon.handlers import run as run_handlers
from mesh.daemon.handlers.approval import handle_approval_approve
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.config import handle_config_set
from mesh.daemon.handlers.run import handle_run_release, handle_workspace_delete
from mesh.daemon.server import DaemonServer
from mesh.errors import MeshError
from tests.gitfixture import GitFixture

# Calls recovery.recover and merge.run_merge_pipeline scanning real /proc.
MESH_PROC_LOCK = "shared"


def _setup_home_and_db(tmp: str):
    home = os.path.join(tmp, "home")
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(home, "workspaces"), mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(home, "run"), mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(home, "runs"), mode=0o700, exist_ok=True)
    conn = init_db(os.path.join(home, "mesh.db"))
    now = "2026-01-01T00:00:00.000Z"
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
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at) "
            "VALUES ('agt_lead', 'none', 'inbox', 1, '[\"intake\"]', 2, ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_work', 'agent', 'worker1', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at) "
            "VALUES ('agt_work', 'none', 'inbox', 0, '[\"relay\"]', 2, ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
            "VALUES ('prj_1', 'P', 'G', 'active', 'usr_1', 'direct', ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
            (now,),
        )
    return home, conn


def _ctx(
    conn: sqlite3.Connection,
    home: str,
    cmd: str,
    args: dict,
    *,
    principal_id: str = "usr_1",
    actor_kind: str = "user",
    via: str = "direct",
    k_user=None,
    approval: str = None,
    testing_mode: bool = True,
    dry_run: bool = False,
    request_id: str = "req_r2",
) -> HandlerContext:
    pname = "user" if actor_kind == "user" else ("lead1" if principal_id == "agt_lead" else "worker1")
    return HandlerContext(
        conn=conn,
        principal={"id": principal_id, "kind": actor_kind, "name": pname},
        actor_kind=actor_kind,
        via=via,
        request_id=request_id,
        home=home,
        cmd=cmd,
        args=args,
        dry_run=dry_run,
        k_user=k_user,
        testing_mode=testing_mode,
        approval=approval,
    )


async def _drive_server(
    srv: DaemonServer,
    cmd: str,
    args: dict,
    *,
    principal: dict = None,
    actor_kind: str = "user",
    via: str = "direct",
    k_user=None,
    approval: str = None,
    idem_key: str = "",
    request_id: str = "req_srv",
) -> dict:
    if principal is None:
        principal = {"id": "usr_1", "kind": "user", "name": "user"}
    reader = asyncio.StreamReader()
    req: dict = {"v": 1, "cmd": cmd, "args": args, "request_id": request_id}
    if approval:
        req["approval"] = approval
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
         mock.patch.object(srv, "authenticate_and_authorize", return_value=(principal, actor_kind, via, k_user)):
        await srv.handle_client(reader, _W())
    return json.loads(buf.decode("utf-8").splitlines()[0])


class TestP1_1WorkspaceDeletePruneReservation(unittest.TestCase):
    """P1-1: workspace.delete and workspace.prune must reserve atomically in TX1 before disk/git mutations."""

    def test_workspace_delete_reserves_approval_and_blocks_concurrent_mutations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2-p11-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                ws_acc = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_acc")
                os.makedirs(ws_acc, mode=0o700)
                with open(os.path.join(ws_acc, "uncommitted.py"), "w", encoding="utf-8") as f:
                    f.write("important = 42\n")
                ws_q = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_q")
                os.makedirs(ws_q, mode=0o700)

                k_user = b"\x02" * 32
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                        "timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_acc', 'prj_1', 'Acc', 'S', 'code', 'accepted', 'agt_work', 'dev', "
                        "600, 3, ?, 'mesh/prj_1/tsk_acc', 'agt_lead', ?, ?);",
                        (ws_acc, now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                        "timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_q', 'prj_1', 'Q', 'S', 'general', 'queued', 'agt_work', 'dev', "
                        "600, 3, NULL, NULL, 'agt_lead', ?, ?);",
                        (now, now),
                    )
                    apr = create_or_get_pending_approval(
                        conn=conn,
                        category="delete",
                        op="workspace.delete",
                        requested_by="agt_lead",
                        params={"task_id": "tsk_acc"},
                        summary="Delete tsk_acc workspace",
                        project_id="prj_1",
                        target_id="tsk_acc",
                        testing_mode=True,
                    )
                    handle_approval_approve(_ctx(
                        conn, home, "approval.approve",
                        {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                        k_user=k_user,
                    ))

                srv = DaemonServer(home, testing_mode=True)
                srv.db_conn = conn

                entered_phase2 = threading.Event()
                release_phase2 = threading.Event()
                orig_delete_sync = run_handlers._delete_workspace_dir_sync

                def _paused_delete(*a, **kw):
                    entered_phase2.set()
                    if not release_phase2.wait(timeout=5.0):
                        raise TimeoutError("test barrier timeout")
                    return orig_delete_sync(*a, **kw)

                async def _scenario() -> None:
                    with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_paused_delete):
                        del_task = asyncio.create_task(_drive_server(
                            srv,
                            "workspace.delete",
                            {"task_id": "tsk_acc"},
                            principal={"id": "agt_lead", "kind": "agent", "name": "lead1"},
                            actor_kind="agent",
                            via="token",
                            approval=apr["approval_id"],
                            idem_key="k-del-1",
                            request_id="req_del_1",
                        ))
                        while not entered_phase2.is_set():
                            await asyncio.sleep(0.01)

                        # 1. Cancelling the pinned approval must be rejected while delete is in progress.
                        r_can_apr = await _drive_server(
                            srv,
                            "approval.cancel",
                            {"approval_id": apr["approval_id"]},
                            k_user=k_user,
                            request_id="req_can_apr",
                        )
                        self.assertFalse(r_can_apr["ok"], r_can_apr)
                        self.assertEqual(r_can_apr["error"]["code"], c.CONCURRENCY_LIMIT, r_can_apr)

                        # 2. Same idempotency key while reserved returns CONCURRENCY_LIMIT; different args -> IDEMPOTENCY_KEY_REUSED.
                        r_dup = await _drive_server(
                            srv,
                            "workspace.delete",
                            {"task_id": "tsk_acc"},
                            principal={"id": "agt_lead", "kind": "agent", "name": "lead1"},
                            actor_kind="agent",
                            via="token",
                            approval=apr["approval_id"],
                            idem_key="k-del-1",
                            request_id="req_del_dup",
                        )
                        self.assertFalse(r_dup["ok"], r_dup)
                        self.assertEqual(r_dup["error"]["code"], c.CONCURRENCY_LIMIT, r_dup)

                        r_reused = await _drive_server(
                            srv,
                            "workspace.delete",
                            {"task_id": "tsk_other"},
                            principal={"id": "agt_lead", "kind": "agent", "name": "lead1"},
                            actor_kind="agent",
                            via="token",
                            approval=apr["approval_id"],
                            idem_key="k-del-1",
                            request_id="req_del_reused",
                        )
                        self.assertFalse(r_reused["ok"], r_reused)
                        self.assertEqual(r_reused["error"]["code"], c.IDEMPOTENCY_KEY_REUSED, r_reused)

                        # 3. Concurrent delete, task.merge, project.set_lead, project.cancel all blocked.
                        r_del2 = await _drive_server(
                            srv, "workspace.delete", {"task_id": "tsk_acc"}, k_user=k_user, request_id="req_del2"
                        )
                        self.assertFalse(r_del2["ok"], r_del2)
                        self.assertEqual(r_del2["error"]["code"], c.CONCURRENCY_LIMIT, r_del2)

                        r_mrg = await _drive_server(
                            srv, "task.merge", {"task_id": "tsk_acc"}, k_user=k_user, request_id="req_mrg"
                        )
                        self.assertFalse(r_mrg["ok"], r_mrg)
                        self.assertEqual(r_mrg["error"]["code"], c.CONCURRENCY_LIMIT, r_mrg)

                        r_lead = await _drive_server(
                            srv,
                            "project.set_lead",
                            {"project_id": "prj_1", "agent": "worker1", "reason": "swap"},
                            k_user=k_user,
                            request_id="req_set_lead",
                        )
                        self.assertFalse(r_lead["ok"], r_lead)
                        self.assertEqual(r_lead["error"]["code"], c.CONCURRENCY_LIMIT, r_lead)

                        r_pcan = await _drive_server(
                            srv, "project.cancel", {"project_id": "prj_1"}, k_user=k_user, request_id="req_pcan"
                        )
                        self.assertFalse(r_pcan["ok"], r_pcan)
                        self.assertEqual(r_pcan["error"]["code"], c.CONCURRENCY_LIMIT, r_pcan)

                        # Release Phase 2 and let TX2 finish.
                        release_phase2.set()
                        r_del = await del_task
                        self.assertTrue(r_del["ok"], r_del)
                        self.assertTrue(r_del["data"]["deleted"], r_del)

                asyncio.run(_scenario())

                self.assertFalse(os.path.exists(ws_acc))
                arow = conn.execute("SELECT status, consumed_at FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()
                self.assertEqual(arow["status"], "consumed")
                self.assertIsNotNone(arow["consumed_at"])
                ev_cnt = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = 'tsk_acc';"
                ).fetchone()[0]
                self.assertEqual(ev_cnt, 1)
                res_cnt = conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0]
                self.assertEqual(res_cnt, 0)
            finally:
                conn.close()

    def test_workspace_delete_pinned_approval_survives_expiry_and_policy_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2-p11exp-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                ws_acc = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_exp")
                os.makedirs(ws_acc, mode=0o700)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                        "timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_exp', 'prj_1', 'Exp', 'S', 'code', 'accepted', 'agt_work', 'dev', "
                        "600, 3, ?, 'mesh/prj_1/tsk_exp', 'agt_lead', ?, ?);",
                        (ws_acc, now, now),
                    )
                    apr = create_or_get_pending_approval(
                        conn=conn,
                        category="delete",
                        op="workspace.delete",
                        requested_by="agt_lead",
                        params={"task_id": "tsk_exp"},
                        summary="Delete tsk_exp workspace",
                        project_id="prj_1",
                        target_id="tsk_exp",
                        testing_mode=True,
                    )
                    # Relayed approval, initially allowed under default direct_or_relayed.
                    handle_approval_approve(HandlerContext(
                        conn=conn,
                        principal={"id": "agt_work", "kind": "agent", "name": "worker1", "capabilities": ["relay"]},
                        actor_kind="agent",
                        via="relayed",
                        request_id="req_rel_apr",
                        home=home,
                        cmd="approval.approve",
                        args={"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                        dry_run=False,
                        testing_mode=True,
                        user_statement="Human approved deletion",
                    ))

                srv = DaemonServer(home, testing_mode=True)
                srv.db_conn = conn
                entered = threading.Event()
                release = threading.Event()
                orig_delete_sync = run_handlers._delete_workspace_dir_sync

                def _paused_delete(*a, **kw):
                    entered.set()
                    release.wait(timeout=5.0)
                    return orig_delete_sync(*a, **kw)

                async def _scenario() -> dict:
                    with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_paused_delete):
                        task = asyncio.create_task(_drive_server(
                            srv,
                            "workspace.delete",
                            {"task_id": "tsk_exp"},
                            principal={"id": "agt_lead", "kind": "agent", "name": "lead1"},
                            actor_kind="agent",
                            via="token",
                            approval=apr["approval_id"],
                            request_id="req_exp_del",
                        ))
                        while not entered.is_set():
                            await asyncio.sleep(0.01)

                        # Simulate expiry passing + policy tightening while disk delete is in flight.
                        with transaction(conn):
                            conn.execute(
                                "UPDATE approvals SET expires_at = '2020-01-01T00:00:00.000Z' WHERE id = ?;",
                                (apr["approval_id"],),
                            )
                            conn.execute(
                                "INSERT INTO settings (key, value_json, updated_at, updated_by) "
                                "VALUES ('approval.policy.delete', '\"direct_only\"', ?, 'usr_1') "
                                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json;",
                                (now_iso_utc(),),
                            )
                        # Sweeper must NOT expire the pinned approval!
                        self.assertEqual(scheduler.sweep_expired_approvals(conn, home), [])

                        release.set()
                        return await task

                resp = asyncio.run(_scenario())
                self.assertTrue(resp["ok"], resp)
                self.assertTrue(resp["data"]["deleted"], resp)
                self.assertFalse(os.path.exists(ws_acc))
                arow = conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()
                self.assertEqual(arow["status"], "consumed")
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = 'tsk_exp';").fetchone()[0],
                    1,
                )
            finally:
                conn.close()

    def test_workspace_delete_blocked_for_queued_task_start_and_oserror_releases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2-p11q-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                ws_q = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_q2")
                os.makedirs(ws_q, mode=0o700)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                        "timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_q2', 'prj_1', 'Q2', 'S', 'general', 'queued', 'agt_work', 'dev', "
                        "600, 3, ?, 'mesh/prj_1/tsk_q2', 'agt_lead', ?, ?);",
                        (ws_q, now, now),
                    )

                srv = DaemonServer(home, testing_mode=True)
                srv.db_conn = conn
                entered = threading.Event()
                release = threading.Event()
                orig_delete_sync = run_handlers._delete_workspace_dir_sync

                def _paused_fail(*a, **kw):
                    entered.set()
                    release.wait(timeout=5.0)
                    raise PermissionError(13, "denied")

                async def _scenario() -> dict:
                    with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_paused_fail):
                        t = asyncio.create_task(_drive_server(
                            srv, "workspace.delete", {"task_id": "tsk_q2"}, idem_key="k-q2", request_id="req_q2"
                        ))
                        while not entered.is_set():
                            await asyncio.sleep(0.01)

                        # task.start and task.cancel blocked while reserved!
                        r_st = await _drive_server(
                            srv,
                            "task.start",
                            {"task_id": "tsk_q2"},
                            principal={"id": "agt_work", "kind": "agent", "name": "worker1"},
                            actor_kind="agent",
                            via="token",
                            request_id="req_st",
                        )
                        self.assertFalse(r_st["ok"], r_st)
                        self.assertEqual(r_st["error"]["code"], c.CONCURRENCY_LIMIT, r_st)

                        r_can = await _drive_server(
                            srv, "task.cancel", {"task_id": "tsk_q2", "reason": "stop"}, request_id="req_can"
                        )
                        self.assertFalse(r_can["ok"], r_can)
                        self.assertEqual(r_can["error"]["code"], c.CONCURRENCY_LIMIT, r_can)

                        release.set()
                        return await t

                resp_err = asyncio.run(_scenario())
                self.assertFalse(resp_err["ok"], resp_err)
                self.assertTrue(os.path.isdir(ws_q))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted';").fetchone()[0], 0)

                # Now retry without failure using the same idempotency key -> succeeds cleanly!
                resp_ok = asyncio.run(_drive_server(
                    srv, "workspace.delete", {"task_id": "tsk_q2"}, idem_key="k-q2", request_id="req_q2_retry"
                ))
                self.assertTrue(resp_ok["ok"], resp_ok)
                self.assertTrue(resp_ok["data"]["deleted"])
                self.assertFalse(os.path.exists(ws_q))
            finally:
                conn.close()

    def test_workspace_prune_reserves_rechecks_authz_and_blocks_merge(self) -> None:
        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        repo = fixture.make_k0("PrnUnit")
        with tempfile.TemporaryDirectory(prefix="mh-r2-p11prn-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            fixture.allow_root(tmp)
            fixture.allow_root(home)
            try:
                now = now_iso_utc()
                fixture.git(repo, "branch", "mesh/prj_1/integration", "main")
                integ = os.path.join(home, "workspaces", "prj_1", "integration")
                os.makedirs(os.path.dirname(integ), mode=0o700, exist_ok=True)
                fixture.git(repo, "worktree", "add", integ, "mesh/prj_1/integration")

                ws_m = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_m")
                fixture.git(repo, "worktree", "add", "-b", "mesh/prj_1/tsk_m", ws_m, "mesh/prj_1/integration")
                with open(os.path.join(ws_m, "done.txt"), "w", encoding="utf-8") as f:
                    f.write("done\n")
                fixture.git(ws_m, "add", "done.txt")
                fixture.git(ws_m, "commit", "-m", "done")
                base_sha = fixture.head(repo, "mesh/prj_1/integration")
                fixture.git(integ, "merge", "--no-ff", "mesh/prj_1/tsk_m", "-m", "Merge tsk_m")
                mc = fixture.head(integ)

                ws_a = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_a")
                fixture.git(repo, "worktree", "add", "-b", "mesh/prj_1/tsk_a", ws_a, mc)
                with open(os.path.join(ws_a, "next.txt"), "w", encoding="utf-8") as f:
                    f.write("next\n")
                fixture.git(ws_a, "add", "next.txt")
                fixture.git(ws_a, "commit", "-m", "next")

                with transaction(conn):
                    conn.execute(
                        "UPDATE projects SET repo_path = ?, base_branch = 'main', integration_branch = 'mesh/prj_1/integration' WHERE id = 'prj_1';",
                        (repo,),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, base_commit, merge_json, created_by, created_at, updated_at) "
                        "VALUES ('tsk_m', 'prj_1', 'M', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_m', ?, ?, 'agt_lead', ?, ?);",
                        (ws_m, base_sha, json.dumps({"merge_commit": mc}), now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, base_commit, created_by, created_at, updated_at) "
                        "VALUES ('tsk_a', 'prj_1', 'A', 'S', 'code', 'accepted', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_a', ?, 'agt_lead', ?, ?);",
                        (ws_a, mc, now, now),
                    )

                srv = DaemonServer(home, testing_mode=True)
                srv.db_conn = conn
                entered = threading.Event()
                release = threading.Event()
                orig_exec = run_handlers.execute_workspace_prune_plan

                def _paused_exec(plan):
                    entered.set()
                    release.wait(timeout=5.0)
                    return orig_exec(plan)

                async def _scenario() -> dict:
                    with mock.patch.object(run_handlers, "execute_workspace_prune_plan", side_effect=_paused_exec):
                        t = asyncio.create_task(_drive_server(
                            srv,
                            "workspace.prune",
                            {"project": "prj_1"},
                            principal={"id": "agt_lead", "kind": "agent", "name": "lead1"},
                            actor_kind="agent",
                            via="token",
                            request_id="req_prn_1",
                        ))
                        while not entered.is_set():
                            await asyncio.sleep(0.01)

                        # Concurrent task.merge in same project is excluded by the prune reservation!
                        r_mrg = await _drive_server(
                            srv, "task.merge", {"task_id": "tsk_a"}, request_id="req_mrg_concurrent"
                        )
                        self.assertFalse(r_mrg["ok"], r_mrg)
                        self.assertEqual(r_mrg["error"]["code"], c.CONCURRENCY_LIMIT, r_mrg)

                        # Suppose agt_lead is superseded right before TX2: TX2 must re-check authz and deny agt_lead!
                        with transaction(conn):
                            conn.execute(
                                "UPDATE lead_assignments SET ended_at = ? WHERE id = 'lead_1';",
                                (now_iso_utc(),),
                            )
                        release.set()
                        return await t

                resp = asyncio.run(_scenario())
                self.assertFalse(resp["ok"], resp)
                self.assertEqual(resp["error"]["code"], c.NOT_PROJECT_LEAD, resp)
                # Yet because tsk_m was physically pruned, a system workspace.pruned event IS recorded,
                # zero agent-authored workspace.pruned events exist, and reservation is cleaned up.
                ev_agent = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND actor_kind != 'system';"
                ).fetchone()[0]
                self.assertEqual(ev_agent, 0)
                ev_sys = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND actor_kind = 'system' AND task_id = 'tsk_m';"
                ).fetchone()[0]
                self.assertEqual(ev_sys, 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)
            finally:
                conn.close()

    def test_recovery_handles_leftover_ws_delete_and_prune_reservations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2-p11rec-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                ws_keep = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_keep")
                os.makedirs(ws_keep, mode=0o700)
                ws_gone = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_gone")

                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_keep', 'prj_1', 'Keep', 'S', 'code', 'accepted', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_keep', 'usr_1', ?, ?);",
                        (ws_keep, now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_gone', 'prj_1', 'Gone', 'S', 'code', 'accepted', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_gone', 'usr_1', ?, ?);",
                        (ws_gone, now, now),
                    )
                    apr_gone = create_or_get_pending_approval(
                        conn=conn, category="delete", op="workspace.delete", requested_by="agt_lead",
                        params={"task_id": "tsk_gone"}, summary="Del gone", project_id="prj_1", target_id="tsk_gone",
                        testing_mode=True,
                    )
                    handle_approval_approve(_ctx(
                        conn, home, "approval.approve",
                        {"approval_id": apr_gone["approval_id"], "op_hash": apr_gone["op_hash"]},
                        k_user=b"\x03" * 32,
                    ))
                    gone_marker = os.path.join(home, "run", "ws_del_tsk_gone_test.json")
                    with open(gone_marker, "w", encoding="utf-8") as mf:
                        json.dump({"rmtree_started": True}, mf)
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?);",
                        ("ws_res:task:tsk_keep", json.dumps({
                            "kind": "delete",
                            "task_id": "tsk_keep",
                            "project_id": "prj_1",
                            "path": ws_keep,
                            "stripped": ws_keep,
                            "real": ws_keep,
                            "branch": "mesh/prj_1/tsk_keep",
                            "approval_id": None,
                            "requested_by": "usr_1",
                            "request_id": "req_k",
                        })),
                    )
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?);",
                        ("ws_res:task:tsk_gone", json.dumps({
                            "kind": "delete",
                            # The worker durably marks rmtree as started (review PR45 R5 P2).
                            "rmtree_marker": gone_marker,
                            "task_id": "tsk_gone",
                            "project_id": "prj_1",
                            "path": ws_gone,
                            "stripped": ws_gone,
                            "real": ws_gone,
                            "branch": "mesh/prj_1/tsk_gone",
                            "approval_id": apr_gone["approval_id"],
                            "requested_by": "agt_lead",
                            "request_id": "req_g",
                            "idempotency": {
                                "principal_id": "agt_lead",
                                "key": "k-gone",
                                "cmd": "workspace.delete",
                                "request_hash": "h_gone",
                            },
                        })),
                    )
                    ckpt = os.path.join(home, "run", "ws_prune_test.json")
                    with open(ckpt, "w", encoding="utf-8") as cf:
                        json.dump({
                            "root": os.path.join(home, "workspaces"),
                            "completed": True,
                            "removed": [{
                                "task_id": "tsk_gone",
                                "project_id": "prj_1",
                                "status": "merged",
                                "path": ws_gone,
                                "branch": "mesh/prj_1/tsk_gone",
                            }],
                            "skipped": [],
                        }, cf)
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?);",
                        ("ws_res:prune_proj:prj_1", json.dumps({
                            "kind": "prune",
                            "project_id": "prj_1",
                            "projects": ["prj_1"],
                            "task_ids": ["tsk_gone"],
                            "checkpoint_path": ckpt,
                            "requested_by": "usr_1",
                            "request_id": "req_p",
                            "idempotency": {
                                "principal_id": "usr_1",
                                "key": "k-prn-rec",
                                "cmd": "workspace.prune",
                                "request_hash": "h_prn",
                            },
                        })),
                    )

                summary = recovery.recover(conn, home, "boot_rec", testing_mode=True)
                self.assertEqual(summary.get("workspace_deletes_finalized"), 1, summary)
                self.assertEqual(summary.get("workspace_deletes_rolled_back"), 1, summary)
                self.assertEqual(summary.get("workspace_prunes_finalized"), 1, summary)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_gone["approval_id"],)).fetchone()["status"],
                    "consumed",
                )
                self.assertIsNotNone(conn.execute("SELECT 1 FROM idempotency WHERE key = 'k-gone';").fetchone())
                self.assertIsNotNone(conn.execute("SELECT 1 FROM idempotency WHERE key = 'k-prn-rec';").fetchone())
                self.assertFalse(os.path.exists(ckpt))
                self.assertEqual(check_i7_event_hash_chain(conn), [])
                self.assertEqual(check_i8_cancelling_and_leases(conn), [])
            finally:
                conn.close()


class TestP1_2MergeTrackedAndUnreadableCleanup(unittest.TestCase):
    """P1-2: test_command editing tracked files or leaving 0o000/0o100/empty dirs must restore cleanly."""

    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.repo = self.fixture.make_k0("P12U")
        self.base = self.fixture.head(self.repo, "main")

    def _make_task_branch(self, branch: str, fname: str, content: str) -> None:
        self.fixture.git(self.repo, "checkout", "-b", branch, self.base)
        with open(os.path.join(self.repo, fname), "w", encoding="utf-8") as f:
            f.write(content)
        self.fixture.git(self.repo, "add", fname)
        self.fixture.git(self.repo, "commit", "-q", "-m", f"update {fname}")
        self.fixture.git(self.repo, "checkout", "-q", "main")

    def test_tracked_involved_file_modified_by_test_command_restores_and_allows_next_merge(self) -> None:
        # Task branch modifies calc.py (so calc.py is staged in the merge!), test_command overwrites calc.py.
        self._make_task_branch("mesh/prj_x/tsk_inv", "calc.py", "def add(a, b):\n    return a + b + 1\n")
        for exit_code, expected_reason in ((0, "integration_dirty"), (1, "tests_failed")):
            run_dir = os.path.join(self.fixture.base, f"run_inv_{exit_code}")
            os.makedirs(run_dir, mode=0o700, exist_ok=True)
            script = (
                "import sys\n"
                "with open('calc.py', 'w') as f:\n"
                "    f.write('mutated_by_test = True\\n')\n"
                f"sys.exit({exit_code})\n"
            )
            plan = {
                "run_id": f"run_inv_{exit_code}",
                "task_id": "tsk_inv",
                "title": "involved",
                "integration": self.repo,
                "worktree": self.repo,
                "branch": "mesh/prj_x/tsk_inv",
                "pre_head": self.base,
                "test_command": "custom",
                "test_argv": [sys.executable, "-c", script],
                "test_timeout_s": 30,
                "kill_grace_s": 1,
                "run_dir": run_dir,
            }
            out = merge.run_merge_pipeline(plan)
            self.assertEqual(out["outcome"], "failed", out)
            self.assertEqual(out["reason"], expected_reason, out)
            self.assertIs(out["details"].get("restored"), True, out)
            self.assertFalse(self.fixture.has_merge_head(self.repo))
            self.assertEqual(self.fixture.head(self.repo), self.base)
            self.assertTrue(self.fixture.is_clean(self.repo))

        # Subsequent clean merge on the same repo must succeed!
        self._make_task_branch("mesh/prj_x/tsk_clean", "clean.txt", "clean\n")
        run_dir_c = os.path.join(self.fixture.base, "run_clean")
        os.makedirs(run_dir_c, mode=0o700, exist_ok=True)
        out_c = merge.run_merge_pipeline({
            "run_id": "run_clean",
            "task_id": "tsk_clean",
            "title": "clean",
            "integration": self.repo,
            "worktree": self.repo,
            "branch": "mesh/prj_x/tsk_clean",
            "pre_head": self.base,
            "test_command": "none",
            "test_argv": None,
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir_c,
        })
        self.assertEqual(out_c["outcome"], "merged", out_c)

    def test_tracked_uninvolved_file_edited_and_deleted_restored(self) -> None:
        self._make_task_branch("mesh/prj_x/tsk_u", "new_mod.txt", "mod\n")
        run_dir = os.path.join(self.fixture.base, "run_u")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        script = (
            "import os\n"
            "with open('tests/test_calc.py', 'w') as f:\n"
            "    f.write('# overwritten\\n')\n"
            "os.unlink('tests/__init__.py')\n"
        )
        out = merge.run_merge_pipeline({
            "run_id": "run_u",
            "task_id": "tsk_u",
            "title": "uninvolved",
            "integration": self.repo,
            "worktree": self.repo,
            "branch": "mesh/prj_x/tsk_u",
            "pre_head": self.base,
            "test_command": "custom",
            "test_argv": [sys.executable, "-c", script],
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir,
        })
        self.assertEqual(out["outcome"], "failed", out)
        self.assertEqual(out["reason"], "integration_dirty", out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        self.assertTrue(self.fixture.is_clean(self.repo))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "tests", "__init__.py")))

    def test_unreadable_000_0100_and_empty_dirs_cleaned_and_recovered(self) -> None:
        self._make_task_branch("mesh/prj_x/tsk_000", "ok.txt", "ok\n")
        run_dir = os.path.join(self.fixture.base, "run_000")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        script = (
            "import os\n"
            "os.makedirs('top000', mode=0o700)\n"
            "open('top000/f', 'w').write('1')\n"
            "os.chmod('top000', 0o000)\n"
            "os.makedirs('cache/sub000', mode=0o700)\n"
            "open('cache/norm.txt', 'w').write('2')\n"
            "open('cache/sub000/g', 'w').write('3')\n"
            "os.chmod('cache/sub000', 0o000)\n"
            "os.makedirs('dir100', mode=0o700)\n"
            "open('dir100/h', 'w').write('4')\n"
            "os.chmod('dir100', 0o100)\n"
            "os.makedirs('empty_dir', mode=0o700)\n"
        )
        try:
            out = merge.run_merge_pipeline({
                "run_id": "run_000",
                "task_id": "tsk_000",
                "title": "000dirs",
                "integration": self.repo,
                "worktree": self.repo,
                "branch": "mesh/prj_x/tsk_000",
                "pre_head": self.base,
                "test_command": "custom",
                "test_argv": [sys.executable, "-c", script],
                "test_timeout_s": 30,
                "kill_grace_s": 1,
                "run_dir": run_dir,
            })
            self.assertEqual(out["outcome"], "merged", out)
            for leftover in ("top000", "cache", "dir100", "empty_dir"):
                self.assertFalse(os.path.exists(os.path.join(self.repo, leftover)), f"leftover: {leftover}")
            self.assertTrue(self.fixture.is_clean(self.repo))
        finally:
            for d in ("top000", "cache", "cache/sub000", "dir100"):
                p = os.path.join(self.repo, d)
                if os.path.exists(p):
                    os.chmod(p, 0o700)

        # Also verify decide_recovery cleans tracked edits + 0o000/empty dirs when pre-merge snapshots exist,
        # and preserves pre-existing empty dirs when pre_empty_dirs.json is absent (run_dir=None).
        self._make_task_branch("mesh/prj_x/tsk_crash_rec", "calc.py", "def add(a, b):\n    return a * b\n")
        cur_head = self.fixture.head(self.repo)
        crash_run_dir = os.path.join(self.fixture.base, "run_crash_rec")
        os.makedirs(crash_run_dir, mode=0o700, exist_ok=True)
        merge._save_pre_untracked(crash_run_dir, set())
        merge._save_pre_empty_dirs(crash_run_dir, set())
        self.fixture.git(self.repo, "merge", "--no-ff", "--no-commit", "mesh/prj_x/tsk_crash_rec")
        with open(os.path.join(self.repo, "calc.py"), "w", encoding="utf-8") as f:
            f.write("# crashed mid-test\n")
        os.makedirs(os.path.join(self.repo, "crash000"), mode=0o700)
        with open(os.path.join(self.repo, "crash000", "x"), "w", encoding="utf-8") as f:
            f.write("x\n")
        os.chmod(os.path.join(self.repo, "crash000"), 0o000)
        os.makedirs(os.path.join(self.repo, "crash_empty"), mode=0o700)
        try:
            rec = merge.decide_recovery(
                self.repo, "tsk_crash_rec", "mesh/prj_x/tsk_crash_rec", cur_head, run_dir=crash_run_dir
            )
            self.assertEqual(rec["outcome"], "failed", rec)
            self.assertIs(rec["details"].get("restored"), True, rec)
            self.assertFalse(self.fixture.has_merge_head(self.repo))
            self.assertTrue(self.fixture.is_clean(self.repo))
            self.assertFalse(os.path.exists(os.path.join(self.repo, "crash000")))
            self.assertFalse(os.path.exists(os.path.join(self.repo, "crash_empty")))
        finally:
            p = os.path.join(self.repo, "crash000")
            if os.path.exists(p):
                os.chmod(p, 0o700)

        preexisting_empty = os.path.join(self.repo, "preexisting_empty")
        os.makedirs(preexisting_empty, mode=0o700)
        self.fixture.git(self.repo, "merge", "--no-ff", "--no-commit", "mesh/prj_x/tsk_crash_rec")
        with open(os.path.join(self.repo, "calc.py"), "w", encoding="utf-8") as f:
            f.write("# crashed without snapshots\n")
        try:
            rec_nosnap = merge.decide_recovery(
                self.repo, "tsk_crash_rec", "mesh/prj_x/tsk_crash_rec", cur_head, run_dir=None
            )
            self.assertEqual(rec_nosnap["outcome"], "failed", rec_nosnap)
            self.assertIs(rec_nosnap["details"].get("restored"), True, rec_nosnap)
            self.assertFalse(self.fixture.has_merge_head(self.repo))
            self.assertTrue(self.fixture.is_clean(self.repo))
            self.assertTrue(os.path.isdir(preexisting_empty))
        finally:
            if os.path.isdir(preexisting_empty):
                os.rmdir(preexisting_empty)


class TestP2ValidateSettingSymlinkAncestors(unittest.TestCase):
    """P2: validate_setting(workspaces.root) and runtime checks must reject symlinked ancestors."""

    def test_validate_setting_rejects_symlink_ancestor_and_runtime_blocks_traversal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2-p2-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                real_a = os.path.join(tmp, "real_a")
                ws_root = os.path.join(real_a, "ws_root")
                os.makedirs(ws_root, mode=0o700)
                link_parent = os.path.join(tmp, "link_parent")
                os.symlink(real_a, link_parent)

                symlinked_root = os.path.join(link_parent, "ws_root")
                with self.assertRaises(MeshError) as cm:
                    validate_setting("workspaces.root", symlinked_root, home, db_conn=conn)
                self.assertEqual(cm.exception.code, c.INVALID_SETTING)
                self.assertIn("symlink", cm.exception.message.lower())

                # Even if a legacy symlink-ancestor path sits in settings/tasks, runtime delete refuses it!
                victim = os.path.join(tmp, "victim")
                victim_ws = os.path.join(victim, "ws_root", "prj_1", "tasks", "tsk_vic")
                os.makedirs(victim_ws, mode=0o700)
                with open(os.path.join(victim_ws, "precious.txt"), "w", encoding="utf-8") as f:
                    f.write("keep me\n")
                now = now_iso_utc()
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO settings (key, value_json, updated_at, updated_by) "
                        "VALUES ('workspaces.root', ?, ?, 'usr_1') "
                        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json;",
                        (json.dumps(symlinked_root), now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_vic', 'prj_1', 'V', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_vic', 'usr_1', ?, ?);",
                        (os.path.join(symlinked_root, "prj_1", "tasks", "tsk_vic"), now, now),
                    )
                os.unlink(link_parent)
                os.symlink(victim, link_parent)
                with self.assertRaises(MeshError) as cm_del:
                    handle_workspace_delete(_ctx(conn, home, "workspace.delete", {"task_id": "tsk_vic"}))
                self.assertEqual(cm_del.exception.code, c.INVALID_ARGUMENT)
                self.assertTrue(os.path.exists(os.path.join(victim_ws, "precious.txt")))

                # config.set workspaces.root with clean path stores canonical protected_path
                os.unlink(link_parent)
                os.symlink(real_a, link_parent)
                clean_new = os.path.join(tmp, "clean_ws")
                os.makedirs(clean_new, mode=0o700)
                with transaction(conn):
                    conn.execute("UPDATE projects SET status = 'completed' WHERE id = 'prj_1';")
                    res = handle_config_set(_ctx(
                        conn, home, "config.set", {"key": "workspaces.root", "value": clean_new},
                        k_user=b"\x04" * 32,
                    ))
                self.assertEqual(res.data["value"], os.path.realpath(clean_new))
                prot = [r[0] for r in conn.execute("SELECT value FROM meta WHERE key LIKE 'protected_path:%';").fetchall()]
                self.assertIn(os.path.realpath(clean_new), prot)
            finally:
                conn.close()


class TestP3TestingModePropagation(unittest.TestCase):
    """P3: handle_run_release, _drain_for_shutdown, _fail_lead_turn_spawn, recovery._lost forward testing_mode."""

    def tearDown(self) -> None:
        supervisor._SPAWN_ABORTS.clear()
        supervisor._WRAPPERS.clear()
        supervisor._SEEN_UNUSABLE.clear()
        supervisor._SEEN_REASON.clear()

    def test_handle_run_release_forwards_testing_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2-p3rel-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                run_dir = os.path.join(home, "runs", "run_rel")
                os.makedirs(run_dir, mode=0o700)
                with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
                    json.dump({"session_id": "sess_from_fake", "final_text": "hi"}, f)
                with open(os.path.join(run_dir, "seen.json"), "w", encoding="utf-8") as f:
                    f.write("{corrupt")
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                        "timeout_s, max_attempts, created_by, created_at, updated_at) "
                        "VALUES ('tsk_rel', 'prj_1', 'R', 'S', 'general', 'running', 'agt_work', 'dev', 600, 3, 'usr_1', ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                        "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
                        "VALUES ('run_rel', 'task', 'tsk_rel', 'prj_1', 'agt_work', 1, 1, 'fake', 'running', 'boot_1', "
                        "999998, 999998, 1, ?, ?);",
                        (run_dir, now),
                    )
                    conn.execute(
                        "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
                        "VALUES ('task:tsk_rel', 'run_rel', 'meshd', 'boot_1', 1, ?, '2099-01-01T00:00:00.000Z', ?);",
                        (now, now),
                    )

                clean_env = {k: v for k, v in os.environ.items() if k != "MESH_TESTING"}
                with mock.patch.dict(os.environ, clean_env, clear=True), \
                     mock.patch.object(supervisor, "_group_occupied", return_value=True), \
                     mock.patch.object(supervisor.terminate, "run_targets", return_value=[]):
                    # Host-independent: the fake pgid must not depend on what /proc shows here.
                    with transaction(conn):
                        handle_run_release(_ctx(
                            conn, home, "run.release",
                            {"run_id": "run_rel", "force_seen_unusable": True},
                            k_user=b"\x05" * 32,
                            testing_mode=True,
                        ))
                self.assertEqual(
                    conn.execute("SELECT session_id FROM runs WHERE id = 'run_rel';").fetchone()["session_id"],
                    "sess_from_fake",
                )
                self.assertEqual(
                    conn.execute("SELECT last_session_id FROM tasks WHERE id = 'tsk_rel';").fetchone()["last_session_id"],
                    "sess_from_fake",
                )

                # Reverse direction: MESH_TESTING=1 in env must not enable fake parser when testing_mode=False
                run_dir2 = os.path.join(home, "runs", "run_rel2")
                os.makedirs(run_dir2, mode=0o700)
                with open(os.path.join(run_dir2, "stdout.log"), "w", encoding="utf-8") as f:
                    json.dump({"session_id": "sess_leak", "final_text": "hi"}, f)
                with open(os.path.join(run_dir2, "seen.json"), "w", encoding="utf-8") as f:
                    f.write("{corrupt")
                with transaction(conn):
                    conn.execute(
                        "UPDATE tasks SET status = 'running' WHERE id = 'tsk_rel';"
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                        "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
                        "VALUES ('run_rel2', 'task', 'tsk_rel', 'prj_1', 'agt_work', 2, 2, 'fake', 'running', 'boot_1', "
                        "999997, 999997, 1, ?, ?);",
                        (run_dir2, now),
                    )
                with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}), \
                     mock.patch.object(supervisor, "_group_occupied", return_value=True), \
                     mock.patch.object(supervisor.terminate, "run_targets", return_value=[]):
                    with transaction(conn):
                        handle_run_release(_ctx(
                            conn, home, "run.release",
                            {"run_id": "run_rel2", "force_seen_unusable": True},
                            k_user=b"\x05" * 32,
                            testing_mode=False,
                        ))
                self.assertIsNone(
                    conn.execute("SELECT session_id FROM runs WHERE id = 'run_rel2';").fetchone()["session_id"],
                )
            finally:
                conn.close()

    def test_drain_for_shutdown_forwards_testing_mode_to_retry_spawn_aborts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2-p3drn-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now_ts = time.time()
                now = now_iso_utc(now_ts)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO settings (key, value_json, updated_at, updated_by) "
                        "VALUES ('lease.ttl_s', '1', ?, 'usr_1') "
                        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json;",
                        (now,),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                        "timeout_s, max_attempts, created_by, created_at, updated_at) "
                        "VALUES ('tsk_ab', 'prj_1', 'Ab', 'S', 'general', 'running', 'agt_work', 'dev', 600, 3, 'usr_1', ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                        "boot_id, run_dir, started_at) "
                        "VALUES ('run_ab', 'task', 'tsk_ab', 'prj_1', 'agt_work', 1, 1, 'none', 'starting', 'boot_1', ?, ?);",
                        (os.path.join(home, "runs", "run_ab"), now),
                    )
                    conn.execute(
                        "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
                        "VALUES ('task:tsk_ab', 'run_ab', 'meshd', 'boot_1', 1, ?, ?, ?);",
                        (now, now, now),
                    )

                class _FakeProc:
                    pid = 999999

                ab = supervisor._SpawnAbort(
                    _FakeProc(), "run_ab", ("tsk_ab", "prj_1", 1, 1, 3, "boom"), home=home, kind="task"
                )
                supervisor._SPAWN_ABORTS["run_ab"] = ab
                srv = DaemonServer(home, testing_mode=False)
                srv.db_conn = conn

                calls = {"n": 0}

                def _abort_kill(_a):
                    calls["n"] += 1
                    if calls["n"] >= 2:
                        supervisor._SPAWN_ABORTS.clear()
                    return False, {999999: 1}

                with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}), \
                     mock.patch.object(supervisor, "_abort_kill_pass", side_effect=_abort_kill):
                    asyncio.run(srv._drain_for_shutdown(grace_s=0.3, settle_s=0.3))

                exp = conn.execute("SELECT expires_at FROM leases WHERE key = 'task:tsk_ab';").fetchone()["expires_at"]
                # In testing_mode=False, lease.ttl_s=1 must be clamped to >= 10s despite MESH_TESTING=1 in env!
                self.assertGreaterEqual(parse_utc(exp).timestamp() - now_ts, 9.0)
            finally:
                conn.close()

    def test_fail_lead_turn_spawn_and_recovery_lost_forward_testing_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2-p3lt-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                with transaction(conn):
                    ev1 = record_event(
                        conn=conn,
                        type_="task.created",
                        actor_id="usr_1",
                        actor_kind="user",
                        via="direct",
                        project_id="prj_1",
                        payload={},
                    )
                    conn.execute(
                        "INSERT INTO settings (key, value_json, updated_at, updated_by) "
                        "VALUES ('lead.wake_backoff_s', '[1, 1, 1]', ?, 'usr_1') "
                        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json;",
                        (now,),
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, project_id, agent_id, attempt, fence, adapter, status, "
                        "boot_id, run_dir, started_at, lead_assignment_id) "
                        "VALUES ('run_lt1', 'lead_turn', 'prj_1', 'agt_lead', 0, 1, 'fake', 'starting', 'boot_1', ?, ?, 'lead_1');",
                        (os.path.join(home, "runs", "run_lt1"), now),
                    )
                    conn.execute(
                        "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                        "VALUES ('leadturn:prj_1', 'run_lt1', 'meshd', 'boot_1', 'agt_lead', 1, ?, '2099-01-01T00:00:00.000Z', ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, "
                        "type, payload_json, status, attempts, delivering_run_id, next_attempt_at, created_at) "
                        "VALUES ('msg_lt1', ?, 'agt_lead', 'lead_wake', 'dk_lt1', 'prj_1', "
                        "'task.created', '{}', 'delivering', 0, 'run_lt1', ?, ?);",
                        (ev1["seq"], now, now),
                    )

                # With MESH_TESTING unset and testing_mode=True, backoff must be 1s (not 30s)!
                clean_env = {k: v for k, v in os.environ.items() if k != "MESH_TESTING"}
                before_ts = time.time()
                with mock.patch.dict(os.environ, clean_env, clear=True):
                    supervisor.handle_spawn_failure(
                        conn, home, "run_lt1", os.path.join(home, "runs", "run_lt1"),
                        None, RuntimeError("spawn err"), kind="lead_turn", fail_args=("prj_1",),
                        testing_mode=True,
                    )
                next_at = conn.execute("SELECT next_attempt_at FROM outbox WHERE id = 'msg_lt1';").fetchone()["next_attempt_at"]
                delta = parse_utc(next_at).timestamp() - before_ts
                self.assertLess(delta, 5.0, f"expected ~1s testing backoff, got {delta:.2f}s")

                # Recovery lost_before_spawn path also forwards testing_mode=True even without MESH_TESTING in env!
                run_dir2 = os.path.join(home, "runs", "run_lt2")
                os.makedirs(run_dir2, mode=0o700)
                with transaction(conn):
                    ev2 = record_event(
                        conn=conn,
                        type_="task.created",
                        actor_id="usr_1",
                        actor_kind="user",
                        via="direct",
                        project_id="prj_1",
                        payload={},
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, project_id, agent_id, attempt, fence, adapter, status, "
                        "boot_id, run_dir, started_at, lead_assignment_id) "
                        "VALUES ('run_lt2', 'lead_turn', 'prj_1', 'agt_lead', 0, 1, 'fake', 'starting', 'boot_old', ?, ?, 'lead_1');",
                        (run_dir2, now),
                    )
                    conn.execute(
                        "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                        "VALUES ('leadturn:prj_1', 'run_lt2', 'meshd', 'boot_old', 'agt_lead', 1, ?, '2099-01-01T00:00:00.000Z', ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, "
                        "type, payload_json, status, attempts, delivering_run_id, next_attempt_at, created_at) "
                        "VALUES ('msg_lt2', ?, 'agt_lead', 'lead_wake', 'dk_lt2', 'prj_1', "
                        "'task.created', '{}', 'delivering', 0, 'run_lt2', ?, ?);",
                        (ev2["seq"], now, now),
                    )
                before_rec = time.time()
                with mock.patch.dict(os.environ, clean_env, clear=True):
                    recovery.recover(conn, home, "boot_new", testing_mode=True)
                next_at2 = conn.execute("SELECT next_attempt_at FROM outbox WHERE id = 'msg_lt2';").fetchone()["next_attempt_at"]
                delta2 = parse_utc(next_at2).timestamp() - before_rec
                self.assertLess(delta2, 5.0, f"expected ~1s testing backoff on recovery, got {delta2:.2f}s")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
