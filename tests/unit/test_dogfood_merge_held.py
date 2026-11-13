"""Dogfood merge failures (tsk_a1d2a2b6a94rw7n3v6hzz7i744).

Root cause: an SSH login (``sshd-session: user@notty``: same uid, non-dumpable, so
EACCES on /proc/<pid>/environ) started after a run's wrapper made every termination
scan of that run uncertain. During a long merge test that (a) failed the /proc-scanning
unit tests run by the merge test whenever someone SSHed in, and (b) kept the live merge
from closing: INTERNAL_ERROR, task stuck in 'merging'.

Fixes: such sessions (parent is another user's process, grandparent older than the run)
are bystanders; and a held live merge is a defined MERGE_HELD (exit 14) with recovery
steps instead of INTERNAL_ERROR.
"""

import os
import unittest
from unittest import mock

import mesh.constants as c
from mesh.daemon import merge, terminate

ME = os.geteuid()
WRAP_ST = 1000


def _fake(procs):
    """procs: pid -> (ppid, starttime, uid)."""
    def stat(pid):
        p = procs.get(int(pid))
        if p is None:
            return None
        return {"ppid": p[0], "starttime": p[1], "pgrp": pid, "session": pid, "state": "S"}

    real_stat = os.stat

    def ostat(path, *a, **k):
        if isinstance(path, str) and path.startswith("/proc/"):
            pid = int(path.split("/")[2])
            p = procs.get(pid)
            if p is None:
                raise FileNotFoundError(path)
            return mock.Mock(st_uid=p[2])
        return real_stat(path, *a, **k)

    return stat, ostat


class TestForeignSpawnedBystander(unittest.TestCase):
    def _check(self, procs, pid):
        stat, ostat = _fake(procs)
        with mock.patch.object(terminate, "get_proc_stat", stat), mock.patch("os.stat", ostat), \
                mock.patch.object(terminate, "_comm", lambda p: "sshd-session"):
            return terminate._foreign_spawned_bystander(stat(pid), WRAP_ST)

    def test_sshd_session_is_bystander(self):
        # listener (root, old) -> [priv] (root, young) -> user@notty (us, young)
        procs = {500: (1, 10, 0), 900: (500, 2000, 0), 901: (900, 2001, ME)}
        self.assertTrue(self._check(procs, 901))

    def test_reparented_escapee_is_not(self):
        procs = {901: (1, 2001, ME)}
        self.assertFalse(self._check(procs, 901))

    def test_same_uid_parent_is_not(self):
        # escapee whose parent is our own (e.g. systemd --user subreaper, old)
        procs = {400: (1, 10, ME), 901: (400, 2001, ME)}
        self.assertFalse(self._check(procs, 901))

    def test_young_grandparent_is_not(self):
        # run -> setsid sudo (root) -> child: the grandparent started after the run
        procs = {800: (1, 1500, ME), 900: (800, 2000, 0), 901: (900, 2001, ME)}
        self.assertFalse(self._check(procs, 901))

    def test_unknown_wrapper_start_is_not(self):
        procs = {500: (1, 10, 0), 900: (500, 2000, 0), 901: (900, 2001, ME)}
        stat, ostat = _fake(procs)
        with mock.patch.object(terminate, "get_proc_stat", stat), mock.patch("os.stat", ostat):
            self.assertFalse(terminate._foreign_spawned_bystander(stat(901), None))

    def test_parent_gone_is_not(self):
        procs = {901: (900, 2001, ME)}
        self.assertFalse(self._check(procs, 901))

    def test_read_error_is_not(self):
        def boom(pid):
            raise OSError(5, "EIO")
        with mock.patch.object(terminate, "get_proc_stat", boom):
            self.assertFalse(terminate._foreign_spawned_bystander({"ppid": 900, "starttime": 2001}, WRAP_ST))


class TestLiveMergeHeldError(unittest.TestCase):
    def test_held_is_defined_and_actionable(self):
        plan = {"task_id": "tsk_x", "merge_id": "mrg_x", "run_id": "run_x"}
        result = {"outcome": "failed", "test_log": "/h/merges/mrg_x/test.log", "test": {"exit_code": 0, "passed": True}}
        err = merge.LiveMergeHeld(plan, result, OSError(5, "merge test run run_x: termination scan uncertain")).to_mesh_error()
        self.assertEqual(err.code, c.MERGE_HELD)
        self.assertEqual(err.exit_code, c.EXIT_MERGE_FAILED)
        self.assertNotEqual(err.code, c.INTERNAL_ERROR)
        self.assertEqual(err.details["reason"], "close_held")
        self.assertEqual(err.details["test_exit_code"], 0)
        self.assertIn("mesh daemon stop --user; mesh daemon start", err.message)
        self.assertIn("'merging'", err.message)


if __name__ == "__main__":
    unittest.main()
