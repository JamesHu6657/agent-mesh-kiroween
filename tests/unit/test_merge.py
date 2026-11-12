"""Unit tests for mesh/daemon/merge.py (plan §10.5, §13.4; review m2-r4b-r5 findings 2, 3, 5, 6).

Git runs on K0 fixture repos under /tmp/mh-* through gitops.run_git; DB tests use a temp
mesh.db built by init_db (full schema). ACC: K4, K6, K8, A8.
"""

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import mesh.constants as c
from mesh.core.db import init_db
from mesh.daemon import merge
from mesh.errors import MeshError
from tests.gitfixture import GitFixture

NOW = "2026-11-09T00:00:00Z"
SECRET = "sk-" + "B" * 24

GERMAN_IDENTITY_STDERR = (
    "Autor-Identität unbekannt\n\n*** Bitte geben Sie an, wer Sie sind.\n\nFühren Sie\n\n"
    '  git config --global user.email "sie@beispiel.de"\n  git config --global user.name "Ihr Name"\n\n'
    "aus, um das als Ihre Standard-Identität zu setzen.\n"
    "fatal: kann E-Mail-Adresse nicht automatisch erkennen\n"
)


class ClassifyMergeFailureTest(unittest.TestCase):
    def test_unmerged_paths_are_a_conflict(self) -> None:
        """ACC: K6 - unmerged paths → conflict."""
        self.assertEqual(merge.classify_merge_failure(["a.txt"], "CONFLICT (content)"), "conflict")

    def test_identity_in_another_language(self) -> None:
        """ACC: K8 - a German identity message with no unmerged path → commit_failed."""
        self.assertEqual(merge.classify_merge_failure([], GERMAN_IDENTITY_STDERR), "commit_failed")
        self.assertEqual(merge.classify_merge_failure([], "Committer identity unknown"), "commit_failed")

    def test_no_unmerged_paths_is_never_a_conflict(self) -> None:
        """ACC: K6 - any other failure with no unmerged path → git_failed (rc 1), not conflict."""
        self.assertEqual(merge.classify_merge_failure([], "fatal: Fehler beim Lesen des Objekts"), "git_failed")
        self.assertEqual(merge.classify_merge_failure([], ""), "git_failed")


class PipelineDetailsTest(unittest.TestCase):
    """Finding 5: failure details are redacted, capped lists carry count and truncated."""

    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[key]
        os.environ.update(self.fixture.git_env_vars())
        self.repo = self.fixture.make_k0()

    def plan(self) -> dict:
        return {"task_id": "tsk_u", "title": "t", "integration": self.repo, "worktree": self.repo,
                "branch": "main", "pre_head": None}

    def test_dirty_worktree_secret_name_redacted(self) -> None:
        """ACC: K4 (merge clause) - an `sk-…` file name never reaches the details; count and flag present."""
        with open(os.path.join(self.repo, f"{SECRET}.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
        out = merge.run_merge_pipeline(self.plan())
        self.assertEqual(out["reason"], "worktree_dirty", out)
        det = out["details"]
        self.assertNotIn(SECRET, json.dumps(out))
        self.assertEqual(det["dirty_files_count"], 1)
        self.assertIs(det["dirty_files_truncated"], False)
        self.assertEqual(len(det["dirty_files"]), 1)

    def test_dirty_list_capped(self) -> None:
        """ACC: K4 (merge clause) - 250 dirty paths: 200 listed, count 250, truncated."""
        d = os.path.join(self.repo, "many")
        os.makedirs(d)
        for i in range(250):
            with open(os.path.join(d, f"f{i:03d}"), "w", encoding="utf-8") as f:
                f.write("x\n")
        det = merge.run_merge_pipeline(self.plan())["details"]
        self.assertEqual(len(det["dirty_files"]), merge.FILES_LIMIT)
        self.assertEqual(det["dirty_files_count"], 250)
        self.assertIs(det["dirty_files_truncated"], True)


class MergeDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-mu-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = init_db(os.path.join(self.tmp, "mesh.db"))
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1','user','u',?);", (NOW,))
        self.conn.execute(
            "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, test_command_json, "
            "created_by, created_via, created_at, updated_at) VALUES ('prj_1','P','g','active','/tmp/x','main',"
            "'mesh/prj_1/integration','\"none\"','usr_1','user',?,?);", (NOW, NOW))
        for tid, status in (("tsk_a", "merging"), ("tsk_b", "accepted")):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) VALUES (?, 'prj_1', 't', 's', 'code', ?, "
                "'default', 600, 1, '/tmp/ws', ?, 'usr_1', ?, ?);", (tid, status, f"mesh/prj_1/{tid}", NOW, NOW))
        self.conn.execute(
            "INSERT INTO runs (id, kind, task_id, project_id, fence, adapter, status, boot_id, run_dir, started_at) "
            "VALUES ('run_m', 'merge_test', 'tsk_a', 'prj_1', 0, 'mesh', 'running', 'boot_1', '/tmp/m', ?);", (NOW,))
        self.conn.execute(
            "INSERT INTO leases (key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
            "VALUES ('merge:prj_1', 'run_m', 'meshd', 'boot_1', 0, ?, ?, ?);", (NOW, NOW, NOW))
        self.conn.commit()

    def task(self, tid: str):
        return self.conn.execute("SELECT status, version FROM tasks WHERE id = ?;", (tid,)).fetchone()


class BusyLeaseTest(MergeDbCase):
    def test_merge_lease_held_is_rc11(self) -> None:
        """ACC: K6 (Q4 guard) - `merge:<prj>` held: rc 11 CONCURRENCY_LIMIT with lease, run_id, task_id; no change."""
        before = tuple(self.task("tsk_b"))
        ctx = SimpleNamespace(conn=self.conn, principal={"id": "usr_1"}, actor_kind="user", via="cli")
        with mock.patch.object(merge, "check_authz") as authz:
            with self.assertRaises(MeshError) as cm:
                merge.check_merge_request(ctx, "tsk_b")
        authz.assert_called_once()
        e = cm.exception
        self.assertEqual(e.exit_code, c.EXIT_BUSY)
        self.assertEqual(e.code, c.CONCURRENCY_LIMIT)
        self.assertEqual(e.details, {"lease": "merge:prj_1", "run_id": "run_m", "task_id": "tsk_a"})
        self.assertEqual(tuple(self.task("tsk_b")), before)


class FinalTxLeftMergingTest(MergeDbCase):
    def test_task_cancelled_mid_merge(self) -> None:
        """ACC: A8 - the task left `merging`: the run is closed, the lease released, the status kept; rc 1."""
        self.conn.execute("UPDATE tasks SET status = 'cancelled' WHERE id = 'tsk_a';")
        self.conn.commit()
        plan = {"task_id": "tsk_a", "project_id": "prj_1", "merge_id": "mrg_1", "run_id": "run_m",
                "pre_head": "a" * 40, "started_at": NOW}
        result = {"outcome": "merged", "noop": False, "pre_head": "a" * 40, "merge_commit": "b" * 40}
        self.conn.execute("BEGIN IMMEDIATE;")
        fin = merge._final_tx(self.conn, plan, result)
        self.conn.execute("COMMIT;")
        self.assertIs(fin["left_merging"], True)
        self.assertEqual(self.task("tsk_a")["status"], "cancelled")
        run = self.conn.execute("SELECT * FROM runs WHERE id = 'run_m';").fetchone()
        self.assertEqual(run["status"], "exited")
        self.assertEqual(run["end_reason"], "task_left_merging")
        self.assertEqual(json.loads(run["result_json"])["pipeline"]["merge_commit"], "b" * 40)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'merge:prj_1';").fetchone())
        resp = merge.merge_response(plan, result, fin)
        self.assertIsInstance(resp, MeshError)
        self.assertEqual(resp.exit_code, c.EXIT_INTERNAL)
        self.assertEqual(resp.details["reason"], "task_left_merging")
        self.assertEqual(resp.details["merge_commit"], "b" * 40)
        # rc 1 is never stored for replay (§13.4).
        merge.store_idempotency(self.conn, {"principal_id": "usr_1", "key": "k", "request_hash": "h"}, resp, "req")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM idempotency;").fetchone())


class ReviewR5bR6MinorsTest(MergeDbCase):
    """Review m2-r5b-r6 minors 1-3."""

    def timeout_result(self) -> dict:
        return {"outcome": "failed", "reason": "tests_failed", "pre_head": "a" * 40, "tests": "failed",
                "test_log": "/tmp/m/test.log",
                "test": {"timeout": True, "exit_code": None, "signal": None,
                         "termination": {"targets": 2, "kill_failed": True, "pids": [4242, 4243]}},
                "details": {"kill_failed": True, "timeout": True}}

    def plan(self) -> dict:
        return {"task_id": "tsk_a", "project_id": "prj_1", "merge_id": "mrg_1", "run_id": "run_m",
                "pre_head": "a" * 40, "started_at": NOW}

    def test_timeout_kill_failed_recorded_on_run(self) -> None:
        """ACC: K7 (timeout clause), §11.4 - timeout: run `killed`/`timeout`; kill_failed emits run.kill_failed with the pids."""
        self.conn.execute("BEGIN IMMEDIATE;")
        fin = merge._final_tx(self.conn, self.plan(), self.timeout_result())
        self.conn.execute("COMMIT;")
        self.assertEqual(fin["to_state"], "merge_failed")
        run = self.conn.execute("SELECT * FROM runs WHERE id = 'run_m';").fetchone()
        self.assertEqual((run["status"], run["end_reason"]), ("killed", "timeout"))
        ev = self.conn.execute("SELECT payload_json FROM events WHERE type = 'run.kill_failed';").fetchall()
        self.assertEqual(len(ev), 1)
        payload = json.loads(ev[0][0])
        self.assertEqual(payload["pids"], [4242, 4243])
        self.assertEqual(payload["end_reason"], "timeout")
        self.assertIs(payload["lease_released"], True)

    def test_leftovers_kill_failed_is_not_a_pass(self) -> None:
        """ACC: K7 - a passing command whose leftover survives termination (terminate.step patched to kill_failed) does not pass."""
        tmp = tempfile.mkdtemp(prefix="mh-mk-")
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.path.join(tmp, "integ")
        os.makedirs(cwd)
        run_id = f"run_mk{os.getpid()}x{NOW[-6:-1].replace(':', '')}"
        plan = {"run_id": run_id, "run_dir": os.path.join(tmp, "merges", "mrg_k"), "integration": cwd,
                "test_argv": ["sh", "-c", "setsid sleep 30 >/dev/null 2>&1 & exit 0"], "test_timeout_s": 30,
                "kill_grace_s": 1}

        def cleanup() -> None:
            t = merge.terminate.begin(run_id, None, None, "test_cleanup", 1)
            for _ in range(200):
                if merge.terminate.step(t) == "done":
                    break
                import time as _t
                _t.sleep(0.05)
            merge.terminate.forget(run_id)

        self.addCleanup(cleanup)
        with mock.patch.object(merge.terminate, "step", return_value="kill_failed"):
            out = merge.run_test_step(plan)
        self.assertEqual(out["exit_code"], 0, out)
        self.assertIs(out["passed"], False, out)
        self.assertEqual(out["error"], "leftovers_kill_failed")
        self.assertIs(out["termination"]["kill_failed"], True)
        self.assertTrue(out["termination"]["pids"], out)

    def test_duplicate_idempotency_key_keeps_the_mutation(self) -> None:
        """ACC: B5 (§13.4) - a key already taken inside the final TX: nothing rolls back, the store is skipped."""
        self.conn.execute(
            "INSERT INTO idempotency (principal_id, key, cmd, request_hash, response_json, exit_code, created_at) "
            "VALUES ('usr_1', 'k1', 'task.merge', 'other', '{}', 0, ?);", (NOW,))
        self.conn.commit()
        plan = self.plan()
        result = {"outcome": "merged", "noop": False, "pre_head": "a" * 40, "merge_commit": "b" * 40}
        plan.update({"branch": "mesh/prj_1/tsk_a", "integration_branch": "mesh/prj_1/integration",
                     "from_state": "accepted", "event": None})
        self.conn.execute("BEGIN IMMEDIATE;")
        fin = merge._final_tx(self.conn, plan, result)
        resp = merge.merge_response(plan, result, fin)
        stored = merge.store_idempotency(self.conn, {"principal_id": "usr_1", "key": "k1", "request_hash": "h"},
                                         resp, "req_x")
        self.conn.execute("COMMIT;")
        self.assertIs(stored, False)
        self.assertEqual(self.task("tsk_a")["status"], "merged")
        row = self.conn.execute("SELECT request_hash FROM idempotency WHERE key = 'k1';").fetchone()
        self.assertEqual(row[0], "other")

    def test_log_tail_byte_cap(self) -> None:
        """ACC: K7 (details) - one 300 KiB line: tail <= 16 KiB, lines <= 1 KiB, truncated flag set."""
        tmp = tempfile.mkdtemp(prefix="mh-lt-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "test.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("FAILED first\n" + "\u00e9" * (150 * 1024) + "\n")
            for i in range(40):
                f.write("x" * 900 + f" {i}\n")
        tail, cut = merge.log_tail_capped(path)
        self.assertIs(cut, True)
        self.assertLessEqual(sum(len(x.encode("utf-8")) + 1 for x in tail), merge.LOG_TAIL_MAX_BYTES)
        self.assertTrue(all(len(x.encode("utf-8")) <= merge.LOG_TAIL_LINE_MAX_BYTES for x in tail))
        self.assertTrue(tail[-1].endswith(" 39"), tail[-1][-10:])
        short = os.path.join(tmp, "short.log")
        with open(short, "w", encoding="utf-8") as f:
            f.write("a\nb\n")
        self.assertEqual(merge.log_tail_capped(short), (["a", "b"], False))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Review m2-r7 minors (r7b)
# ---------------------------------------------------------------------------

def _fake_termination(run_id: str, seen: dict) -> SimpleNamespace:
    return SimpleNamespace(run_id=run_id, pgid=None, wrapper_pid=None, wrapper_starttime=None,
                           initial_pids=sorted(seen), seen=dict(seen), kill_failed=False)


class RecoveryMergeRunsTest(MergeDbCase):
    """Review m2-r7 findings 1, 4 and 5: recovery of open merge_test runs."""

    def setUp(self) -> None:
        super().setUp()
        self.conn.execute("UPDATE tasks SET merge_json = ? WHERE id = 'tsk_a';",
                          (json.dumps({"mrg": "mrg_1", "run_id": "run_m", "pre_head": "a" * 40,
                                       "started_at": NOW}),))
        self.conn.commit()

    def task_row(self):
        return self.conn.execute("SELECT * FROM tasks WHERE id = 'tsk_a';").fetchone()

    def lease(self):
        return self.conn.execute("SELECT run_id FROM leases WHERE key = 'merge:prj_1';").fetchone()

    def test_kill_failed_in_recovery_is_reported(self) -> None:
        """ACC: K14, §11.4 - a merge_test termination that fails in recovery emits run.kill_failed (survivors, kind, lease_released, recovered) and the T16 details carry kill_failed."""
        fake = _fake_termination("run_m", {4242: 11, 4243: 12})

        def step(t):
            t.kill_failed = True
            return "kill_failed"

        with mock.patch.object(merge.terminate, "run_targets", return_value={4242: 11}), \
                mock.patch.object(merge.terminate, "begin", return_value=fake), \
                mock.patch.object(merge.terminate, "step", side_effect=step):
            status = merge.recover_merge_run(self.conn, self.conn.execute(
                "SELECT * FROM runs WHERE id = 'run_m';").fetchone())
        self.assertEqual(status, "killed")
        run = self.conn.execute("SELECT status, end_reason, result_json FROM runs WHERE id = 'run_m';").fetchone()
        self.assertEqual((run["status"], run["end_reason"]), ("killed", "interrupted"))
        term = json.loads(run["result_json"])["termination"]
        self.assertIs(term["kill_failed"], True)
        self.assertEqual(term["pids"], [4242])  # the survivor, not every pid seen (4243 is gone)
        ev = self.conn.execute("SELECT payload_json FROM events WHERE type = 'run.kill_failed';").fetchall()
        self.assertEqual(len(ev), 1)
        payload = json.loads(ev[0][0])
        self.assertEqual(payload["pids"], [4242])
        self.assertEqual(payload["kind"], "merge_test")
        self.assertIs(payload["lease_released"], True)
        self.assertIs(payload["recovered"], True)
        self.assertEqual(payload["end_reason"], "interrupted")
        self.assertIsNone(self.lease())

        # Step 5: the integration worktree does not exist under this home -> interrupted,
        # integration_missing, and kill_failed copied into the T16 details.
        outcome = merge.recover_merging_task(self.conn, self.tmp, self.task_row())
        self.assertEqual(outcome, "interrupted")
        mj = json.loads(self.task_row()["merge_json"])
        self.assertEqual(self.task_row()["status"], "merge_failed")
        self.assertEqual(mj["reason"], "interrupted")
        self.assertIs(mj["details"]["integration_missing"], True)
        self.assertIs(mj["details"]["kill_failed"], True)
        self.assertEqual(mj["details"]["kill_failed_pids"], [4242])
        failed = self.conn.execute("SELECT payload_json FROM events WHERE type = 'merge.failed';").fetchall()
        self.assertEqual(len(failed), 1)
        self.assertIs(json.loads(failed[0][0])["details"]["kill_failed"], True)

    def test_open_run_after_step2_error_keeps_task_and_lease(self) -> None:
        """ACC: K14 - if closing the merge_test run raises, step 5 leaves the task merging with its lease; both counted as errors."""
        from mesh.daemon import recovery

        counts: dict = {}
        with mock.patch.object(merge, "_live_wrapper", side_effect=RuntimeError("boom")):
            recovery._step_merge_runs(self.conn, self.tmp, counts)
        self.assertEqual(counts.get("errors"), 1, counts)
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = 'run_m';").fetchone()[0], "running")
        recovery._step_merging(self.conn, self.tmp, counts)
        self.assertEqual(counts.get("merge_errors"), 1, counts)
        self.assertEqual(self.task_row()["status"], "merging")
        self.assertEqual(self.lease()["run_id"], "run_m")
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = 'run_m';").fetchone()[0], "running")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM events WHERE type IN ('merge.failed', 'task.merged');").fetchone())
        # The next boot (no error) closes the run, then step 5 decides the task.
        counts = {}
        with mock.patch.object(merge.terminate, "run_targets", return_value={}):
            recovery._step_merge_runs(self.conn, self.tmp, counts)
        self.assertEqual(counts.get("merge_run_lost"), 1, counts)
        recovery._step_merging(self.conn, self.tmp, counts)
        self.assertEqual(counts.get("merge_interrupted"), 1, counts)
        self.assertEqual(self.task_row()["status"], "merge_failed")
        self.assertIsNone(self.lease())

    def test_terminations_are_stepped_together(self) -> None:
        """ACC: K14, §11.7 step 2 - two open merge_test runs are terminated concurrently: boot waits about one termination, not two."""
        self.conn.execute(
            "INSERT INTO runs (id, kind, task_id, project_id, fence, adapter, status, boot_id, run_dir, started_at) "
            "VALUES ('run_n', 'merge_test', 'tsk_b', 'prj_1', 0, 'mesh', 'running', 'boot_1', '/tmp/n', ?);", (NOW,))
        self.conn.commit()
        import time as _t
        hold_s = 0.6
        started: dict = {}
        order: list = []

        def begin(run_id, *a, **k):
            started[run_id] = _t.monotonic()
            return _fake_termination(run_id, {5000 + len(started): 1})

        def step(t):
            order.append(t.run_id)
            return "done" if _t.monotonic() - started[t.run_id] >= hold_s else "pending"

        runs = self.conn.execute("SELECT * FROM runs WHERE kind = 'merge_test' ORDER BY id;").fetchall()
        t0 = _t.monotonic()
        with mock.patch.object(merge.terminate, "run_targets", return_value={1: 1}), \
                mock.patch.object(merge.terminate, "begin", side_effect=begin), \
                mock.patch.object(merge.terminate, "step", side_effect=step):
            out = merge.recover_merge_runs(self.conn, list(runs))
        elapsed = _t.monotonic() - t0
        self.assertEqual(out, {"run_m": "killed", "run_n": "killed"})
        self.assertEqual(set(order[:2]), {"run_m", "run_n"})  # both begun before either finished
        self.assertLess(elapsed, 2 * hold_s, elapsed)
        self.assertEqual([r[0] for r in self.conn.execute(
            "SELECT status FROM runs WHERE kind = 'merge_test' ORDER BY id;")], ["killed", "killed"])


class TaskBranchCase(unittest.TestCase):
    """A gitfixture K0 repo on main (the integration worktree) with a task branch one commit ahead."""

    TASK = "tsk_rcv1"
    BRANCH = "mesh/prj_1/tsk_rcv1"

    def setUp(self) -> None:
        PipelineDetailsTest.setUp(self)  # hermetic git env and the K0 repo
        self.base = self.fixture.head(self.repo)
        self.fixture.git(self.repo, "checkout", "-q", "-b", self.BRANCH)
        with open(os.path.join(self.repo, "feature.txt"), "w", encoding="utf-8") as f:
            f.write("feature\n")
        self.fixture.git(self.repo, "add", "feature.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "feature")
        self.tip = self.fixture.head(self.repo)
        self.fixture.git(self.repo, "checkout", "-q", "main")

    def merge_commit(self, message: str) -> str:
        self.fixture.git(self.repo, "merge", "-q", "--no-ff", "-m", message, self.BRANCH)
        return self.fixture.head(self.repo)


class DecideRecoveryTest(TaskBranchCase):
    """Review m2-r7 findings 2 and 3: merge.decide_recovery (§11.7 step 5)."""

    def decide(self, pre_head):
        return merge.decide_recovery(self.repo, self.TASK, self.BRANCH, pre_head)

    def assert_interrupted(self, out) -> dict:
        self.assertEqual(out["outcome"], "failed", out)
        self.assertEqual(out["reason"], "interrupted", out)
        return out["details"]

    def test_reconciles_a_matching_merge_commit_with_its_first_parent(self) -> None:
        """ACC: K14 - positive control; a stale TX1 pre_head is replaced by the merge commit's first parent."""
        head = self.merge_commit(f"Merge mesh task {self.TASK}: t")
        out = self.decide("f" * 40)
        self.assertEqual(out["outcome"], "merged", out)
        self.assertEqual(out["merge_commit"], head)
        self.assertEqual(out["pre_head"], self.base)
        self.assertEqual(out["recorded_pre_head"], "f" * 40)

    def test_second_parent_not_the_task_tip(self) -> None:
        """ACC: K14 - a merge commit naming the task whose second parent is not the branch tip (branch moved) is not reconciled."""
        head = self.merge_commit(f"Merge mesh task {self.TASK}: t")
        self.fixture.git(self.repo, "checkout", "-q", self.BRANCH)
        with open(os.path.join(self.repo, "more.txt"), "w", encoding="utf-8") as f:
            f.write("more\n")
        self.fixture.git(self.repo, "add", "more.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "more")
        self.fixture.git(self.repo, "checkout", "-q", "main")
        det = self.assert_interrupted(self.decide(self.base))
        self.assertEqual(det["head"], head)
        self.assertIs(det["head_moved"], True)
        self.assertIs(det["merge_commit_match"], False)
        self.assertIs(det["restored"], False)

    def test_message_without_the_task_id(self) -> None:
        """ACC: K14 - a merge of the task tip whose message lacks the task id is not reconciled."""
        head = self.merge_commit("Merge something else")
        det = self.assert_interrupted(self.decide(self.base))
        self.assertEqual(det["head"], head)
        self.assertIs(det["merge_commit_match"], False)
        self.assertIs(det["restored"], False)

    def test_non_merge_commit_naming_the_task(self) -> None:
        """ACC: K14 - a non-merge commit whose message contains the task id is not reconciled."""
        with open(os.path.join(self.repo, "x.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
        self.fixture.git(self.repo, "add", "x.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", f"Merge mesh task {self.TASK}: t")
        det = self.assert_interrupted(self.decide(self.base))
        self.assertIs(det["merge_commit_match"], False)
        self.assertIs(det["restored"], False)

    def test_head_unchanged_without_merge_head(self) -> None:
        """ACC: K14 - HEAD == pre_head and no MERGE_HEAD (crash before `merge --no-ff`) -> interrupted, restored."""
        det = self.assert_interrupted(self.decide(self.base))
        self.assertIs(det["restored"], True)
        self.assertNotIn("head_moved", det)
        self.assertEqual(self.fixture.head(self.repo), self.base)

    def test_merge_head_with_a_stale_pre_head(self) -> None:
        """ACC: K14 - MERGE_HEAD: verify the abort against HEAD read before it, not the stale TX1 probe."""
        self.fixture.git(self.repo, "merge", "-q", "--no-ff", "--no-commit", self.BRANCH)
        self.assertTrue(self.fixture.has_merge_head(self.repo))
        out = self.decide("f" * 40)
        det = self.assert_interrupted(out)
        self.assertEqual(out["pre_head"], self.base)
        self.assertIs(det["merge_head"], True)
        self.assertIs(det["restored"], True, det)
        self.assertEqual(det["recorded_pre_head"], "f" * 40)
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        self.assertTrue(self.fixture.is_clean(self.repo))
        self.assertEqual(self.fixture.head(self.repo), self.base)


class PipelineTagShadowTest(TaskBranchCase):
    """Review m2-r7 finding 6: the live pipeline merges refs/heads/<branch> by sha."""

    def test_k10_tag_named_like_the_task_branch(self) -> None:
        """ACC: K10, K5 - a tag named like the task branch (at main) does not turn the merge into a no-op; the branch tip is merged."""
        self.fixture.git(self.repo, "tag", self.BRANCH, self.base)
        plan = {"task_id": self.TASK, "title": "t", "integration": self.repo, "worktree": self.repo,
                "branch": self.BRANCH, "pre_head": None}
        out = merge.run_merge_pipeline(plan)
        self.assertEqual(out["outcome"], "merged", out)
        self.assertIs(out["noop"], False)
        self.assertEqual(out["pre_head"], self.base)
        parents = self.fixture.git(self.repo, "log", "-1", "--format=%P", "HEAD").stdout.split()
        self.assertEqual(parents, [self.base, self.tip])
        self.assertEqual(self.fixture.git(self.repo, "rev-parse", f"refs/tags/{self.BRANCH}").stdout.strip(),
                         self.base)
