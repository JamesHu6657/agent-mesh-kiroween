"""Task resource commands: create, list, show, log, assign, reassign, start, heartbeat, keepalive, progress, submit, fail, accept, reject, retry, escalate, cancel, close, merge, wait."""

import argparse
import threading
import sys
import os
from typing import Any, Dict, Optional
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
        epilog="Examples:\n  mesh task create --project prj_123 --title \"Write tests\" --spec \"Cover edge cases\" --assignee fx\n  mesh task create --project prj_123 --title \"Refactor\" --spec - --assignee fx < spec.md\n  mesh task create --project prj_123 --title \"Deploy\" --spec \"Run deployment\" --profile elevated\n",
    )
    create_parser.add_argument("--project", default=None, help="Parent project identifier (JSON key: project)")
    create_parser.add_argument("--title", default=None, help="Task title (JSON key: title)")
    create_parser.add_argument("--spec", default=None, help="Task specification text, or '-' to read it from stdin (JSON key: spec; a JSON value is taken literally)")
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
    list_parser.add_argument("--status", help="Filter by status, or a comma list (e.g. submitted,accepted); "
                                              "assigned, queued, running, submitted, accepted, failed, etc.")
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

    # keepalive
    keepalive_parser = sub.add_parser(
        "keepalive",
        parents=[parent_parser],
        help="Heartbeat a started inbox task in a loop until its run ends",
        description=(
            "Keep an inbox run's lease alive: send `task heartbeat` every N seconds until\n"
            "the run ends. Run it in the background right after `task start` and leave it;\n"
            "it stops by itself when you submit/fail the task or the run ends any other way."
        ),
        epilog=(
            "Examples:\n"
            "  mesh task keepalive tsk_123 --run run_123 --fence 1 --token-file $T > keepalive.log 2>&1 &\n"
            "  mesh task keepalive tsk_123 --run run_123 --fence 1 --every 30 --token-file $T\n"
            "\n"
            "--every defaults to heartbeat_every_s from `task start` (a third of lease.ttl_s).\n"
            "Each beat is logged to stderr. When the run ends the final task state is printed\n"
            "and the exit code is:\n"
            "  0  the run ended normally (submitted, failed by the worker, cancelled, ...)\n"
            "  9  the lease was lost (LEASE_EXPIRED: no heartbeat within lease.ttl_s) or the\n"
            "     task was restarted with a newer run/fence (FENCE_MISMATCH); stop working on\n"
            "     this run, your writes will be refused\n"
            "SIGINT/SIGTERM stop the loop (exit 0); the lease then runs out after lease.ttl_s.\n"
        ),
    )
    keepalive_parser.add_argument("task_id", help="Task identifier")
    keepalive_parser.add_argument("--run", help="Run identifier (from task start)")
    keepalive_parser.add_argument("--fence", type=int, help="Lease fence (from task start)")
    keepalive_parser.add_argument("--every", type=int, metavar="SECONDS",
                                  help="Seconds between heartbeats (default: heartbeat_every_s of the run)")
    keepalive_parser.set_defaults(func=handle_task_keepalive)

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
        help="Cancel a task (--reason is required)",
        description="Cancel a task. --reason is required and is recorded in the task history.",
        epilog="Examples:\n  mesh task cancel tsk_123 --reason \"No longer needed\"\n",
    )
    cancel_parser.add_argument("task_id", help="Task identifier")
    cancel_parser.add_argument("--reason", required=True, help="Cancellation reason (required)")
    cancel_parser.set_defaults(func=handle_task_cancel)

    # close
    close_parser = sub.add_parser(
        "close",
        parents=[parent_parser],
        help="Close an accepted/merge_failed task whose work landed through another (merged) task",
        description=("Close an accepted or merge_failed code task as superseded: its work reached the "
                     "integration branch through another task's merge. The task ends 'cancelled' with a "
                     "task.closed event linking the other task (task show: superseded_by); its branch and "
                     "worktree are kept. Lead or user only."),
        epilog="Examples:\n  mesh task close tsk_123 --superseded-by tsk_456 --reason \"landed in tsk_456's merge\"\n",
    )
    close_parser.add_argument("task_id", help="Task identifier")
    close_parser.add_argument("--superseded-by", dest="superseded_by", required=True,
                              help="The merged task whose merge carried this task's work")
    close_parser.add_argument("--reason", help="Optional note recorded with the close")
    close_parser.set_defaults(func=handle_task_close)

    # merge
    merge_parser = sub.add_parser(
        "merge",
        parents=[parent_parser],
        help="Merge an accepted task worktree branch into the project integration branch",
        description="Merge an accepted task worktree branch into the project integration branch.",
        epilog="Examples:\n  mesh task merge tsk_123\n  mesh task merge --all-accepted --project prj_123\n",
    )
    merge_parser.add_argument("task_id", nargs="?", help="Task identifier (or use --all-accepted --project)")
    merge_parser.add_argument("--all-accepted", action="store_true", default=False,
                              help="Merge every accepted code task of --project, one at a time in creation "
                                   "order (each one an ordinary `task merge` with its full gate); stops at "
                                   "the first failure and prints a summary")
    merge_parser.add_argument("--project", help="Project for --all-accepted")
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


def _spec_value(args: argparse.Namespace) -> Any:
    """--spec as given, or stdin for ``--spec -`` (a JSON ``spec`` key is always literal)."""
    spec = getattr(args, "spec", None)
    if spec == "-" and "spec" not in getattr(args, "_from_json", set()):
        from mesh.cli.stdin import read_stdin
        spec = read_stdin("--spec")
        if not spec.strip():
            raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                            message="--spec -: nothing was read from stdin", details={"param": "spec"})
    return spec


def handle_task_create(args: argparse.Namespace) -> Dict[str, Any]:
    spec = _spec_value(args)
    client = MeshClient(home=args.home)
    payload = {
        "project": getattr(args, "project", None),
        "title": getattr(args, "title", None),
        "spec": spec,
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
    data = resp.get("data", {})
    if getattr(args, "format", "json") == "text" and isinstance(data.get("items"), list):
        return {"text": task_list_table(data["items"])}
    return data


def _ago(ts: Any, now: float) -> str:
    """Compact age of an ISO timestamp: 45s, 12m, 3h, 2d."""
    from mesh.core.clock import parse_utc

    try:
        secs = max(0, int(now - parse_utc(ts).timestamp()))
    except Exception:
        return "-"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def task_list_table(items: Any, now: Optional[float] = None) -> str:
    """`task list --format text`: id, status, assignee name, age (since created), time in
    the current status (since the last change), attempt, title."""
    import time as _t

    if not items:
        return "(none)"
    now = _t.time() if now is None else now
    rows = [["ID", "STATUS", "ASSIGNEE", "AGE", "IN_STATE", "ATTEMPT", "TITLE"]]
    for t in items:
        title = " ".join(str(t.get("title") or "").split())
        rows.append([str(t.get("id")), str(t.get("status")), str(t.get("assignee") or "-"),
                     _ago(t.get("created_at"), now), _ago(t.get("updated_at"), now),
                     f"{t.get('attempt') or 0}/{t.get('max_attempts') or '-'}",
                     title if len(title) <= 48 else title[:45] + "..."])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(r[i].ljust(widths[i]) for i in range(len(r))).rstrip() for r in rows)


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


def handle_task_close(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(
        args,
        "task.close",
        {"task_id": args.task_id, "superseded_by": args.superseded_by, "reason": getattr(args, "reason", None)},
    )
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


def _merge_progress_note(client: Any, args: argparse.Namespace, done: Any) -> None:
    """Text mode: once the merge has started, say on stderr that tests run and where the
    log is, so a merge that blocks for minutes is not silent."""
    import copy

    show_args = copy.copy(args)
    show_args.idempotency_key = None
    show_args.request_id = None
    show_args.dry_run = False
    for _ in range(40):
        if done.wait(0.25):
            return
        try:
            data = MeshClient(home=client.home).execute_cmd(show_args, "task.show", {"task_id": args.task_id}).get("data", {})
        except Exception:
            continue
        merge = data.get("merge") or {}
        if data.get("status") == "merging" and merge.get("mrg"):
            if done.is_set():
                return
            log = os.path.join(client.home, "merges", merge["mrg"], "test.log")
            sys.stderr.write(f"mesh: merging {args.task_id}: running the project's tests if any "
                             f"(log: {log})...\n")
            sys.stderr.flush()
            return


def handle_task_merge_all(args: argparse.Namespace) -> Dict[str, Any]:
    """`task merge --all-accepted --project P` (batch 5).

    A client-side loop over ordinary `task.merge` requests: every merge is its own
    request with the same authz (lead only), the same gate (tests, leftover scan,
    integration-branch lock) and the same events as `mesh task merge <tsk>`; nothing in
    the daemon is batched or relaxed. The list of accepted tasks is read once up front;
    a task that leaves `accepted` meanwhile fails its own merge (stop)."""
    if args.task_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message="Give either a task id or --all-accepted --project P, not both",
                        details={"param": "task_id"})
    if not getattr(args, "project", None):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message="--all-accepted needs --project P", details={"param": "project"})
    if getattr(args, "idempotency_key", None) or getattr(args, "dry_run", False):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message="--all-accepted cannot be combined with --idempotency-key or --dry-run "
                                "(it sends one merge request per task)",
                        details={"param": "all_accepted"})
    lister = MeshClient(home=args.home)
    resp = lister.execute_cmd(args, "task.list", {"project": args.project, "status": "accepted"})
    items = [t for t in resp.get("data", {}).get("items", []) if t.get("kind", "code") == "code"]
    items.sort(key=lambda t: (t.get("created_at") or "", t.get("id") or ""))
    queue = [t["id"] for t in items]
    text = getattr(args, "format", "json") == "text"
    merged: list = []
    for n, tsk in enumerate(queue):
        if text:
            print(f"merge {n + 1}/{len(queue)}: {tsk} ...", file=sys.stderr, flush=True)
        sub = argparse.Namespace(**vars(args))
        sub.task_id = tsk
        try:
            data = _merge_one(sub)
        except MeshError as e:
            summary = {"project": args.project, "merged": merged, "failed": {"task_id": tsk, "code": e.code,
                       "message": e.message, "details": e.details}, "not_attempted": queue[n + 1:]}
            raise MeshError(e.code, exit_code=e.exit_code,
                            message=(f"merge --all-accepted stopped at {tsk} ({n + 1}/{len(queue)}): {e.message} "
                                     f"[merged {len(merged)}, not attempted {len(queue) - n - 1}]"),
                            details=summary, data=e.data) from e
        merged.append({"task_id": tsk, "merge_commit": data.get("merge_commit") or data.get("commit"),
                       "status": data.get("status")})
    out = {"project": args.project, "merged": merged, "failed": None, "not_attempted": []}
    if text:
        lines = [f"merged {len(merged)} accepted task(s) in {args.project}" if merged
                 else f"no accepted code tasks in {args.project}"]
        lines += [f"  {m['task_id']}  {m.get('merge_commit') or '-'}" for m in merged]
        return {"text": "\n".join(lines)}
    return out


def handle_task_merge(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "all_accepted", False):
        return handle_task_merge_all(args)
    if not args.task_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message="task merge needs a task id (or --all-accepted --project P)",
                        details={"param": "task_id"})
    if getattr(args, "project", None):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message="--project is only used with --all-accepted", details={"param": "project"})
    return _merge_one(args)


def _merge_one(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home, timeout=float(DEFAULT_TEST_TIMEOUT_S + MERGE_CLIENT_SLACK_S))
    note = None
    done = threading.Event()
    if getattr(args, "format", "json") == "text" and not getattr(args, "dry_run", False):
        client.resolve_auth(args)  # one passphrase prompt, before the note thread exists
        note = threading.Thread(target=_merge_progress_note, args=(client, args, done), daemon=True)
        note.start()
    try:
        resp = client.execute_cmd(args, "task.merge", {"task_id": args.task_id})
    finally:
        done.set()
        if note is not None:
            note.join(timeout=2)
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


def new_session_id() -> str:
    import secrets

    return "ses_" + secrets.token_hex(8)


def session_for_key(key: str) -> str:
    import hashlib

    return "ses_" + hashlib.sha256(("mesh-task-start-session\0" + str(key)).encode("utf-8")).hexdigest()[:16]


def handle_task_start(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = _with_session({"task_id": args.task_id})
    if "session" not in payload:
        # Default-on (batch 5): a run always belongs to one client session. The id comes
        # back in the response (`export`, `keepalive`) for this session's later commands.
        # PR #37 D2: with an idempotency key the id is derived from the key, so a retry
        # after a lost response sends the same args and gets the original start replayed
        # (with its session) instead of IDEMPOTENCY_KEY_REUSED.
        key = getattr(args, "idempotency_key", None)
        payload["session"] = session_for_key(key) if key else new_session_id()
    resp = client.execute_cmd(args, "task.start", payload)
    return resp.get("data", {})


def _with_session(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add the client session (``MESH_SESSION`` env) when set: `task start` records it on
    the run and meshd refuses heartbeat/progress/submit/fail of that run from another one."""
    import os

    session = os.environ.get("MESH_SESSION", "").strip()
    if session:
        payload["session"] = session[:200]
    return payload


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
    resp = client.execute_cmd(args, "task.heartbeat", _with_session(payload))
    return resp.get("data", {})


#: Errors that mean this run's lease is gone for good (exit 9).
_LEASE_LOST_CODES = (c.LEASE_EXPIRED, c.FENCE_MISMATCH)


def _keepalive_log(msg: str) -> None:
    import sys
    import time as _t

    sys.stderr.write(f"{_t.strftime('%Y-%m-%dT%H:%M:%SZ', _t.gmtime())} keepalive: {msg}\n")
    sys.stderr.flush()


def _keepalive_args(args: argparse.Namespace) -> argparse.Namespace:
    """A copy of the CLI args for one daemon call: never replay a heartbeat."""
    import copy

    a = copy.copy(args)
    a.idempotency_key = None
    a.request_id = None
    a.dry_run = False
    return a


def handle_task_keepalive(args: argparse.Namespace) -> Dict[str, Any]:
    """Heartbeat until the run ends (CLI-side loop over `task.heartbeat`)."""
    import signal
    import time as _t

    _fill_run_from_env(args)
    if not getattr(args, "run", None):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task keepalive", details={"param": "run"})
    if getattr(args, "fence", None) is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task keepalive", details={"param": "fence"})
    every = getattr(args, "every", None)
    if every is not None and every < 1:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--every must be at least 1 second",
                        details={"param": "every", "got": every})
    if getattr(args, "dry_run", False):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--dry-run is not supported by task keepalive",
                        details={"param": "dry_run"})

    client = MeshClient(home=args.home)
    payload = _with_session({"task_id": args.task_id, "run": args.run, "fence": args.fence})
    stop = {"signal": None}

    def _on_signal(signum, _frame):  # pragma: no cover - exercised by integration test
        stop["signal"] = signum

    old = {sig: signal.signal(sig, _on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    beats = 0
    ttl_s: Optional[int] = None
    last_ok = _t.monotonic()
    try:
        while stop["signal"] is None:
            try:
                resp = client.execute_cmd(_keepalive_args(args), "task.heartbeat", payload)
                beats += 1
                last_ok = _t.monotonic()
                expires = (resp.get("data") or {}).get("expires_at")
                if every is None or ttl_s is None:
                    live = _keepalive_live_run(client, args)
                    if live:
                        ttl_s = live.get("lease_ttl_s")
                        if every is None:
                            every = int(live.get("heartbeat_every_s") or 40)
                    elif every is None:
                        every = 40
                    if ttl_s and every >= ttl_s:
                        _keepalive_log(f"warning: --every {every}s is not shorter than lease_ttl_s {ttl_s}s; the lease may expire between beats")
                _keepalive_log(f"heartbeat {beats} ok, lease expires at {expires}; next in {every}s")
            except MeshError as exc:
                if exc.code in _LEASE_LOST_CODES:
                    return _keepalive_end(client, args, exc, beats)
                # Transient (daemon restarting, socket timeout): keep trying until the
                # lease could have run out, then give up with the last error.
                waited = _t.monotonic() - last_ok
                limit = ttl_s or 120
                _keepalive_log(f"heartbeat failed ({exc.code}: {exc.message}); retrying")
                if waited > limit:
                    raise MeshError(exc.code, exit_code=exc.exit_code,
                                    message=f"task keepalive gave up: no successful heartbeat for {int(waited)}s "
                                            f"(lease_ttl_s {limit}); last error: {exc.message}",
                                    details={"task_id": args.task_id, "run_id": args.run, "beats": beats,
                                             "last_error": exc.code})
                if every is None:
                    every = 40
            slept = 0.0
            step = min(1.0, float(every))
            while slept < every and stop["signal"] is None:
                _t.sleep(step)
                slept += step
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    _keepalive_log(f"stopped by signal {stop['signal']} after {beats} heartbeats; the lease expires after lease_ttl_s")
    return {"task_id": args.task_id, "run_id": args.run, "fence": args.fence, "beats": beats,
            "ended": "stopped", "signal": stop["signal"]}


def _keepalive_live_run(client: "MeshClient", args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    try:
        shown = client.execute_cmd(_keepalive_args(args), "task.show", {"task_id": args.task_id})
    except MeshError:
        return None
    return (shown.get("data") or {}).get("live_run")


def _keepalive_end(client: "MeshClient", args: argparse.Namespace, exc: MeshError, beats: int) -> Dict[str, Any]:
    """The heartbeat was refused: report how this run ended.

    The run itself says why: it ended normally (submit, worker fail, cancel, ...) ->
    exit 0 with the final run and task state; it was lost because the lease expired,
    or a newer run/fence holds the task -> exit 9 with a message that says so.
    """
    def _show(cmd: str, key: str, ident: str) -> Dict[str, Any]:
        try:
            return client.execute_cmd(_keepalive_args(args), cmd, {key: ident}).get("data") or {}
        except MeshError:
            return {}

    run = _show("run.show", "run_id", args.run)
    task = _show("task.show", "task_id", args.task_id)
    result: Dict[str, Any] = {"task_id": args.task_id, "run_id": args.run, "fence": args.fence, "beats": beats,
                              "task_status": task.get("status"), "run_status": run.get("status"),
                              "end_reason": run.get("end_reason")}
    if run.get("end_reason") == "lease_expired" or (not run and exc.code == c.LEASE_EXPIRED):
        _keepalive_log("lease lost")
        raise MeshError(c.LEASE_EXPIRED, exit_code=c.EXIT_STALE,
                        message=f"Lease on task {args.task_id} expired: no heartbeat within lease_ttl_s, so run "
                                f"{args.run} was ended (task is now {task.get('status') or 'unknown'}). Stop working "
                                f"on this run; its writes will be refused. Ask the lead to retry the task.",
                        details={**result, "reason": "lease_expired"})
    if run.get("status") in ("starting", "running") or exc.code == c.FENCE_MISMATCH and not run.get("ended_at"):
        # Our run still looks live but the lease belongs to another run/fence.
        _keepalive_log(f"run superseded: {exc.message}")
        raise MeshError(c.FENCE_MISMATCH, exit_code=c.EXIT_STALE,
                        message=f"Run {args.run} (fence {args.fence}) no longer holds task {args.task_id}'s lease "
                                f"(current fence {task.get('fence_seq')}). Stop working on this run.",
                        details={**result, "current_fence": task.get("fence_seq")})
    _keepalive_log(f"run ended ({run.get('end_reason') or run.get('status')}), task is {task.get('status')}")
    result["ended"] = "run_ended"
    return result


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
    resp = client.execute_cmd(args, "task.progress", _with_session(payload))
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
            from mesh.cli.stdin import read_stdin
            raw_content = read_stdin("--deliverable")
        elif os.path.exists(deliverable_arg):
            with open(deliverable_arg, "r", encoding="utf-8") as fp:
                raw_content = fp.read()
        else:
            raw_content = deliverable_arg
        try:
            deliverable_data = json.loads(raw_content)
        except Exception:
            if deliverable_arg not in ("-",) and not os.path.exists(deliverable_arg) \
                    and not deliverable_arg.lstrip().startswith(("{", "[")):
                # Looks like a path, not JSON: say that the file is missing.
                raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                                message=f"--deliverable: file not found: {deliverable_arg} (and not valid inline JSON)",
                                details={"param": "deliverable", "path": deliverable_arg, "reason": "file_not_found"})
            where = "stdin" if deliverable_arg == "-" else (
                f"file {deliverable_arg}" if os.path.exists(deliverable_arg) else "inline value")
            raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                            message=f"Deliverable must be valid JSON (read from {where})",
                            details={"param": "deliverable"})
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
    resp = client.execute_cmd(args, "task.submit", _with_session(payload))
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
    resp = client.execute_cmd(args, "task.fail", _with_session(payload))
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
