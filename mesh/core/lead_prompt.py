"""Lead prompt rendering with the §12.6 size cap.

Pure module: no DB, no sockets, no subprocess. The lead-turn scheduler (r9)
builds a `LeadPromptInput` from the DB, calls `render_lead_prompt` with
`limit_bytes = <lead adapter>.max_argv_prompt_bytes`, writes `text` to
`runs/<run>/prompt.md`, and marks exactly `included_message_ids` `delivering`.
Messages that did not fit are left untouched, so they stay `pending` for the
next turn.

Section order (plan §12.6):
1. role line and project header
2. the goal on its own line (Q11: the fake test double selects behaviour from
   a line matching `^FAKE {…}$`, so the goal must be a line of its own)
3. criteria with ids
4. task table
5. open approvals
6. last notes
7. recent events
8. pending messages as JSON
9. authority rules
10. the `mesh-actions` schema and the `project complete` report format
11. an "Omitted:" line naming what was dropped

Cap order (plan §12.6): oldest events, then oldest notes, then terminal tasks
(replaced by per-status counts), then fewer pending messages (oldest first, at
least one; a single message that still does not fit is cut to 4000 chars with
a pointer to `mesh inbox list`). Last resort, if the fixed parts alone exceed
the limit: the goal and criteria texts are cut and the prompt says so.

The cap is a hard bound for **every** input: `bytes <= limit_bytes` holds for
any `limit_bytes >= MIN_LIMIT_BYTES` and any field content. Fields that have no
per-item bound of their own are bounded at entry (step 0: project name/id,
every task/criterion/approval id, message id/sender/type, authority category
and model strings, and the number of listed agents/models), and steps 6–12 keep
cutting (task titles, approval summaries, the number of non-terminal tasks and
open approvals, criteria texts and the number of criteria, the single kept
message, the goal, the last kept task and criterion) until the prompt fits. A
final deterministic byte-safe truncation guarantees the bound even if a step
above could not reach it. Every step records what it dropped in `omitted`, so
the "Omitted:" line always names it.

The authority section is cut rule-first: the three rule lines (no assignee, no
approval, models from the allowed list) are emitted outside the byte budget and
are never dropped, the categories line is cut to its own sub-budget and always
ends with a pointer to `mesh agent show`, and the model list takes the rest.
The section is rendered once per render, so its omission counts are exact.

The renderer never mutates its input: every dict it cuts is copied first.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mesh.core.lead_actions import ALLOWED_ACTION_CMDS

#: Terminal task statuses dropped from the task table under pressure (§12.6).
TERMINAL_STATUSES = ("accepted", "merged", "cancelled")

#: A single pending message body is cut to this many characters (§12.6).
MESSAGE_BODY_CUT = 4000

#: Last-resort cut for the goal and criteria texts.
GOAL_CUT = 2000
CRITERION_CUT = 500

#: Steps 6–9: fixed cuts for fields that have no per-item bound of their own.
TASK_TITLE_CUT = 200
APPROVAL_SUMMARY_CUT = 500

#: Step 0: fixed cuts applied at entry, before anything is rendered.
NAME_CUT = 200
ID_CUT = 200
MESSAGE_FIELD_CUT = 200

#: Step 0: the authority section is bounded in total, not only per string. The
#: caps below are the first (cheap) bound; the section is then cut to
#: `AUTHORITY_BYTE_BUDGET` UTF-8 bytes, so a project with many agents and models
#: cannot push the fixed part of the prompt past the limit. The three rule lines
#: (no assignee, no approval, models from the allowed list) are never cut: they
#: are emitted outside the budget, because a lead that does not know them can
#: break the authority model. The categories line gets its own sub-budget
#: (`AUTHORITY_CATEGORIES_BUDGET`) and the model list takes the rest.
AUTHORITY_STRING_CUT = 60
AUTHORITY_LIST_CAP = 10
AUTHORITY_BYTE_BUDGET = 1500
AUTHORITY_CATEGORIES_BUDGET = 600
AUTHORITY_POINTER = "mesh agent show"

#: The authority rules that are always rendered, whatever the budget.
AUTHORITY_RULE_ASSIGNEE = "- You cannot be an assignee of a task."
AUTHORITY_RULE_APPROVE = "- You cannot approve anything; only the user can."
AUTHORITY_RULE_MODELS = (
    "- You may only choose a model from the assignable agent's allowed list:"
)

#: Step 10: the single kept message is re-cut from its original payload to the
#: byte room that is left before it is dropped.
MESSAGE_BODY_CUT_MIN = 200

#: Step 10: the JSON envelope of one message line (`{"created_at": …,
#: "id": …, "payload": …, "type": …}`) and a margin for the "Omitted:" line,
#: both subtracted from the room the re-cut body may use. The envelope is
#: *measured* per render (its fields can be multibyte and up to
#: `MESSAGE_FIELD_CUT` characters each, i.e. ~2.4 KB); `MESSAGE_LINE_OVERHEAD`
#: is only the floor used when the measurement is not available.
MESSAGE_LINE_OVERHEAD = 200
MESSAGE_ROOM_MARGIN = 200

#: Step 10: hard bound on the re-cut passes. Every pass either shrinks the room
#: strictly or drops the message, so the loop terminates on its own; the bound
#: is the belt-and-braces guarantee that it always does.
MESSAGE_RECUT_PASSES = 8

#: Step 11: the goal is cut to this many UTF-8 bytes.
GOAL_BYTE_BUDGET = 500

#: The smallest `limit_bytes` this renderer can honour. It is the measured size
#: of the fixed skeleton (headers, empty sections, the authority section with its
#: three uncut rule lines and the categories line at
#: `AUTHORITY_CATEGORIES_BUDGET`, the `mesh-actions` schema and the completion
#: report format) with every bounded field at its final cut — one task row, one
#: approval line, one criterion id, one message line, the goal at
#: `GOAL_BYTE_BUDGET` — measured with 4-byte UTF-8 characters (emoji) in every
#: one of those fields: 15976 bytes. The ASCII skeleton is 12235 bytes and the
#: CJK one 14967, so the emoji figure is the floor; the constant adds ~400 bytes
#: of margin for the "Omitted:" line and a longer project header. Below it even
#: the fixed part of the prompt cannot fit, so a smaller limit is a caller
#: error, not something to cut around.
MIN_LIMIT_BYTES = 16384

INBOX_POINTER = "mesh inbox list"
TASK_POINTER = "mesh task list"
APPROVAL_POINTER = "mesh approval list"

#: The `project complete` report format (plan §8).
COMPLETE_REPORT_FORMAT = (
    '{"criteria":[{"id":"crit_…","met":true,"evidence":"…"}],"summary":"…"}'
)


@dataclass
class LeadPromptInput:
    """Everything the lead prompt is rendered from."""

    project: Dict[str, Any] = field(default_factory=dict)
    criteria: List[Dict[str, Any]] = field(default_factory=list)
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    approvals: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    authority: Dict[str, Any] = field(default_factory=dict)
    limit_bytes: int = 100_000


@dataclass
class RenderedLeadPrompt:
    """The rendered prompt plus what had to be dropped to fit the cap."""

    text: str = ""
    included_message_ids: List[str] = field(default_factory=list)
    omitted: Dict[str, int] = field(default_factory=dict)
    bytes: int = 0


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut(text: str, limit: int) -> str:
    """Cut text to at most `limit` characters, marking the cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…[cut]"


def _cut_bytes(text: str, budget: int) -> str:
    """Cut text to at most `budget` UTF-8 bytes, marking the cut.

    The cut never splits a character: the result is the longest prefix whose
    encoding fits, plus the marker.
    """
    if _size(text) <= budget:
        return text
    marker = "…[cut]"
    room = max(0, budget - _size(marker))
    kept = text.encode("utf-8")[:room].decode("utf-8", "ignore")
    return kept + marker


def _cut_field(value: Any, limit: int) -> str:
    """A string field cut to `limit` characters (non-strings are stringified)."""
    return _cut("" if value is None else str(value), limit)


#: The sections whose content is variable, in the order the final guard cuts
#: them. Everything outside them is fixed structure and is never removed.
#: `## Authority rules` is deliberately absent: its rule lines are the rules the
#: lead must obey, so the final guard must not empty them.
VARIABLE_SECTIONS = (
    "## Goal",
    "## Acceptance criteria",
    "## Tasks",
    "## Open approvals",
    "## Notes",
    "## Recent events",
    "## Pending messages",
)


def _cut_variable_sections(text: str, limit: int) -> str:
    """Cut `text` to `limit` bytes inside its variable sections only.

    The fixed structure — the role line, the section headings, the `## Authority
    rules` section (its rule lines are the rules the lead must obey), the
    `## Actions` and `## Completion report` sections and the "Omitted:" line — is
    kept verbatim. The variable sections are emptied first (their headings stay),
    and only if that is still not enough is the text cut byte-safely at the end
    of the last variable section, so the fixed tail always survives.
    """
    if _size(text) <= limit:
        return text
    lines = text.split("\n")
    starts = [
        index
        for index, line in enumerate(lines)
        if line in VARIABLE_SECTIONS
    ]
    if not starts:
        return _cut_bytes(text, limit)
    # The variable region runs from the first variable heading to the line
    # before the first fixed heading after it.
    first = starts[0]
    last = len(lines)
    for index in range(first + 1, len(lines)):
        if lines[index].startswith("## ") and lines[index] not in VARIABLE_SECTIONS:
            last = index
            break
    region = lines[first:last]
    # 1. Empty the variable region, keeping its headings.
    emptied = [line for line in region if line.startswith("## ")]
    candidate = "\n".join(lines[:first] + emptied + lines[last:])
    if _size(candidate) <= limit:
        return candidate
    # 2. Cut the region byte-safely, keeping the fixed tail intact.
    tail = "\n".join(lines[last:])
    room = limit - _size(tail) - 1
    if room <= 0:
        return _cut_bytes(tail, limit)
    return _cut_bytes("\n".join(lines[:first]), room) + "\n" + tail


def _copy_task(task: Dict[str, Any]) -> Dict[str, Any]:
    return dict(task)


def _copy_approval(approval: Dict[str, Any]) -> Dict[str, Any]:
    return dict(approval)


def _copy_criterion(criterion: Dict[str, Any]) -> Dict[str, Any]:
    return dict(criterion)


def _copy_message(message: Dict[str, Any]) -> Dict[str, Any]:
    return dict(message)


def _message_body(message: Dict[str, Any]) -> Any:
    """The message payload as a JSON value (never re-encoded as a string)."""
    return message.get("payload")


def _message_line(message: Dict[str, Any], body: Any) -> str:
    return json.dumps(
        {
            "id": message.get("id"),
            "type": message.get("type"),
            "created_at": message.get("created_at"),
            "payload": body,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _cut_body(body: Any, limit: int = MESSAGE_BODY_CUT) -> str:
    """Cut a payload that does not fit; the result is a marked JSON string.

    The cut is by UTF-8 bytes, not characters: a body of multibyte characters
    (CJK, emoji) is up to four times its character count in bytes, and the
    prompt cap is a byte cap.
    """
    if isinstance(body, str):
        text = body
    else:
        text = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return json.dumps(
        {"truncated": True, "text": _cut_bytes(text, limit), "pointer": INBOX_POINTER},
        sort_keys=True,
        ensure_ascii=False,
    )


def _task_row(task: Dict[str, Any]) -> str:
    return "| %s | %s | %s | %s |" % (
        task.get("id", ""),
        task.get("title", ""),
        task.get("status", ""),
        task.get("assignee") or "-",
    )


def _event_line(event: Dict[str, Any]) -> str:
    parts = ["#%s" % (event.get("seq"),), str(event.get("ts", "")), str(event.get("type", ""))]
    if event.get("task_id"):
        parts.append("task=%s" % (event.get("task_id"),))
    if event.get("reason"):
        parts.append("reason=%s" % (event.get("reason"),))
    return "- " + " ".join(parts)


def _note_line(note: Dict[str, Any]) -> str:
    return "- %s %s" % (note.get("ts", ""), note.get("text", ""))


def _approval_line(approval: Dict[str, Any]) -> str:
    return "- %s [%s] %s" % (
        approval.get("id", ""),
        approval.get("category", ""),
        approval.get("summary", ""),
    )


def _authority_lines(authority: Dict[str, Any], omitted: Dict[str, int]) -> List[str]:
    """The authority rules, bounded in total to `AUTHORITY_BYTE_BUDGET` bytes.

    The section is the only part of the prompt whose size the caller controls
    through data (one line per assignable agent, one entry per allowed model),
    so it is cut as a whole. The cut is rule-first: the three rule lines (no
    assignee, no approval, models from the allowed list) are emitted outside the
    budget and are never dropped, because they are the rules the lead must obey.
    The categories line is cut to `AUTHORITY_CATEGORIES_BUDGET` bytes and always
    ends with a count and a pointer to `mesh agent show`; the model list takes
    whatever is left of `AUTHORITY_BYTE_BUDGET`. Every cut is recorded in
    `omitted` (`authority_cut`, `authority_categories`, `authority_agents`,
    `authority_strings_cut`).

    The function is called once per render (the result is reused by every
    `build()`), so the counts it records are exact.
    """
    def cut(value: Any) -> str:
        text = "" if value is None else str(value)
        if len(text) > AUTHORITY_STRING_CUT:
            omitted["authority_strings_cut"] = omitted.get("authority_strings_cut", 0) + 1
        return _cut(text, AUTHORITY_STRING_CUT)

    # The categories line, cut to its own byte sub-budget. The pointer is part
    # of the line, so it survives every cut.
    categories = list(authority.get("approval_categories") or [])
    if categories:
        shown = [cut(c) for c in categories[:AUTHORITY_LIST_CAP]]
        if len(categories) > len(shown):
            omitted["authority_categories"] = len(categories) - len(shown)
        prefix = "- These categories need user approval before you act: "
        pointer = "; see `%s`." % (AUTHORITY_POINTER,)
        room = AUTHORITY_CATEGORIES_BUDGET - _size(prefix) - _size(pointer)
        body = ", ".join(shown)
        if len(categories) > len(shown):
            # The list cap, not the byte budget, hides categories: say how many
            # so the lead knows the list is incomplete.
            body = "%s … and %d more" % (body, len(categories) - len(shown))
        if _size(body) > room:
            # Keep whole categories while they fit, then count the rest.
            kept: List[str] = []
            for item in shown:
                candidate = ", ".join(kept + [item])
                if _size(candidate) > room:
                    break
                kept.append(item)
            rest = len(categories) - len(kept)
            if kept:
                body = "%s … and %d more" % (", ".join(kept), rest)
            else:
                body = "… and %d more" % (rest,)
            if _size(body) > room:
                body = _cut_bytes(body, room)
            omitted["authority_categories"] = rest
        categories_line = prefix + body + pointer
    else:
        categories_line = "- No category needs user approval in this project."

    # The model list takes the rest of the budget.
    models = authority.get("allowed_models") or {}
    model_lines: List[str] = []
    if models:
        agents = sorted(models)
        for agent in agents[:AUTHORITY_LIST_CAP]:
            allowed = list(models[agent] or [])
            shown = [cut(m) for m in allowed[:AUTHORITY_LIST_CAP]]
            if len(allowed) > len(shown):
                shown.append("… and %d more" % (len(allowed) - len(shown),))
            model_lines.append("  - %s: %s" % (cut(agent), ", ".join(shown) or "(none)"))
        if len(agents) > AUTHORITY_LIST_CAP:
            omitted["authority_agents"] = len(agents) - AUTHORITY_LIST_CAP
    else:
        model_lines.append("- No assignable agent has an allowed model list.")

    rules = [AUTHORITY_RULE_ASSIGNEE, AUTHORITY_RULE_APPROVE, AUTHORITY_RULE_MODELS]
    fixed = ["## Authority rules", "", categories_line] + rules
    budget = AUTHORITY_BYTE_BUDGET
    kept_models: List[str] = []
    for line in model_lines:
        if _size("\n".join(fixed + kept_models + [line])) > budget:
            break
        kept_models.append(line)
    dropped = len(model_lines) - len(kept_models)
    if dropped:
        tail = "  - … %d more authority lines omitted; see `%s`" % (dropped, AUTHORITY_POINTER)
        while kept_models and _size("\n".join(fixed + kept_models + [tail])) > budget:
            kept_models.pop()
            dropped += 1
            tail = "  - … %d more authority lines omitted; see `%s`" % (
                dropped,
                AUTHORITY_POINTER,
            )
        omitted["authority_cut"] = dropped
        kept_models.append(tail)
    return fixed + kept_models


def _actions_schema_lines() -> List[str]:
    return [
        "## Actions",
        "",
        "End your final answer with at most one fenced block whose info string is exactly",
        "`mesh-actions` and whose body is a JSON array of objects `{\"cmd\": …, \"args\": {…}}`.",
        "At most 50 actions per turn; they run in order, each with idempotency key",
        "`act:<run_id>:<index>`. An approval-gated cmd takes the id of a user-approved",
        "approval as a top-level `\"approval\"` key next to `args`. `task.merge` is checked",
        "during the turn's close and started after it; its outcome arrives as merge events.",
        "Allowed cmds:",
        "",
        ", ".join(ALLOWED_ACTION_CMDS),
        "",
        "Example item:",
        "",
        "```json",
        json.dumps({"cmd": ALLOWED_ACTION_CMDS[0], "args": {}}, ensure_ascii=False),
        "```",
        "",
        "## Completion report",
        "",
        "`mesh project complete --report -` takes:",
        "",
        "```json",
        COMPLETE_REPORT_FORMAT,
        "```",
    ]


def _render(
    inp: LeadPromptInput,
    events: List[Dict[str, Any]],
    notes: List[Dict[str, Any]],
    tasks: List[Dict[str, Any]],
    terminal_counts: Dict[str, int],
    messages: List[Dict[str, Any]],
    message_bodies: List[Any],
    goal: str,
    criteria: List[Dict[str, Any]],
    approvals: List[Dict[str, Any]],
    authority_lines: List[str],
    omitted: Dict[str, int],
    last_resort: bool,
) -> str:
    project = inp.project or {}
    lines: List[str] = []

    # 1. Role line and project header.
    lines.append(
        "# Agent Mesh lead turn — project \"%s\" (%s)"
        % (_cut_field(project.get("name", ""), NAME_CUT), _cut_field(project.get("id", ""), ID_CUT))
    )
    lines.append("You are the lead agent of this project. You plan, assign, review and report.")
    lines.append("")

    # 2. The goal on its own line (Q11).
    lines.append("## Goal")
    lines.append("")
    lines.append(goal)
    lines.append("")

    # 3. Criteria with ids.
    lines.append("## Acceptance criteria")
    lines.append("")
    if criteria:
        for criterion in criteria:
            text = criterion.get("text", "")
            if text:
                lines.append("- %s: %s" % (criterion.get("id", ""), text))
            else:
                lines.append("- %s" % (criterion.get("id", ""),))
    else:
        lines.append("- (none)")
    if omitted.get("criteria"):
        lines.append("")
        lines.append(
            "Criteria not listed: %d (see `mesh project show`)." % (omitted["criteria"],)
        )
    lines.append("")

    # 4. Task table.
    lines.append("## Tasks")
    lines.append("")
    if tasks:
        lines.append("| id | title | status | assignee |")
        lines.append("|---|---|---|---|")
        for task in tasks:
            lines.append(_task_row(task))
    else:
        lines.append("(no tasks)")
    if terminal_counts:
        lines.append("")
        lines.append(
            "Terminal tasks not listed: %s."
            % (", ".join("%s=%d" % (k, terminal_counts[k]) for k in sorted(terminal_counts)),)
        )
    if omitted.get("tasks"):
        lines.append("")
        lines.append(
            "Open tasks not listed: %d (see `%s`)." % (omitted["tasks"], TASK_POINTER)
        )
    lines.append("")

    # 5. Open approvals.
    lines.append("## Open approvals")
    lines.append("")
    if approvals:
        for approval in approvals:
            lines.append(_approval_line(approval))
    else:
        lines.append("- (none)")
    if omitted.get("approvals"):
        lines.append("")
        lines.append(
            "Open approvals not listed: %d (see `%s`)."
            % (omitted["approvals"], APPROVAL_POINTER)
        )
    lines.append("")

    # 6. Last notes.
    lines.append("## Notes")
    lines.append("")
    if notes:
        for note in notes:
            lines.append(_note_line(note))
    else:
        lines.append("- (none)")
    lines.append("")

    # 7. Recent events.
    lines.append("## Recent events")
    lines.append("")
    if events:
        for event in events:
            lines.append(_event_line(event))
    else:
        lines.append("- (none)")
    lines.append("")

    # 8. Pending messages as JSON.
    lines.append("## Pending messages")
    lines.append("")
    if messages:
        for message, body in zip(messages, message_bodies):
            lines.append(_message_line(message, body))
    else:
        lines.append("(none)")
    lines.append("")

    # 9. Authority rules (rendered once, outside `build()`).
    lines.extend(authority_lines)
    lines.append("")

    # 10. The mesh-actions schema and the completion report format.
    lines.extend(_actions_schema_lines())
    lines.append("")

    # 11. What was omitted.
    if omitted:
        parts = ["%s=%d" % (key, omitted[key]) for key in sorted(omitted) if omitted[key]]
        lines.append("Omitted: %s." % (", ".join(parts),))
    else:
        lines.append("Omitted: nothing.")
    if last_resort:
        lines.append(
            "The goal and criteria texts were cut to fit the prompt size limit; "
            "read the full project with `mesh project show`."
        )
    lines.append("")
    return "\n".join(lines)


def render_lead_prompt(inp: LeadPromptInput) -> RenderedLeadPrompt:
    """Render the lead prompt, dropping parts until it fits `limit_bytes`."""
    limit = inp.limit_bytes
    if limit < MIN_LIMIT_BYTES:
        raise ValueError(
            "limit_bytes %d is below MIN_LIMIT_BYTES %d; the caller must pass the "
            "adapter's real prompt budget" % (limit, MIN_LIMIT_BYTES)
        )
    project = inp.project or {}
    goal = str(project.get("goal", ""))
    criteria = list(inp.criteria or [])
    tasks = list(inp.tasks or [])
    notes = list(inp.notes or [])
    events = list(inp.events or [])
    messages = list(inp.messages or [])
    approvals = list(inp.approvals or [])

    omitted: Dict[str, int] = {}
    last_resort = False

    # 0. Bound the fields that have no per-item bound of their own, on copies
    #    (the caller's dicts are never mutated).
    kept_events = [dict(event) for event in events]
    kept_notes = [dict(note) for note in notes]
    kept_tasks = [_copy_task(task) for task in tasks]
    terminal_counts: Dict[str, int] = {}
    kept_messages = [_copy_message(message) for message in messages]
    bodies = [_message_body(m) for m in kept_messages]
    kept_approvals = [_copy_approval(approval) for approval in approvals]
    criteria = [_copy_criterion(criterion) for criterion in criteria]
    cut_message = False

    def cut_field(record: Dict[str, Any], key: str, limit: int, counter: str) -> None:
        """Cut one field of a copy and record the cut in `omitted`."""
        value = record.get(key)
        if value is None:
            return
        text_value = str(value)
        if len(text_value) > limit:
            record[key] = _cut(text_value, limit)
            omitted[counter] = omitted.get(counter, 0) + 1

    for task in kept_tasks:
        cut_field(task, "id", ID_CUT, "task_ids_cut")
        cut_field(task, "title", TASK_TITLE_CUT, "task_titles_cut")
        cut_field(task, "status", ID_CUT, "task_fields_cut")
        cut_field(task, "assignee", ID_CUT, "task_fields_cut")
    for approval in kept_approvals:
        cut_field(approval, "id", ID_CUT, "approval_ids_cut")
        cut_field(approval, "summary", APPROVAL_SUMMARY_CUT, "approval_summaries_cut")
    for criterion in criteria:
        cut_field(criterion, "id", ID_CUT, "criterion_ids_cut")
        cut_field(criterion, "text", CRITERION_CUT, "criteria_text_cut")
    for message in kept_messages:
        for key in ("id", "type", "sender", "created_at"):
            cut_field(message, key, MESSAGE_FIELD_CUT, "message_fields_cut")
    if len(str(project.get("name", ""))) > NAME_CUT:
        omitted["project_name_cut"] = 1
    if len(str(project.get("id", ""))) > ID_CUT:
        omitted["project_id_cut"] = 1

    # The authority section is rendered exactly once: it depends only on the
    # input, and rendering it per `build()` would count its cuts once per call.
    authority_lines = _authority_lines(inp.authority or {}, omitted)

    def build() -> str:
        return _render(
            inp,
            kept_events,
            kept_notes,
            kept_tasks,
            terminal_counts,
            kept_messages,
            bodies,
            goal,
            criteria,
            kept_approvals,
            authority_lines,
            omitted,
            last_resort,
        )

    def build_without_messages() -> str:
        """The prompt as it would be with no pending message at all."""
        return _render(
            inp,
            kept_events,
            kept_notes,
            kept_tasks,
            terminal_counts,
            [],
            [],
            goal,
            criteria,
            kept_approvals,
            authority_lines,
            omitted,
            last_resort,
        )

    text = build()

    # 1. Drop the oldest events. The loop drops in batches (a quarter of what
    #    is left, at least one) so a huge list does not cost one render per
    #    event; the last few are dropped one at a time to land just under the
    #    limit.
    while _size(text) > limit and kept_events:
        batch = max(1, len(kept_events) // 4)
        del kept_events[:batch]
        omitted["events"] = omitted.get("events", 0) + batch
        text = build()

    # 2. Drop the oldest notes, in the same batches.
    while _size(text) > limit and kept_notes:
        batch = max(1, len(kept_notes) // 4)
        del kept_notes[:batch]
        omitted["notes"] = omitted.get("notes", 0) + batch
        text = build()

    # 3. Drop terminal tasks, replaced by per-status counts.
    if _size(text) > limit:
        remaining = []
        for task in kept_tasks:
            status = task.get("status")
            if status in TERMINAL_STATUSES:
                terminal_counts[status] = terminal_counts.get(status, 0) + 1
                omitted["terminal_tasks"] = omitted.get("terminal_tasks", 0) + 1
            else:
                remaining.append(task)
        kept_tasks = remaining
        text = build()

    # 4. Take fewer pending messages, oldest first, at least one.
    while _size(text) > limit and len(kept_messages) > 1:
        batch = min(max(1, len(kept_messages) // 4), len(kept_messages) - 1)
        del kept_messages[-batch:]
        del bodies[-batch:]
        omitted["messages"] = omitted.get("messages", 0) + batch
        text = build()

    # 4b. A single message that still does not fit is cut, with a pointer.
    if _size(text) > limit and kept_messages and not cut_message:
        cut_message = True
        bodies = [_cut_body(body) for body in bodies]
        omitted["messages_cut"] = len(kept_messages)
        text = build()

    # 5. Last resort: cut the goal and criteria texts.
    if _size(text) > limit:
        last_resort = True
        goal = _cut(goal, GOAL_CUT)
        criteria = [
            {"id": c.get("id", ""), "text": _cut(str(c.get("text", "")), CRITERION_CUT)}
            for c in criteria
        ]
        omitted["goal_criteria_cut"] = 1
        text = build()

    # 6. Cut task titles and approval summaries to fixed lengths (already
    #    bounded at entry; this keeps the step order of the plan explicit).
    if _size(text) > limit:
        cut_titles = 0
        for task in kept_tasks:
            title = str(task.get("title", ""))
            if len(title) > TASK_TITLE_CUT:
                task["title"] = _cut(title, TASK_TITLE_CUT)
                cut_titles += 1
        cut_summaries = 0
        for approval in kept_approvals:
            summary = str(approval.get("summary", ""))
            if len(summary) > APPROVAL_SUMMARY_CUT:
                approval["summary"] = _cut(summary, APPROVAL_SUMMARY_CUT)
                cut_summaries += 1
        if cut_titles:
            omitted["task_titles_cut"] = cut_titles
        if cut_summaries:
            omitted["approval_summaries_cut"] = cut_summaries
        text = build()

    # 7. Keep fewer non-terminal tasks, dropping the newest first (the oldest
    #    tasks are the ones the lead is most likely to act on), with a count
    #    and pointer.
    while _size(text) > limit and len(kept_tasks) > 1:
        batch = min(max(1, len(kept_tasks) // 4), len(kept_tasks) - 1)
        del kept_tasks[-batch:]
        omitted["tasks"] = omitted.get("tasks", 0) + batch
        text = build()

    # 8. Keep fewer open approvals, dropping the newest first, with a count and
    #    pointer.
    while _size(text) > limit and kept_approvals:
        batch = max(1, len(kept_approvals) // 4)
        del kept_approvals[-batch:]
        omitted["approvals"] = omitted.get("approvals", 0) + batch
        text = build()

    # 9. Shorten criteria to ids only, then drop the excess with a count.
    if _size(text) > limit and any(c.get("text") for c in criteria):
        criteria = [{"id": c.get("id", ""), "text": ""} for c in criteria]
        omitted["criteria_text_cut"] = 1
        text = build()
    while _size(text) > limit and len(criteria) > 1:
        batch = min(max(1, len(criteria) // 4), len(criteria) - 1)
        del criteria[-batch:]
        omitted["criteria"] = omitted.get("criteria", 0) + batch
        text = build()

    # 10. Re-cut the single kept message from its ORIGINAL payload to the byte
    #     room that is actually left, then drop it entirely. A dropped message
    #     is not in `included_message_ids`, so it stays `pending`, and the
    #     `messages_cut` count is cleared: nothing was cut, it was dropped.
    #     `cut_message` tracks the cut state: the body is a marked JSON string
    #     after step 4b, so its text can no longer be recognised by a prefix.
    #
    #     Every pass must make progress: the room is measured against the real
    #     envelope of the message line (its id/type/created_at can be multibyte
    #     and up to `MESSAGE_FIELD_CUT` characters each, so a fixed 200-byte
    #     guess is far too small), and it is then shrunk strictly — by the
    #     measured overshoot, at least halved — so a pass can never compute the
    #     same room twice. Once the room falls below `MESSAGE_BODY_CUT_MIN` the
    #     message is dropped, and `MESSAGE_RECUT_PASSES` bounds the loop even if
    #     a future change broke that monotonicity.
    passes = 0
    previous_room = 0
    while _size(text) > limit and kept_messages:
        passes += 1
        original = _message_body(kept_messages[0])
        text_body = (
            original
            if isinstance(original, str)
            else json.dumps(original, sort_keys=True, ensure_ascii=False)
        )
        # The room left for the body: the prompt without the message line, plus
        # the line's own measured envelope, minus a small margin for the
        # Omitted line.
        without = build_without_messages()
        overhead = max(
            MESSAGE_LINE_OVERHEAD,
            _size(build()) - _size(without),
        )
        room = limit - _size(without) - overhead - MESSAGE_ROOM_MARGIN
        if passes > 1:
            # Strict progress: never ask for the same room twice.
            room = min(room, previous_room - max(1, previous_room // 2))
        if (
            passes > MESSAGE_RECUT_PASSES
            or room < MESSAGE_BODY_CUT_MIN
            or _size(text_body) <= MESSAGE_BODY_CUT_MIN
        ):
            kept_messages.pop()
            bodies.pop()
            omitted["messages"] = omitted.get("messages", 0) + 1
            omitted.pop("messages_cut", None)
            cut_message = False
            text = build()
            continue
        previous_room = room
        cut_message = True
        bodies = [_cut_body(original, room)]
        omitted["messages_cut"] = len(kept_messages)
        text = build()

    # 11. Cut the goal further, to a UTF-8 byte budget.
    if _size(text) > limit and _size(goal) > GOAL_BYTE_BUDGET:
        goal = _cut_bytes(goal, GOAL_BYTE_BUDGET)
        omitted["goal_cut"] = 1
        text = build()

    # 12. Drop the last kept task, then the last kept criterion.
    while _size(text) > limit and kept_tasks:
        kept_tasks.pop()
        omitted["tasks"] = omitted.get("tasks", 0) + 1
        text = build()
    while _size(text) > limit and criteria:
        criteria.pop()
        omitted["criteria"] = omitted.get("criteria", 0) + 1
        text = build()

    # 13. Final guard: empty every variable section, then cut inside the
    #     variable sections only. The steps above reach the limit for every
    #     input the daemon can produce; this keeps the bound true even for a
    #     pathological one. The fixed structure — the `## Actions` and
    #     `## Completion report` sections and the "Omitted:" line — is never
    #     removed: a prompt without them would leave the lead unable to act or
    #     report, and without the record of what was dropped.
    if _size(text) > limit:
        for key, kept in (
            ("events", kept_events),
            ("notes", kept_notes),
            ("tasks", kept_tasks),
            ("approvals", kept_approvals),
            ("criteria", criteria),
            ("messages", kept_messages),
        ):
            if kept:
                omitted[key] = omitted.get(key, 0) + len(kept)
                del kept[:]
        bodies = []
        if _size(goal) > GOAL_BYTE_BUDGET:
            goal = _cut_bytes(goal, GOAL_BYTE_BUDGET)
            omitted["goal_cut"] = 1
        text = build()
    if _size(text) > limit:
        omitted["truncated"] = 1
        text = _cut_variable_sections(build(), limit)

    return RenderedLeadPrompt(
        text=text,
        included_message_ids=[m.get("id") for m in kept_messages],
        omitted=omitted,
        bytes=_size(text),
    )
