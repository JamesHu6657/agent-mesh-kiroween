"""Main entry point, parser tree, JSON envelope, and error conversion for Agent Mesh CLI."""

import argparse
import json
import os
import secrets
import sys
from typing import Any, Dict, List, Optional

import mesh.constants as c
from mesh.errors import MeshError
from mesh.core.paths import resolve_home
from mesh.cli.help import TOP_HELP_EPILOG, HELP_TOPICS, get_topic_help

from mesh.cli.commands.top import register_top_commands
from mesh.cli.commands.daemon import register_daemon_commands
from mesh.cli.commands.config import register_config_commands
from mesh.cli.commands.agent import register_agent_commands
from mesh.cli.commands.adapter import register_adapter_commands
from mesh.cli.commands.intake import register_intake_commands
from mesh.cli.commands.project import register_project_commands
from mesh.cli.commands.task import register_task_commands
from mesh.cli.commands.dep import register_dep_commands
from mesh.cli.commands.approval import register_approval_commands
from mesh.cli.commands.inbox import register_inbox_commands
from mesh.cli.commands.event import register_event_commands
from mesh.cli.commands.run import register_run_commands
from mesh.cli.commands.workspace import register_workspace_commands
from mesh.cli.commands.doctor import register_doctor_commands
from mesh.cli.commands.audit import register_audit_commands


def is_text_format(argv: Optional[List[str]] = None) -> bool:
    """Check if --format text was requested in argv."""
    if argv is None:
        argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--format" and i + 1 < len(argv) and argv[i + 1] == "text":
            return True
        if arg.startswith("--format=") and arg.split("=", 1)[1] == "text":
            return True
    return False


def generate_request_id() -> str:
    """Generate a random request ID (e.g. req_0123456789abcdef)."""
    return f"req_{secrets.token_hex(8)}"


def format_error_response(
    code: str,
    message: str,
    exit_code: int = c.EXIT_USAGE,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    text_format: bool = False,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Print error response either as JSON envelope to stdout or text to stderr."""
    if details is None:
        details = {}
    if text_format:
        sys.stderr.write(f"mesh: error [{code}]: {message}\n")
    else:
        doc = {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "details": details,
            },
            "exit_code": exit_code,
            "request_id": request_id,
        }
        if data is not None:
            doc["data"] = data
        sys.stdout.write(json.dumps(doc) + "\n")
        sys.stdout.flush()


def format_success_response(
    data: Any,
    warnings: Optional[List[str]] = None,
    request_id: Optional[str] = None,
    idempotent_replay: bool = False,
    dry_run: bool = False,
    text_format: bool = False,
) -> None:
    """Print success response as JSON envelope or formatted text."""
    if warnings is None:
        warnings = []
    if request_id is None:
        request_id = generate_request_id()

    if text_format:
        from mesh.cli.render import render_text
        sys.stdout.write(render_text(data) + "\n")
        for w in warnings:
            sys.stderr.write(f"mesh: warning: {w}\n")
        sys.stdout.flush()
    else:
        doc = {
            "ok": True,
            "data": data,
            "warnings": warnings,
            "request_id": request_id,
            "idempotent_replay": idempotent_replay,
            "dry_run": dry_run,
        }
        sys.stdout.write(json.dumps(doc) + "\n")
        sys.stdout.flush()


class MeshArgumentParser(argparse.ArgumentParser):
    """Custom ArgumentParser that overrides error() to output the JSON envelope with exit 2."""

    def error(self, message: str) -> None:
        text_fmt = is_text_format()
        format_error_response(
            code=c.INVALID_ARGUMENT,
            message=message,
            exit_code=c.EXIT_USAGE,
            details={},
            request_id=None,
            text_format=text_fmt,
        )
        sys.exit(c.EXIT_USAGE)


def create_parent_parser() -> argparse.ArgumentParser:
    """Create the parent parser holding every global flag."""
    parent = argparse.ArgumentParser(add_help=False)
    default_home = os.environ.get("MESH_HOME") or os.path.expanduser("~/.agent-mesh")
    parent.add_argument("--home", default=argparse.SUPPRESS, help=f"Storage root directory (default: {default_home})")
    parent.add_argument("--format", choices=["json", "text"], default=argparse.SUPPRESS, help="Output format (default: json)")
    parent.add_argument("--user", action="store_true", default=argparse.SUPPRESS, help="Authenticate as direct human user")
    parent.add_argument("--passphrase-fd", type=int, default=argparse.SUPPRESS, help="File descriptor to read passphrase from")
    parent.add_argument("--passphrase-file", default=argparse.SUPPRESS,
                        help="0600 file holding the passphrase (env: MESH_PASSPHRASE_FILE or MESH_PASSPHRASE)")
    parent.add_argument("--token-file", default=argparse.SUPPRESS, help="Path to 0600 file containing agent token")
    parent.add_argument("--input", default=argparse.SUPPRESS, help="Read JSON parameters from FILE or '-' for stdin")
    parent.add_argument("--idempotency-key", default=argparse.SUPPRESS, help="Unique key for safe mutation replay (1-128 chars)")
    parent.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="Simulate execution without persisting changes")
    parent.add_argument("--relayed", action="store_true", default=argparse.SUPPRESS, help="Relay user approval on behalf of human")
    parent.add_argument("--user-statement", default=argparse.SUPPRESS, help="Relayed approval statement from user")
    parent.add_argument("--evidence-ref", default=argparse.SUPPRESS, help="Evidence reference for relayed approval")
    parent.add_argument("--approval", default=argparse.SUPPRESS, help="Approval ID authorizing this gated operation")
    parent.add_argument("--if-version", type=int, default=argparse.SUPPRESS, help="Optimistic concurrency version check")
    parent.add_argument("--timeout", default=argparse.SUPPRESS, help="Client response timeout duration")
    parent.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="Suppress diagnostic output")
    return parent


def build_parser() -> MeshArgumentParser:
    """Build the complete CLI parser tree."""
    parent_parser = create_parent_parser()

    root_parser = MeshArgumentParser(
        prog="mesh",
        description="Agent Mesh v1 — Deterministic local orchestration for autonomous AI coding agents.",
        epilog=TOP_HELP_EPILOG,
        parents=[parent_parser],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    root_parser.add_argument("--version", action="store_true", default=argparse.SUPPRESS, dest="show_version",
                             help="Show the Agent Mesh version and exit (same as `mesh version`)")

    subparsers = root_parser.add_subparsers(dest="resource", metavar="<resource>")

    # Top-level commands
    register_top_commands(subparsers, parent_parser)

    # Resource commands
    register_daemon_commands(subparsers, parent_parser)
    register_config_commands(subparsers, parent_parser)
    register_agent_commands(subparsers, parent_parser)
    register_adapter_commands(subparsers, parent_parser)
    register_intake_commands(subparsers, parent_parser)
    register_project_commands(subparsers, parent_parser)
    register_task_commands(subparsers, parent_parser)
    register_dep_commands(subparsers, parent_parser)
    register_approval_commands(subparsers, parent_parser)
    register_inbox_commands(subparsers, parent_parser)
    register_event_commands(subparsers, parent_parser)
    register_run_commands(subparsers, parent_parser)
    register_workspace_commands(subparsers, parent_parser)
    register_doctor_commands(subparsers, parent_parser)
    from mesh.cli.commands.setup import register_setup_command
    register_setup_command(subparsers, parent_parser)
    register_audit_commands(subparsers, parent_parser)

    _keep_help_layout(root_parser)
    return root_parser


def _keep_help_layout(parser: argparse.ArgumentParser) -> None:
    """Keep the line breaks of every description and epilog (examples stay one per line).

    argparse's default formatter rewraps them into one paragraph; the raw-description
    formatter keeps them as written and still wraps the argument help."""
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                _keep_help_layout(sub)


GLOBAL_FLAG_DESTS = {
    "home",
    "format",
    "user",
    "passphrase_fd",
    "passphrase_file",
    "token_file",
    "input",
    "idempotency_key",
    "dry_run",
    "relayed",
    "user_statement",
    "evidence_ref",
    "approval",
    "if_version",
    "timeout",
    "quiet",
    "func",
    "resource",
    "verb",
    "token_verb",
    "help",
    "show_version",
}

SYNONYMS = {
    "allowed_models": "allowed_model",
    "allowed_efforts": "allowed_effort",
    "capabilities": "capability",
    "options": "option",
    "depends_on": "on",
    "criteria": "criterion",
    "depends_on_projects": "depends_on_project",
}


def get_active_subparser(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                if getattr(args, action.dest, None) == name:
                    return get_active_subparser(sub, args)
    return parser


def relax_required_actions(parser: argparse.ArgumentParser) -> None:
    for action in parser._actions:
        action.required = False
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                relax_required_actions(sub)


def parse_and_apply_input_file(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    argv: Optional[List[str]] = None,
) -> None:
    """Process --input FILE|-."""
    input_source = getattr(args, "input", None)
    if not input_source:
        return

    try:
        if input_source == "-":
            from mesh.cli.stdin import read_stdin
            raw_data = read_stdin("--input")
        else:
            with open(input_source, "r", encoding="utf-8") as f:
                raw_data = f.read()
        json_obj = json.loads(raw_data)
    except Exception as exc:
        raise MeshError(
            code=c.INVALID_JSON,
            exit_code=c.EXIT_USAGE,
            message=f"Failed to parse JSON input from '{input_source}': {exc}",
        ) from exc

    if not isinstance(json_obj, dict):
        raise MeshError(
            code=c.INVALID_JSON,
            exit_code=c.EXIT_USAGE,
            message="JSON input must be a JSON object (mapping)",
        )

    # Disallow passing passphrase via stdin JSON (checklist G17)
    if "passphrase" in json_obj:
        raise MeshError(
            code=c.PASSPHRASE_SOURCE_REJECTED,
            exit_code=c.EXIT_USAGE,
            message="Passphrase cannot be supplied via stdin JSON. Use /dev/tty or --passphrase-fd.",
        )

    active_sub = get_active_subparser(parser, args)

    # Get valid command-specific parameter destinations
    valid_params = set()
    for action in active_sub._actions:
        if action.dest not in GLOBAL_FLAG_DESTS:
            valid_params.add(action.dest)
    # Commands where timeout is also a valid command parameter
    sub_prog = getattr(active_sub, "prog", "")
    if any(cmd in sub_prog for cmd in ("task create", "task wait", "inbox wait", "project wait")):
        valid_params.add("timeout")

    # Add known synonyms
    for syn, canonical in SYNONYMS.items():
        if canonical in valid_params:
            valid_params.add(syn)

    # Track flags specified directly on CLI argv
    cli_specified_flags = set()
    if argv:
        for a in argv:
            if a.startswith("--"):
                flag_name = a[2:].split("=")[0].replace("-", "_")
                cli_specified_flags.add(flag_name)

    from_json = set()
    for key, val in json_obj.items():
        if (key in GLOBAL_FLAG_DESTS and key not in valid_params) or key not in valid_params:
            raise MeshError(
                code=c.UNKNOWN_PARAM,
                exit_code=c.EXIT_USAGE,
                message=f"Unknown parameter in JSON input: '{key}'",
                details={"param": key},
            )

        canonical_key = SYNONYMS.get(key, key)
        if canonical_key in cli_specified_flags or key in cli_specified_flags:
            raise MeshError(
                code=c.DUPLICATE_PARAM,
                exit_code=c.EXIT_USAGE,
                message=f"Parameter '{key}' provided both via command-line flag and JSON input",
                details={"param": key},
            )

        setattr(args, key, val)
        if canonical_key != key:
            setattr(args, canonical_key, val)
        from_json.add(key)
        from_json.add(canonical_key)

    setattr(args, "_from_json", from_json)


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entry point."""
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()

    # If invoked with no arguments at all, print top-level help
    if not argv:
        parser.print_help()
        return c.EXIT_OK

    req_id = generate_request_id()
    text_fmt = is_text_format(argv)
    from mesh.cli import stdin as _stdin
    _stdin.reset()

    has_input = any(arg == "--input" or arg.startswith("--input=") for arg in argv)
    if has_input:
        relax_required_actions(parser)

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code

    setattr(args, "request_id", req_id)
    default_home = os.environ.get("MESH_HOME") or os.path.expanduser("~/.agent-mesh")
    if not hasattr(args, "home"):
        setattr(args, "home", default_home)
    # m3-test-kdf round 3: --home / MESH_HOME is resolved once, here, for every verb.
    # Round 4: an empty home or "/" is refused here (rc 2), before any verb runs.
    try:
        setattr(args, "home", resolve_home(args.home))
    except MeshError as exc:
        format_error_response(
            code=exc.code,
            message=exc.message,
            exit_code=exc.exit_code,
            details=exc.details,
            request_id=req_id,
            text_format=text_fmt,
            data=exc.data,
        )
        return exc.exit_code
    if not hasattr(args, "format"):
        setattr(args, "format", "json")
    if not hasattr(args, "user"):
        setattr(args, "user", False)
    if not hasattr(args, "passphrase_fd"):
        setattr(args, "passphrase_fd", None)
    if not hasattr(args, "passphrase_file"):
        setattr(args, "passphrase_file", None)
    if not hasattr(args, "token_file"):
        setattr(args, "token_file", None)
    if not hasattr(args, "input"):
        setattr(args, "input", None)
    if not hasattr(args, "idempotency_key"):
        setattr(args, "idempotency_key", None)
    if not hasattr(args, "dry_run"):
        setattr(args, "dry_run", False)
    if not hasattr(args, "relayed"):
        setattr(args, "relayed", False)
    if not hasattr(args, "user_statement"):
        setattr(args, "user_statement", None)
    if not hasattr(args, "evidence_ref"):
        setattr(args, "evidence_ref", None)
    if not hasattr(args, "approval"):
        setattr(args, "approval", None)
    if not hasattr(args, "if_version"):
        setattr(args, "if_version", None)
    if not hasattr(args, "timeout"):
        setattr(args, "timeout", None)
    if not hasattr(args, "quiet"):
        setattr(args, "quiet", False)

    if getattr(args, "show_version", False):
        from mesh.cli.commands.top import handle_version
        format_success_response(data=handle_version(args), request_id=req_id, text_format=text_fmt)
        return c.EXIT_OK

    # Check for subcommand function
    func = getattr(args, "func", None)
    if func is None:
        # If resource given without verb, print resource help
        parser.print_help()
        return c.EXIT_USAGE

    try:
        # Handle --input FILE|-
        parse_and_apply_input_file(args, parser, argv)

        # Execute handler
        result = func(args)

        if getattr(args, "_raw_ndjson", False):
            return c.EXIT_OK

        idempotent_replay = bool(getattr(args, "_idempotent_replay", False))
        warnings = getattr(args, "_warnings", [])
        format_success_response(
            data=result,
            warnings=warnings,
            request_id=req_id,
            idempotent_replay=idempotent_replay,
            dry_run=getattr(args, "dry_run", False),
            text_format=text_fmt,
        )
        return c.EXIT_OK

    except MeshError as exc:
        format_error_response(
            code=exc.code,
            message=exc.message,
            exit_code=exc.exit_code,
            details=exc.details,
            request_id=req_id,
            text_format=text_fmt,
            data=exc.data,
        )
        return exc.exit_code
    except Exception as exc:
        format_error_response(
            code=c.INTERNAL_ERROR,
            message=f"Internal unexpected error: {exc}",
            exit_code=c.EXIT_INTERNAL,
            details={},
            request_id=req_id,
            text_format=text_fmt,
        )
        return c.EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
