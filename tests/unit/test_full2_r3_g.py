"""Regression tests for full2-r3.md Group G findings (P1-11, P1-12 approval.py, P2-9 approval.py)."""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.auth import derive_user_keys, generate_agent_token, generate_run_token
from mesh.core.clock import now_iso_utc
from mesh.core.db import connect_db, transaction
from mesh.core.events import compute_event_hash, compute_user_event_mac, record_event
from mesh.core.ids import generate_id
from mesh.core.init import init_mesh_home
from mesh.daemon.handlers.agent import (
    handle_agent_create,
    handle_agent_disable,
    handle_agent_token_issue,
    handle_agent_token_revoke,
    handle_agent_update,
)
from mesh.daemon.handlers.approval import (
    check_approval_replay_authz,
    handle_approval_approve,
    handle_approval_cancel,
    handle_approval_check,
    handle_approval_consume,
    handle_approval_list,
    handle_approval_reject,
    handle_approval_request,
    handle_approval_show,
)
from mesh.daemon.handlers.audit import handle_audit_verify
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.config import handle_config_set
from mesh.daemon.handlers.project import handle_intake_submit, handle_project_set_lead
from mesh.daemon.handlers.task import handle_task_create
from mesh.daemon.server import dispatch_internal_request
from mesh.errors import MeshError

PASS = "correct-horse-battery-staple"


def _derive_k_user(conn: sqlite3.Connection, passphrase: str = PASS) -> bytes:
    cred = conn.execute("SELECT salt FROM credentials WHERE kind = 'passphrase' LIMIT 1;").fetchone()
    assert cred is not None
    _, k_user, _, _ = derive_user_keys(passphrase, salt=cred["salt"], testing_kdf=True)
    return k_user


def _user_ctx(
    conn: sqlite3.Connection,
    home: str,
    cmd: str,
    args: dict,
    k_user: bytes,
) -> HandlerContext:
    user_row = conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;").fetchone()
    uid = user_row["id"] if user_row else "usr_1"
    return HandlerContext(
        conn=conn,
        principal={"id": uid, "name": "user", "kind": "user"},
        actor_kind="user",
        via="direct",
        request_id=generate_id("req"),
        home=home,
        cmd=cmd,
        args=args,
        k_user=k_user,
        testing_mode=True,
    )


def _agent_ctx(
    conn: sqlite3.Connection,
    home: str,
    cmd: str,
    args: dict,
    principal: dict,
    *,
    via: str = "token",
    user_statement: str | None = None,
    evidence_ref: str | None = None,
    credential_id: str | None = None,
) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal=principal,
        actor_kind="agent",
        via=via,
        request_id=generate_id("req"),
        home=home,
        cmd=cmd,
        args=args,
        testing_mode=True,
        relayed_by=principal.get("name") if via == "relayed" else None,
        user_statement=user_statement,
        evidence_ref=evidence_ref,
        credential_id=credential_id,
    )


def _drop_event_triggers(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS events_no_update;")
    conn.execute("DROP TRIGGER IF EXISTS events_no_delete;")


def _recompute_hashes_with_mac(conn: sqlite3.Connection, k_user: bytes, start_idx: int = 0) -> None:
    rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY seq ASC;").fetchall()]
    prev_hash = "genesis" if start_idx == 0 else rows[start_idx - 1]["hash"]
    for i in range(start_idx, len(rows)):
        row = rows[i]
        row["prev_hash"] = prev_hash
        new_hash = compute_event_hash(row, prev_hash)
        new_mac = row["user_mac"]
        if row["actor_kind"] == "user" and row["via"] == "direct":
            new_mac = compute_user_event_mac(k_user, new_hash)
        conn.execute(
            "UPDATE events SET prev_hash = ?, hash = ?, user_mac = ? WHERE seq = ?;",
            (prev_hash, new_hash, new_mac, row["seq"]),
        )
        prev_hash = new_hash


def _setup_initialized_home(tmp: str):
    home = os.path.join(tmp, "home")
    with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
        init_mesh_home(home, PASS)
    conn = connect_db(os.path.join(home, "mesh.db"))
    k_user = _derive_k_user(conn, PASS)

    with transaction(conn):
        lead1 = handle_agent_create(
            _user_ctx(
                conn,
                home,
                "agent.create",
                {
                    "name": "lead1",
                    "adapter": "none",
                    "delivery": "inbox",
                    "can_lead": True,
                    "capabilities": ["intake"],
                    "ack_timeout": "30m",
                },
                k_user,
            )
        ).data
        lead2 = handle_agent_create(
            _user_ctx(
                conn,
                home,
                "agent.create",
                {
                    "name": "lead2",
                    "adapter": "none",
                    "delivery": "inbox",
                    "can_lead": True,
                    "capabilities": ["intake"],
                },
                k_user,
            )
        ).data
        relayer = handle_agent_create(
            _user_ctx(
                conn,
                home,
                "agent.create",
                {
                    "name": "relayer1",
                    "adapter": "none",
                    "delivery": "inbox",
                    "can_lead": False,
                    "capabilities": ["intake", "relay"],
                },
                k_user,
            )
        ).data
        rel_tok = handle_agent_token_issue(
            _user_ctx(conn, home, "agent.token.issue", {"name": "relayer1"}, k_user)
        ).data
        prj = handle_intake_submit(
            _user_ctx(
                conn,
                home,
                "intake.submit",
                {
                    "title": "Proj",
                    "goal": "Goal",
                    "criterion": ["c1"],
                    "lead": "lead1",
                },
                k_user,
            )
        ).data
        tsk = handle_task_create(
            _agent_ctx(
                conn,
                home,
                "task.create",
                {
                    "project": prj["id"],
                    "title": "Task 1",
                    "spec": "Spec 1",
                    "to": "lead1",
                },
                {"id": lead1["id"], "name": "lead1", "kind": "agent", "capabilities": ["intake"]},
            )
        ).data
    return home, conn, k_user, {
        "lead1": lead1,
        "lead2": lead2,
        "relayer": relayer,
        "relayer_cred_id": rel_tok["credential_id"],
        "project": prj,
        "task": tsk,
    }


class TestP1_11AuditAndUserStatement(unittest.TestCase):
    def test_legitimate_home_with_ack_timeout_and_secret_ws_root_passes_audit_verify(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3g-ok-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                # Change workspaces.root to a secret-matching path (redacted in config.updated)
                sec_ws = os.path.join(tmp, "secret=abc", "ws")
                os.makedirs(sec_ws, mode=0o700)
                with transaction(conn):
                    # Cancel project first? No repo project, so config.set workspaces.root succeeds
                    handle_config_set(
                        _user_ctx(conn, home, "config.set", {"key": "workspaces.root", "value": sec_ws}, k_user)
                    )
                    handle_agent_update(
                        _user_ctx(
                            conn,
                            home,
                            "agent.update",
                            {"name": "lead1", "ack_timeout": "15m"},
                            k_user,
                        )
                    )
                res = handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertGreater(res.data["events_verified"], 0)
            finally:
                conn.close()

    def test_tampered_settings_without_config_updated_fails_audit_verify(self) -> None:
        cases = [
            ("update_mode", "UPDATE settings SET value_json = '\"delegated\"' WHERE key = 'approval.mode';"),
            ("update_policy", "UPDATE settings SET value_json = '\"direct_or_relayed\"' WHERE key = 'approval.policy.permission_escalation';"),
            ("update_self_approve", "UPDATE settings SET value_json = 'false' WHERE key = 'approval.delegated_self_approve';"),
            ("delete_setting", "DELETE FROM settings WHERE key = 'approval.policy.permission_escalation';"),
            ("insert_bogus", "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES ('bogus.key', '1', '2026-01-01T00:00:00Z', 'usr_1');"),
        ]
        for label, sql in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(prefix="mh-r3g-set-") as tmp:
                home, conn, k_user, _ = _setup_initialized_home(tmp)
                try:
                    with transaction(conn):
                        conn.execute(sql)
                    with self.assertRaises(MeshError) as cm:
                        handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                    self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
                    self.assertEqual(cm.exception.exit_code, c.EXIT_APPROVAL_INVALID)
                finally:
                    conn.close()

    def test_forged_delegated_or_relayed_decision_violating_policy_at_seq_fails(self) -> None:
        # 1. Forged delegated decision while approval.mode == 'passphrase' at seq (even if enabled later)
        with tempfile.TemporaryDirectory(prefix="mh-r3g-pol1-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                lead1 = info["lead1"]
                prj_id = info["project"]["id"]
                with transaction(conn):
                    apr = create_or_get_pending_approval(
                        conn,
                        category="deploy",
                        op="external.deploy",
                        requested_by=info["relayer"]["id"],
                        params={"env": "prod"},
                        summary="deploy prod",
                        project_id=prj_id,
                        testing_mode=True,
                    )
                    now = now_iso_utc()
                    conn.execute(
                        """UPDATE approvals SET status = 'approved', decided_by = ?, decided_via = 'relayed',
                               relayed_by = ?, user_statement = 'yes', decided_at = ?
                           WHERE id = ?;""",
                        (lead1["id"], lead1["id"], now, apr["approval_id"]),
                    )
                    record_event(
                        conn=conn,
                        type_="approval.decided",
                        actor_id=lead1["id"],
                        actor_kind="agent",
                        via="delegated",
                        relayed_by=lead1["id"],
                        project_id=prj_id,
                        approval_id=apr["approval_id"],
                        payload={
                            "decision": "approved",
                            "note": None,
                            "approval_id": apr["approval_id"],
                            "category": "deploy",
                            "op": "external.deploy",
                            "op_hash": apr["op_hash"],
                            "user_statement": "yes",
                            "evidence_ref": None,
                            "mode": "delegated",
                            "human_confirmed": "yes",
                            "self_requested": False,
                        },
                    )
                    # User later enables delegated mode, hoping to mask earlier violation
                    handle_config_set(
                        _user_ctx(conn, home, "config.set", {"key": "approval.mode", "value": "delegated"}, k_user)
                    )
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
                self.assertIn(apr["approval_id"], cm.exception.details["broken_approval_ids"])
            finally:
                conn.close()

        # 2. Forged relayed decision on direct_only category (permission_escalation)
        with tempfile.TemporaryDirectory(prefix="mh-r3g-pol2-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                relayer = info["relayer"]
                prj_id = info["project"]["id"]
                with transaction(conn):
                    apr = create_or_get_pending_approval(
                        conn,
                        category="permission_escalation",
                        op="task.create",
                        requested_by=info["lead1"]["id"],
                        params={"profile": "elevated"},
                        summary="elevate",
                        project_id=prj_id,
                        testing_mode=True,
                    )
                    now = now_iso_utc()
                    conn.execute(
                        """UPDATE approvals SET status = 'approved', decided_by = ?, decided_via = 'relayed',
                               relayed_by = ?, relayer_credential_id = ?, user_statement = 'ok', decided_at = ?
                           WHERE id = ?;""",
                        (relayer["id"], relayer["id"], info["relayer_cred_id"], now, apr["approval_id"]),
                    )
                    record_event(
                        conn=conn,
                        type_="approval.decided",
                        actor_id=relayer["id"],
                        actor_kind="agent",
                        via="relayed",
                        relayed_by="relayer1",
                        credential_id=info["relayer_cred_id"],
                        project_id=prj_id,
                        approval_id=apr["approval_id"],
                        payload={
                            "decision": "approved",
                            "note": None,
                            "approval_id": apr["approval_id"],
                            "category": "permission_escalation",
                            "op": "task.create",
                            "op_hash": apr["op_hash"],
                            "user_statement": "ok",
                            "evidence_ref": None,
                        },
                    )
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
                self.assertIn(apr["approval_id"], cm.exception.details["broken_approval_ids"])
            finally:
                conn.close()

        # 3. Forged relayed decision by requester (SELF_APPROVAL)
        with tempfile.TemporaryDirectory(prefix="mh-r3g-pol3-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                relayer = info["relayer"]
                prj_id = info["project"]["id"]
                with transaction(conn):
                    apr = create_or_get_pending_approval(
                        conn,
                        category="deploy",
                        op="external.deploy",
                        requested_by=relayer["id"],
                        params={"env": "staging"},
                        summary="deploy",
                        project_id=prj_id,
                        testing_mode=True,
                    )
                    now = now_iso_utc()
                    conn.execute(
                        """UPDATE approvals SET status = 'approved', decided_by = ?, decided_via = 'relayed',
                               relayed_by = ?, relayer_credential_id = ?, user_statement = 'ok', decided_at = ?
                           WHERE id = ?;""",
                        (relayer["id"], relayer["id"], info["relayer_cred_id"], now, apr["approval_id"]),
                    )
                    record_event(
                        conn=conn,
                        type_="approval.decided",
                        actor_id=relayer["id"],
                        actor_kind="agent",
                        via="relayed",
                        relayed_by="relayer1",
                        credential_id=info["relayer_cred_id"],
                        project_id=prj_id,
                        approval_id=apr["approval_id"],
                        payload={
                            "decision": "approved",
                            "note": None,
                            "approval_id": apr["approval_id"],
                            "category": "deploy",
                            "op": "external.deploy",
                            "op_hash": apr["op_hash"],
                            "user_statement": "ok",
                            "evidence_ref": None,
                        },
                    )
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
                self.assertIn(apr["approval_id"], cm.exception.details["broken_approval_ids"])
            finally:
                conn.close()

        # 4. Forged approval.consumed after policy tightened to direct_only
        with tempfile.TemporaryDirectory(prefix="mh-r3g-pol4-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                relayer = info["relayer"]
                lead1 = info["lead1"]
                prj_id = info["project"]["id"]
                with transaction(conn):
                    apr = create_or_get_pending_approval(
                        conn,
                        category="deploy",
                        op="external.deploy",
                        requested_by=lead1["id"],
                        params={"env": "prod"},
                        summary="deploy",
                        project_id=prj_id,
                        testing_mode=True,
                    )
                    handle_approval_approve(
                        _agent_ctx(
                            conn,
                            home,
                            "approval.approve",
                            {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                            {"id": relayer["id"], "name": "relayer1", "kind": "agent", "capabilities": ["intake", "relay"]},
                            via="relayed",
                            user_statement="James: ok",
                            credential_id=info["relayer_cred_id"],
                        )
                    )
                    handle_config_set(
                        _user_ctx(conn, home, "config.set", {"key": "approval.policy.deploy", "value": "direct_only"}, k_user)
                    )
                    now = now_iso_utc()
                    conn.execute(
                        "UPDATE approvals SET status = 'consumed', consumed_at = ?, consumed_by = ?, consumed_request_id = 'req_f' WHERE id = ?;",
                        (now, lead1["id"], apr["approval_id"]),
                    )
                    record_event(
                        conn=conn,
                        type_="approval.consumed",
                        actor_id=lead1["id"],
                        actor_kind="agent",
                        via="token",
                        project_id=prj_id,
                        approval_id=apr["approval_id"],
                        payload={
                            "approval_id": apr["approval_id"],
                            "op_hash": apr["op_hash"],
                            "category": "deploy",
                            "decided_via": "relayed",
                        },
                        request_id="req_f",
                    )
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
                self.assertIn(apr["approval_id"], cm.exception.details["broken_approval_ids"])
            finally:
                conn.close()

    def test_tampered_approval_mutable_columns_detected(self) -> None:
        tamper_updates = [
            ("relayed_by", "UPDATE approvals SET relayed_by = 'agt_evil' WHERE id = ?;"),
            ("relayer_credential_id", "UPDATE approvals SET relayer_credential_id = 'cred_evil' WHERE id = ?;"),
            ("user_statement", "UPDATE approvals SET user_statement = 'User approved prod wipe' WHERE id = ?;"),
            ("evidence_ref", "UPDATE approvals SET evidence_ref = 'forged-msg' WHERE id = ?;"),
            ("decision_note", "UPDATE approvals SET decision_note = 'tampered note' WHERE id = ?;"),
            ("summary", "UPDATE approvals SET summary = 'harmless typo' WHERE id = ?;"),
            ("expires_at", "UPDATE approvals SET expires_at = '2099-12-31T00:00:00Z' WHERE id = ?;"),
        ]
        for col, sql in tamper_updates:
            with self.subTest(col=col), tempfile.TemporaryDirectory(prefix="mh-r3g-col-") as tmp:
                home, conn, k_user, info = _setup_initialized_home(tmp)
                try:
                    relayer = info["relayer"]
                    lead1 = info["lead1"]
                    prj_id = info["project"]["id"]
                    with transaction(conn):
                        apr = create_or_get_pending_approval(
                            conn,
                            category="deploy",
                            op="external.deploy",
                            requested_by=lead1["id"],
                            params={"env": "staging"},
                            summary="deploy staging",
                            project_id=prj_id,
                            testing_mode=True,
                        )
                        handle_approval_approve(
                            _agent_ctx(
                                conn,
                                home,
                                "approval.approve",
                                {
                                    "approval_id": apr["approval_id"],
                                    "op_hash": apr["op_hash"],
                                    "note": "orig note",
                                },
                                {"id": relayer["id"], "name": "relayer1", "kind": "agent", "capabilities": ["intake", "relay"]},
                                via="relayed",
                                user_statement="James: deploy staging",
                                evidence_ref="msg-42",
                                credential_id=info["relayer_cred_id"],
                            )
                        )
                    # Untampered passes
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                    with transaction(conn):
                        conn.execute(sql, (apr["approval_id"],))
                    with self.assertRaises(MeshError) as cm:
                        handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                    self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
                    self.assertIn(apr["approval_id"], cm.exception.details["broken_approval_ids"])
                finally:
                    conn.close()

    def test_tail_truncation_with_sqlite_sequence_reset_and_surviving_entities_detected(self) -> None:
        # Test deleting tail events of each entity type (and resetting sqlite_sequence)
        with tempfile.TemporaryDirectory(prefix="mh-r3g-tail-") as tmp:
            home, conn, k_user, _ = _setup_initialized_home(tmp)
            try:
                # Truncate everything after seq 1 (system.init) AND reset sqlite_sequence to 1!
                _drop_event_triggers(conn)
                conn.execute("DELETE FROM outbox WHERE event_seq > 1;")
                conn.execute("DELETE FROM events WHERE seq > 1;")
                conn.execute("UPDATE sqlite_sequence SET seq = 1 WHERE name = 'events';")
                conn.commit()
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
                self.assertEqual(cm.exception.exit_code, c.EXIT_APPROVAL_INVALID)
            finally:
                conn.close()

        # Also test truncating just the final task.assigned event + resetting sqlite_sequence
        with tempfile.TemporaryDirectory(prefix="mh-r3g-tail2-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                max_seq = conn.execute("SELECT MAX(seq) FROM events;").fetchone()[0]
                _drop_event_triggers(conn)
                conn.execute("DELETE FROM outbox WHERE event_seq = ?;", (max_seq,))
                conn.execute("DELETE FROM events WHERE seq = ?;", (max_seq,))
                conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'events';", (max_seq - 1,))
                conn.commit()
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
            finally:
                conn.close()

        # Test direct-actor spoofing (via='direct', actor_kind='agent') and silent principal enable / token unrevoke
        with tempfile.TemporaryDirectory(prefix="mh-r3g-dis-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                with transaction(conn):
                    handle_agent_disable(_user_ctx(conn, home, "agent.disable", {"name": "relayer1"}, k_user))
                    handle_agent_token_revoke(
                        _user_ctx(conn, home, "agent.token.revoke", {"credential": info["relayer_cred_id"]}, k_user)
                    )
                handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                with transaction(conn):
                    conn.execute("UPDATE principals SET disabled_at = NULL WHERE id = ?;", (info["relayer"]["id"],))
                with self.assertRaises(MeshError) as cm_dis:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm_dis.exception.code, c.AUDIT_MISMATCH)

                with transaction(conn):
                    conn.execute("UPDATE principals SET disabled_at = '2026-01-01T00:00:00Z' WHERE id = ?;", (info["relayer"]["id"],))
                    conn.execute("UPDATE credentials SET revoked_at = NULL WHERE id = ?;", (info["relayer_cred_id"],))
                with self.assertRaises(MeshError) as cm_tok:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm_tok.exception.code, c.AUDIT_MISMATCH)
            finally:
                conn.close()

        # Test via='direct' with actor_kind='agent'
        with tempfile.TemporaryDirectory(prefix="mh-r3g-vdir-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                with transaction(conn):
                    record_event(
                        conn=conn,
                        type_="project.note",
                        actor_id=info["lead1"]["id"],
                        actor_kind="agent",
                        via="direct",
                        project_id=info["project"]["id"],
                        payload={"note": "spoofed direct"},
                    )
                with self.assertRaises(MeshError) as cm_vd:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm_vd.exception.code, c.AUDIT_MISMATCH)
            finally:
                conn.close()

        # Also test surviving run_token without task.started / lead.turn_started
        with tempfile.TemporaryDirectory(prefix="mh-r3g-rtok-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                now = now_iso_utc()
                _, r_lk, r_sh = generate_run_token()
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at) "
                        "VALUES ('cred_rogue_rt', ?, 'run_token', ?, ?, ?, ?);",
                        (
                            info["lead1"]["id"],
                            r_lk,
                            r_sh,
                            json.dumps({"role": "executor", "run_id": "run_missing", "task_id": info["task"]["id"], "project_id": info["project"]["id"]}),
                            now,
                        ),
                    )
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
            finally:
                conn.close()

    def test_pinned_ws_delete_and_direct_upgrade_still_pass_audit_verify(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3g-upg-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                lead1 = info["lead1"]
                prj_id = info["project"]["id"]
                with transaction(conn):
                    handle_config_set(
                        _user_ctx(conn, home, "config.set", {"key": "approval.mode", "value": "delegated"}, k_user)
                    )
                    apr1 = create_or_get_pending_approval(
                        conn,
                        category="deploy",
                        op="external.deploy",
                        requested_by=lead1["id"],
                        params={"env": "prod"},
                        summary="deploy prod",
                        project_id=prj_id,
                        testing_mode=True,
                    )
                    handle_approval_approve(
                        _agent_ctx(
                            conn,
                            home,
                            "approval.approve",
                            {
                                "approval_id": apr1["approval_id"],
                                "op_hash": apr1["op_hash"],
                                "human_confirmed": "Human: yes",
                            },
                            {"id": lead1["id"], "name": "lead1", "kind": "agent", "capabilities": ["intake"]},
                        )
                    )
                    # Tighten deploy to direct_only, then upgrade to direct
                    handle_config_set(
                        _user_ctx(conn, home, "config.set", {"key": "approval.policy.deploy", "value": "direct_only"}, k_user)
                    )
                    handle_approval_approve(
                        _user_ctx(
                            conn,
                            home,
                            "approval.approve",
                            {"approval_id": apr1["approval_id"], "op_hash": apr1["op_hash"]},
                            k_user,
                        )
                    )

                    # Pinned workspace.delete approved under direct_or_relayed, policy tightened, then consumed
                    apr2 = create_or_get_pending_approval(
                        conn,
                        category="delete",
                        op="workspace.delete",
                        requested_by=lead1["id"],
                        params={"task_id": info["task"]["id"]},
                        summary="delete ws",
                        project_id=prj_id,
                        target_id=info["task"]["id"],
                        task_id=info["task"]["id"],
                        testing_mode=True,
                    )
                    handle_approval_approve(
                        _agent_ctx(
                            conn,
                            home,
                            "approval.approve",
                            {"approval_id": apr2["approval_id"], "op_hash": apr2["op_hash"]},
                            {"id": info["relayer"]["id"], "name": "relayer1", "kind": "agent", "capabilities": ["intake", "relay"]},
                            via="relayed",
                            user_statement="Human: delete ws",
                            credential_id=info["relayer_cred_id"],
                        )
                    )
                    handle_config_set(
                        _user_ctx(conn, home, "config.set", {"key": "approval.policy.delete", "value": "direct_only"}, k_user)
                    )
                    now = now_iso_utc()
                    conn.execute(
                        "UPDATE approvals SET status = 'consumed', consumed_at = ?, consumed_by = ?, consumed_request_id = 'req_ws' WHERE id = ?;",
                        (now, lead1["id"], apr2["approval_id"]),
                    )
                    record_event(
                        conn=conn,
                        type_="approval.consumed",
                        actor_id=lead1["id"],
                        actor_kind="agent",
                        via="token",
                        project_id=prj_id,
                        task_id=info["task"]["id"],
                        approval_id=apr2["approval_id"],
                        payload={
                            "approval_id": apr2["approval_id"],
                            "op_hash": apr2["op_hash"],
                            "category": "delete",
                            "decided_via": "relayed",
                        },
                        request_id="req_ws",
                    )

                res = handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
                self.assertEqual(res.data["approvals_verified"], 2)
            finally:
                conn.close()

    def test_relayed_user_statement_divergence_rejected_and_unified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3g-stmt-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                relayer = info["relayer"]
                lead1 = info["lead1"]
                prj_id = info["project"]["id"]
                with transaction(conn):
                    apr = create_or_get_pending_approval(
                        conn,
                        category="deploy",
                        op="external.deploy",
                        requested_by=lead1["id"],
                        params={"env": "staging"},
                        summary="deploy staging",
                        project_id=prj_id,
                        testing_mode=True,
                    )

                rel_principal = {"id": relayer["id"], "name": "relayer1", "kind": "agent", "capabilities": ["intake", "relay"]}
                # 1. Conflicting envelope vs args user_statement -> INVALID_ARGUMENT
                with self.assertRaises(MeshError) as cm_diff:
                    handle_approval_approve(
                        _agent_ctx(
                            conn,
                            home,
                            "approval.approve",
                            {
                                "approval_id": apr["approval_id"],
                                "op_hash": apr["op_hash"],
                                "user_statement": "args says prod",
                            },
                            rel_principal,
                            via="relayed",
                            user_statement="envelope says staging",
                            credential_id=info["relayer_cred_id"],
                        )
                    )
                self.assertEqual((cm_diff.exception.code, cm_diff.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

                # 2. Non-string args.user_statement -> INVALID_ARGUMENT
                with self.assertRaises(MeshError) as cm_type:
                    handle_approval_approve(
                        _agent_ctx(
                            conn,
                            home,
                            "approval.approve",
                            {
                                "approval_id": apr["approval_id"],
                                "op_hash": apr["op_hash"],
                                "user_statement": ["not-a-str"],
                            },
                            rel_principal,
                            via="relayed",
                            user_statement="envelope ok",
                            credential_id=info["relayer_cred_id"],
                        )
                    )
                self.assertEqual((cm_type.exception.code, cm_type.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

                # 3. Single args.user_statement (when ctx.user_statement is None) writes same redacted text to row & event
                with transaction(conn):
                    handle_approval_approve(
                        _agent_ctx(
                            conn,
                            home,
                            "approval.approve",
                            {
                                "approval_id": apr["approval_id"],
                                "op_hash": apr["op_hash"],
                                "user_statement": "James: ok sk-A1b2C3d4E5f6G7h8I9j0",
                            },
                            rel_principal,
                            via="relayed",
                            user_statement=None,
                            credential_id=info["relayer_cred_id"],
                        )
                    )
                row = conn.execute("SELECT user_statement FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()
                ev = conn.execute(
                    "SELECT payload_json FROM events WHERE type = 'approval.decided' AND approval_id = ?;",
                    (apr["approval_id"],),
                ).fetchone()
                self.assertEqual(row["user_statement"], json.loads(ev["payload_json"])["user_statement"])
                self.assertNotIn("sk-A1b2C3d4", row["user_statement"])
                handle_audit_verify(_user_ctx(conn, home, "audit.verify", {}, k_user))
            finally:
                conn.close()


class TestP2_9ApprovalReplayAuthz(unittest.TestCase):
    def test_check_approval_replay_authz_refreshes_live_capabilities_after_relay_revoked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3g-p29-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                relayer = info["relayer"]
                lead1 = info["lead1"]
                prj_id = info["project"]["id"]
                with transaction(conn):
                    apr = create_or_get_pending_approval(
                        conn,
                        category="deploy",
                        op="external.deploy",
                        requested_by=lead1["id"],
                        params={"env": "dev"},
                        summary="deploy dev",
                        project_id=prj_id,
                        testing_mode=True,
                    )
                    # Also create an approval requested by relayer on a project it doesn't lead
                    req_apr = handle_approval_request(
                        _agent_ctx(
                            conn,
                            home,
                            "approval.request",
                            {
                                "category": "deploy",
                                "summary": "deploy other",
                                "operation": {"env": "qa"},
                                "project_id": prj_id,
                            },
                            {"id": relayer["id"], "name": "relayer1", "kind": "agent", "capabilities": ["intake", "relay"]},
                        )
                    ).data

                stale_principal = {
                    "id": relayer["id"],
                    "name": "relayer1",
                    "kind": "agent",
                    "capabilities": ["intake", "relay"],  # stale cached list
                }
                # Initially replay authz passes
                check_approval_replay_authz(
                    conn,
                    "approval.approve",
                    stale_principal,
                    "agent",
                    "relayed",
                    {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                    {"data": {"approval_id": apr["approval_id"]}},
                )

                # Revoke 'relay' capability in DB while caller still holds stale_principal
                with transaction(conn):
                    handle_agent_update(
                        _user_ctx(conn, home, "agent.update", {"name": "relayer1", "capabilities": ["intake"]}, k_user)
                    )

                with self.assertRaises(MeshError) as cm_app:
                    check_approval_replay_authz(
                        conn,
                        "approval.approve",
                        stale_principal,
                        "agent",
                        "relayed",
                        {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                        {"data": {"approval_id": apr["approval_id"]}},
                    )
                self.assertEqual((cm_app.exception.code, cm_app.exception.exit_code), (c.RELAY_NOT_ALLOWED, c.EXIT_FORBIDDEN))

                with self.assertRaises(MeshError) as cm_req:
                    check_approval_replay_authz(
                        conn,
                        "approval.request",
                        stale_principal,
                        "agent",
                        "token",
                        {"category": "deploy", "summary": "deploy other", "operation": {"env": "qa"}, "project_id": prj_id},
                        {"data": {"approval_id": req_apr["approval_id"]}},
                    )
                self.assertEqual((cm_req.exception.code, cm_req.exception.exit_code), (c.CAPABILITY_MISSING, c.EXIT_FORBIDDEN))

                # Also verify dispatch_internal_request replay with stale principal fails after capability revocation
                with transaction(conn):
                    # Give lead1 relay temporarily, record approval.request via dispatch_internal_request, then revoke relay
                    handle_agent_update(
                        _user_ctx(conn, home, "agent.update", {"name": "relayer1", "capabilities": ["intake", "relay"]}, k_user)
                    )
                    env1 = dispatch_internal_request(
                        _agent_ctx(
                            conn,
                            home,
                            "approval.request",
                            {"category": "deploy", "summary": "s", "operation": {"x": 1}, "project_id": prj_id},
                            dict(stale_principal),
                        ),
                        idempotency_key="k-r3g-rel",
                    )
                    self.assertTrue(env1["ok"], env1)
                    handle_agent_update(
                        _user_ctx(conn, home, "agent.update", {"name": "relayer1", "capabilities": ["intake"]}, k_user)
                    )
                    env2 = dispatch_internal_request(
                        _agent_ctx(
                            conn,
                            home,
                            "approval.request",
                            {"category": "deploy", "summary": "s", "operation": {"x": 1}, "project_id": prj_id},
                            dict(stale_principal),
                        ),
                        idempotency_key="k-r3g-rel",
                    )
                    self.assertFalse(env2["ok"], env2)
                    self.assertEqual((env2["exit_code"], env2["error"]["code"]), (c.EXIT_FORBIDDEN, c.CAPABILITY_MISSING))
            finally:
                conn.close()


class TestP1_12ApprovalArgValidation(unittest.TestCase):
    def test_approval_handlers_reject_non_string_args_with_invalid_argument(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3g-p12-") as tmp:
            home, conn, k_user, info = _setup_initialized_home(tmp)
            try:
                lead1 = info["lead1"]
                prj_id = info["project"]["id"]
                with transaction(conn):
                    apr = create_or_get_pending_approval(
                        conn,
                        category="deploy",
                        op="external.deploy",
                        requested_by=lead1["id"],
                        params={"env": "dev"},
                        summary="deploy dev",
                        project_id=prj_id,
                        testing_mode=True,
                    )
                aid = apr["approval_id"]
                ohash = apr["op_hash"]
                bad_vals = (["x"], {"k": "v"}, 123, True, 1.5)

                for bad in bad_vals:
                    # approve / reject: approval_id, op_hash, note, user_statement
                    for fn, cmd in ((handle_approval_approve, "approval.approve"), (handle_approval_reject, "approval.reject")):
                        for bad_args in (
                            {"approval_id": bad, "op_hash": ohash},
                            {"approval_id": aid, "op_hash": bad},
                            {"approval_id": aid, "op_hash": ohash, "note": bad},
                            {"approval_id": aid, "op_hash": ohash, "user_statement": bad},
                        ):
                            with self.subTest(cmd=cmd, bad_args=bad_args):
                                with self.assertRaises(MeshError) as cm:
                                    fn(_user_ctx(conn, home, cmd, bad_args, k_user))
                                self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

                    # cancel: approval_id, reason
                    for bad_args in ({"approval_id": bad}, {"approval_id": aid, "reason": bad}):
                        with self.subTest(cmd="approval.cancel", bad_args=bad_args):
                            with self.assertRaises(MeshError) as cm:
                                handle_approval_cancel(_user_ctx(conn, home, "approval.cancel", bad_args, k_user))
                            self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

                    # check / consume: approval_id, op_hash
                    for fn, cmd in ((handle_approval_check, "approval.check"), (handle_approval_consume, "approval.consume")):
                        for bad_args in ({"approval_id": bad, "op_hash": ohash}, {"approval_id": aid, "op_hash": bad}):
                            with self.subTest(cmd=cmd, bad_args=bad_args):
                                with self.assertRaises(MeshError) as cm:
                                    fn(_user_ctx(conn, home, cmd, bad_args, k_user))
                                self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

                    # show: approval_id
                    with self.subTest(cmd="approval.show", bad=bad):
                        with self.assertRaises(MeshError) as cm:
                            handle_approval_show(_user_ctx(conn, home, "approval.show", {"approval_id": bad}, k_user))
                        self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

                    # list: status, project
                    for bad_args in ({"status": bad}, {"project": bad}):
                        with self.subTest(cmd="approval.list", bad_args=bad_args):
                            with self.assertRaises(MeshError) as cm:
                                handle_approval_list(_user_ctx(conn, home, "approval.list", bad_args, k_user))
                            self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

                    # request: project_id, project, task_id, task
                    for bad_args in (
                        {"category": "deploy", "summary": "s", "operation": {"a": 1}, "project_id": bad},
                        {"category": "deploy", "summary": "s", "operation": {"a": 1}, "project": bad},
                        {"category": "deploy", "summary": "s", "operation": {"a": 1}, "task_id": bad},
                        {"category": "deploy", "summary": "s", "operation": {"a": 1}, "task": bad},
                    ):
                        with self.subTest(cmd="approval.request", bad_args=bad_args):
                            with self.assertRaises(MeshError) as cm:
                                handle_approval_request(_user_ctx(conn, home, "approval.request", bad_args, k_user))
                            self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
