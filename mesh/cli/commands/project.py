"""Project resource commands: create, list, show, leads, update, pause, resume, cancel, set-lead, complete, note, deliver, wait."""

import argparse
import json
import os
import sys
from typing import Any, Dict

from mesh.client import MeshClient
from mesh.errors import MeshError
import mesh.constants as c


def register_project_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    project_parser = subparsers.add_parser(
        "project",
        parents=[parent_parser],
        help="Manage project lifecycle, lead delegation, and completion",
        description="Manage project lifecycle, lead delegation, and completion.",
        epilog="Examples:\n  mesh project list\n  mesh project show prj_123\n  mesh project pause prj_123\n  mesh project set-lead prj_123 lead2 --reason \"Handover\"\n",
    )
    sub = project_parser.add_subparsers(dest="verb", metavar="<verb>")

    # create
    create_parser = sub.add_parser(
        "create",
        parents=[parent_parser],
        help="Create a new project (alias for intake submit)",
        description="Create a new project (alias for intake submit).",
        epilog="Examples:\n  mesh project create --title \"Feature\" --goal \"Build feature\" --criterion \"Tests pass\"\n",
    )
    create_parser.add_argument("--title", required=True, help="Project title")
    create_parser.add_argument("--goal", required=True, help="Project goal description")
    create_parser.add_argument("--criterion", action="append", default=[], help="Acceptance criterion (repeatable)")
    create_parser.add_argument("--lead", help="Designated lead agent identifier")
    create_parser.add_argument("--repo", help="Git repository root path (repo project, plan §10.2)")
    create_parser.add_argument("--base-branch", help="Base git branch")
    create_parser.add_argument("--test-command", help="Test command JSON or 'none'")
    create_parser.add_argument("--depends-on-project", action="append", default=[], help="Project dependency ID")
    create_parser.set_defaults(func=handle_project_create)

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List projects with optional status filtering",
        description="List projects with optional status filtering.",
        epilog="Examples:\n  mesh project list\n  mesh project list --status active\n",
    )
    list_parser.add_argument("--status", help="Filter by status (active, completed, cancelled, paused)")
    list_parser.set_defaults(func=handle_project_list)

    # show
    show_parser = sub.add_parser(
        "show",
        parents=[parent_parser],
        help="Show details, criteria, and progress of a project",
        description="Show details, criteria, and progress of a project.",
        epilog="Examples:\n  mesh project show prj_123\n  mesh project show prj_123 --history\n",
    )
    show_parser.add_argument("project_id", help="Project identifier")
    show_parser.add_argument("--history", action="store_true", default=False, help="Include lead handover and state history")
    show_parser.set_defaults(func=handle_project_show)

    # leads
    leads_parser = sub.add_parser(
        "leads",
        parents=[parent_parser],
        help="Show lead assignment history for a project",
        description="Show lead assignment history for a project.",
        epilog="Examples:\n  mesh project leads prj_123\n",
    )
    leads_parser.add_argument("project_id", help="Project identifier")
    leads_parser.set_defaults(func=handle_project_leads)

    # update
    update_parser = sub.add_parser(
        "update",
        parents=[parent_parser],
        help="Update project goal or acceptance criteria",
        description="Update project goal or acceptance criteria.",
        epilog="Examples:\n  mesh project update prj_123 --goal \"Revised goal\"\n",
    )
    update_parser.add_argument("project_id", help="Project identifier")
    update_parser.add_argument("--goal", help="New project goal")
    update_parser.add_argument("--criterion", action="append", default=[], help="Additional criterion")
    update_parser.set_defaults(func=handle_project_update)

    # pause
    pause_parser = sub.add_parser(
        "pause",
        parents=[parent_parser],
        help="Pause a project, preventing queued tasks from starting",
        description="Pause a project, preventing queued tasks from starting.",
        epilog="Examples:\n  mesh project pause prj_123\n  mesh project pause prj_123 --user\n",
    )
    pause_parser.add_argument("project_id", help="Project identifier")
    pause_parser.set_defaults(func=handle_project_pause)

    # resume
    resume_parser = sub.add_parser(
        "resume",
        parents=[parent_parser],
        help="Resume a paused project",
        description="Resume a paused project.",
        epilog="Examples:\n  mesh project resume prj_123\n  mesh project resume prj_123 --user\n",
    )
    resume_parser.add_argument("project_id", help="Project identifier")
    resume_parser.set_defaults(func=handle_project_resume)

    # cancel
    cancel_parser = sub.add_parser(
        "cancel",
        parents=[parent_parser],
        help="Cancel an active project and its tasks",
        description="Cancel an active project and its tasks.",
        epilog="Examples:\n  mesh project cancel prj_123\n  mesh project cancel prj_123 --cancel-running --user\n",
    )
    cancel_parser.add_argument("project_id", help="Project identifier")
    cancel_parser.add_argument("--cancel-running", action="store_true", default=False, help="Accepted for compatibility (plan §13); running tasks are always cancelled through cancelling (P5)")
    cancel_parser.set_defaults(func=handle_project_cancel)

    # set-lead
    set_lead_parser = sub.add_parser(
        "set-lead",
        parents=[parent_parser],
        help="Change the designated lead agent of a project",
        description="Change the designated lead agent of a project.",
        epilog="Examples:\n  mesh project set-lead prj_123 lead2 --reason \"Shift change\" --user\n",
    )
    set_lead_parser.add_argument("project_id", help="Project identifier")
    set_lead_parser.add_argument("agent", help="New lead agent identifier")
    set_lead_parser.add_argument("--reason", required=True, help="Reason for handover")
    set_lead_parser.set_defaults(func=handle_project_set_lead)

    # complete
    complete_parser = sub.add_parser(
        "complete",
        parents=[parent_parser],
        help="Mark a project completed with a structured criteria verification report",
        description="Mark a project completed with a structured criteria verification report.",
        epilog="Examples:\n  mesh project complete prj_123 --report report.json\n  mesh project complete prj_123 --report -\n",
    )
    complete_parser.add_argument("project_id", help="Project identifier")
    complete_parser.add_argument("--report", required=True, help="Path to report JSON file or '-' for stdin")
    complete_parser.set_defaults(func=handle_project_complete)

    # note
    note_parser = sub.add_parser(
        "note",
        parents=[parent_parser],
        help="Add an audit note to the project record",
        description="Add an audit note to the project record.",
        epilog="Examples:\n  mesh project note prj_123 --text \"Reviewed design doc\"\n",
    )
    note_parser.add_argument("project_id", help="Project identifier")
    note_parser.add_argument("--text", required=True, help="Note content text")
    note_parser.set_defaults(func=handle_project_note)

    # deliver
    deliver_parser = sub.add_parser(
        "deliver",
        parents=[parent_parser],
        help="Deliver project changes to base branch (fast-forward merge)",
        description="Deliver project changes to base branch (fast-forward merge).",
        epilog="Examples:\n  mesh project deliver prj_123\n",
    )
    deliver_parser.add_argument("project_id", help="Project identifier")
    deliver_parser.set_defaults(func=handle_project_deliver)

    # wait
    wait_parser = sub.add_parser(
        "wait",
        parents=[parent_parser],
        help="Block until project reaches a specified state or timeout",
        description="Block until project reaches a specified state or timeout.",
        epilog="Examples:\n  mesh project wait prj_123 --until completed --timeout 60s\n",
    )
    wait_parser.add_argument("project_id", help="Project identifier")
    wait_parser.add_argument("--until", choices=["completed", "terminal"], default="completed", help="Target state")
    wait_parser.set_defaults(func=handle_project_wait)


def handle_project_wait(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "dry_run", False):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--dry-run is not supported on 'project wait'",
            details={"param": "dry_run"},
        )
    timeout_val = getattr(args, "timeout", None)
    if not timeout_val:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--timeout is required for project wait",
            details={"param": "timeout"},
        )
    client = MeshClient(home=args.home)
    payload = {
        "project_id": args.project_id,
        "until": getattr(args, "until", "completed"),
        "timeout": timeout_val,
    }
    resp = client.execute_cmd(args, "project.wait", payload)
    return resp.get("data", {})


def _abs_repo(repo: Any) -> Any:
    """The daemon has its own cwd: send the repo as an absolute path (realpath is the daemon's job)."""
    if isinstance(repo, str) and repo:
        return os.path.abspath(repo)
    return repo


def handle_project_create(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "repo": _abs_repo(getattr(args, "repo", None)),
        "title": getattr(args, "title", None),
        "goal": getattr(args, "goal", None),
        "criterion": getattr(args, "criterion", None) or getattr(args, "criteria", None) or [],
        "lead": getattr(args, "lead", None),
        "base_branch": getattr(args, "base_branch", None),
        "test_command": getattr(args, "test_command", None),
        "depends_on_project": getattr(args, "depends_on_project", None) or getattr(args, "depends_on_projects", None) or [],
        "origin_ref": getattr(args, "origin_ref", None),
    }
    resp = client.execute_cmd(args, "project.create", payload)
    return resp.get("data", {})


def handle_project_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "project.list", {"status": getattr(args, "status", None)})
    return resp.get("data", {})


def handle_project_show(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(
        args,
        "project.show",
        {"project_id": args.project_id, "history": getattr(args, "history", False)},
    )
    return resp.get("data", {})


def handle_project_leads(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "project.leads", {"project_id": args.project_id})
    return resp.get("data", {})


def handle_project_update(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload: Dict[str, Any] = {
        "project_id": args.project_id,
    }
    if getattr(args, "goal", None) is not None:
        payload["goal"] = args.goal
    criterion = getattr(args, "criterion", [])
    if criterion:
        payload["criterion"] = criterion
    resp = client.execute_cmd(args, "project.update", payload)
    return resp.get("data", {})


def handle_project_pause(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "project.pause", {"project_id": args.project_id})
    return resp.get("data", {})


def handle_project_resume(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "project.resume", {"project_id": args.project_id})
    return resp.get("data", {})


def handle_project_cancel(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(
        args,
        "project.cancel",
        {"project_id": args.project_id, "cancel_running": getattr(args, "cancel_running", False)},
    )
    return resp.get("data", {})


def handle_project_set_lead(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(
        args,
        "project.set_lead",
        {"project_id": args.project_id, "agent": args.agent, "reason": args.reason},
    )
    return resp.get("data", {})


def handle_project_complete(args: argparse.Namespace) -> Dict[str, Any]:
    report_arg = getattr(args, "report", "")
    if report_arg == "-":
        report_raw = sys.stdin.read()
    else:
        try:
            with open(report_arg, "r", encoding="utf-8") as f:
                report_raw = f.read()
        except Exception as exc:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Failed to read report file '{report_arg}': {exc}",
            )

    client = MeshClient(home=args.home)
    resp = client.execute_cmd(
        args,
        "project.complete",
        {"project_id": args.project_id, "report": report_raw},
    )
    return resp.get("data", {})


def handle_project_note(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(
        args,
        "project.note",
        {"project_id": args.project_id, "text": args.text},
    )
    return resp.get("data", {})


def handle_project_deliver(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    return client.execute_cmd(args, "project.deliver", {"project_id": args.project_id}).get("data", {})
