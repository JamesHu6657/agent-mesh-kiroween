"""Event resource commands: list, tail."""

import argparse
import json
import sys
from typing import Any, Dict

from mesh.client import MeshClient
import mesh.constants as c
from mesh.errors import MeshError, NotImplementedInM1Error


def register_event_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    event_parser = subparsers.add_parser(
        "event",
        parents=[parent_parser],
        help="Inspect and tail the append-only cryptographic event log",
        description="Inspect and tail the append-only cryptographic event log.",
        epilog="Examples:\n  mesh event list --project prj_123\n  mesh event list --task tsk_456\n  mesh event tail --project prj_123\n",
    )
    sub = event_parser.add_subparsers(dest="verb", metavar="<verb>")

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="Query immutable event log with filters",
        description="Query immutable event log with filters.",
        epilog="Examples:\n  mesh event list\n  mesh event list --project prj_123 --limit 20\n  mesh event list --type task.state_changed\n",
    )
    list_parser.add_argument("--project", help="Filter by project ID")
    list_parser.add_argument("--task", help="Filter by task ID")
    list_parser.add_argument("--type", help="Filter by event type")
    list_parser.add_argument("--since", type=int, help="Sequence number to query events strictly after")
    list_parser.add_argument("--limit", type=int, default=50, help="Maximum number of events to return")
    list_parser.set_defaults(func=handle_event_list)

    # tail
    tail_parser = sub.add_parser(
        "tail",
        parents=[parent_parser],
        help="Stream live events as NDJSON as they are recorded",
        description="Stream live events as NDJSON as they are recorded.",
        epilog="Examples:\n  mesh event tail\n  mesh event tail --project prj_123\n",
    )
    tail_parser.add_argument("--project", help="Filter by project ID")
    tail_parser.add_argument("--task", help="Filter by task ID")
    tail_parser.add_argument("--type", help="Filter by event type")
    tail_parser.add_argument("--since", type=int, help="Sequence number to query events strictly after")
    tail_parser.set_defaults(func=handle_event_tail)


def handle_event_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "project": getattr(args, "project", None),
        "task": getattr(args, "task", None),
        "type": getattr(args, "type", None),
        "since": getattr(args, "since", None),
        "limit": getattr(args, "limit", 50),
    }
    resp = client.execute_cmd(args, "event.list", payload)
    return resp.get("data", {})


def handle_event_tail(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "dry_run", False):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--dry-run is not supported on 'event tail'",
            details={"param": "dry_run"},
        )
    setattr(args, "_raw_ndjson", True)
    client = MeshClient(home=args.home)
    payload = {
        "project": getattr(args, "project", None),
        "task": getattr(args, "task", None),
        "type": getattr(args, "type", None),
        "since": getattr(args, "since", None),
        "timeout": getattr(args, "timeout", None),
    }
    return client.stream_cmd(args, "event.tail", payload)


def handle_event(verb: str, milestone: str) -> Dict[str, Any]:
    raise NotImplementedInM1Error(verb, milestone)
