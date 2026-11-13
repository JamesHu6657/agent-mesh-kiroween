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
from mesh.core.events import last_rejection, record_event
from mesh.core.settings import get_agent_ack_timeout_s

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


#: Scheduler errors since this meshd started (batch 5): count and the latest one. Also
#: written to $MESH_HOME/run/scheduler_errors.json so `mesh doctor` sees them offline.
SCHEDULER_ERRORS: Dict[str, Any] = {"count": 0, "last": None, "by_step": {}}
SCHEDULER_ERRORS_FILE = "scheduler_errors.json"


def reset_scheduler_errors(home: str = "") -> None:
    """At meshd start: forget the previous process's errors."""
    SCHEDULER_ERRORS.update(count=0, last=None, by_step={})
    if home:
        try:
            os.unlink(os.path.join(home, "run", SCHEDULER_ERRORS_FILE))
        except OSError:
            pass


def scheduler_errors_view() -> Dict[str, Any]:
    return {"sweep_errors": SCHEDULER_ERRORS["count"], "last_sweep_error": SCHEDULER_ERRORS["last"],
            "sweep_errors_by_step": dict(SCHEDULER_ERRORS["by_step"])}


def _sweep_error(home: str, step: str, exc: BaseException, item: Optional[str] = None) -> None:
    """A scheduler step failed: log it with its traceback, count it, record the latest.

    Never raises (the tick goes on with its other steps)."""
    import traceback

    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        where = f" on {item}" if item else ""
        _log_to_meshd(home, f"Scheduler step {step} failed{where}: {type(exc).__name__}: {exc}\n{tb}")
        SCHEDULER_ERRORS["count"] += 1
        SCHEDULER_ERRORS["by_step"][step] = SCHEDULER_ERRORS["by_step"].get(step, 0) + 1
        SCHEDULER_ERRORS["last"] = {"step": step, "item": item, "error": redact(f"{type(exc).__name__}: {exc}"),
                                    "at": now_iso_utc()}
        if home:
            run_dir = os.path.join(home, "run")
            os.makedirs(run_dir, mode=0o700, exist_ok=True)
            path = os.path.join(run_dir, SCHEDULER_ERRORS_FILE)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"count": SCHEDULER_ERRORS["count"], "last": SCHEDULER_ERRORS["last"],
                           "by_step": SCHEDULER_ERRORS["by_step"], "pid": os.getpid()}, f)
            os.replace(tmp, path)
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
                            "ack_timeout_s": ack_window_now(conn, task["assignee_agent_id"])[0],
                            "last_rejection": last_rejection(conn, task_id),
                        },
                    )

                promoted.append(task_id)
        except Exception as e:
            _sweep_error(home, "promote", e, task_id)
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
            _sweep_error(home, "launch_hold", e, task["id"])
            continue
    return failed


def ack_window_now(conn: sqlite3.Connection, agent_id: Optional[str]) -> Tuple[int, bool]:
    """The ack window an offer made now gets: (seconds, set per agent?)."""
    own = get_agent_ack_timeout_s(conn, agent_id) if agent_id else None
    if own is not None:
        return own, True
    return get_inbox_ack_timeout_s(conn), False


def offered_ack_window(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """The ack window recorded in the task's latest task.offered event (None for offers
    made before windows were recorded)."""
    row = conn.execute(
        "SELECT json_extract(payload_json, '$.ack_timeout_s') AS w FROM events "
        "WHERE task_id = ? AND type = 'task.offered' ORDER BY seq DESC LIMIT 1;",
        (task_id,),
    ).fetchone()
    try:
        return int(row["w"]) if row and row["w"] is not None else None
    except (TypeError, ValueError):
        return None


def effective_ack_window(conn: sqlite3.Connection, task_id: str, agent_id: Optional[str]) -> Tuple[int, bool, Optional[int]]:
    """Window used for the no_response decision: never shorter than the one the offer was
    made with. Lowering inbox.ack_timeout_s or `--ack-timeout` applies to new offers only;
    raising it also extends offers already out. Returns (seconds, per_agent, offered)."""
    now_s, per_agent = ack_window_now(conn, agent_id)
    offered = offered_ack_window(conn, task_id)
    if offered is not None and offered > now_s:
        return offered, per_agent, offered
    return now_s, per_agent, offered


def offers_kept_by_window(conn: sqlite3.Connection, agent_id: Optional[str], new_window_s: int) -> List[Dict[str, Any]]:
    """Queued inbox offers (of one agent, or all agents using the global window when
    agent_id is None) that the new, shorter window would already have timed out; they
    keep the window they were offered with."""
    from mesh.core.settings import agent_ack_timeout_key

    rows = conn.execute(
        """SELECT t.id, t.offered_at, t.assignee_agent_id FROM tasks t JOIN agents a ON a.id = t.assignee_agent_id
           WHERE t.status = 'queued' AND a.delivery = 'inbox' AND t.offered_at IS NOT NULL"""
        + (" AND t.assignee_agent_id = ?" if agent_id else "") + ";",
        ((agent_id,) if agent_id else ()),
    ).fetchall()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    out: List[Dict[str, Any]] = []
    for r in rows:
        if agent_id is None and conn.execute(
                "SELECT 1 FROM settings WHERE key = ?;", (agent_ack_timeout_key(r["assignee_agent_id"]),)).fetchone():
            continue  # its own window, unaffected by the global setting
        try:
            if now_dt > parse_utc(r["offered_at"]) + datetime.timedelta(seconds=new_window_s):
                offered = offered_ack_window(conn, r["id"])
                out.append({"task_id": r["id"], "offered_at": r["offered_at"], "ack_timeout_s": offered,
                            # offers recorded before windows were stored use the current value
                            "kept": offered is not None and offered > new_window_s})
        except Exception:
            continue
    return out


def no_response_details(task_id: str, agent_name: Optional[str], offered_at: str, window_s: int,
                        per_agent: bool) -> Dict[str, Any]:
    """T6 `no_response` details: the window, which setting set it, and how to recover.

    They are stored in the task's failure and the task.failed event, so the lead's
    notification says the same thing.
    """
    who = agent_name or "the assignee"
    setting = (f"agent {who} --ack-timeout" if per_agent else "inbox.ack_timeout_s")
    return {
        "offered_at": offered_at,
        "ack_window_start": offered_at,
        "timeout_s": window_s,
        "setting": setting,
        "agent": agent_name,
        "hint": (
            f"{who} did not run `mesh task start` within {window_s}s of the offer ({setting}). "
            f"Retry with: mesh task retry {task_id} --reason \"...\". "
            f"To give {who} longer to pick tasks up: mesh agent update {who} --ack-timeout 2h --user"
            + ("" if per_agent else " (or `mesh config set inbox.ack_timeout_s <seconds> --user` for every inbox agent)")
            + "."
        ),
    }


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
                  a.id as assignee_id, a.delivery, a.adapter, a.binary_path, a.allowed_models_json,
                  p.name as assignee_name
           FROM tasks t
           JOIN agents a ON t.assignee_agent_id = a.id
           LEFT JOIN principals p ON p.id = a.id
           WHERE t.status = 'queued';"""
    )
    candidates = cur.fetchall()
    failed_tasks: List[str] = []
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
                    window_s, per_agent, _offered = effective_ack_window(conn, task_id, task["assignee_id"])
                    if now_dt > offered_dt + datetime.timedelta(seconds=window_s):
                        reason = "no_response"
                        details = no_response_details(task_id, task["assignee_name"], task["offered_at"],
                                                      window_s, per_agent)
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
            _sweep_error(home, "t6_check", e, task_id)
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
            _sweep_error(home, "approval_expiry", e, apr_id)
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
                task_failed = cur_upd.rowcount > 0
                holder = conn.execute("SELECT a.name FROM runs r LEFT JOIN principals a ON a.id = r.agent_id "
                                      "WHERE r.id = ?;", (run_id,)).fetchone()
                # 3b. Always a task event for the lost lease (task history + the lead), also
                #     when the task was no longer running and so does not fail here.
                record_event(
                    conn=conn,
                    type_="task.lease_expired",
                    actor_id="system",
                    actor_kind="system",
                    via="system",
                    project_id=project_id,
                    task_id=task_id,
                    run_id=run_id,
                    reason="lease_expired",
                    payload={
                        "task_id": task_id,
                        "project_id": project_id,
                        "run_id": run_id,
                        "fence": fence,
                        "attempt": attempt,
                        "holder": holder["name"] if holder is not None else None,
                        "lease_expires_at": row["expires_at"],
                        "task_status": "failed" if task_failed else row["task_status"],
                        "task_failed": task_failed,
                    },
                )
                if not task_failed:
                    expired_tasks.append(task_id)
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
            _sweep_error(home, "lease_expiry", e, task_id)
            continue

    return expired_tasks


def _injected_failure(home: str, name: str, testing_mode: bool) -> None:
    """Testing hook (like ``run/launch_hold``): ``$MESH_HOME/run/scheduler_fail_step``
    naming a step makes that step raise. Never honoured outside testing mode."""
    if not (testing_mode and home):
        return
    try:
        with open(os.path.join(home, "run", "scheduler_fail_step"), "r", encoding="utf-8") as f:
            wanted = f.read().split()
    except OSError:
        return
    if name in wanted:
        raise RuntimeError(f"injected failure in scheduler step {name} (testing hook)")


def _step(_home: str, _name: str, _fn, *args, testing_hook: bool = False, **kwargs) -> List[str]:
    """Run one tick step; an exception is logged/counted and the tick goes on."""
    return _step_ok(_home, _name, _fn, *args, testing_hook=testing_hook, **kwargs)[0]


def _step_ok(_home: str, _name: str, _fn, *args, testing_hook: bool = False, **kwargs):
    """Like _step, but also says whether the step completed: (result, ok)."""
    home, name, fn = _home, _name, _fn
    try:
        _injected_failure(home, name, testing_hook)
        return (fn(*args, **kwargs) or []), True
    except Exception as e:
        _sweep_error(home, name, e)
        return [], False


def run_scheduler_tick(conn: sqlite3.Connection, home: str = "", testing_mode: bool = False) -> List[str]:
    """Execute a single scheduler tick pass.

    Each step runs on its own (batch 5): one raising step is logged with its traceback,
    counted (`mesh daemon status`, `mesh doctor`) and does not stop the other steps."""
    expired_aprs = _step(home, "approval_expiry", sweep_expired_approvals, testing_hook=testing_mode, conn=conn, home=home)
    expired_leases = _step(home, "lease_expiry", sweep_expired_inbox_leases, testing_hook=testing_mode, conn=conn, home=home)
    closed = _step(home, "supervisor_watch", watch_launch_runs_tick, testing_hook=testing_mode, conn=conn, home=home)
    failed, t6_ok = _step_ok(home, "t6_check", check_queued_tasks_t6, testing_hook=testing_mode, conn=conn, home=home,
                             testing_mode=testing_mode)
    promoted = _step(home, "promote", promote_tasks, testing_hook=testing_mode, conn=conn, home=home, testing_mode=testing_mode)
    # T5 guard (§6.2, §9.1): tasks promoted in this pass get the same model/adapter
    # re-check before any launch, so a disallowed model never starts.
    failed2, t6_ok2 = _step_ok(home, "t6_check", check_queued_tasks_t6, testing_hook=testing_mode, conn=conn,
                               home=home, testing_mode=testing_mode)
    failed = failed + failed2
    # PR #37 review D1: fail closed. If either T6 check raised, this pass did not prove
    # the queued tasks' model/adapter are allowed, so nothing launches this tick (the
    # error is already logged and counted); the next tick re-checks and launches.
    started: List[str] = []
    if t6_ok and t6_ok2:
        started = _step(home, "launch_start", start_queued_launch_tasks, testing_hook=testing_mode, conn=conn,
                        home=home, testing_mode=testing_mode)
    # Lead turns (§12.4, m2-r9): after the task starts, so a turn sees this pass's events.
    lead_turns = _step(home, "lead_turns", start_lead_turns_tick, testing_hook=testing_mode, conn=conn, home=home, testing_mode=testing_mode)
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
        _sweep_error(home, "lead_turns", e)
        return []


def watch_launch_runs_tick(conn: sqlite3.Connection, home: str = "") -> List[str]:
    """Supervisor watch pass (plan §11.3): exit handling, lost wrappers, lease renewal."""
    from mesh.daemon.supervisor import watch_launch_runs

    try:
        return watch_launch_runs(conn, home)
    except Exception as e:
        _sweep_error(home, "supervisor_watch", e)
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
        _sweep_error(home, "launch_hold_t5", e)
        return []

    try:
        return spawn_launch_runs(conn, home, testing_mode=testing_mode)
    except Exception as e:
        _sweep_error(home, "launch_start", e)
        return []
