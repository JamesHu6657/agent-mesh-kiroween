"""Inbox command handlers per plan §12.2 and §13.3."""

import asyncio
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from mesh.core.authz import check_authz, lead_run_token_scope
from mesh.core.clock import now_iso_utc, parse_duration
from mesh.core.db import transaction
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.errors import MeshError


def _lead_scope_filter(ctx: HandlerContext) -> Tuple[Optional[str], str, List[Any]]:
    """Extra WHERE clause confining a lead run token's inbox to its own project.

    Review m2-r9 finding 1(d): a lead turn sees and changes only rows of
    `scope.project_id` and never `lead_wake` rows — those reach the lead through the
    turn prompt only, so `inbox list` cannot consume them.
    """
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is None:
        return None, "", []
    return scope_project, " AND project_id = ? AND channel <> 'lead_wake'", [scope_project]


def handle_inbox_list(ctx: HandlerContext) -> HandlerResult:
    """List inbox messages for calling principal."""
    if ctx.args.get("user"):
        if ctx.actor_kind != "user" or ctx.via != "direct":
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message="--user requires direct user authentication",
                details={"cmd": "inbox.list", "required": "user_direct"},
            )

    check_authz(ctx.conn, "inbox.list", ctx.principal, ctx.actor_kind, ctx.via)

    principal_id = ctx.principal["id"]
    include_all = bool(ctx.args.get("all", False))
    now = now_iso_utc()
    _scope_project, scope_where, scope_params = _lead_scope_filter(ctx)

    if include_all:
        cur = ctx.conn.execute(
            """SELECT id, event_seq, recipient_id, channel, dedup_key, project_id,
                      type, payload_json, status, created_at, delivered_at, acked_at
               FROM outbox
               WHERE recipient_id = ?""" + scope_where + """
               ORDER BY event_seq ASC, id ASC;""",
            [principal_id] + scope_params,
        )
    else:
        cur = ctx.conn.execute(
            """SELECT id, event_seq, recipient_id, channel, dedup_key, project_id,
                      type, payload_json, status, created_at, delivered_at, acked_at
               FROM outbox
               WHERE recipient_id = ? AND status IN ('pending', 'delivered')""" + scope_where + """
               ORDER BY event_seq ASC, id ASC;""",
            [principal_id] + scope_params,
        )

    rows = cur.fetchall()
    items: List[Dict[str, Any]] = []
    pending_ids: List[str] = []

    for r in rows:
        payload = {}
        try:
            payload = json.loads(r["payload_json"])
        except Exception:
            pass
        items.append({
            "id": r["id"],
            "seq": r["event_seq"],
            "type": r["type"],
            "project_id": r["project_id"],
            "channel": r["channel"],
            "status": r["status"],
            "payload": payload,
            "created_at": r["created_at"],
            "delivered_at": r["delivered_at"],
            "acked_at": r["acked_at"],
        })
        if r["status"] == "pending":
            pending_ids.append(r["id"])

    # Update pending messages to delivered (not for --peek: watchers must not change
    # what the worker itself sees).
    if pending_ids and not ctx.args.get("peek"):
        placeholders = ",".join("?" for _ in pending_ids)
        ctx.conn.execute(
            f"UPDATE outbox SET status = 'delivered', delivered_at = ? WHERE id IN ({placeholders});",
            [now] + pending_ids,
        )

    return HandlerResult(data={"items": items})


def handle_inbox_ack(ctx: HandlerContext) -> HandlerResult:
    """Acknowledge one or more inbox messages (idempotent)."""
    if ctx.args.get("user"):
        if ctx.actor_kind != "user" or ctx.via != "direct":
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message="--user requires direct user authentication",
                details={"cmd": "inbox.ack", "required": "user_direct"},
            )

    check_authz(ctx.conn, "inbox.ack", ctx.principal, ctx.actor_kind, ctx.via)

    principal_id = ctx.principal["id"]
    messages = ctx.args.get("messages") or []
    if isinstance(messages, str):
        messages = [messages]

    if not messages:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="No message identifiers provided to ack",
            details={"param": "messages"},
        )

    now = now_iso_utc()
    scope_project, _scope_where, _scope_params = _lead_scope_filter(ctx)
    for mid in messages:
        cur = ctx.conn.execute(
            "SELECT id, recipient_id, status, project_id, channel FROM outbox WHERE id = ?;",
            (mid,),
        )
        row = cur.fetchone()
        if not row or row["recipient_id"] != principal_id:
            # Do not leak existence
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Message '{mid}' not found",
                details={"message": mid},
            )
        if scope_project is not None and (row["project_id"] != scope_project
                                          or row["channel"] == "lead_wake"):
            # Review m2-r9 finding 1(d): a lead turn cannot ack outside its project,
            # and never a `lead_wake` row (the turn owns those).
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Message '{mid}' not found",
                details={"message": mid},
            )
        if row["status"] != "acked":
            ctx.conn.execute(
                "UPDATE outbox SET status = 'acked', acked_at = ? WHERE id = ?;",
                (now, mid),
            )

    return HandlerResult(data={"acked": len(messages)})


async def handle_inbox_wait(server: Any, ctx: HandlerContext, reader: Any = None) -> HandlerResult:
    """Block waiting for new inbox messages (long-poll) per plan §12.3."""
    if ctx.args.get("user"):
        if ctx.actor_kind != "user" or ctx.via != "direct":
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message="--user requires direct user authentication",
                details={"cmd": "inbox.wait", "required": "user_direct"},
            )

    check_authz(ctx.conn, "inbox.wait", ctx.principal, ctx.actor_kind, ctx.via)

    principal_id = ctx.principal["id"]

    timeout_val = ctx.args.get("timeout")
    if not timeout_val:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--timeout is required for inbox wait",
            details={"param": "timeout"},
        )
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

    max_count = ctx.args.get("max", 1)
    try:
        max_count = int(max_count)
        if max_count <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Max messages must be a positive integer",
            details={"param": "max"},
        )

    start_mono = time.monotonic()
    end_mono = start_mono + timeout_s
    _scope_project, scope_where, scope_params = _lead_scope_filter(ctx)

    server.active_waits = getattr(server, "active_waits", 0) + 1
    try:
        while True:
            if reader is not None and reader.at_eof():
                return HandlerResult(data={"items": []})

            cur = ctx.conn.execute(
                """SELECT id, event_seq, recipient_id, channel, dedup_key, project_id,
                          type, payload_json, status, created_at, delivered_at, acked_at
                   FROM outbox
                   WHERE recipient_id = ? AND status IN ('pending', 'delivered')"""
                + scope_where + """
                   ORDER BY event_seq ASC, id ASC
                   LIMIT ?;""",
                [principal_id] + scope_params + [max_count],
            )
            rows = cur.fetchall()
            if rows:
                if reader is not None and reader.at_eof():
                    return HandlerResult(data={"items": []})

                items: List[Dict[str, Any]] = []
                pending_ids: List[str] = []
                now = now_iso_utc()
                for r in rows:
                    payload = {}
                    try:
                        payload = json.loads(r["payload_json"])
                    except Exception:
                        pass
                    items.append({
                        "id": r["id"],
                        "seq": r["event_seq"],
                        "type": r["type"],
                        "project_id": r["project_id"],
                        "channel": r["channel"],
                        "status": r["status"],
                        "payload": payload,
                        "created_at": r["created_at"],
                        "delivered_at": r["delivered_at"],
                        "acked_at": r["acked_at"],
                    })
                    if r["status"] == "pending":
                        pending_ids.append(r["id"])

                if pending_ids:
                    placeholders = ",".join("?" for _ in pending_ids)
                    with transaction(ctx.conn):
                        ctx.conn.execute(
                            f"UPDATE outbox SET status = 'delivered', delivered_at = ? WHERE id IN ({placeholders});",
                            [now] + pending_ids,
                        )

                return HandlerResult(data={"items": items})

            remaining = end_mono - time.monotonic()
            if remaining <= 0:
                raise MeshError(
                    code=c.WAIT_TIMEOUT,
                    exit_code=c.EXIT_WAIT_TIMEOUT,
                    message=f"Inbox wait timed out after {timeout_s}s",
                    details={},
                    data={"items": []},
                )

            async with server.notify_cond:
                try:
                    await asyncio.wait_for(server.notify_cond.wait(), timeout=min(remaining, 0.5))
                except asyncio.TimeoutError:
                    pass

            if reader is not None and reader.at_eof():
                return HandlerResult(data={"items": []})
    finally:
        server.active_waits = max(0, getattr(server, "active_waits", 1) - 1)
