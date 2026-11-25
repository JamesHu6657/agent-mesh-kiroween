"""Audit resource commands: verify."""

import argparse
from typing import Any, Dict

from mesh.client import MeshClient


def register_audit_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    audit_parser = subparsers.add_parser(
        "audit",
        parents=[parent_parser],
        help="Verify event log hash chain continuity and direct user approval MAC signatures",
        description="Verify event log hash chain continuity and direct user approval MAC signatures.",
        epilog="Examples:\n  mesh audit verify --user\n  mesh audit verify --user --passphrase-fd 3\n",
    )
    sub = audit_parser.add_subparsers(dest="verb", metavar="<verb>")

    # verify
    verify_parser = sub.add_parser(
        "verify",
        parents=[parent_parser],
        help="Verify event log sha256 hash chain and recalculate direct approval user MACs",
        description="Verify event log sha256 hash chain and recalculate direct approval user MACs.",
        epilog="Examples:\n  mesh audit verify --user\n",
    )
    verify_parser.set_defaults(func=handle_audit_verify)


def handle_audit_verify(args: argparse.Namespace) -> Dict[str, Any]:
    """Verify event log hash chain and direct approval user MAC signatures."""
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "audit.verify")
    return resp.get("data", {})
