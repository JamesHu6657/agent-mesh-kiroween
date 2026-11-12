"""M3 gate review (Linear, main@8602d33) P1 fixes, with real processes, no mocks of
scanning, termination or the watch path.

P1-1 (N18, A8): an adopted run whose wrapper exits right after adoption, before the
first watch tick, while an env-less member (env={}) stays in the run's process group.
The adoption itself must record the member (seen), so the watch ticks terminate it
and the run stays open (and ``task:<id>`` held) while it lives.

P1-2 (N26, A8): the merge test step must scan the test run's group before it reaps
the wrapper. A test command that leaves a same-group env={} background process with
stdio on /dev/null and exits 0 must not pass without that process being terminated.
"""

import errno
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db
from mesh.core.proc import get_proc_stat, is_tgid_alive
from mesh.daemon import children, merge, recovery, supervisor, terminate

# A wrapper stand-in: a session leader that starts an env={} member in its own group
# (stdio on /dev/null, SIGTERM default), prints "<wrapper pid> <member pid>", then
# exits as soon as the trigger file exists. Started through a launcher that exits at
# once, so the wrapper is NOT this test process's child (as an adopted wrapper is not
# meshd's child): once it exits, nothing keeps its pid from being reaped.
_LAUNCHER = r"""
import os, subprocess, sys
w = subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
print(w.stdout.readline().decode().strip(), flush=True)
os._exit(0)
"""
_ESCAPEE = r"""
import subprocess, sys
e = subprocess.Popen([sys.argv[1], "120"], env={}, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(e.pid, flush=True)
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


def _alive(pid, starttime=None):
    info = get_proc_stat(pid)
    if info is None or not is_tgid_alive(pid, info):
        return False
    return starttime is None or int(info["starttime"]) == int(starttime)


def _kill_same(pid, starttime):
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


class TestAdoptWrapperExitsBeforeFirstWatch(unittest.TestCase):
    """ACC: N18, A8 (M3 gate P1-1)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-gf-adopt-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "logs"))
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)
        self.run_id = f"run_gfa{os.getpid()}"
        self.task_id = "tsk_gfa"
        self.run_dir = os.path.join(self.tmp, "run")
        os.makedirs(self.run_dir)
        self.addCleanup(self._forget)
        self._forget()

    def _forget(self):
        supervisor._CLOSE_REFUSED_LOGGED.pop(self.run_id, None)
        terminate.forget(self.run_id)
        supervisor._CLEAR.pop(self.run_id, None)
        supervisor._evict_seen(set())
        supervisor._SEEN_EVENT_PENDING.pop(self.run_id, None)
        supervisor._SEEN_EVENT_DONE.discard(self.run_id)

    def _rows(self, wrapper, wst):
        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_a','agent','a',?);", (now,))
        ex("INSERT INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
           "VALUES ('agt_a','fake','launch',1,?,?);", (now, now))
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_a','p','g','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
           "timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
           "VALUES (?,'prj_a','t','s','general','running','agt_a','edit',600,1,1,'usr',?,?);",
           (self.task_id, now, now))
        ex("INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
           "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
           "VALUES (?,'task',?,'prj_a','agt_a',1,1,'fake','running','boot_old',?,?,?,?,?);",
           (self.run_id, self.task_id, wrapper, wrapper, wst, self.run_dir, now))
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, "
           "expires_at, heartbeat_at) VALUES (?,?,'meshd','boot_old','agt_a',1,?,?,?);",
           (f"task:{self.task_id}", self.run_id, now, now_iso_utc(time.time() + 3600), now))
        with open(os.path.join(self.run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wrapper, "starttime": wst}, f)

    def _assert_marker(self, reason):
        with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual((doc.get("mesh_seen_marker"), doc.get("reason")), ("unusable", reason), doc)
        self.assertNotIn("members", doc)

    def _status(self):
        return self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0]

    def _lease(self):
        return self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (self.run_id,)).fetchone()

    def test_wrapper_exits_between_adopt_and_first_watch(self):
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        info = get_proc_stat(member)
        self.assertEqual((info["pgrp"], info["session"]), (wrapper, wrapper))
        self.assertNotEqual(get_proc_stat(wrapper)["ppid"], os.getpid())
        self._rows(wrapper, wst)
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                          "VALUES ('kill.grace_s', '1', ?, 'test');", (now_iso_utc(),))

        out = recovery._recover_run(self.conn, self.home,
                                    {"id": self.run_id, "lease_holder": "boot_old"}, "boot_new")
        self.assertEqual(out, "adopted")
        # Linear's window: the wrapper exits right after adoption, before any watch tick.
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15), "wrapper did not exit")
        self.assertTrue(_alive(member, mst))

        violations = []
        terminated = {"v": False}
        end = time.monotonic() + 30
        while time.monotonic() < end:
            alive = _alive(member, mst)
            status = self._status()
            if alive and status not in ("starting", "running"):
                violations.append(f"run {status} while the member lives")
            if alive and self._lease() is None:
                violations.append("task lease released while the member lives")
            if status not in ("starting", "running"):
                break
            run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
            supervisor._watch_one(self.conn, self.home, run)
            if terminate.get(self.run_id) is not None:
                terminated["v"] = True
            time.sleep(0.05)
        self.assertEqual(violations, [])
        self.assertTrue(terminated["v"], "the member was never targeted")
        self.assertFalse(_alive(member, mst), "member survived")
        self.assertNotIn(self._status(), ("starting", "running"), "run never closed")
        self.assertIsNone(self._lease())
        term = self.conn.execute("SELECT payload_json FROM events WHERE type = 'run.leftovers_terminated' "
                                 "AND run_id = ?;", (self.run_id,)).fetchall()
        self.assertEqual(len(term), 1)
        self.assertIn(member, json.loads(term[0][0])["pids"])

    def test_adopt_records_member_without_any_watch(self):
        """The adoption scan alone records the member (in memory and seen.json)."""
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        out = recovery._recover_run(self.conn, self.home,
                                    {"id": self.run_id, "lease_holder": "boot_old"}, "boot_new")
        self.assertEqual(out, "adopted")
        with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
            members = json.load(f)["members"]
        self.assertIn({"pid": member, "starttime": mst}, members)
        self.assertIn({"pid": wrapper, "starttime": wst}, members)
        self.assertEqual(supervisor._SEEN_TICKS.get(self.run_id, 0), 0, "first watch must scan again")


    def test_wrapper_exits_during_adoption_scan_fails_closed(self):
        """ACC: N18, A8 (gate fix round 2) - Linear's window: the real adoption scan
        finds the member, then the wrapper exits before the identity re-check. The scan
        is not trusted and must not count as "no members": recovery reports
        adopted_uncertain, the run stays open and task:<id> held while the env-less
        member lives (nothing signals it by pgid alone), run.seen_unusable and the
        persisted hold are recorded; once the member is gone the run closes."""
        from unittest import mock
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        real_scan = supervisor._scan_group
        scans = []

        def scan_then_wrapper_exits(pgid):
            found = real_scan(pgid)  # the real scan, while the wrapper lives
            scans.append(dict(found))
            open(trigger, "w").close()  # only a scheduling hook: the wrapper exits now
            assert _wait(lambda: not _alive(wrapper, wst), 15), "wrapper did not exit"
            return found

        with mock.patch.object(supervisor, "_scan_group", scan_then_wrapper_exits):
            out = recovery._recover_run(self.conn, self.home,
                                        {"id": self.run_id, "lease_holder": "boot_old"}, "boot_new")
        self.assertEqual(len(scans), 1)
        self.assertIn(member, scans[0])
        self.assertEqual(out, "adopted_uncertain")
        self.assertTrue(_alive(member, mst))
        self._assert_marker("adopt_scan_untrusted")
        hold = self.conn.execute("SELECT value FROM meta WHERE key = ?;",
                                 (f"seen_hold:{self.run_id}",)).fetchone()
        self.assertIsNotNone(hold, "hold not persisted")
        self.assertEqual(json.loads(hold[0])["reason"], "adopt_scan_untrusted")
        ev = self.conn.execute("SELECT payload_json FROM events WHERE type = 'run.seen_unusable' "
                               "AND run_id = ?;", (self.run_id,)).fetchall()
        self.assertEqual([json.loads(e[0])["reason"] for e in ev], ["adopt_scan_untrusted"])

        def tick():
            run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
            if run["status"] in ("starting", "running"):
                supervisor._watch_one(self.conn, self.home, run)

        # Well past two scans a poll apart and a full kill.grace_s: still held, unsignalled.
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            tick()
            self.assertTrue(_alive(member, mst), "member signalled by pgid alone")
            self.assertIn(self._status(), ("starting", "running"), "run closed while the member lives")
            self.assertIsNotNone(self._lease(), "task lease released while the member lives")
            time.sleep(0.05)
        # The operator path (or the member ending) resolves it: the member exits.
        os.kill(member, signal.SIGKILL)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))
        self.assertTrue(_wait(lambda: (tick(), self._status() not in ("starting", "running"))[1], 15),
                        "run never closed after the member was gone")
        self.assertIsNone(self._lease())

    def test_wrapper_exits_after_identity_check_before_adoption_scan(self):
        """ACC: N18, A8 (gate fix round 2, Quest review): the wider window, from the
        identity check in _recover_run to the adoption scan. The wrapper exits just
        before establish_adopted_seen runs: fails closed (held), never "no members"."""
        from unittest import mock
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        real = supervisor.establish_adopted_seen

        def exit_then_establish(run, home=None):
            open(trigger, "w").close()  # scheduling hook only
            assert _wait(lambda: not _alive(wrapper, wst), 15), "wrapper did not exit"
            return real(run, home)

        with mock.patch.object(supervisor, "establish_adopted_seen", exit_then_establish):
            out = recovery._recover_run(self.conn, self.home,
                                        {"id": self.run_id, "lease_holder": "boot_old"}, "boot_new")
        self.assertEqual(out, "adopted_uncertain")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM meta WHERE key = ?;",
                                               (f"seen_hold:{self.run_id}",)).fetchone())
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
            supervisor._watch_one(self.conn, self.home, run)
            self.assertTrue(_alive(member, mst))
            self.assertIn(self._status(), ("starting", "running"))
            self.assertIsNotNone(self._lease())
            time.sleep(0.05)

    def test_adopt_past_deadline_hands_adoption_members_to_termination(self):
        """ACC: N18, A8 (Quest review mutant 1): past the deadline, the adoption-time
        members are handed to begin(); if the wrapper is gone by then, the env-less
        member is still targeted (seen=None would lose it)."""
        from unittest import mock
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        self.conn.execute("UPDATE runs SET deadline_at = ? WHERE id = ?;",
                          (now_iso_utc(time.time() - 60), self.run_id))
        real = supervisor._begin_termination

        def exit_then_begin(*a, **kw):
            open(trigger, "w").close()  # scheduling hook only
            assert _wait(lambda: not _alive(wrapper, wst), 15), "wrapper did not exit"
            return real(*a, **kw)

        with mock.patch.object(supervisor, "_begin_termination", exit_then_begin):
            out = recovery._recover_run(self.conn, self.home,
                                        {"id": self.run_id, "lease_holder": "boot_old"}, "boot_new")
        self.assertEqual(out, "adopted_timeout")
        t = terminate.get(self.run_id)
        self.assertIsNotNone(t)
        self.assertIn(member, t.initial_pids)
    def _uncertain_adopt_with_old_file(self, via_recover=False, patches=(), marker=True):
        """Round 3 setup: a valid seen.json from an earlier trusted scan lists only the
        wrapper (the member came later); then recovery's real scan finds the member and
        the wrapper exits before the identity re-check."""
        from unittest import mock
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertTrue(supervisor._persist_seen(run, {wrapper: wst}, self.home))
        seen_path = os.path.join(self.run_dir, "seen.json")
        with open(seen_path, "r", encoding="utf-8") as f:
            self.assertEqual([m["pid"] for m in json.load(f)["members"]], [wrapper])
        self._forget()  # a restart: memory gone, the old valid file stays
        real_scan = supervisor._scan_group

        def scan_then_wrapper_exits(pgid):
            found = real_scan(pgid)
            open(trigger, "w").close()  # scheduling hook only
            assert _wait(lambda: not _alive(wrapper, wst), 15), "wrapper did not exit"
            return found

        import contextlib
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(supervisor, "_scan_group", scan_then_wrapper_exits))
            for p in patches:
                stack.enter_context(p)
            if via_recover:
                summary = recovery.recover(self.conn, self.home, "boot_new")
                out = "adopted_uncertain" if summary.get("runs_adopted_uncertain") == 1 else summary
            else:
                out = recovery._recover_run(self.conn, self.home,
                                            {"id": self.run_id, "lease_holder": "boot_old"}, "boot_new")
        self.assertEqual(out, "adopted_uncertain")
        if marker:
            self._assert_marker("adopt_scan_untrusted")
        self._wrapper = (wrapper, wst)
        return member, mst

    def _restart(self):
        """A meshd restart: memory cleared, full recover() (refreshes persisted holds)."""
        self._forget()
        summary = recovery.recover(self.conn, self.home, f"boot_r{time.monotonic_ns()}")
        self.assertEqual(summary["runs_lost"], 0, summary)
        return summary

    def _hold_row(self):
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?;", (f"seen_hold:{self.run_id}",)).fetchone()
        return json.loads(r[0]) if r else None

    def test_r4_marker_write_fails_old_file_never_wins(self):
        """ACC: N18, A8 (round 4, Linear): the marker cannot be written (rename and the
        tmp unlink fail, PermissionError injected); the old valid file stays, the run is
        held in memory and by the persisted hold row (with its token); after a restart
        the row wins over the old file: held, never lost."""
        from unittest import mock
        real_replace, real_unlink = os.replace, os.unlink

        def deny_replace(src, dst, *a, **kw):
            if ".marker." in str(src):
                raise PermissionError(13, "injected", src)
            return real_replace(src, dst, *a, **kw)

        def deny_unlink(p, *a, **kw):
            if ".marker." in str(p):
                raise PermissionError(13, "injected", p)
            return real_unlink(p, *a, **kw)
        member, mst = self._uncertain_adopt_with_old_file(
            patches=(mock.patch.object(supervisor.os, "replace", deny_replace),
                     mock.patch.object(supervisor.os, "unlink", deny_unlink)), marker=False)
        with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
            old = json.load(f)
        self.assertIn("members", old)  # the old valid file is still there
        row = self._hold_row()
        self.assertEqual(row["reason"], "adopt_scan_untrusted")
        self.assertTrue(row.get("token"))
        self._assert_held(member, mst, 8)
        self._restart()
        self._assert_held(member, mst)

    def test_r4_crash_after_marker_before_hold_row(self):
        """Round 4 crash point: the durable marker is written, the process dies before
        the hold row/event are persisted. The restart holds the run (marker = unusable,
        never "no members") and persists the hold on boot."""
        from unittest import mock
        member, mst = self._uncertain_adopt_with_old_file(
            patches=(mock.patch.object(supervisor, "report_seen_unusable", lambda *a, **k: None),))
        self.assertIsNone(self._hold_row())
        self._restart()
        self.assertEqual((self._hold_row() or {}).get("reason"), "adopt_scan_untrusted",
                         "hold not persisted on boot with the marker's reason")
        self._assert_held(member, mst)

    def test_r4_crash_after_adopt_commit_before_scan(self):
        """Round 4 crash point: the adoption committed (adopt_pending marker already
        durable), the wrapper exits and the process dies before the adoption scan. The
        restart (wrapper dead) holds the run: the pending marker is never "no members"."""
        from unittest import mock
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertTrue(supervisor._persist_seen(run, {wrapper: wst}, self.home))
        self._forget()

        def crash(*a, **k):
            open(trigger, "w").close()
            assert _wait(lambda: not _alive(wrapper, wst), 15)
            raise KeyboardInterrupt("simulated crash before the adoption scan")
        with mock.patch.object(supervisor, "establish_adopted_seen", crash):
            with self.assertRaises(KeyboardInterrupt):
                recovery.recover(self.conn, self.home, "boot_crash")
        self._assert_marker("adopt_pending")
        self._restart()
        self.assertEqual((self._hold_row() or {}).get("reason"), "adopt_pending")
        self._assert_held(member, mst)

    def test_r4_hold_row_write_fails_event_not_committed_alone(self):
        """Round 4 (Linear): the event and the hold row commit in one transaction. A
        failing hold-row insert (real SQLite trigger) leaves neither; after a restart the
        marker still holds the run and both are written then."""
        self.conn.execute("CREATE TRIGGER gf_no_hold BEFORE INSERT ON meta "
                          "WHEN NEW.key LIKE 'seen_hold:%' BEGIN SELECT RAISE(ABORT, 'injected'); END;")
        member, mst = self._uncertain_adopt_with_old_file()
        self.assertIsNone(self._hold_row())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.seen_unusable' "
                                           "AND run_id = ?;", (self.run_id,)).fetchone()[0], 0)
        self.conn.execute("DROP TRIGGER gf_no_hold;")
        self._restart()
        self.assertIsNotNone(self._hold_row())
        self._assert_held(member, mst)

    def test_r4_crash_before_marker_is_a_clean_restart(self):
        """Round 4 crash point before the marker: nothing of this adoption was committed
        (the marker comes first). The next boot adopts again (wrapper alive); the
        trusted scan records the member, which is terminated when the wrapper exits."""
        from unittest import mock
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                          "VALUES ('kill.grace_s', '1', ?, 'test');", (now_iso_utc(),))

        def crash(*a, **k):
            raise KeyboardInterrupt("simulated crash before the marker")
        with mock.patch.object(supervisor, "prepare_adopt_seen", crash):
            with self.assertRaises(KeyboardInterrupt):
                recovery.recover(self.conn, self.home, "boot_crash")
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "seen.json")))
        self.assertEqual(self.conn.execute("SELECT holder FROM leases WHERE run_id = ?;",
                                           (self.run_id,)).fetchone()[0], "boot_old")
        self._forget()
        summary = recovery.recover(self.conn, self.home, "boot_new")
        self.assertEqual((summary["runs_adopted"], summary["runs_adopted_uncertain"]), (1, 0))
        with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
            self.assertIn(member, [m["pid"] for m in json.load(f)["members"]])
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))

        def tick_until_closed():
            run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
            if run["status"] in ("starting", "running"):
                if _alive(member, mst):
                    self.assertIsNotNone(self._lease())
                supervisor._watch_one(self.conn, self.home, run)
                return False
            return True
        self.assertTrue(_wait(tick_until_closed, 30, 0.05), "run never closed")
        self.assertFalse(_alive(member, mst), "member survived")
        self.assertIsNone(self._lease())

    def test_r4_event_failure_does_not_commit_hold_row_alone(self):
        """Round 4: event and hold row are one transaction, also when the event insert
        fails (injected): neither is committed; the next report writes both."""
        from unittest import mock
        real = supervisor.record_event
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1 and kw.get("type_") == "run.seen_unusable":
                raise sqlite3.OperationalError("injected")
            return real(*a, **kw)
        member, mst = self._uncertain_adopt_with_old_file(
            patches=(mock.patch.object(supervisor, "record_event", flaky),))
        self.assertGreaterEqual(calls["n"], 1)
        self.assertIsNone(self._hold_row(), "hold row committed without its event")
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        supervisor.report_seen_unusable(self.conn, run, self.home)
        self.assertIsNotNone(self._hold_row())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.seen_unusable' "
                                           "AND run_id = ?;", (self.run_id,)).fetchone()[0], 1)
        self._assert_held(member, mst, 8)

    def test_r4_trusted_rescan_after_hold_carries_token(self):
        """Round 4: a persisted adoption hold is lifted only by a seen.json written by a
        scan trusted after it; that file carries the hold's token (a crash before the
        row is deleted must not leave a file the next boot rejects for no reason)."""
        from unittest import mock
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?);",
                          (f"seen_hold:{self.run_id}", json.dumps({"run_id": self.run_id,
                           "reason": "adopt_pending", "since": now_iso_utc(), "token": "tok_gf"})))
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        supervisor.write_unusable_marker(run, "adopt_pending")
        self._forget()
        with mock.patch.object(supervisor, "report_seen_unusable", lambda *a, **k: None):
            summary = recovery.recover(self.conn, self.home, "boot_new")  # wrapper alive
        self.assertEqual((summary["runs_adopted"], summary["runs_adopted_uncertain"]), (1, 0))
        with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc.get("hold_token"), "tok_gf")
        self.assertIn(member, [m["pid"] for m in doc["members"]])
        # A restart before the row was deleted: the file is accepted (token matches).
        self._forget()
        supervisor.refresh_persisted_holds(self.conn, self.home)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertEqual(supervisor.load_seen_state(run)[0], "ok")

    def test_r5_adopt_transaction_fails_cache_not_used(self):
        """Round 5 (Linear P1-a): the adopt transaction fails (a real SQLite trigger
        rejects run.adopted) after the marker; the daemon goes on. The watch must not
        use the cached old members: the run stays held while the member lives."""
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertTrue(supervisor._persist_seen(run, {wrapper: wst}, self.home))  # old valid file
        self._forget()
        self.conn.execute("CREATE TRIGGER gf_no_adopt BEFORE INSERT ON events "
                          "WHEN NEW.type = 'run.adopted' BEGIN SELECT RAISE(ABORT, 'injected'); END;")
        summary = recovery.recover(self.conn, self.home, "boot_new")
        self.assertEqual(summary["run_errors"], 1, summary)
        self._assert_marker("adopt_pending")
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        self._assert_held(member, mst)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.seen_unusable' "
                                           "AND run_id = ?;", (self.run_id,)).fetchone()[0], 1)

    def test_r5_crash_between_marker_fsync_and_replace(self):
        """Round 5 (Linear P1-b): crash after the tmp marker is fsynced, before the
        rename: the old valid seen.json stays (plus a stray tmp). The wrapper exits while
        meshd is down; the next full recover() finds the group occupied beyond the
        recorded members and holds the run (never lost with the member alive)."""
        from unittest import mock
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertTrue(supervisor._persist_seen(run, {wrapper: wst}, self.home))  # old valid file
        self._forget()
        real_replace = os.replace

        def crash_replace(src, dst, *a, **kw):
            if ".marker." in str(src):
                raise KeyboardInterrupt("simulated crash before the marker rename")
            return real_replace(src, dst, *a, **kw)
        with mock.patch.object(supervisor.os, "replace", crash_replace):
            with self.assertRaises(KeyboardInterrupt):
                recovery.recover(self.conn, self.home, "boot_crash")
        with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
            self.assertEqual([m["pid"] for m in json.load(f)["members"]], [wrapper])
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        self._restart()
        self.assertEqual((self._hold_row() or {}).get("reason"), "wrapper_gone_group_occupied")
        self._assert_held(member, mst)

    def test_r5_flush_path_commits_event_with_hold_row(self):
        """Round 5 (Linear P2): the watch's pending-event flush goes through the same
        transaction as the hold row: with the hold-row insert failing (real trigger)
        no event is committed alone; once it can, both are written."""
        from unittest import mock
        member, mst = self._uncertain_adopt_with_old_file(
            patches=(mock.patch.object(supervisor, "report_seen_unusable", lambda *a, **k: None),))
        self.conn.execute("CREATE TRIGGER gf_no_hold2 BEFORE INSERT ON meta "
                          "WHEN NEW.key LIKE 'seen_hold:%' BEGIN SELECT RAISE(ABORT, 'injected'); END;")
        supervisor.flush_pending_seen_events(self.conn, self.home)
        n = lambda: self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.seen_unusable' "
                                      "AND run_id = ?;", (self.run_id,)).fetchone()[0]
        self.assertEqual((n(), self._hold_row()), (0, None))
        self.conn.execute("DROP TRIGGER gf_no_hold2;")
        supervisor.flush_pending_seen_events(self.conn, self.home)
        self.assertEqual(n(), 1)
        self.assertIsNotNone(self._hold_row())
        self._assert_held(member, mst, 8)

    def _wrapper_member_escapee(self):
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        esc = int(subprocess.run([sys.executable, "-c", _ESCAPEE, shutil.which("sleep")],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                                 check=True).stdout.decode().split()[0])
        ids = {p: get_proc_stat(p)["starttime"] for p in (wrapper, member, esc)}
        for p, st in ids.items():
            self.addCleanup(_kill_same, p, st)
        self.assertNotEqual(get_proc_stat(esc).get("pgrp", esc), wrapper)
        self._rows(wrapper, ids[wrapper])
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                          "VALUES ('kill.grace_s', '1', ?, 'test');", (now_iso_utc(),))
        return trigger, wrapper, member, esc, ids

    def _close_or_hold(self, esc, est, limit=30):
        """Tick until the run closes; it may never close while the escapee lives, and
        the escapee must end up terminated."""
        def tick():
            run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
            if run["status"] not in ("starting", "running"):
                return True
            if _alive(esc, est):
                self.assertIsNotNone(self._lease(), "lease released while the escapee lives")
            else:
                return True  # escapee gone; the run may stay held for the env-less member
            supervisor._watch_one(self.conn, self.home, run)
            return False
        self.assertTrue(_wait(tick, limit, 0.05), "neither closed nor escapee terminated")
        self.assertFalse(_alive(esc, est), "escapee survived")
        if self._status() not in ("starting", "running"):
            self.assertIsNone(self._lease())
        else:
            self.assertIsNotNone(self._lease())

    def _escapee_crash_after_commit(self, wrapper_alive_at_boot):
        from unittest import mock
        trigger, wrapper, member, esc, ids = self._wrapper_member_escapee()
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        # An earlier trusted scan recorded the wrapper and the escapee (it left the group).
        self.assertTrue(supervisor._persist_seen(run, {wrapper: ids[wrapper], esc: ids[esc]}, self.home))
        self._forget()

        def crash(*a, **k):
            raise KeyboardInterrupt("simulated crash after the adopt commit")
        with mock.patch.object(supervisor, "establish_adopted_seen", crash):
            with self.assertRaises(KeyboardInterrupt):
                recovery.recover(self.conn, self.home, "boot_crash")
        with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertIn(esc, [m["pid"] for m in doc["prior_members"]])
        if not wrapper_alive_at_boot:
            open(trigger, "w").close()
            self.assertTrue(_wait(lambda: not _alive(wrapper, ids[wrapper]), 15))
        self._forget()
        summary = recovery.recover(self.conn, self.home, "boot_new")
        self.assertEqual(summary["runs_lost"], 0, summary)
        if _alive(esc, ids[esc]):
            self.assertIsNotNone(self._lease())
            self.assertIn(self._status(), ("starting", "running"))
        if wrapper_alive_at_boot:
            open(trigger, "w").close()
            self.assertTrue(_wait(lambda: not _alive(wrapper, ids[wrapper]), 15))
        self._close_or_hold(esc, ids[esc])

    def test_r5_escapee_crash_after_commit_wrapper_alive(self):
        """Round 5 (Quest review P1-1, e1/e6): the marker keeps the escapee recorded in
        the old seen.json; crash after the adopt commit; wrapper alive at boot: the
        escapee is terminated, never lost with the lease gone."""
        self._escapee_crash_after_commit(True)

    def test_r5_escapee_crash_after_commit_wrapper_dead(self):
        """Round 5 (Quest review P1-1, e5): same, wrapper dead at boot."""
        self._escapee_crash_after_commit(False)

    def test_r5_escapee_kept_on_untrusted_adopt(self):
        """Round 5 (Quest review P2-1): an untrusted adoption scan keeps the members
        known before it (the escapee) as termination targets, in memory and in the
        marker."""
        from unittest import mock
        trigger, wrapper, member, esc, ids = self._wrapper_member_escapee()
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertTrue(supervisor._persist_seen(run, {wrapper: ids[wrapper], esc: ids[esc]}, self.home))
        self._forget()
        real_scan = supervisor._scan_group

        def scan_then_wrapper_exits(pgid):
            found = real_scan(pgid)
            open(trigger, "w").close()
            assert _wait(lambda: not _alive(wrapper, ids[wrapper]), 15)
            return found
        with mock.patch.object(supervisor, "_scan_group", scan_then_wrapper_exits):
            out = recovery._recover_run(self.conn, self.home,
                                        {"id": self.run_id, "lease_holder": "boot_old"}, "boot_new")
        self.assertEqual(out, "adopted_uncertain")
        with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["reason"], "adopt_scan_untrusted")
        self.assertIn(esc, [m["pid"] for m in doc["prior_members"]])
        self.assertIn(esc, supervisor._SEEN[self.run_id])
        self._close_or_hold(esc, ids[esc])

    def test_r5_old_file_members_stay_targets_under_token_hold(self):
        """Round 5: the marker could not replace an old valid file (marker write failed)
        and an adoption hold with its token is persisted. After a restart with the
        wrapper dead the old file is unusable (no token), but the escapee it records is
        still terminated; the run is never closed while it lives."""
        trigger, wrapper, member, esc, ids = self._wrapper_member_escapee()
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertTrue(supervisor._persist_seen(run, {wrapper: ids[wrapper], esc: ids[esc]}, self.home))
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?);",
                          (f"seen_hold:{self.run_id}", json.dumps({"run_id": self.run_id,
                           "reason": "adopt_scan_untrusted", "since": now_iso_utc(), "token": "tok_x"})))
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, ids[wrapper]), 15))
        self._restart()
        self._close_or_hold(esc, ids[esc])

    def test_r5_trusted_adopt_old_file_equals_scan_replaces_marker(self):
        """Round 5 (X9): the old file already lists exactly the current members; the
        trusted adoption scan must still replace the adopt_pending marker; after a
        restart with the wrapper dead the member is terminated (not only held)."""
        trigger = os.path.join(self.tmp, "trigger")
        launcher = subprocess.run(
            [sys.executable, "-c", _LAUNCHER, _WRAPPER, shutil.which("sleep"), trigger],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        wrapper, member = (int(x) for x in launcher.stdout.decode().split())
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self._rows(wrapper, wst)
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                          "VALUES ('kill.grace_s', '1', ?, 'test');", (now_iso_utc(),))
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertTrue(supervisor._persist_seen(run, {wrapper: wst, member: mst}, self.home))
        self._forget()
        summary = recovery.recover(self.conn, self.home, "boot_new")
        self.assertEqual((summary["runs_adopted"], summary["runs_adopted_uncertain"]), (1, 0))
        with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertNotIn("mesh_seen_marker", doc)
        self.assertEqual(sorted(m["pid"] for m in doc["members"]), sorted([wrapper, member]))
        # Round 6: the adopt_pending event queued at prepare is dropped by the trusted scan.
        supervisor.flush_pending_seen_events(self.conn, self.home)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.seen_unusable' "
                                           "AND run_id = ?;", (self.run_id,)).fetchone()[0], 0)
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        self._forget()
        summary = recovery.recover(self.conn, self.home, "boot_after")
        self.assertEqual(summary["runs_lost"], 0, summary)
        self._close_or_hold(member, mst)
        self.assertTrue(_wait(lambda: (supervisor._watch_one(self.conn, self.home, self.conn.execute(
            "SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()) if self._status() in
            ("starting", "running") else None) or self._status() not in ("starting", "running"), 15, 0.05))
        self.assertIsNone(self._lease())

    def _runwrap_with_member(self):
        """Real mesh.runwrap (session leader, wrapper.json/exit.json protocol); its command
        leaves an env-less same-group member running and exits 0 once ``trigger`` exists."""
        trigger = os.path.join(self.tmp, "trigger")
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cmd = (f"env -i {sys.executable} -c 'import time; time.sleep(120)' >/dev/null 2>&1 & "
               f"while [ ! -e {trigger} ]; do sleep 0.02; done; exit 0")
        with open(os.path.join(self.run_dir, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"argv": ["sh", "-c", cmd], "cwd": self.run_dir, "stdin": "devnull"}, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        env["PYTHONPATH"] = root
        launcher = subprocess.run(
            [sys.executable, "-c",
             "import os, subprocess, sys\n"
             "p = subprocess.Popen([sys.executable, '-P', '-s', '-m', 'mesh.runwrap', sys.argv[1]],"
             " start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
             " stderr=subprocess.DEVNULL)\nprint(p.pid, flush=True)\nos._exit(0)\n", self.run_dir],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=30, check=True)
        wrapper = int(launcher.stdout.decode().split()[0])
        wpath = os.path.join(self.run_dir, "wrapper.json")
        self.assertTrue(_wait(lambda: os.path.exists(wpath), 15), "runwrap wrote no wrapper.json")

        def find_member():
            for pid in supervisor._scan_group(wrapper):
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        argv = f.read().split(b"\0")
                    if len(argv) > 2 and argv[1] == b"-c" and argv[2] == b"import time; time.sleep(120)":
                        return pid  # the member itself, not the sh whose script mentions it
                except OSError:
                    pass
            return None
        self.assertTrue(_wait(lambda: find_member() is not None, 15), "member not started")
        member = find_member()
        wst = get_proc_stat(wrapper)["starttime"]
        mst = get_proc_stat(member)["starttime"]
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self.addCleanup(lambda: [_kill_same(p, st) for p, st in supervisor._scan_group(wrapper).items()])
        self.assertEqual(get_proc_stat(member)["pgrp"], wrapper)
        return trigger, wrapper, wst, member, mst

    def test_r6_exit_json_path_does_not_close_occupied_group(self):
        """Round 6 (Linear P1-a): real runwrap; the old seen.json records only the
        wrapper; crash before the first marker replace; then the command exits 0
        (exit.json written) leaving an env-less same-group member. A full recover() goes
        through the exit.json -> watch path: the close-safety gate refuses the close; the
        run is held (never exited with the lease released while the member lives)."""
        from unittest import mock
        trigger, wrapper, wst, member, mst = self._runwrap_with_member()
        self._rows(wrapper, wst)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertTrue(supervisor._persist_seen(run, {wrapper: wst}, self.home))
        self._forget()
        real_replace = os.replace

        def crash_replace(src, dst, *a, **kw):
            if ".marker." in str(src):
                raise KeyboardInterrupt("simulated crash before the first marker replace")
            return real_replace(src, dst, *a, **kw)
        with mock.patch.object(supervisor.os, "replace", crash_replace):
            with self.assertRaises(KeyboardInterrupt):
                recovery.recover(self.conn, self.home, "boot_crash")
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "exit.json")))
        self._forget()
        summary = recovery.recover(self.conn, self.home, f"boot_r{time.monotonic_ns()}")
        self.assertEqual((summary["runs_lost"], summary.get("runs_exited", 0)), (0, 0), summary)
        self.assertEqual(summary["runs_terminating"], 1, summary)  # Quest P3-3 (M7)
        self.assertEqual((self._hold_row() or {}).get("reason"), "wrapper_gone_group_occupied")
        self._assert_held(member, mst)

    def test_r6_spawn_window_marker_with_null_db_identity(self):
        """Round 6 (Linear P1-b): real runwrap; the DB has no wrapper_pid/pgid/starttime
        yet (TX B not committed) but wrapper.json exists; prepare writes adopt_pending,
        crash before the adopt transaction; the wrapper dies (SIGKILL, no exit.json). A
        full recover() takes the identity from wrapper.json, honours the marker and
        holds the run (never lost while the same-group member lives)."""
        from unittest import mock
        trigger, wrapper, wst, member, mst = self._runwrap_with_member()
        self._rows(wrapper, wst)
        self.conn.execute("UPDATE runs SET status = 'starting', wrapper_pid = NULL, pgid = NULL, "
                          "proc_starttime = NULL WHERE id = ?;", (self.run_id,))
        real_prepare = supervisor.prepare_adopt_seen

        def prepare_then_crash(*a, **k):
            real_prepare(*a, **k)
            raise KeyboardInterrupt("simulated crash before the adopt transaction")
        with mock.patch.object(supervisor, "prepare_adopt_seen", prepare_then_crash):
            with self.assertRaises(KeyboardInterrupt):
                recovery.recover(self.conn, self.home, "boot_crash")
        self._assert_marker("adopt_pending")
        self.assertIsNone(self.conn.execute("SELECT pgid FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0])
        os.kill(wrapper, signal.SIGKILL)
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "exit.json")))
        self._restart()
        self.assertEqual(self.conn.execute("SELECT pgid FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0],
                         wrapper)
        # Held by recovery itself (identity from wrapper.json, marker honoured), not only
        # by the close-safety gate's backstop (which would say wrapper_gone_group_occupied).
        self.assertEqual((self._hold_row() or {}).get("reason"), "adopt_pending")
        self._assert_held(member, mst)

    def test_r6_close_gate_unit(self):
        """Round 6: close_gate refuses while a recorded identity lives or the group is
        occupied (identity from wrapper.json when the DB has none); _close_run raises and
        writes nothing; the operator override (gate=False) closes."""
        trigger, wrapper, wst, member, mst = self._runwrap_with_member()
        self._rows(wrapper, wst)
        self.conn.execute("UPDATE runs SET wrapper_pid = NULL, pgid = NULL, proc_starttime = NULL "
                          "WHERE id = ?;", (self.run_id,))
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        with self.assertRaises(supervisor.CloseHeld) as cm:
            supervisor.close_gate(run, "exited")
        self.assertEqual(cm.exception.pgid, wrapper)
        self.assertIn(wrapper, cm.exception.live)
        self.assertTrue(cm.exception.occupied)
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        with self.assertRaises(supervisor.CloseHeld) as cm:
            supervisor._close_run(self.conn, run, "exited", "exited", 0, {})
        self.assertEqual(cm.exception.live, {})
        self.assertTrue(cm.exception.occupied)
        self.assertEqual(self._status(), "running")
        self.assertIsNotNone(self._lease())
        supervisor._close_run(self.conn, run, "lost", "lost_on_restart", None, {}, gate=False)
        self.assertEqual(self._status(), "lost")
        self.assertTrue(_alive(member, mst))

    def test_r6_pid_reuse_group_not_ours(self):
        """Round 6 (Quest review Y2): a live process now has pid == pgid but another
        starttime than the wrapper: the group is not the run's; the gate neither holds
        nor counts it as occupied (no false hold on a reused pid)."""
        esc = int(subprocess.run([sys.executable, "-c", _ESCAPEE, shutil.which("sleep")],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                                 check=True).stdout.decode().split()[0])
        est = get_proc_stat(esc)["starttime"]
        self.addCleanup(_kill_same, esc, est)
        self.assertEqual(get_proc_stat(esc)["pgrp"], esc)
        self.assertFalse(supervisor.group_still_ours(esc, est + 7))
        self.assertTrue(supervisor.group_still_ours(esc, est))
        self._rows(esc, est + 7)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        supervisor.close_gate(run, "lost_on_restart")  # no CloseHeld

    def test_r6_marker_of_another_run_gives_no_members(self):
        """Round 6 (Quest review Y9): a marker whose run_id is not the run's is
        unusable (run_id_mismatch) and its prior_members are not used."""
        self._rows(424242, 1)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        with open(os.path.join(self.run_dir, "seen.json"), "w", encoding="utf-8") as f:
            json.dump({"mesh_seen_marker": "unusable", "run_id": "run_other", "reason": "adopt_pending",
                       "prior_members": [{"pid": os.getpid(), "starttime": 5}]}, f)
        state, members, reason, _l = supervisor._load_seen(run)
        self.assertEqual((state, members, reason), ("unusable", {}, "run_id_mismatch"))

    def test_r7_unknown_starttime_fails_closed(self):
        """Round 7 (Quest P3-2): DB identity NULL and wrapper.json without a usable
        starttime (missing, 0, garbage) while the wrapper and its member live: the gate
        cannot prove a pid reuse, so it refuses (group occupied), signalling nothing."""
        trigger, wrapper, wst, member, mst = self._runwrap_with_member()
        self._rows(wrapper, wst)
        self.conn.execute("UPDATE runs SET wrapper_pid = NULL, pgid = NULL, proc_starttime = NULL "
                          "WHERE id = ?;", (self.run_id,))
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        for doc in ({"pid": wrapper}, {"pid": wrapper, "starttime": 0}, {"pid": wrapper, "starttime": "abc"}):
            with open(os.path.join(self.run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
                json.dump(doc, f)
            with self.assertRaises(supervisor.CloseHeld, msg=str(doc)) as cm:
                supervisor.close_gate(run, "exited")
            self.assertTrue(cm.exception.occupied, doc)
            self.assertEqual(cm.exception.live, {}, doc)
        self.assertTrue(supervisor.group_still_ours(wrapper, None))
        self.assertTrue(_alive(wrapper, wst) and _alive(member, mst))

    def test_r8_unknown_starttime_never_trusted_exit_json(self):
        """Round 8 (Linear P1-1): the same with an exit.json present (recovery's exit
        path fills the identity from wrapper.json): the starttime is stored as NULL."""
        self.test_r8_unknown_starttime_never_trusted(exit_json=True)

    def test_r8_unknown_starttime_never_trusted(self, exit_json=False):
        """Round 8 (Linear P1-1, exact repro): DB identity empty, wrapper.json starttime 0
        naming a live unrelated process (own group), deadline passed. Recovery and watch
        ticks must never trust that identity: nothing signalled, no seen members written,
        the run stays open and the lease held."""
        other = subprocess.Popen([shutil.which("setsid"), shutil.which("sleep"), "120"])
        self.addCleanup(lambda: (other.kill(), other.wait()))
        self.assertTrue(_wait(lambda: (get_proc_stat(other.pid) or {}).get("pgrp") == other.pid, 10))
        ost = get_proc_stat(other.pid)["starttime"]
        self._rows(other.pid, ost)
        self.conn.execute("UPDATE runs SET wrapper_pid = NULL, pgid = NULL, proc_starttime = NULL, "
                          "deadline_at = ? WHERE id = ?;", (now_iso_utc(time.time() - 60), self.run_id))
        with open(os.path.join(self.run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": other.pid, "starttime": 0}, f)
        if exit_json:
            with open(os.path.join(self.run_dir, "exit.json"), "w", encoding="utf-8") as f:
                json.dump({"exit_code": 0, "signal": None}, f)
        sent = []
        real_kill, real_killpg = os.kill, os.killpg

        def kill(pid, sig):
            if pid == other.pid and sig != 0:
                sent.append(("kill", sig))
            return real_kill(pid, sig)

        def killpg(pgid, sig):
            if pgid == other.pid and sig != 0:
                sent.append(("killpg", sig))
            return real_killpg(pgid, sig)
        from unittest import mock
        with mock.patch("os.kill", kill), mock.patch("os.killpg", killpg), \
                mock.patch.object(supervisor, "_log_meshd"):
            recovery.recover(self.conn, self.home, "boot_a")
            for _ in range(15):
                supervisor.watch_launch_runs(self.conn, self.home)
                supervisor.advance_terminations(self.conn, self.home)
                time.sleep(0.05)
        self.assertEqual(sent, [])
        self.assertTrue(_alive(other.pid, ost), "unrelated process killed")
        self.assertIn(self._status(), ("starting", "running"))
        self.assertIsNotNone(self._lease())
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertIsNone(run["proc_starttime"], "unknown starttime stored as known")
        self.assertFalse(supervisor.wrapper_alive(run))
        seen_path = os.path.join(self.run_dir, "seen.json")
        if os.path.exists(seen_path):
            with open(seen_path, "r", encoding="utf-8") as f:
                self.assertFalse(json.load(f).get("members"), "seen written from an untrusted identity")

    def test_r7_zombie_identity_not_live(self):
        """Round 7 (Quest P3-3, M6): an exited, unreaped (zombie) recorded identity is
        not live: the gate does not wait on it."""
        proc = subprocess.Popen([shutil.which("sleep"), "30"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        st = get_proc_stat(proc.pid)["starttime"]
        self.assertTrue(supervisor._identity_live(proc.pid, st))
        os.kill(proc.pid, signal.SIGKILL)
        self.assertTrue(_wait(lambda: get_proc_stat(proc.pid)["state"] == "Z", 10))
        self.assertFalse(supervisor._identity_live(proc.pid, st))

    def test_r7_refused_close_logged_once_per_state(self):
        """Round 7 (Quest P3-1): repeated refusals in the same state log one line."""
        trigger, wrapper, wst, member, mst = self._runwrap_with_member()
        self._rows(wrapper, wst)
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()

        def lines():
            n = 0
            for name in os.listdir(os.path.join(self.home, "logs")):
                with open(os.path.join(self.home, "logs", name), "r", encoding="utf-8", errors="replace") as f:
                    n += f.read().count(f"run {self.run_id}: close refused")
            return n
        for _ in range(20):
            with self.assertRaises(supervisor.CloseHeld) as cm:
                supervisor._close_run(self.conn, run, "exited", "exited", 0, {})
            supervisor.hold_close(self.conn, self.home, run, cm.exception)
        self.assertEqual(lines(), 1)
        os.kill(member, signal.SIGKILL)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))
        supervisor._SEEN[self.run_id] = {member: mst}
        self.assertTrue(_wait(lambda: not supervisor._scan_group(wrapper), 10))

    def test_r7_recovery_catches_close_held(self):
        """Round 7 (Quest P3-3, M7): any CloseHeld raised by a recovery path is turned
        into a hold by the per-run loop and counted as terminating (never lost/error)."""
        from unittest import mock
        trigger, wrapper, wst, member, mst = self._runwrap_with_member()
        self._rows(wrapper, wst)
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))

        def refusing(conn, home, row, boot_id):
            supervisor.close_gate(conn.execute("SELECT * FROM runs WHERE id = ?;", (row["id"],)).fetchone(),
                                  "lost_on_restart")
            raise AssertionError("gate passed with the member alive")
        with mock.patch.object(recovery, "_recover_run", refusing):
            summary = recovery.recover(self.conn, self.home, "boot_m7")
        self.assertEqual((summary["runs_terminating"], summary["runs_lost"], summary["run_errors"]), (1, 0, 0),
                         summary)
        self.assertEqual((self._hold_row() or {}).get("reason"), "wrapper_gone_group_occupied")
        self._assert_held(member, mst)

    def _merge_rows(self):
        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_m','p','g','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
           "attempt, merge_json, created_by, created_at, updated_at) "
           "VALUES ('tsk_m','prj_m','t','s','code','merging','edit',600,1,1,?,'usr',?,?);",
           (json.dumps({"run_id": self.run_id}), now, now))
        ex("INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, boot_id, run_dir, "
           "started_at) VALUES (?,'merge_test','tsk_m','prj_m',1,1,'none','running','boot_old',?,?);",
           (self.run_id, self.run_dir, now))
        key = merge.merge_lease_key("prj_m")
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
           "VALUES (?,?,'meshd','boot_old',1,?,?,?);", (key, self.run_id, now, now_iso_utc(time.time() + 3600), now))
        return key

    def test_r7_merge_recovery_holds_occupied_group(self):
        """Round 7 (Linear P1): a merge_test run; real runwrap; the test command leaves an
        env-less same-group member and exits 0 (exit.json), the wrapper is reaped; the DB
        has no identity yet. A full recover() must not close the run as lost and release
        merge:<prj>: the merge close gate holds both (identity from wrapper.json) while the
        member lives; once it is gone the next boot closes the run."""
        trigger, wrapper, wst, member, mst = self._runwrap_with_member()
        key = self._merge_rows()
        ex = self.conn.execute
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "exit.json")))
        lease = lambda: ex("SELECT 1 FROM leases WHERE key = ? AND run_id = ?;", (key, self.run_id)).fetchone()
        for boot in ("boot_a", "boot_b"):  # held again on a second restart
            self._forget()
            summary = recovery.recover(self.conn, self.home, boot)
            self.assertEqual((summary["merge_runs_held"], summary["merge_runs_lost"],
                              summary["merges_interrupted"]), (1, 0, 0), summary)
            self.assertEqual(self._status(), "running")
            self.assertIsNotNone(lease(), "merge lease released while the member lives")
            self.assertTrue(_alive(member, mst), "member signalled by pgid alone")
        self.assertEqual(ex("SELECT pgid FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0], wrapper)
        self.assertEqual(ex("SELECT status FROM tasks WHERE id = 'tsk_m';").fetchone()[0], "merging")
        self.assertEqual(ex("SELECT COUNT(*) FROM events WHERE type = 'merge.recovery_blocked' "
                            "AND run_id = ?;", (self.run_id,)).fetchone()[0], 1)
        os.kill(member, signal.SIGKILL)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))
        self._forget()
        summary = recovery.recover(self.conn, self.home, "boot_c")
        self.assertEqual((summary["merge_runs_held"], summary["merge_runs_lost"]), (0, 1), summary)
        self.assertEqual(self._status(), "lost")
        self.assertIsNone(lease())

    def test_r7_merge_gate_pid_reuse_closes(self):
        """Round 7: wrapper.json names a pid that is now another process's group leader
        (pid == pgid, other starttime): the group is not the run's, so the merge run is
        closed lost (no false hold, nothing signalled)."""
        esc = int(subprocess.run([sys.executable, "-c", _ESCAPEE, shutil.which("sleep")],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                                 check=True).stdout.decode().split()[0])
        est = get_proc_stat(esc)["starttime"]
        self.addCleanup(_kill_same, esc, est)
        key = self._merge_rows()
        with open(os.path.join(self.run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": esc, "starttime": est + 7}, f)
        summary = recovery.recover(self.conn, self.home, "boot_a")
        self.assertEqual((summary["merge_runs_held"], summary["merge_runs_lost"]), (0, 1), summary)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (key,)).fetchone())
        self.assertTrue(_alive(esc, est))

    def test_r7_merge_kill_failed_keeps_recorded_deviation(self):
        """Round 7: a recovery termination that ended kill_failed keeps the recorded §11.4
        deviation (survivors reported in run.kill_failed, lease released) instead of the
        gate's hold."""
        trigger, wrapper, wst, member, mst = self._runwrap_with_member()
        key = self._merge_rows()
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        result = {"reason": "interrupted", "recovered": True,
                  "termination": {"kill_failed": True, "pids": [member]}}
        self.assertEqual(merge._close_recovered_merge_run(self.conn, run, wrapper, wst, result), "killed")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (key,)).fetchone())
        ev = self.conn.execute("SELECT payload_json FROM events WHERE type = 'run.kill_failed' "
                               "AND run_id = ?;", (self.run_id,)).fetchall()
        self.assertEqual([json.loads(e[0])["pids"] for e in ev], [[member]])
        with self.assertRaises(merge.MergeRunHeld):  # the same run without kill_failed: held
            self.conn.execute("UPDATE runs SET status = 'running' WHERE id = ?;", (self.run_id,))
            merge._close_recovered_merge_run(self.conn, run, wrapper, wst, {"termination": {}})

    def test_r7_gate_cleanup_keeps_exit_outcome(self):
        """Round 7 (Linear P2): the gate refuses an exit-0 close because a recorded member
        lives; its cleanup terminates the member as leftovers (H17) and the run then
        closes exited 0 with the task auto-submitted, never killed / task failed."""
        trigger, wrapper, wst, member, mst = self._runwrap_with_member()
        self._rows(wrapper, wst)
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                          "VALUES ('kill.grace_s', '1', ?, 'test');", (now_iso_utc(),))
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        with open(os.path.join(self.run_dir, "exit.json"), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["exit_code"], 0)
        supervisor._SEEN[self.run_id] = {wrapper: wst, member: mst}
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        with self.assertRaises(supervisor.CloseHeld) as cm:
            supervisor._close_run(self.conn, run, "exited", "exited", 0, {})
        self.assertIn(member, cm.exception.live)
        supervisor.hold_close(self.conn, self.home, run, cm.exception)

        def tick():
            r = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
            if r["status"] in ("starting", "running"):
                if _alive(member, mst):
                    self.assertIsNotNone(self._lease())
                supervisor._watch_one(self.conn, self.home, r)
                return False
            return True
        self.assertTrue(_wait(tick, 30, 0.05), "run never closed")
        self.assertFalse(_alive(member, mst), "member survived")
        r = self.conn.execute("SELECT status, exit_code, end_reason FROM runs WHERE id = ?;",
                              (self.run_id,)).fetchone()
        self.assertEqual(tuple(r), ("exited", 0, "exited"))
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = ?;",
                                           (self.task_id,)).fetchone()[0], "submitted")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'run.leftovers_terminated' "
                                           "AND run_id = ?;", (self.run_id,)).fetchone()[0], 1)
        self.assertIsNone(self._lease())

    def test_r4_absent_seen_file_never_lifts_hold(self):
        """Round 4 P3: deleting seen.json (absent) never lifts an adoption hold, live
        or across a restart."""
        member, mst = self._uncertain_adopt_with_old_file()
        os.unlink(os.path.join(self.run_dir, "seen.json"))
        self._assert_held(member, mst, 8)
        self._restart()
        self.assertEqual((self._hold_row() or {}).get("reason"), "adopt_scan_untrusted")
        self._assert_held(member, mst)

    def _assert_held(self, member, mst, ticks=12):
        """Round 5: counts real watch ticks (the seen scans are tick-based) instead of a
        fixed wall-clock window."""
        for _ in range(ticks):
            run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
            supervisor._watch_one(self.conn, self.home, run)
            self.assertTrue(_alive(member, mst), "member signalled")
            self.assertIn(self._status(), ("starting", "running"), "run closed while the member lives")
            self.assertIsNotNone(self._lease(), "lease released while the member lives")
            self.assertIsNotNone(self.conn.execute("SELECT 1 FROM meta WHERE key = ?;",
                                                   (f"seen_hold:{self.run_id}",)).fetchone(),
                                 "persisted hold lifted")
            time.sleep(0.02)

    def test_old_valid_seen_file_does_not_lift_untrusted_hold(self):
        """ACC: N18, A8 (gate fix round 3, Linear): the unchanged old valid file is not a
        repair; the hold stays over real watch ticks."""
        member, mst = self._uncertain_adopt_with_old_file()
        self._assert_held(member, mst)

    def test_untrusted_hold_survives_immediate_restart(self):
        """ACC: N18, A8 (gate fix round 3): hold, then an immediate restart (memory
        cleared, persisted holds reloaded): still held."""
        member, mst = self._uncertain_adopt_with_old_file()
        self._forget()
        supervisor.refresh_persisted_holds(self.conn, self.home)
        self._assert_held(member, mst)
        os.kill(member, signal.SIGKILL)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))

    def test_untrusted_hold_full_recover_counts_and_second_restart(self):
        """ACC: N18, A8 (gate fix round 3, Quest review): restart level. recover()
        records runs_adopted=1 and runs_adopted_uncertain=1 in daemon.recovered; a second
        full recover() (another restart, memory cleared) keeps the run held."""
        member, mst = self._uncertain_adopt_with_old_file(via_recover=True)
        ev = self.conn.execute("SELECT payload_json FROM events WHERE type = 'daemon.recovered' "
                               "ORDER BY rowid;").fetchall()
        self.assertEqual(len(ev), 1)
        p = json.loads(ev[0][0])
        self.assertEqual((p["runs_adopted"], p["runs_adopted_uncertain"], p["runs_lost"]), (1, 1, 0))
        self._forget()
        recovery.recover(self.conn, self.home, "boot_new2")
        p2 = json.loads(self.conn.execute("SELECT payload_json FROM events WHERE type = 'daemon.recovered' "
                                          "ORDER BY rowid DESC LIMIT 1;").fetchone()[0])
        self.assertEqual(p2["runs_lost"], 0, p2)
        self.assertTrue(_alive(member, mst))
        self.assertIn(self._status(), ("starting", "running"))
        self.assertIsNotNone(self._lease())
        self._assert_held(member, mst)
        os.kill(member, signal.SIGKILL)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))

class TestSpawnFailsAfterPopen(unittest.TestCase):
    """Round 7 (Linear P2): Popen succeeded, then registration (or the TX B identity
    read) fails with EIO: the wrapper and its group are killed through the unreaped
    Popen handle before the run closes spawn_failed and the lease is released."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-gf-spawn-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "logs"))
        os.makedirs(os.path.join(self.home, "run"))
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)
        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_s','agent','s',?);", (now,))
        ex("INSERT INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
           "VALUES ('agt_s','fake','launch',1,?,?);", (now, now))
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_s','p','g','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
           "timeout_s, max_attempts, created_by, created_at, updated_at) "
           "VALUES ('tsk_s','prj_s','t','s','general','queued','agt_s','edit',600,1,'usr',?,?);", (now, now))
        self.groups = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for g in self.groups:
            if g <= 2 or g == os.getpgrp():
                continue  # never anything outside the test's own spawns
            for p, st in supervisor._scan_group(g).items():
                _kill_same(p, st)
        for rid, a in list(supervisor._SPAWN_ABORTS.items()):
            supervisor._SPAWN_ABORTS.pop(rid, None)
            try:
                a.proc.kill()
                a.proc.wait(5)
            except Exception:
                pass
        for rid in list(supervisor._WRAPPERS):
            supervisor._WRAPPERS.pop(rid, None)

    def _start(self, patch, cmd=None):
        from unittest import mock
        from mesh.adapters.base import CommandSpec
        from mesh.adapters.registry import get_adapter as real_get
        member_cmd = cmd or (f"env -i {sys.executable} -c 'import time; time.sleep(120)' >/dev/null 2>&1 & "
                             f"{shutil.which('sleep')} 120")

        def get_adapter(adapter_id, testing_mode=False, options=None):
            a = real_get(adapter_id, testing_mode=testing_mode, options=options)
            a.build = lambda req: CommandSpec(argv=["sh", "-c", member_cmd], cwd=req.workspace)
            return a
        task = self.conn.execute("SELECT * FROM tasks WHERE id = 'tsk_s';").fetchone()
        agent = self.conn.execute("SELECT * FROM agents WHERE id = 'agt_s';").fetchone()
        with mock.patch.object(supervisor, "get_adapter", get_adapter), patch:
            out = supervisor.start_launch_run(self.conn, self.home, task, agent, testing_mode=True)
        return out

    def _wait_member(self, pid):
        """The scheduling hook: fail only once the command's env-less member exists."""
        self.groups.append(pid)
        assert _wait(lambda: len(supervisor._scan_group(pid)) >= 3, 15), "member not started"

    def _assert_aborted(self):
        run = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_s';").fetchone()
        self.assertEqual((run["status"], run["end_reason"]), ("spawn_failed", "spawn_failed"))
        res = json.loads(run["result_json"])
        pid = res["after_popen"]["pid"]
        self.assertEqual(run["pgid"], pid)
        self.assertNotIn("survivors", res["after_popen"])
        self.assertNotIn("escapees", res["after_popen"])
        self.assertIsNone(get_proc_stat(pid), "wrapper alive or left a zombie")
        self.assertEqual(supervisor._scan_group(pid), {}, "group members survived")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (run["id"],)).fetchone())
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_s';").fetchone()[0], "failed")
        self.assertNotIn(pid, children.registered())
        self.assertNotIn(run["id"], supervisor._WRAPPERS)

    def test_r8_setsid_escapee_killed_and_recorded(self):
        """Round 8 (Linear P1-2): a setsid child that keeps MESH_RUN_ID (inherited env)
        when registration fails after Popen is found by the run-id scan, killed by pid +
        starttime and recorded; the run then closes spawn_failed."""
        import errno
        from unittest import mock
        pidfile = os.path.join(self.tmp, "esc.pid")
        cmd = (f"{sys.executable} -c 'import os, time; os.setsid(); time.sleep(120)' >/dev/null 2>&1 & "
               f"echo $! > {pidfile}; {shutil.which('sleep')} 120")
        esc = {}

        def eio(pid):
            self.groups.append(pid)
            assert _wait(lambda: os.path.exists(pidfile) and open(pidfile).read().strip(), 15)
            p = int(open(pidfile).read().strip())
            assert _wait(lambda: (get_proc_stat(p) or {}).get("session") == p, 15), "escapee not in own session"
            esc.update(pid=p, st=get_proc_stat(p)["starttime"])
            self.addCleanup(_kill_same, p, esc["st"])
            raise OSError(errno.EIO, "injected EIO", f"/proc/{pid}/stat")
        self.assertIsNone(self._start(mock.patch.object(children, "get_proc_stat", eio), cmd))
        run = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_s';").fetchone()
        res = json.loads(run["result_json"])
        self.assertEqual(res["after_popen"].get("escapees"), [esc["pid"]])
        self.assertTrue(_wait(lambda: not _alive(esc["pid"], esc["st"]), 5), "escapee survived")
        self.assertEqual(run["status"], "spawn_failed")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (run["id"],)).fetchone())

    def test_r8_survivors_keep_lease_and_retry(self):
        """Round 8 (Linear P1-3): killpg fails with EPERM, so the env-less member
        survives the abort: the run stays open, the lease is kept, the wrapper stays
        unreaped (pgid reserved), run.kill_failed says retrying. Once signals work again
        a watch pass kills the group, reaps the wrapper and closes spawn_failed."""
        import errno
        from unittest import mock
        real_killpg = os.killpg
        unpatched_kill = os.kill
        g = {}

        real_stat = children.get_proc_stat

        def eio(pid):
            if g.get("pid"):  # only the registration read fails (reap_orphans reads too)
                return real_stat(pid)
            self._wait_member(pid)
            g["pid"] = pid
            unpatched_kill(pid, signal.SIGKILL)  # the leader dies; its group survives (EPERM below)
            assert _wait(lambda: (get_proc_stat(pid) or {}).get("state") == "Z", 10)
            raise OSError(errno.EIO, "injected EIO", f"/proc/{pid}/stat")

        def killpg(pgid, sig):
            if pgid == g.get("pid") and sig != 0:
                raise PermissionError(errno.EPERM, "injected EPERM")
            return real_killpg(pgid, sig)
        real_kill = os.kill

        def kill(p, sig):
            if sig != 0 and g.get("pid") and (get_proc_stat(p) or {}).get("pgrp") == g["pid"]:
                raise PermissionError(errno.EPERM, "injected EPERM")
            return real_kill(p, sig)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("os.kill", kill))
        stack.enter_context(mock.patch.object(children, "get_proc_stat", eio))
        stack.enter_context(mock.patch("os.killpg", killpg))
        stack.enter_context(mock.patch.object(supervisor, "SPAWN_ABORT_WAIT_S", 0.5))
        with stack:
            self.assertIsNone(self._start(contextlib.nullcontext()))
            pid = g["pid"]
            run = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_s';").fetchone()
            self.assertIn(run["status"], ("starting", "running"))
            self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (run["id"],)).fetchone())
            self.assertEqual(run["pgid"], pid)
            self.assertIn(run["id"], supervisor._SPAWN_ABORTS)
            self.assertEqual((get_proc_stat(pid) or {}).get("state"), "Z", "wrapper reaped while survivors exist")
            children.reap_orphans()  # round 9: the orphan reaper must not reap it either
            self.assertEqual((get_proc_stat(pid) or {}).get("state"), "Z", "orphan reaper reaped the held wrapper")
            self.assertTrue(supervisor._scan_group(pid), "group emptied despite EPERM")
            ev = self.conn.execute("SELECT payload_json FROM events WHERE type = 'run.kill_failed' "
                                   "AND run_id = ?;", (run["id"],)).fetchall()
            self.assertEqual(len(ev), 1)
            payload = json.loads(ev[0][0])
            self.assertEqual((payload["lease_released"], payload["retrying"]), (False, True))
            for _ in range(3):  # retries while EPERM persists: still held
                with mock.patch.object(supervisor, "_log_meshd"):
                    supervisor.watch_launch_runs(self.conn, self.home)
                self.assertIn(self._run_status(), ("starting", "running"))
                self.assertEqual((get_proc_stat(pid) or {}).get("state"), "Z")
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(_wait(lambda: (supervisor.watch_launch_runs(self.conn, self.home),
                                           self._run_status() == "spawn_failed")[1], 10))
        self.assertNotIn(run["id"], supervisor._SPAWN_ABORTS)
        res = json.loads(self.conn.execute("SELECT result_json FROM runs WHERE id = ?;", (run["id"],)).fetchone()[0])
        self.assertGreaterEqual(res["after_popen"]["retries"], 1)
        self.assertTrue(res["after_popen"]["survivors_at_abort"])
        self.assertIsNone(get_proc_stat(pid), "wrapper not reaped")
        self.assertEqual(supervisor._scan_group(pid), {})
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (run["id"],)).fetchone())
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_s';").fetchone()[0], "failed")
        self.assertNotIn(pid, children.registered())

    def _exec_escapee_setup(self):
        """An inherited-MESH_RUN_ID setsid escapee that, on a trigger, execs `sleep`
        with a cleared env (same pid and starttime); EPERM on every kill aimed at the
        wrapper's group or the escapee until ``g["block"]`` is cleared."""
        import errno
        from unittest import mock
        script = os.path.join(self.tmp, "esc.py")
        pidfile = os.path.join(self.tmp, "esc.pid")
        trig = os.path.join(self.tmp, "esc.go")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import os, time\nos.setsid()\n"
                    f"open({pidfile!r} + '.tmp', 'w').write(str(os.getpid()))\n"
                    f"os.rename({pidfile!r} + '.tmp', {pidfile!r})\n"
                    f"while not os.path.exists({trig!r}):\n    time.sleep(0.02)\n"
                    f"os.execve({shutil.which('sleep')!r}, ['sleep', '120'], {{}})\n")
        cmd = f"{sys.executable} {script} >/dev/null 2>&1 & {shutil.which('sleep')} 120"
        g = {"block": True}
        real_kill, real_killpg = os.kill, os.killpg

        def blocked(p):
            return g["block"] and (p == g.get("esc") or (get_proc_stat(p) or {}).get("pgrp") == g.get("pid"))

        def kill(p, sig):
            if sig != 0 and blocked(p):
                raise PermissionError(errno.EPERM, "injected EPERM")
            return real_kill(p, sig)

        def killpg(pgid, sig):
            if sig != 0 and g["block"] and pgid == g.get("pid"):
                raise PermissionError(errno.EPERM, "injected EPERM")
            return real_killpg(pgid, sig)

        def eio(pid):
            self.groups.append(pid)
            g["pid"] = pid
            assert _wait(lambda: os.path.exists(pidfile), 15)
            g["esc"] = int(open(pidfile).read())
            g["est"] = get_proc_stat(g["esc"])["starttime"]
            self.addCleanup(_kill_same, g["esc"], g["est"])
            raise OSError(errno.EIO, "injected EIO", f"/proc/{pid}/stat")
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("os.kill", kill))
        stack.enter_context(mock.patch("os.killpg", killpg))
        stack.enter_context(mock.patch.object(supervisor, "SPAWN_ABORT_WAIT_S", 0.5))
        with stack:
            with mock.patch.object(children, "get_proc_stat", eio):
                self.assertIsNone(self._start(contextlib.nullcontext(), cmd))
            run = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_s';").fetchone()
            self.assertIn(run["id"], supervisor._SPAWN_ABORTS)
            self.assertIn(g["esc"], supervisor._SPAWN_ABORTS[run["id"]].known)
            open(trig, "w").close()

            def env_cleared():
                try:
                    with open(f"/proc/{g['esc']}/environ", "rb") as f:
                        return b"MESH_RUN_ID" not in f.read() and _alive(g["esc"], g["est"])
                except OSError:
                    return False
            self.assertTrue(_wait(env_cleared, 10), "escapee did not exec with a cleared env")
            with open(os.path.join(run["run_dir"], "seen.json"), "r", encoding="utf-8") as f:
                doc = json.load(f)
            self.assertIn({"pid": g["esc"], "starttime": g["est"]}, doc["prior_members"])
        g["block"] = False
        return run, g

    def test_r9_authenticated_escapee_retried_by_identity(self):
        """Round 9 (Linear P1-a, repro): the escapee survives the first kill (EPERM),
        then execs with a cleared env (same pid/starttime). The retry kills it by pid +
        starttime before the run closes spawn_failed."""
        from unittest import mock
        run, g = self._exec_escapee_setup()
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(_wait(lambda: (supervisor.watch_launch_runs(self.conn, self.home),
                                           self._run_status() == "spawn_failed")[1], 10))
        self.assertFalse(_alive(g["esc"], g["est"]), "run closed with the escapee alive")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (run["id"],)).fetchone())

    def test_r9_authenticated_escapee_after_restart(self):
        """Round 9 (Linear P1-a, restart): the same, but meshd "restarts" while the abort
        is pending (memory lost, wrapper reaped as by init): boot recovery and the close
        gate target the persisted escapee; the run never closes with it alive."""
        from unittest import mock
        run, g = self._exec_escapee_setup()
        ab = supervisor._SPAWN_ABORTS.pop(run["id"])
        children.unhold(ab.proc.pid)
        ab.proc.kill()
        ab.proc.wait()
        for p, st in supervisor._scan_group(g["pid"]).items():
            _kill_same(p, st)
        supervisor._SEEN.pop(run["id"], None)
        terminate.forget(run["id"])
        with mock.patch.object(supervisor, "_log_meshd"):
            recovery.recover(self.conn, self.home, "boot_b")
            for _ in range(40):
                supervisor.watch_launch_runs(self.conn, self.home)
                supervisor.advance_terminations(self.conn, self.home)
                if not _alive(g["esc"], g["est"]):
                    break
                self.assertIn(self._run_status(), ("starting", "running"), "run closed with the escapee alive")
                time.sleep(0.05)
        self.assertFalse(_alive(g["esc"], g["est"]), "escapee never terminated after restart")

    def test_r9_abort_error_keeps_pending_abort(self):
        """Round 9 (Linear P1-c): an OSError inside the abort (identity recheck) does not
        leave an untracked run: the pending abort is kept with the wrapper identity and
        the lease; a later watch pass finishes it."""
        import errno
        from unittest import mock
        real = supervisor._identity_live
        state = {"n": 0}

        def flaky(p, st):
            state["n"] += 1
            if state["n"] <= 3:
                raise OSError(errno.EIO, "injected EIO")
            return real(p, st)

        def eio(pid):
            self._wait_member(pid)
            raise OSError(errno.EIO, "injected EIO", f"/proc/{pid}/stat")
        with mock.patch.object(supervisor, "_abort_spawned", side_effect=OSError(errno.EIO, "boom")):
            self.assertIsNone(self._start(mock.patch.object(children, "get_proc_stat", eio)))
        run = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_s';").fetchone()
        self.assertIn(run["id"], supervisor._SPAWN_ABORTS)
        self.assertIsNotNone(run["wrapper_pid"])
        self.assertEqual(run["pgid"], run["wrapper_pid"])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (run["id"],)).fetchone())
        with mock.patch.object(supervisor, "_log_meshd"), mock.patch.object(supervisor, "_identity_live", flaky):
            self.assertTrue(_wait(lambda: (supervisor.watch_launch_runs(self.conn, self.home),
                                           self._run_status() == "spawn_failed")[1], 10))
        self.assertEqual(supervisor._scan_group(run["wrapper_pid"]), {})
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (run["id"],)).fetchone())

    def _setsid_member_setup(self, crash=False, flaky=False):
        """Round 10: an env-less same-group member (not a MESH_RUN_ID carrier) that, on a
        trigger, calls setsid() (leaves the wrapper's group). Every kill aimed at the
        group or the member is refused (EPERM) until ``g["block"]`` is cleared.
        crash: meshd "dies" right after the first abort pass (kill -9 model)."""
        import errno
        from unittest import mock
        script = os.path.join(self.tmp, "mem.py")
        pidfile = os.path.join(self.tmp, "mem.pid")
        trig = os.path.join(self.tmp, "mem.go")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import os, time\n"
                    f"open({pidfile!r} + '.tmp', 'w').write(str(os.getpid()))\n"
                    f"os.rename({pidfile!r} + '.tmp', {pidfile!r})\n"
                    f"while not os.path.exists({trig!r}):\n    time.sleep(0.02)\n"
                    "os.setsid()\ntime.sleep(120)\n")
        # The env-less intermediate sh exits at once: the member is reparented away, so
        # neither its env nor its PPid chain links it to the run; only the group scan
        # authenticates it.
        cmd = (f"env -i {shutil.which('sh')} -c '{sys.executable} {script} >/dev/null 2>&1 &'; "
               f"{shutil.which('sleep')} 120")
        g = {"block": True}
        real_kill, real_killpg = os.kill, os.killpg
        real_stat = children.get_proc_stat

        def blocked(p):
            return g["block"] and (p == g.get("mem") or (get_proc_stat(p) or {}).get("pgrp") == g.get("pid"))

        def kill(p, sig):
            if sig != 0 and p > 2 and blocked(p):
                raise PermissionError(errno.EPERM, "injected EPERM")
            return real_kill(p, sig)

        def killpg(pgid, sig):
            if sig != 0 and g["block"] and pgid == g.get("pid"):
                raise PermissionError(errno.EPERM, "injected EPERM")
            return real_killpg(pgid, sig)

        def eio(pid):
            if g.get("pid"):
                return real_stat(pid)
            self.groups.append(pid)
            g["pid"] = pid
            assert _wait(lambda: os.path.exists(pidfile), 15)
            g["mem"] = int(open(pidfile).read())
            g["mst"] = get_proc_stat(g["mem"])["starttime"]
            self.addCleanup(_kill_same, g["mem"], g["mst"])
            raise OSError(errno.EIO, "injected EIO", f"/proc/{pid}/stat")

        class _Crash(BaseException):
            pass
        real_abort = supervisor._abort_spawned

        def abort_then_crash(ab):
            g["ab"] = ab
            real_abort(ab)
            raise _Crash()
        real_live = supervisor._identity_live
        flaky_n = {"n": 0}

        def flaky_live(p, st):
            if flaky and not g["block"] and p == g.get("mem") and flaky_n["n"] < 2 \
                    and (real_stat(g["pid"]) or {}).get("state") == "Z":
                flaky_n["n"] += 1
                raise OSError(errno.EIO, "injected EIO")
            return real_live(p, st)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("os.kill", kill))
        stack.enter_context(mock.patch("os.killpg", killpg))
        stack.enter_context(mock.patch.object(supervisor, "SPAWN_ABORT_WAIT_S", 0.5))
        stack.enter_context(mock.patch.object(supervisor, "_identity_live", flaky_live))
        if crash:
            stack.enter_context(mock.patch.object(supervisor, "_abort_spawned", abort_then_crash))
        with stack:
            with mock.patch.object(children, "get_proc_stat", eio):
                if crash:
                    with self.assertRaises(_Crash):
                        self._start(contextlib.nullcontext(), cmd)
                else:
                    self.assertIsNone(self._start(contextlib.nullcontext(), cmd))
            run = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_s';").fetchone()
            self.assertEqual(run["pgid"], g["pid"], "identity not in the DB")
            with open(os.path.join(run["run_dir"], "seen.json"), "r", encoding="utf-8") as f:
                doc = json.load(f)
            self.assertIn({"pid": g["mem"], "starttime": g["mst"]}, doc["prior_members"])
            open(trig, "w").close()
            self.assertTrue(_wait(lambda: (get_proc_stat(g["mem"]) or {}).get("session") == g["mem"], 10),
                            "member did not leave the group")
        g["block"] = False
        return run, g

    def _watch_until_closed_member_dead(self, run, g):
        from unittest import mock
        with mock.patch.object(supervisor, "_log_meshd"):
            for _ in range(200):
                supervisor.watch_launch_runs(self.conn, self.home)
                supervisor.advance_terminations(self.conn, self.home)
                st = self._run_status()
                if st not in ("starting", "running"):
                    self.assertFalse(_alive(g["mem"], g["mst"]), f"run closed {st} with the member alive")
                    break
                if not _alive(g["mem"], g["mst"]):
                    pass
                time.sleep(0.05)
        self.assertFalse(_alive(g["mem"], g["mst"]), "member never terminated")

    def test_r10_group_member_leaves_group_after_refused_kill(self):
        """Round 10 (Linear 1, repro): same-group env={} survivor, group SIGKILL refused,
        member then setsid()s: it stays a target by pid + starttime; the run closes
        spawn_failed only after it is dead."""
        run, g = self._setsid_member_setup()
        self.assertIn(g["mem"], supervisor._SPAWN_ABORTS[run["id"]].known)
        self._watch_until_closed_member_dead(run, g)
        self.assertEqual(self._run_status(), "spawn_failed")

    def test_r10_recheck_error_keeps_identity(self):
        """Round 10 (Linear 2): EIO on the identity recheck does not drop the member."""
        import errno
        from unittest import mock
        run, g = self._setsid_member_setup(flaky=True)
        self.assertIn(g["mem"], supervisor._SPAWN_ABORTS[run["id"]].known)
        # While the watch retries: the member's kill is refused and its identity recheck
        # fails (EIO) once the wrapper is dead, i.e. exactly when "uncertain = gone"
        # would let the run close.
        real_live, real_kill = supervisor._identity_live, os.kill
        n = {"eio": 0}

        def leader_dead():
            return (get_proc_stat(g["pid"]) or {}).get("state") == "Z"

        def live(p, st):
            if p == g["mem"] and n["eio"] < 3 and leader_dead():
                n["eio"] += 1
                raise OSError(errno.EIO, "injected EIO")
            return real_live(p, st)

        def kill(p, sig):
            if sig != 0 and p == g["mem"] and n["eio"] < 3:
                raise PermissionError(errno.EPERM, "injected EPERM")
            return real_kill(p, sig)
        with mock.patch.object(supervisor, "_identity_live", live), mock.patch("os.kill", kill):
            self._watch_until_closed_member_dead(run, g)
        self.assertGreaterEqual(n["eio"], 1)

    def test_r11_linear_eacces_on_final_recheck_is_unknown(self):
        """Round 11 (Linear r10 P1-1, exact repro): authenticated member, kills refused
        (EPERM), and one EACCES from get_proc_stat on the member's final recheck once the
        wrapper is dead. EACCES is "unknown", never "gone": the run stays open with its
        lease and closes spawn_failed only after the member is dead."""
        import errno
        from unittest import mock
        run, g = self._setsid_member_setup()
        real_stat, real_kill = supervisor.get_proc_stat, os.kill
        n = {"eacces": 0}

        def leader_dead():
            return (real_stat(g["pid"]) or {}).get("state") == "Z"

        def stat(pid):
            if pid == g["mem"] and n["eacces"] < 1 and leader_dead():
                n["eacces"] += 1
                raise PermissionError(errno.EACCES, "injected EACCES", f"/proc/{pid}/stat")
            return real_stat(pid)

        def kill(p, sig):
            if sig != 0 and p == g["mem"] and n["eacces"] < 1:
                raise PermissionError(errno.EPERM, "injected EPERM")
            return real_kill(p, sig)
        with mock.patch.object(supervisor, "get_proc_stat", stat), mock.patch("os.kill", kill):
            self._watch_until_closed_member_dead(run, g)
        self.assertEqual(n["eacces"], 1)
        self.assertEqual(self._run_status(), "spawn_failed")

    def test_r10_crash_after_first_pass_then_recovery(self):
        """Round 10 (Linear 3, repro): meshd dies right after the first abort pass (no
        pending abort in memory, no marker fault). Full recovery: the persisted
        identities are targeted; the run never closes with the member alive."""
        from unittest import mock
        run, g = self._setsid_member_setup(crash=True)
        ab = g["ab"]
        children.unhold(ab.proc.pid)
        ab.proc.kill()
        ab.proc.wait()  # reaped as by init after meshd's death
        children.unregister_if(ab.proc.pid, "wrapper")
        supervisor._WRAPPERS.pop(run["id"], None)
        self.assertNotIn(run["id"], supervisor._SPAWN_ABORTS)
        supervisor._SEEN.pop(run["id"], None)
        terminate.forget(run["id"])
        with mock.patch.object(supervisor, "_log_meshd"):
            recovery.recover(self.conn, self.home, "boot_b")
        self._watch_until_closed_member_dead(run, g)

    def _run_status(self):
        return self.conn.execute("SELECT status FROM runs WHERE task_id = 'tsk_s';").fetchone()[0]

    def test_r7_registration_eio_kills_spawned_wrapper(self):
        import errno
        from unittest import mock
        real = children.get_proc_stat

        def eio(pid):
            self._wait_member(pid)
            raise OSError(errno.EIO, "injected EIO", f"/proc/{pid}/stat")
        self.assertIsNone(self._start(mock.patch.object(children, "get_proc_stat", eio)))
        self.assertIs(children.get_proc_stat, real)
        self._assert_aborted()

    def test_r7_tx_b_identity_eio_kills_spawned_wrapper(self):
        import errno
        from unittest import mock
        real = supervisor.get_proc_stat
        state = {"done": False}

        def eio_once(pid):
            wrappers = {p.pid for p in supervisor._WRAPPERS.values()}
            if pid in wrappers and not state["done"]:
                state["done"] = True
                self._wait_member(pid)
                raise OSError(errno.EIO, "injected EIO", f"/proc/{pid}/stat")
            return real(pid)
        self.assertIsNone(self._start(mock.patch.object(supervisor, "get_proc_stat", eio_once)))
        self.assertTrue(state["done"])
        self._assert_aborted()


class TestMergeTestStepScansBeforeReap(unittest.TestCase):
    """ACC: N26, A8 (M3 gate P1-2): zero mocks, real mesh.runwrap via run_test_step()."""

    def test_same_group_envless_background_process_terminated(self):
        tmp = tempfile.mkdtemp(prefix="mh-gf-merge-", dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.path.join(tmp, "integ")
        os.makedirs(cwd)
        pidfile = os.path.join(tmp, "bg.pid")
        run_id = f"run_gfm{os.getpid()}"
        self.addCleanup(terminate.forget, run_id)
        cmd = (f"env -i {shutil.which('sleep')} 60 </dev/null >/dev/null 2>&1 & "
               f"echo $! > {pidfile}; exit 0")
        plan = {"run_id": run_id, "run_dir": os.path.join(tmp, "merges", "mrg_gf"), "integration": cwd,
                "test_argv": ["sh", "-c", cmd], "test_timeout_s": 60, "kill_grace_s": 2}
        bg = {}

        def guard():
            if bg:
                _kill_same(bg["pid"], bg["st"])
        self.addCleanup(guard)
        out = merge.run_test_step(plan)
        with open(pidfile, "r", encoding="utf-8") as f:
            bg_pid = int(f.read().strip())
        info = get_proc_stat(bg_pid)
        if info is not None:
            bg.update(pid=bg_pid, st=info["starttime"])
        self.assertEqual(out.get("exit_code"), 0, out)
        self.assertEqual(out.get("leftovers"), 1, out)
        self.assertEqual(out["termination"]["targets"], 1, out)
        self.assertFalse(out["termination"].get("kill_failed"), out)
        self.assertTrue(info is None or info["state"] == "Z" or not _alive(bg_pid, info["starttime"]),
                        f"background process {bg_pid} survived the test step")
        # The wrapper is reaped at the end of the step (not left a zombie).
        self.assertIsNone(get_proc_stat(out["wrapper"]["pid"]))
        # Reaped by the step itself (finally), not by Popen.__del__ later: the child
        # registry no longer holds it (Quest review mutant 3).
        self.assertNotIn(out["wrapper"]["pid"], children.registered())

    def _merge_step_unknown_first_read(self, wrapper_json_ok):
        from unittest import mock
        tmp = tempfile.mkdtemp(prefix="mh-gf-merge9-", dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.path.join(tmp, "integ")
        os.makedirs(cwd)
        pidfile = os.path.join(tmp, "bg.pid")
        run_id = f"run_gfm9{os.getpid()}{int(wrapper_json_ok)}"
        self.addCleanup(terminate.forget, run_id)
        cmd = (f"env -i {shutil.which('sleep')} 60 </dev/null >/dev/null 2>&1 & "
               f"echo $! > {pidfile}; sleep 0.3; exit 0")
        plan = {"run_id": run_id, "run_dir": os.path.join(tmp, "merges", "mrg_gf9"), "integration": cwd,
                "test_argv": ["sh", "-c", cmd], "test_timeout_s": 60, "kill_grace_s": 2}
        real = merge.get_proc_stat
        state = {"first": True}

        def first_none(pid):
            if state["first"]:
                state["first"] = False
                return None
            return real(pid)
        real_read = merge._read_json

        def read(path):
            if not wrapper_json_ok and path.endswith("wrapper.json"):
                return None
            return real_read(path)
        with mock.patch.object(merge, "get_proc_stat", first_none), mock.patch.object(merge, "_read_json", read):
            out = merge.run_test_step(plan)
        bg_pid = int(open(pidfile).read().strip())
        info = get_proc_stat(bg_pid)
        if info is not None and info["state"] != "Z":
            self.addCleanup(_kill_same, bg_pid, info["starttime"])
        self.assertTrue(info is None or info["state"] == "Z", f"same-group member {bg_pid} survived: {out}")
        return out

    def test_r9_merge_step_fills_identity_from_wrapper_json(self):
        """Round 9 (Linear P1-b): the first starttime read is None; the identity is
        filled from wrapper.json and the leftover check terminates the member."""
        out = self._merge_step_unknown_first_read(True)
        self.assertIsNotNone(out["wrapper"]["starttime"], out)
        self.assertEqual(out.get("leftovers"), 1, out)

    def test_r9_merge_step_unknown_identity_fails_closed(self):
        """Round 9 (Linear P1-b): no identity at all (stat and wrapper.json): not a
        pass (wrapper_identity_unknown) and the group member is still terminated."""
        out = self._merge_step_unknown_first_read(False)
        self.assertIsNone(out["wrapper"]["starttime"], out)
        self.assertEqual(out.get("error"), "wrapper_identity_unknown", out)
        self.assertFalse(out["passed"], out)
        self.assertEqual(out.get("leftovers"), 1, out)

    def test_r10_merge_registration_failure_runs_cleanup(self):
        """Round 10 (Linear 4): registration fails after Popen in the merge test step
        (SpawnedThenFailed): the wrapper is not abandoned; the step waits for it, the
        same-group member is terminated before the reap, the wrapper is reaped."""
        import errno
        from unittest import mock
        tmp = tempfile.mkdtemp(prefix="mh-gf-merge10-", dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.path.join(tmp, "integ")
        os.makedirs(cwd)
        pidfile = os.path.join(tmp, "bg.pid")
        run_id = f"run_gfm10{os.getpid()}"
        self.addCleanup(terminate.forget, run_id)
        cmd = (f"env -i {shutil.which('sleep')} 60 </dev/null >/dev/null 2>&1 & "
               f"echo $! > {pidfile}; sleep 0.3; exit 0")
        plan = {"run_id": run_id, "run_dir": os.path.join(tmp, "merges", "mrg_gf10"), "integration": cwd,
                "test_argv": ["sh", "-c", cmd], "test_timeout_s": 60, "kill_grace_s": 2}

        def reg_fail(pid, kind):
            raise OSError(errno.EIO, "injected EIO")
        with mock.patch.object(children, "register", reg_fail):
            out = merge.run_test_step(plan)
        bg_pid = int(open(pidfile).read().strip())
        info = get_proc_stat(bg_pid)
        if info is not None and info["state"] != "Z":
            self.addCleanup(_kill_same, bg_pid, info["starttime"])
        self.assertIn("registration_error", out)
        self.assertEqual(out.get("leftovers"), 1, out)
        self.assertTrue(info is None or info["state"] == "Z", f"member {bg_pid} survived: {out}")
        self.assertIsNone(get_proc_stat(out["wrapper"]["pid"]), "wrapper not reaped")
        self.assertNotIn(out["wrapper"]["pid"], children._held)

    def test_r11_merge_step_leader_zombie_member_is_leftover(self):
        """Round 11 (Carbon P1-1 repro b): the merge test command exits 0 but leaves a
        same-group, env-less member whose main thread called SYS_exit while a worker
        thread keeps writing. It must be found as a leftover and terminated, not
        reported as a clean pass with the worker still writing."""
        import platform
        if platform.machine() != "x86_64":
            self.skipTest("SYS_exit number is x86_64-specific")
        from mesh.core import proc as proc_mod
        tmp = tempfile.mkdtemp(prefix="mh-gf11-mz-", dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.path.join(tmp, "integ")
        os.makedirs(cwd)
        pidfile = os.path.join(tmp, "mz.pid")
        counter = os.path.join(tmp, "counter")
        script = os.path.join(tmp, "mz.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import ctypes, os, threading, time\n"
                    "def work():\n"
                    "    i = 0\n"
                    "    while True:\n"
                    "        i += 1\n"
                    f"        open({counter!r}, 'w').write(str(i))\n"
                    "        time.sleep(0.03)\n"
                    "threading.Thread(target=work).start()\n"
                    f"open({pidfile!r} + '.tmp', 'w').write(str(os.getpid()))\n"
                    f"os.rename({pidfile!r} + '.tmp', {pidfile!r})\n"
                    "time.sleep(0.1)\n"
                    "ctypes.CDLL(None).syscall(60, 0)\n")
        run_id = f"run_gfmz11{os.getpid()}"
        self.addCleanup(terminate.forget, run_id)
        cmd = (f"env -i {sys.executable} {script} </dev/null >/dev/null 2>&1 & "
               f"while [ ! -e {pidfile} ]; do sleep 0.05; done; sleep 0.5; exit 0")
        plan = {"run_id": run_id, "run_dir": os.path.join(tmp, "merges", "mrg_mz11"), "integration": cwd,
                "test_argv": ["sh", "-c", cmd], "test_timeout_s": 60, "kill_grace_s": 2}
        out = merge.run_test_step(plan)
        mpid = int(open(pidfile).read().strip())
        info = get_proc_stat(mpid)
        if info is not None and proc_mod.is_tgid_alive(mpid, info):
            self.addCleanup(_kill_same, mpid, info["starttime"])
        self.assertGreaterEqual(out.get("leftovers") or 0, 1, out)
        self.assertFalse(info is not None and proc_mod.is_tgid_alive(mpid, info),
                         f"leader-zombie member {mpid} still alive: {out}")
        c1 = open(counter).read()
        time.sleep(0.2)
        self.assertEqual(c1, open(counter).read(), "worker thread still writing")

    def test_wait_exit_unreaped_times_out_and_does_not_reap(self):
        """Quest review mutant 4: _wait_exit_unreaped is False on timeout for a live
        process, True once it exited, and leaves the zombie unreaped."""
        proc = subprocess.Popen([shutil.which("sleep"), "30"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        t0 = time.monotonic()
        self.assertFalse(merge._wait_exit_unreaped(proc, 0.4))
        self.assertGreaterEqual(time.monotonic() - t0, 0.35)
        os.kill(proc.pid, signal.SIGKILL)
        self.assertTrue(merge._wait_exit_unreaped(proc, 10))
        self.assertEqual(get_proc_stat(proc.pid)["state"], "Z", "exit wait reaped the wrapper")
        self.assertIsNone(proc.returncode)
        proc.wait()


_MT_LEADER_ZOMBIE = r"""
import ctypes, os, threading, time
ev = threading.Event()
def worker():
    ev.set()
    time.sleep(60)
t = threading.Thread(target=worker)
t.start()
ev.wait()
tid = getattr(t, "native_id", 0) or 0
print(os.getpid(), tid, flush=True)
libc = ctypes.CDLL(None)
SYS_exit = 60 if os.uname().machine == "x86_64" else 93
libc.syscall(SYS_exit, 0)
"""


class TestRound11LeaderZombieAndProcUnknown(unittest.TestCase):
    """ACC: N18, A8 (Round 11: Carbon P1-1 multithreaded leader-zombie; Linear r10 P1-1 EACCES/EIO unknown)."""

    def _spawn_mt_leader_zombie(self, env=None, start_new_session=False):
        p = subprocess.Popen(
            [sys.executable, "-c", _MT_LEADER_ZOMBIE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=start_new_session,
        )
        line = p.stdout.readline().decode().strip()
        p.stdout.close()
        tgid, tid = [int(x) for x in line.split()]
        info = get_proc_stat(tgid)
        st = int(info["starttime"]) if info else None

        def cleanup():
            if st is not None:
                try:
                    cur = get_proc_stat(tgid)
                    if cur is not None and int(cur["starttime"]) == st:
                        os.kill(tgid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                p.kill()
            except OSError:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass

        self.addCleanup(cleanup)
        self.assertTrue(
            _wait(lambda: (get_proc_stat(tgid) or {}).get("state") == "Z", 5.0),
            f"leader {tgid} never entered state Z",
        )
        return p, tgid, tid, st

    def test_r11_mt_leader_zombie_proc_liveness_and_environ(self):
        from mesh.core import proc as proc_mod

        run_id = f"run_mtz_{os.getpid()}"
        env = dict(os.environ, MESH_RUN_ID=run_id)
        p, tgid, _tid, _st = self._spawn_mt_leader_zombie(env=env, start_new_session=True)
        info = proc_mod.get_proc_stat(tgid)
        self.assertIsNotNone(info)
        self.assertEqual(info["state"], "Z")
        self.assertTrue(hasattr(proc_mod, "is_tgid_alive"), "proc.is_tgid_alive missing")
        self.assertTrue(proc_mod.is_tgid_alive(tgid, info), "leader-zombie with live worker reported dead")
        pe = proc_mod.get_proc_environ(tgid)
        self.assertIsNotNone(pe, "get_proc_environ returned None for live leader-zombie")
        self.assertEqual(pe.get("MESH_RUN_ID"), run_id)
        self.assertIn(tgid, proc_mod.find_run_processes(run_id, None))
        # Once worker thread is killed (before reap: true zombie), is_tgid_alive is False.
        os.kill(tgid, signal.SIGKILL)
        self.assertTrue(_wait(lambda: not proc_mod.is_tgid_alive(tgid), 5.0))
        p.wait(timeout=5)

    def test_r11_mt_leader_zombie_wrapper_and_leftovers(self):
        from mesh.core import proc as proc_mod

        tmp = tempfile.mkdtemp(prefix="mh-gf11-mtz-", dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, True)
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, "logs"))
        conn = init_db(os.path.join(home, "mesh.db"))
        self.addCleanup(conn.close)
        run_id = f"run_mtzw_{os.getpid()}"
        run_dir = os.path.join(tmp, "run")
        os.makedirs(run_dir)
        self.addCleanup(lambda: (
            terminate.forget(run_id),
            supervisor._CLEAR.pop(run_id, None),
            supervisor._CLOSE_REFUSED_LOGGED.pop(run_id, None),
            supervisor._evict_seen(set()),
        ))

        # 1. Wrapper itself is a multithreaded leader-zombie: still alive, close_gate refuses.
        wp, wpid, _wtid, wst = self._spawn_mt_leader_zombie(start_new_session=True)
        now = now_iso_utc()
        conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_m','agent','m',?);", (now,))
        conn.execute("INSERT INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
                     "VALUES ('agt_m','fake','launch',1,?,?);", (now, now))
        conn.execute("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                     "VALUES ('prj_m','p','g','active','usr','cli',?,?);", (now, now))
        conn.execute("INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                     "timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
                     "VALUES ('tsk_m','prj_m','t','s','general','running','agt_m','edit',600,1,1,'usr',?,?);",
                     (now, now))
        conn.execute("INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                     "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at) "
                     "VALUES (?,'task','tsk_m','prj_m','agt_m',1,1,'fake','running','b1',?,?,?,?,?,?);",
                     (run_id, wpid, wpid, wst, run_dir, now, now_iso_utc(time.time() + 600)))
        conn.execute("INSERT INTO leases (key, holder_kind, holder, run_id, fence, acquired_at, heartbeat_at, expires_at) "
                     "VALUES ('task:tsk_m','meshd','b1',?,1,?,?,?);",
                     (run_id, now, now, now_iso_utc(time.time() + 60)))
        conn.commit()
        with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as f:
            json.dump({"exit_code": 0, "signal": None, "ended_at": now}, f)
        run = conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertTrue(supervisor.wrapper_alive(run), "leader-zombie wrapper treated as exited")
        self.assertTrue(recovery._identity_alive({"pid": wpid, "starttime": wst}))
        with self.assertRaises(supervisor.CloseHeld):
            supervisor.close_gate(run, "exited")
        supervisor._watch_task_run(conn, home, run)
        self.assertEqual(conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()[0], "running")

        # 2. Spawn a setsid MESH_RUN_ID carrier and an env-less member that are BOTH leader-zombies;
        #    kill+reap wrapper and verify terminate finds, signals, and kills both thread groups.
        cp, cpid, _ctid, cst = self._spawn_mt_leader_zombie(
            env={"PATH": os.environ.get("PATH", ""), "MESH_RUN_ID": run_id},
            start_new_session=True,
        )
        mp, mpid, _mtid, mst = self._spawn_mt_leader_zombie(env={}, start_new_session=False)
        os.kill(wpid, signal.SIGKILL)
        wp.wait(timeout=5)
        targets = terminate.run_targets(run_id, wpid, wpid, wst, seen={mpid: mst})
        self.assertIn(cpid, targets, f"leader-zombie carrier missed: {targets}")
        self.assertIn(mpid, targets, f"leader-zombie seen member missed: {targets}")
        t = terminate.begin(run_id, wpid, wpid, "leftovers", 2.0, wrapper_starttime=wst, seen={mpid: mst})
        self.assertTrue(
            _wait(lambda: (cp.poll() is not None and mp.poll() is not None and terminate.step(t) == "done"), 5.0),
            "terminate failed to finish leader-zombie member/carrier",
        )
        self.assertFalse(proc_mod.is_tgid_alive(cpid))
        self.assertFalse(proc_mod.is_tgid_alive(mpid))

    def test_r11_mt_leader_zombie_reap_daemons_and_smoke_harness(self):
        import importlib.util
        from tests.smoke import harness as smoke_harness

        spec = importlib.util.spec_from_file_location(
            "reap_daemons_r11",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                         "scripts", "coord", "reap_daemons.py"),
        )
        reap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reap)

        p, tgid, _tid, st = self._spawn_mt_leader_zombie(start_new_session=True)
        pstat = reap.process_stat(tgid)
        self.assertNotEqual(pstat[0], "Z", "reap_daemons treated live leader-zombie as Z")
        rec = {"pid": tgid, "starttime": st, "suite_starttime": st}
        self.assertEqual(reap.decision(rec, pstat, True, True), "skipped_live")
        self.assertTrue(reap.adhoc_alive(rec, pstat))
        self.assertTrue(smoke_harness._group_alive(tgid))
        os.kill(tgid, signal.SIGKILL)
        p.wait(timeout=5)
        self.assertFalse(smoke_harness._group_alive(tgid))

    def test_r11_proc_eacces_and_eio_are_unknown_not_gone(self):
        import builtins
        import errno
        from unittest import mock
        from mesh import constants as c
        from mesh.core import auth as auth_mod, proc as proc_mod
        from mesh.errors import MeshError

        proc = subprocess.Popen([shutil.which("sleep"), "60"])
        info = proc_mod.get_proc_stat(proc.pid)
        st = int(info["starttime"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))

        real_open = builtins.open
        err_holder = {"exc": PermissionError(errno.EACCES, "Permission denied", f"/proc/{proc.pid}/stat")}

        def scoped_open(file, *args, **kwargs):
            if file == f"/proc/{proc.pid}/stat" and err_holder["exc"] is not None:
                raise err_holder["exc"]
            return real_open(file, *args, **kwargs)

        tmp = tempfile.mkdtemp(prefix="mh-gf11-eio-", dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, True)
        run_id = f"run_eio_{os.getpid()}"
        self.addCleanup(lambda: (
            terminate.forget(run_id),
            supervisor._CLOSE_REFUSED_LOGGED.pop(run_id, None),
        ))
        run = {
            "id": run_id, "kind": "task", "task_id": "tsk_e", "project_id": "prj_e",
            "wrapper_pid": proc.pid, "pgid": proc.pid, "proc_starttime": st, "run_dir": tmp,
        }

        with mock.patch.object(builtins, "open", scoped_open):
            for errno_val in (errno.EACCES, errno.EIO):
                err_holder["exc"] = (
                    PermissionError(errno.EACCES, "Permission denied", f"/proc/{proc.pid}/stat")
                    if errno_val == errno.EACCES
                    else OSError(errno.EIO, "Input/output error", f"/proc/{proc.pid}/stat")
                )
                with self.assertRaises(OSError):
                    proc_mod.get_proc_stat(proc.pid)
                self.assertTrue(supervisor._identity_live(proc.pid, st))
                with self.assertRaises(supervisor.CloseHeld):
                    supervisor.close_gate(run, "exited")
                self.assertFalse(terminate.signal_if_same(proc.pid, st, signal.SIGTERM))
                self.assertEqual(recovery._live_members({proc.pid: st}), {proc.pid: st})
                # A seen target with unreadable /proc/<pid>/stat must NOT be dropped as gone.
                targets = terminate.run_targets(run_id, None, None, None, seen={proc.pid: st})
                self.assertEqual(targets.get(proc.pid), st)
                with self.assertRaises(MeshError) as cm:
                    auth_mod.classify_peer_run_session(proc.pid, {run_id: proc.pid}, {run_id}, os.getpid())
                self.assertEqual(cm.exception.code, c.CREDENTIAL_NOT_ALLOWED_IN_RUN)

        # Once truly dead and reaped, ENOENT/ESRCH returns None (gone).
        proc.kill()
        proc.wait()
        self.assertIsNone(proc_mod.get_proc_stat(proc.pid))


class TestRound11SpawnAbortAndPostPopen(unittest.TestCase):
    """Round 11 (Items 2, 5, 6a):
    - Item 5: _SpawnAbort.known & prior_members store cumulative (pid, starttime) pairs
      so PID reuse within the same abort never shadows the new generation, and signals
      verify starttime right before kill.
    - Item 2: TX B and post-Popen failures in both task (_spawn_run) and lead_turn
      (commit_lead_turn) kill spawned wrapper/group/carriers, hold lease & outbox while
      anything survives or closing DB write fails, and populate refresh_run_caches even
      when runs.wrapper_pid IS NULL.
    - Item 6a (Carbon P2-1): _advance forgets a Termination only after _finalize_termination
      returns without raising, and advance_terminations routes CloseHeld to hold_close.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-gf11-s2-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "logs"))
        os.makedirs(os.path.join(self.home, "run"))
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)
        self.groups: list[int] = []
        self.spawned_pairs: list[tuple[int, int]] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for rid, a in list(supervisor._SPAWN_ABORTS.items()):
            supervisor._SPAWN_ABORTS.pop(rid, None)
            children.unhold(a.proc.pid)
            try:
                a.proc.kill()
                a.proc.wait(5)
            except Exception:
                pass
            children.unregister_if(a.proc.pid, "wrapper")
        for rid, proc in list(supervisor._WRAPPERS.items()):
            supervisor._WRAPPERS.pop(rid, None)
            children.unhold(proc.pid)
            try:
                proc.kill()
                proc.wait(5)
            except Exception:
                pass
            children.unregister_if(proc.pid, "wrapper")
        for g in self.groups:
            if g <= 2 or g == os.getpgrp():
                continue
            for p, st in supervisor._scan_group(g).items():
                _kill_same(p, st)
        for p, st in self.spawned_pairs:
            _kill_same(p, st)
        terminate._active.clear()
        supervisor._CLEAR.clear()
        supervisor._CLOSE_REFUSED_LOGGED.clear()
        supervisor._evict_seen(set())

    def test_r11_spawn_abort_cumulative_pid_starttime_pairs_and_prior_members(self):
        run_id = f"run_r11_pairs_{os.getpid()}"
        run_dir = os.path.join(self.tmp, "rd_pairs")
        os.makedirs(run_dir)
        wp = subprocess.Popen([shutil.which("sleep"), "60"], start_new_session=True)
        wst = int(get_proc_stat(wp.pid)["starttime"])
        self.spawned_pairs.append((wp.pid, wst))
        self.addCleanup(lambda: (wp.kill(), wp.wait()))

        # Spawn a setsid carrier; pre-seed ab.known with an older generation of the SAME pid
        # to reproduce PID reuse within a single pending _SpawnAbort.
        esc_script = (
            "import os, signal, time\n"
            "p = os.environ['TRIG']\n"
            "while not os.path.exists(p):\n"
            "    time.sleep(0.01)\n"
            "os.execve(os.environ['SLEEP'], ['sleep', '60'], {})\n"
        )
        trig = os.path.join(self.tmp, "esc.trig")
        esc = subprocess.Popen(
            [sys.executable, "-c", esc_script],
            env={
                "PATH": os.environ.get("PATH", ""),
                "MESH_RUN_ID": run_id,
                "TRIG": trig,
                "SLEEP": shutil.which("sleep"),
            },
            start_new_session=True,
        )
        est = int(get_proc_stat(esc.pid)["starttime"])
        self.spawned_pairs.append((esc.pid, est))
        self.addCleanup(lambda: (esc.kill(), esc.wait()))

        ab = supervisor._SpawnAbort(
            wp, run_id, ("tsk_x", "prj_x", 1, 1, 1, "boom"), run_dir=run_dir, home=self.home
        )
        # Earlier dead generation on the same PID must NOT shadow the new generation (esc.pid, est).
        if isinstance(ab.known, set):
            ab.known.add((esc.pid, max(1, est - 10)))
        else:
            ab.known[esc.pid] = max(1, est - 10)

        supervisor._abort_collect(ab)
        with open(os.path.join(run_dir, "seen.json"), "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertIn({"pid": esc.pid, "starttime": max(1, est - 10)}, doc["prior_members"])
        self.assertIn({"pid": esc.pid, "starttime": est}, doc["prior_members"])

        # Escapee clears MESH_RUN_ID via execve (same pid + est); _abort_kill_pass must still kill it!
        with open(trig, "w", encoding="utf-8"):
            pass
        from mesh.core.proc import get_proc_environ
        self.assertTrue(
            _wait(lambda: "MESH_RUN_ID" not in (get_proc_environ(esc.pid) or {}), 5.0),
            "escapee did not clear env",
        )
        # Even if write_unusable_marker rewrites seen.json or close_gate checks prior_members with
        # duplicate pids (newer fake generation listed after live generation), close_gate must refuse!
        run_row = {
            "id": run_id, "kind": "task", "task_id": "tsk_x", "project_id": "prj_x",
            "wrapper_pid": wp.pid, "pgid": wp.pid, "proc_starttime": wst, "run_dir": run_dir,
        }
        wp.kill()
        wp.wait(timeout=5)
        with open(os.path.join(run_dir, "seen.json"), "w", encoding="utf-8") as f:
            json.dump({
                supervisor.SEEN_MARKER_KEY: "unusable", "run_id": run_id, "reason": "spawn_abort_pending",
                "written_at": now_iso_utc(),
                "prior_members": [
                    {"pid": esc.pid, "starttime": est},
                    {"pid": esc.pid, "starttime": est + 999},
                ],
            }, f)
        supervisor.write_unusable_marker(run_row, "adopt_pending", self.home)
        with self.assertRaises(supervisor.CloseHeld) as held_cm:
            supervisor.close_gate(run_row, "lost_on_restart")
        self.assertEqual(held_cm.exception.live.get(esc.pid), est)

        done, _live = supervisor._abort_kill_pass(ab)
        esc.wait(timeout=5)
        self.assertTrue(_wait(lambda: not _alive(esc.pid, est), 3.0), "reused-PID escapee survived abort")

    def test_r11_task_spawn_tx_b_failure_and_locked_close_kills_wrapper_and_retries(self):
        from unittest import mock
        from mesh import constants as c
        from mesh.adapters.base import CommandSpec
        from mesh.adapters.registry import get_adapter as real_get
        from mesh.core import auth as auth_mod
        from mesh.daemon.server import DaemonServer
        from mesh.errors import MeshError

        real_tx = supervisor.transaction

        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_t','agent','t',?);", (now,))
        ex("INSERT INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
           "VALUES ('agt_t','fake','launch',1,?,?);", (now, now))
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_t','p','g','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
           "timeout_s, max_attempts, created_by, created_at, updated_at) "
           "VALUES ('tsk_t','prj_t','t','s','general','queued','agt_t','edit',600,1,'usr',?,?);", (now, now))
        self.conn.commit()

        member_cmd = (f"env -i {sys.executable} -c 'import time; time.sleep(120)' >/dev/null 2>&1 & "
                      f"{shutil.which('sleep')} 120")

        def get_adapter(adapter_id, testing_mode=False, options=None):
            a = real_get(adapter_id, testing_mode=testing_mode, options=options)
            a.build = lambda req: CommandSpec(argv=["sh", "-c", member_cmd], cwd=req.workspace)
            return a

        locked = {"on": True, "wpid": None}

        @contextlib.contextmanager
        def locked_after_popen(conn):
            if locked["on"] and (supervisor._WRAPPERS or supervisor._SPAWN_ABORTS):
                if locked["wpid"] is None and supervisor._WRAPPERS:
                    wpid = next(iter(supervisor._WRAPPERS.values())).pid
                    locked["wpid"] = wpid
                    self.groups.append(wpid)
                    assert _wait(lambda: len(supervisor._scan_group(wpid)) >= 3, 10), "member did not start"
                raise sqlite3.OperationalError("database is locked")
            with real_tx(conn):
                yield

        task = self.conn.execute("SELECT * FROM tasks WHERE id = 'tsk_t';").fetchone()
        agent = self.conn.execute("SELECT * FROM agents WHERE id = 'agt_t';").fetchone()
        with mock.patch.object(supervisor, "get_adapter", get_adapter), \
                mock.patch.object(supervisor, "transaction", locked_after_popen), \
                mock.patch.object(supervisor, "_log_meshd"):
            res = supervisor.start_launch_run(self.conn, self.home, task, agent, testing_mode=True)
        self.assertIsNone(res)
        wpid = locked["wpid"]
        self.assertIsNotNone(wpid)

        run = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_t';").fetchone()
        # DB was locked since Popen: wrapper_pid in DB is still NULL, YET group members are dead,
        # abort is tracked in _SPAWN_ABORTS, and refresh_run_caches classifies the wrapper session!
        self.assertIsNone(run["wrapper_pid"])
        self.assertEqual(supervisor._scan_group(wpid), {}, "TX B failure left group members running")
        self.assertIn(run["id"], supervisor._SPAWN_ABORTS)

        srv = DaemonServer.__new__(DaemonServer)
        srv.db_conn = self.conn
        srv.known_run_ids = set()
        srv.active_runs = {}
        srv.refresh_run_caches()
        self.assertEqual(srv.active_runs.get(run["id"]), wpid)
        self.assertEqual(
            auth_mod.classify_peer_run_session(wpid, srv.active_runs, srv.known_run_ids, os.getpid()),
            run["id"],
        )
        import asyncio
        with self.assertRaises(MeshError) as cm:
            asyncio.run(srv.authenticate_and_authorize(wpid, {"type": "user"}, "task.list", "req_1"))
        self.assertEqual(cm.exception.code, c.CREDENTIAL_NOT_ALLOWED_IN_RUN)

        # Unlock SQLite: next watch_launch_runs closes spawn_failed, records wrapper_pid, releases lease, reaps.
        locked["on"] = False
        with mock.patch.object(supervisor, "_log_meshd"):
            closed = supervisor.watch_launch_runs(self.conn, self.home)
        self.assertIn(run["id"], closed)
        run_after = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run["id"],)).fetchone()
        self.assertEqual((run_after["status"], run_after["end_reason"]), ("spawn_failed", "spawn_failed"))
        self.assertEqual(run_after["wrapper_pid"], wpid)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (run["id"],)).fetchone())
        self.assertIsNone(get_proc_stat(wpid), "wrapper not reaped after retry close")

    def test_r11_lead_turn_post_popen_and_tx_b_failure_kills_wrapper_holds_lease_and_outbox(self):
        import errno
        from unittest import mock
        from mesh.adapters.base import CommandSpec
        from mesh.adapters.registry import get_adapter as real_get
        from mesh.core.events import record_event
        from mesh.daemon import lead_turns

        real_tx = supervisor.transaction

        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO meta (key, value) VALUES ('boot_id', 'b_lt');")
        ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_l','agent','lead',?);", (now,))
        ex("INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
           "VALUES ('agt_l','fake','launch',1,1,?,?);", (now, now))
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_l','pl','gl','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
           "VALUES ('las_l','prj_l','agt_l','usr','cli',?);", (now,))
        ev = record_event(
            self.conn, type_="project.note", actor_id="usr", actor_kind="user", via="direct",
            project_id="prj_l", payload={"note": "hi"},
        )
        ex("DELETE FROM outbox;")
        ex("INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, payload_json, "
           "status, attempts, next_attempt_at, created_at) "
           "VALUES ('out_l',?,'agt_l','lead_wake','dk_l','prj_l','lead.wake','{}','pending',0,?,?);",
           (ev["seq"], now, now))
        self.conn.commit()

        esc_pidfile = os.path.join(self.tmp, "lt_esc.pid")
        esc_script = os.path.join(self.tmp, "lt_esc.py")
        runner_script = os.path.join(self.tmp, "lt_runner.py")
        with open(esc_script, "w", encoding="utf-8") as f:
            f.write(
                "import os, time\n"
                f"open({esc_pidfile!r} + '.t', 'w').write(str(os.getpid()))\n"
                f"os.rename({esc_pidfile!r} + '.t', {esc_pidfile!r})\n"
                "time.sleep(120)\n"
            )
        with open(runner_script, "w", encoding="utf-8") as f:
            f.write(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, {esc_script!r}], start_new_session=True)\n"
                "time.sleep(120)\n"
            )

        def get_adapter(adapter_id, testing_mode=False, options=None):
            a = real_get(adapter_id, testing_mode=testing_mode, options=options)
            a.build = lambda req: CommandSpec(argv=[sys.executable, runner_script], cwd=req.workspace)
            return a

        g = {"block_esc": True, "wpid": None, "esc": None, "est": None, "fail_tx_b": True}
        real_kill = os.kill

        def guarded_kill(pid, sig):
            if sig != 0 and g["block_esc"] and g["esc"] is not None and pid == g["esc"]:
                raise PermissionError(errno.EPERM, "injected EPERM")
            return real_kill(pid, sig)

        @contextlib.contextmanager
        def tx_fail_on_tx_b(conn):
            if g["fail_tx_b"] and supervisor._WRAPPERS:
                wpid = next(iter(supervisor._WRAPPERS.values())).pid
                g["wpid"] = wpid
                self.groups.append(wpid)
                assert _wait(lambda: os.path.exists(esc_pidfile), 10), "lead escapee did not start"
                with open(esc_pidfile, "r", encoding="utf-8") as pf:
                    g["esc"] = int(pf.read())
                g["est"] = int(get_proc_stat(g["esc"])["starttime"])
                self.spawned_pairs.append((g["esc"], g["est"]))
                g["fail_tx_b"] = False
                raise sqlite3.OperationalError("TX B locked")
            with real_tx(conn):
                yield

        lead = lead_turns.get_project_active_lead(self.conn, "prj_l")
        msgs = self.conn.execute("SELECT * FROM outbox WHERE project_id = 'prj_l';").fetchall()
        with mock.patch.object(lead_turns, "get_adapter", get_adapter), \
                mock.patch.object(lead_turns, "transaction", tx_fail_on_tx_b), \
                mock.patch("os.kill", guarded_kill), \
                mock.patch.object(supervisor, "SPAWN_ABORT_WAIT_S", 0.2), \
                mock.patch.object(supervisor, "_log_meshd"):
            rid = lead_turns.start_lead_turn(self.conn, self.home, "prj_l", lead, msgs, testing_mode=True)
        self.assertIsNone(rid)

        run = self.conn.execute("SELECT * FROM runs WHERE project_id = 'prj_l';").fetchone()
        # While escapee survives: run stays open, leadturn lease stays held, outbox stays 'delivering'!
        self.assertIn(run["id"], supervisor._SPAWN_ABORTS)
        self.assertIn(run["status"], ("starting", "running"))
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM leases WHERE key = 'leadturn:prj_l' AND run_id = ?;",
                              (run["id"],)).fetchone(),
            "leadturn lease released while spawned lead escapee still alive",
        )
        out_row = self.conn.execute("SELECT status, attempts FROM outbox WHERE id = 'out_l';").fetchone()
        self.assertEqual(out_row["status"], "delivering", "outbox requeued while spawned lead escapee still alive")

        # Unblock kill: watch_launch_runs kills escapee, reaps wrapper, closes spawn_failed, requeues outbox!
        g["block_esc"] = False
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(
                _wait(lambda: (supervisor.watch_launch_runs(self.conn, self.home),
                               self.conn.execute("SELECT status FROM runs WHERE id = ?;",
                                                 (run["id"],)).fetchone()[0] == "spawn_failed")[1], 10.0)
            )
        self.assertFalse(_alive(g["esc"], g["est"]))
        self.assertIsNone(get_proc_stat(g["wpid"]))
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM leases WHERE key = 'leadturn:prj_l';").fetchone()
        )
        out_after = self.conn.execute("SELECT status, attempts FROM outbox WHERE id = 'out_l';").fetchone()
        self.assertEqual((out_after["status"], out_after["attempts"]), ("pending", 1))

    def test_r11_advance_preserves_termination_end_reason_on_close_held_and_db_error(self):
        from unittest import mock

        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_a','agent','a',?);", (now,))
        ex("INSERT INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
           "VALUES ('agt_a','fake','launch',1,?,?);", (now, now))
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_a','pa','ga','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
           "timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
           "VALUES ('tsk_a','prj_a','ta','sa','general','running','agt_a','edit',600,1,1,'usr',?,?);", (now, now))
        run_id = f"run_adv_{os.getpid()}"
        run_dir = os.path.join(self.tmp, "rd_adv")
        os.makedirs(run_dir)
        # Wrapper already wrote exit.json with exit_code=0 right as timeout termination fired.
        with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as f:
            json.dump({"exit_code": 0, "signal": None, "ended_at": now}, f)

        wp = subprocess.Popen([shutil.which("sleep"), "60"], start_new_session=True)
        wpid = wp.pid
        wst = int(get_proc_stat(wpid)["starttime"])
        self.spawned_pairs.append((wpid, wst))
        wp.kill()
        wp.wait(timeout=5)

        ex("INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
           "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at) "
           "VALUES (?,'task','tsk_a','prj_a','agt_a',1,1,'fake','running','b_old',?,?,?,?,?,?);",
           (run_id, wpid, wpid, wst, run_dir, now, now_iso_utc(time.time() - 5)))
        ex("INSERT INTO leases (key, holder_kind, holder, run_id, fence, acquired_at, heartbeat_at, expires_at) "
           "VALUES ('task:tsk_a','meshd','b_new',?,1,?,?,?);",
           (run_id, now, now, now_iso_utc(time.time() + 60)))
        self.conn.commit()

        t = terminate.begin(run_id, wpid, wpid, "timeout", 1.0, wrapper_starttime=wst)
        self.assertEqual(terminate.step(t), "pending")
        time.sleep(terminate.POLL_S + 0.02)

        # 1. Transient DB error inside _finalize_termination on advance_terminations:
        #    Termination MUST NOT be forgotten!
        real_fin = supervisor._finalize_termination
        calls = {"n": 0}

        def fail_then_hold(conn, home, r, term):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("transient lock")
            if calls["n"] == 2:
                raise supervisor.CloseHeld(run_id, term.end_reason, {}, True, wpid, wst)
            return real_fin(conn, home, r, term)

        with mock.patch.object(supervisor, "_finalize_termination", fail_then_hold), \
                mock.patch.object(supervisor, "_log_meshd"):
            supervisor.advance_terminations(self.conn, self.home)
            self.assertIsNotNone(terminate.get(run_id), "Termination forgotten on transient DB error")
            self.assertEqual(terminate.get(run_id).end_reason, "timeout")

            # 2. Next done step raises CloseHeld inside advance_terminations -> hold_close runs AND
            #    Termination STILL keeps end_reason='timeout' so exit.json(0) cannot auto-submit!
            self.assertEqual(terminate.step(t), "pending")
            time.sleep(terminate.POLL_S + 0.02)
            supervisor.advance_terminations(self.conn, self.home)
            self.assertIsNotNone(terminate.get(run_id), "Termination forgotten on CloseHeld")
            self.assertEqual(terminate.get(run_id).end_reason, "timeout")

            # Clear the hold created by hold_close so finalization can succeed on the third attempt.
            supervisor.release_seen_hold_memory_for_test = None
            supervisor._SEEN_UNUSABLE.discard(run_id)
            if os.path.exists(os.path.join(run_dir, "seen.json")):
                os.unlink(os.path.join(run_dir, "seen.json"))
            self.conn.execute("DELETE FROM meta WHERE key = ?;", (supervisor.seen_hold_key(run_id),))
            self.conn.commit()
            # Re-register t if release_seen_hold wasn't called (it shouldn't have been).
            self.assertIs(terminate.get(run_id), t)

            supervisor.watch_launch_runs(self.conn, self.home)
            time.sleep(terminate.POLL_S + 0.02)
            supervisor.advance_terminations(self.conn, self.home)

        run_row = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_id,)).fetchone()
        task_row = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_a';").fetchone()
        self.assertEqual((run_row["status"], run_row["end_reason"]), ("killed", "timeout"))
        self.assertEqual(task_row["status"], "failed", "Timed-out task was auto-submitted after CloseHeld/DB error!")


class TestRound11AdapterAuthzBinaryAsyncDryRun(unittest.TestCase):
    """Round 11 Item 3:
    1. adapter.{list,show,probe,models} require authentication (anonymous -> rc 3 TOKEN_MISSING),
       allow direct user & direct agent tokens, reject run_tokens (rc 4 CAPABILITY_MISSING, or
       rc 9 LEAD_SUPERSEDED when superseded lead run_token) and relayed calls (rc 4 RELAY_NOT_ALLOWED).
    2. --binary / binary_path removed from adapter.models CLI, rejected on adapter.{list,show,models}
       with rc 2 INVALID_ARGUMENT, and gated to direct user only on adapter.probe (rc 4 CAPABILITY_MISSING).
    3. adapter.* subprocess probes / model listings run off the asyncio event loop and outside
       SQLite BEGIN IMMEDIATE so concurrent status & DB-write requests are never blocked.
    4. --dry-run on adapter.* spawns zero subprocesses and writes zero adapter_probes / idempotency rows.
    """

    def setUp(self):
        from tests.helpers import MeshHarness
        from tests.stubs import create_test_stubs

        self.stubs_dir = tempfile.mkdtemp(prefix="mh-r11-stubs-")
        create_test_stubs(self.stubs_dir)
        self.spawned_pairs = []
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

    def tearDown(self):
        try:
            self.harness.teardown()
        finally:
            for pid, st in reversed(self.spawned_pairs):
                _kill_same(pid, st)
            shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def _mint_run_token(self, principal_id: str, scope: dict) -> str:
        from mesh.core.auth import generate_run_token
        from mesh.core.ids import new_id

        tok, lookup, secret_hash = generate_run_token()
        now = now_iso_utc()
        exp = now_iso_utc(time.time() + 600)
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES (?, ?, 'run_token', ?, ?, ?, ?, ?);",
                (new_id("cred"), principal_id, lookup, secret_hash, json.dumps(scope), now, exp),
            )
            conn.commit()
        finally:
            conn.close()
        return tok

    def test_r11_adapter_authz_matrix_across_all_credentials(self):
        import mesh.constants as c
        from mesh.client import MeshClient
        from mesh.core.authz import lead_run_token_scope

        h = self.harness
        rc, r = h.setup_cli("agent", "create", "--name", "ag-plain", "--adapter", "fake", "--max-concurrency", "1",
                            "--allowed-model", "m1", "--default-model", "m1", user=True)
        self.assertEqual(rc, 0, r)
        ag_plain_id = r["data"]["id"]
        rc, r = h.setup_cli("agent", "token", "issue", "ag-plain", user=True)
        self.assertEqual(rc, 0, r)
        tok_plain = r["data"]["token"]

        rc, r = h.setup_cli("agent", "create", "--name", "ag-relay", "--adapter", "fake", "--max-concurrency", "1",
                            "--allowed-model", "m1", "--default-model", "m1", "--capability", "relay", user=True)
        self.assertEqual(rc, 0, r)
        rc, r = h.setup_cli("agent", "token", "issue", "ag-relay", user=True)
        self.assertEqual(rc, 0, r)
        tok_relay = r["data"]["token"]

        rc, r = h.setup_cli("agent", "create", "--name", "ag-lead1", "--adapter", "fake", "--max-concurrency", "1",
                            "--allowed-model", "m1", "--default-model", "m1", "--can-lead", user=True)
        self.assertEqual(rc, 0, r)
        ag_lead1_id = r["data"]["id"]
        rc, r = h.setup_cli("agent", "create", "--name", "ag-lead2", "--adapter", "fake", "--max-concurrency", "1",
                            "--allowed-model", "m1", "--default-model", "m1", "--can-lead", user=True)
        self.assertEqual(rc, 0, r)

        rc, r = h.setup_cli("project", "create", "--title", "p-authz", "--goal", "g", "--criterion", "c1",
                            "--lead", "ag-lead1", user=True)
        self.assertEqual(rc, 0, r)
        prj_id = r["data"]["id"]

        db_path = os.path.join(h.home, "mesh.db")
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            asg_id = conn.execute(
                "SELECT id FROM lead_assignments WHERE project_id = ? AND ended_at IS NULL;",
                (prj_id,),
            ).fetchone()[0]
        finally:
            conn.close()

        rt_exec = self._mint_run_token(
            ag_plain_id,
            {"task_id": "tsk_dummy", "project_id": prj_id, "run_id": "run_exec_1", "fence": 1},
        )
        rt_lead = self._mint_run_token(
            ag_lead1_id,
            {"role": "lead", "project_id": prj_id, "lead_assignment_id": asg_id, "run_id": "run_lead_1"},
        )

        client = MeshClient(h.home)
        cmds = [
            ("adapter.list", {}),
            ("adapter.show", {"adapter_id": "kiro"}),
            ("adapter.probe", {"adapter_id": "kiro"}),
            ("adapter.models", {"adapter_id": "kiro"}),
        ]

        for cmd_name, cmd_args in cmds:
            # 1. Anonymous -> rc 3 TOKEN_MISSING
            resp = client.send_request({"v": 1, "cmd": cmd_name, "args": cmd_args, "auth": {"type": "none"}})
            self.assertFalse(resp["ok"], (cmd_name, resp))
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_MISSING), cmd_name)

            # 2. Direct user -> rc 0
            resp = client.send_request({
                "v": 1, "cmd": cmd_name, "args": cmd_args,
                "auth": {"type": "user", "passphrase": h.passphrase},
            })
            self.assertTrue(resp["ok"], (cmd_name, resp))
            self.assertEqual(resp["exit_code"], 0, cmd_name)

            # 3. Direct agent token -> rc 0 (and seed an idempotency key to prove run_token/relayed cannot replay it!)
            resp = client.send_request({
                "v": 1, "cmd": cmd_name, "args": cmd_args,
                "idempotency_key": f"idem-{cmd_name}",
                "auth": {"type": "token", "token": tok_plain},
            })
            self.assertTrue(resp["ok"], (cmd_name, resp))
            self.assertEqual(resp["exit_code"], 0, cmd_name)

            # 4. Relayed agent token -> rc 4 RELAY_NOT_ALLOWED
            resp = client.send_request({
                "v": 1, "cmd": cmd_name, "args": cmd_args,
                "relayed": True, "user_statement": "please inspect adapter",
                "auth": {"type": "token", "token": tok_relay},
            })
            self.assertFalse(resp["ok"], (cmd_name, resp))
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_FORBIDDEN, c.RELAY_NOT_ALLOWED), cmd_name)

            # 5. Executor run_token (even with same principal_id + idempotency_key!) -> rc 4 CAPABILITY_MISSING
            resp = client.send_request({
                "v": 1, "cmd": cmd_name, "args": cmd_args,
                "idempotency_key": f"idem-{cmd_name}",
                "auth": {"type": "token", "token": rt_exec},
            })
            self.assertFalse(resp["ok"], (cmd_name, resp))
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_FORBIDDEN, c.CAPABILITY_MISSING), cmd_name)

            # 6. Current lead run_token -> rc 4 CAPABILITY_MISSING
            resp = client.send_request({
                "v": 1, "cmd": cmd_name, "args": cmd_args,
                "auth": {"type": "token", "token": rt_lead},
            })
            self.assertFalse(resp["ok"], (cmd_name, resp))
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_FORBIDDEN, c.CAPABILITY_MISSING), cmd_name)

        # 7. Supersede ag-lead1 -> superseded lead run_token MUST get rc 9 LEAD_SUPERSEDED (even with binary arg!)
        rc, r = h.setup_cli("project", "set-lead", prj_id, "ag-lead2", "--reason", "rotate", user=True)
        self.assertEqual(rc, 0, r)
        for cmd_name, cmd_args in cmds + [("adapter.probe", {"adapter_id": "kiro", "binary": "/nonexistent"})]:
            resp = client.send_request({
                "v": 1, "cmd": cmd_name, "args": cmd_args,
                "auth": {"type": "token", "token": rt_lead},
            })
            self.assertFalse(resp["ok"], (cmd_name, resp))
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_STALE, c.LEAD_SUPERSEDED), cmd_name)

        # 8. Idempotency replay cannot bypass central check_authz for run tokens sharing principal_id:
        resp_seed = client.send_request({
            "v": 1, "cmd": "adapter.list", "args": {},
            "idempotency_key": "idem-r11-adapter-authz",
            "auth": {"type": "token", "token": tok_plain},
        })
        self.assertTrue(resp_seed["ok"], resp_seed)
        resp_replay_exec = client.send_request({
            "v": 1, "cmd": "adapter.list", "args": {},
            "idempotency_key": "idem-r11-adapter-authz",
            "auth": {"type": "token", "token": rt_exec},
        })
        self.assertFalse(resp_replay_exec["ok"], resp_replay_exec)
        self.assertEqual(
            (resp_replay_exec["exit_code"], resp_replay_exec["error"]["code"]),
            (c.EXIT_FORBIDDEN, c.CAPABILITY_MISSING),
        )

    def test_r11_adapter_binary_flag_user_only_on_probe_and_rejected_elsewhere(self):
        import mesh.constants as c
        from mesh.client import MeshClient

        h = self.harness
        rc, r = h.setup_cli("agent", "create", "--name", "ag-bin", "--adapter", "fake", "--max-concurrency", "1",
                            "--allowed-model", "m1", "--default-model", "m1", user=True)
        self.assertEqual(rc, 0, r)
        rc, r = h.setup_cli("agent", "token", "issue", "ag-bin", user=True)
        self.assertEqual(rc, 0, r)
        tok_agent = r["data"]["token"]

        marker = os.path.join(self.stubs_dir, "custom_bin_ran.marker")
        custom_bin = os.path.join(self.stubs_dir, "custom-kiro")
        kiro_stub = os.path.join(self.stubs_dir, "kiro-cli")
        with open(kiro_stub, "r", encoding="utf-8") as f:
            stub_src = f.read()
        with open(custom_bin, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                f"open({marker!r}, 'w').write('ran')\n"
                + "\n".join(stub_src.splitlines()[1:]) + "\n"
            )
        os.chmod(custom_bin, 0o755)

        # 1. CLI `mesh adapter models kiro --binary ...` must be rejected by CLI parser with rc 2!
        rc, resp = h.cli("adapter", "models", "kiro", "--binary", custom_bin, user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertFalse(os.path.exists(marker), "adapter models --binary executed custom binary!")

        # 2. Raw daemon socket: binary / binary_path on adapter.{list,show,models} rejected with rc 2 INVALID_ARGUMENT
        client = MeshClient(h.home)
        for cmd_name, base_args in [
            ("adapter.list", {}),
            ("adapter.show", {"adapter_id": "kiro"}),
            ("adapter.models", {"adapter_id": "kiro"}),
        ]:
            for key in ("binary", "binary_path"):
                args = dict(base_args)
                args[key] = custom_bin
                resp = client.send_request({
                    "v": 1, "cmd": cmd_name, "args": args,
                    "auth": {"type": "user", "passphrase": h.passphrase},
                })
                self.assertFalse(resp["ok"], (cmd_name, key, resp))
                self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), (cmd_name, key))
        self.assertFalse(os.path.exists(marker), "disallowed binary param executed custom binary!")

        # 3. Agent token on `adapter.probe` with --binary / binary_path -> rc 4 CAPABILITY_MISSING, binary NOT executed!
        for key in ("binary", "binary_path"):
            resp = client.send_request({
                "v": 1, "cmd": "adapter.probe", "args": {"adapter_id": "kiro", key: custom_bin},
                "auth": {"type": "token", "token": tok_agent},
            })
            self.assertFalse(resp["ok"], (key, resp))
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_FORBIDDEN, c.CAPABILITY_MISSING), key)
        self.assertFalse(os.path.exists(marker), "agent token adapter.probe --binary executed custom binary!")

        # 4. Direct user on `adapter probe kiro --binary <custom_bin>` -> rc 0 and executes custom_bin!
        rc, resp = h.cli("adapter", "probe", "kiro", "--binary", custom_bin, user=True)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(os.path.exists(marker), "direct user adapter probe --binary did not run custom binary")

    def test_r11_adapter_probe_runs_off_event_loop_and_outside_db_tx(self):
        import threading
        from mesh.client import MeshClient

        h = self.harness
        rc, r = h.setup_cli("agent", "create", "--name", "ag-lp", "--adapter", "fake", "--max-concurrency", "1",
                            "--allowed-model", "m1", "--default-model", "m1", "--can-lead", user=True)
        self.assertEqual(rc, 0, r)
        started_flag = os.path.join(self.stubs_dir, "slow_probe_started")
        pid_file = os.path.join(self.stubs_dir, "slow_probe.pid")
        slow_bin = os.path.join(self.stubs_dir, "slow-kiro")
        kiro_stub = os.path.join(self.stubs_dir, "kiro-cli")
        with open(kiro_stub, "r", encoding="utf-8") as f:
            stub_lines = f.read().splitlines()[1:]
        with open(slow_bin, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import os, sys, time\n"
                "if '--version' in sys.argv:\n"
                f"    open({pid_file!r} + '.t', 'w').write(str(os.getpid()))\n"
                f"    os.rename({pid_file!r} + '.t', {pid_file!r})\n"
                f"    open({started_flag!r}, 'w').write('1')\n"
                "    time.sleep(0.8)\n"
                + "\n".join(stub_lines) + "\n"
            )
        os.chmod(slow_bin, 0o755)

        probe_out = {}

        def _run_probe():
            c_probe = MeshClient(h.home, timeout=15.0)
            probe_out["resp"] = c_probe.send_request({
                "v": 1,
                "cmd": "adapter.probe",
                "args": {"adapter_id": "kiro", "binary": slow_bin},
                "auth": {"type": "user", "passphrase": h.passphrase},
            })

        th = threading.Thread(target=_run_probe, daemon=True)
        th.start()
        try:
            self.assertTrue(_wait(lambda: os.path.exists(started_flag), 5.0), "slow probe never started")
            if os.path.exists(pid_file):
                with open(pid_file, "r", encoding="utf-8") as pf:
                    spid = int(pf.read().strip())
                sinfo = get_proc_stat(spid)
                if sinfo is not None:
                    self.spawned_pairs.append((spid, int(sinfo["starttime"])))

            # While slow_bin is mid-sleep (0.8s remaining), BOTH a status read, a direct SQLite
            # BEGIN IMMEDIATE write lock, AND a daemon DB write MUST complete promptly!
            c_fast = MeshClient(h.home, timeout=5.0)
            t0 = time.monotonic()
            ext_conn = sqlite3.connect(os.path.join(h.home, "mesh.db"), timeout=0.2, isolation_level=None)
            try:
                ext_conn.execute("BEGIN IMMEDIATE;")
                ext_conn.execute("ROLLBACK;")
            finally:
                ext_conn.close()
            st_resp = c_fast.send_request({"v": 1, "cmd": "daemon.status", "auth": {"type": "none"}})
            wr_resp = c_fast.send_request({
                "v": 1,
                "cmd": "project.create",
                "args": {"title": "concurrent-write", "goal": "non-blocking", "criterion": ["c1"], "lead": "ag-lp"},
                "auth": {"type": "user", "passphrase": h.passphrase},
            })
            elapsed = time.monotonic() - t0
        finally:
            th.join(timeout=10.0)

        self.assertTrue(st_resp.get("ok"), st_resp)
        self.assertTrue(wr_resp.get("ok"), wr_resp)
        self.assertLess(
            elapsed, 0.45,
            f"daemon.status + project.create blocked for {elapsed:.3f}s during 0.8s adapter.probe!",
        )
        self.assertTrue(probe_out.get("resp", {}).get("ok"), probe_out)

    def test_r11_adapter_dry_run_spawns_nothing_and_writes_no_rows(self):
        import mesh.constants as c
        from mesh.client import MeshClient

        h = self.harness
        rc, r = h.setup_cli("agent", "create", "--name", "ag-dry", "--adapter", "fake", "--max-concurrency", "1",
                            "--allowed-model", "m1", "--default-model", "m1", user=True)
        self.assertEqual(rc, 0, r)
        rc, r = h.setup_cli("agent", "token", "issue", "ag-dry", user=True)
        self.assertEqual(rc, 0, r)
        tok_agent = r["data"]["token"]

        # Replace stubs in stubs_dir with marker-writing wrappers so ANY subprocess execution is caught!
        exec_log = os.path.join(self.stubs_dir, "dry_run_exec.log")
        for bin_name in ("droid", "agy", "kiro-cli", "vibe"):
            p = os.path.join(self.stubs_dir, bin_name)
            with open(p, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()[1:]
            with open(p, "w", encoding="utf-8") as f:
                f.write(
                    "#!/usr/bin/env python3\n"
                    f"open({exec_log!r}, 'a').write({bin_name!r} + '\\n')\n"
                    + "\n".join(lines) + "\n"
                )
            os.chmod(p, 0o755)

        db_path = os.path.join(h.home, "mesh.db")
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            probes_before = conn.execute("SELECT COUNT(*) FROM adapter_probes;").fetchone()[0]
            idem_before = conn.execute("SELECT COUNT(*) FROM idempotency;").fetchone()[0]
        finally:
            conn.close()

        client = MeshClient(h.home)
        custom_bin = os.path.join(self.stubs_dir, "kiro-cli")
        for idx, (cmd_name, cmd_args) in enumerate([
            ("adapter.list", {}),
            ("adapter.show", {"adapter_id": "kiro"}),
            ("adapter.probe", {"adapter_id": "kiro", "binary": custom_bin}),
            ("adapter.models", {"adapter_id": "kiro"}),
        ]):
            resp = client.send_request({
                "v": 1,
                "cmd": cmd_name,
                "args": cmd_args,
                "dry_run": True,
                "idempotency_key": f"dry-key-{idx}",
                "auth": {"type": "user", "passphrase": h.passphrase},
            })
            self.assertTrue(resp["ok"], (cmd_name, resp))
            self.assertTrue(resp["dry_run"], (cmd_name, resp))
            self.assertIn("would", resp["data"], (cmd_name, resp))

        self.assertFalse(os.path.exists(exec_log), "dry_run spawned adapter subprocesses!")

        # Validation & authz still apply on --dry-run!
        resp_forbid = client.send_request({
            "v": 1,
            "cmd": "adapter.probe",
            "args": {"adapter_id": "kiro", "binary": custom_bin},
            "dry_run": True,
            "auth": {"type": "token", "token": tok_agent},
        })
        self.assertFalse(resp_forbid["ok"], resp_forbid)
        self.assertEqual((resp_forbid["exit_code"], resp_forbid["error"]["code"]), (c.EXIT_FORBIDDEN, c.CAPABILITY_MISSING))

        resp_bad = client.send_request({
            "v": 1,
            "cmd": "adapter.show",
            "args": {"adapter_id": "no-such-adapter"},
            "dry_run": True,
            "auth": {"type": "user", "passphrase": h.passphrase},
        })
        self.assertFalse(resp_bad["ok"], resp_bad)
        self.assertEqual(resp_bad["exit_code"], c.EXIT_USAGE)

        self.assertFalse(os.path.exists(exec_log), "dry_run spawned adapter subprocesses on rejected calls!")

        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            probes_after = conn.execute("SELECT COUNT(*) FROM adapter_probes;").fetchone()[0]
            idem_after = conn.execute("SELECT COUNT(*) FROM idempotency;").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(probes_after, probes_before, "dry_run wrote adapter_probes rows!")
        self.assertEqual(idem_after, idem_before, "dry_run wrote idempotency rows!")


class TestClickUpP1ScanUncertain(unittest.TestCase):
    """ACC: N18, A8 (ClickUp P1-1: /proc scan errors must report uncertainty, never empty)."""

    def setUp(self):
        try:
            import sitecustomize  # type: ignore
            if hasattr(sitecustomize, "_bad"):
                sitecustomize._bad()
        except ImportError:
            pass
        self.tmp = tempfile.mkdtemp(prefix="mh-gf-cup1-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "logs"))
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)
        self.run_id = f"run_cup1_{os.getpid()}_{time.monotonic_ns() % 1000000}"
        self.task_id = "tsk_cup1"
        self.run_dir = os.path.join(self.tmp, "run")
        os.makedirs(self.run_dir)
        self.addCleanup(self._forget)
        self._forget()

    def _forget(self):
        supervisor._CLOSE_REFUSED_LOGGED.pop(self.run_id, None)
        terminate.forget(self.run_id)
        supervisor._CLEAR.pop(self.run_id, None)
        supervisor._evict_seen(set())
        supervisor._SEEN_EVENT_PENDING.pop(self.run_id, None)
        supervisor._SEEN_EVENT_DONE.discard(self.run_id)

    def _rows(self, wrapper, wst):
        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_c','agent','c',?);", (now,))
        ex("INSERT INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
           "VALUES ('agt_c','fake','launch',1,?,?);", (now, now))
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_c','p','g','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
           "timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
           "VALUES (?,'prj_c','t','s','general','running','agt_c','edit',600,1,1,'usr',?,?);",
           (self.task_id, now, now))
        ex("INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
           "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
           "VALUES (?,'task',?,'prj_c','agt_c',1,1,'fake','running','boot_old',?,?,?,?,?);",
           (self.run_id, self.task_id, wrapper, wrapper, wst, self.run_dir, now))
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, "
           "expires_at, heartbeat_at) VALUES (?,?,'meshd','boot_old','agt_c',1,?,?,?);",
           (f"task:{self.task_id}", self.run_id, now, now_iso_utc(time.time() + 3600), now))
        self.conn.commit()

    def _merge_rows(self):
        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_mc','p','g','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
           "attempt, merge_json, created_by, created_at, updated_at) "
           "VALUES ('tsk_mc','prj_mc','t','s','code','merging','edit',600,1,1,?,'usr',?,?);",
           (json.dumps({"run_id": self.run_id}), now, now))
        ex("INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, boot_id, run_dir, "
           "started_at) VALUES (?,'merge_test','tsk_mc','prj_mc',1,1,'none','running','boot_old',?,?);",
           (self.run_id, self.run_dir, now))
        key = merge.merge_lease_key("prj_mc")
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
           "VALUES (?,?,'meshd','boot_old',1,?,?,?);",
           (key, self.run_id, now, now_iso_utc(time.time() + 3600), now))
        self.conn.commit()
        return key

    def _status(self):
        return self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0]

    def _lease(self):
        return self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (self.run_id,)).fetchone()

    def _spawn_runwrap_with_envless_member(self):
        """Real mesh.runwrap session leader + env={} same-group member."""
        from mesh.core.proc import get_proc_environ

        trigger = os.path.join(self.tmp, f"trig_{time.monotonic_ns()}")
        pidfile = os.path.join(self.tmp, f"mem_{time.monotonic_ns()}.pid")
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cmd = (f"env -i {shutil.which('sleep')} 120 </dev/null >/dev/null 2>&1 & "
               f"echo $! > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}; "
               f"while [ ! -e {trigger} ]; do sleep 0.02; done; exit 0")
        with open(os.path.join(self.run_dir, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"argv": ["sh", "-c", cmd], "cwd": self.run_dir, "stdin": "devnull"}, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        env["PYTHONPATH"] = f"/home/node/gf-guard:{root}" if os.path.isdir("/home/node/gf-guard") else root
        launcher = subprocess.run(
            [sys.executable, "-c",
             "import os, subprocess, sys\n"
             "p = subprocess.Popen([sys.executable, '-P', '-s', '-m', 'mesh.runwrap', sys.argv[1]],"
             " start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
             " stderr=subprocess.DEVNULL)\nprint(p.pid, flush=True)\nos._exit(0)\n", self.run_dir],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=30, check=True,
        )
        wrapper = int(launcher.stdout.decode().split()[0])
        wpath = os.path.join(self.run_dir, "wrapper.json")
        self.assertTrue(_wait(lambda: os.path.exists(wpath) and os.path.exists(pidfile), 15),
                        "runwrap/member did not start")
        with open(pidfile, "r", encoding="utf-8") as pf:
            member = int(pf.read().strip())
        self.assertTrue(_wait(lambda: get_proc_environ(member) == {}, 10), "member did not clear env")
        wst = int(get_proc_stat(wrapper)["starttime"])
        mst = int(get_proc_stat(member)["starttime"])
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(_kill_same, member, mst)
        self.addCleanup(lambda: [_kill_same(p, st) for p, st in supervisor._scan_group(wrapper).items()])
        self.assertEqual(get_proc_stat(member)["pgrp"], wrapper)
        return trigger, wrapper, wst, member, mst

    def _repro_task_close_gate(self, make_patch):
        """Wrapper exited & reaped, env-less same-group member NOT in seen.json, scan error injected."""
        from unittest import mock

        trigger, wrapper, wst, member, mst = self._spawn_runwrap_with_envless_member()
        self._rows(wrapper, wst)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        # seen.json records ONLY the wrapper (member spawned between 5-tick rescans).
        self.assertTrue(supervisor._persist_seen(run, {wrapper: wst}, self.home))
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15), "wrapper did not exit")
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "exit.json")))
        self.assertTrue(_alive(member, mst))

        sent_signals = []
        real_kill, real_killpg = os.kill, os.killpg

        def track_kill(pid, sig):
            if sig != 0 and (pid == member or pid == -wrapper):
                sent_signals.append(("kill", pid, sig))
            return real_kill(pid, sig)

        def track_killpg(pgid, sig):
            if sig != 0 and pgid == wrapper:
                sent_signals.append(("killpg", pgid, sig))
            return real_killpg(pgid, sig)

        active = {"on": True, "hits": 0}
        ctx = make_patch(member, active)
        with ctx, mock.patch("os.kill", track_kill), mock.patch("os.killpg", track_killpg), \
                mock.patch.object(supervisor, "_log_meshd"):
            # Full watch + confirm_pending_exits ticks across POLL_S must keep run & lease held.
            supervisor.watch_launch_runs(self.conn, self.home)
            time.sleep(terminate.POLL_S + 0.05)
            supervisor.advance_terminations(self.conn, self.home)
            supervisor.watch_launch_runs(self.conn, self.home)

            self.assertGreaterEqual(active["hits"], 1, "fault injection never fired")
            self.assertEqual(self._status(), "running", "run closed while scan uncertain and member alive")
            self.assertIsNotNone(self._lease(), "task lease released while scan uncertain and member alive")
            self.assertTrue(_alive(member, mst), "member died unexpectedly")
            self.assertEqual(sent_signals, [], f"uncertain scan caused blind signal: {sent_signals}")

            # Direct close_gate must also refuse with occupied=True, live={}.
            with self.assertRaises(supervisor.CloseHeld) as cm:
                supervisor.close_gate(run, "exited")
            self.assertTrue(cm.exception.occupied)
            self.assertEqual(cm.exception.live, {})

            # Injection ends while member still lives: group is now visibly occupied, still held & unsignalled.
            active["on"] = False
            supervisor.watch_launch_runs(self.conn, self.home)
            time.sleep(terminate.POLL_S + 0.05)
            supervisor.advance_terminations(self.conn, self.home)
            self.assertEqual(self._status(), "running", "run closed while member still alive")
            self.assertIsNotNone(self._lease(), "lease released while member still alive")
            self.assertEqual(sent_signals, [], f"unrecorded member blind-signalled: {sent_signals}")

        # Member exits: next watch + confirmation closes the run and releases task:<id>.
        _kill_same(member, mst)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10), "member did not die")
        with mock.patch.object(supervisor, "_log_meshd"):
            for _ in range(6):
                supervisor.watch_launch_runs(self.conn, self.home)
                supervisor.advance_terminations(self.conn, self.home)
                if self._status() not in ("starting", "running"):
                    break
                time.sleep(terminate.POLL_S + 0.03)
        self.assertEqual(self._status(), "exited")
        self.assertIsNone(self._lease())

    def test_task_close_gate_eacces_holds_until_member_gone(self):
        import errno
        from unittest import mock

        real_sup_stat = supervisor.get_proc_stat

        def make_patch(member, active):
            def stat(pid):
                if active["on"] and int(pid) == member:
                    active["hits"] += 1
                    raise PermissionError(errno.EACCES, "injected EACCES", f"/proc/{pid}/stat")
                return real_sup_stat(pid)

            return mock.patch.object(supervisor, "get_proc_stat", stat)

        self._repro_task_close_gate(make_patch)

    def test_task_close_gate_eio_holds_until_member_gone(self):
        import errno
        from unittest import mock

        real_sup_stat = supervisor.get_proc_stat
        real_sup_alive = supervisor.is_tgid_alive

        def make_patch(member, active):
            @contextlib.contextmanager
            def both():
                def stat(pid):
                    if active["on"] and int(pid) == member and active["hits"] % 2 == 0:
                        active["hits"] += 1
                        raise OSError(errno.EIO, "injected stat EIO", f"/proc/{pid}/stat")
                    return real_sup_stat(pid)

                def alive(pid, info=None):
                    if active["on"] and int(pid) == member:
                        active["hits"] += 1
                        raise OSError(errno.EIO, "injected tgid EIO", f"/proc/{pid}/task")
                    return real_sup_alive(pid, info)

                with mock.patch.object(supervisor, "get_proc_stat", stat), \
                        mock.patch.object(supervisor, "is_tgid_alive", alive):
                    yield

            return both()

        self._repro_task_close_gate(make_patch)

    def test_task_close_gate_malformed_stat_holds_until_member_gone(self):
        import builtins
        import io
        from unittest import mock

        real_open = builtins.open

        def make_patch(member, active):
            target = f"/proc/{member}/stat"

            def corrupt_open(file, *args, **kwargs):
                if active["on"] and file == target and sys._getframe(2).f_code.co_name != "_alive":
                    active["hits"] += 1
                    return io.StringIO("corrupt_stat_missing_rparen 1 2 3\n")
                return real_open(file, *args, **kwargs)

            return mock.patch("builtins.open", corrupt_open)

        self._repro_task_close_gate(make_patch)

    def test_task_close_gate_emfile_listdir_holds_until_member_gone(self):
        import errno
        from unittest import mock

        real_listdir = os.listdir

        def make_patch(_member, active):
            def listdir(path="."):
                if active["on"] and path == "/proc":
                    caller = sys._getframe(1).f_code.co_name
                    if caller in ("_scan_group", "run_targets"):
                        active["hits"] += 1
                        raise OSError(errno.EMFILE, "Too many open files", "/proc")
                return real_listdir(path)

            return mock.patch("os.listdir", listdir)

        self._repro_task_close_gate(make_patch)

    def test_adopt_and_recovery_lost_scan_error_never_trusts_seen_or_loses_run(self):
        import errno
        from unittest import mock

        # Part A: adoption scan hits EACCES on member -> adopted_uncertain (never trusts partial seen.json).
        trigger, wrapper, wst, member, mst = self._spawn_runwrap_with_envless_member()
        self._rows(wrapper, wst)
        real_sup_stat = supervisor.get_proc_stat
        active = {"on": True, "hits": 0}

        def stat_eacces(pid):
            if active["on"] and int(pid) == member:
                active["hits"] += 1
                raise PermissionError(errno.EACCES, "injected EACCES", f"/proc/{pid}/stat")
            return real_sup_stat(pid)

        with mock.patch.object(supervisor, "get_proc_stat", stat_eacces), \
                mock.patch.object(supervisor, "_log_meshd"):
            out = recovery._recover_run(self.conn, self.home,
                                        {"id": self.run_id, "lease_holder": "boot_old"}, "boot_new")
            self.assertEqual(out, "adopted_uncertain")
            self.assertGreaterEqual(active["hits"], 1)
            with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
                doc = json.load(f)
            self.assertEqual((doc.get("mesh_seen_marker"), doc.get("reason")),
                             ("unusable", "adopt_scan_untrusted"), doc)
            # Next watch tick while still failing keeps the unusable marker and resets tick for immediate retry.
            supervisor.watch_launch_runs(self.conn, self.home)
            with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
                doc2 = json.load(f)
            self.assertEqual(doc2.get("mesh_seen_marker"), "unusable", doc2)

            # Once EACCES clears while wrapper lives, the VERY NEXT watch tick rescans and records member!
            active["on"] = False
            supervisor.watch_launch_runs(self.conn, self.home)
            with open(os.path.join(self.run_dir, "seen.json"), "r", encoding="utf-8") as f:
                doc3 = json.load(f)
            self.assertNotIn("mesh_seen_marker", doc3, doc3)
            self.assertIn({"pid": member, "starttime": mst}, doc3["members"])

        # Part B: recovery._lost (wrapper dead, no exit.json, no seen.json) with EIO on member holds run.
        os.kill(wrapper, signal.SIGKILL)
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))
        exit_path = os.path.join(self.run_dir, "exit.json")
        if os.path.exists(exit_path):
            os.unlink(exit_path)
        os.unlink(os.path.join(self.run_dir, "seen.json"))
        self.conn.execute("DELETE FROM meta WHERE key = ?;", (f"seen_hold:{self.run_id}",))
        self.conn.commit()
        self._forget()

        active = {"on": True, "hits": 0}

        def stat_eio(pid):
            if active["on"] and int(pid) == member:
                active["hits"] += 1
                raise OSError(errno.EIO, "injected EIO", f"/proc/{pid}/stat")
            return real_sup_stat(pid)

        with mock.patch.object(supervisor, "get_proc_stat", stat_eio), \
                mock.patch.object(supervisor, "_log_meshd"):
            summary = recovery.recover(self.conn, self.home, "boot_lost_eio")
        self.assertGreaterEqual(active["hits"], 1)
        self.assertEqual((summary["runs_lost"], summary["runs_terminating"]), (0, 1), summary)
        self.assertEqual(self._status(), "running")
        self.assertIsNotNone(self._lease())
        self.assertTrue(_alive(member, mst))

    def test_run_targets_scan_error_uncertain_holds_exit_and_termination(self):
        import errno
        from unittest import mock

        # Real session-leader wrapper held unreaped + env-less same-group member.
        mem_pidfile = os.path.join(self.tmp, "rt_mem.pid")
        cmd = (f"env -i {shutil.which('sleep')} 120 </dev/null >/dev/null 2>&1 & "
               f"echo $! > {mem_pidfile}.tmp && mv {mem_pidfile}.tmp {mem_pidfile}; exit 0")
        wp = subprocess.Popen(["sh", "-c", cmd], start_new_session=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.register(wp.pid, "wrapper")
        children.hold(wp.pid)

        def cleanup_wp():
            children.unregister(wp.pid)
            children.unhold(wp.pid)
            if wp.poll() is None:
                wp.kill()
            wp.wait()

        self.addCleanup(cleanup_wp)
        wst = int(get_proc_stat(wp.pid)["starttime"])
        self.assertTrue(_wait(lambda: os.path.exists(mem_pidfile), 10))
        with open(mem_pidfile, "r", encoding="utf-8") as mf:
            member = int(mf.read().strip())
        mst = int(get_proc_stat(member)["starttime"])
        self.addCleanup(_kill_same, member, mst)
        self.assertTrue(_wait(lambda: (get_proc_stat(wp.pid) or {}).get("state") == "Z", 10))

        real_term_stat = terminate.get_proc_stat
        active = {"on": True, "hits": 0}

        def stat_eacces(pid):
            if active["on"] and int(pid) == member:
                active["hits"] += 1
                raise PermissionError(errno.EACCES, "injected EACCES", f"/proc/{pid}/stat")
            return real_term_stat(pid)

        run_id = f"run_rt_{os.getpid()}"
        self.addCleanup(terminate.forget, run_id)
        with mock.patch.object(terminate, "get_proc_stat", stat_eacces):
            found = terminate.run_targets(run_id, wp.pid, wp.pid, wst)
            self.assertEqual(found, {})
            self.assertTrue(getattr(found, "uncertain", False), "run_targets did not report uncertainty on EACCES")

            chk = terminate.ClearCheck()
            self.assertFalse(chk.observe(found, now=100.0))
            self.assertFalse(chk.observe(found, now=100.0 + 2 * terminate.POLL_S))

            rechecked = terminate.run_targets_or_recheck(run_id, wp.pid, wp.pid, wst)
            self.assertEqual(rechecked, {})
            self.assertTrue(getattr(rechecked, "uncertain", False),
                            "run_targets_or_recheck dropped uncertainty")

            t = terminate.begin(run_id, wp.pid, wp.pid, "leftovers", grace_s=5.0, wrapper_starttime=wst)
            self.assertEqual(terminate.step(t), "pending")
            time.sleep(terminate.POLL_S + 0.05)
            self.assertEqual(terminate.step(t), "pending", "terminate.step finished 'done' on uncertain scan!")
            self.assertTrue(_alive(member, mst))

            # Clear injection: terminate.step now sees member, kills it, and reaches 'done'.
            active["on"] = False
            deadline = time.monotonic() + 10.0
            outcome = "pending"
            while time.monotonic() < deadline and outcome == "pending":
                outcome = terminate.step(t)
                if outcome == "pending":
                    time.sleep(0.05)
            self.assertEqual(outcome, "done")
            self.assertFalse(_alive(member, mst))

    def test_merge_close_gate_scan_error_holds_run_and_lease(self):
        import errno
        from unittest import mock

        # merge_close_gate (via recovery.recover) with EACCES on unrecorded same-group member -> held.
        trigger, wrapper, wst, member, mst = self._spawn_runwrap_with_envless_member()
        key = self._merge_rows()
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wrapper, wst), 15))

        real_sup_stat = supervisor.get_proc_stat
        real_term_stat = terminate.get_proc_stat
        active = {"on": True, "sup_hits": 0, "term_hits": 0}

        def sup_stat(pid):
            if active["on"] and int(pid) == member:
                active["sup_hits"] += 1
                raise PermissionError(errno.EACCES, "injected EACCES", f"/proc/{pid}/stat")
            return real_sup_stat(pid)

        def term_stat(pid):
            if active["on"] and int(pid) == member:
                active["term_hits"] += 1
                raise PermissionError(errno.EACCES, "injected EACCES", f"/proc/{pid}/stat")
            return real_term_stat(pid)

        lease = lambda: self.conn.execute(
            "SELECT 1 FROM leases WHERE key = ? AND run_id = ?;", (key, self.run_id)
        ).fetchone()

        # 1) merge_close_gate inside recovery.recover (supervisor._scan_group EACCES) -> merge_runs_held=1.
        with mock.patch.object(supervisor, "get_proc_stat", sup_stat), \
                mock.patch.object(supervisor, "_log_meshd"):
            summary = recovery.recover(self.conn, self.home, "boot_m_gate_eacces")
        self.assertGreaterEqual(active["sup_hits"], 1)
        self.assertEqual((summary["merge_runs_held"], summary["merge_runs_lost"]), (1, 0), summary)
        self.assertEqual(self._status(), "running")
        self.assertIsNotNone(lease(), "merge:<prj> lease released while merge_close_gate scan uncertain")
        self.assertTrue(_alive(member, mst))

        # 2) recover_merge_runs carrier scan EACCES (terminate.run_targets_or_recheck) -> also held.
        with mock.patch.object(supervisor, "get_proc_stat", sup_stat), \
                mock.patch.object(terminate, "get_proc_stat", term_stat), \
                mock.patch.object(supervisor, "_log_meshd"):
            summary_c = recovery.recover(self.conn, self.home, "boot_m_carrier_eacces")
        self.assertGreaterEqual(active["term_hits"], 1)
        self.assertEqual(summary_c["merge_runs_lost"], 0, summary_c)
        self.assertEqual(self._status(), "running")
        self.assertIsNotNone(lease(), "merge:<prj> lease released while carrier scan uncertain")

        # Direct merge_close_gate also raises MergeRunHeld while scan is uncertain.
        run_row = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        with mock.patch.object(supervisor, "get_proc_stat", sup_stat):
            with self.assertRaises(merge.MergeRunHeld):
                merge.merge_close_gate(self.conn, run_row, None, None)

        # Kill member and recover cleanly -> closes lost and releases merge:<prj>.
        _kill_same(member, mst)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))
        self._forget()
        with mock.patch.object(supervisor, "_log_meshd"):
            summary2 = recovery.recover(self.conn, self.home, "boot_m_clean")
        self.assertEqual((summary2["merge_runs_held"], summary2["merge_runs_lost"]), (0, 1), summary2)
        self.assertEqual(self._status(), "lost")
        self.assertIsNone(lease())

    def test_merge_test_step_leftover_scan_error_not_passed(self):
        import errno
        from unittest import mock

        # merge.run_test_step leftover scan with EIO on unrecorded same-group member -> passed is False.
        cwd = os.path.join(self.tmp, "integ")
        os.makedirs(cwd)
        real_term_stat = terminate.get_proc_stat

        def run_case(max_eio_hits):
            pidfile = os.path.join(self.tmp, f"mstep_bg_{max_eio_hits}.pid")
            step_run_id = f"run_mstep_{os.getpid()}_{max_eio_hits}"
            self.addCleanup(terminate.forget, step_run_id)
            cmd = (f"env -i {shutil.which('sleep')} 120 </dev/null >/dev/null 2>&1 & "
                   f"echo $! > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}; exit 0")
            plan = {
                "run_id": step_run_id,
                "run_dir": os.path.join(self.tmp, "merges", f"mrg_cup1_{max_eio_hits}"),
                "integration": cwd,
                "test_argv": ["sh", "-c", cmd],
                "test_timeout_s": 60,
                "kill_grace_s": 0.3,
            }
            bg_holder = {"hits": 0}
            self.addCleanup(lambda: bg_holder.get("pid") and _kill_same(bg_holder["pid"], bg_holder["st"]))

            def step_term_stat(pid):
                if os.path.exists(pidfile):
                    try:
                        with open(pidfile, "r", encoding="utf-8") as pf:
                            bg_pid = int(pf.read().strip())
                    except ValueError:
                        bg_pid = -1
                    if int(pid) == bg_pid:
                        info = real_term_stat(bg_pid)
                        if info is not None and "pid" not in bg_holder:
                            bg_holder.update(pid=bg_pid, st=int(info["starttime"]))
                        if max_eio_hits is None or bg_holder["hits"] < max_eio_hits:
                            bg_holder["hits"] += 1
                            raise OSError(errno.EIO, "injected EIO on leftover scan", f"/proc/{pid}/stat")
                return real_term_stat(pid)

            with mock.patch.object(terminate, "get_proc_stat", step_term_stat), \
                    mock.patch.object(terminate, "KILL_WAIT_S", 0.3):
                out = merge.run_test_step(plan)
            if "pid" not in bg_holder and os.path.exists(pidfile):
                with open(pidfile, "r", encoding="utf-8") as pf:
                    bg_pid = int(pf.read().strip())
                info = get_proc_stat(bg_pid)
                if info is not None:
                    bg_holder.update(pid=bg_pid, st=int(info["starttime"]))
            return out, bg_holder

        # 1) Transient EIO across run_targets_or_recheck (2 scans): wrapper stays unreaped
        #    during _terminate_test, member is terminated once EIO clears, step still not passed.
        out_trans, bg_trans = run_case(2)
        self.assertFalse(out_trans["passed"], f"merge test step passed despite transient EIO: {out_trans}")
        self.assertEqual(out_trans.get("error"), "leftover_scan_uncertain", out_trans)
        self.assertFalse(_alive(bg_trans["pid"], bg_trans["st"]), "leftover survived transient scan EIO")

        # 2) Persistent EIO: _terminate_test reaches kill_failed without blind-signalling; not passed.
        out_pers, bg_pers = run_case(None)
        self.assertFalse(out_pers["passed"], f"merge test step passed despite persistent EIO: {out_pers}")
        self.assertEqual(out_pers.get("error"), "leftover_scan_uncertain", out_pers)
        self.assertTrue(out_pers["termination"]["kill_failed"], out_pers)
        self.assertTrue(out_pers["termination"].get("scan_uncertain"), out_pers)
        self.assertTrue(_alive(bg_pers["pid"], bg_pers["st"]))


class TestClickUpP1SeenHoldUncertain(unittest.TestCase):
    """ACC: N18, A8 (ClickUp P1-1, Quest verification: the seen hold reads an uncertain
    group scan as occupied, so an unusable-seen adopted run never closes on a scan error)."""

    def test_seen_hold_uncertain_scan_counts_as_occupied(self):
        from unittest import mock
        from mesh.core.proc import ScanResult
        run = {"id": "run-cu-hold", "pgid": 424242}
        snap = supervisor.SeenSnapshot({}, True, False)
        with mock.patch.object(supervisor, "is_adopted", return_value=True), \
                mock.patch.object(supervisor, "wrapper_alive", return_value=False), \
                mock.patch.object(supervisor, "_scan_group",
                                  return_value=ScanResult(uncertain=True)) as scan:
            self.assertTrue(supervisor._group_occupied(424242))
            self.assertTrue(supervisor.snapshot_hold(run, snap))
            scan.assert_called_with(424242)
        with mock.patch.object(supervisor, "is_adopted", return_value=True), \
                mock.patch.object(supervisor, "wrapper_alive", return_value=False), \
                mock.patch.object(supervisor, "_scan_group", return_value=ScanResult()):
            self.assertFalse(supervisor.snapshot_hold(run, snap))


class TestClickUpRecheckMergeUnknownIdentityUncertain(unittest.TestCase):
    """ACC: N18, A8 (ClickUp recheck P2): merge test step with unknown wrapper identity and an
    empty but uncertain group scan (EMFILE on /proc). A transient error is rescanned and the
    same-group member terminated; a persistent one is reported as kill_failed + scan_uncertain
    (never a pass, never "no members")."""

    def _run(self, emfile_hits):
        import errno
        from unittest import mock
        tmp = tempfile.mkdtemp(prefix="mh-cu-rc-", dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.path.join(tmp, "integ")
        os.makedirs(cwd)
        pidfile = os.path.join(tmp, "bg.pid")
        run_id = f"run_curc{os.getpid()}{emfile_hits}"
        self.addCleanup(terminate.forget, run_id)
        cmd = (f"env -i {shutil.which('sleep')} 60 </dev/null >/dev/null 2>&1 & "
               f"echo $! > {pidfile}; sleep 0.3; exit 0")
        plan = {"run_id": run_id, "run_dir": os.path.join(tmp, "merges", "mrg_curc"), "integration": cwd,
                "test_argv": ["sh", "-c", cmd], "test_timeout_s": 60, "kill_grace_s": 1}
        real_stat = merge.get_proc_stat
        real_scan = supervisor._scan_group
        real_listdir = os.listdir
        real_read = merge._read_json
        state = {"first": True, "emfile": 0}

        def first_none(pid):
            if state["first"]:
                state["first"] = False
                return None
            return real_stat(pid)

        def listdir_emfile(path="."):
            if path == "/proc" and (emfile_hits is None or state["emfile"] < emfile_hits):
                state["emfile"] += 1
                raise OSError(errno.EMFILE, "injected EMFILE", "/proc")
            return real_listdir(path)

        def scan_emfile(pgid):
            # EMFILE only inside the identity-unknown group scan, never in run_targets.
            with mock.patch.object(supervisor.os, "listdir", listdir_emfile):
                return real_scan(pgid)

        with mock.patch.object(merge, "get_proc_stat", first_none), \
                mock.patch.object(merge, "_read_json",
                                  lambda path: None if path.endswith("wrapper.json") else real_read(path)), \
                mock.patch.object(supervisor, "_scan_group", scan_emfile):
            out = merge.run_test_step(plan)
        bg_pid = int(open(pidfile).read().strip())
        info = get_proc_stat(bg_pid)
        if info is not None and info["state"] != "Z":
            self.addCleanup(_kill_same, bg_pid, info["starttime"])
        self.assertGreaterEqual(state["emfile"], 1, "EMFILE was not injected")
        self.assertIsNone(out["wrapper"]["starttime"], out)
        self.assertEqual(out.get("error"), "wrapper_identity_unknown", out)
        self.assertFalse(out["passed"], out)
        self.assertIn("termination", out)
        return out, info

    def test_merge_step_unknown_identity_transient_emfile_rescans_and_terminates(self):
        out, info = self._run(2)
        self.assertEqual(out.get("leftovers"), 1, out)
        self.assertNotIn("leftover_scan_uncertain", out)
        self.assertTrue(info is None or info["state"] == "Z", f"member survived a transient EMFILE: {out}")

    def test_merge_step_unknown_identity_persistent_emfile_is_kill_failed_uncertain(self):
        out, _info = self._run(None)
        self.assertTrue(out.get("leftover_scan_uncertain"), out)
        self.assertTrue(out["termination"].get("kill_failed"), out)
        self.assertTrue(out["termination"].get("scan_uncertain"), out)


class TestCarbonRoP1b(unittest.TestCase):
    """ACC: N18, A8 (Carbon RO review m3-clickup-p1-carbon.md, round p1b):
    - P1-1: live task.merge holds open merge_test + merge:<prj> on uncertain scan (2A/2B) and
      kills live runwrap + test child directly when /proc fails on timeout (2C).
    - P1-2: terminate.run_targets marks same-uid EACCES/EPERM on /proc/<pid>/environ uncertain
      for in-run carriers without wedging on older host bystanders (3A/3B), and propagates
      _same_uid/_is_kthread/_now_ticks OSError into ScanResult.uncertain (3C).
    - P1-3: supervisor.wrapper_liveness tri-state and recovery._identity_alive preserve live
      runs on transient /proc/<wrapper>/stat OSError while keeping deadline enforcement.
    """

    def setUp(self):
        try:
            import sitecustomize  # type: ignore
            if hasattr(sitecustomize, "_bad"):
                sitecustomize._bad()
        except ImportError:
            pass
        self.tmp = tempfile.mkdtemp(prefix="mh-gf-p1b-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "logs"))
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)
        self.run_id = f"run_p1b_{os.getpid()}_{time.monotonic_ns() % 1000000}"
        self.task_id = "tsk_p1b"
        self.run_dir = os.path.join(self.tmp, "run")
        os.makedirs(self.run_dir)
        self.addCleanup(self._forget)
        self._forget()

    def _forget(self):
        supervisor._CLOSE_REFUSED_LOGGED.pop(self.run_id, None)
        terminate.forget(self.run_id)
        supervisor._CLEAR.pop(self.run_id, None)
        supervisor._evict_seen(set())
        supervisor._SEEN_EVENT_PENDING.pop(self.run_id, None)
        supervisor._SEEN_EVENT_DONE.discard(self.run_id)

    def _setup_live_merge(self, test_argv, test_timeout_s=30, kill_wait_s=None):
        from types import SimpleNamespace
        from unittest import mock
        import mesh.constants as c
        from mesh.core.gitops import run_git
        from tests.gitfixture import GitFixture

        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        # Suite margin (Quest p1b review): an uncertain termination waits KILL_WAIT_S (10 s);
        # 1 s keeps the same paths and saves ~20 s per suite (kill.grace_s is already 1 below).
        # Opt-in per test: 2a's later recovery assertions depend on the full wait.
        if kill_wait_s is not None:
            kw_patch = mock.patch.object(terminate, "KILL_WAIT_S", kill_wait_s)
            kw_patch.start()
            self.addCleanup(kw_patch.stop)
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[key]
        os.environ.update(fixture.git_env_vars())
        repo = fixture.make_k0()

        ws_root = merge.get_workspaces_root(self.conn, self.home)
        integ = merge.integration_worktree_path(ws_root, "prj_m")
        os.makedirs(os.path.dirname(integ), exist_ok=True)
        cp = run_git(["worktree", "add", "-b", "mesh/prj_m/integration", integ, "main"], cwd=repo)
        self.assertEqual(cp.returncode, 0, cp.stderr)

        wt = os.path.join(self.tmp, "wt_tsk_m")
        cp = run_git(["worktree", "add", "-b", "mesh/prj_m/tsk_m", wt, "main"], cwd=repo)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        with open(os.path.join(wt, "p1b.txt"), "w", encoding="utf-8") as f:
            f.write("p1b\n")
        subprocess.run(["git", "add", "p1b.txt"], cwd=wt, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "p1b change"], cwd=wt, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_m','user','u',?);", (now,))
        ex("INSERT OR REPLACE INTO settings (key, value_json, updated_by, updated_at) "
           "VALUES ('kill.grace_s', '1', 'usr_m', ?);", (now,))
        ex("INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
           "test_command_json, test_timeout_s, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_m','PM','g','active',?,'main','mesh/prj_m/integration',?,?, 'usr_m','cli',?,?);",
           (repo, json.dumps(test_argv), test_timeout_s, now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
           "attempt, fence_seq, workspace_path, branch, created_by, created_at, updated_at) "
           "VALUES ('tsk_m','prj_m','tm','s','code','accepted','edit',600,1,1,1,?,'mesh/prj_m/tsk_m','usr_m',?,?);",
           (wt, now, now))
        self.conn.commit()

        logs = []

        async def _noop():
            return None

        server = SimpleNamespace(
            notify_waiters=_noop,
            wake_scheduler=lambda: None,
            log_error=lambda msg: logs.append(msg),
        )
        ctx = SimpleNamespace(
            conn=self.conn, home=self.home, args={"task_id": "tsk_m"}, dry_run=False,
            principal={"id": "usr_m"}, actor_kind="user", via="direct", request_id="req_p1b",
            relayed_by=None, credential_id=None, k_user=None, idempotency=None,
        )
        return server, ctx, logs, c

    def _spawn_runwrap(self, cmd, boot_id="boot_old", run_id=None, task_status="running", deadline_at=None):
        rid = run_id or self.run_id
        rdir = os.path.join(self.tmp, f"rdir_{rid}")
        os.makedirs(rdir, exist_ok=True)
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(rdir, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"argv": ["sh", "-c", cmd], "cwd": rdir, "stdin": "devnull"}, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        env["MESH_RUN_ID"] = rid
        env["PYTHONPATH"] = f"/home/node/gf-guard:{root}" if os.path.isdir("/home/node/gf-guard") else root
        launcher = subprocess.run(
            [sys.executable, "-c",
             "import os, subprocess, sys\n"
             "p = subprocess.Popen([sys.executable, '-P', '-s', '-m', 'mesh.runwrap', sys.argv[1]],"
             " start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
             " stderr=subprocess.DEVNULL)\nprint(p.pid, flush=True)\nos._exit(0)\n", rdir],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=30, check=True,
        )
        wrapper = int(launcher.stdout.decode().split()[0])
        wpath = os.path.join(rdir, "wrapper.json")
        self.assertTrue(_wait(lambda: os.path.exists(wpath), 15), "runwrap did not write wrapper.json")
        with open(wpath, "r", encoding="utf-8") as wf:
            wst = int(json.load(wf)["starttime"])
        self.addCleanup(_kill_same, wrapper, wst)
        self.addCleanup(lambda: [_kill_same(p, st) for p, st in supervisor._scan_group(wrapper).items()])
        self.addCleanup(terminate.forget, rid)

        now = now_iso_utc()
        tid = f"tsk_{rid}"
        ex = self.conn.execute
        ex("INSERT OR IGNORE INTO principals (id, kind, name, created_at) VALUES ('agt_p1b','agent','c',?);", (now,))
        ex("INSERT OR IGNORE INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
           "VALUES ('agt_p1b','fake','launch',4,?,?);", (now, now))
        ex("INSERT OR IGNORE INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_p1b','p','g','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
           "timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
           "VALUES (?,'prj_p1b','t','s','general',?,'agt_p1b','edit',600,1,1,'usr',?,?);",
           (tid, task_status, now, now))
        ex("INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
           "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at) "
           "VALUES (?,'task',?,'prj_p1b','agt_p1b',1,1,'fake','running',?,?,?,?,?,?,?);",
           (rid, tid, boot_id, wrapper, wrapper, wst, rdir, now, deadline_at))
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, "
           "expires_at, heartbeat_at) VALUES (?,?,'meshd',?,'agt_p1b',1,?,?,?);",
           (f"task:{tid}", rid, boot_id, now, now_iso_utc(time.time() + 3600), now))
        self.conn.commit()
        return wrapper, wst, rdir, tid

    def test_p1_1_2a_live_merge_eacces_holds_run_and_merge_lease(self):
        import asyncio
        import errno
        from unittest import mock
        from mesh.core.proc import get_proc_environ
        from mesh.errors import MeshError

        pidfile = os.path.join(self.tmp, "m2a_mem.pid")
        cmd = (f"env -i {shutil.which('sleep')} 120 </dev/null >/dev/null 2>&1 & "
               f"echo $! > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}; sleep 0.25; exit 0")
        server, ctx, _logs, c = self._setup_live_merge(["sh", "-c", cmd])

        real_term_stat = terminate.get_proc_stat
        real_sup_stat = supervisor.get_proc_stat
        state = {"on": True, "member": None, "mst": None, "hits": 0}

        def maybe_load_member():
            if state["member"] is None and os.path.exists(pidfile):
                with open(pidfile, "r", encoding="utf-8") as f:
                    mpid = int(f.read().strip())
                info = real_sup_stat(mpid)
                if info is not None:
                    state["member"] = mpid
                    state["mst"] = int(info["starttime"])

        def term_stat(pid):
            maybe_load_member()
            if state["on"] and state["member"] is not None and int(pid) == state["member"]:
                state["hits"] += 1
                raise PermissionError(errno.EACCES, "injected EACCES", f"/proc/{pid}/stat")
            return real_term_stat(pid)

        def sup_stat(pid):
            maybe_load_member()
            if state["on"] and state["member"] is not None and int(pid) == state["member"]:
                state["hits"] += 1
                raise PermissionError(errno.EACCES, "injected EACCES", f"/proc/{pid}/stat")
            return real_sup_stat(pid)

        sent_signals = []
        real_kill, real_killpg = os.kill, os.killpg

        def track_kill(pid, sig):
            if sig != 0 and state["member"] is not None and pid == state["member"]:
                sent_signals.append(("kill", pid, sig))
            return real_kill(pid, sig)

        def track_killpg(pgid, sig):
            if sig != 0 and state["member"] is not None:
                info = real_sup_stat(state["member"])
                if info is not None and pgid == info["pgrp"]:
                    sent_signals.append(("killpg", pgid, sig))
            return real_killpg(pgid, sig)

        with mock.patch.object(terminate, "get_proc_stat", term_stat), \
                mock.patch.object(supervisor, "get_proc_stat", sup_stat), \
                mock.patch("os.kill", track_kill), mock.patch("os.killpg", track_killpg), \
                mock.patch.object(supervisor, "_log_meshd"):
            with self.assertRaises(MeshError) as cm:
                asyncio.run(merge.run_task_merge(server, ctx))

        self.assertIsNotNone(state["member"], "same-group member was not spawned")
        member, mst = state["member"], state["mst"]
        self.addCleanup(_kill_same, member, mst)
        self.assertEqual(get_proc_environ(member), {})
        self.assertGreaterEqual(state["hits"], 1, "EACCES injection did not fire")

        # Live merge must refuse to close, report INTERNAL_ERROR, keep run/task/lease held, and never emit D25 kill_failed.
        self.assertEqual(cm.exception.code, c.INTERNAL_ERROR, cm.exception.to_envelope("r"))
        run_row = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_m';").fetchone()
        self.addCleanup(terminate.forget, run_row["id"])
        self.assertEqual(run_row["status"], "running", dict(run_row))
        self.assertIsNotNone(run_row["pgid"], dict(run_row))
        task_row = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_m';").fetchone()
        self.assertEqual(task_row["status"], "merging")
        lease_row = self.conn.execute("SELECT * FROM leases WHERE key = 'merge:prj_m';").fetchone()
        self.assertIsNotNone(lease_row, "merge:prj_m released while live merge scan was uncertain!")
        self.assertEqual(lease_row["run_id"], run_row["id"])
        kf_ev = self.conn.execute("SELECT payload_json FROM events WHERE type = 'run.kill_failed';").fetchall()
        self.assertEqual(kf_ev, [], f"D25 run.kill_failed emitted on uncertain scan: {kf_ev}")
        blk_ev = self.conn.execute("SELECT payload_json FROM events WHERE type = 'merge.recovery_blocked';").fetchall()
        self.assertEqual(len(blk_ev), 1, blk_ev)
        self.assertTrue(_alive(member, mst))
        self.assertEqual(sent_signals, [], f"member signalled under uncertain scan: {sent_signals}")

        # Boot recovery while EACCES/member persists also holds merge_test and merge:prj_m
        # (carrier scan EACCES -> merge_errors=1, merge_runs_lost=0; group gate EACCES -> merge_runs_held=1).
        with mock.patch.object(terminate, "get_proc_stat", term_stat), \
                mock.patch.object(supervisor, "get_proc_stat", sup_stat), \
                mock.patch.object(supervisor, "_log_meshd"):
            summary_c = recovery.recover(self.conn, self.home, "boot_m2a_carrier_held")
        self.assertEqual((summary_c["merge_errors"], summary_c["merge_runs_lost"]), (1, 0), summary_c)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_m';").fetchone())
        with mock.patch.object(supervisor, "get_proc_stat", sup_stat), \
                mock.patch.object(supervisor, "_log_meshd"):
            summary = recovery.recover(self.conn, self.home, "boot_m2a_held")
        self.assertEqual((summary["merge_runs_held"], summary["merge_runs_lost"]), (1, 0), summary)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_m';").fetchone())

        # Once EACCES clears and member exits, recovery closes run 'lost', transitions task to 'merge_failed', releases lease.
        state["on"] = False
        _kill_same(member, mst)
        self.assertTrue(_wait(lambda: not _alive(member, mst), 10))
        with mock.patch.object(supervisor, "_log_meshd"):
            summary2 = recovery.recover(self.conn, self.home, "boot_m2a_clean")
        self.assertEqual((summary2["merge_runs_held"], summary2["merge_runs_lost"]), (0, 1), summary2)
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_row["id"],)).fetchone()[0], "lost")
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_m';").fetchone()[0], "merge_failed")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_m';").fetchone())

    def test_p1_1_2b_live_merge_unknown_starttime_scan_error_holds_lease(self):
        import asyncio
        import errno
        from unittest import mock
        from mesh.errors import MeshError

        pidfile = os.path.join(self.tmp, "m2b_mem.pid")
        cmd = (f"env -i {shutil.which('sleep')} 120 </dev/null >/dev/null 2>&1 & "
               f"echo $! > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}; sleep 0.25; exit 0")
        server, ctx, _logs, c = self._setup_live_merge(["sh", "-c", cmd])

        real_merge_stat = merge.get_proc_stat
        real_sup_stat = supervisor.get_proc_stat
        real_read_json = merge._read_json
        state = {"first_merge_stat": True, "on": True, "member": None, "mst": None, "hits": 0}

        def merge_stat_first_eio(pid):
            if state["first_merge_stat"]:
                state["first_merge_stat"] = False
                raise OSError(errno.EIO, "injected wrapper stat EIO", f"/proc/{pid}/stat")
            return real_merge_stat(pid)

        def read_json_no_wst(path):
            doc = real_read_json(path)
            if path.endswith("wrapper.json") and isinstance(doc, dict):
                return {"pid": doc.get("pid"), "starttime": 0}
            return doc

        def sup_stat_member_eio(pid):
            if state["member"] is None and os.path.exists(pidfile):
                with open(pidfile, "r", encoding="utf-8") as f:
                    mpid = int(f.read().strip())
                info = real_sup_stat(mpid)
                if info is not None:
                    state["member"] = mpid
                    state["mst"] = int(info["starttime"])
            if state["on"] and state["member"] is not None and int(pid) == state["member"]:
                state["hits"] += 1
                raise OSError(errno.EIO, "injected member stat EIO", f"/proc/{pid}/stat")
            return real_sup_stat(pid)

        with mock.patch.object(merge, "get_proc_stat", merge_stat_first_eio), \
                mock.patch.object(merge, "_read_json", read_json_no_wst), \
                mock.patch.object(supervisor, "get_proc_stat", sup_stat_member_eio), \
                mock.patch.object(supervisor, "_log_meshd"):
            with self.assertRaises(MeshError) as cm:
                asyncio.run(merge.run_task_merge(server, ctx))

        self.assertIsNotNone(state["member"], "same-group member did not start")
        member, mst = state["member"], state["mst"]
        self.addCleanup(_kill_same, member, mst)
        self.assertGreaterEqual(state["hits"], 1)
        self.assertEqual(cm.exception.code, c.INTERNAL_ERROR, cm.exception.to_envelope("r"))
        run_row = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_m';").fetchone()
        self.addCleanup(terminate.forget, run_row["id"])
        self.assertEqual(run_row["status"], "running", dict(run_row))
        self.assertIsNotNone(run_row["pgid"], dict(run_row))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_m';").fetchone(),
                             "merge:prj_m released under starttime=None + uncertain group scan!")
        self.assertTrue(_alive(member, mst))

    def test_p1_1_2c_live_merge_timeout_under_emfile_kills_wrapper_and_child_and_holds(self):
        import asyncio
        import errno
        from unittest import mock
        from mesh.errors import MeshError

        sh_pidfile = os.path.join(self.tmp, "m2c_sh.pid")
        cmd = f"echo $$ > {sh_pidfile}.tmp && mv {sh_pidfile}.tmp {sh_pidfile}; exec {shutil.which('sleep')} 120"
        server, ctx, _logs, c = self._setup_live_merge(["sh", "-c", cmd], test_timeout_s=1, kill_wait_s=1.0)

        real_listdir = os.listdir
        state = {"emfile": 0}

        def proc_emfile(path="."):
            # Trigger EMFILE once the test child is running (timeout path + close gate).
            if path == "/proc" and os.path.exists(sh_pidfile):
                state["emfile"] += 1
                raise OSError(errno.EMFILE, "injected EMFILE", "/proc")
            return real_listdir(path)

        with mock.patch.object(terminate.os, "listdir", proc_emfile), \
                mock.patch.object(supervisor.os, "listdir", proc_emfile), \
                mock.patch.object(supervisor, "_log_meshd"):
            with self.assertRaises(MeshError) as cm:
                asyncio.run(merge.run_task_merge(server, ctx))

        self.assertTrue(os.path.exists(sh_pidfile), "test child never started")
        with open(sh_pidfile, "r", encoding="utf-8") as f:
            child_pid = int(f.read().strip())
        cinfo = get_proc_stat(child_pid)
        if cinfo is not None and cinfo["state"] != "Z":
            self.addCleanup(_kill_same, child_pid, int(cinfo["starttime"]))

        run_row = self.conn.execute("SELECT * FROM runs WHERE task_id = 'tsk_m';").fetchone()
        self.addCleanup(terminate.forget, run_row["id"])
        with open(os.path.join(run_row["run_dir"], "wrapper.json"), "r", encoding="utf-8") as wf:
            wdoc = json.load(wf)
        wpid, wst = int(wdoc["pid"]), int(wdoc["starttime"])
        if _alive(wpid, wst):
            self.addCleanup(_kill_same, wpid, wst)

        self.assertGreaterEqual(state["emfile"], 1, "EMFILE was not injected")
        # Both mesh.runwrap AND foreground test child in pgid==wpid must be dead despite /proc EMFILE!
        self.assertFalse(_alive(wpid, wst), "mesh.runwrap survived timeout under /proc EMFILE!")
        self.assertTrue(cinfo is None or not is_tgid_alive(child_pid, cinfo),
                        f"foreground test child {child_pid} survived timeout under /proc EMFILE!")
        # And because /proc scan was uncertain (potential setsid escapees unseen), merge:prj_m stays held!
        self.assertEqual(cm.exception.code, c.INTERNAL_ERROR, cm.exception.to_envelope("r"))
        self.assertEqual(run_row["status"], "running")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_m';").fetchone())

    def test_p1_2_3a_same_uid_environ_eacces_marks_uncertain_and_holds(self):
        import errno
        from unittest import mock
        from mesh.core.proc import get_proc_environ

        esc_pidfile = os.path.join(self.tmp, "esc3a.pid")
        cmd = (f"setsid {shutil.which('sleep')} 120 </dev/null >/dev/null 2>&1 & "
               f"echo $! > {esc_pidfile}.tmp && mv {esc_pidfile}.tmp {esc_pidfile}; sleep 0.1; exit 0")
        wrapper, wst, rdir, tid = self._spawn_runwrap(cmd, boot_id=supervisor._get_current_boot_id(self.conn))
        self.assertTrue(_wait(lambda: os.path.exists(esc_pidfile) and os.path.exists(os.path.join(rdir, "exit.json"))
                              and not _alive(wrapper, wst), 15))
        with open(esc_pidfile, "r", encoding="utf-8") as f:
            esc_pid = int(f.read().strip())
        esc_st = int(get_proc_stat(esc_pid)["starttime"])
        self.addCleanup(_kill_same, esc_pid, esc_st)
        self.assertEqual(get_proc_environ(esc_pid).get("MESH_RUN_ID"), self.run_id)
        self.assertNotEqual(get_proc_stat(esc_pid)["pgrp"], wrapper)

        real_env = terminate.get_proc_environ
        active = {"on": True, "hits": 0}

        def env_eacces(pid):
            if active["on"] and int(pid) == esc_pid:
                active["hits"] += 1
                raise PermissionError(errno.EACCES, "injected environ EACCES", f"/proc/{pid}/environ")
            return real_env(pid)

        with mock.patch.object(terminate, "get_proc_environ", env_eacces), \
                mock.patch.object(supervisor, "_log_meshd"):
            found = terminate.run_targets(self.run_id, wrapper, wrapper, wst)
            self.assertEqual(found, {})
            self.assertTrue(getattr(found, "uncertain", False),
                            "run_targets ignored EACCES on young same-uid carrier!")
            carriers = recovery._env_carriers(self.run_id)
            self.assertTrue(getattr(carriers, "uncertain", False),
                            "recovery._env_carriers ignored EACCES on young same-uid carrier!")

            supervisor.watch_launch_runs(self.conn, self.home)
            time.sleep(terminate.POLL_S + 0.05)
            supervisor.advance_terminations(self.conn, self.home)
            supervisor.watch_launch_runs(self.conn, self.home)

            self.assertGreaterEqual(active["hits"], 1)
            self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0],
                             "running", "run closed while young carrier environ raised EACCES!")
            self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{tid}",)).fetchone())
            self.assertTrue(_alive(esc_pid, esc_st))

            # Once EACCES clears, watch/advance sees MESH_RUN_ID carrier, terminates leftovers, and closes run.
            active["on"] = False
            for _ in range(25):
                supervisor.watch_launch_runs(self.conn, self.home)
                supervisor.advance_terminations(self.conn, self.home)
                if self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0] != "running":
                    break
                time.sleep(0.08)

        self.assertFalse(_alive(esc_pid, esc_st))
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0], "exited")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{tid}",)).fetchone())

    def test_p1_2_3b_zeromock_prctl_nondumpable_escapee_held_and_old_bystander_ignored(self):
        from unittest import mock
        from mesh.core.proc import get_proc_environ

        nondump_code = (
            "import ctypes, os, sys, time\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "assert libc.prctl(4, 0, 0, 0, 0) == 0\n"
            "if len(sys.argv) > 1:\n"
            "    open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
            "    os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "print('READY', flush=True)\n"
            "time.sleep(120)\n"
        )
        # 1) Pre-existing same-uid non-dumpable bystander (like ssh-agent) started BEFORE the run.
        bystander_env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        bystander = subprocess.Popen(
            [sys.executable, "-c", nondump_code], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=bystander_env, text=True,
        )

        def cleanup_bystander():
            if bystander.stdout is not None:
                bystander.stdout.close()
            if bystander.poll() is None:
                bystander.kill()
            bystander.wait()

        self.addCleanup(cleanup_bystander)
        self.assertEqual(bystander.stdout.readline().strip(), "READY")
        with self.assertRaises(PermissionError):
            get_proc_environ(bystander.pid)
        bystander_st = int(get_proc_stat(bystander.pid)["starttime"])

        # Advance at least 1 clock tick so wrapper_st > bystander_st.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            probe = subprocess.Popen([sys.executable, "-c", "pass"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pst = int((get_proc_stat(probe.pid) or {"starttime": bystander_st})["starttime"])
            probe.wait()
            if pst > bystander_st:
                break
            time.sleep(0.02)

        boot_id = supervisor._get_current_boot_id(self.conn)
        rid_clean = f"{self.run_id}_clean"
        w1, wst1, rdir1, tid1 = self._spawn_runwrap("exit 0", boot_id=boot_id, run_id=rid_clean)
        self.assertGreater(wst1, bystander_st)
        self.assertTrue(_wait(lambda: os.path.exists(os.path.join(rdir1, "exit.json")), 15))
        with mock.patch.object(supervisor, "_log_meshd"):
            for _ in range(6):
                supervisor.watch_launch_runs(self.conn, self.home)
                supervisor.advance_terminations(self.conn, self.home)
                if self.conn.execute("SELECT status FROM runs WHERE id = ?;", (rid_clean,)).fetchone()[0] != "running":
                    break
                time.sleep(terminate.POLL_S + 0.03)
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (rid_clean,)).fetchone()[0],
                         "exited", "pre-existing non-dumpable bystander wedged clean run close!")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{tid1}",)).fetchone())

        # 2) In-run setsid + prctl(PR_SET_DUMPABLE, 0) MESH_RUN_ID escapee (ZERO MOCKS on /proc!).
        esc_pidfile = os.path.join(self.tmp, "esc3b.pid")
        esc_script = os.path.join(self.tmp, "nondump_esc.py")
        with open(esc_script, "w", encoding="utf-8") as f:
            f.write(nondump_code)
        cmd = (f"setsid {sys.executable} {esc_script} {esc_pidfile} </dev/null >/dev/null 2>&1 & "
               f"while [ ! -e {esc_pidfile} ]; do sleep 0.02; done; exit 0")
        w2, wst2, rdir2, tid2 = self._spawn_runwrap(cmd, boot_id=boot_id, run_id=self.run_id)
        self.assertTrue(_wait(lambda: os.path.exists(esc_pidfile) and os.path.exists(os.path.join(rdir2, "exit.json")), 15))
        with open(esc_pidfile, "r", encoding="utf-8") as f:
            esc_pid = int(f.read().strip())
        esc_st = int(get_proc_stat(esc_pid)["starttime"])
        self.addCleanup(_kill_same, esc_pid, esc_st)
        self.assertGreaterEqual(esc_st, wst2)
        with self.assertRaises(PermissionError):
            get_proc_environ(esc_pid)

        # Zero-mock scan must report uncertain=True and hold run + task lease while escapee lives!
        found = terminate.run_targets(self.run_id, w2, w2, wst2)
        self.assertTrue(getattr(found, "uncertain", False),
                        "zero-mock prctl(PR_SET_DUMPABLE, 0) escapee was NOT marked uncertain!")
        with mock.patch.object(supervisor, "_log_meshd"):
            supervisor.watch_launch_runs(self.conn, self.home)
            time.sleep(terminate.POLL_S + 0.05)
            supervisor.advance_terminations(self.conn, self.home)
            supervisor.watch_launch_runs(self.conn, self.home)
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0],
                         "running", "run closed while real non-dumpable MESH_RUN_ID escapee was alive!")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{tid2}",)).fetchone())
        self.assertTrue(_alive(esc_pid, esc_st))

        # Once escapee exits, run closes 'exited' and releases task:<id>.
        _kill_same(esc_pid, esc_st)
        self.assertTrue(_wait(lambda: not _alive(esc_pid, esc_st), 10))
        with mock.patch.object(supervisor, "_log_meshd"):
            for _ in range(6):
                supervisor.watch_launch_runs(self.conn, self.home)
                supervisor.advance_terminations(self.conn, self.home)
                if self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0] != "running":
                    break
                time.sleep(terminate.POLL_S + 0.03)
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0], "exited")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{tid2}",)).fetchone())

    def test_p1_2_3c_same_uid_and_now_ticks_oserror_mark_uncertain(self):
        import builtins
        import errno
        from unittest import mock

        child = subprocess.Popen([shutil.which("sleep"), "60"], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def cleanup_child():
            if child.poll() is None:
                child.kill()
            child.wait()

        self.addCleanup(cleanup_child)
        cst = int(get_proc_stat(child.pid)["starttime"])

        real_env = terminate.get_proc_environ
        real_stat = terminate.os.stat
        real_open = builtins.open

        def empty_env_for_child(pid):
            if int(pid) == child.pid:
                return {}
            return real_env(pid)

        # 3C-1: os.stat("/proc/<child>") raises EMFILE inside _same_uid during environ=={} window.
        stat_hits = {"n": 0}

        def stat_emfile(path, *args, **kwargs):
            if path == f"/proc/{child.pid}":
                stat_hits["n"] += 1
                raise OSError(errno.EMFILE, "injected stat EMFILE", path)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(terminate, "get_proc_environ", empty_env_for_child), \
                mock.patch.object(terminate.os, "stat", stat_emfile):
            found1 = terminate.run_targets(self.run_id, None, None, cst)
        self.assertGreaterEqual(stat_hits["n"], 1)
        self.assertTrue(getattr(found1, "uncertain", False),
                        "_same_uid EMFILE swallowed without setting ScanResult.uncertain!")

        # 3C-2: open("/proc/uptime") raises EMFILE inside _now_ticks when wrapper_starttime is None.
        uptime_hits = {"n": 0}

        def open_uptime_emfile(file, *args, **kwargs):
            if file == "/proc/uptime":
                uptime_hits["n"] += 1
                raise OSError(errno.EMFILE, "injected uptime EMFILE", file)
            return real_open(file, *args, **kwargs)

        with mock.patch.object(terminate, "get_proc_environ", empty_env_for_child), \
                mock.patch("builtins.open", open_uptime_emfile):
            found2 = terminate.run_targets(self.run_id, None, None, None)
        self.assertGreaterEqual(uptime_hits["n"], 1)
        self.assertTrue(getattr(found2, "uncertain", False),
                        "_now_ticks EMFILE swallowed without setting ScanResult.uncertain!")

        # 3C-3: open("/proc/<child>/stat") raises EIO inside _is_kthread on EACCES environ check.
        kthread_hits = {"n": 0}

        def env_eacces_for_child(pid):
            if int(pid) == child.pid:
                raise PermissionError(errno.EACCES, "injected environ EACCES", f"/proc/{pid}/environ")
            return real_env(pid)

        def open_kthread_eio(file, *args, **kwargs):
            if file == f"/proc/{child.pid}/stat" and sys._getframe(1).f_code.co_name == "_is_kthread":
                kthread_hits["n"] += 1
                raise OSError(errno.EIO, "injected kthread stat EIO", file)
            return real_open(file, *args, **kwargs)

        with mock.patch.object(terminate, "get_proc_environ", env_eacces_for_child), \
                mock.patch("builtins.open", open_kthread_eio):
            found3 = terminate.run_targets(self.run_id, None, None, cst)
        self.assertGreaterEqual(kthread_hits["n"], 1)
        self.assertTrue(getattr(found3, "uncertain", False),
                        "_is_kthread EIO swallowed without setting ScanResult.uncertain!")

    def test_p1_3_watch_transient_wrapper_stat_eio_preserves_live_run_and_enforces_deadline(self):
        import errno
        from unittest import mock

        trigger = os.path.join(self.tmp, "trig_p13w")
        cmd = f"while [ ! -e {trigger} ]; do sleep 0.03; done; exit 0"
        boot_id = supervisor._get_current_boot_id(self.conn)
        wrapper, wst, rdir, tid = self._spawn_runwrap(cmd, boot_id=boot_id)

        real_sup_stat = supervisor.get_proc_stat
        active = {"on": True, "hits": 0}

        def wrapper_stat_eio(pid):
            if active["on"] and int(pid) == wrapper:
                active["hits"] += 1
                raise OSError(errno.EIO, "injected wrapper stat EIO", f"/proc/{pid}/stat")
            return real_sup_stat(pid)

        # 1) Transient EIO on live wrapper during watch_launch_runs must NOT start wrapper_lost termination!
        with mock.patch.object(supervisor, "get_proc_stat", wrapper_stat_eio), \
                mock.patch.object(supervisor, "_log_meshd"):
            supervisor.watch_launch_runs(self.conn, self.home)

        self.assertGreaterEqual(active["hits"], 1)
        self.assertIsNone(terminate.get(self.run_id), "transient wrapper stat EIO started termination on live run!")
        self.assertTrue(_alive(wrapper, wst), "live wrapper was killed on transient stat EIO!")
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0], "running")
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = ?;", (tid,)).fetchone()[0], "running")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{tid}",)).fetchone())

        # EIO clears and agent submits + exits 0 cleanly -> run closes 'exited' (exit_0), task stays 'submitted'.
        active["on"] = False
        self.conn.execute("UPDATE tasks SET status = 'submitted' WHERE id = ?;", (tid,))
        self.conn.commit()
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: os.path.exists(os.path.join(rdir, "exit.json")), 15))
        with mock.patch.object(supervisor, "_log_meshd"):
            for _ in range(6):
                supervisor.watch_launch_runs(self.conn, self.home)
                supervisor.advance_terminations(self.conn, self.home)
                if self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0] != "running":
                    break
                time.sleep(terminate.POLL_S + 0.03)
        run_done = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertEqual((run_done["status"], run_done["end_reason"]), ("exited", "exited"))
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = ?;", (tid,)).fetchone()[0], "submitted")

        # 2) Past-deadline run STILL starts 'timeout' termination even while wrapper_liveness is 'uncertain'.
        rid_dl = f"{self.run_id}_dl"
        trig_dl = os.path.join(self.tmp, "trig_p13dl")
        past = now_iso_utc(time.time() - 60)
        w2, wst2, _rdir2, _tid2 = self._spawn_runwrap(
            f"while [ ! -e {trig_dl} ]; do sleep 0.05; done; exit 0",
            boot_id=boot_id, run_id=rid_dl, deadline_at=past,
        )
        dl_hits = {"n": 0}

        def w2_stat_eio(pid):
            if int(pid) == w2:
                dl_hits["n"] += 1
                raise OSError(errno.EIO, "injected wrapper stat EIO", f"/proc/{pid}/stat")
            return real_sup_stat(pid)

        with mock.patch.object(supervisor, "get_proc_stat", w2_stat_eio), \
                mock.patch.object(supervisor, "_log_meshd"):
            supervisor.watch_launch_runs(self.conn, self.home)
        self.assertGreaterEqual(dl_hits["n"], 1)
        t_dl = terminate.get(rid_dl)
        self.assertIsNotNone(t_dl, "deadline termination bypassed while wrapper_liveness was uncertain!")
        self.assertEqual(t_dl.end_reason, "timeout")
        open(trig_dl, "w").close()

    def test_p1_3_recovery_transient_wrapper_stat_eio_records_error_without_losing_run(self):
        import errno
        from unittest import mock

        trigger = os.path.join(self.tmp, "trig_p13r")
        cmd = f"while [ ! -e {trigger} ]; do sleep 0.03; done; exit 0"
        wrapper, wst, _rdir, tid = self._spawn_runwrap(cmd, boot_id="boot_old")

        real_rec_stat = recovery.get_proc_stat
        active = {"on": True, "hits": 0}

        def rec_stat_eio(pid):
            if active["on"] and int(pid) == wrapper:
                active["hits"] += 1
                raise OSError(errno.EIO, "injected recovery wrapper stat EIO", f"/proc/{pid}/stat")
            return real_rec_stat(pid)

        with mock.patch.object(recovery, "get_proc_stat", rec_stat_eio), \
                mock.patch.object(supervisor, "_log_meshd"):
            summary = recovery.recover(self.conn, self.home, "boot_new_1")

        self.assertGreaterEqual(active["hits"], 1)
        self.assertEqual(summary["run_errors"], 1, summary)
        self.assertEqual(summary["runs_lost"], 0, summary)
        self.assertEqual(summary["runs_terminating"], 0, summary)
        self.assertEqual(summary["runs_adopted"], 0, summary)
        self.assertTrue(_alive(wrapper, wst), "recovery killed live wrapper on transient stat EIO!")
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (self.run_id,)).fetchone()[0], "running")
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = ?;", (tid,)).fetchone()[0], "running")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{tid}",)).fetchone())

        # Once EIO clears, next recovery adopts the live run cleanly.
        active["on"] = False
        with mock.patch.object(supervisor, "_log_meshd"):
            summary2 = recovery.recover(self.conn, self.home, "boot_new_2")
        self.assertEqual((summary2["runs_adopted"], summary2["run_errors"]), (1, 0), summary2)
        self.assertTrue(_alive(wrapper, wst))
        open(trigger, "w").close()

    def test_p1_3_watch_lead_turn_transient_wrapper_stat_eio_preserves_live_turn(self):
        import errno
        from unittest import mock
        from mesh.adapters.base import CommandSpec
        from mesh.adapters.registry import get_adapter as real_get
        from mesh.core.events import record_event
        from mesh.daemon import lead_turns

        now = now_iso_utc()
        ex = self.conn.execute
        ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lt','agent','lead_lt',?);", (now,))
        ex("INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
           "VALUES ('agt_lt','fake','launch',1,1,?,?);", (now, now))
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_lt','plt','glt','active','usr','cli',?,?);", (now, now))
        ex("INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
           "VALUES ('las_lt','prj_lt','agt_lt','usr','cli',?);", (now,))
        ev = record_event(
            self.conn, type_="project.note", actor_id="usr", actor_kind="user", via="direct",
            project_id="prj_lt", payload={"note": "ping"},
        )
        ex("DELETE FROM outbox;")
        ex("INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, payload_json, "
           "status, attempts, next_attempt_at, created_at) "
           "VALUES ('out_lt',?,'agt_lt','lead_wake','dk_lt','prj_lt','lead.wake','{}','pending',0,?,?);",
           (ev["seq"], now, now))
        self.conn.commit()

        trigger = os.path.join(self.tmp, "trig_lt")
        runner = os.path.join(self.tmp, "lt_wait.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(
                "import os, time\n"
                f"while not os.path.exists({trigger!r}):\n"
                "    time.sleep(0.03)\n"
            )

        def get_adapter(adapter_id, testing_mode=False, options=None):
            a = real_get(adapter_id, testing_mode=testing_mode, options=options)
            a.build = lambda req: CommandSpec(argv=[sys.executable, runner], cwd=req.workspace)
            return a

        lead = lead_turns.get_project_active_lead(self.conn, "prj_lt")
        msgs = self.conn.execute("SELECT * FROM outbox WHERE project_id = 'prj_lt';").fetchall()
        with mock.patch.object(lead_turns, "get_adapter", get_adapter), \
                mock.patch.object(supervisor, "_log_meshd"):
            rid = lead_turns.start_lead_turn(self.conn, self.home, "prj_lt", lead, msgs, testing_mode=True)
        self.assertIsNotNone(rid)
        self.addCleanup(terminate.forget, rid)
        lt_run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (rid,)).fetchone()
        wpid = int(lt_run["wrapper_pid"])
        wst = int(lt_run["proc_starttime"])

        def _cleanup_lt():
            for p, st in supervisor._scan_group(wpid).items():
                _kill_same(p, st)
            _kill_same(wpid, wst)
            proc = supervisor._WRAPPERS.pop(rid, None)
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            supervisor.children.unregister(wpid)

        self.addCleanup(_cleanup_lt)

        real_sup_stat = supervisor.get_proc_stat
        hits = {"n": 0}

        def lt_stat_eio(pid):
            if int(pid) == wpid:
                hits["n"] += 1
                raise OSError(errno.EIO, "injected lead_turn stat EIO", f"/proc/{pid}/stat")
            return real_sup_stat(pid)

        with mock.patch.object(supervisor, "get_proc_stat", lt_stat_eio), \
                mock.patch.object(supervisor, "_log_meshd"):
            supervisor.watch_launch_runs(self.conn, self.home)
        self.assertGreaterEqual(hits["n"], 1)
        self.assertIsNone(terminate.get(rid), "transient stat EIO started termination on live lead_turn!")
        self.assertTrue(_alive(wpid, wst), "live lead_turn wrapper was killed on transient stat EIO!")
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (rid,)).fetchone()[0], "running")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'leadturn:prj_lt';").fetchone())
        open(trigger, "w").close()
        self.assertTrue(_wait(lambda: not _alive(wpid, wst), 10))


if __name__ == "__main__":
    unittest.main()


class TestCarbonRoP1bCoverage(unittest.TestCase):
    """ACC: N18, A8 (Quest p1b review P3-4: surviving mutants M2, M4)."""

    def test_live_merge_close_leftover_scan_uncertain_alone_holds(self):
        """M2: only ``leftover_scan_uncertain`` is set (the termination itself was certain and
        did not fail): the live merge close still holds (EIO), never the release path."""
        from types import SimpleNamespace
        from unittest import mock

        row = {"id": "run_m2", "status": "running"}
        conn = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: row),
                               in_transaction=False)
        result = {"test": {"leftover_scan_uncertain": True, "termination": {"targets": 0}, "wrapper": {}}}
        with mock.patch.object(merge, "merge_close_gate") as gate:
            with self.assertRaises(OSError):
                merge._check_live_merge_close(conn, {"run_id": "run_m2"}, result)
        gate.assert_called_once()
        # Control: neither flag set and no kill_failed -> gate only, no hold error.
        clean = {"test": {"termination": {"targets": 0}, "wrapper": {}}}
        with mock.patch.object(merge, "merge_close_gate") as gate2:
            merge._check_live_merge_close(conn, {"run_id": "run_m2"}, clean)
        gate2.assert_called_once()

    def test_is_kthread_stat_error_is_recorded_as_scan_error(self):
        """M4: an EIO reading ``/proc/<pid>/stat`` inside ``_is_kthread`` during a scan is
        recorded (scan uncertain); ENOENT is a lookup miss and is not."""
        import builtins
        import errno
        from unittest import mock

        child = subprocess.Popen([shutil.which("sleep"), "30"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait()))
        info = get_proc_stat(child.pid)
        real_open = builtins.open

        def run_with(err):
            def fake_open(file, *a, **k):
                if file == f"/proc/{child.pid}/stat":
                    raise OSError(err, "injected", file)
                return real_open(file, *a, **k)
            sink = []
            token = terminate._SCAN_ERRORS.set(sink)
            try:
                with mock.patch.object(builtins, "open", fake_open):
                    self.assertFalse(terminate._is_kthread(child.pid, info))
            finally:
                terminate._SCAN_ERRORS.reset(token)
            return sink

        self.assertTrue(run_with(errno.EIO), "_is_kthread EIO was not recorded as a scan error")
        self.assertFalse(run_with(errno.ENOENT), "_is_kthread ENOENT must not be a scan error")


_NONDUMPABLE_ESCAPEE = r"""
import ctypes, os, sys, time
pidfile = sys.argv[1]
os.setsid()
rc = ctypes.CDLL("libc.so.6", use_errno=True).prctl(4, 0, 0, 0, 0)
assert rc == 0, f"prctl(PR_SET_DUMPABLE, 0) failed: {rc}"
with open(pidfile, "w", encoding="utf-8") as f:
    f.write(str(os.getpid()))
while True:
    time.sleep(0.05)
"""

_LZ_MULTI_WORKER = r"""
import ctypes, json, os, platform, sys, threading, time
ready = sys.argv[1]
os.setsid()
libc = ctypes.CDLL("libc.so.6", use_errno=True)
mach = platform.machine()
SYS_exit = 60 if mach in ("x86_64", "amd64") else 93
SYS_gettid = 186 if mach in ("x86_64", "amd64") else 178
info = {}
lock = threading.Lock()

def short_w(name, flag):
    tid = int(libc.syscall(SYS_gettid))
    with lock:
        info[name] = tid
    while not os.path.exists(flag):
        time.sleep(0.005)

def keep_w():
    tid = int(libc.syscall(SYS_gettid))
    with lock:
        info["keep"] = tid
    while True:
        time.sleep(0.05)

for k in ("s1", "s2"):
    t = threading.Thread(target=short_w, args=(k, ready + "." + k))
    t.start()
    while k not in info:
        time.sleep(0.005)
tk = threading.Thread(target=keep_w)
tk.start()
while "keep" not in info:
    time.sleep(0.005)
with open(ready, "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid(), **info}, f)
libc.syscall(SYS_exit, 0)
"""


class TestCarbonRoP1c(unittest.TestCase):
    """ACC: N18, A8 (Carbon RO review m3-clickup-p1b-carbon.md, round p1c):
    - P1-1: crash recovery passes known wrapper_starttime into _env_carriers, _terminate_carriers,
      and merge.recover_merge_runs (1A task/lead >= YOUNG_S, 1B merge_test >= YOUNG_S,
      1C immediate restart then advance_terminations >= YOUNG_S).
    - P2-1: proc._sibling_thread_environ propagates non-lookup OSError on /proc/<cand>/status
      and treats FileNotFoundError / empty environ on exiting sibling threads as lookup misses.
    - P3-1: proc.find_run_processes marks non-lookup get_proc_environ OSError uncertain.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-gf-p1c-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "logs"))
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)
        self.created_runs = []
        self.addCleanup(self._forget_all)

    def _forget_all(self):
        for rid in self.created_runs:
            supervisor._CLOSE_REFUSED_LOGGED.pop(rid, None)
            terminate.forget(rid)
            supervisor._CLEAR.pop(rid, None)
            supervisor._SEEN_EVENT_PENDING.pop(rid, None)
            supervisor._SEEN_EVENT_DONE.discard(rid)
            supervisor._SEEN_UNUSABLE.discard(rid)
        supervisor._evict_seen(set())

    @staticmethod
    def _read_text(path):
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def _dead_wrapper_identity(self):
        w = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.05)"],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        info = get_proc_stat(w.pid)
        self.assertIsNotNone(info)
        wst = int(info["starttime"])
        w.wait(timeout=5)
        return w.pid, wst

    def _spawn_nondumpable_escapee(self, run_id, tag):
        pf = os.path.join(self.tmp, f"esc_{tag}.pid")
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        env["MESH_RUN_ID"] = run_id
        p = subprocess.Popen(
            [sys.executable, "-c", _NONDUMPABLE_ESCAPEE, pf],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertTrue(_wait(lambda: bool(self._read_text(pf)), 10),
                        "nondumpable escapee did not start")
        epid = int(self._read_text(pf))
        est = int(get_proc_stat(epid)["starttime"])

        def _cleanup():
            _kill_same(epid, est)
            try:
                p.wait(timeout=5)
            except Exception:
                pass

        self.addCleanup(_cleanup)
        return p, epid, est

    def test_p1_1_1a_task_and_lead_crash_recovery_after_young_s_holds_nondumpable_escapee(self):
        from unittest import mock

        r_task = f"run_p1c_1a_t_{os.getpid()}"
        r_lead = f"run_p1c_1a_l_{os.getpid()}"
        self.created_runs.extend([r_task, r_lead])
        wpid_t, wst_t = self._dead_wrapper_identity()
        _pt, epid_t, est_t = self._spawn_nondumpable_escapee(r_task, "1at")
        wpid_l, wst_l = self._dead_wrapper_identity()
        _pl, epid_l, est_l = self._spawn_nondumpable_escapee(r_lead, "1al")

        rdir_t = os.path.join(self.home, "runs", r_task)
        rdir_l = os.path.join(self.home, "runs", r_lead)
        os.makedirs(rdir_t)
        os.makedirs(rdir_l)
        with open(os.path.join(rdir_t, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wpid_t, "pgid": wpid_t, "starttime": wst_t}, f)
        with open(os.path.join(rdir_l, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wpid_l, "pgid": wpid_l, "starttime": wst_l}, f)

        now = now_iso_utc()
        exp = now_iso_utc(time.time() + 3600)
        ex = self.conn.execute
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_1a','p','g','active','u','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
           "attempt, created_by, created_at, updated_at) "
           "VALUES ('tsk_1a','prj_1a','t','s','general','running','edit',600,2,1,'u',?,?);", (now, now))
        # Spawn-window task row (pgid/proc_starttime NULL in DB, starttime in wrapper.json).
        ex("INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, boot_id, "
           "wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
           "VALUES (?,'task','tsk_1a','prj_1a',1,1,'none','starting','b_old',NULL,NULL,NULL,?,?);",
           (r_task, rdir_t, now))
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
           "VALUES ('task:tsk_1a',?,'meshd','b_old',1,?,?,?);", (r_task, now, exp, now))
        # Committed lead_turn row (proc_starttime in DB).
        ex("INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, boot_id, "
           "wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
           "VALUES (?,'lead_turn',NULL,'prj_1a',1,1,'none','running','b_old',?,?,?,?,?);",
           (r_lead, wpid_l, wpid_l, wst_l, rdir_l, now))
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
           "VALUES ('leadturn:prj_1a',?,'meshd','b_old',1,?,?,?);", (r_lead, now, exp, now))
        self.conn.commit()

        with mock.patch.object(terminate, "YOUNG_S", 0.15), \
                mock.patch.object(supervisor, "_log_meshd"):
            time.sleep(0.30)
            rc = recovery.recover(self.conn, self.home, "b_new")
            self.assertEqual(rc["runs_lost"], 0, rc)
            self.assertEqual(rc["runs_terminating"], 2, rc)
            for rid, lkey in ((r_task, "task:tsk_1a"), (r_lead, "leadturn:prj_1a")):
                st = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (rid,)).fetchone()[0]
                self.assertIn(st, ("starting", "running"), f"{rid} closed while nondumpable escapee lived")
                self.assertIsNotNone(
                    self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (lkey,)).fetchone(),
                    f"lease {lkey} released while nondumpable escapee lived",
                )

            # Once escapees exit, advance_terminations closes both runs and releases leases.
            _kill_same(epid_t, est_t)
            _kill_same(epid_l, est_l)
            self.assertTrue(_wait(lambda: not _alive(epid_t, est_t) and not _alive(epid_l, est_l), 5))
            supervisor.advance_terminations(self.conn, self.home)
            time.sleep(terminate.POLL_S + 0.05)
            supervisor.advance_terminations(self.conn, self.home)

        for rid, lkey in ((r_task, "task:tsk_1a"), (r_lead, "leadturn:prj_1a")):
            self.assertEqual(
                self.conn.execute("SELECT status FROM runs WHERE id = ?;", (rid,)).fetchone()[0], "lost"
            )
            self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (lkey,)).fetchone())

    def test_p1_1_1b_merge_test_crash_recovery_after_young_s_holds_nondumpable_escapee(self):
        from unittest import mock

        r_mrg = f"run_p1c_1b_{os.getpid()}"
        self.created_runs.append(r_mrg)
        wpid, wst = self._dead_wrapper_identity()
        _pm, epid, est = self._spawn_nondumpable_escapee(r_mrg, "1bm")

        rdir = os.path.join(self.home, "merges", "mrg_1b")
        os.makedirs(rdir)
        with open(os.path.join(rdir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wpid, "pgid": wpid, "starttime": wst}, f)

        now = now_iso_utc()
        exp = now_iso_utc(time.time() + 3600)
        ex = self.conn.execute
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_1b','p','g','active','u','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
           "attempt, merge_json, created_by, created_at, updated_at) "
           "VALUES ('tsk_1b','prj_1b','t','s','code','merging','edit',600,1,1,?,'u',?,?);",
           (json.dumps({"mrg": "mrg_1b", "run_id": r_mrg, "pre_head": "0" * 40, "started_at": now}), now, now))
        # Match _tx1: wrapper_pid/pgid/proc_starttime are NULL in DB while wrapper.json has starttime.
        ex("INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, boot_id, "
           "run_dir, argv_redacted_json, started_at) "
           "VALUES (?,'merge_test','tsk_1b','prj_1b',1,1,'mesh','running','b_old',?,'[]',?);",
           (r_mrg, rdir, now))
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
           "VALUES ('merge:prj_1b',?,'meshd','b_old',1,?,?,?);", (r_mrg, now, exp, now))
        self.conn.commit()

        with mock.patch.object(terminate, "YOUNG_S", 0.15), \
                mock.patch.object(supervisor, "_log_meshd"):
            time.sleep(0.30)
            rc = recovery.recover(self.conn, self.home, "b_new_1")
            self.assertEqual(rc["merge_runs_lost"], 0, rc)
            self.assertEqual(rc["run_errors"], 1, rc)
            self.assertEqual(rc["merge_errors"], 1, rc)
            self.assertEqual(
                self.conn.execute("SELECT status FROM runs WHERE id = ?;", (r_mrg,)).fetchone()[0], "running"
            )
            self.assertEqual(
                self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_1b';").fetchone()[0], "merging"
            )
            self.assertIsNotNone(
                self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_1b';").fetchone(),
                "merge:prj_1b released while nondumpable escapee lived",
            )
            self.assertIsNotNone(
                self.conn.execute(
                    "SELECT 1 FROM events WHERE type = 'merge.recovery_blocked' AND run_id = ?;", (r_mrg,)
                ).fetchone()
            )

            # Once the escapee exits, the next boot closes the merge_test run cleanly.
            _kill_same(epid, est)
            self.assertTrue(_wait(lambda: not _alive(epid, est), 5))
            rc2 = recovery.recover(self.conn, self.home, "b_new_2")
            self.assertEqual(rc2["merge_runs_lost"], 1, rc2)
            self.assertEqual(
                self.conn.execute("SELECT status FROM runs WHERE id = ?;", (r_mrg,)).fetchone()[0], "lost"
            )
            self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_1b';").fetchone())

        # Sub-case: live dumpable carrier + non-dumpable escapee exercises terminate.begin(..., wrapper_starttime=known_wst).
        r_mrg2 = f"run_p1c_1b2_{os.getpid()}"
        self.created_runs.append(r_mrg2)
        wpid2, wst2 = self._dead_wrapper_identity()
        _pm2, epid2, est2 = self._spawn_nondumpable_escapee(r_mrg2, "1bm2")
        cenv = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        cenv["MESH_RUN_ID"] = r_mrg2
        cproc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=cenv, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cst = int(get_proc_stat(cproc.pid)["starttime"])
        self.addCleanup(lambda: (_kill_same(cproc.pid, cst), cproc.wait(timeout=5)))

        rdir2 = os.path.join(self.home, "merges", "mrg_1b2")
        os.makedirs(rdir2)
        with open(os.path.join(rdir2, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wpid2, "pgid": wpid2, "starttime": wst2}, f)
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_1b2','p','g','active','u','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
           "attempt, merge_json, created_by, created_at, updated_at) "
           "VALUES ('tsk_1b2','prj_1b2','t','s','code','merging','edit',600,1,1,?,'u',?,?);",
           (json.dumps({"mrg": "mrg_1b2", "run_id": r_mrg2, "pre_head": "0" * 40, "started_at": now}), now, now))
        ex("INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, boot_id, "
           "run_dir, argv_redacted_json, started_at) "
           "VALUES (?,'merge_test','tsk_1b2','prj_1b2',1,1,'mesh','running','b_old',?,'[]',?);",
           (r_mrg2, rdir2, now))
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
           "VALUES ('merge:prj_1b2',?,'meshd','b_old',1,?,?,?);", (r_mrg2, now, exp, now))
        self.conn.commit()

        from mesh.daemon import merge as mmerge
        with mock.patch.object(terminate, "YOUNG_S", 0.15), \
                mock.patch.object(terminate, "KILL_WAIT_S", 0.25), \
                mock.patch.object(mmerge, "_kill_grace_s", return_value=0), \
                mock.patch.object(supervisor, "_log_meshd"):
            time.sleep(0.30)
            rc3 = recovery.recover(self.conn, self.home, "b_new_3")
            cproc.wait(timeout=5)
            self.assertEqual(rc3["merge_runs_killed"], 0, rc3)
            self.assertEqual(rc3["run_errors"], 1, rc3)
            self.assertEqual(
                self.conn.execute("SELECT status FROM runs WHERE id = ?;", (r_mrg2,)).fetchone()[0], "running"
            )
            self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_1b2';").fetchone())

    def test_p1_1_1c_immediate_restart_then_advance_after_young_s_holds_nondumpable_escapee(self):
        from unittest import mock

        r_c = f"run_p1c_1c_{os.getpid()}"
        self.created_runs.append(r_c)
        wpid, wst = self._dead_wrapper_identity()
        _pc, epid, est = self._spawn_nondumpable_escapee(r_c, "1c")

        rdir = os.path.join(self.home, "runs", r_c)
        os.makedirs(rdir)
        with open(os.path.join(rdir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wpid, "pgid": wpid, "starttime": wst}, f)

        now = now_iso_utc()
        exp = now_iso_utc(time.time() + 3600)
        ex = self.conn.execute
        ex("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
           "VALUES ('prj_1c','p','g','active','u','cli',?,?);", (now, now))
        ex("INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
           "attempt, created_by, created_at, updated_at) "
           "VALUES ('tsk_1c','prj_1c','t','s','general','running','edit',600,2,1,'u',?,?);", (now, now))
        ex("INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, boot_id, "
           "wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
           "VALUES (?,'task','tsk_1c','prj_1c',1,1,'none','running','b_old',?,?,?,?,?);",
           (r_c, wpid, wpid, wst, rdir, now))
        ex("INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
           "VALUES ('task:tsk_1c',?,'meshd','b_old',1,?,?,?);", (r_c, now, exp, now))
        self.conn.commit()

        # Immediate boot (< default YOUNG_S=5s) begins carrier termination on uncertain scan.
        with mock.patch.object(supervisor, "_log_meshd"):
            rc = recovery.recover(self.conn, self.home, "b_new")
        self.assertEqual((rc["runs_lost"], rc["runs_terminating"]), (0, 1), rc)
        t = terminate.get(r_c)
        self.assertIsNotNone(t)

        # Age past patched YOUNG_S and advance terminations: run + lease must remain held.
        with mock.patch.object(terminate, "YOUNG_S", 0.15), \
                mock.patch.object(supervisor, "_log_meshd"):
            time.sleep(0.30)
            for _ in range(3):
                supervisor.advance_terminations(self.conn, self.home)
                time.sleep(terminate.POLL_S + 0.03)
        self.assertEqual(
            self.conn.execute("SELECT status FROM runs WHERE id = ?;", (r_c,)).fetchone()[0],
            "running",
            "advance_terminations closed run after YOUNG_S while nondumpable escapee lived",
        )
        self.assertEqual(t.wrapper_starttime, wst, "terminate.begin lost wrapper_starttime in _terminate_carriers")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'task:tsk_1c';").fetchone())

        _kill_same(epid, est)
        self.assertTrue(_wait(lambda: not _alive(epid, est), 5))
        with mock.patch.object(supervisor, "_log_meshd"):
            supervisor.advance_terminations(self.conn, self.home)
            time.sleep(terminate.POLL_S + 0.05)
            supervisor.advance_terminations(self.conn, self.home)
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (r_c,)).fetchone()[0], "lost")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'task:tsk_1c';").fetchone())

    def _spawn_lz_multi(self, run_id):
        ready = os.path.join(self.tmp, f"lz_{run_id}.json")
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        env["MESH_RUN_ID"] = run_id
        p = subprocess.Popen(
            [sys.executable, "-c", _LZ_MULTI_WORKER, ready],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertTrue(_wait(lambda: bool(self._read_text(ready)), 10))
        meta = json.loads(self._read_text(ready))
        lz_pid = int(meta["pid"])
        self.assertTrue(_wait(lambda: (get_proc_stat(lz_pid) or {}).get("state") == "Z", 5))
        lz_st = int(get_proc_stat(lz_pid)["starttime"])

        def _cleanup():
            _kill_same(lz_pid, lz_st)
            try:
                p.wait(timeout=5)
            except Exception:
                pass

        self.addCleanup(_cleanup)
        return ready, meta, lz_pid, lz_st

    def test_p2_1a_sibling_thread_status_oserror_propagates_and_marks_uncertain(self):
        import builtins
        import errno
        import io
        from unittest import mock
        from mesh.core import proc as mproc

        rid = f"run_p1c_2a_{os.getpid()}"
        self.created_runs.append(rid)
        wpid, wst = self._dead_wrapper_identity()
        _ready, meta, lz_pid, _lz_st = self._spawn_lz_multi(rid)
        sib_statuses = {f"/proc/{int(meta[k])}/status" for k in ("s1", "s2", "keep")}
        real_open = builtins.open

        def open_emfile(file, *a, **kw):
            if file == f"/proc/{lz_pid}/environ":
                return io.BytesIO(b"")
            if file in sib_statuses:
                raise OSError(errno.EMFILE, "injected status EMFILE", file)
            return real_open(file, *a, **kw)

        with mock.patch.object(builtins, "open", open_emfile):
            with self.assertRaises(OSError) as cm:
                mproc.get_proc_environ(lz_pid)
            self.assertEqual(cm.exception.errno, errno.EMFILE)
            rt = terminate.run_targets(rid, wpid, wpid, wst)
        self.assertTrue(getattr(rt, "uncertain", False),
                        "EMFILE on /proc/<cand>/status did not mark run_targets uncertain")

    def test_p2_1b_sibling_thread_environ_enoent_race_and_empty_skips_to_live_thread(self):
        import builtins
        import io
        from unittest import mock
        from mesh.core import proc as mproc

        rid = f"run_p1c_2b_{os.getpid()}"
        self.created_runs.append(rid)
        ready, meta, lz_pid, _lz_st = self._spawn_lz_multi(rid)
        s1_tid, s2_tid = int(meta["s1"]), int(meta["s2"])
        real_open = builtins.open

        def open_race(file, *a, **kw):
            if file == f"/proc/{s1_tid}/environ":
                real_open(ready + ".s1", "w").close()
                _wait(lambda: not os.path.exists(f"/proc/{s1_tid}"), 2.0, step=0.005)
                return real_open(file, *a, **kw)
            if file == f"/proc/{s2_tid}/environ":
                return io.BytesIO(b"")
            return real_open(file, *a, **kw)

        with mock.patch.object(builtins, "open", open_race):
            env = mproc.get_proc_environ(lz_pid)
        self.assertIsNotNone(env)
        self.assertEqual(env.get("MESH_RUN_ID"), rid)

    def test_p3_1_find_run_processes_environ_oserror_sets_uncertain(self):
        import errno
        from unittest import mock
        from mesh.core import proc as mproc

        rid = f"run_p1c_3_{os.getpid()}"
        self.created_runs.append(rid)
        clean = mproc.find_run_processes(rid, None)
        self.assertEqual(clean, [])
        self.assertFalse(clean.uncertain, "clean find_run_processes scan should not be uncertain")

        child = subprocess.Popen(
            [shutil.which("sleep"), "30"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait()))
        real_env = mproc.get_proc_environ

        def env_err(err):
            def _fn(pid):
                if int(pid) == child.pid:
                    raise OSError(err, "injected environ error")
                return real_env(pid)
            return _fn

        for err in (errno.EMFILE, errno.EIO):
            with mock.patch.object(mproc, "get_proc_environ", env_err(err)):
                res = mproc.find_run_processes(rid, None)
            self.assertTrue(res.uncertain, f"find_run_processes did not mark errno={err} uncertain")

        with mock.patch.object(mproc, "get_proc_environ", env_err(errno.ENOENT)):
            res_enoent = mproc.find_run_processes(rid, None)
        self.assertFalse(res_enoent.uncertain, "ENOENT must not mark find_run_processes uncertain")

        # EACCES is a skip here (no wrapper starttime to scope by): a same-uid non-dumpable
        # bystander such as an sshd session must not make every scan uncertain.
        with mock.patch.object(mproc, "get_proc_environ", env_err(errno.EACCES)):
            res_acc = mproc.find_run_processes(rid, None)
        self.assertFalse(res_acc.uncertain, "EACCES must not mark find_run_processes uncertain")




class TestP1cFinalExitingFilter(unittest.TestCase):
    """m3-p1c-final: EACCES from an exiting/zombie/gone process does not hold a run;
    a live non-dumpable same-uid process still does."""

    def test_helper_cases(self) -> None:
        import builtins
        from unittest import mock
        from mesh.daemon import terminate as t
        alive = {"starttime": 100, "state": "S", "num_threads": 1, "ppid": 50}
        real_open = builtins.open

        def fake_status(data=None, err=None):
            def _o(path, *a, **k):
                if str(path).endswith("/status") and str(path).startswith("/proc/4242"):
                    if err is not None:
                        raise OSError(err, "x")
                    import io
                    return io.BytesIO(data)
                return real_open(path, *a, **k)
            return _o

        with mock.patch.object(t, "get_proc_stat", return_value=None):
            self.assertTrue(t._exiting_or_gone(4242, 100))
        with mock.patch.object(t, "get_proc_stat", return_value=dict(alive, starttime=101)):
            self.assertTrue(t._exiting_or_gone(4242, 100))
        with mock.patch.object(t, "get_proc_stat", return_value=dict(alive, state="Z")):
            self.assertTrue(t._exiting_or_gone(4242, 100))
        with mock.patch.object(t, "get_proc_stat", return_value=alive):
            with mock.patch("builtins.open", fake_status(b"Name:\tx\nState:\tS\nVmSize:\t 100 kB\n")):
                self.assertFalse(t._exiting_or_gone(4242, 100))
            with mock.patch("builtins.open", fake_status(b"Name:\tx\nState:\tR\nThreads:\t1\n")):
                self.assertTrue(t._exiting_or_gone(4242, 100))
            with mock.patch("builtins.open", fake_status(err=errno.EIO)):
                self.assertFalse(t._exiting_or_gone(4242, 100))
            with mock.patch("builtins.open", fake_status(err=errno.ESRCH)):
                self.assertTrue(t._exiting_or_gone(4242, 100))
        with mock.patch.object(t, "get_proc_stat", side_effect=OSError(errno.EIO, "x")):
            self.assertFalse(t._exiting_or_gone(4242, 100))

    def test_real_nondumpable_live_process_not_exiting(self) -> None:
        import subprocess, sys, time
        from mesh.daemon import terminate as t
        p = subprocess.Popen([sys.executable, "-c",
                              "import ctypes,time; ctypes.CDLL(None).prctl(4,0,0,0,0); time.sleep(30)"])
        try:
            time.sleep(0.5)
            info = t.get_proc_stat(p.pid)
            self.assertFalse(t._exiting_or_gone(p.pid, int(info["starttime"])))
        finally:
            p.kill(); p.wait()

    def test_leader_exited_threads_alive_is_live(self) -> None:
        import subprocess, sys, time
        from mesh.daemon import terminate as t
        code = ("import ctypes,threading,time,os\n"
                "ctypes.CDLL(None).prctl(4,0,0,0,0)\n"
                "threading.Thread(target=lambda: time.sleep(30), daemon=False).start()\n"
                "time.sleep(0.2)\n"
                "libc=ctypes.CDLL(None, use_errno=True); libc.syscall(60, 0)\n")
        p = subprocess.Popen([sys.executable, "-c", code])
        try:
            time.sleep(1.0)
            info = t.get_proc_stat(p.pid)
            self.assertIsNotNone(info)
            self.assertEqual(info["state"], "Z")
            self.assertFalse(t._exiting_or_gone(p.pid, int(info["starttime"])),
                             "leader-exited process with a live thread must stay live")
        finally:
            p.kill(); p.wait()


class TestP1cFinalLeaderZombie(unittest.TestCase):
    """Carbon RO p1c P1-1: leader Z with a live thread (is_tgid_alive True) is never 'exiting',
    whatever /proc/<pid>/status shows (mainline Linux omits VmSize for the exited leader)."""

    def test_leader_zombie_alive_returns_false(self) -> None:
        from unittest import mock
        from mesh.daemon import terminate as t
        info = {"starttime": 100, "state": "Z", "num_threads": 2, "ppid": 50}
        with mock.patch.object(t, "get_proc_stat", return_value=info), \
                mock.patch.object(t, "is_tgid_alive", return_value=True), \
                mock.patch("builtins.open", side_effect=AssertionError("status must not decide")):
            self.assertFalse(t._exiting_or_gone(4242, 100))
        with mock.patch.object(t, "get_proc_stat", return_value=info), \
                mock.patch.object(t, "is_tgid_alive", return_value=False):
            self.assertTrue(t._exiting_or_gone(4242, 100))


class TestP1cFinalHasMm(unittest.TestCase):
    def test_has_mm(self) -> None:
        from mesh.daemon.terminate import _has_mm
        self.assertTrue(_has_mm(b"Name:\tx\nVmSize:\t  1234 kB\n"))
        self.assertFalse(_has_mm(b"Name:\tx\nVmSize:\t       0 kB\n"))
        self.assertFalse(_has_mm(b"Name:\tx\nState:\tZ\n"))
        self.assertTrue(_has_mm(b"Name:\tx\nVmSize:\tgarbage\n"))
