"""Regression tests for full2-r2.md Group A findings (P1-1, P2-1, P2-2)."""

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mesh.constants as c
from mesh.core import db, invariants
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.auth import generate_agent_token
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.core.workspaces import compute_task_workspace_path
from mesh.daemon.handlers.approval import handle_approval_approve
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.project import handle_project_deliver
from mesh.daemon.handlers.task import (
    handle_task_close,
    handle_task_reject,
    handle_task_retry,
)
from mesh.daemon.server import DaemonServer
from mesh.errors import MeshError
from tests.gitfixture import GitFixture
from tests.helpers import fake_peer_creds


def _make_ctx(
    conn: sqlite3.Connection,
    home: str,
    cmd: str,
    args: dict,
    *,
    principal: dict | None = None,
    actor_kind: str = "user",
    via: str = "direct",
    approval: str | None = None,
    k_user: bytes | None = b"k" * 32,
) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal=principal or {"id": "usr_1", "kind": "user", "name": "user"},
        actor_kind=actor_kind,
        via=via,
        request_id="req_full2_r2_a",
        home=home,
        cmd=cmd,
        args=args,
        approval=approval,
        k_user=k_user,
        testing_mode=True,
    )


class TestP1_1ReverseI9SupersededAndExcused(unittest.TestCase):
    """P1-1: reverse I9 must not blindly trust --superseded-by or deadlock on merged intermediates."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-r2-p1-1-")
        self.fixture = GitFixture()
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for k in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[k]
        os.environ.update(self.fixture.git_env_vars())

        self.repo = self.fixture.make_k0("R2P11")
        self.base_sha = self.fixture.head(self.repo, "main")
        self.integ = "mesh/prj_1/integration"
        self.fixture.git(self.repo, "branch", self.integ, "main")

        self.conn = init_db(os.path.join(self.tmp.name, "mesh.db"))
        ts = "2026-01-01T00:00:00Z"
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (ts,)
        )
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);", (ts,)
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
            "VALUES ('agt_lead', 'none', 'inbox', 1, 1, ?, ?);",
            (ts, ts),
        )
        self.conn.execute(
            "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
            "created_by, created_via, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            ("prj_1", "p", "g", "active", self.repo, "main", self.integ, "usr_1", "direct", ts, ts),
        )
        self.conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
            (ts,),
        )
        record_event(
            self.conn,
            type_="project.created",
            actor_id="usr_1",
            actor_kind="user",
            via="direct",
            project_id="prj_1",
            to_state="active",
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.fixture.cleanup()
        self.tmp.cleanup()

    def _git_out(self, *args: str) -> str:
        return self.fixture.git(self.repo, *args).stdout.strip()

    def _commit_chain(self, branch: str, start_sha: str, files: list[tuple[str, str]]) -> list[str]:
        self._git_out("branch", branch, start_sha)
        wt = self.fixture.path(f"wt-{branch.replace('/', '_')}")
        self.fixture.git(self.repo, "worktree", "add", wt, branch)
        shas: list[str] = []
        for idx, (fname, content) in enumerate(files):
            Path(wt, fname).write_text(content, encoding="utf-8")
            self.fixture.git(wt, "add", fname)
            self.fixture.git(wt, "commit", "-q", "-m", f"{branch} step {idx}", identity=True)
            shas.append(self.fixture.head(wt))
        self.fixture.git(self.repo, "worktree", "remove", wt)
        return shas

    def _merge_branch(self, branch: str, msg: str = "merge branch") -> str:
        wt = self.fixture.path(f"wt-integ-{branch.replace('/', '_')}")
        self.fixture.git(self.repo, "worktree", "add", wt, self.integ)
        self.fixture.git(wt, "merge", "--no-ff", "-m", msg, branch, identity=True)
        mc = self.fixture.head(wt)
        self.fixture.git(self.repo, "worktree", "remove", wt)
        return mc

    def _insert_task(
        self,
        task_id: str,
        status: str,
        branch: str,
        base_commit: str,
        *,
        merge_json: dict | None = None,
    ) -> None:
        ts = "2026-01-01T00:00:00Z"
        mj = json.dumps(merge_json) if merge_json is not None else None
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, fence_seq, branch, base_commit, merge_json, created_by, "
                "created_at, updated_at) VALUES (?, 'prj_1', ?, 'S', 'code', ?, 'agt_lead', 'edit', "
                "3600, 3, 1, ?, ?, ?, 'usr_1', ?, ?);",
                (task_id, task_id, status, branch, base_commit, mj, ts, ts),
            )
            record_event(
                self.conn,
                type_="task.created",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_1",
                task_id=task_id,
                to_state=status,
            )

    def test_bogus_superseded_by_does_not_bypass_reverse_i9_or_allow_close_of_integrated_task(self) -> None:
        # Unrelated merged task tsk_other
        self._commit_chain("mesh/prj_1/tsk_other", self.base_sha, [("other.txt", "o\n")])
        mc_other = self._merge_branch("mesh/prj_1/tsk_other", "Merge tsk_other")
        self._insert_task(
            "tsk_other", "merged", "mesh/prj_1/tsk_other", self.base_sha,
            merge_json={"merge_commit": mc_other, "noop": False},
        )

        # Unmerged task tsk_sneaky was integrated by hand (unrecorded merge), NOT via tsk_other
        self._commit_chain("mesh/prj_1/tsk_sneaky", self.base_sha, [("sneaky.txt", "s\n")])
        unrec_mc = self._merge_branch("mesh/prj_1/tsk_sneaky", "Unrecorded merge")
        self._insert_task("tsk_sneaky", "accepted", "mesh/prj_1/tsk_sneaky", self.base_sha)

        # Closing tsk_sneaky as superseded by unrelated tsk_other must be refused.
        with self.assertRaises(MeshError) as cm:
            handle_task_close(
                _make_ctx(
                    self.conn,
                    self.tmp.name,
                    "task.close",
                    {"task_id": "tsk_sneaky", "superseded_by": "tsk_other"},
                )
            )
        self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_TRANSITION, c.EXIT_CONFLICT))

        # Even if a task.closed event pointing to tsk_other already exists in DB,
        # reverse I9 must still catch tsk_sneaky and block project.deliver.
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET status = 'cancelled' WHERE id = 'tsk_sneaky';")
            record_event(
                self.conn,
                type_="task.closed",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_1",
                task_id="tsk_sneaky",
                from_state="accepted",
                to_state="cancelled",
                payload={"superseded_by": "tsk_other", "from_state": "accepted"},
            )
        viols = invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1")
        self.assertEqual([v.details["task_id"] for v in viols], ["tsk_sneaky"])
        self.assertEqual(viols[0].details["merge_commit"], unrec_mc)
        with self.assertRaises(MeshError) as del_err:
            handle_project_deliver(_make_ctx(self.conn, self.tmp.name, "project.deliver", {"project_id": "prj_1"}))
        self.assertEqual(del_err.exception.code, c.INVALID_TRANSITION)

    def test_fast_forwarded_work_under_later_merged_task_is_not_excused(self) -> None:
        # tsk_ff has C1 -> C2; C1 was fast-forwarded onto integration, then unrelated tsk_m merged on top.
        c_shas = self._commit_chain(
            "mesh/prj_1/tsk_ff", self.base_sha, [("c1.txt", "1\n"), ("c2.txt", "2\n")]
        )
        c1 = c_shas[0]
        self._git_out("update-ref", f"refs/heads/{self.integ}", c1)

        self._commit_chain("mesh/prj_1/tsk_m", self.base_sha, [("m.txt", "m\n")])
        mc_m = self._merge_branch("mesh/prj_1/tsk_m", "Merge tsk_m")
        self._insert_task(
            "tsk_m", "merged", "mesh/prj_1/tsk_m", self.base_sha,
            merge_json={"merge_commit": mc_m, "noop": False},
        )

        # Even if closed --superseded-by tsk_m (whose first parent is c1, NOT second parent!),
        # tsk_ff's c1 must still be flagged as unmerged work on integration.
        self._insert_task("tsk_ff", "cancelled", "mesh/prj_1/tsk_ff", self.base_sha)
        with transaction(self.conn):
            record_event(
                self.conn,
                type_="task.closed",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_1",
                task_id="tsk_ff",
                from_state="cancelled",
                to_state="cancelled",
                payload={"superseded_by": "tsk_m", "from_state": "cancelled"},
            )
        viols = invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1")
        self.assertEqual([v.details["task_id"] for v in viols], ["tsk_ff"])
        self.assertEqual(viols[0].details["merge_commit"], c1)

    def test_intermediate_commit_of_merged_task_does_not_deadlock_deliver(self) -> None:
        # tsk_m has M1 -> M2, merged into integration at mc_m.
        m_shas = self._commit_chain(
            "mesh/prj_1/tsk_m", self.base_sha, [("m1.txt", "m1\n"), ("m2.txt", "m2\n")]
        )
        m1 = m_shas[0]
        mc_m = self._merge_branch("mesh/prj_1/tsk_m", "Merge tsk_m")
        self._insert_task(
            "tsk_m", "merged", "mesh/prj_1/tsk_m", self.base_sha,
            merge_json={"merge_commit": mc_m, "noop": False},
        )

        # tsk_c branched from intermediate commit m1 and added unmerged c1 (never merged).
        self._commit_chain("mesh/prj_1/tsk_c", m1, [("c_unmerged.txt", "u\n")])
        self._insert_task("tsk_c", "cancelled", "mesh/prj_1/tsk_c", self.base_sha)

        self._git_out("checkout", "-q", "--detach", self.base_sha)
        self.assertEqual(invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1"), [])
        res = handle_project_deliver(_make_ctx(self.conn, self.tmp.name, "project.deliver", {"project_id": "prj_1"}))
        self.assertEqual(res.data["new"], mc_m)

    def test_retroactive_task_close_on_cancelled_and_failed_tasks(self) -> None:
        # tsk_orig has A1; tsk_sup built A1 -> S2 and merged; tsk_other is unrelated merged.
        a_shas = self._commit_chain("mesh/prj_1/tsk_orig", self.base_sha, [("a1.txt", "a1\n")])
        a1 = a_shas[0]
        self._commit_chain("mesh/prj_1/tsk_sup", a1, [("s2.txt", "s2\n")])
        mc_sup = self._merge_branch("mesh/prj_1/tsk_sup", "Merge tsk_sup")
        self._insert_task(
            "tsk_sup", "merged", "mesh/prj_1/tsk_sup", self.base_sha,
            merge_json={"merge_commit": mc_sup, "noop": False},
        )
        self._commit_chain("mesh/prj_1/tsk_other", self.base_sha, [("o.txt", "o\n")])
        mc_other = self._merge_branch("mesh/prj_1/tsk_other", "Merge tsk_other")
        self._insert_task(
            "tsk_other", "merged", "mesh/prj_1/tsk_other", self.base_sha,
            merge_json={"merge_commit": mc_other, "noop": False},
        )

        # tsk_orig is already cancelled (via task cancel) and tsk_fail is failed on the same work
        self._insert_task("tsk_orig", "cancelled", "mesh/prj_1/tsk_orig", self.base_sha)
        self._git_out("branch", "mesh/prj_1/tsk_fail", a1)
        self._insert_task("tsk_fail", "failed", "mesh/prj_1/tsk_fail", self.base_sha)

        # Unrelated tsk_other must be rejected for cancelled/failed tasks
        for tid in ("tsk_orig", "tsk_fail"):
            with self.assertRaises(MeshError) as bad_cm:
                handle_task_close(
                    _make_ctx(self.conn, self.tmp.name, "task.close", {"task_id": tid, "superseded_by": "tsk_other"})
                )
            self.assertEqual(bad_cm.exception.code, c.INVALID_TRANSITION)

        # Valid superseding task tsk_sup succeeds for both cancelled and failed tasks
        r1 = handle_task_close(
            _make_ctx(self.conn, self.tmp.name, "task.close", {"task_id": "tsk_orig", "superseded_by": "tsk_sup"})
        )
        self.assertEqual((r1.data["status"], r1.data["superseded_by"]), ("cancelled", "tsk_sup"))
        r2 = handle_task_close(
            _make_ctx(self.conn, self.tmp.name, "task.close", {"task_id": "tsk_fail", "superseded_by": "tsk_sup"})
        )
        self.assertEqual((r2.data["status"], r2.data["superseded_by"]), ("cancelled", "tsk_sup"))

        # Closing an already-closed task a second time must fail with conflict
        with self.assertRaises(MeshError) as dup_cm:
            handle_task_close(
                _make_ctx(self.conn, self.tmp.name, "task.close", {"task_id": "tsk_orig", "superseded_by": "tsk_sup"})
            )
        self.assertEqual((dup_cm.exception.code, dup_cm.exception.exit_code), (c.INVALID_TRANSITION, c.EXIT_CONFLICT))

        self._git_out("checkout", "-q", "--detach", self.base_sha)
        self.assertEqual(invariants.check_invariants(self.conn), [])
        res = handle_project_deliver(_make_ctx(self.conn, self.tmp.name, "project.deliver", {"project_id": "prj_1"}))
        self.assertEqual(res.data["new"], mc_other)

    def test_unintegrated_reimplemented_accepted_task_still_closes_and_zero_noop_does_not_excuse(self) -> None:
        # Unintegrated accepted task tsk_reimp (work re-implemented on tsk_m, not an ancestor of tsk_m)
        # must still be closeable (preserving test_friction4 behavior).
        self._commit_chain("mesh/prj_1/tsk_m", self.base_sha, [("m.txt", "m\n")])
        mc_m = self._merge_branch("mesh/prj_1/tsk_m", "Merge tsk_m")
        self._insert_task(
            "tsk_m", "merged", "mesh/prj_1/tsk_m", self.base_sha,
            merge_json={"merge_commit": mc_m, "noop": False},
        )
        self._commit_chain("mesh/prj_1/tsk_reimp", self.base_sha, [("reimp.txt", "r\n")])
        self._insert_task("tsk_reimp", "accepted", "mesh/prj_1/tsk_reimp", self.base_sha)
        res = handle_task_close(
            _make_ctx(self.conn, self.tmp.name, "task.close", {"task_id": "tsk_reimp", "superseded_by": "tsk_m"})
        )
        self.assertEqual((res.data["status"], res.data["superseded_by"]), ("cancelled", "tsk_m"))

        # Zero-commit noop merged task cannot vouch for an unrecorded integrated task.
        self._git_out("branch", "mesh/prj_1/tsk_zero", self.base_sha)
        self._insert_task(
            "tsk_zero", "merged", "mesh/prj_1/tsk_zero", self.base_sha,
            merge_json={"merge_commit": self.base_sha, "noop": True, "tip": self.base_sha},
        )
        self._commit_chain("mesh/prj_1/tsk_bad", self.base_sha, [("bad.txt", "b\n")])
        self._merge_branch("mesh/prj_1/tsk_bad", "Unrecorded merge of tsk_bad")
        self._insert_task("tsk_bad", "cancelled", "mesh/prj_1/tsk_bad", self.base_sha)
        with transaction(self.conn):
            record_event(
                self.conn,
                type_="task.closed",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_1",
                task_id="tsk_bad",
                from_state="cancelled",
                to_state="cancelled",
                payload={"superseded_by": "tsk_zero", "from_state": "cancelled"},
            )
        viols = invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1")
        self.assertEqual([v.details["task_id"] for v in viols], ["tsk_bad"])


class TestP2_1RetryAndRejectCheckResolvedModel(unittest.TestCase):
    """P2-1: task.retry and task.reject must reject disallowed task['model'] before creating/consuming approval."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-r2-p2-1-")
        self.conn = init_db(os.path.join(self.tmp.name, "mesh.db"))
        ts = "2026-01-01T00:00:00Z"
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (ts,)
        )
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);", (ts,)
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, default_profile, "
            "max_unapproved_profile, default_model, allowed_models_json, created_at, updated_at) "
            "VALUES ('agt_lead', 'none', 'inbox', 1, 1, 'edit', 'edit', 'm1', '[\"m1\",\"m2\"]', ?, ?);",
            (ts, ts),
        )
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_w', 'agent', 'w1', ?);", (ts,)
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, default_profile, "
            "max_unapproved_profile, default_model, allowed_models_json, created_at, updated_at) "
            "VALUES ('agt_w', 'none', 'inbox', 0, 1, 'edit', 'edit', 'm1', '[\"m1\",\"m2\"]', ?, ?);",
            (ts, ts),
        )
        self.conn.execute(
            "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
            "VALUES ('prj_1', 'p', 'g', 'active', 'usr_1', 'direct', ?, ?);",
            (ts, ts),
        )
        self.conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
            (ts,),
        )
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, model, "
            "timeout_s, max_attempts, attempt, fence_seq, created_by, created_at, updated_at) "
            "VALUES ('tsk_fail', 'prj_1', 'T1', 'S', 'general', 'failed', 'agt_w', 'edit', 'm1', 60, 1, 1, 1, "
            "'agt_lead', ?, ?);",
            (ts, ts),
        )
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, model, "
            "timeout_s, max_attempts, attempt, fence_seq, created_by, created_at, updated_at) "
            "VALUES ('tsk_sub', 'prj_1', 'T2', 'S', 'general', 'submitted', 'agt_w', 'dev', 'm1', 60, 3, 1, 1, "
            "'agt_lead', ?, ?);",
            (ts, ts),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _lead_ctx(self, cmd: str, args: dict, approval: str | None = None) -> HandlerContext:
        return _make_ctx(
            self.conn,
            self.tmp.name,
            cmd,
            args,
            principal={"id": "agt_lead", "kind": "agent", "name": "lead1"},
            actor_kind="agent",
            via="token",
            approval=approval,
            k_user=None,
        )

    def test_retry_and_reject_block_disallowed_model_before_creating_or_consuming_approval(self) -> None:
        with transaction(self.conn):
            apr_retry = create_or_get_pending_approval(
                self.conn,
                category="retry_over_limit",
                op="task.retry",
                requested_by="agt_lead",
                params={
                    "task_id": "tsk_fail",
                    "assignee": "w1",
                    "profile": "edit",
                    "model": "m1",
                    "effort": None,
                    "reason": "again",
                    "reuse_workspace": False,
                },
                summary="retry",
                project_id="prj_1",
                target_id="tsk_fail",
                testing_mode=True,
            )
            handle_approval_approve(
                _make_ctx(
                    self.conn,
                    self.tmp.name,
                    "approval.approve",
                    {"approval_id": apr_retry["approval_id"], "op_hash": apr_retry["op_hash"]},
                )
            )
            apr_reject = create_or_get_pending_approval(
                self.conn,
                category="permission_escalation",
                op="task.reject",
                requested_by="agt_lead",
                params={"task_id": "tsk_sub", "assignee": "w1", "profile": "dev", "reason": "fix"},
                summary="reject",
                project_id="prj_1",
                target_id="tsk_sub",
                testing_mode=True,
            )
            handle_approval_approve(
                _make_ctx(
                    self.conn,
                    self.tmp.name,
                    "approval.approve",
                    {"approval_id": apr_reject["approval_id"], "op_hash": apr_reject["op_hash"]},
                )
            )
            # Narrow allowed_models so 'm1' is no longer permitted
            self.conn.execute(
                "UPDATE agents SET default_model = 'm2', allowed_models_json = '[\"m2\"]' WHERE id = 'agt_w';"
            )

        # task.retry without --model must fail MODEL_NOT_ALLOWED before consuming approval
        with self.assertRaises(MeshError) as retry_cm:
            handle_task_retry(
                self._lead_ctx(
                    "task.retry",
                    {"task_id": "tsk_fail", "reason": "again"},
                    approval=apr_retry["approval_id"],
                )
            )
        self.assertEqual((retry_cm.exception.code, retry_cm.exception.exit_code), (c.MODEL_NOT_ALLOWED, c.EXIT_USAGE))
        self.assertEqual(
            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_retry["approval_id"],)).fetchone()["status"],
            "approved",
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_fail';").fetchone()["status"],
            "failed",
        )

        # task.reject must fail MODEL_NOT_ALLOWED before consuming approval
        with self.assertRaises(MeshError) as rej_cm:
            handle_task_reject(
                self._lead_ctx(
                    "task.reject",
                    {"task_id": "tsk_sub", "reason": "fix"},
                    approval=apr_reject["approval_id"],
                )
            )
        self.assertEqual((rej_cm.exception.code, rej_cm.exception.exit_code), (c.MODEL_NOT_ALLOWED, c.EXIT_USAGE))
        self.assertEqual(
            self.conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr_reject["approval_id"],)).fetchone()["status"],
            "approved",
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_sub';").fetchone()["status"],
            "submitted",
        )

        # Also verify no pending approval is created when called without --approval
        before_cnt = self.conn.execute("SELECT COUNT(*) FROM approvals;").fetchone()[0]
        with self.assertRaises(MeshError):
            handle_task_retry(self._lead_ctx("task.retry", {"task_id": "tsk_fail", "reason": "again"}))
        with self.assertRaises(MeshError):
            handle_task_reject(self._lead_ctx("task.reject", {"task_id": "tsk_sub", "reason": "fix"}))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM approvals;").fetchone()[0], before_cnt)

        # Overriding to allowed model 'm2' on approval-gated task.retry (lead) reaches approval gate without erroring on stale model
        with self.assertRaises(MeshError) as gate_cm:
            handle_task_retry(
                self._lead_ctx(
                    "task.retry",
                    {"task_id": "tsk_fail", "model": "m2", "reason": "switch model"},
                )
            )
        self.assertEqual(gate_cm.exception.code, c.APPROVAL_REQUIRED)

        # Effort error takes precedence over stale task['model'] even when approval-gated
        with transaction(self.conn):
            self.conn.execute("UPDATE agents SET adapter = 'vibe' WHERE id = 'agt_w';")
        with self.assertRaises(MeshError) as eff_cm:
            handle_task_retry(
                self._lead_ctx(
                    "task.retry",
                    {"task_id": "tsk_fail", "effort": "low", "reason": "bad effort"},
                )
            )
        self.assertEqual(eff_cm.exception.code, c.EFFORT_UNSUPPORTED)
        with transaction(self.conn):
            self.conn.execute("UPDATE agents SET adapter = 'none' WHERE id = 'agt_w';")

        # Ungated task.reject defers stale model checks to T6 instead of wedging
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET profile = 'edit' WHERE id = 'tsk_sub';")
        ungated_rej = handle_task_reject(self._lead_ctx("task.reject", {"task_id": "tsk_sub", "reason": "fix"}))
        self.assertEqual(ungated_rej.data["status"], "rework")

        # Overriding to allowed model 'm2' on task.retry (user/direct) succeeds cleanly
        ok_res = handle_task_retry(
            _make_ctx(
                self.conn,
                self.tmp.name,
                "task.retry",
                {"task_id": "tsk_fail", "model": "m2", "reason": "switch model"},
            )
        )
        self.assertEqual((ok_res.data["status"], ok_res.data["model"]), ("queued", "m2"))

        # Ungated task.retry without --model also defers stale model checks to T6
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET status = 'failed', model = 'm1', max_attempts = 3 WHERE id = 'tsk_fail';")
        ungated_ret = handle_task_retry(self._lead_ctx("task.retry", {"task_id": "tsk_fail", "reason": "retry"}))
        self.assertEqual((ungated_ret.data["status"], ungated_ret.data["model"]), ("queued", "m1"))


class TestP2_2TaskStartPrechecksIdempotencyAndNotify(unittest.TestCase):
    """P2-2: prepare_task_start_workspace pre-checks gates, persists idempotency on WorkspaceError,
    and notifies waiters + wakes scheduler."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r2-p2-2-")
        os.makedirs(os.path.join(self.tmp, "run"), mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "workspaces"), mode=0o700, exist_ok=True)
        self.fixture = GitFixture()
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for k in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[k]
        os.environ.update(self.fixture.git_env_vars())

        self.repo = self.fixture.make_k0("R2P22")
        self.integ = "mesh/prj_1/integration"
        self.fixture.git(self.repo, "branch", self.integ, "main")

        self.conn = init_db(os.path.join(self.tmp, "mesh.db"))
        now = now_iso_utc()
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (now,)
        )
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_w', 'agent', 'w1', ?);", (now,)
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, default_profile, "
            "default_model, allowed_models_json, default_effort, allowed_efforts_json, created_at, updated_at) "
            "VALUES ('agt_w', 'none', 'inbox', 0, 1, 'edit', 'm1', '[\"m1\"]', 'medium', '[\"medium\"]', ?, ?);",
            (now, now),
        )
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_w2', 'agent', 'w2', ?);", (now,)
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, default_profile, "
            "default_model, allowed_models_json, created_at, updated_at) "
            "VALUES ('agt_w2', 'none', 'inbox', 0, 1, 'edit', 'm1', '[\"m1\"]', ?, ?);",
            (now, now),
        )
        self.tok_w, lookup, shash = generate_agent_token()
        self.conn.execute(
            "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
            "VALUES (?, 'agt_w', 'agent_token', ?, ?, ?);",
            (generate_id("cred"), lookup, shash, now),
        )
        self.conn.execute(
            "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
            "created_by, created_via, created_at, updated_at) "
            "VALUES ('prj_1', 'P', 'G', 'active', ?, 'main', ?, 'usr_1', 'direct', ?, ?);",
            (self.repo, self.integ, now, now),
        )
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, model, effort, "
            "timeout_s, max_attempts, fence_seq, queued_at, offered_at, created_by, created_at, updated_at) "
            "VALUES ('tsk_other', 'prj_1', 'O', 'S', 'general', 'running', 'agt_w', 'edit', 'm1', 'medium', "
            "60, 2, 1, ?, ?, 'usr_1', ?, ?);",
            (now, now, now, now),
        )
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, model, effort, "
            "timeout_s, max_attempts, fence_seq, queued_at, offered_at, created_by, created_at, updated_at) "
            "VALUES ('tsk_q', 'prj_1', 'Q', 'S', 'code', 'queued', 'agt_w', 'edit', 'm1', 'medium', "
            "60, 2, 0, ?, ?, 'usr_1', ?, ?);",
            (now, now, now, now),
        )
        record_event(
            self.conn,
            type_="task.queued",
            actor_id="usr_1",
            actor_kind="user",
            via="direct",
            project_id="prj_1",
            task_id="tsk_q",
            to_state="queued",
        )
        self.conn.commit()

        self.srv = DaemonServer(home=self.tmp, testing_mode=True)
        self.srv.db_conn = self.conn
        self.srv._scheduler_wake = asyncio.Event()

    def tearDown(self) -> None:
        self.conn.close()
        self.fixture.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _send(self, payload: dict) -> dict:
        sock_a, sock_b = socket.socketpair()
        reader_s, writer_s = await asyncio.open_connection(sock=sock_b)
        reader_c, writer_c = await asyncio.open_connection(sock=sock_a)
        with mock.patch("mesh.daemon.server.get_peer_credentials",
                        return_value=fake_peer_creds()), \
                mock.patch("mesh.daemon.server.classify_peer_run_session", return_value=None), \
                mock.patch.object(self.srv, "refresh_run_caches"):
            writer_c.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer_c.drain()
            await self.srv.handle_client(reader_s, writer_s)
            line = await reader_c.readline()
        writer_c.close()
        writer_s.close()
        return json.loads(line.decode("utf-8"))

    def _ws_path(self) -> str:
        return compute_task_workspace_path(os.path.join(self.tmp, "workspaces"), "prj_1", "tsk_q", attempt=1)

    def _assert_no_branch_or_worktree(self) -> None:
        self.assertFalse(os.path.exists(self._ws_path().rstrip("/")), "worktree should not be created when gated")
        cp = self.fixture.git(self.repo, "rev-parse", "--verify", "refs/heads/mesh/prj_1/tsk_q", check=False)
        self.assertNotEqual(cp.returncode, 0, "task branch should not be created when gated")

    def test_prechecks_prevent_worktree_creation_when_start_is_gated(self) -> None:
        auth = {"type": "token", "token": self.tok_w}
        now = now_iso_utc()

        async def _run() -> None:
            # 1. Agent concurrency limit reached -> CONCURRENCY_LIMIT, no worktree/branch
            # (even if workspace path is squatted, must not fail task with workspace_error!)
            ws = self._ws_path()
            os.makedirs(ws, exist_ok=True)
            Path(ws, "squatter.txt").write_text("squat\n", encoding="utf-8")
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                    "boot_id, run_dir, argv_redacted_json, started_at) "
                    "VALUES ('run_busy', 'external', 'tsk_other', 'prj_1', 'agt_w', 1, 1, 'none', 'running', "
                    "'b1', '/tmp/r1', '[]', ?);",
                    (now,),
                )
            r_busy = await self._send({"v": 1, "cmd": "task.start", "auth": auth, "args": {"task_id": "tsk_q"}})
            self.assertFalse(r_busy["ok"])
            self.assertEqual((r_busy["exit_code"], r_busy["error"]["code"]), (c.EXIT_BUSY, c.CONCURRENCY_LIMIT))
            self.assertEqual(
                self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_q';").fetchone()["status"], "queued"
            )
            shutil.rmtree(ws)
            self._assert_no_branch_or_worktree()

            # 2. Global run cap reached (agent max_concurrency raised, global cap=1 with w2 active)
            with transaction(self.conn):
                self.conn.execute("UPDATE runs SET agent_id = 'agt_w2' WHERE id = 'run_busy';")
                self.conn.execute("UPDATE agents SET max_concurrency = 2 WHERE id = 'agt_w';")
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                    "VALUES ('scheduler.max_total_runs', '1', ?, 'usr_1');",
                    (now,),
                )
            r_gcap = await self._send({"v": 1, "cmd": "task.start", "auth": auth, "args": {"task_id": "tsk_q"}})
            self.assertFalse(r_gcap["ok"])
            self.assertEqual((r_gcap["exit_code"], r_gcap["error"]["code"]), (c.EXIT_BUSY, c.CONCURRENCY_LIMIT))
            self._assert_no_branch_or_worktree()

            # 3. Active lease held -> ALREADY_EXISTS, no worktree/branch
            with transaction(self.conn):
                self.conn.execute("DELETE FROM settings WHERE key = 'scheduler.max_total_runs';")
                self.conn.execute("UPDATE runs SET status = 'exited' WHERE id = 'run_busy';")
                self.conn.execute(
                    "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                    "boot_id, run_dir, argv_redacted_json, started_at) "
                    "VALUES ('run_lease', 'external', 'tsk_q', 'prj_1', 'agt_w2', 1, 1, 'none', 'running', "
                    "'b1', '/tmp/r2', '[]', ?);",
                    (now,),
                )
                self.conn.execute(
                    "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, "
                    "slots_in_use_at_acquire, limit_at_acquire, acquired_at, expires_at, heartbeat_at) "
                    "VALUES ('task:tsk_q', 'run_lease', 'agent', 'agt_w2', 'agt_w2', 1, 0, 1, ?, ?, ?);",
                    (now, "2099-01-01T00:00:00Z", now),
                )
            r_lease = await self._send({"v": 1, "cmd": "task.start", "auth": auth, "args": {"task_id": "tsk_q"}})
            self.assertFalse(r_lease["ok"])
            self.assertEqual((r_lease["exit_code"], r_lease["error"]["code"]), (c.EXIT_CONFLICT, c.ALREADY_EXISTS))
            self._assert_no_branch_or_worktree()

            # 4. Disallowed effort and model -> EFFORT_NOT_ALLOWED / MODEL_NOT_ALLOWED, no worktree/branch
            with transaction(self.conn):
                self.conn.execute("DELETE FROM leases WHERE key = 'task:tsk_q';")
                self.conn.execute("UPDATE runs SET status = 'exited' WHERE id = 'run_lease';")
                self.conn.execute(
                    "UPDATE agents SET default_effort = 'low', allowed_efforts_json = '[\"low\"]' WHERE id = 'agt_w';"
                )
            r_eff = await self._send({"v": 1, "cmd": "task.start", "auth": auth, "args": {"task_id": "tsk_q"}})
            self.assertFalse(r_eff["ok"])
            self.assertEqual((r_eff["exit_code"], r_eff["error"]["code"]), (c.EXIT_USAGE, c.EFFORT_NOT_ALLOWED))
            self._assert_no_branch_or_worktree()

            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE agents SET default_model = 'm2', allowed_models_json = '[\"m2\"]', "
                    "default_effort = 'medium', allowed_efforts_json = '[\"medium\"]' WHERE id = 'agt_w';"
                )
            r_mod = await self._send({"v": 1, "cmd": "task.start", "auth": auth, "args": {"task_id": "tsk_q"}})
            self.assertFalse(r_mod["ok"])
            self.assertEqual((r_mod["exit_code"], r_mod["error"]["code"]), (c.EXIT_USAGE, c.MODEL_NOT_ALLOWED))
            self.assertEqual(
                self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_q';").fetchone()["status"], "queued"
            )
            self._assert_no_branch_or_worktree()

            # 5. Once unblocked after integration advances, task.start picks the latest integration HEAD as base_commit.
            wt_adv = self.fixture.path("wt-adv-integ")
            self.fixture.git(self.repo, "worktree", "add", wt_adv, self.integ)
            Path(wt_adv, "adv.txt").write_text("adv\n", encoding="utf-8")
            self.fixture.git(wt_adv, "add", "adv.txt")
            self.fixture.git(wt_adv, "commit", "-q", "-m", "advance integration", identity=True)
            new_integ_sha = self.fixture.head(wt_adv)
            self.fixture.git(self.repo, "worktree", "remove", wt_adv)
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE agents SET default_model = 'm1', allowed_models_json = '[\"m1\"]' WHERE id = 'agt_w';"
                )
            r_ok = await self._send({"v": 1, "cmd": "task.start", "auth": auth, "args": {"task_id": "tsk_q"}})
            self.assertTrue(r_ok["ok"], r_ok)
            row_t = self.conn.execute("SELECT status, base_commit FROM tasks WHERE id = 'tsk_q';").fetchone()
            self.assertEqual(row_t["status"], "running")
            self.assertEqual(row_t["base_commit"], new_integ_sha)

        asyncio.run(_run())

    def test_workspace_error_stores_idempotency_and_notifies_waiters_and_scheduler(self) -> None:
        auth = {"type": "token", "token": self.tok_w}
        ws = self._ws_path()
        os.makedirs(ws, exist_ok=True)
        Path(ws, "squatter.txt").write_text("squat\n", encoding="utf-8")

        async def _run() -> None:
            notify_calls = 0
            orig_notify = self.srv.notify_waiters

            async def spy_notify() -> None:
                nonlocal notify_calls
                notify_calls += 1
                await orig_notify()

            with mock.patch.object(self.srv, "notify_waiters", side_effect=spy_notify):
                r1 = await self._send({
                    "v": 1,
                    "cmd": "task.start",
                    "auth": auth,
                    "args": {"task_id": "tsk_q"},
                    "idempotency_key": "k-ws-err-1",
                })
            self.assertFalse(r1["ok"], r1)
            self.assertEqual((r1["exit_code"], r1["error"]["code"]), (c.EXIT_CONFLICT, c.INVALID_TRANSITION))
            self.assertEqual(r1["error"]["details"]["reason"], "workspace_error")
            self.assertIs(r1["error"]["details"]["failed"], True)
            self.assertGreaterEqual(notify_calls, 1)
            self.assertTrue(self.srv._scheduler_wake.is_set())

            # Idempotency row stored for agt_w / k-ws-err-1
            irow = self.conn.execute(
                "SELECT exit_code FROM idempotency WHERE principal_id = 'agt_w' AND key = 'k-ws-err-1';"
            ).fetchone()
            self.assertIsNotNone(irow)
            self.assertEqual(irow["exit_code"], c.EXIT_CONFLICT)

            # Retry with same idempotency_key replays workspace_error (failed=True)
            r2 = await self._send({
                "v": 1,
                "cmd": "task.start",
                "auth": auth,
                "args": {"task_id": "tsk_q"},
                "idempotency_key": "k-ws-err-1",
            })
            self.assertFalse(r2["ok"], r2)
            self.assertTrue(r2["idempotent_replay"])
            self.assertEqual(r2["exit_code"], c.EXIT_CONFLICT)
            self.assertEqual(r2["error"]["details"]["reason"], "workspace_error")
            self.assertIs(r2["error"]["details"]["failed"], True)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
