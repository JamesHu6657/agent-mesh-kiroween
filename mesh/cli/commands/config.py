"""Config resource commands: list, get, set."""

import argparse
import json
from typing import Any, Dict

import mesh.constants as c
from mesh.errors import MeshError
from mesh.client import MeshClient


def register_config_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    config_parser = subparsers.add_parser(
        "config",
        parents=[parent_parser],
        help="Inspect and configure Mesh runtime settings",
        description="Inspect and configure Mesh runtime settings.",
        epilog="Examples:\n  mesh config list --user\n  mesh config get task.default_timeout_s\n  mesh config set task.default_timeout_s 600 --user\n",
    )
    sub = config_parser.add_subparsers(dest="verb", metavar="<verb>")

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List all configuration settings, current values, and defaults",
        description="List all configuration settings, current values, and defaults.",
        epilog="Examples:\n  mesh config list\n  mesh config list --user\n",
    )
    list_parser.set_defaults(func=handle_config_list)

    # get
    get_parser = sub.add_parser(
        "get",
        parents=[parent_parser],
        help="Get the current value of a configuration setting",
        description="Get the current value of a configuration setting.",
        epilog="Examples:\n  mesh config get lead.default_agent\n  mesh config get scheduler.max_total_runs\n",
    )
    get_parser.add_argument("key", help="Configuration key name")
    get_parser.set_defaults(func=handle_config_get)

    # set
    set_parser = sub.add_parser(
        "set",
        parents=[parent_parser],
        help="Update the value of a configuration setting (JSON encoded)",
        description="Update the value of a configuration setting (JSON encoded).",
        epilog=("Examples:\n  mesh config set task.default_timeout_s 600 --user\n  mesh config set lead.default_agent '\"lead1\"' --user\n"
                "  mesh config set lead.default_agent lead1 --user\n"
                "  mesh config set notify.command '[\"/usr/local/bin/notify\", \"--quiet\"]' --user\n"
                "\n"
                "VALUE is JSON (600, null, true, \"text\", [\"a\", \"b\"]). For settings whose value is a\n"
                "string (lead.default_agent, lead.profile, workspaces.root, adapter.evidence_mode,\n"
                "approval.mode, approval.policy.*) a bare word that is not JSON is taken as the\n"
                "string itself; null, numbers and quoted strings keep their JSON meaning.\n"),
    )
    set_parser.add_argument("key", help="Configuration key name")
    set_parser.add_argument("value", help="New value as JSON (a bare word is accepted for string settings)")
    set_parser.set_defaults(func=handle_config_set)


def handle_config_list(args: argparse.Namespace) -> Dict[str, Any]:
    """List all settings."""
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "config.list")
    return resp.get("data", {})


def handle_config_get(args: argparse.Namespace) -> Dict[str, Any]:
    """Get single setting."""
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "config.get", {"key": args.key})
    return resp.get("data", {})


def handle_config_set(args: argparse.Namespace) -> Dict[str, Any]:
    """Set setting value."""
    from mesh.core.settings import string_setting

    raw_val = args.value
    if "value" in getattr(args, "_from_json", set()):
        # Linear full review C5: --input already decoded a native JSON value.
        client = MeshClient(home=args.home)
        return client.execute_cmd(args, "config.set", {"key": args.key, "value": raw_val}).get("data", {})
    try:
        parsed_val = json.loads(raw_val)
    except json.JSONDecodeError as exc:
        if string_setting(args.key):
            # String settings take a bare word: `config set lead.default_agent lead1`.
            # Anything that parses as JSON (null, numbers, "quoted") keeps its JSON meaning.
            parsed_val = raw_val
        else:
            quoted = json.dumps(raw_val)
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=(f"Value for '{args.key}' is not valid JSON ({exc.msg}): {raw_val!r}. "
                         f"The value is JSON: a number like 600, null, or an array like '[\"a\"]'; "
                         f"a string must be quoted, e.g. '{quoted}'"),
                details={"key": args.key, "got": raw_val, "expected": "JSON value", "quoted_form": quoted},
            ) from exc

    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "config.set", {"key": args.key, "value": parsed_val})
    return resp.get("data", {})
