"""Plan lifecycle command handlers: plan.propose, plan.dispatch, plan.cancel, plan.list, plan.show."""

import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

import mesh.constants as c
from mesh.core.approvals import (
    check_and_consume_approval,
    create_or_get_pending_approval,
    get_row_approval_policy,
)
from mesh.core.authz import (
    check_authz,
    is_acting_project_lead,
    lead_run_token_scope,
)
from mesh.core.canonical import check_no_floats, hash_canonical
from mesh.core.clock import now_iso_utc
from mesh.core.events import record_event
from mesh.core.grants import (
    DEFAULT_PERMISSION_MODE,
    PERMISSION_MODES,
    compute_approval_card_actions,
    find_matching_standing_grant,
    record_grant_used,
)
from mesh.core.ids import generate_id
from mesh.core.redact import redact, redact_value
from mesh.core.workspaces import compute_task_workspace_path, get_workspaces_root
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.daemon.handlers.task import (
    PROFILE_RANKS,
    _format_task,
    _get_task,
    _parse_strict_int,
    _validate_assignment_guards,
)
from mesh.errors import MeshError

MAX_PLAN_STEPS = 30
STEP_KEY_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def compute_plan_hash(title: str, summary: str, steps: List[Dict[str, Any]]) -> str:
    """Deterministic SHA-256 hash over canonical immutable plan document."""
    doc = {
        "v": 1,
        "title": title,
        "summary": summary,
        "steps": steps,
    }
    return "sha256:" + hash_canonical(doc)


_compute_plan_hash = compute_plan_hash


def _topo_sort_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate DAG acyclicity and return steps in deterministic topological order."""
    by_key = {s["key"]: s for s in steps}
    in_deg: Dict[str, int] = {s["key"]: 0 for s in steps}
    dependents: Dict[str, List[str]] = {s["key"]: [] for s in steps}

    for s in steps:
        k = s["key"]
        for dep in s.get("depends_on") or []:
            if dep == k:
                raise MeshError(
                    code=c.DEP_CYCLE,
                    exit_code=c.EXIT_USAGE,
                    message=f"Plan step '{k}' cannot depend on itself",
                    details={"step_key": k},
                )
            if dep not in by_key:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Plan step '{k}' depends on unknown step key '{dep}'",
                    details={"step_key": k, "unknown_dep": dep},
                )
            in_deg[k] += 1
            dependents[dep].append(k)

    order_idx = {s["key"]: i for i, s in enumerate(steps)}
    ready = sorted([k for k, d in in_deg.items() if d == 0], key=lambda k: order_idx[k])
    ordered: List[Dict[str, Any]] = []
    while ready:
        cur = ready.pop(0)
        ordered.append(by_key[cur])
        for nxt in dependents[cur]:
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=lambda k: order_idx[k])

    if len(ordered) != len(steps):
        cycle_keys = sorted(k for k, d in in_deg.items() if d > 0)
        raise MeshError(
            code=c.DEP_CYCLE,
            exit_code=c.EXIT_USAGE,
            message=f"Plan steps contain a dependency cycle involving: {', '.join(cycle_keys)}",
            details={"cycle_keys": cycle_keys},
        )
    return ordered


def _require_project_id(ctx: HandlerContext) -> str:
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
    return project_id.strip()


def _cancel_plan_approval(
    conn: sqlite3.Connection,
    ctx: HandlerContext,
    approval_id: Optional[str],
    reason: str,
) -> bool:
    if not approval_id:
        return False
    arow = conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,)).fetchone()
    if arow is None or arow["status"] not in ("pending", "approved") or arow["consumed_at"] is not None:
        return False
    from_status = arow["status"]
    conn.execute("UPDATE approvals SET status = 'cancelled' WHERE id = ?;", (approval_id,))
    record_event(
        conn=conn,
        type_="approval.cancelled",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=arow["project_id"],
        approval_id=approval_id,
        reason=reason,
        payload={
            "reason": redact(reason),
            "approval_id": approval_id,
            "from_status": from_status,
            "category": arow["category"],
            "op": arow["op_type"],
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    return True


def _emit_step_card(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    plan_id: str,
    task_id: str,
    approval_id: str,
    op_hash: str,
    category: str,
    step_key: str,
    title: str,
    assignee_name: str,
    profile: str,
    run_id: Optional[str],
    request_id: Optional[str],
    now: str,
) -> str:
    card_id = generate_id("cmsg")
    if category == "permission_escalation":
        card_kind = "escalation_card"
        card_body = redact(
            f"需要提权确认：步骤 '{step_key}' ({title}) 请求为 '{assignee_name}' 提升权限至 '{profile}'。"
            "请使用卡片按钮（允许 / 始终允许 / 拒绝）决定。"
        )
    else:
        card_kind = "approval_card"
        card_body = redact(
            f"单步执行确认：步骤 '{step_key}' ({title}) 将由 '{assignee_name}' 以 '{profile}' 执行。"
            "请使用卡片按钮（允许 / 始终允许 / 拒绝）决定。"
        )
    card_ev = record_event(
        conn=conn,
        type_="chat.message",
        actor_id="principal_system",
        actor_kind="system",
        via="system",
        project_id=project_id,
        task_id=task_id,
        approval_id=approval_id,
        payload={
            "message_id": card_id,
            "project_id": project_id,
            "role": "system",
            "kind": card_kind,
            "plan_id": plan_id,
            "task_id": task_id,
            "approval_id": approval_id,
            "category": category,
            "op": "task.assign",
            "op_hash": op_hash,
            "profile": profile,
            "assignee": assignee_name,
            "body": card_body,
        },
        request_id=request_id,
    )
    conn.execute(
        """INSERT INTO chat_messages (
            id, project_id, event_seq, role, kind, actor_id, actor_kind, via,
            run_id, plan_id, approval_id, task_id, body, meta_json,
            consumed_by_approval_id, created_at
        ) VALUES (?, ?, ?, 'system', ?, 'principal_system', 'system', 'system',
                  ?, ?, ?, ?, ?, ?, NULL, ?);""",
        (
            card_id,
            project_id,
            card_ev["seq"],
            card_kind,
            run_id,
            plan_id,
            approval_id,
            task_id,
            card_body,
            json.dumps({
                "category": category,
                "op": "task.assign",
                "op_hash": op_hash,
                "profile": profile,
                "assignee": assignee_name,
                "step_key": step_key,
            }, sort_keys=True),
            now,
        ),
    )
    return card_id


def format_plan_summary(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_tasks: bool = False,
    now: Optional[str] = None,
    testing_mode: Optional[bool] = None,
) -> Dict[str, Any]:
    """Format a plan row with parsed steps, approval summary, and live task progress."""
    try:
        steps = json.loads(row["steps_json"] or "[]")
    except (TypeError, ValueError):
        steps = []

    task_rows = conn.execute(
        """SELECT t.*, p.name AS assignee_name
           FROM tasks t LEFT JOIN principals p ON t.assignee_agent_id = p.id
           WHERE t.plan_id = ?
           ORDER BY t.created_at ASC, t.id ASC;""",
        (row["id"],),
    ).fetchall()

    counts = {
        "total": len(task_rows),
        "completed": 0,
        "running": 0,
        "queued": 0,
        "failed": 0,
        "pending_approval": 0,
    }
    tasks_out: List[Dict[str, Any]] = []
    for trow in task_rows:
        st = trow["status"]
        if st in ("merged", "cancelled") or (st == "accepted" and trow["kind"] == "general"):
            counts["completed"] += 1
        elif st in ("running", "merging", "submitted"):
            counts["running"] += 1
        elif st in ("assigned", "queued", "rework"):
            counts["queued"] += 1
        elif st in ("failed", "escalated", "merge_failed"):
            counts["failed"] += 1
        elif st == "created":
            counts["pending_approval"] += 1
        if include_tasks:
            cur_deps = conn.execute(
                "SELECT depends_on_task_id FROM task_deps WHERE task_id = ? ORDER BY depends_on_task_id;",
                (trow["id"],),
            )
            deps = [d["depends_on_task_id"] for d in cur_deps.fetchall()]
            tasks_out.append(_format_task(trow, conn=conn, depends_on=deps))

    prow = conn.execute(
        "SELECT id, status, permission_mode FROM projects WHERE id = ?;",
        (row["project_id"],),
    ).fetchone()
    pmode = prow["permission_mode"] if (prow and "permission_mode" in prow.keys() and prow["permission_mode"] in PERMISSION_MODES) else DEFAULT_PERMISSION_MODE

    apr_summary = None
    if row["approval_id"]:
        arow = conn.execute("SELECT * FROM approvals WHERE id = ?;", (row["approval_id"],)).fetchone()
        if arow is not None:
            from mesh.core.grants import serialize_approval_card_summary

            _, eff_policy = get_row_approval_policy(conn, arow)
            card_ui = compute_approval_card_actions(
                conn,
                arow,
                now=now,
                testing_mode=testing_mode,
                plan_status=row["status"],
                prefetched_project=prow,
            )
            apr_summary = serialize_approval_card_summary(
                arow,
                policy=eff_policy,
                card_ui=card_ui,
            )

    out: Dict[str, Any] = {
        "id": row["id"],
        "project_id": row["project_id"],
        "permission_mode": pmode,
        "version": row["version"],
        "status": row["status"],
        "title": redact(row["title"]) if row["title"] else row["title"],
        "summary": redact(row["summary"]) if row["summary"] else row["summary"],
        "steps": redact_value(steps),
        "plan_hash": row["plan_hash"],
        "proposed_by_agent_id": row["proposed_by_agent_id"],
        "proposed_by_run_id": row["proposed_by_run_id"],
        "proposed_event_seq": row["proposed_event_seq"],
        "approval_id": row["approval_id"],
        "approval": apr_summary,
        "progress": counts,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "dispatched_at": row["dispatched_at"],
    }
    if include_tasks:
        out["tasks"] = tasks_out
    return out


def handle_plan_propose(ctx: HandlerContext) -> HandlerResult:
    """Propose (or revise) a structured multi-step plan with full Assignment Guard pre-checks."""
    project_id = _require_project_id(ctx)
    check_authz(ctx.conn, "plan.propose", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    if ctx.actor_kind != "agent":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Command 'plan.propose' must be called by the project lead agent",
            details={"cmd": "plan.propose", "actor_kind": ctx.actor_kind},
        )
    if not is_acting_project_lead(ctx.conn, project_id, ctx.principal["id"]):
        raise MeshError(
            code=c.NOT_PROJECT_LEAD,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Agent '{ctx.principal.get('name')}' is not the current lead of project '{project_id}'",
            details={"project_id": project_id, "agent": ctx.principal.get("name")},
        )

    project = ctx.conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
    if project is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )
    if project["status"] != "active":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {project['status']}, not active",
            details={"project_id": project_id, "status": project["status"]},
        )

    pmode = project["permission_mode"] if "permission_mode" in project.keys() and project["permission_mode"] in PERMISSION_MODES else DEFAULT_PERMISSION_MODE
    proposing_agent_id = ctx.principal["id"]

    title = ctx.args.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 300:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'title' must be a non-empty string up to 300 characters",
            details={"param": "title"},
        )
    title = title.strip()

    summary = ctx.args.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > 4000:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'summary' must be a non-empty string up to 4000 characters",
            details={"param": "summary"},
        )
    summary = summary.strip()

    raw_steps = ctx.args.get("steps")
    if not isinstance(raw_steps, list) or not (1 <= len(raw_steps) <= MAX_PLAN_STEPS):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Field 'steps' must be an array of 1..{MAX_PLAN_STEPS} step objects",
            details={"param": "steps"},
        )
    try:
        check_no_floats(raw_steps)
    except ValueError as ve:
        raise MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message=str(ve), details={"param": "steps"})

    # P3-4: Plans are hashed and stored verbatim; never accept secret-bearing text that
    # would be shown redacted to the user while hashed/dispatched raw.
    for field_name, field_val in (("title", title), ("summary", summary), ("steps", raw_steps)):
        if redact_value(field_val) != field_val:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=(
                    f"Plan {field_name} contains what looks like a secret; it would be stored as is. "
                    "Refer to the secret by name (an env var or a file path) instead"
                ),
                details={"param": field_name},
            )

    min_timeout = 1 if ctx.testing_mode else 60
    to_row = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'task.default_timeout_s';").fetchone()
    default_timeout_s = int(json.loads(to_row["value_json"])) if to_row else 3600
    if not ctx.testing_mode:
        default_timeout_s = max(60, default_timeout_s)

    cap_row = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'task.max_attempts_cap';").fetchone()
    attempts_cap = int(json.loads(cap_row["value_json"])) if cap_row else 10
    dma_row = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'task.default_max_attempts';").fetchone()
    default_max_attempts = int(json.loads(dma_row["value_json"])) if dma_row else 3

    seen_keys = set()
    normalized_steps: List[Dict[str, Any]] = []
    ws_root = get_workspaces_root(ctx.conn, ctx.home)

    for idx, step in enumerate(raw_steps):
        if not isinstance(step, dict):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"steps[{idx}] must be a JSON object",
                details={"param": f"steps[{idx}]"},
            )
        skey = step.get("key")
        if not isinstance(skey, str) or not STEP_KEY_RE.match(skey):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"steps[{idx}].key must match ^[A-Za-z0-9._-]{{1,64}}$",
                details={"param": f"steps[{idx}].key", "got": skey},
            )
        if skey in seen_keys:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Duplicate plan step key '{skey}'",
                details={"param": "steps", "key": skey},
            )
        seen_keys.add(skey)

        stitle = step.get("title")
        if not isinstance(stitle, str) or not stitle.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"steps[{idx}].title must be non-empty",
                details={"param": f"steps[{idx}].title"},
            )
        stitle = stitle.strip()

        sspec = step.get("spec")
        if not isinstance(sspec, str) or not sspec.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"steps[{idx}].spec must be non-empty",
                details={"param": f"steps[{idx}].spec"},
            )
        sspec = sspec.strip()

        skind = step.get("kind")
        if skind is not None:
            if not isinstance(skind, str) or skind not in ("code", "general"):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"steps[{idx}].kind must be 'code' or 'general'",
                    details={"param": f"steps[{idx}].kind", "got": skind},
                )
        else:
            skind = "code" if project["repo_path"] else "general"
        if skind == "code" and not project["repo_path"]:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"steps[{idx}] kind 'code' needs a repo project; project '{project_id}' has no repo",
                details={"param": f"steps[{idx}].kind", "reason": "no_repo"},
            )

        assignee = step.get("assignee")
        if not isinstance(assignee, str) or not assignee.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"steps[{idx}].assignee must be a non-empty agent name or id",
                details={"param": f"steps[{idx}].assignee"},
            )
        assignee = assignee.strip()

        priority = 0
        if step.get("priority") is not None:
            priority = _parse_strict_int(step["priority"], f"steps[{idx}].priority")
            if not (-1_000_000 <= priority <= 1_000_000):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"steps[{idx}].priority out of range",
                    details={"param": f"steps[{idx}].priority"},
                )

        timeout_s = default_timeout_s
        if step.get("timeout") is not None:
            timeout_s = _parse_strict_int(step["timeout"], f"steps[{idx}].timeout", allow_s_suffix=True)
            if not (min_timeout <= timeout_s <= 604800):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"steps[{idx}].timeout must be between {min_timeout} and 604800 seconds",
                    details={"param": f"steps[{idx}].timeout"},
                )

        idle_timeout_s = 0
        if step.get("idle_timeout") is not None:
            idle_timeout_s = _parse_strict_int(step["idle_timeout"], f"steps[{idx}].idle_timeout", allow_s_suffix=True)
            if idle_timeout_s != 0 and not (min_timeout <= idle_timeout_s <= 604800):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"steps[{idx}].idle_timeout out of range",
                    details={"param": f"steps[{idx}].idle_timeout"},
                )

        max_attempts = default_max_attempts
        if step.get("max_attempts") is not None:
            max_attempts = _parse_strict_int(step["max_attempts"], f"steps[{idx}].max_attempts")
            if not (1 <= max_attempts <= attempts_cap):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"steps[{idx}].max_attempts must be between 1 and {attempts_cap}",
                    details={"param": f"steps[{idx}].max_attempts"},
                )

        raw_deps = step.get("depends_on")
        if raw_deps is None:
            deps: List[str] = []
        elif isinstance(raw_deps, list) and all(isinstance(d, str) and d.strip() for d in raw_deps):
            deps = list(dict.fromkeys(d.strip() for d in raw_deps))
        else:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"steps[{idx}].depends_on must be an array of step keys",
                details={"param": f"steps[{idx}].depends_on"},
            )

        prof_override = step.get("profile")
        model_override = step.get("model")
        effort_override = step.get("effort")
        for pname, pval in (("profile", prof_override), ("model", model_override), ("effort", effort_override)):
            if pval is not None and (not isinstance(pval, str) or not pval.strip()):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"steps[{idx}].{pname} must be a non-empty string",
                    details={"param": f"steps[{idx}].{pname}"},
                )

        preview_task_id = "tsk_" + "0" * 26
        preview_ws = compute_task_workspace_path(ws_root, project_id, preview_task_id, attempt=1)
        agent_row, resolved_profile, resolved_model, resolved_effort = _validate_assignment_guards(
            conn=ctx.conn,
            ctx=ctx,
            project_id=project_id,
            assignee_ident=assignee,
            profile_override=prof_override,
            model_override=model_override,
            effort_override=effort_override,
            task_title=stitle,
            op="task.create",
            prompt_check={
                "task_doc": {"id": preview_task_id, "title": stitle, "spec": sspec, "kind": skind, "branch": None},
                "workspace": preview_ws,
                "attempt": 1,
                "max_attempts": max_attempts,
            },
            check_approval=False,
        )

        max_unapproved = agent_row["max_unapproved_profile"] or "dev"
        requires_escalation = PROFILE_RANKS.get(resolved_profile, 0) > PROFILE_RANKS.get(max_unapproved, 2)
        step_warnings: List[str] = []
        if requires_escalation:
            step_warnings.append(
                f"Step '{skey}' requests profile '{resolved_profile}' above '{agent_row['name']}' "
                f"max_unapproved_profile '{max_unapproved}'; requires explicit permission_escalation card approval or exact-scope standing grant"
            )

        normalized_steps.append({
            "key": skey,
            "title": stitle,
            "spec": sspec,
            "kind": skind,
            "assignee": agent_row["name"],
            "assignee_agent_id": agent_row["id"],
            "profile": resolved_profile,
            "profile_explicit": bool(prof_override),
            "model": resolved_model,
            "effort": resolved_effort,
            "priority": priority,
            "timeout_s": timeout_s,
            "idle_timeout_s": idle_timeout_s,
            "max_attempts": max_attempts,
            "depends_on": deps,
            "requires_escalation": requires_escalation,
            "warnings": step_warnings,
        })

    _topo_sort_steps(normalized_steps)
    if redact_value(normalized_steps) != normalized_steps:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=(
                "Plan steps contain what looks like a secret; it would be stored as is. "
                "Refer to the secret by name (an env var or a file path) instead"
            ),
            details={"param": "steps"},
        )

    now = now_iso_utc()
    superseded_ids: List[str] = []
    open_plans = ctx.conn.execute(
        "SELECT * FROM plans WHERE project_id = ? AND status IN ('proposed', 'approved_pending_dispatch', 'revising') "
        "ORDER BY version ASC;",
        (project_id,),
    ).fetchall()
    for op in open_plans:
        if op["status"] == "approved_pending_dispatch" and op["approval_id"]:
            arow = ctx.conn.execute(
                "SELECT status, decided_via, expires_at FROM approvals WHERE id = ?;",
                (op["approval_id"],),
            ).fetchone()
            if (
                arow is not None
                and arow["status"] == "approved"
                and arow["decided_via"] == "direct"
                and arow["expires_at"] >= now
            ):
                raise MeshError(
                    code=c.INVALID_TRANSITION,
                    exit_code=c.EXIT_CONFLICT,
                    message=(
                        f"Plan '{op['id']}' is already user-approved and awaiting dispatch; "
                        f"dispatch it or ask the user to cancel it before proposing a new plan"
                    ),
                    details={"plan_id": op["id"], "approval_id": op["approval_id"], "status": op["status"]},
                )
    for op in open_plans:
        _cancel_plan_approval(ctx.conn, ctx, op["approval_id"], "superseded by new plan proposal")
        ctx.conn.execute(
            "UPDATE plans SET status = 'superseded', updated_at = ? WHERE id = ?;",
            (now, op["id"]),
        )
        superseded_ids.append(op["id"])

    ver_row = ctx.conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS max_ver FROM plans WHERE project_id = ?;",
        (project_id,),
    ).fetchone()
    version = int(ver_row["max_ver"]) + 1
    plan_id = generate_id("pln")
    plan_hash = compute_plan_hash(title, summary, normalized_steps)

    run_id: Optional[str] = None
    if ctx.via == "run_token":
        scope = ctx.principal.get("scope") or {}
        if isinstance(scope.get("run_id"), str) and scope["run_id"]:
            run_id = scope["run_id"]

    auto_mode = (pmode == "auto")
    apr_info: Optional[Dict[str, Any]] = None
    apr_id: Optional[str] = None
    initial_status = "approved_pending_dispatch" if auto_mode else "proposed"

    if not auto_mode:
        apr_info = create_or_get_pending_approval(
            conn=ctx.conn,
            category="plan_dispatch",
            op="plan.dispatch",
            requested_by=proposing_agent_id,
            params={
                "plan_id": plan_id,
                "plan_hash": plan_hash,
                "version": version,
                "step_count": len(normalized_steps),
            },
            summary=f"Dispatch plan v{version}: {title} ({len(normalized_steps)} step(s))",
            project_id=project_id,
            target_id=plan_id,
            actor_kind="agent",
            via=ctx.via,
            request_id=ctx.request_id,
            k_user=None,
            testing_mode=ctx.testing_mode,
            emit_chat_card=False,
        )
        apr_id = apr_info["approval_id"]

    ev_type = "plan.revised" if superseded_ids else "plan.proposed"
    proposed_ev = record_event(
        conn=ctx.conn,
        type_=ev_type,
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        credential_id=ctx.credential_id,
        project_id=project_id,
        run_id=run_id,
        approval_id=apr_id,
        payload={
            "plan_id": plan_id,
            "project_id": project_id,
            "version": version,
            "title": title,
            "summary": summary,
            "plan_hash": plan_hash,
            "approval_id": apr_id,
            "status": initial_status,
            "permission_mode": pmode,
            "auto_approved": auto_mode,
            "step_count": len(normalized_steps),
            "supersedes_plan_ids": superseded_ids,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    ctx.conn.execute(
        """INSERT INTO plans (
            id, project_id, version, status, title, summary, steps_json, plan_hash,
            proposed_by_agent_id, proposed_by_run_id, proposed_event_seq,
            approval_id, created_at, updated_at, dispatched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL);""",
        (
            plan_id,
            project_id,
            version,
            initial_status,
            title,
            summary,
            json.dumps(normalized_steps, sort_keys=True, ensure_ascii=False),
            plan_hash,
            proposing_agent_id,
            run_id,
            proposed_ev["seq"],
            apr_id,
            now,
            now,
        ),
    )

    # Emit inline plan_card chat message (system card; does not wake lead).
    # Live decidable actions are hydrated at read time in chat.list from compute_approval_card_actions.
    card_msg_id = generate_id("cmsg")
    card_body = redact(f"Plan v{version}: {title}\n{summary}")
    card_meta: Dict[str, Any] = {
        "version": version,
        "plan_hash": plan_hash,
        "step_count": len(normalized_steps),
        "permission_mode": pmode,
        "auto_approved": auto_mode,
    }
    if apr_info is not None:
        card_meta["op_hash"] = apr_info["op_hash"]
        card_meta["category"] = "plan_dispatch"

    card_ev = record_event(
        conn=ctx.conn,
        type_="chat.message",
        actor_id="principal_system",
        actor_kind="system",
        via="system",
        project_id=project_id,
        run_id=run_id,
        approval_id=apr_id,
        payload={
            "message_id": card_msg_id,
            "project_id": project_id,
            "role": "system",
            "kind": "plan_card",
            "plan_id": plan_id,
            "approval_id": apr_id,
            "version": version,
            "plan_hash": plan_hash,
            "auto_approved": auto_mode,
            "body": card_body,
        },
        request_id=ctx.request_id,
    )
    ctx.conn.execute(
        """INSERT INTO chat_messages (
            id, project_id, event_seq, role, kind, actor_id, actor_kind, via,
            run_id, plan_id, approval_id, task_id, body, meta_json,
            consumed_by_approval_id, created_at
        ) VALUES (?, ?, ?, 'system', 'plan_card', 'principal_system', 'system', 'system',
                  ?, ?, ?, NULL, ?, ?, NULL, ?);""",
        (
            card_msg_id,
            project_id,
            card_ev["seq"],
            run_id,
            plan_id,
            apr_id,
            card_body,
            json.dumps(card_meta, sort_keys=True),
            now,
        ),
    )

    plan_row = ctx.conn.execute("SELECT * FROM plans WHERE id = ?;", (plan_id,)).fetchone()
    warnings: List[str] = []
    for st in normalized_steps:
        warnings.extend(st.get("warnings") or [])

    return HandlerResult(
        data={
            "plan": format_plan_summary(ctx.conn, plan_row, now=now, testing_mode=ctx.testing_mode),
            "approval": apr_info,
            "card_message_id": card_msg_id,
            "superseded_plan_ids": superseded_ids,
        },
        warnings=warnings,
        events=[ev_type, "chat.message"],
    )


def handle_plan_dispatch(ctx: HandlerContext) -> HandlerResult:
    """Atomically materialize an approved immutable plan (or auto-mode plan) into tasks + task_deps."""
    project_id = _require_project_id(ctx)
    check_authz(ctx.conn, "plan.dispatch", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    if ctx.actor_kind != "agent":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Command 'plan.dispatch' must be called by the project lead agent",
            details={"cmd": "plan.dispatch", "actor_kind": ctx.actor_kind},
        )
    if not is_acting_project_lead(ctx.conn, project_id, ctx.principal["id"]):
        raise MeshError(
            code=c.NOT_PROJECT_LEAD,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Agent '{ctx.principal.get('name')}' is not the current lead of project '{project_id}'",
            details={"project_id": project_id, "agent": ctx.principal.get("name")},
        )

    project = ctx.conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
    if project is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )
    if project["status"] != "active":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {project['status']}, not active",
            details={"project_id": project_id, "status": project["status"]},
        )

    pmode = project["permission_mode"] if "permission_mode" in project.keys() and project["permission_mode"] in PERMISSION_MODES else DEFAULT_PERMISSION_MODE

    plan_id = ctx.args.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'plan_id' is required",
            details={"param": "plan_id"},
        )
    plan_id = plan_id.strip()

    plan = ctx.conn.execute(
        "SELECT * FROM plans WHERE id = ? AND project_id = ?;",
        (plan_id, project_id),
    ).fetchone()
    if plan is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Plan '{plan_id}' not found in project '{project_id}'",
            details={"plan_id": plan_id, "project_id": project_id},
        )

    # Re-verify immutable plan steps & plan_hash (anti-TOCTOU / tamper detection)
    try:
        steps = json.loads(plan["steps_json"] or "[]")
    except (TypeError, ValueError) as exc:
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Stored plan steps_json is corrupt: {exc}",
            details={"plan_id": plan_id},
        )
    if not isinstance(steps, list) or not steps:
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Stored plan has no steps",
            details={"plan_id": plan_id},
        )
    recomputed_hash = compute_plan_hash(plan["title"], plan["summary"], steps)
    if recomputed_hash != plan["plan_hash"]:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Stored plan_hash does not match immutable plan document",
            details={"plan_id": plan_id, "expected": plan["plan_hash"], "computed": recomputed_hash},
        )

    current_run_id: Optional[str] = None
    if ctx.via == "run_token":
        scope = ctx.principal.get("scope") or {}
        if isinstance(scope.get("run_id"), str) and scope["run_id"]:
            current_run_id = scope["run_id"]

    caller_for_approval = ctx.principal["id"]
    raw_apr = ctx.approval or ctx.args.get("approval")
    approval_id = raw_apr.strip() if isinstance(raw_apr, str) and raw_apr.strip() else None

    consumed_approval_id: Optional[str] = None
    auto_dispatched = False

    if pmode == "auto":
        if plan["status"] not in ("proposed", "approved_pending_dispatch"):
            raise MeshError(
                code=c.INVALID_TRANSITION,
                exit_code=c.EXIT_CONFLICT,
                message=f"Plan '{plan_id}' is in status '{plan['status']}' and cannot be dispatched",
                details={"plan_id": plan_id, "status": plan["status"]},
            )
        if plan["approval_id"]:
            arow = ctx.conn.execute("SELECT * FROM approvals WHERE id = ?;", (plan["approval_id"],)).fetchone()
            if arow is not None and arow["status"] == "approved":
                apr_row = check_and_consume_approval(
                    conn=ctx.conn,
                    approval_id=plan["approval_id"],
                    op="plan.dispatch",
                    category="plan_dispatch",
                    caller_id=caller_for_approval,
                    params={
                        "plan_id": plan_id,
                        "plan_hash": plan["plan_hash"],
                        "version": plan["version"],
                        "step_count": len(steps),
                    },
                    project_id=project_id,
                    target_id=plan_id,
                    request_id=ctx.request_id,
                    actor_kind=ctx.actor_kind,
                    via=ctx.via,
                    k_user=ctx.k_user,
                )
                if apr_row.get("decided_via") != "direct" or not apr_row.get("user_mac"):
                    raise MeshError(
                        code=c.CHANNEL_NOT_ALLOWED,
                        exit_code=c.EXIT_APPROVAL_INVALID,
                        message="plan.dispatch requires a direct user_mac-signed approval",
                        details={"plan_id": plan_id, "approval_id": plan["approval_id"]},
                    )
                consumed_approval_id = plan["approval_id"]
            elif arow is not None and arow["status"] == "pending":
                _cancel_plan_approval(ctx.conn, ctx, plan["approval_id"], "auto_mode_bypass")
                auto_dispatched = True
            else:
                auto_dispatched = True
        else:
            auto_dispatched = True

        if auto_dispatched:
            record_event(
                conn=ctx.conn,
                type_="policy.auto_allowed",
                actor_id=ctx.principal["id"],
                actor_kind=ctx.actor_kind,
                via=ctx.via,
                credential_id=ctx.credential_id,
                project_id=project_id,
                run_id=current_run_id,
                payload={
                    "category": "plan_dispatch",
                    "op": "plan.dispatch",
                    "plan_id": plan_id,
                    "plan_hash": plan["plan_hash"],
                    "version": plan["version"],
                    "permission_mode": "auto",
                },
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )
    else:
        # 'plan' or 'supervised' mode: requires direct user approval via plan card!
        if plan["status"] == "proposed":
            raise MeshError(
                code=c.APPROVAL_REQUIRED,
                exit_code=c.EXIT_APPROVAL_REQUIRED,
                message=f"Plan '{plan_id}' has not been approved by the user yet (click 允许 on the plan card)",
                details={"plan_id": plan_id, "approval_id": plan["approval_id"], "status": plan["status"]},
            )
        if plan["status"] != "approved_pending_dispatch":
            raise MeshError(
                code=c.INVALID_TRANSITION,
                exit_code=c.EXIT_CONFLICT,
                message=f"Plan '{plan_id}' is in status '{plan['status']}', expected 'approved_pending_dispatch'",
                details={"plan_id": plan_id, "status": plan["status"]},
            )
        if not approval_id:
            raise MeshError(
                code=c.APPROVAL_REQUIRED,
                exit_code=c.EXIT_APPROVAL_REQUIRED,
                message="Plan dispatch requires an approved plan_dispatch approval",
                details={"plan_id": plan_id, "approval_id": plan["approval_id"], "category": "plan_dispatch"},
            )
        if plan["approval_id"] != approval_id:
            raise MeshError(
                code=c.APPROVAL_INVALID,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Provided approval_id does not match this plan's plan_dispatch approval",
                details={"plan_id": plan_id, "approval_id": approval_id, "plan_approval_id": plan["approval_id"]},
            )

        apr_row = check_and_consume_approval(
            conn=ctx.conn,
            approval_id=approval_id,
            op="plan.dispatch",
            category="plan_dispatch",
            caller_id=caller_for_approval,
            params={
                "plan_id": plan_id,
                "plan_hash": plan["plan_hash"],
                "version": plan["version"],
                "step_count": len(steps),
            },
            project_id=project_id,
            target_id=plan_id,
            request_id=ctx.request_id,
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            k_user=ctx.k_user,
        )
        if apr_row.get("decided_via") != "direct" or not apr_row.get("user_mac"):
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="plan.dispatch requires a direct user_mac-signed approval",
                details={"plan_id": plan_id, "approval_id": approval_id},
            )
        consumed_approval_id = approval_id

    # Atomically materialize tasks in topological order from immutable plans.steps_json
    ordered_steps = _topo_sort_steps(steps)
    now = now_iso_utc()
    ws_root = get_workspaces_root(ctx.conn, ctx.home)
    step_key_to_task_id: Dict[str, str] = {}
    created_tasks: List[Dict[str, Any]] = []
    escalated_approvals: List[Dict[str, Any]] = []
    step_approvals: List[Dict[str, Any]] = []
    escalated_task_ids: List[str] = []

    for step in ordered_steps:
        skey = step["key"]
        stitle = step["title"]
        sspec = step["spec"]
        skind = step["kind"]
        assignee_ident = step["assignee"]
        prof_override = step.get("profile")
        model_override = step.get("model")
        effort_override = step.get("effort")
        priority = int(step.get("priority", 0))
        timeout_s = int(step.get("timeout_s", 3600))
        idle_timeout_s = int(step.get("idle_timeout_s", 0))
        max_attempts = int(step.get("max_attempts", 3))
        dep_task_ids = [step_key_to_task_id[dk] for dk in (step.get("depends_on") or [])]

        task_id = generate_id("tsk")
        workspace_path = os.path.join(ws_root, project_id, "tasks", task_id)
        preview_ws = compute_task_workspace_path(ws_root, project_id, task_id, attempt=1)

        agent_row, resolved_profile, resolved_model, resolved_effort = _validate_assignment_guards(
            conn=ctx.conn,
            ctx=ctx,
            project_id=project_id,
            assignee_ident=assignee_ident,
            profile_override=prof_override,
            model_override=model_override,
            effort_override=effort_override,
            task_title=stitle,
            op="task.assign",
            task_id=task_id,
            prompt_check={
                "task_doc": {"id": task_id, "title": stitle, "spec": sspec, "kind": skind, "branch": None},
                "workspace": preview_ws,
                "attempt": 1,
                "max_attempts": max_attempts,
            },
            check_approval=False,
        )

        max_unapproved = agent_row["max_unapproved_profile"] or "dev"
        need_perm = PROFILE_RANKS.get(resolved_profile, 0) > PROFILE_RANKS.get(max_unapproved, 2)
        perm_grant = (
            find_matching_standing_grant(
                ctx.conn,
                project_id=project_id,
                category="permission_escalation",
                agent_id=agent_row["id"],
                profile=resolved_profile,
            )
            if need_perm
            else None
        )
        need_perm_card = need_perm and (perm_grant is None)
        need_step_card = (pmode == "supervised")

        if need_perm_card or need_step_card:
            ctx.conn.execute(
                """INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, priority,
                    assignee_agent_id, model, effort, profile, timeout_s,
                    idle_timeout_s, max_attempts, attempt, fence_seq,
                    queued_at, offered_at, workspace_path, branch, base_commit,
                    last_session_id, deliverable_json, failure_json, merge_json,
                    created_by, version, created_at, updated_at, plan_id, plan_step_key
                ) VALUES (
                    ?, ?, ?, ?, ?, 'created', ?,
                    NULL, ?, ?, ?, ?,
                    ?, ?, 0, 0,
                    NULL, NULL, ?, NULL, NULL,
                    NULL, NULL, NULL, NULL,
                    ?, 1, ?, ?, ?, ?
                );""",
                (
                    task_id, project_id, stitle, sspec, skind, priority,
                    resolved_model, resolved_effort, resolved_profile, timeout_s,
                    idle_timeout_s, max_attempts, workspace_path,
                    ctx.principal["id"], now, now, plan_id, skey,
                ),
            )
            for dep_tid in dep_task_ids:
                ctx.conn.execute(
                    "INSERT INTO task_deps (task_id, depends_on_task_id) VALUES (?, ?);",
                    (task_id, dep_tid),
                )
            record_event(
                conn=ctx.conn,
                type_="task.created",
                actor_id=ctx.principal["id"],
                actor_kind=ctx.actor_kind,
                via=ctx.via,
                credential_id=ctx.credential_id,
                project_id=project_id,
                task_id=task_id,
                from_state=None,
                to_state="created",
                payload={
                    "task_id": task_id,
                    "project_id": project_id,
                    "plan_id": plan_id,
                    "plan_step_key": skey,
                    "title": stitle,
                    "assignee": None,
                    "intended_assignee": agent_row["name"],
                    "profile": resolved_profile,
                    "profile_explicit": True,
                    "model": resolved_model,
                    "effort": resolved_effort,
                    "awaiting_permission_escalation": need_perm_card,
                    "awaiting_step_approval": need_step_card and not need_perm_card,
                },
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )

            card_cat = "permission_escalation" if need_perm_card else "step_dispatch"
            card_summary = (
                f"Permission escalation to '{resolved_profile}' for task '{stitle}' on agent '{agent_row['name']}'"
                if card_cat == "permission_escalation"
                else f"Run step '{skey}' ({stitle}) on agent '{agent_row['name']}' (profile: '{resolved_profile}')"
            )
            step_apr = create_or_get_pending_approval(
                conn=ctx.conn,
                category=card_cat,
                op="task.assign",
                requested_by=caller_for_approval,
                params={
                    "task_id": task_id,
                    "to": agent_row["name"],
                    "profile": resolved_profile,
                    "model": resolved_model,
                    "effort": resolved_effort,
                },
                summary=card_summary,
                project_id=project_id,
                target_id=task_id,
                task_id=task_id,
                actor_kind="agent",
                via=ctx.via,
                request_id=ctx.request_id,
                k_user=None,
                testing_mode=ctx.testing_mode,
                emit_chat_card=False,
            )
            _emit_step_card(
                ctx.conn,
                project_id=project_id,
                plan_id=plan_id,
                task_id=task_id,
                approval_id=step_apr["approval_id"],
                op_hash=step_apr["op_hash"],
                category=card_cat,
                step_key=skey,
                title=stitle,
                assignee_name=agent_row["name"],
                profile=resolved_profile,
                run_id=current_run_id,
                request_id=ctx.request_id,
                now=now,
            )
            step_apr_entry = {
                "task_id": task_id,
                "step_key": skey,
                "approval_id": step_apr["approval_id"],
                "op_hash": step_apr["op_hash"],
                "category": card_cat,
                "profile": resolved_profile,
                "assignee": agent_row["name"],
            }
            step_approvals.append(step_apr_entry)
            if card_cat == "permission_escalation":
                escalated_task_ids.append(task_id)
                escalated_approvals.append(step_apr_entry)
        else:
            if need_perm and perm_grant is not None:
                record_grant_used(
                    ctx.conn,
                    grant=perm_grant,
                    op="plan.dispatch",
                    actor_id=ctx.principal["id"],
                    actor_kind=ctx.actor_kind,
                    via=ctx.via,
                    project_id=project_id,
                    task_id=task_id,
                    credential_id=ctx.credential_id,
                    request_id=ctx.request_id,
                    k_user=ctx.k_user,
                    extra={"plan_id": plan_id, "step_key": skey},
                )
            ctx.conn.execute(
                """INSERT INTO tasks (
                    id, project_id, title, spec, kind, status, priority,
                    assignee_agent_id, model, effort, profile, timeout_s,
                    idle_timeout_s, max_attempts, attempt, fence_seq,
                    queued_at, offered_at, workspace_path, branch, base_commit,
                    last_session_id, deliverable_json, failure_json, merge_json,
                    created_by, version, created_at, updated_at, plan_id, plan_step_key
                ) VALUES (
                    ?, ?, ?, ?, ?, 'assigned', ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, 0, 0,
                    NULL, NULL, ?, NULL, NULL,
                    NULL, NULL, NULL, NULL,
                    ?, 1, ?, ?, ?, ?
                );""",
                (
                    task_id, project_id, stitle, sspec, skind, priority,
                    agent_row["id"], resolved_model, resolved_effort, resolved_profile, timeout_s,
                    idle_timeout_s, max_attempts, workspace_path,
                    ctx.principal["id"], now, now, plan_id, skey,
                ),
            )
            for dep_tid in dep_task_ids:
                ctx.conn.execute(
                    "INSERT INTO task_deps (task_id, depends_on_task_id) VALUES (?, ?);",
                    (task_id, dep_tid),
                )
            record_event(
                conn=ctx.conn,
                type_="task.assigned",
                actor_id=ctx.principal["id"],
                actor_kind=ctx.actor_kind,
                via=ctx.via,
                credential_id=ctx.credential_id,
                project_id=project_id,
                task_id=task_id,
                from_state=None,
                to_state="assigned",
                payload={
                    "task_id": task_id,
                    "project_id": project_id,
                    "plan_id": plan_id,
                    "plan_step_key": skey,
                    "title": stitle,
                    "assignee": agent_row["name"],
                    "profile": resolved_profile,
                    "profile_explicit": bool(step.get("profile_explicit")),
                    "model": resolved_model,
                    "effort": resolved_effort,
                },
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )

        step_key_to_task_id[skey] = task_id
        trow = _get_task(ctx.conn, task_id)
        created_tasks.append(_format_task(trow, conn=ctx.conn, depends_on=dep_task_ids, depended_on_by=[]))

    ctx.conn.execute(
        """UPDATE plans SET
            status = 'dispatched',
            dispatched_at = ?,
            updated_at = ?
        WHERE id = ?;""",
        (now, now, plan_id),
    )

    record_event(
        conn=ctx.conn,
        type_="plan.dispatched",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        credential_id=ctx.credential_id,
        project_id=project_id,
        run_id=current_run_id,
        approval_id=consumed_approval_id,
        payload={
            "plan_id": plan_id,
            "project_id": project_id,
            "version": plan["version"],
            "plan_hash": plan["plan_hash"],
            "approval_id": consumed_approval_id,
            "permission_mode": pmode,
            "auto_dispatched": auto_dispatched,
            "task_ids": [t["id"] for t in created_tasks],
            "escalated_task_ids": escalated_task_ids,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    updated_plan = ctx.conn.execute("SELECT * FROM plans WHERE id = ?;", (plan_id,)).fetchone()
    return HandlerResult(
        data={
            "plan": format_plan_summary(ctx.conn, updated_plan, now=now, testing_mode=ctx.testing_mode),
            "tasks": created_tasks,
            "escalated_approvals": escalated_approvals,
            "step_approvals": step_approvals,
        },
        events=["plan.dispatched"],
    )


def handle_plan_cancel(ctx: HandlerContext) -> HandlerResult:
    """Cancel an undispatched plan and its open plan_dispatch approval."""
    project_id = _require_project_id(ctx)
    check_authz(ctx.conn, "plan.cancel", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    if ctx.actor_kind != "user" and not is_acting_project_lead(ctx.conn, project_id, ctx.principal["id"]):
        raise MeshError(
            code=c.NOT_PROJECT_LEAD,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Agent '{ctx.principal.get('name')}' is not the current lead of project '{project_id}'",
            details={"project_id": project_id, "agent": ctx.principal.get("name")},
        )

    plan_id = ctx.args.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'plan_id' is required",
            details={"param": "plan_id"},
        )
    plan_id = plan_id.strip()

    plan = ctx.conn.execute(
        "SELECT * FROM plans WHERE id = ? AND project_id = ?;",
        (plan_id, project_id),
    ).fetchone()
    if plan is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Plan '{plan_id}' not found in project '{project_id}'",
            details={"plan_id": plan_id, "project_id": project_id},
        )
    if plan["status"] not in ("proposed", "revising", "approved_pending_dispatch"):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Plan '{plan_id}' is '{plan['status']}' and cannot be cancelled",
            details={"plan_id": plan_id, "status": plan["status"]},
        )
    if plan["status"] == "approved_pending_dispatch" and (ctx.actor_kind != "user" or ctx.via != "direct"):
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Only the direct user can cancel approved plan '{plan_id}'",
            details={"plan_id": plan_id, "status": plan["status"], "required": "user_direct"},
        )

    reason = ctx.args.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'reason' must be a string",
            details={"param": "reason"},
        )
    reason_str = reason.strip() if isinstance(reason, str) and reason.strip() else "plan cancelled"

    now = now_iso_utc()
    _cancel_plan_approval(ctx.conn, ctx, plan["approval_id"], reason_str)

    ctx.conn.execute(
        "UPDATE plans SET status = 'cancelled', updated_at = ? WHERE id = ?;",
        (now, plan_id),
    )

    record_event(
        conn=ctx.conn,
        type_="plan.cancelled",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        credential_id=ctx.credential_id,
        project_id=project_id,
        approval_id=plan["approval_id"],
        reason=reason_str,
        payload={
            "plan_id": plan_id,
            "version": plan["version"],
            "from_status": plan["status"],
            "to_status": "cancelled",
            "reason": redact(reason_str),
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    updated = ctx.conn.execute("SELECT * FROM plans WHERE id = ?;", (plan_id,)).fetchone()
    return HandlerResult(
        data={"plan": format_plan_summary(ctx.conn, updated, now=now, testing_mode=ctx.testing_mode)},
        events=["plan.cancelled"],
    )


def handle_plan_list(ctx: HandlerContext) -> HandlerResult:
    """List plans for a project with task progress summaries."""
    project_id = _require_project_id(ctx)
    check_authz(ctx.conn, "plan.list", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    if ctx.conn.execute("SELECT 1 FROM projects WHERE id = ?;", (project_id,)).fetchone() is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    rows = ctx.conn.execute(
        "SELECT * FROM plans WHERE project_id = ? ORDER BY version DESC;",
        (project_id,),
    ).fetchall()
    now = now_iso_utc()
    items = [
        format_plan_summary(ctx.conn, r, include_tasks=False, now=now, testing_mode=ctx.testing_mode)
        for r in rows
    ]
    return HandlerResult(data={"project_id": project_id, "items": items})


def handle_plan_show(ctx: HandlerContext) -> HandlerResult:
    """Show a single plan with steps, live tasks, and approval summary."""
    plan_id = ctx.args.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'plan_id' is required",
            details={"param": "plan_id"},
        )
    plan_id = plan_id.strip()

    row = ctx.conn.execute("SELECT * FROM plans WHERE id = ?;", (plan_id,)).fetchone()
    if row is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Plan '{plan_id}' not found",
            details={"plan_id": plan_id},
        )
    project_id = row["project_id"]
    check_authz(ctx.conn, "plan.show", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    return HandlerResult(
        data=format_plan_summary(
            ctx.conn, row, include_tasks=True, now=now_iso_utc(), testing_mode=ctx.testing_mode
        )
    )
