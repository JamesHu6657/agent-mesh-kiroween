"""Regression tests for full-high-gemini.md (P1-1..P1-4, P2-1, P2-2)."""

import sys
import asyncio
import json
import os
import shutil
import socket
import sqlite3

from tests.helpers import fake_peer_creds
import subprocess
import tempfile
import unittest
from unittest import mock

import mesh.adapters.vibe as vibe_mod
from mesh.adapters.vibe import _read_export, _redact_output_dir
import mesh.constants as c
from mesh.core.approvals import (
    create_or_get_pending_approval,
    verify_row_for_consume,
)
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.core.lead_actions import parse_actions
from mesh.core.repos import _collect_git_info
from mesh.daemon.handlers.approval import handle_approval_approve, handle_approval_cancel, handle_approval_reject
from mesh.daemon.handlers.audit import handle_audit_verify
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.lead_turns import finish_lead_turn
from mesh.daemon.merge import _run_merge_pipeline
from mesh.daemon.server import DaemonServer, idempotency_partition
from mesh.daemon.supervisor import _task_auto_submit
from mesh.errors import MeshError


def _setup_db(tmpdir: str) -> sqlite3.Connection:
    return init_db(os.path.join(tmpdir, "mesh.db"))


def _seed_project_and_agent(conn: sqlite3.Connection) -> tuple[str, str, str]:
    now = now_iso_utc()
    agent_id = generate_id("agt")
    conn.execute(
        "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', 'agent-1', ?);",
        (agent_id, now),
    )
    conn.execute(
        "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, default_profile, created_at, updated_at) "
        "VALUES (?, 'codex', 'launch', 1, '[\"relay\"]', 1, 'edit', ?, ?);",
        (agent_id, now, now),
    )
    prj_id = generate_id("prj")
    conn.execute(
        "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
        "VALUES (?, 'prj-1', 'goal', 'active', 'user', 'direct', ?, ?);",
        (prj_id, now, now),
    )
    assign_id = generate_id("lead")
    conn.execute(
        "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
        "VALUES (?, ?, ?, 'user', 'direct', ?);",
        (assign_id, prj_id, agent_id, now),
    )
    conn.commit()
    return prj_id, agent_id, assign_id


class TestP11ApprovalCrossCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mesh_p11_")
        self.conn = _setup_db(self.tmpdir)
        self.prj_id, self.agent_id, _ = _seed_project_and_agent(self.conn)
        self.k_user = b"0123456789abcdef0123456789abcdef"
        self.user_principal = {"id": "user", "name": "user", "kind": "user"}
        now = now_iso_utc()
        self.relayer_id = generate_id("agt")
        self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', 'relayer-1', ?);", (self.relayer_id, now))
        self.conn.execute("INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, default_profile, created_at, updated_at) VALUES (?, 'codex', 'launch', 0, '[\"relay\"]', 1, 'edit', ?, ?);", (self.relayer_id, now, now))
        self.conn.commit()
        self.relayer_principal = {
            "id": self.relayer_id,
            "name": "relayer-1",
            "kind": "agent",
            "capabilities": ["relay"],
        }

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _ctx(self, cmd: str, args: dict, principal: dict, actor_kind: str, via: str,
             k_user: bytes | None = None) -> HandlerContext:
        return HandlerContext(
            conn=self.conn,
            principal=principal,
            actor_kind=actor_kind,
            via=via,
            request_id=generate_id("req"),
            home=self.tmpdir,
            cmd=cmd,
            args=args,
            k_user=k_user,
            testing_mode=True,
        )

    def test_tampered_expires_at_and_cancelled_direct_approval_rejected(self) -> None:
        res = create_or_get_pending_approval(
            self.conn,
            category="config",
            op="config.set",
            requested_by=self.agent_id,
            params={"key": "k", "value": "v"},
            summary="set config",
            project_id=self.prj_id,
            ttl_seconds=60,
            testing_mode=True,
        )
        aid = res["approval_id"]
        op_hash = res["op_hash"]
        handle_approval_approve(
            self._ctx("approval.approve", {"approval_id": aid, "op_hash": op_hash},
                      self.user_principal, "user", "direct", k_user=self.k_user)
        )
        handle_approval_cancel(
            self._ctx("approval.cancel", {"approval_id": aid},
                      self.user_principal, "user", "direct", k_user=self.k_user)
        )
        self.conn.execute(
            "UPDATE approvals SET status = 'approved', expires_at = '2099-01-01T00:00:00Z' WHERE id = ?;",
            (aid,),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (aid,)).fetchone()
        with self.assertRaises(MeshError):
            verify_row_for_consume(self.conn, row)
        with self.assertRaises(MeshError) as cm:
            handle_audit_verify(self._ctx("audit.verify", {}, self.user_principal, "user", "direct", k_user=self.k_user))
        self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)

    def test_relayed_override_after_direct_reject_blocked(self) -> None:
        res = create_or_get_pending_approval(
            self.conn,
            category="task_mutation",
            op="task.cancel",
            requested_by=self.agent_id,
            params={"task_id": "tsk_1"},
            summary="cancel task",
            project_id=self.prj_id,
            ttl_seconds=60,
            testing_mode=True,
        )
        aid = res["approval_id"]
        op_hash = res["op_hash"]
        handle_approval_reject(
            self._ctx("approval.reject", {"approval_id": aid, "op_hash": op_hash},
                      self.user_principal, "user", "direct", k_user=self.k_user)
        )
        self.conn.execute(
            "UPDATE approvals SET status = 'pending', decided_via = NULL, decided_by = NULL, "
            "decided_at = NULL, user_mac = NULL WHERE id = ?;",
            (aid,),
        )
        self.conn.commit()
        ctx_rel = self._ctx(
            "approval.approve",
            {"approval_id": aid, "op_hash": op_hash, "user_statement": "yes"},
            self.relayer_principal,
            "agent",
            "relayed",
        )
        ctx_rel.user_statement = "yes"
        with self.assertRaises(MeshError):
            handle_approval_approve(ctx_rel)

    def test_deleted_or_reset_relayed_approval_caught_by_audit_verify(self) -> None:
        res = create_or_get_pending_approval(
            self.conn,
            category="task_mutation",
            op="task.cancel",
            requested_by=self.agent_id,
            params={"task_id": "tsk_2"},
            summary="cancel task 2",
            project_id=self.prj_id,
            ttl_seconds=60,
            testing_mode=True,
        )
        aid = res["approval_id"]
        op_hash = res["op_hash"]
        ctx_rel = self._ctx(
            "approval.approve",
            {"approval_id": aid, "op_hash": op_hash, "user_statement": "ok"},
            self.relayer_principal,
            "agent",
            "relayed",
        )
        ctx_rel.user_statement = "ok"
        handle_approval_approve(ctx_rel)
        self.conn.execute(
            "UPDATE approvals SET status = 'pending', decided_via = NULL, decided_by = NULL, "
            "decided_at = NULL WHERE id = ?;",
            (aid,),
        )
        self.conn.commit()
        with self.assertRaises(MeshError) as cm1:
            handle_audit_verify(self._ctx("audit.verify", {}, self.user_principal, "user", "direct", k_user=self.k_user))
        self.assertEqual(cm1.exception.code, c.AUDIT_MISMATCH)

        self.conn.execute("DELETE FROM approvals WHERE id = ?;", (aid,))
        self.conn.commit()
        with self.assertRaises(MeshError) as cm2:
            handle_audit_verify(self._ctx("audit.verify", {}, self.user_principal, "user", "direct", k_user=self.k_user))
        self.assertEqual(cm2.exception.code, c.AUDIT_MISMATCH)

    def test_approved_then_expired_passes_audit_verify(self) -> None:
        res = create_or_get_pending_approval(
            self.conn,
            category="task_mutation",
            op="task.cancel",
            requested_by=self.agent_id,
            params={"task_id": "tsk_3"},
            summary="cancel task 3",
            project_id=self.prj_id,
            ttl_seconds=60,
            testing_mode=True,
        )
        aid = res["approval_id"]
        op_hash = res["op_hash"]
        handle_approval_approve(
            self._ctx("approval.approve", {"approval_id": aid, "op_hash": op_hash},
                      self.user_principal, "user", "direct", k_user=self.k_user)
        )
        self.conn.execute("UPDATE approvals SET status = 'expired' WHERE id = ?;", (aid,))
        record_event(
            conn=self.conn,
            type_="approval.expired",
            actor_id="system",
            actor_kind="system",
            via="system",
            project_id=self.prj_id,
            approval_id=aid,
            payload={"approval_id": aid, "status": "expired", "expires_at": res["expires_at"]},
        )
        self.conn.commit()
        out = handle_audit_verify(
            self._ctx("audit.verify", {}, self.user_principal, "user", "direct", k_user=self.k_user)
        )
        self.assertEqual(out.data["approvals_verified"], 1)


class TestP12IdempotencyReplayAndPartition(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mesh_p12_")
        self.conn = _setup_db(self.tmpdir)
        self.prj_id, self.agent_id, self.assign1_id = _seed_project_and_agent(self.conn)
        now = now_iso_utc()
        self.agent2_id = generate_id("agt")
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', 'agent-2', ?);",
            (self.agent2_id, now),
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, default_profile, created_at, updated_at) "
            "VALUES (?, 'codex', 'launch', 1, '[]', 1, 'edit', ?, ?);",
            (self.agent2_id, now, now),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_idempotency_partition_separates_channels(self) -> None:
        p = {"id": self.agent_id}
        self.assertNotEqual(idempotency_partition(p, "token"), idempotency_partition(p, "relayed"))

    def test_readonly_cmd_not_replayed_and_mutating_replay_rechecks_authz(self) -> None:
        srv = DaemonServer(home=self.tmpdir, testing_mode=True)
        srv.db_conn = self.conn

        async def _call(req: dict, principal: dict, actor_kind: str = "agent", via: str = "token") -> dict:
            sock_a, sock_b = socket.socketpair()
            reader_s, writer_s = await asyncio.open_connection(sock=sock_b)
            reader_c, writer_c = await asyncio.open_connection(sock=sock_a)
            with (
                mock.patch("mesh.daemon.server.get_peer_credentials", return_value=fake_peer_creds()),
                mock.patch.object(srv, "authenticate_and_authorize", new=mock.AsyncMock(return_value=(principal, actor_kind, via, None))),
            ):
                writer_c.write((json.dumps(req) + "\n").encode("utf-8"))
                await writer_c.drain()
                await srv.handle_client(reader_s, writer_s)
                line = await reader_c.readline()
            writer_c.close()
            writer_s.close()
            return json.loads(line.decode("utf-8"))

        async def _run() -> None:
            now = now_iso_utc()
            p1 = {"id": self.agent_id, "name": "agent-1", "kind": "agent", "capabilities": ["relay"]}
            r1 = await _call({"cmd": "agent.list", "args": {}, "idempotency_key": "ro_k1"}, p1)
            self.assertTrue(r1["ok"])
            # Rotate lead to agent-2 and revoke relay & can_lead from agent-1
            self.conn.execute("UPDATE agents SET can_lead = 0, capabilities_json = '[]' WHERE id = ?;", (self.agent_id,))
            self.conn.execute("UPDATE lead_assignments SET ended_at = ?, ended_reason = 'rotated' WHERE id = ?;", (now, self.assign1_id))
            self.assign2_id = generate_id("lead")
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES (?, ?, ?, 'user', 'direct', ?);",
                (self.assign2_id, self.prj_id, self.agent2_id, now),
            )
            self.conn.commit()
            p1_revoked = {"id": self.agent_id, "name": "agent-1", "kind": "agent", "capabilities": []}
            r2 = await _call({"cmd": "agent.list", "args": {}, "idempotency_key": "ro_k1"}, p1_revoked)
            self.assertFalse(r2.get("idempotent_replay"), "Read-only command must not replay from idempotency cache")
            self.assertFalse(r2["ok"], "Revoked agent must be denied on agent.list")

            # Mutating command cached by lead2, then lead rotated back to agent-1, replay must fail authz
            self.conn.execute("UPDATE agents SET can_lead = 1 WHERE id = ?;", (self.agent_id,))
            self.conn.commit()
            p2 = {"id": self.agent2_id, "name": "agent-2", "kind": "agent", "capabilities": []}
            m1 = await _call(
                {"cmd": "project.note", "args": {"project_id": self.prj_id, "text": "hello"}, "idempotency_key": "mut_k1"},
                p2,
            )
            self.assertTrue(m1["ok"])
            now2 = now_iso_utc()
            self.conn.execute("UPDATE lead_assignments SET ended_at = ?, ended_reason = 'rotated' WHERE id = ?;", (now2, self.assign2_id))
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES (?, ?, ?, 'user', 'direct', ?);",
                (generate_id("lead"), self.prj_id, self.agent_id, now2),
            )
            self.conn.commit()
            m2 = await _call(
                {"cmd": "project.note", "args": {"project_id": self.prj_id, "text": "hello"}, "idempotency_key": "mut_k1"},
                p2,
            )
            self.assertFalse(m2["ok"])
            self.assertEqual(m2["error"]["code"], c.NOT_PROJECT_LEAD)

        asyncio.run(_run())


class TestP13GitTagShadowingAndDetachedHead(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mesh_p13_")
        self.repo = os.path.join(self.tmpdir, "repo")
        os.makedirs(self.repo)
        self._git(["init", "-b", "main"])
        self._git(["config", "user.email", "t@example.com"])
        self._git(["config", "user.name", "Test"])
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("base\n")
        self._git(["add", "f.txt"])
        self._git(["commit", "-m", "base"])
        self.base_commit = self._git(["rev-parse", "HEAD"]).strip()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _git(self, args: list[str], cwd: str | None = None) -> str:
        cp = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return cp.stdout

    def test_collect_git_info_rejects_detached_head_and_shadowing_tag(self) -> None:
        branch = "mesh/prj/tsk"
        self._git(["checkout", "-b", branch])
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("benign\n")
        self._git(["commit", "-am", "benign"])

        self._git(["tag", branch, "HEAD"])
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("evil\n")
        self._git(["commit", "-am", "evil"])
        evil_sha = self._git(["rev-parse", "HEAD"]).strip()
        info_tag = _collect_git_info(self.repo, branch, self.base_commit)
        self.assertNotIn("error", info_tag)
        self.assertEqual(info_tag["head"], evil_sha)

        self._git(["tag", "-d", branch])
        self._git(["checkout", "--detach", "HEAD"])
        info_det = _collect_git_info(self.repo, branch, self.base_commit)
        self.assertIn("error", info_det)
        self.assertEqual(info_det["error"]["reason"], "detached_head")

    def test_merge_pipeline_ignores_shadowing_tag(self) -> None:
        branch = "mesh/prj/tsk2"
        self._git(["checkout", "-b", branch])
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("change\n")
        self._git(["commit", "-am", "change"])
        tip = self._git(["rev-parse", "HEAD"]).strip()
        self._git(["tag", branch, self.base_commit])
        self._git(["checkout", "main"])

        plan = {
            "task_id": "tsk_1",
            "title": "tsk 1",
            "project_id": "prj_1",
            "run_id": "mrg_1",
            "run_dir": self.tmpdir,
            "branch": branch,
            "worktree": self.repo,
            "integration": self.repo,
            "expected_head": tip,
            "test_argv": None,
            "test_timeout_s": 30,
            "kill_grace_s": 5,
            "testing_mode": True,
        }
        out = _run_merge_pipeline(plan)
        self.assertEqual(out["outcome"], "merged")
        self.assertFalse(out.get("noop"))
        self.assertTrue(out.get("merge_commit"))


class TestP14VibeOutRedactionPermissionsAndSize(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mesh_p14_")
        self.vibe_out = os.path.join(self.tmpdir, "vibe-out")
        os.makedirs(self.vibe_out)

    def tearDown(self) -> None:
        if sys.platform != "win32":
            for root, dirs, _ in os.walk(self.tmpdir, topdown=True):
                os.chmod(root, 0o700)
                for d in dirs:
                    try:
                        os.chmod(os.path.join(root, d), 0o700)
                    except OSError:
                        pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @unittest.skipUnless(sys.platform != "win32", "POSIX 0o000/0o500 directory chmod redaction test")
    def test_redact_output_dir_handles_0500_and_0000_dirs(self) -> None:
        secret = "sk-ant-api03-" + "A" * 80
        export_path = os.path.join(self.vibe_out, "export.json")
        with open(export_path, "w") as f:
            json.dump({"outcome": "completed", "token": secret}, f)
        sub = os.path.join(self.vibe_out, "sub")
        os.makedirs(sub)
        sub_file = os.path.join(sub, "secret.json")
        with open(sub_file, "w") as f:
            json.dump({"secret": secret}, f)

        os.chmod(sub, 0o000)
        os.chmod(self.vibe_out, 0o500)

        _redact_output_dir(self.vibe_out)

        os.chmod(self.vibe_out, 0o700)
        os.chmod(sub, 0o700)
        for p in (export_path, sub_file):
            if os.path.exists(p):
                with open(p, "r") as f:
                    self.assertNotIn(secret, f.read())

    def test_vibe_file_size_limit(self) -> None:
        export_path = os.path.join(self.vibe_out, "export.json")
        with open(export_path, "w") as f:
            json.dump({"outcome": "completed", "pad": "x" * 2048}, f)
        with mock.patch.object(vibe_mod, "_MAX_VIBE_FILE_BYTES", 512, create=True):
            self.assertIsNone(_read_export(export_path))
            _redact_output_dir(self.vibe_out)
            self.assertFalse(os.path.exists(export_path))


class TestP21LeadActionsRecursionAndAck(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mesh_p21_")
        self.conn = _setup_db(self.tmpdir)
        self.prj_id, self.agent_id, self.assign_id = _seed_project_and_agent(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_actions_handles_recursion_error(self) -> None:
        deep = '{"a":' * 1500 + '1' + '}' * 1500
        text = (
            f"```mesh-actions\n{deep}\n```\n\n"
            '```mesh-actions\n[{"cmd": "project.note", "args": {"note": "ok"}}]\n```\n'
        )
        parsed = parse_actions(text)
        self.assertIsNone(parsed.error)
        self.assertEqual(len(parsed.actions), 1)
        self.assertEqual(parsed.actions[0].cmd, "project.note")

    def test_finish_lead_turn_exception_sets_ok_false_and_retries_messages(self) -> None:
        now = now_iso_utc()
        run_id = generate_id("run")
        run_dir = os.path.join(self.tmpdir, "run")
        os.makedirs(run_dir)
        with open(os.path.join(run_dir, "exit.json"), "w") as f:
            json.dump({"exit_code": 0, "ended_at": now}, f)
        self.conn.execute(
            "INSERT INTO runs (id, project_id, task_id, agent_id, adapter, kind, lead_assignment_id, "
            "fence, status, boot_id, run_dir, started_at) VALUES (?, ?, NULL, ?, 'codex', 'lead_turn', ?, 1, 'running', 'boot1', ?, ?);",
            (run_id, self.prj_id, self.agent_id, self.assign_id, run_dir, now),
        )
        evt = record_event(
            conn=self.conn,
            type_="task.submitted",
            actor_id=self.agent_id,
            actor_kind="agent",
            via="token",
            project_id=self.prj_id,
        )
        msg_id = generate_id("msg")
        self.conn.execute(
            "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, payload_json, "
            "status, attempts, next_attempt_at, delivering_run_id, created_at) "
            "VALUES (?, ?, ?, 'lead_wake', ?, ?, 'lead_wake', '{}', 'delivering', 1, ?, ?, ?);",
            (msg_id, evt["seq"], self.agent_id, msg_id, self.prj_id, now, run_id, now),
        )
        self.conn.commit()
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        with mock.patch("mesh.daemon.supervisor.vendor_failure_meaning", return_value=None), \
             mock.patch("mesh.daemon.lead_turns.execute_lead_actions", side_effect=RuntimeError("boom")):
            outcome = finish_lead_turn(self.conn, self.tmpdir, run, {"exit_code": 0, "ended_at": now}, testing_mode=True)
        self.assertEqual(outcome, "retry")
        msg_row = self.conn.execute("SELECT status FROM outbox WHERE id = ?;", (msg_id,)).fetchone()
        self.assertEqual(msg_row["status"], "pending")


class TestP22AutoSubmitSizeLimit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mesh_p22_")
        self.conn = _setup_db(self.tmpdir)
        self.prj_id, self.agent_id, _ = _seed_project_and_agent(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_task_auto_submit_caps_oversized_deliverable(self) -> None:
        now = now_iso_utc()
        task_id = generate_id("tsk")
        run_id = generate_id("run")
        run_dir = os.path.join(self.tmpdir, "run")
        os.makedirs(run_dir)
        big_payload = {"items": ["x" * 1024 for _ in range(250)]}
        with open(os.path.join(run_dir, "stdout.log"), "w") as f:
            f.write("```mesh-deliverable\n" + json.dumps(big_payload, indent=1) + "\n```\n")
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, attempt, created_by, created_at, updated_at) "
            "VALUES (?, ?, 't', 's', 'general', 'running', 'edit', 60, 1, 1, 'user', ?, ?);",
            (task_id, self.prj_id, now, now),
        )
        self.conn.execute(
            "INSERT INTO runs (id, project_id, task_id, agent_id, adapter, kind, fence, status, boot_id, run_dir, started_at) "
            "VALUES (?, ?, ?, ?, 'codex', 'task', 1, 'running', 'boot1', ?, ?);",
            (run_id, self.prj_id, task_id, self.agent_id, run_dir, now),
        )
        self.conn.commit()
        task = self.conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        _task_auto_submit(self.conn, task, run, testing_mode=True)
        trow = self.conn.execute("SELECT deliverable_json FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        self.assertLess(len(trow["deliverable_json"].encode("utf-8")), 200 * 1024)
        rec = json.loads(trow["deliverable_json"])
        self.assertTrue(rec["deliverable"]["truncated"])


if __name__ == "__main__":
    unittest.main()
