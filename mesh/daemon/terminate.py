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
- Nothing here sleeps on the scheduler path; "nothing left" needs two empty scans
  at least POLL_S apart, taken on separate ticks (``Termination.empty_scan_at``,
  ``ClearCheck``).
"""

import contextvars
import errno
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from mesh.core.proc import ScanResult, get_proc_environ, get_proc_stat, is_tgid_alive
from mesh.daemon import children

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


def get(run_id: str) -> Optional[Termination]:
    return _active.get(run_id)


def active() -> Dict[str, Termination]:
    return dict(_active)


def forget(run_id: str) -> None:
    _active.pop(run_id, None)


def kill_failed_runs() -> List[Dict[str, object]]:
    """Runs whose kill failed and are being retried (shown in `mesh status`)."""
    out = []
    for t in _active.values():
        if t.kill_failed:
            out.append({
                "run_id": t.run_id,
                "end_reason": t.end_reason,
                "pids": sorted(t.seen),
                "attempts": t.kill_failed_count,
            })
    return out


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
    for _ in range(64):
        if cur is None:
            return False
        ppid = int(cur["ppid"])
        if ppid <= 1:
            return False
        parent = stats.get(ppid)
        if parent is None:
            try:
                parent = get_proc_stat(ppid)
            except OSError:
                if errors is not None:
                    errors.append(True)
                return False
        if parent is None:
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
    """Now in /proc starttime units (clock ticks since boot), or None if unknown."""
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
        recent = now - int(YOUNG_S * os.sysconf("SC_CLK_TCK"))
    if wrapper_starttime is not None:
        return int(wrapper_starttime) if recent is None else min(int(wrapper_starttime), recent)
    return recent


def _is_kthread(pid: int, info: Dict[str, int]) -> bool:
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


def _same_uid(pid: int) -> bool:
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
        if not _comm(ppid).startswith("sshd"):
            return False
        if pid is not None and not _comm(pid).startswith("sshd"):
            return False
        pinfo = get_proc_stat(ppid)
        if pinfo is None or os.stat(f"/proc/{ppid}").st_uid == os.geteuid():
            return False
        gpid = int(pinfo["ppid"])
        if gpid < 1:
            return False
        ginfo = get_proc_stat(gpid)
        if ginfo is None or int(ginfo["starttime"]) >= int(eacces_from):
            return False
        # Re-check the parent is still the same process (pid reuse between reads).
        again = get_proc_stat(ppid)
        return bool(again and int(again["starttime"]) == int(pinfo["starttime"]))
    except (OSError, KeyError, ValueError, TypeError):
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


def _run_targets_inner(
    run_id: str,
    pgid: Optional[int],
    wrapper_pid: Optional[int],
    wrapper_starttime: Optional[int],
    seen: Optional[Dict[int, int]],
    unknown: Optional[Dict[int, int]],
    errors: List[bool],
) -> Dict[int, int]:
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
                            and not _foreign_spawned_bystander(info, eacces_from, pid)):
                        found.uncertain = True
            env = None
        if env and env.get("MESH_RUN_ID") == run_id:
            found[pid] = st
            continue
        is_unknown = _environ_unknown(env, pid, info, young_from)
        if is_unknown:
            unknown_pids.add(pid)
        if _in_run_group(info, pgid) and group_trusted:
            found[pid] = st
            if is_unknown and unknown is not None:
                unknown[pid] = st
        elif is_unknown or _in_run_group(info, pgid):
            # A raw pgid/sid match after the wrapper is gone is not enough (pid/group
            # reuse); such a process, and any UNKNOWN one, needs descent from an anchor.
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
    return run_targets(t.run_id, t.pgid, t.wrapper_pid, t.wrapper_starttime, t.seen)


def signal_if_same(pid: int, starttime: int, sig: int) -> bool:
    """Send ``sig`` to ``pid`` only if it is still the same process (starttime match).

    Never signals meshd, pid 1, or a zombie. Returns True if the signal was sent.
    """
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
    existing = _active.get(run_id)
    if existing is not None:
        return existing
    t = Termination(run_id=run_id, pgid=pgid, wrapper_pid=wrapper_pid, end_reason=end_reason,
                    grace_s=float(grace_s), wrapper_starttime=wrapper_starttime)
    for pid, st in (seen or {}).items():
        try:
            info = get_proc_stat(int(pid))
            if info is not None and int(info["starttime"]) == int(st) and is_tgid_alive(int(pid), info):
                t.seen[int(pid)] = int(st)
        except OSError:
            t.seen[int(pid)] = int(st)
    t.deadline = t.started + t.grace_s
    live = _targets(t)
    t.scan_uncertain = getattr(live, "uncertain", False)
    t.initial_pids = sorted(live)
    for pid, st in live.items():
        t.seen[pid] = st
        if signal_if_same(pid, st, signal.SIGTERM):
            t.signalled_term.add(pid)
    _active[run_id] = t
    return t


def step(t: Termination) -> str:
    """Advance a termination. Returns 'done', 'pending' or 'kill_failed' (newly failed)."""
    live = _targets(t)
    uncertain = getattr(live, "uncertain", False)
    t.scan_uncertain = uncertain
    # A pid seen before with a different starttime is a reused pid: it qualified on
    # its own (MESH_RUN_ID, a trusted group, or descent from a seen process).
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
            if pid not in t.signalled_term and signal_if_same(pid, st, signal.SIGTERM):
                t.signalled_term.add(pid)
        if now >= t.deadline:
            _sigkill_all(t, live)
            t.phase = "kill"
            t.deadline = now + KILL_WAIT_S
        return "pending"
    if t.phase == "kill":
        for pid, st in live.items():
            if pid not in t.signalled_kill and signal_if_same(pid, st, signal.SIGKILL):
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
    for pid, st in live.items():
        if signal_if_same(pid, st, signal.SIGKILL):
            t.signalled_kill.add(pid)
