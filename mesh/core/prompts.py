"""Executor prompt rendering and deliverable parsing per plan §12.5."""

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

PROMPT_LIMIT_BYTES = 100_000

DELIVERABLE_FENCE_RE = re.compile(r"```mesh-deliverable[ \t]*\r?\n([\s\S]*?)```")
FALLBACK_SUMMARY_CHARS = 4000

FILE_PROMPT_ADAPTERS = ("droid", "vibe")


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


def parse_deliverable(final_text: str) -> Dict[str, Any]:
    """Parse the last valid ``mesh-deliverable`` block, else fall back (§12.5)."""
    text = final_text or ""
    matches = list(DELIVERABLE_FENCE_RE.finditer(text))
    for match in reversed(matches):
        try:
            value = json.loads(match.group(1).strip())
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {"summary": text[-FALLBACK_SUMMARY_CHARS:], "structured": False}


def read_rework_feedback(conn: Any, task_id: str) -> Optional[Dict[str, str]]:
    """Load the rework reason and the previous deliverable summary (§12.5).

    The reject reason is read from the latest ``task.rejected`` event, and the
    summary from ``tasks.deliverable_json.deliverable.summary`` (the stored value
    is the ``{deliverable, submitted_by, submitted_at}`` wrapper).
    """
    reason = ""
    cur = conn.execute(
        "SELECT reason, payload_json FROM events WHERE task_id = ? AND type = 'task.rejected' "
        "ORDER BY seq DESC LIMIT 1;",
        (task_id,),
    )
    row = cur.fetchone()
    if row:
        reason = row["reason"] or ""
        if not reason and row["payload_json"]:
            try:
                payload = json.loads(row["payload_json"])
                reason = payload.get("reason") or ""
            except (ValueError, TypeError):
                reason = ""

    summary = ""
    cur_t = conn.execute("SELECT deliverable_json FROM tasks WHERE id = ?;", (task_id,))
    trow = cur_t.fetchone()
    if trow and trow["deliverable_json"]:
        try:
            stored = json.loads(trow["deliverable_json"])
            deliverable = stored.get("deliverable") if isinstance(stored, dict) else None
            if isinstance(deliverable, dict):
                summary = deliverable.get("summary") or ""
            elif isinstance(stored, dict):
                summary = stored.get("summary") or ""
        except (ValueError, TypeError):
            summary = ""

    if not reason and not summary:
        return None
    return {"reason": reason, "previous_summary": summary}
