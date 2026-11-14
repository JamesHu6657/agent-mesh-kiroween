"""N18 (plan 11.4, 11.7; checklist A8): an adopted run's group stays trusted after
its wrapper exited while a member recorded in ``seen.json`` still lives.

The member has an empty environment (no ``MESH_RUN_ID``), stays in the run's
process group and session, ignores SIGTERM and blocks on a pipe read. After
``kill -9`` of meshd and a restart the run is adopted; the adopted wrapper is
init's child, so once it exits its pgid alone proves nothing. Before the fix the
run closed and the ``task:<id>`` lease was released while the member still ran.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from mesh.core.proc import get_proc_stat
from mesh.daemon import supervisor
from mesh.testing import fake_agent
from tests.recovery.test_recovery import RecoveryBase, _alive


class TestRecoveryAdoptSeen(RecoveryBase):
    def _member(self, path: str) -> Optional[int]:
        lines = self.pid_lines(path)
        return int(lines[0]) if lines else None

    def _guard_member(self, pid: int) -> None:
        """Cleanup if the test fails early: SIGKILL the member only if it is the same process."""
        info = get_proc_stat(pid)
        starttime = info["starttime"] if info else None

        def _cleanup() -> None:
            cur = get_proc_stat(pid)
            if cur is not None and starttime is not None and cur["starttime"] == starttime \
                    and cur["state"] != "Z":
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(_cleanup)

    def _lease(self, task_id: str) -> Any:
        return self.q1("SELECT * FROM leases WHERE key = ?;", (f"task:{task_id}",))

    def _run(self, run_id: str) -> Any:
        return self.q1("SELECT * FROM runs WHERE id = ?;", (run_id,))

    def _seen_members(self, run: Any) -> List[Dict[str, int]]:
        path = os.path.join(run["run_dir"], "seen.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return list(json.load(f).get("members") or [])
        except ValueError:
            return []

    def _adopt(self, spec: str) -> Any:
        self.set_setting("kill.grace_s", "2")
        task_id = self.create_task(spec)
        run = self.wait_running_run(task_id)
        self.wait_wrapper_started(run)
        self.harness.kill9_daemon()
        self.assertTrue(_alive(run["wrapper_pid"]), "the run must survive meshd's death")
        self.harness.restart_daemon()
        self.assertEqual(len(self.events("run.adopted", task_id)), 1)
        return task_id, run

    def test_adopted_wrapper_exit_with_envless_member(self) -> None:
        """ACC: N18, A8 - adopted run, wrapper exits, env-less SIGTERM-ignoring member
        lives: run open and task lease held (also through the kill.grace_s window after
        SIGTERM); then SIGKILL, member gone, lease released, seen.json listed it."""
        pidfile = os.path.join(self.harness.fixtures_dir, "member-pid")
        trigger = os.path.join(self.harness.fixtures_dir, "member-trigger")
        spec = ('FAKE {"steps":[{"spawn_envless_member":{"path":"%s","trigger":"%s"}},'
                '{"succeed":{}}]}' % (pidfile, trigger))
        self.set_setting("kill.grace_s", "2")
        task_id = self.create_task(spec)
        run = self.wait_running_run(task_id)
        self.assertTrue(self.harness.wait_for(lambda: self._member(pidfile) is not None, timeout=20),
                        "member not started")
        member = self._member(pidfile)
        self._guard_member(member)
        member_st = get_proc_stat(member)["starttime"]
        info = get_proc_stat(member)
        self.assertEqual(info["pgrp"], run["pgid"])
        self.assertEqual(info["session"], run["pgid"])

        self.harness.kill9_daemon()
        self.assertTrue(_alive(run["wrapper_pid"]), "the run must survive meshd's death")
        self.harness.restart_daemon()
        self.assertEqual(len(self.events("run.adopted", task_id)), 1)
        # The adoption scan records the member while the wrapper identity holds.
        self.assertTrue(self.harness.wait_for(
            lambda: {"pid": member, "starttime": member_st} in self._seen_members(run), timeout=15),
            f"seen.json does not list the member: {self._seen_members(run)}")

        with open(trigger, "w", encoding="utf-8") as f:
            f.write("go")
        self.assertTrue(self.harness.wait_for(lambda: not _alive(run["wrapper_pid"]), timeout=20),
                        "wrapper did not exit")

        violations: List[str] = []
        sigterm_seen = {"v": False}

        def member_gone() -> bool:
            alive = _alive(member)
            if alive:
                r = self._run(run["id"])
                if r["status"] not in ("starting", "running"):
                    violations.append(f"run {r['status']} while member alive")
                if self._lease(task_id) is None:
                    violations.append("task lease released while member alive")
                if self.events("run.leftovers_terminated", task_id):
                    sigterm_seen["v"] = True
            return not alive

        self.assertTrue(self.harness.wait_for(member_gone, timeout=40, step=0.1),
                        "member was never killed")
        self.assertEqual(violations, [])
        self.assertTrue(sigterm_seen["v"], "no observation inside the termination/grace window")
        term = self.events("run.leftovers_terminated", task_id)
        self.assertEqual(len(term), 1)
        self.assertIn(member, json.loads(term[0]["payload_json"])["pids"])

        self.assertTrue(self.harness.wait_for(lambda: self._lease(task_id) is None, timeout=20),
                        "task lease not released after the member died")
        self.assertTrue(self.harness.wait_for(
            lambda: self._run(run["id"])["status"] not in ("starting", "running"), timeout=20))
        self.assertIn({"pid": member, "starttime": member_st}, self._seen_members(run))
        self.doctor_ok()

    def test_adopted_wrapper_exit_without_member_closes_normally(self) -> None:
        """ACC: N18 (control) - the same adopted flow without a member closes normally."""
        trigger = os.path.join(self.harness.fixtures_dir, "ctl-trigger")
        spec = 'FAKE {"steps":[{"wait_release":{"path":"%s"}},{"succeed":{}}]}' % trigger
        task_id, run = self._adopt(spec)
        self.assertTrue(self.harness.wait_for(lambda: os.path.exists(
            os.path.join(run["run_dir"], "seen.json")), timeout=15), "no seen.json for the adopted run")
        with open(trigger, "w", encoding="utf-8") as f:
            f.write("go")
        self.wait_status(task_id, "submitted", timeout=40)
        self.assertTrue(self.harness.wait_for(lambda: self._lease(task_id) is None, timeout=20))
        r = self._run(run["id"])
        self.assertEqual(r["status"], "exited")
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(self.events("run.leftovers_terminated", task_id), [])
        self.doctor_ok()


    def test_second_restart_wrapper_lost_member_alive(self) -> None:
        """ACC: N18, A8 (r4b item 1) - restart twice with the env-less member alive across
        both restarts. Before the second restart the wrapper and the fake agent are
        SIGKILLed (no exit.json, no MESH_RUN_ID carrier left), so recovery takes the
        `lost` path. The persisted seen.json keeps the group trusted: the run stays open
        and `task:<id>` is held while the member lives; meshd SIGKILLs it after the
        grace window; then the lease is released, the run closes lost, doctor ok."""
        pidfile = os.path.join(self.harness.fixtures_dir, "member2-pid")
        trigger = os.path.join(self.harness.fixtures_dir, "member2-trigger")
        spec = ('FAKE {"steps":[{"spawn_envless_member":{"path":"%s","trigger":"%s"}},'
                '{"succeed":{}}]}' % (pidfile, trigger))
        self.set_setting("kill.grace_s", "2")
        task_id = self.create_task(spec)
        run = self.wait_running_run(task_id)
        self.assertTrue(self.harness.wait_for(lambda: self._member(pidfile) is not None, timeout=20),
                        "member not started")
        member = self._member(pidfile)
        self._guard_member(member)
        info = get_proc_stat(member)
        member_st = info["starttime"]
        fake_pid = int(info["ppid"])
        self.assertNotEqual(fake_pid, run["wrapper_pid"])
        self._guard_member(fake_pid)

        # First restart: adopted, member recorded.
        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        self.assertEqual(len(self.events("run.adopted", task_id)), 1)
        self.assertTrue(self.harness.wait_for(
            lambda: {"pid": member, "starttime": member_st} in self._seen_members(run), timeout=15),
            f"seen.json does not list the member: {self._seen_members(run)}")

        # Second restart with the wrapper (and the fake agent) gone without exit.json.
        self.harness.kill9_daemon()
        os.kill(run["wrapper_pid"], signal.SIGKILL)
        self.assertTrue(self.harness.wait_for(lambda: not _alive(run["wrapper_pid"]), timeout=10))
        os.kill(fake_pid, signal.SIGKILL)
        self.assertTrue(self.harness.wait_for(lambda: not _alive(fake_pid), timeout=10))
        self.assertFalse(os.path.exists(os.path.join(run["run_dir"], "exit.json")))
        self.assertTrue(_alive(member))
        self.harness.restart_daemon()
        self.assertEqual(len(self.events("run.adopted", task_id)), 1, "the dead wrapper must not be adopted")

        violations: List[str] = []
        observed = {"n": 0}

        def member_gone() -> bool:
            alive = _alive(member)
            if alive:
                observed["n"] += 1
                r = self._run(run["id"])
                if r["status"] not in ("starting", "running"):
                    violations.append(f"run {r['status']} while member alive")
                if self._lease(task_id) is None:
                    violations.append("task lease released while member alive")
            return not alive

        self.assertTrue(self.harness.wait_for(member_gone, timeout=40, step=0.1),
                        f"member was never killed; run {dict(self._run(run['id']))}")
        self.assertEqual(violations, [])
        self.assertGreater(observed["n"], 0, "no observation while the member lived")
        self.assertTrue(self.harness.wait_for(lambda: self._lease(task_id) is None, timeout=20),
                        "task lease not released after the member died")
        self.assertTrue(self.harness.wait_for(
            lambda: self._run(run["id"])["status"] not in ("starting", "running"), timeout=20))
        self.assertEqual(self._run(run["id"])["status"], "lost")
        self.doctor_ok()

    def _meshd_log(self) -> str:
        path = os.path.join(self.harness.home, "logs", "meshd.log")
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def test_seen_write_failure_persisted_while_terminating(self) -> None:
        """ACC: N18, A8 (r4c item 1) - seen.json cannot be written at adoption (a
        directory sits at the path, so the atomic replace fails); the run is then
        terminated (task cancel, SIGTERM-ignoring member, long grace); storage recovers
        (directory removed) and the members are persisted from the termination ticks;
        kill -9 meshd and restart: the second boot still sees the member and holds the
        run and `task:<id>` until it is gone."""
        pidfile = os.path.join(self.harness.fixtures_dir, "member3-pid")
        trigger = os.path.join(self.harness.fixtures_dir, "member3-trigger")
        spec = ('FAKE {"steps":[{"spawn_envless_member":{"path":"%s","trigger":"%s"}},'
                '{"succeed":{}}]}' % (pidfile, trigger))
        self.set_setting("kill.grace_s", "120")
        task_id = self.create_task(spec)
        run = self.wait_running_run(task_id)
        self.assertTrue(self.harness.wait_for(lambda: self._member(pidfile) is not None, timeout=20),
                        "member not started")
        member = self._member(pidfile)
        self._guard_member(member)
        info = get_proc_stat(member)
        member_st = info["starttime"]
        self._guard_member(int(info["ppid"]))
        seen_path = os.path.join(run["run_dir"], "seen.json")

        # Storage fault: a non-empty directory at seen.json, so every write fails.
        self.harness.kill9_daemon()
        os.makedirs(os.path.join(seen_path, "blocker"))
        self.harness.restart_daemon()
        self.assertEqual(len(self.events("run.adopted", task_id)), 1)
        self.assertTrue(self.harness.wait_for(
            lambda: f"run {run['id']}: seen.json write failed" in self._meshd_log(), timeout=15),
            "the adoption scan did not try (and fail) to write seen.json")
        self.assertTrue(os.path.isdir(seen_path))

        rc, resp = self.harness.cli("task", "cancel", task_id, "--reason", "stop", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(self.harness.wait_for(
            lambda: f"terminating run {run['id']}" in self._meshd_log(), timeout=15), "no termination")
        self.assertTrue(_alive(member))

        # Storage recovers inside the grace window: the members are persisted.
        shutil.rmtree(seen_path)
        self.assertTrue(self.harness.wait_for(
            lambda: {"pid": member, "starttime": member_st} in self._seen_members(run), timeout=15),
            f"seen.json not written while terminating: {self._seen_members(run)}")
        self.assertTrue(_alive(member), "member must still be in the grace window")
        self.assertIn(self._run(run["id"])["status"], ("starting", "running"))

        # r4d item 4: the restart keeps kill.grace_s=120, so meshd cannot SIGKILL the
        # member before we observed it: the observation window is guaranteed by the
        # setting, not by timing. Sync point: this boot's recovery started the
        # termination (meshd.log, after the restart offset).
        self.harness.kill9_daemon()
        self.assertTrue(_alive(member))
        offset = len(self._meshd_log())
        self.harness.restart_daemon()
        self.assertTrue(self.harness.wait_for(
            lambda: f"terminating run {run['id']}" in self._meshd_log()[offset:], timeout=15),
            "recovery did not start the termination")

        violations: List[str] = []
        observed = {"n": 0}

        def held_tick() -> bool:
            if self._check_held(run, task_id, member, member_st, violations):
                observed["n"] += 1
            return observed["n"] >= 5
        self.assertTrue(self.harness.wait_for(held_tick, timeout=30, step=0.1),
                        f"member not observed alive in the grace window: {observed}")
        self.assertEqual(violations, [])

        # Then a short grace and one more restart: meshd SIGKILLs the member.
        self.set_setting("kill.grace_s", "2")
        self.harness.kill9_daemon()
        self.assertTrue(self._same_alive(member, member_st))
        self.harness.restart_daemon()

        def member_gone() -> bool:
            self._check_held(run, task_id, member, member_st, violations)
            return not self._same_alive(member, member_st)

        self.assertTrue(self.harness.wait_for(member_gone, timeout=40, step=0.1),
                        f"member was never killed; run {dict(self._run(run['id']))}")
        self.assertEqual(violations, [])
        self.assertTrue(self.harness.wait_for(lambda: self._lease(task_id) is None, timeout=20),
                        "task lease not released after the member died")
        self.assertTrue(self.harness.wait_for(
            lambda: self._run(run["id"])["status"] not in ("starting", "running"), timeout=20))
        self.doctor_ok()

    @staticmethod
    def _same_alive(pid: int, starttime: int) -> bool:
        info = get_proc_stat(pid)
        return info is not None and info.get("state") != "Z" and int(info["starttime"]) == int(starttime)

    def _check_held(self, run: Any, task_id: str, member: int, member_st: int,
                    violations: List[str]) -> bool:
        """r4d item 4: read the run and lease, then re-confirm that the same member
        (pid + starttime) is still alive. A closed run or a released lease counts as a
        violation only when the member was alive before and after the read; returns
        whether this was such an observation."""
        if not self._same_alive(member, member_st):
            return False
        r = self._run(run["id"])
        lease = self._lease(task_id)
        if not self._same_alive(member, member_st):
            return False
        if r["status"] not in ("starting", "running"):
            violations.append(f"run {r['status']} while member alive")
        if lease is None:
            violations.append("task lease released while member alive")
        return True


def _spawn_session_leader() -> subprocess.Popen:
    """A child in its own session (pgid == sid == pid) blocking on a pipe read."""
    return subprocess.Popen([sys.executable, "-c", "import os; r, w = os.pipe(); os.read(r, 1)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class TestSeenState(unittest.TestCase):
    """r4b items 2 and 3: seen.json persistence retry and load validation (in-process)."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="mh-seen-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.leader = _spawn_session_leader()
        self.addCleanup(self._kill, self.leader)
        st = get_proc_stat(self.leader.pid)["starttime"]
        self.run_row = {"id": "run_seen_test", "run_dir": self.dir, "pgid": self.leader.pid,
                        "wrapper_pid": self.leader.pid, "proc_starttime": st,
                        "kind": "task", "task_id": None}
        self.addCleanup(self._forget)
        self._forget()

    def _kill(self, p: subprocess.Popen) -> None:
        if p.poll() is None:
            p.kill()
        p.wait()

    def _forget(self) -> None:
        supervisor._evict_seen(set())

    def _write(self, doc: Any, raw: Optional[str] = None) -> None:
        with open(os.path.join(self.dir, "seen.json"), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(doc))

    def _good(self, **over: Any) -> Dict[str, Any]:
        doc = {"run_id": "run_seen_test", "pgid": self.leader.pid, "sid": self.leader.pid,
               "members": [{"pid": self.leader.pid, "starttime": self.run_row["proc_starttime"]}]}
        doc.update(over)
        return doc

    def test_failed_write_is_retried_without_membership_change(self) -> None:
        """ACC: N18 (r4b item 2) - one injected seen.json write failure is logged and a
        later tick writes the file although the membership did not change."""
        real = supervisor._write_seen
        calls = {"n": 0}

        def flaky(run: Any, seen: Dict[int, int]) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(28, "No space left on device")
            real(run, seen)

        path = os.path.join(self.dir, "seen.json")
        with mock.patch.object(supervisor, "_write_seen", flaky), \
                mock.patch.object(supervisor, "_log_meshd") as log:
            supervisor.track_adopted_seen(self.run_row, self.dir)  # scan tick: write fails
            self.assertFalse(os.path.exists(path))
            self.assertTrue(any("write failed" in str(c.args[1]) for c in log.call_args_list),
                            log.call_args_list)
            supervisor.track_adopted_seen(self.run_row, self.dir)  # same members: retried
        self.assertTrue(os.path.exists(path), "seen.json not rewritten after the failure")
        self.assertEqual(calls["n"], 2)
        state, members = supervisor.load_seen_state(self.run_row)
        self.assertEqual(state, supervisor.SEEN_OK)
        self.assertEqual(members, {self.leader.pid: self.run_row["proc_starttime"]})
        self.assertNotIn(self.run_row["id"], supervisor._SEEN_DIRTY)

    def test_load_missing_is_absent(self) -> None:
        """ACC: N18 (r4b item 3) - no seen.json: no members recorded."""
        self.assertEqual(supervisor.load_seen_state(self.run_row), (supervisor.SEEN_ABSENT, {}))

    def test_load_valid(self) -> None:
        """ACC: N18 (r4b item 3) - a valid file loads its members."""
        self._write(self._good())
        self.assertEqual(supervisor.load_seen_state(self.run_row),
                         (supervisor.SEEN_OK, {self.leader.pid: self.run_row["proc_starttime"]}))

    def test_load_unusable_cases(self) -> None:
        """ACC: N18 (r4b item 3) - corrupt JSON, wrong types and metadata mismatches are
        `unusable` (never an exception)."""
        cases = {
            "corrupt": (None, "{not json"),
            "not_object": ([1, 2], None),
            "members_int": (self._good(members=1), None),
            "members_str": (self._good(members="x"), None),
            "member_not_dict": (self._good(members=[5]), None),
            "member_pid_str": (self._good(members=[{"pid": "5", "starttime": 1}]), None),
            "member_bool": (self._good(members=[{"pid": True, "starttime": 1}]), None),
            "member_missing_starttime": (self._good(members=[{"pid": 5}]), None),
            "run_id_mismatch": (self._good(run_id="run_other"), None),
            "pgid_mismatch": (self._good(pgid=self.leader.pid + 1), None),
            "sid_mismatch": (self._good(sid=self.leader.pid + 1), None),
            "sid_wrong_type": (self._good(sid=str(self.leader.pid)), None),
            "pgid_wrong_type": (self._good(pgid=str(self.leader.pid)), None),
        }
        for name, (doc, raw) in cases.items():
            with self.subTest(name):
                self._write(doc, raw)
                self.assertEqual(supervisor.load_seen_state(self.run_row), (supervisor.SEEN_UNUSABLE, {}))

    def test_seen_for_members_int_does_not_raise(self) -> None:
        """ACC: N18 (r4b item 3) - `"members": 1` no longer raises TypeError."""
        self._write(self._good(members=1))
        with mock.patch.object(supervisor, "_log_meshd") as log:
            self.assertEqual(supervisor.seen_for(self.run_row, self.dir), {})
        self.assertIn(self.run_row["id"], supervisor._SEEN_UNUSABLE)
        self.assertTrue(any("unusable" in str(c.args[1]) for c in log.call_args_list))

    def test_unusable_holds_run_while_group_occupied(self) -> None:
        """ACC: N18 (r4b item 3) - unusable seen.json, wrapper gone: held while a process
        is in the run's group/session; released once the group is empty. A missing file
        never holds (no members recorded)."""
        row = dict(self.run_row, wrapper_pid=_dead_pid())
        self._write(self._good(members=1))
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(supervisor.seen_hold(row))
            self._kill(self.leader)
            self.assertFalse(supervisor.seen_hold(row))
        self._forget()
        os.unlink(os.path.join(self.dir, "seen.json"))
        other = _spawn_session_leader()
        self.addCleanup(self._kill, other)
        row2 = dict(row, pgid=other.pid)
        self.assertFalse(supervisor.seen_hold(row2))

    def test_unusable_rewritten_while_wrapper_alive(self) -> None:
        """ACC: N18 (r4b item 3) - with the wrapper identity alive, an unusable file is
        replaced by a fresh scan (trust kept, not dropped)."""
        self._write(None, "garbage")
        with mock.patch.object(supervisor, "_log_meshd"):
            supervisor.track_adopted_seen(self.run_row, self.dir)
        self.assertEqual(supervisor.load_seen_state(self.run_row),
                         (supervisor.SEEN_OK, {self.leader.pid: self.run_row["proc_starttime"]}))
        self.assertNotIn(self.run_row["id"], supervisor._SEEN_UNUSABLE)

    def _reason_of(self) -> Optional[str]:
        supervisor._evict_seen(set())
        with mock.patch.object(supervisor, "_log_meshd"):
            supervisor.seen_for(self.run_row, self.dir)
        return supervisor._SEEN_REASON.get(self.run_row["id"]) \
            if self.run_row["id"] in supervisor._SEEN_UNUSABLE else None

    def test_unusable_reasons(self) -> None:
        """ACC: N18 (r4c item 3.3) - each unusable file names its reason."""
        cases = {
            "invalid_json": (None, "{not json"),
            "bad_type": (self._good(members=1), None),
            "missing_fields": ({"run_id": "run_seen_test", "pgid": self.leader.pid}, None),
            "run_id_mismatch": (self._good(run_id="run_other"), None),
            "pgid_mismatch": (self._good(pgid=self.leader.pid + 1), None),
            "sid_mismatch": (self._good(sid=self.leader.pid + 1), None),
        }
        for reason, (doc, raw) in cases.items():
            with self.subTest(reason):
                self._write(doc, raw)
                self.assertEqual(self._reason_of(), reason)

    def test_unreadable_path_is_unusable_not_absent(self) -> None:
        """ACC: N18 (r4c item 2) - a path that exists but cannot be read (a directory; a
        chmod-000 file when not root, a symlink loop) is `unusable`, never `absent`.
        Only FileNotFoundError means absent."""
        path = os.path.join(self.dir, "seen.json")
        # A symlink loop: os.path.exists() says False (ELOOP), open() raises ELOOP.
        os.symlink("seen.json", path)
        self.assertEqual(supervisor.load_seen_state(self.run_row), (supervisor.SEEN_UNUSABLE, {}))
        os.unlink(path)
        if os.geteuid() != 0:
            # A run_dir that cannot be searched: exists() says False, open() raises EACCES.
            self._write(self._good())
            os.chmod(self.dir, 0o600)
            try:
                self.assertEqual(supervisor.load_seen_state(self.run_row), (supervisor.SEEN_UNUSABLE, {}))
            finally:
                os.chmod(self.dir, 0o700)
            os.unlink(path)
        os.mkdir(path)
        self.assertEqual(supervisor.load_seen_state(self.run_row), (supervisor.SEEN_UNUSABLE, {}))
        self.assertEqual(self._reason_of(), "unreadable")
        os.rmdir(path)
        if os.geteuid() != 0:
            self._write(self._good())
            os.chmod(path, 0)
            self.addCleanup(os.chmod, path, 0o600)
            self.assertEqual(supervisor.load_seen_state(self.run_row), (supervisor.SEEN_UNUSABLE, {}))
            self.assertEqual(self._reason_of(), "unreadable")

    def test_legacy_file_without_sid_is_usable(self) -> None:
        """ACC: N18 (r4c item 3.1) - an r4-format seen.json (no `sid`) stays usable; its
        members count only while alive with their starttime and pgrp == pgid."""
        other = _spawn_session_leader()  # alive, but its own pgrp: not in the run's group
        self.addCleanup(self._kill, other)
        other_st = get_proc_stat(other.pid)["starttime"]
        doc = {k: v for k, v in self._good().items() if k != "sid"}
        doc["members"].append({"pid": other.pid, "starttime": other_st})
        doc["members"].append({"pid": _dead_pid(), "starttime": 1})
        self._write(doc)
        state, members = supervisor.load_seen_state(self.run_row)
        self.assertEqual(state, supervisor.SEEN_OK)
        self.assertEqual(len(members), 3)
        with mock.patch.object(supervisor, "_log_meshd"):
            got = supervisor.seen_for(self.run_row, self.dir)
        self.assertEqual(got, {self.leader.pid: self.run_row["proc_starttime"]})
        self.assertNotIn(self.run_row["id"], supervisor._SEEN_UNUSABLE)
        row = dict(self.run_row, wrapper_pid=_dead_pid())
        self.assertFalse(supervisor.seen_hold(row))
        # A wrong starttime is not trusted either.
        supervisor._evict_seen(set())
        doc["members"] = [{"pid": self.leader.pid, "starttime": self.run_row["proc_starttime"] + 1}]
        self._write(doc)
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertEqual(supervisor.seen_for(self.run_row, self.dir), {})

    def test_held_run_reloads_repaired_file(self) -> None:
        """ACC: N18 (r4c item 3.2) - while held (unusable file, wrapper gone, group
        occupied) a change of the file is re-read: a still-bad file keeps the hold, a
        repaired one is `ok` and its members are used again. Deleting the file does
        not lift the hold (only a valid file does)."""
        row = dict(self.run_row, wrapper_pid=_dead_pid())
        self._write(None, "garbage")
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(supervisor.seen_hold(row))
            self.assertEqual(supervisor._SEEN_REASON[row["id"]], "invalid_json")
            os.unlink(os.path.join(self.dir, "seen.json"))
            self.assertTrue(supervisor.seen_hold(row), "a deleted file must not lift the hold")
            self._write(self._good(run_id="run_other"))
            self.assertTrue(supervisor.seen_hold(row))
            self.assertEqual(supervisor._SEEN_REASON[row["id"]], "run_id_mismatch")
            self._write(self._good())
            self.assertFalse(supervisor.seen_hold(row))
            self.assertNotIn(row["id"], supervisor._SEEN_UNUSABLE)
            self.assertEqual(supervisor.seen_for(row, self.dir),
                             {self.leader.pid: self.run_row["proc_starttime"]})


class TestEnvlessMemberReady(unittest.TestCase):
    def test_member_ready_before_pid_recorded(self) -> None:
        """ACC: N18 (r4b item 5) - the pid file is written only after the member has
        installed SIG_IGN for SIGTERM (readiness handshake): it reads `<pid>`, `ready`,
        and an immediate SIGTERM does not end the member."""
        d = tempfile.mkdtemp(prefix="mh-ready-", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "pid")
        fake_agent.execute_behavior("spawn_envless_member", {"path": path})
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().split()
        # r4c item 4: register the cleanup before any assertion, so a failing check
        # (e.g. on code without the readiness line) never leaks the member.
        pid = int(lines[0]) if lines and lines[0].isdigit() else None

        def _cleanup() -> None:
            if pid is None:
                return
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        self.addCleanup(_cleanup)
        self.assertIsNotNone(pid, lines)
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(lines[1], "ready")
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            sigign = [ln for ln in f if ln.startswith("SigIgn:")][0].split()[1]
        self.assertTrue(int(sigign, 16) & (1 << (signal.SIGTERM - 1)), "SIGTERM not ignored yet")
        os.kill(pid, signal.SIGTERM)
        self.assertEqual(os.waitpid(pid, os.WNOHANG), (0, 0))
        self.assertEqual(get_proc_stat(pid)["state"] != "Z", True)

if __name__ == "__main__":
    unittest.main()
