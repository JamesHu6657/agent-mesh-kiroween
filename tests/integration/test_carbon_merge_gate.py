"""Regression tests for the Carbon merge-gate review (agent-mesh-kiroween, P1-2/P1-3/P2-5..P2-8).

Real meshd + real K0 git fixtures (see tests/integration/test_merge.py). Merges that must be
observed "in flight" use a project test command that blocks on a gate file, so the merge
lease and the `merging` state are held while the test drives the other commands.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from tests.integration import test_merge as tm
from tests.integration.test_lead_actions import LeadActionCase, _fake_goal
from tests.integration.test_lead_turns import create_agent
from tests.recovery.test_recovery_merge import TestRecoveryMerge

GATE_SCRIPT = ("import os, sys, time\n"
               "g = sys.argv[1]\n"
               "while not os.path.exists(g):\n"
               "    time.sleep(0.05)\n")


class MergeGateCase(unittest.TestCase):
    """Helpers borrowed (not inherited) from TestMerge so its tests do not run twice."""

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
    merge_runs = tm.TestMerge.merge_runs
    assert_no_merge_lease = tm.TestMerge.assert_no_merge_lease
    assert_integration_restored = tm.TestMerge.assert_integration_restored
    assert_remote_unchanged = tm.TestMerge.assert_remote_unchanged
    doctor_ok = tm.TestMerge.doctor_ok

    def gate_command(self, gate: str) -> str:
        return json.dumps(["python3", "-c", GATE_SCRIPT, gate])

    def gate_path(self, name: str = "gate") -> str:
        d = tempfile.mkdtemp(prefix="mh-gate-")
        self.addCleanup(shutil.rmtree, d, True)
        return os.path.join(d, name)

    def open_gate(self, gate: str) -> None:
        with open(gate, "w"):
            pass

    def background_merge(self, tsk: str) -> "Tuple[threading.Thread, Dict[str, Any]]":
        box: Dict[str, Any] = {}

        def run() -> None:
            box["rc"], box["resp"] = self.merge(tsk)

        th = threading.Thread(target=run, daemon=True)
        th.start()
        return th, box

    def wait_merge_lease(self, prj: str) -> None:
        self.assertTrue(self.harness.wait_for(
            lambda: bool(self.q("SELECT 1 FROM leases WHERE key = ?;", (f"merge:{prj}",))), timeout=30),
            "merge lease never appeared")

    def finish_merge(self, th: threading.Thread, box: Dict[str, Any], gate: str) -> None:
        self.open_gate(gate)
        th.join(timeout=90)
        self.assertFalse(th.is_alive(), "background merge did not finish")
        self.assertEqual(box.get("rc"), 0, box.get("resp"))

    def gated_project(self, name: str, gate: str) -> Tuple[str, str]:
        repo, prj = self.make_project(name, test_command=self.gate_command(gate))
        self.repo = repo
        self.remote_before = self.fixture.remote_refs(repo)
        return repo, prj

    def second_project_on_repo(self, repo: str) -> str:
        rc, resp = self.harness.cli("intake", "submit", "--title", "Second", "--goal", "g", "--criterion", "c1",
                                    "--repo", repo, "--base-branch", "main", "--test-command", "none",
                                    "--lead", "lead1", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]


class TestP27DeliverPruneMergeLease(MergeGateCase):
    """P2-7: `project deliver` and `workspace prune` respect merge:<prj> (same repo realpath)."""

    def test_deliver_refused_while_own_merge_runs(self) -> None:
        gate = self.gate_path()
        repo, prj = self.gated_project("D1", gate)
        tsk, _ = self.accepted_task(prj, "Feature", "f.txt", "f\n")
        main_before = self.fixture.head(repo, "main")
        th, box = self.background_merge(tsk)
        self.wait_merge_lease(prj)
        rc, resp = self.harness.cli("project", "deliver", prj, token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, c.EXIT_BUSY, resp)
        err = resp["error"]
        self.assertEqual(err["code"], c.MERGE_IN_PROGRESS, err)
        self.assertEqual(err["details"]["reason"], "merge_in_progress")
        self.assertEqual(err["details"]["task_id"], tsk)
        self.assertEqual(err["details"]["merge_project_id"], prj)
        self.assertEqual(self.fixture.head(repo, "main"), main_before)
        self.finish_merge(th, box, gate)
        rc, resp = self.harness.cli("project", "deliver", prj, token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.fixture.head(repo, "main"), self.fixture.head(repo, self.integ_branch(prj)))
        self.assert_remote_unchanged()
        self.doctor_ok()

    def test_deliver_refused_while_sibling_project_on_same_repo_merges(self) -> None:
        gate = self.gate_path()
        repo, prj = self.gated_project("D2", gate)
        other = self.second_project_on_repo(repo)
        tsk, _ = self.accepted_task(prj, "Feature", "f.txt", "f\n")
        th, box = self.background_merge(tsk)
        self.wait_merge_lease(prj)
        rc, resp = self.harness.cli("project", "deliver", other, token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, c.EXIT_BUSY, resp)
        self.assertEqual(resp["error"]["code"], c.MERGE_IN_PROGRESS)
        self.assertEqual(resp["error"]["details"]["merge_project_id"], prj)
        self.finish_merge(th, box, gate)
        rc, resp = self.harness.cli("project", "deliver", other, token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, 0, resp)

    def test_deliver_refused_for_merging_task_without_lease_row(self) -> None:
        """A task still `merging` blocks deliver even if the lease row is gone (state check, not only lease)."""
        gate = self.gate_path()
        repo, prj = self.gated_project("D3", gate)
        tsk, _ = self.accepted_task(prj, "Feature", "f.txt", "f\n")
        th, box = self.background_merge(tsk)
        self.wait_merge_lease(prj)
        self.assertEqual(self.show(tsk)["status"], "merging")
        rc, resp = self.harness.cli("project", "deliver", prj, "--dry-run", token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, c.EXIT_BUSY, resp)  # dry-run reports the same refusal
        self.finish_merge(th, box, gate)

    def test_prune_skips_with_merge_in_progress(self) -> None:
        gate = self.gate_path()
        repo, prj = self.gated_project("P1", gate)
        self.open_gate(gate)  # the first merge passes at once
        done, _ = self.accepted_task(prj, "Done", "a.txt", "a\n")
        rc, resp = self.merge(done)
        self.assertEqual(rc, 0, resp)
        os.unlink(gate)  # now block the second merge
        tsk, _ = self.accepted_task(prj, "Slow", "b.txt", "b\n")
        th, box = self.background_merge(tsk)
        self.wait_merge_lease(prj)
        wt = self.show(done)["workspace_path"]
        rc, resp = self.harness.cli("workspace", "prune", "--project", prj, "--dry-run",
                                    token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["would"]["transitions"], [], resp)  # nothing would be pruned
        rc, resp = self.harness.cli("workspace", "prune", "--project", prj, token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["removed"], [], resp)
        self.assertEqual([(s["task_id"], s["reason"]) for s in resp["data"]["skipped"]],
                         [(done, "merge_in_progress")], resp)
        self.assertTrue(os.path.isdir(wt))
        self.finish_merge(th, box, gate)
        rc, resp = self.harness.cli("workspace", "prune", "--project", prj, token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(sorted(e["task_id"] for e in resp["data"]["removed"]), sorted([done, tsk]), resp)
        self.assertEqual(resp["data"]["skipped"], [])

    def test_prune_skips_when_sibling_project_on_same_repo_merges(self) -> None:
        gate = self.gate_path()
        repo, prj = self.gated_project("P2", gate)
        other = self.second_project_on_repo(repo)
        # a merged task in the sibling project (test_command none) is a prune candidate
        tsk_o, _ = self.accepted_task(other, "Other done", "o.txt", "o\n")
        rc, resp = self.merge(tsk_o)
        self.assertEqual(rc, 0, resp)
        tsk, _ = self.accepted_task(prj, "Slow", "b.txt", "b\n")
        th, box = self.background_merge(tsk)
        self.wait_merge_lease(prj)
        rc, resp = self.harness.cli("workspace", "prune", "--project", other, token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["removed"], [], resp)
        self.assertEqual([(s["task_id"], s["reason"]) for s in resp["data"]["skipped"]],
                         [(tsk_o, "merge_in_progress")], resp)
        self.finish_merge(th, box, gate)


class TestP26SerialLeadMerges(LeadActionCase):
    """P2-6: two `task.merge` actions of one project in one lead turn run one after the other."""

    needs_git = True

    def _submitted_code_task(self, project_id: str, tok_ibx: str, name: str) -> str:
        rc, resp = self.harness.cli("task", "create", "--project", project_id, "--title", name,
                                    "--spec", "edit", "--assignee", "ibx", "--kind", "code", user=True)
        self.assertEqual(rc, 0, resp)
        task_id = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(lambda: self._task_status(task_id) == "queued",
                                              timeout=15.0, step=0.1))
        rc, st = self.harness.cli("task", "start", task_id, token=tok_ibx)
        self.assertEqual(rc, 0, st)
        ws = st["data"]["workspace"]
        with open(os.path.join(ws, f"{name}.txt"), "w", encoding="utf-8") as f:
            f.write(f"{name}\n")
        self.fixture.git(ws, "add", f"{name}.txt")
        self.fixture.git(ws, "commit", "-m", name, identity=True)
        rc, resp = self.harness.cli("task", "submit", task_id, "--run", st["data"]["run_id"],
                                    "--fence", str(st["data"]["fence"]), "--summary", "done", token=tok_ibx)
        self.assertEqual(rc, 0, resp)
        return task_id

    def test_two_merges_of_one_project_in_one_round_do_not_collide(self) -> None:
        assert self.fixture is not None
        tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        repo = self.fixture.make_k0("SER")
        slow = json.dumps(["python3", "-c", "import time; time.sleep(1.5)"])  # holds the merge lease
        rc, resp = self.harness.cli("intake", "submit", "--title", "Serial merges", "--goal", "g",
                                    "--criterion", "C1", "--lead", "flead", "--repo", repo,
                                    "--base-branch", "main", "--test-command", slow, token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        project_id = resp["data"]["id"]
        a = self._submitted_code_task(project_id, tok_ibx, "alpha")
        b = self._submitted_code_task(project_id, tok_ibx, "beta")
        self._set_actions(project_id, [
            {"cmd": "task.accept", "args": {"task": a}},
            {"cmd": "task.accept", "args": {"task": b}},
            {"cmd": "task.merge", "args": {"task": a}},
            {"cmd": "task.merge", "args": {"task": b}},
        ], "merge both")
        self.assertTrue(self.harness.wait_for(
            lambda: self._task_status(a) == "merged" and self._task_status(b) == "merged",
            timeout=90.0, step=0.2), f"statuses: {self._task_status(a)}, {self._task_status(b)}")
        run_id = self._project_action_results(project_id, "task.merge")[0]["run_id"]
        results = self._action_results(run_id)
        # No `merge_start` failure (a second merge colliding with the first one's lease).
        self.assertEqual([(r["index"], r["cmd"], r["ok"]) for r in results],
                         [(0, "task.accept", True), (1, "task.accept", True),
                          (2, "task.merge", True), (3, "task.merge", True)], results)
        self.assertEqual(self._rows("SELECT 1 FROM outbox WHERE type = 'lead.action_failed';"), [])
        merging = self._rows("SELECT task_id, seq FROM events WHERE type = 'task.merging' ORDER BY seq;")
        merged = {r["task_id"]: r["seq"] for r in
                  self._rows("SELECT task_id, seq FROM events WHERE type = 'task.merged';")}
        self.assertEqual([r["task_id"] for r in merging], [a, b])
        # strictly serial: the second merge starts only after the first one was recorded
        self.assertGreater(merging[1]["seq"], merged[a])


class TestP28ForceMergeHeld(MergeGateCase):
    """P2-8: `run release <run> --force-merge-held --user` for a held merge_test run."""

    def write_db(self, sql: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def held_merge(self) -> Tuple[str, str, str]:
        """A task stuck `merging` with an open merge_test run and the merge:<prj> lease: the state
        MergeRunHeld leaves behind. Returns (project, task, run)."""
        tsk, _ = self.accepted_task(self.prj, "Held", "held.txt", "h\n")
        pre_head = self.fixture.head(self.repo, self.integ_branch(self.prj))
        run_id, mrg = "run_heldmerge0001", "mrg_heldmerge0001"
        run_dir = os.path.join(self.harness.home, "merges", mrg)
        os.makedirs(run_dir, exist_ok=True)
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10)
        try:
            row = conn.execute("SELECT attempt, fence_seq FROM tasks WHERE id = ?;", (tsk,)).fetchone()
            boot = conn.execute("SELECT value FROM meta WHERE key = 'boot_id';").fetchone()
            boot_id = boot[0] if boot else "boot_test"
            conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, run_dir, argv_redacted_json, started_at) "
                "VALUES (?, 'merge_test', ?, ?, NULL, ?, ?, 'mesh', 'running', ?, ?, '[]', "
                "'2026-01-01T00:00:00Z');", (run_id, tsk, self.prj, row[0], row[1], boot_id, run_dir))
            conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, "
                "heartbeat_at) VALUES (?, ?, 'meshd', ?, NULL, ?, '2026-01-01T00:00:00Z', "
                "'2099-01-01T00:00:00Z', '2026-01-01T00:00:00Z');", (f"merge:{self.prj}", run_id, boot_id, row[1]))
            conn.execute("UPDATE tasks SET status = 'merging', merge_json = ? WHERE id = ?;",
                         (json.dumps({"mrg": mrg, "pre_head": pre_head, "started_at": "2026-01-01T00:00:00Z",
                                      "run_id": run_id}), tsk))
            conn.commit()
        finally:
            conn.close()
        return self.prj, tsk, run_id

    def release(self, run_id: str, *flags: str, token: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        argv = ["run", "release", run_id, *flags]
        if token is None:
            return self.harness.cli(*argv, user=True, timeout=60.0)
        return self.harness.cli(*argv, token=token, timeout=60.0)

    def test_release_closes_run_frees_lease_audits_and_unsticks_the_task(self) -> None:
        prj, tsk, run_id = self.held_merge()
        # the merge channel is blocked: both the lease and the `merging` state
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        rc, resp = self.release(run_id, "--force-merge-held")
        self.assertEqual(rc, 0, resp)
        self.assertEqual((resp["data"]["released"], resp["data"]["status"], resp["data"]["task_id"]),
                         (True, "lost", tsk), resp)
        run = self.q("SELECT status, end_reason, ended_at FROM runs WHERE id = ?;", (run_id,))[0]
        self.assertEqual((run["status"], run["end_reason"]), ("lost", "merge_release_forced"))
        self.assertIsNotNone(run["ended_at"])
        self.assert_no_merge_lease(prj)
        ev = self.q("SELECT actor_kind, via, payload_json FROM events WHERE type = 'run.merge_release_forced' "
                    "AND run_id = ?;", (run_id,))
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["actor_kind"], ev[0]["via"]), ("user", "direct"))
        payload = json.loads(ev[0]["payload_json"])
        self.assertEqual((payload["run_id"], payload["task_id"], payload["lease"], payload["lease_released"]),
                         (run_id, tsk, f"merge:{prj}", True))
        # the task was decided like boot recovery would (nothing merged: merge_failed/interrupted) ...
        self.assertTrue(self.harness.wait_for(lambda: self.show(tsk)["status"] == "merge_failed", timeout=15))
        self.assertEqual(self.show(tsk)["merge"]["reason"], "interrupted")
        # ... so the project's merge channel works again, without a restart
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["status"], "merged")
        # a second release: the run is closed
        rc, resp = self.release(run_id, "--force-merge-held")
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["details"]["reason"], "run_not_open")
        self.doctor_ok()

    def test_refused_while_wrapper_alive(self) -> None:
        prj, tsk, run_id = self.held_merge()
        proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        with open(f"/proc/{proc.pid}/stat", "rb") as f:
            starttime = int(f.read().rsplit(b")", 1)[1].split()[19])
        self.write_db("UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? WHERE id = ?;",
                      (proc.pid, proc.pid, starttime, run_id))
        rc, resp = self.release(run_id, "--force-merge-held")
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["code"], c.RUN_STILL_ACTIVE, resp)
        self.assertEqual(resp["error"]["details"]["reason"], "wrapper_alive")
        self.assertEqual(self.q("SELECT status FROM runs WHERE id = ?;", (run_id,))[0]["status"], "running")
        self.assertEqual(len(self.q("SELECT 1 FROM leases WHERE key = ?;", (f"merge:{prj}",))), 1)
        self.assertEqual(self.q("SELECT 1 FROM events WHERE type = 'run.merge_release_forced';"), [])
        # the wrapper exits: the release is allowed (a pid reused by another process would not count)
        proc.kill()
        proc.wait()
        rc, resp = self.release(run_id, "--force-merge-held")
        self.assertEqual(rc, 0, resp)
        self.assert_no_merge_lease(prj)

    def test_reused_pid_is_not_the_wrapper(self) -> None:
        prj, tsk, run_id = self.held_merge()
        # a live pid with a different starttime than the recorded one: another process now
        other = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.write_db("UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = 1 WHERE id = ?;",
                      (other.pid, other.pid, run_id))
        rc, resp = self.release(run_id, "--force-merge-held")
        self.assertEqual(rc, 0, resp)

    def test_only_the_user_open_merge_test_runs_and_exact_flags(self) -> None:
        prj, tsk, run_id = self.held_merge()
        # an agent (even the lead) is forbidden; nothing changes
        rc, resp = self.release(run_id, "--force-merge-held", token=self.tok_lead1)
        self.assertEqual(rc, 4, resp)
        self.assertEqual(self.q("SELECT status FROM runs WHERE id = ?;", (run_id,))[0]["status"], "running")
        # no flag / both flags are usage errors, and the old flag still refuses a merge_test run
        rc, resp = self.release(run_id)
        self.assertEqual(rc, 2, resp)
        self.assertIn("--force-merge-held", json.dumps(resp))
        rc, resp = self.release(run_id, "--force-merge-held", "--force-seen-unusable")
        self.assertEqual(rc, 2, resp)
        rc, resp = self.release(run_id, "--force-seen-unusable")
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["details"]["reason"], "not_seen_held")
        # a run that is not a merge_test run is refused
        self.write_db("UPDATE runs SET kind = 'external' WHERE id = ?;", (run_id,))
        rc, resp = self.release(run_id, "--force-merge-held")
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["details"]["reason"], "not_merge_test")
        self.write_db("UPDATE runs SET kind = 'merge_test' WHERE id = ?;", (run_id,))
        # a dry run changes nothing
        rc, resp = self.harness.cli("run", "release", run_id, "--force-merge-held", "--dry-run", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.q("SELECT status FROM runs WHERE id = ?;", (run_id,))[0]["status"], "running")
        self.assertEqual(len(self.q("SELECT 1 FROM leases WHERE key = ?;", (f"merge:{prj}",))), 1)
        # finally release for a clean doctor
        rc, resp = self.release(run_id, "--force-merge-held")
        self.assertEqual(rc, 0, resp)


class TestP25NoopMergeRunsTests(MergeGateCase):
    """P2-5: a task whose tip is already in integration still runs test_command, and its
    merge_commit is the commit that really merged it, not the tip."""

    def test_zero_commit_task_with_failing_tests_is_not_merged(self) -> None:
        repo, prj = self.make_project("N1", test_command='["false"]')
        self.repo = repo
        self.remote_before = self.fixture.remote_refs(repo)
        tsk, _ = self.accepted_task(prj, "Nothing", None)  # submitted without a commit
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        self.assertEqual(resp["error"]["code"], c.TESTS_FAILED, resp)
        det = resp["error"]["details"]
        self.assertEqual(det["reason"], "tests_failed")
        self.assertNotEqual(det["exit_code"], 0)
        self.assertTrue(os.path.isfile(det["log_path"]))
        self.assertEqual(self.show(tsk)["status"], "merge_failed")
        self.assertEqual(self.show(tsk)["merge"]["tests"], "failed")
        self.assert_integration_restored(repo, prj, pre_head)
        self.assert_no_merge_lease(prj)
        self.assertEqual(self.merge_runs(tsk)[0]["end_reason"], "tests_failed")
        self.doctor_ok()

    def test_noop_merge_runs_passing_tests_and_records_the_merging_commit(self) -> None:
        repo, prj = self.make_project("N2", test_command='["true"]')
        self.repo = repo
        self.remote_before = self.fixture.remote_refs(repo)
        tsk, _ = self.accepted_task(prj, "Feature", "f.txt", "f\n")
        branch = f"mesh/{prj}/{tsk}"
        tip = self.fixture.head(repo, branch)
        # The branch is merged into integration behind Mesh's back (the P1-3 crash window leaves
        # exactly this: a merge commit in git, the task not `merged`), then more history on top.
        wt = self.integ_wt(prj)
        self.fixture.git(wt, "merge", "--no-ff", branch, "-m", "outside merge", identity=True)
        merge_commit = self.fixture.head(wt)
        self.assertNotEqual(merge_commit, tip)
        with open(os.path.join(wt, "later.txt"), "w", encoding="utf-8") as f:
            f.write("later\n")
        self.fixture.git(wt, "add", "later.txt")
        self.fixture.git(wt, "commit", "-m", "later work", identity=True)
        head = self.fixture.head(wt)
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        data = resp["data"]
        self.assertIs(data["noop"], True, data)
        self.assertEqual(data["tests"], "passed", data)  # not "none": the tests ran
        self.assertEqual(data["merge_commit"], merge_commit, data)  # the commit that merged it, not the tip
        self.assertNotEqual(data["merge_commit"], tip)
        self.assertEqual(self.fixture.head(wt), head)  # nothing was committed
        self.assertEqual(self.show(tsk)["merge"]["merge_commit"], merge_commit)
        self.assertTrue(os.path.isfile(data["test_log"]))
        self.doctor_ok()  # I9: merge_commit is an ancestor of integration

    def test_zero_commit_task_after_other_merges_records_its_tip_not_a_later_merge(self) -> None:
        repo, prj = self.make_project("N3", test_command='["true"]')
        self.repo = repo
        self.remote_before = self.fixture.remote_refs(repo)
        tsk_z, _ = self.accepted_task(prj, "Nothing", None)  # its tip is the base it started from
        tip = self.fixture.head(repo, f"mesh/{prj}/{tsk_z}")
        tsk_o, _ = self.accepted_task(prj, "Other", "o.txt", "o\n")
        rc, resp = self.merge(tsk_o)
        self.assertEqual(rc, 0, resp)
        other_merge = resp["data"]["merge_commit"]
        rc, resp = self.merge(tsk_z)
        self.assertEqual(rc, 0, resp)
        self.assertIs(resp["data"]["noop"], True, resp)
        self.assertEqual(resp["data"]["merge_commit"], tip, resp)  # not the other task's merge
        self.assertNotEqual(resp["data"]["merge_commit"], other_merge)
        self.doctor_ok()

    def test_noop_merge_without_tests_keeps_tests_none(self) -> None:
        tsk, _ = self.accepted_task(self.prj, "Nothing", None)
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        self.assertEqual((resp["data"]["noop"], resp["data"]["tests"]), (True, "none"), resp)


class TestP13IntentAndRecoveryByParents(MergeGateCase):
    """P1-3: (pre_head, tip) is made durable before the merge commit, and crash recovery matches
    the merge commit by those parents, not by its message or the current task branch tip."""

    merge_json = TestRecoveryMerge.merge_json
    merge_events = TestRecoveryMerge.merge_events
    recovered_events = TestRecoveryMerge.recovered_events

    def install_message_rewriting_hook(self, repo: str) -> None:
        hook = os.path.join(repo, ".git", "hooks", "commit-msg")
        with open(hook, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho "chore: merge branch" > "$1"\n')
        os.chmod(hook, 0o755)

    def crash_after_merge_commit(self, tsk: str) -> None:
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_merge_commit"})
        rc, resp = self.merge(tsk)
        self.assertNotEqual(rc, 0, resp)
        self.harness.wait_daemon_exit(timeout=30.0)
        self.assertEqual(self.q("SELECT status FROM tasks WHERE id = ?;", (tsk,))[0]["status"], "merging")

    def crashed_project(self, name: str) -> Tuple[str, str, str, str, str]:
        repo, prj = self.make_project(name, test_command='["python3","-c","pass"]')
        self.repo = repo
        self.remote_before = self.fixture.remote_refs(repo)
        tsk, ws = self.accepted_task(prj, "Commit then crash", "crash.txt", "crash\n")
        return repo, prj, tsk, ws, self.fixture.head(repo, f"mesh/{prj}/{tsk}")

    def test_intent_is_durable_and_names_pre_head_and_tip(self) -> None:
        repo, prj, tsk, _ws, tip = self.crashed_project("I1")
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        mrg = self.show(tsk)["merge"]["mrg"]
        path = os.path.join(self.harness.home, "merges", mrg, "merge_intent.json")
        with open(path, encoding="utf-8") as f:
            intent = json.load(f)
        self.assertEqual((intent["task_id"], intent["pre_head"], intent["tip"]), (tsk, pre_head, tip))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))
        self.doctor_ok()

    def test_commit_msg_hook_that_rewrites_the_message_is_still_reconciled(self) -> None:
        repo, prj, tsk, _ws, tip = self.crashed_project("I2")
        self.install_message_rewriting_hook(repo)
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        self.crash_after_merge_commit(tsk)
        head = self.fixture.head(repo, self.integ_branch(prj))
        self.assertNotEqual(head, pre_head)
        self.assertEqual(self.fixture.git(repo, "log", "-1", "--format=%s", head).stdout.strip(),
                         "chore: merge branch")  # the task id is not in the message any more
        self.harness.restart_daemon()
        d = self.show(tsk)
        self.assertEqual(d["status"], "merged", d)
        mj = self.merge_json(tsk)
        self.assertEqual((mj["merge_commit"], mj["pre_head"], mj["reconciled"]), (head, pre_head, True), mj)
        self.assertEqual(mj["matched_by"], "merge_intent")
        parents = self.fixture.git(repo, "rev-list", "--parents", "-n", "1", head).stdout.split()
        self.assertEqual(parents[1:], [pre_head, tip])
        self.assertEqual(self.merge_events(tsk, "merge.failed"), [])
        self.assert_no_merge_lease(prj)
        self.doctor_ok()

    def test_task_branch_advanced_after_the_tip_was_resolved_is_still_reconciled(self) -> None:
        repo, prj, tsk, ws, tip = self.crashed_project("I3")
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        self.crash_after_merge_commit(tsk)
        head = self.fixture.head(repo, self.integ_branch(prj))
        # an inbox executor keeps working on its branch while meshd is down
        self.commit_file(ws, "more.txt", "more\n", "more work after the merge")
        self.assertNotEqual(self.fixture.head(repo, f"mesh/{prj}/{tsk}"), tip)
        self.harness.restart_daemon()
        d = self.show(tsk)
        self.assertEqual(d["status"], "merged", d)
        mj = self.merge_json(tsk)
        self.assertEqual((mj["merge_commit"], mj["pre_head"], mj["reconciled"]), (head, pre_head, True), mj)
        self.assertEqual(self.merge_events(tsk, "merge.failed"), [])
        self.doctor_ok()

    def test_without_intent_and_with_rewritten_message_doctor_flags_the_cancelled_task(self) -> None:
        """The report's scenario when no intent is available (a run from before intents): recovery
        cannot prove the merge, the task ends merge_failed and, once cancelled, `doctor
        --check-invariants` reports that its work is already in integration."""
        repo, prj, tsk, _ws, tip = self.crashed_project("I4")
        self.install_message_rewriting_hook(repo)
        self.crash_after_merge_commit(tsk)
        mrg = json.loads(self.q("SELECT merge_json FROM tasks WHERE id = ?;", (tsk,))[0]["merge_json"])["mrg"]
        os.unlink(os.path.join(self.harness.home, "merges", mrg, "merge_intent.json"))
        self.harness.restart_daemon()
        self.assertEqual(self.show(tsk)["status"], "merge_failed")
        self.assertEqual(self.merge_json(tsk)["reason"], "interrupted")
        rc, resp = self.harness.cli("doctor", "--check-invariants", user=True, timeout=60.0)
        self.assertEqual(rc, 1, resp)  # already: merge_failed but integrated
        rc, resp = self.harness.cli("task", "cancel", tsk, "--reason", "abandon", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("doctor", "--check-invariants", user=True, timeout=60.0)
        self.assertEqual(rc, 1, resp)
        viol = resp["error"]["details"]["violations"]
        self.assertEqual([(v["invariant"], v["details"]["task_id"], v["details"]["reason"]) for v in viol],
                         [("I9", tsk, "integrated_but_not_merged")], viol)
        self.assertEqual(viol[0]["details"]["status"], "cancelled")
        head = self.fixture.head(repo, self.integ_branch(prj))
        self.assertEqual(self.fixture.git(repo, "rev-list", "--parents", "-n", "1", head).stdout.split()[2], tip)
        # the cancelled task's code never reaches main: deliver refuses (P1-2 follow-up gate)
        main_before = self.fixture.head(repo, "main")
        rc, resp = self.harness.cli("project", "deliver", prj, token=self.tok_lead1, timeout=60.0)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["details"]["reason"], "integrated_unmerged_work", resp)
        self.assertEqual(resp["error"]["details"]["tasks"][0]["status"], "cancelled", resp)
        self.assertEqual(self.fixture.head(repo, "main"), main_before)
        self.harness.check_invariants_on_teardown = False  # the violation is the point of this test


# Carbon P1-2 test commands (run with cwd = the integration worktree).
COV_FAIL_SCRIPT = ('open("cov.xml", "w").write("x"); raise SystemExit(1)')  # the report's repro
COV_SCRIPT = ("import os\n"
              "open('cov.xml', 'w').write('x')\n"
              "os.makedirs('build', exist_ok=True)\n"
              "open(os.path.join('build', 'junit.xml'), 'w').write('<x/>')\n"
              "raise SystemExit(1 if os.path.exists('broken.txt') else 0)\n")
STAGE_SCRIPT = ("import subprocess\n"
                "open('injected.py', 'w').write('evil = 1\\n')\n"
                "subprocess.run(['git', 'add', 'injected.py'], check=True)\n")
REWRITE_SCRIPT = ("with open('calc.py', 'a') as f:\n"
                  "    f.write('# rewritten by the test\\n')\n")
COMMIT_SCRIPT = ("import subprocess\n"
                 "subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',\n"
                 "                'commit', '--no-edit', '-m', 'by the test'], check=True)\n")


class TestP12TestStepGitState(MergeGateCase):
    """P1-2: test_command runs in the integration worktree; whatever it changes in git fails
    the step, and what it leaves in the tree is cleared, so the project never ends up
    INTEGRATION_DIRTY and nothing unreviewed enters the merge commit."""

    def py_command(self, script: str) -> str:
        return json.dumps(["python3", "-c", script])

    def project_with(self, name: str, script: str) -> Tuple[str, str]:
        repo, prj = self.make_project(name, test_command=self.py_command(script))
        self.repo = repo
        self.remote_before = self.fixture.remote_refs(repo)
        return repo, prj

    def tree_files(self, repo: str, rev: str) -> List[str]:
        out = self.fixture.git(repo, "ls-tree", "-r", "--name-only", rev).stdout
        return [ln for ln in out.splitlines() if ln]

    def test_report_repro_failed_test_with_untracked_output_does_not_wedge_the_project(self) -> None:
        repo, prj = self.project_with("P12a", COV_FAIL_SCRIPT)
        tsk_a, _ = self.accepted_task(prj, "A", "a.txt", "a\n")
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        rc, resp = self.merge(tsk_a)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        self.assertEqual(resp["error"]["code"], c.TESTS_FAILED, resp)
        det = resp["error"]["details"]
        self.assertIs(det["restored"], True, det)
        self.assertIn("cov.xml", det["cleanup"]["removed"], det)
        self.assert_integration_restored(repo, prj, pre_head)
        self.assertFalse(os.path.exists(os.path.join(self.integ_wt(prj), "cov.xml")))
        # Task B: before the fix rc 14 INTEGRATION_DIRTY; now its own test run decides (and fails).
        tsk_b, _ = self.accepted_task(prj, "B", "b.txt", "b\n")
        rc, resp = self.merge(tsk_b)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        self.assertEqual(resp["error"]["code"], c.TESTS_FAILED, resp)
        self.assertNotEqual(resp["error"]["code"], c.INTEGRATION_DIRTY)
        self.assert_integration_restored(repo, prj, pre_head)
        self.assert_no_merge_lease(prj)
        self.assert_remote_unchanged()
        self.doctor_ok()

    def test_later_merge_succeeds_and_test_output_never_enters_the_merge_commit(self) -> None:
        repo, prj = self.project_with("P12b", COV_SCRIPT)
        tsk_a, _ = self.accepted_task(prj, "Broken", "broken.txt", "x\n")
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        rc, resp = self.merge(tsk_a)
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_MERGE_FAILED, c.TESTS_FAILED), resp)
        self.assert_integration_restored(repo, prj, pre_head)
        tsk_b, _ = self.accepted_task(prj, "Good", "b.txt", "b\n")
        rc, resp = self.merge(tsk_b)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["tests"], "passed", resp)
        mc = resp["data"]["merge_commit"]
        files = self.tree_files(repo, mc)
        self.assertIn("b.txt", files)
        for leaked in ("cov.xml", "build/junit.xml", "broken.txt"):
            self.assertNotIn(leaked, files)
        wt = self.integ_wt(prj)
        self.assertEqual(self.fixture.head(wt), mc)
        self.assertEqual(self.fixture.git(wt, "status", "--porcelain", "--untracked-files=all").stdout, "")
        self.assertEqual(self.show(tsk_b)["status"], "merged")
        self.doctor_ok()

    def test_a_passing_test_that_stages_a_file_is_tests_failed_and_nothing_is_committed(self) -> None:
        repo, prj = self.project_with("P12c", STAGE_SCRIPT)
        tsk, _ = self.accepted_task(prj, "Feature", "f.txt", "f\n")
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        self.assertEqual(resp["error"]["code"], c.TESTS_FAILED, resp)
        det = resp["error"]["details"]
        self.assertEqual(det["exit_code"], 0, det)  # the command itself "passed"
        self.assertIn("index_changed", det["git_changes"], det)
        self.assertEqual(det["error"], "test_changed_git_state", det)
        self.assertIs(det["restored"], True, det)
        self.assert_integration_restored(repo, prj, pre_head)
        self.assertNotIn("injected.py", self.tree_files(repo, self.integ_branch(prj)))
        self.assertFalse(os.path.exists(os.path.join(self.integ_wt(prj), "injected.py")))
        self.assertEqual(self.show(tsk)["status"], "merge_failed")
        self.assertEqual(self.merge_runs(tsk)[0]["end_reason"], "tests_failed")
        self.assert_no_merge_lease(prj)
        self.assert_remote_unchanged()
        self.doctor_ok()

    def test_a_tracked_file_rewritten_by_a_passing_test_is_restored_before_the_commit(self) -> None:
        repo, prj = self.project_with("P12d", REWRITE_SCRIPT)
        original = self.fixture.git(repo, "show", "main:calc.py").stdout
        tsk, _ = self.accepted_task(prj, "Feature", "f.txt", "f\n")
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        mc = resp["data"]["merge_commit"]
        self.assertEqual(self.fixture.git(repo, "show", f"{mc}:calc.py").stdout, original)
        wt = self.integ_wt(prj)
        with open(os.path.join(wt, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        self.assertEqual(self.fixture.git(wt, "status", "--porcelain", "--untracked-files=all").stdout, "")
        self.doctor_ok()

    def test_noop_merge_test_output_is_cleared(self) -> None:
        repo, prj = self.project_with("P12e", COV_SCRIPT)
        tsk, _ = self.accepted_task(prj, "Nothing", None)
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        self.assertEqual((resp["data"]["noop"], resp["data"]["tests"]), (True, "passed"), resp)
        wt = self.integ_wt(prj)
        self.assertEqual(self.fixture.git(wt, "status", "--porcelain", "--untracked-files=all").stdout, "")
        tsk_b, _ = self.accepted_task(prj, "Next", "n.txt", "n\n")
        rc, resp = self.merge(tsk_b)
        self.assertEqual(rc, 0, resp)

    def test_a_test_that_commits_moves_head_and_is_tests_failed(self) -> None:
        repo, prj = self.project_with("P12f", COMMIT_SCRIPT)
        tsk, _ = self.accepted_task(prj, "Feature", "f.txt", "f\n")
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        self.assertEqual(resp["error"]["code"], c.TESTS_FAILED, resp)
        det = resp["error"]["details"]
        self.assertIn("head_moved", det["git_changes"], det)
        # HEAD is never rewound (no reset, no non-CAS ref update): reported as not restored.
        self.assertIs(det["restored"], False, det)
        self.assertEqual(self.show(tsk)["status"], "merge_failed")
        self.assert_no_merge_lease(prj)
        self.assert_remote_unchanged()
        # The commit the test made stays in integration; reverse I9 (P1-3) is what reports it.
        rc, resp = self.harness.cli("doctor", "--check-invariants")
        self.assertNotEqual(rc, 0, resp)
        reasons = [(v["details"]["task_id"], v["details"]["reason"]) for v in resp["error"]["details"]["violations"]]
        self.assertEqual(reasons, [(tsk, "integrated_but_not_merged")], resp)
        # ... and `project deliver` refuses to hand that unreviewed commit to the base branch.
        main_before = self.fixture.head(repo, "main")
        for args in (("--dry-run",), ()):
            rc, resp = self.harness.cli("project", "deliver", prj, *args, token=self.tok_lead1, timeout=60.0)
            self.assertEqual(rc, c.EXIT_CONFLICT, resp)
            err = resp["error"]
            self.assertEqual((err["code"], err["details"]["reason"]),
                             (c.INVALID_TRANSITION, "integrated_unmerged_work"), err)
            self.assertEqual([t["task_id"] for t in err["details"]["tasks"]], [tsk], err)
        self.assertEqual(self.fixture.head(repo, "main"), main_before)
        self.assertEqual(self.q("SELECT 1 FROM events WHERE type = 'project.delivered';"), [])
        self.harness.check_invariants_on_teardown = False


if __name__ == "__main__":
    unittest.main()
