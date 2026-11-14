"""Unit tests for lease and fence verification per plan §11.5. ACC: H4"""

import os
import sqlite3
import unittest

import mesh.constants as c
from mesh.errors import MeshError
from mesh.core.leases import verify_task_lease_and_fence, get_lease_ttl_s


class TestLeasesUnit(unittest.TestCase):
    """Unit tests for task lease and fence verification helper. ACC: H4"""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE leases (
                key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                holder_kind TEXT NOT NULL,
                holder TEXT NOT NULL,
                agent_id TEXT,
                fence INTEGER NOT NULL,
                slots_in_use_at_acquire INTEGER,
                limit_at_acquire INTEGER,
                total_at_acquire INTEGER,
                total_limit_at_acquire INTEGER,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            );"""
        )
        self.conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT);")

    def tearDown(self) -> None:
        self.conn.close()

    def test_verify_lease_expired_when_no_lease(self) -> None:
        """ACC: H4 - When no lease exists for task, verify_task_lease_and_fence raises rc 9 LEASE_EXPIRED."""
        with self.assertRaises(MeshError) as ctx:
            verify_task_lease_and_fence(self.conn, "tsk_123", presented_fence=1)
        self.assertEqual(ctx.exception.code, c.LEASE_EXPIRED)
        self.assertEqual(ctx.exception.exit_code, c.EXIT_STALE)

    def test_verify_fence_mismatch_when_fence_differs(self) -> None:
        """ACC: H4 - When lease exists but presented fence differs, raises rc 9 FENCE_MISMATCH."""
        self.conn.execute(
            """INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at)
               VALUES ('task:tsk_123', 'run_1', 'agent', 'ag_1', 'ag_1', 2, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', '2026-01-01T00:00:00Z');"""
        )

        # Scope fence 1 vs lease fence 2 -> FENCE_MISMATCH
        with self.assertRaises(MeshError) as ctx:
            verify_task_lease_and_fence(self.conn, "tsk_123", presented_fence=1)
        self.assertEqual(ctx.exception.code, c.FENCE_MISMATCH)
        self.assertEqual(ctx.exception.exit_code, c.EXIT_STALE)

    def test_verify_run_id_mismatch(self) -> None:
        """ACC: H4 - When lease exists but presented run_id differs, raises rc 9 FENCE_MISMATCH."""
        self.conn.execute(
            """INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at)
               VALUES ('task:tsk_123', 'run_1', 'agent', 'ag_1', 'ag_1', 2, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', '2026-01-01T00:00:00Z');"""
        )

        with self.assertRaises(MeshError) as ctx:
            verify_task_lease_and_fence(self.conn, "tsk_123", presented_run_id="run_other", presented_fence=2)
        self.assertEqual(ctx.exception.code, c.FENCE_MISMATCH)
        self.assertEqual(ctx.exception.exit_code, c.EXIT_STALE)

    def test_verify_matching_lease_and_fence_succeeds(self) -> None:
        """When presented fence and run_id match the active lease, verification succeeds and returns lease dict."""
        self.conn.execute(
            """INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at)
               VALUES ('task:tsk_123', 'run_1', 'agent', 'ag_1', 'ag_1', 2, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', '2026-01-01T00:00:00Z');"""
        )

        lease = verify_task_lease_and_fence(self.conn, "tsk_123", presented_run_id="run_1", presented_fence=2)
        self.assertEqual(lease["key"], "task:tsk_123")
        self.assertEqual(lease["run_id"], "run_1")
        self.assertEqual(lease["fence"], 2)

    def test_get_lease_ttl_s_default_and_setting(self) -> None:
        """Verify get_lease_ttl_s defaults to 120s and respects DB setting."""
        self.assertEqual(get_lease_ttl_s(self.conn), 120)

        self.conn.execute("INSERT INTO settings (key, value_json) VALUES ('lease.ttl_s', '30');")
        self.assertEqual(get_lease_ttl_s(self.conn), 30)


class TestRunTokenSubmitFence(unittest.TestCase):
    """ACC: H4 (unit clause) - a run token whose scope fence differs from the lease fence
    calls `task submit` (no explicit --fence): rc 9 FENCE_MISMATCH, no state change, and the
    current (fence-2) run is unaffected."""

    def setUp(self) -> None:
        import tempfile
        from mesh.core.db import init_db

        self.tmp = tempfile.TemporaryDirectory(prefix="mh-h4-")
        self.addCleanup(self.tmp.cleanup)
        self.conn = init_db(os.path.join(self.tmp.name, "mesh.db"))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        x = self.conn.execute
        x("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1','user','user','t');")
        x("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_fx','agent','fx','t');")
        x("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
          "VALUES ('prj_1','P','g','active','usr_1','cli','t','t');")
        x("INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
          "attempt, fence_seq, created_by, created_at, updated_at) "
          "VALUES ('tsk_1','prj_1','T','s','general','running','edit',600,3,2,2,'agt_fx','t','t');")
        for run_id, fence, status in (("run_1", 1, "lost"), ("run_2", 2, "running")):
            x("INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, boot_id, run_dir) "
              "VALUES (?, 'task', 'tsk_1', 'prj_1', ?, ?, 'fake', ?, 'boot', '/nonexistent');",
              (run_id, fence, fence, status))
        x("INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
          "VALUES ('task:tsk_1','run_2','meshd','boot','agt_fx',2,'2026-01-01T00:00:00Z','2999-01-01T00:00:00Z',"
          "'2026-01-01T00:00:00Z');")
        self.conn.commit()

    def snapshot(self):
        q = self.conn.execute
        return (
            [tuple(r) for r in q("SELECT * FROM tasks;")],
            [tuple(r) for r in q("SELECT * FROM leases;")],
            [tuple(r) for r in q("SELECT * FROM runs ORDER BY id;")],
            q("SELECT COUNT(*) FROM events;").fetchone()[0],
            q("SELECT COUNT(*) FROM outbox;").fetchone()[0],
        )

    def submit_as(self, scope_run: str, scope_fence: int):
        from mesh.daemon.handlers.base import HandlerContext
        from mesh.daemon.handlers.task import handle_task_submit

        principal = {"id": "agt_fx", "kind": "agent", "name": "fx",
                     "scope": {"task_id": "tsk_1", "project_id": "prj_1", "run_id": scope_run,
                               "fence": scope_fence}}
        ctx = HandlerContext(conn=self.conn, principal=principal, actor_kind="agent", via="run_token",
                             request_id="req_1", home=self.tmp.name, cmd="task.submit",
                             args={"task_id": "tsk_1", "summary": "late"})
        return handle_task_submit(ctx)

    def test_scope_fence_differs_from_lease_fence(self) -> None:
        before = self.snapshot()
        for scope_run, scope_fence in (("run_1", 1), ("run_2", 1)):
            with self.subTest(scope_run=scope_run, scope_fence=scope_fence):
                with self.assertRaises(MeshError) as ctx:
                    self.submit_as(scope_run, scope_fence)
                self.assertEqual(ctx.exception.code, c.FENCE_MISMATCH)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_STALE)
                self.assertEqual(self.snapshot(), before)
        row = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_1';").fetchone()
        self.assertEqual(row["status"], "running")
        lease = self.conn.execute("SELECT run_id, fence FROM leases WHERE key = 'task:tsk_1';").fetchone()
        self.assertEqual((lease["run_id"], lease["fence"]), ("run_2", 2))


if __name__ == "__main__":
    unittest.main()
