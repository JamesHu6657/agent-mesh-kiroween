# Impl notes — M2 run r2 (m2-leadblocks)

Scope: two pure, daemon-free modules used later by the lead-turn runs (r9, r10).
Nothing calls them yet. Only new files were created; no existing file was edited.

Files added:

- `mesh/core/lead_actions.py`
- `mesh/core/lead_prompt.py`
- `tests/unit/test_lead_actions.py`
- `tests/unit/test_lead_prompt.py`
- `docs/impl-notes-m2-r2.md` (this file)

## ACC coverage

| ID | Status | Test |
|---|---|---|
| L2 | **partial** — parsing and the allowed-command list only; execution is r10 | `tests/unit/test_lead_actions.py::TestLeadActions` (class docstring `ACC: L2 (parser part)`; every test method also carries the tag) |
| L6 | **partial** — the checklist's Check `test_lead_prompt_cap` is covered; the integration clause "messages that did not fit stay `pending`" is confirmed in r9 | `tests/unit/test_lead_prompt.py::TestLeadPrompt::test_lead_prompt_cap` (plus 5 supporting tests in the same class, all tagged `ACC: L6`) |

`grep -rn "ACC: L2" tests/` and `grep -rn "ACC: L6" tests/` both find the files above.

## `mesh/core/lead_actions.py` — parsing rules

- `ALLOWED_ACTION_CMDS` is exactly the 15 cmds of plan §12.4, in plan order.
  `MAX_ACTIONS = 50`.
- A block is a fenced code block (3+ backticks) whose info string is **exactly**
  `mesh-actions`. The regex is anchored per line and requires the closing fence
  to use the same backtick run, so `mesh-deliverable` blocks and other info
  strings are ignored.
- The **last** block whose body parses as a JSON **array** wins. Blocks that do
  not parse, or that parse to a non-array (object, string, number), are skipped
  — the same "last valid block" rule as the `mesh-deliverable` block (§12.5).
- No valid block → `actions=[]`, `block_found=False`, `error=None`. An empty or
  non-string input is treated the same way (no exception).
- **Reading (conservative):** more than 50 items refuses the **whole** block:
  `actions=[]`, `error="too_many_actions"`, `block_found=True`. Executing a
  prefix of an over-long plan would silently drop the lead's later intents, so
  the whole block is refused and the executor (r10) reports the failure. Exactly
  50 items is accepted.
- Item errors never stop the other items. `Action.error` is one of:
  - `invalid_item` — the item is not an object, `cmd` is missing or not a
    string, or `args` is present but not an object;
  - `cmd_not_allowed` — `cmd` is a string outside `ALLOWED_ACTION_CMDS`
    (e.g. `config.set`).
  A missing `args` defaults to `{}` and stays valid (the plan's examples always
  carry `args`, but an empty-args command such as `project.complete` reading
  `--input` is not worth rejecting at parse time; the executor's own validation
  decides).
- `Action.index` is the 0-based position in the block, so it is stable even
  when earlier items are invalid.
- `action_idempotency_key(run_id, index)` returns `act:<run_id>:<index>` and
  validates the result against the §13.1 charset `[A-Za-z0-9._:-]{1,128}`. A run
  id containing a character outside the charset raises `ValueError` rather than
  producing a key the daemon would reject later. (The daemon's own check in
  `mesh/daemon/server.py` only enforces the length; the charset is the plan's
  rule, so it is enforced here.)

## `mesh/core/lead_prompt.py` — rendering and the cap

`LeadPromptInput` / `RenderedLeadPrompt` are plain dataclasses; every field has
a default so r9 can build the input incrementally from the DB.

Section order is exactly the one in the run prompt (role + header, goal on its
own line, criteria, task table, open approvals, notes, events, pending messages
as JSON, authority rules, `mesh-actions` schema + `project complete` report
format, "Omitted:" line).

- **Goal line (Q11).** The goal is emitted as its own line, verbatim, directly
  under `## Goal`. `test_goal_is_on_its_own_line` pins that a
  `FAKE {"succeed":{…}}` goal appears as a whole line, which is what
  `mesh/testing/fake_agent.py::parse_fake_spec` matches (`^FAKE\s+(\{.*\})$`).
- **Messages.** Each pending message is one JSON object per line with keys
  `id`, `type`, `created_at`, `payload` (the payload is serialised to a string
  body so a message can be cut without breaking the JSON line).
- **Authority.** Rendered from `authority["approval_categories"]`,
  `authority["allowed_models"]` (per assignable agent) and
  `authority["lead_cannot_be_assignee"]`; the "you cannot approve anything"
  rule is always stated.
- **Actions schema.** The `mesh-actions` example block is generated from
  `ALLOWED_ACTION_CMDS` imported from `lead_actions.py`, so the prompt and the
  parser can never drift. `test_actions_section_lists_exactly_the_allowed_cmds`
  parses that block back and compares the cmd list.

### Cap order (plan §12.6)

The renderer rebuilds the text after each drop and stops as soon as it fits:

1. oldest events (one at a time, from the front);
2. oldest notes (one at a time, from the front);
3. terminal tasks (`accepted`, `merged`, `cancelled`) — all of them in one step,
   replaced by a `Terminal tasks not listed: accepted=N, …` line;
4. fewer pending messages: drop from the **end** (newest first), keeping the
   oldest, and never below one. `included_message_ids` is therefore always a
   prefix of the input order, which is what r9 marks `delivering`; the rest are
   untouched and stay `pending`;
5. if the single remaining message still does not fit, its body is cut to 4000
   characters and gets `…[truncated; full message: mesh inbox list]`;
6. **last resort** (recorded reading): if the fixed parts alone exceed the
   limit, the goal is cut to 2000 characters and each criterion text to 500,
   and the prompt says so ("The goal and criteria texts were cut to fit the
   prompt size limit; read the full project with `mesh project show`."). The
   function never raises for size.

The "Omitted:" line is always present: `Omitted: nothing.` when nothing was
dropped, otherwise `Omitted: events=N, notes=N, terminal_tasks=N, messages=N,
messages_cut=N.` with only the non-zero counters. `omitted` is a dict of the
same counters.

`bytes` is the UTF-8 length of `text`; the tests assert `bytes <= limit` and
`bytes == len(text.encode("utf-8"))`.

### Readings recorded

- The cap is applied to the whole rendered prompt, including the fixed
  authority/schema sections, because that is what is passed to the adapter.
- Dropping terminal tasks is a single all-or-nothing step (the plan says
  "terminal tasks from the task table (replaced by per-status counts)"), not a
  per-task loop; the per-status counts line is what keeps the information.
- `messages_cut` is a separate counter from `messages` so the executor can tell
  "dropped, stays pending" from "included but truncated".
- The renderer never mutates the input lists (it copies them).

## How r9 / r10 are meant to call these modules

r9 (`mesh/daemon/lead_turns.py`):

```python
inp = LeadPromptInput(project=…, criteria=…, tasks=…, approvals=…, notes=…,
                      events=…, messages=pending_lead_wake_messages,
                      authority=…, limit_bytes=lead_adapter.max_argv_prompt_bytes)
rendered = render_lead_prompt(inp)
# write rendered.text to runs/<run>/prompt.md, pass it per §12.5
# mark exactly rendered.included_message_ids delivering with delivering_run_id
# everything else is left untouched -> still pending for the next turn
```

r10 (after the turn exits 0, before closing the run):

```python
parsed = parse_actions(final_text)
if parsed.error == "too_many_actions":
    …report the block-level failure…
for action in parsed.actions:            # in order
    if action.error:                     # invalid_item / cmd_not_allowed
        …lead.action_failed for this index…
        continue
    key = action_idempotency_key(run_id, action.index)   # act:<run>:<index>
    …dispatch internally as the lead run principal, via='lead_turn'…
```

## Test budget / suite wall time

- `bash scripts/test.sh` before the first change: rc 0, 113 unit + 146
  integration + 1 recovery = 260 tests, **430 s**.
- After sub-step 1 (lead_actions): rc 0, 123 unit + 146 integration + 1
  recovery = 270 tests, 690 s (the box was loaded; the same suite ran in 395 s
  two runs later).
- After sub-step 2 (lead_prompt, final): rc 0, 129 unit + 146 integration + 1
  recovery = 276 tests, **395 s**.

Both tripwire files are empty in every run. No existing assertion was changed
or weakened; no existing test file was touched.

## Not done (by design)

- Nothing executes the actions and nothing renders a prompt in a real turn:
  that is r9 (scheduler, "stay pending" clause) and r10 (execution,
  `lead.action_failed`). L2 and L6 are therefore **partial** here.
- No `mesh-actions` handling was added to the fake adapter (r10).
