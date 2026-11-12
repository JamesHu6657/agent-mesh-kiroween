# M2 r2c — fixes for the Quest review of r2b (VERDICT REVISE)

Run: m2-r2c, branch `wt/m2-r2`. Scope: the five findings of the Quest review of
r2b, in the four r2 files only.

## Files changed

- `mesh/core/lead_prompt.py`
- `mesh/core/lead_actions.py`
- `tests/unit/test_lead_prompt.py`
- `tests/unit/test_lead_actions.py`
- `docs/impl-notes-m2-r2c.md` (this file)

## Findings and what was done

### A (MAJOR) — `render_lead_prompt` was still not a hard bound

The r2b steps 6–9 bounded task titles, approval summaries, the number of tasks,
approvals and criteria, and the criteria texts — but not the project name/id,
the ids, the message id/type/sender, the authority category and model strings,
or the goal. A 2048-byte limit with a 5000-char goal returned 8111 bytes, CJK
16000 returned 20491, and a 200 KB project name or allowed-model string returned
201 KB at a 100 000-byte limit.

The renderer now bounds **every** field, in this order:

| step | what it does | `omitted` keys |
|---|---|---|
| 0 (entry) | cut project name/id, every task/criterion/approval id, message id/type/sender/created_at to `ID_CUT`/`NAME_CUT`/`MESSAGE_FIELD_CUT` (200 chars); cut task titles to `TASK_TITLE_CUT` (200) and approval summaries to `APPROVAL_SUMMARY_CUT` (500); cut criterion texts to `CRITERION_CUT` (500); cut each authority category/model/agent string to `AUTHORITY_STRING_CUT` (100) and cap the listed categories, agents and models per agent at `AUTHORITY_LIST_CAP` (20) with an "… and N more" count | `project_name_cut`, `project_id_cut`, `task_ids_cut`, `task_titles_cut`, `approval_ids_cut`, `approval_summaries_cut`, `criterion_ids_cut`, `criteria_text_cut`, `message_fields_cut`, `authority_strings_cut`, `authority_categories`, `authority_agents` |
| 1–5 | unchanged from r2b (oldest events, oldest notes, terminal tasks, fewer messages, last-resort goal/criteria cut) | `events`, `notes`, `terminal_tasks`, `messages`, `messages_cut`, `goal_criteria_cut` |
| 6 | re-cut task titles and approval summaries (already bounded at entry; kept so the plan's step order stays explicit) | `task_titles_cut`, `approval_summaries_cut` |
| 7 | keep fewer non-terminal tasks, **dropping the newest first** | `tasks` |
| 8 | keep fewer open approvals, dropping the newest first | `approvals` |
| 9 | criteria to ids only, then drop the excess | `criteria_text_cut`, `criteria` |
| 10 | cut the single kept message shorter (`MESSAGE_BODY_CUT_MIN` = 200 chars), then **drop it entirely** — a dropped message is not in `included_message_ids`, so it stays `pending` | `messages_cut`, `messages` |
| 11 | cut the goal further to a UTF-8 byte budget (`GOAL_BYTE_BUDGET` = 500 bytes) | `goal_cut` |
| 12 | drop the last kept task, then the last kept criterion | `tasks`, `criteria` |
| 13 | final guard: empty every variable section, then a deterministic byte-safe truncation | the section keys, `truncated` |

`_cut_bytes(text, budget)` is the byte-safe cut: it keeps the longest prefix
whose UTF-8 encoding fits the budget minus the `…[cut]` marker, decoding with
`errors="ignore"` so a character is never split.

**`MIN_LIMIT_BYTES` is now 4096** (was 2048). It is the measured size of the
fixed skeleton — headers, empty sections, authority rules, the `mesh-actions`
schema and the completion report format — with every bounded field at its final
cut (one task row, one approval line, one criterion id, one message line, the
goal at `GOAL_BYTE_BUDGET`): **3899 bytes** for ASCII input. 2048 was below that
skeleton, so the final guard had to truncate the `## Actions` and
`## Completion report` sections away; 4096 leaves ~200 bytes of margin for the
"Omitted:" line and multibyte fields. A limit below `MIN_LIMIT_BYTES` still
raises `ValueError` at entry (caller error, unchanged).

Readings recorded:

- **Drop direction (finding D).** Step 7 drops the **newest** tasks and step 8
  the **newest** approvals, keeping the oldest. The oldest open tasks and
  approvals are the ones the lead is most likely to act on, and the r2b code
  (`pop(0)`) contradicted its own "from the end" comment. Code, comment and
  these notes now agree.
- **Batching.** Steps 1, 2, 4, 7, 8 and 9 drop in batches (a quarter of what is
  left, at least one) instead of one item per render. The result is identical
  (the loop still stops at the first render that fits) but a 300-task table no
  longer costs 300 renders: the property test went from 4.4 s to 0.97 s.
- **Step 10 order.** The message is cut to 200 chars first and only dropped when
  even that does not fit, so the lead keeps at least a pointer to the inbox
  rather than losing the wake-up reason entirely.

### B (MAJOR) — step 6 mutated the caller's dicts

r2b's step 6 wrote `task["title"] = …` and `approval["summary"] = …` straight
into the dicts the caller passed in (`lead_prompt.py:461,467`). Every dict the
renderer cuts is now copied first (`_copy_task`, `_copy_approval`,
`_copy_criterion`, `_copy_message`), and the entry cuts write only to those
copies. `test_render_does_not_mutate_the_input` deep-copies the input, renders,
asserts the input is unchanged, and asserts two renders give identical `text`,
`omitted` and `included_message_ids`.

### C (MINOR) — `_iter_blocks` used `splitlines`

`str.splitlines` also splits on a bare `\r`, on U+2028/U+2029 and on the other
Unicode line separators, so a JSON string argument containing one of them broke
the block body. `_iter_blocks` now does `text.split("\n")` and strips one
trailing `\r` per line, so only LF and CRLF end a line.
`test_only_lf_and_crlf_end_lines` pins that a bare CR inside a JSON string
survives, that U+2028 inside a JSON string survives unchanged, and that a bare
CR between two fence lines is not a line end (so ```` ```mesh-actions\r``` ````
is not a block).

### D (MINOR) — step 7 popped index 0 while the comment said "from the end"

Resolved as described under A: the code, the comment and these notes all say
"drop the newest first".

### E (MINOR) — test gaps

- `test_lead_prompt_cap_property` (new, `ACC: L6`): limits
  `{MIN_LIMIT_BYTES, MIN_LIMIT_BYTES+1, 4096, 16000, KiroAdapter.max_argv_prompt_bytes}`
  × fillers `{"a", "é", "中", "😀"}` against a large fixture (300 tasks with
  1000-char multibyte titles, 50 approvals with 4 KB summaries, 100 criteria of
  2 KB, 3 messages of 300 KB, a 5000-char goal, a 200 KB project name and a
  200 KB allowed-model string). Asserts `bytes <= limit`,
  `bytes == len(text.encode())`, `## Actions` and `## Completion report`
  present, and a non-empty Omitted line. Runs in **1.3 s**.
- `test_render_does_not_mutate_the_input` (new, `ACC: L6`): finding B.
- `test_entry_cuts_are_recorded` (new, `ACC: L6`): every entry cut appears in
  the Omitted line.
- `test_lead_prompt_cap_unbounded_fields` now uses multibyte (`中`) task titles
  and asserts the cut goal's first line is still under `## Goal`.
- `test_only_lf_and_crlf_end_lines` (new, `ACC: L2`): finding C.

## Changed assertions (with reason)

- `test_lead_prompt_cap_unbounded_fields` — the task titles changed from `"t" *
  1000` to `"中" * 1000` (finding E: multibyte titles) and the goal assertion
  changed from "the goal line is present" to "the rendered goal line starts with
  the goal's first line and still contains `Ship it — ünïcode ✓`". The goal is
  cut to 2000 chars, so the whole first line cannot be present; the new
  assertion is strictly stronger than the old one for the property it tests
  (the goal survives the cut) and no assertion was weakened or removed.
- No other assertion was changed. `MIN_LIMIT_BYTES` moved from 2048 to 4096;
  `test_limit_below_minimum_is_a_caller_error` uses `MIN_LIMIT_BYTES - 1` and
  therefore still passes unchanged.

## ACC coverage

| ID | coverage | tests |
|---|---|---|
| L6 | full (renderer part) | `test_lead_prompt_cap`, `test_lead_prompt_cap_property`, `test_lead_prompt_cap_unbounded_fields`, `test_render_does_not_mutate_the_input`, `test_entry_cuts_are_recorded`, `test_limit_below_minimum_is_a_caller_error`, `test_small_input_renders_every_section`, `test_single_huge_message_is_cut_with_pointer`, `test_goal_is_on_its_own_line`, `test_actions_section_lists_exactly_the_allowed_cmds`, `test_last_resort_cuts_goal_and_criteria` |
| L2 | full (parser part) | `test_allowed_cmds_are_exactly_the_plan_list`, `test_last_valid_block_wins`, `test_invalid_json_last_block_falls_back`, `test_non_array_block_is_ignored`, `test_other_info_strings_are_ignored`, `test_cmd_not_allowed_is_an_item_error`, `test_invalid_items_are_item_errors`, `test_too_many_actions_refuses_the_block`, `test_idempotency_key_format_and_charset`, `test_no_block_found`, `test_crlf_line_ends_are_accepted`, `test_only_lf_and_crlf_end_lines`, `test_longer_opening_fence_needs_a_long_enough_closing_fence` |

## Test commands and results

- `python3 -m unittest tests.unit.test_lead_prompt tests.unit.test_lead_actions`
  → rc 0, 24 tests, OK (0.5 s).
- `python3 -m unittest discover -s tests/unit -t .` → rc 0, 137 tests, OK (100 s).
- `bash scripts/test.sh` → **rc 0**, 137 unit + 146 integration + 1 recovery =
  284 tests, OK, **total wall time 852 s** (under the 10 min budget), both
  tripwire files empty.

Suite wall time before the first change: not re-measured in this run (the r2b
notes record 636 s for the same suite); after the last change: **852 s**.

## Not done

Nothing from the review is left open. No file outside the five listed above was
touched; no prompt, review or coordination file was committed.
