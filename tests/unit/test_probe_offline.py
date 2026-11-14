"""Offline tests of the probe tier logic (m3-r1; ACC: N10, N22, N21).

Stub binaries live under /tmp/mh-* and print committed fixtures (tests/stubs.py). The probe
is called with ``binary_path=<stub>``; nothing resolves through PATH and no MESH_PROBE*
variable is written to os.environ.
"""

import json
import os
import shutil
import tempfile
import unittest

from mesh.adapters.droid import DroidAdapter
from mesh.adapters import helpdiff
from tests import tierlib
from tests.probe import test_probe as tier
from tests.stubs import FIXTURES_ROOT, create_test_stubs, make_executable

DROID_FX = os.path.join(FIXTURES_ROOT, "droid", "1.3.0")


def _write_droid_stub(path: str, version: str, help_text: str, exec_help_text: str) -> str:
    with open(path + ".help", "w", encoding="utf-8") as f:
        f.write(help_text)
    with open(path + ".exec-help", "w", encoding="utf-8") as f:
        f.write(exec_help_text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if args == ["--version"]:
    print("droid {version}")
elif args == ["--help"]:
    sys.stdout.write(open({path + '.help'!r}, encoding="utf-8").read())
elif args == ["exec", "--help"]:
    sys.stdout.write(open({path + '.exec-help'!r}, encoding="utf-8").read())
else:
    sys.exit(99)
""")
    make_executable(path)
    return path


class ProbeOfflineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-probe-off-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.assertTrue(self.tmp.startswith("/tmp/mh-"))
        self.capture = os.path.join(self.tmp, "capture")
        with open(os.path.join(DROID_FX, "help.txt"), encoding="utf-8") as f:
            self.help = f.read()
        with open(os.path.join(DROID_FX, "exec-help.txt"), encoding="utf-8") as f:
            self.exec_help = f.read()

    def probe(self, stub: str):
        self.assertTrue(stub.startswith("/tmp/mh-") and os.path.isabs(stub), stub)
        p = DroidAdapter().probe(binary_path=stub)
        self.assertEqual(p.binary_path, stub)
        return p

    def test_no_diff_passes_and_captures_nothing(self) -> None:
        """ACC: N10, N22 - a stub printing the committed 1.3.0 help: shas match, empty diff, no missing flag, nothing captured."""
        stubs = create_test_stubs(os.path.join(self.tmp, "stubs"), tripwire_file=os.path.join(self.tmp, "trip.log"),
                                  droid_version="1.3.0")
        stub = os.path.join(stubs, "droid")
        p = self.probe(stub)
        self.assertEqual(p.version, "1.3.0")
        self.assertIsNone(p.flag_diff)
        report = tier.probe_report("droid", stub, p.version, capture_dir=self.capture)
        tier.check_report(report)
        self.assertTrue(all(f["matches_planned"] for f in report["files"].values()), report["files"])
        self.assertEqual(report["diff_vs_planned"], {"help.txt": {"added": [], "removed": []},
                                                     "exec-help.txt": {"added": [], "removed": []}})
        self.assertEqual(report["missing_emitted_flags"], [])
        self.assertFalse(report["needs_capture"])
        self.assertEqual(report["captured"], [])
        self.assertFalse(os.path.exists(self.capture))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "trip.log")))

    def test_added_flag_is_reported_and_captured(self) -> None:
        """ACC: N22, N21 - a new version with an added flag: the diff lists it, the check passes, live help is captured with sidecars under <capture>/droid/<v>/."""
        stub = _write_droid_stub(os.path.join(self.tmp, "droid"), "9.9.0", self.help,
                                 self.exec_help + "\n  --brand-new-flag <x>  Added in 9.9.0\n")
        p = self.probe(stub)
        self.assertEqual(p.version, "9.9.0")
        report = tier.probe_report("droid", stub, p.version, capture_dir=self.capture)
        print(json.dumps({"diff_vs_planned": report["diff_vs_planned"]}, sort_keys=True))
        tier.check_report(report)
        self.assertEqual(report["diff_vs_planned"]["exec-help.txt"], {"added": ["--brand-new-flag"], "removed": []})
        self.assertEqual(report["diff_vs_planned"]["help.txt"], {"added": [], "removed": []})
        self.assertFalse(report["version_dir_exists"])
        self.assertTrue(report["needs_capture"])
        expected = [os.path.join(self.capture, "droid", "9.9.0", n) for n in ("help.txt", "exec-help.txt")]
        self.assertEqual(sorted(report["captured"]), sorted(expected))
        for path, cmd in zip(expected, (["droid", "--help"], ["droid", "exec", "--help"])):
            meta = tierlib.read_sidecar(path)
            self.assertEqual(meta["command"], cmd)
        with open(expected[1], encoding="utf-8") as f:
            self.assertIn("--brand-new-flag", f.read())
        # The staging tree is promotable (into a temp repo, never the checkout).
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "tests", "fixtures", "real"))
        written = tierlib.promote(self.capture, repo)
        self.assertEqual(len(written), 2)

    def test_same_version_drift_captures_nothing(self) -> None:
        """ACC: N21, N22 - a stub claiming 1.3.0 with changed exec help: reported as same_version_drift, nothing captured (promote could never accept it); the check still passes."""
        stub = _write_droid_stub(os.path.join(self.tmp, "droid"), "1.3.0", self.help,
                                 self.exec_help + "\n  --drifted-flag  same version, new text\n")
        p = self.probe(stub)
        self.assertEqual(p.version, "1.3.0")
        report = tier.probe_report("droid", stub, p.version, capture_dir=self.capture)
        tier.check_report(report)
        self.assertTrue(report["version_dir_exists"])
        self.assertEqual(report["same_version_drift"], ["exec-help.txt"])
        self.assertFalse(report["needs_capture"])
        self.assertEqual(report["captured"], [])
        self.assertFalse(os.path.exists(self.capture))
        self.assertEqual(report["diff_vs_planned"]["exec-help.txt"], {"added": ["--drifted-flag"], "removed": []})

    def test_removed_emitted_flag_fails(self) -> None:
        """ACC: N10, N22 - live help without a flag build() emits (--log-group-id) fails the check and is named."""
        self.assertIn("--log-group-id", helpdiff.emitted_flags(DroidAdapter()))
        strip = lambda t: "".join(l for l in t.splitlines(keepends=True) if "--log-group-id" not in l)
        stub = _write_droid_stub(os.path.join(self.tmp, "droid"), "9.9.0", strip(self.help), strip(self.exec_help))
        p = self.probe(stub)
        self.assertEqual(p.flag_diff, {"match": False, "missing_flags": ["--log-group-id"]})
        report = tier.probe_report("droid", stub, p.version)
        self.assertEqual(report["missing_emitted_flags"], ["--log-group-id"])
        self.assertIn("--log-group-id", report["diff_vs_planned"]["exec-help.txt"]["removed"])
        self.assertEqual(report["captured"], [])
        with self.assertRaises(AssertionError) as cm:
            tier.check_report(report)
        self.assertIn("--log-group-id", str(cm.exception))

    def test_shim_free_daemon_records_the_probe_row(self) -> None:
        """ACC: N10, N22 - the tier's daemon path offline: a use_shims=False harness (no tests/shims on PATH, no MESH_TESTING) probes a /tmp/mh-* stub through the real CLI and records droid 9.9.0 in adapter_probes."""
        from tests.helpers import MeshHarness

        stub = _write_droid_stub(os.path.join(self.tmp, "droid"), "9.9.0", self.help, self.exec_help)
        # Tripwire stubs first on PATH so no real vendor binary can be reached (Kiro r1 minor 2).
        trip = os.path.join(self.tmp, "daemon-trip.log")
        guard = create_test_stubs(os.path.join(self.tmp, "guard"), tripwire_file=trip)
        self.assertTrue(guard.startswith("/tmp/mh-") and trip.startswith("/tmp/mh-"))
        h = MeshHarness(init_db=True, start_daemon=False, use_shims=False, extra_path=guard)
        try:
            env = h.get_env()
            shims = os.path.realpath(os.path.join(h.repo_root, "tests", "shims"))
            self.assertNotIn(shims, [os.path.realpath(p) for p in env["PATH"].split(os.pathsep) if p])
            self.assertNotIn("MESH_TESTING", env)
            self.assertNotIn(shims, [os.path.realpath(p) for p in MeshHarness.get_env(h).get("PATH", "").split(os.pathsep) if p])
            h.start_daemon()
            rc, resp = h.cli("adapter", "probe", "droid", "--binary", stub, user=True)
            self.assertEqual(rc, 0, resp)
            self.assertEqual(resp["data"]["version"], "9.9.0")
            self.assertEqual(resp["data"]["binary_path"], stub)
            conn = h.db()
            try:
                row = conn.execute("SELECT version, binary_path FROM adapter_probes WHERE adapter = 'droid' "
                                   "ORDER BY probed_at DESC LIMIT 1;").fetchone()
            finally:
                conn.close()
            self.assertEqual(tuple(row), ("9.9.0", stub))
            # `adapter probe` under a non-testing daemon has no fake adapter (testing mode is off).
            rc, resp = h.cli("adapter", "probe", "fake", user=True)
            self.assertNotEqual(rc, 0, resp)
            self.assertEqual(env["PATH"].split(os.pathsep)[0], guard)
        finally:
            h.teardown()
        self.assertFalse(os.path.exists(trip), "a vendor stub on the guard PATH was invoked")
        # The tier's own assertion path (daemon_probe, used verbatim by ProbeTier): it compared a
        # sqlite3.Row with a tuple and failed C1 for all four adapters (2026-11-09).
        self.assertEqual(tier.daemon_probe(self, "droid", stub, extra_path=guard), "9.9.0")
        self.assertFalse(os.path.exists(trip), "a vendor stub on the guard PATH was invoked")
        default = MeshHarness(init_db=False)
        try:
            env = default.get_env()
            self.assertEqual(env["MESH_TESTING"], "1")
            self.assertEqual(env["PATH"].split(os.pathsep)[0], os.path.join(default.repo_root, "tests", "shims"))
        finally:
            default.teardown()

    def test_relative_binary_refused(self) -> None:
        """ACC: N10 - the tier never resolves a bare binary name."""
        with self.assertRaises(ValueError):
            tier.probe_report("droid", "droid", "9.9.0")

    def test_tier_is_skipped_without_opt_in(self) -> None:
        """ACC: N10 - without MESH_PROBE=1 the tier class is skipped (this suite never sets it)."""
        self.assertNotEqual(os.environ.get("MESH_PROBE"), "1")
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(tier.ProbeTier).run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.failures + result.errors, [])


if __name__ == "__main__":
    unittest.main()
