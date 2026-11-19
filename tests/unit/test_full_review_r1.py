"""Full-codebase review r1 (main @ 01f1313) regressions.

P0-1: a merge is bound to the reviewed head (``deliverable.git.head``).
P1-1: redaction keeps JSONL lines valid and catches JSON/URL credentials.
P1-2: exact-value labels of non-identifier env names stay verifiable in stored op_json.
P1-3: reverse I9: no false positive on dependency chains; fast-forwarded work is reported.
P1-4: a deleted task worktree (branch kept) merges, and a retry re-attaches the branch.
"""

import json
import os
import unittest
from unittest import mock

from mesh.core.db import transaction
from mesh.daemon import merge
from tests.unit.test_batch7_pr45_r4 import _MergeFixtureBase
from tests.unit.test_carbon_merge_gate import TestReverseI9

# Runs the merge pipeline (real /proc scans).
MESH_PROC_LOCK = "shared"


class TestP0_1MergeBoundToReviewedHead(_MergeFixtureBase):
    BRANCH = "mesh/prj_1/tsk_h"

    def test_reviewed_head_from_deliverable(self) -> None:
        sha = "a" * 40
        rec = {"deliverable": {"summary": "s", "git": {"head": sha}}}
        self.assertEqual(merge.reviewed_head(json.dumps(rec)), sha)
        self.assertIsNone(merge.reviewed_head(None))
        self.assertIsNone(merge.reviewed_head("not json"))
        self.assertIsNone(merge.reviewed_head(json.dumps({"deliverable": {"git": {"head": "HEAD"}}})))
        self.assertIsNone(merge.reviewed_head(json.dumps({"deliverable": {}})))

    def test_branch_moved_after_review_is_refused(self) -> None:
        reviewed = self._make_branch(self.BRANCH, {"a.txt": "a\n"})
        self.fixture.git(self.repo, "checkout", "-q", self.BRANCH)
        with open(os.path.join(self.repo, "late.txt"), "w", encoding="utf-8") as f:
            f.write("unreviewed\n")
        self.fixture.git(self.repo, "add", "late.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "after accept")
        late = self.fixture.head(self.repo)
        self.fixture.git(self.repo, "checkout", "-q", "main")
        plan = self._plan("run_h1", "tsk_h", self.BRANCH, self.base)
        plan["expected_head"] = reviewed
        out = merge.run_merge_pipeline(plan)
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "worktree_dirty"), out)
        self.assertIs(out["details"]["head_mismatch"], True)
        self.assertEqual(out["details"]["actual_head"], late)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)

    def test_detached_head_work_never_merges_the_stale_branch(self) -> None:
        """Work committed on a detached HEAD is what submit recorded; the branch did not move."""
        self._make_branch(self.BRANCH, {"a.txt": "a\n"})
        self.fixture.git(self.repo, "checkout", "-q", "--detach", self.BRANCH)
        with open(os.path.join(self.repo, "d.txt"), "w", encoding="utf-8") as f:
            f.write("detached\n")
        self.fixture.git(self.repo, "add", "d.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "detached")
        detached = self.fixture.head(self.repo)
        self.fixture.git(self.repo, "checkout", "-q", "main")
        plan = self._plan("run_h2", "tsk_h", self.BRANCH, self.base)
        plan["expected_head"] = detached
        out = merge.run_merge_pipeline(plan)
        self.assertEqual(out["outcome"], "failed", out)
        self.assertIs(out["details"]["head_mismatch"], True)

    def test_unrecorded_head_is_refused(self) -> None:
        self._make_branch(self.BRANCH, {"a.txt": "a\n"})
        plan = self._plan("run_h3", "tsk_h", self.BRANCH, self.base)
        plan["expected_head"] = None
        out = merge.run_merge_pipeline(plan)
        self.assertEqual(out["outcome"], "failed", out)
        self.assertIs(out["details"]["head_mismatch"], True)

    def test_reviewed_head_merges(self) -> None:
        reviewed = self._make_branch(self.BRANCH, {"a.txt": "a\n"})
        plan = self._plan("run_h4", "tsk_h", self.BRANCH, self.base)
        plan["expected_head"] = reviewed
        out = merge.run_merge_pipeline(plan)
        self.assertEqual(out["outcome"], "merged", out)

    def test_p1_4_deleted_worktree_still_merges_the_kept_branch(self) -> None:
        reviewed = self._make_branch(self.BRANCH, {"a.txt": "a\n"})
        plan = self._plan("run_h5", "tsk_h", self.BRANCH, self.base)
        plan["worktree"] = os.path.join(self.fixture.base, "deleted-worktree")
        plan["expected_head"] = reviewed
        out = merge.run_merge_pipeline(plan)
        self.assertEqual(out["outcome"], "merged", out)


class TestP1_2NonIdentifierEnvLabels(unittest.TestCase):
    def test_label_name(self) -> None:
        from mesh.core.redact import _label_name
        self.assertEqual(_label_name("npm_config_//registry.npmjs.org/:_authToken"),
                         "npm_config___registry_npmjs_org___authToken")
        self.assertEqual(_label_name("my-secret.token"), "my_secret_token")
        self.assertEqual(_label_name("A]B\nC"), "A_B_C")
        self.assertEqual(_label_name("1KEY"), "_1KEY")
        self.assertEqual(_label_name("API_KEY"), "API_KEY")

    def test_stored_op_json_with_such_a_label_verifies(self) -> None:
        from mesh.core import redact as redact_mod
        from mesh.core.approvals import stored_op_json, verify_stored_op_json
        from mesh.core.canonical import compute_op_hash

        value = "Zq9vX2mK8pL4"
        env = {"npm_config_//registry.npmjs.org/:_authToken": value, "my-secret-token": "Yw7uT5rE3qA1"}
        pairs = tuple(redact_mod.collect_exact_values(env=env))
        self.assertTrue(all(name.replace("_", "a").isalnum() for name, _ in pairs), pairs)
        with mock.patch.object(redact_mod, "_env_cache", pairs):
            op_doc, op_hash = compute_op_hash(op="project.update", requested_by="agt_1",
                                              params={"goal": f"password: {value} and Yw7uT5rE3qA1"},
                                              project_id="prj_1", category="scope_change")
            stored = stored_op_json(op_doc, op_hash)
            self.assertNotIn(value, stored)
            self.assertNotIn("Yw7uT5rE3qA1", stored)
            row = {"op_json": stored, "op_type": "project.update", "requested_by": "agt_1",
                   "project_id": "prj_1", "category": "scope_change"}
            self.assertEqual(verify_stored_op_json(row), op_hash)
        self.assertEqual(verify_stored_op_json(row), op_hash)  # after a restart without that env


class TestP1_3ReverseI9(unittest.TestCase):
    setUp = TestReverseI9.setUp
    tearDown = TestReverseI9.tearDown
    _create_repo_project = TestReverseI9._create_repo_project
    make_task = TestReverseI9.make_task
    merge_into_integration = TestReverseI9.merge_into_integration
    reverse_violations = TestReverseI9.reverse_violations

    def _record_merged(self, task_id: str, mc: str) -> None:
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET merge_json = ? WHERE id = ?;",
                              (json.dumps({"merge_commit": mc}), task_id))

    def test_dependency_chain_is_not_reported(self) -> None:
        self._create_repo_project()
        up = self.make_task("tsk_1", "merged", True)
        down = self.make_task("tsk_2", "cancelled", False)
        wt = self.fixture.path("wt-down")
        self.fixture.git(self.repo, "worktree", "add", wt, down)
        self.fixture.git(wt, "merge", "--no-ff", "-m", "pull upstream", up, identity=True)
        with open(os.path.join(wt, "down.txt"), "w", encoding="utf-8") as f:
            f.write("own\n")
        self.fixture.git(wt, "add", "-A")
        self.fixture.git(wt, "commit", "-m", "own work", identity=True)
        self.fixture.git(self.repo, "worktree", "remove", wt)
        self._record_merged("tsk_1", self.merge_into_integration(up))
        self.assertEqual(self.reverse_violations(), [])
        from mesh.core.invariants import check_i9_integrated_unmerged_tasks
        self.assertEqual(check_i9_integrated_unmerged_tasks(self.conn, "prj_x"), [])

    def test_fast_forwarded_work_is_reported(self) -> None:
        self._create_repo_project()
        branch = self.make_task("tsk_ff", "cancelled", True)
        tip = self.fixture.head(self.repo, branch)
        self.fixture.git(self.repo, "branch", "-f", self.integration, tip)
        got = self.reverse_violations()
        self.assertEqual([v.details["task_id"] for v in got], ["tsk_ff"])
        self.assertEqual(got[0].details["merge_commit"], tip)

    def test_zero_commit_task_is_not_reported(self) -> None:
        self._create_repo_project()
        self.make_task("tsk_z", "cancelled", False)
        self.assertEqual(self.reverse_violations(), [])


class TestP1_4ReattachDeletedWorkspace(unittest.TestCase):
    def test_retry_reattaches_kept_branch(self) -> None:
        from mesh.core import workspaces
        from tests.gitfixture import GitFixture

        fx = GitFixture()
        self.addCleanup(fx.cleanup)
        env_patch = mock.patch.dict(os.environ, fx.git_env_vars())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        repo = fx.make_k0("R1P14")
        branch = "mesh/prj_1/tsk_w"
        path = fx.path("ws", "tasks", "tsk_w")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fx.git(repo, "worktree", "add", "-b", branch, path, "main")
        with open(os.path.join(path, "w.txt"), "w", encoding="utf-8") as f:
            f.write("kept\n")
        fx.git(path, "add", "w.txt")
        fx.git(path, "commit", "-q", "-m", "work", identity=True)
        tip = fx.head(path)
        fx.git(repo, "worktree", "remove", path)  # `workspace delete`: the dir goes, the branch stays
        plan = {"repo": repo, "workspace_path": path, "branch": branch, "task_id": "tsk_w",
                "project_id": "prj_1", "integration": "main", "base_commit": fx.head(repo, "main")}
        out = workspaces.run_code_workspace_plan(plan)
        self.assertEqual((out["workspace_path"], out["branch"], out["action"]), (path, branch, "reused"))
        self.assertEqual(fx.head(path), tip)
        self.assertEqual(fx.git(path, "rev-parse", "--symbolic-full-name", "HEAD").stdout.strip(),
                         f"refs/heads/{branch}")

    def test_missing_branch_is_still_workspace_missing(self) -> None:
        from mesh.core import workspaces
        from tests.gitfixture import GitFixture

        fx = GitFixture()
        self.addCleanup(fx.cleanup)
        env_patch = mock.patch.dict(os.environ, fx.git_env_vars())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        repo = fx.make_k0("R1P14b")
        plan = {"repo": repo, "workspace_path": fx.path("ws", "tasks", "tsk_g"), "branch": "mesh/prj_1/tsk_g",
                "task_id": "tsk_g", "project_id": "prj_1", "integration": "main", "base_commit": None}
        with self.assertRaises(workspaces.WorkspaceError) as cm:
            workspaces.run_code_workspace_plan(plan)
        self.assertEqual(cm.exception.details.get("reason"), "workspace_missing")


class TestP1_1JsonlRedaction(unittest.TestCase):
    EXACT = [("MY_API_TOKEN", "Qx7pL2mN9vB4")]

    def both(self, text: str) -> str:
        from mesh.core.redact import StreamRedactor, redact
        batch = redact(text, self.EXACT, jsonl=True)
        for size in (1, 3, 7, len(text) or 1):
            sr = StreamRedactor(self.EXACT, jsonl=True)
            got = "".join(sr.feed(text[i:i + size]) for i in range(0, len(text), size)) + sr.flush()
            self.assertEqual(got, batch, size)
        return batch

    def test_jsonl_events_stay_valid_and_redacted(self) -> None:
        lines = [
            {"type": "assistant", "text": "set password=hunter22 then"},
            {"type": "result", "result": "done, secret: s3cr3tValue"},
            {"type": "tool", "input": {"api_key": "AKfooBARbaz", "cmd": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz' x"}},
            {"type": "text", "text": "token sk-abcdefghijklmnopqrstuv and Qx7pL2mN9vB4"},
            {"type": "text", "text": "clone https://bob:pw12345678@github.com/r.git"},
            ["password:", "hunter33"],
            {"type": "text", "text": "keep it secret", "kind": "text"},
        ]
        raw = "".join(json.dumps(x) + "\n" for x in lines)
        out = self.both(raw)
        for secret in ("hunter22", "s3cr3tValue", "AKfooBARbaz", "abcdefghijklmnopqrstuvwxyz",
                       "sk-abcdefghijklmnopqrstuv", "Qx7pL2mN9vB4", "pw12345678", "hunter33"):
            self.assertNotIn(secret, out)
        events = [json.loads(ln) for ln in out.splitlines()]
        self.assertEqual(len(events), len(lines))
        self.assertEqual(events[0]["type"], "assistant")
        self.assertEqual(events[2]["input"]["api_key"], "[REDACTED]")
        self.assertIn("[REDACTED:MY_API_TOKEN]", events[3]["text"])
        self.assertEqual(events[4]["text"], "clone https://bob:[REDACTED]@github.com/r.git")
        self.assertEqual(events[6], lines[6])  # nothing to redact: untouched

    def test_clean_jsonl_line_is_byte_identical(self) -> None:
        raw = '{"a":1,  "b":"x"}\n'
        self.assertEqual(self.both(raw), raw)

    def test_value_spanning_json_syntax_falls_back_to_line_rules(self) -> None:
        exact = [("X_SECRET", 'ab","c":"de')]
        from mesh.core.redact import redact
        raw = '{"a":"ab","c":"de"}\n'
        out = redact(raw, exact, jsonl=True)
        self.assertNotIn('ab","c":"de', out)

    def test_key_block_in_json_is_redacted(self) -> None:
        raw = json.dumps(["-----BEGIN PRIVATE KEY-----", "MIIEvQIBADANBg", "-----END PRIVATE KEY-----"]) + "\n"
        self.assertNotIn("MIIEvQIBADANBg", self.both(raw))

    def test_default_rules_unchanged_for_stored_values(self) -> None:
        from mesh.core.redact import redact
        self.assertEqual(redact("https://bob:pw12345678@h/x", []), "https://bob:pw12345678@h/x")
        self.assertEqual(redact('{"x":"password=abc"}', []), '{"x":"password=[REDACTED]')


class TestP1_5DeliverIntentRecovery(unittest.TestCase):
    """A crash after the base branch moved but before the deliver transaction committed."""

    setUp = TestReverseI9.setUp
    tearDown = TestReverseI9.tearDown
    _create_repo_project = TestReverseI9._create_repo_project

    def _advance_integration(self) -> tuple:
        old = self.fixture.head(self.repo, "main")
        wt = self.fixture.path("wt-integ")
        self.fixture.git(self.repo, "worktree", "add", wt, self.integration)
        with open(os.path.join(wt, "f.txt"), "w", encoding="utf-8") as f:
            f.write("f\n")
        self.fixture.git(wt, "add", "-A")
        self.fixture.git(wt, "commit", "-m", "work", identity=True)
        new = self.fixture.head(wt)
        self.fixture.git(self.repo, "worktree", "remove", wt)
        return old, new

    def _intent(self, home: str, old: str, new: str) -> str:
        from mesh.core.repos import write_deliver_intent
        return write_deliver_intent(home, {"project_id": "prj_x", "repo": self.repo, "base_branch": "main",
                                           "integration_branch": self.integration, "old": old, "new": new,
                                           "actor_id": "usr_1", "actor_kind": "user", "via": "direct",
                                           "relayed_by": None, "request_id": "req_d", "approval_id": None})

    def delivered(self):
        return self.conn.execute("SELECT payload_json FROM events WHERE type = 'project.delivered';").fetchall()

    def test_moved_base_without_event_is_recorded(self) -> None:
        from mesh.core.repos import deliver_to_base
        from mesh.daemon import recovery

        self._create_repo_project()
        old, new = self._advance_integration()
        home = self.fixture.path("home")
        os.makedirs(home)
        path = self._intent(home, old, new)
        deliver_to_base(self.repo, "main", self.integration, expected=(old, new))  # then "SIGKILL"
        counts = {}
        recovery._step_deliver_intents(self.conn, home, counts)
        self.assertEqual(counts.get("deliveries_recovered"), 1, counts)
        rows = self.delivered()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0][0])
        self.assertEqual((payload["old"], payload["new"], payload["recovered"]), (old, new, True))
        self.assertFalse(os.path.exists(path))
        self._intent(home, old, new)  # already recorded: dropped, no second event
        recovery._step_deliver_intents(self.conn, home, {})
        self.assertEqual(len(self.delivered()), 1)

    def test_unapplied_intent_is_dropped(self) -> None:
        from mesh.daemon import recovery

        self._create_repo_project()
        old, new = self._advance_integration()
        home = self.fixture.path("home")
        os.makedirs(home)
        path = self._intent(home, old, new)
        recovery._step_deliver_intents(self.conn, home, {})
        self.assertEqual(self.delivered(), [])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.fixture.head(self.repo, "main"), old)

    def test_moved_branches_refuse_a_stale_intent(self) -> None:
        from mesh.core.repos import deliver_to_base
        from mesh.errors import MeshError

        self._create_repo_project()
        old, new = self._advance_integration()
        with self.assertRaises(MeshError) as cm:
            deliver_to_base(self.repo, "main", self.integration, expected=(old, old))
        self.assertEqual(cm.exception.details["reason"], "base_moved")
        self.assertEqual(self.fixture.head(self.repo, "main"), old)


if __name__ == "__main__":
    unittest.main()
