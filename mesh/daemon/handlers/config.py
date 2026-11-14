"""Handlers for config commands: config.list, config.get, config.set."""

import json
import os
from typing import Any, Dict

import mesh.constants as c
from mesh.errors import MeshError
from mesh.core.clock import now_iso_utc
from mesh.core.events import insert_event
from mesh.core.settings import get_default_settings, validate_setting
from mesh.core.token_roots import remember_protected_path
from mesh.core.workspaces import get_workspaces_root
from mesh.daemon.handlers.base import HandlerContext, HandlerResult


def handle_config_list(ctx: HandlerContext) -> HandlerResult:
    """List all settings with current value, default value, and whether it was set."""
    if ctx.actor_kind != "user" or ctx.via != "direct":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="config.list is user-only (direct factor required)",
        )
    defaults = get_default_settings(ctx.home)

    cur = ctx.conn.execute("SELECT key, value_json FROM settings;")
    db_settings: Dict[str, Any] = {}
    for row in cur.fetchall():
        db_settings[row["key"]] = json.loads(row["value_json"])

    items = []
    for key in sorted(defaults.keys()):
        default_val = defaults[key]
        if key in db_settings:
            val = db_settings[key]
            is_set = True
        else:
            val = default_val
            is_set = False
        items.append({
            "key": key,
            "value": val,
            "default": default_val,
            "set": is_set,
        })

    return HandlerResult(data={"settings": items, "items": items})


def handle_config_get(ctx: HandlerContext) -> HandlerResult:
    """Get a specific configuration setting."""
    if ctx.actor_kind != "user" or ctx.via != "direct":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="config.get is user-only (direct factor required)",
        )
    key = ctx.args.get("key")
    defaults = get_default_settings(ctx.home)

    if not key or key not in defaults:
        raise MeshError(
            code=c.INVALID_SETTING,
            exit_code=c.EXIT_USAGE,
            message=f"Unknown setting key: '{key}'",
            details={"key": key},
        )

    cur = ctx.conn.execute("SELECT value_json FROM settings WHERE key = ?;", (key,))
    row = cur.fetchone()
    if row:
        val = json.loads(row["value_json"])
    else:
        val = defaults[key]

    return HandlerResult(data={"key": key, "value": val})


def handle_config_set(ctx: HandlerContext) -> HandlerResult:
    """Set a configuration setting. Direct user only."""
    # Plan §7.2, §7.3: category 'config' is direct-only and cannot be relayed or delegated.
    # "Any agent credential (with or without --relayed, with or without the relay capability)
    # calling a config.*, agent.*, or agent token.* op gets exit 4 CAPABILITY_MISSING"
    if ctx.actor_kind != "user" or ctx.via != "direct":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Configuration modification is user-only (direct factor required)",
        )

    key = ctx.args.get("key")
    raw_val = ctx.args.get("value")

    # Load current settings for dependencies like task.max_attempts_cap
    cur = ctx.conn.execute("SELECT key, value_json FROM settings;")
    current_settings = {r["key"]: json.loads(r["value_json"]) for r in cur.fetchall()}

    # Validate setting
    validated_val = validate_setting(
        key=key,
        val=raw_val,
        home=ctx.home,
        current_settings=current_settings,
        db_conn=ctx.conn,
    )

    if key == "workspaces.root":
        # Linear full review A4: integration worktrees live at <root>/<prj>/integration and
        # the merge pipeline rebuilds that path from the current root, so moving the root
        # under live repo projects would strand them. Refuse until they are finished.
        old_root = get_workspaces_root(ctx.conn, ctx.home)
        if isinstance(validated_val, str) and os.path.abspath(validated_val) != old_root:
            live = [r["id"] for r in ctx.conn.execute(
                "SELECT id FROM projects WHERE repo_path IS NOT NULL "
                "AND status IN ('active','paused') ORDER BY id;").fetchall()]
            if live:
                raise MeshError(
                    code=c.INVALID_SETTING,
                    exit_code=c.EXIT_CONFLICT,
                    message=(f"Cannot change workspaces.root while repo projects are active or paused "
                             f"({', '.join(live[:5])}{', ...' if len(live) > 5 else ''}): their integration "
                             f"worktrees are under {old_root}. Complete or cancel them first."),
                    details={"key": key, "current_root": old_root, "projects": live},
                )

    now = now_iso_utc()
    val_json = json.dumps(validated_val)

    if not ctx.dry_run:
        if key == "workspaces.root":
            # Keep the previous and the new root unredacted for the token guard.
            remember_protected_path(ctx.conn, get_workspaces_root(ctx.conn, ctx.home))
            if isinstance(validated_val, str):
                remember_protected_path(ctx.conn, validated_val)
        ctx.conn.execute(
            """INSERT INTO settings (key, value_json, updated_at, updated_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value_json = excluded.value_json,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by;""",
            (key, val_json, now, ctx.principal["id"]),
        )

        insert_event(
            conn=ctx.conn,
            event_type="config.updated",
            actor_id=ctx.principal["id"],
            actor_kind="user",
            via="direct",
            request_id=ctx.request_id,
            payload={"key": key, "value": validated_val},
            k_user=ctx.k_user,
        )

    data = {"key": key, "value": validated_val, "updated_at": now}
    warnings = []
    if key == "inbox.ack_timeout_s" and isinstance(validated_val, int):
        from mesh.daemon.scheduler import offers_kept_by_window

        kept = offers_kept_by_window(ctx.conn, None, validated_val)
        if kept:
            data["offers_keeping_window"] = kept
            keep = [k["task_id"] for k in kept if k["kept"]]
            lose = [k["task_id"] for k in kept if not k["kept"]]
            if keep:
                warnings.append(
                    f"{len(keep)} task(s) already offered keep the ack window they were offered with "
                    f"(a new window applies to new offers only): " + ", ".join(keep[:10]))
            if lose:
                warnings.append(
                    f"{len(lose)} task(s) offered before ack windows were recorded will fail no_response "
                    f"on the next scheduler tick: " + ", ".join(lose[:10]))
    return HandlerResult(data=data, warnings=warnings)
