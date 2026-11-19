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

    # --- PR #51 R1 ---------------------------------------------------------------

    def redacted_approval(self):
        secret = "sk-" + "z9y8x7w6v5u4t3s2r1q0p9o8n7m6"
        # A gated command whose parameters hold a secret (PR #40 r1: op_json stores a redacted copy).
        rc, resp = self.h.cli("project", "update", self.project, "--goal", "g " + secret, token=self.lead)
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED, resp)
        data = resp["error"]["details"]
        conn = self.h.db()
        try:
            op_json = conn.execute("SELECT op_json FROM approvals WHERE id=?", (data["approval_id"],)).fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn(secret, op_json)
        self.assertIs(json.loads(op_json)["_redacted"], True)
        return data["approval_id"], data["op_hash"]

    def write_db(self, sql, params=()):
        conn = sqlite3.connect(os.path.join(self.h.home, "mesh.db"))
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def test_r1_p1_3_redacted_op_json_keeps_audit_valid(self):
        aid, digest = self.redacted_approval()
        self.ok("audit", "verify", user=True)
        self.ok("approval", "approve", aid, "--op-hash", digest, user=True)
        self.ok("audit", "verify", user=True)

    def test_r1_p1_3_tampered_redacted_envelope_is_detected(self):
        aid, _digest = self.redacted_approval()
        conn = self.h.db()
        try:
            doc = json.loads(conn.execute("SELECT op_json FROM approvals WHERE id=?", (aid,)).fetchone()[0])
        finally:
            conn.close()
        forged = "sha256:" + "0" * 64
        doc["op_hash"] = forged  # a forged envelope and row agree, but the chained request event does not
        self.write_db("UPDATE approvals SET op_json=?, op_hash=? WHERE id=?", (json.dumps(doc), forged, aid))
        rc, resp = self.h.cli("audit", "verify", user=True)
        self.assertEqual(resp["error"]["code"], c.AUDIT_MISMATCH, resp)
        self.assertIn(aid, resp["error"]["details"]["broken_approval_ids"])
        doc["op"]["op"] = "something.else"  # an envelope that does not describe the row
        self.write_db("UPDATE approvals SET op_json=? WHERE id=?", (json.dumps(doc), aid))
        rc, resp = self.h.cli("audit", "verify", user=True)
        self.assertEqual(resp["error"]["code"], c.AUDIT_MISMATCH, resp)

    def test_r1_p1_3_consumed_event_for_unconsumed_row_is_detected(self):
        aid, digest = self.approved()
        self.ok("approval", "consume", aid, "--op-hash", digest, token=self.lead)
        self.write_db("UPDATE approvals SET status='approved', consumed_at=NULL, consumed_by=NULL, "
                      "consumed_request_id=NULL WHERE id=?", (aid,))
        rc, resp = self.h.cli("audit", "verify", user=True)
        self.assertEqual(resp["error"]["code"], c.AUDIT_MISMATCH, resp)
        self.assertIn(aid, resp["error"]["details"]["broken_approval_ids"])

    def test_r1_p2_1_lead_of_finished_projects_can_lose_can_lead(self):
        self.ok("project", "cancel", self.project, user=True)
        self.ok("agent", "update", "lead1", "--no-can-lead", user=True)

    def test_full_r1_p2_1_revoked_lead_keeps_no_lead_rights(self):
        self.ok("project", "cancel", self.project, user=True)
        self.ok("agent", "update", "lead1", "--no-can-lead", user=True)
        conn = self.h.db()
        try:
            from mesh.core.authz import is_acting_project_lead
            lead1 = conn.execute("SELECT id FROM principals WHERE name='lead1'").fetchone()["id"]
            self.assertFalse(is_acting_project_lead(conn, self.project, lead1))
        finally:
            conn.close()
        for extra in (["--project", self.project], []):
            rc, resp = self.h.cli("approval", "request", *extra, "--category", "deploy",
                                  "--summary", "x", "--operation", '{"env":"prod"}', token=self.lead)
            self.assertNotEqual(rc, 0, resp)

    def test_r1_p2_2_direct_upgrade_reaches_the_requester(self):
        aid, digest = self.approved()
        self.ok("config", "set", "approval.policy.deploy", '"direct_only"', user=True)
        self.ok("approval", "approve", aid, "--op-hash", digest, user=True)
        conn = self.h.db()
        try:
            keys = [r["dedup_key"] for r in conn.execute(
                "SELECT dedup_key FROM outbox WHERE dedup_key LIKE ?", (f"apr_dec:{aid}:%",)).fetchall()]
        finally:
            conn.close()
        self.assertIn(f"apr_dec:{aid}:approved", keys)
        self.assertIn(f"apr_dec:{aid}:approved:direct", keys)

    # --- PR #51 R2 ---------------------------------------------------------------

    def row(self, aid):
        conn = self.h.db()
        try:
            return dict(conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone())
        finally:
            conn.close()

    def test_r2_p1_2_forged_clean_redacted_envelope_is_refused(self):
        rc, resp = self.h.cli("project", "update", self.project, "--goal", "DROP ALL TABLES", token=self.lead)
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED, resp)
        aid, digest = resp["error"]["details"]["approval_id"], resp["error"]["details"]["op_hash"]
        doc = json.loads(self.row(aid)["op_json"])
        self.assertNotIn("_redacted", doc)
        doc["params"] = {k: ("Harmless typo fix" if k == "goal" else v) for k, v in doc["params"].items()}
        self.write_db("UPDATE approvals SET op_json=? WHERE id=?",
                      (json.dumps({"_redacted": True, "op": doc, "op_hash": digest}), aid))
        rc, resp = self.h.cli("approval", "approve", aid, "--op-hash", digest, user=True)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)
        self.assertEqual(resp["error"]["code"], c.OP_HASH_MISMATCH, resp)
        rc, resp = self.h.cli("audit", "verify", user=True)
        self.assertEqual(resp["error"]["code"], c.AUDIT_MISMATCH, resp)
        self.assertIn(aid, resp["error"]["details"]["broken_approval_ids"])

    def test_r2_p1_2_plain_op_json_must_describe_the_row(self):
        aid, digest = self.approved()
        self.write_db("UPDATE approvals SET op_type='deploy.other' WHERE id=?", (aid,))
        rc, resp = self.h.cli("audit", "verify", user=True)
        self.assertEqual(resp["error"]["code"], c.AUDIT_MISMATCH, resp)
        self.assertIn(aid, resp["error"]["details"]["broken_approval_ids"])

    def test_r2_p1_3_category_downgrade_is_refused_and_detected(self):
        self.ok("config", "set", "approval.policy.scope_change", '"direct_only"', user=True)
        aid, digest = self.redacted_approval()
        row = self.row(aid)
        self.assertEqual(row["category"], "scope_change")
        doc = json.loads(row["op_json"])
        doc["op"]["category"] = "deploy"
        self.write_db("UPDATE approvals SET category='deploy', op_json=? WHERE id=?", (json.dumps(doc), aid))
        rc, resp = self.h.cli("approval", "approve", aid, "--op-hash", digest,
                              "--human-confirmed", "Human: yes", token=self.lead)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)
        self.assertEqual(self.row(aid)["status"], "pending")
        rc, resp = self.h.cli("audit", "verify", user=True)
        self.assertEqual(resp["error"]["code"], c.AUDIT_MISMATCH, resp)
        self.assertIn(aid, resp["error"]["details"]["broken_approval_ids"])

    def test_r2_p1_3_row_without_requested_event_is_detected(self):
        aid, _digest = self.redacted_approval()
        row = self.row(aid)
        doc = json.loads(row["op_json"])
        forged_hash = "sha256:" + "f" * 64
        doc["op_hash"] = forged_hash
        self.write_db("INSERT INTO approvals (id, project_id, task_id, category, op_type, op_json, op_hash, summary, "
                      "requested_by, requested_at, expires_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending')",
                      ("apr_forged0000000000000000000", row["project_id"], row["task_id"], row["category"],
                       row["op_type"], json.dumps(doc), forged_hash, row["summary"], row["requested_by"],
                       row["requested_at"], row["expires_at"]))
        rc, resp = self.h.cli("audit", "verify", user=True)
        self.assertEqual(resp["error"]["code"], c.AUDIT_MISMATCH, resp)
        self.assertEqual(resp["error"]["details"]["broken_approval_ids"], ["apr_forged0000000000000000000"])
        rc, resp = self.h.cli("approval", "approve", "apr_forged0000000000000000000", "--op-hash", forged_hash, user=True)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)

    # --- PR #51 R3 ---------------------------------------------------------------

    def test_r3_p1_1_consume_refuses_a_rewritten_category(self):
        aid, digest = self.approved()  # delegated decision, category deploy
        self.ok("config", "set", "approval.policy.deploy", '"direct_only"', user=True)
        rc, resp = self.h.cli("approval", "consume", aid, "--op-hash", digest, token=self.lead)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)
        self.write_db("UPDATE approvals SET category='external_other' WHERE id=?", (aid,))
        rc, resp = self.h.cli("approval", "consume", aid, "--op-hash", digest, token=self.lead)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)
        self.assertIn(resp["error"]["details"].get("reason"), ("op_json_mismatch", "requested_event_mismatch"), resp)
        self.assertEqual(self.row(aid)["status"], "approved")

    def test_r3_p1_1_consume_refuses_a_decision_without_its_event(self):
        rc, resp = self.h.cli("project", "update", self.project, "--goal", "G2", token=self.lead)
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED, resp)
        aid, digest = resp["error"]["details"]["approval_id"], resp["error"]["details"]["op_hash"]
        lead_id = self.row(aid)["requested_by"]
        self.write_db("UPDATE approvals SET status='approved', decided_via='relayed', decided_by=?, relayed_by=?, "
                      "decided_at=requested_at WHERE id=?", (lead_id, lead_id, aid))
        rc, resp = self.h.cli("approval", "consume", aid, "--op-hash", digest, token=self.lead)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)
        self.assertEqual(resp["error"]["details"].get("reason"), "decision_event_mismatch", resp)
        rc, resp = self.h.cli("project", "update", self.project, "--goal", "G2", "--approval", aid, token=self.lead)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)
        self.assertEqual(self.row(aid)["status"], "approved")

    def test_r3_p1_1_audit_checks_the_consumed_event_payload(self):
        aid, digest = self.approved()
        self.ok("approval", "consume", aid, "--op-hash", digest, token=self.lead)
        self.ok("audit", "verify", user=True)
        self.write_db("UPDATE approvals SET consumed_by='agt_someone_else' WHERE id=?", (aid,))
        rc, resp = self.h.cli("audit", "verify", user=True)
        self.assertEqual(resp["error"]["code"], c.AUDIT_MISMATCH, resp)
        self.assertIn(aid, resp["error"]["details"]["broken_approval_ids"])


if __name__ == "__main__":
    unittest.main()
