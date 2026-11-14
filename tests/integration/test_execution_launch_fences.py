"""Integration tests for the m1c-run2a review fixes: run-token fences, meshd-held
leases, the T5 model re-check, spawn failure paths and token hygiene.

ACC: H4 (run-token clause), C8 (token value test), plus plan §6.4, §7.1, §11.1, §11.5 and §15.1 clauses.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from typing import Any, Dict, Optional, Tuple

import mesh.constants as c
from mesh.core.clock import parse_utc
from mesh.core.proc import find_run_processes, get_proc_environ
from tests.helpers import MeshHarness, assert_h5
from tests.integration.test_execution_launch import create_agent
from tests.stubs import create_test_stubs


def run_cli_as(env: Dict[str, str], *args: str) -> Tuple[int, Dict[str, Any]]:
    """Run the real CLI with a launched run's environment (the executor's view)."""
    res = subprocess.run(["mesh", *args], capture_output=True, text=True, env=dict(env), timeout=60)
    try:
        return res.returncode, json.loads(res.stdout)
    except ValueError:
        return res.returncode, {"raw": res.stdout, "stderr": res.stderr}


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", errors="replace")
    except OSError:
        return ""


class TestLaunchReviewFixes(unittest.TestCase):
    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.tok_lead1 = create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        self.tok_plain = create_agent(self.harness, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
        self.tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        create_agent(
            self.harness, "fx", adapter="fake", delivery="launch",
            allowed_models=["m-a", "m-b"], default_model="m-a", max_concurrency=1,
        )
        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)
        rc_p, resp_p = self.harness.cli(
            "intake", "submit", "--title", "PRJ Fix", "--goal", "g", "--criterion", "c1",
            "--lead", "lead1", token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0, resp_p)
        self.project_id = resp_p["data"]["id"]

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _create(self, spec: str, assignee: str = "fx", *extra: str) -> str:
        rc, resp = self.harness.cli(
            "task", "create", "--project", self.project_id, "--title", "T", "--spec", spec,
            "--assignee", assignee, *extra, token=self.tok_lead1,
        )
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def _row(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        conn = self.harness.db()
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()

    def _rows(self, sql: str, args: tuple = ()) -> list:
        conn = self.harness.db()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def _wait_running(self, task_id: str, timeout: float = 15.0) -> sqlite3.Row:
        found: Dict[str, Any] = {}

        def ready() -> bool:
            row = self._row("SELECT * FROM runs WHERE task_id = ? AND status = 'running';", (task_id,))
            if row is not None:
                found["row"] = row
                return True
            return False

        self.assertTrue(self.harness.wait_for(ready, timeout=timeout), f"no running run for {task_id}")
        return found["row"]

    # -- finding 1 / H4 ----------------------------------------------------
    def test_h4_run_token_explicit_fence_must_match_scope(self) -> None:
        """ACC: H4 - From a live run's env, task heartbeat --fence <scope+1> -> 9 FENCE_MISMATCH, no state change."""
        task_id = self._create('FAKE {"sleep": {"seconds": 120}}')
        run = self._wait_running(task_id)
        env = get_proc_environ(run["wrapper_pid"])
        self.assertIsNotNone(env)
        # The supervisor renews expires_at/heartbeat_at every tick; a fenced write would
        # change the holder, run, fence or the task's status/fence_seq/version.
        fenced = ("SELECT l.key, l.holder_kind, l.holder, l.agent_id, l.run_id, l.fence, t.status, t.fence_seq, t.version "
                  "FROM leases l JOIN tasks t ON l.key = 'task:' || t.id WHERE l.key = ?;")
        lease_before = dict(self._row(fenced, (f"task:{task_id}",)))

        rc, resp = run_cli_as(env, "task", "heartbeat", task_id, "--fence", str(run["fence"] + 1))
        self.assertEqual(rc, 9, resp)
        self.assertEqual(resp["error"]["code"], c.FENCE_MISMATCH)

        rc, resp = run_cli_as(env, "task", "heartbeat", task_id, "--run", "run_other", "--fence", str(run["fence"]))
        self.assertEqual(rc, 9, resp)
        self.assertEqual(resp["error"]["code"], c.FENCE_MISMATCH)

        lease_after = dict(self._row(fenced, (f"task:{task_id}",)))
        self.assertEqual(lease_before, lease_after)

        # The scope's own values pass (heartbeat on a meshd lease leaves expires_at alone).
        rc, resp = run_cli_as(env, "task", "heartbeat", task_id)
        self.assertEqual(rc, 0, resp)
        lease_hb = self._row("SELECT holder_kind, heartbeat_at FROM leases WHERE key = ?;", (f"task:{task_id}",))
        self.assertEqual(lease_hb["holder_kind"], "meshd")
        assert_h5(self, self.harness)  # ACC: H5 (review m1c-run3 #6)

    # -- finding 2 ---------------------------------------------------------
    def test_launch_submit_keeps_meshd_lease_and_run(self) -> None:
        """§6.4/T7: a run-token submit while the wrapper lives keeps the meshd lease and the run; the slot stays taken."""
        gate = os.path.join(self.harness.fixtures_dir, "submit-gate")
        task_id = self._create('FAKE {"wait_release": {"path": "%s"}}' % gate)
        run = self._wait_running(task_id)
        env = get_proc_environ(run["wrapper_pid"])
        second = self._create('FAKE {"sleep": {"seconds": 120}}')

        rc, resp = run_cli_as(env, "task", "submit", task_id, "--summary", "done early")
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["status"], "submitted")

        task = self._row("SELECT status, deliverable_json FROM tasks WHERE id = ?;", (task_id,))
        self.assertEqual(task["status"], "submitted")
        self.assertEqual(json.loads(task["deliverable_json"])["deliverable"]["summary"], "done early")
        lease = self._row("SELECT holder_kind, run_id FROM leases WHERE key = ?;", (f"task:{task_id}",))
        self.assertIsNotNone(lease)
        self.assertEqual(lease["holder_kind"], "meshd")
        self.assertEqual(lease["run_id"], run["id"])
        self.assertEqual(self._row("SELECT status FROM runs WHERE id = ?;", (run["id"],))["status"], "running")
        time.sleep(1.5)  # several scheduler ticks
        self.assertIsNone(self._row("SELECT id FROM runs WHERE task_id = ?;", (second,)))
        # Accepting while the run still holds the lease is refused.
        rc_a, resp_a = self.harness.cli("task", "accept", task_id, token=self.tok_lead1)
        self.assertEqual(rc_a, 6, resp_a)
        self.assertEqual(resp_a["error"]["code"], c.RUN_STILL_ACTIVE)

        # Review m1c-run2 finding 3: once the process exits, the run closes with
        # run.exited_after_submit, the lease goes, the token is revoked, the task stays submitted.
        open(gate, "w").close()
        self.assertTrue(self.harness.wait_for(
            lambda: self._row("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)) is None, timeout=30.0))
        self.assertEqual(self._row("SELECT status FROM tasks WHERE id = ?;", (task_id,))["status"], "submitted")
        self.assertEqual(self._row("SELECT status FROM runs WHERE id = ?;", (run["id"],))["status"], "exited")
        self.assertEqual(len(self._rows("SELECT seq FROM events WHERE run_id = ? AND type = 'run.exited_after_submit';",
                                        (run["id"],))), 1)
        self.assertEqual(run_cli_as(env, "task", "show", task_id)[1]["error"]["code"], c.TOKEN_REVOKED)

    def test_launch_fail_keeps_meshd_lease_until_exit(self) -> None:
        """§6.4/T8: a run-token fail while the wrapper lives keeps the task running with its lease; the reason is redacted."""
        gate = os.path.join(self.harness.fixtures_dir, "fail-gate")
        task_id = self._create('FAKE {"wait_release": {"path": "%s"}}' % gate)
        run = self._wait_running(task_id)
        env = get_proc_environ(run["wrapper_pid"])
        secret = "sk-" + "Q" * 24
        rc, resp = run_cli_as(env, "task", "fail", task_id, "--reason", f"broke with {secret}")
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self._row("SELECT status FROM tasks WHERE id = ?;", (task_id,))["status"], "running")
        self.assertIsNotNone(self._row("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)))
        run_row = self._row("SELECT status, result_json FROM runs WHERE id = ?;", (run["id"],))
        self.assertEqual(run_row["status"], "running")
        self.assertNotIn(secret, run_row["result_json"] or "")

        # Review m1c-run2 finding 3: after exit → failed(reported), one task.failed to lead1
        # whose actor is the reporting run principal, lease gone, token revoked.
        open(gate, "w").close()
        self.assertTrue(self.harness.wait_for(
            lambda: self._row("SELECT status FROM tasks WHERE id = ?;", (task_id,))["status"] == "failed", timeout=30.0))
        failure = json.loads(self._row("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
        self.assertEqual(failure["reason"], "reported")
        self.assertIsNone(self._row("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)))
        evts = self._rows("SELECT actor_id, actor_kind, via FROM events WHERE task_id = ? AND type = 'task.failed';",
                          (task_id,))
        self.assertEqual(len(evts), 1)
        self.assertNotEqual(evts[0]["actor_kind"], "system")
        msgs = self._rows(
            "SELECT o.id FROM outbox o JOIN events e ON o.event_seq = e.seq WHERE e.task_id = ? "
            "AND e.type = 'task.failed' AND o.recipient_id = (SELECT id FROM principals WHERE name = 'lead1');",
            (task_id,))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(run_cli_as(env, "task", "show", task_id)[1]["error"]["code"], c.TOKEN_REVOKED)

    def test_wrapper_killed_without_exit_json_is_lost(self) -> None:
        """Review m1c-run2 finding 3: SIGKILL the wrapper (pid from this home's DB) → failed(lost) once the child is gone."""
        # The child waits for a release file (not a 1 s sleep that can end before the kill on
        # a loaded host); it is released right after the wrapper is killed, so it exits soon.
        release = os.path.join(self.harness.fixtures_dir, "lost-release")
        task_id = self._create('FAKE {"wait_release": {"path": "%s"}}' % release)
        run = self._wait_running(task_id)
        os.kill(run["wrapper_pid"], 9)
        open(release, "w").close()
        self.assertTrue(self.harness.wait_for(
            lambda: self._row("SELECT status FROM tasks WHERE id = ?;", (task_id,))["status"] == "failed", timeout=30.0))
        failure = json.loads(self._row("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
        self.assertEqual(failure["reason"], "lost")
        self.assertEqual(self._row("SELECT status FROM runs WHERE id = ?;", (run["id"],))["status"], "lost")
        self.assertIsNone(self._row("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)))
        self.assertEqual(find_run_processes(run["id"], run["pgid"]), [])

    def test_inbox_fail_reason_redacted(self) -> None:
        """§15.1: the task fail reason is redacted before failure_json, result_json and the event."""
        task_id = self._create("Inbox work", "ibx")
        rc_s, resp_s = self.harness.cli("task", "start", task_id, token=self.tok_ibx)
        self.assertEqual(rc_s, 0, resp_s)
        secret = "sk-" + "Z" * 24
        rc, resp = self.harness.cli(
            "task", "fail", task_id, "--reason", f"leaked {secret}",
            "--run", resp_s["data"]["run_id"], "--fence", str(resp_s["data"]["fence"]),
            token=self.tok_ibx,
        )
        self.assertEqual(rc, 0, resp)
        task = self._row("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))
        run = self._row("SELECT result_json FROM runs WHERE id = ?;", (resp_s["data"]["run_id"],))
        evt = self._row("SELECT reason, payload_json FROM events WHERE task_id = ? AND type = 'task.failed';", (task_id,))
        for blob in (task["failure_json"], run["result_json"], evt["payload_json"], evt["reason"] or ""):
            self.assertNotIn(secret, blob)
        self.assertIn("[REDACTED]", task["failure_json"])

    # -- finding 3 ---------------------------------------------------------
    def test_t5_rechecks_model_for_task_promoted_same_tick(self) -> None:
        """§6.2 T5/T6: a dependency-waiting --model m-b task whose model left the allow-list fails at promotion, never runs."""
        dep = self._create("Inbox dependency", "ibx")
        rc, resp = self.harness.cli(
            "task", "create", "--project", self.project_id, "--title", "Waits", "--spec", 'FAKE {"succeed": {}}',
            "--assignee", "fx", "--model", "m-b", "--depends-on", dep, token=self.tok_lead1,
        )
        self.assertEqual(rc, 0, resp)
        waiting = resp["data"]["id"]
        self.assertEqual(self._row("SELECT status FROM tasks WHERE id = ?;", (waiting,))["status"], "assigned")

        rc_u, resp_u = self.harness.cli(
            "agent", "update", "fx", "--allowed-model", "m-a", "--default-model", "m-a", user=True
        )
        self.assertEqual(rc_u, 0, resp_u)

        rc_s, resp_s = self.harness.cli("task", "start", dep, token=self.tok_ibx)
        self.assertEqual(rc_s, 0, resp_s)
        rc_sub, resp_sub = self.harness.cli(
            "task", "submit", dep, "--summary", "ok", "--run", resp_s["data"]["run_id"],
            "--fence", str(resp_s["data"]["fence"]), token=self.tok_ibx,
        )
        self.assertEqual(rc_sub, 0, resp_sub)
        rc_a, resp_a = self.harness.cli("task", "accept", dep, token=self.tok_lead1)
        self.assertEqual(rc_a, 0, resp_a)

        def failed() -> bool:
            row = self._row("SELECT status FROM tasks WHERE id = ?;", (waiting,))
            return row["status"] == "failed"

        self.assertTrue(self.harness.wait_for(failed, timeout=10.0))
        failure = json.loads(self._row("SELECT failure_json FROM tasks WHERE id = ?;", (waiting,))["failure_json"])
        self.assertEqual(failure["reason"], "model_not_allowed")
        self.assertIsNone(self._row("SELECT id FROM runs WHERE task_id = ?;", (waiting,)))
        msgs = self._rows(
            "SELECT o.id FROM outbox o JOIN events e ON o.event_seq = e.seq "
            "WHERE e.task_id = ? AND e.type = 'task.failed' AND o.recipient_id = "
            "(SELECT id FROM principals WHERE name = 'lead1');",
            (waiting,),
        )
        self.assertEqual(len(msgs), 1)

    # -- finding 5 ---------------------------------------------------------
    def test_run_token_value_never_in_argv_spec_or_logs(self) -> None:
        """§7.1/§11.1: the run token value is absent from spec.json, argv_redacted_json, both cmdlines and meshd.log; expiry = deadline + 300 s."""
        task_id = self._create('FAKE {"sleep": {"seconds": 120}}')
        run = self._wait_running(task_id)
        env = get_proc_environ(run["wrapper_pid"])
        token = env["MESH_TOKEN"]
        self.assertTrue(token.startswith("mesh_rt_"))

        with open(os.path.join(run["run_dir"], "spec.json"), "r", encoding="utf-8") as f:
            self.assertNotIn(token, f.read())
        self.assertNotIn(token, run["argv_redacted_json"])
        self.assertNotIn(token, _cmdline(run["wrapper_pid"]))
        def child_pids() -> list:
            return [p for p in find_run_processes(run["id"], run["pgid"]) if p != run["wrapper_pid"]]

        # The wrapper starts the child just after the run turns running.
        self.harness.wait_for(lambda: bool(child_pids()), timeout=5.0)
        children = child_pids()
        self.assertTrue(children, "the fake child should be running in the run's process group")
        for pid in children:
            self.assertNotIn(token, _cmdline(pid))
        log_path = os.path.join(self.harness.home, "logs", "meshd.log")
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                self.assertNotIn(token, f.read())

        cred = self._row(
            "SELECT expires_at FROM credentials WHERE kind = 'run_token' AND json_extract(scope_json, '$.run_id') = ?;",
            (run["id"],),
        )
        delta = parse_utc(cred["expires_at"]) - parse_utc(run["deadline_at"])
        self.assertAlmostEqual(delta.total_seconds(), 300.0, delta=1.0)

    def test_spawn_failure_marks_run_spawn_failed(self) -> None:
        """§11.1 step 4: an effect failure -> run spawn_failed, task failed(spawn_failed), no lease, token revoked, one task.failed."""
        self.harness.hold_launch_starts()
        runs_dir = os.path.join(self.harness.home, "runs")
        if os.path.isdir(runs_dir) and not os.listdir(runs_dir):
            os.rmdir(runs_dir)
        self.assertFalse(os.path.isdir(runs_dir), "runs dir must be absent so the blocker file can replace it")
        with open(runs_dir, "w", encoding="utf-8") as f:
            f.write("blocks run dir creation\n")
        task_id = self._create('FAKE {"succeed": {}}')
        self.harness.release_launch_starts()
        self.harness.cli("daemon", "status")

        def failed() -> bool:
            return self._row("SELECT status FROM tasks WHERE id = ?;", (task_id,))["status"] == "failed"

        try:
            self.assertTrue(self.harness.wait_for(failed, timeout=10.0))
        finally:
            os.remove(runs_dir)
        run = self._row("SELECT id, status, end_reason FROM runs WHERE task_id = ?;", (task_id,))
        self.assertEqual(run["status"], "spawn_failed")
        failure = json.loads(self._row("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
        self.assertEqual(failure["reason"], "spawn_failed")
        self.assertIsNone(self._row("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)))
        cred = self._row(
            "SELECT revoked_at FROM credentials WHERE kind = 'run_token' AND json_extract(scope_json, '$.run_id') = ?;",
            (run["id"],),
        )
        self.assertIsNotNone(cred["revoked_at"])
        events = self._rows("SELECT id FROM events WHERE task_id = ? AND type = 'task.failed';", (task_id,))
        self.assertEqual(len(events), 1)

    def test_tx_a_conflict_leaves_no_rows(self) -> None:
        """§11.1 TX A: a conflicting lease makes TX A roll back: no run, lease or credential row for the task."""
        self.harness.hold_launch_starts()
        task_id = self._create('FAKE {"succeed": {}}')
        db_path = os.path.join(self.harness.home, "mesh.db")
        rw = sqlite3.connect(db_path, isolation_level=None, timeout=10)
        rw.row_factory = sqlite3.Row
        try:
            agent_id = rw.execute("SELECT id FROM principals WHERE name = 'fx';").fetchone()["id"]
            boot = rw.execute("SELECT boot_id FROM runs LIMIT 1;").fetchone()
            rw.execute("BEGIN IMMEDIATE;")
            rw.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, boot_id, run_dir) "
                "VALUES ('run_blocker', 'external', NULL, ?, ?, 0, 0, 'none', 'exited', ?, '/nonexistent');",
                (self.project_id, agent_id, boot["boot_id"] if boot else "boot_test"),
            )
            rw.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES (?, 'run_blocker', 'agent', 'blocker', ?, 0, '2026-01-01T00:00:00Z', '2999-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z');",
                (f"task:{task_id}", agent_id),
            )
            rw.execute("COMMIT;")
            self.harness.release_launch_starts()
            self.harness.cli("daemon", "status")
            time.sleep(2.5)  # a few ticks, each attempting TX A
            self.assertEqual(self._row("SELECT status FROM tasks WHERE id = ?;", (task_id,))["status"], "queued")
            self.assertIsNone(self._row("SELECT id FROM runs WHERE task_id = ?;", (task_id,)))
            self.assertEqual(
                [r["run_id"] for r in self._rows("SELECT run_id FROM leases WHERE key = ?;", (f"task:{task_id}",))],
                ["run_blocker"],
            )
            self.assertEqual(
                len(self._rows(
                    "SELECT id FROM credentials WHERE kind = 'run_token' AND json_extract(scope_json, '$.task_id') = ?;",
                    (task_id,),
                )),
                0,
            )
        finally:
            # Remove the blocker so the teardown invariant check sees a consistent DB.
            self.harness.hold_launch_starts()
            rw.execute("BEGIN IMMEDIATE;")
            rw.execute("DELETE FROM leases WHERE run_id = 'run_blocker';")
            rw.execute("DELETE FROM runs WHERE id = 'run_blocker';")
            rw.execute("COMMIT;")
            rw.close()


if __name__ == "__main__":
    unittest.main()
