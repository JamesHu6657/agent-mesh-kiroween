"""N19 (lead clause): no lead turns for vendor adapters in M3-lite.

The rule sits first in `lead_turns.lead_launch_check`, before m2-r10's launch gate: a vendor
lead adapter never starts a turn, whatever the launch evidence says. The messages stay
`pending` and one `lead.turn_refused {reason:"vendor_lead_m3_lite"}` is recorded per
lead assignment. Launch evidence is simulated with a launch-enabled droid adapter
patched into `lead_turns.get_adapter` (the evidence module of m3-r5 is not on this
branch); no vendor binary is ever started.
"""

import json
import os
import sqlite3
import unittest
from typing import Any
from unittest import mock

from mesh.adapters.droid import DroidAdapter
from mesh.adapters.fake import FakeAdapter
from mesh.daemon import lead_turns
from tests.integration.test_lead_actions import LeadActionCase
from tests.integration.test_lead_turns import create_agent


class _EvidencedDroid(DroidAdapter):
    """droid as if its launch evidence were complete (launch_enabled true)."""

    launch_enabled = True


class _DisabledDouble(FakeAdapter):
    """A non-vendor adapter (the test double) that is not launch-enabled."""

    launch_enabled = False


class TestVendorLeadGate(LeadActionCase):
    def _inbox_vendor_project(self, name: str = "vlead") -> str:
        # Inbox delivery: meshd's own tick never plans a turn for this lead, so the
        # only planner is the direct call below.
        create_agent(self.harness, name, adapter="droid", delivery="inbox", can_lead=True)
        rc, resp = self.harness.cli("intake", "submit", "--title", "Vendor gate", "--goal", "g",
                                    "--criterion", "C1", "--lead", name, token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def _lead(self, conn: Any, project_id: str) -> dict:
        row = conn.execute(
            "SELECT id AS assignment_id, agent_id, project_id FROM lead_assignments "
            "WHERE project_id = ? AND ended_at IS NULL;", (project_id,)).fetchone()
        return dict(row)

    def test_vendor_lead_with_evidence_never_starts_a_turn(self) -> None:
        """ACC: N19 - a launch-enabled (evidenced) vendor lead gets no turn: plan is None,
        no lead_turn run, messages stay pending, exactly one lead.turn_refused
        {reason: vendor_lead_m3_lite} per assignment, and no vendor binary ran."""
        self.assertTrue(lead_turns.lead_launch_allowed(None, FakeAdapter(), testing_mode=True))
        self.assertFalse(lead_turns.lead_launch_allowed(None, _EvidencedDroid(), testing_mode=True))
        self.assertFalse(lead_turns.lead_launch_allowed(None, DroidAdapter(), testing_mode=True))

        project_id = self._inbox_vendor_project()
        self._follow_up(project_id, "wake the vendor lead")
        wake_before = [(r["id"], r["status"]) for r in self._lead_wake_rows(project_id)]
        # A writable connection (harness.db() is read-only): the refusal records an event.
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            lead = self._lead(conn, project_id)
            messages = conn.execute(
                "SELECT * FROM outbox WHERE project_id = ? AND status = 'pending' "
                "ORDER BY event_seq ASC, id ASC;", (project_id,)).fetchall()
            with mock.patch.object(lead_turns, "get_adapter", lambda *a, **k: _EvidencedDroid()):
                for _ in range(3):
                    plan = lead_turns.plan_lead_turn(conn, self.harness.home, project_id, lead,
                                                     list(messages), testing_mode=True)
                    self.assertIsNone(plan)
        finally:
            conn.close()

        refused = self._events(project_id, "lead.turn_refused")
        self.assertEqual(len(refused), 1)
        payload = json.loads(refused[0]["payload_json"])
        self.assertEqual(payload["reason"], "vendor_lead_m3_lite")
        self.assertEqual(payload["code"], "ADAPTER_UNSUPPORTED")
        self.assertEqual(payload["milestone"], "M3")
        self.assertEqual(payload["lead_assignment_id"], lead["assignment_id"])
        self.assertEqual(self._lead_turn_runs(project_id), [])
        self.assertEqual([(r["id"], r["status"]) for r in self._lead_wake_rows(project_id)], wake_before)
        self.assertEqual({s for _, s in wake_before} - {"pending"}, set())

    def test_vendor_rule_runs_first(self) -> None:
        """ACC: N19 - the vendor rule runs first in `lead_launch_check` (coordinator ruling
        r4b): a vendor lead is refused with `vendor_lead_m3_lite` whether or not it is
        launch-enabled, and a non-vendor adapter that is not launch-enabled still gets
        the m2-r10 refusal (rc 13, no `details.reason`, reason
        `adapter_not_launch_enabled` in `lead.turn_refused`)."""
        for adapter in (DroidAdapter(), _EvidencedDroid()):
            for testing_mode in (True, False):
                with self.assertRaises(lead_turns.MeshError) as cm:
                    lead_turns.lead_launch_check(None, adapter, testing_mode=testing_mode)
                self.assertEqual(cm.exception.code, "ADAPTER_UNSUPPORTED")
                self.assertEqual(cm.exception.details, {"milestone": "M3", "reason": "vendor_lead_m3_lite"})
        with self.assertRaises(lead_turns.MeshError) as cm:
            lead_turns.lead_launch_check(None, _DisabledDouble(), testing_mode=True)
        self.assertEqual(cm.exception.code, "ADAPTER_UNSUPPORTED")
        self.assertNotIn("reason", cm.exception.details)

    def test_non_vendor_not_launch_enabled_refused_as_m2(self) -> None:
        """ACC: N19 - a launch lead on a non-vendor adapter that is not launch-enabled
        gets no turn and one `lead.turn_refused {reason: adapter_not_launch_enabled}`;
        a vendor lead that is not launch-enabled gets `vendor_lead_m3_lite`."""
        project_id = self._inbox_vendor_project()
        self._follow_up(project_id, "wake the lead")
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            lead = self._lead(conn, project_id)
            messages = conn.execute(
                "SELECT * FROM outbox WHERE project_id = ? AND status = 'pending' "
                "ORDER BY event_seq ASC, id ASC;", (project_id,)).fetchall()
            with mock.patch.object(lead_turns, "get_adapter", lambda *a, **k: _DisabledDouble()):
                self.assertIsNone(lead_turns.plan_lead_turn(conn, self.harness.home, project_id, lead,
                                                            list(messages), testing_mode=True))
        finally:
            conn.close()
        refused = self._events(project_id, "lead.turn_refused")
        self.assertEqual(len(refused), 1)
        self.assertEqual(json.loads(refused[0]["payload_json"])["reason"], "adapter_not_launch_enabled")
        self.assertEqual(self._lead_turn_runs(project_id), [])

        project2 = self._inbox_vendor_project("vlead2")
        self._follow_up(project2, "wake the vendor lead")
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            lead = self._lead(conn, project2)
            messages = conn.execute(
                "SELECT * FROM outbox WHERE project_id = ? AND status = 'pending' "
                "ORDER BY event_seq ASC, id ASC;", (project2,)).fetchall()
            with mock.patch.object(lead_turns, "get_adapter", lambda *a, **k: DroidAdapter()):
                self.assertIsNone(lead_turns.plan_lead_turn(conn, self.harness.home, project2, lead,
                                                            list(messages), testing_mode=True))
        finally:
            conn.close()
        refused = self._events(project2, "lead.turn_refused")
        self.assertEqual(len(refused), 1)
        self.assertEqual(json.loads(refused[0]["payload_json"])["reason"], "vendor_lead_m3_lite")
        self.assertEqual(self._lead_turn_runs(project2), [])

if __name__ == "__main__":
    unittest.main()
