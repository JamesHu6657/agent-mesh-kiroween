"""CLI subcommand wrapper for launching the Agent Mesh desktop window."""

from __future__ import annotations

import argparse
from typing import Any, Dict

import mesh.constants as c
from mesh.errors import MeshError


def register_desktop_command(
    subparsers: argparse._SubParsersAction,
    parent_parser: argparse.ArgumentParser,
) -> None:
    """Register ``mesh desktop`` on the root CLI parser."""
    desktop_parser = subparsers.add_parser(
        "desktop",
        parents=[parent_parser],
        help="Launch the optional graphical desktop client",
        description="Launch the optional graphical desktop client over local IPC.",
        epilog="Examples:\n  mesh desktop\n  mesh desktop --home /tmp/mh\n  mesh desktop --check\n",
    )
    desktop_parser.add_argument(
        "--title",
        default="Agent Mesh",
        help="Desktop window title",
    )
    desktop_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable WebView developer tools when supported",
    )
    desktop_parser.add_argument(
        "--check",
        action="store_true",
        help="Verify desktop static assets without opening a window",
    )
    desktop_parser.set_defaults(func=handle_desktop)


def handle_desktop(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``mesh desktop`` command."""
    if getattr(args, "dry_run", False):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="mesh desktop does not support --dry-run",
            details={"param": "dry_run"},
        )
    if not hasattr(args, "request_id") and getattr(args, "home", None) is not None:
        import mesh.client as _client_mod

        _client_mod.EXPLICIT_HOME = True
    from mesh.desktop.app import launch_desktop, verify_assets

    if getattr(args, "check", False):
        info = verify_assets()
        info["home"] = args.home
        return info

    return launch_desktop(
        home=args.home,
        title=getattr(args, "title", "Agent Mesh"),
        debug=bool(getattr(args, "debug", False)),
    )
