"""Unit tests for the lead-turn termination reason (plan §8, §11.4; checklist L4).

Review m2-r10 finding 1: the set-lead termination of the old lead's turn must not live
only in meshd's memory. `lead_turn_termination_reason` derives `lead_superseded` from
the DB on every watch pass, so a restart or a failed set-lead effect still ends the turn.
"""

import os
import tempfile
import unittest

from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db
from mesh.daemon.lead_turns import lead_turn_termination_reason


class TestLeadTurnTerminationReason(unittest.TestCase):
    """ACC: L4 (durable supersede) - the watch pass's reason for a live lead turn."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-leadsupersede-unit-")
        self.conn = init_db(os.path.join(self.temp_dir.name, "mesh.db"))
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', 'now');")
        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at)
               VALUES ('prj_1', 'P', 'Goal', 'active', 'usr_1', 'direct', 1, 'now', 'now');""")
        for name in ("lead0", "lead1"):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, 'now');", (name, name))
            self.conn.execute(
                """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json,
                                       allowed_models_json, max_concurrency, created_at, updated_at)
                   VALUES (?, 'fake', 'launch', 1, '[]', '["fake-model"]', 1, 'now', 'now');""", (name,))
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at,
                                             ended_at, ended_reason)
               VALUES ('asg_old', 'prj_1', 'lead0', 'usr_1', 'direct', 'now', 'now', 'set-lead');""")
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
               VALUES ('asg_cur', 'prj_1', 'lead1', 'usr_1', 'direct', 'now');""")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    @staticmethod
    def _run(assignment_id: str, deadline_in_s: float) -> dict:
        import time

        return {"id": "run_1", "kind": "lead_turn", "project_id": "prj_1",
                "lead_assignment_id": assignment_id,
                "deadline_at": now_iso_utc(time.time() + deadline_in_s)}

    def test_durable_supersede_reason(self) -> None:
        """ACC: L4 (durable supersede) - an old-assignment run is `lead_superseded`; the
        current assignment is None; the current assignment past its deadline is
        `timeout`; an old assignment past its deadline is still `lead_superseded`."""
        self.assertEqual(lead_turn_termination_reason(self.conn, self._run("asg_old", 600)), "lead_superseded")
        self.assertIsNone(lead_turn_termination_reason(self.conn, self._run("asg_cur", 600)))
        self.assertEqual(lead_turn_termination_reason(self.conn, self._run("asg_cur", -5)), "timeout")
        self.assertEqual(lead_turn_termination_reason(self.conn, self._run("asg_old", -5)), "lead_superseded")


if __name__ == "__main__":
    unittest.main()
