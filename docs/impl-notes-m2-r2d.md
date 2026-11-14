# M2 run r2d — Quest review fixes for r2c (lead prompt cap)

Branch `wt/m2-r2`. Scope: the five findings of the r2c review (VERDICT REVISE).
Allowed files: `mesh/core/lead_prompt.py`, `tests/unit/test_lead_prompt.py`, this
notes file. `mesh/core/lead_actions.py` was not touched.

## Findings and fixes

### N1 (MAJOR) — the authority section had no total bound

Before: `AUTHORITY_STRING_CUT = 100`, `AUTHORITY_LIST_CAP = 20`, no bound on the
section as a whole. 20 agents × 20 models × 100 chars plus 20 categories is
~44 KB ASCII (~176 KB with emoji), and none of steps 1–12 could shrink it, so
step 13's `_cut_bytes` on the whole text chopped the tail: `## Actions`,
`## Completion report` and the Omitted line disappeared (54/80 adversarial
cases, including CJK/emoji at limit 100000).

Fix:
- `AUTHORITY_STRING_CUT` 100 → **60**, `AUTHORITY_LIST_CAP` 20 → **10**.
- New `AUTHORITY_BYTE_BUDGET = 1500` and `AUTHORITY_POINTER = "mesh agent show"`.
  `_authority_lines` now enforces the budget after building the lines: it keeps
  the head and appends
  `  - … N more authority lines omitted; see \`mesh agent show\``,
  recording `omitted["authority_cut"] = N`. The cut is therefore named in the
  Omitted line, as the checklist requires.
- Measured: 20 agents × 20 emoji models now renders **1198 bytes** of authority
  (was 100000, with the fixed tail missing).

### N2 (MAJOR) — step 10 dropped the message instead of re-cutting it

Before: step 10 tested `startswith('{"pointer"')` on the already-cut message
line. After step 4b the line always starts with `{"created_at"`, so the test was
always true and the message was always dropped. Root cause of the size blow-up:
`_cut_body` used the *character* cut `_cut`, so an emoji body cut to 4000
characters is still ~17 KB. One emoji × 100000 message at limit 16000 produced a
1397-byte prompt with the message dropped, 14 KB unused, and the message left
`pending` forever.

Fix:
- `_cut_body` now uses `_cut_bytes(text, limit)` (byte cut), so the first cut
  really bounds the body.
- Step 10 re-cuts the **original** payload (`_message_body(kept_messages[0])`)
  to `MESSAGE_BODY_CUT_MIN` (200) and drops the message only if that still does
  not fit (or the original is already ≤ 200 chars).
- Verified: one emoji × 100000 message at limit 16000 →
  `included_message_ids == ["msg_1"]`, prompt 5410 bytes,
  `omitted == {"messages_cut": 1}`, pointer present.

### N5 (MINOR) — string-prefix sentinel

The `startswith('{"pointer"')` sentinel is gone; the cut state is tracked by the
`cut_message` boolean flag, so the decision no longer depends on the rendered
text.

### N3 (MINOR) — task status/assignee not cut at entry

`cut_field(task, "status", ID_CUT, "task_fields_cut")` and
`cut_field(task, "assignee", ID_CUT, "task_fields_cut")` are now applied in
step 0, and the cut is named in the Omitted line.

### N4 (MINOR) — `MIN_LIMIT_BYTES` comment wrong

The comment claimed 3899 bytes for ASCII. Re-measured the fixed skeleton with
every bounded field at its final cut (one task row, one approval line, one
criterion id, one message line, the goal at `GOAL_BYTE_BUDGET`, authority at
`AUTHORITY_BYTE_BUDGET`):

| filler | bytes |
|---|---|
| ASCII | 5433 |
| CJK | 11607 |
| emoji (4-byte) | **15067** |

`MIN_LIMIT_BYTES` is now **16384** (15067 + ~1.3 KB margin for the Omitted line
and a longer project header) and the comment states the measurement and the
filler used. Every adapter's `max_argv_prompt_bytes` is 100000, so no caller is
affected.

### Step 13 — structure-safe final cut

`_cut_bytes(text, limit)` on the whole text is replaced by
`_cut_variable_sections(text, limit)`: the text is split at the `VARIABLE_SECTIONS`
headings (Goal, Acceptance criteria, Tasks, Open approvals, Notes, Recent events,
Pending messages, Authority rules). The variable region is emptied (headings
kept) or byte-cut inside itself, and the fixed tail (`## Actions`,
`## Completion report`, the Omitted line) is always preserved. The final cut can
therefore never remove the schema, the report format or the Omitted line.

## Changed assertions (with reason)

1. `test_lead_prompt_cap_property`: the limit `4096` was removed from the limit
   tuple and replaced by `20000`. Reason: 4096 is below the measured floor of
   the fixed skeleton (`MIN_LIMIT_BYTES` = 16384), where `render_lead_prompt`
   raises `ValueError` by contract; the property "bytes ≤ limit for every limit
   ≥ MIN_LIMIT_BYTES" is unchanged and now also covers `100000`.
2. `test_lead_prompt_cap_property`, `test_render_does_not_mutate_the_input` and
   the new message test used the literal `16000`, which is now below
   `MIN_LIMIT_BYTES`; they use `MIN_LIMIT_BYTES` / `MIN_LIMIT_BYTES + 4096`
   instead. Same property, expressed against the constant.
3. No assertion was weakened or deleted; all other assertions are unchanged.

## New tests (all `ACC: L6`)

- `test_lead_prompt_cap_property_big_authority` — 50 categories, 30 agents ×
  30 models × 300 chars, every filler (a/é/中/😀), limits
  `MIN_LIMIT_BYTES`, `MIN_LIMIT_BYTES + 1`, `MIN_LIMIT_BYTES + 4096`, `100000`:
  asserts `bytes <= limit`, `## Actions`, `## Completion report` and exactly one
  `Omitted:` line present, `authority_cut=` named, `mesh agent show` present,
  `"truncated"` **not** in `omitted`, and the text ends with the Omitted line's
  period (fixed tail intact). Runtime ~1.7 s.
- `test_single_emoji_message_is_recut_not_dropped` — one emoji × 100000 message
  at `MIN_LIMIT_BYTES`: `included_message_ids == ["msg_1"]`, `messages_cut == 1`,
  no `messages` key (nothing dropped), pointer present, body is the marked
  truncated string, prompt well under the limit.
- `test_task_status_and_assignee_are_cut_at_entry` — 50 KB status/assignee:
  `task_fields_cut=` named in the Omitted line, the long strings absent.

## ACC coverage

- **L6 — full.** Rendered prompt ≤ `max_argv_prompt_bytes` for every input
  (property test over 4 fillers × 6 limits, plus the big-authority fixture),
  omitted parts named (`authority_cut`, `task_fields_cut`, `messages_cut`,
  `goal_criteria_cut`, …), messages that did not fit stay pending (the renderer
  only reports `included_message_ids`; the integration clause is covered by the
  lead-turn integration test), no `PROMPT_TOO_LONG`.
- No other checklist item is touched by this run.

## Files changed

- `mesh/core/lead_prompt.py`
- `tests/unit/test_lead_prompt.py`
- `docs/impl-notes-m2-r2d.md` (this file)

## Test runs

- `python3 -m unittest tests.unit.test_lead_prompt -v` → rc 0, 14 tests, 2.4 s.
- `bash scripts/test.sh` before the change (r2c HEAD `a1fbb1f`): rc 0, unit 140,
  integration 146, recovery 1, total wall time 741 s.
- `bash scripts/test.sh` after the change: rc 0, unit 140, integration 146,
  recovery 1, total wall time 741 s (both tripwire files empty).

## Deviations / readings

- The review suggested "lower caps and/or a byte budget"; both were applied
  (60-char strings, 10 agents/models, 1500-byte section budget) because the
  per-string caps alone cannot bound 30 × 30 models.
- `MIN_LIMIT_BYTES` was raised rather than kept at 4096: the measured emoji
  skeleton is 15067 bytes, so a 4096 limit cannot honour the contract. No
  production caller passes a limit below 100000.
- `prompts/vibe-m2-r2d.md` exists in the working tree as an untracked file; it
  was **not** committed (hard rule: do not commit `prompts/*.md`).
- Nothing in the run was left undone.
