"""m3-kiro-real fix round (Linear review A, MAJOR 1): which parsed outcomes turn an exit 0 into
T8 ``vendor_error``. Real fixtures are copied into /tmp run dirs; expectations are literal."""

import os
import shutil
import tempfile
import unittest
from typing import Dict

from unittest import mock

from mesh.adapters.kiro import KiroAdapter
from mesh.daemon import supervisor
from mesh.daemon.supervisor import VENDOR_FAILURE_REASON, vendor_failure_meaning

REAL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "tests", "fixtures", "real")


class VendorFailureMeaningTest(unittest.TestCase):
    def run_dir(self, files: Dict[str, str]) -> str:
        d = tempfile.mkdtemp(prefix="mh-vfm-")
        self.addCleanup(shutil.rmtree, d, True)
        for dst, src in files.items():
            path = os.path.join(d, dst)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copyfile(os.path.join(REAL, src), path)
        return d

    def test_reason_name(self) -> None:
        self.assertEqual(VENDOR_FAILURE_REASON, "vendor_error")

    def test_kiro(self) -> None:
        """kiro 3.1.0: runError naming the bearer token -> auth_failed; a successful smoke run and a
        refusal that still finished (status success) -> None."""
        self.assertEqual(vendor_failure_meaning(
            self.run_dir({"stdout.log": "kiro/3.1.0/stream-run-error-auth.jsonl"}), "kiro"), "auth_failed")
        self.assertIsNone(vendor_failure_meaning(
            self.run_dir({"stdout.log": "kiro/3.1.0/smoke-S-X-1-stdout.jsonl"}), "kiro"))
        self.assertIsNone(vendor_failure_meaning(
            self.run_dir({"stdout.log": "kiro/3.1.0/stream-shell-forbidden-refusal.jsonl"}), "kiro"))

    def test_agy(self) -> None:
        """agy 1.5.3: the network-error stream -> run_error; a denied-tool run that finished -> None;
        an empty run dir (no stdout.log) -> run_error."""
        self.assertEqual(vendor_failure_meaning(
            self.run_dir({"stdout.log": "agy/1.5.3/stream-step6-network-error.jsonl"}), "agy"), "run_error")
        self.assertIsNone(vendor_failure_meaning(
            self.run_dir({"stdout.log": "agy/1.5.3/stream-fixC-denied.jsonl"}), "agy"))
        self.assertEqual(vendor_failure_meaning(self.run_dir({}), "agy"), "run_error")

    def test_vibe(self) -> None:
        """vibe 3.1.0: export outcome terminated -> terminated; no export -> None (exit code decides)."""
        self.assertEqual(vendor_failure_meaning(self.run_dir({
            "stdout.log": "vibe/3.1.0/streaming-terminated.jsonl",
            "vibe-out/export.json": "vibe/3.1.0/export-terminated.json"}), "vibe"), "terminated")
        self.assertIsNone(vendor_failure_meaning(
            self.run_dir({"stdout.log": "vibe/3.1.0/streaming-terminated.jsonl"}), "vibe"))

    def test_droid_fake_and_unknown_unchanged(self) -> None:
        """droid never sets a meaning (its real N13 run -> None); fake -> None; an unknown adapter or
        a missing run dir -> None, so the exit code alone decides as before."""
        self.assertIsNone(vendor_failure_meaning(self.run_dir({
            "stdout.log": "droid/1.6.0/e2e-N13-run_a1c225d817a3u3i4jeczj2hino-stdout.log"}), "droid"))
        self.assertIsNone(vendor_failure_meaning(
            self.run_dir({"stdout.log": "kiro/3.1.0/stream-run-error-auth.jsonl"}), "fake"))
        self.assertIsNone(vendor_failure_meaning(
            self.run_dir({"stdout.log": "kiro/3.1.0/stream-run-error-auth.jsonl"}), "no-such-adapter"))
        self.assertIsNone(vendor_failure_meaning("/nonexistent/mh-run", "droid"))


class VendorMeaningCacheAndGitTest(unittest.TestCase):
    run_dir = VendorFailureMeaningTest.run_dir

    def setUp(self) -> None:
        supervisor._VENDOR_MEANING.clear()
        self.addCleanup(supervisor._VENDOR_MEANING.clear)

    def test_needs_submit_git_skips_vendor_failure(self) -> None:
        """PR #12 round 2 item 3: a running code task whose run exited 0 collects deliverable.git only
        without a vendor failure: kiro auth stream -> False; successful stream -> True; rc 1 -> False."""
        task = {"kind": "code", "status": "running"}
        ok_exit = {"exit_code": 0, "signal": None}
        bad = {"run_dir": self.run_dir({"stdout.log": "kiro/3.1.0/stream-run-error-auth.jsonl"}),
               "adapter": "kiro", "result_json": None}
        good = {"run_dir": self.run_dir({"stdout.log": "kiro/3.1.0/smoke-S-X-1-stdout.jsonl"}),
                "adapter": "kiro", "result_json": None}
        self.assertIs(supervisor._needs_submit_git(bad, task, ok_exit), False)
        self.assertIs(supervisor._needs_submit_git(good, task, ok_exit), True)
        self.assertIs(supervisor._needs_submit_git(good, task, {"exit_code": 1, "signal": None}), False)

    def test_one_parse_per_run_until_output_changes(self) -> None:
        """PR #12 round 2 item 4: repeated calls (one per tick) parse once; a changed stdout.log is
        parsed again; eviction drops runs that are no longer active."""
        d = self.run_dir({"stdout.log": "kiro/3.1.0/smoke-S-X-1-stdout.jsonl"})
        with mock.patch.object(KiroAdapter, "parse", autospec=True, side_effect=KiroAdapter.parse) as p:
            for _ in range(5):
                self.assertIsNone(vendor_failure_meaning(d, "kiro"))
            self.assertEqual(p.call_count, 1)
            shutil.copyfile(os.path.join(REAL, "kiro/3.1.0/stream-run-error-auth.jsonl"), os.path.join(d, "stdout.log"))
            self.assertEqual(vendor_failure_meaning(d, "kiro"), "auth_failed")
            self.assertEqual(p.call_count, 2)
        supervisor._evict_git_info({os.path.basename(d)})
        self.assertIn(d, supervisor._VENDOR_MEANING)
        supervisor._evict_git_info(set())
        self.assertNotIn(d, supervisor._VENDOR_MEANING)


if __name__ == "__main__":
    unittest.main()
