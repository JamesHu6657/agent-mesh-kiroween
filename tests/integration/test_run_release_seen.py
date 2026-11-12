"""r4c (ACC: N18): a run held because its seen state is unusable.

After a restart with the adopted wrapper gone and ``seen.json`` corrupt, meshd holds
the run (open, lease kept, nothing signalled) while an env-less member is still in
the run's group/session. It records ``run.seen_unusable`` once for the run and logs it.
``mesh run release <run-id> --force-seen-unusable`` (user only) is the operator path:
the run closes ``lost``, the lease is released, no process is signalled and
``run.seen_release_forced {actor, reason}`` is recorded.
"""

import json
import os
import signal
from typing import Any, List

from mesh.core.proc import get_proc_stat
from tests.recovery.test_recovery import RecoveryBase, _alive


class TestRunReleaseSeen(RecoveryBase):
    def _guard(self, pid: int) -> None:
        info = get_proc_stat(pid)
        st = info["starttime"] if info else None

        def _cleanup() -> None:
            cur = get_proc_stat(pid)
            if cur is not None and st is not None and cur["starttime"] == st and cur["state"] != "Z":
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(_cleanup)

    def _lease(self, task_id: str) -> Any:
        return self.q1("SELECT * FROM leases WHERE key = ?;", (f"task:{task_id}",))

    def _run(self, run_id: str) -> Any:
        return self.q1("SELECT * FROM runs WHERE id = ?;", (run_id,))

    def _run_events(self, type_: str, run_id: str) -> List[Any]:
        return self.q("SELECT * FROM events WHERE type = ? AND run_id = ? ORDER BY seq;", (type_, run_id))

    def _meshd_log(self) -> str:
        path = os.path.join(self.harness.home, "logs", "meshd.log")
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _held_run(self) -> Any:
        """An adopted run whose wrapper is gone and whose seen.json is corrupt, with an
        env-less SIGTERM-ignoring member alive in its group: returns (task_id, run, member)."""
        pidfile = os.path.join(self.harness.fixtures_dir, "rel-member-pid")
        trigger = os.path.join(self.harness.fixtures_dir, "rel-member-trigger")
        spec = ('FAKE {"steps":[{"spawn_envless_member":{"path":"%s","trigger":"%s"}},'
                '{"succeed":{}}]}' % (pidfile, trigger))
        self.set_setting("kill.grace_s", "2")
        task_id = self.create_task(spec)
        run = self.wait_running_run(task_id)
        self.assertTrue(self.harness.wait_for(lambda: bool(self.pid_lines(pidfile)), timeout=20))
        member = int(self.pid_lines(pidfile)[0])
        self._guard(member)
        fake_pid = int(get_proc_stat(member)["ppid"])
        self._guard(fake_pid)
        seen_path = os.path.join(run["run_dir"], "seen.json")
        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        self.assertTrue(self.harness.wait_for(lambda: os.path.exists(seen_path), timeout=15))
        self.harness.kill9_daemon()
        os.kill(run["wrapper_pid"], signal.SIGKILL)
        self.assertTrue(self.harness.wait_for(lambda: not _alive(run["wrapper_pid"]), timeout=10))
        os.kill(fake_pid, signal.SIGKILL)
        self.assertTrue(self.harness.wait_for(lambda: not _alive(fake_pid), timeout=10))
        with open(seen_path, "w", encoding="utf-8") as f:
            f.write("{truncated")
        self.harness.restart_daemon()
        return task_id, run, member

    def _wait_held_ticks(self, task_id: str, n: int = 3) -> None:
        """Wait until meshd renewed the held lease ``n`` more times (several watch ticks)."""
        beats = [self._lease(task_id)["heartbeat_at"]]

        def renewed() -> bool:
            lease = self._lease(task_id)
            self.assertIsNotNone(lease, "lease released while held")
            if lease["heartbeat_at"] != beats[-1]:
                beats.append(lease["heartbeat_at"])
            return len(beats) > n
        self.assertTrue(self.harness.wait_for(renewed, timeout=30, step=0.2), beats)

    def test_held_run_event_once_and_forced_release(self) -> None:
        """ACC: N18 (r4c item 3.3, 3.4) - held run: one `run.seen_unusable {run_id,
        reason}` plus a meshd.log line; `run release` refused without the flag, for an
        agent token and for a run that is not held; with the flag the run is lost, the
        lease released, the member untouched and `run.seen_release_forced` recorded."""
        task_id, run, member = self._held_run()
        self._wait_held_ticks(task_id)
        self.assertIn(self._run(run["id"])["status"], ("starting", "running"))
        self.assertTrue(_alive(member), "a member of an unusable seen state must not be signalled")
        ev = self._run_events("run.seen_unusable", run["id"])
        self.assertEqual(len(ev), 1, ev)
        payload = json.loads(ev[0]["payload_json"])
        self.assertEqual(payload, {"run_id": run["id"], "reason": "invalid_json"})
        self.assertIn(f"run {run['id']}: seen.json unusable (reason=invalid_json)", self._meshd_log())
        # Still once after more ticks and after another restart.
        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        self._wait_held_ticks(task_id)
        self.assertEqual(len(self._run_events("run.seen_unusable", run["id"])), 1)

        rc, resp = self.harness.cli("run", "release", run["id"], user=True)
        self.assertEqual(rc, 2, resp)
        self.assertIn("--force-seen-unusable", json.dumps(resp))
        rc, resp = self.harness.cli("run", "release", run["id"], "--force-seen-unusable", token=self.tok_lead1)
        self.assertEqual(rc, 4, resp)
        other = self.create_task(self.held_spec("rel-other"))
        other_run = self.wait_running_run(other)
        rc, resp = self.harness.cli("run", "release", other_run["id"], "--force-seen-unusable", user=True)
        self.assertEqual(rc, 6, resp)
        self.assertIn(self._run(other_run["id"])["status"], ("starting", "running"))
        self.release(self.gate("rel-other"))
        self.assertIn(self._run(run["id"])["status"], ("starting", "running"))
        self.assertIsNotNone(self._lease(task_id))
        self.assertEqual(self._run_events("run.seen_release_forced", run["id"]), [])

        rc, resp = self.harness.cli("run", "release", run["id"], "--force-seen-unusable", user=True)
        self.assertEqual(rc, 0, resp)
        r = self._run(run["id"])
        self.assertEqual(r["status"], "lost")
        self.assertIsNone(self._lease(task_id))
        self.assertTrue(_alive(member), "release must not signal any process")
        forced = self._run_events("run.seen_release_forced", run["id"])
        self.assertEqual(len(forced), 1)
        fp = json.loads(forced[0]["payload_json"])
        self.assertEqual(fp["reason"], "invalid_json")
        self.assertTrue(fp["actor"])
        self.assertEqual(forced[0]["actor_kind"], "user")
        rc, resp = self.harness.cli("run", "release", run["id"], "--force-seen-unusable", user=True)
        self.assertEqual(rc, 6, resp)
        os.kill(member, signal.SIGKILL)
        self.assertTrue(self.harness.wait_for(lambda: not _alive(member), timeout=10))
        self.wait_status(other, "submitted", timeout=40)
        self.doctor_ok()

    # r4d item 3: the hold is persisted in the DB (meta key ``seen_hold:<run_id>``).
    def _hold_row(self, run_id: str) -> Any:
        return self.q1("SELECT * FROM meta WHERE key = ?;", ("seen_hold:" + run_id,))

    @staticmethod
    def _same_alive(pid: int, starttime: int) -> bool:
        info = get_proc_stat(pid)
        return info is not None and info.get("state") != "Z" and int(info["starttime"]) == int(starttime)

    def _held_after_delete(self) -> Any:
        """Held run (unusable seen.json) whose file is then deleted, followed by kill -9 of
        meshd and a restart: still held while the member lives."""
        task_id, run, member = self._held_run()
        self._wait_held_ticks(task_id)
        self.assertEqual(len(self._run_events("run.seen_unusable", run["id"])), 1)
        seen_path = os.path.join(run["run_dir"], "seen.json")
        os.unlink(seen_path)
        self.harness.kill9_daemon()
        self.assertTrue(_alive(member))
        self.harness.restart_daemon()
        self.assertIsNotNone(self._lease(task_id), "lease released after the restart: the hold was lost")
        self.assertIn(self._run(run["id"])["status"], ("starting", "running"),
                      "run closed after the restart: the hold was lost")
        self._wait_held_ticks(task_id)
        self.assertIn(self._run(run["id"])["status"], ("starting", "running"))
        self.assertTrue(_alive(member), "a member of an unusable seen state must not be signalled")
        self.assertFalse(os.path.exists(seen_path))
        hold = self._hold_row(run["id"])
        self.assertIsNotNone(hold, "no persisted hold")
        self.assertEqual(json.loads(hold["value"])["reason"], "invalid_json")
        self.assertIn(f"run {run['id']}: seen.json missing but a persisted seen hold exists", self._meshd_log())
        self.assertEqual(len(self._run_events("run.seen_unusable", run["id"])), 1)
        return task_id, run, member

    def test_deleted_file_after_restart_stays_held_until_forced(self) -> None:
        """ACC: N18, A8 (r4d item 3) - unusable seen.json, the file is deleted, kill -9
        meshd, restart, member alive: the run and `task:<id>` stay held (persisted hold);
        `run release --force-seen-unusable` closes it and clears the persisted hold."""
        task_id, run, member = self._held_after_delete()
        rc, resp = self.harness.cli("run", "release", run["id"], "--force-seen-unusable", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self._run(run["id"])["status"], "lost")
        self.assertIsNone(self._lease(task_id))
        self.assertIsNone(self._hold_row(run["id"]), "force release must clear the persisted hold")
        self.assertTrue(_alive(member), "release must not signal any process")
        os.kill(member, signal.SIGKILL)
        self.assertTrue(self.harness.wait_for(lambda: not _alive(member), timeout=10))
        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        self.assertEqual(self._run(run["id"])["status"], "lost")
        self.assertIsNone(self._hold_row(run["id"]))
        self.doctor_ok()

    def test_deleted_file_after_restart_valid_file_lifts_hold(self) -> None:
        """ACC: N18, A8 (r4d item 3) - the same persisted hold; then a valid seen.json
        listing the member appears: the hold is lifted (persisted state cleared), meshd
        targets and SIGKILLs the member, and only then the run closes and the lease goes."""
        task_id, run, member = self._held_after_delete()
        member_st = get_proc_stat(member)["starttime"]
        doc = {"run_id": run["id"], "pgid": run["pgid"], "sid": run["pgid"],
               "members": [{"pid": member, "starttime": member_st}]}
        seen_path = os.path.join(run["run_dir"], "seen.json")
        with open(seen_path + ".test", "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(seen_path + ".test", seen_path)

        violations: List[str] = []

        def member_gone() -> bool:
            if not self._same_alive(member, member_st):
                return True
            r = self._run(run["id"])
            lease = self._lease(task_id)
            if self._same_alive(member, member_st):
                if r["status"] not in ("starting", "running"):
                    violations.append(f"run {r['status']} while member alive")
                if lease is None:
                    violations.append("task lease released while member alive")
            return False

        self.assertTrue(self.harness.wait_for(member_gone, timeout=40, step=0.1),
                        f"member was never killed; run {dict(self._run(run['id']))}")
        self.assertEqual(violations, [])
        self.assertTrue(self.harness.wait_for(lambda: self._lease(task_id) is None, timeout=20),
                        "task lease not released after the member died")
        self.assertTrue(self.harness.wait_for(
            lambda: self._run(run["id"])["status"] not in ("starting", "running"), timeout=20))
        self.assertIsNone(self._hold_row(run["id"]), "a valid file must clear the persisted hold")
        self.doctor_ok()
