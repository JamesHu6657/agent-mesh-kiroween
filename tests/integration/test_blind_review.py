"""Blind Linear review of main ffa471e, end to end.

Authorization and task workflows:
P1  `task assign` only assigns a created task; a failed task cannot get a fresh attempt
    through it (that is `task retry`, which checks the attempt cap and approval).
P2  a task created unassigned and assigned later gets the assignee's default profile
    unless a profile was asked for.
P2  a pending `task wait` / `project wait` ends with rc 3 when its token is revoked,
    returning nothing.
P2  `agent update` refuses to narrow allowed efforts below the default effort.
P3  `task retry` with a model the assignee does not allow is a validation error, not
    INTERNAL_ERROR."""

import json
import threading
import time
import unittest

import mesh.constants as c
from tests.integration import test_merge as tm
from tests.integration.test_tasks import create_agent


class _Base(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project
    show = tm.TestMerge.show

    def lead(self, *args, **kw):
        return self.harness.cli(*args, token=self.tok_lead1, **kw)

    def create(self, *extra) -> str:
        rc, resp = self.lead("task", "create", "--project", self.prj, "--title", "T", "--spec", "S", *extra)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def fail_once(self, tsk: str) -> None:
        rc, resp = self.harness.cli("task", "start", tsk, token=self.tok_ibx, extra_env={"MESH_SESSION": "ses_bl"})
        self.assertEqual(rc, 0, resp)
        d = resp["data"]
        rc, resp = self.harness.cli("task", "fail", tsk, "--reason", "boom", "--run", d["run_id"],
                                    "--fence", str(d["fence"]), token=self.tok_ibx,
                                    extra_env={"MESH_SESSION": "ses_bl"})
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.show(tsk)["status"], "failed")


class TestAssignOnlyFromCreated(_Base):
    def test_failed_task_cannot_be_assigned_past_the_cap(self) -> None:
        tsk = self.create("--max-attempts", "1")
        rc, resp = self.lead("task", "assign", tsk, "--to", "ibx")
        self.assertEqual(rc, 0, resp)
        self.fail_once(tsk)
        rc, resp = self.lead("task", "assign", tsk, "--to", "ibx")
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_TRANSITION)
        self.assertIn("task retry", resp["error"]["message"])
        self.assertEqual(self.show(tsk)["status"], "failed")
        # The supported path still guards the cap.
        rc, resp = self.lead("task", "retry", tsk)
        self.assertNotEqual(rc, 0, resp)


class TestDeferredAssignProfile(_Base):
    def test_assignee_default_profile_used_when_none_asked(self) -> None:
        create_agent(self.harness, "ro", default_profile="readonly")
        direct = self.create("--assignee", "ro")
        self.assertEqual(self.show(direct)["profile"], "readonly")
        later = self.create()
        rc, resp = self.lead("task", "assign", later, "--to", "ro")
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.show(later)["profile"], "readonly")
        asked = self.create("--profile", "edit")
        rc, resp = self.lead("task", "assign", asked, "--to", "ro")
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.show(asked)["profile"], "edit")


class TestWaitEndsOnRevoke(_Base):
    def _revoke_during(self, argv) -> tuple:
        rc, resp = self.harness.cli("agent", "token", "issue", "lead1", user=True)
        self.assertEqual(rc, 0, resp)
        tok = resp["data"]["token"]
        cred = resp["data"].get("credential_id") or resp["data"].get("id")
        out = {}

        def wait() -> None:
            out["r"] = self.harness.cli(*argv, token=tok, timeout=40.0)

        th = threading.Thread(target=wait)
        th.start()
        time.sleep(1.5)
        rc, resp = self.harness.cli("agent", "token", "revoke", cred, user=True)
        self.assertEqual(rc, 0, resp)
        return th, out

    def test_task_wait(self) -> None:
        tsk = self.create("--assignee", "ibx")
        th, out = self._revoke_during(["task", "wait", tsk, "--until", "submitted", "--timeout", "20s"])
        rc, resp = self.harness.cli("task", "start", tsk, token=self.tok_ibx, extra_env={"MESH_SESSION": "ses_w"})
        self.assertEqual(rc, 0, resp)
        d = resp["data"]
        rc, resp = self.harness.cli("task", "submit", tsk, "--run", d["run_id"], "--fence", str(d["fence"]),
                                    "--summary", "secret summary", token=self.tok_ibx,
                                    extra_env={"MESH_SESSION": "ses_w"})
        self.assertEqual(rc, 0, resp)
        th.join(45)
        rc, resp = out["r"]
        self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, resp)
        self.assertEqual(resp["error"]["code"], c.TOKEN_REVOKED)
        self.assertNotIn("secret summary", json.dumps(resp))

    def test_project_wait(self) -> None:
        th, out = self._revoke_during(["project", "wait", self.prj, "--until", "terminal", "--timeout", "8s"])
        th.join(45)
        rc, resp = out["r"]
        self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, resp)
        self.assertEqual(resp["error"]["code"], c.TOKEN_REVOKED)


class TestAgentUpdateEffort(_Base):
    def test_narrowing_allowed_efforts_below_default_is_refused(self) -> None:
        create_agent(self.harness, "eff", adapter="fake", allowed_efforts=["low", "high"], default_effort="high")
        rc, resp = self.harness.cli("agent", "update", "eff", "--allowed-effort", "low", user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.EFFORT_NOT_ALLOWED)
        rc, resp = self.harness.cli("agent", "update", "eff", "--allowed-effort", "low",
                                    "--default-effort", "low", user=True)
        self.assertEqual(rc, 0, resp)


class TestRetryOverrideValidation(_Base):
    def test_disallowed_model_is_a_validation_error(self) -> None:
        tok = create_agent(self.harness, "pick", adapter="fake", allowed_models=["m-a"], default_model="m-a")
        tsk = self.create("--assignee", "pick", "--max-attempts", "3")
        env = {"MESH_SESSION": "ses_pk"}
        rc, resp = self.harness.cli("task", "start", tsk, token=tok, extra_env=env)
        self.assertEqual(rc, 0, resp)
        d = resp["data"]
        rc, resp = self.harness.cli("task", "fail", tsk, "--reason", "x", "--run", d["run_id"],
                                    "--fence", str(d["fence"]), token=tok, extra_env=env)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.lead("task", "retry", tsk, "--model", "m-zzz")
        self.assertNotEqual(rc, 0, resp)
        self.assertNotEqual(resp["error"]["code"], c.INTERNAL_ERROR, resp)
        self.assertIn("pick", resp["error"]["message"])


if __name__ == "__main__":
    unittest.main()


TOKEN = "gh" + "p_" + "A1b2" * 9


class TestApprovalStorageRedaction(_Base):
    """Blind review P2: approval rows kept secrets (summary, operation, note)."""

    def test_summary_and_note_redacted_operation_refused(self) -> None:
        rc, resp = self.lead("approval", "request", "--category", "deploy", "--summary", "ship " + TOKEN,
                             "--operation", json.dumps({"token": TOKEN}), "--project", self.prj)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)
        rc, resp = self.lead("approval", "request", "--category", "deploy", "--summary", "ship " + TOKEN,
                             "--operation", '{"what":"deploy"}', "--project", self.prj)
        self.assertIn(rc, (0, c.EXIT_APPROVAL_REQUIRED), resp)
        d = resp.get("data") or resp["error"]["details"]
        apr = d.get("approval_id") or d.get("id")
        rc, resp = self.harness.cli("approval", "approve", apr, "--op-hash", d["op_hash"],
                                    "--note", "ok " + TOKEN, user=True)
        self.assertEqual(rc, 0, resp)
        db = self.harness.db()
        try:
            row = db.execute("SELECT summary, op_json, decision_note FROM approvals WHERE id = ?;", (apr,)).fetchone()
        finally:
            db.close()
        self.assertNotIn(TOKEN, json.dumps(dict(row)))


class TestConnectionLimits(_Base):
    """Blind review P2: idle connections were held forever and unbounded in number."""

    def setUp(self) -> None:
        super().setUp()
        self.harness.stop_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_INITIAL_READ_TIMEOUT_S": "1",
                                               "MESH_TEST_MAX_CONNECTIONS": "4"})

    def _sock(self):
        import os
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(os.path.join(self.harness.home, "run", "mesh.sock"))
        s.settimeout(10)
        return s

    def test_idle_client_closed(self) -> None:
        s = self._sock()
        try:
            self.assertEqual(s.recv(100), b"")  # closed by meshd, not held
        finally:
            s.close()

    def test_over_limit_rejected_then_recovers(self) -> None:
        held = [self._sock() for _ in range(4)]
        time.sleep(0.2)
        extra = self._sock()
        try:
            data = extra.recv(4096)
            self.assertIn(b"CONCURRENCY_LIMIT", data)
        finally:
            extra.close()
            for s in held:
                s.close()
        time.sleep(0.3)
        rc, resp = self.harness.cli("daemon", "status", user=True)
        self.assertEqual(rc, 0, resp)


class TestIdempotentReplayOrdering(_Base):
    """Blind review P2/P3: replay before state validation; dry runs never replay."""

    def test_config_set_replays_after_settings_change(self) -> None:
        rc, resp = self.harness.cli("config", "set", "task.max_attempts_cap", "5",
                                    "--idempotency-key", "kcap", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("config", "set", "task.max_attempts_cap", "10", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("config", "set", "task.default_max_attempts", "8", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("config", "set", "task.max_attempts_cap", "5",
                                    "--idempotency-key", "kcap", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(resp.get("idempotent_replay"), resp)

    def test_dry_run_not_replayed(self) -> None:
        rc, resp = self.harness.cli("config", "set", "task.default_timeout_s", "700",
                                    "--idempotency-key", "kdry", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("config", "set", "task.default_timeout_s", "700",
                                    "--idempotency-key", "kdry", "--dry-run", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(resp.get("dry_run"), resp)
        self.assertFalse(resp.get("idempotent_replay", False), resp)


class TestApprovalSecretsR1(_Base):
    """PR #40 r1: internal approvals and JSON-escaped secrets."""

    def _ops(self) -> str:
        db = self.harness.db()
        try:
            return json.dumps([dict(r) for r in db.execute("SELECT op_json, summary FROM approvals;").fetchall()])
        finally:
            db.close()

    def test_internal_approval_stores_no_secret_and_still_works(self) -> None:
        args = ("task", "create", "--project", self.prj, "--title", "fix " + TOKEN, "--spec", "S",
                "--assignee", "plain", "--profile", "unrestricted")
        rc, resp = self.lead(*args)
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED, resp)
        d = resp["error"]["details"]
        self.assertNotIn(TOKEN, self._ops())
        rc, resp = self.harness.cli("approval", "show", d["approval_id"], user=True)
        self.assertEqual(rc, 0, resp)
        self.assertNotIn(TOKEN, json.dumps(resp))
        rc, resp = self.harness.cli("approval", "approve", d["approval_id"], "--op-hash", d["op_hash"], user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.lead(*args, "--approval", d["approval_id"])
        self.assertEqual(rc, 0, resp)

    def test_escaped_quote_secret_refused(self) -> None:
        secret = 'abc"def12345xyz'
        self.harness.stop_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_API_SECRET": secret})
        rc, resp = self.lead("approval", "request", "--category", "deploy", "--summary", "ship",
                             "--operation", json.dumps({"v": secret}), "--project", self.prj)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertNotIn("def12345", self._ops())


class TestOversizeDrainDeadline(_Base):
    """PR #40 r1: a slow oversized sender cannot hold a connection past the deadline."""

    def test_slow_oversized_sender_closed(self) -> None:
        import os
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(os.path.join(self.harness.home, "run", "mesh.sock"))
        s.settimeout(5)
        try:
            s.sendall(b"x" * (1024 * 1024 + 8192))
            s.settimeout(0.1)
            t0 = time.monotonic()
            closed = False
            while time.monotonic() - t0 < 10:
                try:
                    s.sendall(b"y")
                except OSError:
                    closed = True
                    break
                try:
                    if s.recv(65536) == b"":
                        closed = True
                        break
                except socket.timeout:
                    pass
                time.sleep(0.15)
            self.assertTrue(closed, "meshd kept draining a slow oversized request")
            self.assertLess(time.monotonic() - t0, 8)
        finally:
            s.close()
