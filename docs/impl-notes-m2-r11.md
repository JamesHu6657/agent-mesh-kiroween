# M2 r11: session capture, verified resume and queued-merge recovery

Branch: `wt/m2-r11`. Starting HEAD: `d29aa79`.
Scope: L5, plus coordinator additions A (RunRequest options), B (durable queued
lead merges) and C (lead-watch docstring).

## Readings and decisions

- Consulted requirements, plan sections 12.3–12.6, 14.1 and 14.9, checklist
  section 0 (notation/gate rules) and L5, the M2 planner notes/r11 section/Q14,
  M1a/M1b/M1c notes (including M1c run 2–5), and earlier M2 run notes.
- Q14: always render the complete prompt, including the prior deliverable and
  rework feedback, whether or not resume is enabled.
- Resume requires `supported == "yes"` and `verification == "smoke"`, plus an
  existing id. `help`, `stale`, unknown, unsupported and missing capabilities
  never enable resume. Fake agent options are used when constructing adapters
  at both task and lead start.
- Capture sessions independently of resume verification and exit outcome.
  `_close_run` covers ordinary exits, killed runs and lost/recovery closes.
  Store the run's parsed id and update the task or that run's lead assignment.
  Missing/malformed output does not block closing; an exit without a new id
  retains the owner's last captured id. Re-closing an old run cannot replace it.
- Both start paths share `build_run_options`: configured `binary_path` when
  present, the real run id, then stored adapter options. Fake now honors the
  binary override. Droid already reads `binary` and `run_id` correctly, so
  `mesh/adapters/droid.py` needed no change.
- Queued merge results persist normalized request args and original action args.
  The first post-commit supervisor tick per boot re-queues successful queued
  `task.merge` results lacking a `task.merging` event or `phase:merge_start`
  result with the same request id. Queue insertion de-duplicates request ids.
  Legacy r10b events recover their action by index from the saved stdout block.
  Recovery preserves the original credential-backed lead principal/scope, not
  user authority. Merge TX1 rechecks current assignment and every other guard.
- No existing testing hold hook stops queued merges between close COMMIT and
  TX1. Used the coordinator-authorized direct unit-style recovery tests in
  `tests/recovery/test_recovery_lead_merge.py`, rather than adding a hook outside
  the allowed files. These test connection/queue loss, committed-only recovery,
  once-per-boot recovery through the supervisor, de-duplication, start/failure
  filtering, old event compatibility and superseded-lead rejection. No actual
  SIGKILL-at-this-boundary test was added.
- The pre-existing `prompts/droid-m2-r11.md` edit was left untouched and unstaged.
  No prohibited document, fixture, coordination file or other worktree changed.
- Runtime instructions prohibit overriding configured Git author/committer
  identity. Commits use the configured identity, not the requested `--author`,
  and include the required Factory Droid co-author trailer.

## Changed existing expectations

Coordinator-approved change only: the fake branch of
`tests/unit/test_adapters.py::golden_argv` now expects
`--fake-resume sess_1` for the existing resumed fake matrix cases (all five
profiles). This replaces the pre-r11 no-resume expectation identified in the
ruling. Existing matrix assertions, case counts, cwd/stdin checks and forbidden
flag checks are unchanged. The existing no-id cases still expect no flag, and a
new explicit fresh/resume golden test reinforces both outcomes.

No other pre-existing assertion was changed, weakened or deleted.

## ACC coverage and new checks

**L5: full.** New real-CLI scenario:
`tests.integration.test_resume.TestResume.test_l5_executor_rework_retry_and_lead_turns`.
One harness, two fake agents (one `resume_verified=true`, one default), and one
inbox lead. Each fake serves as executor and lead. Checks captured run/task/lead
ids; first-run absence of resume; verified rework/retry/next-turn flags; actual
fake receipt of the flag; unverified fresh runs; complete feedback/summary/spec/
criteria/authority prompts; configured binary in both start paths; and updated
lead session ids. All runs and homes are isolated under `/tmp/mh-*`, with polling
and no fixed sleeps in token-bearing turns. Teardown checks invariants/tripwire.

Four new adapter tests: `test_fake_session_parse_last_json_object`,
`test_fake_resume_golden_and_binary_options`,
`test_droid_real_run_id_and_binary_options`, and
`test_daemon_run_options_and_resume_verification`. These prove last-stdout-object
selection, malformed/non-object/stderr rejection, explicit fresh/resume argv,
binary selection, actual droid log-group id and capability gating, without
launching a vendor.

Four new direct recovery tests include the three queued-merge cases above and
`test_l5_all_close_paths_capture_sessions_without_erasing_last_id` (task and lead
exited/killed/lost closes, repeat closes, and absent ids).

Existing checks retained: the full golden matrix, fixture hashes, gitops
allow-list, supervision/termination/recovery tests, real-CLI rework/lead debounce/
lead merge action tests, invariant checks and both tripwires.

## Validation

The recorded pre-change baseline was **897 s**, as supplied in the coordinator
ruling; it was not re-run. Its tier counts/exit code were not supplied with that
ruling. All three full suites run here returned **rc 0**, and both tripwires
were empty/absent:

| `bash scripts/test.sh` | Unit | Integration | Recovery | Total | Wall |
|---|---:|---:|---:|---:|---:|
| After sub-step 1 | 280 | 252 | 17 | 549 | 850 s |
| Before persistence commit (new scenario tests also present) | 281 | 253 | 21 | 555 | 618 s |
| Final, after last code/test edit | 281 | 253 | 21 | 555 | 896 s |

The final 555 tests include nine new tests. **A1's <600 s gate is NOT met.**
The recorded unchanged baseline already exceeded it. No unrelated test runner,
timeouts, assertions or acceptance thresholds were relaxed to obtain a pass.

Exact focused commands and outcomes:

| Command | rc / count |
|---|---|
| `python3 -m unittest tests.unit.test_adapters -v` | 0 / 28 |
| `python3 -m unittest tests.unit.test_adapters tests.unit.test_supervisor tests.unit.test_recovery_units -v` | 1 / 37 entries: mistakenly named nonexistent `test_supervisor`; corrected below |
| `python3 -m unittest tests.unit.test_adapters tests.unit.test_recovery_units -v` | 0 / 36 |
| `python3 -m unittest tests.integration.test_execution_acc_run2.TestRun2Execution.test_f4_reject_rework_prompt tests.integration.test_lead_actions.TestLeadMergeAction tests.integration.test_lead_turns.TestLeadTurnDebounce -v` | 0 / 4 |
| `python3 -m unittest tests.integration.test_resume -v` | Initially 1 twice (new setup lacked explicit max concurrency, then used `--name` instead of required project `--title`); corrected, 0 / 1 |
| `python3 -m unittest tests.recovery.test_recovery_lead_merge -v` | Initially 1 / 4 setup errors (new task fixture omitted required columns); corrected, 0 / 4 |
| `python3 -m unittest discover -s tests/unit -t .` | 0 / 281 |
| `python3 -m unittest tests.recovery.test_recovery_lead_merge tests.integration.test_resume -v` | 0 / 5 |

`grep -rn "ACC: L5" tests/` found the passing integration/recovery tests and three
adapter test docstrings. `git diff --check` passed. No real vendor launch, network,
inference, push, identity configuration or test git operation was added.
`git branch --list 'mesh/*'` in this checkout returned no branches (rc 0).
Final explicit tripwire checks found both the checkout-specific suite file and
`/tmp/mesh-tripwire-hit.log` absent.

## Files and commits

- `mesh/adapters/fake.py`
- `mesh/testing/fake_agent.py`
- `mesh/daemon/supervisor.py`
- `mesh/daemon/lead_turns.py`
- `tests/unit/test_adapters.py`
- `tests/integration/test_resume.py` (new)
- `tests/recovery/test_recovery_lead_merge.py` (new)
- `docs/impl-notes-m2-r11.md` (new)

1. `a02bf5c`: fake session parse/resume flag and adapter goldens/options.
2. `093592c`: session persistence, verified resume, shared options, durable queued
   merge recovery and corrected watch docstring.
3. `ff5f737`: real-CLI resume scenario and direct recovery regression tests.
4. `docs(m2): notes for r11 (ACC: L5)`: this report.

No temporary iteration files remain. Test-created homes/processes are cleaned by
their fixtures. Dependency/build caches provided or generated by the environment
are left intact. Outstanding: the 600-second suite gate and the requested author
override, as described above.

## r11b

Reviewer follow-up N1, N2 and N3, limited to the five authorized files.

- **N1:** queued-merge recovery isolates each row. Missing/truncated legacy
  stdout, malformed payload JSON, missing/invalid action indices and unexpected
  row errors are logged and produce a terminal `lead.action_result` with
  `ok:false`, `phase:"merge_start"` and `reason`/`error_code:"RECOVERY_FAILED"`.
  The event's top-level reason is also `RECOVERY_FAILED`. JSON extraction in
  both the candidate scan and completion filter is guarded against corrupt
  historical payloads. Good rows still recover under the original principal.
  Completion matching remains request-based because merge TX1 has its own run.
- `_MERGE_RECOVERY_BOOT` is set in `finally`. The watch entry point catches
  unexpected recovery errors, including a failure of its error logger, before
  continuing normal run supervision. Failure recording is transactional; if the
  database cannot record the terminal event, recovery logs that error best-effort
  and continues rather than aborting the watch pass.
- **N2:** stored adapter options are applied first, then the daemon's actual
  `run_id` and configured `binary_path`. An option-level binary remains usable
  when no `binary_path` is configured.
- **N3:** new unit coverage confirms that `resume_verified:true` cannot enable
  resume for either Droid or Kiro.
- Added seven recovery cases, including the requested missing-stdout legacy
  row plus good row and a live run. That test exercises the real watch/lease
  renewal path with only process liveness mocked, verifies same-tick recovery
  and renewal, checks a second tick does not retry, and checks the persisted
  terminal result also prevents recovery on a later scan. Other cases cover
  truncated output, corrupt JSON, missing index, unexpected principal errors,
  distinct merge-run completion, boot marking, and logging/recording failures.
  Added two adapter unit cases only; no existing assertion changed.

Validation:

- Before the implementation, the 11-test regression selection returned rc 1
  (five errors and two failing subtests), reproducing recovery exceptions and
  option overrides. Wall time: 0.221 s. The vendor resume case already passed.
  An initial timing invocation returned rc 127 because `/usr/bin/time` is not
  installed; subsequent commands use the shell's `time`.
- Final focused command:
  `python3 -m unittest tests.recovery.test_recovery_lead_merge tests.unit.test_adapters tests.unit.test_recovery_units -v`.
  **rc 0, 49 tests, 4.212 s wall time**, including the nine new cases and existing
  adapter goldens/recovery-unit checks.
- Full `bash scripts/test.sh`, run once after the final code/test edits:
  **rc 0, 564 tests** (283 unit, 253 integration, 28 recovery),
  **600.086 s wall time** (`time`; runner reports 600 s).
  All existing tiers and both tripwire checks passed. The existing strict
  **A1 <600 s gate is still not met**, by 0.086 s on this run; no runner,
  timeout or threshold was changed.
- `git diff --check`: rc 0. No temporary iteration scripts/files were created.

All five requested r11b items are done; none is blocked. Nine new regression
cases supplement the existing suite. The earlier A1 timing issue remains
outside this fix's scope.

Only the five allowed files were edited. The existing untracked
`prompts/droid-m2-r11b.md` was not touched or staged. No push, fetch, branch or
worktree creation, vendor launch, or assertion weakening was performed.

## r11c

Scope: the seven queued lead-merge recovery follow-ups. Only the five authorized
implementation/test/notes files changed. Starting HEAD: `f4a5da2` (r11 + r11b
already present).

### Changes and decisions

- Added `parse_final_actions_block` without changing normal `parse_actions`
  behavior. Legacy recovery requires a complete, valid final actions fence,
  never an earlier valid plan. Conservatively, any later fenced block (including
  a non-actions block) also refuses recovery. Missing/invalid actions, indices,
  task ids, or disagreement with the event's task column, payload task aliases,
  original action args, or embedded request args produce one terminal failure.
- Invalid JSON is not enough to identify a merge. Decode complete top-level
  JSON members before the damage and require `cmd:"task.merge"`, `ok:true` and
  `status:"queued"`; nested markers and duplicate keys do not qualify. An
  identifiable corrupt queued merge receives one `RECOVERY_FAILED`, never a
  recovered merge. An unidentifiable row gets a durable
  `meta["lead_merge_recovery_skip:<seq>"]` marker and one skip log, not a
  mislabelled merge failure. Subsequent scans, including reopened connections,
  exclude it. An unavailable skip write is logged best-effort and still cannot
  turn an unidentified row into a merge failure.
- JSON validity follows the scan's SQLite `json_valid` result, not Python's
  permissive decoding of `NaN`/`Infinity`. Those values cannot bypass corrupt-row
  classification. Excessively nested damaged JSON is also safely identified or
  skipped without being labelled a merge merely because decoding raised.
- Every recovery failure stores the queued event's `source_seq`. Non-NULL
  request ids still match terminal evidence across runs (merge TX1 has its own
  run). NULL request ids match only exact source-sequence evidence, never other
  actions in the same run.
- A missing run yields a **durable detached terminal event**, not a recurring
  skip: top-level `run_id` is NULL, while payload `run_id` retains the original
  id and `source_seq` retains the source. The current events schema has no run
  FK; the regression additionally installs a restrictive insert trigger to
  exercise the reported FK-like failure condition.
- Strengthened the existing connection/memory-loss test: drain after the first
  supervisor recovery, require no second-boot-local queue insertion or scan,
  then change boot id and require another scan and identical recovered item.
- Extended the existing malformed/legacy test with the real
  `finish_lead_turn` → `execute_lead_actions` → internal merge dispatcher path,
  using a fake adapter and real merge guards. The close transaction commits,
  its payload contains normalized `args` and original `action_args`, then the
  test clears the queue and requires exactly the original item after recovery,
  even with unusable stdout. Only request-id generation is mocked.
- No existing assertion was weakened. The existing corrupt-row fixture now
  includes identifiable queued-merge markers so its original failure assertions
  remain meaningful under the requested new semantics. The bare `{` case is
  retained in the new unidentified-row regression and must produce no failure.
  Test homes remain `/tmp/mh-*`; no fixed sleep or temporary helper file added.

### Red-before-green evidence

Before touching either implementation file, ran:
`python3 -m unittest tests.recovery.test_recovery_lead_merge tests.unit.test_lead_actions -v`.
**rc 1, 31 tests, 10 failing subtests/checks and two errors, 0.330 s wall**.
All six new test methods failed on the unchanged starting implementation:

| New test (method suffix) | Observed old-code failure |
|---|---|
| `legacy_final_block_never_falls_back_to_an_earlier_task` | Earlier task was requeued after a truncated/invalid final block. |
| `legacy_stdout_task_must_agree_with_every_stored_task` | All five contradictory stored-task variants were requeued. |
| `unidentifiable_invalid_json_is_skipped_and_logged_once` | Corrupt unrelated actions were labelled merge failures, not skips. |
| `null_request_failures_match_only_the_source_event` | First terminal row suppressed the later NULL-request row: one failure, expected two. |
| `missing_run_records_detached_terminal_failure_once` | The restrictive insert trigger prevented any durable terminal row: zero, expected one. |
| `recovery_parser_requires_the_final_complete_actions_block` | Strict helper absent (ImportError). Also tested behavior by replacing the helper in memory with unchanged normal `parse_actions`: six failing suffix subtests. |

The extended existing production-path test also failed old code at its new
`source_seq` assertion, after successfully checking the real committed queue
payload and recovering its args. To verify the production-path assertion itself,
removed queued `args`/`action_args` persistence from `execute_lead_actions` in
memory only: that same test failed with `KeyError: 'args'` at the committed
payload check. The parser/persistence mutation checks ran two tests with the
expected failures, harness rc 0, 0.135 s wall.

Removed only `boot != _MERGE_RECOVERY_BOOT` in memory (replaced with `True`):
the strengthened existing boot test failed after the first queue drain because
the second watch requeued the merge. **One expected failure, harness rc 0,
0.118 s wall**. No source file was changed for any mutation check.

### Validation

- Focused final implementation/test run:
  `python3 -m unittest tests.recovery.test_recovery_lead_merge tests.unit.test_lead_actions tests.integration.test_lead_actions.TestLeadMergeAction -v`.
  **rc 0, 32 tests, 6.419 s wall**. This includes six new tests, two strengthened
  existing recovery tests, the existing parser checks and the existing real-CLI
  merge-action integration test.
- Final-review classification check: added nonfinite/nesting cases to the two
  existing affected regressions, reproduced **rc 1, two failures, 0.128 s wall**
  against the initial fix, then fixed classification at the SQL/Python boundary.
  The first incomplete full-suite invocation was stopped with SIGTERM during
  integration (284 unit tests had passed), so it is not counted as a full pass.
- Full `bash scripts/test.sh`, restarted after the final code/test edit:
  **rc 0, 570 tests** (284 unit, 253 integration, 33 recovery),
  **495.576 s wall** (`time`; runner reports 496 s). All three tiers passed.
  This completed run is below the existing strict A1 <600 s threshold.
- Both tripwires passed in the runner; explicit post-run checks found
  `/tmp/mesh-tripwire-suite-2028514813.log` and
  `/tmp/mesh-tripwire-hit.log` absent.
- Before the full run, `pgrep -fa run_tier.py` showed only the inspection
  command's Factory shell wrappers, no other actual test-suite runner.
  After the run it **did show another suite**: PID 64593,
  `python3 tests/run_tier.py tests/integration`, cwd
  `/home/user/workspace/mesh-wt-m3-r4`. That process was left untouched.
- `git diff --check`: rc 0. No temporary scripts or helper files were created.
  All seven requested r11c items are done; none is blocked or left unfinished.
