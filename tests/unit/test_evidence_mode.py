"""Productize: adapter.evidence_mode (warn default outside testing mode, strict opt-in)."""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from mesh.adapters import evidence
from mesh.adapters.registry import get_adapter
from mesh.core.db import init_db
from mesh.core.settings import get_default_settings, validate_setting
from mesh.errors import MeshError


class TestEvidenceMode(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = init_db(os.path.join(self.tmp.name, "mesh.db"))
        self.addCleanup(self.conn.close)
        self.empty = os.path.join(self.tmp.name, "results")
        os.makedirs(self.empty)
        self.env = {evidence.OVERRIDE_ENV: self.empty}

    def _set(self, mode):
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) VALUES "
                          "('adapter.evidence_mode', ?, 'now', 'test');", (json.dumps(mode),))
        self.conn.commit()

    def _ev(self, adapter_id="droid"):
        return evidence.effective_launch(self.conn, get_adapter(adapter_id, testing_mode=True), True, None, self.env)

    def test_strict_refuses_without_evidence(self):
        self._set("strict")
        ev = self._ev()
        self.assertFalse(ev.ok)
        self.assertEqual(ev.reason, "no_smoke_evidence")
        self.assertIsNone(ev.warning)

    def test_warn_allows_with_warning(self):
        self._set("warn")
        ev = self._ev()
        self.assertTrue(ev.ok)
        self.assertEqual(ev.warning, "no_smoke_evidence")

    def test_warn_does_not_lift_launch_hold(self):
        self._set("warn")
        env = dict(self.env, **{evidence.HOLD_ENV: "droid:M9"})
        ev = evidence.effective_launch(self.conn, get_adapter("droid", testing_mode=True), True, None, env)
        self.assertFalse(ev.ok)
        self.assertEqual(ev.reason, "launch_held")

    def test_warn_never_waives_stub_adapters(self):
        self._set("warn")
        for aid in ("hermes", "openclaw"):
            ev = self._ev(aid)
            self.assertFalse(ev.ok, aid)
            self.assertIsNone(ev.warning, aid)

    def test_mode_defaults(self):
        self.assertEqual(evidence.evidence_mode(None, testing_mode=False), "warn")
        self.assertEqual(evidence.evidence_mode(None, testing_mode=True), "strict")
        with mock.patch.dict(os.environ, {"MESH_TESTING": "0"}):
            self.assertEqual(get_default_settings("/h")["adapter.evidence_mode"], "warn")

    def test_setting_validation(self):
        self.assertEqual(validate_setting("adapter.evidence_mode", "strict", "/h"), "strict")
        with self.assertRaises(MeshError):
            validate_setting("adapter.evidence_mode", "off", "/h")

    def test_probe_view_reports_warning(self):
        self._set("warn")
        data = evidence.apply_to_probe({"capabilities": {}, "status": "error"},
                                       get_adapter("droid", testing_mode=True), self._ev())
        self.assertTrue(data["launch_enabled"])
        self.assertEqual(data["evidence_warning"], "no_smoke_evidence")


if __name__ == "__main__":
    unittest.main()
