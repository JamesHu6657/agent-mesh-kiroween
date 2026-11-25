"""Read verbs for runs, task logs and workspaces (plan §13.3, §7.3, §10.1, §15.2).

``run list/show/log``, ``task log``, ``workspace list/show``. Every response is
redacted (§15.2) and authorized as a read command (§7.3): the user and relayers
read everything; an agent reads its own resources; an executor run token reads
only its own task.
"""

import asyncio
import collections
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import sys
from typing import Any, Deque, Dict, List, Mapping, Optional, Set, Tuple

import mesh.constants as c
from mesh.core.authz import can_principal_read_project, can_principal_read_task, check_authz, get_agent_capabilities, lead_run_token_scope
from mesh.core.clock import now_iso_utc
from mesh.core.events import record_event
from mesh.core.paths import make_owned_ancestors_writable, rmtree_allow_readonly
from mesh.core.redact import redact, redact_value
from mesh.core.repos import _worktree_for_branch
from mesh.core.settings import is_testing_mode
from mesh.core.workspaces import (
    WS_PRUNE_ALL_KEY,
    _task_lock,
    allowed_workspace_roots,
    compute_lead_workspace_path,
    ensure_run_dir,
    find_ws_reserved_idempotency,
    get_workspaces_root,
    has_project_any_ws_reservation,
    has_task_ws_reservation,
    load_ws_reservations,
    ws_delete_reservation_key,
    ws_prune_project_reservation_key,
)
from mesh.daemon.handlers.base import Effect, HandlerContext, HandlerResult
from mesh.errors import MeshError
from mesh.platform import fs as _plat_fs
from mesh.platform import paths as _plat_paths
from mesh.platform import perms as _plat_perms
from mesh.platform.proc import crash_self_now

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


_OVER_LINE_BYTES = 128 * 1024
_OVER_HEAD_BYTES = 2048
_BEGIN_BYTES_RE = re.compile(rb"-----BEGIN [A-Z ]{0,64}PRIVATE KEY-----")
_END_BYTES_RE = re.compile(rb"-----END [A-Z ]{0,64}PRIVATE KEY-----")


def _key_after_bytes(data: bytes) -> Tuple[bool, bool]:
    """Mirror ``redact._KeyScan`` on ``bytes`` for ``start in (False, True)``."""
    states = []
    for start in (False, True):
        in_key = start
        pos = 0
        while True:
            rx = _END_BYTES_RE if in_key else _BEGIN_BYTES_RE
            m = rx.search(data, pos)
            if m is None:
                break
            in_key = not in_key
            pos = m.end()
        states.append(in_key)
    return states[0], states[1]


def _clip_over_line(bline: bytes) -> str:
    """Represent a >128 KiB log line as a bounded >LINE_CAP line preserving prefix and PEM state."""
    b0, b1 = _key_after_bytes(bline)
    cut = _OVER_HEAD_BYTES
    h0, h1 = _key_after_bytes(bline[:cut])
    if h0 == h1 and b0 != b1:
        # Overlapping BEGIN/END pair straddled _OVER_HEAD_BYTES; shift cut (>=1024 bytes keeps 256-char prefix).
        for candidate in (cut + 100, cut - 100, 1024):
            c0, c1 = _key_after_bytes(bline[:candidate])
            if c0 != c1:
                cut, h0, h1 = candidate, c0, c1
                break
    head = bline[:cut].decode("utf-8", errors="replace")
    if (h0, h1) == (b0, b1):
        suffix = ""
    elif (b0, b1) == (True, True):
        suffix = " -----BEGIN PRIVATE KEY-----"
    elif (b0, b1) == (False, False):
        suffix = " -----END PRIVATE KEY-----"
    elif h0 != h1 and b0 != b1:
        suffix = " -----END PRIVATE KEY-----BEGIN PRIVATE KEY-----"
    else:
        suffix = " -----BEGIN PRIVATE KEY-----"
    return head + ("x" * (65 * 1024)) + suffix


def _read_log_tail(path: str, tail: int) -> Dict[str, Any]:
    """Last ``tail`` lines of a run log, redacted again on the way out (§15.2)."""
    if not os.path.isfile(path):
        return {"available": False, "lines": [], "total_lines": 0, "truncated": False}
    # deque(maxlen) keeps the tail in O(1) per line (review m1c-run2 finding 4).
    raw_window: Deque[bytes] = collections.deque(maxlen=max(tail, 0))
    total = 0
    with open(path, "rb") as bf:
        for bline in bf:
            total += 1
            if bline.endswith(b"\n"):
                bline = bline[:-1]
            if sys.platform == "win32" and bline.endswith(b"\r"):
                bline = bline[:-1]
            raw_window.append(bline)
    lines: List[str] = [
        _clip_over_line(bline) if len(bline) > _OVER_LINE_BYTES else bline.decode("utf-8", errors="replace")
        for bline in raw_window
    ]
    return {
        "available": True,
        # Full review r2 P2-A: the tail is redacted as one text with the run-log (JSONL)
        # rules, as runwrap wrote it; per-line default rules cut `[REDACTED]"}` lines and
        # lost the key-block state across lines.
        "lines": redact("\n".join(lines), jsonl=True).split("\n") if lines else [],
        "total_lines": total,
        "truncated": total > len(lines),
    }


def _tail_arg(ctx: HandlerContext) -> int:
    tail = ctx.args.get("tail")
    if tail is None:
        return DEFAULT_LOG_LINES
    if isinstance(tail, bool):
        raise _usage("--tail must be a positive integer", "tail")
    try:
        tail = int(tail)
    except (TypeError, ValueError, OverflowError):
        raise _usage("--tail must be a positive integer", "tail")
    if tail < 1 or tail > MAX_LOG_LINES:
        raise _usage(f"--tail must be between 1 and {MAX_LOG_LINES}", "tail")
    return tail


def _streams_arg(ctx: HandlerContext) -> List[str]:
    stream = ctx.args.get("stream")
    if stream is None:
        return list(LOG_STREAMS)
    if not isinstance(stream, str) or stream not in LOG_STREAMS:
        raise _usage("--stream must be stdout or stderr", "stream")
    return [stream]


def _run_logs(run: Mapping[str, Any], streams: List[str], tail: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in streams:
        out[name] = _read_log_tail(os.path.join(run["run_dir"], f"{name}.log"), tail)
    return {
        "run_id": run["id"],
        "task_id": run["task_id"],
        "status": run["status"],
        "streams": out,
    }


def _resolve_run_log_target(ctx: HandlerContext) -> Tuple[sqlite3.Row, List[str], int]:
    run_id = ctx.args.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise _usage("Missing required parameter run_id", "run_id")
    streams = _streams_arg(ctx)
    tail = _tail_arg(ctx)
    run = _get_run(ctx.conn, run_id)
    _authz_run(ctx, "run.log", run)
    return run, streams, tail


def _resolve_task_log_target(ctx: HandlerContext) -> Tuple[str, Optional[sqlite3.Row], List[str], int]:
    task_id = ctx.args.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise _usage("Missing required parameter task_id", "task_id")
    streams = _streams_arg(ctx)
    tail = _tail_arg(ctx)
    task = _get_task_row(ctx.conn, task_id)
    check_authz(ctx.conn, "task.log", ctx.principal, ctx.actor_kind, ctx.via,
                project_id=task["project_id"], task_id=task_id)
    run_id = ctx.args.get("run")
    if run_id is not None and not isinstance(run_id, str):
        raise _usage("Parameter 'run' must be a string", "run")
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
    return task_id, run, streams, tail


async def prepare_log_command(ctx: HandlerContext) -> None:
    """Validate, authorize, and offload run/task log & activity reading off the event loop."""
    if ctx.cmd == "run.log":
        run, streams, tail = _resolve_run_log_target(ctx)
        run_info = dict(run)
        data = await asyncio.to_thread(_run_logs, run_info, streams, tail)
        setattr(ctx, "_prepared_log", {
            "cmd": "run.log",
            "run_id": run_info["id"],
            "run_dir": run_info["run_dir"],
            "streams": streams,
            "tail": tail,
            "data": data,
        })
        return
    if ctx.cmd == "task.log":
        task_id, run, streams, tail = _resolve_task_log_target(ctx)
        if run is None:
            setattr(ctx, "_prepared_log", {
                "cmd": "task.log",
                "task_id": task_id,
                "run_id": None,
                "data": {"task_id": task_id, "run_id": None, "status": None, "streams": {}},
            })
            return
        run_info = dict(run)
        data = await asyncio.to_thread(_run_logs, run_info, streams, tail)
        setattr(ctx, "_prepared_log", {
            "cmd": "task.log",
            "task_id": task_id,
            "run_id": run_info["id"],
            "run_dir": run_info["run_dir"],
            "streams": streams,
            "tail": tail,
            "data": data,
        })
        return
    if ctx.cmd == "run.activity":
        task_id, run, limit = _resolve_run_activity_target(ctx)
        if run is None:
            setattr(ctx, "_prepared_activity", {
                "task_id": task_id,
                "run_id": None,
                "limit": limit,
                "data": {
                    "available": False,
                    "run_id": None,
                    "task_id": task_id,
                    "status": None,
                    "adapter": None,
                    "source": "empty",
                    "entries": [],
                    "counts": {},
                    "total_entries": 0,
                    "lines_scanned": 0,
                    "log_capped": False,
                    "truncated": False,
                },
            })
            return
        run_info = dict(run)
        data = await asyncio.to_thread(_extract_activity_for_run, run_info, limit)
        setattr(ctx, "_prepared_activity", {
            "task_id": task_id,
            "run_id": run_info["id"],
            "run_dir": run_info["run_dir"],
            "limit": limit,
            "data": data,
        })
        return


def _activity_limit_arg(ctx: HandlerContext) -> int:
    from mesh.adapters.activity import MAX_ACTIVITY_ENTRIES

    raw = ctx.args.get("limit")
    if raw is None:
        return MAX_ACTIVITY_ENTRIES
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw > MAX_ACTIVITY_ENTRIES:
        raise _usage(f"Parameter 'limit' must be an integer between 1 and {MAX_ACTIVITY_ENTRIES}", "limit")
    return raw


def _resolve_run_activity_target(ctx: HandlerContext) -> Tuple[Optional[str], Optional[sqlite3.Row], int]:
    run_id = ctx.args.get("run_id")
    task_id = ctx.args.get("task_id") or ctx.args.get("task")
    limit = _activity_limit_arg(ctx)
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise _usage("Parameter 'run_id' must be a non-empty string", "run_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        raise _usage("Parameter 'task_id' must be a non-empty string", "task_id")
    if not run_id and not task_id:
        raise _usage("Either 'run_id' or 'task_id' is required", "run_id")

    if run_id:
        run = _get_run(ctx.conn, run_id.strip())
        _authz_run(ctx, "run.activity", run)
        if task_id and run["task_id"] != task_id.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Run '{run_id}' does not belong to task '{task_id}'",
                details={"run_id": run_id, "task_id": task_id},
            )
        return run["task_id"], run, limit

    assert task_id is not None
    tid = task_id.strip()
    task = _get_task_row(ctx.conn, tid)
    check_authz(
        ctx.conn,
        "run.activity",
        ctx.principal,
        ctx.actor_kind,
        ctx.via,
        project_id=task["project_id"],
        task_id=tid,
    )
    run = ctx.conn.execute(
        """SELECT r.*, p.name AS agent_name FROM runs r LEFT JOIN principals p ON r.agent_id = p.id
           WHERE r.task_id = ? ORDER BY r.fence DESC, r.rowid DESC LIMIT 1;""",
        (tid,),
    ).fetchone()
    return tid, run, limit


def _extract_activity_for_run(run: Mapping[str, Any], limit: int) -> Dict[str, Any]:
    from mesh.adapters.activity import extract_run_activity

    res = extract_run_activity(run["run_dir"], adapter=run["adapter"], limit=limit)
    res["run_id"] = run["id"]
    res["task_id"] = run["task_id"]
    res["project_id"] = run["project_id"]
    res["status"] = run["status"]
    return redact_value(res)


def _diff_max_bytes_arg(ctx: HandlerContext) -> int:
    from mesh.core.diffs import DEFAULT_MAX_DIFF_BYTES, MAX_DIFF_BYTES

    raw = ctx.args.get("max_bytes")
    if raw is None:
        return DEFAULT_MAX_DIFF_BYTES
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1024 or raw > MAX_DIFF_BYTES:
        raise _usage(f"Parameter 'max_bytes' must be an integer between 1024 and {MAX_DIFF_BYTES}", "max_bytes")
    return raw


def _bool_arg(ctx: HandlerContext, name: str, default: bool) -> bool:
    raw = ctx.args.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    raise _usage(f"Parameter '{name}' must be a boolean", name)


def _resolve_task_diff_plan(ctx: HandlerContext) -> Dict[str, Any]:
    task_id = ctx.args.get("task_id") or ctx.args.get("task")
    if not isinstance(task_id, str) or not task_id.strip():
        raise _usage("Missing required parameter task_id", "task_id")
    tid = task_id.strip()
    max_bytes = _diff_max_bytes_arg(ctx)
    include_untracked = _bool_arg(ctx, "include_untracked", True)
    task = _get_task_row(ctx.conn, tid)
    check_authz(
        ctx.conn,
        "task.diff",
        ctx.principal,
        ctx.actor_kind,
        ctx.via,
        project_id=task["project_id"],
        task_id=tid,
    )
    prow = ctx.conn.execute(
        "SELECT id, repo_path, integration_branch FROM projects WHERE id = ?;",
        (task["project_id"],),
    ).fetchone()
    repo_path = prow["repo_path"] if prow is not None else None
    integ_branch = prow["integration_branch"] if prow is not None else None
    allowed_roots = _allowed_workspace_roots(ctx.conn, ctx.home)
    match = _match_task_workspace_with_roots(
        allowed_roots, task["project_id"], tid, task["workspace_path"]
    )
    ws_real = match[1] if match is not None else None
    matched_root = match[2] if match is not None else None
    integ_wt = _resolve_integ_wt_from_values(
        allowed_roots, task["project_id"], repo_path, integ_branch, matched_root=matched_root
    )
    return {
        "task_id": tid,
        "project_id": task["project_id"],
        "task_kind": task["kind"],
        "workspace_path": task["workspace_path"],
        "workspace_real": ws_real,
        "branch": task["branch"],
        "base_commit": task["base_commit"],
        "repo_path": repo_path,
        "integ_wt": integ_wt,
        "max_bytes": max_bytes,
        "include_untracked": include_untracked,
    }


def _resolve_project_diff_plan(ctx: HandlerContext) -> Dict[str, Any]:
    project_id = ctx.args.get("project_id") or ctx.args.get("project")
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if not project_id and scope_project:
        project_id = scope_project
    if not isinstance(project_id, str) or not project_id.strip():
        raise _usage("Missing required parameter project_id", "project_id")
    pid = project_id.strip()
    max_bytes = _diff_max_bytes_arg(ctx)
    check_authz(
        ctx.conn,
        "project.diff",
        ctx.principal,
        ctx.actor_kind,
        ctx.via,
        project_id=pid,
    )
    prow = ctx.conn.execute(
        "SELECT id, repo_path, base_branch, base_commit, integration_branch FROM projects WHERE id = ?;",
        (pid,),
    ).fetchone()
    if prow is None:
        raise _not_found("Project", pid)
    allowed_roots = _allowed_workspace_roots(ctx.conn, ctx.home)
    integ_wt = _resolve_integ_wt_from_values(
        allowed_roots, pid, prow["repo_path"], prow["integration_branch"]
    )
    return {
        "project_id": pid,
        "repo_path": prow["repo_path"],
        "base_branch": prow["base_branch"],
        "base_commit": prow["base_commit"],
        "integration_branch": prow["integration_branch"],
        "integ_wt": integ_wt,
        "max_bytes": max_bytes,
    }


async def prepare_diff_command(ctx: HandlerContext) -> None:
    """Validate, authorize, and offload read-only git diff computation off the event loop."""
    from mesh.core.diffs import compute_project_diff, compute_task_diff

    if ctx.cmd == "task.diff":
        plan = _resolve_task_diff_plan(ctx)
        data = await asyncio.to_thread(compute_task_diff, **plan)
        setattr(ctx, "_prepared_diff", {"cmd": "task.diff", "task_id": plan["task_id"], "data": data})
        return
    if ctx.cmd == "project.diff":
        plan = _resolve_project_diff_plan(ctx)
        data = await asyncio.to_thread(compute_project_diff, **plan)
        setattr(ctx, "_prepared_diff", {"cmd": "project.diff", "project_id": plan["project_id"], "data": data})
        return


# --- run ----------------------------------------------------------------------
def handle_run_list(ctx: HandlerContext) -> HandlerResult:
    task_id = ctx.args.get("task")
    status = ctx.args.get("status")
    statuses = ctx.args.get("statuses")
    raw_project = ctx.args.get("project")
    raw_project_id = ctx.args.get("project_id")
    raw_limit = ctx.args.get("limit")

    if raw_project is not None and (not isinstance(raw_project, str) or not raw_project.strip()):
        raise _usage("--project must be a non-empty string", "project")
    if raw_project_id is not None and (not isinstance(raw_project_id, str) or not raw_project_id.strip()):
        raise _usage("project_id must be a non-empty string", "project_id")
    if raw_project and raw_project_id and raw_project != raw_project_id:
        raise _usage("Conflicting 'project' and 'project_id' arguments", "project")
    project_filter = raw_project or raw_project_id

    if status is not None and status not in RUN_STATUSES:
        raise _usage(f"--status must be one of: {', '.join(RUN_STATUSES)}", "status")
    status_list: List[str] = []
    if statuses is not None:
        if not isinstance(statuses, list) or not statuses:
            raise _usage("Field 'statuses' must be a non-empty list of run statuses", "statuses")
        for st in statuses:
            if not isinstance(st, str) or st not in RUN_STATUSES:
                raise _usage(f"Each entry in 'statuses' must be one of: {', '.join(RUN_STATUSES)}", "statuses")
            if st not in status_list:
                status_list.append(st)
    if status is not None:
        if status_list and status not in status_list:
            status_list = []
        elif not status_list:
            status_list = [status]

    limit: Optional[int] = None
    if raw_limit is not None:
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1 or raw_limit > 500:
            raise _usage("Field 'limit' must be an integer between 1 and 500", "limit")
        limit = raw_limit

    if task_id:
        task = _get_task_row(ctx.conn, task_id)
        check_authz(ctx.conn, "run.list", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=task["project_id"], task_id=task_id)
        if project_filter and task["project_id"] != project_filter:
            check_authz(ctx.conn, "run.list", ctx.principal, ctx.actor_kind, ctx.via,
                        project_id=project_filter)
            return HandlerResult(data={"runs": []})
    elif project_filter:
        check_authz(ctx.conn, "run.list", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=project_filter)
    else:
        check_authz(ctx.conn, "run.list", ctx.principal, ctx.actor_kind, ctx.via)

    if status is not None and statuses is not None and not status_list:
        return HandlerResult(data={"runs": []})

    query = """SELECT r.*, p.name AS agent_name FROM runs r
               LEFT JOIN principals p ON r.agent_id = p.id WHERE 1=1"""
    params: List[Any] = []
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        # Review m2-r9 finding 1(a): a lead run token lists only its own project's runs.
        if project_filter and project_filter != scope_project:
            return HandlerResult(data={"runs": []})
        query += " AND r.project_id = ?"
        params.append(scope_project)
    elif project_filter:
        query += " AND r.project_id = ?"
        params.append(project_filter)
    if task_id:
        query += " AND r.task_id = ?"
        params.append(task_id)
    if status_list:
        if len(status_list) == 1:
            query += " AND r.status = ?"
            params.append(status_list[0])
        else:
            ph = ",".join("?" for _ in status_list)
            query += f" AND r.status IN ({ph})"
            params.extend(status_list)

    unrestricted = scope_project is not None or _is_unrestricted_reader(ctx)
    if limit is not None:
        query += " ORDER BY COALESCE(r.started_at, '') DESC, r.rowid DESC"
        if unrestricted:
            query += " LIMIT ?"
            params.append(limit)
        query += ";"
        rows = list(ctx.conn.execute(query, params).fetchall())
        if not unrestricted:
            agent_id = ctx.principal.get("id")
            rows = [
                r for r in rows
                if (r["task_id"] and can_principal_read_task(ctx.conn, agent_id, r["task_id"]))
                or (not r["task_id"] and can_principal_read_project(ctx.conn, agent_id, r["project_id"]))
            ][:limit]
        rows.reverse()
    else:
        query += " ORDER BY COALESCE(r.started_at, '') ASC, r.rowid ASC;"
        rows = list(ctx.conn.execute(query, params).fetchall())
        if not unrestricted:
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


def _prepared_log_match(
    ctx: HandlerContext,
    cmd: str,
    run: sqlite3.Row,
    streams: List[str],
    tail: int,
    *,
    task_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    prep = getattr(ctx, "_prepared_log", None)
    if (
        isinstance(prep, dict)
        and prep.get("cmd") == cmd
        and (task_id is None or prep.get("task_id") == task_id)
        and prep.get("run_id") == run["id"]
        and prep.get("run_dir") == run["run_dir"]
        and prep.get("streams") == streams
        and prep.get("tail") == tail
        and isinstance(prep.get("data"), dict)
    ):
        data = dict(prep["data"])
        data["status"] = run["status"]
        return data
    return None


def handle_run_log(ctx: HandlerContext) -> HandlerResult:
    run, streams, tail = _resolve_run_log_target(ctx)
    data = _prepared_log_match(ctx, "run.log", run, streams, tail)
    return HandlerResult(data=data if data is not None else _run_logs(run, streams, tail))


# --- task log / run activity / git diffs --------------------------------------
def handle_task_log(ctx: HandlerContext) -> HandlerResult:
    task_id, run, streams, tail = _resolve_task_log_target(ctx)
    if run is None:
        return HandlerResult(data={"task_id": task_id, "run_id": None, "status": None, "streams": {}})
    data = _prepared_log_match(ctx, "task.log", run, streams, tail, task_id=task_id)
    return HandlerResult(data=data if data is not None else _run_logs(run, streams, tail))


def handle_run_activity(ctx: HandlerContext) -> HandlerResult:
    task_id, run, limit = _resolve_run_activity_target(ctx)
    if run is None:
        return HandlerResult(
            data={
                "available": False,
                "run_id": None,
                "task_id": task_id,
                "status": None,
                "adapter": None,
                "source": "empty",
                "entries": [],
                "counts": {},
                "total_entries": 0,
                "lines_scanned": 0,
                "log_capped": False,
                "truncated": False,
            }
        )
    prep = getattr(ctx, "_prepared_activity", None)
    if (
        isinstance(prep, dict)
        and prep.get("run_id") == run["id"]
        and prep.get("run_dir") == run["run_dir"]
        and prep.get("limit") == limit
        and isinstance(prep.get("data"), dict)
    ):
        data = dict(prep["data"])
        data["status"] = run["status"]
        return HandlerResult(data=data)
    return HandlerResult(data=_extract_activity_for_run(run, limit))


def handle_task_diff(ctx: HandlerContext) -> HandlerResult:
    from mesh.core.diffs import compute_task_diff

    plan = _resolve_task_diff_plan(ctx)
    prep = getattr(ctx, "_prepared_diff", None)
    if (
        isinstance(prep, dict)
        and prep.get("cmd") == "task.diff"
        and prep.get("task_id") == plan["task_id"]
        and isinstance(prep.get("data"), dict)
    ):
        return HandlerResult(data=prep["data"])
    return HandlerResult(data=compute_task_diff(**plan))


def handle_project_diff(ctx: HandlerContext) -> HandlerResult:
    from mesh.core.diffs import compute_project_diff

    plan = _resolve_project_diff_plan(ctx)
    prep = getattr(ctx, "_prepared_diff", None)
    if (
        isinstance(prep, dict)
        and prep.get("cmd") == "project.diff"
        and prep.get("project_id") == plan["project_id"]
        and isinstance(prep.get("data"), dict)
    ):
        return HandlerResult(data=prep["data"])
    return HandlerResult(data=compute_project_diff(**plan))


# --- workspace ----------------------------------------------------------------
def _task_workspace_entry(
    row: sqlite3.Row,
    allowed_roots: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from mesh.core.paths import resolve_trusted_path

    path = row["workspace_path"]
    exists = False
    if path:
        if allowed_roots is not None:
            match = _match_task_workspace_with_roots(
                allowed_roots, row["project_id"], row["id"], path
            )
            if match is not None:
                _stripped, real_path, _matched_root = match
                exists = os.path.isdir(real_path) and not _plat_paths.is_reparse_link(_stripped)
        else:
            trusted = resolve_trusted_path(path)
            exists = trusted is not None and os.path.isdir(trusted)
    return {
        "kind": "task",
        "task_id": row["id"],
        "project_id": row["project_id"],
        "status": row["status"],
        "path": path,
        "exists": exists,
        "branch": row["branch"],
        "base_commit": row["base_commit"],
    }


def _lead_workspace_entries(ctx: HandlerContext, project_id: str, root: str) -> List[Dict[str, Any]]:
    from mesh.core.paths import resolve_trusted_path

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
        trusted = resolve_trusted_path(path, root=root)
        out.append({
            "kind": "lead",
            "project_id": project_id,
            "agent_id": r["agent_id"],
            "agent": r["agent_name"],
            "current": bool(r["is_current"]),
            "path": path,
            "exists": trusted is not None and os.path.isdir(trusted),
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
    allowed_roots = _allowed_workspace_roots(ctx.conn, ctx.home)

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
                entries.append(_task_workspace_entry(t, allowed_roots))
    return HandlerResult(data=redact_value({"root": root, "workspaces": entries}))


def handle_workspace_show(ctx: HandlerContext) -> HandlerResult:
    from mesh.core.paths import resolve_trusted_path

    task_id = ctx.args.get("task_id")
    if not task_id:
        raise _usage("Missing required parameter task_id", "task_id")
    task = _get_task_row(ctx.conn, task_id)
    check_authz(ctx.conn, "workspace.show", ctx.principal, ctx.actor_kind, ctx.via,
                project_id=task["project_id"], task_id=task_id)
    allowed_roots = _allowed_workspace_roots(ctx.conn, ctx.home)
    match = _match_task_workspace_with_roots(
        allowed_roots, task["project_id"], task_id, task["workspace_path"]
    )
    entry = _task_workspace_entry(task, allowed_roots)
    files: List[str] = []
    if match is not None and entry["exists"]:
        _stripped, base, matched_root = match
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            if resolve_trusted_path(dirpath, root=matched_root) is None:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(
                d for d in dirnames
                if d != ".git" and not _plat_paths.is_reparse_link(os.path.join(dirpath, d))
            )
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


def _commits_beyond_base(
    workspace: str,
    base_commit: Optional[str],
    *,
    branch: Optional[str] = None,
    integ_wt: Optional[str] = None,
) -> Optional[int]:
    """Max commit count of ``refs/heads/<branch>`` and verified worktree ``HEAD`` beyond ``base_commit``."""
    from mesh.core.gitops import run_git
    from mesh.core.paths import resolve_trusted_path
    from mesh.core.repos import is_registered_worktree

    if not base_commit:
        return None
    ws_clean = _plat_paths.strip_trailing_seps(workspace) if isinstance(workspace, str) else ""
    if _plat_paths.is_fs_root(ws_clean):
        ws_clean = ""
    counts: List[int] = []
    if branch and integ_wt and os.path.isdir(integ_wt):
        ref = f"refs/heads/{branch}"
        try:
            ref_cp = run_git(["show-ref", "--verify", "--quiet", ref], cwd=integ_wt)
        except Exception:
            return None
        if ref_cp.returncode == 0:
            try:
                cp = run_git(["log", "--format=%H", f"{base_commit}..{ref}"], cwd=integ_wt)
            except Exception:
                return None
            if cp.returncode != 0:
                return None
            counts.append(len([ln for ln in (cp.stdout or "").splitlines() if ln.strip()]))
        elif not (ws_clean and os.path.lexists(ws_clean)):
            return None
    if ws_clean and os.path.lexists(ws_clean):
        if not os.path.isdir(ws_clean):
            return None
        if branch is not None or integ_wt is not None:
            if not integ_wt or not os.path.isdir(integ_wt) or not is_registered_worktree(ws_clean, integ_wt):
                return None
        elif resolve_trusted_path(ws_clean) is None:
            return None
        try:
            cp = run_git(["log", "--format=%H", f"{base_commit}..HEAD"], cwd=ws_clean)
        except Exception:
            return None
        if cp.returncode != 0:
            return None
        counts.append(len([ln for ln in (cp.stdout or "").splitlines() if ln.strip()]))
    return max(counts) if counts else None


_allowed_workspace_roots = allowed_workspace_roots


def _has_symlink_ancestor(path: str) -> bool:
    from mesh.core.paths import has_symlink_component

    return has_symlink_component(path)


def _verify_workspace_ancestors_not_symlinked(
    stripped: str,
    matched_root: str,
    project_id: str,
    task_id: str,
) -> Optional[str]:
    """Verify ``stripped`` and all its ancestors up to ``/`` are non-symlinks resolving under ``matched_root``."""
    from mesh.core.paths import resolve_trusted_path

    if (
        not isinstance(stripped, str)
        or not stripped
        or not isinstance(matched_root, str)
        or not matched_root
    ):
        return None
    stripped_clean = _plat_paths.strip_trailing_seps(stripped)
    matched_root_clean = _plat_paths.strip_trailing_seps(matched_root)
    if (
        not stripped_clean
        or not matched_root_clean
        or _plat_paths.is_fs_root(stripped_clean)
        or _plat_paths.is_fs_root(matched_root_clean)
    ):
        return None
    leaf = os.path.basename(stripped_clean)
    if leaf != task_id and not (
        leaf.startswith(f"{task_id}.a") and leaf[len(f"{task_id}.a"):].isdigit()
    ):
        return None
    stripped_real = resolve_trusted_path(stripped_clean, root=matched_root_clean)
    cand_real = resolve_trusted_path(matched_root_clean, allow_equal=True)
    if stripped_real is None or cand_real is None:
        return None
    expected = os.path.join(cand_real, project_id, "tasks", leaf)
    return stripped_real if _plat_paths.same_path(stripped_real, expected) else None


def _match_task_workspace_with_roots(
    roots: List[str],
    project_id: str,
    task_id: str,
    path: Optional[str],
) -> Optional[Tuple[str, str, str]]:
    if not isinstance(path, str) or not path.strip():
        return None
    stripped = _plat_paths.strip_trailing_seps(path)
    if not stripped or _plat_paths.is_fs_root(stripped):
        return None
    for cand_root in roots:
        real = _verify_workspace_ancestors_not_symlinked(stripped, cand_root, project_id, task_id)
        if real is not None:
            return stripped, real, cand_root
    return None


def _task_workspace_match(
    conn: sqlite3.Connection,
    home: str,
    project_id: str,
    task_id: str,
    path: Optional[str],
) -> Optional[Tuple[str, str, str]]:
    """Validate ``path`` is the non-symlinked ``<root>/<project_id>/tasks/<task_id>[.aN]`` dir.

    Returns ``(stripped_path, real_path, matched_root)`` or ``None`` if invalid or symlinked.
    """
    return _match_task_workspace_with_roots(
        _allowed_workspace_roots(conn, home), project_id, task_id, path
    )


def _resolve_integ_wt_from_values(
    allowed_roots: List[str],
    project_id: str,
    repo_path: Optional[str],
    integ_branch: Optional[str],
    matched_root: Optional[str] = None,
) -> Optional[str]:
    """Locate the integration worktree (or repo) from pre-read project metadata."""
    from mesh.core.paths import resolve_trusted_path

    root_candidates: List[str] = []
    if matched_root:
        root_candidates.append(matched_root)
    for r in allowed_roots:
        if r not in root_candidates:
            root_candidates.append(r)
    for r in root_candidates:
        if not r:
            continue
        cand = os.path.join(r, project_id, "integration")
        trusted = resolve_trusted_path(cand, root=r)
        if trusted and os.path.isdir(trusted):
            return trusted
    trusted_repo = resolve_trusted_path(repo_path) if repo_path else None
    if trusted_repo and os.path.isdir(trusted_repo) and integ_branch:
        try:
            wt = _worktree_for_branch(trusted_repo, integ_branch)
            trusted_wt = resolve_trusted_path(wt) if wt else None
            if trusted_wt and os.path.isdir(trusted_wt):
                return trusted_wt
        except Exception:
            pass
    if trusted_repo and os.path.isdir(trusted_repo):
        return trusted_repo
    return None


def _resolve_integ_wt(
    conn: sqlite3.Connection,
    home: str,
    task: Mapping[str, Any],
    matched_root: Optional[str] = None,
) -> Optional[str]:
    """Locate the integration worktree (or repo) for ``task``, even if ``workspaces.root`` changed."""
    project_id = task["project_id"]
    row = conn.execute(
        "SELECT repo_path, integration_branch FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    repo_path = row["repo_path"] if row is not None else None
    integ_branch = row["integration_branch"] if row is not None else None
    return _resolve_integ_wt_from_values(
        _allowed_workspace_roots(conn, home),
        project_id,
        repo_path,
        integ_branch,
        matched_root=matched_root,
    )


def _repo_merge_busy(conn: sqlite3.Connection, project_id: str) -> bool:
    """Carbon P2-7: True while a merge runs on this project's repository.

    The merge may belong to this project or to another one sharing the same repo path
    (same realpath): it owns the shared ``.git``, so prune must not race its git writes.
    """
    from mesh.daemon.merge import merge_in_progress_for_repo

    row = conn.execute("SELECT repo_path FROM projects WHERE id = ?;", (project_id,)).fetchone()
    return row is not None and merge_in_progress_for_repo(conn, row["repo_path"]) is not None


def _prune_skip_reason_from_parts(
    allowed_roots: List[str],
    task: Mapping[str, Any],
    has_task_lease: bool,
    has_merge_lease: bool,
    *,
    integ_wt: Optional[str] = None,
) -> Optional[str]:
    if has_task_lease:
        return "lease_held"
    if task["status"] == "merging" or has_merge_lease:
        return "merge_in_progress"
    match = _match_task_workspace_with_roots(
        allowed_roots, task["project_id"], task["id"], task["workspace_path"]
    )
    if match is None:
        return "outside_workspaces_root"
    _stripped, real_path, _matched_root = match
    if task["status"] == "cancelled":
        count = _commits_beyond_base(
            real_path,
            task["base_commit"],
            branch=task["branch"],
            integ_wt=integ_wt,
        )
        if count is None:
            return "commits_unknown"
        if count > 0:
            return "unmerged_commits"
    return None


def _prune_skip_reason(ctx: HandlerContext, task: Mapping[str, Any]) -> Optional[str]:
    """Why a candidate is skipped, or None when it may be pruned."""
    has_task_lease = bool(
        ctx.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task['id']}",)).fetchone()
    )
    has_merge_lease = bool(
        ctx.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"merge:{task['project_id']}",)).fetchone()
    ) or _repo_merge_busy(ctx.conn, task["project_id"])
    match = _task_workspace_match(
        ctx.conn, ctx.home, task["project_id"], task["id"], task["workspace_path"]
    )
    matched_root = match[2] if match else None
    integ_wt = _resolve_integ_wt(ctx.conn, ctx.home, task, matched_root=matched_root)
    return _prune_skip_reason_from_parts(
        _allowed_workspace_roots(ctx.conn, ctx.home),
        task,
        has_task_lease,
        has_merge_lease,
        integ_wt=integ_wt,
    )


def _local_heads(integ_wt: str) -> Optional[Set[str]]:
    """All ``refs/heads/*`` names in one ``git show-ref --heads`` (None on error)."""
    from mesh.core.gitops import run_git

    try:
        cp = run_git(["show-ref", "--heads"], cwd=integ_wt)
    except Exception:
        return None
    if cp.returncode not in (0, 1):
        return None
    heads: Set[str] = set()
    for line in (cp.stdout or "").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            heads.add(parts[1][len("refs/heads/"):])
    return heads


def _already_pruned(
    task: Mapping[str, Any],
    integ_wt: Optional[str],
    heads_cache: Optional[Dict[str, Optional[Set[str]]]] = None,
) -> bool:
    """True when both the worktree directory and the branch are already gone (§10.7).

    A second `workspace prune` must be a no-op: an already-pruned task is done, not skipped
    with a failure reason (review m2-r8 MINOR-3).
    """
    from mesh.core.gitops import run_git

    wp = _plat_paths.strip_trailing_seps(task["workspace_path"] or "")
    if _plat_paths.is_fs_root(wp):
        wp = ""
    if wp and os.path.lexists(wp):
        return False
    branch = task["branch"]
    if not branch:
        return True
    if not integ_wt or not os.path.isdir(integ_wt):
        return False
    if heads_cache is not None:
        # Review PR45 R5 (second review) P2: one show-ref per integration worktree.
        if integ_wt not in heads_cache:
            heads_cache[integ_wt] = _local_heads(integ_wt)
        heads = heads_cache[integ_wt]
        if heads is not None:
            return branch not in heads
    try:
        cp = run_git(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=integ_wt)
    except Exception:
        return False
    return cp.returncode != 0


def _remove_worktree_and_branch_with_roots(
    allowed_roots: List[str],
    task: Mapping[str, Any],
    integ_wt: Optional[str],
    *,
    home: Optional[str] = None,
    testing_mode: Optional[bool] = None,
) -> Optional[str]:
    from mesh.core.gitops import run_git

    match = _match_task_workspace_with_roots(
        allowed_roots, task["project_id"], task["id"], task["workspace_path"]
    )
    if match is None:
        return "outside_workspaces_root"
    stripped, real_path, _matched_root = match
    if _plat_paths.is_reparse_link(stripped):
        return "outside_workspaces_root"
    from mesh.core.repos import is_registered_worktree

    branch = task["branch"]
    if branch and not (integ_wt and os.path.isdir(integ_wt)):
        return "branch_delete_failed"
    if branch and integ_wt and os.path.isdir(integ_wt):
        ref = f"refs/heads/{branch}"
        try:
            ref_cp = run_git(["show-ref", "--verify", "--quiet", ref], cwd=integ_wt)
            if ref_cp.returncode == 0:
                anc_cp = run_git(["merge-base", "--is-ancestor", ref, "HEAD"], cwd=integ_wt)
                if anc_cp.returncode != 0:
                    return "branch_delete_failed"
        except Exception:
            return "branch_delete_failed"
    if os.path.lexists(real_path):
        if not os.path.isdir(real_path) or (integ_wt and not is_registered_worktree(real_path, integ_wt)):
            return "worktree_remove_failed"
        if integ_wt and os.path.isdir(integ_wt):
            try:
                wt_head_cp = run_git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=real_path)
                if wt_head_cp.returncode != 0 or not (wt_head_cp.stdout or "").strip():
                    return "worktree_remove_failed"
                wt_head = wt_head_cp.stdout.strip()
                wt_anc = run_git(["merge-base", "--is-ancestor", wt_head, "HEAD"], cwd=integ_wt)
                if wt_anc.returncode != 0:
                    return "worktree_remove_failed"
            except Exception:
                return "worktree_remove_failed"
        try:
            cp = run_git(["worktree", "remove", real_path], cwd=integ_wt or real_path)
        except Exception:
            return "worktree_remove_failed"
        if cp.returncode != 0:
            return "worktree_remove_failed"
        if home:
            _maybe_ws_crash(home, "after_ws_prune_worktree_remove", testing_mode)
    elif branch and integ_wt and os.path.isdir(integ_wt):
        try:
            run_git(["worktree", "prune"], cwd=integ_wt)
        except Exception:
            pass
    if branch and integ_wt and os.path.isdir(integ_wt):
        try:
            cp = run_git(["branch", "-d", branch], cwd=integ_wt)
        except Exception:
            return "branch_delete_failed"
        if cp.returncode != 0:
            return "branch_delete_failed"
    return None


def _remove_worktree_and_branch(ctx: HandlerContext, task: Mapping[str, Any], integ_wt: Optional[str]) -> Optional[str]:
    """`worktree remove` (no force) then `branch -d` in the integration worktree.

    Returns None on success or a skip reason. `branch -d` refuses an unmerged branch,
    which is exactly the §10.7 guarantee: Mesh never deletes unmerged work.
    """
    return _remove_worktree_and_branch_with_roots(
        _allowed_workspace_roots(ctx.conn, ctx.home), task, integ_wt
    )


_WS_CRASH_POINTS = (
    "after_ws_reserve",
    "after_ws_delete",
    "after_ws_prune_reserve",
    "before_ws_prune_delete",
    "after_ws_prune_worktree_remove",
    "after_ws_prune_delete",
    "after_ws_prune_last_delete",
    "after_ws_prune",
)


def _maybe_ws_crash(home: str, point: str, testing_mode: Optional[bool] = None) -> None:
    """Testing hook ``MESH_TEST_CRASH_AT`` for workspace delete/prune (testing mode only)."""
    if not is_testing_mode(testing_mode):
        return
    if point not in _WS_CRASH_POINTS or os.environ.get("MESH_TEST_CRASH_AT") != point:
        return
    from mesh.daemon import supervisor

    supervisor._log_meshd(home, f"MESH_TEST_CRASH_AT={point}: killing meshd")
    crash_self_now()


def _write_ws_prune_checkpoint(path: Optional[str], doc: Mapping[str, Any]) -> None:
    if not path:
        return
    ensure_run_dir(os.path.dirname(path))
    tmp = f"{path}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            data = (json.dumps(dict(doc), sort_keys=True) + "\n").encode("utf-8")
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            if sys.platform != "win32" and hasattr(os, "fchmod"):
                _plat_perms.fchmod_private(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        if sys.platform == "win32" or not hasattr(os, "fchmod"):
            _plat_perms.set_private_file_acl(tmp, 0o600)
        _plat_fs.atomic_replace(tmp, path)
    except Exception:
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_ws_prune_checkpoint(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    try:
        data = json.loads(_plat_fs.read_bytes_shared(path).decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _unlink_best_effort(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _collect_prune_project_meta(
    conn: sqlite3.Connection,
    doc: Mapping[str, Any],
    ckpt: Optional[Mapping[str, Any]],
) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    pids: Set[str] = set()
    if isinstance(doc.get("project_id"), str) and doc["project_id"]:
        pids.add(doc["project_id"])
    for p in (doc.get("projects") or []):
        if isinstance(p, str) and p:
            pids.add(p)
    for p in (doc.get("planned") or []):
        if isinstance(p, dict) and isinstance(p.get("project_id"), str) and p["project_id"]:
            pids.add(p["project_id"])
    if isinstance(ckpt, dict):
        in_flight = ckpt.get("in_flight")
        if isinstance(in_flight, dict) and isinstance(in_flight.get("project_id"), str) and in_flight["project_id"]:
            pids.add(in_flight["project_id"])
        for lst in (ckpt.get("removed"), ckpt.get("skipped")):
            if isinstance(lst, list):
                for e in lst:
                    if isinstance(e, dict) and isinstance(e.get("project_id"), str) and e["project_id"]:
                        pids.add(e["project_id"])
    meta: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for pid in sorted(pids):
        row = conn.execute(
            "SELECT repo_path, integration_branch FROM projects WHERE id = ?;",
            (pid,),
        ).fetchone()
        meta[pid] = (row["repo_path"], row["integration_branch"]) if row is not None else (None, None)
    return meta


def reconcile_prune_checkpoint_from_data(
    allowed_roots: List[str],
    default_root: str,
    proj_meta: Mapping[str, Tuple[Optional[str], Optional[str]]],
    doc: Mapping[str, Any],
    ckpt: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Reconcile a prune checkpoint using pre-read DB snapshots and disk/git state."""
    root_val = (
        ckpt.get("root")
        if isinstance(ckpt, dict) and isinstance(ckpt.get("root"), str)
        else default_root
    )
    if isinstance(ckpt, dict) and "active_planned_ids" in ckpt and ckpt["active_planned_ids"] is None:
        return {
            "root": root_val,
            "removed": [],
            "skipped": [],
            "completed": False,
        }

    removed: List[Dict[str, Any]] = []
    removed_ids: Set[str] = set()
    if isinstance(ckpt, dict) and isinstance(ckpt.get("removed"), list):
        for e in ckpt["removed"]:
            if isinstance(e, dict) and e.get("task_id") and e["task_id"] not in removed_ids:
                removed.append(e)
                removed_ids.add(e["task_id"])

    skipped: List[Dict[str, Any]] = []
    skipped_ids: Set[str] = set()
    if isinstance(ckpt, dict) and isinstance(ckpt.get("skipped"), list):
        for e in ckpt["skipped"]:
            if isinstance(e, dict) and e.get("task_id") and e["task_id"] not in skipped_ids:
                skipped.append(e)
                skipped_ids.add(e["task_id"])

    planned_list = [
        p for p in (doc.get("planned") or []) if isinstance(p, dict) and p.get("task_id")
    ]
    if isinstance(ckpt, dict) and "active_planned_ids" in ckpt and isinstance(ckpt["active_planned_ids"], list):
        active_ids = {tid for tid in ckpt["active_planned_ids"] if isinstance(tid, str) and tid}
        target_planned = [p for p in planned_list if p["task_id"] in active_ids]
    else:
        # Review PR45 R5 P2: ``planned`` also lists code tasks pruned by earlier
        # requests (their worktree and branch are long gone, so ``_already_pruned``
        # holds). Without the worker's ``active_planned_ids`` (checkpoint missing,
        # unreadable or never initialised) we cannot tell this request's removals
        # from history, so only an explicit ``in_flight`` entry is reconciled and the
        # prune is never reported as completed from ``planned`` alone.
        target_planned = []
    planned_by_id = {p["task_id"]: p for p in planned_list}
    integ_cache: Dict[Tuple[str, Optional[str]], Optional[str]] = {}
    heads_cache: Dict[str, Optional[Set[str]]] = {}

    ckpt_completed = bool(isinstance(ckpt, dict) and ckpt.get("completed"))
    candidates: List[Dict[str, Any]] = []
    if not ckpt_completed:
        in_flight = ckpt.get("in_flight") if isinstance(ckpt, dict) else None
        if isinstance(in_flight, dict) and in_flight.get("task_id"):
            candidates.append(in_flight)
        candidates.extend(target_planned)

    covered = removed_ids | skipped_ids
    for cand in candidates:
        tid = cand.get("task_id")
        if not tid or tid in covered:
            continue
        meta = planned_by_id.get(tid) or {}
        pid = cand.get("project_id") or meta.get("project_id")
        repo_path = cand.get("repo_path") if "repo_path" in cand else meta.get("repo_path")
        integ_branch = (
            cand.get("integration_branch")
            if "integration_branch" in cand
            else meta.get("integration_branch")
        )
        if repo_path is None and pid and pid in proj_meta:
            db_repo, db_ibr = proj_meta[pid]
            repo_path = db_repo
            if integ_branch is None:
                integ_branch = db_ibr
        matched_root = cand.get("matched_root") or meta.get("matched_root")
        wp = cand.get("path") if "path" in cand else meta.get("path")
        branch = cand.get("branch") if "branch" in cand else meta.get("branch")
        if pid:
            ck = (pid, matched_root)
            if ck not in integ_cache:
                integ_cache[ck] = _resolve_integ_wt_from_values(
                    allowed_roots,
                    pid,
                    repo_path,
                    integ_branch,
                    matched_root=matched_root,
                )
            integ_wt = integ_cache[ck]
        else:
            integ_wt = None
        if _already_pruned({"workspace_path": wp, "branch": branch}, integ_wt, heads_cache):
            entry = {
                "task_id": tid,
                "project_id": pid,
                "status": cand.get("status") or meta.get("status"),
                "path": wp,
                "branch": branch,
            }
            removed.append(entry)
            removed_ids.add(tid)
            covered.add(tid)

    completed = ckpt_completed
    if not completed and target_planned and all(p["task_id"] in covered for p in target_planned):
        completed = True
    return {
        "root": root_val,
        "removed": removed,
        "skipped": skipped,
        "completed": completed,
    }


def reconcile_prune_checkpoint(
    conn: sqlite3.Connection,
    home: str,
    doc: Mapping[str, Any],
    ckpt: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Reconcile a prune checkpoint against ``in_flight`` and ``planned`` candidates."""
    allowed_roots = _allowed_workspace_roots(conn, home)
    default_root = get_workspaces_root(conn, home)
    proj_meta = _collect_prune_project_meta(conn, doc, ckpt)
    return reconcile_prune_checkpoint_from_data(allowed_roots, default_root, proj_meta, doc, ckpt)


def plan_workspace_prune(ctx: HandlerContext) -> Dict[str, Any]:
    """Snapshot SQLite state needed for ``workspace.prune`` without invoking git subprocesses."""
    project_id = ctx.args.get("project")
    if project_id:
        if ctx.conn.execute("SELECT 1 FROM projects WHERE id = ?;", (project_id,)).fetchone() is None:
            raise _not_found("Project", project_id)
    check_authz(ctx.conn, "workspace.prune", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    if project_id:
        res_key = has_project_any_ws_reservation(ctx.conn, project_id)
        if res_key:
            raise MeshError(
                code=c.CONCURRENCY_LIMIT,
                exit_code=c.EXIT_BUSY,
                message=f"Cannot prune workspaces for project '{project_id}': workspace operation '{res_key}' is in progress",
                details={"reservation": res_key, "project_id": project_id},
            )
    else:
        res = load_ws_reservations(ctx.conn)
        if res:
            res_key = res[0][0]
            raise MeshError(
                code=c.CONCURRENCY_LIMIT,
                exit_code=c.EXIT_BUSY,
                message=f"Cannot prune workspaces: workspace operation '{res_key}' is in progress",
                details={"reservation": res_key},
            )

    root = get_workspaces_root(ctx.conn, ctx.home)
    allowed_roots = _allowed_workspace_roots(ctx.conn, ctx.home)
    proj_meta: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    merge_leases: Dict[str, bool] = {}
    items: List[Dict[str, Any]] = []
    # Review PR45 R5 (second review) P2: tasks an earlier prune already recorded as
    # pruned, whose worktree is gone, stay out of the plan (and out of the
    # reservation's ``planned`` list) without any git call.
    pruned_ids = {
        r[0]
        for r in ctx.conn.execute(
            "SELECT DISTINCT task_id FROM events WHERE type = 'workspace.pruned' AND task_id IS NOT NULL;"
        ).fetchall()
    }
    for task_row in _prune_candidates(ctx, project_id):
        task = dict(task_row)
        wp = _plat_paths.strip_trailing_seps(task["workspace_path"] or "")
        if _plat_paths.is_fs_root(wp):
            wp = ""
        if not task["branch"] and (not wp or not os.path.lexists(wp)):
            continue
        if task["id"] in pruned_ids and (not wp or not os.path.lexists(wp)):
            continue
        pid = task["project_id"]
        if pid not in proj_meta:
            prow = ctx.conn.execute(
                "SELECT repo_path, integration_branch FROM projects WHERE id = ?;",
                (pid,),
            ).fetchone()
            proj_meta[pid] = (prow["repo_path"], prow["integration_branch"]) if prow is not None else (None, None)
            merge_leases[pid] = bool(
                ctx.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"merge:{pid}",)).fetchone()
            ) or _repo_merge_busy(ctx.conn, pid)
        has_task_lease = bool(
            ctx.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task['id']}",)).fetchone()
        )
        repo_path, integ_branch = proj_meta[pid]
        match = _match_task_workspace_with_roots(
            allowed_roots, pid, task["id"], task["workspace_path"]
        )
        matched_root = match[2] if match else None
        items.append({
            "task": task,
            "repo_path": repo_path,
            "integration_branch": integ_branch,
            "matched_root": matched_root,
            "has_task_lease": has_task_lease,
            "has_merge_lease": merge_leases[pid],
        })
    return {
        "root": root,
        "allowed_roots": allowed_roots,
        "dry_run": ctx.dry_run,
        "items": items,
    }


def execute_workspace_prune_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Execute git/worktree pruning for a snapshot plan (safe to run off the event loop)."""
    root = plan["root"]
    allowed_roots = plan["allowed_roots"]
    dry_run = bool(plan["dry_run"])
    ckpt_path = None if dry_run else plan.get("checkpoint_path")
    home = plan.get("home")
    testing_mode = plan.get("testing_mode")
    items = plan["items"]
    removed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    integ_cache: Dict[Tuple[str, Optional[str]], Optional[str]] = {}

    def _get_integ_wt(item: Mapping[str, Any]) -> Optional[str]:
        task = item["task"]
        pid = task["project_id"]
        matched_root = item.get("matched_root")
        if matched_root is None:
            match = _match_task_workspace_with_roots(
                allowed_roots, pid, task["id"], task["workspace_path"]
            )
            matched_root = match[2] if match else None
        ck = (pid, matched_root)
        if ck not in integ_cache:
            integ_cache[ck] = _resolve_integ_wt_from_values(
                allowed_roots,
                pid,
                item["repo_path"],
                item["integration_branch"],
                matched_root=matched_root,
            )
        return integ_cache[ck]

    active_items: List[Tuple[Dict[str, Any], Optional[str]]] = []
    heads_cache: Dict[str, Optional[Set[str]]] = {}
    for item in items:
        task = item["task"]
        integ_wt = _get_integ_wt(item)
        with _task_lock(task["id"]):
            if _already_pruned(task, integ_wt, heads_cache):
                continue
        active_items.append((item, integ_wt))

    active_planned_ids = [it["task"]["id"] for it, _ in active_items]
    if ckpt_path:
        _write_ws_prune_checkpoint(
            ckpt_path,
            {
                "root": root,
                "completed": not active_items,
                "removed": removed,
                "skipped": skipped,
                "in_flight": None,
                "active_planned_ids": active_planned_ids,
            },
        )

    for idx, (item, integ_wt) in enumerate(active_items):
        is_last = idx == len(active_items) - 1
        task = item["task"]
        entry = {"task_id": task["id"], "project_id": task["project_id"], "status": task["status"],
                 "path": task["workspace_path"], "branch": task["branch"]}
        with _task_lock(task["id"]):
            if _already_pruned(task, integ_wt):
                if ckpt_path and is_last:
                    _write_ws_prune_checkpoint(
                        ckpt_path,
                        {
                            "root": root,
                            "completed": True,
                            "removed": removed,
                            "skipped": skipped,
                            "in_flight": None,
                            "active_planned_ids": active_planned_ids,
                        },
                    )
                continue  # done by an earlier prune: neither removed nor skipped
            reason = _prune_skip_reason_from_parts(
                allowed_roots,
                task,
                item["has_task_lease"],
                item["has_merge_lease"],
                integ_wt=integ_wt,
            )
            if reason is not None:
                skipped.append(dict(entry, reason=reason))
                if ckpt_path:
                    _write_ws_prune_checkpoint(
                        ckpt_path,
                        {
                            "root": root,
                            "completed": is_last,
                            "removed": removed,
                            "skipped": skipped,
                            "in_flight": None,
                            "active_planned_ids": active_planned_ids,
                        },
                    )
                continue
            if dry_run:
                removed.append(entry)
                continue
            if ckpt_path:
                _write_ws_prune_checkpoint(
                    ckpt_path,
                    {
                        "root": root,
                        "completed": False,
                        "removed": removed,
                        "skipped": skipped,
                        "in_flight": entry,
                        "active_planned_ids": active_planned_ids,
                    },
                )
            if home:
                _maybe_ws_crash(home, "before_ws_prune_delete", testing_mode)
            reason = _remove_worktree_and_branch_with_roots(
                allowed_roots,
                task,
                integ_wt,
                home=home,
                testing_mode=testing_mode,
            )
            if reason is not None:
                skipped.append(dict(entry, reason=reason))
            else:
                removed.append(entry)
                if home:
                    _maybe_ws_crash(home, "after_ws_prune_delete", testing_mode)
                    if is_last:
                        _maybe_ws_crash(home, "after_ws_prune_last_delete", testing_mode)
            if ckpt_path:
                _write_ws_prune_checkpoint(
                    ckpt_path,
                    {
                        "root": root,
                        "completed": is_last,
                        "removed": removed,
                        "skipped": skipped,
                        "in_flight": None,
                        "active_planned_ids": active_planned_ids,
                    },
                )
    return {"root": root, "removed": removed, "skipped": skipped}


def prepare_workspace_prune(ctx: HandlerContext) -> Dict[str, Any]:
    """Plan and perform git/worktree removal for ``workspace.prune`` outside any write TX."""
    return execute_workspace_prune_plan(plan_workspace_prune(ctx))


def handle_workspace_prune(ctx: HandlerContext) -> HandlerResult:
    """`workspace prune [--project] [--dry-run]` (§10.7): lead or user.

    Removes the worktree and branch of `merged` tasks and of `cancelled` tasks with no
    commits beyond `base_commit`. Plain directories are never pruned, nothing is forced,
    and a failure is reported as `skipped` with a reason instead of raising.
    """
    plan = getattr(ctx, "prepared_prune", None)
    if plan is None:
        plan = prepare_workspace_prune(ctx)
    root = plan["root"]
    removed: List[Dict[str, Any]] = plan["removed"]
    skipped: List[Dict[str, Any]] = plan["skipped"]
    transitions = [{"task_id": e["task_id"], "project_id": e["project_id"], "path": e["path"],
                    "branch": e["branch"], "action": "prune"} for e in removed]
    events = ["workspace.pruned" for _ in removed]

    if ctx.dry_run:
        # A dry run rolls back and reports through `data.would` (server.py): the plan is
        # carried as `transitions` + `events` for each worktree that would be removed.
        return HandlerResult(
            data={"root": root, "dry_run": True, "removed": removed, "skipped": skipped},
            transitions=transitions,
            events=events,
        )

    for entry in removed:
        record_event(
            conn=ctx.conn,
            type_="workspace.pruned",
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            relayed_by=ctx.relayed_by,
            project_id=entry["project_id"],
            task_id=entry["task_id"],
            payload={"task_id": entry["task_id"], "path": entry["path"], "branch": entry["branch"]},
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
    return HandlerResult(
        data=redact_value({"root": root, "dry_run": False, "removed": removed, "skipped": skipped}),
        transitions=transitions,
        events=events,
    )


def _check_tx1_idempotency(
    conn: sqlite3.Connection,
    idem: Optional[Mapping[str, Any]],
    cmd: str,
    ctx: Optional[HandlerContext] = None,
) -> Optional[HandlerResult]:
    """Re-check idempotency and active workspace reservations inside ``BEGIN IMMEDIATE``."""
    if not idem or not idem.get("key"):
        return None
    from mesh.daemon.server import _lookup_live_idempotency, _resolve_idempotency_scope

    irow = _lookup_live_idempotency(conn, idem["principal_id"], idem["key"])
    if irow:
        if irow["request_hash"] == idem["request_hash"]:
            stored_envelope = json.loads(irow["response_json"])
            if ctx is not None:
                r_proj, r_task = _resolve_idempotency_scope(conn, cmd, ctx.args, stored_envelope)
                check_authz(
                    conn,
                    cmd,
                    ctx.principal,
                    ctx.actor_kind,
                    ctx.via,
                    project_id=r_proj,
                    task_id=r_task,
                    relayed_by=ctx.relayed_by,
                    resource_resolved=(r_proj is not None or r_task is not None),
                )
            stored_envelope["idempotent_replay"] = True
            res = HandlerResult(
                data=stored_envelope.get("data", {}),
                warnings=stored_envelope.get("warnings", []),
                exit_code=int(irow["exit_code"]),
            )
            res._idempotent_replay_envelope = stored_envelope  # type: ignore[attr-defined]
            return res
        raise MeshError(
            code=c.IDEMPOTENCY_KEY_REUSED,
            exit_code=c.EXIT_CONFLICT,
            message=f"Idempotency key '{idem['key']}' reused with different request parameters",
        )
    r_idem = find_ws_reserved_idempotency(conn, idem["principal_id"], idem["key"])
    if r_idem:
        if r_idem.get("request_hash") == idem["request_hash"]:
            raise MeshError(
                code=c.CONCURRENCY_LIMIT,
                exit_code=c.EXIT_BUSY,
                message=f"Operation '{r_idem.get('cmd') or cmd}' with idempotency key '{idem['key']}' is already in progress",
                details={"idempotency_key": idem['key'], "reservation": r_idem.get("reservation_key")},
            )
        raise MeshError(
            code=c.IDEMPOTENCY_KEY_REUSED,
            exit_code=c.EXIT_CONFLICT,
            message=f"Idempotency key '{idem['key']}' reused with different request parameters",
        )
    return None


def _store_tx_idempotency(
    conn: sqlite3.Connection,
    idem: Optional[Mapping[str, Any]],
    cmd: str,
    request_id: str,
    res: HandlerResult,
) -> None:
    if not idem or not idem.get("key"):
        return
    if res.exit_code in (c.EXIT_INTERNAL, c.EXIT_APPROVAL_REQUIRED, c.EXIT_DAEMON_UNAVAILABLE):
        return
    from mesh.daemon.server import _idempotency_record, _store_idempotency_row

    envelope = {
        "ok": True,
        "data": res.data,
        "warnings": res.warnings,
        "request_id": request_id,
        "idempotent_replay": False,
        "dry_run": False,
        "exit_code": res.exit_code,
    }
    _store_idempotency_row(
        conn,
        idem["principal_id"],
        idem["key"],
        cmd,
        idem["request_hash"],
        json.dumps(_idempotency_record(cmd, envelope)),
        res.exit_code,
        ignore_conflict=True,
    )


async def run_workspace_prune(server: Any, ctx: HandlerContext) -> HandlerResult:
    """3-phase atomic reservation flow for ``workspace.prune`` (TX1 reserve -> worker -> TX2 finalize)."""
    conn = ctx.conn
    idem = getattr(ctx, "idempotency", None)
    project_id = ctx.args.get("project")
    res_key = ws_prune_project_reservation_key(project_id) if project_id else WS_PRUNE_ALL_KEY
    ckpt_path = os.path.join(ctx.home, "run", f"ws_prune_{secrets.token_hex(12)}.json")
    ckpt_written = False

    # TX1: Validate, snapshot candidate list, write checkpoint + meta reservation atomically.
    conn.execute("BEGIN IMMEDIATE;")
    try:
        from mesh.daemon.handlers.inbox import recheck_tx_credential_and_authz

        recheck_tx_credential_and_authz(ctx)
        replay = _check_tx1_idempotency(conn, idem, ctx.cmd, ctx=ctx)
        if replay is not None:
            conn.execute("ROLLBACK;")
            return replay
        plan = plan_workspace_prune(ctx)
        _write_ws_prune_checkpoint(
            ckpt_path,
            {
                "root": plan["root"],
                "completed": False,
                "removed": [],
                "skipped": [],
                "in_flight": None,
                "active_planned_ids": None,
            },
        )
        ckpt_written = True
        plan["checkpoint_path"] = ckpt_path
        plan["home"] = ctx.home
        plan["testing_mode"] = ctx.testing_mode
        projects_list = sorted({item["task"]["project_id"] for item in plan["items"]})
        if project_id and project_id not in projects_list:
            projects_list = sorted([*projects_list, project_id])
        task_ids = [item["task"]["id"] for item in plan["items"]]
        planned_list = [
            {
                "task_id": item["task"]["id"],
                "project_id": item["task"]["project_id"],
                "status": item["task"]["status"],
                "path": item["task"]["workspace_path"],
                "branch": item["task"]["branch"],
                "repo_path": item["repo_path"],
                "integration_branch": item["integration_branch"],
                "matched_root": item.get("matched_root"),
            }
            for item in plan["items"]
        ]
        idem_doc = (
            {
                "principal_id": idem["principal_id"],
                "key": idem["key"],
                "cmd": ctx.cmd,
                "request_hash": idem["request_hash"],
            }
            if idem and idem.get("key")
            else None
        )
        res_doc = {
            "kind": "prune",
            "project_id": project_id,
            "projects": projects_list,
            "task_ids": task_ids,
            "planned": planned_list,
            "checkpoint_path": ckpt_path,
            "requested_by": ctx.principal["id"],
            "actor_kind": ctx.actor_kind,
            "via": ctx.via,
            "relayed_by": ctx.relayed_by,
            "credential_id": ctx.credential_id,
            "request_id": ctx.request_id,
            "created_at": now_iso_utc(),
            "idempotency": idem_doc,
        }
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?);",
            (res_key, json.dumps(res_doc, sort_keys=True)),
        )
        conn.execute("COMMIT;")
    except Exception:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
        except Exception:
            pass
        if ckpt_written:
            _unlink_best_effort(ckpt_path)
        raise

    _maybe_ws_crash(ctx.home, "after_ws_prune_reserve", ctx.testing_mode)

    async def _phase2_and_tx2() -> HandlerResult:
        try:
            result = await asyncio.to_thread(execute_workspace_prune_plan, plan)
        except BaseException:
            ckpt_doc = _read_ws_prune_checkpoint(ckpt_path) or {}
            try:
                allowed_roots = _allowed_workspace_roots(conn, ctx.home)
                default_root = get_workspaces_root(conn, ctx.home)
                proj_meta = _collect_prune_project_meta(conn, res_doc, ckpt_doc)
                rec = await asyncio.to_thread(
                    reconcile_prune_checkpoint_from_data,
                    allowed_roots,
                    default_root,
                    proj_meta,
                    res_doc,
                    ckpt_doc,
                )
            except BaseException:
                rec = {
                    "removed": [
                        e for e in (ckpt_doc.get("removed") or [])
                        if isinstance(e, dict) and e.get("task_id")
                    ]
                }
            conn.execute("BEGIN IMMEDIATE;")
            try:
                for entry in rec["removed"]:
                    if not isinstance(entry, dict) or not entry.get("task_id"):
                        continue
                    if ctx.request_id and conn.execute(
                        "SELECT 1 FROM events WHERE type = 'workspace.pruned' AND task_id = ? AND request_id = ? LIMIT 1;",
                        (entry["task_id"], ctx.request_id),
                    ).fetchone():
                        continue
                    record_event(
                        conn=conn,
                        type_="workspace.pruned",
                        actor_id="system",
                        actor_kind="system",
                        via="system",
                        project_id=entry.get("project_id"),
                        task_id=entry["task_id"],
                        payload={"task_id": entry["task_id"], "path": entry.get("path"), "branch": entry.get("branch")},
                        request_id=ctx.request_id,
                    )
                conn.execute("DELETE FROM meta WHERE key = ?;", (res_key,))
                conn.execute("COMMIT;")
            except Exception:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK;")
                except Exception:
                    pass
            _unlink_best_effort(ckpt_path)
            raise

        _maybe_ws_crash(ctx.home, "after_ws_prune", ctx.testing_mode)

        removed: List[Dict[str, Any]] = result["removed"]
        skipped: List[Dict[str, Any]] = result["skipped"]
        root = result["root"]

        conn.execute("BEGIN IMMEDIATE;")
        try:
            try:
                from mesh.daemon.handlers.inbox import recheck_tx_credential_and_authz

                recheck_tx_credential_and_authz(ctx)
                check_authz(
                    conn,
                    "workspace.prune",
                    ctx.principal,
                    ctx.actor_kind,
                    ctx.via,
                    project_id=project_id,
                )
            except MeshError:
                for entry in removed:
                    record_event(
                        conn=conn,
                        type_="workspace.pruned",
                        actor_id="system",
                        actor_kind="system",
                        via="system",
                        project_id=entry["project_id"],
                        task_id=entry["task_id"],
                        payload={"task_id": entry["task_id"], "path": entry["path"], "branch": entry["branch"]},
                        request_id=ctx.request_id,
                    )
                conn.execute("DELETE FROM meta WHERE key = ?;", (res_key,))
                conn.execute("COMMIT;")
                ctx._committed_side_effect = True  # type: ignore[attr-defined]
                _unlink_best_effort(ckpt_path)
                raise

            for entry in removed:
                record_event(
                    conn=conn,
                    type_="workspace.pruned",
                    actor_id=ctx.principal["id"],
                    actor_kind=ctx.actor_kind,
                    via=ctx.via,
                    relayed_by=ctx.relayed_by,
                    credential_id=ctx.credential_id,
                    project_id=entry["project_id"],
                    task_id=entry["task_id"],
                    payload={"task_id": entry["task_id"], "path": entry["path"], "branch": entry["branch"]},
                    request_id=ctx.request_id,
                    k_user=ctx.k_user,
                )
            transitions = [
                {
                    "task_id": e["task_id"],
                    "project_id": e["project_id"],
                    "path": e["path"],
                    "branch": e["branch"],
                    "action": "prune",
                }
                for e in removed
            ]
            events = ["workspace.pruned" for _ in removed]
            res = HandlerResult(
                data=redact_value({"root": root, "dry_run": False, "removed": removed, "skipped": skipped}),
                transitions=transitions,
                events=events,
            )
            conn.execute("DELETE FROM meta WHERE key = ?;", (res_key,))
            _store_tx_idempotency(conn, idem, ctx.cmd, ctx.request_id, res)
            conn.execute("COMMIT;")
        except Exception:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        _unlink_best_effort(ckpt_path)
        return res

    job = asyncio.ensure_future(_phase2_and_tx2())
    track = getattr(server, "track_merge_job", None)
    if callable(track):
        track(job)
    return await asyncio.shield(job)


def _path_confirmed_absent(path: str) -> bool:
    """True only when ``lstat(path)`` fails with ``ENOENT`` (review PR45 R5 P2).

    ``os.path.isdir``/``lexists`` swallow ``EACCES``: with ``tasks/`` at ``0o000`` an
    intact workspace looked deleted. Any other error means "unknown", not "gone".
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _ws_delete_marker_path(home: str, task_id: str) -> str:
    return os.path.join(home, "run", f"ws_del_{task_id}_{secrets.token_hex(8)}.json")


def _write_ws_delete_marker(path: Optional[str]) -> None:
    """Durably record that rmtree is about to start (startup recovery requires it)."""
    if not path:
        return
    _write_ws_prune_checkpoint(path, {"rmtree_started": True})


def _ws_delete_marker_started(path: Any) -> bool:
    if not isinstance(path, str) or not path:
        return False
    doc = _read_ws_prune_checkpoint(path)
    return bool(isinstance(doc, dict) and doc.get("rmtree_started") is True)


def _validate_delete_target_ancestors(
    stripped: str,
    real: str,
    matched_root: str,
    project_id: str,
    task_id: str,
) -> Optional[str]:
    """Validate ``tasks`` dir exists without symlinks and ancestors resolve under ``matched_root``."""
    if not isinstance(stripped, str) or not stripped.strip():
        return None
    tasks_dir = os.path.dirname(_plat_paths.strip_trailing_seps(stripped))
    if _plat_paths.is_reparse_link(tasks_dir) or not os.path.isdir(tasks_dir):
        return None
    verified_real = _verify_workspace_ancestors_not_symlinked(
        stripped, matched_root, project_id, task_id
    )
    if verified_real is None or not _plat_paths.same_path(verified_real, real):
        return None
    return verified_real


def _delete_workspace_dir_sync(*args: Any, **kwargs: Any) -> bool:
    """`_delete_workspace_dir_impl`, then drop the task's cached workspace preparation:
    it saw the old worktree (full review r3 P1-3: the live `workspace delete` path, not
    only the dry-run handler, so the scheduler tick right after never spawns from it)."""
    task_id = args[4] if len(args) > 4 else kwargs.get("task_id")
    try:
        return _delete_workspace_dir_impl(*args, **kwargs)
    finally:
        if task_id:
            from mesh.daemon import supervisor

            supervisor.forget_ws_prep(task_id)


def _delete_workspace_dir_impl(
    stripped: str,
    real: str,
    matched_root: str,
    project_id: str,
    task_id: str,
    repo_path: Optional[str],
    *,
    state: Optional[Dict[str, bool]] = None,
    marker_path: Optional[str] = None,
) -> bool:
    """Re-verify workspace ancestors have not become symlinks, remove dir, prune worktrees.

    Runs outside any SQLite transaction (and via ``asyncio.to_thread`` in server handlers).
    Raises MeshError on symlink traversal or OSError if removal fails. The target is
    probed with ``lstat`` after owned ancestors (``tasks/``) regain their search bit,
    so ``EACCES`` raises instead of reading as "already gone"; ``marker_path`` is
    written durably right before rmtree so startup recovery can tell a started
    delete from one that never ran (review PR45 R5 P2).
    """
    import stat as _stat

    from mesh.core.gitops import run_git

    verified_real = _validate_delete_target_ancestors(
        stripped, real, matched_root, project_id, task_id
    )
    if verified_real is None:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Workspace path of task '{task_id}' is outside the workspaces root",
            details={"task_id": task_id, "path": stripped, "reason": "outside_workspaces_root"},
        )
    root_real = os.path.realpath(_plat_paths.strip_trailing_seps(matched_root))
    make_owned_ancestors_writable(verified_real, root_real)
    if sys.platform == "win32":
        # Windows SeChangeNotifyPrivilege allows lstat/deletes under a D:P parent dir;
        # require readable/writable parent dir DACL before writing marker or running rmtree.
        parent_dir = os.path.dirname(verified_real)
        os.listdir(parent_dir)
        if os.name == "nt" and not _plat_perms._win_dir_has_owner_rw_acl(parent_dir):
            raise PermissionError(errno.EACCES, "Parent directory is inaccessible", parent_dir)
    try:
        st = os.lstat(verified_real)
    except FileNotFoundError:
        return False
    if _plat_paths.is_reparse_stat(st) or not _stat.S_ISDIR(st.st_mode):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Workspace path of task '{task_id}' is not a directory",
            details={"task_id": task_id, "path": stripped, "reason": "not_a_directory"},
        )
    _write_ws_delete_marker(marker_path)
    if state is not None:
        state["rmtree_started"] = True
    rmtree_allow_readonly(verified_real, boundary_real=root_real)
    if repo_path and os.path.isdir(repo_path):
        try:
            run_git(["worktree", "prune"], cwd=repo_path)
        except Exception:
            pass
    return True


def _worktree_prune_quiet(repo_path: str) -> None:
    from mesh.core.gitops import run_git

    try:
        run_git(["worktree", "prune"], cwd=repo_path)
    except Exception:
        pass


def _validate_workspace_delete(
    ctx: HandlerContext,
    consume_approval: bool,
    *,
    create_approval_if_missing: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """Validate authz, reservation, approval, leases, and workspace path for ``workspace.delete``.

    When ``create_approval_if_missing`` is False (default when ``consume_approval`` is False)
    and an agent caller did not supply ``ctx.approval``, returns None so pre-TX helpers can
    defer creating the pending approval until inside ``BEGIN IMMEDIATE``.
    """
    if create_approval_if_missing is None:
        create_approval_if_missing = consume_approval

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

    res_key = has_task_ws_reservation(ctx.conn, task_id, project_id)
    if res_key:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot delete workspace of task '{task_id}': workspace operation '{res_key}' is in progress",
            details={"reservation": res_key, "task_id": task_id, "project_id": project_id},
        )

    if ctx.actor_kind != "user":
        params = {"task_id": task_id}
        if not ctx.approval:
            if not create_approval_if_missing:
                return None
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
                testing_mode=ctx.testing_mode,
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
            consume=consume_approval,
            actor_kind=ctx.actor_kind, via=ctx.via, k_user=ctx.k_user,
        )

    if (task["status"] == "merging" or ctx.conn.execute(
        "SELECT 1 FROM leases WHERE key = ?;", (f"merge:{task['project_id']}",)
    ).fetchone() or _repo_merge_busy(ctx.conn, task["project_id"])):
        # R1 P2-3 (P2-7): also refuse while a merge on the same repo (sibling project) runs.
        # Full review P1: never remove a worktree while the project's merge runs.
        raise MeshError(
            code=c.RUN_STILL_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot delete the workspace of task '{task_id}': a merge is in progress",
            details={"task_id": task_id, "reason": "merge_in_progress"},
        )
    if ctx.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)).fetchone():
        raise MeshError(
            code=c.RUN_STILL_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot delete the workspace of task '{task_id}': a run still holds its lease",
            details={"task_id": task_id},
        )

    path = task["workspace_path"]
    real: Optional[str] = None
    stripped: Optional[str] = None
    matched_root: Optional[str] = None
    if path:
        match = _task_workspace_match(ctx.conn, ctx.home, project_id, task_id, path)
        if match is None:
            # A path that resolves outside `<workspaces.root>/<project_id>/tasks/<task_id>`
            # (or traverses a symlink) is refused: nothing is deleted and no event is recorded.
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Workspace path of task '{task_id}' is outside the workspaces root",
                details={"task_id": task_id, "path": path, "reason": "outside_workspaces_root"},
            )
        stripped, real, matched_root = match
        # Review PR45 R5 (second review) P2: the "nothing on disk" short-circuit must
        # not trust a moved/renamed ``tasks`` parent or a non-directory target. The
        # ``tasks`` dir has to exist (no symlinks on the way), and an existing target
        # must be a directory; otherwise refuse without consuming the approval.
        if _validate_delete_target_ancestors(stripped, real, matched_root, project_id, task_id) is None:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Workspace path of task '{task_id}' is outside the workspaces root",
                details={"task_id": task_id, "path": path, "reason": "outside_workspaces_root"},
            )
        try:
            st_target = os.lstat(real)
        except FileNotFoundError:
            st_target = None
        except OSError:
            st_target = None
            if not ctx.dry_run:  # a dry run never changes permissions; the real run re-checks
                make_owned_ancestors_writable(real, os.path.realpath(_plat_paths.strip_trailing_seps(matched_root)))
            try:
                st_target = os.lstat(real) if not ctx.dry_run else None
            except FileNotFoundError:
                st_target = None
            except OSError as exc:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Workspace path of task '{task_id}' cannot be inspected",
                    details={"task_id": task_id, "path": path, "reason": "unreadable", "errno": exc.errno},
                )
        if st_target is not None and (
            _plat_paths.is_reparse_stat(st_target) or not stat.S_ISDIR(st_target.st_mode)
        ):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Workspace path of task '{task_id}' is not a directory",
                details={"task_id": task_id, "path": path, "reason": "not_a_directory"},
            )
    repo_row = ctx.conn.execute("SELECT repo_path FROM projects WHERE id = ?;", (project_id,)).fetchone()
    repo_path = repo_row["repo_path"] if repo_row is not None else None
    return {
        "task_id": task_id,
        "project_id": project_id,
        "branch": task["branch"],
        "path": path,
        "stripped": stripped,
        "real": real,
        "matched_root": matched_root,
        "repo_path": repo_path,
    }


async def prepare_workspace_delete(ctx: HandlerContext) -> None:
    """Validate and delete task workspace off the event loop and before ``BEGIN IMMEDIATE``."""
    if ctx.dry_run:
        return
    plan = _validate_workspace_delete(ctx, consume_approval=False, create_approval_if_missing=False)
    if plan is None:
        return
    deleted = False
    if plan["stripped"] and plan["real"] and plan["matched_root"]:
        deleted = await asyncio.to_thread(
            _delete_workspace_dir_sync,
            plan["stripped"],
            plan["real"],
            plan["matched_root"],
            plan["project_id"],
            plan["task_id"],
            plan["repo_path"],
        )
    ctx.prepared_delete = {  # type: ignore[attr-defined]
        "deleted": deleted,
        "real": plan["real"],
        "path": plan["path"],
    }


async def run_workspace_delete(server: Any, ctx: HandlerContext) -> HandlerResult:
    """3-phase atomic reservation flow for ``workspace.delete`` (TX1 reserve -> worker -> TX2 finalize)."""
    conn = ctx.conn
    idem = getattr(ctx, "idempotency", None)

    # TX1: Re-check idempotency, validate, create pending approval (rc 10) or verify approved, reserve atomically.
    conn.execute("BEGIN IMMEDIATE;")
    try:
        from mesh.daemon.handlers.inbox import recheck_tx_credential_and_authz

        recheck_tx_credential_and_authz(ctx)
        replay = _check_tx1_idempotency(conn, idem, ctx.cmd, ctx=ctx)
        if replay is not None:
            conn.execute("ROLLBACK;")
            return replay

        plan = _validate_workspace_delete(ctx, consume_approval=False, create_approval_if_missing=True)
        assert plan is not None
        task_id = plan["task_id"]
        project_id = plan["project_id"]
        path = plan["path"]
        real = plan["real"]
        stripped = plan["stripped"]
        matched_root = plan["matched_root"]
        branch = plan["branch"]

        needs_disk_delete = bool(stripped and real and matched_root and os.path.isdir(real))
        if not needs_disk_delete:
            if ctx.actor_kind != "user" and ctx.approval:
                from mesh.core.approvals import check_and_consume_approval

                check_and_consume_approval(
                    conn=conn,
                    approval_id=ctx.approval,
                    op="workspace.delete",
                    category="delete",
                    caller_id=ctx.principal["id"],
                    params={"task_id": task_id},
                    project_id=project_id,
                    target_id=task_id,
                    request_id=ctx.request_id,
                    consume=True,
                    actor_kind=ctx.actor_kind,
                    via=ctx.via,
                    k_user=ctx.k_user,
                )
            record_event(
                conn=conn,
                type_="workspace.deleted",
                actor_id=ctx.principal["id"],
                actor_kind=ctx.actor_kind,
                via=ctx.via,
                relayed_by=ctx.relayed_by,
                credential_id=ctx.credential_id,
                project_id=project_id,
                task_id=task_id,
                payload={"task_id": task_id, "path": path, "branch": branch, "branch_kept": True},
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )
            res = HandlerResult(
                data=redact_value({"task_id": task_id, "path": path, "deleted": False,
                                   "branch": branch, "branch_kept": True}),
                transitions=[],
                events=["workspace.deleted"],
            )
            _store_tx_idempotency(conn, idem, ctx.cmd, ctx.request_id, res)
            conn.execute("COMMIT;")
            # The directory was already gone: still drop git's stale worktree record
            # (outside the transaction, off the event loop; prune is idempotent).
            repo_path = plan["repo_path"]
            if path and repo_path and os.path.isdir(repo_path):
                await asyncio.to_thread(_worktree_prune_quiet, repo_path)
            return res

        res_key = ws_delete_reservation_key(task_id)
        idem_doc = (
            {
                "principal_id": idem["principal_id"],
                "key": idem["key"],
                "cmd": ctx.cmd,
                "request_hash": idem["request_hash"],
            }
            if idem and idem.get("key")
            else None
        )
        marker_path = _ws_delete_marker_path(ctx.home, task_id)
        res_doc = {
            "kind": "delete",
            "rmtree_marker": marker_path,
            "task_id": task_id,
            "project_id": project_id,
            "path": path,
            "stripped": stripped,
            "real": real,
            "matched_root": matched_root,
            "branch": branch,
            "repo_path": plan["repo_path"],
            "approval_id": ctx.approval if ctx.actor_kind != "user" else None,
            "requested_by": ctx.principal["id"],
            "actor_kind": ctx.actor_kind,
            "via": ctx.via,
            "relayed_by": ctx.relayed_by,
            "credential_id": ctx.credential_id,
            "request_id": ctx.request_id,
            "created_at": now_iso_utc(),
            "idempotency": idem_doc,
        }
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?);",
            (res_key, json.dumps(res_doc, sort_keys=True)),
        )
        conn.execute("COMMIT;")
    except MeshError as exc:
        try:
            if conn.in_transaction:
                if exc.exit_code == c.EXIT_APPROVAL_REQUIRED and not ctx.dry_run:
                    conn.execute("COMMIT;")
                else:
                    conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    except Exception:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise

    _maybe_ws_crash(ctx.home, "after_ws_reserve", ctx.testing_mode)

    del_state: Dict[str, bool] = {"rmtree_started": False}

    def _do_delete_sync() -> bool:
        with _task_lock(task_id):
            return _delete_workspace_dir_sync(
                stripped,
                real,
                matched_root,
                project_id,
                task_id,
                plan["repo_path"],
                state=del_state,
                marker_path=marker_path,
            )

    async def _phase2_and_tx2() -> HandlerResult:
        try:
            deleted = await asyncio.to_thread(_do_delete_sync)
        except BaseException:
            ancestors_ok = (
                _validate_delete_target_ancestors(
                    stripped, real, matched_root, project_id, task_id
                )
                is not None
            )
            check_paths = [
                _plat_paths.strip_trailing_seps(p)
                for p in (real, stripped, path)
                if isinstance(p, str) and p.strip()
            ]
            all_absent = bool(check_paths) and all(_path_confirmed_absent(p) for p in check_paths)
            confirmed_deleted = bool(
                del_state.get("rmtree_started")
                and _ws_delete_marker_started(marker_path)
                and ancestors_ok
                and all_absent
            )
            if confirmed_deleted and plan["repo_path"] and os.path.isdir(plan["repo_path"]):
                from mesh.core.gitops import run_git

                def _prune_worktrees_best_effort() -> None:
                    try:
                        run_git(["worktree", "prune"], cwd=plan["repo_path"])
                    except Exception:
                        pass

                try:
                    await asyncio.to_thread(_prune_worktrees_best_effort)
                except BaseException:
                    pass
            conn.execute("BEGIN IMMEDIATE;")
            try:
                if confirmed_deleted:
                    now = now_iso_utc()
                    if ctx.actor_kind != "user" and ctx.approval:
                        from mesh.core.approvals import record_consumed_event

                        changed = conn.execute(
                            """UPDATE approvals SET
                                status = 'consumed',
                                consumed_at = COALESCE(consumed_at, ?),
                                consumed_by = COALESCE(consumed_by, ?),
                                consumed_request_id = COALESCE(consumed_request_id, ?)
                            WHERE id = ? AND status = 'approved';""",
                            (now, ctx.principal["id"] or "system", ctx.request_id, ctx.approval),
                        ).rowcount
                        if changed:  # R1 P1-2: audit verify needs the consumption event
                            arow = conn.execute("SELECT * FROM approvals WHERE id = ?;",
                                                (ctx.approval,)).fetchone()
                            record_consumed_event(conn, arow, ctx.principal["id"] or "system",
                                                  ctx.actor_kind, ctx.via, ctx.request_id, None)
                    record_event(
                        conn=conn,
                        type_="workspace.deleted",
                        actor_id="system",
                        actor_kind="system",
                        via="system",
                        project_id=project_id,
                        task_id=task_id,
                        payload={"task_id": task_id, "path": path, "branch": branch, "branch_kept": True},
                        request_id=ctx.request_id,
                    )
                conn.execute("DELETE FROM meta WHERE key = ?;", (res_key,))
                conn.execute("COMMIT;")
                ctx._committed_side_effect = True  # type: ignore[attr-defined]
                _unlink_best_effort(marker_path)
            except Exception:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK;")
                except Exception:
                    pass
            raise

        _maybe_ws_crash(ctx.home, "after_ws_delete", ctx.testing_mode)

        conn.execute("BEGIN IMMEDIATE;")
        try:
            try:
                from mesh.daemon.handlers.inbox import recheck_tx_credential_and_authz

                recheck_tx_credential_and_authz(ctx)
                check_authz(
                    conn,
                    "workspace.delete",
                    ctx.principal,
                    ctx.actor_kind,
                    ctx.via,
                    project_id=project_id,
                    task_id=task_id,
                )
            except MeshError:
                if deleted:
                    if ctx.actor_kind != "user" and ctx.approval:
                        from mesh.core.approvals import check_and_consume_approval

                        try:
                            check_and_consume_approval(
                                conn=conn,
                                approval_id=ctx.approval,
                                op="workspace.delete",
                                category="delete",
                                caller_id=ctx.principal["id"],
                                params={"task_id": task_id},
                                project_id=project_id,
                                target_id=task_id,
                                request_id=ctx.request_id,
                                consume=True,
                                actor_kind=ctx.actor_kind,
                                via=ctx.via,
                                k_user=ctx.k_user,
                                allow_expired_if_reserved=True,
                            )
                        except MeshError:
                            pass
                    record_event(
                        conn=conn,
                        type_="workspace.deleted",
                        actor_id="system",
                        actor_kind="system",
                        via="system",
                        project_id=project_id,
                        task_id=task_id,
                        payload={"task_id": task_id, "path": path, "branch": branch, "branch_kept": True},
                        request_id=ctx.request_id,
                    )
                conn.execute("DELETE FROM meta WHERE key = ?;", (res_key,))
                conn.execute("COMMIT;")
                ctx._committed_side_effect = True  # type: ignore[attr-defined]
                _unlink_best_effort(marker_path)
                raise

            if ctx.actor_kind != "user":
                from mesh.core.approvals import check_and_consume_approval

                check_and_consume_approval(
                    conn=conn,
                    approval_id=ctx.approval or "",
                    op="workspace.delete",
                    category="delete",
                    caller_id=ctx.principal["id"],
                    params={"task_id": task_id},
                    project_id=project_id,
                    target_id=task_id,
                    request_id=ctx.request_id,
                    consume=True,
                    actor_kind=ctx.actor_kind,
                    via=ctx.via,
                    k_user=ctx.k_user,
                    allow_expired_if_reserved=True,
                )

            transitions = (
                [{"task_id": task_id, "project_id": project_id, "path": real, "branch": branch, "action": "delete"}]
                if deleted
                else []
            )
            events = ["workspace.deleted"]
            record_event(
                conn=conn,
                type_="workspace.deleted",
                actor_id=ctx.principal["id"],
                actor_kind=ctx.actor_kind,
                via=ctx.via,
                relayed_by=ctx.relayed_by,
                credential_id=ctx.credential_id,
                project_id=project_id,
                task_id=task_id,
                payload={"task_id": task_id, "path": path, "branch": branch, "branch_kept": True},
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )
            res = HandlerResult(
                data=redact_value({"task_id": task_id, "path": path, "deleted": deleted,
                                   "branch": branch, "branch_kept": True}),
                transitions=transitions,
                events=events,
            )
            conn.execute("DELETE FROM meta WHERE key = ?;", (res_key,))
            _store_tx_idempotency(conn, idem, ctx.cmd, ctx.request_id, res)
            conn.execute("COMMIT;")
            _unlink_best_effort(marker_path)
            return res
        except Exception:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise

    job = asyncio.ensure_future(_phase2_and_tx2())
    track = getattr(server, "track_merge_job", None)
    if callable(track):
        track(job)
    return await asyncio.shield(job)


def handle_workspace_delete(ctx: HandlerContext) -> HandlerResult:
    """`workspace delete <tsk>` (§10.7): gated by `delete` for agents; the user acts directly.

    Removes the task directory (after a realpath containment check under
    `workspaces.root`), runs `worktree prune`, and **keeps the branch** (Q7): deleting
    unmerged work is a user decision, and the branch is what holds it.
    """
    plan = _validate_workspace_delete(ctx, consume_approval=True, create_approval_if_missing=True)
    assert plan is not None
    task_id = plan["task_id"]
    project_id = plan["project_id"]
    path = plan["path"]
    real = plan["real"]
    branch = plan["branch"]

    if ctx.dry_run:
        exists_on_disk = bool(real and os.path.isdir(real))
        transitions = (
            [{"task_id": task_id, "project_id": project_id, "path": real, "branch": branch, "action": "delete"}]
            if exists_on_disk
            else []
        )
        return HandlerResult(
            data={"task_id": task_id, "path": real or path, "deleted": False, "dry_run": True,
                  "branch": branch},
            transitions=transitions,
            events=["workspace.deleted"],
        )

    prepared = getattr(ctx, "prepared_delete", None)
    if prepared is not None:
        deleted = bool(prepared["deleted"])
    elif plan["stripped"] and real and plan["matched_root"]:
        deleted = _delete_workspace_dir_sync(
            plan["stripped"],
            real,
            plan["matched_root"],
            project_id,
            task_id,
            plan["repo_path"],
        )
    else:
        deleted = False

    from mesh.daemon import supervisor  # noqa: PLC0415 - avoids an import cycle
    supervisor.forget_ws_prep(task_id)  # r2 P1-C: a cached preparation saw the old worktree
    transitions = (
        [{"task_id": task_id, "project_id": project_id, "path": real, "branch": branch, "action": "delete"}]
        if deleted
        else []
    )
    events = ["workspace.deleted"]
    record_event(
        conn=ctx.conn,
        type_="workspace.deleted",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        payload={"task_id": task_id, "path": path, "branch": branch, "branch_kept": True},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    return HandlerResult(
        data=redact_value({"task_id": task_id, "path": path, "deleted": deleted,
                           "branch": branch, "branch_kept": True}),
        transitions=transitions,
        events=events,
    )


def handle_run_release(ctx: HandlerContext) -> HandlerResult:
    """`mesh run release <run-id> --force-seen-unusable` (r4c, N18 operator path).

    Only for a task run or launch-mode lead turn held because its seen state
    (``seen.json`` / ``adopt_pending``) is unusable: the run closes as ``lost``,
    its lease is released, and its task takes the usual lost-run transition (or a
    lead turn returns its carried outbox wake messages to ``pending`` with backoff).
    No process is signalled. The ordinary rules stay: nothing is ever killed by pgid
    alone and a hold is never released on a timeout; this is the explicit operator
    decision. User only (never an agent or a run token).
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
    from mesh.daemon import lead_turns, supervisor

    if not supervisor.seen_held(run, ctx.home):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Run '{run_id}' is not held because of unusable seen state",
            details={"run_id": run_id, "status": run["status"], "reason": "not_seen_held"},
        )
    reason = supervisor._SEEN_REASON.get(run_id) or "unreadable"
    actor = ctx.principal.get("id")
    payload = {"run_id": run_id, "actor": actor, "reason": reason}
    task = (
        ctx.conn.execute("SELECT * FROM tasks WHERE id = ?;", (run["task_id"],)).fetchone()
        if run["task_id"]
        else None
    )
    transitions: List[Dict[str, Any]] = [
        {"entity": "run", "id": run_id, "from": run["status"], "to": "lost"}
    ]
    events = ["run.seen_release_forced"]
    if task is not None:
        if task["status"] == "cancelling":
            transitions.append(
                {"entity": "task", "id": task["id"], "from": "cancelling", "to": "cancelled"}
            )
            events.append("task.cancelled")
        elif task["status"] == "running":
            transitions.append(
                {"entity": "task", "id": task["id"], "from": "running", "to": "failed"}
            )
            events.append("task.failed")
    if ctx.dry_run:
        ctx.effects.append(Effect(kind="seen_release_forced",
                                  params={"run_id": run_id, "reason": reason, "actor": actor}))
        return HandlerResult(
            data={"run_id": run_id, "released": False, "dry_run": True, "reason": reason},
            transitions=transitions,
            events=events,
        )
    pending = supervisor._pending_failure(run) if run["kind"] == "task" else None
    close_result: Dict[str, Any] = {"reason": "seen_release_forced", "seen_reason": reason, "actor": actor}
    if run["kind"] == "lead_turn":
        close_result["messages"] = lead_turns._release_messages(
            ctx.conn, run_id, False, run["project_id"], ctx.testing_mode
        )
    supervisor._close_run(ctx.conn, run, "lost", "lost_on_restart", None, close_result,
                          gate=False, testing_mode=ctx.testing_mode)  # operator override of the close-safety gate (round 6)
    if task is not None:
        if task["status"] == "cancelling":
            supervisor._task_cancelled(ctx.conn, task, run, "cancelled")
        elif task["status"] == "running":
            if pending:
                supervisor._task_failed(
                    ctx.conn, task, run, "reported", {"message": pending.get("message", "")}, reporter=pending
                )
            else:
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
    return HandlerResult(
        data={"run_id": run_id, "released": True, "status": "lost", "reason": reason},
        transitions=transitions,
        events=events,
    )


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
    from mesh.daemon.merge import is_merge_run_active, merge_lease_key

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
    if is_merge_run_active(run_id):
        # R1 P1-1: between TX1 and the test wrapper (and after it exits) no wrapper is
        # alive, but the pipeline still owns the integration tree and the lease.
        raise MeshError(
            code=c.RUN_STILL_ACTIVE, exit_code=c.EXIT_CONFLICT,
            message=f"Run '{run_id}': its merge pipeline is still running; wait for it to finish",
            details={"run_id": run_id, "status": run["status"], "reason": "merge_in_progress"},
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
    task_id = run["task_id"]
    needs_reconcile = bool(
        task_id
        and ctx.conn.execute("SELECT 1 FROM tasks WHERE id = ? AND status = 'merging';", (task_id,)).fetchone()
    )
    if needs_reconcile:
        # Keep merge:<prj> held until reconcile_merging_task completes (P1-5).
        released = bool(
            ctx.conn.execute("SELECT 1 FROM leases WHERE key = ? AND run_id = ?;", (key, run_id)).fetchone()
        )
    else:
        released = bool(
            ctx.conn.execute("DELETE FROM leases WHERE key = ? AND run_id = ?;", (key, run_id)).rowcount
        )
    record_event(
        conn=ctx.conn,
        type_="run.merge_release_forced",
        actor_id=actor,
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=run["project_id"],
        task_id=task_id,
        run_id=run_id,
        payload={"run_id": run_id, "task_id": task_id, "actor": actor, "lease": key,
                 "lease_released": released, "previous_status": run["status"]},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    if task_id:
        ctx.effects.append(
            Effect(
                kind="reconcile_merging_task",
                params={"task_id": task_id, "lease_key": key, "run_id": run_id},
            )
        )
    if sys.platform == "win32":
        from mesh.daemon import supervisor

        supervisor.close_run_job(run_id)
    return HandlerResult(data={"run_id": run_id, "released": True, "status": "lost", "lease": key,
                               "lease_released": released, "task_id": task_id})
