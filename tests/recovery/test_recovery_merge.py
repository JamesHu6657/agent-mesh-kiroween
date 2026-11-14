"""Merge crash recovery (plan §11.7 step 2 for merge_test runs and step 5; R8; m2-r7).

meshd is killed with SIGKILL during a merge (``kill9_daemon``, or the testing-only
``MESH_TEST_CRASH_AT=after_merge_commit`` hook in merge.py) and restarted with
``restart_daemon``; recovery runs before the socket opens. The helpers are the ones of
tests/integration/test_merge.py (borrowed, not inherited, so its tests do not run twice).

ACC: K14 (R8), plus the P5 reading "project cancel blocks while a task is merging".
"""

import json
import os
import threading
import time
import unittest
from typing import Any, Dict, List

import mesh.constants as c
from mesh.daemon import terminate
from tests.integration import test_merge as tm

SLOW_TESTS = '["python3","-c","import time; time.sleep(20)"]'


class TestRecoveryMerge(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project
    integ_branch = tm.TestMerge.integ_branch
    integ_wt = tm.TestMerge.integ_wt
    show = tm.TestMerge.show
    start_task = tm.TestMerge.start_task
    commit_file = tm.TestMerge.commit_file
    submit_accept = tm.TestMerge.submit_accept
    accepted_task = tm.TestMerge.accepted_task
    merge = tm.TestMerge.merge
    q = tm.TestMerge.q
    lead1_merge_failed = tm.TestMerge.lead1_merge_failed
    merge_runs = tm.TestMerge.merge_runs
    assert_no_merge_lease = tm.TestMerge.assert_no_merge_lease
    assert_integration_restored = tm.TestMerge.assert_integration_restored
    assert_remote_unchanged = tm.TestMerge.assert_remote_unchanged
    doctor_ok = tm.TestMerge.doctor_ok

    # --- helpers -----------------------------------------------------------------

    def slow_project(self, name: str) -> tuple:
        repo, prj = self.make_project(name, test_command=SLOW_TESTS)
        self.repo = repo
        self.remote_before = self.fixture.remote_refs(repo)
        return repo, prj

    def merge_in_background(self, tsk: str) -> Dict[str, Any]:
        box: Dict[str, Any] = {}

        def go() -> None:
            try:
                box["rc"], box["resp"] = self.merge(tsk)
            except Exception as exc:  # the daemon dies under the client
                box["exc"] = exc

        th = threading.Thread(target=go, daemon=True)
        th.start()
        box["thread"] = th
        return box

    def wait_test_step(self, tsk: str) -> Any:
        """Wait until the merge run exists and a process carries its MESH_RUN_ID."""
        found: Dict[str, Any] = {}

        def ready() -> bool:
            runs = self.merge_runs(tsk)
            if not runs:
                return False
            found["run"] = runs[0]
            return bool(terminate.run_targets(runs[0]["id"], None, None))

        self.assertTrue(self.harness.wait_for(ready, timeout=30.0), "test step never started")
        return found["run"]

    def merge_events(self, tsk: str, type_: str) -> List[Any]:
        return self.q("SELECT * FROM events WHERE task_id = ? AND type = ? ORDER BY seq;", (tsk, type_))

    def merge_json(self, tsk: str) -> Dict[str, Any]:
        d = self.show(tsk)
        return d["merge_json"] if "merge_json" in d else d["merge"]

    def recovered_events(self) -> List[Dict[str, Any]]:
        return [json.loads(r["payload_json"]) for r in
                self.q("SELECT payload_json FROM events WHERE type = 'daemon.recovered' ORDER BY seq;")]

    # --- (a) kill -9 during the test step --------------------------------------------

    def test_k14_kill9_during_test_step_interrupted(self) -> None:
        """ACC: K14 - kill -9 meshd during the test step; restart: merge_failed(interrupted), no MERGE_HEAD, clean, HEAD == pre_head, test processes gone; P5 cancel blocked while merging; a second recovery adds nothing."""
        repo, prj = self.slow_project("RA")
        tsk, _ = self.accepted_task(prj, "Slow merge", "slow.txt", "slow\n")
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        box = self.merge_in_background(tsk)
        run = self.wait_test_step(tsk)
        wt = self.integ_wt(prj)
        self.assertTrue(self.fixture.has_merge_head(wt))  # the merge is half-done right now

        # P5 reading (m2-r7): project cancel blocks while a task is merging, and changes nothing.
        before = self.q("SELECT status, version FROM tasks WHERE id = ?;", (tsk,))[0]
        rc, resp = self.harness.cli("project", "cancel", prj, user=True)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        det = resp["error"]["details"]
        self.assertEqual(det["reason"], "merge_in_progress", det)
        self.assertEqual(det["task_id"], tsk)
        self.assertEqual(det["merge_run_id"], run["id"])
        self.assertEqual(self.q("SELECT status FROM projects WHERE id = ?;", (prj,))[0]["status"], "active")
        after = self.q("SELECT status, version FROM tasks WHERE id = ?;", (tsk,))[0]
        self.assertEqual((after["status"], after["version"]), (before["status"], before["version"]))
        self.assertEqual(self.q("SELECT id FROM approvals WHERE project_id = ?;", (prj,)), [])

        self.harness.kill9_daemon()
        box["thread"].join(timeout=30.0)
        self.assertNotEqual(box.get("rc"), 0, box)
        self.assertTrue(terminate.run_targets(run["id"], None, None))  # the test survives meshd
        self.harness.restart_daemon()

        d = self.show(tsk)
        self.assertEqual(d["status"], "merge_failed", d)
        mj = self.merge_json(tsk)
        self.assertEqual(mj["reason"], "interrupted", mj)
        self.assertIs(mj["recovered"], True)
        self.assertIs(mj["reconciled"], False)
        self.assertIs(mj["details"]["merge_head"], True, mj)
        self.assertIs(mj["details"]["restored"], True, mj)
        self.assert_integration_restored(repo, prj, pre_head)
        self.assertEqual(terminate.run_targets(run["id"], None, None), {})
        runs = self.merge_runs(tsk)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "killed", dict(runs[0]))
        self.assertEqual(runs[0]["end_reason"], "interrupted")
        self.assert_no_merge_lease(prj)
        self.assertEqual(len(self.merge_events(tsk, "merge.failed")), 1)
        msgs = self.lead1_merge_failed(tsk)
        self.assertEqual([m.get("reason") for m in msgs], ["interrupted"], msgs)
        rec = self.recovered_events()[-1]
        self.assertEqual(rec["merge_runs_killed"], 1, rec)
        self.assertEqual(rec["merges_interrupted"], 1, rec)

        # (c) a second recovery: no second transition, event or message.
        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        self.assertEqual(self.show(tsk)["status"], "merge_failed")
        self.assertEqual(len(self.merge_events(tsk, "merge.failed")), 1)
        self.assertEqual(len(self.lead1_merge_failed(tsk)), 1)
        rec2 = self.recovered_events()[-1]
        self.assertEqual((rec2["merges_interrupted"], rec2["merges_reconciled"], rec2["merge_runs_killed"]),
                         (0, 0, 0), rec2)
        self.assert_integration_restored(repo, prj, pre_head)

        self.doctor_ok()
        self.assert_remote_unchanged()

    # --- (b) crash right after the merge commit ---------------------------------------

    def test_k14_crash_after_merge_commit_reconciled(self) -> None:
        """ACC: K14, I9 - meshd killed after the merge commit and before T15; restart reconciles to merged (reconciled:true, tests passed); doctor clean; a second recovery adds nothing."""
        repo, prj = self.make_project("RB", test_command='["python3","-c","pass"]')
        self.repo = repo
        self.remote_before = self.fixture.remote_refs(repo)
        tsk, _ = self.accepted_task(prj, "Commit then crash", "crash.txt", "crash\n")
        branch = f"mesh/{prj}/{tsk}"
        tip = self.fixture.head(repo, branch)
        pre_head = self.fixture.head(repo, self.integ_branch(prj))

        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_merge_commit"})
        rc, resp = self.merge(tsk)
        self.assertNotEqual(rc, 0, resp)
        self.harness.wait_daemon_exit(timeout=30.0)
        self.assertEqual(self.q("SELECT status FROM tasks WHERE id = ?;", (tsk,))[0]["status"], "merging")
        head = self.fixture.head(repo, self.integ_branch(prj))
        self.assertNotEqual(head, pre_head)  # the commit exists, T15 never happened

        self.harness.restart_daemon()
        d = self.show(tsk)
        self.assertEqual(d["status"], "merged", d)
        mj = self.merge_json(tsk)
        self.assertEqual(mj["merge_commit"], head, mj)
        self.assertIs(mj["reconciled"], True, mj)
        self.assertIs(mj["recovered"], True)
        self.assertEqual(mj["tests"], "passed")
        self.assertEqual(mj["pre_head"], pre_head)
        parents = self.fixture.git(repo, "rev-list", "--parents", "-n", "1", head).stdout.split()
        self.assertEqual(parents[1:], [pre_head, tip])
        wt = self.integ_wt(prj)
        self.assertTrue(self.fixture.is_clean(wt))
        self.assertFalse(self.fixture.has_merge_head(wt))
        merged = self.merge_events(tsk, "task.merged")
        self.assertEqual(len(merged), 1)
        self.assertIs(json.loads(merged[0]["payload_json"])["reconciled"], True)
        self.assertEqual(self.merge_events(tsk, "merge.failed"), [])
        self.assert_no_merge_lease(prj)
        run = self.merge_runs(tsk)[0]
        self.assertIn(run["status"], ("lost", "killed"), dict(run))
        self.assertEqual(run["end_reason"], "interrupted")
        self.assertEqual(self.recovered_events()[-1]["merges_reconciled"], 1)
        self.doctor_ok()

        self.harness.kill9_daemon()
        self.harness.restart_daemon()
        self.assertEqual(len(self.merge_events(tsk, "task.merged")), 1)
        self.assertEqual(self.recovered_events()[-1]["merges_reconciled"], 0)
        self.doctor_ok()
        self.assert_remote_unchanged()


if __name__ == "__main__":
    unittest.main()
