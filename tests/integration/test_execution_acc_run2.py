"""M1c run 2 acceptance tests: launch execution end to end with the fake adapter.

Fixture (checklist F1): `fx` fake/launch, models m-a/m-b, default m-a, max 2;
agents lead1, lead2, ibx, grok (intake+relay), plain, rlead; PRJ created via
intake by grok with criteria c1 and c2.

ACC: C7 C8 D4 D6 D11 D12 D13 D15 A8 F1 F2 F4 F9 G6 G18 H8 H11 J1 J9
(H12 lives in test_execution_launch.py)
"""

import concurrent.futures
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from mesh.core.clock import parse_utc
from mesh.core.proc import find_run_processes, get_proc_environ
from tests.helpers import MeshHarness, assert_h5, idle_bound, serial
from tests.integration.test_execution_launch import create_agent
from tests.stubs import create_test_stubs

FAKE_KEY = "sk-acc0123456789abcdef"


def _ts(value: Optional[str]) -> float:
    return parse_utc(value).timestamp() if value else 0.0


class Run2AccBase(unittest.TestCase):
    """Shared F1 fixture."""

    daemon_extra_env: Dict[str, str] = {}

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=False, extra_path=self.stubs_dir)
        if self.daemon_extra_env:
            base_get_env = self.harness.get_env
            extra = dict(self.daemon_extra_env)

            def get_env_with_extra() -> Dict[str, str]:
                env = base_get_env()
                env.update(extra)
                return env

            self.harness.get_env = get_env_with_extra  # type: ignore[method-assign]
        self.harness.start_daemon()
        self.tokens: Dict[str, str] = {}
        self.tokens["lead1"] = create_agent(self.harness, "lead1", can_lead=True)
        self.tokens["lead2"] = create_agent(self.harness, "lead2", can_lead=True)
        self.tokens["ibx"] = create_agent(self.harness, "ibx")
        self.tokens["grok"] = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tokens["plain"] = create_agent(self.harness, "plain", capabilities=["intake"])
        self.tokens["rlead"] = create_agent(self.harness, "rlead", can_lead=True)
        self.tokens["fx"] = create_agent(self.harness, "fx", adapter="fake", delivery="launch",
                                         allowed_models=["m-a", "m-b"], default_model="m-a", max_concurrency=2)
        self.tok_lead1 = self.tokens["lead1"]
        rc, resp = self.harness.setup_cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.setup_cli("intake", "submit", "--title", "PRJ", "--goal", "Run 2 acceptance",
                                    "--criterion", "c1", "--criterion", "c2", "--lead", "lead1",
                                    token=self.tokens["grok"])
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    # -- helpers -------------------------------------------------------------
    def create_task(self, spec: str, *extra: str, assignee: str = "fx", title: str = "T") -> str:
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", title,
                                    "--spec", spec, "--assignee", assignee, *extra, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def gate(self, name: str) -> str:
        """Path of a release file for the fake's wait_release step (load-independent hold)."""
        return os.path.join(self.harness.fixtures_dir, f"gate-{name}")

    @staticmethod
    def release(path: str) -> None:
        open(path, "w").close()

    def held_spec(self, name: str) -> str:
        return 'FAKE {"wait_release": {"path": "%s"}}' % self.gate(name)

    def q(self, sql: str, args: tuple = ()) -> List[Any]:
        conn = self.harness.db()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def q1(self, sql: str, args: tuple = ()) -> Any:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def status(self, task_id: str) -> str:
        return self.q1("SELECT status FROM tasks WHERE id = ?;", (task_id,))["status"]

    def wait_status(self, task_id: str, status: str, timeout: float = 30.0) -> None:
        ok = self.harness.wait_for(lambda: self.status(task_id) == status, timeout=timeout)
        self.assertTrue(ok, f"{task_id} is {self.status(task_id)}, never reached {status}")

    def wait_running_run(self, task_id: str, timeout: float = 20.0) -> Any:
        box: Dict[str, Any] = {}

        def ready() -> bool:
            row = self.q1("SELECT * FROM runs WHERE task_id = ? AND status = 'running';", (task_id,))
            if row is not None:
                box["row"] = row
            return row is not None

        self.assertTrue(self.harness.wait_for(ready, timeout=timeout), f"no running run for {task_id}")
        return box["row"]

    def runs_of(self, task_id: str) -> List[Any]:
        return self.q("SELECT * FROM runs WHERE task_id = ? ORDER BY rowid;", (task_id,))

    def doctor_ok(self) -> None:
        """ACC: F2/A8 - invariants clean right now."""
        rc, resp = self.harness.cli("doctor", "--check-invariants", user=True, timeout=30.0)
        self.assertEqual(rc, 0, resp)

    def lead1_task_failed_msgs(self, task_id: str) -> List[Any]:
        return self.q(
            "SELECT o.payload_json FROM outbox o JOIN events e ON o.event_seq = e.seq "
            "WHERE e.task_id = ? AND o.type = 'task.failed' AND o.recipient_id = "
            "(SELECT id FROM principals WHERE name = 'lead1');",
            (task_id,),
        )

    @staticmethod
    def max_overlap(intervals: List[Tuple[float, float]]) -> int:
        points = []
        for start, end in intervals:
            points.append((start, 1))
            points.append((end, -1))
        points.sort(key=lambda p: (p[0], p[1]))  # ends before starts at equal times
        cur = best = 0
        for _, delta in points:
            cur += delta
            best = max(best, cur)
        return best


class TestRun2Execution(Run2AccBase):
    # -- F1 / F2 -------------------------------------------------------------
    def test_f1_happy_path_events(self) -> None:
        """ACC: F1, F2 - succeed -> wait submitted -> accept; events assigned→queued→running→submitted→accepted with full fields."""
        task_id = self.create_task('FAKE {"succeed": {}}')
        rc, resp = self.harness.cli("task", "wait", task_id, "--until", "submitted", "--timeout", "60s",
                                    token=self.tok_lead1, timeout=70.0)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("task", "accept", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.status(task_id), "accepted")
        rc, resp = self.harness.cli("event", "list", "--task", task_id, "--limit", "200", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        events = resp["data"]["items"]
        transitions = [e for e in events if e.get("to_state")]
        self.assertEqual([e["to_state"] for e in transitions],
                         ["assigned", "queued", "running", "submitted", "accepted"])
        prev = None
        for e in transitions:
            for field in ("actor_id", "actor_kind", "via", "ts", "from_state", "to_state", "request_id"):
                self.assertIn(field, e, (field, e))
            self.assertTrue(e["actor_id"] and e["actor_kind"] and e["via"] and e["ts"], e)
            if prev is not None:
                self.assertEqual(e["from_state"], prev)
            prev = e["to_state"]
            # Reading (review m1c-run2 7a): request-driven transitions carry their request_id;
            # system transitions (queued, running, auto-submitted) have no request, so it is null.
            if e["to_state"] in ("assigned", "accepted"):
                self.assertTrue(e["request_id"], e)
            else:
                self.assertIsNone(e["request_id"], e)
        self.doctor_ok()

    # -- H11 -----------------------------------------------------------------
    def test_h11_auto_submit_by_system(self) -> None:
        """ACC: H11 - exit 0 without CLI submit -> submitted, submitted_by system, deliverable from the mesh-deliverable block."""
        task_id = self.create_task('FAKE {"succeed": {"text": "all good"}}')
        self.wait_status(task_id, "submitted")
        row = self.q1("SELECT deliverable_json FROM tasks WHERE id = ?;", (task_id,))
        record = json.loads(row["deliverable_json"])
        self.assertEqual(record["submitted_by"], "system")
        self.assertEqual(record["deliverable"]["summary"], "Task completed.")
        self.assertEqual(record["deliverable"]["tests"], {"command": "none", "result": "not_run"})
        rc, resp = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["submitted_by"], "system")
        run = self.runs_of(task_id)[0]
        self.assertEqual((run["status"], run["exit_code"]), ("exited", 0))
        self.assertIsNone(self.q1("SELECT 1 FROM leases WHERE run_id = ?;", (run["id"],)))
        self.doctor_ok()

    # -- D4 ------------------------------------------------------------------
    def test_d4_model_default_and_explicit(self) -> None:
        """ACC: D4 - no --model -> m-a on task and run; --model m-b -> m-b on task and run (run show)."""
        t1 = self.create_task('FAKE {"succeed": {}}')
        t2 = self.create_task('FAKE {"succeed": {}}', "--model", "m-b")
        for task_id, model in ((t1, "m-a"), (t2, "m-b")):
            self.wait_status(task_id, "submitted")
            self.assertEqual(self.q1("SELECT model FROM tasks WHERE id = ?;", (task_id,))["model"], model)
            run_id = self.runs_of(task_id)[0]["id"]
            rc, resp = self.harness.cli("run", "show", run_id, token=self.tok_lead1)
            self.assertEqual(rc, 0, resp)
            self.assertEqual(resp["data"]["model"], model)
        self.doctor_ok()

    # -- D6 ------------------------------------------------------------------
    def test_d6_queued_model_removed_from_allow_list(self) -> None:
        """ACC: D6 - both slots busy; queued --model m-b; allow-list narrowed -> failed(model_not_allowed), one task.failed to lead1, no run."""
        busy = [self.create_task(self.held_spec("d6")) for _ in range(2)]
        for t in busy:
            self.wait_running_run(t)
        queued = self.create_task('FAKE {"succeed": {}}', "--model", "m-b")
        self.assertEqual(self.status(queued), "queued")
        rc, resp = self.harness.cli("agent", "update", "fx", "--allowed-model", "m-a", "--default-model", "m-a", user=True)
        self.assertEqual(rc, 0, resp)
        self.wait_status(queued, "failed")
        failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (queued,))["failure_json"])
        self.assertEqual(failure["reason"], "model_not_allowed")
        self.assertEqual(self.runs_of(queued), [])
        self.release(self.gate("d6"))
        for t in busy:
            self.wait_status(t, "submitted")
        self.assertEqual(len(self.lead1_task_failed_msgs(queued)), 1)
        self.assertEqual(self.runs_of(queued), [])
        self.doctor_ok()

    # -- D11 -----------------------------------------------------------------
    def test_d11_concurrency_limit_two(self) -> None:
        """ACC: D11 - max 2, five record_pid+sleep tasks: <= 2 overlap; queued items show queue_position and concurrency_limit; all submitted."""
        pid_file = os.path.join(self.harness.fixtures_dir, "acc-pids")
        # 2 s runs (was 3 s, m3-a1-speed): five runs at max 2 keep tasks queued for about
        # 4 s. The queue is watched from before the first create (fix round, Linear MINOR),
        # so the whole queued span is observed, not only what is left after five creates.
        spec = 'FAKE {"steps": [{"record_pid": {"path": "%s"}}, {"sleep": {"seconds": 2}}]}' % pid_file
        watch_errors: List[str] = []

        def watch_queue() -> bool:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rc, resp = self.harness.cli("task", "list", "--assignee", "fx", "--status", "queued", token=self.tok_lead1)
                if rc != 0:
                    watch_errors.append(f"task list rc {rc}: {resp}")
                    return False
                items = resp["data"]["items"] if isinstance(resp["data"], dict) else resp["data"]
                if items:
                    for it in items:
                        if it.get("queue_position") is None or it.get("blocked_reason") != "concurrency_limit":
                            watch_errors.append(f"bad queued item: {it}")
                    return True
                time.sleep(0.1)
            return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            watcher = pool.submit(watch_queue)
            tasks = [self.create_task(spec) for _ in range(5)]
            seen_blocked = watcher.result()
        self.assertEqual(watch_errors, [])
        self.assertTrue(seen_blocked, "never saw a queued fx task")
        for t in tasks:
            self.wait_status(t, "submitted", timeout=40.0)
        intervals = []
        for t in tasks:
            runs = self.runs_of(t)
            self.assertEqual(len(runs), 1)
            intervals.append((_ts(runs[0]["started_at"]), _ts(runs[0]["ended_at"])))
        self.assertLessEqual(self.max_overlap(intervals), 2)
        with open(pid_file, "r", encoding="utf-8") as f:
            self.assertEqual(len([ln for ln in f if ln.strip()]), 5)
        assert_h5(self, self.harness)  # ACC: H5 (review m1c-run3 #6)
        self.doctor_ok()

    # -- D12 -----------------------------------------------------------------
    def test_d12_priority_then_fifo_on_full_agent(self) -> None:
        """ACC: D12 - fx full; queue priorities 0, 5, 0 -> start order: 5, then the two 0s FIFO."""
        busy = [self.create_task(self.held_spec("d12")) for _ in range(2)]
        for t in busy:
            self.wait_running_run(t)
        a = self.create_task('FAKE {"sleep": {"seconds": 1}}', "--priority", "0", title="A")
        b = self.create_task('FAKE {"sleep": {"seconds": 1}}', "--priority", "5", title="B")
        d = self.create_task('FAKE {"sleep": {"seconds": 1}}', "--priority", "0", title="D")
        self.release(self.gate("d12"))
        for t in (a, b, d):
            self.wait_status(t, "submitted", timeout=40.0)
        order = [r["task_id"] for r in self.q(
            "SELECT task_id FROM runs WHERE task_id IN (?, ?, ?) ORDER BY rowid;", (a, b, d))]
        self.assertEqual(order, [b, a, d])
        self.doctor_ok()

    # -- D13 -----------------------------------------------------------------
    def test_d13_global_cap_one(self) -> None:
        """ACC: D13 - scheduler.max_total_runs 1, two fake agents with max 2, four sleeping tasks -> never more than one run."""
        rc, resp = self.harness.cli("config", "set", "scheduler.max_total_runs", "1", user=True)
        self.assertEqual(rc, 0, resp)
        create_agent(self.harness, "fx2", adapter="fake", delivery="launch",
                     allowed_models=["m-a"], default_model="m-a", max_concurrency=2)
        spec = 'FAKE {"sleep": {"seconds": 1}}'
        tasks = [self.create_task(spec, assignee=name) for name in ("fx", "fx2", "fx", "fx2")]
        for t in tasks:
            self.wait_status(t, "submitted", timeout=40.0)
        intervals = [(_ts(r["started_at"]), _ts(r["ended_at"])) for t in tasks for r in self.runs_of(t)]
        self.assertEqual(len(intervals), 4)
        self.assertEqual(self.max_overlap(intervals), 1)
        self.doctor_ok()

    # -- D15 / A8 ------------------------------------------------------------
    def test_d15_lowered_limit_no_kill(self) -> None:
        """ACC: D15, A8 - lower max-concurrency to 1 while 2 run: no kill; doctor rc 0 above the limit; next starts after both finish."""
        busy = [self.create_task(self.held_spec("d15")) for _ in range(2)]
        busy_ids = [self.wait_running_run(t)["id"] for t in busy]
        rc, resp = self.harness.cli("agent", "update", "fx", "--max-concurrency", "1", user=True)
        self.assertEqual(rc, 0, resp)
        nxt = self.create_task('FAKE {"succeed": {}}')

        def both_running() -> List[str]:
            return [self.q1("SELECT status FROM runs WHERE id = ?;", (r,))["status"] for r in busy_ids]

        self.assertEqual(both_running(), ["running", "running"])
        self.doctor_ok()  # A8: running count above the lowered limit is not a violation
        self.assertEqual(both_running(), ["running", "running"])
        time.sleep(1.0)
        self.assertEqual(self.runs_of(nxt), [])
        self.release(self.gate("d15"))
        for t in busy:
            self.wait_status(t, "submitted", timeout=30.0)
        self.wait_status(nxt, "submitted", timeout=30.0)
        busy_runs = [self.runs_of(t)[0] for t in busy]
        for r in busy_runs:
            self.assertEqual((r["status"], r["exit_code"], r["signal"]), ("exited", 0, None))
        last_end = max(_ts(r["ended_at"]) for r in busy_runs)
        self.assertGreaterEqual(_ts(self.runs_of(nxt)[0]["started_at"]), last_end)
        self.doctor_ok()

    # -- F4 ------------------------------------------------------------------
    def test_f4_reject_rework_prompt(self) -> None:
        """ACC: F4 - reject --reason "add tests" -> rework then queued/running; new prompt.md has the reason and attempt 2/; empty reason -> 2."""
        task_id = self.create_task('FAKE {"succeed": {}}', "--max-attempts", "3")
        self.wait_status(task_id, "submitted")
        rc, resp = self.harness.cli("task", "reject", task_id, "--reason", "", token=self.tok_lead1)
        self.assertEqual(rc, 2, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(self.status(task_id), "submitted")
        rc, resp = self.harness.cli("task", "reject", task_id, "--reason", "add tests", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["status"], "rework")
        self.assertTrue(self.harness.wait_for(lambda: len(self.runs_of(task_id)) == 2, timeout=20.0))
        run2 = self.runs_of(task_id)[1]
        with open(os.path.join(run2["run_dir"], "prompt.md"), "r", encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("add tests", prompt)
        self.assertIn("attempt 2/", prompt)
        states = [r["to_state"] for r in self.q(
            "SELECT to_state FROM events WHERE task_id = ? AND to_state IS NOT NULL ORDER BY seq;", (task_id,))]
        i = states.index("rework")
        self.assertEqual(states[i + 1:i + 3], ["queued", "running"])
        self.wait_status(task_id, "submitted")
        self.doctor_ok()

    # -- F9 ------------------------------------------------------------------
    def test_f9_distinct_task_dirs_and_lead_dir(self) -> None:
        """ACC: F9 - two write_file tasks: distinct workspace_path under tasks/<tsk>/, out.txt only in its own dir; lead dir differs."""
        t1 = self.create_task('FAKE {"write_file": {"path": "out.txt", "content": "first"}}', title="W1")
        t2 = self.create_task('FAKE {"write_file": {"path": "out.txt", "content": "second"}}', title="W2")
        for t in (t1, t2):
            self.wait_status(t, "submitted")
        paths = {}
        for t, content in ((t1, "first"), (t2, "second")):
            rc, resp = self.harness.cli("task", "show", t, token=self.tok_lead1)
            self.assertEqual(rc, 0, resp)
            path = resp["data"]["workspace_path"]
            self.assertEqual(path.rstrip("/"), os.path.join(self.harness.home, "workspaces", self.project_id, "tasks", t))
            with open(os.path.join(path, "out.txt"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), content)
            paths[t] = path
        self.assertNotEqual(paths[t1], paths[t2])
        found = []
        for dirpath, _dirs, files in os.walk(os.path.join(self.harness.home, "workspaces")):
            if "out.txt" in files:
                found.append(dirpath.rstrip("/"))
        self.assertEqual(sorted(found), sorted(p.rstrip("/") for p in paths.values()))
        rc, resp = self.harness.cli("workspace", "list", "--project", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        entries = resp["data"]["workspaces"]
        lead_entries = [e for e in entries if e["kind"] == "lead"]
        task_entries = {e["task_id"]: e["path"] for e in entries if e["kind"] == "task"}
        self.assertEqual(task_entries, paths)
        self.assertEqual(len(lead_entries), 1)
        lead_id = self.q1("SELECT id FROM principals WHERE name = 'lead1';")["id"]
        self.assertEqual(lead_entries[0]["path"].rstrip("/"),
                         os.path.join(self.harness.home, "workspaces", self.project_id, "lead", lead_id))
        self.assertTrue(lead_entries[0]["exists"])
        self.assertNotIn(lead_entries[0]["path"], paths.values())
        self.doctor_ok()

    # -- G6 ------------------------------------------------------------------
    def test_g6_run_token_cannot_approve(self) -> None:
        """ACC: G6 - try_approve with the executor run token prints rc 4 CAPABILITY_MISSING; the approval stays pending."""
        rc, resp = self.harness.cli("approval", "request", "--category", "deploy", "--summary", "Deploy",
                                    "--operation", '{"target": "prod"}', "--project", self.project_id,
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        apr = resp["data"].get("approval_id") or resp["data"].get("id")
        self.assertTrue(apr, resp)
        op_hash = resp["data"]["op_hash"]
        spec = 'FAKE {"try_approve": {"approval": "%s", "op_hash": "%s"}}' % (apr, op_hash)
        task_id = self.create_task(spec)
        self.wait_status(task_id, "submitted")
        run_id = self.runs_of(task_id)[0]["id"]
        rc, resp = self.harness.cli("run", "log", run_id, "--stream", "stdout", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        lines = resp["data"]["streams"]["stdout"]["lines"]
        self.assertIn("rc=4", lines)
        self.assertIn(f"error_code={c.CAPABILITY_MISSING}", lines)
        self.assertEqual(self.q1("SELECT status FROM approvals WHERE id = ?;", (apr,))["status"], "pending")
        self.doctor_ok()

    # -- G18 -----------------------------------------------------------------
    def test_g18_revoked_agent_and_ended_run_tokens(self) -> None:
        """ACC: G18 - revoked agent token -> 3 TOKEN_REVOKED; a run token used after its run ended -> 3 TOKEN_REVOKED."""
        rc, resp = self.harness.cli("agent", "token", "issue", "plain", user=True)
        self.assertEqual(rc, 0, resp)
        token = resp["data"]["token"]
        cred = resp["data"].get("credential_id") or resp["data"].get("id")
        self.assertTrue(cred)
        rc, resp = self.harness.cli("agent", "token", "revoke", cred, user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("inbox", "list", token=token)
        self.assertEqual((rc, resp["error"]["code"]), (3, c.TOKEN_REVOKED))

        pid_file = os.path.join(self.harness.fixtures_dir, "g18-pids")
        spec = 'FAKE {"steps": [{"record_pid": {"path": "%s"}}, {"wait_release": {"path": "%s"}}]}' % (
            pid_file, self.gate("g18"))
        task_id = self.create_task(spec)
        run = self.wait_running_run(task_id)
        self.assertTrue(self.harness.wait_for(lambda: os.path.exists(pid_file), timeout=10.0))
        with open(pid_file, "r", encoding="utf-8") as f:
            child_pid = int(f.readline().split(",")[0])
        env = get_proc_environ(child_pid)
        self.assertIsNotNone(env)
        run_token = env["MESH_TOKEN"]
        self.release(self.gate("g18"))
        self.wait_status(task_id, "submitted")
        self.assertTrue(self.harness.wait_for(
            lambda: self.q1("SELECT status FROM runs WHERE id = ?;", (run["id"],))["status"] == "exited", timeout=10.0))
        rc, resp = self.harness.cli("task", "show", task_id, token=run_token)
        self.assertEqual((rc, resp["error"]["code"]), (3, c.TOKEN_REVOKED))
        self.doctor_ok()

    # -- C7 / C8 -------------------------------------------------------------
    def test_c8_no_token_in_argv_spec_or_run_show(self) -> None:
        """ACC: C8 - while a fake sleeps: wrapper and child cmdline, spec.json, argv_redacted_json and run show hold no token."""
        task_id = self.create_task(self.held_spec("c8"))
        run = self.wait_running_run(task_id)
        env = get_proc_environ(run["wrapper_pid"])
        self.assertIsNotNone(env)
        token = env["MESH_TOKEN"]

        def kids() -> List[int]:
            return [p for p in find_run_processes(run["id"], run["pgid"]) if p != run["wrapper_pid"]]

        self.assertTrue(self.harness.wait_for(lambda: bool(kids()), timeout=5.0))
        for pid in [run["wrapper_pid"], *kids()]:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                self.assertNotIn(token.encode(), f.read())
        with open(os.path.join(run["run_dir"], "spec.json"), "r", encoding="utf-8") as f:
            self.assertNotIn(token, f.read())
        self.assertNotIn(token, run["argv_redacted_json"])
        rc, resp = self.harness.cli("run", "show", run["id"], token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertNotIn(token, json.dumps(resp))
        self.release(self.gate("c8"))
        self.wait_status(task_id, "submitted")


class TestRun2Secrets(Run2AccBase):
    daemon_extra_env = {"FAKE_SECRET_API_KEY": FAKE_KEY}

    def test_c7_no_secret_in_home_or_outputs(self) -> None:
        """ACC: C7 - print_secrets with FAKE_SECRET_API_KEY in meshd env: run token, agent tokens, key, passphrase -> 0 hits in $H and log/event output; logs have [REDACTED."""
        task_id = self.create_task('FAKE {"steps": [{"print_secrets": {}}, {"wait_release": {"path": "%s"}}]}'
                                   % self.gate("c7"))
        run = self.wait_running_run(task_id)
        env = get_proc_environ(run["wrapper_pid"])
        self.assertIsNotNone(env)
        run_token = env["MESH_TOKEN"]
        self.release(self.gate("c7"))
        self.wait_status(task_id, "submitted")

        outputs = []
        for argv in (("task", "log", task_id), ("run", "log", run["id"]), ("event", "list", "--limit", "500")):
            rc, resp = self.harness.cli(*argv, user=True)
            self.assertEqual(rc, 0, (argv, resp))
            outputs.append(json.dumps(resp))
        stdout_log = os.path.join(run["run_dir"], "stdout.log")
        with open(stdout_log, "r", encoding="utf-8") as f:
            self.assertIn("[REDACTED", f.read())
        self.assertIn("[REDACTED", outputs[0])

        secrets_to_find = {"run token": run_token, "fake key": FAKE_KEY, "passphrase": self.harness.passphrase}
        for name, tok in self.tokens.items():
            secrets_to_find[f"agent token {name}"] = tok
        # Review m1c-run2 finding 6: a token issued with an idempotency key is not stored
        # in mesh.db, and the replay does not show it again.
        rc, resp = self.harness.cli("agent", "token", "issue", "plain", "--idempotency-key", "c7-issue", user=True)
        self.assertEqual(rc, 0, resp)
        secrets_to_find["idempotent token"] = resp["data"]["token"]
        rc, replay = self.harness.cli("agent", "token", "issue", "plain", "--idempotency-key", "c7-issue", user=True)
        self.assertEqual(rc, 0, replay)
        self.assertTrue(replay.get("idempotent_replay"), replay)
        self.assertNotEqual(replay["data"]["token"], resp["data"]["token"])
        for label, secret in secrets_to_find.items():
            self.assertTrue(secret)
            proc = subprocess.run(["grep", "-rlF", "--", secret, self.harness.home], capture_output=True, text=True)
            self.assertEqual(proc.stdout.strip(), "", f"{label} found in $H: {proc.stdout.strip()}")
            for out in outputs:
                self.assertNotIn(secret, out, label)
        self.doctor_ok()


class TestRun2SerialTiming(Run2AccBase):
    # H8 runs in the parallel phase (review m1c-run2 §2 speedup 4): its only timing clause
    # is "still one run after >= 20 s", and load can only lengthen that window.
    @idle_bound
    def test_h8_exit_nonzero_no_retry(self) -> None:
        """ACC: H8 - fail code 3 stderr boom: failed(exit_nonzero, exit_code 3); one run for 20 s; one task.failed with reason, attempt, max_attempts, stderr tail."""
        task_id = self.create_task('FAKE {"fail": {"code": 3, "stderr": "boom"}}', "--max-attempts", "3")
        self.wait_status(task_id, "failed")
        failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
        self.assertEqual(failure["reason"], "exit_nonzero")
        self.assertEqual(failure["details"]["exit_code"], 3)
        time.sleep(20)
        rc, resp = self.harness.cli("run", "list", "--task", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(len(resp["data"]["runs"]), 1)
        msgs = self.lead1_task_failed_msgs(task_id)
        self.assertEqual(len(msgs), 1)
        payload = json.loads(msgs[0]["payload_json"])
        flat = json.dumps(payload)
        self.assertIn("exit_nonzero", flat)
        self.assertIn("boom", flat)
        body = payload.get("payload", payload)
        self.assertEqual(body["reason"], "exit_nonzero", body)
        self.assertTrue(any("boom" in line for line in body["stderr_tail"]), body)
        self.assertEqual(body["attempt"], 1, body)
        self.assertEqual(body["max_attempts"], 3, body)
        self.doctor_ok()

    @serial
    def test_j1_inbox_wait_wakes_on_submit(self) -> None:
        """ACC: J1 - inbox wait [@lead1] returns rc 0 with the task.submitted message within 1 s of the event ts."""
        # Drain lead1's inbox so the wait blocks until the new message.
        rc, resp = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        ids = [it["id"] for it in resp["data"]["items"]]
        if ids:
            rc, resp = self.harness.cli("inbox", "ack", *ids, token=self.tok_lead1)
            self.assertEqual(rc, 0, resp)

        self.harness.hold_launch_starts()
        task_id = self.create_task('FAKE {"succeed": {}}')
        # task.assigned/queued messages may reach lead1 too; ack them before waiting.
        rc, resp = self.harness.cli("inbox", "list", token=self.tok_lead1)
        ids = [it["id"] for it in resp["data"]["items"]]
        if ids:
            self.harness.cli("inbox", "ack", *ids, token=self.tok_lead1)

        def run_wait() -> Tuple[int, Dict[str, Any], float]:
            rc_w, resp_w = self.harness.cli("inbox", "wait", "--timeout", "30s", token=self.tok_lead1, timeout=40.0)
            return rc_w, resp_w, time.time()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(run_wait)
            self.assertTrue(self.harness.wait_for_active_waits(1), "inbox wait never registered")
            self.harness.release_launch_starts()
            self.harness.cli("daemon", "status")
            rc_w, resp_w, returned_at = fut.result(timeout=45.0)
        self.assertEqual(rc_w, 0, resp_w)
        items = resp_w["data"]["items"]
        self.assertEqual([it["type"] for it in items], ["task.submitted"])
        evt = self.q1("SELECT ts FROM events WHERE task_id = ? AND type = 'task.submitted';", (task_id,))
        self.assertLessEqual(returned_at - _ts(evt["ts"]), 1.0)

    @serial
    def test_j9_event_tail_live_ndjson(self) -> None:
        """ACC: J9 - event tail --project [U] while a task runs: NDJSON, one event per line, live, in seq order."""
        since = self.q1("SELECT MAX(seq) AS s FROM events;")["s"]
        pass_fd = os.open(self.harness.passphrase_file, os.O_RDONLY)
        try:
            proc = subprocess.Popen(
                [self.harness.bin_mesh, "--home", self.harness.home, "--user", "--passphrase-fd", str(pass_fd),
                 "event", "tail", "--project", self.project_id, "--since", str(since), "--timeout", "15s"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.harness.get_env(),
                pass_fds=(pass_fd,),
            )
        finally:
            os.close(pass_fd)
        try:
            time.sleep(0.5)  # event tail has no wait counter; --since covers any start-up gap
            task_id = self.create_task('FAKE {"steps": [{"sleep": {"seconds": 1}}, {"succeed": {}}]}')
            arrivals: List[Tuple[float, Dict[str, Any]]] = []
            deadline = time.monotonic() + 14
            assert proc.stdout is not None
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                arrivals.append((time.time(), json.loads(line)))
                if arrivals[-1][1].get("type") == "task.submitted":
                    break
        finally:
            proc.kill()
            proc.wait(timeout=10)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        types = [e.get("type") for _, e in arrivals]
        for t in ("task.queued", "task.started", "task.submitted"):
            self.assertIn(t, types)
        seqs = [e["seq"] for _, e in arrivals]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        for arrived, e in arrivals:
            if e.get("task_id") == task_id:
                self.assertLess(arrived - _ts(e["ts"]), 2.0, e)


if __name__ == "__main__":
    unittest.main()
