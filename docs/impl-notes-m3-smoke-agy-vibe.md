# m3-smoke-agy-vibe: implementation notes (Grok Bot, 2026-11-10)

Opt-in smoke cases for agy and vibe, modelled on how PR #12 (m3-kiro-real) added kiro.
Branch `grok/m3-smoke-agy-vibe` = `review/m3-kiro-real` + the fixed `grok/m3-agyparse`
(9c7e59d) and `grok/m3-vibeparse` (8441798). **No agy or vibe model run was made**; every new
test uses a stub binary under `/tmp/mh-smoke-a-*` / `/tmp/mh-smoke-v-*` and passes on a box
without agy or vibe installed.

## What changed

| File | Change |
|---|---|
| `mesh/adapters/agy.py` | `AgyAdapter.stream_facts(stdout_text)` staticmethod only. |
| `mesh/adapters/vibe.py` | `VibeAdapter.stream_facts(stdout_text)` staticmethod only. |
| `mesh/adapters/smoke_schema.py` | **One line outside the task's file list:** `"S-VIBE-8": ()` in `CASE_CAPS` (see deviation 1). |
| `tests/smoke/reevaluate.py` | `stream_facts(stdout, adapter_id)` dispatches agy / vibe to the adapters; droid and kiro paths unchanged. |
| `tests/smoke/test_smoke.py` | `AgySmoke` (`agy`), `VibeSmoke` (`vibe`); `AGY_CASES`, `VIBE_CASES` (S-X-1..7 + `case8()`), added to `ADAPTER_CASES` and `SIGNAL_MIN_LINES`; R3 records `not_applicable(effort_refused_by_adapter)` when build() refuses the effort; R6 records the agy / vibe denial counts; `DeferredAdapters` deleted (it was empty). |
| `tests/unit/test_smoke_cases.py` | `test_tier_skips_without_gate` no longer loads `DeferredAdapters` and asserts it is gone. |
| `tests/unit/test_smoke_agy.py` (new, 9 tests), `tests/unit/test_smoke_vibe.py` (new, 7 tests) | Stub flows, vendor-error paths, S-X-6 min lines, gate-closed skip, stream_facts on the real fixtures (sha256 checked against the sidecars). |

## stream_facts

* **agy 1.5.3** (`stream-*.jsonl`): `session_id` = first `init.conversation_id` (fallback: the
  first `conversation_id` on a step_update / result event); `model` = `init.model`; `cwd` =
  `init.cwd`; `effort` = `init.effort` / `init.reasoning_effort` if ever present. agy 1.5.3
  prints no effort (the effort is in the model id, `gemini-3.8-flash-high`), so S-X-5 on agy
  is `observed(not_echoed)` at best.
* **vibe 3.1.0** (`streaming-terminated.jsonl`): only `session_id` (first non-empty
  `sessionId`). The stream has no model, effort or cwd (the model alias is in
  `vibe-out/export.json` `config.active_model`), so S-X-4 is `observed(not_observable)` and
  S-X-2 `init_cwd` is None.
* Since the merge of main after PR #12 (see "Merge with main after #12" below) both also return
  `stream_error` and `vendor_exit_meaning`: agy derives them from the stream, vibe always None.

## SIGNAL_MIN_LINES (S-X-6)

Rule from droid (2) and kiro (4): number of pre-model stdout lines + 1.

* agy = 3: `init` and the `user_input` step_update come before the first `agent_response`
  (stream-step1-denied.jsonl lines 1-2).
* vibe = 2: the user `message` line comes first (streaming-terminated.jsonl line 1).

## S-AGY-8 / S-VIBE-8 observation

* agy: `tool_errors` = tool step_updates in state `ERROR` (stream-fixC-denied.jsonl:14, a denied
  `run_command`); `denied_actions` = `len(result.denied_actions)` of the last result (None without a result,
  and None when that result has no `denied_actions` list; an absent list is not "0 denied").
* vibe: `failed_effects` = effect lines with `state.status == "failed"`; `denied_effects` =
  `state.error.code == "tool_denied"`. streaming-terminated.jsonl has two (lines 5 and 7,
  outside_workdir denials with decision `skip`). Whether vibe's `--agent ask` denies `touch
  y.txt` the same way in headless mode is unverified until a real S-VIBE-8 run.

## Decisions and deviations

1. **S-VIBE-8 added to `smoke_schema.CASE_CAPS`.** The schema had S-AGY-8 but no S-VIBE-8
   (only S-VIBE-9, the `--trust` case). `SmokeSession.run` refuses unknown case ids and
   `validate()` rejects them, so S-VIBE-8 via `case8()` cannot work without it. One line, no
   capability keys; S-VIBE-9 is untouched and still not implemented.
2. **R3 effort refused by build().** vibe raises `EffortUnsupported` for any effort; agy raises
   `EffortNotAllowed` when the model id already ends in an effort suffix (the agy default
   model does). Both raise before anything spawns (runs_used unchanged), so R3 catches them
   and records `not_applicable(effort_refused_by_adapter)` with the error code, effort and
   model. Only agy and vibe raise these, so droid and kiro are unchanged. For a real agy
   S-X-5, set `MESH_SMOKE_MODEL_AGY` to a model without an effort suffix (e.g.
   `claude-sonnet-4-6` from models.txt).
3. **Stub response text.** agy 1.5.3 left `result.response` empty in all three real captures
   (see impl-notes-m3-agyparse.md); the stub fills it with the token so the R1 pass path can be
   exercised. Whether a real R1 passes S-X-1 depends on where agy puts the reply text.
4. **Vendor-error stubs exit 0.** The real exit codes for an agy run without a result event
   and for a vibe export outcome `error` are not captured; the stubs exit 0 so S-X-1 reaches
   the `vendor_error` branch (a non-zero rc would already fail as `rc_nonzero`).
5. **Reevaluate and vibe export.json.** A smoke capture stores stdout and stderr only, so a
   re-parsed vibe S-X-1 sees no export (no outcome-based run_error, session id from the
   stream). Re-evaluating a vibe vendor-error capture can therefore differ from the original
   verdict; storing export.json with the capture would need a harness change (not in scope).
6. **`DeferredAdapters` deleted** (empty after agy and vibe were removed). After the #12 merge
   `reevaluate.deferred_cases()` was removed too (Quest #17 P3): no smoke path used it, only
   `PureTest`, which now asserts it is gone.

## Review decisions (Quest, 2026-11-10) and follow-up commit

1. **S-VIBE-8 in `CASE_CAPS`: approved**, kept to that one line.
2. **R3 `not_applicable(effort_refused_by_adapter)`: approved** provided it passes the
   results-schema validation. `smoke_schema` has no list of allowed reasons (any non-empty
   string is accepted for a non-pass status, `validate()` only checks that a reason exists), so
   nothing was added to the schema. Tested: the agy effort-refused test now writes the stage doc
   with `finish_stage()` and asserts `smoke_schema.validate()` returns no errors; the vibe full
   flow test already validates a doc whose S-X-5 has this reason.
3. **agy stub response is hypothetical.** The stub's pass path now fills `result.response`
   through a function named `stub_agy_response_hypothetical()`, and the module docstring and the
   flow test say that no real 1.5.3 capture shows this. New `STUB_SHAPE=real_1_5_3` mirrors the
   real shape (`result.response` "", no text on any `agent_response` step), and
   `AgyRealShapeTest.test_real_shape_no_final_text` pins what the evaluator does with it:
   `parse()` returns an empty `final_text`, there is no vendor error, so `eval_sx1` falls through
   to **S-X-1 `fail(parse_pending)`** with observation `{"raw_token_seen": false}` (rc 0). S-X-2
   still passes (session id from the init `conversation_id`) and R1 hands that id to R2. This is
   what a real agy smoke would record today, until agy is seen to put the reply somewhere.
   (`parse_pending` is the evaluator's existing "no token in final_text" reason; it is not
   agy-specific.)
4. **Vendor-error stubs exit 0: fine.** kiro does exit 0 after a runError; the agy and vibe exit
   codes on a vendor error are **unverified** (not captured), see decision 4 above.
5. **vibe re-evaluation without export.json: accepted as a documented limitation.** Re-eval
   uses stdout only, so for vibe `vendor_exit_meaning` can only come from the stream, and a
   re-parsed vendor-error capture can differ from the original verdict (see decision 5 above).

## Merge with main after #12 (2026-11-10)

`main` at fdd550d (PR #12 m3-kiro-real merged) was merged into this branch (merge commit, no
rebase). Git reported no textual conflicts. Main's #12 round-2 rules are kept as they are and
extended to agy / vibe where the smoke code uses those evaluators:

* **eval_sx6 signal rule** (unchanged): pass only for rc -15 / 143; other signals fail
  `unexpected_signal`; rc 0..128 fails `exited_with_error` on a `stream_error` or on rc > 0 with
  stderr. It reads `stream_facts(...)["stream_error"]`, so agy / vibe now provide one:
  * **agy** `stream_error` = `"run_error"` when the LAST `result` event has `status` != `SUCCESS`
    or a non-empty `error` (the real stream-step6-network-error.jsonl: status ERROR,
    finished_reason error). A missing result event is not a stream error (expected when the
    SIGTERM cuts the stream), the same split as kiro's runError-only `stream_error`.
  * **vibe** `stream_error` = None: the 3.1.0 stream has no run-level error event (outcome and
    error are only in export.json; `failed` effects are tool steps such as denials).
* **vendor_error checks in eval_sx2 / sx4 / sx5**: `_vendor_error()` falls back to
  `stream_facts(...)["vendor_exit_meaning"]` when no parse is given (sx4, sx5). agy now returns
  `stream_error`, or `"run_error"` when the stream has no result event at all, matching what
  `AgyAdapter.parse()` decides from the same stdout. vibe returns None (vendor errors only via
  `parse()` and export.json, sx1 / sx2).
* **staged_session_id rule** and **kiro any-runError**: kept from main untouched; the agy / vibe
  R1 -> R2 handoff goes through the same `staged_session_id` (S-X-2 pass needed, and S-X-2 now
  fails `vendor_error` on an agy error result).

New tests: `AgyStreamFactsTest.test_stream_error_and_vendor_error_rules` (literal sx2/4/5/6
verdicts on the real network-error capture, a cut stream, last-result-wins) and vibe S-X-6
literals in `test_stream_facts_real_fixture`.

**`raw_token_seen` on real agy / vibe runs** (Quest #17 P3): it is `true` for real runs, because
both CLIs echo the prompt into the stream (agy's `user_input` step_update, vibe's user `message`
line) and the prompt contains the token. So `raw_token_seen: true` there does not mean the model
answered with the token; only `final_text` (S-X-1) shows that. The hypothetical-stub test above
reports `false` because the stub does not echo the prompt.

## Tests

`bash scripts/test.sh` (all three tiers) on grokbot-box; counts in the PR.
