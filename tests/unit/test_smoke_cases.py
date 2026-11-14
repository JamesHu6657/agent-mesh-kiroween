"""Offline tests for the droid smoke cases and re-evaluation (m3-r3; ACC: N11, N12, N25, N24).

A stub ``droid`` written by this file under /tmp/mh-* reacts to the argv the adapter builds
(model, effort, session id, prompt file). The tests/stubs.py droid stub trips on ``exec``, so
it is not used for model runs. Every env is a dict; os.environ never gets MESH_SMOKE* or
MESH_PROBE*. Nothing touches tests/fixtures/real or tests/smoke/results.
"""

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

from mesh.adapters import smoke_schema
from mesh.core.proc import get_proc_stat
from tests import tierlib
from tests.smoke import harness
from tests.smoke import reevaluate as ev
from tests.smoke import test_smoke as cases
from tests.stubs import make_executable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTHETIC = os.path.join(REPO_ROOT, "tests", "fixtures", "synthetic", "smoke", "synthetic_droid_stream.jsonl")

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


if __name__ == "__main__":
    unittest.main()
