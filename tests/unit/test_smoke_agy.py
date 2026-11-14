"""Offline tests for the agy smoke cases (m3-smoke-agy-vibe; ACC: N11, N12, N24, N25).

A stub ``agy`` written under /tmp/mh-smoke-a-* prints lines in the agy 1.5.3
``--print --output-format stream-json`` shape of tests/fixtures/real/agy/1.5.3/ (``init`` with
conversation_id / model / cwd, the ``user_input`` and ``agent_response`` step_update events,
a ``tool`` step that ends in state ERROR as in stream-fixC-denied.jsonl, the ``result`` event,
and the ``AGY_ERROR: {json}`` stderr line of stream-step6-network-error-stderr.txt). It reacts
to the argv AgyAdapter.build() makes. agy 1.5.3 left ``result.response`` empty and put no text
on any ``agent_response`` step in every real capture. By default the stub fills
``result.response`` via ``stub_agy_response_hypothetical()`` so the R1 pass path can be exercised:
that is HYPOTHETICAL behaviour no real fixture shows. ``STUB_SHAPE=real_1_5_3`` mirrors the real
shape (response "", reply text nowhere) and AgyRealShapeTest pins what S-X-1 records then.
Every env is a dict;
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
from mesh.adapters.agy import AgyAdapter
from tests import tierlib
from tests.smoke import harness
from tests.smoke import reevaluate as ev
from tests.smoke import test_smoke as cases
from tests.stubs import make_executable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "real", "agy", "1.5.3")

STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
with open(CALLS, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("1.5.3"); sys.exit(0)
if "--print" not in args:
    sys.exit(99)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
mode = os.environ.get("STUB_MODE", "ok")
prompt = args[-1]
cid = opt("--conversation") or "agy-stub-conv-5"
def emit(obj):
    print(json.dumps(obj), flush=True)
def step(i, step_type, state, **kw):
    body = {"conversation_id": cid, "step_index": i, "state": state, "step_type": step_type}
    body.update(kw)
    emit({"event": "step_update", "step_update": body})
usage = {"input_tokens": 7875, "output_tokens": 1172, "thinking_tokens": 1124, "cache_read_tokens": 0, "total_tokens": 9047}
emit({"event": "init", "conversation_id": cid, "init": {"model": opt("--model"), "cwd": os.getcwd(),
      "tools": ["ask_permission", "run_command", "view_file"], "permission_mode": "request-review"}})
step(0, "user_input", "DONE")
if "Count from 1" in prompt:
    for i in range(1, 100000):
        step(i, "agent_response", "DONE", duration_seconds=0.01, usage=usage)
        time.sleep(0.01)
    sys.exit(0)
step(1, "agent_response", "DONE", duration_seconds=9.203089568, usage=usage)
if mode == "no_result":
    sys.stderr.write("error: agent executor error: generating and executing: request failed: net/http: cannot rewind body after connection loss\n")
    sys.stderr.write('AGY_ERROR: {"short_error":"agent executor error: generating and executing: request failed: net/http: cannot rewind body after connection loss","status":"UNKNOWN","error_code":2,"code_kind":"grpc","retryable":false,"error_id":"stub-101"}\n')
    sys.exit(0)
def stub_agy_response_hypothetical(text):
    # HYPOTHETICAL: real agy 1.5.3 captures all have result.response "" (impl-notes-m3-agyparse.md).
    return text
denied = []
response = "MESH_SMOKE_OK"
if "touch y.txt" in prompt or "x.txt" in prompt:
    cmd = "touch y.txt" if "touch y.txt" in prompt else "echo hello > x.txt"
    step(2, "tool", "ACTIVE", tool_name="run_command", tool_info={"name": "run_command", "parameters": {"CommandLine": cmd}})
    step(2, "tool", "ERROR", tool_name="run_command", duration_seconds=0.037148713,
         tool_info={"name": "run_command", "parameters": {"CommandLine": cmd},
                    "error": {"type": "TOOL_ERROR", "message": "permission check failed for unsandboxed \"" + cmd + "\": user denied permission to run command:\n" + cmd}})
    denied = [{"action": "command", "display_name": "RunCommand"}]
    response = "I could not run that command."
elif "--conversation" in args:
    response = "I replied with MESH_SMOKE_OK."
if os.environ.get("STUB_SHAPE") == "real_1_5_3":
    response = ""  # the real 1.5.3 shape: no reply text in result.response nor on any step
else:
    response = stub_agy_response_hypothetical(response)
result = {"conversation_id": cid, "status": "SUCCESS", "response": response, "duration_seconds": 11.693950816,
          "num_turns": 1, "usage": {"input_tokens": 17263, "output_tokens": 1233, "thinking_tokens": 1135,
                                    "cache_read_tokens": 0, "total_tokens": 18496}}
if mode == "result_error":
    result.update({"status": "ERROR", "response": "", "finished_reason": "error",
                   "error": "agent executor error: generating and executing: request failed"})
if denied:
    result["denied_actions"] = denied
emit({"event": "result", "result": result})
sys.exit(0)
'''


class AgyCasesBase(unittest.TestCase):
    MODEL = "claude-sonnet-4-6"

    def setUp(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])
        self.tmp = tempfile.mkdtemp(prefix="mh-smoke-a-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "calls.log")
        os.makedirs(os.path.join(self.tmp, "bin"))
        self.binary = os.path.join(self.tmp, "bin", "agy")
        first, rest = STUB.split("\n", 1)
        with open(self.binary, "w", encoding="utf-8") as f:
            f.write(first + f"\nCALLS = {self.calls!r}\n" + rest)
        make_executable(self.binary)
        self.out = os.path.join(self.tmp, "out")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MESH_SMOKE": "1",
                    "MESH_SMOKE_ADAPTERS": "agy", "MESH_SMOKE_CONSENT": "agy", "MESH_SMOKE_MAX_RUNS": "6",
                    "MESH_SMOKE_MODEL_AGY": self.MODEL, "MESH_SMOKE_TIMEOUT_S": "20",
                    "MESH_SMOKE_OUT": self.out, "MESH_SMOKE_CONSENT_REF": "offline stub"}

    def tearDown(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])

    def session(self, **env) -> harness.SmokeSession:
        s = harness.SmokeSession(dict(self.env, **env), "agy", self.binary, grace_s=1, version="1.5.3",
                                 mesh_commit="abc1234")
        self.addCleanup(s.cleanup)
        return s

    def model_runs(self) -> list:
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as f:
            return [a for a in map(json.loads, f.read().splitlines()) if "--print" in a]

    @staticmethod
    def by_id(doc) -> dict:
        return {c["id"]: c for c in doc["cases"]}


class AgyFlowTest(AgyCasesBase):
    def test_all_runs_results_and_reevaluate(self) -> None:
        """ACC: N11, N12, N24 - R1..R6 against the agy stub (S-X-1 pass relies on the HYPOTHETICAL stub_agy_response_hypothetical; see AgyRealShapeTest): readonly argv (--mode plan, never --dangerously-skip-permissions), the smoke model, --conversation, --effort; S-X-1/2/3/4/6 pass, S-X-5 observed(not_echoed), S-X-7 observed, S-AGY-8 deny with the ERROR tool step and denied_actions; results validate and list AGY_CASES; reevaluate re-parses S-X-1/2/4/5 without spawning."""
        s = self.session()
        sid = cases.run_r1(s)
        self.assertEqual(sid, "agy-stub-conv-5")
        cases.run_r2(s, sid)
        cases.run_r3(s, "low")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        cases.run_r5(s)
        cases.run_r6(s)
        runs = self.model_runs()
        self.assertEqual(len(runs), 6)
        for argv in runs:
            self.assertEqual(argv[:3], ["--print", "--output-format", "stream-json"])
            self.assertEqual(argv[argv.index("--model") + 1], self.MODEL)
            self.assertEqual(argv[argv.index("--mode") + 1], "plan")
            self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(runs[1][runs[1].index("--conversation") + 1], "agy-stub-conv-5")
        self.assertEqual(runs[2][runs[2].index("--effort") + 1], "low")
        path = cases.finish_stage(s)
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        self.assertEqual(doc["adapter"], "agy")
        self.assertEqual([x["id"] for x in doc["cases"]], list(cases.AGY_CASES))
        c = self.by_id(doc)
        self.assertEqual(c["S-X-1"]["status"], "pass")
        self.assertEqual((c["S-X-2"]["status"], c["S-X-2"]["observation"]["session_id"]), ("pass", "agy-stub-conv-5"))
        self.assertEqual(c["S-X-2"]["observation"]["init_cwd"], c["S-X-2"]["observation"]["workspace"])
        self.assertEqual(c["S-X-3"]["status"], "pass")
        self.assertEqual((c["S-X-4"]["status"], c["S-X-4"]["observation"]["init_model"]), ("pass", self.MODEL))
        self.assertEqual((c["S-X-5"]["status"], c["S-X-5"]["reason"]), ("observed", "not_echoed"))
        self.assertEqual(c["S-X-6"]["status"], "pass")
        self.assertGreaterEqual(c["S-X-6"]["observation"]["lines_at_term"], cases.SIGNAL_MIN_LINES["agy"])
        self.assertEqual(c["S-X-7"]["status"], "observed")
        self.assertFalse(c["S-X-7"]["observation"]["file_created"])
        a8 = c["S-AGY-8"]["observation"]
        self.assertEqual((a8["outcome"], a8["y_txt_exists"], a8["tool_errors"], a8["denied_actions"]),
                         ("deny", False, 1, 1))
        self.assertNotIn("S-DROID-8", c)
        self.assertNotIn("S-KIRO-8", c)
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
        for cid in ("S-X-3", "S-X-6", "S-X-7", "S-AGY-8"):
            self.assertEqual(nc[cid]["carried_from"], src, cid)
        self.assertEqual(tierlib.read_sidecar(new_path)["command"][:3], ["python3", "-m", "tests.smoke.reevaluate"])

    def test_no_result_with_agy_error_is_vendor_error(self) -> None:
        """ACC: N11, N6 - rc 0, no result event, AGY_ERROR on stderr: S-X-1 fail(vendor_error, run_error); no session id handed to R2."""
        s = self.session(STUB_MODE="no_result", MESH_SMOKE_MAX_RUNS="1")
        sid = cases.run_r1(s)
        c = {x["id"]: x for x in s.cases}
        self.assertEqual((c["S-X-1"]["status"], c["S-X-1"]["reason"]), ("fail", "vendor_error"))
        self.assertEqual(c["S-X-1"]["observation"]["vendor_exit_meaning"], "run_error")
        self.assertEqual(c["S-X-1"]["rc"], 0)
        self.assertIsNone(sid)
        self.assertEqual(len(self.model_runs()), 1)

    def test_result_error_is_vendor_error(self) -> None:
        """ACC: N11 - rc 0 with a result status ERROR (stream-step6-network-error.jsonl shape): S-X-1 fail(vendor_error)."""
        s = self.session(STUB_MODE="result_error", MESH_SMOKE_MAX_RUNS="1")
        self.assertIsNone(cases.run_r1(s))
        c = {x["id"]: x for x in s.cases}
        self.assertEqual((c["S-X-1"]["status"], c["S-X-1"]["reason"]), ("fail", "vendor_error"))

    def test_signal_after_min_lines(self) -> None:
        """ACC: N11 - S-X-6 waits for SIGNAL_MIN_LINES["agy"] (3: init and user_input come before the model) stdout lines, then the group is gone."""
        self.assertEqual(cases.SIGNAL_MIN_LINES["agy"], 3)
        s = self.session(MESH_SMOKE_MAX_RUNS="1")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        c = {x["id"]: x for x in s.cases}
        info = c["S-X-6"]["observation"]
        self.assertEqual(c["S-X-6"]["status"], "pass")
        self.assertTrue(info["alive_at_term"])
        self.assertGreaterEqual(info["lines_at_term"], 3)

    def test_effort_suffixed_model_refused_without_spawn(self) -> None:
        """ACC: N11 - a model id ending in an effort suffix plus --effort: build() refuses, S-X-5 not_applicable(effort_refused_by_adapter), nothing spawned."""
        s = self.session(MESH_SMOKE_MODEL_AGY="gemini-3.8-flash-high")
        cases.run_r3(s, "low")
        c = {x["id"]: x for x in s.cases}
        self.assertEqual((c["S-X-5"]["status"], c["S-X-5"]["reason"]), ("not_applicable", "effort_refused_by_adapter"))
        self.assertEqual(c["S-X-5"]["observation"]["error"], "EFFORT_NOT_ALLOWED")
        self.assertEqual(self.model_runs(), [])
        self.assertEqual(s.runs_used, 0)
        # Quest #17 decision 2: the reason code must pass the results-schema validation.
        # smoke_schema has no allowed-reasons list (any non-empty string is accepted for a
        # non-pass status), so the check is that the full results doc validates.
        with open(cases.finish_stage(s), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        self.assertEqual((self.by_id(doc)["S-X-5"]["status"], self.by_id(doc)["S-X-5"]["reason"]),
                         ("not_applicable", "effort_refused_by_adapter"))

    def test_agy_tier_skips_without_gate(self) -> None:
        """ACC: N25 - without MESH_SMOKE=1 AgySmoke skips and spawns nothing."""
        self.assertNotEqual(os.environ.get("MESH_SMOKE"), "1")
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(cases.AgySmoke).run(result)
        self.assertEqual(result.failures + result.errors, [])
        self.assertTrue(result.skipped)
        self.assertEqual(self.model_runs(), [])


class AgyRealShapeTest(AgyCasesBase):
    def test_real_shape_no_final_text(self) -> None:
        """ACC: N11 - STUB_SHAPE=real_1_5_3 (result.response "", no text on any agent_response step, as in every real 1.5.3 capture): parse() finds no final text, so S-X-1 records fail(parse_pending) with raw_token_seen False; S-X-2 still passes from the init conversation_id and R1 still hands the session id on (no vendor error). This is what a real agy smoke would record today."""
        s = self.session(STUB_SHAPE="real_1_5_3", MESH_SMOKE_MAX_RUNS="1")
        sid = cases.run_r1(s)
        c = {x["id"]: x for x in s.cases}
        self.assertEqual((c["S-X-1"]["status"], c["S-X-1"]["reason"]), ("fail", "parse_pending"))
        self.assertEqual(c["S-X-1"]["observation"], {"raw_token_seen": False})
        self.assertEqual(c["S-X-1"]["rc"], 0)
        self.assertEqual(c["S-X-2"]["status"], "pass")
        self.assertEqual(sid, "agy-stub-conv-5")
        self.assertEqual(len(self.model_runs()), 1)


class AgyStreamFactsTest(unittest.TestCase):
    def _fixture(self, name: str) -> str:
        path = os.path.join(FIXTURES, name)
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(tierlib.read_sidecar(path)["sha256"], tierlib.sha256_bytes(data))
        return data.decode("utf-8")

    def test_stream_facts_real_fixtures(self) -> None:
        """ACC: N24 - AgyAdapter.stream_facts on the real 1.5.3 captures: init conversation_id, model, cwd; no effort in the stream; reevaluate dispatches to it."""
        for name, sid, err in (("stream-step1-denied.jsonl", "464ae741-b883-48de-998c-9fc7ba5567c9", None),
                               ("stream-fixC-denied.jsonl", "1010b608-f183-4095-9760-28ede28cc378", None),
                               ("stream-step6-network-error.jsonl", "847d0441-9d3a-4805-aec0-8877153bae15",
                                "run_error")):
            text = self._fixture(name)
            f = AgyAdapter.stream_facts(text)
            self.assertEqual(f, {"session_id": sid, "model": "gemini-3.8-flash-high", "effort": None,
                                 "cwd": "/workspace/mesh-fresh-v1", "stream_error": err,
                                 "vendor_exit_meaning": err}, name)
            self.assertEqual(ev.stream_facts(text, "agy"), f, name)

    def test_stream_error_and_vendor_error_rules(self) -> None:
        """ACC: N24 - PR #12 round-2 rules extended to agy: an explicit error in the LAST result event is
        stream_error (S-X-6 fail at rc 0..128, pass at -15/143); no result event is vendor_exit_meaning
        run_error (S-X-2/4/5 fail(vendor_error) at rc 0) but not a stream_error (S-X-6 pass)."""
        net = self._fixture("stream-step6-network-error.jsonl")
        ok = self._fixture("stream-step1-denied.jsonl")
        init = '{"event":"init","conversation_id":"c1","init":{"model":"gemini-3.8-flash-high"}}\n'
        f = AgyAdapter.stream_facts
        self.assertEqual(f(init)["stream_error"], None)
        self.assertEqual(f(init)["vendor_exit_meaning"], "run_error")
        self.assertEqual(f(init + '{"event":"result","result":{"status":"SUCCESS","error":"boom"}}')["stream_error"],
                         "run_error")
        self.assertEqual(f(init + '{"event":"result","result":{"status":"ERROR"}}\n'
                           '{"event":"result","result":{"status":"SUCCESS"}}')["stream_error"], None)  # last wins
        self.assertEqual(f(init + '{"event":"result","result":{"status":"SUCCESS"}}\n'
                           '{"event":"result","result":{"status":"ERROR"}}')["stream_error"], "run_error")
        self.assertEqual(f(init + '{"event":"result","result":{"status":"SUCCESS","error":""}}')["vendor_exit_meaning"],
                         None)
        gone = {"alive_at_term": True, "group_gone_after_term": True}
        st = ev.eval_sx6(dict(gone, rc=0), net, "", "agy")
        self.assertEqual(st[:2], ("fail", "exited_with_error"))
        self.assertEqual(st[2]["vendor_exit_meaning"], "run_error")
        self.assertEqual(ev.eval_sx6(dict(gone, rc=-15), net, "", "agy")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=143), net, "", "agy")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=0), init, "", "agy")[:2], ("pass", None))  # cut stream
        self.assertEqual(ev.eval_sx6(dict(gone, rc=0), ok, "", "agy")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx4(net, 0, False, "gemini-3.8-flash-high", adapter_id="agy")[:2],
                         ("fail", "vendor_error"))
        self.assertEqual(ev.eval_sx4(init, 0, False, "gemini-3.8-flash-high", adapter_id="agy")[:2],
                         ("fail", "vendor_error"))
        self.assertEqual(ev.eval_sx4(ok, 0, False, "gemini-3.8-flash-high", adapter_id="agy")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx5(net, "", 0, False, "high", adapter_id="agy")[:2], ("fail", "vendor_error"))
        self.assertEqual(ev.eval_sx5(ok, "", 0, False, "high", adapter_id="agy")[:2], ("observed", "not_echoed"))
        self.assertEqual(ev.eval_sx2(net, 0, False, None, adapter_id="agy")[:2], ("fail", "vendor_error"))

    def test_stream_facts_edges(self) -> None:
        """ACC: N24 - empty, garbage and truncated input never raise; no init falls back to a step/result conversation_id."""
        empty = {"session_id": None, "model": None, "effort": None, "cwd": None, "stream_error": None,
                 "vendor_exit_meaning": "run_error"}
        self.assertEqual(AgyAdapter.stream_facts(""), empty)
        self.assertEqual(AgyAdapter.stream_facts(None), empty)  # type: ignore[arg-type]
        self.assertEqual(AgyAdapter.stream_facts("not json\n[1,2]\n{\"event\":\"init\",\"conv"), empty)
        self.assertEqual(AgyAdapter.stream_facts('{"event":"result","result":{"conversation_id":"r1"}}')["session_id"], "r1")
        line = '{"event":"init","conversation_id":"c","init":{"model":"m","reasoning_effort":"low"}}'
        self.assertEqual(AgyAdapter.stream_facts(line)["effort"], "low")

    def test_tool_denials_real_fixture(self) -> None:
        """ACC: N11 - S-AGY-8 helper on stream-fixC-denied.jsonl: one tool step in state ERROR, one denied action."""
        self.assertEqual(cases.agy_tool_denials(self._fixture("stream-fixC-denied.jsonl")),
                         {"tool_errors": 1, "denied_actions": 1})
        self.assertEqual(cases.agy_tool_denials(""), {"tool_errors": 0, "denied_actions": None})
        # Quest #17 P3: a result without denied_actions is None (not observed), not 0
        self.assertEqual(cases.agy_tool_denials('{"event":"result","result":{"status":"SUCCESS"}}'),
                         {"tool_errors": 0, "denied_actions": None})
        self.assertEqual(cases.agy_tool_denials('{"event":"result","result":{"denied_actions":[]}}'),
                         {"tool_errors": 0, "denied_actions": 0})


if __name__ == "__main__":
    unittest.main()
