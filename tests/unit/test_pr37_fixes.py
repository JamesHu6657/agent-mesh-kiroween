"""PR #37 review fixes, unit level: keyed start session (D2), `--out` checks (#2, #3),
the inbox text table (#6) and the quickstart's repo intake command (#7)."""

import argparse
import os
import re
import shlex
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.cli.commands import agent as agent_cmd
from mesh.cli.commands import inbox as inbox_cmd
from mesh.cli.commands import task as task_cmd
from mesh.errors import MeshError


class FakeClient:
    calls = []
    replay = False
    data = {}

    def __init__(self, home=None):
        pass

    def execute_cmd(self, args, cmd, payload):
        FakeClient.calls.append((cmd, dict(payload)))
        if FakeClient.replay:
            setattr(args, "_idempotent_replay", True)
        return {"data": dict(FakeClient.data)}


class TestKeyedStartSession(unittest.TestCase):
    def setUp(self):
        FakeClient.calls, FakeClient.replay, FakeClient.data = [], False, {}
        p = mock.patch.object(task_cmd, "MeshClient", FakeClient)
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"MESH_SESSION": ""})
        e.start()
        self.addCleanup(e.stop)

    def start(self, key):
        task_cmd.handle_task_start(argparse.Namespace(home=None, task_id="tsk_x", idempotency_key=key))
        return FakeClient.calls[-1][1]["session"]

    def test_same_key_same_session(self):
        a, b = self.start("k1"), self.start("k1")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^ses_[0-9a-f]{16}$")
        self.assertNotEqual(a, self.start("k2"))

    def test_no_key_random(self):
        self.assertNotEqual(self.start(None), self.start(None))

    def test_env_session_wins(self):
        with mock.patch.dict(os.environ, {"MESH_SESSION": "ses_mine"}):
            self.assertEqual(self.start("k1"), "ses_mine")


class TestTokenOut(unittest.TestCase):
    def setUp(self):
        FakeClient.calls, FakeClient.replay = [], False
        FakeClient.data = {"token": "PLACEHOLDER", "credential_id": "crd_1"}
        p = mock.patch.object(agent_cmd, "MeshClient", FakeClient)
        p.start()
        self.addCleanup(p.stop)
        self.dir = tempfile.mkdtemp(prefix="mh-pr37-")
        os.chmod(self.dir, 0o700)
        self.home = tempfile.mkdtemp(prefix="mh-pr37-home-")

    def args(self, out, force=False):
        return argparse.Namespace(home=self.home, name="w", expires_in=None, out=out, force=force, dry_run=False)

    def test_empty_out_refused_before_issue(self):
        for out in ("", "  "):
            with self.assertRaises(MeshError) as cm:
                agent_cmd.handle_agent_token_issue(self.args(out))
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
        self.assertEqual(FakeClient.calls, [], "a token was issued for an empty --out")
        with self.assertRaises(MeshError):
            agent_cmd.handle_agent_token_issue(self.args("", force=True))
        self.assertEqual(FakeClient.calls, [])

    def test_replay_never_writes(self):
        path = os.path.join(self.dir, "w.token")
        with open(path, "w") as f:
            f.write("REAL\n")
        os.chmod(path, 0o600)
        FakeClient.replay = True
        with self.assertRaises(MeshError) as cm:
            agent_cmd.handle_agent_token_issue(self.args(path, force=True))
        self.assertEqual(cm.exception.code, c.IDEMPOTENCY_KEY_REUSED)
        self.assertEqual(cm.exception.exit_code, c.EXIT_CONFLICT)
        self.assertFalse(cm.exception.details["written"])
        self.assertNotIn("PLACEHOLDER", cm.exception.message)
        with open(path) as f:
            self.assertEqual(f.read(), "REAL\n")
        missing = os.path.join(self.dir, "new.token")
        with self.assertRaises(MeshError):
            agent_cmd.handle_agent_token_issue(self.args(missing))
        self.assertFalse(os.path.exists(missing))

    def test_first_issue_writes(self):
        path = os.path.join(self.dir, "w.token")
        data = agent_cmd.handle_agent_token_issue(self.args(path))
        self.assertNotIn("token", data)
        with open(path) as f:
            self.assertEqual(f.read(), "PLACEHOLDER\n")


class TestInboxText(unittest.TestCase):
    ITEMS = [
        {"id": "msg_1", "seq": 3, "type": "task.offered", "status": "pending",
         "payload": {"task_id": "tsk_a", "title": "Do   the\nthing", "spec": "x" * 500}},
        {"id": "msg_2", "seq": 4, "type": "task.failed", "status": "pending",
         "payload": {"task_id": "tsk_b", "title": "B", "failure": {"reason": "no_response"}}},
        {"id": "msg_3", "seq": 5, "type": "task.offered", "status": "pending",
         "payload": {"task_id": "tsk_c", "title": "C", "last_rejection": {"reason": "missing tests"}}},
    ]

    def test_table(self):
        out = inbox_cmd.inbox_table(self.ITEMS)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["SEQ", "ID", "TYPE", "STATUS", "PROJECT", "SUBJECT", "TITLE", "REASON"])
        self.assertIn("tsk_a", lines[1])
        self.assertIn("Do the thing", lines[1])
        self.assertNotIn("xxxx", out)
        self.assertIn("no_response", lines[2])
        self.assertIn("rejected: missing tests", lines[3])
        self.assertEqual(inbox_cmd.inbox_table([]), "(no messages)")

    def test_text_view_only_in_text_mode(self):
        data = {"items": self.ITEMS}
        self.assertIs(inbox_cmd._text_view(argparse.Namespace(format="json"), data), data)
        view = inbox_cmd._text_view(argparse.Namespace(format="text"), data)
        self.assertIn("tsk_b", view["text"])


class TestInboxTextProjectAndApprovals(unittest.TestCase):
    """PR37 delta N2 + #6: project column and approval.* details in text mode."""

    def rows(self, items):
        return inbox_cmd.inbox_table(items).splitlines()[1:]

    def test_project_short_id(self):
        pid = "prj_a1d283a9d4ymvospyy2u3uw7ph"
        (row,) = self.rows([{"id": "msg_1", "seq": 1, "type": "task.offered", "status": "pending",
                             "project_id": pid, "payload": {"task_id": "tsk_a", "title": "T"}}])
        self.assertIn("prj_" + pid[-8:], row)
        self.assertNotIn(pid, row)
        (row,) = self.rows([{"id": "msg_2", "type": "x", "payload": {}}])
        self.assertEqual(row.split()[4], "-")
        self.assertEqual(inbox_cmd.short_project("prj_short"), "prj_short")

    def test_approval_rows(self):
        items = [
            {"id": "msg_r", "seq": 1, "type": "approval.requested", "status": "pending", "project_id": "prj_p",
             "payload": {"approval_id": "apr_1", "category": "deploy", "op": "project.deliver",
                         "summary": "Deliver   prj_p\nto main", "expires_at": "2026-11-14T00:00:00Z"}},
            {"id": "msg_d", "seq": 2, "type": "approval.decided", "status": "pending",
             "payload": {"approval_id": "apr_1", "decision": "rejected", "note": "not yet"}},
            {"id": "msg_e", "seq": 3, "type": "approval.expired", "status": "pending",
             "payload": {"approval_id": "apr_2", "category": "money", "op": {"type": "pay"},
                         "expires_at": "2026-11-13T00:00:00Z", "status": "expired"}},
            {"id": "msg_c", "seq": 4, "type": "approval.cancelled", "status": "pending",
             "payload": {"approval_id": "apr_3", "reason": "cancelled_by_requester"}},
        ]
        req, dec, exp, can = self.rows(items)
        self.assertIn("apr_1", req)
        self.assertIn("deploy/project.deliver: Deliver prj_p to main", req)
        self.assertIn("expires 2026-11-14T00:00:00Z", req)
        self.assertIn("apr_1", dec)
        self.assertIn("rejected: not yet", dec)
        self.assertIn("apr_2", exp)
        self.assertIn("money/pay", exp)
        self.assertIn("expired at 2026-11-13T00:00:00Z", exp)
        self.assertIn("apr_3", can)
        self.assertIn("cancelled_by_requester", can)

    def test_json_unchanged(self):
        data = {"items": [{"id": "msg_1", "type": "approval.decided", "payload": {"decision": "approved"}}]}
        self.assertIs(inbox_cmd._text_view(argparse.Namespace(format="json"), data), data)


class TestQuickstartRepoIntake(unittest.TestCase):
    def test_quickstart_intake_parses_with_base_branch_and_test_command(self):
        from mesh.cli.help import HELP_TOPICS
        from mesh.cli.main import build_parser

        text = HELP_TOPICS["quickstart"].replace("\\\n", " ")
        cmds = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("mesh intake submit")]
        self.assertTrue(cmds)
        for cmd in cmds:
            argv = shlex.split(re.sub(r"\s+", " ", cmd))[1:]
            ns = build_parser().parse_args(argv)
            if ns.repo:
                self.assertEqual(ns.base_branch, "main")
                self.assertTrue(ns.test_command)
                import json
                self.assertIsInstance(json.loads(ns.test_command), list)


if __name__ == "__main__":
    unittest.main()
