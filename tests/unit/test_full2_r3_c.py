"""Regression tests for full2-r3.md Group C findings (P1-4, P2-1, P2-4, P2-6, P2-11)."""

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import gitops, repos
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.auth import generate_agent_token
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.daemon import recovery
from mesh.daemon.handlers import project as project_handlers
from mesh.daemon.handlers import run as run_handlers
from mesh.daemon.handlers.approval import handle_approval_approve
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.server import DaemonServer, dispatch_internal_request
from mesh.errors import MeshError
from tests.gitfixture import GitFixture


def _setup_home_and_repo(tmp: str, fixture: GitFixture):
    home = os.path.join(tmp, "home")
    for sub in ("run", "logs", "runs", "merges", "workspaces"):
        os.makedirs(os.path.join(home, sub), mode=0o700, exist_ok=True)
    repo = os.path.realpath(fixture.make_k0("R3C"))
    base_sha = fixture.head(repo, "main")
    ws_root = os.path.realpath(os.path.join(home, "workspaces"))
    integ_info = repos.setup_integration(repo, "prj_1", base_sha, ws_root)
    conn = init_db(os.path.join(home, "mesh.db"))
    now = now_iso_utc()
    with transaction(conn):
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
            (now,),
        )
        for aid, aname, can_lead, caps in (
            ("agt_lead", "lead1", 1, "[]"),
            ("agt_lead2", "lead2", 1, "[]"),
            ("agt_other", "other", 1, "[]"),
            ("agt_plain", "plain", 0, "[]"),
        ):
            conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, ?);",
                (aid, aname, now),
            )
            conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
                "default_profile, created_at, updated_at) VALUES (?, 'none', 'inbox', ?, ?, 1, 'edit', ?, ?);",
                (aid, can_lead, caps, now, now),
            )
        conn.execute(
            "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
            "base_commit, created_by, created_via, version, created_at, updated_at) "
            "VALUES ('prj_1', 'P1', 'goal', 'active', ?, 'main', ?, ?, 'usr_1', 'direct', 1, ?, ?);",
            (repo, integ_info["integration_branch"], base_sha, now, now),
        )
        conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('asg_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
            (now,),
        )
    return home, conn, repo, base_sha, integ_info


def _ctx(
    conn: sqlite3.Connection,
    home: str,
    cmd: str,
    args: dict,
    *,
    principal: dict | None = None,
    actor_kind: str = "user",
    via: str = "direct",
    approval: str | None = None,
    request_id: str = "req_r3c",
    k_user: bytes | None = b"\x04" * 32,
    dry_run: bool = False,
) -> HandlerContext:
    p = principal or ({"id": "usr_1", "kind": "user", "name": "user"} if actor_kind == "user"
                      else {"id": "agt_lead", "kind": "agent", "name": "lead1", "capabilities": []})
    return HandlerContext(
        conn=conn,
        principal=p,
        actor_kind=actor_kind,
        via=via,
        request_id=request_id,
        home=home,
        cmd=cmd,
        args=args,
        approval=approval,
        k_user=k_user if actor_kind == "user" else None,
        dry_run=dry_run,
        testing_mode=True,
    )


def _create_approved_approval(
    conn: sqlite3.Connection,
    home: str,
    *,
    category: str,
    op: str,
    requested_by: str,
    project_id: str,
    target_id: str,
    params: dict,
) -> str:
    with transaction(conn):
        apr = create_or_get_pending_approval(
            conn=conn,
            category=category,
            op=op,
            requested_by=requested_by,
            params=params,
            summary=f"{op} {target_id}",
            project_id=project_id,
            target_id=target_id,
            testing_mode=True,
        )
        handle_approval_approve(
            _ctx(
                conn,
                home,
                "approval.approve",
                {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
            )
        )
    return apr["approval_id"]


class _GitEnvMixin:
    def _init_git_env(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[key]
        os.environ.update(self.fixture.git_env_vars())


class TestP1_4DeliverIntentAndWorktreeSafety(unittest.TestCase, _GitEnvMixin):
    """P1-4: pre-check failure leaves no deliver-intent; reconcile scoping; worktree & git-state guards."""

    def setUp(self) -> None:
        self._init_git_env()
        self._tmp = tempfile.TemporaryDirectory(prefix="mh-r3c-p14-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.fixture.allow_root(self._tmp.name)
        self.home, self.conn, self.repo, self.base_sha, self.integ = _setup_home_and_repo(
            self._tmp.name, self.fixture
        )
        self.addCleanup(self.conn.close)

    def _commit_on_integ(self, fname: str = "feat.txt", content: str = "f\n") -> str:
        wt = self.integ["integration_path"]
        with open(os.path.join(wt, fname), "w", encoding="utf-8") as f:
            f.write(content)
        self.fixture.git(wt, "add", fname)
        self.fixture.git(wt, "commit", "-q", "-m", f"add {fname}", identity=True)
        return self.fixture.head(wt)

    def test_deliver_precheck_base_moved_leaves_no_intent_and_never_consumes_approval(self) -> None:
        new_sha = self._commit_on_integ()
        self.fixture.git(self.repo, "checkout", "-q", "--detach", self.base_sha)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('approval.policy.deliver_to_base', '\"direct_or_relayed\"', 'now', 'usr_1');"
            )
        apr_id = _create_approved_approval(
            self.conn,
            self.home,
            category="deliver_to_base",
            op="project.deliver",
            requested_by="agt_lead",
            project_id="prj_1",
            target_id="prj_1",
            params={"project_id": "prj_1"},
        )

        # Simulate the user manually advancing main to new_sha between deliver_heads() and deliver_to_base().
        real_deliver_heads = repos.deliver_heads
        moved = []

        def _heads_then_external_move(repo: str, base_b: str, integ_b: str):
            res = real_deliver_heads(repo, base_b, integ_b)
            if not moved:
                moved.append(True)
                self.fixture.git(self.repo, "update-ref", "refs/heads/main", new_sha, self.base_sha)
            return res

        intent_path = repos.deliver_intent_path(self.home, "prj_1")
        ctx = _ctx(
            self.conn,
            self.home,
            "project.deliver",
            {"project_id": "prj_1"},
            actor_kind="agent",
            via="token",
            approval=apr_id,
        )
        with mock.patch("mesh.core.repos.deliver_heads", side_effect=_heads_then_external_move):
            with self.assertRaises(MeshError) as cm:
                with transaction(self.conn):
                    project_handlers.handle_project_deliver(ctx)
        self.assertEqual(cm.exception.code, c.VERSION_MISMATCH)
        self.assertFalse(os.path.lexists(intent_path), "failed deliver precheck leaked deliver-intent on disk")

        # Subsequent settle must NOT consume the approval or forge project.delivered.
        recovery.settle_deliver_intent(self.conn, self.home, "prj_1")
        self.assertEqual(
            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_id,)).fetchone()["status"],
            "approved",
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'project.delivered';").fetchone()[0],
            0,
        )

    def test_deliver_internal_request_precheck_failure_and_lost_tx_recovery(self) -> None:
        new_sha = self._commit_on_integ()
        self.fixture.git(self.repo, "checkout", "-q", "--detach", self.base_sha)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('approval.policy.deliver_to_base', '\"direct_or_relayed\"', 'now', 'usr_1');"
            )
        apr_id = _create_approved_approval(
            self.conn,
            self.home,
            category="deliver_to_base",
            op="project.deliver",
            requested_by="agt_lead",
            project_id="prj_1",
            target_id="prj_1",
            params={"project_id": "prj_1"},
        )
        lead_principal = {
            "id": "agt_lead",
            "name": "lead1",
            "kind": "agent",
            "capabilities": [],
            "scope": {"role": "lead", "project_id": "prj_1", "lead_assignment_id": "asg_1", "run_id": "run_l"},
        }
        ctx_fail = _ctx(
            self.conn,
            self.home,
            "project.deliver",
            {"project_id": "prj_1"},
            principal=lead_principal,
            actor_kind="agent",
            via="run_token",
            approval=apr_id,
            request_id="req_int_fail",
        )
        real_deliver_heads = repos.deliver_heads
        # Move base branch to a descendant of new_sha externally during precheck window.
        child_sha = self.fixture.git(
            self.repo,
            "commit-tree",
            self.fixture.git(self.repo, "rev-parse", f"{new_sha}^{{tree}}").stdout.strip(),
            "-p",
            new_sha,
            "-m",
            "user on top",
            identity=True,
        ).stdout.strip()

        moved_past = []

        def _heads_then_move_past(repo: str, base_b: str, integ_b: str):
            res = real_deliver_heads(repo, base_b, integ_b)
            if not moved_past:
                moved_past.append(True)
                self.fixture.git(self.repo, "update-ref", "refs/heads/main", child_sha, self.base_sha)
            return res

        with mock.patch("mesh.core.repos.deliver_heads", side_effect=_heads_then_move_past):
            with transaction(self.conn):
                env = dispatch_internal_request(ctx_fail, idempotency_key="idem-int-1")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], c.VERSION_MISMATCH)
        self.assertFalse(os.path.lexists(repos.deliver_intent_path(self.home, "prj_1")))
        recovery.settle_deliver_intent(self.conn, self.home, "prj_1")
        self.assertEqual(
            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_id,)).fetchone()["status"],
            "approved",
        )
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM idempotency WHERE key = 'idem-int-1';").fetchone()
        )

        # Reset main back to base_sha, succeed via dispatch_internal_request, then roll back DB tx to simulate crash.
        self.fixture.git(self.repo, "update-ref", "refs/heads/main", self.base_sha, child_sha)
        ctx_ok = _ctx(
            self.conn,
            self.home,
            "project.deliver",
            {"project_id": "prj_1"},
            principal=lead_principal,
            actor_kind="agent",
            via="run_token",
            approval=apr_id,
            request_id="req_int_ok",
        )
        self.conn.execute("BEGIN IMMEDIATE;")
        env_ok = dispatch_internal_request(ctx_ok, idempotency_key="idem-int-2")
        self.assertTrue(env_ok["ok"], env_ok)
        self.conn.execute("ROLLBACK;")
        self.assertTrue(os.path.exists(repos.deliver_intent_path(self.home, "prj_1")))
        recovery.settle_deliver_intent(self.conn, self.home, "prj_1")
        self.assertFalse(os.path.exists(repos.deliver_intent_path(self.home, "prj_1")))
        self.assertEqual(
            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_id,)).fetchone()["status"],
            "consumed",
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'project.delivered';").fetchone()[0],
            1,
        )
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM idempotency WHERE key = 'idem-int-2';").fetchone()
        )

    def test_reconcile_deliver_intent_isolates_approval_by_actor_category_and_project(self) -> None:
        new_sha = self._commit_on_integ()
        self.fixture.git(self.repo, "checkout", "-q", "--detach", self.base_sha)
        repos.deliver_to_base(self.repo, "main", self.integ["integration_branch"])
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at) "
                "VALUES ('prj_2', 'P2', 'g', 'active', 'usr_1', 'direct', 1, 'now', 'now');"
            )
        apr_wrong_cat = _create_approved_approval(
            self.conn,
            self.home,
            category="delete",
            op="workspace.delete",
            requested_by="agt_lead",
            project_id="prj_1",
            target_id="tsk_x",
            params={"task_id": "tsk_x"},
        )
        apr_wrong_proj = _create_approved_approval(
            self.conn,
            self.home,
            category="deliver_to_base",
            op="project.deliver",
            requested_by="agt_lead",
            project_id="prj_2",
            target_id="prj_2",
            params={"project_id": "prj_2"},
        )
        apr_for_user = _create_approved_approval(
            self.conn,
            self.home,
            category="deliver_to_base",
            op="project.deliver",
            requested_by="agt_lead",
            project_id="prj_1",
            target_id="prj_1",
            params={"project_id": "prj_1"},
        )
        for idx, (actor_kind, aid) in enumerate([
            ("user", apr_for_user),
            ("agent", apr_wrong_cat),
            ("agent", apr_wrong_proj),
        ]):
            # Clear prior project.delivered event so each reconcile call enters the commit branch.
            with transaction(self.conn):
                self.conn.execute("DROP TRIGGER IF EXISTS events_no_delete;")
                self.conn.execute("DELETE FROM events WHERE type = 'project.delivered';")
            doc = {
                "project_id": "prj_1",
                "old": self.base_sha,
                "new": new_sha,
                "actor_id": "usr_1" if actor_kind == "user" else "agt_lead",
                "actor_kind": actor_kind,
                "via": "direct" if actor_kind == "user" else "token",
                "request_id": f"req_iso_{idx}",
                "approval_id": aid,
                "min_event_seq": 0,
            }
            res = recovery.reconcile_deliver_intent(self.conn, doc)
            self.assertEqual(res, "recorded")
            self.assertEqual(
                self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (aid,)).fetchone()["status"],
                "approved",
                f"approval {aid} ({actor_kind}) should not be consumed",
            )

    def test_deliver_skips_prunable_missing_wt_and_blocks_foreign_parent_or_tampered_wt(self) -> None:
        new_sha = self._commit_on_integ()
        self.fixture.git(self.repo, "checkout", "-q", "--detach", self.base_sha)

        # Case 1: Linked worktree deleted on disk without `git worktree prune` -> prunable, falls back to update-ref.
        wt_main = self.fixture.path("main-wt-gone")
        self.fixture.git(self.repo, "worktree", "add", "-q", wt_main, "main")
        shutil.rmtree(wt_main)
        res = repos.deliver_to_base(self.repo, "main", self.integ["integration_branch"])
        self.assertEqual(res["method"], "update_ref")
        self.assertEqual(self.fixture.head(self.repo, "main"), new_sha)

        # Reset main back to base_sha and prune stale entry.
        self.fixture.git(self.repo, "worktree", "prune")
        self.fixture.git(self.repo, "update-ref", "refs/heads/main", self.base_sha, new_sha)

        # Case 2: Linked worktree path is an empty directory inside a foreign parent repo holding same branch name.
        parent_repo = self.fixture.path("foreign-parent")
        os.makedirs(parent_repo)
        self.fixture.git(parent_repo, "init", "-q", "-b", "main")
        self.fixture.git(parent_repo, "config", "user.email", "p@example.invalid")
        self.fixture.git(parent_repo, "config", "user.name", "p")
        with open(os.path.join(parent_repo, "p.txt"), "w", encoding="utf-8") as f:
            f.write("p\n")
        self.fixture.git(parent_repo, "add", "p.txt")
        self.fixture.git(parent_repo, "commit", "-q", "-m", "p init", identity=True)
        parent_before = self.fixture.head(parent_repo, "main")

        # Sub-repo registered inside another directory, with a linked worktree at parent_repo/sneaky_wt.
        sub_repo = os.path.join(parent_repo, "sub_repo")
        os.makedirs(sub_repo)
        self.fixture.git(sub_repo, "init", "-q", "-b", "main")
        self.fixture.git(sub_repo, "config", "user.email", "s@example.invalid")
        self.fixture.git(sub_repo, "config", "user.name", "s")
        # Share the parent's initial commit object so merge --ff-only in parent_repo would even fast-forward!
        with open(os.path.join(sub_repo, "s.txt"), "w", encoding="utf-8") as f:
            f.write("s\n")
        self.fixture.git(sub_repo, "add", "s.txt")
        self.fixture.git(sub_repo, "commit", "-q", "-m", "sub init", identity=True)
        sub_base = self.fixture.head(sub_repo, "main")
        self.fixture.git(sub_repo, "branch", "mesh/sub/integration", sub_base)
        sub_integ_wt = self.fixture.path("sub-integ")
        self.fixture.git(sub_repo, "worktree", "add", "-q", sub_integ_wt, "mesh/sub/integration")
        with open(os.path.join(sub_integ_wt, "s2.txt"), "w", encoding="utf-8") as f:
            f.write("s2\n")
        self.fixture.git(sub_integ_wt, "add", "s2.txt")
        self.fixture.git(sub_integ_wt, "commit", "-q", "-m", "sub work", identity=True)
        self.fixture.git(sub_repo, "checkout", "-q", "--detach", sub_base)

        sneaky_wt = os.path.join(parent_repo, "sneaky_wt")
        self.fixture.git(sub_repo, "worktree", "add", "-q", sneaky_wt, "main")
        shutil.rmtree(sneaky_wt)
        os.makedirs(sneaky_wt)  # empty dir inside foreign parent_repo!
        with self.assertRaises(MeshError) as cm:
            repos.deliver_to_base(sub_repo, "main", "mesh/sub/integration")
        self.assertEqual(cm.exception.code, c.WORKTREE_DIRTY)
        self.assertEqual(cm.exception.exit_code, c.EXIT_MERGE_FAILED)
        self.assertEqual(self.fixture.head(parent_repo, "main"), parent_before)
        self.assertEqual(self.fixture.head(sub_repo, "main"), sub_base)

        # Case 3: Repointed .git with fsmonitor in linked worktree is refused without running git inside wt.
        self.fixture.git(self.repo, "worktree", "add", "-q", wt_main, "main")
        fake_repo = self.fixture.path("fake-fsmon")
        os.makedirs(fake_repo)
        self.fixture.git(fake_repo, "init", "-q")
        pwn = self.fixture.path("pwned_marker")
        with open(os.path.join(fake_repo, ".git", "config"), "a", encoding="utf-8") as f:
            f.write(f"[core]\n\tfsmonitor = touch {pwn}\n")
        with open(os.path.join(wt_main, ".git"), "w", encoding="utf-8") as f:
            f.write(f"gitdir: {os.path.join(fake_repo, '.git')}\n")
        with self.assertRaises(MeshError) as cm3:
            repos.deliver_to_base(self.repo, "main", self.integ["integration_branch"])
        self.assertEqual(cm3.exception.code, c.WORKTREE_DIRTY)
        self.assertFalse(os.path.exists(pwn))

    def test_deliver_rejects_git_bisect_and_in_progress_states_with_worktree_dirty(self) -> None:
        self._commit_on_integ()
        # repo already has main checked out; start a clean git bisect on it.
        self.fixture.git(self.repo, "bisect", "start")
        try:
            self.assertEqual(
                self.fixture.git(self.repo, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip(),
                "",
            )
            with self.assertRaises(MeshError) as cm:
                repos.deliver_to_base(self.repo, "main", self.integ["integration_branch"])
            self.assertEqual(cm.exception.code, c.WORKTREE_DIRTY)
            self.assertEqual(cm.exception.exit_code, c.EXIT_MERGE_FAILED)
        finally:
            self.fixture.git(self.repo, "bisect", "reset")

        # Also test CHERRY_PICK_HEAD (clean status) and missing cwd OSError mapping.
        cp_head = os.path.join(self.repo, ".git", "CHERRY_PICK_HEAD")
        with open(cp_head, "w", encoding="utf-8") as f:
            f.write(self.base_sha + "\n")
        try:
            with self.assertRaises(MeshError) as cm_cp:
                repos.deliver_to_base(self.repo, "main", self.integ["integration_branch"])
            self.assertEqual(cm_cp.exception.code, c.WORKTREE_DIRTY)
            self.assertEqual(cm_cp.exception.exit_code, c.EXIT_MERGE_FAILED)
        finally:
            os.unlink(cp_head)

        with self.assertRaises(MeshError) as cm_os:
            repos._git("status", ["status"], os.path.join(self._tmp.name, "no-such-dir"))
        self.assertEqual(cm_os.exception.code, c.INTERNAL_ERROR)
        self.assertEqual(cm_os.exception.exit_code, c.EXIT_INTERNAL)


class TestP2_1IgnoredPathsOverwrittenFixes(unittest.TestCase, _GitEnvMixin):
    """P2-1: clean directory->file replacement must deliver; symlink parents must not escape."""

    def setUp(self) -> None:
        self._init_git_env()
        self.repo = self.fixture.make_k0("R3CP21")
        self.repo = os.path.realpath(self.repo)
        self.ws_root = self.fixture.path("ws")
        os.makedirs(self.ws_root, mode=0o700)
        with open(os.path.join(self.repo, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("*.ign\n")
        os.makedirs(os.path.join(self.repo, "pkg"))
        with open(os.path.join(self.repo, "pkg", "mod.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        self.outside = self.fixture.path("outside_dir")
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "sub"), "w", encoding="utf-8") as f:
            f.write("outside\n")
        os.symlink(self.outside, os.path.join(self.repo, "sym"))
        self.fixture.git(self.repo, "add", ".gitignore", "pkg/mod.py", "sym")
        self.fixture.git(self.repo, "commit", "-q", "-m", "add pkg and sym", identity=True)
        self.base = self.fixture.head(self.repo, "main")
        integ = repos.setup_integration(self.repo, "prj_p21", self.base, self.ws_root)
        self.integ_branch = integ["integration_branch"]
        self.integ_wt = integ["integration_path"]

    def test_clean_dir_replaced_with_file_and_tracked_symlink_replaced_with_dir_deliver(self) -> None:
        self.fixture.git(self.integ_wt, "rm", "-q", "pkg/mod.py", "sym")
        with open(os.path.join(self.integ_wt, "pkg"), "w", encoding="utf-8") as f:
            f.write("pkg is now a file\n")
        os.makedirs(os.path.join(self.integ_wt, "sym"))
        with open(os.path.join(self.integ_wt, "sym", "sub"), "w", encoding="utf-8") as f:
            f.write("inside\n")
        self.fixture.git(self.integ_wt, "add", "pkg", "sym/sub")
        self.fixture.git(self.integ_wt, "commit", "-q", "-m", "replace dir and sym", identity=True)

        # Clean worktree must deliver (no false-positive WORKTREE_DIRTY).
        res = repos.deliver_to_base(self.repo, "main", self.integ_branch)
        self.assertEqual(res["method"], "merge_ff_only")
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "pkg")))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "sym", "sub")))

    def test_ignored_file_inside_replaced_dir_still_refused(self) -> None:
        self.fixture.git(self.integ_wt, "rm", "-q", "pkg/mod.py")
        with open(os.path.join(self.integ_wt, "pkg"), "w", encoding="utf-8") as f:
            f.write("pkg file\n")
        self.fixture.git(self.integ_wt, "add", "pkg")
        self.fixture.git(self.integ_wt, "commit", "-q", "-m", "replace pkg", identity=True)

        with open(os.path.join(self.repo, "pkg", "local.ign"), "w", encoding="utf-8") as f:
            f.write("keep me\n")
        with self.assertRaises(MeshError) as cm:
            repos.deliver_to_base(self.repo, "main", self.integ_branch)
        self.assertEqual(cm.exception.code, c.WORKTREE_DIRTY)
        self.assertEqual(cm.exception.details["reason"], "ignored_files_would_be_overwritten")
        self.assertEqual(cm.exception.details["paths"], ["pkg"])


class TestP2_4ApprovalAndCommittedSideEffect(unittest.TestCase, _GitEnvMixin):
    """P2-4: set_lead validates target/unchanged before approval; ws delete TX2 skips consume on deleted=False."""

    def setUp(self) -> None:
        self._init_git_env()
        self._tmp = tempfile.TemporaryDirectory(prefix="mh-r3c-p24-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.fixture.allow_root(self._tmp.name)
        self.home, self.conn, self.repo, self.base_sha, self.integ = _setup_home_and_repo(
            self._tmp.name, self.fixture
        )
        self.addCleanup(self.conn.close)

    def test_set_lead_invalid_target_and_unchanged_never_create_or_consume_approval(self) -> None:
        # 1. Invalid target agent without --approval must return NOT_FOUND, not create pending approval.
        with self.assertRaises(MeshError) as cm_nf:
            with transaction(self.conn):
                project_handlers.handle_project_set_lead(
                    _ctx(
                        self.conn,
                        self.home,
                        "project.set_lead",
                        {"project_id": "prj_1", "agent": "no_such_agent", "reason": "test"},
                        actor_kind="agent",
                        via="token",
                    )
                )
        self.assertEqual(cm_nf.exception.code, c.NOT_FOUND)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM approvals;").fetchone()[0], 0)

        # 2. Same active lead (unchanged=True) with approved approval must NOT consume it.
        apr_id = _create_approved_approval(
            self.conn,
            self.home,
            category="lead_change",
            op="project.set_lead",
            requested_by="agt_lead",
            project_id="prj_1",
            target_id="",
            params={"project_id": "prj_1", "agent": "lead1", "reason": "noop"},
        )
        # Note: compute_op_hash uses target_id=None for project.set_lead. Re-create cleanly with target_id=None.
        with transaction(self.conn):
            self.conn.execute("DELETE FROM approvals WHERE id = ?;", (apr_id,))
        with transaction(self.conn):
            apr = create_or_get_pending_approval(
                conn=self.conn,
                category="lead_change",
                op="project.set_lead",
                requested_by="agt_lead",
                params={"project_id": "prj_1", "agent": "lead1", "reason": "noop"},
                summary="noop set-lead",
                project_id="prj_1",
                testing_mode=True,
            )
            handle_approval_approve(
                _ctx(
                    self.conn,
                    self.home,
                    "approval.approve",
                    {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                )
            )
        res = project_handlers.handle_project_set_lead(
            _ctx(
                self.conn,
                self.home,
                "project.set_lead",
                {"project_id": "prj_1", "agent": "lead1", "reason": "noop"},
                actor_kind="agent",
                via="token",
                approval=apr["approval_id"],
            )
        )
        self.assertTrue(res.data["unchanged"])
        self.assertEqual(
            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()["status"],
            "approved",
        )

    def test_workspace_delete_tx2_revoked_with_deleted_false_keeps_approval_and_no_event(self) -> None:
        ws = os.path.join(self.home, "workspaces", "prj_1", "tasks", "tsk_del")
        os.makedirs(ws, mode=0o700, exist_ok=True)
        now = now_iso_utc()
        tok, lookup, shash = generate_agent_token()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
                "VALUES ('cred_l', 'agt_lead', 'agent_token', ?, ?, ?);",
                (lookup, shash, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES ('tsk_del', 'prj_1', 'Del', 'S', 'code', 'accepted', 'dev', 600, 3, ?, "
                "'mesh/prj_1/tsk_del', 'usr_1', ?, ?);",
                (ws, now, now),
            )
        apr_id = _create_approved_approval(
            self.conn,
            self.home,
            category="delete",
            op="workspace.delete",
            requested_by="agt_lead",
            project_id="prj_1",
            target_id="tsk_del",
            params={"task_id": "tsk_del"},
        )

        # Simulate directory vanishing before _do_delete_sync runs (deleted == False) AND credential revoked before TX2.
        def _vanish_and_revoke(*_args, **_kwargs) -> bool:
            shutil.rmtree(ws)
            tconn = sqlite3.connect(os.path.join(self.home, "mesh.db"), isolation_level=None, timeout=10)
            try:
                with transaction(tconn):
                    tconn.execute("UPDATE credentials SET revoked_at = ? WHERE id = 'cred_l';", (now_iso_utc(),))
            finally:
                tconn.close()
            return False

        ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "agt_lead", "kind": "agent", "name": "lead1", "credential_id": "cred_l"},
            actor_kind="agent",
            via="token",
            request_id="req_del_false",
            home=self.home,
            cmd="workspace.delete",
            args={"task_id": "tsk_del"},
            approval=apr_id,
            credential_id="cred_l",
            testing_mode=True,
        )

        class _DummySrv:
            def track_merge_job(self, job):
                pass

        with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_vanish_and_revoke):
            with self.assertRaises(MeshError) as cm:
                asyncio.run(run_handlers.run_workspace_delete(_DummySrv(), ctx))
        self.assertEqual(cm.exception.code, c.TOKEN_REVOKED)
        self.assertEqual(
            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_id,)).fetchone()["status"],
            "approved",
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted';").fetchone()[0],
            0,
        )

    def test_workspace_delete_tx2_revoked_with_deleted_true_sets_committed_side_effect(self) -> None:
        ws = os.path.join(self.home, "workspaces", "prj_1", "tasks", "tsk_del2")
        os.makedirs(ws, mode=0o700, exist_ok=True)
        now = now_iso_utc()
        _tok, lookup, shash = generate_agent_token()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
                "VALUES ('cred_l2', 'agt_lead', 'agent_token', ?, ?, ?);",
                (lookup, shash, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES ('tsk_del2', 'prj_1', 'Del2', 'S', 'code', 'accepted', 'dev', 600, 3, ?, "
                "'mesh/prj_1/tsk_del2', 'usr_1', ?, ?);",
                (ws, now, now),
            )
        orig_del = run_handlers._delete_workspace_dir_sync

        def _del_then_revoke(*args, **kwargs):
            res = orig_del(*args, **kwargs)
            tconn = sqlite3.connect(os.path.join(self.home, "mesh.db"), isolation_level=None, timeout=10)
            try:
                with transaction(tconn):
                    tconn.execute("UPDATE credentials SET revoked_at = ? WHERE id = 'cred_l2';", (now_iso_utc(),))
            finally:
                tconn.close()
            return res

        ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user", "credential_id": "cred_l2"},
            actor_kind="user",
            via="direct",
            request_id="req_del_true",
            home=self.home,
            cmd="workspace.delete",
            args={"task_id": "tsk_del2"},
            credential_id="cred_l2",
            testing_mode=True,
        )

        class _DummySrv:
            def track_merge_job(self, job):
                pass

        with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_del_then_revoke):
            with self.assertRaises(MeshError):
                asyncio.run(run_handlers.run_workspace_delete(_DummySrv(), ctx))
        self.assertTrue(getattr(ctx, "_committed_side_effect", False))


class TestP2_6LogOffloadAndDeliverBudget(unittest.TestCase, _GitEnvMixin):
    """P2-6: run.log/task.log offload outside BEGIN IMMEDIATE + sync fallback; deliver git_budget."""

    def setUp(self) -> None:
        self._init_git_env()
        self._tmp = tempfile.TemporaryDirectory(prefix="mh-r3c-p26-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.fixture.allow_root(self._tmp.name)
        self.home, self.conn, self.repo, self.base_sha, self.integ = _setup_home_and_repo(
            self._tmp.name, self.fixture
        )
        self.addCleanup(self.conn.close)
        self.run_dir = os.path.join(self.home, "runs", "run_log1")
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write("hello line 1\ntoken=secret_abc123456\n")
        now = now_iso_utc()
        self.tok_lead, lookup, shash = generate_agent_token()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
                "VALUES ('cred_lead', 'agt_lead', 'agent_token', ?, ?, ?);",
                (lookup, shash, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "created_by, created_at, updated_at) "
                "VALUES ('tsk_log1', 'prj_1', 'Log', 'S', 'general', 'submitted', 'edit', 60, 1, 'usr_1', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, model, "
                "effort, profile, status, run_dir, argv_redacted_json, boot_id, started_at) "
                "VALUES ('run_log1', 'task', 'tsk_log1', 'prj_1', 'agt_lead', 1, 1, 'fake', 'm', 'low', "
                "'edit', 'exited', ?, '[]', 'boot_1', ?);",
                (self.run_dir, now),
            )

    def test_sync_direct_handlers_still_work_and_server_offloads_off_loop_and_tx(self) -> None:
        # 1. Direct synchronous unit-test calls without _prepared_log still work.
        r1 = run_handlers.handle_run_log(_ctx(self.conn, self.home, "run.log", {"run_id": "run_log1"}))
        self.assertTrue(r1.data["streams"]["stdout"]["available"])
        self.assertIn("hello line 1", r1.data["streams"]["stdout"]["lines"])
        r2 = run_handlers.handle_task_log(_ctx(self.conn, self.home, "task.log", {"task_id": "tsk_log1"}))
        self.assertEqual(r2.data["run_id"], "run_log1")

        # 2. Through DaemonServer.handle_client, _run_logs runs in worker thread outside BEGIN IMMEDIATE,
        # and mid-flight credential revocation is rejected by recheck_tx_credential_and_authz.
        srv = DaemonServer(home=self.home, testing_mode=True)
        srv.db_conn = self.conn

        async def _send(payload: dict) -> dict:
            sock_a, sock_b = socket.socketpair()
            reader_s, writer_s = await asyncio.open_connection(sock=sock_b)
            reader_c, writer_c = await asyncio.open_connection(sock=sock_a)
            with mock.patch("mesh.daemon.server.get_peer_credentials",
                            return_value=(os.getpid(), os.getuid(), os.getgid())), \
                    mock.patch("mesh.daemon.server.classify_peer_run_session", return_value=None), \
                    mock.patch.object(srv, "refresh_run_caches"):
                writer_c.write((json.dumps(payload) + "\n").encode("utf-8"))
                await writer_c.drain()
                await srv.handle_client(reader_s, writer_s)
                line = await reader_c.readline()
            writer_c.close()
            writer_s.close()
            return json.loads(line.decode("utf-8"))

        async def _run_cases():
            loop_tid = threading.get_ident()
            observed = []
            orig_run_logs = run_handlers._run_logs

            def _spy_run_logs(run, streams, tail):
                observed.append((threading.get_ident(), self.conn.in_transaction))
                return orig_run_logs(run, streams, tail)

            with mock.patch.object(run_handlers, "_run_logs", side_effect=_spy_run_logs):
                res_run = await _send({
                    "v": 1, "cmd": "run.log",
                    "auth": {"type": "token", "token": self.tok_lead},
                    "args": {"run_id": "run_log1", "stream": "stdout"},
                })
                res_task = await _send({
                    "v": 1, "cmd": "task.log",
                    "auth": {"type": "token", "token": self.tok_lead},
                    "args": {"task_id": "tsk_log1", "stream": "stdout"},
                })
            self.assertTrue(res_run["ok"], res_run)
            self.assertTrue(res_task["ok"], res_task)
            self.assertEqual(len(observed), 2)
            for tid, in_tx in observed:
                self.assertNotEqual(tid, loop_tid, "_run_logs ran on the event loop thread")
                self.assertFalse(in_tx, "_run_logs ran inside BEGIN IMMEDIATE")

            # Mid-flight token revocation during offloaded log read must fail closed in TX.
            def _revoke_during_read(run, streams, tail):
                tconn = sqlite3.connect(os.path.join(self.home, "mesh.db"), isolation_level=None, timeout=10)
                try:
                    with transaction(tconn):
                        tconn.execute("UPDATE credentials SET revoked_at = ? WHERE id = 'cred_lead';", (now_iso_utc(),))
                finally:
                    tconn.close()
                return orig_run_logs(run, streams, tail)

            with mock.patch.object(run_handlers, "_run_logs", side_effect=_revoke_during_read):
                res_revoked = await _send({
                    "v": 1, "cmd": "run.log",
                    "auth": {"type": "token", "token": self.tok_lead},
                    "args": {"run_id": "run_log1", "stream": "stdout"},
                })
            self.assertFalse(res_revoked["ok"])
            self.assertEqual(res_revoked["error"]["code"], c.TOKEN_REVOKED)

        asyncio.run(_run_cases())

    def test_project_deliver_and_settle_intent_enforce_git_budget(self) -> None:
        wt = self.integ["integration_path"]
        with open(os.path.join(wt, "b.txt"), "w", encoding="utf-8") as f:
            f.write("b\n")
        self.fixture.git(wt, "add", "b.txt")
        self.fixture.git(wt, "commit", "-q", "-m", "b", identity=True)
        new_sha = self.fixture.head(wt)
        self.fixture.git(self.repo, "checkout", "-q", "--detach", self.base_sha)

        real_run_git = repos.run_git

        def _slow_git(args, cwd=None, timeout_s=30.0, **kw):
            time.sleep(0.15)
            return real_run_git(args, cwd=cwd, timeout_s=timeout_s, **kw)

        with mock.patch.object(repos, "SETUP_INTEGRATION_BUDGET_S", 0.2), \
                mock.patch.object(repos, "run_git", side_effect=_slow_git):
            with self.assertRaises(MeshError) as cm:
                project_handlers.handle_project_deliver(_ctx(self.conn, self.home, "project.deliver", {"project_id": "prj_1"}))
        self.assertEqual(cm.exception.details.get("reason"), "git_timeout")

        # settle_deliver_intent must also enforce SETUP_INTEGRATION_BUDGET_S across its git calls.
        repos.write_deliver_intent(self.home, {
            "project_id": "prj_1",
            "old": self.base_sha,
            "new": new_sha,
            "actor_id": "usr_1",
            "actor_kind": "user",
            "via": "direct",
            "request_id": "req_b",
            "min_event_seq": 0,
        })
        seen_timeouts = []

        def _slow_settle_git(args, cwd=None, timeout_s=30.0, **kw):
            seen_timeouts.append(timeout_s)
            time.sleep(0.15)
            return subprocess.CompletedProcess(args, 0, stdout=new_sha + "\n", stderr="")

        with mock.patch.object(repos, "SETUP_INTEGRATION_BUDGET_S", 0.2), \
                mock.patch("mesh.core.gitops.run_git", side_effect=_slow_settle_git):
            try:
                recovery.settle_deliver_intent(self.conn, self.home, "prj_1")
            except recovery.DeliverIntentUnresolved:
                pass
        self.assertTrue(seen_timeouts and all(t <= 0.21 for t in seen_timeouts), seen_timeouts)


class TestP2_11SetupIntegrationSymlinkAndProjectAuthzFirst(unittest.TestCase, _GitEnvMixin):
    """P2-11: setup_integration rejects symlinked dir before chmod; project.* checks authz before state."""

    def setUp(self) -> None:
        self._init_git_env()
        self._tmp = tempfile.TemporaryDirectory(prefix="mh-r3c-p211-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.fixture.allow_root(self._tmp.name)
        self.home, self.conn, self.repo, self.base_sha, self.integ = _setup_home_and_repo(
            self._tmp.name, self.fixture
        )
        self.addCleanup(self.conn.close)

    def test_setup_integration_rejects_symlink_before_chmod(self) -> None:
        ws_root = os.path.realpath(os.path.join(self.home, "workspaces"))
        outside = os.path.join(self._tmp.name, "outside_victim")
        os.makedirs(outside, mode=0o755)
        os.chmod(outside, 0o755)
        os.symlink(outside, os.path.join(ws_root, "prj_sym"))
        with self.assertRaises(MeshError) as cm:
            repos.setup_integration(self.repo, "prj_sym", self.base_sha, ws_root)
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm.exception.details.get("reason"), "symlink_or_outside_root")
        self.assertEqual(stat.S_IMODE(os.stat(outside).st_mode), 0o755)
        self.assertFalse(os.path.exists(os.path.join(outside, "integration")))

    def test_project_handlers_authorize_before_nonexistent_project_not_found(self) -> None:
        unrelated = {"id": "agt_other", "kind": "agent", "name": "other", "capabilities": []}
        superseded_lead = {
            "id": "agt_lead",
            "kind": "agent",
            "name": "lead1",
            "capabilities": [],
            "scope": {"role": "lead", "project_id": "prj_1", "lead_assignment_id": "asg_old", "run_id": "run_old"},
        }
        sync_calls = [
            ("project.leads", project_handlers.handle_project_leads, {"project_id": "prj_missing"}),
            ("project.show", project_handlers.handle_project_show, {"project_id": "prj_missing"}),
            ("project.set_lead", project_handlers.handle_project_set_lead,
             {"project_id": "prj_missing", "agent": "lead2", "reason": "x"}),
            ("project.pause", project_handlers.handle_project_pause, {"project_id": "prj_missing"}),
            ("project.resume", project_handlers.handle_project_resume, {"project_id": "prj_missing"}),
            ("project.cancel", project_handlers.handle_project_cancel, {"project_id": "prj_missing"}),
            ("project.deliver", project_handlers.handle_project_deliver, {"project_id": "prj_missing"}),
            ("project.update", project_handlers.handle_project_update,
             {"project_id": "prj_missing", "goal": "new goal"}),
            ("project.complete", project_handlers.handle_project_complete,
             {"project_id": "prj_missing", "report": {"criteria": []}}),
            ("project.note", project_handlers.handle_project_note,
             {"project_id": "prj_missing", "text": "hello"}),
        ]
        for cmd, fn, args in sync_calls:
            with self.subTest(cmd=cmd, caller="unrelated"):
                with self.assertRaises(MeshError) as cm:
                    fn(_ctx(self.conn, self.home, cmd, args, principal=unrelated, actor_kind="agent", via="token"))
                self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN, (cmd, cm.exception))
            with self.subTest(cmd=cmd, caller="superseded_lead"):
                with self.assertRaises(MeshError) as cm2:
                    fn(_ctx(self.conn, self.home, cmd, args, principal=superseded_lead,
                            actor_kind="agent", via="run_token"))
                self.assertEqual(cm2.exception.code, c.LEAD_SUPERSEDED, (cmd, cm2.exception))

        # Async project.wait as well.
        class _DummyWaitSrv:
            active_waits = 0

        for caller_name, principal, via, exp_code in (
            ("unrelated", unrelated, "token", c.SCOPE_MISMATCH),
            ("superseded_lead", superseded_lead, "run_token", c.LEAD_SUPERSEDED),
        ):
            with self.subTest(cmd="project.wait", caller=caller_name):
                with self.assertRaises(MeshError) as cm_w:
                    asyncio.run(
                        project_handlers.handle_project_wait(
                            _DummyWaitSrv(),
                            _ctx(
                                self.conn,
                                self.home,
                                "project.wait",
                                {"project_id": "prj_missing", "timeout": "1s"},
                                principal=principal,
                                actor_kind="agent",
                                via=via,
                            ),
                        )
                    )
                self.assertEqual(cm_w.exception.code, exp_code)


if __name__ == "__main__":
    unittest.main()
