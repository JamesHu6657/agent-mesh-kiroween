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
from mesh.core.paths import check_install_path_secure, get_install_root
from mesh.core.repos import (
    encode_test_command,
    integration_branch_name,
    parse_test_command,
    setup_integration,
    tests_view,
    validate_repo,
)
from mesh.core.workspaces import compute_lead_workspace_path, get_workspaces_root
from mesh.daemon.handlers.base import Effect, HandlerContext, HandlerResult
from mesh.errors import MeshError


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


def _check_lead_launch(ctx: HandlerContext, adapter_id: str) -> None:
    """A launch-mode lead must pass the lead-turn launch gate (rc 13, AG step 4)."""
    from mesh.adapters.registry import get_adapter
    from mesh.daemon.lead_turns import lead_launch_check

    # C-5: no binary_path here on purpose (only the adapter id is known at this point); vendor
    # leads are refused before the evidence gate, and non-vendor kinds ignore the binary.
    lead_launch_check(ctx.conn, get_adapter(adapter_id, testing_mode=ctx.testing_mode), ctx.testing_mode)


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
    # The repo must not overlap the install root in either direction (before any git call).
    check_install_path_secure(install_root=get_install_root(), repo_paths=[repo])
    info = validate_repo(repo, ctx.args.get("base_branch"))
    info["tests"] = tests
    return info


def handle_intake_submit(ctx: HandlerContext) -> HandlerResult:
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
        if not message:
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
    if not title:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'title' is required",
            details={"param": "title"},
        )

    goal = ctx.args.get("goal")
    if not goal:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'goal' is required",
            details={"param": "goal"},
        )

    criteria = ctx.args.get("criterion") or ctx.args.get("criteria") or []
    if not criteria or not any(str(cr).strip() for cr in criteria):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="At least one acceptance criterion is required",
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
        """SELECT a.id, a.can_lead, a.delivery, a.adapter, p.disabled_at, p.name
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
    if arow["delivery"] == "launch":
        _check_lead_launch(ctx, arow["adapter"])

    lead_id = arow["id"]
    lead_delivery = arow["delivery"]

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
            repo_path, base_branch, integration_branch, base_commit, test_command_json
        ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?);""",
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

    deps = ctx.args.get("depends_on_project") or []
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
        "created_via": created_via,
        "origin": ctx.principal["name"],
        "criteria": criteria_records,
        "created_at": now,
    }
    if repo_info is not None:
        data.update(_repo_view(repo_cols))
    return HandlerResult(data=data)


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
    """Project create is an alias for intake submit."""
    return handle_intake_submit(ctx)


def handle_project_leads(ctx: HandlerContext) -> HandlerResult:
    """List lead assignment history for a project."""
    project_id = ctx.args.get("project_id")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
        )

    # Verify project exists
    pcur = ctx.conn.execute("SELECT id FROM projects WHERE id = ?;", (project_id,))
    if not pcur.fetchone():
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    check_authz(ctx.conn, "project.leads", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

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
    items = [dict(r) for r in cur.fetchall()]
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
        SELECT pr.id, pr.name, pr.goal, pr.status, pr.created_via,
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
    project_id = ctx.args.get("project_id")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
        )

    cur = ctx.conn.execute(
        """SELECT pr.id, pr.name, pr.goal, pr.status, pr.created_via,
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

    check_authz(ctx.conn, "project.show", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

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
        data["lead_history"] = [dict(r) for r in lcur.fetchall()]

    return HandlerResult(data=data)


def handle_project_set_lead(ctx: HandlerContext) -> HandlerResult:
    """Change the designated lead agent of a project per plan §8."""
    project_id = ctx.args.get("project_id")
    new_lead_name = ctx.args.get("agent")
    reason = ctx.args.get("reason")

    if not project_id or not new_lead_name or not reason:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id, agent, and reason are required",
        )

    # 1. Project must exist and be active
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

    # 2. Authorize
    check_authz(ctx.conn, "project.set_lead", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

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
            )

    # 3. New lead agent validation
    acur = ctx.conn.execute(
        """SELECT a.id, a.can_lead, a.delivery, a.adapter, p.disabled_at, p.name
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
    if arow["delivery"] == "launch":
        _check_lead_launch(ctx, arow["adapter"])

    new_lead_id = arow["id"]
    new_lead_delivery = arow["delivery"]

    # 4. Check active tasks where new lead is assignee (checklist E13)
    tcur = ctx.conn.execute(
        """SELECT id FROM tasks
           WHERE project_id = ? AND assignee_agent_id = ?
             AND status NOT IN ('merged', 'accepted', 'cancelled');""",
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
    old_lead_name = old_lead["agent_name"] if old_lead else "none"
    old_lead_id = old_lead["agent_id"] if old_lead else None

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

    # Step 4: Cancel pending approvals requested by old lead (checklist E10)
    if old_lead_id:
        ctx.conn.execute(
            """UPDATE approvals SET status = 'cancelled'
               WHERE requested_by = ? AND project_id = ? AND status = 'pending';""",
            (old_lead_id, project_id),
        )

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

    handover_payload = {
        "project_id": project_id,
        "goal": prow["goal"],
        "criteria": criteria_list,
        "tasks": tasks_list,
        "notes": notes_list,
        "open_approvals": open_aprs,
        "recent_events": recent_events,
    }

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
                json.dumps({"project_id": project_id, "new_lead": new_lead_name, "reason": reason}),
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
        }
    )


def handle_project_pause(ctx: HandlerContext) -> HandlerResult:
    """Pause a project per plan §6.1 P2."""
    project_id = ctx.args.get("project_id")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
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

    check_authz(ctx.conn, "project.pause", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

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
    project_id = ctx.args.get("project_id")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
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

    check_authz(ctx.conn, "project.resume", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

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
            )

    now = now_iso_utc()
    ctx.conn.execute("UPDATE projects SET status = 'active', updated_at = ? WHERE id = ?;", (now, project_id))

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
    project_id = ctx.args.get("project_id")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
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

    status = row["status"]
    if status in ("completed", "cancelled"):
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is already terminal ({status})",
            details={"project_id": project_id, "status": status},
        )

    check_authz(ctx.conn, "project.cancel", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

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

    # Cancel pending approvals
    ctx.conn.execute(
        "UPDATE approvals SET status = 'cancelled' WHERE project_id = ? AND status = 'pending';",
        (project_id,),
    )

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


def handle_project_deliver(ctx: HandlerContext) -> HandlerResult:
    """`project deliver` (§10.6): fast-forward the base branch to the integration branch.

    Lead or user, category `deliver_to_base` (default `not_required`; a tightened policy
    goes through the usual `--approval` consume path). Mesh never pushes, never rebases
    and never forces: a base that is not an ancestor of the integration branch is rc 14
    `NOT_FAST_FORWARD` with nothing changed.
    """
    from mesh.core.repos import deliver_to_base

    project_id = ctx.args.get("project_id")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
            details={"param": "project_id"},
        )
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
    check_authz(ctx.conn, "project.deliver", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)
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

    result = deliver_to_base(row["repo_path"], base, integration)

    if result["method"] == "noop":
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
    project_id = ctx.args.get("project_id")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
            details={"param": "project_id"},
        )

    goal = ctx.args.get("goal")
    criterion = ctx.args.get("criterion") or []
    if isinstance(criterion, str):
        criterion = [criterion]

    if goal is None and not criterion:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="At least one of --goal or --criterion must be provided",
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

    check_authz(ctx.conn, "project.update", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

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


def handle_project_complete(ctx: HandlerContext) -> HandlerResult:
    """Mark a project completed with criteria verification per plan §6.1 P4."""
    project_id = ctx.args.get("project_id")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
        )

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

    check_authz(ctx.conn, "project.complete", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

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
        evidence = str(entry.get("evidence", "")).strip()
        if not evidence:
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

    if blocking:
        raise MeshError(
            code=c.COMPLETION_BLOCKED,
            exit_code=c.EXIT_CONFLICT,
            message="Project completion is blocked by non-terminal tasks, leases, or pending approvals",
            details={"blocking": blocking},
        )

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
    project_id = ctx.args.get("project_id")
    text = ctx.args.get("text")

    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
        )
    if not text or len(text) > 4000:
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

    check_authz(ctx.conn, "project.note", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

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
    project_id = ctx.args.get("project_id")
    if not project_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="project_id is required",
            details={"param": "project_id"},
        )

    cur = ctx.conn.execute(
        """SELECT pr.id, pr.name, pr.goal, pr.status, pr.created_via,
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

    check_authz(ctx.conn, "project.wait", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    until = ctx.args.get("until", "completed")
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

            cur = ctx.conn.execute(
                """SELECT pr.id, pr.name, pr.goal, pr.status, pr.created_via,
                          pr.created_at, pr.updated_at, pr.completed_at, pr.origin_ref,
                          op.name as origin, lp.name as lead
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
