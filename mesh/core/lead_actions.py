"""`mesh-actions` block parsing and the allowed lead command list (plan §12.4).

Pure module: no DB, no sockets, no subprocess. The lead-turn executor (r10)
calls `parse_actions` on the lead's final text after the turn exits 0 and then
dispatches each item in order with the idempotency key from
`action_idempotency_key`, with the request args from `action_request_args`.

Parsing rules (plan §12.4, §12.5):
1. A block is a fenced code block whose info string is exactly `mesh-actions`.
2. The **last** such block whose body parses as a JSON array wins. Blocks that
   do not parse, or that parse to something other than an array, are skipped
   (the same "last valid block" rule as the `mesh-deliverable` block, §12.5).
3. No valid block at all is not an error: `actions=[]`, `block_found=False`.
4. More than `MAX_ACTIONS` items refuses the whole block
   (`error="too_many_actions"`, `actions=[]`): a partial execution of an
   over-long plan would be worse than none.
5. Per-item problems do not stop the other items; they are recorded on the
   item and reported by the executor as `lead.action_failed`.
6. An item may carry an optional top-level `approval` (next to `args`): the id of
   a user-decided approval for an approval-gated command, the action form of the
   CLI's `--approval`. It must be a string when present; an empty string is the
   same as no approval, as on the socket.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: The 15 commands a lead turn may execute (plan §12.4).
ALLOWED_ACTION_CMDS: List[str] = [
    "task.create",
    "task.assign",
    "task.reassign",
    "task.accept",
    "task.reject",
    "task.retry",
    "task.escalate",
    "task.cancel",
    "task.merge",
    "dep.add",
    "dep.remove",
    "project.note",
    "project.complete",
    "project.deliver",
    "approval.request",
]

#: At most this many actions per turn (plan §12.4).
MAX_ACTIONS = 50

#: Item error codes.
ERR_INVALID_ITEM = "invalid_item"
ERR_CMD_NOT_ALLOWED = "cmd_not_allowed"

#: Block-level error code.
ERR_TOO_MANY_ACTIONS = "too_many_actions"

#: `--idempotency-key` charset and length (plan §13.1).
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

#: An opening fence line: 3+ backticks, an optional info string, LF or CRLF.
_OPEN_RE = re.compile(r"^[ \t]*(`{3,})[ \t]*([^\r\n`]*)\r?$")

#: A closing fence line: 3+ backticks alone on a line (LF or CRLF).
_CLOSE_RE = re.compile(r"^[ \t]*(`{3,})[ \t]*$")


@dataclass
class Action:
    """One item of a `mesh-actions` block."""

    index: int
    cmd: Optional[str]
    args: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    approval: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "cmd": self.cmd, "args": self.args, "error": self.error,
                "approval": self.approval}


@dataclass
class ParsedActions:
    """Result of parsing a lead's final text."""

    actions: List[Action] = field(default_factory=list)
    error: Optional[str] = None
    block_found: bool = False


def _iter_blocks(text: str):
    """Yield (info_string, body) for every fenced block in text, in order.

    A block opens on a line of 3+ backticks plus an info string and closes on
    the first later line that is nothing but backticks, at least as many as the
    opening fence. Shorter runs inside the body are body text.

    Lines are split on LF only, and one trailing CR is stripped from each line,
    so CRLF input parses like LF input. `str.splitlines` is deliberately not
    used: it also splits on a bare CR, on U+2028/U+2029 and on the other
    Unicode line separators, which would break a JSON string argument that
    contains one of them.
    """
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    index = 0
    while index < len(lines):
        opening = _OPEN_RE.match(lines[index])
        if opening is None:
            index += 1
            continue
        fence = opening.group(1)
        info = opening.group(2).strip()
        body: List[str] = []
        index += 1
        closed = False
        while index < len(lines):
            closing = _CLOSE_RE.match(lines[index])
            if closing is not None and len(closing.group(1)) >= len(fence):
                closed = True
                index += 1
                break
            body.append(lines[index])
            index += 1
        if closed:
            yield info, "\n".join(body)


def _parse_item(index: int, item: Any) -> Action:
    """Validate one block item; never raises."""
    if not isinstance(item, dict):
        return Action(index=index, cmd=None, args={}, error=ERR_INVALID_ITEM)
    cmd = item.get("cmd")
    if not isinstance(cmd, str):
        return Action(index=index, cmd=None, args={}, error=ERR_INVALID_ITEM)
    args = item.get("args", {})
    if not isinstance(args, dict):
        return Action(index=index, cmd=cmd, args={}, error=ERR_INVALID_ITEM)
    approval = item.get("approval")
    if approval is not None and not isinstance(approval, str):
        return Action(index=index, cmd=cmd, args=args, error=ERR_INVALID_ITEM)
    approval = approval or None
    if cmd not in ALLOWED_ACTION_CMDS:
        return Action(index=index, cmd=cmd, args=args, error=ERR_CMD_NOT_ALLOWED, approval=approval)
    return Action(index=index, cmd=cmd, args=args, error=None, approval=approval)


def parse_actions(final_text: str) -> ParsedActions:
    """Parse the last valid `mesh-actions` block of a lead's final text."""
    if not isinstance(final_text, str) or not final_text:
        return ParsedActions()

    chosen: Optional[List[Any]] = None
    for info, body in _iter_blocks(final_text):
        if info != "mesh-actions":
            continue
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        chosen = parsed

    if chosen is None:
        return ParsedActions()

    if len(chosen) > MAX_ACTIONS:
        return ParsedActions(actions=[], error=ERR_TOO_MANY_ACTIONS, block_found=True)

    actions = [_parse_item(i, item) for i, item in enumerate(chosen)]
    return ParsedActions(actions=actions, error=None, block_found=True)


def parse_final_actions_block(final_text: str) -> ParsedActions:
    """Parse only the final fenced block, for fail-closed legacy recovery.

    Unlike normal turn parsing, an invalid or unclosed final block must never
    select an earlier plan. A later non-actions fence also refuses recovery.
    Reuse the normal parser only after isolating a complete final actions block.
    """
    if not isinstance(final_text, str) or not final_text:
        return ParsedActions()
    lines = final_text.split("\n")
    fence = None
    start = end = None
    info = None
    for index, line in enumerate(lines):
        line = line[:-1] if line.endswith("\r") else line
        if fence is None:
            opening = _OPEN_RE.match(line)
            if opening is not None:
                fence = opening.group(1)
                info = opening.group(2).strip()
                start, end = index, None
        else:
            closing = _CLOSE_RE.match(line)
            if closing is not None and len(closing.group(1)) >= len(fence):
                end = index
                fence = None
    if info != "mesh-actions" or end is None:
        return ParsedActions()
    return parse_actions("\n".join(lines[start:end + 1]))


def action_idempotency_key(run_id: str, index: int) -> str:
    """Return `act:<run_id>:<index>` and assert it fits the §13.1 charset."""
    key = "act:%s:%d" % (run_id, index)
    if not IDEMPOTENCY_KEY_RE.match(key):
        raise ValueError("idempotency key %r does not match the §13.1 charset" % (key,))
    return key


#: Commands whose handler names the task `task_id`; the §12.4 example writes `task`.
_TASK_ARG_CMDS = frozenset({
    "task.assign", "task.reassign", "task.accept", "task.reject", "task.retry",
    "task.escalate", "task.cancel", "task.merge", "dep.add", "dep.remove",
})

#: Commands whose handler names the project `project_id` (the CLI's positional).
_PROJECT_ID_CMDS = frozenset({"project.note", "project.complete", "project.deliver"})


def _alias(args: Dict[str, Any], src: str, dst: str) -> None:
    if src in args and dst not in args:
        args[dst] = args.pop(src)


def action_request_args(cmd: str, args: Dict[str, Any], scope_project_id: Optional[str]) -> Dict[str, Any]:
    """The request args one action is dispatched with (the dispatch mapping, r10).

    Maps the short names a lead writes to the handler's names (`task` → `task_id`;
    `project` → `project_id` for `project.*`; `project_id` → `project` and `note` →
    `text` where the handler expects those) and fills in the turn's own project when
    the action names none. A name the handler already has is never overwritten, and an
    explicit other project is kept, so authorization still rejects it (rc 4).
    """
    out = dict(args)
    if cmd in _TASK_ARG_CMDS:
        _alias(out, "task", "task_id")
    if cmd == "task.create":
        _alias(out, "project_id", "project")
        if not out.get("project") and scope_project_id:
            out["project"] = scope_project_id
    if cmd in _PROJECT_ID_CMDS:
        _alias(out, "project", "project_id")
        if not out.get("project_id") and scope_project_id:
            out["project_id"] = scope_project_id
    if cmd == "project.note":
        _alias(out, "note", "text")
    if cmd == "approval.request":
        named = any(out.get(k) for k in ("project", "project_id", "task", "task_id"))
        if not named and scope_project_id:
            out["project_id"] = scope_project_id
    return out
