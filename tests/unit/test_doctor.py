"""Unit tests for 'mesh doctor --check-invariants' CLI command.

ACC: A8
"""

import json
import os
import shutil
import tempfile
import unittest

from tests.helpers import MeshHarness
from mesh.core.db import connect, transaction
from mesh.core.events import record_event


class TestDoctorCLI(unittest.TestCase):
    """Test doctor --check-invariants CLI output and exit codes. ACC: A8"""

    def setUp(self):
        self.harness = MeshHarness()

    def tearDown(self):
        self.harness.teardown()

    def test_doctor_check_invariants_fresh_home(self):
        """Verify doctor --check-invariants returns rc 0 and empty violations on fresh home. ACC: A8"""
        # Fresh uninitialized home (mesh.db does not exist yet)
        rc, envelope = self.harness.cli("doctor", "--check-invariants")
        self.assertEqual(rc, 0)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["violations"], [])

    def test_doctor_check_invariants_initialized_clean_db(self):
        """Verify doctor --check-invariants on clean initialized DB returns rc 0 and empty violations. ACC: A8"""
        # A real `mesh init` home: since m3-test-kdf round 2 doctor fails closed on a
        # mesh.db without the user passphrase credential (see test_test_kdf).
        rc_init, _ = self.harness.init()
        self.assertEqual(rc_init, 0)

        rc, envelope = self.harness.cli("doctor", "--check-invariants")
        self.assertEqual(rc, 0)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["violations"], [])

    def test_doctor_check_invariants_corrupted_db(self):
        """Verify doctor --check-invariants returns exit 1 INTERNAL_ERROR when violations exist. ACC: A8"""
        rc_init, _ = self.harness.init()
        self.assertEqual(rc_init, 0)
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = connect(db_path)
        with transaction(conn):
            # Insert project without active lead -> violates I5
            conn.execute(
                """
                INSERT INTO projects (
                    id, name, goal, status, created_by, created_via, created_at, updated_at
                ) VALUES (
                    'prj_bad', 'Bad Prj', 'Goal', 'active', 'usr_1', 'direct',
                    '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z'
                );
                """
            )
            record_event(
                conn,
                type_="project.created",
                actor_id="usr_1",
                actor_kind="user",
                via="direct",
                project_id="prj_bad",
                to_state="active",
            )
        conn.close()

        rc, envelope = self.harness.cli("doctor", "--check-invariants")
        self.assertEqual(rc, 1)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "INTERNAL_ERROR")
        self.assertEqual(envelope["exit_code"], 1)
        violations = envelope["error"]["details"]["violations"]
        self.assertGreaterEqual(len(violations), 1)
        self.assertEqual(violations[0]["invariant"], "I5")
        self.harness.check_invariants_on_teardown = False


if __name__ == "__main__":
    unittest.main()
