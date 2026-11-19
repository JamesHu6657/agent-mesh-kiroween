"""Regression tests for Round 7 review findings (P1-1, P1-2, P2-1)."""

import json
import os
import subprocess
import tempfile
import unittest

from mesh.adapters.vibe import VibeAdapter, _read_export
from mesh.daemon.handlers.run import (
    _remove_worktree_and_branch_with_roots,
    _resolve_integ_wt_from_values,
)


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


class TestVibeParseRedactionAndSymlink(unittest.TestCase):
    def test_vibe_parse_rejects_symlinked_export_and_redacts_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret_file = os.path.join(tmp, "secret.json")
            with open(secret_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "session_id": "secret-session-123",
                        "outcome": "completed",
                        "usage": {"api_key": "sk-test-secret-1234567890abcdef"},
                        "config": {"active_model": "secret-model"},
                    },
                    f,
                )

            run_dir = os.path.join(tmp, "run")
            vibe_out = os.path.join(run_dir, "vibe-out")
            os.makedirs(vibe_out, exist_ok=True)
            with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
                f.write("")
            export_link = os.path.join(vibe_out, "export.json")
            os.symlink(secret_file, export_link)

            res = VibeAdapter().parse(run_dir)
            self.assertIsNone(res.session_id)
            self.assertEqual(res.vendor_exit_meaning, "run_error")
            self.assertEqual(res.usage, {"error": "no export"})
            self.assertFalse(os.path.lexists(export_link))

    def test_vibe_parse_redacts_export_before_reading_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "run")
            vibe_out = os.path.join(run_dir, "vibe-out")
            os.makedirs(vibe_out, exist_ok=True)
            with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
                f.write("")
            secret_token = "sk-ant-api03-1234567890abcdefghijklmnop"
            with open(os.path.join(vibe_out, "export.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "session_id": "sess-1",
                        "outcome": "completed",
                        "usage": {"note": f"Bearer {secret_token}"},
                        "config": {"active_model": "devstral"},
                    },
                    f,
                )

            res = VibeAdapter().parse(run_dir)
            self.assertIsNotNone(res.usage)
            self.assertNotIn(secret_token, res.usage.get("note", ""))

    def test_read_export_rejects_symlink_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target.json")
            with open(target, "w", encoding="utf-8") as f:
                json.dump({"session_id": "leak"}, f)
            link = os.path.join(tmp, "export.json")
            os.symlink(target, link)
            self.assertIsNone(_read_export(link))


class TestResolveIntegWtTrust(unittest.TestCase):
    def test_resolve_integ_wt_rejects_symlinked_ancestor_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "workspaces")
            outside = os.path.join(tmp, "outside_proj")
            os.makedirs(root, exist_ok=True)
            os.makedirs(os.path.join(outside, "integration"), exist_ok=True)
            os.symlink(outside, os.path.join(root, "proj-1"))

            resolved = _resolve_integ_wt_from_values(
                [root], "proj-1", None, None, matched_root=root
            )
            self.assertIsNone(resolved)

    def test_resolve_integ_wt_rejects_symlinked_repo_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "workspaces")
            os.makedirs(root, exist_ok=True)
            real_repo = os.path.join(tmp, "real_repo")
            os.makedirs(real_repo, exist_ok=True)
            _git(real_repo, "init", "-b", "main")
            _git(real_repo, "config", "user.email", "test@example.com")
            _git(real_repo, "config", "user.name", "Test")
            with open(os.path.join(real_repo, "README.md"), "w", encoding="utf-8") as f:
                f.write("hi\n")
            _git(real_repo, "add", "README.md")
            _git(real_repo, "commit", "-m", "init")

            link_parent = os.path.join(tmp, "link_parent")
            os.symlink(real_repo, link_parent)

            resolved = _resolve_integ_wt_from_values(
                [root], "proj-1", link_parent, "main"
            )
            self.assertIsNone(resolved)


class TestRemoveWorktreeDetachedHead(unittest.TestCase):
    def _setup_repo_and_worktree(self, tmp: str):
        root = os.path.join(tmp, "workspaces")
        project_id = "proj-1"
        task_id = "task-1"
        integ_wt = os.path.join(root, project_id, "integration")
        wt_path = os.path.join(root, project_id, "tasks", task_id)
        os.makedirs(integ_wt, exist_ok=True)
        os.makedirs(os.path.dirname(wt_path), exist_ok=True)

        _git(integ_wt, "init", "-b", "main")
        _git(integ_wt, "config", "user.email", "test@example.com")
        _git(integ_wt, "config", "user.name", "Test")
        with open(os.path.join(integ_wt, "base.txt"), "w", encoding="utf-8") as f:
            f.write("base\n")
        _git(integ_wt, "add", "base.txt")
        _git(integ_wt, "commit", "-m", "base")

        _git(integ_wt, "worktree", "add", "-b", "task-branch", wt_path, "HEAD")
        return root, project_id, task_id, integ_wt, wt_path

    def test_remove_worktree_refuses_detached_head_with_unmerged_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, project_id, task_id, integ_wt, wt_path = self._setup_repo_and_worktree(tmp)

            _git(wt_path, "checkout", "--detach")
            with open(os.path.join(wt_path, "unmerged.txt"), "w", encoding="utf-8") as f:
                f.write("important work\n")
            _git(wt_path, "add", "unmerged.txt")
            _git(wt_path, "commit", "-m", "unmerged detached commit")

            task = {
                "id": task_id,
                "project_id": project_id,
                "workspace_path": wt_path,
                "branch": "task-branch",
                "status": "merged",
            }
            err = _remove_worktree_and_branch_with_roots([root], task, integ_wt)
            self.assertEqual(err, "worktree_remove_failed")
            self.assertTrue(os.path.isdir(wt_path))

    def test_remove_worktree_allows_merged_worktree_on_merged_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, project_id, task_id, integ_wt, wt_path = self._setup_repo_and_worktree(tmp)

            task = {
                "id": task_id,
                "project_id": project_id,
                "workspace_path": wt_path,
                "branch": "task-branch",
                "status": "merged",
            }
            err = _remove_worktree_and_branch_with_roots([root], task, integ_wt)
            self.assertIsNone(err)
            self.assertFalse(os.path.lexists(wt_path))


if __name__ == "__main__":
    unittest.main()
