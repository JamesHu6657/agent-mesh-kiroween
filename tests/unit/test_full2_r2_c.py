"""Regression tests for full2-r2.md Group C findings (P1-4, P2-3, P2-9)."""

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
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.ids import generate_id
from mesh.core.redact import redact
from mesh.core.workspaces import is_approval_pinned_by_ws_reservation, ws_delete_reservation_key
from mesh.daemon import merge, recovery, supervisor
from mesh.daemon.handlers import run as run_handlers
from mesh.daemon.handlers.approval import handle_approval_approve
from mesh.daemon.handlers.base import Effect, HandlerContext
from mesh.daemon.server import DaemonServer, _store_idempotency_row
from mesh.errors import MeshError

MESH_PROC_LOCK = "shared"


def _setup_home_and_db(tmp: str):
    home = os.path.join(tmp, "home")
    for sub in ("run", "logs", "runs", "merges", "workspaces"):
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
            "default_profile, created_at, updated_at) VALUES ('agt_lead', 'none', 'inbox', 1, '[]', 1, 'edit', ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_other', 'agent', 'lead2', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) VALUES ('agt_other', 'none', 'inbox', 1, '[]', 1, 'edit', ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
            "created_by, created_via, version, created_at, updated_at) "
            "VALUES ('prj_1', 'P1', 'goal', 'active', ?, 'main', 'mesh/prj_1/integration', 'usr_1', 'direct', 1, ?, ?);",
            (os.path.join(tmp, "repo1"), now, now),
        )
        conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
            (now,),
        )
    os.makedirs(os.path.join(tmp, "repo1"), mode=0o700, exist_ok=True)
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
    approval: str = None,
    request_id: str = "req_test",
    k_user: bytes = None,
    dry_run: bool = False,
) -> HandlerContext:
    name = "user" if actor_kind == "user" else "lead1"
    return HandlerContext(
        conn=conn,
        principal={"id": principal_id, "kind": actor_kind, "name": name},
        actor_kind=actor_kind,
        via=via,
        request_id=request_id,
        home=home,
        cmd=cmd,
        args=args,
        approval=approval,
        k_user=k_user,
        dry_run=dry_run,
        testing_mode=True,
    )


class _DummyServer:
    def __init__(self) -> None:
        self.logs = []

    def track_merge_job(self, job) -> None:
        pass

    async def notify_waiters(self) -> None:
        pass

    def wake_scheduler(self) -> None:
        pass

    def log_error(self, msg: str) -> None:
        self.logs.append(msg)


class TestP1_4TaskMergeTx1Idempotency(unittest.TestCase):
    """P1-4: task.merge must re-check idempotency and replay authz inside TX1."""

    def _insert_task(self, conn: sqlite3.Connection, home: str, task_id: str = "tsk_m1", status: str = "accepted"):
        now = now_iso_utc()
        ws = os.path.join(home, "workspaces", "prj_1", "tasks", task_id)
        os.makedirs(ws, mode=0o700, exist_ok=True)
        with transaction(conn):
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES (?, 'prj_1', 'MergeTask', 'S', 'code', ?, 'dev', 600, 3, ?, ?, 'usr_1', ?, ?);",
                (task_id, status, ws, f"mesh/prj_1/{task_id}", now, now),
            )

    def test_concurrent_merge_replays_after_probe_window_including_merge_failed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2c-p14a-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                self._insert_task(conn, home, "tsk_m1", status="accepted")
                ctx = _ctx(
                    conn,
                    home,
                    "task.merge",
                    {"task_id": "tsk_m1"},
                    principal_id="agt_lead",
                    actor_kind="agent",
                    via="token",
                    request_id="req_retry",
                )
                ctx.idempotency = {  # type: ignore[attr-defined]
                    "principal_id": "agt_lead",
                    "key": "k-merge-1",
                    "request_hash": "hash_same",
                }
                stored_err = {
                    "ok": False,
                    "error": {"code": c.MERGE_CONFLICT, "message": "conflict", "details": {"task_id": "tsk_m1"}},
                    "exit_code": c.EXIT_MERGE_FAILED,
                    "request_id": "req_orig",
                    "idempotent_replay": False,
                    "dry_run": False,
                }

                def _probe_while_r1_finishes(_integ: str) -> str:
                    # Simulate a concurrent request with the same idempotency key finishing TX2
                    # with merge_failed (rc 14) while this request was probing integration HEAD.
                    # Without TX1 idempotency check, merge_failed -> merging (T17) would re-run!
                    tconn = sqlite3.connect(os.path.join(home, "mesh.db"), isolation_level=None, timeout=10)
                    tconn.row_factory = sqlite3.Row
                    try:
                        with transaction(tconn):
                            tconn.execute("UPDATE tasks SET status = 'merge_failed' WHERE id = 'tsk_m1';")
                            _store_idempotency_row(
                                tconn,
                                "agt_lead",
                                "k-merge-1",
                                "task.merge",
                                "hash_same",
                                json.dumps(stored_err),
                                c.EXIT_MERGE_FAILED,
                            )
                    finally:
                        tconn.close()
                    return "0" * 40

                with mock.patch.object(merge, "probe_integration_head", side_effect=_probe_while_r1_finishes), \
                     mock.patch.object(merge, "run_merge_pipeline", side_effect=AssertionError("second merge ran!")):
                    res = asyncio.run(merge.run_task_merge(_DummyServer(), ctx))

                self.assertEqual(res.exit_code, c.EXIT_MERGE_FAILED)
                env = getattr(res, "_idempotent_replay_envelope", None)
                self.assertIsNotNone(env)
                self.assertTrue(env["idempotent_replay"])
                self.assertEqual(env["request_id"], "req_orig")
                self.assertEqual(
                    conn.execute("SELECT status FROM tasks WHERE id = 'tsk_m1';").fetchone()["status"],
                    "merge_failed",
                )
            finally:
                conn.close()

    def test_concurrent_merge_key_reuse_different_hash_raises_conflict(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2c-p14b-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                self._insert_task(conn, home, "tsk_m2", status="accepted")
                ctx = _ctx(
                    conn,
                    home,
                    "task.merge",
                    {"task_id": "tsk_m2"},
                    principal_id="agt_lead",
                    actor_kind="agent",
                    via="token",
                    request_id="req_reuse",
                )
                ctx.idempotency = {  # type: ignore[attr-defined]
                    "principal_id": "agt_lead",
                    "key": "k-merge-reuse",
                    "request_hash": "hash_new",
                }

                def _probe_stores_other_hash(_integ: str) -> str:
                    tconn = sqlite3.connect(os.path.join(home, "mesh.db"), isolation_level=None, timeout=10)
                    tconn.row_factory = sqlite3.Row
                    try:
                        with transaction(tconn):
                            _store_idempotency_row(
                                tconn,
                                "agt_lead",
                                "k-merge-reuse",
                                "task.merge",
                                "hash_other",
                                json.dumps({"ok": True, "data": {}, "exit_code": 0}),
                                0,
                            )
                    finally:
                        tconn.close()
                    return "0" * 40

                with mock.patch.object(merge, "probe_integration_head", side_effect=_probe_stores_other_hash), \
                     mock.patch.object(merge, "run_merge_pipeline", side_effect=AssertionError("merge should not run")):
                    with self.assertRaises(MeshError) as cm:
                        asyncio.run(merge.run_task_merge(_DummyServer(), ctx))

                self.assertEqual(cm.exception.code, c.IDEMPOTENCY_KEY_REUSED)
                self.assertFalse(conn.in_transaction)
            finally:
                conn.close()

    def test_tx1_idempotency_rechecks_authz_on_replay(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2c-p14c-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                self._insert_task(conn, home, "tsk_m3", status="merged")
                now = now_iso_utc()
                with transaction(conn):
                    _store_idempotency_row(
                        conn,
                        "agt_lead",
                        "k-merge-authz",
                        "task.merge",
                        "hash_m3",
                        json.dumps({"ok": True, "data": {"task_id": "tsk_m3", "status": "merged"}, "exit_code": 0}),
                        0,
                    )
                    # Lead assignment ends before TX1 runs (e.g. during probe await).
                    conn.execute(
                        "UPDATE lead_assignments SET ended_at = ?, ended_reason = 'superseded' WHERE id = 'lead_1';",
                        (now,),
                    )
                ctx = _ctx(
                    conn,
                    home,
                    "task.merge",
                    {"task_id": "tsk_m3"},
                    principal_id="agt_lead",
                    actor_kind="agent",
                    via="token",
                )
                idem = {"principal_id": "agt_lead", "key": "k-merge-authz", "request_hash": "hash_m3"}
                with self.assertRaises(MeshError) as cm:
                    run_handlers._check_tx1_idempotency(conn, idem, "task.merge", ctx=ctx)
                self.assertEqual(cm.exception.code, c.NOT_PROJECT_LEAD)
            finally:
                conn.close()


class TestP2_3WorkspaceDeleteApprovalIsolation(unittest.TestCase):
    """P2-3: direct user delete must not store/consume approvals; recovery must scope approvals."""

    def _insert_task_with_ws(self, conn: sqlite3.Connection, home: str, task_id: str, project_id: str = "prj_1") -> str:
        now = now_iso_utc()
        ws = os.path.join(home, "workspaces", project_id, "tasks", task_id)
        os.makedirs(ws, mode=0o700, exist_ok=True)
        with open(os.path.join(ws, "work.txt"), "w", encoding="utf-8") as f:
            f.write("data\n")
        with transaction(conn):
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES (?, ?, 'DelTask', 'S', 'code', 'accepted', 'dev', 600, 3, ?, ?, 'usr_1', ?, ?);",
                (task_id, project_id, ws, f"mesh/{project_id}/{task_id}", now, now),
            )
        return ws

    def _create_approved(
        self,
        conn: sqlite3.Connection,
        home: str,
        *,
        category: str,
        op: str,
        project_id: str,
        target_id: str,
        params: dict,
    ) -> str:
        with transaction(conn):
            apr = create_or_get_pending_approval(
                conn=conn,
                category=category,
                op=op,
                requested_by="agt_lead",
                params=params,
                summary=f"{op} {target_id}",
                project_id=project_id,
                target_id=target_id,
                testing_mode=True,
            )
            handle_approval_approve(_ctx(
                conn,
                home,
                "approval.approve",
                {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                k_user=b"\x04" * 32,
            ))
        return apr["approval_id"]

    def test_direct_user_delete_does_not_pin_or_consume_unrelated_approval(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2c-p23a-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                ws = self._insert_task_with_ws(conn, home, "tsk_u1")
                apr_deploy = self._create_approved(
                    conn,
                    home,
                    category="deploy",
                    op="external.deploy",
                    project_id="prj_1",
                    target_id="env_prod",
                    params={"env": "prod"},
                )
                observed = {}
                orig_del = run_handlers._delete_workspace_dir_sync

                def _spy_del(*args, **kwargs):
                    tconn = sqlite3.connect(os.path.join(home, "mesh.db"), isolation_level=None, timeout=10)
                    tconn.row_factory = sqlite3.Row
                    try:
                        raw = tconn.execute(
                            "SELECT value FROM meta WHERE key = ?;",
                            (ws_delete_reservation_key("tsk_u1"),),
                        ).fetchone()["value"]
                        observed["res_doc"] = json.loads(raw)
                        observed["pinned"] = is_approval_pinned_by_ws_reservation(tconn, apr_deploy)
                    finally:
                        tconn.close()
                    return orig_del(*args, **kwargs)

                ctx = _ctx(
                    conn,
                    home,
                    "workspace.delete",
                    {"task_id": "tsk_u1"},
                    principal_id="usr_1",
                    actor_kind="user",
                    via="direct",
                    approval=apr_deploy,
                    request_id="req_user_del",
                )
                with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_spy_del):
                    res = asyncio.run(run_handlers.run_workspace_delete(_DummyServer(), ctx))

                self.assertTrue(res.data["deleted"])
                self.assertFalse(os.path.exists(ws))
                self.assertIsNone(observed["res_doc"]["approval_id"])
                self.assertFalse(observed["pinned"])
                self.assertEqual(
                    conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_deploy,)).fetchone()["status"],
                    "approved",
                )
            finally:
                conn.close()

    def test_recovery_isolates_approval_by_actor_kind_category_and_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2c-p23b-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects (id, name, goal, status, base_branch, integration_branch, "
                        "created_by, created_via, version, created_at, updated_at) "
                        "VALUES ('prj_2', 'P2', 'goal', 'active', 'main', 'mesh/prj_2/integration', 'usr_1', 'direct', 1, ?, ?);",
                        (now, now),
                    )
                for tid, pid in (("tsk_r_user", "prj_1"), ("tsk_r_cat", "prj_1"), ("tsk_r_proj", "prj_1"), ("tsk_r_ok", "prj_1")):
                    ws = self._insert_task_with_ws(conn, home, tid, project_id=pid)
                    shutil.rmtree(ws)

                apr_user_del = self._create_approved(
                    conn, home, category="delete", op="workspace.delete",
                    project_id="prj_1", target_id="tsk_r_user", params={"task_id": "tsk_r_user"},
                )
                apr_wrong_cat = self._create_approved(
                    conn, home, category="deploy", op="external.deploy",
                    project_id="prj_1", target_id="env_stage", params={"env": "stage"},
                )
                apr_other_proj = self._create_approved(
                    conn, home, category="delete", op="workspace.delete",
                    project_id="prj_2", target_id="tsk_other", params={"task_id": "tsk_other"},
                )
                apr_valid = self._create_approved(
                    conn, home, category="delete", op="workspace.delete",
                    project_id="prj_1", target_id="tsk_r_ok", params={"task_id": "tsk_r_ok"},
                )

                cases = [
                    ("tsk_r_user", "user", apr_user_del),
                    ("tsk_r_cat", "agent", apr_wrong_cat),
                    ("tsk_r_proj", "agent", apr_other_proj),
                    ("tsk_r_ok", "agent", apr_valid),
                ]
                with transaction(conn):
                    for tid, actor_kind, apr_id in cases:
                        ws = os.path.join(home, "workspaces", "prj_1", "tasks", tid)
                        marker = os.path.join(home, "run", f"ws_del_{tid}.json")
                        with open(marker, "w", encoding="utf-8") as mf:
                            json.dump({"rmtree_started": True}, mf)
                        conn.execute(
                            "INSERT INTO meta (key, value) VALUES (?, ?);",
                            (ws_delete_reservation_key(tid), json.dumps({
                                "kind": "delete",
                                "rmtree_marker": marker,
                                "task_id": tid,
                                "project_id": "prj_1",
                                "path": ws,
                                "stripped": ws,
                                "real": ws,
                                "matched_root": os.path.join(home, "workspaces"),
                                "branch": f"mesh/prj_1/{tid}",
                                "approval_id": apr_id,
                                "requested_by": "usr_1" if actor_kind == "user" else "agt_lead",
                                "actor_kind": actor_kind,
                                "request_id": f"req_{tid}",
                            })),
                        )

                counts = {}
                recovery._step_ws_reservations(conn, home, counts)
                self.assertEqual(counts.get("workspace_deletes_finalized"), 4)
                status_of = lambda aid: conn.execute("SELECT status FROM approvals WHERE id = ?;", (aid,)).fetchone()["status"]
                self.assertEqual(status_of(apr_user_del), "approved")
                self.assertEqual(status_of(apr_wrong_cat), "approved")
                self.assertEqual(status_of(apr_other_proj), "approved")
                self.assertEqual(status_of(apr_valid), "consumed")
            finally:
                conn.close()


class TestP2_9EventLoopOffloadAndLogTail(unittest.TestCase):
    """P2-9: _read_log_tail, reconcile_merging_task offload, and non-blocking _abort_spawned."""

    def test_read_log_tail_caps_giant_line_and_preserves_pem_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2c-p29a-") as tmp:
            log_path = os.path.join(tmp, "stdout.log")
            # 20 MiB single line with a PEM BEGIN marker straddling byte 2048 so key-block
            # state after the withheld line must still redact subsequent lines.
            huge_line = (
                b"password=hunter22 "
                + (b"A" * 2022)
                + b"-----BEGIN PRIVATE KEY-----"
                + (b"B" * (20 * 1024 * 1024))
                + b"\n"
            )
            with open(log_path, "wb") as f:
                f.write(huge_line)
                f.write(b"raw-key-body secret-second-token\n")
                f.write(b"raw-key-line-2\n")
                f.write(b"-----END PRIVATE KEY-----\n")
                f.write(b"tok1 after-key-visible\n")
                f.write(b"plain-after\n")

            seen_lens = []
            orig_redact = redact

            def _spy_redact(text: str, *args, **kwargs) -> str:
                seen_lens.append(len(text))
                return orig_redact(text, *args, **kwargs)

            with mock.patch.object(run_handlers, "redact", side_effect=_spy_redact):
                got = run_handlers._read_log_tail(log_path, 10)

            self.assertTrue(got["available"])
            self.assertEqual(got["total_lines"], 6)
            self.assertEqual(len(got["lines"]), 6)
            self.assertTrue(seen_lens and max(seen_lens) < 256 * 1024, f"giant line passed unclipped to redact: {seen_lens}")
            self.assertNotIn("hunter22", got["lines"][0])
            self.assertIn("[REDACTED:unbroken output]", got["lines"][0])
            self.assertEqual(got["lines"][1], "[REDACTED]")
            self.assertEqual(got["lines"][2], "[REDACTED]")
            self.assertEqual(got["lines"][3], "[REDACTED]")
            self.assertEqual(got["lines"][4], "[REDACTED] after-key-visible")
            self.assertEqual(got["lines"][5], "plain-after")

    def test_read_log_tail_shared_dashes_end_begin_preserves_key_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2c-p29d-") as tmp:
            # 1. Shared dashes after byte 2048: END-----BEGIN transitions False->True, True->False.
            p1 = os.path.join(tmp, "shared_after.log")
            with open(p1, "wb") as f:
                f.write(
                    b"prefix "
                    + (b"A" * 4000)
                    + b"-----END PRIVATE KEY-----BEGIN PRIVATE KEY-----"
                    + (b"B" * (140 * 1024))
                    + b"\n"
                )
                f.write(b"leaked-if-in-key-missed second-tok\n")
                f.write(b"still-in-key-line-2\n")
                f.write(b"-----END PRIVATE KEY-----\n")
                f.write(b"tok1 visible-after\n")
            got1 = run_handlers._read_log_tail(p1, 10)
            self.assertEqual(got1["lines"][1], "[REDACTED]")
            self.assertEqual(got1["lines"][2], "[REDACTED]")
            self.assertEqual(got1["lines"][3], "[REDACTED]")
            self.assertEqual(got1["lines"][4], "[REDACTED] visible-after")

            # 2. END ends right at byte 2048 ([2023, 2048)) and BEGIN shares its dashes ([2043, 2070)).
            p2 = os.path.join(tmp, "shared_at_2048.log")
            with open(p2, "wb") as f:
                f.write(
                    (b"A" * 2023)
                    + b"-----END PRIVATE KEY-----BEGIN PRIVATE KEY-----"
                    + (b"B" * (140 * 1024))
                    + b"\n"
                )
                f.write(b"secret-body second-tok\n")
                f.write(b"still-secret-line-2\n")
            got2 = run_handlers._read_log_tail(p2, 10)
            self.assertEqual(got2["lines"][1], "[REDACTED]")
            self.assertEqual(got2["lines"][2], "[REDACTED]")

            # 3. Starting inside a key block, shared END-----BEGIN closes the key block (True->False).
            p3 = os.path.join(tmp, "shared_when_in_key.log")
            with open(p3, "wb") as f:
                f.write(b"-----BEGIN PRIVATE KEY-----\n")
                f.write(
                    (b"A" * 4000)
                    + b"-----END PRIVATE KEY-----BEGIN PRIVATE KEY-----"
                    + (b"B" * (140 * 1024))
                    + b"\n"
                )
                f.write(b"first-tok plain-outside-key\n")
                f.write(b"second-line-outside-key\n")
            got3 = run_handlers._read_log_tail(p3, 10)
            self.assertEqual(got3["lines"][2], "[REDACTED] plain-outside-key")
            self.assertEqual(got3["lines"][3], "second-line-outside-key")

    def test_reconcile_merging_task_offloads_off_event_loop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2c-p29b-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                integ = os.path.join(home, "workspaces", "prj_1", "integration")
                os.makedirs(integ, mode=0o700, exist_ok=True)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "branch, merge_json, created_by, created_at, updated_at) "
                        "VALUES ('tsk_rec', 'prj_1', 'Rec', 'S', 'code', 'merging', 'dev', 600, 3, "
                        "'mesh/prj_1/tsk_rec', ?, 'usr_1', ?, ?);",
                        (json.dumps({"mrg": "mrg_1", "pre_head": "0" * 40, "started_at": now}), now, now),
                    )

                srv = DaemonServer(home=home, testing_mode=True)
                srv.db_conn = conn
                srv.log_error = lambda _m: None

                entered = threading.Event()
                release = threading.Event()
                info = {}

                def _slow_decide(_integ, _tid, _branch, _pre_head, run_dir=None):
                    info["worker_tid"] = threading.get_ident()
                    entered.set()
                    release.wait(timeout=5.0)
                    return {"outcome": "failed", "reason": "interrupted", "pre_head": "0" * 40, "details": {}}

                async def _run():
                    info["loop_tid"] = threading.get_ident()
                    with mock.patch.object(merge, "decide_recovery", side_effect=_slow_decide):
                        srv.execute_effects([Effect(kind="reconcile_merging_task", params={"task_id": "tsk_rec"})])
                        # execute_effects must return immediately before decide_recovery completes
                        self.assertTrue(len(srv._merge_jobs) == 1)
                        # Wait on event loop for worker thread to enter decide_recovery
                        deadline = time.monotonic() + 5.0
                        while not entered.is_set() and time.monotonic() < deadline:
                            await asyncio.sleep(0.01)
                        self.assertTrue(entered.is_set(), "decide_recovery did not start in worker thread")
                        # Event loop remains responsive while decide_recovery is blocked
                        await asyncio.sleep(0.01)
                        release.set()
                        await asyncio.gather(*list(srv._merge_jobs))

                asyncio.run(_run())
                self.assertNotEqual(info["worker_tid"], info["loop_tid"])
                row = conn.execute("SELECT status FROM tasks WHERE id = 'tsk_rec';").fetchone()
                self.assertEqual(row["status"], "merge_failed")
            finally:
                conn.close()

    def test_abort_spawned_nonblocking_on_event_loop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r2c-p29c-") as tmp:
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                ab = supervisor._SpawnAbort(
                    proc,
                    "run_abort_loop",
                    ("tsk_x", "prj_1", 1, 1, 3, "err"),
                    home=tmp,
                    kind="task",
                )
                pass_calls = {"n": 0}

                def _still_alive(_ab):
                    pass_calls["n"] += 1
                    return False, {proc.pid: 1}

                async def _run():
                    t0 = time.monotonic()
                    with mock.patch.object(supervisor, "SPAWN_ABORT_WAIT_S", 1.5), \
                         mock.patch.object(supervisor, "_abort_collect", return_value=True), \
                         mock.patch.object(supervisor, "_abort_kill_pass", side_effect=_still_alive):
                        done = supervisor._abort_spawned(ab)
                    return done, time.monotonic() - t0

                done, elapsed = asyncio.run(_run())
                self.assertFalse(done)
                self.assertEqual(pass_calls["n"], 1)
                self.assertLess(elapsed, 0.5)
            finally:
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
