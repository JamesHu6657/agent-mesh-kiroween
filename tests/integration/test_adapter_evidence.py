"""Integration: evidence-driven launch through the daemon (m3-r5; ACC: N19, N20, N23).

One testing-mode daemon with tests.stubs.create_test_stubs on PATH (the droid stub reports
1.3.0, so the probe row comes from the live probe; nothing is seeded) and
MESH_TEST_SMOKE_RESULTS_DIR (meshd's env only) pointing at a /tmp/mh-* staging tree built from
copies of real fixtures plus sidecars.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

import mesh.constants as c
from mesh.adapters import evidence, smoke_schema
from tests import tierlib
from tests.helpers import MeshHarness
from tests.integration import test_tasks as tt
from tests.stubs import create_test_stubs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_INIT = os.path.join(REPO_ROOT, "tests", "fixtures", "real", "droid", "1.3.0", "exec-init-event-opus55-high.json")
STUB_VERSION = "1.3.0"


def build_droid_evidence(stage: str, version: str = STUB_VERSION, date: str = "2026-11-20",
                         failing: tuple = ()) -> str:
    """Staging tree ``<stage>/results`` + ``<stage>/droid/<version>/`` with S-X-1/2/4/5/6 pass
    (``failing`` ones fail), results named ``droid-<version>-<date>.json``."""
    with open(REAL_INIT, "rb") as f:
        data = f.read()
    cases = []
    for cid in ("S-X-1", "S-X-2", "S-X-4", "S-X-5", "S-X-6"):
        name = f"smoke-{cid}-{date}-stdout.jsonl"
        tierlib.write_capture(stage, f"droid/{version}/{name}", data, ["droid", "exec"])
        bad = cid in failing
        cases.append({"id": cid, "status": "fail" if bad else "pass", "reason": "model_mismatch" if bad else None,
                      "rc": 0, "duration_s": 1.0,
                      "fixture": smoke_schema.fixture_rel("droid", version, name),
                      "fixture_sha256": tierlib.sha256_bytes(data), "observation": None,
                      "evaluated_from": None, "carried_from": None})
    doc = {"schema": smoke_schema.SCHEMA, "adapter": "droid", "binary": "/usr/local/bin/droid", "version": version,
           "captured_at_utc": f"{date}T01:02:03Z", "mesh_commit": "abc1234", "model": "claude-sonnet-5-5",
           "effort": "low", "profile": "readonly", "timeout_s": 180, "consent_ref": "James 2026-11-09 12:44",
           "cases": cases}
    tierlib.write_capture(stage, f"results/droid-{version}-{date}.json", json.dumps(doc).encode(), ["test"])
    return os.path.join(stage, "results")


class TestAdapterEvidence(unittest.TestCase):
    def setUp(self) -> None:
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)
        self.tmp = tempfile.mkdtemp(prefix="mh-ev-int-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs_dir = os.path.join(self.tmp, "stubs")
        os.makedirs(self.stubs_dir)
        create_test_stubs(self.stubs_dir, droid_version=STUB_VERSION)
        # N20 "two files": an older-dated file (S-X-4 fail) next to the newer one (all pass)
        build_droid_evidence(os.path.join(self.tmp, "S"), date="2026-11-19", failing=("S-X-4",))
        self.results = build_droid_evidence(os.path.join(self.tmp, "S"))

    def tearDown(self) -> None:
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)

    def project(self, h: MeshHarness):
        lead = tt.create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
        plain = tt.create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
        rc, _ = h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)
        rc, resp = h.cli("intake", "submit", "--title", "P", "--goal", "g", "--criterion", "c1", token=plain)
        self.assertEqual(rc, 0, resp)
        return lead, resp["data"]["id"]

    def runs_count(self, h: MeshHarness) -> int:
        conn = h.db()
        try:
            return conn.execute("SELECT COUNT(*) FROM runs;").fetchone()[0]
        finally:
            conn.close()

    def test_show_and_gate_with_and_without_evidence(self) -> None:
        """ACC: N19, N20 - testing-mode daemon with the override: `adapter show droid` has smoke rows and launch_enabled:true; `task create --assignee <droid agent> --dry-run` -> rc 0 would, no run, tripwire empty. Without the override -> rc 13 ADAPTER_UNSUPPORTED reason no_smoke_evidence."""
        h = MeshHarness(init_db=True, start_daemon=False, extra_path=self.stubs_dir)
        try:
            h.start_daemon(extra_env={evidence.OVERRIDE_ENV: self.results})
            rc, resp = h.cli("adapter", "show", "droid", user=True)
            self.assertEqual(rc, 0, resp)
            data = resp["data"]
            self.assertEqual(data["version"], STUB_VERSION)
            self.assertEqual(data["capabilities"]["headless"]["verification"], "smoke")
            self.assertEqual(data["capabilities"]["stream_output"]["verified_version"], STUB_VERSION)
            self.assertEqual(data["capabilities"]["headless"]["results_file"],
                             os.path.join(self.results, "droid-1.3.0-2026-11-20.json"))
            # Q4: the newer of the two files decides (Kiro r5 finding 5)
            self.assertEqual(sorted(n for n in os.listdir(self.results) if n.endswith(".json") and ".meta" not in n),
                             ["droid-1.3.0-2026-11-19.json", "droid-1.3.0-2026-11-20.json"])
            self.assertEqual(data["results_file"], os.path.join(self.results, "droid-1.3.0-2026-11-20.json"))
            self.assertEqual(data["capabilities"]["headless"]["verified_at"], "2026-11-20T01:02:03Z")
            self.assertEqual(data["capabilities"]["model_selection"]["verification"], "smoke")
            self.assertTrue(data["launch_enabled"])
            self.assertIsNone(data["launch_reason"])
            rc, resp = h.cli("adapter", "list", user=True)
            self.assertEqual(rc, 0, resp)
            droid = next(a for a in resp["data"]["adapters"] if a["id"] == "droid")
            self.assertTrue(droid["launch_enabled"])
            agy = next(a for a in resp["data"]["adapters"] if a["id"] == "agy")
            self.assertEqual((agy["launch_enabled"], agy["launch_milestone"], agy["launch_reason"]),
                             (False, "M3", "no_smoke_evidence"))
            lead, project = self.project(h)
            tt.create_agent(h, "ag_droid", adapter="droid", delivery="launch", default_profile="readonly")
            rc, resp = h.cli("task", "create", "--project", project, "--title", "T", "--spec", "s",
                             "--assignee", "ag_droid", "--dry-run", token=lead)
            self.assertEqual(rc, 0, resp)
            self.assertIn("would", resp["data"])
            self.assertEqual(self.runs_count(h), 0)
            h.assert_tripwire_empty()
            # Same home, daemon restarted without the override: no evidence in testing mode.
            h.stop_daemon()
            h.start_daemon()
            rc, resp = h.cli("task", "create", "--project", project, "--title", "T2", "--spec", "s",
                             "--assignee", "ag_droid", token=lead)
            self.assertEqual(rc, c.EXIT_ADAPTER_UNAVAILABLE, resp)
            self.assertEqual(resp["error"]["code"], c.ADAPTER_UNSUPPORTED)
            self.assertEqual(resp["error"]["details"], {"milestone": "M3", "reason": "no_smoke_evidence"})
            rc, resp = h.cli("adapter", "show", "droid", user=True)
            self.assertEqual((resp["data"]["launch_enabled"], resp["data"]["launch_reason"]), (False, "no_smoke_evidence"))
            self.assertEqual(resp["data"]["capabilities"]["headless"]["verification"], "help")
            self.assertEqual(self.runs_count(h), 0)
            h.assert_tripwire_empty()
        finally:
            h.teardown()

    def test_retry_and_reject_gated(self) -> None:
        """ACC: N19 - requeue paths outside AG are gated too (Kiro r5 finding 1): a failed droid task -> `task retry` rc 13 {milestone M3, reason no_smoke_evidence}, still failed; a submitted one -> `task reject` rc 13, still submitted; no run row, tripwire empty."""
        h = MeshHarness(init_db=True, start_daemon=False, extra_path=self.stubs_dir)
        try:
            h.start_daemon()  # testing mode, no override: no evidence
            lead, project = self.project(h)
            tt.create_agent(h, "ag_droid", adapter="droid", delivery="launch", default_profile="readonly")
            ids = []
            for title in ("R", "J"):
                rc, resp = h.cli("task", "create", "--project", project, "--title", title, "--spec", "s", token=lead)
                self.assertEqual(rc, 0, resp)
                ids.append(resp["data"]["id"])
            conn = h.db()
            try:
                agent_id = conn.execute("SELECT id FROM principals WHERE name = 'ag_droid';").fetchone()[0]
            finally:
                conn.close()
            w = sqlite3.connect(os.path.join(h.home, "mesh.db"))
            try:
                w.execute("UPDATE tasks SET status = 'failed', attempt = 1, assignee_agent_id = ? WHERE id = ?;", (agent_id, ids[0]))
                w.execute("UPDATE tasks SET status = 'submitted', attempt = 1, assignee_agent_id = ? WHERE id = ?;", (agent_id, ids[1]))
                w.commit()
            finally:
                w.close()
            unsupported = {"milestone": "M3", "reason": "no_smoke_evidence"}
            rc, resp = h.cli("task", "retry", ids[0], token=lead)
            self.assertEqual(rc, c.EXIT_ADAPTER_UNAVAILABLE, resp)
            self.assertEqual((resp["error"]["code"], resp["error"]["details"]), (c.ADAPTER_UNSUPPORTED, unsupported))
            rc, resp = h.cli("task", "reject", ids[1], "--reason", "redo", token=lead)
            self.assertEqual(rc, c.EXIT_ADAPTER_UNAVAILABLE, resp)
            self.assertEqual((resp["error"]["code"], resp["error"]["details"]), (c.ADAPTER_UNSUPPORTED, unsupported))
            conn = h.db()
            try:
                st = dict(conn.execute("SELECT id, status FROM tasks WHERE id IN (?, ?);", tuple(ids)).fetchall())
            finally:
                conn.close()
            self.assertEqual(st, {ids[0]: "failed", ids[1]: "submitted"})
            self.assertEqual(self.runs_count(h), 0)
            h.assert_tripwire_empty()
            # Undo the seeded states (they have no events) so teardown's I4 check holds.
            w = sqlite3.connect(os.path.join(h.home, "mesh.db"))
            try:
                w.execute("UPDATE tasks SET status = 'created', attempt = 0, assignee_agent_id = NULL WHERE id IN (?, ?);", tuple(ids))
                w.commit()
            finally:
                w.close()
        finally:
            h.teardown()

    def test_override_ignored_outside_testing_mode(self) -> None:
        """ACC: N19 - a non-testing daemon ignores MESH_TEST_SMOKE_RESULTS_DIR: droid's launch fields equal effective_launch over the committed results dir, never the override's."""
        h = MeshHarness(init_db=False, start_daemon=False, extra_path=self.stubs_dir)
        try:
            # m3-test-kdf: production-KDF home for the non-testing meshd.
            h.cli("init", user=True, extra_env={"MESH_TESTING": "0"})
            env = h.get_env()
            env.pop("MESH_TESTING", None)
            env[evidence.OVERRIDE_ENV] = self.results
            h.daemon_proc = subprocess.Popen([h.bin_mesh, "--home", h.home, "daemon", "run"], env=env,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            h.wait_for(lambda: h.cli("status")[0] == 0, timeout=5.0)
            rc, resp = h.cli("adapter", "show", "droid", user=True)
            self.assertEqual(rc, 0, resp)
            data = resp["data"]
            self.assertEqual(data["version"], STUB_VERSION)
            conn = h.db()
            try:
                expected = evidence.effective_launch(conn, evidence_adapter("droid"), False, data["binary_path"], env={})
            finally:
                conn.close()
            self.assertEqual(data["launch_enabled"], expected.ok)
            self.assertEqual(data["launch_reason"], expected.reason)
            self.assertNotEqual(expected.results_path and os.path.dirname(expected.results_path), self.results)
            self.assertNotEqual(data["capabilities"]["headless"]["results_file"],
                                os.path.join(self.results, "droid-1.3.0-2026-11-20.json"))
            h.assert_tripwire_empty()
        finally:
            h.teardown()


def evidence_adapter(adapter_id: str):
    from mesh.adapters.registry import get_adapter

    return get_adapter(adapter_id, testing_mode=False)


if __name__ == "__main__":
    unittest.main()
