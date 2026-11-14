"""PR #39 Linear review round 1: B4 (git hooks die with a timed-out git), B5 (request-wide
git budget for project setup), D1 (slow coder start keeps the admission lock), D13 (split
multibyte record)."""

import os
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from mesh.core import gitops, repos
from mesh.errors import MeshError
from tests.unit.test_linear_full_scripts import RepoCase, git, write


class TestGitTimeoutKillsHooks(unittest.TestCase):
    def test_b4_hook_descendant_dies(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r")
            os.makedirs(repo)
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "t")
            git(repo, "config", "user.email", "t@x")
            marker = os.path.join(d, "late")
            write(os.path.join(repo, ".git", "hooks", "pre-commit"),
                  f"#!/bin/sh\n(sleep 3; touch '{marker}') &\nsleep 30\n", 0o755)
            write(os.path.join(repo, "f"), "x\n")
            git(repo, "add", "f")
            with self.assertRaises(subprocess.TimeoutExpired):
                gitops.run_git(["commit", "-q", "-m", "m"], cwd=repo, timeout_s=1.0)
            time.sleep(4)
            self.assertFalse(os.path.exists(marker), "a hook descendant outlived the git timeout")


class TestSetupIntegrationBudget(unittest.TestCase):
    def test_b5_total_budget(self):
        seen = []

        def slow(args, cwd=None, timeout_s=30.0, **kw):
            seen.append(timeout_s)
            time.sleep(0.6)
            return subprocess.CompletedProcess(args, 1 if args[0] == "show-ref" else 0, "", "")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(repos, "SETUP_INTEGRATION_BUDGET_S", 1.0), \
                mock.patch.object(repos, "run_git", slow):
            t0 = time.monotonic()
            with self.assertRaises(MeshError) as cm:
                repos.setup_integration(d, "prj_b5", "deadbeef", d)
            self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(cm.exception.details["reason"], "git_timeout")
        self.assertTrue(all(t <= 1.0 for t in seen), seen)


class TestCoderGuardSlowStart(RepoCase):
    def test_d1_slow_start_keeps_lock(self):
        wt = os.path.join(self.tmp, "workspace", "mesh-d1s")
        os.makedirs(wt)
        first = os.path.join(self.tmp, "first.sh")
        write(first, f'cd "{wt}"; source "{self.repo}/scripts/coord/coder-guard.sh"; coder_guard\n'
                     "bash -c 'sleep 22; n=\"droid exe\"; exec -a \"${n}c -f p-linear-d1s\" sleep 15' < /dev/null > /dev/null 2>&1 9>&- &\n"
                     "echo ADMITTED\n")
        second = os.path.join(self.tmp, "second.sh")
        write(second, f'cd "{wt}"; source "{self.repo}/scripts/coord/coder-guard.sh"; coder_guard\necho ADMITTED\n')
        env = dict(os.environ, HOME=self.tmp, CODER_GUARD_LOCK=os.path.join(self.tmp, "g.lock"), MAX_CODERS="5")
        try:
            p1 = subprocess.Popen(["bash", first], stdout=subprocess.PIPE, text=True, env=env)
            time.sleep(1)
            p2 = subprocess.run(["bash", second], capture_output=True, text=True, env=env, timeout=120)
            self.assertNotIn("ADMITTED", p2.stdout)
            self.assertIn("REFUSED", p2.stdout)
            p1.communicate(timeout=60)
        finally:
            subprocess.run(["pkill", "-f", "p-linear-d1s"])

    def test_d1_failed_start_releases(self):
        wt = os.path.join(self.tmp, "workspace", "mesh-d1f")
        os.makedirs(wt)
        first = os.path.join(self.tmp, "first.sh")
        write(first, f'cd "{wt}"; source "{self.repo}/scripts/coord/coder-guard.sh"; coder_guard\n'
                     "bash -c 'exit 1' 9>&- &\necho ADMITTED\n")
        env = dict(os.environ, HOME=self.tmp, CODER_GUARD_LOCK=os.path.join(self.tmp, "g.lock"))
        r = subprocess.run(["bash", first], capture_output=True, text=True, env=env, timeout=20)
        self.assertIn("ADMITTED", r.stdout)


class TestViewerMultibyte(RepoCase):
    def test_d13_split_multibyte(self):
        log = os.path.join(self.repo, "logs", "raw", "kiro-coord-2026.jsonl")
        line = '{"data": {"update": {"sessionUpdate": "tool_call", "title": "工具-D13"}}}\n'.encode("utf-8")
        cut = line.index("工".encode("utf-8")) + 1
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "wb") as f:
            f.write(line[:cut])
        p = subprocess.Popen(["python3", os.path.join(self.repo, "scripts", "coord", "view-kiro-live.py")],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            time.sleep(2.5)
            with open(log, "ab") as f:
                f.write(line[cut:])
            time.sleep(3)
        finally:
            p.send_signal(signal.SIGTERM)
            out = p.communicate(timeout=10)[0].decode("utf-8", "replace")
        self.assertIn("工具-D13", out)


if __name__ == "__main__":
    unittest.main()
