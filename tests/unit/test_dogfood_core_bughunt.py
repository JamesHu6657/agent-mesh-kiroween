"""Dogfood core bug hunt (tsk_a1d28487fb5rvfuxc3l54jbbrq) regressions.

1. LEASE_EXPIRED gave a remote inbox worker no hint why or what to do: it now
   says the run's lease expired (TTL, task status) and to stop, with details.
2. P3: a workspaces.root / task workspace path matching a redaction pattern
   (e.g. ".../secret=x/...") was stored redacted in events, so after a root change
   the old root lost token-file protection. Paths are now kept unredacted in meta.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import mesh.constants as c
from mesh.cli.commands import setup as msetup
from mesh.core.leases import verify_task_lease_and_fence
from mesh.core.redact import redact_value
from mesh.core.token_roots import configured_roots, remember_protected_path
from mesh.errors import MeshError


class TestLeaseExpiredHint(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE leases (key TEXT PRIMARY KEY, run_id TEXT, fence INTEGER);")
        self.conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT);")
        self.conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT);")
        self.conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT, status TEXT, end_reason TEXT, ended_at TEXT);")
        self.conn.execute("INSERT INTO settings VALUES ('lease.ttl_s', '120');")

    def _err(self, run_id="run_1"):
        with self.assertRaises(MeshError) as cm:
            verify_task_lease_and_fence(self.conn, "tsk_1", presented_run_id=run_id, presented_fence=1)
        self.assertEqual(cm.exception.code, c.LEASE_EXPIRED)
        self.assertEqual(cm.exception.exit_code, c.EXIT_STALE)
        return cm.exception

    def test_expired_lease_is_explained(self):
        self.conn.execute("INSERT INTO tasks VALUES ('tsk_1', 'failed');")
        self.conn.execute("INSERT INTO runs VALUES ('run_1', 'tsk_1', 'lost', 'lease_expired', '2026-11-13T01:00:00Z');")
        e = self._err()
        self.assertIn("lease expired", e.message)
        self.assertIn("120s", e.message)
        self.assertIn("Stop work", e.message)
        self.assertEqual(e.details["run_end_reason"], "lease_expired")
        self.assertEqual(e.details["task_status"], "failed")
        self.assertEqual(e.details["lease_ttl_s"], 120)

    def test_ended_run_is_explained(self):
        self.conn.execute("INSERT INTO tasks VALUES ('tsk_1', 'submitted');")
        self.conn.execute("INSERT INTO runs VALUES ('run_1', 'tsk_1', 'exited', 'submitted', '2026-11-13T01:00:00Z');")
        e = self._err()
        self.assertIn("already ended", e.message)
        self.assertEqual(e.details["task_status"], "submitted")

    def test_unknown_run_keeps_plain_message(self):
        e = self._err(run_id=None)
        self.assertTrue(e.message.startswith("No active lease held for task 'tsk_1'"))


class TestRedactedRootStaysProtected(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-dfc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.conn = sqlite3.connect(os.path.join(self.home, "mesh.db"))
        for ddl in (
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);",
            "CREATE TABLE projects (id TEXT PRIMARY KEY, repo_path TEXT);",
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, workspace_path TEXT);",
            "CREATE TABLE events (seq INTEGER PRIMARY KEY, type TEXT, payload_json TEXT);",
        ):
            self.conn.execute(ddl)
        self.addCleanup(self.conn.close)

    def _set_root(self, root):
        self.conn.execute("INSERT OR REPLACE INTO settings VALUES ('workspaces.root', ?, 'x', 'u');", (json.dumps(root),))
        # Events are stored redacted, exactly as record_event/insert_event do.
        payload = redact_value({"key": "workspaces.root", "value": root})
        self.conn.execute("INSERT INTO events (type, payload_json) VALUES ('config.updated', ?);", (json.dumps(payload),))

    def test_secret_like_old_root_lead_dir_stays_protected(self):
        old_root = os.path.join(self.tmp, "secret=abc", "ws")
        new_root = os.path.join(self.tmp, "rootB")
        lead = os.path.join(old_root, "prj_1", "lead", "agt_1")
        os.makedirs(lead)
        self._set_root(old_root)
        remember_protected_path(self.conn, old_root)
        self._set_root(new_root)
        remember_protected_path(self.conn, new_root)
        self.conn.commit()
        _, paths = configured_roots(self.home)
        self.assertIn(os.path.abspath(old_root), paths)
        with self.assertRaises(MeshError):
            msetup._check_token_destination(self.home, os.path.join(lead, "lead.token"))

    def test_redacted_event_alone_loses_the_path(self):
        """Documents the root cause: the event payload no longer holds the real path."""
        old_root = os.path.join(self.tmp, "secret=abc", "ws")
        self._set_root(old_root)
        (pj,) = self.conn.execute("SELECT payload_json FROM events;").fetchone()
        self.assertNotIn("secret=abc", pj)

    def test_remember_is_idempotent_and_normalised(self):
        remember_protected_path(self.conn, " /a/b/../c ")
        remember_protected_path(self.conn, "/a/c")
        remember_protected_path(self.conn, "")
        remember_protected_path(self.conn, None)
        rows = self.conn.execute("SELECT value FROM meta;").fetchall()
        self.assertEqual(rows, [("/a/c",)])


if __name__ == "__main__":
    unittest.main()
