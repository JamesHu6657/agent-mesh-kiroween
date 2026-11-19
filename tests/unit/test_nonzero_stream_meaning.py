"""Opt-in switch ``nonzero_exit_uses_stream_meaning`` in supervisor._vendor_nonzero_failure.

codex and claude report auth / quota / network failures in their stdout stream and exit
non-zero, so for them (and only them) the parsed stream meaning names the failure reason.
Every other adapter keeps exit_nonzero (vibe exit 3 -> timeout), refined by stderr only.

Streams: tests/fixtures/synthetic/{codex,claude}/ (hand-written, never captured) and, for the
old adapters, real fixtures copied into /tmp run dirs. Expectations are literals.
"""

import os
import shutil
import tempfile
import unittest
from typing import Optional

from mesh.adapters.claude import ClaudeAdapter
from mesh.adapters.codex import CodexAdapter
from mesh.adapters.devin import DevinAdapter
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.kiro import KiroAdapter
from mesh.daemon import supervisor
from mesh.daemon.supervisor import _vendor_nonzero_failure

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "tests", "fixtures")
KIRO_AUTH = "real/kiro/3.1.0/stream-run-error-auth.jsonl"
DROID_RUN = "real/droid/1.6.0/e2e-N13-run_a1c225d817a3u3i4jeczj2hino-stdout.log"
KIRO_EFFORT_STDERR = "error: failed to set effort: the model does not support additional fields\n"


class NonzeroStreamMeaningTest(unittest.TestCase):
    def run_dir(self, stdout: Optional[str] = None, stderr: str = "") -> str:
        d = tempfile.mkdtemp(prefix="mh-nzm-")
        self.addCleanup(shutil.rmtree, d, True)
        if stdout:
            shutil.copyfile(os.path.join(FIXTURES, stdout), os.path.join(d, "stdout.log"))
        with open(os.path.join(d, "stderr.log"), "w", encoding="utf-8") as f:
            f.write(stderr)
        return d

    def failure(self, adapter: str, exit_code: int, stdout: Optional[str] = None, stderr: str = ""):
        return _vendor_nonzero_failure(self.run_dir(stdout, stderr), adapter, exit_code, testing_mode=True)

    def test_switch_is_opt_in(self) -> None:
        self.assertIs(CodexAdapter.nonzero_exit_uses_stream_meaning, True)
        self.assertIs(ClaudeAdapter.nonzero_exit_uses_stream_meaning, True)
        self.assertIs(DevinAdapter.nonzero_exit_uses_stream_meaning, True)
        for cls in (KiroAdapter, DroidAdapter):
            self.assertFalse(hasattr(cls, "nonzero_exit_uses_stream_meaning"), cls.__name__)

    def test_new_adapters_get_stream_meaning(self) -> None:
        cases = [
            ("codex", "synthetic/codex/synthetic_not_logged_in.jsonl", "auth_failed"),
            ("codex", "synthetic/codex/synthetic_quota.jsonl", "quota_exhausted"),
            ("codex", "synthetic/codex/synthetic_network.jsonl", "network_error"),
            ("claude", "synthetic/claude/synthetic_not_logged_in.jsonl", "auth_failed"),
            ("claude", "synthetic/claude/synthetic_quota.jsonl", "quota_exhausted"),
            ("claude", "synthetic/claude/synthetic_network.jsonl", "network_error"),
        ]
        for adapter, stream, meaning in cases:
            with self.subTest(adapter=adapter, stream=stream):
                reason, extra = self.failure(adapter, 1, stream)
                self.assertEqual(reason, meaning)
                self.assertEqual(extra, {"vendor_exit_meaning": meaning})

    def test_old_adapters_unchanged(self) -> None:
        """A kiro auth stream with exit 1 stays exit_nonzero (kiro only refines by stderr); droid
        stays exit_nonzero; vibe exit 3 stays timeout; kiro's stderr signature still applies."""
        self.assertEqual(self.failure("kiro", 1, KIRO_AUTH), ("exit_nonzero", {}))
        self.assertEqual(self.failure("droid", 1, DROID_RUN), ("exit_nonzero", {}))
        self.assertEqual(self.failure("vibe", 3)[0], "timeout")
        reason, extra = self.failure("kiro", 1, KIRO_AUTH, KIRO_EFFORT_STDERR)
        self.assertEqual(reason, "model_or_effort_rejected")
        self.assertNotIn("vendor_exit_meaning", extra)

    def test_no_meaning_keeps_exit_code(self) -> None:
        """A finished stream, a stopped run (incomplete) or no stdout at all -> exit_nonzero."""
        for adapter, stream in (("codex", "synthetic/codex/synthetic_ok.jsonl"),
                                ("codex", "synthetic/codex/synthetic_stopped.jsonl"),
                                ("claude", "synthetic/claude/synthetic_ok.jsonl"),
                                ("claude", "synthetic/claude/synthetic_stopped.jsonl"),
                                ("claude", None)):
            with self.subTest(adapter=adapter, stream=stream):
                self.assertEqual(self.failure(adapter, 143 if stream and "stopped" in stream else 1, stream),
                                 ("exit_nonzero", {}))

    def test_stderr_kept_beside_stream_meaning(self) -> None:
        reason, extra = self.failure("codex", 2, "synthetic/codex/synthetic_not_logged_in.jsonl",
                                     "error: unexpected argument '--bogus' found\n")
        self.assertEqual(reason, "auth_failed")
        self.assertEqual(extra["vendor_exit_meaning"], "auth_failed")
        self.assertEqual(extra["stderr_error"]["reason"], "cli_usage_error")
        # Without a stream meaning the stderr signature decides, as for every adapter.
        self.assertEqual(self.failure("codex", 2, None, "error: unexpected argument '--bogus' found\n")[0],
                         "cli_usage_error")

    def test_parse_error_falls_back(self) -> None:
        orig = CodexAdapter.parse

        def boom(self, run_dir):
            raise RuntimeError("parser bug")

        CodexAdapter.parse = boom
        self.addCleanup(setattr, CodexAdapter, "parse", orig)
        self.assertEqual(self.failure("codex", 1, "synthetic/codex/synthetic_quota.jsonl"), ("exit_nonzero", {}))

    def test_meaning_set(self) -> None:
        self.assertEqual(supervisor._NONZERO_STREAM_MEANINGS, frozenset({
            "auth_failed", "quota_exhausted", "rate_limited", "network_error", "run_error",
            "output_truncated", "turn_limit", "budget_exceeded"}))


if __name__ == "__main__":
    unittest.main()
