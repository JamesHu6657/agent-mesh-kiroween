"""Per-project permission modes and narrowly-scoped persistent standing grants.

Permission modes (per project, stored on ``projects.permission_mode``):
- ``supervised``: every dispatched step requires a direct-user card approval.
- ``plan`` (DEFAULT): approving the plan card runs all its non-escalated steps.
- ``auto``: no plan approval; ``retry_over_limit`` is auto-allowed.
  ``permission_escalation`` is NEVER auto-approved by mode alone.

Standing grants (stored in ``standing_grants``, created only by direct user ``always_allow``):
- ``permission_escalation``: scoped strictly to exact ``(project_id, agent_id, exact_profile)`` (never broader).
- ``retry_over_limit``: scoped to exact ``project_id``.
- ``config`` writes remain strictly direct-user-only and do NOT support standing grants.

Security note on runtime ``user_mac`` verification:
Runtime grant lookup (``find_matching_standing_grant``) runs on agent RPC paths where
``k_user`` is not available, so ``_verify_grant_row_against_events`` verifies ``user_mac``
presence, ``created_via == 'direct'``, exact ``scope_key``, and matching ``grant.created`` /
absence of ``grant.revoked`` events (mirroring ``verify_row_for_consume``). Cryptographic
HMAC verification of ``user_mac`` and full hash-chain verification run in ``audit.verify``
with direct user passphrase authentication.
"""

import hashlib
import hmac
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from mesh.core.clock import now_iso_utc
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.core.redact import redact
from mesh.core.settings import PROFILES
from mesh.core.states import is_task_terminal
from mesh.errors import MeshError

PERMISSION_MODES = ("supervised", "plan", "auto")
DEFAULT_PERMISSION_MODE = "plan"
STANDING_GRANT_CATEGORIES = ("permission_escalation", "retry_over_limit")
COMPOUND_REQUEUE_OPS = ("task.reassign", "task.retry", "task.reject")
INTERNAL_ONLY_APPROVAL_CATEGORIES = ("plan_dispatch", "step_dispatch")
INTERNAL_STANDING_GRANT_OPS = ("task.assign", "task.create", "task.reassign", "task.retry", "task.reject")
INTERNAL_AUTO_SWITCH_OPS = {
    "plan_dispatch": "plan.dispatch",
    "step_dispatch": "task.assign",
}


def get_project_permission_mode(conn: sqlite3.Connection, project_id: Optional[str]) -> str:
    """Return effective permission_mode for a project (defaults to 'plan')."""
    if not project_id:
        return DEFAULT_PERMISSION_MODE
    try:
        row = conn.execute(
            "SELECT permission_mode FROM projects WHERE id = ?;",
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return DEFAULT_PERMISSION_MODE
    if row is None:
        return DEFAULT_PERMISSION_MODE
    mode = row["permission_mode"]
    return mode if mode in PERMISSION_MODES else DEFAULT_PERMISSION_MODE


def build_grant_scope_key(
    category: str,
    project_id: Optional[str],
    *,
    scope_agent_id: Optional[str] = None,
    scope_profile: Optional[str] = None,
    scope_config_key: Optional[str] = None,
) -> str:
    """Build deterministic narrow scope_key for a standing grant."""
    if category == "permission_escalation":
        if not project_id or not scope_agent_id or scope_profile not in PROFILES or scope_config_key is not None:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="permission_escalation standing grant requires exact project_id, agent_id, and target profile",
                details={"category": category, "project_id": project_id, "agent_id": scope_agent_id, "profile": scope_profile},
            )
        return f"permission_escalation:{project_id}:{scope_agent_id}:{scope_profile}"

    if category == "retry_over_limit":
        if not project_id or scope_agent_id is not None or scope_profile is not None or scope_config_key is not None:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="retry_over_limit standing grant requires project_id",
                details={"category": category, "project_id": project_id},
            )
        return f"retry_over_limit:{project_id}"

    if category == "config":
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Standing grants are not supported for category 'config' (config writes remain direct-user-only)",
            details={"category": category},
        )

    raise MeshError(
        code=c.INVALID_ARGUMENT,
        exit_code=c.EXIT_USAGE,
        message=f"Standing grants are not supported for category '{category}'",
        details={"category": category},
    )


def compute_user_grant_mac(
    k_user: bytes,
    grant_id: str,
    project_id: Optional[str],
    category: str,
    scope_key: str,
    source_approval_id: Optional[str],
    created_at: str,
) -> str:
    """Compute HMAC-SHA256 user MAC binding a standing grant creation to direct user auth."""
    msg = (
        f"mesh-grant-v1\n{grant_id}\n{project_id or ''}\n{category}\n"
        f"{scope_key}\n{source_approval_id or ''}\n{created_at}"
    ).encode("utf-8")
    return hmac.new(k_user, msg, hashlib.sha256).hexdigest()


def compute_user_grant_revoke_mac(
    k_user: bytes,
    grant_id: str,
    revoked_at: str,
) -> str:
    """Compute HMAC-SHA256 user MAC binding a standing grant revocation to direct user auth."""
    msg = f"mesh-grant-revoke-v1\n{grant_id}\n{revoked_at}".encode("utf-8")
    return hmac.new(k_user, msg, hashlib.sha256).hexdigest()


def _parse_approval_op_doc(op_json: Optional[str]) -> Optional[Dict[str, Any]]:
    if not op_json:
        return None
    try:
        doc = json.loads(op_json)
    except (TypeError, ValueError):
        return None
    if isinstance(doc, dict) and doc.get("_redacted") is True:
        inner = doc.get("op")
        return inner if isinstance(inner, dict) else None
    return doc if isinstance(doc, dict) else None


def resolve_agent_row_for_grant(
    conn: sqlite3.Connection,
    ident: Optional[str],
    task_id: Optional[str] = None,
) -> Optional[sqlite3.Row]:
    """Deterministically resolve an agent row for standing-grant creation, preview, and lookup.

    Exact ``agents.id`` matches always take precedence over ``principals.name`` matches,
    with ``a.id ASC`` tie-breaking, so creation-time and use-time resolution never diverge.
    """
    if isinstance(ident, str) and ident.strip():
        clean = ident.strip()
        row = conn.execute(
            """SELECT a.id, p.name, a.adapter, a.delivery, a.max_unapproved_profile
               FROM agents a JOIN principals p ON a.id = p.id
               WHERE a.id = ? OR p.name = ?
               ORDER BY CASE WHEN a.id = ? THEN 0 ELSE 1 END, a.id ASC
               LIMIT 1;""",
            (clean, clean, clean),
        ).fetchone()
        if row is not None:
            return row
    if task_id:
        row = conn.execute(
            """SELECT a.id, p.name, a.adapter, a.delivery, a.max_unapproved_profile
               FROM tasks t
               JOIN agents a ON t.assignee_agent_id = a.id
               JOIN principals p ON a.id = p.id
               WHERE t.id = ?
               LIMIT 1;""",
            (task_id,),
        ).fetchone()
        if row is not None:
            return row
    return None


_resolve_agent_row_for_grant = resolve_agent_row_for_grant


def _validate_no_profile_broadening(
    agent_row: sqlite3.Row,
    target_profile: str,
    testing_mode: Optional[bool] = None,
) -> None:
    """Ensure standing grant never broadens beyond a profile the adapter explicitly supports."""
    if target_profile not in PROFILES:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid target profile '{target_profile}' for permission_escalation standing grant",
            details={"profile": target_profile},
        )
    if agent_row["delivery"] == "launch":
        from mesh.adapters.base import ProfileUnsupported
        from mesh.adapters.registry import get_adapter

        adapter = get_adapter(agent_row["adapter"], testing_mode=testing_mode)
        try:
            adapter.profile_flags(target_profile)
        except ProfileUnsupported:
            raise ProfileUnsupported(agent_row["adapter"], target_profile)


def _get_row_field(row: Any, field: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(field, default)
    try:
        return row[field]
    except (KeyError, IndexError, TypeError):
        return default


def _validate_and_expand_grant_categories(
    approval_row: Any,
    params: Dict[str, Any],
) -> List[str]:
    """Return grant categories for an approval row, honoring compound ``params.gates`` ONLY for
    the exact compound pair the codebase emits (``category == 'retry_over_limit'`` on internal
    requeue ops with extra gates ⊆ ``{'permission_escalation'}``). Rejects any other ``gates``.
    """
    category = _get_row_field(approval_row, "category")
    op_type = _get_row_field(approval_row, "op_type")
    categories: List[str] = [category]

    if "gates" not in params or params["gates"] is None:
        return categories

    gates = params["gates"]
    if not isinstance(gates, list) or not all(isinstance(g, str) and g.strip() for g in gates):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Invalid 'gates' in approval operation parameters",
            details={"approval_id": _get_row_field(approval_row, "id"), "category": category, "gates": gates},
        )
    if not gates:
        return categories

    extra_gates = [g for g in gates if g != category]
    is_allowed_op = op_type in COMPOUND_REQUEUE_OPS
    if (
        category != "retry_over_limit"
        or not is_allowed_op
        or any(g not in ("retry_over_limit", "permission_escalation") for g in gates)
        or not set(extra_gates).issubset({"permission_escalation"})
    ):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=(
                "Compound 'gates' is only supported on retry_over_limit task approvals "
                "with permission_escalation; refusing standing grant creation"
            ),
            details={
                "approval_id": _get_row_field(approval_row, "id"),
                "category": category,
                "op_type": op_type,
                "gates": gates,
            },
        )
    if "permission_escalation" in extra_gates:
        categories.append("permission_escalation")
    return categories


def extract_grant_scopes_from_approval(
    conn: sqlite3.Connection,
    approval_row: Any,
    *,
    testing_mode: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Extract exact narrow standing-grant scope(s) from an approval row.

    Returns a list of scope dicts (1 item normally; 2 narrow items only for a genuine compound
    ``retry_over_limit`` + ``permission_escalation`` requeue approval). Never returns a broader scope.
    """
    category = approval_row["category"]
    op_type = _get_row_field(approval_row, "op_type")
    project_id = approval_row["project_id"]
    task_id = approval_row["task_id"]
    if op_type not in INTERNAL_STANDING_GRANT_OPS:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=(
                f"Standing grants ('always_allow') are only supported for internal gated operations "
                f"(not '{op_type}')"
            ),
            details={"approval_id": _get_row_field(approval_row, "id"), "category": category, "op_type": op_type},
        )
    op_doc = _parse_approval_op_doc(approval_row["op_json"])
    params = op_doc.get("params") if isinstance(op_doc, dict) and isinstance(op_doc.get("params"), dict) else {}

    categories = _validate_and_expand_grant_categories(approval_row, params)

    scopes: List[Dict[str, Any]] = []
    for cat in categories:
        if cat == "permission_escalation":
            if not project_id:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="permission_escalation standing grant requires a project-scoped approval",
                    details={"approval_id": approval_row["id"], "category": cat},
                )
            target_profile = params.get("profile")
            if not isinstance(target_profile, str) or target_profile not in PROFILES:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Cannot create permission_escalation standing grant without an exact target profile",
                    details={"approval_id": approval_row["id"], "profile": target_profile},
                )
            agent_ident = params.get("to") or params.get("assignee") or params.get("agent")
            agent_row = resolve_agent_row_for_grant(conn, agent_ident, task_id)
            if agent_row is None:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Cannot create permission_escalation standing grant without an exact target agent",
                    details={"approval_id": approval_row["id"], "agent": agent_ident},
                )
            _validate_no_profile_broadening(agent_row, target_profile, testing_mode=testing_mode)
            scope_key = build_grant_scope_key(
                "permission_escalation",
                project_id,
                scope_agent_id=agent_row["id"],
                scope_profile=target_profile,
            )
            scopes.append({
                "category": "permission_escalation",
                "project_id": project_id,
                "scope_agent_id": agent_row["id"],
                "scope_agent_name": agent_row["name"],
                "scope_profile": target_profile,
                "scope_config_key": None,
                "scope_key": scope_key,
            })
        elif cat == "retry_over_limit":
            if not project_id:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="retry_over_limit standing grant requires a project-scoped approval",
                    details={"approval_id": approval_row["id"], "category": cat},
                )
            scope_key = build_grant_scope_key("retry_over_limit", project_id)
            scopes.append({
                "category": "retry_over_limit",
                "project_id": project_id,
                "scope_agent_id": None,
                "scope_agent_name": None,
                "scope_profile": None,
                "scope_config_key": None,
                "scope_key": scope_key,
            })
        elif cat == "config":
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=(
                    "Standing grants ('always_allow') are not supported for category 'config'; "
                    "config writes remain direct-user-only"
                ),
                details={
                    "approval_id": approval_row["id"],
                    "category": cat,
                    "supported_actions": ["allow", "reject"],
                },
            )
        else:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=(
                    f"Standing grants ('always_allow') are only supported for "
                    f"plan_dispatch, step_dispatch, permission_escalation, and retry_over_limit (not '{cat}')"
                ),
                details={"approval_id": approval_row["id"], "category": cat},
            )
    return scopes


def preview_grant_scope_for_card(
    conn: sqlite3.Connection,
    approval_row: Any,
    *,
    testing_mode: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """Exact UI preview of every scope '始终允许' would create (or auto-mode switch) for an approval card.

    Returns ``None`` if ``always_allow`` is not supported for this approval or if any grant scope
    cannot be validated and enumerated accurately.
    """
    cat = _get_row_field(approval_row, "category")
    op_type = _get_row_field(approval_row, "op_type")
    project_id = _get_row_field(approval_row, "project_id")
    op_doc = _parse_approval_op_doc(_get_row_field(approval_row, "op_json"))
    params = op_doc.get("params") if isinstance(op_doc, dict) and isinstance(op_doc.get("params"), dict) else {}

    if cat in ("plan_dispatch", "step_dispatch"):
        if not project_id or op_type != INTERNAL_AUTO_SWITCH_OPS.get(cat):
            return None
        if params.get("gates"):
            return None
        return {
            "kind": "switch_permission_mode",
            "target_permission_mode": "auto",
            "project_id": project_id,
            "confirmation_required": True,
        }
    if cat not in STANDING_GRANT_CATEGORIES or op_type not in INTERNAL_STANDING_GRANT_OPS:
        return None

    from mesh.adapters.base import ProfileUnsupported

    try:
        scopes = extract_grant_scopes_from_approval(conn, approval_row, testing_mode=testing_mode)
    except (MeshError, ProfileUnsupported):
        return None

    if not scopes:
        return None

    enumerated_scopes: List[Dict[str, Any]] = []
    for sc in scopes:
        entry: Dict[str, Any] = {
            "category": sc["category"],
            "project_id": sc["project_id"],
            "scope_key": sc["scope_key"],
        }
        if sc["category"] == "permission_escalation":
            entry["agent_id"] = sc["scope_agent_id"]
            entry["agent_name"] = sc["scope_agent_name"]
            entry["profile"] = sc["scope_profile"]
        enumerated_scopes.append(entry)

    primary = scopes[0]
    if primary["category"] == "permission_escalation":
        return {
            "kind": "standing_grant",
            "category": "permission_escalation",
            "project_id": primary["project_id"],
            "agent": primary["scope_agent_name"],
            "agent_id": primary["scope_agent_id"],
            "agent_name": primary["scope_agent_name"],
            "profile": primary["scope_profile"],
            "scope_key": primary["scope_key"],
            "scopes": enumerated_scopes,
        }
    if primary["category"] == "retry_over_limit":
        out: Dict[str, Any] = {
            "kind": "standing_grant",
            "category": "retry_over_limit",
            "project_id": primary["project_id"],
            "scope_key": primary["scope_key"],
            "scopes": enumerated_scopes,
        }
        for extra_sc in scopes[1:]:
            if extra_sc["category"] == "permission_escalation":
                out["also_grants_escalation"] = {
                    "agent": extra_sc["scope_agent_name"],
                    "agent_id": extra_sc["scope_agent_id"],
                    "agent_name": extra_sc["scope_agent_name"],
                    "profile": extra_sc["scope_profile"],
                    "scope_key": extra_sc["scope_key"],
                }
        return out
    return None


TASK_REASSIGN_FROM_STATES = ("assigned", "queued", "rework", "failed", "escalated", "merge_failed")


def expected_task_statuses_for_approval(
    op_type: Optional[str],
    category: Optional[str],
) -> Optional[Tuple[str, ...]]:
    """Return the valid task from-state(s) that can consume a task-bound approval, or None if unconstrained."""
    if op_type == "task.assign" or category == "step_dispatch":
        return ("created",)
    if op_type == "task.retry":
        return ("failed", "escalated")
    if op_type == "task.reject":
        return ("submitted", "merge_failed")
    if op_type == "task.reassign":
        if category == "retry_over_limit":
            return ("failed", "escalated")
        return TASK_REASSIGN_FROM_STATES
    return None


def evaluate_bound_task_for_approval(
    conn: sqlite3.Connection,
    approval_row: Any,
    *,
    prefetched_tasks: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Check whether a task-bound approval's task is terminal or has progressed past its gating state.

    Shared by ``compute_approval_card_actions``, ``handle_approval_decide``, and
    ``_handle_approval_decision`` so UI action suppression and server-side refusal remain identical.
    """
    task_id = _get_row_field(approval_row, "task_id")
    if not task_id:
        return {
            "task_id": None,
            "task_status": None,
            "task_kind": None,
            "task_terminal": False,
            "task_progressed": False,
            "expected_statuses": None,
            "dead_reason": None,
        }

    if prefetched_tasks is not None:
        trow = prefetched_tasks.get(task_id)
    else:
        trow = conn.execute(
            "SELECT id, status, kind FROM tasks WHERE id = ?;",
            (task_id,),
        ).fetchone()
    if trow is None:
        return {
            "task_id": task_id,
            "task_status": None,
            "task_kind": None,
            "task_terminal": True,
            "task_progressed": False,
            "expected_statuses": None,
            "dead_reason": "task_terminal",
        }

    t_status = trow["status"]
    t_kind = trow["kind"]
    if is_task_terminal(t_status, t_kind) or t_status == "cancelling":
        return {
            "task_id": task_id,
            "task_status": t_status,
            "task_kind": t_kind,
            "task_terminal": True,
            "task_progressed": False,
            "expected_statuses": None,
            "dead_reason": "task_terminal",
        }

    op_type = _get_row_field(approval_row, "op_type")
    cat = _get_row_field(approval_row, "category")
    expected = expected_task_statuses_for_approval(op_type, cat)
    if expected is not None and t_status not in expected:
        return {
            "task_id": task_id,
            "task_status": t_status,
            "task_kind": t_kind,
            "task_terminal": False,
            "task_progressed": True,
            "expected_statuses": expected,
            "dead_reason": "task_progressed",
        }

    return {
        "task_id": task_id,
        "task_status": t_status,
        "task_kind": t_kind,
        "task_terminal": False,
        "task_progressed": False,
        "expected_statuses": expected,
        "dead_reason": None,
    }


def compute_approval_card_actions(
    conn: sqlite3.Connection,
    approval_row: Any,
    *,
    now: Optional[str] = None,
    testing_mode: Optional[bool] = None,
    plan_status: Optional[str] = None,
    prefetched_project: Optional[Any] = None,
    prefetched_tasks: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute truthful UI card actions and decidability metadata for an approval row.

    Shared by ``chat.list`` and ``format_plan_summary`` so UI card buttons match server
    behavior exactly:
    - Approval actions (``allow`` / ``always_allow``) are suppressed when the card is expired,
      the project is inactive, the bound plan is terminal/non-proposed, the bound task is
      terminal or progressed past its gating state, or the project is already in ``auto`` mode.
    - Pending dead cards caused by an inactive project or a terminal/progressed bound task
      retain ``["reject"]`` (``dismiss_only=True``, ``approvable=False``) so users can dismiss
      them directly from the card UI (matching ``approval.decide(action="reject")``).
    - Relayed ``rejected`` cards retain ``["reject"]`` so a direct user can upgrade an
      unverified relayed rejection to ``direct_verified`` with ``user_mac``.
    - Expired, terminal-plan, consumed, cancelled, direct-rejected, and already-approved dead
      cards return ``[]`` because the server refuses every action on them.
    """
    now_ts = now or now_iso_utc()
    cat = _get_row_field(approval_row, "category")
    status = _get_row_field(approval_row, "status")
    decided_via = _get_row_field(approval_row, "decided_via")
    project_id = _get_row_field(approval_row, "project_id")
    expires_at = _get_row_field(approval_row, "expires_at")

    project_status: Optional[str] = None
    inactive_project = False
    project_pmode = DEFAULT_PERMISSION_MODE
    if project_id:
        if prefetched_project is not None and _get_row_field(prefetched_project, "id") == project_id:
            prow = prefetched_project
        else:
            prow = conn.execute(
                "SELECT id, status, permission_mode FROM projects WHERE id = ?;",
                (project_id,),
            ).fetchone()
        if prow is None:
            inactive_project = True
        else:
            project_status = prow["status"]
            inactive_project = (project_status != "active")
            pm_val = _get_row_field(prow, "permission_mode")
            if pm_val in PERMISSION_MODES:
                project_pmode = pm_val

    task_eval = evaluate_bound_task_for_approval(
        conn,
        approval_row,
        prefetched_tasks=prefetched_tasks,
    )
    task_status = task_eval["task_status"]
    task_terminal = task_eval["task_terminal"]
    task_progressed = task_eval["task_progressed"]

    eff_plan_status = plan_status
    if cat == "plan_dispatch" and eff_plan_status is None:
        has_plans = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plans';"
        ).fetchone()
        if has_plans:
            plrow = conn.execute(
                "SELECT status FROM plans WHERE approval_id = ? LIMIT 1;",
                (_get_row_field(approval_row, "id"),),
            ).fetchone()
            if plrow is not None:
                eff_plan_status = plrow["status"]
    plan_terminal = (
        cat == "plan_dispatch"
        and eff_plan_status is not None
        and eff_plan_status not in ("proposed", "approved_pending_dispatch")
    )
    plan_blocked = plan_terminal or (
        cat == "plan_dispatch"
        and status == "pending"
        and eff_plan_status is not None
        and eff_plan_status != "proposed"
    )

    is_expired = (status == "expired") or (
        status in ("pending", "approved") and isinstance(expires_at, str) and bool(expires_at) and expires_at < now_ts
    )
    already_auto = (
        cat in ("plan_dispatch", "step_dispatch")
        and bool(project_id)
        and project_pmode == "auto"
    )

    raw_preview = preview_grant_scope_for_card(conn, approval_row, testing_mode=testing_mode)

    avail: List[str]
    eff_preview: Optional[Dict[str, Any]]
    decidable: bool
    approvable: bool
    dismiss_only: bool
    undecidable_reason: Optional[str]
    unapprovable_reason: Optional[str]

    if is_expired:
        avail = []
        eff_preview = None
        decidable = False
        approvable = False
        dismiss_only = False
        undecidable_reason = "expired"
        unapprovable_reason = "expired"
    elif status == "rejected" and decided_via == "relayed":
        avail = ["reject"]
        eff_preview = None
        decidable = True
        approvable = False
        dismiss_only = False
        undecidable_reason = None
        unapprovable_reason = "rejected"
    elif status not in ("pending", "approved"):
        reason = str(status) if status else "terminal"
        avail = []
        eff_preview = None
        decidable = False
        approvable = False
        dismiss_only = False
        undecidable_reason = reason
        unapprovable_reason = reason
    elif plan_blocked:
        reason = f"plan_{eff_plan_status}"
        avail = []
        eff_preview = None
        decidable = False
        approvable = False
        dismiss_only = False
        undecidable_reason = reason
        unapprovable_reason = reason
    elif inactive_project or task_terminal or task_progressed:
        if inactive_project:
            block_reason = "project_not_active"
        elif task_terminal:
            block_reason = "task_terminal"
        else:
            block_reason = "task_progressed"
        eff_preview = None
        approvable = False
        unapprovable_reason = block_reason
        if status == "pending":
            avail = ["reject"]
            decidable = True
            dismiss_only = True
            undecidable_reason = None
        else:
            avail = []
            decidable = False
            dismiss_only = False
            undecidable_reason = block_reason
    else:
        already_granted = False
        if (
            raw_preview is not None
            and raw_preview.get("kind") == "standing_grant"
            and isinstance(raw_preview.get("scopes"), list)
            and raw_preview["scopes"]
        ):
            scope_keys = [
                s.get("scope_key")
                for s in raw_preview["scopes"]
                if isinstance(s, dict) and s.get("scope_key")
            ]
            if scope_keys and len(scope_keys) == len(raw_preview["scopes"]):
                try:
                    placeholders = ",".join("?" for _ in scope_keys)
                    active_rows = conn.execute(
                        f"SELECT * FROM standing_grants WHERE revoked_at IS NULL AND scope_key IN ({placeholders});",
                        scope_keys,
                    ).fetchall()
                    active_keys = {
                        r["scope_key"]
                        for r in active_rows
                        if _verify_grant_row_against_events(conn, r)
                    }
                    already_granted = all(k in active_keys for k in scope_keys)
                except sqlite3.OperationalError:
                    already_granted = False

        supports_always = (raw_preview is not None) and (not already_auto) and (not already_granted)
        eff_preview = raw_preview if supports_always else None
        if status == "pending":
            avail = ["allow", "always_allow", "reject"] if supports_always else ["allow", "reject"]
        elif status == "approved" and decided_via == "relayed":
            avail = ["allow", "always_allow"] if supports_always else ["allow"]
        elif status == "approved" and decided_via == "direct":
            avail = ["always_allow"] if supports_always else []
        else:
            avail = []
        decidable = bool(avail)
        approvable = any(a in ("allow", "always_allow") for a in avail)
        dismiss_only = False
        if decidable:
            undecidable_reason = None
            unapprovable_reason = None
        elif already_auto:
            undecidable_reason = "already_auto_mode"
            unapprovable_reason = "already_auto_mode"
        else:
            undecidable_reason = "already_decided"
            unapprovable_reason = "already_decided"

    return {
        "available_actions": avail,
        "always_allow_effect": eff_preview,
        "decidable": decidable,
        "approvable": approvable,
        "dismiss_only": dismiss_only,
        "undecidable_reason": undecidable_reason,
        "unapprovable_reason": unapprovable_reason,
        "inactive_project": inactive_project,
        "expired": is_expired,
        "project_status": project_status,
        "task_status": task_status,
        "task_terminal": task_terminal,
        "task_progressed": task_progressed,
    }


def serialize_approval_card_summary(
    approval_row: Any,
    *,
    policy: str,
    card_ui: Dict[str, Any],
    plan_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a field-identical approval card view shared by ``chat.list`` and ``format_plan_summary``."""
    summary_raw = _get_row_field(approval_row, "summary")
    note_raw = _get_row_field(approval_row, "decision_note")
    out: Dict[str, Any] = {
        "id": _get_row_field(approval_row, "id"),
        "category": _get_row_field(approval_row, "category"),
        "policy": policy,
        "op_type": _get_row_field(approval_row, "op_type"),
        "op_hash": _get_row_field(approval_row, "op_hash"),
        "status": _get_row_field(approval_row, "status"),
        "summary": redact(summary_raw) if summary_raw else summary_raw,
        "decided_via": _get_row_field(approval_row, "decided_via"),
        "decision_note": redact(note_raw) if note_raw else None,
        "expires_at": _get_row_field(approval_row, "expires_at"),
        "task_id": _get_row_field(approval_row, "task_id"),
        "task_status": card_ui["task_status"],
        "task_terminal": card_ui["task_terminal"],
        "task_progressed": card_ui["task_progressed"],
        "available_actions": card_ui["available_actions"],
        "always_allow_effect": card_ui["always_allow_effect"],
        "decidable": card_ui["decidable"],
        "approvable": card_ui["approvable"],
        "dismiss_only": card_ui["dismiss_only"],
        "undecidable_reason": card_ui["undecidable_reason"],
        "unapprovable_reason": card_ui["unapprovable_reason"],
        "inactive_project": card_ui["inactive_project"],
        "expired": card_ui["expired"],
    }
    if plan_meta is not None:
        out["plan_id"] = plan_meta["id"]
        out["plan_version"] = plan_meta["version"]
        out["plan_hash"] = plan_meta["plan_hash"]
    return out


def format_grant(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    """Serialize a standing_grants row for API/UI consumers."""
    agent_name: Optional[str] = None
    if row["scope_agent_id"]:
        arow = conn.execute("SELECT name FROM principals WHERE id = ?;", (row["scope_agent_id"],)).fetchone()
        agent_name = arow["name"] if arow else row["scope_agent_id"]
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "category": row["category"],
        "scope_agent_id": row["scope_agent_id"],
        "scope_agent_name": agent_name,
        "scope_profile": row["scope_profile"],
        "scope_config_key": row["scope_config_key"],
        "scope_key": row["scope_key"],
        "source_approval_id": row["source_approval_id"],
        "created_by": row["created_by"],
        "created_via": row["created_via"],
        "created_at": row["created_at"],
        "user_mac": row["user_mac"],
        "active": row["revoked_at"] is None,
        "revoked_at": row["revoked_at"],
        "revoked_by": row["revoked_by"],
        "revoked_reason": redact(row["revoked_reason"]) if row["revoked_reason"] else None,
    }


def create_or_get_standing_grant(
    conn: sqlite3.Connection,
    *,
    project_id: Optional[str],
    category: str,
    scope_agent_id: Optional[str] = None,
    scope_profile: Optional[str] = None,
    scope_config_key: Optional[str] = None,
    source_approval_id: Optional[str] = None,
    actor_id: str,
    k_user: bytes,
    request_id: Optional[str] = None,
    now: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Create a direct-user signed standing grant or return the existing active grant for the exact scope."""
    if not k_user:
        raise MeshError(
            code=c.PASSPHRASE_REQUIRED,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message="Creating a standing grant requires direct user authentication",
        )
    scope_key = build_grant_scope_key(
        category,
        project_id,
        scope_agent_id=scope_agent_id,
        scope_profile=scope_profile,
        scope_config_key=scope_config_key,
    )
    existing = conn.execute(
        "SELECT * FROM standing_grants WHERE scope_key = ? AND revoked_at IS NULL LIMIT 1;",
        (scope_key,),
    ).fetchone()
    if existing is not None:
        return format_grant(conn, existing), False

    grant_id = generate_id("grt")
    created_at = now or now_iso_utc()
    user_mac = compute_user_grant_mac(
        k_user, grant_id, project_id, category, scope_key, source_approval_id, created_at
    )
    conn.execute(
        """INSERT INTO standing_grants (
            id, project_id, category, scope_agent_id, scope_profile, scope_config_key,
            scope_key, source_approval_id, created_by, created_via, created_at, user_mac,
            revoked_at, revoked_by, revoked_reason, revoked_user_mac
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'direct', ?, ?, NULL, NULL, NULL, NULL);""",
        (
            grant_id,
            project_id,
            category,
            scope_agent_id,
            scope_profile,
            scope_config_key,
            scope_key,
            source_approval_id,
            actor_id,
            created_at,
            user_mac,
        ),
    )

    agent_name: Optional[str] = None
    if scope_agent_id:
        arow = conn.execute("SELECT name FROM principals WHERE id = ?;", (scope_agent_id,)).fetchone()
        agent_name = arow["name"] if arow else None

    record_event(
        conn=conn,
        type_="grant.created",
        actor_id=actor_id,
        actor_kind="user",
        via="direct",
        project_id=project_id,
        approval_id=source_approval_id,
        payload={
            "grant_id": grant_id,
            "project_id": project_id,
            "category": category,
            "scope_agent_id": scope_agent_id,
            "scope_agent_name": agent_name,
            "scope_profile": scope_profile,
            "scope_config_key": scope_config_key,
            "scope_key": scope_key,
            "source_approval_id": source_approval_id,
            "created_at": created_at,
        },
        request_id=request_id,
        k_user=k_user,
    )
    row = conn.execute("SELECT * FROM standing_grants WHERE id = ?;", (grant_id,)).fetchone()
    return format_grant(conn, row), True


def _verify_grant_row_against_events(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Runtime defense-in-depth: confirm grant row matches direct-user grant.created and has no grant.revoked.

    Threat-model note: runtime lookup runs on agent RPC paths where ``k_user`` is not available
    in memory, so this check verifies ``user_mac`` presence, ``created_via == 'direct'``, exact
    ``scope_key`` consistency, and event-row correspondence — mirroring ``verify_row_for_consume``.
    A direct-SQLite attacker who fabricates a consistent ``standing_grants`` row + ``grant.created``
    event pair without ``k_user`` cannot pass cryptographic HMAC or hash-chain verification, which
    ``audit.verify`` checks under direct user passphrase authentication.
    """
    if row["revoked_at"] is not None or row["created_via"] != "direct" or not row["user_mac"]:
        return False
    expected_key = build_grant_scope_key(
        row["category"],
        row["project_id"],
        scope_agent_id=row["scope_agent_id"],
        scope_profile=row["scope_profile"],
        scope_config_key=row["scope_config_key"],
    )
    if row["scope_key"] != expected_key:
        return False

    if row["project_id"] is not None:
        ev_rows = conn.execute(
            """SELECT type, actor_kind, via, user_mac, payload_json
               FROM events
               WHERE type IN ('grant.created', 'grant.revoked')
                 AND project_id = ?
               ORDER BY seq ASC;""",
            (row["project_id"],),
        ).fetchall()
    else:
        ev_rows = conn.execute(
            """SELECT type, actor_kind, via, user_mac, payload_json
               FROM events
               WHERE type IN ('grant.created', 'grant.revoked')
                 AND project_id IS NULL
               ORDER BY seq ASC;"""
        ).fetchall()
    has_created = False
    for ev in ev_rows:
        try:
            pay = json.loads(ev["payload_json"] or "{}")
        except (TypeError, ValueError):
            return False
        if not isinstance(pay, dict) or pay.get("grant_id") != row["id"]:
            continue
        if ev["type"] == "grant.revoked":
            return False
        if ev["type"] == "grant.created":
            if ev["actor_kind"] != "user" or ev["via"] != "direct" or not ev["user_mac"]:
                return False
            if (
                pay.get("project_id") == row["project_id"]
                and pay.get("category") == row["category"]
                and pay.get("scope_agent_id") == row["scope_agent_id"]
                and pay.get("scope_profile") == row["scope_profile"]
                and pay.get("scope_config_key") == row["scope_config_key"]
                and pay.get("scope_key") == row["scope_key"]
            ):
                has_created = True
    return has_created


def find_matching_standing_grant(
    conn: sqlite3.Connection,
    *,
    project_id: Optional[str],
    category: str,
    agent_id: Optional[str] = None,
    profile: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Look up an active, event-verified standing grant matching the EXACT narrow scope."""
    if category not in STANDING_GRANT_CATEGORIES:
        return None
    has_tbl = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'standing_grants';"
    ).fetchone()
    if has_tbl is None:
        return None
    try:
        scope_key = build_grant_scope_key(
            category,
            project_id,
            scope_agent_id=agent_id if category == "permission_escalation" else None,
            scope_profile=profile if category == "permission_escalation" else None,
        )
    except MeshError:
        return None

    row = conn.execute(
        "SELECT * FROM standing_grants WHERE scope_key = ? AND revoked_at IS NULL LIMIT 1;",
        (scope_key,),
    ).fetchone()
    if row is None:
        return None
    if not _verify_grant_row_against_events(conn, row):
        return None
    return format_grant(conn, row)


def record_grant_used(
    conn: sqlite3.Connection,
    *,
    grant: Dict[str, Any],
    op: str,
    actor_id: str,
    actor_kind: str,
    via: str,
    project_id: Optional[str],
    task_id: Optional[str] = None,
    credential_id: Optional[str] = None,
    request_id: Optional[str] = None,
    k_user: Optional[bytes] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Audit every use of a standing grant in the hash-chained events table."""
    payload: Dict[str, Any] = {
        "grant_id": grant["id"],
        "project_id": grant["project_id"],
        "category": grant["category"],
        "op": op,
        "scope_agent_id": grant["scope_agent_id"],
        "scope_agent_name": grant.get("scope_agent_name"),
        "scope_profile": grant["scope_profile"],
        "scope_config_key": grant["scope_config_key"],
        "scope_key": grant["scope_key"],
    }
    if extra:
        payload.update(extra)
    return record_event(
        conn=conn,
        type_="grant.used",
        actor_id=actor_id,
        actor_kind=actor_kind,
        via=via,
        credential_id=credential_id,
        project_id=project_id,
        task_id=task_id,
        payload=payload,
        request_id=request_id,
        k_user=k_user,
    )


def revoke_standing_grant(
    conn: sqlite3.Connection,
    *,
    grant_id: str,
    actor_id: str,
    k_user: bytes,
    reason: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Revoke a standing grant (direct user only) and audit with signed user_mac."""
    if not k_user:
        raise MeshError(
            code=c.PASSPHRASE_REQUIRED,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message="Revoking a standing grant requires direct user authentication",
        )
    row = conn.execute("SELECT * FROM standing_grants WHERE id = ?;", (grant_id,)).fetchone()
    if row is None:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Standing grant '{grant_id}' not found",
            details={"grant_id": grant_id},
        )
    if row["revoked_at"] is not None:
        return format_grant(conn, row), False

    revoked_at = now_iso_utc()
    clean_reason = redact(reason.strip()) if isinstance(reason, str) and reason.strip() else None
    revoked_mac = compute_user_grant_revoke_mac(k_user, grant_id, revoked_at)

    conn.execute(
        """UPDATE standing_grants
           SET revoked_at = ?, revoked_by = ?, revoked_reason = ?, revoked_user_mac = ?
           WHERE id = ? AND revoked_at IS NULL;""",
        (revoked_at, actor_id, clean_reason, revoked_mac, grant_id),
    )

    record_event(
        conn=conn,
        type_="grant.revoked",
        actor_id=actor_id,
        actor_kind="user",
        via="direct",
        project_id=row["project_id"],
        approval_id=row["source_approval_id"],
        reason=clean_reason,
        payload={
            "grant_id": grant_id,
            "project_id": row["project_id"],
            "category": row["category"],
            "scope_key": row["scope_key"],
            "revoked_at": revoked_at,
            "reason": clean_reason,
        },
        request_id=request_id,
        k_user=k_user,
    )
    updated = conn.execute("SELECT * FROM standing_grants WHERE id = ?;", (grant_id,)).fetchone()
    return format_grant(conn, updated), True
