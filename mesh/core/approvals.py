"""Approvals lifecycle, op hashing, MAC verification, and policies per plan §7.4."""

import hashlib
import hmac
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from mesh.core.canonical import canonical_json, compute_op_hash
from mesh.core.clock import now_iso_utc, now_utc
from mesh.core.events import record_event
from mesh.core.redact import SECRET_ENV_NAME_RE, redact, redact_value
from mesh.core.ids import generate_id
from mesh.errors import MeshError

EXCLUDED_OP_PARAMS = {"idempotency_key", "dry_run", "approval", "format", "if_version"}

DEFAULT_POLICIES = {
    "config": "direct_only",
    "lead_change": "direct_or_relayed",
    "project_control": "direct_or_relayed",
    "scope_change": "direct_or_relayed",
    "permission_escalation": "direct_only",
    "retry_over_limit": "direct_or_relayed",
    "delete": "direct_or_relayed",
    "deliver_to_base": "not_required",
    "spend_money": "direct_or_relayed",
    "deploy": "direct_or_relayed",
    "delete_external": "direct_or_relayed",
    "external_other": "direct_or_relayed",
}


def get_approval_policy(conn: sqlite3.Connection, category: str) -> str:
    """Retrieve effective approval policy for a category."""
    if category == "config":
        return "direct_only"
    key = f"approval.policy.{category}"
    cur = conn.execute("SELECT value_json FROM settings WHERE key = ?;", (key,))
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            return json.loads(row["value_json"])
        except Exception:
            pass
    return DEFAULT_POLICIES.get(category, "direct_or_relayed")


def get_row_approval_policy(
    conn: sqlite3.Connection,
    row: Any,
    extra_categories: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Effective policy across the approval row's category and any compound gate categories."""
    categories: List[str] = [row["category"]]
    if extra_categories:
        for cat in extra_categories:
            if cat and cat not in categories:
                categories.append(cat)
    op_type = row["op_type"] if "op_type" in row.keys() else None
    op_json = row["op_json"] if "op_json" in row.keys() else None
    if op_type in ("task.reassign", "task.retry", "task.reject") and op_json:
        try:
            stored = json.loads(op_json)
            if isinstance(stored, dict) and stored.get("_redacted") is True:
                stored = stored.get("op")
            gates = stored.get("params", {}).get("gates") if isinstance(stored, dict) else None
            if isinstance(gates, list):
                for cat in gates:
                    if isinstance(cat, str) and cat in DEFAULT_POLICIES and cat not in categories:
                        categories.append(cat)
        except Exception:
            pass
    for cat in categories:
        if get_approval_policy(conn, cat) == "direct_only":
            return cat, "direct_only"
    return row["category"], get_approval_policy(conn, row["category"])


def is_delegated_decision(conn: sqlite3.Connection, approval_id: str) -> bool:
    """Return True if the latest decision event on ``approval_id`` used ``via = 'delegated'``."""
    ev = conn.execute(
        "SELECT via FROM events WHERE type = 'approval.decided' AND approval_id = ? ORDER BY seq DESC LIMIT 1;",
        (approval_id,),
    ).fetchone()
    return bool(ev and ev["via"] == "delegated")


def validate_delegated_decision_at_consume(conn: sqlite3.Connection, row: Any) -> None:
    """Re-verify delegated decision authority and settings at consume time (§7.4)."""
    if row["decided_via"] != "relayed":
        return
    approval_id = row["id"]
    if not is_delegated_decision(conn, approval_id):
        return

    if get_approval_mode(conn) != "delegated":
        raise MeshError(
            code=c.CHANNEL_NOT_ALLOWED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Delegated approval cannot be consumed while approval.mode is 'passphrase'",
            details={"approval_id": approval_id, "setting": "approval.mode", "expected": "delegated"},
        )

    if row["relayed_by"] == row["requested_by"] and not get_delegated_self_approve(conn):
        raise MeshError(
            code=c.CHANNEL_NOT_ALLOWED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Self-approved delegated decision cannot be consumed while approval.delegated_self_approve is false",
            details={
                "approval_id": approval_id,
                "setting": "approval.delegated_self_approve",
                "requested_by": row["requested_by"],
                "relayed_by": row["relayed_by"],
            },
        )

    from mesh.core.authz import get_project_active_lead, is_project_lead

    project_id = row["project_id"]
    relayed_by = row["relayed_by"]
    if not project_id or not relayed_by or not is_project_lead(conn, project_id, relayed_by):
        raise MeshError(
            code=c.CHANNEL_NOT_ALLOWED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Delegated approval cannot be consumed because the deciding lead is no longer the current project lead",
            details={"approval_id": approval_id, "project_id": project_id, "decided_by": relayed_by},
        )

    lead = get_project_active_lead(conn, project_id)
    if not lead or lead.get("delivery") == "launch" or lead.get("disabled_at") is not None:
        raise MeshError(
            code=c.CHANNEL_NOT_ALLOWED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Delegated approval requires an active, enabled inbox project lead",
            details={"approval_id": approval_id, "project_id": project_id},
        )


APPROVAL_MODES = ("passphrase", "delegated")
#: Max length of the human's words recorded with a delegated decision (as --user-statement).
HUMAN_CONFIRMED_MAX = 4000


def get_approval_mode(conn: sqlite3.Connection) -> str:
    """Effective ``approval.mode``: ``passphrase`` (default) or ``delegated``."""
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'approval.mode';")
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            val = json.loads(row["value_json"])
        except Exception:
            val = None
        if val in APPROVAL_MODES:
            return val
    return "passphrase"


def get_delegated_self_approve(conn: sqlite3.Connection) -> bool:
    """Effective ``approval.delegated_self_approve`` (default true): in delegated mode, may
    the lead decide an approval it requested itself? Anything but a stored JSON ``false``
    keeps the default."""
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'approval.delegated_self_approve';")
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            val = json.loads(row["value_json"])
        except Exception:
            val = None
        if isinstance(val, bool):
            return val
    return True


def compute_user_approval_mac(k_user: bytes, approval_id: str, op_hash: str, decision: str, decided_at: str) -> str:
    """Compute HMAC-SHA256 user MAC for direct approval decisions per plan §7.4."""
    msg = f"mesh-approval-v1\n{approval_id}\n{op_hash}\n{decision}\n{decided_at}".encode("utf-8")
    return hmac.new(k_user, msg, hashlib.sha256).hexdigest()


_MAX_OP_REDACT_PASSES = 16


def _fail_closed_op_doc(op_doc: Any) -> Any:
    """Fail-closed fixed point when ``stored_op_json`` does not converge within hard cap."""
    if isinstance(op_doc, dict) and _OP_DOC_KEYS.issubset(op_doc.keys()):
        out: Dict[str, Any] = {
            "v": 1,
            "op": op_doc.get("op"),
            "requested_by": op_doc.get("requested_by"),
            "project": op_doc.get("project"),
            "target": "[REDACTED]" if op_doc.get("target") is not None else None,
            "params": {"_redacted": "[REDACTED]"},
        }
        raw_params = op_doc.get("params")
        if isinstance(raw_params, dict) and isinstance(raw_params.get("gates"), list):
            safe_gates = [
                g for g in raw_params["gates"]
                if isinstance(g, str) and g in DEFAULT_POLICIES
            ]
            if safe_gates:
                out["params"]["gates"] = safe_gates
        if "category" in op_doc:
            out["category"] = op_doc.get("category")
        return out
    if isinstance(op_doc, dict):
        return {"_redacted": "[REDACTED]"}
    if isinstance(op_doc, list):
        return ["[REDACTED]"]
    return "[REDACTED]"


def stored_op_json(op_doc: Any, op_hash: str) -> str:
    """The op_json column (PR #40 r1 P2): the operation itself, or, when any decoded
    value holds a recognized secret, ``{"_redacted": true, "op": <redacted>, "op_hash":
    ...}``. The hash still binds the full operation; only the stored copy is redacted."""
    red = redact_value(op_doc)
    if red == op_doc:
        return json.dumps(op_doc)
    # Full review r5 P2-5 / r6 P2-2: iterate to the fixed point verify_stored_op_json
    # checks with a hard cap; if still not converged, fail closed to a fixed-point value.
    for _ in range(_MAX_OP_REDACT_PASSES):
        norm = _strip_stored_labels(red)
        again = redact_value(norm, exact_values=())
        if again == norm:
            break
        red = again
    else:
        red = _fail_closed_op_doc(op_doc)
    return json.dumps({"_redacted": True, "op": red, "op_hash": op_hash})


_REDACTION_MARKER_RE = re.compile(r"\[REDACTED(?::[^\]]+)?\]")
_STORED_LABEL_RE = re.compile(r"\[REDACTED:(unbroken output|run_token:[A-Za-z0-9_-]+|[A-Za-z_][A-Za-z0-9_]*)\]")


def _strip_stored_labels(value: Any) -> Any:
    if isinstance(value, str):
        return _STORED_LABEL_RE.sub(
            lambda m: "[REDACTED]"
            if m.group(1) == "unbroken output" or m.group(1).startswith("run_token:") or SECRET_ENV_NAME_RE.search(m.group(1))
            else m.group(0),
            value,
        )
    if isinstance(value, dict):
        return {_strip_stored_labels(k): _strip_stored_labels(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_stored_labels(v) for v in value]
    return value
_OP_DOC_KEYS = {"v", "op", "requested_by", "project", "target", "params"}


def _has_redaction_marker(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_REDACTION_MARKER_RE.search(value))
    if isinstance(value, dict):
        return any(_has_redaction_marker(k) or _has_redaction_marker(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_redaction_marker(v) for v in value)
    return False


def _op_doc_matches_row(doc: Any, row: Any) -> bool:
    """The operation document has the compute_op_hash shape and describes this row."""
    if not isinstance(doc, dict):
        return False
    keys = set(_OP_DOC_KEYS)
    if row["category"] is not None:
        keys.add("category")
    return (set(doc) == keys and doc.get("v") == 1 and isinstance(doc.get("params"), dict)
            and doc.get("op") == row["op_type"] and doc.get("requested_by") == row["requested_by"]
            and doc.get("project") == row["project_id"] and doc.get("category") == row["category"])


def verify_stored_op_json(row: Any) -> Optional[str]:
    """R2 P1-2: the op_hash that ``row['op_json']`` vouches for, or None.

    A plain document must have the compute_op_hash shape, describe the row (op, requester,
    project, category) and is re-hashed. A ``_redacted`` envelope cannot be re-hashed, so it
    must be exactly what stored_op_json writes: the three keys, an inner document of the
    same shape that describes the row, is canonical (no floats), is a fixed point of
    redaction and really carries a redaction marker (a clean document is never wrapped);
    external.* operations never hold secrets, so they are never wrapped. Raises
    TypeError/ValueError on invalid JSON.
    """
    op_doc = json.loads(row["op_json"])
    if isinstance(op_doc, dict) and op_doc.get("_redacted") is True:
        inner = op_doc.get("op")
        op_hash = op_doc.get("op_hash")
        if (set(op_doc) != {"_redacted", "op", "op_hash"} or not isinstance(op_hash, str)
                or str(row["op_type"] or "").startswith("external.")
                or not _op_doc_matches_row(inner, row)
                # R3 P2-2 / R5: fixed rules on normalized stored markers; env-derived exact
                # values change across meshd restarts without invalidating stored rows.
                or redact_value(_strip_stored_labels(inner), exact_values=()) != _strip_stored_labels(inner)
                or not _has_redaction_marker(inner)):
            return None
        canonical_json(inner)
        return op_hash
    if not _op_doc_matches_row(op_doc, row):
        return None
    return "sha256:" + hashlib.sha256(canonical_json(op_doc)).hexdigest()


def requested_event_problem(conn: sqlite3.Connection, row: Any) -> Optional[str]:
    """R2 P1-3: the row must match the hash-chained approval.requested event written with
    it (create_or_get_pending_approval is the only writer). None when it does."""
    ev = conn.execute(
        "SELECT actor_id, project_id, task_id, payload_json FROM events "
        "WHERE type = 'approval.requested' AND approval_id = ? ORDER BY seq ASC LIMIT 1;",
        (row["id"],),
    ).fetchone()
    return requested_event_mismatch(ev, row)


def requested_event_mismatch(ev: Any, row: Any) -> Optional[str]:
    if ev is None:
        return "approval has no approval.requested event"
    try:
        payload = json.loads(ev["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict) or payload.get("op_hash") != row["op_hash"]:
        return "requested event op_hash mismatch"
    if (ev["actor_id"] != row["requested_by"] or ev["project_id"] != row["project_id"]
            or ev["task_id"] != row["task_id"] or payload.get("op") != row["op_type"]
            or payload.get("category") != row["category"]):
        return "approval row differs from approval.requested event"
    return None


def is_legacy_consumed_approval(
    row_status: str,
    ev_terminal: Optional[str],
    dec_ev: Optional[Dict[str, Any]],
    has_consumed_ev: bool,
) -> bool:
    """True for a pre-#53 consumed approval row (no approval.consumed event, no op_hash in approval.decided)."""
    if row_status != "consumed" or ev_terminal != "approved" or has_consumed_ev or dec_ev is None:
        return False
    try:
        payload = json.loads(dec_ev.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and "op_hash" not in payload


def verify_approval_event_history(events: List[Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Fold ordered approval.* events for one approval_id into (latest_decision_ev, terminal_state, error)."""
    dec_ev: Optional[Dict[str, Any]] = None
    terminal: Optional[str] = None
    for raw in events:
        ev = dict(raw)
        etype = ev["type"]
        if etype == "approval.decided":
            if terminal in ("cancelled", "expired", "consumed"):
                return dec_ev, terminal, f"approval.decided after {terminal}"
            try:
                payload = json.loads(ev["payload_json"] or "{}")
            except (TypeError, ValueError):
                return dec_ev, terminal, "invalid approval.decided payload"
            if not isinstance(payload, dict) or payload.get("decision") not in ("approved", "rejected"):
                return dec_ev, terminal, "invalid approval.decided decision"
            if dec_ev is not None:
                try:
                    prev_payload = json.loads(dec_ev["payload_json"] or "{}")
                except (TypeError, ValueError):
                    prev_payload = {}
                prev_chan = "relayed" if dec_ev["via"] == "delegated" else dec_ev["via"]
                if not (
                    isinstance(prev_payload, dict)
                    and prev_payload.get("decision") == "approved"
                    and prev_chan == "relayed"
                    and payload.get("decision") == "approved"
                    and ev["via"] == "direct"
                ):
                    return dec_ev, terminal, "conflicting multiple approval.decided events"
            dec_ev = ev
            terminal = payload["decision"]
        elif etype == "approval.cancelled":
            if terminal in ("rejected", "consumed", "cancelled", "expired"):
                return dec_ev, terminal, f"approval.cancelled after {terminal}"
            terminal = "cancelled"
        elif etype == "approval.expired":
            if terminal in ("rejected", "consumed", "cancelled", "expired"):
                return dec_ev, terminal, f"approval.expired after {terminal}"
            terminal = "expired"
        elif etype == "approval.consumed":
            if terminal != "approved":
                return dec_ev, terminal, f"approval.consumed from state {terminal or 'pending'}"
            terminal = "consumed"
    return dec_ev, terminal, None


def verify_row_for_consume(conn: sqlite3.Connection, row: Any) -> None:
    """R3 P1-1: before a consume trusts the row's category, policy and decision, the row must
    be the one the hash chain describes: its stored operation (verify_stored_op_json), its
    approval.requested event, and its approval.decided event (approved, same channel and
    decider, same op_hash when the event carries one)."""
    def bad(reason: str, message: str) -> MeshError:
        return MeshError(code=c.APPROVAL_INVALID, exit_code=c.EXIT_APPROVAL_INVALID,
                         message=f"Database corruption detected: {message}",
                         details={"approval_id": row["id"], "reason": reason})

    try:
        recomputed = verify_stored_op_json(row)
    except Exception:
        recomputed = None
    if recomputed != row["op_hash"]:
        raise bad("op_json_mismatch", "stored op_json does not match stored op_hash")
    req_problem = requested_event_problem(conn, row)
    if req_problem:
        raise bad("requested_event_mismatch", req_problem)
    ev_rows = conn.execute(
        "SELECT seq, type, actor_id, via, payload_json FROM events "
        "WHERE approval_id = ? AND type IN ('approval.decided', 'approval.cancelled', 'approval.expired', 'approval.consumed') "
        "ORDER BY seq ASC;",
        (row["id"],),
    ).fetchall()
    ev, terminal, hist_err = verify_approval_event_history(ev_rows)
    if hist_err or terminal != "approved":
        raise bad("decision_event_mismatch", hist_err or f"approval event state is '{terminal or 'pending'}', not 'approved'")
    try:
        payload = json.loads(ev["payload_json"] or "{}") if ev else None
    except (TypeError, ValueError):
        payload = None
    channel = None if ev is None else ("relayed" if ev["via"] == "delegated" else ev["via"])
    if (ev is None or not isinstance(payload, dict) or payload.get("decision") != "approved"
            or channel != row["decided_via"] or ev["actor_id"] != row["decided_by"]
            or payload.get("op_hash", row["op_hash"]) != row["op_hash"]):
        raise bad("decision_event_mismatch", "the decision does not match its approval.decided event")


def create_or_get_pending_approval(
    conn: sqlite3.Connection,
    category: str,
    op: str,
    requested_by: str,
    params: Dict[str, Any],
    summary: str,
    project_id: Optional[str] = None,
    target_id: Optional[str] = None,
    task_id: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    actor_kind: str = "agent",
    via: str = "token",
    request_id: Optional[str] = None,
    k_user: Optional[bytes] = None,
    testing_mode: Optional[bool] = None,
) -> Dict[str, Any]:
    """Create a pending approval row or return existing open approval with matching op_hash."""
    op_doc, op_hash = compute_op_hash(
        op=op,
        requested_by=requested_by,
        params=params,
        project_id=project_id,
        target_id=target_id,
        category=category,
    )
    summary = redact(summary)
    if dry_run:
        return {
            "approval_id": None,
            "op_hash": op_hash,
            "category": category,
            "summary": summary,
            "expires_at": None,
        }

    # Check for existing open approval (unique index on op_hash where status in pending, approved)
    cur = conn.execute(
        "SELECT id, op_hash, category, summary, expires_at, status FROM approvals WHERE op_hash = ? AND status IN ('pending', 'approved');",
        (op_hash,),
    )
    row = cur.fetchone()
    if row and row["expires_at"] < now_iso_utc():
        from mesh.core.workspaces import is_approval_pinned_by_ws_reservation

        if not is_approval_pinned_by_ws_reservation(conn, row["id"]):
            # Past its TTL but not swept yet: never hand out an expired approval for a new
            # request. Expire it here (same as the scheduler sweep) and create a fresh one.
            conn.execute("UPDATE approvals SET status = 'expired' WHERE id = ? AND status IN ('pending', 'approved');",
                         (row["id"],))
            old = conn.execute("SELECT project_id, task_id, op_type, requested_by FROM approvals WHERE id = ?;",
                               (row["id"],)).fetchone()
            record_event(
                conn=conn,
                type_="approval.expired",
                actor_id="system",
                actor_kind="system",
                via="system",
                project_id=old["project_id"],
                task_id=old["task_id"],
                approval_id=row["id"],
                payload={"approval_id": row["id"], "category": row["category"], "op": old["op_type"],
                         "requested_by": old["requested_by"], "expires_at": row["expires_at"],
                         "status": "expired", "expired_on": "new_request"},
            )
            row = None
    if row:
        return {
            "approval_id": row["id"],
            "op_hash": row["op_hash"],
            "category": row["category"],
            "summary": redact(row["summary"]),
            "expires_at": row["expires_at"],
            "status": row["status"],
        }

    # Fetch TTL setting if not passed
    if ttl_seconds is None:
        from mesh.core.settings import is_testing_mode

        cur_ttl = conn.execute("SELECT value_json FROM settings WHERE key = 'approval.ttl_seconds';")
        ttl_row = cur_ttl.fetchone()
        if ttl_row and ttl_row["value_json"]:
            try:
                ttl_seconds = int(json.loads(ttl_row["value_json"]))
            except Exception:
                ttl_seconds = 86400
        else:
            ttl_seconds = 86400
        if not is_testing_mode(testing_mode):
            ttl_seconds = max(60, ttl_seconds)

    # Blind review P2: the stored summary never keeps a recognized secret (events were
    # already redacted; the row and `approval show` were not).
    if isinstance(summary, str):
        summary = redact(summary)

    import time
    now_ts = time.time()
    requested_at = now_iso_utc(now_ts)
    expires_at = now_iso_utc(now_ts + ttl_seconds)
    apr_id = generate_id("apr")

    conn.execute(
        """INSERT INTO approvals (
            id, project_id, task_id, category, op_type, op_json, op_hash,
            summary, requested_by, requested_at, expires_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending');""",
        (
            apr_id,
            project_id,
            task_id,
            category,
            op,
            stored_op_json(op_doc, op_hash),
            op_hash,
            summary,
            requested_by,
            requested_at,
            expires_at,
        ),
    )

    record_event(
        conn=conn,
        type_="approval.requested",
        actor_id=requested_by,
        actor_kind=actor_kind,
        via=via,
        project_id=project_id,
        task_id=task_id,
        approval_id=apr_id,
        payload={
            "approval_id": apr_id,
            "category": category,
            "op": op,
            "summary": summary,
            "expires_at": expires_at,
            "op_hash": op_hash,
        },
        request_id=request_id,
        k_user=k_user,
    )

    return {
        "approval_id": apr_id,
        "op_hash": op_hash,
        "category": category,
        "summary": summary,
        "expires_at": expires_at,
        "status": "pending",
    }


def check_and_consume_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    op: str,
    category: str,
    caller_id: str,
    params: Dict[str, Any],
    project_id: Optional[str] = None,
    target_id: Optional[str] = None,
    request_id: Optional[str] = None,
    extra_categories: Optional[List[str]] = None,
    consume: bool = True,
    allow_expired_if_reserved: bool = False,
    actor_kind: str = "agent",
    via: str = "token",
    k_user: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Verify (and optionally consume) an approval when executing with --approval."""
    cur = conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Approval '{approval_id}' not found",
            details={"approval_id": approval_id},
        )

    # Recompute op_hash from incoming parameters using expected gate category and CALLER as requested_by (plan §7.4 step 4)
    _, computed_hash = compute_op_hash(
        op=op,
        requested_by=caller_id,
        params=params,
        project_id=project_id,
        target_id=target_id,
        category=category,
    )

    if computed_hash != row["op_hash"]:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Operation parameters do not match approved operation hash",
            details={"approval_id": approval_id, "expected_hash": row["op_hash"], "computed_hash": computed_hash},
        )

    pinned_reserved = False
    if allow_expired_if_reserved:
        from mesh.core.workspaces import is_approval_pinned_by_ws_reservation

        pinned_reserved = is_approval_pinned_by_ws_reservation(conn, approval_id)

    now = now_iso_utc()
    if row["status"] == "expired" or (not pinned_reserved and row["expires_at"] < now):
        raise MeshError(
            code=c.APPROVAL_EXPIRED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Approval has expired",
            details={"approval_id": approval_id, "expires_at": row["expires_at"]},
        )

    status = row["status"]
    if status == "pending":
        raise MeshError(
            code=c.APPROVAL_REQUIRED,
            exit_code=c.EXIT_APPROVAL_REQUIRED,
            message=f"Approval '{approval_id}' is still pending decision",
            details={"approval_id": approval_id, "op_hash": row["op_hash"]},
        )
    elif status == "consumed":
        raise MeshError(
            code=c.APPROVAL_CONSUMED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' has already been consumed",
            details={"approval_id": approval_id, "consumed_at": row["consumed_at"]},
        )
    elif status == "rejected":
        raise MeshError(
            code=c.APPROVAL_REJECTED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was rejected",
            details={"approval_id": approval_id, "decided_at": row["decided_at"]},
        )
    elif status == "cancelled":
        raise MeshError(
            code=c.APPROVAL_CANCELLED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was cancelled",
            details={"approval_id": approval_id},
        )
    elif status != "approved":
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' is in invalid state '{status}'",
            details={"approval_id": approval_id, "status": status},
        )

    if row["decided_via"] not in ("direct", "relayed"):
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' has invalid decided_via '{row['decided_via']}'",
            details={"approval_id": approval_id, "decided_via": row["decided_via"]},
        )

    if not row["decided_at"]:
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' has missing decided_at timestamp",
            details={"approval_id": approval_id},
        )

    if row["decided_via"] == "direct" and not row["user_mac"]:
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' direct decision is missing user_mac",
            details={"approval_id": approval_id},
        )

    verify_row_for_consume(conn, row)

    if not pinned_reserved:
        # Carbon review (public PR #2): a delegated decision cannot outlive its lead
        # assignment (A->B->A included) or a lead that lost can_lead.
        ensure_delegated_decider_current(conn, row)

        # Policy channel check against expected gate category (and any compound gates)
        blocking_cat, policy = get_row_approval_policy(
            conn, row, extra_categories=[category] + list(extra_categories or [])
        )
        if row["decided_via"] == "relayed" and policy == "direct_only":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message=f"Category '{blocking_cat}' requires direct approval; relayed decision disallowed",
                details={"category": blocking_cat, "policy": policy},
            )

        validate_delegated_decision_at_consume(conn, row)

    if not consume:
        return dict(row)

    # Consume
    conn.execute(
        """UPDATE approvals SET
            status = 'consumed',
            consumed_at = ?,
            consumed_by = ?,
            consumed_request_id = ?
        WHERE id = ?;""",
        (now, caller_id, request_id, approval_id),
    )
    record_consumed_event(conn, row, caller_id, actor_kind, via, request_id, k_user)
    return dict(row)


def record_consumed_event(conn, row, actor_id, actor_kind, via, request_id, k_user):
    """Record consumption in the same transaction as the approval update."""
    record_event(conn=conn, type_="approval.consumed", actor_id=actor_id,
                 actor_kind=actor_kind, via=via, project_id=row["project_id"],
                 task_id=row["task_id"], approval_id=row["id"],
                 payload={"approval_id": row["id"], "op_hash": row["op_hash"],
                          "category": row["category"], "decided_via": row["decided_via"]},
                 request_id=request_id, k_user=k_user)


def ensure_delegated_decider_current(conn, row):
    """A delegated decision cannot outlive its lead assignment, including A->B->A."""
    if row["decided_via"] != "relayed" or not row["project_id"]:
        return
    ev = conn.execute(
        "SELECT via FROM events WHERE type='approval.decided' AND approval_id=? ORDER BY seq DESC LIMIT 1",
        (row["id"],)).fetchone()
    if not ev or ev["via"] != "delegated":
        return
    from mesh.core.authz import get_project_active_lead
    lead = get_project_active_lead(conn, row["project_id"])
    if (not lead or not lead["can_lead"] or lead["delivery"] != "inbox"
            or lead["agent_id"] != row["relayed_by"]
            or not row["decided_at"] or lead["started_at"] > row["decided_at"]):
        raise MeshError(code=c.APPROVAL_INVALID, exit_code=c.EXIT_APPROVAL_INVALID,
                        message="Delegated approval no longer belongs to the current lead assignment",
                        details={"approval_id": row["id"], "reason": "delegated_decider_not_current_lead"})


def cancel_open_approvals(conn, rows, actor_id, actor_kind, via, reason,
                          request_id=None, relayed_by=None, k_user=None):
    """Withdraw unconsumed decisions atomically with one hash-chained event per row.

    Port note (private repo): an approval pinned by an in-progress workspace reservation is
    left alone, like ``approval cancel`` refuses it: the reserved operation already started
    under that authority and must be able to finish (or recover) consuming it.
    """
    from mesh.core.workspaces import is_approval_pinned_by_ws_reservation

    cancelled = []
    for row in rows:
        if is_approval_pinned_by_ws_reservation(conn, row["id"]):
            continue
        changed = conn.execute(
            "UPDATE approvals SET status='cancelled' WHERE id=? AND status IN ('pending','approved')"
            " AND consumed_at IS NULL",
            (row["id"],)).rowcount
        if not changed:
            continue
        cancelled.append(row["id"])
        record_event(conn=conn, type_="approval.cancelled", actor_id=actor_id,
                     actor_kind=actor_kind, via=via, relayed_by=relayed_by,
                     project_id=row["project_id"], approval_id=row["id"],
                     payload={"approval_id": row["id"], "reason": reason, "from_status": row["status"]},
                     request_id=request_id, k_user=k_user)
    return cancelled
