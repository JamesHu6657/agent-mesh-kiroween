"""KiroAdapter.parse() and stderr mapping on real kiro-cli 3.1.0 captures (m3-kiro-real; ACC: N24, N6, N12).

The fixtures under tests/fixtures/real/kiro/3.1.0/stream-*.jsonl and *stderr*.txt come from
earlier coordinator runs on base (logs/raw, 2026-11-07 and 2026-11-09); see
docs/impl-notes-m3-kiro-real.md for provenance and the one excerpted file. Every input is copied
into a fresh ``/tmp/mh-kiroparse-*`` run dir as ``stdout.log`` after its sidecar sha256 check.
No vendor process is started.
"""

import json
import os
import shutil
import tempfile
import unittest

from mesh.adapters import kiro as kiro_mod
from mesh.adapters.kiro import KiroAdapter
from tests import tierlib
from tests.unit.test_fixture_hashes import FIXTURE_HASHES

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL = os.path.join(REPO_ROOT, "tests", "fixtures", "real", "kiro", "3.1.0")

OK_SESSION = "8ad2fa07-b863-44b9-819e-e44c18517e5e"
REFUSAL_SESSION = "b2aa35c5-4da6-4c0b-bc37-4e00438ff06a"
RUNERR_SESSION = "05dfa9fa-cbf5-41d3-86bf-75d59203a829"
REFUSAL_FINAL = ("I've read the handoff. Running the two commands now, one per call.\n"
                 "(1) bash scripts/coord/status.sh \u2192 ALLOWED  (2) whoami \u2192 DENIED")


def real(name: str) -> str:
    path = os.path.join(REAL, name)
    tierlib.read_sidecar(path)  # raises TierError on a sha256 mismatch
    return path


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class KiroParseBase(unittest.TestCase):
    def stage(self, src: str, name: str = "stdout.log") -> str:
        d = tempfile.mkdtemp(prefix="mh-kiroparse-", dir="/tmp")
        self.assertTrue(d.startswith("/tmp/mh-"), d)
        self.addCleanup(shutil.rmtree, d, True)
        if src:
            shutil.copyfile(src, os.path.join(d, name))
        return d


class TestKiroParseReal(KiroParseBase):
    def test_success_stream_final_text_session_usage(self) -> None:
        """ACC: N24, N6 - stream-effort-high-ok: final_text is runFinished.finalText, session_id the first sessionId, usage from metadata (credits, turn time, effort, status), vendor_exit_meaning None."""
        d = self.stage(real("stream-effort-high-ok.jsonl"))
        res = KiroAdapter().parse(d)
        self.assertEqual(res.final_text, "KIRO_OK")
        self.assertEqual(res.session_id, OK_SESSION)
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.usage, {
            "kiro_credits": 0.054589, "turn_duration_ms": 2617, "turns": 1,
            "context_usage_pct": 1.287999987602234, "reasoning_effort": "high",
            "status": "success", "stop_reason": "end_turn",
        })
        scan = kiro_mod._scan_stream(os.path.join(d, "stdout.log"))
        self.assertEqual(scan["malformed_lines"], 0)
        self.assertTrue(scan["run_started"])
        self.assertEqual(scan["message_text"], scan["final_text"])

    def test_refusal_then_success_stream(self) -> None:
        """ACC: N24, N6 - stream-shell-forbidden-refusal: a mid-run CONTENT_FILTERED refusal and a forbidden shell call still end in status success; final_text is finalText (two messages joined by one newline), model from metadata.modelId, refusal kept in usage, vendor_exit_meaning None (the run finished)."""
        d = self.stage(real("stream-shell-forbidden-refusal.jsonl"))
        res = KiroAdapter().parse(d)
        self.assertEqual(res.final_text, REFUSAL_FINAL)
        self.assertEqual(res.session_id, REFUSAL_SESSION)
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.usage["model"], "claude-opus-5.5")
        self.assertEqual(res.usage["status"], "success")
        self.assertTrue(res.usage["content_filtered"])
        self.assertEqual(res.usage["refusal_category"], "CYBER")
        self.assertAlmostEqual(res.usage["kiro_credits"], 0.451907, places=6)
        scan = kiro_mod._scan_stream(os.path.join(d, "stdout.log"))
        self.assertEqual(scan["malformed_lines"], 0)
        # The chunk fallback rebuilds the same text 3.1.0 put in finalText.
        self.assertEqual(scan["message_text"], REFUSAL_FINAL)

    def test_run_error_auth_stream(self) -> None:
        """ACC: N24, N6 - stream-run-error-auth (runError, no runFinished): vendor_exit_meaning auth_failed, final_text falls back to the streamed message chunks, session id kept, no credits."""
        d = self.stage(real("stream-run-error-auth.jsonl"))
        res = KiroAdapter().parse(d)
        self.assertEqual(res.vendor_exit_meaning, "auth_failed")
        self.assertEqual(res.session_id, RUNERR_SESSION)
        self.assertEqual(res.final_text, "I'll start by reading the authoritative docs and the existing draft.")
        self.assertNotIn("kiro_credits", res.usage)
        self.assertNotIn("status", res.usage)
        facts = KiroAdapter.stream_facts(read_text(os.path.join(d, "stdout.log")))
        self.assertIn("bearer token included in the request is invalid", facts["error_message"])
        self.assertEqual(facts["vendor_exit_meaning"], "auth_failed")
        self.assertIsNone(facts["status"])

    def test_stream_facts_success(self) -> None:
        """ACC: N24 - stream_facts on the success capture: session id, effort, status; model None (3.1.0 only sometimes sends modelId)."""
        facts = KiroAdapter.stream_facts(read_text(real("stream-effort-high-ok.jsonl")))
        self.assertEqual((facts["session_id"], facts["effort"], facts["status"], facts["model"]),
                         (OK_SESSION, "high", "success", None))
        self.assertTrue(facts["run_started"])

    def test_metadata_subset_fixture_still_parses(self) -> None:
        """ACC: N24 - regression guard: the older effort-high-run-metadata.jsonl (sha in test_fixture_hashes) gives its session id, as stdout.log and under its own name."""
        rel = "kiro/3.1.0/effort-high-run-metadata.jsonl"
        self.assertIn(rel, FIXTURE_HASHES)
        src = os.path.join(REAL, "effort-high-run-metadata.jsonl")
        for name in ("stdout.log", "effort-high-run-metadata.jsonl"):
            with self.subTest(name=name):
                res = KiroAdapter().parse(self.stage(src, name))
                self.assertEqual(res.session_id, OK_SESSION)
                self.assertEqual(res.final_text, "")  # metadata lines only

    def test_empty_missing_and_malformed(self) -> None:
        """ACC: N24, N6 - no stdout.log, an empty one, or garbage lines: final_text "" (supervisor falls back to raw stdout), session None, usage None, vendor_exit_meaning run_error (no successful runFinished, PR #12 round 2); malformed lines are counted, never raised."""
        res = KiroAdapter().parse(self.stage(""))
        self.assertEqual((res.final_text, res.session_id, res.usage, res.vendor_exit_meaning), ("", None, None, "run_error"))
        d = self.stage("")
        with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('not json\n[1,2]\n{"type":"runFinished","data":"x"}\n\n')
        res = KiroAdapter().parse(d)
        self.assertEqual((res.final_text, res.session_id, res.usage, res.vendor_exit_meaning), ("", None, None, "run_error"))
        self.assertEqual(kiro_mod._scan_stream(os.path.join(d, "stdout.log"))["malformed_lines"], 2)

    def test_thought_chunks_never_become_final_text(self) -> None:
        """ACC: N24 - agent_thought_chunk text is not part of final_text; only agent_message_chunk is."""
        lines = [
            {"type": "metadata", "data": {"sessionId": "s1"}},
            {"type": "sessionUpdate", "data": {"sessionId": "s1", "update": {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "secret plan"}}}},
            {"type": "sessionUpdate", "data": {"sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "A"}}}},
            {"type": "sessionUpdate", "data": {"sessionId": "s1", "update": {"sessionUpdate": "tool_call", "toolCallId": "t"}}},
            {"type": "sessionUpdate", "data": {"sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "B"}}}},
        ]
        d = self.stage("")
        with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))
        res = KiroAdapter().parse(d)
        self.assertEqual(res.final_text, "A\nB")
        self.assertEqual(res.session_id, "s1")

    def test_content_filtered_without_finish(self) -> None:
        """ACC: N6 - a CONTENT_FILTERED refusal with no runFinished maps to vendor_exit_meaning content_filtered."""
        d = self.stage("")
        with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "metadata", "data": {"sessionId": "s2", "stopReason": "CONTENT_FILTERED",
                                                             "refusal": {"category": "CYBER"}}}) + "\n")
        res = KiroAdapter().parse(d)
        self.assertEqual(res.vendor_exit_meaning, "content_filtered")
        self.assertEqual(res.usage["refusal_category"], "CYBER")


class TestKiroStderrReal(unittest.TestCase):
    def test_denied_stderr(self) -> None:
        """ACC: N6 - stderr-tool-denied.txt (2 headless denials from a mesh-reviewer run) maps to tool_denied with the count."""
        err = KiroAdapter.parse_stderr_error(read_text(real("stderr-tool-denied.txt")))
        self.assertEqual(err["reason"], "tool_denied")
        self.assertEqual(err["details"]["count"], 2)

    def test_refusal_warning_is_not_an_error(self) -> None:
        """ACC: N6 - the CYBER refusal warning on stderr is not mapped (the run went on to success)."""
        self.assertIsNone(KiroAdapter.parse_stderr_error(read_text(real("stream-shell-forbidden-refusal-stderr.txt"))))

    def test_effort_rejection_kept(self) -> None:
        """ACC: N6 - the README effort-rejection signature still maps to model_or_effort_rejected."""
        err = KiroAdapter.parse_stderr_error("error: failed to set effort: model does not support additional fields")
        self.assertEqual(err["reason"], "model_or_effort_rejected")


class TestKiroStillGated(unittest.TestCase):
    def test_launch_still_disabled_and_profiles_unchanged(self) -> None:
        """ACC: N12, G24 - parse support alone does not open launch: the class value stays False (vendor launch comes only from smoke evidence, mesh/adapters/evidence.py); dev/elevated stay PROFILE_UNSUPPORTED (plan §14.2)."""
        from mesh.adapters.base import ProfileUnsupported
        a = KiroAdapter()
        self.assertFalse(a.launch_enabled)
        for p in ("dev", "elevated"):
            with self.assertRaises(ProfileUnsupported):
                a.profile_flags(p)
        self.assertEqual(a.profile_flags("readonly"), ["--trust-tools="])


if __name__ == "__main__":
    unittest.main()
