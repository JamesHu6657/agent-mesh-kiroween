"""r7b recovery minors, using real git refs and /tmp/mh-* databases. ACC: K14."""

import json
import os
from unittest import mock

from mesh.core.db import connect
from mesh.core.events import verify_event_chain
from mesh.daemon import merge, recovery
from tests.unit import test_merge as tm


class CurrentRefsRecoveryTest(tm.TaskBranchCase):
    def test_no_merge_head_uses_current_refs(self) -> None:
        """ACC: K14 - a stale, valid probe is not evidence that recovery failed."""
        with open(os.path.join(self.repo, "other.txt"), "w", encoding="utf-8") as f:
            f.write("another integration change\n")
        self.fixture.git(self.repo, "add", "other.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "another task")
        current = self.fixture.head(self.repo)
        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                if dirty:
                    with open(os.path.join(self.repo, "untracked.txt"), "w", encoding="utf-8") as f:
                        f.write("left behind\n")
                out = merge.decide_recovery(self.repo, self.TASK, self.BRANCH, self.base)
                self.assertEqual((out["outcome"], out["reason"]), ("failed", "interrupted"))
                self.assertEqual(out["pre_head"], current)
                self.assertEqual(out["details"]["recorded_pre_head"], self.base)
                self.assertIs(out["details"]["restored"], not dirty)
                self.assertNotIn("head_moved", out["details"])
                self.assertFalse(self.fixture.has_merge_head(self.repo))
                self.assertEqual(self.fixture.head(self.repo), current)
                if dirty:
                    self.assertEqual(out["details"]["dirty_files"], ["untracked.txt"])
        with mock.patch.object(merge, "_dirty_paths", side_effect=merge._GitFailed("verify status")):
            out = merge.decide_recovery(self.repo, self.TASK, self.BRANCH, self.base)
        self.assertEqual(out["pre_head"], current)
        self.assertEqual(out["details"]["recorded_pre_head"], self.base)
        self.assertIsNone(out["details"]["restored"])
        self.assertEqual(out["details"]["git_error"]["step"], "verify status")

    def test_retry_after_abort_uses_current_refs(self) -> None:
        """ACC: K14 - a second crash after abort cannot reintroduce the stale probe."""
        self.fixture.git(self.repo, "merge", "-q", "--no-ff", "--no-commit", self.BRANCH)
        first = merge.decide_recovery(self.repo, self.TASK, self.BRANCH, self.tip)
        self.assertIs(first["details"]["restored"], True)
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        again = merge.decide_recovery(self.repo, self.TASK, self.BRANCH, self.tip)
        self.assertEqual(again["pre_head"], self.base)
        self.assertEqual(again["details"]["recorded_pre_head"], self.tip)
        self.assertIs(again["details"]["restored"], True)
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_matching_commit_is_inspected_even_when_probe_equals_head(self) -> None:
        """ACC: K14 - current parents and task tip, not probe equality, decide reconciliation."""
        head = self.merge_commit(f"Merge mesh task {self.TASK}: t")
        out = merge.decide_recovery(self.repo, self.TASK, self.BRANCH, head)
        self.assertEqual(out["outcome"], "merged")
        self.assertEqual(out["merge_commit"], head)
        self.assertEqual(out["pre_head"], self.base)
        self.assertEqual(out["recorded_pre_head"], head)


class BlockedRecoveryTest(tm.MergeDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn.execute("UPDATE tasks SET merge_json = ? WHERE id = 'tsk_a';",
                          (json.dumps({"mrg": "mrg_1", "run_id": "run_m", "pre_head": "a" * 40,
                                       "started_at": tm.NOW}),))

    def events(self):
        return self.conn.execute(
            "SELECT * FROM events WHERE type = 'merge.recovery_blocked' ORDER BY seq;"
        ).fetchall()

    def test_no_merge_head_persists_current_refs(self) -> None:
        """ACC: K14 - step 2 and step 5 persist the current baseline and stale probe separately."""
        tm.PipelineDetailsTest.setUp(self)
        current = self.fixture.head(self.repo)
        counts = {}
        with mock.patch.object(merge.terminate, "run_targets", return_value={}):
            recovery._step_merge_runs(self.conn, self.tmp, counts)
        with mock.patch.object(merge, "integration_worktree_path", return_value=self.repo):
            recovery._step_merging(self.conn, self.tmp, counts)
        self.assertEqual((counts["merge_run_lost"], counts["merge_interrupted"]), (1, 1))
        row = self.conn.execute("SELECT status, merge_json FROM tasks WHERE id = 'tsk_a';").fetchone()
        self.assertEqual(row["status"], "merge_failed")
        mj = json.loads(row["merge_json"])
        self.assertEqual(mj["pre_head"], current)
        self.assertEqual(mj["details"]["recorded_pre_head"], "a" * 40)
        self.assertIs(mj["details"]["restored"], True)
        self.assertIs(mj["recovered"], True)
        event = self.conn.execute("SELECT payload_json FROM events WHERE type = 'merge.failed';").fetchone()
        payload = json.loads(event[0])
        self.assertEqual(payload["pre_head"], current)
        self.assertEqual(payload["details"], mj["details"])
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        self.assertTrue(self.fixture.is_clean(self.repo))
        before = self.conn.execute("SELECT COUNT(*) FROM events;").fetchone()[0]
        recovery._step_merge_runs(self.conn, self.tmp, {})
        recovery._step_merging(self.conn, self.tmp, {})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events;").fetchone()[0], before)

    def test_blocked_event_deduplicates_across_restarts_and_errors(self) -> None:
        """ACC: K14 - one durable event per task/error, with no transition or lease release."""
        before = tuple(self.task("tsk_a"))
        for message, expected in (("boom " + tm.SECRET, 1), ("boom " + tm.SECRET, 1),
                                  ("different", 2), ("boom " + tm.SECRET, 2)):
            with self.subTest(message=message[:9]):
                counts = {}
                with mock.patch.object(merge, "_live_wrapper", side_effect=RuntimeError(message)):
                    recovery._step_merge_runs(self.conn, self.tmp, counts)
                recovery._step_merging(self.conn, self.tmp, counts)
                self.assertEqual((counts["errors"], counts["merge_errors"]), (1, 1))
                self.assertEqual(len(self.events()), expected)
                self.assertEqual(tuple(self.task("tsk_a")), before)
                self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = 'run_m';").fetchone()[0],
                                 "running")
                self.assertEqual(self.conn.execute("SELECT run_id FROM leases;").fetchone()[0], "run_m")
                self.conn.close()
                self.conn = connect(os.path.join(self.tmp, "mesh.db"))
                self.addCleanup(self.conn.close)
        for event in self.events():
            self.assertEqual((event["task_id"], event["run_id"]), ("tsk_a", "run_m"))
            payload = json.loads(event["payload_json"])
            self.assertEqual(payload, {"task_id": "tsk_a", "run_id": "run_m", "reason": event["reason"]})
            self.assertNotIn(tm.SECRET, event["reason"])
            self.assertTrue(event["reason"].startswith("RuntimeError: "))
        self.assertEqual(verify_event_chain(self.conn), [])
        with open(os.path.join(self.tmp, "logs", "meshd.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("run_m", log)
        self.assertIn("tsk_a", log)
        self.assertIn("RuntimeError: boom [REDACTED]", log)
        self.assertNotIn(tm.SECRET, log)
        with mock.patch.object(merge.terminate, "run_targets", return_value={}):
            recovery._step_merge_runs(self.conn, self.tmp, {})
        recovery._step_merging(self.conn, self.tmp, {})
        self.assertEqual(self.task("tsk_a")["status"], "merge_failed")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases;").fetchone())
        self.assertEqual(len(self.events()), 2)

    def test_one_run_error_is_logged_without_stopping_other_runs(self) -> None:
        """ACC: K14 - preparation and final-TX errors leave only that run open."""
        self.conn.execute(
            "INSERT INTO runs (id, kind, task_id, project_id, fence, adapter, status, boot_id, run_dir, started_at) "
            "VALUES ('run_n', 'merge_test', 'tsk_b', 'prj_1', 0, 'mesh', 'running', 'boot_1', ?, ?);",
            (os.path.join(self.tmp, "merges", "mrg_n"), tm.NOW))
        runs = self.conn.execute("SELECT * FROM runs ORDER BY id;").fetchall()
        close = merge._close_recovered_merge_run
        for stage in ("prepare", "close"):
            with self.subTest(stage=stage):
                self.conn.execute("UPDATE runs SET status = 'running', ended_at = NULL;")
                error = RuntimeError(stage + " failed")

                def prepare(run):
                    if stage == "prepare" and run["id"] == "run_m":
                        raise error
                    return None, None

                def finish(conn, run, *args):
                    if stage == "close" and run["id"] == "run_m":
                        raise error
                    return close(conn, run, *args)

                with mock.patch.object(merge, "_live_wrapper", side_effect=prepare), \
                        mock.patch.object(merge.terminate, "run_targets", return_value={}), \
                        mock.patch.object(merge, "_close_recovered_merge_run", side_effect=finish):
                    out = merge.recover_merge_runs(self.conn, list(runs))
                self.assertIs(out["run_m"], error)
                self.assertEqual(out["run_n"], "lost")
                self.assertEqual([r[0] for r in self.conn.execute("SELECT status FROM runs ORDER BY id;")],
                                 ["running", "lost"])
                self.assertEqual(self.conn.execute("SELECT run_id FROM leases;").fetchone()[0], "run_m")
        with open(os.path.join(self.tmp, "logs", "meshd.log"), encoding="utf-8") as f:
            log = f.read()
        for stage in ("prepare", "close"):
            self.assertIn(f"merge run run_m task tsk_a failed: RuntimeError: {stage} failed", log)
        self.assertEqual(len(self.events()), 2)

    def test_diagnostic_failure_does_not_stop_other_runs(self) -> None:
        """ACC: K14 - an unavailable event log cannot turn one run error into a batch failure."""
        self.conn.execute(
            "INSERT INTO runs (id, kind, task_id, project_id, fence, adapter, status, boot_id, run_dir, started_at) "
            "VALUES ('run_n', 'merge_test', 'tsk_b', 'prj_1', 0, 'mesh', 'running', 'boot_1', ?, ?);",
            (os.path.join(self.tmp, "merges", "mrg_n"), tm.NOW))
        runs = self.conn.execute("SELECT * FROM runs ORDER BY id;").fetchall()
        error = RuntimeError("prepare failed")
        with mock.patch.object(merge, "_live_wrapper", side_effect=[error, (None, None)]), \
                mock.patch.object(merge.terminate, "run_targets", return_value={}), \
                mock.patch.object(merge, "record_event", side_effect=RuntimeError("event unavailable")):
            out = merge.recover_merge_runs(self.conn, list(runs))
        self.assertIs(out["run_m"], error)
        self.assertEqual(out["run_n"], "lost")
        self.assertFalse(self.conn.in_transaction)
        with open(os.path.join(self.tmp, "logs", "meshd.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("run_m task tsk_a failed: RuntimeError: prepare failed", log)
        self.assertIn("recording merge.recovery_blocked failed: RuntimeError: event unavailable", log)
