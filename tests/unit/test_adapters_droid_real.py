"""DroidAdapter.parse() on the real droid 1.6.0 C2 capture (m3-r6; ACC: N24, N12, N6).

Every input is copied into a fresh ``/tmp/mh-droidparse-*`` run dir as ``stdout.log``.
Real fixtures are read-only and checked against their sidecar sha256 (or, for the 1.3.0
init event, which has no sidecar, against tests/unit/test_fixture_hashes.py) before use.
No vendor process is started: the re-evaluation test runs with ``subprocess.Popen`` patched
to fail.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from mesh.adapters import droid as droid_mod
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.kiro import KiroAdapter
from tests import tierlib
from tests.unit.test_fixture_hashes import FIXTURE_HASHES

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_ROOT = os.path.join(REPO_ROOT, "tests", "fixtures", "real")
SYNTH_ROOT = os.path.join(REPO_ROOT, "tests", "fixtures", "synthetic", "droid")
RESULTS = os.path.join(REPO_ROOT, "tests", "smoke", "results", "droid-1.6.0-2026-11-09-2.json")
TOKEN = "MESH_SMOKE_OK"

SX1_SESSION = "9e176d53-937b-458d-997f-71b470bb2b9c"
SX1_USAGE = {
    "input_tokens": 4, "output_tokens": 14, "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 13561, "factory_credits": 13621, "thinking_tokens": 0, "ttft_ms": 1088,
}
SX3_USAGE = {
    "input_tokens": 8, "output_tokens": 35, "cache_read_input_tokens": 10603,
    "cache_creation_input_tokens": 16548, "factory_credits": 17120, "thinking_tokens": 0, "ttft_ms": 1565,
}
SX5_USAGE = {
    "input_tokens": 4, "output_tokens": 14, "cache_read_input_tokens": 13119,
    "cache_creation_input_tokens": 445, "factory_credits": 1029, "thinking_tokens": 0, "ttft_ms": 1076,
}
INIT_1_3_0 = "droid/1.3.0/exec-init-event-opus55-high.json"
INIT_1_3_0_SESSION = "d72c2698-16db-41f0-aa24-9b3090171deb"


def real_fixture(rel: str) -> str:
    """Path of a real fixture after checking its sha256 (sidecar, else test_fixture_hashes)."""
    path = os.path.join(REAL_ROOT, rel)
    if os.path.isfile(path + tierlib.SIDECAR_SUFFIX):
        tierlib.read_sidecar(path)  # raises TierError on any sha256 mismatch
        return path
    with open(path, "rb") as f:
        actual = hashlib.sha256(f.read()).hexdigest()
    assert actual == FIXTURE_HASHES[rel], f"sha256 mismatch for {rel}: {actual}"
    return path


def caps_snapshot(adapter):
    return {n: (c.supported, c.verification, c.note) for n, c in adapter.capabilities.items()}


class DroidParseBase(unittest.TestCase):
    def run_dir(self) -> str:
        d = tempfile.mkdtemp(prefix="mh-droidparse-", dir="/tmp")
        self.assertTrue(d.startswith("/tmp/mh-"), d)
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def stage(self, src: str, name: str = "stdout.log") -> str:
        d = self.run_dir()
        shutil.copyfile(src, os.path.join(d, name))
        return d

    def stage_real(self, case: str) -> str:
        return self.stage(real_fixture(f"droid/1.6.0/smoke-{case}-stdout.jsonl"))


class TestDroidParseReal(DroidParseBase):
    def test_droid_parse_real_sx1_final_text_session_usage(self) -> None:
        """ACC: N24, N6 - S-X-1 and S-X-5: final_text is the completion event's finalText, session_id the init event's, usage the completion usage (factory_credits kept), vendor_exit_meaning None."""
        for case, sid, usage in (("S-X-1", SX1_SESSION, SX1_USAGE),
                                 ("S-X-5", "c09dcfd8-ee77-4d2d-bdb9-e97fd15d3dd4", SX5_USAGE)):
            with self.subTest(case=case):
                d = self.stage_real(case)
                res = DroidAdapter().parse(d)
                self.assertEqual(droid_mod._scan_stream(os.path.join(d, "stdout.log"))["malformed"], 0)
                self.assertEqual(res.final_text, TOKEN)
                self.assertEqual(res.session_id, sid)
                self.assertEqual(res.usage, usage)
                self.assertIsNone(res.vendor_exit_meaning)
        res = DroidAdapter().parse(self.stage_real("S-X-1"))
        self.assertEqual(res.usage["factory_credits"], 13621)

    def test_droid_parse_real_sx3_resumed_token(self) -> None:
        """ACC: N24 - S-X-3 (resumed R1 session): the token is in final_text and the session id is the R1 session."""
        d = self.stage_real("S-X-3")
        res = DroidAdapter().parse(d)
        self.assertEqual(droid_mod._scan_stream(os.path.join(d, "stdout.log"))["malformed"], 0)
        self.assertIn(TOKEN, res.final_text)
        self.assertEqual(res.final_text, "I replied with `MESH_SMOKE_OK`.")
        self.assertEqual(res.session_id, SX1_SESSION)
        self.assertEqual(res.usage, SX3_USAGE)
        self.assertIsNone(res.vendor_exit_meaning)

    def test_droid_parse_real_streams_without_completion(self) -> None:
        """ACC: N24, N6 - S-X-6 (SIGTERM), S-X-7 and S-DROID-8 (error event, rc 1) have no completion: final_text "", init session id, usage None, nothing malformed; message/tool/error text never becomes final_text."""
        for case, sid in (("S-X-6", "6dbacd6c-a492-4be7-a65c-700ebc7799cf"),
                          ("S-X-7", "d71ac66d-f418-479d-a2ee-a26fc67e5097"),
                          ("S-DROID-8", "ce360ce6-07f0-43fd-a3bf-6f5ce0cf1079")):
            with self.subTest(case=case):
                d = self.stage_real(case)
                res = DroidAdapter().parse(d)
                self.assertEqual(res.final_text, "")
                self.assertEqual(res.session_id, sid)
                self.assertIsNone(res.usage)
                self.assertIsNone(res.vendor_exit_meaning)
                scan = droid_mod._scan_stream(os.path.join(d, "stdout.log"))
                self.assertEqual(scan["malformed"], 0)
                self.assertIsNone(scan["final_text"])

    def test_droid_parse_init_fixture_1_3_0(self) -> None:
        """ACC: N24 - regression guard: the 1.3.0 init event still gives its session id, as stdout.log and under its own name (fallback, session id only)."""
        src = real_fixture(INIT_1_3_0)
        for name in ("stdout.log", "exec-init-event-opus55-high.json"):
            with self.subTest(name=name):
                d = self.stage(src, name)
                res = DroidAdapter().parse(d)
                self.assertEqual(droid_mod._scan_stream(os.path.join(d, name))["malformed"], 0)
                self.assertEqual(res.session_id, INIT_1_3_0_SESSION)
                self.assertEqual(res.final_text, "")
                self.assertIsNone(res.usage)


class TestDroidParseRobustness(DroidParseBase):
    def test_droid_parse_synthetic_robustness(self) -> None:
        """ACC: N24, N12 - robustness only, never evidence: synthetic streams (truncated last line, unknown events, non-object JSON, a bad line, undecodable bytes, no stdout.log) never raise, keep the init session id, take final_text only from a complete completion event, and leave every capability's verification unchanged."""
        adapter = DroidAdapter()
        before = caps_snapshot(adapter)
        expected = {
            "synthetic_truncated.jsonl": ("", "synthetic-droid-0001", 1),
            "synthetic_unknown_event.jsonl": ("SYNTHETIC_FINAL", "synthetic-droid-0002", 1),
        }
        for name, (text, sid, malformed) in expected.items():
            with self.subTest(name=name):
                src = os.path.join(SYNTH_ROOT, name)
                self.assertTrue(name.startswith("synthetic_"))
                with open(src, encoding="utf-8") as f:
                    raw = f.read()
                self.assertNotIn(TOKEN, raw)
                self.assertIn("SYNTHETIC", raw.splitlines()[0])
                d = self.stage(src)
                res = adapter.parse(d)
                self.assertEqual(res.final_text, text)
                self.assertEqual(res.session_id, sid)
                self.assertIsNone(res.usage)  # truncated: no completion; unknown_event: usage not a dict
                self.assertIsNone(res.vendor_exit_meaning)
                self.assertEqual(droid_mod._scan_stream(os.path.join(d, "stdout.log"))["malformed"], malformed)
        # undecodable leading bytes: replaced on read, counted as one malformed line, session id kept
        with open(real_fixture("droid/1.6.0/smoke-S-X-1-stdout.jsonl"), "rb") as f:
            init_line = f.readline()
        d = self.run_dir()
        log = os.path.join(d, "stdout.log")
        with open(log, "wb") as f:
            f.write(b"\xff\xfe\n" + init_line)
        res = adapter.parse(d)
        self.assertEqual(res.session_id, SX1_SESSION)
        self.assertEqual(res.final_text, "")
        self.assertIsNone(res.usage)
        self.assertEqual(droid_mod._scan_stream(log)["malformed"], 1)
        # blank lines are neither parsed nor counted; a later completion wins and resets usage
        with open(log, "w", encoding="utf-8") as f:
            f.write('\n   \n{"type":"completion","finalText":"A","usage":{"factory_credits":1}}\n\n'
                    '{"type":"completion","finalText":"B"}\n{"type":"completion","finalText":7}\n'
                    '{"type":"completion","finalText":"C"}\n')
        scan = droid_mod._scan_stream(log)
        self.assertEqual((scan["final_text"], scan["usage"], scan["malformed"], scan["session_id"]), ("C", None, 0, None))
        with open(log, "w", encoding="utf-8") as f:
            f.write('{"type":"completion","finalText":"C","usage":{"factory_credits":1}}\n'
                    '{"type":"completion","finalText":5}\n')
        res = adapter.parse(d)
        self.assertEqual((res.final_text, res.usage), ("", None))
        # no stdout.log at all: empty result, no exception
        res = adapter.parse(self.run_dir())
        self.assertEqual((res.final_text, res.session_id, res.usage, res.vendor_exit_meaning), ("", None, None, None))
        self.assertEqual(caps_snapshot(adapter), before)
        self.assertEqual(caps_snapshot(DroidAdapter()), before)


class TestReasoningEffortClassValue(unittest.TestCase):
    def test_reasoning_effort_class_value_is_help(self) -> None:
        """ACC: N12 - D6: the class-declared reasoning_effort is supported "yes" with verification "help" (one fixture is not a smoke result) on droid and kiro; the note text is unchanged."""
        for adapter, note in ((DroidAdapter(), "exec-init-event-opus55-high.json"),
                              (KiroAdapter(), "effort-high-run-metadata.jsonl")):
            with self.subTest(adapter=adapter.id):
                cap = adapter.capabilities["reasoning_effort"]
                self.assertEqual((cap.supported, cap.verification, cap.note), ("yes", "help", note))


class TestReevaluateCommittedDroidResults(unittest.TestCase):
    def test_reevaluate_committed_droid_results_offline(self) -> None:
        """ACC: N12, N24 - offline, in memory: reevaluate_doc on the committed droid 1.6.0 results with the r6 parser turns S-X-1 into pass (evaluated_from set); S-X-2/4/5 pass; S-X-3, S-X-6, S-X-7, S-DROID-8 carried unchanged. No process starts, nothing is written outside /tmp/mh-*."""
        from tests.smoke import reevaluate

        tierlib.read_sidecar(RESULTS)
        with open(RESULTS, encoding="utf-8") as f:
            doc = json.load(f)
        name = os.path.basename(RESULTS)
        source = f"tests/smoke/results/{name}"
        made = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            made.append(path)
            return path

        with mock.patch.object(tempfile, "tempdir", "/tmp"), \
                mock.patch.object(tempfile, "mkdtemp", side_effect=tracking_mkdtemp), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no process may start")):
            new = reevaluate.reevaluate_doc(doc, name, REAL_ROOT)
        self.assertTrue(made)
        for path in made:
            self.assertTrue(path.startswith("/tmp/mh-"), path)
        before = {c["id"]: c for c in doc["cases"]}
        after = {c["id"]: c for c in new["cases"]}
        self.assertEqual(set(after), set(before))
        self.assertEqual((before["S-X-1"]["status"], before["S-X-1"]["reason"]), ("fail", "parse_pending"))
        self.assertEqual((after["S-X-1"]["status"], after["S-X-1"]["reason"]), ("pass", None))
        self.assertEqual(after["S-X-1"]["evaluated_from"], source)
        self.assertIsNone(after["S-X-1"]["carried_from"])
        for cid in ("S-X-2", "S-X-4", "S-X-5"):
            self.assertEqual(after[cid]["status"], "pass", cid)
            self.assertEqual(after[cid]["evaluated_from"], source, cid)
        for cid in ("S-X-3", "S-X-6", "S-X-7", "S-DROID-8"):
            self.assertEqual(after[cid]["status"], before[cid]["status"], cid)
            self.assertEqual(after[cid]["reason"], before[cid]["reason"], cid)
            self.assertEqual(after[cid]["observation"], before[cid]["observation"], cid)
            self.assertEqual(after[cid]["carried_from"], source, cid)
            self.assertIsNone(after[cid]["evaluated_from"], cid)
        # the committed document itself is untouched
        with open(RESULTS, encoding="utf-8") as f:
            self.assertEqual(json.load(f), doc)


if __name__ == "__main__":
    unittest.main()
