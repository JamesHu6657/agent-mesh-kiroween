"""Crash/recovery tier (plan §17.4 R1–R7, R9, R10; checklist I1–I8, I10, I11, J3, J5).

Every test kills or stops meshd while work is in flight and checks what the
next boot's recovery (§11.7) does with it. meshd is killed with SIGKILL through
``MeshHarness.kill9_daemon`` (the pid is trusted only if its environ carries this
home's MESH_HOME) and restarted with ``restart_daemon`` (which waits for the
socket; recovery runs before the socket opens).

Runs are held with the fake's ``wait_release`` gate rather than sleeps, so the
assertions do not depend on host load.
"""

import json
import os
import signal
import sqlite3
import threading
import time
import unittest
from typing import Any, Dict, List

from mesh.core.proc import get_proc_environ, get_proc_stat
from tests.integration.test_execution_acc_run2 import Run2AccBase


def _alive(pid: int) -> bool:
    info = get_proc_stat(pid)
    return info is not None and info.get("state") != "Z"


def _carriers(run_id: str) -> List[int]:
    """Live pids whose environ carries MESH_RUN_ID=<run_id>."""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            env = get_proc_environ(pid)
        except OSError:
            continue
        if env and env.get("MESH_RUN_ID") == run_id and _alive(pid):
            out.append(pid)
    return out


def _group_members(pgid: int) -> List[int]:
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            info = get_proc_stat(int(entry))
        except OSError:
            continue
        if info and info.get("state") != "Z" and (info.get("pgrp") == pgid or info.get("session") == pgid):
            out.append(int(entry))
    return out


class RecoveryBase(Run2AccBase):
    def pid_lines(self, path: str) -> List[str]:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]

    def events(self, type_: str, task_id: str = None) -> List[Any]:
        if task_id is None:
            return self.q("SELECT * FROM events WHERE type = ? ORDER BY seq;", (type_,))
        return self.q("SELECT * FROM events WHERE type = ? AND task_id = ? ORDER BY seq;", (type_, task_id))

    def current_boot(self) -> str:
        return self.q1("SELECT id FROM boots ORDER BY started_at DESC, rowid DESC LIMIT 1;")["id"]

    def lead1_msgs(self, task_id: str, type_: str) -> List[Any]:
        return self.q(
            "SELECT o.* FROM outbox o JOIN events e ON o.event_seq = e.seq WHERE e.task_id = ? AND o.type = ? "
            "AND o.recipient_id = (SELECT id FROM principals WHERE name = 'lead1');",
            (task_id, type_),
        )

    def wait_wrapper_started(self, run: Any, carrier: bool = True) -> None:
        """Wait until runwrap wrote wrapper.json (TX B commits right after Popen, before it)
        and, with ``carrier``, until a fake process carries the run's MESH_RUN_ID."""
        path = os.path.join(run["run_dir"], "wrapper.json")
        self.assertTrue(self.harness.wait_for(lambda: os.path.exists(path), timeout=20), "no wrapper.json")
        if carrier:
            self.assertTrue(self.harness.wait_for(
                lambda: any(p != run["wrapper_pid"] for p in _carriers(run["id"])), timeout=20), "fake not started")

    def set_setting(self, key: str, value: str) -> None:
        rc, resp = self.harness.setup_cli("config", "set", key, value, user=True)
        self.assertEqual(rc, 0, resp)

    def recovered(self) -> List[Dict[str, Any]]:
        return [json.loads(e["payload_json"]) for e in self.events("daemon.recovered")]


class TestRecoveryLaunch(RecoveryBase):
    def test_i1_i11_adopt_after_kill9(self) -> None:
        """ACC: I1, I11 (R1, R10) - kill -9 meshd during a record_pid + sleep 8 run; restart:
        run.adopted, the lease holder is the new boot id, the run finishes, 1 pid, task submitted."""
        pids = os.path.join(self.harness.fixtures_dir, "acc-pids")
        spec = 'FAKE {"steps":[{"record_pid":{"path":"%s"}},{"sleep":{"seconds":8}},{"succeed":{}}]}' % pids
        task_id = self.create_task(spec)
        run = self.wait_running_run(task_id)
        self.assertTrue(self.harness.wait_for(lambda: len(self.pid_lines(pids)) == 1, timeout=20))
        old_boot = self.current_boot()
        self.assertEqual(self.q1("SELECT holder FROM leases WHERE run_id = ?;", (run["id"],))["holder"], old_boot)

        self.harness.kill9_daemon()
        self.assertTrue(_alive(run["wrapper_pid"]), "the run must survive meshd's death")
        self.harness.restart_daemon()
        new_boot = self.current_boot()
        self.assertNotEqual(new_boot, old_boot)
        self.assertEqual(self.q1("SELECT crashed FROM boots WHERE id = ?;", (old_boot,))["crashed"], 1)

        adopted = self.events("run.adopted", task_id)
        self.assertEqual(len(adopted), 1)
        payload = json.loads(adopted[0]["payload_json"])
        self.assertEqual(payload["old_boot_id"], old_boot)
        self.assertEqual(payload["new_boot_id"], new_boot)
        self.assertEqual(payload["run_id"], run["id"])
        # I11 / R10: the lease is now held by the new boot (asserted while the run still runs).
        lease = self.q1("SELECT holder, holder_kind FROM leases WHERE run_id = ?;", (run["id"],))
        self.assertIsNotNone(lease, "the run finished before the lease could be checked")
        self.assertEqual(lease["holder_kind"], "meshd")
        self.assertEqual(lease["holder"], new_boot)
        self.assertEqual(self.recovered()[-1]["runs_adopted"], 1)

        self.wait_status(task_id, "submitted", timeout=40)
        self.assertEqual(len(self.pid_lines(pids)), 1)
        runs = self.runs_of(task_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "exited")
        self.assertEqual(runs[0]["exit_code"], 0)
        self.assertEqual(len(self.events("task.submitted", task_id)), 1)
        self.assertIsNone(self.q1("SELECT * FROM leases WHERE run_id = ?;", (run["id"],)))
        self.doctor_ok()

    def test_i2_j3_lost_on_restart_once(self) -> None:
        """ACC: I2, J3 (R2) - kill -9 meshd, kill -9 the wrapper group, restart: run lost,
        task failed(lost_on_restart), lead1 has exactly 1 task.failed; recovery again (kill -9
        and a clean stop/start): still exactly one task.failed event and message."""
        gate = self.gate("i2")
        task_id = self.create_task(self.held_spec("i2"))
        run = self.wait_running_run(task_id)
        self.wait_wrapper_started(run)
        try:
            self.harness.kill9_daemon()
            os.killpg(run["pgid"], signal.SIGKILL)
            self.assertTrue(self.harness.wait_for(lambda: not _group_members(run["pgid"]), timeout=10))
            self.assertEqual(_carriers(run["id"]), [])
            self.harness.restart_daemon()

            row = self.q1("SELECT status, end_reason FROM runs WHERE id = ?;", (run["id"],))
            self.assertEqual((row["status"], row["end_reason"]), ("lost", "lost_on_restart"))
            self.assertEqual(self.status(task_id), "failed")
            failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
            self.assertEqual(failure["reason"], "lost_on_restart")
            self.assertEqual(len(self.lead1_task_failed_msgs(task_id)), 1)
            self.assertIsNone(self.q1("SELECT * FROM leases WHERE run_id = ?;", (run["id"],)))
            self.assertEqual(self.recovered()[-1]["runs_lost"], 1)

            # J3: re-run recovery twice.
            n_recovered = len(self.recovered())
            self.harness.kill9_daemon()
            self.harness.restart_daemon()
            self.harness.stop_daemon()
            self.harness.restart_daemon()
            self.assertEqual(len(self.recovered()), n_recovered + 2)
            self.assertEqual(self.recovered()[-1]["runs_lost"], 0)
            self.assertEqual(len(self.events("task.failed", task_id)), 1)
            msgs = self.lead1_task_failed_msgs(task_id)
            self.assertEqual(len(msgs), 1)
            dedup = self.q("SELECT dedup_key, COUNT(*) AS n FROM outbox GROUP BY dedup_key HAVING n > 1;")
            self.assertEqual(dedup, [])
            self.assertEqual(len(self.runs_of(task_id)), 1)
            self.doctor_ok()
        finally:
            self.release(gate)

    def test_i3_deadline_passed_while_down(self) -> None:
        """ACC: I3 (R3) - meshd stays down past the run's deadline; restart: the run is
        terminated as timeout, its group is verified gone, task failed(timeout)."""
        self.set_setting("kill.grace_s", "2")
        task_id = self.create_task(self.held_spec("i3"), "--timeout", "3s")
        run = self.wait_running_run(task_id)
        self.wait_wrapper_started(run)
        try:
            self.harness.kill9_daemon()
            deadline = self.q1("SELECT deadline_at FROM runs WHERE id = ?;", (run["id"],))["deadline_at"]
            from mesh.core.clock import parse_utc
            wait_s = parse_utc(deadline).timestamp() + 1.0 - time.time()
            if wait_s > 0:
                time.sleep(wait_s)
            self.assertTrue(_alive(run["wrapper_pid"]), "the run must still be alive past its deadline")
            self.harness.restart_daemon()
            self.assertEqual(len(self.events("run.adopted", task_id)), 1)
            self.wait_status(task_id, "failed", timeout=30)
            row = self.q1("SELECT status, end_reason, ended_at FROM runs WHERE id = ?;", (run["id"],))
            self.assertEqual((row["status"], row["end_reason"]), ("killed", "timeout"))
            failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
            self.assertEqual(failure["reason"], "timeout")
            self.assertEqual(_group_members(run["pgid"]), [])
            self.assertEqual(_carriers(run["id"]), [])
            self.assertEqual(self.recovered()[-1]["runs_adopted_past_deadline"], 1)
            self.assertEqual(len(self.lead1_task_failed_msgs(task_id)), 1)
            self.doctor_ok()
        finally:
            self.release(self.gate("i3"))

    def test_i8_daemon_stop_leaves_run_running(self) -> None:
        """ACC: I8 - `daemon stop` (SIGTERM) during a fake run: the run keeps running while
        meshd is down; on restart it is adopted and finishes once."""
        gate = self.gate("i8")
        task_id = self.create_task(self.held_spec("i8"))
        run = self.wait_running_run(task_id)
        self.wait_wrapper_started(run)
        try:
            rc, resp = self.harness.cli("daemon", "stop", user=True, timeout=10.0)
            self.assertEqual(rc, 0, resp)
            self.harness.wait_daemon_exit(timeout=15)
            self.assertIsNone(self.harness.meshd_pid())
            time.sleep(1.0)
            self.assertTrue(_alive(run["wrapper_pid"]), "daemon stop must not stop runs")
            self.assertTrue(_carriers(run["id"]), "the fake must still run while meshd is down")
            self.harness.restart_daemon()
            self.assertEqual(len(self.events("run.adopted", task_id)), 1)
            self.assertEqual(self.status(task_id), "running")
        finally:
            self.release(gate)
        self.wait_status(task_id, "submitted", timeout=30)
        self.assertTrue(self.harness.wait_for(
            lambda: self.q1("SELECT status FROM runs WHERE id = ?;", (run["id"],))["status"] == "exited", timeout=20))
        self.assertEqual(len(self.runs_of(task_id)), 1)
        self.assertEqual(len(self.events("task.submitted", task_id)), 1)
        self.assertEqual(len(self.lead1_msgs(task_id, "task.submitted")), 1)
        self.doctor_ok()

    def test_lost_wrapper_leftovers_cancelling_and_outbox(self) -> None:
        """§11.7 steps 2 and 4: (A) the wrapper alone was killed, its fake child lives on:
        recovery never signals the dead wrapper's pid, terminates the MESH_RUN_ID carrier,
        then closes the run lost / failed(lost_on_restart); (B) a task in cancelling whose
        group died with meshd ends cancelled; a `delivering` outbox row goes back to pending
        with attempts + 1."""
        self.set_setting("kill.grace_s", "60")
        task_a = self.create_task(self.held_spec("la"))
        task_b = self.create_task('FAKE {"ignore_sigterm": {}}')
        run_a = self.wait_running_run(task_a)
        run_b = self.wait_running_run(task_b)
        self.wait_wrapper_started(run_a)
        self.wait_wrapper_started(run_b)
        rc, resp = self.harness.cli("task", "cancel", task_b, "--reason", "stop", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.status(task_b), "cancelling")
        try:
            self.harness.kill9_daemon()
            os.kill(run_a["wrapper_pid"], signal.SIGKILL)
            os.killpg(run_b["pgid"], signal.SIGKILL)
            self.assertTrue(self.harness.wait_for(lambda: not _alive(run_a["wrapper_pid"]), timeout=10))
            self.assertTrue(self.harness.wait_for(lambda: not _group_members(run_b["pgid"]), timeout=10))
            survivors = _carriers(run_a["id"])
            self.assertTrue(survivors, "the fake child must outlive its wrapper")
            msg = self.q1("SELECT id, attempts FROM outbox WHERE status = 'pending' ORDER BY rowid LIMIT 1;")
            conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
            try:
                conn.execute("UPDATE outbox SET status = 'delivering' WHERE id = ?;", (msg["id"],))
                conn.commit()
            finally:
                conn.close()

            self.harness.restart_daemon()
            summary = self.recovered()[-1]
            # Run A always terminates its carrier. Run B is normally closed lost at once, but
            # a same-uid non-dumpable process (e.g. an sshd login, environ EACCES) started
            # after run B's wrapper makes its carrier scan uncertain, and recovery then holds
            # run B in terminating until that process exits (fail-safe, limit L14). Accept
            # both, then wait for run B to close (m3-p1c-final: host-independent).
            self.assertIn(summary["runs_terminating"], (1, 2), summary)
            self.assertEqual(summary["runs_terminating"] + summary["runs_lost"], 2, summary)
            self.assertEqual(summary["outbox_requeued"], 1)
            self.assertTrue(self.harness.wait_for(
                lambda: self.q1("SELECT status FROM runs WHERE id = ?;", (run_b["id"],))["status"] == "lost",
                timeout=60))
            row = self.q1("SELECT status, attempts FROM outbox WHERE id = ?;", (msg["id"],))
            self.assertIn(row["status"], ("pending", "delivered"))
            self.assertEqual(row["attempts"], msg["attempts"] + 1)
            # (B) cancelling without a live run -> cancelled.
            rb = self.q1("SELECT status, end_reason FROM runs WHERE id = ?;", (run_b["id"],))
            self.assertEqual((rb["status"], rb["end_reason"]), ("lost", "lost_on_restart"))
            self.assertEqual(self.status(task_b), "cancelled")
            self.assertEqual(len(self.lead1_task_failed_msgs(task_b)), 0)
            # (A) carriers terminated first; only then the run closes and the lease goes.
            self.wait_status(task_a, "failed", timeout=30)
            ra = self.q1("SELECT status, end_reason FROM runs WHERE id = ?;", (run_a["id"],))
            self.assertEqual((ra["status"], ra["end_reason"]), ("lost", "lost_on_restart"))
            for pid in survivors:
                self.assertFalse(_alive(pid))
            self.assertEqual(_carriers(run_a["id"]), [])
            self.assertIsNone(self.q1("SELECT * FROM leases WHERE run_id = ?;", (run_a["id"],)))
            failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (task_a,))["failure_json"])
            self.assertEqual(failure["reason"], "lost_on_restart")
            self.assertEqual(len(self.lead1_task_failed_msgs(task_a)), 1)
            self.doctor_ok()
        finally:
            self.release(self.gate("la"))


class TestRecoveryCrashHooks(RecoveryBase):
    def create_task_and_crash(self, point: str, spec: str) -> str:
        """Restart meshd with MESH_TEST_CRASH_AT=<point>, create the task while launch
        starts are held (T5 runs in the scheduler; the crash must not hit the create
        request itself), then release the hold and wait for meshd to kill itself."""
        self.harness.stop_daemon()
        self.harness.hold_launch_starts()
        self.harness.start_daemon(extra_env={"MESH_TEST_CRASH_AT": point})
        task_id = self.create_task(spec)
        self.assertEqual(self.status(task_id), "queued")
        self.harness.release_launch_starts()
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30), -signal.SIGKILL)
        return task_id

    def test_i7_crash_after_tx_a(self) -> None:
        """ACC: I7 (R7) - MESH_TEST_CRASH_AT=after_tx_a; restart; wait 20 s: run spawn_failed,
        task failed(lost_before_spawn), no new run, lead notified once."""
        task_id = self.create_task_and_crash("after_tx_a", 'FAKE {"succeed": {}}')
        runs = self.runs_of(task_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "starting")
        self.assertFalse(os.path.exists(os.path.join(runs[0]["run_dir"], "wrapper.json")))

        self.harness.restart_daemon()
        row = self.q1("SELECT status, end_reason FROM runs WHERE id = ?;", (runs[0]["id"],))
        self.assertEqual((row["status"], row["end_reason"]), ("spawn_failed", "lost_before_spawn"))
        self.assertEqual(self.status(task_id), "failed")
        failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
        self.assertEqual(failure["reason"], "lost_before_spawn")
        self.assertEqual(self.recovered()[-1]["runs_spawn_failed"], 1)
        time.sleep(20)
        self.assertEqual(len(self.runs_of(task_id)), 1, "no automatic respawn")
        self.assertEqual(self.status(task_id), "failed")
        self.assertEqual(len(self.lead1_task_failed_msgs(task_id)), 1)
        self.doctor_ok()

    def test_i10_crash_after_popen_adopts(self) -> None:
        """ACC: I10 (R9) - MESH_TEST_CRASH_AT=after_popen with a record_pid task (runwrap delays
        wrapper.json by 3 s); restart: run.adopted (not spawn_failed), exactly 1 recorded pid,
        exactly one run for the task."""
        pids = os.path.join(self.harness.fixtures_dir, "acc-pids")
        gate = self.gate("i10")
        spec = ('FAKE {"steps":[{"record_pid":{"path":"%s"}},{"wait_release":{"path":"%s"}},{"succeed":{}}]}'
                % (pids, gate))
        task_id = self.create_task_and_crash("after_popen", spec)
        try:
            runs = self.runs_of(task_id)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "starting")
            self.assertIsNone(runs[0]["wrapper_pid"])
            with open(os.path.join(runs[0]["run_dir"], "spec.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f).get("testing_wrapper_delay_s"), 3.0)
            self.harness.restart_daemon()
            adopted = self.events("run.adopted", task_id)
            self.assertEqual(len(adopted), 1)
            self.assertTrue(json.loads(adopted[0]["payload_json"])["from_spawn_window"])
            row = self.q1("SELECT * FROM runs WHERE id = ?;", (runs[0]["id"],))
            self.assertEqual(row["status"], "running")
            self.assertTrue(row["wrapper_pid"])
            self.assertEqual(row["pgid"], row["wrapper_pid"])
            self.assertEqual(self.q1("SELECT holder FROM leases WHERE run_id = ?;", (row["id"],))["holder"],
                             self.current_boot())
            self.assertTrue(self.harness.wait_for(lambda: len(self.pid_lines(pids)) >= 1, timeout=20))
        finally:
            self.release(gate)
        self.wait_status(task_id, "submitted", timeout=30)
        self.assertEqual(len(self.pid_lines(pids)), 1)
        self.assertEqual(len(self.runs_of(task_id)), 1)
        self.assertEqual(self.events("run.spawn_failed", task_id), [])
        self.assertNotIn(self.runs_of(task_id)[0]["status"], ("spawn_failed", "lost"))
        self.doctor_ok()


class TestRecoveryState(RecoveryBase):
    def test_i4_idempotent_burst_across_kill9(self) -> None:
        """ACC: I4 (R4) - kill -9 meshd during 200 `task create` calls with idempotency keys;
        restart; replay all 200 keys: integrity_check ok, exactly 200 tasks, replays are
        idempotent_replay:true."""
        keys = [f"i4-{n:03d}" for n in range(200)]
        before = self.q1("SELECT COUNT(*) AS n FROM tasks;")["n"]
        results: Dict[str, int] = {}

        def create(key: str) -> Any:
            return self.harness.setup_cli("task", "create", "--project", self.project_id, "--title", key,
                                          "--spec", f"spec {key}", "--idempotency-key", key,
                                          token=self.tok_lead1)

        def burst() -> None:
            for key in keys:
                rc, _ = create(key)
                results[key] = rc

        worker = threading.Thread(target=burst)
        worker.start()
        self.assertTrue(self.harness.wait_for(lambda: len(results) >= 40, timeout=60, step=0.01))
        self.harness.kill9_daemon()
        worker.join(timeout=120)
        self.assertFalse(worker.is_alive())
        self.assertTrue(any(rc != 0 for rc in results.values()), "the kill must land inside the burst")
        self.assertTrue(any(rc == 0 for rc in results.values()))

        self.harness.restart_daemon()
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check;").fetchone()[0], "ok")
        finally:
            conn.close()
        committed = {k for k, rc in results.items() if rc == 0}
        ids = {}
        for key in keys:
            rc, resp = create(key)
            self.assertEqual(rc, 0, (key, resp))
            if key in committed:
                self.assertTrue(resp.get("idempotent_replay"), (key, resp))
            ids[key] = resp["data"]["id"]
        # Every key now has a stored result: a second replay is idempotent. Review m1c-run4 §5:
        # a 20-key spot check (every 10th key, plus up to 5 keys first created after the
        # crash) instead of a second full replay; the checklist asks for one full replay.
        uncommitted = [k for k in keys if k not in committed]
        spot = sorted(set(keys[::10]) | set(uncommitted[:5]))
        for key in spot:
            rc, resp = create(key)
            self.assertEqual(rc, 0, (key, resp))
            self.assertTrue(resp.get("idempotent_replay"), (key, resp))
            self.assertEqual(resp["data"]["id"], ids[key])
        # Spot-check the checked path through a separate CLI process too.
        for key in (keys[0], keys[100], keys[199]):
            rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", key,
                                        "--spec", f"spec {key}", "--idempotency-key", key, token=self.tok_lead1)
            self.assertEqual(rc, 0, resp)
            self.assertTrue(resp.get("idempotent_replay"), resp)
        self.assertEqual(self.q1("SELECT COUNT(*) AS n FROM tasks;")["n"] - before, 200)
        self.assertEqual(len(set(ids.values())), 200)
        self.doctor_ok()

    def inbox_task(self) -> Dict[str, Any]:
        task_id = self.create_task("inbox work", assignee="ibx")
        rc, resp = self.harness.cli("task", "start", task_id, token=self.tokens["ibx"])
        self.assertEqual(rc, 0, resp)
        return {"task_id": task_id, "run_id": resp["data"]["run_id"], "fence": resp["data"]["fence"]}

    def test_i5_j3_submit_message_once_across_restarts(self) -> None:
        """ACC: I5, J3 - a task.submitted message for lead1 (inbox submit replayed with the same
        idempotency key) survives kill -9 + restart and a second recovery exactly once."""
        t = self.inbox_task()
        args = ("task", "submit", t["task_id"], "--summary", "done", "--run", t["run_id"],
                "--fence", str(t["fence"]), "--idempotency-key", "j3-submit")
        rc, resp = self.harness.cli(*args, token=self.tokens["ibx"])
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli(*args, token=self.tokens["ibx"])
        self.assertEqual(rc, 0, resp)
        self.assertTrue(resp.get("idempotent_replay"), resp)

        msg_ids = {m["id"] for m in self.lead1_msgs(t["task_id"], "task.submitted")}
        self.assertEqual(len(msg_ids), 1)

        def submitted_items() -> List[Any]:
            rc, resp = self.harness.cli("inbox", "list", token=self.tok_lead1)
            self.assertEqual(rc, 0, resp)
            return [it for it in resp["data"]["items"] if it.get("type") == "task.submitted"
                    and (it["id"] in msg_ids or it.get("payload", {}).get("task_id") == t["task_id"])]

        # R5: the lead does not read it before the crash; check the DB only.
        rows = self.lead1_msgs(t["task_id"], "task.submitted")
        self.assertEqual([r["id"] for r in rows], sorted(msg_ids))
        self.assertEqual(rows[0]["status"], "pending")
        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        first = submitted_items()  # the first read happens after the restart
        self.assertEqual(len(first), 1)
        self.assertEqual({it["id"] for it in first}, msg_ids)
        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        self.assertEqual(len(submitted_items()), 1)
        self.assertEqual(self.q1("SELECT status FROM outbox WHERE id = ?;", (rows[0]["id"],))["status"], "delivered")
        self.assertEqual(len(self.events("task.submitted", t["task_id"])), 1)
        self.assertEqual(len(self.lead1_msgs(t["task_id"], "task.submitted")), 1)
        self.doctor_ok()

    def test_i6_inbox_lease_survives_restart(self) -> None:
        """ACC: I6 (R6) - inbox task running; meshd down for less than the ttl; restart; the
        lease gets the grace (now + ttl), heartbeats resume: task still running, submit rc 0."""
        self.set_setting("lease.ttl_s", "4")
        t = self.inbox_task()
        hb = ("task", "heartbeat", t["task_id"], "--run", t["run_id"], "--fence", str(t["fence"]))
        rc, resp = self.harness.cli(*hb, token=self.tokens["ibx"])
        self.assertEqual(rc, 0, resp)
        before = self.q1("SELECT expires_at FROM leases WHERE run_id = ?;", (t["run_id"],))["expires_at"]
        self.harness.kill9_daemon()
        time.sleep(3.0)
        self.harness.restart_daemon()
        after = self.q1("SELECT expires_at FROM leases WHERE run_id = ?;", (t["run_id"],))
        self.assertIsNotNone(after, "the agent lease must survive the restart")
        from mesh.core.clock import parse_utc
        self.assertGreater(parse_utc(after["expires_at"]).timestamp(), parse_utc(before).timestamp())
        self.assertEqual(self.recovered()[-1]["agent_leases_extended"], 1)
        # Past the original expiry, heartbeats resume.
        wait_s = parse_utc(before).timestamp() + 0.5 - time.time()
        if wait_s > 0:
            time.sleep(wait_s)
        rc, resp = self.harness.cli(*hb, token=self.tokens["ibx"])
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.status(t["task_id"]), "running")
        rc, resp = self.harness.cli("task", "submit", t["task_id"], "--summary", "done", "--run", t["run_id"],
                                    "--fence", str(t["fence"]), token=self.tokens["ibx"])
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.status(t["task_id"]), "submitted")
        self.doctor_ok()

    def test_j5_never_connected_origin_keeps_messages(self) -> None:
        """ACC: J5 - `plain` (never connected) is the origin of a project that completes;
        restart meshd; all its messages are present."""
        rc, resp = self.harness.cli("intake", "submit", "--title", "J5", "--goal", "never-connected origin",
                                    "--criterion", "c1", token=self.tokens["plain"])
        self.assertEqual(rc, 0, resp)
        prj = resp["data"]["id"]
        crit = resp["data"]["criteria"][0]["id"]
        report = json.dumps({"criteria": [{"id": crit, "met": True, "evidence": "ok"}], "summary": "done"})
        rc, resp = self.harness.cli("project", "complete", prj, "--report", "-", stdin=report, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)

        user_plain = self.q1("SELECT id FROM principals WHERE name = 'plain';")["id"]

        def plain_rows() -> List[Any]:
            return self.q("SELECT id, type, project_id, payload_json, status FROM outbox WHERE recipient_id = ? AND channel = 'inbox' "
                          "ORDER BY id;", (user_plain,))

        # plain never connects before the restart: the "before" set comes from the DB.
        before = plain_rows()
        self.assertTrue(before)
        self.assertTrue(all(r["status"] == "pending" for r in before), [dict(r) for r in before])
        self.assertIn("project.completed", {r["type"] for r in before})
        self.assertTrue(any(r["project_id"] == prj for r in before))
        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        self.assertEqual([dict(r) for r in plain_rows()], [dict(r) for r in before])
        rc, resp = self.harness.cli("inbox", "list", token=self.tokens["plain"])  # first connection
        self.assertEqual(rc, 0, resp)
        after = resp["data"]["items"]
        self.assertEqual(sorted(it["id"] for it in after), sorted(r["id"] for r in before))
        self.assertEqual({it["id"]: it.get("type") for it in after}, {r["id"]: r["type"] for r in before})
        self.assertIn("project.completed", {it.get("type") for it in after})
        self.doctor_ok()


class TestRecoveryBranches(RecoveryBase):
    """Review m1c-run4 §3: the §11.7 step-2 branches not covered by I1–I11."""

    def rw(self, sql: str, args: tuple = ()) -> None:
        """Write to mesh.db while meshd is down (simulates the state a crash leaves)."""
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def kill_run_group(self, run: Any) -> None:
        os.killpg(run["pgid"], signal.SIGKILL)
        self.assertTrue(self.harness.wait_for(lambda: not _group_members(run["pgid"]), timeout=10))
        self.assertEqual(_carriers(run["id"]), [])

    def side_proc(self, env_run_id: str = None, term_file: str = None):
        """A process outside any run (own session); optionally carrying MESH_RUN_ID. With
        term_file it records SIGTERM's arrival and exits 1 s later."""
        script = ("import signal,sys,time\n"
                  "f=sys.argv[1]\n"
                  "def h(*a):\n"
                  "    if f:\n"
                  "        open(f,'a').write('%f\\n' % time.time())\n"
                  "    time.sleep(1.0)\n"
                  "    if f:\n"
                  "        open(f,'a').write('%f\\n' % time.time())\n"
                  "    sys.exit(0)\n"
                  "signal.signal(signal.SIGTERM,h)\n"
                  "print('r',flush=True)\n"
                  "time.sleep(60)\n")
        env = {k: v for k, v in os.environ.items() if k != "MESH_RUN_ID"}
        if env_run_id:
            env["MESH_RUN_ID"] = env_run_id
        import subprocess
        import sys
        proc = subprocess.Popen([sys.executable, "-c", script, term_file or ""], stdout=subprocess.PIPE,
                                start_new_session=True, env=env)
        proc.stdout.readline()

        def _cleanup() -> None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            proc.stdout.close()
        self.addCleanup(_cleanup)
        return proc

    def test_run_exited_while_meshd_down(self) -> None:
        """§11.7 step 2 (exit.json present, wrapper dead): the run is closed from exit.json
        on restart, not adopted: exited 0, T7 auto-submit, one lead message, runs_exited 1."""
        gate = self.gate("exited-down")
        task_id = self.create_task(self.held_spec("exited-down"))
        run = self.wait_running_run(task_id)
        self.wait_wrapper_started(run)
        self.harness.kill9_daemon()
        self.release(gate)
        exit_path = os.path.join(run["run_dir"], "exit.json")
        self.assertTrue(self.harness.wait_for(lambda: os.path.exists(exit_path), timeout=20), "no exit.json")
        self.assertTrue(self.harness.wait_for(lambda: not _alive(run["wrapper_pid"]), timeout=10))
        # The wrapper can die a moment before the rest of its process group (the fake agent
        # and its children). Recovery then correctly defers the close (occupied-group gate)
        # and the scheduler closes the run on a later tick, so under load the immediate
        # status read below saw "running". This test covers the "exited while down" branch,
        # whose precondition is an empty group: wait for it before restarting.
        self.assertTrue(self.harness.wait_for(
            lambda: not _group_members(run["pgid"]) and not _carriers(run["id"]), timeout=10),
            "run group still occupied")
        self.harness.restart_daemon()
        self.assertEqual(self.status(task_id), "submitted")
        row = self.q1("SELECT status, exit_code FROM runs WHERE id = ?;", (run["id"],))
        self.assertEqual((row["status"], row["exit_code"]), ("exited", 0))
        self.assertEqual(self.events("run.adopted", task_id), [])
        submitted = self.events("task.submitted", task_id)
        self.assertEqual(len(submitted), 1)
        self.assertIs(json.loads(submitted[0]["payload_json"]).get("auto"), True, submitted[0]["payload_json"])
        self.assertEqual(len(self.lead1_msgs(task_id, "task.submitted")), 1)
        self.assertEqual(self.recovered()[-1]["runs_exited"], 1)
        self.assertIsNone(self.q1("SELECT * FROM leases WHERE run_id = ?;", (run["id"],)))
        self.doctor_ok()

    def test_no_wrapper_json_carrier_terminated_then_spawn_failed(self) -> None:
        """§11.7 step 2, last case: no wrapper.json but a MESH_RUN_ID carrier lives and no
        wrapper.json appears within the wait: the carriers are terminated and the run closes
        spawn_failed(lost_before_spawn) only after they are gone; never respawned."""
        self.set_setting("kill.grace_s", "5")
        gate = self.gate("nowrap")
        task_id = self.create_task(self.held_spec("nowrap"))
        run = self.wait_running_run(task_id)
        self.wait_wrapper_started(run)
        try:
            self.harness.kill9_daemon()
            self.kill_run_group(run)
            os.unlink(os.path.join(run["run_dir"], "wrapper.json"))
            term_file = os.path.join(self.harness.fixtures_dir, "nowrap-term")
            carrier = self.side_proc(env_run_id=run["id"], term_file=term_file)
            self.harness.restart_daemon()
            self.assertEqual(self.recovered()[-1]["runs_terminating"], 1)
            self.wait_status(task_id, "failed", timeout=30)
            self.assertTrue(self.harness.wait_for(lambda: not _alive(carrier.pid), timeout=10))
            row = self.q1("SELECT status, end_reason, ended_at FROM runs WHERE id = ?;", (run["id"],))
            self.assertEqual((row["status"], row["end_reason"]), ("spawn_failed", "lost_before_spawn"))
            failure = json.loads(self.q1("SELECT failure_json FROM tasks WHERE id = ?;", (task_id,))["failure_json"])
            self.assertEqual(failure["reason"], "lost_before_spawn")
            # The carrier got SIGTERM and lived 1 s more; the run closed only after it was gone.
            stamps = [float(x) for x in self.pid_lines(term_file)]
            self.assertEqual(len(stamps), 2, stamps)
            from mesh.core.clock import parse_utc
            self.assertGreaterEqual(parse_utc(row["ended_at"]).timestamp() + 0.05, stamps[1])
            self.assertEqual(len(self.runs_of(task_id)), 1)  # never respawned
            self.assertEqual(self.events("run.adopted", task_id), [])
            self.doctor_ok()
        finally:
            self.release(gate)

    def test_cancelling_task_without_run_cancelled_on_recovery(self) -> None:
        """§11.7 step 6 (_step_cancelling): a cancelling task with no active run becomes
        cancelled on recovery, with recovered:true and dependents notified."""
        task_id = self.create_task("queued work", assignee="ibx")
        self.wait_status(task_id, "queued")
        self.harness.kill9_daemon()
        self.rw("UPDATE tasks SET status = 'cancelling', version = version + 1 WHERE id = ?;", (task_id,))
        self.harness.restart_daemon()
        self.assertEqual(self.status(task_id), "cancelled")
        ev = self.events("task.cancelled", task_id)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["from_state"], ev[0]["to_state"]), ("cancelling", "cancelled"))
        payload = json.loads(ev[0]["payload_json"])
        payload = payload.get("payload", payload)
        self.assertIs(payload["recovered"], True, payload)
        self.assertEqual(self.recovered()[-1]["tasks_cancelled"], 1)
        # Idempotent: another recovery does not cancel it again.
        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        self.assertEqual(self.recovered()[-1]["tasks_cancelled"], 0)
        self.assertEqual(len(self.events("task.cancelled", task_id)), 1)
        self.doctor_ok()

    def test_stale_wrapper_json_identity_never_signalled(self) -> None:
        """§11.7 step 2: wrapper.json names a live pid whose starttime differs (a reused pid);
        the run is lost_on_restart and that pid (also the row's wrapper_pid/pgid) is never
        signalled."""
        gate = self.gate("stale")
        task_id = self.create_task(self.held_spec("stale"))
        run = self.wait_running_run(task_id)
        self.wait_wrapper_started(run)
        try:
            self.harness.kill9_daemon()
            self.kill_run_group(run)
            term_file = os.path.join(self.harness.fixtures_dir, "stale-term")
            reuser = self.side_proc(term_file=term_file)
            st = int(get_proc_stat(reuser.pid)["starttime"])
            wpath = os.path.join(run["run_dir"], "wrapper.json")
            with open(wpath, "r", encoding="utf-8") as f:
                wrapper = json.load(f)
            wrapper["pid"] = reuser.pid
            wrapper["starttime"] = st + 1
            with open(wpath, "w", encoding="utf-8") as f:
                json.dump(wrapper, f)
            self.rw("UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? WHERE id = ?;",
                    (reuser.pid, reuser.pid, st + 1, run["id"]))
            self.harness.restart_daemon()
            row = self.q1("SELECT status, end_reason FROM runs WHERE id = ?;", (run["id"],))
            self.assertEqual((row["status"], row["end_reason"]), ("lost", "lost_on_restart"))
            self.assertEqual(self.status(task_id), "failed")
            self.assertEqual(self.recovered()[-1]["runs_lost"], 1)
            time.sleep(2.0)  # several supervisor ticks
            self.assertTrue(_alive(reuser.pid), "the reused pid must not be killed")
            self.assertEqual(self.pid_lines(term_file), [], "the reused pid must never be signalled")
            self.assertEqual(int(get_proc_stat(reuser.pid)["starttime"]), st)
            self.doctor_ok()
        finally:
            self.release(gate)


if __name__ == "__main__":
    unittest.main()
