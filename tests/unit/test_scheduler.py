"""Unit tests for scheduler atomicity and promotions per plan §5.2, §6.2, §16 M1.6."""

import os
import tempfile
import unittest
from unittest.mock import patch

from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.daemon.scheduler import check_queued_tasks_t6, promote_tasks


class TestSchedulerUnit(unittest.TestCase):
    """Unit test suite for scheduler transaction atomicity."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-sched-unit-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")
        self.conn = init_db(self.db_path)
        now = now_iso_utc()

        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr-1', 'user', 'testuser', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt-lead', 'agent', 'lead1', ?);",
                (now,),
            )
            self.conn.execute(
                """INSERT INTO agents (
                    id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at
                ) VALUES ('agt-lead', 'none', 'inbox', 1, 1, ?, ?);""",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt-ibx', 'agent', 'ibx', ?);",
                (now,),
            )
            self.conn.execute(
                """INSERT INTO agents (
                    id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at
                ) VALUES ('agt-ibx', 'none', 'inbox', 0, 1, ?, ?);""",
                (now, now),
            )
            self.conn.execute(
                """INSERT INTO projects (
                    id, name, goal, status, created_by, created_via, created_at, updated_at
                ) VALUES ('prj-1', 'Proj 1', 'Goal', 'active', 'usr-1', 'direct', ?, ?);""",
                (now, now),
            )
            self.conn.execute(
                """INSERT INTO lead_assignments (
                    id, project_id, agent_id, assigned_by, via, started_at
                ) VALUES ('la-1', 'prj-1', 'agt-lead', 'usr-1', 'direct', ?);""",
                (now,),
            )
            self.conn.execute(
                """INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, priority,
                    assignee_agent_id, profile, timeout_s, max_attempts,
                    created_by, version, created_at, updated_at
                ) VALUES (
                    'tsk-1', 'prj-1', 'Task 1', 'Spec 1', 'general', 'assigned', 0,
                    'agt-ibx', 'edit', 3600, 1, 'usr-1', 1, ?, ?
                );""",
                (now, now),
            )

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_promotion_failure_rolls_back_atomically(self) -> None:
        """When event recording fails during promotion, the status update is rolled back and other tasks continue."""
        with patch("mesh.daemon.scheduler.record_event", side_effect=RuntimeError("injected event error")):
            promoted = promote_tasks(self.conn, home=self.temp_dir.name)
            self.assertEqual(promoted, [])

        # Verify task is still in 'assigned' status and version is unchanged
        row = self.conn.execute("SELECT status, version, queued_at FROM tasks WHERE id = 'tsk-1';").fetchone()
        self.assertEqual(row["status"], "assigned")
        self.assertEqual(row["version"], 1)
        self.assertIsNone(row["queued_at"])

        # Verify no orphan events were recorded
        ev_rows = self.conn.execute("SELECT seq, type FROM events WHERE task_id = 'tsk-1';").fetchall()
        self.assertEqual(len(ev_rows), 0)

        # Verify no outbox rows exist
        outbox_rows = self.conn.execute("SELECT id FROM outbox WHERE project_id = 'prj-1';").fetchall()
        self.assertEqual(len(outbox_rows), 0)

    def test_promotion_tick_isolation_task_a_fails_task_b_promotes(self) -> None:
        """One task failing during promotion does not prevent another task from being promoted."""
        from mesh.core.events import record_event as real_record_event

        now = now_iso_utc()
        self.conn.execute(
            """INSERT INTO tasks (
                id, project_id, title, spec, kind, status, priority,
                assignee_agent_id, profile, timeout_s, max_attempts,
                created_by, version, created_at, updated_at
            ) VALUES (
                'tsk-2', 'prj-1', 'Task 2', 'Spec 2', 'general', 'assigned', 0,
                'agt-ibx', 'edit', 3600, 1, 'usr-1', 1, ?, ?
            );""",
            (now, now),
        )

        def selective_record_event(*args, **kwargs):
            if kwargs.get("task_id") == "tsk-1":
                raise RuntimeError("injected tsk-1 error")
            return real_record_event(*args, **kwargs)

        with patch("mesh.daemon.scheduler.record_event", side_effect=selective_record_event):
            promoted = promote_tasks(self.conn, home=self.temp_dir.name)

        # tsk-1 failed and rolled back; tsk-2 succeeded
        self.assertEqual(promoted, ["tsk-2"])

        row1 = self.conn.execute("SELECT status, version FROM tasks WHERE id = 'tsk-1';").fetchone()
        self.assertEqual(row1["status"], "assigned")
        self.assertEqual(row1["version"], 1)

        row2 = self.conn.execute("SELECT status, version FROM tasks WHERE id = 'tsk-2';").fetchone()
        self.assertEqual(row2["status"], "queued")
        self.assertEqual(row2["version"], 2)

    def test_promotion_success_commits_all_atomically(self) -> None:
        """Successful promotion updates task status, bumps version, records events and outbox."""
        promoted = promote_tasks(self.conn)
        self.assertEqual(promoted, ["tsk-1"])

        row = self.conn.execute("SELECT status, version, queued_at FROM tasks WHERE id = 'tsk-1';").fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["version"], 2)
        self.assertIsNotNone(row["queued_at"])

        # Check task.queued and task.offered events exist
        ev_types = [r["type"] for r in self.conn.execute("SELECT type FROM events WHERE task_id = 'tsk-1' ORDER BY seq ASC;").fetchall()]
        self.assertIn("task.queued", ev_types)
        self.assertIn("task.offered", ev_types)

        # Check outbox has single task.offered message
        ob_rows = self.conn.execute("SELECT type, recipient_id, dedup_key FROM outbox WHERE recipient_id = 'agt-ibx';").fetchall()
        self.assertEqual(len(ob_rows), 1)
        self.assertEqual(ob_rows[0]["type"], "task.offered")
        self.assertEqual(ob_rows[0]["dedup_key"], "offered:tsk-1:1:v2")

    def test_t6_model_not_allowed_and_rollback(self) -> None:
        """T6 failure transitions queued task to failed atomically or rolls back on error."""
        import json

        now = now_iso_utc()
        # Set task tsk-1 to queued with model 'disallowed-model'
        self.conn.execute(
            """UPDATE tasks
               SET status = 'queued', model = 'disallowed-model', queued_at = ?
               WHERE id = 'tsk-1';""",
            (now,),
        )

        # 1. Rollback test when event recording fails
        with patch("mesh.daemon.scheduler.record_event", side_effect=RuntimeError("injected t6 event error")):
            failed = check_queued_tasks_t6(self.conn, home=self.temp_dir.name)
            self.assertEqual(failed, [])

        # Task remains queued and version unchanged
        row = self.conn.execute("SELECT status, version, failure_json FROM tasks WHERE id = 'tsk-1';").fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["version"], 1)
        self.assertIsNone(row["failure_json"])

        # 2. Success test: task transitions to failed, version bumped, failure_json recorded
        failed = check_queued_tasks_t6(self.conn)
        self.assertEqual(failed, ["tsk-1"])

        row_after = self.conn.execute("SELECT status, version, failure_json FROM tasks WHERE id = 'tsk-1';").fetchone()
        self.assertEqual(row_after["status"], "failed")
        self.assertEqual(row_after["version"], 2)
        self.assertIsNotNone(row_after["failure_json"])
        fail_dict = json.loads(row_after["failure_json"])
        self.assertEqual(fail_dict["reason"], "model_not_allowed")

        # Outbox message fanned out to lead
        ob_rows = self.conn.execute("SELECT type, recipient_id, payload_json FROM outbox WHERE recipient_id = 'agt-lead';").fetchall()
        self.assertEqual(len(ob_rows), 1)
        self.assertEqual(ob_rows[0]["type"], "task.failed")
        payload = json.loads(ob_rows[0]["payload_json"])
        self.assertEqual(payload["attempt"], 0)
        self.assertEqual(payload["max_attempts"], 1)

    def test_t6_adapter_unavailable(self) -> None:
        """T6 failure transitions queued task to failed when launch adapter is unavailable."""
        import json
        from unittest.mock import MagicMock

        now = now_iso_utc()
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt-launch', 'agent', 'launch-agt', ?);",
            (now,),
        )
        self.conn.execute(
            """INSERT INTO agents (
                id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at
            ) VALUES ('agt-launch', 'fake', 'launch', 0, 1, ?, ?);""",
            (now, now),
        )
        self.conn.execute(
            """INSERT INTO tasks (
                id, project_id, title, spec, kind, status, priority,
                assignee_agent_id, profile, timeout_s, max_attempts, attempt, fence_seq,
                created_by, version, queued_at, created_at, updated_at
            ) VALUES (
                'tsk-launch', 'prj-1', 'Launch Task', 'Spec', 'general', 'queued', 0,
                'agt-launch', 'edit', 3600, 2, 0, 0, 'usr-1', 1, ?, ?, ?
            );""",
            (now, now, now),
        )

        mock_adapter = MagicMock()
        mock_adapter.kind = "cli"
        mock_adapter.is_installed.return_value = False

        with patch("mesh.daemon.scheduler.get_adapter", return_value=mock_adapter):
            failed = check_queued_tasks_t6(self.conn)

        self.assertIn("tsk-launch", failed)
        row = self.conn.execute("SELECT status, version, failure_json FROM tasks WHERE id = 'tsk-launch';").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["version"], 2)
        fail_dict = json.loads(row["failure_json"])
        self.assertEqual(fail_dict["reason"], "adapter_unavailable")
        self.assertEqual(fail_dict["details"], {"adapter": "fake"})


if __name__ == "__main__":
    unittest.main()
