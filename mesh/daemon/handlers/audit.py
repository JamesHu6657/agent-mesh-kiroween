"""Handler for audit resource commands: verify."""

import json
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

import mesh.constants as c
from mesh.core.approvals import (
    DEFAULT_POLICIES,
    compute_user_approval_mac,
    is_delegated_decision,
    is_legacy_consumed_approval,
    requested_event_mismatch,
    verify_approval_event_history,
    verify_stored_op_json,
)
from mesh.core.canonical import canonical_json
from mesh.core.events import compute_user_event_mac, verify_event_chain
from mesh.core.invariants import check_i4_status_events
from mesh.core.paths import resolve_home
from mesh.core.redact import redact, redact_value
from mesh.core.settings import get_default_settings, is_testing_mode
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.errors import MeshError


def _parse_json_dict(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _has_redaction_marker(value: Any) -> bool:
    if isinstance(value, str):
        return "[REDACTED" in value
    if isinstance(value, dict):
        return any(_has_redaction_marker(k) or _has_redaction_marker(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_redaction_marker(v) for v in value)
    return False


def _json_values_equal(a: Any, b: Any) -> bool:
    try:
        return canonical_json(a) == canonical_json(b)
    except ValueError:
        return type(a) is type(b) and a == b


def _effective_approval_policy(adict: Dict[str, Any], effective_settings: Dict[str, Any]) -> Tuple[str, str]:
    """Effective policy at a historical seq across row category and compound gates."""
    def _policy_for_cat(cat: str) -> str:
        if cat == "config":
            return "direct_only"
        val = effective_settings.get(f"approval.policy.{cat}")
        if isinstance(val, str) and val in ("direct_only", "direct_or_relayed", "not_required"):
            return val
        return DEFAULT_POLICIES.get(cat, "direct_or_relayed")

    categories: List[str] = [adict["category"]]
    op_type = adict.get("op_type")
    op_json = adict.get("op_json")
    if op_type in ("task.reassign", "task.retry", "task.reject") and op_json:
        stored = _parse_json_dict(op_json)
        if stored is not None:
            if stored.get("_redacted") is True:
                stored = stored.get("op") if isinstance(stored.get("op"), dict) else None
            gates = stored.get("params", {}).get("gates") if isinstance(stored, dict) and isinstance(stored.get("params"), dict) else None
            if isinstance(gates, list):
                for cat in gates:
                    if isinstance(cat, str) and cat in DEFAULT_POLICIES and cat not in categories:
                        categories.append(cat)

    for cat in categories:
        if _policy_for_cat(cat) == "direct_only":
            return cat, "direct_only"
    return adict["category"], _policy_for_cat(adict["category"])


def _init_baseline(
    conn: sqlite3.Connection,
    events_rows: List[sqlite3.Row],
    ctx: HandlerContext,
) -> Tuple[bool, bool, str, bool, Dict[str, Any]]:
    has_init_markers = conn.execute(
        "SELECT 1 FROM principals WHERE id = 'principal_system' "
        "UNION ALL SELECT 1 FROM credentials WHERE kind = 'passphrase' LIMIT 1;"
    ).fetchone() is not None
    has_system_init = bool(events_rows and events_rows[0]["type"] == "system.init")
    is_initialized_home = has_init_markers or has_system_init

    init_home_redacted = False
    init_home = ""
    if has_system_init:
        sys_payload = _parse_json_dict(events_rows[0]["payload_json"])
        raw_home = sys_payload.get("home") if sys_payload else None
        if isinstance(raw_home, str) and raw_home:
            if "[REDACTED" in raw_home:
                init_home_redacted = True
            else:
                init_home = raw_home
    if not init_home and ctx.home:
        init_home = resolve_home(ctx.home)

    pass_row = conn.execute("SELECT kdf_params FROM credentials WHERE kind = 'passphrase' LIMIT 1;").fetchone()
    kdf_dict = _parse_json_dict(pass_row["kdf_params"]) if pass_row else None
    init_testing = bool(kdf_dict.get("testing", False)) if kdf_dict is not None else is_testing_mode(ctx.testing_mode)
    default_settings = get_default_settings(init_home or "/nonexistent", testing_mode=init_testing)
    return is_initialized_home, has_system_init, init_home_redacted, bool(init_home), default_settings


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
    state_mismatches: List[str] = []
    reasons: List[str] = []

    def _flag_event(seq: Optional[int], reason: str) -> None:
        if seq is not None and seq not in broken_event_seqs:
            broken_event_seqs.append(seq)
        reasons.append(reason)

    def _flag_approval(aid: str, reason: str, seq: Optional[int] = None) -> None:
        if aid not in broken_approval_ids:
            broken_approval_ids.append(aid)
        if seq is not None and seq not in broken_event_seqs:
            broken_event_seqs.append(seq)
        reasons.append(reason)

    def _flag_state(tag: str, reason: str) -> None:
        if tag not in state_mismatches:
            state_mismatches.append(tag)
        reasons.append(reason)

    # 1. Verify event hash chain, sequence contiguity, direct-user event rules & MAC signatures
    events_cur = ctx.conn.execute("SELECT * FROM events ORDER BY seq ASC;")
    events_rows = events_cur.fetchall()

    for violation in verify_event_chain(ctx.conn):
        _flag_event(violation.get("seq"), violation["message"])

    is_initialized_home, has_system_init, init_home_redacted, has_init_home, default_settings = _init_baseline(
        ctx.conn, events_rows, ctx
    )

    apr_rows = ctx.conn.execute("SELECT * FROM approvals ORDER BY id ASC;").fetchall()
    apr_by_id: Dict[str, Dict[str, Any]] = {r["id"]: dict(r) for r in apr_rows}
    principals_rows = ctx.conn.execute("SELECT id, kind, name, disabled_at FROM principals ORDER BY id ASC;").fetchall()
    agent_id_by_name: Dict[str, str] = {r["name"]: r["id"] for r in principals_rows if r["kind"] == "agent"}

    effective_settings: Dict[str, Any] = dict(default_settings) if is_initialized_home else {}
    config_updated_keys: Set[str] = set()
    redacted_config_keys: Set[str] = set()
    created_agents: Set[str] = set()
    agent_version_at_seq: Dict[str, int] = {}
    agent_disabled_at_seq: Dict[str, bool] = {}
    issued_agent_tokens: Dict[str, Dict[str, Any]] = {}
    revoked_agent_tokens: Dict[str, bool] = {}
    created_projects: Set[str] = set()
    project_lead_at_seq: Dict[str, str] = {}
    created_tasks: Set[str] = set()
    started_runs: Set[str] = set()
    latest_decided_by_apr: Dict[str, Dict[str, Any]] = {}

    by_approval_events: Dict[str, List[Dict[str, Any]]] = {}
    consumed: Dict[str, List[Dict[str, Any]]] = {}
    requested: Dict[str, Dict[str, Any]] = {}

    events_verified = 0
    for row in events_rows:
        row_dict = dict(row)
        seq = row_dict["seq"]
        event_id = row_dict["id"]
        row_hash = row_dict["hash"]
        user_mac = row_dict["user_mac"]
        etype = row_dict["type"]

        if row_dict.get("via") == "direct" and row_dict.get("actor_kind") != "user":
            _flag_event(
                seq,
                f"Event {event_id} (seq {seq}) has via 'direct' with non-user actor_kind '{row_dict.get('actor_kind')}'",
            )

        # Check direct user event MAC signature
        if row_dict.get("actor_kind") == "user" and row_dict.get("via") == "direct":
            if not user_mac:
                _flag_event(seq, f"Event {event_id} (seq {seq}) is missing user_mac signature for direct user actor")
            else:
                expected_mac = compute_user_event_mac(ctx.k_user, row_hash)
                if user_mac != expected_mac:
                    _flag_event(seq, f"Event {event_id} (seq {seq}) user_mac signature mismatch")

        payload_obj = _parse_json_dict(row_dict.get("payload_json"))
        if etype == "config.updated":
            if payload_obj is None or "key" not in payload_obj or "value" not in payload_obj:
                _flag_event(seq, f"Event {event_id} (seq {seq}) has invalid config.updated payload")
            else:
                cfg_key = payload_obj["key"]
                cfg_val = payload_obj["value"]
                if not isinstance(cfg_key, str) or cfg_key not in default_settings:
                    _flag_event(seq, f"Event {event_id} (seq {seq}) config.updated references unknown key '{cfg_key}'")
                else:
                    effective_settings[cfg_key] = cfg_val
                    config_updated_keys.add(cfg_key)
                    if _has_redaction_marker(cfg_val):
                        redacted_config_keys.add(cfg_key)
                    else:
                        redacted_config_keys.discard(cfg_key)
        elif etype == "agent.created" and payload_obj:
            agt_id = payload_obj.get("agent_id")
            if isinstance(agt_id, str) and agt_id:
                created_agents.add(agt_id)
                agent_version_at_seq[agt_id] = 1
                agent_disabled_at_seq[agt_id] = False
        elif etype == "agent.updated" and payload_obj:
            agt_id = payload_obj.get("agent_id")
            ver = payload_obj.get("version")
            if isinstance(agt_id, str) and agt_id and isinstance(ver, int) and not isinstance(ver, bool):
                agent_version_at_seq[agt_id] = ver
        elif etype == "agent.disabled" and payload_obj:
            agt_id = payload_obj.get("agent_id")
            if isinstance(agt_id, str) and agt_id:
                agent_disabled_at_seq[agt_id] = True
        elif etype == "agent.enabled" and payload_obj:
            agt_id = payload_obj.get("agent_id")
            if isinstance(agt_id, str) and agt_id:
                agent_disabled_at_seq[agt_id] = False
        elif etype == "agent.token_issued" and row_dict.get("credential_id"):
            cid = row_dict["credential_id"]
            issued_agent_tokens[cid] = row_dict
            revoked_agent_tokens[cid] = False
        elif etype == "agent.token_revoked" and row_dict.get("credential_id"):
            revoked_agent_tokens[row_dict["credential_id"]] = True
        elif etype == "project.created" and row_dict.get("project_id"):
            pid = row_dict["project_id"]
            created_projects.add(pid)
            if payload_obj and isinstance(payload_obj.get("lead"), str):
                lid = agent_id_by_name.get(payload_obj["lead"])
                if lid:
                    project_lead_at_seq[pid] = lid
        elif etype == "project.lead_changed" and row_dict.get("project_id"):
            pid = row_dict["project_id"]
            if payload_obj and isinstance(payload_obj.get("new_lead"), str):
                lid = agent_id_by_name.get(payload_obj["new_lead"])
                if lid:
                    project_lead_at_seq[pid] = lid
        elif etype in ("task.created", "task.assigned") and row_dict.get("task_id") and row_dict.get("from_state") is None:
            created_tasks.add(row_dict["task_id"])
        elif etype in ("task.started", "lead.turn_started", "task.merging") and row_dict.get("run_id"):
            started_runs.add(row_dict["run_id"])

        aid_ev = row_dict.get("approval_id")
        if aid_ev:
            if etype == "approval.requested":
                requested.setdefault(aid_ev, row_dict)
            elif etype in ("approval.decided", "approval.cancelled", "approval.expired", "approval.consumed"):
                by_approval_events.setdefault(aid_ev, []).append(row_dict)
                if etype == "approval.consumed":
                    consumed.setdefault(aid_ev, []).append(row_dict)

            if etype == "approval.decided":
                latest_decided_by_apr[aid_ev] = row_dict
                adict_ev = apr_by_id.get(aid_ev)
                ev_via = row_dict.get("via")
                ev_actor = row_dict.get("actor_id")
                if ev_via not in ("direct", "relayed", "delegated"):
                    _flag_approval(aid_ev, f"Approval {aid_ev}: invalid approval.decided via '{ev_via}' at seq {seq}", seq)
                elif ev_via in ("relayed", "delegated") and row_dict.get("actor_kind") != "agent":
                    _flag_approval(
                        aid_ev,
                        f"Approval {aid_ev}: {ev_via} decision at seq {seq} has non-agent actor_kind '{row_dict.get('actor_kind')}'",
                        seq,
                    )
                elif adict_ev is not None and ev_via in ("relayed", "delegated"):
                    blocking_cat, eff_policy = _effective_approval_policy(adict_ev, effective_settings)
                    if eff_policy == "direct_only":
                        _flag_approval(
                            aid_ev,
                            f"Approval {aid_ev}: {ev_via} decision at seq {seq} violates direct_only policy for '{blocking_cat}'",
                            seq,
                        )
                    if ev_via == "delegated":
                        eff_mode = effective_settings.get("approval.mode", "passphrase")
                        if eff_mode != "delegated":
                            _flag_approval(
                                aid_ev,
                                f"Approval {aid_ev}: delegated decision at seq {seq} while approval.mode was '{eff_mode}'",
                                seq,
                            )
                        if not adict_ev.get("project_id"):
                            _flag_approval(
                                aid_ev,
                                f"Approval {aid_ev}: delegated decision at seq {seq} without project_id",
                                seq,
                            )
                        else:
                            self_app = effective_settings.get("approval.delegated_self_approve", True)
                            if ev_actor == adict_ev.get("requested_by") and not self_app:
                                _flag_approval(
                                    aid_ev,
                                    f"Approval {aid_ev}: delegated self-approval at seq {seq} violates approval.delegated_self_approve=false",
                                    seq,
                                )
                            seq_lead = project_lead_at_seq.get(adict_ev["project_id"])
                            if seq_lead is not None and ev_actor != seq_lead:
                                _flag_approval(
                                    aid_ev,
                                    f"Approval {aid_ev}: delegated decider '{ev_actor}' at seq {seq} was not project lead '{seq_lead}'",
                                    seq,
                                )
                    else:  # relayed
                        if ev_actor == adict_ev.get("requested_by"):
                            _flag_approval(
                                aid_ev,
                                f"Approval {aid_ev}: relayed decision at seq {seq} by requester violates SELF_APPROVAL",
                                seq,
                            )
                        pid_ev = adict_ev.get("project_id")
                        seq_lead = project_lead_at_seq.get(pid_ev) if pid_ev else None
                        if seq_lead is not None and ev_actor == seq_lead:
                            _flag_approval(
                                aid_ev,
                                f"Approval {aid_ev}: relayed decision at seq {seq} by project lead violates SELF_APPROVAL",
                                seq,
                            )
            elif etype == "approval.consumed":
                adict_ev = apr_by_id.get(aid_ev)
                dec_before = latest_decided_by_apr.get(aid_ev)
                if adict_ev is not None and dec_before is not None:
                    dvia = dec_before.get("via")
                    dactor = dec_before.get("actor_id")
                    if dvia in ("relayed", "delegated") and adict_ev.get("op_type") not in (
                        "workspace.delete",
                        "workspace.prune",
                        "project.deliver",
                    ):
                        blocking_cat, eff_policy = _effective_approval_policy(adict_ev, effective_settings)
                        if eff_policy == "direct_only":
                            _flag_approval(
                                aid_ev,
                                f"Approval {aid_ev}: consumed at seq {seq} under direct_only policy for '{blocking_cat}'",
                                seq,
                            )
                        if dvia == "delegated":
                            eff_mode = effective_settings.get("approval.mode", "passphrase")
                            if eff_mode != "delegated":
                                _flag_approval(
                                    aid_ev,
                                    f"Approval {aid_ev}: delegated approval consumed at seq {seq} while approval.mode was '{eff_mode}'",
                                    seq,
                                )
                            self_app = effective_settings.get("approval.delegated_self_approve", True)
                            if dactor == adict_ev.get("requested_by") and not self_app:
                                _flag_approval(
                                    aid_ev,
                                    f"Approval {aid_ev}: self-approved delegated approval consumed at seq {seq} while delegated_self_approve is false",
                                    seq,
                                )
                            pid_ev = adict_ev.get("project_id")
                            seq_lead = project_lead_at_seq.get(pid_ev) if pid_ev else None
                            if seq_lead is not None and dactor != seq_lead:
                                _flag_approval(
                                    aid_ev,
                                    f"Approval {aid_ev}: delegated approval consumed at seq {seq} after lead changed",
                                    seq,
                                )

        events_verified += 1

    # 2. Verify approvals: direct user decision signatures & relayed summaries
    approvals_verified = 0
    direct_decisions = 0
    relayed_decisions = 0
    delegated_decisions = 0
    relayed_counts: Dict[Tuple[str, str], int] = {}

    for arow in apr_rows:
        adict = dict(arow)
        problems: List[str] = []
        try:
            # R2 P1-2: one check shared with the decision path (approval.py).
            if verify_stored_op_json(adict) != adict["op_hash"]:
                problems.append("op_json hash mismatch")
        except (TypeError, ValueError):
            problems.append("invalid op_json")
        # R2 P1-3: every row is anchored by its hash-chained approval.requested event.
        req_ev = requested.get(adict["id"])
        req_problem = requested_event_mismatch(req_ev, adict)
        if req_problem:
            problems.append(req_problem)
        elif req_ev is not None:
            req_payload = _parse_json_dict(req_ev.get("payload_json"))
            if req_payload is not None:
                if "summary" in req_payload and adict["summary"] != req_payload["summary"] and redact(adict["summary"] or "") != req_payload["summary"]:
                    problems.append("summary differs from approval.requested event")
                if "expires_at" in req_payload and adict["expires_at"] != req_payload["expires_at"]:
                    problems.append("expires_at differs from approval.requested event")
        dec_ev, terminal, hist_err = verify_approval_event_history(by_approval_events.get(adict["id"], []))
        if hist_err:
            problems.append(hist_err)
        cons_evs = consumed.get(adict["id"], [])
        legacy_consumed = is_legacy_consumed_approval(adict["status"], terminal, dec_ev, bool(cons_evs))
        expected_status = "consumed" if legacy_consumed else (terminal or "pending")
        if adict["status"] != expected_status:
            problems.append(f"approval status '{adict['status']}' differs from event chain state '{expected_status}'")
        if dec_ev is not None or adict["decided_via"] is not None or adict["status"] in ("approved", "rejected", "consumed"):
            ev = dec_ev
            if not ev:
                problems.append("decision has no approval.decided event")
            else:
                payload = _parse_json_dict(ev.get("payload_json"))
                if payload is None:
                    problems.append("invalid approval.decided payload")
                else:
                    expected_decision = "rejected" if adict["status"] == "rejected" else "approved"
                    expected_via = "relayed" if ev["via"] == "delegated" else ev["via"]
                    if (payload.get("decision") != expected_decision or ev["actor_id"] != adict["decided_by"]
                            or expected_via != adict["decided_via"]):
                        problems.append("decision differs from approval.decided event")
                    # Old events predate the op_hash payload; require equality when present.
                    if payload.get("op_hash", adict["op_hash"]) != adict["op_hash"]:
                        problems.append("decision event op_hash mismatch")
                    if adict["decided_via"] is not None:
                        if ev["via"] == "direct":
                            if (adict["relayed_by"] is not None or adict["relayer_credential_id"] is not None
                                    or adict["user_statement"] is not None or adict["evidence_ref"] is not None):
                                problems.append("direct decision has non-null relayed metadata")
                            if adict["decision_note"] != payload.get("note"):
                                problems.append("decision_note differs from approval.decided event")
                        elif ev["via"] in ("relayed", "delegated"):
                            if adict["user_mac"] is not None:
                                problems.append("relayed/delegated decision has non-null user_mac")
                            if adict["relayed_by"] != ev["actor_id"]:
                                problems.append("relayed_by differs from approval.decided event")
                            if adict["relayer_credential_id"] != ev["credential_id"]:
                                problems.append("relayer_credential_id differs from approval.decided event")
                            if adict["decision_note"] != payload.get("note"):
                                problems.append("decision_note differs from approval.decided event")
                            if "user_statement" in payload and adict["user_statement"] != payload.get("user_statement"):
                                problems.append("user_statement differs from approval.decided event")
                            if "evidence_ref" in payload and adict["evidence_ref"] != payload.get("evidence_ref"):
                                problems.append("evidence_ref differs from approval.decided event")
                            if ev["via"] == "delegated" and (
                                payload.get("mode") != "delegated"
                                or payload.get("human_confirmed") != adict["user_statement"]
                            ):
                                problems.append("delegated metadata differs from approval.decided event")
        # Port note (private repo): `approval.consumed` events exist only since this change.
        # A decision event carrying `op_hash` was written by the same version, so only those
        # rows must have one; older consumed rows (existing installs) stay valid.
        dec_new = False
        if dec_ev:
            try:
                dec_new = "op_hash" in json.loads(dec_ev["payload_json"] or "{}")
            except (TypeError, ValueError):
                dec_new = False
        if adict["status"] == "consumed" and not cons_evs and dec_new:
            problems.append("consumption has no approval.consumed event")
        if adict["status"] == "consumed" and (not adict["consumed_at"] or not adict["consumed_by"]):
            problems.append("consumed approval missing consumed_at or consumed_by")
        if cons_evs and adict["status"] != "consumed":
            problems.append("approval.consumed event exists for a row that is not consumed")
        # R3 P1-1: the consumption event records what was consumed; the row must agree.
        if len(cons_evs) > 1:
            problems.append("multiple approval.consumed events for one approval")
        for cev in cons_evs:
            cpayload = _parse_json_dict(cev.get("payload_json"))
            row_req_id = adict["consumed_request_id"]
            red_req_id = redact(row_req_id) if isinstance(row_req_id, str) and row_req_id else row_req_id
            if (cpayload is None
                    or cpayload.get("op_hash") != adict["op_hash"]
                    or cpayload.get("category") != adict["category"]
                    or cpayload.get("decided_via") != adict["decided_via"]
                    or cev["project_id"] != adict["project_id"]
                    or cev["task_id"] != adict["task_id"]
                    or red_req_id != cev["request_id"]
                    or (cev["actor_kind"] != "system" and cev["actor_id"] != adict["consumed_by"])):
                problems.append("consumption differs from approval.consumed event")
        if problems:
            for reason in problems:
                _flag_approval(adict["id"], f"Approval {adict['id']}: {reason}")

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
                _flag_approval(aid, f"Approval {aid} status '{status}' (or has decided_at) has invalid decided_via '{decided_via}'")

            if not decided_at:
                _flag_approval(aid, f"Approval {aid} decision row has missing decided_at")

            if not decided_by:
                _flag_approval(aid, f"Approval {aid} decision row has missing decided_by")

            if decided_via == "direct":
                direct_decisions += 1
                if not decided_at or not user_mac:
                    _flag_approval(aid, f"Approval {aid} direct decision missing decided_at or user_mac signature")
                else:
                    if status in ("consumed", "expired", "cancelled"):
                        decision = "approved"
                    else:
                        decision = status
                    expected_mac = compute_user_approval_mac(ctx.k_user, aid, op_hash, decision, decided_at)
                    if user_mac != expected_mac:
                        _flag_approval(aid, f"Approval {aid} direct user_mac signature mismatch")
            elif decided_via == "relayed":
                relayed_decisions += 1
                if is_delegated_decision(ctx.conn, aid):
                    delegated_decisions += 1
                r_by = adict["relayed_by"] or "unknown"
                c_id = adict["relayer_credential_id"] or "unknown"
                key = (r_by, c_id)
                relayed_counts[key] = relayed_counts.get(key, 0) + 1

        approvals_verified += 1

    seen_apr_ids = {r["id"] for r in apr_rows}
    for maid in sorted((set(requested.keys()) | set(by_approval_events.keys())) - seen_apr_ids):
        _flag_approval(maid, f"Approval {maid}: referenced in events but missing from approvals table")

    # 2b. Cross-check settings table against config.updated stream (and init defaults)
    settings_rows = ctx.conn.execute("SELECT key, value_json FROM settings ORDER BY key ASC;").fetchall()
    db_settings: Dict[str, Any] = {}
    for srow in settings_rows:
        skey = srow["key"]
        try:
            db_settings[skey] = json.loads(srow["value_json"])
        except (TypeError, ValueError):
            _flag_state(f"settings:{skey}", f"Setting '{skey}' has invalid value_json")

    def _check_setting_value(skey: str, exp_val: Any) -> None:
        if skey not in db_settings:
            _flag_state(f"settings:{skey}", f"Setting '{skey}' is missing from settings table")
            return
        db_val = db_settings[skey]
        if skey in redacted_config_keys:
            if redact_value(db_val) != exp_val:
                _flag_state(
                    f"settings:{skey}",
                    f"Setting '{skey}' redacted value differs from config.updated event history",
                )
        elif not _json_values_equal(db_val, exp_val):
            _flag_state(
                f"settings:{skey}",
                f"Setting '{skey}' value ({db_val!r}) differs from event history ({exp_val!r})",
            )

    if is_initialized_home:
        for skey, exp_val in effective_settings.items():
            if skey == "workspaces.root" and skey not in config_updated_keys and init_home_redacted and not has_init_home:
                continue
            _check_setting_value(skey, exp_val)
        agent_ids_db = {r["id"] for r in ctx.conn.execute("SELECT id FROM agents;").fetchall()}
        for skey, db_val in db_settings.items():
            if skey in effective_settings:
                continue
            if skey.startswith("agent.") and skey.endswith(".inbox.ack_timeout_s"):
                agt_id = skey[len("agent."):-len(".inbox.ack_timeout_s")]
                if agt_id not in agent_ids_db or agt_id not in created_agents:
                    _flag_state(f"settings:{skey}", f"Per-agent ack timeout '{skey}' references unknown agent '{agt_id}'")
                elif isinstance(db_val, bool) or not isinstance(db_val, int) or not (1 <= db_val <= 86400):
                    _flag_state(f"settings:{skey}", f"Per-agent ack timeout '{skey}' has invalid value {db_val!r}")
            else:
                _flag_state(f"settings:{skey}", f"Unexpected setting '{skey}' in settings table with no config.updated event")
    else:
        for skey in sorted(config_updated_keys):
            _check_setting_value(skey, effective_settings[skey])

    # 2c. For initialized homes, verify surviving entities & I4 state events match the event chain
    if is_initialized_home:
        agent_rows = ctx.conn.execute("SELECT id, version FROM agents ORDER BY id ASC;").fetchall()
        agent_ids_set = {r["id"] for r in agent_rows}
        principals_by_id = {r["id"]: dict(r) for r in principals_rows}

        for arow in agent_rows:
            agid = arow["id"]
            if agid not in created_agents:
                _flag_state(f"agent:{agid}", f"Agent '{agid}' has no agent.created event")
                continue
            exp_ver = agent_version_at_seq.get(agid, 1)
            if arow["version"] != exp_ver:
                _flag_state(f"agent:{agid}", f"Agent '{agid}' version {arow['version']} differs from event history version {exp_ver}")
            prow = principals_by_id.get(agid)
            if prow is None or prow["kind"] != "agent":
                _flag_state(f"agent:{agid}", f"Agent '{agid}' missing agent principal row")
            else:
                is_dis = prow["disabled_at"] is not None
                exp_dis = agent_disabled_at_seq.get(agid, False)
                if is_dis != exp_dis:
                    _flag_state(
                        f"agent:{agid}",
                        f"Agent '{agid}' disabled state ({is_dis}) differs from event history ({exp_dis})",
                    )

        for pdict in principals_by_id.values():
            pid = pdict["id"]
            if pdict["kind"] == "agent" and (pid not in agent_ids_set or pid not in created_agents):
                _flag_state(f"principal:{pid}", f"Agent principal '{pid}' has no matching agent.created event")
            elif pdict["kind"] == "user" and not has_system_init:
                _flag_state(f"principal:{pid}", f"User principal '{pid}' has no system.init event")

        cred_rows = ctx.conn.execute(
            "SELECT id, principal_id, kind, lookup, scope_json, revoked_at FROM credentials ORDER BY id ASC;"
        ).fetchall()
        for crow in cred_rows:
            cid = crow["id"]
            ckind = crow["kind"]
            if ckind == "passphrase":
                if not has_system_init:
                    _flag_state(f"credential:{cid}", f"Passphrase credential '{cid}' has no system.init event")
            elif ckind == "agent_token":
                iev = issued_agent_tokens.get(cid)
                if iev is None:
                    _flag_state(f"credential:{cid}", f"Agent token '{cid}' has no agent.token_issued event")
                else:
                    ipay = _parse_json_dict(iev.get("payload_json")) or {}
                    if ipay.get("agent_id") is not None and ipay.get("agent_id") != crow["principal_id"]:
                        _flag_state(f"credential:{cid}", f"Agent token '{cid}' principal differs from agent.token_issued event")
                    if ipay.get("lookup") is not None and ipay.get("lookup") != crow["lookup"]:
                        _flag_state(f"credential:{cid}", f"Agent token '{cid}' lookup differs from agent.token_issued event")
                    is_rev = crow["revoked_at"] is not None
                    exp_rev = revoked_agent_tokens.get(cid, False)
                    if is_rev != exp_rev:
                        _flag_state(
                            f"credential:{cid}",
                            f"Agent token '{cid}' revoked state ({is_rev}) differs from event history ({exp_rev})",
                        )
            elif ckind == "run_token":
                scope = _parse_json_dict(crow["scope_json"])
                rid = scope.get("run_id") if scope else None
                if not isinstance(rid, str) or not rid or rid not in started_runs:
                    _flag_state(f"credential:{cid}", f"Run token '{cid}' has no matching task.started or lead.turn_started event")

        for prow in ctx.conn.execute("SELECT id FROM projects ORDER BY id ASC;").fetchall():
            if prow["id"] not in created_projects:
                _flag_state(f"project:{prow['id']}", f"Project '{prow['id']}' has no project.created event")

        for trow in ctx.conn.execute("SELECT id FROM tasks ORDER BY id ASC;").fetchall():
            if trow["id"] not in created_tasks:
                _flag_state(f"task:{trow['id']}", f"Task '{trow['id']}' has no task.created or task.assigned event")

        for rrow in ctx.conn.execute("SELECT id FROM runs ORDER BY id ASC;").fetchall():
            if rrow["id"] not in started_runs:
                _flag_state(f"run:{rrow['id']}", f"Run '{rrow['id']}' has no start event in event chain")

        for viol in check_i4_status_events(ctx.conn):
            subj = viol.details.get("project_id") or viol.details.get("task_id") or "i4"
            _flag_state(f"i4:{subj}", viol.message)

    relayed_summary = [
        {"relayed_by": r_by, "credential_id": c_id, "count": count}
        for (r_by, c_id), count in sorted(relayed_counts.items())
    ]

    # 3. Mismatch evaluation
    if broken_event_seqs or broken_approval_ids or state_mismatches:
        msg = f"Audit verification failed: {len(broken_event_seqs)} broken event(s), {len(broken_approval_ids)} broken approval(s)"
        if state_mismatches:
            msg += f", {len(state_mismatches)} state mismatch(es)"
        details: Dict[str, Any] = {
            "broken_event_seqs": broken_event_seqs,
            "broken_approval_ids": broken_approval_ids,
            "reasons": reasons,
            "events_verified": events_verified,
            "approvals_verified": approvals_verified,
            "relayed_summary": relayed_summary,
        }
        if state_mismatches:
            details["state_mismatches"] = state_mismatches
        raise MeshError(
            code=c.AUDIT_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,  # 12
            message=msg,
            details=details,
        )

    return HandlerResult(
        data={
            "events_verified": events_verified,
            "approvals_verified": approvals_verified,
            "direct_decisions": direct_decisions,
            "relayed_decisions": relayed_decisions,
            "delegated_decisions": delegated_decisions,
            "relayed_summary": relayed_summary,
        }
    )
