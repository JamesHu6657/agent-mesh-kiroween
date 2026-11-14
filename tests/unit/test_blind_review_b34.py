"""Blind Linear review of main ffa471e: execution, recovery, repo and client fixes."""

import argparse
import json
import os
import sys
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest import mock

from mesh.daemon import children


def _git(cwd: str, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def _repo(path: str, branch: str) -> None:
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    with open(os.path.join(path, "f.txt"), "w") as f:
        f.write("x\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class TestSpawnedThenFailedCleanup(unittest.TestCase):
    def test_child_killed_when_registration_fails(self) -> None:
        seen = {}
        real_popen = subprocess.Popen

        def popen(*a, **kw):
            p = real_popen(*a, **kw)
            seen["pid"] = p.pid
            return p

        with mock.patch.object(children, "register", side_effect=OSError("proc gone")), \
                mock.patch.object(children.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(children.SpawnedThenFailed):
                children.run_registered("probe", ["sleep", "60"], timeout=1)
        time.sleep(0.2)
        self.assertFalse(_alive(seen["pid"]))
        self.assertNotIn(seen["pid"], children._held)


class TestProbeBounds(unittest.TestCase):
    def test_timeout_kills_descendants_quickly(self) -> None:
        from mesh.adapters.base import run_readonly_cmd

        d = tempfile.mkdtemp()
        try:
            pidfile = os.path.join(d, "pid")
            t0 = time.monotonic()
            res = run_readonly_cmd(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], timeout_s=0.3)
            took = time.monotonic() - t0
            self.assertEqual(res.returncode, -1)
            self.assertLess(took, 3.0)
            time.sleep(0.2)
            with open(pidfile) as f:
                self.assertFalse(_alive(int(f.read().strip())))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_escaped_descendant_holding_pipe_does_not_block(self) -> None:
        from mesh.adapters.base import run_readonly_cmd

        d = tempfile.mkdtemp()
        try:
            pidfile = os.path.join(d, "pid")
            t0 = time.monotonic()
            res = run_readonly_cmd(["sh", "-c", f"setsid sleep 30 & echo $! > {pidfile}; sleep 30"], timeout_s=0.3)
            took = time.monotonic() - t0
            with open(pidfile) as f:
                try:
                    os.kill(int(f.read().strip()), 9)
                except OSError:
                    pass
            self.assertEqual(res.returncode, -1)
            self.assertLess(took, 4.0)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_output_cap_bounds_memory(self) -> None:
        from mesh.adapters.base import run_readonly_cmd

        tracemalloc.start()
        try:
            res = run_readonly_cmd(["python3", "-c", "import sys; sys.stdout.write('x' * (16 * 1024 * 1024))"],
                                   timeout_s=20, max_output_bytes=64)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(res.stdout), 64)
        self.assertLess(peak, 4 * 1024 * 1024)


class TestDeferredRecovery(unittest.TestCase):
    """Real path (PR #40 r1): only the first identity check fails; the retry runs the
    real _recover_run on the real schema and adopts the live wrapper."""

    def _insert(self, conn, table: str, values: dict) -> None:
        cols = conn.execute(f"PRAGMA table_info({table});").fetchall()
        row = {}
        for col in cols:
            name, typ, notnull, default = col[1], col[2], col[3], col[4]
            if name in values:
                row[name] = values[name]
            elif notnull and default is None:
                row[name] = 1 if typ.upper() == "INTEGER" else "x"
        names = ", ".join(row)
        conn.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' for _ in row)});",
                     tuple(row.values()))

    def test_failed_recovery_is_retried_on_the_real_path(self) -> None:
        from mesh.core.proc import get_proc_stat
        from mesh.core.schema import INITIAL_SCHEMA_STATEMENTS
        from mesh.daemon import recovery

        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        run_dir = os.path.join(home, "runs", "run_x")
        os.makedirs(run_dir)
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        with open(os.path.join(run_dir, "wrapper.json"), "w") as f:
            f.write('{"pid": %d, "starttime": %d}' % (child.pid, int(get_proc_stat(child.pid)["starttime"])))
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        for st in INITIAL_SCHEMA_STATEMENTS:
            conn.execute(st)
        conn.execute("PRAGMA foreign_keys = OFF;")
        self._insert(conn, "runs", {"id": "run_x", "kind": "task", "status": "starting", "boot_id": "boot_0",
                                    "run_dir": run_dir, "wrapper_pid": None, "pgid": None,
                                    "proc_starttime": None, "started_at": "2026-01-01T00:00:00Z"})
        self._insert(conn, "leases", {"key": "task:x", "run_id": "run_x", "holder_kind": "meshd",
                                      "holder": "boot_0"})
        real = recovery._identity_alive
        calls = []

        def flaky(wrapper):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("transient /proc error")
            return real(wrapper)

        recovery.DEFERRED.clear()
        self.addCleanup(recovery.DEFERRED.clear)
        with mock.patch.object(recovery, "_identity_alive", side_effect=flaky):
            kept = recovery._step_runs(conn, home, "boot_1", {})
            self.assertIn("run_x", kept)
            self.assertIn("run_x", recovery.DEFERRED)
            recovery.retry_deferred(conn, home)
        self.assertNotIn("run_x", recovery.DEFERRED)
        run = conn.execute("SELECT status, wrapper_pid FROM runs WHERE id = 'run_x';").fetchone()
        self.assertEqual((run["status"], run["wrapper_pid"]), ("running", child.pid))
        self.assertEqual(conn.execute("SELECT holder FROM leases WHERE run_id = 'run_x';").fetchone()[0], "boot_1")


class TestWorkspaceRepoIdentity(unittest.TestCase):
    def test_foreign_repo_with_same_branch_refused(self) -> None:
        from mesh.core import workspaces

        d = tempfile.mkdtemp()
        try:
            repo, other = os.path.join(d, "repo"), os.path.join(d, "ws")
            _repo(repo, "main")
            _repo(other, "mesh/prj/tsk")
            plan = {"repo": repo, "workspace_path": other}
            with self.assertRaises(workspaces.WorkspaceError) as cm:
                workspaces._reuse(plan, other, "mesh/prj/tsk")
            self.assertEqual(cm.exception.details.get("reason"), "foreign_repository")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestMergeBranchIdentity(unittest.TestCase):
    def test_integration_on_wrong_branch_refused(self) -> None:
        from mesh.daemon import merge

        d = tempfile.mkdtemp()
        try:
            integ = os.path.join(d, "integ")
            _repo(integ, "mesh/prj/integration")
            _git(integ, "checkout", "-q", "-b", "mesh/prj/tsk")
            with open(os.path.join(integ, "g.txt"), "w") as f:
                f.write("y\n")
            _git(integ, "add", "g.txt")
            _git(integ, "commit", "-q", "-m", "task")
            _git(integ, "checkout", "-q", "-b", "unrelated", "mesh/prj/integration")
            before = _git(integ, "rev-parse", "mesh/prj/integration").strip()
            out = merge._run_merge_pipeline({"integration": integ, "worktree": integ, "branch": "mesh/prj/tsk",
                                             "integration_branch": "mesh/prj/integration"})
            self.assertEqual(out["outcome"], "failed", out)
            self.assertTrue(out["details"].get("wrong_branch"), out)
            self.assertEqual(_git(integ, "rev-parse", "mesh/prj/integration").strip(), before)
            self.assertEqual(_git(integ, "rev-parse", "unrelated").strip(), before)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestWorktreeNewlinePath(unittest.TestCase):
    def test_path_with_newline(self) -> None:
        from mesh.core import repos

        d = tempfile.mkdtemp()
        try:
            repo = os.path.join(d, "repo")
            _repo(repo, "main")
            wt = os.path.join(d, "new\nrepo")
            _git(repo, "worktree", "add", "-q", "-b", "feat", wt)
            self.assertEqual(os.path.realpath(repos._worktree_for_branch(repo, "feat")), os.path.realpath(wt))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestClientHomeAndTail(unittest.TestCase):
    def test_explicit_default_home_beats_mesh_socket(self) -> None:
        d = tempfile.mkdtemp()
        try:
            env = {k: v for k, v in os.environ.items() if k not in ("MESH_HOME", "MESH_SOCKET")}
            env.update(HOME=d, MESH_SOCKET=os.path.join(d, "other.sock"))
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            cp = subprocess.run([os.path.join(root, "bin", "mesh"), "--home", os.path.join(d, ".agent-mesh"),
                                 "daemon", "status"], env=env, capture_output=True, text=True, timeout=30)
            self.assertNotIn("other.sock", cp.stdout + cp.stderr)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_tail_does_not_retain_events(self) -> None:
        from mesh.client import MeshClient

        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "run"))
            path = os.path.join(d, "run", "mesh.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path)
            srv.listen(1)

            def serve() -> None:
                conn, _ = srv.accept()
                conn.makefile("r").readline()
                conn.sendall(b"".join(b'{"seq": %d}\n' % i for i in range(2000)))
                conn.close()

            t = threading.Thread(target=serve, daemon=True)
            t.start()
            client = MeshClient(d)
            args = argparse.Namespace(request_id="req_x", relayed=False, timeout=None)
            with mock.patch.object(client, "resolve_auth", return_value={"type": "none"}), \
                    mock.patch("sys.stdout"):
                res = client.stream_cmd(args, "event.tail", {})
            t.join(5)
            srv.close()
            self.assertFalse(res.get("items"), "streamed events must not be kept")
        finally:
            shutil.rmtree(d, ignore_errors=True)


def _real_db():
    from mesh.core.schema import INITIAL_SCHEMA_STATEMENTS

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for st in INITIAL_SCHEMA_STATEMENTS:
        conn.execute(st)
    conn.execute("PRAGMA foreign_keys = OFF;")
    return conn


class TestDeferredRecoveryR2(unittest.TestCase):
    """PR #40 r2: the live retry never blocks, and releases the closed run's messages."""

    _insert = None  # set below

    def setUp(self) -> None:
        from mesh.daemon import recovery

        self.recovery = recovery
        recovery.DEFERRED.clear()
        self.addCleanup(recovery.DEFERRED.clear)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.conn = _real_db()

    def add_run(self, run_id: str, kind: str = "task") -> str:
        run_dir = os.path.join(self.home, "runs", run_id)
        os.makedirs(run_dir)
        TestDeferredRecovery._insert(self, self.conn, "runs", {
            "id": run_id, "kind": kind, "status": "starting", "boot_id": "boot_0", "run_dir": run_dir,
            "wrapper_pid": None, "pgid": None, "proc_starttime": None, "task_id": None,
            "started_at": "2026-01-01T00:00:00Z"})
        TestDeferredRecovery._insert(self, self.conn, "leases", {
            "key": "k:" + run_id, "run_id": run_id, "holder_kind": "meshd", "holder": "boot_0"})
        return run_dir

    def test_spawn_window_wait_does_not_block(self) -> None:
        run_id = "run_blk" + str(os.getpid())
        self.add_run(run_id)
        env = dict(os.environ, MESH_RUN_ID=run_id)
        carrier = subprocess.Popen(["sleep", "30"], env=env)
        self.addCleanup(carrier.wait)
        self.addCleanup(carrier.kill)
        time.sleep(0.1)
        self.recovery.DEFERRED[run_id] = ("boot_1", 0.0)
        t0 = time.monotonic()
        self.recovery.retry_deferred(self.conn, self.home)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn(run_id, self.recovery.DEFERRED)

    def test_closed_run_releases_delivering_messages(self) -> None:
        run_id = "run_obx" + str(os.getpid())
        run_dir = self.add_run(run_id, kind="lead_turn")
        with open(os.path.join(run_dir, "wrapper.json"), "w") as f:
            f.write('{"pid": 999999, "starttime": 12345}')  # a wrapper that is gone
        TestDeferredRecovery._insert(self, self.conn, "outbox", {
            "id": "obx_1", "event_seq": 1, "recipient_id": "prn_x", "channel": "lead_wake",
            "dedup_key": "d1", "type": "t", "payload_json": "{}", "status": "delivering",
            "attempts": 1, "next_attempt_at": "t", "delivering_run_id": run_id})
        self.recovery.DEFERRED[run_id] = ("boot_1", 0.0)
        with mock.patch.object(self.recovery, "_log"):
            # The real retry: the wrapper is gone, so the run is handed to termination.
            self.recovery.retry_deferred(self.conn, self.home)
            st = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()[0]
        if st in ("starting", "running"):
            self.assertIn(run_id, self.recovery.DEFERRED)
            # The termination engine closes it later (its grace period is not under test).
            self.conn.execute("UPDATE runs SET status = 'lost', ended_at = 't' WHERE id = ?;", (run_id,))
            from mesh.daemon import terminate
            terminate.forget(run_id)
            entry = self.recovery.DEFERRED[run_id]
            self.recovery.DEFERRED[run_id] = (entry[0], 0.0) + tuple(entry[2:])
            with mock.patch.object(self.recovery, "_log"):
                self.recovery.retry_deferred(self.conn, self.home)
        self.assertNotIn(run_id, self.recovery.DEFERRED)
        self.assertNotIn(self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()[0],
                         ("starting", "running"))
        row = self.conn.execute("SELECT status, delivering_run_id FROM outbox WHERE id = 'obx_1';").fetchone()
        self.assertEqual((row["status"], row["delivering_run_id"]), ("pending", None))


class TestDeferredRecoveryR3(TestDeferredRecoveryR2):
    """PR #40 r3: live recovery keeps observed identities, never sleeps, and keeps its
    entry until the closed run's messages are released."""

    def tick_until_closed(self, run_id: str, limit_s: float = 30.0) -> list:
        # 30 s: closing waits for the termination engine, which is slow under full-suite
        # load (merge gate flake at 5 s); each tick is still bounded below (no sleep).
        durations = []
        end = time.monotonic() + limit_s
        while time.monotonic() < end:
            entry = self.recovery.DEFERRED.get(run_id)
            if entry is not None:
                self.recovery.DEFERRED[run_id] = (entry[0], 0.0) + tuple(entry[2:])
            t0 = time.monotonic()
            with mock.patch.object(self.recovery, "_log"):
                self.recovery.retry_deferred(self.conn, self.home)
            durations.append(time.monotonic() - t0)
            if run_id not in self.recovery.DEFERRED:
                break
            time.sleep(0.05)
        return durations

    def test_empty_spawn_window_does_not_sleep(self) -> None:
        for n in range(3):
            run_id = "run_nsl%d_%d" % (n, os.getpid())
            self.add_run(run_id)
            self.recovery.DEFERRED[run_id] = ("boot_1", 0.0)
            durations = self.tick_until_closed(run_id)
            self.assertNotIn(run_id, self.recovery.DEFERRED)
            self.assertLess(max(durations), 0.15, durations)
            st = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()[0]
            self.assertNotIn(st, ("starting", "running"))

    def test_dead_wrapper_does_not_sleep(self) -> None:
        run_id = "run_dws" + str(os.getpid())
        run_dir = self.add_run(run_id)
        with open(os.path.join(run_dir, "wrapper.json"), "w") as f:
            f.write('{"pid": 999999, "starttime": 12345}')
        self.recovery.DEFERRED[run_id] = ("boot_1", 0.0)
        durations = self.tick_until_closed(run_id, limit_s=1.0)
        self.assertLess(max(durations), 0.15, durations)

    def test_carrier_child_survives_parent_exit_between_ticks(self) -> None:
        run_id = "run_kid" + str(os.getpid())
        self.add_run(run_id)
        pidfile = os.path.join(self.home, "kid.pid")
        env = dict(os.environ, MESH_RUN_ID=run_id)
        parent = subprocess.Popen(
            ["sh", "-c", 'setsid env -i sleep 30 & echo $! > "$1"; sleep 0.8', "sh", pidfile], env=env)
        self.addCleanup(parent.wait)
        deadline = time.monotonic() + 3
        while not os.path.exists(pidfile) or not open(pidfile).read().strip():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        kid = int(open(pidfile).read())

        def _kill() -> None:
            try:
                os.kill(kid, 9)
            except OSError:
                pass
        self.addCleanup(_kill)
        time.sleep(0.1)
        self.recovery.DEFERRED[run_id] = ("boot_1", 0.0)
        with mock.patch.object(self.recovery, "_log"):
            self.recovery.retry_deferred(self.conn, self.home)  # sees parent + child
            self.assertIn(run_id, self.recovery.DEFERRED)
            parent.wait(timeout=5)  # the tagged parent exits; the child lives on
            entry = self.recovery.DEFERRED[run_id]
            entry[2].wait_until = 0.0  # spawn window over
            self.recovery.DEFERRED[run_id] = (entry[0], 0.0) + tuple(entry[2:])
            self.recovery.retry_deferred(self.conn, self.home)
        st = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()[0]
        self.assertIn(st, ("starting", "running"))  # held for the live child, not closed
        lease = self.conn.execute("SELECT 1 FROM leases WHERE run_id = ?;", (run_id,)).fetchone()
        self.assertIsNotNone(lease)
        from mesh.daemon import terminate
        term = terminate.get(run_id)
        self.assertIsNotNone(term)
        self.addCleanup(terminate.forget, run_id)
        self.assertIn(kid, set(term.initial_pids) | set(term.seen))

    def test_failed_release_is_retried(self) -> None:
        run_id = "run_rel" + str(os.getpid())
        self.add_run(run_id, kind="lead_turn")
        TestDeferredRecovery._insert(self, self.conn, "outbox", {
            "id": "obx_2", "event_seq": 1, "recipient_id": "prn_x", "channel": "lead_wake",
            "dedup_key": "d2", "type": "t", "payload_json": "{}", "status": "delivering",
            "attempts": 1, "next_attempt_at": "t", "delivering_run_id": run_id})
        self.conn.execute("UPDATE runs SET status = 'lost', ended_at = 't' WHERE id = ?;", (run_id,))
        self.conn.execute("CREATE TRIGGER deny BEFORE UPDATE ON outbox BEGIN SELECT RAISE(ABORT, 'busy'); END;")
        self.recovery.DEFERRED[run_id] = ("boot_1", 0.0)
        with mock.patch.object(self.recovery, "_log"):
            self.recovery.retry_deferred(self.conn, self.home)
            self.assertIn(run_id, self.recovery.DEFERRED)
            self.conn.execute("DROP TRIGGER deny;")
            entry = self.recovery.DEFERRED[run_id]
            self.recovery.DEFERRED[run_id] = (entry[0], 0.0) + tuple(entry[2:])
            self.recovery.retry_deferred(self.conn, self.home)
        self.assertNotIn(run_id, self.recovery.DEFERRED)
        row = self.conn.execute("SELECT status FROM outbox WHERE id = 'obx_2';").fetchone()
        self.assertEqual(row[0], "pending")


class TestDeferredRecoveryR4(TestDeferredRecoveryR3):
    """PR #40 r4: identities found by earlier live-recovery scans survive adoption and
    both exit-file paths (real processes, no mocked process table)."""

    def start_parent_and_kid(self, run_id: str):
        pidfile = os.path.join(self.home, "kid.pid")
        env = dict(os.environ, MESH_RUN_ID=run_id)
        parent = subprocess.Popen(
            ["sh", "-c", 'setsid env -i sleep 30 & echo $! > "$1"; sleep 0.8', "sh", pidfile], env=env)
        self.addCleanup(parent.wait)
        deadline = time.monotonic() + 3
        while not os.path.exists(pidfile) or not open(pidfile).read().strip():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        kid = int(open(pidfile).read())

        def _kill() -> None:
            try:
                os.kill(kid, 9)
            except OSError:
                pass
        self.addCleanup(_kill)
        time.sleep(0.1)
        return parent, kid

    def first_tick_then_parent_exits(self, run_id: str, parent) -> None:
        self.recovery.DEFERRED[run_id] = ("boot_1", 0.0)
        with mock.patch.object(self.recovery, "_log"):
            self.recovery.retry_deferred(self.conn, self.home)  # sees parent + child
        self.assertIn(run_id, self.recovery.DEFERRED)
        self.assertTrue(self.recovery.DEFERRED[run_id][2].found)
        parent.wait(timeout=5)
        entry = self.recovery.DEFERRED[run_id]
        self.recovery.DEFERRED[run_id] = (entry[0], 0.0) + tuple(entry[2:])

    def second_tick(self, run_id: str) -> None:
        from mesh.daemon import supervisor

        with mock.patch.object(self.recovery, "_log"), mock.patch.object(supervisor, "_log_meshd"):
            self.recovery.retry_deferred(self.conn, self.home)

    def cleanup_run(self, run_id: str) -> None:
        from mesh.daemon import supervisor, terminate

        self.addCleanup(terminate.forget, run_id)
        for m in (supervisor._SEEN, supervisor._SEEN_TICKS):
            self.addCleanup(m.pop, run_id, None)
        self.addCleanup(supervisor._SEEN_UNUSABLE.discard, run_id)

    def targets(self, run_id: str) -> set:
        from mesh.daemon import terminate

        term = terminate.get(run_id)
        return set() if term is None else set(term.initial_pids) | set(term.seen)

    def test_adopt_then_wrapper_exits_keeps_child(self) -> None:
        from mesh.core.proc import get_proc_stat
        from mesh.daemon import supervisor

        run_id = "run_r4a" + str(os.getpid())
        run_dir = self.add_run(run_id)
        self.cleanup_run(run_id)
        parent, kid = self.start_parent_and_kid(run_id)
        self.first_tick_then_parent_exits(run_id, parent)
        wrapper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(wrapper.wait)
        self.addCleanup(wrapper.kill)
        st = int(get_proc_stat(wrapper.pid)["starttime"])
        with open(os.path.join(run_dir, "wrapper.json"), "w") as f:
            json.dump({"pid": wrapper.pid, "starttime": st}, f)
        self.second_tick(run_id)
        self.assertNotIn(run_id, self.recovery.DEFERRED)  # adopted
        self.assertIn(kid, supervisor._SEEN.get(run_id) or {})
        with open(os.path.join(run_dir, "seen.json")) as f:
            self.assertIn(kid, [m["pid"] for m in json.load(f)["members"]])
        wrapper.kill()
        wrapper.wait()
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        with self.assertRaises(supervisor.CloseHeld) as cm:
            supervisor.close_gate(run, "exited")
        self.assertIn(kid, cm.exception.live)

    def test_exit_file_with_wrapper_identity_keeps_child(self) -> None:
        run_id = "run_r4b" + str(os.getpid())
        run_dir = self.add_run(run_id)
        self.cleanup_run(run_id)
        parent, kid = self.start_parent_and_kid(run_id)
        self.first_tick_then_parent_exits(run_id, parent)
        # The wrapper ran and exited between ticks: wrapper.json (gone pid) + exit.json.
        gone = subprocess.Popen(["true"], start_new_session=True)
        gone.wait()
        with open(os.path.join(run_dir, "wrapper.json"), "w") as f:
            json.dump({"pid": gone.pid, "starttime": 1}, f)
        with open(os.path.join(run_dir, "exit.json"), "w") as f:
            json.dump({"exit_code": 0, "signal": None}, f)
        self.second_tick(run_id)
        st = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()[0]
        self.assertIn(st, ("starting", "running"))  # not closed while the child lives
        self.assertIn(kid, self.targets(run_id))

    def test_exit_file_without_wrapper_identity_keeps_child(self) -> None:
        run_id = "run_r4c" + str(os.getpid())
        run_dir = self.add_run(run_id)
        self.cleanup_run(run_id)
        parent, kid = self.start_parent_and_kid(run_id)
        self.first_tick_then_parent_exits(run_id, parent)
        with open(os.path.join(run_dir, "exit.json"), "w") as f:
            json.dump({"exit_code": 0, "signal": None}, f)
        self.second_tick(run_id)
        st = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()[0]
        self.assertIn(st, ("starting", "running"))
        self.assertIn(kid, self.targets(run_id))


class TestDeferredRecoveryR5(TestDeferredRecoveryR4):
    """PR #40 r5: on an uncertain adoption the found identities are persisted in the
    unusable marker (hold kept); if that fails, recovery keeps the run and retries."""

    def uncertain_adoption(self, run_id: str):
        from mesh.core.proc import get_proc_stat
        from mesh.daemon import supervisor

        run_dir = self.add_run(run_id)
        self.cleanup_run(run_id)
        for m in (supervisor._SEEN_REASON, supervisor._SEEN_HOLD_TOKEN, supervisor._SEEN_EVENT_PENDING):
            self.addCleanup(m.pop, run_id, None)
        parent, kid = self.start_parent_and_kid(run_id)
        self.first_tick_then_parent_exits(run_id, parent)
        wrapper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(wrapper.wait)
        self.addCleanup(wrapper.kill)
        st = int(get_proc_stat(wrapper.pid)["starttime"])
        with open(os.path.join(run_dir, "wrapper.json"), "w") as f:
            json.dump({"pid": wrapper.pid, "starttime": st}, f)
        real_track = supervisor.track_adopted_seen

        def track_then_wrapper_exits(run, home=None):
            # The wrapper really exits during the adoption scan: the scan is untrusted.
            wrapper.kill()
            wrapper.wait()
            return real_track(run, home)
        return run_dir, kid, mock.patch.object(supervisor, "track_adopted_seen", track_then_wrapper_exits)

    def test_uncertain_adoption_persists_found_in_marker(self) -> None:
        from mesh.daemon import supervisor

        run_id = "run_r5a" + str(os.getpid())
        run_dir, kid, patch = self.uncertain_adoption(run_id)
        with patch:
            self.second_tick(run_id)
        self.assertIn(run_id, supervisor._SEEN_UNUSABLE)  # the hold stands
        with open(os.path.join(run_dir, "seen.json")) as f:
            doc = json.load(f)
        self.assertIn(supervisor.SEEN_MARKER_KEY, doc)
        self.assertIn(kid, [m["pid"] for m in doc["prior_members"]])
        # After a restart (memory gone) the close gate still refuses while the child lives.
        supervisor._SEEN.pop(run_id, None)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        with self.assertRaises(supervisor.CloseHeld) as cm:
            supervisor.close_gate(run, "lost_on_restart")
        self.assertIn(kid, cm.exception.live)

    def test_marker_write_failure_keeps_retry(self) -> None:
        from mesh.daemon import supervisor

        run_id = "run_r5b" + str(os.getpid())
        run_dir, kid, patch = self.uncertain_adoption(run_id)
        real_write = supervisor.write_unusable_marker
        calls = []

        def write(run, reason, home=None):
            calls.append(reason)
            # The adoption's own marker is written; the hand-over rewrite hits a full disk.
            return real_write(run, reason, home) if len(calls) == 1 else False
        with patch, mock.patch.object(supervisor, "write_unusable_marker", write):
            self.second_tick(run_id)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn(run_id, self.recovery.DEFERRED)  # recovery keeps responsibility
        self.assertIn(kid, self.recovery.DEFERRED[run_id][2].found)


class TestDeferredRecoveryR6(TestDeferredRecoveryR5):
    """PR #40 r6: a failed hand-over write is redone on the retry; the hold survives a
    restart in a fresh interpreter."""

    def test_failed_write_is_redone_and_survives_restart(self) -> None:
        from mesh.daemon import supervisor

        run_id = "run_r6a" + str(os.getpid())
        run_dir, kid, patch = self.uncertain_adoption(run_id)
        self.addCleanup(getattr(self.recovery, "_HANDED_OVER", {}).pop, run_id, None)
        real_write = supervisor.write_unusable_marker
        calls = []

        def write(run, reason, home=None):
            calls.append(reason)
            return real_write(run, reason, home) if len(calls) == 1 else False
        with patch, mock.patch.object(supervisor, "write_unusable_marker", write):
            self.second_tick(run_id)
        self.assertIn(run_id, self.recovery.DEFERRED)
        with open(os.path.join(run_dir, "seen.json")) as f:
            self.assertNotIn(kid, [m["pid"] for m in json.load(f).get("prior_members", [])])
        # Storage restored: the retry writes the identities (in-memory state matches already).
        self.assertIn(kid, supervisor._SEEN.get(run_id) or {})
        entry = self.recovery.DEFERRED[run_id]
        self.recovery.DEFERRED[run_id] = (entry[0], 0.0) + tuple(entry[2:])
        self.second_tick(run_id)
        with open(os.path.join(run_dir, "seen.json")) as f:
            doc = json.load(f)
        self.assertIn(supervisor.SEEN_MARKER_KEY, doc)
        self.assertIn(kid, [m["pid"] for m in doc["prior_members"]])
        # The retry also hands the child to termination.
        self.assertIn(kid, self.targets(run_id))
        # A fresh interpreter (restart) reads the identity back from disk as a held member.
        run = dict(self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone())
        code = (
            "import json,sys\n"
            "from mesh.daemon import supervisor\n"
            "run = json.loads(sys.argv[1])\n"
            "state, _seen = supervisor.load_seen_state(run)\n"
            "print(state, sorted(p for p, _s in supervisor._load_prior_member_pairs(run)))\n")
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = subprocess.run([sys.executable, "-c", code, json.dumps(run)], cwd=root, capture_output=True,
                             text=True, timeout=30, env=dict(os.environ, PYTHONPATH=root))
        self.assertTrue(out.stdout.startswith(supervisor.SEEN_UNUSABLE), out.stdout + out.stderr)
        self.assertIn(str(kid), out.stdout)

    def test_uncertain_adoption_hold_survives_fresh_interpreter(self) -> None:
        from mesh.daemon import supervisor

        run_id = "run_r6b" + str(os.getpid())
        run_dir, kid, patch = self.uncertain_adoption(run_id)
        self.addCleanup(getattr(self.recovery, "_HANDED_OVER", {}).pop, run_id, None)
        with patch:
            self.second_tick(run_id)
        run = dict(self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone())
        code = (
            "import json,sys\n"
            "from mesh.daemon import supervisor\n"
            "run = json.loads(sys.argv[1])\n"
            "try:\n"
            "    supervisor.close_gate(run, 'lost_on_restart')\n"
            "    print('CLOSED')\n"
            "except supervisor.CloseHeld as e:\n"
            "    print('HELD', sorted(e.live))\n")
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = subprocess.run([sys.executable, "-c", code, json.dumps(run)], cwd=root, capture_output=True,
                             text=True, timeout=30, env=dict(os.environ, PYTHONPATH=root))
        self.assertTrue(out.stdout.startswith("HELD"), out.stdout + out.stderr)
        self.assertIn(str(kid), out.stdout)


class TestProbeExitAfterClose(unittest.TestCase):
    def test_closed_output_then_clean_exit_is_not_killed(self) -> None:
        from mesh.adapters.base import run_readonly_cmd

        res = run_readonly_cmd(["sh", "-c", "echo hi; exec >/dev/null 2>&1; sleep 0.3; exit 0"], timeout_s=3)
        self.assertEqual(res.returncode, 0)
        self.assertIn("hi", res.stdout)


if __name__ == "__main__":
    unittest.main()


class TestCaptureErrorNeverSubmits(unittest.TestCase):
    def test_needs_submit_git_false_on_capture_error(self) -> None:
        from mesh.daemon import supervisor

        run = {"id": "run_x", "run_dir": "/nonexistent", "adapter": "fake"}
        task = {"kind": "code", "status": "running"}
        with mock.patch.object(supervisor, "_pending_failure", return_value=None), \
                mock.patch.object(supervisor, "vendor_failure_meaning", return_value=None):
            self.assertTrue(supervisor._needs_submit_git(run, task, {"exit_code": 0, "signal": None}))
            self.assertFalse(supervisor._needs_submit_git(
                run, task, {"exit_code": 0, "signal": None, "capture_error": "stdout.log: ENOSPC"}))
