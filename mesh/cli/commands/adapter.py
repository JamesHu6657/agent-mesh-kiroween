"""Adapter resource commands: list, show, probe, models."""

import argparse
import os
from typing import Any, Dict

import mesh.constants as c
from mesh.client import MeshClient
from mesh.errors import MeshError


def register_adapter_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    adapter_parser = subparsers.add_parser(
        "adapter",
        parents=[parent_parser],
        help="Inspect external CLI adapter status, probed models, and capabilities",
        description="Inspect external CLI adapter status, probed models, and capabilities.",
        epilog="Examples:\n  mesh adapter list\n  mesh adapter show droid\n  mesh adapter models kiro\n  mesh adapter probe vibe\n",
    )
    sub = adapter_parser.add_subparsers(dest="verb", metavar="<verb>")

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List all supported adapters and their verification statuses",
        description="List all supported adapters and their verification statuses.",
        epilog="Examples:\n  mesh adapter list\n  mesh adapter list --format json\n",
    )
    list_parser.set_defaults(func=handle_adapter_list)

    # show
    show_parser = sub.add_parser(
        "show",
        parents=[parent_parser],
        help="Show detailed capability matrix and evidence for an adapter",
        description="Show detailed capability matrix and evidence for an adapter.",
        epilog="Examples:\n  mesh adapter show droid\n  mesh adapter show kiro\n",
    )
    show_parser.add_argument("adapter_id", help="Adapter identifier (droid, agy, kiro, vibe, hermes, openclaw, none, fake)")
    show_parser.set_defaults(func=handle_adapter_show)

    # probe
    probe_parser = sub.add_parser(
        "probe",
        parents=[parent_parser],
        help="Probe live binary for version and help flags",
        description="Probe live binary for version and help flags.",
        epilog="Examples:\n  mesh adapter probe\n  mesh adapter probe agy\n",
    )
    probe_parser.add_argument("adapter_id", nargs="?", help="Optional adapter identifier")
    probe_parser.add_argument("--binary", help="Path to executable binary to probe")
    probe_parser.set_defaults(func=handle_adapter_probe)

    # models
    models_parser = sub.add_parser(
        "models",
        parents=[parent_parser],
        help="List available models for an adapter by querying vendor CLI",
        description="List available models for an adapter by querying vendor CLI.",
        epilog="Examples:\n  mesh adapter models kiro\n  mesh adapter models agy\n",
    )
    models_parser.add_argument("adapter_id", help="Adapter identifier")
    models_parser.set_defaults(func=handle_adapter_models)


def handle_adapter_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "adapter.list")
    return resp.get("data", {})


def handle_adapter_show(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {"adapter_id": args.adapter_id}
    resp = client.execute_cmd(args, "adapter.show", payload)
    return resp.get("data", {})


def handle_adapter_probe(args: argparse.Namespace) -> Dict[str, Any]:
    raw_bin = getattr(args, "binary", None)
    if raw_bin is not None and not args.adapter_id:
        raise MeshError(
            c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="'adapter_id' is required when '--binary' is specified",
            details={"param": "adapter_id"},
        )
    client = MeshClient(home=args.home)
    payload: Dict[str, Any] = {}
    if args.adapter_id is not None:
        payload["adapter_id"] = args.adapter_id
    if raw_bin is not None:
        if (
            isinstance(raw_bin, str)
            and raw_bin.strip()
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_bin)
            and not raw_bin.lstrip().startswith("-")
        ):
            payload["binary"] = os.path.abspath(os.path.expanduser(raw_bin.strip()))
        else:
            payload["binary"] = raw_bin
    resp = client.execute_cmd(args, "adapter.probe", payload)
    return resp.get("data", {})


def handle_adapter_models(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {"adapter_id": args.adapter_id}
    resp = client.execute_cmd(args, "adapter.models", payload)
    return resp.get("data", {})
