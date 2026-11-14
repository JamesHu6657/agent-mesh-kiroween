"""Sakana review of public PR #4, core half, end to end.

P1-1: in delegated mode the lead may decide an approval it requested itself; that is now
explicit (``approval.delegated_self_approve``, default true; ``self_requested`` in the
event, the response and ``approval show``) and can be turned off (-> SELF_APPROVAL).
P2-3: a keyed ``task start`` retry without MESH_SESSION replays a first call that set it.
"""

import json
import unittest

import mesh.constants as c
from tests.integration import test_merge as tm
from tests.integration.test_delegated_approval import TestDelegatedApproval

NO_SESSION = {"MESH_SESSION": ""}


class TestDelegatedSelfApproval(unittest.TestCase):
    setUp = TestDelegatedApproval.setUp
    tearDown = TestDelegatedApproval.tearDown
    _set_mode = TestDelegatedApproval._set_mode
    _gated_pause = TestDelegatedApproval._gated_pause
    _decide = TestDelegatedApproval._decide

    def _set_self(self, value) -> None:
        rc, resp = self.harness.cli("config", "set", "approval.delegated_self_approve", json.dumps(value), user=True)
        self.assertEqual(rc, 0, resp)

    def _event_payload(self, apr: str) -> dict:
        conn = self.harness.db()
        ev = conn.execute("SELECT payload_json FROM events WHERE type='approval.decided' AND approval_id = ?;",
                          (apr,)).fetchone()
        conn.close()
        return json.loads(ev["payload_json"])

    def test_default_allows_and_records_self_requested(self) -> None:
        self._set_mode("delegated")
        rc, resp = self.harness.cli("config", "get", "approval.delegated_self_approve", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertIs(resp["data"]["value"], True)
        apr, h = self._gated_pause()  # lead1 raised it itself
        rc, resp = self._decide("approve", apr, h, self.tok_lead1, "James 09:00: yes, pause it")
        self.assertEqual(rc, 0, resp)
        self.assertIs(resp["data"]["self_requested"], True)
        self.assertIs(self._event_payload(apr)["self_requested"], True)
        rc, show = self.harness.cli("approval", "show", apr, user=True)
        self.assertEqual(rc, 0, show)
        self.assertIs(show["data"]["self_requested"], True)
        self.assertIn("its own request", show["data"]["via"])
        self.assertEqual(show["data"]["requested_by"], show["data"]["relayed_by"])
        rc, txt = self.harness.cli("--format", "text", "approval", "show", apr, user=True)
        self.assertIn("self_requested", json.dumps(txt))
        rc, audit = self.harness.cli("audit", "verify", user=True)
        self.assertEqual(rc, 0, audit)

    def test_off_refuses_self_approval_but_user_still_decides(self) -> None:
        self._set_mode("delegated")
        self._set_self(False)
        apr, h = self._gated_pause()
        rc, resp = self._decide("approve", apr, h, self.tok_lead1)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        self.assertEqual(resp["error"]["code"], c.SELF_APPROVAL)
        self.assertEqual(resp["error"]["details"]["setting"], "approval.delegated_self_approve")
        self.assertIn("approval.delegated_self_approve true --user", resp["error"]["message"])
        rc, resp = self._decide("reject", apr, h, self.tok_lead1, "James: no")
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        self.assertEqual(resp["error"]["code"], c.SELF_APPROVAL)
        rc, show = self.harness.cli("approval", "show", apr, user=True)
        self.assertEqual(show["data"]["status"], "pending")
        # approval pending tells the lead it is the user's call now.
        rc, resp = self.harness.cli("approval", "pending", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        it = [i for i in resp["data"]["items"] if i["id"] == apr][0]
        self.assertEqual(it["decision_channel"], "user_only")
        self.assertNotIn("--human-confirmed", it["approve"])
        # The user's direct approval works and is not marked self_requested.
        rc, resp = self.harness.cli("approval", "approve", apr, "--op-hash", h, user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["decided_via"], "direct")
        self.assertNotIn("self_requested", self._event_payload(apr))
        # Turning it back on restores the lead's path for new requests.
        self._set_self(True)
        rc, resp = self.harness.cli("project", "pause", self.prj, "--approval", apr, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)

    def test_off_does_not_block_requests_raised_by_others(self) -> None:
        self._set_mode("delegated")
        self._set_self(False)
        # A different requester (the relayer agent) asks for an external action in lead1's project.
        rc, resp = self.harness.cli("approval", "request", "--category", "deploy", "--summary", "ship",
                                    "--operation", '{"what":"deploy"}', "--project", self.prj,
                                    token=self.tok_grok)
        if rc != c.EXIT_APPROVAL_REQUIRED and rc != 0:
            self.skipTest(f"approval request by a non-lead not allowed here: {resp}")
        d = resp.get("data") or resp["error"]["details"]
        apr, h = d["approval_id"] if "approval_id" in d else d["id"], d["op_hash"]
        rc, resp = self._decide("approve", apr, h, self.tok_lead1, "James: ship it")
        self.assertEqual(rc, 0, resp)
        self.assertIs(resp["data"]["self_requested"], False)
        self.assertIs(self._event_payload(apr)["self_requested"], False)

    def test_setting_validation(self) -> None:
        for bad in ('"yes"', "1", "null"):
            rc, resp = self.harness.cli("config", "set", "approval.delegated_self_approve", bad, user=True)
            self.assertEqual(rc, c.EXIT_USAGE, (bad, resp))
            self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)
        rc, resp = self.harness.cli("config", "set", "approval.delegated_self_approve", "false",
                                    token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)


class TestKeyedStartReplayAcrossSession(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project

    def create_task(self, title: str) -> str:
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", title, "--spec", "Work",
                                    "--assignee", "ibx", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def test_retry_without_session_replays_first_call_with_session(self) -> None:
        tsk = self.create_task("Keyed start, session then none")
        args = ("--idempotency-key", "start-sk-1", "task", "start", tsk)
        rc, first = self.harness.cli(*args, token=self.tok_ibx, extra_env={"MESH_SESSION": "ses_mine_1"})
        self.assertEqual(rc, 0, first)
        self.assertEqual(first["data"]["session"], "ses_mine_1")
        # Lost response; the retry runs in a shell without MESH_SESSION.
        rc, again = self.harness.cli(*args, token=self.tok_ibx, extra_env=NO_SESSION)
        self.assertEqual(rc, 0, again)
        self.assertTrue(again.get("idempotent_replay") or again.get("meta", {}).get("idempotent_replay"), again)
        self.assertEqual(again["data"]["run_id"], first["data"]["run_id"])
        self.assertEqual(again["data"]["session"], "ses_mine_1")  # the run's real session comes back
        rc, hb = self.harness.cli("task", "heartbeat", tsk, "--run", first["data"]["run_id"],
                                  "--fence", str(first["data"]["fence"]), token=self.tok_ibx,
                                  extra_env={"MESH_SESSION": again["data"]["session"]})
        self.assertEqual(rc, 0, hb)

    def test_explicit_other_session_is_still_reused(self) -> None:
        tsk = self.create_task("Keyed start, two explicit sessions")
        args = ("--idempotency-key", "start-sk-2", "task", "start", tsk)
        rc, first = self.harness.cli(*args, token=self.tok_ibx, extra_env={"MESH_SESSION": "ses_a"})
        self.assertEqual(rc, 0, first)
        rc, again = self.harness.cli(*args, token=self.tok_ibx, extra_env={"MESH_SESSION": "ses_b"})
        self.assertEqual(rc, c.EXIT_CONFLICT, again)
        self.assertEqual(again["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)

    def test_other_changed_argument_is_still_reused(self) -> None:
        tsk = self.create_task("Keyed start one")
        tsk2 = self.create_task("Keyed start two")
        rc, first = self.harness.cli("--idempotency-key", "start-sk-3", "task", "start", tsk,
                                     token=self.tok_ibx, extra_env={"MESH_SESSION": "ses_c"})
        self.assertEqual(rc, 0, first)
        rc, again = self.harness.cli("--idempotency-key", "start-sk-3", "task", "start", tsk2,
                                     token=self.tok_ibx, extra_env=NO_SESSION)
        self.assertEqual(rc, c.EXIT_CONFLICT, again)
        self.assertEqual(again["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)


if __name__ == "__main__":
    unittest.main()
