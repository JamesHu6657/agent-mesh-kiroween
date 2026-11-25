"""Run termination per plan §11.4.

A termination is a small state machine kept in memory per run and advanced by
the supervisor (every scheduler tick and by meshd's 200 ms termination loop):

    term:   SIGTERM targets ∪ escapees; poll every 200 ms up to kill.grace_s
    kill:   SIGKILL what remains; poll up to 10 s
    failed: run.kill_failed was emitted; the lease is kept; retry (SIGKILL,
            10 s poll) every 30 s

Zombies count as gone. The release condition (§11.4) is "no live process in
the run's group or session and none carrying ``MESH_RUN_ID=<run>``"; only then
does the supervisor close the run and release the lease.

Safety rules:
- A pid is signalled only if its /proc starttime still equals the one seen when
  it was found (a reused pid is never signalled, ``test_proc_identity``).
- meshd itself, pid 1, and every registered meshd child are never signalled,
  except this run's own wrapper with the registered starttime equal to the
  run's ``proc_starttime`` (the child registry, §11.4).
- A pid number is never trusted on its own (review m1c-run3 #1). A process is a
  target only if it carries ``MESH_RUN_ID=<run>``, or it is the same process (pid and
  starttime) as one this termination already found or seen, or it is in the run's
  process group or session **and** that group is provably still the run's: the
  wrapper is alive with its stored starttime, or the process was already seen
  by this termination with the same starttime, or its PPid chain reaches such a
  seen process. Once the wrapper is gone, a fresh process whose only link is the
  (possibly reused) pgid/sid number is left alone.
- An empty or unreadable environ (UNKNOWN, m3-runtargets-race / Carbon RO P1-2)
  is never a reason to target a process; it only stops "no MESH_RUN_ID" from
  being concluded. Empty environ is UNKNOWN for young same-uid non-kthreads;
  ``EACCES``/``EPERM`` on ``/proc/<pid>/environ`` is uncertain for same-uid
  non-kthreads started at/after ``wrapper_starttime`` (or within ``YOUNG_S``
  when ``wrapper_starttime`` is unknown), leaving older host bystanders alone.
  Full review r7 (R2-P2-1): for a run this meshd spawned, it is not uncertain when
  the process's PPid chain never passes through meshd (meshd is a child subreaper,
  so every process of such a run, setsid'd and double-forked ones included, stays
  in meshd's tree): an ``ssh-agent``/``gpg-agent`` the user starts during a run is
  not one of its processes. An adopted run keeps the rule above unchanged.
- Nothing here sleeps on the scheduler path; "nothing left" needs two empty scans
  at least POLL_S apart, taken on separate ticks (``Termination.empty_scan_at``,
  ``ClearCheck``).
"""

import contextvars
import errno
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from mesh.core.proc import ScanResult, get_proc_environ, get_proc_stat, is_tgid_alive
from mesh.daemon import children
from mesh.platform import proc as plat_proc

SIGTERM = plat_proc.SIGTERM
SIGKILL = plat_proc.SIGKILL
_WIN_EPOCH_OFFSET_100NS = 116_444_736_000_000_000
_WIN_TICKS_PER_S = 10_000_000

POLL_S = 0.2
KILL_WAIT_S = 10.0
KILL_RETRY_S = 30.0
# /proc/<pid>/environ can lag after Popen (92/200 misses on a gVisor dev box): an
# empty or unreadable environ is UNKNOWN, not "no MESH_RUN_ID". Nothing here sleeps:
# an UNKNOWN pid is simply read again on the next scan (the next tick).
# Only young processes of the daemon's own uid can be UNKNOWN (review #20 Q2): started
# at/after the run's wrapper, or within YOUNG_S of now. Older or other-user processes
# with an unreadable environ are not candidates (they were never this run's to miss).
YOUNG_S = 5.0
PF_KTHREAD = 0x00200000

@dataclass
class Termination:
    run_id: str
    pgid: Optional[int]
    wrapper_pid: Optional[int]
    end_reason: str
    grace_s: float
    wrapper_starttime: Optional[int] = None
    phase: str = "term"
    started: float = field(default_factory=time.monotonic)
    deadline: float = 0.0
    next_retry: float = 0.0
    kill_failed: bool = False
    kill_failed_count: int = 0
    leftovers_reported: bool = False
    seen: Dict[int, int] = field(default_factory=dict)  # pid -> starttime when first found
    signalled_term: Set[int] = field(default_factory=set)
    signalled_kill: Set[int] = field(default_factory=set)
    initial_pids: List[int] = field(default_factory=list)
    empty_scan_at: Optional[float] = None  # first of two empty scans required for "done"
    scan_uncertain: bool = False  # ClickUp P1-1: last target scan had a /proc read error


@dataclass
class ClearCheck:
    """Per-run "nothing left" confirmation for an exited run that has no termination.

    Owned by the caller (one object per run); ``observe`` must be called exactly once
    per real scan. It returns True only on a second empty scan at least POLL_S after
    the first one; a hit, ``invalidate`` (hold / changed seen state), or a premature
    second scan never closes. No module state: two runs, or two call sites, cannot
    share or renew each other's confirmation (review #20 L3/Q1).
    """
    first_empty_at: Optional[float] = None
    # Earliest time the meshd loop may try this check again after a due attempt that did
    # not consume it (git not ready, a termination owns the run, or the watch raised):
    # the loop backs off to POLL_S instead of re-waking every 5 ms (review #20c).
    retry_at: Optional[float] = None

    def observe(self, found: Dict[int, int], now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        self.retry_at = None
        if found or getattr(found, "uncertain", False):
            self.first_empty_at = None
            return False
        if self.first_empty_at is None:
            self.first_empty_at = now
            return False
        if now - self.first_empty_at < POLL_S:
            return False  # too soon: keep the first empty scan, do not renew it
        self.first_empty_at = None
        return True

    def invalidate(self) -> None:
        self.first_empty_at = None
        self.retry_at = None

    def defer(self, now: Optional[float] = None) -> None:
        """A due attempt that did not consume the check: not due again for POLL_S."""
        now = time.monotonic() if now is None else now
        self.retry_at = now + POLL_S

    def due_at(self) -> Optional[float]:
        """Monotonic time the check falls due (None: nothing pending)."""
        if self.first_empty_at is None:
            return None
        at = self.first_empty_at + POLL_S
        return at if self.retry_at is None else max(at, self.retry_at)

    def due(self, now: Optional[float] = None) -> bool:
        """True when a pending first empty scan can be confirmed by a scan now."""
        now = time.monotonic() if now is None else now
        at = self.due_at()
        return at is not None and now >= at


_active: Dict[str, Termination] = {}
_active_lock = threading.RLock()


def get(run_id: str) -> Optional[Termination]:
    with _active_lock:
        return _active.get(run_id)


def active() -> Dict[str, Termination]:
    with _active_lock:
        return dict(_active)


def active_run_ids() -> Set[str]:
    """Return a thread-safe snapshot of run IDs with an active termination."""
    with _active_lock:
        return set(_active)


def forget(run_id: str) -> None:
    with _active_lock:
        _active.pop(run_id, None)


def kill_failed_runs() -> List[Dict[str, object]]:
    """Runs whose kill failed and are being retried (shown in `mesh status`)."""
    with _active_lock:
        snapshot = [
            (t.run_id, t.end_reason, list(t.seen), t.kill_failed_count)
            for t in _active.values()
            if t.kill_failed
        ]
    return [
        {
            "run_id": run_id,
            "end_reason": end_reason,
            "pids": sorted(pids),
            "attempts": attempts,
        }
        for run_id, end_reason, pids, attempts in snapshot
    ]


def _wrapper_identity_alive(wrapper_pid: Optional[int], wrapper_starttime: Optional[int],
                            errors: Optional[List[bool]] = None) -> bool:
    if not wrapper_pid or wrapper_starttime is None or int(wrapper_starttime) <= 0:
        return False
    try:
        info = get_proc_stat(int(wrapper_pid))
        if info is None or int(info["starttime"]) != int(wrapper_starttime):
            return False
        if is_tgid_alive(int(wrapper_pid), info):
            return True
    except OSError:
        if errors is not None:
            errors.append(True)
        return False
    if sys.platform == "win32":
        return False
    # meshd's own unreaped child (review m1c-run4 §2): its pid cannot be reused until meshd
    # reaps it, so the identity still holds; an adopted wrapper is init's child and fails here.
    return int(info["ppid"]) == os.getpid()


def _session_anchored(pgid: int, seen: Dict[int, int],
                      errors: Optional[List[bool]] = None) -> bool:
    """True while a seen member with its recorded starttime is still in the run's
    session or process group (session == pgid or pgrp == pgid).

    A session id cannot be recycled while any process is still in that session, and
    a process-group id cannot be recycled while any member of the group lives. This
    is what keeps an adopted run's group trusted after its wrapper exited (N18): the
    supervisor records the group's members while the wrapper identity holds
    (``seen.json``) and passes them here.
    """
    had_error = False
    for pid, st in seen.items():
        try:
            info = get_proc_stat(int(pid))
            if info is not None and is_tgid_alive(int(pid), info) and int(info["starttime"]) == int(st) \
                    and (int(info["session"]) == int(pgid) or int(info["pgrp"]) == int(pgid)):
                return True
        except OSError:
            had_error = True
            continue
    if had_error and errors is not None:
        errors.append(True)
    return False


def _descends_from(pid: int, stats: Dict[int, Dict[str, int]], anchors: Dict[int, int],
                   errors: Optional[List[bool]] = None) -> bool:
    """True if pid's PPid chain reaches an anchor pid with the anchor's starttime."""
    cur = stats.get(pid)
    win32 = sys.platform == "win32"
    for _ in range(64):
        if cur is None:
            return False
        ppid = int(cur["ppid"])
        if ppid <= 1 or (win32 and ppid == 4):
            return False
        parent = stats.get(ppid)
        if parent is None:
            try:
                parent = get_proc_stat(ppid)
            except OSError as exc:
                if win32:
                    gone = exc.errno in (errno.ENOENT, errno.ESRCH) or getattr(exc, "winerror", None) == 87
                    if not gone and not isinstance(exc, PermissionError) and _same_uid(ppid) and errors is not None:
                        errors.append(True)
                elif errors is not None:
                    errors.append(True)
                return False
        if parent is None:
            return False
        if win32 and int(parent["starttime"]) > int(cur["starttime"]):
            # Windows does not clear PPID when a parent exits; a newer starttime means PID reuse.
            return False
        if anchors.get(ppid) == int(parent["starttime"]):
            return True
        cur = parent
    return False


_SCAN_ERRORS: contextvars.ContextVar[Optional[List[bool]]] = contextvars.ContextVar(
    "terminate_scan_errors", default=None
)


def _record_scan_error(errors: Optional[List[bool]] = None) -> None:
    sink = errors if errors is not None else _SCAN_ERRORS.get()
    if sink is not None:
        sink.append(True)


def _now_ticks(errors: Optional[List[bool]] = None) -> Optional[int]:
    """Now in starttime units (POSIX clock ticks or Win32 100-ns FILETIME), or None if unknown."""
    if sys.platform == "win32":
        return int(time.time() * _WIN_TICKS_PER_S) + _WIN_EPOCH_OFFSET_100NS
    try:
        with open("/proc/uptime", "r", encoding="ascii") as f:
            up = float(f.read().split()[0])
        return int(up * os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        _record_scan_error(errors)
        return None


def _young_from(wrapper_starttime: Optional[int]) -> Optional[int]:
    """Earliest starttime (ticks) a process may have and still count as UNKNOWN."""
    now = _now_ticks()
    recent = None
    if now is not None:
        ticks_per_s = _WIN_TICKS_PER_S if sys.platform == "win32" else os.sysconf("SC_CLK_TCK")
        recent = now - int(YOUNG_S * ticks_per_s)
    if wrapper_starttime is not None:
        return int(wrapper_starttime) if recent is None else min(int(wrapper_starttime), recent)
    return recent


def _is_kthread(pid: int, info: Dict[str, int]) -> bool:
    if sys.platform == "win32":
        return pid in (0, 4)
    if pid == 2 or int(info["ppid"]) == 2:
        return True
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return bool(int(rest[6]) & PF_KTHREAD)  # field 9: flags
    except (OSError, ValueError, IndexError) as exc:
        if not isinstance(exc, OSError) or exc.errno not in (errno.ENOENT, errno.ESRCH):
            _record_scan_error()
        return False


_WIN_MY_SID_LOCK = threading.Lock()
_WIN_MY_SID: Optional[str] = None


def _win_current_user_sid() -> Optional[str]:
    global _WIN_MY_SID
    with _WIN_MY_SID_LOCK:
        if _WIN_MY_SID is not None:
            return _WIN_MY_SID
        try:
            _WIN_MY_SID = plat_proc.get_process_user_sid(os.getpid())
        except OSError:
            return None
        return _WIN_MY_SID


def _same_uid(pid: int) -> bool:
    if sys.platform == "win32":
        my_sid = _win_current_user_sid()
        if not my_sid:
            return False
        try:
            return plat_proc.get_process_user_sid(pid) == my_sid
        except OSError:
            return False
    try:
        return os.stat(f"/proc/{pid}").st_uid == os.geteuid()
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ESRCH):
            _record_scan_error()
        return False


def _comm(pid: int) -> str:
    """/proc/<pid>/comm (readable even for non-dumpable processes); '' when unreadable."""
    if pid <= 0:
        return ""
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


#: OpenSSH executables: the listener/per-connection ``sshd``, the per-session
#: ``sshd-session`` (OpenSSH 9.8+) and the pre-auth ``sshd-auth`` (OpenSSH 10).
_SSHD_NAMES = ("sshd", "sshd-session", "sshd-auth")


def _is_sshd_comm(name: str) -> bool:
    """Sakana PR #4 P3-1: an OpenSSH process name, exactly. ``comm`` is the executable name
    (``sshd``, ``sshd-session``, ``sshd-auth``); some kernels/ports show the privsep
    proctitle instead (``sshd: user [priv]``, ``sshd: user@notty``), accepted only in that
    ``sshd: `` form. A bare prefix such as ``sshdx`` or ``sshd-evil`` is not sshd."""
    return name in _SSHD_NAMES or name.startswith("sshd: ")


def _socket_activation_root(gpid: int) -> bool:
    """Sakana PR #4 P3-1 (a): systemd socket activation (``sshd.socket``, ``sshd -i`` per
    connection) has no listener process: the root per-connection sshd's parent is PID 1.
    Accept only PID 1 itself, running as root and named ``systemd``; anything else (another
    init, a container's PID 1 we cannot identify, a read error) stays unproven."""
    if gpid != 1:
        return False
    try:
        return _comm(1) == "systemd" and _real_uid(1) == 0
    except (OSError, ValueError):
        return False


def _real_uid(pid: int) -> int:
    """Real uid from /proc/<pid>/status (readable for every process). Raises OSError/ValueError.

    The real uid, not the owner of /proc/<pid>: a setuid-root program started by the run
    (e.g. a symlink named ``sshd`` pointing at su) has euid 0 but keeps the run's real uid."""
    with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("Uid:"):
                return int(line.split()[1])
    raise ValueError(f"no Uid line for pid {pid}")


def _exe_contradicts_sshd(pid: int) -> bool:
    """True when /proc/<pid>/exe is readable and is not an sshd binary.

    Root's exe is unreadable to an unprivileged daemon (EACCES; checked on the build hosts),
    so an unreadable link is no evidence either way; the real-uid and ancestry checks carry
    the proof. A readable link must name sshd/sshd-session and not be deleted."""
    try:
        target = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False
    return target.endswith(" (deleted)") or os.path.basename(target) not in _SSHD_NAMES


def _foreign_spawned_bystander(info: Dict[str, int], eacces_from: Optional[int],
                               pid: Optional[int] = None) -> bool:
    """True for a young non-dumpable same-uid process a *pre-existing other-user daemon*
    created, e.g. ``sshd-session: user@notty`` (parent: root ``sshd-session [priv]``,
    grandparent: the old root ``sshd`` listener).

    Dogfood merge failure: every SSH login during a long merge test made the run's
    termination scan uncertain (EACCES on the session's environ), so the merge could
    not close (INTERNAL_ERROR, task stuck in 'merging') and the unit tests that scan
    /proc failed whenever someone logged in. Such a process cannot carry this run's
    MESH_RUN_ID: an unprivileged run cannot make another user's process its parent,
    and a daemon that was running before the run (the grandparent) builds the child's
    environment itself. A parent of our own uid, pid 1 (reparented escapee), a
    grandparent started at/after the run, or any read error keeps it uncertain.
    """
    if eacces_from is None:
        return False
    try:
        ppid = int(info["ppid"])
        if ppid <= 1:
            return False
        # PR #37 review #1: only sshd's own session processes. Any other foreign parent
        # (e.g. a root subreaper that adopted an escaped run process) stays uncertain.
        if not _is_sshd_comm(_comm(ppid)):
            return False
        if pid is not None and not _is_sshd_comm(_comm(pid)):
            return False
        pinfo = get_proc_stat(ppid)
        if pinfo is None or os.stat(f"/proc/{ppid}").st_uid == os.geteuid():
            return False
        # Release-note follow-up (b): comm alone can be spoofed. The parent must really be
        # root (real uid 0, which no process of ours can get), its exe must not contradict
        # sshd, and its parent must be a root sshd listener that predates the run.
        if _real_uid(ppid) != 0 or _exe_contradicts_sshd(ppid):
            return False
        gpid = int(pinfo["ppid"])
        if gpid < 1:
            return False
        ginfo = get_proc_stat(gpid)
        if ginfo is None or int(ginfo["starttime"]) >= int(eacces_from):
            return False
        if not _socket_activation_root(gpid):
            # Classic: a root sshd listener that predates the run.
            if not _is_sshd_comm(_comm(gpid)) or _real_uid(gpid) != 0 or _exe_contradicts_sshd(gpid):
                return False
        # Re-check the parent is still the same process (pid reuse between reads).
        again = get_proc_stat(ppid)
        return bool(again and int(again["starttime"]) == int(pinfo["starttime"]))
    except (OSError, KeyError, ValueError, TypeError):
        return False


#: prctl(2) PR_GET_CHILD_SUBREAPER.
_PR_GET_CHILD_SUBREAPER = 37
_libc = None


def _is_child_subreaper() -> bool:
    """True when this process is a child subreaper (meshd sets it at start, §11.4).
    Unknown (no libc, prctl error) is False: the stricter rule then applies."""
    if sys.platform == "win32":
        return False
    global _libc
    try:
        import ctypes
        if _libc is None:
            _libc = ctypes.CDLL(None, use_errno=True)
        val = ctypes.c_int(0)
        if _libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(val), 0, 0, 0) != 0:
            return False
        return val.value != 0
    except (OSError, AttributeError, ValueError):
        return False


def _tree_root_for(wrapper_starttime: Optional[int]) -> Optional[Tuple[int, int]]:
    """Full review r7 (R2-P2-1): ``(meshd pid, meshd starttime)`` when every process of the
    run is provably in meshd's process tree, else None.

    That holds when meshd is a child subreaper and the run's wrapper started after meshd
    did (this meshd spawned it; an adopted run's wrapper predates it, and its orphans went
    to the old meshd's ancestors). An orphan reparents to its nearest living subreaper
    ancestor, which for such a run is meshd or one of the run's own processes."""
    if wrapper_starttime is None or int(wrapper_starttime) <= 0 or not _is_child_subreaper():
        return None
    me = os.getpid()
    try:
        info = get_proc_stat(me)
    except OSError:
        return None
    if info is None or int(wrapper_starttime) < int(info["starttime"]):
        return None
    return me, int(info["starttime"])


def _outside_tree(pid: int, info: Dict[str, int], stats: Dict[int, Dict[str, int]],
                  root: Tuple[int, int]) -> bool:
    """True when ``pid``'s PPid chain provably never passes through meshd: it ends at pid
    0/1, or at a process older than meshd (which cannot descend from it). Each parent must
    have started no later than its child (else the ppid was reused mid-walk). A read
    error, a vanished parent or an over-long chain is no proof (False: stays uncertain)."""
    me, me_start = root
    cur = info
    for _ in range(64):
        ppid = int(cur["ppid"])
        if ppid == me:
            return False
        if ppid <= 1:
            return True
        parent = stats.get(ppid)
        if parent is None:
            try:
                parent = get_proc_stat(ppid)
            except OSError:
                return False
        if parent is None or int(parent["starttime"]) > int(cur["starttime"]):
            return False
        if int(parent["starttime"]) < me_start:
            return True
        cur = parent
    return False


def _environ_unknown(env: Optional[Dict[str, str]], pid: int, info: Dict[str, int],
                     young_from: Optional[int]) -> bool:
    """True when an empty/unreadable environ may just not be visible yet (UNKNOWN).

    Readable and non-empty is never UNKNOWN (a definite "no MESH_RUN_ID"). An empty
    cmdline does not make a process known (review #20 Q4: in most real misses the
    cmdline is empty too); kernel threads, other-uid and old processes are excluded.
    """
    if env:
        return False
    if young_from is None or int(info["starttime"]) < young_from:
        return False
    return _same_uid(pid) and not _is_kthread(pid, info)


def _in_run_group(info: Dict[str, int], pgid: Optional[int]) -> bool:
    return bool(pgid) and (int(info["pgrp"]) == int(pgid) or int(info["session"]) == int(pgid))


def _has_mm(status: bytes) -> bool:
    """A task has an mm when /proc status shows a non-zero VmSize (gVisor prints
    ``VmSize: 0 kB`` for tasks without one; mainline omits the line)."""
    for line in status.split(b"\n"):
        if line.startswith(b"VmSize:"):
            try:
                return int(line.split()[1]) > 0
            except (IndexError, ValueError):
                return True  # unparsable: assume live (fail-safe)
    return False


def _exiting_or_gone(pid: int, starttime: int) -> bool:
    """m3-p1c-final: True when an EACCES on ``environ`` came from a process that is already
    exiting (its mm released: no ``VmSize`` in ``/proc/<pid>/status``), a zombie, or gone /
    reused. Such a process cannot carry on as an escapee, so it must not hold a run (seen on
    base: exiting test processes with an empty cmdline). A live non-dumpable process always
    has an mm, so this does not reopen the Carbon RO P1-2 escapee case. Any other read error
    keeps the scan uncertain (returns False)."""
    if sys.platform == "win32":
        try:
            info = get_proc_stat(pid)
            return info is None or int(info["starttime"]) != int(starttime) or not is_tgid_alive(pid, info)
        except OSError as exc:
            return exc.errno in (errno.ENOENT, errno.ESRCH)
    try:
        info = get_proc_stat(pid)
        if info is None or int(info["starttime"]) != int(starttime) or not is_tgid_alive(pid, info):
            return True
        if info["state"] in ("Z", "X"):
            # Carbon RO p1c P1-1: a leader-zombie with a live sibling thread is a live process
            # (is_tgid_alive above); its leader status has no VmSize on mainline Linux.
            return False
        with open(f"/proc/{pid}/status", "rb") as fh:
            status = fh.read()
    except OSError as exc:
        return exc.errno in (errno.ENOENT, errno.ESRCH)
    if _has_mm(status):
        return False
    # The leader may have exited alone (SYS_exit) while sibling threads still run with the
    # group's mm: any live thread with an mm keeps the process "live".
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError as exc:
        return exc.errno in (errno.ENOENT, errno.ESRCH)
    for tid in tids:
        if tid == str(pid):
            continue
        try:
            with open(f"/proc/{pid}/task/{tid}/status", "rb") as fh:
                if _has_mm(fh.read()):
                    return False
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ESRCH):
                return False
    return True


def run_targets(
    run_id: str,
    pgid: Optional[int],
    wrapper_pid: Optional[int],
    wrapper_starttime: Optional[int] = None,
    seen: Optional[Dict[int, int]] = None,
    unknown: Optional[Dict[int, int]] = None,
) -> Dict[int, int]:
    """Live §11.4 targets ∪ escapees of a run as {pid: starttime} (see module rules).

    One pass over /proc, no sleeping. A pid is a target if it carries
    ``MESH_RUN_ID=<run>``, has a confirmed identity (pid and starttime as in ``seen``),
    is in a trusted run group, or descends (PPid chain) from a target or seen process;
    the descent is a closure, so it does not depend on /proc order (review #20 L1/L2).
    An UNKNOWN pid (see ``_environ_unknown``) is only ever taken through one of those
    links, never through its environ. If ``unknown`` is a dict, UNKNOWN pids that were
    taken are also recorded there as {pid: starttime}.
    """
    errors: List[bool] = []
    err_tok = _SCAN_ERRORS.set(errors)
    try:
        return _run_targets_inner(
            run_id, pgid, wrapper_pid, wrapper_starttime, seen, unknown, errors
        )
    finally:
        _SCAN_ERRORS.reset(err_tok)


def _env_get_run_id(env: Dict[str, str]) -> Optional[str]:
    if sys.platform != "win32":
        return env.get("MESH_RUN_ID")
    if "MESH_RUN_ID" in env:
        return env["MESH_RUN_ID"]
    for k, v in env.items():
        if k.upper() == "MESH_RUN_ID":
            return v
    return None


def _win_resolve_run_job(run_id: str, *, terminate: bool = False) -> Tuple[Optional[Any], bool, Optional[str]]:
    """Return ``(job_handle, should_close, home)`` for *run_id* on Windows."""
    from mesh.daemon import supervisor as _sup

    home = _sup.get_run_home(run_id)
    job, should_close = _sup.resolve_run_job(run_id, home=home, terminate=terminate)
    return job, should_close, home


def _win_run_home(run_id: str) -> Optional[str]:
    from mesh.daemon import supervisor as _sup

    return _sup.get_run_home(run_id) or os.environ.get("MESH_HOME")


def _run_targets_win(
    run_id: str,
    pgid: Optional[int],
    wrapper_pid: Optional[int],
    wrapper_starttime: Optional[int],
    seen: Optional[Dict[int, int]],
    unknown: Optional[Dict[int, int]],
    errors: List[bool],
) -> Dict[int, int]:
    """Windows target/escapee scan combining Job Object PIDs, ``seen``, PEB env, and Toolhelp."""
    _ = pgid
    seen = seen or {}
    me = os.getpid()
    registered = children.registered()
    own_wrapper: Optional[int] = None
    if wrapper_pid and wrapper_starttime is not None:
        reg = registered.get(int(wrapper_pid))
        if reg is None or (reg[1] is not None and int(reg[1]) == int(wrapper_starttime)):
            own_wrapper = int(wrapper_pid)
    excluded = {me, 0, 1, 4} | (set(registered) - ({own_wrapper} if own_wrapper else set()))
    wrapper_alive_now = _wrapper_identity_alive(wrapper_pid, wrapper_starttime, errors)
    young_from = _young_from(wrapper_starttime)
    eacces_from = (
        int(wrapper_starttime)
        if wrapper_starttime is not None and int(wrapper_starttime) > 0
        else young_from
    )

    job_pids: Set[int] = set()
    try:
        job, should_close, _home = _win_resolve_run_job(run_id, terminate=False)
    except OSError:
        job, should_close = None, False
        errors.append(True)
    if job is not None:
        try:
            try:
                job_pids = {int(p) for p in plat_proc.query_job_pids(job) if int(p) > 0}
            except OSError:
                errors.append(True)
        finally:
            if should_close:
                try:
                    job.close()
                except Exception:
                    pass

    try:
        parent_map = plat_proc.list_toolhelp_processes()
    except OSError:
        parent_map = {}
        errors.append(True)

    all_pids = (set(parent_map.keys()) | job_pids | {int(p) for p in seen}) - excluded
    stats: Dict[int, Dict[str, int]] = {}
    found = ScanResult()
    candidates: List[int] = []
    same_uid_candidates: List[int] = []
    unknown_pids: Set[int] = set()

    for pid in sorted(all_pids):
        if pid <= 1 or pid == 4:
            continue
        try:
            info = get_proc_stat(pid)
        except PermissionError:
            if pid in seen or pid in job_pids:
                found.uncertain = True
                if pid in seen:
                    found[pid] = int(seen[pid])
                    if unknown is not None:
                        unknown[pid] = int(seen[pid])
            continue
        except OSError as exc:
            gone = exc.errno in (errno.ENOENT, errno.ESRCH) or getattr(exc, "winerror", None) == 87
            if not gone and (pid in seen or pid in job_pids or _same_uid(pid)):
                found.uncertain = True
            if pid in seen and not gone:
                found[pid] = int(seen[pid])
                if unknown is not None:
                    unknown[pid] = int(seen[pid])
            continue
        if info is None:
            continue
        stats[pid] = info
        try:
            alive = is_tgid_alive(pid, info)
        except OSError:
            found.uncertain = True
            if pid in seen:
                found[pid] = int(seen[pid])
                if unknown is not None:
                    unknown[pid] = int(seen[pid])
            continue
        if not alive:
            continue
        st = int(info["starttime"])
        if seen.get(pid) == st or pid in job_pids:
            found[pid] = st
            if unknown is not None:
                try:
                    env_known = get_proc_environ(pid)
                except OSError:
                    env_known = None
                if not env_known:
                    unknown[pid] = st
            continue
        try:
            env = get_proc_environ(pid)
        except PermissionError:
            if (eacces_from is None or st >= eacces_from) and not _is_kthread(pid, info) and _same_uid(pid):
                if not _exiting_or_gone(pid, st):
                    found.uncertain = True
            env = None
        except OSError as exc:
            gone = exc.errno in (errno.ENOENT, errno.ESRCH) or getattr(exc, "winerror", None) == 87
            if (
                not gone
                and not getattr(exc, "_mesh_null_peb", False)
                and (eacces_from is None or st >= eacces_from)
                and not _is_kthread(pid, info)
                and _same_uid(pid)
                and not _exiting_or_gone(pid, st)
            ):
                found.uncertain = True
            env = None
        if env and _env_get_run_id(env) == run_id:
            found[pid] = st
            continue
        is_unknown = _environ_unknown(env, pid, info, young_from)
        if is_unknown:
            unknown_pids.add(pid)
            candidates.append(pid)
        elif env:
            same_uid_candidates.append(pid)

    anchors = dict(seen)
    anchors.update(found)
    if own_wrapper and wrapper_alive_now:
        anchors.setdefault(own_wrapper, int(wrapper_starttime))
    if anchors and same_uid_candidates:
        for pid in same_uid_candidates:
            if _same_uid(pid):
                candidates.append(pid)
    progress = True
    while candidates and progress:
        progress = False
        rest: List[int] = []
        for pid in candidates:
            if _descends_from(pid, stats, anchors, errors):
                st = int(stats[pid]["starttime"])
                found[pid] = st
                anchors[pid] = st
                if pid in unknown_pids and unknown is not None:
                    unknown[pid] = st
                progress = True
            else:
                rest.append(pid)
        candidates = rest
    if errors:
        found.uncertain = True
    return found


def _run_targets_inner(
    run_id: str,
    pgid: Optional[int],
    wrapper_pid: Optional[int],
    wrapper_starttime: Optional[int],
    seen: Optional[Dict[int, int]],
    unknown: Optional[Dict[int, int]],
    errors: List[bool],
) -> Dict[int, int]:
    if sys.platform == "win32":
        return _run_targets_win(
            run_id, pgid, wrapper_pid, wrapper_starttime, seen, unknown, errors
        )
    seen = seen or {}
    me = os.getpid()
    registered = children.registered()
    own_wrapper: Optional[int] = None
    if wrapper_pid and wrapper_starttime is not None:
        reg = registered.get(int(wrapper_pid))
        if reg is None or (reg[1] is not None and int(reg[1]) == int(wrapper_starttime)):
            own_wrapper = int(wrapper_pid)
    excluded = {me, 1} | (set(registered) - ({own_wrapper} if own_wrapper else set()))
    group_trusted = bool(pgid) and (_wrapper_identity_alive(wrapper_pid, wrapper_starttime, errors)
                                    or _session_anchored(int(pgid), seen, errors))
    young_from = _young_from(wrapper_starttime)
    eacces_from = (
        int(wrapper_starttime)
        if wrapper_starttime is not None and int(wrapper_starttime) > 0
        else young_from
    )
    tree_root = _tree_root_for(wrapper_starttime)

    stats: Dict[int, Dict[str, int]] = {}
    found = ScanResult()
    candidates: List[int] = []  # may still qualify through PPid descent
    unknown_pids: Set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        found.uncertain = True
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            info = get_proc_stat(pid)
        except OSError:
            if pid not in excluded:
                found.uncertain = True
            if pid in seen and pid not in excluded:
                found[pid] = int(seen[pid])
                if unknown is not None:
                    unknown[pid] = int(seen[pid])
            continue
        if info is None:
            continue
        stats[pid] = info
        if pid in excluded:
            continue
        try:
            alive = is_tgid_alive(pid, info)
        except OSError:
            found.uncertain = True
            if pid in seen:
                found[pid] = int(seen[pid])
                if unknown is not None:
                    unknown[pid] = int(seen[pid])
            continue
        if not alive:
            continue
        st = int(info["starttime"])
        if seen.get(pid) == st:
            found[pid] = st  # confirmed identity: same process as when it was found (L1)
            if unknown is not None:
                try:
                    env_seen = get_proc_environ(pid)
                except OSError:
                    env_seen = None
                if not env_seen:
                    unknown[pid] = st  # environ read only when the caller asks (review #20b P3)
            continue
        try:
            env = get_proc_environ(pid)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EMFILE, errno.ENFILE, errno.ENOMEM):
                found.uncertain = True
            elif exc.errno in (errno.EACCES, errno.EPERM):
                # Carbon RO P1-2: a same-uid non-dumpable escapee (setsid + prctl
                # PR_SET_DUMPABLE=0, or transient EACCES) started at/after wrapper_starttime
                # (or within YOUNG_S when wrapper_starttime is unknown) may carry MESH_RUN_ID,
                # whereas pre-existing host bystanders (e.g. ssh-agent started before the run)
                # have st < eacces_from and do not wedge scans.
                if _same_uid(pid) and not _is_kthread(pid, info):
                    if ((eacces_from is None or st >= eacces_from) and not _exiting_or_gone(pid, st)
                            and not _foreign_spawned_bystander(info, eacces_from, pid)
                            and not (tree_root is not None and _outside_tree(pid, info, stats, tree_root))):
                        found.uncertain = True
            env = None
        if env and _env_get_run_id(env) == run_id:
            found[pid] = st
            continue
        is_unknown = _environ_unknown(env, pid, info, young_from)
        if is_unknown:
            unknown_pids.add(pid)
        if _in_run_group(info, pgid) and group_trusted:
            found[pid] = st
            if is_unknown and unknown is not None:
                unknown[pid] = st
        elif is_unknown or _in_run_group(info, pgid) or (env and _same_uid(pid)):
            # A raw pgid/sid match after the wrapper is gone is not enough (pid/group
            # reuse); such a process, and any UNKNOWN one, needs descent from an anchor.
            # Linear full review B3: so does a same-uid process with a readable but
            # sanitized environment (no MESH_RUN_ID) that left the run group/session:
            # ancestry proven from an anchor is ownership, whatever its environment says.
            candidates.append(pid)

    anchors = dict(seen)
    anchors.update(found)
    if own_wrapper and group_trusted:
        anchors.setdefault(own_wrapper, int(wrapper_starttime))
    # Descendant closure: a newly taken candidate is an anchor for the others at once.
    progress = True
    while candidates and progress:
        progress = False
        rest: List[int] = []
        for pid in candidates:
            if _descends_from(pid, stats, anchors, errors):
                st = int(stats[pid]["starttime"])
                found[pid] = st
                anchors[pid] = st
                if pid in unknown_pids and unknown is not None:
                    unknown[pid] = st
                progress = True
            else:
                rest.append(pid)
        candidates = rest
    if errors:
        found.uncertain = True
    return found


def run_targets_or_recheck(
    run_id: str,
    pgid: Optional[int],
    wrapper_pid: Optional[int],
    wrapper_starttime: Optional[int] = None,
    seen: Optional[Dict[int, int]] = None,
) -> Dict[int, int]:
    """``run_targets``, and if the first scan is empty, one more after POLL_S.

    For one-shot leftover / env-only checks that must not conclude "nothing left"
    from a single scan and have no next tick (merge test step in its worker thread,
    startup recovery). Never call it on the scheduler / termination loop.
    """
    found = run_targets(run_id, pgid, wrapper_pid, wrapper_starttime, seen)
    if found:
        return found
    time.sleep(POLL_S)
    second = run_targets(run_id, pgid, wrapper_pid, wrapper_starttime, seen)
    if getattr(found, "uncertain", False) and not second and not getattr(second, "uncertain", False):
        return ScanResult(second, uncertain=True)
    return second


def _targets(t: Termination) -> Dict[int, int]:
    with _active_lock:
        seen = dict(t.seen)
    return run_targets(t.run_id, t.pgid, t.wrapper_pid, t.wrapper_starttime, seen)


def signal_if_same(
    pid: int,
    starttime: int,
    sig: int,
    run_id: Optional[str] = None,
    home: Optional[str] = None,
) -> bool:
    """Send ``sig`` to ``pid`` only if it is still the same process (starttime match).

    Never signals meshd, pid 1, or a zombie. Returns True if the signal was sent.
    """
    if sys.platform == "win32":
        return plat_proc.signal_if_same(pid, starttime, sig, run_id=run_id, home=home)
    if pid <= 1 or pid == os.getpid():
        return False
    try:
        info = get_proc_stat(pid)
        if info is None or int(info["starttime"]) != int(starttime) or not is_tgid_alive(pid, info):
            return False
    except OSError:
        return False
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _signal_target(t: Termination, pid: int, starttime: int, sig: int) -> bool:
    """Dispatch ``signal_if_same`` passing Win32 run/home metadata only on Windows."""
    if sys.platform != "win32":
        return signal_if_same(pid, starttime, sig)
    home = _win_run_home(t.run_id)
    try:
        return signal_if_same(pid, starttime, sig, run_id=t.run_id, home=home)
    except TypeError:
        return signal_if_same(pid, starttime, sig)


def _filter_live_seen(seen: Optional[Dict[int, int]]) -> Dict[int, int]:
    kept: Dict[int, int] = {}
    for pid, st in (seen or {}).items():
        ip = int(pid)
        ist = int(st)
        try:
            info = get_proc_stat(ip)
            if info is not None and int(info["starttime"]) == ist and is_tgid_alive(ip, info):
                kept[ip] = ist
        except OSError:
            kept[ip] = ist
    return kept


def _merge_seen_locked(
    existing: Termination,
    additions: Dict[int, int],
    *,
    signalled_term: Optional[Set[int]] = None,
    scan_uncertain: bool = False,
) -> List[Tuple[int, int]]:
    newly_added: List[Tuple[int, int]] = []
    for pid, st in additions.items():
        if existing.seen.get(pid) != st:
            existing.seen[pid] = st
            existing.signalled_kill.discard(pid)
            if signalled_term is not None and pid in signalled_term:
                existing.signalled_term.add(pid)
            else:
                existing.signalled_term.discard(pid)
            newly_added.append((pid, st))
        elif signalled_term is not None and pid in signalled_term:
            existing.signalled_term.add(pid)
    if scan_uncertain:
        existing.scan_uncertain = True
    if newly_added:
        existing.empty_scan_at = None
        if existing.phase == "failed":
            existing.next_retry = 0.0
    return newly_added


def begin(
    run_id: str,
    pgid: Optional[int],
    wrapper_pid: Optional[int],
    end_reason: str,
    grace_s: float,
    wrapper_starttime: Optional[int] = None,
    seen: Optional[Dict[int, int]] = None,
) -> Termination:
    """Start terminating a run (idempotent: an existing termination is returned).

    ``seen`` (pid -> starttime) pre-seeds the termination's seen map: the members an
    adopted run's supervisor recorded while the wrapper identity held (N18). Only
    entries still alive with the same starttime (or whose /proc stat is unreadable,
    so they cannot be proven gone) are kept.
    """
    verified_seen = _filter_live_seen(seen)
    with _active_lock:
        existing = _active.get(run_id)
        if existing is not None:
            newly_added = _merge_seen_locked(existing, verified_seen)
            phase = existing.phase
        else:
            newly_added = []
            phase = None
    if existing is not None:
        if newly_added and phase == "term":
            termed: List[Tuple[int, int]] = []
            for pid, st in newly_added:
                if _signal_target(existing, pid, st, SIGTERM):
                    termed.append((pid, st))
            if termed:
                with _active_lock:
                    for pid, st in termed:
                        if existing.seen.get(pid) == st:
                            existing.signalled_term.add(pid)
        return existing
    t = Termination(run_id=run_id, pgid=pgid, wrapper_pid=wrapper_pid, end_reason=end_reason,
                    grace_s=float(grace_s), wrapper_starttime=wrapper_starttime)
    t.seen.update(verified_seen)
    t.deadline = t.started + t.grace_s
    live = _targets(t)
    t.scan_uncertain = getattr(live, "uncertain", False)
    t.initial_pids = sorted(live)
    for pid, st in live.items():
        t.seen[pid] = st
        if _signal_target(t, pid, st, SIGTERM):
            t.signalled_term.add(pid)
    with _active_lock:
        existing = _active.get(run_id)
        if existing is not None:
            _merge_seen_locked(
                existing,
                t.seen,
                signalled_term=t.signalled_term,
                scan_uncertain=t.scan_uncertain,
            )
            return existing
        _active[run_id] = t
    return t


def step(t: Termination) -> str:
    """Advance a termination. Returns 'done', 'pending' or 'kill_failed' (newly failed)."""
    live = _targets(t)
    uncertain = getattr(live, "uncertain", False)
    t.scan_uncertain = uncertain
    # A pid seen before with a different starttime is a reused pid: it qualified on
    # its own (MESH_RUN_ID, a trusted group, or descent from a seen process).
    with _active_lock:
        for pid, st in live.items():
            if t.seen.get(pid) != st:
                t.seen[pid] = st
                t.signalled_term.discard(pid)
                t.signalled_kill.discard(pid)
    now = time.monotonic()
    if not live and not uncertain:
        # Two consecutive empty scans at least one poll apart; one empty scan is not "done".
        if t.empty_scan_at is None or now - t.empty_scan_at < POLL_S:
            if t.empty_scan_at is None:
                t.empty_scan_at = now
            return "pending"
        t.empty_scan_at = None
        return "done"
    t.empty_scan_at = None
    if t.phase == "term":
        for pid, st in live.items():
            if pid not in t.signalled_term and _signal_target(t, pid, st, SIGTERM):
                t.signalled_term.add(pid)
        if now >= t.deadline:
            _sigkill_all(t, live)
            t.phase = "kill"
            t.deadline = now + KILL_WAIT_S
        return "pending"
    if t.phase == "kill":
        for pid, st in live.items():
            if pid not in t.signalled_kill and _signal_target(t, pid, st, SIGKILL):
                t.signalled_kill.add(pid)
        if now >= t.deadline:
            t.phase = "failed"
            t.next_retry = now + KILL_RETRY_S
            t.kill_failed_count += 1
            newly = not t.kill_failed
            t.kill_failed = True
            return "kill_failed" if newly else "pending"
        return "pending"
    # failed: retry every 30 s
    if now >= t.next_retry:
        t.signalled_kill.clear()
        _sigkill_all(t, live)
        t.phase = "kill"
        t.deadline = now + KILL_WAIT_S
    return "pending"


def _sigkill_all(t: Termination, live: Dict[int, int]) -> None:
    if sys.platform == "win32":
        try:
            job, should_close, _home = _win_resolve_run_job(t.run_id, terminate=True)
        except OSError:
            job, should_close = None, False
        if job is not None:
            try:
                plat_proc.terminate_job(job, 137)
            except OSError:
                pass
            finally:
                if should_close:
                    try:
                        job.close()
                    except Exception:
                        pass
    for pid, st in live.items():
        if _signal_target(t, pid, st, SIGKILL):
            t.signalled_kill.add(pid)
