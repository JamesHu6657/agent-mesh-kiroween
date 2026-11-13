"""Integration tests for the merge test step (plan §10.5 step 3, §11.4; m2-r6).

K0 fixture repos under /tmp/mh-*-git, projects created with the K0 unittest command
(`["python3","-m","unittest"]`), meshd with the fixture's hermetic git env. The helpers
are the ones of tests/integration/test_merge.py (borrowed, not inherited, so its tests do
not run twice). The timeout path is tested on `merge.run_test_step` directly with a 1 s
limit: `projects.test_timeout_s` has no CLI flag yet (m2-r6 notes).

ACC: K5, K7, K11 (partial: remote refs unchanged).
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from typing import Any, Dict
from unittest import mock

import mesh.constants as c
from mesh.core.paths import get_install_root
from mesh.daemon import children, merge, terminate
from tests.integration import test_merge as tm

K0_TESTS = '["python3","-m","unittest"]'

BROKEN_CALC = "def add(a, b):\n    return a - b\n"

ENV_PROBE = (
    "import os\n"
    "import unittest\n"
    "\n"
    "\n"
    "class TestEnv(unittest.TestCase):\n"
    "    def test_env(self):\n"
    "        env = os.environ\n"
    "        self.assertTrue(env.get('MESH_RUN_ID', '').startswith('run_'))\n"
    "        for name in ('MESH_TOKEN', 'MESH_SOCKET', 'MESH_TASK_ID', 'MESH_FENCE', 'MESH_PROJECT_ID'):\n"
    "            self.assertNotIn(name, env)\n"
    "        self.assertNotIn('_MESH_ORIG_PYTHONPATH', env)\n"
    "        print('ENV-PROBE-OK', env['MESH_RUN_ID'])\n"
)


class TestMergeTests(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project
    integ_branch = tm.TestMerge.integ_branch
    integ_wt = tm.TestMerge.integ_wt
    show = tm.TestMerge.show
    start_task = tm.TestMerge.start_task
    commit_file = tm.TestMerge.commit_file
    submit_accept = tm.TestMerge.submit_accept
    accepted_task = tm.TestMerge.accepted_task
    merge = tm.TestMerge.merge
    q = tm.TestMerge.q
    lead1_merge_failed = tm.TestMerge.lead1_merge_failed
    merge_runs = tm.TestMerge.merge_runs
    assert_no_merge_lease = tm.TestMerge.assert_no_merge_lease
    assert_integration_restored = tm.TestMerge.assert_integration_restored
    assert_remote_unchanged = tm.TestMerge.assert_remote_unchanged

    def make_tested_project(self, name: str) -> tuple:
        repo, prj = self.make_project(name, test_command=K0_TESTS)
        self.remote_before = self.fixture.remote_refs(repo)
        self.repo = repo
        return repo, prj

    def test_k5_tests_pass_then_merged(self) -> None:
        """ACC: K5 - tests pass: rc 0, `merged`, merge_commit an ancestor of integration, tests passed, log under $H/merges/."""
        repo, prj = self.make_tested_project("T5")
        tsk, _ = self.accepted_task(prj, "Env probe", "test_env_probe.py", ENV_PROBE)
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        data = resp["data"]
        self.assertEqual(data["status"], "merged", data)
        self.assertEqual(data["tests"], "passed", data)
        self.assertIs(data["noop"], False)
        log = data["test_log"]
        merges_root = os.path.join(self.harness.home, "merges") + os.sep
        self.assertTrue(log.startswith(merges_root), log)
        self.assertEqual(os.path.basename(log), "test.log")
        integ_tip = self.fixture.head(repo, self.integ_branch(prj))
        self.assertEqual(integ_tip, data["merge_commit"])
        self.assertEqual(self.fixture.git(repo, "merge-base", "--is-ancestor", data["merge_commit"],
                                          self.integ_branch(prj), check=False).returncode, 0)
        parents = self.fixture.git(repo, "rev-list", "--parents", "-n", "1", data["merge_commit"]).stdout.split()
        self.assertEqual(parents[1], pre_head)

        d = self.show(tsk)
        self.assertEqual(d["status"], "merged")
        mj = d["merge_json"] if "merge_json" in d else d["merge"]
        self.assertEqual(mj["tests"], "passed", mj)
        self.assertEqual(mj["test_log"], log)
        self.assertEqual(mj["test"]["exit_code"], 0, mj)
        self.assertIs(mj["test"]["timeout"], False)

        # Both streams in one redacted file; the env probe ran with MESH_RUN_ID = the merge run.
        with open(log, "r", encoding="utf-8") as f:
            text = f.read()
        runs = self.merge_runs(tsk)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertIn(f"ENV-PROBE-OK {run['id']}", text)  # stdout
        self.assertIn("Ran 2 tests", text)                 # unittest writes this to stderr
        self.assertEqual(oct(os.stat(log).st_mode & 0o777), oct(0o600))
        self.assertEqual(run["status"], "exited")
        self.assertEqual(run["end_reason"], "merged")
        self.assertIsNotNone(run["wrapper_pid"])
        self.assertEqual(run["run_dir"].rstrip("/"), os.path.dirname(log))
        self.assertEqual(terminate.run_targets(run["id"], None, None), {})
        self.assert_no_merge_lease(prj)
        self.assert_remote_unchanged()

    def test_k7_failing_tests_abort_the_merge(self) -> None:
        """ACC: K7 - a commit that breaks the tests: rc 14 TESTS_FAILED, log_path, integration HEAD unchanged and clean."""
        repo, prj = self.make_tested_project("T7")
        tsk, _ = self.accepted_task(prj, "Break add", "calc.py", BROKEN_CALC)
        pre_head = self.fixture.head(repo, self.integ_branch(prj))
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        err = resp["error"]
        self.assertEqual(err["code"], c.TESTS_FAILED, err)
        det = err["details"]
        self.assertEqual(det["reason"], "tests_failed")
        self.assertIs(det["timeout"], False)
        self.assertNotEqual(det["exit_code"], 0)
        self.assertTrue(det["log_path"].startswith(os.path.join(self.harness.home, "merges") + os.sep), det)
        self.assertTrue(os.path.isfile(det["log_path"]))
        self.assertLessEqual(len(det["log_tail"]), 50)
        self.assertTrue(any("FAILED" in line for line in det["log_tail"]), det["log_tail"])
        self.assertIs(det["restored"], True, det)
        self.assertEqual(det["pre_head"], pre_head)
        # The failing test ids are parsed out of the log and named in the message.
        self.assertGreaterEqual(det["failing_tests_total"], 1, det)
        self.assertIs(det["failing_tests_truncated"], False)
        ids = [t["id"] for t in det["failing_tests"]]
        self.assertTrue(all("." in i and i.split(".")[-1].startswith("test") for i in ids), ids)
        for i in ids:
            self.assertIn(i, err["message"])
        self.assertIn(f"log: {det['log_path']}", err["message"])
        self.assert_integration_restored(repo, prj, pre_head)
        self.assertEqual(self.fixture.git(self.integ_wt(prj), "show", "HEAD:calc.py").stdout,
                         "def add(a, b):\n    return a + b\n")

        d = self.show(tsk)
        self.assertEqual(d["status"], "merge_failed")
        mj = d["merge_json"] if "merge_json" in d else d["merge"]
        self.assertEqual(mj["tests"], "failed", mj)
        self.assertEqual(mj["reason"], "tests_failed")
        self.assertEqual(mj["test_log"], det["log_path"])
        msgs = self.lead1_merge_failed(tsk)
        self.assertTrue(any(m.get("reason") == "tests_failed" for m in msgs), msgs)
        run = self.merge_runs(tsk)[0]
        self.assertEqual(run["end_reason"], "tests_failed")
        self.assert_no_merge_lease(prj)
        self.assert_remote_unchanged()


    def test_k7_text_mode_names_failing_tests_and_log(self) -> None:
        """Text mode: a one-line "running tests (log: ...)" note on stderr at start, and the
        TESTS_FAILED message lists the failing test ids and the log path."""
        repo, prj = self.make_tested_project("T7T")
        slow_fail = ("import time\nimport unittest\n\n\nclass TestSlow(unittest.TestCase):\n"
                     "    def test_slow_fail(self):\n        time.sleep(3)\n        self.fail('boom')\n")
        tsk, _ = self.accepted_task(prj, "Slow failing test", "test_slow.py", slow_fail)
        rc, resp = self.harness.cli("--format", "text", "task", "merge", tsk, token=self.tok_lead1, timeout=120.0)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        err = resp["raw_stderr"]
        self.assertIn(f"mesh: merging {tsk}: running the project's tests", err)
        mrg = self.show(tsk)["merge"]["mrg"]
        log = os.path.join(os.path.realpath(self.harness.home), "merges", mrg, "test.log")
        self.assertIn(f"(log: {log})", err)
        self.assertIn("[TESTS_FAILED]", err)
        self.assertIn("failing test", err)
        self.assertIn("  FAIL: test_slow.TestSlow.test_slow_fail", err)
        self.assertIn(f"  log: {log}", err)


class TestRunTestStep(unittest.TestCase):
    """`merge.run_test_step` on its own (no meshd): timeout, pass, env."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-mt-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cwd = os.path.join(self.tmp, "integ")
        os.makedirs(self.cwd)
        self.n = 0

    def plan(self, argv: list, timeout_s: int = 30) -> Dict[str, Any]:
        self.n += 1
        run_id = f"run_mt{os.getpid()}x{self.n}x{int(time.time() * 1000)}"
        return {"run_id": run_id, "run_dir": os.path.join(self.tmp, "merges", f"mrg_{self.n}"),
                "integration": self.cwd, "test_argv": argv, "test_timeout_s": timeout_s, "kill_grace_s": 1}

    def test_timeout_terminates_group_and_escapee(self) -> None:
        """ACC: K7 (timeout clause) - 1 s limit, a sleeping command plus a setsid escapee: both terminated, timeout reported."""
        p = self.plan(["sh", "-c", "setsid sleep 60 & echo started; sleep 60"], timeout_s=1)
        t0 = time.monotonic()
        out = merge.run_test_step(p)
        elapsed = time.monotonic() - t0
        self.assertIs(out["timeout"], True, out)
        self.assertIs(out["passed"], False)
        self.assertEqual(out["timeout_s"], 1)
        self.assertLess(elapsed, 15.0, out)
        self.assertGreaterEqual(out["termination"]["targets"], 2, out)  # group + MESH_RUN_ID escapee
        self.assertNotIn("kill_failed", out["termination"])
        self.assertEqual(terminate.run_targets(p["run_id"], None, None), {})
        self.assertIsNone(terminate.get(p["run_id"]))
        self.assertNotIn(out["wrapper"]["pid"], children.registered_pids())
        with open(out["log_path"], "r", encoding="utf-8") as f:
            self.assertIn("started", f.read())
        self.assertEqual(out["log_path"], os.path.join(p["run_dir"], "test.log"))

    def test_pass_combines_streams_and_cleans_env(self) -> None:
        """ACC: K5 (test-step clause) - exit 0 passes; stdout+stderr in test.log; no MESH_TOKEN, no install-root PYTHONPATH."""
        install_root = get_install_root()
        code = (
            "import os, sys\n"
            "pp = os.environ.get('PYTHONPATH', '')\n"
            f"bad = {install_root!r} in pp.split(os.pathsep) or 'MESH_TOKEN' in os.environ\n"
            "print('out-line'); sys.stderr.write('err-line\\n')\n"
            "print('RUNID', os.environ.get('MESH_RUN_ID'))\n"
            "sys.exit(3 if bad else 0)\n"
        )
        p = self.plan([sys.executable, "-c", code])
        with mock.patch.dict(os.environ, {"MESH_TOKEN": "mesh_rt_" + "z" * 32, "_MESH_ORIG_PYTHONPATH": "__UNSET__",
                                          "PYTHONPATH": install_root}):
            out = merge.run_test_step(p)
        self.assertIs(out["passed"], True, out)
        self.assertEqual(out["exit_code"], 0)
        self.assertIs(out["timeout"], False)
        with open(out["log_path"], "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("out-line", text)
        self.assertIn("err-line", text)
        self.assertIn(f"RUNID {p['run_id']}", text)
        self.assertFalse(os.path.exists(os.path.join(p["run_dir"], "stdout.log")))
        with open(os.path.join(p["run_dir"], "spec.json"), "r", encoding="utf-8") as f:
            spec = json.load(f)
        self.assertNotIn("mesh_rt_", json.dumps(spec))
        self.assertEqual(spec["combined_log"], "test.log")

    def test_nonzero_exit_fails(self) -> None:
        """ACC: K7 (test-step clause) - a non-zero exit is a failure with its exit code."""
        out = merge.run_test_step(self.plan([sys.executable, "-c", "import sys; sys.exit(4)"]))
        self.assertIs(out["passed"], False)
        self.assertEqual(out["exit_code"], 4)
        self.assertIs(out["timeout"], False)


if __name__ == "__main__":
    unittest.main()
