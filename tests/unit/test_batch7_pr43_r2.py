"""Unit/DB/daemon regression tests for batch7 PR #43 round-2 review findings."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from typing import Any, Dict, Optional
from unittest import mock

import mesh.constants as c
from mesh.core.auth import generate_run_token
from mesh.core.authz import check_authz
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.daemon import lead_turns, scheduler
from mesh.daemon.handlers.agent import handle_agent_create, handle_agent_update
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.project import handle_project_cancel
from mesh.daemon.handlers.task import (
    _validate_model_and_effort,
    handle_task_reject,
    handle_task_start,
)
from mesh.daemon.server import DaemonServer
from mesh.errors import MeshError


def _make_ctx(
    conn: sqlite3.Connection,
    home: str,
    cmd: str,
    args: Dict[str, Any],
    *,
    principal: Optional[Dict[str, Any]] = None,
    actor_kind: str = "user",
    via: str = "direct",
    request_id: str = "req_1",
) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal=principal or {"id": "usr_1", "kind": "user", "name": "user"},
        actor_kind=actor_kind,
        via=via,
        request_id=request_id,
        home=home,
        cmd=cmd,
        args=args,
        testing_mode=True,
    )


def _setup_git_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as f:
        f.write("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)


class TestBatch7PR43R2Unit(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-pr43r2-u-")
        for d in ("db", "run", "logs", "workspaces", "runs", "merges", "repo"):
            os.makedirs(os.path.join(self.home, d), exist_ok=True)
        self.db_path = os.path.join(self.home, "db", "mesh.db")
        self.conn = init_db(self.db_path)
        self.now = now_iso_utc()
        self.repo_path = os.path.join(self.home, "repo")
        _setup_git_repo(self.repo_path)

        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
                (self.now,),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);",
                (self.now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, default_profile, max_unapproved_profile, "
                "default_model, allowed_models_json, default_effort, allowed_efforts_json, "
                "max_concurrency, created_at, updated_at) "
                "VALUES ('agt_lead', 'fake', 'launch', 1, 'edit', 'dev', 'm1', '[\"m1\"]', "
                "'low', '[\"low\",\"high\"]', 2, ?, ?);",
                (self.now, self.now),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_ibx', 'agent', 'ibx1', ?);",
                (self.now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, default_profile, max_unapproved_profile, "
                "default_model, allowed_models_json, default_effort, allowed_efforts_json, "
                "max_concurrency, created_at, updated_at) "
                "VALUES ('agt_ibx', 'fake', 'inbox', 0, 'edit', 'dev', 'm1', '[\"m1\"]', "
                "'low', '[\"low\",\"high\"]', 2, ?, ?);",
                (self.now, self.now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
                "created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P', 'G', 'active', ?, 'main', 'mesh/prj_1/integration', "
                "'usr_1', 'direct', ?, ?);",
                (self.repo_path, self.now, self.now),
            )
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES ('la_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
                (self.now,),
            )

        lead_turns.take_queued_lead_merges()

    def tearDown(self) -> None:
        lead_turns.take_queued_lead_merges()
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.home, ignore_errors=True)

    def _insert_lead_run_and_cred(self, run_id: str = "run_lt1", revoked: bool = False) -> Dict[str, Any]:
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        _, lookup, verifier = generate_run_token()
        cred_id = f"crd_{run_id}"
        scope = {"role": "lead", "project_id": "prj_1", "lead_assignment_id": "la_1", "run_id": run_id}
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, revoked_at) "
                "VALUES (?, 'agt_lead', 'run_token', ?, ?, ?, ?, ?);",
                (cred_id, lookup, verifier, json.dumps(scope), self.now, self.now if revoked else None),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, fence, adapter, status, "
                "boot_id, run_dir, started_at, ended_at, lead_assignment_id) "
                "VALUES (?, 'lead_turn', NULL, 'prj_1', 'agt_lead', 1, 'fake', 'exited', "
                "'boot_1', ?, ?, ?, 'la_1');",
                (run_id, run_dir, self.now, self.now),
            )
        return {
            "id": "agt_lead",
            "kind": "agent",
            "name": "lead1",
            "credential_id": cred_id,
            "scope": scope,
        }

    def _insert_accepted_code_task(self, task_id: str = "tsk_m1") -> None:
        ws = os.path.join(self.home, "workspaces", "prj_1", task_id)
        os.makedirs(ws, exist_ok=True)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, "
                "profile, timeout_s, max_attempts, attempt, workspace_path, branch, created_by, "
                "created_at, updated_at) "
                "VALUES (?, 'prj_1', 'Code', 'Spec', 'code', 'accepted', 'agt_ibx', 'edit', 60, 2, 1, "
                "?, ?, 'agt_lead', ?, ?);",
                (task_id, ws, f"mesh/prj_1/{task_id}", self.now, self.now),
            )

    def test_f1_disabled_lead_check_authz_run_lead_merge_and_recovery(self) -> None:
        """Finding 1: a disabled lead agent's queued task.merge and authz are rejected with TOKEN_INVALID."""
        principal = self._insert_lead_run_and_cred("run_lt1", revoked=False)
        self._insert_accepted_code_task("tsk_m1")

        # Disable lead1 while it is still the active lead assignment.
        with transaction(self.conn):
            self.conn.execute("UPDATE principals SET disabled_at = ? WHERE id = 'agt_lead';", (self.now,))

        # 1. Direct check_authz for a disabled lead run token raises TOKEN_INVALID.
        with self.assertRaises(MeshError) as ctx_err:
            check_authz(
                self.conn,
                "task.merge",
                principal,
                "agent",
                "run_token",
                project_id="prj_1",
                task_id="tsk_m1",
                resource_resolved=True,
            )
        self.assertEqual(ctx_err.exception.code, c.TOKEN_INVALID)

        # 2. _run_lead_merge for an already-queued FIFO item fails in TX1 with TOKEN_INVALID and does not merge.
        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        item = {
            "request_id": "req_m1",
            "cmd": "task.merge",
            "args": {"task_id": "tsk_m1"},
            "action_args": {"task": "tsk_m1"},
            "project_id": "prj_1",
            "run_id": "run_lt1",
            "index": 0,
            "principal": principal,
        }
        asyncio.run(srv._run_lead_merge(item))
        task_row = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_m1';").fetchone()
        self.assertEqual(task_row["status"], "accepted")
        fail_ev = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'lead.action_result' AND run_id = 'run_lt1' "
            "ORDER BY seq DESC LIMIT 1;"
        ).fetchone()
        self.assertIsNotNone(fail_ev)
        payload = json.loads(fail_ev["payload_json"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["phase"], "merge_start")
        self.assertEqual(payload["error_code"], c.TOKEN_INVALID)

        # 3. recover_queued_lead_merges skips/fails disabled lead even when run token was revoked by _close_run,
        # while still recovering an enabled lead whose run token was revoked by _close_run.
        principal2 = self._insert_lead_run_and_cred("run_lt2", revoked=True)
        self._insert_accepted_code_task("tsk_m2")
        with transaction(self.conn):
            record_event(
                conn=self.conn,
                type_="lead.action_result",
                actor_id="agt_lead",
                actor_kind="agent",
                via="lead_turn",
                credential_id=principal2["credential_id"],
                project_id="prj_1",
                run_id="run_lt2",
                request_id="req_rec_dis",
                payload={
                    "index": 0,
                    "cmd": "task.merge",
                    "args": {"task": "tsk_m2"},
                    "ok": True,
                    "status": "queued",
                    "request_id": "req_rec_dis",
                },
            )
        recovered_dis = lead_turns.recover_queued_lead_merges(self.conn)
        self.assertEqual(recovered_dis, 0)
        self.assertEqual(lead_turns.take_queued_lead_merges(), [])
        rec_fail = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'lead.action_result' AND run_id = 'run_lt2' "
            "ORDER BY seq DESC LIMIT 1;"
        ).fetchone()
        self.assertFalse(json.loads(rec_fail["payload_json"])["ok"])
        self.assertEqual(json.loads(rec_fail["payload_json"])["error_code"], c.TOKEN_INVALID)

        # Now re-enable lead1 and verify normal revoked-token recovery still works.
        with transaction(self.conn):
            self.conn.execute("UPDATE principals SET disabled_at = NULL WHERE id = 'agt_lead';")
            record_event(
                conn=self.conn,
                type_="lead.action_result",
                actor_id="agt_lead",
                actor_kind="agent",
                via="lead_turn",
                credential_id=principal2["credential_id"],
                project_id="prj_1",
                run_id="run_lt2",
                request_id="req_rec_ok",
                payload={
                    "index": 1,
                    "cmd": "task.merge",
                    "args": {"task": "tsk_m2"},
                    "ok": True,
                    "status": "queued",
                    "request_id": "req_rec_ok",
                },
            )
        recovered_ok = lead_turns.recover_queued_lead_merges(self.conn)
        self.assertEqual(recovered_ok, 1)
        items = lead_turns.take_queued_lead_merges()
        self.assertEqual([it["request_id"] for it in items], ["req_rec_ok"])

    def test_f2_empty_allowed_efforts_validation_and_inbox_reject_start_and_t6(self) -> None:
        """Finding 2: empty [] allowed_efforts, default_effort null clearing, inbox reject/start, launch T6."""
        # 1. agent.create rejects default_effort when allowed_efforts is [].
        ctx_bad_create = _make_ctx(
            self.conn,
            self.home,
            "agent.create",
            {
                "name": "w_empty",
                "adapter": "fake",
                "delivery": "launch",
                "allowed_models": ["m1"],
                "default_model": "m1",
                "allowed_efforts": [],
                "default_effort": "high",
                "max_concurrency": 1,
            },
        )
        with self.assertRaises(MeshError) as err_ac:
            handle_agent_create(ctx_bad_create)
        self.assertEqual(err_ac.exception.code, c.EFFORT_NOT_ALLOWED)

        # 2. agent.update refuses clearing allowed_efforts to [] while keeping default_effort='low',
        # and accepts clearing both when default_effort is explicitly None.
        ctx_upd_bad = _make_ctx(
            self.conn,
            self.home,
            "agent.update",
            {"name": "ibx1", "allowed_efforts": []},
        )
        with self.assertRaises(MeshError) as err_au:
            handle_agent_update(ctx_upd_bad)
        self.assertEqual(err_au.exception.code, c.EFFORT_NOT_ALLOWED)

        ctx_upd_ok = _make_ctx(
            self.conn,
            self.home,
            "agent.update",
            {"name": "ibx1", "allowed_efforts": [], "default_effort": None},
        )
        with transaction(self.conn):
            res_au = handle_agent_update(ctx_upd_ok)
        self.assertIsNone(res_au.data["default_effort"])
        self.assertEqual(res_au.data["allowed_efforts"], [])

        # 3. _validate_model_and_effort rejects resolved_effort when allowed_efforts == [].
        ag_ibx = self.conn.execute(
            "SELECT p.name, p.disabled_at, a.* FROM agents a JOIN principals p ON a.id = p.id WHERE a.id = 'agt_ibx';"
        ).fetchone()
        with self.assertRaises(MeshError) as err_val:
            _validate_model_and_effort(
                agent=ag_ibx,
                model_override=None,
                effort_override=None,
                current_model="m1",
                current_effort="high",
                testing_mode=True,
            )
        self.assertEqual(err_val.exception.code, c.EFFORT_NOT_ALLOWED)

        # 4. inbox-mode handle_task_reject rechecks allowed_efforts (both tightened ['low'] and []).
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, "
                "model, effort, profile, timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
                "VALUES ('tsk_ibx_rej', 'prj_1', 'T', 'S', 'general', 'submitted', 'agt_ibx', "
                "'m1', 'high', 'edit', 60, 2, 1, 'agt_lead', ?, ?);",
                (self.now, self.now),
            )
        ctx_rej = _make_ctx(
            self.conn,
            self.home,
            "task.reject",
            {"task_id": "tsk_ibx_rej", "reason": "needs rework"},
            principal={"id": "agt_lead", "kind": "agent", "name": "lead1"},
            actor_kind="agent",
        )
        with self.assertRaises(MeshError) as err_rej:
            handle_task_reject(ctx_rej)
        self.assertEqual(err_rej.exception.code, c.EFFORT_NOT_ALLOWED)
        self.assertEqual(
            self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_ibx_rej';").fetchone()["status"],
            "submitted",
        )

        # Also test non-empty tightened allowed_efforts=['low'] for inbox task.reject.
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE agents SET allowed_efforts_json = '[\"low\"]', default_effort = 'low' WHERE id = 'agt_ibx';"
            )
        with self.assertRaises(MeshError) as err_rej2:
            handle_task_reject(ctx_rej)
        self.assertEqual(err_rej2.exception.code, c.EFFORT_NOT_ALLOWED)

        # 5. inbox-mode handle_task_start rechecks allowed_efforts (['low'] and []) and empty allowed_models [].
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, "
                "model, effort, profile, timeout_s, max_attempts, attempt, queued_at, offered_at, "
                "created_by, created_at, updated_at) "
                "VALUES ('tsk_ibx_st', 'prj_1', 'T', 'S', 'general', 'queued', 'agt_ibx', "
                "'m1', 'high', 'edit', 60, 2, 0, ?, ?, 'agt_lead', ?, ?);",
                (self.now, self.now, self.now, self.now),
            )
        ctx_st = _make_ctx(
            self.conn,
            self.home,
            "task.start",
            {"task_id": "tsk_ibx_st"},
            principal={"id": "agt_ibx", "kind": "agent", "name": "ibx1"},
            actor_kind="agent",
            via="agent_token",
        )
        with self.assertRaises(MeshError) as err_st1:
            handle_task_start(ctx_st)
        self.assertEqual(err_st1.exception.code, c.EFFORT_NOT_ALLOWED)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = 'tsk_ibx_st';").fetchone()[0],
            0,
        )

        with transaction(self.conn):
            self.conn.execute(
                "UPDATE agents SET allowed_efforts_json = '[]', default_effort = NULL WHERE id = 'agt_ibx';"
            )
        with self.assertRaises(MeshError) as err_st2:
            handle_task_start(ctx_st)
        self.assertEqual(err_st2.exception.code, c.EFFORT_NOT_ALLOWED)

        # Empty allowed_models [] when task["model"] is set -> MODEL_NOT_ALLOWED at task.start.
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE agents SET allowed_models_json = '[]', default_model = NULL, "
                "allowed_efforts_json = '[\"low\",\"high\"]' WHERE id = 'agt_ibx';"
            )
        with self.assertRaises(MeshError) as err_st3:
            handle_task_start(ctx_st)
        self.assertEqual(err_st3.exception.code, c.MODEL_NOT_ALLOWED)

        # 6. launch-mode queued task fails T6 effort_not_allowed when allowed_efforts is tightened or cleared.
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_ln', 'agent', 'ln1', ?);",
                (self.now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, default_profile, max_unapproved_profile, "
                "default_model, allowed_models_json, default_effort, allowed_efforts_json, "
                "max_concurrency, created_at, updated_at) "
                "VALUES ('agt_ln', 'fake', 'launch', 0, 'edit', 'dev', 'm1', '[\"m1\"]', "
                "NULL, '[]', 1, ?, ?);",
                (self.now, self.now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, "
                "model, effort, profile, timeout_s, max_attempts, attempt, queued_at, "
                "created_by, created_at, updated_at) "
                "VALUES ('tsk_ln_t6', 'prj_1', 'T', 'S', 'general', 'queued', 'agt_ln', "
                "'m1', 'high', 'edit', 60, 2, 0, ?, 'agt_lead', ?, ?);",
                (self.now, self.now, self.now),
            )
        failed_ids = scheduler.check_queued_tasks_t6(self.conn, home=self.home, testing_mode=True)
        self.assertIn("tsk_ln_t6", failed_ids)
        t_row = self.conn.execute("SELECT status, failure_json FROM tasks WHERE id = 'tsk_ln_t6';").fetchone()
        self.assertEqual(t_row["status"], "failed")
        self.assertEqual(json.loads(t_row["failure_json"])["reason"], "effort_not_allowed")

    def test_f3_project_cancel_clears_queue_and_no_pending_lead_wake_on_drain_or_recovery(self) -> None:
        """Finding 3: draining or recovering queued FIFO task.merge after project cancel inserts no pending lead_wake."""
        principal = self._insert_lead_run_and_cred("run_lt_f3", revoked=False)
        self._insert_accepted_code_task("tsk_c1")

        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        item = {
            "request_id": "req_c1",
            "cmd": "task.merge",
            "args": {"task_id": "tsk_c1"},
            "action_args": {"task": "tsk_c1"},
            "project_id": "prj_1",
            "run_id": "run_lt_f3",
            "index": 0,
            "principal": principal,
        }
        srv._project_merge_queues.setdefault("prj_1", srv._project_merge_queues.get("prj_1") or __import__("collections").deque()).append(item)
        srv._project_merge_inflight_reqs.add("req_c1")

        ctx_cancel = _make_ctx(self.conn, self.home, "project.cancel", {"project_id": "prj_1"})
        with transaction(self.conn):
            handle_project_cancel(ctx_cancel)
        srv.execute_effects(ctx_cancel.effects)

        # 1. Queue and inflight request ids for cancelled project are cleared.
        self.assertEqual(list(srv._project_merge_queues.get("prj_1") or []), [])
        self.assertNotIn("req_c1", srv._project_merge_inflight_reqs)

        # 2. Even if an already-popped item runs _run_lead_merge after project cancel,
        # NO outbox lead_wake / lead.action_failed row is created (for launch OR inbox lead).
        asyncio.run(srv._run_lead_merge(item))
        wakes = self.conn.execute(
            "SELECT * FROM outbox WHERE project_id = 'prj_1' AND (channel = 'lead_wake' OR type = 'lead.action_failed') AND status = 'pending';"
        ).fetchall()
        self.assertEqual(wakes, [])

        # Also check inbox-delivery lead agent after project cancel.
        with transaction(self.conn):
            self.conn.execute("UPDATE agents SET delivery = 'inbox' WHERE id = 'agt_lead';")
        item2 = dict(item, request_id="req_c2", index=1)
        asyncio.run(srv._run_lead_merge(item2))
        inbox_wakes = self.conn.execute(
            "SELECT * FROM outbox WHERE project_id = 'prj_1' AND status = 'pending';"
        ).fetchall()
        self.assertEqual(inbox_wakes, [])

        # 3. recover_queued_lead_merges skips queued merge events on a cancelled project without waking lead.
        with transaction(self.conn):
            record_event(
                conn=self.conn,
                type_="lead.action_result",
                actor_id="agt_lead",
                actor_kind="agent",
                via="lead_turn",
                credential_id=principal["credential_id"],
                project_id="prj_1",
                run_id="run_lt_f3",
                request_id="req_c3",
                payload={
                    "index": 2,
                    "cmd": "task.merge",
                    "args": {"task": "tsk_c1"},
                    "ok": True,
                    "status": "queued",
                    "request_id": "req_c3",
                },
            )
        recovered = lead_turns.recover_queued_lead_merges(self.conn)
        self.assertEqual(recovered, 0)
        self.assertEqual(lead_turns.take_queued_lead_merges(), [])
        self.assertEqual(
            self.conn.execute("SELECT * FROM outbox WHERE project_id = 'prj_1' AND status = 'pending';").fetchall(),
            [],
        )

    def test_f4_pr43_r1_kiro_prompt_length_test_works_under_noexec_tmp(self) -> None:
        """Finding 4: TestBatch7PR43R1.test_p1_1_reject_checks_spec_prompt_length_for_launch passes under noexec /tmp."""
        from tests.unit.test_batch7_pr43_r1 import TestBatch7PR43R1Unit

        real_access = os.access
        tmp_roots = tuple({"/tmp", os.path.realpath("/tmp"), os.path.abspath(tempfile.gettempdir()), os.path.realpath(tempfile.gettempdir())})

        def noexec_tmp_access(path: Any, mode: int, *args: Any, **kwargs: Any) -> bool:
            if (mode & os.X_OK) and isinstance(path, (str, bytes)):
                p = os.fsdecode(path)
                abs_p = os.path.abspath(p)
                real_p = os.path.realpath(p)
                if any(
                    cand == root or cand.startswith(root + os.sep)
                    for root in tmp_roots
                    for cand in (abs_p, real_p)
                ):
                    return False
            return real_access(path, mode, *args, **kwargs)

        t = TestBatch7PR43R1Unit("test_p1_1_reject_checks_spec_prompt_length_for_launch")
        with mock.patch("os.access", side_effect=noexec_tmp_access):
            t.setUp()
            try:
                t.test_p1_1_reject_checks_spec_prompt_length_for_launch()
            finally:
                t.tearDown()


if __name__ == "__main__":
    unittest.main()
