"""Unit tests for event hash chain and tamper detection per plan §5.5.

ACC: F3
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest

import mesh.constants as c
from mesh.core.db import init_db, transaction
from mesh.core.events import compute_event_hash, record_event, verify_event_chain
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.event import handle_event_list, handle_event_tail_live
from mesh.errors import MeshError


class TestEvents(unittest.TestCase):
    """Test event recording, hash chain calculation, and verification."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-events-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_event_hash_chain(self):
        """Verify unbroken event hash chain from genesis."""
        with transaction(self.conn):
            e1 = record_event(
                self.conn,
                type_="project.created",
                actor_id="usr_001",
                actor_kind="user",
                via="direct",
                project_id="prj_001",
                to_state="active",
                payload={"goal": "Build feature"},
            )

        self.assertEqual(e1["prev_hash"], "genesis")
        self.assertEqual(len(e1["hash"]), 64)

        with transaction(self.conn):
            e2 = record_event(
                self.conn,
                type_="task.created",
                actor_id="usr_001",
                actor_kind="user",
                via="direct",
                project_id="prj_001",
                task_id="tsk_001",
                to_state="created",
                payload={"title": "Task 1"},
            )

        self.assertEqual(e2["prev_hash"], e1["hash"])

        with transaction(self.conn):
            e3 = record_event(
                self.conn,
                type_="task.assigned",
                actor_id="usr_001",
                actor_kind="user",
                via="direct",
                project_id="prj_001",
                task_id="tsk_001",
                from_state="created",
                to_state="assigned",
                payload={"assignee": "agt_001"},
            )

        self.assertEqual(e3["prev_hash"], e2["hash"])

        # Chain verification should find zero violations
        violations = verify_event_chain(self.conn)
        self.assertEqual(violations, [])

    def test_tamper_detection(self):
        """Verify hash tampering or broken prev_hash is reported as I7 violation. ACC: F3"""
        with transaction(self.conn):
            e1 = record_event(
                self.conn,
                type_="project.created",
                actor_id="usr_001",
                actor_kind="user",
                via="direct",
                project_id="prj_001",
                to_state="active",
            )
            e2 = record_event(
                self.conn,
                type_="task.created",
                actor_id="usr_001",
                actor_kind="user",
                via="direct",
                project_id="prj_001",
                task_id="tsk_001",
                to_state="created",
            )

        # Directly tamper with row by temporarily bypassing triggers if triggers allow,
        # or inserting a forged event with invalid hash / invalid prev_hash
        with transaction(self.conn):
            # Insert a third event with an invalid prev_hash
            self.conn.execute(
                """
                INSERT INTO events (
                    id, ts, type, actor_id, actor_kind, via,
                    prev_hash, hash
                ) VALUES (
                    'evt_forged', '2026-11-07T00:00:00.000Z', 'forged.event',
                    'usr_001', 'user', 'direct',
                    'bogus_prev_hash', 'bogus_hash'
                );
                """
            )

        violations = verify_event_chain(self.conn)
        self.assertGreaterEqual(len(violations), 1)
        self.assertEqual(violations[0]["invariant"], "I7")

    def test_event_list_and_tail_authz_bypass_rejected(self):
        """Verify that event.list and event.tail do not honor __cmd__ authz bypass in ctx.args."""
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', 'now');"
        )
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agent_unrelated', 'agent', 'unrelated', 'now');"
        )
        self.conn.execute(
            """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at)
               VALUES ('agent_unrelated', 'none', 'inbox', 0, '[]', 1, 'now', 'now');"""
        )
        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, origin_principal_id, created_by, created_via, created_at, updated_at)
               VALUES ('prj_secret', 'Secret', 'Goal', 'active', 'usr_1', 'usr_1', 'direct', 'now', 'now');"""
        )

        unrelated_principal = {"id": "agent_unrelated", "name": "unrelated", "capabilities": []}
        run_token_principal = {"id": "agent_unrelated", "name": "unrelated", "scope": {"task_id": "tsk_x", "project_id": "prj_x"}}

        # (a) agent principal without access with __cmd__=project.list
        ctx_agent_list = HandlerContext(
            conn=self.conn,
            principal=unrelated_principal,
            actor_kind="agent",
            via="token",
            request_id="req-1",
            home=self.temp_dir.name,
            cmd="event.list",
            args={"project": "prj_secret", "__cmd__": "project.list"},
        )
        with self.assertRaises(MeshError) as cm:
            handle_event_list(ctx_agent_list)
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)

        # (b) run-token principal with __cmd__=task.heartbeat
        ctx_run_list = HandlerContext(
            conn=self.conn,
            principal=run_token_principal,
            actor_kind="agent",
            via="run_token",
            request_id="req-2",
            home=self.temp_dir.name,
            cmd="event.list",
            args={"project": "prj_secret", "__cmd__": "task.heartbeat"},
        )
        with self.assertRaises(MeshError) as cm:
            handle_event_list(ctx_run_list)
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)

        # (c) both cases again through handle_event_tail_live
        ctx_agent_tail = HandlerContext(
            conn=self.conn,
            principal=unrelated_principal,
            actor_kind="agent",
            via="token",
            request_id="req-3",
            home=self.temp_dir.name,
            cmd="event.tail",
            args={"project": "prj_secret", "__cmd__": "project.list"},
        )
        with self.assertRaises(MeshError) as cm:
            asyncio.run(handle_event_tail_live(None, ctx_agent_tail, None, None))
        self.assertEqual(cm.exception.code, c.SCOPE_MISMATCH)

        ctx_run_tail = HandlerContext(
            conn=self.conn,
            principal=run_token_principal,
            actor_kind="agent",
            via="run_token",
            request_id="req-4",
            home=self.temp_dir.name,
            cmd="event.tail",
            args={"project": "prj_secret", "__cmd__": "task.heartbeat"},
        )
        with self.assertRaises(MeshError) as cm:
            asyncio.run(handle_event_tail_live(None, ctx_run_tail, None, None))
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)


if __name__ == "__main__":
    unittest.main()
