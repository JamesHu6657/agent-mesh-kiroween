"""Regression tests for full2-r1.md findings (P1 idempotency partition for relayed, P2 I9 reverse check).

1. P1 (mesh/daemon/server.py): ``idem_pid = idempotency_partition(principal, via)`` was
   computed before ``via`` became ``"relayed"``, so relayed and non-relayed token requests
   shared one idempotency partition and replayed across channels.
2. P2 (mesh/core/invariants.py, mesh/core/gitops.py, mesh/daemon/children.py): reverse I9
   ``_is_excused`` now excludes ``base_commit`` and all excused commits jointly in a single
   ``git log --stdin`` call via ``run_git(..., input=...)`` and fails closed on git errors.
   (Note: for a single commit ``mb`` already filtered against ``base_commit``, joint
   ``--stdin`` exclusion and 200-commit chunking are mathematically equivalent on real git
   DAGs, so the ``check_i9_integrated_unmerged_tasks`` scenarios pass both before and after
   the #2 refactor, whereas ``run_git(..., input=...)`` directly exercises the new stdin
   support.)
"""

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
from mesh.core.gitops import run_git
from mesh.core.ids import generate_id
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.project import handle_project_deliver
from mesh.daemon.server import DaemonServer, idempotency_partition
from mesh.errors import MeshError
from tests.gitfixture import GitFixture


def _make_ctx(conn: sqlite3.Connection, home: str, cmd: str, args: dict) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal={"id": "user", "kind": "user", "name": "user"},
        actor_kind="user",
        via="direct",
        request_id="req_full2_r1",
        home=home,
        cmd=cmd,
        args=args,
        k_user=b"k" * 32,
        testing_mode=True,
    )


class TestP1RelayedIdempotencyPartition(unittest.TestCase):
    """Finding 1 (P1): relayed and token requests must not share an idempotency partition."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mh-full2-p1-")
        os.makedirs(os.path.join(self.tmpdir, "run"), mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(self.tmpdir, "workspaces"), mode=0o700, exist_ok=True)
        self.conn = init_db(os.path.join(self.tmpdir, "mesh.db"))
        now = now_iso_utc()

        self.user_id = "usr_1"
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'user', 'user', ?);",
            (self.user_id, now),
        )

        # Lead agent (inbox)
        self.lead_id = generate_id("agt")
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', 'ag-lead', ?);",
            (self.lead_id, now),
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) VALUES (?, 'none', 'inbox', 1, '[]', 1, 'edit', ?, ?);",
            (self.lead_id, now, now),
        )

        # Agent with both intake and relay capabilities + real agent token
        self.relayer_id = generate_id("agt")
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', 'ag-relay', ?);",
            (self.relayer_id, now),
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) VALUES (?, 'none', 'inbox', 1, '[\"intake\", \"relay\"]', "
            "1, 'edit', ?, ?);",
            (self.relayer_id, now, now),
        )
        self.tok_relayer, lookup, secret_hash = generate_agent_token()
        self.conn.execute(
            "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
            "VALUES (?, ?, 'agent_token', ?, ?, ?);",
            (generate_id("cred"), self.relayer_id, lookup, secret_hash, now),
        )

        # Seed an active project led by ag-lead
        self.prj_id = generate_id("prj")
        self.conn.execute(
            "INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at) "
            "VALUES (?, 'P1', 'goal', 'active', 'usr_1', 'direct', 1, ?, ?);",
            (self.prj_id, now, now),
        )
        self.assign_id = generate_id("lead")
        self.conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES (?, ?, ?, 'usr_1', 'direct', ?);",
            (self.assign_id, self.prj_id, self.lead_id, now),
        )
        self.conn.commit()

        self.srv = DaemonServer(home=self.tmpdir, testing_mode=True)
        self.srv.db_conn = self.conn

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _send(self, payload: dict) -> dict:
        sock_a, sock_b = socket.socketpair()
        reader_s, writer_s = await asyncio.open_connection(sock=sock_b)
        reader_c, writer_c = await asyncio.open_connection(sock=sock_a)
        with mock.patch("mesh.daemon.server.get_peer_credentials",
                        return_value=(os.getpid(), os.getuid(), os.getgid())), \
                mock.patch("mesh.daemon.server.classify_peer_run_session", return_value=None), \
                mock.patch.object(self.srv, "refresh_run_caches"):
            writer_c.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer_c.drain()
            await self.srv.handle_client(reader_s, writer_s)
            line = await reader_c.readline()
        writer_c.close()
        writer_s.close()
        return json.loads(line.decode("utf-8"))

    def test_partition_formats_preserved(self) -> None:
        p = {"id": "agt_x"}
        self.assertEqual(idempotency_partition(p, "token"), "agt_x")
        self.assertEqual(idempotency_partition(p, "direct"), "agt_x")
        self.assertEqual(idempotency_partition(p, "relayed"), "agt_x#relayed")
        scoped = {"id": "agt_x", "scope": {"role": "lead", "project_id": "prj_1", "run_id": "run_1"}}
        part = idempotency_partition(scoped, "run_token")
        self.assertTrue(part.startswith("agt_x#scope:"), part)

    def test_token_then_relayed_does_not_replay_or_skip_relay_event(self) -> None:
        async def _run() -> None:
            auth = {"type": "token", "token": self.tok_relayer}
            args = {
                "title": "New Project",
                "goal": "ship it",
                "criteria": ["c1"],
                "lead": "ag-lead",
            }
            r1 = await self._send({
                "v": 1, "cmd": "intake.submit", "auth": auth, "args": args,
                "idempotency_key": "k-int-1",
            })
            self.assertTrue(r1["ok"], r1)
            self.assertFalse(r1["idempotent_replay"])
            self.assertEqual(r1["data"]["created_via"], "token")
            prj_tok = r1["data"]["id"]

            # Relayed call with identical cmd, args, idempotency_key must NOT replay the token call.
            r2 = await self._send({
                "v": 1, "cmd": "intake.submit", "auth": auth, "args": args,
                "idempotency_key": "k-int-1",
                "relayed": True,
                "user_statement": "User asked to create this project",
            })
            self.assertTrue(r2["ok"], r2)
            self.assertFalse(r2["idempotent_replay"], "relayed call must not replay non-relayed response")
            self.assertEqual(r2["data"]["created_via"], "relayed")
            prj_rel = r2["data"]["id"]
            self.assertNotEqual(prj_rel, prj_tok)

            # Repeating the relayed call replays the relayed response.
            r3 = await self._send({
                "v": 1, "cmd": "intake.submit", "auth": auth, "args": args,
                "idempotency_key": "k-int-1",
                "relayed": True,
                "user_statement": "User asked to create this project",
            })
            self.assertTrue(r3["ok"], r3)
            self.assertTrue(r3["idempotent_replay"])
            self.assertEqual(r3["data"]["id"], prj_rel)
            self.assertEqual(r3["data"]["created_via"], "relayed")

            # Verify both partitions exist in SQLite and events reflect each channel.
            pids = {
                r["principal_id"]
                for r in self.conn.execute("SELECT principal_id FROM idempotency WHERE key = 'k-int-1';").fetchall()
            }
            self.assertEqual(pids, {self.relayer_id, f"{self.relayer_id}#relayed"})
            ev_tok = self.conn.execute(
                "SELECT via FROM events WHERE type = 'project.created' AND project_id = ?;", (prj_tok,)
            ).fetchone()
            ev_rel = self.conn.execute(
                "SELECT via FROM events WHERE type = 'project.created' AND project_id = ?;", (prj_rel,)
            ).fetchone()
            self.assertEqual(ev_tok["via"], "token")
            self.assertEqual(ev_rel["via"], "relayed")

        asyncio.run(_run())

    def test_relayed_then_token_does_not_replay_or_bypass_approval_and_rechecks_authz(self) -> None:
        async def _run() -> None:
            auth = {"type": "token", "token": self.tok_relayer}

            # 1. Follow-up intake.submit: relayed first, then non-relayed token.
            fu_args = {"project": self.prj_id, "message": "please prioritize"}
            f1 = await self._send({
                "v": 1, "cmd": "intake.submit", "auth": auth, "args": fu_args,
                "idempotency_key": "k-fu-1",
                "relayed": True,
                "user_statement": "Human says prioritize",
            })
            self.assertTrue(f1["ok"], f1)
            self.assertFalse(f1["idempotent_replay"])

            f2 = await self._send({
                "v": 1, "cmd": "intake.submit", "auth": auth, "args": fu_args,
                "idempotency_key": "k-fu-1",
            })
            self.assertTrue(f2["ok"], f2)
            self.assertFalse(f2["idempotent_replay"], "token call must not replay relayed response")
            vias = [
                r["via"]
                for r in self.conn.execute(
                    "SELECT via FROM events WHERE type = 'project.request_received' AND project_id = ? ORDER BY seq;",
                    (self.prj_id,),
                ).fetchall()
            ]
            self.assertEqual(vias, ["relayed", "token"])

            # 2. Relayed project.pause stored under #relayed must not let a subsequent lead-token
            #    call bypass the approval gate once ag-relay becomes lead.
            p1 = await self._send({
                "v": 1, "cmd": "project.pause", "auth": auth,
                "args": {"project_id": self.prj_id},
                "idempotency_key": "k-pause-1",
                "relayed": True,
                "user_statement": "Human says pause now",
            })
            self.assertTrue(p1["ok"], p1)
            self.assertEqual(p1["data"]["status"], "paused")

            # Resume project and rotate lead to ag-relay
            now = now_iso_utc()
            with transaction(self.conn):
                self.conn.execute("UPDATE projects SET status = 'active' WHERE id = ?;", (self.prj_id,))
                self.conn.execute("UPDATE lead_assignments SET ended_at = ?, ended_reason = 'swap' WHERE id = ?;",
                                  (now, self.assign_id))
                self.conn.execute(
                    "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                    "VALUES (?, ?, ?, 'usr_1', 'direct', ?);",
                    (generate_id("lead"), self.prj_id, self.relayer_id, now),
                )

            # Non-relayed call by new lead without --approval must hit APPROVAL_REQUIRED (not replay p1!)
            p2 = await self._send({
                "v": 1, "cmd": "project.pause", "auth": auth,
                "args": {"project_id": self.prj_id},
                "idempotency_key": "k-pause-1",
            })
            self.assertFalse(p2["ok"], p2)
            self.assertEqual((p2["exit_code"], p2["error"]["code"]),
                             (c.EXIT_APPROVAL_REQUIRED, c.APPROVAL_REQUIRED))

            # Relayed replay of k-pause-1 now that ag-relay leads prj_1 must fail re-authz (SELF_APPROVAL)
            p3 = await self._send({
                "v": 1, "cmd": "project.pause", "auth": auth,
                "args": {"project_id": self.prj_id},
                "idempotency_key": "k-pause-1",
                "relayed": True,
                "user_statement": "Human says pause now",
            })
            self.assertFalse(p3["ok"], p3)
            self.assertEqual((p3["exit_code"], p3["error"]["code"]), (c.EXIT_FORBIDDEN, c.SELF_APPROVAL))

            # 3. Relayed approval.approve stores user_statement and replays only on relayed channel;
            #    revoking relay capability blocks replay.
            with transaction(self.conn):
                apr = create_or_get_pending_approval(
                    self.conn, category="deploy", op="external.deploy",
                    requested_by=self.lead_id, params={"env": "prod"},
                    summary="deploy", project_id=None, testing_mode=True,
                )
            a1 = await self._send({
                "v": 1, "cmd": "approval.approve", "auth": auth,
                "args": {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                "idempotency_key": "k-apr-1",
                "relayed": True,
                "user_statement": "Human approved prod deploy",
            })
            self.assertTrue(a1["ok"], a1)
            row_apr = self.conn.execute(
                "SELECT decided_via, user_statement FROM approvals WHERE id = ?;", (apr["approval_id"],)
            ).fetchone()
            self.assertEqual((row_apr["decided_via"], row_apr["user_statement"]),
                             ("relayed", "Human approved prod deploy"))
            a2 = await self._send({
                "v": 1, "cmd": "approval.approve", "auth": auth,
                "args": {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                "idempotency_key": "k-apr-1",
                "relayed": True,
                "user_statement": "Human approved prod deploy",
            })
            self.assertTrue(a2["ok"], a2)
            self.assertTrue(a2["idempotent_replay"])

            with transaction(self.conn):
                self.conn.execute("UPDATE agents SET capabilities_json = '[\"intake\"]' WHERE id = ?;",
                                  (self.relayer_id,))
            a3 = await self._send({
                "v": 1, "cmd": "approval.approve", "auth": auth,
                "args": {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                "idempotency_key": "k-apr-1",
                "relayed": True,
                "user_statement": "Human approved prod deploy",
            })
            self.assertFalse(a3["ok"], a3)
            self.assertEqual((a3["exit_code"], a3["error"]["code"]), (c.EXIT_FORBIDDEN, c.RELAY_NOT_ALLOWED))

        asyncio.run(_run())


class TestP2ReverseI9JointExclusion(unittest.TestCase):
    """Finding 2 (P2): run_git stdin support and reverse I9 joint exclusion + fail-closed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-full2-i9-")
        self.fixture = GitFixture()
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for k in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[k]
        os.environ.update(self.fixture.git_env_vars())

        self.repo = self.fixture.make_k0("Full2I9")
        self.old_base = self.fixture.head(self.repo, "main")
        # Advance main to base_sha so an older upstream branch can fork before base_commit.
        Path(self.repo, "advance.txt").write_text("adv\n", encoding="utf-8")
        self.fixture.git(self.repo, "add", "advance.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "advance base", identity=True)
        self.base_sha = self.fixture.head(self.repo, "main")

        self.integ = "mesh/proj_1/integration"
        self.fixture.git(self.repo, "branch", self.integ, "main")

        self.conn = db.init_db(os.path.join(self.tmp.name, "mesh.db"))
        ts = "2026-01-01T00:00:00Z"
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('lead_1', 'agent', 'Lead', ?);", (ts,)
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
            "VALUES ('lead_1', 'codex', 'inbox', 1, 1, ?, ?);", (ts, ts)
        )
        self.conn.execute(
            "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
            "created_by, created_via, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            ("proj_1", "p", "g", "active", self.repo, "main", self.integ, "user", "direct", ts, ts),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.fixture.cleanup()
        self.tmp.cleanup()

    def _git_out(self, *args: str) -> str:
        return self.fixture.git(self.repo, *args).stdout.strip()

    def _insert_task(self, task_id: str, status: str, branch: str, base_commit: str,
                     merge_commit: str | None = None) -> None:
        ts = "2026-01-01T00:00:00Z"
        mj = json.dumps({"merge_commit": merge_commit, "noop": False}) if merge_commit else None
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
            "branch, base_commit, merge_json, created_by, created_at, updated_at) "
            "VALUES (?, 'proj_1', 't', 's', 'code', ?, 'edit', 60, 1, ?, ?, ?, 'user', ?, ?);",
            (task_id, status, branch, base_commit, mj, ts, ts),
        )
        self.conn.commit()

    def test_run_git_supports_stdin_input(self) -> None:
        cp_excl = run_git(
            ["log", "-n", "1", "--format=%H", "--stdin", self.base_sha],
            cwd=self.repo,
            input=f"^{self.base_sha}\n",
        )
        self.assertEqual(cp_excl.returncode, 0, cp_excl.stderr)
        self.assertEqual(cp_excl.stdout.strip(), "")

        cp_keep = run_git(
            ["log", "-n", "1", "--format=%H", "--stdin", self.base_sha],
            cwd=self.repo,
            input=f"^{self.old_base}\n",
        )
        self.assertEqual(cp_keep.returncode, 0, cp_keep.stderr)
        self.assertEqual(cp_keep.stdout.strip(), self.base_sha)

    def test_reverse_i9_over_200_excused_commits_and_pre_base_upstream(self) -> None:
        # Build 210 merged tasks on integration: task 0 starts from old_base (before base_sha),
        # tasks 1..209 start from base_sha.
        tree = self._git_out("rev-parse", f"{self.base_sha}^{{tree}}")
        old_tree = self._git_out("rev-parse", f"{self.old_base}^{{tree}}")
        cur = self.base_sha
        tips: list[str] = []
        for i in range(210):
            if i == 0:
                tip = self._git_out("commit-tree", old_tree, "-p", self.old_base, "-m", "pre_base_task")
                task_base = self.old_base
            else:
                tip = self._git_out("commit-tree", tree, "-p", self.base_sha, "-m", f"task_{i}")
                task_base = self.base_sha
            cur = self._git_out("commit-tree", tree, "-p", cur, "-p", tip, "-m", f"merge_{i}")
            tips.append(tip)
            self._insert_task(f"tsk_m{i}", "merged", f"mesh/proj_1/m{i}", task_base, merge_commit=cur)
        self._git_out("update-ref", f"refs/heads/{self.integ}", cur)

        # Cancelled task branched from base_sha that pulled 206 merged upstream tips (including
        # task 0 from pre-base history), plus its own unmerged work.
        cancel_tip = self.base_sha
        for i in range(206):
            cancel_tip = self._git_out(
                "commit-tree", tree, "-p", cancel_tip, "-p", tips[i], "-m", f"pull_up_{i}"
            )
        cancel_tip = self._git_out("commit-tree", tree, "-p", cancel_tip, "-m", "own_unmerged")
        self._git_out("branch", "mesh/proj_1/cancel_ok", cancel_tip)
        self._insert_task("tsk_cancel_ok", "cancelled", "mesh/proj_1/cancel_ok", self.base_sha)

        # Detach HEAD from main so deliver can update refs/heads/main
        self._git_out("checkout", "-q", "--detach", self.base_sha)
        self.assertEqual(invariants.check_i9_integrated_unmerged_tasks(self.conn, "proj_1"), [])
        res = handle_project_deliver(_make_ctx(self.conn, self.tmp.name, "project.deliver", {"project_id": "proj_1"}))
        self.assertEqual(res.data["new"], cur)

        # Now test >200 excused commits reaching `git log --stdin` (non-excused commit fast-forwarded
        # onto integration on top of the 210 recorded merges) and verify both detection and
        # fail-closed error handling when `git log --stdin` fails.
        ff_own = self._git_out("commit-tree", tree, "-p", cur, "-m", "ff_own_integrated")
        self._git_out("branch", "mesh/proj_1/sneaky_ff", ff_own)
        self._git_out("update-ref", f"refs/heads/{self.integ}", ff_own)
        self._insert_task("tsk_sneaky", "merge_failed", "mesh/proj_1/sneaky_ff", self.base_sha)

        real_run_git = run_git
        stdin_seen: list[str] = []

        def spy_run_git(args, cwd=None, **kw):
            if args and args[0] == "log" and "--stdin" in args and "input" in kw:
                stdin_seen.append(kw["input"])
            return real_run_git(args, cwd=cwd, **kw)

        with mock.patch("mesh.core.gitops.run_git", side_effect=spy_run_git):
            viols_ff = invariants.check_i9_integrated_unmerged_tasks(self.conn, "proj_1")
        self.assertEqual([v.details["task_id"] for v in viols_ff], ["tsk_sneaky"])
        self.assertEqual(viols_ff[0].details["reason"], "integrated_but_not_merged")
        self.assertTrue(stdin_seen)
        exc_lines = [ln for ln in stdin_seen[0].splitlines() if ln.strip()]
        self.assertGreater(len(exc_lines), 200)
        self.assertIn(f"^{self.base_sha}", exc_lines)

        def fail_stdin_log(args, cwd=None, **kw):
            if args and args[0] == "log" and "--stdin" in args:
                raise RuntimeError("git log --stdin failed")
            return real_run_git(args, cwd=cwd, **kw)

        with mock.patch("mesh.core.gitops.run_git", side_effect=fail_stdin_log):
            viols_err = invariants.check_i9_integrated_unmerged_tasks(self.conn, "proj_1")
            self.assertEqual(len(viols_err), 1)
            self.assertEqual(viols_err[0].details["reason"], "unverifiable")
            with self.assertRaises(MeshError) as cm:
                handle_project_deliver(_make_ctx(self.conn, self.tmp.name, "project.deliver", {"project_id": "proj_1"}))
            self.assertEqual(cm.exception.code, c.INVALID_TRANSITION)


if __name__ == "__main__":
    unittest.main()
