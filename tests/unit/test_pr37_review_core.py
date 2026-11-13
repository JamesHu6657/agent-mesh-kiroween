"""PR #37 Linear review fixes, daemon/core half (tsk_a1d37dfe9dpmysnrwjddwxrssp).

D1 scheduler fails closed when T6 raises; #4 session fencing uses a digest, not the
redacted event value; D3 the recovery hint shell-quotes the session; #1 the foreign
bystander exception is sshd-only; #8/#9 MERGE_HELD text and log path; #5 the roster
for run tokens is current-lead only.
"""

import sqlite3
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import authz, leases
from mesh.daemon import merge, scheduler, terminate
from mesh.errors import MeshError


class TestSchedulerFailsClosedOnT6Error(unittest.TestCase):
    def _tick(self, t6):
        launch = mock.Mock(return_value=["started"])
        with mock.patch.object(scheduler, "check_queued_tasks_t6", t6), \
                mock.patch.object(scheduler, "start_queued_launch_tasks", launch), \
                mock.patch.object(scheduler, "sweep_expired_approvals", return_value=[]), \
                mock.patch.object(scheduler, "sweep_expired_inbox_leases", return_value=[]), \
                mock.patch.object(scheduler, "watch_launch_runs_tick", return_value=[]), \
                mock.patch.object(scheduler, "promote_tasks", return_value=[]), \
                mock.patch.object(scheduler, "start_lead_turns_tick", return_value=[]), \
                mock.patch.object(scheduler, "_sweep_error"):
            out = scheduler.run_scheduler_tick(mock.Mock(), "")
        return launch, out

    def test_t6_error_skips_launch(self):
        launch, out = self._tick(mock.Mock(side_effect=sqlite3.OperationalError("locked")))
        launch.assert_not_called()
        self.assertNotIn("started", out)

    def test_second_t6_error_skips_launch(self):
        launch, _ = self._tick(mock.Mock(side_effect=[[], sqlite3.OperationalError("locked")]))
        launch.assert_not_called()

    def test_t6_ok_launches(self):
        launch, out = self._tick(mock.Mock(return_value=[]))
        launch.assert_called_once()
        self.assertIn("started", out)


class TestSessionDigest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
        self.started = {"ts": "t0", "actor": "w", "request_id": "r", "session": "worker-[REDACTED]"}

    def _check(self, session):
        with mock.patch.object(leases, "_run_event", return_value=dict(self.started)):
            leases.check_run_session(self.conn, "tsk_a", "run_a", session)

    def test_secret_like_session_still_matches(self):
        leases.remember_run_session(self.conn, "run_a", "worker-secret=alpha")
        self._check("worker-secret=alpha")  # no raise

    def test_redacted_literal_no_longer_matches(self):
        leases.remember_run_session(self.conn, "run_a", "worker-secret=alpha")
        with self.assertRaises(MeshError) as cm:
            self._check("worker-[REDACTED]")
        self.assertEqual(cm.exception.details["reason"], "session_mismatch")

    def test_other_and_missing_session(self):
        leases.remember_run_session(self.conn, "run_a", "ses_1")
        for s, reason in (("ses_2", "session_mismatch"), (None, "session_missing")):
            with self.assertRaises(MeshError) as cm:
                self._check(s)
            self.assertEqual(cm.exception.code, c.FENCE_MISMATCH)
            self.assertEqual(cm.exception.details["reason"], reason)

    def test_digest_stored_not_raw(self):
        leases.remember_run_session(self.conn, "run_a", "worker-secret=alpha")
        (v,) = self.conn.execute("SELECT value FROM meta;").fetchone()
        self.assertNotIn("alpha", v)

    def test_legacy_run_without_digest_compares_event(self):
        self.started["session"] = "ses_old"
        self._check("ses_old")

    def test_missing_hint_is_shell_quoted(self):
        self.started["session"] = "laptop; printf INJECTED"
        with self.assertRaises(MeshError) as cm:
            self._check(None)
        self.assertIn("export MESH_SESSION='laptop; printf INJECTED'", cm.exception.message)


class TestRedactedSessionHint(unittest.TestCase):
    """PR #37 review N1: never tell the worker to export the redacted placeholder."""

    def test_redacted_hint_points_to_task_start(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
        leases.remember_run_session(conn, "run_a", "worker-secret=alpha")
        started = {"ts": "t0", "actor": "w", "request_id": "r", "session": "worker-[REDACTED]"}
        with mock.patch.object(leases, "_run_event", return_value=started):
            with self.assertRaises(MeshError) as cm:
                leases.check_run_session(conn, "tsk_a", "run_a", None)
        self.assertNotIn("export MESH_SESSION=", cm.exception.message)
        self.assertIn("task start", cm.exception.message)
        self.assertEqual(cm.exception.details["reason"], "session_missing")
        self.assertEqual(cm.exception.code, c.FENCE_MISMATCH)


class TestBystanderSshdOnly(unittest.TestCase):
    # listener (root, old) -> parent (root, young) -> candidate (us, young)
    def _check(self, comms, pid=901):
        procs = {500: (1, 10), 900: (500, 2000), 901: (900, 2001)}

        def stat(p):
            v = procs.get(int(p))
            return None if v is None else {"ppid": v[0], "starttime": v[1], "state": "S"}

        real_stat = __import__("os").stat

        def ostat(path, *a, **k):
            if isinstance(path, str) and path.startswith("/proc/"):
                return mock.Mock(st_uid=0 if int(path.split("/")[2]) != 901 else __import__("os").geteuid())
            return real_stat(path, *a, **k)

        with mock.patch.object(terminate, "get_proc_stat", stat), mock.patch("os.stat", ostat), \
                mock.patch.object(terminate, "_comm", lambda p: comms.get(p, "")):
            return terminate._foreign_spawned_bystander(stat(pid), 1000, pid)

    def test_sshd_session_is_bystander(self):
        self.assertTrue(self._check({900: "sshd-session", 901: "sshd-session"}))

    def test_foreign_subreaper_adoption_is_not(self):
        # review #1: a root subreaper adopted an escaped (non-dumpable) run process
        self.assertFalse(self._check({900: "containerd-shim", 901: "python3"}))

    def test_sshd_parent_but_non_sshd_candidate_is_not(self):
        self.assertFalse(self._check({900: "sshd-session", 901: "python3"}))

    def test_unreadable_comm_is_not(self):
        self.assertFalse(self._check({}))


class TestMergeHeldText(unittest.TestCase):
    plan = {"task_id": "tsk_x", "merge_id": "mrg_x", "run_id": "run_x"}

    def _err(self, result):
        return merge.LiveMergeHeld(self.plan, result, OSError(5, "scan uncertain")).to_mesh_error()

    def test_held_after_commit_says_so(self):
        # production pipeline shape: log path at top level, nested test without it
        result = {"outcome": "merged", "noop": False, "merge_commit": "abcdef1234567890", "tests": "passed",
                  "test_log": "/h/merges/mrg_x/test.log", "test": {"exit_code": 0, "passed": True}}
        err = self._err(result)
        self.assertNotIn("Nothing was merged", err.message)
        self.assertIn("abcdef123456", err.message)
        self.assertEqual(err.details["merge_commit"], "abcdef1234567890")
        self.assertEqual(err.details["log_path"], "/h/merges/mrg_x/test.log")

    def test_failed_pipeline_says_nothing_merged(self):
        result = {"outcome": "failed", "reason": "tests_failed", "tests": "failed",
                  "test_log": "/h/merges/mrg_x/test.log", "test": {"exit_code": 1, "passed": False}}
        err = self._err(result)
        self.assertIn("Nothing was merged", err.message)
        self.assertIsNone(err.details["merge_commit"])
        self.assertEqual(err.details["log_path"], "/h/merges/mrg_x/test.log")


class TestRosterRunTokens(unittest.TestCase):
    def _authz(self, principal, current):
        with mock.patch.object(authz, "get_project_active_lead", return_value=current):
            authz.check_authz(mock.Mock(), "agent.list", principal, "agent", "run_token")

    def test_superseded_lead_denied(self):
        p = {"id": "agt_l", "name": "lead", "scope": {"role": "lead", "project_id": "prj", "lead_assignment_id": "la_old"}}
        with self.assertRaises(MeshError) as cm:
            self._authz(p, {"assignment_id": "la_new"})
        self.assertEqual(cm.exception.code, c.LEAD_SUPERSEDED)

    def test_current_lead_allowed(self):
        p = {"id": "agt_l", "name": "lead", "scope": {"role": "lead", "project_id": "prj", "lead_assignment_id": "la_1"}}
        self._authz(p, {"assignment_id": "la_1"})

    def test_executor_run_token_denied(self):
        p = {"id": "agt_e", "name": "exec", "scope": {"role": "executor", "project_id": "prj"}}
        with self.assertRaises(MeshError) as cm:
            self._authz(p, None)
        self.assertEqual(cm.exception.code, c.CAPABILITY_MISSING)

    def test_agent_token_still_handler_decided(self):
        with mock.patch.object(authz, "get_project_active_lead") as g:
            authz.check_authz(mock.Mock(), "agent.list", {"id": "agt_l", "name": "l"}, "agent", "token")
        g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
