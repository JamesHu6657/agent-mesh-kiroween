"""Regression tests for full2-r2.md Group D findings (P1-6, P2-7).

1. P1-6 (mesh/daemon/supervisor.py): ``_finalize_termination`` on ``t.end_reason == 'leftovers'``
   must collect ``deliverable.git`` when ``_needs_submit_git`` holds (e.g. after ``hold_close``
   with ``end_reason='exited'``) and defer via ``_advance`` (keeping termination and lease alive)
   until async git collection finishes, so subsequent ``task.merge`` has ``expected_head``.
2. P2-7 (mesh/daemon/terminate.py): ``terminate._active`` and ``Termination.seen`` must be
   thread-safe across concurrent merge worker threads and the main daemon loop.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from mesh.core import repos
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.proc import get_proc_stat
from mesh.daemon import merge, supervisor, terminate
from tests.gitfixture import GitFixture

MESH_PROC_LOCK = "shared"


def _wait_until(pred, timeout: float = 10.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def _kill_proc(pid: int, starttime: int) -> None:
    try:
        info = get_proc_stat(pid)
        if info is not None and int(info["starttime"]) == int(starttime):
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class TestP1_6LeftoversFinalizationGit(unittest.TestCase):
    """P1-6: leftovers finalization collects deliverable.git and defers while git runs."""

    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.repo = self.fixture.make_k0("P16")
        self.base = self.fixture.head(self.repo, "main")

        self.home = self.fixture.path("home")
        for sub in ("workspaces", "run", "runs", "logs", "merges"):
            os.makedirs(os.path.join(self.home, sub), mode=0o700, exist_ok=True)
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)

        self.project_id = "prj_p16"
        self.task_id = "tsk_p16"
        self.run_id = f"run_p16_{os.getpid()}"
        self.branch = f"mesh/{self.project_id}/{self.task_id}"
        self.ws_root = os.path.join(self.home, "workspaces")
        repos.setup_integration(self.repo, self.project_id, self.base, self.ws_root)
        self.integ_branch = repos.integration_branch_name(self.project_id)

        self.ws_path = os.path.join(self.ws_root, self.project_id, "tasks", self.task_id)
        os.makedirs(os.path.dirname(self.ws_path), mode=0o700, exist_ok=True)
        self.fixture.git(self.repo, "worktree", "add", "-b", self.branch, self.ws_path, self.base)
        work_file = os.path.join(self.ws_path, "feature.txt")
        with open(work_file, "w", encoding="utf-8") as f:
            f.write("implemented by task\n")
        self.fixture.git(self.ws_path, "add", "feature.txt")
        self.fixture.git(self.ws_path, "commit", "-q", "-m", "task work")
        self.head_sha = self.fixture.head(self.ws_path)

        self.run_dir = os.path.join(self.home, "runs", self.run_id)
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)

        self._reset_supervisor_state()
        self.addCleanup(self._reset_supervisor_state)

    def _reset_supervisor_state(self) -> None:
        terminate._active.clear()
        supervisor._CLEAR.clear()
        with supervisor._WS_PREP_LOCK:
            supervisor._GIT_INFO.clear()
        supervisor._SEEN.clear()
        supervisor._SEEN_UNUSABLE.clear()
        supervisor._SEEN_HOLD_TOKEN.clear()
        supervisor._SEEN_MARKER.clear()
        supervisor._SEEN_SIG.clear()
        supervisor._SEEN_PERSISTED.clear()
        supervisor._SEEN_REASON.clear()
        supervisor._SEEN_EVENT_PENDING.clear()
        supervisor._SEEN_EVENT_DONE.clear()
        supervisor._CLOSE_REFUSED_LOGGED.clear()

    def _seed_running_code_task(self, *, kind: str = "code", exit_code: int | None = 0) -> tuple[int, int]:
        wp = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        wpid = wp.pid
        wst = int(get_proc_stat(wpid)["starttime"])
        wp.kill()
        wp.wait(timeout=5)
        self.addCleanup(_kill_proc, wpid, wst)

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'ag1', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_1', 'fake', 'launch', 1, ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, base_commit, "
                "integration_branch, created_by, created_via, created_at, updated_at) "
                "VALUES (?, 'P16', 'g', 'active', ?, 'main', ?, ?, 'usr_1', 'direct', ?, ?);",
                (self.project_id, self.repo, self.base, self.integ_branch, now, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "workspace_path, branch, base_commit, timeout_s, max_attempts, attempt, created_by, "
                "created_at, updated_at) "
                "VALUES (?, ?, 'T16', 'spec', ?, 'running', 'agt_1', 'edit', ?, ?, ?, 600, 1, 1, 'usr_1', ?, ?);",
                (self.task_id, self.project_id, kind, self.ws_path, self.branch, self.base, now, now),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, wrapper_pid, pgid, proc_starttime, run_dir, started_at) "
                "VALUES (?, 'task', ?, ?, 'agt_1', 1, 1, 'fake', 'running', 'boot_1', ?, ?, ?, ?, ?);",
                (self.run_id, self.task_id, self.project_id, wpid, wpid, wst, self.run_dir, now),
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, "
                "expires_at, heartbeat_at) VALUES (?, ?, 'meshd', 'boot_1', 'agt_1', 1, ?, ?, ?);",
                (f"task:{self.task_id}", self.run_id, now, now_iso_utc(time.time() + 3600), now),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('kill.grace_s', '1', ?, 'test');",
                (now,),
            )

        with open(os.path.join(self.run_dir, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": wpid, "starttime": wst}, f)
        with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('{"summary": "done"}\n')
        if exit_code is not None:
            with open(os.path.join(self.run_dir, "exit.json"), "w", encoding="utf-8") as f:
                json.dump({"exit_code": exit_code, "signal": None, "ended_at": now}, f)
        return wpid, wst

    def test_p1_6_hold_close_leftovers_collects_deliverable_git_for_merge(self) -> None:
        wpid, wst = self._seed_running_code_task(kind="code", exit_code=0)
        member = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        mst = int(get_proc_stat(member.pid)["starttime"])
        self.addCleanup(lambda: (_kill_proc(member.pid, mst), member.wait()))

        # A live recorded member triggers CloseHeld on an exit-0 close, and hold_close
        # turns that into a leftovers termination while task is still 'running'.
        supervisor._SEEN[self.run_id] = {wpid: wst, member.pid: mst}
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        with self.assertRaises(supervisor.CloseHeld) as cm:
            supervisor._close_run(self.conn, run, "exited", "exited", 0, {})
        self.assertIn(member.pid, cm.exception.live)
        supervisor.hold_close(self.conn, self.home, run, cm.exception)
        self.assertEqual(terminate.get(self.run_id).end_reason, "leftovers")

        member.kill()
        member.wait(timeout=5)

        def step_until_closed() -> bool:
            r = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
            if r["status"] not in ("starting", "running"):
                return True
            supervisor.advance_terminations(self.conn, self.home, testing_mode=True)
            return False

        self.assertTrue(_wait_until(step_until_closed, timeout=15.0, step=0.05), "run never closed")

        r = self.conn.execute("SELECT status, exit_code, end_reason FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        self.assertEqual(tuple(r), ("exited", 0, "exited"))

        task = self.conn.execute("SELECT * FROM tasks WHERE id = ?;", (self.task_id,)).fetchone()
        self.assertEqual(task["status"], "submitted")
        self.assertEqual(
            merge.reviewed_head(task["deliverable_json"]),
            self.head_sha,
            f"deliverable.git.head missing or wrong: {task['deliverable_json']}",
        )
        git_meta = json.loads(task["deliverable_json"])["deliverable"]["git"]
        self.assertEqual(git_meta["branch"], self.branch)
        self.assertEqual(git_meta["base_commit"], self.base)
        self.assertFalse(git_meta["dirty"])

        # Verify task.merge succeeds using reviewed_head (previously failed WORKTREE_DIRTY with expected_head=None).
        integ = repos.integration_worktree_path(self.ws_root, self.project_id)
        plan = {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "title": task["title"],
            "branch": self.branch,
            "expected_head": merge.reviewed_head(task["deliverable_json"]),
            "worktree": self.ws_path,
            "integration": integ,
            "integration_branch": self.integ_branch,
            "merge_id": "mrg_p16",
            "run_id": "run_mrg_p16",
            "run_dir": os.path.join(self.home, "merges", "mrg_p16"),
            "pre_head": self.base,
            "started_at": now_iso_utc(),
            "test_argv": None,
            "test_timeout_s": 30,
            "kill_grace_s": 1,
        }
        os.makedirs(plan["run_dir"], mode=0o700, exist_ok=True)
        out = merge.run_merge_pipeline(plan)
        self.assertEqual(out["outcome"], "merged", out)

    def test_p1_6_leftovers_defers_finalization_until_git_ready_and_renews_lease(self) -> None:
        wpid, wst = self._seed_running_code_task(kind="code", exit_code=0)
        old_hb = now_iso_utc(time.time() - 30)
        old_exp = now_iso_utc(time.time() + 5)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE key = ?;",
                (old_hb, old_exp, f"task:{self.task_id}"),
            )
        t = terminate.begin(self.run_id, wpid, wpid, "leftovers", 1.0, wrapper_starttime=wst)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        lease_before = self.conn.execute(
            "SELECT heartbeat_at, expires_at FROM leases WHERE key = ?;", (f"task:{self.task_id}",)
        ).fetchone()

        gate = threading.Event()
        entered = threading.Event()
        real_collect = repos.submission_git_info

        def gated_collect(*args, **kwargs):
            entered.set()
            gate.wait(timeout=10.0)
            return real_collect(*args, **kwargs)

        try:
            with mock.patch.object(supervisor, "submission_git_info", gated_collect):
                # Advance two empty scans so terminate.step(t) returns "done".
                self.assertIsNone(supervisor._advance(self.conn, self.home, run, t, testing_mode=True))
                time.sleep(terminate.POLL_S + 0.02)

                # First "done" starts background git collection and must defer without forgetting t or dropping lease.
                res = supervisor._advance(self.conn, self.home, run, t, testing_mode=True)
                self.assertIsNone(res)
                self.assertTrue(entered.wait(timeout=5.0))
                self.assertIs(terminate.get(self.run_id), t)

                lease_mid = self.conn.execute(
                    "SELECT heartbeat_at, expires_at FROM leases WHERE key = ?;", (f"task:{self.task_id}",)
                ).fetchone()
                self.assertIsNotNone(lease_mid)
                self.assertGreater(lease_mid["heartbeat_at"], lease_before["heartbeat_at"])
                self.assertGreater(lease_mid["expires_at"], lease_before["expires_at"])

                task_mid = self.conn.execute("SELECT status FROM tasks WHERE id = ?;", (self.task_id,)).fetchone()
                self.assertEqual(task_mid["status"], "running")

                gate.set()
                self.assertTrue(
                    _wait_until(
                        lambda: supervisor._GIT_INFO.get(self.run_id, {}).get("done") is True,
                        timeout=5.0,
                    )
                )

                # Subsequent scans reach "done" and finalize auto-submit with deliverable.git.
                final_res = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    final_res = supervisor._advance(self.conn, self.home, run, t, testing_mode=True)
                    if final_res is not None:
                        break
                    time.sleep(terminate.POLL_S / 2 + 0.02)

                self.assertEqual(final_res, "auto_submitted")
                self.assertIsNone(terminate.get(self.run_id))
                task_after = self.conn.execute("SELECT status, deliverable_json FROM tasks WHERE id = ?;",
                                               (self.task_id,)).fetchone()
                self.assertEqual(task_after["status"], "submitted")
                self.assertEqual(merge.reviewed_head(task_after["deliverable_json"]), self.head_sha)
        finally:
            gate.set()

    def test_p1_6_nonzero_exit_leftovers_does_not_collect_git(self) -> None:
        wpid, wst = self._seed_running_code_task(kind="code", exit_code=1)
        t = terminate.begin(self.run_id, wpid, wpid, "leftovers", 1.0, wrapper_starttime=wst)
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (self.run_id,)).fetchone()
        with mock.patch.object(supervisor, "_submit_git_state", side_effect=AssertionError("should not collect git")):
            self.assertIsNone(supervisor._advance(self.conn, self.home, run, t, testing_mode=True))
            time.sleep(terminate.POLL_S + 0.02)
            res = supervisor._advance(self.conn, self.home, run, t, testing_mode=True)
        self.assertEqual(res, "exit_nonzero")
        self.assertIsNone(terminate.get(self.run_id))


class TestP2_7TerminateThreadSafety(unittest.TestCase):
    """P2-7: terminate._active and Termination.seen are safe under concurrent thread access."""

    def setUp(self) -> None:
        terminate._active.clear()
        self.addCleanup(terminate._active.clear)

    def test_p2_7_concurrent_begin_same_run_id_returns_single_instance(self) -> None:
        barrier = threading.Barrier(2)
        results: list[terminate.Termination] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def fake_targets(run_id, pgid, wrapper_pid, wrapper_starttime=None, seen=None, unknown=None):
            barrier.wait(timeout=5.0)
            return {}

        def worker() -> None:
            try:
                t = terminate.begin("run_race_same", None, None, "cancelled", 5.0)
                with lock:
                    results.append(t)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        with mock.patch.object(terminate, "run_targets", fake_targets):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], results[1])
        self.assertIs(terminate.get("run_race_same"), results[0])

    def test_p2_7_concurrent_active_kill_failed_runs_step_and_forget(self) -> None:
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-5)
        self.addCleanup(sys.setswitchinterval, old_interval)

        # Keep several kill_failed terminations active whose t.seen dicts mutate concurrently
        # while kill_failed_runs() iterates _active.values() and sorted(t.seen).
        fixed_terms: list[terminate.Termination] = []
        for idx in range(10):
            ft = terminate.Termination(
                run_id=f"run_kf_{idx}",
                pgid=None,
                wrapper_pid=None,
                end_reason="timeout",
                grace_s=0.0,
                phase="failed",
                kill_failed=True,
                kill_failed_count=1,
                seen={100000 + idx: 123},
            )
            terminate._active[ft.run_id] = ft
            fixed_terms.append(ft)

        # Pre-seed additional entries so iteration over _active.values() spans many items.
        for idx in range(80):
            terminate._active[f"run_seed_{idx}"] = terminate.Termination(
                run_id=f"run_seed_{idx}",
                pgid=None,
                wrapper_pid=None,
                end_reason="cancelled",
                grace_s=1.0,
                kill_failed=True,
                seen={200000 + idx: 456},
            )

        stop = threading.Event()
        errors: list[BaseException] = []
        err_lock = threading.Lock()
        counter = [0]

        def fake_run_targets(run_id, pgid, wrapper_pid, wrapper_starttime=None, seen=None, unknown=None):
            if run_id.startswith("run_kf_"):
                with err_lock:
                    counter[0] += 1
                    cval = counter[0]
                return {300000 + (cval % 20): cval}
            return {}

        def writer(wid: int) -> None:
            try:
                for i in range(400):
                    if stop.is_set():
                        break
                    rid = f"run_w_{wid}_{i % 40}"
                    terminate.begin(rid, None, None, "cancelled", 1.0)
                    ft = fixed_terms[i % len(fixed_terms)]
                    terminate.step(ft)
                    terminate.forget(rid)
            except BaseException as exc:
                stop.set()
                with err_lock:
                    errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(800):
                    if stop.is_set():
                        break
                    kf = terminate.kill_failed_runs()
                    self.assertIsInstance(kf, list)
                    act = terminate.active()
                    self.assertIsInstance(act, dict)
            except BaseException as exc:
                stop.set()
                with err_lock:
                    errors.append(exc)

        with mock.patch.object(terminate, "run_targets", fake_run_targets):
            threads = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
            threads.extend(threading.Thread(target=reader) for _ in range(3))
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=20.0)

        self.assertEqual(errors, [], f"Concurrent terminate access raised: {errors}")


if __name__ == "__main__":
    unittest.main()
