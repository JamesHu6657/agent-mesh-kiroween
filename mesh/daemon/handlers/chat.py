"""Chat command handlers: chat.send, chat.list, chat.reply.

Chat text NEVER approves, rejects, or retracts any approval or plan.
All approvals happen strictly via inline approval card buttons (`approval.decide`)
authenticated by a direct user session.
"""

import json
import sqlite3
from typing import Any, Dict, List, Optional

import mesh.constants as c
from mesh.core.approvals import get_row_approval_policy
from mesh.core.authz import (
    check_authz,
    check_relayed_chat_send_scope,
    is_acting_project_lead,
    lead_run_token_scope,
)
from mesh.core.clock import now_iso_utc
from mesh.core.events import record_event
from mesh.core.grants import (
    DEFAULT_PERMISSION_MODE,
    PERMISSION_MODES,
    compute_approval_card_actions,
)
from mesh.core.ids import generate_id
from mesh.core.redact import redact, redact_value
from mesh.daemon.handlers.base import Effect, HandlerContext, HandlerResult
from mesh.errors import MeshError

MAX_CHAT_TEXT_LEN = 16000
VALID_REPLY_KINDS = ("text", "clarification", "dispatch_notice")
ALLOWED_CHAT_SEND_ARGS = frozenset({
    "project_id",
    "project",
    "text",
    "reply_to_msg_id",
    # Auto-project creation arguments (direct user when project_id is absent/null)
    "title",
    "lead",
    "repo",
    "base_branch",
    "test_command",
    "criterion",
    "criteria",
    "origin_ref",
    "permission_mode",
})


def _require_chat_text(raw: Any, param: str = "text") -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Field '{param}' is required and must be non-empty",
            details={"param": param},
        )
    if len(raw) > MAX_CHAT_TEXT_LEN:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Field '{param}' exceeds {MAX_CHAT_TEXT_LEN} character limit",
            details={"param": param, "max_length": MAX_CHAT_TEXT_LEN, "actual_length": len(raw)},
        )
    return raw


def _format_chat_message(row: sqlite3.Row, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if row["meta_json"]:
        try:
            parsed = json.loads(row["meta_json"])
            if isinstance(parsed, dict):
                meta = parsed
        except (TypeError, ValueError):
            meta = {}
    is_relayed = row["via"] == "relayed"
    relayed_by = meta.get("relayed_by") or (row["actor_id"] if is_relayed else None)
    relayed_by_name = meta.get("relayed_by_name")
    if is_relayed and not relayed_by_name and conn is not None and relayed_by:
        prow = conn.execute("SELECT name FROM principals WHERE id = ?;", (relayed_by,)).fetchone()
        if prow is not None:
            relayed_by_name = prow["name"]
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "event_seq": row["event_seq"],
        "role": row["role"],
        "kind": row["kind"],
        "actor_id": row["actor_id"],
        "actor_kind": row["actor_kind"],
        "via": row["via"],
        "relayed": is_relayed,
        "relayed_by": relayed_by,
        "relayed_by_name": relayed_by_name,
        "run_id": row["run_id"],
        "plan_id": row["plan_id"],
        "approval_id": row["approval_id"],
        "task_id": row["task_id"],
        "body": redact(row["body"]) if isinstance(row["body"], str) else row["body"],
        "meta": redact_value(meta),
        "created_at": row["created_at"],
    }


def _build_lead_status(conn: sqlite3.Connection, project_id: str) -> Dict[str, Any]:
    """Return authoritative read-only delivery & turn status for the project's current lead."""
    from mesh.daemon.lead_turns import vendor_leads_enabled

    prow = conn.execute(
        """SELECT la.agent_id, pr.name AS agent_name, pr.disabled_at, a.adapter, a.delivery
           FROM lead_assignments la
           JOIN principals pr ON pr.id = la.agent_id
           JOIN agents a ON a.id = la.agent_id
           WHERE la.project_id = ? AND la.ended_at IS NULL
           LIMIT 1;""",
        (project_id,),
    ).fetchone()

    active_run_row = conn.execute(
        """SELECT r.id, r.agent_id, p.name AS agent_name, r.status, r.started_at
           FROM runs r LEFT JOIN principals p ON r.agent_id = p.id
           WHERE r.project_id = ? AND r.kind = 'lead_turn' AND r.status IN ('starting', 'running')
           ORDER BY r.started_at DESC LIMIT 1;""",
        (project_id,),
    ).fetchone()
    active_lead_run = dict(active_run_row) if active_run_row else None

    lead_agent_id = prow["agent_id"] if prow else None
    queued_count = 0
    delivering_count = 0
    oldest_queued_at: Optional[str] = None
    if lead_agent_id:
        ob_rows = conn.execute(
            """SELECT status, COUNT(*) AS cnt, MIN(created_at) AS oldest
               FROM outbox
               WHERE project_id = ? AND recipient_id = ? AND status IN ('pending', 'delivering')
               GROUP BY status;""",
            (project_id, lead_agent_id),
        ).fetchall()
        for r in ob_rows:
            if r["status"] == "pending":
                queued_count = int(r["cnt"] or 0)
                oldest_queued_at = r["oldest"]
            elif r["status"] == "delivering":
                delivering_count = int(r["cnt"] or 0)

    return {
        "agent_id": lead_agent_id,
        "agent_name": prow["agent_name"] if prow else None,
        "adapter": prow["adapter"] if prow else None,
        "delivery": prow["delivery"] if prow else None,
        "disabled": bool(prow["disabled_at"]) if prow else False,
        "vendor_leads_enabled": vendor_leads_enabled(conn),
        "queued_outbox_count": queued_count,
        "delivering_outbox_count": delivering_count,
        "oldest_queued_at": oldest_queued_at,
        "active_run": active_lead_run,
    }


def handle_chat_send(ctx: HandlerContext) -> HandlerResult:
    """Send a user chat message on a project. Never decides approvals or plans."""
    if ctx.actor_kind != "user" and ctx.via != "relayed":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Command 'chat.send' requires direct user authentication (agents use 'chat.reply')",
            details={"cmd": "chat.send", "via": ctx.via},
        )

    unknown_keys = sorted(set(ctx.args.keys()) - ALLOWED_CHAT_SEND_ARGS)
    if unknown_keys:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Unknown argument(s) for 'chat.send': {', '.join(unknown_keys)}",
            details={"unknown_args": unknown_keys},
        )

    raw_text = _require_chat_text(ctx.args.get("text"), "text")
    body = redact(raw_text)
    now = now_iso_utc()

    has_proj_id = "project_id" in ctx.args and ctx.args["project_id"] is not None
    has_proj = "project" in ctx.args and ctx.args["project"] is not None
    if has_proj_id and has_proj and ctx.args["project_id"] != ctx.args["project"]:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Conflicting 'project_id' and 'project' arguments",
            details={"param": "project_id"},
        )
    if has_proj_id or has_proj:
        raw_proj = ctx.args["project_id"] if has_proj_id else ctx.args["project"]
        if not isinstance(raw_proj, str) or not raw_proj.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'project_id' must be a non-empty string (omit or pass null to create a new project)",
                details={"param": "project_id"},
            )
    else:
        raw_proj = None

    created_project: Optional[Dict[str, Any]] = None
    if raw_proj is None:
        if ctx.actor_kind != "user" or ctx.via != "direct":
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'project_id' is required",
                details={"param": "project_id"},
            )
        from mesh.daemon.handlers.project import handle_intake_submit

        first_line = raw_text.strip().splitlines()[0].strip()
        derived_title = str(ctx.args.get("title") or first_line[:40] or "New Chat Project").strip()
        intake_args: Dict[str, Any] = {
            "title": derived_title,
            "goal": raw_text.strip(),
            "plan_gate_mode": "require_approval",
            "permission_mode": ctx.args.get("permission_mode") or DEFAULT_PERMISSION_MODE,
        }
        for opt in ("lead", "repo", "base_branch", "test_command", "criterion", "criteria", "origin_ref"):
            if ctx.args.get(opt) is not None:
                intake_args[opt] = ctx.args[opt]
        sub_ctx = HandlerContext(
            conn=ctx.conn,
            principal=ctx.principal,
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            request_id=ctx.request_id,
            home=ctx.home,
            cmd="intake.submit",
            args=intake_args,
            dry_run=ctx.dry_run,
            k_user=ctx.k_user,
            effects=ctx.effects,
            testing_mode=ctx.testing_mode,
            credential_id=ctx.credential_id,
        )
        intake_res = handle_intake_submit(sub_ctx)
        created_project = intake_res.data
        project_id = created_project["id"]
    else:
        project_id = raw_proj.strip()
        check_authz(ctx.conn, "chat.send", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    prow = ctx.conn.execute(
        "SELECT id, status, plan_gate_mode, permission_mode, origin_principal_id FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    if prow is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )
    if ctx.via == "relayed":
        check_relayed_chat_send_scope(ctx.conn, ctx.principal, project_id)
    if prow["status"] != "active":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {prow['status']}, not active",
            details={"project_id": project_id, "status": prow["status"]},
        )

    reply_to = ctx.args.get("reply_to_msg_id")
    if reply_to is not None:
        if not isinstance(reply_to, str) or not reply_to.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'reply_to_msg_id' must be a non-empty string",
                details={"param": "reply_to_msg_id"},
            )
        reply_to_row = ctx.conn.execute(
            "SELECT id FROM chat_messages WHERE id = ? AND project_id = ?;",
            (reply_to, project_id),
        ).fetchone()
        if reply_to_row is None:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Reply target message '{reply_to}' not found in project '{project_id}'",
                details={"reply_to_msg_id": reply_to, "project_id": project_id},
            )

    cmsg_id = generate_id("cmsg")
    ev_payload: Dict[str, Any] = {
        "message_id": cmsg_id,
        "project_id": project_id,
        "role": "user",
        "kind": "text",
        "body": body,
        "priority": "chat",
    }
    if reply_to:
        ev_payload["reply_to_msg_id"] = reply_to
    if ctx.via == "relayed":
        ev_payload["relayed"] = True
        ev_payload["relayed_by"] = ctx.principal["id"]
        ev_payload["relayed_by_name"] = ctx.principal.get("name")

    event = record_event(
        conn=ctx.conn,
        type_="chat.message",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        credential_id=ctx.credential_id,
        project_id=project_id,
        payload=ev_payload,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    meta: Dict[str, Any] = {}
    if reply_to:
        meta["reply_to_msg_id"] = reply_to
    if ctx.via == "relayed":
        meta["relayed"] = True
        meta["relayed_by"] = ctx.principal["id"]
        meta["relayed_by_name"] = ctx.principal.get("name")

    ctx.conn.execute(
        """INSERT INTO chat_messages (
            id, project_id, event_seq, role, kind, actor_id, actor_kind, via,
            run_id, plan_id, approval_id, task_id, body, meta_json,
            consumed_by_approval_id, created_at
        ) VALUES (?, ?, ?, 'user', 'text', ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, NULL, ?);""",
        (
            cmsg_id,
            project_id,
            event["seq"],
            ctx.principal["id"],
            ctx.actor_kind,
            ctx.via,
            body,
            json.dumps(meta, sort_keys=True, ensure_ascii=False),
            now,
        ),
    )

    msg_row = ctx.conn.execute("SELECT * FROM chat_messages WHERE id = ?;", (cmsg_id,)).fetchone()
    lead_status = _build_lead_status(ctx.conn, project_id)
    resp_data: Dict[str, Any] = {
        "project_id": project_id,
        "message": _format_chat_message(msg_row, ctx.conn),
        "lead_status": lead_status,
    }
    if created_project is not None:
        resp_data["project"] = created_project
    return HandlerResult(data=resp_data, events=["chat.message"])


def handle_chat_reply(ctx: HandlerContext) -> HandlerResult:
    """Emit a visible Lead reply into the project chat stream (never wakes the lead again)."""
    project_id = ctx.args.get("project_id") or ctx.args.get("project")
    if not isinstance(project_id, str) or not project_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'project_id' is required",
            details={"param": "project_id"},
        )
    project_id = project_id.strip()

    check_authz(ctx.conn, "chat.reply", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    if ctx.actor_kind == "user":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Command 'chat.reply' is for project leads only (users use 'chat.send')",
            details={"cmd": "chat.reply", "use": "chat.send", "actor_kind": ctx.actor_kind},
        )
    if not is_acting_project_lead(ctx.conn, project_id, ctx.principal["id"]):
        raise MeshError(
            code=c.NOT_PROJECT_LEAD,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Agent '{ctx.principal.get('name')}' is not the current lead of project '{project_id}'",
            details={"project_id": project_id, "agent": ctx.principal.get("name")},
        )

    prow = ctx.conn.execute("SELECT id, status FROM projects WHERE id = ?;", (project_id,)).fetchone()
    if prow is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )
    if prow["status"] == "cancelled":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is cancelled",
            details={"project_id": project_id, "status": prow["status"]},
        )

    raw_text = _require_chat_text(ctx.args.get("text"), "text")
    body = redact(raw_text)

    kind = ctx.args.get("kind") or "text"
    if not isinstance(kind, str) or kind not in VALID_REPLY_KINDS:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Field 'kind' must be one of {list(VALID_REPLY_KINDS)}",
            details={"param": "kind", "expected": list(VALID_REPLY_KINDS), "got": kind},
        )

    reply_to = ctx.args.get("reply_to_msg_id")
    if reply_to is not None:
        if not isinstance(reply_to, str) or not reply_to.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'reply_to_msg_id' must be a non-empty string",
                details={"param": "reply_to_msg_id"},
            )
        rrow = ctx.conn.execute(
            "SELECT id FROM chat_messages WHERE id = ? AND project_id = ?;",
            (reply_to, project_id),
        ).fetchone()
        if rrow is None:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Reply target message '{reply_to}' not found in project '{project_id}'",
                details={"reply_to_msg_id": reply_to, "project_id": project_id},
            )

    plan_id = ctx.args.get("plan_id")
    if plan_id is not None:
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'plan_id' must be a non-empty string",
                details={"param": "plan_id"},
            )
        plrow = ctx.conn.execute(
            "SELECT id FROM plans WHERE id = ? AND project_id = ?;",
            (plan_id, project_id),
        ).fetchone()
        if plrow is None:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Plan '{plan_id}' not found in project '{project_id}'",
                details={"plan_id": plan_id, "project_id": project_id},
            )

    run_id: Optional[str] = None
    if ctx.via == "run_token":
        scope = ctx.principal.get("scope") or {}
        if isinstance(scope.get("run_id"), str) and scope["run_id"]:
            run_id = scope["run_id"]

    now = now_iso_utc()
    cmsg_id = generate_id("cmsg")
    ev_payload: Dict[str, Any] = {
        "message_id": cmsg_id,
        "project_id": project_id,
        "role": "lead",
        "kind": kind,
        "body": body,
    }
    if reply_to:
        ev_payload["reply_to_msg_id"] = reply_to
    if plan_id:
        ev_payload["plan_id"] = plan_id

    event = record_event(
        conn=ctx.conn,
        type_="chat.message",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        credential_id=ctx.credential_id,
        project_id=project_id,
        run_id=run_id,
        payload=ev_payload,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    meta: Dict[str, Any] = {}
    if reply_to:
        meta["reply_to_msg_id"] = reply_to

    ctx.conn.execute(
        """INSERT INTO chat_messages (
            id, project_id, event_seq, role, kind, actor_id, actor_kind, via,
            run_id, plan_id, approval_id, task_id, body, meta_json,
            consumed_by_approval_id, created_at
        ) VALUES (?, ?, ?, 'lead', ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, ?);""",
        (
            cmsg_id,
            project_id,
            event["seq"],
            kind,
            ctx.principal["id"],
            ctx.actor_kind,
            ctx.via,
            run_id,
            plan_id,
            body,
            json.dumps(meta, sort_keys=True, ensure_ascii=False),
            now,
        ),
    )

    msg_row = ctx.conn.execute("SELECT * FROM chat_messages WHERE id = ?;", (cmsg_id,)).fetchone()
    return HandlerResult(
        data={"project_id": project_id, "message": _format_chat_message(msg_row, ctx.conn)},
        events=["chat.message"],
    )


def handle_chat_list(ctx: HandlerContext) -> HandlerResult:
    """List paginated project chat history with embedded plan and approval card summaries."""
    project_id = ctx.args.get("project_id") or ctx.args.get("project")
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if not project_id and scope_project:
        project_id = scope_project
    if not isinstance(project_id, str) or not project_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'project_id' is required",
            details={"param": "project_id"},
        )
    project_id = project_id.strip()

    check_authz(ctx.conn, "chat.list", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    prow = ctx.conn.execute(
        "SELECT id, status, plan_gate_mode, permission_mode FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    if prow is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    limit = ctx.args.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 200):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'limit' must be an integer between 1 and 200",
            details={"param": "limit", "expected": "1..200", "got": limit},
        )

    before_seq = ctx.args.get("before_seq")
    after_seq = ctx.args.get("after_seq")
    for pname, pval in (("before_seq", before_seq), ("after_seq", after_seq)):
        if pval is not None and (isinstance(pval, bool) or not isinstance(pval, int) or pval < 0):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Field '{pname}' must be a non-negative integer",
                details={"param": pname, "got": pval},
            )

    where = ["project_id = ?"]
    params: List[Any] = [project_id]
    if before_seq is not None:
        where.append("event_seq < ?")
        params.append(before_seq)
    if after_seq is not None:
        where.append("event_seq > ?")
        params.append(after_seq)

    where_sql = " AND ".join(where)
    if after_seq is not None and before_seq is None:
        rows = ctx.conn.execute(
            f"SELECT * FROM chat_messages WHERE {where_sql} ORDER BY event_seq ASC LIMIT ?;",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
    else:
        rows = ctx.conn.execute(
            f"SELECT * FROM chat_messages WHERE {where_sql} ORDER BY event_seq DESC LIMIT ?;",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = list(reversed(rows[:limit]))

    items = [_format_chat_message(r, ctx.conn) for r in rows]

    from mesh.daemon.handlers.plan import format_plan_summary

    now = now_iso_utc()
    plan_ids = sorted({it["plan_id"] for it in items if it.get("plan_id")})
    plans_by_id: Dict[str, Any] = {}
    for pid in plan_ids:
        plrow = ctx.conn.execute("SELECT * FROM plans WHERE id = ?;", (pid,)).fetchone()
        if plrow is not None:
            plans_by_id[pid] = format_plan_summary(
                ctx.conn, plrow, now=now, testing_mode=ctx.testing_mode
            )

    apr_ids = sorted(
        {it["approval_id"] for it in items if it.get("approval_id")}
        | {p["approval_id"] for p in plans_by_id.values() if p.get("approval_id")}
    )
    apr_rows_by_id: Dict[str, Any] = {}
    if apr_ids:
        placeholders = ",".join("?" for _ in apr_ids)
        for arow in ctx.conn.execute(
            f"SELECT * FROM approvals WHERE id IN ({placeholders});",
            tuple(apr_ids),
        ).fetchall():
            apr_rows_by_id[arow["id"]] = arow

    task_ids = sorted({r["task_id"] for r in apr_rows_by_id.values() if r["task_id"]})
    prefetched_tasks: Dict[str, Any] = {}
    if task_ids:
        t_placeholders = ",".join("?" for _ in task_ids)
        for trow in ctx.conn.execute(
            f"SELECT id, status, kind FROM tasks WHERE id IN ({t_placeholders});",
            tuple(task_ids),
        ).fetchall():
            prefetched_tasks[trow["id"]] = trow

    from mesh.core.grants import serialize_approval_card_summary

    approvals_by_id: Dict[str, Any] = {}
    for aid in apr_ids:
        arow = apr_rows_by_id.get(aid)
        if arow is not None:
            _, policy = get_row_approval_policy(ctx.conn, arow)
            cat = arow["category"]
            pl_match = (
                next((p for p in plans_by_id.values() if p.get("approval_id") == aid), None)
                if cat == "plan_dispatch"
                else None
            )
            card_ui = compute_approval_card_actions(
                ctx.conn,
                arow,
                now=now,
                testing_mode=ctx.testing_mode,
                plan_status=pl_match["status"] if pl_match is not None else None,
                prefetched_project=prow,
                prefetched_tasks=prefetched_tasks,
            )
            approvals_by_id[aid] = serialize_approval_card_summary(
                arow,
                policy=policy,
                card_ui=card_ui,
                plan_meta=pl_match,
            )

    # Hydrate live meta.actions on card messages from compute_approval_card_actions so
    # item.meta.actions never diverges from approvals_by_id[aid].available_actions.
    for it in items:
        if it.get("kind") in ("plan_card", "approval_card", "escalation_card"):
            aid = it.get("approval_id")
            if aid and aid in approvals_by_id:
                it["meta"]["actions"] = list(approvals_by_id[aid]["available_actions"])
            else:
                it["meta"]["actions"] = []

    lead_status = _build_lead_status(ctx.conn, project_id)
    active_lead_run = lead_status["active_run"]

    # B2: Identify unconsumed, non-approval direct-user queued messages eligible for edit/cancel
    queued_message_ids: List[str] = []
    if items:
        pending_seqs = {
            int(r["event_seq"])
            for r in ctx.conn.execute(
                """SELECT DISTINCT event_seq FROM outbox
                   WHERE project_id = ? AND channel IN ('lead_wake', 'inbox')
                     AND type = 'chat.message'
                     AND status = 'pending' AND delivering_run_id IS NULL;""",
                (project_id,),
            ).fetchall()
        }
        consumed_seqs = {
            int(r["event_seq"])
            for r in ctx.conn.execute(
                """SELECT DISTINCT event_seq FROM outbox
                   WHERE project_id = ? AND channel IN ('lead_wake', 'inbox')
                     AND type = 'chat.message'
                     AND (status != 'pending' OR delivering_run_id IS NOT NULL);""",
                (project_id,),
            ).fetchall()
        }
        for raw_row, item in zip(rows, items):
            if (
                raw_row["role"] == "user"
                and raw_row["kind"] == "text"
                and raw_row["via"] == "direct"
                and raw_row["consumed_by_approval_id"] is None
                and raw_row["approval_id"] is None
                and raw_row["plan_id"] is None
                and raw_row["task_id"] is None
                and not (isinstance(item.get("meta"), dict) and item["meta"].get("queued_cancelled"))
                and int(raw_row["event_seq"]) in pending_seqs
                and int(raw_row["event_seq"]) not in consumed_seqs
            ):
                queued_message_ids.append(item["id"])

    pmode = prow["permission_mode"] if "permission_mode" in prow.keys() and prow["permission_mode"] in PERMISSION_MODES else DEFAULT_PERMISSION_MODE
    return HandlerResult(
        data={
            "project_id": project_id,
            "project_status": prow["status"],
            "plan_gate_mode": prow["plan_gate_mode"],
            "permission_mode": pmode,
            "items": items,
            "has_more": has_more,
            "plans_by_id": plans_by_id,
            "approvals_by_id": approvals_by_id,
            "active_lead_run": active_lead_run,
            "lead_status": lead_status,
            "queued_message_ids": queued_message_ids,
        }
    )


def _require_direct_user(ctx: HandlerContext, cmd: str) -> None:
    if not (ctx.actor_kind == "user" and ctx.via == "direct"):
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Command '{cmd}' requires direct user authentication",
            details={"cmd": cmd, "required": "user_direct"},
        )


def _load_mutable_queued_message(
    conn: sqlite3.Connection,
    project_id: str,
    message_id: str,
) -> tuple[sqlite3.Row, Dict[str, Any], List[sqlite3.Row]]:
    """Return ``(chat_row, meta, pending_outbox_rows)`` ONLY for unconsumed non-approval queued user messages."""
    row = conn.execute(
        "SELECT * FROM chat_messages WHERE id = ? AND project_id = ?;",
        (message_id, project_id),
    ).fetchone()
    if row is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Chat message '{message_id}' not found in project '{project_id}'",
            details={"message_id": message_id, "project_id": project_id},
        )
    if row["role"] != "user" or row["kind"] != "text":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message="Only queued user text messages can be edited or cancelled",
            details={"message_id": message_id, "reason": "not_user_text_message"},
        )
    if row["via"] != "direct":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message="Relayed chat messages cannot be edited or cancelled",
            details={"message_id": message_id, "reason": "relayed_message_immutable"},
        )
    if (
        row["consumed_by_approval_id"] is not None
        or row["approval_id"] is not None
        or row["plan_id"] is not None
        or row["task_id"] is not None
    ):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message="Approval-bound or plan-bound messages cannot be modified or cancelled",
            details={"message_id": message_id, "reason": "approval_bound_message"},
        )
    meta: Dict[str, Any] = {}
    if row["meta_json"]:
        try:
            parsed = json.loads(row["meta_json"])
            if isinstance(parsed, dict):
                meta = parsed
        except (TypeError, ValueError):
            meta = {}
    if meta.get("queued_cancelled"):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Queued message '{message_id}' was already cancelled",
            details={"message_id": message_id, "reason": "message_already_cancelled"},
        )

    outbox_rows = conn.execute(
        """SELECT * FROM outbox
           WHERE project_id = ? AND event_seq = ? AND channel IN ('lead_wake', 'inbox')
             AND type = 'chat.message'
           ORDER BY id ASC;""",
        (project_id, row["event_seq"]),
    ).fetchall()
    if not outbox_rows:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message="Message is no longer queued for delivery",
            details={"message_id": message_id, "reason": "message_already_consumed"},
        )
    if any(ob["status"] != "pending" or ob["delivering_run_id"] is not None for ob in outbox_rows):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message="Message has already been picked up or consumed by the Lead",
            details={"message_id": message_id, "reason": "message_already_consumed"},
        )
    return row, meta, list(outbox_rows)


def handle_chat_queue_cancel(ctx: HandlerContext) -> HandlerResult:
    """Cancel an unconsumed non-approval queued follow-up user message (audit-preserving)."""
    _require_direct_user(ctx, "chat.queue_cancel")
    project_id = ctx.args.get("project_id") or ctx.args.get("project")
    message_id = ctx.args.get("message_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'project_id' is required",
            details={"param": "project_id"},
        )
    if not isinstance(message_id, str) or not message_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'message_id' is required",
            details={"param": "message_id"},
        )
    pid = project_id.strip()
    mid = message_id.strip()
    row, meta, outbox_rows = _load_mutable_queued_message(ctx.conn, pid, mid)

    now = now_iso_utc()
    outbox_ids = [ob["id"] for ob in outbox_rows]
    for oid in outbox_ids:
        ctx.conn.execute(
            """UPDATE outbox
               SET status = 'superseded', acked_at = ?, last_error = 'cancelled_by_user'
               WHERE id = ? AND status = 'pending' AND delivering_run_id IS NULL;""",
            (now, oid),
        )
    meta["queued_cancelled"] = True
    meta["cancelled_at"] = now
    ctx.conn.execute(
        "UPDATE chat_messages SET meta_json = ? WHERE id = ?;",
        (json.dumps(meta, sort_keys=True, ensure_ascii=False), mid),
    )
    record_event(
        conn=ctx.conn,
        type_="chat.cancelled",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        credential_id=ctx.credential_id,
        project_id=pid,
        payload={
            "message_id": mid,
            "project_id": pid,
            "original_event_seq": row["event_seq"],
            "outbox_ids": outbox_ids,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    updated = ctx.conn.execute("SELECT * FROM chat_messages WHERE id = ?;", (mid,)).fetchone()
    return HandlerResult(
        data={
            "project_id": pid,
            "message_id": mid,
            "cancelled": True,
            "message": _format_chat_message(updated, ctx.conn),
            "lead_status": _build_lead_status(ctx.conn, pid),
        },
        events=["chat.cancelled"],
    )


def handle_chat_queue_edit(ctx: HandlerContext) -> HandlerResult:
    """Edit an unconsumed non-approval queued follow-up user message (audit-preserving)."""
    _require_direct_user(ctx, "chat.queue_edit")
    project_id = ctx.args.get("project_id") or ctx.args.get("project")
    message_id = ctx.args.get("message_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'project_id' is required",
            details={"param": "project_id"},
        )
    if not isinstance(message_id, str) or not message_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'message_id' is required",
            details={"param": "message_id"},
        )
    raw_text = _require_chat_text(ctx.args.get("text"), "text")
    new_body = redact(raw_text)
    pid = project_id.strip()
    mid = message_id.strip()
    row, meta, outbox_rows = _load_mutable_queued_message(ctx.conn, pid, mid)

    now = now_iso_utc()
    outbox_ids: List[str] = []
    for ob in outbox_rows:
        outbox_ids.append(ob["id"])
        try:
            ob_payload = json.loads(ob["payload_json"] or "{}")
            if not isinstance(ob_payload, dict):
                ob_payload = {}
        except (TypeError, ValueError):
            ob_payload = {}
        ob_payload["body"] = new_body
        ob_payload["edited_at"] = now
        ctx.conn.execute(
            """UPDATE outbox SET payload_json = ?
               WHERE id = ? AND status = 'pending' AND delivering_run_id IS NULL;""",
            (json.dumps(redact_value(ob_payload), sort_keys=True, separators=(",", ":")), ob["id"]),
        )

    meta["edited_at"] = now
    meta["edit_count"] = int(meta.get("edit_count") or 0) + 1
    ctx.conn.execute(
        "UPDATE chat_messages SET body = ?, meta_json = ? WHERE id = ?;",
        (new_body, json.dumps(meta, sort_keys=True, ensure_ascii=False), mid),
    )
    record_event(
        conn=ctx.conn,
        type_="chat.edited",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        credential_id=ctx.credential_id,
        project_id=pid,
        payload={
            "message_id": mid,
            "project_id": pid,
            "original_event_seq": row["event_seq"],
            "body": new_body,
            "edit_count": meta["edit_count"],
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    updated = ctx.conn.execute("SELECT * FROM chat_messages WHERE id = ?;", (mid,)).fetchone()
    return HandlerResult(
        data={
            "project_id": pid,
            "message_id": mid,
            "edited": True,
            "message": _format_chat_message(updated, ctx.conn),
            "lead_status": _build_lead_status(ctx.conn, pid),
        },
        events=["chat.edited"],
    )


def handle_lead_interrupt(ctx: HandlerContext) -> HandlerResult:
    """Interrupt an active read-only ``lead_turn`` run (and optionally steer with a new message).

    Direct user only. Never touches worker ``task`` or ``merge_test`` runs, never
    cancels committed ``plan.dispatch`` work, and never mutates approval audit records.
    """
    from mesh.daemon.lead_turns import lead_interrupt_meta_key

    _require_direct_user(ctx, "lead.interrupt")
    project_id = ctx.args.get("project_id") or ctx.args.get("project")
    run_id_arg = ctx.args.get("run_id")
    raw_steer = ctx.args.get("steer_text")
    raw_reason = ctx.args.get("reason")

    if run_id_arg is not None and (not isinstance(run_id_arg, str) or not run_id_arg.strip()):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'run_id' must be a non-empty string",
            details={"param": "run_id"},
        )
    if raw_steer is not None:
        steer_text: Optional[str] = _require_chat_text(raw_steer, "steer_text")
    else:
        steer_text = None

    reason = redact(str(raw_reason).strip())[:200] if isinstance(raw_reason, str) and raw_reason.strip() else "user_interrupted"

    run_row: Optional[sqlite3.Row] = None
    if run_id_arg:
        run_row = ctx.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id_arg.strip(),)).fetchone()
        if run_row is None:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Run '{run_id_arg}' not found",
                details={"run_id": run_id_arg},
            )
        if run_row["kind"] != "lead_turn":
            raise MeshError(
                code=c.INVALID_TRANSITION,
                exit_code=c.EXIT_CONFLICT,
                message=f"Run '{run_row['id']}' is kind '{run_row['kind']}', not a lead_turn",
                details={"run_id": run_row["id"], "kind": run_row["kind"], "reason": "not_lead_turn"},
            )
        if project_id and str(project_id).strip() != run_row["project_id"]:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Provided 'project_id' does not match run's project",
                details={"project_id": project_id, "run_project_id": run_row["project_id"]},
            )
        project_id = run_row["project_id"]

    if not isinstance(project_id, str) or not project_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'project_id' is required",
            details={"param": "project_id"},
        )
    pid = project_id.strip()

    prow = ctx.conn.execute("SELECT id, status FROM projects WHERE id = ?;", (pid,)).fetchone()
    if prow is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{pid}' not found",
            details={"project_id": pid},
        )
    if steer_text is not None and prow["status"] != "active":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{pid}' is {prow['status']}, not active",
            details={"project_id": pid, "status": prow["status"]},
        )

    if run_row is None:
        run_row = ctx.conn.execute(
            """SELECT * FROM runs
               WHERE project_id = ? AND kind = 'lead_turn' AND status IN ('starting', 'running')
               ORDER BY started_at DESC, id DESC LIMIT 1;""",
            (pid,),
        ).fetchone()
    elif run_row["status"] not in ("starting", "running"):
        run_row = None

    now = now_iso_utc()
    interrupted = False
    interrupted_run_id: Optional[str] = None
    events: List[str] = []

    if run_row is not None:
        interrupted = True
        interrupted_run_id = run_row["id"]
        meta_val = json.dumps(
            {
                "requested_at": now,
                "actor": ctx.principal["id"],
                "reason": reason,
                "steer": steer_text is not None,
            },
            sort_keys=True,
        )
        ctx.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?);",
            (lead_interrupt_meta_key(interrupted_run_id), meta_val),
        )
        record_event(
            conn=ctx.conn,
            type_="lead.interrupted",
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            credential_id=ctx.credential_id,
            project_id=pid,
            run_id=interrupted_run_id,
            reason=reason,
            payload={
                "run_id": interrupted_run_id,
                "project_id": pid,
                "reason": reason,
                "steer": steer_text is not None,
            },
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
        events.append("lead.interrupted")
        ctx.effects.append(
            Effect("terminate_lead_turn", {"run_id": interrupted_run_id, "end_reason": "user_interrupted"})
        )

    steer_message: Optional[Dict[str, Any]] = None
    if steer_text is not None:
        body = redact(steer_text)
        cmsg_id = generate_id("cmsg")
        ev_payload: Dict[str, Any] = {
            "message_id": cmsg_id,
            "project_id": pid,
            "role": "user",
            "kind": "text",
            "body": body,
            "priority": "chat",
            "steer": True,
        }
        if interrupted_run_id:
            ev_payload["steered_from_run_id"] = interrupted_run_id
        ev = record_event(
            conn=ctx.conn,
            type_="chat.message",
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            credential_id=ctx.credential_id,
            project_id=pid,
            payload=ev_payload,
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
        meta: Dict[str, Any] = {"steer": True}
        if interrupted_run_id:
            meta["steered_from_run_id"] = interrupted_run_id
        ctx.conn.execute(
            """INSERT INTO chat_messages (
                id, project_id, event_seq, role, kind, actor_id, actor_kind, via,
                run_id, plan_id, approval_id, task_id, body, meta_json,
                consumed_by_approval_id, created_at
            ) VALUES (?, ?, ?, 'user', 'text', ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, NULL, ?);""",
            (
                cmsg_id,
                pid,
                ev["seq"],
                ctx.principal["id"],
                ctx.actor_kind,
                ctx.via,
                body,
                json.dumps(meta, sort_keys=True, ensure_ascii=False),
                now,
            ),
        )
        mrow = ctx.conn.execute("SELECT * FROM chat_messages WHERE id = ?;", (cmsg_id,)).fetchone()
        steer_message = _format_chat_message(mrow, ctx.conn)
        events.append("chat.message")

    resp: Dict[str, Any] = {
        "project_id": pid,
        "interrupted": interrupted,
        "run_id": interrupted_run_id,
        "reason": reason if interrupted else "no_active_lead_turn",
        "steer": steer_text is not None,
        "lead_status": _build_lead_status(ctx.conn, pid),
    }
    if steer_message is not None:
        resp["message"] = steer_message
    return HandlerResult(data=resp, events=events)
