"""Integration tests for delegated approvals (approval.mode = delegated).

The project's current inbox lead asks the human and decides on their behalf with
--human-confirmed; the human's words are recorded in the approval and the audit event.
"""

import json
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness
from tests.integration.test_projects import create_agent


class TestDelegatedApproval(unittest.TestCase):

    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_lead2 = create_agent(self.harness, "lead2", can_lead=True)
        self.tok_plain = create_agent(self.harness, "plain", capabilities=["intake"])
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("intake", "submit", "--title", "P", "--goal", "G",
                                    "--criterion", "c1", token=self.tok_plain)
        self.assertEqual(rc, 0, resp)
        self.prj = resp["data"]["id"]

    def tearDown(self) -> None:
        self.harness.teardown()

    # helpers
    def _set_mode(self, mode: str) -> None:
        rc, resp = self.harness.cli("config", "set", "approval.mode", json.dumps(mode), user=True)
        self.assertEqual(rc, 0, resp)

    def _gated_pause(self):
        rc, resp = self.harness.cli("project", "pause", self.prj, token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED, resp)
        d = resp["error"]["details"]
        return d["approval_id"], d["op_hash"]

    def _decide(self, verb: str, apr: str, h: str, token: str, words="James 08:03: yes, go ahead", *extra):
        args = ["approval", verb, apr, "--op-hash", h]
        if words is not None:
            args += ["--human-confirmed", words]
        return self.harness.cli(*args, *extra, token=token)

    # tests
    def test_default_mode_is_passphrase_and_refuses_delegation(self) -> None:
        apr, h = self._gated_pause()
        rc, resp = self._decide("approve", apr, h, self.tok_lead1)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        self.assertEqual(resp["error"]["code"], c.CAPABILITY_MISSING)
        self.assertEqual(resp["error"]["details"]["setting"], "approval.mode")
        rc, resp = self.harness.cli("approval", "show", apr, user=True)
        self.assertEqual(resp["data"]["status"], "pending")

    def test_mode_setting_validation_and_user_only(self) -> None:
        rc, resp = self.harness.cli("config", "set", "approval.mode", '"bogus"', user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)
        rc, resp = self.harness.cli("config", "set", "approval.mode", '"delegated"', token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)

    def test_lead_approves_on_human_word_and_action_runs(self) -> None:
        self._set_mode("delegated")
        apr, h = self._gated_pause()
        rc, resp = self._decide("approve", apr, h, self.tok_lead1, "James 08:03: yes, pause it", "--note", "asked in chat")
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["status"], "approved")
        self.assertTrue(resp["data"]["delegated"])
        self.assertEqual(resp["data"]["trust"], "relayed_unverified")

        rc, show = self.harness.cli("approval", "show", apr, user=True)
        self.assertEqual(rc, 0, show)
        self.assertTrue(show["data"]["delegated"])
        self.assertIn("delegated", show["data"]["via"])
        self.assertEqual(show["data"]["user_statement"], "James 08:03: yes, pause it")
        self.assertEqual(show["data"]["relayed_by"], show["data"]["decided_by"])

        conn = self.harness.db()
        ev = conn.execute("SELECT via, relayed_by, payload_json FROM events WHERE type='approval.decided' "
                          "AND approval_id = ?;", (apr,)).fetchone()
        conn.close()
        self.assertEqual(ev["via"], "delegated")
        payload = json.loads(ev["payload_json"])
        self.assertEqual(payload["mode"], "delegated")
        self.assertEqual(payload["human_confirmed"], "James 08:03: yes, pause it")

        rc, resp = self.harness.cli("project", "pause", self.prj, "--approval", apr, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["status"], "paused")

        # Same decision again: idempotent and still re-authorized.
        rc, resp = self._decide("approve", apr, h, self.tok_lead2)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)

        rc, audit = self.harness.cli("audit", "verify", user=True)
        self.assertEqual(rc, 0, audit)

    def test_lead_rejects_on_human_word(self) -> None:
        self._set_mode("delegated")
        apr, h = self._gated_pause()
        rc, resp = self._decide("reject", apr, h, self.tok_lead1, "James: no, keep it running")
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["status"], "rejected")
        rc, resp = self.harness.cli("project", "pause", self.prj, "--approval", apr, token=self.tok_lead1)
        self.assertNotEqual(rc, 0, resp)

    def test_only_the_current_lead_can_delegate(self) -> None:
        self._set_mode("delegated")
        apr, h = self._gated_pause()
        for tok in (self.tok_lead2, self.tok_plain):
            rc, resp = self._decide("approve", apr, h, tok)
            self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
            self.assertEqual(resp["error"]["code"], c.NOT_PROJECT_LEAD)
        # The user approves directly, not with --human-confirmed.
        rc, resp = self.harness.cli("approval", "approve", apr, "--op-hash", h,
                                    "--human-confirmed", "me", user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        # A relayer cannot mix the two paths.
        rc, resp = self._decide("approve", apr, h, self.tok_grok, "James: ok", "--relayed",
                                "--user-statement", "James: ok")
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        # Without --human-confirmed the lead still cannot decide.
        rc, resp = self._decide("approve", apr, h, self.tok_lead1, None)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        # Empty words are refused.
        rc, resp = self._decide("approve", apr, h, self.tok_lead1, "   ")
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        # A former lead loses the right as soon as leadership moves.
        rc, resp = self.harness.cli("project", "set-lead", self.prj, "lead2", "--reason", "swap", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self._decide("approve", apr, h, self.tok_lead1)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        self.assertEqual(resp["error"]["code"], c.NOT_PROJECT_LEAD)
        # The handover cancelled the old lead's open request; the new lead cannot revive it.
        rc, resp = self._decide("approve", apr, h, self.tok_lead2)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)
        self.assertEqual(resp["error"]["code"], c.APPROVAL_CANCELLED)

    def test_direct_only_category_still_needs_passphrase(self) -> None:
        self._set_mode("delegated")
        rc, resp = self.harness.cli("config", "set", "approval.policy.project_control", '"direct_only"', user=True)
        self.assertEqual(rc, 0, resp)
        apr, h = self._gated_pause()
        rc, resp = self._decide("approve", apr, h, self.tok_lead1)
        self.assertEqual(rc, c.EXIT_APPROVAL_INVALID, resp)
        self.assertEqual(resp["error"]["code"], c.CHANNEL_NOT_ALLOWED)

    def test_lead_is_told_and_pending_lists_commands(self) -> None:
        apr0, _ = self._gated_pause()
        conn = self.harness.db()
        n0 = conn.execute("SELECT COUNT(*) AS n FROM outbox WHERE type='approval.requested' AND recipient_id = "
                          "(SELECT id FROM principals WHERE name='lead1');").fetchone()["n"]
        conn.close()
        self.assertEqual(n0, 0)

        self._set_mode("delegated")
        rc, resp = self.harness.cli("intake", "submit", "--title", "P2", "--goal", "G",
                                    "--criterion", "c1", token=self.tok_plain)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("project", "pause", resp["data"]["id"], token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED, resp)
        conn = self.harness.db()
        n1 = conn.execute("SELECT COUNT(*) AS n FROM outbox WHERE type='approval.requested' AND recipient_id = "
                          "(SELECT id FROM principals WHERE name='lead1');").fetchone()["n"]
        conn.close()
        self.assertEqual(n1, 1)

        rc, resp = self.harness.cli("approval", "pending", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        items = resp["data"]["items"]
        self.assertEqual(resp["data"]["count"], 2)
        it = [i for i in items if i["id"] == apr0][0]
        self.assertIn("--human-confirmed", it["approve"])
        self.assertIn(it["op_hash"], it["reject"])
        self.assertTrue(it["summary"])

    def test_evidence_ref_is_redacted_for_delegated_and_relayed(self) -> None:
        secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0"
        self._set_mode("delegated")
        apr, h = self._gated_pause()
        rc, resp = self._decide("approve", apr, h, self.tok_lead1, f"James: yes {secret}",
                                "--evidence-ref", f"https://chat.example/m?k={secret}")
        self.assertEqual(rc, 0, resp)
        # Relayed path, on a second project the relayer does not lead.
        rc, resp = self.harness.cli("intake", "submit", "--title", "P2", "--goal", "G",
                                    "--criterion", "c1", token=self.tok_plain)
        prj2 = resp["data"]["id"]
        rc, resp = self.harness.cli("project", "pause", prj2, token=self.tok_lead1)
        d = resp["error"]["details"]
        rc, resp = self.harness.cli("approval", "approve", d["approval_id"], "--op-hash", d["op_hash"],
                                    "--relayed", "--user-statement", "James: ok",
                                    "--evidence-ref", f"ref {secret}", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        conn = self.harness.db()
        rows = conn.execute("SELECT user_statement, evidence_ref FROM approvals;").fetchall()
        evs = conn.execute("SELECT payload_json FROM events WHERE type='approval.decided';").fetchall()
        conn.close()
        blob = json.dumps([dict(r) for r in rows]) + json.dumps([dict(e) for e in evs])
        self.assertNotIn(secret, blob)
        for aid in (apr, d["approval_id"]):
            rc, show = self.harness.cli("approval", "show", aid, user=True)
            self.assertNotIn(secret, json.dumps(show))
        rc, lst = self.harness.cli("approval", "list", user=True)
        self.assertNotIn(secret, json.dumps(lst))

    def test_pending_commands_match_mode_caller_and_policy(self) -> None:
        apr, h = self._gated_pause()
        # Default mode, the user: a direct --user command, no --human-confirmed.
        rc, resp = self.harness.cli("approval", "pending", user=True)
        self.assertEqual(rc, 0, resp)
        it = resp["data"]["items"][0]
        self.assertEqual(it["decision_channel"], "direct")
        self.assertIn("--user", it["approve"])
        self.assertNotIn("--human-confirmed", it["approve"])
        # Default mode, the lead: only the user can decide.
        rc, resp = self.harness.cli("approval", "pending", token=self.tok_lead1)
        it = resp["data"]["items"][0]
        self.assertEqual(it["decision_channel"], "user_only")
        self.assertNotIn("--human-confirmed", it["approve"])
        self.assertIn("note", it)
        # Delegated mode, the lead: a delegated command that carries its token file.
        self._set_mode("delegated")
        rc, resp = self.harness.cli("approval", "pending", token=self.tok_lead1)
        it = resp["data"]["items"][0]
        self.assertEqual(it["decision_channel"], "delegated")
        self.assertIn("--human-confirmed", it["approve"])
        self.assertIn("--token-file", it["approve"])
        self.assertNotIn("<token file>", it["approve"])
        # Delegated mode but direct_only policy: back to the user.
        rc, resp = self.harness.cli("config", "set", "approval.policy.project_control", '"direct_only"', user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("approval", "pending", token=self.tok_lead1)
        it = resp["data"]["items"][0]
        self.assertEqual(it["decision_channel"], "user_only")
        self.assertNotIn("--human-confirmed", it["approve"])

    def test_pending_for_relayer_and_env_token(self) -> None:
        apr, h = self._gated_pause()
        rc, resp = self.harness.cli("approval", "pending", "--relayed", "--user-statement", "Human: show",
                                    token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        it = resp["data"]["items"][0]
        self.assertEqual(it["decision_channel"], "relayed")
        self.assertIn("--relayed", it["approve"])
        rc, resp = self.harness.cli("approval", "pending", extra_env={"MESH_TOKEN": self.tok_lead1})
        if rc == 0:
            it = resp["data"]["items"][0]
            self.assertNotIn("<token file>", it["approve"])
            self.assertNotIn(self.tok_lead1, it["approve"])
            self.assertNotIn("--token-file", it["approve"])

    def test_disabled_hint_is_a_valid_command(self) -> None:
        apr, h = self._gated_pause()
        rc, resp = self._decide("approve", apr, h, self.tok_lead1)
        self.assertIn("'\"delegated\"'", resp["error"]["message"])


if __name__ == "__main__":
    unittest.main()
