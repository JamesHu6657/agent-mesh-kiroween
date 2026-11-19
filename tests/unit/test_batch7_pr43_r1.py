"""Unit/DB/supervisor/recovery/authz regression tests for batch7 PR #43 review findings (r1)."""

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

import mesh.constants as c
from mesh.core.auth import generate_run_token
from mesh.core.authz import check_authz
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.proc import get_proc_stat, is_tgid_alive
from mesh.daemon import children, recovery, supervisor, terminate
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.project import handle_project_cancel
from mesh.daemon.handlers.run import handle_run_release
from mesh.daemon.handlers.task import handle_task_reject
from mesh.daemon.server import DaemonServer
from mesh.errors import MeshError

# Tests spawn real child processes and scan real /proc: hold host-wide /proc lock shared.
MESH_PROC_LOCK = "shared"

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
    _reap_if_child(pid, st, proc)
    info = get_proc_stat(pid)
    if info is None or int(info["starttime"]) != int(st):
        return True
    return info["state"] in ("Z", "X") and not is_tgid_alive(pid, info)


class TestBatch7PR43R1Unit(unittest.TestCase):
    """Focused unit tests for PR #43 review findings (P1-1, P1-3, P2-1, P2-2)."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-pr43r1-u-", dir="/tmp")
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
            f"leader {grp['wrapper_pid']} did not exit",
        )

    def _kill_pid(self, pid: int, st: int) -> None:
        info = get_proc_stat(pid)
        if info is not None and int(info["starttime"]) == st and info["state"] not in ("Z", "X"):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertTrue(_wait_until(lambda: _is_pid_dead(pid, st), timeout=5.0), f"pid {pid} did not die")

    def _make_lead_turn_run(
        self,
        run_id: str,
        wrapper_pid: Optional[int],
        pgid: Optional[int],
        wrapper_st: Optional[int],
        status: str = "running",
        exit_info: Optional[Dict[str, Any]] = None,
        stdout_text: str = "",
        deadline_at: Optional[str] = None,
        assignment_id: str = "lead_1",
        with_wake_row: bool = False,
    ) -> str:
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        if exit_info is not None:
            with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as f:
                json.dump(exit_info, f)
        if stdout_text:
            with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
                f.write(stdout_text)
        now = now_iso_utc()
        _, lookup, tok_hash = generate_run_token()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at, lead_assignment_id) "
                "VALUES (?, 'lead_turn', 'prj_1', 'agt_1', 0, 1, 'fake', ?, 'boot_cur', ?, ?, ?, ?, ?, ?, ?);",
                (run_id, status, wrapper_pid, pgid, wrapper_st, run_dir, now, deadline_at or now_iso_utc(time.time() + 300), assignment_id),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('leadturn:prj_1', ?, 'meshd', 'boot_cur', 'agt_1', 1, ?, ?, ?);",
                (run_id, now, now_iso_utc(time.time() + 300), now),
            )
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES (?, 'agt_1', 'run_token', ?, ?, ?, ?, ?);",
                (
                    f"cred_{run_id}",
                    lookup,
                    tok_hash,
                    json.dumps({"role": "lead", "project_id": "prj_1", "lead_assignment_id": assignment_id}),
                    now,
                    now_iso_utc(time.time() + 300),
                ),
            )
            if with_wake_row:
                self.conn.execute(
                    "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, payload_json, "
                    "status, delivering_run_id, attempts, next_attempt_at, created_at) "
                    "VALUES (?, 1, 'agt_1', 'lead_wake', ?, 'prj_1', 'task.failed', '{}', 'delivering', ?, 0, ?, ?);",
                    (f"msg_{run_id}", f"wake:{run_id}", run_id, now, now),
                )
        return run_dir

    # ------------------------------------------------------------------
    # P1-3: seen_held() must allow release after crash recovery even when
    # seen.json has reason='spawn_abort_pending', while still refusing when
    # run_id is in supervisor._SPAWN_ABORTS.
    # ------------------------------------------------------------------
    def test_p1_3_seen_held_allows_release_after_spawn_abort_recovery(self) -> None:
        run_id = "run_p13"
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        grp = self._spawn_detached_group(run_id, keep_env=False)
        self._stop_group_leader(grp)

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, model, "
                "profile, timeout_s, max_attempts, attempt, fence_seq, created_by, created_at, updated_at) "
                "VALUES ('tsk_p13', 'prj_1', 'T13', 'S', 'general', 'running', 'agt_1', 'model-a', "
                "'edit', 600, 3, 1, 1, 'usr_1', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at, deadline_at) "
                "VALUES (?, 'task', 'tsk_p13', 'prj_1', 'agt_1', 1, 1, 'fake', 'running', 'boot_old', ?, ?, ?, ?, ?, ?);",
                (run_id, grp["wrapper_pid"], grp["pgid"], grp["wrapper_st"], run_dir, now, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('task:tsk_p13', ?, 'meshd', 'boot_old', 'agt_1', 1, ?, ?, ?);",
                (run_id, now, now, now),
            )
        with open(os.path.join(run_dir, "seen.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"mesh_seen_marker": "unusable", "run_id": run_id, "reason": "spawn_abort_pending", "prior_members": []},
                f,
            )

        summary = recovery.recover(self.conn, self.home, "boot_cur")
        self.assertGreaterEqual(summary["runs_terminating"], 1)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertTrue(supervisor.seen_hold(run))
        ev = self.conn.execute(
            "SELECT payload_json FROM events WHERE run_id = ? AND type = 'run.seen_unusable';", (run_id,)
        ).fetchone()
        self.assertIsNotNone(ev)

        # In-process _SPAWN_ABORTS guard still returns False.
        supervisor._SPAWN_ABORTS[run_id] = object()  # type: ignore[assignment]
        try:
            self.assertFalse(supervisor.seen_held(run, self.home))
        finally:
            supervisor._SPAWN_ABORTS.pop(run_id, None)

        # After crash recovery (_SPAWN_ABORTS empty), only envless occupant remains -> seen_held is True!
        self.assertTrue(supervisor.seen_held(run, self.home))

        ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_p13",
            home=self.home,
            cmd="run.release",
            args={"run_id": run_id, "force_seen_unusable": True},
            testing_mode=True,
        )
        with transaction(self.conn):
            res = handle_run_release(ctx)
        self.assertTrue(res.data["released"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'task:tsk_p13';").fetchone())
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()["status"], "lost")

    # ------------------------------------------------------------------
    # P1-1: handle_task_reject checks spec+rework prompt length for launch agents
    # ------------------------------------------------------------------
    def test_p1_1_reject_checks_spec_prompt_length_for_launch(self) -> None:
        kiro_bin = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shims", "kiro-cli"))
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('adapter.evidence_mode', '\"warn\"', ?, 'usr_1');",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_kiro', 'agent', 'kirow', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, binary_path, delivery, can_lead, default_profile, "
                "max_unapproved_profile, default_model, allowed_models_json, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_kiro', 'kiro', ?, 'launch', 0, 'readonly', 'dev', 'claude-sonnet-4.5', "
                "'[\"claude-sonnet-4.5\"]', 1, ?, ?);",
                (kiro_bin, now, now),
            )
            # 98,500-byte spec fits <= 100,000, but 2,000-byte deliverable summary + 1,000-byte reject reason exceeds 100,000.
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, model, "
                "profile, timeout_s, max_attempts, attempt, fence_seq, deliverable_json, created_by, created_at, updated_at) "
                "VALUES ('tsk_len', 'prj_1', 'Long Kiro', ?, 'general', 'submitted', 'agt_kiro', 'claude-sonnet-4.5', "
                "'readonly', 600, 3, 1, 1, ?, 'usr_1', ?, ?);",
                (
                    "S" * 98_500,
                    json.dumps({"deliverable": {"summary": "D" * 2_000}, "submitted_by": "agt_kiro"}),
                    now,
                    now,
                ),
            )

        ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_p11_len",
            home=self.home,
            cmd="task.reject",
            args={"task_id": "tsk_len", "reason": "R" * 1_000},
            testing_mode=True,
        )
        with self.assertRaises(MeshError) as cm:
            with transaction(self.conn):
                handle_task_reject(ctx)
        self.assertEqual(cm.exception.code, c.PROMPT_TOO_LONG)
        self.assertEqual(
            self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_len';").fetchone()["status"],
            "submitted",
        )

    # ------------------------------------------------------------------
    # P2-1: _check_lead_run_token matches persistent token semantics:
    # allow completed (deliver/prune) and paused ops, block only cancelled.
    # ------------------------------------------------------------------
    def test_p2_1_lead_run_token_completed_paused_vs_cancelled(self) -> None:
        principal = {
            "id": "agt_1",
            "kind": "agent",
            "name": "worker",
            "capabilities": ["intake"],
            "scope": {"role": "lead", "project_id": "prj_1", "lead_assignment_id": "lead_1"},
        }
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, model, "
                "profile, timeout_s, max_attempts, attempt, fence_seq, created_by, created_at, updated_at) "
                "VALUES ('tsk_p21', 'prj_1', 'T21', 'S', 'general', 'assigned', 'agt_1', 'model-a', "
                "'edit', 600, 3, 0, 0, 'usr_1', ?, ?);",
                (now, now),
            )

        # 1. Completed project: project.deliver and workspace.prune pass authz for both token & run_token.
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'completed' WHERE id = 'prj_1';")
        for cmd, kwargs in (
            ("project.deliver", {"project_id": "prj_1"}),
            ("workspace.prune", {"project_id": "prj_1"}),
        ):
            check_authz(self.conn, cmd, principal, "agent", "token", **kwargs)
            check_authz(self.conn, cmd, principal, "agent", "run_token", **kwargs)

        # 2. Paused project: task.* and workspace.prune pass authz for both token & run_token.
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'paused' WHERE id = 'prj_1';")
        for cmd, kwargs in (
            ("task.create", {"project_id": "prj_1"}),
            ("task.assign", {"project_id": "prj_1", "task_id": "tsk_p21"}),
            ("task.reassign", {"project_id": "prj_1", "task_id": "tsk_p21"}),
            ("task.cancel", {"project_id": "prj_1", "task_id": "tsk_p21"}),
            ("workspace.prune", {"project_id": "prj_1"}),
            ("project.deliver", {"project_id": "prj_1"}),
            ("inbox.ack", {}),
        ):
            check_authz(self.conn, cmd, principal, "agent", "token", **kwargs)
            check_authz(self.conn, cmd, principal, "agent", "run_token", **kwargs)

        # 3. Cancelled project: run_token blocks mutating cmds immediately with PROJECT_NOT_ACTIVE.
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'cancelled' WHERE id = 'prj_1';")
        for cmd, kwargs in (
            ("task.create", {"project_id": "prj_1"}),
            ("workspace.prune", {"project_id": "prj_1"}),
            ("project.deliver", {"project_id": "prj_1"}),
            ("intake.submit", {"project_id": "prj_1"}),
        ):
            with self.assertRaises(MeshError) as cm:
                check_authz(self.conn, cmd, principal, "agent", "run_token", **kwargs)
            self.assertEqual(cm.exception.code, c.PROJECT_NOT_ACTIVE)

    # ------------------------------------------------------------------
    # P2-2: lead_turn termination during leftovers, gone, and starting windows
    # ------------------------------------------------------------------
    def test_p2_2_lead_turn_cancel_during_leftovers_window(self) -> None:
        run_id = "run_lt_left"
        grp = self._spawn_detached_group(run_id, keep_env=True)
        # Unknown cmd (config.set) in mesh-actions would trigger lead.action_failed -> lead_wake if executed!
        stdout_actions = '```mesh-actions\n[{"cmd": "config.set", "args": {"key": "k", "value": "1"}}]\n```\n'
        self._make_lead_turn_run(
            run_id,
            wrapper_pid=grp["wrapper_pid"],
            pgid=grp["pgid"],
            wrapper_st=grp["wrapper_st"],
            exit_info={"exit_code": 0, "signal": None, "ended_at": now_iso_utc()},
            stdout_text=stdout_actions,
            with_wake_row=True,
        )
        self._stop_group_leader(grp)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()

        # First watch starts leftovers termination.
        self.assertIsNone(supervisor._watch_lead_turn(self.conn, self.home, run))
        t = terminate.get(run_id)
        self.assertIsNotNone(t)
        self.assertEqual(t.end_reason, "leftovers")

        # Now cancel the project and apply DaemonServer effects -> t.end_reason upgraded to project_cancelled.
        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_p22_cancel",
            home=self.home,
            cmd="project.cancel",
            args={"project_id": "prj_1"},
            testing_mode=True,
        )
        with transaction(self.conn):
            handle_project_cancel(ctx)
        srv.execute_effects(ctx.effects)
        self.assertEqual(terminate.get(run_id).end_reason, "project_cancelled")

        # Kill leftover child and advance until closed -> killed / project_cancelled, 0 actions run, 0 pending wakes.
        self._kill_pid(grp["member_pid"], grp["member_st"])
        self.assertTrue(
            _wait_until(
                lambda: (
                    supervisor.advance_terminations(self.conn, self.home),
                    self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()["status"] == "killed",
                )[1],
                timeout=5.0,
            )
        )
        closed = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertEqual((closed["status"], closed["end_reason"]), ("killed", "project_cancelled"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events WHERE run_id = ? AND type = 'lead.action_result';", (run_id,)).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM outbox WHERE project_id = 'prj_1' AND channel = 'lead_wake' AND status = 'pending';").fetchone()[0],
            0,
        )

    def test_p2_2_lead_turn_set_lead_finalize_fallback_and_gone_no_leftovers(self) -> None:
        # Case A: _finalize_termination fallback when lead was superseded while t.end_reason was still 'leftovers'.
        run_id = "run_lt_sup"
        grp = self._spawn_detached_group(run_id, keep_env=True)
        stdout_actions = '```mesh-actions\n[{"cmd": "config.set", "args": {"key": "k", "value": "1"}}]\n```\n'
        self._make_lead_turn_run(
            run_id,
            wrapper_pid=grp["wrapper_pid"],
            pgid=grp["pgid"],
            wrapper_st=grp["wrapper_st"],
            exit_info={"exit_code": 0, "signal": None, "ended_at": now_iso_utc()},
            stdout_text=stdout_actions,
        )
        self._stop_group_leader(grp)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        supervisor._watch_lead_turn(self.conn, self.home, run)
        self.assertEqual(terminate.get(run_id).end_reason, "leftovers")

        # Simulate lead superseded in DB directly (bypassing _terminate_lead_turn).
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_2', 'agent', 'lead2', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, default_profile, max_unapproved_profile, "
                "default_model, allowed_models_json, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_2', 'fake', 'launch', 1, 'edit', 'dev', 'model-a', '[\"model-a\"]', 2, ?, ?);",
                (now, now),
            )
            self.conn.execute("UPDATE lead_assignments SET ended_at = ?, ended_reason = 'switch' WHERE id = 'lead_1';", (now,))
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES ('lead_2', 'prj_1', 'agt_2', 'usr_1', 'direct', ?);",
                (now,),
            )

        self._kill_pid(grp["member_pid"], grp["member_st"])
        self.assertTrue(
            _wait_until(
                lambda: (
                    supervisor.advance_terminations(self.conn, self.home),
                    self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()["status"] == "killed",
                )[1],
                timeout=5.0,
            )
        )
        closed = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_id,)).fetchone()
        self.assertEqual((closed["status"], closed["end_reason"]), ("killed", "lead_superseded"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events WHERE run_id = ? AND type = 'lead.action_result';", (run_id,)).fetchone()[0],
            0,
        )

        # Case B: _watch_lead_turn gone path with NO leftovers when project is already cancelled.
        run_gone = "run_lt_gone"
        dead = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={},
        )
        dead_st = int((get_proc_stat(dead.pid) or {"starttime": 1})["starttime"])
        dead.wait(timeout=5)
        self.assertTrue(_wait_until(lambda: _is_pid_dead(dead.pid, dead_st, dead), timeout=5.0))
        self._make_lead_turn_run(
            run_gone,
            wrapper_pid=dead.pid,
            pgid=dead.pid,
            wrapper_st=dead_st,
            exit_info={"exit_code": 0, "signal": None, "ended_at": now_iso_utc()},
            stdout_text=stdout_actions,
            assignment_id="lead_2",
        )
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'cancelled' WHERE id = 'prj_1';")
        run_g = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_gone,)).fetchone()
        self.assertTrue(
            _wait_until(
                lambda: (
                    supervisor._watch_one(self.conn, self.home, run_g),
                    self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_gone,)).fetchone()["status"] == "killed",
                )[1],
                timeout=5.0,
                step=terminate.POLL_S / 2.0,
            )
        )
        closed_g = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_gone,)).fetchone()
        self.assertEqual((closed_g["status"], closed_g["end_reason"]), ("killed", "project_cancelled"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events WHERE run_id = ? AND type = 'lead.action_result';", (run_gone,)).fetchone()[0],
            0,
        )

    def test_p2_2_terminate_lead_turn_starting_window_and_expired_leftovers_control(self) -> None:
        # Subcase A: starting window (wrapper_pid IS NULL in DB, wrapper.json on disk)
        run_start = "run_lt_start"
        wrap = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={"MESH_RUN_ID": run_start},
        )
        wrap_st = int(get_proc_stat(wrap.pid)["starttime"])
        self.addCleanup(lambda: (self._kill_pid(wrap.pid, wrap_st), wrap.wait(timeout=5)))
        run_dir = self._make_lead_turn_run(
            run_start, wrapper_pid=None, pgid=None, wrapper_st=None, status="starting"
        )
        with open(os.path.join(run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wrap.pid, "pgid": wrap.pid, "starttime": wrap_st}, f)

        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn
        srv._terminate_lead_turn(run_start, "project_cancelled")
        t = terminate.get(run_start)
        self.assertIsNotNone(t)
        self.assertEqual(t.end_reason, "project_cancelled")
        wrap.wait(timeout=5)
        terminate.forget(run_start)

        # Subcase B (control): exited-0 lead turn with expired deadline_at & leftovers still closes as exited.
        run_ctrl = "run_lt_ctrl"
        grp = self._spawn_detached_group(run_ctrl, keep_env=True)
        self._make_lead_turn_run(
            run_ctrl,
            wrapper_pid=grp["wrapper_pid"],
            pgid=grp["pgid"],
            wrapper_st=grp["wrapper_st"],
            exit_info={"exit_code": 0, "signal": None, "ended_at": now_iso_utc()},
            deadline_at=now_iso_utc(time.time() - 60),
        )
        self._stop_group_leader(grp)
        run_c = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_ctrl,)).fetchone()
        supervisor._watch_lead_turn(self.conn, self.home, run_c)
        self.assertEqual(terminate.get(run_ctrl).end_reason, "leftovers")
        self._kill_pid(grp["member_pid"], grp["member_st"])
        self.assertTrue(
            _wait_until(
                lambda: (
                    supervisor.advance_terminations(self.conn, self.home),
                    self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_ctrl,)).fetchone()["status"] == "exited",
                )[1],
                timeout=5.0,
            )
        )
        ctrl_row = self.conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_ctrl,)).fetchone()
        self.assertEqual((ctrl_row["status"], ctrl_row["end_reason"]), ("exited", "exited"))


if __name__ == "__main__":
    unittest.main()
