"""Unit tests for database invariants per plan §5.6.

ACC: A8, A10
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.invariants import check_invariants
from tests.gitfixture import GitFixture


class TestInvariants(unittest.TestCase):
    """Test invariant verification on valid and corrupted DB states. ACC: A10, A8"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-inv-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")
        self.conn = init_db(self.db_path)
        self._seed_base_data()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def _seed_base_data(self):
        """Seed minimal valid base principals, agents, and boot row."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO boots (id, pid, started_at) VALUES ('boot_1', 1000, '2026-11-07T00:00:00.000Z');"
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', '2026-11-07T00:00:00.000Z');"
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', '2026-11-07T00:00:00.000Z');"
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_fx', 'agent', 'fx', '2026-11-07T00:00:00.000Z');"
            )
            self.conn.execute(
                """
                INSERT INTO agents (
                    id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at
                ) VALUES (
                    'agt_lead', 'none', 'inbox', 1, 1, '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO agents (
                    id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at
                ) VALUES (
                    'agt_fx', 'fake', 'launch', 0, 2, '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )

    def _create_active_project(self, project_id: str = "prj_1") -> None:
        """Helper to create an active project with valid lead assignment and matching event."""
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO projects (
                    id, name, goal, status, created_by, created_via, created_at, updated_at
                ) VALUES (
                    ?, 'Test Project', 'Test Goal', 'active', 'usr_1', 'direct',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """,
                (project_id,),
            )
            self.conn.execute(
                """
                INSERT INTO lead_assignments (
                    id, project_id, agent_id, assigned_by, via, started_at
                ) VALUES (
                    ?, ?, 'agt_lead', 'usr_1', 'direct', '2026-11-07T00:00:00.000Z'
                );
                """,
                (f"lead_{project_id}", project_id),
            )
            record_event(
                self.conn,
                type_="project.created",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id=project_id,
                to_state="active",
            )

    def test_invariants_valid_states(self):
        """Verify zero violations for valid states F8, T9, and D15. ACC: A10, A8"""
        # --- State F8: submitted with a live run in launch-mode post-submit grace ---
        self._create_active_project("prj_f8")
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, assignee_agent_id,
                    profile, timeout_s, max_attempts, fence_seq, created_by,
                    created_at, updated_at
                ) VALUES (
                    'tsk_f8', 'prj_f8', 'F8 Task', 'Spec', 'code', 'submitted', 'agt_fx',
                    'edit', 3600, 3, 1, 'usr_1',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO runs (
                    id, kind, task_id, project_id, agent_id, fence, adapter,
                    status, boot_id, run_dir, started_at
                ) VALUES (
                    'run_f8', 'task', 'tsk_f8', 'prj_f8', 'agt_fx', 1, 'fake',
                    'running', 'boot_1', '/tmp/run_f8', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO leases (
                    key, run_id, holder_kind, holder, agent_id, fence,
                    slots_in_use_at_acquire, limit_at_acquire,
                    acquired_at, expires_at, heartbeat_at
                ) VALUES (
                    'task:tsk_f8', 'run_f8', 'meshd', 'boot_1', 'agt_fx', 1,
                    0, 2, '2026-11-07T00:00:00.000Z', '2026-11-07T01:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            record_event(
                self.conn,
                type_="task.submitted",
                actor_id="agt_fx",
                actor_kind="agent",
                via="token",
                project_id="prj_f8",
                task_id="tsk_f8",
                from_state="running",
                to_state="submitted",
            )

        violations_f8 = check_invariants(self.conn)
        self.assertEqual(violations_f8, [], f"F8 state had unexpected violations: {violations_f8}")

        # --- State T9: cancelling with a live run ---
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, assignee_agent_id,
                    profile, timeout_s, max_attempts, fence_seq, created_by,
                    created_at, updated_at
                ) VALUES (
                    'tsk_t9', 'prj_f8', 'T9 Task', 'Spec', 'code', 'cancelling', 'agt_fx',
                    'edit', 3600, 3, 2, 'usr_1',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO runs (
                    id, kind, task_id, project_id, agent_id, fence, adapter,
                    status, boot_id, run_dir, started_at
                ) VALUES (
                    'run_t9', 'task', 'tsk_t9', 'prj_f8', 'agt_fx', 2, 'fake',
                    'running', 'boot_1', '/tmp/run_t9', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO leases (
                    key, run_id, holder_kind, holder, agent_id, fence,
                    slots_in_use_at_acquire, limit_at_acquire,
                    acquired_at, expires_at, heartbeat_at
                ) VALUES (
                    'task:tsk_t9', 'run_t9', 'meshd', 'boot_1', 'agt_fx', 2,
                    1, 2, '2026-11-07T00:00:00.000Z', '2026-11-07T01:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            record_event(
                self.conn,
                type_="task.cancelling",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_f8",
                task_id="tsk_t9",
                from_state="running",
                to_state="cancelling",
            )

        violations_t9 = check_invariants(self.conn)
        self.assertEqual(violations_t9, [], f"T9 state had unexpected violations: {violations_t9}")

        # --- State D15: limit lowered below the running count ---
        # agt_fx currently has 2 active task leases (tsk_f8 and tsk_t9), both acquired with limit 2.
        # Lower max_concurrency from 2 down to 1.
        with transaction(self.conn):
            self.conn.execute("UPDATE agents SET max_concurrency = 1 WHERE id = 'agt_fx';")

        violations_d15 = check_invariants(self.conn)
        self.assertEqual(violations_d15, [], f"D15 state had unexpected violations: {violations_d15}")

    def test_corrupted_i1_lease_on_accepted_task(self):
        """Verify corrupted I1: lease on an accepted task reports I1. ACC: A10"""
        self._create_active_project("prj_corrupt")
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, assignee_agent_id,
                    profile, timeout_s, max_attempts, fence_seq, created_by,
                    created_at, updated_at
                ) VALUES (
                    'tsk_acc', 'prj_corrupt', 'Accepted Task', 'Spec', 'code', 'accepted', 'agt_fx',
                    'edit', 3600, 3, 1, 'usr_1',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO runs (
                    id, kind, task_id, project_id, agent_id, fence, adapter,
                    status, boot_id, run_dir, started_at
                ) VALUES (
                    'run_acc', 'task', 'tsk_acc', 'prj_corrupt', 'agt_fx', 1, 'fake',
                    'running', 'boot_1', '/tmp/run_acc', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO leases (
                    key, run_id, holder_kind, holder, agent_id, fence,
                    slots_in_use_at_acquire, limit_at_acquire,
                    acquired_at, expires_at, heartbeat_at
                ) VALUES (
                    'task:tsk_acc', 'run_acc', 'meshd', 'boot_1', 'agt_fx', 1,
                    0, 2, '2026-11-07T00:00:00.000Z', '2026-11-07T01:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            record_event(
                self.conn,
                type_="task.accepted",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_corrupt",
                task_id="tsk_acc",
                to_state="accepted",
            )

        violations = check_invariants(self.conn)
        invariants = [v.invariant for v in violations]
        self.assertIn("I1", invariants)
        self.assertEqual(set(invariants), {"I1"}, violations)

    def test_corrupted_i2_fence_mismatch(self):
        """Verify corrupted I2: fence mismatch between lease, task, or run. ACC: A10"""
        self._create_active_project("prj_i2")
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, assignee_agent_id,
                    profile, timeout_s, max_attempts, fence_seq, created_by,
                    created_at, updated_at
                ) VALUES (
                    'tsk_i2', 'prj_i2', 'I2 Task', 'Spec', 'code', 'running', 'agt_fx',
                    'edit', 3600, 3, 1, 'usr_1',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO runs (
                    id, kind, task_id, project_id, agent_id, fence, adapter,
                    status, boot_id, run_dir, started_at
                ) VALUES (
                    'run_i2', 'task', 'tsk_i2', 'prj_i2', 'agt_fx', 1, 'fake',
                    'running', 'boot_1', '/tmp/run_i2', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            # Lease has fence 2 while task and run have fence 1
            self.conn.execute(
                """
                INSERT INTO leases (
                    key, run_id, holder_kind, holder, agent_id, fence,
                    slots_in_use_at_acquire, limit_at_acquire,
                    acquired_at, expires_at, heartbeat_at
                ) VALUES (
                    'task:tsk_i2', 'run_i2', 'meshd', 'boot_1', 'agt_fx', 2,
                    0, 2, '2026-11-07T00:00:00.000Z', '2026-11-07T01:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            record_event(
                self.conn,
                type_="task.running",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_i2",
                task_id="tsk_i2",
                to_state="running",
            )

        violations = check_invariants(self.conn)
        invariants = [v.invariant for v in violations]
        self.assertIn("I2", invariants)
        self.assertEqual(set(invariants), {"I2"}, violations)

    def test_corrupted_i3_slots_ge_limit(self):
        """Verify corrupted I3: slots_in_use_at_acquire >= limit_at_acquire reports I3. ACC: A10"""
        self._create_active_project("prj_i3")
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, assignee_agent_id,
                    profile, timeout_s, max_attempts, fence_seq, created_by,
                    created_at, updated_at
                ) VALUES (
                    'tsk_i3', 'prj_i3', 'I3 Task', 'Spec', 'code', 'running', 'agt_fx',
                    'edit', 3600, 3, 1, 'usr_1',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO runs (
                    id, kind, task_id, project_id, agent_id, fence, adapter,
                    status, boot_id, run_dir, started_at
                ) VALUES (
                    'run_i3', 'task', 'tsk_i3', 'prj_i3', 'agt_fx', 1, 'fake',
                    'running', 'boot_1', '/tmp/run_i3', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            # slots (2) >= limit (2)
            self.conn.execute(
                """
                INSERT INTO leases (
                    key, run_id, holder_kind, holder, agent_id, fence,
                    slots_in_use_at_acquire, limit_at_acquire,
                    acquired_at, expires_at, heartbeat_at
                ) VALUES (
                    'task:tsk_i3', 'run_i3', 'meshd', 'boot_1', 'agt_fx', 1,
                    2, 2, '2026-11-07T00:00:00.000Z', '2026-11-07T01:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            record_event(
                self.conn,
                type_="task.running",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_i3",
                task_id="tsk_i3",
                to_state="running",
            )

        violations = check_invariants(self.conn)
        invariants = [v.invariant for v in violations]
        self.assertIn("I3", invariants)
        self.assertEqual(set(invariants), {"I3"}, violations)

    def test_corrupted_i4_status_mismatch(self):
        """Verify corrupted I4: task status does not match latest event to_state. ACC: A10"""
        self._create_active_project("prj_i4")
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, assignee_agent_id,
                    profile, timeout_s, max_attempts, fence_seq, created_by,
                    created_at, updated_at
                ) VALUES (
                    'tsk_i4', 'prj_i4', 'I4 Task', 'Spec', 'code', 'running', 'agt_fx',
                    'edit', 3600, 3, 1, 'usr_1',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO runs (
                    id, kind, task_id, project_id, agent_id, fence, adapter,
                    status, boot_id, run_dir, started_at
                ) VALUES (
                    'run_i4', 'task', 'tsk_i4', 'prj_i4', 'agt_fx', 1, 'fake',
                    'running', 'boot_1', '/tmp/run_i4', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            self.conn.execute(
                """
                INSERT INTO leases (
                    key, run_id, holder_kind, holder, agent_id, fence,
                    slots_in_use_at_acquire, limit_at_acquire,
                    acquired_at, expires_at, heartbeat_at
                ) VALUES (
                    'task:tsk_i4', 'run_i4', 'meshd', 'boot_1', 'agt_fx', 1,
                    0, 2, '2026-11-07T00:00:00.000Z', '2026-11-07T01:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            # Latest event says queued, but task status is running
            record_event(
                self.conn,
                type_="task.queued",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_i4",
                task_id="tsk_i4",
                to_state="queued",
            )

        violations = check_invariants(self.conn)
        invariants = [v.invariant for v in violations]
        self.assertIn("I4", invariants)
        self.assertEqual(set(invariants), {"I4"}, violations)

    def test_corrupted_i5_lead_assignments(self):
        """Verify corrupted I5: active project with zero active leads. ACC: A10"""
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO projects (
                    id, name, goal, status, created_by, created_via, created_at, updated_at
                ) VALUES (
                    'prj_nolead', 'No Lead Prj', 'Goal', 'active', 'usr_1', 'direct',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            record_event(
                self.conn,
                type_="project.created",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_nolead",
                to_state="active",
            )

        violations = check_invariants(self.conn)
        invariants = [v.invariant for v in violations]
        self.assertIn("I5", invariants)
        self.assertEqual(set(invariants), {"I5"}, violations)

    def test_corrupted_i6_consumed_approval(self):
        """Verify corrupted I6: consumed approval missing required attributes. ACC: A10"""
        self._create_active_project("prj_i6")
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO approvals (
                    id, project_id, category, op_type, op_json, op_hash, summary,
                    requested_by, requested_at, expires_at, status
                ) VALUES (
                    'apr_1', 'prj_i6', 'config', 'config.set', '{}', 'hash_1', 'summary',
                    'usr_1', '2026-11-07T00:00:00.000Z', '2026-11-08T00:00:00.000Z', 'consumed'
                );
                """
            )

        violations = check_invariants(self.conn)
        invariants = [v.invariant for v in violations]
        self.assertIn("I6", invariants)
        self.assertEqual(set(invariants), {"I6"}, violations)

    def _tamper_events(self, sql, params=()):
        """Drop the append-only update trigger, run a tampering UPDATE, and restore the trigger."""
        self.conn.execute("DROP TRIGGER events_no_update;")
        self.conn.execute(sql, params)
        self.conn.execute(
            "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
            "BEGIN SELECT RAISE(ABORT,'events are append-only'); END;"
        )
        self.conn.commit()

    def test_corrupted_i7_event_hash_tampered(self):
        """Verify corrupted I7: an event payload edited after the fact breaks the hash chain. ACC: A10"""
        self._create_active_project("prj_i7")
        self.assertEqual(check_invariants(self.conn), [], "pre-tamper state must be valid")
        seq = self.conn.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1;").fetchone()[0]
        self._tamper_events("UPDATE events SET actor_id = 'agt_fx' WHERE seq = ?;", (seq,))
        violations = check_invariants(self.conn)
        self.assertEqual({v.invariant for v in violations}, {"I7"}, violations)
        self.assertTrue(any(v.details.get("seq") == seq for v in violations), violations)

    def test_corrupted_i7_event_prev_hash_broken(self):
        """Verify corrupted I7: a broken prev_hash link reports I7 only. ACC: A10"""
        self._create_active_project("prj_i7a")
        self._create_active_project("prj_i7b")
        self.assertEqual(check_invariants(self.conn), [], "pre-tamper state must be valid")
        seq = self.conn.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1;").fetchone()[0]
        self._tamper_events("UPDATE events SET prev_hash = 'sha256:' || hex(randomblob(32)) WHERE seq = ?;", (seq,))
        violations = check_invariants(self.conn)
        self.assertEqual({v.invariant for v in violations}, {"I7"}, violations)
        self.assertTrue(any("prev_hash" in v.message and v.details.get("seq") == seq for v in violations), violations)

    def test_corrupted_i8_cancelling_task_without_lease(self):
        """Verify corrupted I8: launch-mode task cancelling without lease. ACC: A10"""
        self._create_active_project("prj_i8")
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, assignee_agent_id,
                    profile, timeout_s, max_attempts, fence_seq, created_by,
                    created_at, updated_at
                ) VALUES (
                    'tsk_i8', 'prj_i8', 'I8 Task', 'Spec', 'code', 'cancelling', 'agt_fx',
                    'edit', 3600, 3, 1, 'usr_1',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            record_event(
                self.conn,
                type_="task.cancelling",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_i8",
                task_id="tsk_i8",
                to_state="cancelling",
            )

        violations = check_invariants(self.conn)
        invariants = [v.invariant for v in violations]
        self.assertIn("I8", invariants)
        self.assertEqual(set(invariants), {"I8"}, violations)


class TestInvariantI9(unittest.TestCase):
    """I9 clause (plan §5.6): merged code task commits exist and are ancestors of the integration branch.

    ACC: A8
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-inv-i9-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")
        self.conn = init_db(self.db_path)
        self.fixture = GitFixture()
        # Hermetic against the outer environment (review m2-r1): the I9 check runs git
        # in-process with os.environ, so drop every inherited GIT_* (an outer GIT_DIR from a
        # hook or wrapper would select another repository) and add the fixture's git keys.
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[key]
        os.environ.update(self.fixture.git_env_vars())
        self.repo = self.fixture.make_k0()
        self.integration = "mesh/prj_x/integration"
        self.fixture.git(self.repo, "branch", self.integration, "main")
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO boots (id, pid, started_at) VALUES ('boot_1', 1000, '2026-11-07T00:00:00.000Z');"
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', '2026-11-07T00:00:00.000Z');"
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', '2026-11-07T00:00:00.000Z');"
            )
            self.conn.execute(
                """
                INSERT INTO agents (
                    id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at
                ) VALUES (
                    'agt_lead', 'none', 'inbox', 1, 1, '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )

    def tearDown(self):
        self.conn.close()
        self.fixture.cleanup()
        self.temp_dir.cleanup()

    def _create_repo_project(self, project_id: str = "prj_x", repo_path: str = None) -> None:
        """Create an active project with a repo, an integration branch and a lead."""
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO projects (
                    id, name, goal, status, repo_path, base_branch, integration_branch,
                    created_by, created_via, created_at, updated_at
                ) VALUES (
                    ?, 'Repo Project', 'Goal', 'active', ?, 'main', ?,
                    'usr_1', 'direct', '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """,
                (project_id, repo_path if repo_path is not None else self.repo, self.integration),
            )
            self.conn.execute(
                """
                INSERT INTO lead_assignments (
                    id, project_id, agent_id, assigned_by, via, started_at
                ) VALUES (
                    ?, ?, 'agt_lead', 'usr_1', 'direct', '2026-11-07T00:00:00.000Z'
                );
                """,
                (f"lead_{project_id}", project_id),
            )
            record_event(
                self.conn,
                type_="project.created",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id=project_id,
                to_state="active",
            )

    def _create_merged_task(self, task_id: str, project_id: str, merge_json) -> None:
        """Create a merged code task with the given merge_json value (None = SQL NULL)."""
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, assignee_agent_id,
                    profile, timeout_s, max_attempts, fence_seq, merge_json, created_by,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, 'Merged Task', 'Spec', 'code', 'merged', 'agt_lead',
                    'edit', 3600, 3, 1, ?, 'usr_1',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """,
                (task_id, project_id, merge_json),
            )
            record_event(
                self.conn,
                type_="task.merged",
                actor_id="agt_lead",
                actor_kind="agent",
                via="token",
                project_id=project_id,
                task_id=task_id,
                from_state="accepted",
                to_state="merged",
            )

    def _merge_commit_on_integration(self) -> str:
        """Make a real merge commit on the integration branch and return its sha.

        The `--no-ff` merge is performed in a worktree of `mesh/prj_x/integration`, so the
        returned commit is a merge commit on that branch (not `main`'s initial commit).
        """
        self.fixture.git(self.repo, "branch", "mesh/prj_x/tsk_1", "main")
        wt = self.fixture.path("wt-tsk1")
        self.fixture.git(self.repo, "worktree", "add", wt, "mesh/prj_x/tsk_1")
        with open(os.path.join(wt, "calc.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef mul(a, b):\n    return a * b\n")
        self.fixture.git(wt, "add", "-A")
        self.fixture.git(wt, "commit", "-m", "task work", identity=True)
        self.fixture.git(self.repo, "worktree", "remove", wt)

        integ = self.fixture.path("wt-integ")
        self.fixture.git(self.repo, "worktree", "add", integ, self.integration)
        self.fixture.git(integ, "merge", "--no-ff", "--no-commit", "mesh/prj_x/tsk_1", identity=True)
        self.fixture.git(integ, "commit", "-m", "merge tsk_1", identity=True)
        merge_commit = self.fixture.head(integ, "HEAD")
        self.fixture.git(self.repo, "worktree", "remove", integ)
        return merge_commit

    def test_i9_merged_task_with_ancestor_commit_is_clean(self):
        """I9 clause: a real merge commit on the integration branch yields zero violations. ACC: A8"""
        self._create_repo_project()
        initial = self.fixture.head(self.repo, "main")
        merge_commit = self._merge_commit_on_integration()
        # It really is a merge commit on the integration branch, not main's initial commit.
        self.assertNotEqual(merge_commit, initial)
        self.assertEqual(
            self.fixture.git(self.repo, "rev-parse", f"{merge_commit}^2").returncode, 0
        )
        # One more commit on the integration branch makes the merge commit a strict ancestor.
        integ = self.fixture.path("wt-integ-2")
        self.fixture.git(self.repo, "worktree", "add", integ, self.integration)
        with open(os.path.join(integ, "calc.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef neg(a):\n    return -a\n")
        self.fixture.git(integ, "add", "-A")
        self.fixture.git(integ, "commit", "-m", "integration follow-up", identity=True)
        self.assertNotEqual(self.fixture.head(integ, "HEAD"), merge_commit)
        self.fixture.git(self.repo, "worktree", "remove", integ)

        self._create_merged_task("tsk_1", "prj_x", json.dumps({"merge_commit": merge_commit}))
        self.assertEqual(check_invariants(self.conn), [])

    def test_i9_commit_not_ancestor(self):
        """I9 clause: a commit that exists but is not an ancestor of the integration branch. ACC: A8"""
        self._create_repo_project()
        self.fixture.git(self.repo, "branch", "side", "main")
        self.fixture.git(self.repo, "worktree", "add", self.fixture.path("wt-side"), "side")
        wt = self.fixture.path("wt-side")
        with open(os.path.join(wt, "calc.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef div(a, b):\n    return a / b\n")
        self.fixture.git(wt, "add", "-A")
        self.fixture.git(wt, "commit", "-m", "side work", identity=True)
        self.fixture.git(self.repo, "worktree", "remove", wt)
        side_sha = self.fixture.head(self.repo, "side")
        self._create_merged_task("tsk_1", "prj_x", json.dumps({"merge_commit": side_sha}))

        violations = check_invariants(self.conn)
        self.assertEqual([v.invariant for v in violations], ["I9"])
        self.assertEqual(violations[0].details["task_id"], "tsk_1")
        self.assertIn("not an ancestor", violations[0].details["reason"])

    def test_i9_bogus_sha(self):
        """I9 clause: a merge_commit that does not exist in the repo. ACC: A8"""
        self._create_repo_project()
        bogus = "0" * 40
        self._create_merged_task("tsk_1", "prj_x", json.dumps({"merge_commit": bogus}))

        violations = check_invariants(self.conn)
        self.assertEqual([v.invariant for v in violations], ["I9"])
        self.assertEqual(violations[0].details["task_id"], "tsk_1")
        self.assertIn("does not exist", violations[0].details["reason"])

    def test_i9_missing_merge_json(self):
        """I9 clause: a merged code task with merge_json NULL. ACC: A8"""
        self._create_repo_project()
        self._create_merged_task("tsk_1", "prj_x", None)

        violations = check_invariants(self.conn)
        self.assertEqual([v.invariant for v in violations], ["I9"])
        self.assertEqual(violations[0].details["task_id"], "tsk_1")
        self.assertIn("merge_json", violations[0].details["reason"])

    def test_i9_missing_repo_path_is_one_violation(self):
        """I9 clause: a missing repo dir is one violation, not a crash. ACC: A8"""
        self._create_repo_project(repo_path=self.fixture.path("does-not-exist"))
        self._create_merged_task("tsk_1", "prj_x", json.dumps({"merge_commit": "a" * 40}))

        violations = check_invariants(self.conn)
        self.assertEqual([v.invariant for v in violations], ["I9"])
        self.assertIn("repo_path", violations[0].details["reason"])

    def test_i9_non_sha_merge_commit_is_rejected_before_git(self):
        """I9 clause: a merge_commit that is not a sha is one violation and starts no git. ACC: A8"""
        self._create_repo_project()
        head_sha = self.fixture.head(self.repo, "main")
        cases = {
            "tsk_1": "--upload-pack=x",
            # A trailing newline must not slip past the sha check into git argv.
            "tsk_2": head_sha + "\n",
        }
        for task_id, merge_commit in cases.items():
            self._create_merged_task(task_id, "prj_x", json.dumps({"merge_commit": merge_commit}))

        with mock.patch(
            "mesh.core.gitops.subprocess.run",
            side_effect=AssertionError("no git subprocess may start for a non-sha merge_commit"),
        ):
            violations = check_invariants(self.conn)
        self.assertEqual([v.invariant for v in violations], ["I9", "I9"])
        self.assertEqual([v.details["task_id"] for v in violations], ["tsk_1", "tsk_2"])
        for violation in violations:
            self.assertIn("not a sha", violation.details["reason"])

    def test_i9_merge_base_error_is_reported_as_git_error(self):
        """I9 clause: a git error (unknown integration branch) is not reported as 'not an ancestor'. ACC: A8"""
        self._create_repo_project()
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE projects SET integration_branch = 'mesh/prj_x/no-such-branch' WHERE id = 'prj_x';"
            )
        self._create_merged_task("tsk_1", "prj_x", json.dumps({"merge_commit": self.fixture.head(self.repo, "main")}))

        violations = check_invariants(self.conn)
        self.assertEqual([v.invariant for v in violations], ["I9"])
        self.assertEqual(violations[0].details["task_id"], "tsk_1")
        reason = violations[0].details["reason"]
        self.assertIn("git merge-base failed (rc ", reason)
        self.assertNotIn("not an ancestor", reason)

    def test_i9_no_merged_tasks_starts_no_git(self):
        """I9 clause: without merged code tasks there are no violations and no git subprocess. ACC: A8"""
        self._create_repo_project()
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, assignee_agent_id,
                    profile, timeout_s, max_attempts, fence_seq, created_by,
                    created_at, updated_at
                ) VALUES (
                    'tsk_open', 'prj_x', 'Open Task', 'Spec', 'code', 'accepted', 'agt_lead',
                    'edit', 3600, 3, 1, 'usr_1',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            record_event(
                self.conn,
                type_="task.accepted",
                actor_id="agt_lead",
                actor_kind="agent",
                via="token",
                project_id="prj_x",
                task_id="tsk_open",
                from_state="submitted",
                to_state="accepted",
            )

        with mock.patch(
            "mesh.core.gitops.subprocess.run",
            side_effect=AssertionError("no git subprocess may start without merged code tasks"),
        ):
            self.assertEqual(check_invariants(self.conn), [])


if __name__ == "__main__":
    unittest.main()
