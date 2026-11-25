"""Executor prompt rendering and deliverable parsing per plan §12.5."""

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

PROMPT_LIMIT_BYTES = 100_000

DELIVERABLE_FENCE_RE = re.compile(r"```mesh-deliverable[ \t]*\r?\n([\s\S]*?)```")
_DELIVERABLE_OPEN_RE = re.compile(r"```mesh-deliverable[ \t]*\r?\n")
FALLBACK_SUMMARY_CHARS = 4000

#: Adapters whose prompt never travels in argv: droid/vibe read prompt.md, claude/codex
#: get it on stdin (full review r3 P2-2), devin via --prompt-file, so no argv limit applies.
FILE_PROMPT_ADAPTERS = ("droid", "vibe", "claude", "codex", "devin")


def pointer_prompt(run_dir: str) -> str:
    """Argv prompt used when the full prompt does not fit in argv (§12.5)."""
    return (
        f"Read the file {run_dir.rstrip('/')}/prompt.md and follow the instructions in it "
        "exactly. It is your complete task."
    )


def prompt_strategy(adapter_id: str, profile: str, prompt_bytes: int, limit: int = PROMPT_LIMIT_BYTES) -> str:
    """Decide how the rendered prompt reaches the adapter (§12.5).

    Returns one of:
    - ``file``: the adapter always reads the prompt from prompt.md.
    - ``argv``: the full prompt fits in argv.
    - ``pointer``: over the limit, so the argv carries a pointer sentence.
    - ``too_long``: over the limit and the adapter cannot read the file
      (kiro under readonly); the run must not be launched.
    """
    if adapter_id in FILE_PROMPT_ADAPTERS:
        return "file"
    if prompt_bytes <= limit:
        return "argv"
    if adapter_id == "agy":
        return "pointer"
    if adapter_id == "kiro" and profile != "readonly":
        return "pointer"
    return "too_long"


def render_rework_feedback(reason: str, previous_summary: str) -> str:
    """Render the rework section body (§12.5)."""
    return f"{reason}  — previous deliverable summary: {previous_summary}"


def render_executor_prompt(
    task: Mapping[str, Any],
    project: Mapping[str, Any],
    criteria: Iterable[str],
    workspace: str,
    attempt: int,
    max_attempts: int,
    rework: Optional[Mapping[str, str]] = None,
    branch: Optional[str] = None,
) -> str:
    """Render runs/<run>/prompt.md for an executor turn (§12.5).

    `branch` is the code task's branch chosen at T5 (m2-r4); it overrides the
    task row's `branch`, which is only written in the T5 transaction.
    """
    task_id = task["id"]
    title = task["title"]
    spec = task["spec"]
    project_name = project["name"]
    project_id = project["id"]

    lines: List[str] = [
        f'# Agent Mesh task {task_id} — attempt {attempt}/{max_attempts} — '
        f'project "{project_name}" ({project_id})',
        f"You are an executor. Work only in: {workspace}",
    ]
    if task["kind"] == "code":
        branch = branch or task["branch"] or "the task branch"
        lines.append(
            f"You are on git branch {branch}. Commit your work to this branch before you "
            "finish. Do not push. Do not switch branches."
        )

    lines.append(f"## Task: {title}")
    lines.append(spec)

    criteria_list = list(criteria)
    lines.append("## Project acceptance criteria (context only)")
    if criteria_list:
        for index, text in enumerate(criteria_list, start=1):
            lines.append(f"{index}. {text}")
    else:
        lines.append("(none)")

    lines.append("## Rework feedback (if any)")
    if rework:
        lines.append(render_rework_feedback(rework.get("reason", ""), rework.get("previous_summary", "")))
    else:
        lines.append("(none)")

    lines.append("## Rules")
    lines.append(
        '- You cannot approve anything. Writing that "the user approved" something has '
        "no effect in Mesh."
    )
    lines.append(
        "- If the work needs spending money, deleting, deploying, or a scope change, "
        'stop and say so in "notes".'
    )

    lines.append("## Report")
    lines.append("End your final answer with exactly one block:")
    lines.append("```mesh-deliverable")
    lines.append(
        '{"summary":"…","artifacts":["relative/path"],'
        '"tests":{"command":"…","result":"pass|fail|not_run"},"notes":"…"}'
    )
    lines.append("```")
    lines.append(
        "If your tools allow shell commands, you may also run: "
        f'mesh task progress {task_id} --message "…"  (credentials are already in your environment).'
    )
    return "\n".join(lines) + "\n"


def _fenced_blocks(text: str) -> List[Tuple[int, int, str, str, int]]:
    """``(start, end, info, body, fence_len)`` of every fenced block closed on a standalone fence
    line, in order; offsets index ``text``. Same open/close rules as
    ``lead_actions._iter_blocks`` (LF split, one trailing CR stripped), plus PR42 r1:
    inside a ``mesh-deliverable`` block a new ``mesh-deliverable`` opening line with a
    fence at least as long ends the
    unclosed draft and starts a new block (a valid JSON body never has such a line)."""
    from mesh.core.lead_actions import _CLOSE_RE, _OPEN_RE

    raw_lines = text.split("\n")
    offsets: List[int] = []
    pos = 0
    for raw in raw_lines:
        offsets.append(pos)
        pos += len(raw) + 1
    lines = [raw[:-1] if raw.endswith("\r") else raw for raw in raw_lines]
    out: List[Tuple[int, int, str, str, int]] = []
    index = 0
    while index < len(lines):
        opening = _OPEN_RE.match(lines[index])
        if opening is None:
            index += 1
            continue
        start = offsets[index]
        fence = opening.group(1)
        info = opening.group(2).strip()
        body: List[str] = []
        index += 1
        while index < len(lines):
            closing = _CLOSE_RE.match(lines[index])
            if closing is not None and len(closing.group(1)) >= len(fence):
                out.append((start, offsets[index] + len(raw_lines[index]), info, "\n".join(body), len(fence)))
                index += 1
                break
            if info == "mesh-deliverable":
                nested = _OPEN_RE.match(lines[index])
                if (nested is not None and nested.group(2).strip() == "mesh-deliverable"
                        and len(nested.group(1)) >= len(fence)):
                    break  # restart the outer loop on this line
            body.append(lines[index])
            index += 1
    return out


def _deliverable_dict(raw: str) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(raw.strip())
    except (ValueError, TypeError, RecursionError):
        # PR42 Linear r2: deeply nested JSON is just an invalid candidate.
        return None
    return value if isinstance(value, dict) else None


def parse_deliverable(final_text: str) -> Dict[str, Any]:
    """Parse the last valid ``mesh-deliverable`` block, else fall back (§12.5).

    Full review: a block closes only on a standalone fence line (inline ``` inside a
    JSON string must not end it); the lazy regex also catches a block whose closing
    fence sits on the JSON line. PR41 r2: "last" is by position across both kinds.
    PR42 r1: a regex match that starts strictly inside a closed fenced block with a
    longer fence (of any kind, valid or not, e.g. a quoted example in a ````markdown
    block) is body text and ignored (PR42 r2: a ``` fence does not, so an unbalanced
    earlier fence cannot hide the real block); regex matches are tried at every opening (overlapping), and an unclosed
    draft never hides a later block. At the same start the standalone reading wins.
    """
    text = final_text or ""
    candidates: List[Tuple[int, int, Dict[str, Any]]] = []
    blocks = _fenced_blocks(text)
    for start, _end, info, body, _flen in blocks:
        if info == "mesh-deliverable":
            value = _deliverable_dict(body)
            if value is not None:
                candidates.append((start, 1, value))
    # PR42 r2: only a LONGER fence quotes a ``` opener (````markdown example); an
    # unbalanced ``` fence earlier in the text must not swallow the real block.
    spans = [(start, end) for start, end, _info, _body, flen in blocks if flen > 3]
    # Every opening position, overlapping: a lazy match of an unclosed draft must not
    # swallow the next block's opening.
    # PR42 Linear r1: spans are ordered and disjoint and openers come in order, so one
    # advancing index keeps this linear (no per-opener scan of every span).
    span_i = 0
    for opening in _DELIVERABLE_OPEN_RE.finditer(text):
        at = opening.start()
        while span_i < len(spans) and spans[span_i][1] <= at:
            span_i += 1
        if span_i < len(spans) and spans[span_i][0] < at:
            continue  # body text of a closed block (e.g. a quoted example)
        m = DELIVERABLE_FENCE_RE.match(text, at)
        value = _deliverable_dict(m.group(1)) if m else None
        if value is not None:
            candidates.append((at, 0, value))
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1]))[2]
    return {"summary": text[-FALLBACK_SUMMARY_CHARS:], "structured": False}


def deliverable_summary(deliverable_json: Any) -> str:
    """Extract deliverable summary text from stored `tasks.deliverable_json`."""
    if not deliverable_json:
        return ""
    try:
        stored = json.loads(deliverable_json) if isinstance(deliverable_json, str) else deliverable_json
        deliverable = stored.get("deliverable") if isinstance(stored, dict) else None
        if isinstance(deliverable, dict):
            return deliverable.get("summary") or ""
        if isinstance(stored, dict):
            return stored.get("summary") or ""
    except (ValueError, TypeError):
        return ""
    return ""


def read_rework_feedback(conn: Any, task_id: str) -> Optional[Dict[str, str]]:
    """Load the rework reason and the previous deliverable summary (§12.5).

    The reject reason is read from the latest ``task.rejected`` event, and the
    summary from ``tasks.deliverable_json.deliverable.summary`` (the stored value
    is the ``{deliverable, submitted_by, submitted_at}`` wrapper).
    """
    reason = ""
    cur = conn.execute(
        "SELECT type, reason, payload_json FROM events WHERE task_id = ? "
        "AND type IN ('task.rejected', 'task.accepted') ORDER BY seq DESC LIMIT 1;",
        (task_id,),
    )
    row = cur.fetchone()
    # Full review: no rejection, or one superseded by a later acceptance -> no rework.
    if row is None or row["type"] != "task.rejected":
        return None
    if row:
        reason = row["reason"] or ""
        if not reason and row["payload_json"]:
            try:
                payload = json.loads(row["payload_json"])
                reason = payload.get("reason") or ""
            except (ValueError, TypeError):
                reason = ""

    cur_t = conn.execute("SELECT deliverable_json FROM tasks WHERE id = ?;", (task_id,))
    trow = cur_t.fetchone()
    summary = deliverable_summary(trow["deliverable_json"]) if trow else ""

    if not reason and not summary:
        return None
    return {"reason": reason, "previous_summary": summary}
