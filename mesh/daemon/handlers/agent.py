"""Handlers for agent resource commands: create, update, list, show, disable, enable, token."""

import json
import os
import re
from typing import Any, Dict, List, Optional

import mesh.constants as c
from mesh.errors import MeshError
from mesh.core.auth import generate_agent_token
from mesh.core.clock import now_iso_utc, parse_duration
from mesh.core.events import insert_event
from mesh.core.ids import new_id
from mesh.core.policy import parse_positive_int
from mesh.core.settings import get_agent_ack_timeout_s, set_agent_ack_timeout_s, validate_ack_timeout
from mesh.adapters.registry import get_adapter, is_testing_mode
from mesh.adapters.base import AdapterNotInstalled, EffortNotAllowed, EffortUnsupported
from mesh.daemon.handlers.base import HandlerContext, HandlerResult


VALID_PROFILES = {"readonly", "edit", "dev", "elevated", "unrestricted"}
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-xhigh", "-max")
VALID_CAPABILITIES = {"intake", "relay"}


#: Capabilities an agent gets when `agent create` names none.
DEFAULT_CAPABILITIES = ("intake",)


def _ack_timeout_arg(ctx: HandlerContext, delivery: str) -> Optional[int]:
    """`--ack-timeout`: seconds, or None for 'default' / not given. Inbox agents only."""
    raw = ctx.args.get("ack_timeout")
    if raw is None or (isinstance(raw, str) and raw.strip().lower() == "default"):
        return None
    if delivery != "inbox":
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--ack-timeout applies to inbox agents only (launch agents are started by meshd)",
            details={"param": "ack_timeout", "delivery": delivery},
        )
    return validate_ack_timeout(raw)


def _ack_timeout_view(conn: Any, own: Optional[int], delivery: str = "inbox") -> Dict[str, Any]:
    """The ack window an inbox agent gets, and where it comes from."""
    if delivery != "inbox":
        return {}
    if own is not None:
        return {"ack_timeout_s": own, "ack_timeout_source": "agent"}
    from mesh.daemon.scheduler import get_inbox_ack_timeout_s

    return {"ack_timeout_s": get_inbox_ack_timeout_s(conn), "ack_timeout_source": "inbox.ack_timeout_s"}


#: Fields a lead sees in `agent list/show` (the roster view): what it needs to pick an
#: assignee and a model. Never credentials, adapter options or binary paths.
ROSTER_FIELDS = ("id", "name", "adapter", "delivery", "can_lead", "max_concurrency", "disabled",
                 "default_model", "allowed_models", "default_effort", "allowed_efforts",
                 "ack_timeout_s", "ack_timeout_source")


def _roster_view(ctx: HandlerContext, op_name: str) -> bool:
    """Who may read agents: the direct user (full view, False) or a lead (roster view, True).

    A lead is an enabled agent with can_lead, or the current lead of an active project.
    Everyone else (other agents, relayed calls) keeps the user-only rule (plan §7.3):
    agent definitions are configuration, and only leads need the roster to assign work.
    """
    if ctx.actor_kind == "user" and ctx.via == "direct":
        return False
    pid = (ctx.principal or {}).get("id")
    if ctx.actor_kind == "agent" and pid and ctx.via in ("token", "run_token"):
        row = ctx.conn.execute(
            """SELECT a.can_lead FROM agents a JOIN principals p ON p.id = a.id
               WHERE a.id = ? AND p.disabled_at IS NULL;""", (pid,)).fetchone()
        if row is not None and (row["can_lead"] or ctx.conn.execute(
                """SELECT 1 FROM lead_assignments la JOIN projects pr ON pr.id = la.project_id
                   WHERE la.agent_id = ? AND la.ended_at IS NULL AND pr.status = 'active' LIMIT 1;""",
                (pid,)).fetchone()):
            return True
    _check_direct_user_only(ctx, op_name)
    return False  # not reached


def _check_direct_user_only(ctx: HandlerContext, op_name: str) -> None:
    """Plan §7.2, §7.3: agent mutations and token operations are direct user only."""
    if ctx.actor_kind != "user" or ctx.via != "direct":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Operation '{op_name}' requires direct user authentication",
            details={"required": "user_direct", "actor_kind": ctx.actor_kind, "via": ctx.via},
        )


def _validate_model_name(model: str) -> None:
    if not model or len(model) > 256 or any(ch.isspace() or ord(ch) < 32 for ch in model):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid model name '{model}': must be 1-256 characters without whitespace or control chars",
            details={"param": "allowed_models", "value": model},
        )


def handle_agent_create(ctx: HandlerContext) -> HandlerResult:
    """Register a new agent definition. Direct user only."""
    _check_direct_user_only(ctx, "agent.create")

    name = ctx.args.get("name")
    if not name or not isinstance(name, str):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Agent 'name' is required and must be a string",
            details={"param": "name"},
        )

    # Check if agent already exists
    cur = ctx.conn.execute("SELECT id FROM principals WHERE name = ?;", (name,))
    if cur.fetchone():
        raise MeshError(
            code=c.ALREADY_EXISTS,
            exit_code=c.EXIT_CONFLICT,
            message=f"Principal or agent '{name}' already exists",
            details={"name": name},
        )

    adapter_name = ctx.args.get("adapter")
    if not adapter_name or not isinstance(adapter_name, str):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Agent 'adapter' is required",
            details={"param": "adapter"},
        )

    # Check adapter validity and test double status
    testing_mode = bool(getattr(ctx, "testing_mode", False) or is_testing_mode())
    options_raw = ctx.args.get("options") or ctx.args.get("option") or {}
    options: Dict[str, Any] = {}
    if isinstance(options_raw, list):
        for item in options_raw:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                try:
                    options[k.strip()] = json.loads(v.strip())
                except json.JSONDecodeError:
                    options[k.strip()] = v.strip()
    elif isinstance(options_raw, dict):
        options = dict(options_raw)

    adapter = get_adapter(adapter_name, testing_mode=testing_mode, options=options)
    binary_override = options.get("binary") or ctx.args.get("binary")
    if not adapter.is_installed(binary_path=binary_override):
        from mesh.adapters.base import AdapterNotInstalled
        raise AdapterNotInstalled(adapter_name)

    # Delivery mode
    delivery = ctx.args.get("delivery", "launch")
    if delivery not in ("launch", "inbox"):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid delivery mode '{delivery}': must be 'launch' or 'inbox'",
            details={"param": "delivery", "value": delivery},
        )

    if adapter_name == "none" and delivery != "inbox":
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Adapter 'none' is for inbox-only agents and delivery must be 'inbox'",
            details={"adapter": "none", "delivery": delivery},
        )

    # Max concurrency: 1 when not given (same as `agent onboard`).
    max_concurrency = (parse_positive_int(ctx.args["max_concurrency"], "max_concurrency")
                       if "max_concurrency" in ctx.args else 1)

    # Allowed models & Default model
    allowed_models = ctx.args.get("allowed_models") or ctx.args.get("allowed_model") or []
    if isinstance(allowed_models, str):
        allowed_models = [allowed_models]
    allowed_models = list(dict.fromkeys(allowed_models))  # deduplicate preserving order
    for m in allowed_models:
        _validate_model_name(m)

    default_model = ctx.args.get("default_model")

    if delivery == "launch":
        if not allowed_models:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Launch-mode agent requires a non-empty allowed_models list",
                details={"param": "allowed_models"},
            )
        if default_model is None:
            default_model = allowed_models[0]
        elif default_model not in allowed_models:
            raise MeshError(
                code=c.DEFAULT_MODEL_NOT_ALLOWED,
                exit_code=c.EXIT_USAGE,
                message=f"Default model '{default_model}' is not in allowed models list {allowed_models}",
                details={"default_model": default_model, "allowed": allowed_models},
            )
    else:
        # delivery == "inbox"
        if adapter_name == "none":
            allowed_models = []
            default_model = None
        else:
            if default_model and allowed_models and default_model not in allowed_models:
                raise MeshError(
                    code=c.DEFAULT_MODEL_NOT_ALLOWED,
                    exit_code=c.EXIT_USAGE,
                    message=f"Default model '{default_model}' is not in allowed models list",
                    details={"default_model": default_model, "allowed": allowed_models},
                )

    # Allowed efforts & Default effort
    allowed_efforts = ctx.args.get("allowed_efforts") or ctx.args.get("allowed_effort") or []
    if isinstance(allowed_efforts, str):
        allowed_efforts = [allowed_efforts]
    allowed_efforts = list(dict.fromkeys(allowed_efforts))

    default_effort = ctx.args.get("default_effort")

    # Check effort vs profile separation (G24)
    for eff in allowed_efforts:
        if eff in VALID_PROFILES:
            raise MeshError(
                code=c.EFFORT_NOT_ALLOWED,
                exit_code=c.EXIT_USAGE,
                message=f"Permission profile '{eff}' cannot be used as an effort level",
                details={"param": "allowed_efforts", "value": eff},
            )
    if default_effort:
        if default_effort in VALID_PROFILES:
            raise MeshError(
                code=c.EFFORT_NOT_ALLOWED,
                exit_code=c.EXIT_USAGE,
                message=f"Permission profile '{default_effort}' cannot be used as an effort level",
                details={"param": "default_effort", "value": default_effort},
            )

    # Check adapter effort support (e.g. vibe has none)
    effort_cap = adapter.capabilities.get("reasoning_effort")
    if effort_cap and effort_cap.supported == "no":
        if default_effort or allowed_efforts:
            raise EffortUnsupported(adapter_name)

    if default_effort and allowed_efforts and default_effort not in allowed_efforts:
        raise MeshError(
            code=c.EFFORT_NOT_ALLOWED,
            exit_code=c.EXIT_USAGE,
            message=f"Default effort '{default_effort}' is not in allowed efforts list",
            details={"default_effort": default_effort, "allowed": allowed_efforts},
        )

    # AGY model effort encoding rule (D17, §14.4)
    if adapter_name == "agy" and default_effort and default_model:
        for suffix in EFFORT_SUFFIXES:
            if default_model.endswith(suffix):
                raise EffortNotAllowed(
                    f"Model '{default_model}' already encodes reasoning effort; cannot specify effort '{default_effort}'",
                    details={"reason": "model_encodes_effort", "model": default_model, "effort": default_effort},
                )

    # Permission profiles
    default_profile = ctx.args.get("default_profile", "edit")
    if default_profile not in VALID_PROFILES:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid default_profile '{default_profile}': must be one of {sorted(VALID_PROFILES)}",
            details={"param": "default_profile", "value": default_profile},
        )
    if default_profile in VALID_EFFORTS:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Reasoning effort '{default_profile}' cannot be used as a permission profile",
            details={"param": "default_profile", "value": default_profile},
        )

    max_unapproved_profile = ctx.args.get("max_unapproved_profile", "dev")
    if max_unapproved_profile not in VALID_PROFILES:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid max_unapproved_profile '{max_unapproved_profile}': must be one of {sorted(VALID_PROFILES)}",
            details={"param": "max_unapproved_profile", "value": max_unapproved_profile},
        )

    can_lead = 1 if ctx.args.get("can_lead") else 0

    if "capabilities" in ctx.args and ctx.args["capabilities"] is not None:
        capabilities = ctx.args["capabilities"]
    elif "capability" in ctx.args and ctx.args["capability"] is not None:
        capabilities = ctx.args["capability"]
    else:
        capabilities = None
    explicit_caps = capabilities is not None
    if capabilities is None:
        capabilities = [] if ctx.args.get("no_intake") else list(DEFAULT_CAPABILITIES)
    if isinstance(capabilities, str):
        capabilities = [capabilities]
    capabilities = list(dict.fromkeys(capabilities))
    if ctx.args.get("no_intake"):
        capabilities = [cap for cap in capabilities if cap != "intake"]
    for cap in capabilities:
        if cap not in VALID_CAPABILITIES:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid capability '{cap}': must be in {sorted(VALID_CAPABILITIES)}",
                details={"param": "capabilities", "value": cap},
            )

    binary_path = ctx.args.get("binary")
    ack_timeout_s = _ack_timeout_arg(ctx, delivery)

    now = now_iso_utc()
    agent_id = new_id("agt")

    # Insert into principals
    ctx.conn.execute(
        """INSERT INTO principals (id, kind, name, created_at, disabled_at)
           VALUES (?, 'agent', ?, ?, NULL);""",
        (agent_id, name, now),
    )

    # Insert into agents
    ctx.conn.execute(
        """INSERT INTO agents (
               id, adapter, binary_path, delivery, can_lead, capabilities_json,
               default_model, allowed_models_json, default_effort, allowed_efforts_json,
               max_concurrency, default_profile, max_unapproved_profile,
               adapter_options_json, version, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?);""",
        (
            agent_id,
            adapter_name,
            binary_path,
            delivery,
            can_lead,
            json.dumps(capabilities),
            default_model,
            json.dumps(allowed_models),
            default_effort,
            json.dumps(allowed_efforts),
            max_concurrency,
            default_profile,
            max_unapproved_profile,
            json.dumps(options),
            now,
            now,
        ),
    )

    if ack_timeout_s is not None:
        set_agent_ack_timeout_s(ctx.conn, agent_id, ack_timeout_s, now, ctx.principal["id"])

    insert_event(
        ctx.conn,
        type_="agent.created",
        actor_id=ctx.principal["id"],
        actor_kind="user",
        via="direct",
        request_id=ctx.request_id,
        payload={
            "agent_id": agent_id,
            "name": name,
            "adapter": adapter_name,
            "delivery": delivery,
        },
        k_user=ctx.k_user,
    )

    data = {
        "id": agent_id,
        "name": name,
        "adapter": adapter_name,
        "delivery": delivery,
        "can_lead": bool(can_lead),
        "capabilities": capabilities,
        "default_model": default_model,
        "allowed_models": allowed_models,
        "default_effort": default_effort,
        "allowed_efforts": allowed_efforts,
        "max_concurrency": max_concurrency,
        "default_profile": default_profile,
        "max_unapproved_profile": max_unapproved_profile,
        "options": options,
        **_ack_timeout_view(ctx.conn, ack_timeout_s, delivery),
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }
    warnings = []
    if explicit_caps and "intake" not in capabilities and not ctx.args.get("no_intake"):
        warnings.append(
            "--capability replaces the default capability list ['intake']: this agent cannot "
            "submit projects with `mesh intake submit`. Add --capability intake to keep it, or "
            "pass --no-intake to drop it on purpose (and silence this warning).")
    if delivery == "inbox" and ack_timeout_s is None:
        from mesh.daemon.scheduler import get_inbox_ack_timeout_s

        global_ack = get_inbox_ack_timeout_s(ctx.conn)
        if global_ack < 3600:
            warnings.append(
                f"inbox agent {name} uses the global ack window inbox.ack_timeout_s = {global_ack}s: an offer "
                f"not started within it fails no_response. For a worker that polls slowly (email, cron), "
                f"set its own window: `mesh agent update {name} --ack-timeout 2h --user`.")
    return HandlerResult(data=data, warnings=warnings)


def handle_agent_update(ctx: HandlerContext) -> HandlerResult:
    """Update settings of an existing agent. Direct user only."""
    _check_direct_user_only(ctx, "agent.update")

    name = ctx.args.get("name")
    if not name:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Agent 'name' is required",
            details={"param": "name"},
        )

    cur = ctx.conn.execute(
        """SELECT p.id, p.disabled_at, a.*
           FROM principals p
           JOIN agents a ON p.id = a.id
           WHERE p.name = ?;""",
        (name,),
    )
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Agent '{name}' not found",
            details={"name": name},
        )

    # Optimistic concurrency check (B9)
    if_version = ctx.args.get("if_version")
    if if_version is not None:
        try:
            if_version_int = int(if_version)
        except (ValueError, TypeError):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid --if-version '{if_version}'",
                details={"param": "if_version", "value": if_version},
            )
        if if_version_int != row["version"]:
            raise MeshError(
                code=c.VERSION_MISMATCH,
                exit_code=c.EXIT_CONFLICT,
                message=f"Version mismatch: agent has version {row['version']}, but --if-version was {if_version_int}",
                details={"name": name, "version": row["version"], "if_version": if_version_int},
            )

    # Linear full review C4: these are fixed at create time; refuse them instead of
    # reporting success while ignoring them.
    fixed = [k for k in ("adapter", "delivery", "binary") if ctx.args.get(k) is not None]
    if ctx.args.get("options") is not None:
        fixed.append("options")
    if ctx.args.get("strict_models") is not None:
        fixed.append("strict_models")
    if fixed:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=(f"agent update cannot change {', '.join(fixed)}; these are set when the agent is "
                     f"created (disable this agent and create a new one)"),
            details={"param": fixed[0], "unsupported": fixed},
        )

    # Concurrency
    max_concurrency = row["max_concurrency"]
    if "max_concurrency" in ctx.args and ctx.args["max_concurrency"] is not None:
        max_concurrency = parse_positive_int(ctx.args["max_concurrency"], "max_concurrency")

    # Allowed models & Default model
    allowed_models = json.loads(row["allowed_models_json"])
    if "allowed_models" in ctx.args and ctx.args["allowed_models"] is not None:
        allowed_models = ctx.args["allowed_models"]
        if isinstance(allowed_models, str):
            allowed_models = [allowed_models]
        allowed_models = list(dict.fromkeys(allowed_models))
        for m in allowed_models:
            _validate_model_name(m)

    default_model = row["default_model"]
    if "default_model" in ctx.args and ctx.args["default_model"] is not None:
        default_model = ctx.args["default_model"]

    if row["delivery"] == "launch":
        if not allowed_models:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Launch-mode agent requires non-empty allowed_models",
                details={"param": "allowed_models"},
            )
        if default_model not in allowed_models:
            raise MeshError(
                code=c.DEFAULT_MODEL_NOT_ALLOWED,
                exit_code=c.EXIT_USAGE,
                message=f"Default model '{default_model}' is not in allowed models list",
                details={"default_model": default_model, "allowed": allowed_models},
            )

    # Allowed efforts & Default effort
    allowed_efforts = json.loads(row["allowed_efforts_json"])
    if "allowed_efforts" in ctx.args and ctx.args["allowed_efforts"] is not None:
        allowed_efforts = ctx.args["allowed_efforts"]
        if isinstance(allowed_efforts, str):
            allowed_efforts = [allowed_efforts]
        allowed_efforts = list(dict.fromkeys(allowed_efforts))

    default_effort = row["default_effort"]
    if "default_effort" in ctx.args and ctx.args["default_effort"] is not None:
        default_effort = ctx.args["default_effort"]

    # Blind review P2: validate the resolved pair, not only the changed field; narrowing
    # allowed_efforts must not leave a default outside it.
    if default_effort and allowed_efforts and default_effort not in allowed_efforts:
        raise MeshError(
            code=c.EFFORT_NOT_ALLOWED,
            exit_code=c.EXIT_USAGE,
            message=(f"Default effort '{default_effort}' is not in allowed efforts list "
                     f"(set --default-effort too)"),
            details={"default_effort": default_effort, "allowed": allowed_efforts},
        )

    # AGY model effort encoding rule (D17, §14.4)
    if row["adapter"] == "agy" and default_effort and default_model:
        for suffix in EFFORT_SUFFIXES:
            if default_model.endswith(suffix):
                raise EffortNotAllowed(
                    f"Model '{default_model}' already encodes reasoning effort; cannot specify effort '{default_effort}'",
                    details={"reason": "model_encodes_effort", "model": default_model, "effort": default_effort},
                )

    can_lead = row["can_lead"]
    if "can_lead" in ctx.args and ctx.args["can_lead"] is not None:
        can_lead = 1 if ctx.args["can_lead"] else 0
        if not can_lead and ctx.conn.execute(
                "SELECT 1 FROM lead_assignments WHERE agent_id=? AND ended_at IS NULL",
                (row["id"],)).fetchone():
            raise MeshError(code=c.INVALID_TRANSITION, exit_code=c.EXIT_CONFLICT,
                            message="Hand over active projects before revoking can_lead",
                            details={"reason": "active_lead"})

    capabilities = json.loads(row["capabilities_json"])
    if "capabilities" in ctx.args and ctx.args["capabilities"] is not None:
        capabilities = ctx.args["capabilities"]
        if isinstance(capabilities, str):
            capabilities = [capabilities]
        for cap in capabilities:
            if cap not in VALID_CAPABILITIES:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Invalid capability '{cap}': must be in {sorted(VALID_CAPABILITIES)}",
                    details={"param": "capabilities", "value": cap},
                )
        capabilities = list(dict.fromkeys(capabilities))
    if ctx.args.get("no_intake"):
        capabilities = [cap for cap in capabilities if cap != "intake"]

    ack_raw = ctx.args.get("ack_timeout")
    ack_change = ack_raw is not None
    ack_timeout_s = _ack_timeout_arg(ctx, row["delivery"]) if ack_change else get_agent_ack_timeout_s(ctx.conn, row["id"])

    default_profile = row["default_profile"]
    if "default_profile" in ctx.args and ctx.args["default_profile"] is not None:
        default_profile = ctx.args["default_profile"]
        if default_profile not in VALID_PROFILES:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid default_profile '{default_profile}'",
                details={"param": "default_profile"},
            )

    max_unapproved_profile = row["max_unapproved_profile"]
    if "max_unapproved_profile" in ctx.args and ctx.args["max_unapproved_profile"] is not None:
        max_unapproved_profile = ctx.args["max_unapproved_profile"]
        if max_unapproved_profile not in VALID_PROFILES:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid max_unapproved_profile '{max_unapproved_profile}'",
                details={"param": "max_unapproved_profile"},
            )

    now = now_iso_utc()
    new_version = row["version"] + 1

    ctx.conn.execute(
        """UPDATE agents
           SET default_model = ?,
               allowed_models_json = ?,
               default_effort = ?,
               allowed_efforts_json = ?,
               max_concurrency = ?,
               can_lead = ?,
               capabilities_json = ?,
               default_profile = ?,
               max_unapproved_profile = ?,
               version = ?,
               updated_at = ?
           WHERE id = ?;""",
        (
            default_model,
            json.dumps(allowed_models),
            default_effort,
            json.dumps(allowed_efforts),
            max_concurrency,
            can_lead,
            json.dumps(capabilities),
            default_profile,
            max_unapproved_profile,
            new_version,
            now,
            row["id"],
        ),
    )

    if ack_change:
        set_agent_ack_timeout_s(ctx.conn, row["id"], ack_timeout_s, now, ctx.principal["id"])

    insert_event(
        ctx.conn,
        type_="agent.updated",
        actor_id=ctx.principal["id"],
        actor_kind="user",
        via="direct",
        request_id=ctx.request_id,
        payload={"agent_id": row["id"], "name": name, "version": new_version},
        k_user=ctx.k_user,
    )

    data = {
        "id": row["id"],
        "name": name,
        "adapter": row["adapter"],
        "delivery": row["delivery"],
        "can_lead": bool(can_lead),
        "capabilities": capabilities,
        "default_model": default_model,
        "allowed_models": allowed_models,
        "default_effort": default_effort,
        "allowed_efforts": allowed_efforts,
        "max_concurrency": max_concurrency,
        "default_profile": default_profile,
        "max_unapproved_profile": max_unapproved_profile,
        **_ack_timeout_view(ctx.conn, ack_timeout_s, row["delivery"]),
        "version": new_version,
        "updated_at": now,
    }
    warnings: List[str] = []
    if ack_change and row["delivery"] == "inbox":
        from mesh.daemon.scheduler import get_inbox_ack_timeout_s, offers_kept_by_window

        new_window = ack_timeout_s if ack_timeout_s is not None else get_inbox_ack_timeout_s(ctx.conn)
        kept = offers_kept_by_window(ctx.conn, row["id"], new_window)
        if kept:
            data["offers_keeping_window"] = kept
            keep = [k["task_id"] for k in kept if k["kept"]]
            lose = [k["task_id"] for k in kept if not k["kept"]]
            if keep:
                warnings.append(
                    f"{len(keep)} task(s) already offered to {name} keep the ack window they were offered with "
                    f"(a new window applies to new offers only): " + ", ".join(keep[:10]))
            if lose:
                warnings.append(
                    f"{len(lose)} task(s) offered to {name} before ack windows were recorded will fail no_response "
                    f"on the next scheduler tick: " + ", ".join(lose[:10]))
    return HandlerResult(data=data, warnings=warnings)


def handle_agent_show(ctx: HandlerContext) -> HandlerResult:
    """Show details and capability status for an agent (leads get the roster view)."""
    roster = _roster_view(ctx, "agent.show")
    name = ctx.args.get("name")
    if not name:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Agent 'name' is required",
            details={"param": "name"},
        )

    cur = ctx.conn.execute(
        """SELECT p.id, p.name, p.disabled_at, a.*
           FROM principals p
           JOIN agents a ON p.id = a.id
           WHERE p.name = ?;""",
        (name,),
    )
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Agent '{name}' not found",
            details={"name": name},
        )

    data = {
        "id": row["id"],
        "name": row["name"],
        "adapter": row["adapter"],
        "delivery": row["delivery"],
        "can_lead": bool(row["can_lead"]),
        "capabilities": json.loads(row["capabilities_json"]),
        "default_model": row["default_model"],
        "allowed_models": json.loads(row["allowed_models_json"]),
        "default_effort": row["default_effort"],
        "allowed_efforts": json.loads(row["allowed_efforts_json"]),
        "max_concurrency": row["max_concurrency"],
        "default_profile": row["default_profile"],
        "max_unapproved_profile": row["max_unapproved_profile"],
        "options": json.loads(row["adapter_options_json"]),
        **_ack_timeout_view(ctx.conn, get_agent_ack_timeout_s(ctx.conn, row["id"]), row["delivery"]),
        "version": row["version"],
        "disabled": row["disabled_at"] is not None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if roster:
        data = {k: data[k] for k in ROSTER_FIELDS if k in data}
    return HandlerResult(data=data)


def handle_agent_list(ctx: HandlerContext) -> HandlerResult:
    """List all registered agents (leads get the roster view)."""
    roster = _roster_view(ctx, "agent.list")
    cur = ctx.conn.execute(
        """SELECT p.id, p.name, p.disabled_at, a.adapter, a.delivery, a.can_lead,
                  a.default_model, a.max_concurrency, a.default_profile,
                  a.max_unapproved_profile, a.version, a.created_at,
                  a.allowed_models_json, a.default_effort, a.allowed_efforts_json
           FROM principals p
           JOIN agents a ON p.id = a.id
           ORDER BY p.name ASC;"""
    )
    items = []
    for row in cur.fetchall():
        items.append({
            "id": row["id"],
            "name": row["name"],
            "adapter": row["adapter"],
            "delivery": row["delivery"],
            "can_lead": bool(row["can_lead"]),
            "default_model": row["default_model"],
            "max_concurrency": row["max_concurrency"],
            "default_profile": row["default_profile"],
            "max_unapproved_profile": row["max_unapproved_profile"],
            "version": row["version"],
            "disabled": row["disabled_at"] is not None,
            "created_at": row["created_at"],
        })
        items[-1].update(_ack_timeout_view(
            ctx.conn, get_agent_ack_timeout_s(ctx.conn, row["id"]), row["delivery"]))
        if roster:
            item = items[-1]
            item["allowed_models"] = json.loads(row["allowed_models_json"] or "[]")
            item["default_effort"] = row["default_effort"]
            item["allowed_efforts"] = json.loads(row["allowed_efforts_json"] or "[]")
            items[-1] = {k: item[k] for k in ROSTER_FIELDS if k in item}
    return HandlerResult(data={"agents": items, "items": items})


def handle_agent_disable(ctx: HandlerContext) -> HandlerResult:
    """Disable an agent. Direct user only."""
    _check_direct_user_only(ctx, "agent.disable")

    name = ctx.args.get("name")
    cur = ctx.conn.execute("SELECT id FROM principals WHERE name = ? AND kind = 'agent';", (name,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Agent '{name}' not found",
            details={"name": name},
        )

    now = now_iso_utc()
    ctx.conn.execute("UPDATE principals SET disabled_at = ? WHERE id = ?;", (now, row["id"]))
    insert_event(
        ctx.conn,
        type_="agent.disabled",
        actor_id=ctx.principal["id"],
        actor_kind="user",
        via="direct",
        request_id=ctx.request_id,
        payload={"agent_id": row["id"], "name": name},
        k_user=ctx.k_user,
    )
    return HandlerResult(data={"name": name, "disabled": True, "disabled_at": now})


def handle_agent_enable(ctx: HandlerContext) -> HandlerResult:
    """Re-enable a disabled agent. Direct user only."""
    _check_direct_user_only(ctx, "agent.enable")

    name = ctx.args.get("name")
    cur = ctx.conn.execute("SELECT id FROM principals WHERE name = ? AND kind = 'agent';", (name,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Agent '{name}' not found",
            details={"name": name},
        )

    ctx.conn.execute("UPDATE principals SET disabled_at = NULL WHERE id = ?;", (row["id"],))
    insert_event(
        ctx.conn,
        type_="agent.enabled",
        actor_id=ctx.principal["id"],
        actor_kind="user",
        via="direct",
        request_id=ctx.request_id,
        payload={"agent_id": row["id"], "name": name},
        k_user=ctx.k_user,
    )
    return HandlerResult(data={"name": name, "disabled": False})


def handle_agent_token_issue(ctx: HandlerContext) -> HandlerResult:
    """Issue a new agent token. Direct user only (ACC: C9, G5)."""
    _check_direct_user_only(ctx, "agent.token.issue")

    name = ctx.args.get("name")
    cur = ctx.conn.execute("SELECT id, disabled_at FROM principals WHERE name = ? AND kind = 'agent';", (name,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Agent '{name}' not found",
            details={"name": name},
        )

    if row["disabled_at"] is not None:
        raise MeshError(
            code=c.TOKEN_INVALID,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{name}' is disabled; cannot issue token",
            details={"name": name},
        )

    token, lookup, secret_hash = generate_agent_token()
    cred_id = new_id("cred")
    now = now_iso_utc()

    expires_at = None
    expires_in_raw = ctx.args.get("expires_in")
    if expires_in_raw:
        dur_s = parse_duration(expires_in_raw)
        import time
        expires_at = now_iso_utc(time.time() + dur_s)

    ctx.conn.execute(
        """INSERT INTO credentials (
               id, principal_id, kind, lookup, secret_hash, scope_json,
               created_at, expires_at, revoked_at
           ) VALUES (?, ?, 'agent_token', ?, ?, '{}', ?, ?, NULL);""",
        (cred_id, row["id"], lookup, secret_hash, now, expires_at),
    )

    insert_event(
        ctx.conn,
        type_="agent.token_issued",
        actor_id=ctx.principal["id"],
        actor_kind="user",
        via="direct",
        credential_id=cred_id,
        request_id=ctx.request_id,
        payload={"agent_id": row["id"], "name": name, "lookup": lookup},
        k_user=ctx.k_user,
    )

    data = {
        "token": token,
        "lookup": lookup,
        "credential_id": cred_id,
        "expires_at": expires_at,
    }
    return HandlerResult(data=data)


def handle_agent_token_list(ctx: HandlerContext) -> HandlerResult:
    """List issued credentials for an agent. Direct user only (ACC: C9)."""
    _check_direct_user_only(ctx, "agent.token.list")

    name = ctx.args.get("name")
    cur = ctx.conn.execute("SELECT id FROM principals WHERE name = ? AND kind = 'agent';", (name,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Agent '{name}' not found",
            details={"name": name},
        )

    ccur = ctx.conn.execute(
        """SELECT id, lookup, created_at, expires_at, revoked_at
           FROM credentials
           WHERE principal_id = ? AND kind = 'agent_token'
           ORDER BY created_at ASC;""",
        (row["id"],),
    )
    items = []
    for crow in ccur.fetchall():
        items.append({
            "id": crow["id"],
            "lookup": crow["lookup"],
            "created_at": crow["created_at"],
            "expires_at": crow["expires_at"],
            "revoked_at": crow["revoked_at"],
        })
    return HandlerResult(data={"credentials": items, "items": items})


def handle_agent_token_revoke(ctx: HandlerContext) -> HandlerResult:
    """Revoke an agent credential. Direct user only (ACC: G18)."""
    _check_direct_user_only(ctx, "agent.token.revoke")

    cred_arg = ctx.args.get("credential")
    if not cred_arg:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Credential identifier is required",
            details={"param": "credential"},
        )

    cur = ctx.conn.execute(
        """SELECT id, principal_id, revoked_at
           FROM credentials
           WHERE id = ? OR lookup = ?;""",
        (cred_arg, cred_arg),
    )
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Credential '{cred_arg}' not found",
            details={"credential": cred_arg},
        )

    now = now_iso_utc()
    if row["revoked_at"] is None:
        ctx.conn.execute("UPDATE credentials SET revoked_at = ? WHERE id = ?;", (now, row["id"]))
        insert_event(
            ctx.conn,
            type_="agent.token_revoked",
            actor_id=ctx.principal["id"],
            actor_kind="user",
            via="direct",
            credential_id=row["id"],
            request_id=ctx.request_id,
            payload={"credential_id": row["id"], "principal_id": row["principal_id"]},
            k_user=ctx.k_user,
        )

    return HandlerResult(data={"credential_id": row["id"], "revoked": True, "revoked_at": now})
