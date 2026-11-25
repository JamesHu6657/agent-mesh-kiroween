"""Top-level CLI commands: init, status, whoami, version, help."""

import argparse
import getpass
import os
import sys
from typing import Any, Dict

import mesh
import mesh.constants as c
from mesh.errors import MeshError, NotImplementedInM1Error
from mesh.cli.help import get_topic_help, HELP_TOPICS
from mesh.client import MeshClient
from mesh.core.init import init_mesh_home


def register_top_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    # init
    init_parser = subparsers.add_parser(
        "init",
        parents=[parent_parser],
        help="Initialize Mesh storage directory, database, and cryptographic verifiers",
        description="Initialize Mesh storage directory, database, and cryptographic verifiers.",
        epilog="Examples:\n  mesh init\n  mesh init --passphrase-fd 3 3<\"$P\"\n",
    )
    init_parser.set_defaults(func=handle_init)

    # status
    status_parser = subparsers.add_parser(
        "status",
        parents=[parent_parser],
        help="Show status of the local mesh daemon and storage",
        description="Show status of the local mesh daemon and storage.",
        epilog="Examples:\n  mesh status\n  mesh --home /tmp/mh status\n",
    )
    status_parser.set_defaults(func=handle_status)

    # whoami
    whoami_parser = subparsers.add_parser(
        "whoami",
        parents=[parent_parser],
        help="Inspect the calling principal identity and capabilities",
        description="Inspect the calling principal identity and capabilities.",
        epilog="Examples:\n  mesh whoami --user\n  mesh whoami --token-file ~/.config/agent.tok\n",
    )
    whoami_parser.set_defaults(func=handle_whoami)

    # version
    version_parser = subparsers.add_parser(
        "version",
        parents=[parent_parser],
        help="Display the Agent Mesh version",
        description="Display the Agent Mesh version.",
        epilog="Examples:\n  mesh version\n  mesh version --format text\n",
    )
    version_parser.set_defaults(func=handle_version)

    # help
    help_parser = subparsers.add_parser(
        "help",
        parents=[parent_parser],
        help="Display layered help topics and exit-code documentation",
        description="Display layered help topics and exit-code documentation.",
        epilog="Examples:\n  mesh help quickstart\n  mesh help exit-codes\n  mesh help auth\n  mesh help approvals\n",
    )
    help_parser.add_argument(
        "topic",
        nargs="?",
        choices=sorted(HELP_TOPICS.keys()),
        help=f"Help topic ({', '.join(sorted(HELP_TOPICS.keys()))})",
    )
    help_parser.set_defaults(func=handle_help)

    # top
    top_parser = subparsers.add_parser(
        "top",
        parents=[parent_parser],
        help="Live overview (not available yet)",
        description="Live overview of projects, tasks and runs (not available yet).",
    )
    top_parser.set_defaults(func=lambda args: _handle_top(args))


def _handle_top(args: argparse.Namespace) -> Dict[str, Any]:
    raise NotImplementedInM1Error("top", milestone=None)


def _refuse_dry_run(args: argparse.Namespace, what: str) -> None:
    """Linear full review C1: local commands that write cannot simulate; refuse rather
    than perform the change under --dry-run."""
    if getattr(args, "dry_run", False):
        import mesh.constants as _c
        from mesh.errors import MeshError as _ME
        raise _ME(_c.INVALID_ARGUMENT, exit_code=_c.EXIT_USAGE,
                  message=f"{what} does not support --dry-run (it would change local state); run it without --dry-run",
                  details={"param": "dry_run"})


def handle_init(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle mesh init command."""
    _refuse_dry_run(args, "mesh init")
    from mesh.core.passphrase import read_passphrase
    passphrase = read_passphrase(args, "Enter user passphrase (>= 12 chars): ")

    res = init_mesh_home(home=args.home, passphrase=passphrase)
    return res


def handle_status(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle mesh status command."""
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "daemon.status")
    return resp.get("data", {})


def handle_whoami(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle mesh whoami command."""
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "whoami")
    return resp.get("data", {})


def handle_version(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle mesh version command."""
    return {"version": mesh.__version__}


def handle_help(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle mesh help command."""
    topic = getattr(args, "topic", None)
    if not topic:
        topics_list = sorted(HELP_TOPICS.keys())
        text = f"Available help topics: {', '.join(topics_list)}\n\nUse 'mesh help <topic>' for details.\n\nExamples:\n  mesh help quickstart\n  mesh help exit-codes\n  mesh help auth\n"
        return {"topics": topics_list, "text": text}
    return {"topic": topic, "text": get_topic_help(topic)}
