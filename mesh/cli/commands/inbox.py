"""Inbox resource commands: list, wait, ack."""

import argparse
from typing import Any, Dict

from mesh.client import MeshClient
import mesh.constants as c
from mesh.errors import MeshError, NotImplementedInM1Error


def register_inbox_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    inbox_parser = subparsers.add_parser(
        "inbox",
        parents=[parent_parser],
        help="Manage agent and user notification queues (long-polling and acks)",
        description="Manage agent and user notification queues (long-polling and acks).",
        epilog="Examples:\n  mesh inbox list\n  mesh inbox wait --timeout 30s\n  mesh inbox ack msg_1 msg_2\n",
    )
    sub = inbox_parser.add_subparsers(dest="verb", metavar="<verb>")

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List pending messages in the calling principal's inbox",
        description="List pending messages in the calling principal's inbox.",
        epilog="Examples:\n  mesh inbox list\n  mesh inbox list --all --user\n",
    )
    list_parser.add_argument("--all", action="store_true", default=False, help="Include acknowledged/historical messages")
    list_parser.set_defaults(func=handle_inbox_list)

    # wait
    wait_parser = sub.add_parser(
        "wait",
        parents=[parent_parser],
        help="Block waiting for new inbox messages (long-poll)",
        description="Block waiting for new inbox messages (long-poll).",
        epilog="Examples:\n  mesh inbox wait --timeout 60s\n  mesh inbox wait --timeout 10m --max 5\n",
    )
    wait_parser.add_argument("--max", type=int, default=1, help="Maximum number of messages to return")
    wait_parser.set_defaults(func=handle_inbox_wait)

    # ack
    ack_parser = sub.add_parser(
        "ack",
        parents=[parent_parser],
        help="Acknowledge processing of one or more inbox messages",
        description="Acknowledge processing of one or more inbox messages.",
        epilog="Examples:\n  mesh inbox ack msg_123\n  mesh inbox ack msg_1 msg_2\n",
    )
    ack_parser.add_argument("messages", nargs="+", help="Message identifier(s) to acknowledge")
    ack_parser.set_defaults(func=handle_inbox_ack)


def handle_inbox_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "all": getattr(args, "all", False),
        "user": getattr(args, "user", False),
    }
    resp = client.execute_cmd(args, "inbox.list", payload)
    return resp.get("data", {})


def handle_inbox_wait(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "dry_run", False):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--dry-run is not supported on 'inbox wait'",
            details={"param": "dry_run"},
        )
    timeout_val = getattr(args, "timeout", None)
    if not timeout_val:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--timeout is required for inbox wait",
            details={"param": "timeout"},
        )
    client = MeshClient(home=args.home)
    payload = {
        "timeout": timeout_val,
        "max": getattr(args, "max", 1),
        "user": getattr(args, "user", False),
    }
    resp = client.execute_cmd(args, "inbox.wait", payload)
    return resp.get("data", {})


def handle_inbox_ack(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "messages": args.messages,
        "user": getattr(args, "user", False),
    }
    resp = client.execute_cmd(args, "inbox.ack", payload)
    return resp.get("data", {})


def handle_inbox(verb: str, milestone: str) -> Dict[str, Any]:
    raise NotImplementedInM1Error(verb, milestone)
