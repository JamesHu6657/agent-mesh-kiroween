"""Dependency command handlers per plan §6.2, §7.3, §13.3."""

import sqlite3
from typing import Any, Dict, List, Set

import mesh.constants as c
from mesh.core.authz import check_authz, is_project_lead
from mesh.core.events import record_event
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.errors import MeshError


def _dfs_cycle_check(conn: sqlite3.Connection, start_task_id: str, target_task_id: str) -> bool:
    """Check if adding start_task_id -> target_task_id creates a cycle.

    That is, check if target_task_id can already reach start_task_id.
    """
    visited: Set[str] = set()
    queue = [target_task_id]

    while queue:
        curr = queue.pop()
        if curr == start_task_id:
            return True
        if curr in visited:
            continue
        visited.add(curr)
        cur = conn.execute("SELECT depends_on_task_id FROM task_deps WHERE task_id = ?;", (curr,))
        for row in cur.fetchall():
            queue.append(row["depends_on_task_id"])

    return False


def handle_dep_add(ctx: HandlerContext) -> HandlerResult:
    """Add a prerequisite dependency: task depends on on_task."""
    task_id = ctx.args.get("task_id")
    on_task_id = ctx.args.get("on") or ctx.args.get("on_arg") or ctx.args.get("depends_on")

    if not task_id or not str(task_id).strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing task_id",
            details={"param": "task_id"},
        )
    if not on_task_id or not str(on_task_id).strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing prerequisite task id (--on)",
            details={"param": "on"},
        )

    task_id = str(task_id).strip()
    on_task_id = str(on_task_id).strip()

    if task_id == on_task_id:
        raise MeshError(
            code=c.DEP_CYCLE,
            exit_code=c.EXIT_USAGE,
            message=f"Task '{task_id}' cannot depend on itself",
            details={"task_id": task_id, "on": on_task_id},
        )

    # Fetch both tasks
    cur = ctx.conn.execute(
        "SELECT id, project_id, status FROM tasks WHERE id IN (?, ?);",
        (task_id, on_task_id),
    )
    rows = {r["id"]: r for r in cur.fetchall()}

    if task_id not in rows:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{task_id}' not found",
            details={"task_id": task_id},
        )
    if on_task_id not in rows:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{on_task_id}' not found",
            details={"task_id": on_task_id},
        )

    t1 = rows[task_id]
    t2 = rows[on_task_id]

    if t1["project_id"] != t2["project_id"]:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Tasks must belong to the same project",
            details={"task_id": task_id, "on": on_task_id},
        )

    project_id = t1["project_id"]
    check_authz(ctx.conn, "dep.add", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check for cycle (ACC: F5)
    if _dfs_cycle_check(ctx.conn, task_id, on_task_id):
        raise MeshError(
            code=c.DEP_CYCLE,
            exit_code=c.EXIT_USAGE,
            message=f"Adding dependency from '{task_id}' to '{on_task_id}' creates a cycle",
            details={"task_id": task_id, "on": on_task_id},
        )

    # Pre-public review P2: T4 checks deps only on the way into queued, so a new
    # unfinished prerequisite on an already-queued task would never block it.
    # Reject that instead of silently letting the task start.
    if t1["status"] == "queued" and t2["status"] not in ("accepted", "merged"):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=(
                f"Task '{task_id}' is already queued; it cannot gain unfinished "
                f"prerequisite '{on_task_id}' (status '{t2['status']}'). "
                "Reassign it back to assigned first, or wait for the prerequisite."
            ),
            details={"task_id": task_id, "on": on_task_id, "task_status": "queued",
                     "on_status": t2["status"]},
        )

    # Insert into task_deps if not already present
    ctx.conn.execute(
        "INSERT OR IGNORE INTO task_deps (task_id, depends_on_task_id) VALUES (?, ?);",
        (task_id, on_task_id),
    )

    record_event(
        conn=ctx.conn,
        type_="dep.added",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        payload={"task_id": task_id, "depends_on_task_id": on_task_id},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(
        data={"task_id": task_id, "depends_on": on_task_id},
        events=["dep.added"],
    )


def handle_dep_remove(ctx: HandlerContext) -> HandlerResult:
    """Remove a prerequisite dependency."""
    task_id = ctx.args.get("task_id")
    on_task_id = ctx.args.get("on") or ctx.args.get("on_arg") or ctx.args.get("depends_on")

    if not task_id or not on_task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Both task_id and --on are required",
        )

    task_id = str(task_id).strip()
    on_task_id = str(on_task_id).strip()

    cur = ctx.conn.execute("SELECT id, project_id FROM tasks WHERE id = ?;", (task_id,))
    trow = cur.fetchone()
    if not trow:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{task_id}' not found",
            details={"task_id": task_id},
        )

    project_id = trow["project_id"]
    check_authz(ctx.conn, "dep.remove", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    ctx.conn.execute(
        "DELETE FROM task_deps WHERE task_id = ? AND depends_on_task_id = ?;",
        (task_id, on_task_id),
    )

    record_event(
        conn=ctx.conn,
        type_="dep.removed",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        payload={"task_id": task_id, "depends_on_task_id": on_task_id},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(
        data={"task_id": task_id, "removed": on_task_id},
        events=["dep.removed"],
    )


def handle_dep_list(ctx: HandlerContext) -> HandlerResult:
    """List dependencies for a task."""
    task_id = ctx.args.get("task_id")
    if not task_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Missing task_id",
        )

    cur = ctx.conn.execute("SELECT id, project_id FROM tasks WHERE id = ?;", (task_id,))
    trow = cur.fetchone()
    if not trow:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{task_id}' not found",
            details={"task_id": task_id},
        )

    project_id = trow["project_id"]
    check_authz(ctx.conn, "dep.list", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

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

    return HandlerResult(
        data={
            "task_id": task_id,
            "depends_on": depends_on,
            "depended_on_by": depended_on_by,
        }
    )
