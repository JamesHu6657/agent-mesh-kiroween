"""Unit tests for DDL, migrations, append-only triggers, and STRICT mode.

ACC: F3, I9
"""

import os
import sqlite3
import tempfile
import unittest

from mesh.errors import DbIntegrityError, SchemaTooNewError
from mesh.core.db import (
    check_quick,
    connect,
    get_schema_version,
    init_db,
    migrate_db,
    transaction,
)
from mesh.core.schema import CURRENT_SCHEMA_VERSION


class TestSchema(unittest.TestCase):
    """Test schema creation, STRICT tables, migrations, and triggers."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-schema-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schema_init_and_strict_tables(self):
        """Verify DDL creation, PRAGMA checks, and STRICT table enforcement."""
        conn = init_db(self.db_path)
        try:
            # Check schema version
            ver = get_schema_version(conn)
            self.assertEqual(ver, CURRENT_SCHEMA_VERSION)

            # Check PRAGMA quick_check passes
            check_quick(conn)

            # Test STRICT mode: invalid datatype on STRICT table should raise sqlite3.Error
            # e.g., meta key and value are TEXT
            with self.assertRaises(sqlite3.Error):
                with transaction(conn):
                    # In STRICT table with TEXT column, binding binary or incompatible object
                    # or violating CHECK constraint
                    conn.execute("INSERT INTO meta (key, value) VALUES (123, 456);")
                    # Try inserting invalid kind in principals CHECK constraint
                    conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'alien', 'bob', 'ts');")
        finally:
            conn.close()

    def test_events_append_only_triggers(self):
        """Verify UPDATE and DELETE on events table abort with 'events are append-only'. ACC: F3"""
        conn = init_db(self.db_path)
        try:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO events (
                        id, ts, type, actor_id, actor_kind, via,
                        reason, prev_hash, hash
                    ) VALUES (
                        'evt_1', '2026-11-07T00:00:00.000Z', 'task.created',
                        'usr_1', 'user', 'direct',
                        'initial reason', 'genesis', 'hash1'
                    );
                    """
                )

            # Test UPDATE aborts
            with self.assertRaises(sqlite3.Error) as ctx:
                with transaction(conn):
                    conn.execute("UPDATE events SET reason = 'tampered' WHERE id = 'evt_1';")
            self.assertIn("events are append-only", str(ctx.exception))

            # Test DELETE aborts
            with self.assertRaises(sqlite3.Error) as ctx2:
                with transaction(conn):
                    conn.execute("DELETE FROM events WHERE id = 'evt_1';")
            self.assertIn("events are append-only", str(ctx2.exception))
        finally:
            conn.close()

    def test_schema_too_new(self):
        """Verify database with future schema version raises SCHEMA_TOO_NEW error. ACC: I9"""
        conn = init_db(self.db_path)
        try:
            # Set meta.schema_version to future value
            with transaction(conn):
                conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version';")

            # Subsequent connection/migration attempt should raise SchemaTooNewError
            with self.assertRaises(SchemaTooNewError) as ctx:
                migrate_db(conn)

            self.assertEqual(ctx.exception.code, "SCHEMA_TOO_NEW")
            self.assertEqual(ctx.exception.exit_code, 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
