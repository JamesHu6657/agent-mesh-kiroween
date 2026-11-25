"""Offline tests for the droid smoke cases and re-evaluation (m3-r3; ACC: N11, N12, N25, N24).

A stub ``droid`` written by this file under /tmp/mh-* reacts to the argv the adapter builds
(model, effort, session id, prompt file). The tests/stubs.py droid stub trips on ``exec``, so
it is not used for model runs. Every env is a dict; os.environ never gets MESH_SMOKE* or
MESH_PROBE*. Nothing touches tests/fixtures/real or tests/smoke/results.

codex, claude and devin (smoke-codex-claude-devin): stubs written under /tmp/mh-smoke-n-* react
to the argv CodexAdapter / ClaudeAdapter / DevinAdapter build. The codex and claude stubs read
the prompt from stdin and print the event shapes of mesh/adapters/codex.py "Event stream" and
mesh/adapters/claude.py "Stream" (as in tests/fixtures/synthetic/{codex,claude}/); the devin
stub reads ``--prompt-file`` and prints plain text with the ``ESC[?2004l`` prefix of
tests/fixtures/synthetic/devin/stdout_ok.txt and devin's real stderr wording. None of the three
has a real capture yet: these tests prove the harness paths, not the vendors' behaviour.
"""

import sys
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
import signal
import subprocess
import time

from mesh.adapters import evidence, smoke_schema
from mesh.adapters.base import ProfileUnsupported, RunRequest
from mesh.adapters.registry import ADAPTER_CLASSES
from mesh.core.proc import get_proc_stat
from tests import tierlib
from tests.smoke import harness
from tests.smoke import reevaluate as ev
from tests.smoke import test_smoke as cases
from tests.stubs import make_executable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTHETIC = os.path.join(REPO_ROOT, "tests", "fixtures", "synthetic", "smoke", "synthetic_droid_stream.jsonl")
SYNTH_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "synthetic")
NEW_ADAPTERS = ("codex", "claude", "devin")
CODEX_SID = "0199a213-81c0-7800-8aa1-bbab2a035a53"
CLAUDE_SID = "5f2b1c7e-0000-4000-8000-000000000001"
DEVIN_SECRET = "dk-PLANTEDsecretVALUE0123456789abcdef"

STUB = r'''#!/usr/bin/env python3
import json, os, signal, sys, time
args = sys.argv[1:]
with open(CALLS, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("droid 1.6.0"); sys.exit(0)
if not args or args[0] != "exec":
    sys.exit(99)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
mode = os.environ.get("STUB_MODE", "ok")
prompt = open(opt("--file"), encoding="utf-8").read()
if "Count from 1" in prompt and mode == "count_rc1":
    sys.exit(1)  # no output at all
if "Count from 1" in prompt and mode == "count_one":
    print("1", flush=True)
    sys.exit(0)
effort = opt("--reasoning-effort")
if effort and mode == "reject_effort":
    sys.stderr.write("error: reasoning effort not supported for this model\n"); sys.exit(2)
init = {"type": "system", "subtype": "init", "session_id": "stub-session-42", "model": opt("--model")}
if effort:
    init["reasoning_effort"] = effort
if mode != "noinit":
    print(json.dumps(init), flush=True)
if "Count from 1" in prompt:
    if mode == "term_then_err":
        def on_term(signum, frame):
            time.sleep(1)
            sys.stderr.write("API error: 429 rate limited\n"); sys.stderr.flush()
            os._exit(1)
        signal.signal(signal.SIGTERM, on_term)
    if mode == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open(PIDS, "w") as f:
            f.write(str(os.getpid()))
    for i in range(1, 100000):
        print(i, flush=True)
        time.sleep(0.01)
    sys.exit(0)
if "x.txt" in prompt:
    open("x.txt", "w").write("hello\n")
if "touch y.txt" in prompt:
    sys.stderr.write("permission denied: shell tool not allowed in readonly\n"); sys.exit(0)
text = "MESH_SMOKE_OK"
print(json.dumps({"type": "message", "role": "assistant", "text": text}))
print(json.dumps({"type": "result", "subtype": "success", "final_text": text, "session_id": "stub-session-42"}))
sys.exit(0)
'''


@unittest.skipUnless(sys.platform != "win32", 'Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only')
class CasesBase(unittest.TestCase):
    """Shared setup; no tests here."""

    def setUp(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])
        self.tmp = tempfile.mkdtemp(prefix="mh-smoke-c-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "calls.log")
        self.pids = os.path.join(self.tmp, "pids")
        os.makedirs(os.path.join(self.tmp, "bin"))
        self.binary = os.path.join(self.tmp, "bin", "droid")
        first, rest = STUB.split("\n", 1)
        with open(self.binary, "w", encoding="utf-8") as f:
            f.write(first + f"\nCALLS = {self.calls!r}\nPIDS = {self.pids!r}\n" + rest)
        make_executable(self.binary)
        self.assertTrue(self.binary.startswith("/tmp/mh-"))
        self.out = os.path.join(self.tmp, "out")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MESH_SMOKE": "1",
                    "MESH_SMOKE_ADAPTERS": "droid", "MESH_SMOKE_CONSENT": "droid", "MESH_SMOKE_MAX_RUNS": "6",
                    "MESH_SMOKE_MODEL_DROID": "claude-sonnet-5-5", "MESH_SMOKE_TIMEOUT_S": "20",
                    "MESH_SMOKE_OUT": self.out, "MESH_SMOKE_CONSENT_REF": "James 2026-11-09 12:44"}

    def tearDown(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])

    def session(self, **env) -> harness.SmokeSession:
        s = harness.SmokeSession(dict(self.env, **env), "droid", self.binary, grace_s=1, version="1.6.0",
                                 mesh_commit="abc1234")
        self.addCleanup(s.cleanup)
        return s

    def model_runs(self) -> list:
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as f:
            return [a for a in map(json.loads, f.read().splitlines()) if a[:1] == ["exec"]]

    def by_id(self, doc_or_cases) -> dict:
        cs = doc_or_cases["cases"] if isinstance(doc_or_cases, dict) else doc_or_cases
        return {c["id"]: c for c in cs}

    def load(self, path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)


@unittest.skipUnless(sys.platform != "win32", 'Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only')
class FlowTest(CasesBase):
    def test_all_runs_results_and_reevaluate(self) -> None:
        """ACC: N11, N12, N24 - R1..R6 against the stub: statuses per case (S-X-1 fail(parse_pending) with the stub parser, raw token seen), results validate, readonly argv; reevaluate re-parses S-X-1/2/4/5 (evaluated_from) and carries S-X-3/6/7/DROID-8 (carried_from) without spawning."""
        s = self.session()
        sid = cases.run_r1(s)
        self.assertEqual(sid, "stub-session-42")
        cases.run_r2(s, sid)
        cases.run_r3(s, "low")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        cases.run_r5(s)
        cases.run_r6(s)
        runs = self.model_runs()
        self.assertEqual(len(runs), 6)
        for argv in runs:
            self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
            self.assertNotIn("--auto", argv)
            self.assertNotIn("--skip-permissions-unsafe", argv)
        self.assertEqual(runs[1][runs[1].index("--session-id") + 1], "stub-session-42")
        self.assertEqual(runs[2][runs[2].index("--reasoning-effort") + 1], "low")
        path = cases.finish_stage(s)
        doc = self.load(path)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        c = self.by_id(doc)
        self.assertEqual([x["id"] for x in doc["cases"]], list(cases.DROID_CASES))
        self.assertEqual((c["S-X-1"]["status"], c["S-X-1"]["reason"]), ("fail", "parse_pending"))
        self.assertEqual(c["S-X-1"]["observation"], {"raw_token_seen": True})
        self.assertEqual(c["S-X-2"]["status"], "pass")
        self.assertEqual(c["S-X-2"]["observation"]["session_id"], "stub-session-42")
        self.assertEqual(c["S-X-4"]["status"], "pass")
        self.assertEqual(c["S-X-3"]["status"], "pass")
        self.assertEqual(c["S-X-5"]["status"], "pass")
        self.assertEqual(c["S-X-5"]["observation"]["requested_effort"], "low")
        self.assertEqual(c["S-X-6"]["status"], "pass")
        self.assertTrue(c["S-X-6"]["observation"]["first_byte"])
        self.assertTrue(c["S-X-6"]["observation"]["alive_at_term"])
        self.assertGreaterEqual(c["S-X-6"]["observation"]["lines_at_term"], 2)
        self.assertEqual(c["S-X-2"]["observation"]["workspace"][:14], "/tmp/mh-smoke-")
        self.assertIn("init_cwd", c["S-X-2"]["observation"])
        self.assertEqual(c["S-X-3"]["observation"]["cwd"], c["S-X-2"]["observation"]["workspace"])
        self.assertEqual(c["S-X-7"]["status"], "observed")
        self.assertTrue(c["S-X-7"]["observation"]["file_created"])
        self.assertEqual(c["S-DROID-8"]["observation"]["outcome"], "deny")
        self.assertIn("permission denied", c["S-DROID-8"]["observation"]["stderr_excerpt"])
        # Re-evaluation: never spawns, new Q4 name, sources recorded.
        before = len(self.model_runs())
        rout = os.path.join(self.tmp, "reeval")
        buf = io.StringIO()
        # Kiro r3 finding 7b: no process at all, not just no stub call.
        with redirect_stdout(buf), mock.patch("subprocess.Popen", side_effect=AssertionError("spawned")):
            rc = ev.main([path, "--out", rout])
        self.assertEqual(rc, 0, buf.getvalue())
        new_path = json.loads(buf.getvalue())["results"]
        self.assertEqual(len(self.model_runs()), before)
        self.assertNotEqual(os.path.basename(new_path), os.path.basename(path))
        new = self.load(new_path)
        self.assertEqual(tierlib.read_sidecar(new_path)["command"][:3], ["python3", "-m", "tests.smoke.reevaluate"])
        self.assertEqual(smoke_schema.validate(new, REPO_ROOT, fixture_root=self.out), [])
        nc = self.by_id(new)
        src = "tests/smoke/results/" + os.path.basename(path)
        for cid in ("S-X-1", "S-X-2", "S-X-4", "S-X-5"):
            self.assertEqual((nc[cid]["evaluated_from"], nc[cid]["carried_from"]), (src, None), cid)
        for cid in ("S-X-3", "S-X-6", "S-X-7", "S-DROID-8"):
            self.assertEqual((nc[cid]["carried_from"], nc[cid]["evaluated_from"]), (src, None), cid)
            self.assertEqual(nc[cid]["status"], c[cid]["status"])
        self.assertEqual(nc["S-X-1"]["status"], "fail")  # current droid parse() has no final_text yet
        self.assertEqual(nc["S-X-2"]["status"], "pass")

    def test_reevaluate_hang_dict(self) -> None:
        """ACC: N12 - a timed-out case whose group survived ({"value": "hang", "leftover_processes": true}) re-evaluates as fail(timeout), like a bare "hang" (Kiro r3 finding 7a)."""
        s = self.session(MESH_SMOKE_MAX_RUNS="1")
        cases.run_r1(s)
        path = cases.finish_stage(s, ["R1"])
        doc = self.load(path)
        for obs in ("hang", {"value": "hang", "leftover_processes": True}):
            d = json.loads(json.dumps(doc))
            for c in d["cases"]:
                if c["id"] == "S-X-1":
                    c["rc"], c["observation"] = None, obs
            new = self.by_id(ev.reevaluate_doc(d, os.path.basename(path), self.out))
            self.assertEqual((new["S-X-1"]["status"], new["S-X-1"]["reason"]), ("fail", "timeout"), obs)

    def test_two_stages_merge(self) -> None:
        """ACC: N11 - stage 1 (R1, MAX_RUNS=1) then stage 2 (R2, R3) in the same MESH_SMOKE_OUT: R2 reads R1's session id from the staged results; the stage-2 file (-2) holds all 8 cases, S-X-1 from stage 1, unrun ones not_run(not_selected)."""
        s1 = self.session(MESH_SMOKE_MAX_RUNS="1")
        cases.run_r1(s1)
        p1 = cases.finish_stage(s1)
        d1 = self.by_id(self.load(p1))
        self.assertEqual(d1["S-X-3"]["reason"], "not_selected")
        s2 = self.session(MESH_SMOKE_MAX_RUNS="5")
        self.assertEqual(cases.staged_session_id(s2), "stub-session-42")
        cases.run_r2(s2, cases.staged_session_id(s2))
        cases.run_r3(s2, "low")
        p2 = cases.finish_stage(s2)
        self.assertTrue(p2.endswith("-2.json"), p2)
        doc = self.load(p2)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        d2 = self.by_id(doc)
        self.assertEqual(d2["S-X-1"], dict(d1["S-X-1"], carried_from="tests/smoke/results/" + os.path.basename(p1)))
        self.assertEqual([c["id"] for c in doc["cases"]], list(cases.DROID_CASES))
        self.assertEqual(d2["S-X-3"]["status"], "pass")
        self.assertEqual(d2["S-X-5"]["status"], "pass")
        self.assertEqual(d2["S-X-6"]["reason"], "not_selected")
        self.assertEqual(len(self.model_runs()), 3)
        self.assertEqual(smoke_schema.load_latest(os.path.join(self.out, "results"), "droid"), p2)
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "tests", "fixtures", "real"))
        tierlib.promote(self.out, repo)
        self.assertEqual(smoke_schema.validate(doc, repo), [])


@unittest.skipUnless(sys.platform != "win32", 'Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only')
class CaseEdgeTest(CasesBase):
    def test_sigterm_ignoring_stub_fails_sx6(self) -> None:
        """ACC: N11, N25 - a SIGTERM-ignoring stub with a 1 s window (grace_s=1 session): S-X-6 fail(group_survived_sigterm); the group is SIGKILLed afterwards and gone."""
        s = self.session(STUB_MODE="ignore_term")
        cases.run_r4(s, gone_within_s=1, wait_first_byte_s=2)
        case = s.cases[-1]
        self.assertEqual((case["id"], case["status"], case["reason"]), ("S-X-6", "fail", "group_survived_sigterm"))
        self.assertTrue(case["observation"]["first_byte"])
        self.assertIsNone(case["observation"]["seconds_to_gone"])
        with open(self.pids, encoding="utf-8") as f:
            pid = int(f.read())
        st = get_proc_stat(pid)
        self.assertTrue(st is None or st["state"] in ("Z", "X"), st)

    def test_signal_run_guards(self) -> None:
        """ACC: N25 - run_and_signal keeps the r2b guards: a second S-X-6 in the same OUT is refused before spawning; an interrupt during the first-byte wait terminates the session and leaves no raw-* file."""
        s = self.session()
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        with self.assertRaises(harness.SmokeConfigError):
            cases.run_r4(s)
        self.assertEqual(len(self.model_runs()), 1)
        s2 = self.session(STUB_MODE="ignore_term", MESH_SMOKE_OUT=os.path.join(self.tmp, "out2"))
        real_sleep = harness.time.sleep
        pids_file = self.pids

        fired = []

        def interrupting_sleep(t):
            if not fired and os.path.exists(pids_file):
                fired.append(t)
                raise KeyboardInterrupt()
            real_sleep(t)

        with mock.patch.object(harness.time, "sleep", interrupting_sleep):
            with self.assertRaises(KeyboardInterrupt):
                s2.run_and_signal("S-X-6", cases.PROMPTS["R4"], wait_first_byte_s=30, gone_within_s=1)
        with open(self.pids, encoding="utf-8") as f:
            pid = int(f.read())
        st = get_proc_stat(pid)
        self.assertTrue(st is None or st["state"] in ("Z", "X"), st)
        raws = [n for _, _, fs in os.walk(s2.runs_root) for n in fs if n.startswith("raw-")]
        self.assertEqual(raws, [])

    def test_sx6_exited_before_signal(self) -> None:
        """ACC: N11, N25 - droid that exits before the SIGTERM (rc 1 with no output; one line then exit 0) gives S-X-6 fail(exited_before_signal), alive_at_term False, one model run each (Kiro r3 finding 1)."""
        real_getsize = harness.os.path.getsize

        def settled_getsize(p):
            size = real_getsize(p)
            if size and p.endswith("raw-stdout"):
                harness.time.sleep(0.5)  # let a stub that printed and exited finish exiting
            return size

        for n, mode in enumerate(("count_rc1", "count_one")):
            with self.subTest(mode=mode):
                s = self.session(STUB_MODE=mode, MESH_SMOKE_OUT=os.path.join(self.tmp, f"out-{mode}"))
                before = len(self.model_runs())
                with mock.patch.object(harness.os.path, "getsize", settled_getsize):
                    cases.run_r4(s, gone_within_s=2, wait_first_byte_s=5)
                case = s.cases[-1]
                self.assertEqual((case["id"], case["status"], case["reason"]), ("S-X-6", "fail", "exited_before_signal"))
                self.assertFalse(case["observation"]["alive_at_term"])
                self.assertEqual(case["observation"]["rc"], 1 if mode == "count_rc1" else 0)
                self.assertEqual(case["observation"]["first_byte"], mode == "count_one")
                self.assertEqual(len(self.model_runs()), before + 1)

    def test_sx6_exited_with_error(self) -> None:
        """ACC: N11, N25 - droid that ignores SIGTERM but exits 1 with a stderr API error 1 s later gives S-X-6 fail(exited_with_error), not pass; the SIGTERM came after the second stdout line; one model run (Kiro r5 finding 4)."""
        s = self.session(STUB_MODE="term_then_err")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        case = s.cases[-1]
        self.assertEqual((case["id"], case["status"], case["reason"]), ("S-X-6", "fail", "exited_with_error"))
        obs = case["observation"]
        self.assertTrue(obs["alive_at_term"])
        self.assertTrue(obs["group_gone_after_term"])
        self.assertEqual(obs["rc"], 1)
        self.assertGreaterEqual(obs["lines_at_term"], 2)
        self.assertIn("429", obs["stderr_excerpt"])
        self.assertEqual(len(self.model_runs()), 1)

    def test_sigterm_to_harness_kills_vendor(self) -> None:
        """ACC: N25 - with the smoke tier's handlers installed, SIGTERM to the harness process during a run becomes KeyboardInterrupt: the vendor session is killed and no raw-* stays (Kiro r3 finding 3), even when a second SIGTERM arrives during that cleanup (Kiro r5 finding 7); handlers are restored."""
        s = self.session(STUB_MODE="ignore_term")
        real_wait = subprocess.Popen.wait
        pids_file = self.pids

        fired = []
        real_signal_session = harness.signal_session

        def signal_session_twice(pgid, sig):
            real_signal_session(pgid, sig)
            if fired and sig == signal.SIGTERM:
                os.kill(os.getpid(), signal.SIGTERM)  # a second SIGTERM during cleanup (Kiro r5 finding 7)
                time.sleep(0.2)

        def wait_then_sigterm(proc, timeout=None):
            if timeout is not None and timeout > 5:
                fired.append(True)
                deadline = time.monotonic() + 10
                while not os.path.exists(pids_file) and time.monotonic() < deadline:
                    time.sleep(0.02)
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)  # the handler raises before this ends
                raise AssertionError("SIGTERM handler did not raise")
            return real_wait(proc, timeout=timeout)

        before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)
        previous = harness.install_interrupt_handlers()
        try:
            with mock.patch.object(subprocess.Popen, "wait", wait_then_sigterm), \
                    mock.patch.object(harness, "signal_session", signal_session_twice):
                with self.assertRaises(KeyboardInterrupt):
                    s.run("S-X-1", cases.PROMPTS["R4"])
        finally:
            harness.restore_interrupt_handlers(previous)
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)), before)
        with open(self.pids, encoding="utf-8") as f:
            pid = int(f.read())
        st = get_proc_stat(pid)
        self.assertTrue(st is None or st["state"] in ("Z", "X"), st)
        raws = [n for _, _, fs in os.walk(s.runs_root) for n in fs if n.startswith("raw-")]
        self.assertEqual(raws, [])

    def test_run_that_raised_is_blocked(self) -> None:
        """ACC: N11 - parse() raising after R1 spent its run: finish_stage marks S-X-1, S-X-2, S-X-4 blocked(harness_error), not not_selected; one model run (Kiro r3 finding 4)."""
        s = self.session()
        with mock.patch.object(s, "parse", side_effect=RuntimeError("parser blew up")):
            with self.assertRaises(RuntimeError):
                cases.run_r1(s)
        path = cases.finish_stage(s, ["R1"])
        doc = self.by_id(self.load(path))
        for cid in ("S-X-1", "S-X-2", "S-X-4"):
            self.assertEqual((doc[cid]["status"], doc[cid]["reason"]), ("blocked", "harness_error"), cid)
        self.assertEqual((doc["S-X-3"]["status"], doc["S-X-3"]["reason"]), ("not_run", "not_selected"))
        self.assertEqual(len(self.model_runs()), 1)

    def test_no_init_event_blocks_sx3(self) -> None:
        """ACC: N11 - R1 stub without an init event: S-X-2 fail(no_session_id), S-X-4 observed(not_observable), S-X-3 blocked(no_session_id) with no spawn."""
        s = self.session(STUB_MODE="noinit")
        sid = cases.run_r1(s)
        self.assertIsNone(sid)
        cases.run_r2(s, sid or cases.staged_session_id(s))
        c = self.by_id(s.cases)
        self.assertEqual((c["S-X-2"]["status"], c["S-X-2"]["reason"]), ("fail", "no_session_id"))
        self.assertEqual((c["S-X-4"]["status"], c["S-X-4"]["reason"]), ("observed", "not_observable"))
        self.assertEqual((c["S-X-3"]["status"], c["S-X-3"]["reason"]), ("blocked", "no_session_id"))
        self.assertEqual(len(self.model_runs()), 1)

    def test_effort_rejected_none_and_cap(self) -> None:
        """ACC: N11, N25 - effort rejected -> not_applicable(model_or_effort_rejected) with stderr excerpt; effort none -> not_applicable(effort_not_listed), no spawn; MAX_RUNS=1 -> the next run's cases not_run(run_cap)."""
        s = self.session(STUB_MODE="reject_effort", MESH_SMOKE_MAX_RUNS="1")
        cases.run_r3(s, "low")
        c = s.cases[-1]
        self.assertEqual((c["status"], c["reason"]), ("not_applicable", "model_or_effort_rejected"))
        self.assertIn("not supported", c["observation"]["stderr_excerpt"])
        cases.run_r3(s, "none")
        self.assertEqual((s.cases[-1]["status"], s.cases[-1]["reason"]), ("not_applicable", "effort_not_listed"))
        cases.run_r1(s)
        self.assertEqual([(x["id"], x["status"], x["reason"]) for x in s.cases[-3:]],
                         [(i, "not_run", "run_cap") for i in ("S-X-1", "S-X-2", "S-X-4")])
        cases.run_r4(s)
        self.assertEqual((s.cases[-1]["id"], s.cases[-1]["reason"]), ("S-X-6", "run_cap"))
        self.assertEqual(len(self.model_runs()), 1)


@unittest.skipUnless(sys.platform != "win32", 'Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only')
class ReevaluateRefusalTest(CasesBase):
    def staged_r1(self) -> str:
        s = self.session(MESH_SMOKE_MAX_RUNS="1")
        cases.run_r1(s)
        return cases.finish_stage(s)

    def test_refuses_synthetic_and_hash_mismatch(self) -> None:
        """ACC: N12, N25 - reevaluate refuses a synthetic results path, a doc pointing at a synthetic fixture, a tampered fixture and a tampered results file; nothing is written; --out must be /tmp/mh-*."""
        path = self.staged_r1()
        rout = os.path.join(self.tmp, "reeval")
        # synthetic results path
        syn_dir = os.path.join(self.tmp, "synthetic", "results")
        os.makedirs(syn_dir)
        syn = os.path.join(syn_dir, "synthetic_droid-1.6.0-2026-11-20.json")
        shutil.copyfile(path, syn)
        shutil.copyfile(path + ".meta.json", syn + ".meta.json")
        with self.assertRaises(ev.ReevaluateError):
            ev.reevaluate(syn, rout)
        # doc referencing a synthetic fixture (re-signed so only the synthetic rule trips)
        doc = self.load(path)
        doc["cases"][0]["fixture"] = smoke_schema.fixture_rel("droid", "1.6.0", "synthetic_droid_stream.jsonl")
        bad_stage = os.path.join(self.tmp, "stage-syn")
        p = tierlib.write_capture(bad_stage, "results/droid-1.6.0-2026-11-20.json",
                                  json.dumps(doc).encode(), ["test"])
        shutil.copytree(os.path.join(self.out, "droid"), os.path.join(bad_stage, "droid"))
        with self.assertRaises(ev.ReevaluateError) as cm:
            ev.reevaluate(p, rout)
        self.assertIn("synthetic", str(cm.exception))
        # tampered fixture
        fx = os.path.join(self.out, "droid", "1.6.0", "smoke-S-X-1-stdout.jsonl")
        with open(fx, "ab") as f:
            f.write(b"{}\n")
        with self.assertRaises(ev.ReevaluateError):
            ev.reevaluate(path, rout)
        # tampered results file (sidecar mismatch)
        path2 = os.path.join(self.tmp, "stage2", "results", os.path.basename(path))
        os.makedirs(os.path.dirname(path2))
        shutil.copyfile(path + ".meta.json", path2 + ".meta.json")
        with open(path2, "w") as f:
            f.write("{}")
        with self.assertRaises(tierlib.TierError):
            ev.reevaluate(path2, rout)
        self.assertFalse(os.path.exists(rout))
        with self.assertRaises(ev.ReevaluateError):
            ev.reevaluate(path, "/tmp/not-mh-out")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(ev.main([path, "--out", rout]), 1)
        self.assertFalse(json.loads(buf.getvalue())["ok"])

    def test_synthetic_input_is_never_pass(self) -> None:
        """ACC: N12, N24 - S-X-1 evaluated on synthetic_* input is never pass, even when the parser would find the token; reevaluate_doc refuses it outright."""
        with open(SYNTHETIC, encoding="utf-8") as f:
            text = f.read()

        class Parsed:
            final_text = "MESH_SMOKE_OK"
            session_id = "synthetic-session-0001"

        for fn in (ev.eval_sx1, ev.eval_sx2):
            st, _, _ = fn(text, 0, False, Parsed(), synthetic=True)
            self.assertNotEqual(st, "pass")
        self.assertEqual(ev.eval_sx1(text, 0, False, Parsed())[0], "pass")
        self.assertNotEqual(ev.eval_sx4(text, 0, False, "synthetic-model", synthetic=True)[0], "pass")
        doc = {"schema": "mesh.smoke.v1", "adapter": "droid", "binary": "/usr/bin/droid", "version": "1.6.0",
               "captured_at_utc": "2026-11-20T00:00:00Z", "mesh_commit": "x", "model": "m", "effort": None,
               "profile": "readonly", "timeout_s": 180, "consent_ref": "c",
               "cases": [{"id": "S-X-1", "status": "pass", "reason": None, "rc": 0, "duration_s": 1,
                          "fixture": os.path.relpath(SYNTHETIC, REPO_ROOT), "fixture_sha256": "0" * 64,
                          "observation": None, "evaluated_from": None, "carried_from": None}]}
        with self.assertRaises(ev.ReevaluateError):
            ev.reevaluate_doc(doc, "droid-1.6.0-2026-11-20.json", os.path.join(REPO_ROOT, "tests", "fixtures", "real"))


class PureTest(unittest.TestCase):
    def test_selection_deferred_and_evaluators(self) -> None:
        """ACC: N11 - MESH_SMOKE_CASES selection; no deferred_m3_lite helper left (all four adapters run); evaluator edge verdicts."""
        self.assertEqual(cases.selected_runs({}), list(cases.RUNS))
        self.assertEqual(cases.selected_runs({"MESH_SMOKE_CASES": "r3, R1"}), ["R1", "R3"])
        with self.assertRaises(harness.SmokeConfigError):
            cases.selected_runs({"MESH_SMOKE_CASES": "R7"})
        # deferred_cases() was removed in m3-smoke-agy-vibe (Quest #17 P3): kiro, agy and vibe all
        # have opt-in smoke runs now, so nothing emits not_run(deferred_m3_lite) any more.
        self.assertFalse(hasattr(ev, "deferred_cases"))
        self.assertEqual(ev.eval_sx1("", None, True, None), ("fail", "timeout", "hang"))
        self.assertEqual(ev.eval_sx1("", 3, False, None)[:2], ("fail", "rc_nonzero"))
        self.assertEqual(ev.eval_sx4('{"type":"system","subtype":"init","model":"b"}', 0, False, "a")[:2],
                         ("fail", "model_mismatch"))
        self.assertEqual(ev.eval_sx5('{"type":"system","subtype":"init"}', "", 0, False, "low")[:2],
                         ("observed", "not_echoed"))
        # Kiro r3 finding 6: a rejection only when stderr names effort or model
        self.assertEqual(ev.eval_sx5("", "Error: Reasoning EFFORT 'low' not allowed", 2, False, "low")[:2],
                         ("not_applicable", "model_or_effort_rejected"))
        self.assertEqual(ev.eval_sx5("", "unknown model claude-x", 2, False, "low")[:2],
                         ("not_applicable", "model_or_effort_rejected"))
        self.assertEqual(ev.eval_sx5("", "401 unauthorized: please log in", 1, False, "low")[:2],
                         ("fail", "rc_nonzero"))
        self.assertEqual(ev.eval_sx5("", "", 1, False, "low")[:2], ("fail", "rc_nonzero"))
        self.assertEqual(ev.eval_sx3("nothing", 0, False, None)[:2], ("fail", "token_missing"))
        self.assertEqual(ev.excerpt("x" * 1000), "x" * ev.EXCERPT_CHARS)

    def test_tier_skips_without_gate(self) -> None:
        """ACC: N25 - without MESH_SMOKE=1 (this suite never sets it) the droid tier skips and spawns nothing (DeferredAdapters is gone since m3-smoke-agy-vibe; agy/vibe skips are in test_smoke_agy.py / test_smoke_vibe.py)."""
        self.assertNotEqual(os.environ.get("MESH_SMOKE"), "1")
        self.assertFalse(hasattr(cases, "DeferredAdapters"))
        for klass in (cases.DroidSmoke,):
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(klass).run(result)
            self.assertEqual(result.failures + result.errors, [])
            self.assertTrue(result.skipped)


CODEX_STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
with open(CALLS, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("codex-cli 0.166.0"); sys.exit(0)
if "exec" not in args or "--json" not in args or args[-1] != "-":
    sys.exit(99)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
mode = os.environ.get("STUB_MODE", "ok")
prompt = sys.stdin.read()
with open(SEEN, "a", encoding="utf-8") as f:
    f.write(json.dumps({"prompt": prompt, "cwd": os.getcwd(),
                        "mesh_keys": sorted(k for k in os.environ if k.startswith("MESH_"))}) + "\n")
if not prompt:
    sys.exit(98)
sid = opt("resume") or "0199a213-81c0-7800-8aa1-bbab2a035a53"
def emit(obj):
    print(json.dumps(obj), flush=True)
def message(text):
    emit({"type": "item.completed", "item": {"id": "item_m", "type": "agent_message", "text": text}})
def failed_command(cmd, output):
    item = {"id": "item_c", "type": "command_execution", "command": "bash -lc " + cmd}
    emit({"type": "item.started", "item": dict(item, aggregated_output="", exit_code=None, status="in_progress")})
    emit({"type": "item.completed", "item": dict(item, aggregated_output=output, exit_code=1, status="failed")})
emit({"type": "thread.started", "thread_id": sid})
emit({"type": "turn.started"})
if mode == "turn_failed":
    emit({"type": "turn.failed", "error": {"message": "The 'gpt-0-nonexistent' model is not supported when using Codex with a ChatGPT account."}})
    sys.exit(1)
if mode == "auth":
    msg = "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"
    emit({"type": "error", "message": "Reconnecting... 1/5 (" + msg + ")"})
    emit({"type": "error", "message": msg})
    emit({"type": "turn.failed", "error": {"message": msg}})
    sys.exit(1)
if "Count from 1" in prompt:
    for i in range(1, 100000):
        message(str(i))
        time.sleep(0.01)
    sys.exit(0)
if "touch y.txt" in prompt:
    failed_command("touch y.txt", "touch: cannot touch 'y.txt': Read-only file system\n")
    text = "The sandbox is read-only, so the command failed."
elif "x.txt" in prompt:
    text = "I cannot write files in a read-only sandbox."
elif "resume" in args:
    text = "I replied with MESH_SMOKE_OK."
else:
    text = "MESH_SMOKE_OK"
message(text)
if mode != "no_turn_completed":
    emit({"type": "turn.completed", "usage": {"input_tokens": 24763, "cached_input_tokens": 24448,
                                              "output_tokens": 122, "reasoning_output_tokens": 40}})
sys.exit(0)
'''

CLAUDE_STUB = r'''#!/usr/bin/env python3
import json, os, signal, sys, time
args = sys.argv[1:]
with open(CALLS, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("2.1.325 (Claude Code)"); sys.exit(0)
if args[:1] != ["-p"] or "stream-json" not in args:
    sys.exit(99)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
mode = os.environ.get("STUB_MODE", "ok")
prompt = sys.stdin.read()
with open(SEEN, "a", encoding="utf-8") as f:
    f.write(json.dumps({"prompt": prompt, "cwd": os.getcwd(),
                        "mesh_keys": sorted(k for k in os.environ if k.startswith("MESH_"))}) + "\n")
if not prompt:
    sys.exit(98)
sid = opt("--resume") or "5f2b1c7e-0000-4000-8000-000000000001"
model = "claude-sonnet-5-5-20261001" if mode == "alias" else opt("--model")
def emit(obj):
    print(json.dumps(dict(obj, session_id=sid)), flush=True)
def assistant(text, error=None):
    event = {"type": "assistant", "parent_tool_use_id": None,
             "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                         "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}}
    if error:
        event["error"] = error
    emit(event)
def result(text, subtype="success", is_error=False, denials=()):
    emit({"type": "result", "subtype": subtype, "is_error": is_error, "duration_ms": 900, "num_turns": 1,
          "total_cost_usd": 0.0123, "usage": {"input_tokens": 150, "output_tokens": 70},
          "permission_denials": list(denials), "result": text})
emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "tools": ["Read"], "mcp_servers": [],
      "model": model, "permissionMode": opt("--permission-mode"), "apiKeySource": "none",
      "claude_code_version": "2.1.325"})
if mode == "error_rc0":
    result("API Error: 500 internal server error", subtype="error_during_execution", is_error=True)
    sys.exit(0)
if mode == "reject_effort" and "--effort" in args:
    result("API Error: 400 effort 'low' is not supported by this model", is_error=True)
    sys.exit(1)
if mode == "not_logged_in":
    assistant("Not logged in · Please run /login", error="authentication_failed")
    result("Not logged in · Please run /login", is_error=True)
    sys.exit(1)
if "Count from 1" in prompt:
    if mode == "term_143":
        signal.signal(signal.SIGTERM, lambda signo, frame: os._exit(143))
    for i in range(1, 100000):
        assistant(str(i))
        time.sleep(0.01)
    sys.exit(0)
if "touch y.txt" in prompt:
    text = "I am not able to run shell commands in this session."
    assistant(text)
    result(text, denials=[{"tool_name": "Bash", "tool_use_id": "toolu_9", "tool_input": {"command": "touch y.txt"}}])
    sys.exit(0)
if "x.txt" in prompt:
    text = "I am not able to write files in this session."
elif "--resume" in args:
    text = "I replied with MESH_SMOKE_OK."
else:
    text = "MESH_SMOKE_OK"
assistant(text)
result(text)
sys.exit(0)
'''

DEVIN_STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
with open(CALLS, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("devin 3000.14.30821 (a09cd7f323ee)"); sys.exit(0)
if args == ["--help"]:
    print("Usage: devin [OPTIONS] [PROMPT]\n  -p, --print\n      --prompt-file <FILE>\n      --model <MODEL>\n"
          "      --permission-mode <MODE>\n      --respect-workspace-trust <BOOL>")
    sys.exit(0)
if args == ["auth", "status"]:
    print("Logged in (via Devin)."); sys.exit(0)
if args[:2] != ["-p", "--prompt-file"]:
    sys.exit(99)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
if opt("--permission-mode") != "dangerous" or "--cloud" in args:
    sys.exit(97)
mode = os.environ.get("STUB_MODE", "ok")
with open(opt("--prompt-file"), encoding="utf-8") as f:
    prompt = f.read()
with open(SEEN, "a", encoding="utf-8") as f:
    f.write(json.dumps({"prompt": prompt, "cwd": os.getcwd(),
                        "mesh_keys": sorted(k for k in os.environ if k.startswith("MESH_"))}) + "\n")
sys.stdout.write("\x1b[?2004l")
if mode == "login_canceled":
    sys.stderr.write("Error: Login canceled\n"); sys.exit(1)
if "Count from 1" in prompt:
    for i in range(1, 100000):
        print(i, flush=True)
        time.sleep(0.01)
    sys.exit(0)
if "touch y.txt" in prompt:
    if mode == "rejected":
        sys.stderr.write("warning: rejected a tool call that requires confirmation. Running in non-interactive "
                         "mode. Use --permission-mode dangerous to auto-approve all tools.\n")
        text = "The shell command was rejected."
    elif mode == "no_exec":
        text = "I did not run the command."
    else:
        open("y.txt", "w").close()
        text = "Ran touch y.txt."
elif "x.txt" in prompt:
    with open("x.txt", "w", encoding="utf-8") as f:
        f.write("hello\n")
    text = "Created x.txt."
else:
    text = "MESH_SMOKE_OK"
if mode == "leak":
    text += "\nkey " + os.environ.get("DEVIN_API_KEY", "")
    sys.stderr.write("warning: api_key=" + os.environ.get("DEVIN_API_KEY", "") + "\n")
if mode == "error_rc0":
    sys.stderr.write("Error: the session ended unexpectedly\n")
print(text)
sys.exit(0)
'''


@unittest.skipUnless(sys.platform != "win32", 'Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only')
class NewAdapterBase(unittest.TestCase):
    """Shared setup for the codex / claude / devin stubs; no tests here."""

    ADAPTER = ""
    STUB = ""
    VERSION = ""
    MODEL = ""

    def setUp(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])
        self.tmp = tempfile.mkdtemp(prefix="mh-smoke-n-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "calls.log")
        self.seen_log = os.path.join(self.tmp, "seen.log")
        os.makedirs(os.path.join(self.tmp, "bin"))
        self.binary = os.path.join(self.tmp, "bin", self.ADAPTER)
        first, rest = self.STUB.split("\n", 1)
        with open(self.binary, "w", encoding="utf-8") as f:
            f.write(first + f"\nCALLS = {self.calls!r}\nSEEN = {self.seen_log!r}\n" + rest)
        make_executable(self.binary)
        self.assertTrue(self.binary.startswith("/tmp/mh-"))
        self.out = os.path.join(self.tmp, "out")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MESH_SMOKE": "1",
                    "MESH_SMOKE_ADAPTERS": self.ADAPTER, "MESH_SMOKE_CONSENT": self.ADAPTER,
                    "MESH_SMOKE_MAX_RUNS": "6", f"MESH_SMOKE_MODEL_{self.ADAPTER.upper()}": self.MODEL,
                    "MESH_SMOKE_TIMEOUT_S": "20", "MESH_SMOKE_OUT": self.out,
                    "MESH_SMOKE_CONSENT_REF": "offline stub", "MESH_HOME": "/tmp/mh-should-not-leak"}

    def tearDown(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])

    def session(self, **env) -> harness.SmokeSession:
        s = harness.SmokeSession(dict(self.env, **env), self.ADAPTER, self.binary, grace_s=1, version=self.VERSION,
                                 mesh_commit="abc1234")
        self.addCleanup(s.cleanup)
        return s

    def other_out(self, name: str) -> str:
        return os.path.join(self.tmp, name)

    def model_runs(self) -> list:
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as f:
            return [a for a in map(json.loads, f.read().splitlines())
                    if a not in (["--version"], ["--help"], ["auth", "status"])]

    def seen(self) -> list:
        if not os.path.exists(self.seen_log):
            return []
        with open(self.seen_log, encoding="utf-8") as f:
            return [json.loads(line) for line in f.read().splitlines()]

    def assert_confined(self, s: harness.SmokeSession, prompts: list) -> None:
        # every run got its prompt, ran in the session's scratch dir and saw no MESH_* variable
        seen = self.seen()
        self.assertEqual([x["prompt"] for x in seen], prompts)
        for x in seen:
            self.assertEqual(os.path.realpath(x["cwd"]), os.path.realpath(s.workspace))
            self.assertTrue(os.path.realpath(x["cwd"]).startswith("/tmp/mh-smoke-"), x["cwd"])
            self.assertEqual(x["mesh_keys"], [])

    def staged_evidence(self) -> evidence.Evidence:
        # mesh/adapters/evidence.py on the staging tree the harness wrote (testing mode override)
        return evidence.load_evidence(self.ADAPTER, True, self.VERSION,
                                      env={evidence.OVERRIDE_ENV: os.path.join(self.out, "results")})

    def reevaluate(self, path: str) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("subprocess.Popen", side_effect=AssertionError("spawned")):
            rc = ev.main([path, "--out", self.other_out("reeval")])
        self.assertEqual(rc, 0, buf.getvalue())
        new_path = json.loads(buf.getvalue())["results"]
        self.assertNotEqual(os.path.basename(new_path), os.path.basename(path))
        new = self.load(new_path)
        self.assertEqual(smoke_schema.validate(new, REPO_ROOT, fixture_root=self.out), [])
        return self.by_id(new)

    @staticmethod
    def by_id(doc_or_cases) -> dict:
        cs = doc_or_cases["cases"] if isinstance(doc_or_cases, dict) else doc_or_cases
        return {c["id"]: c for c in cs}

    @staticmethod
    def load(path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def verdict(case: dict) -> tuple:
        return case["status"], case["reason"]


@unittest.skipUnless(sys.platform != "win32", 'Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only')
class CodexSmokeTest(NewAdapterBase):
    ADAPTER, STUB, VERSION, MODEL = "codex", CODEX_STUB, "0.166.0", "gpt-6.1-sol"

    def test_all_runs_results_and_reevaluate(self) -> None:
        """ACC: N11, N12, N24 - R1..R6 against the codex stub: readonly argv (--ask-for-approval never, --sandbox read-only, never the bypass flag), the smoke model, resume <id>, model_reasoning_effort, the prompt on stdin, cwd the /tmp/mh-smoke-* workspace; S-X-1/2/3/6 pass, S-X-4 observed(not_observable), S-X-5 observed(not_echoed), S-X-7 observed, S-CODEX-8 deny with the failed command count; results validate, list CODEX_CASES, say profile readonly and name .jsonl fixtures; evidence.py reads the staged file as passing; reevaluate re-parses S-X-1/2/4/5 without spawning."""
        s = self.session()
        self.assertEqual(s.profile, "readonly")
        sid = cases.run_r1(s)
        self.assertEqual(sid, CODEX_SID)
        cases.run_r2(s, sid)
        cases.run_r3(s, "low")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        cases.run_r5(s)
        cases.run_r6(s)
        runs = self.model_runs()
        self.assertEqual(len(runs), 6)
        for argv in runs:
            self.assertEqual(argv[:5], ["--ask-for-approval", "never", "exec", "--json", "--skip-git-repo-check"])
            self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertEqual(argv[argv.index("--model") + 1], self.MODEL)
            self.assertEqual(os.path.realpath(argv[argv.index("--cd") + 1]), os.path.realpath(s.workspace))
            self.assertEqual(argv[-1], "-")
            self.assertFalse([a for a in argv if "MESH_SMOKE_OK" in a or "Count from" in a])  # no prompt in argv
        self.assertEqual(runs[1][-3:-1], ["resume", CODEX_SID])
        self.assertEqual(runs[2][runs[2].index("--config") + 1], "model_reasoning_effort=low")
        self.assertEqual([a for a in runs[0] if a.startswith("model_reasoning_effort")], [])
        self.assert_confined(s, [cases.PROMPTS[r] for r in cases.RUNS])
        path = cases.finish_stage(s)
        doc = self.load(path)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        self.assertEqual((doc["adapter"], doc["version"], doc["profile"], doc["model"]),
                         ("codex", "0.166.0", "readonly", self.MODEL))
        self.assertEqual(os.path.basename(path), f"codex-0.166.0-{doc['captured_at_utc'][:10]}.json")
        self.assertEqual([x["id"] for x in doc["cases"]], list(cases.CODEX_CASES))
        c = self.by_id(doc)
        self.assertEqual(c["S-X-1"]["fixture"], "tests/fixtures/real/codex/0.166.0/smoke-S-X-1-stdout.jsonl")
        self.assertEqual(c["S-X-1"]["status"], "pass")
        self.assertEqual((c["S-X-2"]["status"], c["S-X-2"]["observation"]["session_id"]), ("pass", CODEX_SID))
        self.assertIsNone(c["S-X-2"]["observation"]["init_cwd"])  # codex names no cwd in its stream
        self.assertEqual(c["S-X-3"]["status"], "pass")
        self.assertEqual(c["S-X-3"]["observation"]["resumed_session_id"], CODEX_SID)
        self.assertEqual(self.verdict(c["S-X-4"]), ("observed", "not_observable"))
        self.assertEqual(self.verdict(c["S-X-5"]), ("observed", "not_echoed"))
        self.assertEqual(c["S-X-5"]["observation"]["requested_effort"], "low")
        self.assertEqual(c["S-X-6"]["status"], "pass")
        self.assertTrue(c["S-X-6"]["observation"]["alive_at_term"])
        self.assertGreaterEqual(c["S-X-6"]["observation"]["lines_at_term"], cases.SIGNAL_MIN_LINES["codex"])
        self.assertEqual(c["S-X-7"]["status"], "observed")
        self.assertEqual(c["S-X-7"]["observation"]["file_created"], False)
        self.assertNotIn("profile", c["S-X-7"]["observation"])  # readonly is the default, not repeated
        x8 = c["S-CODEX-8"]["observation"]
        self.assertEqual(c["S-CODEX-8"]["status"], "observed")
        self.assertEqual((x8["outcome"], x8["y_txt_exists"], x8["commands"], x8["failed_commands"], x8["file_changes"]),
                         ("deny", False, 1, 1, 0))
        got = self.staged_evidence()
        self.assertTrue(got.ok, (got.reason, got.errors))
        self.assertEqual((got.results_path, got.results_version), (path, "0.166.0"))
        self.assertEqual(evidence.apply_hold(got, "codex", False).ok, True)  # codex is on no hold
        nc = self.reevaluate(path)
        src = "tests/smoke/results/" + os.path.basename(path)
        for cid in ("S-X-1", "S-X-2", "S-X-4", "S-X-5"):
            self.assertEqual((nc[cid]["evaluated_from"], nc[cid]["carried_from"]), (src, None), cid)
            self.assertEqual(self.verdict(nc[cid]), self.verdict(c[cid]), cid)
        for cid in ("S-X-3", "S-X-6", "S-X-7", "S-CODEX-8"):
            self.assertEqual((nc[cid]["carried_from"], nc[cid]["evaluated_from"]), (src, None), cid)
            self.assertEqual(nc[cid]["status"], c[cid]["status"], cid)

    def test_failed_turns_are_not_passes(self) -> None:
        """ACC: N11, N6 - codex turn.failed with exit 1: S-X-1/2/4 fail(rc_nonzero), no session id handed to R2 (blocked, no spawn), S-X-5 not_applicable(model_or_effort_rejected) from the stream's error message; a 401 in the stream is fail(rc_nonzero), not a rejection; rc 0 without turn.completed is fail(vendor_error, incomplete)."""
        s = self.session(STUB_MODE="turn_failed")
        sid = cases.run_r1(s)
        self.assertIsNone(sid)
        cases.run_r2(s, sid or cases.staged_session_id(s))
        cases.run_r3(s, "low")
        c = self.by_id(s.cases)
        for cid in ("S-X-1", "S-X-2", "S-X-4"):
            self.assertEqual(self.verdict(c[cid]), ("fail", "rc_nonzero"), cid)
        self.assertEqual(self.verdict(c["S-X-3"]), ("blocked", "no_session_id"))
        self.assertEqual(self.verdict(c["S-X-5"]), ("not_applicable", "model_or_effort_rejected"))
        self.assertIn("model is not supported", c["S-X-5"]["observation"]["error_excerpt"])
        self.assertEqual(len(self.model_runs()), 2)
        s2 = self.session(STUB_MODE="auth", MESH_SMOKE_OUT=self.other_out("out-auth"))
        cases.run_r3(s2, "low")
        self.assertEqual(self.verdict(s2.cases[-1]), ("fail", "rc_nonzero"))
        s3 = self.session(STUB_MODE="no_turn_completed", MESH_SMOKE_OUT=self.other_out("out-cut"))
        self.assertIsNone(cases.run_r1(s3))
        c3 = self.by_id(s3.cases)
        for cid in ("S-X-1", "S-X-2", "S-X-4"):
            self.assertEqual(self.verdict(c3[cid]), ("fail", "vendor_error"), cid)
            self.assertEqual(c3[cid]["observation"]["vendor_exit_meaning"], "incomplete", cid)
        self.assertTrue(c3["S-X-1"]["observation"]["raw_token_seen"])
        self.assertEqual(c3["S-X-1"]["rc"], 0)

    def test_effort_outside_the_cli_list_is_refused_without_spawn(self) -> None:
        """ACC: N11 - an effort codex does not document (minimal): build() refuses, S-X-5 not_applicable(effort_refused_by_adapter), nothing spawned; none -> not_applicable(effort_not_listed)."""
        s = self.session()
        cases.run_r3(s, "minimal")
        self.assertEqual(self.verdict(s.cases[-1]), ("not_applicable", "effort_refused_by_adapter"))
        self.assertEqual(s.cases[-1]["observation"]["error"], "EFFORT_NOT_ALLOWED")
        cases.run_r3(s, "none")
        self.assertEqual(self.verdict(s.cases[-1]), ("not_applicable", "effort_not_listed"))
        self.assertEqual((self.model_runs(), s.runs_used), ([], 0))


@unittest.skipUnless(sys.platform != "win32", 'Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only')
class ClaudeSmokeTest(NewAdapterBase):
    ADAPTER, STUB, VERSION, MODEL = "claude", CLAUDE_STUB, "2.1.325", "claude-sonnet-5-5"

    def test_all_runs_results_and_reevaluate(self) -> None:
        """ACC: N11, N12, N24 - R1..R6 against the claude stub: readonly argv (--permission-mode dontAsk, the readonly deny list, user settings only, never bypassPermissions), the smoke model, --resume, --effort, the prompt on stdin; S-X-1/2/3/4/6 pass (init model and cwd read from the stream), S-X-5 observed(not_echoed), S-X-7 observed, S-CLAUDE-8 deny with the permission_denials count and tool names; results validate, list CLAUDE_CASES, profile readonly; evidence.py reads the staged file as passing; reevaluate re-parses S-X-1/2/4/5 without spawning."""
        s = self.session()
        self.assertEqual(s.profile, "readonly")
        sid = cases.run_r1(s)
        self.assertEqual(sid, CLAUDE_SID)
        cases.run_r2(s, sid)
        cases.run_r3(s, "low")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        cases.run_r5(s)
        cases.run_r6(s)
        runs = self.model_runs()
        self.assertEqual(len(runs), 6)
        for argv in runs:
            self.assertEqual(argv[:4], ["-p", "--output-format", "stream-json", "--verbose"])
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch")
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")
            self.assertIn("--strict-mcp-config", argv)
            self.assertNotIn("bypassPermissions", argv)
            self.assertNotIn("--allowedTools", argv)
            self.assertEqual(argv[argv.index("--model") + 1], self.MODEL)
            self.assertFalse([a for a in argv if "MESH_SMOKE_OK" in a or "Count from" in a])  # no prompt in argv
        self.assertEqual(runs[1][runs[1].index("--resume") + 1], CLAUDE_SID)
        self.assertEqual(runs[2][runs[2].index("--effort") + 1], "low")
        self.assertNotIn("--effort", runs[0])
        self.assert_confined(s, [cases.PROMPTS[r] for r in cases.RUNS])
        path = cases.finish_stage(s)
        doc = self.load(path)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        self.assertEqual((doc["adapter"], doc["version"], doc["profile"]), ("claude", "2.1.325", "readonly"))
        self.assertEqual([x["id"] for x in doc["cases"]], list(cases.CLAUDE_CASES))
        c = self.by_id(doc)
        self.assertEqual(c["S-X-1"]["fixture"], "tests/fixtures/real/claude/2.1.325/smoke-S-X-1-stdout.jsonl")
        self.assertEqual(c["S-X-1"]["status"], "pass")
        self.assertEqual((c["S-X-2"]["status"], c["S-X-2"]["observation"]["session_id"]), ("pass", CLAUDE_SID))
        self.assertEqual(os.path.realpath(c["S-X-2"]["observation"]["init_cwd"]),
                         os.path.realpath(c["S-X-2"]["observation"]["workspace"]))
        self.assertEqual(c["S-X-3"]["status"], "pass")
        self.assertEqual(c["S-X-4"]["status"], "pass")
        self.assertEqual(c["S-X-4"]["observation"], {"init_model": self.MODEL})
        self.assertEqual(self.verdict(c["S-X-5"]), ("observed", "not_echoed"))
        self.assertEqual(c["S-X-6"]["status"], "pass")
        self.assertGreaterEqual(c["S-X-6"]["observation"]["lines_at_term"], cases.SIGNAL_MIN_LINES["claude"])
        self.assertEqual(c["S-X-7"]["observation"]["file_created"], False)
        x8 = c["S-CLAUDE-8"]["observation"]
        self.assertEqual((x8["outcome"], x8["y_txt_exists"], x8["permission_denials"], x8["denied_tools"]),
                         ("deny", False, 1, ["Bash"]))
        self.assertNotIn("profile", x8)
        got = self.staged_evidence()
        self.assertTrue(got.ok, (got.reason, got.errors))
        self.assertEqual(got.results_path, path)
        nc = self.reevaluate(path)
        src = "tests/smoke/results/" + os.path.basename(path)
        for cid in ("S-X-1", "S-X-2", "S-X-4", "S-X-5"):
            self.assertEqual((nc[cid]["evaluated_from"], nc[cid]["carried_from"]), (src, None), cid)
            self.assertEqual(self.verdict(nc[cid]), self.verdict(c[cid]), cid)
        for cid in ("S-X-3", "S-X-6", "S-X-7", "S-CLAUDE-8"):
            self.assertEqual((nc[cid]["carried_from"], nc[cid]["evaluated_from"]), (src, None), cid)

    def test_error_results_alias_and_effort_rejection(self) -> None:
        """ACC: N11, N6 - claude: an error result at rc 0 is S-X-1/2/4 fail(vendor_error, run_error) and no session id is handed to R2; an init model other than the requested id (an alias) is S-X-4 fail(model_mismatch); an effort rejected in the result at exit 1 is S-X-5 not_applicable(model_or_effort_rejected); a not-logged-in result at exit 1 is S-X-5 fail(rc_nonzero)."""
        s = self.session(STUB_MODE="error_rc0")
        self.assertIsNone(cases.run_r1(s))
        c = self.by_id(s.cases)
        for cid in ("S-X-1", "S-X-2", "S-X-4"):
            self.assertEqual(self.verdict(c[cid]), ("fail", "vendor_error"), cid)
            self.assertEqual(c[cid]["observation"]["vendor_exit_meaning"], "run_error", cid)
        s2 = self.session(STUB_MODE="alias", MESH_SMOKE_OUT=self.other_out("out-alias"))
        self.assertEqual(cases.run_r1(s2), CLAUDE_SID)
        c2 = self.by_id(s2.cases)
        self.assertEqual(self.verdict(c2["S-X-4"]), ("fail", "model_mismatch"))
        self.assertEqual(c2["S-X-4"]["observation"],
                         {"init_model": "claude-sonnet-5-5-20261001", "requested": self.MODEL})
        s3 = self.session(STUB_MODE="reject_effort", MESH_SMOKE_OUT=self.other_out("out-effort"))
        cases.run_r3(s3, "low")
        self.assertEqual(self.verdict(s3.cases[-1]), ("not_applicable", "model_or_effort_rejected"))
        self.assertIn("effort 'low' is not supported", s3.cases[-1]["observation"]["error_excerpt"])
        self.assertEqual(s3.cases[-1]["observation"]["requested_effort"], "low")
        s4 = self.session(STUB_MODE="not_logged_in", MESH_SMOKE_OUT=self.other_out("out-auth"))
        cases.run_r3(s4, "low")
        self.assertEqual(self.verdict(s4.cases[-1]), ("fail", "rc_nonzero"))

    def test_sigterm_exit_143_passes_sx6(self) -> None:
        """ACC: N11 - claude's documented SIGTERM exit (143): S-X-6 pass after SIGNAL_MIN_LINES["claude"] (2: the init event comes before the model) stdout lines; the group is gone."""
        self.assertEqual(cases.SIGNAL_MIN_LINES["claude"], 2)
        s = self.session(STUB_MODE="term_143", MESH_SMOKE_MAX_RUNS="1")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        case = s.cases[-1]
        self.assertEqual((case["id"], case["status"]), ("S-X-6", "pass"))
        self.assertEqual(case["rc"], 143)
        self.assertTrue(case["observation"]["alive_at_term"])
        self.assertTrue(case["observation"]["group_gone_after_term"])
        self.assertGreaterEqual(case["observation"]["lines_at_term"], 2)


@unittest.skipUnless(sys.platform != "win32", 'Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only')
class DevinSmokeTest(NewAdapterBase):
    ADAPTER, STUB, VERSION, MODEL = "devin", DEVIN_STUB, "3000.14.30821", "swe-2-flash"

    def out_files(self) -> list:
        found = []
        for d, _, names in os.walk(self.out):
            found += [os.path.relpath(os.path.join(d, n), self.out) for n in names]
        return sorted(found)

    def test_all_runs_results_and_reevaluate(self) -> None:
        """ACC: N11, N12, N21, N24 - R1..R6 against the devin stub: the session profile is unrestricted (--permission-mode dangerous, never --cloud), the prompt in --prompt-file inside the private run dir, cwd the /tmp/mh-smoke-* workspace; 4 model runs (R2 and R3 spawn nothing); stdout is captured as smoke-<case>-stdout.txt with sidecars; S-X-1 pass from DevinAdapter.parse, S-X-2 and S-X-3 not_applicable(resume_unsupported), S-X-4 observed(not_observable), S-X-5 not_applicable(effort_refused_by_adapter), S-X-6 pass, S-X-7 and S-DEVIN-8 observed as what happened under unrestricted (file created, command executed, no deny); results validate, list DEVIN_CASES and say profile unrestricted; evidence.py reads the staged file as passing and the launch hold still refuses; reevaluate re-parses S-X-1/2/4 from the text fixture without spawning; the staging tree promotes."""
        s = self.session()
        self.assertEqual(s.profile, "unrestricted")
        self.assertIsNone(cases.run_r1(s))
        cases.run_r2(s, cases.staged_session_id(s))
        cases.run_r3(s, "low")
        cases.run_r4(s, gone_within_s=5, wait_first_byte_s=5)
        cases.run_r5(s)
        cases.run_r6(s)
        runs = self.model_runs()
        self.assertEqual(len(runs), 4)
        self.assertEqual(s.runs_used, 4)
        for argv in runs:
            self.assertEqual(argv[:2], ["-p", "--prompt-file"])
            self.assertTrue(argv[2].startswith(os.path.join(self.out, ".runs") + os.sep), argv[2])
            self.assertEqual(argv[3:], ["--model", self.MODEL, "--permission-mode", "dangerous",
                                        "--respect-workspace-trust", "false"])
        self.assert_confined(s, [cases.PROMPTS[r] for r in ("R1", "R4", "R5", "R6")])
        path = cases.finish_stage(s)
        doc = self.load(path)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        self.assertEqual((doc["adapter"], doc["version"], doc["profile"], doc["effort"]),
                         ("devin", "3000.14.30821", "unrestricted", None))
        self.assertEqual(os.path.basename(path), f"devin-3000.14.30821-{doc['captured_at_utc'][:10]}.json")
        self.assertEqual([x["id"] for x in doc["cases"]], list(cases.DEVIN_CASES))
        base = "devin/3000.14.30821/"
        captured = [f"smoke-{cid}-{kind}" for cid in ("S-DEVIN-8", "S-X-1", "S-X-6", "S-X-7")
                    for kind in ("stderr.txt", "stdout.txt")]
        self.assertEqual(self.out_files(), sorted(
            [base + n + suffix for n in captured for suffix in ("", ".meta.json")]
            + [os.path.relpath(path, self.out), os.path.relpath(path, self.out) + ".meta.json"]))
        c = self.by_id(doc)
        fx = "tests/fixtures/real/devin/3000.14.30821/smoke-S-X-1-stdout.txt"
        self.assertEqual((c["S-X-1"]["status"], c["S-X-1"]["fixture"]), ("pass", fx))
        self.assertEqual(c["S-X-1"]["observation"], {"raw_token_seen": True})
        staged_fx = os.path.join(self.out, "devin", "3000.14.30821", "smoke-S-X-1-stdout.txt")
        with open(staged_fx, "rb") as f:
            data = f.read()
        self.assertEqual(data, b"\x1b[?2004lMESH_SMOKE_OK\n")  # the text as printed, escapes kept
        self.assertEqual(tierlib.read_sidecar(staged_fx)["sha256"], tierlib.sha256_bytes(data))
        self.assertEqual(c["S-X-1"]["fixture_sha256"], tierlib.sha256_bytes(data))
        self.assertEqual(tierlib.read_sidecar(staged_fx)["command"][:2], ["devin", "-p"])
        self.assertEqual(self.verdict(c["S-X-2"]), ("not_applicable", "resume_unsupported"))
        self.assertEqual((c["S-X-2"]["fixture"], c["S-X-2"]["rc"]), (fx, 0))
        obs2 = c["S-X-2"]["observation"]
        self.assertEqual((obs2["session_id"], obs2["capability"], obs2["supported"], obs2["init_cwd"]),
                         (None, "resume", "no", None))
        self.assertEqual(obs2["note"], "no session id is printed in print mode")
        self.assertEqual(self.verdict(c["S-X-3"]), ("not_applicable", "resume_unsupported"))
        self.assertEqual((c["S-X-3"]["fixture"], c["S-X-3"]["rc"]), (None, None))
        self.assertEqual(self.verdict(c["S-X-4"]), ("observed", "not_observable"))
        self.assertEqual(self.verdict(c["S-X-5"]), ("not_applicable", "effort_refused_by_adapter"))
        self.assertEqual(c["S-X-5"]["observation"],
                         {"error": "EFFORT_NOT_ALLOWED", "effort": "low", "model": self.MODEL})
        self.assertIsNone(c["S-X-5"]["fixture"])
        self.assertEqual(c["S-X-6"]["status"], "pass")
        self.assertTrue(c["S-X-6"]["observation"]["alive_at_term"])
        self.assertGreaterEqual(c["S-X-6"]["observation"]["lines_at_term"], cases.SIGNAL_MIN_LINES["devin"])
        self.assertTrue(c["S-X-6"]["fixture"].endswith("/smoke-S-X-6-stdout.txt"))
        self.assertEqual(c["S-X-7"]["status"], "observed")
        self.assertEqual((c["S-X-7"]["observation"]["file_created"], c["S-X-7"]["observation"]["profile"]),
                         (True, "unrestricted"))
        d8 = c["S-DEVIN-8"]["observation"]
        self.assertEqual(c["S-DEVIN-8"]["status"], "observed")
        self.assertEqual((d8["outcome"], d8["y_txt_exists"], d8["tool_rejections"], d8["profile"], d8["rc"]),
                         ("executed", True, 0, "unrestricted", 0))
        self.assertNotIn("pass", {c[cid]["status"] for cid in smoke_schema.UNSUPPORTED_CASES["devin"]})
        got = self.staged_evidence()
        self.assertTrue(got.ok, (got.reason, got.errors))
        self.assertEqual((got.results_path, got.results_version), (path, "3000.14.30821"))
        held = evidence.apply_hold(got, "devin", False)
        self.assertEqual((held.ok, held.reason, held.milestone), (False, "launch_held", "M5"))
        nc = self.reevaluate(path)
        src = "tests/smoke/results/" + os.path.basename(path)
        for cid in ("S-X-1", "S-X-2", "S-X-4"):
            self.assertEqual((nc[cid]["evaluated_from"], nc[cid]["carried_from"]), (src, None), cid)
            self.assertEqual(self.verdict(nc[cid]), self.verdict(c[cid]), cid)
        self.assertEqual(nc["S-X-2"]["observation"]["session_id"], None)
        for cid in ("S-X-3", "S-X-5", "S-X-6", "S-X-7", "S-DEVIN-8"):
            self.assertEqual((nc[cid]["carried_from"], nc[cid]["evaluated_from"]), (src, None), cid)
            self.assertEqual(self.verdict(nc[cid]), self.verdict(c[cid]), cid)
        repo = self.other_out("repo")
        os.makedirs(os.path.join(repo, "tests", "fixtures", "real"))
        promoted = tierlib.promote(self.out, repo)
        self.assertIn(os.path.join(repo, "tests", "fixtures", "real", "devin", "3000.14.30821", "smoke-S-X-1-stdout.txt"),
                      promoted)
        self.assertIn(os.path.join(repo, "tests", "smoke", "results", os.path.basename(path)), promoted)
        self.assertEqual(smoke_schema.validate(doc, repo), [])

    def test_plain_text_capture_is_redacted(self) -> None:
        """ACC: N21, N25 - a secret devin prints on stdout and stderr is redacted before the text fixture, its sidecar hash and the results doc are written; no file under MESH_SMOKE_OUT holds it; the redacted fixture still passes S-X-1 and re-evaluates."""
        s = self.session(**{"STUB_MODE": "leak", "DEVIN_API_KEY": DEVIN_SECRET, "MESH_SMOKE_MAX_RUNS": "1"})
        cases.run_r1(s)
        path = cases.finish_stage(s, ["R1"])
        base = os.path.join(self.out, "devin", "3000.14.30821")
        for name in ("smoke-S-X-1-stdout.txt", "smoke-S-X-1-stderr.txt"):
            with open(os.path.join(base, name), encoding="utf-8") as f:
                body = f.read()
            self.assertIn("[REDACTED", body, name)
            self.assertEqual(tierlib.read_sidecar(os.path.join(base, name))["sha256"],
                             tierlib.sha256_bytes(body.encode("utf-8")), name)
        for rel in self.out_files():
            with open(os.path.join(self.out, rel), encoding="utf-8") as f:
                self.assertNotIn(DEVIN_SECRET, f.read(), rel)
        self.assertEqual([n for _, _, names in os.walk(s.runs_root) for n in names if n.startswith("raw-")], [])
        c = self.by_id(self.load(path))
        self.assertEqual(c["S-X-1"]["status"], "pass")
        with open(os.path.join(base, "smoke-S-X-1-stdout.txt"), "rb") as f:
            self.assertEqual(c["S-X-1"]["fixture_sha256"], tierlib.sha256_bytes(f.read()))
        self.assertEqual(self.reevaluate(path)["S-X-1"]["status"], "pass")

    def test_shell_case_records_what_happened_not_a_deny(self) -> None:
        """ACC: N11 - S-DEVIN-8 under unrestricted never says deny: y.txt absent at rc 0 is outcome not_executed (with the stderr rejection count when the CLI rejected a call), y.txt present is executed; the status stays observed."""
        expected = {"ok": ("executed", True, 0), "no_exec": ("not_executed", False, 0),
                    "rejected": ("not_executed", False, 1)}
        for mode, want in expected.items():
            with self.subTest(mode=mode):
                s = self.session(STUB_MODE=mode, MESH_SMOKE_OUT=self.other_out(f"out-{mode}"), MESH_SMOKE_MAX_RUNS="1")
                cases.run_r6(s)
                case = s.cases[-1]
                obs = case["observation"]
                self.assertEqual((case["id"], case["status"], case["reason"]), ("S-DEVIN-8", "observed", None))
                self.assertEqual((obs["outcome"], obs["y_txt_exists"], obs["tool_rejections"]), want)
                self.assertEqual(obs["profile"], "unrestricted")
                self.assertNotEqual(obs["outcome"], "deny")

    def test_error_lines_are_not_passes(self) -> None:
        """ACC: N11, N6 - devin: an Error: line with exit 1 is S-X-1/S-X-4 fail(rc_nonzero); an Error: line at exit 0 is S-X-1/S-X-4 fail(vendor_error, run_error) although the token is on stdout; S-X-2 stays not_applicable either way; re-evaluation from the staged text and stderr fixtures gives the same verdicts."""
        s = self.session(STUB_MODE="login_canceled", MESH_SMOKE_MAX_RUNS="1")
        self.assertIsNone(cases.run_r1(s))
        c = self.by_id(s.cases)
        self.assertEqual(self.verdict(c["S-X-1"]), ("fail", "rc_nonzero"))
        self.assertEqual(self.verdict(c["S-X-4"]), ("fail", "rc_nonzero"))
        self.assertEqual(self.verdict(c["S-X-2"]), ("not_applicable", "resume_unsupported"))
        s2 = self.session(STUB_MODE="error_rc0", MESH_SMOKE_OUT=self.other_out("out-rc0"), MESH_SMOKE_MAX_RUNS="1")
        self.assertIsNone(cases.run_r1(s2))
        c2 = self.by_id(s2.cases)
        for cid in ("S-X-1", "S-X-4"):
            self.assertEqual(self.verdict(c2[cid]), ("fail", "vendor_error"), cid)
            self.assertEqual(c2[cid]["observation"]["vendor_exit_meaning"], "run_error", cid)
        self.assertTrue(c2["S-X-1"]["observation"]["raw_token_seen"])
        self.assertEqual(self.verdict(c2["S-X-2"]), ("not_applicable", "resume_unsupported"))
        path = cases.finish_stage(s2, ["R1"])
        nc = self.by_id(ev.reevaluate_doc(self.load(path), os.path.basename(path), s2.config.out))
        for cid in ("S-X-1", "S-X-4"):
            self.assertEqual(self.verdict(nc[cid]), ("fail", "vendor_error"), cid)
        self.assertFalse(evidence.load_evidence("devin", True, self.VERSION,
                                                env={evidence.OVERRIDE_ENV: os.path.join(s2.config.out, "results")}).ok)

    def test_version_from_probe_and_stage_merge(self) -> None:
        """ACC: N11, N25 - without a given version the session reads 3000.14.30821 from the stub's --version (probe: version, help, auth status; no model run); stage 1 (R1) and stage 2 (R4) in one MESH_SMOKE_OUT merge into a -2 file that carries S-X-1 and keeps the unrun cases not_run(not_selected); evidence.py then finds S-X-1 and S-X-6 passing in the latest file."""
        s1 = harness.SmokeSession(dict(self.env, MESH_SMOKE_MAX_RUNS="1"), "devin", self.binary, grace_s=1,
                                  mesh_commit="abc1234")
        self.addCleanup(s1.cleanup)
        self.assertEqual(s1.version, "3000.14.30821")
        self.assertEqual(self.model_runs(), [])
        cases.run_r1(s1)
        p1 = cases.finish_stage(s1, ["R1"])
        self.assertEqual(self.staged_evidence().reason, "launch_cases_failed")  # S-X-6 not run yet
        s2 = self.session(MESH_SMOKE_MAX_RUNS="1")
        self.assertIsNone(cases.staged_session_id(s2))
        cases.run_r4(s2, gone_within_s=5, wait_first_byte_s=5)
        p2 = cases.finish_stage(s2, ["R4"])
        self.assertTrue(p2.endswith("-2.json"), p2)
        d2 = self.by_id(self.load(p2))
        self.assertEqual((d2["S-X-1"]["status"], d2["S-X-1"]["carried_from"]),
                         ("pass", "tests/smoke/results/" + os.path.basename(p1)))
        self.assertEqual(d2["S-X-6"]["status"], "pass")
        self.assertEqual(self.verdict(d2["S-X-7"]), ("not_run", "not_selected"))
        self.assertEqual(self.verdict(d2["S-DEVIN-8"]), ("not_run", "not_selected"))
        self.assertEqual(len(self.model_runs()), 2)
        got = self.staged_evidence()
        self.assertEqual((got.ok, got.results_path), (True, p2))


class NewAdapterRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])
        self.tmp = tempfile.mkdtemp(prefix="mh-smoke-n-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def synth(self, *rel: str) -> str:
        with open(os.path.join(SYNTH_DIR, *rel), encoding="utf-8") as f:
            return f.read()

    @unittest.skipUnless(sys.platform != "win32", "Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only")
    def test_gate_model_and_effort_env(self) -> None:
        """ACC: N25 - codex, claude and devin: a closed gate (MESH_SMOKE unset, adapter not listed, no consent) skips; MESH_SMOKE_MODEL_<ADAPTER> is required and never auto; MESH_SMOKE_EFFORT_<ADAPTER> defaults to low; nothing is spawned or written."""
        for aid in NEW_ADAPTERS:
            with self.subTest(adapter=aid):
                binary = os.path.join(self.tmp, "bin-" + aid, aid)
                os.makedirs(os.path.dirname(binary))
                marker = os.path.join(self.tmp, f"spawned-{aid}")
                with open(binary, "w", encoding="utf-8") as f:
                    f.write(f"#!/bin/sh\ntouch {marker}\nexit 99\n")
                make_executable(binary)
                out = os.path.join(self.tmp, "out-" + aid)
                var = f"MESH_SMOKE_MODEL_{aid.upper()}"
                env = {"PATH": "/usr/bin:/bin", "MESH_SMOKE": "1", "MESH_SMOKE_ADAPTERS": aid,
                       "MESH_SMOKE_CONSENT": aid, "MESH_SMOKE_MAX_RUNS": "1", var: "some-model", "MESH_SMOKE_OUT": out}
                self.assertIsNone(harness.gate(env, aid))
                closed = {"unset": {}, "smoke_0": dict(env, MESH_SMOKE="0"),
                          "not_listed": dict(env, MESH_SMOKE_ADAPTERS="droid,kiro"),
                          "no_consent": dict(env, MESH_SMOKE_CONSENT="droid")}
                for name, bad in closed.items():
                    self.assertIsNotNone(harness.gate(bad, aid), name)
                    with self.assertRaises(harness.SmokeSkip):
                        harness.SmokeSession(bad, aid, binary, grace_s=1)
                self.assertEqual(harness.load_config(env, aid, binary).model, "some-model")
                without = {k: v for k, v in env.items() if k != var}
                for name, bad in {"missing": without, "auto": dict(env, **{var: "auto"}),
                                  "AUTO": dict(env, **{var: "Auto"}),
                                  "another_adapters_var": dict(without, MESH_SMOKE_MODEL_DROID="claude-sonnet-5-5")}.items():
                    with self.assertRaises(harness.SmokeConfigError) as cm:
                        harness.SmokeSession(bad, aid, binary, grace_s=1)
                    self.assertIn(var, str(cm.exception), name)
                self.assertEqual(cases.smoke_effort({}, aid), "low")
                self.assertEqual(cases.smoke_effort({f"MESH_SMOKE_EFFORT_{aid.upper()}": "high"}, aid), "high")
                self.assertEqual(cases.smoke_effort({"MESH_SMOKE_EFFORT_DROID": "high"}, aid), "low")
                self.assertFalse(os.path.exists(marker))
                self.assertFalse(os.path.exists(out))

    def test_tier_classes_skip_without_gate(self) -> None:
        """ACC: N25 - without MESH_SMOKE=1 CodexSmoke, ClaudeSmoke and DevinSmoke skip and spawn nothing; each names its adapter and binary."""
        self.assertNotEqual(os.environ.get("MESH_SMOKE"), "1")
        for klass, aid in ((cases.CodexSmoke, "codex"), (cases.ClaudeSmoke, "claude"), (cases.DevinSmoke, "devin")):
            self.assertEqual((klass.ADAPTER, klass.BINARY), (aid, ADAPTER_CLASSES[aid]().binary_names[0]))
            result = unittest.TestResult()
            with mock.patch("subprocess.Popen", side_effect=AssertionError("spawned")):
                unittest.defaultTestLoader.loadTestsFromTestCase(klass).run(result)
            self.assertEqual(result.failures + result.errors, [], aid)
            self.assertTrue(result.skipped, aid)

    def test_tables_match_the_adapters(self) -> None:
        """ACC: N11, N12 - every adapter in ADAPTER_CASES has S-X-1..7 plus its own case 8 (all known to the schema) and a SIGNAL_MIN_LINES value; its smoke profile is one its build() accepts (readonly; devin unrestricted because readonly raises PROFILE_UNSUPPORTED); PLAIN_STDOUT_ADAPTERS and UNSUPPORTED_CASES equal what the adapters' capabilities declare."""
        self.assertEqual(set(cases.ADAPTER_CASES), {"droid", "kiro", "agy", "vibe", "codex", "claude", "devin"})
        self.assertEqual((cases.CODEX_CASES[-1], cases.CLAUDE_CASES[-1], cases.DEVIN_CASES[-1]),
                         ("S-CODEX-8", "S-CLAUDE-8", "S-DEVIN-8"))
        self.assertEqual({a: cases.SIGNAL_MIN_LINES[a] for a in NEW_ADAPTERS}, {"codex": 3, "claude": 2, "devin": 1})
        self.assertEqual(smoke_schema.SMOKE_PROFILES, {"devin": "unrestricted"})
        prompt = os.path.join(self.tmp, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("x")
        for aid, ids in cases.ADAPTER_CASES.items():
            with self.subTest(adapter=aid):
                self.assertEqual(ids, tuple(f"S-X-{n}" for n in range(1, 8)) + (cases.case8(aid),))
                self.assertTrue(set(ids) <= set(smoke_schema.CASE_CAPS))
                self.assertEqual(smoke_schema.CASE_CAPS[cases.case8(aid)], ())
                self.assertGreaterEqual(cases.SIGNAL_MIN_LINES[aid], 1)
                adapter = ADAPTER_CLASSES[aid]()
                profile = smoke_schema.smoke_profile(aid)
                self.assertEqual(profile, "unrestricted" if aid == "devin" else "readonly")
                req = RunRequest(prompt_path=prompt, workspace=self.tmp, model="m", effort=None, profile=profile)
                self.assertEqual(adapter.build(req).cwd, self.tmp)
                if aid in smoke_schema.SMOKE_PROFILES:
                    with self.assertRaises(ProfileUnsupported):
                        adapter.build(RunRequest(prompt_path=prompt, workspace=self.tmp, model="m", effort=None,
                                                 profile="readonly"))
                caps = adapter.capabilities
                self.assertEqual(aid in smoke_schema.PLAIN_STDOUT_ADAPTERS, caps["stream_output"].supported == "no")
                unsupported = tuple(cid for cid in ids if smoke_schema.CASE_CAPS[cid]
                                    and all(caps[k].supported == "no" for k in smoke_schema.CASE_CAPS[cid]))
                self.assertEqual(smoke_schema.UNSUPPORTED_CASES.get(aid, ()), unsupported)
                for cid in ids:
                    got = ev.unsupported_case(aid, cid)
                    self.assertEqual(got is not None, cid in unsupported, cid)
                    if got:
                        self.assertEqual(got[:2], ("not_applicable", smoke_schema.CASE_CAPS[cid][0] + "_unsupported"))
        self.assertEqual(set(smoke_schema.UNSUPPORTED_CASES) | set(smoke_schema.SMOKE_PROFILES)
                         | set(smoke_schema.PLAIN_STDOUT_ADAPTERS), {"devin", "vibe"})
        self.assertEqual(smoke_schema.UNSUPPORTED_CASES["devin"], ("S-X-2", "S-X-3", "S-X-5"))
        self.assertEqual(smoke_schema.stdout_capture_name("devin", "S-X-1"), "smoke-S-X-1-stdout.txt")
        self.assertEqual(smoke_schema.stdout_capture_name("codex", "S-X-1"), "smoke-S-X-1-stdout.jsonl")
        for name in ("smoke-S-X-1-stdout.txt", "smoke-S-X-1-stdout.jsonl"):
            self.assertEqual(smoke_schema.capture_stem(name), "smoke-S-X-1")
        self.assertEqual(smoke_schema.capture_stem("smoke-S-X-1-stderr.txt"), "smoke-S-X-1-stderr.txt")

    def test_stream_facts_dispatch(self) -> None:
        """ACC: N24 - reevaluate.stream_facts for codex, claude and devin on the synthetic adapter fixtures: session id / model / cwd where the format has them, effort never, vendor_exit_meaning from the adapter, stream_error only for an explicit failure (not for a stream that just stopped); empty and garbage input never raise."""
        empty = {"session_id": None, "model": None, "effort": None, "cwd": None}
        ok = ev.stream_facts(self.synth("codex", "synthetic_ok.jsonl"), "codex")
        self.assertEqual(ok, dict(empty, session_id=CODEX_SID, vendor_exit_meaning=None, stream_error=None))
        failed = ev.stream_facts(self.synth("codex", "synthetic_turn_failed.jsonl"), "codex")
        self.assertEqual((failed["vendor_exit_meaning"], failed["stream_error"]), ("run_error", "run_error"))
        auth = ev.stream_facts(self.synth("codex", "synthetic_not_logged_in.jsonl"), "codex")
        self.assertEqual((auth["vendor_exit_meaning"], auth["stream_error"]), ("auth_failed", "auth_failed"))
        stopped = ev.stream_facts(self.synth("codex", "synthetic_stopped.jsonl"), "codex")
        self.assertEqual((stopped["vendor_exit_meaning"], stopped["stream_error"]), ("incomplete", None))
        ok = ev.stream_facts(self.synth("claude", "synthetic_ok.jsonl"), "claude")
        self.assertEqual(ok, {"session_id": CLAUDE_SID, "model": "claude-opus-5-5", "effort": None, "cwd": "/tmp/mh-ws",
                              "vendor_exit_meaning": None, "stream_error": None})
        failed = ev.stream_facts(self.synth("claude", "synthetic_error.jsonl"), "claude")
        self.assertEqual((failed["vendor_exit_meaning"], failed["stream_error"]), ("run_error", "run_error"))
        stopped = ev.stream_facts(self.synth("claude", "synthetic_stopped.jsonl"), "claude")
        self.assertEqual((stopped["vendor_exit_meaning"], stopped["stream_error"]), ("incomplete", None))
        text = self.synth("devin", "stdout_ok.txt")
        self.assertEqual(ev.stream_facts(text, "devin"), dict(empty, vendor_exit_meaning=None, stream_error=None))
        rejected = ev.stream_facts(text, "devin", self.synth("devin", "stderr_tool_rejected.txt"))
        self.assertEqual((rejected["vendor_exit_meaning"], rejected["stream_error"]), (None, None))  # warnings only
        canceled = ev.stream_facts("", "devin", self.synth("devin", "stderr_login_canceled.txt"))
        self.assertEqual((canceled["vendor_exit_meaning"], canceled["stream_error"]), ("auth_failed", "auth_failed"))
        unknown = ev.stream_facts("", "devin", self.synth("devin", "stderr_unknown_model.txt"))
        self.assertEqual(unknown["vendor_exit_meaning"], "run_error")
        for aid in NEW_ADAPTERS:
            for junk in ("", None, "garbage\n[1]\n{\"type\": \"thr", "{\"a\":" * 100000):
                facts = ev.stream_facts(junk, aid)  # type: ignore[arg-type]
                self.assertEqual({k: facts[k] for k in empty}, empty, (aid, (junk or "")[:20]))
        self.assertEqual(ev.stream_facts("", "codex")["vendor_exit_meaning"], "incomplete")
        self.assertEqual(ev.stream_facts("", "devin")["vendor_exit_meaning"], None)

    def test_evaluators_on_the_new_formats(self) -> None:
        """ACC: N11, N12 - pure verdicts: S-X-6 passes on the harness SIGTERM exits and on rc 0 with a stream that just stopped, fails on an explicit stream error (codex turn.failed, claude error result, devin Error: line); devin S-X-1 needs the token in the parsed text and no Error: line; devin S-X-2 and a devin or vibe S-X-5 are never pass; R6 observation helpers count from the adapters' own facts."""
        gone = {"alive_at_term": True, "group_gone_after_term": True}
        for aid, stopped, failed in (("codex", self.synth("codex", "synthetic_stopped.jsonl"),
                                      self.synth("codex", "synthetic_turn_failed.jsonl")),
                                     ("claude", self.synth("claude", "synthetic_stopped.jsonl"),
                                      self.synth("claude", "synthetic_error.jsonl"))):
            with self.subTest(adapter=aid):
                for rc in (-15, 143, 0):
                    self.assertEqual(ev.eval_sx6(dict(gone, rc=rc), stopped, "", aid)[:2], ("pass", None), rc)
                self.assertEqual(ev.eval_sx6(dict(gone, rc=-15), failed, "", aid)[:2], ("pass", None))
                for rc in (0, 1):
                    st, reason, obs = ev.eval_sx6(dict(gone, rc=rc), failed, "", aid)
                    self.assertEqual((st, reason, obs["vendor_exit_meaning"]), ("fail", "exited_with_error", "run_error"))
                self.assertEqual(ev.eval_sx6(dict(gone, rc=-9), stopped, "", aid)[:2], ("fail", "unexpected_signal"))
                self.assertEqual(ev.eval_sx6(dict(gone, rc=0, alive_at_term=False), stopped, "", aid)[:2],
                                 ("fail", "exited_before_signal"))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=0), "1\n2\n", "", "devin")[:2], ("pass", None))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=-15), "1\n2\n", "Error: x\n", "devin")[:2], ("pass", None))
        st, reason, obs = ev.eval_sx6(dict(gone, rc=0), "1\n2\n", "Error: Login canceled\n", "devin")
        self.assertEqual((st, reason, obs["vendor_exit_meaning"]), ("fail", "exited_with_error", "auth_failed"))
        self.assertEqual(ev.eval_sx6(dict(gone, rc=0), "1\n2\n", "warning: slow\n", "devin")[:2], ("pass", None))

        class Parsed:
            def __init__(self, final_text="", vendor_exit_meaning=None, session_id=None):
                self.final_text, self.vendor_exit_meaning, self.session_id = final_text, vendor_exit_meaning, session_id
                self.usage = {}

        self.assertEqual(ev.eval_sx1("MESH_SMOKE_OK\n", 0, False, Parsed("MESH_SMOKE_OK"))[0], "pass")
        self.assertEqual(ev.eval_sx1("MESH_SMOKE_OK\n", 0, False, Parsed("MESH_SMOKE_OK", "run_error"))[:2],
                         ("fail", "vendor_error"))
        self.assertEqual(ev.eval_sx1("something else\n", 0, False, Parsed("something else"))[:2],
                         ("fail", "parse_pending"))
        # never a pass, even if a parser one day returned a session id or the output echoed the effort
        for rc, timed_out in ((0, False), (1, False), (None, True)):
            self.assertEqual(ev.eval_sx2("x", rc, timed_out, Parsed(session_id="made-up"), adapter_id="devin")[:2],
                             ("not_applicable", "resume_unsupported"))
        echo = '{"type":"system","subtype":"init","reasoning_effort":"low"}'
        for aid in ("devin", "vibe"):
            self.assertEqual(ev.eval_sx5(echo, "", 0, False, "low", adapter_id=aid)[:2],
                             ("not_applicable", "reasoning_effort_unsupported"), aid)
        self.assertEqual(ev.eval_sx5(echo, "", 0, False, "low", adapter_id="droid")[0], "pass")
        self.assertEqual(ev.eval_sx4("MESH_SMOKE_OK\n", 0, False, "swe-2-flash", adapter_id="devin")[:2],
                         ("observed", "not_observable"))
        self.assertEqual(ev.eval_sx4("MESH_SMOKE_OK\n", 0, False, "swe-2-flash", adapter_id="devin",
                                     stderr="Error: boom\n")[:2], ("fail", "vendor_error"))
        self.assertEqual(cases.codex_command_facts({"commands": 2, "failed_commands": 1, "status": "completed"}),
                         {"commands": 2, "failed_commands": 1, "file_changes": 0})
        self.assertEqual(cases.codex_command_facts(None), {"commands": 0, "failed_commands": 0, "file_changes": 0})
        denied = self.synth("claude", "synthetic_denied.jsonl")
        self.assertEqual(cases.claude_permission_denials(denied, {"denied_tools": ["Bash"], "tool_denied": 1}),
                         {"permission_denials": 1, "denied_tools": ["Bash"]})
        self.assertEqual(cases.claude_permission_denials(self.synth("claude", "synthetic_ok.jsonl"), None),
                         {"permission_denials": 0, "denied_tools": []})
        self.assertEqual(cases.devin_tool_rejections("ok\n", self.synth("devin", "stderr_tool_rejected.txt")),
                         {"tool_rejections": 2})
        self.assertEqual(cases.devin_tool_rejections("warning: rejected a tool call\n", ""), {"tool_rejections": 0})
        self.assertEqual([cases.case8(a) for a in NEW_ADAPTERS], ["S-CODEX-8", "S-CLAUDE-8", "S-DEVIN-8"])

    @unittest.skipUnless(sys.platform != "win32", "Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only")
    def test_harness_refuses_a_foreign_cwd_or_stdin(self) -> None:
        """ACC: N25 - prepare() refuses, before any spawn, a command whose cwd is not the session's /tmp/mh-smoke-* workspace and a stdin file that is not the run's prompt.md; the child's stdin is the prompt file only when the adapter asks for it."""
        binary = os.path.join(self.tmp, "codex")
        marker = os.path.join(self.tmp, "spawned")
        with open(binary, "w", encoding="utf-8") as f:
            f.write(f"#!/bin/sh\ntouch {marker}\nexit 99\n")
        make_executable(binary)
        env = {"PATH": "/usr/bin:/bin", "MESH_SMOKE": "1", "MESH_SMOKE_ADAPTERS": "codex", "MESH_SMOKE_CONSENT": "codex",
               "MESH_SMOKE_MAX_RUNS": "3", "MESH_SMOKE_MODEL_CODEX": "gpt-6.1-sol",
               "MESH_SMOKE_OUT": os.path.join(self.tmp, "out")}
        s = harness.SmokeSession(env, "codex", binary, grace_s=1, version="0.166.0")
        self.addCleanup(s.cleanup)
        run_dir, _, spec = s.prepare("S-X-1", "hello")
        self.assertEqual(spec.stdin, os.path.join(run_dir, "prompt.md"))
        with harness.child_stdin(spec) as fi:
            self.assertEqual(fi.read(), b"hello")
        real_build = s.adapter.build
        other = tempfile.mkdtemp(prefix="mh-other-", dir="/tmp")
        self.addCleanup(shutil.rmtree, other, True)
        stray = os.path.join(other, "stray.md")
        with open(stray, "w", encoding="utf-8") as f:
            f.write("not the prompt")

        def moved_cwd(req):
            spec = real_build(req)
            spec.cwd = other
            return spec

        def other_stdin(req):
            spec = real_build(req)
            spec.stdin = stray
            return spec

        for build, text in ((moved_cwd, "cwd is not the smoke workspace"), (other_stdin, "stdin is not the run's prompt")):
            with mock.patch.object(s.adapter, "build", build):
                with self.assertRaises(harness.SmokeConfigError) as cm:
                    s.run("S-X-1", "hello")
                self.assertIn(text, str(cm.exception))
                with self.assertRaises(harness.SmokeConfigError):
                    s.run_and_signal("S-X-6", "hello")
        self.assertEqual(s.runs_used, 0)
        self.assertFalse(os.path.exists(marker))

        class Devnull:
            stdin = "devnull"

        with harness.child_stdin(Devnull()) as fi:
            self.assertEqual(fi, subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
