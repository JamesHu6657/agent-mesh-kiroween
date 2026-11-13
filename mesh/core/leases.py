"""Task lease management and fencing verification per plan §11.5."""

import hashlib
import json
import shlex
import sqlite3
from typing import Any, Dict, Optional

import mesh.constants as c
from mesh.errors import MeshError


def verify_task_lease_and_fence(
    conn: sqlite3.Connection,
    task_id: str,
    presented_run_id: Optional[str] = None,
    presented_fence: Optional[int] = None,
) -> Dict[str, Any]:
    """Verify stale-write fence and active lease in the exact order specified by plan §11.5.

    Check order (§11.5):
    1. If no active lease exists for task:<task_id> -> exit 9 LEASE_EXPIRED.
    2. If lease exists but presented_fence != lease.fence -> exit 9 FENCE_MISMATCH.
    3. If lease exists and presented_run_id is given but != lease.run_id -> exit 9 FENCE_MISMATCH.

    Returns the lease row as a dictionary on success.
    """
    lease_key = f"task:{task_id}"
    cur = conn.execute("SELECT * FROM leases WHERE key = ?;", (lease_key,))
    row = cur.fetchone()
    if not row:
        details: Dict[str, Any] = {"task_id": task_id, "lease_key": lease_key}
        message = f"No active lease held for task '{task_id}'"
        hint = _lost_lease_hint(conn, task_id, presented_run_id, details)
        if hint:
            message = f"{message}: {hint}"
        raise MeshError(
            code=c.LEASE_EXPIRED,
            exit_code=c.EXIT_STALE,  # 9
            message=message,
            details=details,
        )

    lease_dict = dict(row)
    lease_fence = lease_dict.get("fence")
    lease_run_id = lease_dict.get("run_id")

    if presented_fence is not None and presented_fence != lease_fence:
        raise MeshError(
            code=c.FENCE_MISMATCH,
            exit_code=c.EXIT_STALE,  # 9
            message=f"Fence mismatch on task '{task_id}': presented fence {presented_fence} does not match current lease fence {lease_fence}",
            details={"task_id": task_id, "presented_fence": presented_fence, "current_fence": lease_fence},
        )

    if presented_run_id is not None and presented_run_id != lease_run_id:
        raise MeshError(
            code=c.FENCE_MISMATCH,
            exit_code=c.EXIT_STALE,  # 9
            message=f"Run mismatch on task '{task_id}': presented run '{presented_run_id}' does not match current lease run '{lease_run_id}'",
            details={"task_id": task_id, "presented_run_id": presented_run_id, "current_run_id": lease_run_id},
        )

    return lease_dict


def _lost_lease_hint(conn: sqlite3.Connection, task_id: str, presented_run_id: Optional[str],
                     details: Dict[str, Any]) -> str:
    """Explain to a remote (inbox) worker why its lease is gone and what to do next.

    Adds run/task state to ``details`` and returns a one-line hint ("" if nothing is
    known). Read-only; never changes the error code.
    """
    try:
        trow = conn.execute("SELECT status FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        rrow = None
        if presented_run_id:
            rrow = conn.execute("SELECT status, end_reason, ended_at FROM runs WHERE id = ? AND task_id = ?;",
                                (presented_run_id, task_id)).fetchone()
    except sqlite3.Error:
        return ""
    task_status = trow["status"] if trow is not None else "unknown"
    if trow is not None:
        details["task_status"] = task_status
    if rrow is not None:
        details["run_status"] = rrow["status"]
        details["run_end_reason"] = rrow["end_reason"]
        details["run_ended_at"] = rrow["ended_at"]
    if rrow is not None and rrow["end_reason"] == "lease_expired":
        ttl = get_lease_ttl_s(conn)
        details["lease_ttl_s"] = ttl
        return (f"this run's lease expired at {rrow['ended_at']} (no heartbeat/progress within {ttl}s) "
                f"and the task is now '{task_status}'. Stop work on this run; its writes are fenced off. "
                f"The lead can retry or reassign the task, and you will get a new offer. "
                f"Heartbeat at least every {max(1, ttl // 3)}s to keep a lease.")
    if rrow is not None and rrow["end_reason"] == "submitted":
        sub = _run_event(conn, task_id, presented_run_id, "task.submitted")
        if sub is not None:
            details["submitted_at"] = sub["ts"]
            details["submitted_by"] = sub["actor"]
            details["submitted_request_id"] = sub["request_id"]
            if sub["session"]:
                details["submitted_session"] = sub["session"]
            by = f" by {sub['actor']}" if sub["actor"] else ""
            sess = f", session {sub['session']}" if sub["session"] else ""
            return (f"this run was already submitted at {sub['ts']}{by} (request {sub['request_id']}{sess}); "
                    f"the task is now '{task_status}'. If this client did not submit it, another session "
                    f"is using the same token. Stop work on this run; nothing more is needed for it.")
    if rrow is not None and rrow["end_reason"]:
        return (f"this run already ended ({rrow['status']}: {rrow['end_reason']}) and the task is now "
                f"'{task_status}'. Stop work on this run.")
    if trow is not None and task_status != "running":
        return f"the task is '{task_status}', not running; run `mesh task start` after it is offered to you."
    return ""


def _run_event(conn: sqlite3.Connection, task_id: str, run_id: Optional[str],
               type_: str) -> Optional[Dict[str, Any]]:
    """The latest ``type_`` event of a run: ts, actor name, request id, client session."""
    if not run_id:
        return None
    try:
        row = conn.execute(
            "SELECT e.ts, e.actor_id, e.request_id, e.payload_json, a.name AS actor_name FROM events e "
            "LEFT JOIN principals a ON a.id = e.actor_id "
            "WHERE e.task_id = ? AND e.run_id = ? AND e.type = ? ORDER BY e.seq DESC LIMIT 1;",
            (task_id, run_id, type_)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except ValueError:
        payload = {}
    return {"ts": row["ts"], "actor": row["actor_name"] or row["actor_id"], "request_id": row["request_id"],
            "session": payload.get("session") if isinstance(payload, dict) else None}


def _session_key(run_id: Optional[str]) -> str:
    return f"run_session:{run_id}"


def session_digest(session: str) -> str:
    return hashlib.sha256(session.encode("utf-8")).hexdigest()


def remember_run_session(conn: sqlite3.Connection, run_id: str, session: Optional[str]) -> None:
    """Store the starting session's digest (PR #37 review #4): the ``task.started`` event
    payload is redacted, so a session id that looks secret-like (``worker-secret=alpha``)
    is stored as ``worker-[REDACTED]`` there and can never match again."""
    if session:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?);",
                     (_session_key(run_id), session_digest(session)))


def _stored_session_digest(conn: sqlite3.Connection, run_id: Optional[str]) -> Optional[str]:
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?;", (_session_key(run_id),)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row is not None else None


def check_run_session(conn: sqlite3.Connection, task_id: str, run_id: Optional[str],
                      session: Optional[str]) -> None:
    """Refuse a fenced write from a different client session than the one that started the run.

    Only when the run's ``task.started`` recorded a session: since batch 5 the CLI's
    ``task start`` always sends one (``MESH_SESSION``, or a generated id it prints), so
    two worker sessions sharing one agent token cannot both drive the same run. A write
    naming another session -> ``session_mismatch``; naming none -> ``session_missing``
    (both exit 9 ``FENCE_MISMATCH``, so a keepalive stops). A run started without a
    session (an older client, a launch run) is never checked.
    """
    started = _run_event(conn, task_id, run_id, "task.started")
    if started is None or not started["session"]:
        return  # no session recorded (old client, launch run): nothing to check
    digest = _stored_session_digest(conn, run_id)
    if digest is not None:
        if session and session_digest(session) == digest:
            return
    elif started["session"] == session:
        return  # run started before digests were stored
    if not session:
        stored = str(started["session"])
        if "[REDACTED" in stored:
            # PR #37 review N1: the stored copy is display-redacted; exporting it cannot match
            # the digest. Point to the original value instead of offering a broken command.
            how = ("In the shell that started it, set MESH_SESSION to the exact `session` value that "
                   "`task start` printed (its `export` line); the copy Mesh shows here is redacted.")
        else:
            how = (f"In the shell that started it, run `export MESH_SESSION={shlex.quote(stored)}` "
                   f"(printed by `task start`).")
        raise MeshError(
            code=c.FENCE_MISMATCH,
            exit_code=c.EXIT_STALE,
            message=(f"Run '{run_id}' of task '{task_id}' belongs to client session '{started['session']}' "
                     f"(started at {started['ts']}), and this request names no session. {how} "
                     f"If you did not start this run, another worker session is using the same token: "
                     f"leave it alone."),
            details={"task_id": task_id, "run_id": run_id, "reason": "session_missing",
                     "run_session": started["session"], "started_at": started["ts"]},
        )
    raise MeshError(
        code=c.FENCE_MISMATCH,
        exit_code=c.EXIT_STALE,
        message=(f"Run '{run_id}' of task '{task_id}' was started at {started['ts']} by client session "
                 f"'{started['session']}', not this one ('{session}'): another worker session is using "
                 f"the same token. Only the session that started a run may heartbeat, report or submit it."),
        details={"task_id": task_id, "run_id": run_id, "reason": "session_mismatch",
                 "run_session": started["session"], "presented_session": session,
                 "started_at": started["ts"]},
    )


def get_lease_ttl_s(conn: sqlite3.Connection) -> int:
    """Retrieve lease.ttl_s setting from database (default 120s)."""
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'lease.ttl_s';")
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            return int(json.loads(row["value_json"]))
        except Exception:
            return 120
    return 120
