"""Integration tests for adapter CLI commands: list, show, probe, models.

ACC: N1, N2, N3, N7, N8, N10
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import mesh.constants as c
from mesh.adapters.base import EXPECTED_HELP_HASHES
from tests.helpers import MeshHarness
from tests.stubs import (
    create_failing_stub,
    create_garbage_stub,
    create_hanging_stub,
    create_missing_flag_stub,
    create_modified_help_stub,
    create_stale_stub,
    create_stderr_help_stub,
    create_test_stubs,
)


class TestAdaptersIntegration(unittest.TestCase):
    """Integration test suite for CLI adapter inspection commands."""

    def setUp(self):
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)

    def tearDown(self):
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def test_adapter_list_without_and_with_testing_mode(self) -> None:
        """ACC: N1, N3 - adapter list without MESH_TESTING (fake absent) and with MESH_TESTING (fake present)."""
        # 1. Daemon without MESH_TESTING
        harness_no_test = MeshHarness(init_db=False, start_daemon=False, extra_path=self.stubs_dir)
        try:
            # m3-test-kdf: a non-testing meshd refuses a testing (cheap KDF) home, so this
            # home is created outside testing mode (production KDF).
            harness_no_test.cli("init", user=True, extra_env={"MESH_TESTING": "0"})
            # Start daemon with MESH_TESTING unset
            env_no_test = harness_no_test.get_env()
            env_no_test.pop("MESH_TESTING", None)
            cmd = [harness_no_test.bin_mesh, "--home", harness_no_test.home, "daemon", "run"]
            proc = subprocess.Popen(cmd, env=env_no_test, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            harness_no_test.daemon_proc = proc

            harness_no_test.wait_for(lambda: harness_no_test.cli("status")[0] == 0, timeout=5.0)

            rc, resp = harness_no_test.cli("adapter", "list", user=True)
            self.assertEqual(rc, 0)
            self.assertTrue(resp["ok"])
            adapters = resp["data"]["adapters"]
            ids = [a["id"] for a in adapters]

            # droid, agy, kiro, vibe, hermes, openclaw (+ none)
            expected_ids = ["droid", "agy", "kiro", "vibe", "hermes", "openclaw", "none"]
            self.assertEqual(ids, expected_ids)
            self.assertNotIn("fake", ids)

            # Check droid/agy/kiro/vibe partially_verified with launch_enabled:false, launch_milestone:"M3"
            # ACC: N1 - versions come from the probe (droid 1.6.0, agy 1.5.2, kiro 3.1.0, vibe 3.1.0)
            expected_versions = {
                "droid": "1.6.0",
                "agy": "1.5.2",
                "kiro": "3.1.0",
                "vibe": "3.1.0",
                "hermes": None,
                "openclaw": None,
                "none": None,
            }
            # ACC: N1 superseded by N23 (m3-r5): launch_enabled is the effective value over the
            # results directory this non-testing daemon reads (committed tests/smoke/results).
            from mesh.adapters.evidence import LAUNCH_HOLDS, effective_launch
            from mesh.adapters.registry import get_adapter

            conn_n1 = harness_no_test.db()
            try:
                effective = {}
                for aid in ("droid", "agy", "kiro", "vibe"):
                    row = conn_n1.execute("SELECT binary_path FROM adapter_probes WHERE adapter = ? "
                                          "ORDER BY probed_at DESC LIMIT 1;", (aid,)).fetchone()
                    effective[aid] = effective_launch(conn_n1, get_adapter(aid, testing_mode=False), False,
                                                      tuple(row)[0] if row else None, env={}).ok
            finally:
                conn_n1.close()
            for a in adapters:
                aid = a["id"]
                if aid in ("droid", "agy", "kiro", "vibe"):
                    # F7: droid 1.6.0 has passing committed smoke evidence, so it is verified.
                    self.assertEqual(a["status"], "verified" if aid == "droid" else "partially_verified")
                    self.assertEqual(a["launch_enabled"], effective[aid])
                    if not a["launch_enabled"]:
                        # L11: a held adapter (kiro) reports the hold's milestone (M4), others M3.
                        if a.get("launch_reason") == "launch_held":
                            self.assertEqual(a["launch_milestone"], LAUNCH_HOLDS[aid])
                        else:
                            self.assertEqual(a["launch_milestone"], "M3")
                    if aid == "kiro":
                        # M4: kiro N13 passed and the hold is gone; committed kiro 3.1.0 evidence
                        # passes and the stub probes 3.1.0 -> launch open again.
                        self.assertTrue(a["launch_enabled"])
                        self.assertIsNone(a["launch_reason"])
                    if aid == "droid":
                        # F7 (m4-p3-cleanup): the stub probes 1.6.0, which has passing committed
                        # evidence, so droid is launch-enabled by evidence (not by a test hook).
                        self.assertTrue(a["launch_enabled"])
                    self.assertEqual(a["version"], expected_versions[aid], f"Expected {aid} version {expected_versions[aid]}, got {a['version']}")
                elif aid in ("hermes", "openclaw"):
                    self.assertEqual(a["status"], "not_installed")
                    self.assertFalse(a["launch_enabled"])
                    self.assertIsNone(a["version"])

            # ACC: N3 - Without testing mode: agent create --adapter fake -> rc 13 TEST_DOUBLE_DISABLED
            rc_fake, resp_fake = harness_no_test.cli(
                "agent", "create",
                "--name", "fake_no_test",
                "--adapter", "fake",
                "--max-concurrency", "1",
                "--allowed-model", "m-a",
                user=True,
            )
            self.assertEqual(rc_fake, c.EXIT_ADAPTER_UNAVAILABLE)
            self.assertFalse(resp_fake["ok"])
            self.assertEqual(resp_fake["error"]["code"], c.TEST_DOUBLE_DISABLED)
        finally:
            harness_no_test.teardown()

        # 2. In testing mode: adapter list includes fake with kind: "test_double", vendor: null
        harness_test = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        try:
            rc_t, resp_t = harness_test.cli("adapter", "list", user=True)
            self.assertEqual(rc_t, 0)
            adapters_t = resp_t["data"]["adapters"]
            ids_t = [a["id"] for a in adapters_t]
            self.assertIn("fake", ids_t)

            fake_info = next(a for a in adapters_t if a["id"] == "fake")
            self.assertEqual(fake_info["kind"], "test_double")
            self.assertIsNone(fake_info["vendor"])
            self.assertEqual(fake_info["note"], "not a vendor integration")
        finally:
            harness_test.teardown()

    def test_adapter_probe_and_show_not_installed(self) -> None:
        """ACC: N2 - adapter probe hermes reports not_installed; adapter show openclaw lists no flags and all capabilities unknown."""
        harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        try:
            # 1. m adapter probe hermes -> reports not_installed
            rc_probe, resp_probe = harness.cli("adapter", "probe", "hermes", user=True)
            self.assertEqual(rc_probe, 0)
            self.assertTrue(resp_probe["ok"])
            self.assertEqual(resp_probe["data"]["status"], "not_installed")
            self.assertEqual(resp_probe["data"]["adapter"], "hermes")

            # 2. m adapter show openclaw -> lists no flags and all capabilities unknown
            rc_show, resp_show = harness.cli("adapter", "show", "openclaw", user=True)
            self.assertEqual(rc_show, 0)
            self.assertTrue(resp_show["ok"])
            data = resp_show["data"]
            self.assertEqual(data["adapter"], "openclaw")
            self.assertEqual(data["flags"], [])
            for cname, cinfo in data["capabilities"].items():
                self.assertEqual(cinfo["supported"], "unknown")
        finally:
            harness.teardown()

    def test_adapter_models(self) -> None:
        """ACC: N7 - m adapter models kiro lists model ids; m adapter models vibe returns null; agy/droid return probed models."""
        harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        try:
            # 1. kiro: model ids from kiro-cli chat --list-models -f json (22 entries)
            rc_kiro, resp_kiro = harness.cli("adapter", "models", "kiro", user=True)
            self.assertEqual(rc_kiro, 0)
            self.assertTrue(resp_kiro["ok"])
            models = resp_kiro["data"]["models"]
            self.assertIsNotNone(models)
            self.assertEqual(len(models), 22)
            self.assertIn("claude-opus-5.5", models)
            self.assertIn("gpt-5.6-sol", models)

            # 2. vibe: null (no list command per §14.6 and ACC: N7)
            rc_vibe, resp_vibe = harness.cli("adapter", "models", "vibe", user=True)
            self.assertEqual(rc_vibe, 0)
            self.assertTrue(resp_vibe["ok"])
            self.assertIsNone(resp_vibe["data"]["models"])

            # 3. agy: models from agy models
            rc_agy, resp_agy = harness.cli("adapter", "models", "agy", user=True)
            self.assertEqual(rc_agy, 0)
            self.assertTrue(resp_agy["ok"])
            self.assertIn("gemini-3.8-flash-high", resp_agy["data"]["models"])

            # 4. droid: models from droid exec --help
            rc_droid, resp_droid = harness.cli("adapter", "models", "droid", user=True)
            self.assertEqual(rc_droid, 0)
            self.assertTrue(resp_droid["ok"])
            self.assertIn("claude-opus-5-5", resp_droid["data"]["models"])
        finally:
            harness.teardown()

    def test_adapter_probe_cli_live_and_stale(self) -> None:
        """Regression test driving real CLI path: bin/mesh adapter probe with daemon start."""
        harness = MeshHarness(init_db=True, start_daemon=False, extra_path=self.stubs_dir)
        try:
            # Start daemon via 'daemon start' command
            rc_start, resp_start = harness.cli("daemon", "start")
            self.assertEqual(rc_start, 0)
            self.assertTrue(resp_start["ok"])

            # Probe agy via CLI
            rc_probe, resp_probe = harness.cli("adapter", "probe", "agy", user=True)
            self.assertEqual(rc_probe, 0)
            data = resp_probe["data"]
            self.assertEqual(data["adapter"], "agy")
            self.assertEqual(data["version"], "1.5.2")
            self.assertEqual(data["status"], "partially_verified")
            self.assertEqual(data["help_sha256"], EXPECTED_HELP_HASHES["agy"])
            self.assertIn("--print", data["flags"])
            self.assertIn("gemini-3.8-flash-high", data["models"])

            # Test stale version path with custom binary
            stale_bin = create_stale_stub(self.stubs_dir, "agy_stale", version="99.0.0")
            rc_stale, resp_stale = harness.cli("adapter", "probe", "agy", "--binary", stale_bin, user=True)
            self.assertEqual(rc_stale, 0)
            data_stale = resp_stale["data"]
            self.assertEqual(data_stale["version"], "99.0.0")
            for cname, cinfo in data_stale["capabilities"].items():
                if cinfo["verification"] != "none":
                    self.assertEqual(cinfo["verification"], "stale")

            # Test flag-diff / stale help hash path
            alt_bin = create_modified_help_stub(self.stubs_dir, "agy_mod_help", version="1.5.2", help_text="Altered agy help without flags")
            rc_mod, resp_mod = harness.cli("adapter", "probe", "agy", "--binary", alt_bin, user=True)
            self.assertEqual(rc_mod, 0)
            data_mod = resp_mod["data"]
            self.assertIsNotNone(data_mod.get("flag_diff"))
            self.assertFalse(data_mod["flag_diff"]["match"])
            for cname, cinfo in data_mod["capabilities"].items():
                if cinfo["verification"] != "none":
                    self.assertEqual(cinfo["verification"], "stale")

            # Test F3 follow-up: stub printing help to stderr only (exit 0)
            stderr_bin = create_stderr_help_stub(self.stubs_dir, "agy_stderr", version="1.5.2")
            rc_err, resp_err = harness.cli("adapter", "probe", "agy", "--binary", stderr_bin, user=True)
            self.assertEqual(rc_err, 0)
            data_err = resp_err["data"]
            self.assertEqual(data_err["version"], "1.5.2")
            self.assertEqual(data_err["help_sha256"], EXPECTED_HELP_HASHES["agy"])
            # All flags present in stderr -> no false missing flags, flag_diff is None
            self.assertIsNone(data_err.get("flag_diff"))

            # Test F3 follow-up: stub whose help lacks an emitted flag -> missing_flags lists exactly it
            miss_bin = create_missing_flag_stub(
                self.stubs_dir,
                "agy_miss_flag",
                version="1.5.2",
                missing_flag="--dangerously-skip-permissions",
                to_stderr=True,
            )
            rc_miss, resp_miss = harness.cli("adapter", "probe", "agy", "--binary", miss_bin, user=True)
            self.assertEqual(rc_miss, 0)
            data_miss = resp_miss["data"]
            self.assertIsNotNone(data_miss.get("flag_diff"))
            self.assertFalse(data_miss["flag_diff"]["match"])
            self.assertEqual(data_miss["flag_diff"]["missing_flags"], ["--dangerously-skip-permissions"])

            # Test F3 follow-up: agy 1.5.3 (version != verified 1.5.2) printing help to stderr
            # Version different from verified marks capabilities stale (N8), but flag_diff has no missing flags
            v153_bin = create_stderr_help_stub(self.stubs_dir, "agy_153", version="1.5.3")
            rc_153, resp_153 = harness.cli("adapter", "probe", "agy", "--binary", v153_bin, user=True)
            self.assertEqual(rc_153, 0)
            data_153 = resp_153["data"]
            self.assertEqual(data_153["version"], "1.5.3")
            self.assertEqual(data_153["help_sha256"], EXPECTED_HELP_HASHES["agy"])
            self.assertIsNone(data_153.get("flag_diff"))
            for cname, cinfo in data_153["capabilities"].items():
                if cinfo["verification"] != "none":
                    self.assertEqual(cinfo["verification"], "stale")
        finally:
            harness.cli("daemon", "stop", user=True)
            harness.teardown()

    def test_adapter_probe_error_and_timeout(self) -> None:
        """Verify CLI adapter probe reports error status on failing or hanging binary."""
        harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        try:
            # 1. Failing binary (exit 1)
            fail_bin = create_failing_stub(self.stubs_dir, "droid_fail", exit_code=1)
            rc, resp = harness.cli("adapter", "probe", "droid", "--binary", fail_bin, user=True)
            self.assertEqual(rc, 0)
            self.assertEqual(resp["data"]["status"], "error")
            self.assertIsNone(resp["data"]["version"])
            self.assertEqual(resp["data"]["details"]["exit_code"], 1)

            # 2. Garbage unparseable version output
            garbage_bin = create_garbage_stub(self.stubs_dir, "droid_garbage", text="UNPARSEABLE")
            rc_g, resp_g = harness.cli("adapter", "probe", "droid", "--binary", garbage_bin, user=True)
            self.assertEqual(rc_g, 0)
            self.assertEqual(resp_g["data"]["status"], "unverified")
            self.assertIsNone(resp_g["data"]["version"])
        finally:
            harness.teardown()

    def test_adapter_probes_db_persistence(self) -> None:
        """Verify probe invocations persist rows into adapter_probes table in mesh.db."""
        harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        try:
            # Run adapter list
            harness.cli("adapter", "list", user=True)

            conn = harness.db()
            cur = conn.execute("SELECT adapter, version, status, help_sha256 FROM adapter_probes ORDER BY adapter;")
            rows = {r[0]: {"version": r[1], "status": r[2], "help_sha256": r[3]} for r in cur.fetchall()}
            conn.close()

            # Verify rows exist for probed adapters
            self.assertIn("droid", rows)
            self.assertEqual(rows["droid"]["version"], "1.6.0")
            self.assertEqual(rows["droid"]["status"], "partially_verified")
            self.assertEqual(rows["droid"]["help_sha256"], EXPECTED_HELP_HASHES["droid"])

            self.assertIn("agy", rows)
            self.assertEqual(rows["agy"]["version"], "1.5.2")
            self.assertEqual(rows["agy"]["status"], "partially_verified")

            self.assertIn("kiro", rows)
            self.assertEqual(rows["kiro"]["version"], "3.1.0")

            self.assertIn("vibe", rows)
            self.assertEqual(rows["vibe"]["version"], "3.1.0")
        finally:
            harness.teardown()


if __name__ == "__main__":
    unittest.main()
