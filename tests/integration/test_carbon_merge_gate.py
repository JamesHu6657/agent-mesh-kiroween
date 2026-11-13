"""Regression tests for the Carbon merge-gate review (agent-mesh-kiroween, P1-2/P1-3/P2-5..P2-8).

Real meshd + real K0 git fixtures (see tests/integration/test_merge.py). Merges that must be
observed "in flight" use a project test command that blocks on a gate file, so the merge
lease and the `merging` state are held while the test drives the other commands.
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from tests.integration import test_merge as tm

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


if __name__ == "__main__":
    unittest.main()
