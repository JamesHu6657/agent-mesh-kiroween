"""Handler for audit resource commands: verify."""

import hashlib
import json

from mesh.core.canonical import canonical_json
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from mesh.errors import MeshError
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.core.events import compute_event_hash, compute_user_event_mac
from mesh.core.approvals import compute_user_approval_mac


def handle_audit_verify(ctx: HandlerContext) -> HandlerResult:
    """Verify event log hash chain continuity and direct user approval signatures per plan §7.4, §7.6."""
    # Plan §7.3 row 'audit verify': direct user only
    if ctx.actor_kind != "user" or ctx.via != "direct":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Operation 'audit.verify' requires direct user authentication",
            details={"required": "user_direct", "actor_kind": ctx.actor_kind, "via": ctx.via},
        )

    if not ctx.k_user:
        raise MeshError(
            code=c.PASSPHRASE_REQUIRED,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message="Audit verify requires direct user passphrase",
        )

    broken_event_seqs: List[int] = []
    broken_approval_ids: List[str] = []
    reasons: List[str] = []

    # 1. Verify event hash chain & direct user event signatures
    events_cur = ctx.conn.execute("SELECT * FROM events ORDER BY seq ASC;")
    events_rows = events_cur.fetchall()

    prev_hash = "genesis"
    events_verified = 0

    for row in events_rows:
        row_dict = dict(row)
        seq = row_dict["seq"]
        event_id = row_dict["id"]
        row_prev = row_dict["prev_hash"]
        row_hash = row_dict["hash"]
        user_mac = row_dict["user_mac"]

        # Check hash chain continuity
        if row_prev != prev_hash:
            if seq not in broken_event_seqs:
                broken_event_seqs.append(seq)
            reasons.append(f"Event {event_id} (seq {seq}) prev_hash '{row_prev}' does not match expected '{prev_hash}'")

        expected_hash = compute_event_hash(row_dict, row_prev)
        if row_hash != expected_hash:
            if seq not in broken_event_seqs:
                broken_event_seqs.append(seq)
            reasons.append(f"Event {event_id} (seq {seq}) hash '{row_hash}' does not match recomputed hash '{expected_hash}'")

        prev_hash = row_hash

        # Check direct user event MAC signature
        if row_dict.get("actor_kind") == "user" and row_dict.get("via") == "direct":
            if not user_mac:
                if seq not in broken_event_seqs:
                    broken_event_seqs.append(seq)
                reasons.append(f"Event {event_id} (seq {seq}) is missing user_mac signature for direct user actor")
            else:
                expected_mac = compute_user_event_mac(ctx.k_user, row_hash)
                if user_mac != expected_mac:
                    if seq not in broken_event_seqs:
                        broken_event_seqs.append(seq)
                    reasons.append(f"Event {event_id} (seq {seq}) user_mac signature mismatch")

        events_verified += 1

    # 2. Verify approvals: direct user decision signatures & relayed summaries
    apr_cur = ctx.conn.execute("SELECT * FROM approvals ORDER BY id ASC;")
    apr_rows = apr_cur.fetchall()

    approvals_verified = 0
    direct_decisions = 0
    relayed_decisions = 0
    relayed_counts: Dict[Tuple[str, str], int] = {}

    # Approval rows are mutable SQL data: cross-check them against the event chain.
    decisions = {}
    consumed = set()
    for event in events_rows:
        if event["type"] == "approval.decided":
            decisions[event["approval_id"]] = dict(event)
        elif event["type"] == "approval.consumed":
            consumed.add(event["approval_id"])

    for arow in apr_rows:
        adict = dict(arow)
        problems = []
        try:
            op_doc = json.loads(adict["op_json"])
            actual_hash = "sha256:" + hashlib.sha256(canonical_json(op_doc)).hexdigest()
            if actual_hash != adict["op_hash"]:
                problems.append("op_json hash mismatch")
        except (TypeError, ValueError):
            problems.append("invalid op_json")
        if adict["decided_via"] is not None or adict["status"] in ("approved", "rejected", "consumed"):
            ev = decisions.get(adict["id"])
            if not ev:
                problems.append("decision has no approval.decided event")
            else:
                payload = json.loads(ev["payload_json"])
                expected_decision = "rejected" if adict["status"] == "rejected" else "approved"
                expected_via = "relayed" if ev["via"] == "delegated" else ev["via"]
                if (payload.get("decision") != expected_decision or ev["actor_id"] != adict["decided_by"]
                        or expected_via != adict["decided_via"]):
                    problems.append("decision differs from approval.decided event")
                # Old events predate the op_hash payload; require equality when present.
                if payload.get("op_hash", adict["op_hash"]) != adict["op_hash"]:
                    problems.append("decision event op_hash mismatch")
        if adict["status"] == "consumed" and adict["id"] not in consumed:
            problems.append("consumption has no approval.consumed event")
        if problems:
            broken_approval_ids.append(adict["id"])
            reasons.extend(f"Approval {adict['id']}: {reason}" for reason in problems)

        aid = adict["id"]
        status = adict["status"]
        decided_via = adict["decided_via"]
        decided_at = adict["decided_at"]
        decided_by = adict["decided_by"]
        op_hash = adict["op_hash"]
        user_mac = adict["user_mac"]

        is_decision = (
            status in ("approved", "rejected", "consumed")
            or decided_via is not None
            or decided_at is not None
        )

        if is_decision:
            if not decided_via or decided_via not in ("direct", "relayed"):
                if aid not in broken_approval_ids:
                    broken_approval_ids.append(aid)
                reasons.append(f"Approval {aid} status '{status}' (or has decided_at) has invalid decided_via '{decided_via}'")

            if not decided_at:
                if aid not in broken_approval_ids:
                    broken_approval_ids.append(aid)
                reasons.append(f"Approval {aid} decision row has missing decided_at")

            if not decided_by:
                if aid not in broken_approval_ids:
                    broken_approval_ids.append(aid)
                reasons.append(f"Approval {aid} decision row has missing decided_by")

            if decided_via == "direct":
                direct_decisions += 1
                if not decided_at or not user_mac:
                    if aid not in broken_approval_ids:
                        broken_approval_ids.append(aid)
                    reasons.append(f"Approval {aid} direct decision missing decided_at or user_mac signature")
                else:
                    if status in ("consumed", "expired", "cancelled"):
                        decision = "approved"
                    else:
                        decision = status
                    expected_mac = compute_user_approval_mac(ctx.k_user, aid, op_hash, decision, decided_at)
                    if user_mac != expected_mac:
                        if aid not in broken_approval_ids:
                            broken_approval_ids.append(aid)
                        reasons.append(f"Approval {aid} direct user_mac signature mismatch")
            elif decided_via == "relayed":
                relayed_decisions += 1
                r_by = adict["relayed_by"] or "unknown"
                c_id = adict["relayer_credential_id"] or "unknown"
                key = (r_by, c_id)
                relayed_counts[key] = relayed_counts.get(key, 0) + 1

        approvals_verified += 1

    relayed_summary = [
        {"relayed_by": r_by, "credential_id": c_id, "count": count}
        for (r_by, c_id), count in sorted(relayed_counts.items())
    ]

    # 3. Mismatch evaluation
    if broken_event_seqs or broken_approval_ids:
        raise MeshError(
            code=c.AUDIT_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,  # 12
            message=f"Audit verification failed: {len(broken_event_seqs)} broken event(s), {len(broken_approval_ids)} broken approval(s)",
            details={
                "broken_event_seqs": broken_event_seqs,
                "broken_approval_ids": broken_approval_ids,
                "reasons": reasons,
                "events_verified": events_verified,
                "approvals_verified": approvals_verified,
                "relayed_summary": relayed_summary,
            },
        )

    return HandlerResult(
        data={
            "events_verified": events_verified,
            "approvals_verified": approvals_verified,
            "direct_decisions": direct_decisions,
            "relayed_decisions": relayed_decisions,
            "relayed_summary": relayed_summary,
        }
    )
