"""Workspace resource commands: list, show, prune, delete."""

import argparse
from typing import Any, Dict
from mesh.client import MeshClient


def register_workspace_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    workspace_parser = subparsers.add_parser(
        "workspace",
        parents=[parent_parser],
        help="Inspect and maintain isolated task directories and git worktrees",
        description="Inspect and maintain isolated task directories and git worktrees.",
        epilog="Examples:\n  mesh workspace list --project prj_123\n  mesh workspace show tsk_456\n  mesh workspace prune --project prj_123\n",
    )
    sub = workspace_parser.add_subparsers(dest="verb", metavar="<verb>")

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List allocated task workspaces and directories",
        description="List allocated task workspaces and directories.",
        epilog="Examples:\n  mesh workspace list\n  mesh workspace list --project prj_123\n",
    )
    list_parser.add_argument("--project", help="Filter by project ID")
    list_parser.set_defaults(func=handle_workspace_list)

    # show
    show_parser = sub.add_parser(
        "show",
        parents=[parent_parser],
        help="Show path, branch, and worktree status for a task workspace",
        description="Show path, branch, and worktree status for a task workspace.",
        epilog="Examples:\n  mesh workspace show tsk_123\n",
    )
    show_parser.add_argument("task_id", help="Task identifier")
    show_parser.set_defaults(func=handle_workspace_show)

    # prune
    prune_parser = sub.add_parser(
        "prune",
        parents=[parent_parser],
        help="Prune merged or clean-cancelled task worktrees",
        description="Prune merged or clean-cancelled task worktrees.",
        epilog="Examples:\n  mesh workspace prune --project prj_123\n  mesh workspace prune --dry-run\n",
    )
    prune_parser.add_argument("--project", help="Filter by project ID")
    prune_parser.set_defaults(func=handle_workspace_prune)

    # delete
    delete_parser = sub.add_parser(
        "delete",
        parents=[parent_parser],
        help="Delete a specific task workspace (human approval gated)",
        description="Delete a specific task workspace (human approval gated).",
        epilog="Examples:\n  mesh workspace delete tsk_123\n",
    )
    delete_parser.add_argument("task_id", help="Task identifier")
    delete_parser.set_defaults(func=handle_workspace_delete)


def handle_workspace_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    return client.execute_cmd(args, "workspace.list", {"project": getattr(args, "project", None)}).get("data", {})


def handle_workspace_show(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    return client.execute_cmd(args, "workspace.show", {"task_id": args.task_id}).get("data", {})


def handle_workspace_prune(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    return client.execute_cmd(
        args, "workspace.prune", {"project": getattr(args, "project", None)}
    ).get("data", {})


def handle_workspace_delete(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    return client.execute_cmd(args, "workspace.delete", {"task_id": args.task_id}).get("data", {})
