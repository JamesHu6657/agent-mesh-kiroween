"""Offline tests for the kiro smoke cases (m3-kiro-real; ACC: N11, N12, N24, N25).

A stub ``kiro-cli`` written under /tmp/mh-* replays the kiro-cli 3.1.0 stream-json shape seen
in tests/fixtures/real/kiro/3.1.0/ (runStarted, metadata with sessionId / reasoning.effort /
meteringUsage, agent_message_chunk, runFinished; runError and the headless ``[denied]`` stderr
line in their modes). It reacts to the argv KiroAdapter.build() makes. Every env is a dict;
os.environ never gets MESH_SMOKE* or MESH_PROBE*. No real vendor binary runs.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from mesh.adapters import smoke_schema
from tests import tierlib
from tests.smoke import harness
from tests.smoke import reevaluate as ev
from tests.smoke import test_smoke as cases
from tests.stubs import make_executable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STUB = r'''#!/usr/bin/env python3
import json, os, signal, sys, time
args = sys.argv[1:]
with open(CALLS, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("kiro-cli 3.1.0"); sys.exit(0)
if not args or args[0] != "chat":
    sys.exit(99)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
mode = os.environ.get("STUB_MODE", "ok")
prompt = args[-1]
sid = opt("--resume-id") or "kiro-stub-session-7"
effort = opt("--effort") or "high"
def emit(t, data):
    print(json.dumps({"type": t, "data": data}), flush=True)
def chunk(text):
    emit("sessionUpdate", {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk",
                                                        "content": {"type": "text", "text": text}}})
emit("runStarted", {"payloadSchema": "acp", "acpProtocolVersion": 1, "engine": "v2"})
emit("metadata", {"sessionId": sid, "contextUsagePercentage": 0.7, "reasoning": {"support": "unavailable", "effortLevels": []}})
meta = {"sessionId": sid, "reasoning": {"support": "alwaysOn", "effort": effort,
                                         "effortLevels": ["low", "medium", "high", "xhigh", "max"]}}
if mode == "model_id":
    meta["modelId"] = opt("--model")
emit("metadata", meta)
if mode == "run_error":
    chunk("I'll start")
    emit("runError", {"sessionId": sid, "stage": "prompt", "message": "Internal error (code -32603): Encountered an error in the response stream: The bearer token included in the request is invalid."})
    sys.exit(0)
if "Count from 1" in prompt:
    if mode == "term_abort":
        def on_term(signo, frame):
            time.sleep(0.2)
            os.abort()
        signal.signal(signal.SIGTERM, on_term)
    if mode == "term_run_error":
        def on_term(signo, frame):
            emit("runError", {"sessionId": sid, "stage": "prompt", "message": "Internal error: the response stream was closed."})
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
    for i in range(1, 100000):
        chunk(str(i) + "\n")
        time.sleep(0.01)
    sys.exit(0)
if "x.txt" in prompt or "touch y.txt" in prompt:
    sys.stderr.write("[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.\n")
    text = "I could not run that tool."
elif "--resume-id" in args:
    text = "I replied with MESH_SMOKE_OK."
else:
    text = "MESH_SMOKE_OK"
chunk(text)
emit("metadata", {"sessionId": sid, "meteringUsage": [{"value": 0.05, "unit": "credit", "unitPlural": "credits"}], "turnDurationMs": 900})
emit("runFinished", {"sessionId": sid, "status": "success", "stopReason": "end_turn", "finalText": text, "finalTextTruncated": False})
sys.exit(0)
'''


class KiroCasesBase(unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])
        self.tmp = tempfile.mkdtemp(prefix="mh-smoke-k-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "calls.log")
        os.makedirs(os.path.join(self.tmp, "bin"))
        self.binary = os.path.join(self.tmp, "bin", "kiro-cli")
        first, rest = STUB.split("\n", 1)
        with open(self.binary, "w", encoding="utf-8") as f:
            f.write(first + f"\nCALLS = {self.calls!r}\n" + rest)
        make_executable(self.binary)
        self.out = os.path.join(self.tmp, "out")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MESH_SMOKE": "1",
                    "MESH_SMOKE_ADAPTERS": "kiro", "MESH_SMOKE_CONSENT": "kiro", "MESH_SMOKE_MAX_RUNS": "6",
                    "MESH_SMOKE_MODEL_KIRO": "claude-haiku-5", "MESH_SMOKE_TIMEOUT_S": "20",
                    "MESH_SMOKE_OUT": self.out, "MESH_SMOKE_CONSENT_REF": "offline stub"}

    def tearDown(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])

    def session(self, **env) -> harness.SmokeSession:
        s = harness.SmokeSession(dict(self.env, **env), "kiro", self.binary, grace_s=1, version="3.1.0",
                                 mesh_commit="abc1234")
        self.addCleanup(s.cleanup)
        return s

    def model_runs(self) -> list:
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as f:
            return [a for a in map(json.loads, f.read().splitlines()) if a[:1] == ["chat"]]

    @staticmethod
    def by_id(doc) -> dict:
        return {c["id"]: c for c in doc["cases"]}


class KiroFlowTest(KiroCasesBase):
    def test_all_runs_results_and_reevaluate(self) -> None:
        """ACC: N11, N12, N24 - R1..R6 against the kiro stub: readonly argv (--trust-tools=, never trust-all), the smoke model, --resume-id, --effort; S-X-1/2/3/5/6 pass, S-X-4 observed(not_observable), S-X-7 observed, S-KIRO-8 deny with the [denied] count; results validate and list KIRO_CASES; reevaluate re-parses S-X-1/2/4/5 without spawning."""
        s = self.session()
        sid = cases.run_r1(s)
        self.assertEqual(sid, "kiro-stub-session-7")
        cases.run_r2(s, sid)
        cases.run_r3(s, "low")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        cases.run_r5(s)
        cases.run_r6(s)
        runs = self.model_runs()
        self.assertEqual(len(runs), 6)
        for argv in runs:
            self.assertEqual(argv[:4], ["chat", "--no-interactive", "--output-format", "stream-json"])
            self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-5")
            self.assertIn("--trust-tools=", argv)
            self.assertNotIn("--trust-all-tools", argv)
            self.assertNotIn("-a", argv)
        self.assertEqual(runs[1][runs[1].index("--resume-id") + 1], "kiro-stub-session-7")
        self.assertEqual(runs[2][runs[2].index("--effort") + 1], "low")
        path = cases.finish_stage(s)
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        self.assertEqual(doc["adapter"], "kiro")
        self.assertEqual([x["id"] for x in doc["cases"]], list(cases.KIRO_CASES))
        c = self.by_id(doc)
        self.assertEqual(c["S-X-1"]["status"], "pass")
        self.assertEqual((c["S-X-2"]["status"], c["S-X-2"]["observation"]["session_id"]), ("pass", "kiro-stub-session-7"))
        self.assertEqual(c["S-X-3"]["status"], "pass")
        self.assertEqual((c["S-X-4"]["status"], c["S-X-4"]["reason"]), ("observed", "not_observable"))
        self.assertEqual((c["S-X-5"]["status"], c["S-X-5"]["observation"]["echoed"]), ("pass", "low"))
        self.assertEqual(c["S-X-6"]["status"], "pass")
        self.assertGreaterEqual(c["S-X-6"]["observation"]["lines_at_term"], 4)
        self.assertEqual(c["S-X-7"]["status"], "observed")
        self.assertFalse(c["S-X-7"]["observation"]["file_created"])
        k8 = c["S-KIRO-8"]["observation"]
        self.assertEqual((k8["outcome"], k8["y_txt_exists"], k8["denied_lines"]), ("deny", False, 1))
        self.assertNotIn("S-DROID-8", c)
        rout = os.path.join(self.tmp, "reeval")
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("subprocess.Popen", side_effect=AssertionError("spawned")):
            rc = ev.main([path, "--out", rout])
        self.assertEqual(rc, 0, buf.getvalue())
        new_path = json.loads(buf.getvalue())["results"]
        with open(new_path, encoding="utf-8") as f:
            new = json.load(f)
        self.assertEqual(smoke_schema.validate(new, REPO_ROOT, fixture_root=self.out), [])
        nc = self.by_id(new)
        src = "tests/smoke/results/" + os.path.basename(path)
        for cid in ("S-X-1", "S-X-2", "S-X-4", "S-X-5"):
            self.assertEqual(nc[cid]["evaluated_from"], src, cid)
            self.assertEqual(nc[cid]["status"], c[cid]["status"], cid)
        for cid in ("S-X-3", "S-X-6", "S-X-7", "S-KIRO-8"):
            self.assertEqual(nc[cid]["carried_from"], src, cid)
        self.assertEqual(tierlib.read_sidecar(new_path)["command"][:3], ["python3", "-m", "tests.smoke.reevaluate"])

    def test_model_id_mode_sx4_pass(self) -> None:
        """ACC: N11 - when the kiro stream carries metadata.modelId, S-X-4 compares it: pass on the requested model."""
        s = self.session(STUB_MODE="model_id", MESH_SMOKE_MAX_RUNS="1")
        cases.run_r1(s)
        c = {x["id"]: x for x in s.cases}
        self.assertEqual((c["S-X-4"]["status"], c["S-X-4"]["observation"]["init_model"]), ("pass", "claude-haiku-5"))

    def test_run_error_is_not_a_pass(self) -> None:
        """ACC: N11, N6 - rc 0 with a runError (bearer token invalid): S-X-1 fail(vendor_error, auth_failed); no session id handed to R2."""
        s = self.session(STUB_MODE="run_error", MESH_SMOKE_MAX_RUNS="1")
        sid = cases.run_r1(s)
        c = {x["id"]: x for x in s.cases}
        self.assertEqual((c["S-X-1"]["status"], c["S-X-1"]["reason"]), ("fail", "vendor_error"))
        self.assertEqual(c["S-X-1"]["observation"]["vendor_exit_meaning"], "auth_failed")
        self.assertIsNone(sid)  # a runError session is not handed to R2 (no wasted resume run)
        self.assertEqual(len(self.model_runs()), 1)

    def test_run_cap_one(self) -> None:
        """ACC: N25 - kiro credit rule: MESH_SMOKE_MAX_RUNS=1 spawns one model run; the next run's cases are not_run(run_cap)."""
        s = self.session(MESH_SMOKE_MAX_RUNS="1")
        cases.run_r1(s)
        cases.run_r6(s)
        self.assertEqual((s.cases[-1]["id"], s.cases[-1]["reason"]), ("S-KIRO-8", "run_cap"))
        self.assertEqual(len(self.model_runs()), 1)

    def test_stream_facts_droid_unchanged(self) -> None:
        """ACC: N24 - stream_facts keeps the droid init-event reading; unknown adapters give all None."""
        line = '{"type":"system","subtype":"init","session_id":"d1","model":"m","reasoning_effort":"low","cwd":"/w"}'
        self.assertEqual(ev.stream_facts(line, "droid"),
                         {"session_id": "d1", "model": "m", "effort": "low", "cwd": "/w", "vendor_exit_meaning": None,
                          "stream_error": None})
        self.assertEqual(ev.stream_facts(line, "agy")["session_id"], None)
        self.assertEqual(ev.stream_facts(line, "kiro")["session_id"], None)

    def test_kiro_tier_skips_without_gate(self) -> None:
        """ACC: N25 - without MESH_SMOKE=1 KiroSmoke skips and spawns nothing; the base class skips too."""
        self.assertNotEqual(os.environ.get("MESH_SMOKE"), "1")
        for klass in (cases.KiroSmoke, cases._VendorSmoke):
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(klass).run(result)
            self.assertEqual(result.failures + result.errors, [])
            self.assertTrue(result.skipped)
        self.assertEqual(self.model_runs(), [])


R_KIRO = os.path.join(REPO_ROOT, "tests", "fixtures", "real", "kiro", "3.1.0")


def _read(name: str) -> str:
    with open(os.path.join(R_KIRO, name), encoding="utf-8") as f:
        return f.read()


class KiroFixRoundTest(KiroCasesBase):
    """Linear review A of PR #12 (MAJOR 2, MINOR 1, MINOR 2): rc-0 vendor errors never pass."""

    def test_sx6_rc0_runerror_on_sigterm_fails(self) -> None:
        """MAJOR 2: kiro alive at SIGTERM, then writes a runError to stdout and exits 0 (no stderr):
        S-X-6 fail(exited_with_error) with vendor_exit_meaning, not pass."""
        s = self.session(STUB_MODE="term_run_error")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        c = {x["id"]: x for x in s.cases}["S-X-6"]
        self.assertEqual((c["status"], c["reason"]), ("fail", "exited_with_error"))
        self.assertEqual(c["observation"]["rc"], 0)
        self.assertTrue(c["observation"]["alive_at_term"])
        self.assertEqual(c["observation"]["vendor_exit_meaning"], "run_error")

    def test_sx6_eval_counterexamples(self) -> None:
        """MAJOR 2: eval_sx6 on literal observations. rc 0 + the real auth runError stream -> fail;
        rc -15 with the same stream (killed by the signal) -> pass; rc 1 + stderr -> fail; rc 143 +
        stderr -> pass; rc 0 + a clean stream -> pass; not alive at term -> fail; group left -> fail."""
        auth = _read("stream-run-error-auth.jsonl")
        clean = _read("smoke-S-X-1-stdout.jsonl")
        gone = {"alive_at_term": True, "group_gone_after_term": True}
        st = ev.eval_sx6(dict(gone, rc=0), auth, "", "kiro")
        self.assertEqual(st[:2], ("fail", "exited_with_error"))
        self.assertEqual(st[2]["vendor_exit_meaning"], "auth_failed")
        self.assertEqual(ev.eval_sx6(dict(gone, rc=-15), auth, "", "kiro")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=1), clean, "quota exceeded\n", "kiro")[:2],
                         ("fail", "exited_with_error"))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=143), clean, "terminated\n", "kiro")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=0), clean, "", "kiro")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx6({"alive_at_term": False, "group_gone_after_term": True, "rc": 0}, clean, "",
                                     "kiro")[:2], ("fail", "exited_before_signal"))
        self.assertEqual(ev.eval_sx6({"alive_at_term": True, "group_gone_after_term": False, "rc": None}, clean, "",
                                     "kiro")[:2], ("fail", "group_survived_sigterm"))
        # droid has no in-stream meaning: rc 0 after SIGTERM stays a pass (unchanged rule)
        self.assertEqual(ev.eval_sx6(dict(gone, rc=0), auth, "", "droid")[:2], ("pass", None))

    def test_sx6_committed_r4_evidence_still_passes(self) -> None:
        """MAJOR 2: the committed kiro R4 (rc -15) re-judged by eval_sx6 from its recorded observation,
        stdout and stderr fixtures -> pass."""
        path = os.path.join(REPO_ROOT, "tests", "smoke", "results", "kiro-3.1.0-2026-11-10-2.json")
        with open(path, encoding="utf-8") as f:
            case = self.by_id(json.load(f))["S-X-6"]
        self.assertEqual((case["status"], case["observation"]["rc"]), ("pass", -15))
        info = {k: v for k, v in case["observation"].items() if k != "stderr_excerpt"}
        st = ev.eval_sx6(info, _read("smoke-S-X-6-stdout.jsonl"), _read("smoke-S-X-6-stderr.txt"), "kiro")
        self.assertEqual(st[:2], ("pass", None))

    def test_staged_resume_refuses_failed_r1(self) -> None:
        """MINOR 1: stage 1 = R1 with an rc-0 runError (S-X-1 fail, S-X-2 fail(vendor_error));
        stage 2 = R2 only: staged_session_id is None, S-X-3 blocked(no_session_id), no resume spawned."""
        s1 = self.session(STUB_MODE="run_error", MESH_SMOKE_MAX_RUNS="1")
        self.assertIsNone(cases.run_r1(s1))
        c = {x["id"]: x for x in s1.cases}
        self.assertEqual((c["S-X-2"]["status"], c["S-X-2"]["reason"]), ("fail", "vendor_error"))
        self.assertEqual(c["S-X-2"]["observation"]["vendor_exit_meaning"], "auth_failed")
        cases.finish_stage(s1, ["R1"])
        s2 = self.session(MESH_SMOKE_MAX_RUNS="1")
        self.assertIsNone(cases.staged_session_id(s2))
        cases.run_r2(s2, cases.staged_session_id(s2))
        c3 = {x["id"]: x for x in s2.cases}["S-X-3"]
        self.assertEqual((c3["status"], c3["reason"]), ("blocked", "no_session_id"))
        self.assertEqual(len(self.model_runs()), 1)

    def test_staged_resume_uses_good_r1(self) -> None:
        """MINOR 1 (control): a successful staged R1 -> its session id is resumed in stage 2."""
        s1 = self.session(MESH_SMOKE_MAX_RUNS="1")
        self.assertEqual(cases.run_r1(s1), "kiro-stub-session-7")
        cases.finish_stage(s1, ["R1"])
        s2 = self.session(MESH_SMOKE_MAX_RUNS="1")
        self.assertEqual(cases.staged_session_id(s2), "kiro-stub-session-7")
        cases.run_r2(s2, cases.staged_session_id(s2))
        runs = self.model_runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[1][runs[1].index("--resume-id") + 1], "kiro-stub-session-7")

    def test_staged_session_requires_sx1_pass_same_capture(self) -> None:
        """MINOR 1: staged documents written by hand. S-X-2 pass but S-X-1 fail(vendor_error) -> None;
        S-X-1 and S-X-2 pass from different captures -> None; S-X-1 fail(parse_pending) at rc 0 in the
        same capture -> resumed (droid rule unchanged); a newer failed R1 hides an older good one -> None."""
        s = self.session()
        fx_a = {"fixture": "tests/fixtures/real/kiro/3.1.0/smoke-S-X-1-stdout.jsonl", "fixture_sha256": "a" * 64}
        fx_b = {"fixture": "tests/fixtures/real/kiro/3.1.0/smoke-S-X-1-stdout-2.jsonl", "fixture_sha256": "b" * 64}
        good2 = dict(fx_a, id="S-X-2", status="pass", observation={"session_id": "sid-1"})
        with mock.patch.object(s, "staged_documents", return_value=[
                {"cases": [dict(fx_a, id="S-X-1", status="fail", reason="vendor_error", rc=0, observation=None), good2]}]):
            self.assertIsNone(cases.staged_session_id(s))
        with mock.patch.object(s, "staged_documents", return_value=[
                {"cases": [dict(fx_a, id="S-X-1", status="fail", reason="parse_pending", rc=0, observation=None), good2]}]):
            self.assertEqual(cases.staged_session_id(s), "sid-1")
        with mock.patch.object(s, "staged_documents", return_value=[
                {"cases": [dict(fx_b, id="S-X-1", status="pass", rc=0, observation=None), good2]}]):
            self.assertIsNone(cases.staged_session_id(s))
        with mock.patch.object(s, "staged_documents", return_value=[
                {"cases": [dict(fx_a, id="S-X-1", status="pass", rc=0, observation=None), good2]}]):
            self.assertEqual(cases.staged_session_id(s), "sid-1")
        with mock.patch.object(s, "staged_documents", return_value=[
                {"cases": [dict(fx_a, id="S-X-1", status="pass", rc=0, observation=None), good2]},
                {"cases": [dict(fx_b, id="S-X-1", status="fail", observation=None),
                           dict(fx_b, id="S-X-2", status="fail", observation={"session_id": "sid-2"})]}]):
            self.assertIsNone(cases.staged_session_id(s))

    def test_sx5_rc0_vendor_error_fails(self) -> None:
        """MINOR 2: the real auth-error stream echoes effort=high before its runError; rc 0, requested
        high -> S-X-5 fail(vendor_error), not pass. The successful effort-high stream -> pass."""
        st = ev.eval_sx5(_read("stream-run-error-auth.jsonl"), "", 0, False, "high", adapter_id="kiro")
        self.assertEqual(st[:2], ("fail", "vendor_error"))
        self.assertEqual(st[2], {"echoed": "high", "vendor_exit_meaning": "auth_failed"})
        self.assertEqual(ev.eval_sx5(_read("stream-effort-high-ok.jsonl"), "", 0, False, "high",
                                     adapter_id="kiro"), ("pass", None, {"echoed": "high"}))

    def test_sx5_run_r3_stub_run_error(self) -> None:
        """MINOR 2: run_r3 against the stub's rc-0 runError mode -> S-X-5 fail(vendor_error)."""
        s = self.session(STUB_MODE="run_error", MESH_SMOKE_MAX_RUNS="1")
        cases.run_r3(s, "high")
        c = {x["id"]: x for x in s.cases}["S-X-5"]
        self.assertEqual((c["status"], c["reason"]), ("fail", "vendor_error"))
        self.assertEqual(c["observation"]["requested_effort"], "high")

    def test_sx2_sx4_rc0_vendor_error_fail(self) -> None:
        """MINOR 1: eval_sx2 / eval_sx4 on the real auth-error stream at rc 0 -> fail(vendor_error),
        though the stream carries a session id."""
        from mesh.adapters.kiro import KiroAdapter
        d = tempfile.mkdtemp(prefix="mh-sx2-")
        self.addCleanup(shutil.rmtree, d, True)
        shutil.copyfile(os.path.join(R_KIRO, "stream-run-error-auth.jsonl"), os.path.join(d, "stdout.log"))
        parsed = KiroAdapter().parse(d)
        stdout = _read("stream-run-error-auth.jsonl")
        self.assertEqual(ev.eval_sx2(stdout, 0, False, parsed, adapter_id="kiro"),
                         ("fail", "vendor_error", {"session_id": "05dfa9fa-cbf5-41d3-86bf-75d59203a829",
                                                   "vendor_exit_meaning": "auth_failed"}))
        self.assertEqual(ev.eval_sx4(stdout, 0, False, "claude-haiku-5.5", adapter_id="kiro")[:2],
                         ("fail", "vendor_error"))


class KiroRound2Test(KiroCasesBase):
    """PR #12 fix round 2 (Linear r2 + Quest review on 81e2836)."""

    def _parse(self, events) -> str:
        d = tempfile.mkdtemp(prefix="mh-kr2-")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
            for e in events:
                f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")
        from mesh.adapters.kiro import KiroAdapter
        return KiroAdapter().parse(d).vendor_exit_meaning

    def test_any_runerror_shape_is_an_error(self) -> None:
        """Item 2: every runError counts. Literal shapes after a successful-looking start."""
        start = [{"type": "runStarted", "data": {}}, {"type": "metadata", "data": {"sessionId": "s"}}]
        fin = {"type": "runFinished", "data": {"sessionId": "s", "status": "success", "finalText": "ok"}}
        self.assertEqual(self._parse(start + [{"type": "runError", "data": {"stage": None, "message": None}}]), "run_error")
        self.assertEqual(self._parse(start + [{"type": "runError"}]), "run_error")
        self.assertEqual(self._parse(start + [{"type": "runError", "data": None}]), "run_error")
        self.assertEqual(self._parse(start + [{"type": "runError", "data": "boom"}]), "run_error")
        self.assertEqual(self._parse(start + [{"type": "runError", "data": {"message": "The bearer token included in the request is invalid."}}]), "auth_failed")
        self.assertEqual(self._parse(start + [fin, {"type": "runError", "data": {}}]), "run_error")
        self.assertEqual(self._parse(start + [{"type": "runError", "data": {}}, fin]), "run_error")
        self.assertIsNone(self._parse(start + [fin]))

    def test_no_successful_runfinished_is_run_error(self) -> None:
        """Item 2: no completion evidence at rc 0 -> run_error: truncated stream, empty file,
        runFinished without status or with status error. A refusal that did not finish -> content_filtered."""
        start = [{"type": "runStarted", "data": {}}, {"type": "metadata", "data": {"sessionId": "s"}}]
        self.assertEqual(self._parse(start), "run_error")
        self.assertEqual(self._parse([]), "run_error")
        self.assertEqual(self._parse(start + [{"type": "runFinished", "data": {"finalText": "x"}}]), "run_error")
        self.assertEqual(self._parse(start + [{"type": "runFinished", "data": {"status": "error"}}]), "run_error")
        self.assertEqual(self._parse(start + [{"type": "metadata", "data": {"stopReason": "CONTENT_FILTERED"}}]),
                         "content_filtered")

    def test_real_fixtures_unchanged(self) -> None:
        """Item 2: real kiro 3.1.0 fixtures: ok/effort/refusal/S-X-1 -> None, auth -> auth_failed,
        the SIGTERM-cut S-X-6 stream (no runFinished) -> run_error but no stream_error."""
        from mesh.adapters.kiro import KiroAdapter
        want = {"smoke-S-X-1-stdout.jsonl": None, "stream-effort-high-ok.jsonl": None,
                "stream-shell-forbidden-refusal.jsonl": None, "stream-run-error-auth.jsonl": "auth_failed",
                "smoke-S-X-6-stdout.jsonl": "run_error"}
        for name, meaning in want.items():
            f = KiroAdapter.stream_facts(_read(name))
            self.assertEqual(f["vendor_exit_meaning"], meaning, name)
        self.assertIsNone(KiroAdapter.stream_facts(_read("smoke-S-X-6-stdout.jsonl"))["stream_error"])

    def test_sx6_only_harness_sigterm_passes(self) -> None:
        """Item 1: literal eval_sx6 verdicts on a clean cut stream (gone in window, alive at term)."""
        clean = _read("smoke-S-X-6-stdout.jsonl")
        auth = _read("stream-run-error-auth.jsonl")
        gone = {"alive_at_term": True, "group_gone_after_term": True}
        v = lambda rc, out=clean, err="": ev.eval_sx6(dict(gone, rc=rc), out, err, "kiro")[:2]
        self.assertEqual(v(-15), ("pass", None))
        self.assertEqual(v(143), ("pass", None))
        self.assertEqual(v(-6), ("fail", "unexpected_signal"))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=-6), clean, "", "kiro")[2]["signal"], 6)
        self.assertEqual(v(-9), ("fail", "unexpected_signal"))
        self.assertEqual(v(137), ("fail", "unexpected_signal"))
        self.assertEqual(v(130), ("fail", "unexpected_signal"))
        self.assertEqual(v(-2), ("fail", "unexpected_signal"))
        self.assertEqual(v(None), ("fail", "no_rc"))
        self.assertEqual(v(None, auth), ("fail", "no_rc"))
        self.assertEqual(v(0), ("pass", None))  # handled SIGTERM, exited itself; no runError
        self.assertEqual(v(0, auth), ("fail", "exited_with_error"))
        self.assertEqual(v(-15, auth), ("pass", None))

    def test_sx6_stub_ignores_sigterm_then_aborts(self) -> None:
        """Item 1: a stub that ignores SIGTERM and then calls os.abort() (rc -6) -> fail(unexpected_signal)."""
        s = self.session(STUB_MODE="term_abort")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        c = {x["id"]: x for x in s.cases}["S-X-6"]
        self.assertEqual((c["status"], c["reason"]), ("fail", "unexpected_signal"))
        self.assertEqual((c["observation"]["rc"], c["observation"]["signal"]), (-6, 6))
        self.assertTrue(c["observation"]["alive_at_term"])


if __name__ == "__main__":
    unittest.main()
