"""Unit tests for standing grants scoping, MAC signing, revocation, and no profile broadening."""

import os
import tempfile
import unittest

import mesh.constants as c
from mesh.adapters.base import ProfileUnsupported
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.grants import (
    build_grant_scope_key,
    compute_user_grant_mac,
    compute_user_grant_revoke_mac,
    create_or_get_standing_grant,
    find_matching_standing_grant,
    revoke_standing_grant,
)
from mesh.errors import MeshError


class TestStandingGrantsUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-grants-")
        self.db_path = os.path.join(self.tmp.name, "mesh.db")
        self.conn = init_db(self.db_path)
        self.k_user = b"test-user-mac-key-32-bytes-12345"
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (now,))
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'w1', ?);", (now,))
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_2', 'agent', 'w2', ?);", (now,))
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_1', 'fake', 'launch', 0, 1, ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_2', 'fake', 'launch', 0, 1, ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P1', 'G1', 'active', 'usr_1', 'direct', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_2', 'P2', 'G2', 'active', 'usr_1', 'direct', ?, ?);",
                (now, now),
            )

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_scope_key_validation_and_narrow_matching(self) -> None:
        self.assertEqual(
            build_grant_scope_key("permission_escalation", "prj_1", scope_agent_id="agt_1", scope_profile="elevated"),
            "permission_escalation:prj_1:agt_1:elevated",
        )
        # config is removed from standing grants (config writes remain direct-user-only)
        with self.assertRaises(MeshError) as cfg_ctx:
            build_grant_scope_key("config", "prj_1", scope_config_key="lead.profile")
        self.assertEqual(cfg_ctx.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(
            build_grant_scope_key("retry_over_limit", "prj_1"),
            "retry_over_limit:prj_1",
        )
        with self.assertRaises(MeshError) as ctx:
            build_grant_scope_key("plan_dispatch", "prj_1")
        self.assertEqual(ctx.exception.code, c.INVALID_ARGUMENT)

        # Create exact permission_escalation grant for (prj_1, agt_1, elevated)
        with transaction(self.conn):
            grant, created = create_or_get_standing_grant(
                self.conn,
                project_id="prj_1",
                category="permission_escalation",
                scope_agent_id="agt_1",
                scope_profile="elevated",
                actor_id="usr_1",
                k_user=self.k_user,
            )
            self.assertTrue(created)
            # Duplicate active scope returns existing without creating a second row
            grant2, created2 = create_or_get_standing_grant(
                self.conn,
                project_id="prj_1",
                category="permission_escalation",
                scope_agent_id="agt_1",
                scope_profile="elevated",
                actor_id="usr_1",
                k_user=self.k_user,
            )
            self.assertFalse(created2)
            self.assertEqual(grant2["id"], grant["id"])

        # Exact match succeeds
        self.assertIsNotNone(find_matching_standing_grant(
            self.conn, project_id="prj_1", category="permission_escalation", agent_id="agt_1", profile="elevated"
        ))
        # Broader profile, narrower profile, different agent, different project ALL miss!
        self.assertIsNone(find_matching_standing_grant(
            self.conn, project_id="prj_1", category="permission_escalation", agent_id="agt_1", profile="unrestricted"
        ))
        self.assertIsNone(find_matching_standing_grant(
            self.conn, project_id="prj_1", category="permission_escalation", agent_id="agt_1", profile="dev"
        ))
        self.assertIsNone(find_matching_standing_grant(
            self.conn, project_id="prj_1", category="permission_escalation", agent_id="agt_2", profile="elevated"
        ))
        self.assertIsNone(find_matching_standing_grant(
            self.conn, project_id="prj_2", category="permission_escalation", agent_id="agt_1", profile="elevated"
        ))

        # Verify MAC helpers match stored MACs
        self.assertEqual(
            grant["user_mac"],
            compute_user_grant_mac(
                self.k_user, grant["id"], "prj_1", "permission_escalation", grant["scope_key"], None, grant["created_at"]
            ),
        )

        # Revoke -> immediately stops matching
        with transaction(self.conn):
            rev, newly = revoke_standing_grant(
                self.conn, grant_id=grant["id"], actor_id="usr_1", k_user=self.k_user, reason="revoke test"
            )
            self.assertTrue(newly)
            self.assertFalse(rev["active"])
        self.assertIsNone(find_matching_standing_grant(
            self.conn, project_id="prj_1", category="permission_escalation", agent_id="agt_1", profile="elevated"
        ))
        row = self.conn.execute("SELECT revoked_at, revoked_user_mac FROM standing_grants WHERE id = ?;", (grant["id"],)).fetchone()
        self.assertEqual(row["revoked_user_mac"], compute_user_grant_revoke_mac(self.k_user, grant["id"], row["revoked_at"]))

    def test_no_profile_broadening_rejects_unsupported_adapter_profile(self) -> None:
        import json
        from mesh.core.grants import extract_grant_scopes_from_approval

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_cl', 'agent', 'claude-w', ?);", (now,))
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_cl', 'claude', 'launch', 0, 1, ?, ?);",
                (now, now),
            )
        fake_apr = {
            "id": "apr_test1",
            "project_id": "prj_1",
            "task_id": None,
            "category": "permission_escalation",
            "op_type": "task.assign",
            "op_json": json.dumps({"params": {"to": "claude-w", "profile": "dev"}}),
        }
        with self.assertRaises(ProfileUnsupported):
            extract_grant_scopes_from_approval(self.conn, fake_apr, testing_mode=True)

    def test_gates_injection_rejected_and_compound_preview_enumerates_all_scopes(self) -> None:
        import json
        from mesh.core.grants import extract_grant_scopes_from_approval, preview_grant_scope_for_card

        # 1. Injected gates on permission_escalation -> rejected; preview is None
        bad_perm = {
            "id": "apr_inj1",
            "project_id": "prj_1",
            "task_id": None,
            "category": "permission_escalation",
            "op_type": "external.permission_escalation",
            "op_json": json.dumps({"params": {"to": "w1", "profile": "elevated", "gates": ["config", "retry_over_limit"]}}),
        }
        with self.assertRaises(MeshError) as ctx1:
            extract_grant_scopes_from_approval(self.conn, bad_perm, testing_mode=True)
        self.assertEqual(ctx1.exception.code, c.INVALID_ARGUMENT)
        self.assertIsNone(preview_grant_scope_for_card(self.conn, bad_perm, testing_mode=True))

        # 2. Injected gates on external.retry_over_limit -> rejected; preview is None
        bad_ext_retry = {
            "id": "apr_inj2",
            "project_id": "prj_1",
            "task_id": None,
            "category": "retry_over_limit",
            "op_type": "external.retry_over_limit",
            "op_json": json.dumps({"params": {"to": "w1", "profile": "elevated", "gates": ["permission_escalation"]}}),
        }
        with self.assertRaises(MeshError) as ctx2:
            extract_grant_scopes_from_approval(self.conn, bad_ext_retry, testing_mode=True)
        self.assertEqual(ctx2.exception.code, c.INVALID_ARGUMENT)
        self.assertIsNone(preview_grant_scope_for_card(self.conn, bad_ext_retry, testing_mode=True))

        # 3. Unknown/disallowed gate in task.retry gates -> rejected
        bad_task_gate = {
            "id": "apr_inj3",
            "project_id": "prj_1",
            "task_id": None,
            "category": "retry_over_limit",
            "op_type": "task.retry",
            "op_json": json.dumps({"params": {"to": "w1", "profile": "elevated", "gates": ["retry_over_limit", "config"]}}),
        }
        with self.assertRaises(MeshError) as ctx3:
            extract_grant_scopes_from_approval(self.conn, bad_task_gate, testing_mode=True)
        self.assertEqual(ctx3.exception.code, c.INVALID_ARGUMENT)
        self.assertIsNone(preview_grant_scope_for_card(self.conn, bad_task_gate, testing_mode=True))

        # 4. Non-list gates -> rejected
        bad_type_gate = {
            "id": "apr_inj4",
            "project_id": "prj_1",
            "task_id": None,
            "category": "retry_over_limit",
            "op_type": "task.retry",
            "op_json": json.dumps({"params": {"gates": "permission_escalation"}}),
        }
        with self.assertRaises(MeshError) as ctx4:
            extract_grant_scopes_from_approval(self.conn, bad_type_gate, testing_mode=True)
        self.assertEqual(ctx4.exception.code, c.INVALID_ARGUMENT)
        self.assertIsNone(preview_grant_scope_for_card(self.conn, bad_type_gate, testing_mode=True))

        # 5. Legitimate compound task.retry (retry_over_limit + permission_escalation) -> preview enumerates ALL scopes
        good_compound = {
            "id": "apr_cmp1",
            "project_id": "prj_1",
            "task_id": None,
            "category": "retry_over_limit",
            "op_type": "task.retry",
            "op_json": json.dumps({
                "params": {
                    "to": "w1",
                    "profile": "elevated",
                    "gates": ["permission_escalation", "retry_over_limit"],
                }
            }),
        }
        scopes = extract_grant_scopes_from_approval(self.conn, good_compound, testing_mode=True)
        self.assertEqual(len(scopes), 2)
        preview = preview_grant_scope_for_card(self.conn, good_compound, testing_mode=True)
        self.assertIsNotNone(preview)
        self.assertEqual(
            {s["scope_key"] for s in preview["scopes"]},
            {s["scope_key"] for s in scopes},
        )
        self.assertEqual(preview["also_grants_escalation"]["agent_id"], "agt_1")
        self.assertEqual(preview["also_grants_escalation"]["profile"], "elevated")

    def test_deterministic_agent_resolution_id_beats_name(self) -> None:
        import json
        from mesh.core.grants import (
            extract_grant_scopes_from_approval,
            preview_grant_scope_for_card,
            resolve_agent_row_for_grant,
        )

        now = now_iso_utc()
        # Agent B's principal name equals Agent A's id ('agt_1')!
        with transaction(self.conn):
            self.conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_0_collide', 'agent', 'agt_1', ?);", (now,))
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_0_collide', 'fake', 'launch', 0, 1, ?, ?);",
                (now, now),
            )
        # Even though 'agt_0_collide' sorts lexicographically before 'agt_1', exact id='agt_1' MUST win!
        resolved = resolve_agent_row_for_grant(self.conn, "agt_1")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["id"], "agt_1")
        self.assertEqual(resolved["name"], "w1")

        apr = {
            "id": "apr_det1",
            "project_id": "prj_1",
            "task_id": None,
            "category": "permission_escalation",
            "op_type": "task.assign",
            "op_json": json.dumps({"params": {"to": "agt_1", "profile": "elevated"}}),
        }
        scopes = extract_grant_scopes_from_approval(self.conn, apr, testing_mode=True)
        preview = preview_grant_scope_for_card(self.conn, apr, testing_mode=True)
        self.assertEqual(scopes[0]["scope_agent_id"], "agt_1")
        self.assertEqual(scopes[0]["scope_agent_name"], "w1")
        self.assertEqual(preview["agent_id"], "agt_1")
        self.assertEqual(preview["agent_name"], "w1")
        self.assertEqual(preview["scopes"][0]["scope_key"], scopes[0]["scope_key"])

    def test_find_matching_standing_grant_rejects_removed_config_key_param(self) -> None:
        with self.assertRaises(TypeError):
            find_matching_standing_grant(  # type: ignore[call-arg]
                self.conn,
                project_id="prj_1",
                category="config",
                config_key="lead.profile",
            )

    def test_escalation_grant_matches_after_agent_rename_and_rejects_recycled_name(self) -> None:
        import json
        from mesh.core.grants import (
            extract_grant_scopes_from_approval,
            format_grant,
            resolve_agent_row_for_grant,
        )

        # Create escalation grant from an approval referencing agent by name ('w1')
        apr = {
            "id": "apr_ren1",
            "project_id": "prj_1",
            "task_id": None,
            "category": "permission_escalation",
            "op_type": "task.assign",
            "op_json": json.dumps({"params": {"to": "w1", "profile": "elevated"}}),
        }
        scopes = extract_grant_scopes_from_approval(self.conn, apr, testing_mode=True)
        self.assertEqual(scopes[0]["scope_agent_id"], "agt_1")
        with transaction(self.conn):
            grant, created = create_or_get_standing_grant(
                self.conn,
                project_id="prj_1",
                category="permission_escalation",
                scope_agent_id=scopes[0]["scope_agent_id"],
                scope_profile=scopes[0]["scope_profile"],
                actor_id="usr_1",
                k_user=self.k_user,
            )
            self.assertTrue(created)

            # Rename agt_1 from 'w1' -> 'w1-renamed' and create a new agent recycling the old name 'w1'
            now = now_iso_utc()
            self.conn.execute("UPDATE principals SET name = 'w1-renamed' WHERE id = 'agt_1';")
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_new', 'agent', 'w1', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_new', 'fake', 'launch', 0, 1, ?, ?);",
                (now, now),
            )

        # Renamed original agent still resolves to agt_1 and matches the standing grant
        renamed_row = resolve_agent_row_for_grant(self.conn, "w1-renamed")
        self.assertIsNotNone(renamed_row)
        self.assertEqual(renamed_row["id"], "agt_1")
        matched = find_matching_standing_grant(
            self.conn,
            project_id="prj_1",
            category="permission_escalation",
            agent_id=renamed_row["id"],
            profile="elevated",
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], grant["id"])
        self.assertEqual(matched["scope_agent_name"], "w1-renamed")

        # Recycled name 'w1' now belongs to agt_new and MUST NOT match agt_1's grant
        recycled_row = resolve_agent_row_for_grant(self.conn, "w1")
        self.assertIsNotNone(recycled_row)
        self.assertEqual(recycled_row["id"], "agt_new")
        self.assertIsNone(
            find_matching_standing_grant(
                self.conn,
                project_id="prj_1",
                category="permission_escalation",
                agent_id=recycled_row["id"],
                profile="elevated",
            )
        )
        raw_row = self.conn.execute("SELECT * FROM standing_grants WHERE id = ?;", (grant["id"],)).fetchone()
        self.assertEqual(format_grant(self.conn, raw_row)["scope_agent_name"], "w1-renamed")

    def test_always_allow_rejected_on_cancelled_project(self) -> None:
        from mesh.core.approvals import create_or_get_pending_approval
        from mesh.daemon.handlers.approval import handle_approval_decide
        from mesh.daemon.handlers.base import HandlerContext

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_cancelled', 'Cancelled', 'G', 'cancelled', 'usr_1', 'direct', ?, ?);",
                (now, now),
            )
            step_apr = create_or_get_pending_approval(
                self.conn,
                category="step_dispatch",
                op="task.assign",
                requested_by="agt_2",
                params={"to": "w1", "profile": "edit"},
                summary="Step on cancelled project",
                project_id="prj_cancelled",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            esc_apr = create_or_get_pending_approval(
                self.conn,
                category="permission_escalation",
                op="task.assign",
                requested_by="agt_2",
                params={"to": "w1", "profile": "elevated"},
                summary="Escalation on cancelled project",
                project_id="prj_cancelled",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )

        for apr, extra in (
            (step_apr, {"confirm_auto_mode": True}),
            (step_apr, {"confirm_auto_mode": False}),
            (esc_apr, {}),
        ):
            ctx = HandlerContext(
                conn=self.conn,
                principal={"id": "usr_1", "kind": "user", "name": "user"},
                actor_kind="user",
                via="direct",
                request_id="req_cancelled",
                home=self.tmp.name,
                cmd="approval.decide",
                args={"approval_id": apr["approval_id"], "action": "always_allow", "op_hash": apr["op_hash"], **extra},
                k_user=self.k_user,
                testing_mode=True,
            )
            with self.assertRaises(MeshError) as err_ctx:
                handle_approval_decide(ctx)
            self.assertEqual(err_ctx.exception.code, c.PROJECT_NOT_ACTIVE)
            self.assertEqual(err_ctx.exception.exit_code, c.EXIT_CONFLICT)

        # Mode stays 'plan' and zero grants were minted
        mode = self.conn.execute("SELECT permission_mode FROM projects WHERE id = 'prj_cancelled';").fetchone()["permission_mode"]
        self.assertEqual(mode, "plan")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM standing_grants WHERE project_id = 'prj_cancelled';").fetchone()[0],
            0,
        )

    def test_compute_approval_card_actions_inactive_expired_dead_task_and_already_auto(self) -> None:
        from mesh.core.approvals import create_or_get_pending_approval
        from mesh.core.grants import compute_approval_card_actions

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at) "
                "VALUES ('tsk_live', 'prj_1', 'Live', 'S', 'code', 'created', 'edit', 60, 3, 'agt_2', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at) "
                "VALUES ('tsk_cancelled', 'prj_1', 'Dead', 'S', 'code', 'cancelled', 'edit', 60, 3, 'agt_2', ?, ?);",
                (now, now),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at) "
                "VALUES ('tsk_failed', 'prj_1', 'Failed', 'S', 'code', 'failed', 'edit', 60, 3, 'agt_2', ?, ?);",
                (now, now),
            )
            live_apr = create_or_get_pending_approval(
                self.conn,
                category="step_dispatch",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_live", "to": "w1", "profile": "edit"},
                summary="Live step",
                project_id="prj_1",
                task_id="tsk_live",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            dead_apr = create_or_get_pending_approval(
                self.conn,
                category="step_dispatch",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_cancelled", "to": "w1", "profile": "edit"},
                summary="Dead step",
                project_id="prj_1",
                task_id="tsk_cancelled",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            retry_apr = create_or_get_pending_approval(
                self.conn,
                category="retry_over_limit",
                op="task.retry",
                requested_by="agt_2",
                params={"task_id": "tsk_failed"},
                summary="Retry failed task",
                project_id="prj_1",
                task_id="tsk_failed",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )

        live_row = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (live_apr["approval_id"],)).fetchone()
        dead_row = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (dead_apr["approval_id"],)).fetchone()
        retry_row = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (retry_apr["approval_id"],)).fetchone()

        # 1. Active project + live task + unexpired -> full actions
        ui_live = compute_approval_card_actions(self.conn, live_row, now=now, testing_mode=True)
        self.assertEqual(ui_live["available_actions"], ["allow", "always_allow", "reject"])
        self.assertTrue(ui_live["decidable"])
        self.assertIsNone(ui_live["undecidable_reason"])
        self.assertFalse(ui_live["inactive_project"])
        self.assertFalse(ui_live["expired"])
        self.assertEqual(ui_live["task_status"], "created")
        self.assertIsNotNone(ui_live["always_allow_effect"])

        # 2. Failed task is NOT terminal -> retry_over_limit stays decidable
        ui_retry = compute_approval_card_actions(self.conn, retry_row, now=now, testing_mode=True)
        self.assertEqual(ui_retry["available_actions"], ["allow", "always_allow", "reject"])
        self.assertTrue(ui_retry["decidable"])
        self.assertFalse(ui_retry["task_terminal"])
        self.assertEqual(ui_retry["task_status"], "failed")

        # 3. Cancelled task IS terminal -> pending card is dismiss-only (reject only); approved card has []
        ui_dead = compute_approval_card_actions(self.conn, dead_row, now=now, testing_mode=True)
        self.assertEqual(ui_dead["available_actions"], ["reject"])
        self.assertIsNone(ui_dead["always_allow_effect"])
        self.assertTrue(ui_dead["decidable"])
        self.assertFalse(ui_dead["approvable"])
        self.assertTrue(ui_dead["dismiss_only"])
        self.assertIsNone(ui_dead["undecidable_reason"])
        self.assertEqual(ui_dead["unapprovable_reason"], "task_terminal")
        self.assertTrue(ui_dead["task_terminal"])
        self.assertEqual(ui_dead["task_status"], "cancelled")

        # 4. Past-TTL unswept pending card -> all actions suppressed (server refuses reject with APPROVAL_EXPIRED)
        ui_exp = compute_approval_card_actions(
            self.conn, live_row, now="2099-01-01T00:00:00.000Z", testing_mode=True
        )
        self.assertEqual(ui_exp["available_actions"], [])
        self.assertIsNone(ui_exp["always_allow_effect"])
        self.assertFalse(ui_exp["decidable"])
        self.assertFalse(ui_exp["approvable"])
        self.assertFalse(ui_exp["dismiss_only"])
        self.assertTrue(ui_exp["expired"])
        self.assertEqual(ui_exp["undecidable_reason"], "expired")
        self.assertEqual(ui_exp["unapprovable_reason"], "expired")

        # 5. Paused project -> pending card is dismiss-only (reject only), approved card has []
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'paused' WHERE id = 'prj_1';")
        ui_paused = compute_approval_card_actions(self.conn, live_row, now=now, testing_mode=True)
        self.assertEqual(ui_paused["available_actions"], ["reject"])
        self.assertIsNone(ui_paused["always_allow_effect"])
        self.assertTrue(ui_paused["decidable"])
        self.assertFalse(ui_paused["approvable"])
        self.assertTrue(ui_paused["dismiss_only"])
        self.assertTrue(ui_paused["inactive_project"])
        self.assertIsNone(ui_paused["undecidable_reason"])
        self.assertEqual(ui_paused["unapprovable_reason"], "project_not_active")

        approved_paused = dict(live_row)
        approved_paused["status"] = "approved"
        approved_paused["decided_via"] = "direct"
        ui_paused_app = compute_approval_card_actions(self.conn, approved_paused, now=now, testing_mode=True)
        self.assertEqual(ui_paused_app["available_actions"], [])
        self.assertFalse(ui_paused_app["decidable"])
        self.assertFalse(ui_paused_app["approvable"])
        self.assertFalse(ui_paused_app["dismiss_only"])
        self.assertEqual(ui_paused_app["undecidable_reason"], "project_not_active")
        self.assertEqual(ui_paused_app["unapprovable_reason"], "project_not_active")

        # 6. Active project already in 'auto' mode -> approved step_dispatch no longer offers always_allow
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'active', permission_mode = 'auto' WHERE id = 'prj_1';")
            self.conn.execute(
                "UPDATE approvals SET status = 'approved', decided_via = 'direct' WHERE id = ?;",
                (live_apr["approval_id"],),
            )
        live_row_auto = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (live_apr["approval_id"],)).fetchone()
        ui_auto = compute_approval_card_actions(self.conn, live_row_auto, now=now, testing_mode=True)
        self.assertEqual(ui_auto["available_actions"], [])
        self.assertIsNone(ui_auto["always_allow_effect"])
        self.assertFalse(ui_auto["decidable"])
        self.assertEqual(ui_auto["undecidable_reason"], "already_auto_mode")

        # 7. Active standing grant already covers approved permission_escalation -> no redundant always_allow
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'active', permission_mode = 'supervised' WHERE id = 'prj_1';")
            esc_apr = create_or_get_pending_approval(
                self.conn,
                category="permission_escalation",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_live", "to": "w1", "profile": "unrestricted"},
                summary="Escalate to unrestricted",
                project_id="prj_1",
                task_id="tsk_live",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            self.conn.execute(
                "UPDATE approvals SET status = 'approved', decided_via = 'direct' WHERE id = ?;",
                (esc_apr["approval_id"],),
            )
        esc_row = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (esc_apr["approval_id"],)).fetchone()
        ui_before_grant = compute_approval_card_actions(self.conn, esc_row, now=now, testing_mode=True)
        self.assertEqual(ui_before_grant["available_actions"], ["always_allow"])
        # Bare/unverified standing_grants row without grant.created event must not suppress always_allow
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO standing_grants (id, project_id, category, scope_key, scope_agent_id, scope_profile, created_by, created_via, user_mac, created_at) "
                "VALUES ('grt_unverified_1', 'prj_1', 'permission_escalation', 'permission_escalation:prj_1:agt_1:unrestricted', 'agt_1', 'unrestricted', 'usr_1', 'direct', 'testmac', ?);",
                (now,),
            )
        ui_unverified = compute_approval_card_actions(self.conn, esc_row, now=now, testing_mode=True)
        self.assertEqual(ui_unverified["available_actions"], ["always_allow"])
        self.assertTrue(ui_unverified["decidable"])

        # Real event-backed standing grant suppresses redundant always_allow
        with transaction(self.conn):
            self.conn.execute("DELETE FROM standing_grants WHERE id = 'grt_unverified_1';")
            create_or_get_standing_grant(
                self.conn,
                project_id="prj_1",
                category="permission_escalation",
                scope_agent_id="agt_1",
                scope_profile="unrestricted",
                actor_id="usr_1",
                k_user=self.k_user,
            )
        ui_after_grant = compute_approval_card_actions(self.conn, esc_row, now=now, testing_mode=True)
        self.assertEqual(ui_after_grant["available_actions"], [])
        self.assertIsNone(ui_after_grant["always_allow_effect"])
        self.assertFalse(ui_after_grant["decidable"])

    def test_always_allow_rejected_on_terminal_task(self) -> None:
        from mesh.core.approvals import create_or_get_pending_approval
        from mesh.daemon.handlers.approval import handle_approval_decide
        from mesh.daemon.handlers.base import HandlerContext

        now = now_iso_utc()
        with transaction(self.conn):
            for tid, kind, st in (
                ("tsk_term_can", "code", "cancelled"),
                ("tsk_term_mrg", "code", "merged"),
                ("tsk_term_acc", "general", "accepted"),
            ):
                self.conn.execute(
                    "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at) "
                    "VALUES (?, 'prj_1', 'T', 'S', ?, ?, 'edit', 60, 3, 'agt_2', ?, ?);",
                    (tid, kind, st, now, now),
                )
            step_can = create_or_get_pending_approval(
                self.conn,
                category="step_dispatch",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_term_can", "to": "w1", "profile": "edit"},
                summary="Dead step",
                project_id="prj_1",
                task_id="tsk_term_can",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            esc_mrg = create_or_get_pending_approval(
                self.conn,
                category="permission_escalation",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_term_mrg", "to": "w1", "profile": "elevated"},
                summary="Dead esc merged",
                project_id="prj_1",
                task_id="tsk_term_mrg",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            esc_acc = create_or_get_pending_approval(
                self.conn,
                category="permission_escalation",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_term_acc", "to": "w1", "profile": "elevated"},
                summary="Dead esc accepted general",
                project_id="prj_1",
                task_id="tsk_term_acc",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )

        for apr, action, extra in (
            (step_can, "always_allow", {"confirm_auto_mode": False}),
            (step_can, "always_allow", {"confirm_auto_mode": True}),
            (step_can, "allow", {}),
            (esc_mrg, "always_allow", {}),
            (esc_acc, "always_allow", {}),
        ):
            ctx = HandlerContext(
                conn=self.conn,
                principal={"id": "usr_1", "kind": "user", "name": "user"},
                actor_kind="user",
                via="direct",
                request_id="req_dead_task",
                home=self.tmp.name,
                cmd="approval.decide",
                args={"approval_id": apr["approval_id"], "action": action, "op_hash": apr["op_hash"], **extra},
                k_user=self.k_user,
                testing_mode=True,
            )
            with self.assertRaises(MeshError) as err_ctx:
                handle_approval_decide(ctx)
            self.assertEqual(err_ctx.exception.code, c.INVALID_TRANSITION)
            self.assertEqual(err_ctx.exception.exit_code, c.EXIT_CONFLICT)
            self.assertEqual(err_ctx.exception.details.get("reason"), "task_terminal")

        self.assertEqual(
            self.conn.execute("SELECT permission_mode FROM projects WHERE id = 'prj_1';").fetchone()["permission_mode"],
            "plan",
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM standing_grants WHERE project_id = 'prj_1';").fetchone()[0],
            0,
        )

    def test_progressed_task_cards_suppressed_and_refused(self) -> None:
        from mesh.core.approvals import create_or_get_pending_approval
        from mesh.core.grants import compute_approval_card_actions
        from mesh.daemon.handlers.approval import handle_approval_decide
        from mesh.daemon.handlers.base import HandlerContext

        now = now_iso_utc()
        with transaction(self.conn):
            for tid, st in (
                ("tsk_prog_assigned", "assigned"),
                ("tsk_prog_running", "running"),
                ("tsk_prog_queued", "queued"),
                ("tsk_prog_accepted", "accepted"),
                ("tsk_ok_escalated", "escalated"),
            ):
                self.conn.execute(
                    "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at) "
                    "VALUES (?, 'prj_1', 'T', 'S', 'code', ?, 'edit', 60, 1, 'agt_2', ?, ?);",
                    (tid, st, now, now),
                )
            step_prog = create_or_get_pending_approval(
                self.conn,
                category="step_dispatch",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_prog_assigned", "to": "w1", "profile": "edit"},
                summary="Step on progressed assigned task",
                project_id="prj_1",
                task_id="tsk_prog_assigned",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            esc_prog = create_or_get_pending_approval(
                self.conn,
                category="permission_escalation",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_prog_running", "to": "w1", "profile": "elevated"},
                summary="Escalation on progressed running task",
                project_id="prj_1",
                task_id="tsk_prog_running",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            retry_prog = create_or_get_pending_approval(
                self.conn,
                category="retry_over_limit",
                op="task.retry",
                requested_by="agt_2",
                params={"task_id": "tsk_prog_queued"},
                summary="Retry on already-queued task",
                project_id="prj_1",
                task_id="tsk_prog_queued",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            reject_prog = create_or_get_pending_approval(
                self.conn,
                category="retry_over_limit",
                op="task.reject",
                requested_by="agt_2",
                params={"task_id": "tsk_prog_accepted"},
                summary="Reject on already-accepted task",
                project_id="prj_1",
                task_id="tsk_prog_accepted",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            reassign_prog = create_or_get_pending_approval(
                self.conn,
                category="permission_escalation",
                op="task.reassign",
                requested_by="agt_2",
                params={"task_id": "tsk_prog_running", "to": "w1", "profile": "elevated"},
                summary="Reassign on running task",
                project_id="prj_1",
                task_id="tsk_prog_running",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            retry_esc_ok = create_or_get_pending_approval(
                self.conn,
                category="retry_over_limit",
                op="task.retry",
                requested_by="agt_2",
                params={"task_id": "tsk_ok_escalated"},
                summary="Retry on escalated task (valid from-state)",
                project_id="prj_1",
                task_id="tsk_ok_escalated",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            ext_ok = create_or_get_pending_approval(
                self.conn,
                category="permission_escalation",
                op="external.permission_escalation",
                requested_by="agt_2",
                params={"to": "w1", "profile": "elevated"},
                summary="External approval bound to assigned task",
                project_id="prj_1",
                task_id="tsk_prog_assigned",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )

        # Valid from-state (escalated for task.retry) offers always_allow; external.* stays decidable (allow/reject only, no always_allow)
        row_retry_ok = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (retry_esc_ok["approval_id"],)).fetchone()
        ui_retry_ok = compute_approval_card_actions(self.conn, row_retry_ok, now=now, testing_mode=True)
        self.assertEqual(ui_retry_ok["available_actions"], ["allow", "always_allow", "reject"])
        self.assertTrue(ui_retry_ok["decidable"])
        self.assertFalse(ui_retry_ok["task_terminal"])
        self.assertFalse(ui_retry_ok["task_progressed"])

        row_ext_ok = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (ext_ok["approval_id"],)).fetchone()
        ui_ext_ok = compute_approval_card_actions(self.conn, row_ext_ok, now=now, testing_mode=True)
        self.assertEqual(ui_ext_ok["available_actions"], ["allow", "reject"])
        self.assertIsNone(ui_ext_ok["always_allow_effect"])
        self.assertTrue(ui_ext_ok["decidable"])
        self.assertFalse(ui_ext_ok["task_terminal"])
        self.assertFalse(ui_ext_ok["task_progressed"])

        # Progressed non-terminal tasks offer reject-only dismissal in compute_approval_card_actions
        # AND refuse allow/always_allow in handle_approval_decide
        for apr, exp_states in (
            (step_prog, ["created"]),
            (esc_prog, ["created"]),
            (retry_prog, ["failed", "escalated"]),
            (reject_prog, ["submitted", "merge_failed"]),
            (reassign_prog, ["assigned", "queued", "rework", "failed", "escalated", "merge_failed"]),
        ):
            row = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()
            ui = compute_approval_card_actions(self.conn, row, now=now, testing_mode=True)
            self.assertEqual(ui["available_actions"], ["reject"])
            self.assertIsNone(ui["always_allow_effect"])
            self.assertTrue(ui["decidable"])
            self.assertFalse(ui["approvable"])
            self.assertTrue(ui["dismiss_only"])
            self.assertFalse(ui["task_terminal"])
            self.assertTrue(ui["task_progressed"])
            self.assertIsNone(ui["undecidable_reason"])
            self.assertEqual(ui["unapprovable_reason"], "task_progressed")

            for action, extra in (("allow", {}), ("always_allow", {"confirm_auto_mode": True})):
                ctx = HandlerContext(
                    conn=self.conn,
                    principal={"id": "usr_1", "kind": "user", "name": "user"},
                    actor_kind="user",
                    via="direct",
                    request_id="req_prog_task",
                    home=self.tmp.name,
                    cmd="approval.decide",
                    args={"approval_id": apr["approval_id"], "action": action, "op_hash": apr["op_hash"], **extra},
                    k_user=self.k_user,
                    testing_mode=True,
                )
                with self.assertRaises(MeshError) as err_ctx:
                    handle_approval_decide(ctx)
                self.assertEqual(err_ctx.exception.code, c.INVALID_TRANSITION)
                self.assertEqual(err_ctx.exception.exit_code, c.EXIT_CONFLICT)
                self.assertEqual(err_ctx.exception.details.get("reason"), "task_progressed")
                self.assertEqual(err_ctx.exception.details.get("expected_task_statuses"), exp_states)

        self.assertEqual(
            self.conn.execute("SELECT permission_mode FROM projects WHERE id = 'prj_1';").fetchone()["permission_mode"],
            "plan",
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM standing_grants WHERE project_id = 'prj_1';").fetchone()[0],
            0,
        )

    def test_always_allow_on_already_auto_project_skips_misleading_copy(self) -> None:
        import json
        from mesh.core.approvals import create_or_get_pending_approval
        from mesh.daemon.handlers.approval import handle_approval_decide
        from mesh.daemon.handlers.base import HandlerContext

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET permission_mode = 'auto' WHERE id = 'prj_1';")
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at) "
                "VALUES ('tsk_auto_step', 'prj_1', 'Step', 'S', 'code', 'created', 'edit', 60, 3, 'agt_2', ?, ?);",
                (now, now),
            )
            step_apr = create_or_get_pending_approval(
                self.conn,
                category="step_dispatch",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_auto_step", "to": "w1", "profile": "edit"},
                summary="Step on already-auto project",
                project_id="prj_1",
                task_id="tsk_auto_step",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )

        # Calling always_allow WITHOUT confirm_auto_mode on an already-auto project skips the switch prompt
        ctx1 = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_already_auto_1",
            home=self.tmp.name,
            cmd="approval.decide",
            args={
                "approval_id": step_apr["approval_id"],
                "action": "always_allow",
                "op_hash": step_apr["op_hash"],
                "confirm_auto_mode": False,
            },
            k_user=self.k_user,
            testing_mode=True,
        )
        with transaction(self.conn):
            res1 = handle_approval_decide(ctx1)
        self.assertFalse(res1.data["confirmation_required"])
        self.assertEqual(res1.data["status"], "approved")
        self.assertEqual(res1.data["permission_mode"], "auto")
        self.assertEqual(res1.data["previous_permission_mode"], "auto")
        self.assertFalse(res1.data["mode_changed"])
        self.assertTrue(res1.data["already_auto"])
        self.assertIn("已处于「全自动 (auto)」模式", res1.data["message"])
        self.assertNotIn("将项目权限模式切换为", res1.data["message"])

        # approval.decided payload MUST NOT claim switched_permission_mode_to='auto'
        ev = self.conn.execute(
            "SELECT payload_json FROM events WHERE approval_id = ? AND type = 'approval.decided';",
            (step_apr["approval_id"],),
        ).fetchone()
        self.assertIsNotNone(ev)
        self.assertNotIn("switched_permission_mode_to", json.loads(ev["payload_json"]))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE project_id = 'prj_1' AND type = 'project.permission_mode_changed';"
            ).fetchone()[0],
            0,
        )

        # Calling always_allow AGAIN with confirm_auto_mode=True on the now-approved card stays truthful & idempotent
        ctx2 = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_already_auto_2",
            home=self.tmp.name,
            cmd="approval.decide",
            args={
                "approval_id": step_apr["approval_id"],
                "action": "always_allow",
                "op_hash": step_apr["op_hash"],
                "confirm_auto_mode": True,
            },
            k_user=self.k_user,
            testing_mode=True,
        )
        with transaction(self.conn):
            res2 = handle_approval_decide(ctx2)
        self.assertFalse(res2.data["confirmation_required"])
        self.assertTrue(res2.data["already_decided"])
        self.assertFalse(res2.data["mode_changed"])
        self.assertTrue(res2.data["already_auto"])
        self.assertIn("已处于「全自动 (auto)」模式", res2.data["message"])
        self.assertNotIn("将项目权限模式切换为", res2.data["message"])

    def test_r5_legacy_approve_liveness_dismiss_and_relayed_reject_upgrade(self) -> None:
        from mesh.core.approvals import create_or_get_pending_approval
        from mesh.core.grants import compute_approval_card_actions
        from mesh.daemon.handlers.approval import (
            handle_approval_approve,
            handle_approval_decide,
            handle_approval_reject,
        )
        from mesh.daemon.handlers.base import HandlerContext

        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_rel', 'agent', 'relayer', ?);",
                (now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, capabilities_json, created_at, updated_at) "
                "VALUES ('agt_rel', 'none', 'inbox', 0, 1, '[\"relay\"]', ?, ?);",
                (now, now),
            )
            for tid, st in (
                ("tsk_r5_assigned", "assigned"),
                ("tsk_r5_cancelled", "cancelled"),
                ("tsk_r5_failed", "failed"),
                ("tsk_r5_failed2", "failed"),
            ):
                self.conn.execute(
                    "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at) "
                    "VALUES (?, 'prj_1', 'T', 'S', 'code', ?, 'edit', 60, 1, 'agt_2', ?, ?);",
                    (tid, st, now, now),
                )
            step_prog = create_or_get_pending_approval(
                self.conn,
                category="step_dispatch",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_r5_assigned", "to": "w1", "profile": "edit"},
                summary="Progressed step",
                project_id="prj_1",
                task_id="tsk_r5_assigned",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            esc_term = create_or_get_pending_approval(
                self.conn,
                category="permission_escalation",
                op="task.assign",
                requested_by="agt_2",
                params={"task_id": "tsk_r5_cancelled", "to": "w1", "profile": "elevated"},
                summary="Cancelled esc",
                project_id="prj_1",
                task_id="tsk_r5_cancelled",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            retry_live = create_or_get_pending_approval(
                self.conn,
                category="retry_over_limit",
                op="task.retry",
                requested_by="agt_2",
                params={"task_id": "tsk_r5_failed"},
                summary="Retry live",
                project_id="prj_1",
                task_id="tsk_r5_failed",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )
            retry_live2 = create_or_get_pending_approval(
                self.conn,
                category="retry_over_limit",
                op="task.retry",
                requested_by="agt_2",
                params={"task_id": "tsk_r5_failed2"},
                summary="Retry live 2",
                project_id="prj_1",
                task_id="tsk_r5_failed2",
                actor_kind="agent",
                via="token",
                testing_mode=True,
            )

        def _user_ctx(cmd: str, args: dict) -> HandlerContext:
            return HandlerContext(
                conn=self.conn,
                principal={"id": "usr_1", "kind": "user", "name": "user"},
                actor_kind="user",
                via="direct",
                request_id="req_r5",
                home=self.tmp.name,
                cmd=cmd,
                args=args,
                k_user=self.k_user,
                testing_mode=True,
            )

        # 1. Legacy approval.approve refuses progressed and terminal tasks
        with self.assertRaises(MeshError) as e_prog:
            handle_approval_approve(_user_ctx("approval.approve", {
                "approval_id": step_prog["approval_id"], "op_hash": step_prog["op_hash"],
            }))
        self.assertEqual(e_prog.exception.code, c.INVALID_TRANSITION)
        self.assertEqual(e_prog.exception.details.get("reason"), "task_progressed")

        with self.assertRaises(MeshError) as e_term:
            handle_approval_approve(_user_ctx("approval.approve", {
                "approval_id": esc_term["approval_id"], "op_hash": esc_term["op_hash"],
            }))
        self.assertEqual(e_term.exception.code, c.INVALID_TRANSITION)
        self.assertEqual(e_term.exception.details.get("reason"), "task_terminal")

        # 2. Legacy approval.approve refuses terminal (cancelled) project for both direct user and relayer,
        #    and refuses paused project for step_dispatch cards
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'cancelled' WHERE id = 'prj_1';")
        with self.assertRaises(MeshError) as e_cancelled:
            handle_approval_approve(_user_ctx("approval.approve", {
                "approval_id": retry_live["approval_id"], "op_hash": retry_live["op_hash"],
            }))
        self.assertEqual(e_cancelled.exception.code, c.PROJECT_NOT_ACTIVE)

        rel_ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "agt_rel", "kind": "agent", "name": "relayer", "capabilities": ["relay"]},
            actor_kind="agent",
            via="relayed",
            request_id="req_r5_rel",
            home=self.tmp.name,
            cmd="approval.approve",
            args={
                "approval_id": retry_live["approval_id"],
                "op_hash": retry_live["op_hash"],
                "user_statement": "approve retry",
            },
            testing_mode=True,
        )
        with self.assertRaises(MeshError) as e_rel_cancelled:
            handle_approval_approve(rel_ctx)
        self.assertEqual(e_rel_cancelled.exception.code, c.PROJECT_NOT_ACTIVE)

        # 3. Dead cards CAN be dismissed via approval.decide(action="reject") and legacy approval.reject
        with transaction(self.conn):
            rej1 = handle_approval_decide(_user_ctx("approval.decide", {
                "approval_id": step_prog["approval_id"], "action": "reject", "op_hash": step_prog["op_hash"],
            }))
            rej2 = handle_approval_reject(_user_ctx("approval.reject", {
                "approval_id": esc_term["approval_id"], "op_hash": esc_term["op_hash"],
            }))
        self.assertEqual(rej1.data["status"], "rejected")
        self.assertEqual(rej2.data["status"], "rejected")

        # 4. Relayed-rejected card upgrades to direct_verified on direct reject (both decide & legacy reject)
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'active' WHERE id = 'prj_1';")
            for apr_r in (retry_live, retry_live2):
                handle_approval_reject(HandlerContext(
                    conn=self.conn,
                    principal={"id": "agt_rel", "kind": "agent", "name": "relayer", "capabilities": ["relay"]},
                    actor_kind="agent",
                    via="relayed",
                    request_id="req_r5_rel_rej",
                    home=self.tmp.name,
                    cmd="approval.reject",
                    args={
                        "approval_id": apr_r["approval_id"],
                        "op_hash": apr_r["op_hash"],
                        "user_statement": "no thanks",
                    },
                    testing_mode=True,
                ))

        row_r1 = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (retry_live["approval_id"],)).fetchone()
        ui_r1 = compute_approval_card_actions(self.conn, row_r1, now=now, testing_mode=True)
        self.assertEqual(ui_r1["available_actions"], ["reject"])
        self.assertTrue(ui_r1["decidable"])
        self.assertFalse(ui_r1["approvable"])
        self.assertFalse(ui_r1["dismiss_only"])

        with transaction(self.conn):
            up1 = handle_approval_decide(_user_ctx("approval.decide", {
                "approval_id": retry_live["approval_id"], "action": "reject", "op_hash": retry_live["op_hash"],
            }))
            up2 = handle_approval_reject(_user_ctx("approval.reject", {
                "approval_id": retry_live2["approval_id"], "op_hash": retry_live2["op_hash"],
            }))
        self.assertEqual(up1.data["trust"], "direct_verified")
        self.assertEqual(up1.data["upgraded_from"], "relayed")
        self.assertTrue(up1.data["user_mac"])
        self.assertEqual(up2.data["trust"], "direct_verified")
        self.assertEqual(up2.data["upgraded_from"], "relayed")
        self.assertTrue(up2.data["user_mac"])

        # After direct upgrade, card offers no actions
        row_r1_after = self.conn.execute("SELECT * FROM approvals WHERE id = ?;", (retry_live["approval_id"],)).fetchone()
        prow = self.conn.execute("SELECT id, status, permission_mode FROM projects WHERE id = 'prj_1';").fetchone()
        trow = self.conn.execute("SELECT id, status, kind FROM tasks WHERE id = 'tsk_r5_failed';").fetchone()
        ui_r1_after = compute_approval_card_actions(self.conn, row_r1_after, now=now, testing_mode=True)
        ui_r1_prefetched = compute_approval_card_actions(
            self.conn,
            row_r1_after,
            now=now,
            testing_mode=True,
            prefetched_project=prow,
            prefetched_tasks={"tsk_r5_failed": trow},
        )
        self.assertEqual(ui_r1_after, ui_r1_prefetched)
        self.assertEqual(ui_r1_after["available_actions"], [])
        self.assertFalse(ui_r1_after["decidable"])
        self.assertEqual(ui_r1_after["undecidable_reason"], "rejected")

    def test_r6_external_ops_never_preview_or_extract_grants_and_indexed_event_lookup(self) -> None:
        import json
        from mesh.core.grants import extract_grant_scopes_from_approval, preview_grant_scope_for_card

        # 1. External.* and missing op_type NEVER return preview or extract standing grants
        for cat, op_type, params in (
            ("step_dispatch", "external.step_dispatch", {"to": "w1", "profile": "edit"}),
            ("plan_dispatch", "external.plan_dispatch", {"plan_id": "pln_1"}),
            ("step_dispatch", None, {"to": "w1", "profile": "edit"}),
            ("permission_escalation", "external.permission_escalation", {"to": "w1", "profile": "unrestricted"}),
            ("permission_escalation", None, {"to": "w1", "profile": "unrestricted"}),
            ("retry_over_limit", "external.retry_over_limit", {"n": 1}),
            ("retry_over_limit", None, {"n": 1}),
        ):
            row = {
                "id": f"apr_ext_{cat}_{op_type}",
                "project_id": "prj_1",
                "task_id": None,
                "category": cat,
                "op_type": op_type,
                "op_json": json.dumps({"params": params}),
            }
            self.assertIsNone(preview_grant_scope_for_card(self.conn, row, testing_mode=True))
            with self.assertRaises(MeshError) as err:
                extract_grant_scopes_from_approval(self.conn, row, testing_mode=True)
            self.assertEqual(err.exception.code, c.INVALID_ARGUMENT)

        # 2. Nit 1: idx_events_type_project exists and SQLite uses it for grant event verification
        plan_rows = self.conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT type, actor_kind, via, user_mac, payload_json
               FROM events
               WHERE type IN ('grant.created', 'grant.revoked')
                 AND project_id = 'prj_1'
               ORDER BY seq ASC;"""
        ).fetchall()
        plan_text = " ".join(str(r[-1]) for r in plan_rows)
        self.assertIn("idx_events_type_project", plan_text)

        # Create + lookup + revoke grant and confirm grant.revoked records approval_id
        with transaction(self.conn):
            g, created = create_or_get_standing_grant(
                self.conn,
                project_id="prj_1",
                category="permission_escalation",
                scope_agent_id="agt_1",
                scope_profile="elevated",
                actor_id="usr_1",
                k_user=self.k_user,
            )
            self.assertTrue(created)
        self.assertIsNotNone(
            find_matching_standing_grant(
                self.conn, project_id="prj_1", category="permission_escalation", agent_id="agt_1", profile="elevated"
            )
        )
        with transaction(self.conn):
            revoke_standing_grant(self.conn, grant_id=g["id"], actor_id="usr_1", k_user=self.k_user, reason="done")
        self.assertIsNone(
            find_matching_standing_grant(
                self.conn, project_id="prj_1", category="permission_escalation", agent_id="agt_1", profile="elevated"
            )
        )


if __name__ == "__main__":
    unittest.main()
