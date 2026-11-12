"""Integration tests for code tasks (plan §10.3, §10.4, §6.2 T5/T6; checklist K3, K4).

Every git repo is a K0 fixture under /tmp/mh-*-git; meshd runs with the fixture's hermetic
git env (GitMeshHarness). `fx` is the fake launch adapter; `ibx` is an inbox agent.
Nothing here touches the Mesh checkout, and nothing is ever pushed.

ACC: K3, K4 (deliverable clause: `deliverable.git.dirty` and `dirty_files`; the merge
refusal is r5), K11 (partial: remote refs unchanged).
"""

import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List

import mesh.constants as c
from tests.gitfixture import GitFixture, GitMeshHarness
from tests.integration.test_execution_launch import create_agent
from tests.stubs import create_test_stubs


def _commit_spec(path: str, content: str, message: str) -> str:
    return "FAKE " + json.dumps({"git_commit": {"path": path, "content": content, "message": message}})


class TestCodeTasks(unittest.TestCase):
    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.fixture = GitFixture()
        self.harness = GitMeshHarness(self.fixture, init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)  # also removes the fixture
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tok_ibx = create_agent(self.harness, "ibx")
        self.tok_fx = create_agent(self.harness, "fx", adapter="fake", delivery="launch",
                                   allowed_models=["m-a"], default_model="m-a", max_concurrency=2)
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)
        self.repo = self.fixture.make_k0()
        self.main = self.fixture.head(self.repo, "main")
        self.remote_before = self.fixture.remote_refs(self.repo)
        rc, resp = self.harness.cli("intake", "submit", "--title", "Code PRJ", "--goal", "g", "--criterion", "c1",
                                    "--repo", self.repo, "--base-branch", "main", "--test-command", "none",
                                    "--lead", "lead1", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        self.prj = resp["data"]["id"]
        self.integration = f"mesh/{self.prj}/integration"

    # --- helpers -----------------------------------------------------------------

    def create_task(self, spec: str, assignee: str, *extra: str, title: str = "T") -> str:
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", title, "--spec", spec,
                                    "--assignee", assignee, *extra, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def show(self, task_id: str) -> Dict[str, Any]:
        rc, resp = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return resp["data"]

    def wait_status(self, task_id: str, status: str, timeout: float = 60.0) -> Dict[str, Any]:
        box: Dict[str, Any] = {}

        def reached() -> bool:
            box["d"] = self.show(task_id)
            return box["d"]["status"] == status

        ok = self.harness.wait_for(reached, timeout=timeout)
        self.assertTrue(ok, f"{task_id} is {box.get('d', {}).get('status')}, never reached {status}: {box.get('d')}")
        return box["d"]

    def wait_queued(self, task_id: str) -> None:
        rc, resp = self.harness.cli("task", "wait", task_id, "--until", "state:queued", "--timeout", "10s",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)

    def worktrees(self) -> Dict[str, Dict[str, Any]]:
        return {os.path.realpath(str(e["worktree"])): e for e in self.fixture.worktrees(self.repo)}

    def has_path(self, ref: str, path: str) -> bool:
        return self.fixture.git(self.repo, "cat-file", "-e", f"{ref}:{path}", check=False).returncode == 0

    def lead1_task_failed(self, task_id: str) -> List[Dict[str, Any]]:
        conn = self.harness.db()
        try:
            rows = conn.execute(
                "SELECT o.payload_json FROM outbox o JOIN events e ON o.event_seq = e.seq "
                "WHERE e.task_id = ? AND o.type = 'task.failed' AND o.recipient_id = "
                "(SELECT id FROM principals WHERE name = 'lead1');",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        return [json.loads(r[0]) for r in rows]

    def assert_remote_unchanged(self) -> None:
        self.assertEqual(self.fixture.remote_refs(self.repo), self.remote_before)

    # --- K3 ----------------------------------------------------------------------

    def test_k3_two_code_tasks_each_in_own_worktree_and_branch(self) -> None:
        """ACC: K3, K11 (partial) - two `fx` code tasks commit on their own branches only."""
        t1 = self.create_task(_commit_spec("one.txt", "one\n", "task one"), "fx", title="one")
        t2 = self.create_task(_commit_spec("two.txt", "two\n", "task two"), "fx", title="two")
        d1 = self.wait_status(t1, "submitted")
        d2 = self.wait_status(t2, "submitted")

        wts = self.worktrees()
        heads: Dict[str, str] = {}
        for tsk, d, fname in ((t1, d1, "one.txt"), (t2, d2, "two.txt")):
            branch = f"mesh/{self.prj}/{tsk}"
            self.assertEqual(d["kind"], "code", d)
            self.assertEqual(d["branch"], branch, d)
            self.assertEqual(d["base_commit"], self.main, d)
            ws = os.path.realpath(d["workspace_path"])
            self.assertEqual(ws, os.path.realpath(os.path.join(self.harness.home, "workspaces", self.prj,
                                                               "tasks", tsk)))
            self.assertIn(ws, wts)
            self.assertEqual(wts[ws]["branch"], f"refs/heads/{branch}")
            head = self.fixture.head(self.repo, branch)
            heads[tsk] = head
            # One new commit on top of the integration tip, holding the task's file.
            self.assertEqual(self.fixture.head(self.repo, f"{branch}^"), self.main)
            self.assertTrue(self.has_path(branch, fname))
            # §10.4 on the auto-submit path.
            git = d["deliverable"]["git"]
            self.assertEqual(git["head"], head, git)
            self.assertEqual(git["branch"], branch, git)
            self.assertEqual(git["base_commit"], self.main, git)
            self.assertIs(git["dirty"], False, git)
            self.assertEqual(git["dirty_files"], [], git)
            self.assertEqual(git["commit_count"], 1, git)
            self.assertIn(fname, git["diff_stat"], git)
            self.assertNotIn("error", git)
            # §12.5 [code] line with the branch.
            conn = self.harness.db()
            try:
                run = conn.execute("SELECT run_dir FROM runs WHERE task_id = ?;", (tsk,)).fetchone()
            finally:
                conn.close()
            with open(os.path.join(run[0], "prompt.md"), encoding="utf-8") as f:
                prompt = f.read()
            self.assertIn(f"You are on git branch {branch}. Commit your work to this branch before you finish. "
                          "Do not push. Do not switch branches.", prompt)

        # Each commit is on its own branch only: not on integration, not on the other branch.
        self.assertNotEqual(heads[t1], heads[t2])
        self.assertEqual(self.fixture.head(self.repo, self.integration), self.main)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.main)
        self.assertFalse(self.has_path(self.integration, "one.txt"))
        self.assertFalse(self.has_path(self.integration, "two.txt"))
        self.assertFalse(self.has_path(f"mesh/{self.prj}/{t1}", "two.txt"))
        self.assertFalse(self.has_path(f"mesh/{self.prj}/{t2}", "one.txt"))
        self.assertEqual(sorted(self.fixture.branches(self.repo, f"mesh/{self.prj}/*")),
                         sorted([self.integration, f"mesh/{self.prj}/{t1}", f"mesh/{self.prj}/{t2}"]))
        self.assert_remote_unchanged()

    # --- K4 (deliverable clause) ---------------------------------------------------

    def test_k4_inbox_code_task_dirty_deliverable(self) -> None:
        """ACC: K4 (deliverable clause), K11 (partial) - an uncommitted file shows as git.dirty."""
        tsk = self.create_task("Edit things", "ibx")
        self.wait_queued(tsk)
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        branch = f"mesh/{self.prj}/{tsk}"
        self.assertEqual(st["data"]["branch"], branch, st)
        ws = st["data"]["workspace"]
        self.assertEqual(self.worktrees()[os.path.realpath(ws)]["branch"], f"refs/heads/{branch}")
        d = self.show(tsk)
        self.assertEqual(d["branch"], branch)
        self.assertEqual(d["base_commit"], self.main)

        # One committed change and one uncommitted file.
        with open(os.path.join(ws, "done.txt"), "w", encoding="utf-8") as f:
            f.write("done\n")
        self.fixture.git(ws, "add", "done.txt")
        self.fixture.git(ws, "commit", "-m", "done")
        with open(os.path.join(ws, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("not committed\n")
        head = self.fixture.head(ws)

        # An agent-sent `git` key is replaced by what Mesh records.
        rc, sub = self.harness.cli("task", "submit", tsk, "--run", st["data"]["run_id"],
                                   "--fence", str(st["data"]["fence"]), "--deliverable",
                                   json.dumps({"summary": "half done", "git": {"dirty": False}}),
                                   token=self.tok_ibx)
        self.assertEqual(rc, 0, sub)
        git = self.show(tsk)["deliverable"]["git"]
        self.assertIs(git["dirty"], True, git)
        self.assertIn("notes.txt", git["dirty_files"], git)
        self.assertEqual(git["head"], head, git)
        self.assertEqual(git["branch"], branch, git)
        self.assertEqual(git["base_commit"], self.main, git)
        self.assertEqual(git["commit_count"], 1, git)
        self.assertIn("done.txt", git["diff_stat"], git)
        # No auto-commit: the file is still uncommitted and the branch did not move.
        self.assertTrue(os.path.exists(os.path.join(ws, "notes.txt")))
        self.assertEqual(self.fixture.head(self.repo, branch), head)
        self.assertFalse(self.fixture.is_clean(ws))
        self.assertEqual(self.fixture.head(self.repo, self.integration), self.main)
        self.assert_remote_unchanged()

    # --- fresh-workspace rule (§6.2) ------------------------------------------------

    def test_fresh_branch_after_lease_expiry_keeps_old_worktree(self) -> None:
        """ACC: K3 (fresh-workspace rule), K11 (partial) - retry after lease_expired forks `-a2`."""
        rc, resp = self.harness.cli("config", "set", "lease.ttl_s", "2", user=True)
        self.assertEqual(rc, 0, resp)
        tsk = self.create_task("Work", "ibx")
        self.wait_queued(tsk)
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        old_ws = st["data"]["workspace"]
        old_branch = f"mesh/{self.prj}/{tsk}"
        with open(os.path.join(old_ws, "partial.txt"), "w", encoding="utf-8") as f:
            f.write("partial\n")
        self.fixture.git(old_ws, "add", "partial.txt")
        self.fixture.git(old_ws, "commit", "-m", "partial")
        old_tip = self.fixture.head(self.repo, old_branch)
        d = self.wait_status(tsk, "failed", timeout=20.0)
        self.assertEqual(d["failure"]["reason"], "lease_expired", d)
        # Only the first attempt needs the 2 s lease. Restore a long TTL so the second
        # attempt's lease cannot expire before its submit on a loaded box (rc 9
        # LEASE_EXPIRED seen on main, 2026-11-09); the K3 assertions are unchanged.
        rc, resp = self.harness.cli("config", "set", "lease.ttl_s", "600", user=True)
        self.assertEqual(rc, 0, resp)

        rc, resp = self.harness.cli("task", "retry", tsk, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.wait_queued(tsk)
        rc, st2 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, 0, st2)
        new_branch = f"mesh/{self.prj}/{tsk}-a2"
        self.assertEqual(st2["data"]["branch"], new_branch, st2)
        new_ws = st2["data"]["workspace"]
        self.assertTrue(new_ws.rstrip("/").endswith(f"tasks/{tsk}.a2"), new_ws)
        d = self.show(tsk)
        self.assertEqual(d["branch"], new_branch)
        self.assertEqual(d["workspace_path"], new_ws)
        self.assertEqual(d["base_commit"], self.main)  # still the integration fork point
        # The new branch starts at the old tip; the old worktree and branch are kept (§10.7).
        self.assertEqual(self.fixture.head(self.repo, new_branch), old_tip)
        self.assertEqual(self.fixture.head(self.repo, old_branch), old_tip)
        wts = self.worktrees()
        self.assertEqual(wts[os.path.realpath(new_ws)]["branch"], f"refs/heads/{new_branch}")
        self.assertEqual(wts[os.path.realpath(old_ws)]["branch"], f"refs/heads/{old_branch}")
        self.assertTrue(os.path.exists(os.path.join(old_ws, "partial.txt")))
        self.assertTrue(os.path.exists(os.path.join(new_ws, "partial.txt")))

        # Submit from the fresh worktree: git info is measured against the original base.
        rc, sub = self.harness.cli("task", "submit", tsk, "--run", st2["data"]["run_id"],
                                   "--fence", str(st2["data"]["fence"]), "--summary", "ok", token=self.tok_ibx)
        self.assertEqual(rc, 0, sub)
        git = self.show(tsk)["deliverable"]["git"]
        self.assertEqual(git["branch"], new_branch, git)
        self.assertEqual(git["head"], old_tip, git)
        self.assertEqual(git["commit_count"], 1, git)
        self.assertIs(git["dirty"], False, git)
        self.assert_remote_unchanged()

    def test_fresh_worktree_prepared_by_refused_start_is_used(self) -> None:
        """ACC: K3 (fresh-workspace rule; review m2-r4 finding 1) - a refused start forks `-a2`; the next start uses it."""
        rc, resp = self.harness.cli("config", "set", "lease.ttl_s", "2", user=True)
        self.assertEqual(rc, 0, resp)
        b = self.create_task("Work B", "ibx", title="B")
        self.wait_queued(b)
        rc, st = self.harness.cli("task", "start", b, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        old_ws = st["data"]["workspace"]
        old_branch = f"mesh/{self.prj}/{b}"
        self.fixture.git(old_ws, "commit", "--allow-empty", "-m", "partial", identity=True)
        old_tip = self.fixture.head(self.repo, old_branch)
        d = self.wait_status(b, "failed", timeout=20.0)
        self.assertEqual(d["failure"]["reason"], "lease_expired", d)
        rc, resp = self.harness.cli("config", "set", "lease.ttl_s", "600", user=True)
        self.assertEqual(rc, 0, resp)

        # ibx (max 1) is busy with A.
        a = self.create_task("Work A", "ibx", title="A")
        self.wait_queued(a)
        rc, st_a = self.harness.cli("task", "start", a, token=self.tok_ibx)
        self.assertEqual(rc, 0, st_a)

        rc, resp = self.harness.cli("task", "retry", b, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.wait_queued(b)
        new_branch = f"mesh/{self.prj}/{b}-a2"
        rc, refused = self.harness.cli("task", "start", b, token=self.tok_ibx)
        self.assertEqual(rc, c.EXIT_BUSY, refused)
        # The prepare ran before the concurrency check: `.a2` exists on `-a2`, T5 did not commit.
        self.assertIn(new_branch, self.fixture.branches(self.repo, f"mesh/{self.prj}/*"))
        self.assertEqual(self.show(b)["branch"], old_branch)

        rc, sub = self.harness.cli("task", "submit", a, "--run", st_a["data"]["run_id"],
                                   "--fence", str(st_a["data"]["fence"]), "--summary", "ok", token=self.tok_ibx)
        self.assertEqual(rc, 0, sub)
        rc, st2 = self.harness.cli("task", "start", b, token=self.tok_ibx)
        self.assertEqual(rc, 0, st2)
        self.assertEqual(st2["data"]["branch"], new_branch, st2)
        new_ws = st2["data"]["workspace"]
        self.assertTrue(new_ws.rstrip("/").endswith(f"tasks/{b}.a2"), new_ws)
        d = self.show(b)
        self.assertEqual(d["status"], "running")
        self.assertEqual(d["branch"], new_branch)
        self.assertEqual(d["base_commit"], self.main)
        self.assertEqual(self.fixture.head(self.repo, new_branch), old_tip)
        # The old worktree and branch are kept (§10.7).
        self.assertEqual(self.fixture.head(self.repo, old_branch), old_tip)
        wts = self.worktrees()
        self.assertEqual(wts[os.path.realpath(old_ws)]["branch"], f"refs/heads/{old_branch}")
        self.assertEqual(wts[os.path.realpath(new_ws)]["branch"], f"refs/heads/{new_branch}")
        self.assert_remote_unchanged()

    def test_code_task_in_prep_holds_its_slot(self) -> None:
        """ACC: D12, K3 (review m2-r4 finding 2) - fx1 full; a priority-5 code task starts before a priority-0 general task."""
        create_agent(self.harness, "fx1", adapter="fake", delivery="launch",
                     allowed_models=["m-a"], default_model="m-a", max_concurrency=1)
        busy = self.create_task('FAKE {"sleep": {"seconds": 3}}', "fx1", "--kind", "general", title="busy")
        self.wait_status(busy, "running", timeout=30.0)
        g = self.create_task('FAKE {"sleep": {"seconds": 1}}', "fx1", "--kind", "general", "--priority", "0",
                             title="general")
        code = self.create_task(_commit_spec("c.txt", "c\n", "c"), "fx1", "--priority", "5", title="code")
        self.wait_status(code, "submitted", timeout=60.0)
        self.wait_status(g, "submitted", timeout=60.0)
        conn = self.harness.db()
        try:
            order = [r[0] for r in conn.execute(
                "SELECT task_id FROM runs WHERE task_id IN (?, ?, ?) ORDER BY rowid;", (busy, g, code)).fetchall()]
        finally:
            conn.close()
        self.assertEqual(order, [busy, code, g])
        self.assert_remote_unchanged()

    def test_rework_reuses_worktree_and_branch(self) -> None:
        """ACC: K3 (rework reuses the dir and branch) - reject to rework, start again."""
        tsk = self.create_task("Work", "ibx")
        self.wait_queued(tsk)
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        ws = st["data"]["workspace"]
        rc, sub = self.harness.cli("task", "submit", tsk, "--run", st["data"]["run_id"],
                                   "--fence", str(st["data"]["fence"]), "--summary", "v1", token=self.tok_ibx)
        self.assertEqual(rc, 0, sub)
        rc, rej = self.harness.cli("task", "reject", tsk, "--reason", "again", token=self.tok_lead1)
        self.assertEqual(rc, 0, rej)
        self.wait_queued(tsk)
        rc, st2 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, 0, st2)
        self.assertEqual(st2["data"]["workspace"], ws)
        self.assertEqual(st2["data"]["branch"], f"mesh/{self.prj}/{tsk}")
        self.assertEqual(self.fixture.branches(self.repo, f"mesh/{self.prj}/{tsk}*"), [f"mesh/{self.prj}/{tsk}"])

    # --- T6 workspace_error, Q13 ------------------------------------------------------

    def test_workspace_error_inbox_and_launch(self) -> None:
        """ACC: K3 (T6 `workspace_error`: inbox rc 6 + failed, launch failed; lead notified)."""
        # Inbox: the workspace path is taken by a plain directory that is not a worktree.
        tsk = self.create_task("Work", "ibx")
        self.wait_queued(tsk)
        ws = self.show(tsk)["workspace_path"]
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "squatter.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, c.EXIT_CONFLICT, st)
        self.assertEqual(st["error"]["code"], c.INVALID_TRANSITION, st)
        self.assertEqual(st["error"]["details"]["reason"], "workspace_error", st)
        d = self.show(tsk)
        self.assertEqual(d["status"], "failed", d)
        self.assertEqual(d["failure"]["reason"], "workspace_error", d)
        self.assertIsNone(d["branch"])
        msgs = self.lead1_task_failed(tsk)
        self.assertTrue(any(m.get("reason") == "workspace_error" for m in msgs), msgs)

        # Launch: hold launches, squat on the path, release.
        hold = os.path.join(self.harness.home, "run", "launch_hold")
        open(hold, "w").close()
        t2 = self.create_task(_commit_spec("x.txt", "x\n", "x"), "fx")
        ws2 = self.show(t2)["workspace_path"]
        os.makedirs(ws2, exist_ok=True)
        with open(os.path.join(ws2, "squatter.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
        os.unlink(hold)
        d2 = self.wait_status(t2, "failed", timeout=30.0)
        self.assertEqual(d2["failure"]["reason"], "workspace_error", d2)
        conn = self.harness.db()
        try:
            n_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?;", (t2,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n_runs, 0)
        msgs = self.lead1_task_failed(t2)
        self.assertTrue(any(m.get("reason") == "workspace_error" for m in msgs), msgs)
        self.assertEqual(self.fixture.branches(self.repo, f"mesh/{self.prj}/*"), [self.integration])
        self.assert_remote_unchanged()

    def test_kind_code_needs_repo_project(self) -> None:
        """ACC: K3 (Q13) - `task create --kind code` on a non-repo project is rc 2 `param:"kind"`."""
        rc, resp = self.harness.cli("intake", "submit", "--title", "Plain", "--goal", "g", "--criterion", "c1",
                                    "--lead", "lead1", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        plain = resp["data"]["id"]
        rc, resp = self.harness.cli("task", "create", "--project", plain, "--title", "T", "--spec", "s",
                                    "--assignee", "ibx", "--kind", "code", token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT, resp)
        self.assertEqual(resp["error"]["details"]["param"], "kind", resp)
        # A general task on the repo project stays a plain directory task (no branch).
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", "G", "--spec", "s",
                                    "--assignee", "ibx", "--kind", "general", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        g = resp["data"]["id"]
        self.wait_queued(g)
        rc, st = self.harness.cli("task", "start", g, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        self.assertNotIn("branch", st["data"])
        d = self.show(g)
        self.assertIsNone(d["branch"])
        self.assertNotIn(os.path.realpath(d["workspace_path"]), self.worktrees())


if __name__ == "__main__":
    unittest.main()
