"""Task command handlers per plan §6.2, §7.3, §9.1, §9.4, §13.3."""

import asyncio
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from mesh.core.redact import redact, redact_value
import mesh.constants as c
from mesh.adapters.base import (
    AdapterNotInstalled,
    AdapterUnsupported,
    EffortUnsupported,
    ProfileUnsupported,
)
from mesh.adapters.registry import get_adapter
from mesh.core.approvals import (
    check_and_consume_approval,
    create_or_get_pending_approval,
)
from mesh.core.authz import check_authz, get_agent_capabilities, is_project_lead, lead_run_token_scope
from mesh.core.clock import now_iso_utc, parse_duration
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.core.leases import get_lease_ttl_s, verify_task_lease_and_fence
from mesh.core.states import check_task_transition, is_task_terminal
from mesh.core.db import transaction
from mesh.core.repos import submission_git_info
from mesh.core.workspaces import (
    WorkspaceError,
    compute_task_workspace_path,
    get_workspaces_root,
    plan_code_workspace,
    run_code_workspace_plan,
)
from mesh.daemon.handlers.base import Effect, HandlerContext, HandlerResult
from mesh.daemon.scheduler import compute_queue_info, fail_queued_task_t6, promote_tasks
from mesh.errors import MeshError

PROFILE_RANKS: Dict[str, int] = {
    "readonly": 0,
    "edit": 1,
    "dev": 2,
    "elevated": 3,
    "unrestricted": 4,
}


def _get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    """Fetch task by ID or raise NOT_FOUND."""
    cur = conn.execute(
        """SELECT t.*, p.name as assignee_name
           FROM tasks t
           LEFT JOIN principals p ON t.assignee_agent_id = p.id
           WHERE t.id = ?;""",
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{task_id}' not found",
            details={"task_id": task_id},
        )
    return row


def _format_task(
    row: sqlite3.Row,
    conn: Optional[sqlite3.Connection] = None,
    depends_on: Optional[List[str]] = None,
    depended_on_by: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Format task row into API response dictionary."""
    deliverable = None
    submitted_by = None
    submitted_at = None
    if row["deliverable_json"]:
        try:
            d_raw = json.loads(row["deliverable_json"])
            if isinstance(d_raw, dict) and "deliverable" in d_raw and "submitted_by" in d_raw:
                deliverable = d_raw["deliverable"]
                submitted_by = d_raw["submitted_by"]
                submitted_at = d_raw.get("submitted_at")
            else:
                deliverable = d_raw
        except Exception:
            pass

    failure = None
    if row["failure_json"]:
        try:
            failure = json.loads(row["failure_json"])
        except Exception:
            pass

    merge = None
    if row["merge_json"]:
        try:
            merge = json.loads(row["merge_json"])
        except Exception:
            pass

    assignee_name = row["assignee_name"] if "assignee_name" in row.keys() and row["assignee_name"] else None

    queue_position = None
    blocked_reason = None
    if row["status"] == "queued" and conn is not None:
        queue_position, blocked_reason = compute_queue_info(conn, row["id"])

    history: List[Dict[str, Any]] = []
    if conn is not None:
        cur_ev = conn.execute(
            """SELECT seq, ts, type, reason, payload_json
               FROM events
               WHERE task_id = ?
                 AND type IN ('task.failed', 'task.rejected', 'task.retried', 'task.reassigned', 'task.escalated')
               ORDER BY seq ASC;""",
            (row["id"],),
        )
        for erow in cur_ev.fetchall():
            att = None
            if erow["payload_json"]:
                try:
                    pld = json.loads(erow["payload_json"])
                    att = pld.get("attempt")
                except Exception:
                    pass
            history.append({
                "ts": erow["ts"],
                "type": erow["type"],
                "reason": erow["reason"],
                "attempt": att,
            })

    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "title": row["title"],
        "spec": row["spec"],
        "kind": row["kind"],
        "status": row["status"],
        "priority": row["priority"],
        "assignee": assignee_name or row["assignee_agent_id"],
        "assignee_agent_id": row["assignee_agent_id"],
        "model": row["model"],
        "effort": row["effort"],
        "profile": row["profile"],
        "timeout_s": row["timeout_s"],
        "idle_timeout_s": row["idle_timeout_s"],
        "max_attempts": row["max_attempts"],
        "attempt": row["attempt"],
        "fence_seq": row["fence_seq"],
        "queued_at": row["queued_at"],
        "offered_at": row["offered_at"],
        "queue_position": queue_position,
        "blocked_reason": blocked_reason,
        "workspace_path": row["workspace_path"],
        "branch": row["branch"],
        "base_commit": row["base_commit"],
        "last_session_id": row["last_session_id"],
        "deliverable": deliverable,
        "submitted_by": submitted_by,
        "submitted_at": submitted_at,
        "failure": failure,
        "merge": merge,
        "created_by": row["created_by"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "depends_on": depends_on if depends_on is not None else [],
        "depended_on_by": depended_on_by if depended_on_by is not None else [],
        "history": history,
    }


def _validate_profile_and_effort(profile_override: Optional[str], effort_override: Optional[str]) -> None:
    """Validate profile and effort overrides and check profile/effort swap (§9.1)."""
    valid_profiles = set(PROFILE_RANKS.keys())
    valid_efforts = {"low", "medium", "high", "xhigh"}
    if profile_override is not None:
        if profile_override in valid_efforts:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Effort level '{profile_override}' cannot be used as a permission profile",
                details={"param": "profile", "value": profile_override},
            )
        if profile_override not in valid_profiles:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid profile '{profile_override}': must be one of {sorted(valid_profiles)}",
                details={"param": "profile", "value": profile_override},
            )

    if effort_override is not None:
        if effort_override in valid_profiles:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Permission profile '{effort_override}' cannot be used as an effort level",
                details={"param": "effort", "value": effort_override},
            )


def _validate_model_and_effort(
    agent: sqlite3.Row,
    model_override: Optional[str],
    effort_override: Optional[str],
    current_model: Optional[str] = None,
    current_effort: Optional[str] = None,
    testing_mode: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """Validate model and effort overrides against agent allowed lists and adapter capabilities (AG Step 5)."""
    allowed_models = []
    if agent["allowed_models_json"]:
        try:
            allowed_models = json.loads(agent["allowed_models_json"])
        except Exception:
            pass

    resolved_model = model_override or current_model or agent["default_model"]
    if model_override:
        if model_override not in allowed_models:
            raise MeshError(
                code=c.MODEL_NOT_ALLOWED,
                exit_code=c.EXIT_USAGE,
                message=f"Model '{model_override}' is not allowed for agent '{agent['name']}'",
                details={"model": model_override, "allowed": allowed_models},
            )

    allowed_efforts = []
    if agent["allowed_efforts_json"]:
        try:
            allowed_efforts = json.loads(agent["allowed_efforts_json"])
        except Exception:
            pass

    resolved_effort = effort_override or current_effort or agent["default_effort"]
    if effort_override:
        # Check adapter effort capability
        adapter_inst = get_adapter(agent["adapter"], testing_mode=testing_mode)
        effort_cap = adapter_inst.capabilities.get("reasoning_effort")
        if effort_cap and effort_cap.supported == "no":
            raise EffortUnsupported(agent["adapter"])

        if effort_override not in allowed_efforts:
            raise MeshError(
                code=c.EFFORT_NOT_ALLOWED,
                exit_code=c.EXIT_USAGE,
                message=f"Effort '{effort_override}' is not allowed for agent '{agent['name']}'",
                details={"effort": effort_override, "allowed": allowed_efforts},
            )

    return resolved_model, resolved_effort


def _validate_assignment_guards(
    conn: sqlite3.Connection,
    ctx: HandlerContext,
    project_id: str,
    assignee_ident: str,
    profile_override: Optional[str] = None,
    model_override: Optional[str] = None,
    effort_override: Optional[str] = None,
    task_title: str = "",
    op: str = "task.create",
) -> Tuple[sqlite3.Row, str, Optional[str], Optional[str]]:
    """Execute Assignment Guards (AG) 1-6 in order per plan §6.2.

    Returns:
        (agent_row, resolved_profile, resolved_model, resolved_effort)
    """
    # 1. Agent exists and is enabled; not current lead of project
    cur = conn.execute(
        """SELECT a.*, p.name, p.disabled_at
           FROM agents a
           JOIN principals p ON a.id = p.id
           WHERE a.id = ? OR p.name = ?;""",
        (assignee_ident, assignee_ident),
    )
    agent = cur.fetchone()
    if not agent:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Agent '{assignee_ident}' not found",
            details={"agent": assignee_ident},
        )
    if agent["disabled_at"]:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{agent['name']}' is disabled",
            details={"agent": agent["name"], "reason": "disabled"},
        )

    # Check if agent is current lead (ACC: E9)
    cur_lead = conn.execute(
        "SELECT agent_id FROM lead_assignments WHERE project_id = ? AND ended_at IS NULL LIMIT 1;",
        (project_id,),
    )
    lead_row = cur_lead.fetchone()
    if lead_row and lead_row["agent_id"] == agent["id"]:
        raise MeshError(
            code=c.LEAD_CANNOT_BE_ASSIGNEE,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{agent['name']}' is the current lead of project '{project_id}' and cannot be assignee",
            details={"agent": agent["name"], "project_id": project_id},
        )

    # Validate profile and effort overrides and check profile/effort swap (§9.1)
    _validate_profile_and_effort(profile_override, effort_override)

    resolved_profile = profile_override or agent["default_profile"] or "edit"
    delivery = agent["delivery"]

    # Steps 2-4: only for delivery == launch (inbox agents skip 2-4)
    if delivery == "launch":
        adapter = get_adapter(agent["adapter"], testing_mode=ctx.testing_mode)

        # 2. Adapter installed
        if adapter.kind == "test_double" and not ctx.testing_mode:
            raise MeshError(
                code=c.TEST_DOUBLE_DISABLED,
                exit_code=c.EXIT_ADAPTER_UNAVAILABLE,
                message="Test double adapter 'fake' is only available when MESH_TESTING=1",
            )
        if not adapter.is_installed():
            raise AdapterNotInstalled(agent["adapter"])

        # 3. Profile supported by adapter (ACC: N5)
        try:
            adapter.profile_flags(resolved_profile)
        except ProfileUnsupported:
            raise ProfileUnsupported(agent["adapter"], resolved_profile)

        # 4. Effective launch_enabled from probe + smoke evidence (ACC: N4, N19)
        from mesh.adapters.evidence import effective_launch

        ev = effective_launch(ctx.conn, adapter, ctx.testing_mode, agent["binary_path"])
        if not ev.ok:
            raise AdapterUnsupported(
                f"Adapter '{agent['adapter']}' is not launch-enabled in this milestone"
                + (f" ({ev.reason})" if ev.reason else ""),
                milestone=ev.milestone or adapter.launch_milestone or "M3",
                reason=ev.reason,
            )

    # 5. Model and effort resolve and are allowed (ACC: D3, D7)
    resolved_model, resolved_effort = _validate_model_and_effort(
        agent=agent,
        model_override=model_override,
        effort_override=effort_override,
        testing_mode=ctx.testing_mode,
    )

    # 6. Profile rank vs max_unapproved_profile (ACC: D16)
    max_unapproved = agent["max_unapproved_profile"] or "dev"
    if PROFILE_RANKS.get(resolved_profile, 0) > PROFILE_RANKS.get(max_unapproved, 2):
        if ctx.actor_kind != "user" and ctx.via != "direct":
            if not ctx.approval:
                apr_info = create_or_get_pending_approval(
                    conn=conn,
                    category="permission_escalation",
                    op=op,
                    requested_by=ctx.principal["id"],
                    params=ctx.args,
                    summary=f"Permission escalation to '{resolved_profile}' for task '{task_title}' on agent '{agent['name']}'",
                    project_id=project_id,
                    dry_run=ctx.dry_run,
                    request_id=ctx.request_id,
                    k_user=ctx.k_user,
                )
                raise MeshError(
                    code=c.APPROVAL_REQUIRED,
                    exit_code=c.EXIT_APPROVAL_REQUIRED,
                    message=f"Profile '{resolved_profile}' requires user approval (category: permission_escalation)",
                    details=apr_info,
                )
            else:
                check_and_consume_approval(
                    conn=conn,
                    approval_id=ctx.approval,
                    op=op,
                    category="permission_escalation",
                    caller_id=ctx.principal["id"],
                    params=ctx.args,
                    project_id=project_id,
                    request_id=ctx.request_id,
                )

    return agent, resolved_profile, resolved_model, resolved_effort


def handle_task_create(ctx: HandlerContext) -> HandlerResult:
    """Create a new task under a project per plan §6.2 (T1/T2)."""
    project_id = ctx.args.get("project")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter --project",
            details={"param": "project"},
        )

    # Check project exists
    cur_p = ctx.conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,))
    project = cur_p.fetchone()
    if not project:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    # ACC: E18 - task create on completed or cancelled project returns rc 6 PROJECT_NOT_ACTIVE
    p_status = project["status"]
    if p_status in ("completed", "cancelled"):
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {p_status}, not active",
            details={"project_id": project_id, "status": p_status},
        )

    check_authz(ctx.conn, "task.create", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    title = ctx.args.get("title")
    if not title or not str(title).strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Parameter --title must be non-empty",
            details={"param": "title"},
        )
    title = str(title).strip()

    spec = ctx.args.get("spec")
    if not spec or not str(spec).strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Parameter --spec must be non-empty",
            details={"param": "spec"},
        )
    spec = str(spec).strip()

    # Priority
    priority = 0
    if "priority" in ctx.args and ctx.args["priority"] is not None:
        try:
            priority = int(ctx.args["priority"])
        except (ValueError, TypeError):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid priority: {ctx.args['priority']}",
                details={"param": "priority"},
            )

    # Timeout
    min_timeout = 1 if ctx.testing_mode else 60
    timeout_s = None
    if "timeout" in ctx.args and ctx.args["timeout"] is not None:
        raw_to = ctx.args["timeout"]
        try:
            if isinstance(raw_to, str) and raw_to.endswith("s"):
                timeout_s = int(raw_to[:-1])
            else:
                timeout_s = int(raw_to)
        except (ValueError, TypeError):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid timeout: {raw_to}",
                details={"param": "timeout"},
            )
        if not (min_timeout <= timeout_s <= 604800):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"timeout must be between {min_timeout} and 604800 seconds; got {timeout_s}",
                details={"param": "timeout", "expected": f"{min_timeout}..604800", "got": timeout_s},
            )
    else:
        # Default from settings
        cur_to = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'task.default_timeout_s';")
        to_row = cur_to.fetchone()
        timeout_s = int(json.loads(to_row["value_json"])) if to_row else 3600

    # Idle timeout
    idle_timeout_s = 0
    if "idle_timeout" in ctx.args and ctx.args["idle_timeout"] is not None:
        raw_ito = ctx.args["idle_timeout"]
        try:
            if isinstance(raw_ito, str) and raw_ito.endswith("s"):
                idle_timeout_s = int(raw_ito[:-1])
            else:
                idle_timeout_s = int(raw_ito)
        except (ValueError, TypeError):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid idle_timeout: {raw_ito}",
                details={"param": "idle_timeout"},
            )
        if idle_timeout_s != 0 and not (min_timeout <= idle_timeout_s <= 604800):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"idle_timeout must be 0 or between {min_timeout} and 604800 seconds; got {idle_timeout_s}",
                details={"param": "idle_timeout", "expected": f"0|{min_timeout}..604800", "got": idle_timeout_s},
            )

    # Max attempts (ACC: H9 cap 10)
    cur_cap = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'task.max_attempts_cap';")
    cap_row = cur_cap.fetchone()
    cap = int(json.loads(cap_row["value_json"])) if cap_row else 10

    max_attempts = None
    if "max_attempts" in ctx.args and ctx.args["max_attempts"] is not None:
        try:
            max_attempts = int(ctx.args["max_attempts"])
        except (ValueError, TypeError):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid max_attempts: {ctx.args['max_attempts']}",
                details={"param": "max_attempts"},
            )
        if not (1 <= max_attempts <= cap):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"max_attempts must be between 1 and {cap}; got {max_attempts}",
                details={"param": "max_attempts", "expected": f"1..{cap}", "got": max_attempts},
            )
    else:
        cur_dma = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'task.default_max_attempts';")
        dma_row = cur_dma.fetchone()
        max_attempts = int(json.loads(dma_row["value_json"])) if dma_row else 3

    # Kind: code or general
    kind = ctx.args.get("kind")
    if kind:
        if kind not in ("code", "general"):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid task kind '{kind}'; must be 'code' or 'general'",
                details={"param": "kind", "value": kind},
            )
    else:
        kind = "code" if project["repo_path"] else "general"
    if kind == "code" and not project["repo_path"]:
        # Q13: a code task needs a repo project (its worktree comes from the repo, §10.3).
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Task kind 'code' needs a repo project; project '{project['id']}' has no repo",
            details={"param": "kind", "value": kind, "reason": "no_repo"},
        )

    # 1. Dependencies check (validated before AG per plan §6.2)
    deps = ctx.args.get("depends_on") or []
    if isinstance(deps, str):
        deps = [deps]
    # Deduplicate preserving order
    clean_deps: List[str] = []
    for d in deps:
        if d and d not in clean_deps:
            clean_deps.append(d)

    for dep_id in clean_deps:
        cur_dep = ctx.conn.execute(
            "SELECT id, project_id FROM tasks WHERE id = ?;",
            (dep_id,),
        )
        dep_row = cur_dep.fetchone()
        if not dep_row:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Prerequisite dependency task '{dep_id}' not found",
                details={"task_id": dep_id},
            )
        if dep_row["project_id"] != project_id:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Dependency task '{dep_id}' belongs to project '{dep_row['project_id']}', not '{project_id}'",
                details={"task_id": dep_id, "project_id": project_id},
            )

    # 2. Assignee & assignment guards
    assignee_ident = ctx.args.get("assignee")
    agent_id = None
    agent_name = None
    profile = ctx.args.get("profile") or "edit"
    model = ctx.args.get("model")
    effort = ctx.args.get("effort")

    if assignee_ident:
        agent_row, profile, model, effort = _validate_assignment_guards(
            conn=ctx.conn,
            ctx=ctx,
            project_id=project_id,
            assignee_ident=assignee_ident,
            profile_override=ctx.args.get("profile"),
            model_override=ctx.args.get("model"),
            effort_override=ctx.args.get("effort"),
            task_title=title,
            op="task.create",
        )
        agent_id = agent_row["id"]
        agent_name = agent_row["name"]
        initial_status = "assigned"
        event_type = "task.assigned"
    else:
        # Validate profile and effort overrides even when unassigned
        profile_override = ctx.args.get("profile")
        effort_override = ctx.args.get("effort")
        _validate_profile_and_effort(profile_override, effort_override)
        initial_status = "created"
        event_type = "task.created"

    now = now_iso_utc()
    task_id = generate_id("tsk")
    workspace_path = os.path.join(ctx.home, "workspaces", project_id, "tasks", task_id)

    ctx.conn.execute(
        """INSERT INTO tasks (
            id, project_id, title, spec, kind, status, priority,
            assignee_agent_id, model, effort, profile, timeout_s,
            idle_timeout_s, max_attempts, attempt, fence_seq,
            queued_at, offered_at, workspace_path, branch, base_commit,
            last_session_id, deliverable_json, failure_json, merge_json,
            created_by, version, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, 0, 0,
            NULL, NULL, ?, NULL, NULL,
            NULL, NULL, NULL, NULL,
            ?, 1, ?, ?
        );""",
        (
            task_id, project_id, title, spec, kind, initial_status, priority,
            agent_id, model, effort, profile, timeout_s,
            idle_timeout_s, max_attempts, workspace_path,
            ctx.principal["id"], now, now,
        ),
    )

    for dep_id in clean_deps:
        ctx.conn.execute(
            "INSERT INTO task_deps (task_id, depends_on_task_id) VALUES (?, ?);",
            (task_id, dep_id),
        )

    record_event(
        conn=ctx.conn,
        type_=event_type,
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=None,
        to_state=initial_status,
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "title": title,
            "assignee": agent_name,
            "profile": profile,
            "model": model,
            "effort": effort,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    task_row = _get_task(ctx.conn, task_id)
    return HandlerResult(
        data=_format_task(task_row, conn=ctx.conn, depends_on=clean_deps, depended_on_by=[]),
        events=[event_type],
        transitions=[{"entity": "task", "id": task_id, "from": None, "to": initial_status}],
    )


def handle_task_assign(ctx: HandlerContext) -> HandlerResult:
    """Assign an unassigned task to an agent per plan §6.2 (T3)."""
    task_id = ctx.args.get("task_id")
    to_agent = ctx.args.get("to")
    if not task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter task_id",
            details={"param": "task_id"},
        )
    if not to_agent:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter --to",
            details={"param": "to"},
        )

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.assign", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check transition: T3 is from created to assigned
    check_task_transition(task["status"], "assigned")

    agent_row, profile, model, effort = _validate_assignment_guards(
        conn=ctx.conn,
        ctx=ctx,
        project_id=project_id,
        assignee_ident=to_agent,
        profile_override=ctx.args.get("profile") or task["profile"],
        model_override=ctx.args.get("model") or task["model"],
        effort_override=ctx.args.get("effort") or task["effort"],
        task_title=task["title"],
        op="task.assign",
    )

    now = now_iso_utc()
    ctx.conn.execute(
        """UPDATE tasks
           SET assignee_agent_id = ?, status = 'assigned', profile = ?, model = ?, effort = ?, updated_at = ?, version = version + 1
           WHERE id = ?;""",
        (agent_row["id"], profile, model, effort, now, task_id),
    )

    record_event(
        conn=ctx.conn,
        type_="task.assigned",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="assigned",
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "assignee": agent_row["name"],
            "profile": profile,
            "model": model,
            "effort": effort,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    updated_row = _get_task(ctx.conn, task_id)
    cur_deps = ctx.conn.execute("SELECT depends_on_task_id FROM task_deps WHERE task_id = ?;", (task_id,))
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn, depends_on=depends_on),
        events=["task.assigned"],
        transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": "assigned"}],
    )


def handle_task_show(ctx: HandlerContext) -> HandlerResult:
    """Show details of a task."""
    task_id = ctx.args.get("task_id")
    if not task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter task_id",
            details={"param": "task_id"},
        )

    row = _get_task(ctx.conn, task_id)
    project_id = row["project_id"]
    check_authz(ctx.conn, "task.show", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    cur_deps = ctx.conn.execute(
        "SELECT depends_on_task_id FROM task_deps WHERE task_id = ? ORDER BY depends_on_task_id;",
        (task_id,),
    )
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    cur_dep_by = ctx.conn.execute(
        "SELECT task_id FROM task_deps WHERE depends_on_task_id = ? ORDER BY task_id;",
        (task_id,),
    )
    depended_on_by = [r["task_id"] for r in cur_dep_by.fetchall()]

    return HandlerResult(data=_format_task(row, conn=ctx.conn, depends_on=depends_on, depended_on_by=depended_on_by))


def handle_task_list(ctx: HandlerContext) -> HandlerResult:
    """List tasks with optional filters."""
    project_id = ctx.args.get("project") or ctx.args.get("project_id")
    check_authz(ctx.conn, "task.list", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    query = """
        SELECT t.*, p.name as assignee_name
        FROM tasks t
        LEFT JOIN principals p ON t.assignee_agent_id = p.id
        WHERE 1=1
    """
    params: List[Any] = []

    project_id = ctx.args.get("project") or ctx.args.get("project_id")
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        # Review m2-r9 finding 1(a): a lead run token lists only its own project's tasks.
        project_id = scope_project
    if project_id:
        query += " AND t.project_id = ?"
        params.append(project_id)

    status = ctx.args.get("status")
    if status:
        query += " AND t.status = ?"
        params.append(status)

    assignee = ctx.args.get("assignee")
    if assignee:
        query += " AND (t.assignee_agent_id = ? OR p.name = ?)"
        params.extend([assignee, assignee])

    caps = ctx.principal.get("capabilities", [])
    if not caps and ctx.principal.get("id"):
        caps = get_agent_capabilities(ctx.conn, ctx.principal["id"])
    if scope_project is None and ctx.actor_kind != "user" and "relay" not in caps:
        agent_id = ctx.principal.get("id")
        query += """ AND (
            t.assignee_agent_id = ?
            OR t.project_id IN (
                SELECT project_id FROM lead_assignments WHERE agent_id = ?
                UNION
                SELECT id FROM projects WHERE origin_principal_id = ?
            )
        )"""
        params.extend([agent_id, agent_id, agent_id])

    query += " ORDER BY t.priority DESC, t.created_at ASC;"

    cur = ctx.conn.execute(query, params)
    items = []
    for row in cur.fetchall():
        items.append(_format_task(row, conn=ctx.conn))

    return HandlerResult(data={"items": items})


def handle_task_cancel(ctx: HandlerContext) -> HandlerResult:
    """Cancel a task per plan §6.2 (T22)."""
    task_id = ctx.args.get("task_id")
    reason = ctx.args.get("reason")
    if not task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter task_id",
            details={"param": "task_id"},
        )
    if not reason or not str(reason).strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Parameter --reason is required and non-empty for task cancellation",
            details={"param": "reason"},
        )
    reason = str(reason).strip()

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.cancel", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check if task is terminal (e.g. accepted general task, merged, cancelled)
    if is_task_terminal(task["status"], task["kind"]):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' in state '{task['status']}' is terminal and cannot be cancelled",
            details={"task_id": task_id, "status": task["status"], "kind": task["kind"]},
        )

    lease = ctx.conn.execute(
        "SELECT * FROM leases WHERE key = ?;", (f"task:{task_id}",)
    ).fetchone()
    if lease is not None:
        # T9: a task that holds a lease (running, or submitted in post-submit grace)
        # goes through `cancelling` so the run is always terminated first (§6.2, §11.4).
        check_task_transition(task["status"], "cancelling")
        begin_task_cancel(ctx.conn, task, lease, reason, ctx)
        updated_row = _get_task(ctx.conn, task_id)
        return HandlerResult(
            data=_format_task(updated_row, conn=ctx.conn),
            events=["task.cancelling"] + (["task.cancelled"] if updated_row["status"] == "cancelled" else []),
            transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": updated_row["status"]}],
        )

    # Check transition (T22)
    check_task_transition(task["status"], "cancelled")

    now = now_iso_utc()
    _supersede_offers(ctx.conn, task)

    ctx.conn.execute(
        "UPDATE tasks SET status = 'cancelled', updated_at = ?, version = version + 1 WHERE id = ?;",
        (now, task_id),
    )

    record_event(
        conn=ctx.conn,
        type_="task.cancelled",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="cancelled",
        reason=reason,
        payload={"task_id": task_id, "project_id": project_id, "reason": reason},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    emit_dependents_blocked(ctx.conn, task_id, ctx.request_id)

    updated_row = _get_task(ctx.conn, task_id)
    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn),
        events=["task.cancelled"],
        transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": "cancelled"}],
    )


def _supersede_offers(conn: Any, task: Any) -> None:
    if task["assignee_agent_id"]:
        conn.execute(
            """UPDATE outbox
               SET status = 'superseded'
               WHERE recipient_id = ?
                 AND type = 'task.offered'
                 AND json_extract(payload_json, '$.task_id') = ?
                 AND status IN ('pending', 'delivered');""",
            (task["assignee_agent_id"], task["id"]),
        )


def begin_task_cancel(conn: Any, task: Any, lease: Any, reason: str, ctx: Optional[HandlerContext]) -> None:
    """T9 (and for inbox mode T10 at once) for a task that holds a lease.

    Launch mode: the task becomes `cancelling`; the supervisor terminates the run
    (§11.4) and applies T10 once the release condition holds. Inbox mode: Mesh
    cannot stop the agent, so the fence is invalidated at once: the lease is
    deleted, the external run ends `killed` with end_reason = the cancel reason
    (`cancelled`, or `project_cancelled` from P5; review m1c-run3 #5) and the
    task goes straight on to `cancelled` (T10, "inbox mode: immediately").
    """
    now = now_iso_utc()
    actor = dict(
        actor_id=ctx.principal["id"] if ctx else "system",
        actor_kind=ctx.actor_kind if ctx else "system",
        via=ctx.via if ctx else "system",
        relayed_by=ctx.relayed_by if ctx else None,
    )
    request_id = ctx.request_id if ctx else None
    conn.execute(
        "UPDATE tasks SET status = 'cancelling', updated_at = ?, version = version + 1 WHERE id = ?;",
        (now, task["id"]),
    )
    record_event(
        conn=conn,
        type_="task.cancelling",
        project_id=task["project_id"],
        task_id=task["id"],
        run_id=lease["run_id"],
        from_state=task["status"],
        to_state="cancelling",
        reason=reason,
        payload={"task_id": task["id"], "project_id": task["project_id"], "run_id": lease["run_id"], "reason": reason},
        request_id=request_id,
        k_user=ctx.k_user if ctx else None,
        **actor,
    )
    if lease["holder_kind"] == "meshd":
        return
    conn.execute("DELETE FROM leases WHERE key = ?;", (f"task:{task['id']}",))
    conn.execute(
        "UPDATE runs SET status = 'killed', end_reason = ?, ended_at = ? "
        "WHERE id = ? AND status IN ('starting', 'running');",
        ("project_cancelled" if reason == "project_cancelled" else "cancelled", now, lease["run_id"]),
    )
    conn.execute(
        "UPDATE tasks SET status = 'cancelled', updated_at = ?, version = version + 1 WHERE id = ?;",
        (now, task["id"]),
    )
    record_event(
        conn=conn,
        type_="task.cancelled",
        actor_id="system",
        actor_kind="system",
        via="system",
        project_id=task["project_id"],
        task_id=task["id"],
        run_id=lease["run_id"],
        from_state="cancelling",
        to_state="cancelled",
        reason=reason,
        payload={"task_id": task["id"], "project_id": task["project_id"], "run_id": lease["run_id"], "reason": reason},
        request_id=request_id,
    )
    emit_dependents_blocked(conn, task["id"], request_id)


def emit_dependents_blocked(conn: Any, task_id: str, request_id: Optional[str]) -> None:
    """Emit task.blocked for dependents in 'assigned' of a cancelled task (plan §6.2)."""
    cur_deps = conn.execute(
        """SELECT t.id, t.project_id
           FROM tasks t
           JOIN task_deps td ON t.id = td.task_id
           WHERE td.depends_on_task_id = ? AND t.status = 'assigned';""",
        (task_id,),
    )
    for dep_row in cur_deps.fetchall():
        record_event(
            conn=conn,
            type_="task.blocked",
            actor_id="system",
            actor_kind="system",
            via="system",
            project_id=dep_row["project_id"],
            task_id=dep_row["id"],
            payload={
                "task_id": dep_row["id"],
                "blocked_by": task_id,
                "reason": f"Prerequisite dependency '{task_id}' was cancelled",
            },
            request_id=request_id,
        )


def handle_task_reassign(ctx: HandlerContext) -> HandlerResult:
    """Reassign a task to a different agent per plan §6.2 (T20)."""
    task_id = ctx.args.get("task_id")
    to_agent = ctx.args.get("to")
    reason = ctx.args.get("reason")
    reuse_workspace = bool(ctx.args.get("reuse_workspace", False))

    if not task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter task_id",
            details={"param": "task_id"},
        )
    if not to_agent:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter --to",
            details={"param": "to"},
        )
    if not reason or not str(reason).strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Parameter --reason is required and non-empty for task reassignment",
            details={"param": "reason"},
        )
    reason = str(reason).strip()

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.reassign", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check transition: T20 is from assigned, queued, rework, failed, escalated, merge_failed -> assigned
    check_task_transition(task["status"], "assigned")

    # If coming from failed or escalated, attempt rule is same as T19 (retry_over_limit)
    if task["status"] in ("failed", "escalated") and task["attempt"] >= task["max_attempts"]:
        if ctx.actor_kind != "user" and ctx.via != "direct":
            if not ctx.approval:
                apr_info = create_or_get_pending_approval(
                    conn=ctx.conn,
                    category="retry_over_limit",
                    op="task.reassign",
                    requested_by=ctx.principal["id"],
                    params=ctx.args,
                    summary=f"Reassignment of task '{task['title']}' at attempt cap ({task['attempt']}/{task['max_attempts']}) requires user approval",
                    project_id=project_id,
                    task_id=task_id,
                    dry_run=ctx.dry_run,
                    request_id=ctx.request_id,
                    k_user=ctx.k_user,
                )
                raise MeshError(
                    code=c.APPROVAL_REQUIRED,
                    exit_code=c.EXIT_APPROVAL_REQUIRED,
                    message=f"Task reassignment exceeds max attempts ({task['max_attempts']}) and requires user approval (category: retry_over_limit)",
                    details=apr_info,
                )
            else:
                check_and_consume_approval(
                    conn=ctx.conn,
                    approval_id=ctx.approval,
                    op="task.reassign",
                    category="retry_over_limit",
                    caller_id=ctx.principal["id"],
                    params=ctx.args,
                    project_id=project_id,
                    request_id=ctx.request_id,
                )

    # Validate assignment guards for new assignee
    agent_row, profile, model, effort = _validate_assignment_guards(
        conn=ctx.conn,
        ctx=ctx,
        project_id=project_id,
        assignee_ident=to_agent,
        profile_override=task["profile"],
        model_override=None,
        effort_override=None,
        task_title=task["title"],
        op="task.reassign",
    )

    old_assignee_name = task["assignee_name"]
    now = now_iso_utc()

    # On T20 from queued, supersede old assignee's unacked task.offered rows for that task
    if task["status"] == "queued" and task["assignee_agent_id"]:
        ctx.conn.execute(
            """UPDATE outbox
               SET status = 'superseded'
               WHERE recipient_id = ?
                 AND type = 'task.offered'
                 AND json_extract(payload_json, '$.task_id') = ?
                 AND status IN ('pending', 'delivered');""",
            (task["assignee_agent_id"], task_id),
        )

    # Fresh workspace after an inbox lease expiry (§6.2 T20)
    cur_prev_run = ctx.conn.execute(
        "SELECT end_reason FROM runs WHERE task_id = ? ORDER BY rowid DESC LIMIT 1;",
        (task_id,),
    )
    prev_run = cur_prev_run.fetchone()
    prev_lease_expired = bool(prev_run and prev_run["end_reason"] == "lease_expired")

    workspace_choice = None
    new_ws_path = task["workspace_path"]
    if prev_lease_expired:
        workspace_choice = "reused" if reuse_workspace else "fresh"
        if workspace_choice == "fresh":
            next_attempt = max(1, task["attempt"]) + 1
            ws_root = get_workspaces_root(ctx.conn, ctx.home)
            new_ws_path = compute_task_workspace_path(ws_root, project_id, task_id, attempt=next_attempt)

    ctx.conn.execute(
        """UPDATE tasks
           SET assignee_agent_id = ?, status = 'assigned', offered_at = NULL, profile = ?, model = ?, effort = ?,
               workspace_path = ?, updated_at = ?, version = version + 1
           WHERE id = ?;""",
        (agent_row["id"], profile, model, effort, new_ws_path, now, task_id),
    )

    reassign_payload: Dict[str, Any] = {
        "task_id": task_id,
        "project_id": project_id,
        "assignee": agent_row["name"],
        "old_assignee": old_assignee_name,
        "reason": reason,
        "profile": profile,
        "model": model,
        "effort": effort,
        "reuse_workspace": reuse_workspace,
    }
    if workspace_choice:
        reassign_payload["workspace"] = workspace_choice
        reassign_payload["workspace_path"] = new_ws_path

    record_event(
        conn=ctx.conn,
        type_="task.reassigned",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="assigned",
        reason=reason,
        payload=reassign_payload,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    updated_row = _get_task(ctx.conn, task_id)
    cur_deps = ctx.conn.execute("SELECT depends_on_task_id FROM task_deps WHERE task_id = ?;", (task_id,))
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn, depends_on=depends_on),
        events=["task.reassigned"],
        transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": "assigned"}],
    )


def _requeue_launch_guard(ctx: HandlerContext, assignee_id: Optional[str]) -> None:
    """Evidence check for paths that requeue a task without AG (retry T19, reject T12 -> rework,
    which the scheduler requeues). Same rule as AG step 4 (ACC: N19; Kiro r5 finding 1): a
    launch-delivery assignee whose adapter has no matching smoke evidence is refused with
    ``ADAPTER_UNSUPPORTED`` before any write or approval use."""
    if not assignee_id:
        return
    agent = ctx.conn.execute("SELECT * FROM agents WHERE id = ?;", (assignee_id,)).fetchone()
    if agent is None or agent["delivery"] != "launch":
        return
    from mesh.adapters.evidence import effective_launch

    adapter = get_adapter(agent["adapter"], testing_mode=ctx.testing_mode)
    ev = effective_launch(ctx.conn, adapter, ctx.testing_mode, agent["binary_path"])
    if not ev.ok:
        raise AdapterUnsupported(
            f"Adapter '{agent['adapter']}' is not launch-enabled in this milestone"
            + (f" ({ev.reason})" if ev.reason else ""),
            milestone=ev.milestone or adapter.launch_milestone or "M3",
            reason=ev.reason,
        )


def handle_task_retry(ctx: HandlerContext) -> HandlerResult:
    """Retry a failed or escalated task per plan §6.2 (T19)."""
    task_id = ctx.args.get("task_id")
    model_override = ctx.args.get("model")
    effort_override = ctx.args.get("effort")
    reason = ctx.args.get("reason")
    reuse_workspace = bool(ctx.args.get("reuse_workspace", False))

    if not task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter task_id",
            details={"param": "task_id"},
        )

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.retry", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check transition: T19 is from failed, escalated -> queued
    check_task_transition(task["status"], "queued")

    # Launch evidence, as AG step 4 (ACC: N19), before any write or approval use
    _requeue_launch_guard(ctx, task["assignee_agent_id"])

    # Attempt cap check (retry_over_limit)
    if task["attempt"] >= task["max_attempts"]:
        if ctx.actor_kind != "user" and ctx.via != "direct":
            if not ctx.approval:
                apr_info = create_or_get_pending_approval(
                    conn=ctx.conn,
                    category="retry_over_limit",
                    op="task.retry",
                    requested_by=ctx.principal["id"],
                    params=ctx.args,
                    summary=f"Retry of task '{task['title']}' at attempt cap ({task['attempt']}/{task['max_attempts']}) requires user approval",
                    project_id=project_id,
                    task_id=task_id,
                    dry_run=ctx.dry_run,
                    request_id=ctx.request_id,
                    k_user=ctx.k_user,
                )
                raise MeshError(
                    code=c.APPROVAL_REQUIRED,
                    exit_code=c.EXIT_APPROVAL_REQUIRED,
                    message=f"Task retry exceeds max attempts ({task['max_attempts']}) and requires user approval (category: retry_over_limit)",
                    details=apr_info,
                )
            else:
                check_and_consume_approval(
                    conn=ctx.conn,
                    approval_id=ctx.approval,
                    op="task.retry",
                    category="retry_over_limit",
                    caller_id=ctx.principal["id"],
                    params=ctx.args,
                    project_id=project_id,
                    request_id=ctx.request_id,
                )

    # Validate model and effort overrides
    _validate_profile_and_effort(profile_override=None, effort_override=effort_override)

    assignee_id = task["assignee_agent_id"]
    resolved_model = model_override or task["model"]
    resolved_effort = effort_override or task["effort"]

    if assignee_id and (model_override or effort_override):
        cur_ag = ctx.conn.execute("SELECT * FROM agents WHERE id = ?;", (assignee_id,))
        ag_row = cur_ag.fetchone()
        if ag_row:
            resolved_model, resolved_effort = _validate_model_and_effort(
                agent=ag_row,
                model_override=model_override,
                effort_override=effort_override,
                current_model=task["model"],
                current_effort=task["effort"],
                testing_mode=ctx.testing_mode,
            )

    is_inbox = False
    if assignee_id:
        cur_d = ctx.conn.execute("SELECT delivery FROM agents WHERE id = ?;", (assignee_id,))
        d_row = cur_d.fetchone()
        if d_row and d_row["delivery"] == "inbox":
            is_inbox = True

    now = now_iso_utc()
    if assignee_id:
        ctx.conn.execute(
            """UPDATE outbox
               SET status = 'superseded'
               WHERE recipient_id = ?
                 AND type = 'task.offered'
                 AND json_extract(payload_json, '$.task_id') = ?
                 AND status IN ('pending', 'delivered');""",
            (assignee_id, task_id),
        )

    # Fresh workspace after an inbox lease expiry (§6.2 T19)
    cur_prev_run = ctx.conn.execute(
        "SELECT end_reason FROM runs WHERE task_id = ? ORDER BY rowid DESC LIMIT 1;",
        (task_id,),
    )
    prev_run = cur_prev_run.fetchone()
    prev_lease_expired = bool(prev_run and prev_run["end_reason"] == "lease_expired")

    workspace_choice = None
    new_ws_path = task["workspace_path"]
    if prev_lease_expired:
        workspace_choice = "reused" if reuse_workspace else "fresh"
        if workspace_choice == "fresh":
            next_attempt = max(1, task["attempt"]) + 1
            ws_root = get_workspaces_root(ctx.conn, ctx.home)
            new_ws_path = compute_task_workspace_path(ws_root, project_id, task_id, attempt=next_attempt)

    offered_at = now if is_inbox else None
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'queued', queued_at = ?, offered_at = ?, model = ?, effort = ?, workspace_path = ?, updated_at = ?, version = version + 1
           WHERE id = ?;""",
        (now, offered_at, resolved_model, resolved_effort, new_ws_path, now, task_id),
    )

    retry_payload: Dict[str, Any] = {
        "task_id": task_id,
        "project_id": project_id,
        "assignee": task["assignee_name"],
        "model": resolved_model,
        "effort": resolved_effort,
        "reason": reason,
        "reuse_workspace": reuse_workspace,
    }
    if workspace_choice:
        retry_payload["workspace"] = workspace_choice
        retry_payload["workspace_path"] = new_ws_path

    record_event(
        conn=ctx.conn,
        type_="task.retried",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="queued",
        reason=reason,
        payload=retry_payload,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    # Check if inbox mode agent: if inbox delivery, record task.offered event for outbox delivery
    if is_inbox:
        record_event(
            conn=ctx.conn,
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
                "assignee_id": assignee_id,
            },
            request_id=ctx.request_id,
        )

    updated_row = _get_task(ctx.conn, task_id)
    cur_deps = ctx.conn.execute("SELECT depends_on_task_id FROM task_deps WHERE task_id = ?;", (task_id,))
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn, depends_on=depends_on),
        events=["task.retried"],
        transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": "queued"}],
    )


def handle_task_escalate(ctx: HandlerContext) -> HandlerResult:
    """Escalate a failed task per plan §6.2 (T21)."""
    task_id = ctx.args.get("task_id")
    reason = ctx.args.get("reason")

    if not task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter task_id",
            details={"param": "task_id"},
        )
    if not reason or not str(reason).strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Parameter --reason is required and non-empty for task escalation",
            details={"param": "reason"},
        )
    reason = str(reason).strip()

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.escalate", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check transition: T21 is from failed -> escalated
    check_task_transition(task["status"], "escalated")

    now = now_iso_utc()
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'escalated', updated_at = ?, version = version + 1
           WHERE id = ?;""",
        (now, task_id),
    )

    # Fan-out to user and origin agent is performed automatically by record_event -> _fan_out_event
    record_event(
        conn=ctx.conn,
        type_="task.escalated",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state="failed",
        to_state="escalated",
        reason=reason,
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "reason": reason,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    updated_row = _get_task(ctx.conn, task_id)
    cur_deps = ctx.conn.execute("SELECT depends_on_task_id FROM task_deps WHERE task_id = ?;", (task_id,))
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn, depends_on=depends_on),
        events=["task.escalated"],
        transitions=[{"entity": "task", "id": task_id, "from": "failed", "to": "escalated"}],
    )


async def handle_task_wait(server: Any, ctx: HandlerContext, reader: Any = None) -> HandlerResult:
    """Block until task reaches a target state or terminal condition per plan §13."""
    task_id = ctx.args.get("task_id")
    if not task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing required parameter task_id",
            details={"param": "task_id"},
        )

    # Validate task existence and scoping
    row = _get_task(ctx.conn, task_id)
    project_id = row["project_id"]
    check_authz(ctx.conn, "task.wait", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    until = ctx.args.get("until", "submitted")
    if until == "submitted":
        match_fn = lambda s, k: s == "submitted"
    elif until == "terminal":
        match_fn = lambda s, k: is_task_terminal(s, k)
    elif until.startswith("state:"):
        target_state = until.split(":", 1)[1]
        from mesh.core.states import TASK_STATES
        if target_state not in TASK_STATES:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid task state in --until: '{target_state}'",
                details={"param": "until", "state": target_state},
            )
        match_fn = lambda s, k: s == target_state
    else:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid --until target: '{until}'. Must be 'submitted', 'terminal', or 'state:<s>'",
            details={"param": "until"},
        )

    timeout_val = ctx.args.get("timeout")
    if not timeout_val:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--timeout is required for task wait",
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

    start_mono = time.monotonic()
    end_mono = start_mono + timeout_s

    server.active_waits = getattr(server, "active_waits", 0) + 1
    try:
        while True:
            if reader is not None and reader.at_eof():
                raise MeshError(
                    code=c.WAIT_TIMEOUT,
                    exit_code=c.EXIT_WAIT_TIMEOUT,
                    message="Client disconnected during wait",
                    data={"items": []},
                )

            row = _get_task(ctx.conn, task_id)
            if match_fn(row["status"], row["kind"]):
                cur_deps = ctx.conn.execute(
                    "SELECT depends_on_task_id FROM task_deps WHERE task_id = ? ORDER BY depends_on_task_id;",
                    (task_id,),
                )
                depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]
                return HandlerResult(data=_format_task(row, conn=ctx.conn, depends_on=depends_on))

            remaining = end_mono - time.monotonic()
            if remaining <= 0:
                raise MeshError(
                    code=c.WAIT_TIMEOUT,
                    exit_code=c.EXIT_WAIT_TIMEOUT,
                    message=f"Task wait timed out after {timeout_s}s",
                    details={"task": task_id, "until": until},
                    data={"items": []},
                )

            async with server.notify_cond:
                try:
                    await asyncio.wait_for(server.notify_cond.wait(), timeout=min(remaining, 0.5))
                except asyncio.TimeoutError:
                    pass

            if reader is not None and reader.at_eof():
                raise MeshError(
                    code=c.WAIT_TIMEOUT,
                    exit_code=c.EXIT_WAIT_TIMEOUT,
                    message="Client disconnected during wait",
                    data={"items": []},
                )
    finally:
        server.active_waits = max(0, getattr(server, "active_waits", 1) - 1)


def _get_current_boot_id(conn: sqlite3.Connection) -> str:
    cur = conn.execute("SELECT id FROM boots WHERE stopped_at IS NULL ORDER BY started_at DESC LIMIT 1;")
    row = cur.fetchone()
    if row:
        return row[0]
    cur2 = conn.execute("SELECT id FROM boots ORDER BY started_at DESC LIMIT 1;")
    row2 = cur2.fetchone()
    if row2:
        return row2[0]
    return generate_id("boot")


async def prepare_task_start_workspace(ctx: HandlerContext) -> None:
    """Inbox `task start` of a code task: create/reuse its worktree before T5 (m2-r4).

    Runs before the request transaction, with the git work in a worker thread so the
    event loop is never blocked by git (review m2-r3 finding 5). It only acts when
    the start would get past the handler's own checks for authz, inbox delivery, an
    active project and a queued task; otherwise it returns and the handler reports
    the error as before. On success the result is put on `ctx.prepared_workspace`.

    A git failure is T6 `workspace_error` (committed here, lead notified by the
    `task.failed` event) and the call returns rc 6 INVALID_TRANSITION with
    `details.reason:"workspace_error"` (Q13).
    """
    if ctx.dry_run or ctx.cmd != "task.start":
        return
    task_id = ctx.args.get("task_id")
    if not task_id:
        return
    task = ctx.conn.execute(
        "SELECT t.*, a.delivery AS _delivery, p.status AS _project_status FROM tasks t "
        "LEFT JOIN agents a ON t.assignee_agent_id = a.id JOIN projects p ON t.project_id = p.id "
        "WHERE t.id = ?;",
        (task_id,),
    ).fetchone()
    if not task or task["kind"] != "code" or task["status"] != "queued":
        return
    if task["_delivery"] != "inbox" or task["_project_status"] != "active":
        return
    try:
        check_authz(ctx.conn, "task.start", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=task["project_id"], task_id=task_id)
    except MeshError:
        return
    plan = plan_code_workspace(ctx.conn, ctx.home, task)
    if plan is None:
        return
    try:
        result = await asyncio.to_thread(run_code_workspace_plan, plan)
    except WorkspaceError as e:
        details = dict(e.details)
        details["message"] = str(e)
        with transaction(ctx.conn):
            current = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            failed = bool(current and current["fence_seq"] == task["fence_seq"]) and fail_queued_task_t6(
                ctx.conn, current, "workspace_error", details,
                assignee_id=current["assignee_agent_id"], request_id=ctx.request_id,
            )
        err = {"task_id": task_id, "reason": "workspace_error", "failed": failed}
        err.update({k: v for k, v in details.items() if k not in err})
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' cannot start: {e}",
            details=err,
        )
    result = dict(result)
    result["task_id"] = task_id
    result["fence_seq"] = task["fence_seq"]
    ctx.prepared_workspace = result  # type: ignore[attr-defined]


def handle_task_start(ctx: HandlerContext) -> HandlerResult:
    """Start execution of an inbox-assigned task per plan §6.2 (T5), §11.1, §11.5."""
    task_id = ctx.args.get("task_id")
    if not task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Task ID is required for task start",
            details={"param": "task_id"},
        )

    # 1. Task exists
    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    # 2. Actor is assignee (or user direct)
    check_authz(ctx.conn, "task.start", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # 3. Delivery is inbox (if launch mode -> 6 INVALID_TRANSITION, reason: "launch_mode")
    cur_ag = ctx.conn.execute("SELECT a.*, p.name FROM agents a JOIN principals p ON a.id = p.id WHERE a.id = ?;", (task["assignee_agent_id"],))
    agent = cur_ag.fetchone()
    if not agent or agent["delivery"] != "inbox":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message="Task start is only valid for inbox-delivery agents",
            details={"reason": "launch_mode"},
        )

    # 4. Project active
    cur_p = ctx.conn.execute("SELECT status FROM projects WHERE id = ?;", (project_id,))
    proj = cur_p.fetchone()
    if not proj or proj["status"] != "active":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is not active",
            details={"project_id": project_id, "status": proj["status"] if proj else "unknown"},
        )

    # 5. Task status is queued
    if task["status"] != "queued":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' cannot start from status '{task['status']}' (expected 'queued')",
            details={"task_id": task_id, "from_state": task["status"], "expected": "queued"},
        )

    # 6. Concurrency limits
    cur_slots = ctx.conn.execute(
        "SELECT COUNT(*) FROM runs WHERE agent_id = ? AND status IN ('starting', 'running') AND kind IN ('task', 'external');",
        (agent["id"],),
    )
    active_slots = cur_slots.fetchone()[0]
    max_concurrency = agent["max_concurrency"] or 1
    if active_slots >= max_concurrency:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Agent '{agent['name']}' has reached concurrency limit ({active_slots}/{max_concurrency})",
            details={"agent": agent["name"], "active": active_slots, "max_concurrency": max_concurrency},
        )

    # Global cap (scheduler.max_total_runs)
    cur_set = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'scheduler.max_total_runs';")
    set_row = cur_set.fetchone()
    total_active_slots = None
    global_limit = None
    if set_row and set_row["value_json"]:
        try:
            global_limit = int(json.loads(set_row["value_json"]))
            cur_tot = ctx.conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('starting', 'running') AND kind IN ('task', 'external');")
            total_active_slots = cur_tot.fetchone()[0]
            if total_active_slots >= global_limit:
                raise MeshError(
                    code=c.CONCURRENCY_LIMIT,
                    exit_code=c.EXIT_BUSY,
                    message=f"Global concurrency cap reached ({total_active_slots}/{global_limit})",
                    details={"total_active": total_active_slots, "limit": global_limit},
                )
        except MeshError:
            raise
        except Exception:
            pass

    # 7. Model allowed
    task_model = task["model"] or agent["default_model"]
    allowed_models = json.loads(agent["allowed_models_json"]) if agent["allowed_models_json"] else []
    if allowed_models and task_model not in allowed_models:
        raise MeshError(
            code=c.MODEL_NOT_ALLOWED,
            exit_code=c.EXIT_USAGE,
            message=f"Model '{task_model}' is not in agent '{agent['name']}' allowed models",
            details={"model": task_model, "allowed": allowed_models},
        )

    # 8. Lease is free
    cur_l = ctx.conn.execute("SELECT key FROM leases WHERE key = ?;", (f"task:{task_id}",))
    if cur_l.fetchone():
        raise MeshError(
            code=c.ALREADY_EXISTS,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' already holds an active lease",
            details={"task_id": task_id},
        )

    # Transaction allocations
    now = now_iso_utc()
    now_ts = time.time()
    lease_ttl_s = get_lease_ttl_s(ctx.conn)
    expires_at = now_iso_utc(now_ts + lease_ttl_s)

    fence = task["fence_seq"] + 1
    attempt = task["attempt"] + 1
    run_id = generate_id("run")
    boot_id = _get_current_boot_id(ctx.conn)
    run_dir = os.path.join(ctx.home, "runs", run_id)

    workspaces_root = get_workspaces_root(ctx.conn, ctx.home)
    ws_path = task["workspace_path"]
    if not ws_path:
        ws_path = compute_task_workspace_path(workspaces_root, project_id, task_id, attempt=attempt)

    # Code task (§10.3): the worktree was prepared before this transaction, off the
    # event loop, by prepare_task_start_workspace (m2-r4; review m2-r3 finding 5).
    branch = task["branch"]
    base_commit = task["base_commit"]
    if task["kind"] == "code" and not ctx.dry_run:
        prepared = getattr(ctx, "prepared_workspace", None)
        if not prepared or prepared.get("task_id") != task_id or prepared.get("fence_seq") != task["fence_seq"]:
            raise MeshError(
                code=c.INVALID_TRANSITION,
                exit_code=c.EXIT_CONFLICT,
                message=f"Task '{task_id}' workspace was not prepared; retry the start",
                details={"task_id": task_id, "reason": "workspace_not_prepared"},
            )
        ws_path = prepared["workspace_path"]
        branch = prepared["branch"]
        base_commit = prepared["base_commit"]

    # Insert run
    ctx.conn.execute(
        """INSERT INTO runs (
            id, kind, task_id, project_id, agent_id, attempt, fence,
            adapter, model, effort, profile, status, boot_id, run_dir,
            argv_redacted_json, started_at
        ) VALUES (
            ?, 'external', ?, ?, ?, ?, ?,
            ?, ?, ?, ?, 'running', ?, ?,
            '[]', ?
        );""",
        (
            run_id, task_id, project_id, agent["id"], attempt, fence,
            agent["adapter"], task_model, task["effort"], task["profile"],
            boot_id, run_dir, now,
        ),
    )

    # Insert lease
    ctx.conn.execute(
        """INSERT INTO leases (
            key, run_id, holder_kind, holder, agent_id, fence,
            slots_in_use_at_acquire, limit_at_acquire, total_at_acquire, total_limit_at_acquire,
            acquired_at, expires_at, heartbeat_at
        ) VALUES (
            ?, ?, 'agent', ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?
        );""",
        (
            f"task:{task_id}", run_id, agent["id"], agent["id"], fence,
            active_slots, max_concurrency, total_active_slots, global_limit,
            now, expires_at, now,
        ),
    )

    # Update task
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'running', fence_seq = ?, attempt = ?, workspace_path = ?, branch = ?, base_commit = ?,
               version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (fence, attempt, ws_path, branch, base_commit, now, task_id),
    )

    # Event task.started (queued -> running)
    evt = record_event(
        conn=ctx.conn,
        type_="task.started",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        from_state="queued",
        to_state="running",
        payload={"run_id": run_id, "fence": fence, "attempt": attempt},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    # Queue post-commit effect for directories
    ctx.effects.append(Effect("create_run_and_workspace_dirs", {"run_dir": run_dir, "workspace_dir": ws_path}))

    data = {
        "run_id": run_id,
        "fence": fence,
        "lease_ttl_s": lease_ttl_s,
        "workspace": ws_path,
        "attempt": attempt,
    }
    if task["kind"] == "code":
        data["branch"] = branch
    return HandlerResult(
        data=data,
        transitions=[{"from": "queued", "to": "running", "task_id": task_id}],
        events=[evt],
    )


def _apply_run_token_scope(ctx: HandlerContext, task_id: str) -> Tuple[Optional[str], Optional[int]]:
    """Take run id and fence from the run token scope per plan §7.1, §7.2, §11.5.

    For an executor run token the scope's ``run_id`` and ``fence`` are
    authoritative: explicit ``--run``/``--fence`` may only repeat them, and a
    differing value is a stale write (exit 9 ``FENCE_MISMATCH``, no state change).
    """
    if ctx.via != "run_token":
        return ctx.args.get("run"), ctx.args.get("fence")
    scope = ctx.principal.get("scope") or {}
    if scope.get("task_id") and scope.get("task_id") != task_id:
        raise MeshError(
            code=c.SCOPE_MISMATCH,
            exit_code=c.EXIT_FORBIDDEN,
            message="Run token cannot write to a task outside its run scope",
            details={"scope_task_id": scope.get("task_id"), "task_id": task_id},
        )
    run_id = scope.get("run_id")
    fence = scope.get("fence")
    arg_run = ctx.args.get("run")
    arg_fence = ctx.args.get("fence")
    fence_differs = False
    if arg_fence is not None:
        try:
            fence_differs = int(arg_fence) != int(fence)
        except (TypeError, ValueError):
            fence_differs = True
    if (arg_run and arg_run != run_id) or fence_differs:
        raise MeshError(
            code=c.FENCE_MISMATCH,
            exit_code=c.EXIT_STALE,
            message="Presented run/fence differ from the run token's scope",
            details={"task_id": task_id, "scope_run_id": run_id, "scope_fence": fence,
                     "presented_run_id": arg_run, "presented_fence": arg_fence},
        )
    return run_id, fence


def handle_task_heartbeat(ctx: HandlerContext) -> HandlerResult:
    """Heartbeat an active task lease per plan §11.5."""
    task_id = ctx.args.get("task_id")
    run_id, fence = _apply_run_token_scope(ctx, task_id or "")

    if not task_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Task ID is required", details={"param": "task_id"})
    if not run_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task heartbeat", details={"param": "run"})
    if fence is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task heartbeat", details={"param": "fence"})

    task = _get_task(ctx.conn, task_id)
    check_authz(ctx.conn, "task.heartbeat", ctx.principal, ctx.actor_kind, ctx.via, project_id=task["project_id"], task_id=task_id)

    # Verify lease and fence
    lease = verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)

    # Update lease
    ttl_s = get_lease_ttl_s(ctx.conn)
    now_ts = time.time()
    now = now_iso_utc(now_ts)
    expires_at = now_iso_utc(now_ts + ttl_s)

    if lease["holder_kind"] == "meshd":
        # A meshd-held (launch) lease is renewed by the supervisor only (§6.4).
        expires_at = lease["expires_at"]
        ctx.conn.execute("UPDATE leases SET heartbeat_at = ? WHERE key = ?;", (now, lease["key"]))
    else:
        ctx.conn.execute(
            "UPDATE leases SET expires_at = ?, heartbeat_at = ? WHERE key = ?;",
            (expires_at, now, lease["key"]),
        )

    # Heartbeat writes no event (avoid event spam; recorded in impl notes)
    return HandlerResult(
        data={
            "task_id": task_id,
            "run_id": run_id,
            "fence": fence,
            "expires_at": expires_at,
        }
    )


def handle_task_progress(ctx: HandlerContext) -> HandlerResult:
    """Report task progress and heartbeat lease per plan §11.5."""
    task_id = ctx.args.get("task_id")
    message = ctx.args.get("message")
    run_id, fence = _apply_run_token_scope(ctx, task_id or "")

    if not task_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Task ID is required", details={"param": "task_id"})
    if not run_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task progress", details={"param": "run"})
    if fence is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task progress", details={"param": "fence"})
    if not message or len(message.encode("utf-8")) > 4096:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--message is required and must be non-empty and <= 4 KB", details={"param": "message"})

    task = _get_task(ctx.conn, task_id)
    check_authz(ctx.conn, "task.progress", ctx.principal, ctx.actor_kind, ctx.via, project_id=task["project_id"], task_id=task_id)

    # Verify lease and fence
    lease = verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)

    # Update lease expiration
    ttl_s = get_lease_ttl_s(ctx.conn)
    now_ts = time.time()
    now = now_iso_utc(now_ts)
    expires_at = now_iso_utc(now_ts + ttl_s)

    if lease["holder_kind"] == "meshd":
        # A meshd-held (launch) lease is renewed by the supervisor only (§6.4).
        expires_at = lease["expires_at"]
        ctx.conn.execute("UPDATE leases SET heartbeat_at = ? WHERE key = ?;", (now, lease["key"]))
    else:
        ctx.conn.execute(
            "UPDATE leases SET expires_at = ?, heartbeat_at = ? WHERE key = ?;",
            (expires_at, now, lease["key"]),
        )

    evt = record_event(
        conn=ctx.conn,
        type_="task.progress",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=task["project_id"],
        task_id=task_id,
        run_id=run_id,
        payload={"message": message, "fence": fence, "run_id": run_id},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(
        data={
            "task_id": task_id,
            "run_id": run_id,
            "fence": fence,
            "message": message,
            "expires_at": expires_at,
        },
        events=[evt],
    )


def _submit_git_info(ctx: HandlerContext, task: sqlite3.Row) -> Dict[str, Any]:
    prepared = getattr(ctx, "submit_git", None)
    if prepared and prepared.get("task_id") == task["id"] and prepared.get("fence_seq") == task["fence_seq"]:
        return prepared["git"]
    # Not collected before the transaction (should not happen for a running task):
    # never run git here on the event loop; record why instead (no submit is blocked).
    return {"branch": task["branch"], "base_commit": task["base_commit"],
            "error": {"step": "collect", "reason": "not_collected"}}


async def prepare_task_submit_git(ctx: HandlerContext) -> None:
    """Fenced `task submit` of a running code task: collect `deliverable.git` (§10.4)
    in a worker thread before the request transaction (m2-r4). Read-only git; the
    handler does all checks and uses the result only for the same task and fence.
    """
    if ctx.dry_run or ctx.cmd != "task.submit":
        return
    task_id = ctx.args.get("task_id")
    if not task_id:
        return
    task = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
    if not task or task["kind"] != "code" or task["status"] != "running":
        return
    # Authz and the fence first (review m2-r4 finding 4): no git process for a caller the
    # handler would refuse. Both checks are read-only; the handler repeats them in its
    # transaction and raises the real error there, so a MeshError here only skips git.
    try:
        run_id, fence = _apply_run_token_scope(ctx, task_id)
        if not run_id or fence is None:
            return
        check_authz(ctx.conn, "task.submit", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=task["project_id"], task_id=task_id)
        verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)
    except MeshError:
        return
    git = await asyncio.to_thread(submission_git_info, task["workspace_path"], task["branch"], task["base_commit"])
    ctx.submit_git = {"task_id": task_id, "fence_seq": task["fence_seq"], "git": git}  # type: ignore[attr-defined]


def handle_task_submit(ctx: HandlerContext) -> HandlerResult:
    """Submit completed task deliverable per plan §6.2 (T7), §11.3."""
    task_id = ctx.args.get("task_id")
    deliverable = ctx.args.get("deliverable")
    summary = ctx.args.get("summary")
    run_id, fence = _apply_run_token_scope(ctx, task_id or "")

    if not task_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Task ID is required", details={"param": "task_id"})
    if not run_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task submit", details={"param": "run"})
    if fence is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task submit", details={"param": "fence"})

    task = _get_task(ctx.conn, task_id)
    check_authz(ctx.conn, "task.submit", ctx.principal, ctx.actor_kind, ctx.via, project_id=task["project_id"], task_id=task_id)

    # Verify lease and fence first per plan §11.5
    lease = verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)

    # Verify task state
    if task["status"] != "running":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot submit task '{task_id}' in status '{task['status']}' (expected 'running')",
            details={"task_id": task_id, "from_state": task["status"], "expected": "running"},
        )

    # Deliverable parsing & validation
    if deliverable is None and summary is not None:
        deliverable_data = {"summary": summary}
    elif isinstance(deliverable, dict):
        deliverable_data = deliverable
    elif deliverable is None:
        deliverable_data = {}
    else:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Deliverable must be a JSON object", details={"param": "deliverable"})

    # Plan §15.2: deliverables are redacted before they are stored or echoed.
    deliverable_data = redact_value(deliverable_data)
    deliverable_raw = json.dumps(deliverable_data)
    if len(deliverable_raw.encode("utf-8")) > 200 * 1024:
        raise MeshError(
            c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Deliverable exceeds 200 KB limit",
            details={"param": "deliverable", "bytes": len(deliverable_raw.encode("utf-8")), "limit": 204800},
        )

    if task["kind"] == "code" and not ctx.dry_run:
        # §10.4 (m2-r4): Mesh records the worktree's git state; an agent-sent `git`
        # key is replaced. Collected off the event loop before this transaction.
        deliverable_data = dict(deliverable_data)
        deliverable_data["git"] = _submit_git_info(ctx, task)

    now = now_iso_utc()
    submitted_by = ctx.principal.get("name") or ctx.principal.get("id")

    deliverable_record = {
        "deliverable": deliverable_data,
        "submitted_by": submitted_by,
        "submitted_at": now,
    }

    if lease["holder_kind"] != "meshd":
        # Inbox mode releases at once: delete lease, set run exited
        ctx.conn.execute("DELETE FROM leases WHERE key = ?;", (f"task:{task_id}",))
        ctx.conn.execute(
            """UPDATE runs
               SET status = 'exited', end_reason = 'submitted', ended_at = ?, result_json = ?
               WHERE id = ?;""",
            (now, json.dumps(deliverable_record), run_id),
        )
    # Launch mode (T7): the lease stays until the run's processes are gone (§11.3/§11.4);
    # the supervisor then closes the run with run.exited_after_submit.

    # Update task
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'submitted', deliverable_json = ?, version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (json.dumps(deliverable_record), now, task_id),
    )

    # Event task.submitted (running -> submitted) with payload fence
    evt = record_event(
        conn=ctx.conn,
        type_="task.submitted",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=task["project_id"],
        task_id=task_id,
        run_id=run_id,
        from_state="running",
        to_state="submitted",
        payload={
            "fence": fence,
            "attempt": task["attempt"],
            "submitted_by": submitted_by,
            "submitted_at": now,
            "deliverable": deliverable_data,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(
        data={
            "task_id": task_id,
            "status": "submitted",
            "run_id": run_id,
            "fence": fence,
            "deliverable": deliverable_data,
            "submitted_by": submitted_by,
            "submitted_at": now,
        },
        transitions=[{"from": "running", "to": "submitted", "task_id": task_id}],
        events=[evt],
    )


def handle_task_fail(ctx: HandlerContext) -> HandlerResult:
    """Report task execution failure per plan §6.2 (T8)."""
    task_id = ctx.args.get("task_id")
    reason = ctx.args.get("reason")
    run_id, fence = _apply_run_token_scope(ctx, task_id or "")

    if not task_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Task ID is required", details={"param": "task_id"})
    if not run_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task fail", details={"param": "run"})
    if fence is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task fail", details={"param": "fence"})
    reason = reason.strip() if isinstance(reason, str) else reason
    if not reason or len(reason.encode("utf-8")) > 4096:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--reason is required and must be non-empty and <= 4 KB", details={"param": "reason"})
    reason = redact(reason)  # §15.1/§15.2: stored in failure_json, result_json and the event

    task = _get_task(ctx.conn, task_id)
    check_authz(ctx.conn, "task.fail", ctx.principal, ctx.actor_kind, ctx.via, project_id=task["project_id"], task_id=task_id)

    # Verify lease and fence first per plan §11.5 (stale writes report 9, not 6)
    lease = verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)

    if task["status"] != "running":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot fail task '{task_id}' in status '{task['status']}' (expected 'running')",
            details={"task_id": task_id, "from_state": task["status"], "expected": "running"},
        )

    now = now_iso_utc()
    failure_record = {
        "reason": "reported",
        "details": {
            "message": reason,
            "run_id": run_id,
        },
    }

    if lease["holder_kind"] == "meshd":
        # Launch mode (T8): the failure is applied once the run's processes are gone
        # (§11.4 release condition); until then the task stays running with the lease.
        pending = {"pending_failure": {
            "message": reason, "reported_at": now,
            "actor_id": ctx.principal["id"], "actor_kind": ctx.actor_kind, "via": ctx.via,
        }}
        ctx.conn.execute("UPDATE runs SET result_json = ? WHERE id = ?;", (json.dumps(redact_value(pending)), run_id))
        return HandlerResult(
            data={"task_id": task_id, "status": "running", "run_id": run_id, "fence": fence, "failure_pending": True}
        )

    # Release lease and end run
    ctx.conn.execute("DELETE FROM leases WHERE key = ?;", (f"task:{task_id}",))
    ctx.conn.execute(
        """UPDATE runs
           SET status = 'exited', end_reason = 'failed', ended_at = ?, result_json = ?
           WHERE id = ?;""",
        (now, json.dumps(failure_record), run_id),
    )

    # Update task
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'failed', failure_json = ?, version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (json.dumps(failure_record), now, task_id),
    )

    # Event task.failed (running -> failed)
    evt = record_event(
        conn=ctx.conn,
        type_="task.failed",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=task["project_id"],
        task_id=task_id,
        run_id=run_id,
        from_state="running",
        to_state="failed",
        reason="reported",
        payload={
            "reason": "reported",
            "message": reason,
            "fence": fence,
            "attempt": task["attempt"],
            "max_attempts": task["max_attempts"],
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(
        data={
            "task_id": task_id,
            "status": "failed",
            "reason": "reported",
            "message": reason,
            "attempt": task["attempt"],
            "max_attempts": task["max_attempts"],
        },
        transitions=[{"from": "running", "to": "failed", "task_id": task_id}],
        events=[evt],
    )


async def handle_task_merge(server: Any, ctx: HandlerContext) -> HandlerResult:
    """`task merge` (§10.5, T14–T17): dispatched to the merge pipeline in mesh/daemon/merge.py."""
    from mesh.daemon.merge import run_task_merge

    return await run_task_merge(server, ctx)


def handle_task_accept(ctx: HandlerContext) -> HandlerResult:
    """Accept a submitted task deliverable per plan §6.2 (T11)."""
    task_id = ctx.args.get("task_id")
    note = ctx.args.get("note")

    if not task_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Task ID is required", details={"param": "task_id"})

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.accept", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # 1. Transition check first (§6.2 T11): only submitted -> accepted
    check_task_transition(task["status"], "accepted")

    # 2. Check no active lease
    cur_l = ctx.conn.execute("SELECT key FROM leases WHERE key = ?;", (f"task:{task_id}",))
    if cur_l.fetchone():
        raise MeshError(
            code=c.RUN_STILL_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot accept task '{task_id}': active lease still held",
            details={"task_id": task_id},
        )

    # 3. Lead is not assignee (§6.2 T11, E9)
    if ctx.actor_kind != "user" and task["assignee_agent_id"] == ctx.principal["id"]:
        raise MeshError(
            code=c.LEAD_CANNOT_BE_ASSIGNEE,
            exit_code=c.EXIT_FORBIDDEN,
            message="Lead agent cannot accept its own assigned task",
            details={"task_id": task_id, "agent": ctx.principal.get("name")},
        )

    now = now_iso_utc()
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'accepted', version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (now, task_id),
    )

    evt = record_event(
        conn=ctx.conn,
        type_="task.accepted",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="accepted",
        payload={"note": note} if note else {},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    # Wake scheduler so dependent tasks promote (T4)
    promote_tasks(ctx.conn)

    return HandlerResult(
        data={
            "task_id": task_id,
            "status": "accepted",
            "note": note,
        },
        transitions=[{"from": task["status"], "to": "accepted", "task_id": task_id}],
        events=[evt],
    )


def handle_task_reject(ctx: HandlerContext) -> HandlerResult:
    """Reject a submitted task deliverable and request rework per plan §6.2 (T12)."""
    task_id = ctx.args.get("task_id")
    reason = ctx.args.get("reason")

    if not task_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Task ID is required", details={"param": "task_id"})
    if not reason or not isinstance(reason, str) or not reason.strip():
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Rejection reason is required", details={"param": "reason"})
    if len(reason.encode("utf-8")) > 4096:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Rejection reason exceeds 4096 byte limit",
            details={"param": "reason", "bytes": len(reason.encode("utf-8")), "limit": 4096},
        )

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.reject", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # 1. Transition check first (§6.2 T12): only submitted (or merge_failed) -> rework
    check_task_transition(task["status"], "rework")
    # rework is requeued by the scheduler (T13) without AG: same evidence check (ACC: N19)
    _requeue_launch_guard(ctx, task["assignee_agent_id"])

    # 2. Check no active lease
    cur_l = ctx.conn.execute("SELECT key FROM leases WHERE key = ?;", (f"task:{task_id}",))
    if cur_l.fetchone():
        raise MeshError(
            code=c.RUN_STILL_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot reject task '{task_id}': active lease still held",
            details={"task_id": task_id},
        )

    # 3. Check attempt cap (retry_over_limit)
    if task["attempt"] >= task["max_attempts"]:
        if ctx.actor_kind != "user" and ctx.via != "direct":
            if not ctx.approval:
                apr_info = create_or_get_pending_approval(
                    conn=ctx.conn,
                    category="retry_over_limit",
                    op="task.reject",
                    requested_by=ctx.principal["id"],
                    params=ctx.args,
                    summary=f"Reject to rework for task '{task['title']}' at attempt cap ({task['attempt']}/{task['max_attempts']}) requires user approval",
                    project_id=project_id,
                    task_id=task_id,
                    dry_run=ctx.dry_run,
                    request_id=ctx.request_id,
                    k_user=ctx.k_user,
                )
                raise MeshError(
                    code=c.APPROVAL_REQUIRED,
                    exit_code=c.EXIT_APPROVAL_REQUIRED,
                    message=f"Task rejection to rework exceeds max attempts ({task['max_attempts']}) and requires user approval (category: retry_over_limit)",
                    details=apr_info,
                )
            else:
                check_and_consume_approval(
                    conn=ctx.conn,
                    approval_id=ctx.approval,
                    op="task.reject",
                    category="retry_over_limit",
                    caller_id=ctx.principal["id"],
                    params=ctx.args,
                    project_id=project_id,
                    request_id=ctx.request_id,
                )

    now = now_iso_utc()
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'rework', version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (now, task_id),
    )

    evt = record_event(
        conn=ctx.conn,
        type_="task.rejected",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="rework",
        reason=reason,
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "reason": reason,
            "attempt": task["attempt"],
            "max_attempts": task["max_attempts"],
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    # Wake scheduler so task promotes to queued (T13)
    promote_tasks(ctx.conn)

    return HandlerResult(
        data={
            "task_id": task_id,
            "status": "rework",
            "reason": reason,
        },
        transitions=[{"from": task["status"], "to": "rework", "task_id": task_id}],
        events=[evt],
    )
