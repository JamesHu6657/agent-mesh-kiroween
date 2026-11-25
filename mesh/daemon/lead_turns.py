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
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

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
    extract_fallback_reply_text,
    parse_actions,
    parse_final_actions_block,
    strip_mesh_actions_blocks,
)
from mesh.core.leases import get_lease_ttl_s
from mesh.core.paths import get_install_root
from mesh.core.prompts import PROMPT_LIMIT_BYTES, pointer_prompt, prompt_strategy
from mesh.core.redact import redact, redact_value, register_secret, unregister_secret
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

#: Interval/TTL helper (patchable in unit/integration tests without replacing `time.monotonic`).
_monotonic = time.monotonic

#: §12.4: at most this many messages go into one turn.
LEAD_WAKE_BATCH_MAX = 50

#: §12.4: this many pending messages trigger a turn without waiting for the debounce.
LEAD_WAKE_BATCH_TRIGGER = 20

#: §12.4: the lead prompt's own cap, used when the adapter has no smaller one.
LEAD_PROMPT_LIMIT_BYTES = PROMPT_LIMIT_BYTES

#: The message type that never triggers a turn by itself (Q8).
ACTION_FAILED_TYPE = "lead.action_failed"

#: Fast-lane wake message types (Chat-First Batch 1.2): use `lead.chat_wake_debounce_s` (default 0s).
IMMEDIATE_WAKE_TYPES = ("chat.message", "approval.decided")

#: Run-token expiry grace after the deadline (§7.1).
RUN_TOKEN_EXPIRY_GRACE_S = 300

#: §12.4 default lead profile and turn timeout.
DEFAULT_LEAD_PROFILE = "readonly"
DEFAULT_LEAD_TURN_TIMEOUT_S = 1800

#: How many recent events / notes / chat messages the prompt carries (§12.6).
PROMPT_EVENT_LIMIT = 200
PROMPT_NOTE_LIMIT = 100
PROMPT_CHAT_LIMIT = 20


def _setting(conn: sqlite3.Connection, key: str, default: Any) -> Any:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?;", (key,)).fetchone()
    if row is None or row["value_json"] is None:
        return default
    try:
        value = json.loads(row["value_json"])
    except (ValueError, TypeError):
        return default
    return default if value is None else value


def vendor_leads_enabled(conn: Optional[sqlite3.Connection]) -> bool:
    """True only when direct-user opt-in setting ``lead.vendor_leads`` is enabled (fail closed)."""
    if conn is None:
        return False
    try:
        return _setting(conn, "lead.vendor_leads", False) is True
    except sqlite3.Error:
        return False


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


def _project_uses_chat(conn: sqlite3.Connection, project_id: str, plan_gate_mode: Optional[str] = None) -> bool:
    """True when project is chat-gated (`require_approval`) or already has chat_messages rows (P3-1)."""
    if plan_gate_mode == "require_approval":
        return True
    if plan_gate_mode is None:
        prow = conn.execute("SELECT plan_gate_mode FROM projects WHERE id = ?;", (project_id,)).fetchone()
        if prow is not None and prow["plan_gate_mode"] == "require_approval":
            return True
    has_tbl = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chat_messages';"
    ).fetchone()
    if not has_tbl:
        return False
    return conn.execute(
        "SELECT 1 FROM chat_messages WHERE project_id = ? AND role = 'user' LIMIT 1;",
        (project_id,),
    ).fetchone() is not None


def should_start_turn(conn: sqlite3.Connection, project_id: str, lead: Mapping[str, Any],
                      now: str) -> Optional[List[sqlite3.Row]]:
    """The messages a turn would carry, or None when the trigger does not fire (§12.4)."""
    if lead.get("delivery") != "launch":
        return None
    project = conn.execute("SELECT status, plan_gate_mode FROM projects WHERE id = ?;", (project_id,)).fetchone()
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
    use_fast_lane = any(m["type"] == "chat.message" for m in messages) or (
        any(m["type"] == "approval.decided" for m in messages)
        and _project_uses_chat(conn, project_id, project["plan_gate_mode"])
    )
    if use_fast_lane:
        debounce_s = int(_setting(conn, "lead.chat_wake_debounce_s", 0) or 0)
    else:
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
    return [{"id": r["id"], "text": redact(r["text"]) if r["text"] else r["text"]} for r in rows]


def _tasks(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT t.id, t.title, t.status, t.kind, p.name AS assignee
           FROM tasks t LEFT JOIN principals p ON t.assignee_agent_id = p.id
           WHERE t.project_id = ?
           ORDER BY t.created_at ASC, t.id ASC;""",
        (project_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": redact(r["title"]) if r["title"] else r["title"],
            "status": r["status"],
            "kind": r["kind"],
            "assignee": r["assignee"],
        }
        for r in rows
    ]


def _approvals(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, category, summary FROM approvals WHERE project_id = ? AND status = 'pending' "
        "ORDER BY requested_at ASC, id ASC;",
        (project_id,),
    ).fetchall()
    return [{"id": r["id"], "category": r["category"], "summary": redact(r["summary"]) if r["summary"] else r["summary"]} for r in rows]


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
        note_text = payload.get("note") or payload.get("text") or ""
        notes.append({"ts": r["ts"], "text": redact(note_text) if isinstance(note_text, str) else redact_value(note_text)})
    return notes


def _events(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    """The project's recent events, oldest first (§12.6)."""
    rows = conn.execute(
        """SELECT seq, ts, type, task_id, reason FROM events
           WHERE project_id = ? ORDER BY seq DESC LIMIT ?;""",
        (project_id, PROMPT_EVENT_LIMIT),
    ).fetchall()
    return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"], "task_id": r["task_id"],
             "reason": redact(r["reason"]) if r["reason"] else r["reason"]} for r in reversed(rows)]


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
                    "payload": redact_value(payload)})
    return out


def _workers(conn: sqlite3.Connection, project_id: str, testing_mode: bool = False) -> List[Dict[str, Any]]:
    """Enabled worker agents (excluding the project's current lead) with profiles & allowed models."""
    lead = get_project_active_lead(conn, project_id)
    lead_id = lead["agent_id"] if lead else None
    rows = conn.execute(
        """SELECT a.id, p.name, a.adapter, a.delivery, a.default_profile,
                  a.max_unapproved_profile, a.allowed_models_json
           FROM agents a JOIN principals p ON a.id = p.id
           WHERE p.disabled_at IS NULL
           ORDER BY p.name ASC;"""
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        if lead_id is not None and r["id"] == lead_id:
            continue
        try:
            allowed_models = json.loads(r["allowed_models_json"] or "[]")
        except (ValueError, TypeError):
            allowed_models = []
        supported_profiles: List[str] = []
        try:
            adapter = get_adapter(r["adapter"], testing_mode=testing_mode)
            supported_profiles = list(getattr(adapter, "supported_profiles", ()) or ())
        except Exception:
            supported_profiles = []
        out.append({
            "name": r["name"],
            "adapter": r["adapter"],
            "delivery": r["delivery"],
            "default_profile": r["default_profile"],
            "max_unapproved_profile": r["max_unapproved_profile"],
            "supported_profiles": supported_profiles,
            "allowed_models": [str(m) for m in allowed_models],
        })
    return out


def _active_plan(conn: sqlite3.Connection, project_id: str) -> Optional[Dict[str, Any]]:
    """Most recent active or dispatched plan for this project."""
    row = conn.execute(
        """SELECT * FROM plans
           WHERE project_id = ? AND status IN ('proposed', 'approved_pending_dispatch', 'revising', 'dispatched')
           ORDER BY version DESC LIMIT 1;""",
        (project_id,),
    ).fetchone()
    if row is None:
        return None

    try:
        steps = json.loads(row["steps_json"] or "[]")
    except (ValueError, TypeError):
        steps = []
    apr_status = None
    if row["approval_id"]:
        arow = conn.execute("SELECT status FROM approvals WHERE id = ?;", (row["approval_id"],)).fetchone()
        if arow is not None:
            apr_status = arow["status"]
    return {
        "id": row["id"],
        "version": row["version"],
        "status": row["status"],
        "title": redact(row["title"]) if row["title"] else row["title"],
        "summary": redact(row["summary"]) if row["summary"] else row["summary"],
        "plan_hash": row["plan_hash"],
        "approval_id": row["approval_id"],
        "approval_status": apr_status,
        "steps": redact_value(steps),
    }


def _chat_messages(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    """Last PROMPT_CHAT_LIMIT non-cancelled chat messages for this project, oldest first."""
    rows = conn.execute(
        """SELECT cm.id, cm.role, cm.kind, cm.actor_id, cm.via, cm.body, cm.meta_json, cm.created_at,
                  p.name AS actor_name
           FROM chat_messages cm
           LEFT JOIN principals p ON cm.actor_id = p.id
           WHERE cm.project_id = ?
           ORDER BY cm.event_seq DESC LIMIT ?;""",
        (project_id, PROMPT_CHAT_LIMIT * 2),
    ).fetchall()
    kept: List[Dict[str, Any]] = []
    for r in rows:
        meta_obj: Dict[str, Any] = {}
        if r["meta_json"]:
            try:
                m = json.loads(r["meta_json"])
                if isinstance(m, dict):
                    meta_obj = m
            except (TypeError, ValueError):
                meta_obj = {}
        if meta_obj.get("queued_cancelled"):
            continue
        relayed_by_name = (
            (r["actor_name"] or meta_obj.get("relayed_by_name"))
            if r["via"] == "relayed"
            else None
        )
        kept.append(
            {
                "id": r["id"],
                "role": r["role"],
                "kind": r["kind"],
                "actor_id": r["actor_id"],
                "actor_name": r["actor_name"],
                "relayed_by_name": relayed_by_name,
                "via": r["via"],
                "body": redact(r["body"]) if r["body"] else r["body"],
                "created_at": r["created_at"],
            }
        )
        if len(kept) >= PROMPT_CHAT_LIMIT:
            break
    kept.reverse()
    return kept


def build_prompt_input(conn: sqlite3.Connection, project_id: str, messages: List[sqlite3.Row],
                       limit_bytes: int, testing_mode: bool = False) -> LeadPromptInput:
    """Everything the lead prompt is rendered from (§12.6)."""
    project = conn.execute(
        "SELECT id, name, goal, plan_gate_mode, permission_mode FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    plan_gate_mode = (
        project["plan_gate_mode"]
        if (project is not None and "plan_gate_mode" in project.keys() and project["plan_gate_mode"])
        else "off"
    )
    permission_mode = (
        project["permission_mode"]
        if (project is not None and "permission_mode" in project.keys() and project["permission_mode"])
        else "plan"
    )
    chat_msgs = _chat_messages(conn, project_id)
    active_plan = _active_plan(conn, project_id)
    workers = _workers(conn, project_id, testing_mode=testing_mode) if (
        plan_gate_mode == "require_approval" or chat_msgs or active_plan is not None
    ) else []
    return LeadPromptInput(
        project={
            "id": project["id"],
            "name": redact(project["name"]) if project["name"] else project["name"],
            "goal": redact(project["goal"]) if project["goal"] else project["goal"],
        },
        criteria=_criteria(conn, project_id),
        tasks=_tasks(conn, project_id),
        approvals=_approvals(conn, project_id),
        notes=_notes(conn, project_id),
        events=_events(conn, project_id),
        messages=_messages(messages),
        authority=_authority(conn, project_id),
        limit_bytes=limit_bytes,
        workers=workers,
        active_plan=active_plan,
        chat_messages=chat_msgs,
        plan_gate_mode=plan_gate_mode,
        permission_mode=permission_mode,
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

    from mesh.core.paths import resolve_trusted_path
    from mesh.core.repos import is_registered_worktree

    repo = plan["repo"]
    project_id = plan["project_id"]
    ws_root = plan["ws_root"]
    agent_id = plan["agent_id"]
    path = plan["path"]
    trusted = resolve_trusted_path(path, root=ws_root)
    if trusted is None:
        _log(home, f"lead repo/ path of {project_id} is symlinked or outside workspaces.root; skipped")
        return None
    if not os.path.lexists(trusted):
        # Missing (never created, or removed by hand): create it now.
        try:
            return add_lead_repo_worktree(repo, project_id, ws_root, agent_id)
        except Exception as exc:  # noqa: BLE001 - the turn still runs without a repo/
            _log(home, f"lead repo/ worktree not created for {project_id}: {type(exc).__name__}: {exc}")
            return None
    if not os.path.isdir(trusted) or not is_registered_worktree(trusted, repo, root=ws_root):
        # Full review r5 P1-3 / r6 P1-3: never run git in a repo/ the lead re-pointed; keep it.
        _log(home, f"lead repo/ of {project_id} is not a worktree of the project repository; kept as it is")
        return path
    try:
        status = run_git(["status", "--porcelain"], cwd=trusted)
    except Exception as exc:  # noqa: BLE001
        _log(home, f"lead repo/ status failed for {project_id}: {type(exc).__name__}: {exc}")
        return path
    if (status.stdout or "").strip():
        _log(home, f"lead repo/ worktree of {project_id} is dirty; kept as it is (K13)")
        return path
    try:
        cp = run_git(["worktree", "remove", trusted], cwd=repo)
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

#: Round 2 P3-7 / Round 3 P1-1, P3-1: per-assignment cache for vendor launch refusals during
#: turn planning. Stores only error metadata (code, exit_code, message, details) and never a live
#: exception/traceback; keyed on settings, holds, binary stat, latest probe row, results dir
#: stat, results file + sidecar stats, and referenced fixture + sidecar stats with a 30s TTL.
VENDOR_REFUSAL_CACHE_TTL_S = 30.0
_VENDOR_REFUSAL_CACHE_MAX = 256
_VENDOR_REFUSAL_LOCK = threading.Lock()


class _VendorRefusalEntry(NamedTuple):
    code: str
    exit_code: int
    message: str
    details: Dict[str, Any]
    state_key: Tuple[Any, ...]
    results_path: Optional[str]
    fixture_paths: Tuple[str, ...]
    cached_at: float


_VENDOR_REFUSAL_CACHE: Dict[Tuple[str, str], _VendorRefusalEntry] = {}


def _vendor_refusal_signature(
    conn: sqlite3.Connection,
    adapter: Any,
    testing_mode: bool,
    binary_path: Optional[str],
    *,
    use_known: bool = False,
    results_path: Optional[str] = None,
    fixture_paths: Tuple[str, ...] = (),
) -> Tuple[Tuple[Any, ...], Optional[str], Tuple[str, ...]]:
    from mesh.adapters import evidence

    if not vendor_leads_enabled(conn):
        return (("disabled", adapter.id, bool(testing_mode)), None, ())
    if use_known:
        sig = evidence.launch_inputs_signature(
            conn, adapter, testing_mode, binary_path, known_paths=(results_path, fixture_paths),
        )
    else:
        sig = evidence.launch_inputs_signature(conn, adapter, testing_mode, binary_path)
    return (("enabled", sig.signature), sig.results_path, sig.fixture_paths)


def _plan_lead_launch_check(
    conn: sqlite3.Connection,
    home: str,
    lead: Mapping[str, Any],
    adapter: Any,
    testing_mode: bool,
    binary_path: Optional[str],
) -> Optional[str]:
    """Run ``lead_launch_check`` with per-assignment refusal caching for vendor leads."""
    if getattr(adapter, "kind", None) != "vendor":
        return lead_launch_check(conn, adapter, testing_mode, binary_path=binary_path)
    cache_id = (home or "", str(lead.get("assignment_id") or lead.get("agent_id") or ""))
    with _VENDOR_REFUSAL_LOCK:
        cached = _VENDOR_REFUSAL_CACHE.get(cache_id)
    now_mono = _monotonic()
    if cached is not None and (now_mono - cached.cached_at) < VENDOR_REFUSAL_CACHE_TTL_S:
        try:
            cur_key, _, _ = _vendor_refusal_signature(
                conn,
                adapter,
                testing_mode,
                binary_path,
                use_known=True,
                results_path=cached.results_path,
                fixture_paths=cached.fixture_paths,
            )
            matched = cur_key == cached.state_key
        except Exception:  # noqa: BLE001 - fall through to fresh check on signature error
            matched = False
        if matched:
            raise MeshError(
                code=cached.code,
                exit_code=cached.exit_code,
                message=cached.message,
                details=dict(cached.details),
            )
    try:
        warning = lead_launch_check(conn, adapter, testing_mode, binary_path=binary_path)
    except MeshError as exc:
        try:
            new_key, rpath, fpaths = _vendor_refusal_signature(conn, adapter, testing_mode, binary_path)
            entry = _VendorRefusalEntry(
                code=exc.code,
                exit_code=exc.exit_code,
                message=exc.message,
                details=dict(exc.details or {}),
                state_key=new_key,
                results_path=rpath,
                fixture_paths=fpaths,
                cached_at=_monotonic(),
            )
            with _VENDOR_REFUSAL_LOCK:
                if len(_VENDOR_REFUSAL_CACHE) >= _VENDOR_REFUSAL_CACHE_MAX and cache_id not in _VENDOR_REFUSAL_CACHE:
                    _VENDOR_REFUSAL_CACHE.pop(next(iter(_VENDOR_REFUSAL_CACHE)))
                _VENDOR_REFUSAL_CACHE[cache_id] = entry
        except Exception:  # noqa: BLE001 - never let cache-key failure hide the refusal MeshError
            pass
        raise
    with _VENDOR_REFUSAL_LOCK:
        _VENDOR_REFUSAL_CACHE.pop(cache_id, None)
    return warning


def lead_launch_check(conn: Optional[sqlite3.Connection], adapter: Any, testing_mode: bool = False,
                      binary_path: Optional[str] = None) -> Optional[str]:
    """Raise unless a lead turn may launch on ``adapter`` (rc 13); return any evidence warning.

    Vendor leads are refused by default (``vendor_lead_m3_lite``) until
    ``lead.vendor_leads`` is explicitly enabled (direct user setting). When enabled,
    vendor leads pass through ``evidence.effective_launch`` (with the lead's
    ``binary_path``) under the same ``adapter.evidence_mode`` policy as task
    executors (``warn`` allows launch with an evidence warning; ``strict`` requires
    valid launch evidence; launch holds and unimplemented stubs stay refused).
    Non-vendor adapters keep testing-mode / ``launch_enabled`` gating.
    """
    if adapter.kind == "vendor" and not vendor_leads_enabled(conn):
        # Default (off): vendor lead turns are refused whatever the evidence says.
        # Checked first so every vendor lead is refused with `vendor_lead_m3_lite`.
        raise vendor_lead_refused(adapter)
    if adapter.kind == "test_double" and not testing_mode:
        raise TestDoubleDisabled()
    from mesh.adapters.evidence import effective_launch

    ev = effective_launch(conn, adapter, testing_mode, binary_path)
    if not ev.ok:
        raise AdapterUnsupported(
            f"Adapter '{adapter.id}' is not enabled for launch"
            + (f" ({ev.reason})" if ev.reason else ""),
            milestone=getattr(ev, "milestone", None) or adapter.launch_milestone or "M3",
            reason=ev.reason,
        )
    return ev.warning


#: N19: the `lead.turn_refused` reason for a launch-enabled vendor lead when opt-in is off.
VENDOR_LEAD_REFUSED_REASON = "vendor_lead_m3_lite"


def vendor_lead_refused(adapter: Any) -> AdapterUnsupported:
    """The rc 13 error for a vendor lead turn while ``lead.vendor_leads`` is off."""
    exc = AdapterUnsupported(
        f"Adapter '{adapter.id}' cannot run launch-mode lead turns (vendor lead turns are off; set lead.vendor_leads true --user or use an inbox lead)",
        milestone="M3",
    )
    exc.details["reason"] = VENDOR_LEAD_REFUSED_REASON
    return exc


def lead_launch_allowed(conn: Optional[sqlite3.Connection], adapter: Any, testing_mode: bool = False,
                        binary_path: Optional[str] = None) -> bool:
    """True when `lead_launch_check` passes."""
    try:
        lead_launch_check(conn, adapter, testing_mode, binary_path=binary_path)
    except MeshError:
        return False
    return True


def _record_turn_refused(conn: sqlite3.Connection, project_id: str, lead: Mapping[str, Any],
                         adapter_id: str, exc: MeshError) -> None:
    """One `lead.turn_refused` event per `(lead_assignment_id, reason)`; messages stay pending."""
    assignment_id = lead.get("assignment_id")
    reason = (exc.details or {}).get("reason") or _TURN_REFUSED_REASONS.get(exc.code, "launch_refused")
    seen = conn.execute(
        "SELECT 1 FROM events WHERE type = 'lead.turn_refused' AND project_id = ? "
        "AND json_extract(payload_json, '$.lead_assignment_id') = ? "
        "AND json_extract(payload_json, '$.reason') = ? LIMIT 1;",
        (project_id, assignment_id, reason),
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
                "reason": reason,
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
        evidence_warning = _plan_lead_launch_check(
            conn, home, lead, adapter, testing_mode, binary_path=agent["binary_path"]
        )
    except MeshError as exc:
        _record_turn_refused(conn, project_id, lead, adapter_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        _log(home, f"lead turn adapter '{adapter_id}' unavailable: {type(exc).__name__}: {exc}")
        return None

    profile = str(_setting(conn, "lead.profile", DEFAULT_LEAD_PROFILE) or DEFAULT_LEAD_PROFILE)
    timeout_s = int(_setting(conn, "lead.turn_timeout_s", DEFAULT_LEAD_TURN_TIMEOUT_S)
                    or DEFAULT_LEAD_TURN_TIMEOUT_S)
    if not testing_mode:
        timeout_s = max(60, timeout_s)
    limit_bytes = int(getattr(adapter, "max_argv_prompt_bytes", LEAD_PROMPT_LIMIT_BYTES)
                      or LEAD_PROMPT_LIMIT_BYTES)

    # --- Render the prompt (before TX: it decides which messages are included) ---
    batch = list(messages)[:LEAD_WAKE_BATCH_MAX]
    inp = build_prompt_input(conn, project_id, batch, limit_bytes, testing_mode=testing_mode)
    rendered = render_lead_prompt(inp)
    included = list(rendered.included_message_ids)
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
        "evidence_warning": evidence_warning,
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
    evidence_warning = plan.get("evidence_warning")
    try:
        adapter = get_adapter(adapter_id, testing_mode=testing_mode,
                              options=json.loads(agent["adapter_options_json"] or "{}"))
    except Exception as exc:  # noqa: BLE001
        _log(home, f"lead turn adapter '{adapter_id}' unavailable: {type(exc).__name__}: {exc}")
        return None

    from mesh.core.policy import resolve_model_and_effort

    resolved_model, resolved_effort = resolve_model_and_effort(
        default_model=agent["default_model"],
        default_effort=agent["default_effort"],
    )
    run_id = generate_id("run")
    run_dir = os.path.join(home, "runs", run_id)
    ws_root = get_workspaces_root(conn, home)
    lead_dir = compute_lead_workspace_path(ws_root, project_id, agent["id"])
    now = now_iso_utc()
    now_ts = time.time()
    deadline_at = now_iso_utc(now_ts + timeout_s)
    token_expires_at = now_iso_utc(now_ts + timeout_s + RUN_TOKEN_EXPIRY_GRACE_S)
    lease_ttl_s = get_lease_ttl_s(conn, testing_mode=testing_mode)
    boot_id = _boot_id(conn)

    # --- TX: messages delivering, run row, run token, lease ---
    try:
        with transaction(conn):
            if not _lease_free(conn, project_id):
                raise sqlite3.IntegrityError("leadturn lease already held")
            # Pre-public re-review P3: a disabled lead agent never starts a turn.
            dis = conn.execute("SELECT disabled_at FROM principals WHERE id = ?;", (agent["id"],)).fetchone()
            if dis is None or dis["disabled_at"] is not None:
                raise sqlite3.IntegrityError("lead agent is disabled")
            conn.execute(
                """INSERT INTO runs (
                    id, kind, task_id, project_id, agent_id, attempt, fence,
                    adapter, model, effort, profile, status, boot_id, run_dir,
                    argv_redacted_json, started_at, deadline_at, lead_assignment_id
                ) VALUES (?, 'lead_turn', NULL, ?, ?, NULL, 0, ?, ?, ?, ?, 'starting', ?, ?,
                          '[]', ?, ?, ?);""",
                (run_id, project_id, agent["id"], adapter_id, resolved_model, resolved_effort,
                 profile, boot_id, run_dir, now, deadline_at, lead["assignment_id"]),
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
            turn_payload: Dict[str, Any] = {
                "run_id": run_id,
                "project_id": project_id,
                "lead_assignment_id": lead["assignment_id"],
                "messages": len(included),
                "prompt_bytes": rendered.bytes,
                "omitted": rendered.omitted,
            }
            if evidence_warning:
                turn_payload["evidence_warning"] = evidence_warning
            record_event(
                conn=conn,
                type_="lead.turn_started",
                actor_id="system",
                actor_kind="system",
                via="system",
                project_id=project_id,
                run_id=run_id,
                payload=turn_payload,
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
        assignment = conn.execute("SELECT session_id FROM lead_assignments WHERE id = ?;",
                                  (lead["assignment_id"],)).fetchone()
        request = RunRequest(
            prompt_path=prompt_path,
            workspace=lead_dir,
            model=resolved_model,
            effort=resolved_effort,
            profile=profile,
            timeout_s=timeout_s,
            resume_session_id=verified_resume_session(
                adapter,
                assignment["session_id"] if assignment else None,
                conn=conn,
                testing_mode=testing_mode,
                binary_path=agent["binary_path"],
            ),
            options=build_run_options(agent, run_id),
        )
        from mesh.daemon.supervisor import apply_cmd_pointer_fallback, register_run_job, register_wrapper
        from mesh.platform import ipc as plat_ipc, proc as plat_proc

        spec_cmd = adapter.build(request)
        strategy, argv = apply_cmd_pointer_fallback(
            list(spec_cmd.argv),
            strategy,
            prompt_path=prompt_path,
            prompt_text=rendered.text,
            run_dir=run_dir,
            adapter_id=adapter_id,
            profile=profile,
        )
        spec = build_spec(
            argv=argv,
            cwd=spec_cmd.cwd,
            stdin=spec_cmd.stdin,
            redact_env_names=["MESH_TOKEN"],
            idle_timeout_s=0,
        )
        _write_json(os.path.join(run_dir, "spec.json"), spec)

        socket_path = plat_ipc.daemon_endpoint_path(home)
        child_env = build_lead_child_env(
            base_env=dict(os.environ),
            token=token,
            run_id=run_id,
            project_id=project_id,
            socket_path=socket_path,
        )
        run_job = None
        if sys.platform == "win32":
            run_job = plat_proc.create_run_job(home, run_id, kill_on_close=False)
            register_run_job(run_id, run_job, home=home)
        popen_extra: Dict[str, Any] = {"job": run_job} if run_job is not None else {}
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
            **popen_extra,
        )
        from mesh.core.proc import get_proc_stat

        register_wrapper(run_id, proc)
        # --- TX B: run running with wrapper identity (inside try so any TX B error aborts) ---
        stat_info = get_proc_stat(proc.pid)
        if sys.platform == "win32":
            register_run_job(
                run_id,
                run_job,
                pgid=proc.pid,
                wrapper_starttime=stat_info["starttime"] if stat_info else None,
                home=home,
            )
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
            testing_mode=testing_mode,
        )
    return run_id


def _write_text(path: str, text: str, mode: int = 0o600) -> None:
    from mesh.platform import perms as plat_perms

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
        if sys.platform == "win32":
            plat_perms.fchmod_private(fd, mode)
    finally:
        os.close(fd)
    if sys.platform != "win32":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _write_json(path: str, obj: Any, mode: int = 0o600) -> None:
    _write_text(path, json.dumps(obj, sort_keys=True) + "\n", mode)


def _fail_lead_turn_spawn(conn: sqlite3.Connection, run_id: str, project_id: str,
                          message: str, after_popen: Optional[Dict[str, Any]] = None,
                          testing_mode: Optional[bool] = None) -> None:
    """A lead turn that could not spawn: close it through the gate and put its messages back (§12.4)."""
    from mesh.daemon import supervisor

    tmode = supervisor._testing_mode() if testing_mode is None else bool(testing_mode)
    payload: Dict[str, Any] = {"reason": "spawn_failed", "message": message}
    if after_popen:
        payload["after_popen"] = after_popen
        if after_popen.get("pid"):
            conn.execute(
                "UPDATE runs SET wrapper_pid = COALESCE(wrapper_pid, ?), pgid = COALESCE(pgid, ?), "
                "proc_starttime = COALESCE(proc_starttime, ?) WHERE id = ?;",
                (after_popen["pid"], after_popen["pid"], after_popen.get("starttime"), run_id),
            )
    run = conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
    if run is None:
        raise RuntimeError(f"lead_turn run '{run_id}' missing in _fail_lead_turn_spawn")
    supervisor._close_run(
        conn, run, "spawn_failed", "spawn_failed", None, payload, testing_mode=tmode,
    )
    _release_messages(conn, run_id, ok=False, project_id=project_id, testing_mode=tmode)


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
        "SELECT id, attempts FROM outbox "
        "WHERE delivering_run_id = ? AND channel = 'lead_wake' AND status = 'delivering';",
        (run_id,),
    ).fetchall()
    return _release_lead_wake_rows(conn, rows, ok, project_id, run_id, testing_mode)


def _release_lead_wake_rows(
    conn: sqlite3.Connection,
    rows: Sequence[Mapping[str, Any]],
    ok: bool,
    project_id: Optional[str],
    run_id: Optional[str],
    testing_mode: bool = False,
) -> Dict[str, int]:
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
    if dead_ids and project_id:
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


def _queued_merge_ids_for(run_id: str) -> set:
    with _QUEUED_MERGES_LOCK:
        return {m["request_id"] for m in _QUEUED_MERGES if m.get("run_id") == run_id}


def _discard_queued_merges_since(run_id: str, keep_ids: set) -> None:
    with _QUEUED_MERGES_LOCK:
        _QUEUED_MERGES[:] = [
            m for m in _QUEUED_MERGES
            if not (m.get("run_id") == run_id and m.get("request_id") not in keep_ids)
        ]


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
            principal, cred_err = _lead_turn_principal(conn, run)
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
                "run_id": run["id"], "project_id": run["project_id"], "index": index,
                "request_id": row["request_id"], "principal": principal, "args": args,
                "action_args": action_args or args,
            }
            proj_row = conn.execute("SELECT status FROM projects WHERE id = ?;", (run["project_id"],)).fetchone()
            if proj_row is not None and proj_row["status"] == "cancelled":
                with transaction(conn):
                    record_queued_merge_failure(
                        conn,
                        item,
                        MeshError(
                            code=c.PROJECT_NOT_ACTIVE,
                            exit_code=c.EXIT_CONFLICT,
                            message=f"Project '{run['project_id']}' is cancelled",
                            details={"project_id": run["project_id"], "status": "cancelled"},
                        ),
                    )
                continue
            disabled_row = conn.execute(
                "SELECT disabled_at FROM principals WHERE id = ?;", (principal["id"],)
            ).fetchone()
            if (cred_err is not None and cred_err.code == c.TOKEN_INVALID) or (
                disabled_row is not None and disabled_row["disabled_at"] is not None
            ):
                dis_err = (
                    cred_err
                    if (cred_err is not None and cred_err.code == c.TOKEN_INVALID)
                    else MeshError(
                        code=c.TOKEN_INVALID,
                        exit_code=c.EXIT_UNAUTHENTICATED,
                        message="Principal is disabled",
                    )
                )
                with transaction(conn):
                    record_queued_merge_failure(conn, item, dis_err)
                continue
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
                "project_id": run["project_id"],
                "index": action.index,
                "request_id": request_id,
                "principal": dict(principal),
                "args": dict(eff.params.get("args") or args),
                "action_args": action.args,
            })
            extra = {"status": "queued", "args": dict(eff.params.get("args") or args),
                     "action_args": action.args}
        unlinks = [e for e in others if e.kind == "unlink_deliver_intent"] if ok else []
        others = [e for e in others if e.kind != "unlink_deliver_intent"]
        for eff in unlinks:
            # Full review r3 P1-4: dropped only after the turn's transaction commits
            # (`finish_lead_turn_committed`); until then recovery needs it.
            _PENDING_INTENT_UNLINKS.append(eff.params["path"])
        if others:
            _log(home, f"lead action {action.cmd} of run {run['id']} queued effects that were not run: "
                       f"{[e.kind for e in others]}")
        results.append(_record_action_result(
            conn, run, principal, action.index, action.cmd, action.args, ok, exit_code,
            None if ok else error.get("code"), None if ok else error.get("message"),
            request_id, extra))
    return results

#: Deliver intents of lead actions, unlinked once the turn's transaction committed.
_PENDING_INTENT_UNLINKS: List[str] = []


def finish_lead_turn_committed(conn: sqlite3.Connection, home: str, run: Mapping[str, Any],
                               exit_info: Optional[Dict[str, Any]], testing_mode: bool = False) -> str:
    """`finish_lead_turn` in its own transaction; then the deliver intents its actions
    left are dropped (r3 P1-4). On a rollback they stay for recovery."""
    from mesh.core.db import transaction

    del _PENDING_INTENT_UNLINKS[:]
    run_id = run["id"] if "id" in run.keys() else None
    if run_id is not None:
        from mesh.daemon.supervisor import close_gate

        close_gate(run, "lost" if exit_info is None else "exited")
    merges_before = _queued_merge_ids_for(run_id) if run_id is not None else set()
    settle_project = run["project_id"] if "project_id" in run.keys() else None
    if settle_project:
        # Full review r4 P1-1: an earlier lost delivery of this project is settled in
        # its own transaction before the turn's actions run (as `handle_client` does).
        from mesh.daemon.recovery import settle_deliver_intent

        try:
            settle_deliver_intent(conn, home, settle_project)
        except Exception as exc:  # noqa: BLE001 - a deliver action then refuses
            _log(home, f"deliver intent of {settle_project} not settled: {type(exc).__name__}: {exc}")
    try:
        with transaction(conn):
            if run_id is not None:
                outcome = finish_lead_turn(conn, home, run, exit_info, testing_mode, pre_gated=True)
            else:
                outcome = finish_lead_turn(conn, home, run, exit_info, testing_mode)
    except BaseException:
        del _PENDING_INTENT_UNLINKS[:]
        if run_id is not None:
            _discard_queued_merges_since(run_id, merges_before)
        raise
    paths, _PENDING_INTENT_UNLINKS[:] = list(_PENDING_INTENT_UNLINKS), []
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
    return outcome


def _token_expired_before_finish(
    conn: sqlite3.Connection,
    run: Mapping[str, Any],
    exit_info: Optional[Dict[str, Any]],
) -> bool:
    """True when an exited lead turn's run token expired before finishing (e.g. gate hold or downtime)."""
    from mesh.daemon import supervisor

    run_id = run["id"]
    ended_at = (exit_info or {}).get("ended_at")
    if isinstance(ended_at, str) and ended_at:
        exp = conn.execute(
            "SELECT expires_at FROM credentials WHERE kind = 'run_token' "
            "AND json_extract(scope_json, '$.run_id') = ? ORDER BY created_at DESC LIMIT 1;",
            (run_id,),
        ).fetchone()
        if exp and isinstance(exp["expires_at"], str) and exp["expires_at"] and ended_at <= exp["expires_at"]:
            return True
    if run_id in supervisor._CLOSE_REFUSED_LOGGED or run_id in supervisor._SEEN_UNUSABLE:
        return True
    return bool(conn.execute("SELECT 1 FROM meta WHERE key = ?;", (supervisor.seen_hold_key(run_id),)).fetchone())


def finish_lead_turn(conn: sqlite3.Connection, home: str, run: Mapping[str, Any],
                     exit_info: Optional[Dict[str, Any]],
                     testing_mode: bool = False,
                     *,
                     pre_gated: bool = False) -> str:
    """Close a lead turn and apply its outcome (§12.4). Caller owns the transaction.

    The supervisor routes `kind='lead_turn'` runs here, never to T7/T8: a lead turn
    has no task, so no task transition may fire. Close-gate checks run first so a
    held close never runs `execute_lead_actions` or releases outbox messages
    prematurely; on exit 0 once clear, actions run, messages ack and the run closes.
    """
    from mesh.daemon.supervisor import _close_run, close_gate, normalize_exit_info, vendor_failure_meaning

    if not pre_gated:
        close_gate(run, "lost" if exit_info is None else "exited")
    project_id = run["project_id"]
    exit_code = None
    signo = None
    capture_error = None
    vendor_meaning = None
    if exit_info is not None:
        exit_code, signo = normalize_exit_info(exit_info)
        capture_error = exit_info.get("capture_error")
        if exit_code == 0 and signo is None and not capture_error:
            vendor_meaning = vendor_failure_meaning(run["run_dir"], run["adapter"], testing_mode=testing_mode)
    ok = (
        exit_info is not None
        and exit_code == 0
        and signo is None
        and not capture_error
        and vendor_meaning is None
    )
    actions: Optional[Dict[str, int]] = None
    actions_deferred: Optional[str] = None
    chat_reply_count = 0
    if ok:
        # Q10: the actions run while the run is still open and its token valid; a
        # terminated turn (set-lead, timeout) never reaches here.
        conn.execute("SAVEPOINT lead_actions;")
        pending_before = len(_PENDING_INTENT_UNLINKS)
        merges_before = _queued_merge_ids_for(run["id"])
        token_expired_deferred = False
        try:
            executed = execute_lead_actions(conn, home, run, testing_mode)
            if any(r.get("error_code") == c.TOKEN_EXPIRED for r in executed) and _token_expired_before_finish(
                conn, run, exit_info
            ):
                token_expired_deferred = True
                raise RuntimeError("lead run token expired before finish")
        except Exception as exc:  # noqa: BLE001 - a broken action pass must not wedge the run
            conn.execute("ROLLBACK TO lead_actions;")
            # r4 P1-1: the rolled-back deliveries' intents stay for recovery.
            del _PENDING_INTENT_UNLINKS[pending_before:]
            _discard_queued_merges_since(run["id"], merges_before)
            executed = []
            ok = False
            if token_expired_deferred:
                actions_deferred = "token_expired"
                _log(home, f"lead actions of run {run['id']} deferred (token expired before finish); retrying wake")
            else:
                _log(home, f"lead actions of run {run['id']} not executed: {type(exc).__name__}: {exc}")
        conn.execute("RELEASE lead_actions;")
        actions = {"ok": sum(1 for r in executed if r["ok"]),
                   "failed": sum(1 for r in executed if not r["ok"])}
        chat_reply_count = sum(1 for r in executed if r["ok"] and r.get("cmd") == "chat.reply")

        # Safe fallback reply on chat-first projects if lead exited 0 without emitting chat.reply (P3-2)
        if ok and chat_reply_count == 0 and project_id:
            prow = conn.execute("SELECT plan_gate_mode FROM projects WHERE id = ?;", (project_id,)).fetchone()
            had_chat_wake = conn.execute(
                "SELECT 1 FROM outbox WHERE delivering_run_id = ? AND channel = 'lead_wake' AND type = 'chat.message' LIMIT 1;",
                (run["id"],),
            ).fetchone() is not None
            if (prow is not None and prow["plan_gate_mode"] == "require_approval") or had_chat_wake:
                fallback_raw = extract_fallback_reply_text(lead_turn_final_text(run, testing_mode))
                if fallback_raw:
                    fallback_body = redact(fallback_raw)
                    fb_msg_id = generate_id("cmsg")
                    fb_now = now_iso_utc()
                    fb_ev = record_event(
                        conn=conn,
                        type_="chat.message",
                        actor_id=run["agent_id"] or "principal_system",
                        actor_kind="agent" if run["agent_id"] else "system",
                        via="lead_turn",
                        project_id=project_id,
                        run_id=run["id"],
                        payload={
                            "message_id": fb_msg_id,
                            "project_id": project_id,
                            "role": "lead",
                            "kind": "text",
                            "body": fallback_body,
                            "fallback_reply": True,
                        },
                    )
                    conn.execute(
                        """INSERT INTO chat_messages (
                            id, project_id, event_seq, role, kind, actor_id, actor_kind, via,
                            run_id, plan_id, approval_id, task_id, body, meta_json,
                            consumed_by_approval_id, created_at
                        ) VALUES (?, ?, ?, 'lead', 'text', ?, ?, 'lead_turn',
                                  ?, NULL, NULL, NULL, ?, '{"fallback_reply":true}', NULL, ?);""",
                        (
                            fb_msg_id,
                            project_id,
                            fb_ev["seq"],
                            run["agent_id"] or "principal_system",
                            "agent" if run["agent_id"] else "system",
                            run["id"],
                            fallback_body,
                            fb_now,
                        ),
                    )
                    chat_reply_count = 1
    counts = _release_messages(conn, run["id"], ok, project_id, testing_mode)
    # P3-1: Emit lead.turn_finished only on chat-gated or chat-active projects so legacy projects stay unchanged.
    if project_id and (chat_reply_count > 0 or _project_uses_chat(conn, project_id)):
        record_event(
            conn=conn,
            type_="lead.turn_finished",
            actor_id="system",
            actor_kind="system",
            via="system",
            project_id=project_id,
            run_id=run["id"],
            payload={
                "run_id": run["id"],
                "project_id": project_id,
                "ok": ok,
                "exit_code": exit_code,
                "chat_reply_count": chat_reply_count,
                "actions_summary": actions or {"ok": 0, "failed": 0},
            },
        )
    result = {
        "exit_code": exit_code,
        "signal": signo,
        "ended_at": (exit_info or {}).get("ended_at"),
        "messages": counts,
    }
    if capture_error:
        result["capture_error"] = capture_error
    if vendor_meaning is not None:
        result["vendor_exit_meaning"] = vendor_meaning
    if actions is not None:
        result["actions"] = actions
    if actions_deferred is not None:
        result["actions_deferred"] = actions_deferred
    if exit_info is None:
        _close_run(
            conn,
            run,
            "lost",
            "lost",
            None,
            {"reason": "lost", "messages": counts},
            testing_mode=testing_mode,
        )
        return "lost"
    _close_run(conn, run, "exited", "exited", exit_code, result, testing_mode=testing_mode)
    if signo is not None:
        conn.execute("UPDATE runs SET signal = ? WHERE id = ?;", (signo, run["id"]))
    return "acked" if ok else "retry"


LEAD_INTERRUPT_META_PREFIX = "lead_interrupt:"


def lead_interrupt_meta_key(run_id: str) -> str:
    return f"{LEAD_INTERRUPT_META_PREFIX}{run_id}"


def lead_turn_termination_reason(conn: sqlite3.Connection, run: Mapping[str, Any]) -> Optional[str]:
    """Why a live lead turn must be terminated now, or None (§11.3, §11.4).

    A lead turn has no task, so no task status, no post-submit grace and no idle
    timeout can end it. Durable termination reasons checked on every watch pass:
    the turn's assignment is no longer current (`lead_superseded`), the project
    was cancelled (`project_cancelled`), an explicit direct-user interrupt was
    requested (`user_interrupted`), or the deadline expired (`timeout`).
    """
    current = get_project_active_lead(conn, run["project_id"])
    if current is None or current["assignment_id"] != run["lead_assignment_id"] or not current["can_lead"]:
        return "lead_superseded"  # r2 P2-C: also when the agent's can_lead was revoked
    prow = conn.execute("SELECT status FROM projects WHERE id = ?;", (run["project_id"],)).fetchone()
    if prow is None or prow["status"] == "cancelled":
        return "project_cancelled"
    run_id = run["id"] if "id" in run.keys() else None
    if run_id and conn.execute(
        "SELECT 1 FROM meta WHERE key = ?;",
        (lead_interrupt_meta_key(run_id),),
    ).fetchone() is not None:
        return "user_interrupted"
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
        entry["done_at"] = _monotonic()


def _lead_repo_state(home: str, plan: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The finished `repo/` refresh for this turn, or None while it is in flight."""
    key = (plan["project_id"], plan["assignment_id"])
    with _LEAD_REPO_PREP_LOCK:
        entry = _LEAD_REPO_PREP.get(key)
        if entry is not None:
            if not entry["done"]:
                return None
            del _LEAD_REPO_PREP[key]
            if _monotonic() - entry.get("done_at", 0.0) <= LEAD_REPO_PREP_MAX_AGE_S:
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
           JOIN principals pr ON pr.id = a.id
           WHERE la.ended_at IS NULL AND a.delivery = 'launch' AND p.status = 'active'
             AND pr.disabled_at IS NULL
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
