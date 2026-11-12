"""Approval resource commands: request, list, show, approve, reject, cancel, check, consume."""

import argparse
import json
from typing import Any, Dict

import mesh.constants as c
from mesh.client import MeshClient
from mesh.core.canonical import check_no_floats
from mesh.errors import MeshError, NotImplementedInM1Error


def register_approval_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    approval_parser = subparsers.add_parser(
        "approval",
        parents=[parent_parser],
        help="Manage human-in-the-loop approvals, cryptographic MACs, and consumption",
        description="Manage human-in-the-loop approvals, cryptographic MACs, and consumption.",
        epilog="Examples:\n  mesh approval list\n  mesh approval show apr_123\n  mesh approval approve apr_123 --op-hash sha256:... --user\n",
    )
    sub = approval_parser.add_subparsers(dest="verb", metavar="<verb>")

    # request
    request_parser = sub.add_parser(
        "request",
        parents=[parent_parser],
        help="Create a pending approval request for a gated operation",
        description="Create a pending approval request for a gated operation.",
        epilog="Examples:\n  mesh approval request --category spend_money --summary \"API credit\" --operation '{\"usd\": 10}'\n",
    )
    request_parser.add_argument("--category", required=True, help="Approval category (project_control, permission_escalation, spend_money, deploy, etc.)")
    request_parser.add_argument("--summary", required=True, help="Human-readable summary of the requested action")
    request_parser.add_argument("--operation", required=True, help="Canonical operation payload (JSON string)")
    request_parser.add_argument("--project", help="Associated project identifier")
    request_parser.add_argument("--task", help="Associated task identifier")
    request_parser.add_argument("--expires-in", help="Expiration duration (e.g. 1h, 24h)")
    request_parser.set_defaults(func=handle_approval_request)

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List approval requests with optional status and project filtering",
        description="List approval requests with optional status and project filtering.",
        epilog="Examples:\n  mesh approval list\n  mesh approval list --status pending\n",
    )
    list_parser.add_argument("--status", help="Filter by status (pending, approved, rejected, cancelled, consumed, expired)")
    list_parser.add_argument("--project", help="Filter by project ID")
    list_parser.set_defaults(func=handle_approval_list)

    # show
    show_parser = sub.add_parser(
        "show",
        parents=[parent_parser],
        help="Show detailed status, operation payload, hash, and decision channel for an approval",
        description="Show detailed status, operation payload, hash, and decision channel for an approval.",
        epilog="Examples:\n  mesh approval show apr_123\n",
    )
    show_parser.add_argument("approval_id", help="Approval identifier")
    show_parser.set_defaults(func=handle_approval_show)

    # approve
    approve_parser = sub.add_parser(
        "approve",
        parents=[parent_parser],
        help="Approve a pending approval request (direct or relayed)",
        description="Approve a pending approval request (direct or relayed).",
        epilog="Examples:\n  mesh approval approve apr_123 --op-hash sha256:... --user\n  mesh approval approve apr_123 --op-hash sha256:... --relayed --user-statement \"Approved\" --token-file agent.tok\n",
    )
    approve_parser.add_argument("approval_id", help="Approval identifier")
    approve_parser.add_argument("--op-hash", required=True, help="Expected sha256 operation hash")
    approve_parser.add_argument("--note", help="Decision note")
    approve_parser.set_defaults(func=handle_approval_approve)

    # reject
    reject_parser = sub.add_parser(
        "reject",
        parents=[parent_parser],
        help="Reject a pending approval request",
        description="Reject a pending approval request.",
        epilog="Examples:\n  mesh approval reject apr_123 --op-hash sha256:... --note \"Cost too high\" --user\n",
    )
    reject_parser.add_argument("approval_id", help="Approval identifier")
    reject_parser.add_argument("--op-hash", required=True, help="Expected sha256 operation hash")
    reject_parser.add_argument("--note", help="Rejection rationale")
    reject_parser.set_defaults(func=handle_approval_reject)

    # cancel
    cancel_parser = sub.add_parser(
        "cancel",
        parents=[parent_parser],
        help="Cancel a pending approval request (requester only)",
        description="Cancel a pending approval request (requester only).",
        epilog="Examples:\n  mesh approval cancel apr_123\n",
    )
    cancel_parser.add_argument("approval_id", help="Approval identifier")
    cancel_parser.set_defaults(func=handle_approval_cancel)

    # check
    check_parser = sub.add_parser(
        "check",
        parents=[parent_parser],
        help="Check approval status and trust level without consuming it",
        description="Check approval status and trust level without consuming it.",
        epilog="Examples:\n  mesh approval check apr_123 --op-hash sha256:...\n",
    )
    check_parser.add_argument("approval_id", help="Approval identifier")
    check_parser.add_argument("--op-hash", required=True, help="Expected sha256 operation hash")
    check_parser.set_defaults(func=handle_approval_check)

    # consume
    consume_parser = sub.add_parser(
        "consume",
        parents=[parent_parser],
        help="Consume an approved approval token before external action execution",
        description="Consume an approved approval token before external action execution.",
        epilog="Examples:\n  mesh approval consume apr_123 --op-hash sha256:...\n",
    )
    consume_parser.add_argument("approval_id", help="Approval identifier")
    consume_parser.add_argument("--op-hash", required=True, help="Expected sha256 operation hash")
    consume_parser.set_defaults(func=handle_approval_consume)


def handle_approval_consume(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "approval_id": args.approval_id,
        "op_hash": args.op_hash,
    }
    resp = client.execute_cmd(args, "approval.consume", payload)
    return resp.get("data", {})


def handle_approval_approve(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "approval_id": args.approval_id,
        "op_hash": args.op_hash,
        "note": getattr(args, "note", None),
    }
    resp = client.execute_cmd(args, "approval.approve", payload)
    return resp.get("data", {})


def handle_approval_reject(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "approval_id": args.approval_id,
        "op_hash": args.op_hash,
        "note": getattr(args, "note", None),
    }
    resp = client.execute_cmd(args, "approval.reject", payload)
    return resp.get("data", {})


def handle_approval_cancel(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "approval_id": args.approval_id,
    }
    resp = client.execute_cmd(args, "approval.cancel", payload)
    return resp.get("data", {})


def handle_approval_check(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "approval_id": args.approval_id,
        "op_hash": args.op_hash,
    }
    resp = client.execute_cmd(args, "approval.check", payload)
    return resp.get("data", {})


def handle_approval_show(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "approval.show", {"approval_id": args.approval_id})
    return resp.get("data", {})


def handle_approval_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "status": getattr(args, "status", None),
        "project": getattr(args, "project", None),
    }
    resp = client.execute_cmd(args, "approval.list", payload)
    return resp.get("data", {})


def handle_approval_request(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    raw_op = args.operation
    if isinstance(raw_op, str):
        try:
            operation = json.loads(raw_op)
        except json.JSONDecodeError as e:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid JSON in --operation: {e}",
                details={"param": "operation"},
            )
    elif isinstance(raw_op, dict):
        operation = raw_op
    else:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--operation must be a JSON object",
            details={"param": "operation"},
        )

    if not isinstance(operation, dict):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--operation must be a JSON object",
            details={"param": "operation"},
        )

    try:
        check_no_floats(operation)
    except ValueError as e:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=str(e),
            details={"param": "operation"},
        )

    payload = {
        "category": args.category,
        "summary": args.summary,
        "operation": operation,
        "project": getattr(args, "project", None),
        "task": getattr(args, "task", None),
        "expires_in": getattr(args, "expires_in", None),
    }
    resp = client.execute_cmd(args, "approval.request", payload)
    return resp.get("data", {})


def handle_approval(verb: str, milestone: str) -> Dict[str, Any]:
    raise NotImplementedInM1Error(verb, milestone)
