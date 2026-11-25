"""Handlers for daemon commands: daemon.status, daemon.stop."""

import os
import mesh.constants as c
from mesh.errors import MeshError
from mesh.daemon.handlers.base import Effect, HandlerContext, HandlerResult


def handle_daemon_status(ctx: HandlerContext, daemon_info: dict) -> HandlerResult:
    """Return live status of meshd."""
    return HandlerResult(data=daemon_info)


def handle_daemon_stop(ctx: HandlerContext) -> HandlerResult:
    """Stop meshd gracefully. User only."""
    if ctx.actor_kind != "user":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Only the direct user principal can stop meshd",
        )

    # Queue side effect to initiate server shutdown after commit
    ctx.effects.append(Effect(kind="stop_daemon"))
    return HandlerResult(data={"status": "stopping", "message": "Daemon shutdown initiated"})
