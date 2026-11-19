"""Regression tests for full2-r2.md Group B findings (P1-2, P1-3, P1-5, P1-9)."""

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import events, logfile
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.auth import generate_agent_token, generate_run_token
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.ids import generate_id
from mesh.daemon import merge
from mesh.daemon.handlers import adapter as adapter_handlers
from mesh.daemon.handlers import approval as approval_handlers
from mesh.daemon.handlers import run as run_handlers
from mesh.daemon.handlers import task as task_handlers
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.server import DaemonServer, dispatch_internal_request
from mesh.errors import MeshError

MESH_PROC_LOCK = "shared"


def _setup_home() -> tuple[str, sqlite3.Connection, dict]:
    home = tempfile.mkdtemp(prefix="mh-r2b-")
    for sub in ("logs", "run", "runs", "workspaces"):
        os.makedirs(os.path.join(home, sub), mode=0o700, exist_ok=True)
    conn = init_db(os.path.join(home, "mesh.db"))
    now = now_iso_utc()

    with transaction(conn):
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
            (now,),
        )
        # Lead agent (inbox, no relay capability)
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'ag-lead', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) "
            "VALUES ('agt_lead', 'none', 'inbox', 1, '[]', 2, 'edit', ?, ?);",
            (now, now),
        )
        tok_lead, l_lk, l_sh = generate_agent_token()
        cred_lead = generate_id("cred")
        conn.execute(
            "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
            "VALUES (?, 'agt_lead', 'agent_token', ?, ?, ?);",
            (cred_lead, l_lk, l_sh, now),
        )

        # Relayer agent (with intake + relay, can_lead=1)
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_relay', 'agent', 'ag-relay', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) "
            "VALUES ('agt_relay', 'none', 'inbox', 1, '[\"intake\",\"relay\"]', 2, 'edit', ?, ?);",
            (now, now),
        )
        tok_relay, r_lk, r_sh = generate_agent_token()
        cred_relay = generate_id("cred")
        conn.execute(
            "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
            "VALUES (?, 'agt_relay', 'agent_token', ?, ?, ?);",
            (cred_relay, r_lk, r_sh, now),
        )

        # Worker agent (inbox, no relay, no can_lead)
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_work', 'agent', 'ag-work', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) "
            "VALUES ('agt_work', 'none', 'inbox', 0, '[]', 2, 'edit', ?, ?);",
            (now, now),
        )
        tok_work, w_lk, w_sh = generate_agent_token()
        cred_work = generate_id("cred")
        conn.execute(
            "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at) "
            "VALUES (?, 'agt_work', 'agent_token', ?, ?, ?);",
            (cred_work, w_lk, w_sh, now),
        )

        # Active project led by ag-lead
        conn.execute(
            "INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at) "
            "VALUES ('prj_1', 'Proj1', 'goal', 'active', 'usr_1', 'direct', 1, ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
            (now,),
        )

    tokens = {
        "lead": (tok_lead, cred_lead),
        "relay": (tok_relay, cred_relay),
        "work": (tok_work, cred_work),
    }
    return home, conn, tokens


async def _send_req(srv: DaemonServer, payload: dict, *, peer_pid: int | None = None) -> dict:
    sock_a, sock_b = socket.socketpair()
    reader_s, writer_s = await asyncio.open_connection(sock=sock_b)
    reader_c, writer_c = await asyncio.open_connection(sock=sock_a)
    try:
        with mock.patch(
            "mesh.daemon.server.get_peer_credentials",
            return_value=(peer_pid if peer_pid is not None else os.getpid(), os.getuid(), os.getgid()),
        ), mock.patch("mesh.daemon.server.classify_peer_run_session", return_value=None), mock.patch.object(
            srv, "refresh_run_caches"
        ):
            writer_c.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer_c.drain()
            await srv.handle_client(reader_s, writer_s)
            line = await reader_c.readline()
    finally:
        writer_c.close()
        writer_s.close()
    return json.loads(line.decode("utf-8"))


class TestP1_2ReplayAuthz(unittest.TestCase):
    """P1-2: idempotent replay must re-check intake.submit project scope and approval fine-grained authz."""

    def setUp(self) -> None:
        self.home, self.conn, self.tokens = _setup_home()
        self.srv = DaemonServer(home=self.home, testing_mode=True)
        self.srv.db_conn = self.conn

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_intake_followup_replay_blocked_after_relayer_becomes_lead(self) -> None:
        async def _run() -> None:
            tok_relay = self.tokens["relay"][0]
            req = {
                "v": 1,
                "cmd": "intake.submit",
                "auth": {"type": "token", "token": tok_relay},
                "args": {"project": "prj_1", "message": "follow up requirement"},
                "relayed": True,
                "user_statement": "User requested priority",
                "idempotency_key": "k-p12-fu",
            }
            r1 = await _send_req(self.srv, req)
            self.assertTrue(r1["ok"], r1)
            self.assertFalse(r1["idempotent_replay"])

            # Rotate lead of prj_1 to ag-relay
            now = now_iso_utc()
            with transaction(self.conn):
                self.conn.execute("UPDATE lead_assignments SET ended_at = ? WHERE id = 'lead_1';", (now,))
                self.conn.execute(
                    "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                    "VALUES ('lead_2', 'prj_1', 'agt_relay', 'usr_1', 'direct', ?);",
                    (now,),
                )

            # Replaying relayed follow-up must now fail with SELF_APPROVAL, not replay
            r2 = await _send_req(self.srv, req)
            self.assertFalse(r2["ok"], r2)
            self.assertEqual((r2["exit_code"], r2["error"]["code"]), (c.EXIT_FORBIDDEN, c.SELF_APPROVAL))

        asyncio.run(_run())

    def test_approval_replay_rechecks_delegated_relayed_cancel_and_request_authz(self) -> None:
        async def _run() -> None:
            tok_lead = self.tokens["lead"][0]
            tok_relay = self.tokens["relay"][0]
            now = now_iso_utc()

            # Enable delegated approval mode and create project-scoped + projectless approvals
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                    "VALUES ('approval.mode', '\"delegated\"', ?, 'usr_1');",
                    (now,),
                )
                apr_proj = create_or_get_pending_approval(
                    self.conn,
                    category="deploy",
                    op="external.deploy",
                    requested_by="agt_work",
                    params={"env": "staging"},
                    summary="deploy staging",
                    project_id="prj_1",
                    testing_mode=True,
                )
                apr_noproj = create_or_get_pending_approval(
                    self.conn,
                    category="deploy",
                    op="external.deploy",
                    requested_by="agt_work",
                    params={"env": "prod"},
                    summary="deploy prod",
                    project_id=None,
                    testing_mode=True,
                )
                apr_cancel = create_or_get_pending_approval(
                    self.conn,
                    category="deploy",
                    op="external.deploy",
                    requested_by="agt_work",
                    params={"env": "qa"},
                    summary="deploy qa",
                    project_id="prj_1",
                    testing_mode=True,
                )

            # 1. Delegated approval.approve by ag-lead, then approval.mode -> passphrase / lead replaced
            del_req = {
                "v": 1,
                "cmd": "approval.approve",
                "auth": {"type": "token", "token": tok_lead},
                "args": {
                    "approval_id": apr_proj["approval_id"],
                    "op_hash": apr_proj["op_hash"],
                    "human_confirmed": "Human said yes",
                },
                "idempotency_key": "k-p12-del",
            }
            d1 = await _send_req(self.srv, del_req)
            self.assertTrue(d1["ok"], d1)

            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE settings SET value_json = '\"passphrase\"' WHERE key = 'approval.mode';"
                )
            d2 = await _send_req(self.srv, del_req)
            self.assertFalse(d2["ok"], d2)
            self.assertEqual((d2["exit_code"], d2["error"]["code"]), (c.EXIT_FORBIDDEN, c.CAPABILITY_MISSING))

            # Restore delegated mode, rotate lead away from ag-lead -> NOT_PROJECT_LEAD
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE settings SET value_json = '\"delegated\"' WHERE key = 'approval.mode';"
                )
                self.conn.execute("UPDATE lead_assignments SET ended_at = ? WHERE id = 'lead_1';", (now,))
                self.conn.execute(
                    "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                    "VALUES ('lead_rot', 'prj_1', 'agt_relay', 'usr_1', 'direct', ?);",
                    (now,),
                )
            d3 = await _send_req(self.srv, del_req)
            self.assertFalse(d3["ok"], d3)
            self.assertEqual((d3["exit_code"], d3["error"]["code"]), (c.EXIT_FORBIDDEN, c.NOT_PROJECT_LEAD))

            # Restore ag-lead so we can test cancel and request below
            with transaction(self.conn):
                self.conn.execute("UPDATE lead_assignments SET ended_at = ? WHERE id = 'lead_rot';", (now,))
                self.conn.execute(
                    "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                    "VALUES ('lead_back', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
                    (now,),
                )

            # 2. Relayed approval.approve on projectless approval, then tighten category policy -> direct_only
            rel_req = {
                "v": 1,
                "cmd": "approval.approve",
                "auth": {"type": "token", "token": tok_relay},
                "args": {"approval_id": apr_noproj["approval_id"], "op_hash": apr_noproj["op_hash"]},
                "relayed": True,
                "user_statement": "Human approved prod deploy",
                "idempotency_key": "k-p12-rel",
            }
            rel1 = await _send_req(self.srv, rel_req)
            self.assertTrue(rel1["ok"], rel1)

            with transaction(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                    "VALUES ('approval.policy.deploy', '\"direct_only\"', ?, 'usr_1');",
                    (now,),
                )
            rel2 = await _send_req(self.srv, rel_req)
            self.assertFalse(rel2["ok"], rel2)
            self.assertEqual(
                (rel2["exit_code"], rel2["error"]["code"]),
                (c.EXIT_APPROVAL_INVALID, c.CHANNEL_NOT_ALLOWED),
            )

            # 3. approval.cancel by ag-lead, then remove ag-lead -> NOT_REQUESTER
            can_req = {
                "v": 1,
                "cmd": "approval.cancel",
                "auth": {"type": "token", "token": tok_lead},
                "args": {"approval_id": apr_cancel["approval_id"]},
                "idempotency_key": "k-p12-can",
            }
            c1 = await _send_req(self.srv, can_req)
            self.assertTrue(c1["ok"], c1)

            # 4. approval.request by ag-lead (no relay), then remove ag-lead -> CAPABILITY_MISSING
            req_req = {
                "v": 1,
                "cmd": "approval.request",
                "auth": {"type": "token", "token": tok_lead},
                "args": {
                    "category": "deploy",
                    "summary": "need deploy",
                    "operation": {"action": "ship"},
                    "project_id": "prj_1",
                },
                "idempotency_key": "k-p12-req",
            }
            q1 = await _send_req(self.srv, req_req)
            self.assertTrue(q1["ok"], q1)

            with transaction(self.conn):
                self.conn.execute("UPDATE lead_assignments SET ended_at = ? WHERE id = 'lead_back';", (now,))
            c2 = await _send_req(self.srv, can_req)
            self.assertFalse(c2["ok"], c2)
            self.assertEqual((c2["exit_code"], c2["error"]["code"]), (c.EXIT_FORBIDDEN, c.NOT_REQUESTER))

            q2 = await _send_req(self.srv, req_req)
            self.assertFalse(q2["ok"], q2)
            self.assertEqual((q2["exit_code"], q2["error"]["code"]), (c.EXIT_FORBIDDEN, c.CAPABILITY_MISSING))

        asyncio.run(_run())

    def test_dispatch_internal_request_rechecks_authz_on_replay(self) -> None:
        now = now_iso_utc()
        run_dir = os.path.join(self.home, "runs", "run_lt_p12")
        os.makedirs(run_dir, exist_ok=True)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, status, boot_id, "
                "run_dir, started_at, lead_assignment_id) "
                "VALUES ('run_lt_p12', 'lead_turn', 'prj_1', 'agt_lead', 1, 'fake', 'running', "
                "'boot_1', ?, ?, 'lead_1');",
                (run_dir, now),
            )
        principal = {
            "id": "agt_lead",
            "kind": "agent",
            "name": "ag-lead",
            "capabilities": [],
            "scope": {
                "role": "lead",
                "project_id": "prj_1",
                "lead_assignment_id": "lead_1",
                "run_id": "run_lt_p12",
            },
        }
        args = {
            "category": "deploy",
            "summary": "internal deploy",
            "operation": {"action": "ship"},
            "project_id": "prj_1",
        }
        with transaction(self.conn):
            env1 = dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_int_1",
                    home=self.home,
                    cmd="approval.request",
                    args=dict(args),
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_p12:0",
            )
            self.assertTrue(env1["ok"], env1)

            # Supersede lead_1
            self.conn.execute("UPDATE lead_assignments SET ended_at = ? WHERE id = 'lead_1';", (now,))
            env2 = dispatch_internal_request(
                HandlerContext(
                    conn=self.conn,
                    principal=dict(principal),
                    actor_kind="agent",
                    via="run_token",
                    request_id="req_int_2",
                    home=self.home,
                    cmd="approval.request",
                    args=dict(args),
                    testing_mode=True,
                ),
                idempotency_key="act:run_lt_p12:0",
            )
            self.assertFalse(env2["ok"], env2)
            self.assertEqual((env2["exit_code"], env2["error"]["code"]), (c.EXIT_STALE, c.LEAD_SUPERSEDED))


class TestP1_3EnvelopeValidationAndHashIntegrity(unittest.TestCase):
    """P1-3: validate evidence_ref/if_version, honor top-level if_version, include top-level fields in req_hash."""

    def setUp(self) -> None:
        self.home, self.conn, self.tokens = _setup_home()
        self.srv = DaemonServer(home=self.home, testing_mode=True)
        self.srv.db_conn = self.conn

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_evidence_ref_validation_blocks_strict_sqlite_internal_error(self) -> None:
        async def _run() -> None:
            tok_relay = self.tokens["relay"][0]
            tok_lead = self.tokens["lead"][0]
            now = now_iso_utc()
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                    "VALUES ('approval.mode', '\"delegated\"', ?, 'usr_1');",
                    (now,),
                )
                apr = create_or_get_pending_approval(
                    self.conn,
                    category="deploy",
                    op="external.deploy",
                    requested_by="agt_work",
                    params={"env": "staging"},
                    summary="deploy staging",
                    project_id="prj_1",
                    testing_mode=True,
                )

            # Top-level non-string and overlong evidence_ref -> INVALID_ARGUMENT (never INTERNAL_ERROR)
            for bad_ev in (123, True, ["ref"], {"k": "v"}, "x" * 4001):
                resp = await _send_req(
                    self.srv,
                    {
                        "v": 1,
                        "cmd": "approval.approve",
                        "auth": {"type": "token", "token": tok_relay},
                        "args": {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                        "relayed": True,
                        "user_statement": "Human approved",
                        "evidence_ref": bad_ev,
                    },
                )
                self.assertFalse(resp["ok"], (bad_ev, resp))
                self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), bad_ev)

            # Args-level non-string and overlong evidence_ref -> INVALID_ARGUMENT
            for bad_ev in (456, False, ["ref"], "y" * 4001):
                resp = await _send_req(
                    self.srv,
                    {
                        "v": 1,
                        "cmd": "approval.approve",
                        "auth": {"type": "token", "token": tok_lead},
                        "args": {
                            "approval_id": apr["approval_id"],
                            "op_hash": apr["op_hash"],
                            "human_confirmed": "yes",
                            "evidence_ref": bad_ev,
                        },
                    },
                )
                self.assertFalse(resp["ok"], (bad_ev, resp))
                self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), bad_ev)

        asyncio.run(_run())

    def test_top_level_if_version_validated_and_enforced_by_agent_update(self) -> None:
        async def _run() -> None:
            user_auth = {"type": "user", "passphrase": "pw"}
            with mock.patch.object(
                self.srv,
                "authenticate_and_authorize",
                new=mock.AsyncMock(return_value=({"id": "usr_1", "kind": "user", "name": "user"}, "user", "direct", None)),
            ):
                # Invalid top-level if_version types -> INVALID_ARGUMENT
                for bad_iv in (True, False, 1.5, "1", [1]):
                    r_bad = await _send_req(
                        self.srv,
                        {
                            "v": 1,
                            "cmd": "agent.update",
                            "auth": user_auth,
                            "args": {"name": "ag-work", "max_concurrency": 3},
                            "if_version": bad_iv,
                        },
                    )
                    self.assertFalse(r_bad["ok"], (bad_iv, r_bad))
                    self.assertEqual(
                        (r_bad["exit_code"], r_bad["error"]["code"]),
                        (c.EXIT_USAGE, c.INVALID_ARGUMENT),
                        bad_iv,
                    )

                # Top-level mismatched if_version -> VERSION_MISMATCH (not ignored!)
                r_mis = await _send_req(
                    self.srv,
                    {
                        "v": 1,
                        "cmd": "agent.update",
                        "auth": user_auth,
                        "args": {"name": "ag-work", "max_concurrency": 3},
                        "if_version": 99,
                    },
                )
                self.assertFalse(r_mis["ok"], r_mis)
                self.assertEqual((r_mis["exit_code"], r_mis["error"]["code"]), (c.EXIT_CONFLICT, c.VERSION_MISMATCH))

                # Conflicting args.if_version and envelope if_version -> INVALID_ARGUMENT
                r_conf = await _send_req(
                    self.srv,
                    {
                        "v": 1,
                        "cmd": "agent.update",
                        "auth": user_auth,
                        "args": {"name": "ag-work", "max_concurrency": 3, "if_version": 1},
                        "if_version": 2,
                    },
                )
                self.assertFalse(r_conf["ok"], r_conf)
                self.assertEqual(
                    (r_conf["exit_code"], r_conf["error"]["code"]),
                    (c.EXIT_USAGE, c.INVALID_ARGUMENT),
                )

                # Matching top-level if_version succeeds and bumps version
                r_ok = await _send_req(
                    self.srv,
                    {
                        "v": 1,
                        "cmd": "agent.update",
                        "auth": user_auth,
                        "args": {"name": "ag-work", "max_concurrency": 3},
                        "if_version": 1,
                        "idempotency_key": "k-p13-iv",
                    },
                )
                self.assertTrue(r_ok["ok"], r_ok)
                self.assertEqual(r_ok["data"]["version"], 2)

                # Replay with altered top-level if_version -> IDEMPOTENCY_KEY_REUSED
                r_replay_diff = await _send_req(
                    self.srv,
                    {
                        "v": 1,
                        "cmd": "agent.update",
                        "auth": user_auth,
                        "args": {"name": "ag-work", "max_concurrency": 3},
                        "if_version": 2,
                        "idempotency_key": "k-p13-iv",
                    },
                )
                self.assertFalse(r_replay_diff["ok"], r_replay_diff)
                self.assertEqual(
                    (r_replay_diff["exit_code"], r_replay_diff["error"]["code"]),
                    (c.EXIT_CONFLICT, c.IDEMPOTENCY_KEY_REUSED),
                )

        asyncio.run(_run())

    def test_user_statement_and_evidence_ref_tampering_rejected_on_idempotency_replay(self) -> None:
        async def _run() -> None:
            tok_relay = self.tokens["relay"][0]
            with transaction(self.conn):
                apr = create_or_get_pending_approval(
                    self.conn,
                    category="deploy",
                    op="external.deploy",
                    requested_by="agt_work",
                    params={"env": "dev"},
                    summary="deploy dev",
                    project_id="prj_1",
                    testing_mode=True,
                )

            base_req = {
                "v": 1,
                "cmd": "approval.approve",
                "auth": {"type": "token", "token": tok_relay},
                "args": {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                "relayed": True,
                "user_statement": "Human approved dev",
                "evidence_ref": "msg-100",
                "idempotency_key": "k-p13-rel",
            }
            r1 = await _send_req(self.srv, base_req)
            self.assertTrue(r1["ok"], r1)

            # Tamper user_statement -> IDEMPOTENCY_KEY_REUSED
            r_stmt = await _send_req(self.srv, {**base_req, "user_statement": "Human approved prod!"})
            self.assertFalse(r_stmt["ok"], r_stmt)
            self.assertEqual(
                (r_stmt["exit_code"], r_stmt["error"]["code"]),
                (c.EXIT_CONFLICT, c.IDEMPOTENCY_KEY_REUSED),
            )

            # Tamper evidence_ref -> IDEMPOTENCY_KEY_REUSED
            r_ev = await _send_req(self.srv, {**base_req, "evidence_ref": "msg-999"})
            self.assertFalse(r_ev["ok"], r_ev)
            self.assertEqual(
                (r_ev["exit_code"], r_ev["error"]["code"]),
                (c.EXIT_CONFLICT, c.IDEMPOTENCY_KEY_REUSED),
            )

        asyncio.run(_run())


class TestP1_5AsyncPrecheckRevocationTOCTOU(unittest.TestCase):
    """P1-5: token revocation/expiry/run-close during async worker precheck must be caught inside TX."""

    def setUp(self) -> None:
        self.home, self.conn, self.tokens = _setup_home()
        self.srv = DaemonServer(home=self.home, testing_mode=True)
        self.srv.db_conn = self.conn

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def _revoke_cred(self, cred_id: str) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE credentials SET revoked_at = ? WHERE id = ?;",
                (now_iso_utc(), cred_id),
            )

    async def _wait_and_revoke(self, entered: threading.Event, release: threading.Event, cred_id: str) -> None:
        try:
            for _ in range(300):
                if entered.is_set():
                    self._revoke_cred(cred_id)
                    return
                await asyncio.sleep(0.01)
            self.fail("Timed out waiting for worker precheck to start")
        finally:
            release.set()

    def test_task_start_revoked_during_workspace_prep_fails_token_revoked(self) -> None:
        async def _run() -> None:
            tok_work, cred_work = self.tokens["work"]
            now = now_iso_utc()
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE projects SET repo_path = '/tmp/nonexistent-repo', base_branch = 'main', "
                    "integration_branch = 'mesh/prj_1/integration', base_commit = ? WHERE id = 'prj_1';",
                    ("a" * 40,),
                )
                self.conn.execute(
                    "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, "
                    "profile, timeout_s, max_attempts, created_by, created_at, updated_at) "
                    "VALUES ('tsk_start', 'prj_1', 'T1', 'S', 'code', 'queued', 'agt_work', 'edit', "
                    "600, 3, 'agt_lead', ?, ?);",
                    (now, now),
                )

            entered = threading.Event()
            release = threading.Event()

            def _blocking_prep(_plan):
                entered.set()
                release.wait(timeout=5.0)
                return {
                    "workspace_path": os.path.join(self.home, "workspaces", "prj_1", "tasks", "tsk_start"),
                    "branch": "mesh/prj_1/tsk_start",
                    "base_commit": "a" * 40,
                }

            with mock.patch.object(
                task_handlers,
                "plan_code_workspace",
                return_value={"task_id": "tsk_start"},
            ), mock.patch.object(task_handlers, "run_code_workspace_plan", side_effect=_blocking_prep):
                req_task = asyncio.create_task(
                    _send_req(
                        self.srv,
                        {
                            "v": 1,
                            "cmd": "task.start",
                            "auth": {"type": "token", "token": tok_work},
                            "args": {"task_id": "tsk_start"},
                        },
                    )
                )
                rev_task = asyncio.create_task(self._wait_and_revoke(entered, release, cred_work))
                resp, _ = await asyncio.gather(req_task, rev_task)

            self.assertFalse(resp["ok"], resp)
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_REVOKED))
            trow = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_start';").fetchone()
            self.assertEqual(trow["status"], "queued")
            self.assertIsNone(self.conn.execute("SELECT 1 FROM leases WHERE key = 'task:tsk_start';").fetchone())

        asyncio.run(_run())

    def test_task_submit_run_token_revoked_during_git_prep_fails_token_revoked(self) -> None:
        async def _run() -> None:
            now = now_iso_utc()
            tok_run, r_lk, r_sh = generate_run_token()
            cred_run = generate_id("cred")
            run_dir = os.path.join(self.home, "runs", "run_sub")
            os.makedirs(run_dir, exist_ok=True)
            ws_dir = os.path.join(self.home, "workspaces", "prj_1", "tasks", "tsk_sub")
            os.makedirs(ws_dir, exist_ok=True)
            scope = {"role": "executor", "project_id": "prj_1", "task_id": "tsk_sub", "run_id": "run_sub", "fence": 1}
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE projects SET repo_path = '/tmp/nonexistent-repo' WHERE id = 'prj_1';"
                )
                self.conn.execute(
                    "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, "
                    "profile, timeout_s, max_attempts, fence_seq, attempt, workspace_path, branch, "
                    "base_commit, created_by, created_at, updated_at) "
                    "VALUES ('tsk_sub', 'prj_1', 'T2', 'S', 'code', 'running', 'agt_work', 'edit', "
                    "600, 3, 1, 1, ?, 'mesh/prj_1/tsk_sub', ?, 'agt_lead', ?, ?);",
                    (ws_dir, "a" * 40, now, now),
                )
                self.conn.execute(
                    "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, "
                    "status, boot_id, run_dir, started_at) "
                    "VALUES ('run_sub', 'external', 'tsk_sub', 'prj_1', 'agt_work', 1, 1, 'none', "
                    "'running', 'boot_1', ?, ?);",
                    (run_dir, now),
                )
                self.conn.execute(
                    "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, "
                    "acquired_at, expires_at, heartbeat_at) "
                    "VALUES ('task:tsk_sub', 'run_sub', 'agent', 'agt_work', 'agt_work', 1, ?, '2099-01-01T00:00:00Z', ?);",
                    (now, now),
                )
                self.conn.execute(
                    "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at) "
                    "VALUES (?, 'agt_work', 'run_token', ?, ?, ?, ?);",
                    (cred_run, r_lk, r_sh, json.dumps(scope), now),
                )

            entered = threading.Event()
            release = threading.Event()

            def _blocking_git(*_a, **_kw):
                entered.set()
                release.wait(timeout=5.0)
                return {"branch": "mesh/prj_1/tsk_sub", "base_commit": "a" * 40, "head": "b" * 40, "ahead": 1}

            with mock.patch.object(task_handlers, "submission_git_info", side_effect=_blocking_git):
                req_task = asyncio.create_task(
                    _send_req(
                        self.srv,
                        {
                            "v": 1,
                            "cmd": "task.submit",
                            "auth": {"type": "token", "token": tok_run},
                            "args": {"task_id": "tsk_sub", "run": "run_sub", "fence": 1, "summary": "done"},
                        },
                    )
                )
                rev_task = asyncio.create_task(self._wait_and_revoke(entered, release, cred_run))
                resp, _ = await asyncio.gather(req_task, rev_task)

            self.assertFalse(resp["ok"], resp)
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_REVOKED))
            trow = self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_sub';").fetchone()
            self.assertEqual(trow["status"], "running")

        asyncio.run(_run())

    def test_adapter_probe_revoked_during_worker_probe_fails_token_revoked(self) -> None:
        async def _run() -> None:
            tok_work, cred_work = self.tokens["work"]
            entered = threading.Event()
            release = threading.Event()

            def _blocking_probe(adapter, binary_path=None):
                entered.set()
                release.wait(timeout=5.0)
                return adapter, adapter.probe(binary_path=binary_path, conn=None)

            with mock.patch.object(adapter_handlers, "_probe_one_off_db", side_effect=_blocking_probe):
                req_task = asyncio.create_task(
                    _send_req(
                        self.srv,
                        {
                            "v": 1,
                            "cmd": "adapter.probe",
                            "auth": {"type": "token", "token": tok_work},
                            "args": {"adapter_id": "fake"},
                        },
                    )
                )
                rev_task = asyncio.create_task(self._wait_and_revoke(entered, release, cred_work))
                resp, _ = await asyncio.gather(req_task, rev_task)

            self.assertFalse(resp["ok"], resp)
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_REVOKED))

        asyncio.run(_run())

    def test_task_merge_revoked_during_probe_fails_token_revoked_while_queued_lead_merge_still_allowed(self) -> None:
        async def _run() -> None:
            tok_lead, cred_lead = self.tokens["lead"]
            now = now_iso_utc()
            ws_dir = os.path.join(self.home, "workspaces", "prj_1", "tasks", "tsk_mrg")
            os.makedirs(ws_dir, exist_ok=True)
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE projects SET repo_path = '/tmp/nonexistent-repo', base_branch = 'main', "
                    "integration_branch = 'mesh/prj_1/integration', base_commit = ? WHERE id = 'prj_1';",
                    ("a" * 40,),
                )
                self.conn.execute(
                    "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, "
                    "profile, timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                    "VALUES ('tsk_mrg', 'prj_1', 'M', 'S', 'code', 'accepted', 'agt_work', 'edit', "
                    "600, 3, ?, 'mesh/prj_1/tsk_mrg', 'agt_lead', ?, ?);",
                    (ws_dir, now, now),
                )

            entered = threading.Event()
            release = threading.Event()

            def _blocking_head(_integ):
                entered.set()
                release.wait(timeout=5.0)
                return "a" * 40

            with mock.patch.object(merge, "probe_integration_head", side_effect=_blocking_head):
                req_task = asyncio.create_task(
                    _send_req(
                        self.srv,
                        {
                            "v": 1,
                            "cmd": "task.merge",
                            "auth": {"type": "token", "token": tok_lead},
                            "args": {"task_id": "tsk_mrg"},
                        },
                    )
                )
                rev_task = asyncio.create_task(self._wait_and_revoke(entered, release, cred_lead))
                resp, _ = await asyncio.gather(req_task, rev_task)

            self.assertFalse(resp["ok"], resp)
            self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_REVOKED))
            self.assertEqual(
                self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_mrg';").fetchone()["status"],
                "accepted",
            )

            # Queued lead merge (_run_lead_merge) uses an already-closed turn's revoked run_token
            # and must NOT fail credential check when lead assignment is still active and enabled.
            _, lk, sh = generate_run_token()
            cred_lt = generate_id("cred")
            run_dir = os.path.join(self.home, "runs", "run_lt_q")
            os.makedirs(run_dir, exist_ok=True)
            scope = {
                "role": "lead",
                "project_id": "prj_1",
                "lead_assignment_id": "lead_1",
                "run_id": "run_lt_q",
            }
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, status, boot_id, "
                    "run_dir, started_at, ended_at, lead_assignment_id) "
                    "VALUES ('run_lt_q', 'lead_turn', 'prj_1', 'agt_lead', 1, 'fake', 'exited', 'boot_1', "
                    "?, ?, ?, 'lead_1');",
                    (run_dir, now, now),
                )
                self.conn.execute(
                    "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, "
                    "created_at, revoked_at) VALUES (?, 'agt_lead', 'run_token', ?, ?, ?, ?, ?);",
                    (cred_lt, lk, sh, json.dumps(scope), now, now),
                )
            item = {
                "request_id": "req_qm",
                "cmd": "task.merge",
                "args": {"task_id": "tsk_mrg"},
                "action_args": {"task": "tsk_mrg"},
                "project_id": "prj_1",
                "run_id": "run_lt_q",
                "index": 0,
                "principal": {
                    "id": "agt_lead",
                    "kind": "agent",
                    "name": "ag-lead",
                    "credential_id": cred_lt,
                    "scope": scope,
                },
            }
            with mock.patch.object(merge, "probe_integration_head", return_value="a" * 40), mock.patch.object(
                merge, "run_merge_pipeline", return_value={"outcome": "merged", "noop": False, "pre_head": "a" * 40, "merge_commit": "b" * 40}
            ), mock.patch.object(merge, "_check_live_merge_close"):
                await self.srv._run_lead_merge(item)
            self.assertEqual(
                self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_mrg';").fetchone()["status"],
                "merged",
            )

        asyncio.run(_run())

    def test_workspace_prune_dry_run_and_workspace_delete_revoked_midflight(self) -> None:
        async def _run() -> None:
            tok_lead, cred_lead = self.tokens["lead"]
            now = now_iso_utc()
            ws_del = os.path.join(self.home, "workspaces", "prj_1", "tasks", "tsk_del")
            os.makedirs(ws_del, exist_ok=True)
            k_user = b"u" * 32
            with transaction(self.conn):
                apr = create_or_get_pending_approval(
                    self.conn,
                    category="delete",
                    op="workspace.delete",
                    requested_by="agt_lead",
                    params={"task_id": "tsk_del"},
                    summary="del",
                    project_id="prj_1",
                    target_id="tsk_del",
                    k_user=k_user,
                    testing_mode=True,
                )
                approval_handlers.handle_approval_approve(
                    HandlerContext(
                        conn=self.conn,
                        principal={"id": "usr_1", "kind": "user", "name": "user"},
                        actor_kind="user",
                        via="direct",
                        request_id="req_apr_del",
                        home=self.home,
                        cmd="approval.approve",
                        args={"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                        k_user=k_user,
                        testing_mode=True,
                    )
                )
                self.conn.execute(
                    "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, "
                    "profile, timeout_s, max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                    "VALUES ('tsk_del', 'prj_1', 'D', 'S', 'code', 'merged', 'agt_work', 'edit', "
                    "600, 3, ?, 'mesh/prj_1/tsk_del', 'agt_lead', ?, ?);",
                    (ws_del, now, now),
                )

            # 1. workspace.prune --dry-run revoked during execute_workspace_prune_plan
            entered_p = threading.Event()
            release_p = threading.Event()

            def _blocking_prune(_plan):
                entered_p.set()
                release_p.wait(timeout=5.0)
                return {"root": os.path.join(self.home, "workspaces"), "removed": [], "skipped": []}

            with mock.patch.object(run_handlers, "execute_workspace_prune_plan", side_effect=_blocking_prune):
                req_p = asyncio.create_task(
                    _send_req(
                        self.srv,
                        {
                            "v": 1,
                            "cmd": "workspace.prune",
                            "auth": {"type": "token", "token": tok_lead},
                            "args": {"project": "prj_1"},
                            "dry_run": True,
                        },
                    )
                )
                rev_p = asyncio.create_task(self._wait_and_revoke(entered_p, release_p, cred_lead))
                resp_p, _ = await asyncio.gather(req_p, rev_p)
            self.assertFalse(resp_p["ok"], resp_p)
            self.assertEqual((resp_p["exit_code"], resp_p["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_REVOKED))

            # 2. Un-revoke, then test workspace.delete revoked during Phase 2 disk delete -> TX2 fails TOKEN_REVOKED
            with transaction(self.conn):
                self.conn.execute("UPDATE credentials SET revoked_at = NULL WHERE id = ?;", (cred_lead,))
            entered_d = threading.Event()
            release_d = threading.Event()
            orig_delete = run_handlers._delete_workspace_dir_sync

            def _blocking_delete(*args, **kwargs):
                entered_d.set()
                release_d.wait(timeout=5.0)
                return orig_delete(*args, **kwargs)

            with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_blocking_delete):
                req_d = asyncio.create_task(
                    _send_req(
                        self.srv,
                        {
                            "v": 1,
                            "cmd": "workspace.delete",
                            "auth": {"type": "token", "token": tok_lead},
                            "args": {"task_id": "tsk_del"},
                            "approval": apr["approval_id"],
                        },
                    )
                )
                rev_d = asyncio.create_task(self._wait_and_revoke(entered_d, release_d, cred_lead))
                resp_d, _ = await asyncio.gather(req_d, rev_d)
            self.assertFalse(resp_d["ok"], resp_d)
            self.assertEqual((resp_d["exit_code"], resp_d["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.TOKEN_REVOKED))

            # 3. Direct recheck_tx_credential_and_authz: refreshes capabilities, detects expired token & inactive run
            from mesh.daemon.handlers.inbox import recheck_tx_credential_and_authz

            with transaction(self.conn):
                self.conn.execute("UPDATE credentials SET revoked_at = NULL WHERE id = ?;", (cred_lead,))
                self.conn.execute(
                    "UPDATE agents SET capabilities_json = '[\"intake\",\"relay\"]' WHERE id = 'agt_lead';"
                )
            ctx_rel = HandlerContext(
                conn=self.conn,
                principal={"id": "agt_lead", "kind": "agent", "name": "ag-lead", "credential_id": cred_lead,
                           "capabilities": ["relay"]},
                actor_kind="agent",
                via="relayed",
                request_id="req_cap_ref",
                home=self.home,
                cmd="project.list",
                args={},
                testing_mode=True,
            )
            recheck_tx_credential_and_authz(ctx_rel)
            with transaction(self.conn):
                self.conn.execute("UPDATE agents SET capabilities_json = '[]' WHERE id = 'agt_lead';")
            with self.assertRaises(MeshError) as cm_rel:
                recheck_tx_credential_and_authz(ctx_rel)
            self.assertEqual(cm_rel.exception.code, c.RELAY_NOT_ALLOWED)

            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE credentials SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?;", (cred_lead,)
                )
            ctx_exp = HandlerContext(
                conn=self.conn,
                principal={"id": "agt_lead", "kind": "agent", "name": "ag-lead", "credential_id": cred_lead,
                           "capabilities": []},
                actor_kind="agent",
                via="token",
                request_id="req_exp",
                home=self.home,
                cmd="adapter.probe",
                args={},
                testing_mode=True,
            )
            with self.assertRaises(MeshError) as cm_exp:
                recheck_tx_credential_and_authz(ctx_exp)
            self.assertEqual(cm_exp.exception.code, c.TOKEN_EXPIRED)

            # Active credential whose run_token's run exited -> TOKEN_REVOKED
            _, rt_lk, rt_sh = generate_run_token()
            cred_rt = generate_id("cred")
            rt_dir = os.path.join(self.home, "runs", "run_rt_ex")
            os.makedirs(rt_dir, exist_ok=True)
            rt_scope = {"role": "executor", "project_id": "prj_1", "task_id": "tsk_del", "run_id": "run_rt_ex", "fence": 1}
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, "
                    "status, boot_id, run_dir, started_at) "
                    "VALUES ('run_rt_ex', 'external', 'tsk_del', 'prj_1', 'agt_work', 1, 1, 'none', "
                    "'exited', 'boot_1', ?, ?);",
                    (rt_dir, now),
                )
                self.conn.execute(
                    "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at) "
                    "VALUES (?, 'agt_work', 'run_token', ?, ?, ?, ?);",
                    (cred_rt, rt_lk, rt_sh, json.dumps(rt_scope), now),
                )
            ctx_rt = HandlerContext(
                conn=self.conn,
                principal={"id": "agt_work", "kind": "agent", "name": "ag-work", "credential_id": cred_rt,
                           "scope": rt_scope},
                actor_kind="agent",
                via="run_token",
                request_id="req_rt_ex",
                home=self.home,
                cmd="task.submit",
                args={"task_id": "tsk_del"},
                testing_mode=True,
            )
            with self.assertRaises(MeshError) as cm_rt:
                recheck_tx_credential_and_authz(ctx_rt)
            self.assertEqual(cm_rt.exception.code, c.TOKEN_REVOKED)

            # SimpleNamespace ctx without .cmd works when explicit cmd is supplied, and fails closed without it
            from types import SimpleNamespace

            ns_ctx = SimpleNamespace(
                conn=self.conn,
                principal={"id": "usr_1", "kind": "user", "name": "user"},
                actor_kind="user",
                via="direct",
                relayed_by=None,
                credential_id=None,
            )
            recheck_tx_credential_and_authz(ns_ctx, cmd="task.merge")
            with self.assertRaises(AttributeError):
                recheck_tx_credential_and_authz(ns_ctx)

        asyncio.run(_run())


class TestP1_9RequestIdAndLogInjection(unittest.TestCase):
    """P1-9: request_id allowlist + redaction, logfile.escape_field escapes space/'=' while _escape_line keeps spaces."""

    def setUp(self) -> None:
        self.home, self.conn, self.tokens = _setup_home()
        self.srv = DaemonServer(home=self.home, testing_mode=True)
        self.srv.db_conn = self.conn

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_request_id_allowlist_and_secret_redaction_in_events(self) -> None:
        async def _run() -> None:
            tok_lead = self.tokens["lead"][0]
            # 1. Disallowed request_id rejected with INVALID_ARGUMENT
            for bad_rid in ("", "req with space", "req=1", "req\nrc=0", "x" * 129, 42):
                resp = await _send_req(
                    self.srv,
                    {
                        "v": 1,
                        "cmd": "project.list",
                        "auth": {"type": "token", "token": tok_lead},
                        "args": {},
                        "request_id": bad_rid,
                    },
                )
                self.assertFalse(resp["ok"], (bad_rid, resp))
                self.assertEqual((resp["exit_code"], resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), bad_rid)

            # 2. Allowlisted secret-shaped request_id is redacted before storing in events & on event.list/tail
            secret_rid = "sk-1234567890abcdef1234567890abcdef"
            with transaction(self.conn):
                ev = events.record_event(
                    conn=self.conn,
                    type_="project.noted",
                    actor_id="agt_lead",
                    actor_kind="agent",
                    via="token",
                    project_id="prj_1",
                    payload={"note": "hi"},
                    request_id=secret_rid,
                )
            self.assertNotIn("1234567890abcdef", ev["request_id"] or "")
            self.assertEqual(events.verify_event_chain(self.conn), [])

            # Legacy unredacted row in events is still redacted on event.list and event.tail
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO events (id, ts, type, project_id, actor_id, actor_kind, via, "
                    "payload_json, request_id, prev_hash, hash) "
                    "VALUES (?, ?, 'project.noted', 'prj_1', 'agt_lead', 'agent', 'token', "
                    "'{\"note\":\"legacy\"}', ?, ?, ?);",
                    (generate_id("evt"), now_iso_utc(), secret_rid, ev["hash"], "legacyhash"),
                )

            r_list = await _send_req(
                self.srv,
                {
                    "v": 1,
                    "cmd": "event.list",
                    "auth": {"type": "token", "token": tok_lead},
                    "args": {"project": "prj_1"},
                },
            )
            self.assertTrue(r_list["ok"], r_list)
            items = r_list["data"]["items"]
            self.assertEqual(len(items), 2)
            for item in items:
                self.assertNotIn("1234567890abcdef", item["request_id"] or "")

            # Stream event.tail and read lines concurrently so we don't wait the full timeout
            sock_a, sock_b = socket.socketpair()
            reader_s, writer_s = await asyncio.open_connection(sock=sock_b)
            reader_c, writer_c = await asyncio.open_connection(sock=sock_a)
            try:
                with mock.patch(
                    "mesh.daemon.server.get_peer_credentials",
                    return_value=(os.getpid(), os.getuid(), os.getgid()),
                ), mock.patch("mesh.daemon.server.classify_peer_run_session", return_value=None), mock.patch.object(
                    self.srv, "refresh_run_caches"
                ):
                    writer_c.write(
                        (
                            json.dumps(
                                {
                                    "v": 1,
                                    "cmd": "event.tail",
                                    "auth": {"type": "token", "token": tok_lead},
                                    "args": {"project": "prj_1", "since": 0, "timeout": "5s"},
                                }
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    await writer_c.drain()
                    srv_task = asyncio.create_task(self.srv.handle_client(reader_s, writer_s))
                    line1 = await asyncio.wait_for(reader_c.readline(), timeout=2.0)
                    line2 = await asyncio.wait_for(reader_c.readline(), timeout=2.0)
                    writer_c.write_eof()
                    await asyncio.wait_for(srv_task, timeout=2.0)
            finally:
                writer_c.close()
                writer_s.close()
            tail_evs = [json.loads(ln.decode("utf-8")) for ln in (line1, line2)]
            self.assertEqual(len(tail_evs), 2)
            for tev in tail_evs:
                self.assertIn("seq", tev)
                self.assertNotIn("1234567890abcdef", tev["request_id"] or "")

        asyncio.run(_run())

    def test_logfile_escape_field_blocks_kv_injection(self) -> None:
        escaped = logfile.escape_field("bad rc=0 principal=usr_1\nforged")
        self.assertNotIn(" ", escaped)
        self.assertNotIn("=", escaped)
        self.assertNotIn("\n", escaped)
        self.assertIn("\\x20", escaped)
        self.assertIn("\\x3d", escaped)
        self.assertIn("\\x0a", escaped)

        async def _run() -> None:
            tok_work = self.tokens["work"][0]
            resp = await _send_req(
                self.srv,
                {
                    "v": 1,
                    "cmd": "unknown rc=0 principal=usr_1",
                    "auth": {"type": "token", "token": tok_work},
                    "args": {},
                    "request_id": "req_log_inj",
                },
            )
            self.assertFalse(resp["ok"])
            log_path = os.path.join(self.home, "logs", "meshd.log")
            with open(log_path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 1)
            tokens = lines[0].split(" ")
            # Timestamp + req= + cmd= + principal= + rc= + dur_ms= -> exactly 6 tokens (no injected keys)
            self.assertEqual(len(tokens), 6, lines[0])
            self.assertEqual(tokens[1], "req=req_log_inj")
            self.assertEqual(tokens[2], "cmd=unknown\\x20rc\\x3d0\\x20principal\\x3dusr_1")
            self.assertEqual(tokens[3], "principal=agt_work")
            self.assertEqual(tokens[4], f"rc={c.EXIT_FORBIDDEN}")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
