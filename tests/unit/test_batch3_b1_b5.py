"""Unit tests for Batch 3 items B1 (diffs), B3 (structured activity), B4 (Windows manifests & install.ps1), and B5 (passive CLI auth probes)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mesh.adapters.activity import (
    MAX_ACTIVITY_ENTRIES,
    MAX_DETAIL_CHARS,
    MAX_TARGET_CHARS,
    extract_activity_from_lines,
    extract_run_activity,
)
from mesh.adapters.base import ReadonlyCmdResult
from mesh.core import diffs as diffs_mod
from mesh.core.diffs import (
    DIFF_TOTAL_TIMEOUT_S,
    MAX_FILE_PATCH_BYTES,
    REDACT_LOOKAHEAD_BYTES,
    _open_regular_beneath_nofollow,
    _read_untracked_preview,
    compute_project_diff,
    compute_task_diff,
)
from mesh.core.gitops import run_git, run_git_bounded
from mesh.desktop.bridge import DesktopBridge, _LONG_RPC_TIMEOUTS
from scripts import gen_win_manifests

REPO_ROOT = Path(__file__).resolve().parents[2]


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    (path / "keep.txt").write_text("line1\nline2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    cp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True)
    return cp.stdout.strip()


class TestB1ReadOnlyGitDiffs(unittest.TestCase):
    def test_compute_task_diff_committed_dirty_untracked_redacted_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-b1-diff-") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base_sha = _init_git_repo(repo)
            wt = root / "ws" / "prj_1" / "tasks" / "tsk_1"
            wt.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "worktree", "add", "-b", "mesh/prj_1/tsk_1", str(wt), base_sha],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            # Committed modification + secret token that MUST be redacted
            (wt / "README.md").write_text(
                "hello\nupdated line with secret sk-ant-api03-1234567890abcdef1234567890abcdef\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "README.md"], cwd=wt, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "edit readme"], cwd=wt, check=True, capture_output=True)

            # Uncommitted deletion + untracked file + untracked binary file
            (wt / "keep.txt").unlink()
            (wt / "new_note.txt").write_text("fresh untracked line\n", encoding="utf-8")
            (wt / "blob.bin").write_bytes(b"\x00\x01\x02binary")

            # Symlinked untracked file pointing outside worktree must NOT leak content
            outside = root / "outside_secret.txt"
            outside.write_text("TOP_SECRET_OUTSIDE_CONTENT\n", encoding="utf-8")
            try:
                os.symlink(str(outside), str(wt / "evil_link.txt"))
            except OSError:
                pass

            res = compute_task_diff(
                task_id="tsk_1",
                project_id="prj_1",
                task_kind="code",
                workspace_path=str(wt),
                workspace_real=os.path.realpath(str(wt)),
                branch="mesh/prj_1/tsk_1",
                base_commit=base_sha,
                repo_path=str(repo),
                integ_wt=None,
            )

            self.assertTrue(res["available"])
            self.assertTrue(res["worktree_exists"])
            self.assertTrue(res["dirty"])
            by_path = {f["path"]: f for f in res["files"]}
            self.assertIn("README.md", by_path)
            self.assertEqual(by_path["README.md"]["status"], "M")
            self.assertNotIn("sk-ant-api03-1234567890", by_path["README.md"]["patch"])
            self.assertIn("[REDACTED", by_path["README.md"]["patch"])
            self.assertIn("keep.txt", by_path)
            self.assertEqual(by_path["keep.txt"]["status"], "D")
            self.assertIn("new_note.txt", by_path)
            self.assertTrue(by_path["new_note.txt"]["untracked"])
            self.assertIn("blob.bin", by_path)
            self.assertTrue(by_path["blob.bin"]["binary"])
            # Symlinked outside file content never leaked
            serialized = json.dumps(res)
            self.assertNotIn("TOP_SECRET_OUTSIDE_CONTENT", serialized)

            # Tight max_bytes cap marks truncated=True
            (wt / "big.txt").write_text("A" * 5000 + "\n", encoding="utf-8")
            bounded = compute_task_diff(
                task_id="tsk_1",
                project_id="prj_1",
                task_kind="code",
                workspace_path=str(wt),
                workspace_real=os.path.realpath(str(wt)),
                branch="mesh/prj_1/tsk_1",
                base_commit=base_sha,
                repo_path=str(repo),
                integ_wt=None,
                max_bytes=1024,
            )
            self.assertTrue(bounded["available"])
            self.assertTrue(bounded["truncated"])

    def test_compute_task_diff_refuses_foreign_worktree_and_general_task(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-b1-foreign-") as tmp:
            root = Path(tmp)
            repo_a = root / "repo_a"
            repo_b = root / "repo_b"
            sha_a = _init_git_repo(repo_a)
            _init_git_repo(repo_b)

            # Non-code task
            gen = compute_task_diff(
                task_id="tsk_g",
                project_id="prj_1",
                task_kind="general",
                workspace_path=None,
                workspace_real=None,
                branch=None,
                base_commit=None,
                repo_path=str(repo_a),
                integ_wt=None,
            )
            self.assertFalse(gen["available"])
            self.assertEqual(gen["reason"], "not_code_task")

            # Foreign repo passed as task worktree -> refused before running git
            foreign = compute_task_diff(
                task_id="tsk_f",
                project_id="prj_1",
                task_kind="code",
                workspace_path=str(repo_b),
                workspace_real=os.path.realpath(str(repo_b)),
                branch="main",
                base_commit=sha_a,
                repo_path=str(repo_a),
                integ_wt=None,
            )
            self.assertFalse(foreign["available"])
            self.assertEqual(foreign["reason"], "untrusted_worktree")

    def test_compute_project_diff_reads_integration_vs_base_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-b1-proj-") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base_sha = _init_git_repo(repo)
            integ_wt = root / "ws" / "prj_1" / "integration"
            integ_wt.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "worktree", "add", "-b", "mesh/prj_1/integration", str(integ_wt), base_sha],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            (integ_wt / "feature.py").write_text("print('hi')\n", encoding="utf-8")
            subprocess.run(["git", "add", "feature.py"], cwd=integ_wt, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "add feature"], cwd=integ_wt, check=True, capture_output=True)

            res = compute_project_diff(
                project_id="prj_1",
                repo_path=str(repo),
                base_branch="main",
                base_commit=base_sha,
                integration_branch="mesh/prj_1/integration",
                integ_wt=os.path.realpath(str(integ_wt)),
            )
            self.assertTrue(res["available"])
            self.assertEqual(res["commits_ahead"], 1)
            self.assertEqual(res["stat"]["files_changed"], 1)
            self.assertEqual(res["files"][0]["path"], "feature.py")
            self.assertEqual(res["files"][0]["status"], "A")
            # Ensure base branch HEAD was never mutated
            after_main = run_git(["rev-parse", "refs/heads/main"], cwd=str(repo)).stdout.strip()
            self.assertEqual(after_main, base_sha)

    def test_giant_diff_is_bounded_at_source_and_special_filenames_produce_patches(self) -> None:
        """P2-1 & P3-2: giant diffs stop reading git stdout at the cap; C-quoted/non-ASCII filenames keep patches."""
        with tempfile.TemporaryDirectory(prefix="mh-b1-giant-") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base_sha = _init_git_repo(repo)
            wt = root / "ws" / "prj_1" / "tasks" / "tsk_giant"
            wt.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "worktree", "add", "-b", "mesh/prj_1/tsk_giant", str(wt), base_sha],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            # Giant 2 MiB tracked file + small files with non-ASCII, quotes, and tab in filename
            (wt / "00_giant.txt").write_text(("X" * 100 + "\n") * 22000, encoding="utf-8")
            special_name = "café 'quoted' file.py" if os.name == "nt" else 'café "quoted" file.py'
            (wt / special_name).write_text("print('special ok')\n", encoding="utf-8")
            tab_name = None
            if os.name != "nt":
                tab_name = 'café\t"tab".py'
                (wt / tab_name).write_text("print('tab ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "add giant and special"], cwd=wt, check=True, capture_output=True)

            seen_caps: list[int] = []
            real_bounded = run_git_bounded

            def _spy_bounded(args, cwd=None, *, max_output_bytes, timeout_s=30.0):
                if args and args[0] == "diff" and "--unified=3" in args:
                    seen_caps.append(max_output_bytes)
                return real_bounded(args, cwd=cwd, max_output_bytes=max_output_bytes, timeout_s=timeout_s)

            with mock.patch("mesh.core.diffs.run_git_bounded", side_effect=_spy_bounded):
                res = compute_task_diff(
                    task_id="tsk_giant",
                    project_id="prj_1",
                    task_kind="code",
                    workspace_path=str(wt),
                    workspace_real=os.path.realpath(str(wt)),
                    branch="mesh/prj_1/tsk_giant",
                    base_commit=base_sha,
                    repo_path=str(repo),
                    integ_wt=None,
                )

            self.assertTrue(res["available"])
            self.assertTrue(res["truncated"])
            self.assertTrue(seen_caps, "Expected bounded per-file git diff calls")
            for cap_arg in seen_caps:
                self.assertLessEqual(cap_arg, MAX_FILE_PATCH_BYTES + REDACT_LOOKAHEAD_BYTES)
            by_path = {f["path"]: f for f in res["files"]}
            self.assertIn("00_giant.txt", by_path)
            self.assertTrue(by_path["00_giant.txt"]["truncated"])
            self.assertLessEqual(
                len(by_path["00_giant.txt"]["patch"].encode("utf-8")),
                MAX_FILE_PATCH_BYTES + 64,
            )
            self.assertIn(special_name, by_path)
            self.assertIn("special ok", by_path[special_name]["patch"])
            if tab_name is not None:
                self.assertIn(tab_name, by_path)
                self.assertIn("tab ok", by_path[tab_name]["patch"])

        # P3-6: production-dead _split_unified_patches helper removed from diffs module
        self.assertFalse(hasattr(diffs_mod, "_split_unified_patches"))

    def test_diff_enforces_cumulative_time_budget_and_marks_git_failures(self) -> None:
        """P2-2 & P3-1: cumulative deadline caps git spawns below bridge 30s timeout; git failure marks file truncated/error."""
        self.assertLess(DIFF_TOTAL_TIMEOUT_S, _LONG_RPC_TIMEOUTS["task.diff"])
        self.assertLess(DIFF_TOTAL_TIMEOUT_S, _LONG_RPC_TIMEOUTS["project.diff"])

        with tempfile.TemporaryDirectory(prefix="mh-b1-budget-") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base_sha = _init_git_repo(repo)
            wt = root / "ws" / "prj_1" / "tasks" / "tsk_budget"
            wt.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "worktree", "add", "-b", "mesh/prj_1/tsk_budget", str(wt), base_sha],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            for idx in range(5):
                (wt / f"f{idx}.txt").write_text(f"content {idx}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "add five files"], cwd=wt, check=True, capture_output=True)
            (wt / "untracked_late.txt").write_text("untracked\n", encoding="utf-8")

            # P2-2: simulate clock advancing past the cumulative deadline after the 1st per-file diff
            fake_clock = [100.0]
            per_file_spawns: list[tuple[list[str], float]] = []
            real_bounded = run_git_bounded

            def _clock_monotonic() -> float:
                return fake_clock[0]

            def _slow_bounded(args, cwd=None, *, max_output_bytes, timeout_s=30.0):
                if args and args[0] == "diff" and "--unified=3" in args:
                    per_file_spawns.append((list(args), float(timeout_s)))
                    res = real_bounded(args, cwd=cwd, max_output_bytes=max_output_bytes, timeout_s=timeout_s)
                    # Advance clock past total_timeout_s=5.0 after first per-file diff completes
                    fake_clock[0] += 6.0
                    return res
                return real_bounded(args, cwd=cwd, max_output_bytes=max_output_bytes, timeout_s=timeout_s)

            with (
                mock.patch("mesh.core.diffs.time.monotonic", side_effect=_clock_monotonic),
                mock.patch("mesh.core.diffs.run_git_bounded", side_effect=_slow_bounded),
            ):
                res = compute_task_diff(
                    task_id="tsk_budget",
                    project_id="prj_1",
                    task_kind="code",
                    workspace_path=str(wt),
                    workspace_real=os.path.realpath(str(wt)),
                    branch="mesh/prj_1/tsk_budget",
                    base_commit=base_sha,
                    repo_path=str(repo),
                    integ_wt=None,
                    total_timeout_s=5.0,
                )

            self.assertTrue(res["available"])
            self.assertTrue(res["truncated"])
            # Must stop spawning per-file git diffs immediately once cumulative deadline is exceeded
            self.assertEqual(len(per_file_spawns), 1)
            self.assertLessEqual(per_file_spawns[0][1], 5.0)
            by_path = {f["path"]: f for f in res["files"]}
            self.assertEqual(len(by_path), 6)
            self.assertTrue(by_path["f0.txt"]["patch"])
            self.assertFalse(by_path["f0.txt"]["truncated"])
            for skipped in ("f1.txt", "f2.txt", "f3.txt", "f4.txt", "untracked_late.txt"):
                self.assertTrue(by_path[skipped]["truncated"], f"{skipped} must be marked truncated on deadline")
                self.assertEqual(by_path[skipped].get("error"), "timeout")
                self.assertEqual(by_path[skipped]["patch"], "")

            # P3-1: git failure on a single file must NOT silently yield truncated=False with empty patch
            def _fail_one_bounded(args, cwd=None, *, max_output_bytes, timeout_s=30.0):
                if args and args[0] == "diff" and "--unified=3" in args and any("f2.txt" in a for a in args):
                    return (128, "", False)
                return real_bounded(args, cwd=cwd, max_output_bytes=max_output_bytes, timeout_s=timeout_s)

            with mock.patch("mesh.core.diffs.run_git_bounded", side_effect=_fail_one_bounded):
                res_fail = compute_task_diff(
                    task_id="tsk_budget",
                    project_id="prj_1",
                    task_kind="code",
                    workspace_path=str(wt),
                    workspace_real=os.path.realpath(str(wt)),
                    branch="mesh/prj_1/tsk_budget",
                    base_commit=base_sha,
                    repo_path=str(repo),
                    integ_wt=None,
                )

            self.assertTrue(res_fail["available"])
            self.assertTrue(res_fail["truncated"])
            by_path_fail = {f["path"]: f for f in res_fail["files"]}
            self.assertTrue(by_path_fail["f2.txt"]["truncated"])
            self.assertEqual(by_path_fail["f2.txt"].get("error"), "git_diff_failed")
            self.assertEqual(by_path_fail["f2.txt"]["patch"], "")
            self.assertFalse(by_path_fail["f0.txt"]["truncated"])
            self.assertIn("content 0", by_path_fail["f0.txt"]["patch"])

            # Round 3 P3-3: metadata-phase (--name-status) git error or timeout must return available=False + reason
            def _fail_name_status(args, cwd=None, *, max_output_bytes, timeout_s=30.0):
                if args and "--name-status" in args:
                    return (128, "", False)
                return real_bounded(args, cwd=cwd, max_output_bytes=max_output_bytes, timeout_s=timeout_s)

            with mock.patch("mesh.core.diffs.run_git_bounded", side_effect=_fail_name_status):
                res_meta_err = compute_task_diff(
                    task_id="tsk_budget",
                    project_id="prj_1",
                    task_kind="code",
                    workspace_path=str(wt),
                    workspace_real=os.path.realpath(str(wt)),
                    branch="mesh/prj_1/tsk_budget",
                    base_commit=base_sha,
                    repo_path=str(repo),
                    integ_wt=None,
                )
            self.assertFalse(res_meta_err["available"])
            self.assertTrue(res_meta_err["truncated"])
            self.assertEqual(res_meta_err["reason"], "git_diff_failed")
            self.assertEqual(res_meta_err["files"], [])

            # Metadata-phase timeout after eff_base resolution -> available=False, reason="diff_timeout"
            meta_clock = [200.0]

            def _timeout_name_status(args, cwd=None, *, max_output_bytes, timeout_s=30.0):
                if args and "--name-status" in args:
                    meta_clock[0] += 10.0
                    return (124, "", False)
                return real_bounded(args, cwd=cwd, max_output_bytes=max_output_bytes, timeout_s=timeout_s)

            with (
                mock.patch("mesh.core.diffs.time.monotonic", side_effect=lambda: meta_clock[0]),
                mock.patch("mesh.core.diffs.run_git_bounded", side_effect=_timeout_name_status),
            ):
                res_meta_to = compute_task_diff(
                    task_id="tsk_budget",
                    project_id="prj_1",
                    task_kind="code",
                    workspace_path=str(wt),
                    workspace_real=os.path.realpath(str(wt)),
                    branch="mesh/prj_1/tsk_budget",
                    base_commit=base_sha,
                    repo_path=str(repo),
                    integ_wt=None,
                    total_timeout_s=5.0,
                )
            self.assertFalse(res_meta_to["available"])
            self.assertTrue(res_meta_to["truncated"])
            self.assertEqual(res_meta_to["reason"], "diff_timeout")
            self.assertEqual(res_meta_to["files"], [])

            # Also verify compute_project_diff propagates metadata-phase failure cleanly
            subprocess.run(
                ["git", "branch", "mesh/prj_1/integration", "mesh/prj_1/tsk_budget"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            with mock.patch("mesh.core.diffs.run_git_bounded", side_effect=_fail_name_status):
                res_proj_err = compute_project_diff(
                    project_id="prj_1",
                    repo_path=str(repo),
                    base_branch="main",
                    base_commit=base_sha,
                    integration_branch="mesh/prj_1/integration",
                    integ_wt=None,
                )
            self.assertFalse(res_proj_err["available"])
            self.assertTrue(res_proj_err["truncated"])
            self.assertEqual(res_proj_err["reason"], "git_diff_failed")

    def test_untracked_preview_refuses_ancestor_directory_symlink(self) -> None:
        """P3-3: _open_regular_beneath_nofollow refuses ancestor directory symlinks pointing outside."""
        with tempfile.TemporaryDirectory(prefix="mh-b1-sym-") as tmp:
            root = Path(tmp)
            wt = root / "wt"
            wt.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("OUTSIDE_SECRET_DATA\n", encoding="utf-8")
            try:
                os.symlink(str(outside), str(wt / "linked_dir"))
            except OSError:
                self.skipTest("symlinks unavailable on this platform")

            self.assertIsNone(_open_regular_beneath_nofollow(os.path.realpath(str(wt)), "linked_dir/secret.txt"))
            patch, adds, is_bin, trunc = _read_untracked_preview(os.path.realpath(str(wt)), "linked_dir/secret.txt", 4096)
            self.assertEqual(patch, "")
            self.assertEqual(adds, 0)
            self.assertFalse(is_bin)
            self.assertFalse(trunc)


class TestB3StructuredActivityExtraction(unittest.TestCase):
    def test_extracts_across_all_six_adapters_and_redacts_secrets(self) -> None:
        secret = "sk-proj-secret1234567890abcdef1234567890"
        samples = {
            "claude": [
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "Inspecting codebase first"},
                            {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "app.py"}},
                            {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"command": f"echo {secret}"}},
                        ]
                    },
                }),
            ],
            "codex": [
                json.dumps({"type": "item.started", "item": {"id": "it_1", "type": "command_execution", "command": "pytest -q"}}),
                json.dumps({"type": "item.completed", "item": {"id": "it_1", "type": "command_execution", "command": "pytest -q", "exit_code": 0}}),
                json.dumps({"type": "item.completed", "item": {"id": "it_2", "type": "file_change", "changes": [{"path": "foo.py"}]}}),
            ],
            "agy": [
                json.dumps({"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "write_file", "tool_info": {"name": "write_file", "parameters": {"Path": "src/a.py"}}}}),
                json.dumps({"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "write_file", "tool_info": {"name": "write_file", "parameters": {"Path": "src/a.py"}}}}),
            ],
            "droid": [
                json.dumps({"type": "tool_call", "id": "tc_1", "toolName": "Execute", "parameters": {"command": "touch y.txt"}}),
                json.dumps({"type": "tool_result", "id": "tc_1", "isError": True, "error": {"message": "permission denied"}}),
            ],
            "kiro": [
                json.dumps({"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "tool_call", "toolCallId": "k_1", "title": "fs_read", "status": "completed", "rawInput": {"path": "README.md"}}}}),
            ],
            "vibe": [
                json.dumps({"id": "ef_1", "type": "effect", "title": "file_system.bash", "generationStatus": "completed", "detail": {"command": "ls -la"}}),
            ],
        }

        claude_res = extract_activity_from_lines(samples["claude"], adapter="claude")
        self.assertEqual(claude_res["total_entries"], 3)
        self.assertEqual([e["kind"] for e in claude_res["entries"]], ["thought", "read", "command"])
        self.assertNotIn(secret, json.dumps(claude_res))
        self.assertIn("[REDACTED", json.dumps(claude_res))

        codex_res = extract_activity_from_lines(samples["codex"], adapter="codex")
        # Deduplicated it_1 (started -> completed) + it_2
        self.assertEqual(codex_res["total_entries"], 2)
        self.assertEqual([e["kind"] for e in codex_res["entries"]], ["command", "edit"])
        self.assertEqual(codex_res["entries"][0]["status"], "ok")

        agy_res = extract_activity_from_lines(samples["agy"], adapter="agy")
        self.assertEqual(agy_res["total_entries"], 1)
        self.assertEqual(agy_res["entries"][0]["kind"], "edit")
        self.assertEqual(agy_res["entries"][0]["status"], "ok")

        droid_res = extract_activity_from_lines(samples["droid"], adapter="droid")
        self.assertEqual(droid_res["total_entries"], 1)
        self.assertEqual(droid_res["entries"][0]["kind"], "command")
        self.assertEqual(droid_res["entries"][0]["status"], "error")

        kiro_res = extract_activity_from_lines(samples["kiro"], adapter="kiro")
        self.assertEqual(kiro_res["total_entries"], 1)
        self.assertEqual(kiro_res["entries"][0]["kind"], "read")

        vibe_res = extract_activity_from_lines(samples["vibe"], adapter="vibe")
        self.assertEqual(vibe_res["total_entries"], 1)
        self.assertEqual(vibe_res["entries"][0]["kind"], "command")

    def test_extracts_real_vendor_fixtures_and_enforces_caps_and_mesh_actions(self) -> None:
        for adapter_id, rel in (
            ("agy", "tests/fixtures/real/agy/1.5.12/smoke-S-AGY-8-stdout.jsonl"),
            ("droid", "tests/fixtures/real/droid/1.6.0/smoke-S-DROID-8-stdout.jsonl"),
            ("vibe", "tests/fixtures/real/vibe/3.1.0/streaming-terminated.jsonl"),
        ):
            fpath = REPO_ROOT / rel
            if fpath.is_file():
                lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
                res = extract_activity_from_lines(lines, adapter=adapter_id)
                self.assertGreater(res["total_entries"], 0, f"Expected entries for real fixture {rel}")

        # Cap enforcement
        many = [
            json.dumps({"type": "tool_call", "id": f"id_{i}", "toolName": "Read", "parameters": {"path": f"f{i}.py"}})
            for i in range(MAX_ACTIVITY_ENTRIES + 25)
        ]
        capped = extract_activity_from_lines(many, adapter="droid")
        self.assertEqual(capped["total_entries"], MAX_ACTIVITY_ENTRIES)
        self.assertTrue(capped["truncated"])

        # Lead mesh-actions extraction + file symlink refusal
        with tempfile.TemporaryDirectory(prefix="mh-b3-act-") as tmp:
            run_dir = Path(tmp) / "run_1"
            run_dir.mkdir()
            (run_dir / "stdout.log").write_text(
                'Plan ready:\n```mesh-actions\n[{"cmd": "plan.propose", "args": {"title": "Ship B3"}}]\n```\n',
                encoding="utf-8",
            )
            act = extract_run_activity(str(run_dir), adapter="fake")
            self.assertTrue(act["available"])
            self.assertEqual(act["total_entries"], 1)
            self.assertEqual(act["entries"][0]["kind"], "mesh-action")
            self.assertEqual(act["entries"][0]["name"], "plan.propose")

    def test_mesh_actions_flood_respects_entry_cap_and_thought_target_redacts_before_clip(self) -> None:
        """P2-2 & P3-4: mesh-actions flood cannot bypass entry cap; thought target redacts before clipping."""
        # Flood via Claude assistant text blocks (inline _extract_mesh_actions_from_text path)
        flood_lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": f'```mesh-actions\n[{{"cmd": "chat.reply", "args": {{"text": "msg {i}"}}}}]\n```',
                        }
                    ]
                },
            })
            for i in range(MAX_ACTIVITY_ENTRIES + 60)
        ]
        res_default = extract_activity_from_lines(flood_lines, adapter="claude")
        self.assertEqual(res_default["total_entries"], MAX_ACTIVITY_ENTRIES)
        self.assertTrue(res_default["truncated"])

        res_small = extract_activity_from_lines(flood_lines, adapter="claude", limit=15)
        self.assertEqual(res_small["total_entries"], 15)
        self.assertTrue(res_small["truncated"])

        # Flood via Codex agent_message and plain text
        codex_flood = [
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": f'```mesh-actions\n[{{"cmd": "task.create", "args": {{"title": "t {i}"}}}}]\n```',
                },
            })
            for i in range(40)
        ]
        res_codex = extract_activity_from_lines(codex_flood, adapter="codex", limit=10)
        self.assertEqual(res_codex["total_entries"], 10)
        self.assertTrue(res_codex["truncated"])

        # P3-4: Secret straddling MAX_TARGET_CHARS (180) in Claude thinking & Kiro agent_thought_chunk
        secret = "sk-ant-api03-1234567890abcdef1234567890abcdef"
        prefix = "A" * (MAX_TARGET_CHARS - 12)
        straddle_line = f"{prefix} {secret} tail"
        claude_think = [
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": straddle_line}]},
            })
        ]
        kiro_think = [
            json.dumps({
                "type": "sessionUpdate",
                "data": {"update": {"sessionUpdate": "agent_thought_chunk", "content": {"text": straddle_line}}},
            })
        ]
        for adapter_name, sample in (("claude", claude_think), ("kiro", kiro_think)):
            r = extract_activity_from_lines(sample, adapter=adapter_name)
            self.assertEqual(r["total_entries"], 1)
            serialized = json.dumps(r)
            self.assertNotIn("sk-ant-api03", serialized)
            self.assertIn("[REDACTED", r["entries"][0]["target"])

    def test_droid_error_text_survives_when_prior_detail_is_at_cap(self) -> None:
        """P3-3: droid tool_result error message must not be clipped away when initial tool_call detail was at cap."""
        secret = "sk-ant-api03-1234567890abcdef1234567890abcdef"
        huge_payload = "Z" * (MAX_DETAIL_CHARS + 500)
        lines = [
            json.dumps({
                "type": "tool_call",
                "id": "tc_huge",
                "toolName": "Execute",
                "parameters": {"command": "run-big", "blob": huge_payload},
            }),
            json.dumps({
                "type": "tool_result",
                "id": "tc_huge",
                "isError": True,
                "error": {"message": f"disk quota exceeded with token {secret}"},
            }),
        ]
        res = extract_activity_from_lines(lines, adapter="droid")
        self.assertEqual(res["total_entries"], 1)
        entry = res["entries"][0]
        self.assertEqual(entry["status"], "error")
        self.assertTrue(entry["detail"].startswith("[error]\ndisk quota exceeded"))
        self.assertIn("[REDACTED", entry["detail"])
        self.assertNotIn(secret, entry["detail"])
        self.assertLessEqual(len(entry["detail"]), MAX_DETAIL_CHARS + len("...[truncated]"))


class TestB2LeadInterruptOutboxPreservation(unittest.TestCase):
    def test_user_interrupted_acks_only_chat_message_and_requeues_non_chat_wakes(self) -> None:
        """P1-1: user_interrupted must NOT drop approval.decided, task.failed, or task.merged lead wakes."""
        from mesh.core.clock import now_iso_utc
        from mesh.core.db import init_db
        from mesh.core.events import record_event
        from mesh.core.ids import generate_id
        from mesh.daemon import supervisor, terminate

        with tempfile.TemporaryDirectory(prefix="mh-p1-1-") as home:
            conn = init_db(os.path.join(home, "mesh.db"))
            try:
                now = now_iso_utc()
                conn.execute(
                    "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', 'lead1', ?);",
                    ("agt_lead1", now),
                )
                conn.execute(
                    """INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, default_profile,
                       max_unapproved_profile, capabilities_json, allowed_models_json, version, created_at, updated_at)
                       VALUES ('agt_lead1', 'fake', 'launch', 1, 1, 'readonly', 'readonly', '[]', '[]', 1, ?, ?);""",
                    (now, now),
                )
                conn.execute(
                    """INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at)
                       VALUES ('prj_1', 'P1', 'G', 'active', 'user', 'direct', ?, ?);""",
                    (now, now),
                )
                run_id = "run_lead_int_1"
                conn.execute(
                    """INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, profile, status, boot_id, run_dir, started_at)
                       VALUES (?, 'lead_turn', 'prj_1', 'agt_lead1', 1, 'fake', 'readonly', 'running', 'boot_1', ?, ?);""",
                    (run_id, os.path.join(home, "runs", run_id), now),
                )

                def _add_wake(etype: str, dedup: str) -> str:
                    ev = record_event(
                        conn=conn,
                        type_=etype,
                        actor_id="system",
                        actor_kind="system",
                        via="system",
                        project_id="prj_1",
                        payload={"test": etype},
                    )
                    oid = generate_id("msg")
                    conn.execute(
                        """INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id,
                           type, payload_json, status, attempts, next_attempt_at, delivering_run_id, created_at)
                           VALUES (?, ?, 'agt_lead1', 'lead_wake', ?, 'prj_1', ?, '{}', 'delivering', 0, ?, ?, ?);""",
                        (oid, ev["seq"], dedup, etype, now, run_id, now),
                    )
                    return oid

                chat_oid = _add_wake("chat.message", "dedup:chat:1")
                apr_oid = _add_wake("approval.decided", "dedup:apr:1")
                fail_oid = _add_wake("task.failed", "dedup:fail:1")
                merged_oid = _add_wake("task.merged", "dedup:merged:1")
                conn.commit()

                run_row = dict(conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone())
                term = terminate.Termination(
                    run_id=run_id,
                    pgid=0,
                    wrapper_pid=0,
                    end_reason="user_interrupted",
                    grace_s=1.0,
                )
                supervisor._finalize_lead_turn_termination(
                    conn, home, run_row, term, {"exit_code": 143, "signal": 15}, testing_mode=True
                )

                rows = {
                    r["id"]: dict(r)
                    for r in conn.execute("SELECT id, type, status, attempts, delivering_run_id, last_error FROM outbox;").fetchall()
                }
                # chat.message is acked with user_interrupted so Stop stays stopped
                self.assertEqual(rows[chat_oid]["status"], "acked")
                self.assertEqual(rows[chat_oid]["last_error"], "user_interrupted")
                self.assertIsNone(rows[chat_oid]["delivering_run_id"])
                # Non-chat lifecycle wakes MUST survive as pending for the next lead turn
                for oid in (apr_oid, fail_oid, merged_oid):
                    self.assertEqual(rows[oid]["status"], "pending", f"{rows[oid]['type']} was dropped!")
                    self.assertIsNone(rows[oid]["delivering_run_id"])
                    self.assertEqual(rows[oid]["attempts"], 1)
            finally:
                conn.close()


class TestB4WindowsManifestsAndInstallPs1(unittest.TestCase):
    def test_checked_in_manifests_pass_template_check_and_fail_strict_placeholder_check(self) -> None:
        # P3-1: strict default check MUST reject all-zero placeholder hashes
        ok_strict, errs_strict = gen_win_manifests.verify_manifests(REPO_ROOT / "packaging")
        self.assertFalse(ok_strict, "Strict verify_manifests must reject all-zero placeholders")
        self.assertTrue(any("placeholder" in e.lower() for e in errs_strict), str(errs_strict))
        self.assertEqual(
            gen_win_manifests.main(["--check", "--out-dir", str(REPO_ROOT / "packaging")]),
            1,
        )

        # Explicit --allow-placeholders validates checked-in template structure
        ok_tpl, errs_tpl = gen_win_manifests.verify_manifests(
            REPO_ROOT / "packaging", allow_placeholders=True
        )
        self.assertTrue(ok_tpl, f"Checked-in manifests failed template validation: {errs_tpl}")
        self.assertEqual(
            gen_win_manifests.main(["--check", "--allow-placeholders", "--out-dir", str(REPO_ROOT / "packaging")]),
            0,
        )

    def test_manifest_generator_validates_and_quotes_yaml_inputs(self) -> None:
        """P3-6: reject newlines/invalid identifiers and YAML-quote special publisher/package_name."""
        p_hash = "a" * 64
        s_hash = "b" * 64
        # Special YAML characters in publisher/package_name are double-quoted safely,
        # and PackageVersion / InstallerSha256 are always quoted strings (P3-2: avoids YAML float/int coercion).
        digit_hash = "1" * 64
        files = gen_win_manifests.build_winget_manifests(
            version="1.0",
            repo="Owner/Repo",
            portable_sha256=digit_hash,
            setup_sha256=s_hash,
            publisher="Acme: Labs",
            package_name='Agent #1 "Mesh"',
        )
        ver_yaml = files["AgentMesh.AgentMesh.yaml"]
        inst_yaml = files["AgentMesh.AgentMesh.installer.yaml"]
        locale = files["AgentMesh.AgentMesh.locale.en-US.yaml"]
        self.assertIn('PackageVersion: "1.0"', ver_yaml)
        self.assertIn('PackageVersion: "1.0"', inst_yaml)
        self.assertIn('PackageVersion: "1.0"', locale)
        self.assertIn(f'InstallerSha256: "{s_hash.upper()}"', inst_yaml)
        self.assertIn(f'InstallerSha256: "{digit_hash}"', inst_yaml)
        self.assertIn('Publisher: "Acme: Labs"', locale)
        self.assertIn('PackageName: "Agent #1 \\"Mesh\\""', locale)

        # verify_manifests rejects unquoted PackageVersion / InstallerSha256
        with tempfile.TemporaryDirectory(prefix="mh-b4-unquoted-") as tmp:
            out_dir = Path(tmp)
            gen_win_manifests.generate_all_manifests(
                out_dir=out_dir,
                version="1.0",
                portable_sha256=digit_hash,
                setup_sha256=s_hash,
            )
            ok, errs = gen_win_manifests.verify_manifests(out_dir)
            self.assertTrue(ok, str(errs))
            inst_path = out_dir / "winget" / "AgentMesh.AgentMesh.installer.yaml"
            inst_path.write_text(
                inst_path.read_text(encoding="utf-8")
                .replace('PackageVersion: "1.0"', "PackageVersion: 1.0")
                .replace(f'InstallerSha256: "{digit_hash}"', f"InstallerSha256: {digit_hash}"),
                encoding="utf-8",
            )
            ok_bad, errs_bad = gen_win_manifests.verify_manifests(out_dir)
            self.assertFalse(ok_bad)
            self.assertTrue(any("PackageVersion" in e for e in errs_bad), str(errs_bad))
            self.assertTrue(any("InstallerSha256" in e for e in errs_bad), str(errs_bad))

        # Newlines / control chars / bad version / bad package_id / bad URL rejected
        for bad_kwargs in (
            {"version": "."},
            {"version": ".."},
            {"version": ".1"},
            {"publisher": "Acme\nInjected: true"},
            {"package_name": "Mesh\r\nBad: 1"},
            {"package_id": "Bad ID: evil"},
            {"package_id": "../escape"},
            {"release_base_url": "https://example.com/a\nInjected: 1"},
            {"release_base_url": "javascript:alert(1)"},
        ):
            call_kwargs = {
                "version": "1.2.3",
                "repo": "Owner/Repo",
                "portable_sha256": p_hash,
                "setup_sha256": s_hash,
            }
            call_kwargs.update(bad_kwargs)
            with self.assertRaises(ValueError):
                gen_win_manifests.build_winget_manifests(**call_kwargs)

    def test_build_windows_portable_writes_sha256sums_after_inno_before_manifests(self) -> None:
        """P3-1: SHA256SUMS.txt is generated after --inno so setup.exe hash feeds gen_win_manifests.py."""
        src = (REPO_ROOT / "scripts" / "build_windows_portable.py").read_text(encoding="utf-8")
        inno_idx = src.index("build_windows_installer.py")
        sums_idx = src.index('os.path.join(args.out, "SHA256SUMS.txt")')
        man_idx = src.index("gen_win_manifests.py")
        self.assertLess(inno_idx, sums_idx)
        self.assertLess(sums_idx, man_idx)

    def test_generator_reads_sha256sums_and_emits_valid_scoop_and_winget(self) -> None:
        p_hash = "a" * 64
        s_hash = "b" * 64
        with tempfile.TemporaryDirectory(prefix="mh-b4-gen-") as tmp:
            tmp_path = Path(tmp)
            sums = tmp_path / "SHA256SUMS.txt"
            sums.write_text(
                f"{p_hash}  AgentMesh-2.3.4-win-x64-portable.zip\n"
                f"{s_hash}  AgentMesh-2.3.4-win-x64-setup.exe\n",
                encoding="utf-8",
            )
            out_dir = tmp_path / "out"
            rc = gen_win_manifests.main([
                "--version", "2.3.4",
                "--sha256sums", str(sums),
                "--out-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            ok, errs = gen_win_manifests.verify_manifests(out_dir)
            self.assertTrue(ok, str(errs))

            scoop = json.loads((out_dir / "scoop" / "agent-mesh.json").read_text(encoding="utf-8"))
            self.assertEqual(scoop["version"], "2.3.4")
            self.assertEqual(scoop["architecture"]["64bit"]["hash"], p_hash)
            self.assertEqual(scoop["license"], "MIT")

            inst_yaml = (out_dir / "winget" / "AgentMesh.AgentMesh.installer.yaml").read_text(encoding="utf-8")
            self.assertIn('PackageVersion: "2.3.4"', inst_yaml)
            self.assertIn(f'InstallerSha256: "{s_hash.upper()}"', inst_yaml)
            self.assertIn(f'InstallerSha256: "{p_hash.upper()}"', inst_yaml)
            self.assertIn("Scope: user", inst_yaml)

    @staticmethod
    def _extract_ps1_function(ps1: str, name: str) -> str:
        match = re.search(
            rf"^function\s+{re.escape(name)}\b.*?^\}}",
            ps1,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert match is not None, f"Could not find PowerShell function {name!r} in scripts/install.ps1"
        return match.group(0)

    def test_install_ps1_security_and_attribution_contracts(self) -> None:
        ps1_path = REPO_ROOT / "scripts" / "install.ps1"
        self.assertTrue(ps1_path.is_file())
        ps1 = ps1_path.read_text(encoding="utf-8")

        # Strict TLS 1.2 assignment without preserving pre-existing Ssl3/Tls/Tls11 bits
        self.assertIn("[System.Net.SecurityProtocolType]::Tls12", ps1)
        self.assertIn("[System.Net.ServicePointManager]::SecurityProtocol =", ps1)
        self.assertNotIn("ServicePointManager]::SecurityProtocol -bor", ps1)
        self.assertNotIn("::Ssl3", ps1)
        self.assertNotIn("::Tls11", ps1)

        # Statically prove SHA-256 verification happens strictly before Expand-Archive in execution order
        self.assertIn("SHA256SUMS.txt", ps1)
        checksum_fn = self._extract_ps1_function(ps1, "Confirm-ArchiveChecksum")
        expand_fn = self._extract_ps1_function(ps1, "Expand-PortableZip")
        install_fn = self._extract_ps1_function(ps1, "Install-VersionedRelease")
        self.assertIn("Get-FileHash -Algorithm SHA256", checksum_fn)
        self.assertIn("Stop-InstallFailure", checksum_fn)
        self.assertIn("Expand-Archive", expand_fn)
        # Expand-Archive must only be invoked inside Expand-PortableZip
        self.assertEqual(ps1.count("Expand-Archive"), 1)
        # Expand-PortableZip must only be invoked inside Install-VersionedRelease (1 def + 1 call)
        self.assertEqual(ps1.count("Expand-PortableZip"), 2)
        self.assertIn("Confirm-ArchiveChecksum", install_fn)
        self.assertIn("Expand-PortableZip", install_fn)
        self.assertLess(
            install_fn.index("Confirm-ArchiveChecksum"),
            install_fn.index("Expand-PortableZip"),
            "Install-VersionedRelease must call Confirm-ArchiveChecksum before Expand-PortableZip",
        )

        # P2-1: Version regex must reject bare dot segments (., ..) and targetDir must be verified beneath versionsDir
        ver_match = re.search(r"\$version\s+-notmatch\s+'([^']+)'", ps1)
        self.assertIsNotNone(ver_match, "install.ps1 missing AGENT_MESH_VERSION regex validation")
        ver_re = re.compile(ver_match.group(1))
        for bad_ver in (".", "..", "...", ".hidden", "-1.0", "../escape", "1.0/2", "1.0\\2", ""):
            self.assertIsNone(ver_re.match(bad_ver), f"install.ps1 version regex must reject {bad_ver!r}")
        for good_ver in ("1.0", "0.1.0", "1.0.0.dev0", "1.2.3-rc.1+build.7", "v2.3.4"):
            self.assertIsNotNone(ver_re.match(good_ver), f"install.ps1 version regex must accept {good_ver!r}")

        # Unpinned resolution must prefer the latest stable release but fall back to a prerelease-only repository.
        resolve_fn = self._extract_ps1_function(ps1, "Resolve-TargetVersion")
        self.assertIn("/releases/latest", resolve_fn)
        self.assertIn("/releases?per_page=1", resolve_fn)
        self.assertIn("if ($httpStatus -ne 404) { throw }", resolve_fn)
        self.assertLess(
            resolve_fn.index("/releases/latest"),
            resolve_fn.index("/releases?per_page=1"),
            "install.ps1 must try the stable latest-release endpoint before the prerelease fallback",
        )
        self.assertIn("$candidate.PSObject.Properties['draft']", resolve_fn)

        self.assertIn("$resolvedTarget = [System.IO.Path]::GetFullPath($targetDir)", install_fn)
        self.assertIn("$resolvedTarget.StartsWith($versionsRoot", install_fn)
        self.assertLess(
            install_fn.index("$resolvedTarget.StartsWith($versionsRoot"),
            install_fn.index("Remove-DirectorySafe $targetDir"),
        )

        # Partial-install recovery must guard against reparse points and 0-byte .install-complete
        completion_fn = self._extract_ps1_function(ps1, "Test-VersionInstallComplete")
        self.assertIn("Test-Path -LiteralPath $completeStamp -PathType Leaf", completion_fn)
        self.assertIn("Get-Content -LiteralPath $completeStamp -Raw -ErrorAction SilentlyContinue", completion_fn)
        self.assertIn("$null -ne $stampText", completion_fn)
        self.assertIn("$existingInstallDir.PSIsContainer", completion_fn)
        self.assertIn("$existingInstallDir.Attributes -band [System.IO.FileAttributes]::ReparsePoint", completion_fn)
        self.assertIn("Test-VersionInstallComplete -TargetDir $targetDir -Version $Version", install_fn)

        # P2-3: Must preserve REG_EXPAND_SZ via HKCU\Environment registry API, never collapse with SetEnvironmentVariable
        self.assertIn("DoNotExpandEnvironmentNames", ps1)
        self.assertIn("RegistryValueKind]::ExpandString", ps1)
        self.assertNotIn("SetEnvironmentVariable", ps1)

        # P3-9 & Round 3 P3-4: Remove-DirectorySafe and $targetDir/$staging cleanup use Get-Item -Force (detecting dangling symlinks)
        self.assertIn("ReparsePoint", ps1)
        self.assertIn("Remove-DirectorySafe", ps1)
        self.assertNotIn("Remove-Item -LiteralPath $targetDir -Recurse", ps1)
        rm_fn_body = self._extract_ps1_function(ps1, "Remove-DirectorySafe")
        self.assertNotIn("Test-PathListContains", rm_fn_body)
        self.assertNotIn("Send-EnvironmentChangeBroadcast", rm_fn_body)
        self.assertNotIn("Test-Path -LiteralPath $path", rm_fn_body)
        self.assertIn("$item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue", rm_fn_body)
        self.assertIn("if ($null -eq $item) { return }", rm_fn_body)
        self.assertNotIn("if (Test-Path -LiteralPath $targetDir)", ps1)
        self.assertIn("Get-Item -LiteralPath $targetDir -Force -ErrorAction SilentlyContinue", ps1)
        self.assertNotIn("if (Test-Path -LiteralPath $staging)", ps1)
        self.assertIn("Get-Item -LiteralPath $staging -Force -ErrorAction SilentlyContinue", ps1)

        # P3-4: Must detect dangling symlinks/reparse points at $shim via Get-Item -Force before WriteAllText
        self.assertIn("$shimItem = Get-Item -LiteralPath $shim -Force -ErrorAction SilentlyContinue", ps1)
        self.assertIn("$shimItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint", ps1)
        self.assertLess(
            ps1.index("$shimItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint"),
            ps1.index("[System.IO.File]::WriteAllText($shim"),
        )
        # Must refuse overwriting foreign mesh binaries/shims
        self.assertIn("refusing to shadow or overwrite", ps1)
        self.assertIn("refusing to overwrite", ps1)

        # Must contain no third-party upstream attribution or NOTICE file, and preserve .agent-mesh
        ps1_lower = ps1.lower()
        for forbidden in ("t3" + " code", "t3" + "code", "t3" + " tools", "pingdot" + "gg"):
            self.assertNotIn(forbidden, ps1_lower)
        self.assertFalse((REPO_ROOT / "NOTICE").exists(), "Repo root NOTICE file must not exist")
        self.assertIn("MIT License", ps1)
        self.assertIn(".agent-mesh", ps1)

        # Must NOT add GitHub workflows in B4
        workflows_dir = REPO_ROOT / ".github" / "workflows"
        if workflows_dir.exists():
            for wf in workflows_dir.iterdir():
                self.assertNotIn("release-desktop", wf.name.lower())

    def test_install_ps1_pipeline_hygiene_and_progress_preference(self) -> None:
        ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        # P0: Invoke-HttpWithBackoff must return directly without intermediate capture or trailing return $null
        retry_fn = self._extract_ps1_function(ps1, "Invoke-HttpWithBackoff")
        self.assertIn("return (& $Operation)", retry_fn)
        self.assertNotIn("$outcome = & $Operation", retry_fn)
        self.assertNotIn("return $null", retry_fn)

        # P0: Save-RemoteAsset and Expand-PortableZip must discard all pipeline output
        save_fn = self._extract_ps1_function(ps1, "Save-RemoteAsset")
        self.assertIn("[void](Invoke-HttpWithBackoff", save_fn)
        self.assertIn("| Out-Null", save_fn)
        expand_fn = self._extract_ps1_function(ps1, "Expand-PortableZip")
        self.assertIn("Invoke-WithoutProgress", expand_fn)
        self.assertIn("| Out-Null", expand_fn)

        # P0: Install-VersionedRelease must only return $targetDir (no extra pipeline output)
        install_fn = self._extract_ps1_function(ps1, "Install-VersionedRelease")
        returns = re.findall(r"^\s*return\b.*$", install_fn, flags=re.MULTILINE)
        self.assertTrue(returns, "Install-VersionedRelease must return $targetDir")
        for ret_stmt in returns:
            self.assertEqual(ret_stmt.strip(), "return $targetDir")
        self.assertIn("[void][System.IO.Directory]::CreateDirectory($releasesDir)", install_fn)
        self.assertIn("[void][System.IO.Directory]::CreateDirectory($staging)", install_fn)
        self.assertIn("& $stagedCli --version | Out-Null", install_fn)

        # P3: Global ProgressPreference toggle via Set-Variable -Scope Global with try/finally restore
        progress_fn = self._extract_ps1_function(ps1, "Invoke-WithoutProgress")
        self.assertIn("Set-Variable -Name 'ProgressPreference' -Scope Global -Value 'SilentlyContinue'", progress_fn)
        self.assertIn("Get-Variable -Name 'ProgressPreference' -Scope Global", progress_fn)
        self.assertIn("finally", progress_fn)

        # P3: SkipUserPath wired to Add-UserPathEntry and APPDATA guarded before Combine
        add_path_fn = self._extract_ps1_function(ps1, "Add-UserPathEntry")
        self.assertNotIn("$env:AGENT_MESH_NO_PATH", add_path_fn)
        self.assertIn("if ($Skip) { return $false }", add_path_fn)
        main_fn = self._extract_ps1_function(ps1, "Invoke-AgentMeshInstall")
        self.assertIn("Add-UserPathEntry -EntryDir $cfg.ShimRoot -Skip $cfg.SkipUserPath", main_fn)

        shortcut_fn = self._extract_ps1_function(ps1, "New-StartMenuShortcut")
        self.assertIn("[string]::IsNullOrWhiteSpace($env:APPDATA)", shortcut_fn)
        self.assertLess(
            shortcut_fn.index("[string]::IsNullOrWhiteSpace($env:APPDATA)"),
            shortcut_fn.index("[System.IO.Path]::Combine($env:APPDATA"),
        )

    def test_install_ps1_repo_base_url_shim_and_ps51_compat(self) -> None:
        ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        # P1: AGENT_MESH_REPO validation rejects path traversal, query/fragment injection, and dot segments
        cfg_fn = self._extract_ps1_function(ps1, "Read-InstallerConfig")
        repo_match = re.search(r"\$slug\s+-notmatch\s+'([^']+)'", cfg_fn)
        self.assertIsNotNone(repo_match, "Read-InstallerConfig missing AGENT_MESH_REPO regex validation")
        repo_re = re.compile(repo_match.group(1))
        for good_repo in ("JamesHu6657/agent-mesh", "owner/repo", "a_b-c.1/x_y-z.2"):
            self.assertIsNotNone(repo_re.match(good_repo), f"repo regex must accept {good_repo!r}")
        for bad_repo in (
            "owner/repo/../../other/repo",
            "../../gists/id",
            "owner/repo?x=1#",
            "owner/repo#frag",
            "owner",
            "/owner/repo",
            "owner/repo extra",
            "",
        ):
            self.assertIsNone(repo_re.match(bad_repo), f"repo regex must reject {bad_repo!r}")
        self.assertIn("$slugParts[0] -eq '.' -or $slugParts[0] -eq '..'", cfg_fn)
        self.assertIn("$slugParts[1] -eq '.' -or $slugParts[1] -eq '..'", cfg_fn)
        self.assertIn("invalid AGENT_MESH_REPO", cfg_fn)

        # P1: AGENT_MESH_RELEASE_BASE_URL validation allows http(s) mirrors and rejects schemes/query/fragment/control chars
        url_match = re.search(r"\$releaseRootUrl\s+-notmatch\s+'((?:[^']|'')+)'", cfg_fn)
        self.assertIsNotNone(url_match, "Read-InstallerConfig missing AGENT_MESH_RELEASE_BASE_URL regex validation")
        url_pattern = url_match.group(1).replace("''", "'")
        url_re = re.compile(url_pattern)
        for good_url in (
            "https://github.com/JamesHu6657/agent-mesh/releases/download",
            "http://127.0.0.1:8080/releases",
            "https://mirror.example.com/agent-mesh",
        ):
            self.assertIsNotNone(url_re.match(good_url), f"base URL regex must accept {good_url!r}")
        for bad_url in (
            "file:///C:/evil",
            "javascript:alert(1)",
            "ftp://example.com/a",
            "https://example.com/a?x=1",
            "https://example.com/a#frag",
            "https://example.com/a b",
            "https://example.com/a'b",
            'https://example.com/a"b',
            "https://example.com/a\\b",
            "https://example.com/a<b",
        ):
            self.assertIsNone(url_re.match(bad_url), f"base URL regex must reject {bad_url!r}")
        self.assertIn(r"[\x00-\x1F\x7F]", cfg_fn)
        self.assertIn("invalid AGENT_MESH_RELEASE_BASE_URL", cfg_fn)

        # P2: Test-ManagedShim handles both legacy 2-line and new 3-line shims, requires fully-qualified paths, avoids Split-Path
        shim_fn = self._extract_ps1_function(ps1, "Test-ManagedShim")
        self.assertIn("$lines.Length -eq 3", shim_fn)
        self.assertIn("rem agent-mesh shim", shim_fn)
        self.assertIn("$lines.Length -eq 2", shim_fn)
        self.assertNotIn("IsPathRooted", shim_fn)
        self.assertIn(r"^[A-Za-z]:[\\/]", shim_fn)
        self.assertIn(r".StartsWith('\\')", shim_fn)
        self.assertIn("[System.IO.Path]::GetFullPath($rawTarget)", shim_fn)
        self.assertNotIn("Split-Path", shim_fn)
        self.assertIn("$versionSeg -eq '.' -or $versionSeg -eq '..'", shim_fn)

        # Non-ASCII install paths: generated shims stay ASCII-only and resolve the
        # exact target through a Unicode-aware wildcard lookup instead of embedding
        # UTF-8 bytes that cmd.exe would decode with the console OEM code page.
        self.assertIn("function ConvertTo-CmdWildcardText", ps1)
        self.assertIn("function ConvertTo-Utf8Hex", ps1)
        self.assertIn("function ConvertFrom-Utf8Hex", ps1)
        self.assertIn("rem agent-mesh-target-hex:", ps1)
        self.assertIn("setlocal EnableExtensions DisableDelayedExpansion", ps1)
        self.assertIn("Get-MeshCliShimContent -ResolvedCli $resolvedCli", ps1)
        self.assertIn("[System.Guid]::NewGuid().ToString('N')", ps1)
        self.assertIn("WriteAllText($shimTokenPath, $shimId, $asciiEncoding)", ps1)
        self.assertIn("Get-MeshCliShimContent -ResolvedCli $resolvedCli -ShimId $shimId", ps1)
        self.assertIn("New-Object System.Text.ASCIIEncoding", ps1)
        self.assertIn("& $shimProbe --version | Out-Null", ps1)
        self.assertIn("installed CLI shim failed --version smoke test", ps1)
        self.assertIn("$lines.Length -ge 9", shim_fn)
        self.assertIn("ConvertFrom-Utf8Hex", shim_fn)
        self.assertIn("Get-MeshCliShimLines -ResolvedCli $rawTarget", shim_fn)
        shim_gen_fn = self._extract_ps1_function(ps1, "Get-MeshCliShimLines")
        self.assertIn('"%_MESH_CLI%" %*', shim_gen_fn)
        self.assertNotIn('call "%_MESH_CLI%"', shim_gen_fn)
        self.assertIn("for /d %%", shim_gen_fn)
        self.assertIn("if %_MESH_N% GTR 1", ps1)
        self.assertIn("ambiguous mesh.exe path", ps1)
        self.assertIn("rem agent-mesh-shim-id:", shim_gen_fn)
        self.assertIn(r"\.shim-id-", shim_gen_fn)
        self.assertIn("'if exist \"' + $cliExpression", shim_gen_fn)
        self.assertIn("\" if exist \"' + $tokenExpression", shim_gen_fn)
        self.assertIn("$tokenExpression + '\" (set /a _MESH_N+=1", shim_gen_fn)
        self.assertIn("$untrimmedLines[$lineIdx].Equals", shim_fn)
        self.assertIn("mesh-shim-test.cmd", ps1)
        self.assertIn("if (-not $hadCompleteInstall)", ps1)
        self.assertIn("Remove-DirectorySafe $installedDir", ps1)
        self.assertIn("if ($createdShimToken)", ps1)
        self.assertIn("Remove-Item -LiteralPath $shimTokenPath", ps1)
        existing_token_fn = self._extract_ps1_function(ps1, "Get-ExistingShimTokenId")
        self.assertIn(r"^\.shim-id-[0-9a-f]{32}$", existing_token_fn)
        self.assertIn("ReparsePoint", existing_token_fn)
        self.assertIn("$storedId.Trim() -eq $tokenId", existing_token_fn)

        # Regression: trailing semicolons in the existing/user-session PATH must not produce `;;`.
        add_path_fn = self._extract_ps1_function(ps1, "Add-UserPathEntry")
        self.assertIn("$currentPath.TrimEnd([char[]]';')", add_path_fn)
        self.assertIn("$env:Path.TrimEnd([char[]]';')", add_path_fn)

        # P1: PowerShell 5.1 compatibility — no PS 7-only operators (??, ?., &&) in code lines
        code_lines = []
        for raw_line in ps1.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Remove single-quoted and double-quoted string literals before checking operators
            no_strings = re.sub(r"'(?:[^']|'')*'", "''", stripped)
            no_strings = re.sub(r'"(?:[^"`]|`.)*"', '""', no_strings)
            code_part = no_strings.split("#", 1)[0]
            code_lines.append(code_part)
        code_blob = "\n".join(code_lines)
        for ps7_op in ("??", "?.", "&&", "||"):
            self.assertNotIn(ps7_op, code_blob, f"PS 7-only operator {ps7_op!r} is incompatible with PowerShell 5.1")


class TestB5PassiveCliAuthProbes(unittest.TestCase):
    def test_passive_auth_probe_claude_codex_and_no_login_spawns(self) -> None:
        from mesh.adapters.claude import ClaudeAdapter
        from mesh.adapters.codex import CodexAdapter
        from mesh.adapters.vibe import VibeAdapter

        claude = ClaudeAdapter()
        codex = CodexAdapter()
        vibe = VibeAdapter()

        # Claude logged-in via passive `claude auth status`
        with mock.patch(
            "mesh.adapters.claude.run_readonly_cmd",
            return_value=ReadonlyCmdResult(0, json.dumps({"authMethod": "claude.ai", "email": "secret@example.com"}), "", "", None),
        ) as m_claude:
            st = DesktopBridge._passive_cli_auth_probe(claude, "/usr/bin/claude")
            self.assertEqual(st["state"], "ready")
            self.assertEqual(st["method"], "subscription")
            self.assertNotIn("secret@example.com", json.dumps(st))
            called_argv = m_claude.call_args[0][0]
            self.assertEqual(called_argv, ["/usr/bin/claude", "auth", "status"])
            self.assertNotIn("login", called_argv)

        # Claude not logged in (no env keys)
        with mock.patch(
            "mesh.adapters.claude.run_readonly_cmd",
            return_value=ReadonlyCmdResult(1, json.dumps({"authMethod": "none"}), "", "", None),
        ), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            st = DesktopBridge._passive_cli_auth_probe(claude, "/usr/bin/claude")
            self.assertEqual(st["state"], "needs_login")

        # Codex passive login status (never leaks key or spawns login)
        with mock.patch(
            "mesh.adapters.codex.run_readonly_cmd",
            return_value=ReadonlyCmdResult(0, "Logged in using ChatGPT", "", "", None),
        ) as m_codex:
            st = DesktopBridge._passive_cli_auth_probe(codex, "/usr/bin/codex")
            self.assertEqual(st["state"], "ready")
            self.assertEqual(st["method"], "chatgpt")
            self.assertEqual(m_codex.call_args[0][0], ["/usr/bin/codex", "login", "status"])

        # Vibe env-key passive check never spawns any subprocess and never leaks key value
        fake_key = "mistral-live-super-secret-value-999999"
        with mock.patch("subprocess.run") as m_sub, mock.patch.dict(os.environ, {"MISTRAL_API_KEY": fake_key}):
            st = DesktopBridge._passive_cli_auth_probe(vibe, "/usr/bin/vibe")
            self.assertEqual(st["state"], "ready")
            self.assertEqual(st["method"], "env_key")
            self.assertNotIn(fake_key, json.dumps(st))
            m_sub.assert_not_called()


if __name__ == "__main__":
    unittest.main()
