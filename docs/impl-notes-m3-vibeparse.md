# m3-vibeparse: real VibeAdapter.parse() for vibe 3.1.0 (Grok Bot, 2026-11-10)

Only `mesh/adapters/vibe.py` parse() + module helpers changed; build/profiles/probe untouched.
Worked only from `tests/fixtures/real/vibe/3.1.0/` (no vendor CLI run).

Inputs (layout from `VibeAdapter.build`): `<run_dir>/stdout.log` (`--output streaming`) and
`<run_dir>/vibe-out/export.json` (`--output-dir`).

| RunResult field | Source |
|---|---|
| final_text | stdout.log: the LAST `type=message, role=assistant` line; its `content[*]` parts of `type=text` joined and stripped. If that last message's text is empty or it has no text parts, final_text is `""` (no fallback to an older assistant message; fix round after review). User messages and `type=effect` (tool) lines are ignored. `""` if none. |
| session_id (precedence) | export `session_id` wins over the stream `sessionId` when both are present and differ (tested). |
| session_id | export `session_id`, else first `sessionId` on a stream line |
| usage | export `usage` (input/output/cached_input/total_tokens) + top-level `cost_usd`, `steps`, `outcome`, `stop_reason`, `exit_code`, and `model` = export `config.active_model` (the alias vibe ran with; the stream lines carry no model). NaN / Infinity in those numbers become `None`, also inside nested dicts / lists of usage, up to 32 nested levels (deeper containers are left as-is so deep nesting never raises). Without an export: `None` if the stream has an assistant message; otherwise `{"error": "no export"}` (`"export not an object"` when export.json is valid JSON but not an object) plus `usage["stderr_tail"]` (last non-empty `stderr.log` line, capped to its last 500 chars) when present. |
| vendor_exit_meaning | `run_error` if export `error` is set: only `null`, `""`, `false`, `0`, `{}` and `[]` are not errors; anything else (`true`, `1`, other numbers, a non-empty str / dict / list) is `run_error`, or if `outcome` is `error` even with `error` null, missing, false or 0 (fix round after review); else by `outcome`: `completed` -> None, `turn_limit` -> `turn_limit`, `terminated`/`interrupted` -> `terminated`. Any other (or missing / non-string) outcome falls back on `stop_reason`: `interrupted` -> `terminated`, `limit` -> `limit` (generic, not `turn_limit`, because `--max-price` / `--max-tokens` stops also report `limit`), anything else -> None. The raw outcome stays in `usage["outcome"]`. Without an export: `run_error` only when there is also no assistant message; with an assistant message, meaning stays `None`. |

Export schema seen: `schema_version` 1, `vibe_version` 3.1.0; keys outcome, stop_reason, error,
session_id, usage, cost_usd, steps, config, journal_dir, started_at, ended_at, limits, exit_code.
The parser does not reject other schema versions; it reads the same keys leniently.

Evidence gap: the only real export is `outcome: terminated` / `stop_reason: interrupted` /
`exit_code: 3` / `error: null`. `completed`, `turn_limit` and a non-null `error` (and its shape)
follow the task spec, not a capture; outcomes for `--max-price` / `--max-tokens` limits are
unknown. A real completed run, a `--max-turns` run and an error run would close this.

Robustness: missing/empty files, non-JSON or non-object lines, a truncated last line and a
truncated/non-object export never raise (tests in `tests/unit/test_adapters_vibe_real.py`).

## Caller notes (m3-vibeparse-p3)

- `final_text` on `terminated` / `turn_limit` (and fallback `limit`) runs is the last assistant
  message vibe wrote before it stopped: mid-run narration (the real terminated fixture ends on "...find the r1 worktree..."),
  not an answer to the task. Callers must check `vendor_exit_meaning` before treating
  `final_text` as a result. `None` means no abnormal ending was recognised; it does not by
  itself prove a normal finish (missing export with an assistant message, an unknown
  outcome, or `completed` are all `None`).
- `usage["model"]` is the configured alias from export `config.active_model` (e.g. `ds-flash`),
  not the provider model the request was routed to; the export and stream carry no routed model.
- Mutation check: changing the role test in `_scan_stream` to `!= "user"` makes
  `test_non_user_role_after_last_assistant` fail (the real fixture alone does not catch it).
- Missing or unreadable `export.json` with no assistant message is `run_error` /
  `usage["error"] = "no export"` (and `stderr_tail` when stderr has a line). If the stream
  already has an assistant message, usage and meaning stay `None`: the run produced model
  text, and a missing export is an incomplete artifact, not evidence of a failed run. The
  export is what classifies the exit; we do not invent `run_error` from its absence alone.
- Files are read as `utf-8-sig` (BOM stripped) in `_read_export` and the stream scan.
- If the LAST assistant line is truncated and unparseable, it is skipped and `final_text`
  falls back to the previous complete assistant message (pinned by
  `test_truncated_last_assistant_falls_back_to_previous`). An *empty but complete* last
  assistant message still wins as `""` and does not fall back.

## Fix round (Linear REVISE on #19)

- `error: true` / `error: 1` are errors again (`run_error`). The rule is now: not an error only
  for `None`, `""`, `false`, `0`, `{}`, `[]`; everything else is. Pinned by separate tests
  `test_export_error_true_is_error` and `test_export_error_one_is_error`.
- NaN / Infinity are replaced recursively in usage (nested dicts / lists). Recursion is bounded
  at `_NONFINITE_MAX_DEPTH` = 32 levels: below that a container is returned unchanged, so a
  non-finite number nested deeper stays a float NaN/Infinity. json.load already caps nesting
  (RecursionError -> no export), so the bound only keeps the sanitiser itself from recursing deeply.
- `usage["stderr_tail"]` is capped to the last 500 characters of the last non-empty stderr line.
- A valid-JSON export.json that is not an object (list, string, number, null) is labelled
  `"export not an object"` instead of `"no export"`; with an assistant message it still keeps
  usage/meaning `None`, the same as a missing export.
