"""gVisor environment probe for supervision primitives (plan §11.1, §11.3, §11.4).

This is an environment prerequisite check, not an acceptance-checklist item: it
proves that the primitives process supervision depends on work on this host:
/proc/<pid>/stat parsing (pgrp, session, starttime) through mesh.core.proc,
/proc/<pid>/environ of another process, PR_SET_CHILD_SUBREAPER, and orphan
re-parenting to a subreaper.

Observed on this box (gVisor): all four hold. Earlier attempts used 34 for
PR_SET_CHILD_SUBREAPER, which is PR_SET_THP_DISABLE and returns EINVAL; the
correct Linux constant is 36 (see mesh/daemon/server.py).
"""

import ctypes
import os
import select
import signal
import subprocess
import sys
import time
import unittest

from mesh.core.proc import get_proc_environ, get_proc_stat

PR_SET_CHILD_SUBREAPER = 36
PR_SET_NAME = 15

# Generous deadline for waits that end as soon as their condition holds (m3-flake-hunt).
WAIT_S = 30.0


def _set_child_subreaper() -> int:
    """Call prctl(PR_SET_CHILD_SUBREAPER, 1) and return the rc."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    ctypes.set_errno(0)
    return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)


def _read_line_or_fail(proc: subprocess.Popen, timeout_s: float = WAIT_S) -> str:
    """Read one stdout line from ``proc`` within ``timeout_s`` (m3-flake-hunt, Linear #18).

    A bare ``proc.stdout.readline()`` blocks forever if the child is alive but never
    prints, so the test would never reach its cleanup. This polls the pipe with
    ``select`` against a monotonic deadline. On timeout (or EOF before a full line)
    it kills and reaps the child, then raises ``AssertionError`` with a clear message.
    ``proc`` must have been started with ``stdout=subprocess.PIPE`` in binary mode.
    No retry.
    """
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout_s
    buf = b""
    reason = None
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = f"no line within {timeout_s:g}s"
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            reason = f"no line within {timeout_s:g}s"
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            reason = "stdout closed before a full line"
            break
        buf += chunk
    if reason is None:
        return buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        pass
    raise AssertionError(
        f"child pid {proc.pid}: {reason} (partial output {buf!r}); child killed and reaped"
    )


class TestProcProbe(unittest.TestCase):
    """Probe /proc fidelity and prctl support on real child processes."""

    def test_proc_stat_environ_and_subreaper(self) -> None:
        env = dict(os.environ)
        env["MESH_PROBE_TOKEN"] = "probe-value-123456"
        # The child reports readiness once its interpreter runs. On this gVisor host
        # /proc/<pid>/environ of a process that has only just exec'd can read back empty for a
        # few ms (seen 2/30 solo, 3/30 under load), so the probe reads /proc only after the
        # child is known to be running (m3-flake-hunt).
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        try:
            # Bounded readiness wait: a silent child fails within WAIT_S instead of hanging.
            self.assertEqual(_read_line_or_fail(proc), "ready")
            stat_info = get_proc_stat(proc.pid)
            self.assertIsNotNone(stat_info, "/proc/<pid>/stat must be readable")
            # start_new_session=True makes the child a session and group leader.
            self.assertEqual(stat_info["pgrp"], proc.pid)
            self.assertEqual(stat_info["session"], proc.pid)
            self.assertGreater(stat_info["starttime"], 0)

            child_env = get_proc_environ(proc.pid)
            self.assertIsNotNone(child_env, "/proc/<pid>/environ must be readable")
            self.assertEqual(child_env.get("MESH_PROBE_TOKEN"), "probe-value-123456")

            ctypes.set_errno(0)
            rc = _set_child_subreaper()
            err = ctypes.get_errno()

            # Control call: proves prctl is reachable and the signature is right.
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl.argtypes = [
                ctypes.c_int,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
            ]
            libc.prctl.restype = ctypes.c_int
            ctypes.set_errno(0)
            name_buf = ctypes.create_string_buffer(b"probe")
            name_rc = libc.prctl(PR_SET_NAME, ctypes.addressof(name_buf), 0, 0, 0)
            name_err = ctypes.get_errno()

            self.assertEqual(
                rc,
                0,
                "prctl(PR_SET_CHILD_SUBREAPER=36) must return 0 "
                f"(errno={err}); control PR_SET_NAME rc={name_rc} errno={name_err}",
            )
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            proc.wait(timeout=WAIT_S)
            proc.stdout.close()

    def test_silent_child_fails_within_deadline(self) -> None:
        """A live child that never prints fails the readiness wait within the deadline.

        Guards the bounded wait itself (Linear review of #18): with a short deadline
        override, ``_read_line_or_fail`` must raise AssertionError promptly and leave
        the child killed and reaped, instead of blocking forever on readline().
        """
        short_s = 0.5
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            started = time.monotonic()
            with self.assertRaises(AssertionError) as cm:
                _read_line_or_fail(proc, timeout_s=short_s)
            elapsed = time.monotonic() - started
            self.assertIn("no line within", str(cm.exception))
            # Fails at the deadline, not after the child's 30 s sleep.
            self.assertLess(elapsed, short_s + 10.0)
            self.assertIsNotNone(proc.returncode, "silent child must be reaped on timeout")
        finally:
            if proc.returncode is None:
                proc.kill()
                proc.wait(timeout=WAIT_S)
            proc.stdout.close()

    def test_orphan_grandchild_reparents_to_subreaper(self) -> None:
        """A start_new_session grandchild re-parents to this (subreaper) process."""
        self.assertEqual(_set_child_subreaper(), 0, "test process must become a subreaper")

        child_code = (
            "import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "                     start_new_session=True,\n"
            "                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
            "                     stderr=subprocess.DEVNULL)\n"
            "print(p.pid, flush=True)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            out = _read_line_or_fail(child)
            self.assertTrue(out.isdigit(), f"child must report the grandchild pid, got {out!r}")
            grandchild_pid = int(out)
            child.wait(timeout=WAIT_S)

            # The grandchild's parent exited; as a subreaper we must inherit it.
            deadline = time.monotonic() + WAIT_S
            ppid = None
            while time.monotonic() < deadline:
                gstat = get_proc_stat(grandchild_pid)
                if gstat is not None:
                    ppid = gstat["ppid"]
                    if ppid == os.getpid():
                        break
                time.sleep(0.05)
            self.assertEqual(
                ppid,
                os.getpid(),
                f"orphaned grandchild {grandchild_pid} must reparent to the subreaper",
            )
        finally:
            try:
                os.kill(grandchild_pid, signal.SIGKILL)
            except (ProcessLookupError, UnboundLocalError):
                pass
            # The re-parented grandchild becomes our child; reap it.
            deadline = time.monotonic() + WAIT_S
            while time.monotonic() < deadline:
                try:
                    pid, _ = os.waitpid(grandchild_pid, os.WNOHANG)
                except (ChildProcessError, UnboundLocalError):
                    break
                if pid:
                    break
                time.sleep(0.05)
            try:
                child.kill()
            except ProcessLookupError:
                pass
            if child.stdout is not None:
                child.stdout.close()
            try:
                child.wait(timeout=WAIT_S)
            except subprocess.TimeoutExpired:
                pass


if __name__ == "__main__":
    unittest.main()
