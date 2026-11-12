"""Unit tests for evidence-driven launch (m3-r5; ACC: N19, N20, N23).

Results and fixture trees are built in /tmp/mh-* from copies of real fixtures plus sidecars
(the smoke staging layout ``<S>/results`` + ``<S>/<adapter>/<version>/``). The override is
passed as an explicit env dict; os.environ never gets MESH_TEST_SMOKE_RESULTS_DIR.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from typing import Any, Dict, Optional
from unittest import mock

import mesh.core.schema as schema_mod
from mesh.adapters import evidence, smoke_schema
from mesh.adapters.base import AdapterUnsupported
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.registry import get_adapter
from tests import tierlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_INIT = os.path.join(REPO_ROOT, "tests", "fixtures", "real", "droid", "1.3.0", "exec-init-event-opus55-high.json")
SYNTHETIC = os.path.join(REPO_ROOT, "tests", "fixtures", "synthetic", "smoke", "synthetic_droid_stream.jsonl")


def _probes_ddl() -> str:
    for value in vars(schema_mod).values():
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            if isinstance(item, str) and item.startswith("CREATE TABLE adapter_probes"):
                return item
    raise AssertionError("adapter_probes DDL not found")


class EvidenceBase(unittest.TestCase):
    """Shared builders; no tests here."""

    VERSION = "1.3.0"

    def setUp(self) -> None:
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)
        self.tmp = tempfile.mkdtemp(prefix="mh-ev-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stage = os.path.join(self.tmp, "S")
        self.results = os.path.join(self.stage, "results")
        self.env = {evidence.OVERRIDE_ENV: self.results}
        self.binary = os.path.join(self.tmp, "bin", "droid")
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row  # like the daemon's connection
        self.addCleanup(self.conn.close)
        self.conn.execute(_probes_ddl())
        self.adapter = DroidAdapter()

    def tearDown(self) -> None:
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)

    def add_probe(self, version: Optional[str], binary: Optional[str] = None, at: str = "2026-11-20T00:00:00Z",
                  status: str = "partially_verified") -> None:
        self.conn.execute("INSERT INTO adapter_probes (adapter, probed_at, binary_path, version, help_sha256, "
                          "flags_json, models_json, status) VALUES ('droid', ?, ?, ?, NULL, '[]', NULL, ?);",
                          (at, binary or self.binary, version, status))

    def capture(self, case_id: str, version: Optional[str] = None, src: str = REAL_INIT) -> Dict[str, Any]:
        version = version or self.VERSION
        with open(src, "rb") as f:
            data = f.read()
        name = f"smoke-{case_id}-stdout.jsonl"
        rel = f"droid/{version}/{name}"
        if not os.path.exists(os.path.join(self.stage, rel)):
            tierlib.write_capture(self.stage, rel, data, ["droid", "exec"])
        return {"fixture": smoke_schema.fixture_rel("droid", version, name), "fixture_sha256": tierlib.sha256_bytes(data)}

    def case(self, cid: str, status: str = "pass", version: Optional[str] = None, with_fixture: bool = True) -> Dict[str, Any]:
        c = {"id": cid, "status": status, "reason": None if status in ("pass", "observed") else "x", "rc": 0,
             "duration_s": 1.0, "fixture": None, "fixture_sha256": None, "observation": None,
             "evaluated_from": None, "carried_from": None}
        if with_fixture:
            c.update(self.capture(cid, version))
        return c

    def write_results(self, cases, version: Optional[str] = None, date: str = "2026-11-20", doc_version: Optional[str] = None) -> str:
        version = version or self.VERSION
        doc = {"schema": smoke_schema.SCHEMA, "adapter": "droid", "binary": "/usr/local/bin/droid",
               "version": doc_version or version, "captured_at_utc": f"{date}T01:02:03Z", "mesh_commit": "abc1234",
               "model": "claude-sonnet-5-5", "effort": "low", "profile": "readonly", "timeout_s": 180,
               "consent_ref": "James 2026-11-09 12:44", "cases": cases}
        existing = os.listdir(self.results) if os.path.isdir(self.results) else []
        name = smoke_schema.results_name("droid", version, date, existing)
        return tierlib.write_capture(self.stage, f"results/{name}", json.dumps(doc).encode(), ["test"])

    def all_pass(self, version: Optional[str] = None):
        return [self.case(cid, version=version) for cid in ("S-X-1", "S-X-2", "S-X-4", "S-X-5", "S-X-6")]

    def launch(self, testing_mode: bool = True, env: Optional[Dict[str, str]] = None, binary: Optional[str] = None):
        return evidence.effective_launch(self.conn, self.adapter, testing_mode, binary or self.binary,
                                         env=self.env if env is None else env)


class EffectiveLaunchTest(EvidenceBase):
    def test_ok_and_results_location(self) -> None:
        """ACC: N19 - matching probe row + valid latest results with S-X-1 and S-X-6 pass -> ok; testing mode reads only the override (fixtures in its parent), none without it; outside testing mode the override is ignored."""
        self.add_probe(self.VERSION)
        path = self.write_results(self.all_pass())
        ev = self.launch()
        self.assertTrue(ev.ok, ev)
        self.assertIsNone(ev.reason)
        self.assertEqual(ev.results_path, path)
        self.assertEqual((ev.probed_version, ev.results_version), (self.VERSION, self.VERSION))
        self.assertEqual(evidence.results_dir(True, self.env), evidence.Path(self.results))
        self.assertEqual(evidence.fixture_root(True, self.env), evidence.Path(self.stage))
        self.assertIsNone(evidence.results_dir(True, {}))
        self.assertIsNone(evidence.fixture_root(True, {}))
        self.assertEqual(self.launch(env={}).reason, "no_smoke_evidence")
        committed = evidence.REPO_ROOT / "tests" / "smoke" / "results"
        self.assertEqual(evidence.results_dir(False, self.env), committed)
        self.assertEqual(evidence.fixture_root(False, self.env), evidence.REPO_ROOT / "tests" / "fixtures" / "real")
        non_testing = self.launch(testing_mode=False)
        self.assertFalse(non_testing.ok)
        self.assertNotEqual(non_testing.results_path, path)
        if smoke_schema.load_latest(str(committed), "droid") is None:
            self.assertEqual(non_testing.reason, "no_smoke_evidence")

    def test_probe_row_rules(self) -> None:
        """ACC: N19 - no row, a row for another binary, or a row without version -> no_probe_version; the newest row (probed_at DESC) decides."""
        self.write_results(self.all_pass())
        self.assertEqual(self.launch().reason, "no_probe_version")
        self.add_probe(self.VERSION, binary="/tmp/mh-other/droid")
        self.assertEqual(self.launch().reason, "no_probe_version")
        self.assertTrue(self.launch(binary="/tmp/mh-other/droid").ok)
        self.add_probe(None, at="2026-11-20T00:00:01Z")
        self.assertEqual(self.launch().reason, "no_probe_version")
        self.add_probe(self.VERSION, at="2026-11-20T00:00:02Z")
        self.assertTrue(self.launch().ok)
        self.add_probe("1.6.0", at="2026-11-20T00:00:03Z")
        self.assertEqual(self.launch().reason, "evidence_stale")
        # default binary: resolve_binary(None) (PATH lookup only)
        with mock.patch.object(DroidAdapter, "resolve_binary", return_value=self.binary):
            ev = evidence.effective_launch(self.conn, self.adapter, True, None, env=self.env)
        self.assertEqual(ev.reason, "evidence_stale")
        with mock.patch.object(DroidAdapter, "resolve_binary", return_value=None):
            self.assertEqual(evidence.effective_launch(self.conn, self.adapter, True, None, env=self.env).reason,
                             "no_probe_version")

    def test_stale_invalid_failed(self) -> None:
        """ACC: N19 - probed version != results version -> evidence_stale; a newer matching probe row -> ok; a tampered fixture -> evidence_invalid with errors."""
        self.add_probe("1.6.0")
        self.write_results(self.all_pass())
        ev = self.launch()
        self.assertEqual((ev.reason, ev.probed_version, ev.results_version), ("evidence_stale", "1.6.0", self.VERSION))
        self.add_probe(self.VERSION, at="2026-11-20T00:00:05Z")
        self.assertTrue(self.launch().ok)
        fx = os.path.join(self.stage, "droid", self.VERSION, "smoke-S-X-1-stdout.jsonl")
        os.chmod(fx, 0o644)
        with open(fx, "ab") as f:
            f.write(b"\n")
        ev = self.launch()
        self.assertEqual(ev.reason, "evidence_invalid")
        self.assertTrue(ev.errors)

    def test_invalid_variants(self) -> None:
        """ACC: N19 - results sidecar missing, synthetic fixture, doc version != file name version -> evidence_invalid."""
        self.add_probe(self.VERSION)
        path = self.write_results(self.all_pass())
        os.unlink(path + smoke_schema.SIDECAR_SUFFIX)
        self.assertEqual(self.launch().reason, "evidence_invalid")
        shutil.rmtree(self.results)
        cases = self.all_pass()
        cases[0]["fixture"] = os.path.relpath(SYNTHETIC, REPO_ROOT)
        self.write_results(cases)
        ev = self.launch()
        self.assertEqual(ev.reason, "evidence_invalid")
        self.assertTrue(any("synthetic" in e for e in ev.errors), ev.errors)
        shutil.rmtree(self.results)
        self.write_results(self.all_pass(), doc_version="1.3.1")
        self.assertEqual(self.launch().reason, "evidence_invalid")

    def test_launch_cases_and_q4_order(self) -> None:
        """ACC: N19 - S-X-6 fail or S-X-1 missing -> launch_cases_failed; the latest file per Q4 (date, then -N) decides, older passing files do not count."""
        self.add_probe(self.VERSION)
        self.write_results(self.all_pass(), date="2026-11-20")
        self.assertTrue(self.launch().ok)
        cases = self.all_pass()
        cases[-1] = self.case("S-X-6", status="fail", with_fixture=False)
        p2 = self.write_results(cases, date="2026-11-20")
        self.assertTrue(p2.endswith("-2.json"))
        ev = self.launch()
        self.assertEqual((ev.reason, ev.results_path), ("launch_cases_failed", p2))
        self.write_results([c for c in self.all_pass() if c["id"] != "S-X-1"], date="2026-11-21")
        self.assertEqual(self.launch().reason, "launch_cases_failed")
        self.write_results(self.all_pass(), date="2026-11-21")
        self.assertTrue(self.launch().ok)
        # an older, earlier-dated file never wins
        self.write_results(cases, date="2026-11-01")
        self.assertTrue(self.launch().ok)

    def test_failed_probe_rows_and_changed_binary(self) -> None:
        """ACC: N19 - an error/unverified probe row that recorded V never counts (no_probe_version); a binary modified after the newest good probe -> evidence_stale (stat only, Popen patched to raise); re-probing makes it ok again (Kiro r5 finding 3)."""
        self.write_results(self.all_pass())
        os.makedirs(os.path.dirname(self.binary))
        with open(self.binary, "w") as f:
            f.write("#!/bin/sh\n")
        os.utime(self.binary, (1_700_000_000, 1_700_000_000))  # 2023-11-14, before every probe row
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")):
            for status in ("error", "unverified", "not_installed"):
                self.add_probe(self.VERSION, at=f"2026-11-20T00:00:0{len(status) % 10}Z", status=status)
                self.assertEqual(self.launch().reason, "no_probe_version", status)
            self.add_probe(self.VERSION, at="2026-11-20T00:01:00.000Z")
            self.assertTrue(self.launch().ok)
            self.add_probe(self.VERSION, at="2026-11-20T00:02:00.000Z", status="error")  # newer, but failed
            self.assertTrue(self.launch().ok)
            # droid updated in place after the good probe (2026-11-20T00:01:00Z = 1795132860)
            os.utime(self.binary, (1_795_132_861, 1_795_132_861))
            ev = self.launch()
            self.assertEqual(ev.reason, "evidence_stale")
            self.assertTrue(any("modified after its last probe" in e for e in ev.errors), ev.errors)
            data = evidence.apply_to_probe(self.probe_dict(), self.adapter, ev)
            self.assertEqual(data["capabilities"]["headless"]["verification"], "stale")
            self.add_probe(self.VERSION, at="2026-11-20T00:03:00.000Z")
            self.assertTrue(self.launch().ok)

    def probe_dict(self) -> Dict[str, Any]:
        return ShowRowsTest.probe_dict(self)

    def test_never_executes_and_non_vendor(self) -> None:
        """ACC: N19 - evaluation never starts a process (Popen patched to raise); non-vendor kinds keep the class attribute; launch_allowed works by id; AdapterUnsupported carries details.reason."""
        self.add_probe(self.VERSION)
        self.write_results(self.all_pass())
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")), \
                mock.patch.object(os, "execv", side_effect=AssertionError("exec")):
            self.assertTrue(self.launch().ok)
            data = evidence.apply_to_probe({"status": "partially_verified", "capabilities": {}}, self.adapter, self.launch())
            self.assertTrue(data["launch_enabled"])
            none = get_adapter("none", testing_mode=True)
            self.assertEqual(evidence.effective_launch(self.conn, none, True, env=self.env).ok, bool(none.launch_enabled))
            fake = get_adapter("fake", testing_mode=True)
            self.assertEqual(evidence.effective_launch(self.conn, fake, True).ok, bool(fake.launch_enabled))
            self.assertEqual(evidence.launch_allowed(self.conn, "droid", True, self.binary).reason, "no_smoke_evidence")
            # Kiro r5 finding 2: the result's truthiness is the verdict
            self.assertFalse(bool(evidence.launch_allowed(self.conn, "droid", True, self.binary)))
            self.assertTrue(bool(self.launch()))
        self.assertFalse(DroidAdapter.launch_enabled)  # the class attribute stays False
        err = AdapterUnsupported("x", milestone="M3", reason="evidence_stale")
        self.assertEqual(err.details, {"milestone": "M3", "reason": "evidence_stale"})
        self.assertEqual(AdapterUnsupported("x", milestone="M3").details, {"milestone": "M3"})


class ShowRowsTest(EvidenceBase):
    def probe_dict(self) -> Dict[str, Any]:
        caps = {n: c.to_dict() for n, c in self.adapter.capabilities.items()}
        return {"adapter": "droid", "version": self.VERSION, "status": "partially_verified", "launch_enabled": False,
                "launch_milestone": "M3", "capabilities": caps}

    def test_capability_rows_status_and_launch_fields(self) -> None:
        """ACC: N20, N12 - smoke rows from passing mapped cases at the probed version (verified_at/version/results_file), verified only when every supported:"yes" capability is smoke; version mismatch -> stale rows; launch_enabled/milestone/reason printed."""
        self.add_probe(self.VERSION)
        path = self.write_results(self.all_pass())
        data = evidence.apply_to_probe(self.probe_dict(), self.adapter, self.launch())
        caps = data["capabilities"]
        for name in ("headless", "stream_output", "model_selection", "reasoning_effort", "resume"):
            self.assertEqual(caps[name]["verification"], "smoke", name)
            self.assertEqual(caps[name]["verified_version"], self.VERSION)
            self.assertEqual(caps[name]["verified_at"], "2026-11-20T01:02:03Z")
            self.assertEqual(caps[name]["results_file"], path)
        self.assertEqual(data["status"], "verified")
        self.assertEqual((data["launch_enabled"], data["launch_milestone"], data["launch_reason"]), (True, None, None))
        # S-X-4 fails: model_selection keeps its class value, status partially_verified
        cases = self.all_pass()
        cases[2] = self.case("S-X-4", status="fail", with_fixture=False)
        self.write_results(cases)
        data = evidence.apply_to_probe(self.probe_dict(), self.adapter, self.launch())
        self.assertEqual(data["capabilities"]["model_selection"]["verification"], "help")
        self.assertIsNone(data["capabilities"]["model_selection"]["results_file"])
        self.assertEqual(data["status"], "partially_verified")
        self.assertTrue(data["launch_enabled"])
        # probed version differs -> stale rows, launch off with reason
        self.add_probe("1.6.0", at="2026-11-20T00:00:09Z")
        data = evidence.apply_to_probe(dict(self.probe_dict(), version="1.6.0"), self.adapter, self.launch())
        self.assertEqual(data["capabilities"]["headless"]["verification"], "stale")
        self.assertEqual(data["capabilities"]["headless"]["verified_version"], self.VERSION)
        self.assertEqual(data["status"], "partially_verified")
        self.assertEqual((data["launch_enabled"], data["launch_milestone"], data["launch_reason"]),
                         (False, "M3", "evidence_stale"))
        # no evidence: class values, null provenance
        data = evidence.apply_to_probe(self.probe_dict(), self.adapter, self.launch(env={}))
        self.assertEqual(data["capabilities"]["headless"]["verification"], "help")
        self.assertIsNone(data["capabilities"]["headless"]["verified_at"])
        self.assertEqual((data["launch_enabled"], data["launch_reason"]), (False, "no_smoke_evidence"))
        self.assertIsNone(data["results_file"])
        # not_installed is unchanged
        for status in ("not_installed", "error", "unverified"):
            data = evidence.apply_to_probe(dict(self.probe_dict(), status=status), self.adapter, self.launch())
            self.assertEqual(data["status"], status)


if __name__ == "__main__":
    unittest.main()
