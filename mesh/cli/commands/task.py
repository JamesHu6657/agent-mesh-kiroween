"""Task resource commands: create, list, show, log, assign, reassign, start, heartbeat, progress, submit, fail, accept, reject, retry, escalate, cancel, merge, wait."""

import argparse
from typing import Any, Dict
from mesh.client import MeshClient
import mesh.constants as c
from mesh.errors import MeshError, NotImplementedInM1Error


def register_task_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    task_parser = subparsers.add_parser(
        "task",
        parents=[parent_parser],
        help="Manage discrete work units, assignments, execution, and reviews",
        description="Manage discrete work units, assignments, execution, and reviews.",
        epilog="Examples:\n  mesh task list --project prj_123\n  mesh task show tsk_456\n  mesh task create --project prj_123 --title \"Fix bug\" --spec \"Fix NPE in auth\" --assignee fx\n",
    )
    sub = task_parser.add_subparsers(dest="verb", metavar="<verb>")

    # create
    create_parser = sub.add_parser(
        "create",
        parents=[parent_parser],
        help="Create a new task under a project",
        description="Create a new task under a project. Flags map directly to JSON keys when using --input -.",
        epilog="Examples:\n  mesh task create --project prj_123 --title \"Write tests\" --spec \"Cover edge cases\" --assignee fx\n  mesh task create --project prj_123 --title \"Deploy\" --spec \"Run deployment\" --profile elevated\n",
    )
    create_parser.add_argument("--project", default=None, help="Parent project identifier (JSON key: project)")
    create_parser.add_argument("--title", default=None, help="Task title (JSON key: title)")
    create_parser.add_argument("--spec", default=None, help="Task specification text or '-' for stdin (JSON key: spec)")
    create_parser.add_argument("--kind", help="Task kind classification (JSON key: kind)")
    create_parser.add_argument("--assignee", help="Agent identifier to assign task to (JSON key: assignee)")
    create_parser.add_argument("--model", help="Model override for task execution (JSON key: model)")
    create_parser.add_argument("--effort", help="Reasoning effort level (JSON key: effort)")
    create_parser.add_argument("--profile", help="Tool permission profile (JSON key: profile)")
    create_parser.add_argument("--idle-timeout", help="Idle timeout (JSON key: idle_timeout)")
    create_parser.add_argument("--max-attempts", type=int, help="Maximum execution attempts (JSON key: max_attempts)")
    create_parser.add_argument("--priority", type=int, help="Task priority (higher runs first, JSON key: priority)")
    create_parser.add_argument("--depends-on", action="append", default=[], help="Prerequisite task ID (repeatable, JSON key: depends_on)")
    create_parser.set_defaults(func=handle_task_create)

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List tasks with optional project, status, and assignee filters",
        description="List tasks with optional project, status, and assignee filters.",
        epilog="Examples:\n  mesh task list --project prj_123\n  mesh task list --status running\n  mesh task list --assignee fx\n",
    )
    list_parser.add_argument("--project", help="Filter by project ID")
    list_parser.add_argument("--status", help="Filter by status (assigned, queued, running, submitted, accepted, failed, etc.)")
    list_parser.add_argument("--assignee", help="Filter by assignee agent ID")
    list_parser.set_defaults(func=handle_task_list)

    # show
    show_parser = sub.add_parser(
        "show",
        parents=[parent_parser],
        help="Display full status, run history, and deliverables for a task",
        description="Display full status, run history, and deliverables for a task.",
        epilog="Examples:\n  mesh task show tsk_123\n",
    )
    show_parser.add_argument("task_id", help="Task identifier")
    show_parser.set_defaults(func=handle_task_show)

    # log
    log_parser = sub.add_parser(
        "log",
        parents=[parent_parser],
        help="Retrieve logs from a task execution run",
        description="Retrieve logs from a task execution run.",
        epilog="Examples:\n  mesh task log tsk_123\n  mesh task log tsk_123 --tail 50 --stream stderr\n",
    )
    log_parser.add_argument("task_id", help="Task identifier")
    log_parser.add_argument("--run", help="Specific run ID (defaults to latest)")
    log_parser.add_argument("--stream", choices=["stdout", "stderr"], help="Filter by log stream")
    log_parser.add_argument("--tail", type=int, help="Number of trailing lines to return")
    log_parser.set_defaults(func=handle_task_log)

    # assign
    assign_parser = sub.add_parser(
        "assign",
        parents=[parent_parser],
        help="Assign a task to an agent",
        description="Assign a task to an agent.",
        epilog="Examples:\n  mesh task assign tsk_123 --to fx\n",
    )
    assign_parser.add_argument("task_id", help="Task identifier")
    assign_parser.add_argument("--to", required=True, help="Target agent identifier")
    assign_parser.add_argument("--profile", help="Permission profile override")
    assign_parser.add_argument("--model", help="Model override")
    assign_parser.add_argument("--effort", help="Reasoning effort override")
    assign_parser.set_defaults(func=handle_task_assign)

    # reassign
    reassign_parser = sub.add_parser(
        "reassign",
        parents=[parent_parser],
        help="Reassign a task to a different agent",
        description="Reassign a task to a different agent.",
        epilog="Examples:\n  mesh task reassign tsk_123 --to ibx --reason \"Specialist needed\"\n",
    )
    reassign_parser.add_argument("task_id", help="Task identifier")
    reassign_parser.add_argument("--to", required=True, help="New target agent identifier")
    reassign_parser.add_argument("--reason", required=True, help="Reason for reassignment")
    reassign_parser.add_argument("--reuse-workspace", action="store_true", default=False, help="Preserve existing workspace directory")
    reassign_parser.set_defaults(func=handle_task_reassign)

    # start
    start_parser = sub.add_parser(
        "start",
        parents=[parent_parser],
        help="Start executing a queued task (inbox delivery mode)",
        description="Start executing a queued task (inbox delivery mode).",
        epilog="Examples:\n  mesh task start tsk_123\n",
    )
    start_parser.add_argument("task_id", help="Task identifier")
    start_parser.set_defaults(func=handle_task_start)

    # heartbeat
    heartbeat_parser = sub.add_parser(
        "heartbeat",
        parents=[parent_parser],
        help="Send execution heartbeat to extend lease (inbox delivery mode)",
        description="Send execution heartbeat to extend lease (inbox delivery mode).",
        epilog="Examples:\n  mesh task heartbeat tsk_123 --run run_123 --fence 1\n",
    )
    heartbeat_parser.add_argument("task_id", help="Task identifier")
    heartbeat_parser.add_argument("--run", help="Run identifier")
    heartbeat_parser.add_argument("--fence", type=int, help="Lease fence sequence number")
    heartbeat_parser.set_defaults(func=handle_task_heartbeat)

    # progress
    progress_parser = sub.add_parser(
        "progress",
        parents=[parent_parser],
        help="Report execution progress message",
        description="Report execution progress message.",
        epilog="Examples:\n  mesh task progress tsk_123 --message \"Parsed 50% of files\"\n",
    )
    progress_parser.add_argument("task_id", help="Task identifier")
    progress_parser.add_argument("--message", required=True, help="Progress description text")
    progress_parser.add_argument("--run", help="Run identifier")
    progress_parser.add_argument("--fence", type=int, help="Fence sequence number")
    progress_parser.set_defaults(func=handle_task_progress)

    # submit
    submit_parser = sub.add_parser(
        "submit",
        parents=[parent_parser],
        help="Submit completed task deliverables for lead review",
        description="Submit completed task deliverables for lead review.",
        epilog=(
            "Examples:\n"
            "  mesh task submit tsk_123 --deliverable out.json --summary \"Implemented export\"\n"
            "  mesh task submit tsk_123 --deliverable '{\"result\": \"ok\"}' --run run_1 --fence 1\n"
            "\n"
            "--deliverable accepts a file path, '-' for stdin, or an inline JSON object\n"
            "when the argument is not an existing path. When both --deliverable and\n"
            "--summary are given, the summary is merged into the deliverable as its\n"
            "'summary' key (an explicit 'summary' key in the JSON wins).\n"
        ),
    )
    submit_parser.add_argument("task_id", help="Task identifier")
    submit_parser.add_argument("--deliverable", help="Deliverable file path, '-' for stdin, or inline JSON object")
    submit_parser.add_argument("--summary", help="Summary of work completed (merged into the deliverable)")
    submit_parser.add_argument("--run", help="Run identifier")
    submit_parser.add_argument("--fence", type=int, help="Fence sequence number")
    submit_parser.set_defaults(func=handle_task_submit)

    # fail
    fail_parser = sub.add_parser(
        "fail",
        parents=[parent_parser],
        help="Report task execution failure",
        description="Report task execution failure.",
        epilog="Examples:\n  mesh task fail tsk_123 --reason \"Compilation error\"\n",
    )
    fail_parser.add_argument("task_id", help="Task identifier")
    fail_parser.add_argument("--reason", required=True, help="Reason for failure")
    fail_parser.add_argument("--run", help="Run identifier")
    fail_parser.add_argument("--fence", type=int, help="Fence sequence number")
    fail_parser.set_defaults(func=handle_task_fail)

    # accept
    accept_parser = sub.add_parser(
        "accept",
        parents=[parent_parser],
        help="Accept a submitted task as meeting requirements",
        description="Accept a submitted task as meeting requirements.",
        epilog="Examples:\n  mesh task accept tsk_123\n  mesh task accept tsk_123 --note \"Approved after review\"\n",
    )
    accept_parser.add_argument("task_id", help="Task identifier")
    accept_parser.add_argument("--note", help="Approval note")
    accept_parser.set_defaults(func=handle_task_accept)

    # reject
    reject_parser = sub.add_parser(
        "reject",
        parents=[parent_parser],
        help="Reject a submitted task and request rework",
        description="Reject a submitted task and request rework.",
        epilog="Examples:\n  mesh task reject tsk_123 --reason \"Missing edge case tests\"\n",
    )
    reject_parser.add_argument("task_id", help="Task identifier")
    reject_parser.add_argument("--reason", required=True, help="Rejection rework feedback")
    reject_parser.set_defaults(func=handle_task_reject)

    # retry
    retry_parser = sub.add_parser(
        "retry",
        parents=[parent_parser],
        help="Retry a failed task with optional model/effort adjustments",
        description="Retry a failed task with optional model/effort adjustments.",
        epilog="Examples:\n  mesh task retry tsk_123 --reason \"Transient failure\"\n  mesh task retry tsk_123 --effort high\n",
    )
    retry_parser.add_argument("task_id", help="Task identifier")
    retry_parser.add_argument("--model", help="New model ID")
    retry_parser.add_argument("--effort", help="New reasoning effort")
    retry_parser.add_argument("--reason", help="Retry rationale")
    retry_parser.add_argument("--reuse-workspace", action="store_true", default=False, help="Reuse previous workspace directory")
    retry_parser.set_defaults(func=handle_task_retry)

    # escalate
    escalate_parser = sub.add_parser(
        "escalate",
        parents=[parent_parser],
        help="Escalate a task issue to the human user and project origin",
        description="Escalate a task issue to the human user and project origin.",
        epilog="Examples:\n  mesh task escalate tsk_123 --reason \"Blocked by third-party service outage\"\n",
    )
    escalate_parser.add_argument("task_id", help="Task identifier")
    escalate_parser.add_argument("--reason", required=True, help="Escalation explanation")
    escalate_parser.set_defaults(func=handle_task_escalate)

    # cancel
    cancel_parser = sub.add_parser(
        "cancel",
        parents=[parent_parser],
        help="Cancel a task",
        description="Cancel a task.",
        epilog="Examples:\n  mesh task cancel tsk_123 --reason \"No longer needed\"\n",
    )
    cancel_parser.add_argument("task_id", help="Task identifier")
    cancel_parser.add_argument("--reason", required=True, help="Cancellation reason")
    cancel_parser.set_defaults(func=handle_task_cancel)

    # merge (M2)
    merge_parser = sub.add_parser(
        "merge",
        parents=[parent_parser],
        help="Merge an accepted task worktree branch into project integration branch (M2)",
        description="Merge an accepted task worktree branch into project integration branch (M2).",
        epilog="Examples:\n  mesh task merge tsk_123\n",
    )
    merge_parser.add_argument("task_id", help="Task identifier")
    merge_parser.set_defaults(func=handle_task_merge)

    # wait
    wait_parser = sub.add_parser(
        "wait",
        parents=[parent_parser],
        help="Block until task reaches a target state or terminal condition",
        description="Block until task reaches a target state or terminal condition.",
        epilog="Examples:\n  mesh task wait tsk_123 --until submitted --timeout 60s\n  mesh task wait tsk_123 --until terminal\n",
    )
    wait_parser.add_argument("task_id", help="Task identifier")
    wait_parser.add_argument("--until", default="submitted", help="Target state (submitted, terminal, or state:<s>)")
    wait_parser.set_defaults(func=handle_task_wait)


def handle_task_wait(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "dry_run", False):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--dry-run is not supported on 'task wait'",
            details={"param": "dry_run"},
        )
    timeout_val = getattr(args, "timeout", None)
    if not timeout_val:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--timeout is required for task wait",
            details={"param": "timeout"},
        )
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "until": getattr(args, "until", "submitted"),
        "timeout": timeout_val,
    }
    resp = client.execute_cmd(args, "task.wait", payload)
    return resp.get("data", {})


def handle_task_create(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "project": getattr(args, "project", None),
        "title": getattr(args, "title", None),
        "spec": getattr(args, "spec", None),
        "kind": getattr(args, "kind", None),
        "assignee": getattr(args, "assignee", None),
        "model": getattr(args, "model", None),
        "effort": getattr(args, "effort", None),
        "profile": getattr(args, "profile", None),
        "timeout": getattr(args, "timeout", None),
        "idle_timeout": getattr(args, "idle_timeout", None),
        "max_attempts": getattr(args, "max_attempts", None),
        "priority": getattr(args, "priority", None),
        "depends_on": getattr(args, "depends_on", None) or [],
    }
    resp = client.execute_cmd(args, "task.create", payload)
    return resp.get("data", {})


def handle_task_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "project": getattr(args, "project", None),
        "status": getattr(args, "status", None),
        "assignee": getattr(args, "assignee", None),
    }
    resp = client.execute_cmd(args, "task.list", payload)
    return resp.get("data", {})


def handle_task_show(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "task.show", {"task_id": args.task_id})
    return resp.get("data", {})


def handle_task_log(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "run": getattr(args, "run", None),
        "stream": getattr(args, "stream", None),
        "tail": getattr(args, "tail", None),
    }
    return client.execute_cmd(args, "task.log", payload).get("data", {})


def handle_task_assign(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "to": getattr(args, "to", None),
        "profile": getattr(args, "profile", None),
        "model": getattr(args, "model", None),
        "effort": getattr(args, "effort", None),
    }
    resp = client.execute_cmd(args, "task.assign", payload)
    return resp.get("data", {})


def handle_task_cancel(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(
        args,
        "task.cancel",
        {"task_id": args.task_id, "reason": getattr(args, "reason", None)},
    )
    return resp.get("data", {})


# `task merge` waits for the whole pipeline (§10.5). The client timeout is the project
# test timeout + 120 s (m2-plan r5). `test_timeout_s` is the schema default (1800 s) and is
# not settable in M2 yet, so the CLI uses that value without an extra round trip.
DEFAULT_TEST_TIMEOUT_S = 1800
MERGE_CLIENT_SLACK_S = 120


def handle_task_merge(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home, timeout=float(DEFAULT_TEST_TIMEOUT_S + MERGE_CLIENT_SLACK_S))
    resp = client.execute_cmd(args, "task.merge", {"task_id": args.task_id})
    return resp.get("data", {})


def handle_task_reassign(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "to": getattr(args, "to", None),
        "reason": getattr(args, "reason", None),
        "reuse_workspace": getattr(args, "reuse_workspace", False),
    }
    resp = client.execute_cmd(args, "task.reassign", payload)
    return resp.get("data", {})


def handle_task_retry(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "model": getattr(args, "model", None),
        "effort": getattr(args, "effort", None),
        "reason": getattr(args, "reason", None),
        "reuse_workspace": getattr(args, "reuse_workspace", False),
    }
    resp = client.execute_cmd(args, "task.retry", payload)
    return resp.get("data", {})


def handle_task_escalate(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "reason": getattr(args, "reason", None),
    }
    resp = client.execute_cmd(args, "task.escalate", payload)
    return resp.get("data", {})


def handle_task_start(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {"task_id": args.task_id}
    resp = client.execute_cmd(args, "task.start", payload)
    return resp.get("data", {})


def _fill_run_from_env(args: argparse.Namespace) -> None:
    """Inside a launched run, default --run/--fence from the run env (plan §7.1, §12.5).

    The executor's prompt tells it credentials are already in its environment, so
    MESH_RUN_ID and MESH_FENCE stand in for missing flags. Explicit flags win; the
    daemon still checks scope and fence.
    """
    import os

    if not getattr(args, "run", None) and os.environ.get("MESH_RUN_ID"):
        args.run = os.environ["MESH_RUN_ID"]
    if getattr(args, "fence", None) is None and os.environ.get("MESH_FENCE", "").isdigit():
        args.fence = int(os.environ["MESH_FENCE"])


def handle_task_heartbeat(args: argparse.Namespace) -> Dict[str, Any]:
    _fill_run_from_env(args)
    if not getattr(args, "run", None):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task heartbeat", details={"param": "run"})
    if getattr(args, "fence", None) is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task heartbeat", details={"param": "fence"})
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "run": args.run,
        "fence": args.fence,
    }
    resp = client.execute_cmd(args, "task.heartbeat", payload)
    return resp.get("data", {})


def handle_task_progress(args: argparse.Namespace) -> Dict[str, Any]:
    _fill_run_from_env(args)
    if not getattr(args, "run", None):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task progress", details={"param": "run"})
    if getattr(args, "fence", None) is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task progress", details={"param": "fence"})
    if not getattr(args, "message", None):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--message is required for task progress", details={"param": "message"})
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "message": args.message,
        "run": args.run,
        "fence": args.fence,
    }
    resp = client.execute_cmd(args, "task.progress", payload)
    return resp.get("data", {})


def handle_task_submit(args: argparse.Namespace) -> Dict[str, Any]:
    import json
    import os
    import sys

    _fill_run_from_env(args)
    if not getattr(args, "run", None):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task submit", details={"param": "run"})
    if getattr(args, "fence", None) is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task submit", details={"param": "fence"})

    deliverable_arg = getattr(args, "deliverable", None)
    summary_arg = getattr(args, "summary", None)

    deliverable_data = None
    if deliverable_arg is not None:
        if deliverable_arg == "-":
            raw_content = sys.stdin.read()
        elif os.path.exists(deliverable_arg):
            with open(deliverable_arg, "r", encoding="utf-8") as fp:
                raw_content = fp.read()
        else:
            raw_content = deliverable_arg
        try:
            deliverable_data = json.loads(raw_content)
        except Exception:
            raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Deliverable must be valid JSON", details={"param": "deliverable"})
        if not isinstance(deliverable_data, dict):
            raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Deliverable must be a JSON object", details={"param": "deliverable"})
        if len(json.dumps(deliverable_data).encode("utf-8")) > 200 * 1024:
            raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Deliverable exceeds 200 KB limit", details={"param": "deliverable", "bytes": len(json.dumps(deliverable_data).encode("utf-8")), "limit": 204800})
    elif summary_arg is not None:
        deliverable_data = {"summary": summary_arg}
    else:
        deliverable_data = {}

    # Both given: merge the summary into the deliverable instead of dropping it.
    if deliverable_arg is not None and summary_arg is not None:
        deliverable_data = dict(deliverable_data)
        deliverable_data.setdefault("summary", summary_arg)

    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "deliverable": deliverable_data,
        "summary": summary_arg,
        "run": args.run,
        "fence": args.fence,
    }
    resp = client.execute_cmd(args, "task.submit", payload)
    return resp.get("data", {})


def handle_task_fail(args: argparse.Namespace) -> Dict[str, Any]:
    _fill_run_from_env(args)
    if not getattr(args, "run", None):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task fail", details={"param": "run"})
    if getattr(args, "fence", None) is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task fail", details={"param": "fence"})
    if not getattr(args, "reason", None):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--reason is required for task fail", details={"param": "reason"})
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "reason": args.reason,
        "run": args.run,
        "fence": args.fence,
    }
    resp = client.execute_cmd(args, "task.fail", payload)
    return resp.get("data", {})


def handle_task_accept(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "note": getattr(args, "note", None),
    }
    resp = client.execute_cmd(args, "task.accept", payload)
    return resp.get("data", {})


def handle_task_reject(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "task_id": args.task_id,
        "reason": getattr(args, "reason", None),
    }
    resp = client.execute_cmd(args, "task.reject", payload)
    return resp.get("data", {})


def handle_task(verb: str, milestone: str) -> Dict[str, Any]:
    raise NotImplementedInM1Error(verb, milestone)
