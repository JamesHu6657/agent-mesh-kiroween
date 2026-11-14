"""Carbon review regressions; no synthetic decision rows without audit events."""
import json
import os
import sqlite3
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness
from tests.integration.test_projects import create_agent


class TestCarbonReviewApprovals(unittest.TestCase):
    def setUp(self):
        self.h = MeshHarness(init_db=True, start_daemon=True)
        self.lead = create_agent(self.h, "lead1", can_lead=True)
        create_agent(self.h, "lead2", can_lead=True)
        token = create_agent(self.h, "plain", capabilities=["intake"])
        self.ok("config", "set", "lead.default_agent", '"lead1"', user=True)
        data = self.ok("intake", "submit", "--title", "P", "--goal", "G",
                       "--criterion", "c1", token=token)
        self.project = data["id"]
        self.ok("config", "set", "approval.mode", '"delegated"', user=True)

    def tearDown(self):
        self.h.teardown()

    def ok(self, *args, **kw):
        rc, resp = self.h.cli(*args, **kw)
        self.assertEqual(rc, 0, resp)
        return resp["data"]

    def approved(self):
        secret = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4"
        data = self.ok("approval", "request", "--project", self.project, "--category", "deploy",
                       "--summary", "deploy " + secret, "--operation", '{"env":"prod"}', token=self.lead)
        aid, digest = data["approval_id"], data["op_hash"]
        self.ok("approval", "approve", aid, "--op-hash", digest,
                "--human-confirmed", "Human: yes", "--note", secret, token=self.lead)
        self.assertNotIn(secret, json.dumps(self.ok("approval", "show", aid, user=True)))
        return aid, digest

    def test_handover_cancels_approved_and_does_not_resurrect(self):
        aid, digest = self.approved()
        self.ok("project", "set-lead", self.project, "lead2", "--reason", "handover", user=True)
        self.ok("project", "set-lead", self.project, "lead1", "--reason", "return", user=True)
        self.assertEqual(self.ok("approval", "show", aid, user=True)["status"], "cancelled")
        rc, resp = self.h.cli("approval", "consume", aid, "--op-hash", digest, token=self.lead)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)
        self.assertEqual(resp["error"]["code"], c.APPROVAL_CANCELLED)
        conn = self.h.db()
        try:
            events = conn.execute("SELECT * FROM events WHERE type='approval.cancelled' AND approval_id=?", (aid,)).fetchall()
            self.assertEqual(len(events), 1)
            new_id = conn.execute("SELECT id FROM principals WHERE name='lead2'").fetchone()["id"]
            count = conn.execute("SELECT COUNT(*) AS n FROM outbox o JOIN events e ON o.event_seq=e.seq "
                                 "WHERE o.recipient_id=? AND e.approval_id=?", (new_id, aid)).fetchone()["n"]
            self.assertEqual(count, 0)
        finally:
            conn.close()
        self.ok("audit", "verify", user=True)

    def test_consumption_has_event_and_valid_audit(self):
        aid, digest = self.approved()
        self.ok("approval", "consume", aid, "--op-hash", digest, token=self.lead)
        conn = self.h.db()
        try:
            row = conn.execute("SELECT payload_json FROM events WHERE type='approval.consumed' AND approval_id=?", (aid,)).fetchone()
            self.assertEqual(json.loads(row["payload_json"])["op_hash"], digest)
        finally:
            conn.close()
        self.ok("audit", "verify", user=True)

    def test_direct_upgrade_and_cancellation_preserve_valid_signature(self):
        aid, digest = self.approved()
        self.ok("config", "set", "approval.policy.deploy", '"direct_only"', user=True)
        data = self.ok("approval", "approve", aid, "--op-hash", digest, user=True)
        self.assertEqual(data["decided_via"], "direct")
        self.assertEqual(data["upgraded_from"], "relayed")
        self.ok("audit", "verify", user=True)
        self.ok("approval", "cancel", aid, user=True)
        self.ok("audit", "verify", user=True)

    def test_cannot_revoke_active_lead_but_can_after_handover(self):
        rc, resp = self.h.cli("agent", "update", "lead1", "--no-can-lead", user=True)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["details"]["reason"], "active_lead")
        self.ok("project", "set-lead", self.project, "lead2", "--reason", "handover", user=True)
        self.ok("agent", "update", "lead1", "--no-can-lead", user=True)

    def test_audit_detects_operation_tampering(self):
        aid, digest = self.approved()
        conn = sqlite3.connect(os.path.join(self.h.home, "mesh.db"))
        try:
            conn.execute("UPDATE approvals SET op_json='{}' WHERE id=?", (aid,))
            conn.commit()
        finally:
            conn.close()
        rc, resp = self.h.cli("audit", "verify", user=True)
        self.assertEqual(resp["error"]["code"], c.AUDIT_MISMATCH)
        self.assertIn(aid, resp["error"]["details"]["broken_approval_ids"])


if __name__ == "__main__":
    unittest.main()
