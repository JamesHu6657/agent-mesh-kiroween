"""Unit tests: proc identity (§11.4 pid reuse), the termination state machine and the
child registry / orphan reaper (§11.4, H13, H18 reaper half)."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import unittest
import uuid

from mesh.core.proc import get_proc_environ, get_proc_stat, parse_proc_stat
from mesh.daemon import children, terminate

# Unique per test process: run ids carried as a real MESH_RUN_ID must not collide
# with another tier run on the same host (dogfood concurrency).
_RUN_SFX = f"_{os.getpid()}"


# Generous deadline for wait-for-condition polls: they return as soon as the condition holds,
# so the deadline only matters on a real failure, never on a slow but healthy box.
WAIT_S = 30.0


def _wait(pred, timeout=WAIT_S):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _run_id(tag: str) -> str:
    """A run id unique to this test process and call. run_targets() matches MESH_RUN_ID on
    every process in /proc, so a fixed id ("run_zomb") lets two suites running in parallel
    see each other's processes as targets (m3-flake-hunt)."""
    return f"{tag}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


class TestProcIdentity(unittest.TestCase):
    def test_comm_with_parens_and_spaces(self) -> None:
        fields = " ".join(["S", "1", "77", "77"] + ["0"] * 15 + ["123456"] + ["0"] * 10)
        info = parse_proc_stat(f"4242 (a) (b) {fields}")
        self.assertEqual(info["state"], "S")
        self.assertEqual(info["pgrp"], 77)
        self.assertEqual(info["starttime"], 123456)

    def test_proc_identity(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            start = get_proc_stat(proc.pid)["starttime"]
            self.assertFalse(terminate.signal_if_same(proc.pid, start + 1, signal.SIGTERM))
            self.assertIsNone(proc.poll())
            self.assertTrue(terminate.signal_if_same(proc.pid, start, signal.SIGTERM))
            proc.wait(timeout=WAIT_S)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestTerminationStateMachine(unittest.TestCase):
    def tearDown(self) -> None:
        for run_id in list(terminate.active()):
            terminate.forget(run_id)

    def test_sigterm_then_sigkill_escalation(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "print('r', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, start_new_session=True)
        try:
            proc.stdout.readline()
            # The process models the run's wrapper (session leader) with its starttime.
            st = get_proc_stat(proc.pid)["starttime"]
            t = terminate.begin(_run_id(f"run_unit_esc{_RUN_SFX}"), proc.pid, proc.pid, "timeout", 1, wrapper_starttime=st)
            self.assertIn(proc.pid, t.initial_pids)
            self.assertEqual(terminate.step(t), "pending")
            reaped = []

            def done():
                if proc.poll() is not None and not reaped:
                    reaped.append(True)
                return terminate.step(t) == "done"

            self.assertTrue(_wait(done))
            self.assertEqual(proc.returncode, -signal.SIGKILL)
            self.assertFalse(t.kill_failed)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()


class TestChildRegistry(unittest.TestCase):
    def test_reaper_reaps_only_unregistered_zombies(self) -> None:
        mine = children.popen_registered("wrapper", [sys.executable, "-c", "pass"])
        stray = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            self.assertTrue(_wait(lambda: (get_proc_stat(stray.pid) or {}).get("state") == "Z"))
            self.assertTrue(_wait(lambda: (get_proc_stat(mine.pid) or {}).get("state") == "Z"))
            reaped = children.reap_orphans()
            self.assertIn(stray.pid, reaped)
            self.assertNotIn(mine.pid, reaped)
            self.assertIsNone(get_proc_stat(stray.pid))
            stray.returncode = 0  # collected by the reaper, not by Popen
            # The registered child is still collectable by its owner.
            self.assertEqual(mine.wait(timeout=WAIT_S), 0)
        finally:
            children.unregister(mine.pid)

    def test_reaper_skips_while_spawn_in_flight(self) -> None:
        stray = subprocess.Popen([sys.executable, "-c", "pass"])
        self.assertTrue(_wait(lambda: (get_proc_stat(stray.pid) or {}).get("state") == "Z"))
        with children.spawn_window():
            self.assertEqual(children.reap_orphans(), [])
            self.assertEqual((get_proc_stat(stray.pid) or {}).get("state"), "Z")
        self.assertIn(stray.pid, children.reap_orphans())
        stray.returncode = 0  # collected by the reaper, not by Popen

    def test_reaper_inflight_spawn(self) -> None:
        """ACC: H18 - 200 fast-exiting registered asyncio children all return 0 with the reaper every 10 ms."""
        before = dict(children.stats)

        async def scenario() -> list:
            stop = asyncio.Event()

            async def reaper() -> None:
                while not stop.is_set():
                    children.reap_orphans()
                    await asyncio.sleep(0.01)

            reaper_task = asyncio.create_task(reaper())
            codes = []
            try:
                for _ in range(200):
                    proc = await children.create_subprocess_exec_registered("probe", "true")
                    try:
                        codes.append(await proc.wait())
                    finally:
                        children.unregister(proc.pid)
            finally:
                stop.set()
                await reaper_task
            return codes

        codes = asyncio.run(scenario())
        self.assertEqual(len(codes), 200)
        self.assertEqual(set(codes), {0})
        self.assertGreater(children.stats["scans"], before["scans"])
        with children.spawn_window():
            skipped = children.stats["scans_skipped_in_flight"]
            self.assertEqual(children.reap_orphans(), [])
            self.assertEqual(children.stats["scans_skipped_in_flight"], skipped + 1)

    def test_run_registered_unregisters(self) -> None:
        res = children.run_registered("gitops", [sys.executable, "-c", "print('x')"], capture_output=True, text=True)
        self.assertEqual(res.stdout.strip(), "x")
        self.assertEqual(children.registered_pids(), set())



class TestPidReuseSafety(unittest.TestCase):
    """Review m1c-run3 #1: bare pid / pgid numbers are never trusted on their own."""

    def _session_proc(self, script: str = "import time; print('r', flush=True); time.sleep(30)"):
        env = {k: v for k, v in os.environ.items() if k != "MESH_RUN_ID"}
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                start_new_session=True, env=env)
        proc.stdout.readline()
        self.addCleanup(self._kill, proc)
        return proc

    @staticmethod
    def _kill(proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        proc.stdout.close()

    def test_registered_wrapper_pid_with_other_starttime_excluded(self) -> None:
        """A registered meshd child whose pid equals the run's (stale) wrapper_pid but whose
        starttime differs (another run's wrapper reusing the number) is never a target."""
        other = self._session_proc()
        st = get_proc_stat(other.pid)["starttime"]
        children.register(other.pid, "wrapper")
        self.addCleanup(children.unregister, other.pid)
        run_id = _run_id(f"run_stale{_RUN_SFX}")
        found = terminate.run_targets(run_id, other.pid, other.pid, int(st) + 1)
        self.assertNotIn(other.pid, found)
        self.assertEqual(found, {})
        # Control: with the registered starttime it is this run's own wrapper again.
        self.assertIn(other.pid, terminate.run_targets(run_id, other.pid, other.pid, int(st)))

    def test_stale_pgid_member_without_run_id_after_wrapper_gone(self) -> None:
        """Once the wrapper identity is gone, a process whose only link is the pgid/sid number
        (no MESH_RUN_ID, not seen, not a descendant of a seen process) is left alone."""
        stranger = self._session_proc(
            "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
            " print('r', flush=True); time.sleep(30)")
        st = int(get_proc_stat(stranger.pid)["starttime"])
        run_id = _run_id(f"run_gone{_RUN_SFX}")
        self.assertEqual(terminate.run_targets(run_id, stranger.pid, stranger.pid, st + 1), {})
        self.assertEqual(terminate.run_targets(run_id, stranger.pid, None, None), {})
        # Already seen by this termination with the same starttime: still a target,
        # and its group member qualifies by descent.
        found = terminate.run_targets(run_id, stranger.pid, stranger.pid, st + 1, seen={stranger.pid: st})
        self.assertIn(stranger.pid, found)
        members = {int(e) for e in os.listdir("/proc") if e.isdigit()
                   and (get_proc_stat(int(e)) or {}).get("pgrp") == stranger.pid}
        self.assertEqual(len(members), 2, members)
        self.assertEqual(set(found), members, (found, members, children.registered()))
        # Seen with another starttime (a reused number): not a target.
        self.assertEqual(terminate.run_targets(run_id, stranger.pid, None, None, seen={stranger.pid: st + 5}), {})

    def test_wrapper_reaped_through_popen_only(self) -> None:
        """_reap_wrapper polls the run's own Popen; a run without one waits on nothing."""
        from mesh.daemon import supervisor

        proc = subprocess.Popen(["true"])
        children.register(proc.pid, "wrapper")
        supervisor._WRAPPERS[f"run_reap{_RUN_SFX}"] = proc
        deadline = time.monotonic() + WAIT_S
        while f"run_reap{_RUN_SFX}" in supervisor._WRAPPERS and time.monotonic() < deadline:
            supervisor._reap_wrapper({"id": f"run_reap{_RUN_SFX}", "wrapper_pid": proc.pid})
            time.sleep(0.02)
        self.assertNotIn(f"run_reap{_RUN_SFX}", supervisor._WRAPPERS)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn(proc.pid, children.registered_pids())
        # A run with no Popen (adopted): nothing is waited on, the registry is untouched.
        children.register(os.getpid(), "stub")
        try:
            supervisor._reap_wrapper({"id": f"run_adopted{_RUN_SFX}", "wrapper_pid": os.getpid()})
            self.assertIn(os.getpid(), children.registered_pids())
        finally:
            children.unregister(os.getpid())


class TestTerminationReason(unittest.TestCase):
    """supervisor.termination_reason on plain rows (review m1c-run3 #3 and #6)."""

    def setUp(self) -> None:
        import sqlite3
        import tempfile

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT);")
        self.conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, status TEXT);")
        self.conn.execute("INSERT INTO settings VALUES ('kill.post_submit_grace_s', '1');")
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-treason-")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def _task(self, status: str, record=None, idle: int = 0):
        return {"status": status, "project_id": "prj", "idle_timeout_s": idle,
                "deliverable_json": json.dumps(record) if record is not None else None}

    def _run(self):
        return {"deadline_at": None, "adapter": "x", "run_dir": self.tmp.name}

    def test_agent_named_system_still_gets_post_submit_grace(self) -> None:
        from mesh.daemon import supervisor

        old = "2000-01-01T00:00:00.000Z"
        named_system = {"deliverable": {}, "submitted_by": "system", "submitted_at": old, "run_id": "r"}
        self.assertFalse(supervisor.is_auto_submit(named_system))
        self.assertEqual(supervisor.termination_reason(self.conn, self._run(), self._task("submitted", named_system)),
                         "post_submit_grace")
        auto = dict(named_system, auto=True)
        self.assertTrue(supervisor.is_auto_submit(auto))
        self.assertIsNone(supervisor.termination_reason(self.conn, self._run(), self._task("submitted", auto)))
        self.assertFalse(supervisor.is_auto_submit(dict(named_system, auto="true")))

    def test_idle_timeout_only_for_stream_liveness(self) -> None:
        from unittest import mock

        from mesh.daemon import supervisor

        activity = os.path.join(self.tmp.name, "activity")
        with open(activity, "w", encoding="utf-8") as f:
            f.write("x")
        past = time.time() - 30
        os.utime(activity, (past, past))
        task = self._task("running", idle=5)
        with mock.patch.object(supervisor, "_adapter_liveness", lambda _a: "stream"):
            self.assertEqual(supervisor.termination_reason(self.conn, self._run(), task), "idle_timeout")
            os.utime(activity, None)
            self.assertIsNone(supervisor.termination_reason(self.conn, self._run(), task))
            os.utime(activity, (past, past))
            self.assertIsNone(supervisor.termination_reason(self.conn, self._run(), self._task("running", idle=0)))
        with mock.patch.object(supervisor, "_adapter_liveness", lambda _a: "none"):
            self.assertIsNone(supervisor.termination_reason(self.conn, self._run(), task))


class TestZombieWrapperGroup(unittest.TestCase):
    """Review m1c-run4 §2: while meshd's own wrapper is an unreaped zombie, the pgid is still
    this run's, so a group member that dropped MESH_RUN_ID is a target."""

    def test_env_stripped_member_found_while_wrapper_unreaped(self) -> None:
        run_id = _run_id(f"run_zomb{_RUN_SFX}")
        env = dict(os.environ, MESH_RUN_ID=run_id)
        leader = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], env={});"
             " print(p.pid, flush=True)"],
            stdout=subprocess.PIPE, start_new_session=True, env=env)
        member = int(leader.stdout.readline())
        leader.stdout.close()

        def _cleanup() -> None:
            try:
                os.kill(member, signal.SIGKILL)
            except OSError:
                pass
            leader.wait()
        self.addCleanup(_cleanup)
        st = int(get_proc_stat(leader.pid)["starttime"])
        deadline = time.time() + WAIT_S
        while (get_proc_stat(leader.pid) or {}).get("state") != "Z" and time.time() < deadline:
            time.sleep(0.05)
        info = get_proc_stat(leader.pid)
        self.assertEqual(info["state"], "Z", "leader must be an unreaped zombie of this process")
        self.assertEqual(int(info["ppid"]), os.getpid())
        self.assertEqual(get_proc_stat(member)["pgrp"], leader.pid)
        self.assertNotIn("MESH_RUN_ID", get_proc_environ(member) or {})
        self.assertTrue(terminate._wrapper_identity_alive(leader.pid, st))
        self.assertFalse(terminate._wrapper_identity_alive(leader.pid, st + 1))
        found = terminate.run_targets(run_id, leader.pid, leader.pid, st)
        self.assertIn(member, found)
        self.assertNotIn(leader.pid, found)  # zombies are never targets
        # After the reap, a seen member keeps the session trusted (_session_anchored) ...
        leader.wait()
        seen = {member: int(get_proc_stat(member)["starttime"])}
        self.assertIn(member, terminate.run_targets(run_id, leader.pid, leader.pid, st, seen=seen))
        # ... but without the zombie or a seen member the bare number is not trusted.
        self.assertEqual(terminate.run_targets(run_id, leader.pid, leader.pid, st), {})


if __name__ == "__main__":
    unittest.main()
