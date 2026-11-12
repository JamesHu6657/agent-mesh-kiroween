"""Process inspection and /proc parsing helpers per plan §11.1, §11.4."""

import errno
import os
from typing import Any, Dict, Optional, Set

#: M3 gate re-review P3-2: the gVisor fallback in :func:`_sibling_thread_environ` probes at
#: most this many candidate pids after the leader (gVisor allocates a thread's tid right
#: after its leader's pid, so live siblings sit at small offsets). ``pid_max`` can be
#: 4194304 on 64-bit hosts, and walking all of it would stall meshd's scheduler path. Past
#: the cap the scan stops and raises ``OSError(EIO)``: the environ is unknown, so callers
#: mark their scan uncertain (never "no MESH_RUN_ID") and read it again on a later scan.
SIBLING_SCAN_MAX_PROBES = 32768


class ScanPids(list):
    """Sorted pid list carrying ``uncertain`` when ``/proc`` reads failed (ClickUp recheck P3)."""

    def __init__(self, *args: Any, uncertain: bool = False) -> None:
        super().__init__(*args)
        self.uncertain: bool = bool(uncertain)


class ScanResult(dict):
    """``{pid: starttime}`` scan map carrying ``uncertain`` when ``/proc`` reads failed."""

    def __init__(self, *args: Any, uncertain: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.uncertain: bool = bool(uncertain)


def parse_proc_stat(stat_content: str) -> Dict[str, Any]:
    """Parse /proc/<pid>/stat content per plan §11.1.

    comm (field 2) may contain spaces and parentheses, so split only the text after
    the last ')' on whitespace. That list starts at field 3, so field N is at index N-3:
    - state: index 0 (field 3)
    - ppid: index 1 (field 4)
    - pgrp: index 2 (field 5)
    - session: index 3 (field 6)
    - num_threads: index 17 (field 20)
    - starttime: index 19 (field 22)
    """
    last_rparen = stat_content.rfind(")")
    if last_rparen == -1:
        raise ValueError("Invalid stat format: missing closing parenthesis for comm")

    rest = stat_content[last_rparen + 1 :].strip().split()
    if len(rest) < 20:
        raise ValueError(f"Invalid stat format: expected at least 20 fields after comm, got {len(rest)}")

    return {
        "state": rest[0],
        "ppid": int(rest[1]),
        "pgrp": int(rest[2]),
        "session": int(rest[3]),
        "num_threads": int(rest[17]),
        "starttime": int(rest[19]),
    }


def get_proc_stat(pid: int) -> Optional[Dict[str, Any]]:
    """Read and parse /proc/<pid>/stat.

    Returns ``None`` strictly when the process is gone (``ENOENT`` / ``ESRCH``).
    Non-lookup ``OSError`` (e.g. ``EACCES`` / ``EIO``) propagates so callers treat
    unreadable ``/proc`` state as unknown rather than gone (Linear r10 P1-1).
    """
    path = f"/proc/{pid}/stat"
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (FileNotFoundError, ProcessLookupError):
        return None
    try:
        return parse_proc_stat(content)
    except ValueError as exc:
        raise OSError(errno.EIO, f"Malformed {path}: {exc}", path) from exc


def is_tgid_alive(pid: int, info: Optional[Dict[str, Any]] = None) -> bool:
    """True while any thread in thread-group ``pid`` is alive (not ``Z``/``X``).

    On Linux, if a multithreaded process's main thread calls ``SYS_exit`` without
    ``exit_group``, ``/proc/<pid>/stat`` reports ``state == 'Z'`` for the leader
    while sibling worker threads remain alive (``num_threads > 1`` and/or live
    entries under ``/proc/<pid>/task``). Propagates non-lookup ``OSError``
    (``EACCES`` / ``EIO``) so callers never misread an unreadable check as gone.
    """
    if info is None:
        info = get_proc_stat(pid)
    if info is None:
        return False
    if info["state"] not in ("Z", "X"):
        return True
    if int(info.get("num_threads") or 1) > 1:
        return True
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except (FileNotFoundError, ProcessLookupError):
        return False
    for tid in tids:
        if not tid.isdigit() or int(tid) == int(pid):
            continue
        tpath = f"{task_dir}/{tid}/stat"
        try:
            with open(tpath, "r", encoding="utf-8") as f:
                tinfo = parse_proc_stat(f.read())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except ValueError as exc:
            raise OSError(errno.EIO, f"Malformed {tpath}: {exc}", tpath) from exc
        if tinfo["state"] not in ("Z", "X"):
            return True
    return False


def _parse_environ_bytes(content: bytes) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for entry in content.split(b"\x00"):
        if b"=" in entry:
            k, v = entry.split(b"=", 1)
            env[k.decode("utf-8", errors="replace")] = v.decode("utf-8", errors="replace")
    return env


def _pid_max() -> int:
    try:
        with open("/proc/sys/kernel/pid_max", "r", encoding="ascii") as f:
            val = int(f.read().strip())
            if val > 2:
                return val
    except (OSError, ValueError):
        pass
    return 65536


def _sibling_thread_environ(pid: int) -> Optional[bytes]:
    """Read raw environ bytes from a live sibling thread of leader-zombie ``pid``.

    The gVisor fallback probes at most :data:`SIBLING_SCAN_MAX_PROBES` candidate pids; if
    it stops there without finding a sibling it raises ``OSError(EIO)`` (unknown, not
    "no sibling"), so the caller's scan is uncertain instead of blocking on ``pid_max``.
    """
    saw_empty = False
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except (FileNotFoundError, ProcessLookupError):
        tids = None
    if tids is not None:
        for tid in tids:
            if not tid.isdigit() or int(tid) == int(pid):
                continue
            try:
                with open(f"{task_dir}/{tid}/environ", "rb") as f:
                    data = f.read()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if data:
                return data
            saw_empty = True
        return b"" if saw_empty else None
    # gVisor fallback: /proc/<tgid>/task raises ENOENT once the leader thread exits,
    # while live worker threads remain addressable at /proc/<tid> with Tgid == <pid>.
    pmax = _pid_max()
    needle = f"\nTgid:\t{pid}\n"
    span = pmax - 1  # candidate offsets 1 .. pmax-1 (every pid but the leader's own)
    limit = min(span, max(0, int(SIBLING_SCAN_MAX_PROBES)))
    for off in range(1, limit + 1):
        cand = (pid + off - 1) % pmax + 1
        if cand <= 1 or cand == pid:
            continue
        try:
            with open(f"/proc/{cand}/status", "r", encoding="utf-8", errors="replace") as sf:
                status_txt = sf.read()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if needle not in ("\n" + status_txt):
            continue
        try:
            with open(f"/proc/{cand}/environ", "rb") as ef:
                data = ef.read()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if data:
            return data
        saw_empty = True
    if saw_empty:
        return b""
    if span > limit:
        # M3 gate re-review P3-2: the bounded scan did not cover the pid space, so "no live
        # sibling" is not proven. Unknown, never gone: callers mark their scan uncertain.
        raise OSError(errno.EIO, f"sibling-thread environ scan of pid {pid} stopped after {limit} "
                                 f"probes (pid_max {pmax}); environ unknown")
    return None


def get_proc_environ(pid: int) -> Optional[Dict[str, str]]:
    """Read and parse ``/proc/<pid>/environ`` into a dict of environment variables.

    Returns ``None`` when the process (including all threads) is gone, ``{}`` when
    its environment is genuinely empty, and raises non-lookup ``OSError`` (such as
    ``PermissionError`` or ``EIO``) when the process is alive and unreadable. For a
    multithreaded leader-zombie whose leader ``/proc/<pid>/environ`` is empty or
    unreadable, falls back to a live sibling thread's ``environ``.
    """
    leader_empty = False
    leader_err: Optional[OSError] = None
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            content = f.read()
        if content:
            return _parse_environ_bytes(content)
        leader_empty = True
    except (FileNotFoundError, ProcessLookupError):
        pass
    except OSError as exc:
        leader_err = exc

    try:
        info = get_proc_stat(pid)
    except OSError:
        if leader_err is not None:
            raise leader_err
        raise

    if info is None:
        return {} if leader_empty else None
    if info["state"] not in ("Z", "X"):
        if leader_err is not None:
            raise leader_err
        return {} if leader_empty else None
    if not is_tgid_alive(pid, info):
        return {} if leader_empty else None

    sib = _sibling_thread_environ(pid)
    if sib:
        return _parse_environ_bytes(sib)
    if not is_tgid_alive(pid):
        return None
    if leader_err is not None:
        raise leader_err
    if sib is not None:
        return {}
    return {} if leader_empty else None


def get_proc_ancestry(pid: int, stop_at_pids: Optional[Set[int]] = None) -> list[int]:
    """Walk PPid upward from pid until pid 1 or a stop pid is encountered."""
    if stop_at_pids is None:
        stop_at_pids = set()

    chain: list[int] = []
    curr = pid
    visited: Set[int] = {curr}

    while curr > 1 and curr not in stop_at_pids:
        stat_info = get_proc_stat(curr)
        if not stat_info:
            break
        ppid = stat_info["ppid"]
        if ppid <= 1 or ppid in visited:
            break
        chain.append(ppid)
        visited.add(ppid)
        curr = ppid

    return chain


def find_run_processes(
    run_id: str,
    pgid: Optional[int],
    exclude: Optional[Set[int]] = None,
) -> "ScanPids":
    """Live processes that belong to a run (plan §11.4 targets ∪ escapees).

    A process belongs to the run if its process group or session equals the
    run's pgid (the wrapper is the session leader), or its environ carries
    ``MESH_RUN_ID=<run_id>``. True zombies (all threads dead) count as gone. A ``/proc``
    listing or stat error sets ``uncertain`` on the result instead of reading as "gone"
    (ClickUp recheck P3); test and smoke-harness helper only.
    """
    skip = set(exclude or ())
    found: list[int] = []
    uncertain = False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return ScanPids(uncertain=True)
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in skip:
            continue
        try:
            stat_info = get_proc_stat(pid)
            if stat_info is None or not is_tgid_alive(pid, stat_info):
                continue
        except OSError:
            uncertain = True
            continue
        if pgid and (stat_info["pgrp"] == pgid or stat_info["session"] == pgid):
            found.append(pid)
            continue
        try:
            env = get_proc_environ(pid)
        except OSError as exc:
            env = None
            if exc.errno in (errno.EACCES, errno.EPERM):
                # No wrapper starttime here to scope by (cf. run_targets' young rule), so a
                # same-uid non-dumpable bystander (e.g. an sshd session) must not taint every
                # scan; EACCES stays a skip, as in L14 for processes older than the run.
                pass
            elif exc.errno not in (errno.ENOENT, errno.ESRCH):
                uncertain = True
        if env and env.get("MESH_RUN_ID") == run_id:
            found.append(pid)
    return ScanPids(sorted(found), uncertain=uncertain)
