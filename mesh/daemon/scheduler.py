"""Scheduler tick loop, task promotion, and queue ordering per plan §6.2, §9.3, §16 M1.6."""

import datetime
import json
import os
import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Tuple

from mesh.adapters.registry import get_adapter
from mesh.core.clock import now_iso_utc, parse_utc
from mesh.core.db import transaction
from mesh.core.redact import redact
from mesh.core.events import record_event

#: M3 gate re-review P2 (L11): T6 failure reason for a queued launch task whose adapter is
#: held (``evidence.LAUNCH_HOLDS``), e.g. a kiro task queued before the hold landed.
LAUNCH_HELD_REASON = "launch_held"


def get_inbox_ack_timeout_s(conn: sqlite3.Connection) -> int:
    """Retrieve inbox.ack_timeout_s setting from database (default 600s)."""
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'inbox.ack_timeout_s';")
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            return int(json.loads(row["value_json"]))
        except Exception:
            return 600
    return 600


def _log_to_meshd(home: str, msg: str) -> None:
    """Log an error message to meshd.log if home is provided."""
    if not home:
        return
    logs_dir = os.path.join(home, "logs")
    os.makedirs(logs_dir, mode=0o700, exist_ok=True)
    log_path = os.path.join(logs_dir, "meshd.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{now_iso_utc()} ERROR {redact(msg)}\n")
        os.chmod(log_path, 0o600)
    except Exception:
        pass


def compute_queue_info(conn: sqlite3.Connection, task_id: str) -> Tuple[Optional[int], Optional[str]]:
    """Compute queue_position (1-based per agent) and blocked_reason for a queued task per plan §9.3.

    Returns:
        (queue_position, blocked_reason) or (None, None) if task is not queued.
    """
    cur = conn.execute(
        """SELECT t.id, t.status, t.priority, t.queued_at, t.project_id, t.assignee_agent_id,
                  p.status as project_status, a.delivery, a.max_concurrency
           FROM tasks t
           JOIN projects p ON t.project_id = p.id
           LEFT JOIN agents a ON t.assignee_agent_id = a.id
           WHERE t.id = ?;""",
        (task_id,),
    )
    row = cur.fetchone()
    if not row or row["status"] != "queued":
        return None, None

    assignee_id = row["assignee_agent_id"]
    priority = row["priority"]
    queued_at = row["queued_at"] or ""
    project_status = row["project_status"]
    delivery = row["delivery"]

    # 1. Blocked reason: project_paused > waiting_for_start (inbox) > concurrency_limit
    if project_status == "paused":
        blocked_reason = "project_paused"
    elif delivery == "inbox":
        max_concurrency = row["max_concurrency"] or 1
        cur_slots = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE agent_id = ? AND status IN ('starting', 'running') AND kind IN ('task', 'external');",
            (assignee_id,),
        )
        active_slots = cur_slots.fetchone()[0]

        global_cap_reached = False
        cur_set = conn.execute("SELECT value_json FROM settings WHERE key = 'scheduler.max_total_runs';")
        set_row = cur_set.fetchone()
        if set_row and set_row["value_json"]:
            try:
                g_limit = int(json.loads(set_row["value_json"]))
                cur_tot = conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('starting', 'running') AND kind IN ('task', 'external');")
                tot_active = cur_tot.fetchone()[0]
                if tot_active >= g_limit:
                    global_cap_reached = True
            except Exception:
                pass

        if active_slots >= max_concurrency or global_cap_reached:
            blocked_reason = "concurrency_limit"
        else:
            blocked_reason = "waiting_for_start"
    else:
        blocked_reason = "concurrency_limit"

    # 2. Queue position: 1-based index per agent ordered by priority DESC, queued_at ASC, id ASC
    cur_pos = conn.execute(
        """SELECT COUNT(*) as rank
           FROM tasks
           WHERE assignee_agent_id = ?
             AND status = 'queued'
             AND (
                 priority > ?
                 OR (priority = ? AND queued_at < ?)
                 OR (priority = ? AND queued_at = ? AND id < ?)
             );""",
        (assignee_id, priority, priority, queued_at, priority, queued_at, task_id),
    )
    rank_row = cur_pos.fetchone()
    queue_position = (rank_row["rank"] if rank_row else 0) + 1

    return queue_position, blocked_reason


def promote_tasks(conn: sqlite3.Connection, home: str = "", testing_mode: bool = False) -> List[str]:
    """Check assigned/rework tasks for dependency satisfaction and promote to queued per plan §6.2.

    Promotes tasks in active projects whose:
    1. Direct task dependencies are all in status 'accepted' or 'merged'
    2. Direct project dependencies are all in status 'completed'

    Returns:
        List of task IDs that were promoted in this pass.
    """
    cur = conn.execute(
        """SELECT t.id, t.project_id, t.title, t.spec, t.status, t.assignee_agent_id,
                  t.fence_seq, a.delivery, pr.name as assignee_name
           FROM tasks t
           JOIN projects p ON t.project_id = p.id
           LEFT JOIN agents a ON t.assignee_agent_id = a.id
           LEFT JOIN principals pr ON t.assignee_agent_id = pr.id
           WHERE t.status IN ('assigned', 'rework')
             AND t.assignee_agent_id IS NOT NULL
             AND p.status = 'active';"""
    )
    candidates = cur.fetchall()
    promoted: List[str] = []

    for task in candidates:
        task_id = task["id"]
        project_id = task["project_id"]

        try:
            # Check task dependencies: all must be 'accepted' or 'merged'
            cur_deps = conn.execute(
                """SELECT td.depends_on_task_id, dep.status
                   FROM task_deps td
                   JOIN tasks dep ON td.depends_on_task_id = dep.id
                   WHERE td.task_id = ?;""",
                (task_id,),
            )
            task_deps = cur_deps.fetchall()
            if any(d["status"] not in ("accepted", "merged") for d in task_deps):
                continue

            # Check project dependencies: all must be 'completed'
            cur_p_deps = conn.execute(
                """SELECT pd.depends_on_project_id, dep.status
                   FROM project_deps pd
                   JOIN projects dep ON pd.depends_on_project_id = dep.id
                   WHERE pd.project_id = ?;""",
                (project_id,),
            )
            project_deps = cur_p_deps.fetchall()
            if any(pd["status"] != "completed" for pd in project_deps):
                continue

            # All guards satisfied: promote to 'queued' (T4 or T13) inside an explicit transaction
            with transaction(conn):
                now = now_iso_utc()
                is_inbox = (task["delivery"] == "inbox")
                offered_at = now if is_inbox else None

                cur_upd = conn.execute(
                    """UPDATE tasks
                       SET status = 'queued',
                           queued_at = ?,
                           offered_at = ?,
                           updated_at = ?,
                           version = version + 1
                       WHERE id = ? AND status IN ('assigned', 'rework');""",
                    (now, offered_at, now, task_id),
                )
                if cur_upd.rowcount == 0:
                    continue

                old_status = task["status"]
                record_event(
                    conn=conn,
                    type_="task.queued",
                    actor_id="system",
                    actor_kind="system",
                    via="system",
                    project_id=project_id,
                    task_id=task_id,
                    from_state=old_status,
                    to_state="queued",
                    payload={
                        "task_id": task_id,
                        "project_id": project_id,
                        "title": task["title"],
                        "assignee": task["assignee_name"],
                    },
                )

                if is_inbox:
                    # Emit task.offered event for inbox agent per plan §12.2.
                    # fan_out_event automatically inserts outbox row with dedup key offered:<task_id>:<fence_seq+1>
                    record_event(
                        conn=conn,
                        type_="task.offered",
                        actor_id="system",
                        actor_kind="system",
                        via="system",
                        project_id=project_id,
                        task_id=task_id,
                        payload={
                            "task_id": task_id,
                            "project_id": project_id,
                            "title": task["title"],
                            "spec": task["spec"],
                            "fence": task["fence_seq"] + 1,
                        },
                    )

                promoted.append(task_id)
        except Exception as e:
            _log_to_meshd(home, f"Scheduler promotion failed on task {task_id}: {e}")
            continue

    return promoted


def fail_queued_task_t6(
    conn: sqlite3.Connection,
    task: Mapping[str, Any],
    reason: str,
    details: Dict[str, Any],
    assignee_id: Optional[str] = None,
    actor_id: str = "system",
    actor_kind: str = "system",
    via: str = "system",
    request_id: Optional[str] = None,
) -> bool:
    """T6 queued → failed (plan §6.2); the caller owns the transaction.

    Used by the T6 checks and by T5 for `workspace_error` (m2-r4). Returns False when
    the task was no longer queued (nothing written). The `task.failed` event is what
    notifies the lead (outbox fan-out in record_event).
    """
    task_id = task["id"]
    project_id = task["project_id"]
    now = now_iso_utc()
    failure_dict = {"reason": reason, "details": details}
    cur_upd = conn.execute(
        """UPDATE tasks
           SET status = 'failed',
               failure_json = ?,
               updated_at = ?,
               version = version + 1
           WHERE id = ? AND status = 'queued';""",
        (json.dumps(failure_dict), now, task_id),
    )
    if cur_upd.rowcount == 0:
        return False

    if assignee_id:
        conn.execute(
            """UPDATE outbox
               SET status = 'superseded'
               WHERE recipient_id = ?
                 AND type = 'task.offered'
                 AND json_extract(payload_json, '$.task_id') = ?
                 AND status IN ('pending', 'delivered');""",
            (assignee_id, task_id),
        )

    record_event(
        conn=conn,
        type_="task.failed",
        actor_id=actor_id,
        actor_kind=actor_kind,
        via=via,
        project_id=project_id,
        task_id=task_id,
        from_state="queued",
        to_state="failed",
        reason=reason,
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "reason": reason,
            "details": details,
            "fence": task["fence_seq"],
            "attempt": task["attempt"],
            "max_attempts": task["max_attempts"],
        },
        request_id=request_id,
    )
    return True


def _launch_hold_details(adapter_id: Optional[str], testing_mode: bool) -> Optional[Dict[str, Any]]:
    """M3 gate re-review P2 (L11): the T6 failure details for a launch task whose adapter is
    held (``evidence.LAUNCH_HOLDS``, not lifted by the testing-only ``MESH_TEST_LAUNCH_UNHOLD``),
    else None. AG step 4 refuses new assignments to a held adapter; this re-check also stops
    tasks that were already assigned or queued before the hold (T5 does not re-check evidence,
    L3). Reads only the hold table and meshd's env: no probe, no file, no subprocess."""
    from mesh.adapters import evidence

    milestone = evidence.held_milestone(adapter_id or "", testing_mode)
    if milestone is None:
        return None
    return {"adapter": adapter_id, "reason": LAUNCH_HELD_REASON, "milestone": milestone}


def fail_held_launch_tasks(conn: sqlite3.Connection, home: str = "", testing_mode: bool = False) -> List[str]:
    """M3 gate re-review P2 (L11): T6 ``launch_held`` for every queued launch task whose adapter
    is held, run right before T5 starts launch runs (:func:`start_queued_launch_tasks`).

    Each task fails in its own transaction (status, event and outbox fan-out together). A
    failure on one task is logged and the pass goes on with the others (m4-p3-cleanup), so
    one bad row cannot block every launch spawn on every tick. The task that errored stays
    queued and still cannot start: ``supervisor.spawn_launch_runs`` skips every task whose
    adapter is held (fail closed per task).

    Returns:
        List of task IDs that failed in this pass.
    """
    from mesh.adapters import evidence

    held = sorted(evidence.effective_holds(testing_mode))
    if not held:
        return []
    marks = ", ".join("?" for _ in held)
    cur = conn.execute(
        f"""SELECT t.id, t.project_id, t.fence_seq, t.attempt, t.max_attempts,
                   a.id AS assignee_id, a.adapter
            FROM tasks t
            JOIN agents a ON t.assignee_agent_id = a.id
            WHERE t.status = 'queued' AND a.delivery = 'launch' AND a.adapter IN ({marks});""",
        tuple(held),
    )
    failed: List[str] = []
    for task in cur.fetchall():
        details = _launch_hold_details(task["adapter"], testing_mode)
        if details is None:
            continue
        try:
            with transaction(conn):
                if fail_queued_task_t6(conn, task, LAUNCH_HELD_REASON, details, assignee_id=task["assignee_id"]):
                    failed.append(task["id"])
        except Exception as e:
            _log_to_meshd(home, f"Scheduler launch-hold check failed on task {task['id']}: {e}")
            continue
    return failed


def check_queued_tasks_t6(conn: sqlite3.Connection, home: str = "", testing_mode: bool = False) -> List[str]:
    """Check queued tasks against assignee constraints per plan §6.2 (T6).

    Transitions queued tasks to failed if:
    1. The task's model is no longer allowed for its assignee (reason: model_not_allowed)
    2. The assignee is delivery=inbox and offered_at + inbox.ack_timeout_s < now (reason: no_response)
    3. The assignee is delivery=launch and its adapter is held by ``evidence.LAUNCH_HOLDS``
       (reason: launch_held; M3 gate re-review P2, L11), checked before the install check
    4. The assignee is delivery=launch and the adapter is not installed (reason: adapter_unavailable)

    Returns:
        List of task IDs that failed in this pass.
    """
    cur = conn.execute(
        """SELECT t.id, t.project_id, t.title, t.spec, t.model, t.fence_seq,
                  t.attempt, t.max_attempts, t.offered_at,
                  a.id as assignee_id, a.delivery, a.adapter, a.binary_path, a.allowed_models_json
           FROM tasks t
           JOIN agents a ON t.assignee_agent_id = a.id
           WHERE t.status = 'queued';"""
    )
    candidates = cur.fetchall()
    failed_tasks: List[str] = []
    ack_timeout_s = get_inbox_ack_timeout_s(conn)
    now_dt = datetime.datetime.now(datetime.timezone.utc)

    for task in candidates:
        task_id = task["id"]
        project_id = task["project_id"]

        try:
            reason = None
            details: Dict[str, Any] = {}

            # 1. Model check: task model must be in allowed_models
            allowed_models: List[str] = []
            if task["allowed_models_json"]:
                try:
                    allowed_models = json.loads(task["allowed_models_json"])
                except Exception:
                    allowed_models = []

            if task["model"] and task["model"] not in allowed_models:
                reason = "model_not_allowed"
                details = {"model": task["model"], "allowed": allowed_models}
            elif task["delivery"] == "inbox" and task["offered_at"]:
                # 2. Inbox ack timeout check. E17 reading (run 3): the clock runs only
                # while the assignee could actually `task start` the task. While the
                # project is paused or the agent is at its concurrency limit, a start
                # would be refused (T5 guards), so `offered_at` is moved to now and
                # no_response is never decided for those ticks.
                _pos, blocked = compute_queue_info(conn, task_id)
                if blocked in ("project_paused", "concurrency_limit"):
                    conn.execute(
                        "UPDATE tasks SET offered_at = ? WHERE id = ? AND status = 'queued';",
                        (now_iso_utc(), task_id),
                    )
                    continue
                try:
                    offered_dt = parse_utc(task["offered_at"])
                    if now_dt > offered_dt + datetime.timedelta(seconds=ack_timeout_s):
                        reason = "no_response"
                        details = {"offered_at": task["offered_at"], "ack_window_start": task["offered_at"],
                                   "timeout_s": ack_timeout_s}
                except Exception:
                    pass
            elif task["delivery"] == "launch":
                # 3. Launch hold (M3 gate re-review P2, L11): a held adapter never starts,
                # also for a task assigned or queued before the hold (AG step 4 only
                # guards new assignments). Checked first: it needs no binary.
                held = _launch_hold_details(task["adapter"], testing_mode)
                if held is not None:
                    reason = LAUNCH_HELD_REASON
                    details = held
                else:
                    # 4. Launch adapter installed check
                    try:
                        adapter = get_adapter(task["adapter"], testing_mode=testing_mode)
                        if adapter.kind == "test_double" and not testing_mode:
                            installed = False
                        else:
                            installed = adapter.is_installed(binary_path=task["binary_path"])
                    except Exception:
                        installed = False

                    if not installed:
                        reason = "adapter_unavailable"
                        details = {"adapter": task["adapter"]}

            if not reason:
                continue

            # Single transaction covering status update, event record, and outbox fan-out
            with transaction(conn):
                if not fail_queued_task_t6(conn, task, reason, details, assignee_id=task["assignee_id"]):
                    continue
                failed_tasks.append(task_id)
        except Exception as e:
            _log_to_meshd(home, f"Scheduler T6 check failed on task {task_id}: {e}")
            continue

    return failed_tasks


def sweep_expired_approvals(conn: sqlite3.Connection, home: str = "") -> List[str]:
    """Sweep approvals with expires_at < now and status pending or approved per plan §6.5, §7.4."""
    now = now_iso_utc()
    cur = conn.execute(
        """SELECT id, category, op_type, requested_by, project_id, task_id, status, expires_at
           FROM approvals
           WHERE status IN ('pending', 'approved') AND expires_at < ?;""",
        (now,),
    )
    candidates = cur.fetchall()
    expired_ids: List[str] = []

    for row in candidates:
        apr_id = row["id"]
        try:
            with transaction(conn):
                cur_upd = conn.execute(
                    "UPDATE approvals SET status = 'expired' WHERE id = ? AND status IN ('pending', 'approved');",
                    (apr_id,),
                )
                if cur_upd.rowcount == 0:
                    continue

                record_event(
                    conn=conn,
                    type_="approval.expired",
                    actor_id="system",
                    actor_kind="system",
                    via="system",
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    approval_id=apr_id,
                    payload={
                        "approval_id": apr_id,
                        "category": row["category"],
                        "op": row["op_type"],
                        "requested_by": row["requested_by"],
                        "expires_at": row["expires_at"],
                        "status": "expired",
                    },
                )
                expired_ids.append(apr_id)
        except Exception as e:
            _log_to_meshd(home, f"Scheduler approval expiry sweep failed on approval {apr_id}: {e}")
            continue

    return expired_ids


def sweep_expired_inbox_leases(conn: sqlite3.Connection, home: str = "") -> List[str]:
    """Sweep expired agent-held leases per plan §6.2 (T8) and §6.4.

    For leases with holder_kind='agent' and expires_at < now:
    - delete the lease
    - run -> 'lost' with end_reason='lease_expired'
    - task 'running' -> 'failed' with failure_json.reason='lease_expired'
    - record event task.failed
    - fan-out notifies the project lead
    """
    now = now_iso_utc()
    cur = conn.execute(
        """SELECT l.key, l.run_id, l.fence, l.expires_at,
                  r.id as run_id, r.task_id, r.project_id,
                  t.attempt, t.max_attempts, t.status as task_status
           FROM leases l
           JOIN runs r ON l.run_id = r.id
           JOIN tasks t ON r.task_id = t.id
           WHERE l.holder_kind = 'agent' AND l.expires_at < ?;""",
        (now,),
    )
    candidates = cur.fetchall()
    expired_tasks: List[str] = []

    for row in candidates:
        lease_key = row["key"]
        run_id = row["run_id"]
        task_id = row["task_id"]
        project_id = row["project_id"]
        fence = row["fence"]
        attempt = row["attempt"]
        max_attempts = row["max_attempts"]

        try:
            with transaction(conn):
                # 1. Delete lease
                cur_del = conn.execute("DELETE FROM leases WHERE key = ?;", (lease_key,))
                if cur_del.rowcount == 0:
                    continue

                # 2. Update run to lost
                conn.execute(
                    """UPDATE runs
                       SET status = 'lost',
                           end_reason = 'lease_expired',
                           ended_at = ?,
                           result_json = ?
                       WHERE id = ?;""",
                    (now, json.dumps({"reason": "lease_expired"}), run_id),
                )

                # 3. Update task running -> failed
                failure_dict = {
                    "reason": "lease_expired",
                    "details": {"run_id": run_id, "fence": fence},
                }
                cur_upd = conn.execute(
                    """UPDATE tasks
                       SET status = 'failed',
                           failure_json = ?,
                           updated_at = ?,
                           version = version + 1
                       WHERE id = ? AND status = 'running';""",
                    (json.dumps(failure_dict), now, task_id),
                )
                if cur_upd.rowcount == 0:
                    continue

                # 4. Record event task.failed (notifies lead via outbox fan-out)
                record_event(
                    conn=conn,
                    type_="task.failed",
                    actor_id="system",
                    actor_kind="system",
                    via="system",
                    project_id=project_id,
                    task_id=task_id,
                    run_id=run_id,
                    from_state="running",
                    to_state="failed",
                    reason="lease_expired",
                    payload={
                        "task_id": task_id,
                        "project_id": project_id,
                        "reason": "lease_expired",
                        "details": {"run_id": run_id, "fence": fence},
                        "fence": fence,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                    },
                )
                expired_tasks.append(task_id)
        except Exception as e:
            _log_to_meshd(home, f"Scheduler lease expiry sweep failed on task {task_id}: {e}")
            continue

    return expired_tasks


def run_scheduler_tick(conn: sqlite3.Connection, home: str = "", testing_mode: bool = False) -> List[str]:
    """Execute a single scheduler tick pass."""
    expired_aprs = sweep_expired_approvals(conn=conn, home=home)
    expired_leases = sweep_expired_inbox_leases(conn=conn, home=home)
    closed = watch_launch_runs_tick(conn=conn, home=home)
    failed = check_queued_tasks_t6(conn=conn, home=home, testing_mode=testing_mode)
    promoted = promote_tasks(conn=conn, home=home, testing_mode=testing_mode)
    # T5 guard (§6.2, §9.1): tasks promoted in this pass get the same model/adapter
    # re-check before any launch, so a disallowed model never starts.
    failed = failed + check_queued_tasks_t6(conn=conn, home=home, testing_mode=testing_mode)
    started = start_queued_launch_tasks(conn=conn, home=home, testing_mode=testing_mode)
    # Lead turns (§12.4, m2-r9): after the task starts, so a turn sees this pass's events.
    lead_turns = start_lead_turns_tick(conn=conn, home=home, testing_mode=testing_mode)
    return promoted + failed + expired_aprs + expired_leases + closed + started + lead_turns


def start_lead_turns_tick(conn: sqlite3.Connection, home: str = "",
                          testing_mode: bool = False) -> List[str]:
    """Start lead turns whose trigger fires (plan §12.4, m2-r9)."""
    from mesh.daemon.lead_turns import start_lead_turns

    if testing_mode and home and os.path.exists(os.path.join(home, "run", "launch_hold")):
        return []

    try:
        return start_lead_turns(conn, home, testing_mode=testing_mode)
    except Exception as e:
        _log_to_meshd(home, f"Scheduler lead turn start failed: {type(e).__name__}: {e}")
        return []


def watch_launch_runs_tick(conn: sqlite3.Connection, home: str = "") -> List[str]:
    """Supervisor watch pass (plan §11.3): exit handling, lost wrappers, lease renewal."""
    from mesh.daemon.supervisor import watch_launch_runs

    try:
        return watch_launch_runs(conn, home)
    except Exception as e:
        _log_to_meshd(home, f"Supervisor watch failed: {type(e).__name__}: {e}")
        return []


def start_queued_launch_tasks(conn: sqlite3.Connection, home: str = "", testing_mode: bool = False) -> List[str]:
    """Start queued launch-mode tasks (T5) per plan §9.3, §11.1.

    Runs after promotion so a task promoted in this pass can start in the same
    pass. The tick runs in autocommit mode (after the request transaction has
    committed), so the supervisor opens its own TX A / TX B per plan §11.1.

    M3 gate re-review P2 (L11): right before any spawn, every queued launch task whose
    adapter is held (``evidence.LAUNCH_HOLDS``) is failed with ``launch_held``
    (:func:`fail_held_launch_tasks`), so this path never starts one even when the T6
    pass did not run first. If that check fails, no launch run starts on this tick.

    Testing hook: when ``$MESH_HOME/run/launch_hold`` exists, no launch run is
    started. M1b tests that assert on the queued state of a launch task create
    that file in their setup; it is never honoured outside testing mode.
    """
    from mesh.daemon.supervisor import spawn_launch_runs

    if testing_mode and home and os.path.exists(os.path.join(home, "run", "launch_hold")):
        return []

    try:
        fail_held_launch_tasks(conn, home=home, testing_mode=testing_mode)
    except Exception as e:
        _log_to_meshd(home, f"Scheduler T5 launch-hold check failed; no launch run this tick: "
                            f"{type(e).__name__}: {e}")
        return []

    try:
        return spawn_launch_runs(conn, home, testing_mode=testing_mode)
    except Exception as e:
        _log_to_meshd(home, f"Scheduler T5 launch start failed: {type(e).__name__}: {e}")
        return []
