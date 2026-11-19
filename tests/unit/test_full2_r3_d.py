"""Regression tests for full2-r3.md Group D findings:
- P1-7 (mesh/core/lead_actions.py, mesh/daemon/server.py): task.close in lead actions,
  empty/None scope run_token idempotency partition, dispatch_internal_request dry_run,
  pre-idempotency check_authz, and top-level envelope field hashing.
- P2-10 (mesh/core/lead_actions.py:60, mesh/core/policy.py:10): \\Z anchors on
  IDEMPOTENCY_KEY_RE and POSITIVE_INT_RE.
- P2-9 (mesh/daemon/handlers/event.py): event.tail stream re-check checks active run status
  for run_token.
- P1-12 (mesh/daemon/handlers/event.py): strict string/int/range validation for
  project, task, type, since, limit.
- P2-5 (mesh/daemon/supervisor.py): lease renewal while waiting on _submit_git_state, and
  refreshing deliverable.git after leftovers exit.
- P2-7 (mesh/daemon/terminate.py): terminate.begin merges newly handed or raced seen PIDs
  into existing Termination.
- P2-8 (mesh/daemon/supervisor.py): event-loop _abort_spawned defers run.kill_failed until
  SPAWN_ABORT_WAIT_S elapses in retry_spawn_aborts.
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import policy, repos
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.lead_actions import (
    ALLOWED_ACTION_CMDS,
    IDEMPOTENCY_KEY_RE,
    action_request_args,
    parse_actions,
)
from mesh.core.proc import get_proc_stat
from mesh.daemon import lead_turns, server, supervisor, terminate
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.event import _stream_still_authorized, handle_event_list, handle_event_tail_live
from mesh.daemon.merge import reviewed_head
from mesh.errors import MeshError
from tests.gitfixture import GitFixture

MESH_PROC_LOCK = "shared"


def _wait_until(pred, timeout: float = 10.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def _kill_proc(pid: int, starttime: int) -> None:
    try:
        info = get_proc_stat(pid)
        if info is not None and int(info["starttime"]) == int(starttime):
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _seed_lead_db(tmp_dir: str):
    conn = init_db(os.path.join(tmp_dir, "mesh.db"))
    now = now_iso_utc()
    with transaction(conn):
        conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (now,))
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'ag-lead', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) "
            "VALUES ('agt_lead', 'fake', 'launch', 1, '[\"relay\"]', 1, 'edit', ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_exec', 'agent', 'ag-exec', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) "
            "VALUES ('agt_exec', 'fake', 'launch', 0, '[]', 1, 'edit', ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
            "VALUES ('prj_1', 'P1', 'goal', 'active', 'usr_1', 'direct', ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
            (now,),
        )
        run_dir = os.path.join(tmp_dir, "runs", "run_lt_1")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        conn.execute(
            "INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, status, boot_id, "
            "run_dir, started_at, lead_assignment_id) "
            "VALUES ('run_lt_1', 'lead_turn', 'prj_1', 'agt_lead', 1, 'fake', 'running', "
            "'boot_1', ?, ?, 'lead_1');",
            (run_dir, now),
        )
    return conn, now


class TestP1_7LeadActionsAndDispatchInternal(unittest.TestCase):
    """P1-7: task.close in lead_actions, empty run_token scope partition, and dispatch_internal_request."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r3d-p17-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for sub in ("runs", "workspaces", "run", "logs"):
            os.makedirs(os.path.join(self.tmp, sub), mode=0o700, exist_ok=True)
        self.conn, self.now = _seed_lead_db(self.tmp)
        self.addCleanup(self.conn.close)
        self.lead_principal = {
            "id": "agt_lead",
            "kind": "agent",
            "name": "ag-lead",
            "capabilities": ["relay"],
            "scope": {
                "role": "lead",
                "project_id": "prj_1",
                "lead_assignment_id": "lead_1",
                "run_id": "run_lt_1",
            },
        }

    def test_lead_actions_allows_task_close_and_maps_task_arg(self) -> None:
        self.assertIn("task.close", ALLOWED_ACTION_CMDS)
        parsed = parse_actions(
            '```mesh-actions\n[{"cmd": "task.close", "args": {"task": "tsk_1", "superseded_by": "tsk_2"}}]\n```'
        )
        self.assertIsNone(parsed.error)
        self.assertEqual(len(parsed.actions), 1)
        self.assertIsNone(parsed.actions[0].error)
        mapped = action_request_args("task.close", parsed.actions[0].args, "prj_1")
        self.assertEqual(mapped, {"task_id": "tsk_1", "superseded_by": "tsk_2"})

        # Execute task.close via dispatch_internal_request / lead turn actions
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, "
                "timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
                "VALUES ('tsk_2', 'prj_1', 'Winner', 's', 'code', 'merged', 'edit', 600, 1, 1, 'usr_1', ?, ?);",
                (self.now, self.now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, "
                "timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
                "VALUES ('tsk_1', 'prj_1', 'Old', 's', 'code', 'accepted', 'edit', 600, 1, 1, 'usr_1', ?, ?);",
                (self.now, self.now),
            )
            env = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_close_1",
                    home=self.tmp,
                    cmd="task.close",
                    args=mapped,
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:0",
            )
        self.assertTrue(env["ok"], env)
        self.assertEqual(env["exit_code"], 0)
        row = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_1';").fetchone()
        self.assertEqual(row["status"], "cancelled")
        ev = self.conn.execute("SELECT payload_json FROM events WHERE type = 'task.closed' AND task_id = 'tsk_1';").fetchone()
        self.assertIsNotNone(ev)
        self.assertEqual(json.loads(ev["payload_json"])["superseded_by"], "tsk_2")

    def test_idempotency_partition_empty_and_none_scope_on_run_token(self) -> None:
        p_empty = {"id": "agt_1", "scope": {}}
        p_none = {"id": "agt_1", "scope": None}
        p_missing = {"id": "agt_1"}
        p_real = {"id": "agt_1", "scope": {"role": "lead", "project_id": "prj_1", "run_id": "run_1"}}

        part_empty = server.idempotency_partition(p_empty, "run_token")
        part_none = server.idempotency_partition(p_none, "run_token")
        part_missing = server.idempotency_partition(p_missing, "run_token")
        part_real = server.idempotency_partition(p_real, "run_token")

        self.assertTrue(part_empty.startswith("agt_1#scope:"), part_empty)
        self.assertEqual(len(part_empty), len("agt_1#scope:") + 32)
        self.assertEqual(part_empty, part_none)
        self.assertEqual(part_empty, part_missing)
        self.assertNotEqual(part_empty, "agt_1")
        self.assertNotEqual(part_empty, "agt_1#relayed")
        self.assertNotEqual(part_empty, part_real)
        self.assertEqual(server.idempotency_partition(p_empty, "token"), "agt_1")
        self.assertEqual(server.idempotency_partition(p_empty, "direct"), "agt_1")
        self.assertEqual(server.idempotency_partition(p_empty, "relayed"), "agt_1#relayed")

    def test_dispatch_internal_request_dry_run_rolls_back_and_does_not_store_idem(self) -> None:
        with transaction(self.conn):
            env = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_dry_1",
                    home=self.tmp,
                    cmd="task.create",
                    args={"project": "prj_1", "title": "Dry Task", "spec": "spec"},
                    dry_run=True,
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:dry",
            )
        self.assertTrue(env["ok"], env)
        self.assertTrue(env["dry_run"], env)
        self.assertIn("would", env["data"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM idempotency;").fetchone()[0], 0)

        # Even EXIT_APPROVAL_REQUIRED (rc 7) must roll back under dry_run=True
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('approval.policy.task_retry', '\"required\"', ?, 'usr_1') "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json;",
                (self.now,),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, "
                "timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
                "VALUES ('tsk_cap', 'prj_1', 'Capped', 's', 'general', 'failed', 'edit', 600, 1, 1, 'usr_1', ?, ?);",
                (self.now, self.now),
            )
            env_apr = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_dry_apr",
                    home=self.tmp,
                    cmd="task.retry",
                    args={"task_id": "tsk_cap"},
                    dry_run=True,
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:dry_apr",
            )
        self.assertFalse(env_apr["ok"])
        self.assertEqual(env_apr["exit_code"], c.EXIT_APPROVAL_REQUIRED)
        self.assertTrue(env_apr.get("dry_run"), env_apr)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM approvals;").fetchone()[0], 0)

        # Dry run must not replay or collide with an existing stored idempotency key
        with transaction(self.conn):
            env_real = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_real_1",
                    home=self.tmp,
                    cmd="task.create",
                    args={"project": "prj_1", "title": "Real Task", "spec": "spec"},
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:shared_k",
            )
            self.assertTrue(env_real["ok"], env_real)
            env_dry2 = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_dry_2",
                    home=self.tmp,
                    cmd="task.create",
                    args={"project": "prj_1", "title": "Different Dry Task", "spec": "spec"},
                    dry_run=True,
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:shared_k",
            )
            self.assertTrue(env_dry2["ok"], env_dry2)
            self.assertTrue(env_dry2["dry_run"], env_dry2)
            self.assertFalse(env_dry2["idempotent_replay"], env_dry2)

        # task.merge with dry_run=True must not leak start_task_merge effect
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET repo_path = '/tmp/r' WHERE id = 'prj_1';")
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, "
                "workspace_path, branch, timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
                "VALUES ('tsk_acc', 'prj_1', 'Acc', 's', 'code', 'accepted', 'edit', '/tmp/wt', "
                "'mesh/prj_1/tsk_acc', 600, 1, 1, 'usr_1', ?, ?);",
                (self.now, self.now),
            )
            ctx_m = HandlerContext(
                conn=self.conn,
                principal=dict(self.lead_principal),
                actor_kind="agent",
                via="run_token",
                request_id="req_dry_m",
                home=self.tmp,
                cmd="task.merge",
                args={"task_id": "tsk_acc"},
                dry_run=True,
                testing_mode=True,
            )
            env_m = server.dispatch_internal_request(ctx_m, idempotency_key="act:run_lt_1:dry_m")
        self.assertTrue(env_m["ok"], env_m)
        self.assertTrue(env_m["dry_run"], env_m)
        self.assertEqual(ctx_m.effects, [])

    def test_dispatch_internal_request_checks_authz_before_idempotency(self) -> None:
        with transaction(self.conn):
            env1 = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_a1",
                    home=self.tmp,
                    cmd="task.create",
                    args={"project": "prj_1", "title": "First", "spec": "s1"},
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:authz",
            )
            self.assertTrue(env1["ok"], env1)

            # Supersede lead_1; reusing key with DIFFERENT args must return LEAD_SUPERSEDED (rc 9), not IDEMPOTENCY_KEY_REUSED (rc 6)
            self.conn.execute("UPDATE lead_assignments SET ended_at = ? WHERE id = 'lead_1';", (self.now,))
            env2 = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_a2",
                    home=self.tmp,
                    cmd="task.create",
                    args={"project": "prj_1", "title": "Tampered", "spec": "s2"},
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:authz",
            )
        self.assertFalse(env2["ok"])
        self.assertEqual((env2["exit_code"], env2["error"]["code"]), (c.EXIT_STALE, c.LEAD_SUPERSEDED))

    def test_dispatch_internal_request_hashes_top_level_fields(self) -> None:
        with transaction(self.conn):
            args = {"project": "prj_1", "title": "TopFields", "spec": "s"}
            env1 = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_t1",
                    home=self.tmp,
                    cmd="task.create",
                    args=dict(args),
                    evidence_ref="ev-1",
                    if_version=1,
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:top",
            )
            self.assertTrue(env1["ok"], env1)

            # Altered evidence_ref -> IDEMPOTENCY_KEY_REUSED
            env_ev = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_t2",
                    home=self.tmp,
                    cmd="task.create",
                    args=dict(args),
                    evidence_ref="ev-2",
                    if_version=1,
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:top",
            )
            self.assertFalse(env_ev["ok"], env_ev)
            self.assertEqual((env_ev["exit_code"], env_ev["error"]["code"]), (c.EXIT_CONFLICT, c.IDEMPOTENCY_KEY_REUSED))

            # Altered if_version -> IDEMPOTENCY_KEY_REUSED
            env_iv = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(self.lead_principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_t3",
                    home=self.tmp,
                    cmd="task.create",
                    args=dict(args),
                    evidence_ref="ev-1",
                    if_version=2,
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_1:top",
            )
            self.assertFalse(env_iv["ok"], env_iv)
            self.assertEqual((env_iv["exit_code"], env_iv["error"]["code"]), (c.EXIT_CONFLICT, c.IDEMPOTENCY_KEY_REUSED))

            # Relayed user_statement must also be included in hash
            self.conn.execute("UPDATE agents SET capabilities_json = '[\"relay\"]' WHERE id = 'agt_exec';")
            rel_principal = {"id": "agt_exec", "kind": "agent", "name": "ag-exec", "capabilities": ["relay"]}
            rel_args = {
                "category": "deploy",
                "summary": "deploy dev",
                "operation": {"env": "dev"},
                "project_id": "prj_1",
            }
            env_r1 = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(rel_principal),
                    actor_kind="agent",
                    via="relayed",
                    relayed_by="ag-exec",
                    user_statement="Human asked for deploy 1",
                    request_id="req_r1",
                    home=self.tmp,
                    cmd="approval.request",
                    args=dict(rel_args),
                    testing_mode=True,
                ),
                idempotency_key="act:rel:1",
            )
            self.assertTrue(env_r1["ok"], env_r1)
            env_r2 = server.dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(rel_principal),
                    actor_kind="agent",
                    via="relayed",
                    relayed_by="ag-exec",
                    user_statement="Human asked for DIFFERENT deploy",
                    request_id="req_r2",
                    home=self.tmp,
                    cmd="approval.request",
                    args=dict(rel_args),
                    testing_mode=True,
                ),
                idempotency_key="act:rel:1",
            )
            self.assertFalse(env_r2["ok"], env_r2)
            self.assertEqual((env_r2["exit_code"], env_r2["error"]["code"]), (c.EXIT_CONFLICT, c.IDEMPOTENCY_KEY_REUSED))


class TestP2_10RegexZAnchors(unittest.TestCase):
    """P2-10: IDEMPOTENCY_KEY_RE and POSITIVE_INT_RE must use \\Z to reject trailing newline."""

    def test_idempotency_key_re_rejects_trailing_newline(self) -> None:
        self.assertIsNotNone(IDEMPOTENCY_KEY_RE.match("valid-key_1:0"))
        self.assertIsNone(IDEMPOTENCY_KEY_RE.match("valid-key_1:0\n"))
        self.assertIsNone(IDEMPOTENCY_KEY_RE.match("k\n"))

    def test_positive_int_re_rejects_trailing_newline(self) -> None:
        self.assertIsNotNone(policy.POSITIVE_INT_RE.match("42"))
        self.assertIsNone(policy.POSITIVE_INT_RE.match("42\n"))
        self.assertEqual(policy.parse_cli_positive_int("42"), 42)
        with self.assertRaises(MeshError) as cm:
            policy.parse_cli_positive_int("42\n")
        self.assertEqual(cm.exception.code, c.INVALID_CONCURRENCY)


class TestP2_9AndP1_12EventHandler(unittest.TestCase):
    """P2-9 (event.tail run_token active-run check) and P1-12 (event.py input validation)."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r3d-evt-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for sub in ("runs", "workspaces", "run", "logs"):
            os.makedirs(os.path.join(self.tmp, sub), mode=0o700, exist_ok=True)
        self.conn, self.now = _seed_lead_db(self.tmp)
        self.addCleanup(self.conn.close)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at) "
                "VALUES ('cred_rt_1', 'agt_lead', 'run_token', 'lkp1', ?, ?, ?);",
                (
                    b"hash1",
                    json.dumps({
                        "role": "lead",
                        "project_id": "prj_1",
                        "lead_assignment_id": "lead_1",
                        "run_id": "run_lt_1",
                    }),
                    self.now,
                ),
            )
        self.run_token_principal = {
            "id": "agt_lead",
            "kind": "agent",
            "name": "ag-lead",
            "credential_id": "cred_rt_1",
            "capabilities": ["relay"],
            "scope": {
                "role": "lead",
                "project_id": "prj_1",
                "lead_assignment_id": "lead_1",
                "run_id": "run_lt_1",
            },
        }

    def test_p2_9_stream_still_authorized_rejects_non_active_run_token(self) -> None:
        ctx = HandlerContext(
            conn=self.conn,
            principal=dict(self.run_token_principal),
            actor_kind="agent",
            via="run_token",
            request_id="req_tail_1",
            home=self.tmp,
            cmd="event.tail",
            args={"project": "prj_1"},
            credential_id="cred_rt_1",
        )
        self.assertIsNotNone(_stream_still_authorized(ctx, "prj_1", None))

        # Run exits while credential row itself is not yet revoked (e.g. seen hold window)
        for dead_status in ("exited", "killed", "lost", "spawn_failed"):
            with transaction(self.conn):
                self.conn.execute("UPDATE runs SET status = ? WHERE id = 'run_lt_1';", (dead_status,))
            self.assertIsNone(
                _stream_still_authorized(ctx, "prj_1", None),
                f"expected stream authz to stop when run status is {dead_status}",
            )

    def test_p1_12_event_list_and_tail_reject_non_string_filters_and_bad_ints(self) -> None:
        user_ctx_base = dict(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_bad",
            home=self.tmp,
        )
        for param in ("project", "task", "type"):
            for bad_val in (["x"], 123, True, {"k": "v"}):
                with self.subTest(param=param, bad_val=bad_val):
                    ctx_l = HandlerContext(cmd="event.list", args={param: bad_val}, **user_ctx_base)
                    with self.assertRaises(MeshError) as cm_l:
                        handle_event_list(ctx_l)
                    self.assertEqual((cm_l.exception.exit_code, cm_l.exception.code), (c.EXIT_USAGE, c.INVALID_ARGUMENT))

                    ctx_t = HandlerContext(cmd="event.tail", args={param: bad_val, "timeout": "1s"}, **user_ctx_base)
                    with self.assertRaises(MeshError) as cm_t:
                        asyncio.run(handle_event_tail_live(mock.Mock(), ctx_t, None, mock.Mock()))
                    self.assertEqual((cm_t.exception.exit_code, cm_t.exception.code), (c.EXIT_USAGE, c.INVALID_ARGUMENT))

        for bad_since in (10**20, -1, True, 1.5, "abc"):
            with self.subTest(bad_since=bad_since):
                ctx_l = HandlerContext(cmd="event.list", args={"since": bad_since}, **user_ctx_base)
                with self.assertRaises(MeshError) as cm_l:
                    handle_event_list(ctx_l)
                self.assertEqual((cm_l.exception.exit_code, cm_l.exception.code), (c.EXIT_USAGE, c.INVALID_ARGUMENT))

                ctx_t = HandlerContext(cmd="event.tail", args={"since": bad_since, "timeout": "1s"}, **user_ctx_base)
                with self.assertRaises(MeshError) as cm_t:
                    asyncio.run(handle_event_tail_live(mock.Mock(), ctx_t, None, mock.Mock()))
                self.assertEqual((cm_t.exception.exit_code, cm_t.exception.code), (c.EXIT_USAGE, c.INVALID_ARGUMENT))

        with self.assertRaises(MeshError) as cm_lim:
            handle_event_list(HandlerContext(cmd="event.list", args={"limit": 1.5}, **user_ctx_base))
        self.assertEqual((cm_lim.exception.exit_code, cm_lim.exception.code), (c.EXIT_USAGE, c.INVALID_ARGUMENT))


class TestP2_5SupervisorLeaseAndLeftoversGitRefresh(unittest.TestCase):
    """P2-5: _watch_task_run renews lease while waiting on _submit_git_state, and refreshes deliverable.git after leftovers die."""

    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.repo = self.fixture.make_k0("P25")
        self.base = self.fixture.head(self.repo, "main")

        self.home = self.fixture.path("home")
        for sub in ("workspaces", "run", "runs", "logs", "merges"):
            os.makedirs(os.path.join(self.home, sub), mode=0o700, exist_ok=True)
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)

        self.project_id = "prj_p25"
        self.task_id = "tsk_p25"
        self.run_id = f"run_p25_{os.getpid()}"
        self.branch = f"mesh/{self.project_id}/{self.task_id}"
        self.ws_root = os.path.join(self.home, "workspaces")
        repos.setup_integration(self.repo, self.project_id, self.base, self.ws_root)
        self.integ_branch = repos.integration_branch_name(self.project_id)

        self.ws_path = os.path.join(self.ws_root, self.project_id, "tasks", self.task_id)
        os.makedirs(os.path.dirname(self.ws_path), mode=0o700, exist_ok=True)
        self.fixture.git(self.repo, "worktree", "add", "-b", self.branch, self.ws_path, self.base)
        with open(os.path.join(self.ws_path, "f1.txt"), "w", encoding="utf-8") as f:
            f.write("initial work\n")
        self.fixture.git(self.ws_path, "add", "f1.txt")
        self.fixture.git(self.ws_path, "commit", "-q", "-m", "initial commit")
        self.first_head = self.fixture.head(self.ws_path)

        self.run_dir = os.path.join(self.home, "runs", self.run_id)
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        self._reset_supervisor_state()
        self.addCleanup(self._reset_supervisor_state)

    def _reset_supervisor_state(self) -> None:
        terminate._active.clear()
        supervisor._CLEAR.clear()
        with supervisor._WS_PREP_LOCK:
            supervisor._GIT_INFO.clear()
        supervisor._SEEN.clear()
        supervisor._SEEN_UNUSABLE.clear()
        supervisor._SEEN_HOLD_TOKEN.clear()
        supervisor._SEEN_MARKER.clear()
        supervisor._SEEN_SIG.clear()
        supervisor._SEEN_PERSISTED.clear()
        supervisor._SEEN_REASON.clear()
        supervisor._SEEN_EVENT_PENDING.clear()
        supervisor._SEEN_EVENT_DONE.clear()
        supervisor._CLOSE_REFUSED_LOGGED.clear()
        supervisor._SPAWN_ABORTS.clear()
        supervisor._WRAPPERS.clear()

    def _seed_code_run(self) -> tuple[int, int]:
        wp = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        wpid = wp.pid
        wst = int(get_proc_stat(wpid)["starttime"])
        wp.kill()
        wp.wait(timeout=5)
        self.addCleanup(_kill_proc, wpid, wst)

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (now,))
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'ag1', ?);", (now,))
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_1', 'fake', 'launch', 1, ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, base_commit, "
                "integration_branch, created_by, created_via, created_at, updated_at) "
                "VALUES (?, 'P25', 'g', 'active', ?, 'main', ?, ?, 'usr_1', 'direct', ?, ?);",
                (self.project_id, self.repo, self.base, self.integ_branch, now, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "workspace_path, branch, base_commit, timeout_s, max_attempts, attempt, created_by, "
                "created_at, updated_at) "
                "VALUES (?, ?, 'T25', 'spec', 'code', 'running', 'agt_1', 'edit', ?, ?, ?, 600, 1, 1, 'usr_1', ?, ?);",
                (self.task_id, self.project_id, self.ws_path, self.branch, self.base, now, now),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
                "VALUES (?, 'task', ?, ?, 'agt_1', 1, 1, 'fake', 'running', 'boot_1', ?, ?, ?, ?, ?);",
                (self.run_id, self.task_id, self.project_id, wpid, wpid, wst, self.run_dir, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, "
                "expires_at, heartbeat_at) VALUES (?, ?, 'meshd', 'boot_1', 'agt_1', 1, ?, ?, ?);",
                (f"task:{self.task_id}", self.run_id, now, now_iso_utc(time.time() + 3600), now),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('kill.grace_s', '1', ?, 'test');",
                (now,),
            )
        with open(os.path.join(self.run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wpid, "starttime": wst}, f)
        with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('{"summary": "done"}\n')
        with open(os.path.join(self.run_dir, "exit.json"), "w", encoding="utf-8") as f:
            json.dump({"exit_code": 0, "signal": None, "ended_at": now}, f)
        return wpid, wst

    def test_watch_task_run_renews_lease_while_waiting_for_submit_git_state(self) -> None:
        self._seed_code_run()
        old_hb = now_iso_utc(time.time() - 30)
        old_exp = now_iso_utc(time.time() + 5)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE key = ?;",
                (old_hb, old_exp, f"task:{self.task_id}"),
            )
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        with mock.patch.object(supervisor, "_submit_git_state", return_value=None):
            res = supervisor._watch_one(self.conn, self.home, run, testing_mode=True)
        self.assertIsNone(res)
        lease = self.conn.execute(
            "SELECT heartbeat_at, expires_at FROM leases WHERE key = ?;", (f"task:{self.task_id}",)
        ).fetchone()
        self.assertGreater(lease["heartbeat_at"], old_hb)
        self.assertGreater(lease["expires_at"], old_exp)

    def test_leftovers_exit_refreshes_deliverable_git(self) -> None:
        wpid, wst = self._seed_code_run()
        # Start a leftover process carrying MESH_RUN_ID while wrapper is gone
        env = dict(os.environ)
        env["MESH_RUN_ID"] = self.run_id
        leftover = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            env=env,
        )
        lst = int(get_proc_stat(leftover.pid)["starttime"])
        self.addCleanup(lambda: (_kill_proc(leftover.pid, lst), leftover.wait()))

        # Watch pass 1: collects initial git (first_head) in background
        supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True)
        self.assertTrue(
            _wait_until(lambda: supervisor._GIT_INFO.get(self.run_id, {}).get("done") is True, timeout=5.0)
        )

        # Watch pass 2: sees leftover, auto-submits task with first_head, begins leftovers termination
        supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True)
        task_mid = self.conn.execute("SELECT status, deliverable_json FROM tasks WHERE id = ?;", (self.task_id,)).fetchone()
        self.assertEqual(task_mid["status"], "submitted")
        self.assertEqual(reviewed_head(task_mid["deliverable_json"]), self.first_head)
        self.assertIsNotNone(terminate.get(self.run_id))

        # Before leftover exits, it produces a second commit in ws_path
        with open(os.path.join(self.ws_path, "f2.txt"), "w", encoding="utf-8") as f:
            f.write("flushed by leftover\n")
        self.fixture.git(self.ws_path, "add", "f2.txt")
        self.fixture.git(self.ws_path, "commit", "-q", "-m", "leftover commit")
        second_head = self.fixture.head(self.ws_path)
        self.assertNotEqual(second_head, self.first_head)

        # Now leftover dies and termination advances to done
        leftover.kill()
        leftover.wait(timeout=5)

        def step_until_closed() -> bool:
            r = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
            if r["status"] not in ("starting", "running"):
                return True
            supervisor.advance_terminations(self.conn, self.home, testing_mode=True)
            return False

        self.assertTrue(_wait_until(step_until_closed, timeout=15.0, step=0.05), "run never closed")
        task_final = self.conn.execute("SELECT status, deliverable_json FROM tasks WHERE id = ?;", (self.task_id,)).fetchone()
        self.assertEqual(task_final["status"], "submitted")
        self.assertEqual(reviewed_head(task_final["deliverable_json"]), second_head)


class TestP2_7TerminateBeginMergesSeen(unittest.TestCase):
    """P2-7: terminate.begin merges newly provided or raced seen entries into existing Termination."""

    def setUp(self) -> None:
        terminate._active.clear()
        self.addCleanup(terminate._active.clear)

    def test_reentry_merges_new_seen_and_clears_empty_scan_at(self) -> None:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        st = int(get_proc_stat(p.pid)["starttime"])
        self.addCleanup(lambda: (_kill_proc(p.pid, st), p.wait()))

        t1 = terminate.begin("run_merge_1", None, None, "leftovers", 5.0, seen={})
        t1.empty_scan_at = time.monotonic()
        t1.phase = "failed"
        t1.next_retry = time.monotonic() + 30.0

        t2 = terminate.begin("run_merge_1", None, None, "leftovers", 5.0, seen={p.pid: st})
        self.assertIs(t2, t1)
        self.assertEqual(t1.seen.get(p.pid), st)
        self.assertIsNone(t1.empty_scan_at)
        self.assertEqual(t1.next_retry, 0.0)

    def test_concurrent_begin_merges_seen_from_loser_thread(self) -> None:
        p1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        st1 = int(get_proc_stat(p1.pid)["starttime"])
        self.addCleanup(lambda: (_kill_proc(p1.pid, st1), p1.wait()))
        p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        st2 = int(get_proc_stat(p2.pid)["starttime"])
        self.addCleanup(lambda: (_kill_proc(p2.pid, st2), p2.wait()))

        barrier = threading.Barrier(2)
        out = []
        lock = threading.Lock()

        def fake_targets(*args, **kwargs):
            barrier.wait(timeout=5.0)
            return {}

        def worker(seen_arg):
            t = terminate.begin("run_race_merge", None, None, "cancelled", 5.0, seen=seen_arg)
            with lock:
                out.append(t)

        with mock.patch.object(terminate, "run_targets", fake_targets):
            th1 = threading.Thread(target=worker, args=({p1.pid: st1},))
            th2 = threading.Thread(target=worker, args=({p2.pid: st2},))
            th1.start()
            th2.start()
            th1.join(timeout=10.0)
            th2.join(timeout=10.0)

        self.assertEqual(len(out), 2)
        self.assertIs(out[0], out[1])
        self.assertEqual(out[0].seen.get(p1.pid), st1)
        self.assertEqual(out[0].seen.get(p2.pid), st2)


class TestP2_8SpawnAbortKillFailedDeferral(unittest.TestCase):
    """P2-8: event-loop handle_spawn_failure defers run.kill_failed until SPAWN_ABORT_WAIT_S elapses."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r3d-p28-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for sub in ("runs", "workspaces", "run", "logs"):
            os.makedirs(os.path.join(self.tmp, sub), mode=0o700, exist_ok=True)
        self.conn = init_db(os.path.join(self.tmp, "mesh.db"))
        self.addCleanup(self.conn.close)
        now = now_iso_utc()
        self.run_dir = os.path.join(self.tmp, "runs", "run_p28")
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        with transaction(self.conn):
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (now,))
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'ag1', ?);", (now,))
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_1', 'fake', 'launch', 1, ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P28', 'g', 'active', 'usr_1', 'direct', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
                "VALUES ('tsk_1', 'prj_1', 'T28', 's', 'general', 'running', 'agt_1', 'edit', 600, 3, 1, 'usr_1', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, run_dir, started_at) "
                "VALUES ('run_p28', 'task', 'tsk_1', 'prj_1', 'agt_1', 1, 1, 'fake', 'starting', 'boot_1', ?, ?);",
                (self.run_dir, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, "
                "expires_at, heartbeat_at) VALUES ('task:tsk_1', 'run_p28', 'meshd', 'boot_1', 'agt_1', 1, ?, ?, ?);",
                (now, now_iso_utc(time.time() + 3600), now),
            )
        supervisor._SPAWN_ABORTS.clear()
        supervisor._WRAPPERS.clear()
        self.addCleanup(supervisor._SPAWN_ABORTS.clear)
        self.addCleanup(supervisor._WRAPPERS.clear)

    def _run_event_loop_failure(self, proc):
        async def _go():
            return supervisor.handle_spawn_failure(
                self.conn,
                self.tmp,
                "run_p28",
                self.run_dir,
                proc,
                RuntimeError("boom"),
                kind="task",
                fail_args=("tsk_1", "prj_1", 1, 1, 3),
                testing_mode=True,
            )

        asyncio.run(_go())

    def test_event_loop_transient_exit_mmap_window_emits_no_kill_failed(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        st = int(get_proc_stat(proc.pid)["starttime"])
        self.addCleanup(lambda: (_kill_proc(proc.pid, st), proc.wait()))

        # First pass inside event loop sees transient survivor; retry sees process gone.
        passes = {"n": 0}

        def fake_pass(ab):
            passes["n"] += 1
            if passes["n"] == 1:
                return False, {proc.pid: st}
            proc.kill()
            proc.wait(timeout=5)
            return True, {}

        with mock.patch.object(supervisor, "SPAWN_ABORT_WAIT_S", 0.5), \
             mock.patch.object(supervisor, "_abort_collect", return_value=True), \
             mock.patch.object(supervisor, "_abort_kill_pass", side_effect=fake_pass):
            self._run_event_loop_failure(proc)
            # Immediately after handle_spawn_failure: NO spurious run.kill_failed!
            kf1 = self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.kill_failed';").fetchone()[0]
            self.assertEqual(kf1, 0)
            self.assertIn("run_p28", supervisor._SPAWN_ABORTS)

            closed = supervisor.retry_spawn_aborts(self.conn, self.tmp, testing_mode=True)
            self.assertEqual(closed, ["run_p28"])

        kf2 = self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.kill_failed';").fetchone()[0]
        self.assertEqual(kf2, 0)
        run_row = self.conn.execute("SELECT status FROM runs WHERE id = 'run_p28';").fetchone()
        self.assertEqual(run_row["status"], "spawn_failed")

    def test_event_loop_persistent_survivor_emits_kill_failed_once_after_wait(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        st = int(get_proc_stat(proc.pid)["starttime"])
        self.addCleanup(lambda: (_kill_proc(proc.pid, st), proc.wait()))

        state = {"alive": True}

        def fake_pass(ab):
            if state["alive"]:
                return False, {proc.pid: st}
            proc.kill()
            proc.wait(timeout=5)
            return True, {}

        with mock.patch.object(supervisor, "SPAWN_ABORT_WAIT_S", 0.15), \
             mock.patch.object(supervisor, "_abort_collect", return_value=True), \
             mock.patch.object(supervisor, "_abort_kill_pass", side_effect=fake_pass):
            self._run_event_loop_failure(proc)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.kill_failed';").fetchone()[0], 0)

            # Retry before SPAWN_ABORT_WAIT_S -> still 0 events
            supervisor.retry_spawn_aborts(self.conn, self.tmp, testing_mode=True)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.kill_failed';").fetchone()[0], 0)

            # Wait past SPAWN_ABORT_WAIT_S -> emits run.kill_failed exactly once
            time.sleep(0.2)
            supervisor.retry_spawn_aborts(self.conn, self.tmp, testing_mode=True)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.kill_failed';").fetchone()[0], 1)

            # Subsequent retry while still alive -> still only 1 event
            supervisor.retry_spawn_aborts(self.conn, self.tmp, testing_mode=True)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.kill_failed';").fetchone()[0], 1)

            state["alive"] = False
            closed = supervisor.retry_spawn_aborts(self.conn, self.tmp, testing_mode=True)
            self.assertEqual(closed, ["run_p28"])


if __name__ == "__main__":
    unittest.main()
