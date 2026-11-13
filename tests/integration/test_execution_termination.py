"""M1c run 3 acceptance tests: termination (plan §11.3, §11.4), cancel, post-submit
grace, leftovers, project pause/cancel and Python isolation, with the fake adapter.

Fixture: checklist F1 (see test_execution_acc_run2.Run2AccBase).
ACC: C15 E17 F8 H1 H2 H3 H5 H16 H17 A8
"""

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Set

import mesh.constants as c
from mesh.core.clock import parse_utc
from mesh.core.proc import get_proc_environ, get_proc_stat
from tests.helpers import assert_h5, idle_bound
from tests.integration.test_execution_acc_run2 import Run2AccBase


def _alive(pid: int) -> bool:
    info = get_proc_stat(pid)
    return info is not None and info["state"] != "Z"


def _ts(value: Optional[str]) -> float:
    return parse_utc(value).timestamp() if value else 0.0


def procs_with_run_id(run_id: str) -> List[int]:
    """Live processes whose environ carries MESH_RUN_ID=<run_id> (zombies excluded)."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            env = get_proc_environ(pid)
        except OSError:
            continue
        if env and env.get("MESH_RUN_ID") == run_id and _alive(pid):
            found.append(pid)
    return found


class PidWatcher:
    """Background poller: last time each pid was seen alive (time.time())."""

    def __init__(self) -> None:
        self.pids: Set[int] = set()
        self.last_alive: Dict[int, float] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def add(self, pid: int) -> None:
        self.pids.add(pid)

    def _loop(self) -> None:
        while not self._stop.is_set():
            for pid in list(self.pids):
                now = time.time()
                if _alive(pid):
                    self.last_alive[pid] = now
            time.sleep(0.02)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


class TerminationBase(Run2AccBase):
    def setUp(self) -> None:
        super().setUp()
        self.watcher = PidWatcher()

    def tearDown(self) -> None:
        self.watcher.stop()
        super().tearDown()

    def set_config(self, key: str, value: Any) -> None:
        rc, resp = self.harness.cli("config", "set", key, json.dumps(value), user=True)
        self.assertEqual(rc, 0, resp)

    def pid_file(self, name: str) -> str:
        return os.path.join(self.harness.fixtures_dir, name)

    def recorded_pids(self, path: str, minimum: int = 1, timeout: float = 15.0) -> List[int]:
        def read() -> List[int]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return [int(line.split(",")[0]) for line in f if line.strip()]
            except OSError:
                return []

        self.assertTrue(self.harness.wait_for(lambda: len(read()) >= minimum, timeout=timeout), f"no pids in {path}")
        pids = read()
        for pid in pids:
            self.watcher.add(pid)
        return pids

    def event(self, task_id: str, etype: str) -> Any:
        return self.q1("SELECT * FROM events WHERE task_id = ? AND type = ? ORDER BY seq DESC LIMIT 1;",
                       (task_id, etype))

    def transitions(self, task_id: str) -> List[str]:
        return [r["to_state"] for r in self.q(
            "SELECT to_state FROM events WHERE task_id = ? AND to_state IS NOT NULL ORDER BY seq;", (task_id,))]

    def lease_of(self, task_id: str) -> Any:
        return self.q1("SELECT * FROM leases WHERE key = ?;", (f"task:{task_id}",))

    def cancel(self, task_id: str, reason: str = "r") -> Dict[str, Any]:
        rc, resp = self.harness.cli("task", "cancel", task_id, "--reason", reason, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return resp

    def assert_h5(self) -> None:
        """ACC: H5 - never two active runs per task; lease fence = task fence_seq = run fence."""
        assert_h5(self, self.harness)


class TestTermination(TerminationBase):
    def test_h1_timeout_kills_before_failed(self) -> None:
        """ACC: H1, H5 - --timeout 2s: failed(timeout), run killed(timeout), pids dead before task.failed ts, one task.failed."""
        path = self.pid_file("h1-pids")
        spec = 'FAKE {"steps": [{"record_pid": {"path": "%s"}}, {"sleep": {"seconds": 60}}]}' % path
        task_id = self.create_task(spec, "--timeout", "2s")
        pids = self.recorded_pids(path)
        self.assert_h5()
        self.wait_status(task_id, "failed", timeout=30.0)
        failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
        self.assertEqual(failure["reason"], "timeout")
        run = self.runs_of(task_id)[0]
        self.assertEqual((run["status"], run["end_reason"]), ("killed", "timeout"))
        failed_ts = _ts(self.event(task_id, "task.failed")["ts"])
        for pid in pids:
            self.assertFalse(_alive(pid), pid)
            self.assertLess(self.watcher.last_alive.get(pid, 0.0), failed_ts)
        self.assertEqual(len(self.lead1_task_failed_msgs(task_id)), 1)
        self.assertIsNone(self.lease_of(task_id))
        self.assert_h5()
        self.doctor_ok()

    def test_h2_cancel_ignore_sigterm(self) -> None:
        """ACC: H2, A8 - ignore_sigterm + cancel: cancelling then cancelled within kill.grace_s + 10 s; nothing of the group remains; doctor rc 0 while cancelling."""
        self.set_config("kill.grace_s", 2)
        task_id = self.create_task('FAKE {"ignore_sigterm": {}}')
        run = self.wait_running_run(task_id)
        self.assertTrue(self.harness.wait_for(lambda: len(procs_with_run_id(run["id"])) >= 2, timeout=10.0))
        t0 = time.monotonic()
        resp = self.cancel(task_id)
        self.assertEqual(resp["data"]["status"], "cancelling")
        self.assertEqual(self.status(task_id), "cancelling")
        self.assertIsNotNone(self.lease_of(task_id))
        self.assert_h5()
        self.doctor_ok()  # A8 while cancelling
        self.wait_status(task_id, "cancelled", timeout=2 + 10 + 2)
        self.assertLessEqual(time.monotonic() - t0, 2 + 10)
        self.assertEqual(procs_with_run_id(run["id"]), [])
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                info = get_proc_stat(int(entry))
                if info and info["state"] != "Z":
                    self.assertNotIn(run["pgid"], (info["pgrp"], info["session"]), entry)
        run = self.runs_of(task_id)[0]
        self.assertEqual((run["status"], run["end_reason"]), ("killed", "cancelled"))
        states = self.transitions(task_id)
        self.assertEqual(states[-2:], ["cancelling", "cancelled"])
        self.assertIsNone(self.lease_of(task_id))
        self.doctor_ok()

    def test_h3_cancel_grandchildren(self) -> None:
        """ACC: H3 - cancel spawn_grandchild and spawn_setsid_grandchild tasks: no live process carries either run id after cancelled."""
        t1 = self.create_task('FAKE {"steps": [{"spawn_grandchild": {}}, {"sleep": {"seconds": 60}}]}', title="G")
        t2 = self.create_task('FAKE {"steps": [{"spawn_setsid_grandchild": {}}, {"sleep": {"seconds": 60}}]}', title="S")
        runs = {}
        for t in (t1, t2):
            runs[t] = self.wait_running_run(t)["id"]
            self.assertTrue(self.harness.wait_for(lambda r=runs[t]: len(procs_with_run_id(r)) >= 3, timeout=10.0),
                            "grandchild never appeared")
        for t in (t1, t2):
            self.cancel(t)
        for t in (t1, t2):
            self.wait_status(t, "cancelled", timeout=30.0)
            self.assertEqual(procs_with_run_id(runs[t]), [])
        self.assert_h5()
        self.doctor_ok()

    def test_h16_cancel_during_post_submit_grace(self) -> None:
        """ACC: H16, A8 - post_submit_grace 60: submit then cancel → submitted→cancelling→cancelled; pids dead before the cancelled ts."""
        self.set_config("kill.post_submit_grace_s", 60)
        path = self.pid_file("h16-pids")
        spec = ('FAKE {"steps": [{"record_pid": {"path": "%s"}}, {"submit_via_cli": {}}, '
                '{"sleep": {"seconds": 30}}]}' % path)
        task_id = self.create_task(spec)
        pids = self.recorded_pids(path)
        self.wait_status(task_id, "submitted")
        self.assertIsNotNone(self.lease_of(task_id))
        self.doctor_ok()  # A8 during post-submit grace
        self.cancel(task_id)
        self.wait_status(task_id, "cancelled", timeout=30.0)
        states = self.transitions(task_id)
        self.assertEqual(states[-3:], ["submitted", "cancelling", "cancelled"])
        cancelled_ts = _ts(self.event(task_id, "task.cancelled")["ts"])
        for pid in pids:
            self.assertFalse(_alive(pid))
            self.assertLess(self.watcher.last_alive.get(pid, 0.0), cancelled_ts)
        run = self.runs_of(task_id)[0]
        self.assertEqual((run["status"], run["end_reason"]), ("killed", "cancelled"))
        self.doctor_ok()

    def test_f8_accept_blocked_until_post_submit_grace_kill(self) -> None:
        """ACC: F8, A8 - accept right after submit → 6 RUN_STILL_ACTIVE; after the post-submit grace kill → 0."""
        self.set_config("kill.post_submit_grace_s", 2)
        task_id = self.create_task('FAKE {"steps": [{"submit_via_cli": {}}, {"sleep": {"seconds": 10}}]}')
        self.wait_status(task_id, "submitted")
        rc, resp = self.harness.cli("task", "accept", task_id, token=self.tok_lead1)
        self.assertEqual((rc, resp["error"]["code"]), (6, c.RUN_STILL_ACTIVE))
        self.assertEqual(self.status(task_id), "submitted")
        self.doctor_ok()  # A8 during post-submit grace
        self.assertTrue(self.harness.wait_for(lambda: self.lease_of(task_id) is None, timeout=20.0))
        run = self.runs_of(task_id)[0]
        self.assertEqual((run["status"], run["end_reason"]), ("killed", "post_submit_grace"))
        rc, resp = self.harness.cli("task", "accept", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.status(task_id), "accepted")
        self.doctor_ok()

    def test_h17_leftover_escapee_after_exit_zero(self) -> None:
        """ACC: H17 - exit 0 with a setsid escapee: auto-submitted (system), run.leftovers_terminated lists the pid, accept 6 while the lease is held, no run-id process once the lease is gone."""
        self.set_config("kill.grace_s", 2)
        task_id = self.create_task(
            'FAKE {"steps": [{"spawn_setsid_grandchild": {"ignore_sigterm": true}}, {"succeed": {}}]}')
        run_id = self.wait_running_run(task_id)["id"]
        accepted_while_held = None
        released_clean = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            lease = self.lease_of(task_id)
            if lease is None:
                released_clean = procs_with_run_id(run_id) == []
                break
            if accepted_while_held is None and self.status(task_id) == "submitted":
                rc, resp = self.harness.cli("task", "accept", task_id, token=self.tok_lead1)
                if self.lease_of(task_id) is not None:
                    accepted_while_held = (rc, (resp.get("error") or {}).get("code"))
            time.sleep(0.05)
        self.assertEqual(accepted_while_held, (6, c.RUN_STILL_ACTIVE))
        self.assertTrue(released_clean, "a process still carried the run id when the lease was released")
        self.assertEqual(self.status(task_id), "submitted")
        record = json.loads(self.q1("SELECT deliverable_json FROM tasks WHERE id = ?;", (task_id,))["deliverable_json"])
        self.assertEqual(record["submitted_by"], "system")
        evt = self.event(task_id, "run.leftovers_terminated")
        self.assertIsNotNone(evt)
        self.assertGreaterEqual(len(json.loads(evt["payload_json"])["pids"]), 1)
        run = self.runs_of(task_id)[0]
        self.assertEqual((run["status"], run["exit_code"]), ("exited", 0))
        self.doctor_ok()

    def test_h5_no_overlap_across_retry(self) -> None:
        """ACC: H5 - timeout then retry: attempt 1 pid dead before attempt 2 starts; one active run; fences agree."""
        path = self.pid_file("h5-pids")
        spec = 'FAKE {"steps": [{"record_pid": {"path": "%s"}}, {"sleep": {"seconds": 60}}]}' % path
        task_id = self.create_task(spec, "--timeout", "1s")
        first = self.recorded_pids(path, 1)[0]
        self.wait_status(task_id, "failed", timeout=30.0)
        rc, resp = self.harness.cli("task", "retry", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        second = self.recorded_pids(path, 2)[1]
        self.assert_h5()
        with open(path, "r", encoding="utf-8") as f:
            starts = [float(line.split(",")[1]) for line in f if line.strip()]
        self.assertLess(self.watcher.last_alive.get(first, 0.0), starts[1])
        self.assertNotEqual(first, second)
        self.wait_status(task_id, "failed", timeout=30.0)
        runs = self.runs_of(task_id)
        self.assertEqual([r["fence"] for r in runs], [1, 2])
        self.assertLess(_ts(runs[0]["ended_at"]), _ts(runs[1]["started_at"]) + 1e-6)
        self.assert_h5()
        self.doctor_ok()

    def test_c15_python_isolation_flags(self) -> None:
        """ACC: C15 - shadow mesh/ in the workspace: wrapper cmdline has -P -s -m mesh.runwrap, shadow file never written, attempt 2 fails exit_nonzero."""
        shadow = self.pid_file("acc-shadow")
        # Attempt 2 must still be running when its cmdline is read: it waits for a release
        # file (absent then) instead of a 2 s sleep that can end first on a loaded host.
        # Attempt 1 finds the file already there and fails at once.
        release = self.pid_file("acc-c15-release")
        open(release, "w").close()
        spec = ('FAKE {"steps": [{"write_file": {"path": "mesh/__init__.py", "content": ""}}, '
                '{"write_file": {"path": "mesh/runwrap.py", "content": "open(\'%s\',\'w\')"}}, '
                '{"wait_release": {"path": "%s"}}, {"fail": {}}]}' % (shadow, release))
        task_id = self.create_task(spec)
        self.wait_status(task_id, "failed", timeout=30.0)
        os.unlink(release)
        rc, resp = self.harness.cli("task", "retry", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(self.harness.wait_for(
            lambda: any(r["status"] == "running" for r in self.runs_of(task_id)[1:]), timeout=15.0))
        run2 = self.runs_of(task_id)[1]
        with open(f"/proc/{run2['wrapper_pid']}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode()
        self.assertIn("-P -s -m mesh.runwrap", cmdline)
        open(release, "w").close()
        self.wait_status(task_id, "failed", timeout=30.0)
        self.assertFalse(os.path.exists(shadow))
        failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
        self.assertEqual(failure["reason"], "exit_nonzero")
        self.assertEqual(self.runs_of(task_id)[1]["status"], "exited")
        self.doctor_ok()


class TestProjectControl(TerminationBase):
    def test_e17_pause_resume_and_cancel_running(self) -> None:
        """ACC: E17 - paused: queued with project_paused; resume starts it; cancel --cancel-running: tasks cancelled (running via cancelling), approvals cancelled, project cancelled."""
        self.harness.hold_launch_starts()
        queued = self.create_task('FAKE {"sleep": {"seconds": 60}}', title="Q")
        rc, resp = self.harness.cli("project", "pause", self.project_id, user=True)
        self.assertEqual(rc, 0, resp)
        self.harness.release_launch_starts()
        time.sleep(1.2)  # at least one scheduler tick with the hold released
        rc, resp = self.harness.cli("task", "show", queued, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["status"], "queued")
        self.assertEqual(resp["data"]["blocked_reason"], "project_paused")
        self.assertEqual(self.runs_of(queued), [])
        rc, resp = self.harness.cli("project", "resume", self.project_id, user=True)
        self.assertEqual(rc, 0, resp)
        run = self.wait_running_run(queued)

        other = self.create_task('FAKE {"succeed": {}}', "--depends-on", queued, title="DEP")
        rc, resp = self.harness.cli("approval", "request", "--category", "deploy", "--summary", "Deploy",
                                    "--operation", '{"target": "prod"}', "--project", self.project_id,
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        apr = resp["data"].get("approval_id") or resp["data"].get("id")
        rc, resp = self.harness.cli("project", "cancel", self.project_id, "--cancel-running", user=True)
        self.assertEqual(rc, 0, resp)
        # The fake dies on SIGTERM within one 200 ms step, so `cancelling` may already be
        # over here; the event trail proves the route (A8 while cancelling: H2).
        self.wait_status(queued, "cancelled", timeout=30.0)
        self.assertEqual(self.transitions(queued)[-2:], ["cancelling", "cancelled"])
        self.assertEqual(self.status(other), "cancelled")
        self.assertEqual(self.q1("SELECT status FROM approvals WHERE id = ?;", (apr,))["status"], "cancelled")
        self.assertEqual(self.q1("SELECT status FROM projects WHERE id = ?;", (self.project_id,))["status"],
                         "cancelled")
        run = self.runs_of(queued)[0]
        self.assertEqual((run["status"], run["end_reason"]), ("killed", "project_cancelled"))
        self.assertEqual(procs_with_run_id(run["id"]), [])
        live = self.q("SELECT id, status FROM tasks WHERE project_id = ? AND status NOT IN ('cancelled', 'merged');",
                      (self.project_id,))
        self.assertEqual([dict(r) for r in live], [])
        self.doctor_ok()

    def test_e17_inbox_no_response_clock_paused(self) -> None:
        """ACC: E17 (reading) - an inbox task is not failed no_response while its project is paused; the clock restarts on resume."""
        self.set_config("inbox.ack_timeout_s", 2)
        task_id = self.create_task("inbox work", assignee="ibx")
        self.assertTrue(self.harness.wait_for(lambda: self.status(task_id) == "queued", timeout=10.0))
        rc, resp = self.harness.cli("project", "pause", self.project_id, user=True)
        self.assertEqual(rc, 0, resp)
        time.sleep(3.5)
        self.assertEqual(self.status(task_id), "queued")
        rc, resp = self.harness.cli("project", "resume", self.project_id, user=True)
        self.assertEqual(rc, 0, resp)
        self.wait_status(task_id, "failed", timeout=15.0)
        failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
        self.assertEqual(failure["reason"], "no_response")
        # Daemon clock on both sides (load-proof): the ack window restarts at the last
        # scheduler tick while paused (at most one ~1 s tick before the project.resumed
        # event), so with a 2 s window task.failed comes >= 1 s after project.resumed. Had
        # the clock run during the 3.5 s pause, it would fail at once on resume.
        resumed = _ts(self.q("SELECT ts FROM events WHERE type = 'project.resumed' AND project_id = ? "
                             "ORDER BY seq DESC LIMIT 1;", (self.project_id,))[0]["ts"])
        self.assertGreaterEqual(_ts(self.event(task_id, "task.failed")["ts"]), resumed + 1.0)
        self.doctor_ok()


    def test_e17_inbox_no_response_clock_paused_at_concurrency_limit(self) -> None:
        """ACC: E17 (reading, concurrency_limit branch) - a queued inbox task is not failed no_response while its agent's slot is taken; the clock restarts when the slot frees."""
        # Keep the default ack window for setup: a loaded CLI can take longer than
        # 2 s to start the first task. Only the blocked second task needs a short window.
        first = self.create_task("inbox one", assignee="ibx", title="ONE")
        self.assertTrue(self.harness.wait_for(lambda: self.status(first) == "queued", timeout=10.0))
        rc, resp = self.harness.cli("task", "start", first, token=self.tokens["ibx"])
        self.assertEqual(rc, 0, resp)
        run1, fence1 = resp["data"]["run_id"], resp["data"]["fence"]
        self.set_config("inbox.ack_timeout_s", 2)
        second = self.create_task("inbox two", assignee="ibx", title="TWO")
        self.assertTrue(self.harness.wait_for(lambda: self.status(second) == "queued", timeout=10.0))
        rc, resp = self.harness.cli("task", "show", second, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"].get("blocked_reason"), "concurrency_limit", resp["data"])
        # Observe scheduler ticks refreshing the blocked offer past a full ack window,
        # not merely wall time passing while the scheduler might not have run.
        queued_at = _ts(self.event(second, "task.queued")["ts"])
        self.assertTrue(self.harness.wait_for(
            lambda: _ts(self.q1("SELECT offered_at FROM tasks WHERE id = ?;", (second,))["offered_at"])
            >= queued_at + 3.5, timeout=15.0), "blocked inbox ack clock did not refresh")
        self.assertEqual(self.status(second), "queued")
        rc, resp = self.harness.cli("task", "submit", first, "--summary", "done", "--run", run1,
                                    "--fence", str(fence1), token=self.tokens["ibx"])
        self.assertEqual(rc, 0, resp)
        self.wait_status(second, "failed", timeout=15.0)
        failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (second,))["failure_json"])
        self.assertEqual(failure["reason"], "no_response")
        # Daemon clock on both sides (load-proof): the slot frees at the task.submitted event.
        freed = _ts(self.event(first, "task.submitted")["ts"])
        self.assertGreaterEqual(_ts(self.event(second, "task.failed")["ts"]), freed + 1.0)
        self.doctor_ok()

    def test_project_cancel_ends_started_inbox_run_project_cancelled(self) -> None:
        """ACC: E17 (inbox case, review m1c-run3 #5) - project cancel with a started inbox task: task cancelled, external run killed(project_cancelled), lease gone, fence invalid."""
        task_id = self.create_task("inbox work", assignee="ibx")
        self.assertTrue(self.harness.wait_for(lambda: self.status(task_id) == "queued", timeout=10.0))
        rc, resp = self.harness.cli("task", "start", task_id, token=self.tokens["ibx"])
        self.assertEqual(rc, 0, resp)
        run_id, fence = resp["data"]["run_id"], resp["data"]["fence"]
        rc, resp = self.harness.cli("project", "cancel", self.project_id, user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.status(task_id), "cancelled")
        run = self.runs_of(task_id)[-1]
        self.assertEqual((run["status"], run["end_reason"]), ("killed", "project_cancelled"))
        self.assertIsNone(self.lease_of(task_id))
        rc, resp = self.harness.cli("task", "submit", task_id, "--summary", "late", "--run", run_id,
                                    "--fence", str(fence), token=self.tokens["ibx"])
        self.assertNotEqual(rc, 0, resp)
        self.doctor_ok()


class TestKillFailed(TerminationBase):
    """Review m1c-run3 #6: the kill_failed path driven through the real supervisor code on
    real rows. meshd is stopped (runs keep running, I8); the test process then runs the
    supervisor's termination functions with signals made ineffective and a fake clock."""

    def test_kill_failed_retry_then_dead(self) -> None:
        """kill_failed once (one run.kill_failed, user + origin messages), lease kept, SIGKILL retried after 30 s, listed in kill_failed_runs, then failed(kill_failed_then_dead)."""
        from unittest import mock

        from mesh.core.db import connect
        from mesh.daemon import children, supervisor, terminate

        task_id = self.create_task('FAKE {"steps": [{"record_pid": {}}, {"sleep": {"seconds": 120}}]}')
        run = self.wait_running_run(task_id)
        run_dir = run["run_dir"]
        self.assertTrue(self.harness.wait_for(
            lambda: os.path.exists(os.path.join(run_dir, "wrapper.json")) and len(procs_with_run_id(run["id"])) >= 2,
            timeout=15.0))
        self.harness.stop_daemon()
        conn = connect(os.path.join(self.harness.home, "mesh.db"))
        clock = [1000.0]
        kills: List[int] = []

        def no_signal(pid: int, starttime: int, signo: int) -> bool:
            if signo == 9:
                kills.append(pid)
            return False

        try:
            run = conn.execute("SELECT * FROM runs WHERE id = ?;", (run["id"],)).fetchone()
            with mock.patch.object(terminate, "signal_if_same", no_signal), \
                    mock.patch.object(terminate.time, "monotonic", lambda: clock[0]), \
                    mock.patch.object(children, "reap_orphans", lambda: []):
                t = terminate.begin(run["id"], run["pgid"], run["wrapper_pid"], "timeout", 1,
                                    wrapper_starttime=run["proc_starttime"])
                self.assertGreaterEqual(len(t.initial_pids), 2, t.initial_pids)
                # `started` came from the dataclass default factory (the real clock).
                t.started, t.deadline = clock[0], clock[0] + t.grace_s
                clock[0] += 2
                self.assertIsNone(supervisor._advance(conn, self.harness.home, run, t))  # grace over: SIGKILL
                self.assertTrue(kills)
                clock[0] += terminate.KILL_WAIT_S + 1
                self.assertIsNone(supervisor._advance(conn, self.harness.home, run, t))  # kill_failed
                for _ in range(3):
                    clock[0] += 1
                    self.assertIsNone(supervisor._advance(conn, self.harness.home, run, t))
                evts = conn.execute("SELECT * FROM events WHERE type = 'run.kill_failed' AND run_id = ?;",
                                    (run["id"],)).fetchall()
                self.assertEqual(len(evts), 1)
                user = conn.execute("SELECT id FROM principals WHERE kind = 'user';").fetchone()["id"]
                origin = conn.execute("SELECT origin_principal_id FROM projects WHERE id = ?;",
                                      (self.project_id,)).fetchone()["origin_principal_id"]
                recipients = {r["recipient_id"] for r in conn.execute(
                    "SELECT recipient_id FROM outbox WHERE type = 'run.kill_failed' AND event_seq = ?;",
                    (evts[0]["seq"],)).fetchall()}
                self.assertEqual(recipients, {user, origin} - {None})
                self.assertIn(user, recipients)
                self.assertIn(run["id"], [k["run_id"] for k in terminate.kill_failed_runs()])
                self.assertIsNotNone(conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)).fetchone())
                self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id = ?;", (task_id,)).fetchone()["status"], "running")
                before = len(kills)
                clock[0] += terminate.KILL_RETRY_S
                self.assertIsNone(supervisor._advance(conn, self.harness.home, run, t))
                self.assertGreater(len(kills), before, "SIGKILL not retried after 30 s")
                # The processes finally die.
                for pid in procs_with_run_id(run["id"]):
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
                self.assertTrue(self.harness.wait_for(lambda: procs_with_run_id(run["id"]) == [], timeout=10.0))
                clock[0] += 1
                # step() needs two empty scans a poll apart before "done".
                self.assertIsNone(supervisor._advance(conn, self.harness.home, run, t))
                clock[0] += terminate.POLL_S
                self.assertEqual(supervisor._advance(conn, self.harness.home, run, t), "killed")
            task = conn.execute("SELECT status, failure_json FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            self.assertEqual(task["status"], "failed")
            failure = json.loads(task["failure_json"])
            self.assertEqual(failure["reason"], "kill_failed_then_dead")
            self.assertEqual(failure["details"]["original_reason"], "timeout")
            self.assertNotIn(run["id"], [k["run_id"] for k in terminate.kill_failed_runs()])
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM events WHERE type = 'run.kill_failed' AND run_id = ?;",
                                          (run["id"],)).fetchone()["n"], 1)
        finally:
            terminate.forget(run["id"])
            conn.close()
        self.harness.restart_daemon()
        self.doctor_ok()


class TestCancelSparesUnrelatedChildren(TerminationBase):
    # Checklist value: the stub starts with meshd, so most of its 30 s overlaps setUp.
    daemon_extra_env = {"MESH_TEST_STUB_CHILD_S": "30"}

    @idle_bound
    def test_cancel_does_not_kill_unrelated_children(self) -> None:
        """ACC: H13 - the registered stub survives a cancel and exits 0; no run-id process remains; no zombie of meshd."""
        run_dir = os.path.join(self.harness.home, "run")
        stub_pid_path = os.path.join(run_dir, "stub.pid")
        self.assertTrue(self.harness.wait_for(lambda: os.path.exists(stub_pid_path), timeout=10.0))
        with open(stub_pid_path, "r", encoding="utf-8") as f:
            stub_pid = int(f.read().strip())
        with open(os.path.join(run_dir, "meshd.pid"), "r", encoding="utf-8") as f:
            meshd_pid = int(f.read().strip())
        task_id = self.create_task('FAKE {"steps": [{"spawn_setsid_grandchild": {}}, {"sleep": {"seconds": 60}}]}')
        run_id = self.wait_running_run(task_id)["id"]
        self.assertTrue(self.harness.wait_for(lambda: len(procs_with_run_id(run_id)) >= 3, timeout=10.0))
        self.cancel(task_id)
        self.wait_status(task_id, "cancelled", timeout=30.0)
        self.assertTrue(_alive(stub_pid), "the stub was killed by the cancel")
        self.assertEqual(procs_with_run_id(run_id), [])
        rc_path = os.path.join(run_dir, "stub.rc")
        self.assertTrue(self.harness.wait_for(lambda: os.path.exists(rc_path), timeout=40.0))
        with open(rc_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "0")

        def zombies() -> List[int]:
            found = []
            for entry in os.listdir("/proc"):
                if entry.isdigit():
                    info = get_proc_stat(int(entry))
                    if info and info["state"] == "Z" and info["ppid"] == meshd_pid:
                        found.append(int(entry))
            return found

        self.assertTrue(self.harness.wait_for(lambda: zombies() == [], timeout=10.0), zombies())
        self.doctor_ok()


class TestCredentialInRun(TerminationBase):
    def stdout_of(self, task_id: str) -> str:
        run = self.runs_of(task_id)[-1]
        with open(os.path.join(run["run_dir"], "stdout.log"), "r", encoding="utf-8") as f:
            return f.read()

    def write_token(self, name: str) -> str:
        path = os.path.join(self.harness.fixtures_dir, f"{name}.tok")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.tokens[name] + "\n")
        return path

    def wait_ended(self, task_id: str) -> None:
        self.assertTrue(self.harness.wait_for(
            lambda: bool(self.runs_of(task_id)) and self.runs_of(task_id)[-1]["status"] not in ("starting", "running"),
            timeout=30.0))

    def test_g25_g26_stolen_tokens_rejected(self) -> None:
        """ACC: G25, G26 - another agent's token inside a run → rc 4 CREDENTIAL_NOT_ALLOWED_IN_RUN; approval pending, task submitted; one event; one user message."""
        rc, resp = self.harness.cli("approval", "request", "--category", "deploy", "--summary", "Deploy",
                                    "--operation", '{"target": "prod"}', "--project", self.project_id,
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        apr, op_hash = resp["data"]["approval_id"], resp["data"]["op_hash"]
        grok_tok = self.write_token("grok")
        args = ["approval", "approve", apr, "--op-hash", op_hash, "--relayed", "--user-statement", "ok"]
        t25 = self.create_task('FAKE {"steal_token": %s}' % json.dumps({"path": grok_tok, "args": args}), title="G25")
        self.wait_ended(t25)
        out = self.stdout_of(t25)
        self.assertIn("rc=4", out)
        self.assertIn(f"error_code={c.CREDENTIAL_NOT_ALLOWED_IN_RUN}", out)
        self.assertEqual(self.q1("SELECT status FROM approvals WHERE id = ?;", (apr,))["status"], "pending")
        run_id = self.runs_of(t25)[-1]["id"]
        evts = self.q("SELECT * FROM events WHERE type = 'auth.credential_in_run' AND run_id = ?;", (run_id,))
        self.assertEqual(len(evts), 1)
        self.assertNotIn(self.tokens["grok"], evts[0]["payload_json"])
        self.assertNotIn(self.tokens["grok"].split(".")[-1], evts[0]["payload_json"])
        user = self.q1("SELECT id FROM principals WHERE kind = 'user';")["id"]
        msgs = self.q("SELECT * FROM outbox WHERE recipient_id = ? AND type = 'security.credential_in_run' "
                      "AND dedup_key LIKE ?;", (user, f"cred_in_run:{run_id}:%"))
        self.assertEqual(len(msgs), 1)

        target = self.create_task('FAKE {"succeed": {}}', title="SUB")
        self.wait_status(target, "submitted")
        self.assertTrue(self.harness.wait_for(lambda: self.lease_of(target) is None, timeout=15.0))
        lead_tok = self.write_token("lead1")
        t26 = self.create_task('FAKE {"steal_token": %s}' % json.dumps(
            {"path": lead_tok, "args": ["task", "accept", target]}), title="G26")
        self.wait_ended(t26)
        out = self.stdout_of(t26)
        self.assertIn("rc=4", out)
        self.assertIn(f"error_code={c.CREDENTIAL_NOT_ALLOWED_IN_RUN}", out)
        self.assertEqual(self.status(target), "submitted")
        self.doctor_ok()

    def test_g27_bad_passphrase_in_run_no_lockout(self) -> None:
        """ACC: G27 - six wrong passphrases inside a run → six rc 4; auth_failures unchanged; whoami as user rc 0."""
        before = self.q1("SELECT COUNT(*) AS n FROM auth_failures;")["n"]
        task_id = self.create_task('FAKE {"bad_passphrase": {"count": 6}}')
        self.wait_ended(task_id)
        out = self.stdout_of(task_id)
        self.assertEqual(out.count("rc=4"), 6, out)
        self.assertEqual(out.count(f"error_code={c.CREDENTIAL_NOT_ALLOWED_IN_RUN}"), 6, out)
        self.assertEqual(self.q1("SELECT COUNT(*) AS n FROM auth_failures;")["n"], before)
        # Plan step 4 dedup (review m1c-run3 #6): six events, one user message per run.
        run_id = self.runs_of(task_id)[-1]["id"]
        evts = self.q("SELECT * FROM events WHERE type = 'auth.credential_in_run' AND run_id = ?;", (run_id,))
        self.assertEqual(len(evts), 6)
        user = self.q1("SELECT id FROM principals WHERE kind = 'user';")["id"]
        msgs = self.q("SELECT * FROM outbox WHERE recipient_id = ? AND type = 'security.credential_in_run' "
                      "AND dedup_key LIKE ?;", (user, f"cred_in_run:{run_id}:%"))
        self.assertEqual(len(msgs), 1)
        rc, resp = self.harness.cli("whoami", user=True)
        self.assertEqual(rc, 0, resp)
        self.doctor_ok()
