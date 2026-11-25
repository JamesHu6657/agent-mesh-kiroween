"""Full review r7 (R2-P2-1): a same-uid non-dumpable daemon the *user* starts during a run
(ssh-agent, gpg-agent: environ EACCES, PPid 1) no longer makes every scan of that run
uncertain, while a non-dumpable escapee of the run itself still does.

The scenario runs in a forked child that plays meshd (a child subreaper that spawns the
run's wrapper); the test process, outside that tree, starts the bystander."""

import ctypes
import os
import subprocess
import sys
import time
import unittest

from mesh.core.proc import get_proc_stat
from mesh.daemon import terminate

MESH_PROC_LOCK = "shared"

PR_SET_CHILD_SUBREAPER = 36

#: A detached non-dumpable process: fork, setsid, PR_SET_DUMPABLE=0, sleep; the first
#: child exits at once so the sleeper is orphaned (like `eval $(ssh-agent)`).
NON_DUMPABLE = (
    "import ctypes, os, sys, time\n"
    "if os.fork():\n"
    "    os._exit(0)\n"
    "os.setsid()\n"
    "ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)\n"
    "sys.stdout.write(str(os.getpid()) + '\\n'); sys.stdout.flush()\n"
    "fd = os.open(os.devnull, os.O_RDWR)\n"
    "for n in (0, 1, 2):\n"
    "    os.dup2(fd, n)\n"
    "time.sleep(60)\n"
)


def _spawn_non_dumpable(env=None):
    p = subprocess.Popen([sys.executable, "-c", NON_DUMPABLE], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env, text=True)
    with p.stdout:
        pid = int(p.stdout.readline())
    p.wait()
    return pid


def _kill(pid):
    try:
        os.kill(pid, 9)
    except OSError:
        pass


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux PR_SET_CHILD_SUBREAPER, PR_SET_DUMPABLE=0, and /proc/<pid>/environ EACCES semantics")
class NonDumpableBystanderTest(unittest.TestCase):
    def _scenario(self, bystander_outside: bool, escapee_inside: bool):
        """Returns (uncertain, found) of one run_targets scan done by a subreaper 'meshd'."""
        r_ready, w_ready = os.pipe()
        r_go, w_go = os.pipe()
        r_res, w_res = os.pipe()
        child = os.fork()
        if child == 0:  # "meshd"
            code = 1
            try:
                os.close(r_ready); os.close(w_go); os.close(r_res)
                ctypes.CDLL(None).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
                time.sleep(0.05)  # the wrapper must start in a later clock tick than meshd
                wrapper = subprocess.Popen(["sleep", "60"], start_new_session=True, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wst = int(get_proc_stat(wrapper.pid)["starttime"])
                time.sleep(0.05)
                os.write(w_ready, b"r")
                os.read(r_go, 1)
                inside = None
                if escapee_inside:
                    env = dict(os.environ, MESH_RUN_ID="run_r7")
                    inside = _spawn_non_dumpable(env)  # orphaned: reparents to this subreaper
                    time.sleep(0.1)
                res = terminate.run_targets("run_r7", wrapper.pid, wrapper.pid, wst)
                msg = ("1" if getattr(res, "uncertain", False) else "0") + ",".join(str(p) for p in sorted(res))
                os.write(w_res, msg.encode())
                wrapper.kill(); wrapper.wait()
                if inside:
                    _kill(inside)
                code = 0
            except BaseException:  # surface the child's failure in the test log
                import traceback
                traceback.print_exc()
            finally:
                os._exit(code)
        os.close(w_ready); os.close(r_go); os.close(w_res)
        outside = None
        try:
            os.read(r_ready, 1)
            if bystander_outside:
                outside = _spawn_non_dumpable()
                time.sleep(0.1)
            os.write(w_go, b"g")
            msg = os.read(r_res, 65536).decode()
        finally:
            if outside:
                _kill(outside)
            os.waitpid(child, 0)
        return msg[0] == "1", msg[1:]

    def test_user_daemon_started_during_the_run_does_not_wedge_the_scan(self):
        uncertain, _found = self._scenario(bystander_outside=True, escapee_inside=False)
        self.assertFalse(uncertain)

    def test_non_dumpable_escapee_of_the_run_stays_uncertain(self):
        uncertain, _found = self._scenario(bystander_outside=False, escapee_inside=True)
        self.assertTrue(uncertain)

    def test_both_still_uncertain(self):
        uncertain, _found = self._scenario(bystander_outside=True, escapee_inside=True)
        self.assertTrue(uncertain)


class OutsideTreeTest(unittest.TestCase):
    ROOT = (1000, 500)  # meshd pid, meshd starttime

    def test_chain_to_init_without_meshd_is_outside(self):
        stats = {2000: {"ppid": 1999, "starttime": 900}, 1999: {"ppid": 1, "starttime": 800}}
        self.assertTrue(terminate._outside_tree(2000, stats[2000], stats, self.ROOT))

    def test_chain_through_meshd_is_inside(self):
        stats = {2000: {"ppid": 1500, "starttime": 900}, 1500: {"ppid": 1000, "starttime": 600}}
        self.assertFalse(terminate._outside_tree(2000, stats[2000], stats, self.ROOT))

    def test_parent_older_than_meshd_is_outside(self):
        stats = {2000: {"ppid": 300, "starttime": 900}, 300: {"ppid": 250, "starttime": 100}}
        self.assertTrue(terminate._outside_tree(2000, stats[2000], stats, self.ROOT))

    def test_reused_parent_pid_is_no_proof(self):
        # The recorded parent started after the child: its pid was reused mid-walk.
        stats = {2000: {"ppid": 1999, "starttime": 900}, 1999: {"ppid": 1, "starttime": 950}}
        self.assertFalse(terminate._outside_tree(2000, stats[2000], stats, self.ROOT))

    def test_adopted_run_gets_no_tree_root(self):
        me = get_proc_stat(os.getpid())
        self.assertIsNone(terminate._tree_root_for(int(me["starttime"]) - 1))
        self.assertIsNone(terminate._tree_root_for(None))


if __name__ == "__main__":
    unittest.main()
