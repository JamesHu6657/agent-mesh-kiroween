# M2 run r2f — fix the Quest review of r2e (VERDICT REVISE)

Branch `wt/m2-r2`. Scope of this run: the two findings of the r2e review, in
`mesh/core/lead_prompt.py` and `tests/unit/test_lead_prompt.py` only, plus this
notes file. R1/R2/R3/R5 of the review were already verified and are untouched.

## N1 (MAJOR) — step 10 could loop forever

**Reading.** Step 10 re-cut the single kept message to
`room = limit - size(without) - MESSAGE_LINE_OVERHEAD - MESSAGE_ROOM_MARGIN`
with a fixed `MESSAGE_LINE_OVERHEAD = 200`. The message line's envelope
(`{"created_at": …, "id": …, "payload": …, "type": …}`) is not 200 bytes when
`id`/`type`/`created_at` are multibyte: each is cut at `MESSAGE_FIELD_CUT`
characters, so the envelope can be ~2.4 KB. With the real overhead larger than
the guess, `room` stayed positive but the re-cut body still did not fit, and the
next pass computed the *same* `room` — an infinite loop. Reproduced with the
review's repro (20 s timeout, rc -9).

**Fix.** Step 10 now makes progress on every pass:

- the envelope is *measured* per pass:
  `overhead = max(MESSAGE_LINE_OVERHEAD, size(build()) - size(without))`, so
  `MESSAGE_LINE_OVERHEAD` is only a floor;
- `room = limit - size(without) - overhead - MESSAGE_ROOM_MARGIN`;
- from the second pass on the room is shrunk strictly:
  `room = min(room, previous_room - max(1, previous_room // 2))`, so the same
  room can never be computed twice;
- the message is dropped (counted in `messages`, `messages_cut` cleared,
  `cut_message` reset) when `room < MESSAGE_BODY_CUT_MIN`, when the body is
  already at or below `MESSAGE_BODY_CUT_MIN`, or after
  `MESSAGE_RECUT_PASSES = 8` passes — an explicit bound on top of the
  monotonicity;
- the "body already tiny" test now measures UTF-8 bytes
  (`_size(text_body) <= MESSAGE_BODY_CUT_MIN`) instead of characters, so a
  multibyte body cannot look small while being large.

New constants: `MESSAGE_RECUT_PASSES = 8`; the `MESSAGE_LINE_OVERHEAD` comment
now says the envelope is measured and 200 is only the floor.

**Outcome on the repro** (`😀` and `中`, at `MIN_LIMIT_BYTES` and
`MIN_LIMIT_BYTES + 1`): the render returns immediately, `bytes <= limit`, and
the message is either kept with `messages_cut = 1` or dropped with
`messages = 1` and no `messages_cut`. For `😀` the measured envelope leaves no
room, so the message is dropped (10 437 B); for `中` it is kept and cut
(16 079 B). Both are the honest result and both are allowed by the checklist
wording.

## N2 (MINOR) — categories over the list cap lost their count

**Reading.** `_authority_lines` cut the categories list to
`AUTHORITY_LIST_CAP = 10` and only appended `"… and N more"` on the *byte*
budget path. A list of 11–15 short categories fits the 600-byte budget, so the
line ended `"c9; see `mesh agent show`."` with no hint that categories were
hidden.

**Fix.** When `len(categories) > len(shown)` the joined body gets
`" … and %d more"` before the pointer, independently of the byte budget. The
over-budget path is unchanged (it recomputes the count from what it kept).

## Tests added / extended (all `ACC: L6`)

- `test_step_10_terminates_on_multibyte_envelope` (new): the review's repro for
  `😀` and `中` at `MIN_LIMIT_BYTES` and `MIN_LIMIT_BYTES + 1`; asserts the
  render returns, `bytes <= limit`, the fixed sections survive, and the message
  is either kept with `messages_cut == 1` or dropped with `messages == 1` and no
  `messages_cut`. The test itself is the guard: before the fix it hung, and the
  suite's own timeout is the alarm (no `signal.alarm` needed — the render is
  pure and the loop was the only unbounded part).
- `test_lead_prompt_cap_property` (extended): a second message set
  (`wide_messages`) whose `id`/`type`/`created_at` are 300 multibyte characters
  each, run for every limit and every filler, so the cap property covers the
  ~2.4 KB envelope.
- `test_authority_categories_over_the_list_cap_are_counted` (new): 15 short
  categories → the line contains `"and 5 more"` and the pointer, and
  `omitted["authority_categories"] == 5`.

No existing assertion was weakened or removed. The only edits to existing tests
are the added `wide_messages` sub-loop in `test_lead_prompt_cap_property`
(additive) and the byte-based tiny-body check in the renderer.

## Files changed

- `mesh/core/lead_prompt.py` — step 10 loop, `MESSAGE_RECUT_PASSES`, comment on
  `MESSAGE_LINE_OVERHEAD`, categories overflow count.
- `tests/unit/test_lead_prompt.py` — two new tests, one extended.
- `docs/impl-notes-m2-r2f.md` — this file.

## Verification

- `python3 -m unittest tests.unit.test_lead_prompt -v` → rc 0, 20 tests, 2.5 s.
- `bash scripts/test.sh` → rc 0, unit 146 tests OK, integration 146 tests OK,
  recovery 1 test OK, **total wall time 589 s** (under the 10-minute budget).
  Both tripwire files empty. Wall time before this run's change: 589 s (same
  suite, measured at the start of the run).

## Not done

Nothing from the run section is outstanding. `prompts/*.md` was not touched or
committed.
