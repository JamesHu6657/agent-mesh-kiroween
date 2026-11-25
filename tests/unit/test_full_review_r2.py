"""Full-codebase review r2 (fix/full-review-r1 @ 6142135) regressions."""

import sys
import json
import os
import unittest
from unittest import mock

from mesh.core.db import transaction
from tests.unit.test_full_review_r1 import TestP1_3ReverseI9, TestP1_5DeliverIntentRecovery

# Runs a real merge test step (runwrap, /proc scans).
MESH_PROC_LOCK = "shared"


class TestR2P1A_JsonlEscapedCredentials(unittest.TestCase):
    EXACT = [("MY_KEY", 'ab"cdefghij'), ("X_TOKEN", "Zq8wE3rT6yU1")]

    def both(self, text: str, exact=None) -> str:
        from mesh.core.redact import StreamRedactor, redact
        exact = self.EXACT if exact is None else exact
        batch = redact(text, exact, jsonl=True)
        for size in (1, 5, len(text) or 1):
            sr = StreamRedactor(exact, jsonl=True)
            got = "".join(sr.feed(text[i:i + size]) for i in range(0, len(text), size)) + sr.flush()
            self.assertEqual(got, batch, size)
        return batch

    def test_escaped_credentials_are_redacted(self) -> None:
        lines = [
            {"type": "function_call", "arguments": json.dumps({"api_key": "AKIA_SUPER_SECRET_123"})},
            {"type": "tool_result", "out": "Authorization: Bearer\teyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"},
            {"out": "password\n: hunter22xyz"},
            {"env": 'ab"cdefghij'},
        ]
        raw = "".join(json.dumps(x) + "\n" for x in lines)
        raw += '{"url": "https:\\/\\/bob:pw12345678@github.com\\/r.git"}\n'
        raw += '{"api\\u005fkey": "secret123zz"}\n'
        out = self.both(raw)
        for s in ("AKIA_SUPER_SECRET_123", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "hunter22xyz",
                  "cdefghij", "pw12345678", "secret123zz"):
            self.assertNotIn(s, out)
        for ln in out.splitlines():
            json.loads(ln)  # still valid JSONL

    def test_value_spanning_json_syntax_falls_back(self) -> None:
        exact = [("X_SECRET", 'ab","c":"de')]
        out = self.both('{"a":"ab","c":"de"}\n', exact)
        # r5 P1-1: a value across JSON syntax gives the whole line (fail closed).
        self.assertEqual(out, '"[REDACTED]"\n')

    def test_credential_key_containers_and_forged_markers(self) -> None:
        out = self.both('{"api_key": ["SECRET_IN_ARRAY", {"x": "NESTED_SECRET"}]}\n'
                        '{"api_key": "[REDACTED:real_secret]"}\n'
                        '{"password": "[REDACTED:X_TOKEN]", "secret": "[REDACTED]"}\n')
        for s in ("SECRET_IN_ARRAY", "NESTED_SECRET", "real_secret"):
            self.assertNotIn(s, out)
        self.assertIn('"[REDACTED:X_TOKEN]"', out)  # a real marker stays
        for ln in out.splitlines():
            json.loads(ln)

    def test_clean_lines_unchanged(self) -> None:
        raw = '{"a":1,  "b":"x"}\n[1, "two"]\n'
        self.assertEqual(self.both(raw), raw)


class TestR2P2A_LogTailKeepsJsonl(unittest.TestCase):
    def test_tail_lines_stay_valid_json(self) -> None:
        import tempfile
        from mesh.core.redact import redact
        from mesh.daemon.handlers.run import _read_log_tail
        stored = redact(json.dumps({"type": "assistant", "text": "set password=hunter22"}) + "\n"
                        + "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----\n",
                        [], jsonl=True)
        self.assertNotIn("hunter22", stored)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "stdout.log")
            with open(p, "w", encoding="utf-8") as f:
                f.write(stored)
                f.write("MIIEvQIBADANBgkq-late\n")  # a raw tail line, not redacted on write
            got = _read_log_tail(p, 50)
        self.assertEqual(json.loads(got["lines"][0])["text"], "set password=[REDACTED]")
        self.assertEqual(got["total_lines"], 5)


class TestR2P1B_ReverseI9(unittest.TestCase):
    setUp = TestP1_3ReverseI9.setUp
    tearDown = TestP1_3ReverseI9.tearDown
    _create_repo_project = TestP1_3ReverseI9._create_repo_project
    make_task = TestP1_3ReverseI9.make_task
    merge_into_integration = TestP1_3ReverseI9.merge_into_integration
    reverse_violations = TestP1_3ReverseI9.reverse_violations
    _record_merged = TestP1_3ReverseI9._record_merged

    def _commit_on(self, branch: str, name: str) -> str:
        wt = self.fixture.path(f"wt-{name}")
        self.fixture.git(self.repo, "worktree", "add", wt, branch)
        with open(os.path.join(wt, f"{name}.txt"), "w", encoding="utf-8") as f:
            f.write(name + "\n")
        self.fixture.git(wt, "add", "-A")
        self.fixture.git(wt, "commit", "-m", name, identity=True)
        sha = self.fixture.head(wt)
        self.fixture.git(self.repo, "worktree", "remove", wt)
        return sha

    def test_synced_integration_without_own_work_is_not_reported(self) -> None:
        self._create_repo_project()
        up = self.make_task("tsk_1", "merged", True)
        down = self.make_task("tsk_2", "cancelled", False)
        m1 = self.merge_into_integration(up)
        self._record_merged("tsk_1", m1)
        self.fixture.git(self.repo, "branch", "-f", down, m1)  # synced (fast-forward), then cancelled
        self.assertEqual(self.reverse_violations(), [])

    def test_partly_fast_forwarded_work_is_reported(self) -> None:
        self._create_repo_project()
        branch = self.make_task("tsk_ff", "cancelled", True)
        c1 = self.fixture.head(self.repo, branch)
        self.fixture.git(self.repo, "branch", "-f", self.integration, c1)
        self._commit_on(branch, "c2")
        self.assertEqual([v.details["task_id"] for v in self.reverse_violations()], ["tsk_ff"])

    def test_work_carried_by_a_downstream_task_without_supersede_is_reported(self) -> None:
        self._create_repo_project()
        unmerged = self.make_task("tsk_u", "cancelled", True)
        down = "mesh/prj_x/tsk_3"
        self.fixture.git(self.repo, "branch", down, unmerged)
        self._commit_on(down, "c3")
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, fence_seq, branch, base_commit, created_by, created_at, updated_at) "
                "VALUES ('tsk_3', 'prj_x', 'T', 'S', 'code', 'merged', 'agt_lead', 'edit', 3600, 3, 1, ?, ?, "
                "'usr_1', '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z');",
                (down, self.fixture.head(self.repo, unmerged)))
        self._record_merged("tsk_3", self.merge_into_integration(down))
        self.assertEqual([v.details["task_id"] for v in self.reverse_violations()], ["tsk_u"])


class TestR2P1C_DeletedWorktreeSlot(unittest.TestCase):
    def test_code_task_slot_is_never_created_empty(self) -> None:
        import tempfile
        from mesh.core.workspaces import WorkspaceError, require_task_worktree
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.join(d, "tasks", "tsk_1")
            with self.assertRaises(WorkspaceError):
                require_task_worktree(ws)
            self.assertFalse(os.path.exists(ws))

    def test_start_effect_does_not_create_a_deleted_worktree(self) -> None:
        import tempfile
        from mesh.daemon.handlers.base import Effect
        from mesh.daemon.server import DaemonServer
        with tempfile.TemporaryDirectory() as d:
            ws, rd = os.path.join(d, "tasks", "tsk_1"), os.path.join(d, "runs", "run_1")
            fake = mock.Mock()
            DaemonServer.execute_effects(fake, [Effect("create_run_and_workspace_dirs",
                                                       {"run_dir": rd, "workspace_dir": ws, "worktree": True})])
            self.assertTrue(os.path.isdir(rd))
            self.assertFalse(os.path.exists(ws))
            DaemonServer.execute_effects(fake, [Effect("create_run_and_workspace_dirs",
                                                       {"run_dir": rd, "workspace_dir": ws})])
            self.assertTrue(os.path.isdir(ws))  # a general task's dir is still created

    def test_delete_drops_a_cached_preparation(self) -> None:
        import time
        from mesh.daemon import supervisor
        key = ("/ws/tsk_c", "mesh/prj/tsk_c", 1)
        done = {"key": key, "done": True, "done_at": time.monotonic(), "result": {"action": "reused"}, "error": None}
        running = {"key": key, "done": False, "result": None, "error": None}
        plan = {"workspace_path": key[0], "branch": key[1], "fence_seq": key[2]}
        with mock.patch.object(supervisor, "plan_code_workspace", return_value=plan), \
                mock.patch.object(supervisor.threading, "Thread") as th:
            supervisor._WS_PREP["tsk_c"] = done
            supervisor.forget_ws_prep("tsk_c")
            self.assertIsNone(supervisor._code_workspace_state(None, "/h", {"id": "tsk_c"}))
            self.assertTrue(th.called)  # prepared again
            supervisor._WS_PREP["tsk_c"] = running
            supervisor.forget_ws_prep("tsk_c")
            running.update(done=True, done_at=time.monotonic())
            self.assertIsNone(supervisor._code_workspace_state(None, "/h", {"id": "tsk_c"}))
        supervisor._WS_PREP.pop("tsk_c", None)


class TestR2P1E_DeliverIntentHorizon(unittest.TestCase):
    setUp = TestP1_5DeliverIntentRecovery.setUp
    tearDown = TestP1_5DeliverIntentRecovery.tearDown
    _create_repo_project = TestP1_5DeliverIntentRecovery._create_repo_project
    _advance_integration = TestP1_5DeliverIntentRecovery._advance_integration
    delivered = TestP1_5DeliverIntentRecovery.delivered

    def test_redelivery_to_a_commit_delivered_before_is_recovered(self) -> None:
        from mesh.core.events import record_event
        from mesh.core.repos import deliver_to_base, write_deliver_intent
        from mesh.daemon import recovery

        self._create_repo_project()
        old, new = self._advance_integration()
        with transaction(self.conn):  # delivered to `new` once, then main was reset to `old`
            record_event(self.conn, type_="project.delivered", actor_id="usr_1", actor_kind="user",
                         via="direct", project_id="prj_x", payload={"old": old, "new": new, "method": "x"})
        horizon = self.conn.execute("SELECT MAX(seq) FROM events;").fetchone()[0]
        home = self.fixture.path("home")
        os.makedirs(home)
        write_deliver_intent(home, {
            "project_id": "prj_x", "repo": self.repo, "base_branch": "main",
            "integration_branch": self.integration, "old": old, "new": new, "actor_id": "usr_1",
            "actor_kind": "user", "via": "direct", "relayed_by": None, "request_id": "req_2",
            "approval_id": None, "min_event_seq": horizon,
            "idempotency": {"principal_id": "usr_1", "key": "k-2", "cmd": "project.deliver",
                            "request_hash": "h" * 64}})
        deliver_to_base(self.repo, "main", self.integration, expected=(old, new))  # then SIGKILL
        counts = {}
        recovery._step_deliver_intents(self.conn, home, counts)
        self.assertEqual(counts.get("deliveries_recovered"), 1, counts)
        self.assertEqual(len(self.delivered()), 2)
        row = self.conn.execute("SELECT cmd, response_json FROM idempotency WHERE key = 'k-2';").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row[1])["data"]["new"], new)


class TestR2P1F_MergeTestRunSession(unittest.TestCase):
    mesh_serial = True  # real runwrap and process walks

    def test_test_command_without_mesh_run_id_is_inside_the_run(self) -> None:
        import shutil
        import tempfile
        import threading
        import time
        from mesh.core.auth import classify_peer_run_session
        from mesh.daemon import merge, terminate
        from mesh.daemon.server import DaemonServer
        from mesh.platform.paths import temp_root

        tmp = tempfile.mkdtemp(prefix="mh-r2f-", dir=temp_root())
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.path.join(tmp, "integ")
        os.makedirs(cwd)
        pidfile = os.path.join(tmp, "child.pid")
        run_id = f"run_r2f{os.getpid()}"
        self.addCleanup(terminate.forget, run_id)
        if sys.platform == "win32":
            code = (
                "import os, subprocess, sys, time\n"
                f"pf = {pidfile!r}\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'], env={})\n"
                "open(pf + '.tmp', 'w').write(str(p.pid))\n"
                "os.replace(pf + '.tmp', pf)\n"
                "p.wait()\n"
            )
            test_argv = [sys.executable, "-c", code]
        else:
            # The test command's child drops MESH_RUN_ID (`env -u`), as a hostile test would.
            cmd = (f"env -u MESH_RUN_ID sh -c 'echo $$ > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}; "
                   f"exec {shutil.which('sleep')} 3'")
            test_argv = ["sh", "-c", cmd]
        plan = {"run_id": run_id, "run_dir": os.path.join(tmp, "merges", "mrg_r2f"), "integration": cwd,
                "test_argv": test_argv, "test_timeout_s": 30, "kill_grace_s": 1}
        out = {}
        th = threading.Thread(target=lambda: out.update(merge.run_test_step(plan)), daemon=True)
        th.start()
        deadline = time.monotonic() + 15
        while not os.path.exists(pidfile) and time.monotonic() < deadline:
            time.sleep(0.05)
        with open(pidfile, "r", encoding="utf-8") as f:
            child = int(f.read().strip())
        fake = mock.Mock()
        fake.db_conn.execute.return_value.fetchall.return_value = []
        fake.known_run_ids, fake.active_runs = set(), {}
        DaemonServer.refresh_run_caches(fake)
        got = classify_peer_run_session(child, fake.active_runs, fake.known_run_ids, os.getpid())
        th.join(30)
        self.assertEqual(got, run_id)
        self.assertEqual(out.get("exit_code"), 0, out)
        self.assertNotIn(run_id, merge.in_memory_active_test_runs())


class TestR2P2C_RevokedLeadPaths(unittest.TestCase):
    setUp = __import__("tests.unit.test_projects", fromlist=["TestAuthzMatrix"]).TestAuthzMatrix.setUp
    tearDown = __import__("tests.unit.test_projects", fromlist=["TestAuthzMatrix"]).TestAuthzMatrix.tearDown

    def revoke(self) -> None:
        self.conn.execute("UPDATE projects SET status = 'completed' WHERE id = 'prj_1';")
        self.conn.execute("UPDATE agents SET can_lead = 0 WHERE id = 'lead1';")

    def test_lead_run_token_is_superseded(self) -> None:
        import mesh.constants as c
        from mesh.core.authz import check_authz
        from mesh.errors import MeshError
        lead = {"id": "lead1", "name": "lead1",
                "scope": {"role": "lead", "project_id": "prj_1", "lead_assignment_id": "asg_1"}}
        check_authz(self.conn, "project.deliver", lead, "agent", "run_token", project_id="prj_1")
        self.revoke()
        for cmd in ("project.deliver", "workspace.prune", "approval.request", "project.show"):
            with self.assertRaises(MeshError, msg=cmd) as cm:
                check_authz(self.conn, cmd, lead, "agent", "run_token", project_id="prj_1")
            self.assertEqual(cm.exception.code, c.LEAD_SUPERSEDED, cmd)

    def test_running_lead_turn_is_terminated(self) -> None:
        from mesh.daemon.lead_turns import lead_turn_termination_reason
        run = {"project_id": "prj_1", "lead_assignment_id": "asg_1", "deadline_at": None}
        self.assertIsNone(lead_turn_termination_reason(self.conn, run))
        self.revoke()
        self.assertEqual(lead_turn_termination_reason(self.conn, run), "lead_superseded")

    def test_no_lead_event_fan_out(self) -> None:
        from mesh.core.events import _get_project_lead_info
        self.assertIsNotNone(_get_project_lead_info(self.conn, "prj_1"))
        self.revoke()
        self.assertIsNone(_get_project_lead_info(self.conn, "prj_1"))

    def test_revoked_lead_cannot_cancel_others_approvals(self) -> None:
        import mesh.constants as c
        from mesh.daemon.handlers import approval as ah
        from mesh.errors import MeshError
        row = {"id": "apr_1", "project_id": "prj_1", "requested_by": "grok", "status": "pending"}
        real = self.conn

        class Conn:
            def execute(self, sql, *a):
                if "FROM approvals WHERE id" in sql:
                    m = mock.Mock()
                    m.fetchone.return_value = row
                    return m
                return real.execute(sql, *a)

        ctx = mock.Mock(actor_kind="agent", via="token", principal={"id": "lead1", "name": "lead1"},
                        args={"approval_id": "apr_1"}, conn=Conn())
        self.revoke()
        with mock.patch.object(ah, "_check_approval_run_scope"):
            with self.assertRaises(MeshError) as cm:
                ah.handle_approval_cancel(ctx)
        self.assertEqual(cm.exception.code, c.NOT_REQUESTER)


if __name__ == "__main__":
    unittest.main()
