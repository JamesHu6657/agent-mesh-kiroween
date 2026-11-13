"""Append-only event log and hash chain verification per plan §5.5 and §12.1.

Event hash chain rule:
    hash = sha256(prev_hash + "\\n" + canonical_json(event without hash/user_mac))
    The first event uses prev_hash = "genesis".
"""

import hashlib
import hmac
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from mesh.core.redact import redact, redact_value
from mesh.core.canonical import canonical_json
from mesh.core.clock import now_utc
from mesh.core.ids import generate_id

EXCLUDED_EVENT_HASH_KEYS = {"seq", "hash", "user_mac"}


def compute_user_event_mac(k_user: bytes, event_hash: str) -> str:
    """Compute HMAC-SHA256 user MAC for direct user events per plan §7.4."""
    msg = f"mesh-event-v1\n{event_hash}".encode("utf-8")
    return hmac.new(k_user, msg, hashlib.sha256).hexdigest()


def compute_event_hash(event_data: Dict[str, Any], prev_hash: str) -> str:
    """Compute the tamper-evident SHA-256 hash for an event row per plan §5.5.

    Args:
        event_data: Dictionary containing event attributes.
        prev_hash: Hash of preceding event, or 'genesis' for the first event.

    Returns:
        64-character lowercase hexadecimal SHA-256 string.
    """
    # Clean copy without seq, hash, user_mac
    cleaned = {
        k: v for k, v in event_data.items()
        if k not in EXCLUDED_EVENT_HASH_KEYS
    }
    # Ensure prev_hash is recorded in event data
    cleaned["prev_hash"] = prev_hash

    # canonical_json ensures keys are sorted and floats are forbidden
    canonical_bytes = canonical_json(cleaned)
    payload = (prev_hash + "\n").encode("utf-8") + canonical_bytes
    return hashlib.sha256(payload).hexdigest()


def record_event(
    conn: sqlite3.Connection,
    type_: Optional[str] = None,
    actor_id: str = "",
    actor_kind: str = "",
    via: str = "",
    project_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    approval_id: Optional[str] = None,
    relayed_by: Optional[str] = None,
    credential_id: Optional[str] = None,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    reason: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    user_mac: Optional[str] = None,
    ts: Optional[str] = None,
    event_type: Optional[str] = None,
    k_user: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Insert a new event into the events table with verified prev_hash and hash.

    Must be called within an active transaction.
    """
    effective_type = type_ or event_type or ""
    # Determine prev_hash from the last inserted event
    cursor = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1;")
    last_row = cursor.fetchone()
    if last_row is None:
        prev_hash = "genesis"
    else:
        prev_hash = last_row["hash"]

    event_id = generate_id("evt")
    if ts is None:
        ts = now_utc()

    # Plan §15.2: event reason and payload are redacted before they are stored.
    if reason:
        reason = redact(reason)
    payload_json = json.dumps(redact_value(payload or {}), sort_keys=True, separators=(",", ":"))

    event_dict = {
        "id": event_id,
        "ts": ts,
        "type": effective_type,
        "project_id": project_id,
        "task_id": task_id,
        "run_id": run_id,
        "approval_id": approval_id,
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "via": via,
        "relayed_by": relayed_by,
        "credential_id": credential_id,
        "from_state": from_state,
        "to_state": to_state,
        "reason": reason,
        "payload_json": payload_json,
        "request_id": request_id,
        "prev_hash": prev_hash,
    }

    event_hash = compute_event_hash(event_dict, prev_hash)
    if user_mac is None and k_user is not None:
        user_mac = compute_user_event_mac(k_user, event_hash)

    conn.execute(
        """
        INSERT INTO events (
            id, ts, type, project_id, task_id, run_id, approval_id,
            actor_id, actor_kind, via, relayed_by, credential_id,
            from_state, to_state, reason, payload_json, request_id,
            prev_hash, hash, user_mac
        ) VALUES (
            :id, :ts, :type, :project_id, :task_id, :run_id, :approval_id,
            :actor_id, :actor_kind, :via, :relayed_by, :credential_id,
            :from_state, :to_state, :reason, :payload_json, :request_id,
            :prev_hash, :hash, :user_mac
        );
        """,
        {
            **event_dict,
            "hash": event_hash,
            "user_mac": user_mac,
        },
    )

    cursor = conn.execute("SELECT * FROM events WHERE id = ?;", (event_id,))
    row = cursor.fetchone()
    event_row = dict(row)

    fan_out_event(conn, event_row)
    return event_row


def _get_project_lead_info(conn: sqlite3.Connection, project_id: str) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """SELECT la.id as assignment_id, la.agent_id, a.delivery
           FROM lead_assignments la
           JOIN agents a ON la.agent_id = a.id
           WHERE la.project_id = ? AND la.ended_at IS NULL
           LIMIT 1;""",
        (project_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _approval_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value_json FROM settings WHERE key = 'approval.mode';").fetchone()
    if row and row["value_json"]:
        try:
            return json.loads(row["value_json"])
        except Exception:
            pass
    return "passphrase"


def _get_user_id(conn: sqlite3.Connection) -> Optional[str]:
    cur = conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;")
    row = cur.fetchone()
    return row["id"] if row else None


def _get_origin_agent_id(conn: sqlite3.Connection, project_id: str) -> Optional[str]:
    cur = conn.execute("SELECT origin_principal_id FROM projects WHERE id = ?;", (project_id,))
    row = cur.fetchone()
    return row["origin_principal_id"] if row and row["origin_principal_id"] else None


def _insert_outbox_msg(
    conn: sqlite3.Connection,
    event_seq: int,
    recipient_id: str,
    channel: str,
    dedup_key: str,
    project_id: Optional[str],
    msg_type: str,
    payload_json: str,
    now: str,
) -> None:
    msg_id = generate_id("msg")
    conn.execute(
        """INSERT INTO outbox (
            id, event_seq, recipient_id, channel, dedup_key, project_id,
            type, payload_json, status, attempts, next_attempt_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
        ON CONFLICT(dedup_key) DO NOTHING;""",
        (msg_id, event_seq, recipient_id, channel, dedup_key, project_id, msg_type, payload_json, now, now),
    )


def fan_out_event(conn: sqlite3.Connection, event: Dict[str, Any]) -> None:
    """Fan out an event to recipient inboxes per Table 12.2 in plan §12.2."""
    etype = event.get("type", "")
    project_id = event.get("project_id")
    task_id = event.get("task_id")
    approval_id = event.get("approval_id")
    run_id = event.get("run_id")
    event_seq = event.get("seq", 0)
    event_id = event.get("id", "")
    payload_raw = event.get("payload_json") or "{}"
    now = event.get("ts") or now_utc()

    try:
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else (payload_raw or {})
    except Exception:
        payload = {}

    # 1. project.created, project.request_received -> lead
    if etype in ("project.created", "project.request_received") and project_id:
        lead = _get_project_lead_info(conn, project_id)
        if lead:
            channel = "lead_wake" if lead["delivery"] == "launch" else "inbox"
            dedup = f"{etype}:{project_id}:{event_id}:{lead['assignment_id']}"
            _insert_outbox_msg(conn, event_seq, lead["agent_id"], channel, dedup, project_id, etype, payload_raw, now)

    # 2. project.handover -> new lead
    elif etype == "project.handover" and project_id:
        asg_id = payload.get("new_assignment_id") or payload.get("assignment_id")
        new_lead = _get_project_lead_info(conn, project_id)
        if asg_id and new_lead:
            channel = "lead_wake" if new_lead["delivery"] == "launch" else "inbox"
            dedup = f"handover:{asg_id}"
            _insert_outbox_msg(conn, event_seq, new_lead["agent_id"], channel, dedup, project_id, etype, payload_raw, now)

    # 3. task.offered -> inbox assignee
    elif etype == "task.offered" and task_id:
        fence = payload.get("fence", 0)
        cur_t = conn.execute("SELECT assignee_agent_id, version FROM tasks WHERE id = ?;", (task_id,))
        trow = cur_t.fetchone()
        assignee_id = trow["assignee_agent_id"] if trow else payload.get("assignee_id")
        tver = trow["version"] if (trow and "version" in trow.keys()) else payload.get("version", 1)
        if assignee_id:
            dedup = f"offered:{task_id}:{fence}:v{tver}"
            _insert_outbox_msg(conn, event_seq, assignee_id, "inbox", dedup, project_id, etype, payload_raw, now)

    # 3b. task.rejected -> inbox assignee (the reason travels with it; launch agents get
    #     it in the next attempt's prompt instead)
    elif etype == "task.rejected" and task_id:
        cur_t = conn.execute(
            "SELECT t.assignee_agent_id, a.delivery FROM tasks t "
            "LEFT JOIN agents a ON a.id = t.assignee_agent_id WHERE t.id = ?;",
            (task_id,),
        )
        trow = cur_t.fetchone()
        if trow and trow["assignee_agent_id"] and trow["delivery"] == "inbox":
            dedup = f"rejected:{task_id}:{event_id}"
            _insert_outbox_msg(conn, event_seq, trow["assignee_agent_id"], "inbox", dedup, project_id, etype,
                               payload_raw, now)

    # 4. task.submitted, task.failed -> lead
    elif etype in ("task.submitted", "task.failed") and project_id:
        lead = _get_project_lead_info(conn, project_id)
        fence = payload.get("fence", 0)
        if lead:
            channel = "lead_wake" if lead["delivery"] == "launch" else "inbox"
            if etype == "task.failed" and task_id:
                cur_t = conn.execute("SELECT version FROM tasks WHERE id = ?;", (task_id,))
                trow = cur_t.fetchone()
                tver = trow["version"] if (trow and "version" in trow.keys()) else payload.get("version", 1)
                dedup = f"{etype}:{task_id}:{fence}:{lead['assignment_id']}:v{tver}"
            else:
                dedup = f"{etype}:{task_id}:{fence}:{lead['assignment_id']}"
            _insert_outbox_msg(conn, event_seq, lead["agent_id"], channel, dedup, project_id, etype, payload_raw, now)

    # 4b. task.lease_expired -> lead, only when the task did not fail with it (a failed
    #     task already notifies the lead with task.failed reason lease_expired)
    elif etype == "task.lease_expired" and project_id and not payload.get("task_failed"):
        lead = _get_project_lead_info(conn, project_id)
        if lead:
            channel = "lead_wake" if lead["delivery"] == "launch" else "inbox"
            dedup = f"{etype}:{task_id}:{run_id}:{lead['assignment_id']}"
            _insert_outbox_msg(conn, event_seq, lead["agent_id"], channel, dedup, project_id, etype, payload_raw, now)

    # 5. task.blocked, task.merged, merge.failed -> lead
    elif etype in ("task.blocked", "task.merged", "merge.failed") and project_id:
        lead = _get_project_lead_info(conn, project_id)
        if lead:
            channel = "lead_wake" if lead["delivery"] == "launch" else "inbox"
            dedup = f"{etype}:{task_id}:{event_id}:{lead['assignment_id']}"
            _insert_outbox_msg(conn, event_seq, lead["agent_id"], channel, dedup, project_id, etype, payload_raw, now)

    # 6. approval.requested -> user (inbox) + origin agent (inbox)
    elif etype == "approval.requested" and approval_id:
        user_id = _get_user_id(conn)
        origin_id = _get_origin_agent_id(conn, project_id) if project_id else None
        recipients = [r for r in [user_id, origin_id] if r]
        # Delegated mode: the project's inbox lead asks the human, so it is told too.
        if project_id and _approval_mode(conn) == "delegated":
            lead = _get_project_lead_info(conn, project_id)
            if lead and lead["delivery"] == "inbox" and lead["agent_id"] not in recipients:
                recipients.append(lead["agent_id"])
        for r in recipients:
            dedup = f"apr_req:{approval_id}:{r}"
            _insert_outbox_msg(conn, event_seq, r, "inbox", dedup, project_id, etype, payload_raw, now)

    # 7. approval.decided, approval.expired -> requester
    elif etype in ("approval.decided", "approval.expired") and approval_id:
        cur_a = conn.execute("SELECT requested_by, status FROM approvals WHERE id = ?;", (approval_id,))
        arow = cur_a.fetchone()
        requester = arow["requested_by"] if arow else payload.get("requested_by")
        status = arow["status"] if arow else payload.get("status", "decided")
        if requester:
            dedup = f"apr_dec:{approval_id}:{status}"
            _insert_outbox_msg(conn, event_seq, requester, "inbox", dedup, project_id, etype, payload_raw, now)
            # Keep inbox delivery, and let the normal debounced scheduler consume
            # a wake only when the requester is this project's launch-mode lead.
            lead = _get_project_lead_info(conn, project_id) if project_id else None
            if lead and lead["agent_id"] == requester and lead["delivery"] == "launch":
                _insert_outbox_msg(conn, event_seq, requester, "lead_wake", f"{dedup}:lead_wake",
                                   project_id, etype, payload_raw, now)

    # 8. task.escalated, project.completed, lead.unreachable, run.kill_failed -> user + origin agent
    elif etype in ("task.escalated", "project.completed", "lead.unreachable", "run.kill_failed"):
        # Review m2-r9 finding 5: `lead.unreachable` is per episode, so its dedup
        # subject is the run id — a second dead episode in the same project must reach
        # the user again (§11.6). It stays deterministic, so recovery is idempotent.
        if etype == "lead.unreachable":
            subject_id = run_id or event_id
        else:
            subject_id = task_id or project_id or run_id or event_id
        user_id = _get_user_id(conn)
        origin_id = _get_origin_agent_id(conn, project_id) if project_id else None
        recipients = [r for r in [user_id, origin_id] if r]
        tver = None
        if etype == "task.escalated" and task_id:
            cur_t = conn.execute("SELECT version FROM tasks WHERE id = ?;", (task_id,))
            trow = cur_t.fetchone()
            tver = trow["version"] if (trow and "version" in trow.keys()) else payload.get("version", 1)
        for r in recipients:
            if etype == "task.escalated" and tver is not None:
                dedup = f"{etype}:{subject_id}:{r}:v{tver}"
            else:
                dedup = f"{etype}:{subject_id}:{r}"
            _insert_outbox_msg(conn, event_seq, r, "inbox", dedup, project_id, etype, payload_raw, now)

    # 9. project.lead_changed -> old lead
    elif etype == "project.lead_changed":
        old_lead = payload.get("old_lead_id")
        asg = payload.get("new_assignment_id") or payload.get("assignment_id")
        if old_lead and asg:
            dedup = f"lead_changed:{asg}:{old_lead}"
            _insert_outbox_msg(conn, event_seq, old_lead, "inbox", dedup, project_id, etype, payload_raw, now)

    # 10. security.credential_in_run -> user + project lead
    elif etype in ("security.credential_in_run", "auth.credential_in_run"):
        user_id = _get_user_id(conn)
        lead = _get_project_lead_info(conn, project_id) if project_id else None
        recipients = [r for r in [user_id, lead["agent_id"] if lead else None] if r]
        # The audit event is auth.credential_in_run; the inbox message type is
        # security.credential_in_run (§7.1, §12.2), one per run and recipient.
        for r in recipients:
            dedup = f"cred_in_run:{run_id or event_id}:{r}"
            _insert_outbox_msg(conn, event_seq, r, "inbox", dedup, project_id, "security.credential_in_run",
                               payload_raw, now)

    # 11. lead.action_result with ok=false -> the current lead, as lead.action_failed (§12.4).
    # Q8: the message reaches the next turn but never triggers one by itself.
    elif etype == "lead.action_result" and project_id and payload.get("ok") is False:
        lead = _get_project_lead_info(conn, project_id)
        if lead:
            channel = "lead_wake" if lead["delivery"] == "launch" else "inbox"
            index = payload.get("index")
            subject = "block" if index is None else index
            dedup = f"lead.action_failed:{run_id or payload.get('run_id')}:{subject}"
            _insert_outbox_msg(conn, event_seq, lead["agent_id"], channel, dedup, project_id,
                               "lead.action_failed", payload_raw, now)


def verify_event_chain(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Verify that the events table forms an unbroken, untampered hash chain.

    Returns:
        List of violation dictionaries (empty if valid).
    """
    violations: List[Dict[str, Any]] = []
    cursor = conn.execute("SELECT * FROM events ORDER BY seq ASC;")
    rows = cursor.fetchall()

    prev_hash = "genesis"
    for idx, row in enumerate(rows):
        row_dict = dict(row)
        seq = row_dict["seq"]
        event_id = row_dict["id"]
        row_prev = row_dict["prev_hash"]
        row_hash = row_dict["hash"]

        if row_prev != prev_hash:
            violations.append({
                "invariant": "I7",
                "seq": seq,
                "event_id": event_id,
                "message": f"Event {event_id} (seq {seq}) prev_hash '{row_prev}' does not match expected '{prev_hash}'",
            })

        expected_hash = compute_event_hash(row_dict, row_prev)
        if row_hash != expected_hash:
            violations.append({
                "invariant": "I7",
                "seq": seq,
                "event_id": event_id,
                "message": f"Event {event_id} (seq {seq}) hash '{row_hash}' does not match recomputed hash '{expected_hash}'",
            })

        prev_hash = row_hash

    return violations


insert_event = record_event


def last_rejection(conn: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    """The latest `task reject` of a task: reason, who, when, attempt. None if never rejected.

    Shown to the assignee and the lead in `task show` and carried in the rework offer, so
    a worker picking a task up again knows what to change.
    """
    row = conn.execute(
        """SELECT e.ts, e.reason, e.payload_json, e.actor_id, e.actor_kind, e.via, p.name AS actor_name
           FROM events e LEFT JOIN principals p ON p.id = e.actor_id
           WHERE e.task_id = ? AND e.type = 'task.rejected'
           ORDER BY e.seq DESC LIMIT 1;""",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    by = row["actor_name"] or row["actor_id"]
    if row["actor_kind"] == "user":
        by = "user"
    return {
        "reason": row["reason"] or payload.get("reason") or "",
        "rejected_by": by,
        "via": row["via"],
        "rejected_at": row["ts"],
        "attempt": payload.get("attempt"),
    }
