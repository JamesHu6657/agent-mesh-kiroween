"""Phase 6 (T10) Windows Job Object, termination, close-safety gate, and recovery tests.

Covers every Win32 branch in ``mesh/daemon/{terminate,supervisor,lead_turns,recovery}.py``
via mocked Win32 kernel/Toolhelp/Job primitives on Linux, plus real-Windows Job Object
integration under ``@unittest.skipUnless(sys.platform == "win32", ...)``.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from unittest import mock

import mesh.constants as c
from mesh.adapters.agy import AgyAdapter
from mesh.adapters.base import RunRequest
from mesh.adapters.fake import FakeAdapter
from mesh.adapters.kiro import KiroAdapter
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.proc import ScanResult
from mesh.daemon import children, lead_turns, recovery, supervisor, terminate
from mesh.platform import fs as plat_fs, ipc as plat_ipc, perms as plat_perms, proc as plat_proc

MESH_PROC_LOCK = "shared"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _win_ticks_now(offset_s: float = 0.0) -> int:
    return int((time.time() + offset_s) * terminate._WIN_TICKS_PER_S) + terminate._WIN_EPOCH_OFFSET_100NS


def _reset_daemon_globals() -> None:
    terminate._active.clear()
    terminate._WIN_MY_SID = None
    supervisor._WRAPPERS.clear()
    supervisor._SPAWN_ABORTS.clear()
    supervisor._SEEN.clear()
    supervisor._SEEN_TICKS.clear()
    supervisor._SEEN_UNUSABLE.clear()
    supervisor._SEEN_DIRTY.clear()
    supervisor._SEEN_REASON.clear()
    supervisor._SEEN_SIG.clear()
    supervisor._SEEN_LEGACY.clear()
    supervisor._SEEN_HOLD_TOKEN.clear()
    supervisor._SEEN_MARKER.clear()
    supervisor._SEEN_EVENT_PENDING.clear()
    supervisor._SEEN_EVENT_DONE.clear()
    supervisor._SEEN_PERSISTED.clear()
    supervisor._CLEAR.clear()
    supervisor._CLOSE_REFUSED_LOGGED.clear()
    with supervisor._RUN_JOBS_LOCK:
        for job in supervisor._RUN_JOBS.values():
            try:
                job.close()
            except Exception:
                pass
        supervisor._RUN_JOBS.clear()
        supervisor._RUN_HOMES.clear()
        supervisor._PGID_RUN_INFO.clear()
    with supervisor._WS_PREP_LOCK:
        supervisor._GIT_INFO.clear()
    recovery._HANDED_OVER.clear()
    recovery.DEFERRED.clear()


class _FakeJobHandle:
    """In-memory fake Win32 Named Run Job Object tracking live PIDs and termination."""

    def __init__(self, run_id: str, name: str, *, kill_on_close: bool = False) -> None:
        self.run_id = run_id
        self.name = name
        self.kill_on_close = kill_on_close
        self.pids: Set[int] = set()
        self.closed = False
        self.close_calls = 0
        self.terminated_codes: List[int] = []

    def query_pids(self) -> List[int]:
        if self.closed:
            raise OSError(errno.EBADF, "Job handle closed")
        return sorted(self.pids)

    def terminate(self, exit_code: int = 137) -> None:
        if self.closed:
            raise OSError(errno.EBADF, "Job handle closed")
        self.terminated_codes.append(int(exit_code))
        self.pids.clear()

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self.kill_on_close:
            self.pids.clear()


class _FakeProcEntry:
    def __init__(
        self,
        pid: int,
        ppid: int,
        starttime: int,
        *,
        env: Optional[Dict[str, str]] = None,
        sid: str = "S-1-5-21-1000-1001",
        alive: bool = True,
        stat_exc: Optional[BaseException] = None,
        env_exc: Optional[BaseException] = None,
        sid_exc: Optional[BaseException] = None,
    ) -> None:
        self.pid = int(pid)
        self.ppid = int(ppid)
        self.starttime = int(starttime)
        self.env = {} if env is None else dict(env)
        self.sid = sid
        self.alive = alive
        self.stat_exc = stat_exc
        self.env_exc = env_exc
        self.sid_exc = sid_exc


class _FakeWinKernel:
    """Simulated Win32 kernel process table, Named Run Jobs, and Named Stop Events."""

    def __init__(self, *, my_pid: int = 100, my_sid: str = "S-1-5-21-1000-1001") -> None:
        self.my_pid = my_pid
        self.my_sid = my_sid
        self.procs: Dict[int, _FakeProcEntry] = {
            my_pid: _FakeProcEntry(my_pid, 10, _win_ticks_now(-600), env={"USER": "test"}, sid=my_sid)
        }
        self.jobs: Dict[str, _FakeJobHandle] = {}
        self.signals_sent: List[Tuple[int, int, int, Optional[str], Optional[str]]] = []
        self.stop_events_fired: List[Tuple[int, str, Optional[str]]] = []
        self.list_toolhelp_exc: Optional[BaseException] = None
        self.open_job_exc: Optional[BaseException] = None
        self.query_job_exc: Optional[BaseException] = None

    def add_proc(self, pid: int, ppid: int, starttime: int, **kwargs: Any) -> _FakeProcEntry:
        entry = _FakeProcEntry(pid, ppid, starttime, **kwargs)
        self.procs[int(pid)] = entry
        return entry

    def get_proc_stat(self, pid: int) -> Optional[Dict[str, Any]]:
        entry = self.procs.get(int(pid))
        if entry is None:
            return None
        if entry.stat_exc is not None:
            raise entry.stat_exc
        if not entry.alive:
            return None
        return {
            "state": "R",
            "ppid": entry.ppid,
            "pgrp": 0,
            "session": 0,
            "num_threads": 1,
            "starttime": entry.starttime,
        }

    def is_tgid_alive(self, pid: int, info: Optional[Dict[str, Any]] = None) -> bool:
        _ = info
        entry = self.procs.get(int(pid))
        if entry is None:
            return False
        if entry.stat_exc is not None:
            raise entry.stat_exc
        return bool(entry.alive)

    def is_process_alive(self, pid: int, expected_starttime: Optional[int] = None) -> bool:
        info = self.get_proc_stat(pid)
        if info is None:
            return False
        if expected_starttime is not None and int(info["starttime"]) != int(expected_starttime):
            return False
        return True

    def get_proc_environ(self, pid: int) -> Optional[Dict[str, str]]:
        entry = self.procs.get(int(pid))
        if entry is None or not entry.alive:
            return None
        if entry.env_exc is not None:
            raise entry.env_exc
        return dict(entry.env)

    def get_process_user_sid(self, pid: int) -> str:
        if int(pid) == self.my_pid or int(pid) == os.getpid():
            return self.my_sid
        entry = self.procs.get(int(pid))
        if entry is None:
            raise PermissionError(errno.EACCES, "missing")
        if entry.sid_exc is not None:
            raise entry.sid_exc
        return entry.sid

    def list_toolhelp(self) -> Dict[int, int]:
        if self.list_toolhelp_exc is not None:
            raise self.list_toolhelp_exc
        return {p: e.ppid for p, e in self.procs.items() if e.alive}

    def create_run_job(self, home: str, run_id: str, *, kill_on_close: bool = False) -> _FakeJobHandle:
        name = rf"Local\mesh-job-fake-{run_id}"
        job = _FakeJobHandle(run_id, name, kill_on_close=kill_on_close)
        self.jobs[run_id] = job
        return job

    def open_run_job(self, home: str, run_id: str, *, terminate: bool = True) -> Optional[_FakeJobHandle]:
        _ = (home, terminate)
        if self.open_job_exc is not None:
            raise self.open_job_exc
        job = self.jobs.get(run_id)
        if job is None:
            return None
        reopened = _FakeJobHandle(run_id, job.name, kill_on_close=False)
        reopened.pids = set(job.pids)
        self.jobs[run_id] = reopened
        return reopened

    def query_job_pids(self, job: _FakeJobHandle) -> List[int]:
        if self.query_job_exc is not None:
            raise self.query_job_exc
        return sorted(p for p in job.query_pids() if (p in self.procs and self.procs[p].alive))

    def terminate_job(self, job: _FakeJobHandle, exit_code: int = 137) -> None:
        pids = list(job.pids)
        job.terminate(exit_code=exit_code)
        for p in pids:
            if p in self.procs:
                self.procs[p].alive = False

    def signal_if_same(
        self,
        pid: int,
        starttime: int,
        sig: int,
        run_id: Optional[str] = None,
        home: Optional[str] = None,
    ) -> bool:
        entry = self.procs.get(int(pid))
        if entry is None or not entry.alive or int(entry.starttime) != int(starttime):
            return False
        self.signals_sent.append((int(pid), int(starttime), int(sig), run_id, home))
        if int(sig) == 15 and run_id and home:
            self.stop_events_fired.append((int(pid), run_id, home))
            return True
        if int(sig) == 9:
            entry.alive = False
            for job in self.jobs.values():
                job.pids.discard(int(pid))
            return True
        return True

    def read_bytes_shared(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    @contextlib.contextmanager
    def activate(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "platform", "win32"))
            stack.enter_context(mock.patch.object(os, "getpid", return_value=self.my_pid))
            stack.enter_context(mock.patch.object(terminate, "SIGTERM", 15))
            stack.enter_context(mock.patch.object(terminate, "SIGKILL", 9))
            stack.enter_context(mock.patch.object(plat_proc, "SIGTERM", 15))
            stack.enter_context(mock.patch.object(plat_proc, "SIGKILL", 9))
            stack.enter_context(mock.patch.object(terminate, "get_proc_stat", side_effect=self.get_proc_stat))
            stack.enter_context(mock.patch.object(supervisor, "get_proc_stat", side_effect=self.get_proc_stat))
            stack.enter_context(mock.patch.object(recovery, "get_proc_stat", side_effect=self.get_proc_stat))
            stack.enter_context(mock.patch.object(terminate, "is_tgid_alive", side_effect=self.is_tgid_alive))
            stack.enter_context(mock.patch.object(supervisor, "is_tgid_alive", side_effect=self.is_tgid_alive))
            stack.enter_context(mock.patch.object(recovery, "is_tgid_alive", side_effect=self.is_tgid_alive))
            stack.enter_context(mock.patch.object(terminate, "get_proc_environ", side_effect=self.get_proc_environ))
            stack.enter_context(mock.patch.object(plat_proc, "get_proc_stat", side_effect=self.get_proc_stat))
            stack.enter_context(mock.patch.object(plat_proc, "is_process_alive", side_effect=self.is_process_alive))
            stack.enter_context(mock.patch.object(plat_proc, "list_toolhelp_processes", side_effect=self.list_toolhelp))
            stack.enter_context(mock.patch.object(plat_proc, "get_process_user_sid", side_effect=self.get_process_user_sid))
            stack.enter_context(mock.patch.object(plat_proc, "create_run_job", side_effect=self.create_run_job))
            stack.enter_context(mock.patch.object(plat_proc, "open_run_job", side_effect=self.open_run_job))
            stack.enter_context(mock.patch.object(plat_proc, "query_job_pids", side_effect=self.query_job_pids))
            stack.enter_context(mock.patch.object(plat_proc, "terminate_job", side_effect=self.terminate_job))
            stack.enter_context(mock.patch.object(plat_proc, "signal_if_same", side_effect=self.signal_if_same))
            stack.enter_context(mock.patch.object(plat_fs, "read_bytes_shared", side_effect=self.read_bytes_shared))
            stack.enter_context(mock.patch.object(plat_fs, "atomic_replace", side_effect=os.replace))
            stack.enter_context(mock.patch.object(plat_fs, "fsync_dir", return_value=None))
            stack.enter_context(mock.patch.object(plat_perms, "fchmod_private", return_value=None))
            stack.enter_context(mock.patch.object(plat_perms, "chmod_private", return_value=None))
            stack.enter_context(mock.patch.object(plat_perms, "set_private_dir_acl", return_value=None))
            stack.enter_context(mock.patch.object(plat_perms, "set_private_file_acl", return_value=None))
            stack.enter_context(mock.patch.object(children, "registered", return_value={}))
            yield self


class _FakePopen:
    def __init__(self, pid: int, returncode: Optional[int] = None) -> None:
        self.pid = int(pid)
        self._rc = returncode
        self.wait_calls = 0

    def poll(self) -> Optional[int]:
        return self._rc

    def wait(self, timeout: Optional[float] = None) -> int:
        _ = timeout
        self.wait_calls += 1
        return 0 if self._rc is None else self._rc


def _insert_lease(
    conn: sqlite3.Connection,
    key: str,
    holder: str,
    run_id: str,
    *,
    fence: int = 1,
    agent_id: Optional[str] = "agt_1",
    ttl_s: int = 60,
) -> None:
    now_ts = time.time()
    conn.execute(
        "INSERT INTO leases (key, holder, holder_kind, run_id, agent_id, fence, acquired_at, heartbeat_at, expires_at) "
        "VALUES (?, ?, 'meshd', ?, ?, ?, ?, ?, ?);",
        (key, holder, run_id, agent_id, fence, now_iso_utc(now_ts), now_iso_utc(now_ts), now_iso_utc(now_ts + ttl_s)),
    )


def _seed_base_entities(
    conn: sqlite3.Connection,
    *,
    project_id: str = "prj_1",
    agent_id: str = "agt_1",
    adapter_id: str = "fake",
    binary_path: str = "fake-agent",
    can_lead: int = 0,
    with_lead_assignment: bool = False,
) -> None:
    now = now_iso_utc()
    conn.execute(
        "INSERT OR IGNORE INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
        (now,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, ?);",
        (agent_id, f"agent-{agent_id}", now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO agents (id, adapter, binary_path, delivery, can_lead, capabilities_json, "
        "allowed_models_json, max_concurrency, created_at, updated_at) "
        "VALUES (?, ?, ?, 'launch', ?, '[]', '[\"fake-1\"]', 2, ?, ?);",
        (agent_id, adapter_id, binary_path, can_lead, now, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
        "created_by, created_via, version, created_at, updated_at) "
        "VALUES (?, 'P', 'G', 'active', NULL, 'main', 'mesh/prj/int', 'usr_1', 'direct', 1, ?, ?);",
        (project_id, now, now),
    )
    if with_lead_assignment:
        conn.execute(
            "INSERT OR IGNORE INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES (?, ?, ?, 'usr_1', 'direct', ?);",
            (f"la_{project_id}", project_id, agent_id, now),
        )


def _seed_task_and_run(
    conn: sqlite3.Connection,
    run_dir: str,
    *,
    run_id: str = "run_1",
    task_id: str = "tsk_1",
    project_id: str = "prj_1",
    status: str = "running",
    kind: str = "task",
    wrapper_pid: Optional[int] = 200,
    pgid: Optional[int] = 200,
    proc_starttime: Optional[int] = None,
    adapter_id: str = "fake",
    lease_holder: str = "boot_1",
) -> None:
    os.makedirs(run_dir, exist_ok=True)
    now = now_iso_utc()
    with transaction(conn):
        _seed_base_entities(
            conn,
            project_id=project_id,
            agent_id="agt_1",
            adapter_id=adapter_id,
            can_lead=1,
            with_lead_assignment=(kind == "lead_turn"),
        )
        if kind == "task":
            conn.execute(
                "INSERT OR IGNORE INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, "
                "max_attempts, attempt, fence_seq, created_by, created_at, updated_at) "
                "VALUES (?, ?, 'T', 'Spec', 'general', 'running', 'edit', 600, 3, 1, 1, 'usr_1', ?, ?);",
                (task_id, project_id, now, now),
            )
            conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, adapter, attempt, fence, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, argv_redacted_json, started_at) "
                "VALUES (?, 'task', ?, ?, 'agt_1', ?, 1, 1, ?, 'boot_1', ?, ?, ?, ?, '[]', ?);",
                (run_id, task_id, project_id, adapter_id, status, wrapper_pid, pgid, proc_starttime, run_dir, now),
            )
            _insert_lease(conn, f"task:{task_id}", lease_holder, run_id)
        else:
            conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, adapter, lead_assignment_id, attempt, fence, "
                "status, boot_id, wrapper_pid, pgid, proc_starttime, run_dir, argv_redacted_json, started_at) "
                "VALUES (?, 'lead_turn', NULL, ?, 'agt_1', ?, ?, 0, 0, ?, 'boot_1', ?, ?, ?, ?, '[]', ?);",
                (run_id, project_id, adapter_id, f"la_{project_id}", status, wrapper_pid, pgid, proc_starttime, run_dir, now),
            )
            _insert_lease(conn, f"leadturn:{project_id}", lease_holder, run_id)


class TestWinTerminateTargetsAndSignals(unittest.TestCase):
    def setUp(self) -> None:
        _reset_daemon_globals()
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-win-t10-")
        self.home = self.tmp.name

    def tearDown(self) -> None:
        _reset_daemon_globals()
        self.tmp.cleanup()

    def test_win_helpers_ticks_kthread_subreaper_uid_and_exiting(self) -> None:
        k = _FakeWinKernel()
        now_st = _win_ticks_now(-1.0)
        k.add_proc(200, 100, now_st)
        k.add_proc(300, 1, now_st, sid="S-1-5-18")
        with k.activate():
            self.assertFalse(terminate._is_child_subreaper())
            self.assertTrue(terminate._is_kthread(0, {"ppid": 0}))
            self.assertTrue(terminate._is_kthread(4, {"ppid": 0}))
            self.assertFalse(terminate._is_kthread(200, {"ppid": 100}))
            with mock.patch.object(terminate.time, "time", return_value=1_700_000_000.0):
                ticks = terminate._now_ticks()
                self.assertIsNotNone(ticks)
                self.assertGreater(ticks, terminate._WIN_EPOCH_OFFSET_100NS)
                yf = terminate._young_from(None)
                self.assertEqual(yf, ticks - int(terminate.YOUNG_S * terminate._WIN_TICKS_PER_S))
                older_st = yf - 1000
                self.assertEqual(terminate._young_from(older_st), older_st)
            self.assertTrue(terminate._same_uid(200))
            self.assertFalse(terminate._same_uid(300))
            self.assertFalse(terminate._exiting_or_gone(200, now_st))
            k.procs[200].alive = False
            self.assertTrue(terminate._exiting_or_gone(200, now_st))
            self.assertTrue(terminate._exiting_or_gone(9999, now_st))
            k.procs[201] = _FakeProcEntry(201, 100, now_st, stat_exc=OSError(errno.EIO, "io"))
            self.assertFalse(terminate._exiting_or_gone(201, now_st))

    def test_wrapper_identity_and_descends_from_reject_pid_reuse(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-10.0)
        parent_reused_st = _win_ticks_now(-1.0)
        child_older_st = _win_ticks_now(-5.0)
        k.add_proc(200, 100, w_st)
        k.add_proc(201, 200, _win_ticks_now(-8.0))
        k.add_proc(300, 10, parent_reused_st)  # ppid 300 restarted AFTER 301
        k.add_proc(301, 300, child_older_st)
        k.add_proc(302, 4, child_older_st)  # SYSTEM parent
        with k.activate():
            self.assertTrue(terminate._wrapper_identity_alive(200, w_st))
            k.procs[200].alive = False
            # Dead Win32 process never counts as alive even with ppid == os.getpid()
            self.assertFalse(terminate._wrapper_identity_alive(200, w_st))

            stats = {
                200: {"ppid": 100, "starttime": w_st},
                201: {"ppid": 200, "starttime": k.procs[201].starttime},
                300: {"ppid": 10, "starttime": parent_reused_st},
                301: {"ppid": 300, "starttime": child_older_st},
                302: {"ppid": 4, "starttime": child_older_st},
            }
            anchors = {200: w_st, 300: parent_reused_st}
            self.assertTrue(terminate._descends_from(201, stats, anchors))
            self.assertFalse(terminate._descends_from(301, stats, anchors))
            self.assertFalse(terminate._descends_from(302, stats, anchors))

    def test_run_targets_win_job_envless_peb_toolhelp_and_eacces_scoping(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-2.0)
        c_st = _win_ticks_now(-1.5)
        g_st = _win_ticks_now(-1.0)
        old_sys_st = _win_ticks_now(-200.0)

        job = k.create_run_job(self.home, "run_1", kill_on_close=False)
        job.pids.add(210)
        # Job member with stripped env and broken parent (ppid=9999 nonexistent)
        k.add_proc(210, 9999, c_st, env={})
        # Child descending from Job member with stripped env via Toolhelp
        k.add_proc(211, 210, g_st, env={})
        # PEB carrier outside Job
        k.add_proc(212, 1, c_st, env={"MESH_RUN_ID": "run_1"})
        # Foreign SYSTEM process where OpenProcess raises PermissionError -> must NOT set uncertain
        k.add_proc(400, 4, old_sys_st, sid="S-1-5-18", stat_exc=PermissionError(errno.EACCES, "denied"))
        # Stale unrelated process reusing bare pgid 200 after wrapper died
        reused_st = _win_ticks_now(-0.5)
        k.add_proc(200, 10, reused_st, env={"OTHER": "1"})
        k.add_proc(299, 200, _win_ticks_now(-0.2), env={"OTHER": "1"})

        with k.activate():
            supervisor.register_run_job("run_1", job, pgid=200, wrapper_starttime=w_st, home=self.home)
            unknown: Dict[int, int] = {}
            found = terminate.run_targets("run_1", 200, 200, w_st, unknown=unknown)
            self.assertEqual(dict(found), {210: c_st, 211: g_st, 212: c_st})
            self.assertEqual(unknown, {210: c_st, 211: g_st})
            self.assertFalse(getattr(found, "uncertain", False))
            self.assertNotIn(200, found)
            self.assertNotIn(299, found)

            # Same-user young process whose PEB cannot be read -> fail-closed uncertain=True
            k.add_proc(215, 1, g_st, env_exc=PermissionError(errno.EACCES, "non-dumpable"))
            found_uncertain = terminate.run_targets("run_1", 200, 200, w_st)
            self.assertTrue(getattr(found_uncertain, "uncertain", False))
            k.procs.pop(215)

            # PermissionError on a known seen PID -> preserved AND uncertain=True
            k.add_proc(216, 1, g_st, stat_exc=PermissionError(errno.EACCES, "seen-denied"))
            found_seen = terminate.run_targets("run_1", 200, 200, w_st, seen={216: g_st})
            self.assertEqual(found_seen.get(216), g_st)
            self.assertTrue(getattr(found_seen, "uncertain", False))

            # Windows kernel/foreign-SID/pre-existing unreadable bystanders -> NOT uncertain
            from mesh.platform import _win32 as plat_win32

            saved_216 = k.procs.pop(216)
            try:
                k.add_proc(
                    217,
                    4,
                    w_st,
                    env_exc=plat_win32._NullPebError(errno.EIO, "Target PEB address is NULL"),
                )
                found_null_peb = terminate.run_targets("run_1", 200, 200, w_st)
                self.assertFalse(getattr(found_null_peb, "uncertain", False))
                k.procs.pop(217)

                k.add_proc(
                    218,
                    4,
                    g_st,
                    sid="S-1-5-18",
                    env_exc=PermissionError(errno.EACCES, "system-env"),
                )
                k.add_proc(
                    219,
                    4,
                    g_st,
                    sid="S-1-5-18",
                    stat_exc=PermissionError(errno.EACCES, "system-stat"),
                )
                found_foreign = terminate.run_targets("run_1", 200, 200, w_st)
                self.assertFalse(getattr(found_foreign, "uncertain", False))
                k.procs.pop(218)
                k.procs.pop(219)

                if w_st > 1:
                    k.add_proc(
                        220,
                        1,
                        w_st - 1,
                        env_exc=OSError(errno.EIO, "preexisting-bystander"),
                    )
                    found_old = terminate.run_targets("run_1", 200, 200, w_st)
                    self.assertFalse(getattr(found_old, "uncertain", False))
                    k.procs.pop(220)
            finally:
                k.procs[216] = saved_216

    def test_p2_4_run_targets_win_case_insensitive_mesh_run_id(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-5.0)
        g_st = _win_ticks_now(-2.0)
        k.add_proc(200, 100, w_st, env={"mesh_run_id": "run_case"})
        k.add_proc(221, 1, g_st, env={"Mesh_Run_Id": "run_case"})
        k.add_proc(222, 1, g_st, env={"Mesh_Run_Id": "other_run"})
        with k.activate():
            found = terminate.run_targets("run_case", 200, 200, w_st)
            self.assertEqual(sorted(found.keys()), [200, 221])

    def test_terminate_begin_step_stop_event_and_job_kill(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-5.0)
        c_st = _win_ticks_now(-4.0)
        job = k.create_run_job(self.home, "run_term", kill_on_close=False)
        job.pids.update({200, 201})
        k.add_proc(200, 100, w_st, env={"MESH_RUN_ID": "run_term"})
        k.add_proc(201, 200, c_st, env={})

        with k.activate():
            supervisor.register_run_job("run_term", job, pgid=200, wrapper_starttime=w_st, home=self.home)
            t = terminate.begin("run_term", 200, 200, "cancelled", grace_s=5.0, wrapper_starttime=w_st)
            self.assertEqual(sorted(t.seen), [200, 201])
            self.assertTrue(any(ev == (200, "run_term", self.home) for ev in k.stop_events_fired))

            # Advance past grace_s -> _sigkill_all calls terminate_job AND SIGKILL per target
            t.deadline = time.monotonic() - 1.0
            res1 = terminate.step(t)
            self.assertEqual(res1, "pending")
            self.assertEqual(t.phase, "kill")
            self.assertEqual(job.terminated_codes, [137])

            # Confirm two empty scans >= POLL_S apart
            self.assertEqual(terminate.step(t), "pending")  # first empty scan
            t.empty_scan_at = time.monotonic() - (terminate.POLL_S + 0.05)
            self.assertEqual(terminate.step(t), "done")


class TestWinSupervisorSpawnAbortAndExitNormalization(unittest.TestCase):
    def setUp(self) -> None:
        _reset_daemon_globals()
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-win-sup-")
        self.home = self.tmp.name
        self.db_path = os.path.join(self.home, "mesh.db")
        self.conn = init_db(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        _reset_daemon_globals()
        self.tmp.cleanup()

    def test_normalize_exit_info_and_db_finish_paths(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(supervisor.normalize_exit_info({"exit_code": 137, "signal": None}), (137, None))
        k = _FakeWinKernel()
        with k.activate():
            self.assertEqual(supervisor.normalize_exit_info({"exit_code": 137, "signal": None}), (None, 9))
            self.assertEqual(supervisor.normalize_exit_info({"exit_code": 143, "signal": None}), (None, 15))
            self.assertEqual(supervisor.normalize_exit_info({"exit_code": 0, "signal": None}), (0, None))
            self.assertEqual(supervisor.normalize_exit_info({"exit_code": 1, "signal": None}), (1, None))

            # 1) _finish_run task exit 143 -> signal 15, exit_code None
            rd1 = os.path.join(self.home, "runs", "run_fin1")
            _seed_task_and_run(self.conn, rd1, run_id="run_fin1", task_id="tsk_1", wrapper_pid=None, pgid=None)
            supervisor._finish_run(self.conn, self.conn.execute("SELECT * FROM runs WHERE id='run_fin1'").fetchone(),
                                   {"exit_code": 143, "signal": None, "ended_at": now_iso_utc()}, testing_mode=True)
            row1 = self.conn.execute("SELECT exit_code, signal, result_json FROM runs WHERE id='run_fin1'").fetchone()
            self.assertIsNone(row1["exit_code"])
            self.assertEqual(row1["signal"], 15)
            self.assertEqual(json.loads(row1["result_json"])["signal"], 15)

            # 2) _finalize_termination exit 137 -> signal 9, exit_code None
            rd2 = os.path.join(self.home, "runs", "run_fin2")
            _seed_task_and_run(self.conn, rd2, run_id="run_fin2", task_id="tsk_2", wrapper_pid=None, pgid=None)
            with open(os.path.join(rd2, "exit.json"), "w", encoding="utf-8") as f:
                json.dump({"exit_code": 137, "signal": None, "ended_at": now_iso_utc()}, f)
            t2 = terminate.Termination("run_fin2", None, None, "timeout", 5.0)
            supervisor._finalize_termination(self.conn, self.home,
                                             self.conn.execute("SELECT * FROM runs WHERE id='run_fin2'").fetchone(),
                                             t2, testing_mode=True)
            row2 = self.conn.execute("SELECT exit_code, signal FROM runs WHERE id='run_fin2'").fetchone()
            self.assertIsNone(row2["exit_code"])
            self.assertEqual(row2["signal"], 9)

            # 3) _finalize_lead_turn_termination exit 143 -> signal 15, exit_code None
            rd3 = os.path.join(self.home, "runs", "run_fin3")
            _seed_task_and_run(self.conn, rd3, run_id="run_fin3", kind="lead_turn", wrapper_pid=None, pgid=None)
            t3 = terminate.Termination("run_fin3", None, None, "project_cancelled", 5.0)
            supervisor._finalize_lead_turn_termination(
                self.conn, self.home,
                self.conn.execute("SELECT * FROM runs WHERE id='run_fin3'").fetchone(),
                t3, {"exit_code": 143, "signal": None}, testing_mode=True,
            )
            row3 = self.conn.execute("SELECT exit_code, signal FROM runs WHERE id='run_fin3'").fetchone()
            self.assertIsNone(row3["exit_code"])
            self.assertEqual(row3["signal"], 15)

            # 4) lead_turns.finish_lead_turn exit 137 -> signal 9, exit_code None
            rd4 = os.path.join(self.home, "runs", "run_fin4")
            _seed_task_and_run(self.conn, rd4, run_id="run_fin4", kind="lead_turn", wrapper_pid=None, pgid=None)
            with transaction(self.conn):
                outcome4 = lead_turns.finish_lead_turn(
                    self.conn, self.home,
                    self.conn.execute("SELECT * FROM runs WHERE id='run_fin4'").fetchone(),
                    {"exit_code": 137, "signal": None, "ended_at": now_iso_utc()},
                    testing_mode=True,
                )
            self.assertEqual(outcome4, "retry")
            row4 = self.conn.execute("SELECT exit_code, signal FROM runs WHERE id='run_fin4'").fetchone()
            self.assertIsNone(row4["exit_code"])
            self.assertEqual(row4["signal"], 9)

    def test_apply_cmd_pointer_fallback_handles_fake_agy_and_kiro(self) -> None:
        run_dir = os.path.join(self.home, "run_ptr")
        ws_dir = os.path.join(self.home, "ws")
        os.makedirs(run_dir, exist_ok=True)
        os.makedirs(ws_dir, exist_ok=True)
        prompt_path = os.path.join(run_dir, "prompt.md")
        multiline = "Line one with \"quotes\"\nAnd %TEMP% var"
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(multiline)

        k = _FakeWinKernel()
        with k.activate():
            cmd_shim = os.path.join(self.home, "agent.cmd")
            with open(cmd_shim, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n")

            # Agy argv -> forced pointer on .cmd + multiline + adds --add-dir run_dir
            agy_spec = AgyAdapter().build(RunRequest(
                prompt_path=prompt_path, workspace=ws_dir, model="gemini-3.8-flash-high",
                effort=None, profile="edit", resume_session_id=None, timeout_s=60,
                options={"binary": cmd_shim},
            ))
            st_agy, argv_agy = supervisor.apply_cmd_pointer_fallback(
                list(agy_spec.argv), "argv",
                prompt_path=prompt_path, prompt_text=multiline, run_dir=run_dir, adapter_id="agy",
            )
            self.assertEqual(st_agy, "pointer")
            self.assertNotIn(multiline, argv_agy)
            self.assertIn("Read the file", argv_agy[2])
            idx_add = argv_agy.index("--add-dir")
            self.assertEqual(argv_agy[idx_add + 1], os.path.abspath(run_dir))

            # Kiro argv -> forced pointer on .bat + unsafe prompt
            bat_shim = os.path.join(self.home, "kiro.BAT")
            with open(bat_shim, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n")
            kiro_spec = KiroAdapter().build(RunRequest(
                prompt_path=prompt_path, workspace=ws_dir, model="claude-sonnet-4.5",
                effort=None, profile="edit", resume_session_id=None, timeout_s=60,
                options={"binary": bat_shim},
            ))
            st_kiro, argv_kiro = supervisor.apply_cmd_pointer_fallback(
                list(kiro_spec.argv), "argv",
                prompt_path=prompt_path, prompt_text=multiline, run_dir=run_dir, adapter_id="kiro",
            )
            self.assertEqual(st_kiro, "pointer")
            self.assertIn("Read the file", argv_kiro[-1])

            # Fake .exe stays argv even with multiline prompt
            exe_bin = os.path.join(self.home, "agent.exe")
            st_exe, argv_exe = supervisor.apply_cmd_pointer_fallback(
                [exe_bin, multiline], "argv",
                prompt_path=prompt_path, prompt_text=multiline, run_dir=run_dir, adapter_id="fake",
            )
            self.assertEqual(st_exe, "argv")
            self.assertEqual(argv_exe[1], multiline)

            # PATH resolution to .cmd works too
            with mock.patch.object(supervisor.shutil, "which", return_value=cmd_shim):
                st_path, argv_path = supervisor.apply_cmd_pointer_fallback(
                    ["agy", "--print", multiline], "argv",
                    prompt_path=prompt_path, prompt_text=multiline, run_dir=run_dir, adapter_id="agy",
                )
                self.assertEqual(st_path, "pointer")
                self.assertEqual(argv_path[0], cmd_shim)

            # Expanded cmd.exe metacharacters (&, |, <, >, ^, !) also trigger pointer fallback
            for ch in ('"', "\r", "\n", "%", "&", "|", "<", ">", "^", "!"):
                unsafe = f"check {ch} prompt"
                with open(prompt_path, "w", encoding="utf-8") as f:
                    f.write(unsafe)
                st_ch, argv_ch = supervisor.apply_cmd_pointer_fallback(
                    [cmd_shim, "--print", unsafe, "--output-format", "stream-json"],
                    "argv",
                    prompt_path=prompt_path,
                    prompt_text=unsafe,
                    run_dir=run_dir,
                    adapter_id="agy",
                    profile="edit",
                )
                self.assertEqual(st_ch, "pointer", f"expected pointer fallback for {ch!r}")
                self.assertIn("Read the file", argv_ch[2])
                self.assertIn("--add-dir", argv_ch)

            # kiro under readonly profile cannot read prompt.md: clean stays argv, unsafe refuses
            st_ro_ok, argv_ro_ok = supervisor.apply_cmd_pointer_fallback(
                [bat_shim, "chat", "--trust-tools=", "plain prompt"],
                "argv",
                prompt_path=prompt_path,
                prompt_text="plain prompt",
                run_dir=run_dir,
                adapter_id="kiro",
                profile="readonly",
            )
            self.assertEqual(st_ro_ok, "argv")
            self.assertEqual(argv_ro_ok[-1], "plain prompt")

            for ch in ("&", "|", "<", ">", "^", "!", "%", '"', "\n"):
                with self.assertRaises(ValueError, msg=f"kiro readonly should reject {ch!r}"):
                    supervisor.apply_cmd_pointer_fallback(
                        [bat_shim, "chat", "--trust-tools=", f"bad {ch} prompt"],
                        "argv",
                        prompt_path=prompt_path,
                        prompt_text=f"bad {ch} prompt",
                        run_dir=run_dir,
                        adapter_id="kiro",
                        profile="readonly",
                    )

    def test_spawn_run_kiro_readonly_cmd_unsafe_prompt_fails_without_popen(self) -> None:
        k = _FakeWinKernel()
        now = now_iso_utc()
        ws_dir = os.path.join(self.home, "ws_kiro_ro")
        run_dir = os.path.join(self.home, "runs", "run_kiro_ro")
        cmd_bin = os.path.join(self.home, "kiro-cli.cmd")
        with open(cmd_bin, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
        os.makedirs(os.path.join(self.home, "run"), exist_ok=True)
        with transaction(self.conn):
            _seed_base_entities(
                self.conn, agent_id="agt_kiro", adapter_id="kiro", binary_path=cmd_bin, can_lead=0
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, "
                "max_attempts, attempt, fence_seq, created_by, created_at, updated_at) "
                "VALUES ('tsk_ro', 'prj_1', 'T', 'Spec', 'general', 'queued', 'readonly', 600, 3, 0, 0, "
                "'usr_1', ?, ?);",
                (now, now),
            )
        task_row = self.conn.execute("SELECT * FROM tasks WHERE id = 'tsk_ro'").fetchone()
        agent_row = self.conn.execute("SELECT * FROM agents WHERE id = 'agt_kiro'").fetchone()
        with k.activate(), \
             mock.patch.object(children, "popen_registered") as popen_mock, \
             mock.patch.object(supervisor, "verified_resume_session", return_value=None):
            res = supervisor._spawn_run(
                self.conn, self.home, task_row, agent_row,
                KiroAdapter(), "kiro", "readonly", "claude-sonnet-4.5", None,
                1, 1, 3, "run_kiro_ro", run_dir, ws_dir, "Unsafe & prompt", "argv",
                testing_mode=True,
            )
            self.assertIsNone(res)
            popen_mock.assert_not_called()
            row = self.conn.execute("SELECT status FROM runs WHERE id = 'run_kiro_ro'").fetchone()
            self.assertEqual(row["status"], "spawn_failed")

    def test_watch_launch_runs_preserves_jobs_for_null_wrapper_pid_in_terminate_or_deferred(self) -> None:
        k = _FakeWinKernel()
        rd_t = os.path.join(self.home, "runs", "run_term_null")
        rd_d = os.path.join(self.home, "runs", "run_def_null")
        _seed_task_and_run(
            self.conn, rd_t, run_id="run_term_null", task_id="tsk_t", project_id="prj_t",
            status="starting", wrapper_pid=None, pgid=None,
        )
        _seed_task_and_run(
            self.conn, rd_d, run_id="run_def_null", task_id="tsk_d", project_id="prj_d",
            status="starting", wrapper_pid=None, pgid=None,
        )
        job_t = k.create_run_job(self.home, "run_term_null", kill_on_close=False)
        job_d = k.create_run_job(self.home, "run_def_null", kill_on_close=False)
        job_stale = k.create_run_job(self.home, "run_stale_gone", kill_on_close=False)

        with k.activate():
            supervisor.register_run_job("run_term_null", job_t, home=self.home)
            supervisor.register_run_job("run_def_null", job_d, home=self.home)
            supervisor.register_run_job("run_stale_gone", job_stale, home=self.home)

            terminate._active["run_term_null"] = terminate.Termination(
                "run_term_null", None, None, "lost_before_spawn", 5.0
            )
            recovery.DEFERRED["run_def_null"] = ("boot_1", time.monotonic() + 60.0)

            supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True)

            self.assertFalse(job_t.closed)
            self.assertFalse(job_d.closed)
            self.assertIs(supervisor.get_run_job("run_term_null"), job_t)
            self.assertIs(supervisor.get_run_job("run_def_null"), job_d)
            self.assertEqual(supervisor.get_run_home("run_term_null"), self.home)
            self.assertEqual(supervisor.get_run_home("run_def_null"), self.home)
            self.assertTrue(job_stale.closed)
            self.assertIsNone(supervisor.get_run_job("run_stale_gone"))

    def test_watch_launch_runs_preserves_merge_test_jobs_in_db_or_in_memory(self) -> None:
        from mesh.daemon import merge as _merge

        k = _FakeWinKernel()
        rd_open = os.path.join(self.home, "merges", "mrg_open")
        rd_done = os.path.join(self.home, "merges", "mrg_done")
        _seed_task_and_run(
            self.conn, rd_open, run_id="run_mrg_open", task_id="tsk_m1", project_id="prj_m1",
            status="running", wrapper_pid=None, pgid=None,
        )
        _seed_task_and_run(
            self.conn, rd_done, run_id="run_mrg_done", task_id="tsk_m2", project_id="prj_m2",
            status="exited", wrapper_pid=None, pgid=None,
        )
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET kind = 'merge_test' WHERE id IN ('run_mrg_open', 'run_mrg_done');"
            )
        job_open = k.create_run_job(self.home, "run_mrg_open", kill_on_close=False)
        job_done = k.create_run_job(self.home, "run_mrg_done", kill_on_close=False)
        job_mem = k.create_run_job(self.home, "run_mrg_mem", kill_on_close=False)
        job_pipe = k.create_run_job(self.home, "run_mrg_pipe", kill_on_close=False)

        with _merge._ACTIVE_TEST_LOCK:
            _merge._ACTIVE_TEST_JOB_RUNS.add("run_mrg_mem")
            _merge._ACTIVE_MERGE_RUNS.add("run_mrg_pipe")
        self.addCleanup(lambda: _merge._ACTIVE_TEST_JOB_RUNS.discard("run_mrg_mem"))
        self.addCleanup(lambda: _merge._ACTIVE_MERGE_RUNS.discard("run_mrg_pipe"))

        with k.activate():
            supervisor.register_run_job("run_mrg_open", job_open, home=self.home)
            supervisor.register_run_job("run_mrg_done", job_done, home=self.home)
            supervisor.register_run_job("run_mrg_mem", job_mem, home=self.home)
            supervisor.register_run_job("run_mrg_pipe", job_pipe, home=self.home)

            supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True)

            self.assertFalse(job_open.closed)
            self.assertFalse(job_mem.closed)
            self.assertFalse(job_pipe.closed)
            self.assertIs(supervisor.get_run_job("run_mrg_open"), job_open)
            self.assertIs(supervisor.get_run_job("run_mrg_mem"), job_mem)
            self.assertIs(supervisor.get_run_job("run_mrg_pipe"), job_pipe)
            self.assertTrue(job_done.closed)
            self.assertIsNone(supervisor.get_run_job("run_mrg_done"))

    @unittest.skipIf(sys.platform == "win32", "Linux non-win32 guard hygiene test")
    def test_linux_paths_skip_win32_pointer_and_job_ctx_wiring(self) -> None:
        run_dir = os.path.join(self.home, "runs", "run_linux")
        os.makedirs(run_dir, exist_ok=True)
        prompt_path = os.path.join(run_dir, "prompt.md")
        unsafe = "Line 1 & | < > ^ !\n%HOME%"
        st_arg, argv_arg = supervisor.apply_cmd_pointer_fallback(
            ["agent.cmd", "--print", unsafe],
            "argv",
            prompt_path=prompt_path,
            prompt_text=unsafe,
            run_dir=run_dir,
            adapter_id="agy",
            profile="edit",
        )
        self.assertEqual(st_arg, "argv")
        self.assertEqual(argv_arg, ["agent.cmd", "--print", unsafe])

        st_ptr, argv_ptr = supervisor.apply_cmd_pointer_fallback(
            ["agy", "--print", unsafe],
            "pointer",
            prompt_path=prompt_path,
            prompt_text=unsafe,
            run_dir=run_dir,
            adapter_id="agy",
            profile="edit",
        )
        self.assertEqual(st_ptr, "pointer")
        self.assertEqual(argv_ptr, ["agy", "--print", unsafe])
        self.assertNotIn("--add-dir", argv_ptr)

        _seed_task_and_run(
            self.conn, run_dir, run_id="run_linux", wrapper_pid=999998, pgid=999998, proc_starttime=12345
        )
        run_row = self.conn.execute("SELECT * FROM runs WHERE id = 'run_linux'").fetchone()
        with mock.patch.object(supervisor, "group_still_ours", return_value=True), \
             mock.patch.object(supervisor, "_scan_group", return_value=ScanResult()):
            supervisor.close_gate(run_row, "exited")
            supervisor._SEEN_UNUSABLE.add("run_linux")
            self.assertFalse(supervisor.snapshot_hold(run_row, supervisor.SeenSnapshot({}, True, False)))
            self.assertFalse(supervisor.seen_hold(run_row))
            supervisor._SEEN_UNUSABLE.discard("run_linux")
            supervisor.track_adopted_seen(run_row, self.home)
        self.assertEqual(supervisor._PGID_RUN_INFO, {})
        self.assertEqual(supervisor._RUN_HOMES, {})
        self.assertIsNone(supervisor._SCAN_RUN_CTX.get())

    def test_spawn_run_and_lead_turn_wire_job_object_and_pipe_endpoint(self) -> None:
        k = _FakeWinKernel()
        now = now_iso_utc()
        w_st = _win_ticks_now(-1.0)
        ws_dir = os.path.join(self.home, "ws_task")
        run_dir = os.path.join(self.home, "runs", "run_sp1")
        cmd_bin = os.path.join(self.home, "fake-agent.cmd")
        with open(cmd_bin, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
        os.makedirs(os.path.join(self.home, "run"), exist_ok=True)
        with transaction(self.conn):
            _seed_base_entities(self.conn, agent_id="agt_1", binary_path=cmd_bin, can_lead=1)
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, "
                "max_attempts, attempt, fence_seq, created_by, created_at, updated_at) "
                "VALUES ('tsk_1', 'prj_1', 'T', 'Spec\nline2', 'general', 'queued', 'edit', 600, 3, 0, 0, "
                "'usr_1', ?, ?);",
                (now, now),
            )

        popen_calls: List[Dict[str, Any]] = []

        def fake_popen(role: str, argv: List[str], **kw: Any) -> _FakePopen:
            popen_calls.append({"role": role, "argv": argv, "env": dict(kw["env"]), "job": kw.get("job")})
            pid = 500 + len(popen_calls)
            k.add_proc(pid, k.my_pid, w_st, env=kw["env"])
            if kw.get("job") is not None:
                kw["job"].pids.add(pid)
            return _FakePopen(pid)

        with k.activate(), \
             mock.patch.object(children, "popen_registered", side_effect=fake_popen), \
             mock.patch.object(supervisor, "verified_resume_session", return_value=None):
            task_row = self.conn.execute("SELECT * FROM tasks WHERE id = 'tsk_1'").fetchone()
            rid = supervisor._spawn_run(
                self.conn, self.home, task_row,
                self.conn.execute("SELECT * FROM agents WHERE id = 'agt_1'").fetchone(),
                FakeAdapter(), "fake", "edit", "fake-1", None,
                1, 1, 3, "run_sp1", run_dir, ws_dir, "Line 1\nLine \"2\" %X%", "inline",
                testing_mode=True,
            )
            self.assertEqual(rid, "run_sp1")
            self.assertEqual(len(popen_calls), 1)
            job = popen_calls[0]["job"]
            self.assertIsNotNone(job)
            self.assertIs(supervisor.get_run_job("run_sp1"), job)
            self.assertEqual(popen_calls[0]["env"]["MESH_SOCKET"], plat_ipc.daemon_endpoint_path_win(self.home))
            with open(os.path.join(run_dir, "spec.json"), "r", encoding="utf-8") as f:
                saved_spec = json.load(f)
            self.assertIn("Read the file", " ".join(saved_spec["argv"]))

            # Run is active, _reap_wrapper keeps _RUN_JOBS open until close_gate + _close_run
            run_row = self.conn.execute("SELECT * FROM runs WHERE id = 'run_sp1'").fetchone()
            popen_obj = supervisor._WRAPPERS["run_sp1"]
            popen_obj._rc = 0
            k.procs[501].alive = False
            job.pids.clear()
            supervisor._reap_wrapper(run_row)
            self.assertIs(supervisor.get_run_job("run_sp1"), job)
            self.assertFalse(job.closed)
            with transaction(self.conn):
                supervisor._close_run(self.conn, run_row, "exited", "exited", 0, {"exit_code": 0}, testing_mode=True)
            self.assertIsNone(supervisor.get_run_job("run_sp1"))
            self.assertTrue(job.closed)

            # Lead turn spawn also wires Job Object and pre-Popen cleanup
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                    "VALUES ('la_1', 'prj_1', 'agt_1', 'usr_1', 'direct', ?);",
                    (now,),
                )
            job2 = plat_proc.create_run_job(self.home, "run_lt_pre", kill_on_close=False)
            supervisor.register_run_job("run_lt_pre", job2, home=self.home)
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO runs (id, kind, task_id, project_id, agent_id, adapter, lead_assignment_id, "
                    "fence, attempt, status, boot_id, run_dir, argv_redacted_json, started_at) "
                    "VALUES ('run_lt_pre', 'lead_turn', NULL, 'prj_1', 'agt_1', 'fake', 'la_1', 0, 0, "
                    "'starting', 'boot_1', ?, '[]', ?);",
                    (os.path.join(self.home, "runs", "run_lt_pre"), now),
                )
                _insert_lease(self.conn, "leadturn:prj_1", "boot_1", "run_lt_pre")
            supervisor.handle_spawn_failure(
                self.conn, self.home, "run_lt_pre", os.path.join(self.home, "runs", "run_lt_pre"),
                None, RuntimeError("pre-popen boom"), kind="lead_turn", fail_args=("prj_1",), testing_mode=True,
            )
            self.assertTrue(job2.closed)
            self.assertIsNone(supervisor.get_run_job("run_lt_pre"))

    def test_lead_turn_commit_wires_job_object_and_pipe_endpoint(self) -> None:
        k = _FakeWinKernel()
        now = now_iso_utc()
        w_st = _win_ticks_now(-1.0)
        ws_dir = os.path.join(self.home, "projects", "prj_1", "lead")
        cmd_bin = os.path.join(self.home, "lead.cmd")
        with open(cmd_bin, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
        os.makedirs(ws_dir, exist_ok=True)
        with transaction(self.conn):
            _seed_base_entities(
                self.conn,
                agent_id="agt_1",
                adapter_id="agy",
                binary_path=cmd_bin,
                can_lead=1,
                with_lead_assignment=True,
            )

        popen_calls: List[Dict[str, Any]] = []

        def fake_popen(role: str, argv: List[str], **kw: Any) -> _FakePopen:
            popen_calls.append({"role": role, "argv": argv, "env": dict(kw["env"]), "job": kw.get("job")})
            pid = 601
            k.add_proc(pid, k.my_pid, w_st, env=kw["env"])
            return _FakePopen(pid)

        fake_rendered = types.SimpleNamespace(
            text="Lead\nPrompt",
            bytes=11,
            included_message_ids=[],
            omitted=0,
        )
        lead_dict = {"project_id": "prj_1", "agent_id": "agt_1", "assignment_id": "la_prj_1"}
        agent_row = self.conn.execute("SELECT * FROM agents WHERE id = 'agt_1'").fetchone()
        plan = {
            "project_id": "prj_1",
            "lead": lead_dict,
            "agent": agent_row,
            "adapter_id": "agy",
            "profile": "edit",
            "timeout_s": 60,
            "limit_bytes": 100_000,
            "rendered": fake_rendered,
            "included": [],
            "testing_mode": True,
        }
        with k.activate(), \
             mock.patch.object(children, "popen_registered", side_effect=fake_popen), \
             mock.patch.object(supervisor, "verified_resume_session", return_value=None), \
             mock.patch("mesh.core.policy.resolve_model_and_effort", return_value=("gemini-3.8-flash-high", None)):
            rid = lead_turns.commit_lead_turn(self.conn, self.home, plan)
            self.assertIsNotNone(rid)
            self.assertEqual(len(popen_calls), 1)
            self.assertIsNotNone(popen_calls[0]["job"])
            self.assertIs(supervisor.get_run_job(rid), popen_calls[0]["job"])
            self.assertEqual(popen_calls[0]["env"]["MESH_SOCKET"], plat_ipc.daemon_endpoint_path_win(self.home))
            with open(os.path.join(self.home, "runs", rid, "spec.json"), "r", encoding="utf-8") as f:
                saved_spec = json.load(f)
            self.assertIn("Read the file", " ".join(saved_spec["argv"]))

    def test_spawn_abort_kill_pass_terminates_job_and_closes_handle(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-2.0)
        c_st = _win_ticks_now(-1.0)
        run_dir = os.path.join(self.home, "runs", "run_ab1")
        _seed_task_and_run(
            self.conn, run_dir, run_id="run_ab1", status="starting", wrapper_pid=None, pgid=None
        )
        job = k.create_run_job(self.home, "run_ab1", kill_on_close=False)
        job.pids.update({700, 701})
        k.add_proc(700, k.my_pid, w_st, env={"MESH_RUN_ID": "run_ab1"})
        k.add_proc(701, 700, c_st, env={})
        proc = _FakePopen(700, returncode=None)

        def on_term_job(j: _FakeJobHandle, exit_code: int = 137) -> None:
            k.terminate_job(j, exit_code)
            proc._rc = 137

        with k.activate(), \
             mock.patch.object(plat_proc, "terminate_job", side_effect=on_term_job):
            supervisor.register_run_job("run_ab1", job, pgid=700, wrapper_starttime=w_st, home=self.home)
            children.register(700, "wrapper")
            supervisor.handle_spawn_failure(
                self.conn, self.home, "run_ab1", run_dir, proc,
                RuntimeError("TX B failed"), kind="task",
                fail_args=("tsk_1", "prj_1", 1, 1, 3), testing_mode=True,
            )
            self.assertEqual(job.terminated_codes, [137])
            self.assertTrue(job.closed)
            self.assertIsNone(supervisor.get_run_job("run_ab1"))
            self.assertNotIn("run_ab1", supervisor._SPAWN_ABORTS)
            row = self.conn.execute("SELECT status FROM runs WHERE id = 'run_ab1'").fetchone()
            self.assertEqual(row["status"], "spawn_failed")


class TestWinCloseGateSeenAndRecovery(unittest.TestCase):
    def setUp(self) -> None:
        _reset_daemon_globals()
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-win-rec-")
        self.home = self.tmp.name
        self.db_path = os.path.join(self.home, "mesh.db")
        self.conn = init_db(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        _reset_daemon_globals()
        self.tmp.cleanup()

    def test_scan_group_and_close_gate_hold_on_job_and_exit_json_members(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-10.0)
        c_st = _win_ticks_now(-8.0)
        run_dir = os.path.join(self.home, "runs", "run_cg1")
        _seed_task_and_run(
            self.conn, run_dir, run_id="run_cg1", wrapper_pid=300, pgid=300, proc_starttime=w_st
        )
        job = k.create_run_job(self.home, "run_cg1", kill_on_close=False)
        job.pids.add(301)
        k.add_proc(301, 300, c_st, env={})  # wrapper 300 already exited; env-less child 301 remains

        with k.activate():
            supervisor.register_run_job("run_cg1", job, pgid=300, wrapper_starttime=w_st, home=self.home)
            run_row = self.conn.execute("SELECT * FROM runs WHERE id = 'run_cg1'").fetchone()

            # 1) Held while Job Object still contains live child 301
            with self.assertRaises(supervisor.CloseHeld) as held_ctx:
                supervisor.close_gate(run_row, "exited")
            self.assertTrue(held_ctx.exception.occupied)

            # 2) Clear Job Object, but record live member in exit.json["job_members_at_exit"] -> still held!
            job.pids.clear()
            k.add_proc(302, 1, c_st, env={})
            with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as f:
                json.dump({"exit_code": 0, "signal": None,
                           "job_members_at_exit": [{"pid": 302, "starttime": c_st}]}, f)
            with self.assertRaises(supervisor.CloseHeld) as held_exit:
                supervisor.close_gate(run_row, "exited")
            self.assertEqual(held_exit.exception.live, {302: c_st})

            # 3) Kill 302 -> close_gate clears and _close_run releases Job handle
            k.procs[302].alive = False
            with transaction(self.conn):
                supervisor._close_run(self.conn, run_row, "exited", "exited", 0, {"exit_code": 0}, testing_mode=True)
            self.assertTrue(job.closed)
            self.assertIsNone(supervisor.get_run_job("run_cg1"))

    def test_seen_json_schema_invariants_and_hold_on_win32(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-15.0)
        c_st = _win_ticks_now(-10.0)
        run_dir = os.path.join(self.home, "runs", "run_seen1")
        _seed_task_and_run(
            self.conn, run_dir, run_id="run_seen1", wrapper_pid=400, pgid=400, proc_starttime=w_st
        )
        job = k.create_run_job(self.home, "run_seen1", kill_on_close=False)
        job.pids.update({400, 401})
        k.add_proc(400, 1, w_st, env={"MESH_RUN_ID": "run_seen1"})
        k.add_proc(401, 400, c_st, env={})
        with k.activate():
            supervisor.register_run_job("run_seen1", job, pgid=400, wrapper_starttime=w_st, home=self.home)
            run_row = self.conn.execute("SELECT * FROM runs WHERE id = 'run_seen1'").fetchone()
            self.assertTrue(supervisor.track_adopted_seen(run_row, self.home))
            with open(os.path.join(run_dir, "seen.json"), "r", encoding="utf-8") as f:
                doc = json.load(f)
            self.assertEqual(doc["run_id"], "run_seen1")
            self.assertEqual(doc["pgid"], 400)
            self.assertEqual(doc["sid"], 400)
            self.assertEqual(doc["wrapper_pid"], 400)
            self.assertEqual(doc["wrapper_starttime"], w_st)
            self.assertEqual(doc["members"], [{"pid": 400, "starttime": w_st}, {"pid": 401, "starttime": c_st}])

            # Corrupt seen.json and kill wrapper 400 -> seen_hold + seen_held hold the run
            k.procs[400].alive = False
            job.pids.discard(400)
            supervisor._SEEN.pop("run_seen1", None)
            with open(os.path.join(run_dir, "seen.json"), "w", encoding="utf-8") as f:
                f.write("{corrupt")
            self.assertTrue(supervisor.seen_hold(run_row))
            self.assertFalse(supervisor.seen_held(run_row, self.home))  # 401 is still in Job

            # Drop 401 from Job via unseen handle and check forced release closes Job handle
            supervisor.finish_forced_release(self.home, "run_seen1", "invalid_json", "usr_1")
            self.assertTrue(job.closed)
            self.assertIsNone(supervisor.get_run_job("run_seen1"))

    def test_p1_2_scan_group_win_bystander_is_tgid_alive_and_foreign_uid(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-10.0)
        g_st = _win_ticks_now(-5.0)
        run_dir = os.path.join(self.home, "runs", "run_p12")
        _seed_task_and_run(
            self.conn, run_dir, run_id="run_p12", wrapper_pid=300, pgid=300, proc_starttime=w_st
        )
        k.add_proc(300, 100, w_st, env={"MESH_RUN_ID": "run_p12"})
        # Unknown same-UID bystander created after wrapper; is_tgid_alive fails with non-lookup EIO
        k.add_proc(310, 1, g_st, env={})
        # Foreign-UID bystander (System S-1-5-18) raising EIO in is_tgid_alive
        k.add_proc(311, 4, g_st, sid="S-1-5-18", env={})

        orig_is_tgid = supervisor.is_tgid_alive
        alive_exc_by_pid: dict[int, BaseException] = {
            310: OSError(errno.EIO, "transient-io"),
            311: OSError(errno.EIO, "foreign"),
        }

        def fake_is_tgid(pid: int, info: Any) -> bool:
            if pid in alive_exc_by_pid:
                raise alive_exc_by_pid[pid]
            return orig_is_tgid(pid, info)

        token = supervisor._SCAN_RUN_CTX.set(
            ("run_p12", 300, supervisor.known_starttime(w_st), self.home)
        )
        try:
            with (
                k.activate(),
                mock.patch.object(supervisor, "is_tgid_alive", side_effect=fake_is_tgid),
            ):
                # Same-UID OSError in is_tgid_alive must be caught and mark uncertain=True
                res_same = supervisor._scan_group_win(300)
                self.assertTrue(res_same.uncertain)

                # Once same-UID 310 reports ESRCH (gone) or WinError 87, only foreign 311 fails -> uncertain=False
                alive_exc_by_pid[310] = OSError(errno.ESRCH, "gone")
                res_gone = supervisor._scan_group_win(300)
                self.assertFalse(res_gone.uncertain)

                win87 = OSError(errno.EINVAL, "gone-win87")
                win87.winerror = 87  # type: ignore[attr-defined]
                alive_exc_by_pid[310] = win87
                res_win87 = supervisor._scan_group_win(300)
                self.assertFalse(res_win87.uncertain)
        finally:
            supervisor._SCAN_RUN_CTX.reset(token)

    def test_recovery_adopt_reopens_job_and_double_fault_exit_json_recovers_survivor(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-20.0)
        c_st = _win_ticks_now(-15.0)

        # Case A: live wrapper -> _adopt reopens Named Run Job and seeds seen.json
        rd_adopt = os.path.join(self.home, "runs", "run_rec_adopt")
        _seed_task_and_run(
            self.conn, rd_adopt, run_id="run_rec_adopt", task_id="tsk_a",
            wrapper_pid=500, pgid=500, proc_starttime=w_st, lease_holder="old_boot",
        )
        with open(os.path.join(rd_adopt, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": 500, "starttime": w_st}, f)
        job_a = k.create_run_job(self.home, "run_rec_adopt", kill_on_close=False)
        job_a.pids.update({500, 501})
        k.add_proc(500, 1, w_st, env={"MESH_RUN_ID": "run_rec_adopt"})
        k.add_proc(501, 500, c_st, env={})

        # Case B: double-fault: runwrap wrote exit.json with job_members_at_exit, job vanished on crash,
        # env-less survivor 601 still alive -> _recover_run hands it over & starts terminating!
        rd_df = os.path.join(self.home, "runs", "run_rec_df")
        _seed_task_and_run(
            self.conn, rd_df, run_id="run_rec_df", task_id="tsk_b", project_id="prj_2",
            wrapper_pid=600, pgid=600, proc_starttime=w_st, lease_holder="old_boot",
        )
        with open(os.path.join(rd_df, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": 600, "starttime": w_st}, f)
        with open(os.path.join(rd_df, "exit.json"), "w", encoding="utf-8") as f:
            json.dump({
                "exit_code": 0,
                "signal": None,
                "ended_at": now_iso_utc(),
                "job_members_at_exit": [{"pid": 601, "starttime": c_st}],
            }, f)
        k.add_proc(601, 1, c_st, env={})  # detached env-less survivor; no open Job left

        # Case C: wrapper died before exit.json, exit_members via _lost -> still caught!
        rd_lost = os.path.join(self.home, "runs", "run_rec_lost")
        _seed_task_and_run(
            self.conn, rd_lost, run_id="run_rec_lost", task_id="tsk_c", project_id="prj_3",
            wrapper_pid=700, pgid=700, proc_starttime=w_st, lease_holder="old_boot",
        )
        with open(os.path.join(rd_lost, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": 700, "starttime": w_st}, f)
        with open(os.path.join(rd_lost, "exit.json"), "w", encoding="utf-8") as f:
            json.dump({
                "exit_code": 143,
                "signal": None,
                "ended_at": now_iso_utc(),
                "job_members_at_exit": [{"pid": 701, "starttime": c_st}],
            }, f)
        k.add_proc(701, 1, c_st, env={})

        with k.activate():
            row_a = {"id": "run_rec_adopt", "lease_holder": "old_boot"}
            self.assertEqual(recovery._recover_run(self.conn, self.home, row_a, "boot_2", testing_mode=True), "adopted")
            reopened = supervisor.get_run_job("run_rec_adopt")
            self.assertIsNotNone(reopened)
            self.assertIn(501, reopened.pids)
            self.assertEqual(supervisor.seen_for(self.conn.execute("SELECT * FROM runs WHERE id='run_rec_adopt'").fetchone(), self.home),
                             {500: w_st, 501: c_st})

            row_b = {"id": "run_rec_df", "lease_holder": "old_boot"}
            self.assertEqual(recovery._recover_run(self.conn, self.home, row_b, "boot_2", testing_mode=True), "terminating")
            self.assertIn("run_rec_df", terminate._active)
            self.assertEqual(terminate.get("run_rec_df").seen, {601: c_st})

            row_c = self.conn.execute("SELECT * FROM runs WHERE id='run_rec_lost'").fetchone()
            self.assertEqual(recovery._lost(self.conn, self.home, row_c, "lost_on_restart", "boot_2", testing_mode=True),
                             "terminating")
            self.assertEqual(terminate.get("run_rec_lost").seen, {701: c_st})

    def test_close_gate_and_recovery_lost_scan_job_when_wrapper_pid_reused(self) -> None:
        k = _FakeWinKernel()
        w_st = _win_ticks_now(-20.0)
        reused_st = _win_ticks_now(-1.0)
        c_st = _win_ticks_now(-15.0)
        g_st = _win_ticks_now(-12.0)

        # 1) close_gate: wrapper_pid 300 was reused (group_still_ours == False), no seen.json,
        #    no exit.json, but env-less child 301 (and Toolhelp grandchild 302) live in Job!
        rd_cg = os.path.join(self.home, "runs", "run_cg_reuse")
        _seed_task_and_run(
            self.conn, rd_cg, run_id="run_cg_reuse", task_id="tsk_cg_r", project_id="prj_cg_r",
            wrapper_pid=300, pgid=300, proc_starttime=w_st,
        )
        job_cg = k.create_run_job(self.home, "run_cg_reuse", kill_on_close=False)
        job_cg.pids.add(301)
        k.add_proc(300, 10, reused_st, env={"UNRELATED": "1"})  # PID 300 reused by unrelated proc
        k.add_proc(301, 300, c_st, env={})
        k.add_proc(302, 301, g_st, env={})

        # 2) recovery._lost: wrapper_pid 700 was reused (group_still_ours == False), no seen.json,
        #    no exit.json, but env-less child 701 lives in Named Job Object!
        rd_lost = os.path.join(self.home, "runs", "run_lost_reuse")
        _seed_task_and_run(
            self.conn, rd_lost, run_id="run_lost_reuse", task_id="tsk_l_r", project_id="prj_l_r",
            wrapper_pid=700, pgid=700, proc_starttime=w_st, lease_holder="old_boot",
        )
        job_lost = k.create_run_job(self.home, "run_lost_reuse", kill_on_close=False)
        job_lost.pids.add(701)
        k.add_proc(700, 10, reused_st, env={"UNRELATED": "1"})  # PID 700 reused by unrelated proc
        k.add_proc(701, 700, c_st, env={})

        with k.activate():
            self.assertFalse(supervisor.group_still_ours(300, w_st))
            supervisor.register_run_job("run_cg_reuse", job_cg, pgid=300, wrapper_starttime=w_st, home=self.home)
            run_cg = self.conn.execute("SELECT * FROM runs WHERE id = 'run_cg_reuse'").fetchone()
            with self.assertRaises(supervisor.CloseHeld) as held_ctx:
                supervisor.close_gate(run_cg, "exited")
            self.assertTrue(held_ctx.exception.occupied)
            self.assertNotIn(300, held_ctx.exception.live)

            # Once Job empties, close_gate passes even while unrelated PID 300 stays alive
            k.procs[301].alive = False
            k.procs[302].alive = False
            job_cg.pids.clear()
            supervisor.close_gate(run_cg, "exited")

            self.assertFalse(supervisor.group_still_ours(700, w_st))
            row_lost = self.conn.execute("SELECT * FROM runs WHERE id = 'run_lost_reuse'").fetchone()
            outcome = recovery._lost(self.conn, self.home, row_lost, "lost_on_restart", "boot_2", testing_mode=True)
            self.assertEqual(outcome, "terminating")
            term = terminate.get("run_lost_reuse")
            self.assertIsNotNone(term)
            self.assertIn(701, term.seen)
            self.assertNotIn(700, term.seen)

    def test_recovery_lost_and_scan_helpers_avoid_pgid_run_info_collision(self) -> None:
        k = _FakeWinKernel()
        w_st_a = _win_ticks_now(-30.0)
        w_st_b = _win_ticks_now(-2.0)
        c_st = _win_ticks_now(-1.0)

        # Case A: Run A has empty Job A and dead wrapper 800; Run B reused PID 800 with live child 801
        rd_a = os.path.join(self.home, "runs", "run_col_a")
        rd_b = os.path.join(self.home, "runs", "run_col_b")
        _seed_task_and_run(
            self.conn, rd_a, run_id="run_col_a", task_id="tsk_ca", project_id="prj_ca",
            wrapper_pid=800, pgid=800, proc_starttime=w_st_a, lease_holder="old_boot",
        )
        _seed_task_and_run(
            self.conn, rd_b, run_id="run_col_b", task_id="tsk_cb", project_id="prj_cb",
            wrapper_pid=800, pgid=800, proc_starttime=w_st_b, lease_holder="boot_2",
        )
        job_a = k.create_run_job(self.home, "run_col_a", kill_on_close=False)
        job_b = k.create_run_job(self.home, "run_col_b", kill_on_close=False)
        job_b.pids.update({800, 801})
        k.add_proc(800, 10, w_st_b, env={"MESH_RUN_ID": "run_col_b"})
        k.add_proc(801, 800, c_st, env={})

        # Case B: Run A2 has live child 811 in Job A2; Run B2 reused PID 810 with empty Job B2
        rd_a2 = os.path.join(self.home, "runs", "run_col_a2")
        rd_b2 = os.path.join(self.home, "runs", "run_col_b2")
        _seed_task_and_run(
            self.conn, rd_a2, run_id="run_col_a2", task_id="tsk_ca2", project_id="prj_ca2",
            wrapper_pid=810, pgid=810, proc_starttime=w_st_a, lease_holder="old_boot",
        )
        _seed_task_and_run(
            self.conn, rd_b2, run_id="run_col_b2", task_id="tsk_cb2", project_id="prj_cb2",
            wrapper_pid=810, pgid=810, proc_starttime=w_st_b, lease_holder="boot_2",
        )
        job_a2 = k.create_run_job(self.home, "run_col_a2", kill_on_close=False)
        job_a2.pids.add(811)
        job_b2 = k.create_run_job(self.home, "run_col_b2", kill_on_close=False)
        k.add_proc(810, 10, w_st_b, env={"MESH_RUN_ID": "run_col_b2"})
        k.add_proc(811, 1, c_st, env={})

        with k.activate():
            supervisor.register_run_job("run_col_b", job_b, pgid=800, wrapper_starttime=w_st_b, home=self.home)
            supervisor.register_run_job("run_col_b2", job_b2, pgid=810, wrapper_starttime=w_st_b, home=self.home)

            # Empty Job A must NOT see Run B's child 801 via _PGID_RUN_INFO[800]
            row_a = self.conn.execute("SELECT * FROM runs WHERE id = 'run_col_a'").fetchone()
            self.assertEqual(
                recovery._lost(self.conn, self.home, row_a, "lost_on_restart", "boot_2", testing_mode=True),
                "lost",
            )
            self.assertEqual(supervisor._PGID_RUN_INFO[800][0], "run_col_b")

            # Live Job A2 must still be scanned (catching 811) even though _PGID_RUN_INFO[810] points to run_col_b2
            supervisor.record_run_home("run_col_a2", self.home)
            row_a2 = self.conn.execute("SELECT * FROM runs WHERE id = 'run_col_a2'").fetchone()
            self.assertEqual(
                dict(supervisor.scan_run_group("run_col_a2", 810, w_st_a, self.home)),
                {811: c_st},
            )
            with self.assertRaises(supervisor.CloseHeld):
                supervisor.close_gate(row_a2, "exited")
            supervisor._SEEN_UNUSABLE.add("run_col_a2")
            self.assertTrue(supervisor.snapshot_hold(row_a2, supervisor.SeenSnapshot({}, True, False)))
            self.assertTrue(supervisor.seen_hold(row_a2))
            supervisor._SEEN_UNUSABLE.discard("run_col_a2")

            self.assertEqual(
                recovery._lost(self.conn, self.home, row_a2, "lost_on_restart", "boot_2", testing_mode=True),
                "terminating",
            )
            term_a2 = terminate.get("run_col_a2")
            self.assertIsNotNone(term_a2)
            self.assertEqual(term_a2.seen, {811: c_st})

            # track_adopted_seen and _abort_collect also bind _SCAN_RUN_CTX rather than reading _PGID_RUN_INFO[810]
            k.add_proc(812, 811, c_st, env={})
            self.assertFalse(supervisor.track_adopted_seen(row_a2, self.home))
            ab = supervisor._SpawnAbort(
                _FakePopen(810, returncode=None), "run_col_a2", ("tsk_ca2", "prj_ca2", 1, 1, 3, "err"),
                run_dir=rd_a2, home=self.home,
            )
            ab.wrapper_starttime = w_st_a
            self.assertTrue(supervisor._abort_collect(ab))
            self.assertIn((811, c_st), ab.known)
            self.assertIn((812, c_st), ab.known)

            # Re-verify _PGID_RUN_INFO[810] belongs to live run_col_b2
            supervisor.register_run_job("run_col_b2", job_b2, pgid=810, wrapper_starttime=w_st_b, home=self.home)
            self.assertEqual(supervisor._PGID_RUN_INFO[810][0], "run_col_b2")
            _ = (job_a, job_a2)


@unittest.skipUnless(sys.platform == "win32", "Windows-only real Job Object integration tests")
class TestRealWinJobObjectsAndCloseGate(unittest.TestCase):
    def setUp(self) -> None:
        _reset_daemon_globals()
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-win-real-")
        self.home = os.path.realpath(self.tmp.name)
        self.db_path = os.path.join(self.home, "mesh.db")
        self.conn = init_db(self.db_path)
        # LIFO cleanup order: test process/Job cleanups run first, then DB close, globals reset, tmpdir removal.
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(_reset_daemon_globals)
        self.addCleanup(self.conn.close)

    def _register_job_cleanup(self, job_ref: List[Any], proc: Optional[subprocess.Popen[Any]] = None) -> None:
        def _cleanup() -> None:
            for job in job_ref:
                if job is None:
                    continue
                try:
                    if not getattr(job, "closed", False):
                        plat_proc.terminate_job(job, 137)
                except OSError:
                    pass
                try:
                    if not getattr(job, "closed", False):
                        job.close()
                except Exception:
                    pass
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
                try:
                    children.unregister_if(proc.pid, "wrapper")
                except Exception:
                    pass

        self.addCleanup(_cleanup)

    def _spawn_real_runwrap(
        self,
        run_id: str,
        fake_spec: Dict[str, Any],
        run_job: Any,
        *,
        task_id: str = "tsk_real",
        project_id: str = "prj_real",
    ) -> Tuple[str, subprocess.Popen[Any]]:
        run_dir = os.path.join(self.home, "runs", run_id)
        ws_dir = os.path.join(self.home, "ws", run_id)
        os.makedirs(run_dir, exist_ok=True)
        os.makedirs(ws_dir, exist_ok=True)
        _seed_task_and_run(
            self.conn,
            run_dir,
            run_id=run_id,
            task_id=task_id,
            project_id=project_id,
            wrapper_pid=None,
            pgid=None,
        )
        prompt_path = os.path.join(run_dir, "prompt.md")
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(f"FAKE {json.dumps(fake_spec)}\n")
        with open(os.path.join(run_dir, "spec.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "argv": [sys.executable, "-P", "-s", "-m", "mesh.testing.fake_agent", prompt_path],
                    "cwd": ws_dir,
                    "stdin": "devnull",
                    "idle_timeout_s": 0,
                },
                f,
            )
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT
        env["MESH_HOME"] = self.home
        env["MESH_RUN_ID"] = run_id
        env["MESH_TESTING"] = "1"
        proc = children.popen_registered(
            "wrapper",
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", run_dir],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=ws_dir,
            env=env,
            close_fds=True,
            job=run_job,
        )
        supervisor.register_wrapper(run_id, proc)
        return run_dir, proc

    def test_real_runwrap_stop_event_graceful_termination(self) -> None:
        run_id = "run_real_stop"
        run_job = plat_proc.create_run_job(self.home, run_id, kill_on_close=False)
        jobs: List[Any] = [run_job]
        supervisor.register_run_job(run_id, run_job, home=self.home)

        fake_spec = {"steps": [{"sleep": {"seconds": 0.2, "output_every": 0.2}}, {"sleep": {"seconds": 30}}]}
        run_dir, proc = self._spawn_real_runwrap(run_id, fake_spec, run_job, task_id="tsk_stop", project_id="prj_stop")
        self._register_job_cleanup(jobs, proc)

        stdout_log = os.path.join(run_dir, "stdout.log")
        wrapper_json = os.path.join(run_dir, "wrapper.json")
        deadline = time.monotonic() + 15.0
        ready = False
        while time.monotonic() < deadline:
            if os.path.exists(wrapper_json) and os.path.exists(stdout_log):
                with open(stdout_log, "r", encoding="utf-8", errors="replace") as f:
                    if "Slept" in f.read():
                        ready = True
                        break
            time.sleep(0.05)
        self.assertTrue(ready, "fake_agent did not arm stop watcher in time")

        stat_w = plat_proc.get_proc_stat(proc.pid)
        self.assertIsNotNone(stat_w)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? WHERE id = ?;",
                (proc.pid, proc.pid, stat_w["starttime"], run_id),
            )
        supervisor.register_run_job(run_id, run_job, pgid=proc.pid, wrapper_starttime=stat_w["starttime"], home=self.home)

        # Long grace window ensures terminate.begin sends graceful stop_event (143), never TerminateJobObject
        t = terminate.begin(run_id, proc.pid, proc.pid, "cancelled", grace_s=15.0, wrapper_starttime=stat_w["starttime"])
        end_deadline = time.monotonic() + 12.0
        status = "pending"
        while time.monotonic() < end_deadline:
            status = terminate.step(t)
            if status == "done":
                break
            time.sleep(0.05)
        self.assertEqual(status, "done")
        self.assertEqual(t.phase, "term")
        proc.wait(timeout=10)

        exit_doc = supervisor._read_json_file(os.path.join(run_dir, "exit.json"))
        self.assertIsNotNone(exit_doc)
        self.assertEqual(exit_doc.get("exit_code"), 143)
        self.assertIsNone(exit_doc.get("signal"))

        run_row = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        supervisor._finalize_termination(self.conn, self.home, run_row, t, testing_mode=True)
        final_row = self.conn.execute("SELECT status, exit_code, signal FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertEqual(final_row["status"], "killed")
        self.assertIsNone(final_row["exit_code"])
        self.assertEqual(final_row["signal"], 15)
        self.assertIsNone(supervisor.get_run_job(run_id))

    def test_real_runwrap_job_reopen_and_envless_close_gate(self) -> None:
        run_id = "run_real_reopen"
        run_dir = os.path.join(self.home, "runs", run_id)
        pid_file = os.path.join(run_dir, "envless_pid.txt")
        trigger = os.path.join(run_dir, "release_wrapper.flag")
        run_job = plat_proc.create_run_job(self.home, run_id, kill_on_close=False)
        jobs: List[Any] = [run_job]
        supervisor.register_run_job(run_id, run_job, home=self.home)

        fake_spec = {"steps": [{"spawn_envless_member": {"path": pid_file, "trigger": trigger, "timeout_s": 30}}]}
        _, proc = self._spawn_real_runwrap(run_id, fake_spec, run_job, task_id="tsk_reopen", project_id="prj_reopen")
        self._register_job_cleanup(jobs, proc)

        deadline = time.monotonic() + 15.0
        while not os.path.exists(pid_file) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(pid_file), "envless child did not become ready")
        with open(pid_file, "r", encoding="utf-8") as f:
            child_pid = int(f.read().splitlines()[0].strip())

        stat_w = plat_proc.get_proc_stat(proc.pid)
        self.assertIsNotNone(stat_w)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? WHERE id = ?;",
                (proc.pid, proc.pid, stat_w["starttime"], run_id),
            )

        # Simulate meshd restart: drop meshd's Job handle while runwrap holds the Named Job Object open
        supervisor.close_run_job(run_id)
        supervisor._WRAPPERS.pop(run_id, None)
        self.assertIsNone(supervisor.get_run_job(run_id))

        recovery._win_adopt_run_job(self.home, run_id, pgid=proc.pid, wrapper_starttime=stat_w["starttime"])
        reopened = supervisor.get_run_job(run_id)
        self.assertIsNotNone(reopened)
        jobs.append(reopened)
        self.assertIn(child_pid, set(plat_proc.query_job_pids(reopened)))

        # Let wrapper exit while env-less SIG_IGN child survives in reopened Job Object
        with open(trigger, "w", encoding="utf-8") as f:
            f.write("go\n")
        proc.wait(timeout=15)
        children.unregister_if(proc.pid, "wrapper")

        run_row = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertIsNotNone(supervisor.get_run_job(run_id))

        # close_gate must hold because env-less child is still live in reopened Job Object and exit.json
        with self.assertRaises(supervisor.CloseHeld):
            supervisor.close_gate(run_row, "exited")

        # Termination kills the env-less child via TerminateJobObject / TerminateProcess
        t = terminate.begin(run_id, proc.pid, proc.pid, "leftovers", grace_s=0.1, wrapper_starttime=stat_w["starttime"])
        t.deadline = time.monotonic()
        end_deadline = time.monotonic() + 15.0
        status = "pending"
        while time.monotonic() < end_deadline:
            status = terminate.step(t)
            if status == "done":
                break
            time.sleep(0.1)
        self.assertEqual(status, "done")
        self.assertFalse(plat_proc.is_process_alive(child_pid))

        with transaction(self.conn):
            supervisor._close_run(self.conn, run_row, "exited", "exited", 0, {"exit_code": 0}, testing_mode=True)
        self.assertIsNone(supervisor.get_run_job(run_id))
        self.assertTrue(reopened.closed)

    def test_real_close_gate_holds_after_wrapper_pid_reuse(self) -> None:
        run_id = "run_real_reuse"
        run_dir = os.path.join(self.home, "runs", run_id)
        pid_file = os.path.join(run_dir, "envless_pid.txt")
        trigger = os.path.join(run_dir, "release_wrapper.flag")
        run_job = plat_proc.create_run_job(self.home, run_id, kill_on_close=False)
        jobs: List[Any] = [run_job]
        supervisor.register_run_job(run_id, run_job, home=self.home)

        fake_spec = {"steps": [{"spawn_envless_member": {"path": pid_file, "trigger": trigger, "timeout_s": 30}}]}
        _, proc = self._spawn_real_runwrap(run_id, fake_spec, run_job, task_id="tsk_reuse", project_id="prj_reuse")
        self._register_job_cleanup(jobs, proc)

        deadline = time.monotonic() + 15.0
        while not os.path.exists(pid_file) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(pid_file), "envless child did not become ready")
        with open(pid_file, "r", encoding="utf-8") as f:
            child_pid = int(f.read().splitlines()[0].strip())

        with open(trigger, "w", encoding="utf-8") as f:
            f.write("go\n")
        proc.wait(timeout=15)
        children.unregister_if(proc.pid, "wrapper")
        supervisor._WRAPPERS.pop(run_id, None)

        # Remove exit.json so only the live Job Object covers child_pid
        exit_json = os.path.join(run_dir, "exit.json")
        if os.path.exists(exit_json):
            os.unlink(exit_json)

        # Spawn an unrelated live process and point wrapper_pid/pgid at it with an older starttime
        # so real GetProcessTimes proves PID reuse (group_still_ours == False).
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: (unrelated.kill() if unrelated.poll() is None else None, unrelated.wait(timeout=5)))
        stat_u = plat_proc.get_proc_stat(unrelated.pid)
        self.assertIsNotNone(stat_u)
        older_st = max(1, int(stat_u["starttime"]) - 10_000_000)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? WHERE id = ?;",
                (unrelated.pid, unrelated.pid, older_st, run_id),
            )
        supervisor.register_run_job(run_id, run_job, pgid=unrelated.pid, wrapper_starttime=older_st, home=self.home)
        run_row = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertFalse(supervisor.group_still_ours(unrelated.pid, older_st))

        # close_gate must still scan the real Job Object and hold due to child_pid
        with self.assertRaises(supervisor.CloseHeld) as held_ctx:
            supervisor.close_gate(run_row, "exited")
        self.assertTrue(held_ctx.exception.occupied)
        self.assertNotIn(unrelated.pid, held_ctx.exception.live)

        # Terminate Job Object -> close_gate now clears even while unrelated process remains alive
        plat_proc.terminate_job(run_job, 137)
        wait_dead = time.monotonic() + 10.0
        while plat_proc.is_process_alive(child_pid) and time.monotonic() < wait_dead:
            time.sleep(0.05)
        self.assertFalse(plat_proc.is_process_alive(child_pid))
        with transaction(self.conn):
            supervisor._close_run(self.conn, run_row, "exited", "exited", 0, {"exit_code": 0}, testing_mode=True)
        self.assertIsNone(supervisor.get_run_job(run_id))
        self.assertIsNone(unrelated.poll())


if __name__ == "__main__":
    unittest.main()
