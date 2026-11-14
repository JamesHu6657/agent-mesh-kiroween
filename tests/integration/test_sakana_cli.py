"""Sakana review of public PR #4, CLI half, end to end:

P2-1 `task merge --all-accepted` after a mid-batch failure: the batch stops at the first
failure, the integration worktree is left clean and the project merge lock is free (a
later merge in the same project goes through), and the result names every task's outcome
plus the next step.
P2-2 `task keepalive` while a daemon call is blocked: SIGTERM/SIGINT exit promptly and
quietly (exit 0, `ended: stopped`), not after the client timeout."""

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

import mesh.constants as c
from mesh.daemon.merge import merge_lease_key
from tests.helpers import MeshHarness
from tests.integration import test_merge as tm

FAIL_IF_MARKER = '["sh","-c","test ! -e fail.txt"]'


class TestMergeAllMidBatchFailure(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project
    show = tm.TestMerge.show
    start_task = tm.TestMerge.start_task
    commit_file = tm.TestMerge.commit_file
    submit_accept = tm.TestMerge.submit_accept
    accepted_task = tm.TestMerge.accepted_task
    integ_wt = tm.TestMerge.integ_wt
    q = tm.TestMerge.q

    def merge_all(self, prj, *extra):
        return self.harness.cli(*extra, "task", "merge", "--all-accepted", "--project", prj,
                                token=self.tok_lead1, timeout=300.0)

    def assert_clean_and_unlocked(self, prj):
        wt = self.integ_wt(prj)
        self.assertEqual(self.fixture.git(wt, "status", "--porcelain", "--untracked-files=all").stdout, "")
        self.assertFalse(self.fixture.has_merge_head(wt))
        self.assertEqual(self.q("SELECT run_id FROM leases WHERE key = ?;", (merge_lease_key(prj),)), [])

    def test_tests_failed_in_the_middle_stops_cleanly(self) -> None:
        _, prj = self.make_project("RT", test_command=FAIL_IF_MARKER)
        a, _ = self.accepted_task(prj, "A", "a.txt", "a\n")
        b, _ = self.accepted_task(prj, "B", "fail.txt", "boom\n")  # its merge fails the tests
        d, _ = self.accepted_task(prj, "D", "d.txt", "d\n")
        rc, resp = self.merge_all(prj)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        det = resp["error"]["details"]
        self.assertEqual(resp["error"]["code"], c.TESTS_FAILED)
        self.assertEqual([m["task_id"] for m in det["merged"]], [a])
        self.assertEqual((det["failed"]["task_id"], det["failed"]["code"]), (b, c.TESTS_FAILED))
        self.assertEqual(det["not_attempted"], [d])
        self.assertIn(f"mesh task show {b}", det["next_step"])
        self.assertIn(f"--all-accepted --project {prj}", det["next_step"])
        self.assertIn("Next:", resp["error"]["message"])
        self.assertNotIn("task retry", det["next_step"])  # PR #38 review: retry is invalid here
        self.assertIn(f"mesh task merge {b}", det["next_step"])
        self.assertIn(f"mesh task reject {b} --reason", det["next_step"])
        self.assertEqual([self.show(t)["status"] for t in (a, b, d)], ["merged", "merge_failed", "accepted"])
        # Nothing of B on the integration branch, tree clean, lock free.
        self.assertEqual(self.show(a)["merge"]["merge_commit"],
                         self.fixture.head(self.integ_wt(prj)))
        self.assert_clean_and_unlocked(prj)
        # The rest of the batch is not blocked: the next run merges D.
        rc, resp = self.merge_all(prj, "--format", "text")
        self.assertEqual(rc, 0, resp)
        self.assertIn(f"merged {d}", resp["raw_stderr"] if "raw_stderr" in resp else resp["raw_stdout"])
        self.assertEqual(self.show(d)["status"], "merged")
        self.assert_clean_and_unlocked(prj)
        # PR #38 review: the suggested commands actually work from merge_failed.
        rc, resp = self.harness.cli("task", "merge", b, token=self.tok_lead1, timeout=120.0)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)  # rerun happens (no INVALID_TRANSITION)
        self.assertEqual(resp["error"]["code"], c.TESTS_FAILED, resp)
        self.assert_clean_and_unlocked(prj)
        rc, resp = self.harness.cli("task", "reject", b, "--reason", "fail.txt breaks the tests",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertIn(self.show(b)["status"], ("rework", "queued"))

    def test_conflict_in_the_middle_stops_cleanly(self) -> None:
        a, _ = self.accepted_task(self.prj, "A", "same.txt", "a\n")
        b, _ = self.accepted_task(self.prj, "B", "same.txt", "b\n")  # conflicts with A
        d, _ = self.accepted_task(self.prj, "D", "d.txt", "d\n")
        rc, resp = self.merge_all(self.prj, "--format", "text")
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        err = resp.get("raw_stderr", "") + resp.get("raw_stdout", "")
        for line in (f"merged {a}", f"failed {b}: {c.MERGE_CONFLICT}", f"not attempted {d}",
                     f"mesh task close {b} --superseded-by"):
            self.assertIn(line, err)
        self.assertNotIn("task retry", err)  # PR #38 review: retry is invalid from merge_failed
        self.assertIn(f"mesh task reject {b} --reason", err)
        self.assert_clean_and_unlocked(self.prj)
        rc, resp = self.harness.cli("task", "merge", d, token=self.tok_lead1, timeout=120.0)
        self.assertEqual(rc, 0, resp)
        self.assert_clean_and_unlocked(self.prj)
        # The suggested rework path works: merge_failed -> rework.
        rc, resp = self.harness.cli("task", "reject", b, "--reason", "rebase on the integration branch",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertIn(self.show(b)["status"], ("rework", "queued"))

    def test_conflict_close_superseded_suggestion_works(self) -> None:
        a, _ = self.accepted_task(self.prj, "A", "same.txt", "a\n")
        b, _ = self.accepted_task(self.prj, "B", "same.txt", "b\n")
        rc, resp = self.merge_all(self.prj)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        self.assertIn(f"mesh task close {b} --superseded-by", resp["error"]["details"]["next_step"])
        rc, resp = self.harness.cli("task", "close", b, "--superseded-by", a, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.show(b)["status"], "cancelled")
        self.assert_clean_and_unlocked(self.prj)


class _HungDaemon:
    """A unix socket that accepts requests and never answers (a stuck meshd)."""

    def __init__(self, path: str) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        self.conns = []
        self.accepted = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            self.accepted.set()

    def close(self) -> None:
        for conn in self.conns:
            conn.close()
        self.sock.close()


class TestKeepaliveSignalDuringBlockedCall(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=False, start_daemon=False)
        self.addCleanup(self.harness.teardown)
        os.makedirs(os.path.join(self.harness.home, "run"), exist_ok=True)
        self.daemon = _HungDaemon(os.path.join(self.harness.home, "run", "mesh.sock"))
        self.addCleanup(self.daemon.close)
        self.tokdir = tempfile.mkdtemp(prefix="mh-tok-")
        self.addCleanup(shutil.rmtree, self.tokdir, True)
        os.chmod(self.tokdir, 0o700)
        self.tf = os.path.join(self.tokdir, "w.token")
        with open(self.tf, "w", encoding="utf-8") as f:
            f.write("mesh_at_fake_" + "x" * 32 + "\n")
        os.chmod(self.tf, 0o600)

    def run_and_signal(self, sig: int):
        cmd = [self.harness.bin_mesh, "--home", self.harness.home, "task", "keepalive", "tsk_x",
               "--run", "run_x", "--fence", "1", "--every", "30", "--token-file", self.tf]
        env = self.harness.get_env()
        env["MESH_SESSION"] = "ses_test"
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(self.daemon.accepted.wait(20), "keepalive never called the daemon")
            time.sleep(0.3)  # the heartbeat is now blocked reading the reply
            sent = time.monotonic()
            proc.send_signal(sig)
            out, err = proc.communicate(timeout=30)
            took = time.monotonic() - sent
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        return proc.returncode, out, err, took

    def check(self, sig: int) -> None:
        rc, out, err, took = self.run_and_signal(sig)
        self.assertEqual(rc, 0, out + err)
        self.assertLess(took, 3.0, f"exit took {took:.1f}s after the signal")
        self.assertNotIn("Traceback", err)
        data = json.loads(out)["data"]
        self.assertEqual((data["ended"], data["signal"], data["beats"]), ("stopped", sig, 0))

    def test_sigterm_during_blocked_heartbeat_exits_promptly(self) -> None:
        self.check(signal.SIGTERM)

    def test_sigint_during_blocked_heartbeat_exits_promptly(self) -> None:
        self.check(signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
