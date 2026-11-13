"""Intake resource commands: submit."""

import argparse
import os
from typing import Any, Dict

from mesh.client import MeshClient


def register_intake_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    intake_parser = subparsers.add_parser(
        "intake",
        parents=[parent_parser],
        help="Submit new project requests and follow-up messages into Mesh",
        description="Submit new project requests and follow-up messages into Mesh.",
        epilog="Examples:\n  mesh intake submit --title \"New Feature\" --goal \"Build feature\" --criterion \"Tests pass\"\n  mesh intake submit --project prj_123 --message \"Additional requirement\"\n",
    )
    sub = intake_parser.add_subparsers(dest="verb", metavar="<verb>")

    submit_parser = sub.add_parser(
        "submit",
        parents=[parent_parser],
        help="Submit a project goal or send a follow-up message to an existing project",
        description="Submit a project goal or send a follow-up message to an existing project.",
        epilog="Examples:\n  mesh intake submit --title \"Add CSV export\" --goal \"Export reports as CSV\" --criterion \"RFC4180 format\"\n  mesh intake submit --project prj_123 --message \"Priority elevated\"\n",
    )
    submit_parser.add_argument("--title", help="Project title")
    submit_parser.add_argument("--goal", help="Project goal description")
    submit_parser.add_argument("--criterion", action="append", default=[], help="Acceptance criterion (repeatable)")
    submit_parser.add_argument("--lead", help="Designated lead agent identifier")
    submit_parser.add_argument("--repo", help="Git repository root path; makes this a repo project whose code tasks get their own git worktree and branch")
    submit_parser.add_argument("--base-branch", help="Base git branch for integration")
    submit_parser.add_argument("--test-command", help="Test command JSON array or 'none'")
    submit_parser.add_argument("--depends-on-project", action="append", default=[], help="Project dependency ID (repeatable)")
    submit_parser.add_argument("--origin-ref", help="External tracking reference")
    submit_parser.add_argument("--project", help="Existing project ID (for follow-up messages)")
    submit_parser.add_argument("--message", help="Follow-up message text")
    submit_parser.set_defaults(func=handle_intake_submit)


def handle_intake_submit(args: argparse.Namespace) -> Dict[str, Any]:
    repo = getattr(args, "repo", None)
    client = MeshClient(home=args.home)
    payload = {
        # The daemon has its own cwd: send an absolute path (realpath is the daemon's job).
        "repo": os.path.abspath(repo) if isinstance(repo, str) and repo else repo,
        "title": getattr(args, "title", None),
        "goal": getattr(args, "goal", None),
        "criterion": getattr(args, "criterion", None) or getattr(args, "criteria", None) or [],
        "lead": getattr(args, "lead", None),
        "base_branch": getattr(args, "base_branch", None),
        "test_command": getattr(args, "test_command", None),
        "depends_on_project": getattr(args, "depends_on_project", None) or getattr(args, "depends_on_projects", None) or [],
        "origin_ref": getattr(args, "origin_ref", None),
        "project": getattr(args, "project", None),
        "message": getattr(args, "message", None),
    }
    resp = client.execute_cmd(args, "intake.submit", payload)
    return resp.get("data", {})
