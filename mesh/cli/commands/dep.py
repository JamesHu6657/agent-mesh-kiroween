"""Dependency resource commands: add, remove, list."""

import argparse
from typing import Any, Dict

from mesh.client import MeshClient
from mesh.errors import NotImplementedInM1Error


def register_dep_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    dep_parser = subparsers.add_parser(
        "dep",
        parents=[parent_parser],
        help="Manage prerequisite dependencies between tasks",
        description="Manage prerequisite dependencies between tasks.",
        epilog="Examples:\n  mesh dep add tsk_2 --on tsk_1\n  mesh dep remove tsk_2 --on tsk_1\n  mesh dep list tsk_2\n",
    )
    sub = dep_parser.add_subparsers(dest="verb", metavar="<verb>")

    # add
    add_parser = sub.add_parser(
        "add",
        parents=[parent_parser],
        help="Add a prerequisite dependency: task depends on task2",
        description="Add a prerequisite dependency: task depends on task2.",
        epilog="Examples:\n  mesh dep add tsk_2 --on tsk_1\n",
    )
    add_parser.add_argument("task_id", nargs="?", default=None, help="Dependent task identifier")
    add_parser.add_argument("on_arg", nargs="?", default=None, help="Prerequisite task identifier (positional alternative)")
    add_parser.add_argument("--on", default=None, help="Prerequisite task identifier")
    add_parser.set_defaults(func=handle_dep_add)

    # remove
    remove_parser = sub.add_parser(
        "remove",
        parents=[parent_parser],
        help="Remove a prerequisite dependency between tasks",
        description="Remove a prerequisite dependency between tasks.",
        epilog="Examples:\n  mesh dep remove tsk_2 --on tsk_1\n",
    )
    remove_parser.add_argument("task_id", nargs="?", default=None, help="Dependent task identifier")
    remove_parser.add_argument("on_arg", nargs="?", default=None, help="Prerequisite task identifier (positional alternative)")
    remove_parser.add_argument("--on", default=None, help="Prerequisite task identifier to remove")
    remove_parser.set_defaults(func=handle_dep_remove)

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List all dependencies for a task",
        description="List all dependencies for a task.",
        epilog="Examples:\n  mesh dep list tsk_2\n",
    )
    list_parser.add_argument("task_id", help="Task identifier")
    list_parser.set_defaults(func=handle_dep_list)


def handle_dep_add(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    on_val = getattr(args, "on", None) or getattr(args, "on_arg", None)
    resp = client.execute_cmd(
        args,
        "dep.add",
        {"task_id": getattr(args, "task_id", None), "on": on_val},
    )
    return resp.get("data", {})


def handle_dep_remove(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    on_val = getattr(args, "on", None) or getattr(args, "on_arg", None)
    resp = client.execute_cmd(
        args,
        "dep.remove",
        {"task_id": getattr(args, "task_id", None), "on": on_val},
    )
    return resp.get("data", {})


def handle_dep_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(
        args,
        "dep.list",
        {"task_id": getattr(args, "task_id", None)},
    )
    return resp.get("data", {})
