"""Crash recovery at meshd boot (plan §11.7, steps 1–7; step 5 and merge_test runs since m2-r7).

Step 1 (flock, ``quick_check``, the boot row, older open boots marked
``crashed=1``) is done by the server before :func:`recover` runs. The other
steps run here, in order, each in its own transaction (a run's own steps may
take one more transaction each), before the socket opens and before the
scheduler and termination loops start:

2. Every ``starting``/``running`` run whose lease is held by meshd:

   - ``wrapper.json`` present, pid alive, starttime matches → **adopt**: the
     lease holder becomes the current boot id (deadline unchanged), event
     ``run.adopted`` with the old and new boot id. A passed deadline starts a
     ``timeout`` termination at once. A run adopted from the spawn window (TX B
     never committed) gets its wrapper identity from ``wrapper.json``.
   - ``exit.json`` present → the exit is processed exactly as the supervisor
     does it (§11.3, including leftovers).
   - ``wrapper.json`` present but the process is dead or its starttime differs
     → ``lost`` → T8 ``lost_on_restart``. That pid is never signalled; only
     processes whose environ carries ``MESH_RUN_ID=<run>`` are terminated
     before the lease is released (the release condition, §11.4).
   - no ``wrapper.json`` → scan ``/proc/*/environ`` for ``MESH_RUN_ID=<run>``;
     if a live process carries it, wait up to 5 s for ``wrapper.json`` and
     adopt; otherwise terminate those processes, and only once they are gone
     close the run ``spawn_failed`` → T8 ``lost_before_spawn``. Never respawn.

   Terminations started here are advanced by meshd's 200 ms termination loop
   once the daemon runs; the run is closed in the supervisor's finalize step.
   ``merge_test`` runs (the merge pipeline's test step) are never adopted: a live
   wrapper or ``MESH_RUN_ID`` carrier is terminated (§11.4) and waited for here, the
   run is closed ``killed``/``lost`` (end_reason ``interrupted``) and ``merge:<prj>``
   is released (``merge.recover_merge_run``).
3. Agent-held leases: ``expires_at = max(expires_at, now + lease.ttl_s)``.
4. Outbox ``delivering`` rows whose lead-turn run was not adopted → ``pending``
   (``attempts += 1``).
5. Tasks in ``merging`` (``merge.recover_merging_task``): ``MERGE_HEAD`` →
   ``merge --abort`` → T16 ``interrupted``; HEAD ≠ pre_head and HEAD is the task's
   merge commit (second parent = task branch tip, message contains the task id) →
   reconciled T15 ``merged``; otherwise T16 ``interrupted``. One transaction per task.
6. ``cancelling`` tasks without a live run → ``cancelled``.
7. Past-due approvals expire; event ``daemon.recovered`` with the counts.

Every transition emits its outbox rows through ``record_event``, whose dedup
keys are deterministic (§12.2), and every step re-reads the current state, so
a crash during recovery followed by another recovery neither repeats a
transition nor duplicates a notification (J3).
"""

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from mesh.core.clock import now_iso_utc, parse_utc
from mesh.core.db import transaction
from mesh.core.events import record_event
from mesh.core.leases import get_lease_ttl_s
from mesh.core.proc import ScanResult, get_proc_stat, is_tgid_alive
from mesh.daemon import supervisor, terminate

WRAPPER_WAIT_S = 5.0
WRAPPER_POLL_S = 0.1

RECOVERY_END_STATUS = supervisor.RECOVERY_END_STATUS


def _log(home: str, msg: str) -> None:
    supervisor._log_meshd(home, f"recovery: {msg}")


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    return supervisor._read_json_file(path)


def _identity_alive(wrapper: Optional[Mapping[str, Any]]) -> bool:
    """``wrapper.json``'s pid is alive with the recorded starttime (not a zombie).

    Carbon RO P1-3: non-lookup ``OSError`` from ``get_proc_stat`` / ``is_tgid_alive``
    propagates to ``_step_runs``, which logs ``run_errors`` and leaves the run and lease
    untouched rather than misclassifying a live wrapper as ``wrapper_lost``.
    """
    if not wrapper:
        return False
    try:
        pid = int(wrapper.get("pid") or 0)
        starttime = int(wrapper.get("starttime") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 1 or starttime <= 0:
        return False
    info = get_proc_stat(pid)
    if info is None or int(info.get("starttime") or 0) != starttime:
        return False
    return is_tgid_alive(pid, info)


class _StillWaiting(Exception):
    """Live (deferred) recovery: not settled yet without blocking; check next tick."""


class _Live:
    """PR #40 r3: state of a deferred run's live (supervisor-tick) recovery, kept in
    DEFERRED between ticks so nothing is lost and nothing blocks.

    ``found``: every carrier identity observed on any tick (pid -> starttime). Each
    scan passes it as ``seen``, so descendants of a known carrier are picked up while
    it lives, and a carrier that has exited is not forgotten (a re-parented empty-env
    child seen earlier stays a target). ``empty_at``: when the first empty scan was
    taken; "nothing left" is concluded only from a second empty scan on a later tick,
    at least ``terminate.POLL_S`` after it (instead of sleeping on the event loop)."""

    def __init__(self, wait_until: float) -> None:
        self.wait_until = wait_until
        self.found: Dict[int, int] = {}
        self.empty_at: Optional[float] = None


def _env_carriers(run_id: str, wrapper_starttime: Optional[int] = None,
                  live: Optional[_Live] = None) -> Dict[int, int]:
    """Live processes whose environ carries ``MESH_RUN_ID=<run>`` (no group/session match).
    ``live``: one non-sleeping scan per tick (see ``_Live``); may raise ``_StillWaiting``."""
    if live is None:
        return terminate.run_targets_or_recheck(run_id, None, None, wrapper_starttime)  # environ carriers only
    scan = terminate.run_targets(run_id, None, None, wrapper_starttime, dict(live.found))
    live.found.update(scan)
    alive = _live_members(live.found)
    uncertain = bool(getattr(scan, "uncertain", False))
    if alive or uncertain:
        live.empty_at = None
        return ScanResult(alive, uncertain=uncertain)
    now = time.monotonic()
    if live.empty_at is None or now - live.empty_at < terminate.POLL_S:
        if live.empty_at is None:
            live.empty_at = now
        raise _StillWaiting(run_id)
    return ScanResult({}, uncertain=False)


def _reload_run(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()


#: PR #40 r6: per run, the recovery-found identities whose persisting write committed.
_HANDED_OVER: Dict[str, Set[Tuple[int, int]]] = {}


class _HandOverFailed(Exception):
    """PR #40 r5: recovery-found identities could not be persisted; recovery keeps the
    run (and the identities) and retries."""

    def __init__(self, run_id: str, found: Mapping[int, int]) -> None:
        super().__init__(f"run {run_id}: recovery-found identities not persisted; retrying")
        self.found = dict(found)


def _hand_over(home: str, run: Optional[Mapping[str, Any]], found: Optional[Mapping[int, int]]) -> None:
    """PR #40 r4: identities an earlier recovery scan found (pid -> starttime) join the
    supervisor's recorded members of the run, so the watch terminates them and the
    close-safety gate refuses to close while one is alive (a parent that exited before
    adoption or exit handling must not orphan its empty-env child)."""
    if run is None or not found or not run["pgid"] or not supervisor.is_adopted(run):
        return
    alive = _live_members(found)
    if not alive:
        return
    supervisor.seen_for(run, home)  # load seen.json first: never shadow recorded members
    merged = dict(supervisor._SEEN.get(run["id"]) or {})
    durable = _HANDED_OVER.setdefault(run["id"], set())
    if all((p, st) in durable for p, st in alive.items()):
        return  # r6: skipped only once a write holding them has committed
    merged.update(alive)
    supervisor._SEEN[run["id"]] = merged
    _log(home, f"run {run['id']}: recovery-found identities {sorted(alive)} handed to the supervisor")
    if run["id"] in supervisor._SEEN_UNUSABLE:
        # PR #40 r5: a held run keeps its hold; the identities go into the unusable
        # marker's prior_members (durable across a restart), never a valid seen.json.
        reason = supervisor._SEEN_REASON.get(run["id"]) or supervisor.ADOPT_SCAN_UNTRUSTED
        ok = supervisor.write_unusable_marker(run, reason, home)
    else:
        ok = supervisor._persist_seen(run, merged, home)
    if not ok:
        raise _HandOverFailed(run["id"], alive)
    durable.update(alive.items())


def _adopt(conn: sqlite3.Connection, home: str, run: Mapping[str, Any], old_holder: Optional[str],
           boot_id: str, wrapper: Mapping[str, Any], found: Optional[Mapping[int, int]] = None) -> str:
    pid = int(wrapper["pid"])
    starttime = int(wrapper["starttime"])
    from_spawn_window = run["status"] == "starting" or not run["wrapper_pid"]
    # M3 gate fix round 4: durable adopt_pending marker before anything is committed.
    supervisor.prepare_adopt_seen(run, home)
    with transaction(conn):
        conn.execute(
            "UPDATE leases SET holder = ? WHERE run_id = ? AND holder_kind = 'meshd';",
            (boot_id, run["id"]),
        )
        if from_spawn_window:
            # Adopted from the spawn window: TX B never committed (§11.1).
            conn.execute(
                "UPDATE runs SET status = 'running', wrapper_pid = ?, pgid = ?, proc_starttime = ? "
                "WHERE id = ? AND status IN ('starting', 'running');",
                (pid, pid, starttime, run["id"]),
            )
        record_event(
            conn=conn,
            type_="run.adopted",
            actor_id="system",
            actor_kind="system",
            via="system",
            project_id=run["project_id"],
            task_id=run["task_id"],
            run_id=run["id"],
            payload={
                "run_id": run["id"],
                "old_boot_id": old_holder,
                "new_boot_id": boot_id,
                "wrapper_pid": pid,
                "deadline_at": run["deadline_at"],
                "from_spawn_window": from_spawn_window,
            },
        )
    _log(home, f"adopted run {run['id']} (wrapper {pid}; boot {old_holder} -> {boot_id})")
    fresh = _reload_run(conn, run["id"])
    if fresh is not None:
        # M3 gate P1-1: record the group members now, while the wrapper identity is
        # proven (the scan counts only if the wrapper pid + starttime still hold after
        # it). If the wrapper exits before the first watch tick, that tick still has
        # these members (in memory and in seen.json) for the two-scan termination;
        # without them an env-less member left in the group is no longer trusted.
        if not supervisor.establish_adopted_seen(fresh, home):
            # Fail closed (gate fix round 2): the adoption scan lost its anchor. The
            # run is held (seen state unusable, adopt_scan_untrusted) and the hold and
            # run.seen_unusable event are persisted now, not on a later tick.
            supervisor.report_seen_unusable(conn, fresh, home)
            _hand_over(home, fresh, found)
            _log(home, f"adopted run {run['id']} uncertain: adoption scan not trusted; held")
            return "adopted_uncertain"
        _hand_over(home, fresh, found)
    if run["deadline_at"] and time.time() > parse_utc(run["deadline_at"]).timestamp():
        if fresh is not None:
            supervisor._begin_termination(conn, home, fresh, "timeout",
                                          seen=supervisor.seen_for(fresh, home))
            return "adopted_timeout"
    return "adopted"


def _close_without_process(conn: sqlite3.Connection, run: Mapping[str, Any], end_reason: str) -> None:
    """Close a run none of whose processes exist any more and apply the task transition."""
    status = RECOVERY_END_STATUS[end_reason]
    task = conn.execute("SELECT * FROM tasks WHERE id = ?;", (run["task_id"],)).fetchone()
    with transaction(conn):
        supervisor._close_run(conn, run, status, end_reason, None, {"reason": end_reason})
        if task is None:
            return
        if task["status"] == "cancelling":
            supervisor._task_cancelled(conn, task, run, "cancelled")
        elif task["status"] == "running":
            tail = supervisor.stderr_tail(run["run_dir"]) if os.path.isdir(run["run_dir"] or "") else []
            supervisor._task_failed(conn, task, run, end_reason, {}, tail)
        # submitted: only the run closes (the lead still reviews the submission).


def _terminate_carriers(conn: sqlite3.Connection, home: str, run: Mapping[str, Any], end_reason: str,
                        carriers: Optional[Dict[int, int]] = None,
                        wrapper_starttime: Optional[int] = None) -> None:
    """Terminate the run's MESH_RUN_ID carriers only (never by pid/group of a dead wrapper).

    ``carriers`` (the scan that found them) is handed to begin() (review #20b L1 handoff).
    """
    grace = supervisor._setting_int(conn, "kill.grace_s", supervisor.DEFAULT_KILL_GRACE_S)
    t = terminate.begin(run["id"], None, None, end_reason, grace,
                        wrapper_starttime=wrapper_starttime, seen=carriers)
    _log(home, f"terminating run {run['id']} ({end_reason}); MESH_RUN_ID carriers {t.initial_pids}")


def _live_members(seen: Mapping[int, int]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for pid, st in seen.items():
        try:
            info = get_proc_stat(int(pid))
            if info is not None and int(info.get("starttime") or 0) == int(st) and is_tgid_alive(int(pid), info):
                out[int(pid)] = int(st)
        except OSError:
            out[int(pid)] = int(st)
    return out


def _terminate_group(conn: sqlite3.Connection, home: str, run: Mapping[str, Any], end_reason: str,
                     seen: Dict[int, int]) -> None:
    """Terminate an adopted run's group anchored by its recorded members (N18, r4b)."""
    grace = supervisor._setting_int(conn, "kill.grace_s", supervisor.DEFAULT_KILL_GRACE_S)
    t = terminate.begin(run["id"], int(run["pgid"]), run["wrapper_pid"], end_reason, grace,
                        wrapper_starttime=supervisor.close_identity(run)[1], seen=seen)  # Carbon RO p1c P2-1
    _log(home, f"terminating run {run['id']} ({end_reason}); seen members {sorted(seen)}, "
               f"targets {t.initial_pids}")


def _recover_run(conn: sqlite3.Connection, home: str, row: Mapping[str, Any], boot_id: str,
                 live: Optional[_Live] = None) -> str:
    """``live`` (PR #40 r2/r3): set when the live supervisor retries a deferred run.
    Nothing then blocks: process scans are single passes, the spawn-window wait is one
    check per tick until ``live.wait_until`` (``_StillWaiting``), and an exited run is
    left to the normal watch."""
    live_wait_until = live.wait_until if live is not None else None
    found_so_far = lambda: dict(live.found) if live is not None else None  # noqa: E731
    run_id = row["id"]
    old_holder = row["lease_holder"]
    run = _reload_run(conn, run_id)
    if run is None or run["status"] not in ("starting", "running"):
        return "skipped"
    run_dir = run["run_dir"] or ""
    wrapper = _read_json(os.path.join(run_dir, "wrapper.json"))
    exit_path = os.path.join(run_dir, "exit.json")

    if _identity_alive(wrapper):
        return _adopt(conn, home, run, old_holder, boot_id, wrapper or {}, found=found_so_far())

    if os.path.exists(exit_path):
        with transaction(conn):
            conn.execute(
                "UPDATE leases SET holder = ? WHERE run_id = ? AND holder_kind = 'meshd';", (boot_id, run_id)
            )
            if wrapper and not run["wrapper_pid"]:
                conn.execute(
                    "UPDATE runs SET status = 'running', wrapper_pid = ?, pgid = ?, proc_starttime = ? "
                    "WHERE id = ?;",
                    (int(wrapper["pid"]), int(wrapper["pid"]),
                     supervisor.known_starttime(wrapper.get("starttime")), run_id),
                )
            elif wrapper and run["proc_starttime"] is None and supervisor._is_int(wrapper.get("pid")) \
                    and int(wrapper["pid"]) == int(run["wrapper_pid"]):
                # Carbon RO p1c P2-1: backfill a missing starttime from wrapper.json so the
                # watch's run_targets scopes EACCES escapees by the real wrapper start.
                conn.execute("UPDATE runs SET proc_starttime = ? WHERE id = ? AND proc_starttime IS NULL;",
                             (supervisor.known_starttime(wrapper.get("starttime")), run_id))
        fresh = _reload_run(conn, run_id)
        if fresh is None:
            return "skipped"
        if not fresh["wrapper_pid"]:
            # exit.json without any wrapper identity cannot come from runwrap; treat as lost.
            return _lost(conn, home, fresh, "lost_on_restart", boot_id, found=found_so_far(), live_ctx=live)
        _hand_over(home, fresh, found_so_far())
        supervisor._watch_one(conn, home, fresh)
        after = _reload_run(conn, run_id)
        chk = supervisor._CLEAR.get(run_id)
        if (live_wait_until is None and after is not None and after["status"] in ("starting", "running")
                and chk is not None and chk.first_empty_at is not None):
            # Startup recovery (not the scheduler loop) has no next tick of its own:
            # take the confirming second empty scan one poll later in this call
            # (m3-runtargets-race), so a clean exit is still closed from exit.json.
            time.sleep(max(0.0, chk.first_empty_at + terminate.POLL_S - time.monotonic()))
            supervisor._watch_one(conn, home, after)
            after = _reload_run(conn, run_id)
        if after is not None and after["status"] in ("starting", "running") \
                and (run_id in supervisor._SEEN_UNUSABLE or terminate.get(run_id) is not None):
            return "terminating"  # round 6: refused by the close-safety gate (held / terminating)
        if after is None or after["status"] not in ("starting", "running") or terminate.get(run_id) is not None:
            return "exited"
        # A code task's exit 0 defers until its deliverable.git is collected off the loop;
        # the next watch pass closes the run (review m2-r4 finding 8).
        return "exit_deferred"

    if wrapper:
        return _lost(conn, home, run, "lost_on_restart", boot_id, found=found_so_far(), live_ctx=live)

    # No wrapper.json: crash before or during the spawn (§11.7 step 2, last case).
    if live is not None:
        try:
            carriers = _env_carriers(run_id, live=live)
        except _StillWaiting:
            wrapper = _read_json(os.path.join(run_dir, "wrapper.json"))
            if wrapper and _identity_alive(wrapper):
                return _adopt(conn, home, run, old_holder, boot_id, wrapper, found=dict(live.found))
            raise
        if carriers:
            wrapper = _read_json(os.path.join(run_dir, "wrapper.json"))
            if wrapper and _identity_alive(wrapper):
                return _adopt(conn, home, run, old_holder, boot_id, wrapper, found=dict(live.found))
            if time.monotonic() < live.wait_until:
                raise _StillWaiting(run_id)
        return _lost(conn, home, run, "lost_before_spawn", boot_id, found=dict(live.found), live_ctx=live)
    carriers = _env_carriers(run_id)
    if carriers:
        deadline = time.monotonic() + WRAPPER_WAIT_S
        while time.monotonic() < deadline:
            wrapper = _read_json(os.path.join(run_dir, "wrapper.json"))
            if wrapper and _identity_alive(wrapper):
                return _adopt(conn, home, run, old_holder, boot_id, wrapper, found=carriers)
            time.sleep(WRAPPER_POLL_S)
    # Review #20c (Linear): the carriers found above are handed on; a parent that exits
    # while we wait for wrapper.json must not lose its re-parented empty-env child.
    return _lost(conn, home, run, "lost_before_spawn", boot_id, found=carriers)


def _lost(conn: sqlite3.Connection, home: str, run: Mapping[str, Any], end_reason: str, boot_id: str,
          found: Optional[Dict[int, int]] = None, live_ctx: Optional[_Live] = None) -> str:
    """``found``: identities an earlier scan of this recovery found (pid -> starttime);
    kept only while alive with the same starttime and handed to begin() with this scan's."""
    filled = False
    with transaction(conn):
        conn.execute(
            "UPDATE leases SET holder = ? WHERE run_id = ? AND holder_kind = 'meshd';",
            (boot_id, run["id"]),
        )
        if not run["pgid"]:
            # Round 6 / Round 11: spawn window, TX B never committed: take the identity
            # from wrapper.json so seen markers and group/carrier checks apply.
            pgid, wst = supervisor.close_identity(run)
            if pgid is not None:
                conn.execute("UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? "
                             "WHERE id = ? AND pgid IS NULL;", (pgid, pgid, wst, run["id"]))
                filled = True
        elif run["proc_starttime"] is None:
            # Carbon RO p1c P2-1: pgid committed but starttime missing: backfill from wrapper.json.
            wst = supervisor.close_identity(run)[1]
            if wst is not None:
                conn.execute("UPDATE runs SET proc_starttime = ? WHERE id = ? AND proc_starttime IS NULL;",
                             (wst, run["id"]))
                filled = True
    if filled:
        run = _reload_run(conn, run["id"]) or run
    if run["pgid"] and run["kind"] != "lead_turn":
        # r4b (N18): an adopted run's persisted members keep its group trusted across
        # any number of restarts. While a member recorded in seen.json lives with its
        # starttime, the run stays open, the lease is held and those members (and the
        # rest of the anchored group) are terminated. An unusable seen.json holds the
        # run while the group/session is occupied (supervisor.seen_hold).
        # r4d: one read of the seen state (snapshot) for the members and the hold.
        snap = supervisor.seen_snapshot(run, home)
        supervisor.report_seen_unusable(conn, run, home)
        # r6: earlier scans' identities are made durable (marker / seen.json) first;
        # a failed write raises and the retry keeps them.
        _hand_over(home, run, found)
        live = _live_members(snap.members)
        live.update(_live_members(found or {}))  # PR #40 r4: earlier recovery scans' identities
        for p, st in sorted(supervisor._load_prior_member_pairs(run)):
            if supervisor._identity_live(p, st):
                live[p] = st
        if live or snap.changed or supervisor.snapshot_hold(run, snap):
            _terminate_group(conn, home, run, end_reason, live)
            return "terminating"
        if supervisor.group_still_ours(int(run["pgid"]), run["proc_starttime"]):
            scanned = supervisor._scan_group(int(run["pgid"]))
            if scanned or getattr(scanned, "uncertain", False):
                # Round 5 (Linear P1-b) + ClickUp P1-1: the wrapper died while meshd was down
                # and processes remain in its pgid/session (or the scan was uncertain) that no
                # recorded member covers. Never "no members": hold the run while the group is
                # occupied/uncertain; nothing is signalled by pgid alone; operator release as usual.
                supervisor.hold_wrapper_gone(run, home)
                supervisor.report_seen_unusable(conn, run, home)
                _terminate_group(conn, home, run, end_reason, {})
                return "terminating"
    carriers = _live_members(found or {})
    for p, st in sorted(supervisor._load_prior_member_pairs(run)):
        if supervisor._identity_live(p, st):
            carriers[p] = st
    wst = supervisor.close_identity(run)[1]
    env_scan = _env_carriers(run["id"], wst, live=live_ctx)
    carriers.update(env_scan)
    if carriers or getattr(found, "uncertain", False) or getattr(env_scan, "uncertain", False):
        _terminate_carriers(conn, home, run, end_reason, carriers, wrapper_starttime=wst)
        return "terminating"
    _close_without_process(conn, run, end_reason)
    return RECOVERY_END_STATUS[end_reason]


def _step_runs(conn: sqlite3.Connection, home: str, boot_id: str, counts: Dict[str, int]) -> Set[str]:
    rows = conn.execute(
        "SELECT r.id, l.holder AS lease_holder FROM runs r JOIN leases l ON l.run_id = r.id "
        "WHERE r.status IN ('starting', 'running') AND l.holder_kind = 'meshd' "
        # merge_test runs are never adopted; _step_merge_runs handles them (m2-r7).
        "AND r.kind <> 'merge_test' "
        "ORDER BY r.started_at ASC, r.id ASC;"
    ).fetchall()
    kept_open: Set[str] = set()
    for row in rows:
        try:
            try:
                outcome = _recover_run(conn, home, row, boot_id)
            except supervisor.CloseHeld as held:
                # Round 6: the close-safety gate refused a close (any recovery path).
                supervisor.hold_close(conn, home, _reload_run(conn, row["id"]) or row, held)
                outcome = "terminating"
        except Exception as exc:  # one run must not stop recovery of the others
            _log(home, f"run {row['id']} failed: {type(exc).__name__}: {exc}")
            counts["errors"] = counts.get("errors", 0) + 1
            still = _reload_run(conn, row["id"])
            if still is not None and still["status"] in ("starting", "running"):
                kept_open.add(row["id"])
                # Blind review P2: retried by the supervisor; the normal watch skips a
                # run whose wrapper pid was never recorded.
                if isinstance(exc, _HandOverFailed):
                    keep = _Live(time.monotonic())
                    keep.found.update(exc.found)  # r5: the identities stay with the retry
                    DEFERRED[row["id"]] = (boot_id, 0.0, keep, None)
                else:
                    DEFERRED[row["id"]] = (boot_id, 0.0)
            continue
        if outcome.startswith("adopted") or outcome in ("terminating", "exit_deferred"):
            kept_open.add(row["id"])
        counts[outcome] = counts.get(outcome, 0) + 1
    return kept_open


#: Runs whose recovery raised (a transient error): run id -> (boot id, next try at,
#: monotonic). ``retry_deferred`` runs from the supervisor pass until each settles.
DEFERRED: Dict[str, Tuple[Any, ...]] = {}
DEFERRED_RETRY_S = 5.0
DEFERRED_POLL_S = 0.5


def retry_deferred(conn: sqlite3.Connection, home: str) -> List[str]:
    """Retry recovery of the runs it could not settle (blind review P2). Returns the ids
    that are no longer open. Never blocks (PR #40 r3): see ``_Live``.

    Entry: ``(boot_id, due, live, state)``; state "await_close" means recovery handed the
    run on (termination / watch) and only its closing is awaited. An entry is removed
    only after a closed run's delivering messages were released (PR #40 r3), so a
    failed release is retried on the next tick."""
    import time

    settled: List[str] = []
    now = time.monotonic()
    for run_id, entry in list(DEFERRED.items()):
        boot_id, due = entry[0], entry[1]
        live = entry[2] if len(entry) > 2 else None
        state = entry[3] if len(entry) > 3 else None
        if now < due:
            continue
        if live is None:
            live = _Live(now + WRAPPER_WAIT_S)
        try:
            plain = _reload_run(conn, run_id)
            if plain is None or plain["status"] not in ("starting", "running"):
                _release_outbox(conn, run_id)
                DEFERRED.pop(run_id, None)
                settled.append(run_id)
                continue
        except Exception as exc:  # noqa: BLE001 - keep the entry; try again later
            _log(home, f"deferred release of run {run_id} failed: {type(exc).__name__}: {exc}")
            DEFERRED[run_id] = (boot_id, now + DEFERRED_RETRY_S, live, state)
            continue
        if state == "await_close":
            DEFERRED[run_id] = (boot_id, now + DEFERRED_POLL_S, live, state)
            continue
        # PR #40 r1: the same row shape as _step_runs (with the lease holder).
        row = conn.execute(
            "SELECT r.*, l.holder AS lease_holder FROM runs r JOIN leases l ON l.run_id = r.id "
            "WHERE r.id = ? AND l.holder_kind = 'meshd';", (run_id,)
        ).fetchone()
        if row is None:
            DEFERRED.pop(run_id, None)  # no meshd lease any more: not recovery's run
            continue
        try:
            try:
                outcome = _recover_run(conn, home, row, boot_id, live=live)
            except supervisor.CloseHeld as held:
                supervisor.hold_close(conn, home, _reload_run(conn, run_id) or row, held)
                outcome = "terminating"
        except _StillWaiting:
            DEFERRED[run_id] = (boot_id, now + WRAPPER_POLL_S, live, None)
            continue
        except Exception as exc:  # noqa: BLE001 - still transient: try again later
            _log(home, f"deferred recovery of run {run_id} failed: {type(exc).__name__}: {exc}")
            if isinstance(exc, _HandOverFailed) and live is not None:
                live.found.update(exc.found)
            DEFERRED[run_id] = (boot_id, now + DEFERRED_RETRY_S, live, None)
            continue
        _log(home, f"deferred recovery of run {run_id}: {outcome}")
        if outcome.startswith("adopted"):
            DEFERRED.pop(run_id, None)  # live again: its messages stay with it
            continue
        # Closed now or later: the next tick (or this check) releases its messages
        # before the entry goes.
        DEFERRED[run_id] = (boot_id, 0.0, live, "await_close")
        try:
            still = _reload_run(conn, run_id)
            if still is None or still["status"] not in ("starting", "running"):
                _release_outbox(conn, run_id)
                DEFERRED.pop(run_id, None)
                settled.append(run_id)
            else:
                DEFERRED[run_id] = (boot_id, now + DEFERRED_POLL_S, live, "await_close")
        except Exception as exc:  # noqa: BLE001 - the entry stays; retried next tick
            _log(home, f"deferred release of run {run_id} failed: {type(exc).__name__}: {exc}")
    return settled


def _release_outbox(conn: sqlite3.Connection, run_id: str) -> None:
    """PR #40 r2: boot kept a deferred run's delivering messages (the run was still
    open); once it closes they are requeued, as boot reconciliation (_step_outbox) does."""
    with transaction(conn):
        conn.execute(
            "UPDATE outbox SET status = 'pending', attempts = attempts + 1, delivering_run_id = NULL "
            "WHERE delivering_run_id = ? AND status = 'delivering';",
            (run_id,),
        )


def _step_merge_runs(conn: sqlite3.Connection, home: str, counts: Dict[str, int]) -> None:
    from mesh.daemon import merge

    rows = conn.execute(
        "SELECT * FROM runs WHERE kind = 'merge_test' AND status IN ('starting', 'running') "
        "ORDER BY started_at ASC, id ASC;"
    ).fetchall()
    if not rows:
        return
    try:
        results = merge.recover_merge_runs(conn, list(rows))
    except Exception as exc:  # a bug here must not stop boot; step 5 skips open runs
        _log(home, f"merge runs failed: {type(exc).__name__}: {exc}")
        counts["errors"] = counts.get("errors", 0) + len(rows)
        return
    for run in rows:
        status = results.get(run["id"])
        if isinstance(status, merge.MergeRunHeld):
            _log(home, f"merge run {run['id']} held: {status}")
            counts["merge_run_held"] = counts.get("merge_run_held", 0) + 1
            continue
        if status is None or isinstance(status, BaseException):
            _log(home, f"merge run {run['id']} failed: {type(status).__name__}: {status}")
            counts["errors"] = counts.get("errors", 0) + 1
            continue
        _log(home, f"merge run {run['id']} closed {status} (interrupted)")
        counts[f"merge_run_{status}"] = counts.get(f"merge_run_{status}", 0) + 1


def _step_merging(conn: sqlite3.Connection, home: str, counts: Dict[str, int]) -> None:
    from mesh.daemon import merge

    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'merging' ORDER BY updated_at ASC, id ASC;"
    ).fetchall()
    for task in rows:
        try:
            outcome = merge.recover_merging_task(conn, home, task)
        except Exception as exc:
            _log(home, f"merging task {task['id']} failed: {type(exc).__name__}: {exc}")
            counts["merge_errors"] = counts.get("merge_errors", 0) + 1
            continue
        if outcome == "run_open":
            # Its merge_test run is still open (step 2 failed): left `merging`, retried next boot.
            _log(home, f"merging task {task['id']} left merging: its merge_test run is still open")
            counts["merge_errors"] = counts.get("merge_errors", 0) + 1
            continue
        _log(home, f"merging task {task['id']}: {outcome}")
        counts[f"merge_{outcome}"] = counts.get(f"merge_{outcome}", 0) + 1


def _step_agent_leases(conn: sqlite3.Connection) -> int:
    now_ts = time.time()
    floor = now_iso_utc(now_ts + get_lease_ttl_s(conn))
    extended = 0
    with transaction(conn):
        for lease in conn.execute(
            "SELECT key, expires_at FROM leases WHERE holder_kind = 'agent';"
        ).fetchall():
            current = lease["expires_at"]
            if current is None or parse_utc(current).timestamp() < parse_utc(floor).timestamp():
                conn.execute("UPDATE leases SET expires_at = ? WHERE key = ?;", (floor, lease["key"]))
                extended += 1
    return extended


def _step_outbox(conn: sqlite3.Connection, adopted: Set[str]) -> int:
    requeued = 0
    with transaction(conn):
        for row in conn.execute(
            "SELECT id, delivering_run_id FROM outbox WHERE status = 'delivering';"
        ).fetchall():
            if row["delivering_run_id"] and row["delivering_run_id"] in adopted:
                continue
            conn.execute(
                "UPDATE outbox SET status = 'pending', attempts = attempts + 1, delivering_run_id = NULL "
                "WHERE id = ? AND status = 'delivering';",
                (row["id"],),
            )
            requeued += 1
    return requeued


def _step_cancelling(conn: sqlite3.Connection) -> int:
    from mesh.daemon.handlers.task import emit_dependents_blocked

    done = 0
    rows = conn.execute(
        "SELECT t.* FROM tasks t WHERE t.status = 'cancelling' AND NOT EXISTS "
        "(SELECT 1 FROM runs r WHERE r.task_id = t.id AND r.status IN ('starting', 'running'));"
    ).fetchall()
    for task in rows:
        now = now_iso_utc()
        with transaction(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'cancelled', updated_at = ?, version = version + 1 "
                "WHERE id = ? AND status = 'cancelling';",
                (now, task["id"]),
            )
            if cur.rowcount != 1:
                continue
            record_event(
                conn=conn,
                type_="task.cancelled",
                actor_id="system",
                actor_kind="system",
                via="system",
                project_id=task["project_id"],
                task_id=task["id"],
                from_state="cancelling",
                to_state="cancelled",
                reason="cancelled",
                payload={"task_id": task["id"], "project_id": task["project_id"], "run_id": None,
                         "end_reason": "cancelled", "recovered": True},
            )
            emit_dependents_blocked(conn, task["id"], None)
        done += 1
    return done


def recover(conn: sqlite3.Connection, home: str, boot_id: str) -> Dict[str, Any]:
    """Run §11.7 steps 2–7. Returns the counts recorded in ``daemon.recovered``."""
    from mesh.daemon.scheduler import sweep_expired_approvals

    counts: Dict[str, int] = {}
    # r4d: persisted seen holds (a held run whose seen.json was deleted stays held).
    supervisor.refresh_persisted_holds(conn, home)
    adopted = _step_runs(conn, home, boot_id, counts)
    _step_merge_runs(conn, home, counts)
    agent_leases = _step_agent_leases(conn)
    outbox = _step_outbox(conn, adopted)
    _step_merging(conn, home, counts)
    cancelled = _step_cancelling(conn)
    approvals = sweep_expired_approvals(conn, home)
    summary: Dict[str, Any] = {
        "boot_id": boot_id,
        "runs_adopted": counts.get("adopted", 0) + counts.get("adopted_timeout", 0)
        + counts.get("adopted_uncertain", 0),
        "runs_adopted_uncertain": counts.get("adopted_uncertain", 0),
        "runs_adopted_past_deadline": counts.get("adopted_timeout", 0),
        "runs_exited": counts.get("exited", 0),
        "runs_exit_deferred": counts.get("exit_deferred", 0),
        "runs_lost": counts.get("lost", 0),
        "runs_spawn_failed": counts.get("spawn_failed", 0),
        "runs_terminating": counts.get("terminating", 0),
        "run_errors": counts.get("errors", 0),
        "merge_runs_killed": counts.get("merge_run_killed", 0),
        "merge_runs_lost": counts.get("merge_run_lost", 0),
        "merge_runs_held": counts.get("merge_run_held", 0),
        "merges_interrupted": counts.get("merge_interrupted", 0),
        "merges_reconciled": counts.get("merge_reconciled", 0),
        "merge_errors": counts.get("merge_errors", 0),
        "agent_leases_extended": agent_leases,
        "outbox_requeued": outbox,
        "tasks_cancelled": cancelled,
        "approvals_expired": len(approvals),
    }
    with transaction(conn):
        record_event(
            conn=conn,
            type_="daemon.recovered",
            actor_id="system",
            actor_kind="system",
            via="system",
            payload=summary,
        )
    _log(home, json.dumps(summary, sort_keys=True))
    return summary
