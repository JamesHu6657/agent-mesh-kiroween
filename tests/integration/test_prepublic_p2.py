"""Regression tests for the pre-public review (Linear Agent chat, main@11bf012) P2 findings.

P2-4 event.tail: a lead run token streams only its own project's events.
P2-5 idempotency replay: a run token scoped to project A cannot replay a cached
     response stored under the same key by a run token scoped to project B.
P2-6 project.complete: an accepted but unmerged code task blocks completion.
P2-7 disabled agents: queued work of a disabled launch agent is never started.
P2-8 dep.add: an already-queued task cannot gain an unfinished prerequisite.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

import mesh.constants as c
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.dep import handle_dep_add
from mesh.daemon.handlers.project import handle_project_complete
from mesh.errors import MeshError

USER = {"id": "usr_1", "name": "user"}


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-p2-")
        self.conn = init_db(os.path.join(self.tmp.name, "mesh.db"))
        x = self.conn.execute
        x("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1','user','user','now');")
        x("INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at) "
          "VALUES ('prj_1','P','G','active','usr_1','direct',1,'now','now');")
        x("INSERT INTO acceptance_criteria (id, project_id, ordinal, text, created_at) "
          "VALUES ('crt_1','prj_1',1,'c1','now');")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def task(self, tid, kind, status, assignee=None):
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, timeout_s, "
            "max_attempts, created_by, created_at, updated_at, queued_at) "
            "VALUES (?, 'prj_1', ?, 's', ?, ?, ?, 'edit', 600, 3, 'usr_1', 'now', 'now', 'now');",
            (tid, tid, kind, status, assignee),
        )
        self.conn.commit()

    def ctx(self, cmd, args):
        return HandlerContext(conn=self.conn, principal=dict(USER), actor_kind="user", via="direct",
                              request_id="req-p2", home=self.tmp.name, cmd=cmd, args=args)


class TestProjectCompleteRequiresMerge(_DbCase):
    REPORT = {"summary": "s", "criteria": [{"id": "crt_1", "met": True, "evidence": "e"}]}

    def complete(self):
        return handle_project_complete(self.ctx("project.complete", {"project_id": "prj_1", "report": self.REPORT}))

    def test_accepted_code_task_blocks(self):
        self.task("tsk_code", "code", "accepted")
        with self.assertRaises(MeshError) as cm:
            self.complete()
        self.assertEqual(cm.exception.code, c.COMPLETION_BLOCKED)
        self.assertIn("tsk_code", cm.exception.details["blocking"])

    def test_merged_code_and_accepted_general_do_not_block(self):
        self.task("tsk_code", "code", "merged")
        self.task("tsk_gen", "general", "accepted")
        self.task("tsk_cx", "code", "cancelled")
        self.complete()
        row = self.conn.execute("SELECT status FROM projects WHERE id = 'prj_1';").fetchone()
        self.assertEqual(row["status"], "completed")


class TestDepAddOnQueuedTask(_DbCase):
    def add(self):
        return handle_dep_add(self.ctx("dep.add", {"task_id": "tsk_a", "on": "tsk_b"}))

    def test_queued_task_rejects_unfinished_prerequisite(self):
        self.task("tsk_a", "general", "queued")
        self.task("tsk_b", "general", "assigned")
        with self.assertRaises(MeshError) as cm:
            self.add()
        self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_TRANSITION, c.EXIT_CONFLICT))
        n = self.conn.execute("SELECT COUNT(*) AS n FROM task_deps;").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_queued_task_accepts_finished_prerequisite(self):
        self.task("tsk_a", "general", "queued")
        self.task("tsk_b", "general", "accepted")
        self.add()

    def test_unqueued_task_accepts_unfinished_prerequisite(self):
        self.task("tsk_a", "general", "assigned")
        self.task("tsk_b", "general", "assigned")
        self.add()


class TestDisabledAgentNotLaunched(_DbCase):
    def test_disabled_launch_agent_queued_task_not_started(self):
        from mesh.daemon import supervisor

        x = self.conn.execute
        x("INSERT INTO principals (id, kind, name, created_at, disabled_at) VALUES ('agt_f','agent','fx','now','now');")
        x("INSERT INTO agents (id, adapter, delivery, max_concurrency, default_model, allowed_models_json, "
          "created_at, updated_at) VALUES ('agt_f','fake','launch',2,'m-a','[\"m-a\"]','now','now');")
        self.conn.commit()
        self.task("tsk_q", "general", "queued", assignee="agt_f")
        started = supervisor.spawn_launch_runs(self.conn, self.tmp.name, testing_mode=True)
        self.assertEqual(started, [])
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id='tsk_q';").fetchone()["status"], "queued")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM runs;").fetchone()["n"], 0)


class TestIdempotencyPartition(unittest.TestCase):
    def test_partition_by_scope(self):
        from mesh.daemon.server import idempotency_partition

        a = {"id": "agt_l", "scope": {"role": "lead", "project_id": "prj_a", "run_id": "r1"}}
        b = {"id": "agt_l", "scope": {"role": "lead", "project_id": "prj_b", "run_id": "r2"}}
        self.assertNotEqual(idempotency_partition(a, "run_token"), idempotency_partition(b, "run_token"))
        self.assertEqual(idempotency_partition(a, "run_token"), idempotency_partition(dict(a), "run_token"))
        self.assertEqual(idempotency_partition({"id": "agt_l"}, "token"), "agt_l")
        self.assertEqual(idempotency_partition(a, "token"), "agt_l")


class TestRunTokenScopeOverSocket(unittest.TestCase):
    """event.tail and idempotent replay through a real meshd (P2-4, P2-5)."""

    def setUp(self):
        from tests.helpers import MeshHarness
        from tests.stubs import create_test_stubs

        self.stubs_dir = tempfile.mkdtemp(prefix="mh-p2-stubs-")
        create_test_stubs(self.stubs_dir)
        self.h = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        h = self.h
        rc, r = h.setup_cli("agent", "create", "--name", "ag-lead", "--adapter", "none", "--delivery", "inbox",
                            "--max-concurrency", "1", "--can-lead", user=True)
        self.assertEqual(rc, 0, r)
        self.lead_id = r["data"]["id"]
        self.prj = {}
        for name in ("a", "b"):
            rc, r = h.setup_cli("project", "create", "--title", f"p-{name}", "--goal", "g", "--criterion", "c1",
                                "--lead", "ag-lead", user=True)
            self.assertEqual(rc, 0, r)
            self.prj[name] = r["data"]["id"]
        conn = sqlite3.connect(os.path.join(h.home, "mesh.db"), timeout=10.0)
        try:
            self.asg = {n: conn.execute("SELECT id FROM lead_assignments WHERE project_id = ? AND ended_at IS NULL;",
                                        (p,)).fetchone()[0] for n, p in self.prj.items()}
        finally:
            conn.close()
        self.rt = {n: self._mint(n) for n in ("a", "b")}

    def tearDown(self):
        self.h.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def _mint(self, n):
        from mesh.core.auth import generate_run_token
        from mesh.core.ids import new_id

        tok, lookup, secret_hash = generate_run_token()
        scope = {"role": "lead", "project_id": self.prj[n], "lead_assignment_id": self.asg[n], "run_id": f"run_lead_{n}"}
        conn = sqlite3.connect(os.path.join(self.h.home, "mesh.db"), timeout=10.0)
        try:
            conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES (?, ?, 'run_token', ?, ?, ?, ?, ?);",
                (new_id("cred"), self.lead_id, lookup, secret_hash, json.dumps(scope), now_iso_utc(),
                 now_iso_utc(time.time() + 600)),
            )
            conn.commit()
        finally:
            conn.close()
        return tok

    def test_event_tail_confined_to_token_project(self):
        rc, r = self.h.cli("event", "tail", "--since", "0", "--timeout", "1s", token=self.rt["a"])
        self.assertEqual(rc, 0, r)
        # One NDJSON line parses as a plain envelope in the harness; several stay raw.
        lines = ([json.loads(l) for l in r["raw_stdout"].splitlines() if l.strip()]
                 if "raw_stdout" in r else [r])
        self.assertTrue(lines, r)
        self.assertEqual({e["project_id"] for e in lines}, {self.prj["a"]})

    def test_replay_does_not_cross_scope(self):
        from mesh.client import MeshClient

        client = MeshClient(self.h.home)
        req = {"v": 1, "cmd": "project.note", "args": {"project_id": self.prj["b"], "text": "secret b note"},
               "idempotency_key": "same-key"}
        first = client.send_request(dict(req, auth={"type": "token", "token": self.rt["b"]}))
        self.assertTrue(first["ok"], first)
        second = client.send_request(dict(req, auth={"type": "token", "token": self.rt["a"]}))
        self.assertFalse(second["ok"], second)
        self.assertFalse(second.get("idempotent_replay", False), second)
        self.assertEqual(second["exit_code"], c.EXIT_FORBIDDEN, second)
        # The B token still replays its own response.
        again = client.send_request(dict(req, auth={"type": "token", "token": self.rt["b"]}))
        self.assertTrue(again.get("idempotent_replay"), again)


if __name__ == "__main__":
    unittest.main()
