"""Project and intake command handlers per plan §6.1, §8, §13.3."""

import asyncio
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

import mesh.constants as c
from mesh.core.approvals import (
    check_and_consume_approval,
    create_or_get_pending_approval,
    get_approval_policy,
)
from mesh.core.authz import (
    check_authz,
    get_agent_capabilities,
    get_project_active_lead,
    is_project_lead,
    lead_run_token_scope,
)
from mesh.core.clock import now_iso_utc, parse_duration
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.core.paths import check_install_path_secure, get_install_root, is_path_equal_or_parent
from mesh.core.repos import (
    encode_test_command,
    integration_branch_name,
    parse_test_command,
    setup_integration,
    tests_view,
    validate_repo,
)
from mesh.core.workspaces import (
    allowed_workspace_roots,
    compute_lead_workspace_path,
    get_workspaces_root,
    has_project_any_ws_reservation,
    list_project_ws_reservation_keys,
)
from mesh.core.redact import redact, redact_value
from mesh.daemon.handlers.base import Effect, HandlerContext, HandlerResult
from mesh.daemon.handlers.inbox import _recheck_wait_authz
from mesh.errors import MeshError


def _redact_lead_assignment(row: Dict[str, Any]) -> Dict[str, Any]:
    """Redact free-text lead assignment reasons on read."""
    for field in ("reason", "ended_reason"):
        if isinstance(row.get(field), str) and row[field]:
            row[field] = redact(row[field])
    return row


def _lead_dir_effect(ctx: HandlerContext, project_id: str, agent_id: str) -> None:
    """Queue the lead dir as a post-COMMIT effect: a dry run or rollback creates nothing (§5).

    For a repo project the effect also adds the lead's detached `repo/` worktree (§10.1, §8).
    """
    ws_root = get_workspaces_root(ctx.conn, ctx.home)
    path = compute_lead_workspace_path(ws_root, project_id, agent_id)
    params: Dict[str, Any] = {"path": path}
    row = ctx.conn.execute("SELECT repo_path FROM projects WHERE id = ?;", (project_id,)).fetchone()
    if row is not None and row["repo_path"]:
        params.update({"repo": row["repo_path"], "project_id": project_id, "agent_id": agent_id,
                       "ws_root": ws_root})
    ctx.effects.append(Effect("create_lead_dir", params))


def _check_lead_launch(
    ctx: HandlerContext,
    adapter_id: str,
    binary_path: Optional[str] = None,
    options_json: Optional[str] = None,
) -> Optional[str]:
    """A launch-mode lead must pass the lead-turn launch gate (rc 13, AG step 4)."""
    from mesh.adapters.registry import get_adapter
    from mesh.daemon.lead_turns import lead_launch_check

    try:
        options = json.loads(options_json or "{}")
    except (ValueError, TypeError):
        options = {}
    if not isinstance(options, dict):
        options = {}
    adapter = get_adapter(adapter_id, testing_mode=ctx.testing_mode, options=options)
    return lead_launch_check(ctx.conn, adapter, ctx.testing_mode, binary_path=binary_path)


def _lead_evidence_warning_message(adapter_id: str, warning: Optional[str]) -> Optional[str]:
    if not warning:
        return None
    return (
        f"Lead adapter '{adapter_id}' allowed in warn mode without valid smoke evidence "
        f"({warning}); run `mesh adapter show {adapter_id}` for details"
    )


def _prepare_repo_project(ctx: HandlerContext) -> Optional[Dict[str, Any]]:
    """Validate the repo arguments of a new project (§10.2 steps 1–2, §7.6 mitigation 7).

    Returns None for a non-repo project. `--base-branch`/`--test-command` without `--repo`
    are rc 2 (they would be silently ignored otherwise).
    """
    repo = ctx.args.get("repo")
    if not repo:
        for param in ("base_branch", "test_command"):
            if ctx.args.get(param) not in (None, ""):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"'{param}' is only valid with --repo",
                    details={"param": param},
                )
        return None
    if not ctx.args.get("base_branch"):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--base-branch is required with --repo",
            details={"param": "base_branch"},
        )
    tests = parse_test_command(ctx.args.get("test_command"))
    if not isinstance(repo, str) or not os.path.isabs(repo):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Repo path must be absolute: '{repo}'",
            details={"param": "repo", "repo": repo},
        )
    # The repo must not overlap the install root, MESH_HOME, or workspaces.root in either direction (before any git call).
    check_install_path_secure(install_root=get_install_root(), repo_paths=[repo])
    home_real = os.path.realpath(ctx.home)
    repo_real = os.path.realpath(repo)
    if is_path_equal_or_parent(home_real, repo_real) or is_path_equal_or_parent(repo_real, home_real):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Repo path '{repo}' must not overlap MESH_HOME '{home_real}'",
            details={"param": "repo", "repo": repo, "conflict": home_real},
        )
    for ws_root in allowed_workspace_roots(ctx.conn, ctx.home):
        ws_root_real = os.path.realpath(ws_root)
        if is_path_equal_or_parent(ws_root_real, repo_real) or is_path_equal_or_parent(repo_real, ws_root_real):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Repo path '{repo}' must not overlap workspaces.root '{ws_root_real}'",
                details={"param": "repo", "repo": repo, "conflict": ws_root_real},
            )
    info = validate_repo(repo, ctx.args.get("base_branch"))
    info["tests"] = tests
    return info


def handle_intake_submit(ctx: HandlerContext) -> HandlerResult:
    # PR #39 review round 3 (B5): intake with --repo validates and sets up the repo inside
    # the request transaction too; same request-wide git deadline as project.create.
    from mesh.core import repos as _repos
    with _repos.git_budget(_repos.SETUP_INTEGRATION_BUDGET_S):
        return _handle_intake_submit(ctx)


def _handle_intake_submit(ctx: HandlerContext) -> HandlerResult:
    """Handle intake submit (both new project creation and follow-up message)."""
    now = now_iso_utc()

    # Form 2: Follow-up message on an existing project
    if ctx.args.get("project"):
        project_id = ctx.args["project"]
        check_authz(ctx.conn, "intake.submit", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)
        for param in ("repo", "base_branch", "test_command"):
            if ctx.args.get(param) not in (None, ""):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"'{param}' is only valid when creating a project",
                    details={"param": param},
                )

        # Check project exists and is active
        cur = ctx.conn.execute("SELECT id, status FROM projects WHERE id = ?;", (project_id,))
        prow = cur.fetchone()
        if not prow:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Project '{project_id}' not found",
                details={"project_id": project_id},
            )
        if prow["status"] != "active":
            raise MeshError(
                code=c.PROJECT_NOT_ACTIVE,
                exit_code=c.EXIT_CONFLICT,
                message=f"Project '{project_id}' is {prow['status']}, not active",
                details={"project_id": project_id, "status": prow["status"]},
            )

        message = ctx.args.get("message")
        if not isinstance(message, str) or not message.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'message' is required for follow-up intake submit",
                details={"param": "message"},
            )

        event = record_event(
            conn=ctx.conn,
            type_="project.request_received",
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            project_id=project_id,
            payload={
                "project_id": project_id,
                "message": message,
                "origin_ref": ctx.args.get("origin_ref"),
            },
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )

        return HandlerResult(data={"project_id": project_id, "message_recorded": True})

    # Form 1: New project submission
    check_authz(ctx.conn, "intake.submit", ctx.principal, ctx.actor_kind, ctx.via)

    # Repo project (§10.2): argument checks, install-path check and read-only validation
    # come right after authz; the branch and worktree are created only after every other
    # check passed, just before the insert (m2 Q3).
    repo_info = _prepare_repo_project(ctx)

    title = ctx.args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'title' is required",
            details={"param": "title"},
        )
    title = title.strip()

    goal = ctx.args.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'goal' is required",
            details={"param": "goal"},
        )
    goal = goal.strip()

    # Resolve plan_gate_mode (Chat-First Batch 1 + P2-5):
    # - Direct user: explicit arg -> project.default_plan_gate -> 'off'.
    # - Non-user caller: CANNOT explicitly set plan_gate_mode='off'; if the caller
    #   currently leads any active `require_approval` project, force `require_approval`
    #   so an inbox lead with `intake` cannot spawn an ungated sibling project.
    raw_pgm = ctx.args.get("plan_gate_mode")
    if raw_pgm is not None:
        if not isinstance(raw_pgm, str) or raw_pgm not in ("off", "require_approval"):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'plan_gate_mode' must be 'off' or 'require_approval'",
                details={"param": "plan_gate_mode", "expected": ["off", "require_approval"], "got": raw_pgm},
            )
        if ctx.actor_kind != "user" and raw_pgm == "off":
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message="Non-user callers cannot disable plan_gate_mode ('off')",
                details={"param": "plan_gate_mode", "actor_kind": ctx.actor_kind},
            )
        plan_gate_mode = raw_pgm
    else:
        pgm_row = ctx.conn.execute(
            "SELECT value_json FROM settings WHERE key = 'project.default_plan_gate';"
        ).fetchone()
        plan_gate_mode = "off"
        if pgm_row and pgm_row["value_json"]:
            try:
                parsed_pgm = json.loads(pgm_row["value_json"])
                if parsed_pgm in ("off", "require_approval"):
                    plan_gate_mode = parsed_pgm
            except Exception:
                plan_gate_mode = "off"

    if ctx.actor_kind != "user" and plan_gate_mode != "require_approval":
        leads_gated = ctx.conn.execute(
            """SELECT 1 FROM lead_assignments la
               JOIN projects p ON p.id = la.project_id
               WHERE la.agent_id = ? AND la.ended_at IS NULL
                 AND p.status = 'active' AND p.plan_gate_mode = 'require_approval'
               LIMIT 1;""",
            (ctx.principal["id"],),
        ).fetchone()
        if leads_gated is not None:
            plan_gate_mode = "require_approval"

    # Resolve per-project permission_mode: 'supervised' | 'plan' (DEFAULT) | 'auto'.
    # Non-user callers can never set permission_mode='auto'.
    raw_pmode = ctx.args.get("permission_mode")
    if raw_pmode is not None:
        if not isinstance(raw_pmode, str) or raw_pmode not in ("supervised", "plan", "auto"):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'permission_mode' must be 'supervised', 'plan', or 'auto'",
                details={"param": "permission_mode", "expected": ["supervised", "plan", "auto"], "got": raw_pmode},
            )
        if ctx.actor_kind != "user" and raw_pmode == "auto":
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message="Non-user callers cannot set permission_mode='auto'",
                details={"param": "permission_mode", "actor_kind": ctx.actor_kind},
            )
        permission_mode = raw_pmode
    else:
        permission_mode = "plan"

    criteria = ctx.args.get("criterion") or ctx.args.get("criteria") or []
    if isinstance(criteria, str):
        criteria = [criteria]
    if not criteria and plan_gate_mode == "require_approval":
        criteria = ["完成用户对话目标并经 Lead 验收"]
    if not isinstance(criteria, list) or not criteria or any(
        not isinstance(cr, str) or not cr.strip() for cr in criteria
    ):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="At least one non-empty acceptance criterion is required",
            details={"param": "criterion"},
        )

    # Resolve lead agent per plan §8
    lead_name = ctx.args.get("lead")
    if not lead_name:
        cur = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'lead.default_agent';")
        srow = cur.fetchone()
        if srow and srow["value_json"]:
            try:
                lead_name = json.loads(srow["value_json"])
            except Exception:
                lead_name = None

    if not lead_name:
        raise MeshError(
            code=c.NO_LEAD,
            exit_code=c.EXIT_USAGE,
            message="No project lead specified and lead.default_agent is null",
            details={},
        )

    cur = ctx.conn.execute(
        """SELECT a.id, a.can_lead, a.delivery, a.adapter, a.binary_path, a.adapter_options_json,
                  p.disabled_at, p.name
           FROM agents a
           JOIN principals p ON a.id = p.id
           WHERE p.name = ?;""",
        (lead_name,),
    )
    arow = cur.fetchone()
    if not arow:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Lead agent '{lead_name}' not found",
            details={"agent": lead_name},
        )
    if not arow["can_lead"]:
        raise MeshError(
            code=c.LEAD_NOT_ELIGIBLE,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{lead_name}' does not have can_lead capability",
            details={"agent": lead_name, "reason": "not_can_lead"},
        )
    if arow["disabled_at"] is not None:
        raise MeshError(
            code=c.LEAD_NOT_ELIGIBLE,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{lead_name}' is disabled",
            details={"agent": lead_name, "reason": "disabled"},
        )
    warnings: List[str] = []
    if arow["delivery"] == "launch":
        ev_warning = _check_lead_launch(
            ctx, arow["adapter"], binary_path=arow["binary_path"], options_json=arow["adapter_options_json"]
        )
        msg = _lead_evidence_warning_message(arow["adapter"], ev_warning)
        if msg:
            warnings.append(msg)

    lead_id = arow["id"]
    lead_delivery = arow["delivery"]

    # P3-5: A non-user agent with `intake` cannot bypass plan_gate_mode by creating an
    # ungated sibling project assigned to a lead that currently leads an active gated project.
    if ctx.actor_kind != "user" and plan_gate_mode != "require_approval":
        lead_leads_gated = ctx.conn.execute(
            """SELECT 1 FROM lead_assignments la
               JOIN projects p ON p.id = la.project_id
               WHERE la.agent_id = ? AND la.ended_at IS NULL
                 AND p.status = 'active' AND p.plan_gate_mode = 'require_approval'
               LIMIT 1;""",
            (lead_id,),
        ).fetchone()
        if lead_leads_gated is not None:
            plan_gate_mode = "require_approval"

    prj_id = generate_id("prj")
    origin_id = ctx.principal["id"]
    created_via = ctx.via

    repo_cols: Dict[str, Any] = {
        "repo_path": None, "base_branch": None, "integration_branch": None,
        "base_commit": None, "test_command_json": None,
    }
    if repo_info is not None:
        repo_cols = {
            "repo_path": repo_info["repo"],
            "base_branch": repo_info["base_branch"],
            "integration_branch": integration_branch_name(prj_id),
            "base_commit": repo_info["base_commit"],
            "test_command_json": encode_test_command(repo_info["tests"]),
        }

    ctx.conn.execute(
        """INSERT INTO projects (
            id, name, goal, status, origin_principal_id, origin_ref,
            created_by, created_via, version, created_at, updated_at,
            repo_path, base_branch, integration_branch, base_commit, test_command_json,
            plan_gate_mode, permission_mode
        ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
        (
            prj_id,
            title,
            goal,
            origin_id,
            ctx.args.get("origin_ref"),
            ctx.principal["id"],
            created_via,
            now,
            now,
            repo_cols["repo_path"],
            repo_cols["base_branch"],
            repo_cols["integration_branch"],
            repo_cols["base_commit"],
            repo_cols["test_command_json"],
            plan_gate_mode,
            permission_mode,
        ),
    )

    criteria_records = []
    for idx, ctext in enumerate(criteria):
        crit_id = generate_id("crit")
        clean_text = str(ctext).strip()
        ctx.conn.execute(
            """INSERT INTO acceptance_criteria (
                id, project_id, ordinal, text, created_at
            ) VALUES (?, ?, ?, ?, ?);""",
            (crit_id, prj_id, idx + 1, clean_text, now),
        )
        criteria_records.append({"id": crit_id, "ordinal": idx + 1, "text": clean_text})

    if "depends_on_project" in ctx.args and ctx.args["depends_on_project"] is not None:
        deps = ctx.args["depends_on_project"]
    elif "depends_on_projects" in ctx.args and ctx.args["depends_on_projects"] is not None:
        deps = ctx.args["depends_on_projects"]
    else:
        deps = []
    # Full review: a list of existing project ids, deduplicated (no INTERNAL_ERROR).
    if isinstance(deps, str):
        deps = [deps]
    if not isinstance(deps, list) or not all(isinstance(d, str) and d for d in deps):
        raise MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message="depends_on_project must be a list of project ids",
                        details={"param": "depends_on_project"})
    deps = list(dict.fromkeys(deps))
    for dep_id in deps:
        if dep_id != prj_id and ctx.conn.execute(
                "SELECT 1 FROM projects WHERE id = ?;", (dep_id,)).fetchone() is None:
            raise MeshError(code=c.NOT_FOUND, exit_code=c.EXIT_NOT_FOUND,
                            message=f"Project '{dep_id}' not found",
                            details={"project_id": dep_id})
    for dep_id in deps:
        if dep_id == prj_id:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Project cannot depend on itself",
            )
        ctx.conn.execute(
            "INSERT INTO project_deps (project_id, depends_on_project_id) VALUES (?, ?);",
            (prj_id, dep_id),
        )

    asg_id = generate_id("lead")
    ctx.conn.execute(
        """INSERT INTO lead_assignments (
            id, project_id, agent_id, assigned_by, via, reason, started_at
        ) VALUES (?, ?, ?, ?, ?, 'initial assignment', ?);""",
        (asg_id, prj_id, lead_id, ctx.principal["id"], created_via, now),
    )
    _lead_dir_effect(ctx, prj_id, lead_id)  # §10.1 lead dir at P1 (after COMMIT)

    event = record_event(
        conn=ctx.conn,
        type_="project.created",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=created_via,
        project_id=prj_id,
        to_state="active",
        payload={
            "project_id": prj_id,
            "title": title,
            "name": title,
            "goal": goal,
            "lead": lead_name,
            "plan_gate_mode": plan_gate_mode,
            "permission_mode": permission_mode,
            "criteria": criteria_records,
            "criteria_count": len(criteria_records),
            "origin_ref": ctx.args.get("origin_ref"),
            **(_repo_view(repo_cols) if repo_info is not None else {}),
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    if repo_info is not None and not ctx.dry_run:
        # Last statement of the new-project branch (review m2-r3 finding 1): every DB
        # statement that can fail on the caller's input (criteria, deps FK, lead) has run,
        # so such a failure leaves no git state. A dry run validates only. A git failure
        # after the branch step leaves the branch, named in details.branch, while the rows
        # roll back (nothing is deleted automatically, §10.7, Q3). The lead-dir effect
        # reads repo_path only after COMMIT.
        setup_integration(repo_info["repo"], prj_id, repo_info["base_commit"],
                          get_workspaces_root(ctx.conn, ctx.home))

    data = {
        "id": prj_id,
        "name": title,
        "goal": goal,
        "status": "active",
        "lead": lead_name,
        "plan_gate_mode": plan_gate_mode,
        "permission_mode": permission_mode,
        "created_via": created_via,
        "origin": ctx.principal["name"],
        "criteria": criteria_records,
        "created_at": now,
    }
    if repo_info is not None:
        data.update(_repo_view(repo_cols))
    return HandlerResult(data=data, warnings=warnings)


def _repo_view(cols: Any) -> Dict[str, Any]:
    """Repo fields of a project for `project show` / create responses (§13.3)."""
    return {
        "repo": cols["repo_path"],
        "base_branch": cols["base_branch"],
        "integration_branch": cols["integration_branch"],
        "base_commit": cols["base_commit"],
        "tests": tests_view(cols["test_command_json"]),
    }


def handle_project_create(ctx: HandlerContext) -> HandlerResult:
    # PR #39 review round 2 (B5): repo validation and integration setup run inside the
    # request transaction; their git calls share one request-wide deadline.
    from mesh.core import repos as _repos
    with _repos.git_budget(_repos.SETUP_INTEGRATION_BUDGET_S):
        return _handle_project_create(ctx)


def _handle_project_create(ctx: HandlerContext) -> HandlerResult:
    """Project create is an alias for intake submit."""
    return handle_intake_submit(ctx)


def _require_authorized_project_id(ctx: HandlerContext, cmd: str) -> str:
    """Validate non-empty string ``project_id`` and authorize ``cmd`` before reading project state."""
    project_id = ctx.args.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
            details={"param": "project_id"},
        )
    check_authz(ctx.conn, cmd, ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)
    return project_id


def handle_project_leads(ctx: HandlerContext) -> HandlerResult:
    """List lead assignment history for a project."""
    project_id = _require_authorized_project_id(ctx, "project.leads")

    # Verify project exists
    pcur = ctx.conn.execute("SELECT id FROM projects WHERE id = ?;", (project_id,))
    if not pcur.fetchone():
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    cur = ctx.conn.execute(
        """SELECT la.id, la.project_id, la.agent_id, p.name as agent,
                  la.assigned_by, la.via, la.reason, la.started_at,
                  la.ended_at, la.ended_reason
           FROM lead_assignments la
           JOIN principals p ON la.agent_id = p.id
           WHERE la.project_id = ?
           ORDER BY la.started_at ASC, la.id ASC;""",
        (project_id,),
    )
    items = [_redact_lead_assignment(dict(r)) for r in cur.fetchall()]
    current_lead = None
    for item in items:
        if item["ended_at"] is None:
            current_lead = item["agent"]
            break

    return HandlerResult(data={"items": items, "current_lead": current_lead})


def handle_project_list(ctx: HandlerContext) -> HandlerResult:
    """List projects with optional status filter."""
    check_authz(ctx.conn, "project.list", ctx.principal, ctx.actor_kind, ctx.via)
    status_filter = ctx.args.get("status")
    query = """
        SELECT pr.id, pr.name, pr.goal, pr.status, pr.plan_gate_mode, pr.permission_mode, pr.created_via,
               pr.created_at, pr.updated_at, pr.completed_at, pr.origin_ref,
               op.name as origin, lp.name as lead
        FROM projects pr
        LEFT JOIN principals op ON pr.origin_principal_id = op.id
        LEFT JOIN lead_assignments la ON pr.id = la.project_id AND la.ended_at IS NULL
        LEFT JOIN principals lp ON la.agent_id = lp.id
    """
    params: List[Any] = []
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        # Review m2-r9 finding 1(a): a lead run token lists only its own project.
        query += " WHERE pr.id = ?"
        params.append(scope_project)
    if status_filter:
        query += " AND pr.status = ?" if scope_project is not None else " WHERE pr.status = ?"
        params.append(status_filter)

    caps = ctx.principal.get("capabilities", [])
    if not caps and ctx.principal.get("id"):
        caps = get_agent_capabilities(ctx.conn, ctx.principal["id"])
    if scope_project is None and ctx.actor_kind != "user" and "relay" not in caps:
        agent_id = ctx.principal.get("id")
        where_or_and = "WHERE" if not status_filter else "AND"
        query += f""" {where_or_and} (
            pr.origin_principal_id = ?
            OR pr.id IN (SELECT project_id FROM lead_assignments WHERE agent_id = ?)
        )"""
        params.extend([agent_id, agent_id])

    query += " ORDER BY pr.created_at ASC, pr.id ASC;"

    cur = ctx.conn.execute(query, params)
    items = [dict(r) for r in cur.fetchall()]
    return HandlerResult(data={"items": items, "next_cursor": None})


def handle_project_show(ctx: HandlerContext) -> HandlerResult:
    """Show details, criteria, and progress of a project."""
    project_id = _require_authorized_project_id(ctx, "project.show")

    cur = ctx.conn.execute(
        """SELECT pr.id, pr.name, pr.goal, pr.status, pr.plan_gate_mode, pr.permission_mode, pr.created_via,
                  pr.created_at, pr.updated_at, pr.completed_at, pr.origin_ref,
                  op.name as origin, lp.name as lead,
                  pr.repo_path, pr.base_branch, pr.integration_branch, pr.base_commit,
                  pr.test_command_json
           FROM projects pr
           LEFT JOIN principals op ON pr.origin_principal_id = op.id
           LEFT JOIN lead_assignments la ON pr.id = la.project_id AND la.ended_at IS NULL
           LEFT JOIN principals lp ON la.agent_id = lp.id
           WHERE pr.id = ?;""",
        (project_id,),
    )
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    data = dict(row)
    repo_cols = {k: data.pop(k) for k in ("repo_path", "base_branch", "integration_branch",
                                          "base_commit", "test_command_json")}
    if repo_cols["repo_path"]:
        data.update(_repo_view(repo_cols))

    # Fetch criteria
    ccur = ctx.conn.execute(
        """SELECT id, ordinal, text, created_at, superseded_at
           FROM acceptance_criteria
           WHERE project_id = ? AND superseded_at IS NULL
           ORDER BY ordinal ASC;""",
        (project_id,),
    )
    data["criteria"] = [dict(r) for r in ccur.fetchall()]

    if ctx.args.get("history"):
        lcur = ctx.conn.execute(
            """SELECT la.id, p.name as agent, la.assigned_by, la.via,
                      la.reason, la.started_at, la.ended_at, la.ended_reason
               FROM lead_assignments la
               JOIN principals p ON la.agent_id = p.id
               WHERE la.project_id = ?
               ORDER BY la.started_at ASC;""",
            (project_id,),
        )
        data["lead_history"] = [_redact_lead_assignment(dict(r)) for r in lcur.fetchall()]

    return HandlerResult(data=data)


def handle_project_set_lead(ctx: HandlerContext) -> HandlerResult:
    """Change the designated lead agent of a project per plan §8."""
    project_id = _require_authorized_project_id(ctx, "project.set_lead")
    new_lead_name = ctx.args.get("agent")
    reason = ctx.args.get("reason")

    if not isinstance(new_lead_name, str) or not new_lead_name or not isinstance(reason, str) or not reason:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id, agent, and reason are required",
        )

    # 2. Project must exist and be active
    pcur = ctx.conn.execute("SELECT id, name, goal, status FROM projects WHERE id = ?;", (project_id,))
    prow = pcur.fetchone()
    if not prow:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )
    if prow["status"] != "active":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {prow['status']}, not active",
            details={"project_id": project_id, "status": prow["status"]},
        )

    res_key = has_project_any_ws_reservation(ctx.conn, project_id)
    if res_key:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot change lead of project '{project_id}': workspace operation '{res_key}' is in progress",
            details={"reservation": res_key, "project_id": project_id},
        )

    # 3. New lead agent validation
    acur = ctx.conn.execute(
        """SELECT a.id, a.can_lead, a.delivery, a.adapter, a.binary_path, a.adapter_options_json,
                  p.disabled_at, p.name
           FROM agents a
           JOIN principals p ON a.id = p.id
           WHERE p.name = ?;""",
        (new_lead_name,),
    )
    arow = acur.fetchone()
    if not arow:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Agent '{new_lead_name}' not found",
            details={"agent": new_lead_name},
        )
    if not arow["can_lead"]:
        raise MeshError(
            code=c.LEAD_NOT_ELIGIBLE,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{new_lead_name}' does not have can_lead capability",
            details={"agent": new_lead_name, "reason": "not_can_lead"},
        )
    if arow["disabled_at"] is not None:
        raise MeshError(
            code=c.LEAD_NOT_ELIGIBLE,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{new_lead_name}' is disabled",
            details={"agent": new_lead_name, "reason": "disabled"},
        )
    warnings: List[str] = []
    if arow["delivery"] == "launch":
        ev_warning = _check_lead_launch(
            ctx, arow["adapter"], binary_path=arow["binary_path"], options_json=arow["adapter_options_json"]
        )
        msg = _lead_evidence_warning_message(arow["adapter"], ev_warning)
        if msg:
            warnings.append(msg)

    new_lead_id = arow["id"]
    new_lead_delivery = arow["delivery"]

    # 4. Check active tasks where new lead is assignee (checklist E13)
    tcur = ctx.conn.execute(
        """SELECT id FROM tasks
           WHERE project_id = ? AND assignee_agent_id = ?
             AND status NOT IN ('merged', 'cancelled')
             AND NOT (status = 'accepted' AND COALESCE(kind, 'general') = 'general');""",
        (project_id, new_lead_id),
    )
    active_tasks = [r["id"] for r in tcur.fetchall()]
    if active_tasks:
        raise MeshError(
            code=c.LEAD_HAS_TASKS,
            exit_code=c.EXIT_CONFLICT,
            message=f"Agent '{new_lead_name}' has {len(active_tasks)} active task(s) in project and cannot become lead",
            details={"agent": new_lead_name, "tasks": active_tasks},
        )

    # 5. Get current active lead assignment
    old_lead = get_project_active_lead(ctx.conn, project_id)
    if old_lead and old_lead["agent_id"] == new_lead_id:
        # Already the active lead: avoid rotating assignment_id, killing an active turn, or consuming approvals.
        return HandlerResult(
            data={
                "project_id": project_id,
                "old_lead": old_lead["agent_name"],
                "new_lead": new_lead_name,
                "assignment_id": old_lead["assignment_id"],
                "unchanged": True,
            },
            warnings=warnings,
        )

    # 6. Approval gate after target validation and unchanged noop check
    if ctx.via == "relayed":
        policy = get_approval_policy(ctx.conn, "lead_change")
        if policy == "direct_only":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Approval policy for category 'lead_change' is 'direct_only'; relayed execution not allowed",
                details={"category": "lead_change", "policy": policy},
            )
    elif ctx.actor_kind != "user":
        if not ctx.approval:
            apr_info = create_or_get_pending_approval(
                conn=ctx.conn,
                category="lead_change",
                op="project.set_lead",
                requested_by=ctx.principal["id"],
                params={"project_id": project_id, "agent": new_lead_name, "reason": reason},
                summary=f"Change project {project_id} lead to {new_lead_name}",
                project_id=project_id,
                dry_run=ctx.dry_run,
                request_id=ctx.request_id,
                k_user=ctx.k_user,
                testing_mode=ctx.testing_mode,
            )
            raise MeshError(
                code=c.APPROVAL_REQUIRED,
                exit_code=c.EXIT_APPROVAL_REQUIRED,
                message=f"Operation 'project.set_lead' requires user approval (category: lead_change)",
                details=apr_info,
            )
        else:
            check_and_consume_approval(
                conn=ctx.conn,
                approval_id=ctx.approval,
                op="project.set_lead",
                category="lead_change",
                caller_id=ctx.principal["id"],
                params={"project_id": project_id, "agent": new_lead_name, "reason": reason},
                project_id=project_id,
                request_id=ctx.request_id,
                actor_kind=ctx.actor_kind, via=ctx.via, k_user=ctx.k_user,
            )
    old_lead_name = old_lead["agent_name"] if old_lead else "none"
    old_lead_id = old_lead["agent_id"] if old_lead else None

    reason = redact(reason) if isinstance(reason, str) else redact_value(reason)
    now = now_iso_utc()

    # Step 1: Close current lead assignment
    ctx.conn.execute(
        """UPDATE lead_assignments SET ended_at = ?, ended_reason = ?
           WHERE project_id = ? AND ended_at IS NULL;""",
        (now, reason, project_id),
    )

    # Step 2: Insert new lead assignment
    new_asg_id = generate_id("lead")
    ctx.conn.execute(
        """INSERT INTO lead_assignments (
            id, project_id, agent_id, assigned_by, via, reason, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?);""",
        (new_asg_id, project_id, new_lead_id, ctx.principal["id"], ctx.via, reason, now),
    )
    _lead_dir_effect(ctx, project_id, new_lead_id)  # §10.1 lead dir at set-lead (after COMMIT)

    # Withdraw approvals before re-delivering any of the old lead's messages.
    from mesh.core.approvals import cancel_open_approvals
    cancelled_approvals = []
    if old_lead_id:
        approval_rows = ctx.conn.execute(
            """SELECT * FROM approvals WHERE project_id=? AND (
               (requested_by=? AND status IN ('pending','approved')) OR
               (relayed_by=? AND decided_via='relayed' AND status='approved'))""",
            (project_id, old_lead_id, old_lead_id)).fetchall()
        cancelled_approvals = cancel_open_approvals(
            ctx.conn, approval_rows, ctx.principal["id"], ctx.actor_kind, ctx.via,
            "lead_changed", ctx.request_id, ctx.relayed_by, ctx.k_user)

    # Step 3: Mark old lead's pending outbox messages for this project superseded and re-create them for new lead.
    # 'delivering' rows belong to the old lead's running turn, which is terminated below:
    # they are superseded too, so its termination cannot put them back to 'pending'.
    if old_lead_id:
        ocur = ctx.conn.execute(
            """SELECT id, event_seq, type, payload_json, channel
               FROM outbox
               WHERE recipient_id = ? AND project_id = ? AND status IN ('pending', 'delivered', 'delivering');""",
            (old_lead_id, project_id),
        )
        old_messages = [dict(r) for r in ocur.fetchall()]
        ctx.conn.execute(
            """UPDATE outbox SET status = 'superseded'
               WHERE recipient_id = ? AND project_id = ? AND status IN ('pending', 'delivered', 'delivering');""",
            (old_lead_id, project_id),
        )
        for msg in old_messages:
            if msg["type"].startswith("approval."):
                ev = ctx.conn.execute("SELECT approval_id FROM events WHERE seq=?",
                                      (msg["event_seq"],)).fetchone()
                if ev and ev["approval_id"] in cancelled_approvals:
                    continue
            new_msg_id = generate_id("msg")
            target_channel = "lead_wake" if new_lead_delivery == "launch" else "inbox"
            new_dedup = f"{msg['type']}:{project_id}:{msg['event_seq']}:{new_asg_id}"
            ctx.conn.execute(
                """INSERT INTO outbox (
                    id, event_seq, recipient_id, channel, dedup_key, project_id,
                    type, payload_json, status, attempts, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                ON CONFLICT(dedup_key) DO NOTHING;""",
                (
                    new_msg_id,
                    msg["event_seq"],
                    new_lead_id,
                    target_channel,
                    new_dedup,
                    project_id,
                    msg["type"],
                    msg["payload_json"],
                    now,
                    now,
                ),
            )

    # Step 3b (§8, §11.4): the old assignment's running lead turn is terminated after
    # COMMIT (end_reason `lead_superseded`); until it is gone its token gets rc 9.
    if old_lead:
        for trow in ctx.conn.execute(
            """SELECT id FROM runs
               WHERE kind = 'lead_turn' AND project_id = ? AND lead_assignment_id = ?
                 AND status IN ('starting', 'running');""",
            (project_id, old_lead["assignment_id"]),
        ).fetchall():
            ctx.effects.append(Effect("terminate_lead_turn",
                                      {"run_id": trow["id"], "end_reason": "lead_superseded"}))

    # Step 4: approvals were already cancelled before copying outbox messages.

    # Step 5: Emit project.lead_changed event
    event = record_event(
        conn=ctx.conn,
        type_="project.lead_changed",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        payload={
            "old_lead": old_lead_name,
            "new_lead": new_lead_name,
            "reason": reason,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    # Step 6: Handover message to new lead
    # Build handover summary
    crit_cur = ctx.conn.execute(
        "SELECT id, ordinal, text FROM acceptance_criteria WHERE project_id = ? AND superseded_at IS NULL ORDER BY ordinal ASC;",
        (project_id,),
    )
    criteria_list = [dict(r) for r in crit_cur.fetchall()]

    tasks_cur = ctx.conn.execute(
        """SELECT t.id, t.title, t.status, p.name as assignee
           FROM tasks t
           LEFT JOIN principals p ON t.assignee_agent_id = p.id
           WHERE t.project_id = ?
           ORDER BY t.created_at ASC;""",
        (project_id,),
    )
    tasks_list = [dict(r) for r in tasks_cur.fetchall()]

    # Last 20 lead notes
    notes_cur = ctx.conn.execute(
        """SELECT payload_json FROM events
           WHERE project_id = ? AND type = 'project.note'
           ORDER BY seq DESC LIMIT 20;""",
        (project_id,),
    )
    notes_list = []
    for nr in reversed(notes_cur.fetchall()):
        try:
            pld = json.loads(nr["payload_json"])
            if "text" in pld:
                notes_list.append(pld["text"])
        except Exception:
            pass

    # Open approvals
    aprs_cur = ctx.conn.execute(
        "SELECT id, category, summary, requested_at FROM approvals WHERE project_id = ? AND status IN ('pending', 'approved');",
        (project_id,),
    )
    open_aprs = [dict(r) for r in aprs_cur.fetchall()]

    # Last 50 events
    evts_cur = ctx.conn.execute(
        "SELECT id, seq, ts, type, actor_id, from_state, to_state FROM events WHERE project_id = ? ORDER BY seq DESC LIMIT 50;",
        (project_id,),
    )
    recent_events = [dict(r) for r in reversed(evts_cur.fetchall())]

    handover_payload = redact_value({
        "project_id": project_id,
        "goal": prow["goal"],
        "criteria": criteria_list,
        "tasks": tasks_list,
        "notes": notes_list,
        "open_approvals": open_aprs,
        "recent_events": recent_events,
    })

    handover_msg_id = generate_id("msg")
    handover_channel = "lead_wake" if new_lead_delivery == "launch" else "inbox"
    ctx.conn.execute(
        """INSERT INTO outbox (
            id, event_seq, recipient_id, channel, dedup_key, project_id,
            type, payload_json, status, attempts, next_attempt_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'project.handover', ?, 'pending', 0, ?, ?)
        ON CONFLICT(dedup_key) DO NOTHING;""",
        (
            handover_msg_id,
            event["seq"],
            new_lead_id,
            handover_channel,
            f"handover:{new_asg_id}",
            project_id,
            json.dumps(handover_payload),
            now,
            now,
        ),
    )

    # Also notify old lead if present
    if old_lead_id:
        lead_changed_payload = redact_value({
            "project_id": project_id,
            "new_lead": new_lead_name,
            "reason": reason,
        })
        ctx.conn.execute(
            """INSERT INTO outbox (
                id, event_seq, recipient_id, channel, dedup_key, project_id,
                type, payload_json, status, attempts, next_attempt_at, created_at
            ) VALUES (?, ?, ?, 'inbox', ?, ?, 'project.lead_changed', ?, 'pending', 0, ?, ?)
            ON CONFLICT(dedup_key) DO NOTHING;""",
            (
                generate_id("msg"),
                event["seq"],
                old_lead_id,
                f"lead_changed:{new_asg_id}:{old_lead_id}",
                project_id,
                json.dumps(lead_changed_payload),
                now,
                now,
            ),
        )

    return HandlerResult(
        data={
            "project_id": project_id,
            "old_lead": old_lead_name,
            "new_lead": new_lead_name,
            "assignment_id": new_asg_id,
            "cancelled_approvals": cancelled_approvals,
        },
        warnings=warnings,
    )


def handle_project_pause(ctx: HandlerContext) -> HandlerResult:
    """Pause a project per plan §6.1 P2."""
    project_id = _require_authorized_project_id(ctx, "project.pause")

    cur = ctx.conn.execute("SELECT id, status FROM projects WHERE id = ?;", (project_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    status = row["status"]
    if status in ("completed", "cancelled"):
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {status}, not active",
            details={"project_id": project_id, "status": status},
        )
    if status == "paused":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is already paused",
            details={"project_id": project_id, "from": "paused", "to": "paused"},
        )

    # Lead requires approval category project_control (Checklist B12)
    if ctx.via == "relayed":
        policy = get_approval_policy(ctx.conn, "project_control")
        if policy == "direct_only":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Approval policy for category 'project_control' is 'direct_only'; relayed execution not allowed",
                details={"category": "project_control", "policy": policy},
            )
    elif ctx.actor_kind != "user":
        if not ctx.approval:
            apr_info = create_or_get_pending_approval(
                conn=ctx.conn,
                category="project_control",
                op="project.pause",
                requested_by=ctx.principal["id"],
                params={"project_id": project_id},
                summary=f"Pause project {project_id}",
                project_id=project_id,
                target_id=project_id,
                dry_run=ctx.dry_run,
                request_id=ctx.request_id,
                k_user=ctx.k_user,
                testing_mode=ctx.testing_mode,
            )
            raise MeshError(
                code=c.APPROVAL_REQUIRED,
                exit_code=c.EXIT_APPROVAL_REQUIRED,
                message=f"Operation 'project.pause' requires user approval (category: project_control)",
                details=apr_info,
            )
        else:
            check_and_consume_approval(
                conn=ctx.conn,
                approval_id=ctx.approval,
                op="project.pause",
                category="project_control",
                caller_id=ctx.principal["id"],
                params={"project_id": project_id},
                project_id=project_id,
                target_id=project_id,
                request_id=ctx.request_id,
                actor_kind=ctx.actor_kind, via=ctx.via, k_user=ctx.k_user,
            )

    now = now_iso_utc()
    ctx.conn.execute("UPDATE projects SET status = 'paused', updated_at = ? WHERE id = ?;", (now, project_id))

    record_event(
        conn=ctx.conn,
        type_="project.paused",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        from_state="active",
        to_state="paused",
        payload={"project_id": project_id},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(data={"id": project_id, "status": "paused"})


def handle_project_resume(ctx: HandlerContext) -> HandlerResult:
    """Resume a paused project per plan §6.1 P3."""
    project_id = _require_authorized_project_id(ctx, "project.resume")

    cur = ctx.conn.execute("SELECT id, status FROM projects WHERE id = ?;", (project_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    status = row["status"]
    if status in ("completed", "cancelled"):
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {status}, not active",
            details={"project_id": project_id, "status": status},
        )
    if status != "paused":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {status}, not paused",
            details={"project_id": project_id, "from": status, "to": "active"},
        )

    if ctx.via == "relayed":
        policy = get_approval_policy(ctx.conn, "project_control")
        if policy == "direct_only":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Approval policy for category 'project_control' is 'direct_only'; relayed execution not allowed",
                details={"category": "project_control", "policy": policy},
            )
    elif ctx.actor_kind != "user":
        if not ctx.approval:
            apr_info = create_or_get_pending_approval(
                conn=ctx.conn,
                category="project_control",
                op="project.resume",
                requested_by=ctx.principal["id"],
                params={"project_id": project_id},
                summary=f"Resume project {project_id}",
                project_id=project_id,
                target_id=project_id,
                dry_run=ctx.dry_run,
                request_id=ctx.request_id,
                k_user=ctx.k_user,
                testing_mode=ctx.testing_mode,
            )
            raise MeshError(
                code=c.APPROVAL_REQUIRED,
                exit_code=c.EXIT_APPROVAL_REQUIRED,
                message=f"Operation 'project.resume' requires user approval (category: project_control)",
                details=apr_info,
            )
        else:
            check_and_consume_approval(
                conn=ctx.conn,
                approval_id=ctx.approval,
                op="project.resume",
                category="project_control",
                caller_id=ctx.principal["id"],
                params={"project_id": project_id},
                project_id=project_id,
                target_id=project_id,
                request_id=ctx.request_id,
                actor_kind=ctx.actor_kind, via=ctx.via, k_user=ctx.k_user,
            )

    now = now_iso_utc()
    ctx.conn.execute("UPDATE projects SET status = 'active', updated_at = ? WHERE id = ?;", (now, project_id))
    # Full review r5 P2-3: the no-response window restarts on resume (no tick may have
    # run while paused, e.g. meshd down), never 0 s of grace.
    ctx.conn.execute("UPDATE tasks SET offered_at = ? WHERE project_id = ? AND status = 'queued' "
                     "AND offered_at IS NOT NULL;", (now, project_id))

    record_event(
        conn=ctx.conn,
        type_="project.resumed",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        from_state="paused",
        to_state="active",
        payload={"project_id": project_id},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(data={"id": project_id, "status": "active"})


def handle_project_cancel(ctx: HandlerContext) -> HandlerResult:
    """Cancel a project per plan §6.1 P5."""
    project_id = _require_authorized_project_id(ctx, "project.cancel")

    cur = ctx.conn.execute("SELECT id, status FROM projects WHERE id = ?;", (project_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    status = row["status"]
    if status in ("completed", "cancelled"):
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is already terminal ({status})",
            details={"project_id": project_id, "status": status},
        )

    # P5 while a merge runs (coordinator reading, m2-r7): block, never abort a merge or
    # its test step halfway. rc 6, no change (no approval is created either).
    merging = ctx.conn.execute(
        "SELECT t.id, l.run_id FROM tasks t LEFT JOIN leases l ON l.key = 'merge:' || t.project_id "
        "WHERE t.project_id = ? AND t.status = 'merging' ORDER BY t.id LIMIT 1;",
        (project_id,),
    ).fetchone()
    if merging is not None:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' cannot be cancelled while task '{merging['id']}' is merging",
            details={"project_id": project_id, "reason": "merge_in_progress",
                     "task_id": merging["id"], "merge_run_id": merging["run_id"]},
        )

    res_key = has_project_any_ws_reservation(ctx.conn, project_id)
    if res_key:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot cancel project '{project_id}': workspace operation '{res_key}' is in progress",
            details={"reservation": res_key, "project_id": project_id},
        )

    if ctx.via == "relayed":
        policy = get_approval_policy(ctx.conn, "project_control")
        if policy == "direct_only":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Approval policy for category 'project_control' is 'direct_only'; relayed execution not allowed",
                details={"category": "project_control", "policy": policy},
            )
    elif ctx.actor_kind != "user":
        if not ctx.approval:
            apr_info = create_or_get_pending_approval(
                conn=ctx.conn,
                category="project_control",
                op="project.cancel",
                requested_by=ctx.principal["id"],
                params={"project_id": project_id, "cancel_running": ctx.args.get("cancel_running", False)},
                summary=f"Cancel project {project_id}",
                project_id=project_id,
                target_id=project_id,
                dry_run=ctx.dry_run,
                request_id=ctx.request_id,
                k_user=ctx.k_user,
                testing_mode=ctx.testing_mode,
            )
            raise MeshError(
                code=c.APPROVAL_REQUIRED,
                exit_code=c.EXIT_APPROVAL_REQUIRED,
                message=f"Operation 'project.cancel' requires user approval (category: project_control)",
                details=apr_info,
            )
        else:
            check_and_consume_approval(
                conn=ctx.conn,
                approval_id=ctx.approval,
                op="project.cancel",
                category="project_control",
                caller_id=ctx.principal["id"],
                params={"project_id": project_id, "cancel_running": ctx.args.get("cancel_running", False)},
                project_id=project_id,
                target_id=project_id,
                request_id=ctx.request_id,
                actor_kind=ctx.actor_kind, via=ctx.via, k_user=ctx.k_user,
            )

    now = now_iso_utc()
    ctx.conn.execute("UPDATE projects SET status = 'cancelled', updated_at = ? WHERE id = ?;", (now, project_id))

    # P5: every non-terminal task → cancelled; tasks holding a lease (running, or
    # submitted in post-submit grace) go through `cancelling` (T9) so their runs are
    # terminated first (end_reason project_cancelled, §11.4). `--cancel-running` is
    # recorded; P5 applies to running tasks either way (run 3 reading).
    from mesh.core.states import is_task_terminal
    from mesh.daemon.handlers.task import _supersede_offers, begin_task_cancel, emit_dependents_blocked

    cancel_reason = "project_cancelled"
    task_rows = ctx.conn.execute(
        "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at ASC, id ASC;", (project_id,)
    ).fetchall()
    for trow in task_rows:
        if is_task_terminal(trow["status"], trow["kind"]) or trow["status"] == "cancelling":
            continue
        lease = ctx.conn.execute("SELECT * FROM leases WHERE key = ?;", (f"task:{trow['id']}",)).fetchone()
        if lease is not None:
            begin_task_cancel(ctx.conn, trow, lease, cancel_reason, ctx)
            continue
        _supersede_offers(ctx.conn, trow)
        ctx.conn.execute(
            "UPDATE tasks SET status = 'cancelled', updated_at = ?, version = version + 1 WHERE id = ?;",
            (now, trow["id"]),
        )
        record_event(
            conn=ctx.conn,
            type_="task.cancelled",
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            relayed_by=ctx.relayed_by,
            project_id=project_id,
            task_id=trow["id"],
            from_state=trow["status"],
            to_state="cancelled",
            reason=cancel_reason,
            payload={"task_id": trow["id"], "project_id": project_id, "reason": cancel_reason},
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
        emit_dependents_blocked(ctx.conn, trow["id"], ctx.request_id)

    from mesh.core.approvals import cancel_open_approvals
    cancel_open_approvals(
        ctx.conn, ctx.conn.execute(
            "SELECT * FROM approvals WHERE project_id=? AND status IN ('pending','approved')",
            (project_id,)).fetchall(),
        ctx.principal["id"], ctx.actor_kind, ctx.via, "project_cancelled",
        ctx.request_id, ctx.relayed_by, ctx.k_user)

    # Supersede pending/delivering lead_wake messages and terminate any active lead turn.
    ctx.conn.execute(
        "UPDATE outbox SET status = 'superseded' "
        "WHERE project_id = ? AND channel = 'lead_wake' AND status IN ('pending', 'delivering');",
        (project_id,),
    )
    for lrow in ctx.conn.execute(
        "SELECT id FROM runs WHERE kind = 'lead_turn' AND project_id = ? AND status IN ('starting', 'running');",
        (project_id,),
    ).fetchall():
        ctx.effects.append(Effect("terminate_lead_turn", {"run_id": lrow["id"], "end_reason": cancel_reason}))
    ctx.effects.append(Effect("cancel_project_merges", {"project_id": project_id}))

    record_event(
        conn=ctx.conn,
        type_="project.cancelled",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        from_state=status,
        to_state="cancelled",
        payload={"project_id": project_id, "cancel_running": bool(ctx.args.get("cancel_running", False))},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(data={"id": project_id, "status": "cancelled"})


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def handle_project_deliver(ctx: HandlerContext) -> HandlerResult:
    """`project deliver` (§10.6): fast-forward the base branch to the integration branch.

    Lead or user, category `deliver_to_base` (default `not_required`; a tightened policy
    goes through the usual `--approval` consume path). Mesh never pushes, never rebases
    and never forces: a base that is not an ancestor of the integration branch is rc 14
    `NOT_FAST_FORWARD` with nothing changed.
    """
    from mesh.core import repos as _repos

    with _repos.git_budget(_repos.SETUP_INTEGRATION_BUDGET_S):
        return _handle_project_deliver(ctx)


def _handle_project_deliver(ctx: HandlerContext) -> HandlerResult:
    from mesh.core.repos import deliver_to_base

    project_id = _require_authorized_project_id(ctx, "project.deliver")
    row = ctx.conn.execute(
        "SELECT id, status, repo_path, base_branch, integration_branch FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    if row is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )
    if row["status"] == "cancelled":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is cancelled",
            details={"project_id": project_id, "status": row["status"]},
        )
    if not row["repo_path"]:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Project '{project_id}' is not a repo project; there is no base branch to deliver to",
            details={"param": "project_id", "reason": "no_repo", "project_id": project_id},
        )

    # Carbon P2-7: never deliver while a merge runs on the same repository (this project or
    # another one sharing the repo path): the merge commit may exist in git before the
    # database records it. rc 11, before approval, with no mutation.
    from mesh.daemon.merge import merge_in_progress_for_repo

    busy = merge_in_progress_for_repo(ctx.conn, row["repo_path"])
    if busy is not None:
        raise MeshError(
            code=c.MERGE_IN_PROGRESS,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot deliver project '{project_id}': a merge is in progress on its repository",
            details={"project_id": project_id, "reason": "merge_in_progress",
                     "merge_project_id": busy["project_id"], "task_id": busy["task_id"],
                     "merge_run_id": busy["run_id"], "lease": busy["lease"]},
        )

    # Carbon P1-2 / P1-3: never deliver work nobody merged. A code task of this project that
    # is not `merged` but whose own commits are already in the integration branch (a test
    # command that committed during its merge, a crash the recovery could not reconcile)
    # would reach the base branch with this delivery. Same check as `doctor
    # --check-invariants` (reverse I9); rc 6, before approval, with no mutation.
    from mesh.core.invariants import check_i9_integrated_unmerged_tasks

    unmerged = check_i9_integrated_unmerged_tasks(ctx.conn, project_id)
    if unmerged:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=(f"Cannot deliver project '{project_id}': the integration branch carries work of "
                     f"tasks that are not merged ({', '.join(v.details['task_id'] for v in unmerged)})"),
            details={"project_id": project_id, "reason": "integrated_unmerged_work",
                     "tasks": [{"task_id": v.details["task_id"], "status": v.details["status"],
                                "merge_commit": v.details["merge_commit"]} for v in unmerged]},
        )

    # Full review r1 P1-5: already delivered (base == integration tip) is a noop that
    # writes nothing, so it must not consume a single-use approval either.
    from mesh.core.repos import deliver_heads

    heads = None if ctx.dry_run else deliver_heads(row["repo_path"], row["base_branch"],
                                                   row["integration_branch"] or integration_branch_name(project_id))
    if heads is None and not ctx.dry_run:
        # Full review r2 P2-B: a deliver is always pinned to the heads it read and backed by
        # an intent; unreadable heads fail before any approval is consumed.
        raise MeshError(
            code=c.INTERNAL_ERROR,
            exit_code=c.EXIT_INTERNAL,
            message=f"Cannot read the base or integration branch of project '{project_id}'; nothing was changed",
            details={"project_id": project_id, "reason": "git_failed", "step": "rev-parse heads"},
        )
    if not ctx.dry_run:
        # Full review r4 P1-1: an earlier delivery's intent is settled in its own
        # transaction before this request (server / lead turn); one still here is
        # unsettled (or an earlier action of this lead turn, not yet committed). Never
        # overwrite it.
        from mesh.core.repos import deliver_intent_path as _dip

        from mesh.daemon import lead_turns as _lt

        # r5 P2-2: checked before the noop shortcut, which must not hide an unsettled
        # delivery; an intent this lead turn wrote itself (pending commit) allows only a noop.
        dip = _dip(ctx.home, project_id)
        own_noop = dip in _lt._PENDING_INTENT_UNLINKS and heads is not None and heads[0] == heads[1]
        if os.path.lexists(dip) and not own_noop:
            raise MeshError(
                code=c.INVALID_TRANSITION,
                exit_code=c.EXIT_CONFLICT,
                message=(f"An earlier delivery of project '{project_id}' is not settled yet; "
                         f"nothing was changed, retry `mesh project deliver`"),
                details={"project_id": project_id, "reason": "deliver_intent_pending"},
            )

    if heads is not None and heads[0] == heads[1]:
        return HandlerResult(
            data={"project_id": project_id, "base_branch": row["base_branch"],
                  "integration_branch": row["integration_branch"] or integration_branch_name(project_id),
                  "old": heads[0], "new": heads[1], "method": "noop", "worktree": None},
            events=[],
        )

    # Approval gate (§7.4): `deliver_to_base` defaults to `not_required`.
    policy = get_approval_policy(ctx.conn, "deliver_to_base")
    if policy != "not_required":
        if ctx.via == "relayed" and policy == "direct_only":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Approval policy for category 'deliver_to_base' is 'direct_only'; relayed execution not allowed",
                details={"category": "deliver_to_base", "policy": policy},
            )
        if ctx.actor_kind != "user":
            params = {"project_id": project_id}
            if not ctx.approval:
                apr_info = create_or_get_pending_approval(
                    conn=ctx.conn,
                    category="deliver_to_base",
                    op="project.deliver",
                    requested_by=ctx.principal["id"],
                    params=params,
                    summary=f"Deliver project {project_id} to {row['base_branch']}",
                    project_id=project_id,
                    target_id=project_id,
                    dry_run=ctx.dry_run,
                    request_id=ctx.request_id,
                    k_user=ctx.k_user,
                    testing_mode=ctx.testing_mode,
                )
                raise MeshError(
                    code=c.APPROVAL_REQUIRED,
                    exit_code=c.EXIT_APPROVAL_REQUIRED,
                    message=f"Operation 'project.deliver' requires user approval (category: deliver_to_base)",
                    details=apr_info,
                )
            check_and_consume_approval(
                conn=ctx.conn,
                approval_id=ctx.approval,
                op="project.deliver",
                category="deliver_to_base",
                caller_id=ctx.principal["id"],
                params=params,
                project_id=project_id,
                target_id=project_id,
                request_id=ctx.request_id,
                actor_kind=ctx.actor_kind, via=ctx.via, k_user=ctx.k_user,
            )

    base = row["base_branch"]
    integration = row["integration_branch"] or integration_branch_name(project_id)
    if ctx.dry_run:
        # A dry run reports through `data.would` (server.py rolls the transaction back), so
        # the plan travels as one `transitions` entry (review m2-r8 MINOR-5).
        from mesh.core.repos import deliver_plan

        plan = deliver_plan(row["repo_path"], base, integration)
        return HandlerResult(
            data={"would": {"project_id": project_id, "base_branch": base,
                            "integration_branch": integration, "fast_forward": plan["fast_forward"]}},
            transitions=[{"project_id": project_id, "base_branch": base,
                          "integration_branch": integration, "fast_forward": plan["fast_forward"]}],
            events=[],
        )

    # Full review r1 P1-5: the base branch moves before this transaction commits; a durable
    # intent lets recovery record the delivery if meshd dies in between. r2 P1-D: it lives
    # only while this request is in flight (removed on failure, noop, and after commit);
    # r2 P1-E: it carries the event horizon and the idempotency key of this request.
    from mesh.core.repos import deliver_intent_path, write_deliver_intent

    assert heads is not None
    idem = getattr(ctx, "idempotency", None)
    last = ctx.conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events;").fetchone()
    intent_path = deliver_intent_path(ctx.home, project_id)
    intent_written = False

    def _before_write() -> None:
        nonlocal intent_written
        try:
            write_deliver_intent(ctx.home, {
                "project_id": project_id, "repo": row["repo_path"], "base_branch": base,
                "integration_branch": integration, "old": heads[0], "new": heads[1],
                "actor_id": ctx.principal["id"], "actor_kind": ctx.actor_kind, "via": ctx.via,
                "relayed_by": ctx.relayed_by, "request_id": ctx.request_id,
                "approval_id": ctx.approval if policy != "not_required" and ctx.actor_kind != "user" else None,
                "min_event_seq": int(last["m"] if last is not None else 0),
                "idempotency": ({"principal_id": idem["principal_id"], "key": idem["key"], "cmd": ctx.cmd,
                                 "request_hash": idem["request_hash"]} if idem and idem.get("key") else None),
            })
        except OSError as e:
            raise MeshError(
                code=c.INTERNAL_ERROR,
                exit_code=c.EXIT_INTERNAL,
                message=f"Cannot record the deliver intent: {e}; nothing was changed",
                details={"project_id": project_id, "reason": "intent_failed"},
            )
        intent_written = True

    try:
        result = deliver_to_base(row["repo_path"], base, integration, expected=heads, before_write=_before_write)
    except BaseException as exc:
        if intent_written:
            # Devin full-review P2-1: a mismatch read after the ff merge may follow a write;
            # it keeps the intent unless the base is back at old (settle records it).
            no_write_err = isinstance(exc, MeshError) and not exc.details.get("after_write") and (
                exc.code in (c.VERSION_MISMATCH, c.NOT_FAST_FORWARD, c.WORKTREE_DIRTY, c.INVALID_ARGUMENT)
                or (
                    exc.details.get("reason") == "git_failed"
                    and exc.details.get("step") in ("merge --ff-only", "update-ref")
                )
            )
            if no_write_err:
                _unlink_quiet(intent_path)
            else:
                now_heads = deliver_heads(row["repo_path"], base, integration)
                if now_heads is not None and now_heads[0] == heads[0]:
                    _unlink_quiet(intent_path)
        raise

    if result["method"] == "noop":
        if intent_written:
            _unlink_quiet(intent_path)
        # Already delivered: no git write happened, so there is nothing to record
        # (review m2-r8 MINOR-2).
        return HandlerResult(
            data={
                "project_id": project_id,
                "base_branch": base,
                "integration_branch": integration,
                "old": result["old"],
                "new": result["new"],
                "method": result["method"],
                "worktree": result.get("worktree"),
            },
            events=[],
        )

    ctx.effects.append(Effect("unlink_deliver_intent", {"path": intent_path}))
    record_event(
        conn=ctx.conn,
        type_="project.delivered",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        payload={"old": result["old"], "new": result["new"], "method": result["method"]},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    return HandlerResult(
        data={
            "project_id": project_id,
            "base_branch": base,
            "integration_branch": integration,
            "old": result["old"],
            "new": result["new"],
            "method": result["method"],
            "worktree": result.get("worktree"),
        },
        events=[{"type": "project.delivered", "project_id": project_id}],
    )


def handle_project_update(ctx: HandlerContext) -> HandlerResult:
    """Update project goal or acceptance criteria per plan §6.1, §7.4 (scope_change)."""
    project_id = _require_authorized_project_id(ctx, "project.update")

    goal = ctx.args.get("goal")
    criterion = ctx.args.get("criterion")
    if criterion is None or (
        isinstance(criterion, list) and not criterion and ctx.args.get("criteria") is not None
    ):
        criterion = ctx.args.get("criteria")
    if criterion is None:
        criterion = []
    elif isinstance(criterion, str):
        criterion = [criterion]

    if goal is None and not criterion:
        if isinstance(criterion, list):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="At least one of --goal or --criterion must be provided",
            )
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Each acceptance criterion must be a non-empty string",
            details={"param": "criterion"},
        )
    if goal is not None and (not isinstance(goal, str) or not goal.strip()):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'goal' must be a non-empty string",
            details={"param": "goal"},
        )
    has_criterion = "criterion" in ctx.args or "criteria" in ctx.args
    if has_criterion:
        if not isinstance(criterion, list) or any(
            not isinstance(cr, str) or not cr.strip() for cr in criterion
        ):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Each acceptance criterion must be a non-empty string",
                details={"param": "criterion"},
            )

    cur = ctx.conn.execute("SELECT id, status, goal, version FROM projects WHERE id = ?;", (project_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    status = row["status"]
    if status in ("completed", "cancelled"):
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is terminal ({status})",
            details={"project_id": project_id, "status": status},
        )

    # Build canonical params
    canonical_params: Dict[str, Any] = {"project_id": project_id}
    if goal is not None:
        canonical_params["goal"] = str(goal).strip()
    if criterion:
        canonical_params["criterion"] = [str(cr).strip() for cr in criterion if str(cr).strip()]

    if ctx.via == "relayed":
        policy = get_approval_policy(ctx.conn, "scope_change")
        if policy == "direct_only":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Approval policy for category 'scope_change' is 'direct_only'; relayed execution not allowed",
                details={"category": "scope_change", "policy": policy},
            )
    elif ctx.actor_kind != "user":
        if not ctx.approval:
            apr_info = create_or_get_pending_approval(
                conn=ctx.conn,
                category="scope_change",
                op="project.update",
                requested_by=ctx.principal["id"],
                params=canonical_params,
                summary=f"Update scope for project {project_id}",
                project_id=project_id,
                target_id=project_id,
                dry_run=ctx.dry_run,
                request_id=ctx.request_id,
                k_user=ctx.k_user,
                testing_mode=ctx.testing_mode,
            )
            raise MeshError(
                code=c.APPROVAL_REQUIRED,
                exit_code=c.EXIT_APPROVAL_REQUIRED,
                message="Operation 'project.update' requires user approval (category: scope_change)",
                details=apr_info,
            )
        else:
            check_and_consume_approval(
                conn=ctx.conn,
                approval_id=ctx.approval,
                op="project.update",
                category="scope_change",
                caller_id=ctx.principal["id"],
                params=canonical_params,
                project_id=project_id,
                target_id=project_id,
                request_id=ctx.request_id,
                actor_kind=ctx.actor_kind, via=ctx.via, k_user=ctx.k_user,
            )

    now = now_iso_utc()
    updated_goal = str(goal).strip() if goal is not None else row["goal"]
    ctx.conn.execute(
        "UPDATE projects SET goal = ?, updated_at = ?, version = version + 1 WHERE id = ?;",
        (updated_goal, now, project_id),
    )

    if canonical_params.get("criterion"):
        cur_ord = ctx.conn.execute("SELECT MAX(ordinal) as max_ord FROM acceptance_criteria WHERE project_id = ?;", (project_id,))
        max_ord_row = cur_ord.fetchone()
        max_ord = (max_ord_row["max_ord"] or 0) if max_ord_row else 0
        for i, crit_text in enumerate(canonical_params["criterion"]):
            crit_id = generate_id("crit")
            ctx.conn.execute(
                """INSERT INTO acceptance_criteria (id, project_id, ordinal, text, created_at)
                   VALUES (?, ?, ?, ?, ?);""",
                (crit_id, project_id, max_ord + 1 + i, crit_text, now),
            )

    record_event(
        conn=ctx.conn,
        type_="project.updated",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        credential_id=ctx.credential_id,
        project_id=project_id,
        payload={
            "project_id": project_id,
            "goal": updated_goal,
            "criterion": canonical_params.get("criterion", []),
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(data={"id": project_id, "goal": updated_goal})


def handle_project_set_permission_mode(ctx: HandlerContext) -> HandlerResult:
    """Set a project's per-project permission mode ('supervised' | 'plan' | 'auto'). Direct user only.

    Mode transitions are forward-only:
    - Switching away from 'supervised' ('supervised' -> 'plan' or 'auto') sweep-cancels
      obsolete pending 'step_dispatch' cards with audited 'approval.cancelled' events
      (pending 'permission_escalation' cards are preserved).
    - Switching into 'supervised' does not retroactively re-gate tasks already materialized
      in 'assigned' or later states.
    """
    raw_pid = ctx.args.get("project_id") if ctx.args.get("project_id") is not None else ctx.args.get("project")
    if not isinstance(raw_pid, str) or not raw_pid.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
            details={"param": "project_id"},
        )
    project_id = raw_pid.strip()
    check_authz(ctx.conn, "project.set_permission_mode", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    if ctx.actor_kind != "user" or ctx.via != "direct":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Command 'project.set_permission_mode' requires direct user authentication",
            details={"cmd": "project.set_permission_mode", "required": "user_direct"},
        )
    if not ctx.k_user:
        raise MeshError(
            code=c.PASSPHRASE_REQUIRED,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message="Command 'project.set_permission_mode' requires direct user passphrase",
        )

    raw_mode = ctx.args.get("permission_mode") if "permission_mode" in ctx.args else ctx.args.get("mode")
    if not isinstance(raw_mode, str) or raw_mode not in ("supervised", "plan", "auto"):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'permission_mode' must be 'supervised', 'plan', or 'auto'",
            details={"param": "permission_mode", "expected": ["supervised", "plan", "auto"], "got": raw_mode},
        )
    raw_reason = ctx.args.get("reason")
    if raw_reason is not None and (not isinstance(raw_reason, str) or len(raw_reason) > 4000):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'reason' must be a string up to 4000 characters",
            details={"param": "reason"},
        )
    clean_reason = redact(raw_reason.strip()) if isinstance(raw_reason, str) and raw_reason.strip() else None

    row = ctx.conn.execute(
        "SELECT id, status, plan_gate_mode, permission_mode FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    if row is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )
    if row["status"] in ("completed", "cancelled"):
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is terminal ({row['status']})",
            details={"project_id": project_id, "status": row["status"]},
        )

    old_mode = row["permission_mode"] if "permission_mode" in row.keys() and row["permission_mode"] else "plan"
    now = now_iso_utc()
    cancelled_step_approvals: List[str] = []
    cancelled_plan_approvals: List[str] = []
    events_emitted: List[str] = []
    if old_mode != raw_mode:
        ctx.conn.execute(
            "UPDATE projects SET permission_mode = ?, updated_at = ?, version = version + 1 WHERE id = ?;",
            (raw_mode, now, project_id),
        )
        payload = {
            "project_id": project_id,
            "from_mode": old_mode,
            "to_mode": raw_mode,
        }
        if clean_reason:
            payload["reason"] = clean_reason
        record_event(
            conn=ctx.conn,
            type_="project.permission_mode_changed",
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            credential_id=ctx.credential_id,
            project_id=project_id,
            reason=clean_reason,
            payload=payload,
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
        events_emitted.append("project.permission_mode_changed")
        from mesh.core.approvals import (
            cancel_pending_plan_dispatch_approvals,
            cancel_pending_step_dispatch_approvals,
        )

        cancelled_step_approvals = cancel_pending_step_dispatch_approvals(
            ctx.conn,
            project_id,
            from_mode=old_mode,
            to_mode=raw_mode,
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
        cancelled_plan_approvals = cancel_pending_plan_dispatch_approvals(
            ctx.conn,
            project_id,
            from_mode=old_mode,
            to_mode=raw_mode,
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
        if cancelled_step_approvals or cancelled_plan_approvals:
            events_emitted.append("approval.cancelled")

    return HandlerResult(
        data={
            "id": project_id,
            "project_id": project_id,
            "plan_gate_mode": row["plan_gate_mode"],
            "permission_mode": raw_mode,
            "previous_permission_mode": old_mode,
            "changed": old_mode != raw_mode,
            "cancelled_step_approvals": cancelled_step_approvals,
            "cancelled_plan_approvals": cancelled_plan_approvals,
        },
        events=events_emitted,
    )


def handle_project_complete(ctx: HandlerContext) -> HandlerResult:
    """Mark a project completed with criteria verification per plan §6.1 P4."""
    project_id = _require_authorized_project_id(ctx, "project.complete")

    cur = ctx.conn.execute("SELECT id, status, origin_principal_id FROM projects WHERE id = ?;", (project_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    status = row["status"]
    if status != "active":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {status}, not active",
            details={"project_id": project_id, "status": status},
        )

    raw_report = ctx.args.get("report")
    if not raw_report:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'report' is required",
            details={"param": "report"},
        )

    if isinstance(raw_report, str):
        try:
            report = json.loads(raw_report)
        except Exception as exc:
            raise MeshError(
                code=c.INVALID_JSON,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid report JSON: {exc}",
            )
    else:
        report = raw_report

    if not isinstance(report, dict) or "criteria" not in report or not isinstance(report["criteria"], list):
        raise MeshError(
            code=c.CRITERIA_REPORT_INCOMPLETE,
            exit_code=c.EXIT_USAGE,
            message="Report must contain a 'criteria' list",
            details={"missing": "criteria"},
        )

    # Guard (d) checked first (plan §6.1 P4): report covers every current criterion id with met:true and non-empty evidence
    crit_cur = ctx.conn.execute(
        "SELECT id, text FROM acceptance_criteria WHERE project_id = ? AND superseded_at IS NULL;",
        (project_id,),
    )
    current_criteria = {r["id"]: r["text"] for r in crit_cur.fetchall()}

    report_crit_map = {}
    for cr in report["criteria"]:
        if isinstance(cr, dict) and "id" in cr:
            report_crit_map[cr["id"]] = cr

    for crit_id in current_criteria:
        if crit_id not in report_crit_map:
            raise MeshError(
                code=c.CRITERIA_REPORT_INCOMPLETE,
                exit_code=c.EXIT_USAGE,
                message=f"Report omits acceptance criterion '{crit_id}'",
                details={"criterion_id": crit_id, "reason": "omitted"},
            )
        entry = report_crit_map[crit_id]
        if entry.get("met") is not True:
            raise MeshError(
                code=c.CRITERIA_REPORT_INCOMPLETE,
                exit_code=c.EXIT_USAGE,
                message=f"Criterion '{crit_id}' is not marked met:true",
                details={"criterion_id": crit_id, "reason": "not_met"},
            )
        raw_ev = entry.get("evidence")
        if not isinstance(raw_ev, str) or not raw_ev.strip():
            raise MeshError(
                code=c.CRITERIA_REPORT_INCOMPLETE,
                exit_code=c.EXIT_USAGE,
                message=f"Criterion '{crit_id}' has empty evidence",
                details={"criterion_id": crit_id, "reason": "empty_evidence"},
            )

    # Guards (a), (b), (c): tasks, leases, approvals
    blocking: List[str] = []

    # (a) every non-cancelled task is merged (code) or accepted (general), plan P4.
    # Pre-public review P2: an accepted-but-unmerged code task must still block.
    tcur = ctx.conn.execute(
        """SELECT id, status FROM tasks
           WHERE project_id = ? AND status != 'cancelled'
             AND NOT (kind = 'code' AND status = 'merged')
             AND NOT (kind = 'general' AND status IN ('accepted', 'merged'));""",
        (project_id,),
    )
    for tr in tcur.fetchall():
        blocking.append(tr["id"])

    # (b) leases. Actions execute before the lead turn closes: only its verified
    # run-token caller may disregard exactly that run's own leadturn lease.
    own_turn = None
    if lead_run_token_scope(ctx.principal, ctx.via) == project_id:
        scope = ctx.principal["scope"]
        own_turn = ctx.conn.execute(
            """SELECT id FROM runs WHERE id = ? AND kind = 'lead_turn'
               AND project_id = ? AND agent_id = ? AND lead_assignment_id = ?
               AND status IN ('starting', 'running');""",
            (scope.get("run_id"), project_id, ctx.principal["id"], scope.get("lead_assignment_id")),
        ).fetchone()
    lcur = ctx.conn.execute(
        """SELECT l.key, l.run_id FROM leases l
           WHERE l.key IN (SELECT 'task:' || id FROM tasks WHERE project_id = ?)
              OR l.key = 'leadturn:' || ? OR l.key = 'merge:' || ?;""",
        (project_id, project_id, project_id),
    )
    for lr in lcur.fetchall():
        if own_turn and lr["key"] == f"leadturn:{project_id}" and lr["run_id"] == own_turn["id"]:
            continue
        blocking.append(lr["key"])

    # (c) approvals
    acur = ctx.conn.execute(
        "SELECT id, status FROM approvals WHERE project_id = ? AND status IN ('pending', 'approved');",
        (project_id,),
    )
    for ar in acur.fetchall():
        blocking.append(ar["id"])

    # (d) active workspace delete/prune reservations
    blocking.extend(list_project_ws_reservation_keys(ctx.conn, project_id))

    if blocking:
        raise MeshError(
            code=c.COMPLETION_BLOCKED,
            exit_code=c.EXIT_CONFLICT,
            message="Project completion is blocked by non-terminal tasks, leases, or pending approvals",
            details={"blocking": blocking},
        )

    report = redact_value(report)
    now = now_iso_utc()
    ctx.conn.execute(
        """UPDATE projects SET
            status = 'completed',
            completion_report_json = ?,
            completed_at = ?,
            updated_at = ?
        WHERE id = ?;""",
        (json.dumps(report), now, now, project_id),
    )

    event = record_event(
        conn=ctx.conn,
        type_="project.completed",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        from_state="active",
        to_state="completed",
        payload={"summary": report.get("summary", ""), "for_user": True},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    # Fan-out to user and origin agent
    ucur = ctx.conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;")
    urow = ucur.fetchone()
    user_id = urow["id"] if urow else None

    recipients = set()
    if user_id:
        recipients.add(user_id)
    origin_id = row["origin_principal_id"]
    if origin_id:
        recipients.add(origin_id)

    for rec_id in recipients:
        msg_id = generate_id("msg")
        dedup = f"project.completed:{project_id}:{rec_id}"
        ctx.conn.execute(
            """INSERT INTO outbox (
                id, event_seq, recipient_id, channel, dedup_key, project_id,
                type, payload_json, status, attempts, next_attempt_at, created_at
            ) VALUES (?, ?, ?, 'inbox', ?, ?, 'project.completed', ?, 'pending', 0, ?, ?)
            ON CONFLICT(dedup_key) DO NOTHING;""",
            (
                msg_id,
                event["seq"],
                rec_id,
                dedup,
                project_id,
                json.dumps({
                    "project_id": project_id,
                    "summary": report.get("summary", ""),
                    "for_user": True,
                }),
                now,
                now,
            ),
        )

    return HandlerResult(data={"id": project_id, "status": "completed", "completed_at": now})


def handle_project_note(ctx: HandlerContext) -> HandlerResult:
    """Add an audit note to a project per plan §8."""
    project_id = _require_authorized_project_id(ctx, "project.note")
    text = ctx.args.get("text")

    if not isinstance(text, str) or not text or len(text) > 4000:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Note text must be non-empty and <= 4000 characters",
            details={"param": "text"},
        )

    cur = ctx.conn.execute("SELECT id, status FROM projects WHERE id = ?;", (project_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )
    if row["status"] != "active":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {row['status']}, not active",
            details={"project_id": project_id, "status": row["status"]},
        )

    record_event(
        conn=ctx.conn,
        type_="project.note",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        payload={"text": text},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(data={"project_id": project_id, "noted": True})


async def handle_project_wait(server: Any, ctx: HandlerContext, reader: Any = None) -> HandlerResult:
    """Block until project reaches completed or terminal state per plan §13."""
    project_id = _require_authorized_project_id(ctx, "project.wait")

    cur = ctx.conn.execute(
        """SELECT pr.id, pr.name, pr.goal, pr.status, pr.plan_gate_mode, pr.permission_mode, pr.created_via,
                  pr.created_at, pr.updated_at, pr.completed_at, pr.origin_ref,
                  op.name as origin, lp.name as lead,
                  pr.repo_path, pr.base_branch, pr.integration_branch, pr.base_commit,
                  pr.test_command_json
           FROM projects pr
           LEFT JOIN principals op ON pr.origin_principal_id = op.id
           LEFT JOIN lead_assignments la ON pr.id = la.project_id AND la.ended_at IS NULL
           LEFT JOIN principals lp ON la.agent_id = lp.id
           WHERE pr.id = ?;""",
        (project_id,),
    )
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    until = ctx.args.get("until", "completed")
    if not isinstance(until, str):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Parameter 'until' must be a string",
            details={"param": "until", "got": type(until).__name__},
        )
    from mesh.core.states import PROJECT_TERMINAL_STATES
    if until == "completed":
        match_fn = lambda s: s == "completed"
    elif until == "terminal":
        match_fn = lambda s: s in PROJECT_TERMINAL_STATES
    else:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid --until target: '{until}'. Must be 'completed' or 'terminal'",
            details={"param": "until"},
        )

    timeout_val = ctx.args.get("timeout")
    if not timeout_val:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--timeout is required for project wait",
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

            # Blind review P2: re-check credential and access on every pass of the wait.
            _recheck_wait_authz(ctx, "project.wait", project_id=project_id)
            cur = ctx.conn.execute(
                """SELECT pr.id, pr.name, pr.goal, pr.status, pr.plan_gate_mode, pr.permission_mode, pr.created_via,
                          pr.created_at, pr.updated_at, pr.completed_at, pr.origin_ref,
                          op.name as origin, lp.name as lead,
                          pr.repo_path, pr.base_branch, pr.integration_branch, pr.base_commit,
                          pr.test_command_json
                   FROM projects pr
                   LEFT JOIN principals op ON pr.origin_principal_id = op.id
                   LEFT JOIN lead_assignments la ON pr.id = la.project_id AND la.ended_at IS NULL
                   LEFT JOIN principals lp ON la.agent_id = lp.id
                   WHERE pr.id = ?;""",
                (project_id,),
            )
            row = cur.fetchone()
            if match_fn(row["status"]):
                data = dict(row)
                repo_cols = {k: data.pop(k) for k in ("repo_path", "base_branch", "integration_branch",
                                                      "base_commit", "test_command_json")}
                if repo_cols["repo_path"]:
                    data.update(_repo_view(repo_cols))
                ccur = ctx.conn.execute(
                    """SELECT id, ordinal, text, created_at, superseded_at
                       FROM acceptance_criteria
                       WHERE project_id = ? AND superseded_at IS NULL
                       ORDER BY ordinal ASC;""",
                    (project_id,),
                )
                data["criteria"] = [dict(r) for r in ccur.fetchall()]
                return HandlerResult(data=data)

            remaining = end_mono - time.monotonic()
            if remaining <= 0:
                raise MeshError(
                    code=c.WAIT_TIMEOUT,
                    exit_code=c.EXIT_WAIT_TIMEOUT,
                    message=f"Project wait timed out after {timeout_s}s",
                    details={"project": project_id, "until": until},
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
