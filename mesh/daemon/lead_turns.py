"""Lead turns for launch-mode leads (plan §12.4, §12.6; checklist L1, L3, L6, K13).

A lead turn is a headless run of the lead agent, started by the scheduler tick when
the lead has pending `lead_wake` messages. The turn is one run row (`kind='lead_turn'`)
that holds the `leadturn:<prj>` lease, so a project never has two turns at once.

Trigger (§12.4, Q8): a launch-mode lead; pending `lead_wake` messages whose
`next_attempt_at <= now`, at least one of a type other than `lead.action_failed`
(an action failure alone must not wake the lead again, or a failing action would
loop forever); the oldest of them at least `lead.wake_debounce_s` old, or at least
`LEAD_WAKE_BATCH_TRIGGER` of them pending; the `leadturn:<prj>` lease free; the
project active.

The prompt is rendered by `mesh.core.lead_prompt.render_lead_prompt` (r2) with
`limit_bytes = <lead adapter>.max_argv_prompt_bytes`, and exactly
`rendered.included_message_ids` are marked `delivering` in the same transaction that
creates the run and takes the lease. Messages that did not fit stay `pending` for the
next turn (L6).

Exit handling lives in `finish_lead_turn`, called by the supervisor's watch pass for
`kind='lead_turn'` runs only (never T7/T8): exit 0 acks the messages; anything else
puts them back to `pending` with `attempts += 1` and the §12.4 backoff, and after
`outbox.max_attempts` they go `dead` with one `lead.unreachable` event (L3).
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

import mesh.constants as c
from mesh.adapters.base import AdapterUnsupported, RunRequest, TestDoubleDisabled
from mesh.adapters.registry import get_adapter
from mesh.core.auth import generate_run_token
from mesh.core.authz import get_agent_capabilities, get_project_active_lead
from mesh.core.clock import now_iso_utc, parse_utc
from mesh.core.db import transaction
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.core.lead_prompt import LeadPromptInput, render_lead_prompt
from mesh.core.lead_actions import (
    ERR_CMD_NOT_ALLOWED,
    MAX_ACTIONS,
    action_idempotency_key,
    action_request_args,
    parse_actions,
    parse_final_actions_block,
)
from mesh.core.leases import get_lease_ttl_s
from mesh.core.paths import get_install_root
from mesh.core.prompts import PROMPT_LIMIT_BYTES, pointer_prompt, prompt_strategy
from mesh.core.redact import redact_value, register_secret, unregister_secret
from mesh.core.repos import add_lead_repo_worktree, integration_branch_name, lead_repo_worktree_path
from mesh.core.settings import LEAD_WAKE_BACKOFF_S
from mesh.core.workspaces import (
    compute_lead_workspace_path,
    ensure_run_dir,
    ensure_workspace_dir,
    get_workspaces_root,
)
from mesh.daemon.handlers.base import HandlerContext
from mesh.errors import MeshError

#: §12.4: at most this many messages go into one turn.
LEAD_WAKE_BATCH_MAX = 50

#: §12.4: this many pending messages trigger a turn without waiting for the debounce.
LEAD_WAKE_BATCH_TRIGGER = 20

#: §12.4: the lead prompt's own cap, used when the adapter has no smaller one.
LEAD_PROMPT_LIMIT_BYTES = PROMPT_LIMIT_BYTES

#: The message type that never triggers a turn by itself (Q8).
ACTION_FAILED_TYPE = "lead.action_failed"

#: Run-token expiry grace after the deadline (§7.1).
RUN_TOKEN_EXPIRY_GRACE_S = 300

#: §12.4 default lead profile and turn timeout.
DEFAULT_LEAD_PROFILE = "readonly"
DEFAULT_LEAD_TURN_TIMEOUT_S = 1800

#: How many recent events / notes the prompt carries (§12.6).
PROMPT_EVENT_LIMIT = 200
PROMPT_NOTE_LIMIT = 100


def _setting(conn: sqlite3.Connection, key: str, default: Any) -> Any:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?;", (key,)).fetchone()
    if row is None or row["value_json"] is None:
        return default
    try:
        value = json.loads(row["value_json"])
    except (ValueError, TypeError):
        return default
    return default if value is None else value


def lead_wake_backoff_s(conn: sqlite3.Connection, testing_mode: bool = False) -> List[int]:
    """The retry backoff of §12.4 (30/120/600 s), overridable in testing mode only (Q9).

    Review m2-r9 finding 6: outside testing mode the production constant is returned
    without reading the DB, exactly like `auth.lockout_window_s` at read time, so a
    home configured in testing mode cannot carry a short backoff into production.
    """
    if not testing_mode:
        return list(LEAD_WAKE_BACKOFF_S)
    value = _setting(conn, "lead.wake_backoff_s", list(LEAD_WAKE_BACKOFF_S))
    if (isinstance(value, list) and len(value) == len(LEAD_WAKE_BACKOFF_S)
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
        return [int(v) for v in value]
    return list(LEAD_WAKE_BACKOFF_S)


def _log(home: str, msg: str) -> None:
    from mesh.daemon.scheduler import _log_to_meshd

    _log_to_meshd(home, msg)


def _lease_free(conn: sqlite3.Connection, project_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"leadturn:{project_id}",)).fetchone()
    return row is None


def _active_lead_turn(conn: sqlite3.Connection, project_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs WHERE kind = 'lead_turn' AND project_id = ? "
        "AND status IN ('starting', 'running') ORDER BY started_at ASC, id ASC LIMIT 1;",
        (project_id,),
    ).fetchone()


def pending_lead_wake(conn: sqlite3.Connection, project_id: str, agent_id: str,
                      now: str) -> List[sqlite3.Row]:
    """Pending `lead_wake` messages of this lead that are due, oldest first (§12.4)."""
    return conn.execute(
        """SELECT * FROM outbox
           WHERE recipient_id = ? AND project_id = ? AND channel = 'lead_wake'
             AND status = 'pending' AND next_attempt_at <= ?
           ORDER BY event_seq ASC, id ASC;""",
        (agent_id, project_id, now),
    ).fetchall()


def should_start_turn(conn: sqlite3.Connection, project_id: str, lead: Mapping[str, Any],
                      now: str) -> Optional[List[sqlite3.Row]]:
    """The messages a turn would carry, or None when the trigger does not fire (§12.4)."""
    if lead.get("delivery") != "launch":
        return None
    project = conn.execute("SELECT status FROM projects WHERE id = ?;", (project_id,)).fetchone()
    if project is None or project["status"] != "active":
        return None
    if not _lease_free(conn, project_id):
        return None
    if _active_lead_turn(conn, project_id) is not None:
        return None

    messages = pending_lead_wake(conn, project_id, lead["agent_id"], now)
    if not messages:
        return None
    # Q8: an action failure alone never triggers a turn.
    if not any(m["type"] != ACTION_FAILED_TYPE for m in messages):
        return None
    if len(messages) >= LEAD_WAKE_BATCH_TRIGGER:
        return messages
    debounce_s = int(_setting(conn, "lead.wake_debounce_s", 5) or 0)
    oldest = messages[0]["created_at"]
    try:
        age = parse_utc(now).timestamp() - parse_utc(oldest).timestamp()
    except (ValueError, TypeError):
        return messages
    if age >= debounce_s:
        return messages
    return None


# ---------------------------------------------------------------------------
# The prompt input (§12.6), built from the DB.
# ---------------------------------------------------------------------------


def _criteria(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, text FROM acceptance_criteria WHERE project_id = ? AND superseded_at IS NULL "
        "ORDER BY ordinal ASC, id ASC;",
        (project_id,),
    ).fetchall()
    return [{"id": r["id"], "text": r["text"]} for r in rows]


def _tasks(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT t.id, t.title, t.status, p.name AS assignee
           FROM tasks t LEFT JOIN principals p ON t.assignee_agent_id = p.id
           WHERE t.project_id = ?
           ORDER BY t.created_at ASC, t.id ASC;""",
        (project_id,),
    ).fetchall()
    return [{"id": r["id"], "title": r["title"], "status": r["status"], "assignee": r["assignee"]}
            for r in rows]


def _approvals(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, category, summary FROM approvals WHERE project_id = ? AND status = 'pending' "
        "ORDER BY requested_at ASC, id ASC;",
        (project_id,),
    ).fetchall()
    return [{"id": r["id"], "category": r["category"], "summary": r["summary"]} for r in rows]


def _notes(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    """The project's last notes, oldest first (§12.6)."""
    rows = conn.execute(
        """SELECT ts, payload_json FROM events
           WHERE project_id = ? AND type = 'project.note'
           ORDER BY seq DESC LIMIT ?;""",
        (project_id, PROMPT_NOTE_LIMIT),
    ).fetchall()
    notes: List[Dict[str, Any]] = []
    for r in reversed(rows):
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        notes.append({"ts": r["ts"], "text": payload.get("note") or payload.get("text") or ""})
    return notes


def _events(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    """The project's recent events, oldest first (§12.6)."""
    rows = conn.execute(
        """SELECT seq, ts, type, task_id, reason FROM events
           WHERE project_id = ? ORDER BY seq DESC LIMIT ?;""",
        (project_id, PROMPT_EVENT_LIMIT),
    ).fetchall()
    return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"], "task_id": r["task_id"],
             "reason": r["reason"]} for r in reversed(rows)]


def _authority(conn: sqlite3.Connection, project_id: str) -> Dict[str, Any]:
    """The authority section's data: approval categories and assignable models (§12.6).

    Review m2-r9 finding 7: only the categories whose current policy is not
    `not_required` are listed (the prompt says "need user approval"), and the model
    list covers every enabled launch agent except the project's current lead (E9
    forbids only the current lead as an assignee).
    """
    categories = sorted(
        r["key"].split(".", 2)[2]
        for r in conn.execute("SELECT key, value_json FROM settings WHERE key LIKE 'approval.policy.%';").fetchall()
        if _policy_needs_approval(r["value_json"])
    )
    lead = get_project_active_lead(conn, project_id)
    lead_id = lead["agent_id"] if lead else None
    models: Dict[str, List[str]] = {}
    for r in conn.execute(
        """SELECT p.name AS name, a.id AS id, a.allowed_models_json AS models
           FROM agents a JOIN principals p ON a.id = p.id
           WHERE a.delivery = 'launch' AND p.disabled_at IS NULL
           ORDER BY p.name ASC;"""
    ).fetchall():
        if lead_id is not None and r["id"] == lead_id:
            continue
        try:
            allowed = json.loads(r["models"] or "[]")
        except (ValueError, TypeError):
            allowed = []
        if allowed:
            models[r["name"]] = [str(m) for m in allowed]
    return {"approval_categories": categories, "allowed_models": models}


def _policy_needs_approval(value_json: Any) -> bool:
    """True unless the stored approval policy is `not_required` (the default)."""
    if value_json is None:
        return False
    try:
        value = json.loads(value_json)
    except (ValueError, TypeError):
        return False
    if isinstance(value, dict):
        value = value.get("policy", value.get("value"))
    return value not in (None, "not_required")


def _messages(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """The pending messages in the shape `render_lead_prompt` expects (§12.6)."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        out.append({"id": r["id"], "type": r["type"], "created_at": r["created_at"],
                    "payload": payload})
    return out


def build_prompt_input(conn: sqlite3.Connection, project_id: str, messages: List[sqlite3.Row],
                       limit_bytes: int) -> LeadPromptInput:
    """Everything the lead prompt is rendered from (§12.6)."""
    project = conn.execute("SELECT id, name, goal FROM projects WHERE id = ?;", (project_id,)).fetchone()
    return LeadPromptInput(
        project={"id": project["id"], "name": project["name"], "goal": project["goal"]},
        criteria=_criteria(conn, project_id),
        tasks=_tasks(conn, project_id),
        approvals=_approvals(conn, project_id),
        notes=_notes(conn, project_id),
        events=_events(conn, project_id),
        messages=_messages(messages),
        authority=_authority(conn, project_id),
        limit_bytes=limit_bytes,
    )


# ---------------------------------------------------------------------------
# Starting a turn: TX (messages, run, token, lease), effect (repo/, prompt, spawn).
# ---------------------------------------------------------------------------


def _lead_agent(conn: sqlite3.Connection, agent_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, p.name FROM agents a JOIN principals p ON a.id = p.id WHERE a.id = ?;",
        (agent_id,),
    ).fetchone()


def _boot_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM boots WHERE stopped_at IS NULL ORDER BY started_at DESC LIMIT 1;"
    ).fetchone()
    return row["id"] if row else "boot_unknown"


def plan_lead_repo_refresh(conn: sqlite3.Connection, home: str, project_id: str,
                           agent_id: str,
                           assignment_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The git work a turn's `repo/` refresh needs, or None when there is none (§10.1).

    Review m2-r9 finding 3: this is DB and filesystem reads only. The git calls run in
    `run_lead_repo_refresh` on a worker thread, never on the scheduler tick's thread.
    """
    project = conn.execute("SELECT repo_path FROM projects WHERE id = ?;", (project_id,)).fetchone()
    if project is None or not project["repo_path"]:
        return None
    ws_root = get_workspaces_root(conn, home)
    return {
        "repo": project["repo_path"],
        "project_id": project_id,
        "agent_id": agent_id,
        "assignment_id": assignment_id,
        "ws_root": ws_root,
        "path": lead_repo_worktree_path(ws_root, project_id, agent_id),
        "branch": integration_branch_name(project_id),
    }


def run_lead_repo_refresh(home: str, plan: Mapping[str, Any]) -> Optional[str]:
    """Refresh the lead's detached `repo/` worktree before a turn (§10.1, K13).

    Runs on a worker thread (review m2-r9 finding 3). Only when it is clean:
    `worktree remove` then `worktree add --detach` at the integration branch tip. A
    dirty worktree is kept as it is (the lead may have uncommitted notes) and the
    reason is logged. Returns the worktree path, or None when there is no repo/.
    """
    from mesh.core.gitops import run_git

    repo = plan["repo"]
    project_id = plan["project_id"]
    ws_root = plan["ws_root"]
    agent_id = plan["agent_id"]
    path = plan["path"]
    if not os.path.isdir(path):
        # Missing (never created, or removed by hand): create it now.
        try:
            return add_lead_repo_worktree(repo, project_id, ws_root, agent_id)
        except Exception as exc:  # noqa: BLE001 - the turn still runs without a repo/
            _log(home, f"lead repo/ worktree not created for {project_id}: {type(exc).__name__}: {exc}")
            return None
    try:
        status = run_git(["status", "--porcelain"], cwd=path)
    except Exception as exc:  # noqa: BLE001
        _log(home, f"lead repo/ status failed for {project_id}: {type(exc).__name__}: {exc}")
        return path
    if (status.stdout or "").strip():
        _log(home, f"lead repo/ worktree of {project_id} is dirty; kept as it is (K13)")
        return path
    try:
        cp = run_git(["worktree", "remove", path], cwd=repo)
        if cp.returncode != 0:
            _log(home, f"lead repo/ worktree remove failed for {project_id}: {(cp.stderr or '')[-500:]}")
            return path
        return add_lead_repo_worktree(repo, project_id, ws_root, agent_id)
    except Exception as exc:  # noqa: BLE001 - a failed refresh never blocks the turn
        _log(home, f"lead repo/ refresh failed for {project_id}: {type(exc).__name__}: {exc}")
        return path


# ---------------------------------------------------------------------------
# The lead-turn launch gate (AG step 4 of §6.2 for lead turns).
# ---------------------------------------------------------------------------

#: `lead.turn_refused` reasons, by the error code of the failed check.
_TURN_REFUSED_REASONS = {
    c.ADAPTER_UNSUPPORTED: "adapter_not_launch_enabled",
    c.TEST_DOUBLE_DISABLED: "test_double_disabled",
}


def lead_launch_check(conn: sqlite3.Connection, adapter: Any, testing_mode: bool = False,
                      binary_path: Optional[str] = None) -> None:
    """Raise unless a lead turn may launch on `adapter` (rc 13).

    The same check as a launch assignee of `task create/assign`
    (handlers/task.py `_validate_assignment_guards`): the test double only in testing
    mode, and only a `launch_enabled` adapter; a vendor adapter is rc 13
    `ADAPTER_UNSUPPORTED` with the milestone that enables it.

    C-5 (m4-p3-cleanup): after the vendor rule, the gate is `evidence.effective_launch`
    (the same evidence gate as task assignment, with the lead's `binary_path`). Today only
    non-vendor kinds reach it, where it returns the class attribute; it is already in place
    for when vendor leads open (which also needs L4 resolved first).
    """
    if adapter.kind == "vendor":
        # N19 (M3-lite): vendor lead turns are off whatever the evidence says. Checked
        # first (coordinator ruling, r4b), before the launch-enabled and any evidence
        # gate, so every vendor lead is refused with `vendor_lead_m3_lite`.
        raise vendor_lead_refused(adapter)
    if adapter.kind == "test_double" and not testing_mode:
        raise TestDoubleDisabled()
    from mesh.adapters.evidence import effective_launch

    ev = effective_launch(conn, adapter, testing_mode, binary_path)
    if not ev.ok:
        raise AdapterUnsupported(
            f"Adapter '{adapter.id}' is not launch-enabled in this milestone",
            milestone=getattr(ev, "milestone", None) or adapter.launch_milestone or "M3",
            reason=ev.reason,
        )


#: N19: the `lead.turn_refused` reason for a launch-enabled vendor lead in M3-lite.
VENDOR_LEAD_REFUSED_REASON = "vendor_lead_m3_lite"


def vendor_lead_refused(adapter: Any) -> AdapterUnsupported:
    """The rc 13 error for a vendor lead turn in M3-lite (`details.reason` set)."""
    exc = AdapterUnsupported(
        f"Adapter '{adapter.id}' cannot run lead turns in M3-lite (vendor lead turns are off)",
        milestone="M3",
    )
    exc.details["reason"] = VENDOR_LEAD_REFUSED_REASON
    return exc


def lead_launch_allowed(conn: sqlite3.Connection, adapter: Any, testing_mode: bool = False) -> bool:
    """True when `lead_launch_check` passes."""
    try:
        lead_launch_check(conn, adapter, testing_mode)
    except MeshError:
        return False
    return True


def _record_turn_refused(conn: sqlite3.Connection, project_id: str, lead: Mapping[str, Any],
                         adapter_id: str, exc: MeshError) -> None:
    """One `lead.turn_refused` event per lead assignment; the messages stay pending."""
    assignment_id = lead.get("assignment_id")
    seen = conn.execute(
        "SELECT 1 FROM events WHERE type = 'lead.turn_refused' AND project_id = ? "
        "AND json_extract(payload_json, '$.lead_assignment_id') = ? LIMIT 1;",
        (project_id, assignment_id),
    ).fetchone()
    if seen is not None:
        return
    with transaction(conn):
        record_event(
            conn=conn,
            type_="lead.turn_refused",
            actor_id="system",
            actor_kind="system",
            via="system",
            project_id=project_id,
            payload={
                "project_id": project_id,
                "lead_assignment_id": assignment_id,
                "agent_id": lead.get("agent_id"),
                "adapter": adapter_id,
                "reason": (exc.details or {}).get("reason")
                or _TURN_REFUSED_REASONS.get(exc.code, "launch_refused"),
                "code": exc.code,
                "milestone": (exc.details or {}).get("milestone"),
            },
        )


def start_lead_turn(conn: sqlite3.Connection, home: str, project_id: str,
                    lead: Mapping[str, Any], messages: List[sqlite3.Row],
                    testing_mode: bool = False) -> Optional[str]:
    """Start one lead turn (§12.4). Returns the run id, or None when nothing started."""
    plan = plan_lead_turn(conn, home, project_id, lead, messages, testing_mode)
    if plan is None:
        return None
    return commit_lead_turn(conn, home, plan)


def plan_lead_turn(conn: sqlite3.Connection, home: str, project_id: str,
                   lead: Mapping[str, Any], messages: List[sqlite3.Row],
                   testing_mode: bool = False) -> Optional[Dict[str, Any]]:
    """Everything a turn needs before its TX: DB reads and the prompt render (§12.4).

    Review m2-r9 finding 3: no git runs here, so the scheduler tick may call it.
    """
    agent = _lead_agent(conn, lead["agent_id"])
    if agent is None:
        return None
    adapter_id = agent["adapter"]
    try:
        adapter = get_adapter(adapter_id, testing_mode=testing_mode,
                              options=json.loads(agent["adapter_options_json"] or "{}"))
        lead_launch_check(conn, adapter, testing_mode, binary_path=agent["binary_path"])
    except MeshError as exc:
        _record_turn_refused(conn, project_id, lead, adapter_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        _log(home, f"lead turn adapter '{adapter_id}' unavailable: {type(exc).__name__}: {exc}")
        return None

    profile = str(_setting(conn, "lead.profile", DEFAULT_LEAD_PROFILE) or DEFAULT_LEAD_PROFILE)
    timeout_s = int(_setting(conn, "lead.turn_timeout_s", DEFAULT_LEAD_TURN_TIMEOUT_S)
                    or DEFAULT_LEAD_TURN_TIMEOUT_S)
    limit_bytes = int(getattr(adapter, "max_argv_prompt_bytes", LEAD_PROMPT_LIMIT_BYTES)
                      or LEAD_PROMPT_LIMIT_BYTES)

    # --- Render the prompt (before TX: it decides which messages are included) ---
    inp = build_prompt_input(conn, project_id, messages, limit_bytes)
    rendered = render_lead_prompt(inp)
    included = list(rendered.included_message_ids)[:LEAD_WAKE_BATCH_MAX]
    if not included:
        return None
    return {
        "project_id": project_id,
        "lead": dict(lead),
        "agent": agent,
        "adapter_id": adapter_id,
        "profile": profile,
        "timeout_s": timeout_s,
        "limit_bytes": limit_bytes,
        "rendered": rendered,
        "included": included,
        "testing_mode": testing_mode,
    }


def commit_lead_turn(conn: sqlite3.Connection, home: str,
                     plan: Mapping[str, Any]) -> Optional[str]:
    """TX (messages, run, token, lease), then the effect (prompt, spawn) (§12.4).

    The `repo/` refresh has already run on a worker thread (review m2-r9 finding 3), so
    the order is: refresh → TX → prompt.md/spec.json → spawn → TX B. A failed refresh
    never blocks the turn.
    """
    from mesh.daemon import children
    from mesh.daemon.supervisor import (
        build_lead_child_env, build_run_options, build_spec, verified_resume_session,
    )

    project_id = plan["project_id"]
    lead = plan["lead"]
    agent = plan["agent"]
    adapter_id = plan["adapter_id"]
    profile = plan["profile"]
    timeout_s = plan["timeout_s"]
    limit_bytes = plan["limit_bytes"]
    rendered = plan["rendered"]
    included = plan["included"]
    testing_mode = plan["testing_mode"]
    try:
        adapter = get_adapter(adapter_id, testing_mode=testing_mode,
                              options=json.loads(agent["adapter_options_json"] or "{}"))
    except Exception as exc:  # noqa: BLE001
        _log(home, f"lead turn adapter '{adapter_id}' unavailable: {type(exc).__name__}: {exc}")
        return None

    run_id = generate_id("run")
    run_dir = os.path.join(home, "runs", run_id)
    ws_root = get_workspaces_root(conn, home)
    lead_dir = compute_lead_workspace_path(ws_root, project_id, agent["id"])
    now = now_iso_utc()
    now_ts = time.time()
    deadline_at = now_iso_utc(now_ts + timeout_s)
    token_expires_at = now_iso_utc(now_ts + timeout_s + RUN_TOKEN_EXPIRY_GRACE_S)
    lease_ttl_s = get_lease_ttl_s(conn)
    boot_id = _boot_id(conn)

    # --- TX: messages delivering, run row, run token, lease ---
    try:
        with transaction(conn):
            if not _lease_free(conn, project_id):
                raise sqlite3.IntegrityError("leadturn lease already held")
            conn.execute(
                """INSERT INTO runs (
                    id, kind, task_id, project_id, agent_id, attempt, fence,
                    adapter, model, effort, profile, status, boot_id, run_dir,
                    argv_redacted_json, started_at, deadline_at, lead_assignment_id
                ) VALUES (?, 'lead_turn', NULL, ?, ?, NULL, 0, ?, NULL, NULL, ?, 'starting', ?, ?,
                          '[]', ?, ?, ?);""",
                (run_id, project_id, agent["id"], adapter_id, profile, boot_id, run_dir,
                 now, deadline_at, lead["assignment_id"]),
            )
            conn.execute(
                """INSERT INTO leases (
                    key, run_id, holder_kind, holder, agent_id, fence,
                    slots_in_use_at_acquire, limit_at_acquire, total_at_acquire, total_limit_at_acquire,
                    acquired_at, expires_at, heartbeat_at
                ) VALUES (?, ?, 'meshd', ?, ?, 0, NULL, NULL, NULL, NULL, ?, ?, ?);""",
                (f"leadturn:{project_id}", run_id, boot_id, agent["id"], now,
                 now_iso_utc(now_ts + lease_ttl_s), now),
            )
            token, lookup, secret_hash = generate_run_token()
            scope = {
                "role": "lead",
                "run_id": run_id,
                "project_id": project_id,
                "lead_assignment_id": lead["assignment_id"],
            }
            conn.execute(
                """INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at)
                   VALUES (?, ?, 'run_token', ?, ?, ?, ?, ?);""",
                (generate_id("cred"), agent["id"], lookup, secret_hash, json.dumps(scope),
                 now, token_expires_at),
            )
            register_secret(f"run_token:{run_id}", token)
            placeholders = ",".join("?" for _ in included)
            conn.execute(
                f"""UPDATE outbox SET status = 'delivering', delivering_run_id = ?
                    WHERE id IN ({placeholders}) AND status = 'pending';""",
                (run_id, *included),
            )
            record_event(
                conn=conn,
                type_="lead.turn_started",
                actor_id="system",
                actor_kind="system",
                via="system",
                project_id=project_id,
                run_id=run_id,
                payload={
                    "run_id": run_id,
                    "project_id": project_id,
                    "lead_assignment_id": lead["assignment_id"],
                    "messages": len(included),
                    "prompt_bytes": rendered.bytes,
                    "omitted": rendered.omitted,
                },
            )
    except sqlite3.IntegrityError as exc:
        unregister_secret(f"run_token:{run_id}")
        _log(home, f"lead turn TX rejected for {project_id}: {exc}")
        return None

    # --- Effect: run dir, prompt.md, spec.json, spawn, TX B ---
    proc: Optional["subprocess.Popen[Any]"] = None
    try:
        ensure_run_dir(run_dir)
        ensure_workspace_dir(lead_dir)
        prompt_path = os.path.join(run_dir, "prompt.md")
        _write_text(prompt_path, rendered.text)
        strategy = prompt_strategy(adapter_id, profile, rendered.bytes, limit_bytes)
        if strategy == "too_long":
            # The renderer caps at the adapter's own limit, so this cannot happen; if it
            # ever does, the turn must not launch (a lead that cannot read its task).
            raise RuntimeError(f"lead prompt of {rendered.bytes} bytes exceeds {limit_bytes}")
        argv_prompt = pointer_prompt(run_dir) if strategy == "pointer" else rendered.text
        assignment = conn.execute("SELECT session_id FROM lead_assignments WHERE id = ?;",
                                  (lead["assignment_id"],)).fetchone()
        request = RunRequest(
            prompt_path=prompt_path,
            workspace=lead_dir,
            model=None,
            effort=None,
            profile=profile,
            timeout_s=timeout_s,
            resume_session_id=verified_resume_session(adapter, assignment["session_id"] if assignment else None),
            options=build_run_options(agent, run_id),
        )
        spec_cmd = adapter.build(request)
        argv = list(spec_cmd.argv)
        if strategy == "pointer":
            argv = [argv_prompt if part == prompt_path else part for part in argv]
        spec = build_spec(
            argv=argv,
            cwd=spec_cmd.cwd,
            stdin=spec_cmd.stdin,
            redact_env_names=["MESH_TOKEN"],
            idle_timeout_s=0,
        )
        _write_json(os.path.join(run_dir, "spec.json"), spec)

        socket_path = os.path.join(home, "run", "mesh.sock")
        child_env = build_lead_child_env(
            base_env=dict(os.environ),
            token=token,
            run_id=run_id,
            project_id=project_id,
            socket_path=socket_path,
        )
        proc = children.popen_registered(
            "wrapper",
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", run_dir],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=lead_dir,
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
        from mesh.core.proc import get_proc_stat
        from mesh.daemon.supervisor import handle_spawn_failure, register_wrapper

        register_wrapper(run_id, proc)
        # --- TX B: run running with wrapper identity (inside try so any TX B error aborts) ---
        stat_info = get_proc_stat(proc.pid)
        with transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'running', wrapper_pid = ?, pgid = ?, proc_starttime = ?, "
                "argv_redacted_json = ? WHERE id = ?;",
                (proc.pid, proc.pid, stat_info["starttime"] if stat_info else None,
                 json.dumps(redact_value(argv)), run_id),
            )
    except Exception as exc:  # noqa: BLE001 - any spawn failure (incl. TX B) is spawn_failed
        from mesh.daemon.supervisor import handle_spawn_failure

        _log(home, f"lead turn spawn failed for {project_id}: {type(exc).__name__}: {exc}")
        spawned = proc if proc is not None else getattr(exc, "proc", None)
        return handle_spawn_failure(
            conn, home, run_id, run_dir, spawned, exc,
            kind="lead_turn",
            fail_args=(project_id,),
        )
    return run_id


def _write_text(path: str, text: str, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _write_json(path: str, obj: Any, mode: int = 0o600) -> None:
    _write_text(path, json.dumps(obj, sort_keys=True) + "\n", mode)


def _fail_lead_turn_spawn(conn: sqlite3.Connection, run_id: str, project_id: str,
                          message: str, after_popen: Optional[Dict[str, Any]] = None) -> None:
    """A lead turn that could not spawn: close it and put its messages back (§12.4)."""
    now = now_iso_utc()
    payload: Dict[str, Any] = {"reason": "spawn_failed", "message": message}
    if after_popen:
        payload["after_popen"] = after_popen
    conn.execute(
        "UPDATE runs SET status = 'spawn_failed', end_reason = 'spawn_failed', ended_at = ?, "
        "result_json = ? WHERE id = ?;",
        (now, json.dumps(redact_value(payload)), run_id),
    )
    conn.execute("DELETE FROM leases WHERE key = ? AND run_id = ?;", (f"leadturn:{project_id}", run_id))
    _release_messages(conn, run_id, ok=False, project_id=project_id)
    from mesh.daemon.supervisor import revoke_run_token

    revoke_run_token(conn, run_id)


# ---------------------------------------------------------------------------
# Exit handling (§12.4): ack on exit 0, backoff otherwise, dead + lead.unreachable.
# ---------------------------------------------------------------------------


def _release_messages(conn: sqlite3.Connection, run_id: str, ok: bool,
                      project_id: str, testing_mode: bool = False) -> Dict[str, int]:
    """Apply the turn's outcome to the messages it carried (caller owns the tx).

    Exit 0 → `acked`. Otherwise → `pending` with `attempts += 1` and the §12.4
    backoff, or `dead` once `attempts >= outbox.max_attempts` (then one
    `lead.unreachable` event, whose fan-out reaches the user and the origin agent).
    """
    rows = conn.execute(
        "SELECT id, attempts FROM outbox WHERE delivering_run_id = ? AND status = 'delivering';",
        (run_id,),
    ).fetchall()
    counts = {"acked": 0, "pending": 0, "dead": 0}
    if not rows:
        return counts
    now_ts = time.time()
    now = now_iso_utc(now_ts)
    max_attempts = int(_setting(conn, "outbox.max_attempts", 3) or 3)
    backoff = lead_wake_backoff_s(conn, testing_mode)
    dead_ids: List[str] = []
    for row in rows:
        if ok:
            # `delivering_run_id` is kept: it records which turn carried the message.
            conn.execute(
                "UPDATE outbox SET status = 'acked', acked_at = ? "
                "WHERE id = ? AND status = 'delivering';",
                (now, row["id"]),
            )
            counts["acked"] += 1
            continue
        attempts = int(row["attempts"] or 0) + 1
        if attempts >= max_attempts:
            conn.execute(
                "UPDATE outbox SET status = 'dead', attempts = ?, delivering_run_id = NULL, "
                "last_error = ? WHERE id = ? AND status = 'delivering';",
                (attempts, "lead turn failed", row["id"]),
            )
            counts["dead"] += 1
            dead_ids.append(row["id"])
            continue
        delay = backoff[min(attempts - 1, len(backoff) - 1)]
        conn.execute(
            "UPDATE outbox SET status = 'pending', attempts = ?, next_attempt_at = ?, "
            "delivering_run_id = NULL, last_error = ? WHERE id = ? AND status = 'delivering';",
            (attempts, now_iso_utc(now_ts + delay), "lead turn failed", row["id"]),
        )
        counts["pending"] += 1
    if dead_ids:
        record_event(
            conn=conn,
            type_="lead.unreachable",
            actor_id="system",
            actor_kind="system",
            via="system",
            project_id=project_id,
            run_id=run_id,
            payload={
                "project_id": project_id,
                "run_id": run_id,
                "attempts": max_attempts,
                "messages": dead_ids,
            },
        )
    return counts


# ---------------------------------------------------------------------------
# Actions (§12.4, Q10): after exit 0, before the messages are acked and the run is
# closed (which revokes its token).
# ---------------------------------------------------------------------------

#: The rc an item error stands for: a malformed item is a usage error; a command
#: outside the action list is what the lead run token gets for it on the CLI (rc 4).
_ITEM_ERROR_EXIT = {ERR_CMD_NOT_ALLOWED: c.EXIT_FORBIDDEN}


def lead_turn_final_text(run: Mapping[str, Any], testing_mode: bool = False) -> str:
    """The turn's final text: the adapter's parse, else stdout.log (as `parse_run_output`)."""
    text = ""
    try:
        text = get_adapter(run["adapter"], testing_mode=testing_mode).parse(run["run_dir"]).final_text or ""
    except Exception:  # noqa: BLE001 - fall back to the raw output
        text = ""
    if not text:
        try:
            with open(os.path.join(run["run_dir"], "stdout.log"), "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
    return text


def _lead_turn_principal(conn: sqlite3.Connection,
                         run: Mapping[str, Any]) -> Tuple[Dict[str, Any], Optional[MeshError]]:
    """The run token's principal, as `authenticate_and_authorize` builds it (§7.1).

    The second value is the error the CLI would get with this token (missing,
    revoked, expired, or a disabled agent: rc 3), or None.
    """
    row = conn.execute(
        """SELECT c.id, c.principal_id, c.scope_json, c.expires_at, c.revoked_at,
                  p.kind AS principal_kind, p.name, p.disabled_at
           FROM credentials c JOIN principals p ON c.principal_id = p.id
           WHERE c.kind = 'run_token' AND json_extract(c.scope_json, '$.run_id') = ?
           ORDER BY c.created_at DESC LIMIT 1;""",
        (run["id"],),
    ).fetchone()
    if row is None:
        principal = {"id": run["agent_id"], "kind": "agent", "name": None, "scope": {}}
        return principal, MeshError(code=c.TOKEN_INVALID, exit_code=c.EXIT_UNAUTHENTICATED,
                                    message="Lead turn has no run token")
    try:
        scope = json.loads(row["scope_json"] or "{}")
    except (ValueError, TypeError):
        scope = {}
    principal = {
        "id": row["principal_id"],
        "kind": row["principal_kind"],
        "name": row["name"],
        "credential_id": row["id"],
        "scope": scope,
        "capabilities": get_agent_capabilities(conn, row["principal_id"]),
    }
    error: Optional[MeshError] = None
    if row["revoked_at"] is not None:
        error = MeshError(code=c.TOKEN_REVOKED, exit_code=c.EXIT_UNAUTHENTICATED, message="Token has been revoked")
    elif row["expires_at"] is not None and row["expires_at"] < now_iso_utc():
        error = MeshError(code=c.TOKEN_EXPIRED, exit_code=c.EXIT_UNAUTHENTICATED, message="Token has expired")
    elif row["disabled_at"] is not None:
        error = MeshError(code=c.TOKEN_INVALID, exit_code=c.EXIT_UNAUTHENTICATED, message="Principal is disabled")
    return principal, error


def _record_action_result(conn: sqlite3.Connection, run: Mapping[str, Any],
                          principal: Mapping[str, Any], index: Optional[int], cmd: Optional[str],
                          args: Optional[Dict[str, Any]], ok: bool, exit_code: Optional[int],
                          error_code: Optional[str], message: Optional[str],
                          request_id: Optional[str] = None,
                          extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One `lead.action_result` event; a failure's fan-out is the `lead.action_failed` message.

    `request_id` is the dispatched request's id (the handler's own events carry the
    same one); None for an item error or a block-level result, which dispatch nothing.
    """
    payload: Dict[str, Any] = {
        "run_id": run["id"],
        "index": index,
        "cmd": cmd,
        "ok": ok,
        "exit_code": exit_code,
        "error_code": error_code,
        "request_id": request_id,
    }
    if extra:
        payload.update(extra)
    if not ok:
        payload["message"] = message
        payload["args"] = args
    record_event(
        conn=conn,
        type_="lead.action_result",
        actor_id=principal.get("id") or run["agent_id"],
        actor_kind=principal.get("kind") or "agent",
        via="lead_turn",
        credential_id=principal.get("credential_id"),
        project_id=run["project_id"],
        run_id=run["id"],
        payload=payload,
        request_id=request_id,
    )
    return payload


# ---------------------------------------------------------------------------
# Queued merges (`task.merge` actions, Quest decision r10b option b).
#
# A `task.merge` action is checked inside the close TX (`dispatch_internal_request`
# runs the merge's own TX1 checks under the lead run principal) and queued here. The
# server starts it after the close TX committed, through `handle_task_merge` (its own
# transactions and worker thread), never inside the supervisor's transaction. The
# queue is reconstructed from committed result events on the first supervisor tick
# after each boot, covering a crash between the close COMMIT and the merge's TX1.
# ---------------------------------------------------------------------------

_QUEUED_MERGES: List[Dict[str, Any]] = []
_QUEUED_MERGES_LOCK = threading.Lock()
_MERGE_RECOVERY_BOOT: Optional[str] = None


def _queue_lead_merge(item: Dict[str, Any]) -> None:
    with _QUEUED_MERGES_LOCK:
        if not any(m["request_id"] == item["request_id"] for m in _QUEUED_MERGES):
            _QUEUED_MERGES.append(item)


def _log_queued_merge_recovery_error(conn: sqlite3.Connection, message: str) -> None:
    try:
        from mesh.daemon.supervisor import _log_meshd

        db = next(row for row in conn.execute("PRAGMA database_list;") if row["name"] == "main")
        if db["file"]:
            _log_meshd(os.path.dirname(db["file"]), f"queued lead merge recovery: {message}")
    except Exception:
        pass  # Recovery and the watch pass must survive unavailable logs.


def _identifiable_corrupt_queued_merge(raw: str) -> bool:
    """Recognize complete top-level queue markers before damaged JSON.

    Decode JSON members, not substrings that might occur inside another action's
    message or args. Duplicated keys are ambiguous. This is identification only:
    corrupt payloads are never used to reconstruct a merge.
    """
    decoder = json.JSONDecoder()
    tail = raw.lstrip()
    if not tail.startswith("{"):
        return False
    tail = tail[1:].lstrip()
    fields: Dict[str, Any] = {}
    while tail:
        try:
            key, end = decoder.raw_decode(tail)
            if not isinstance(key, str) or key in fields:
                return False
            tail = tail[end:].lstrip()
            if not tail.startswith(":"):
                break
            tail = tail[1:].lstrip()
            value, end = decoder.raw_decode(tail)
            fields[key] = value
            tail = tail[end:].lstrip()
            if not tail.startswith(","):
                break
            tail = tail[1:].lstrip()
        except (ValueError, RecursionError):
            break
    return (fields.get("cmd") == "task.merge" and fields.get("ok") is True
            and fields.get("status") == "queued")


def recover_queued_lead_merges(conn: sqlite3.Connection) -> int:
    """Re-queue committed lead merges with no TX1 or merge-start failure result.

    Reuse the stored run principal, never a new credential or user authority. TX1
    rechecks current assignment, project scope and every merge guard. Pre-r11
    queued events lack args; only an unambiguous final stdout block is usable.
    Unidentifiable corrupt rows are durably skipped without calling them merges.
    Each unrecoverable row gets a terminal merge-start failure, not a boot-wide
    retry that could starve supervision of active runs.
    """
    try:
        if conn.in_transaction:
            return 0  # Only committed close results may escape into the queue.
        rows = conn.execute(
            """SELECT e.*, json_valid(e.payload_json) AS payload_valid FROM events e
               WHERE e.type = 'lead.action_result'
                 AND CASE WHEN json_valid(e.payload_json) THEN
                     json_extract(e.payload_json, '$.cmd') = 'task.merge'
                     AND json_extract(e.payload_json, '$.ok') = 1
                     AND json_extract(e.payload_json, '$.status') = 'queued'
                     ELSE 1 END
                 AND NOT EXISTS (
                     SELECT 1 FROM meta WHERE key = 'lead_merge_recovery_skip:' || e.seq)
                 AND NOT EXISTS (
                     SELECT 1 FROM events done WHERE
                       ((e.request_id IS NOT NULL AND done.request_id = e.request_id) OR
                        (e.request_id IS NULL AND
                         CASE WHEN json_valid(done.payload_json) THEN
                             json_extract(done.payload_json, '$.source_seq') = e.seq
                             ELSE 0 END))
                       AND (done.type = 'task.merging' OR
                            (done.type = 'lead.action_result' AND
                             CASE WHEN json_valid(done.payload_json) THEN
                                 json_extract(done.payload_json, '$.phase') = 'merge_start'
                                 ELSE 0 END)))
               ORDER BY e.seq;"""
        ).fetchall()
    except Exception as exc:
        _log_queued_merge_recovery_error(conn, f"scan failed: {type(exc).__name__}: {exc}")
        return 0
    recovered = 0
    for row in rows:
        payload: Dict[str, Any] = {}
        run = None
        try:
            # Use the scan's JSON definition, not Python's permissive NaN /
            # Infinity decoding, for deciding which rows need identification.
            if not row["payload_valid"]:
                if _identifiable_corrupt_queued_merge(row["payload_json"]):
                    raise ValueError("invalid queued task.merge payload JSON")
                # Events are immutable. Persist the skip so subsequent scans and
                # boots neither rescan nor repeatedly log an unidentified row.
                try:
                    with transaction(conn):
                        skipped = conn.execute(
                            "INSERT OR IGNORE INTO meta (key,value) VALUES (?, 'invalid_json');",
                            (f"lead_merge_recovery_skip:{row['seq']}",))
                except Exception as skip_exc:
                    _log_queued_merge_recovery_error(
                        conn, f"Cannot persist invalid JSON skip source_seq={row['seq']}: "
                              f"{type(skip_exc).__name__}: {skip_exc}")
                    continue  # Unidentified rows must never become merge failures.
                if skipped.rowcount:
                    _log_queued_merge_recovery_error(
                        conn, f"Skipping invalid JSON action result source_seq={row['seq']}: "
                              "not identifiable as a queued task.merge")
                continue
            payload = json.loads(row["payload_json"])
            run = conn.execute("SELECT * FROM runs WHERE id = ?;", (row["run_id"],)).fetchone()
            index = payload["index"]
            if type(index) is not int or index < 0:
                raise ValueError("missing or invalid action index")
            if run is None or not row["request_id"]:
                raise ValueError("missing run or request id")
            principal, _ = _lead_turn_principal(conn, run)
            args = payload.get("args")
            action_args = payload.get("action_args")
            if not isinstance(args, dict):
                parsed = parse_final_actions_block(lead_turn_final_text(run))
                action = next((a for a in parsed.actions
                               if a.index == index and a.cmd == "task.merge" and not a.error), None)
                if action is None:
                    raise ValueError("missing action")
                action_args = action.args
                args = action_request_args("task.merge", action_args, run["project_id"])
                task_id = args.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    raise ValueError("missing or invalid legacy task id")
                request = payload.get("request")
                sources = [payload, payload.get("action_args"), action_args, request]
                if isinstance(request, dict):
                    sources.append(request.get("args"))
                task_ids = [row["task_id"]]
                for source in sources:
                    if isinstance(source, dict):
                        task_ids.extend(source.get(key) for key in ("task", "task_id"))
                if any(stored is not None and stored != task_id for stored in task_ids):
                    raise ValueError("legacy action disagrees with stored task id")
            item = {
                "run_id": run["id"], "index": index, "request_id": row["request_id"],
                "principal": principal, "args": args, "action_args": action_args or args,
            }
            _queue_lead_merge(item)
            recovered += 1
        except Exception as exc:
            message = f"Cannot recover queued lead merge {row['request_id']}: {type(exc).__name__}: {exc}"
            _log_queued_merge_recovery_error(conn, message)
            try:
                with transaction(conn):
                    record_event(
                        conn=conn, type_="lead.action_result",
                        actor_id=row["actor_id"], actor_kind=row["actor_kind"], via="lead_turn",
                        project_id=row["project_id"], run_id=run["id"] if run is not None else None,
                        request_id=row["request_id"], reason="RECOVERY_FAILED",
                        payload={
                            "run_id": row["run_id"],
                            "source_seq": row["seq"],
                            "index": payload.get("index") if isinstance(payload, dict) else None,
                            "cmd": "task.merge", "ok": False, "exit_code": c.EXIT_INTERNAL,
                            "error_code": "RECOVERY_FAILED", "reason": "RECOVERY_FAILED",
                            "phase": "merge_start", "request_id": row["request_id"],
                            "message": message,
                        },
                    )
            except Exception as record_exc:
                _log_queued_merge_recovery_error(
                    conn, f"cannot record failure for {row['request_id']}: "
                          f"{type(record_exc).__name__}: {record_exc}")
    return recovered


def recover_queued_lead_merges_once(conn: sqlite3.Connection) -> None:
    """Recover on the first post-commit supervisor tick of this boot."""
    global _MERGE_RECOVERY_BOOT
    if conn.in_transaction:
        return
    boot = _boot_id(conn)
    if boot != _MERGE_RECOVERY_BOOT:
        try:
            recover_queued_lead_merges(conn)
        finally:
            _MERGE_RECOVERY_BOOT = boot


def take_queued_lead_merges() -> List[Dict[str, Any]]:
    """Remove and return every queued merge (the server starts them post-commit)."""
    with _QUEUED_MERGES_LOCK:
        items = list(_QUEUED_MERGES)
        _QUEUED_MERGES.clear()
    return items


def requeue_lead_merges(items: List[Dict[str, Any]]) -> None:
    """Put merges back, ahead of newer ones (the drain found a transaction open)."""
    with _QUEUED_MERGES_LOCK:
        _QUEUED_MERGES[:0] = items


def queued_lead_merge_committed(conn: sqlite3.Connection, item: Mapping[str, Any]) -> bool:
    """True once the close TX that queued `item` committed (its result event exists)."""
    row = conn.execute(
        "SELECT 1 FROM events WHERE type = 'lead.action_result' AND run_id = ? AND request_id = ? LIMIT 1;",
        (item["run_id"], item["request_id"]),
    ).fetchone()
    return row is not None


def record_queued_merge_failure(conn: sqlite3.Connection, item: Mapping[str, Any],
                                error: MeshError) -> None:
    """A queued merge that could not start: one more `lead.action_result` (ok false),
    so the lead gets a `lead.action_failed` message. Caller owns the transaction."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?;", (item["run_id"],)).fetchone()
    if run is None:
        return
    _record_action_result(conn, run, item["principal"], item["index"], "task.merge", item["action_args"],
                          False, error.exit_code, error.code, error.message, item["request_id"],
                          extra={"phase": "merge_start"})


def execute_lead_actions(conn: sqlite3.Connection, home: str, run: Mapping[str, Any],
                         testing_mode: bool = False) -> List[Dict[str, Any]]:
    """Execute the turn's `mesh-actions` block in order (§12.4, Q10). Caller owns the tx.

    Each action is dispatched inside meshd as the lead run principal: the run token's
    principal and scope with `via='run_token'`, so the handlers' `check_authz` applies
    exactly as for a CLI call with `MESH_TOKEN` (`LEAD_SUPERSEDED` first, then the lead
    column, then the scope project). Each request runs in its own savepoint with
    idempotency key `act:<run>:<index>`. Every item gets one `lead.action_result`
    event (`via='lead_turn'`); every failure becomes one `lead.action_failed` message.
    """
    from mesh.daemon.server import dispatch_internal_request

    parsed = parse_actions(lead_turn_final_text(run, testing_mode))
    if not parsed.block_found:
        return []
    principal, auth_error = _lead_turn_principal(conn, run)
    scope_project = (principal.get("scope") or {}).get("project_id") or run["project_id"]
    if parsed.error:
        return [_record_action_result(
            conn, run, principal, None, None, None, False, c.EXIT_USAGE, parsed.error,
            f"The mesh-actions block has more than {MAX_ACTIONS} actions; none was executed")]

    results: List[Dict[str, Any]] = []
    for action in parsed.actions:
        if action.error:
            results.append(_record_action_result(
                conn, run, principal, action.index, action.cmd, action.args, False,
                _ITEM_ERROR_EXIT.get(action.error, c.EXIT_USAGE), action.error,
                f"Action {action.index} was not executed: {action.error}"))
            continue
        if auth_error is not None:
            results.append(_record_action_result(
                conn, run, principal, action.index, action.cmd, action.args, False,
                auth_error.exit_code, auth_error.code, auth_error.message))
            continue
        args = action_request_args(action.cmd, action.args, scope_project)
        request_id = generate_id("req")
        ctx = HandlerContext(
            conn=conn,
            principal=dict(principal),
            actor_kind=principal["kind"],
            via="run_token",
            request_id=request_id,
            home=home,
            cmd=action.cmd,
            args=args,
            testing_mode=testing_mode,
            credential_id=principal.get("credential_id"),
            approval=action.approval,
        )
        envelope = dispatch_internal_request(ctx, action_idempotency_key(run["id"], action.index))
        exit_code = envelope.get("exit_code")
        ok = bool(envelope.get("ok")) and exit_code == 0
        error = envelope.get("error") or {}
        extra: Optional[Dict[str, Any]] = None
        merges = [e for e in ctx.effects if e.kind == "start_task_merge"] if ok else []
        others = [e for e in ctx.effects if e.kind != "start_task_merge"]
        for eff in merges:
            _queue_lead_merge({
                "run_id": run["id"],
                "index": action.index,
                "request_id": request_id,
                "principal": dict(principal),
                "args": dict(eff.params.get("args") or args),
                "action_args": action.args,
            })
            extra = {"status": "queued", "args": dict(eff.params.get("args") or args),
                     "action_args": action.args}
        if others:
            _log(home, f"lead action {action.cmd} of run {run['id']} queued effects that were not run: "
                       f"{[e.kind for e in others]}")
        results.append(_record_action_result(
            conn, run, principal, action.index, action.cmd, action.args, ok, exit_code,
            None if ok else error.get("code"), None if ok else error.get("message"),
            request_id, extra))
    return results

def finish_lead_turn(conn: sqlite3.Connection, home: str, run: Mapping[str, Any],
                     exit_info: Optional[Dict[str, Any]],
                     testing_mode: bool = False) -> str:
    """Close a lead turn and apply its outcome (§12.4). Caller owns the transaction.

    The supervisor routes `kind='lead_turn'` runs here, never to T7/T8: a lead turn
    has no task, so no task transition may fire. On exit 0 the turn's actions run
    first (`execute_lead_actions`), then the messages are acked and the run closes.
    """
    from mesh.daemon.supervisor import _close_run

    project_id = run["project_id"]
    exit_code = None
    signo = None
    if exit_info is not None:
        exit_code = exit_info.get("exit_code")
        signo = exit_info.get("signal")
    ok = exit_info is not None and exit_code == 0 and signo is None
    actions: Optional[Dict[str, int]] = None
    if ok:
        # Q10: the actions run while the run is still open and its token valid; a
        # terminated turn (set-lead, timeout) never reaches here.
        conn.execute("SAVEPOINT lead_actions;")
        try:
            executed = execute_lead_actions(conn, home, run, testing_mode)
        except Exception as exc:  # noqa: BLE001 - a broken action pass must not wedge the run
            conn.execute("ROLLBACK TO lead_actions;")
            _log(home, f"lead actions of run {run['id']} not executed: {type(exc).__name__}: {exc}")
            executed = []
        conn.execute("RELEASE lead_actions;")
        actions = {"ok": sum(1 for r in executed if r["ok"]),
                   "failed": sum(1 for r in executed if not r["ok"])}
    counts = _release_messages(conn, run["id"], ok, project_id, testing_mode)
    result = {
        "exit_code": exit_code,
        "signal": signo,
        "ended_at": (exit_info or {}).get("ended_at"),
        "messages": counts,
    }
    if actions is not None:
        result["actions"] = actions
    if exit_info is None:
        _close_run(conn, run, "lost", "lost", None, {"reason": "lost", "messages": counts})
        return "lost"
    _close_run(conn, run, "exited", "exited", exit_code, result)
    if signo is not None:
        conn.execute("UPDATE runs SET signal = ? WHERE id = ?;", (signo, run["id"]))
    return "acked" if ok else "retry"


def lead_turn_termination_reason(conn: sqlite3.Connection, run: Mapping[str, Any]) -> Optional[str]:
    """Why a live lead turn must be terminated now, or None (§11.3, §11.4).

    A lead turn has no task, so no task status, no post-submit grace and no idle
    timeout can end it. Two reasons apply, both derived from durable state on every
    watch pass: the turn's assignment is no longer the project's current one
    (`lead_superseded`, §8 set-lead; the backstop for the set-lead effect after a
    restart or a failed effect), checked first, then the deadline (`timeout`).
    """
    current = get_project_active_lead(conn, run["project_id"])
    if current is None or current["assignment_id"] != run["lead_assignment_id"]:
        return "lead_superseded"
    if run["deadline_at"] and time.time() > parse_utc(run["deadline_at"]).timestamp():
        return "timeout"
    return None


# ---------------------------------------------------------------------------
# The scheduler entry point.
# ---------------------------------------------------------------------------
# The scheduler entry point: two ticks, so no git runs on the event loop.
#
# Review m2-r9 finding 3 (the r4 pattern, supervisor.py `_code_workspace_state`):
# the tick that would start a turn does DB reads only and starts a registered
# worker thread for the `repo/` refresh; a later tick finds the finished result,
# commits the turn TX and spawns. Results are keyed by
# (project_id, lead_assignment_id) and dropped when stale.
# ---------------------------------------------------------------------------

_LEAD_REPO_PREP: Dict[Tuple[str, str], Dict[str, Any]] = {}
#: A finished refresh older than this is not used; the worktree is checked again.
LEAD_REPO_PREP_MAX_AGE_S = 10.0
_LEAD_REPO_PREP_LOCK = threading.Lock()


def _lead_repo_prep_worker(entry: Dict[str, Any], home: str, plan: Mapping[str, Any]) -> None:
    result: Optional[str] = None
    error: Optional[str] = None
    try:
        result = run_lead_repo_refresh(home, plan)
    except Exception as exc:  # noqa: BLE001 - a failed refresh never blocks the turn
        error = f"{type(exc).__name__}: {exc}"
    with _LEAD_REPO_PREP_LOCK:
        entry["result"] = result
        entry["error"] = error
        entry["done"] = True
        entry["done_at"] = time.monotonic()


def _lead_repo_state(home: str, plan: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The finished `repo/` refresh for this turn, or None while it is in flight."""
    key = (plan["project_id"], plan["assignment_id"])
    with _LEAD_REPO_PREP_LOCK:
        entry = _LEAD_REPO_PREP.get(key)
        if entry is not None:
            if not entry["done"]:
                return None
            del _LEAD_REPO_PREP[key]
            if time.monotonic() - entry.get("done_at", 0.0) <= LEAD_REPO_PREP_MAX_AGE_S:
                return entry
        entry = {"key": key, "done": False, "result": None, "error": None}
        _LEAD_REPO_PREP[key] = entry
    threading.Thread(target=_lead_repo_prep_worker, args=(entry, home, plan),
                     name=f"mesh-leadrepo-{plan['project_id']}", daemon=True).start()
    return None


def _evict_lead_repo_prep(candidate_keys: Any) -> None:
    """Drop finished refreshes of leads that are no longer candidates."""
    with _LEAD_REPO_PREP_LOCK:
        for key in [k for k, e in _LEAD_REPO_PREP.items() if e["done"] and k not in candidate_keys]:
            del _LEAD_REPO_PREP[key]


def lead_repo_prep_in_flight() -> int:
    """Worker threads still refreshing a lead `repo/` (tests and diagnostics)."""
    with _LEAD_REPO_PREP_LOCK:
        return sum(1 for e in _LEAD_REPO_PREP.values() if not e["done"])


def start_lead_turns(conn: sqlite3.Connection, home: str = "",
                     testing_mode: bool = False) -> List[str]:
    """Start a lead turn for every project whose trigger fires (§12.4).

    Called from the scheduler tick. Returns the run ids started in this pass. The
    `repo/` refresh of a repo project runs on a worker thread, so a turn starts on
    the tick after its refresh finished (review m2-r9 finding 3).
    """
    started: List[str] = []
    now = now_iso_utc()
    rows = conn.execute(
        """SELECT la.id AS assignment_id, la.agent_id, la.project_id, a.delivery
           FROM lead_assignments la
           JOIN agents a ON la.agent_id = a.id
           JOIN projects p ON la.project_id = p.id
           WHERE la.ended_at IS NULL AND a.delivery = 'launch' AND p.status = 'active'
           ORDER BY la.started_at ASC, la.project_id ASC;"""
    ).fetchall()
    candidates = {(r["project_id"], r["assignment_id"]) for r in rows}
    try:
        for lead in rows:
            project_id = lead["project_id"]
            try:
                messages = should_start_turn(conn, project_id, dict(lead), now)
                if messages is None:
                    continue
                plan = plan_lead_turn(conn, home, project_id, dict(lead), messages,
                                      testing_mode=testing_mode)
                if plan is None:
                    continue
                repo_plan = plan_lead_repo_refresh(conn, home, project_id, lead["agent_id"],
                                                   lead["assignment_id"])
                if repo_plan is not None:
                    state = _lead_repo_state(home, repo_plan)
                    if state is None:
                        continue  # the refresh is in flight; a later tick starts the turn
                    if state.get("error"):
                        _log(home, f"lead repo/ refresh failed for {project_id}: {state['error']}")
                run_id = commit_lead_turn(conn, home, plan)
                if run_id:
                    started.append(run_id)
            except Exception as exc:  # one bad project must not stop the others
                _log(home, f"lead turn start failed for {project_id}: {type(exc).__name__}: {exc}")
                continue
    finally:
        _evict_lead_repo_prep(candidates)
    return started
