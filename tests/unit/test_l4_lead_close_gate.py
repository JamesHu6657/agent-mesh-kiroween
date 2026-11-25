"""L4 real-process tests: seen tracking and close-safety gate for launch-mode lead turns.

Runs real ``mesh.runwrap`` wrappers, launcher subprocesses, env-less group members,
and SIGTERM-ignoring ``setsid`` escapees. Faults and boundaries that cannot be triggered
deterministically from outside are injected selectively (``/proc`` errno failures via
``os.listdir`` / ``supervisor._scan_group``, a wrapper-exit scheduling hook,
``lead_turns.get_adapter`` command override, and ``_close_run`` closing-gate hold).
"""

import contextlib
import errno
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict
import unittest
from unittest import mock

from mesh.adapters.base import CommandSpec
from mesh.adapters.registry import get_adapter as real_get_adapter
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.proc import get_proc_stat, is_tgid_alive
from mesh.daemon import children, lead_turns, recovery, supervisor, terminate
from mesh.platform.paths import temp_root

# Real /proc scans: hold the runner's host-wide /proc lock shared (tests/run_tier.py).
MESH_PROC_LOCK = "shared"

_LAUNCHER = r"""
import os, subprocess, sys
w = subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
print(w.stdout.readline().decode().strip(), flush=True)
os._exit(0)
"""
_WRAPPER = r"""
import os, subprocess, sys, time
m = subprocess.Popen([sys.argv[1], "120"], env={}, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(os.getpid(), m.pid, flush=True)
while not os.path.exists(sys.argv[2]):
    time.sleep(0.01)
os._exit(0)
"""
_WRAPPER_IGNORE_TERM_MEMBER = r"""
import os, signal, subprocess, sys, time
m = subprocess.Popen(
    [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"],
    env={}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(os.getpid(), m.pid, flush=True)
while not os.path.exists(sys.argv[2]):
    time.sleep(0.01)
os._exit(0)
"""
_ESCAPEE_IGNORE_TERM = r"""
import subprocess, sys
e = subprocess.Popen(
    [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"],
    env={}, start_new_session=True, stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(e.pid, flush=True)
"""


def _alive(pid, starttime=None):
    info = get_proc_stat(pid)
    if info is None or not is_tgid_alive(pid, info):
        return False
    return starttime is None or int(info["starttime"]) == int(starttime)


def _kill_same(pid, starttime):
    if sys.platform == "win32":
        terminate.signal_if_same(pid, starttime, terminate.SIGKILL)
        return
    if _alive(pid, starttime):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _wait(pred, timeout, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class TestLeadTurnAdoptSeenAndCloseGate(unittest.TestCase):
    """ACC: L4, N18, A8 - seen tracking & close-safety gate equality for lead turns."""

    mesh_serial = True

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-l4-gate-", dir=temp_root())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "logs"), mode=0o700)
        os.makedirs(os.path.join(self.home, "runs"), mode=0o700)
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)
        self.seq = 0
        self.addCleanup(self._cleanup_globals)
        self._cleanup_globals()

    def _cleanup_globals(self) -> None:
        terminate.forget_all() if hasattr(terminate, "forget_all") else None
        for r in getattr(self, "_run_ids", ()):
            supervisor._CLOSE_REFUSED_LOGGED.pop(r, None)
            terminate.forget(r)
            supervisor._CLEAR.pop(r, None)
            supervisor._SEEN_EVENT_PENDING.pop(r, None)
            supervisor._SEEN_EVENT_DONE.discard(r)
            supervisor._SPAWN_ABORTS.pop(r, None)
            proc = supervisor._WRAPPERS.pop(r, None)
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                children.unhold(proc.pid)
                children.unregister(proc.pid)
        supervisor._evict_seen(set())
        recovery.DEFERRED.clear()
        recovery._HANDED_OVER.clear()
        lead_turns._PENDING_INTENT_UNLINKS.clear()
        with lead_turns._QUEUED_MERGES_LOCK:
            lead_turns._QUEUED_MERGES.clear()

    def _next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}_l4_{os.getpid()}_{self.seq}"

    def _seed_project(self, project_id: str = "prj_l4", agent_id: str = "agt_l4",
                      assignment_id: str = "las_l4") -> str:
        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT OR IGNORE INTO principals (id, kind, name, created_at) VALUES ('usr_l4','user','user',?);", (now,))
        ex("INSERT OR IGNORE INTO principals (id, kind, name, created_at) VALUES (?,'agent',?,?);",
           (agent_id, f"name_{agent_id}", now))
        ex("INSERT OR IGNORE INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
           "VALUES (?,'fake','launch',1,1,?,?);", (agent_id, now, now))
        ex("INSERT OR IGNORE INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES (?,'L4P','L4 Goal','active','usr_l4','direct',?,?);", (project_id, now, now))
        ex("INSERT OR IGNORE INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
           "VALUES (?,?,?,'usr_l4','direct',?);", (assignment_id, project_id, agent_id, now))
        ex("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
           "VALUES ('kill.grace_s', '1', ?, 'test');", (now,))
        ex("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
           "VALUES ('lead.wake_backoff_s', '[1, 1, 1]', ?, 'test');", (now,))
        ex("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
           "VALUES ('outbox.max_attempts', '3', ?, 'test');", (now,))
        self.conn.commit()
        return project_id

    def _insert_lead_run(
        self,
        *,
        project_id: str = "prj_l4",
        agent_id: str = "agt_l4",
        assignment_id: str = "las_l4",
        status: str = "running",
        wpid=None,
        pgid=None,
        wst=None,
        deadline_at=None,
        boot_id: str = "boot_old",
        outbox_status: str = "delivering",
    ):
        run_id = self._next_id("run")
        out_id = self._next_id("out")
        self._run_ids = getattr(self, "_run_ids", []) + [run_id]
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        now = now_iso_utc()
        exp = now_iso_utc(time.time() + 3600)
        with transaction(self.conn):
            ev = record_event(
                self.conn, type_="project.note", actor_id="usr_l4", actor_kind="user",
                via="direct", project_id=project_id, payload={"text": f"wake {run_id}"},
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at, lead_assignment_id) "
                "VALUES (?, 'lead_turn', ?, ?, 0, 1, 'fake', ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (run_id, project_id, agent_id, status, boot_id, wpid, pgid, wst, run_dir, now, deadline_at,
                 assignment_id),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, "
                "expires_at, heartbeat_at) VALUES (?, ?, 'meshd', ?, ?, 1, ?, ?, ?);",
                (f"leadturn:{project_id}", run_id, boot_id, agent_id, now, exp, now),
            )
            self.conn.execute(
                "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, "
                "payload_json, status, attempts, delivering_run_id, next_attempt_at, created_at) "
                "VALUES (?, ?, ?, 'lead_wake', ?, ?, 'lead.wake', '{}', ?, 0, ?, ?, ?);",
                (out_id, ev["seq"], agent_id, f"dk_{run_id}", project_id, outbox_status,
                 run_id if outbox_status == "delivering" else None, now, now),
            )
        return run_id, run_dir, out_id

    def _insert_run_token(self, run_id: str, agent_id: str, project_id: str, assignment_id: str) -> str:
        cred_id = self._next_id("cred")
        now = now_iso_utc()
        scope = {"role": "lead", "run_id": run_id, "project_id": project_id, "lead_assignment_id": assignment_id}
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES (?, ?, 'run_token', ?, ?, ?, ?, ?);",
                (cred_id, agent_id, f"lk_{run_id}", b"hash", json.dumps(scope), now, now_iso_utc(time.time() + 3600)),
            )
        return cred_id

    def _start_real_runwrap_lead(self, script_body: str, project_id: str = "prj_l4"):
        self._seed_project(project_id=project_id)
        with transaction(self.conn):
            ev = record_event(
                self.conn, type_="project.note", actor_id="usr_l4", actor_kind="user",
                via="direct", project_id=project_id, payload={"text": "ping"},
            )
            out_id = self._next_id("out")
            now = now_iso_utc()
            self.conn.execute(
                "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, "
                "payload_json, status, attempts, next_attempt_at, created_at) "
                "VALUES (?, ?, 'agt_l4', 'lead_wake', ?, ?, 'lead.wake', '{}', 'pending', 0, ?, ?);",
                (out_id, ev["seq"], f"dk_{out_id}", project_id, now, now),
            )
        runner = os.path.join(self.tmp, f"runner_{out_id}.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(script_body)

        def get_adapter(adapter_id, testing_mode=False, options=None):
            a = real_get_adapter(adapter_id, testing_mode=testing_mode, options=options)
            a.build = lambda req: CommandSpec(argv=[sys.executable, runner], cwd=req.workspace)
            return a

        lead = lead_turns.get_project_active_lead(self.conn, project_id)
        msgs = self.conn.execute("SELECT * FROM outbox WHERE project_id = ? AND status = 'pending';",
                                 (project_id,)).fetchall()
        with mock.patch.object(lead_turns, "get_adapter", get_adapter), \
                mock.patch.object(supervisor, "_log_meshd"):
            rid = lead_turns.start_lead_turn(self.conn, self.home, project_id, lead, msgs, testing_mode=True)
        self.assertIsNotNone(rid)
        self._run_ids = getattr(self, "_run_ids", []) + [rid]
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (rid,)).fetchone()
        wpid = int(run["wrapper_pid"])
        wst = int(run["proc_starttime"])

        def _cleanup_run() -> None:
            for p, st in supervisor._scan_group(wpid).items():
                _kill_same(p, st)
            _kill_same(wpid, wst)

        self.addCleanup(_cleanup_run)
        return rid, out_id, run

    def _status(self, run_id: str) -> str:
        return self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()[0]

    def _lease(self, project_id: str = "prj_l4"):
        return self.conn.execute("SELECT * FROM leases WHERE key = ?;", (f"leadturn:{project_id}",)).fetchone()

    def _outbox(self, out_id: str):
        return self.conn.execute("SELECT * FROM outbox WHERE id = ?;", (out_id,)).fetchone()

    def _assert_marker(self, run_dir: str, reason: str) -> None:
        with open(os.path.join(run_dir, "seen.json"), "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual((doc.get("mesh_seen_marker"), doc.get("reason")), ("unusable", reason), doc)
        self.assertNotIn("members", doc)

    @unittest.skipUnless(sys.platform != "win32", "POSIX setsid/env-stripped carrier PGID seen_held and _scan_group (Windows Job Object close-gate covered in test_win_close_gate_jobs)")
    def test_lead_restart_dead_wrapper_envless_member_held_via_gate(self) -> None:
        """Restart + dead lead wrapper + env-less same-group process -> held via close_gate,
        leadturn lease kept, outbox stays 'delivering', member alive, runs_lost 0."""
        self._seed_project()
        trigger = os.path.join(self.tmp, "trig_dead")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True,
        )
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)

        # Wrapper exits cleanly without writing exit.json or seen.json (simulating crash/death while meshd was down).
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        self.assertTrue(_alive(member, mst))

        run_id, run_dir, out_id = self._insert_lead_run(wpid=wrapper, pgid=wrapper, wst=wst)
        with open(os.path.join(run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wrapper, "starttime": wst}, f)

        with mock.patch.object(supervisor, "_log_meshd"), mock.patch.object(recovery, "_log"):
            summary = recovery.recover(self.conn, self.home, "boot_new", testing_mode=True)
        self.assertEqual(summary["runs_lost"], 0, summary)
        self.assertEqual(summary["runs_terminating"], 1, summary)
        self.assertIn(self._status(run_id), ("starting", "running"))
        self.assertIsNotNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "delivering")
        self.assertTrue(_alive(member, mst))
        self._assert_marker(run_dir, supervisor.WRAPPER_GONE_OCCUPIED)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM meta WHERE key = ?;",
                                               (supervisor.seen_hold_key(run_id),)).fetchone())

        # Watch passes while member lives: still held, outbox still delivering.
        with mock.patch.object(supervisor, "_log_meshd"):
            for _ in range(3):
                run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
                supervisor._watch_one(self.conn, self.home, run, testing_mode=True)
                self.assertIn(self._status(run_id), ("starting", "running"))
                self.assertIsNotNone(self._lease())
                self.assertEqual(self._outbox(out_id)["status"], "delivering")
        self.assertTrue(_alive(member, mst))

        # Once the member exits, the close-safety gate allows close and outbox is released.
        _kill_same(member, mst)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(
                _wait(
                    lambda: (
                        supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True),
                        supervisor.confirm_pending_exits(self.conn, self.home, testing_mode=True),
                        self._status(run_id) == "lost",
                    )[2],
                    10.0,
                )
            )
        self.assertIsNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "pending")

    @unittest.skipUnless(sys.platform != "win32", "POSIX setsid/env-stripped carrier PGID seen_held and _scan_group (Windows Job Object close-gate covered in test_win_close_gate_jobs)")
    def test_lead_adopted_wrapper_exits_before_first_watch_terminates_envless_member(self) -> None:
        """P1-1 for lead turns: live wrapper adopted, exits right after adopt before first watch;
        adopt-time seen.json drives two-scan termination before lease release or outbox requeue."""
        self._seed_project()
        trigger = os.path.join(self.tmp, "trig_adopt")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True,
        )
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)

        run_id, run_dir, out_id = self._insert_lead_run(wpid=wrapper, pgid=wrapper, wst=wst)
        with open(os.path.join(run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wrapper, "starttime": wst}, f)

        with mock.patch.object(recovery, "_log"), mock.patch.object(supervisor, "_log_meshd"):
            out = recovery._recover_run(self.conn, self.home,
                                        {"id": run_id, "lease_holder": "boot_old"}, "boot_new", testing_mode=True)
        self.assertEqual(out, "adopted")
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        self.assertTrue(_alive(member, mst))

        violations = []
        observed_alive = 0
        terminated_seen = False
        end = time.monotonic() + 20.0
        while time.monotonic() < end:
            alive = _alive(member, mst)
            if alive:
                observed_alive += 1
            st = self._status(run_id)
            if alive and st in ("exited", "lost", "killed"):
                violations.append(f"closed {st} while member alive")
            if alive and self._lease() is None:
                violations.append("lease released while member alive")
            if alive and self._outbox(out_id)["status"] != "delivering":
                violations.append("outbox released while member alive")
            if terminate.get(run_id) is not None:
                terminated_seen = True
            if st not in ("starting", "running"):
                break
            run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
            with mock.patch.object(supervisor, "_log_meshd"):
                supervisor._watch_one(self.conn, self.home, run, testing_mode=True)
            time.sleep(0.05)

        self.assertEqual(violations, [])
        self.assertGreater(observed_alive, 0)
        self.assertTrue(terminated_seen, "member termination was never started")
        self.assertFalse(_alive(member, mst))
        self.assertEqual(self._status(run_id), "lost")
        self.assertIsNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "pending")

    @unittest.skipUnless(sys.platform != "win32", "POSIX setsid/env-stripped carrier PGID seen_held and _scan_group (Windows Job Object close-gate covered in test_win_close_gate_jobs)")
    def test_lead_wrapper_exits_during_adoption_scan_fails_closed(self) -> None:
        """Wrapper exits mid-adoption scan -> adopted_uncertain, adopt_scan_untrusted marker
        keeping prior_members, hold persisted, leadturn lease and delivering outbox kept."""
        self._seed_project()
        trigger = os.path.join(self.tmp, "trig_scan")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True,
        )
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)

        run_id, run_dir, out_id = self._insert_lead_run(wpid=wrapper, pgid=wrapper, wst=wst)
        with open(os.path.join(run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wrapper, "starttime": wst}, f)

        real_scan = supervisor._scan_group
        fired = {"done": False}

        def scan_then_exit(pgid):
            res = real_scan(pgid)
            if not fired["done"] and int(pgid) == wrapper:
                fired["done"] = True
                open(trigger, "w").close()
                assert _wait(lambda: not _alive(wrapper, wst), 10)
            return res

        with mock.patch.object(supervisor, "_scan_group", scan_then_exit), \
                mock.patch.object(recovery, "_log"), mock.patch.object(supervisor, "_log_meshd"):
            out = recovery._recover_run(self.conn, self.home,
                                        {"id": run_id, "lease_holder": "boot_old"}, "boot_new", testing_mode=True)
        self.assertEqual(out, "adopted_uncertain")
        self._assert_marker(run_dir, supervisor.ADOPT_SCAN_UNTRUSTED)
        self.assertIn(self._status(run_id), ("starting", "running"))
        self.assertIsNotNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "delivering")
        self.assertTrue(_alive(member, mst))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM meta WHERE key = ?;",
                                               (supervisor.seen_hold_key(run_id),)).fetchone())
        self.assertEqual(len(self.conn.execute("SELECT 1 FROM events WHERE type = 'run.seen_unusable' AND run_id = ?;",
                                               (run_id,)).fetchall()), 1)

        # Clean up member -> closes once empty.
        _kill_same(member, mst)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(
                _wait(
                    lambda: (
                        supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True),
                        supervisor.confirm_pending_exits(self.conn, self.home, testing_mode=True),
                        self._status(run_id) == "lost",
                    )[2],
                    10.0,
                )
            )
        self.assertIsNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "pending")

    @unittest.skipUnless(sys.platform != "win32", "POSIX setsid/env-stripped carrier PGID seen_held and _scan_group (Windows Job Object close-gate covered in test_win_close_gate_jobs)")
    def test_lead_crash_after_adopt_pending_marker_before_scan_held_after_restart(self) -> None:
        """Crash after prepare_adopt_seen (adopt_pending marker, DB pgid NULL in spawn window)
        + dead wrapper -> next boot recover() reads wrapper.json, holds adopt_pending, keeps lease & outbox."""
        self._seed_project()
        trigger = os.path.join(self.tmp, "trig_pend")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True,
        )
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)

        # Spawn-window lead run (wrapper_pid/pgid NULL in DB; wrapper.json exists).
        run_id, run_dir, out_id = self._insert_lead_run(status="starting", wpid=None, pgid=None, wst=None)
        with open(os.path.join(run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wrapper, "starttime": wst}, f)

        run_row = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        with mock.patch.object(supervisor, "_log_seen"):
            supervisor.prepare_adopt_seen(run_row, self.home)
        self._assert_marker(run_dir, supervisor.ADOPT_PENDING)
        # Simulated crash before adopt commit: drop in-memory state and kill wrapper.
        self._cleanup_globals()
        self._run_ids = [run_id]
        _kill_same(wrapper, wst)
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 10))
        self.assertTrue(_alive(member, mst))

        with mock.patch.object(recovery, "_log"), mock.patch.object(supervisor, "_log_meshd"):
            summary = recovery.recover(self.conn, self.home, "boot_new", testing_mode=True)
        self.assertEqual(summary["runs_lost"], 0, summary)
        self.assertEqual(summary["runs_terminating"], 1, summary)
        self.assertIn(self._status(run_id), ("starting", "running"))
        self.assertIsNotNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "delivering")
        self.assertTrue(_alive(member, mst))

        _kill_same(member, mst)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(
                _wait(
                    lambda: (
                        supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True),
                        supervisor.confirm_pending_exits(self.conn, self.home, testing_mode=True),
                        self._status(run_id) == "lost",
                    )[2],
                    10.0,
                )
            )
        self.assertIsNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "pending")

    @unittest.skipUnless(sys.platform != "win32", "POSIX setsid/env-stripped carrier PGID seen_held and _scan_group (Windows Job Object close-gate covered in test_win_close_gate_jobs)")
    def test_lead_exit0_with_in_group_member_and_actions_held_then_closes_ok(self) -> None:
        """Real mesh.runwrap lead turn spawns env-less same-group member and exits 0 with
        mesh-actions -> actions do NOT run and outbox/lease stay held while member lives; once
        terminated & clear, mesh-actions execute once, outbox acks, run closes exited 0."""
        member_pidfile = os.path.join(self.tmp, "lt_member.pid")
        member_script = os.path.join(self.tmp, "lt_member_child.py")
        with open(member_script, "w", encoding="utf-8") as mf:
            mf.write(
                "import os, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"open({member_pidfile!r} + '.t', 'w').write(str(os.getpid()))\n"
                f"os.rename({member_pidfile!r} + '.t', {member_pidfile!r})\n"
                "time.sleep(120)\n"
            )
        script = (
            "import json, subprocess, sys\n"
            "m = subprocess.Popen(\n"
            f"    [sys.executable, {member_script!r}],\n"
            "    env={}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            ")\n"
            "print('```mesh-actions')\n"
            "print(json.dumps([{'cmd':'project.note','args':{'note':'executed after gate'}}]))\n"
            "print('```')\n"
        )
        rid, out_id, run = self._start_real_runwrap_lead(script)
        self.assertTrue(_wait(lambda: os.path.exists(member_pidfile), 15), "member did not start")
        with open(member_pidfile, "r", encoding="utf-8") as f:
            mpid = int(f.read().strip())
        mst = get_proc_stat(mpid)["starttime"]
        self.addCleanup(_kill_same, mpid, mst)
        self.assertTrue(_wait(lambda: os.path.exists(os.path.join(run["run_dir"], "exit.json")), 15), "exit.json missing")

        actions_early = []
        outbox_early = []
        lease_early = []
        end = time.monotonic() + 25.0
        with mock.patch.object(supervisor, "_log_meshd"):
            while time.monotonic() < end:
                if _alive(mpid, mst):
                    actions_early.append(len(self.conn.execute(
                        "SELECT 1 FROM events WHERE type = 'project.note' AND via = 'run_token';"
                    ).fetchall()))
                    outbox_early.append(self._outbox(out_id)["status"])
                    lease_early.append(self._lease() is not None)
                supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True)
                supervisor.advance_terminations(self.conn, self.home, testing_mode=True)
                if self._status(rid) not in ("starting", "running"):
                    break
                time.sleep(0.05)

        self.assertGreaterEqual(len(actions_early), 1, "member exited before hold window was observed")
        self.assertEqual(set(actions_early), {0}, f"actions ran prematurely: {actions_early}")
        self.assertEqual(set(outbox_early), {"delivering"}, f"outbox acked prematurely: {outbox_early}")
        self.assertTrue(all(lease_early), "leadturn lease released while member lived")
        self.assertEqual(len(self.conn.execute(
            "SELECT 1 FROM events WHERE type = 'run.leftovers_terminated' AND run_id = ?;", (rid,)
        ).fetchall()), 1)
        self.assertFalse(_alive(mpid, mst))
        self.assertEqual(self._status(rid), "exited")
        self.assertIsNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "acked")
        notes = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'project.note' AND via = 'run_token';"
        ).fetchall()
        self.assertEqual(len(notes), 1)
        self.assertEqual(json.loads(notes[0]["payload_json"])["text"], "executed after gate")

    @unittest.skipUnless(sys.platform != "win32", "POSIX setsid/env-stripped carrier PGID seen_held and _scan_group (Windows Job Object close-gate covered in test_win_close_gate_jobs)")
    def test_lead_finish_close_held_prevents_actions_merges_and_outbox_leak(self) -> None:
        """If close_gate holds inside finish_lead_turn_committed, actions never execute,
        _QUEUED_MERGES/_PENDING_INTENT_UNLINKS stay clean, outbox stays delivering, lease kept;
        once member ends, watch closes exit 0, executes the action once, and acks outbox."""
        self._seed_project()
        trigger = os.path.join(self.tmp, "trig_finish_held")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True,
        )
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))

        run_id, run_dir, out_id = self._insert_lead_run(wpid=wrapper, pgid=wrapper, wst=wst)
        self._insert_run_token(run_id, "agt_l4", "prj_l4", "las_l4")
        with open(os.path.join(run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wrapper, "starttime": wst}, f)
        exit_info = {"exit_code": 0, "signal": None, "ended_at": now_iso_utc()}
        with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as f:
            json.dump(exit_info, f)
        with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"project.note","args":{"note":"must run once after hold"}}]\n```\n')

        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        with mock.patch.object(lead_turns, "execute_lead_actions", wraps=lead_turns.execute_lead_actions) as exec_spy:
            with self.assertRaises(supervisor.CloseHeld):
                lead_turns.finish_lead_turn_committed(self.conn, self.home, run, exit_info, testing_mode=True)
        exec_spy.assert_not_called()
        self.assertEqual(lead_turns._QUEUED_MERGES, [])
        self.assertEqual(lead_turns._PENDING_INTENT_UNLINKS, [])
        self.assertEqual(self._status(run_id), "running")
        self.assertIsNotNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "delivering")

        # Hold via watch while member lives, then end member -> exactly one action & exited 0!
        with mock.patch.object(supervisor, "_log_meshd"):
            supervisor._watch_one(self.conn, self.home, run, testing_mode=True)
        self.assertEqual(self._status(run_id), "running")
        self.assertEqual(self.conn.execute("SELECT 1 FROM events WHERE type = 'project.note' AND via = 'run_token';").fetchall(), [])

        _kill_same(member, mst)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(
                _wait(
                    lambda: (
                        supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True),
                        supervisor.confirm_pending_exits(self.conn, self.home, testing_mode=True),
                        self._status(run_id) == "exited",
                    )[2],
                    10.0,
                )
            )
        r_done = self.conn.execute("SELECT status, exit_code FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertEqual((r_done["status"], r_done["exit_code"]), ("exited", 0))
        self.assertIsNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "acked")
        action_evs = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'lead.action_result' AND run_id = ?;", (run_id,)
        ).fetchall()
        self.assertEqual(len(action_evs), 1, action_evs)
        self.assertIs(json.loads(action_evs[0]["payload_json"])["ok"], True)
        notes = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'project.note' AND via = 'run_token' AND project_id = 'prj_l4';"
        ).fetchall()
        self.assertEqual(len(notes), 1)
        self.assertEqual(json.loads(notes[0]["payload_json"])["text"], "must run once after hold")

    @unittest.skipUnless(sys.platform != "win32", "POSIX setsid/env-stripped carrier PGID seen_held and _scan_group (Windows Job Object close-gate covered in test_win_close_gate_jobs)")
    def test_lead_finish_second_close_gate_hold_rolls_back_queued_merges(self) -> None:
        """P2-2(b) / r2 P3-1 / r6 P3-1: CloseHeld raised by the real closing close_gate
        inside _close_run (after a real live process enters seen.json during execute_lead_actions
        once the pre-settle gate passed) rolls back _QUEUED_MERGES for the run and commits no
        lead.action_result; retrying once clear queues exactly one merge."""
        self._seed_project(project_id="prj_qm", agent_id="agt_qm", assignment_id="las_qm")
        now = now_iso_utc()
        self.conn.execute("UPDATE projects SET repo_path = ? WHERE id = 'prj_qm';", (self.home,))
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
            "branch, workspace_path, created_by, created_at, updated_at) "
            "VALUES ('tsk_qm', 'prj_qm', 't', 's', 'code', 'accepted', 'edit', 600, 3, 'task/tsk_qm', ?, 'usr_l4', ?, ?);",
            (self.home, now, now),
        )
        probe = subprocess.Popen([sys.executable, "-c", "pass"])
        wpid = probe.pid
        wst = (get_proc_stat(wpid) or {}).get("starttime") or 1
        probe.wait()
        self.assertTrue(_wait(lambda: not _alive(wpid, wst), 10))

        run_id, run_dir, out_id = self._insert_lead_run(
            project_id="prj_qm", agent_id="agt_qm", assignment_id="las_qm",
            wpid=wpid, pgid=wpid, wst=wst,
        )
        with open(os.path.join(run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wpid, "starttime": wst}, f)
        self._insert_run_token(run_id, "agt_qm", "prj_qm", "las_qm")
        with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"task.merge","args":{"task":"tsk_qm"}}]\n```\n')

        other_item = {"run_id": "run_other", "request_id": "req_other"}
        lead_turns._queue_lead_merge(other_item)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        exit_info = {"exit_code": 0, "signal": None, "ended_at": now}

        member: Dict[str, Any] = {"proc": None, "st": None}
        real_exec = lead_turns.execute_lead_actions

        def exec_then_record_live_member(*args, **kwargs):
            res = real_exec(*args, **kwargs)
            if member["proc"] is None:
                sleep_bin = shutil.which("sleep")
                argv = [sleep_bin, "60"] if sleep_bin else [sys.executable, "-c", "import time; time.sleep(60)"]
                proc = subprocess.Popen(
                    argv,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={},
                )
                member["proc"] = proc

                def _cleanup_member() -> None:
                    if member["st"] is not None:
                        _kill_same(proc.pid, member["st"])
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass

                self.assertTrue(_wait(lambda: get_proc_stat(proc.pid) is not None, 5.0))
                cst = int(get_proc_stat(proc.pid)["starttime"])
                member["st"] = cst
                self.addCleanup(_cleanup_member)
                with open(os.path.join(run_dir, "seen.json"), "w", encoding="utf-8") as sf:
                    json.dump(
                        {"run_id": run_id, "pgid": wpid, "sid": wpid, "members": [{"pid": proc.pid, "starttime": cst}]},
                        sf,
                    )
            return res

        # Pre-settle close_gate passes naturally; once execute_lead_actions queues the merge,
        # the real live recorded member causes the REAL closing close_gate inside _close_run to raise CloseHeld!
        with mock.patch.object(lead_turns, "execute_lead_actions", side_effect=exec_then_record_live_member):
            with self.assertRaises(supervisor.CloseHeld):
                lead_turns.finish_lead_turn_committed(self.conn, self.home, run, exit_info, testing_mode=True)
        self.assertIsNotNone(member["proc"])
        self.assertEqual(
            [m for m in lead_turns._QUEUED_MERGES if m.get("run_id") == run_id],
            [],
            "queued merge leaked across CloseHeld rollback",
        )
        self.assertEqual(lead_turns._QUEUED_MERGES, [other_item], "unrelated queued merge modified")
        self.assertEqual(self.conn.execute(
            "SELECT 1 FROM events WHERE type = 'lead.action_result' AND run_id = ?;", (run_id,)
        ).fetchall(), [])
        self.assertEqual(self._status(run_id), "running")
        self.assertIsNotNone(self._lease("prj_qm"))
        self.assertEqual(self._outbox(out_id)["status"], "delivering")

        # Terminate and reap recorded member, then retry without hold: exactly one merge queued!
        _kill_same(member["proc"].pid, member["st"])
        member["proc"].wait(timeout=5)
        self.assertTrue(_wait(lambda: not _alive(member["proc"].pid, member["st"]), 10))
        outcome = lead_turns.finish_lead_turn_committed(self.conn, self.home, run, exit_info, testing_mode=True)
        self.assertEqual(outcome, "acked")
        run_merges = [m for m in lead_turns._QUEUED_MERGES if m.get("run_id") == run_id]
        self.assertEqual(len(run_merges), 1, run_merges)
        self.assertEqual(run_merges[0]["args"], {"task_id": "tsk_qm"})
        results = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'lead.action_result' AND run_id = ?;", (run_id,)
        ).fetchall()
        self.assertEqual(len(results), 1)
        payload = json.loads(results[0]["payload_json"])
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(self._status(run_id), "exited")
        self.assertIsNone(self._lease("prj_qm"))
        self.assertEqual(self._outbox(out_id)["status"], "acked")

    @unittest.skipUnless(sys.platform != "win32", "POSIX setsid/env-stripped carrier PGID seen_held and _scan_group (Windows Job Object close-gate covered in test_win_close_gate_jobs)")
    def test_lead_recorded_setsid_escapee_terminated_across_restart(self) -> None:
        """Recorded SIGTERM-ignoring setsid escapee (in seen.json + prior_members of adopt_pending)
        keeps lead turn open, lease held, outbox delivering through grace, then SIGKILL closes."""
        self._seed_project()
        esc_out = subprocess.run(
            [sys.executable, "-c", _ESCAPEE_IGNORE_TERM],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True,
        )
        escapee = int(esc_out.stdout.decode().strip())
        est = get_proc_stat(escapee)["starttime"]
        self.addCleanup(_kill_same, escapee, est)
        self.assertTrue(_alive(escapee, est))

        # Dead wrapper identity not matching escapee's group/session.
        probe = subprocess.Popen([sys.executable, "-c", "pass"])
        wpid = probe.pid
        wst = get_proc_stat(wpid)["starttime"] if get_proc_stat(wpid) else 1
        probe.wait()
        self.assertTrue(_wait(lambda: not _alive(wpid, wst), 10))

        run_id, run_dir, out_id = self._insert_lead_run(wpid=wpid, pgid=wpid, wst=wst)
        with open(os.path.join(run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wpid, "starttime": wst}, f)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        supervisor._SEEN[run_id] = {escapee: est}
        supervisor._SEEN_HOLD_TOKEN[run_id] = "tok_esc"
        supervisor.write_unusable_marker(run, supervisor.ADOPT_PENDING, self.home)
        self._assert_marker(run_dir, supervisor.ADOPT_PENDING)
        # Drop memory (restart) so only disk prior_members carries the escapee!
        self._cleanup_globals()
        self._run_ids = [run_id]

        with mock.patch.object(recovery, "_log"), mock.patch.object(supervisor, "_log_meshd"):
            summary = recovery.recover(self.conn, self.home, "boot_new", testing_mode=True)
        self.assertEqual(summary["runs_lost"], 0, summary)
        self.assertEqual(summary["runs_terminating"], 1, summary)
        self.assertTrue(_alive(escapee, est))
        self.assertIsNotNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "delivering")

        # Advance termination until SIGKILL clears the escapee.
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(
                _wait(
                    lambda: (
                        supervisor.advance_terminations(self.conn, self.home, testing_mode=True),
                        self._status(run_id) == "lost",
                    )[1],
                    12.0,
                )
            )
        self.assertFalse(_alive(escapee, est))
        self.assertIsNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "pending")

    @unittest.skipUnless(sys.platform != "win32", "Linux /proc EMFILE/EACCES scan error injection via os.listdir('/proc')")
    def test_lead_uncertain_proc_scan_emfile_eacces_holds_run_and_lease(self) -> None:
        """EMFILE and EACCES on /proc hold lead close, watch exit, and recovery; lease & outbox stay consistent."""
        self._seed_project()
        for err_no in (errno.EMFILE, errno.EACCES):
            with self.subTest(errno=err_no):
                probe = subprocess.Popen([sys.executable, "-c", "pass"])
                wpid = probe.pid
                wst = (get_proc_stat(wpid) or {}).get("starttime") or 1
                probe.wait()
                self.assertTrue(_wait(lambda: not _alive(wpid, wst), 10))

                run_id, run_dir, out_id = self._insert_lead_run(wpid=wpid, pgid=wpid, wst=wst)
                with open(os.path.join(run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
                    json.dump({"pid": wpid, "starttime": wst}, f)
                run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()

                real_listdir = os.listdir

                def fail_proc_listdir(path="."):
                    if str(path) == "/proc":
                        raise OSError(err_no, "injected scan failure", "/proc")
                    return real_listdir(path)

                with mock.patch("os.listdir", side_effect=fail_proc_listdir), \
                        mock.patch.object(supervisor, "_log_meshd"), \
                        mock.patch.object(recovery, "_log"):
                    # 1) close_gate via finish_lead_turn_committed refuses.
                    with self.assertRaises(supervisor.CloseHeld):
                        lead_turns.finish_lead_turn_committed(
                            self.conn, self.home, run, {"exit_code": 0, "signal": None}, testing_mode=True
                        )
                    self.assertIsNotNone(self._lease())
                    self.assertEqual(self._outbox(out_id)["status"], "delivering")

                    # 2) _watch_one catches CloseHeld / uncertain run_targets and keeps run open & lease held.
                    supervisor._watch_one(self.conn, self.home, run, testing_mode=True)
                    self.assertEqual(self._status(run_id), "running")
                    self.assertIsNotNone(self._lease())
                    self.assertEqual(self._outbox(out_id)["status"], "delivering")

                    # 3) crash recovery refuses close (runs_lost == 0, holds run & lease & outbox).
                    summary = recovery.recover(self.conn, self.home, "boot_err", testing_mode=True)
                    self.assertEqual(summary["runs_lost"], 0, summary)
                    self.assertEqual(self._status(run_id), "running")
                    self.assertIsNotNone(self._lease())
                    self.assertEqual(self._outbox(out_id)["status"], "delivering")

                # Clear error: gate passes cleanly and closes.
                with mock.patch.object(supervisor, "_log_meshd"):
                    self.assertTrue(
                        _wait(
                            lambda: (
                                supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True),
                                supervisor.confirm_pending_exits(self.conn, self.home, testing_mode=True),
                                self._status(run_id) == "lost",
                            )[2],
                            10.0,
                        )
                    )
                self.assertIsNone(self._lease())
                self.assertEqual(self._outbox(out_id)["status"], "pending")

    @unittest.skipUnless(sys.platform != "win32", "Linux /proc/os.listdir('/proc') spawn-abort close-gate check (Windows close-gate covered in test_win_close_gate_jobs)")
    def test_lead_spawn_abort_routed_through_close_gate(self) -> None:
        """_fail_lead_turn_spawn and post-Popen spawn abort go through close_gate: held on
        uncertain scan or live recorded member (lease + delivering outbox intact), closed once clear."""
        self._seed_project()
        probe = subprocess.Popen([sys.executable, "-c", "pass"])
        wpid = probe.pid
        wst = (get_proc_stat(wpid) or {}).get("starttime") or 1
        probe.wait()
        self.assertTrue(_wait(lambda: not _alive(wpid, wst), 10))

        # 1) Direct _fail_lead_turn_spawn rolls back completely if close_gate refuses!
        run_id, run_dir, out_id = self._insert_lead_run(status="starting", wpid=wpid, pgid=wpid, wst=wst)
        real_listdir = os.listdir

        def fail_proc(path="."):
            if str(path) == "/proc":
                raise OSError(errno.EMFILE, "too many open files", "/proc")
            return real_listdir(path)

        with mock.patch("os.listdir", side_effect=fail_proc):
            with self.assertRaises(supervisor.CloseHeld):
                with transaction(self.conn):
                    lead_turns._fail_lead_turn_spawn(
                        self.conn, run_id, "prj_l4", "boom", testing_mode=True,
                    )
        self.assertEqual(self._status(run_id), "starting")
        self.assertIsNotNone(self._lease())
        self.assertEqual(self._outbox(out_id)["status"], "delivering")

        # 2) Post-Popen handle_spawn_failure running the real abort path with uncertainty injected
        # only inside close_gate's scan after the kill pass: keeps single ownership in _SPAWN_ABORTS,
        # holds lease + delivering outbox, and closes once on retry after uncertainty clears.
        self._seed_project(project_id="prj_l4_ab", agent_id="agt_l4_ab", assignment_id="las_l4_ab")
        run_id2, run_dir2, out_id2 = self._insert_lead_run(
            project_id="prj_l4_ab", agent_id="agt_l4_ab", assignment_id="las_l4_ab", status="starting"
        )
        proc = children.popen_registered(
            "wrapper",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ab_wpid = proc.pid
        ab_wst = get_proc_stat(ab_wpid)["starttime"]
        self.addCleanup(_kill_same, ab_wpid, ab_wst)

        real_cg = supervisor.close_gate
        cg_uncertain = {"on": True}

        def cg_with_scan_uncertainty(run_arg, end_reason=""):
            if cg_uncertain["on"] and run_arg["id"] == run_id2:
                real_sg = supervisor._scan_group
                with mock.patch.object(
                    supervisor, "_scan_group",
                    side_effect=lambda pg: supervisor.ScanResult(real_sg(pg), uncertain=True),
                ):
                    return real_cg(run_arg, end_reason)
            return real_cg(run_arg, end_reason)

        with mock.patch.object(supervisor, "close_gate", side_effect=cg_with_scan_uncertainty), \
                mock.patch.object(supervisor, "_log_meshd"), \
                mock.patch.object(supervisor, "_log_seen"):
            supervisor.handle_spawn_failure(
                self.conn, self.home, run_id2, run_dir2, proc, RuntimeError("tx b failed"),
                kind="lead_turn", fail_args=("prj_l4_ab",), testing_mode=True,
            )
            self.assertIn(run_id2, supervisor._SPAWN_ABORTS)
            self.assertIsNone(terminate.get(run_id2), "dual Termination created for _SPAWN_ABORTS run")
            self.assertIn(self._status(run_id2), ("starting", "running"))
            self.assertIsNotNone(self._lease("prj_l4_ab"))
            self.assertEqual(self._outbox(out_id2)["status"], "delivering")
            self.assertIsNone(
                self.conn.execute("SELECT 1 FROM meta WHERE key = ?;",
                                  (supervisor.seen_hold_key(run_id2),)).fetchone(),
                "r2 P3-5: _SPAWN_ABORTS hold must not emit an unreachable seen_hold row",
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT 1 FROM events WHERE type = 'run.seen_unusable' AND run_id = ?;", (run_id2,)
                ).fetchall(),
                [],
                "r2 P3-5: _SPAWN_ABORTS hold must not emit an unactionable run.seen_unusable event",
            )
            # r6 P3-7: first CloseHeld records exactly one run.kill_failed event with close_held=True.
            held_evs = self.conn.execute(
                "SELECT payload_json FROM events WHERE type = 'run.kill_failed' AND run_id = ?;", (run_id2,)
            ).fetchall()
            self.assertEqual(len(held_evs), 1, held_evs)
            held_payload = json.loads(held_evs[0]["payload_json"])
            self.assertIs(held_payload.get("close_held"), True)
            self.assertIs(held_payload.get("lease_released"), False)
            self.assertIs(held_payload.get("retrying"), True)
            # Driving retry_spawn_aborts + advance_terminations while uncertain stays held under one owner
            # and emits no duplicate run.kill_failed event.
            supervisor.retry_spawn_aborts(self.conn, self.home, testing_mode=True)
            supervisor.advance_terminations(self.conn, self.home, testing_mode=True)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM events WHERE type = 'run.kill_failed' AND run_id = ?;", (run_id2,)
                ).fetchone()[0],
                1,
            )
            self.assertIsNone(terminate.get(run_id2))
            self.assertIsNotNone(self._lease("prj_l4_ab"))
            self.assertEqual(self._outbox(out_id2)["status"], "delivering")

            # Clear close_gate scan uncertainty: retry_spawn_aborts closes once, reaps, leaves no termination.
            cg_uncertain["on"] = False
            closes = supervisor.retry_spawn_aborts(self.conn, self.home, testing_mode=True)
            supervisor.advance_terminations(self.conn, self.home, testing_mode=True)
            self.assertEqual(closes, [run_id2])
        self.assertEqual(self._status(run_id2), "spawn_failed")
        self.assertFalse(supervisor._SPAWN_ABORTS)
        self.assertNotIn(run_id2, supervisor._WRAPPERS)
        self.assertFalse(_alive(ab_wpid, ab_wst))
        self.assertIsNone(terminate.get(run_id2))
        self.assertIsNone(self._lease("prj_l4_ab"))
        self.assertEqual(self._outbox(out_id2)["status"], "pending")

    def test_p3_2_spawn_abort_single_owner_and_early_exit(self) -> None:
        """P3-2 / r2 P3-4 / r2 P3-5: hold_close on a _SPAWN_ABORTS run skips hold_wrapper_gone,
        report_seen_unusable, and Termination; advance_terminations skips _SPAWN_ABORTS; CloseHeld
        handlers catch hold_close DB errors; and retry_spawn_aborts early-exit runs _abort_kill_pass
        before reaping."""
        self._seed_project(project_id="prj_ab2", agent_id="agt_ab2", assignment_id="las_ab2")
        run_id, run_dir, _out_id = self._insert_lead_run(
            project_id="prj_ab2", agent_id="agt_ab2", assignment_id="las_ab2", status="starting"
        )
        proc = children.popen_registered(
            "wrapper",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wpid = proc.pid
        wst = get_proc_stat(wpid)["starttime"]
        self.addCleanup(_kill_same, wpid, wst)
        with transaction(self.conn):
            self.conn.execute("UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? WHERE id = ?;",
                              (wpid, wpid, wst, run_id))
        seen_path = os.path.join(run_dir, "seen.json")
        with open(seen_path, "w", encoding="utf-8") as f:
            json.dump({"mesh_seen_marker": "spawn_abort_pending", "run_id": run_id}, f)
        ab = supervisor._SpawnAbort(proc, run_id, ("prj_ab2", "err"), run_dir=run_dir, home=self.home,
                                    kind="lead_turn", testing_mode=True)
        ab.wrapper_starttime = wst
        supervisor._SPAWN_ABORTS[run_id] = ab
        supervisor._WRAPPERS[run_id] = proc
        children.hold(wpid)

        run_row = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        held = supervisor.CloseHeld(run_id, "spawn_failed", {wpid: wst}, True, wpid, wst)
        with mock.patch.object(supervisor, "_log_seen"):
            supervisor.hold_close(self.conn, self.home, run_row, held)
        self.assertIsNone(terminate.get(run_id), "hold_close started a duplicate Termination")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM meta WHERE key = ?;",
                                            (supervisor.seen_hold_key(run_id),)).fetchone())
        self.assertEqual(self.conn.execute(
            "SELECT 1 FROM events WHERE type = 'run.seen_unusable' AND run_id = ?;", (run_id,)
        ).fetchall(), [])
        with open(seen_path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["mesh_seen_marker"], "spawn_abort_pending")

        # Even if a foreign Termination existed, advance_terminations must skip _SPAWN_ABORTS runs.
        terminate.begin(run_id, wpid, wpid, "spawn_failed", 1.0, wrapper_starttime=wst, seen={wpid: wst})
        with mock.patch.object(supervisor, "_advance") as adv_spy:
            supervisor.advance_terminations(self.conn, self.home, testing_mode=True)
        adv_spy.assert_not_called()
        terminate.forget(run_id)

        # r2 P3-5: DB errors inside hold_close are caught and logged in both CloseHeld handlers.
        with mock.patch.object(supervisor, "_close_spawn_abort_db", side_effect=held), \
                mock.patch.object(supervisor, "hold_close", side_effect=sqlite3.OperationalError("busy")), \
                mock.patch.object(supervisor, "_log_meshd") as log_spy:
            closed_busy = supervisor.retry_spawn_aborts(self.conn, self.home, testing_mode=True)
        self.assertEqual(closed_busy, [])
        self.assertIn(run_id, supervisor._SPAWN_ABORTS)
        self.assertTrue(any("hold_close deferred on CloseHeld" in call.args[1] for call in log_spy.call_args_list))

        self._seed_project(project_id="prj_ab3", agent_id="agt_ab3", assignment_id="las_ab3")
        run_id3, run_dir3, _out3 = self._insert_lead_run(
            project_id="prj_ab3", agent_id="agt_ab3", assignment_id="las_ab3", status="starting"
        )
        proc3 = children.popen_registered(
            "wrapper",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wpid3 = proc3.pid
        wst3 = get_proc_stat(wpid3)["starttime"]
        self.addCleanup(_kill_same, wpid3, wst3)
        with mock.patch.object(supervisor, "_close_spawn_abort_db",
                               side_effect=supervisor.CloseHeld(run_id3, "spawn_failed", {}, True, wpid3, wst3)), \
                mock.patch.object(supervisor, "hold_close", side_effect=sqlite3.OperationalError("busy")), \
                mock.patch.object(supervisor, "_log_meshd") as log_spy3:
            supervisor.handle_spawn_failure(
                self.conn, self.home, run_id3, run_dir3, proc3, RuntimeError("tx fail"),
                kind="lead_turn", fail_args=("prj_ab3",), testing_mode=True,
            )
        self.assertIn(run_id3, supervisor._SPAWN_ABORTS)
        self.assertTrue(any("hold_close deferred on CloseHeld" in call.args[1] for call in log_spy3.call_args_list))
        ab3_evs = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'run.kill_failed' AND run_id = ?;", (run_id3,)
        ).fetchall()
        self.assertEqual(len(ab3_evs), 1, ab3_evs)
        self.assertIs(json.loads(ab3_evs[0]["payload_json"]).get("close_held"), True)
        supervisor._SPAWN_ABORTS.pop(run_id3, None)
        _kill_same(wpid3, wst3)
        proc3.wait()
        children.unhold(wpid3)
        children.unregister_if(wpid3, "wrapper")

        # Write unredacted secret in vibe-out/session.json, mark run externally closed, and verify
        # r2 P3-4: early exit runs _abort_kill_pass first, skips reap while not done, then reaps once done.
        vibe_dir = os.path.join(run_dir, "vibe-out")
        os.makedirs(vibe_dir, exist_ok=True)
        out_log = os.path.join(vibe_dir, "session.json")
        secret_tok = "msh_deadbeef_secret_tok_001"
        supervisor.register_secret(f"run_token:{run_id}", secret_tok)
        self.addCleanup(supervisor.unregister_secret, f"run_token:{run_id}")
        with open(out_log, "w", encoding="utf-8") as f:
            f.write(json.dumps({"token": secret_tok}) + "\n")
        with transaction(self.conn):
            self.conn.execute("UPDATE runs SET status = 'lost', ended_at = ? WHERE id = ?;",
                              (now_iso_utc(), run_id))
        with mock.patch.object(supervisor, "_abort_kill_pass", return_value=(False, {wpid: wst})) as kp_spy, \
                mock.patch.object(supervisor, "_reap_aborted_wrapper") as reap_spy, \
                mock.patch.object(supervisor, "_log_meshd"):
            closed_pending = supervisor.retry_spawn_aborts(self.conn, self.home, testing_mode=True)
        kp_spy.assert_called_once()
        reap_spy.assert_not_called()
        self.assertEqual(closed_pending, [])
        self.assertIn(run_id, supervisor._SPAWN_ABORTS)

        # Real kill pass now kills proc cleanly (done=True), reaps without blocking, redacts, and closes.
        with mock.patch.object(supervisor, "_log_meshd") as log_spy:
            closed = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                closed = supervisor.retry_spawn_aborts(self.conn, self.home, testing_mode=True)
                if closed == [run_id]:
                    break
                time.sleep(0.02)
        self.assertEqual(closed, [run_id])
        self.assertNotIn(run_id, supervisor._SPAWN_ABORTS)
        self.assertFalse(_alive(wpid, wst))
        self.assertTrue(any("spawn abort skipped" in call.args[1] for call in log_spy.call_args_list))
        with open(out_log, "r", encoding="utf-8") as f:
            self.assertNotIn(secret_tok, f.read())

    @unittest.skipUnless(sys.platform != "win32", "POSIX supervisor._scan_group uncertain close-gate hold check (Windows close-gate covered in test_win_close_gate_jobs)")
    def test_lead_cancel_supersede_and_deadline_routed_through_close_gate(self) -> None:
        """Cancel (project.status='cancelled'), lead_superseded (lead_assignments.ended_at),
        and deadline (deadline_at past) terminate live lead turns through _close_run/close_gate,
        keeping lease and delivering outbox while close_gate holds, releasing once clear."""
        for mode in ("project_cancelled", "lead_superseded", "timeout"):
            with self.subTest(mode=mode):
                pid = f"prj_{mode}"
                self._seed_project(project_id=pid, agent_id=f"agt_{mode}", assignment_id=f"las_{mode}")
                # Reset global active lead row to the right agent id.
                trigger = os.path.join(self.tmp, f"trig_{mode}")
                script = (
                    "import os, time\n"
                    f"while not os.path.exists({trigger!r}):\n"
                    "    time.sleep(0.02)\n"
                )
                with transaction(self.conn):
                    ev = record_event(
                        self.conn, type_="project.note", actor_id="usr_l4", actor_kind="user",
                        via="direct", project_id=pid, payload={"text": mode},
                    )
                    out_id = self._next_id("out")
                    now = now_iso_utc()
                    self.conn.execute(
                        "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, "
                        "type, payload_json, status, attempts, next_attempt_at, created_at) "
                        "VALUES (?, ?, ?, 'lead_wake', ?, ?, 'lead.wake', '{}', 'pending', 0, ?, ?);",
                        (out_id, ev["seq"], f"agt_{mode}", f"dk_{out_id}", pid, now, now),
                    )
                runner = os.path.join(self.tmp, f"r_{mode}.py")
                with open(runner, "w", encoding="utf-8") as f:
                    f.write(script)

                def get_adapter(adapter_id, testing_mode=False, options=None, _r=runner):
                    a = real_get_adapter(adapter_id, testing_mode=testing_mode, options=options)
                    a.build = lambda req: CommandSpec(argv=[sys.executable, _r], cwd=req.workspace)
                    return a

                lead = lead_turns.get_project_active_lead(self.conn, pid)
                msgs = self.conn.execute("SELECT * FROM outbox WHERE project_id = ? AND status = 'pending';",
                                         (pid,)).fetchall()
                with mock.patch.object(lead_turns, "get_adapter", get_adapter), \
                        mock.patch.object(supervisor, "_log_meshd"):
                    rid = lead_turns.start_lead_turn(self.conn, self.home, pid, lead, msgs, testing_mode=True)
                self.assertIsNotNone(rid)
                self._run_ids = getattr(self, "_run_ids", []) + [rid]
                run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (rid,)).fetchone()
                wpid = int(run["wrapper_pid"])
                wst = int(run["proc_starttime"])
                self.addCleanup(_kill_same, wpid, wst)

                # Trigger the termination condition in DB.
                with transaction(self.conn):
                    if mode == "project_cancelled":
                        self.conn.execute("UPDATE projects SET status = 'cancelled' WHERE id = ?;", (pid,))
                    elif mode == "lead_superseded":
                        self.conn.execute("UPDATE lead_assignments SET ended_at = ? WHERE project_id = ?;",
                                          (now_iso_utc(), pid))
                    else:
                        self.conn.execute("UPDATE runs SET deadline_at = ? WHERE id = ?;",
                                          (now_iso_utc(time.time() - 5), rid))

                # Keep wrapper alive (do NOT write trigger): first _watch_one must detect termination reason
                # and start termination; let termination kill the process while close_gate scan is uncertain.
                real_scan = supervisor._scan_group
                hold = {"on": True}

                def uncertain_scan(pg):
                    res = real_scan(pg)
                    if hold["on"]:
                        return supervisor.ScanResult(res, uncertain=True)
                    return res

                with mock.patch.object(supervisor, "_scan_group", uncertain_scan), \
                        mock.patch.object(supervisor, "_log_meshd"):
                    rrow = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (rid,)).fetchone()
                    supervisor._watch_one(self.conn, self.home, rrow, testing_mode=True)
                    self.assertIsNotNone(terminate.get(rid), "watch did not start live-turn termination")
                    self.assertTrue(
                        _wait(
                            lambda: (
                                supervisor.advance_terminations(self.conn, self.home, testing_mode=True),
                                rid in supervisor._SEEN_UNUSABLE,
                            )[1],
                            12.0,
                        ),
                        f"termination never reached CloseHeld / _SEEN_UNUSABLE for {mode}",
                    )
                self.assertIn(rid, supervisor._SEEN_UNUSABLE)
                # While held: run still running, lease kept, outbox still delivering!
                self.assertEqual(self._status(rid), "running")
                self.assertIsNotNone(self._lease(pid))
                self.assertEqual(self._outbox(out_id)["status"], "delivering")

                hold["on"] = False
                with mock.patch.object(supervisor, "_log_meshd"):
                    self.assertTrue(
                        _wait(
                            lambda: (
                                supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True),
                                supervisor.advance_terminations(self.conn, self.home, testing_mode=True),
                                self._status(rid) not in ("starting", "running"),
                            )[2],
                            10.0,
                        )
                    )
                r_final = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (rid,)).fetchone()
                self.assertEqual(r_final["status"], "killed", dict(r_final))
                self.assertEqual(r_final["end_reason"], mode, dict(r_final))
                self.assertIsNone(self._lease(pid))
                self.assertEqual(self._outbox(out_id)["status"], "pending")

    def test_lead_finish_gate_before_settle_and_token_expired_before_finish_retries(self) -> None:
        """r3 P3-5 & P3-3 / r4 P3-1: finish_lead_turn_committed checks close_gate before calling
        settle_deliver_intent, and if the run token expired before finish (gate hold or daemon
        downtime), actions roll back with `actions_deferred='token_expired'` in result_json and
        wake messages retry (ok=False)."""
        from mesh.core.auth import generate_run_token
        from mesh.core.ids import generate_id

        # 1) Gate hold outlasting run token:
        self._seed_project(project_id="prj_tok", agent_id="agt_tok", assignment_id="las_tok")
        run_id, run_dir, out_id = self._insert_lead_run(
            project_id="prj_tok", agent_id="agt_tok", assignment_id="las_tok",
        )
        _tok, lookup, shash = generate_run_token()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES (?, 'agt_tok', 'run_token', ?, ?, ?, ?, ?);",
                (
                    generate_id("cred"),
                    lookup,
                    shash,
                    json.dumps({"role": "lead", "run_id": run_id, "project_id": "prj_tok", "lead_assignment_id": "las_tok"}),
                    now_iso_utc(time.time() - 600),
                    now_iso_utc(time.time() + 10),
                ),
            )
        with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"project.note","args":{"text":"must retry not drop"}}]\n```\n')

        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        exit_info = {"exit_code": 0, "signal": None, "ended_at": now_iso_utc(time.time() + 1)}

        # r3 P3-5 & r5 P3-6: While close_gate holds, settle_deliver_intent must NOT be called;
        # when clear, finish_lead_turn_committed invokes close_gate twice (pre-settle and _close_run),
        # never three times.
        with mock.patch.object(
            supervisor, "close_gate",
            side_effect=supervisor.CloseHeld(run_id, "exited", {}, True, 999999, 1),
        ), mock.patch.object(recovery, "settle_deliver_intent") as settle_spy:
            with self.assertRaises(supervisor.CloseHeld):
                lead_turns.finish_lead_turn_committed(self.conn, self.home, run, exit_info, testing_mode=True)
        settle_spy.assert_not_called()

        # Simulate the hold outlasting the run token: hold clears after expires_at passed.
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE credentials SET expires_at = ? WHERE kind = 'run_token' "
                "AND json_extract(scope_json, '$.run_id') = ?;",
                (now_iso_utc(time.time() - 5), run_id),
            )
        supervisor._CLOSE_REFUSED_LOGGED[run_id] = ("exited", (), True)
        with mock.patch.object(supervisor, "close_gate", wraps=supervisor.close_gate) as gate_spy:
            outcome = lead_turns.finish_lead_turn_committed(self.conn, self.home, run, exit_info, testing_mode=True)
        self.assertEqual(gate_spy.call_count, 2, "expected 2 close_gate scans (pre-settle and _close_run)")
        self.assertEqual(outcome, "retry")
        r_row = self.conn.execute("SELECT status, result_json FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertEqual(r_row["status"], "exited")
        self.assertEqual(json.loads(r_row["result_json"]).get("actions_deferred"), "token_expired")
        self.assertIsNone(self._lease("prj_tok"))
        self.assertEqual(self._outbox(out_id)["status"], "pending", "expired hold acked unexecuted actions")
        self.assertEqual(
            self.conn.execute(
                "SELECT 1 FROM events WHERE type IN ('lead.action_result', 'project.note') AND run_id = ?;",
                (run_id,),
            ).fetchall(),
            [],
        )

        # 2) Long daemon downtime (no gate hold, ended_at <= expires_at < now): also defers safely.
        self._seed_project(project_id="prj_down", agent_id="agt_down", assignment_id="las_down")
        run_id2, run_dir2, out_id2 = self._insert_lead_run(
            project_id="prj_down", agent_id="agt_down", assignment_id="las_down",
        )
        _tok2, lookup2, shash2 = generate_run_token()
        ended_ts = time.time() - 60
        exp_ts = time.time() - 10
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES (?, 'agt_down', 'run_token', ?, ?, ?, ?, ?);",
                (
                    generate_id("cred"),
                    lookup2,
                    shash2,
                    json.dumps({"role": "lead", "run_id": run_id2, "project_id": "prj_down", "lead_assignment_id": "las_down"}),
                    now_iso_utc(time.time() - 600),
                    now_iso_utc(exp_ts),
                ),
            )
        with open(os.path.join(run_dir2, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"project.note","args":{"text":"downtime retry"}}]\n```\n')
        run2 = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id2,)).fetchone()
        exit_info2 = {"exit_code": 0, "signal": None, "ended_at": now_iso_utc(ended_ts)}
        with mock.patch.object(supervisor, "close_gate", wraps=supervisor.close_gate):
            outcome2 = lead_turns.finish_lead_turn_committed(self.conn, self.home, run2, exit_info2, testing_mode=True)
        self.assertEqual(outcome2, "retry")
        r_row2 = self.conn.execute("SELECT status, result_json FROM runs WHERE id = ?;", (run_id2,)).fetchone()
        self.assertEqual(r_row2["status"], "exited")
        self.assertEqual(json.loads(r_row2["result_json"]).get("actions_deferred"), "token_expired")
        out2 = self._outbox(out_id2)
        self.assertEqual((out2["status"], out2["attempts"]), ("pending", 1))
        self.assertEqual(
            self.conn.execute(
                "SELECT 1 FROM events WHERE type IN ('lead.action_result', 'project.note') AND run_id = ?;",
                (run_id2,),
            ).fetchall(),
            [],
        )

        # 3) r7 P3-2: malformed non-str ended_at does not raise TypeError; exception in helper
        #    rolls back SAVEPOINT lead_actions cleanly inside the try without wedging close.
        self._seed_project(project_id="prj_badt", agent_id="agt_badt", assignment_id="las_badt")
        run_id3, run_dir3, out_id3 = self._insert_lead_run(
            project_id="prj_badt", agent_id="agt_badt", assignment_id="las_badt",
        )
        _tok3, lookup3, shash3 = generate_run_token()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES (?, 'agt_badt', 'run_token', ?, ?, ?, ?, ?);",
                (
                    generate_id("cred"),
                    lookup3,
                    shash3,
                    json.dumps({"role": "lead", "run_id": run_id3, "project_id": "prj_badt", "lead_assignment_id": "las_badt"}),
                    now_iso_utc(time.time() - 600),
                    now_iso_utc(time.time() - 10),
                ),
            )
        with open(os.path.join(run_dir3, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"project.note","args":{"text":"bad ended_at type"}}]\n```\n')
        supervisor._CLOSE_REFUSED_LOGGED[run_id3] = ("exited", (), True)
        run3 = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id3,)).fetchone()
        with mock.patch.object(supervisor, "close_gate", wraps=supervisor.close_gate):
            outcome3 = lead_turns.finish_lead_turn_committed(
                self.conn, self.home, run3, {"exit_code": 0, "signal": None, "ended_at": 12345}, testing_mode=True
            )
        self.assertEqual(outcome3, "retry")
        r_row3 = self.conn.execute("SELECT status, result_json FROM runs WHERE id = ?;", (run_id3,)).fetchone()
        self.assertEqual(r_row3["status"], "exited")
        self.assertEqual(json.loads(r_row3["result_json"]).get("actions_deferred"), "token_expired")
        self.assertEqual(self._outbox(out_id3)["status"], "pending")

        # Also test helper raising inside the action try: run still closes cleanly without wedging.
        self._seed_project(project_id="prj_bade", agent_id="agt_bade", assignment_id="las_bade")
        run_id4, run_dir4, out_id4 = self._insert_lead_run(
            project_id="prj_bade", agent_id="agt_bade", assignment_id="las_bade",
        )
        _tok4, lookup4, shash4 = generate_run_token()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES (?, 'agt_bade', 'run_token', ?, ?, ?, ?, ?);",
                (
                    generate_id("cred"),
                    lookup4,
                    shash4,
                    json.dumps({"role": "lead", "run_id": run_id4, "project_id": "prj_bade", "lead_assignment_id": "las_bade"}),
                    now_iso_utc(time.time() - 600),
                    now_iso_utc(time.time() - 10),
                ),
            )
        with open(os.path.join(run_dir4, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"project.note","args":{"text":"helper error"}}]\n```\n')
        run4 = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id4,)).fetchone()
        with mock.patch.object(supervisor, "close_gate", wraps=supervisor.close_gate), mock.patch.object(
            lead_turns, "_token_expired_before_finish", side_effect=RuntimeError("boom in expiry helper")
        ):
            outcome4 = lead_turns.finish_lead_turn_committed(
                self.conn, self.home, run4, {"exit_code": 0, "signal": None, "ended_at": now_iso_utc()}, testing_mode=True
            )
        self.assertEqual(outcome4, "retry")
        self.assertEqual(self._status(run_id4), "exited")
        self.assertEqual(self._outbox(out_id4)["status"], "pending")

    def test_lead_termination_finalizer_gates_before_releasing_messages(self) -> None:
        """r5 P3-5: `_finalize_lead_turn_termination` runs `close_gate` before `_release_messages`,
        so held terminations neither update outbox nor record `lead.unreachable`."""
        self._seed_project(project_id="prj_tgate", agent_id="agt_tgate", assignment_id="las_tgate")
        run_id, _rdir, out_id = self._insert_lead_run(
            project_id="prj_tgate", agent_id="agt_tgate", assignment_id="las_tgate",
        )
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        term = terminate.Termination(
            run_id=run_id, pgid=999999, wrapper_pid=999999, end_reason="timeout", grace_s=1.0,
        )
        with mock.patch.object(
            supervisor, "close_gate",
            side_effect=supervisor.CloseHeld(run_id, "timeout", {}, True, 999999, 1),
        ), mock.patch.object(lead_turns, "_release_messages") as rel_spy:
            with self.assertRaises(supervisor.CloseHeld):
                supervisor._finalize_lead_turn_termination(self.conn, self.home, run, term, None, testing_mode=True)
        rel_spy.assert_not_called()
        self.assertEqual(self._outbox(out_id)["status"], "delivering")
        self.assertIsNotNone(self._lease("prj_tgate"))


if __name__ == "__main__":
    unittest.main()
