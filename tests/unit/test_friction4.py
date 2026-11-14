"""Dogfood friction batch 4 (no daemon): expired approvals are never reused for a new
request, the client session payload, and the `task close` CLI."""

import os
import tempfile
import unittest
from unittest import mock

from mesh.cli.commands import task as task_cmd
from mesh.cli.main import build_parser
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.db import init_db


class TestExpiredApprovalNotReused(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-apr4-")
        self.addCleanup(self.tmp.cleanup)
        self.conn = init_db(os.path.join(self.tmp.name, "mesh.db"))
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'lead1', 'now');")

    def request(self):
        return create_or_get_pending_approval(
            conn=self.conn, category="project_control", op="project.pause", requested_by="agt_1",
            params={"project_id": "prj_1"}, summary="Pause prj_1", project_id="prj_1")

    def status(self, apr_id):
        return self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_id,)).fetchone()["status"]

    def test_open_approval_is_reused_while_valid(self) -> None:
        a = self.request()
        b = self.request()
        self.assertEqual(a["approval_id"], b["approval_id"])

    def test_expired_pending_or_approved_gets_a_fresh_approval(self) -> None:
        for status in ("pending", "approved"):
            with self.subTest(status=status):
                old = self.request()
                self.conn.execute("UPDATE approvals SET status = ?, expires_at = '2000-01-01T00:00:00.000Z' "
                                  "WHERE id = ?;", (status, old["approval_id"]))
                new = self.request()
                self.assertNotEqual(new["approval_id"], old["approval_id"])
                self.assertEqual(new["status"], "pending")
                self.assertGreater(new["expires_at"], "2001")
                self.assertEqual(self.status(old["approval_id"]), "expired")
                ev = self.conn.execute(
                    "SELECT payload_json FROM events WHERE type = 'approval.expired' AND approval_id = ?;",
                    (old["approval_id"],)).fetchall()
                self.assertEqual(len(ev), 1)
                self.assertIn('"expired_on":"new_request"', ev[0]["payload_json"].replace(" ", ""))
                # and the fresh one is reused from now on
                self.assertEqual(self.request()["approval_id"], new["approval_id"])
                self.conn.execute("UPDATE approvals SET status = 'cancelled' WHERE id = ?;", (new["approval_id"],))


class TestSessionPayload(unittest.TestCase):
    def test_with_session(self) -> None:
        with mock.patch.dict(os.environ, {"MESH_SESSION": " box-1 "}):
            self.assertEqual(task_cmd._with_session({"a": 1}), {"a": 1, "session": "box-1"})
        with mock.patch.dict(os.environ, {"MESH_SESSION": "x" * 500}):
            self.assertEqual(len(task_cmd._with_session({})["session"]), 200)
        env = {k: v for k, v in os.environ.items() if k != "MESH_SESSION"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(task_cmd._with_session({"a": 1}), {"a": 1})


class TestTaskCloseCli(unittest.TestCase):
    def test_parser(self) -> None:
        a = build_parser().parse_args(["task", "close", "tsk_1", "--superseded-by", "tsk_2", "--reason", "r"])
        self.assertEqual((a.task_id, a.superseded_by, a.reason), ("tsk_1", "tsk_2", "r"))
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["task", "close", "tsk_1"])

    def test_payload(self) -> None:
        sent = {}
        client = mock.Mock()
        client.execute_cmd.side_effect = lambda args, cmd, payload=None, **_: sent.update(cmd=cmd, **payload) or {"data": {}}
        a = build_parser().parse_args(["task", "close", "tsk_1", "--superseded-by", "tsk_2"])
        a.home = None
        with mock.patch.object(task_cmd, "MeshClient", return_value=client):
            task_cmd.handle_task_close(a)
        self.assertEqual(sent, {"cmd": "task.close", "task_id": "tsk_1", "superseded_by": "tsk_2", "reason": None})


if __name__ == "__main__":
    unittest.main()
