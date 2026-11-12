"""Run resource commands: list, show, log, release."""

import argparse
from typing import Any, Dict
from mesh.client import MeshClient


def register_run_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    run_parser = subparsers.add_parser(
        "run",
        parents=[parent_parser],
        help="Inspect individual task execution attempts, wrapper states, and logs",
        description="Inspect individual task execution attempts, wrapper states, and logs.",
        epilog="Examples:\n  mesh run list --task tsk_123\n  mesh run show run_abc456\n  mesh run log run_abc456\n",
    )
    sub = run_parser.add_subparsers(dest="verb", metavar="<verb>")

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List execution runs with optional task and status filters",
        description="List execution runs with optional task and status filters.",
        epilog="Examples:\n  mesh run list\n  mesh run list --task tsk_123\n  mesh run list --status running\n",
    )
    list_parser.add_argument("--task", help="Filter by task ID")
    list_parser.add_argument("--status", help="Filter by status (starting, running, exited, killed, lost, spawn_failed)")
    list_parser.set_defaults(func=handle_run_list)

    # show
    show_parser = sub.add_parser(
        "show",
        parents=[parent_parser],
        help="Display detailed metadata, timings, and exit reason for a run",
        description="Display detailed metadata, timings, and exit reason for a run.",
        epilog="Examples:\n  mesh run show run_abc456\n",
    )
    show_parser.add_argument("run_id", help="Run identifier")
    show_parser.set_defaults(func=handle_run_show)

    # log
    log_parser = sub.add_parser(
        "log",
        parents=[parent_parser],
        help="Inspect redacted stdout/stderr streams of a run",
        description="Inspect redacted stdout/stderr streams of a run.",
        epilog="Examples:\n  mesh run log run_abc456\n  mesh run log run_abc456 --stream stderr --tail 20\n",
    )
    log_parser.add_argument("run_id", help="Run identifier")
    log_parser.add_argument("--stream", choices=["stdout", "stderr"], help="Filter by stream name")
    log_parser.add_argument("--tail", type=int, help="Number of trailing lines to show")
    log_parser.set_defaults(func=handle_run_log)

    release_parser = sub.add_parser(
        "release",
        parents=[parent_parser],
        help="Release a run held because its seen state is unusable (operator, user only)",
        description=("Release a task run that meshd holds because its recorded group members "
                     "(seen.json) are unusable. Requires --force-seen-unusable. The run closes as "
                     "lost and its lease is released; no process is signalled."),
        epilog="Examples:\n  mesh run release run_abc456 --force-seen-unusable\n",
    )
    release_parser.add_argument("run_id", help="Run identifier")
    release_parser.add_argument("--force-seen-unusable", action="store_true", dest="force_seen_unusable",
                                help="Confirm releasing the seen-state hold without signalling any process")
    release_parser.set_defaults(func=handle_run_release)


def handle_run_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {"task": getattr(args, "task", None), "status": getattr(args, "status", None)}
    return client.execute_cmd(args, "run.list", payload).get("data", {})


def handle_run_show(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    return client.execute_cmd(args, "run.show", {"run_id": args.run_id}).get("data", {})


def handle_run_log(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {"run_id": args.run_id, "stream": getattr(args, "stream", None), "tail": getattr(args, "tail", None)}
    return client.execute_cmd(args, "run.log", payload).get("data", {})


def handle_run_release(args: argparse.Namespace) -> Dict[str, Any]:
    """Execute `mesh run release` (r4c)."""
    client = MeshClient(home=args.home)
    payload = {"run_id": args.run_id, "force_seen_unusable": bool(getattr(args, "force_seen_unusable", False))}
    return client.execute_cmd(args, "run.release", payload).get("data", {})
