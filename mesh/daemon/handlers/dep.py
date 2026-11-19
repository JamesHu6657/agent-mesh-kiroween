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



def _require_open_project(conn, project_id):
    """Full review: dependencies of a completed/cancelled project are frozen (E18)."""
    row = conn.execute("SELECT status FROM projects WHERE id = ?;", (project_id,)).fetchone()
    if row is not None and row["status"] in ("completed", "cancelled"):
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {row['status']}",
            details={"project_id": project_id, "status": row["status"]},
        )

def _require_task_str(val: Any, param: str, msg: str) -> str:
    if not isinstance(val, str) or not val.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=msg,
            details={"param": param},
        )
    return val.strip()


def handle_dep_add(ctx: HandlerContext) -> HandlerResult:
    """Add a prerequisite dependency: task depends on on_task."""
    task_id = _require_task_str(ctx.args.get("task_id"), "task_id", "Missing task_id")
    raw_on = (
        ctx.args.get("on")
        if ctx.args.get("on") is not None
        else (ctx.args.get("on_arg") if ctx.args.get("on_arg") is not None else ctx.args.get("depends_on"))
    )
    on_task_id = _require_task_str(raw_on, "on", "Missing prerequisite task id (--on)")

    t1 = ctx.conn.execute(
        "SELECT id, project_id, status FROM tasks WHERE id = ?;",
        (task_id,),
    ).fetchone()
    if t1 is None:
        check_authz(ctx.conn, "dep.add", ctx.principal, ctx.actor_kind, ctx.via, resource_resolved=False)
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{task_id}' not found",
            details={"task_id": task_id},
        )

    project_id = t1["project_id"]
    check_authz(ctx.conn, "dep.add", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    if task_id == on_task_id:
        raise MeshError(
            code=c.DEP_CYCLE,
            exit_code=c.EXIT_USAGE,
            message=f"Task '{task_id}' cannot depend on itself",
            details={"task_id": task_id, "on": on_task_id},
        )

    t2 = ctx.conn.execute(
        "SELECT id, project_id, status FROM tasks WHERE id = ?;",
        (on_task_id,),
    ).fetchone()
    if t2 is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{on_task_id}' not found",
            details={"task_id": on_task_id},
        )

    if t1["project_id"] != t2["project_id"]:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Tasks must belong to the same project",
            details={"task_id": task_id, "on": on_task_id},
        )

    # Check for cycle (ACC: F5)
    if _dfs_cycle_check(ctx.conn, task_id, on_task_id):
        raise MeshError(
            code=c.DEP_CYCLE,
            exit_code=c.EXIT_USAGE,
            message=f"Adding dependency from '{task_id}' to '{on_task_id}' creates a cycle",
            details={"task_id": task_id, "on": on_task_id},
        )

    # Pre-public review P2 + r3 P2-11: T4 checks deps only on the way into queued, so a new
    # unfinished prerequisite on an already-queued or post-queue task would never block it.
    if (
        t1["status"] in ("queued", "running", "submitted", "accepted", "merging", "merged", "cancelling", "cancelled")
        and t2["status"] not in ("accepted", "merged")
    ):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=(
                f"Task '{task_id}' is '{t1['status']}'; it cannot gain unfinished "
                f"prerequisite '{on_task_id}' (status '{t2['status']}')."
            ),
            details={"task_id": task_id, "on": on_task_id, "task_status": t1["status"],
                     "on_status": t2["status"]},
        )

    _require_open_project(ctx.conn, t1["project_id"])
    # Insert into task_deps if not already present
    cur = ctx.conn.execute(
        "INSERT OR IGNORE INTO task_deps (task_id, depends_on_task_id) VALUES (?, ?);",
        (task_id, on_task_id),
    )

    events: List[str] = []
    if cur.rowcount > 0:
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
        events.append("dep.added")

    return HandlerResult(
        data={"task_id": task_id, "depends_on": on_task_id, "added": cur.rowcount > 0},
        events=events,
    )


def handle_dep_remove(ctx: HandlerContext) -> HandlerResult:
    """Remove a prerequisite dependency."""
    task_id = _require_task_str(ctx.args.get("task_id"), "task_id", "Missing task_id")
    raw_on = (
        ctx.args.get("on")
        if ctx.args.get("on") is not None
        else (ctx.args.get("on_arg") if ctx.args.get("on_arg") is not None else ctx.args.get("depends_on"))
    )
    on_task_id = _require_task_str(raw_on, "on", "Missing prerequisite task id (--on)")

    t1 = ctx.conn.execute(
        "SELECT id, project_id FROM tasks WHERE id = ?;",
        (task_id,),
    ).fetchone()
    if t1 is None:
        check_authz(ctx.conn, "dep.remove", ctx.principal, ctx.actor_kind, ctx.via, resource_resolved=False)
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{task_id}' not found",
            details={"task_id": task_id},
        )

    project_id = t1["project_id"]
    check_authz(ctx.conn, "dep.remove", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    t2 = ctx.conn.execute(
        "SELECT id, project_id FROM tasks WHERE id = ?;",
        (on_task_id,),
    ).fetchone()
    if t2 is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{on_task_id}' not found",
            details={"task_id": on_task_id},
        )

    _require_open_project(ctx.conn, project_id)

    del_cur = ctx.conn.execute(
        "DELETE FROM task_deps WHERE task_id = ? AND depends_on_task_id = ?;",
        (task_id, on_task_id),
    )

    events: List[str] = []
    if del_cur.rowcount > 0:
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
        events.append("dep.removed")

    return HandlerResult(
        data={"task_id": task_id, "removed": on_task_id, "removed_count": del_cur.rowcount},
        events=events,
    )


def handle_dep_list(ctx: HandlerContext) -> HandlerResult:
    """List dependencies for a task."""
    task_id = _require_task_str(ctx.args.get("task_id"), "task_id", "Missing task_id")

    cur = ctx.conn.execute("SELECT id, project_id FROM tasks WHERE id = ?;", (task_id,))
    trow = cur.fetchone()
    if not trow:
        check_authz(ctx.conn, "dep.list", ctx.principal, ctx.actor_kind, ctx.via, resource_resolved=False)
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
