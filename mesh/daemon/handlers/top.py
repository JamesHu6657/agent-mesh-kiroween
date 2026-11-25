"""Handlers for top-level commands: whoami, version."""

import mesh
from mesh.daemon.handlers.base import HandlerContext, HandlerResult


def handle_whoami(ctx: HandlerContext) -> HandlerResult:
    """Return principal details for the authenticated caller."""
    data = {
        "principal_id": ctx.principal["id"],
        "kind": ctx.principal["kind"],
        "name": ctx.principal["name"],
        "via": ctx.via,
    }
    if "capabilities" in ctx.principal:
        data["capabilities"] = ctx.principal["capabilities"]
    return HandlerResult(data=data)


def handle_version(ctx: HandlerContext) -> HandlerResult:
    """Return Agent Mesh version."""
    return HandlerResult(data={"version": mesh.__version__})
