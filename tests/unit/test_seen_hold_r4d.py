"""r4d (ACC: N18, A8): one seen-state read per tick, and the seen-unusable event is
marked done only after it is stored.

In-process, against a real database and real processes. The run is adopted (its
wrapper is not this process's child), the wrapper (a session leader) has exited and
an env-less member that ignores SIGTERM is still in the wrapper's process group and
session, so only the members recorded in ``seen.json`` can anchor the group.
"""

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
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db
from mesh.core.proc import get_proc_stat
from mesh.daemon import supervisor, terminate

_MEMBER = (
    "import os, signal, sys\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    os.write(1, b'%d\\n' % os.getpid())\n"
    "    r, w = os.pipe()\n"
    "    os.read(r, 1)\n"
    "    os._exit(0)\n"
    "sys.stdin.read()\n"
)


def _wait_for(pred: Callable[[], bool], timeout: float = 30.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


class _SeenDb(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-r4d-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        os.makedirs(os.path.join(self.home, "logs"), exist_ok=True)
        x = self.conn.execute
        x("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1','user','user','t');")
        x("INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
          "VALUES ('prj_1','P','g','active','usr_1','cli','t','t');")
        x("INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES ('kill.grace_s','1','t','usr_1');")
        self._forget()
        self.addCleanup(self._forget)

    def _forget(self) -> None:
        supervisor._evict_seen(set())
        supervisor._SEEN_EVENT_PENDING.clear()  # r4e: not evicted with the runs
        for run_id in list(terminate.active()):
            terminate.forget(run_id)

    def _task(self, task_id: str, status: str = "running") -> None:
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
            "attempt, fence_seq, created_by, created_at, updated_at) "
            "VALUES (?, 'prj_1','T','s','general', ?, 'edit', 600, 1, 1, 1, 'usr_1','t','t');",
            (task_id, status))

    def _spawn_group(self) -> Dict[str, int]:
        """A session leader (the 'wrapper') with an env-less SIGTERM-ignoring member in its
        group and session; the leader then exits and is reaped (gone, not a zombie)."""
        p = subprocess.Popen([sys.executable, "-c", _MEMBER], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             start_new_session=True, env={})
        line = p.stdout.readline()
        member = int(line.strip())
        mst = get_proc_stat(member)["starttime"]

        def _cleanup() -> None:
            cur = get_proc_stat(member)
            if cur is not None and cur["starttime"] == mst and cur["state"] != "Z":
                try:
                    os.kill(member, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(_cleanup)
        wst = get_proc_stat(p.pid)["starttime"]
        p.stdin.close()
        p.wait()
        p.stdout.close()
        info = get_proc_stat(member)
        self.assertEqual(int(info["pgrp"]), p.pid)
        self.assertEqual(int(info["session"]), p.pid)
        return {"wrapper": p.pid, "wst": wst, "member": member, "mst": mst}

    def _run_row(self, run_id: str, task_id: str, g: Dict[str, int]) -> str:
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir)
        self.conn.execute(
            "INSERT INTO runs (id, kind, task_id, project_id, attempt, fence, adapter, status, boot_id, "
            "wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
            "VALUES (?, 'task', ?, 'prj_1', 1, 1, 'fake', 'running', 'boot_old', ?, ?, ?, ?, ?);",
            (run_id, task_id, g["wrapper"], g["wrapper"], g["wst"], run_dir, now_iso_utc()))
        self.conn.execute(
            "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
            "VALUES (?, ?, 'meshd', 'boot', 1, '2026-01-01T00:00:00Z', '2999-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z');", (f"task:{task_id}", run_id))
        return run_dir

    def _row(self, run_id: str) -> Any:
        return self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()

    def _lease(self, task_id: str) -> Any:
        return self.conn.execute("SELECT * FROM leases WHERE key = ?;", (f"task:{task_id}",)).fetchone()

    def _write_seen(self, run_dir: str, doc: Any = None, raw: Optional[str] = None) -> None:
        tmp = os.path.join(run_dir, "seen.json.tmp-test")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(doc))
        os.replace(tmp, os.path.join(run_dir, "seen.json"))

    def _good(self, run_id: str, g: Dict[str, int]) -> Dict[str, Any]:
        return {"run_id": run_id, "pgid": g["wrapper"], "sid": g["wrapper"],
                "members": [{"pid": g["member"], "starttime": g["mst"]}]}


def _same_alive(pid: int, starttime: int) -> bool:
    info = get_proc_stat(pid)
    return info is not None and info["state"] != "Z" and int(info["starttime"]) == int(starttime)


class TestOneSeenReadPerTick(_SeenDb):
    """r4d item 1 (MAJOR): the target scan and the hold decision use one read of the
    seen state. seen.json is repaired exactly between the two read points of the old
    code (a seam around the target scan / termination step); the run must stay open
    and `task:<id>` held until the repaired member has been targeted and is dead."""

    def _drive(self, run_id: str, task_id: str, g: Dict[str, int]) -> List[str]:
        violations: List[str] = []

        def tick() -> bool:
            row = self._row(run_id)
            if row["status"] not in ("starting", "running"):
                return True
            with mock.patch.object(supervisor, "_log_meshd"):
                supervisor._watch_one(self.conn, self.home, row)
            after = self._row(run_id)
            lease = self._lease(task_id)
            closed = after["status"] not in ("starting", "running")
            # Re-confirm the same member (pid + starttime) after reading the DB state.
            if (closed or lease is None) and _same_alive(g["member"], g["mst"]):
                violations.append(f"run {after['status']}, lease {'held' if lease else 'released'} "
                                  "while the member is alive")
            return closed
        self.assertTrue(_wait_for(tick, timeout=30), f"run never closed: {dict(self._row(run_id))}")
        return violations

    def _assert_closed_after_kill(self, run_id: str, task_id: str, g: Dict[str, int],
                                  violations: List[str]) -> None:
        self.assertEqual(violations, [])
        self.assertFalse(_same_alive(g["member"], g["mst"]), "the repaired member was not killed")
        self.assertIsNone(self._lease(task_id))
        # The member was a termination target: the leftovers event (wrapper-exit path)
        # or the closed run's result (an already running termination) lists it.
        pids = list(json.loads(self._row(run_id)["result_json"] or "{}").get("pids") or [])
        for ev in self.conn.execute("SELECT payload_json FROM events WHERE type = 'run.leftovers_terminated' "
                                    "AND run_id = ?;", (run_id,)).fetchall():
            pids += json.loads(ev[0]).get("pids") or []
        self.assertIn(g["member"], pids)

    def test_repair_between_scan_and_hold_in_watch(self) -> None:
        """ACC: N18, A8 (r4d item 1) - wrapper gone, seen.json unusable, env-less member
        alive: the file is repaired right after the tick's target scan
        (`terminate.run_targets`, before the old code's second read in `seen_hold`)."""
        g = self._spawn_group()
        self._task("tsk_w")
        run_dir = self._run_row("run_w", "tsk_w", g)
        self._write_seen(run_dir, raw="{truncated")
        real = terminate.run_targets
        fired = {"n": 0}

        def seam(*args: Any, **kwargs: Any) -> Any:
            out = real(*args, **kwargs)
            if fired["n"] == 0:
                fired["n"] = 1
                self._write_seen(run_dir, self._good("run_w", g))
            return out

        with mock.patch.object(terminate, "run_targets", seam):
            row = self._row("run_w")
            with mock.patch.object(supervisor, "_log_meshd"):
                supervisor._watch_one(self.conn, self.home, row)
            self.assertEqual(fired["n"], 1, "the seam did not run")
            self.assertTrue(_same_alive(g["member"], g["mst"]))
            self.assertIn(self._row("run_w")["status"], ("starting", "running"),
                          "run closed on the tick whose second read saw the repaired file")
            self.assertIsNotNone(self._lease("tsk_w"), "lease released before the repaired member was scanned")
            violations = self._drive("run_w", "tsk_w", g)
        self._assert_closed_after_kill("run_w", "tsk_w", g, violations)

    def test_repair_between_step_and_hold_in_advance(self) -> None:
        """ACC: N18, A8 (r4d item 1) - the same race while the run is terminating: the
        file is repaired right after `terminate.step` returned `done` (no targets), before
        the old code's hold check re-read it."""
        g = self._spawn_group()
        self._task("tsk_a", status="cancelling")
        run_dir = self._run_row("run_a", "tsk_a", g)
        self._write_seen(run_dir, raw="{truncated")
        with mock.patch.object(supervisor, "_log_meshd"):
            t = supervisor._begin_termination(self.conn, self.home, self._row("run_a"), "cancelled")
        self.assertEqual(t.initial_pids, [])
        real = terminate.step
        fired = {"n": 0}

        def seam(term: Any) -> str:
            out = real(term)
            # step() requires two empty scans a poll apart; fire on the "done" tick.
            if out == "done" and fired["n"] == 0:
                fired["n"] = 1
                self._write_seen(run_dir, self._good("run_a", g))
            return out

        with mock.patch.object(terminate, "step", seam):
            row = self._row("run_a")
            with mock.patch.object(supervisor, "_log_meshd"):
                supervisor._watch_one(self.conn, self.home, row)
                time.sleep(terminate.POLL_S)  # second real tick a poll later (review #20b P3)
                supervisor._watch_one(self.conn, self.home, row)
            self.assertEqual(fired["n"], 1, "the seam did not run")
            self.assertTrue(_same_alive(g["member"], g["mst"]))
            self.assertIn(self._row("run_a")["status"], ("starting", "running"),
                          "run closed on the tick whose second read saw the repaired file")
            self.assertIsNotNone(self._lease("tsk_a"), "lease released before the repaired member was scanned")
            violations = self._drive("run_a", "tsk_a", g)
        self._assert_closed_after_kill("run_a", "tsk_a", g, violations)


class TestSeenUnusableEventStored(_SeenDb):
    """r4d item 2 (MINOR): `run.seen_unusable` is done only once it is stored."""

    def _events(self, run_id: str) -> List[Any]:
        return self.conn.execute("SELECT * FROM events WHERE type = 'run.seen_unusable' AND run_id = ?;",
                                 (run_id,)).fetchall()

    def _held(self, run_id: str, task_id: str) -> Any:
        g = self._spawn_group()
        self._task(task_id)
        run_dir = self._run_row(run_id, task_id, g)
        self._write_seen(run_dir, raw="{truncated")
        row = self._row(run_id)
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(supervisor.seen_hold(row))
        return row

    def test_write_failure_then_retry_records_once(self) -> None:
        """ACC: N18 (r4d item 2) - the first insert fails (injected DB error), the next
        tick stores it; later ticks add nothing: exactly one event."""
        row = self._held("run_e1", "tsk_e1")
        real = supervisor.record_event
        calls = {"n": 0}

        def flaky(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args, **kwargs)

        with mock.patch.object(supervisor, "record_event", flaky), \
                mock.patch.object(supervisor, "_log_meshd"):
            try:
                supervisor.report_seen_unusable(self.conn, row)
            except sqlite3.OperationalError:
                pass  # either surfaced or logged; the event must stay pending
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(self._events("run_e1"), [])
            for _ in range(3):
                supervisor.report_seen_unusable(self.conn, row)
        ev = self._events("run_e1")
        self.assertEqual(len(ev), 1, ev)
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(json.loads(ev[0]["payload_json"]), {"run_id": "run_e1", "reason": "invalid_json"})

    def test_outer_rollback_keeps_it_pending(self) -> None:
        """ACC: N18 (r4d item 2) - called inside a caller's transaction that is rolled
        back: nothing is lost; the next call stores exactly one event."""
        row = self._held("run_e2", "tsk_e2")
        with mock.patch.object(supervisor, "_log_meshd"):
            self.conn.execute("BEGIN IMMEDIATE;")
            try:
                supervisor.report_seen_unusable(self.conn, row)
            finally:
                self.conn.execute("ROLLBACK;")
            self.assertEqual(self._events("run_e2"), [])
            supervisor.report_seen_unusable(self.conn, row)
            supervisor.report_seen_unusable(self.conn, row)
        self.assertEqual(len(self._events("run_e2")), 1)


if __name__ == "__main__":
    unittest.main()
