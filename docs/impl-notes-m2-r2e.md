# M2 run r2e — Quest review fixes for r2d (lead prompt authority section)

Branch `wt/m2-r2`. Scope: the findings of the r2d review (VERDICT REVISE).
Allowed files: `mesh/core/lead_prompt.py`, `tests/unit/test_lead_prompt.py`, this
notes file. No other file was touched.

## Findings and fixes

### R1 (MAJOR) — the authority section dropped the safety rules first

Before: `_authority_lines` built the section in order (categories line, model
list, "cannot be an assignee", "cannot approve") and then cut it to
`AUTHORITY_BYTE_BUDGET` (1500 B) by keeping the **head** and replacing the
**tail** with `  - … N more authority lines omitted; see \`mesh agent show\``.
The two safety rules came last, so they were the first thing dropped. With
emoji/CJK the categories line alone exceeds 1500 B, so the whole section
collapsed to the single "… N more authority lines omitted" line — even at
`limit_bytes = 100000` with ~85 KB unused (24/140 sweep cases).

Fix — the section is now cut **rule-first**:

- The three rule lines are emitted **outside** the byte budget and are never
  cut: `AUTHORITY_RULE_ASSIGNEE` ("You cannot be an assignee of a task."),
  `AUTHORITY_RULE_APPROVE` ("You cannot approve anything; only the user can.")
  and `AUTHORITY_RULE_MODELS` ("You may only choose a model from the assignable
  agent's allowed list:"). A lead that does not know them can break the
  authority model, so they are not negotiable against a byte budget.
- The categories line gets its own sub-budget,
  `AUTHORITY_CATEGORIES_BUDGET = 600` B. It keeps whole categories while they
  fit, then ends with `… and N more; see \`mesh agent show\`.` — the pointer is
  part of the line, so it survives every cut. The count is recorded in
  `omitted["authority_categories"]`.
- The model list takes whatever is left of `AUTHORITY_BYTE_BUDGET`; only the
  model lines can be dropped, and the drop is recorded as `authority_cut`.

Measured on the review's own sweep (4 fillers × 6 limits × 5 authority shapes =
120 cases): **0 failures** — `bytes <= limit`, `## Authority rules`,
"You cannot be an assignee", "You cannot approve anything" and the categories
line are present in every case, including emoji at `MIN_LIMIT_BYTES`.

### R2 (MINOR) — `authority_strings_cut` counted once per `build()`

Before: `_authority_lines` was called from inside `_render`, which `build()`
calls on every cut step, so one 61-char category was counted 11 times.

Fix: the section is rendered **once** in `render_lead_prompt`
(`authority_lines = _authority_lines(...)`) and the result is passed to
`_render` as a parameter. The counts are now exact: one 61-char category gives
`authority_strings_cut == 1`.

### R3 (MINOR) — `messages_cut` stayed set when the message was dropped

Before: step 10 set `omitted["messages_cut"]` and, on a later iteration, dropped
the message without clearing it, so a dropped (still `pending`) message was
reported as cut.

Fix: step 10 now
1. computes the byte room that is actually left
   (`limit - size(prompt without any message) - MESSAGE_LINE_OVERHEAD -
   MESSAGE_ROOM_MARGIN`) and re-cuts the **original** payload to that room
   instead of to the fixed 200-char floor, so the message is kept whenever it
   can be; and
2. when it still cannot fit, drops the message, records `omitted["messages"]`
   and **clears** `omitted.pop("messages_cut", None)`.

New constants `MESSAGE_LINE_OVERHEAD = 200` (the JSON envelope of one message
line) and `MESSAGE_ROOM_MARGIN = 200` (room for the "Omitted:" line).

### R5 — `## Authority rules` was in `VARIABLE_SECTIONS`

The final guard could empty the section. `## Authority rules` is removed from
`VARIABLE_SECTIONS`, so the guard now treats it as fixed structure: the variable
region ends before it and the rule lines always survive. The
`_cut_variable_sections` docstring says so.

## Re-measured `MIN_LIMIT_BYTES`

The authority section grew by the three uncut rule lines, so the fixed skeleton
was re-measured with every bounded field at its final cut:

| filler | bytes |
|---|---|
| ASCII | 12235 |
| CJK | 14967 |
| emoji (4-byte) | **15976** |

`MIN_LIMIT_BYTES` stays **16384** (15976 + ~400 B margin). The comment on the
constant was updated with the new figures; the value did not change, so no
caller is affected (every adapter's `max_argv_prompt_bytes` is 100000).

## Changed assertions (with reason)

None were weakened or deleted. Two existing tests were **extended** with
additional assertions (strictly stronger):

1. `test_lead_prompt_cap_property` — added, inside the existing
   `subTest(limit, filler)` loop: `## Authority rules`, "You cannot be an
   assignee", "You cannot approve anything" and "These categories need user
   approval" are present. Reason: this is the R1 regression guard the review
   asked for, over every filler (incl. emoji) and every limit ≥
   `MIN_LIMIT_BYTES`.
2. `test_lead_prompt_cap_property_big_authority` — added the same four
   assertions. Reason: the big-authority fixture is exactly the case where the
   old head-cut dropped the rules.

## New tests (all `ACC: L6`)

- `test_authority_omission_counts_are_exact` — one 61-char category:
  `authority_strings_cut == 1` (was 11), no `authority_cut`, categories line and
  pointer present. Covers R2.
- `test_authority_categories_line_is_cut_to_its_sub_budget` — 50 categories ×
  300 emoji: the categories line is ≤ `AUTHORITY_CATEGORIES_BUDGET` bytes, ends
  with "more" and the `mesh agent show` pointer, `authority_categories > 0`, and
  both rule lines survive. Covers R1.
- `test_dropped_message_clears_messages_cut` — forces the drop branch (step 4b's
  cut does not shrink the body, and the room is negative): the message is
  dropped, `included_message_ids == []`, `omitted["messages"] == 1`,
  `messages_cut` **absent**, and the Omitted line says `messages=1` but not
  `messages_cut=`. Covers R3.
- `test_kept_message_is_recut_to_the_remaining_room` — a 300 KB message at
  `MIN_LIMIT_BYTES` is kept and re-cut to the room that is left: the body is
  longer than `MESSAGE_BODY_CUT_MIN`, `messages_cut == 1`, no `messages` key.
  Covers R3 (the "cut to remaining room before dropping" half).

## ACC coverage

- **L6 — full.** The rendered prompt is ≤ `max_argv_prompt_bytes` for every
  input (property tests over 4 fillers × 6 limits, plus the big-authority
  fixture), the omitted parts are named, the authority rules are always present,
  and messages that did not fit stay pending (the renderer only reports
  `included_message_ids`; the integration clause is covered by the lead-turn
  integration test). No other checklist item is touched by this run.

## Files changed

- `mesh/core/lead_prompt.py`
- `tests/unit/test_lead_prompt.py`
- `docs/impl-notes-m2-r2e.md` (this file)

## Test runs

- `python3 -m unittest tests.unit.test_lead_prompt -v` → rc 0, 18 tests, 1.4 s
  (was 14 tests).
- `python3 -m unittest discover -s tests/unit -t .` → rc 0, 144 tests, 9.6 s
  (was 140).
- `bash scripts/test.sh` before the change (r2d HEAD `1dbfd4e`): rc 0, unit 140,
  integration 146, recovery 1, total wall time **675 s**.
- `bash scripts/test.sh` after the change: rc 0, unit 144, integration 146,
  recovery 1, total wall time **662 s**; both tripwire files
  (`/tmp/mesh-tripwire-suite.log`, `/tmp/mesh-tripwire-hit.log`) absent, i.e.
  empty.

## Deviations / readings

- The review's fix note 1 offered "remove `## Authority rules` from
  `VARIABLE_SECTIONS` **or** keep the fixed rule lines through the guard"; both
  halves were applied, because the guard's region logic keys off the heading
  list and the rule lines must survive either way.
- The review's fix note 3 said "cut to remaining byte room before dropping".
  The room is computed against a render with **no** message at all, minus the
  line envelope and a margin; that is the conservative reading (it never
  over-estimates the room), and it is what makes the 300 KB message fit at
  `MIN_LIMIT_BYTES` instead of being dropped.
- `prompts/vibe-m2-r2e.md` exists in the working tree as an untracked file; it
  was **not** committed (hard rule: do not commit `prompts/*.md`).
- One integration test, `test_h4_stale_task_fail_reports_lease_expired_then_fence_mismatch`,
  failed once during an intermediate full-suite run under load and passes in
  isolation (36 s) and in the final full run (7.1 s). `mesh/core/lead_prompt.py`
  is not imported by any module yet (the lead-turn scheduler lands in r9), so
  this run cannot affect it; it is a pre-existing timing-sensitive flake, not a
  regression from this change.
- Nothing in the run was left undone.
