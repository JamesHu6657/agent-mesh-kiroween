"""Inbox resource commands: list, wait, ack."""

import argparse
from typing import Any, Dict

from mesh.client import MeshClient
import mesh.constants as c
from mesh.errors import MeshError, NotImplementedInM1Error


def register_inbox_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    inbox_parser = subparsers.add_parser(
        "inbox",
        parents=[parent_parser],
        help="Manage agent and user notification queues (long-polling and acks)",
        description="Manage agent and user notification queues (long-polling and acks).",
        epilog="Examples:\n  mesh inbox list\n  mesh inbox wait --timeout 30s\n  mesh inbox ack msg_1 msg_2\n",
    )
    sub = inbox_parser.add_subparsers(dest="verb", metavar="<verb>")

    # list
    list_parser = sub.add_parser(
        "list",
        parents=[parent_parser],
        help="List pending messages in the calling principal's inbox",
        description=("List unacknowledged messages in the calling principal's inbox.\n"
                     "Listing marks pending messages delivered; --peek leaves them untouched."),
        epilog=("Examples:\n  mesh inbox list\n  mesh inbox list --all --user\n"
                "  mesh inbox list --peek --token-file worker.token   # a watcher/cron job checking for work\n"
                "\nWatchers and pollers that are not the worker itself should use --peek, so the\n"
                "worker's own `inbox list`/`inbox wait` still sees the messages as pending.\n"),
    )
    list_parser.add_argument("--all", action="store_true", default=False, help="Include acknowledged/historical messages")
    list_parser.add_argument("--peek", action="store_true", default=False,
                             help="Read only: do not mark pending messages delivered")
    list_parser.set_defaults(func=handle_inbox_list)

    # wait
    wait_parser = sub.add_parser(
        "wait",
        parents=[parent_parser],
        help="Block waiting for new inbox messages (long-poll)",
        description="Block waiting for new inbox messages (long-poll).",
        epilog="Examples:\n  mesh inbox wait --timeout 60s\n  mesh inbox wait --timeout 10m --max 5\n",
    )
    wait_parser.add_argument("--max", type=int, default=1, help="Maximum number of messages to return")
    wait_parser.set_defaults(func=handle_inbox_wait)

    # ack
    ack_parser = sub.add_parser(
        "ack",
        parents=[parent_parser],
        help="Acknowledge processing of one or more inbox messages",
        description="Acknowledge processing of one or more inbox messages.",
        epilog="Examples:\n  mesh inbox ack msg_123\n  mesh inbox ack msg_1 msg_2\n",
    )
    ack_parser.add_argument("messages", nargs="+", help="Message identifier(s) to acknowledge")
    ack_parser.set_defaults(func=handle_inbox_ack)


def _one_line(v: Any, limit: int = 60) -> str:
    s = " ".join(str(v).split()) if v is not None else "-"
    return s if len(s) <= limit else s[: limit - 3] + "..."


def short_project(pid: Any) -> str:
    """`prj_` + the last 8 characters: ids share a time prefix, the tail tells them apart."""
    if not pid:
        return "-"
    pid = str(pid)
    return pid if len(pid) <= 12 or not pid.startswith("prj_") else "prj_" + pid[-8:]


def _approval_kind(p: Dict[str, Any]) -> str:
    op = p.get("op")
    if isinstance(op, dict):
        op = op.get("type") or op.get("cmd") or op.get("op")
    parts = [str(x) for x in (p.get("category"), op) if x]
    return "/".join(parts) if parts else "-"


def _approval_reason(mtype: str, p: Dict[str, Any]) -> Any:
    if mtype == "approval.decided":
        decision = p.get("decision") or p.get("status")
        note = p.get("note")
        return f"{decision}: {note}" if decision and note else (decision or note)
    if mtype == "approval.expired":
        return "expired" + (f" at {p['expires_at']}" if p.get("expires_at") else "")
    if mtype == "approval.requested":
        return p.get("summary") or (f"expires {p['expires_at']}" if p.get("expires_at") else None)
    return p.get("reason") or p.get("summary") or p.get("status")


def inbox_table(items: Any) -> str:
    """Text form of inbox messages with the payload fields a worker acts on (PR #37 #6, N2):
    project (short id), subject (task id, or approval id for approval.*), title (task title,
    or approval kind and summary) and reason (failure/rejection/cancel reason, or the
    approval decision/note/expiry) next to id/type/status."""
    if not items:
        return "(no messages)"
    rows = [["SEQ", "ID", "TYPE", "STATUS", "PROJECT", "SUBJECT", "TITLE", "REASON"]]
    for m in items:
        p = m.get("payload") if isinstance(m.get("payload"), dict) else {}
        mtype = str(m.get("type") or "-")
        if mtype.startswith("approval."):
            subject = p.get("approval_id") or m.get("approval_id")
            kind = _approval_kind(p)
            title = kind if kind != "-" else None
            if mtype == "approval.requested" and p.get("summary") and title:
                title = f"{kind}: {p['summary']}"
            reason = _approval_reason(mtype, p)
            if mtype == "approval.requested" and title and p.get("summary"):
                reason = f"expires {p['expires_at']}" if p.get("expires_at") else None
        else:
            subject = p.get("task_id") or m.get("task_id")
            title = p.get("title")
            failure = p.get("failure") if isinstance(p.get("failure"), dict) else {}
            reason = p.get("reason") or failure.get("reason")
            if not reason and isinstance(p.get("last_rejection"), dict):
                reason = "rejected: " + str(p["last_rejection"].get("reason") or "")
        rows.append([str(m.get("seq") if m.get("seq") is not None else "-"), str(m.get("id") or "-"),
                     mtype, str(m.get("status") or "-"),
                     short_project(m.get("project_id") or p.get("project_id")),
                     str(subject or "-"), _one_line(title, 48), _one_line(reason, 60)])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(r[i].ljust(widths[i]) for i in range(len(r))).rstrip() for r in rows)


def _text_view(args: argparse.Namespace, data: Any) -> Any:
    if getattr(args, "format", "json") == "text" and isinstance(data, dict) and isinstance(data.get("items"), list):
        extra = {k: v for k, v in data.items() if k != "items" and v is not None}
        text = inbox_table(data["items"])
        if extra:
            text += "\n" + "\n".join(f"{k}: {v}" for k, v in extra.items())
        return {"text": text}
    return data


def handle_inbox_list(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "all": getattr(args, "all", False),
        "user": getattr(args, "user", False),
        "peek": bool(getattr(args, "peek", False)),
    }
    resp = client.execute_cmd(args, "inbox.list", payload)
    return _text_view(args, resp.get("data", {}))


def handle_inbox_wait(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "dry_run", False):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--dry-run is not supported on 'inbox wait'",
            details={"param": "dry_run"},
        )
    timeout_val = getattr(args, "timeout", None)
    if not timeout_val:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--timeout is required for inbox wait",
            details={"param": "timeout"},
        )
    client = MeshClient(home=args.home)
    payload = {
        "timeout": timeout_val,
        "max": getattr(args, "max", 1),
        "user": getattr(args, "user", False),
    }
    resp = client.execute_cmd(args, "inbox.wait", payload)
    return _text_view(args, resp.get("data", {}))


def handle_inbox_ack(args: argparse.Namespace) -> Dict[str, Any]:
    client = MeshClient(home=args.home)
    payload = {
        "messages": args.messages,
        "user": getattr(args, "user", False),
    }
    resp = client.execute_cmd(args, "inbox.ack", payload)
    return resp.get("data", {})


def handle_inbox(verb: str, milestone: str) -> Dict[str, Any]:
    raise NotImplementedInM1Error(verb, milestone)
