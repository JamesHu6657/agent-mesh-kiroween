"""Agent resource commands: create, update, list, show, disable, enable, token."""

import os
import argparse
from typing import Any, Dict

from mesh.client import MeshClient
from mesh.core.policy import parse_cli_positive_int


def register_agent_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    agent_parser = subparsers.add_parser(
        "agent",
        parents=[parent_parser],
        help="Register and manage agent definitions and tokens",
        description="Register and manage agent definitions and tokens.",
        epilog="Examples:\n  mesh agent list\n  mesh agent show lead1\n  mesh agent create --name agy-worker --adapter agy --max-concurrency 2 --user\n",
    )
    sub = agent_parser.add_subparsers(dest="verb", metavar="<verb>")

    # create
    create_parser = sub.add_parser(
        "create",
        parents=[parent_parser],
        help="Register a new agent with adapter, model allowances, and concurrency limits",
        description="Register a new agent with adapter, model allowances, and concurrency limits.",
        epilog=("Examples:\n  mesh agent create --name worker1 --adapter fake --allowed-model m-a --default-model m-a --max-concurrency 1 --user\n  mesh agent create --name grok --adapter none --delivery inbox --capability relay --capability intake --max-concurrency 1 --user\n"
                "  mesh agent create --name mailbot --adapter none --delivery inbox --ack-timeout 2h --max-concurrency 1 --user\n"
                "\n"
                "Capabilities: an agent gets ['intake'] when no --capability is given. Each\n"
                "--capability replaces that default, so list intake too if the agent should keep\n"
                "submitting projects (a warning says so when it is dropped); --no-intake creates\n"
                "an agent without intake on purpose.\n"
                "For a new inbox worker, `mesh agent onboard` does create + token file in one step.\n"),
    )
    create_parser.add_argument("--name", required=True, help="Unique agent identifier")
    create_parser.add_argument("--adapter", required=True, help="Adapter id (droid, agy, kiro, vibe, hermes, openclaw, none, fake)")
    create_parser.add_argument("--delivery", choices=["launch", "inbox"], default="launch", help="Delivery mode (launch or inbox)")
    create_parser.add_argument("--default-model", help="Default model id")
    create_parser.add_argument("--allowed-model", action="append", default=[], help="Model id allowed for this agent (repeatable)")
    create_parser.add_argument("--default-effort", help="Default reasoning effort level")
    create_parser.add_argument("--allowed-effort", action="append", default=[], help="Reasoning effort level allowed (repeatable)")
    create_parser.add_argument("--max-concurrency", help="Maximum concurrent runs allowed for this agent (default: 1)")
    create_parser.add_argument("--can-lead", action="store_true", default=False, help="Whether this agent is eligible to lead projects")
    create_parser.add_argument("--capability", action="append", choices=["intake", "relay"], default=None,
                               help="Capability (intake, relay; repeatable). Default: intake. Giving any replaces the default")
    create_parser.add_argument("--no-intake", action="store_true", default=False,
                               help="Create the agent without the default intake capability")
    create_parser.add_argument("--ack-timeout", metavar="DURATION",
                               help="Inbox agents: time to `task start` an offered task before it fails "
                                    "no_response (e.g. 30m, 2h; default: the global inbox.ack_timeout_s)")
    create_parser.add_argument("--default-profile", help="Default permission profile (default: edit)")
    create_parser.add_argument("--max-unapproved-profile", help="Highest permission profile requiring no human approval (default: dev)")
    create_parser.add_argument("--binary", help="Explicit path to external CLI binary")
    create_parser.add_argument("--option", action="append", default=[], help="Adapter-specific option KEY=JSON (repeatable)")
    create_parser.add_argument("--strict-models", action="store_true", default=False, help="Reject models not in probed vendor list")
    create_parser.set_defaults(func=handle_agent_create)

    # update
    update_parser = sub.add_parser(
        "update",
        parents=[parent_parser],
        help="Update settings of an existing agent",
        description="Update settings of an existing agent.",
        epilog=("Examples:\n  mesh agent update worker1 --max-concurrency 3 --user\n  mesh agent update worker1 --allowed-model m-b --user\n"
                "  mesh agent update mailbot --ack-timeout 2h --user        # email-driven agent, slow to pick up\n"
                "  mesh agent update mailbot --ack-timeout default --user   # back to inbox.ack_timeout_s\n"),
    )
    update_parser.add_argument("name", help="Name of agent to update")
    update_parser.add_argument("--adapter", help="Adapter id")
    update_parser.add_argument("--delivery", choices=["launch", "inbox"], help="Delivery mode")
    update_parser.add_argument("--default-model", help="Default model id")
    update_parser.add_argument("--allowed-model", action="append", default=None, help="Allowed models (replaces existing list)")
    update_parser.add_argument("--default-effort", help="Default reasoning effort")
    update_parser.add_argument("--allowed-effort", action="append", default=None, help="Allowed reasoning efforts (replaces list)")
    update_parser.add_argument("--max-concurrency", help="Maximum concurrency limit")
    update_parser.add_argument("--can-lead", action=argparse.BooleanOptionalAction, default=None, help="Lead eligibility")
    update_parser.add_argument("--capability", action="append", choices=["intake", "relay"], default=None,
                               help="Capabilities (repeatable; replaces the agent's whole list)")
    update_parser.add_argument("--no-intake", action="store_true", default=False,
                               help="Remove the intake capability")
    update_parser.add_argument("--ack-timeout", metavar="DURATION",
                               help="Inbox agents: own ack window (e.g. 2h), or 'default' to use inbox.ack_timeout_s again")
    update_parser.add_argument("--default-profile", help="Default profile")
    update_parser.add_argument("--max-unapproved-profile", help="Max unapproved profile")
    update_parser.add_argument("--binary", help="Binary path")
    update_parser.add_argument("--option", action="append", default=None, help="Adapter options")
    update_parser.add_argument("--strict-models", action="store_true", default=None, help="Strict models enforcement")
    update_parser.set_defaults(func=handle_agent_update)

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List all registered agents",
        description="List all registered agents.",
        epilog="Examples:\n  mesh agent list\n  mesh agent list --format text\n",
    )
    list_parser.set_defaults(func=handle_agent_list)

    # show
    show_parser = sub.add_parser(
        "show",
        parents=[parent_parser],
        help="Show detailed configuration and capability status of an agent",
        description="Show detailed configuration and capability status of an agent.",
        epilog="Examples:\n  mesh agent show lead1\n  mesh agent show fx --user\n",
    )
    show_parser.add_argument("name", help="Agent identifier")
    show_parser.set_defaults(func=handle_agent_show)

    # disable
    disable_parser = sub.add_parser(
        "disable",
        parents=[parent_parser],
        help="Disable an agent from receiving new task assignments",
        description="Disable an agent from receiving new task assignments.",
        epilog="Examples:\n  mesh agent disable worker1 --user\n",
    )
    disable_parser.add_argument("name", help="Agent identifier")
    disable_parser.set_defaults(func=handle_agent_disable)

    # enable
    enable_parser = sub.add_parser(
        "enable",
        parents=[parent_parser],
        help="Re-enable a previously disabled agent",
        description="Re-enable a previously disabled agent.",
        epilog="Examples:\n  mesh agent enable worker1 --user\n",
    )
    enable_parser.add_argument("name", help="Agent identifier")
    enable_parser.set_defaults(func=handle_agent_enable)

    # onboard
    onboard_parser = sub.add_parser(
        "onboard",
        parents=[parent_parser],
        help="Create an inbox worker agent and its token file in one step",
        description=(
            "Create an inbox agent, issue its token into a new 0600 file, and print a worker\n"
            "cheat-sheet with the exact inbox/start/keepalive/submit commands for that token.\n"
            "Same as `agent create --delivery inbox` + `agent token issue --out`."
        ),
        epilog=(
            "Examples:\n"
            "  mesh agent onboard worker2 --out ~/.config/agent-mesh/worker2.token --user\n"
            "  mesh agent onboard mailbot --out ~/.config/agent-mesh/mailbot.token --ack-timeout 2h --user\n"
            "\n"
            "The token file is checked before anything is created (it must not exist unless\n"
            "--force, and must not be in a workspace, a repository or a shared directory).\n"
            "Hand the token file and the printed cheat-sheet to the worker.\n"
        ),
    )
    onboard_parser.add_argument("name", help="New agent name")
    onboard_parser.add_argument("--out", required=True, help="Token file to create (mode 0600)")
    onboard_parser.add_argument("--force", action="store_true", default=False, help="Replace an existing token file")
    onboard_parser.add_argument("--adapter", default="none", help="Adapter id (default: none, for agents outside Mesh)")
    onboard_parser.add_argument("--max-concurrency", default="1", help="Tasks the agent may run at once (default: 1)")
    onboard_parser.add_argument("--ack-timeout", metavar="DURATION",
                                help="Time to `task start` an offered task (e.g. 2h; default: inbox.ack_timeout_s)")
    onboard_parser.add_argument("--capability", action="append", choices=["intake", "relay"], default=None,
                                help="Capability (repeatable). Default: intake. Giving any replaces the default")
    onboard_parser.add_argument("--no-intake", action="store_true", default=False,
                                help="Create the agent without the default intake capability")
    onboard_parser.add_argument("--expires-in", help="Token expiration duration (e.g. 30d)")
    onboard_parser.set_defaults(func=handle_agent_onboard)

    # token subcommand
    token_parser = sub.add_parser(
        "token",
        parents=[parent_parser],
        help="Issue, list, and revoke agent authentication tokens",
        description="Issue, list, and revoke agent authentication tokens.",
        epilog="Examples:\n  mesh agent token issue lead1 --user\n  mesh agent token list lead1 --user\n  mesh agent token revoke cred_123 --user\n",
    )
    token_sub = token_parser.add_subparsers(dest="token_verb", metavar="<token-verb>")

    issue_parser = token_sub.add_parser(
        "issue",
        parents=[parent_parser],
        help="Issue a new bearer token for an agent (shown once)",
        description="Issue a new bearer token for an agent (shown once).",
        epilog=("Examples:\n  mesh agent token issue lead1 --user\n  mesh agent token issue lead1 --expires-in 24h --user\n"
                "  mesh agent token issue worker1 --out ~/.config/agent-mesh/worker1.token --user\n"
                "\n"
                "With --out the token is written to a new 0600 file and left out of the output.\n"
                "The file must not exist (use --force to replace it), and it may not be inside the\n"
                "Mesh home's runs/ or workspaces/, a configured workspace root, a project repository,\n"
                "a git work tree, or a directory other users can write to.\n"),
    )
    issue_parser.add_argument("name", help="Agent identifier")
    issue_parser.add_argument("--expires-in", help="Token expiration duration (e.g. 24h, 7d)")
    issue_parser.add_argument("--out", help="Write the token to this file (created mode 0600) instead of printing it")
    issue_parser.add_argument("--force", action="store_true", default=False,
                              help="With --out: replace an existing token file")
    issue_parser.set_defaults(func=handle_agent_token_issue)

    revoke_parser = token_sub.add_parser(
        "revoke",
        parents=[parent_parser],
        help="Revoke an issued agent credential",
        description="Revoke an issued agent credential.",
        epilog="Examples:\n  mesh agent token revoke cred_abc123 --user\n",
    )
    revoke_parser.add_argument("credential", help="Credential identifier (cred_...)")
    revoke_parser.set_defaults(func=handle_agent_token_revoke)

    token_list_parser = token_sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List all active credentials issued for an agent",
        description="List all active credentials issued for an agent.",
        epilog="Examples:\n  mesh agent token list lead1 --user\n",
    )
    token_list_parser.add_argument("name", help="Agent identifier")
    token_list_parser.set_defaults(func=handle_agent_token_list)


def handle_agent_create(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)

    max_concurrency = args.max_concurrency
    if max_concurrency is not None:
        if "max_concurrency" not in getattr(args, "_from_json", set()):
            max_concurrency = parse_cli_positive_int(max_concurrency, "max_concurrency")

    allowed_models = getattr(args, "allowed_models", None)
    if allowed_models is None:
        allowed_models = args.allowed_model

    allowed_efforts = getattr(args, "allowed_efforts", None)
    if allowed_efforts is None:
        allowed_efforts = args.allowed_effort

    capabilities = getattr(args, "capabilities", None)
    if capabilities is None:
        capabilities = args.capability

    options = getattr(args, "options", None)
    if options is None:
        options = args.option

    payload = {
        "name": args.name,
        "adapter": args.adapter,
        "delivery": args.delivery,
        "default_model": args.default_model,
        "allowed_models": allowed_models,
        "default_effort": args.default_effort,
        "allowed_efforts": allowed_efforts,
        "max_concurrency": max_concurrency,
        "can_lead": args.can_lead,
        "capabilities": capabilities,
        "default_profile": args.default_profile or "edit",
        "max_unapproved_profile": args.max_unapproved_profile or "dev",
        "binary": os.path.abspath(os.path.expanduser(args.binary)) if args.binary else args.binary,
        "options": options,
        "strict_models": args.strict_models,
        "no_intake": bool(getattr(args, "no_intake", False)),
        "ack_timeout": getattr(args, "ack_timeout", None),
    }
    if max_concurrency is None and "max_concurrency" not in getattr(args, "_from_json", set()):
        del payload["max_concurrency"]  # not given: meshd uses 1 (an explicit null is still refused)
    resp = client.execute_cmd(args, "agent.create", payload)
    return resp.get("data", {})


def handle_agent_update(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)

    max_concurrency = args.max_concurrency
    if max_concurrency is not None:
        if "max_concurrency" not in getattr(args, "_from_json", set()):
            max_concurrency = parse_cli_positive_int(max_concurrency, "max_concurrency")

    allowed_models = getattr(args, "allowed_models", None)
    if allowed_models is None:
        allowed_models = args.allowed_model

    allowed_efforts = getattr(args, "allowed_efforts", None)
    if allowed_efforts is None:
        allowed_efforts = args.allowed_effort

    capabilities = getattr(args, "capabilities", None)
    if capabilities is None:
        capabilities = args.capability

    options = getattr(args, "options", None)
    if options is None:
        options = args.option

    payload = {
        "name": args.name,
        "adapter": args.adapter,
        "delivery": args.delivery,
        "default_model": args.default_model,
        "allowed_models": allowed_models,
        "default_effort": args.default_effort,
        "allowed_efforts": allowed_efforts,
        "max_concurrency": max_concurrency,
        "can_lead": args.can_lead,
        "capabilities": capabilities,
        "default_profile": args.default_profile,
        "max_unapproved_profile": args.max_unapproved_profile,
        "binary": args.binary,
        "options": options,
        "strict_models": args.strict_models,
        "if_version": getattr(args, "if_version", None),
        "no_intake": bool(getattr(args, "no_intake", False)),
        "ack_timeout": getattr(args, "ack_timeout", None),
    }
    resp = client.execute_cmd(args, "agent.update", payload)
    return resp.get("data", {})


def handle_agent_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.list")
    return resp.get("data", {})


def handle_agent_show(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.show", {"name": args.name})
    return resp.get("data", {})


def handle_agent_disable(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.disable", {"name": args.name})
    return resp.get("data", {})


def handle_agent_enable(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.enable", {"name": args.name})
    return resp.get("data", {})


def _check_out_path(home: str, out: str, force: bool, prefix: str = "mesh agent token issue --out") -> str:
    """Validate ``--out`` before any token is issued; returns the absolute path."""
    import os
    import stat as stat_mod

    import mesh.constants as c
    from mesh.cli.commands.setup import _check_token_destination
    from mesh.errors import MeshError

    if not str(out).strip():
        # PR #37 #3: `--out ""` must never fall back to printing the token.
        raise MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message=f"{prefix}: the token file path is empty; give a file path",
                        details={"param": "out"})
    path = os.path.abspath(os.path.expanduser(out))

    def usage(msg: str) -> MeshError:
        return MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message=f"{prefix}: {msg}",
                         details={"param": "out", "token_file": path})

    if out.endswith(os.sep) or os.path.isdir(path):
        raise usage(f"{path} is a directory; give a file path")
    if os.path.lexists(path) and not os.path.islink(path) and not force:
        raise usage(f"{path} already exists; use --force to replace it")
    # Same location guards as `mesh setup` (and the --token-file read side): not under
    # runs/, workspaces/, a configured workspace root, a project repo or a git work tree;
    # never through a symlink; never over a non-regular file.
    _check_token_destination(home, path, prefix=prefix)
    tdir = os.path.dirname(path)
    if os.path.isdir(tdir):
        st = os.stat(tdir)
        if hasattr(os, "geteuid") and st.st_uid not in (os.geteuid(), 0):
            raise usage(f"directory {tdir} is owned by another user")
        if st.st_mode & 0o022 and not st.st_mode & stat_mod.S_ISVTX:
            raise usage(f"directory {tdir} is writable by other users (mode {oct(stat_mod.S_IMODE(st.st_mode))}); "
                        "choose a private directory such as ~/.config/agent-mesh")
    return path


def _write_out(path: str, token: str, force: bool) -> None:
    """Write the token file with mode 0600. Without --force the file is created with
    O_EXCL (never overwrites, never follows a symlink); with --force it is replaced
    atomically by a rename."""
    import os

    if force:
        from mesh.cli.commands.setup import _write_token
        _write_token(path, token)
        return
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def handle_agent_token_issue(args: argparse.Namespace) -> Dict[str, Any]:
    out = getattr(args, "out", None)
    force = bool(getattr(args, "force", False))
    if force and out is None:
        import mesh.constants as c
        from mesh.errors import MeshError
        raise MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message="--force only applies together with --out", details={"param": "force"})
    path = _check_out_path(args.home, out, force) if out is not None else None
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.token.issue", {"name": args.name, "expires_in": args.expires_in})
    data = resp.get("data", {})
    if path is None or not isinstance(data, dict) or "token" not in data:
        return data  # no --out, or nothing issued
    if getattr(args, "dry_run", False):
        data.pop("token", None)  # a dry run issues nothing, so nothing is written
        data["token_file"] = path
        data["written"] = False
        return data
    if getattr(args, "_idempotent_replay", False):
        # PR #37 #2: a replay carries no usable token (the original is never stored), so
        # nothing is written: an existing token file is never replaced by a placeholder.
        import mesh.constants as c
        from mesh.errors import MeshError
        cred = data.get("credential_id")
        raise MeshError(
            code=c.IDEMPOTENCY_KEY_REUSED, exit_code=c.EXIT_CONFLICT,
            message=(f"this idempotency key already issued token {cred}; a replay cannot return the "
                     f"token again, so {path} was not written. If the first call wrote it, it is there; "
                     f"otherwise revoke {cred} (`mesh agent token revoke {cred} --user`) and issue a new "
                     f"one with a new idempotency key"),
            details={"credential_id": cred, "token_file": path, "written": False, "replay": True},
        )
    token = data.pop("token")
    try:
        _write_out(path, token, force)
    except OSError as exc:
        import mesh.constants as c
        from mesh.errors import MeshError
        cred = data.get("credential_id")
        raise MeshError(
            code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
            message=(f"token {cred} was issued but could not be written to {path} ({exc}); "
                     f"revoke it with `mesh agent token revoke {cred} --user` and try again"),
            details={"credential_id": cred, "token_file": path},
        )
    data["token_file"] = path
    return data


def worker_cheat_sheet(name: str, token_file: str, ack_timeout_s: Any = None, home: Any = None) -> str:
    """The commands an inbox worker runs, with its token path filled in."""
    import shlex

    h = f" --home {shlex.quote(home)}" if home else ""
    tf = f"--token-file {shlex.quote(token_file)}"
    rf = "--run <run_id> --fence <fence>"
    lines = [
        f"Worker cheat-sheet for {name}",
        f"  mesh{h} whoami {tf}  # check the token",
        f"  mesh{h} inbox list {tf}  # offered tasks (task.offered)",
        f"  mesh{h} task show <tsk> {tf}  # read the spec",
        f"  mesh{h} task start <tsk> {tf}  # -> run_id, fence, session, heartbeat_every_s",
        "  export MESH_SESSION=<session>  # from task start; keepalive/progress/submit/fail must send it",
        f"  mesh{h} task keepalive <tsk> {rf} {tf} > keepalive.log 2>&1 &  # keeps the lease alive",
        f"  mesh{h} task progress <tsk> {rf} --message \"...\" {tf}",
        f"  mesh{h} task submit <tsk> {rf} --summary \"...\" --deliverable result.json {tf}",
        f"  mesh{h} task fail <tsk> {rf} --reason \"...\" {tf}  # if it cannot be done",
    ]
    if ack_timeout_s:
        lines.append(f"Start an offered task within {ack_timeout_s}s or it fails no_response.")
    lines.append("keepalive stops by itself after submit/fail; exit code 9 means the lease was lost.")
    lines.append("A run belongs to the shell session that started it (MESH_SESSION); another session "
                 "using this token is refused.")
    return "\n".join(lines)


def handle_agent_onboard(args: argparse.Namespace) -> Dict[str, Any]:
    """agent create (inbox) + token issue --out + cheat-sheet."""
    import mesh.constants as c
    from mesh.errors import MeshError

    force = bool(getattr(args, "force", False))
    path = _check_out_path(args.home, args.out, force, prefix="mesh agent onboard --out")
    max_concurrency = args.max_concurrency
    if "max_concurrency" not in getattr(args, "_from_json", set()):
        max_concurrency = parse_cli_positive_int(max_concurrency, "max_concurrency")
    client = MeshClient(home=args.home)
    payload = {
        "name": args.name,
        "adapter": args.adapter,
        "delivery": "inbox",
        "max_concurrency": max_concurrency,
        "capabilities": args.capability,
        "no_intake": bool(args.no_intake),
        "ack_timeout": args.ack_timeout,
        "allowed_models": [],
        "allowed_efforts": [],
        "default_profile": "edit",
        "max_unapproved_profile": "dev",
        "options": [],
    }
    created = client.execute_cmd(args, "agent.create", payload).get("data", {})
    if getattr(args, "dry_run", False):
        return {"agent": created, "token_file": path, "written": False,
                "cheat_sheet": worker_cheat_sheet(args.name, path, created.get("ack_timeout_s"), args.home)}
    try:
        import copy
        issue_args = copy.copy(args)  # keeps the resolved auth (one passphrase prompt)
        # A second command must not replay the create; a retried onboard with the same key
        # must replay the issue too instead of minting another token (Linear full review C3).
        key = getattr(args, "idempotency_key", None)
        if key:
            import hashlib
            # Bounded: the daemon caps keys at 128 characters (PR #39 review).
            issue_args.idempotency_key = (f"{key}:token" if len(key) <= 122
                                          else "onboard-token:" + hashlib.sha256(key.encode("utf-8")).hexdigest())
        else:
            issue_args.idempotency_key = None
        issued = client.execute_cmd(issue_args, "agent.token.issue",
                                    {"name": args.name, "expires_in": args.expires_in}).get("data", {})
    except MeshError as exc:
        raise MeshError(exc.code, exit_code=exc.exit_code,
                        message=(f"agent {args.name} was created but no token was issued ({exc.message}); "
                                 f"issue one with `mesh agent token issue {args.name} --out {path} --user`"),
                        details={**(exc.details or {}), "agent_id": created.get("id")})
    token = issued.pop("token", None)
    cred = issued.get("credential_id")
    if isinstance(token, str) and token.startswith("[REDACTED"):
        # Replay of an earlier onboard: the token was shown (and written) once already.
        # PR #39 review round 2: the file must hold THIS credential's token (its lookup part),
        # not an older one that a failed --force replace left behind.
        held = ""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                held = fh.read().strip()
        except OSError:
            pass
        parts = held.split("_")
        lookup = issued.get("lookup")
        genuine = bool(lookup and len(parts) >= 4 and parts[2] == lookup)
        if genuine:
            # PR #39 review round 3: the whole token must authenticate as this agent, not only
            # carry the right lookup (a truncated or edited secret is not a usable token file).
            try:
                who_args = copy.copy(args)
                who_args.idempotency_key = None
                who = client.execute_cmd(who_args, "whoami", {},
                                         auth_override={"type": "token", "token": held}).get("data", {})
                genuine = who.get("name") == args.name
            except MeshError:
                genuine = False
        if not genuine:
            # The first attempt could not write it, and a replay cannot show it again.
            raise MeshError(
                code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                message=(f"agent {args.name} and token {cred} were created by an earlier onboard with this key, "
                         f"but the token file {path} does not hold that token and it cannot be shown again; "
                         f"revoke it with `mesh agent token revoke {cred} --user` and run "
                         f"`mesh agent token issue {args.name} --out {path} --user`"),
                details={"agent_id": created.get("id"), "credential_id": cred, "token_file": path},
            )
        return {
            "agent": {k: created.get(k) for k in ("id", "name", "adapter", "delivery", "max_concurrency",
                                                   "capabilities", "ack_timeout_s", "ack_timeout_source")},
            "credential_id": cred, "expires_at": issued.get("expires_at"), "token_file": path,
            "written": False, "replayed": True,
            "cheat_sheet": worker_cheat_sheet(args.name, path, created.get("ack_timeout_s"), args.home),
        }
    try:
        if token is None:
            raise OSError("no token in the response")
        _write_out(path, token, force)
    except OSError as exc:
        raise MeshError(
            code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
            message=(f"agent {args.name} was created and token {cred} issued, but it could not be written to "
                     f"{path} ({exc}); revoke it with `mesh agent token revoke {cred} --user` and run "
                     f"`mesh agent token issue {args.name} --out {path} --user`"),
            details={"agent_id": created.get("id"), "credential_id": cred, "token_file": path},
        )
    return {
        "agent": {k: created.get(k) for k in ("id", "name", "adapter", "delivery", "max_concurrency",
                                               "capabilities", "ack_timeout_s", "ack_timeout_source")},
        "credential_id": cred,
        "expires_at": issued.get("expires_at"),
        "token_file": path,
        "cheat_sheet": worker_cheat_sheet(args.name, path, created.get("ack_timeout_s"), args.home),
    }


def handle_agent_token_revoke(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.token.revoke", {"credential": args.credential})
    return resp.get("data", {})


def handle_agent_token_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.token.list", {"name": args.name})
    return resp.get("data", {})
