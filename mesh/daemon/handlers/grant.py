"""Standing grant command handlers: grant.list, grant.revoke (direct user only)."""

from typing import Any, List

import mesh.constants as c
from mesh.core.grants import STANDING_GRANT_CATEGORIES, format_grant, revoke_standing_grant
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.errors import MeshError


def _require_direct_user(ctx: HandlerContext, cmd: str, *, require_key: bool = False) -> None:
    if ctx.actor_kind != "user" or ctx.via != "direct":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Command '{cmd}' requires direct user authentication",
            details={"cmd": cmd, "required": "user_direct", "actor_kind": ctx.actor_kind, "via": ctx.via},
        )
    if require_key and not ctx.k_user:
        raise MeshError(
            code=c.PASSPHRASE_REQUIRED,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message=f"Command '{cmd}' requires direct user passphrase",
        )


def handle_grant_list(ctx: HandlerContext) -> HandlerResult:
    """List standing grants for the settings UI (direct user only)."""
    _require_direct_user(ctx, "grant.list", require_key=False)

    project_id = ctx.args.get("project_id") or ctx.args.get("project")
    if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'project_id' must be a non-empty string when provided",
            details={"param": "project_id"},
        )
    category = ctx.args.get("category")
    if category is not None:
        if not isinstance(category, str) or category not in STANDING_GRANT_CATEGORIES:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Field 'category' must be one of {list(STANDING_GRANT_CATEGORIES)}",
                details={"param": "category", "expected": list(STANDING_GRANT_CATEGORIES), "got": category},
            )
    include_revoked = ctx.args.get("include_revoked", False)
    if not isinstance(include_revoked, bool):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'include_revoked' must be a boolean",
            details={"param": "include_revoked", "got": include_revoked},
        )

    where = ["1=1"]
    params: List[Any] = []
    if project_id:
        where.append("project_id = ?")
        params.append(project_id.strip())
    if category:
        where.append("category = ?")
        params.append(category)
    if not include_revoked:
        where.append("revoked_at IS NULL")

    rows = ctx.conn.execute(
        f"SELECT * FROM standing_grants WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC;",
        params,
    ).fetchall()
    items = [format_grant(ctx.conn, r) for r in rows]
    return HandlerResult(data={"items": items})


def handle_grant_revoke(ctx: HandlerContext) -> HandlerResult:
    """Revoke a standing grant (direct user only, audited with signed user_mac)."""
    _require_direct_user(ctx, "grant.revoke", require_key=True)

    grant_id = ctx.args.get("grant_id") or ctx.args.get("id")
    if not isinstance(grant_id, str) or not grant_id.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'grant_id' is required",
            details={"param": "grant_id"},
        )
    reason = ctx.args.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 4000):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'reason' must be a string up to 4000 characters",
            details={"param": "reason"},
        )

    grant, newly_revoked = revoke_standing_grant(
        ctx.conn,
        grant_id=grant_id.strip(),
        actor_id=ctx.principal["id"],
        k_user=ctx.k_user,
        reason=reason,
        request_id=ctx.request_id,
    )
    return HandlerResult(
        data={
            "grant": grant,
            "grant_id": grant["id"],
            "revoked": True,
            "already_revoked": not newly_revoked,
        },
        events=["grant.revoked"] if newly_revoked else [],
    )
