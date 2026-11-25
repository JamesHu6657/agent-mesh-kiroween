"""Dogfood friction batch 4, end to end: `task close --superseded-by`, client sessions
on a run (two sessions sharing one token), the lease-expiry task event, and the
agent create warning about a short global ack window."""

import json
import os
import sqlite3
import time
import unittest

import mesh.constants as c
from tests.helpers import serial
from tests.integration import test_merge as tm


class TestSupersededClose(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project
    show = tm.TestMerge.show
    start_task = tm.TestMerge.start_task
    commit_file = tm.TestMerge.commit_file
    submit_accept = tm.TestMerge.submit_accept
    accepted_task = tm.TestMerge.accepted_task
    merge = tm.TestMerge.merge
    q = tm.TestMerge.q

    def close(self, tsk, other, *extra, token=None, user=False):
        kw = {"user": True} if user else {"token": token or self.tok_lead1}
        return self.harness.cli("task", "close", tsk, "--superseded-by", other, *extra, **kw)

    def test_close_accepted_task_superseded_by_merged_task(self) -> None:
        a, ws_a = self.accepted_task(self.prj, "A", "a.txt", "a\n")
        b, _ = self.accepted_task(self.prj, "B", "b.txt", "b\n")
        # B is not merged yet: refused, A unchanged.
        rc, resp = self.close(a, b)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertIn("not merged", resp["error"]["message"])
        self.assertEqual(self.show(a)["status"], "accepted")
        rc, resp = self.merge(b)
        self.assertEqual(rc, 0, resp)
        # Guards: self, unknown task, a non-lead worker.
        rc, resp = self.close(a, a)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        rc, resp = self.close(a, "tsk_nope")
        self.assertEqual(rc, c.EXIT_NOT_FOUND, resp)
        rc, resp = self.close(a, b, token=self.tok_ibx)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        # Merged B cannot itself be closed.
        rc, resp = self.close(b, a)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)

        rc, resp = self.close(a, b, "--reason", "a.txt landed with B's merge")
        self.assertEqual(rc, 0, resp)
        d = resp["data"]
        self.assertEqual((d["status"], d["superseded_by"]), ("cancelled", b))
        self.assertEqual(d["history"][-1]["type"], "task.closed")
        self.assertEqual(d["history"][-1]["superseded_by"], b)
        self.assertEqual(d["history"][-1]["from_state"], "accepted")
        # The branch and the worktree are kept.
        self.assertTrue(os.path.isdir(ws_a))
        self.assertEqual(self.fixture.git(self.repo, "rev-parse", "--verify", d["branch"], check=False).returncode, 0)
        ev = self.q("SELECT payload_json FROM events WHERE task_id = ? AND type = 'task.closed';", (a,))
        self.assertEqual(json.loads(ev[0]["payload_json"])["note"], "a.txt landed with B's merge")
        # Closing twice is a conflict (terminal).
        rc, resp = self.close(a, b)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        # The user may close too; text mode shows the link.
        c_tsk, _ = self.accepted_task(self.prj, "C", "c.txt", "c\n")
        rc, resp = self.close(c_tsk, b, user=True)
        self.assertEqual(rc, 0, resp)
        rc, out = self.harness.cli("--format", "text", "task", "show", c_tsk, token=self.tok_lead1)
        self.assertIn(b, out["raw_stdout"])

    def test_close_refuses_open_dependents(self) -> None:
        a, _ = self.accepted_task(self.prj, "A", "a.txt", "a\n")
        b, _ = self.accepted_task(self.prj, "B", "b.txt", "b\n")
        self.assertEqual(self.merge(b)[0], 0)
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", "D", "--spec", "S",
                                    "--assignee", "ibx", "--depends-on", a, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        dep = resp["data"]["id"]
        rc, resp = self.close(a, b)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["details"]["dependents"], [dep])
        self.assertEqual(self.show(a)["status"], "accepted")


class TestSessionsAndLeaseEvents(unittest.TestCase):
    """Inbox runs: client sessions (MESH_SESSION) and the lease-expiry task event."""

    def setUp(self) -> None:
        import shutil
        import tempfile

        from tests.helpers import MeshHarness
        from tests.integration.test_execution_inbox import create_agent
        from tests.stubs import create_test_stubs

        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tok_w1 = create_agent(self.harness, "w1")
        rc, resp = self.harness.cli("intake", "submit", "--title", "PRJ", "--goal", "G", "--criterion", "C1",
                                    "--lead", "lead1", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]

    def started(self, session=None):
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", "T", "--spec", "S",
                                    "--assignee", "w1", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        tsk = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(lambda: self.status(tsk) == "queued", timeout=10.0))
        env = {"MESH_SESSION": session} if session else None
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_w1, extra_env=env)
        self.assertEqual(rc, 0, st)
        return tsk, st["data"]["run_id"], str(st["data"]["fence"])

    def status(self, tsk):
        rc, resp = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        return resp["data"]["status"]

    def w1(self, *args, session=None):
        return self.harness.cli(*args, token=self.tok_w1, extra_env={"MESH_SESSION": session} if session else None)

    def test_second_session_is_refused_and_late_submit_explains(self) -> None:
        tsk, run, fence = self.started(session="laptop-1")
        rc, resp = self.w1("task", "heartbeat", tsk, "--run", run, "--fence", fence, session="vps-2")
        self.assertEqual(rc, c.EXIT_STALE, resp)
        err = resp["error"]
        self.assertEqual((err["code"], err["details"]["reason"]), (c.FENCE_MISMATCH, "session_mismatch"))
        self.assertEqual(err["details"]["run_session"], "laptop-1")
        self.assertIn("another worker session is using the same token", err["message"])
        rc, resp = self.w1("task", "submit", tsk, "--run", run, "--fence", fence, "--summary", "x", session="vps-2")
        self.assertEqual(rc, c.EXIT_STALE, resp)
        self.assertEqual(self.status(tsk), "running")
        # The starting session still works.
        rc, resp = self.w1("task", "heartbeat", tsk, "--run", run, "--fence", fence, session="laptop-1")
        self.assertEqual(rc, 0, resp)
        rc, resp = self.w1("task", "progress", tsk, "--run", run, "--fence", fence, "--message", "m",
                           session="laptop-1")
        self.assertEqual(rc, 0, resp)
        rc, resp = self.w1("task", "submit", tsk, "--run", run, "--fence", fence, "--summary", "done",
                           session="laptop-1")
        self.assertEqual(rc, 0, resp)
        # A late submit / heartbeat of the same run says who submitted it and when.
        for args in (("task", "submit", tsk, "--run", run, "--fence", fence, "--summary", "again"),
                     ("task", "heartbeat", tsk, "--run", run, "--fence", fence)):
            rc, resp = self.w1(*args, session="vps-2")
            self.assertEqual(rc, c.EXIT_STALE, resp)
            err = resp["error"]
            self.assertEqual(err["code"], c.LEASE_EXPIRED)
            self.assertIn("already submitted at", err["message"])
            self.assertIn("another session", err["message"])
            self.assertEqual(err["details"]["submitted_by"], "w1")
            self.assertEqual(err["details"]["submitted_session"], "laptop-1")
            self.assertTrue(err["details"]["submitted_request_id"])

    def test_session_is_on_by_default(self) -> None:
        """No MESH_SESSION: `task start` makes one, returns it with an export line and a
        keepalive command that carries it; a later write naming no session is refused."""
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", "T", "--spec", "S",
                                    "--assignee", "w1", token=self.tok_lead1)
        tsk = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(lambda: self.status(tsk) == "queued", timeout=10.0))
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_w1, extra_env={"MESH_SESSION": ""})
        self.assertEqual(rc, 0, st)
        d = st["data"]
        ses = d["session"]
        self.assertRegex(ses, r"^ses_[0-9a-f]{16}$")
        self.assertEqual(d["export"], f"export MESH_SESSION={ses}")
        self.assertTrue(d["keepalive"].startswith(f"MESH_SESSION={ses} mesh task keepalive {tsk} "), d)
        run, fence = d["run_id"], str(d["fence"])
        rc, resp = self.harness.cli("task", "heartbeat", tsk, "--run", run, "--fence", fence, token=self.tok_w1,
                                    extra_env={"MESH_SESSION": ""})
        self.assertEqual(rc, c.EXIT_STALE, resp)
        self.assertEqual(resp["error"]["details"]["reason"], "session_missing")
        self.assertIn(f"export MESH_SESSION={ses}", resp["error"]["message"])
        rc, resp = self.w1("task", "heartbeat", tsk, "--run", run, "--fence", fence, session="ses_other")
        self.assertEqual(resp["error"]["details"]["reason"], "session_mismatch", resp)
        rc, resp = self.w1("task", "submit", tsk, "--run", run, "--fence", fence, "--summary", "ok", session=ses)
        self.assertEqual(rc, 0, resp)
        # text mode prints the export line
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", "T2", "--spec", "S",
                                    "--assignee", "w1", token=self.tok_lead1)
        t2 = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(lambda: self.status(t2) == "queued", timeout=10.0))
        rc, out = self.harness.cli("--format", "text", "task", "start", t2, token=self.tok_w1,
                                   extra_env={"MESH_SESSION": ""})
        self.assertEqual(rc, 0, out)
        self.assertRegex(out["raw_stdout"], r"export MESH_SESSION=ses_[0-9a-f]{16}")

    @serial
    def test_lease_expiry_records_task_event(self) -> None:
        rc, resp = self.harness.cli("config", "set", "lease.ttl_s", "2", user=True)
        self.assertEqual(rc, 0, resp)
        tsk, run, fence = self.started()
        self.assertTrue(self.harness.wait_for(lambda: self.status(tsk) == "failed", timeout=15.0))
        rc, sh = self.harness.cli("task", "show", tsk, token=self.tok_w1)
        hist = sh["data"]["history"]
        le = [h for h in hist if h["type"] == "task.lease_expired"]
        self.assertEqual(len(le), 1, hist)
        self.assertEqual((le[0]["run_id"], le[0]["fence"], le[0]["holder"], le[0]["task_status"]),
                         (run, int(fence), "w1", "failed"))
        self.assertEqual([h["reason"] for h in hist if h["type"] == "task.failed"], ["lease_expired"])
        # The lead hears it once (task.failed), not twice.
        rc, inbox = self.harness.cli("inbox", "list", "--peek", token=self.tok_lead1)
        types = [m["type"] for m in inbox["data"]["items"] if m["payload"].get("task_id") == tsk]
        self.assertEqual(types.count("task.failed"), 1, types)
        self.assertNotIn("task.lease_expired", types)

    @serial
    def test_lease_expiry_on_non_running_task_still_recorded(self) -> None:
        tsk, run, fence = self.started()
        # A lease left on a task that is no longer running (simulated): the sweep used to
        # drop it without any event.
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        try:
            conn.execute("UPDATE tasks SET status = 'submitted' WHERE id = ?;", (tsk,))
            conn.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00.000Z' WHERE key = ?;",
                         (f"task:{tsk}",))
            conn.commit()
        finally:
            conn.close()
        def lead_msgs():
            rc, inbox = self.harness.cli("inbox", "list", "--peek", token=self.tok_lead1)
            return [m for m in inbox["data"]["items"] if m["type"] == "task.lease_expired"]
        self.assertTrue(self.harness.wait_for(lambda: bool(lead_msgs()), timeout=10.0))
        p = lead_msgs()[0]["payload"]
        self.assertEqual((p["task_id"], p["run_id"], p["fence"], p["task_failed"], p["task_status"]),
                         (tsk, run, int(fence), False, "submitted"))
        rc, sh = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(sh["data"]["status"], "submitted")
        self.assertIn("task.lease_expired", [h["type"] for h in sh["data"]["history"]])
        # restore a consistent state for the teardown invariants check
        rc, resp = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)

    def test_agent_create_warns_about_short_global_ack_window(self) -> None:
        rc, resp = self.harness.cli("agent", "create", "--name", "slowbox", "--adapter", "none",
                                    "--delivery", "inbox", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(any("inbox.ack_timeout_s = 600s" in w and "--ack-timeout" in w for w in resp["warnings"]),
                        resp)
        rc, resp = self.harness.cli("agent", "create", "--name", "slowbox2", "--adapter", "none",
                                    "--delivery", "inbox", "--ack-timeout", "2h", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertFalse(any("ack window" in w for w in resp["warnings"]), resp)
        rc, resp = self.harness.cli("config", "set", "inbox.ack_timeout_s", "7200", user=True)
        rc, resp = self.harness.cli("agent", "create", "--name", "slowbox3", "--adapter", "none",
                                    "--delivery", "inbox", user=True)
        self.assertFalse(any("ack window" in w for w in resp["warnings"]), resp)


if __name__ == "__main__":
    unittest.main()
