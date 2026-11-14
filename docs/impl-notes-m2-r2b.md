# M2 r2b — fixes for the Kiro review of r2

Run: m2-r2b, branch `wt/m2-r2`. Scope: implement the "Fix prompt notes" of
`logs/raw/review-m2-r2.md` (MAJOR finding 1 and MINOR findings 2–6) in the five
r2 files only.

## Files changed

- `mesh/core/lead_prompt.py`
- `mesh/core/lead_actions.py`
- `tests/unit/test_lead_prompt.py`
- `tests/unit/test_lead_actions.py`
- `docs/impl-notes-m2-r2b.md` (this file)

## Findings and what was done

### MAJOR 1 — the cap was not a hard bound

`render_lead_prompt` stopped after the last-resort goal/criteria cut and
returned without re-checking the size, so a prompt with many long task titles,
long approval summaries or many criteria could still exceed
`limit_bytes`. Four new steps now run after step 5, each re-rendering and each
recording its drop in `omitted` (so the "Omitted:" line names it):

| step | what it does | `omitted` key |
|---|---|---|
| 6 | cut task titles to `TASK_TITLE_CUT` (200 chars) and approval summaries to `APPROVAL_SUMMARY_CUT` (500 chars), marking each cut with `…[cut]` | `task_titles_cut`, `approval_summaries_cut` |
| 7 | keep fewer non-terminal tasks, dropping from the front (oldest first), leaving at least one; the Tasks section then prints `Open tasks not listed: N (see \`mesh task list\`).` | `tasks` |
| 8 | keep fewer open approvals, dropping from the end; the section prints `Open approvals not listed: N (see \`mesh approval list\`).` | `approvals` |
| 9 | shorten criteria to ids only, then drop the excess criteria (keeping at least one); the section prints `Criteria not listed: N (see \`mesh project show\`).` | `criteria_text_cut`, `criteria` |

`MIN_LIMIT_BYTES = 2048` is the smallest limit the renderer can honour; a
smaller `limit_bytes` raises `ValueError` at entry. That is a caller error: the
caller passes the adapter's real prompt budget, and below the fixed part of the
prompt (headers, authority rules, schema) no amount of cutting can help.

Readings: the step order is 6 → 7 → 8 → 9 because titles and summaries are the
cheapest single-field cuts, then whole rows, then the criteria (which the lead
needs most to judge completion). Cut lengths 200/500 keep a task title and an
approval summary recognisable while bounding them.

### MINOR 2 — the prompt contained a `mesh-actions` block

The Actions section used to print the allowed commands as a fenced
` ```mesh-actions ` block. A lead that echoed the prompt would submit the schema
itself as actions. The commands are now a plain comma-separated list and the
example item is a single object under a ` ```json ` fence. The prompt contains
no `mesh-actions` fence at all, asserted with
`parse_actions(render_lead_prompt(make_input()).text).block_found is False`.

### MINOR 3 — message payloads were re-encoded as strings

`_message_body` used to `json.dumps` every payload, so a `{"text": "hello"}`
payload appeared as a JSON string. The payload is now kept as the JSON value it
is; only a payload that has to be cut becomes a string holding
`{"truncated": true, "text": …, "pointer": "mesh inbox list"}`.

### MINOR 4 — the last-resort cut was invisible

Step 5 now records `goal_criteria_cut=1`, so the "Omitted:" line names it
alongside the existing "cut to fit the prompt size limit" sentence.

### MINOR 5 — the fence rule

`_FENCE_RE` is replaced by a line scanner (`_OPEN_RE`, `_CLOSE_RE`,
`_iter_blocks`): a block opens on a line of 3+ backticks plus an info string and
closes on the first later line that is nothing but backticks, at least as many
as the opening fence. Line ends may be LF or CRLF (the closing line's trailing
`\r` is accepted). A shorter run inside the body stays body text, so a
4-backtick block may hold 3-backtick lines. A 4-backtick opening closed by only
3 backticks is not a block.

Note: `mesh/core/prompts.py` (the deliverable fence parser the review pointed
at) does not exist in this checkout; the rule above is the one the review
states, applied to the actions parser.

### MINOR 6 — test gaps

- `test_small_input_renders_every_section` now asserts the ten headings appear
  in strictly increasing offsets, each exactly once, with the Omitted line after
  the last one.
- `test_idempotency_key_format_and_charset` asserts the literal
  `^[A-Za-z0-9._:-]{1,128}$` instead of the module's own regex, and adds an
  over-length run id that must raise `ValueError`.

## Changed assertions (with reason)

- `test_actions_section_lists_exactly_the_allowed_cmds` — the only assertion the
  run section allows to change. It used to parse the ` ```mesh-actions ` block
  out of the prompt; finding 2 removes that block, so it now reads the plain
  list from the Actions section and additionally asserts the block is absent.
- `test_idempotency_key_format_and_charset` — `assertRegex(key,
  IDEMPOTENCY_KEY_RE)` replaced by the literal pattern (finding 6: a test must
  not validate the module with the module's own regex). No assertion was
  weakened; the over-length case is new.

## ACC coverage

| ID | coverage | tests |
|---|---|---|
| L6 | full (renderer part) | `test_lead_prompt_cap`, `test_lead_prompt_cap_unbounded_fields`, `test_limit_below_minimum_is_a_caller_error`, `test_small_input_renders_every_section`, `test_single_huge_message_is_cut_with_pointer`, `test_goal_is_on_its_own_line`, `test_actions_section_lists_exactly_the_allowed_cmds`, `test_last_resort_cuts_goal_and_criteria` |
| L2 | full (parser part) | `test_allowed_cmds_are_exactly_the_plan_list`, `test_last_valid_block_wins`, `test_invalid_json_last_block_falls_back`, `test_non_array_block_is_ignored`, `test_other_info_strings_are_ignored`, `test_cmd_not_allowed_is_an_item_error`, `test_invalid_items_are_item_errors`, `test_too_many_actions_refuses_the_block`, `test_idempotency_key_format_and_charset`, `test_no_block_found`, `test_crlf_line_ends_are_accepted`, `test_longer_opening_fence_needs_a_long_enough_closing_fence` |

## Test commands and results

- `python3 -m unittest tests.unit.test_lead_actions tests.unit.test_lead_prompt -v`
  → rc 0, 20 tests, OK.
- `bash scripts/test.sh` → rc 0, 133 unit + 146 integration + 1 recovery tests,
  OK, total wall time 636 s (under the 10 min budget), both tripwire files empty.

## Not done

Nothing from the review is left open. No file outside the five listed above was
touched.
