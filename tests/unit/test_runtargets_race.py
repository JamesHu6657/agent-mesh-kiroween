"""The /proc environ read race in run_targets (m3-runtargets-race, review #20 fix round).

On a gVisor dev box, reading /proc/<pid>/environ right after Popen missed
MESH_RUN_ID in 92/200 cases: get_proc_environ returned {} so "not visible yet"
looked like "no match". An empty/unreadable environ of a young own-uid process is
UNKNOWN; it is taken only through a confirmed identity, a trusted group or PPid
descent, never through its environ, and nothing sleeps on the scheduler path.
"""

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from mesh.core.proc import get_proc_environ, get_proc_stat
from mesh.daemon import merge, recovery, server, supervisor, terminate
from tests.unit.test_proc_probe import _read_line_or_fail

# Unique per test process: run ids carried as a real MESH_RUN_ID must not collide
# with another tier run on the same host (dogfood concurrency).
_RUN_SFX = f"_{os.getpid()}"

# Real /proc scans: hold the runner's host-wide /proc lock shared (tests/run_tier.py).
MESH_PROC_LOCK = "shared"

RUN = f"run_race{_RUN_SFX}"


def _spawn(run_id=None, extra_env=None, args=None):
    env = os.environ.copy()
    env.pop("MESH_RUN_ID", None)
    if run_id is not None:
        env["MESH_RUN_ID"] = run_id
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        args or [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _empty_first(pid, n, real=get_proc_environ):
    counts = {"n": 0}

    def fake(p):
        if int(p) == int(pid):
            counts["n"] += 1
            if counts["n"] <= n:
                return {}
        return real(p)

    return fake, counts


class FakeProc:
    """A fake /proc table: {pid: dict(ppid, pgrp, session, starttime, state, env, uid, kthread)}."""

    def __init__(self, table: Dict[int, Dict[str, Any]], order: Optional[List[int]] = None):
        self.table = table
        self.order = order

    def stat(self, pid):
        p = self.table.get(int(pid))
        if p is None:
            return None
        return {"state": p.get("state", "S"), "ppid": p["ppid"], "pgrp": p.get("pgrp", pid),
                "session": p.get("session", pid), "starttime": p.get("starttime", 1000)}

    def environ(self, pid):
        p = self.table.get(int(pid))
        return None if p is None else p.get("env")

    def patches(self):
        real_listdir = os.listdir
        order = self.order if self.order is not None else sorted(self.table)

        def listdir(path="."):
            if path == "/proc":
                return [str(p) for p in order if p in self.table] + ["self", "uptime"]
            return real_listdir(path)

        return [
            mock.patch.object(terminate.os, "listdir", listdir),
            mock.patch.object(terminate, "get_proc_stat", self.stat),
            mock.patch.object(terminate, "get_proc_environ", self.environ),
            mock.patch.object(terminate, "_same_uid",
                              lambda pid: self.table[int(pid)].get("uid", "me") == "me"),
            mock.patch.object(terminate, "_is_kthread",
                              lambda pid, info: bool(self.table[int(pid)].get("kthread"))),
            mock.patch.object(terminate, "_young_from", lambda wst: 500),
            mock.patch.object(terminate.children, "registered", lambda: {}),
        ]

    def __enter__(self):
        self._p = self.patches()
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()


def _no_sleep(*_a, **_k):
    raise AssertionError("slept on the scheduler path")


class RealProcBase(unittest.TestCase):
    def setUp(self):
        self._procs = []

    def tearDown(self):
        for run_id in list(terminate.active()):
            terminate.forget(run_id)
        supervisor._CLEAR.clear()
        for proc in self._procs:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    def spawn(self, run_id=None, extra_env=None, args=None):
        proc = _spawn(run_id, extra_env, args)
        self._procs.append(proc)
        self.assertIsNone(proc.poll())
        return proc


class TestClearCheck(unittest.TestCase):
    """Kills the mutations "clear on the first scan" and "no reset on a hit" (Q3)."""

    def test_first_empty_scan_never_clear(self):
        self.assertFalse(terminate.ClearCheck().observe({}, now=100.0))

    def test_second_scan_too_soon_not_clear_and_does_not_renew(self):
        c = terminate.ClearCheck()
        self.assertFalse(c.observe({}, now=100.0))
        self.assertFalse(c.observe({}, now=100.0 + terminate.POLL_S / 2))
        self.assertEqual(c.first_empty_at, 100.0)
        self.assertTrue(c.observe({}, now=100.0 + terminate.POLL_S))

    def test_hit_resets(self):
        c = terminate.ClearCheck()
        self.assertFalse(c.observe({}, now=100.0))
        self.assertFalse(c.observe({42: 7}, now=100.1))
        self.assertIsNone(c.first_empty_at)
        self.assertFalse(c.observe({}, now=100.0 + 5 * terminate.POLL_S))
        self.assertTrue(c.observe({}, now=100.0 + 6 * terminate.POLL_S))

    def test_invalidate_resets(self):
        c = terminate.ClearCheck()
        c.observe({}, now=100.0)
        c.invalidate()
        self.assertFalse(c.observe({}, now=101.0))
        self.assertTrue(c.observe({}, now=101.0 + terminate.POLL_S))

    def test_no_module_state(self):
        self.assertFalse(hasattr(terminate, "_empty_scans"))
        self.assertFalse(hasattr(terminate, "confirmed_clear"))

    def test_step_done_needs_two_empty_scans_a_poll_apart(self):
        t = terminate.Termination(run_id=f"run_empty{_RUN_SFX}", pgid=None, wrapper_pid=None,
                                  end_reason="timeout", grace_s=1.0)
        with mock.patch.object(terminate, "run_targets", return_value={}):
            self.assertEqual(terminate.step(t), "pending")
            self.assertEqual(terminate.step(t), "pending")  # same poll
            t.empty_scan_at = time.monotonic() - terminate.POLL_S
            self.assertEqual(terminate.step(t), "done")

    def test_step_hit_between_empty_scans_resets(self):
        """Review #20b M15: empty, hit, empty a poll after the first is not "done"."""
        t = terminate.Termination(run_id=f"run_hit{_RUN_SFX}", pgid=None, wrapper_pid=None,
                                  end_reason="timeout", grace_s=100.0)
        t.deadline = 1e9
        scans = iter([{}, {4242: 7}, {}, {}])
        clock = iter([100.0, 100.1, 100.3, 100.6])
        with mock.patch.object(terminate, "run_targets", side_effect=lambda *a, **k: next(scans)), \
                mock.patch.object(terminate, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=time.sleep)), \
                mock.patch.object(terminate, "signal_if_same", return_value=False):
            self.assertEqual(terminate.step(t), "pending")  # first empty scan
            self.assertEqual(terminate.step(t), "pending")  # hit: the empty scan is void
            self.assertEqual(terminate.step(t), "pending")  # 0.3 s after the first, fresh first
            self.assertEqual(terminate.step(t), "done")     # 0.3 s after that one


class TestClosureFakeProc(unittest.TestCase):
    """Descendant closure, confirmed identity and safety on a fake /proc table."""

    def test_unknown_escapee_found_via_parent_anchor(self):
        # 10 carries MESH_RUN_ID; 11 (own session, UNKNOWN) is its child.
        table = {10: {"ppid": 1, "env": {"MESH_RUN_ID": RUN}},
                 11: {"ppid": 10, "session": 11, "pgrp": 11, "env": {}}}
        with FakeProc(table):
            unknown: Dict[int, int] = {}
            found = terminate.run_targets(RUN, None, None, unknown=unknown)
        self.assertEqual(set(found), {10, 11})  # kills "drop UNKNOWN escapee descent" (Q3)
        self.assertEqual(set(unknown), {11})

    def test_confirmed_identity_after_parent_exits(self):
        """L1: found once, the parent exits, 11 is re-parented and still UNKNOWN."""
        table = {11: {"ppid": 1, "session": 11, "pgrp": 11, "env": None}}
        with FakeProc(table):
            self.assertEqual(terminate.run_targets(RUN, None, None, seen={11: 1000}), {11: 1000})
            # A reused pid (other starttime) is not the same process.
            self.assertEqual(terminate.run_targets(RUN, None, None, seen={11: 999}), {})

    def test_unknown_trusted_group_member_is_anchor_any_order(self):
        """L2: an UNKNOWN trusted-group member anchors its setsid child in the same scan,
        whatever the /proc order."""
        # Group 50 trusted through the seen member 51; 52 is an UNKNOWN group member
        # re-parented to init; 53 is 52's UNKNOWN child in its own session, 54 is 53's.
        table = {51: {"ppid": 1, "pgrp": 50, "session": 50, "env": {"X": "1"}},
                 52: {"ppid": 1, "pgrp": 50, "session": 50, "env": {}},
                 53: {"ppid": 52, "pgrp": 53, "session": 53, "env": None},
                 54: {"ppid": 53, "pgrp": 54, "session": 54, "env": {}}}
        for order in ([51, 52, 53, 54], [54, 53, 52, 51], [53, 54, 51, 52]):
            with FakeProc(table, order=order):
                found = terminate.run_targets(RUN, 50, None, None, seen={51: 1000})
            self.assertEqual(set(found), {51, 52, 53, 54}, order)

    def test_empty_cmdline_is_unknown_not_known(self):
        """Q4: a non-kernel process with empty environ (and no cmdline) is UNKNOWN."""
        table = {10: {"ppid": 1, "env": {"MESH_RUN_ID": RUN}},
                 11: {"ppid": 10, "session": 11, "pgrp": 11, "env": {}}}
        with FakeProc(table), mock.patch("builtins.open", side_effect=OSError("no cmdline")):
            self.assertIn(11, terminate.run_targets(RUN, None, None))

    def test_other_user_kthread_zombie_old_never_targeted(self):
        """Safety: other-uid, kernel-thread, zombie and old processes are never targets,
        even under an anchor or in the run's (trusted) group."""
        table = {10: {"ppid": 1, "pgrp": 10, "session": 10, "env": {"MESH_RUN_ID": RUN}},
                 20: {"ppid": 1, "session": 20, "pgrp": 20, "env": None, "uid": "other"},
                 21: {"ppid": 2, "session": 0, "pgrp": 0, "env": {}, "kthread": True},
                 22: {"ppid": 10, "pgrp": 10, "session": 10, "env": {"MESH_RUN_ID": RUN},
                      "state": "Z"},
                 23: {"ppid": 1, "session": 23, "pgrp": 23, "env": {}, "starttime": 10},
                 24: {"ppid": 1, "session": 24, "pgrp": 24, "env": None, "uid": "other"}}
        with FakeProc(table):
            unknown: Dict[int, int] = {}
            found = terminate.run_targets(RUN, None, None, unknown=unknown)
            self.assertEqual(set(found), {10})
            self.assertEqual(unknown, {})
            for pid in (20, 21, 23):
                self.assertFalse(terminate._environ_unknown(table[pid]["env"], pid,
                                                            FakeProc(table).stat(pid), 500), pid)

    def test_unknown_unrelated_session_not_targeted(self):
        table = {10: {"ppid": 1, "env": {"MESH_RUN_ID": RUN}},
                 30: {"ppid": 1, "session": 30, "pgrp": 30, "env": {}}}
        with FakeProc(table):
            unknown: Dict[int, int] = {}
            found = terminate.run_targets(RUN, None, None, unknown=unknown)
        self.assertEqual(set(found), {10})
        self.assertNotIn(30, unknown)

    def test_untrusted_group_raw_match_not_targeted(self):
        """Wrapper gone, nothing seen: a raw pgid match (UNKNOWN or not) is left alone."""
        table = {60: {"ppid": 1, "pgrp": 60, "session": 60, "env": {}},
                 61: {"ppid": 1, "pgrp": 60, "session": 60, "env": {"X": "1"}}}
        with FakeProc(table):
            self.assertEqual(terminate.run_targets(RUN, 60, 60, 1, seen={}), {})

    def test_deep_ancestry_closure_beyond_64_hops(self):
        """Linear #20b MINOR: a 150-deep chain of UNKNOWN setsid descendants is found in
        one scan, whatever the order: each one taken anchors the next 64 hops."""
        table = {10: {"ppid": 1, "env": {"MESH_RUN_ID": RUN}}}
        for pid in range(11, 161):
            table[pid] = {"ppid": pid - 1, "session": pid, "pgrp": pid, "env": {}}
        expected = set(range(10, 161))
        for order in (sorted(table), sorted(table, reverse=True)):
            with FakeProc(table, order=order):
                self.assertEqual(set(terminate.run_targets(RUN, None, None)), expected)

    def test_trusted_group_boundaries(self):
        """Linear #20b MINOR: other-uid and old processes INSIDE a trusted run group or
        session are group members (targets); outside it, or in an untrusted group, they
        are not UNKNOWN and are never taken."""
        table = {51: {"ppid": 1, "pgrp": 50, "session": 50, "env": {"X": "1"}},          # seen anchor
                 52: {"ppid": 1, "pgrp": 50, "session": 50, "env": None, "uid": "other"},
                 53: {"ppid": 1, "pgrp": 50, "session": 50, "env": {}, "starttime": 10},
                 56: {"ppid": 1, "pgrp": 56, "session": 50, "env": None, "uid": "other"},  # same session
                 54: {"ppid": 51, "pgrp": 54, "session": 54, "env": None, "uid": "other"},
                 55: {"ppid": 51, "pgrp": 55, "session": 55, "env": {}, "starttime": 10}}
        with FakeProc(table):
            unknown: Dict[int, int] = {}
            found = terminate.run_targets(RUN, 50, None, None, seen={51: 1000}, unknown=unknown)
            self.assertEqual(set(found), {51, 52, 53, 56})
            self.assertEqual(unknown, {})  # none of them is UNKNOWN (other uid / old / readable)
            # Untrusted (nothing seen alive, no wrapper): the same group is left alone.
            self.assertEqual(terminate.run_targets(RUN, 50, None, None, seen={}), {})

    def test_run_targets_never_sleeps(self):
        """L4/Q2: permanently UNKNOWN processes do not make a scan sleep."""
        table = {p: {"ppid": 1, "session": p, "pgrp": p, "env": None} for p in range(100, 140)}
        with FakeProc(table), mock.patch.object(terminate.time, "sleep", _no_sleep):
            self.assertEqual(terminate.run_targets(RUN, None, None), {})
            t = terminate.Termination(run_id=RUN, pgid=None, wrapper_pid=None,
                                      end_reason="timeout", grace_s=1.0)
            self.assertEqual(terminate.step(t), "pending")


class TestFullLoopRealProcesses(RealProcBase):
    """L-MINOR: UNKNOWN -> found -> parent exits / re-parented -> SIGKILL -> dead -> done."""

    _PARENT = (
        "import os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"  # exits only when told (stdin)
        "c = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print(1, flush=True); time.sleep(60)'], "
        "start_new_session=True, stdout=subprocess.PIPE)\n"
        "c.stdout.readline()\n"
        "print(c.pid, flush=True)\n"
        "sys.stdin.readline()\n"
    )

    def test_unknown_escapee_full_loop(self):
        env = os.environ.copy()
        env["MESH_RUN_ID"] = RUN
        parent = subprocess.Popen([sys.executable, "-c", self._PARENT], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, start_new_session=True)
        self._procs.append(parent)
        self.addCleanup(parent.stdout.close)
        self.addCleanup(parent.stdin.close)
        child = int(_read_line_or_fail(parent, 15.0))
        self.addCleanup(lambda: _kill_quiet(child))
        real = get_proc_environ
        # The child's environ never becomes readable (UNKNOWN for the whole test).
        fake = lambda p: {} if int(p) == child else real(p)  # noqa: E731
        with mock.patch.object(terminate, "get_proc_environ", side_effect=fake):
            t = terminate.begin(RUN, None, None, "timeout", 0.2)
            self.assertIn(child, t.initial_pids)  # found through the parent anchor
            # The parent exits; the child is re-parented and still UNKNOWN.
            try:
                parent.stdin.write(b"\n")
                parent.stdin.flush()
            except (BrokenPipeError, OSError):
                pass  # already gone: the window under test is "parent exited" either way
            parent.wait(timeout=10)
            self.assertNotEqual(get_proc_stat(child)["ppid"], parent.pid)
            deadline = time.monotonic() + 15
            outcome = "pending"
            while time.monotonic() < deadline:
                outcome = terminate.step(t)
                if outcome == "done":
                    break
                time.sleep(terminate.POLL_S)
            self.assertEqual(outcome, "done")
            self.assertIn(child, t.signalled_kill)  # ignored SIGTERM, got SIGKILL
            info = get_proc_stat(child)
            self.assertTrue(info is None or info["state"] == "Z")

    def test_readable_without_run_id_not_targeted(self):
        stranger = self.spawn(None)
        other = self.spawn("someone-else")
        ours = self.spawn(f"run_safe{_RUN_SFX}")
        self.assertTrue(get_proc_environ(stranger.pid))
        found = terminate.run_targets(f"run_safe{_RUN_SFX}", ours.pid, ours.pid,
                                      int(get_proc_stat(ours.pid)["starttime"]))
        self.assertIn(ours.pid, found)
        self.assertNotIn(stranger.pid, found)
        self.assertNotIn(other.pid, found)
        env_only = terminate.run_targets(f"run_safe{_RUN_SFX}", None, None)
        self.assertIn(ours.pid, env_only)
        self.assertNotIn(stranger.pid, env_only)
        self.assertNotIn(other.pid, env_only)

    def test_real_zombie_never_targeted(self):
        z = _spawn(f"run_z{_RUN_SFX}", args=[sys.executable, "-c", "pass"])
        self._procs.append(z)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = get_proc_stat(z.pid)
            if info is not None and info["state"] == "Z":
                break
            time.sleep(0.02)
        self.assertEqual(get_proc_stat(z.pid)["state"], "Z")
        self.assertNotIn(z.pid, terminate.run_targets(f"run_z{_RUN_SFX}", z.pid, None))
        z.wait()

    def test_recovery_env_only_after_empty_first_read(self):
        proc = self.spawn(f"run_rec{_RUN_SFX}")
        fake, _ = _empty_first(proc.pid, 1)
        with mock.patch.object(terminate, "get_proc_environ", side_effect=fake), \
                mock.patch.object(terminate, "POLL_S", 0.02), \
                mock.patch.object(supervisor, "_setting_int", return_value=1.0):
            self.assertIn(proc.pid, recovery._env_carriers(f"run_rec{_RUN_SFX}"))
            recovery._terminate_carriers(mock.Mock(), "/tmp", {"id": f"run_rec{_RUN_SFX}"}, "lost_before_spawn")
        t = terminate.get(f"run_rec{_RUN_SFX}")
        self.assertIsNotNone(t)
        self.assertIn(proc.pid, t.initial_pids)


def _kill_quiet(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class _Closing(Exception):
    pass


class TestWatchTickPath(unittest.TestCase):
    """Q1/Q5 and the changed-snapshot regression, through supervisor._watch_task_run."""

    def setUp(self):
        supervisor._CLEAR.clear()
        self.run = {"id": f"run_w{_RUN_SFX}", "task_id": "tsk_w", "pgid": 777, "wrapper_pid": 777,
                    "proc_starttime": 1, "run_dir": "/nonexistent", "kind": "task", "adapter": "fake"}
        self.changed = [False]
        self.targets = [{}]
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {"status": "running"}
        self.conn = conn
        snap = lambda run, home=None: supervisor.SeenSnapshot({}, False, self.changed[0])  # noqa: E731
        self.renewed = []
        self.p = [
            mock.patch.object(supervisor, "wrapper_alive", return_value=False),
            mock.patch.object(supervisor, "_read_json_file", return_value={"exit_code": 0}),
            mock.patch.object(supervisor, "_needs_submit_git", return_value=False),
            mock.patch.object(supervisor, "seen_snapshot", snap),
            mock.patch.object(supervisor, "snapshot_hold", return_value=False),
            mock.patch.object(supervisor.terminate, "run_targets",
                              lambda *a, **k: dict(self.targets[0])),
            mock.patch.object(supervisor, "_renew_meshd_lease",
                              lambda *a: self.renewed.append(1)),
            mock.patch.object(supervisor, "_reap_wrapper", side_effect=self._reap),
        ]
        self.reaped = []
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in reversed(self.p):
            p.stop()
        supervisor._CLEAR.clear()

    def _reap(self, run):
        self.reaped.append(self.changed[0])
        if not self.changed[0]:
            raise _Closing()  # the clean-exit close path starts right after the reap

    def _tick(self):
        try:
            return supervisor._watch_task_run(self.conn, "/tmp", self.run)
        except _Closing:
            return "closing"

    def test_first_empty_scan_keeps_run_open(self):
        with mock.patch.object(supervisor.time, "sleep", _no_sleep), \
                mock.patch.object(terminate.time, "sleep", _no_sleep):
            self.assertIsNone(self._tick())
            self.assertIsNone(self._tick())  # same poll: still open, lease renewed
        self.assertEqual(len(self.renewed), 2)
        self.assertEqual(self.reaped, [])  # wrapper unreaped until confirmed
        time.sleep(terminate.POLL_S)
        self.assertEqual(self._tick(), "closing")

    def test_changed_tick_invalidates_then_closes(self):
        self.assertIsNone(self._tick())
        time.sleep(terminate.POLL_S)
        self.changed[0] = True
        self.assertIsNone(self._tick())  # a changed-seen tick never confirms
        self.changed[0] = False
        self.assertIsNone(self._tick())  # first empty scan again
        time.sleep(terminate.POLL_S)
        self.assertEqual(self._tick(), "closing")

    def test_hit_between_scans_resets(self):
        self.assertIsNone(self._tick())
        self.targets[0] = {4242: 1}
        with mock.patch.object(supervisor, "_begin_termination", side_effect=_Closing()):
            self.assertEqual(self._tick(), "closing")
        self.assertNotIn(f"run_w{_RUN_SFX}", supervisor._CLEAR)

    def test_run_eventually_closes(self):
        """Q1 regression: empty scans a poll apart close the run within a few ticks."""
        outcomes = []
        for _ in range(4):
            outcomes.append(self._tick())
            if outcomes[-1] == "closing":
                break
            time.sleep(terminate.POLL_S)
        self.assertEqual(outcomes, [None, "closing"])

    def test_confirm_pending_exits_takes_the_second_scan(self):
        self.assertIsNone(self._tick())
        self.assertTrue(supervisor.pending_exit_checks())
        self.conn.execute.return_value.fetchone.return_value = self.run | {"status": "running"}
        with mock.patch.object(supervisor, "_watch_one", side_effect=lambda c, h, r: self._tick()) as w:
            self.assertEqual(supervisor.confirm_pending_exits(self.conn, "/tmp"), [])
            w.assert_not_called()  # not due yet
            time.sleep(terminate.POLL_S)
            self.assertEqual(supervisor.confirm_pending_exits(self.conn, "/tmp"), [f"run_w{_RUN_SFX}"])


# --- review #20b: scan -> begin() handoff window (Linear MAJOR L1) -------------------

_HANDOFF_PARENT = (
    "import os, subprocess, sys, time\n"
    "c = subprocess.Popen([sys.argv[1], '60'], env={}, start_new_session=True,\n"
    "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print(c.pid, flush=True)\n"
    "if len(sys.argv) > 2:\n"
    "    open(sys.argv[2], 'w').close()\n"
    "time.sleep(60)\n"
)


def _gone_or_zombie(pid):
    info = get_proc_stat(pid)
    return info is None or info["state"] == "Z"


class _HandoffRace:
    """Wraps terminate.run_targets: the first scan is real, then every MESH_RUN_ID
    carrier it found (the parent P) is killed before the caller goes on, so the empty-env
    setsid child C is re-parented and has no link left. That is the window between the
    discovering scan and begin()'s own scan."""

    def __init__(self, test, run_id):
        self.test, self.run_id = test, run_id
        self.real = terminate.run_targets
        self.first = None
        self.escapees = []

    def __call__(self, *a, **k):
        found = self.real(*a, **k)
        if self.first is not None:
            return found
        self.first = dict(found)
        parents = [p for p in found if (get_proc_environ(p) or {}).get("MESH_RUN_ID") == self.run_id]
        self.escapees = [p for p in found if p not in parents]
        self.test.assertTrue(parents and self.escapees, f"scan did not find P and C: {found}")
        for p in parents:
            _kill_quiet(p)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not all(_gone_or_zombie(p) for p in parents):
            time.sleep(0.01)
        for c in self.escapees:
            info = get_proc_stat(c)
            self.test.assertTrue(info is not None and int(info["ppid"]) not in parents,
                                 "escapee not re-parented")
        return found


class TestHandoffWindow(unittest.TestCase):
    """P (MESH_RUN_ID) has a setsid child C with an empty environ (UNKNOWN). The watch
    scan finds C through P; P exits before begin() scans; begin() must still get C."""

    def setUp(self):
        supervisor._CLEAR.clear()
        self.sleep_bin = shutil.which("sleep")
        self.kill_after = []

    def tearDown(self):
        for run_id in list(terminate.active()):
            terminate.forget(run_id)
        supervisor._CLEAR.clear()
        for pid in self.kill_after:
            _kill_quiet(pid)

    def _spawn_p(self, run_id):
        env = os.environ.copy()
        env["MESH_RUN_ID"] = run_id
        p = subprocess.Popen([sys.executable, "-c", _HANDOFF_PARENT, self.sleep_bin], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait(), p.stdout.close()))
        c = int(_read_line_or_fail(p, 15.0))
        self.kill_after.append(c)
        return p, c

    def _run(self, run_id, p, kind):
        st = int(get_proc_stat(p.pid)["starttime"])
        return {"id": run_id, "task_id": "tsk_h", "project_id": "prj_h", "pgid": p.pid,
                "wrapper_pid": p.pid, "proc_starttime": st, "run_dir": "/nonexistent",
                "kind": kind, "adapter": "fake"}

    def _common_patches(self, race):
        return [
            mock.patch.object(terminate, "run_targets", race),
            mock.patch.object(supervisor, "wrapper_alive", return_value=False),
            mock.patch.object(supervisor, "_read_json_file", return_value={"exit_code": 0}),
            mock.patch.object(supervisor, "_setting_int", return_value=5),
            mock.patch.object(supervisor, "transaction", lambda conn: contextlib.nullcontext()),
            mock.patch.object(supervisor, "record_event"),
            mock.patch.object(supervisor, "_log_meshd"),
            mock.patch.object(supervisor, "_reap_wrapper", side_effect=_Closing()),
        ]

    def _assert_c_taken(self, run_id, c):
        t = terminate.get(run_id)
        self.assertIsNotNone(t)
        self.assertIn(c, t.initial_pids)   # begin()'s targets include C
        self.assertIn(c, t.signalled_term)

    def test_lead_turn_leftovers_handoff(self):
        run_id = f"run_hl{_RUN_SFX}"
        p, c = self._spawn_p(run_id)
        race = _HandoffRace(self, run_id)
        with contextlib.ExitStack() as stack:
            for patch in self._common_patches(race):
                stack.enter_context(patch)
            with self.assertRaises(_Closing):
                supervisor._watch_lead_turn(mock.Mock(), "/tmp", self._run(run_id, p, "lead_turn"))
        self.assertEqual(race.escapees, [c])
        self._assert_c_taken(run_id, c)

    def test_task_leftovers_handoff(self):
        run_id = f"run_ht{_RUN_SFX}"
        p, c = self._spawn_p(run_id)
        race = _HandoffRace(self, run_id)
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {"status": "running"}
        with contextlib.ExitStack() as stack:
            for patch in self._common_patches(race):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(supervisor, "_needs_submit_git", return_value=False))
            stack.enter_context(mock.patch.object(
                supervisor, "seen_snapshot", lambda run, home=None: supervisor.SeenSnapshot({}, False, False)))
            stack.enter_context(mock.patch.object(supervisor, "snapshot_hold", return_value=False))
            with self.assertRaises(_Closing):
                supervisor._watch_task_run(conn, "/tmp", self._run(run_id, p, "task"))
        self.assertEqual(race.escapees, [c])
        self._assert_c_taken(run_id, c)

    def test_merge_test_step_leftovers_handoff(self):
        tmp = tempfile.mkdtemp(prefix="mh-ho-")
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.path.join(tmp, "integ")
        os.makedirs(cwd)
        ready = os.path.join(tmp, "ready")
        run_id = f"run_hm{os.getpid()}"
        script = os.path.join(tmp, "p.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(_HANDOFF_PARENT)
        cmd = (f"{sys.executable} {script} {self.sleep_bin} {ready} >/dev/null 2>&1 & "
               f"while [ ! -e {ready} ]; do sleep 0.02; done; exit 0")
        plan = {"run_id": run_id, "run_dir": os.path.join(tmp, "merges", "mrg_h"), "integration": cwd,
                "test_argv": ["sh", "-c", cmd], "test_timeout_s": 30, "kill_grace_s": 1}
        race = _HandoffRace(self, run_id)
        with mock.patch.object(terminate, "run_targets", race), \
                mock.patch.object(terminate, "POLL_S", 0.05):
            out = merge.run_test_step(plan)
        self.kill_after.extend(race.escapees)
        self.assertEqual(len(race.escapees), 1, race.first)
        self.assertEqual(out["termination"]["targets"], 1, out)  # begin() took C
        self.assertTrue(_gone_or_zombie(race.escapees[0]))


# --- review #20b: killing tests for the surviving mutations M13 M14 M17 M18 --------

class TestLeadTurnTwoScanGate(unittest.TestCase):
    """M14: an exited lead turn with nothing left closes only on a second empty scan."""

    def setUp(self):
        supervisor._CLEAR.clear()
        self.addCleanup(supervisor._CLEAR.clear)

    def test_lead_turn_needs_two_empty_scans(self):
        from mesh.daemon import lead_turns
        run = {"id": f"run_lt{_RUN_SFX}", "pgid": 9, "wrapper_pid": 9, "proc_starttime": 1,
               "run_dir": "/nonexistent", "kind": "lead_turn", "project_id": "prj"}
        clock = [100.0]
        with mock.patch.object(supervisor, "wrapper_alive", return_value=False), \
                mock.patch.object(supervisor, "_read_json_file", return_value={"exit_code": 0}), \
                mock.patch.object(terminate, "run_targets", return_value={}), \
                mock.patch.object(terminate, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep)), \
                mock.patch.object(supervisor, "_renew_lead_turn_lease") as renew, \
                mock.patch.object(supervisor, "_reap_wrapper") as reap, \
                mock.patch.object(supervisor, "transaction", lambda conn: contextlib.nullcontext()), \
                mock.patch.object(lead_turns, "finish_lead_turn", return_value="succeeded") as fin:
            self.assertIsNone(supervisor._watch_lead_turn(mock.Mock(), "/tmp", run))
            clock[0] += terminate.POLL_S / 2
            self.assertIsNone(supervisor._watch_lead_turn(mock.Mock(), "/tmp", run))
            fin.assert_not_called()
            reap.assert_not_called()
            self.assertEqual(renew.call_count, 2)
            clock[0] += terminate.POLL_S  # >= POLL_S after the first
            self.assertEqual(supervisor._watch_lead_turn(mock.Mock(), "/tmp", run), "succeeded")
            fin.assert_called_once()
            reap.assert_called_once()


class TestRecoveryConfirmingScan(unittest.TestCase):
    """M13: startup recovery of an exited run takes the confirming scan itself."""

    def test_exit_json_run_closed_by_recovery(self):
        tmp = tempfile.mkdtemp(prefix="mh-rc-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "exit.json"), "w", encoding="utf-8") as f:
            f.write('{"exit_code": 0}')
        supervisor._CLEAR.clear()
        self.addCleanup(supervisor._CLEAR.clear)
        state = {"status": "running", "watches": 0}

        def row(_conn, run_id):
            return {"id": run_id, "status": state["status"], "run_dir": tmp, "wrapper_pid": 77,
                    "pgid": 77, "kind": "task", "lease_holder": "old"}

        def watch(_conn, _home, run):
            # Stand-in for an exited run with nothing left: one ClearCheck step per scan.
            state["watches"] += 1
            if supervisor._clear_check(run["id"]).observe({}):
                supervisor._CLEAR.pop(run["id"], None)
                state["status"] = "succeeded"

        conn = mock.Mock()
        with mock.patch.object(recovery, "_reload_run", side_effect=row), \
                mock.patch.object(recovery, "transaction", lambda c: contextlib.nullcontext()), \
                mock.patch.object(recovery, "_identity_alive", return_value=False), \
                mock.patch.object(supervisor, "_watch_one", side_effect=watch), \
                mock.patch.object(terminate, "POLL_S", 0.02):
            out = recovery._recover_run(conn, "/tmp", {"id": f"run_rc{_RUN_SFX}", "lease_holder": "old"}, "boot")
        self.assertEqual(out, "exited")
        self.assertEqual(state["watches"], 2)
        self.assertEqual(state["status"], "succeeded")


class TestTerminationLoopPendingExits(unittest.TestCase):
    """M17: meshd's 200 ms loop also runs while only an exit confirmation is pending."""

    def test_loop_advances_pending_exit_check(self):
        supervisor._CLEAR.clear()
        self.addCleanup(supervisor._CLEAR.clear)
        supervisor._CLEAR[f"run_lp{_RUN_SFX}"] = terminate.ClearCheck(first_empty_at=1.0)
        self.assertEqual(terminate.active(), {})
        calls = []

        async def main():
            stop = asyncio.Event()

            async def notify():
                return None

            fake = types.SimpleNamespace(stop_event=stop, db_conn=object(), home="/tmp",
                                         log_error=lambda m: None, start_queued_lead_merges=lambda: None,
                                         notify_waiters=notify, wake_scheduler=lambda: None)

            def adv(conn, home):
                calls.append(1)
                stop.set()
                return [f"run_lp{_RUN_SFX}"]

            with mock.patch.object(supervisor, "advance_terminations", adv), \
                    mock.patch.object(terminate, "POLL_S", 0.01):
                await asyncio.wait_for(server.DaemonServer._termination_loop(fake), 2.0)

        asyncio.run(main())
        self.assertEqual(calls, [1])

    def test_next_exit_check_in(self):
        supervisor._CLEAR.clear()
        self.addCleanup(supervisor._CLEAR.clear)
        self.assertIsNone(supervisor.next_exit_check_in(now=10.0))
        supervisor._CLEAR["a"] = terminate.ClearCheck(first_empty_at=10.0)
        supervisor._CLEAR["b"] = terminate.ClearCheck(first_empty_at=9.95)
        supervisor._CLEAR["c"] = terminate.ClearCheck()
        self.assertAlmostEqual(supervisor.next_exit_check_in(now=10.0), terminate.POLL_S - 0.05)
        self.assertAlmostEqual(supervisor.next_exit_check_in(now=10.5), 9.95 + terminate.POLL_S - 10.5)


class TestMergeTestStepRecheck(unittest.TestCase):
    """M18: the merge test step does not conclude "no leftovers" from one empty scan."""

    def test_empty_first_scan_rechecked(self):
        tmp = tempfile.mkdtemp(prefix="mh-mr-")
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.path.join(tmp, "integ")
        os.makedirs(cwd)
        run_id = f"run_mr{os.getpid()}"
        plan = {"run_id": run_id, "run_dir": os.path.join(tmp, "merges", "mrg_r"), "integration": cwd,
                "test_argv": ["sh", "-c", "setsid sleep 30 >/dev/null 2>&1 & exit 0"],
                "test_timeout_s": 30, "kill_grace_s": 1}
        real = terminate.run_targets
        calls = {"n": 0}

        def first_empty(*a, **k):
            calls["n"] += 1
            return {} if calls["n"] == 1 else real(*a, **k)  # the environ-read race once

        def cleanup():
            t = terminate.begin(run_id, None, None, "test_cleanup", 1)
            for _ in range(100):
                if terminate.step(t) == "done":
                    break
                time.sleep(0.05)
            terminate.forget(run_id)

        self.addCleanup(cleanup)
        with mock.patch.object(terminate, "run_targets", first_empty), \
                mock.patch.object(terminate, "POLL_S", 0.05):
            out = merge.run_test_step(plan)
        self.assertEqual(out.get("leftovers"), 1, out)
        self.assertEqual(out["termination"]["targets"], 1, out)
        self.assertEqual(real(run_id, None, None), {})


# --- review #20c: one deliverable.git collection per exit, no loop spin, recovery handoffs ---

def _run_loop(seconds, advance=None, conn=None):
    """Drive meshd's real _termination_loop for ``seconds``; returns its iteration count."""
    iters, errors = [], []
    real_advance = advance or supervisor.advance_terminations

    async def main():
        stop = asyncio.Event()

        async def notify():
            return None

        fake = types.SimpleNamespace(stop_event=stop, db_conn=conn or object(), home="/tmp",
                                     log_error=lambda m: errors.append(m), start_queued_lead_merges=lambda: None,
                                     notify_waiters=notify, wake_scheduler=lambda: None)

        def adv(conn, home):
            iters.append(time.monotonic())
            return real_advance(conn, home)

        async def stopper():
            await asyncio.sleep(seconds)
            stop.set()

        with mock.patch.object(supervisor, "advance_terminations", adv):
            await asyncio.gather(server.DaemonServer._termination_loop(fake), stopper())

    asyncio.run(main())
    assert not errors, errors[:3]
    return len(iters)


class TestGitCollectedOnce(unittest.TestCase):
    """A clean code-task exit collects deliverable.git exactly once, through the real
    watch tick and meshd's real loop, and the loop does not spin while it waits."""

    def setUp(self):
        supervisor._CLEAR.clear()
        supervisor._GIT_INFO.clear()
        self.addCleanup(supervisor._CLEAR.clear)
        self.addCleanup(supervisor._GIT_INFO.clear)

    def test_one_collection_per_clean_exit(self):
        run = {"id": f"run_g1{_RUN_SFX}", "task_id": "tsk_g1", "project_id": "prj", "pgid": 778, "wrapper_pid": 778,
               "proc_starttime": 1, "run_dir": "/nonexistent", "kind": "task", "adapter": "fake",
               "status": "running"}
        task = {"id": "tsk_g1", "status": "running", "kind": "code", "workspace_path": "/nonexistent",
                "branch": "b", "base_commit": "c"}
        conn = mock.Mock()
        conn.execute.side_effect = lambda sql, args=(): mock.Mock(
            fetchone=mock.Mock(return_value=task if "FROM tasks" in sql else run))
        collections = []

        def collect(ws, branch, base):
            collections.append(1)
            time.sleep(0.3)
            return {"branch": branch, "head": "x"}

        finished = []
        with mock.patch.object(supervisor, "wrapper_alive", return_value=False), \
                mock.patch.object(supervisor, "_read_json_file", return_value={"exit_code": 0}), \
                mock.patch.object(supervisor, "_needs_submit_git", return_value=True), \
                mock.patch.object(supervisor, "submission_git_info", collect), \
                mock.patch.object(supervisor, "is_adopted", return_value=False), \
                mock.patch.object(supervisor, "seen_snapshot",
                                  lambda r, home=None: supervisor.SeenSnapshot({}, False, False)), \
                mock.patch.object(supervisor, "snapshot_hold", return_value=False), \
                mock.patch.object(terminate, "run_targets", lambda *a, **k: {}), \
                mock.patch.object(supervisor, "_renew_meshd_lease"), \
                mock.patch.object(supervisor, "_reap_wrapper"), \
                mock.patch.object(supervisor, "_finish_run",
                                  lambda c, r, e, git=None: finished.append(git) or "succeeded"):
            self.assertIsNone(supervisor._watch_one(conn, "/tmp", run))  # starts the worker
            deadline = time.monotonic() + 5
            while not supervisor._GIT_INFO[f"run_g1{_RUN_SFX}"]["done"] and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNone(supervisor._watch_one(conn, "/tmp", run))  # first empty scan
            self.assertTrue(supervisor.pending_exit_checks())
            iters = _run_loop(1.0, conn=conn)
        self.assertEqual(len(collections), 1)
        self.assertEqual(finished, [{"branch": "b", "head": "x"}])
        self.assertFalse(supervisor.pending_exit_checks())
        self.assertLessEqual(iters, 8)  # 1.0 s at a 200 ms beat, never the 5 ms floor

    def test_entry_evicted_when_run_inactive(self):
        supervisor._GIT_INFO[f"run_g2{_RUN_SFX}"] = {"done": True, "git": {}}
        supervisor._GIT_INFO[f"run_g3{_RUN_SFX}"] = {"done": True, "git": {}}
        supervisor._evict_git_info({f"run_g3{_RUN_SFX}"})
        self.assertEqual(sorted(supervisor._GIT_INFO), [f"run_g3{_RUN_SFX}"])


class TestLoopBackoff(unittest.TestCase):
    """A due exit check the loop cannot consume backs off to POLL_S (no 5 ms spin)."""

    def setUp(self):
        supervisor._CLEAR.clear()
        self.addCleanup(supervisor._CLEAR.clear)
        supervisor._CLEAR[f"run_bo{_RUN_SFX}"] = terminate.ClearCheck(first_empty_at=time.monotonic() - 1.0)
        self.conn = mock.Mock()
        self.conn.execute.return_value.fetchone.return_value = {"id": f"run_bo{_RUN_SFX}", "status": "running",
                                                                "kind": "task"}

    def _count(self, watch):
        calls, logs = [], []

        def w(*a):
            calls.append(1)
            return watch()

        with mock.patch.object(supervisor, "_watch_one", w), \
                mock.patch.object(supervisor, "_log_meshd", lambda h, m: logs.append(m)):
            iters = _run_loop(1.0, conn=self.conn)
        return len(calls), len(logs), iters

    def test_unconsumed_check_retried_at_poll(self):
        calls, _, iters = self._count(lambda: None)  # e.g. deliverable.git not ready
        self.assertGreaterEqual(calls, 3)
        self.assertLessEqual(calls, 6)
        self.assertLessEqual(iters, 8)

    def test_raising_watch_rate_limited(self):
        def boom():
            raise RuntimeError("db gone")
        calls, logs, iters = self._count(boom)
        self.assertLessEqual(calls, 6)
        self.assertEqual(logs, calls)
        self.assertLessEqual(iters, 8)

    def test_defer_and_observe(self):
        chk = terminate.ClearCheck(first_empty_at=10.0)
        self.assertTrue(chk.due(10.0 + terminate.POLL_S))
        chk.defer(now=11.0)
        self.assertFalse(chk.due(11.0 + terminate.POLL_S / 2))
        self.assertTrue(chk.due(11.0 + terminate.POLL_S))
        self.assertEqual(chk.due_at(), 11.0 + terminate.POLL_S)
        self.assertTrue(chk.observe({}, now=12.0))  # a real scan still confirms
        self.assertIsNone(chk.retry_at)
        self.assertIsNone(chk.due_at())


class TestBeginStarttimeCheck(unittest.TestCase):
    """begin() keeps a handed `seen` entry only with the live starttime (pid reuse)."""

    def test_mismatched_starttime_not_targeted(self):
        p = _spawn()
        self.addCleanup(lambda: (_kill_quiet(p.pid), p.wait()))
        st = int(get_proc_stat(p.pid)["starttime"])
        run_id = f"run_bst{_RUN_SFX}"
        self.addCleanup(terminate.forget, run_id)
        with mock.patch.object(terminate, "signal_if_same", return_value=True) as sig:
            t = terminate.begin(run_id, None, None, "test", 1, seen={p.pid: st + 1})
        self.assertNotIn(p.pid, t.seen)
        self.assertNotIn(p.pid, t.initial_pids)
        sig.assert_not_called()
        terminate.forget(run_id)
        with mock.patch.object(terminate, "signal_if_same", return_value=True):
            t = terminate.begin(run_id, None, None, "test", 1, seen={p.pid: st})
        self.assertEqual(t.seen, {p.pid: st})  # control: the right starttime is kept


_EXEC_CARRIER = (
    "import os, sys, time\n"
    "print(os.getpid(), flush=True)\n"
    "while not os.path.exists(sys.argv[2]):\n"
    "    time.sleep(0.01)\n"
    "os.execve(sys.argv[1], [sys.argv[1], '60'], {})\n"
)


def _cmdline_has(pid, needle):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return needle in f.read()
    except OSError:
        return False


class _ExecRace:
    """Wraps run_targets_or_recheck: the env-only scan is real, then every carrier it
    found exec's into an empty environment (same pid and starttime) before the caller
    goes on. begin()'s own env-only scan cannot see it any more; only the handed
    identity can (review #20c, recovery shape)."""

    def __init__(self, test, trigger):
        self.test, self.trigger = test, trigger
        self.real = terminate.run_targets_or_recheck
        self.first = None

    def __call__(self, *a, **k):
        found = self.real(*a, **k)
        if self.first is not None:
            return found
        self.first = dict(found)
        self.test.assertTrue(found, "scan did not find the carrier")
        open(self.trigger, "w").close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all("MESH_RUN_ID" not in (get_proc_environ(p) or {}) and _cmdline_has(p, b"sleep")
                   for p in found):
                break
            time.sleep(0.01)
        for p in found:
            self.test.assertEqual(int(get_proc_stat(p)["starttime"]), found[p])
            self.test.assertNotIn("MESH_RUN_ID", get_proc_environ(p) or {})
        return found


class TestRecoveryHandoffWindow(unittest.TestCase):
    """L1 handoff at the two recovery sites: recovery._lost -> _terminate_carriers and
    merge.recover_merge_runs. The carrier is found, then exec's to env={} before begin()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-rh-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.trigger = os.path.join(self.tmp, "go")
        self.sleep_bin = shutil.which("sleep")

    def _spawn_carrier(self, run_id):
        env = os.environ.copy()
        env["MESH_RUN_ID"] = run_id
        p = subprocess.Popen([sys.executable, "-c", _EXEC_CARRIER, self.sleep_bin, self.trigger], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait(), p.stdout.close()))
        self.assertEqual(int(_read_line_or_fail(p, 15.0)), p.pid)
        return p

    def test_recovery_terminate_carriers_handoff(self):
        run_id = f"run_rh{os.getpid()}"
        self.addCleanup(terminate.forget, run_id)
        p = self._spawn_carrier(run_id)
        race = _ExecRace(self, self.trigger)
        run = {"id": run_id, "task_id": "tsk_rh", "project_id": "prj", "pgid": None, "wrapper_pid": None,
               "proc_starttime": None, "run_dir": self.tmp, "kind": "task", "status": "running"}
        with mock.patch.object(terminate, "run_targets_or_recheck", race), \
                mock.patch.object(recovery, "transaction", lambda c: contextlib.nullcontext()), \
                mock.patch.object(recovery, "_log"), \
                mock.patch.object(supervisor, "_setting_int", return_value=1), \
                mock.patch.object(recovery, "_close_without_process") as close:
            out = recovery._lost(mock.Mock(), "/tmp", run, "lost_on_restart", "boot")
        self.assertEqual(out, "terminating")
        close.assert_not_called()
        self.assertEqual(list(race.first), [p.pid])
        t = terminate.get(run_id)
        self.assertEqual(t.initial_pids, [p.pid])  # begin() still targets the carrier
        self.assertIn(p.pid, t.signalled_term)

    def test_merge_recovery_handoff(self):
        run_id = f"run_mrh{os.getpid()}"
        self.addCleanup(terminate.forget, run_id)
        p = self._spawn_carrier(run_id)
        race = _ExecRace(self, self.trigger)
        run = {"id": run_id, "task_id": "tsk_mrh", "project_id": "prj", "run_dir": self.tmp,
               "kind": "merge_test", "status": "running"}
        closed = {}
        with mock.patch.object(terminate, "run_targets_or_recheck", race), \
                mock.patch.object(merge, "_kill_grace_s", return_value=1), \
                mock.patch.object(merge, "_close_recovered_merge_run",
                                  lambda c, r, pid, st, result: closed.setdefault("r", result) and "killed"):
            out = merge.recover_merge_runs(mock.Mock(), [run])
        self.assertEqual(out, {run_id: "killed"})
        self.assertEqual(list(race.first), [p.pid])
        self.assertEqual(closed["r"]["termination"]["targets"], 1)  # begin() took the carrier
        self.assertTrue(_gone_or_zombie(p.pid))


class TestRecoverRunFirstScanHandoff(unittest.TestCase):
    """Linear #20c MAJOR: _recover_run (no wrapper.json) finds P (MESH_RUN_ID) and its
    setsid env={} child C; P exits while recovery waits for the wrapper; _lost's own scan
    is then empty. C must still be terminated (handed from the first scan)."""

    def test_first_scan_identities_reach_lost(self):
        tmp = tempfile.mkdtemp(prefix="mh-rr-")
        self.addCleanup(shutil.rmtree, tmp, True)
        run_id = f"run_rr{os.getpid()}"
        self.addCleanup(terminate.forget, run_id)
        env = os.environ.copy()
        env["MESH_RUN_ID"] = run_id
        p = subprocess.Popen([sys.executable, "-c", _HANDOFF_PARENT, shutil.which("sleep")], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait(), p.stdout.close()))
        c = int(_read_line_or_fail(p, 15.0))
        self.addCleanup(_kill_quiet, c)
        run = {"id": run_id, "task_id": "tsk_rr", "project_id": "prj", "pgid": None, "wrapper_pid": None,
               "proc_starttime": None, "run_dir": tmp, "kind": "task", "status": "starting"}
        race = _HandoffRace(self, run_id)
        with mock.patch.object(terminate, "run_targets", race), \
                mock.patch.object(terminate, "POLL_S", 0.05), \
                mock.patch.object(recovery, "WRAPPER_WAIT_S", 0.2), \
                mock.patch.object(recovery, "_reload_run", return_value=run), \
                mock.patch.object(recovery, "transaction", lambda c_: contextlib.nullcontext()), \
                mock.patch.object(recovery, "_log"), \
                mock.patch.object(supervisor, "_setting_int", return_value=1), \
                mock.patch.object(recovery, "_close_without_process") as close:
            out = recovery._recover_run(mock.Mock(), "/tmp", {"id": run_id, "lease_holder": "old"}, "boot")
        self.assertEqual(race.escapees, [c])
        self.assertEqual(out, "terminating")
        close.assert_not_called()
        t = terminate.get(run_id)
        self.assertIn(c, t.initial_pids)
        self.assertIn(c, t.signalled_term)


if __name__ == "__main__":
    unittest.main()
