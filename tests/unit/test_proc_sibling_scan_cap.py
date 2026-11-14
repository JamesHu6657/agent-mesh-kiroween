"""M3 gate re-review P3-2: ``proc._sibling_thread_environ`` bounds its gVisor fallback scan.

ACC: N18, A8

Offline and host-independent: every /proc access for the fake leader-zombie pid is mocked
(``/proc/<pid>/task`` missing, as on gVisor once the leader exits; ``pid_max`` large), so the
tests run the gVisor branch on any kernel. Past ``SIBLING_SCAN_MAX_PROBES`` probes the scan
stops and raises ``OSError(EIO)`` (unknown, never "no sibling"); within the cap it behaves as
before.
"""

import builtins
import contextlib
import errno
import io
import os
import re
import unittest
from typing import Iterator, List, Optional
from unittest import mock

from mesh.core import proc as mproc

PID = 4_000_000  # fake leader-zombie pid; never a real process here, every access is mocked
STATUS_RE = re.compile(r"^/proc/(\d+)/status$")
ENV = b"MESH_RUN_ID=run_cap\x00PATH=/bin\x00"


@contextlib.contextmanager
def fake_gvisor(pid_max: int, cap: int, sibling: Optional[int] = None,
                leader_environ: Optional[bytes] = None) -> Iterator[List[int]]:
    """Mock a gVisor leader-zombie ``PID``: no ``/proc/PID/task``, only ``sibling`` (if any)
    reports ``Tgid: PID``. Yields the list of candidate pids whose status was probed."""
    probes: List[int] = []
    real_open = builtins.open
    real_listdir = os.listdir

    def fake_listdir(path, *a, **k):
        if path == f"/proc/{PID}/task":
            raise FileNotFoundError(errno.ENOENT, "gVisor: task dir gone with the leader", path)
        return real_listdir(path, *a, **k)

    def fake_open(file, *a, **k):
        if isinstance(file, str):
            m = STATUS_RE.match(file)
            if m:
                cand = int(m.group(1))
                probes.append(cand)
                if sibling is not None and cand == sibling:
                    return io.StringIO(f"Name:\tworker\nTgid:\t{PID}\nPid:\t{cand}\n")
                raise FileNotFoundError(errno.ENOENT, "no such process", file)
            if sibling is not None and file == f"/proc/{sibling}/environ":
                return io.BytesIO(ENV)
            if leader_environ is not None and file == f"/proc/{PID}/environ":
                return io.BytesIO(leader_environ)
        return real_open(file, *a, **k)

    with mock.patch.object(mproc, "_pid_max", return_value=pid_max), \
            mock.patch.object(mproc, "SIBLING_SCAN_MAX_PROBES", cap), \
            mock.patch.object(mproc.os, "listdir", fake_listdir), \
            mock.patch.object(builtins, "open", fake_open):
        yield probes


class TestSiblingScanCap(unittest.TestCase):
    def test_default_cap_is_bounded(self):
        """ACC: N18 - the default cap is a positive bound well below a 64-bit pid_max (4194304)."""
        self.assertGreater(mproc.SIBLING_SCAN_MAX_PROBES, 0)
        self.assertLessEqual(mproc.SIBLING_SCAN_MAX_PROBES, 65536)

    def test_cap_exceeded_raises_eio_after_cap_probes(self):
        """ACC: N18, A8 - pid_max 10,000,000 and no sibling: exactly `cap` status probes, then
        OSError(EIO) (unknown), not None ("no sibling") and not a walk of the whole pid space."""
        with fake_gvisor(pid_max=10_000_000, cap=50) as probes:
            with self.assertRaises(OSError) as cm:
                mproc._sibling_thread_environ(PID)
        self.assertEqual(cm.exception.errno, errno.EIO)
        self.assertEqual(len(probes), 50)
        self.assertEqual(probes[0], PID + 1)
        self.assertEqual(probes[-1], PID + 50)

    def test_sibling_within_cap_is_found(self):
        """ACC: N18 - a live sibling at a small offset is still found (gVisor tid right after
        the leader): its environ is returned and the scan stops there."""
        with fake_gvisor(pid_max=10_000_000, cap=50, sibling=PID + 3) as probes:
            data = mproc._sibling_thread_environ(PID)
        self.assertEqual(data, ENV)
        self.assertEqual(probes, [PID + 1, PID + 2, PID + 3])

    def test_sibling_beyond_cap_is_unknown_not_missing(self):
        """ACC: N18, A8 - a sibling past the cap is not reached: the result is EIO (unknown),
        never None, so no caller concludes "no MESH_RUN_ID" from the cut-off scan."""
        with fake_gvisor(pid_max=10_000_000, cap=50, sibling=PID + 51) as probes:
            with self.assertRaises(OSError) as cm:
                mproc._sibling_thread_environ(PID)
        self.assertEqual(cm.exception.errno, errno.EIO)
        self.assertNotIn(PID + 51, probes)

    def test_full_scan_below_cap_keeps_old_result(self):
        """ACC: N18 - when pid_max - 1 <= cap the whole space is scanned as before: no sibling
        gives None (no error)."""
        with fake_gvisor(pid_max=64, cap=100) as probes:
            self.assertIsNone(mproc._sibling_thread_environ(PID))
        self.assertLessEqual(len(probes), 63)
        self.assertGreater(len(probes), 0)

    def test_get_proc_environ_propagates_cap_eio(self):
        """ACC: N18, A8 - get_proc_environ on a leader-zombie (state Z, 2 threads, empty leader
        environ) whose capped sibling scan finds nothing raises EIO, which run_targets and
        find_run_processes already read as an uncertain scan."""
        info = {"state": "Z", "ppid": 1, "pgrp": PID, "session": PID, "num_threads": 2, "starttime": 7}
        with fake_gvisor(pid_max=10_000_000, cap=20, leader_environ=b""), \
                mock.patch.object(mproc, "get_proc_stat", return_value=info):
            with self.assertRaises(OSError) as cm:
                mproc.get_proc_environ(PID)
        self.assertEqual(cm.exception.errno, errno.EIO)


if __name__ == "__main__":
    unittest.main()
