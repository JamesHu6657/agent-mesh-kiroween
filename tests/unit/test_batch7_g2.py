"""Focused unit/DB/supervisor/recovery regression tests for batch7 g2 findings."""

import asyncio
import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, Optional
from unittest import mock

if sys.platform != "win32":
    import fcntl
else:
    fcntl = None  # type: ignore[assignment]

import mesh.constants as c
import mesh.core.schema as schema_mod
from mesh.adapters import evidence, smoke_schema
from mesh.adapters.base import Capability, RunRequest
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.vibe import VibeAdapter
from mesh.core.auth import generate_run_token
from mesh.core.clock import now_iso_utc, parse_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.proc import get_proc_stat, is_tgid_alive
from mesh.daemon import children, lead_turns, recovery, supervisor, terminate
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.run import handle_run_release
from mesh.daemon.handlers.task import handle_task_retry
from mesh.daemon.server import DaemonServer
from mesh.errors import MeshError, SchemaTooNewError
from mesh.platform import proc as plat_proc
from mesh.platform.paths import temp_root
from tests import tierlib

MESH_PROC_LOCK = "shared"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_INIT = os.path.join(
    REPO_ROOT, "tests", "fixtures", "real", "droid", "1.3.0", "exec-init-event-opus55-high.json"
)

_GROUP_SPAWN = (
    "import os, signal, sys\n"
    "mode = sys.argv[1]\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    if mode == 'strip':\n"
    "        os.execve(sys.executable, [sys.executable, '-c',\n"
    "            'import os, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); '\n"
    "            'os.write(1, b\"%d\\\\n\" % os.getpid()); r, w = os.pipe(); os.read(r, 1)'], {})\n"
    "    os.write(1, b'%d\\n' % os.getpid())\n"
    "    r, w = os.pipe()\n"
    "    os.read(r, 1)\n"
    "    os._exit(0)\n"
    "sys.stdin.read()\n"
)


def _wait_until(pred, timeout: float = 10.0, step: float = 0.03) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def _read_line_bounded(proc: subprocess.Popen, timeout: float = 10.0) -> str:
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buf += chunk
    if b"\n" in buf:
        return buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    raise AssertionError(f"child pid {proc.pid}: no readiness line within {timeout:g}s (got {buf!r})")


def _reap_if_child(pid: int, st: int, proc: Optional[subprocess.Popen] = None) -> None:
    if proc is not None:
        try:
            proc.poll()
        except Exception:
            pass
        return
    info = get_proc_stat(pid)
    if info is not None and int(info["starttime"]) == int(st) and int(info["ppid"]) == os.getpid():
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass


def _is_pid_dead(pid: int, st: int, proc: Optional[subprocess.Popen] = None) -> bool:
    """True once ``(pid, st)`` has disappeared, been recycled, or reached state Z/X
    with no live threads (reaping first when parented to this process)."""
    _reap_if_child(pid, st, proc)
    info = get_proc_stat(pid)
    if info is None or int(info["starttime"]) != int(st):
        return True
    return info["state"] in ("Z", "X") and not is_tgid_alive(pid, info)


class _Batch7G2Base(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-b7g2-", dir=temp_root())
        self.addCleanup(shutil.rmtree, self.home, True)
        for sub in ("logs", "run", "runs", "workspaces"):
            os.makedirs(os.path.join(self.home, sub), mode=0o700, exist_ok=True)
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self._reset_globals()
        self.addCleanup(self._reset_globals)
        self._seed_base()

    def _reset_globals(self) -> None:
        supervisor._evict_seen(set())
        supervisor._SEEN_EVENT_PENDING.clear()
        supervisor._SEEN_EVENT_DONE.clear()
        supervisor._SEEN_PERSISTED.clear()
        supervisor._SEEN_HOLD_TOKEN.clear()
        supervisor._CLEAR.clear()
        supervisor._SPAWN_ABORTS.clear()
        supervisor._WRAPPERS.clear()
        recovery.DEFERRED.clear()
        for rid in list(terminate.active()):
            terminate.forget(rid)

    def _seed_base(self) -> None:
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO boots (id, pid, started_at, crashed) VALUES ('boot_cur', ?, ?, 0);",
                (os.getpid(), now),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'worker', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, default_profile, max_unapproved_profile, "
                "default_model, allowed_models_json, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_1', 'fake', 'launch', 1, 'edit', 'dev', 'model-a', '[\"model-a\",\"model-b\"]', 2, ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'Proj', 'Goal', 'active', 'usr_1', 'direct', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES ('lead_1', 'prj_1', 'agt_1', 'usr_1', 'direct', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES ('kill.grace_s', '1', ?, 'usr_1');",
                (now,),
            )
            record_event(
                conn=self.conn,
                type_="project.created",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_1",
            )
            self.conn.execute("DELETE FROM outbox;")

    def _insert_task(
        self,
        task_id: str = "tsk_1",
        status: str = "running",
        model: str = "model-a",
        last_session_id: Optional[str] = None,
    ) -> None:
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, model, "
                "profile, timeout_s, max_attempts, attempt, fence_seq, last_session_id, created_by, created_at, updated_at) "
                "VALUES (?, 'prj_1', 'Task', 'Spec', 'general', ?, 'agt_1', ?, 'edit', 600, 3, 1, 1, ?, 'usr_1', ?, ?);",
                (task_id, status, model, last_session_id, now, now),
            )

    def _spawn_detached_group(self, run_id: str, keep_env: bool) -> Dict[str, Any]:
        env = {"MESH_RUN_ID": run_id} if keep_env else {}
        mode = "keep" if keep_env else "strip"
        leader = subprocess.Popen(
            [sys.executable, "-c", _GROUP_SPAWN, mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        assert leader.stdout is not None and leader.stdin is not None
        member_pid = int(_read_line_bounded(leader))
        try:
            leader.stdout.close()
        except OSError:
            pass
        leader_stat = get_proc_stat(leader.pid)
        member_stat = get_proc_stat(member_pid)
        assert leader_stat is not None and member_stat is not None
        leader_st = int(leader_stat["starttime"])
        member_st = int(member_stat["starttime"])

        def _cleanup() -> None:
            for pid, st in ((member_pid, member_st), (leader.pid, leader_st)):
                info = get_proc_stat(pid)
                if info is not None and int(info["starttime"]) == st and info["state"] not in ("Z", "X"):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            for pipe in (leader.stdin, leader.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            try:
                leader.wait(timeout=5)
            except Exception:
                pass
            _wait_until(
                lambda: _is_pid_dead(leader.pid, leader_st, leader) and _is_pid_dead(member_pid, member_st),
                timeout=5.0,
            )

        self.addCleanup(_cleanup)
        return {
            "leader_proc": leader,
            "wrapper_pid": leader.pid,
            "pgid": leader.pid,
            "wrapper_st": leader_st,
            "member_pid": member_pid,
            "member_st": member_st,
        }

    def _stop_group_leader(self, grp: Dict[str, Any]) -> None:
        leader: subprocess.Popen = grp["leader_proc"]
        for pipe in (leader.stdin, leader.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        leader.wait(timeout=5)
        self.assertTrue(
            _wait_until(lambda: _is_pid_dead(grp["wrapper_pid"], grp["wrapper_st"], leader), timeout=5.0),
            f"leader {grp['wrapper_pid']} did not exit: {get_proc_stat(grp['wrapper_pid'])}",
        )

    def _kill_pid(
        self,
        pid: int,
        st: int,
        proc: Optional[subprocess.Popen] = None,
        check: bool = True,
    ) -> None:
        if sys.platform == "win32":
            terminate.signal_if_same(pid, st, plat_proc.SIGKILL)
        else:
            info = get_proc_stat(pid)
            if info is not None and int(info["starttime"]) == st and info["state"] not in ("Z", "X"):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        dead = _wait_until(lambda: _is_pid_dead(pid, st, proc), timeout=5.0)
        if check:
            self.assertTrue(dead, f"pid {pid} did not die: {get_proc_stat(pid)}")


class TestBatch7G2Unit(_Batch7G2Base):
    @unittest.skipUnless(sys.platform != "win32", "POSIX os.fork/PGID detached carrier and seen_held PGID scan (Windows carrier close-gate covered in test_win_close_gate_jobs)")
    def test_f1_seen_held_rejects_live_carrier_and_prior_members(self) -> None:
        """Finding 1: `seen_held` / `run release --force-seen-unusable` must refuse while a
        `MESH_RUN_ID` carrier, `prior_members` entry, or `t.seen` target is still alive."""
        self._insert_task("tsk_f1", status="running")
        run_id = "run_f1"
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir, mode=0o700, exist_ok=True)

        # Spawn group with an unauthenticated occupant AND a separate MESH_RUN_ID carrier.
        grp = self._spawn_detached_group(run_id, keep_env=False)
        self._stop_group_leader(grp)
        self.assertFalse(_is_pid_dead(grp["member_pid"], grp["member_st"]))

        carrier = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"MESH_RUN_ID": run_id},
        )
        carrier_st = int(get_proc_stat(carrier.pid)["starttime"])
        self.addCleanup(lambda: self._kill_pid(carrier.pid, carrier_st, carrier, check=False))

        prior = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={},
        )
        prior_st = int(get_proc_stat(prior.pid)["starttime"])
        self.addCleanup(lambda: self._kill_pid(prior.pid, prior_st, prior, check=False))

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at) "
                "VALUES (?, 'task', 'tsk_f1', 'prj_1', 'agt_1', 1, 1, 'fake', 'running', 'boot_old', ?, ?, ?, ?, ?, ?);",
                (run_id, grp["wrapper_pid"], grp["pgid"], grp["wrapper_st"], run_dir, now, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('task:tsk_f1', ?, 'meshd', 'boot_cur', 'agt_1', 1, ?, ?, ?);",
                (run_id, now, now, now),
            )

        with open(os.path.join(run_dir, "seen.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "mesh_seen_marker": "unusable",
                    "run_id": run_id,
                    "reason": "adopt_scan_untrusted",
                    "prior_members": [{"pid": prior.pid, "starttime": prior_st}],
                },
                f,
            )

        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertTrue(supervisor.seen_hold(run))
        # Both carrier and prior_members are alive -> seen_held MUST be False.
        self.assertFalse(supervisor.seen_held(run))

        ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_f1_1",
            home=self.home,
            cmd="run.release",
            args={"run_id": run_id, "force_seen_unusable": True},
            testing_mode=True,
        )
        with self.assertRaises(MeshError) as cm:
            with transaction(self.conn):
                handle_run_release(ctx)
        self.assertEqual(cm.exception.code, c.INVALID_TRANSITION)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'task:tsk_f1';").fetchone())

        # Kill carrier; prior_members is STILL alive -> seen_held MUST still be False.
        self._kill_pid(carrier.pid, carrier_st, carrier)
        self.assertFalse(supervisor.seen_held(run))

        # Kill prior_members, but register a live SIGTERM-ignoring pid in terminate.get(run_id).seen -> still False.
        self._kill_pid(prior.pid, prior_st, prior)
        extra = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "os.write(1, b'ok\\n'); time.sleep(60)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
        )
        self.assertEqual(_read_line_bounded(extra), "ok")
        extra.stdout.close()
        extra_st = int(get_proc_stat(extra.pid)["starttime"])
        self.addCleanup(lambda: self._kill_pid(extra.pid, extra_st, extra, check=False))
        terminate.begin(run_id, grp["pgid"], grp["wrapper_pid"], "lost_on_restart", 1,
                        wrapper_starttime=grp["wrapper_st"], seen={extra.pid: extra_st})
        self.assertFalse(supervisor.seen_held(run))

        # Once extra dies too, only the unauthenticated group occupant remains -> seen_held is True!
        self._kill_pid(extra.pid, extra_st, extra)
        self.assertTrue(_wait_until(lambda: supervisor.seen_held(run), timeout=5.0))
        with transaction(self.conn):
            res = handle_run_release(ctx)
        self.assertTrue(res.data["released"])
        self._kill_pid(grp["member_pid"], grp["member_st"])

    def test_f2_post_popen_spawn_abort_recovery_and_shutdown_drain(self) -> None:
        """Finding 2: post-Popen spawn abort never adopts a revoked-token wrapper or auto-submits
        an aborted exit.json, and `_drain_for_shutdown` drains `_SPAWN_ABORTS`."""
        # Case A: live wrapper + revoked run_token -> terminated as spawn_failed, never adopted.
        self._insert_task("tsk_f2a", status="running")
        run_a = "run_f2a"
        dir_a = os.path.join(self.home, "runs", run_a)
        os.makedirs(dir_a, mode=0o700, exist_ok=True)
        clean_env = (
            {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")}
            if sys.platform == "win32"
            else {}
        )
        live_wrap = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=clean_env,
        )
        wrap_st = int(get_proc_stat(live_wrap.pid)["starttime"])
        self.addCleanup(lambda: self._kill_pid(live_wrap.pid, wrap_st, live_wrap, check=False))
        with open(os.path.join(dir_a, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": live_wrap.pid, "pgid": live_wrap.pid, "starttime": wrap_st}, f)

        # Case B: dead wrapper + exit.json (exit_code=0) + spawn_abort_pending -> spawn_failed, never T7 auto-submit.
        self._insert_task("tsk_f2b", status="running")
        run_b = "run_f2b"
        dir_b = os.path.join(self.home, "runs", run_b)
        os.makedirs(dir_b, mode=0o700, exist_ok=True)
        dead_wrap = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=clean_env,
        )
        dead_stat = get_proc_stat(dead_wrap.pid)
        dead_st = int(dead_stat["starttime"]) if dead_stat is not None else 1
        dead_wrap.wait(timeout=5)
        self.assertTrue(_wait_until(lambda: _is_pid_dead(dead_wrap.pid, dead_st, dead_wrap), timeout=5.0))
        with open(os.path.join(dir_b, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": dead_wrap.pid, "pgid": dead_wrap.pid, "starttime": dead_st}, f)
        with open(os.path.join(dir_b, "exit.json"), "w", encoding="utf-8") as f:
            json.dump({"exit_code": 0, "signal": None, "ended_at": now_iso_utc()}, f)
        with open(os.path.join(dir_b, "seen.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"mesh_seen_marker": "unusable", "run_id": run_b, "reason": "spawn_abort_pending", "prior_members": []},
                f,
            )

        now = now_iso_utc()
        _, lookup_a, hash_a = generate_run_token()
        with transaction(self.conn):
            for rid, tid, rdir in ((run_a, "tsk_f2a", dir_a), (run_b, "tsk_f2b", dir_b)):
                self.conn.execute(
                    "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                    "boot_id, run_dir, started_at, deadline_at) "
                    "VALUES (?, 'task', ?, 'prj_1', 'agt_1', 1, 1, 'fake', 'starting', 'boot_old', ?, ?, ?);",
                    (rid, tid, rdir, now, now),
                )
                self.conn.execute(
                    "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                    "VALUES (?, ?, 'meshd', 'boot_old', 'agt_1', 1, ?, ?, ?);",
                    (f"task:{tid}", rid, now, now, now),
                )
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at, revoked_at) "
                "VALUES ('cred_f2a', 'agt_1', 'run_token', ?, ?, ?, ?, ?, ?);",
                (lookup_a, hash_a, json.dumps({"role": "executor", "run_id": run_a}), now, now, now),
            )

        summary = recovery.recover(self.conn, self.home, "boot_cur")
        self.assertEqual(summary["runs_adopted"], 0)
        self.assertEqual(summary["runs_exited"], 0)

        # run_b had no live processes -> closed immediately as spawn_failed, task failed (NOT submitted!).
        rb_row = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_b,)).fetchone()
        tb_row = self.conn.execute("SELECT status, failure_json FROM tasks WHERE id = 'tsk_f2b';").fetchone()
        self.assertEqual((rb_row["status"], rb_row["end_reason"]), ("spawn_failed", "spawn_failed"))
        self.assertEqual(tb_row["status"], "failed")
        self.assertEqual(json.loads(tb_row["failure_json"])["reason"], "spawn_failed")

        # run_a had a live wrapper -> terminating with end_reason='spawn_failed'; drive termination to completion.
        t_a = terminate.get(run_a)
        self.assertIsNotNone(t_a)
        self.assertEqual(t_a.end_reason, "spawn_failed")

        def _run_a_closed() -> bool:
            live_wrap.poll()
            supervisor.advance_terminations(self.conn, self.home)
            return self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_a,)).fetchone()["status"] == "spawn_failed"

        self.assertTrue(_wait_until(_run_a_closed, timeout=10.0))
        live_wrap.wait(timeout=5)
        self.assertTrue(_wait_until(lambda: _is_pid_dead(live_wrap.pid, wrap_st, live_wrap), timeout=5.0))
        ra_row = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_a,)).fetchone()
        self.assertEqual((ra_row["status"], ra_row["end_reason"]), ("spawn_failed", "spawn_failed"))

        # Case C: DaemonServer._drain_for_shutdown drains _SPAWN_ABORTS.
        self._insert_task("tsk_f2c", status="running")
        run_c = "run_f2c"
        dir_c = os.path.join(self.home, "runs", run_c)
        os.makedirs(dir_c, mode=0o700, exist_ok=True)
        ab_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=clean_env,
        )
        ab_st = int(get_proc_stat(ab_proc.pid)["starttime"])
        self.addCleanup(lambda: self._kill_pid(ab_proc.pid, ab_st, ab_proc, check=False))
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at) "
                "VALUES (?, 'task', 'tsk_f2c', 'prj_1', 'agt_1', 1, 1, 'fake', 'starting', 'boot_cur', ?, ?, ?, ?, ?, ?);",
                (run_c, ab_proc.pid, ab_proc.pid, ab_st, dir_c, now, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('task:tsk_f2c', ?, 'meshd', 'boot_cur', 'agt_1', 1, ?, ?, ?);",
                (run_c, now, now, now),
            )
        children.hold(ab_proc.pid)
        self.addCleanup(children.unhold, ab_proc.pid)
        ab = supervisor._SpawnAbort(
            ab_proc,
            run_c,
            ("tsk_f2c", "prj_1", 1, 1, 3, "RuntimeError: tx b failed"),
            run_dir=dir_c,
            home=self.home,
            kind="task",
        )
        ab.wrapper_starttime = ab_st
        ab.out = {"pid": ab_proc.pid, "starttime": ab_st}
        supervisor._SPAWN_ABORTS[run_c] = ab

        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        asyncio.run(srv._drain_for_shutdown(grace_s=2.0, settle_s=0.1))
        ab_proc.wait(timeout=5)
        self.assertTrue(_wait_until(lambda: _is_pid_dead(ab_proc.pid, ab_st, ab_proc), timeout=5.0))
        self.assertNotIn(run_c, supervisor._SPAWN_ABORTS)
        rc_row = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_c,)).fetchone()
        self.assertEqual((rc_row["status"], rc_row["end_reason"]), ("spawn_failed", "spawn_failed"))

    def test_f3_finish_run_without_exit_json_cancelling_and_pending_failure(self) -> None:
        """Finding 3: missing `exit.json` completes `cancelling -> cancelled` and preserves
        fenced `pending_failure` (`reason='reported'`) across supervisor, recovery, and release."""
        now = now_iso_utc()
        # 1. cancelling task + _finish_run(..., exit_info=None) -> cancelled
        self._insert_task("tsk_f3_cancel", status="cancelling")
        dir_1 = os.path.join(self.home, "runs", "run_f3_1")
        os.makedirs(dir_1, mode=0o700, exist_ok=True)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, run_dir, started_at, deadline_at) "
                "VALUES ('run_f3_1', 'task', 'tsk_f3_cancel', 'prj_1', 'agt_1', 1, 1, 'fake', 'running', 'boot_cur', ?, ?, ?);",
                (dir_1, now, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('task:tsk_f3_cancel', 'run_f3_1', 'meshd', 'boot_cur', 'agt_1', 1, ?, ?, ?);",
                (now, now, now),
            )
        r1 = self.conn.execute("SELECT * FROM runs WHERE id = 'run_f3_1';").fetchone()
        out1 = supervisor._finish_run(self.conn, r1, exit_info=None)
        self.assertEqual(out1, "cancelled")
        t1 = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_f3_cancel';").fetchone()
        self.assertEqual(t1["status"], "cancelled")

        # 2. running task with pending_failure + _finish_run(..., exit_info=None) -> reported
        self._insert_task("tsk_f3_rep1", status="running")
        dir_2 = os.path.join(self.home, "runs", "run_f3_2")
        os.makedirs(dir_2, mode=0o700, exist_ok=True)
        pending_doc = json.dumps(
            {"pending_failure": {"message": "worker hit fatal invariant", "actor_id": "agt_1", "actor_kind": "agent", "via": "run_token"}}
        )
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, run_dir, result_json, started_at, deadline_at) "
                "VALUES ('run_f3_2', 'task', 'tsk_f3_rep1', 'prj_1', 'agt_1', 1, 1, 'fake', 'running', 'boot_cur', ?, ?, ?, ?);",
                (dir_2, pending_doc, now, now),
            )
        r2 = self.conn.execute("SELECT * FROM runs WHERE id = 'run_f3_2';").fetchone()
        out2 = supervisor._finish_run(self.conn, r2, exit_info=None)
        self.assertEqual(out2, "reported")
        f2 = json.loads(self.conn.execute("SELECT failure_json FROM tasks WHERE id = 'tsk_f3_rep1';").fetchone()[0])
        self.assertEqual(f2["reason"], "reported")
        self.assertEqual(f2["details"]["message"], "worker hit fatal invariant")

        # 3. recovery._close_without_process preserves pending_failure -> reported
        self._insert_task("tsk_f3_rep2", status="running")
        dir_3 = os.path.join(self.home, "runs", "run_f3_3")
        os.makedirs(dir_3, mode=0o700, exist_ok=True)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, run_dir, result_json, started_at, deadline_at) "
                "VALUES ('run_f3_3', 'task', 'tsk_f3_rep2', 'prj_1', 'agt_1', 1, 1, 'fake', 'running', 'boot_old', ?, ?, ?, ?);",
                (dir_3, pending_doc, now, now),
            )
        r3 = self.conn.execute("SELECT * FROM runs WHERE id = 'run_f3_3';").fetchone()
        recovery._close_without_process(self.conn, r3, "lost_on_restart")
        f3 = json.loads(self.conn.execute("SELECT failure_json FROM tasks WHERE id = 'tsk_f3_rep2';").fetchone()[0])
        self.assertEqual(f3["reason"], "reported")

    def test_f4_finish_lead_turn_rejects_capture_error_and_vendor_failure(self) -> None:
        """Finding 4: `finish_lead_turn` treats `capture_error` and `vendor_failure_meaning`
        as failed turns (`retry`), preserving `lead_wake` messages instead of acking them."""
        now = now_iso_utc()
        for idx, extra_exit, vendor_err in (
            (1, {"capture_error": "ENOSPC: stdout.log"}, None),
            (2, {}, "quota_exceeded"),
        ):
            run_id = f"run_f4_{idx}"
            msg_id = f"msg_f4_{idx}"
            run_dir = os.path.join(self.home, "runs", run_id)
            os.makedirs(run_dir, mode=0o700, exist_ok=True)
            with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
                f.write('```mesh-actions\n[{"cmd":"project.note","args":{"text":"should not run"}}]\n```\n')
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, status, boot_id, "
                    "run_dir, started_at, deadline_at, lead_assignment_id) "
                    "VALUES (?, 'lead_turn', 'prj_1', 'agt_1', 0, 'fake', 'running', 'boot_cur', ?, ?, ?, 'lead_1');",
                    (run_id, run_dir, now, now),
                )
                self.conn.execute(
                    "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, "
                    "payload_json, status, attempts, next_attempt_at, delivering_run_id, created_at) "
                    "VALUES (?, 1, 'agt_1', 'lead_wake', ?, 'prj_1', 'project.created', '{}', 'delivering', 0, ?, ?, ?);",
                    (msg_id, f"dedup_f4_{idx}", now, run_id, now),
                )
            run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
            exit_info: Dict[str, Any] = {"exit_code": 0, "signal": None, "ended_at": now}
            exit_info.update(extra_exit)
            with mock.patch.object(supervisor, "vendor_failure_meaning", return_value=vendor_err):
                with transaction(self.conn):
                    outcome = lead_turns.finish_lead_turn(self.conn, self.home, run, exit_info, testing_mode=True)
            self.assertEqual(outcome, "retry")
            msg = self.conn.execute("SELECT status, attempts FROM outbox WHERE id = ?;", (msg_id,)).fetchone()
            self.assertEqual((msg["status"], msg["attempts"]), ("pending", 1))
            note_ev = self.conn.execute(
                "SELECT 1 FROM events WHERE type = 'project.note_added' AND run_id = ?;", (run_id,)
            ).fetchone()
            self.assertIsNone(note_ev)

    def test_f7_recovery_outbox_respects_max_attempts_dead_unreachable_and_backoff(self) -> None:
        """Finding 7: `_step_outbox` and `_release_outbox` transition exhausted rows to `dead`,
        emit `lead.unreachable` for `lead_wake`, and apply wake backoff on retry."""
        now_ts = time.time()
        now = now_iso_utc(now_ts)
        with transaction(self.conn):
            # Exhausted lead_wake (attempts=2 of max_attempts=3) -> dead + lead.unreachable
            self.conn.execute(
                "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, "
                "payload_json, status, attempts, next_attempt_at, delivering_run_id, created_at) "
                "VALUES ('msg_wake_dead', 1, 'agt_1', 'lead_wake', 'd_wd', 'prj_1', 'task.submitted', '{}', "
                "'delivering', 2, ?, 'run_dead_turn', ?);",
                (now, now),
            )
            # Retryable lead_wake (attempts=0) -> pending with >= 30s backoff
            self.conn.execute(
                "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, "
                "payload_json, status, attempts, next_attempt_at, delivering_run_id, created_at) "
                "VALUES ('msg_wake_retry', 1, 'agt_1', 'lead_wake', 'd_wr', 'prj_1', 'task.submitted', '{}', "
                "'delivering', 0, ?, 'run_dead_turn', ?);",
                (now, now),
            )
            # Exhausted inbox (attempts=2 of max_attempts=3) -> dead
            self.conn.execute(
                "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, "
                "payload_json, status, attempts, next_attempt_at, created_at) "
                "VALUES ('msg_inbox_dead', 1, 'agt_1', 'inbox', 'd_id', 'prj_1', 'task.offered', '{}', "
                "'delivering', 2, ?, ?);",
                (now, now),
            )

        requeued = recovery._step_outbox(self.conn, adopted=set())
        self.assertEqual(requeued, 1)

        wd = self.conn.execute("SELECT status, attempts, delivering_run_id FROM outbox WHERE id = 'msg_wake_dead';").fetchone()
        self.assertEqual((wd["status"], wd["attempts"], wd["delivering_run_id"]), ("dead", 3, None))

        wr = self.conn.execute("SELECT status, attempts, next_attempt_at FROM outbox WHERE id = 'msg_wake_retry';").fetchone()
        self.assertEqual((wr["status"], wr["attempts"]), ("pending", 1))
        self.assertGreaterEqual(parse_utc(wr["next_attempt_at"]).timestamp(), now_ts + 28.0)

        ib = self.conn.execute("SELECT status, attempts FROM outbox WHERE id = 'msg_inbox_dead';").fetchone()
        self.assertEqual((ib["status"], ib["attempts"]), ("dead", 3))

        unreachable = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'lead.unreachable' AND project_id = 'prj_1';"
        ).fetchall()
        self.assertEqual(len(unreachable), 1)
        self.assertEqual(json.loads(unreachable[0]["payload_json"])["messages"], ["msg_wake_dead"])

    @unittest.skipUnless(sys.platform != "win32", "POSIX os.fork/PGID detached carrier and seen_held PGID scan (Windows carrier close-gate covered in test_win_close_gate_jobs)")
    def test_f8_adopted_lead_turn_seen_tracking_close_gate_and_forced_release(self) -> None:
        """Finding 8: adopted `lead_turn` tracks `seen.json`, terminates env-stripped leftovers,
        enforces `close_gate`, and supports `run release --force-seen-unusable`."""
        run_id = "run_f8_lt"
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        grp = self._spawn_detached_group(run_id, keep_env=False)
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, status, boot_id, "
                "wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at, lead_assignment_id) "
                "VALUES (?, 'lead_turn', 'prj_1', 'agt_1', 0, 'fake', 'running', 'boot_old', ?, ?, ?, ?, ?, ?, 'lead_1');",
                (run_id, grp["wrapper_pid"], grp["pgid"], grp["wrapper_st"], run_dir, now, now_iso_utc(time.time() + 600)),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('leadturn:prj_1', ?, 'meshd', 'boot_cur', 'agt_1', 0, ?, ?, ?);",
                (run_id, now, now, now),
            )
            self.conn.execute(
                "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, "
                "payload_json, status, attempts, next_attempt_at, delivering_run_id, created_at) "
                "VALUES ('msg_f8', 1, 'agt_1', 'lead_wake', 'd_f8', 'prj_1', 'project.created', '{}', "
                "'delivering', 0, ?, ?, ?);",
                (now, run_id, now),
            )

        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        # Tick while adopted wrapper is alive -> writes seen.json recording env-stripped member!
        def _seen_recorded() -> bool:
            supervisor._watch_one(self.conn, self.home, run)
            st_code, mems = supervisor.load_seen_state(run)
            return st_code == supervisor.SEEN_OK and mems.get(grp["member_pid"]) == grp["member_st"]

        self.assertTrue(_wait_until(_seen_recorded, timeout=5.0))
        state, members = supervisor.load_seen_state(run)
        self.assertEqual(state, supervisor.SEEN_OK)
        self.assertEqual(members.get(grp["member_pid"]), grp["member_st"])

        # close_gate MUST refuse closing a lead_turn while recorded member is alive.
        self.assertFalse(_is_pid_dead(grp["member_pid"], grp["member_st"]))
        with self.assertRaises(supervisor.CloseHeld):
            supervisor.close_gate(run, "exited")

        # Wrapper exits cleanly, leaving the env-stripped SIGTERM-ignoring member in pgid.
        with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as f:
            json.dump({"exit_code": 0, "signal": None, "ended_at": now_iso_utc()}, f)
        self._stop_group_leader(grp)
        self.assertFalse(_is_pid_dead(grp["member_pid"], grp["member_st"]))

        # Watch pass uses seen.json to start leftovers termination and kill the env-stripped child!
        supervisor._watch_one(self.conn, self.home, run)
        self.assertIsNotNone(terminate.get(run_id))

        def _turn_closed() -> bool:
            supervisor.advance_terminations(self.conn, self.home)
            _reap_if_child(grp["member_pid"], grp["member_st"])
            return self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()["status"] == "exited"

        self.assertTrue(_wait_until(_turn_closed, timeout=10.0))
        self.assertTrue(
            _wait_until(lambda: _is_pid_dead(grp["member_pid"], grp["member_st"]), timeout=5.0),
            f"expected member {grp['member_pid']} to be dead, got {get_proc_stat(grp['member_pid'])}",
        )
        self.assertEqual(self.conn.execute("SELECT status FROM outbox WHERE id = 'msg_f8';").fetchone()[0], "acked")

        # Part 2: adopted lead_turn held by unusable seen.json supports forced release and cleans meta + outbox.
        run2_id = "run_f8_hold"
        run2_dir = os.path.join(self.home, "runs", run2_id)
        os.makedirs(run2_dir, mode=0o700, exist_ok=True)
        grp2 = self._spawn_detached_group(run2_id, keep_env=False)
        self._stop_group_leader(grp2)
        self.assertFalse(_is_pid_dead(grp2["member_pid"], grp2["member_st"]))
        with open(os.path.join(run2_dir, "seen.json"), "w", encoding="utf-8") as f:
            json.dump({"mesh_seen_marker": "unusable", "run_id": run2_id, "reason": "adopt_scan_untrusted"}, f)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, status, boot_id, "
                "wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at, lead_assignment_id) "
                "VALUES (?, 'lead_turn', 'prj_1', 'agt_1', 0, 'fake', 'running', 'boot_old', ?, ?, ?, ?, ?, ?, 'lead_1');",
                (run2_id, grp2["wrapper_pid"], grp2["pgid"], grp2["wrapper_st"], run2_dir, now, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('leadturn:prj_1', ?, 'meshd', 'boot_cur', 'agt_1', 0, ?, ?, ?);",
                (run2_id, now, now, now),
            )
            self.conn.execute(
                "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, "
                "payload_json, status, attempts, next_attempt_at, delivering_run_id, created_at) "
                "VALUES ('msg_f8_2', 1, 'agt_1', 'lead_wake', 'd_f8_2', 'prj_1', 'project.created', '{}', "
                "'delivering', 0, ?, ?, ?);",
                (now, run2_id, now),
            )
        run2 = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run2_id,)).fetchone()
        supervisor._watch_one(self.conn, self.home, run2)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM meta WHERE key = ?;", (supervisor.seen_hold_key(run2_id),)).fetchone()
        )
        self.assertTrue(_wait_until(lambda: supervisor.seen_held(run2, self.home), timeout=5.0))

        ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_f8_rel",
            home=self.home,
            cmd="run.release",
            args={"run_id": run2_id, "force_seen_unusable": True},
            testing_mode=True,
        )
        with transaction(self.conn):
            handle_run_release(ctx)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM meta WHERE key = ?;", (supervisor.seen_hold_key(run2_id),)).fetchone()
        )
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'leadturn:prj_1';").fetchone())
        msg2 = self.conn.execute("SELECT status, attempts, delivering_run_id FROM outbox WHERE id = 'msg_f8_2';").fetchone()
        self.assertEqual((msg2["status"], msg2["attempts"], msg2["delivering_run_id"]), ("pending", 1, None))
        self._kill_pid(grp2["member_pid"], grp2["member_st"])

    def test_f9_plan_lead_turn_slices_wake_batch_before_rendering_prompt(self) -> None:
        """Finding 9: `plan_lead_turn` slices `LEAD_WAKE_BATCH_MAX` (50) BEFORE rendering
        `prompt.md`, so messages 51+ never appear in turn 1's rendered prompt."""
        now = "2020-01-01T00:00:00Z"
        with transaction(self.conn):
            for i in range(55):
                marker = f"WAKE_BATCH_MARKER_{i:02d}"
                record_event(
                    conn=self.conn,
                    type_="project.request_received",
                    actor_id="usr_1",
                    actor_kind="user",
                    via="direct",
                    project_id="prj_1",
                    payload={"message": marker},
                )
        lead = {"assignment_id": "lead_1", "agent_id": "agt_1"}
        messages = lead_turns.pending_lead_wake(self.conn, "prj_1", "agt_1", now_iso_utc())
        self.assertEqual(len(messages), 55)

        plan = lead_turns.plan_lead_turn(self.conn, self.home, "prj_1", lead, messages, testing_mode=True)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(len(plan["included"]), lead_turns.LEAD_WAKE_BATCH_MAX)
        prompt_text = plan["rendered"].text
        self.assertIn("WAKE_BATCH_MARKER_00", prompt_text)
        self.assertIn("WAKE_BATCH_MARKER_49", prompt_text)
        for i in range(50, 55):
            self.assertNotIn(f"WAKE_BATCH_MARKER_{i:02d}", prompt_text)

    def test_f10_vendor_smoke_evidence_enables_resume_and_model_change_clears_session(self) -> None:
        """Finding 10: `verified_resume_session` checks smoke evidence (`S-X-2` + `S-X-3`),
        and `task retry --model` clears `last_session_id` so Vibe uses the new model."""
        # Part A: vendor smoke evidence check on fresh DroidAdapter (static verification == 'help').
        stage = os.path.join(self.home, "smoke_stage")
        results_dir = os.path.join(stage, "results")
        binary = os.path.join(self.home, "bin", "droid.cmd" if sys.platform == "win32" else "droid")
        os.makedirs(os.path.dirname(binary), exist_ok=True)
        with open(binary, "w", encoding="utf-8") as f:
            if sys.platform == "win32":
                f.write("@echo off\r\nexit /b 0\r\n")
            else:
                f.write("#!/bin/sh\nexit 0\n")
        if sys.platform != "win32":
            os.chmod(binary, 0o755)
        version = "1.3.0"
        probed_at = now_iso_utc(time.time() + 10)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO adapter_probes (adapter, probed_at, binary_path, version, help_sha256, "
                "flags_json, models_json, status) VALUES ('droid', ?, ?, ?, NULL, '[]', NULL, 'partially_verified');",
                (probed_at, binary, version),
            )
        with open(REAL_INIT, "rb") as f:
            raw = f.read()

        def _make_case(cid: str, status: str = "pass") -> Dict[str, Any]:
            name = f"smoke-{cid}-stdout.jsonl"
            rel = f"droid/{version}/{name}"
            if not os.path.exists(os.path.join(stage, rel)):
                tierlib.write_capture(stage, rel, raw, ["droid", "exec"])
            return {
                "id": cid,
                "status": status,
                "reason": None if status == "pass" else "failed",
                "rc": 0,
                "duration_s": 1.0,
                "fixture": smoke_schema.fixture_rel("droid", version, name),
                "fixture_sha256": tierlib.sha256_bytes(raw),
                "observation": None,
                "evaluated_from": None,
                "carried_from": None,
            }

        droid = DroidAdapter()
        droid.capabilities["resume"] = Capability("resume", supported="yes", verification="help", note="-s/--session-id")
        self.assertEqual(droid.capabilities["resume"].verification, "help")

        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: results_dir, "MESH_TESTING_MODE": "1"}):
            # S-X-2 pass, S-X-3 fail -> resume NOT verified
            doc_fail = {
                "schema": smoke_schema.SCHEMA,
                "adapter": "droid",
                "binary": binary,
                "version": version,
                "captured_at_utc": "2026-11-20T01:02:03Z",
                "mesh_commit": "abc1234",
                "model": "claude-sonnet-5-5",
                "effort": "low",
                "profile": "readonly",
                "timeout_s": 180,
                "consent_ref": "James 2026-11-09 12:44",
                "cases": [_make_case("S-X-1"), _make_case("S-X-2", "pass"), _make_case("S-X-3", "fail"), _make_case("S-X-6")],
            }
            name1 = smoke_schema.results_name("droid", version, "2026-11-20", [])
            tierlib.write_capture(stage, f"results/{name1}", json.dumps(doc_fail).encode(), ["test"])
            # Old signature had 2 args; check via start_launch_run / verified_resume_session cleanly:
            self.assertIsNone(
                supervisor.verified_resume_session(droid, "sid-123", self.conn, True, binary)
                if supervisor.verified_resume_session.__code__.co_argcount >= 3
                else supervisor.verified_resume_session(droid, "sid-123")
            )

            # Both S-X-2 and S-X-3 pass -> resume IS verified!
            doc_pass = dict(
                doc_fail,
                captured_at_utc="2026-11-21T01:02:03Z",
                cases=[_make_case("S-X-1"), _make_case("S-X-2", "pass"), _make_case("S-X-3", "pass"), _make_case("S-X-6")],
            )
            name2 = smoke_schema.results_name("droid", version, "2026-11-21", os.listdir(results_dir))
            tierlib.write_capture(stage, f"results/{name2}", json.dumps(doc_pass).encode(), ["test"])
            got_sid = (
                supervisor.verified_resume_session(droid, "sid-123", self.conn, True, binary)
                if supervisor.verified_resume_session.__code__.co_argcount >= 3
                else supervisor.verified_resume_session(droid, "sid-123")
            )
            self.assertEqual(got_sid, "sid-123")

        # Part B: task retry --model clears last_session_id so Vibe passes --model instead of --resume.
        now = now_iso_utc()
        self._insert_task("tsk_f10", status="failed", model="model-a", last_session_id="sid-old")
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, model, "
                "status, boot_id, run_dir, session_id, started_at, deadline_at) "
                "VALUES ('run_f10_1', 'task', 'tsk_f10', 'prj_1', 'agt_1', 1, 1, 'fake', 'model-a', "
                "'exited', 'boot_cur', ?, 'sid-old', ?, ?);",
                (os.path.join(self.home, "runs", "run_f10_1"), now, now),
            )
        # Same model retry preserves last_session_id.
        ctx_same = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_f10_same",
            home=self.home,
            cmd="task.retry",
            args={"task_id": "tsk_f10"},
            testing_mode=True,
        )
        with transaction(self.conn):
            handle_task_retry(ctx_same)
        self.assertEqual(
            self.conn.execute("SELECT last_session_id FROM tasks WHERE id = 'tsk_f10';").fetchone()[0],
            "sid-old",
        )

        # Model change retry clears last_session_id -> VibeAdapter uses --model model-b!
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET status = 'failed' WHERE id = 'tsk_f10';")
        ctx_diff = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_f10_diff",
            home=self.home,
            cmd="task.retry",
            args={"task_id": "tsk_f10", "model": "model-b"},
            testing_mode=True,
        )
        with transaction(self.conn):
            handle_task_retry(ctx_diff)
        t_after = self.conn.execute("SELECT model, last_session_id FROM tasks WHERE id = 'tsk_f10';").fetchone()
        self.assertEqual(t_after["model"], "model-b")
        self.assertIsNone(t_after["last_session_id"])

        vibe_spec = VibeAdapter().build(
            RunRequest(
                prompt_path=os.path.join(self.home, "prompt.md"),
                workspace=self.home,
                model=t_after["model"],
                effort=None,
                profile="edit",
                timeout_s=60,
                resume_session_id=t_after["last_session_id"],
                options={},
            )
        )
        self.assertNotIn("--resume", vibe_spec.argv)
        self.assertIn("--model", vibe_spec.argv)
        self.assertEqual(vibe_spec.argv[vibe_spec.argv.index("--model") + 1], "model-b")

    @unittest.skipUnless(sys.platform != "win32", "POSIX fcntl.flock and /proc/self/fd leak checks (Windows locking covered in test_platform_locks_perms)")
    def test_f11_lock_storage_startup_and_registered_exec_cleanup(self) -> None:
        """Finding 11: failed `acquire_lock` / `setup_storage` / `run_server` never leak fds or
        delete a live daemon's `meshd.pid` / `mesh.sock`, and `create_subprocess_exec_registered`
        kills and awaits `proc` if `register()` raises."""
        # Part A: live lock held -> second DaemonServer fails cleanly without touching meshd.pid / mesh.sock.
        run_dir = os.path.join(self.home, "run")
        lock_path = os.path.join(run_dir, "meshd.lock")
        pid_path = os.path.join(run_dir, "meshd.pid")
        sock_path = os.path.join(run_dir, "mesh.sock")
        held_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, held_fd)
        fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(pid_path, "w", encoding="utf-8") as f:
            f.write("99999")
        with open(sock_path, "w", encoding="utf-8") as f:
            f.write("live-sock")

        def _open_fds_for(target: str) -> int:
            real_target = os.path.realpath(target)
            count = 0
            for entry in os.listdir("/proc/self/fd"):
                try:
                    if os.path.realpath(os.readlink(f"/proc/self/fd/{entry}")) == real_target:
                        count += 1
                except OSError:
                    pass
            return count

        # Close self.conn first so SQLite does not defer unixClose fd release for same-process POSIX locks.
        with transaction(self.conn):
            pass
        self.conn.close()
        db_path = os.path.join(self.home, "mesh.db")
        self.assertEqual(_open_fds_for(db_path), 0)
        lock_fds_before = _open_fds_for(lock_path)
        srv = DaemonServer(home=self.home, testing_mode=True)
        with self.assertRaises(MeshError) as cm:
            asyncio.run(srv.run_server())
        self.assertEqual(cm.exception.code, c.DAEMON_ALREADY_RUNNING)
        self.assertIsNone(srv.lock_fd)
        self.assertIsNone(srv.db_conn)
        self.assertTrue(os.path.exists(pid_path))
        with open(pid_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "99999")
        self.assertTrue(os.path.exists(sock_path))
        self.assertEqual(_open_fds_for(lock_path), lock_fds_before)
        self.assertEqual(_open_fds_for(db_path), 0)

        # Part B: setup_storage failure closes db_conn and leaves zero open DB fds.
        c_bump = sqlite3.connect(db_path)
        with c_bump:
            c_bump.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version';",
                (str(schema_mod.CURRENT_SCHEMA_VERSION + 1),),
            )
        c_bump.close()
        srv2 = DaemonServer(home=self.home, testing_mode=True)
        with self.assertRaises(SchemaTooNewError):
            srv2.setup_storage()
        self.assertIsNone(srv2.db_conn)
        self.assertEqual(_open_fds_for(db_path), 0)

        # Part C: create_subprocess_exec_registered kills & awaits proc if register() raises.
        spawned_holder: Dict[str, Any] = {}
        orig_create = asyncio.create_subprocess_exec

        async def _spy_create(*argv: Any, **kwargs: Any) -> Any:
            p = await orig_create(*argv, **kwargs)
            info = get_proc_stat(p.pid)
            spawned_holder["proc"] = p
            spawned_holder["st"] = int(info["starttime"]) if info is not None else None
            return p

        self.addCleanup(
            lambda: self._kill_pid(spawned_holder["proc"].pid, spawned_holder["st"], check=False)
            if spawned_holder.get("proc") is not None and spawned_holder.get("st") is not None
            else None
        )

        async def _run_failing_register() -> None:
            with mock.patch("asyncio.create_subprocess_exec", side_effect=_spy_create):
                with mock.patch.object(children, "register", side_effect=OSError("proc stat error")):
                    await children.create_subprocess_exec_registered(
                        "stub", sys.executable, "-c", "import time; time.sleep(60)"
                    )

        with self.assertRaises(OSError):
            asyncio.run(_run_failing_register())
        leaked_proc = spawned_holder["proc"]
        self.assertIsNotNone(leaked_proc.returncode)
        if spawned_holder.get("st") is not None:
            self.assertTrue(_wait_until(lambda: _is_pid_dead(leaked_proc.pid, spawned_holder["st"]), timeout=5.0))
        self.assertEqual(children.spawns_in_flight(), 0)


if __name__ == "__main__":
    unittest.main()
