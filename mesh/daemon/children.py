"""meshd child registry, spawn-window counter and orphan reaper (plan §11.4).

Every child meshd spawns (run wrappers, git, test commands, probes, list_models
and session-list calls, the testing stub child) is recorded here as
``{pid: (kind, starttime)}``. Termination uses the registry to never signal a
registered child that belongs to another purpose; the reaper uses it to never
reap a child whose exit status belongs to its owner.

Reaping rules (§11.4): meshd is a subreaper, so orphans of runs reparent to it
and become zombies when they die. The reaper waits only for zombies with
``PPid == getpid()`` that are *not* registered, one pid at a time with
``WNOHANG``. It never calls ``waitpid(-1)`` or ``os.wait()`` (that would steal
exit statuses from asyncio's child watcher, which then reports 255). While a
spawn is in flight (``spawns_in_flight > 0``) the child may exist before its
pid is registered, so the whole scan is skipped.
"""

import asyncio
import contextlib
import os
import subprocess
import threading
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from mesh.core.proc import get_proc_stat, is_tgid_alive

_lock = threading.Lock()
_registry: Dict[int, Tuple[str, Optional[int]]] = {}
_spawns_in_flight = 0

# Test instrumentation (read by unit tests; never changes behaviour).
stats: Dict[str, int] = {"scans": 0, "scans_skipped_in_flight": 0, "scans_aborted_in_flight": 0, "reaped": 0}


def register(pid: int, kind: str) -> None:
    """Record a meshd child with its /proc starttime."""
    info = get_proc_stat(pid)
    with _lock:
        _registry[pid] = (kind, info["starttime"] if info else None)


def unregister(pid: Optional[int]) -> None:
    if not pid:
        return
    with _lock:
        _registry.pop(int(pid), None)


def unregister_if(pid: Optional[int], kind: str) -> None:
    """Unregister pid only if it is still registered with ``kind``."""
    if not pid:
        return
    with _lock:
        entry = _registry.get(int(pid))
        if entry is not None and entry[0] == kind:
            _registry.pop(int(pid), None)


def registered() -> Dict[int, Tuple[str, Optional[int]]]:
    with _lock:
        return dict(_registry)


def registered_pids() -> Set[int]:
    with _lock:
        return set(_registry)


def spawns_in_flight() -> int:
    with _lock:
        return _spawns_in_flight


@contextlib.contextmanager
def spawn_window() -> Iterator[None]:
    """Count a spawn as in flight until the caller has registered the pid (or failed)."""
    global _spawns_in_flight
    with _lock:
        _spawns_in_flight += 1
    try:
        yield
    finally:
        with _lock:
            _spawns_in_flight -= 1


def popen_registered(kind: str, argv: List[str], **kwargs: Any) -> "subprocess.Popen[Any]":
    """``subprocess.Popen`` with registration inside the spawn window."""
    with spawn_window():
        proc = subprocess.Popen(argv, **kwargs)
        try:
            register(proc.pid, kind)
        except BaseException as exc:
            # Round 7 (Linear P2): the process exists; the caller must not treat this
            # as "nothing was spawned". The Popen handle travels with the error.
            # Round 10: held from the orphan reaper inside the spawn window (no race).
            hold(proc.pid)
            raise SpawnedThenFailed(proc, exc) from exc
    return proc


class SpawnedThenFailed(Exception):
    """Popen succeeded, then a later step of the spawn failed: ``proc`` is the live
    (unreaped) child that the caller must terminate before it closes anything."""

    def __init__(self, proc: "subprocess.Popen[Any]", cause: BaseException) -> None:
        super().__init__(f"after Popen (pid {proc.pid}): {type(cause).__name__}: {cause}")
        self.proc = proc


def run_registered(kind: str, argv: List[str], timeout: Optional[float] = None, **kwargs: Any) -> "subprocess.CompletedProcess[Any]":
    """Blocking ``subprocess.run`` equivalent for meshd-owned children (git, probes)."""
    input_data = kwargs.pop("input", None)
    check = kwargs.pop("check", False)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    proc = popen_registered(kind, argv, **kwargs)
    try:
        try:
            out, err = proc.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    finally:
        unregister(proc.pid)
    result = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    if check:
        result.check_returncode()
    return result


async def create_subprocess_exec_registered(kind: str, *argv: str, **kwargs: Any) -> "asyncio.subprocess.Process":
    """``asyncio.create_subprocess_exec`` with registration inside the spawn window.

    The fork happens before the await returns, so the child may already exist
    (or even have exited) while the pid is unknown; the window covers that gap.
    """
    with spawn_window():
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        register(proc.pid, kind)
    return proc


#: Round 9: pids meshd keeps unreaped on purpose although they are not registered (a
#: pending spawn abort's wrapper, whose pid keeps its pgid reserved). Never reaped here.
_held: set = set()


def hold(pid: int) -> None:
    with _lock:
        _held.add(int(pid))


def unhold(pid: int) -> None:
    with _lock:
        _held.discard(int(pid))


def reap_orphans() -> List[int]:
    """Reap unregistered zombie children of meshd; skip entirely while a spawn is in flight."""
    me = os.getpid()
    with _lock:
        if _spawns_in_flight > 0:
            stats["scans_skipped_in_flight"] += 1
            return []
        stats["scans"] += 1
    reaped: List[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return reaped
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            info = get_proc_stat(pid)
            if info is None or is_tgid_alive(pid, info) or info["ppid"] != me:
                continue
        except OSError:
            continue
        with _lock:
            if _spawns_in_flight > 0:
                # A spawn started during the scan; its child may be this zombie.
                stats["scans_aborted_in_flight"] += 1
                return reaped
            if pid in _registry or pid in _held:
                continue
        try:
            done_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if done_pid == pid:
            reaped.append(pid)
            stats["reaped"] += 1
    return reaped
