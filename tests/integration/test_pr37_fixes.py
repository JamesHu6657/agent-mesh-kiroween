"""PR #37 review fixes, end to end: keyed `task start` replay with the default session (D2),
`agent token issue --out` on a replay and with an empty path (#2, #3), and the text form of
`inbox list` / `inbox wait` showing the payload fields (#6)."""

import os
import unittest

import mesh.constants as c
from tests.integration import test_merge as tm

NO_SESSION = {"MESH_SESSION": ""}  # the CLI generates the default session itself


class TestPR37(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project

    def create_task(self, title: str, assignee: str = "ibx") -> str:
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", title, "--spec", "Work",
                                    "--assignee", assignee, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def test_d2_keyed_start_retry_replays_with_its_session(self) -> None:
        tsk = self.create_task("Keyed start")
        args = ("--idempotency-key", "start-once-1", "task", "start", tsk)
        rc, first = self.harness.cli(*args, token=self.tok_ibx, extra_env=NO_SESSION)
        self.assertEqual(rc, 0, first)
        session = first["data"]["session"]
        self.assertRegex(session, r"^ses_[0-9a-f]{16}$")
        # The response was "lost": the same command again replays it instead of IDEMPOTENCY_KEY_REUSED.
        rc, again = self.harness.cli(*args, token=self.tok_ibx, extra_env=NO_SESSION)
        self.assertEqual(rc, 0, again)
        self.assertTrue(again.get("idempotent_replay") or again.get("meta", {}).get("idempotent_replay"), again)
        self.assertEqual(again["data"]["run_id"], first["data"]["run_id"])
        self.assertEqual(again["data"]["session"], session)
        # The replayed session is the run's: a heartbeat with it is accepted.
        rc, hb = self.harness.cli("task", "heartbeat", tsk, "--run", first["data"]["run_id"],
                                  "--fence", str(first["data"]["fence"]), token=self.tok_ibx,
                                  extra_env={"MESH_SESSION": session})
        self.assertEqual(rc, 0, hb)
        # Without a key every start still gets a fresh random session (another agent's task:
        # ibx is at its concurrency limit).
        other = self.create_task("Unkeyed start", assignee="plain")
        rc, st = self.harness.cli("task", "start", other, token=self.tok_plain, extra_env=NO_SESSION)
        self.assertEqual(rc, 0, st)
        self.assertNotEqual(st["data"]["session"], session)

    def test_token_issue_out_replay_never_overwrites_and_empty_out_refused(self) -> None:
        out = os.path.join(self.harness.fixtures_dir, "tokdir", "plain.token")
        os.makedirs(os.path.dirname(out), mode=0o700)
        args = ("--idempotency-key", "issue-once-1", "agent", "token", "issue", "plain", "--out", out, "--force")
        rc, first = self.harness.cli(*args, user=True)
        self.assertEqual(rc, 0, first)
        self.assertNotIn("token", first["data"])
        with open(out, encoding="utf-8") as f:
            real = f.read()
        self.assertTrue(real.strip())
        rc, again = self.harness.cli(*args, user=True)
        self.assertEqual(rc, c.EXIT_CONFLICT, again)
        self.assertEqual(again["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)
        self.assertFalse(again["error"]["details"]["written"])
        self.assertEqual(again["error"]["details"]["credential_id"], first["data"]["credential_id"])
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), real, "replay overwrote the token file")
        # The file still holds a working token.
        rc, who = self.harness.cli("whoami", token=real.strip())
        self.assertEqual(rc, 0, who)

        rc, resp = self.harness.cli("agent", "token", "issue", "plain", "--out", "", user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)
        self.assertNotIn("token", resp.get("data") or {})
        self.assertNotIn("token", resp.get("error", {}).get("details", {}))
        rc, resp = self.harness.cli("agent", "token", "issue", "plain", "--out", "", "--force", user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)

    def test_inbox_text_shows_payload_fields(self) -> None:
        tsk = self.create_task("Text inbox title")
        rc, resp = self.harness.cli("--format", "text", "inbox", "list", "--peek", token=self.tok_ibx)
        self.assertEqual(rc, 0, resp)
        out = resp["raw_stdout"]
        self.assertIn("SUBJECT", out)
        self.assertIn("PROJECT", out)
        self.assertIn(tsk, out)
        self.assertIn("Text inbox title", out)
        self.assertIn("task.offered", out)
        rc, resp = self.harness.cli("--format", "text", "inbox", "wait", "--timeout", "5s", token=self.tok_ibx)
        self.assertEqual(rc, 0, resp)
        self.assertIn(tsk, resp["raw_stdout"])
        self.assertIn("Text inbox title", resp["raw_stdout"])


    def test_inbox_text_project_and_approval_details(self) -> None:
        """PR37 delta N2 + #6: PROJECT column; approval.* rows keep id, kind and reason."""
        rc, resp = self.harness.cli("project", "pause", self.prj, token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED, resp)
        apr = resp["error"]["details"]["approval_id"]
        op_hash = resp["error"]["details"]["op_hash"]
        short = "prj_" + self.prj[-8:]
        # approval.requested reaches the project's origin agent (grok).
        rc, out = self.harness.cli("--format", "text", "inbox", "list", "--peek", token=self.tok_grok)
        self.assertEqual(rc, 0, out)
        req = [ln for ln in out["raw_stdout"].splitlines() if "approval.requested" in ln]
        self.assertTrue(req, out["raw_stdout"])
        self.assertIn(apr, req[0])
        self.assertIn(short, req[0])
        self.assertRegex(req[0], r"\w+/\S+")  # category/op kind
        self.assertIn("PROJECT", out["raw_stdout"].splitlines()[0])
        # JSON output is unchanged by the text view.
        rc, js = self.harness.cli("inbox", "list", "--peek", token=self.tok_grok)
        self.assertEqual(rc, 0, js)
        self.assertNotIn("text", js["data"])
        self.assertTrue(any(m["type"] == "approval.requested" and m["project_id"] == self.prj
                            for m in js["data"]["items"]))
        # The user rejects; approval.decided reaches the requester (lead1) with the decision and note.
        rc, resp = self.harness.cli("approval", "reject", apr, "--op-hash", op_hash, "--note", "not now please",
                                    user=True)
        self.assertEqual(rc, 0, resp)
        rc, out = self.harness.cli("--format", "text", "inbox", "list", "--peek", token=self.tok_lead1)
        self.assertEqual(rc, 0, out)
        dec = [ln for ln in out["raw_stdout"].splitlines() if "approval.decided" in ln]
        self.assertTrue(dec, out["raw_stdout"])
        self.assertIn("rejected: not now please", dec[0])
        self.assertIn(short, dec[0])
        # Release follow-up (c): the decided payload names the approval and its kind.
        self.assertIn(apr, dec[0])
        self.assertRegex(dec[0], r"\w+/\S+")
        rc, js = self.harness.cli("inbox", "list", "--peek", token=self.tok_lead1)
        decided = [m for m in js["data"]["items"] if m["type"] == "approval.decided"]
        self.assertTrue(decided, js)
        self.assertEqual(decided[0]["payload"]["approval_id"], apr)
        self.assertTrue(decided[0]["payload"]["category"])
        self.assertTrue(decided[0]["payload"]["op"])


if __name__ == "__main__":
    unittest.main()
