"""Offline tests for the vibe smoke cases (m3-smoke-agy-vibe; ACC: N11, N12, N24, N25).

A stub ``vibe`` written under /tmp/mh-smoke-v-* prints lines in the vibe 3.1.0
``--output streaming`` shape of tests/fixtures/real/vibe/3.1.0/streaming-terminated.jsonl
(``message`` lines with role user / assistant and text parts, ``effect`` tool lines with a
``state``; every line carries ``sessionId``) and writes ``<--output-dir>/export.json`` in the
export-terminated.json shape (outcome, stop_reason, error, session_id, usage, config.active_model,
exit_code). Its denied effect copies streaming-terminated.jsonl:5 (status "failed", decision
"skip", error code "tool_denied"; that real denial was outside_workdir, the stub reuses the shape
for `touch y.txt`). It reacts to the argv VibeAdapter.build() makes. Every env is a dict; os.environ never gets MESH_SMOKE* or MESH_PROBE*. No real
vendor binary runs.
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
from mesh.adapters.vibe import VibeAdapter
from tests import tierlib
from tests.smoke import harness
from tests.smoke import reevaluate as ev
from tests.smoke import test_smoke as cases
from tests.stubs import make_executable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "real", "vibe", "3.1.0")

STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
with open(CALLS, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("vibe 3.1.0"); sys.exit(0)
if "--prompt-file" not in args:
    sys.exit(99)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
mode = os.environ.get("STUB_MODE", "ok")
with open(opt("--prompt-file"), encoding="utf-8") as f:
    prompt = f.read()
sid = opt("--resume") or "vibe-stub-session-3"
turn = "turn-stub-1"
n = [0]
def emit(obj):
    obj = dict({"id": "%s-%d" % (obj.get("type"), n[0]), "sessionId": sid, "turnId": turn, "inputEntryId": None,
                "createdAt": 1794143618897 + n[0], "updatedAt": 1794143618897 + n[0], "generationStatus": "completed",
                "relatedEntryId": None}, **obj)
    n[0] += 1
    print(json.dumps(obj), flush=True)
def message(role, text, source):
    emit({"type": "message", "role": role, "content": [{"type": "text", "text": text}], "source": source,
          "userDisplayContent": None, "outcome": None if role == "user" else {"type": "committed"}})
def effect(command, denied=False):
    detail = {"toolName": "file_system.bash",
              "display": {"summary": "bash: " + command, "content": None, "suffix": "", "verb": "Running",
                          "message": command, "settledVerb": "Ran", "settledMessage": command,
                          "statusText": "Running command"},
              "kind": "shell", "input": {"command": command}}
    if not denied:  # streaming-terminated.jsonl:3
        state = {"status": "completed", "outputText": "", "decision": "execute",
                 "display": {"success": True, "verb": "Ran", "message": command, "warnings": [],
                             "approvalNote": None, "suffix": ""},
                 "output": {"stdout": "", "stderr": "", "output": "", "truncated": False},
                 "approvalSource": "config", "approvalType": "always", "durationMs": 0.0}
    else:  # streaming-terminated.jsonl:5
        msg = "Access denied: the ask agent does not run " + command + " without approval."
        state = {"status": "failed", "outputText": "", "decision": "skip",
                 "error": {"message": msg, "code": "tool_denied", "details": None},
                 "display": {"success": False, "verb": "", "message": msg, "warnings": [],
                             "approvalNote": None, "suffix": ""},
                 "output": {"type": "failure", "content": [],
                            "_meta": {"approval": {"approvalSource": "never", "approvalType": "never",
                                                   "decision": "skip"}},
                            "error": {"code": "tool_denied", "message": msg, "retryable": False}},
                 "approvalSource": "never", "approvalType": "never", "durationMs": 0.0}
    emit({"type": "effect", "title": "file_system.bash", "detail": detail, "state": state})
message("user", prompt, "turn_start")
if "Count from 1" in prompt:
    for i in range(1, 100000):
        message("assistant", str(i) + "\n", "harness")
        time.sleep(0.01)
    sys.exit(0)
outcome, stop, error, exit_code = "completed", "end_turn", None, 0
if "touch y.txt" in prompt or "x.txt" in prompt:
    effect("pwd")
    effect("touch y.txt" if "touch y.txt" in prompt else "echo hello > x.txt", denied=True)
    text = "I could not run that command."
elif "--resume" in args:
    text = "I replied with MESH_SMOKE_OK."
else:
    text = "MESH_SMOKE_OK"
if mode == "outcome_error":
    text, outcome, stop, error, exit_code = "", "error", "error", None, 0
message("assistant", text, "harness")
export = {"outcome": outcome, "stop_reason": stop, "error": error, "session_id": sid,
          "usage": {"input_tokens": 60614, "output_tokens": 381, "cached_input_tokens": 38400, "total_tokens": 60995},
          "cost_usd": 0.0094254, "steps": 2, "config": {"active_model": opt("--model")}, "schema_version": 1,
          "vibe_version": "3.1.0", "exit_code": exit_code}
os.makedirs(opt("--output-dir"), exist_ok=True)
with open(os.path.join(opt("--output-dir"), "export.json"), "w", encoding="utf-8") as f:
    json.dump(export, f)
sys.exit(exit_code)
'''


class VibeCasesBase(unittest.TestCase):
    MODEL = "ds-flash"

    def setUp(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])
        self.tmp = tempfile.mkdtemp(prefix="mh-smoke-v-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "calls.log")
        os.makedirs(os.path.join(self.tmp, "bin"))
        self.binary = os.path.join(self.tmp, "bin", "vibe")
        first, rest = STUB.split("\n", 1)
        with open(self.binary, "w", encoding="utf-8") as f:
            f.write(first + f"\nCALLS = {self.calls!r}\n" + rest)
        make_executable(self.binary)
        self.out = os.path.join(self.tmp, "out")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MESH_SMOKE": "1",
                    "MESH_SMOKE_ADAPTERS": "vibe", "MESH_SMOKE_CONSENT": "vibe", "MESH_SMOKE_MAX_RUNS": "6",
                    "MESH_SMOKE_MODEL_VIBE": self.MODEL, "MESH_SMOKE_TIMEOUT_S": "20",
                    "MESH_SMOKE_OUT": self.out, "MESH_SMOKE_CONSENT_REF": "offline stub"}

    def tearDown(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])

    def session(self, **env) -> harness.SmokeSession:
        s = harness.SmokeSession(dict(self.env, **env), "vibe", self.binary, grace_s=1, version="3.1.0",
                                 mesh_commit="abc1234")
        self.addCleanup(s.cleanup)
        return s

    def model_runs(self) -> list:
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as f:
            return [a for a in map(json.loads, f.read().splitlines()) if "--prompt-file" in a]

    @staticmethod
    def by_id(doc) -> dict:
        return {c["id"]: c for c in doc["cases"]}


class VibeFlowTest(VibeCasesBase):
    def test_all_runs_results_and_reevaluate(self) -> None:
        """ACC: N11, N12, N24 - R1..R6 against the vibe stub: readonly argv (--agent ask, never auto-approve or --trust), the smoke model (omitted on --resume), --output-dir in the run dir; S-X-1/2/3/6 pass, S-X-4 observed(not_observable), S-X-5 not_applicable (no effort flag, nothing spawned), S-X-7 observed, S-VIBE-8 deny with one failed/denied effect; results validate and list VIBE_CASES; reevaluate re-parses S-X-1/2/4 without spawning."""
        s = self.session()
        sid = cases.run_r1(s)
        self.assertEqual(sid, "vibe-stub-session-3")
        cases.run_r2(s, sid)
        cases.run_r3(s, "low")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        cases.run_r5(s)
        cases.run_r6(s)
        runs = self.model_runs()
        self.assertEqual(len(runs), 5)  # R3 refused by build(), no spawn
        for argv in runs:
            self.assertEqual(argv[argv.index("--output") + 1], "streaming")
            self.assertEqual(argv[argv.index("--agent") + 1], "ask")
            self.assertNotIn("--auto-approve", argv)
            self.assertNotIn("--trust", argv)
            self.assertNotIn("--effort", argv)
            self.assertTrue(argv[argv.index("--output-dir") + 1].endswith("/vibe-out"))
        self.assertEqual(runs[0][runs[0].index("--model") + 1], self.MODEL)
        self.assertEqual(runs[1][runs[1].index("--resume") + 1], "vibe-stub-session-3")
        self.assertNotIn("--model", runs[1])
        path = cases.finish_stage(s)
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        self.assertEqual(doc["adapter"], "vibe")
        self.assertEqual([x["id"] for x in doc["cases"]], list(cases.VIBE_CASES))
        c = self.by_id(doc)
        self.assertEqual(c["S-X-1"]["status"], "pass")
        self.assertEqual((c["S-X-2"]["status"], c["S-X-2"]["observation"]["session_id"]), ("pass", "vibe-stub-session-3"))
        self.assertIsNone(c["S-X-2"]["observation"]["init_cwd"])
        self.assertEqual(c["S-X-3"]["status"], "pass")
        self.assertEqual((c["S-X-4"]["status"], c["S-X-4"]["reason"]), ("observed", "not_observable"))
        self.assertEqual((c["S-X-5"]["status"], c["S-X-5"]["reason"]), ("not_applicable", "effort_refused_by_adapter"))
        self.assertEqual(c["S-X-5"]["observation"]["error"], "EFFORT_UNSUPPORTED")
        self.assertEqual(c["S-X-6"]["status"], "pass")
        self.assertGreaterEqual(c["S-X-6"]["observation"]["lines_at_term"], cases.SIGNAL_MIN_LINES["vibe"])
        self.assertEqual(c["S-X-7"]["status"], "observed")
        self.assertFalse(c["S-X-7"]["observation"]["file_created"])
        v8 = c["S-VIBE-8"]["observation"]
        self.assertEqual((v8["outcome"], v8["y_txt_exists"], v8["failed_effects"], v8["denied_effects"]),
                         ("deny", False, 1, 1))
        self.assertNotIn("S-VIBE-9", c)
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
        for cid in ("S-X-1", "S-X-2", "S-X-4"):
            self.assertEqual(nc[cid]["evaluated_from"], src, cid)
            self.assertEqual(nc[cid]["status"], c[cid]["status"], cid)
        for cid in ("S-X-3", "S-X-6", "S-X-7", "S-VIBE-8"):
            self.assertEqual(nc[cid]["carried_from"], src, cid)
        self.assertEqual(nc["S-X-5"]["status"], "not_applicable")

    def test_outcome_error_is_vendor_error(self) -> None:
        """ACC: N11, N6 - rc 0 with export outcome "error" (error null): S-X-1 fail(vendor_error, run_error); no session id handed to R2."""
        s = self.session(STUB_MODE="outcome_error", MESH_SMOKE_MAX_RUNS="1")
        sid = cases.run_r1(s)
        c = {x["id"]: x for x in s.cases}
        self.assertEqual((c["S-X-1"]["status"], c["S-X-1"]["reason"]), ("fail", "vendor_error"))
        self.assertEqual(c["S-X-1"]["observation"]["vendor_exit_meaning"], "run_error")
        self.assertEqual(c["S-X-1"]["rc"], 0)
        self.assertIsNone(sid)
        self.assertEqual(len(self.model_runs()), 1)

    def test_signal_after_min_lines(self) -> None:
        """ACC: N11 - S-X-6 waits for SIGNAL_MIN_LINES["vibe"] (2: the user message line comes before the model) stdout lines, then the group is gone."""
        self.assertEqual(cases.SIGNAL_MIN_LINES["vibe"], 2)
        s = self.session(MESH_SMOKE_MAX_RUNS="1")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        c = {x["id"]: x for x in s.cases}
        info = c["S-X-6"]["observation"]
        self.assertEqual(c["S-X-6"]["status"], "pass")
        self.assertTrue(info["alive_at_term"])
        self.assertGreaterEqual(info["lines_at_term"], 2)

    def test_effort_none_is_not_listed(self) -> None:
        """ACC: N11 - MESH_SMOKE_EFFORT_VIBE=none keeps the existing not_applicable(effort_not_listed) path."""
        s = self.session()
        cases.run_r3(s, "none")
        self.assertEqual((s.cases[-1]["status"], s.cases[-1]["reason"]), ("not_applicable", "effort_not_listed"))
        self.assertEqual(self.model_runs(), [])

    def test_vibe_tier_skips_without_gate(self) -> None:
        """ACC: N25 - without MESH_SMOKE=1 VibeSmoke skips and spawns nothing."""
        self.assertNotEqual(os.environ.get("MESH_SMOKE"), "1")
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(cases.VibeSmoke).run(result)
        self.assertEqual(result.failures + result.errors, [])
        self.assertTrue(result.skipped)
        self.assertEqual(self.model_runs(), [])


class VibeStreamFactsTest(unittest.TestCase):
    def _fixture(self, name: str) -> str:
        path = os.path.join(FIXTURES, name)
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(tierlib.read_sidecar(path)["sha256"], tierlib.sha256_bytes(data))
        return data.decode("utf-8")

    def test_stream_facts_real_fixture(self) -> None:
        """ACC: N24 - VibeAdapter.stream_facts on streaming-terminated.jsonl: the sessionId; no model, effort or cwd in the stream; reevaluate dispatches to it."""
        text = self._fixture("streaming-terminated.jsonl")
        f = VibeAdapter.stream_facts(text)
        self.assertEqual(f, {"session_id": "d76f6107-47ca-badd-301b-f7348b4930a2", "model": None, "effort": None,
                             "cwd": None, "stream_error": None, "vendor_exit_meaning": None})
        self.assertEqual(ev.stream_facts(text, "vibe"), f)
        # PR #12 round-2 S-X-6 rule: vibe's stream has no run-level error, so rc 0 after the
        # SIGTERM passes and rc > 0 with stderr fails, as for any adapter without a stream_error
        gone = {"alive_at_term": True, "group_gone_after_term": True}
        self.assertEqual(ev.eval_sx6(dict(gone, rc=0), text, "", "vibe")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=-15), text, "x\n", "vibe")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=1), text, "boom\n", "vibe")[:2], ("fail", "exited_with_error"))
        # lines 5 and 7: outside_workdir denials (status failed, error code tool_denied)
        self.assertEqual(cases.vibe_effect_failures(text), {"failed_effects": 2, "denied_effects": 2})
        self.assertEqual(cases.vibe_effect_failures(""), {"failed_effects": 0, "denied_effects": 0})

    def test_stream_facts_edges(self) -> None:
        """ACC: N24 - empty, garbage and truncated input never raise; the first non-empty sessionId wins."""
        empty = {"session_id": None, "model": None, "effort": None, "cwd": None, "stream_error": None,
                 "vendor_exit_meaning": None}
        self.assertEqual(VibeAdapter.stream_facts(""), empty)
        self.assertEqual(VibeAdapter.stream_facts(None), empty)  # type: ignore[arg-type]
        self.assertEqual(VibeAdapter.stream_facts('garbage\n[1]\n{"sessionId": "tr'), empty)
        self.assertEqual(VibeAdapter.stream_facts('{"sessionId": ""}\n{"sessionId": "a"}\n{"sessionId": "b"}')["session_id"], "a")


if __name__ == "__main__":
    unittest.main()
