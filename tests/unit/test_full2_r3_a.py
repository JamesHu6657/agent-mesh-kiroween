"""Regression tests for full2-r3.md Group A findings (P1-1, P1-6, and P2-6 task.close)."""

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import mesh.constants as c
from mesh.core import invariants
from mesh.core.auth import generate_agent_token
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.gitops import run_git
from mesh.core.ids import generate_id
from mesh.core.workspaces import WorkspaceError, compute_task_workspace_path
from mesh.daemon.handlers import task as task_handlers
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.project import handle_project_deliver
from mesh.daemon.handlers.task import handle_task_close
from mesh.daemon.server import DaemonServer, dispatch_internal_request
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
        request_id="req_full2_r3_a",
        home=home,
        cmd=cmd,
        args=args,
        approval=approval,
        k_user=k_user,
        testing_mode=True,
    )


async def _send_req(srv: DaemonServer, payload: dict) -> dict:
    sock_a, sock_b = socket.socketpair()
    reader_s, writer_s = await asyncio.open_connection(sock=sock_b)
    reader_c, writer_c = await asyncio.open_connection(sock=sock_a)
    try:
        with mock.patch(
            "mesh.daemon.server.get_peer_credentials",
            return_value=fake_peer_creds(),
        ), mock.patch(
            "mesh.daemon.server.classify_peer_run_session", return_value=None
        ), mock.patch.object(srv, "refresh_run_caches"):
            writer_c.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer_c.drain()
            await srv.handle_client(reader_s, writer_s)
            line = await reader_c.readline()
    finally:
        writer_c.close()
        writer_s.close()
    return json.loads(line.decode("utf-8"))


class _GitRepoTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r3-a-")
        for sub in ("run", "runs", "workspaces", "logs"):
            os.makedirs(os.path.join(self.tmp, sub), mode=0o700, exist_ok=True)
        self.fixture = GitFixture()
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for k in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[k]
        os.environ.update(self.fixture.git_env_vars())

        self.repo = self.fixture.make_k0("R3A")
        self.base_sha = self.fixture.head(self.repo, "main")
        self.integ = "mesh/prj_1/integration"
        self.fixture.git(self.repo, "branch", self.integ, "main")

        self.conn = init_db(os.path.join(self.tmp, "mesh.db"))
        ts = "2026-01-01T00:00:00Z"
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
                (ts,),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);",
                (ts,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_lead', 'none', 'inbox', 1, 1, ?, ?);",
                (ts, ts),
            )
            self.tok_lead, l_lk, l_sh = generate_agent_token()
            self.cred_lead = generate_id("cred")
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
                "VALUES (?, 'agt_lead', 'agent_token', ?, ?, ?);",
                (self.cred_lead, l_lk, l_sh, ts),
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

    def tearDown(self) -> None:
        self.conn.close()
        self.fixture.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

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


class TestP1_1ReverseI9PriorIntegrationWhitewash(_GitRepoTestBase):
    """P1-1: later normal merges or --superseded-by must not excuse pre-existing leaked commits."""

    def test_later_normal_merge_does_not_whitewash_prior_fast_forwarded_work(self) -> None:
        # T1 has B0 -> C1 -> C2; C1 was fast-forwarded onto integration, C2 left on branch.
        c_shas = self._commit_chain(
            "mesh/prj_1/tsk_1", self.base_sha, [("c1.txt", "1\n"), ("c2.txt", "2\n")]
        )
        c1 = c_shas[0]
        self._git_out("update-ref", f"refs/heads/{self.integ}", c1)
        self._insert_task("tsk_1", "cancelled", "mesh/prj_1/tsk_1", self.base_sha)

        # Before T2 merges, reverse I9 catches tsk_1
        viols_before = invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1")
        self.assertEqual([v.details["task_id"] for v in viols_before], ["tsk_1"])

        # Legitimate task T2 starts from integration (C1) and merges normally on top of C1
        self._commit_chain("mesh/prj_1/tsk_2", c1, [("t2.txt", "2\n")])
        mc_2 = self._merge_branch("mesh/prj_1/tsk_2", "Merge tsk_2")
        self._insert_task(
            "tsk_2", "merged", "mesh/prj_1/tsk_2", c1,
            merge_json={"merge_commit": mc_2, "noop": False},
        )

        # T2's merge must NOT whitewash tsk_1's unmerged C1!
        viols_after = invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1")
        self.assertEqual([v.details["task_id"] for v in viols_after], ["tsk_1"])
        self.assertEqual(viols_after[0].details["merge_commit"], c1)
        with self.assertRaises(MeshError) as cm:
            handle_project_deliver(_make_ctx(self.conn, self.tmp, "project.deliver", {"project_id": "prj_1"}))
        self.assertEqual(cm.exception.code, c.INVALID_TRANSITION)

    def test_syncing_later_recorded_merge_does_not_whitewash_prior_fast_forwarded_work(self) -> None:
        # T1 has B0 -> C1, fast-forwards C1 onto integration, T2 merges on top of C1,
        # and T1 syncs mc_2 into T1's branch (parts[0] in branch_commits).
        c_shas = self._commit_chain("mesh/prj_1/tsk_1", self.base_sha, [("c1.txt", "1\n")])
        c1 = c_shas[0]
        self._git_out("update-ref", f"refs/heads/{self.integ}", c1)

        self._commit_chain("mesh/prj_1/tsk_2", c1, [("t2.txt", "2\n")])
        mc_2 = self._merge_branch("mesh/prj_1/tsk_2", "Merge tsk_2")
        self._insert_task(
            "tsk_2", "merged", "mesh/prj_1/tsk_2", c1,
            merge_json={"merge_commit": mc_2, "noop": False},
        )

        wt1 = self.fixture.path("wt-sync-t1")
        self.fixture.git(self.repo, "worktree", "add", wt1, "mesh/prj_1/tsk_1")
        self.fixture.git(wt1, "merge", "--no-ff", "-m", "sync mc_2", mc_2, identity=True)
        self.fixture.git(self.repo, "worktree", "remove", wt1)
        self._insert_task("tsk_1", "accepted", "mesh/prj_1/tsk_1", self.base_sha)

        viols = invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1")
        self.assertEqual([v.details["task_id"] for v in viols], ["tsk_1"])
        with self.assertRaises(MeshError) as cm:
            handle_project_deliver(_make_ctx(self.conn, self.tmp, "project.deliver", {"project_id": "prj_1"}))
        self.assertEqual(cm.exception.code, c.INVALID_TRANSITION)

    def test_superseded_by_later_unrelated_merge_cannot_whitewash_prior_unrecorded_leak(self) -> None:
        # T_sneaky (base = B0) enters integration via unrecorded merge U.
        self._commit_chain("mesh/prj_1/tsk_sneaky", self.base_sha, [("sneaky.txt", "s\n")])
        unrec_mc = self._merge_branch("mesh/prj_1/tsk_sneaky", "Unrecorded merge")
        self._insert_task("tsk_sneaky", "accepted", "mesh/prj_1/tsk_sneaky", self.base_sha)

        # Later legitimate task T_later starts from U (or integration) and merges normally.
        self._commit_chain("mesh/prj_1/tsk_later", unrec_mc, [("later.txt", "l\n")])
        mc_later = self._merge_branch("mesh/prj_1/tsk_later", "Merge tsk_later")
        self._insert_task(
            "tsk_later", "merged", "mesh/prj_1/tsk_later", unrec_mc,
            merge_json={"merge_commit": mc_later, "noop": False},
        )

        # Closing tsk_sneaky (even as cancelled/accepted) --superseded-by tsk_later MUST be rejected!
        with self.assertRaises(MeshError) as close_cm:
            handle_task_close(
                _make_ctx(
                    self.conn,
                    self.tmp,
                    "task.close",
                    {"task_id": "tsk_sneaky", "superseded_by": "tsk_later"},
                )
            )
        self.assertEqual((close_cm.exception.code, close_cm.exception.exit_code), (c.INVALID_TRANSITION, c.EXIT_CONFLICT))

        # Even if a bogus task.closed event exists in DB, reverse I9 and deliver MUST fail!
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
                payload={"superseded_by": "tsk_later", "from_state": "accepted"},
            )
        viols = invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1")
        self.assertEqual([v.details["task_id"] for v in viols], ["tsk_sneaky"])
        self.assertEqual(viols[0].details["merge_commit"], unrec_mc)
        with self.assertRaises(MeshError) as del_cm:
            handle_project_deliver(_make_ctx(self.conn, self.tmp, "project.deliver", {"project_id": "prj_1"}))
        self.assertEqual(del_cm.exception.code, c.INVALID_TRANSITION)

    def test_superseded_by_must_name_merging_task_not_subsequent_merged_task(self) -> None:
        # T1 has B0 -> C1; T2 (merged) built B0 -> C1 -> T2_1 and merged;
        # T3 (merged) built from mc_2 -> T3_1 and merged.
        # T1 must only close --superseded-by T2, NOT --superseded-by T3.
        c1_shas = self._commit_chain("mesh/prj_1/tsk_1", self.base_sha, [("c1.txt", "1\n")])
        c1 = c1_shas[0]
        self._commit_chain("mesh/prj_1/tsk_2", c1, [("t2.txt", "2\n")])
        mc_2 = self._merge_branch("mesh/prj_1/tsk_2", "Merge tsk_2")
        self._insert_task(
            "tsk_2", "merged", "mesh/prj_1/tsk_2", self.base_sha,
            merge_json={"merge_commit": mc_2, "noop": False},
        )
        self._commit_chain("mesh/prj_1/tsk_3", mc_2, [("t3.txt", "3\n")])
        mc_3 = self._merge_branch("mesh/prj_1/tsk_3", "Merge tsk_3")
        self._insert_task(
            "tsk_3", "merged", "mesh/prj_1/tsk_3", mc_2,
            merge_json={"merge_commit": mc_3, "noop": False},
        )
        self._insert_task("tsk_1", "cancelled", "mesh/prj_1/tsk_1", self.base_sha)

        with self.assertRaises(MeshError) as bad_cm:
            handle_task_close(
                _make_ctx(self.conn, self.tmp, "task.close", {"task_id": "tsk_1", "superseded_by": "tsk_3"})
            )
        self.assertEqual(bad_cm.exception.code, c.INVALID_TRANSITION)

        ok_res = handle_task_close(
            _make_ctx(self.conn, self.tmp, "task.close", {"task_id": "tsk_1", "superseded_by": "tsk_2"})
        )
        self.assertEqual(ok_res.data["superseded_by"], "tsk_2")
        self.assertEqual(invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1"), [])


class TestP2_6TaskCloseAbandonedTrailingAndOffload(_GitRepoTestBase):
    """P2-6 (task.close): close failed/cancelled with abandoned trailing commit, plus off-TX offload & drift check."""

    def test_close_failed_and_cancelled_with_abandoned_trailing_commit(self) -> None:
        # Both failed and cancelled tasks have base -> C1 (merged by tsk_sup) -> C2 (abandoned).
        for idx, st in enumerate(("failed", "cancelled")):
            tid = f"tsk_trail_{idx}"
            sup_id = f"tsk_sup_{idx}"
            shas = self._commit_chain(
                f"mesh/prj_1/{tid}", self.base_sha,
                [(f"c1_{idx}.txt", "c1\n"), (f"c2_{idx}.txt", "c2\n")],
            )
            c1 = shas[0]
            self._commit_chain(f"mesh/prj_1/{sup_id}", c1, [(f"sup_{idx}.txt", "s\n")])
            mc_sup = self._merge_branch(f"mesh/prj_1/{sup_id}", f"Merge {sup_id}")
            self._insert_task(
                sup_id, "merged", f"mesh/prj_1/{sup_id}", self.base_sha,
                merge_json={"merge_commit": mc_sup, "noop": False},
            )
            self._insert_task(tid, st, f"mesh/prj_1/{tid}", self.base_sha)

            res = handle_task_close(
                _make_ctx(self.conn, self.tmp, "task.close", {"task_id": tid, "superseded_by": sup_id})
            )
            self.assertEqual((res.data["status"], res.data["superseded_by"]), ("cancelled", sup_id))

        # Unrelated merged task still cannot close a failed/cancelled task with trailing commit
        self._commit_chain("mesh/prj_1/tsk_unrel", self.base_sha, [("u.txt", "u\n")])
        mc_u = self._merge_branch("mesh/prj_1/tsk_unrel", "Merge unrel")
        self._insert_task(
            "tsk_unrel", "merged", "mesh/prj_1/tsk_unrel", self.base_sha,
            merge_json={"merge_commit": mc_u, "noop": False},
        )
        self._commit_chain("mesh/prj_1/tsk_nope", self.base_sha, [("n1.txt", "1\n"), ("n2.txt", "2\n")])
        self._insert_task("tsk_nope", "failed", "mesh/prj_1/tsk_nope", self.base_sha)
        with self.assertRaises(MeshError) as bad_cm:
            handle_task_close(
                _make_ctx(self.conn, self.tmp, "task.close", {"task_id": "tsk_nope", "superseded_by": "tsk_unrel"})
            )
        self.assertEqual(bad_cm.exception.code, c.INVALID_TRANSITION)

        # Also check deliver succeeds cleanly once detached from main
        self._git_out("checkout", "-q", "--detach", self.base_sha)
        self.assertEqual(invariants.check_i9_integrated_unmerged_tasks(self.conn, "prj_1"), [])
        del_res = handle_project_deliver(_make_ctx(self.conn, self.tmp, "project.deliver", {"project_id": "prj_1"}))
        self.assertEqual(del_res.data["new"], mc_u)

        # If C1 was fast-forwarded onto integration BEFORE tsk_pre_sup started (so tsk_pre_sup.base_commit == C1),
        # closing tsk_pre_trail --superseded-by tsk_pre_sup MUST still be rejected!
        pre_shas = self._commit_chain("mesh/prj_1/tsk_pre_trail", self.base_sha, [("p1.txt", "1\n"), ("p2.txt", "2\n")])
        p1 = pre_shas[0]
        self._git_out("update-ref", f"refs/heads/{self.integ}", p1)
        self._commit_chain("mesh/prj_1/tsk_pre_sup", p1, [("ps.txt", "s\n")])
        mc_pre = self._merge_branch("mesh/prj_1/tsk_pre_sup", "Merge tsk_pre_sup")
        self._insert_task(
            "tsk_pre_sup", "merged", "mesh/prj_1/tsk_pre_sup", p1,
            merge_json={"merge_commit": mc_pre, "noop": False},
        )
        self._insert_task("tsk_pre_trail", "failed", "mesh/prj_1/tsk_pre_trail", self.base_sha)
        with self.assertRaises(MeshError) as pre_cm:
            handle_task_close(
                _make_ctx(self.conn, self.tmp, "task.close", {"task_id": "tsk_pre_trail", "superseded_by": "tsk_pre_sup"})
            )
        self.assertEqual(pre_cm.exception.code, c.INVALID_TRANSITION)

    def test_task_close_offloads_git_off_event_loop_and_rechecks_cred_and_drift(self) -> None:
        srv = DaemonServer(home=self.tmp, testing_mode=True)
        srv.db_conn = self.conn

        # Task A (accepted, unintegrated) and Task B (merged)
        self._commit_chain("mesh/prj_1/tsk_a", self.base_sha, [("a.txt", "a\n")])
        self._insert_task("tsk_a", "accepted", "mesh/prj_1/tsk_a", self.base_sha)
        self._commit_chain("mesh/prj_1/tsk_b", self.base_sha, [("b.txt", "b\n")])
        mc_b = self._merge_branch("mesh/prj_1/tsk_b", "Merge tsk_b")
        self._insert_task(
            "tsk_b", "merged", "mesh/prj_1/tsk_b", self.base_sha,
            merge_json={"merge_commit": mc_b, "noop": False},
        )

        # 1. Verify git verification runs off-TX and off event loop, and token revocation during offload
        # is caught by recheck_tx_credential_and_authz inside TX.
        entered = threading.Event()
        release = threading.Event()
        observed_in_tx: list[bool] = []
        observed_thread_ids: list[int] = []
        main_tid = threading.get_ident()
        real_run_git = run_git

        def _blocking_run_git(args, cwd=None, **kw):
            if args and args[0] == "merge-base" and "--all" in args and not entered.is_set():
                observed_in_tx.append(self.conn.in_transaction)
                observed_thread_ids.append(threading.get_ident())
                entered.set()
                release.wait(timeout=1.5)
            return real_run_git(args, cwd=cwd, **kw)

        async def _run_revoke_test() -> None:
            async def _revoke_on_enter() -> None:
                try:
                    for _ in range(300):
                        if entered.is_set():
                            # Event loop stays alive while worker thread runs git!
                            with transaction(self.conn):
                                self.conn.execute(
                                    "UPDATE credentials SET revoked_at = ? WHERE id = ?;",
                                    (now_iso_utc(), self.cred_lead),
                                )
                            return
                        await asyncio.sleep(0.01)
                    self.fail("Timed out waiting for off-TX task.close git check")
                finally:
                    release.set()

            with mock.patch("mesh.core.gitops.run_git", side_effect=_blocking_run_git):
                req_t = asyncio.create_task(
                    _send_req(
                        srv,
                        {
                            "v": 1,
                            "cmd": "task.close",
                            "auth": {"type": "token", "token": self.tok_lead},
                            "args": {"task_id": "tsk_a", "superseded_by": "tsk_b"},
                        },
                    )
                )
                rev_t = asyncio.create_task(_revoke_on_enter())
                resp, _ = await asyncio.gather(req_t, rev_t)

            self.assertFalse(resp["ok"], resp)
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_REVOKED))
            self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_a';").fetchone()["status"], "accepted")

        asyncio.run(_run_revoke_test())
        self.assertEqual(observed_in_tx, [False], "task.close git check must run before BEGIN IMMEDIATE")
        self.assertEqual(len(observed_thread_ids), 1)
        self.assertNotEqual(observed_thread_ids[0], main_tid, "task.close git check must run in worker thread")

        # 2. Unrevoke token and verify git drift between prepare_task_close and TX fails closed!
        with transaction(self.conn):
            self.conn.execute("UPDATE credentials SET revoked_at = NULL WHERE id = ?;", (self.cred_lead,))

        async def _run_drift_test() -> None:
            orig_prepare = task_handlers.prepare_task_close

            async def _prepare_then_fast_forward(ctx: HandlerContext) -> None:
                await orig_prepare(ctx)
                # Mutate integration branch AFTER off-TX prep succeeded, BEFORE BEGIN IMMEDIATE!
                a_tip = self._git_out("rev-parse", "refs/heads/mesh/prj_1/tsk_a")
                self._git_out("update-ref", f"refs/heads/{self.integ}", a_tip)

            with mock.patch("mesh.daemon.handlers.task.prepare_task_close", side_effect=_prepare_then_fast_forward):
                resp = await _send_req(
                    srv,
                    {
                        "v": 1,
                        "cmd": "task.close",
                        "auth": {"type": "token", "token": self.tok_lead},
                        "args": {"task_id": "tsk_a", "superseded_by": "tsk_b"},
                    },
                )
            self.assertFalse(resp["ok"], resp)
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_CONFLICT, c.INVALID_TRANSITION))
            self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_a';").fetchone()["status"], "accepted")

        asyncio.run(_run_drift_test())

        # 3. Reset integration back to mc_b and verify DB drift between prep and TX falls back & fails closed
        self._git_out("update-ref", f"refs/heads/{self.integ}", mc_b)

        async def _run_db_drift_test() -> None:
            orig_prepare = task_handlers.prepare_task_close

            async def _prepare_then_change_other(ctx: HandlerContext) -> None:
                await orig_prepare(ctx)
                # Flip superseding task's merge_json and integration branch -> DB + git drift
                with transaction(self.conn):
                    self.conn.execute(
                        "UPDATE tasks SET status = 'merge_failed' WHERE id = 'tsk_b';"
                    )

            with mock.patch("mesh.daemon.handlers.task.prepare_task_close", side_effect=_prepare_then_change_other):
                resp = await _send_req(
                    srv,
                    {
                        "v": 1,
                        "cmd": "task.close",
                        "auth": {"type": "token", "token": self.tok_lead},
                        "args": {"task_id": "tsk_a", "superseded_by": "tsk_b"},
                    },
                )
            self.assertFalse(resp["ok"], resp)
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_CONFLICT, c.INVALID_TRANSITION))

        asyncio.run(_run_db_drift_test())
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET status = 'merged' WHERE id = 'tsk_b';")

        # 4. Verify clean close via server uses offloaded result (no check_i9 call inside TX) + internal dispatch works
        async def _run_clean() -> dict:
            with mock.patch(
                "mesh.core.invariants.check_i9_integrated_unmerged_tasks",
                side_effect=AssertionError("check_i9 must not re-run inside TX when nothing drifted"),
            ):
                return await _send_req(
                    srv,
                    {
                        "v": 1,
                        "cmd": "task.close",
                        "auth": {"type": "token", "token": self.tok_lead},
                        "args": {"task_id": "tsk_a", "superseded_by": "tsk_b"},
                    },
                )
        clean_resp = asyncio.run(_run_clean())
        self.assertTrue(clean_resp["ok"], clean_resp)
        self.assertEqual((clean_resp["data"]["status"], clean_resp["data"]["superseded_by"]), ("cancelled", "tsk_b"))

        # Direct synchronous dispatch_internal_request without prepared_close still works!
        self._commit_chain("mesh/prj_1/tsk_c", self.base_sha, [("c.txt", "c\n")])
        self._insert_task("tsk_c", "accepted", "mesh/prj_1/tsk_c", self.base_sha)
        with transaction(self.conn):
            env = dispatch_internal_request(
                _make_ctx(
                    self.conn,
                    self.tmp,
                    "task.close",
                    {"task_id": "tsk_c", "superseded_by": "tsk_b"},
                ),
                idempotency_key="act:close:1",
            )
        self.assertTrue(env["ok"], env)
        self.assertEqual((env["data"]["status"], env["data"]["superseded_by"]), ("cancelled", "tsk_b"))


class TestP1_6WorkspaceErrorTxRecheckAndAuthz(unittest.TestCase):
    """P1-6: prepare_task_start_workspace WorkspaceError TX must recheck cred, authz, assignee, and ws_res."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r3-p16-")
        for sub in ("run", "runs", "workspaces", "logs"):
            os.makedirs(os.path.join(self.tmp, sub), mode=0o700, exist_ok=True)
        self.conn = init_db(os.path.join(self.tmp, "mesh.db"))
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (now,))
            for wid, name in (("agt_w1", "w1"), ("agt_w2", "w2")):
                self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, ?);", (wid, name, now))
                self.conn.execute(
                    "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, default_profile, "
                    "default_model, allowed_models_json, created_at, updated_at) "
                    "VALUES (?, 'none', 'inbox', 0, 1, 'edit', 'm1', '[\"m1\"]', ?, ?);",
                    (wid, now, now),
                )
            self.tok_w1, lk1, sh1 = generate_agent_token()
            self.cred_w1 = generate_id("cred")
            self.conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
                "VALUES (?, 'agt_w1', 'agent_token', ?, ?, ?);",
                (self.cred_w1, lk1, sh1, now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
                "base_commit, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P', 'G', 'active', '/tmp/fake-repo', 'main', 'mesh/prj_1/integration', "
                "?, 'usr_1', 'direct', ?, ?);",
                ("a" * 40, now, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, model, "
                "timeout_s, max_attempts, fence_seq, queued_at, offered_at, created_by, created_at, updated_at) "
                "VALUES ('tsk_q', 'prj_1', 'Q', 'S', 'code', 'queued', 'agt_w1', 'edit', 'm1', 60, 2, 0, ?, ?, "
                "'usr_1', ?, ?);",
                (now, now, now, now),
            )
        self.srv = DaemonServer(home=self.tmp, testing_mode=True)
        self.srv.db_conn = self.conn
        self.srv._scheduler_wake = asyncio.Event()

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_ws_err_with_concurrent_mutation(self, req: dict, mutate_fn) -> dict:
        entered = threading.Event()
        release = threading.Event()

        def _failing_prep(_plan):
            entered.set()
            release.wait(timeout=2.0)
            raise WorkspaceError("simulated dirty workspace", {"step": "dirty"})

        async def _driver() -> dict:
            async def _mutator() -> None:
                try:
                    for _ in range(300):
                        if entered.is_set():
                            mutate_fn()
                            return
                        await asyncio.sleep(0.01)
                    self.fail("Timed out waiting for workspace prep thread")
                finally:
                    release.set()

            with mock.patch.object(
                task_handlers,
                "plan_code_workspace",
                return_value={
                    "task_id": "tsk_q",
                    "workspace_path": compute_task_workspace_path(os.path.join(self.tmp, "workspaces"), "prj_1", "tsk_q", attempt=1),
                    "branch": "mesh/prj_1/tsk_q",
                },
            ), mock.patch.object(task_handlers, "run_code_workspace_plan", side_effect=_failing_prep):
                t_req = asyncio.create_task(_send_req(self.srv, req))
                t_mut = asyncio.create_task(_mutator())
                resp, _ = await asyncio.gather(t_req, t_mut)
                return resp

        return asyncio.run(_driver())

    def test_revoked_token_during_workspace_error_returns_token_revoked_and_keeps_queued(self) -> None:
        def _revoke() -> None:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE credentials SET revoked_at = ? WHERE id = ?;",
                    (now_iso_utc(), self.cred_w1),
                )

        resp = self._run_ws_err_with_concurrent_mutation(
            {
                "v": 1,
                "cmd": "task.start",
                "auth": {"type": "token", "token": self.tok_w1},
                "args": {"task_id": "tsk_q"},
                "idempotency_key": "k-rev-wserr",
            },
            _revoke,
        )
        self.assertFalse(resp["ok"], resp)
        self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_REVOKED))
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_q';").fetchone()["status"], "queued")
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM idempotency WHERE key = 'k-rev-wserr';").fetchone()
        )

    def test_reassigned_task_during_workspace_error_blocks_former_assignee(self) -> None:
        def _reassign() -> None:
            with transaction(self.conn):
                self.conn.execute("UPDATE tasks SET assignee_agent_id = 'agt_w2' WHERE id = 'tsk_q';")

        resp = self._run_ws_err_with_concurrent_mutation(
            {
                "v": 1,
                "cmd": "task.start",
                "auth": {"type": "token", "token": self.tok_w1},
                "args": {"task_id": "tsk_q"},
                "idempotency_key": "k-reass-wserr",
            },
            _reassign,
        )
        self.assertFalse(resp["ok"], resp)
        self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_FORBIDDEN, c.NOT_ASSIGNEE))
        row = self.conn.execute("SELECT status, assignee_agent_id FROM tasks WHERE id = 'tsk_q';").fetchone()
        self.assertEqual((row["status"], row["assignee_agent_id"]), ("queued", "agt_w2"))

    def test_ws_reserved_idempotency_during_workspace_error_returns_busy_and_keeps_queued(self) -> None:
        from mesh.core.canonical import canonical_json
        import hashlib

        req = {
            "v": 1,
            "cmd": "task.start",
            "auth": {"type": "token", "token": self.tok_w1},
            "args": {"task_id": "tsk_q"},
            "idempotency_key": "k-wsres-wserr",
        }
        req_hash = hashlib.sha256(canonical_json({"cmd": "task.start", "args": {"task_id": "tsk_q"}})).hexdigest()

        def _reserve_ws() -> None:
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('ws_res:task:tsk_q', ?);",
                    (json.dumps({
                        "kind": "delete",
                        "task_id": "tsk_q",
                        "project_id": "prj_1",
                        "idempotency": {
                            "principal_id": "agt_w1",
                            "key": "k-wsres-wserr",
                            "cmd": "task.start",
                            "request_hash": req_hash,
                        },
                    }),),
                )

        resp = self._run_ws_err_with_concurrent_mutation(req, _reserve_ws)
        self.assertFalse(resp["ok"], resp)
        self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_BUSY, c.CONCURRENCY_LIMIT))
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_q';").fetchone()["status"], "queued")

    def test_disabled_principal_during_workspace_error_returns_token_invalid_and_keeps_queued(self) -> None:
        def _disable() -> None:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE principals SET disabled_at = ? WHERE id = 'agt_w1';",
                    (now_iso_utc(),),
                )

        resp = self._run_ws_err_with_concurrent_mutation(
            {
                "v": 1,
                "cmd": "task.start",
                "auth": {"type": "token", "token": self.tok_w1},
                "args": {"task_id": "tsk_q"},
                "idempotency_key": "k-dis-wserr",
            },
            _disable,
        )
        self.assertFalse(resp["ok"], resp)
        self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_INVALID))
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_q';").fetchone()["status"], "queued")


if __name__ == "__main__":
    unittest.main()
