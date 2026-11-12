"""Agent resource commands: create, update, list, show, disable, enable, token."""

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
        epilog="Examples:\n  mesh agent create --name worker1 --adapter fake --allowed-model m-a --default-model m-a --max-concurrency 1 --user\n  mesh agent create --name grok --adapter none --delivery inbox --capability relay --capability intake --max-concurrency 1 --user\n",
    )
    create_parser.add_argument("--name", required=True, help="Unique agent identifier")
    create_parser.add_argument("--adapter", required=True, help="Adapter id (droid, agy, kiro, vibe, hermes, openclaw, none, fake)")
    create_parser.add_argument("--delivery", choices=["launch", "inbox"], default="launch", help="Delivery mode (launch or inbox)")
    create_parser.add_argument("--default-model", help="Default model id")
    create_parser.add_argument("--allowed-model", action="append", default=[], help="Model id allowed for this agent (repeatable)")
    create_parser.add_argument("--default-effort", help="Default reasoning effort level")
    create_parser.add_argument("--allowed-effort", action="append", default=[], help="Reasoning effort level allowed (repeatable)")
    create_parser.add_argument("--max-concurrency", help="Maximum concurrent runs allowed for this agent")
    create_parser.add_argument("--can-lead", action="store_true", default=False, help="Whether this agent is eligible to lead projects")
    create_parser.add_argument("--capability", action="append", choices=["intake", "relay"], default=None, help="Special capability (intake, relay)")
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
        epilog="Examples:\n  mesh agent update worker1 --max-concurrency 3 --user\n  mesh agent update worker1 --allowed-model m-b --user\n",
    )
    update_parser.add_argument("name", help="Name of agent to update")
    update_parser.add_argument("--adapter", help="Adapter id")
    update_parser.add_argument("--delivery", choices=["launch", "inbox"], help="Delivery mode")
    update_parser.add_argument("--default-model", help="Default model id")
    update_parser.add_argument("--allowed-model", action="append", default=None, help="Allowed models (replaces existing list)")
    update_parser.add_argument("--default-effort", help="Default reasoning effort")
    update_parser.add_argument("--allowed-effort", action="append", default=None, help="Allowed reasoning efforts (replaces list)")
    update_parser.add_argument("--max-concurrency", help="Maximum concurrency limit")
    update_parser.add_argument("--can-lead", action="store_true", default=None, help="Lead eligibility")
    update_parser.add_argument("--capability", action="append", choices=["intake", "relay"], default=None, help="Capabilities")
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
        epilog="Examples:\n  mesh agent token issue lead1 --user\n  mesh agent token issue lead1 --expires-in 24h --user\n",
    )
    issue_parser.add_argument("name", help="Agent identifier")
    issue_parser.add_argument("--expires-in", help="Token expiration duration (e.g. 24h, 7d)")
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
        "binary": args.binary,
        "options": options,
        "strict_models": args.strict_models,
    }
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


def handle_agent_token_issue(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.token.issue", {"name": args.name, "expires_in": args.expires_in})
    return resp.get("data", {})


def handle_agent_token_revoke(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.token.revoke", {"credential": args.credential})
    return resp.get("data", {})


def handle_agent_token_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "agent.token.list", {"name": args.name})
    return resp.get("data", {})
