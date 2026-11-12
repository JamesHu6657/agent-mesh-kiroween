"""Unit tests for Projects, Intake, Criteria, and Approvals logic."""

import json
import sqlite3
import os
import tempfile
import unittest

import mesh.constants as c
from mesh.core.approvals import (
    compute_op_hash,
    compute_user_approval_mac,
    create_or_get_pending_approval,
    check_and_consume_approval,
)
from mesh.core.authz import check_authz, is_project_lead, get_project_active_lead, lead_run_token_scope
from mesh.core.db import init_db
from mesh.core.ids import generate_id
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.approval import (
    handle_approval_cancel,
    handle_approval_check,
    handle_approval_consume,
    handle_approval_list,
    handle_approval_request,
    handle_approval_show,
)
from mesh.daemon.handlers.event import handle_event_list
from mesh.daemon.handlers.run import handle_workspace_list
from mesh.daemon.handlers.task import handle_task_list
from mesh.errors import MeshError


class TestApprovalsCore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-proj-test-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()
        self.temp_dir.cleanup()

    def test_op_hash_consistency(self):
        op1, hash1 = compute_op_hash(
            op="project.pause",
            requested_by="agt_1",
            params={"project_id": "prj_1", "idempotency_key": "k1", "dry_run": True},
            project_id="prj_1",
        )
        op2, hash2 = compute_op_hash(
            op="project.pause",
            requested_by="agt_1",
            params={"project_id": "prj_1"},
            project_id="prj_1",
        )
        self.assertEqual(hash1, hash2)
        self.assertTrue(hash1.startswith("sha256:"))

    def test_user_mac_generation(self):
        k_user = b"0" * 32
        mac = compute_user_approval_mac(k_user, "apr_123", "sha256:abc", "approved", "2026-11-07T00:00:00.000Z")
        self.assertEqual(len(mac), 64)

    def test_create_and_consume_approval(self):
        # Insert test principals
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', 'now');")
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'lead1', 'now');")

        apr = create_or_get_pending_approval(
            conn=self.conn,
            category="project_control",
            op="project.pause",
            requested_by="agt_1",
            params={"project_id": "prj_1"},
            summary="Pause project prj_1",
            project_id="prj_1",
        )
        apr_id = apr["approval_id"]
        op_hash = apr["op_hash"]
        self.assertIsNotNone(apr_id)

        # Cannot consume pending approval
        with self.assertRaises(MeshError) as cm:
            check_and_consume_approval(
                conn=self.conn,
                approval_id=apr_id,
                op="project.pause",
                category="project_control",
                caller_id="agt_1",
                params={"project_id": "prj_1"},
                project_id="prj_1",
            )
        self.assertEqual(cm.exception.code, c.APPROVAL_REQUIRED)

        # Approve directly
        self.conn.execute(
            "UPDATE approvals SET status = 'approved', decided_by = 'usr_1', decided_via = 'direct', decided_at = '2026-01-01T00:00:00Z', user_mac = 'valid_mac' WHERE id = ?;",
            (apr_id,),
        )

        # Consume with different op params should fail OP_HASH_MISMATCH
        with self.assertRaises(MeshError) as cm:
            check_and_consume_approval(
                conn=self.conn,
                approval_id=apr_id,
                op="project.pause",
                category="project_control",
                caller_id="agt_1",
                params={"project_id": "prj_2"},
                project_id="prj_1",
            )
        self.assertEqual(cm.exception.code, c.OP_HASH_MISMATCH)

        # Consume with non-requester fails OP_HASH_MISMATCH per plan §7.4 step 4 / defect (b)
        with self.assertRaises(MeshError) as cm:
            check_and_consume_approval(
                conn=self.conn,
                approval_id=apr_id,
                op="project.pause",
                category="project_control",
                caller_id="agt_2",
                params={"project_id": "prj_1"},
                project_id="prj_1",
            )
        self.assertEqual(cm.exception.code, c.OP_HASH_MISMATCH)

        # Successful consume
        consumed = check_and_consume_approval(
            conn=self.conn,
            approval_id=apr_id,
            op="project.pause",
            category="project_control",
            caller_id="agt_1",
            params={"project_id": "prj_1"},
            project_id="prj_1",
            request_id="req_1",
        )
        self.assertEqual(consumed["id"], apr_id)

        # Second consume fails with APPROVAL_CONSUMED
        with self.assertRaises(MeshError) as cm:
            check_and_consume_approval(
                conn=self.conn,
                approval_id=apr_id,
                op="project.pause",
                category="project_control",
                caller_id="agt_1",
                params={"project_id": "prj_1"},
                project_id="prj_1",
            )
        self.assertEqual(cm.exception.code, c.APPROVAL_CONSUMED)

    def test_consume_approval_category_mismatch(self):
        """Finding 1 (a): Approval consumption category mismatch yields OP_HASH_MISMATCH and row remains approved."""
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_m', 'user', 'user_m', 'now');")
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_m', 'agent', 'lead_m', 'now');")

        apr = create_or_get_pending_approval(
            conn=self.conn,
            category="retry_over_limit",
            op="task.reassign",
            requested_by="agt_m",
            params={"task_id": "tsk_1", "to": "worker1", "reason": "retry"},
            summary="Reassign task",
            project_id="prj_1",
        )
        apr_id = apr["approval_id"]

        # Approve directly
        self.conn.execute(
            "UPDATE approvals SET status = 'approved', decided_by = 'usr_m', decided_via = 'direct', decided_at = '2026-01-01T00:00:00Z', user_mac = 'valid_mac' WHERE id = ?;",
            (apr_id,),
        )

        # Consuming with category="permission_escalation" must fail with OP_HASH_MISMATCH
        with self.assertRaises(MeshError) as cm:
            check_and_consume_approval(
                conn=self.conn,
                approval_id=apr_id,
                op="task.reassign",
                category="permission_escalation",
                caller_id="agt_m",
                params={"task_id": "tsk_1", "to": "worker1", "reason": "retry"},
                project_id="prj_1",
            )
        self.assertEqual(cm.exception.code, c.OP_HASH_MISMATCH)
        self.assertEqual(cm.exception.exit_code, c.EXIT_APPROVAL_INVALID)

        # Row must still be approved
        cur = self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_id,))
        row = cur.fetchone()
        self.assertEqual(row["status"], "approved")


class TestAuthzMatrix(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-authz-test-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")
        self.conn = init_db(self.db_path)
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', 'now');")
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('lead1', 'agent', 'lead1', 'now');")
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('plain', 'agent', 'plain', 'now');")
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('grok', 'agent', 'grok', 'now');")

        self.conn.execute(
            """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at)
               VALUES ('lead1', 'none', 'inbox', 1, '["intake"]', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at)
               VALUES ('plain', 'none', 'inbox', 0, '["intake"]', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at)
               VALUES ('grok', 'none', 'inbox', 0, '["intake","relay"]', 1, 'now', 'now');"""
        )

        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at)
               VALUES ('prj_1', 'Test', 'Goal', 'active', 'usr_1', 'direct', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
               VALUES ('asg_1', 'prj_1', 'lead1', 'usr_1', 'direct', 'now');"""
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_authz_matrix_rules(self):
        """ACC: E6 (unit clause; the checklist's `test_authz_matrix`) - plus the plan §7.3 matrix
        rows below. An executor run token calling intake gets rc 4, even when its agent has
        the intake capability."""
        # 1. User direct factor can do everything
        check_authz(self.conn, "config.set", {"id": "usr_1", "name": "user"}, "user", "direct")
        check_authz(self.conn, "project.pause", {"id": "usr_1", "name": "user"}, "user", "direct", project_id="prj_1")
        check_authz(self.conn, "audit.verify", {"id": "usr_1", "name": "user"}, "user", "direct")

        # 2. Agent token calling config.set gets CAPABILITY_MISSING
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "config.set", {"id": "lead1", "name": "lead1"}, "agent", "token")
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)

        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "audit.verify", {"id": "lead1", "name": "lead1"}, "agent", "token")
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)

        # 3. Agent without intake cap gets CAPABILITY_MISSING on intake
        self.conn.execute("UPDATE agents SET capabilities_json = '[]' WHERE id = 'plain';")
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "intake.submit", {"id": "plain", "name": "plain", "capabilities": []}, "agent", "token")
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)

        # 3b. E6: an executor run token cannot submit intake (rc 4), whatever the agent's caps.
        self.conn.execute("UPDATE agents SET capabilities_json = '[\"intake\"]' WHERE id = 'plain';")
        run_scope = {"task_id": "tsk_x", "project_id": "prj_1", "run_id": "run_x", "fence": 1}
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "intake.submit",
                        {"id": "plain", "name": "plain", "capabilities": ["intake"], "scope": run_scope},
                        "agent", "run_token")
        self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN)
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)
        # Control: the same agent through its agent token may submit intake.
        check_authz(self.conn, "intake.submit", {"id": "plain", "name": "plain", "capabilities": ["intake"]},
                    "agent", "token")
        self.conn.execute("UPDATE agents SET capabilities_json = '[]' WHERE id = 'plain';")

        # 4. Agent without relay cap gets RELAY_NOT_ALLOWED on relayed op
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "project.set_lead", {"id": "plain", "name": "plain", "capabilities": ["intake"]}, "agent", "relayed", project_id="prj_1")
        self.assertEqual(cm.exception.code, c.RELAY_NOT_ALLOWED)

        # 5. Relayer with relay cap succeeds on relayed op
        check_authz(self.conn, "project.set_lead", {"id": "grok", "name": "grok", "capabilities": ["intake", "relay"]}, "agent", "relayed", project_id="prj_1")

        # 6. Relayer cannot be current lead of affected project (SELF_APPROVAL)
        self.conn.execute("UPDATE lead_assignments SET agent_id = 'grok' WHERE id = 'asg_1';")
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "project.set_lead", {"id": "grok", "name": "grok", "capabilities": ["intake", "relay"]}, "agent", "relayed", project_id="prj_1")
        self.assertEqual(cm.exception.code, c.SELF_APPROVAL)

        # 7. Non-lead agent calling project.complete gets NOT_PROJECT_LEAD
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "project.complete", {"id": "plain", "name": "plain"}, "agent", "token", project_id="prj_1")
        self.assertEqual(cm.exception.code, c.NOT_PROJECT_LEAD)

        # 8. Lead agent calling project.complete succeeds (lead is currently grok after step 6)
        check_authz(self.conn, "project.complete", {"id": "grok", "name": "grok"}, "agent", "token", project_id="prj_1")

        # 9. Relayed restrictions: task and dep mutations are not allowed via relay (finding 1)
        for disallowed_cmd in ("task.create", "task.assign", "task.cancel", "dep.add", "dep.remove"):
            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, disallowed_cmd, {"id": "plain", "name": "plain", "capabilities": ["intake", "relay"]}, "agent", "relayed", project_id="prj_1")
            self.assertEqual(cm.exception.code, c.RELAY_NOT_ALLOWED)

        # Relayed project.pause allowed
        check_authz(self.conn, "project.pause", {"id": "plain", "name": "plain", "capabilities": ["intake", "relay"]}, "agent", "relayed", project_id="prj_1")

        # 10. Read scoping tests (finding 10)
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('worker', 'agent', 'worker', 'now');")
        self.conn.execute(
            """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at)
               VALUES ('worker', 'none', 'inbox', 0, '[]', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, profile, timeout_s, idle_timeout_s, max_attempts, attempt, fence_seq, created_by, assignee_agent_id, version, created_at, updated_at)
               VALUES ('tsk_1', 'prj_1', 'Task 1', 'Spec 1', 'general', 'assigned', 0, 'dev', 3600, 0, 1, 0, 0, 'usr_1', 'worker', 1, 'now', 'now');"""
        )

        # Unrelated agent (plain) cannot read task.show or dep.list on tsk_1
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "task.show", {"id": "plain", "name": "plain", "capabilities": []}, "agent", "token", project_id="prj_1", task_id="tsk_1")
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)

        # 11. Approval commands authz per §7.3 matrix (Finding 7)
        # Approval authority (e.g. user-only direct decisions, requester-only check/consume)
        # is enforced in handlers/approval.py (G7/G8/G10/G19 and check-restriction tests),
        # not in check_authz.
        for apr_cmd in ("approval.request", "approval.approve", "approval.reject", "approval.check", "approval.consume", "approval.cancel"):
            check_authz(self.conn, apr_cmd, {"id": "usr_1", "name": "user"}, "user", "direct")
            check_authz(self.conn, apr_cmd, {"id": "lead1", "name": "lead1"}, "agent", "token")

        # Relayed approval.approve without relay cap gets RELAY_NOT_ALLOWED
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "approval.approve", {"id": "plain", "name": "plain", "capabilities": ["intake"]}, "agent", "relayed")
        self.assertEqual(cm.exception.code, c.RELAY_NOT_ALLOWED)

        # Relayed approval.approve with relay cap passes check_authz
        check_authz(self.conn, "approval.approve", {"id": "grok", "name": "grok", "capabilities": ["intake", "relay"]}, "agent", "relayed")

        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "dep.list", {"id": "plain", "name": "plain", "capabilities": []}, "agent", "token", project_id="prj_1", task_id="tsk_1")
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)

        # Assignee (worker) can read task.show and dep.list
        check_authz(self.conn, "task.show", {"id": "worker", "name": "worker", "capabilities": []}, "agent", "token", project_id="prj_1", task_id="tsk_1")
        check_authz(self.conn, "dep.list", {"id": "worker", "name": "worker", "capabilities": []}, "agent", "token", project_id="prj_1", task_id="tsk_1")

        # Project lead (grok) can read task.show and dep.list
        check_authz(self.conn, "task.show", {"id": "grok", "name": "grok", "capabilities": ["relay"]}, "agent", "token", project_id="prj_1", task_id="tsk_1")

        # Relayer can read task.show and project.show
        check_authz(self.conn, "task.show", {"id": "plain", "name": "plain", "capabilities": ["relay"]}, "agent", "relayed", project_id="prj_1", task_id="tsk_1")
        check_authz(self.conn, "project.show", {"id": "plain", "name": "plain", "capabilities": ["relay"]}, "agent", "relayed", project_id="prj_1")

        # Lead without relay can read task.show
        self.conn.execute("UPDATE agents SET capabilities_json = '[\"intake\"]' WHERE id = 'grok';")
        check_authz(self.conn, "task.show", {"id": "grok", "name": "grok", "capabilities": []}, "agent", "token", project_id="prj_1", task_id="tsk_1")

        # Unrelated agent gets SCOPE_MISMATCH on event.list --project
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "event.list", {"id": "plain", "name": "plain", "capabilities": []}, "agent", "token", project_id="prj_1")
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)

        # Unknown op from agent token is rejected
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "unknown.operation", {"id": "plain", "name": "plain"}, "agent", "token")
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)
        self.assertEqual(cm.exception.details.get("cmd"), "unknown.operation")

        # Run token scoping
        run_token_principal = {"id": "worker", "name": "worker", "scope": {"task_id": "tsk_1", "project_id": "prj_1"}}
        # In scope
        check_authz(self.conn, "task.show", run_token_principal, "agent", "run_token", project_id="prj_1", task_id="tsk_1")
        check_authz(self.conn, "project.show", run_token_principal, "agent", "run_token", project_id="prj_1")
        # Out of scope task
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "task.show", run_token_principal, "agent", "run_token", project_id="prj_1", task_id="tsk_other")
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)
        # Out of scope project
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "project.show", run_token_principal, "agent", "run_token", project_id="prj_other")
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)
        # Unallowed command for run token
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "task.list", run_token_principal, "agent", "run_token", project_id="prj_1")
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "inbox.list", run_token_principal, "agent", "run_token", project_id="prj_1", task_id="tsk_1")
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)

        # Task execution authz matrix (§7.3)
        exec_cmds = ["task.start", "task.heartbeat", "task.progress", "task.submit", "task.fail"]
        for exec_cmd in exec_cmds:
            # User direct: allowed
            check_authz(self.conn, exec_cmd, {"id": "usr_1", "name": "user"}, "user", "direct", project_id="prj_1", task_id="tsk_1")

            # Inbox assignee (worker): allowed
            check_authz(self.conn, exec_cmd, {"id": "worker", "name": "worker"}, "agent", "token", project_id="prj_1", task_id="tsk_1")

            # Lead and other non-assignee agents: forbidden -> rc 4 NOT_ASSIGNEE
            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, exec_cmd, {"id": "lead1", "name": "lead1"}, "agent", "token", project_id="prj_1", task_id="tsk_1")
            self.assertEqual(cm.exception.code, c.NOT_ASSIGNEE)

            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, exec_cmd, {"id": "plain", "name": "plain"}, "agent", "token", project_id="prj_1", task_id="tsk_1")
            self.assertEqual(cm.exception.code, c.NOT_ASSIGNEE)

            # Relayed agent: forbidden -> rc 4 RELAY_NOT_ALLOWED
            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, exec_cmd, {"id": "grok", "name": "grok", "capabilities": ["intake", "relay"]}, "agent", "relayed", project_id="prj_1", task_id="tsk_1")
            self.assertEqual(cm.exception.code, c.RELAY_NOT_ALLOWED)

        # Run token executor allowed on heartbeat, progress, submit, fail for own task
        for run_allowed_cmd in ["task.heartbeat", "task.progress", "task.submit", "task.fail"]:
            check_authz(self.conn, run_allowed_cmd, run_token_principal, "agent", "run_token", project_id="prj_1", task_id="tsk_1")

        # Run token executor denied on task.start -> CAPABILITY_MISSING (§7.2, H12)
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "task.start", run_token_principal, "agent", "run_token", project_id="prj_1", task_id="tsk_1")
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)

        # Launch-mode assignee: its own agent token has no fenced-write rights (plan §7.2/§7.3)
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('fx', 'agent', 'fx', 'now');")
        self.conn.execute(
            """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at)
               VALUES ('fx', 'fake', 'launch', 0, '[]', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, profile, timeout_s, idle_timeout_s, max_attempts, attempt, fence_seq, created_by, assignee_agent_id, version, created_at, updated_at)
               VALUES ('tsk_fx', 'prj_1', 'Launch Task', 'Spec', 'general', 'running', 0, 'dev', 3600, 0, 1, 1, 1, 'usr_1', 'fx', 1, 'now', 'now');"""
        )
        for fenced_cmd in ("task.heartbeat", "task.progress", "task.submit", "task.fail"):
            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, fenced_cmd, {"id": "fx", "name": "fx"}, "agent", "token", project_id="prj_1", task_id="tsk_fx")
            self.assertEqual(cm.exception.code, c.NOT_ASSIGNEE)
            self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN)
            self.assertEqual(cm.exception.details.get("delivery"), "launch")

    def test_authz_lead_run_token_matrix(self):
        """ACC: L1 (lead run-token authority clause) - the §7.3 lead column for a lead
        run token, scoped to `scope.project_id`, and `LEAD_SUPERSEDED` (rc 9) checked
        before every other lead check (§7.1, §7.2)."""
        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at)
               VALUES ('prj_2', 'Other', 'Goal', 'active', 'usr_1', 'direct', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
               VALUES ('asg_2', 'prj_2', 'plain', 'usr_1', 'direct', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, profile, timeout_s, idle_timeout_s, max_attempts, attempt, fence_seq, created_by, version, created_at, updated_at)
               VALUES ('tsk_1', 'prj_1', 'T1', 'Spec', 'general', 'assigned', 0, 'dev', 3600, 0, 1, 0, 0, 'usr_1', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, profile, timeout_s, idle_timeout_s, max_attempts, attempt, fence_seq, created_by, version, created_at, updated_at)
               VALUES ('tsk_2', 'prj_2', 'T2', 'Spec', 'general', 'assigned', 0, 'dev', 3600, 0, 1, 0, 0, 'usr_1', 1, 'now', 'now');"""
        )

        lead_scope = {"role": "lead", "run_id": "run_lt", "project_id": "prj_1",
                      "lead_assignment_id": "asg_1"}
        lead_principal = {"id": "lead1", "name": "lead1", "capabilities": ["intake"],
                          "scope": lead_scope}

        # The lead column of §7.3 for its own project.
        for cmd in ("task.create", "task.assign", "task.reassign", "task.accept", "task.reject",
                    "task.retry", "task.escalate", "task.cancel", "task.merge", "dep.add",
                    "dep.remove", "project.complete", "project.note", "project.deliver",
                    "approval.request", "intake.submit", "project.create"):
            check_authz(self.conn, cmd, lead_principal, "agent", "run_token", project_id="prj_1")
        for cmd in ("project.show", "project.leads", "project.list", "task.show", "task.list",
                    "dep.list", "event.list", "event.tail", "run.log", "run.list", "run.show",
                    "task.log", "workspace.list", "workspace.show", "approval.show",
                    "approval.list", "inbox.list", "inbox.wait", "inbox.ack"):
            check_authz(self.conn, cmd, lead_principal, "agent", "run_token", project_id="prj_1")
        check_authz(self.conn, "task.show", lead_principal, "agent", "run_token",
                    project_id="prj_1", task_id="tsk_1")

        # Not the lead column: config/agent/audit stay user-only (rc 4), and the
        # executor-only fenced writes are not lead commands either.
        for cmd in ("config.set", "agent.create", "agent.token.issue", "audit.verify",
                    "daemon.stop", "task.start", "task.heartbeat", "task.progress",
                    "task.submit", "task.fail", "approval.approve", "approval.reject",
                    "project.pause", "project.resume", "project.cancel", "project.set_lead",
                    "project.update"):
            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, cmd, lead_principal, "agent", "run_token", project_id="prj_1")
            self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING, cmd)
            self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN, cmd)

        # Scope: another project, or a task of another project, is rc 4 SCOPE_MISMATCH.
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "project.show", lead_principal, "agent", "run_token",
                        project_id="prj_2")
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "task.show", lead_principal, "agent", "run_token",
                        project_id="prj_1", task_id="tsk_2")
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "task.accept", lead_principal, "agent", "run_token",
                        project_id="prj_1", task_id="tsk_2")
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)

        # Review m2-r9 finding 1(b): every command that carries a task_id is scoped,
        # including the event and run reads.
        for cmd in ("event.list", "event.tail", "run.list", "run.show", "run.log",
                    "task.log", "task.wait", "dep.list", "workspace.show"):
            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, cmd, lead_principal, "agent", "run_token",
                            project_id="prj_1", task_id="tsk_2")
            self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH, cmd)
            check_authz(self.conn, cmd, lead_principal, "agent", "run_token",
                        project_id="prj_1", task_id="tsk_1")

        # Review m2-r9 finding 1(a): the list/read commands without a project are
        # allowed (the handlers confine them to scope.project_id via
        # lead_run_token_scope), and the relay capability never widens them.
        relay_lead = dict(lead_principal, capabilities=["intake", "relay"])
        for cmd in ("project.list", "task.list", "event.list", "event.tail", "run.list",
                    "inbox.list", "inbox.wait", "inbox.ack", "approval.list",
                    "workspace.list"):
            check_authz(self.conn, cmd, lead_principal, "agent", "run_token")
            check_authz(self.conn, cmd, relay_lead, "agent", "run_token")
            self.assertEqual(lead_run_token_scope(relay_lead, "run_token"), "prj_1", cmd)
        self.assertIsNone(lead_run_token_scope(lead_principal, "token"))
        self.assertIsNone(lead_run_token_scope({"id": "worker", "scope": {"task_id": "tsk_1"}},
                                               "run_token"))

        # Review m2-r9 finding 1(c): a follow-up to the scope project is allowed, new
        # intake and project creation are rc 4 SCOPE_MISMATCH.
        check_authz(self.conn, "intake.submit", lead_principal, "agent", "run_token",
                    project_id="prj_1")
        for cmd in ("intake.submit", "project.create"):
            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, cmd, lead_principal, "agent", "run_token")
            self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH, cmd)
            self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN, cmd)

        # A superseded assignment is rc 9 LEAD_SUPERSEDED, checked before the other
        # lead checks: even a command outside the lead column reports it. The
        # user-only rows (config/agent/audit) are rejected earlier, at step 2 of
        # check_authz, and stay rc 4 CAPABILITY_MISSING.
        self.conn.execute(
            "UPDATE lead_assignments SET ended_at = 'now', ended_reason = 'superseded' WHERE id = 'asg_1';"
        )
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
               VALUES ('asg_1b', 'prj_1', 'plain', 'usr_1', 'direct', 'now');"""
        )
        for cmd in ("task.create", "project.show", "task.start", "approval.approve"):
            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, cmd, lead_principal, "agent", "run_token", project_id="prj_1")
            self.assertEqual(cm.exception.code, c.LEAD_SUPERSEDED, cmd)
            self.assertEqual(cm.exception.exit_code, c.EXIT_STALE, cmd)
            self.assertEqual(cm.exception.details.get("lead_assignment_id"), "asg_1")
            self.assertEqual(cm.exception.details.get("current_lead_assignment_id"), "asg_1b")
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "config.set", lead_principal, "agent", "run_token", project_id="prj_1")
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)

        # The new assignment's own token works again.
        new_principal = {"id": "plain", "name": "plain", "capabilities": ["intake"],
                         "scope": {"role": "lead", "run_id": "run_lt2", "project_id": "prj_1",
                                   "lead_assignment_id": "asg_1b"}}
        check_authz(self.conn, "task.create", new_principal, "agent", "run_token", project_id="prj_1")

        # An executor run token (no `role`) keeps the executor branch unchanged.
        executor = {"id": "worker", "name": "worker",
                    "scope": {"task_id": "tsk_1", "project_id": "prj_1", "run_id": "run_x", "fence": 1}}
        with self.assertRaises(MeshError) as cm:
            check_authz(self.conn, "task.create", executor, "agent", "run_token", project_id="prj_1")
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)
        self.assertEqual(cm.exception.details.get("role"), "executor")

    def test_lead_run_token_approval_and_workspace_list_confined(self):
        """ACC: L1 (lead run-token scope clause) - `approval.list` and `workspace.list`
        with a lead run token return only the scope project's rows, with or without the
        relay capability; an explicit other `--project` is rc 4 SCOPE_MISMATCH (r9c N1)."""
        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at)
               VALUES ('prj_2', 'Other', 'Goal', 'active', 'usr_1', 'direct', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
               VALUES ('asg_2', 'prj_2', 'lead1', 'usr_1', 'direct', 'now');"""
        )
        for pid, tid in (("prj_1", "tsk_1"), ("prj_2", "tsk_2")):
            self.conn.execute(
                """INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, profile, timeout_s, idle_timeout_s, max_attempts, attempt, fence_seq, workspace_path, created_by, version, created_at, updated_at)
                   VALUES (?, ?, 'T', 'Spec', 'general', 'assigned', 0, 'dev', 3600, 0, 1, 0, 0, ?, 'usr_1', 1, 'now', 'now');""",
                (tid, pid, os.path.join(self.temp_dir.name, "ws", tid)),
            )
        for aid, pid in (("apr_1", "prj_1"), ("apr_2", "prj_2")):
            self.conn.execute(
                """INSERT INTO approvals (id, project_id, category, op_type, op_json, op_hash, summary, requested_by, requested_at, expires_at, status)
                   VALUES (?, ?, 'external_action', 'x', '{}', ?, 'S', 'lead1', 'now', 'later', 'pending');""",
                (aid, pid, "hash_" + aid),
            )

        scope = {"role": "lead", "run_id": "run_lt", "project_id": "prj_1",
                 "lead_assignment_id": "asg_1"}

        def ctx(caps, cmd, args):
            return HandlerContext(
                conn=self.conn,
                principal={"id": "lead1", "name": "lead1", "capabilities": caps, "scope": scope},
                actor_kind="agent", via="run_token", request_id="req-lead-list",
                home=self.temp_dir.name, cmd=cmd, args=args,
            )

        for caps in (["intake"], ["intake", "relay"]):
            for args in ({}, {"project": "prj_1"}):
                items = handle_approval_list(ctx(caps, "approval.list", args)).data["items"]
                self.assertEqual([a["id"] for a in items], ["apr_1"], (caps, args))
                entries = handle_workspace_list(ctx(caps, "workspace.list", args)).data["workspaces"]
                self.assertEqual({e["project_id"] for e in entries} - {None}, {"prj_1"}, (caps, args))
                self.assertIn("tsk_1", {e.get("task_id") for e in entries}, (caps, args))
                self.assertNotIn("tsk_2", {e.get("task_id") for e in entries}, (caps, args))
            for handler, cmd in ((handle_approval_list, "approval.list"),
                                 (handle_workspace_list, "workspace.list")):
                with self.assertRaises(MeshError) as cm:
                    handler(ctx(caps, cmd, {"project": "prj_2"}))
                self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH, (caps, cmd))
                self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN, (caps, cmd))

        # Without a run token the relay capability still lists everything.
        relay = HandlerContext(
            conn=self.conn, principal={"id": "grok", "name": "grok", "capabilities": ["intake", "relay"]},
            actor_kind="agent", via="token", request_id="req-relay-list",
            home=self.temp_dir.name, cmd="approval.list", args={},
        )
        self.assertEqual({a["id"] for a in handle_approval_list(relay).data["items"]},
                         {"apr_1", "apr_2"})

    def test_lead_run_token_approval_id_handlers_confined(self):
        """ACC: L1 - approval IDs cannot bypass the lead turn's project scope,
        even for the requester, a lead of both projects, or a relay-capable lead."""
        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at)
               VALUES ('prj_2', 'Other', 'Goal', 'active', 'usr_1', 'direct', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
               VALUES ('asg_2', 'prj_2', 'lead1', 'usr_1', 'direct', 'now');"""
        )
        for aid, pid, requester in (("apr_other", "prj_2", "lead1"),
                                    ("apr_user", "prj_2", "usr_1"),
                                    ("apr_global", None, "lead1")):
            self.conn.execute(
                """INSERT INTO approvals (id, project_id, category, op_type, op_json, op_hash, summary, requested_by, requested_at, expires_at, status)
                   VALUES (?, ?, 'deploy', 'external.deploy', '{}', ?, 'S', ?, 'now', '2999-01-01T00:00:00.000Z', 'pending');""",
                (aid, pid, "hash_" + aid, requester),
            )

        scope = {"role": "lead", "run_id": "run_lt", "project_id": "prj_1",
                 "lead_assignment_id": "asg_1"}

        def ctx(caps, cmd, aid, via="run_token"):
            return HandlerContext(
                conn=self.conn,
                principal={"id": "lead1", "name": "lead1", "capabilities": caps, "scope": scope},
                actor_kind="agent", via=via, request_id="req-lead-approval",
                home=self.temp_dir.name, cmd=cmd,
                args={"approval_id": aid, "op_hash": "hash_" + aid},
            )

        handlers = ((handle_approval_show, "approval.show"),
                    (handle_approval_check, "approval.check"),
                    (handle_approval_consume, "approval.consume"),
                    (handle_approval_cancel, "approval.cancel"))
        for caps in (["intake"], ["intake", "relay"]):
            for handler, cmd in handlers:
                for aid in ("apr_other", "apr_user", "apr_global"):
                    with self.subTest(caps=caps, cmd=cmd, approval_id=aid):
                        with self.assertRaises(MeshError) as cm:
                            handler(ctx(caps, cmd, aid))
                        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)
                        self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN)
                        self.assertEqual(
                            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (aid,)).fetchone()["status"],
                            "pending",
                        )

                # Each handler still works for the requester inside the scope.
                own_id = "apr_own_" + cmd.split(".")[1] + str(len(caps))
                status = "approved" if cmd == "approval.consume" else "pending"
                self.conn.execute(
                    """INSERT INTO approvals (id, project_id, category, op_type, op_json, op_hash, summary, requested_by, requested_at, expires_at, status, decided_via, decided_at, user_mac)
                       VALUES (?, 'prj_1', 'deploy', 'external.deploy', '{}', ?, 'S', 'lead1', 'now', '2999-01-01T00:00:00.000Z', ?, 'direct', 'now', 'mac');""",
                    (own_id, "hash_" + own_id, status),
                )
                result = handler(ctx(caps, cmd, own_id))
                result_id = result.data["id"] if cmd == "approval.show" else result.data["approval_id"]
                self.assertEqual(result_id, own_id, (caps, cmd))
                expected = {"approval.consume": "consumed", "approval.cancel": "cancelled"}.get(cmd, status)
                self.assertEqual(result.data["status"], expected, (caps, cmd))

        # Ordinary agent tokens retain requester and cross-project read access.
        self.assertEqual(handle_approval_show(ctx(["intake"], "approval.show", "apr_other", via="token")).data["id"],
                         "apr_other")
        self.assertEqual(handle_approval_show(ctx(["intake"], "approval.show", "apr_user", via="token")).data["id"],
                         "apr_user")
        self.assertEqual(handle_approval_show(ctx(["relay"], "approval.show", "apr_global", via="token")).data["id"],
                         "apr_global")

    def test_lead_run_token_approval_request_own_project_only(self):
        """ACC: L2 (approval.request action) - a lead run token may request an approval
        for its scope project (requested_by = the lead agent); another project, a task
        of another project, or no project at all is rc 4 SCOPE_MISMATCH, a superseded
        assignment is rc 9 LEAD_SUPERSEDED, and an executor run token stays rc 4
        CREDENTIAL_NOT_ALLOWED_IN_RUN (plan §7.3, §12.4)."""
        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at)
               VALUES ('prj_2', 'Other', 'Goal', 'active', 'usr_1', 'direct', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
               VALUES ('asg_2', 'prj_2', 'lead1', 'usr_1', 'direct', 'now');"""
        )
        for pid, tid in (("prj_1", "tsk_1"), ("prj_2", "tsk_2")):
            self.conn.execute(
                """INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, profile, timeout_s, idle_timeout_s, max_attempts, attempt, fence_seq, workspace_path, created_by, version, created_at, updated_at)
                   VALUES (?, ?, 'T', 'Spec', 'general', 'assigned', 0, 'dev', 3600, 0, 1, 0, 0, ?, 'usr_1', 1, 'now', 'now');""",
                (tid, pid, os.path.join(self.temp_dir.name, "ws", tid)),
            )

        def ctx(scope, extra, via="run_token"):
            args = {"category": "deploy", "summary": "Deploy", "operation": {"target": "x"}}
            args.update(extra)
            return HandlerContext(
                conn=self.conn,
                principal={"id": "lead1", "name": "lead1", "capabilities": ["intake"], "scope": scope},
                actor_kind="agent", via=via, request_id="req-lead-apr",
                home=self.temp_dir.name, cmd="approval.request", args=args,
            )

        lead_scope = {"role": "lead", "run_id": "run_lt", "project_id": "prj_1",
                      "lead_assignment_id": "asg_1"}
        result = handle_approval_request(ctx(lead_scope, {"project_id": "prj_1"}))
        row = self.conn.execute("SELECT project_id, requested_by, status FROM approvals WHERE id = ?;",
                                (result.data["approval_id"],)).fetchone()
        self.assertEqual((row["project_id"], row["requested_by"], row["status"]),
                         ("prj_1", "lead1", "pending"))
        # A task of the scope project names the project too.
        result = handle_approval_request(ctx(lead_scope, {"task_id": "tsk_1"}))
        row = self.conn.execute("SELECT project_id, requested_by FROM approvals WHERE id = ?;",
                                (result.data["approval_id"],)).fetchone()
        self.assertEqual((row["project_id"], row["requested_by"]), ("prj_1", "lead1"))

        before = self.conn.execute("SELECT COUNT(*) AS n FROM approvals;").fetchone()["n"]
        for extra in ({"project_id": "prj_2"}, {"task_id": "tsk_2"},
                      {"project_id": "prj_1", "task_id": "tsk_2"}, {}):
            with self.subTest(extra=extra):
                with self.assertRaises(MeshError) as cm:
                    handle_approval_request(ctx(lead_scope, extra))
                self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)
                self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN)

        stale_scope = dict(lead_scope, lead_assignment_id="asg_old")
        with self.assertRaises(MeshError) as cm:
            handle_approval_request(ctx(stale_scope, {"project_id": "prj_1"}))
        self.assertEqual(cm.exception.code, c.LEAD_SUPERSEDED)
        self.assertEqual(cm.exception.exit_code, c.EXIT_STALE)

        executor_scope = {"task_id": "tsk_1", "project_id": "prj_1", "run_id": "run_ex"}
        with self.assertRaises(MeshError) as cm:
            handle_approval_request(ctx(executor_scope, {"project_id": "prj_1"}))
        self.assertEqual(cm.exception.code, c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
        self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM approvals;").fetchone()["n"], before)

    def test_task_and_event_list_without_project_filter(self):
        """task.list and event.list without --project return only the agent's own rows."""
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('worker', 'agent', 'worker', 'now');")
        self.conn.execute(
            """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at)
               VALUES ('worker', 'none', 'inbox', 0, '[]', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, profile, timeout_s, idle_timeout_s, max_attempts, attempt, fence_seq, created_by, assignee_agent_id, version, created_at, updated_at)
               VALUES ('tsk_1', 'prj_1', 'Task 1', 'Spec 1', 'general', 'assigned', 0, 'dev', 3600, 0, 1, 0, 0, 'usr_1', 'worker', 1, 'now', 'now');"""
        )
        # Insert a second task assigned to 'plain'
        self.conn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, profile, timeout_s, idle_timeout_s, max_attempts, attempt, fence_seq, created_by, assignee_agent_id, version, created_at, updated_at)
               VALUES ('tsk_plain', 'prj_1', 'Plain Task', 'Spec', 'general', 'assigned', 0, 'dev', 3600, 0, 1, 0, 0, 'usr_1', 'plain', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO events (id, ts, type, actor_id, actor_kind, via, task_id, prev_hash, hash)
               VALUES ('evt_plain', 'now', 'task.created', 'plain', 'agent', 'token', 'tsk_plain', 'h1', 'h2');"""
        )
        self.conn.execute(
            """INSERT INTO events (id, ts, type, actor_id, actor_kind, via, task_id, prev_hash, hash)
               VALUES ('evt_worker', 'now', 'task.created', 'worker', 'agent', 'token', 'tsk_1', 'h2', 'h3');"""
        )

        # Worker lists tasks without project filter
        ctx_worker = HandlerContext(
            conn=self.conn,
            principal={"id": "worker", "name": "worker", "capabilities": []},
            actor_kind="agent",
            via="token",
            request_id="req-worker-tasks",
            home=self.temp_dir.name,
            cmd="task.list",
            args={},
        )
        res_tasks = handle_task_list(ctx_worker)
        task_ids = [t["id"] for t in res_tasks.data["items"]]
        self.assertIn("tsk_1", task_ids)
        self.assertNotIn("tsk_plain", task_ids)

        ctx_worker_events = HandlerContext(
            conn=self.conn,
            principal={"id": "worker", "name": "worker", "capabilities": []},
            actor_kind="agent",
            via="token",
            request_id="req-worker-events",
            home=self.temp_dir.name,
            cmd="event.list",
            args={},
        )
        res_events = handle_event_list(ctx_worker_events)
        evt_ids = [e["id"] for e in res_events.data["items"]]
        self.assertIn("evt_worker", evt_ids)
        self.assertNotIn("evt_plain", evt_ids)
