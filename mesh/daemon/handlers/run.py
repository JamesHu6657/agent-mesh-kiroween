"""Read verbs for runs, task logs and workspaces (plan §13.3, §7.3, §10.1, §15.2).

``run list/show/log``, ``task log``, ``workspace list/show``. Every response is
redacted (§15.2) and authorized as a read command (§7.3): the user and relayers
read everything; an agent reads its own resources; an executor run token reads
only its own task.
"""

import collections
import json
import os
import sqlite3
from typing import Any, Deque, Dict, List, Optional

import mesh.constants as c
from mesh.core.authz import can_principal_read_project, can_principal_read_task, check_authz, get_agent_capabilities, lead_run_token_scope
from mesh.core.clock import now_iso_utc
from mesh.core.events import record_event
from mesh.core.redact import redact, redact_value
from mesh.core.workspaces import compute_lead_workspace_path, get_workspaces_root
from mesh.daemon.handlers.base import Effect, HandlerContext, HandlerResult
from mesh.errors import MeshError

RUN_STATUSES = ("starting", "running", "exited", "killed", "lost", "spawn_failed")
LOG_STREAMS = ("stdout", "stderr")
# Without --tail a log read returns at most this many trailing lines per stream,
# so a 50 MiB log never turns into one huge socket reply (reading, impl notes).
DEFAULT_LOG_LINES = 1000
MAX_LOG_LINES = 100000


def _not_found(kind: str, ident: str) -> MeshError:
    return MeshError(
        code=c.NOT_FOUND,
        exit_code=c.EXIT_NOT_FOUND,
        message=f"{kind} '{ident}' not found",
        details={kind.lower(): ident},
    )


def _usage(message: str, param: str) -> MeshError:
    return MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message=message, details={"param": param})


def _get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute(
        """SELECT r.*, p.name AS agent_name FROM runs r
           LEFT JOIN principals p ON r.agent_id = p.id WHERE r.id = ?;""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise _not_found("Run", run_id)
    return row


def _get_task_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
    if row is None:
        raise _not_found("Task", task_id)
    return row


def _loads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _format_run(row: sqlite3.Row) -> Dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "kind": row["kind"],
        "task_id": row["task_id"],
        "project_id": row["project_id"],
        "agent_id": row["agent_id"],
        "agent": row["agent_name"] if "agent_name" in keys else None,
        "attempt": row["attempt"],
        "fence": row["fence"],
        "adapter": row["adapter"],
        "model": row["model"],
        "effort": row["effort"],
        "profile": row["profile"],
        "status": row["status"],
        "wrapper_pid": row["wrapper_pid"],
        "pgid": row["pgid"],
        "run_dir": row["run_dir"],
        "argv_redacted": _loads(row["argv_redacted_json"], []),
        "started_at": row["started_at"],
        "deadline_at": row["deadline_at"],
        "ended_at": row["ended_at"],
        "exit_code": row["exit_code"],
        "signal": row["signal"],
        "end_reason": row["end_reason"],
        "session_id": row["session_id"],
        "result": _loads(row["result_json"], None),
    }


def _authz_run(ctx: HandlerContext, cmd: str, run: sqlite3.Row) -> None:
    check_authz(ctx.conn, cmd, ctx.principal, ctx.actor_kind, ctx.via,
                project_id=run["project_id"], task_id=run["task_id"])


def _is_unrestricted_reader(ctx: HandlerContext) -> bool:
    """User (direct or relayed reads go through check_authz) or a relayer reads everything."""
    if ctx.actor_kind == "user":
        return True
    if ctx.via == "run_token":
        return False
    caps = ctx.principal.get("capabilities") or []
    if not caps and ctx.principal.get("id"):
        caps = get_agent_capabilities(ctx.conn, ctx.principal["id"])
    return "relay" in caps


def _read_log_tail(path: str, tail: int) -> Dict[str, Any]:
    """Last ``tail`` lines of a run log, redacted again on the way out (§15.2)."""
    if not os.path.isfile(path):
        return {"available": False, "lines": [], "total_lines": 0, "truncated": False}
    # deque(maxlen) keeps the tail in O(1) per line (review m1c-run2 finding 4).
    window: Deque[str] = collections.deque(maxlen=max(tail, 0))
    total = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            total += 1
            window.append(line.rstrip("\n"))
    lines = list(window)
    return {
        "available": True,
        "lines": [redact(line) for line in lines],
        "total_lines": total,
        "truncated": total > len(lines),
    }


def _tail_arg(ctx: HandlerContext) -> int:
    tail = ctx.args.get("tail")
    if tail is None:
        return DEFAULT_LOG_LINES
    try:
        tail = int(tail)
    except (TypeError, ValueError):
        raise _usage("--tail must be a positive integer", "tail")
    if tail < 1 or tail > MAX_LOG_LINES:
        raise _usage(f"--tail must be between 1 and {MAX_LOG_LINES}", "tail")
    return tail


def _streams_arg(ctx: HandlerContext) -> List[str]:
    stream = ctx.args.get("stream")
    if stream is None:
        return list(LOG_STREAMS)
    if stream not in LOG_STREAMS:
        raise _usage("--stream must be stdout or stderr", "stream")
    return [stream]


def _run_logs(run: sqlite3.Row, streams: List[str], tail: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in streams:
        out[name] = _read_log_tail(os.path.join(run["run_dir"], f"{name}.log"), tail)
    return {
        "run_id": run["id"],
        "task_id": run["task_id"],
        "status": run["status"],
        "streams": out,
    }


# --- run ----------------------------------------------------------------------
def handle_run_list(ctx: HandlerContext) -> HandlerResult:
    task_id = ctx.args.get("task")
    status = ctx.args.get("status")
    if status is not None and status not in RUN_STATUSES:
        raise _usage(f"--status must be one of: {', '.join(RUN_STATUSES)}", "status")
    if task_id:
        task = _get_task_row(ctx.conn, task_id)
        check_authz(ctx.conn, "run.list", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=task["project_id"], task_id=task_id)
    else:
        check_authz(ctx.conn, "run.list", ctx.principal, ctx.actor_kind, ctx.via)

    query = """SELECT r.*, p.name AS agent_name FROM runs r
               LEFT JOIN principals p ON r.agent_id = p.id WHERE 1=1"""
    params: List[Any] = []
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        # Review m2-r9 finding 1(a): a lead run token lists only its own project's runs.
        query += " AND r.project_id = ?"
        params.append(scope_project)
    if task_id:
        query += " AND r.task_id = ?"
        params.append(task_id)
    if status:
        query += " AND r.status = ?"
        params.append(status)
    query += " ORDER BY COALESCE(r.started_at, '') ASC, r.rowid ASC;"
    rows = ctx.conn.execute(query, params).fetchall()

    if scope_project is None and not _is_unrestricted_reader(ctx):
        agent_id = ctx.principal.get("id")
        rows = [
            r for r in rows
            if (r["task_id"] and can_principal_read_task(ctx.conn, agent_id, r["task_id"]))
            or (not r["task_id"] and can_principal_read_project(ctx.conn, agent_id, r["project_id"]))
        ]
    return HandlerResult(data=redact_value({"runs": [_format_run(r) for r in rows]}))


def handle_run_show(ctx: HandlerContext) -> HandlerResult:
    run_id = ctx.args.get("run_id")
    if not run_id:
        raise _usage("Missing required parameter run_id", "run_id")
    run = _get_run(ctx.conn, run_id)
    _authz_run(ctx, "run.show", run)
    return HandlerResult(data=redact_value(_format_run(run)))


def handle_run_log(ctx: HandlerContext) -> HandlerResult:
    run_id = ctx.args.get("run_id")
    if not run_id:
        raise _usage("Missing required parameter run_id", "run_id")
    streams = _streams_arg(ctx)
    tail = _tail_arg(ctx)
    run = _get_run(ctx.conn, run_id)
    _authz_run(ctx, "run.log", run)
    return HandlerResult(data=_run_logs(run, streams, tail))


# --- task log -----------------------------------------------------------------
def handle_task_log(ctx: HandlerContext) -> HandlerResult:
    task_id = ctx.args.get("task_id")
    if not task_id:
        raise _usage("Missing required parameter task_id", "task_id")
    streams = _streams_arg(ctx)
    tail = _tail_arg(ctx)
    task = _get_task_row(ctx.conn, task_id)
    check_authz(ctx.conn, "task.log", ctx.principal, ctx.actor_kind, ctx.via,
                project_id=task["project_id"], task_id=task_id)
    run_id = ctx.args.get("run")
    if run_id:
        run = _get_run(ctx.conn, run_id)
        if run["task_id"] != task_id:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Run '{run_id}' does not belong to task '{task_id}'",
                details={"run_id": run_id, "task_id": task_id},
            )
    else:
        run = ctx.conn.execute(
            """SELECT r.*, p.name AS agent_name FROM runs r LEFT JOIN principals p ON r.agent_id = p.id
               WHERE r.task_id = ? ORDER BY r.fence DESC, r.rowid DESC LIMIT 1;""",
            (task_id,),
        ).fetchone()
        if run is None:
            return HandlerResult(data={"task_id": task_id, "run_id": None, "status": None, "streams": {}})
    return HandlerResult(data=_run_logs(run, streams, tail))


# --- workspace ----------------------------------------------------------------
def _task_workspace_entry(row: sqlite3.Row) -> Dict[str, Any]:
    path = row["workspace_path"]
    return {
        "kind": "task",
        "task_id": row["id"],
        "project_id": row["project_id"],
        "status": row["status"],
        "path": path,
        "exists": bool(path) and os.path.isdir(path),
        "branch": row["branch"],
        "base_commit": row["base_commit"],
    }


def _lead_workspace_entries(ctx: HandlerContext, project_id: str, root: str) -> List[Dict[str, Any]]:
    rows = ctx.conn.execute(
        """SELECT la.agent_id, p.name AS agent_name, MIN(la.started_at) AS first_started,
                  MAX(CASE WHEN la.ended_at IS NULL THEN 1 ELSE 0 END) AS is_current
           FROM lead_assignments la LEFT JOIN principals p ON la.agent_id = p.id
           WHERE la.project_id = ? GROUP BY la.agent_id ORDER BY first_started ASC;""",
        (project_id,),
    ).fetchall()
    out = []
    for r in rows:
        path = compute_lead_workspace_path(root, project_id, r["agent_id"])
        out.append({
            "kind": "lead",
            "project_id": project_id,
            "agent_id": r["agent_id"],
            "agent": r["agent_name"],
            "current": bool(r["is_current"]),
            "path": path,
            "exists": os.path.isdir(path),
        })
    return out


def handle_workspace_list(ctx: HandlerContext) -> HandlerResult:
    project_id = ctx.args.get("project")
    if project_id:
        if ctx.conn.execute("SELECT 1 FROM projects WHERE id = ?;", (project_id,)).fetchone() is None:
            raise _not_found("Project", project_id)
    check_authz(ctx.conn, "workspace.list", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        # Review m2-r9c N1: a lead run token lists only its own project's workspaces.
        project_id = scope_project
    if project_id:
        project_ids = [project_id]
    else:
        project_ids = [r["id"] for r in ctx.conn.execute("SELECT id FROM projects ORDER BY created_at, id;")]
    unrestricted = _is_unrestricted_reader(ctx)
    agent_id = ctx.principal.get("id")
    root = get_workspaces_root(ctx.conn, ctx.home)

    entries: List[Dict[str, Any]] = []
    for pid in project_ids:
        project_readable = unrestricted or can_principal_read_project(ctx.conn, agent_id, pid)
        if project_readable:
            entries.extend(_lead_workspace_entries(ctx, pid, root))
        tasks = ctx.conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND workspace_path IS NOT NULL ORDER BY created_at, id;",
            (pid,),
        ).fetchall()
        for t in tasks:
            if project_readable or can_principal_read_task(ctx.conn, agent_id, t["id"]):
                entries.append(_task_workspace_entry(t))
    return HandlerResult(data=redact_value({"root": root, "workspaces": entries}))


def handle_workspace_show(ctx: HandlerContext) -> HandlerResult:
    task_id = ctx.args.get("task_id")
    if not task_id:
        raise _usage("Missing required parameter task_id", "task_id")
    task = _get_task_row(ctx.conn, task_id)
    check_authz(ctx.conn, "workspace.show", ctx.principal, ctx.actor_kind, ctx.via,
                project_id=task["project_id"], task_id=task_id)
    entry = _task_workspace_entry(task)
    files: List[str] = []
    if entry["exists"]:
        base = entry["path"]
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d != ".git")
            for name in sorted(filenames):
                files.append(os.path.relpath(os.path.join(dirpath, name), base))
                if len(files) >= 1000:
                    break
            if len(files) >= 1000:
                break
    entry["files"] = files
    entry["files_truncated"] = len(files) >= 1000
    return HandlerResult(data=redact_value(entry))


# --- workspace prune / delete (§10.7) -----------------------------------------
def _prune_candidates(ctx: HandlerContext, project_id: Optional[str]) -> List[sqlite3.Row]:
    """Code tasks that `workspace prune` may remove (§10.7).

    Only `merged` tasks, or `cancelled` tasks with no commits beyond `base_commit`.
    A plain (non-repo) task has no worktree and is never a candidate.
    """
    sql = ("SELECT * FROM tasks WHERE kind = 'code' AND workspace_path IS NOT NULL "
           "AND branch IS NOT NULL AND status IN ('merged', 'cancelled')")
    params: List[Any] = []
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY created_at, id;"
    return list(ctx.conn.execute(sql, tuple(params)).fetchall())


def _commits_beyond_base(workspace: str, base_commit: Optional[str]) -> Optional[int]:
    """Commit count of HEAD beyond `base_commit`, or None when git cannot tell."""
    from mesh.core.gitops import run_git

    if not base_commit or not os.path.isdir(workspace):
        return None
    try:
        cp = run_git(["log", "--format=%H", f"{base_commit}..HEAD"], cwd=workspace)
    except Exception:
        return None
    if cp.returncode != 0:
        return None
    return len([ln for ln in (cp.stdout or "").splitlines() if ln.strip()])


def _prune_skip_reason(ctx: HandlerContext, task: sqlite3.Row) -> Optional[str]:
    """Why a candidate is skipped, or None when it may be pruned."""
    if ctx.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task['id']}",)).fetchone():
        return "lease_held"
    # Carbon P2-7: a merge running on the same repository (this project or another one
    # sharing the repo path) owns the shared .git; prune must not race its git writes.
    from mesh.daemon.merge import merge_in_progress_for_repo

    project = ctx.conn.execute("SELECT repo_path FROM projects WHERE id = ?;", (task["project_id"],)).fetchone()
    if project is not None and merge_in_progress_for_repo(ctx.conn, project["repo_path"]) is not None:
        return "merge_in_progress"
    if task["status"] == "cancelled":
        count = _commits_beyond_base(task["workspace_path"], task["base_commit"])
        if count is None:
            return "commits_unknown"
        if count > 0:
            return "unmerged_commits"
    return None


def _already_pruned(task: sqlite3.Row, integ_wt: Optional[str]) -> bool:
    """True when both the worktree directory and the branch are already gone (§10.7).

    A second `workspace prune` must be a no-op: an already-pruned task is done, not skipped
    with a failure reason (review m2-r8 MINOR-3).
    """
    from mesh.core.gitops import run_git

    if task["workspace_path"] and os.path.isdir(task["workspace_path"]):
        return False
    branch = task["branch"]
    if not branch:
        return True
    if not integ_wt or not os.path.isdir(integ_wt):
        return False
    try:
        cp = run_git(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=integ_wt)
    except Exception:
        return False
    return cp.returncode != 0


def _remove_worktree_and_branch(ctx: HandlerContext, task: sqlite3.Row, integ_wt: Optional[str]) -> Optional[str]:
    """`worktree remove` (no force) then `branch -d` in the integration worktree.

    Returns None on success or a skip reason. `branch -d` refuses an unmerged branch,
    which is exactly the §10.7 guarantee: Mesh never deletes unmerged work.
    """
    from mesh.core.gitops import run_git

    path = task["workspace_path"]
    branch = task["branch"]
    if os.path.isdir(path):
        try:
            cp = run_git(["worktree", "remove", path], cwd=integ_wt or path)
        except Exception:
            return "worktree_remove_failed"
        if cp.returncode != 0:
            return "worktree_remove_failed"
    if branch and integ_wt and os.path.isdir(integ_wt):
        try:
            cp = run_git(["branch", "-d", branch], cwd=integ_wt)
        except Exception:
            return "branch_delete_failed"
        if cp.returncode != 0:
            return "branch_delete_failed"
    return None


def handle_workspace_prune(ctx: HandlerContext) -> HandlerResult:
    """`workspace prune [--project] [--dry-run]` (§10.7): lead or user.

    Removes the worktree and branch of `merged` tasks and of `cancelled` tasks with no
    commits beyond `base_commit`. Plain directories are never pruned, nothing is forced,
    and a failure is reported as `skipped` with a reason instead of raising.
    """
    project_id = ctx.args.get("project")
    if project_id:
        if ctx.conn.execute("SELECT 1 FROM projects WHERE id = ?;", (project_id,)).fetchone() is None:
            raise _not_found("Project", project_id)
    check_authz(ctx.conn, "workspace.prune", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    root = get_workspaces_root(ctx.conn, ctx.home)
    removed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for task in _prune_candidates(ctx, project_id):
        entry = {"task_id": task["id"], "project_id": task["project_id"], "status": task["status"],
                 "path": task["workspace_path"], "branch": task["branch"]}
        integ_wt = os.path.join(root, task["project_id"], "integration")
        if _already_pruned(task, integ_wt):
            continue  # done by an earlier prune: neither removed nor skipped
        reason = _prune_skip_reason(ctx, task)
        if reason is not None:
            skipped.append(dict(entry, reason=reason))
            continue
        if ctx.dry_run:
            removed.append(entry)
            continue
        reason = _remove_worktree_and_branch(ctx, task, integ_wt)
        if reason is not None:
            skipped.append(dict(entry, reason=reason))
            continue
        removed.append(entry)
        record_event(
            conn=ctx.conn,
            type_="workspace.pruned",
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            relayed_by=ctx.relayed_by,
            project_id=task["project_id"],
            task_id=task["id"],
            payload={"task_id": task["id"], "path": task["workspace_path"], "branch": task["branch"]},
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
    if ctx.dry_run:
        # A dry run rolls back and reports through `data.would` (server.py): the plan is
        # carried as one `transitions` entry per worktree that would be removed.
        return HandlerResult(
            data={"root": root, "dry_run": True, "removed": removed, "skipped": skipped},
            transitions=[{"task_id": e["task_id"], "project_id": e["project_id"], "path": e["path"],
                          "branch": e["branch"], "action": "prune"} for e in removed],
        )
    return HandlerResult(data=redact_value({"root": root, "dry_run": False,
                                            "removed": removed, "skipped": skipped}))


def handle_workspace_delete(ctx: HandlerContext) -> HandlerResult:
    """`workspace delete <tsk>` (§10.7): gated by `delete` for agents; the user acts directly.

    Removes the task directory (after a realpath containment check under
    `workspaces.root`), runs `worktree prune`, and **keeps the branch** (Q7): deleting
    unmerged work is a user decision, and the branch is what holds it.
    """
    import shutil

    from mesh.core.gitops import run_git

    task_id = ctx.args.get("task_id")
    if not task_id:
        raise _usage("Missing required parameter task_id", "task_id")
    task = _get_task_row(ctx.conn, task_id)
    project_id = task["project_id"]
    check_authz(ctx.conn, "workspace.delete", ctx.principal, ctx.actor_kind, ctx.via,
                project_id=project_id, task_id=task_id)

    # Approval gate (§7.4): category `delete`, default `direct_or_relayed`.
    from mesh.core.approvals import check_and_consume_approval, create_or_get_pending_approval, get_approval_policy

    policy = get_approval_policy(ctx.conn, "delete")
    if ctx.via == "relayed" and policy == "direct_only":
        raise MeshError(
            code=c.CHANNEL_NOT_ALLOWED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Approval policy for category 'delete' is 'direct_only'; relayed execution not allowed",
            details={"category": "delete", "policy": policy},
        )
    if ctx.actor_kind != "user":
        params = {"task_id": task_id}
        if not ctx.approval:
            apr_info = create_or_get_pending_approval(
                conn=ctx.conn,
                category="delete",
                op="workspace.delete",
                requested_by=ctx.principal["id"],
                params=params,
                summary=f"Delete workspace of task {task_id}",
                project_id=project_id,
                target_id=task_id,
                dry_run=ctx.dry_run,
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )
            raise MeshError(
                code=c.APPROVAL_REQUIRED,
                exit_code=c.EXIT_APPROVAL_REQUIRED,
                message="Operation 'workspace.delete' requires user approval (category: delete)",
                details=apr_info,
            )
        check_and_consume_approval(
            conn=ctx.conn,
            approval_id=ctx.approval,
            op="workspace.delete",
            category="delete",
            caller_id=ctx.principal["id"],
            params=params,
            project_id=project_id,
            target_id=task_id,
            request_id=ctx.request_id,
            actor_kind=ctx.actor_kind, via=ctx.via, k_user=ctx.k_user,
        )

    if ctx.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)).fetchone():
        raise MeshError(
            code=c.RUN_STILL_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot delete the workspace of task '{task_id}': a run still holds its lease",
            details={"task_id": task_id},
        )

    root = os.path.realpath(get_workspaces_root(ctx.conn, ctx.home))
    path = task["workspace_path"]
    deleted = False
    if path:
        real = os.path.realpath(path)
        if real == root or not real.startswith(root + os.sep):
            # A path that resolves outside `workspaces.root` (a symlink swapped in after the
            # task was created, say) is refused: nothing is deleted and no event is recorded
            # (review m2-r8 MINOR-1).
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Workspace path of task '{task_id}' is outside the workspaces root",
                details={"task_id": task_id, "path": path, "reason": "outside_workspaces_root"},
            )
        if os.path.isdir(real):
            if ctx.dry_run:
                return HandlerResult(data={"task_id": task_id, "path": real, "deleted": False,
                                           "dry_run": True, "branch": task["branch"]})
            shutil.rmtree(real)
            deleted = True
            repo = ctx.conn.execute("SELECT repo_path FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if repo is not None and repo["repo_path"] and os.path.isdir(repo["repo_path"]):
                try:
                    run_git(["worktree", "prune"], cwd=repo["repo_path"])
                except Exception:
                    pass  # best effort: the directory is already gone

    record_event(
        conn=ctx.conn,
        type_="workspace.deleted",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        payload={"task_id": task_id, "path": path, "branch": task["branch"], "branch_kept": True},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    return HandlerResult(data=redact_value({"task_id": task_id, "path": path, "deleted": deleted,
                                            "branch": task["branch"], "branch_kept": True}))


def handle_run_release(ctx: HandlerContext) -> HandlerResult:
    """`mesh run release <run-id> --force-seen-unusable` (r4c, N18 operator path).

    Only for a task run held because its seen state (``seen.json``) is unusable: the
    run closes as ``lost``, its lease is released and its task takes the usual
    lost-run transition. No process is signalled. The ordinary rules stay: nothing
    is ever killed by pgid alone and a hold is never released on a timeout; this is
    the explicit operator decision. User only (never an agent or a run token).
    """
    if not (ctx.actor_kind == "user" and ctx.via == "direct"):
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Command 'run.release' requires direct user authentication",
            details={"cmd": "run.release", "required": "user_direct"},
        )
    run_id = ctx.args.get("run_id")
    if not run_id:
        raise _usage("Missing required parameter run_id", "run_id")
    run = _get_run(ctx.conn, run_id)
    force_seen = bool(ctx.args.get("force_seen_unusable"))
    force_merge = bool(ctx.args.get("force_merge_held"))
    if force_seen and force_merge:
        raise _usage("run release takes exactly one of --force-seen-unusable and --force-merge-held",
                     "force_merge_held")
    if force_merge:
        return _release_merge_held(ctx, run)
    if not force_seen:
        raise _usage(
            "run release only lifts a hold caused by unusable seen state and requires "
            "--force-seen-unusable (usage: mesh run release <run-id> --force-seen-unusable); "
            "a merge_test run held by the merge close gate needs --force-merge-held instead",
            "force_seen_unusable",
        )
    from mesh.daemon import supervisor

    if not supervisor.seen_held(run):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Run '{run_id}' is not held because of unusable seen state",
            details={"run_id": run_id, "status": run["status"], "reason": "not_seen_held"},
        )
    reason = supervisor._SEEN_REASON.get(run_id) or "unreadable"
    actor = ctx.principal.get("id")
    payload = {"run_id": run_id, "actor": actor, "reason": reason}
    if ctx.dry_run:
        return HandlerResult(data={"run_id": run_id, "released": False, "dry_run": True, "reason": reason})
    task = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?;", (run["task_id"],)).fetchone()
    supervisor._close_run(ctx.conn, run, "lost", "lost_on_restart", None,
                          {"reason": "seen_release_forced", "seen_reason": reason, "actor": actor},
                          gate=False)  # operator override of the close-safety gate (round 6)
    if task is not None:
        if task["status"] == "cancelling":
            supervisor._task_cancelled(ctx.conn, task, run, "cancelled")
        elif task["status"] == "running":
            supervisor._task_failed(ctx.conn, task, run, "lost_on_restart", {"seen_release_forced": True},
                                    supervisor.stderr_tail(run["run_dir"])
                                    if os.path.isdir(run["run_dir"] or "") else [])
    record_event(
        conn=ctx.conn,
        type_="run.seen_release_forced",
        actor_id=actor,
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=run["project_id"],
        task_id=run["task_id"],
        run_id=run_id,
        payload=payload,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    # r4d: the operator decision also clears the persisted hold (same transaction).
    ctx.conn.execute("DELETE FROM meta WHERE key = ?;", (supervisor.seen_hold_key(run_id),))
    # r4e: the in-memory seen/termination state is forgotten only after this request's
    # transaction committed (a post-commit effect). A rolled-back release leaves the
    # effective hold exactly as it was: memory, persisted row, run and lease.
    ctx.effects.append(Effect(kind="seen_release_forced",
                              params={"run_id": run_id, "reason": reason, "actor": actor}))
    return HandlerResult(data={"run_id": run_id, "released": True, "status": "lost", "reason": reason})


def _merge_wrapper_alive(run: Any) -> bool:
    """True while the merge_test run's wrapper process may still be alive (fail closed).

    The identity comes from the DB (``pgid``/``proc_starttime``, recorded by the merge
    close gate) or ``wrapper.json``. A pid whose starttime differs is a reused pid, not the
    wrapper. An unreadable ``/proc`` (EACCES/EIO) is unknown, never gone. A run with no
    recorded identity at all has no known wrapper to wait for.
    """
    from mesh.core.proc import get_proc_stat, is_tgid_alive
    from mesh.daemon import supervisor

    pid, starttime = supervisor.close_identity(run)
    if not pid or pid <= 1:
        return False
    try:
        info = get_proc_stat(int(pid))
        if info is None:
            return False
        if starttime and int(info["starttime"]) != int(starttime):
            return False
        return is_tgid_alive(int(pid), info)
    except OSError:
        return True


def _release_merge_held(ctx: HandlerContext, run: Any) -> HandlerResult:
    """`mesh run release <run-id> --force-merge-held --user` (Carbon P2-8).

    The operator path for a ``merge_test`` run that the merge close gate keeps open
    (:class:`mesh.daemon.merge.MergeRunHeld`): the run stays ``running``, the task stays
    ``merging`` and ``merge:<prj>`` stays held until the next boot, which the
    ``--force-seen-unusable`` path (task runs only) cannot lift. Refused unless the run is
    a ``merge_test`` run that is still open, and while its wrapper process is alive. No
    process is signalled: occupants of the run's group are the operator's call. The run
    closes ``lost``, ``merge:<prj>`` is released and ``run.merge_release_forced`` is
    recorded; after the commit the task is decided exactly as boot recovery would
    (reconciled ``merged`` when its merge commit is in integration, else ``merge_failed``
    ``interrupted``), so the project's merge channel is usable again without a restart.
    """
    from mesh.daemon.merge import merge_lease_key

    run_id = run["id"]
    if run["kind"] != "merge_test":
        raise MeshError(
            code=c.INVALID_TRANSITION, exit_code=c.EXIT_CONFLICT,
            message=f"Run '{run_id}' is not a merge_test run; --force-merge-held only releases merge test runs",
            details={"run_id": run_id, "kind": run["kind"], "reason": "not_merge_test"},
        )
    if run["status"] not in ("starting", "running"):
        raise MeshError(
            code=c.INVALID_TRANSITION, exit_code=c.EXIT_CONFLICT,
            message=f"Run '{run_id}' is already closed ({run['status']})",
            details={"run_id": run_id, "status": run["status"], "reason": "run_not_open"},
        )
    if _merge_wrapper_alive(run):
        raise MeshError(
            code=c.RUN_STILL_ACTIVE, exit_code=c.EXIT_CONFLICT,
            message=f"Run '{run_id}': its wrapper process is still alive; wait for it to exit or stop it first",
            details={"run_id": run_id, "status": run["status"], "reason": "wrapper_alive"},
        )
    actor = ctx.principal.get("id")
    key = merge_lease_key(run["project_id"])
    if ctx.dry_run:
        return HandlerResult(data={"run_id": run_id, "released": False, "dry_run": True,
                                   "lease": key, "task_id": run["task_id"]})
    now = now_iso_utc()
    result = {"reason": "merge_release_forced", "actor": actor}
    ctx.conn.execute(
        "UPDATE runs SET status = 'lost', end_reason = 'merge_release_forced', ended_at = ?, result_json = ? "
        "WHERE id = ? AND status IN ('starting', 'running');",
        (now, json.dumps(result), run_id),
    )
    released = ctx.conn.execute("DELETE FROM leases WHERE key = ? AND run_id = ?;", (key, run_id)).rowcount
    record_event(
        conn=ctx.conn,
        type_="run.merge_release_forced",
        actor_id=actor,
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=run["project_id"],
        task_id=run["task_id"],
        run_id=run_id,
        payload={"run_id": run_id, "task_id": run["task_id"], "actor": actor, "lease": key,
                 "lease_released": bool(released), "previous_status": run["status"]},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    if run["task_id"]:
        ctx.effects.append(Effect(kind="reconcile_merging_task", params={"task_id": run["task_id"]}))
    return HandlerResult(data={"run_id": run_id, "released": True, "status": "lost", "lease": key,
                               "lease_released": bool(released), "task_id": run["task_id"]})
