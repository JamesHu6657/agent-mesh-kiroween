"""Authorization matrix and capability checking per plan §7.2 and §7.3."""

import json
import sqlite3
from typing import Any, Dict, List, Optional

import mesh.constants as c
from mesh.errors import MeshError


def get_project_active_lead(conn: sqlite3.Connection, project_id: str) -> Optional[Dict[str, Any]]:
    """Get the active lead assignment and agent details for a project."""
    cur = conn.execute(
        """SELECT la.id as assignment_id, la.agent_id, la.started_at,
                  p.name as agent_name, a.delivery, a.can_lead
           FROM lead_assignments la
           JOIN principals p ON la.agent_id = p.id
           JOIN agents a ON la.agent_id = a.id
           WHERE la.project_id = ? AND la.ended_at IS NULL LIMIT 1;""",
        (project_id,),
    )
    row = cur.fetchone()
    if row:
        return dict(row)
    return None


def is_project_lead(conn: sqlite3.Connection, project_id: str, principal_id: str) -> bool:
    """Check if principal_id is the active lead for project_id."""
    lead = get_project_active_lead(conn, project_id)
    if lead and lead["agent_id"] == principal_id:
        return True
    return False


def can_principal_read_project(conn: sqlite3.Connection, principal_id: str, project_id: str) -> bool:
    """Check if agent has read access to project (lead, past lead, or origin)."""
    if is_project_lead(conn, project_id, principal_id):
        return True
    cur = conn.execute("SELECT 1 FROM lead_assignments WHERE project_id = ? AND agent_id = ? LIMIT 1;", (project_id, principal_id))
    if cur.fetchone():
        return True
    cur2 = conn.execute("SELECT 1 FROM projects WHERE id = ? AND origin_principal_id = ? LIMIT 1;", (project_id, principal_id))
    if cur2.fetchone():
        return True
    return False


def can_principal_read_task(conn: sqlite3.Connection, principal_id: str, task_id: str) -> bool:
    """Check if agent has read access to task (assignee, or project read access)."""
    cur = conn.execute("SELECT project_id, assignee_agent_id FROM tasks WHERE id = ? LIMIT 1;", (task_id,))
    row = cur.fetchone()
    if not row:
        return False
    if row["assignee_agent_id"] == principal_id:
        return True
    return can_principal_read_project(conn, principal_id, row["project_id"])


def get_agent_capabilities(conn: sqlite3.Connection, agent_id: str) -> List[str]:
    """Retrieve capabilities list for an agent."""
    cur = conn.execute("SELECT capabilities_json FROM agents WHERE id = ?;", (agent_id,))
    row = cur.fetchone()
    if row and row["capabilities_json"]:
        try:
            return json.loads(row["capabilities_json"])
        except Exception:
            return []
    return []


#: Commands allowed without credentials (`auth.type == "none"`).
UNAUTH_CMDS = frozenset({
    "daemon.status",
    "version",
})

#: Basic daemon/caller info commands allowed for any authenticated direct/token/active-run caller
#: (never relayed, and never for a superseded lead run token).
ANY_AUTH_INFO_CMDS = frozenset({
    "daemon.status",
    "version",
    "whoami",
})

#: Adapter inspection/probe commands (§7.2, §7.3, §13.3): user direct or direct agent token only.
#: Denied for anonymous (`TOKEN_MISSING`), run tokens (`CAPABILITY_MISSING` / `LEAD_SUPERSEDED`),
#: and `--relayed` (`RELAY_NOT_ALLOWED`).
ADAPTER_CMDS = frozenset({
    "adapter.list",
    "adapter.show",
    "adapter.probe",
    "adapter.models",
})

#: Operator-only commands requiring direct user authentication (checked after superseded-lead
#: and relay gates so superseded lead turns get `LEAD_SUPERSEDED` and `--relayed` gets `RELAY_NOT_ALLOWED`).
USER_DIRECT_CMDS = frozenset({
    "daemon.stop",
    "run.release",
})

#: The lead column of the §7.3 matrix, as the lead run token sees it (§7.2).
#: `config.*`, `agent.*`, `agent token.*`, `audit.*` and `daemon stop` are user-only
#: and are rejected before this set is consulted.
LEAD_RUN_TOKEN_CMDS = frozenset({
    # intake submit / project create (lead needs the `intake` capability, checked below)
    "intake.submit",
    "project.create",
    # project complete/note/deliver
    "project.complete",
    "project.note",
    "project.deliver",
    # task create/assign/reassign/accept/reject/retry/escalate/cancel/merge, dep *
    "task.create",
    "task.assign",
    "task.reassign",
    "task.accept",
    "task.reject",
    "task.retry",
    "task.escalate",
    "task.cancel",
    "task.merge",
    "dep.add",
    "dep.remove",
    # approval request, and check/consume as the requester (checked in the handler)
    "approval.request",
    "approval.check",
    "approval.consume",
    # read commands: own project only
    "project.show",
    "project.leads",
    "project.list",
    "project.wait",
    "task.show",
    "task.list",
    "task.wait",
    "task.log",
    "dep.list",
    "event.list",
    "event.tail",
    "run.log",
    "run.list",
    "run.show",
    "workspace.list",
    "workspace.show",
    "approval.show",
    "approval.list",
    "inbox.list",
    "inbox.wait",
    "inbox.ack",
})

#: Lead run-token commands that act on a task: the task must be in `scope.project_id`.
#: Review m2-r9 finding 1(b): every command that carries a `task_id` is checked, so
#: `event.list`, `event.tail`, `run.list`, `run.show` and `run.log` are included.
LEAD_RUN_TOKEN_TASK_CMDS = frozenset({
    "task.show",
    "task.list",
    "task.wait",
    "task.log",
    "dep.list",
    "workspace.show",
    "task.create",
    "task.assign",
    "task.reassign",
    "task.accept",
    "task.reject",
    "task.retry",
    "task.escalate",
    "task.cancel",
    "task.merge",
    "dep.add",
    "dep.remove",
    "event.list",
    "event.tail",
    "run.list",
    "run.show",
    "run.log",
})

#: Documentation of the lead run-token list/read commands that name no project. Each
#: handler confines itself to `scope.project_id` by calling `lead_run_token_scope`
#: (review m2-r9 finding 1(a), r9c N1), so a lead turn never sees another project's
#: rows. This set is not consulted at runtime.
LEAD_RUN_TOKEN_SCOPED_READS = frozenset({
    "project.list",
    "task.list",
    "event.list",
    "event.tail",
    "run.list",
    "inbox.list",
    "inbox.wait",
    "inbox.ack",
    "approval.list",
    "workspace.list",
})


def lead_run_token_scope(principal: Dict[str, Any], via: str) -> Optional[str]:
    """The project a lead run token is confined to, or None for every other caller.

    Review m2-r9 finding 1(a): handlers use this to add `project_id = scope.project_id`
    to list/read queries. The relay capability never widens a run token's reads.
    """
    if via != "run_token":
        return None
    scope = principal.get("scope") or {}
    if scope.get("role") != "lead":
        return None
    return scope.get("project_id")


def _lead_superseded(project_id: Optional[str], assignment_id: Optional[str],
                     current: Optional[str]) -> MeshError:
    return MeshError(
        code=c.LEAD_SUPERSEDED,
        exit_code=c.EXIT_STALE,
        message=(
            f"Lead assignment '{assignment_id}' is no longer the current lead of "
            f"project '{project_id}'"
        ),
        details={"project_id": project_id, "lead_assignment_id": assignment_id,
                 "current_lead_assignment_id": current},
    )


def _check_lead_run_token(
    conn: sqlite3.Connection,
    cmd: str,
    principal: Dict[str, Any],
    project_id: Optional[str],
    task_id: Optional[str],
    resource_resolved: bool = True,
) -> None:
    """Authorize a lead run token (§7.1, §7.2, §7.3).

    Order: (1) the assignment must still be current → rc 9 `LEAD_SUPERSEDED`, checked
    before every other lead check; (2) the command must be in the lead column;
    (3) the resource must be inside `scope.project_id`.
    """
    scope = principal.get("scope") or {}
    scope_project = scope.get("project_id")
    assignment_id = scope.get("lead_assignment_id")

    # (1) A superseded lead turn can do nothing at all, not even read (§7.2).
    current = get_project_active_lead(conn, scope_project) if scope_project else None
    current_id = current["assignment_id"] if current else None
    if not assignment_id or current_id != assignment_id:
        raise _lead_superseded(scope_project, assignment_id, current_id)

    # (2) The lead column of §7.3 (plus basic caller info commands; never adapter.*).
    if cmd not in LEAD_RUN_TOKEN_CMDS and cmd not in ANY_AUTH_INFO_CMDS:
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Lead run tokens cannot execute command '{cmd}'",
            details={"cmd": cmd, "via": "run_token", "role": "lead"},
        )

    # (3) Scope: the lead acts only for its own project.
    # Review m2-r9 finding 1(c): a lead turn may follow up on its own project, but it
    # may not open new intake or create a project (no project to scope it to).
    if resource_resolved and cmd in ("intake.submit", "project.create") and project_id is None:
        raise MeshError(
            code=c.SCOPE_MISMATCH,
            exit_code=c.EXIT_FORBIDDEN,
            message="Lead run token can only submit a follow-up to its own project",
            details={"scope_project_id": scope_project, "cmd": cmd},
        )
    if project_id is not None and project_id != scope_project:
        raise MeshError(
            code=c.SCOPE_MISMATCH,
            exit_code=c.EXIT_FORBIDDEN,
            message="Lead run token cannot access a project outside its run scope",
            details={"scope_project_id": scope_project, "project_id": project_id},
        )
    if task_id is not None and cmd in LEAD_RUN_TOKEN_TASK_CMDS:
        row = conn.execute("SELECT project_id FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        if row is None or row["project_id"] != scope_project:
            raise MeshError(
                code=c.SCOPE_MISMATCH,
                exit_code=c.EXIT_FORBIDDEN,
                message="Lead run token cannot access a task outside its run scope",
                details={"scope_project_id": scope_project, "task_id": task_id},
            )

    # `intake submit` / `project create` still need the agent's `intake` capability (§7.2).
    if cmd in ("intake.submit", "project.create"):
        caps = principal.get("capabilities", [])
        if not caps and principal.get("id"):
            caps = get_agent_capabilities(conn, principal["id"])
        if "intake" not in caps:
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Agent '{principal.get('name')}' lacks 'intake' capability",
                details={"principal": principal.get("name"), "missing": "intake"},
            )


def check_authz(
    conn: sqlite3.Connection,
    cmd: str,
    principal: Dict[str, Any],
    actor_kind: str,
    via: str,
    project_id: Optional[str] = None,
    task_id: Optional[str] = None,
    relayed_by: Optional[str] = None,
    resource_resolved: bool = True,
) -> None:
    """Authorize an operation according to the authorization matrix in plan §7.3."""
    # 0. Anonymous / unauthenticated callers (`auth.type == "none"`)
    if via == "none" or actor_kind == "anonymous":
        if cmd in UNAUTH_CMDS:
            return
        raise MeshError(
            code=c.TOKEN_MISSING,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message=f"Command '{cmd}' requires authentication (--user or --token-file)",
        )

    # 1. User direct factor
    if actor_kind == "user" and via == "direct":
        return

    # 2. Config, agent, and audit operations are user-only (direct factor)
    if cmd.startswith("config.") or cmd.startswith("agent.") or cmd.startswith("audit."):
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Command '{cmd}' requires direct user authentication",
            details={"cmd": cmd, "required": "user_direct"},
        )

    # 3. Lead run tokens (§7.1, §7.2, §7.3): the lead column of the matrix, for
    # `scope.project_id` only, and only while `scope.lead_assignment_id` is current.
    if via == "run_token" and (principal.get("scope") or {}).get("role") == "lead":
        return _check_lead_run_token(
            conn, cmd, principal, project_id, task_id, resource_resolved=resource_resolved
        )

    # 3b. Executor run tokens have strictly scoped task capabilities only (§7.2, §7.3)
    if via == "run_token":
        allowed_run_cmds = {
            "task.heartbeat",
            "task.progress",
            "task.submit",
            "task.fail",
            "task.show",
            "project.show",
            # §7.3: "own task" reads
            "task.log",
            "run.show",
            "run.log",
            "workspace.show",
        }
        if cmd not in allowed_run_cmds and cmd not in ANY_AUTH_INFO_CMDS:
            # §7.2: the executor run token simply lacks the capability (H12).
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Executor run tokens cannot execute command '{cmd}'",
                details={"cmd": cmd, "via": via, "role": "executor"},
            )
        if not resource_resolved:
            return
        scope = principal.get("scope", {})
        if cmd in ("task.heartbeat", "task.progress", "task.submit", "task.fail"):
            # Fenced writes only on scope.task_id (§7.2, H12); the fence is checked by the handler (exit 9).
            if not task_id or scope.get("task_id") != task_id:
                raise MeshError(
                    code=c.SCOPE_MISMATCH,
                    exit_code=c.EXIT_FORBIDDEN,
                    message="Run token cannot write to a task outside its run scope",
                    details={"scope_task_id": scope.get("task_id"), "task_id": task_id},
                )
        if cmd in ("task.show", "task.log", "run.show", "run.log", "workspace.show"):
            if not task_id or scope.get("task_id") != task_id:
                raise MeshError(
                    code=c.SCOPE_MISMATCH,
                    exit_code=c.EXIT_FORBIDDEN,
                    message="Run token cannot access task outside its run scope",
                    details={"scope_task_id": scope.get("task_id"), "task_id": task_id},
                )
        if cmd == "project.show":
            if not project_id or scope.get("project_id") != project_id:
                raise MeshError(
                    code=c.SCOPE_MISMATCH,
                    exit_code=c.EXIT_FORBIDDEN,
                    message="Run token cannot access project outside its run scope",
                    details={"scope_project_id": scope.get("project_id"), "project_id": project_id},
                )
        return

    # 4. Relayed actions
    if via == "relayed":
        # Relayer must have 'relay' capability
        caps = principal.get("capabilities", [])
        if not caps and principal.get("id"):
            caps = get_agent_capabilities(conn, principal["id"])
        if "relay" not in caps:
            raise MeshError(
                code=c.RELAY_NOT_ALLOWED,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Principal '{principal.get('name')}' lacks 'relay' capability",
                details={"principal": principal.get("name"), "missing": "relay"},
            )

        # Plan §7.3 and §7.5: only user-authority project ops, approvals, intake, and reads are relayable
        allowed_relayed_cmds = {
            "project.set_lead",
            "project.pause",
            "project.resume",
            "project.cancel",
            "project.update",
            "approval.request",
            "approval.approve",
            "approval.reject",
            "intake.submit",
            "project.create",
            # Read commands
            "project.show",
            "project.list",
            "project.leads",
            "task.show",
            "task.list",
            "dep.list",
            "event.list",
            "event.tail",
            "run.log",
            "run.list",
            "run.show",
            "task.log",
            "workspace.list",
            "workspace.show",
            "inbox.list",
            "inbox.wait",
            "inbox.ack",
            "task.wait",
            "project.wait",
            "approval.show",
            "approval.list",
        }
        if cmd not in allowed_relayed_cmds:
            raise MeshError(
                code=c.RELAY_NOT_ALLOWED,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Command '{cmd}' cannot be executed via relay",
                details={"cmd": cmd, "via": via},
            )

        # Relayer cannot be current lead of the affected project
        if project_id and is_project_lead(conn, project_id, principal["id"]):
            raise MeshError(
                code=c.SELF_APPROVAL,
                exit_code=c.EXIT_FORBIDDEN,
                message="Project lead cannot act as relayer for its own project",
                details={"project_id": project_id, "principal": principal.get("name")},
            )

        if cmd in ("intake.submit", "project.create"):
            if "intake" not in caps:
                raise MeshError(
                    code=c.CAPABILITY_MISSING,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{principal.get('name')}' lacks 'intake' capability",
                    details={"principal": principal.get("name"), "missing": "intake"},
                )
        return

    # 4b. Operator-only commands requiring direct user authentication
    if cmd in USER_DIRECT_CMDS:
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Command '{cmd}' requires direct user authentication",
            details={"cmd": cmd, "required": "user_direct"},
        )

    # 4c. Basic info commands and adapter inspection/probe commands (direct agent token allowed)
    if cmd in ANY_AUTH_INFO_CMDS or cmd in ADAPTER_CMDS:
        return

    # 5. Intake submit / project create
    if cmd in ("intake.submit", "project.create"):
        caps = principal.get("capabilities", [])
        if not caps and principal.get("id"):
            caps = get_agent_capabilities(conn, principal["id"])
        if "intake" not in caps:
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Agent '{principal.get('name')}' lacks 'intake' capability",
                details={"principal": principal.get("name"), "missing": "intake"},
            )
        return

    # 6. Project operations by lead or user
    if cmd in ("project.complete", "project.note"):
        if project_id:
            if not is_project_lead(conn, project_id, principal["id"]):
                raise MeshError(
                    code=c.NOT_PROJECT_LEAD,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{principal.get('name')}' is not the current lead of project '{project_id}'",
                    details={"project_id": project_id, "agent": principal.get("name")},
                )
        return

    # 7. Project control & lead change (pause, resume, cancel, set_lead, update, deliver)
    if cmd in ("project.pause", "project.resume", "project.cancel", "project.set_lead", "project.update",
               "project.deliver"):
        # Lead can execute these via approval
        if project_id and not is_project_lead(conn, project_id, principal["id"]):
            raise MeshError(
                code=c.NOT_PROJECT_LEAD,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Agent '{principal.get('name')}' is not the current lead of project '{project_id}'",
                details={"project_id": project_id, "agent": principal.get("name")},
            )
        return

    # 7b. Workspace maintenance (§10.7, §13.3): lead or user. `workspace delete` is
    # additionally gated by the `delete` approval category in its handler (§7.4).
    # An agent must name its project: without one the command would reach every project's
    # worktrees, which no single lead is entitled to (review m2-r8 MAJOR-1). Only the user
    # may run these unscoped.
    if cmd in ("workspace.prune", "workspace.delete"):
        if resource_resolved and not project_id and principal.get("kind") != "user":
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"'{cmd}' requires an explicit project for agent '{principal.get('name')}'",
                details={"param": "project", "agent": principal.get("name")},
            )
        if project_id and not is_project_lead(conn, project_id, principal["id"]):
            raise MeshError(
                code=c.NOT_PROJECT_LEAD,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Agent '{principal.get('name')}' is not the current lead of project '{project_id}'",
                details={"project_id": project_id, "agent": principal.get("name")},
            )
        return

    # 8. Task and dependency mutations: lead or user only per plan §7.3
    task_mutations = {
        "task.create",
        "task.assign",
        "task.reassign",
        "task.accept",
        "task.reject",
        "task.retry",
        "task.escalate",
        "task.cancel",
        "task.merge",
        "dep.add",
        "dep.remove",
    }
    if cmd in task_mutations:
        if project_id and not is_project_lead(conn, project_id, principal["id"]):
            raise MeshError(
                code=c.NOT_PROJECT_LEAD,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Agent '{principal.get('name')}' is not the current lead of project '{project_id}'",
                details={"project_id": project_id, "agent": principal.get("name")},
            )
        return

    # 8b. Task execution mutations: assignee or user only per plan §7.3
    task_execution_cmds = {
        "task.start",
        "task.heartbeat",
        "task.progress",
        "task.submit",
        "task.fail",
    }
    # Plan §7.2/§7.3: assignee rights on the fenced writes belong to the inbox agent
    # token only. A launch-mode agent's own token must not be able to drive a run.
    fenced_write_cmds = {"task.heartbeat", "task.progress", "task.submit", "task.fail"}
    if cmd in task_execution_cmds:
        if task_id:
            cur = conn.execute(
                """SELECT t.assignee_agent_id, a.delivery
                   FROM tasks t
                   LEFT JOIN agents a ON t.assignee_agent_id = a.id
                   WHERE t.id = ?;""",
                (task_id,),
            )
            row = cur.fetchone()
            if row and row["assignee_agent_id"] != principal.get("id"):
                raise MeshError(
                    code=c.NOT_ASSIGNEE,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{principal.get('name')}' is not the assignee of task '{task_id}'",
                    details={"task_id": task_id, "agent": principal.get("name")},
                )
            if row and cmd in fenced_write_cmds and row["delivery"] != "inbox":
                raise MeshError(
                    code=c.NOT_ASSIGNEE,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=(
                        f"Agent '{principal.get('name')}' is a launch-mode assignee; "
                        f"'{cmd}' is reserved for inbox-mode assignees"
                    ),
                    details={
                        "task_id": task_id,
                        "agent": principal.get("name"),
                        "delivery": row["delivery"],
                        "required_delivery": "inbox",
                    },
                )
        return

    # 9. Read commands (project.leads, project.show, project.list, task.show, task.list, dep.list, etc.)
    read_cmds = {
        "project.show",
        "project.leads",
        "project.list",
        "task.show",
        "task.list",
        "dep.list",
        "event.list",
        "event.tail",
        "run.log",
        "run.list",
        "run.show",
        "task.log",
        "workspace.list",
        "workspace.show",
        "task.wait",
        "project.wait",
        "inbox.list",
        "inbox.wait",
        "inbox.ack",
        "approval.show",
        "approval.list",
    }
    if cmd in read_cmds:
        caps = principal.get("capabilities", [])
        if not caps and principal.get("id"):
            caps = get_agent_capabilities(conn, principal["id"])
        # Relayers read all projects and approvals per plan §7.3
        if "relay" in caps:
            return

        agent_id = principal.get("id")
        if not agent_id:
            return

        if cmd in ("task.show", "dep.list", "task.wait", "task.log", "workspace.show") or (
            cmd in ("run.show", "run.log", "run.list") and task_id
        ):
            if task_id and not can_principal_read_task(conn, agent_id, task_id):
                raise MeshError(
                    code=c.SCOPE_MISMATCH,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{principal.get('name')}' cannot access task '{task_id}' outside its scope",
                    details={"task_id": task_id, "principal": principal.get("name")},
                )
        elif cmd in ("project.show", "project.leads", "project.wait", "workspace.list") or (
            cmd in ("run.show", "run.log") and not task_id
        ):
            if project_id and not can_principal_read_project(conn, agent_id, project_id):
                raise MeshError(
                    code=c.SCOPE_MISMATCH,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{principal.get('name')}' cannot access project '{project_id}' outside its scope",
                    details={"project_id": project_id, "principal": principal.get("name")},
                )
        elif cmd in ("event.list", "event.tail"):
            if project_id and not can_principal_read_project(conn, agent_id, project_id):
                raise MeshError(
                    code=c.SCOPE_MISMATCH,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{principal.get('name')}' cannot access events for project '{project_id}' outside its scope",
                    details={"project_id": project_id, "principal": principal.get("name")},
                )
            if task_id and not can_principal_read_task(conn, agent_id, task_id):
                raise MeshError(
                    code=c.SCOPE_MISMATCH,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{principal.get('name')}' cannot access events for task '{task_id}' outside its scope",
                    details={"task_id": task_id, "principal": principal.get("name")},
                )
        return

    # 10. Approval commands (fine-grained auth checks in handlers)
    if cmd in ("approval.request", "approval.approve", "approval.reject", "approval.cancel", "approval.check", "approval.consume"):
        return

    # 11. Deny unknown operations
    raise MeshError(
        code=c.CAPABILITY_MISSING,
        exit_code=c.EXIT_FORBIDDEN,
        message=f"Unknown or unauthorized command '{cmd}'",
        details={"cmd": cmd},
    )
