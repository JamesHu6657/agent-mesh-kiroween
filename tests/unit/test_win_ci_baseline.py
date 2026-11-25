"""Baseline cross-platform and Windows CI invariants (Phase 0 / PR-0)."""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REAL_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "real"
GITATTRIBUTES_PATH = REPO_ROOT / ".gitattributes"


class TestWinCiBaseline(unittest.TestCase):
    """Cross-platform baseline checks required on both Linux and Windows CI."""

    def test_hashlib_scrypt_production_params(self) -> None:
        """hashlib.scrypt must support production KDF params (n=32768, r=8, p=1, maxmem=64MiB)."""
        from mesh.core import auth

        self.assertEqual(auth.SCRYPT_N, 32768)
        self.assertEqual(auth.SCRYPT_R, 8)
        self.assertEqual(auth.SCRYPT_P, 1)
        self.assertEqual(auth.SCRYPT_DKLEN, 64)
        self.assertGreaterEqual(auth.SCRYPT_MAXMEM, 64 * 1024 * 1024)

        d1 = hashlib.scrypt(
            b"win-ci-baseline-passphrase",
            salt=b"0123456789abcdef",
            n=32768,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
            dklen=64,
        )
        d2 = hashlib.scrypt(
            b"win-ci-baseline-passphrase",
            salt=b"0123456789abcdef",
            n=32768,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
            dklen=64,
        )
        d3 = hashlib.scrypt(
            b"win-ci-baseline-passphrase",
            salt=b"fedcba9876543210",
            n=32768,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
            dklen=64,
        )
        self.assertEqual(len(d1), 64)
        self.assertEqual(d1, d2)
        self.assertNotEqual(d1, d3)

    def test_gitattributes_enforces_lf_and_cmd_crlf(self) -> None:
        """.gitattributes must enforce LF default and CRLF for *.cmd shims."""
        self.assertTrue(GITATTRIBUTES_PATH.is_file(), f"Missing {GITATTRIBUTES_PATH}")
        lines = [
            ln.strip()
            for ln in GITATTRIBUTES_PATH.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        self.assertIn("* text=auto eol=lf", lines)
        self.assertIn("*.cmd text eol=crlf", lines)
        self.assertIn("tests/fixtures/** -text", lines)
        self.assertIn("tests/smoke/results/** -text", lines)

    def test_real_fixtures_preserve_lf_bytes(self) -> None:
        """All files under tests/fixtures/real/** must keep exact LF line endings without CR."""
        self.assertTrue(REAL_FIXTURES_DIR.is_dir(), f"Missing {REAL_FIXTURES_DIR}")
        files = sorted(p for p in REAL_FIXTURES_DIR.rglob("*") if p.is_file())
        self.assertGreater(len(files), 0)
        for path in files:
            data = path.read_bytes()
            rel = path.relative_to(REPO_ROOT)
            self.assertNotIn(b"\r\n", data, f"CRLF detected in fixture {rel}")
            self.assertNotIn(b"\r", data, f"CR byte detected in fixture {rel}")

    def test_win_skips_removed(self) -> None:
        """Phase 8: centralized tests/win_skips.py is removed in favor of @unittest.skipUnless decorators."""
        self.assertFalse((REPO_ROOT / "tests" / "win_skips.py").exists())

    def test_run_tier_win32_helpers(self) -> None:
        """run_tier Windows helpers handle missing getuid and win32 tempdir."""
        from tests import run_tier

        # _user_id_tag fallback when os.getuid is absent
        fake_os = type("_FakeOs", (), {"environ": {"USERNAME": "Runner Admin/1"}})()
        with mock.patch.object(run_tier, "os", fake_os):
            self.assertEqual(run_tier._user_id_tag(), "Runner_Admin_1")

        # _default_durations_path preserves /tmp on Linux and uses gettempdir on win32
        linux_path = run_tier._default_durations_path("tests/unit", platform="linux")
        self.assertTrue(linux_path.startswith("/tmp/mesh-test-durations-"))
        with mock.patch.object(run_tier.tempfile, "gettempdir", return_value=r"C:\Temp"):
            win_path = run_tier._default_durations_path("tests/unit", platform="win32")
        self.assertEqual(os.path.dirname(win_path), r"C:\Temp")
        self.assertIn("mesh-test-durations-", os.path.basename(win_path))

    def test_ci_workflow_present_and_configured(self) -> None:
        """CI workflow must define linux (bash scripts/test.sh) and windows (python tests/run_tier.py tests/unit; integration+recovery run on a Windows VM before merges) jobs."""
        ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(ci_path.is_file(), f"Missing {ci_path}")
        text = ci_path.read_text(encoding="utf-8")
        for needle in (
            "ubuntu-latest",
            "windows-latest",
            "actions/setup-python@v5",
            'python-version: "3.12"',
            "bash scripts/test.sh",
            "core.longpaths true",
            "PYTHONUTF8",
            "MESH_TEST_JOBS",
            "hashlib.scrypt",
            "python tests/run_tier.py tests/unit",
        ):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
