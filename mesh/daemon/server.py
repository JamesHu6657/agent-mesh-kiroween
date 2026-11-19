"""Asynchronous Unix-socket server, dispatch, auth, and single-instance daemon (meshd)."""

import asyncio
import ctypes
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import socket
import sys
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Set, Tuple

import mesh.constants as c
from mesh.core.logfile import append_line, escape_field
from mesh.errors import DbIntegrityError, MeshError, SchemaTooNewError
from mesh.core.auth import (
    classify_peer_run_session,
    derive_user_keys,
    get_peer_credentials,
    parse_token_lookup,
    verify_token_hash,
    parse_kdf_params,
    verify_user_passphrase,
)
from mesh.core.authz import ADAPTER_CMDS, ROSTER_READ_CMDS, UNAUTH_CMDS, check_authz
from mesh.core.canonical import canonical_json
from mesh.core.clock import now_iso_utc, now_monotonic
from mesh.core.db import connect_db, parse_schema_version
from mesh.core.events import insert_event
from mesh.core.ids import new_id
from mesh.core.lead_actions import IDEMPOTENCY_KEY_RE
from mesh.core.leases import task_start_replay_matches
from mesh.core.paths import (
    check_home_permissions,
    check_install_path_secure,
    check_socket_path_length,
    get_install_root,
    refuse_testing_on_default_home,
    resolve_home,
    rmtree_allow_readonly,
)
from mesh.core.schema import CURRENT_SCHEMA_VERSION
from mesh.daemon.handlers.base import Effect, HandlerContext, HandlerResult
from mesh.daemon.handlers.top import handle_version, handle_whoami
from mesh.daemon.handlers.daemon import handle_daemon_status, handle_daemon_stop
from mesh.daemon.handlers.config import handle_config_get, handle_config_list, handle_config_set
from mesh.daemon.handlers.agent import (
    handle_agent_create,
    handle_agent_disable,
    handle_agent_enable,
    handle_agent_list,
    handle_agent_show,
    handle_agent_token_issue,
    handle_agent_token_list,
    handle_agent_token_revoke,
    handle_agent_update,
)
from mesh.daemon.handlers.adapter import (
    handle_adapter_list,
    handle_adapter_models,
    handle_adapter_probe,
    handle_adapter_show,
    prepare_adapter_command,
)
from mesh.daemon.handlers.project import (
    handle_intake_submit,
    handle_project_cancel,
    handle_project_complete,
    handle_project_create,
    handle_project_deliver,
    handle_project_leads,
    handle_project_list,
    handle_project_note,
    handle_project_pause,
    handle_project_resume,
    handle_project_set_lead,
    handle_project_show,
    handle_project_update,
    handle_project_wait,
)
from mesh.daemon.handlers.inbox import (
    handle_inbox_ack,
    handle_inbox_list,
    handle_inbox_wait,
    recheck_tx_credential_and_authz,
)
from mesh.daemon.handlers.event import handle_event_list, handle_event_tail_live
from mesh.daemon.handlers.approval import (
    check_approval_replay_authz,
    handle_approval_approve,
    handle_approval_cancel,
    handle_approval_check,
    handle_approval_consume,
    handle_approval_list,
    handle_approval_reject,
    handle_approval_request,
    handle_approval_show,
)
from mesh.daemon.handlers.task import (
    handle_task_accept,
    prepare_task_start_workspace,
    prepare_task_submit_git,
    handle_task_assign,
    handle_task_cancel,
    handle_task_close,
    handle_task_create,
    handle_task_escalate,
    handle_task_fail,
    handle_task_heartbeat,
    handle_task_list,
    handle_task_merge,
    handle_task_progress,
    handle_task_reassign,
    handle_task_reject,
    handle_task_retry,
    handle_task_show,
    handle_task_start,
    handle_task_submit,
    handle_task_wait,
)
from mesh.daemon.handlers.dep import (
    handle_dep_add,
    handle_dep_list,
    handle_dep_remove,
)
from mesh.daemon.handlers.audit import handle_audit_verify
from mesh.daemon.handlers import run as run_handlers
from mesh.daemon.handlers.run import (
    handle_run_list,
    handle_run_log,
    handle_run_release,
    handle_run_show,
    handle_task_log,
    handle_workspace_list,
    handle_workspace_prune,
    handle_workspace_delete,
    handle_workspace_show,
    prepare_workspace_prune,
)
from mesh.daemon.scheduler import _sweep_error, reset_scheduler_errors, run_scheduler_tick, scheduler_errors_view
from mesh.core.policy import parse_positive_int
from mesh.core.redact import redact, redact_value
from mesh.core.settings import validate_setting
from mesh.core.repos import add_lead_repo_worktree, project_repo_paths
from mesh.core.workspaces import ensure_run_dir, ensure_workspace_dir, find_ws_reserved_idempotency

# Linux PR_SET_CHILD_SUBREAPER. 34 is PR_SET_THP_DISABLE and yields EINVAL (M1a bug).
PR_SET_CHILD_SUBREAPER = 36
MAX_REQUEST_BYTES = 1024 * 1024  # 1 MiB
#: Blind review P2: connections served at once, and how long a client may take to send
#: its request line (long waits start after the line is read, so they are unaffected).
MAX_CONNECTIONS = 256
INITIAL_READ_TIMEOUT_S = 30.0
#: How long an oversized request line is still drained before the connection closes.
OVERSIZE_DRAIN_S = 2.0



def _redact_msg(text: str) -> str:
    """Full review: unexpected-error text is redacted like MeshError envelopes (§15.2)."""
    from mesh.core.redact import redact

    return redact(text)

def _conn_limits() -> Tuple[int, float]:
    """(max connections, initial read timeout); tests may lower them (MESH_TESTING only)."""
    if os.environ.get("MESH_TESTING") == "1":
        try:
            return (int(os.environ.get("MESH_TEST_MAX_CONNECTIONS", MAX_CONNECTIONS)),
                    float(os.environ.get("MESH_TEST_INITIAL_READ_TIMEOUT_S", INITIAL_READ_TIMEOUT_S)))
        except ValueError:
            pass
    return MAX_CONNECTIONS, INITIAL_READ_TIMEOUT_S

PLANNED_COMMAND_MILESTONES = {
    # Task commands
    # Intake / Project
    # Workspace
    # Top
    "top": None,
}


REAP_INTERVAL_S = 5.0


def _start_replay_ok(
    cmd: str,
    args: Dict[str, Any],
    approval: Any,
    idempotency_key: Optional[str],
    stored_hash: str,
    stored_response_json: str,
    *,
    via: Optional[str] = None,
    user_statement: Any = None,
    evidence_ref: Any = None,
    if_version: Any = None,
) -> bool:
    """Sakana PR #4 P2-3: a `task.start` retry that differs from the stored request only in
    its key-derived default session replays the original start (see
    ``task_start_replay_matches``). Every other command keeps the exact-hash rule."""
    if cmd != "task.start":
        return False
    try:
        stored = json.loads(stored_response_json)
    except (TypeError, ValueError):
        return False

    def matches(new_args: Dict[str, Any]) -> bool:
        doc: Dict[str, Any] = {"cmd": cmd, "args": new_args}
        if approval:
            doc["approval"] = approval
        if via == "relayed" and user_statement is not None:
            doc["user_statement"] = user_statement
        if evidence_ref is not None:
            doc["evidence_ref"] = evidence_ref
        if if_version is not None:
            doc["if_version"] = if_version
        try:
            return hashlib.sha256(canonical_json(doc)).hexdigest() == stored_hash
        except ValueError:
            return False

    return task_start_replay_matches(args, idempotency_key, stored, matches)


def idempotency_partition(principal: Dict[str, Any], via: Optional[str]) -> str:
    """Key the idempotency cache by effective credential scope and channel, not just principal."""
    pid = principal["id"]
    if via == "run_token":
        scope = principal.get("scope")
        scope_doc = scope if isinstance(scope, dict) else {}
        return pid + "#scope:" + hashlib.sha256(canonical_json(scope_doc)).hexdigest()[:32]
    if via == "relayed":
        return pid + "#relayed"
    return pid


_NON_MUTATING_CMDS = frozenset({
    "daemon.status", "whoami", "version",
    "config.get", "config.list",
    "agent.list", "agent.show", "agent.token.list",
    "adapter.list", "adapter.show", "adapter.models",
    "project.list", "project.show", "project.leads", "project.wait",
    "task.list", "task.show", "task.log", "task.wait", "dep.list",
    "inbox.list", "inbox.wait",
    "run.list", "run.show", "run.log", "run.attach_info",
    "workspace.list", "workspace.show",
    "approval.list", "approval.show", "approval.check",
    "event.list", "event.tail", "audit.verify", "doctor",
})


def _resolve_idempotency_scope(
    conn: Any,
    cmd: str,
    args: Any,
    stored_envelope: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """Extract (project_id, task_id) for resource-scoped authz re-check on idempotent replay."""
    proj_id: Optional[str] = None
    tsk_id: Optional[str] = None
    if isinstance(args, dict):
        if isinstance(args.get("project_id"), str) and args["project_id"]:
            proj_id = args["project_id"]
        elif isinstance(args.get("project"), str) and args["project"]:
            prow = conn.execute(
                "SELECT id FROM projects WHERE id = ? OR name = ? LIMIT 1;",
                (args["project"], args["project"]),
            ).fetchone()
            if prow is not None:
                proj_id = prow["id"]
        if isinstance(args.get("task_id"), str) and args["task_id"]:
            tsk_id = args["task_id"]
        elif isinstance(args.get("task"), str) and args["task"]:
            tsk_id = args["task"]
    data = stored_envelope.get("data") if isinstance(stored_envelope, dict) else None
    if isinstance(data, dict):
        if not proj_id and isinstance(data.get("project_id"), str) and data["project_id"]:
            proj_id = data["project_id"]
        if not tsk_id and isinstance(data.get("task_id"), str) and data["task_id"]:
            tsk_id = data["task_id"]
        if not tsk_id and isinstance(data.get("task"), dict) and isinstance(data["task"].get("id"), str):
            tsk_id = data["task"]["id"]
        if not proj_id and isinstance(data.get("project"), dict) and isinstance(data["project"].get("id"), str):
            proj_id = data["project"]["id"]
    if tsk_id and not proj_id:
        trow = conn.execute("SELECT project_id FROM tasks WHERE id = ?;", (tsk_id,)).fetchone()
        if trow is not None:
            proj_id = trow["project_id"]
    if not proj_id and not tsk_id and isinstance(args, dict):
        if isinstance(args.get("approval_id"), str) and args["approval_id"]:
            arow = conn.execute("SELECT project_id, task_id FROM approvals WHERE id = ?;", (args["approval_id"],)).fetchone()
            if arow is not None:
                proj_id, tsk_id = arow["project_id"], arow["task_id"]
        elif isinstance(args.get("run_id"), str) and args["run_id"]:
            rrow = conn.execute("SELECT project_id, task_id FROM runs WHERE id = ?;", (args["run_id"],)).fetchone()
            if rrow is not None:
                proj_id, tsk_id = rrow["project_id"], rrow["task_id"]
    if cmd in ("intake.submit", "project.create") and not (
        isinstance(args, dict) and (args.get("project") or args.get("project_id"))
    ):
        return None, None
    return proj_id, tsk_id


def _idempotency_record(cmd: str, envelope: Dict[str, Any]) -> Dict[str, Any]:
    """The envelope as stored for replay: secrets never reach mesh.db (§15.1).

    A token is shown once: an `agent.token.issue` replay returns the stored envelope
    with the token value replaced (review m1c-run2 finding 6). Everything else is
    redacted with the §15.2 patterns.
    """
    stored = redact_value(envelope)
    data = stored.get("data") if isinstance(stored, dict) else None
    if cmd == "agent.token.issue" and isinstance(data, dict) and "token" in data:
        data["token"] = "[REDACTED:shown_once]"
    return stored


def _idempotency_cutoff_iso(now_ts: Optional[float] = None) -> str:
    ts = time.time() if now_ts is None else now_ts
    return now_iso_utc(ts - c.IDEMPOTENCY_TTL_S)


def _lookup_live_idempotency(
    conn: Any,
    principal_id: str,
    key: str,
    now_ts: Optional[float] = None,
) -> Optional[Any]:
    cutoff = _idempotency_cutoff_iso(now_ts)
    return conn.execute(
        "SELECT request_hash, response_json, exit_code FROM idempotency "
        "WHERE principal_id = ? AND key = ? AND created_at >= ?;",
        (principal_id, key, cutoff),
    ).fetchone()


def _store_idempotency_row(
    conn: Any,
    principal_id: str,
    key: str,
    cmd: str,
    req_hash: str,
    response_json: str,
    exit_code: int,
    now_ts: Optional[float] = None,
    *,
    ignore_conflict: bool = False,
) -> bool:
    ts = time.time() if now_ts is None else now_ts
    cutoff = now_iso_utc(ts - c.IDEMPOTENCY_TTL_S)
    now = now_iso_utc(ts)
    conn.execute(
        "DELETE FROM idempotency WHERE principal_id = ? AND key = ? AND created_at < ?;",
        (principal_id, key, cutoff),
    )
    sql = (
        "INSERT OR IGNORE INTO idempotency (principal_id, key, cmd, request_hash, response_json, exit_code, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?);"
        if ignore_conflict else
        "INSERT INTO idempotency (principal_id, key, cmd, request_hash, response_json, exit_code, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?);"
    )
    cur = conn.execute(sql, (principal_id, key, cmd, req_hash, response_json, exit_code, now))
    return cur.rowcount == 1


def _kill_failed_runs() -> List[Dict[str, Any]]:
    """Runs whose termination failed and is being retried (§11.4, shown in `mesh status`)."""
    from mesh.daemon import terminate

    return terminate.kill_failed_runs()


#: Internal requests (lead-turn actions, plan §12.4, Q10): the one-transaction handlers
#: of the lead action list, the same functions `handle_client` dispatches. `task.merge`
#: owns its transactions and a worker thread (m2-r5), so it cannot run inside the
#: supervisor's transaction and is not here: `_queue_internal_merge` checks it and
#: queues it, and `DaemonServer` starts it after the caller's transaction committed.
INTERNAL_REQUEST_HANDLERS: Dict[str, Callable[[HandlerContext], HandlerResult]] = {
    "task.create": handle_task_create,
    "task.assign": handle_task_assign,
    "task.reassign": handle_task_reassign,
    "task.accept": handle_task_accept,
    "task.reject": handle_task_reject,
    "task.retry": handle_task_retry,
    "task.escalate": handle_task_escalate,
    "task.cancel": handle_task_cancel,
    "task.close": handle_task_close,
    "dep.add": handle_dep_add,
    "dep.remove": handle_dep_remove,
    "project.note": handle_project_note,
    "project.complete": handle_project_complete,
    "project.deliver": handle_project_deliver,
    "approval.request": handle_approval_request,
}


def dispatch_internal_request(ctx: HandlerContext, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    """Run one request inside the caller's open transaction, as `handle_client` would.

    Used for lead-turn actions (§12.4, Q10). A SAVEPOINT stands in for the request's
    own transaction: a MeshError rolls the request back, except rc 10, whose pending
    approval row is kept exactly as `handle_client` commits it. A successful response
    is stored under `idempotency_key` inside the same savepoint and replayed on reuse
    (§13.4). Authorization is the handler's own `check_authz`, so the caller's
    principal and `via` decide it. Returns the response envelope (with `exit_code`);
    never raises for a request error.
    """
    conn = ctx.conn
    cmd = ctx.cmd
    principal_id = idempotency_partition(ctx.principal, ctx.via)
    dry_run = bool(getattr(ctx, "dry_run", False))

    def _err_env(err: MeshError) -> Dict[str, Any]:
        env = err.to_envelope(request_id=ctx.request_id)
        if dry_run:
            env["dry_run"] = True
        return env

    approval_val = getattr(ctx, "approval", None)
    if approval_val is not None and not isinstance(approval_val, str):
        return _err_env(MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'approval' must be a string",
            details={"param": "approval"},
        ))
    user_stmt = getattr(ctx, "user_statement", None)
    if ctx.via == "relayed":
        if not isinstance(user_stmt, str) or not user_stmt.strip():
            return _err_env(MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field '--user-statement' is required and non-empty when using --relayed",
                details={"param": "user_statement"},
            ))
        if len(user_stmt) > 4000:
            return _err_env(MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="User statement exceeds 4000 character limit",
                details={"param": "user_statement"},
            ))
    ev_ref = getattr(ctx, "evidence_ref", None)
    if ev_ref is not None and (not isinstance(ev_ref, str) or len(ev_ref) > 4000):
        return _err_env(MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'evidence_ref' must be a string up to 4000 characters",
            details={"param": "evidence_ref"},
        ))
    top_if_version = getattr(ctx, "if_version", None)
    if top_if_version is not None and (isinstance(top_if_version, bool) or not isinstance(top_if_version, int)):
        return _err_env(MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'if_version' must be an integer",
            details={"param": "if_version"},
        ))

    relayed_by = (getattr(ctx, "relayed_by", None) or ctx.principal.get("name")) if ctx.via == "relayed" else None
    try:
        check_authz(
            conn,
            cmd,
            ctx.principal,
            ctx.actor_kind,
            ctx.via,
            relayed_by=relayed_by if isinstance(relayed_by, str) else None,
            resource_resolved=False,
        )
    except MeshError as exc:
        return _err_env(exc)

    cmd_and_args: Dict[str, Any] = {"cmd": cmd, "args": ctx.args}
    if approval_val:
        cmd_and_args["approval"] = approval_val  # as handle_client hashes it (§13.4)
    if ctx.via == "relayed" and user_stmt is not None:
        cmd_and_args["user_statement"] = user_stmt
    if ev_ref is not None:
        cmd_and_args["evidence_ref"] = ev_ref
    if top_if_version is not None:
        cmd_and_args["if_version"] = top_if_version
    try:
        req_hash = hashlib.sha256(canonical_json(cmd_and_args)).hexdigest()
    except ValueError as ve:
        return _err_env(MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message=str(ve)))
    if idempotency_key and not dry_run and cmd not in _NON_MUTATING_CMDS:
        irow = _lookup_live_idempotency(conn, principal_id, idempotency_key)
        if irow:
            if irow["request_hash"] != req_hash and not _start_replay_ok(
                    cmd, ctx.args, approval_val, idempotency_key, irow["request_hash"], irow["response_json"],
                    via=ctx.via, user_statement=user_stmt, evidence_ref=ev_ref, if_version=top_if_version):
                return _err_env(MeshError(
                    code=c.IDEMPOTENCY_KEY_REUSED,
                    exit_code=c.EXIT_CONFLICT,
                    message=f"Idempotency key '{idempotency_key}' reused with different request parameters",
                ))
            stored = json.loads(irow["response_json"])
            r_proj, r_task = _resolve_idempotency_scope(conn, cmd, ctx.args, stored)
            try:
                check_authz(
                    conn,
                    cmd,
                    ctx.principal,
                    ctx.actor_kind,
                    ctx.via,
                    project_id=r_proj,
                    task_id=r_task,
                    relayed_by=relayed_by if isinstance(relayed_by, str) else None,
                    resource_resolved=(r_proj is not None or r_task is not None),
                )
                check_approval_replay_authz(conn, cmd, ctx.principal, ctx.actor_kind, ctx.via, ctx.args, stored)
            except MeshError as exc:
                return _err_env(exc)
            stored["idempotent_replay"] = True
            stored.setdefault("exit_code", irow["exit_code"])
            return stored
    if cmd == "task.merge":
        effects_before = len(ctx.effects) if isinstance(getattr(ctx, "effects", None), list) else None
        env = _queue_internal_merge(ctx)
        if dry_run:
            added = []
            if effects_before is not None:
                added = list(ctx.effects[effects_before:])
                del ctx.effects[effects_before:]
            if env.get("ok"):
                return {
                    "ok": True,
                    "data": {"would": {"transitions": [], "events": [], "effects": [e.kind for e in added]}},
                    "warnings": env.get("warnings", []),
                    "request_id": ctx.request_id,
                    "idempotent_replay": False,
                    "dry_run": True,
                    "exit_code": env.get("exit_code", c.EXIT_OK),
                }
            env["dry_run"] = True
        return env
    handler = INTERNAL_REQUEST_HANDLERS.get(cmd)
    if handler is None:
        return _err_env(MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Command '{cmd}' cannot run as an internal request",
            details={"cmd": cmd, "reason": "not_internal"},
        ))

    if cmd == "project.deliver":
        # Full review r3 P1-4: a lead action's deliver intent carries its idempotency key
        # too, so recovery stores the replay of a delivery whose transaction was lost.
        ctx.idempotency = (  # type: ignore[attr-defined]
            {"principal_id": principal_id, "key": idempotency_key, "request_hash": req_hash}
            if idempotency_key and not dry_run else None
        )
    effects_before = len(ctx.effects) if isinstance(getattr(ctx, "effects", None), list) else None
    conn.execute("SAVEPOINT internal_request;")
    try:
        res = handler(ctx)
        if dry_run:
            added = []
            if effects_before is not None:
                added = list(ctx.effects[effects_before:])
                del ctx.effects[effects_before:]
            conn.execute("ROLLBACK TO internal_request;")
            conn.execute("RELEASE internal_request;")
            return {
                "ok": True,
                "data": {
                    "would": {
                        "transitions": res.transitions,
                        "events": res.events,
                        "effects": [eff.kind for eff in added],
                    }
                },
                "warnings": res.warnings,
                "request_id": ctx.request_id,
                "idempotent_replay": False,
                "dry_run": True,
                "exit_code": res.exit_code,
            }
        envelope = {
            "ok": True,
            "data": res.data,
            "warnings": res.warnings,
            "request_id": ctx.request_id,
            "idempotent_replay": False,
            "dry_run": False,
            "exit_code": res.exit_code,
        }
        if idempotency_key and cmd not in _NON_MUTATING_CMDS and res.exit_code not in (
            c.EXIT_INTERNAL, c.EXIT_APPROVAL_REQUIRED, c.EXIT_DAEMON_UNAVAILABLE
        ):
            _store_idempotency_row(
                conn,
                principal_id,
                idempotency_key,
                cmd,
                req_hash,
                json.dumps(_idempotency_record(cmd, envelope)),
                res.exit_code,
            )
    except MeshError as exc:
        if exc.exit_code != c.EXIT_APPROVAL_REQUIRED or dry_run:
            conn.execute("ROLLBACK TO internal_request;")
            if effects_before is not None:
                del ctx.effects[effects_before:]
        conn.execute("RELEASE internal_request;")
        return _err_env(exc)
    except Exception as exc:  # noqa: BLE001 - reported like handle_client's rc 1
        conn.execute("ROLLBACK TO internal_request;")
        if effects_before is not None:
            del ctx.effects[effects_before:]
        conn.execute("RELEASE internal_request;")
        err_env: Dict[str, Any] = {
            "ok": False,
            "error": {"code": c.INTERNAL_ERROR, "message": _redact_msg(f"Daemon unexpected error: {exc}"), "details": {}},
            "exit_code": c.EXIT_INTERNAL,
            "request_id": ctx.request_id,
        }
        if dry_run:
            err_env["dry_run"] = True
        return err_env
    conn.execute("RELEASE internal_request;")
    return envelope


def _queue_internal_merge(ctx: HandlerContext) -> Dict[str, Any]:
    """Check an internal `task.merge` and queue it; never merges (Quest r10b, option b).

    The checks are the merge's own TX1 guards (`check_merge_request`: task exists,
    `check_authz` under the caller's principal and `via`, kind, transition, no task
    lease, merge lease free), run read-only in a savepoint that is always rolled back.
    If they pass, one `start_task_merge` effect is queued; the merge itself runs later
    through `handle_task_merge`, which repeats every check in its own TX1. Nothing is
    stored under the idempotency key: the queued response is not the merge's outcome.
    """
    from mesh.daemon.merge import check_merge_request

    conn = ctx.conn
    conn.execute("SAVEPOINT internal_request;")
    try:
        task, _project = check_merge_request(ctx, ctx.args.get("task_id"))
    except MeshError as exc:
        conn.execute("ROLLBACK TO internal_request;")
        conn.execute("RELEASE internal_request;")
        return exc.to_envelope(request_id=ctx.request_id)
    except Exception as exc:  # noqa: BLE001 - reported like handle_client's rc 1
        conn.execute("ROLLBACK TO internal_request;")
        conn.execute("RELEASE internal_request;")
        return {
            "ok": False,
            "error": {"code": c.INTERNAL_ERROR, "message": _redact_msg(f"Daemon unexpected error: {exc}"), "details": {}},
            "exit_code": c.EXIT_INTERNAL,
            "request_id": ctx.request_id,
        }
    conn.execute("ROLLBACK TO internal_request;")
    conn.execute("RELEASE internal_request;")
    ctx.effects.append(Effect(kind="start_task_merge", params={"args": dict(ctx.args)}))
    return {
        "ok": True,
        "data": {"task_id": task["id"], "status": "queued"},
        "warnings": [],
        "request_id": ctx.request_id,
        "idempotent_replay": False,
        "dry_run": False,
        "exit_code": c.EXIT_OK,
    }


# PR42 r1: handlers shutdown may cancel ("unknown"/"" = request line not read yet).
_CANCEL_ON_STOP = frozenset({"unknown", "", "inbox.wait", "task.wait", "project.wait", "event.tail"})


class DaemonServer:
    """The background orchestration daemon service (meshd)."""

    def __init__(self, home: str, testing_mode: Optional[bool] = None):
        self.home = resolve_home(home)  # round 3: the one home resolution
        # m3-test-kdf fix round (mode authority): meshd decides its testing mode once,
        # here. An explicit argument wins over the environment; only when the caller
        # passes nothing (`mesh daemon run`) does MESH_TESTING == "1" decide. The
        # stored value is what the unlock path passes to the KDF check; auth helpers
        # never read the environment themselves.
        if testing_mode is None:
            testing_mode = os.environ.get("MESH_TESTING") == "1"
        self.testing_mode = bool(testing_mode)
        self.boot_id: Optional[str] = None
        self.db_conn = None
        self.lock_fd: Optional[int] = None
        self.server: Optional[asyncio.Server] = None
        self.start_time: float = 0.0
        self.stop_event: Optional[asyncio.Event] = None
        self.active_runs: Dict[str, int] = {}
        self.known_run_ids: Set[str] = set()
        self.active_streams: int = 0
        self.active_waits: int = 0
        self.log_file: Optional[Any] = None
        self._scheduler_wake: Optional[asyncio.Event] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._termination_task: Optional[asyncio.Task] = None
        self._stub_task: Optional[asyncio.Task] = None
        self._last_reap = 0.0
        self._notify_cond: Optional[asyncio.Condition] = None
        self._lead_merge_tasks: Set[asyncio.Future] = set()
        self._project_merge_queues: Dict[str, Deque[Dict[str, Any]]] = {}
        self._project_merge_workers: Dict[str, asyncio.Future] = {}
        self._project_merge_inflight_reqs: Set[str] = set()
        # Full review P1 (daemon-rest): shutdown waits for these before it releases
        # meshd.lock and closes SQLite. `_merge_jobs` holds the shielded
        # `_pipeline_and_finish` futures (merge.py), `_client_tasks` the socket handlers.
        self._merge_jobs: Set[asyncio.Future] = set()
        self._client_tasks: Set[asyncio.Task] = set()
        # PR42 r1: the command each handler is serving ("unknown" until its request
        # line is read); shutdown cancels only long polls and unread requests.
        self._client_cmds: Dict[asyncio.Task, str] = {}
        # PR42 Linear r1: each handler's writer, so shutdown can abort a connection
        # whose client stopped reading (a blocked drain() would hold meshd.lock forever).
        self._client_writers: Dict[asyncio.Task, Any] = {}
        self._stopping = False

    def track_merge_job(self, job: "asyncio.Future") -> None:
        """Register an in-flight merge pipeline; shutdown awaits it (full review P1)."""
        self._merge_jobs.add(job)
        job.add_done_callback(self._merge_jobs.discard)

    @property
    def notify_cond(self) -> asyncio.Condition:
        if getattr(self, "_notify_cond", None) is None:
            self._notify_cond = asyncio.Condition()
        return self._notify_cond

    async def notify_waiters(self) -> None:
        """Wake up any long-polling wait or tail connections."""
        async with self.notify_cond:
            self.notify_cond.notify_all()

    def get_lockout_window_s(self) -> int:
        """Return the passphrase lockout window in seconds.

        Plan §7.1 fixes it at 300 s. In testing mode the window is also 300 s
        unless a test sets the testing-only setting `auth.lockout_window_s`
        (range 1..3600, rejected outside testing mode); the 2 s fallback below
        applies only to homes without that settings row.
        """
        if not self.testing_mode:
            return 300
        default = 2
        if not self.db_conn:
            return default
        try:
            cur = self.db_conn.execute("SELECT value_json FROM settings WHERE key = 'auth.lockout_window_s';")
            row = cur.fetchone()
            if row and row["value_json"]:
                return int(json.loads(row["value_json"]))
        except Exception:
            return default
        return default

    def wake_scheduler(self) -> None:
        """Wake up the scheduler loop immediately."""
        if getattr(self, "_scheduler_wake", None):
            self._scheduler_wake.set()

    async def _termination_loop(self) -> None:
        """Advance run terminations every 200 ms (plan §11.4 poll interval)."""
        from mesh.daemon import terminate
        from mesh.daemon.supervisor import advance_terminations, next_exit_check_in, pending_exit_checks

        while self.stop_event and not self.stop_event.is_set():
            # Wake when a pending exit confirmation falls due rather than on the next
            # fixed 200 ms beat (m3-runtargets-race A1: the second scan is taken
            # POLL_S after the first, not up to 2 x POLL_S).
            delay = terminate.POLL_S
            due_in = next_exit_check_in()
            if due_in is not None:
                delay = max(0.005, min(delay, due_in))
            await asyncio.sleep(delay)
            if not self.db_conn or not (terminate.active() or pending_exit_checks()):
                continue
            tmode = getattr(self, "testing_mode", None)
            try:
                if tmode is None:
                    closed = advance_terminations(self.db_conn, self.home)
                else:
                    closed = advance_terminations(self.db_conn, self.home, testing_mode=tmode)
            except Exception as e:
                self.log_error(f"Termination loop failed: {e}")
                continue
            self.start_queued_lead_merges()
            if closed:
                await self.notify_waiters()
                self.wake_scheduler()

    async def _stub_child(self, seconds: float) -> None:
        """Testing hook MESH_TEST_STUB_CHILD_S (§17.2, H13): one registered asyncio child.

        Writes run/stub.pid at start and run/stub.rc with asyncio's returncode at exit.
        """
        from mesh.daemon import children

        run_dir = os.path.join(self.home, "run")
        try:
            proc = await children.create_subprocess_exec_registered("stub", "sleep", str(seconds))
        except Exception as e:
            self.log_error(f"Stub child failed to start: {e}")
            return
        try:
            with open(os.path.join(run_dir, "stub.pid"), "w", encoding="utf-8") as f:
                f.write(f"{proc.pid}\n")
            rc = await proc.wait()
            with open(os.path.join(run_dir, "stub.rc"), "w", encoding="utf-8") as f:
                f.write(f"{rc}\n")
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            raise
        finally:
            children.unregister(proc.pid)

    def _maybe_reap(self) -> None:
        """Reap orphan zombies every 5 s (§11.4); terminations also reap on completion."""
        from mesh.daemon import children

        now = time.monotonic()
        if now - self._last_reap < REAP_INTERVAL_S:
            return
        self._last_reap = now
        try:
            children.reap_orphans()
        except Exception as e:
            self.log_error(f"Reaper failed: {e}")

    async def _scheduler_loop(self) -> None:
        """Periodic scheduler loop ticking every 1 s or on demand."""
        while self.stop_event and not self.stop_event.is_set():
            try:
                if self.db_conn:
                    run_scheduler_tick(self.db_conn, self.home, testing_mode=self.testing_mode)
                    self.start_queued_lead_merges()
                    await self.notify_waiters()
            except Exception as e:
                _sweep_error(self.home, "tick", e)
            self._maybe_reap()
            if not self._scheduler_wake:
                break
            try:
                await asyncio.wait_for(self._scheduler_wake.wait(), timeout=1.0)
                self._scheduler_wake.clear()
            except asyncio.TimeoutError:
                pass

    def log_request(
        self,
        cmd: str,
        principal_id: str,
        request_id: str,
        exit_code: int,
        duration_ms: float,
    ) -> None:
        """Write request audit log per plan §15.2 and checklist C11.

        Never records the auth field or credential secrets.
        """
        # Format line without the word "auth" (C11: grep -c '"auth"' meshd.log == 0).
        # Blind review P3: request fields are client-supplied: redacted and escaped, and
        # the file rotates (mesh.core.logfile).
        append_line(self.home, (
            f"{now_iso_utc()} req={escape_field(request_id)} cmd={escape_field(cmd)} principal={escape_field(principal_id)} "
            f"rc={exit_code} dur_ms={duration_ms:.2f}"
        ))

    def log_error(self, message: str) -> None:
        """Write error log to meshd.log per plan §15.2 and checklist C11 without secrets."""
        append_line(self.home, f"{now_iso_utc()} ERROR {message}")

    def setup_storage(self) -> None:
        """Check filesystem permissions, paths, and SQLite integrity."""
        # 0. Plan §17.1: testing mode (fake adapter, crash hooks, lowered minimums) is
        # refused on the default home (review m1c-run4 §6).
        # Same guard (and alias detection) as `mesh init` and `mesh doctor`:
        # mesh.core.paths.refuse_testing_on_default_home (m3-test-kdf round 2).
        refuse_testing_on_default_home(self.home, self.testing_mode)
        # 1. Permission checks
        check_home_permissions(self.home)

        # 2. Bidirectional install-path security check (§7.6 mitigation 7)
        check_install_path_secure(install_root=get_install_root(), home=self.home)

        # 3. Check socket path length
        sock_path = os.path.join(self.home, "run", "mesh.sock")
        check_socket_path_length(sock_path)

        # 4. Open and verify database
        db_path = os.path.join(self.home, "mesh.db")
        if not os.path.exists(db_path):
            raise MeshError(
                code=c.INTERNAL_ERROR,
                exit_code=c.EXIT_INTERNAL,
                message=f"Database does not exist at '{db_path}'. Run 'mesh init' first.",
            )

        self.db_conn = connect_db(db_path)
        try:
            # Check configured workspaces.root if set in DB
            configured_ws = None
            try:
                cur = self.db_conn.execute("SELECT value_json FROM settings WHERE key = 'workspaces.root';")
                row = cur.fetchone()
                if row and row["value_json"]:
                    configured_ws = json.loads(row["value_json"])
            except Exception:
                pass

            if configured_ws:
                check_install_path_secure(
                    install_root=get_install_root(), home=self.home, workspaces_root=configured_ws
                )

            # Registered project repos must not overlap the install root either (§7.6 mitigation 7).
            repo_paths = project_repo_paths(self.db_conn)
            if repo_paths:
                check_install_path_secure(install_root=get_install_root(), repo_paths=repo_paths)

            # Check schema version
            cur = self.db_conn.execute("SELECT value FROM meta WHERE key = 'schema_version';")
            row = cur.fetchone()
            if not row:
                raise MeshError(
                    code=c.INTERNAL_ERROR,
                    exit_code=c.EXIT_INTERNAL,
                    message="Missing schema_version in database meta table",
                )
            raw = row["value"]
            schema_version = parse_schema_version(raw)
            if schema_version is None:
                raise DbIntegrityError([f"schema_version is not an integer: {raw!r}"])
            if schema_version > CURRENT_SCHEMA_VERSION:
                raise SchemaTooNewError(schema_version, CURRENT_SCHEMA_VERSION)
            if schema_version < 1:
                raise DbIntegrityError([f"schema_version out of range: {schema_version}"])

            # Integrity quick check
            cur = self.db_conn.execute("PRAGMA quick_check;")
            rows = [r[0] for r in cur.fetchall()]
            if rows != ["ok"]:
                raise DbIntegrityError(rows)
        except BaseException:
            self.db_conn.close()
            self.db_conn = None
            raise

    def acquire_lock(self) -> None:
        """Acquire single-instance flock on $MESH_HOME/run/meshd.lock."""
        run_dir = os.path.join(self.home, "run")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        lock_path = os.path.join(run_dir, "meshd.lock")

        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise MeshError(
                code=c.DAEMON_ALREADY_RUNNING,
                exit_code=c.EXIT_INTERNAL,
                message=f"Another meshd instance is already running on '{self.home}'",
            ) from exc

        # Write pid file
        pid_path = os.path.join(run_dir, "meshd.pid")
        try:
            with open(pid_path, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            os.chmod(pid_path, 0o600)
        except BaseException:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        self.lock_fd = fd

    def release_lock(self) -> None:
        """Release flock and remove pid file."""
        if self.lock_fd is None:
            return
        run_dir = os.path.join(self.home, "run")
        pid_path = os.path.join(run_dir, "meshd.pid")
        if os.path.exists(pid_path):
            try:
                os.remove(pid_path)
            except OSError:
                pass

        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.lock_fd)
        except OSError:
            pass
        self.lock_fd = None

    def record_boot(self) -> None:
        """Insert boots table row and mark previous active boots crashed."""
        self.boot_id = new_id("boot")
        reset_scheduler_errors(self.home)
        now = now_iso_utc()
        with self.db_conn:
            self.db_conn.execute("BEGIN IMMEDIATE;")
            # Mark previous active boots as crashed (§11.7 step 1)
            self.db_conn.execute(
                "UPDATE boots SET crashed = 1 WHERE stopped_at IS NULL;"
            )
            # Insert this boot
            self.db_conn.execute(
                "INSERT INTO boots (id, pid, started_at, stopped_at, crashed) VALUES (?, ?, ?, NULL, 0);",
                (self.boot_id, os.getpid(), now),
            )
            self.db_conn.execute("COMMIT;")

    def record_stop(self) -> None:
        """Mark boot as stopped."""
        if self.boot_id and self.db_conn:
            try:
                now = now_iso_utc()
                with self.db_conn:
                    self.db_conn.execute("BEGIN IMMEDIATE;")
                    self.db_conn.execute(
                        "UPDATE boots SET stopped_at = ? WHERE id = ?;",
                        (now, self.boot_id),
                    )
                    self.db_conn.execute("COMMIT;")
            except Exception:
                pass

    def run_recovery(self) -> None:
        """Crash recovery (§11.7 steps 2–4, 6, 7) before the socket opens.

        Runs on every boot: after a crash and after a clean `daemon stop`
        (SIGTERM leaves runs running, so they are adopted here, I8).
        """
        from mesh.daemon.recovery import recover

        if not self.db_conn or not self.boot_id:
            return
        try:
            recover(self.db_conn, self.home, self.boot_id, testing_mode=self.testing_mode)
        except Exception as e:  # recovery must not keep meshd down; each step is its own tx
            self.log_error(f"Recovery failed: {type(e).__name__}: {e}")
        self.refresh_run_caches()

    def setup_subreaper(self) -> None:
        """Set PR_SET_CHILD_SUBREAPER so orphaned descendants reparent to meshd (§11.4)."""
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl.argtypes = [
                ctypes.c_int,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
            ]
            libc.prctl.restype = ctypes.c_int
            ctypes.set_errno(0)
            rc = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
            err = ctypes.get_errno()
        except Exception as exc:
            self.log_error(
                f"prctl(PR_SET_CHILD_SUBREAPER) failed: {exc}; "
                "orphan reaping is degraded on this host"
            )
            return
        if rc != 0:
            self.log_error(
                f"prctl(PR_SET_CHILD_SUBREAPER) returned {rc} (errno={err}); "
                "orphan reaping is degraded on this host"
            )

    def refresh_run_caches(self) -> None:
        """Refresh active and known runs for session binding checks."""
        from mesh.daemon import supervisor

        cur = self.db_conn.execute(
            "SELECT id, wrapper_pid, proc_starttime, status, run_dir FROM runs;"
        )
        self.known_run_ids.clear()
        self.active_runs.clear()
        for r in cur.fetchall():
            self.known_run_ids.add(r["id"])
            if r["status"] in ("starting", "running"):
                wpid, st = r["wrapper_pid"], r["proc_starttime"]
                if not wpid and r["run_dir"]:
                    # Full review r3 P1-5: a live run whose wrapper pid is not recorded
                    # yet (merge_test before its TX2, a spawn window, recovery deferred
                    # after a restart): the wrapper's own wrapper.json identifies it.
                    wpid, st = supervisor.close_identity({"run_dir": r["run_dir"]})
                # r4 P2-1: a pid reused by another process (proven by its starttime)
                # is not the run's; an unknown starttime keeps it (fail closed).
                if wpid and supervisor.group_still_ours(int(wpid), st):
                    self.active_runs[r["id"]] = int(wpid)
        for rid, wpid in supervisor.in_memory_active_runs().items():
            self.known_run_ids.add(rid)
            self.active_runs.setdefault(rid, wpid)
        from mesh.daemon import merge  # r2 P1-F: merge_test wrappers before TX2 records them

        for rid, wpid in merge.in_memory_active_test_runs().items():
            self.known_run_ids.add(rid)
            self.active_runs.setdefault(rid, wpid)

    async def authenticate_and_authorize(
        self,
        peer_pid: int,
        auth_info: Dict[str, Any],
        cmd: str,
        request_id: str,
    ) -> Tuple[Dict[str, Any], str, str, Optional[bytes]]:
        """Authenticate caller, enforcing run-session binding and lockout rules.

        Returns:
            (principal_dict, actor_kind, via, k_user)
        """
        self.refresh_run_caches()

        # Run-session classification (§7.1, G25–G27)
        bound_run_id = classify_peer_run_session(
            peer_pid=peer_pid,
            active_runs=self.active_runs,
            known_run_ids=self.known_run_ids,
            meshd_pid=os.getpid(),
        )

        if not isinstance(auth_info, dict):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'auth' must be a JSON object",
                details={"param": "auth"},
            )
        auth_type = auth_info.get("type", "none")
        if not isinstance(auth_type, str):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'auth.type' must be a string",
                details={"param": "auth.type"},
            )
        if auth_type == "token":
            token_val = auth_info.get("token", "")
            if not isinstance(token_val, str):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'auth.token' must be a string",
                    details={"param": "auth.token"},
                )
        elif auth_type == "user":
            if "passphrase" in auth_info and not isinstance(auth_info["passphrase"], str):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'auth.passphrase' must be a string",
                    details={"param": "auth.passphrase"},
                )

        # If inside a run, ONLY that run's own token is allowed
        if bound_run_id is not None:
            is_valid_run_token = False
            token_lookup = None
            if auth_type == "token":
                token = auth_info.get("token", "")
                token_lookup = parse_token_lookup(token)
                # Check if this token matches bound_run_id
                cur = self.db_conn.execute(
                    "SELECT principal_id, scope_json FROM credentials WHERE lookup = ? AND kind = 'run_token';",
                    (token_lookup,),
                )
                crow = cur.fetchone()
                if crow:
                    try:
                        scope = json.loads(crow["scope_json"])
                        if scope.get("run_id") == bound_run_id:
                            is_valid_run_token = True
                    except Exception:
                        pass

            if not is_valid_run_token:
                # Reject before verification; write auth.credential_in_run event
                bound_run = self.db_conn.execute(
                    "SELECT project_id, task_id FROM runs WHERE id = ?;", (bound_run_id,)
                ).fetchone()
                with self.db_conn:
                    self.db_conn.execute("BEGIN IMMEDIATE;")
                    insert_event(
                        conn=self.db_conn,
                        event_type="auth.credential_in_run",
                        actor_id=bound_run_id,
                        actor_kind="system",
                        via="system",
                        project_id=bound_run["project_id"] if bound_run else None,
                        task_id=bound_run["task_id"] if bound_run else None,
                        run_id=bound_run_id,
                        request_id=request_id,
                        payload={
                            "peer_pid": peer_pid,
                            "run_id": bound_run_id,
                            "credential_kind": auth_type,
                            "lookup": token_lookup,
                        },
                    )
                    self.db_conn.execute("COMMIT;")

                self.wake_scheduler()
                await self.notify_waiters()

                raise MeshError(
                    code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Process inside run '{bound_run_id}' cannot use {auth_type} credential",
                    details={"run_id": bound_run_id, "peer_pid": peer_pid},
                )

        # Normal authentication outside run session
        if auth_type == "user":
            passphrase = auth_info.get("passphrase", "")

            # Retrieve user credential first: the KDF params check (m3-test-kdf) runs
            # BEFORE the lockout check, so a weak or invalid home answers
            # KDF_TOO_WEAK / KDF_PARAMS_INVALID even after 5 prior failures (the
            # refusal is about the home, not about this caller). It runs no scrypt
            # and records no auth_failures row, so it cannot feed the lockout.
            cur = self.db_conn.execute(
                """SELECT c.secret_hash, c.salt, c.kdf_params, p.id, p.kind, p.name
                   FROM credentials c
                   JOIN principals p ON c.principal_id = p.id
                   WHERE p.kind = 'user' AND c.kind = 'passphrase' LIMIT 1;"""
            )
            urow = cur.fetchone()
            kdf_params = None
            if urow:
                kdf_params = parse_kdf_params(urow["kdf_params"], testing_mode=self.testing_mode)

            # Check lockout: 5 failures within the lockout window (300 s per plan §7.1).
            # Testing mode is also 300 s unless a test sets auth.lockout_window_s; the
            # 2 s fallback applies only to homes without that settings row.
            now = now_iso_utc()
            lockout_window = self.get_lockout_window_s()
            window_start = now_iso_utc(time.time() - lockout_window)
            cur = self.db_conn.execute(
                "SELECT COUNT(*) as cnt FROM auth_failures WHERE kind = 'passphrase' AND at >= ?;",
                (window_start,),
            )
            failures_cnt = cur.fetchone()["cnt"]
            if failures_cnt >= 5:
                raise MeshError(
                    code=c.LOCKED_OUT,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="User authentication locked out due to 5 consecutive failures within 5 minutes",
                    details={"failures": failures_cnt},
                )

            if not urow:
                raise MeshError(
                    code=c.PASSPHRASE_INVALID,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="User principal or credential not initialized",
                )

            salt = urow["salt"]
            stored_verifier = urow["secret_hash"]

            valid, k_user = verify_user_passphrase(passphrase, salt, kdf_params, stored_verifier,
                                                   testing_mode=self.testing_mode)
            if not valid:
                # Record failure in auth_failures and write auth.failed event
                with self.db_conn:
                    self.db_conn.execute("BEGIN IMMEDIATE;")
                    self.db_conn.execute(
                        "INSERT INTO auth_failures (kind, at, peer_pid) VALUES ('passphrase', ?, ?);",
                        (now, peer_pid),
                    )
                    insert_event(
                        conn=self.db_conn,
                        event_type="auth.failed",
                        actor_id=urow["id"],
                        actor_kind="system",
                        via="system",
                        request_id=request_id,
                        payload={"peer_pid": peer_pid, "target_user_id": urow["id"]},
                    )
                    self.db_conn.execute("COMMIT;")

                await self.notify_waiters()

                raise MeshError(
                    code=c.PASSPHRASE_INVALID,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="Passphrase verification failed",
                )

            principal = {"id": urow["id"], "kind": "user", "name": urow["name"]}
            return principal, "user", "direct", k_user

        elif auth_type == "token":
            token = auth_info.get("token", "")
            lookup = parse_token_lookup(token)
            if not lookup:
                raise MeshError(
                    code=c.TOKEN_INVALID,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="Malformed token string",
                )

            cur = self.db_conn.execute(
                """SELECT c.id, c.principal_id, c.kind as cred_kind, c.secret_hash,
                          c.scope_json, c.created_at, c.expires_at, c.revoked_at,
                          p.kind as principal_kind, p.name, p.disabled_at
                   FROM credentials c
                   JOIN principals p ON c.principal_id = p.id
                   WHERE c.lookup = ?;""",
                (lookup,),
            )
            row = cur.fetchone()
            if not row or not verify_token_hash(token, row["secret_hash"]):
                raise MeshError(
                    code=c.TOKEN_INVALID,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="Token not found or invalid",
                )

            if row["revoked_at"] is not None:
                raise MeshError(
                    code=c.TOKEN_REVOKED,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="Token has been revoked",
                )

            now = now_iso_utc()
            if row["expires_at"] is not None and row["expires_at"] < now:
                raise MeshError(
                    code=c.TOKEN_EXPIRED,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="Token has expired",
                )

            if row["disabled_at"] is not None:
                raise MeshError(
                    code=c.TOKEN_INVALID,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="Principal is disabled",
                )

            principal = {
                "id": row["principal_id"],
                "kind": row["principal_kind"],
                "name": row["name"],
                "credential_id": row["id"],
            }
            if row["cred_kind"] == "agent_token":
                # Load agent capabilities
                acur = self.db_conn.execute("SELECT capabilities_json FROM agents WHERE id = ?;", (row["principal_id"],))
                arow = acur.fetchone()
                if arow:
                    principal["capabilities"] = json.loads(arow["capabilities_json"])
                return principal, row["principal_kind"], "token", None
            else:
                if row["scope_json"]:
                    try:
                        principal["scope"] = json.loads(row["scope_json"])
                    except Exception:
                        principal["scope"] = {}
                else:
                    principal["scope"] = {}
                # A lead run token acts as its agent for the `intake` capability check
                # (§7.2); the executor branch above never reads capabilities.
                if (principal["scope"] or {}).get("role") == "lead":
                    acur = self.db_conn.execute(
                        "SELECT capabilities_json FROM agents WHERE id = ?;", (row["principal_id"],)
                    )
                    arow = acur.fetchone()
                    if arow:
                        try:
                            principal["capabilities"] = json.loads(arow["capabilities_json"])
                        except Exception:
                            principal["capabilities"] = []
                return principal, row["principal_kind"], "run_token", None

        elif auth_type == "none":
            # Allowed only for unauthenticated read/info commands
            if cmd in UNAUTH_CMDS:
                principal = {"id": "anonymous", "kind": "anonymous", "name": "anonymous"}
                return principal, "anonymous", "none", None

            raise MeshError(
                code=c.TOKEN_MISSING,
                exit_code=c.EXIT_UNAUTHENTICATED,
                message=f"Command '{cmd}' requires authentication (--user or --token-file)",
            )

        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Unknown auth type: '{auth_type}'",
        )

    def _rewrite_idempotency_response(self, principal_id: str, key: str, cmd: str,
                                      envelope: Dict[str, Any], exit_code: int) -> None:
        """Store ``envelope`` as the replay of an already committed keyed request."""
        if not self.db_conn:
            return
        from mesh.core.db import transaction

        try:
            with transaction(self.db_conn):
                self.db_conn.execute(
                    "UPDATE idempotency SET response_json = ?, exit_code = ? WHERE principal_id = ? AND key = ?;",
                    (json.dumps(_idempotency_record(cmd, envelope)), exit_code, principal_id, key),
                )
        except Exception as e:  # noqa: BLE001 - the live error is still returned
            self.log_error(f"Idempotent replay of {cmd} not updated after a reconcile failure: {e}")

    def execute_effects(self, effects: List[Effect]) -> List[asyncio.Future]:
        """Execute side-effects outside transaction."""
        pending_reconciles: List[asyncio.Future] = []
        for eff in effects:
            if eff.kind == "stop_daemon":
                if self.stop_event:
                    self.stop_event.set()
            elif eff.kind == "create_run_and_workspace_dirs":
                run_dir = eff.params.get("run_dir")
                ws_dir = eff.params.get("workspace_dir")
                if run_dir:
                    ensure_run_dir(run_dir)
                if ws_dir and eff.params.get("worktree"):
                    # Full review r2 P1-C: a code task's worktree is never created as a
                    # plain directory (a `workspace delete` raced the start); the next
                    # attempt re-attaches the kept branch.
                    if not os.path.lexists(os.path.join(ws_dir, ".git")):
                        self.log_error(f"Task worktree {ws_dir} is missing at start")
                elif ws_dir:
                    ensure_workspace_dir(ws_dir)
            elif eff.kind == "unlink_deliver_intent":
                # Full review r2 P1-D: the delivery is committed; its intent is done.
                try:
                    os.unlink(eff.params["path"])
                except OSError:
                    pass
            elif eff.kind == "create_lead_dir":
                # Best effort: re-created at the next set-lead (§10.1, m1c Q6).
                try:
                    ensure_workspace_dir(eff.params["path"])
                except OSError as e:
                    self.log_error(f"Lead dir not created: {e}")
                if eff.params.get("repo"):
                    # Repo project: the lead's detached repo/ worktree (§10.1, §8). Best
                    # effort like the dir; set-lead adds it again if it is missing.
                    try:
                        add_lead_repo_worktree(eff.params["repo"], eff.params["project_id"],
                                               eff.params["ws_root"], eff.params["agent_id"])
                    except (MeshError, OSError) as e:
                        self.log_error(f"Lead repo/ worktree not created: {e}")
            elif eff.kind == "terminate_lead_turn":
                self._terminate_lead_turn(eff.params["run_id"], eff.params.get("end_reason", "lead_superseded"))
            elif eff.kind == "cancel_project_merges":
                self._clear_project_merge_queue(eff.params["project_id"])
            elif eff.kind == "reconcile_merging_task":
                # Carbon P2-8 + R2 P2-9 + R3 P1-5: run heavy git/worktree inspection in a
                # worker thread when an event loop is running, track/return the future so
                # handle_client awaits asyncio.shield(fut) before responding, and keep any
                # deferred merge:<prj> lease held until reconciliation finishes.
                import sqlite3
                from mesh.core.db import transaction
                from mesh.daemon import merge

                tid = eff.params["task_id"]
                lease_key = eff.params.get("lease_key")
                lease_run_id = eff.params.get("run_id")

                def _cleanup_held_lease(
                    l_key: Optional[str] = lease_key,
                    l_run_id: Optional[str] = lease_run_id,
                ) -> None:
                    if not (l_key and l_run_id and self.db_conn):
                        return
                    try:
                        still_there = self.db_conn.execute(
                            "SELECT 1 FROM leases WHERE key = ? AND run_id = ?;",
                            (l_key, l_run_id),
                        ).fetchone()
                        if still_there is None:
                            return
                        with transaction(self.db_conn):
                            self.db_conn.execute(
                                "DELETE FROM leases WHERE key = ? AND run_id = ?;",
                                (l_key, l_run_id),
                            )
                    except Exception as lease_exc:  # noqa: BLE001
                        self.log_error(
                            f"Lease {l_key} for run {l_run_id} not released after "
                            f"reconcile: {type(lease_exc).__name__}: {lease_exc}"
                        )

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None and loop.is_running():
                    async def _bg_reconcile(
                        target_tid: str = tid,
                        l_key: Optional[str] = lease_key,
                        l_run_id: Optional[str] = lease_run_id,
                    ) -> None:
                        try:
                            if not self.db_conn:
                                return
                            for attempt in range(4):
                                t_row = self.db_conn.execute(
                                    "SELECT * FROM tasks WHERE id = ? AND status = 'merging';",
                                    (target_tid,),
                                ).fetchone()
                                if t_row is None:
                                    break
                                try:
                                    await merge.recover_merging_task_async(self.db_conn, self.home, t_row)
                                    break
                                except sqlite3.OperationalError as op_exc:
                                    msg = str(op_exc).lower()
                                    if attempt < 3 and ("locked" in msg or "busy" in msg):
                                        await asyncio.sleep(0.05 * (attempt + 1))
                                        continue
                                    raise
                            await self.notify_waiters()
                            self.wake_scheduler()
                        except Exception as e:  # noqa: BLE001 - logged and surfaced to shielded caller
                            self.log_error(
                                f"Task {target_tid} not reconciled after a forced "
                                f"merge release: {type(e).__name__}: {e}"
                            )
                            raise
                        finally:
                            _cleanup_held_lease(l_key, l_run_id)

                    bg_job = asyncio.ensure_future(_bg_reconcile())
                    bg_job.add_done_callback(lambda fut: fut.exception() if not fut.cancelled() else None)
                    track = getattr(self, "track_merge_job", None)
                    if callable(track):
                        track(bg_job)
                    pending_reconciles.append(bg_job)
                else:
                    try:
                        for attempt in range(4):
                            task = self.db_conn.execute(
                                "SELECT * FROM tasks WHERE id = ? AND status = 'merging';",
                                (tid,),
                            ).fetchone()
                            if task is None:
                                break
                            try:
                                merge.recover_merging_task(self.db_conn, self.home, task)
                                break
                            except sqlite3.OperationalError as op_exc:
                                msg = str(op_exc).lower()
                                if attempt < 3 and ("locked" in msg or "busy" in msg):
                                    time.sleep(0.05 * (attempt + 1))
                                    continue
                                raise
                    except Exception as e:  # noqa: BLE001 - the next boot decides it
                        self.log_error(
                            f"Task {tid} not reconciled after a forced "
                            f"merge release: {type(e).__name__}: {e}"
                        )
                    finally:
                        _cleanup_held_lease()
            elif eff.kind == "seen_release_forced":
                # r4e: after the commit of `run release --force-seen-unusable` only.
                from mesh.daemon import supervisor

                try:
                    supervisor.finish_forced_release(self.home, eff.params["run_id"],
                                                     eff.params.get("reason"), eff.params.get("actor"))
                except Exception as e:  # noqa: BLE001 - the run is closed; eviction cleans up
                    self.log_error(f"Seen hold of run {eff.params.get('run_id')} not forgotten: {e}")
            elif eff.kind == "delete_workspace":
                from mesh.core.gitops import run_git

                p = eff.params.get("path")
                raw_p = (eff.params.get("raw_path") or p or "").rstrip(os.sep)
                repo_p = eff.params.get("repo_path")
                try:
                    tasks_d = os.path.dirname(raw_p) if raw_p else ""
                    proj_d = os.path.dirname(tasks_d) if tasks_d else ""
                    root_d = os.path.dirname(proj_d) if proj_d else ""
                    if (
                        raw_p
                        and not os.path.islink(raw_p)
                        and not (tasks_d and os.path.islink(tasks_d))
                        and not (proj_d and os.path.islink(proj_d))
                        and not (root_d and os.path.islink(root_d))
                        and p
                        and not os.path.islink(p)
                        and os.path.isdir(p)
                    ):
                        rmtree_allow_readonly(p, boundary_real=os.path.realpath(root_d) if root_d else None)
                    if repo_p and os.path.isdir(repo_p):
                        try:
                            run_git(["worktree", "prune"], cwd=repo_p)
                        except Exception:
                            pass
                except Exception as e:
                    self.log_error(f"Workspace delete failed for {p}: {e}")
        return pending_reconciles

    def _terminate_lead_turn(self, run_id: str, end_reason: str) -> None:
        """Begin the §11.4 termination of a superseded lead turn (set-lead, §8).

        The termination loop finishes it; the supervisor then closes the run with
        `end_reason` and executes none of its actions.
        """
        from mesh.daemon import supervisor, terminate

        try:
            t = terminate.get(run_id)
            if t is not None:
                if t.end_reason == "leftovers":
                    t.end_reason = end_reason
                return
            run = self.db_conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
            if run is None or run["status"] not in ("starting", "running"):
                return
            pgid, _wst = supervisor.close_identity(run)
            if not (run["wrapper_pid"] or pgid):
                return
            supervisor._begin_termination(
                self.db_conn, self.home, run, end_reason, seen=supervisor.seen_for(run, self.home)
            )
        except Exception as e:  # noqa: BLE001 - see below
            # The supervisor watch pass terminates a superseded turn on its own: its
            # `lead_turn_termination_reason` returns `lead_superseded` once the run's
            # assignment is no longer current.
            self.log_error(f"Lead turn {run_id} termination not started: {type(e).__name__}: {e}")

    def _queued_merge_project_id(self, item: Mapping[str, Any]) -> str:
        pid = (
            item.get("project_id")
            or ((item.get("principal") or {}).get("scope") or {}).get("project_id")
            or (item.get("args") or {}).get("project_id")
        )
        if isinstance(pid, str) and pid:
            return pid
        if self.db_conn is not None:
            if item.get("run_id"):
                rrow = self.db_conn.execute(
                    "SELECT project_id FROM runs WHERE id = ?;", (item["run_id"],)
                ).fetchone()
                if rrow is not None and rrow["project_id"]:
                    return str(rrow["project_id"])
            tid = (item.get("args") or {}).get("task_id")
            if tid:
                trow = self.db_conn.execute(
                    "SELECT project_id FROM tasks WHERE id = ?;", (tid,)
                ).fetchone()
                if trow is not None and trow["project_id"]:
                    return str(trow["project_id"])
        return ""

    def _clear_project_merge_queue(self, project_id: str) -> None:
        q = self._project_merge_queues.pop(project_id, None)
        if not q:
            return
        for rem in list(q):
            if rem.get("request_id"):
                self._project_merge_inflight_reqs.discard(rem["request_id"])
        q.clear()

    async def _drain_project_lead_merges(self, project_id: str) -> None:
        try:
            q = self._project_merge_queues.get(project_id)
            while q:
                if self._stopping:
                    for rem in list(q):
                        if rem.get("request_id"):
                            self._project_merge_inflight_reqs.discard(rem["request_id"])
                    q.clear()
                    break
                if self.db_conn is not None:
                    prow = self.db_conn.execute(
                        "SELECT status FROM projects WHERE id = ?;", (project_id,)
                    ).fetchone()
                    if prow is not None and prow["status"] == "cancelled":
                        self._clear_project_merge_queue(project_id)
                        break
                item = q.popleft()
                try:
                    await self._run_lead_merge(item)
                finally:
                    if item.get("request_id"):
                        self._project_merge_inflight_reqs.discard(item["request_id"])
        finally:
            if not self._project_merge_queues.get(project_id):
                self._project_merge_queues.pop(project_id, None)
            if self._project_merge_workers.get(project_id) is asyncio.current_task():
                self._project_merge_workers.pop(project_id, None)

    def start_queued_lead_merges(self) -> None:
        """Start the `task.merge` actions queued by closed lead turns (Quest r10b, option b).

        Called on the event loop after a scheduler tick or a termination pass, never
        inside a transaction: a merge is started only once the close TX that queued it
        committed (its `lead.action_result` exists); one whose TX rolled back is dropped.
        Queued merges for the same project run strictly in FIFO order.
        """
        from mesh.daemon import lead_turns

        if not self.db_conn:
            return
        if self._stopping:
            # Full review P1: no new merge once shutdown began; the queued items stay
            # in memory and go with the process, as they did before.
            return
        items = lead_turns.take_queued_lead_merges()
        if not items:
            return
        if self.db_conn.in_transaction:
            lead_turns.requeue_lead_merges(items)
            return
        for item in items:
            try:
                committed = lead_turns.queued_lead_merge_committed(self.db_conn, item)
            except Exception as e:  # noqa: BLE001 - keep it for the next pass
                self.log_error(f"Queued lead merge check failed: {type(e).__name__}: {e}")
                lead_turns.requeue_lead_merges([item])
                continue
            if not committed:
                self.log_error(f"Queued lead merge of run {item['run_id']} dropped: its turn did not commit")
                continue
            req_id = item.get("request_id")
            if req_id and req_id in self._project_merge_inflight_reqs:
                continue
            project_id = self._queued_merge_project_id(item)
            q = self._project_merge_queues.setdefault(project_id, deque())
            if req_id:
                self._project_merge_inflight_reqs.add(req_id)
            q.append(item)
            worker = self._project_merge_workers.get(project_id)
            if worker is None or worker.done():
                task = asyncio.ensure_future(self._drain_project_lead_merges(project_id))
                self._project_merge_workers[project_id] = task
                self._lead_merge_tasks.add(task)
                task.add_done_callback(self._lead_merge_tasks.discard)

    async def _run_lead_merge(self, item: Dict[str, Any]) -> None:
        """One queued lead merge, through the socket's entry point (`handle_task_merge`).

        The principal is the lead run principal of the turn (`via='run_token'`), so the
        merge's TX1 authorizes it exactly as the CLI would (rc 9 if the lead was
        superseded meanwhile). The outcome is recorded by the merge's own events; a merge
        that never started (TX1 refused it) is one more `lead.action_result` with
        `ok:false`, i.e. a `lead.action_failed` message to the lead.
        """
        from mesh.core.db import transaction
        from mesh.daemon import lead_turns

        principal = dict(item["principal"])
        ctx = HandlerContext(
            conn=self.db_conn,
            principal=principal,
            actor_kind=principal["kind"],
            via="run_token",
            request_id=item["request_id"],
            home=self.home,
            cmd="task.merge",
            args=dict(item["args"]),
            testing_mode=self.testing_mode,
            credential_id=principal.get("credential_id"),
        )
        ctx.idempotency = None  # type: ignore[attr-defined]
        ctx._queued_lead_merge = True  # type: ignore[attr-defined]
        error: Optional[MeshError] = None
        try:
            await handle_task_merge(self, ctx)
        except MeshError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - reported like handle_client's rc 1
            error = MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                              message=_redact_msg(f"Daemon unexpected error: {exc}"))
        if self.db_conn.in_transaction:
            try:
                self.db_conn.execute("ROLLBACK;")
            except Exception:
                pass
        if error is None:
            return
        if "merge_id" in (error.details or {}):
            # The merge ran; its T16 / failure events already carry the outcome.
            self.log_error(f"Lead merge of {item['args'].get('task_id')} ended rc {error.exit_code} {error.code}")
            return
        try:
            with transaction(self.db_conn):
                lead_turns.record_queued_merge_failure(self.db_conn, item, error)
        except Exception as e:  # noqa: BLE001
            self.log_error(f"Lead merge failure not recorded: {type(e).__name__}: {e}")
        self.wake_scheduler()
        await self.notify_waiters()

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming client request over Unix socket.

        Blind review P2: at most ``MAX_CONNECTIONS`` connections are served at once (a
        connection over the limit gets a BUSY error and is closed), and a client must send
        its request line within ``INITIAL_READ_TIMEOUT_S``."""
        self.active_connections = getattr(self, "active_connections", 0) + 1
        me = asyncio.current_task()
        clients = getattr(self, "_client_tasks", None)
        if me is not None and clients is not None:
            clients.add(me)
            if hasattr(self, "_client_writers"):
                self._client_writers[me] = writer
        try:
            max_conn, _ = _conn_limits()
            if self.active_connections > max_conn:
                try:
                    writer.write((json.dumps({
                        "ok": False,
                        "error": {"code": c.CONCURRENCY_LIMIT, "message":
                                  f"meshd is serving {max_conn} connections; retry shortly",
                                  "details": {"max_connections": max_conn}},
                        "exit_code": c.EXIT_BUSY, "request_id": None}) + "\n").encode("utf-8"))
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                return
            await self._handle_client(reader, writer)
        finally:
            self.active_connections -= 1
            if me is not None and clients is not None:
                clients.discard(me)
                getattr(self, "_client_cmds", {}).pop(me, None)
                getattr(self, "_client_writers", {}).pop(me, None)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        sock = writer.get_extra_info("socket")
        start_mono = now_monotonic()

        # SO_PEERCRED check (C6)
        peer_pid, peer_uid, peer_gid = get_peer_credentials(sock)
        if peer_uid != os.getuid():
            # Reject immediately
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

        cmd = "unknown"
        request_id = new_id("req")
        principal_id = "unknown"
        exit_code = c.EXIT_OK
        dry_run = False

        try:
            try:
                line_bytes = await asyncio.wait_for(reader.readline(), timeout=_conn_limits()[1])
            except asyncio.TimeoutError:
                return  # idle client: closed in finally
            except (ValueError, asyncio.LimitOverrunError):
                # Drain remaining line data up to newline or EOF so writer.close() does not send RST
                # PR #40 r1: one overall deadline, so a slow sender cannot hold the slot.
                drain_deadline = time.monotonic() + OVERSIZE_DRAIN_S
                try:
                    while not reader.at_eof():
                        left = drain_deadline - time.monotonic()
                        if left <= 0:
                            break
                        chunk = await asyncio.wait_for(reader.read(65536), timeout=min(0.5, left))
                        if not chunk or b"\n" in chunk:
                            break
                except Exception:
                    pass
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Request payload exceeds 1 MiB limit",
                )
            if not line_bytes:
                return

            if len(line_bytes) > MAX_REQUEST_BYTES:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Request payload exceeds 1 MiB limit",
                )

            try:
                req = json.loads(line_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                raise MeshError(
                    code=c.INVALID_JSON,
                    exit_code=c.EXIT_USAGE,
                    message=f"Malformed request JSON: {exc}",
                ) from exc
            if not isinstance(req, dict):
                raise MeshError(
                    code=c.INVALID_JSON,
                    exit_code=c.EXIT_USAGE,
                    message="Request envelope must be a JSON object",
                )
            raw_request_id = req.get("request_id")
            if raw_request_id is not None:
                if (
                    not isinstance(raw_request_id, str)
                    or not IDEMPOTENCY_KEY_RE.fullmatch(raw_request_id)
                ):
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message="Field 'request_id' must match ^[A-Za-z0-9._:-]{1,128}$",
                        details={"param": "request_id"},
                    )
                request_id = redact(raw_request_id)
            cmd_val = req.get("cmd")
            if not isinstance(cmd_val, str) or not cmd_val:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'cmd' must be a non-empty string",
                    details={"param": "cmd"},
                )
            cmd = cmd_val
            _me = asyncio.current_task()
            if _me is not None and hasattr(self, "_client_cmds"):
                self._client_cmds[_me] = cmd
            auth_info = req.get("auth") if "auth" in req else {}
            if not isinstance(auth_info, dict):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'auth' must be a JSON object",
                    details={"param": "auth"},
                )
            args = req.get("args") if "args" in req else {}
            if not isinstance(args, dict):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'args' must be a JSON object",
                    details={"param": "args"},
                )
            dry_raw = req.get("dry_run", False)
            if dry_raw is not None and not isinstance(dry_raw, bool):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'dry_run' must be a boolean",
                    details={"param": "dry_run"},
                )
            dry_run = bool(dry_raw)
            replay_only_raw = req.get("replay_only", False)
            if replay_only_raw is not None and not isinstance(replay_only_raw, bool):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'replay_only' must be a boolean",
                    details={"param": "replay_only"},
                )
            replay_only = bool(replay_only_raw)
            approval_val = req.get("approval")
            if approval_val is not None and not isinstance(approval_val, str):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'approval' must be a string",
                    details={"param": "approval"},
                )
            relayed_raw = req.get("relayed")
            if relayed_raw is not None and not isinstance(relayed_raw, bool):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'relayed' must be a boolean",
                    details={"param": "relayed"},
                )
            idempotency_key = req.get("idempotency_key")
            if idempotency_key is not None and (
                not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_RE.match(idempotency_key)
            ):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="idempotency_key must match ^[A-Za-z0-9._:-]{1,128}$",
                    details={"param": "idempotency_key"},
                )
            if replay_only and not idempotency_key:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'replay_only' requires an idempotency_key",
                    details={"param": "idempotency_key"},
                )
            if replay_only and dry_run:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'replay_only' cannot be combined with 'dry_run'",
                    details={"param": "replay_only"},
                )

            # Authenticate
            principal, actor_kind, via, k_user = await self.authenticate_and_authorize(
                peer_pid=peer_pid,
                auth_info=auth_info,
                cmd=cmd,
                request_id=request_id,
            )
            principal_id = principal["id"]

            # User-only operations per plan §7.3: config.*, agent.*, audit.*
            # Any agent credential (with or without --relayed, with or without relay capability) gets exit 4 CAPABILITY_MISSING
            if cmd.startswith("config.") or cmd.startswith("agent.") or cmd.startswith("audit."):
                if actor_kind != "user" and not (cmd in ROSTER_READ_CMDS and actor_kind == "agent"
                                                 and not req.get("relayed")):
                    raise MeshError(
                        code=c.CAPABILITY_MISSING,
                        exit_code=c.EXIT_FORBIDDEN,
                        message=f"Operation '{cmd}' requires direct user authentication",
                        details={"required": "user_direct", "actor_kind": actor_kind, "cmd": cmd},
                    )

            if req.get("relayed"):
                if via != "token":
                    raise MeshError(
                        code=c.RELAY_NOT_ALLOWED,
                        exit_code=c.EXIT_FORBIDDEN,
                        message="Only agent tokens with relay capability can use --relayed",
                    )
                caps = principal.get("capabilities", [])
                if "relay" not in caps:
                    raise MeshError(
                        code=c.RELAY_NOT_ALLOWED,
                        exit_code=c.EXIT_FORBIDDEN,
                        message=f"Principal '{principal.get('name')}' lacks 'relay' capability",
                        details={"principal": principal.get("name"), "missing": "relay"},
                    )
                user_stmt = req.get("user_statement")
                if not isinstance(user_stmt, str) or not user_stmt.strip():
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message="Field '--user-statement' is required and non-empty when using --relayed",
                        details={"param": "user_statement"},
                    )
                if len(user_stmt) > 4000:
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message="User statement exceeds 4000 character limit",
                        details={"param": "user_statement"},
                    )
                via = "relayed"

            ev_ref = req.get("evidence_ref")
            if ev_ref is not None and (not isinstance(ev_ref, str) or len(ev_ref) > 4000):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'evidence_ref' must be a string up to 4000 characters",
                    details={"param": "evidence_ref"},
                )
            top_if_version = req.get("if_version")
            if top_if_version is not None and (
                isinstance(top_if_version, bool) or not isinstance(top_if_version, int)
            ):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Field 'if_version' must be an integer",
                    details={"param": "if_version"},
                )

            idem_pid = idempotency_partition(principal, via)

            # Central command-level authorization (§7.2, §7.3, §13.4) before idempotency
            # replay, long-poll/stream/merge dispatch, or any worker-thread/DB work.
            # Resource-scoped checks (`project_id` / `task_id`) run in handlers once IDs resolve.
            check_authz(
                self.db_conn,
                cmd,
                principal,
                actor_kind,
                via,
                relayed_by=principal["name"] if via == "relayed" else None,
                resource_resolved=False,
            )

            # Pre-validate before canonical hashing and idempotency check
            if cmd == "agent.create":
                # Absent: 1 (the handler applies the default). Present (even null): validated.
                if "max_concurrency" in args:
                    parse_positive_int(args["max_concurrency"], "max_concurrency")
            elif cmd == "agent.update":
                if "max_concurrency" in args:
                    parse_positive_int(args["max_concurrency"], "max_concurrency")

            # Idempotency check (§13.4, B5)
            cmd_and_args: Dict[str, Any] = {"cmd": cmd, "args": args}
            if req.get("approval"):
                cmd_and_args["approval"] = req["approval"]
            if via == "relayed" and req.get("user_statement") is not None:
                cmd_and_args["user_statement"] = req["user_statement"]
            if ev_ref is not None:
                cmd_and_args["evidence_ref"] = ev_ref
            if top_if_version is not None:
                cmd_and_args["if_version"] = top_if_version
            try:
                req_hash = hashlib.sha256(canonical_json(cmd_and_args)).hexdigest()
            except ValueError as ve:
                if cmd == "config.set":
                    raise MeshError(
                        code=c.INVALID_SETTING,
                        exit_code=c.EXIT_USAGE,
                        message=str(ve),
                        details={"key": args.get("key")},
                    )
                if "max_concurrency" in args and isinstance(args["max_concurrency"], float):
                    raise MeshError(
                        code=c.INVALID_CONCURRENCY,
                        exit_code=c.EXIT_USAGE,
                        message=f"max_concurrency must be a positive integer (1..9223372036854775807); got '{args['max_concurrency']}'",
                        details={"param": "max_concurrency", "got": args["max_concurrency"]},
                    )
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=str(ve),
                )

            if cmd == "project.deliver" and not dry_run and isinstance(args, dict):
                # Full review r4 P1-1: settle an earlier, lost delivery first (own
                # transaction), so a keyed retry of it replays its recorded result.
                from mesh.daemon.recovery import DeliverIntentUnresolved, settle_deliver_intent

                try:
                    settle_deliver_intent(self.db_conn, self.home, args.get("project_id"))
                except DeliverIntentUnresolved as e:
                    raise MeshError(
                        code=c.INTERNAL_ERROR,
                        exit_code=c.EXIT_INTERNAL,
                        message=f"An earlier delivery of this project cannot be checked: {e}; nothing was changed",
                        details={"reason": "git_failed", "step": "deliver intent"},
                    )

            if idempotency_key:
                if not (1 <= len(idempotency_key) <= 128):
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message="idempotency_key must be between 1 and 128 characters",
                    )
                # Blind review P3: a dry run never replays a real execution's stored result.
                irow = None if (dry_run or cmd in _NON_MUTATING_CMDS) else _lookup_live_idempotency(self.db_conn, idem_pid, idempotency_key)
                if irow:
                    if irow["request_hash"] == req_hash or _start_replay_ok(
                            cmd, args, req.get("approval"), idempotency_key,
                            irow["request_hash"], irow["response_json"],
                            via=via, user_statement=req.get("user_statement"),
                            evidence_ref=ev_ref, if_version=top_if_version):
                        stored_envelope = json.loads(irow["response_json"])
                        r_proj, r_task = _resolve_idempotency_scope(self.db_conn, cmd, args, stored_envelope)
                        check_authz(
                            self.db_conn,
                            cmd,
                            principal,
                            actor_kind,
                            via,
                            project_id=r_proj,
                            task_id=r_task,
                            relayed_by=principal["name"] if via == "relayed" else None,
                            resource_resolved=(r_proj is not None or r_task is not None),
                        )
                        check_approval_replay_authz(
                            self.db_conn, cmd, principal, actor_kind, via, args, stored_envelope
                        )
                        stored_envelope["idempotent_replay"] = True
                        writer.write((json.dumps(stored_envelope) + "\n").encode("utf-8"))
                        await writer.drain()
                        exit_code = irow["exit_code"]
                        return
                    else:
                        raise MeshError(
                            code=c.IDEMPOTENCY_KEY_REUSED,
                            exit_code=c.EXIT_CONFLICT,
                            message=f"Idempotency key '{idempotency_key}' reused with different request parameters",
                        )
                elif not dry_run or replay_only:
                    r_idem = (
                        find_ws_reserved_idempotency(self.db_conn, idem_pid, idempotency_key)
                        if not dry_run else None
                    )
                    if r_idem:
                        if r_idem.get("request_hash") == req_hash:
                            raise MeshError(
                                code=c.CONCURRENCY_LIMIT,
                                exit_code=c.EXIT_BUSY,
                                message=(
                                    f"Operation '{r_idem.get('cmd') or cmd}' with idempotency key "
                                    f"'{idempotency_key}' is already in progress"
                                ),
                                details={
                                    "idempotency_key": idempotency_key,
                                    "reservation": r_idem.get("reservation_key"),
                                },
                            )
                        raise MeshError(
                            code=c.IDEMPOTENCY_KEY_REUSED,
                            exit_code=c.EXIT_CONFLICT,
                            message=f"Idempotency key '{idempotency_key}' reused with different request parameters",
                        )
                    if replay_only:
                        raise MeshError(
                            code=c.INVALID_ARGUMENT,
                            exit_code=c.EXIT_USAGE,
                            message="No matching idempotent execution found for replay_only request",
                            details={"param": "idempotency_key", "reason": "replay_only_miss"},
                        )

            # Blind review P2: state-dependent validation runs after the replay lookup, so a
            # keyed request that already succeeded replays even if settings changed since.
            if cmd == "config.set":
                cur = self.db_conn.execute("SELECT key, value_json FROM settings;")
                current_settings = {r["key"]: json.loads(r["value_json"]) for r in cur.fetchall()}
                validate_setting(
                    key=args.get("key"),
                    val=args.get("value"),
                    home=self.home,
                    current_settings=current_settings,
                    db_conn=self.db_conn,
                    testing_mode=self.testing_mode,
                )

            # Handler context
            ctx = HandlerContext(
                conn=self.db_conn,
                principal=principal,
                actor_kind=actor_kind,
                via=via,
                request_id=request_id,
                home=self.home,
                cmd=cmd,
                args=args,
                dry_run=dry_run,
                k_user=k_user,
                testing_mode=self.testing_mode,
                approval=req.get("approval"),
                relayed_by=principal["name"] if via == "relayed" else None,
                user_statement=req.get("user_statement"),
                evidence_ref=req.get("evidence_ref"),
                if_version=req.get("if_version"),
                credential_id=principal.get("credential_id"),
            )

            # Long-poll wait commands do NOT hold the DB write lock (plan §12.3)
            if cmd in ("inbox.wait", "task.wait", "project.wait", "event.tail"):
                if ctx.dry_run:
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message=f"--dry-run is not supported on '{cmd}'",
                        details={"param": "dry_run"},
                    )

            if cmd in ("inbox.wait", "task.wait", "project.wait"):
                if cmd == "inbox.wait":
                    res = await handle_inbox_wait(self, ctx, reader)
                elif cmd == "task.wait":
                    res = await handle_task_wait(self, ctx, reader)
                elif cmd == "project.wait":
                    res = await handle_project_wait(self, ctx, reader)
                else:
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message=f"Unknown command: '{cmd}'",
                        details={"cmd": cmd},
                    )

                exit_code = res.exit_code
                envelope = {
                    "ok": True,
                    "data": res.data,
                    "warnings": res.warnings,
                    "request_id": request_id,
                    "idempotent_replay": False,
                    "dry_run": False,
                    "exit_code": exit_code,
                }
                writer.write((json.dumps(envelope) + "\n").encode("utf-8"))
                await writer.drain()
                return

            if cmd == "event.tail":
                await handle_event_tail_live(self, ctx, reader, writer)
                return

            # `task merge` (m2-r5, §10.5) and `workspace delete`/`prune` (PR #45 r2):
            # long requests that own their TX1 -> off-TX worker -> final TX lifecycle
            # instead of holding BEGIN IMMEDIATE across git/filesystem I/O.
            if cmd == "project.deliver":
                # Full review r2 P1-E: the deliver intent carries the key, so recovery can
                # store the replay of a delivery whose request transaction was lost.
                ctx.idempotency = (  # type: ignore[attr-defined]
                    {"principal_id": idem_pid, "key": idempotency_key, "request_hash": req_hash}
                    if idempotency_key and not dry_run else None
                )
            if cmd == "task.merge" or (cmd in ("workspace.delete", "workspace.prune") and not dry_run):
                # §13.4: merge.py and workspace delete/prune store idempotency inside
                # their own transaction, never in a separate transaction after commit.
                ctx.idempotency = (  # type: ignore[attr-defined]
                    {"principal_id": idem_pid, "key": idempotency_key, "request_hash": req_hash}
                    if idempotency_key and not dry_run else None
                )
                if cmd == "task.merge":
                    res = await handle_task_merge(self, ctx)
                elif cmd == "workspace.delete":
                    res = await run_handlers.run_workspace_delete(self, ctx)
                else:
                    res = await run_handlers.run_workspace_prune(self, ctx)
                exit_code = res.exit_code
                replay_env = getattr(res, "_idempotent_replay_envelope", None)
                if replay_env is not None:
                    writer.write((json.dumps(replay_env) + "\n").encode("utf-8"))
                    await writer.drain()
                    return
                if dry_run:
                    envelope = {
                        "ok": True,
                        "data": res.data,
                        "warnings": res.warnings,
                        "request_id": request_id,
                        "idempotent_replay": False,
                        "dry_run": True,
                        "exit_code": exit_code,
                    }
                else:
                    envelope = {
                        "ok": True,
                        "data": res.data,
                        "warnings": res.warnings,
                        "request_id": request_id,
                        "idempotent_replay": False,
                        "dry_run": False,
                        "exit_code": exit_code,
                    }
                    if cmd != "task.merge":
                        self.execute_effects(ctx.effects)
                        await self.notify_waiters()
                    # PR42 r2: no tick (promotion, launches, lead turns) once stopping;
                    # the next boot's scheduler does it.
                    if not getattr(self, "_stopping", False):
                        try:
                            run_scheduler_tick(self.db_conn, self.home, testing_mode=self.testing_mode)
                        except Exception as e:
                            _sweep_error(self.home, "tick_post_merge" if cmd == "task.merge" else "tick_post_commit", e)
                    if cmd == "task.merge" or not getattr(self, "_stopping", False):
                        self.start_queued_lead_merges()
                        self.wake_scheduler()
                        await self.notify_waiters()
                writer.write((json.dumps(envelope) + "\n").encode("utf-8"))
                await writer.drain()
                return

            # Code tasks (m2-r4): the `task start` worktree (§10.3) and the `task submit`
            # git info (§10.4) run git in a worker thread before the request
            # transaction, never on the event loop (review m2-r3 finding 5).
            if cmd == "task.start":
                ctx.idempotency = (  # type: ignore[attr-defined]
                    {"principal_id": idem_pid, "key": idempotency_key, "request_hash": req_hash}
                    if idempotency_key and not dry_run else None
                )
                await prepare_task_start_workspace(ctx)
            elif cmd == "task.submit":
                await prepare_task_submit_git(ctx)
            elif cmd == "workspace.prune" and dry_run:
                prune_plan = run_handlers.plan_workspace_prune(ctx)
                ctx.prepared_prune = await asyncio.to_thread(
                    run_handlers.execute_workspace_prune_plan,
                    prune_plan,
                )
            elif cmd == "task.close":
                from mesh.daemon.handlers.task import prepare_task_close

                await prepare_task_close(ctx)
            elif cmd in ADAPTER_CMDS:
                await prepare_adapter_command(ctx, self.testing_mode)
            elif cmd in ("run.log", "task.log"):
                await run_handlers.prepare_log_command(ctx)

            # Transaction execution
            if True:
                self.db_conn.execute("BEGIN IMMEDIATE;")
                if (
                    cmd in ("task.start", "task.submit", "task.close", "run.log", "task.log")
                    or (cmd == "workspace.prune" and dry_run)
                    or cmd in ADAPTER_CMDS
                ):
                    recheck_tx_credential_and_authz(ctx)

                # Linear full review B1: the cache was read before the awaits above
                # (workspace/git preparation); an identical keyed request may have finished
                # meanwhile. Re-read it under the write lock and replay it, so overlapping
                # retries get the original response instead of INVALID_TRANSITION.
                if idempotency_key and not dry_run and cmd not in _NON_MUTATING_CMDS:
                    irow = _lookup_live_idempotency(self.db_conn, idem_pid, idempotency_key)
                    if irow:
                        self.db_conn.execute("ROLLBACK;")
                        if irow["request_hash"] == req_hash or _start_replay_ok(
                                cmd, args, req.get("approval"), idempotency_key,
                                irow["request_hash"], irow["response_json"],
                                via=via, user_statement=req.get("user_statement"),
                                evidence_ref=ev_ref, if_version=top_if_version):
                            stored_envelope = json.loads(irow["response_json"])
                            r_proj, r_task = _resolve_idempotency_scope(self.db_conn, cmd, args, stored_envelope)
                            check_authz(
                                self.db_conn,
                                cmd,
                                principal,
                                actor_kind,
                                via,
                                project_id=r_proj,
                                task_id=r_task,
                                relayed_by=principal["name"] if via == "relayed" else None,
                                resource_resolved=(r_proj is not None or r_task is not None),
                            )
                            check_approval_replay_authz(
                                self.db_conn, cmd, principal, actor_kind, via, args, stored_envelope
                            )
                            stored_envelope["idempotent_replay"] = True
                            writer.write((json.dumps(stored_envelope) + "\n").encode("utf-8"))
                            await writer.drain()
                            exit_code = irow["exit_code"]
                            return
                        raise MeshError(
                            code=c.IDEMPOTENCY_KEY_REUSED,
                            exit_code=c.EXIT_CONFLICT,
                            message=f"Idempotency key '{idempotency_key}' reused with different request parameters",
                        )
                    r_idem = find_ws_reserved_idempotency(self.db_conn, idem_pid, idempotency_key)
                    if r_idem:
                        self.db_conn.execute("ROLLBACK;")
                        if r_idem.get("request_hash") == req_hash:
                            raise MeshError(
                                code=c.CONCURRENCY_LIMIT,
                                exit_code=c.EXIT_BUSY,
                                message=(
                                    f"Operation '{r_idem.get('cmd') or cmd}' with idempotency key "
                                    f"'{idempotency_key}' is already in progress"
                                ),
                                details={
                                    "idempotency_key": idempotency_key,
                                    "reservation": r_idem.get("reservation_key"),
                                },
                            )
                        raise MeshError(
                            code=c.IDEMPOTENCY_KEY_REUSED,
                            exit_code=c.EXIT_CONFLICT,
                            message=f"Idempotency key '{idempotency_key}' reused with different request parameters",
                        )
                    if replay_only:
                        self.db_conn.execute("ROLLBACK;")
                        raise MeshError(
                            code=c.INVALID_ARGUMENT,
                            exit_code=c.EXIT_USAGE,
                            message="No matching idempotent execution found for replay_only request",
                            details={"param": "idempotency_key", "reason": "replay_only_miss"},
                        )

                # Dispatch
                res: HandlerResult
                if cmd == "daemon.status":
                    daemon_info = {
                        "status": "running",
                        "pid": os.getpid(),
                        "uptime_s": int(time.time() - self.start_time),
                        "boot_id": self.boot_id,
                        "testing_mode": self.testing_mode,
                        "active_runs": len(self.active_runs),
                        "active_streams": self.active_streams,
                        "active_waits": self.active_waits,
                        "kill_failed_runs": _kill_failed_runs(),
                        "scheduler": scheduler_errors_view(),
                    }
                    res = handle_daemon_status(ctx, daemon_info)
                elif cmd == "daemon.stop":
                    res = handle_daemon_stop(ctx)
                elif cmd == "whoami":
                    res = handle_whoami(ctx)
                elif cmd == "version":
                    res = handle_version(ctx)
                elif cmd == "config.list":
                    res = handle_config_list(ctx)
                elif cmd == "config.get":
                    res = handle_config_get(ctx)
                elif cmd == "config.set":
                    res = handle_config_set(ctx)
                elif cmd == "agent.create":
                    res = handle_agent_create(ctx)
                elif cmd == "agent.update":
                    res = handle_agent_update(ctx)
                elif cmd == "agent.show":
                    res = handle_agent_show(ctx)
                elif cmd == "agent.list":
                    res = handle_agent_list(ctx)
                elif cmd == "agent.disable":
                    res = handle_agent_disable(ctx)
                elif cmd == "agent.enable":
                    res = handle_agent_enable(ctx)
                elif cmd == "agent.token.issue":
                    res = handle_agent_token_issue(ctx)
                elif cmd == "agent.token.list":
                    res = handle_agent_token_list(ctx)
                elif cmd == "agent.token.revoke":
                    res = handle_agent_token_revoke(ctx)
                elif cmd == "adapter.list":
                    res = handle_adapter_list(ctx, self.testing_mode)
                elif cmd == "adapter.show":
                    res = handle_adapter_show(ctx, self.testing_mode)
                elif cmd == "adapter.probe":
                    res = handle_adapter_probe(ctx, self.testing_mode)
                elif cmd == "adapter.models":
                    res = handle_adapter_models(ctx, self.testing_mode)
                elif cmd == "intake.submit":
                    res = handle_intake_submit(ctx)
                elif cmd == "project.create":
                    res = handle_project_create(ctx)
                elif cmd == "project.list":
                    res = handle_project_list(ctx)
                elif cmd == "project.show":
                    res = handle_project_show(ctx)
                elif cmd == "project.leads":
                    res = handle_project_leads(ctx)
                elif cmd == "project.set_lead":
                    res = handle_project_set_lead(ctx)
                elif cmd == "project.pause":
                    res = handle_project_pause(ctx)
                elif cmd == "project.resume":
                    res = handle_project_resume(ctx)
                elif cmd == "project.cancel":
                    res = handle_project_cancel(ctx)
                elif cmd == "project.update":
                    res = handle_project_update(ctx)
                elif cmd == "project.complete":
                    res = handle_project_complete(ctx)
                elif cmd == "project.note":
                    res = handle_project_note(ctx)
                elif cmd == "project.deliver":
                    res = handle_project_deliver(ctx)
                elif cmd == "inbox.list":
                    res = handle_inbox_list(ctx)
                elif cmd == "inbox.ack":
                    res = handle_inbox_ack(ctx)
                elif cmd == "event.list":
                    res = handle_event_list(ctx)
                elif cmd == "approval.request":
                    res = handle_approval_request(ctx)
                elif cmd == "approval.approve":
                    res = handle_approval_approve(ctx)
                elif cmd == "approval.reject":
                    res = handle_approval_reject(ctx)
                elif cmd == "approval.cancel":
                    res = handle_approval_cancel(ctx)
                elif cmd == "approval.check":
                    res = handle_approval_check(ctx)
                elif cmd == "approval.consume":
                    res = handle_approval_consume(ctx)
                elif cmd == "approval.show":
                    res = handle_approval_show(ctx)
                elif cmd == "approval.list":
                    res = handle_approval_list(ctx)
                elif cmd == "task.create":
                    res = handle_task_create(ctx)
                elif cmd == "task.assign":
                    res = handle_task_assign(ctx)
                elif cmd == "task.reassign":
                    res = handle_task_reassign(ctx)
                elif cmd == "task.retry":
                    res = handle_task_retry(ctx)
                elif cmd == "task.escalate":
                    res = handle_task_escalate(ctx)
                elif cmd == "task.show":
                    res = handle_task_show(ctx)
                elif cmd == "task.log":
                    res = handle_task_log(ctx)
                elif cmd == "run.list":
                    res = handle_run_list(ctx)
                elif cmd == "run.show":
                    res = handle_run_show(ctx)
                elif cmd == "run.log":
                    res = handle_run_log(ctx)
                elif cmd == "run.release":
                    res = handle_run_release(ctx)
                elif cmd == "workspace.list":
                    res = handle_workspace_list(ctx)
                elif cmd == "workspace.show":
                    res = handle_workspace_show(ctx)
                elif cmd == "workspace.prune":
                    res = handle_workspace_prune(ctx)
                elif cmd == "workspace.delete":
                    res = handle_workspace_delete(ctx)
                elif cmd == "task.list":
                    res = handle_task_list(ctx)
                elif cmd == "task.cancel":
                    res = handle_task_cancel(ctx)
                elif cmd == "task.close":
                    res = handle_task_close(ctx)
                elif cmd == "task.start":
                    res = handle_task_start(ctx)
                elif cmd == "task.heartbeat":
                    res = handle_task_heartbeat(ctx)
                elif cmd == "task.progress":
                    res = handle_task_progress(ctx)
                elif cmd == "task.submit":
                    res = handle_task_submit(ctx)
                elif cmd == "task.fail":
                    res = handle_task_fail(ctx)
                elif cmd == "task.accept":
                    res = handle_task_accept(ctx)
                elif cmd == "task.reject":
                    res = handle_task_reject(ctx)
                elif cmd == "dep.add":
                    res = handle_dep_add(ctx)
                elif cmd == "dep.remove":
                    res = handle_dep_remove(ctx)
                elif cmd == "dep.list":
                    res = handle_dep_list(ctx)
                elif cmd == "audit.verify":
                    res = handle_audit_verify(ctx)
                elif cmd in PLANNED_COMMAND_MILESTONES:
                    ms = PLANNED_COMMAND_MILESTONES[cmd]
                    details = {"cmd": cmd}
                    if ms is not None:
                        details["milestone"] = ms
                    raise MeshError(
                        code=c.NOT_IMPLEMENTED_IN_M1,
                        exit_code=c.EXIT_USAGE,
                        message=f"Command '{cmd}' is not available in this version of Agent Mesh",
                        details=details,
                    )
                else:
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message=f"Unknown command: '{cmd}'",
                        details={"cmd": cmd},
                    )

                exit_code = res.exit_code

                if dry_run:
                    self.db_conn.execute("ROLLBACK;")
                    envelope = {
                        "ok": True,
                        "data": {
                            "would": {
                                "transitions": res.transitions,
                                "events": res.events,
                                "effects": [eff.kind for eff in ctx.effects],
                            }
                        },
                        "warnings": res.warnings,
                        "request_id": request_id,
                        "idempotent_replay": False,
                        "dry_run": True,
                        "exit_code": exit_code,
                    }
                else:
                    # Store idempotency if key provided and exit code not in (1, 7, 10). The one
                    # exit-1 exception (plan §13.4): a post-commit reconcile failure below rewrites
                    # this row with its error envelope in a separate tx, because the command is
                    # already committed and must never run again.
                    envelope = {
                        "ok": True,
                        "data": res.data,
                        "warnings": res.warnings,
                        "request_id": request_id,
                        "idempotent_replay": False,
                        "dry_run": False,
                        "exit_code": exit_code,
                    }
                    idem_stored = False
                    if idempotency_key and cmd not in _NON_MUTATING_CMDS and exit_code not in (c.EXIT_INTERNAL, c.EXIT_APPROVAL_REQUIRED, c.EXIT_DAEMON_UNAVAILABLE):
                        _store_idempotency_row(
                            self.db_conn,
                            idem_pid,
                            idempotency_key,
                            cmd,
                            req_hash,
                            json.dumps(_idempotency_record(cmd, envelope)),
                            exit_code,
                        )
                        idem_stored = True

                    self.db_conn.execute("COMMIT;")
                    for fut in self.execute_effects(ctx.effects):
                        try:
                            await asyncio.shield(fut)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            ctx._committed_side_effect = True  # type: ignore[attr-defined]
                            err = exc if isinstance(exc, MeshError) else MeshError(
                                code=c.INTERNAL_ERROR,
                                exit_code=c.EXIT_INTERNAL,
                                message=_redact_msg(f"Post-commit reconcile failed: {exc}"),
                                details={
                                    "reason": "reconcile_failed",
                                    "committed": True,
                                    "hint": ("the command is committed; do not retry with a new key; "
                                             "the task is reconciled at the next meshd start"),
                                },
                            )
                            # Devin full-review P2-2: the keyed request is committed, so its
                            # replay must return this same error, not the ok stored above.
                            if idem_stored:
                                self._rewrite_idempotency_response(
                                    idem_pid, idempotency_key, cmd,
                                    err.to_envelope(request_id=request_id), err.exit_code,
                                )
                            if err is exc:
                                raise
                            raise err from exc
                    await self.notify_waiters()
                    if not dry_run and exit_code == 0 and not getattr(self, "_stopping", False):
                        try:
                            run_scheduler_tick(self.db_conn, self.home, testing_mode=self.testing_mode)
                        except Exception as e:
                            _sweep_error(self.home, "tick_post_commit", e)
                        self.start_queued_lead_merges()
                        self.wake_scheduler()
                        await self.notify_waiters()

            writer.write((json.dumps(envelope) + "\n").encode("utf-8"))
            await writer.drain()

        except MeshError as exc:
            exit_code = exc.exit_code
            if self.db_conn and self.db_conn.in_transaction:
                try:
                    if exit_code == c.EXIT_APPROVAL_REQUIRED and not dry_run:
                        self.db_conn.execute("COMMIT;")
                    else:
                        self.db_conn.execute("ROLLBACK;")
                except Exception:
                    pass
            if getattr(locals().get("ctx"), "_committed_side_effect", False):
                await self.notify_waiters()
                self.wake_scheduler()
            err_envelope = exc.to_envelope(request_id=request_id)
            if dry_run:
                err_envelope["dry_run"] = True
            try:
                writer.write((json.dumps(err_envelope) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception:
                pass
        except Exception as exc:
            exit_code = c.EXIT_INTERNAL
            if self.db_conn and self.db_conn.in_transaction:
                try:
                    self.db_conn.execute("ROLLBACK;")
                except Exception:
                    pass
            err_envelope = {
                "ok": False,
                "error": {
                    "code": c.INTERNAL_ERROR,
                    "message": _redact_msg(f"Daemon unexpected error: {exc}"),
                    "details": {},
                },
                "exit_code": c.EXIT_INTERNAL,
                "request_id": request_id,
            }
            try:
                writer.write((json.dumps(err_envelope) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception:
                pass
        finally:
            dur_ms = (now_monotonic() - start_mono) * 1000.0
            self.log_request(cmd, principal_id, request_id, exit_code, dur_ms)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _drain_for_shutdown(self, grace_s: float = 2.0, settle_s: float = 10.0) -> None:
        """Full review P1 (daemon-rest): settle all in-flight work before the lock goes.

        Order: no new lead merges (`_stopping`), give socket handlers a short grace
        (the `daemon stop` caller is still writing its response), cancel long polls and
        unread requests, then await every other handler, merge pipeline and queued lead
        merge to completion: their
        worker threads run git and the test command in the integration worktree, and
        their final transaction needs the open SQLite connection. Only after this does
        `run_server` record the stop, release meshd.lock and close the database, so a new
        meshd cannot run boot recovery against a merge that is still running.
        """
        self._stopping = True
        me = asyncio.current_task()
        # PR42 r2: let handler tasks of connections accepted just before close() start
        # and register before the snapshot.
        for _ in range(3):
            await asyncio.sleep(0)

        def cancel_polls(tasks):
            hit = [t for t in tasks if not t.done()
                   and self._client_cmds.get(t, "unknown") in _CANCEL_ON_STOP]
            for t in hit:
                # PR42 Linear r2: abort first, so the handler's cleanup (wait_closed on
                # a client that stopped reading) cannot block the gather below.
                w = self._client_writers.get(t)
                try:
                    if w is not None:
                        w.transport.abort()
                except Exception:
                    pass
                t.cancel()
            return hit

        clients = [t for t in self._client_tasks if t is not me and not t.done()]
        if clients:
            _done, pending = await asyncio.wait(clients, timeout=grace_s)
            # PR42 r1: cancel only long polls and connections whose request was never
            # read. Every other handler (task.merge parked on its shielded job, task.start
            # or task.submit in a worker thread, ...) is awaited below so its caller gets
            # the real response and no worker thread outlives meshd.lock.
            cancel = cancel_polls(pending)
            if cancel:
                await asyncio.gather(*cancel, return_exceptions=True)
        idle_since: Optional[float] = None
        aborted: Set[asyncio.Task] = set()
        while True:
            jobs = [j for j in (self._merge_jobs | self._lead_merge_tasks) if not j.done()]
            busy = [t for t in self._client_tasks if t is not me and not t.done()]
            # PR42 Linear r1: once no merge is left, handlers get settle_s to deliver
            # their responses; after that their connections are aborted, so a client
            # that never reads cannot block shutdown in drain(). A handler still in a
            # worker thread keeps being awaited (its late write just fails).
            if jobs:
                idle_since = None
            elif busy:
                now = time.monotonic()
                if idle_since is None:
                    idle_since = now
                elif now - idle_since >= settle_s:
                    for t in busy:
                        if t in aborted:
                            continue
                        aborted.add(t)
                        w = self._client_writers.get(t)
                        try:
                            if w is not None:
                                w.transport.abort()
                        except Exception:
                            pass
            if not jobs and not busy:
                # One more turn: a handler created in this iteration registers on its
                # first step (PR42 r2).
                await asyncio.sleep(0)
                if not any(t is not me and not t.done() for t in self._client_tasks):
                    break
                continue
            # PR42 r2: late handlers get the same policy (a poll would hold shutdown).
            cancel_polls(busy)
            try:
                self.log_error(f"meshd stopping: waiting for {len(jobs)} in-flight merge(s) "
                               f"and {len(busy)} request(s)")
            except Exception:
                pass
            # Not cancelled: a cancelled to_thread() await would not stop the thread.
            # Bounded wait so newly registered late handlers are re-checked.
            await asyncio.wait(jobs + busy, timeout=1.0, return_when=asyncio.ALL_COMPLETED)
        # A cancelled handler may have left a transaction open; the stop record needs none.
        if self.db_conn is not None and self.db_conn.in_transaction:
            try:
                self.db_conn.execute("ROLLBACK;")
            except Exception:
                pass
        if self.db_conn is not None:
            from mesh.daemon import supervisor

            abort_deadline = time.monotonic() + max(0.5, float(grace_s))
            while supervisor._SPAWN_ABORTS and time.monotonic() < abort_deadline:
                try:
                    supervisor.retry_spawn_aborts(self.db_conn, self.home, testing_mode=self.testing_mode)
                except Exception as exc:  # noqa: BLE001
                    self.log_error(f"Spawn abort drain error: {type(exc).__name__}: {exc}")
                if not supervisor._SPAWN_ABORTS:
                    break
                await asyncio.sleep(0.05)

    async def run_server(self) -> None:
        """Run the daemon server loop."""
        self.start_time = time.time()
        self.stop_event = asyncio.Event()
        sock_path = os.path.join(self.home, "run", "mesh.sock")
        sock_bound = False
        old_umask = os.umask(0o077)

        try:
            # Check setup and lock
            self.setup_storage()
            self.acquire_lock()
            self.record_boot()
            self.setup_subreaper()
            self.run_recovery()

            if os.path.exists(sock_path):
                try:
                    os.remove(sock_path)
                except OSError:
                    pass

            sock_bound = True
            self.server = await asyncio.start_unix_server(
                self.handle_client,
                path=sock_path,
                backlog=64,
                limit=MAX_REQUEST_BYTES + 4096,
            )
            os.chmod(sock_path, 0o600)

            loop = asyncio.get_running_loop()
            self._scheduler_wake = asyncio.Event()
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            self._termination_task = asyncio.create_task(self._termination_loop())
            stub_s = os.environ.get("MESH_TEST_STUB_CHILD_S")
            if self.testing_mode and stub_s:
                try:
                    self._stub_task = asyncio.create_task(self._stub_child(float(stub_s)))
                except ValueError:
                    self.log_error("MESH_TEST_STUB_CHILD_S is not a number; ignored")

            def on_signal() -> None:
                if self.stop_event:
                    self.stop_event.set()

            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, on_signal)
                except NotImplementedError:
                    pass

            await self.stop_event.wait()
        finally:
            try:
                # Graceful shutdown
                for bg_task in (self._scheduler_task, self._termination_task, self._stub_task):
                    if bg_task:
                        bg_task.cancel()
                        try:
                            await bg_task
                        except (asyncio.CancelledError, Exception):
                            pass

                if self.server:
                    # Stop accepting first. On Python 3.11 wait_closed() does not wait for
                    # the connection handlers, so they are drained below.
                    self.server.close()
                if self.lock_fd is not None or self.server is not None:
                    await self._drain_for_shutdown()
                if self.server:
                    try:
                        await asyncio.wait_for(self.server.wait_closed(), timeout=5.0)
                    except (asyncio.TimeoutError, Exception):
                        pass

                if sock_bound and os.path.exists(sock_path):
                    try:
                        os.remove(sock_path)
                    except OSError:
                        pass

                self.record_stop()
                self.release_lock()
                if self.db_conn:
                    self.db_conn.close()
                    self.db_conn = None
            finally:
                os.umask(old_umask)
