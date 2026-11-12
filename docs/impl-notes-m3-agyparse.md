# m3-agyparse: real AgyAdapter.parse() for agy 1.5.3 stream-json (a helper agent, 2026-11-10; p3 polish same day)

Source: `tests/fixtures/real/agy/1.5.3/` only (no vendor CLI was run). Code: `mesh/adapters/agy.py`
(`parse()`, `parse_stderr_error()`, `_scan_stream()`, `_read_text()`); tests:
`tests/unit/test_adapters_agy_real.py`.

## What the 1.5.3 stream contains

| Event | Fields used |
|---|---|
| `{"event":"init","conversation_id":...,"init":{"model","cwd","tools","permission_mode"}}` | `conversation_id` -> session_id, `init.model` -> usage.model |
| `step_update` `step_type:"agent_response"` | only `usage` (tokens) and `duration_seconds` |
| `step_update` `step_type:"tool"` (`ACTIVE`/`DONE`/`ERROR`, `tool_info.output` / `tool_info.error`) | never used |
| `step_update` `user_input` / `system_message` / `error_message` | never used (no text fields) |
| `{"event":"result","result":{...}}` | `status`, `response`, `error`, `usage`, `duration_seconds`, `num_turns`, `denied_actions`, `conversation_id` |

**Where the model text is:** in all three 1.5.3 captures, `agent_response` steps carry *no*
text field at all (keys: conversation_id, step_index, state, step_type, duration_seconds, usage),
and `result.response` is `""` in all three. So `final_text` is `""` for every real fixture and the
supervisor falls back to the raw `stdout.log`. The fallback code still looks for a string under
`text` / `response` / `content` / `message` on `agent_response` steps (the last `agent_response` wins, including when its text is empty or it has none of those keys; an older non-empty step is not kept — same rule as vibe) in case a
later version or a run that ends with a plain answer adds it; that path is unverified against real
output. Tool output and tool errors are never read into `final_text`.

## Decisions

- **usage**: copied from `result.usage` (`usage["source"] = "result"`): input_tokens,
  output_tokens, thinking_tokens, cache_read_tokens, total_tokens. With no result usage at all
  the `agent_response` step usages are summed (`source = "steps"`). When result.usage exists
  but lacks a token key (or holds a non-int there), that key takes the step sum for it, or `0`
  when there are no steps; a token counter is never `None` (p3, tested with a synthetic stream
  whose result.usage omits output_tokens). Plus `model`, `duration_seconds`, `num_turns`, `status`,
  `denied_actions` (count), `malformed_lines` when non-zero.
- **vendor_exit_meaning**: `None` when status is `SUCCESS` and there is no `error`; otherwise
  `"run_error"` with the error text in `usage["error"]` (`result.error`, else `status X`).
  A stream with events but no result event (e.g. truncated) is `"run_error"`, error
  `"no result event"`. On run_error, if `<run_dir>/stderr.log` has an `AGY_ERROR:` line, its
  parsed fields are added as `usage["stderr_error"]`.
- **Objects without an `event` key**: a stream of JSON objects none of which has an `event`
  key (so no init and no result) counts as "seen" and is `"run_error"` with error
  `"no result event"`, `source = "steps"`, all token counters `0`, `model` and session_id
  `None`, `final_text` `""` (p3, pinned by `test_objects_without_event_key`). A stream with no
  JSON object line at all differs only in `source = "none"` and its error text (see below).
- **session_id**: init `conversation_id`, else `result.conversation_id`.
- **No result event** (fix round after review): every run without a result event takes the
  same path, including a missing/unreadable `stdout.log` (`usage["error"] = "missing stdout"`),
  an empty one (`"empty stdout"`), one with no JSON-object line
  (`"no result event (no parseable events)"`), and a stream cut before the result
  (`"no result event"`). All give `vendor_exit_meaning = "run_error"`, and `stderr.log` is still
  parsed, so an `AGY_ERROR`-only failure shows up in `usage["stderr_error"]`.
  `usage["source"]` is `"none"` when no event was read, with all token counters `0` (rebased
  p3: never missing or None). Never raises. (This replaces the earlier
  "empty RunResult" rule.)
- **parse_stderr_error(text)** (staticmethod): last `AGY_ERROR: {json}` line ->
  `{"short_error", "retryable"}` plus `status`, `error_code`, `code_kind` when present; None if
  absent or unparseable. Never raises. step6: retryable is `false` for the network error.

## Reviewer finding: step sums equal result.usage

Quest's review of PR #13 found, and `test_step_usage_sums_match_result` now pins, that the
`agent_response` step sums equal `result.usage` for every token key on all three real 1.5.3
captures: step1-denied (total 18496), fixC-denied (total 38803) and step6-network-error
(total 236950, including `cache_read_tokens` 2131756). So the step-sum fallback agrees with
result.usage wherever both exist in real output.

## Several result events, and ACTIVE agent_response updates

If a stream contains several `result` events, the last one wins (`_scan_stream` overwrites
`scan["result"]`). That is untested against real 1.5.3 output (each capture has one result).

Token fallback sums every `agent_response` step that carries a usage dict. The 1.5.3 fixtures
only show those steps in a terminal state, and the sums equal `result.usage`. If a later
version also emits `ACTIVE` (in-progress) updates for the same `agent_response` step, those
partial usages would be added too and the step sums would double-count.

## Several init events: first init EVENT wins (fix round, Linear REVISE on #19)

`session_id`, `model` and `cwd` all come from the FIRST `init` event only. Later init events are
ignored completely, even when the first one lacks a field; fields are never mixed across init
events. A first init without `init.model` gives `usage["model"] = None`, never the second
init's model. The only fallback is `result.conversation_id` for the session id, used when the
first init has no `conversation_id`. `cwd` is kept in the scan only (not exposed in usage).
Pinned by `test_first_init_missing_model_second_init_not_used` and
`test_init_fields_not_mixed_across_init_events`.

Also pinned: a last `agent_response` step with no text key at all gives `final_text` `""`
(`test_last_agent_response_without_text_key`), and with two valid result events response,
status and usage all come from the last (`test_two_result_events_last_wins`).

## Open points

- No real capture shows a non-empty `result.response` or model text on steps; a successful run
  that ends with an answer is needed to confirm where 1.5.3 puts it.
- step6's `retryable:false` comes from agy itself; whether the supervisor should still treat a
  connection-loss error as retryable is a policy call for the lead, not parse().
