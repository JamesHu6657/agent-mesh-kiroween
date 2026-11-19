"""Regression tests for Full Review Round 6 (P1-1..P1-6, P2-1..P2-5)."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from mesh.adapters.base import Probe, record_adapter_probe
from mesh.adapters.vibe import VibeAdapter, _redact_output_dir
from mesh.core import approvals, redact as redact_mod
from mesh.core.clock import now_iso_utc
from mesh.core.db import connect_db, transaction
from mesh.core.gitops import run_git
from mesh.core.init import init_mesh_home
from mesh.core.redact import LINE_CAP, StreamRedactor, redact
from mesh.core.repos import (
    add_lead_repo_worktree,
    is_registered_worktree,
    lead_repo_worktree_path,
    setup_integration,
    submission_git_info,
)
from mesh.core.workspaces import (
    WorkspaceError,
    compute_lead_workspace_path,
    compute_task_workspace_path,
    run_code_workspace_plan,
)
from mesh.daemon import lead_turns, recovery, scheduler, supervisor
from mesh.daemon.handlers.adapter import handle_adapter_list, handle_adapter_models
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.run import (
    execute_workspace_prune_plan,
    handle_workspace_list,
    handle_workspace_show,
    plan_workspace_prune,
)

MESH_PROC_LOCK = "shared"


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _stream_chunks(text: str, chunk_size: int = 97, *, exact_values=(), jsonl: bool = False) -> str:
    sr = StreamRedactor(exact_values=exact_values, jsonl=jsonl)
    out = []
    for idx in range(0, len(text), chunk_size):
        out.append(sr.feed(text[idx:idx + chunk_size]))
    out.append(sr.flush())
    return "".join(out)


def _init_git_repo(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", path], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "Test User"], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "test@example.com"], check=True, capture_output=True)
    readme = os.path.join(path, "README.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write("initial\n")
    subprocess.run(["git", "-C", path, "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "commit", "-m", "initial"], check=True, capture_output=True)
    cp = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


class TestFullReviewR6(unittest.TestCase):
    """Regression suite covering all 11 Round 6 review findings."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self._orig_testing = os.environ.get("MESH_TESTING")
        os.environ["MESH_TESTING"] = "1"

    def tearDown(self) -> None:
        if self._orig_testing is None:
            os.environ.pop("MESH_TESTING", None)
        else:
            os.environ["MESH_TESTING"] = self._orig_testing
        self._tmp.cleanup()

    def _make_home(self, name: str = "home"):
        home = os.path.join(self.tmp, name)
        info = init_mesh_home(home, "test-passphrase-1234")
        conn = connect_db(os.path.join(info["home"], "mesh.db"))
        self.addCleanup(conn.close)
        return info["home"], conn, info["user_id"]

    # ------------------------------------------------------------------
    # P1-1: _json_line early-return / exception / nested cross-line state
    # ------------------------------------------------------------------
    def test_p1_1_json_max_depth_preserves_cross_line_armed_and_inkey(self) -> None:
        inner_pw = '{"prompt": "password:"}\nsuper_secret_hunter2_value'
        wrapped_pw = inner_pw
        for _ in range(4):
            wrapped_pw = json.dumps({"wrap": wrapped_pw})

        out_pw = redact(wrapped_pw, exact_values=(), jsonl=True)
        self.assertNotIn("super_secret_hunter2_value", out_pw)
        self.assertEqual(
            _stream_chunks(wrapped_pw, 53, exact_values=(), jsonl=True),
            out_pw,
        )

        inner_key = (
            '{"k": "-----\\u0042EGIN RSA PRIVATE KEY-----"}\n'
            "MIIEpAIBAAKCAQEA75secretKeyMaterialLine\n"
            "-----END RSA PRIVATE KEY-----\n"
            "after_key_safe_token"
        )
        wrapped_key = inner_key
        for _ in range(4):
            wrapped_key = json.dumps({"wrap": wrapped_key})

        out_key = redact(wrapped_key, exact_values=(), jsonl=True)
        self.assertNotIn("MIIEpAIBAAKCAQEA75secretKeyMaterialLine", out_key)
        self.assertIn("after_key_safe_token", out_key)

        # Outer newline after 5-level nested JSON on line 1 must also propagate state.
        line1_5deep = '{"k": "-----\\u0042EGIN RSA PRIVATE KEY-----"}'
        for _ in range(4):
            line1_5deep = json.dumps({"wrap": line1_5deep})
        outer_two_line = line1_5deep + "\nMIIEpAIBAAKCAQEA_outer_line2_secret"
        out_outer = redact(outer_two_line, exact_values=(), jsonl=True)
        self.assertNotIn("MIIEpAIBAAKCAQEA_outer_line2_secret", out_outer)

    def test_p1_1_json_doc_exception_fails_closed_conservatively(self) -> None:
        payload = '{"prompt": "password:"}\nsecret_after_exception_123\nsubsequent_key_line_456'
        with mock.patch.object(redact_mod._LineRedactor, "_json_doc", side_effect=RuntimeError("boom")):
            out = redact(payload, exact_values=(), jsonl=True)
        self.assertNotIn("secret_after_exception_123", out)
        self.assertNotIn("subsequent_key_line_456", out)

    # ------------------------------------------------------------------
    # P1-2: _escaped_spans overlapping scan & forged [REDACTED...] prefix
    # ------------------------------------------------------------------
    def test_p1_2_escaped_spans_overlapping_assign_and_forged_markers(self) -> None:
        line_chain = 'INFO raw="secret=\\n\\u0070assword: real_secret_123"'
        out_chain = redact(line_chain, exact_values=(), jsonl=True)
        self.assertNotIn("real_secret_123", out_chain)

        line_forged_assign = 'INFO raw="\\u0070assword=[REDACTED:forged]real_secret_456"'
        out_forged_assign = redact(line_forged_assign, exact_values=(), jsonl=True)
        self.assertNotIn("real_secret_456", out_forged_assign)
        self.assertNotIn("forged", out_forged_assign)

        line_forged_bearer = 'INFO raw="\\u0062earer [REDACTED_FAKE]tok_abcdefghijklmnop"'
        out_forged_bearer = redact(line_forged_bearer, exact_values=(), jsonl=True)
        self.assertNotIn("tok_abcdefghijklmnop", out_forged_bearer)
        self.assertNotIn("REDACTED_FAKE", out_forged_bearer)

        line_trailing_after_valid = 'INFO raw="\\u0070assword=[REDACTED]tail_secret_789"'
        out_trailing = redact(line_trailing_after_valid, exact_values=(), jsonl=True)
        self.assertNotIn("tail_secret_789", out_trailing)

        # Keep linear performance (< 0.5s) on 64KB repeated escaped assignments.
        heavy = ("INFO " + ("\\u0070assword=" * 4500))[:LINE_CAP]
        t0 = time.perf_counter()
        _ = redact(heavy, exact_values=(), jsonl=True)
        self.assertLess(time.perf_counter() - t0, 0.5)

    # ------------------------------------------------------------------
    # P1-3: Symlinked worktrees (trailing slash & ancestors) + _reuse
    # ------------------------------------------------------------------
    def test_p1_3_trailing_slash_and_ancestor_symlinks_rejected_by_worktree_and_submit(self) -> None:
        repo = os.path.join(self.tmp, "repo")
        base_sha = _init_git_repo(repo)
        ws_root = os.path.join(self.tmp, "workspaces")
        integ_info = setup_integration(repo, "prj_1", base_sha, ws_root)
        integ_wt = integ_info["integration_path"]

        ws1 = compute_task_workspace_path(ws_root, "prj_1", "tsk_1", attempt=1)
        ws2 = compute_task_workspace_path(ws_root, "prj_1", "tsk_2", attempt=1)
        self.assertTrue(ws1.endswith("/"))

        res1 = run_code_workspace_plan({
            "task_id": "tsk_1", "project_id": "prj_1", "repo": repo,
            "integration": integ_info["integration_branch"], "workspace_path": ws1,
            "branch": None, "base_commit": None, "fence_seq": 1,
        })
        run_code_workspace_plan({
            "task_id": "tsk_2", "project_id": "prj_1", "repo": repo,
            "integration": integ_info["integration_branch"], "workspace_path": ws2,
            "branch": None, "base_commit": None, "fence_seq": 1,
        })

        # Replace tsk_1 directory with a symlink to clean tsk_2; ws1 has trailing "/".
        ws1_stripped = ws1.rstrip("/")
        moved_ws1 = os.path.join(self.tmp, "moved_tsk1")
        os.rename(ws1_stripped, moved_ws1)
        os.symlink(ws2.rstrip("/"), ws1_stripped)

        self.assertFalse(is_registered_worktree(ws1, integ_wt))
        git_info = submission_git_info(ws1, res1["branch"], res1["base_commit"], repo_path=repo)
        self.assertIn("error", git_info)
        self.assertEqual(git_info["error"]["reason"], "foreign_repository")

        # Also test workspaces._reuse rejecting a fake .git / commondir spoof without trusting it.
        os.unlink(ws1_stripped)
        os.makedirs(ws1_stripped, exist_ok=True)
        fake_gitdir = os.path.join(self.tmp, "fake_gitdir")
        os.makedirs(fake_gitdir, exist_ok=True)
        with open(os.path.join(fake_gitdir, "HEAD"), "w", encoding="utf-8") as f:
            f.write(f"ref: refs/heads/{res1['branch']}\n")
        with open(os.path.join(fake_gitdir, "commondir"), "w", encoding="utf-8") as f:
            f.write(os.path.join(repo, ".git") + "\n")
        pwn_marker = os.path.join(self.tmp, "reuse_fsmonitor_pwned")
        with open(os.path.join(fake_gitdir, "config"), "w", encoding="utf-8") as f:
            f.write(f"[core]\n\trepositoryformatversion = 0\n\tfsmonitor = touch {pwn_marker}\n")
        with open(os.path.join(ws1_stripped, ".git"), "w", encoding="utf-8") as f:
            f.write(f"gitdir: {fake_gitdir}\n")

        with self.assertRaises(WorkspaceError) as ctx:
            run_code_workspace_plan({
                "task_id": "tsk_1", "project_id": "prj_1", "repo": repo,
                "integration": integ_info["integration_branch"], "workspace_path": ws1,
                "branch": res1["branch"], "base_commit": res1["base_commit"], "fence_seq": 2,
            })
        self.assertEqual(ctx.exception.details.get("reason"), "foreign_repository")
        self.assertFalse(os.path.exists(pwn_marker))

    def test_p1_3_lead_repo_refresh_rejects_symlinked_lead_dir(self) -> None:
        home, _conn, _uid = self._make_home("home_lead")
        repo = os.path.join(self.tmp, "repo_lead")
        base_sha = _init_git_repo(repo)
        ws_root = os.path.join(home, "workspaces")
        setup_integration(repo, "prj_l", base_sha, ws_root)

        # Lead 2 has a valid clean repo/ worktree.
        ag2_repo = add_lead_repo_worktree(repo, "prj_l", ws_root, "ag_lead2")
        self.assertTrue(os.path.isdir(ag2_repo))

        # Case 1: Lead 1's workspace dir is a symlink to external dir (ext_dir/repo missing).
        ext_dir = os.path.join(self.tmp, "ext_outside")
        os.makedirs(ext_dir, exist_ok=True)
        ag1_ws = compute_lead_workspace_path(ws_root, "prj_l", "ag_lead1").rstrip("/")
        os.makedirs(os.path.dirname(ag1_ws), exist_ok=True)
        os.symlink(ext_dir, ag1_ws)

        plan1 = {
            "repo": repo,
            "project_id": "prj_l",
            "agent_id": "ag_lead1",
            "assignment_id": "la_1",
            "ws_root": ws_root,
            "path": lead_repo_worktree_path(ws_root, "prj_l", "ag_lead1"),
            "branch": "mesh/prj_l/integration",
        }
        lead_turns.run_lead_repo_refresh(home, plan1)
        self.assertFalse(os.path.exists(os.path.join(ext_dir, "repo")))

        # Case 2: Lead 1's workspace dir is a symlink to Lead 2's workspace dir.
        os.unlink(ag1_ws)
        ag2_ws = compute_lead_workspace_path(ws_root, "prj_l", "ag_lead2").rstrip("/")
        os.symlink(ag2_ws, ag1_ws)
        lead_turns.run_lead_repo_refresh(home, plan1)
        self.assertTrue(os.path.isdir(ag2_repo))
        self.assertTrue(is_registered_worktree(ag2_repo, repo))

    # ------------------------------------------------------------------
    # P1-4: run_git preserves GIT_CONFIG_COUNT identity & strips GIT_CONFIG_PARAMETERS
    # ------------------------------------------------------------------
    def test_p1_4_run_git_preserves_caller_git_config_and_strips_parameters(self) -> None:
        repo = os.path.join(self.tmp, "repo_k8")
        os.makedirs(repo, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
        empty_home = os.path.join(self.tmp, "empty_home")
        os.makedirs(empty_home, exist_ok=True)
        pwn_file = os.path.join(self.tmp, "fsmonitor_param_pwn")

        with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8") as f:
            f.write(f"\n[core]\n\tfsmonitor = touch {pwn_file}\n")

        env_patch = {
            "HOME": empty_home,
            "XDG_CONFIG_HOME": empty_home,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "K8EnvIdentity",
            "GIT_CONFIG_KEY_1": "user.email",
            "GIT_CONFIG_VALUE_1": "k8env@example.com",
            "GIT_CONFIG_PARAMETERS": f"'core.fsmonitor=touch {pwn_file}'",
        }
        removed_keys = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
        with mock.patch.dict(os.environ, env_patch, clear=False):
            for k in removed_keys:
                os.environ.pop(k, None)
            st = run_git(["status", "--porcelain"], cwd=repo)
            self.assertEqual(st.returncode, 0, st.stderr)
            self.assertFalse(os.path.exists(pwn_file))

            cm = run_git(["commit", "--allow-empty", "-m", "k8 env commit"], cwd=repo)
            self.assertEqual(cm.returncode, 0, cm.stderr)
            log = run_git(["log", "-1", "--format=%an <%ae>"], cwd=repo)
            self.assertEqual(log.stdout.strip(), "K8EnvIdentity <k8env@example.com>")
            self.assertFalse(os.path.exists(pwn_file))

    # ------------------------------------------------------------------
    # P1-5: VibeAdapter._redact_output_dir JSON keys, dup keys, subdirs, symlink, spawn_failed
    # ------------------------------------------------------------------
    def test_p1_5_vibe_redact_output_dir_json_keys_dups_subdirs_and_symlink(self) -> None:
        run_dir = os.path.join(self.tmp, "vibe_run1")
        vibe_out = os.path.join(run_dir, "vibe-out")
        sub_dir = os.path.join(vibe_out, "nested", "deep")
        os.makedirs(sub_dir, exist_ok=True)
        with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write("")

        raw_export = (
            '{\n'
            '  "session_id": "ses_123",\n'
            '  "outcome": "completed",\n'
            '  "api_key": "vibe_raw_api_secret_val",\n'
            '  "endpoint": "postgres://dbuser:super_db_pass@localhost:5432/app",\n'
            '  "nested_json": "{\\"password\\": \\"inner_json_pw_999\\"}",\n'
            '  "dup": "sk-ant-api03-1234567890abcdefghij",\n'
            '  "dup": "clean"\n'
            '}\n'
        )
        export_path = os.path.join(vibe_out, "export.json")
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(raw_export)

        trace_path = os.path.join(sub_dir, "trace.txt")
        with open(trace_path, "w", encoding="utf-8") as f:
            f.write("password=subdir_secret_value_321\n")

        VibeAdapter().parse(run_dir)

        red_export = _read_text(export_path)
        self.assertNotIn("vibe_raw_api_secret_val", red_export)
        self.assertNotIn("super_db_pass", red_export)
        self.assertNotIn("inner_json_pw_999", red_export)
        self.assertNotIn("sk-ant-api03-1234567890abcdefghij", red_export)
        json.loads(red_export)

        red_trace = _read_text(trace_path)
        self.assertNotIn("subdir_secret_value_321", red_trace)

        # Symlinked vibe-out must NOT traverse or mutate external target directory.
        ext_target = os.path.join(self.tmp, "external_victim")
        os.makedirs(ext_target, exist_ok=True)
        victim_file = os.path.join(ext_target, "keep.txt")
        with open(victim_file, "w", encoding="utf-8") as f:
            f.write("password=victim_external_data_must_not_change\n")
        victim_link = os.path.join(ext_target, "keep_link")
        os.symlink(victim_file, victim_link)

        run_dir2 = os.path.join(self.tmp, "vibe_run2")
        os.makedirs(run_dir2, exist_ok=True)
        vibe_out_link = os.path.join(run_dir2, "vibe-out")
        os.symlink(ext_target, vibe_out_link)
        _redact_output_dir(vibe_out_link + "/")

        self.assertFalse(os.path.lexists(vibe_out_link))
        self.assertTrue(os.path.islink(victim_link))
        self.assertIn("victim_external_data_must_not_change", _read_text(victim_file))

    def test_p1_5_spawn_failed_closes_with_vibe_output_redacted(self) -> None:
        home, conn, user_id = self._make_home("home_vibe_sf")
        now = now_iso_utc()
        run_dir3 = os.path.join(home, "runs", "run_vibe_sf")
        os.makedirs(os.path.join(run_dir3, "vibe-out"), exist_ok=True)
        sf_secret_file = os.path.join(run_dir3, "vibe-out", "partial.json")
        with open(sf_secret_file, "w", encoding="utf-8") as f:
            f.write('{"api_key": "spawn_failed_vibe_secret_777"}\n')
        with transaction(conn):
            conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_v', 'pv', 'g', 'active', ?, 'direct', ?, ?);",
                (user_id, now, now),
            )
            conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('ag_v', 'agent', 'av', ?);",
                (now,),
            )
            conn.execute(
                "INSERT INTO agents (id, delivery, adapter, max_concurrency, created_at, updated_at) "
                "VALUES ('ag_v', 'launch', 'vibe', 1, ?, ?);",
                (now, now),
            )
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, created_by, created_at, updated_at) "
                "VALUES ('tsk_v', 'prj_v', 't', 's', 'general', 'running', 'ag_v', 'edit', 60, 1, ?, ?, ?);",
                (user_id, now, now),
            )
            conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, profile, "
                "status, boot_id, run_dir, started_at) "
                "VALUES ('run_vibe_sf', 'task', 'tsk_v', 'prj_v', 'ag_v', 1, 1, 'vibe', 'edit', "
                "'starting', 'boot_1', ?, ?);",
                (run_dir3, now),
            )
            run_row = conn.execute("SELECT * FROM runs WHERE id = 'run_vibe_sf';").fetchone()
            supervisor._close_run(
                conn, run_row, "spawn_failed", "spawn_failed", None,
                {"reason": "spawn_failed"}, gate=False, testing_mode=True,
            )
        self.assertNotIn("spawn_failed_vibe_secret_777", _read_text(sf_secret_file))

    # ------------------------------------------------------------------
    # P1-6: workspace prune detached HEAD with branch commits & untrusted ws
    # ------------------------------------------------------------------
    def test_p1_6_workspace_prune_preserves_cancelled_detached_head_with_branch_commits(self) -> None:
        home, conn, user_id = self._make_home("home_prune")
        repo = os.path.join(self.tmp, "repo_prune")
        base_sha = _init_git_repo(repo)
        ws_root = os.path.join(home, "workspaces")
        integ_info = setup_integration(repo, "prj_p", base_sha, ws_root)

        ws_det = compute_task_workspace_path(ws_root, "prj_p", "tsk_det", attempt=1)
        prep_det = run_code_workspace_plan({
            "task_id": "tsk_det", "project_id": "prj_p", "repo": repo,
            "integration": integ_info["integration_branch"], "workspace_path": ws_det,
            "branch": None, "base_commit": None, "fence_seq": 1,
        })
        # Commit unmerged work on task branch, then detach HEAD back to base_commit.
        with open(os.path.join(ws_det, "feature.py"), "w", encoding="utf-8") as f:
            f.write("x = 42\n")
        subprocess.run(["git", "-C", ws_det, "add", "feature.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", ws_det, "commit", "-m", "unmerged work"], check=True, capture_output=True)
        subprocess.run(["git", "-C", ws_det, "checkout", "--detach", base_sha], check=True, capture_output=True)
        with open(os.path.join(repo, ".git", "info", "exclude"), "a", encoding="utf-8") as f:
            f.write("\nignored_build.bin\n")
        local_artifact = os.path.join(ws_det, "ignored_build.bin")
        with open(local_artifact, "w", encoding="utf-8") as f:
            f.write("precious local artifact\n")

        # Second cancelled task whose workspace was replaced by a spoofed directory.
        ws_spoof = compute_task_workspace_path(ws_root, "prj_p", "tsk_spoof", attempt=1)
        prep_spoof = run_code_workspace_plan({
            "task_id": "tsk_spoof", "project_id": "prj_p", "repo": repo,
            "integration": integ_info["integration_branch"], "workspace_path": ws_spoof,
            "branch": None, "base_commit": None, "fence_seq": 1,
        })
        shutil.rmtree(ws_spoof.rstrip("/"))
        os.makedirs(ws_spoof.rstrip("/"), exist_ok=True)
        pwn_prune = os.path.join(self.tmp, "prune_spoof_pwn")
        subprocess.run(["git", "init", "-b", "main", ws_spoof.rstrip("/")], check=True, capture_output=True)
        with open(os.path.join(ws_spoof.rstrip("/"), ".git", "config"), "a", encoding="utf-8") as f:
            f.write(f"\n[core]\n\tfsmonitor = touch {pwn_prune}\n")

        now = now_iso_utc()
        with transaction(conn):
            conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
                "test_command_json, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_p', 'pp', 'g', 'active', ?, 'main', ?, '\"none\"', ?, 'direct', ?, ?);",
                (repo, integ_info["integration_branch"], user_id, now, now),
            )
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, base_commit, created_by, created_at, updated_at) "
                "VALUES ('tsk_det', 'prj_p', 't1', 's', 'code', 'cancelled', 'edit', 60, 1, ?, ?, ?, ?, ?, ?);",
                (ws_det, prep_det["branch"], prep_det["base_commit"], user_id, now, now),
            )
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, base_commit, created_by, created_at, updated_at) "
                "VALUES ('tsk_spoof', 'prj_p', 't2', 's', 'code', 'cancelled', 'edit', 60, 1, ?, ?, ?, ?, ?, ?);",
                (ws_spoof, prep_spoof["branch"], prep_spoof["base_commit"], user_id, now, now),
            )

        ctx = HandlerContext(
            conn=conn,
            principal={"id": user_id, "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_prune_r6",
            home=home,
            cmd="workspace.prune",
            args={"project": "prj_p"},
            testing_mode=True,
        )
        res = execute_workspace_prune_plan(plan_workspace_prune(ctx))
        self.assertEqual(res["removed"], [])
        reasons = {entry["task_id"]: entry["reason"] for entry in res["skipped"]}
        self.assertEqual(reasons.get("tsk_det"), "unmerged_commits")
        self.assertEqual(reasons.get("tsk_spoof"), "commits_unknown")
        self.assertTrue(os.path.isdir(ws_det.rstrip("/")))
        self.assertTrue(os.path.isfile(local_artifact))
        self.assertFalse(os.path.exists(pwn_prune))

    # ------------------------------------------------------------------
    # P2-1: Non-JSON LINE_CAP output enforcement, idempotency & 64KB speed
    # ------------------------------------------------------------------
    def test_p2_1_line_cap_enforced_after_redaction_idempotent_and_64kb_fast(self) -> None:
        near_cap = "x" * (LINE_CAP - 12) + " password: a\nfollowing_tok rest_of_line"
        self.assertEqual(len(near_cap.splitlines()[0]), LINE_CAP)

        first = redact(near_cap, exact_values=())
        for out_line in first.splitlines():
            self.assertLessEqual(len(out_line), LINE_CAP)
        second = redact(first, exact_values=())
        self.assertEqual(second, first)
        self.assertEqual(_stream_chunks(near_cap, 137, exact_values=()), first)

        # 64KB JSON with thousands of escaped short leaves + exact values must run < 0.5s.
        pairs = [(f"TOK_{i}", f"secret_exact_val_{i:04d}_xyz") for i in range(25)]
        many_leaves = "[" + ",".join(['"\\u0061"'] * 5400) + "]"
        self.assertLessEqual(len(many_leaves), LINE_CAP)
        t0 = time.perf_counter()
        out_fast = redact(many_leaves, exact_values=pairs, jsonl=True)
        elapsed = time.perf_counter() - t0
        self.assertEqual(out_fast, many_leaves)
        self.assertLess(elapsed, 0.5)

    # ------------------------------------------------------------------
    # P2-2: stored_op_json convergence on cascading near-LINE_CAP lines + fail-closed
    # ------------------------------------------------------------------
    def test_p2_2_stored_op_json_converges_on_cascading_near_cap_lines_and_fallback(self) -> None:
        cascading_spec = "\n".join(["x" * (LINE_CAP - 12) + " password: a"] * 6)
        op_doc = {
            "v": 1,
            "op": "task.create",
            "requested_by": "ag_req",
            "project": "prj_1",
            "target": None,
            "params": {"title": "big", "spec": cascading_spec},
            "category": "scope_change",
        }
        op_hash = "sha256:" + "ab" * 32
        stored = approvals.stored_op_json(op_doc, op_hash)
        row = {
            "op_json": stored,
            "op_type": "task.create",
            "requested_by": "ag_req",
            "project_id": "prj_1",
            "category": "scope_change",
        }
        self.assertEqual(approvals.verify_stored_op_json(row), op_hash)

        # Simulate pathological non-convergence inside stored_op_json's loop and verify
        # fail-closed output is still a valid fixed point accepted by verify_stored_op_json.
        real_redact_value = approvals.redact_value
        calls = [0]

        def oscillating_redact(val, exact_values=None):
            calls[0] += 1
            if exact_values == ():
                return {"v": 1, "op": "task.create", "requested_by": "ag_req", "project": "prj_1",
                        "target": None, "params": {"spec": f"osc_{calls[0]} password: x"},
                        "category": "scope_change"}
            return real_redact_value(val, exact_values)

        with mock.patch.object(approvals, "redact_value", side_effect=oscillating_redact):
            fallback_stored = approvals.stored_op_json(
                {**op_doc, "params": {"spec": "password: secret_init", "gates": ["config"]}},
                op_hash,
            )
        row_fb = dict(row, op_json=fallback_stored)
        self.assertEqual(approvals.verify_stored_op_json(row_fb), op_hash)
        self.assertNotIn("secret_init", fallback_stored)

    # ------------------------------------------------------------------
    # P2-3: adapter.list, adapter.models & record_adapter_probe redaction
    # ------------------------------------------------------------------
    def test_p2_3_adapter_list_models_and_probe_db_redacted(self) -> None:
        home, conn, user_id = self._make_home("home_adapter")
        secret_ver = "1.0.0 Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        secret_flag = "--dsn=postgres://admin:flag_secret_pw@db.local/prod"
        secret_model = "model-err sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234"
        probe = Probe(
            adapter="vibe",
            version=secret_ver,
            status="error",
            launch_enabled=False,
            launch_milestone="M3",
            capabilities={},
            flags=[secret_flag],
            models=[secret_model],
            binary_path="/usr/bin/vibe",
        )
        with transaction(conn):
            record_adapter_probe(conn, "vibe", probe, "/usr/bin/vibe")
        db_row = conn.execute("SELECT * FROM adapter_probes WHERE adapter = 'vibe';").fetchone()
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", db_row["version"] or "")
        self.assertNotIn("flag_secret_pw", db_row["flags_json"] or "")
        self.assertNotIn("sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234", db_row["models_json"] or "")

        ctx_list = HandlerContext(
            conn=conn,
            principal={"id": user_id, "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_alist",
            home=home,
            cmd="adapter.list",
            args={},
            testing_mode=True,
        )
        fake_adapter = VibeAdapter()
        setattr(ctx_list, "_prepared_adapter", ("list", [(fake_adapter, probe)], None))
        with transaction(conn):
            res_list = handle_adapter_list(ctx_list, testing_mode=True)
        serialized_list = json.dumps(res_list.data)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", serialized_list)

        ctx_models = HandlerContext(
            conn=conn,
            principal={"id": user_id, "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_amodels",
            home=home,
            cmd="adapter.models",
            args={"adapter": "vibe"},
            testing_mode=True,
        )
        setattr(
            ctx_models,
            "_prepared_adapter",
            ("models", ("vibe", [secret_model, "postgres://u:model_url_pw@host/db"]), None),
        )
        res_models = handle_adapter_models(ctx_models, testing_mode=True)
        serialized_models = json.dumps(res_models.data)
        self.assertNotIn("sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234", serialized_models)
        self.assertNotIn("model_url_pw", serialized_models)

    # ------------------------------------------------------------------
    # P2-4: workspace.show rejects symlinks and paths outside workspaces.root
    # ------------------------------------------------------------------
    def test_p2_4_workspace_show_rejects_symlink_and_outside_root(self) -> None:
        home, conn, user_id = self._make_home("home_wsshow")
        ext_secret_dir = os.path.join(self.tmp, "ext_secret_dir")
        os.makedirs(ext_secret_dir, exist_ok=True)
        with open(os.path.join(ext_secret_dir, "id_ed25519"), "w", encoding="utf-8") as f:
            f.write("secret key file")

        ws_root = os.path.join(home, "workspaces")
        ws_link = compute_task_workspace_path(ws_root, "prj_s", "tsk_sym", attempt=1)
        os.makedirs(os.path.dirname(ws_link.rstrip("/")), exist_ok=True)
        os.symlink(ext_secret_dir, ws_link.rstrip("/"))

        now = now_iso_utc()
        with transaction(conn):
            conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_s', 'ps', 'g', 'active', ?, 'direct', ?, ?);",
                (user_id, now, now),
            )
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, created_by, created_at, updated_at) "
                "VALUES ('tsk_sym', 'prj_s', 't1', 's', 'general', 'running', 'edit', 60, 1, ?, ?, ?, ?);",
                (ws_link, user_id, now, now),
            )
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, created_by, created_at, updated_at) "
                "VALUES ('tsk_out', 'prj_s', 't2', 's', 'general', 'running', 'edit', 60, 1, ?, ?, ?, ?);",
                (ext_secret_dir, user_id, now, now),
            )

        for tid in ("tsk_sym", "tsk_out"):
            ctx = HandlerContext(
                conn=conn,
                principal={"id": user_id, "kind": "user", "name": "user"},
                actor_kind="user",
                via="direct",
                request_id=f"req_show_{tid}",
                home=home,
                cmd="workspace.show",
                args={"task_id": tid},
                testing_mode=True,
            )
            res = handle_workspace_show(ctx)
            self.assertFalse(res.data["exists"], tid)
            self.assertEqual(res.data["files"], [], tid)

        ctx_list = HandlerContext(
            conn=conn,
            principal={"id": user_id, "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_list_ws",
            home=home,
            cmd="workspace.list",
            args={"project": "prj_s"},
            testing_mode=True,
        )
        res_list = handle_workspace_list(ctx_list)
        by_id = {w["task_id"]: w for w in res_list.data["workspaces"] if w["kind"] == "task"}
        self.assertFalse(by_id["tsk_sym"]["exists"])
        self.assertFalse(by_id["tsk_out"]["exists"])

    # ------------------------------------------------------------------
    # P2-5: recover() resets offered_at on queued inbox tasks
    # ------------------------------------------------------------------
    def test_p2_5_recover_resets_queued_inbox_offered_at_after_downtime(self) -> None:
        home, conn, user_id = self._make_home("home_rec_inbox")
        old_ts = now_iso_utc(time.time() - 7200)  # 2 hours ago (> 600s default ack_timeout_s)
        now = now_iso_utc()
        with transaction(conn):
            conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_in', 'pin', 'g', 'active', ?, 'direct', ?, ?);",
                (user_id, now, now),
            )
            conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('ag_in', 'agent', 'inbox_worker', ?);",
                (now,),
            )
            conn.execute(
                "INSERT INTO agents (id, delivery, adapter, max_concurrency, created_at, updated_at) "
                "VALUES ('ag_in', 'inbox', 'none', 2, ?, ?);",
                (now, now),
            )
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, queued_at, offered_at, created_by, created_at, updated_at) "
                "VALUES ('tsk_in_q', 'prj_in', 'inbox task', 's', 'general', 'queued', 'ag_in', 'edit', "
                "60, 1, ?, ?, ?, ?, ?);",
                (old_ts, old_ts, user_id, old_ts, old_ts),
            )

        before_recover = time.time()
        recovery.recover(conn, home, "boot_after_downtime", testing_mode=True)

        row_after = conn.execute("SELECT status, offered_at FROM tasks WHERE id = 'tsk_in_q';").fetchone()
        self.assertEqual(row_after["status"], "queued")
        self.assertNotEqual(row_after["offered_at"], old_ts)
        self.assertGreaterEqual(row_after["offered_at"], now_iso_utc(before_recover - 1))

        with transaction(conn):
            failed = scheduler.check_queued_tasks_t6(conn, home, testing_mode=True)
        self.assertEqual(failed, [])
        row_tick = conn.execute("SELECT status FROM tasks WHERE id = 'tsk_in_q';").fetchone()
        self.assertEqual(row_tick["status"], "queued")


if __name__ == "__main__":
    unittest.main()
