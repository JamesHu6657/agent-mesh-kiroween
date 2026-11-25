"""Integration tests for `project deliver`, `workspace prune` and `workspace delete` (m2-r8).

Plan §10.6 (deliver to base), §10.7 (cleanup), checklist K10, K11, K12. Every repo is a K0
fixture under /tmp/mh-*-git and meshd runs with the fixture's hermetic git env
(GitMeshHarness). Projects use `--test-command none`; the executor is the inbox agent `ibx`
and the task commits are made by the test in the task worktree. Nothing is ever pushed: the
bare `origin` of the fixture is only ever read with `ls-remote`.

The harness teardown runs `doctor --check-invariants`, so I9 is checked live after every
scenario here (A8).

ACC: K10, K11, K12.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from tests.gitfixture import GitFixture, GitMeshHarness
from tests.helpers import restore_accessible_for_test, set_inaccessible_for_test
from tests.integration.test_execution_launch import create_agent
from tests.stubs import create_test_stubs


class TestDeliverPrune(unittest.TestCase):
    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.fixture = GitFixture()
        self.harness = GitMeshHarness(self.fixture, init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)  # doctor --check-invariants (I9 live), then fixture removal
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tok_ibx = create_agent(self.harness, "ibx")
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)
        self.repo, self.prj = self.make_project("R")
        self.remote_before = self.fixture.remote_refs(self.repo)

    # --- helpers -----------------------------------------------------------------

    def make_project(self, name: str) -> Tuple[str, str]:
        repo = self.fixture.make_k0(name)
        if not hasattr(self, "repo_remote_refs"):
            self.repo_remote_refs = {}
        self.repo_remote_refs[repo] = self.fixture.remote_refs(repo)
        rc, resp = self.harness.cli("intake", "submit", "--title", f"Code {name}", "--goal", "g",
                                    "--criterion", "c1", "--repo", repo, "--base-branch", "main",
                                    "--test-command", "none", "--lead", "lead1", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        return repo, resp["data"]["id"]

    def integ_branch(self) -> str:
        return f"mesh/{self.prj}/integration"

    def integ_wt(self) -> str:
        return os.path.join(self.harness.home, "workspaces", self.prj, "integration")

    def start_task(self, title: str) -> Tuple[str, Dict[str, Any]]:
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", title,
                                    "--spec", "Work", "--assignee", "ibx", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        tsk = resp["data"]["id"]
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        return tsk, st["data"]

    def commit_file(self, ws: str, path: str, content: str, message: str) -> None:
        with open(os.path.join(ws, path), "w", encoding="utf-8") as f:
            f.write(content)
        self.fixture.git(ws, "add", path)
        self.fixture.git(ws, "commit", "-m", message, identity=True)

    def submit_accept(self, tsk: str, st: Dict[str, Any]) -> None:
        rc, sub = self.harness.cli("task", "submit", tsk, "--run", st["run_id"], "--fence", str(st["fence"]),
                                   "--summary", "done", token=self.tok_ibx)
        self.assertEqual(rc, 0, sub)
        rc, acc = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc, 0, acc)

    def accepted_task(self, title: str, path: str, content: str = "x\n") -> Tuple[str, str]:
        tsk, st = self.start_task(title)
        self.commit_file(st["workspace"], path, content, title)
        self.submit_accept(tsk, st)
        return tsk, st["workspace"]

    def merge(self, tsk: str) -> Tuple[int, Dict[str, Any]]:
        return self.harness.cli("task", "merge", tsk, token=self.tok_lead1, timeout=120.0)

    def deliver(self, token: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        return self.harness.cli("project", "deliver", self.prj, token=token or self.tok_lead1, timeout=120.0)

    def prune(self, dry_run: bool = False, token: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        argv = ["workspace", "prune", "--project", self.prj]
        if dry_run:
            argv.append("--dry-run")
        return self.harness.cli(*argv, token=token or self.tok_lead1, timeout=120.0)

    def q(self, sql: str, params: tuple = ()) -> List[Any]:
        conn = self.harness.db()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def assert_remote_unchanged(self) -> None:
        self.assertEqual(self.fixture.remote_refs(self.repo), self.remote_before)
        for repo, before in self.repo_remote_refs.items():
            self.assertEqual(self.fixture.remote_refs(repo), before, repo)

    # --- K10 ---------------------------------------------------------------------

    def test_k10_deliver_fast_forwards_then_refuses_non_ff(self) -> None:
        """ACC: K10 - deliver fast-forwards main to integration; a diverged main is rc 14 NOT_FAST_FORWARD."""
        tsk, _ = self.accepted_task("Add feature", "feature.txt", "feature\n")
        rc, merged = self.merge(tsk)
        self.assertEqual(rc, 0, merged)
        integ_head = self.fixture.head(self.repo, self.integ_branch())
        self.assertNotEqual(integ_head, self.fixture.head(self.repo, "main"))

        # 1. First deliver: rc 0, main fast-forwarded to the integration tip. R itself has
        #    `main` checked out, so §10.6 step 2 applies: merge --ff-only in that worktree.
        rc, out = self.deliver()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["data"]["method"], "merge_ff_only")
        self.assertEqual(os.path.realpath(out["data"]["worktree"]), os.path.realpath(self.repo))
        self.assertEqual(out["data"]["new"], integ_head)
        self.assertEqual(self.fixture.head(self.repo, "main"), integ_head)
        rows = self.q("SELECT payload_json FROM events WHERE type = 'project.delivered';")
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0][0])
        self.assertEqual(payload["new"], integ_head)
        self.assertEqual(payload["method"], "merge_ff_only")
        self.assertIn("old", payload)

        # 2. Deliver again with nothing new: rc 0, method "noop", no git write and no second
        #    event (review m2-r8 MINOR-2).
        rc, out = self.deliver()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["data"]["method"], "noop")
        self.assertEqual(out["data"]["old"], integ_head)
        self.assertEqual(out["data"]["new"], integ_head)
        self.assertEqual(self.fixture.head(self.repo, "main"), integ_head)
        self.assertEqual(len(self.q("SELECT 1 FROM events WHERE type = 'project.delivered';")), 1)

        # 3. A dry run reports the plan (fast_forward) and writes nothing (MINOR-5).
        rc, out = self.harness.cli("project", "deliver", self.prj, "--dry-run", token=self.tok_lead1,
                                   timeout=120.0)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["dry_run"])
        # server.py reports a dry run as `data.would.{transitions,events,effects}`, so the
        # plan travels as one transitions entry.
        would = out["data"]["would"]["transitions"]
        self.assertEqual(len(would), 1, would)
        self.assertEqual(would[0]["project_id"], self.prj)
        self.assertEqual(would[0]["base_branch"], "main")
        self.assertEqual(would[0]["integration_branch"], self.integ_branch())
        self.assertTrue(would[0]["fast_forward"])
        self.assertEqual(len(self.q("SELECT 1 FROM events WHERE type = 'project.delivered';")), 1)

        # 4. Advance main independently in R, then merge another task: main is no longer
        #    an ancestor of the integration branch, so deliver must refuse.
        with open(os.path.join(self.repo, "independent.txt"), "w", encoding="utf-8") as f:
            f.write("independent\n")
        self.fixture.git(self.repo, "add", "independent.txt")
        self.fixture.git(self.repo, "commit", "-m", "independent work on main", identity=True)
        main_after = self.fixture.head(self.repo, "main")
        self.assertNotEqual(main_after, integ_head)

        tsk2, _ = self.accepted_task("Second feature", "second.txt", "second\n")
        rc, merged2 = self.merge(tsk2)
        self.assertEqual(rc, 0, merged2)

        rc, out = self.deliver()
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, out)
        self.assertEqual(out["error"]["code"], c.NOT_FAST_FORWARD)
        # Nothing changed: main still points at the independent commit.
        self.assertEqual(self.fixture.head(self.repo, "main"), main_after)
        # Only the first deliver recorded an event.
        self.assertEqual(len(self.q("SELECT 1 FROM events WHERE type = 'project.delivered';")), 1)
        self.assert_remote_unchanged()

    def test_full_r1_p1_5_noop_deliver_needs_no_approval(self) -> None:
        """Full review r1 P1-5: nothing to deliver is a noop before the approval gate, so a
        single-use approval is never consumed (or requested) for it."""
        rc, resp = self.harness.cli("config", "set", "approval.policy.deliver_to_base", '"direct_or_relayed"',
                                    user=True)
        self.assertEqual(rc, 0, resp)
        rc, out = self.deliver()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["data"]["method"], "noop")
        self.assertEqual(self.q("SELECT id FROM approvals;"), [])
        self.assertEqual(len(self.q("SELECT 1 FROM events WHERE type = 'project.delivered';")), 0)

    def intents(self) -> List[str]:
        d = os.path.join(self.harness.home, "deliver-intents")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_full_r3_p1_4_retry_after_a_lost_transaction_records_the_delivery(self) -> None:
        """Full review r3 P1-4: the base moved but the request's transaction was lost while
        meshd stayed up (its intent is on disk); the retry finds base == integration and
        records that delivery instead of answering a bare noop."""
        from mesh.core.repos import write_deliver_intent

        tsk, _ = self.accepted_task("Add feature", "feature.txt", "feature\n")
        rc, merged = self.merge(tsk)
        self.assertEqual(rc, 0, merged)
        old = self.fixture.head(self.repo, "main")
        new = self.fixture.head(self.repo, self.integ_branch())
        horizon = self.q("SELECT MAX(seq) FROM events;")[0][0]
        write_deliver_intent(self.harness.home, {
            "project_id": self.prj, "repo": self.repo, "base_branch": "main",
            "integration_branch": self.integ_branch(), "old": old, "new": new, "actor_id": "usr_x",
            "actor_kind": "user", "via": "direct", "relayed_by": None, "request_id": "req_lost",
            "approval_id": None, "min_event_seq": horizon, "idempotency": None})
        self.fixture.git(self.repo, "update-ref", "refs/heads/main", new, old)  # the lost request's git write
        rc, out = self.deliver()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["data"]["method"], "noop")
        rows = self.q("SELECT payload_json, request_id FROM events WHERE type = 'project.delivered';")
        self.assertEqual(len(rows), 1, rows)
        payload = json.loads(rows[0][0])
        self.assertEqual((payload["old"], payload["new"], payload["method"]), (old, new, "recovered"))
        self.assertEqual(self.intents(), [])

    def test_full_r4_p1_1_lost_delivery_is_kept_when_the_next_one_fails(self) -> None:
        """Full review r4 P1-1: a lost delivery (A -> B, intent on disk) is settled in its
        own transaction before the next `project deliver`; that request failing (here
        NOT_FAST_FORWARD) no longer rolls the A -> B record back or drops its intent."""
        from mesh.core.repos import write_deliver_intent

        tsk, _ = self.accepted_task("Add feature", "feature.txt", "feature\n")
        rc, merged = self.merge(tsk)
        self.assertEqual(rc, 0, merged)
        old = self.fixture.head(self.repo, "main")
        new = self.fixture.head(self.repo, self.integ_branch())
        horizon = self.q("SELECT MAX(seq) FROM events;")[0][0]
        write_deliver_intent(self.harness.home, {
            "project_id": self.prj, "repo": self.repo, "base_branch": "main",
            "integration_branch": self.integ_branch(), "old": old, "new": new, "actor_id": "usr_x",
            "actor_kind": "user", "via": "direct", "relayed_by": None, "request_id": "req_lost",
            "approval_id": None, "min_event_seq": horizon, "idempotency": None})
        self.fixture.git(self.repo, "update-ref", "refs/heads/main", new, old)  # the lost git write
        tree = self.fixture.git(self.repo, "rev-parse", old + "^{tree}").stdout.strip()
        side = self.fixture.git(self.repo, "commit-tree", tree, "-p", old, "-m", "side",
                                identity=True).stdout.strip()
        self.fixture.git(self.repo, "update-ref", "refs/heads/" + self.integ_branch(), side, new)
        rc, out = self.deliver()
        self.assertNotEqual(rc, 0, out)
        rows = self.q("SELECT payload_json FROM events WHERE type = 'project.delivered';")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(json.loads(rows[0][0])["new"], new)
        self.assertEqual(self.intents(), [])
        self.fixture.git(self.repo, "update-ref", "refs/heads/" + self.integ_branch(), new, side)  # I9

    def test_full_r5_p1_3_merge_never_runs_git_in_a_repointed_worktree(self) -> None:
        """Full review r5 P1-3: a task worktree whose `.git` now points at a task-made
        repository (with `core.fsmonitor`) is refused before any git runs in it."""
        tsk, ws = self.accepted_task("Add feature", "feature.txt", "feature\n")
        dot_git = os.path.join(ws, ".git")
        with open(dot_git) as f:
            saved = f.read()
        fake = os.path.join(ws, "sub")
        self.fixture.git(ws, "init", "-q", fake)
        marker = os.path.join(ws, "PWNED")
        with open(os.path.join(fake, ".git", "config"), "a") as f:
            f.write("[core]\n\tfsmonitor = touch %s\n" % marker)
        if sys.platform == "win32" and os.path.exists(dot_git):
            os.chmod(dot_git, 0o600)
            os.unlink(dot_git)
        with open(dot_git, "w") as f:
            f.write("gitdir: sub/.git\n")
        try:
            rc, out = self.merge(tsk)
        finally:
            if sys.platform == "win32" and os.path.exists(dot_git):
                os.chmod(dot_git, 0o600)
                os.unlink(dot_git)
            with open(dot_git, "w") as f:
                f.write(saved)
        self.assertFalse(os.path.exists(marker))
        self.assertIn("foreign_repository", json.dumps(out), out)
        self.assertNotEqual(self.q("SELECT status FROM tasks WHERE id = ?;", (tsk,))[0][0], "merged")

    def test_full_r5_p2_1_unreadable_intent_is_kept(self) -> None:
        """Full review r5 P2-1: an intent that cannot be read right now is kept (the
        request fails), never deleted as if it were corrupt."""
        from mesh.core.repos import deliver_intent_path

        tsk, _ = self.accepted_task("Add feature", "feature.txt", "feature\n")
        rc, merged = self.merge(tsk)
        self.assertEqual(rc, 0, merged)
        path = deliver_intent_path(self.harness.home, self.prj)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{}")
        set_inaccessible_for_test(path)
        if sys.platform != "win32" and os.access(path, os.R_OK):
            os.unlink(path)
            self.skipTest("running as root")
        try:
            rc, out = self.deliver()
            self.assertNotEqual(rc, 0, out)
            self.assertTrue(os.path.lexists(path))
            self.assertEqual(self.q("SELECT COUNT(*) FROM events WHERE type = 'project.delivered';")[0][0], 0)
        finally:
            restore_accessible_for_test(path)
            os.unlink(path)

    def test_full_r2_p1_d_no_intent_outlives_its_request(self) -> None:
        """Full review r2 P1-D: a failed deliver (dirty base worktree, nothing moved) and a
        committed one both leave no deliver intent behind for a later start to replay."""
        tsk, _ = self.accepted_task("Add feature", "feature.txt", "feature\n")
        rc, merged = self.merge(tsk)
        self.assertEqual(rc, 0, merged)
        with open(os.path.join(self.repo, "calc.py"), "a", encoding="utf-8") as f:
            f.write("dirty\n")
        main_before = self.fixture.head(self.repo, "main")
        rc, out = self.deliver()
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, out)
        self.assertEqual(self.fixture.head(self.repo, "main"), main_before)
        self.assertEqual(self.intents(), [])
        self.fixture.git(self.repo, "checkout", "--", "calc.py")
        rc, out = self.deliver()
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(out["data"]["method"], "noop")
        self.assertEqual(self.intents(), [])

    def test_full_r2_p2_b_unreadable_heads_fail_before_the_approval_gate(self) -> None:
        """Full review r2 P2-B: heads that cannot be read never fall back to an unpinned
        deliver without intent; the request fails before any approval is touched."""
        rc, resp = self.harness.cli("config", "set", "approval.policy.deliver_to_base", '"direct_or_relayed"',
                                    user=True)
        self.assertEqual(rc, 0, resp)
        tsk, _ = self.accepted_task("Add feature", "feature.txt", "feature\n")
        rc, merged = self.merge(tsk)
        self.assertEqual(rc, 0, merged)
        ref = f"refs/heads/{self.integ_branch()}"
        sha = self.fixture.head(self.repo, self.integ_branch())
        self.fixture.git(self.repo, "update-ref", "-d", ref)
        try:
            rc, out = self.deliver()
        finally:
            self.fixture.git(self.repo, "update-ref", ref, sha)
        self.assertEqual(rc, c.EXIT_INTERNAL, out)
        self.assertEqual(out["error"]["details"]["reason"], "git_failed")
        self.assertEqual(self.q("SELECT id FROM approvals;"), [])
        self.assertEqual(self.intents(), [])

    def test_k10_deliver_through_a_worktree_holding_the_base(self) -> None:
        """ACC: K10 (partial) - a worktree with the base checked out gets merge --ff-only; a dirty one is rc 14."""
        tsk, _ = self.accepted_task("Add feature", "feature.txt", "feature\n")
        rc, merged = self.merge(tsk)
        self.assertEqual(rc, 0, merged)
        integ_head = self.fixture.head(self.repo, self.integ_branch())

        # Move R off `main` (a fixture branch, never a Mesh branch) and give `main` its own
        # worktree, so §10.6 step 2 is the path taken.
        self.fixture.git(self.repo, "checkout", "-b", "scratch")
        wt = self.fixture.path("R-main-wt")
        self.fixture.git(self.repo, "worktree", "add", wt, "main")

        # A dirty base worktree is refused with details.worktree and nothing changes.
        with open(os.path.join(wt, "dirty.txt"), "w", encoding="utf-8") as f:
            f.write("dirty\n")
        main_before = self.fixture.head(self.repo, "main")
        rc, out = self.deliver()
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, out)
        self.assertEqual(out["error"]["code"], c.WORKTREE_DIRTY)
        self.assertEqual(os.path.realpath(out["error"]["details"]["worktree"]), os.path.realpath(wt))
        self.assertEqual(self.fixture.head(self.repo, "main"), main_before)

        # Clean it: deliver now fast-forwards through that worktree.
        os.remove(os.path.join(wt, "dirty.txt"))
        rc, out = self.deliver()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["data"]["method"], "merge_ff_only")
        self.assertEqual(os.path.realpath(out["data"]["worktree"]), os.path.realpath(wt))
        self.assertEqual(self.fixture.head(self.repo, "main"), integ_head)
        self.assertEqual(self.fixture.head(wt), integ_head)

        # With no worktree holding `main`, §10.6 step 3 applies: a compare-and-swap
        # update-ref. Merge another task and deliver again.
        self.fixture.git(self.repo, "worktree", "remove", wt)
        tsk2, _ = self.accepted_task("Second feature", "second.txt", "second\n")
        rc, merged2 = self.merge(tsk2)
        self.assertEqual(rc, 0, merged2)
        integ_head2 = self.fixture.head(self.repo, self.integ_branch())
        self.assertNotEqual(integ_head2, integ_head)
        rc, out = self.deliver()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["data"]["method"], "update_ref")
        self.assertIsNone(out["data"]["worktree"])
        self.assertEqual(out["data"]["old"], integ_head)
        self.assertEqual(out["data"]["new"], integ_head2)
        self.assertEqual(self.fixture.head(self.repo, "main"), integ_head2)
        self.assert_remote_unchanged()

    def test_k10_tag_shadowing_the_integration_branch(self) -> None:
        """ACC: K10 (r8 N-1) - a tag named like the integration branch: the worktree path merges the integration sha, and main lands on it."""
        tsk, _ = self.accepted_task("Add feature", "feature.txt", "feature\n")
        rc, merged = self.merge(tsk)
        self.assertEqual(rc, 0, merged)
        integ_head = self.fixture.head(self.repo, self.integ_branch())
        main_before = self.fixture.head(self.repo, "main")
        self.assertNotEqual(integ_head, main_before)
        # refs/tags/<integration branch name> at the old main: by name, git would pick the tag.
        self.fixture.git(self.repo, "tag", self.integ_branch(), main_before)
        self.fixture.git(self.repo, "checkout", "-b", "scratch")
        wt = self.fixture.path("R-main-wt-tag")
        self.fixture.git(self.repo, "worktree", "add", wt, "main")
        rc, out = self.deliver()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["data"]["method"], "merge_ff_only")
        self.assertEqual(out["data"]["new"], integ_head)
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), integ_head)
        self.assertEqual(self.fixture.head(wt), integ_head)
        self.assertEqual(self.fixture.git(self.repo, "rev-parse", f"refs/tags/{self.integ_branch()}").stdout.strip(),
                         main_before)  # the tag is left alone
        self.assert_remote_unchanged()

    # --- K11 ---------------------------------------------------------------------

    def test_k11_no_push_across_intake_merge_deliver_prune(self) -> None:
        """ACC: K11 - repo intake -> merge -> deliver -> prune leaves the remote refs unchanged."""
        self.assertEqual(self.fixture.remote_refs(self.repo), self.remote_before)
        tsk, ws = self.accepted_task("Add feature", "feature.txt", "feature\n")
        rc, merged = self.merge(tsk)
        self.assertEqual(rc, 0, merged)
        self.assert_remote_unchanged()
        rc, out = self.deliver()
        self.assertEqual(rc, 0, out)
        self.assert_remote_unchanged()
        rc, out = self.prune()
        self.assertEqual(rc, 0, out)
        self.assertEqual([r["task_id"] for r in out["data"]["removed"]], [tsk])
        self.assertFalse(os.path.isdir(ws))
        # The bare origin still holds exactly what it held before the project existed.
        self.assert_remote_unchanged()

    # --- K12 ---------------------------------------------------------------------

    def test_k12_prune_and_delete(self) -> None:
        """ACC: K12 - prune removes only merged/commit-less-cancelled worktrees; delete is gated."""
        # 1. A merged task: prunable.
        merged_tsk, merged_ws = self.accepted_task("Merged work", "merged.txt", "merged\n")
        rc, out = self.merge(merged_tsk)
        self.assertEqual(rc, 0, out)

        # 2. A cancelled task with no commits beyond base_commit: prunable.
        cancelled_tsk, cancelled_st = self.start_task("Cancelled work")
        rc, out = self.harness.cli("task", "cancel", cancelled_tsk, "--reason", "not needed",
                                   token=self.tok_lead1)
        self.assertEqual(rc, 0, out)
        cancelled_ws = cancelled_st["workspace"]

        # 3. An accepted but unmerged task: never pruned, never deleted automatically.
        unmerged_tsk, unmerged_ws = self.accepted_task("Unmerged work", "unmerged.txt", "unmerged\n")

        # 4. A cancelled task WITH a commit beyond base_commit: not prunable.
        committed_tsk, committed_st = self.start_task("Cancelled with work")
        self.commit_file(committed_st["workspace"], "kept.txt", "kept\n", "work in progress")
        rc, out = self.harness.cli("task", "cancel", committed_tsk, "--reason", "abandoned",
                                   token=self.tok_lead1)
        self.assertEqual(rc, 0, out)
        committed_ws = committed_st["workspace"]

        # Dry run lists the two prunable tasks and removes nothing. A dry run reports
        # through `data.would` (server.py rolls the transaction back).
        rc, out = self.prune(dry_run=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["dry_run"])
        would = out["data"]["would"]["transitions"]
        self.assertEqual(sorted(t["task_id"] for t in would), sorted([merged_tsk, cancelled_tsk]))
        self.assertTrue(all(t["action"] == "prune" for t in would))
        self.assertTrue(os.path.isdir(merged_ws))
        self.assertTrue(os.path.isdir(cancelled_ws))
        self.assertTrue(os.path.isdir(unmerged_ws))
        self.assertTrue(os.path.isdir(committed_ws))

        # The real prune removes exactly those two worktrees and their branches.
        rc, out = self.prune()
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(r["task_id"] for r in out["data"]["removed"]),
                         sorted([merged_tsk, cancelled_tsk]))
        self.assertFalse(os.path.isdir(merged_ws))
        self.assertFalse(os.path.isdir(cancelled_ws))
        self.assertTrue(os.path.isdir(unmerged_ws))
        self.assertTrue(os.path.isdir(committed_ws))
        branches = self.fixture.branches(self.repo, "mesh/*")
        self.assertNotIn(f"mesh/{self.prj}/{merged_tsk}", branches)
        self.assertNotIn(f"mesh/{self.prj}/{cancelled_tsk}", branches)
        self.assertIn(f"mesh/{self.prj}/{unmerged_tsk}", branches)
        self.assertIn(f"mesh/{self.prj}/{committed_tsk}", branches)
        # The integration branch and its worktree are untouched.
        self.assertIn(self.integ_branch(), branches)
        self.assertTrue(os.path.isdir(self.integ_wt()))

        # A second prune is a no-op: already-pruned tasks are done, not skipped with a
        # failure reason (review m2-r8 MINOR-3). The cancelled task that still holds a
        # commit beyond base_commit stays a legitimate skip.
        rc, out = self.prune()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["data"]["removed"], [])
        self.assertEqual([(s["task_id"], s["reason"]) for s in out["data"]["skipped"]],
                         [(committed_tsk, "unmerged_commits")])

        # Nothing was deleted automatically: the unmerged task's dir and branch are intact.
        self.assertTrue(os.path.isdir(unmerged_ws))
        self.assertTrue(os.path.isfile(os.path.join(unmerged_ws, "unmerged.txt")))

        # `workspace delete` by the lead is gated by the `delete` category: rc 7.
        rc, out = self.harness.cli("workspace", "delete", unmerged_tsk, token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED, out)
        self.assertEqual(out["error"]["code"], c.APPROVAL_REQUIRED)
        self.assertEqual(out["error"]["details"]["category"], "delete")
        self.assertTrue(os.path.isdir(unmerged_ws))

        # The user acts directly: the dir goes, the branch is kept (Q7).
        rc, out = self.harness.cli("workspace", "delete", unmerged_tsk, user=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["data"]["deleted"])
        self.assertTrue(out["data"]["branch_kept"])
        self.assertFalse(os.path.isdir(unmerged_ws))
        self.assertIn(f"mesh/{self.prj}/{unmerged_tsk}", self.fixture.branches(self.repo, "mesh/*"))
        self.assertEqual(len(self.q("SELECT 1 FROM events WHERE type = 'workspace.deleted';")), 1)
        self.assert_remote_unchanged()

    def test_k12_delete_refuses_a_task_with_a_lease(self) -> None:
        """ACC: K12 (partial) - `workspace delete` on a task whose run holds its lease is rc 6."""
        tsk, st = self.start_task("Running work")
        rc, out = self.harness.cli("workspace", "delete", tsk, user=True)
        self.assertEqual(rc, c.EXIT_CONFLICT, out)
        self.assertEqual(out["error"]["code"], c.RUN_STILL_ACTIVE)
        self.assertTrue(os.path.isdir(st["workspace"]))

    def test_k12_unscoped_prune_is_refused_for_agents(self) -> None:
        """ACC: K12 (partial) - an agent may not prune without --project; only the user may.

        Review m2-r8 MAJOR-1: `workspace prune` with no `--project` reaches every project's
        worktrees, so an agent must name its project (rc 2 `INVALID_ARGUMENT`,
        `details.param: "project"`). A lead of project A is refused too, and B's worktree
        survives.
        """
        merged_tsk, merged_ws = self.accepted_task("Merged work", "merged.txt", "merged\n")
        rc, out = self.merge(merged_tsk)
        self.assertEqual(rc, 0, out)

        # A second project B with its own merged task: its worktree must survive every
        # refused unscoped prune below.
        repo_b, prj_b = self.make_project("B")
        rc, resp = self.harness.cli("task", "create", "--project", prj_b, "--title", "B work",
                                    "--spec", "Work", "--assignee", "ibx", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        tsk_b = resp["data"]["id"]
        rc, st_b = self.harness.cli("task", "start", tsk_b, token=self.tok_ibx)
        self.assertEqual(rc, 0, st_b)
        self.commit_file(st_b["data"]["workspace"], "b.txt", "b\n", "B work")
        self.submit_accept(tsk_b, st_b["data"])
        rc, out = self.harness.cli("task", "merge", tsk_b, token=self.tok_lead1, timeout=120.0)
        self.assertEqual(rc, 0, out)
        ws_b = st_b["data"]["workspace"]
        self.assertTrue(os.path.isdir(ws_b))

        for token in (self.tok_ibx, self.tok_grok, self.tok_lead1):
            rc, out = self.harness.cli("workspace", "prune", token=token, timeout=120.0)
            self.assertEqual(rc, c.EXIT_USAGE, out)
            self.assertEqual(out["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(out["error"]["details"]["param"], "project")
            self.assertTrue(os.path.isdir(merged_ws))
            self.assertTrue(os.path.isdir(ws_b))
        self.assertEqual(len(self.q("SELECT 1 FROM events WHERE type = 'workspace.pruned';")), 0)

        # The user may prune unscoped; the lead may prune its own project.
        rc, out = self.harness.cli("workspace", "prune", "--project", self.prj, token=self.tok_lead1,
                                   timeout=120.0)
        self.assertEqual(rc, 0, out)
        self.assertEqual([r["task_id"] for r in out["data"]["removed"]], [merged_tsk])
        self.assertFalse(os.path.isdir(merged_ws))
        self.assertTrue(os.path.isdir(ws_b))
        self.assert_remote_unchanged()

    def test_k12_delete_refuses_a_path_outside_the_workspaces_root(self) -> None:
        """ACC: K12 (partial) - a task dir that resolves outside `workspaces.root` is refused.

        Review m2-r8 MINOR-1: the containment check must fail loudly (rc != 0,
        `details.reason: "outside_workspaces_root"`), delete nothing and record no event.
        """
        tsk, st = self.start_task("Symlinked work")
        ws = st["workspace"]
        # Cancel first: a live lease would be refused with RUN_STILL_ACTIVE before the
        # containment check is reached.
        rc, out = self.harness.cli("task", "cancel", tsk, "--reason", "not needed", token=self.tok_lead1)
        self.assertEqual(rc, 0, out)
        outside = tempfile.mkdtemp(prefix="mh-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        with open(os.path.join(outside, "keep.txt"), "w", encoding="utf-8") as f:
            f.write("keep\n")
        # Swap the task dir for a symlink pointing outside the workspaces root.
        shutil.rmtree(ws)
        os.symlink(outside, ws)

        rc, out = self.harness.cli("workspace", "delete", tsk, user=True)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(out["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(out["error"]["details"]["reason"], "outside_workspaces_root")
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.txt")))
        self.assertEqual(len(self.q("SELECT 1 FROM events WHERE type = 'workspace.deleted';")), 0)


if __name__ == "__main__":
    unittest.main()
