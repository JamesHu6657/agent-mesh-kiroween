"""PR #39 Linear review round 2: B5 request-wide git budget (validation included), D1
disowned launchers keep the admission lock."""

import os
import subprocess
import time
import unittest
from unittest import mock

from mesh.core import repos
from mesh.daemon.handlers import project
from mesh.errors import MeshError
from tests.unit.test_linear_full_scripts import RepoCase, write


class TestRequestWideBudget(unittest.TestCase):
    def test_b5_budget_spans_calls(self):
        def slow(args, cwd=None, timeout_s=30.0, **kw):
            time.sleep(min(0.6, timeout_s))
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(repos, "run_git", slow):
            t0 = time.monotonic()
            with self.assertRaises(MeshError) as cm, repos.git_budget(1.0):
                for _ in range(5):
                    repos._git("x", ["rev-parse", "HEAD"], "/")
            self.assertLess(time.monotonic() - t0, 1.8)
        self.assertEqual(cm.exception.details["reason"], "git_timeout")

    def test_b5_handler_runs_under_budget(self):
        seen = {}

        def inner(ctx):
            seen["deadline"] = getattr(repos._budget, "deadline", None)
            return "ok"

        with mock.patch.object(project, "_handle_project_create", inner):
            self.assertEqual(project.handle_project_create(object()), "ok")
        self.assertIsNotNone(seen["deadline"])
        self.assertIsNone(getattr(repos._budget, "deadline", None))


class TestDisownedLauncher(RepoCase):
    def test_d1_disowned_slow_start_keeps_lock(self):
        wt = os.path.join(self.tmp, "workspace", "mesh-d1d")
        os.makedirs(wt)
        first = os.path.join(self.tmp, "first.sh")
        write(first, f'cd "{wt}"; source "{self.repo}/scripts/coord/coder-guard.sh"; coder_guard\n'
                     "bash -c 'sleep 3; n=\"droid exe\"; exec -a \"${n}c -f p-linear-d1d\" sleep 15' < /dev/null > /dev/null 2>&1 9>&- &\n"
                     "PID=$!; disown\necho ADMITTED\n")
        second = os.path.join(self.tmp, "second.sh")
        write(second, f'cd "{wt}"; source "{self.repo}/scripts/coord/coder-guard.sh"; coder_guard\necho ADMITTED\n')
        env = dict(os.environ, HOME=self.tmp, CODER_GUARD_LOCK=os.path.join(self.tmp, "g.lock"), MAX_CODERS="5")
        try:
            p1 = subprocess.Popen(["bash", first], stdout=subprocess.PIPE, text=True, env=env)
            time.sleep(1)
            p2 = subprocess.run(["bash", second], capture_output=True, text=True, env=env, timeout=60)
            self.assertNotIn("ADMITTED", p2.stdout)
            self.assertIn("REFUSED", p2.stdout)
            p1.communicate(timeout=60)
        finally:
            subprocess.run(["pkill", "-f", "p-linear-d1d"])


if __name__ == "__main__":
    unittest.main()
