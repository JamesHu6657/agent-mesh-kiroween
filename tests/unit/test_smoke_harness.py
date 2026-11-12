"""Offline tests for the smoke harness core and results schema (m3-r2; ACC: N11, N12, N21, N25).

A stub ``droid`` written by this file under /tmp/mh-* replays a synthetic stream (the
tests/stubs.py droid stub trips on ``exec``). Every env is a plain dict; no test writes
MESH_SMOKE* or MESH_PROBE* into os.environ. Nothing touches tests/fixtures/real or
tests/smoke/results.
"""

import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from mesh.adapters import smoke_schema
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.kiro import KiroAdapter
from mesh.core.proc import get_proc_stat
from tests import tierlib
from tests.smoke import harness
from tests.stubs import make_executable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTHETIC = os.path.join(REPO_ROOT, "tests", "fixtures", "synthetic", "smoke", "synthetic_droid_stream.jsonl")
SECRET = "fk-PLANTEDsecretVALUE0123456789abcdef"

STUB = r'''#!/usr/bin/env python3
import os, signal, subprocess, sys, time
args = sys.argv[1:]
with open(CALLS, "a", encoding="utf-8") as f:
    f.write(repr(args) + "\n")
if args == ["--version"]:
    print("droid 1.6.0")
    sys.exit(0)
if not args or args[0] != "exec":
    sys.exit(99)
with open(ENVLOG, "w", encoding="utf-8") as f:
    f.write(repr({"cwd": os.getcwd(), "keys": sorted(os.environ)}))
mode = os.environ.get("STUB_MODE", "ok")
if mode in ("escape", "leftover"):
    code = ("import os,signal,time\n"
            + ("os.setpgid(0, 0)\n" if mode == "escape" else "")
            + "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-c", code])
    with open(PIDS, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {child.pid}")
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            st = open(f"/proc/{child.pid}/stat").read().rsplit(")", 1)[1].split()
        except OSError:
            break
        if mode == "leftover" or int(st[2]) == child.pid:
            break
        time.sleep(0.01)
    print("leaving a child behind")
    sys.exit(0)
if mode == "hang":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    with open(PIDS, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {child.pid}")
    sys.stdout.write("partial\n")
    sys.stdout.flush()
    time.sleep(60)
sys.stdout.write(open(STREAM, encoding="utf-8").read())
sys.stdout.write("token " + os.environ.get("FACTORY_API_KEY", "") + "\n")
sys.stderr.write("warn api_key=" + os.environ.get("FACTORY_API_KEY", "") + "\n")
sys.exit(0)
'''


class SmokeBase(unittest.TestCase):
    """Shared setup; no tests here."""

    def setUp(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])
        self.tmp = tempfile.mkdtemp(prefix="mh-smoke-t-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "calls.log")
        self.envlog = os.path.join(self.tmp, "env.log")
        self.pids = os.path.join(self.tmp, "pids")
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        self.binary = os.path.join(bindir, "droid")
        head = (f"CALLS = {self.calls!r}\nENVLOG = {self.envlog!r}\nPIDS = {self.pids!r}\n"
                f"STREAM = {SYNTHETIC!r}\n")
        lines = STUB.split("\n", 2)
        with open(self.binary, "w", encoding="utf-8") as f:
            f.write(lines[0] + "\n" + lines[1] + "\n" + head + lines[2])
        make_executable(self.binary)
        self.assertTrue(self.binary.startswith("/tmp/mh-"), self.binary)
        self.out = os.path.join(self.tmp, "out")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MESH_SMOKE": "1",
                    "MESH_SMOKE_ADAPTERS": "droid", "MESH_SMOKE_CONSENT": "droid", "MESH_SMOKE_MAX_RUNS": "3",
                    "MESH_SMOKE_MODEL_DROID": "claude-sonnet-5-5", "MESH_SMOKE_TIMEOUT_S": "20",
                    "MESH_SMOKE_OUT": self.out, "MESH_SMOKE_CONSENT_REF": "test consent",
                    "FACTORY_API_KEY": SECRET, "MESH_HOME": "/tmp/mh-should-not-leak"}

    def tearDown(self) -> None:
        self.assertEqual([k for k in os.environ if k.startswith(("MESH_SMOKE", "MESH_PROBE"))], [])

    def spawns(self) -> list:
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as f:
            return [l for l in f.read().splitlines() if l]

    def model_runs(self) -> list:
        return [a for a in map(eval, self.spawns()) if a[:1] == ["exec"] and "--file" in a]

    def all_files(self, root: str) -> list:
        out = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            out += [os.path.join(d, n) for n in names]
        return sorted(out)


class GateTest(SmokeBase):
    def test_closed_gate_spawns_nothing(self) -> None:
        """ACC: N25 - (a) empty env, (b) consent missing, MESH_SMOKE!=1, adapter not listed: SmokeSkip, 0 spawns, nothing written."""
        variants = {"empty": {}, "no_consent": dict(self.env, MESH_SMOKE_CONSENT="kiro"),
                    "smoke_0": dict(self.env, MESH_SMOKE="0"), "not_listed": dict(self.env, MESH_SMOKE_ADAPTERS="kiro")}
        for name, env in variants.items():
            with self.subTest(variant=name):
                self.assertIsNotNone(harness.gate(env, "droid"))
                with self.assertRaises(harness.SmokeSkip):
                    harness.SmokeSession(env, "droid", self.binary, grace_s=1)
        self.assertIsNone(harness.gate(self.env, "droid"))
        self.assertEqual(self.spawns(), [])
        self.assertFalse(os.path.exists(self.out))

    def test_config_refusals_spawn_nothing(self) -> None:
        """ACC: N25 - (i) non-absolute binary, MAX_RUNS missing/0/garbage, model missing or auto, OUT outside /tmp/mh-* or in the repo: refused before any spawn."""
        bad = {
            "relative_binary": (self.env, "droid"),
            "bare_name": (self.env, "bin/droid"),
            "no_max_runs": ({k: v for k, v in self.env.items() if k != "MESH_SMOKE_MAX_RUNS"}, self.binary),
            "max_runs_0": (dict(self.env, MESH_SMOKE_MAX_RUNS="0"), self.binary),
            "max_runs_x": (dict(self.env, MESH_SMOKE_MAX_RUNS="many"), self.binary),
            "no_model": ({k: v for k, v in self.env.items() if k != "MESH_SMOKE_MODEL_DROID"}, self.binary),
            "auto": (dict(self.env, MESH_SMOKE_MODEL_DROID="auto"), self.binary),
            "AUTO": (dict(self.env, MESH_SMOKE_MODEL_DROID="AUTO"), self.binary),
            "out_missing": ({k: v for k, v in self.env.items() if k != "MESH_SMOKE_OUT"}, self.binary),
            "out_tmp": (dict(self.env, MESH_SMOKE_OUT="/tmp/smoke-out"), self.binary),
            "out_repo": (dict(self.env, MESH_SMOKE_OUT=os.path.join(REPO_ROOT, "tests", "smoke", "results")), self.binary),
            "out_traversal": (dict(self.env, MESH_SMOKE_OUT="/tmp/mh-x/../../etc"), self.binary),
            "timeout_0": (dict(self.env, MESH_SMOKE_TIMEOUT_S="0"), self.binary),
            "missing_binary": (self.env, os.path.join(self.tmp, "nope")),
        }
        for name, (env, binary) in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(harness.SmokeConfigError):
                    harness.SmokeSession(env, "droid", binary, grace_s=1)
        self.assertEqual(self.spawns(), [])
        self.assertFalse(os.path.exists(self.out))
        cfg = harness.load_config(dict(self.env, MESH_SMOKE_TIMEOUT_S=""), "droid", self.binary)
        self.assertEqual(cfg.timeout_s, 180.0)


class RunTest(SmokeBase):
    def test_run_capture_redaction_cap_and_results(self) -> None:
        """ACC: N11, N25, N21 - (c) cap 1 -> second case not_run(run_cap); (d) writes only under MESH_SMOKE_OUT, cwd a /tmp/mh-smoke-* workspace, MESH_* stripped; (e) planted secret redacted; (f) sidecar keys and hash; results doc validates and is named per Q4."""
        env = dict(self.env, MESH_SMOKE_MAX_RUNS="1")
        before = set(self.all_files(REPO_ROOT + "/tests"))
        s = harness.SmokeSession(env, "droid", self.binary, grace_s=1, mesh_commit="abc1234")
        self.addCleanup(s.cleanup)
        self.assertEqual(s.version, "1.6.0")
        self.assertTrue(s.workspace.startswith("/tmp/mh-smoke-"))
        o = s.run_or_cap("S-X-1", "Reply with exactly MESH_SMOKE_OK")
        self.assertIsNotNone(o)
        self.assertEqual(o.rc, 0)
        self.assertFalse(o.timed_out)
        self.assertTrue(o.group_gone)
        # argv: absolute binary handed to build(); readonly adds no autonomy flag.
        runs = self.model_runs()
        self.assertEqual(len(runs), 1)  # the probe's --version/--help calls are not model runs
        argv = runs[0]
        self.assertEqual(argv[:4], ["exec", "--output-format", "stream-json", "--cwd"])
        self.assertIn("claude-sonnet-5-5", argv)
        self.assertNotIn("--auto", argv)
        self.assertNotIn("--skip-permissions-unsafe", argv)
        self.assertEqual(o.argv[0], "droid")
        with open(self.envlog, encoding="utf-8") as f:
            seen = eval(f.read())
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(s.workspace))
        self.assertEqual([k for k in seen["keys"] if k.startswith("MESH_")], [])
        # parse() sees the redacted stdout.log.
        self.assertEqual(s.parse(o).session_id, "synthetic-session-0001")
        s.record("S-X-1", "observed", outcome=o, observation={"raw_token_seen": "MESH_SMOKE_OK" in o.stdout})
        self.assertIsNone(s.run_or_cap("S-X-2", "second run"))
        self.assertEqual(len(self.model_runs()), 1)
        self.assertEqual(s.cases[-1]["status"], "not_run")
        self.assertEqual(s.cases[-1]["reason"], "run_cap")
        path = s.finish()
        self.assertEqual(path, os.path.join(self.out, "results", f"droid-1.6.0-{s.captured_at_utc[:10]}.json"))
        files = self.all_files(self.out)
        self.assertEqual(sorted(os.path.relpath(p, self.out) for p in files), sorted([
            "droid/1.6.0/smoke-S-X-1-stdout.jsonl", "droid/1.6.0/smoke-S-X-1-stdout.jsonl.meta.json",
            "droid/1.6.0/smoke-S-X-1-stderr.txt", "droid/1.6.0/smoke-S-X-1-stderr.txt.meta.json",
            os.path.relpath(path, self.out), os.path.relpath(path, self.out) + ".meta.json"]))
        for p in files:
            with open(p, encoding="utf-8") as f:
                body = f.read()
            self.assertNotIn(SECRET, body, p)
            if p.endswith(".meta.json"):
                continue
            meta = tierlib.read_sidecar(p)
            self.assertEqual(set(meta), {"command", "captured_at_utc", "sha256"})
        with open(os.path.join(self.out, "droid", "1.6.0", "smoke-S-X-1-stderr.txt"), encoding="utf-8") as f:
            self.assertIn("[REDACTED", f.read())
        self.assertIn("[REDACTED", o.stdout)
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        self.assertEqual(doc["cases"][0]["fixture"], "tests/fixtures/real/droid/1.6.0/smoke-S-X-1-stdout.jsonl")
        self.assertEqual(doc["consent_ref"], "test consent")
        self.assertEqual(doc["mesh_commit"], "abc1234")
        self.assertEqual(doc["profile"], "readonly")
        # Staging tree promotes into a temp repo (never the checkout).
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "tests", "fixtures", "real"))
        self.assertEqual(len(tierlib.promote(self.out, repo)), 3)
        self.assertEqual(smoke_schema.validate(doc, repo), [])
        self.assertFalse(os.path.exists(s.workspace))
        self.assertEqual(set(self.all_files(REPO_ROOT + "/tests")) - before, set())

    def test_hang_kills_the_group(self) -> None:
        """ACC: N11, N25 - (h) a SIGTERM-ignoring stub with a child and a 1 s timeout, grace_s=1: observation hang, rc null, whole group gone."""
        env = dict(self.env, STUB_MODE="hang", MESH_SMOKE_TIMEOUT_S="1")
        s = harness.SmokeSession(env, "droid", self.binary, grace_s=1, version="1.6.0")
        self.addCleanup(s.cleanup)
        t0 = time.monotonic()
        o = s.run("S-X-6", "long output")
        took = time.monotonic() - t0
        self.assertTrue(o.timed_out)
        self.assertIsNone(o.rc)
        self.assertTrue(o.group_gone)
        self.assertLess(took, 10.0)
        self.assertGreaterEqual(took, 1.9)  # timeout + grace, polled
        with open(self.pids, encoding="utf-8") as f:
            pids = [int(x) for x in f.read().split()]
        for pid in pids:
            st = get_proc_stat(pid)
            self.assertTrue(st is None or st["state"] in ("Z", "X") or st["pgrp"] != pids[0], (pid, st))
        case = s.record("S-X-6", "fail", reason="timeout", outcome=o)
        self.assertEqual(case["observation"], "hang")
        self.assertIn("partial", o.stdout)
        s.cleanup()

    def test_results_names_never_overwrite(self) -> None:
        """ACC: N21 - two finishes on the same day get -2; neither overwrites."""
        paths = []
        for _ in range(2):
            out = self.out
            s = harness.SmokeSession(dict(self.env, MESH_SMOKE_OUT=out), "droid", self.binary, grace_s=1, version="1.6.0")
            self.addCleanup(s.cleanup)
            s.record("S-X-7", "not_run", reason="deferred")
            paths.append(s.finish())
        self.assertEqual(os.path.basename(paths[1]), os.path.basename(paths[0])[:-5] + "-2.json")
        self.assertEqual(smoke_schema.load_latest(os.path.join(self.out, "results"), "droid"), paths[1])
        self.assertEqual(self.spawns(), [])  # version given, no model run


class R2bTest(SmokeBase):
    """Kiro r1b+r2 review findings 1-4, 5d, 6."""

    def session(self, **env) -> harness.SmokeSession:
        s = harness.SmokeSession(dict(self.env, **env), "droid", self.binary, grace_s=1, version="1.6.0")
        self.addCleanup(s.cleanup)
        return s

    def pids_of(self) -> list:
        with open(self.pids, encoding="utf-8") as f:
            return [int(x) for x in f.read().split()]

    def assert_gone(self, pids) -> None:
        for pid in pids:
            st = get_proc_stat(pid)
            self.assertTrue(st is None or st["state"] in ("Z", "X"), (pid, st))

    def test_interrupt_kills_child_and_removes_raw(self) -> None:
        """ACC: N11, N25 - KeyboardInterrupt during the wait: the run's session is terminated, no raw-* file is left, the interrupt propagates (finding 1)."""
        s = self.session(STUB_MODE="hang")
        real_wait = subprocess.Popen.wait
        pids_file = self.pids

        def interrupting_wait(proc, timeout=None):
            if timeout is not None and timeout > 5:
                deadline = time.monotonic() + 10
                while not os.path.exists(pids_file) and time.monotonic() < deadline:
                    time.sleep(0.02)
                raise KeyboardInterrupt()
            return real_wait(proc, timeout=timeout)

        with mock.patch.object(subprocess.Popen, "wait", interrupting_wait):
            with self.assertRaises(KeyboardInterrupt):
                s.run("S-X-1", "x")
        self.assert_gone(self.pids_of())
        raws = [p for p in self.all_files(s.runs_root) if os.path.basename(p).startswith("raw-")]
        self.assertEqual(raws, [])

    def test_setpgid_escapee_and_normal_exit_leftover_are_killed(self) -> None:
        """ACC: N11, N25 - a child that leaves the group with setpgid (same session), and a child left behind by a normally exiting leader, are both killed; group_gone is True only when nothing of the session remains (finding 2)."""
        for mode in ("escape", "leftover"):
            with self.subTest(mode=mode):
                if os.path.exists(self.pids):
                    os.unlink(self.pids)
                s = self.session(STUB_MODE=mode, MESH_SMOKE_OUT=os.path.join(self.tmp, f"out-{mode}"))
                o = s.run("S-X-7", "x")
                self.assertEqual(o.rc, 0)
                self.assertFalse(o.timed_out)
                leader, child = self.pids_of()
                if mode == "escape":
                    st = get_proc_stat(child)
                    self.assertTrue(st is None or st["state"] in ("Z", "X") or st["pgrp"] == child)
                self.assertTrue(o.group_gone)
                self.assert_gone([leader, child])
                self.assertFalse(harness._group_alive(leader))
                case = s.record("S-X-7", "observed", outcome=o, observation={"k": 1})
                self.assertNotIn("leftover_processes", case["observation"])
                stuck = dataclasses.replace(o, group_gone=False)
                self.assertEqual(s.record("S-X-7", "observed", outcome=stuck, observation={"k": 1})["observation"],
                                 {"k": 1, "leftover_processes": True})
                self.assertEqual(s.record("S-X-7", "observed", outcome=stuck)["observation"],
                                 {"value": None, "leftover_processes": True})

    def test_same_case_twice_refused_before_spawn(self) -> None:
        """ACC: N25 - a second run of the same case in one MESH_SMOKE_OUT raises SmokeConfigError with nothing spawned: one model run (finding 3)."""
        s = self.session()
        self.assertIsNotNone(s.run("S-X-1", "x"))
        s2 = self.session()
        for sess in (s, s2):
            with self.assertRaises(harness.SmokeConfigError):
                sess.run("S-X-1", "again")
        self.assertEqual(len(self.model_runs()), 1)
        self.assertEqual(s.runs_used, 1)
        self.assertEqual(s2.runs_used, 0)

    def test_staged_cases_are_carried(self) -> None:
        """ACC: N11, N12 - stage 2 in the same MESH_SMOKE_OUT carries the stage-1 cases it did not record with carried_from, so the latest doc holds S-X-1 and S-X-6 (finding 4)."""
        s1 = self.session(MESH_SMOKE_MAX_RUNS="1")
        o = s1.run("S-X-1", "Reply with exactly MESH_SMOKE_OK")
        s1.record("S-X-1", "pass", outcome=o)
        s1.record("S-X-6", "not_run", reason="not_selected")
        p1 = s1.finish()
        s2 = self.session(MESH_SMOKE_MAX_RUNS="1")
        o2 = s2.run("S-X-6", "long output")
        s2.record("S-X-6", "pass", outcome=o2)
        p2 = s2.finish()
        self.assertEqual(smoke_schema.load_latest(os.path.join(self.out, "results"), "droid"), p2)
        with open(p2, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.out), [])
        by = {c["id"]: c for c in doc["cases"]}
        self.assertEqual([c["id"] for c in doc["cases"]], ["S-X-1", "S-X-6"])
        self.assertEqual(by["S-X-1"]["status"], "pass")
        self.assertEqual(by["S-X-1"]["carried_from"], "tests/smoke/results/" + os.path.basename(p1))
        self.assertIsNone(by["S-X-6"]["carried_from"])
        self.assertEqual(by["S-X-6"]["status"], "pass")
        # A third stage keeps the original source.
        s3 = self.session(MESH_SMOKE_MAX_RUNS="1")
        s3.record("S-X-7", "not_run", reason="not_selected")
        with open(s3.finish(), encoding="utf-8") as f:
            by3 = {c["id"]: c for c in json.load(f)["cases"]}
        self.assertEqual(by3["S-X-1"]["carried_from"], "tests/smoke/results/" + os.path.basename(p1))
        self.assertEqual(by3["S-X-6"]["carried_from"], "tests/smoke/results/" + os.path.basename(p2))

    def test_bad_version_refused_before_spawn(self) -> None:
        """ACC: N25 - a version the results schema rejects is refused in SmokeSession.__init__, before any model run (finding 5d)."""
        for v in ("1.6.0-beta", "v1.6.0"):
            with self.assertRaises(harness.SmokeConfigError):
                harness.SmokeSession(self.env, "droid", self.binary, grace_s=1, version=v)
        self.assertEqual(self.model_runs(), [])
        self.assertFalse(os.path.exists(self.out))

    def test_main_closed_gate_subprocess(self) -> None:
        """ACC: N25 - `python3 -m tests.smoke.harness droid --prompt x` with an explicit env without MESH_SMOKE: rc 0, skipped, nothing spawned or written (finding 6)."""
        env = {k: v for k, v in self.env.items() if not k.startswith("MESH_SMOKE")}
        env["PATH"] = os.path.dirname(self.binary) + os.pathsep + env["PATH"]
        res = subprocess.run([sys.executable, "-m", "tests.smoke.harness", "droid", "--prompt", "x"], cwd=REPO_ROOT,
                             env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertTrue(out["ok"])
        self.assertIn("MESH_SMOKE", out["skipped"])
        self.assertEqual(self.spawns(), [])
        self.assertFalse(os.path.exists(self.out))


class SchemaTest(SmokeBase):
    def make_doc(self, root: str, name: str = "smoke-S-X-1-stdout.jsonl", data: bytes = b"{}\n") -> dict:
        tierlib.write_capture(root, f"droid/1.6.0/{name}", data, ["droid", "exec"])
        case = {"id": "S-X-1", "status": "pass", "reason": None, "rc": 0, "duration_s": 1.5,
                "fixture": smoke_schema.fixture_rel("droid", "1.6.0", name),
                "fixture_sha256": tierlib.sha256_bytes(data), "observation": None, "evaluated_from": None,
                "carried_from": None}
        return {"schema": "mesh.smoke.v1", "adapter": "droid", "binary": "/usr/local/bin/droid", "version": "1.6.0",
                "captured_at_utc": "2026-11-20T03:04:05Z", "mesh_commit": "abc", "model": "claude-sonnet-5-5",
                "effort": None, "profile": "readonly", "timeout_s": 180, "consent_ref": "James 2026-11-09 12:44",
                "cases": [case]}

    def test_case_caps_use_real_capability_keys(self) -> None:
        """ACC: N12, N20 - CASE_CAPS maps onto droid/kiro capability keys only; launch cases; statuses; capabilities_used = supported yes."""
        keys = set(DroidAdapter().capabilities) | set(KiroAdapter().capabilities)
        for cid, caps in smoke_schema.CASE_CAPS.items():
            self.assertTrue(set(caps) <= keys, (cid, caps))
        self.assertEqual(smoke_schema.CASE_CAPS["S-X-1"], ("headless", "stream_output"))
        self.assertEqual(smoke_schema.cases_for_capability("resume"), ["S-X-2", "S-X-3"])
        self.assertEqual(smoke_schema.cases_for_capability("reasoning_effort"), ["S-X-5"])
        self.assertEqual(smoke_schema.LAUNCH_CASES, ("S-X-1", "S-X-6"))
        self.assertEqual(smoke_schema.STATUSES, ("pass", "fail", "observed", "not_applicable", "not_run", "blocked"))
        self.assertEqual(smoke_schema.capabilities_used(DroidAdapter()),
                         ["headless", "model_selection", "reasoning_effort", "stream_output"])
        for cap in smoke_schema.capabilities_used(DroidAdapter()):
            self.assertTrue(smoke_schema.cases_for_capability(cap), cap)

    def test_validate(self) -> None:
        """ACC: N12, N21 - (g) a doc referencing a synthetic file fails; fixture outside <adapter>/<version>, missing sidecar, sha mismatch and field errors fail; a good doc is clean."""
        root = os.path.join(self.tmp, "fx")
        doc = self.make_doc(root)
        self.assertEqual(smoke_schema.validate(doc, REPO_ROOT, fixture_root=root), [])
        # The committed checkout has no droid 1.6.0 tree: the fixture is missing there.
        self.assertTrue(smoke_schema.validate(doc, REPO_ROOT))

        def errs(mutate) -> list:
            d = json.loads(json.dumps(doc))
            mutate(d)
            return smoke_schema.validate(d, REPO_ROOT, fixture_root=root)

        def case(**kw):
            return lambda d: d["cases"][0].update(kw)

        rel_syn = os.path.relpath(SYNTHETIC, REPO_ROOT)
        bad = {
            "synthetic_path": case(fixture=rel_syn),
            "synthetic_name": case(fixture=smoke_schema.fixture_rel("droid", "1.6.0", "synthetic_x.jsonl")),
            "evaluated_from_synthetic": case(evaluated_from=rel_syn),
            "other_version": case(fixture=smoke_schema.fixture_rel("droid", "1.3.0", "help.txt")),
            "other_adapter": case(fixture=smoke_schema.fixture_rel("kiro", "1.6.0", "smoke-S-X-1-stdout.jsonl")),
            "traversal": case(fixture="tests/fixtures/real/droid/1.6.0/../1.3.0"),
            "outside": case(fixture="docs/plan.md"),
            "sha": case(fixture_sha256="0" * 64),
            "status": case(status="ok"),
            "reason_missing": case(status="fail"),
            "case_id": case(id="S-X-99"),
            "extra_case_key": case(note="x"),
            "rc_type": case(rc="0"),
            "duration": case(duration_s=-1),
            "schema": lambda d: d.update(schema="mesh.smoke.v0"),
            "auto": lambda d: d.update(model="auto"),
            "profile": lambda d: d.update(profile="dev"),
            "binary_rel": lambda d: d.update(binary="droid"),
            "ts": lambda d: d.update(captured_at_utc="2026-11-20"),
            "missing_key": lambda d: d.pop("consent_ref"),
            "extra_key": lambda d: d.update(extra=1),
            "dup_case": lambda d: d["cases"].append(dict(d["cases"][0])),
            # Kiro r2 finding 5c: dict-valued sources are refused (string or null only)
            "carried_from_dict_synthetic": case(carried_from={"path": rel_syn}),
            "evaluated_from_dict": case(evaluated_from={"fixture": "x"}),
            "carried_from_synthetic": case(carried_from="tests/smoke/results/synthetic_droid-1.6.0-2026-11-20.json"),
        }
        for name, mutate in bad.items():
            with self.subTest(case=name):
                self.assertTrue(errs(mutate), name)
        # missing sidecar / tampered file
        os.unlink(os.path.join(root, "droid", "1.6.0", "smoke-S-X-1-stdout.jsonl.meta.json"))
        self.assertTrue(any("sidecar" in e for e in smoke_schema.validate(doc, REPO_ROOT, fixture_root=root)))
        root2 = os.path.join(self.tmp, "fx2")
        doc2 = self.make_doc(root2)
        with open(os.path.join(root2, "droid", "1.6.0", "smoke-S-X-1-stdout.jsonl"), "ab") as f:
            f.write(b"x")
        self.assertTrue(smoke_schema.validate(doc2, REPO_ROOT, fixture_root=root2))
        self.assertEqual(smoke_schema.validate([], REPO_ROOT), ["document is not an object"])
        # 5a: a real, signed non-capture file (help.txt) cannot back a case.
        root3 = os.path.join(self.tmp, "fx3")
        doc3 = self.make_doc(root3)
        tierlib.write_capture(root3, "droid/1.6.0/help.txt", b"Usage: droid\n", ["droid", "--help"])
        doc3["cases"][0]["fixture"] = smoke_schema.fixture_rel("droid", "1.6.0", "help.txt")
        doc3["cases"][0]["fixture_sha256"] = tierlib.sha256_bytes(b"Usage: droid\n")
        self.assertTrue(any("smoke-S-" in e for e in smoke_schema.validate(doc3, REPO_ROOT, fixture_root=root3)))
        # 5b: sidecar command and timestamp are checked like tierlib.read_sidecar.
        for field, value in (("command", []), ("command", "droid"), ("captured_at_utc", "yesterday")):
            root4 = os.path.join(self.tmp, f"fx4-{field}-{type(value).__name__}")
            doc4 = self.make_doc(root4)
            side = os.path.join(root4, "droid", "1.6.0", "smoke-S-X-1-stdout.jsonl.meta.json")
            with open(side, encoding="utf-8") as f:
                meta = json.load(f)
            meta[field] = value
            with open(side, "w", encoding="utf-8") as f:
                json.dump(meta, f)
            with self.subTest(sidecar=field, value=value):
                self.assertTrue(any("sidecar" in e for e in smoke_schema.validate(doc4, REPO_ROOT, fixture_root=root4)))
        # 5d: the schema's version rule.
        self.assertTrue(smoke_schema.version_ok("1.6.0"))
        for v in ("1.6.0-beta", "v1.6", "", None):
            self.assertFalse(smoke_schema.version_ok(v), v)

    def test_names_and_latest(self) -> None:
        """ACC: N12 - Q4 names with -2/-3 collisions; load_latest orders by (date, suffix) and ignores sidecars and other adapters."""
        self.assertEqual(smoke_schema.results_name("droid", "1.6.0", "2026-11-20", []), "droid-1.6.0-2026-11-20.json")
        self.assertEqual(smoke_schema.results_name("droid", "1.6.0", "2026-11-20", ["droid-1.6.0-2026-11-20.json"]),
                         "droid-1.6.0-2026-11-20-2.json")
        self.assertEqual(smoke_schema.results_name("droid", "1.6.0", "2026-11-20",
                                                   ["droid-1.6.0-2026-11-20.json", "droid-1.6.0-2026-11-20-2.json"]),
                         "droid-1.6.0-2026-11-20-3.json")
        for bad in (("Droid", "1.6.0", "2026-11-20"), ("droid", "v1", "2026-11-20"), ("droid", "1.6.0", "20261120")):
            with self.assertRaises(ValueError):
                smoke_schema.results_name(*bad, [])
        d = os.path.join(self.tmp, "results")
        os.makedirs(d)
        names = ["droid-1.6.0-2026-11-20.json", "droid-1.6.0-2026-11-20-2.json", "droid-1.6.0-2026-11-20-10.json",
                 "droid-1.5.0-2026-11-19-3.json", "droid-1.6.0-2026-11-21.json.meta.json", "kiro-3.1.0-2026-12-01.json",
                 "droid-1.6.0-2026-11-20-1.json", "notes.txt"]
        for n in names:
            with open(os.path.join(d, n), "w") as f:
                f.write("{}")
        self.assertEqual(os.path.basename(smoke_schema.load_latest(d, "droid")), "droid-1.6.0-2026-11-20-10.json")
        self.assertEqual(os.path.basename(smoke_schema.load_latest(d, "kiro")), "kiro-3.1.0-2026-12-01.json")
        self.assertIsNone(smoke_schema.load_latest(d, "agy"))
        self.assertIsNone(smoke_schema.load_latest(os.path.join(self.tmp, "none"), "droid"))
        self.assertEqual(smoke_schema.parse_results_name("droid", "droid-1.6.0-2026-11-20-2.json"), ("1.6.0", "2026-11-20", 2))


if __name__ == "__main__":
    unittest.main()
