"""Event log query handlers per plan §12.1 and §13.3."""

import asyncio
import json
import sqlite3
import time
from typing import Any, Dict, List

import mesh.constants as c
from mesh.core.authz import check_authz, get_agent_capabilities, lead_run_token_scope
from mesh.core.clock import parse_duration
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.errors import MeshError


def handle_event_list(ctx: HandlerContext) -> HandlerResult:
    """List events with optional project, task, type, and since filters."""
    return _list_events(ctx, "event.list")


async def handle_event_tail_live(server: Any, ctx: HandlerContext, reader: Any, writer: Any) -> None:
    """Stream live events matching filters as NDJSON over the Unix socket."""
    project_id = ctx.args.get("project")
    task_id = ctx.args.get("task")
    event_type = ctx.args.get("type")
    since_seq = ctx.args.get("since")
    timeout_val = ctx.args.get("timeout")

    timeout_s = None
    if timeout_val is not None:
        try:
            timeout_s = parse_duration(timeout_val)
        except ValueError:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid duration format: '{timeout_val}'",
                details={"param": "timeout"},
            )
        if timeout_s <= 0:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Timeout must be greater than zero",
                details={"param": "timeout"},
            )

    check_authz(ctx.conn, "event.tail", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    if since_seq is None:
        cur_m = ctx.conn.execute("SELECT MAX(seq) as m FROM events;")
        row_m = cur_m.fetchone()
        last_seq = row_m["m"] if row_m and row_m["m"] is not None else 0
    else:
        last_seq = int(since_seq)

    start_mono = time.monotonic()
    end_mono = (start_mono + timeout_s) if timeout_s is not None else None

    # Run-scoped (lead run) tokens may only stream their own project,
    # mirroring _list_events (pre-public review P2: tail ignored scope).
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        project_id = scope_project

    caps = ctx.principal.get("capabilities", [])
    if not caps and ctx.principal.get("id"):
        caps = get_agent_capabilities(ctx.conn, ctx.principal["id"])

    apply_own_filter = (
        scope_project is None
        and ctx.actor_kind != "user"
        and "relay" not in caps
        and not project_id
        and not task_id
    )
    agent_id = ctx.principal.get("id")

    server.active_streams = getattr(server, "active_streams", 0) + 1
    try:
        while True:
            if (reader is not None and reader.at_eof()) or writer.is_closing():
                break
            # Pre-public re-review P2: authority is re-checked before every batch, so a
            # stream ends once its credential expires or is revoked, its principal is
            # disabled, or (for a lead run token) the lead assignment is superseded.
            if not _stream_still_authorized(ctx, project_id, task_id):
                break

            query = """
            SELECT seq, id, ts, type, project_id, task_id, run_id, approval_id,
                   actor_id, actor_kind, via, relayed_by, credential_id,
                   from_state, to_state, reason, payload_json, request_id,
                   prev_hash, hash, user_mac
            FROM events
            WHERE seq > ?
        """
            params: List[Any] = [last_seq]

            if project_id:
                query += " AND project_id = ?"
                params.append(project_id)
            if task_id:
                query += " AND task_id = ?"
                params.append(task_id)
            if event_type:
                query += " AND type = ?"
                params.append(event_type)

            if apply_own_filter and agent_id:
                query += """ AND (
                actor_id = ?
                OR task_id IN (SELECT id FROM tasks WHERE assignee_agent_id = ?)
                OR project_id IN (
                    SELECT project_id FROM lead_assignments WHERE agent_id = ?
                    UNION
                    SELECT id FROM projects WHERE origin_principal_id = ?
                )
            )"""
                params.extend([agent_id, agent_id, agent_id, agent_id])

            query += " ORDER BY seq ASC LIMIT 100;"

            cur = ctx.conn.execute(query, params)
            rows = cur.fetchall()

            for r in rows:
                payload = {}
                try:
                    payload = json.loads(r["payload_json"])
                except Exception:
                    pass
                event_obj = {
                    "seq": r["seq"],
                    "id": r["id"],
                    "ts": r["ts"],
                    "type": r["type"],
                    "project_id": r["project_id"],
                    "task_id": r["task_id"],
                    "run_id": r["run_id"],
                    "approval_id": r["approval_id"],
                    "actor_id": r["actor_id"],
                    "actor_kind": r["actor_kind"],
                    "via": r["via"],
                    "relayed_by": r["relayed_by"],
                    "credential_id": r["credential_id"],
                    "from_state": r["from_state"],
                    "to_state": r["to_state"],
                    "reason": r["reason"],
                    "payload": payload,
                    "request_id": r["request_id"],
                    "prev_hash": r["prev_hash"],
                    "hash": r["hash"],
                    "user_mac": r["user_mac"],
                }
                writer.write((json.dumps(event_obj) + "\n").encode("utf-8"))
                last_seq = r["seq"]

            if rows:
                try:
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    break

            if (reader is not None and reader.at_eof()) or writer.is_closing():
                break

            if end_mono is not None:
                remaining = end_mono - time.monotonic()
                if remaining <= 0:
                    break
                wait_timeout = min(remaining, 0.5)
            else:
                wait_timeout = 0.5

            async with server.notify_cond:
                try:
                    await asyncio.wait_for(server.notify_cond.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    pass

            if (reader is not None and reader.at_eof()) or writer.is_closing():
                break
    finally:
        server.active_streams = max(0, getattr(server, "active_streams", 1) - 1)


def _stream_still_authorized(ctx: HandlerContext, project_id: Any, task_id: Any) -> bool:
    """Re-validate an open event.tail stream: credential, principal and authorization."""
    from mesh.core.clock import now_iso_utc

    if ctx.actor_kind == "user" and ctx.via == "direct":
        return True
    cred_id = ctx.principal.get("credential_id") or ctx.credential_id
    try:
        if cred_id:
            row = ctx.conn.execute(
                "SELECT expires_at, revoked_at FROM credentials WHERE id = ?;", (cred_id,)
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return False
            if row["expires_at"] is not None and row["expires_at"] < now_iso_utc():
                return False
        pid = ctx.principal.get("id")
        if pid:
            prow = ctx.conn.execute("SELECT disabled_at FROM principals WHERE id = ?;", (pid,)).fetchone()
            if prow is None or prow["disabled_at"] is not None:
                return False
        check_authz(ctx.conn, "event.tail", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=project_id, task_id=task_id)
    except MeshError:
        return False
    return True


def _list_events(ctx: HandlerContext, op: str) -> HandlerResult:
    """Internal event query implementation with strict operation parameter."""
    project_id = ctx.args.get("project")
    task_id = ctx.args.get("task")
    event_type = ctx.args.get("type")
    since_seq = ctx.args.get("since")
    limit = int(ctx.args.get("limit") or (100 if op == "event.tail" else 50))

    if op == "event.tail" and since_seq is None:
        cur_m = ctx.conn.execute("SELECT MAX(seq) as m FROM events;")
        row_m = cur_m.fetchone()
        since_seq = row_m["m"] if row_m and row_m["m"] is not None else 0

    check_authz(ctx.conn, op, ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    query = """
        SELECT seq, id, ts, type, project_id, task_id, run_id, approval_id,
               actor_id, actor_kind, via, relayed_by, credential_id,
               from_state, to_state, reason, payload_json, request_id,
               prev_hash, hash, user_mac
        FROM events
        WHERE 1=1
    """
    params: List[Any] = []

    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        # Review m2-r9 finding 1(a): a lead run token reads only its own project's events.
        project_id = scope_project
    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)
    if task_id:
        query += " AND task_id = ?"
        params.append(task_id)
    if event_type:
        query += " AND type = ?"
        params.append(event_type)
    if since_seq is not None:
        query += " AND seq > ?"
        params.append(int(since_seq))

    caps = ctx.principal.get("capabilities", [])
    if not caps and ctx.principal.get("id"):
        caps = get_agent_capabilities(ctx.conn, ctx.principal["id"])
    if scope_project is None and ctx.actor_kind != "user" and "relay" not in caps and not project_id and not task_id:
        agent_id = ctx.principal.get("id")
        query += """ AND (
            actor_id = ?
            OR task_id IN (SELECT id FROM tasks WHERE assignee_agent_id = ?)
            OR project_id IN (
                SELECT project_id FROM lead_assignments WHERE agent_id = ?
                UNION
                SELECT id FROM projects WHERE origin_principal_id = ?
            )
        )"""
        params.extend([agent_id, agent_id, agent_id, agent_id])

    query += " ORDER BY seq ASC LIMIT ?;"
    params.append(limit)

    cur = ctx.conn.execute(query, params)
    items: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        payload = {}
        try:
            payload = json.loads(r["payload_json"])
        except Exception:
            pass
        items.append({
            "seq": r["seq"],
            "id": r["id"],
            "ts": r["ts"],
            "type": r["type"],
            "project_id": r["project_id"],
            "task_id": r["task_id"],
            "run_id": r["run_id"],
            "approval_id": r["approval_id"],
            "actor_id": r["actor_id"],
            "actor_kind": r["actor_kind"],
            "via": r["via"],
            "relayed_by": r["relayed_by"],
            "credential_id": r["credential_id"],
            "from_state": r["from_state"],
            "to_state": r["to_state"],
            "reason": r["reason"],
            "payload": payload,
            "request_id": r["request_id"],
            "prev_hash": r["prev_hash"],
            "hash": r["hash"],
            "user_mac": r["user_mac"],
        })

    return HandlerResult(data={"items": items})
