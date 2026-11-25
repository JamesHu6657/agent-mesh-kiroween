"""L4 end-to-end recovery tests with a real ``meshd`` daemon for launch-mode lead turns.

Covers:
1. Daemon ``kill -9`` + dead lead wrapper + live env-less same-group member -> held after
   restart, ``leadturn:<prj>`` lease kept, outbox stays ``delivering``, ``runs_lost == 0``,
   and ``mesh run release --force-seen-unusable`` releases it properly without signalling
   the live member.
2. Daemon ``kill -9`` + live lead wrapper adopted at restart -> wrapper exits while an
   env-less same-group member lives -> member terminated before close, outbox/lease held
   while alive, then closed exit 0 and acked once clear.
"""

import json
import os
import signal
import sys
import time
import unittest
from typing import Any, Dict, List, Optional

from mesh.core.proc import get_proc_stat
from tests.integration.test_lead_turns import create_agent
from tests.recovery.test_recovery import RecoveryBase, _alive


class TestRecoveryLeadSeen(RecoveryBase):
    MESH_PROC_LOCK = "shared"
    mesh_serial = True

    def _pid_file_val(self, path: str) -> Optional[int]:
        lines = self.pid_lines(path)
        return int(lines[0]) if lines else None

    def _guard_pid(self, pid: int) -> None:
        info = get_proc_stat(pid)
        st = info["starttime"] if info else None

        def _cleanup() -> None:
            cur = get_proc_stat(pid)
            if cur is not None and st is not None and cur["starttime"] == st and cur["state"] != "Z":
                try:
                    self._kill_pid(pid)
                except ProcessLookupError:
                    pass

        self.addCleanup(_cleanup)

    def _create_launch_lead_project(self, goal: str, title: str = "L4 Recovery Lead") -> Dict[str, Any]:
        tok_flead = create_agent(self.harness, "flead_l4", adapter="fake", delivery="launch", can_lead=True)
        tok_grok = create_agent(self.harness, "grok_l4", adapter="none", delivery="inbox", capabilities=["intake"])
        self.set_setting("lead.wake_debounce_s", "1")
        self.set_setting("lead.wake_backoff_s", "[1, 1, 1]")
        self.set_setting("kill.grace_s", "2")
        rc, resp = self.harness.cli(
            "intake", "submit", "--title", title, "--goal", goal,
            "--criterion", "C1", "--lead", "flead_l4", token=tok_grok,
        )
        self.assertEqual(rc, 0, resp)
        return {"project_id": resp["data"]["id"], "tok_flead": tok_flead, "tok_grok": tok_grok}

    def _wait_running_lead_run(self, project_id: str, timeout: float = 30.0) -> Any:
        def _find():
            rows = self.q(
                "SELECT * FROM runs WHERE kind = 'lead_turn' AND project_id = ? "
                "ORDER BY started_at ASC, id ASC;",
                (project_id,),
            )
            for r in rows:
                if r["status"] == "running" and r["wrapper_pid"]:
                    return r
            return None

        self.assertTrue(self.harness.wait_for(lambda: _find() is not None, timeout=timeout, step=0.05),
                        "lead turn never started running")
        return _find()

    @unittest.skipIf(
        sys.platform == "win32",
        "Windows Named Job Objects vanish when all handles close without seen.json; "
        "Win32 Job-retained corrupt-seen lead recovery is tested in "
        "test_win_lead_corrupt_seen_retained_job_terminates_and_recovers",
    )
    def test_lead_restart_dead_wrapper_occupied_group_held_and_force_release(self) -> None:
        """ACC: L4 - kill9 meshd, dead lead wrapper and fake agent with live env-less same-group
        member -> restart holds run & leadturn lease, keeps outbox delivering, runs_lost 0;
        ``mesh run release --force-seen-unusable`` closes lost and restores pending outbox
        without killing the member, letting the next lead turn run."""
        mem_pidfile = os.path.join(self.harness.fixtures_dir, "lt-mem-pid")
        mem_trigger = os.path.join(self.harness.fixtures_dir, "lt-mem-trig")
        spec = "FAKE " + json.dumps(
            {"steps": [{"spawn_envless_member": {"path": mem_pidfile, "trigger": mem_trigger}}, {"succeed": {}}]}
        )
        info = self._create_launch_lead_project(spec, title="L4 Dead Wrapper Occupied")
        pid = info["project_id"]
        run = self._wait_running_lead_run(pid)
        self.wait_wrapper_started(run)
        self.assertTrue(self.harness.wait_for(lambda: self._pid_file_val(mem_pidfile) is not None, timeout=20))
        member = self._pid_file_val(mem_pidfile)
        self._guard_pid(member)
        fake_pid = int(get_proc_stat(member)["ppid"])
        self.assertNotEqual(fake_pid, run["wrapper_pid"])
        self._guard_pid(fake_pid)
        wst = int(run["proc_starttime"])

        # Kill meshd cleanly with SIGKILL, remove seen.json so the dead wrapper leaves an
        # unrecorded env-less group occupant, then SIGKILL wrapper and fake agent (no exit.json).
        self.harness.kill9_daemon()
        seen_path = os.path.join(run["run_dir"], "seen.json")
        if os.path.exists(seen_path):
            os.unlink(seen_path)
        os.kill(int(run["wrapper_pid"]), signal.SIGKILL)
        os.kill(fake_pid, signal.SIGKILL)
        self.assertTrue(self.harness.wait_for(
            lambda: ((st := get_proc_stat(int(run["wrapper_pid"]))) is None or st["state"] == "Z"
                     or int(st["starttime"]) != wst) and not _alive(fake_pid),
            timeout=10,
        ))
        self.assertTrue(_alive(member))

        self.harness.restart_daemon()
        rec = self.recovered()[-1]
        self.assertEqual(rec["runs_lost"], 0, rec)
        self.assertGreaterEqual(rec["runs_terminating"], 1, rec)

        run_after = self.q1("SELECT * FROM runs WHERE id = ?;", (run["id"],))
        self.assertEqual(run_after["status"], "running")
        self.assertIsNotNone(self.q1("SELECT 1 FROM leases WHERE key = ? AND run_id = ?;",
                                     (f"leadturn:{pid}", run["id"])))
        self.assertEqual(
            [r["status"] for r in self.q("SELECT status FROM outbox WHERE project_id = ? AND channel = 'lead_wake';",
                                        (pid,))],
            ["delivering"],
        )
        self.assertIsNotNone(self.q1("SELECT 1 FROM meta WHERE key = ?;", (f"seen_hold:{run['id']}",)))
        self.assertEqual(len(self.q("SELECT 1 FROM events WHERE type = 'run.seen_unusable' AND run_id = ?;",
                                    (run["id"],))), 1)
        self.assertTrue(_alive(member))

        # Only direct user can release; agent token fails rc 4.
        rc, resp = self.harness.cli("run", "release", run["id"], "--force-seen-unusable", token=info["tok_grok"])
        self.assertEqual(rc, 4, resp)
        self.assertEqual(resp["error"]["code"], "CAPABILITY_MISSING")

        # Wait until the daemon has finished terminating any MESH_RUN_ID carrier (e.g. fake_agent)
        # so the run is held purely because its seen state is unusable with the env-less occupant alive.
        self.assertTrue(
            self.harness.wait_for(
                lambda: len([
                    e for e in self.q("SELECT * FROM events WHERE run_id = ?;", (run["id"],))
                    if e["type"] == "run.seen_unusable"
                ]) >= 1 and self.harness.cli("run", "release", run["id"], "--force-seen-unusable", "--dry-run", user=True)[0] == 0,
                timeout=15.0,
                step=0.1,
            ),
            f"run never entered seen_held plateau: events={self.q('SELECT type, payload_json FROM events WHERE run_id = ?;', (run['id'],))}",
        )

        # Update goal to succeed quickly after release creates next turn, then force release.
        rc, resp = self.harness.cli("project", "update", pid, "--goal", 'FAKE {"succeed":{}}', user=True)
        self.assertEqual(rc, 0, resp)

        rc, resp = self.harness.cli("run", "release", run["id"], "--force-seen-unusable", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertIs(resp["data"]["released"], True)
        self.assertEqual(resp["data"]["status"], "lost")

        # Wait for next lead turn (triggered by restored pending lead_wake outbox) or verify closed state.
        closed_old = self.q1("SELECT status, end_reason FROM runs WHERE id = ?;", (run["id"],))
        self.assertEqual((closed_old["status"], closed_old["end_reason"]), ("lost", "lost_on_restart"))
        self.assertIsNone(self.q1("SELECT 1 FROM meta WHERE key = ?;", (f"seen_hold:{run['id']}",)))
        self.assertEqual(len(self.q("SELECT 1 FROM events WHERE type = 'run.seen_release_forced' AND run_id = ?;",
                                    (run["id"],))), 1)
        self.assertTrue(_alive(member), "run release must not signal live member")

        # Clean up member and confirm next lead turn runs cleanly to exit 0.
        open(mem_trigger, "w").close()
        self.assertTrue(self.harness.wait_for(
            lambda: any(
                r["id"] != run["id"] and r["status"] == "exited"
                for r in self.q("SELECT * FROM runs WHERE kind = 'lead_turn' AND project_id = ?;", (pid,))
            ),
            timeout=25.0,
        ))
        self.doctor_ok()

    @unittest.skipUnless(
        sys.platform == "win32",
        "Win32 Named Run Job counterpart for lead-turn corrupt seen.json recovery",
    )
    def test_win_lead_corrupt_seen_retained_job_terminates_and_recovers(self) -> None:
        """Win32 L4 counterpart: retained Named Run Job + dead wrapper + corrupt seen.json ->
        emits run.seen_unusable, enforces CAPABILITY_MISSING on agent run release, terminates
        Job occupant instead of blocking in seen_held, restores lead_wake outbox, and runs
        the next lead turn to completion."""
        from mesh.platform import proc as plat_proc

        mem_pidfile = os.path.join(self.harness.fixtures_dir, "lt-win-mem-pid")
        mem_trigger = os.path.join(self.harness.fixtures_dir, "lt-win-mem-trig")
        spec = "FAKE " + json.dumps(
            {"steps": [{"spawn_envless_member": {"path": mem_pidfile, "trigger": mem_trigger}}, {"succeed": {}}]}
        )
        info = self._create_launch_lead_project(spec, title="L4 Win Corrupt Seen Recovery")
        pid = info["project_id"]
        run = self._wait_running_lead_run(pid)
        self.wait_wrapper_started(run)
        self.assertTrue(self.harness.wait_for(lambda: self._pid_file_val(mem_pidfile) is not None, timeout=20))
        member = self._pid_file_val(mem_pidfile)
        self._guard_pid(member)
        fake_pid = int(get_proc_stat(member)["ppid"])
        self.assertNotEqual(fake_pid, run["wrapper_pid"])
        self._guard_pid(fake_pid)

        # Keep a query handle open so the Named Run Job persists across daemon + wrapper kill.
        job = plat_proc.open_run_job(self.harness.home, run["id"], terminate=False)
        self.assertIsNotNone(job)
        self.addCleanup(job.close)

        self.harness.kill9_daemon()
        seen_path = os.path.join(run["run_dir"], "seen.json")
        with open(seen_path, "w", encoding="utf-8") as f:
            f.write("{corrupt")
        self._kill_pid(int(run["wrapper_pid"]))
        self._kill_pid(fake_pid)
        self.assertTrue(_alive(member))

        self.harness.restart_daemon()
        rec = self.recovered()[-1]
        self.assertGreaterEqual(rec["runs_terminating"], 1, rec)

        # Update goal immediately while old run still holds leadturn:<prj> lease during kill.grace_s.
        rc, resp = self.harness.cli("project", "update", pid, "--goal", 'FAKE {"succeed":{}}', user=True)
        self.assertEqual(rc, 0, resp)

        ev = self.q("SELECT payload_json FROM events WHERE type = 'run.seen_unusable' AND run_id = ?;", (run["id"],))
        self.assertEqual(len(ev), 1, ev)
        self.assertEqual(json.loads(ev[0]["payload_json"])["reason"], "invalid_json")

        # Capability check rejects non-user tokens regardless of seen_held state.
        rc, resp = self.harness.cli("run", "release", run["id"], "--force-seen-unusable", token=info["tok_grok"])
        self.assertEqual(rc, 4, resp)
        self.assertEqual(resp["error"]["code"], "CAPABILITY_MISSING")

        # Win32 Job members are proven targets (seen_held == False), so operator override is unnecessary.
        rc, resp = self.harness.cli("run", "release", run["id"], "--force-seen-unusable", "--dry-run", user=True)
        self.assertEqual(rc, 6, resp)
        self.assertEqual(resp["error"]["details"].get("reason"), "not_seen_held")

        # Daemon terminates env-less Job occupant, closes old run lost, clears hold, and runs next turn.
        self.assertTrue(
            self.harness.wait_for(
                lambda: not _alive(member)
                and self.q1("SELECT status FROM runs WHERE id = ?;", (run["id"],))["status"] == "lost",
                timeout=20.0,
            )
        )
        closed_old = self.q1("SELECT status, end_reason FROM runs WHERE id = ?;", (run["id"],))
        self.assertEqual((closed_old["status"], closed_old["end_reason"]), ("lost", "lost_on_restart"))
        self.assertIsNone(self.q1("SELECT 1 FROM meta WHERE key = ?;", (f"seen_hold:{run['id']}",)))
        self.assertTrue(
            self.harness.wait_for(
                lambda: any(
                    r["id"] != run["id"] and r["status"] == "exited"
                    for r in self.q("SELECT * FROM runs WHERE kind = 'lead_turn' AND project_id = ?;", (pid,))
                ),
                timeout=25.0,
            )
        )
        self.assertEqual(
            [r["status"] for r in self.q("SELECT status FROM outbox WHERE project_id = ? AND channel = 'lead_wake';", (pid,))],
            ["acked"],
        )
        self.doctor_ok()

    def test_lead_adopted_exits_after_restart_terminates_envless_member(self) -> None:
        """ACC: L4 - kill9 meshd while lead wrapper lives with env-less member -> restart adopts;
        releasing wrapper lets it exit 0; watch terminates member before acking outbox or
        releasing leadturn lease, then closes exit 0 cleanly."""
        mem_pidfile = os.path.join(self.harness.fixtures_dir, "lt-adm-pid")
        wrap_trigger = os.path.join(self.harness.fixtures_dir, "lt-adw-trig")
        spec = "FAKE " + json.dumps(
            {"steps": [{"spawn_envless_member": {"path": mem_pidfile}}, {"wait_release": {"path": wrap_trigger}}, {"succeed": {}}]}
        )
        info = self._create_launch_lead_project(spec, title="L4 Adopted Exit Leftover")
        pid = info["project_id"]
        run = self._wait_running_lead_run(pid)
        self.wait_wrapper_started(run)
        self.assertTrue(self.harness.wait_for(lambda: self._pid_file_val(mem_pidfile) is not None, timeout=20))
        member = self._pid_file_val(mem_pidfile)
        self._guard_pid(member)

        self.harness.kill9_daemon()
        self.assertTrue(_alive(run["wrapper_pid"]))
        self.assertTrue(_alive(member))
        self.harness.restart_daemon()
        adopted_evs = self.q("SELECT * FROM events WHERE type = 'run.adopted' AND run_id = ?;", (run["id"],))
        self.assertEqual(len(adopted_evs), 1)
        self.assertTrue(_alive(member))

        # Let wrapper exit 0 while member continues running -> watch terminates member first.
        open(wrap_trigger, "w").close()
        self.assertTrue(
            self.harness.wait_for(
                lambda: self.q1("SELECT status FROM runs WHERE id = ?;", (run["id"],))["status"] not in ("starting", "running"),
                timeout=25.0,
            ),
            f"run={dict(self.q1('SELECT * FROM runs WHERE id = ?;', (run['id'],)))} "
            f"wrap_alive={_alive(run['wrapper_pid'])} mem_alive={_alive(member)} "
            f"events={[(e['type'], e['payload_json']) for e in self.q('SELECT type, payload_json FROM events WHERE run_id = ?;', (run['id'],))]}",
        )
        final_run = self.q1("SELECT * FROM runs WHERE id = ?;", (run["id"],))
        self.assertEqual(final_run["status"], "exited", dict(final_run))
        self.assertFalse(_alive(member), "env-less member survived adopted lead turn close")
        self.assertIsNone(self.q1("SELECT 1 FROM leases WHERE key = ?;", (f"leadturn:{pid}",)))
        self.assertEqual(
            [r["status"] for r in self.q("SELECT status FROM outbox WHERE project_id = ? AND channel = 'lead_wake';",
                                        (pid,))],
            ["acked"],
        )
        self.doctor_ok()


if __name__ == "__main__":
    unittest.main()
