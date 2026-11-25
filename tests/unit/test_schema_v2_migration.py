"""Unit tests for Schema Migration v2 (chat_messages, plans, tasks.plan_id, projects.plan_gate_mode, plan_dispatch)."""

import os
import sqlite3
import tempfile
import unittest

from mesh.core.approvals import DEFAULT_POLICIES
from mesh.core.db import backup_db, check_quick, connect, get_schema_version, init_db, migrate_db, transaction
from mesh.core.schema import CURRENT_SCHEMA_VERSION, INITIAL_SCHEMA_STATEMENTS
from mesh.core.settings import APPROVAL_CATEGORIES, get_default_settings, validate_setting
from mesh.errors import MeshError
import mesh.constants as c


class TestSchemaV2Migration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-schema-v2-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_v1_to_v2_migration_preserves_legacy_projects_and_is_idempotent(self) -> None:
        # 1. Build a genuine v1 database using only INITIAL_SCHEMA_STATEMENTS
        conn = connect(self.db_path)
        try:
            with transaction(conn):
                for stmt in INITIAL_SCHEMA_STATEMENTS:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_legacy', 'user', 'user', '2026-01-01T00:00:00.000Z');"
                )
                conn.execute(
                    "INSERT INTO settings (key, value_json, updated_at, updated_by) "
                    "VALUES ('lead.wake_debounce_s', '5', '2026-01-01T00:00:00.000Z', 'usr_legacy');"
                )
                conn.execute(
                    """INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at)
                       VALUES ('prj_legacy', 'Legacy', 'Keep working', 'active', 'usr_legacy', 'direct', 1, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');"""
                )
                conn.execute(
                    """INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at)
                       VALUES ('tsk_legacy', 'prj_legacy', 'Old task', 'spec', 'general', 'created', 'edit', 600, 3, 'usr_legacy', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');"""
                )

            self.assertEqual(get_schema_version(conn), 1)

            # 2. Run migration v1 -> v2
            ver = migrate_db(conn)
            self.assertEqual(ver, 2)
            self.assertEqual(CURRENT_SCHEMA_VERSION, 2)
            self.assertEqual(get_schema_version(conn), 2)
            check_quick(conn)

            # 3. Existing project defaults to plan_gate_mode='off', permission_mode='plan'; existing task has NULL plan_id/plan_step_key
            prow = conn.execute("SELECT plan_gate_mode, permission_mode FROM projects WHERE id = 'prj_legacy';").fetchone()
            self.assertEqual(prow["plan_gate_mode"], "off")
            self.assertEqual(prow["permission_mode"], "plan")
            trow = conn.execute("SELECT plan_id, plan_step_key FROM tasks WHERE id = 'tsk_legacy';").fetchone()
            self.assertIsNone(trow["plan_id"])
            self.assertIsNone(trow["plan_step_key"])

            # 4. V2 settings backfilled for initialized v1 DB
            srows = {
                r["key"]: r["value_json"]
                for r in conn.execute("SELECT key, value_json FROM settings;").fetchall()
            }
            self.assertEqual(srows["approval.policy.plan_dispatch"], '"direct_only"')
            self.assertEqual(srows["approval.policy.step_dispatch"], '"direct_only"')
            self.assertEqual(srows["lead.chat_wake_debounce_s"], "0")
            self.assertEqual(srows["project.default_plan_gate"], '"off"')
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='standing_grants';"
            ).fetchone())

            # 5. Re-running migrate_db is idempotent
            self.assertEqual(migrate_db(conn), 2)
            check_quick(conn)
        finally:
            conn.close()

    def test_backup_db_creates_restorable_v1_snapshot(self) -> None:
        """P2-2: Pre-migration backup is a valid, restorable v1 SQLite database."""
        conn = connect(self.db_path)
        bak_path = os.path.join(self.temp_dir.name, "mesh.db.v1.bak")
        try:
            with transaction(conn):
                for stmt in INITIAL_SCHEMA_STATEMENTS:
                    conn.execute(stmt)
            self.assertEqual(get_schema_version(conn), 1)
            saved = backup_db(conn, bak_path)
            self.assertTrue(os.path.exists(saved))
            self.assertEqual(migrate_db(conn), 2)
            self.assertEqual(get_schema_version(conn), 2)
        finally:
            conn.close()

        bak_conn = connect(bak_path, read_only=True)
        try:
            self.assertEqual(get_schema_version(bak_conn), 1)
            check_quick(bak_conn)
        finally:
            bak_conn.close()

    def test_one_proposed_plan_unique_index_and_strict_constraints(self) -> None:
        conn = init_db(self.db_path)
        try:
            with transaction(conn):
                conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', 'now');")
                conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'lead1', 'now');")
                conn.execute(
                    "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                    "VALUES ('agt_1', 'fake', 'launch', 1, 1, 'now', 'now');"
                )
                conn.execute(
                    "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at, plan_gate_mode) "
                    "VALUES ('prj_1', 'Chat', 'Goal', 'active', 'usr_1', 'direct', 'now', 'now', 'require_approval');"
                )
                conn.execute(
                    """INSERT INTO plans (id, project_id, version, status, title, summary, steps_json, plan_hash,
                                          proposed_by_agent_id, proposed_event_seq, created_at, updated_at)
                       VALUES ('pln_1', 'prj_1', 1, 'proposed', 'T1', 'S1', '[]', 'sha256:aaa', 'agt_1', 1, 'now', 'now');"""
                )

            # Second proposed plan on same project violates unique index
            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(conn):
                    conn.execute(
                        """INSERT INTO plans (id, project_id, version, status, title, summary, steps_json, plan_hash,
                                              proposed_by_agent_id, proposed_event_seq, created_at, updated_at)
                           VALUES ('pln_2', 'prj_1', 2, 'proposed', 'T2', 'S2', '[]', 'sha256:bbb', 'agt_1', 2, 'now', 'now');"""
                    )

            # Once v1 is superseded, v2 proposed is allowed
            with transaction(conn):
                conn.execute("UPDATE plans SET status = 'superseded' WHERE id = 'pln_1';")
                conn.execute(
                    """INSERT INTO plans (id, project_id, version, status, title, summary, steps_json, plan_hash,
                                          proposed_by_agent_id, proposed_event_seq, created_at, updated_at)
                       VALUES ('pln_2', 'prj_1', 2, 'approved_pending_dispatch', 'T2', 'S2', '[]', 'sha256:bbb', 'agt_1', 2, 'now', 'now');"""
                )

            # Invalid plan_gate_mode rejected by CHECK constraint
            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at, plan_gate_mode) "
                        "VALUES ('prj_bad', 'Bad', 'Goal', 'active', 'usr_1', 'direct', 'now', 'now', 'invalid');"
                    )

            # Invalid permission_mode rejected by CHECK constraint
            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at, permission_mode) "
                        "VALUES ('prj_bad_pm', 'BadPM', 'Goal', 'active', 'usr_1', 'direct', 'now', 'now', 'yolo');"
                    )
        finally:
            conn.close()

    def test_v2_approval_category_and_settings(self) -> None:
        self.assertEqual(DEFAULT_POLICIES["plan_dispatch"], "direct_only")
        self.assertEqual(APPROVAL_CATEGORIES["plan_dispatch"], "direct_only")
        self.assertEqual(DEFAULT_POLICIES["step_dispatch"], "direct_only")
        self.assertEqual(APPROVAL_CATEGORIES["step_dispatch"], "direct_only")
        defaults = get_default_settings(self.temp_dir.name)
        self.assertEqual(defaults["approval.policy.plan_dispatch"], "direct_only")
        self.assertEqual(defaults["approval.policy.step_dispatch"], "direct_only")
        self.assertEqual(defaults["lead.chat_wake_debounce_s"], 0)
        self.assertEqual(defaults["project.default_plan_gate"], "off")

        for fixed_cat in ("config", "plan_dispatch", "step_dispatch"):
            with self.assertRaises(MeshError) as fctx:
                validate_setting(f"approval.policy.{fixed_cat}", "direct_or_relayed", self.temp_dir.name)
            self.assertEqual(fctx.exception.code, c.INVALID_SETTING)

        self.assertEqual(validate_setting("lead.chat_wake_debounce_s", 0, self.temp_dir.name), 0)
        self.assertEqual(validate_setting("lead.chat_wake_debounce_s", 2, self.temp_dir.name), 2)
        with self.assertRaises(MeshError) as ctx:
            validate_setting("lead.chat_wake_debounce_s", -1, self.temp_dir.name)
        self.assertEqual(ctx.exception.code, c.INVALID_SETTING)

        self.assertEqual(validate_setting("project.default_plan_gate", "require_approval", self.temp_dir.name), "require_approval")
        self.assertEqual(validate_setting("project.default_plan_gate", "off", self.temp_dir.name), "off")
        with self.assertRaises(MeshError) as ctx2:
            validate_setting("project.default_plan_gate", "maybe", self.temp_dir.name)
        self.assertEqual(ctx2.exception.code, c.INVALID_SETTING)


if __name__ == "__main__":
    unittest.main()
