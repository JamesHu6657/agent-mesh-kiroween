# Implementation notes: m3-r3 (smoke cases and offline re-evaluation)

Branch wt/m3-r3, created from wt/m3-r2 at ef354e5. r2b (6792be9, 922c568, 110fbf4) was merged in at ecfe378 before the r3 commits.

Suite wall time: before r3, r2b on wt/m3-r2 took 822 s (rc 0, overlapped with m2-r7c). After r3, see the end of this file.

## Files
- **`tests/smoke/reevaluate.py` (new).** Pure evaluators shared by capture and re-evaluation:
  - `eval_sx1`..`eval_sx5`, `with_effort`, `excerpt`, `init_event`, `deferred_cases`;
  - `reevaluate_doc`, `reevaluate`, and the CLI `python3 -m tests.smoke.reevaluate RESULTS [--out /tmp/mh-*] [--fixture-root]`.
  - It re-parses the output-derived cases S-X-1/2/4/5 from their committed fixtures and sets `evaluated_from`. S-X-3, S-X-6, S-X-7 and S-DROID-8 are copied with `carried_from` (Q5).
  - It never spawns. It refuses a synthetic results path, synthetic fixtures, sha or sidecar mismatches, and any `--out` that isn't `/tmp/mh-*`. A synthetic input is never `pass`.
  - Output uses the Q4 name in `<out>/results/`, with a sidecar.
- **`tests/smoke/test_smoke.py` (new).**
  - R1..R6 as `run_r1`..`run_r6`.
  - `selected_runs` reads `MESH_SMOKE_CASES`.
  - `staged_session_id`: R2 resumes the session id from R1 in this process or from the staged S-X-2 observation.
  - `finish_stage` adds `not_run(not_selected)` for any droid case that is neither recorded nor in the latest staged doc, then calls `finish()`, which carries the staged cases (r2b).
  - `DroidSmoke`: gate in setUpClass. Its tests run R1..R6 in order and share one session. Model comes from `MESH_SMOKE_MODEL_DROID`. Effort comes from `MESH_SMOKE_EFFORT_DROID`, default `low`; `none` gives `not_applicable(effort_not_listed)`.
  - `DeferredAdapters`: kiro, agy and vibe get `not_run(deferred_m3_lite)`.
  - `mesh_commit` comes from `gitops.run_git`.
- **`tests/smoke/harness.py` (additive only, +117 lines vs wt/m3-r2).**
  - `SmokeSession.run_and_signal`, the S-X-6 shape: SIGTERM to every group in the session after the first byte or 5 s, then a poll until no pgid/sid member and no run-id process remains (15 s). It carries the r2b guards: duplicate refusal, terminate on BaseException, raw files removed in `finally`.
  - `SmokeSession.staged_documents()`.
- **`tests/unit/test_smoke_cases.py` (new, 10 tests).** Uses its own `/tmp/mh-*` droid stub that reacts to argv. Covers:
  - the full R1..R6 flow, then re-evaluation;
  - the two-stage merge, then promote;
  - S-X-6 failing when the stub ignores SIGTERM;
  - no init event, so S-X-3 is blocked with no spawn;
  - effort rejected, effort `none`, and the run cap;
  - the run_and_signal guards;
  - re-evaluation refusals;
  - synthetic input never passing;
  - selection, deferred cases and evaluator edge cases;
  - the tier skipping without the gate.

## Behaviour to know before C2
- **S-X-1 with droid 1.6.0 today.** droid `parse()` gives no `final_text` for the stream shape we expect. A run that prints MESH_SMOKE_OK records `fail(parse_pending)` with `{"raw_token_seen": true}`. When the parser learns final_text, the C3 re-evaluation turns it into `pass` with no new model run.
- **Retries.** A case that already has a capture in `$S` is refused before spawning (r2b). A C2 single-case retry therefore needs a fresh staging dir. Stage 2 can still resume R1's session if it is pointed at the stage-1 dir, because the carry goes through `results/`.
- **Discovery.** `scripts/test.sh` discovers tests/unit, tests/integration and tests/recovery only. tests/smoke runs only by explicit invocation and skips without `MESH_SMOKE=1`; the closed-gate check gave OK with skipped=2.

## Suite
- **After r3 (merged tree, r2b+r3):** rc 0. Unit 308 (+16), integration 239, recovery 17. Wall time 1254 s, overlapped with suites in mesh-fresh-v1 (main) and mesh-wt-m2-r10, so it isn't an idle number. One rerun follows.
- Tripwires empty. No writes to tests/fixtures/real/** or tests/smoke/results/**.
- **Rerun (idle, 07:55-08:02):** rc 0. Wall time **451 s** (unit 26 s, integration 390 s, recovery 35 s), under the 560 s m3-a1 threshold.

## r3b: Kiro's r2b+r3 review (logs/raw/m3-r2b-r3.md, REVISE: 2 MAJOR + 7 minor)

1. **S-X-6 exited before the signal (MAJOR 1).**
   - `run_and_signal` records `alive_at_term = proc.poll() is None` just before the SIGTERM, and `signal_info` now also carries `rc`.
   - `run_r4` gives `pass` only if `alive_at_term` and the session is gone. A leader that had already exited gives `fail(exited_before_signal)`; survivors stay `fail(group_survived_sigterm)`.
   - Tests:
     - rc 1 with no output, and one line then exit 0, both give `fail(exited_before_signal)` with one model run each. The one-line case delays `getsize` so the stub's exit is settled; this keeps it deterministic.
     - The pass test asserts `alive_at_term`.
2. **Single-case retries (MAJOR 2). Coordinator decision (a): there are no single-case retries in C2.** A re-capture is a full new C2 (stage 1 + stage 2) in a fresh `$S`. The harness refuses a second capture of a case in the same `$S` before spawning (r2b), so a retry can't spend credits there.
3. **SIGTERM/SIGHUP to the harness (finding 3).**
   - `harness.install_interrupt_handlers()` / `restore_interrupt_handlers()` turn both signals into `KeyboardInterrupt`. `DroidSmoke.setUpClass` and `harness.main` install them; `tearDownClass` and the end of `main` restore them.
   - Test: SIGTERM to the test process during a run kills the vendor session, leaves no `raw-*`, and the handlers are restored afterwards.
   - **C2 runs inside tmux.**
4. **A run that raised (finding 4).**
   - `finish_stage(s, runs)`: a case of a selected run with no record becomes `blocked(harness_error)`, not `not_selected`.
   - `tearDownClass` prints `$OUT` and the error when finishing fails.
   - Test: `parse` raises in R1 → S-X-1/2/4 `blocked(harness_error)`, one model run.
5. **Stage-2 resume cwd (finding 5): record only (coordinator decision).** The S-X-2 observation has `workspace` (R1's `--cwd`) and `init_cwd` (droid's init event `cwd`); the S-X-3 observation has `cwd`. The stage-1 workspace path is not recreated. So if S-X-3 fails in stage 2, a different cwd is a possible cause, and the observations show it.
6. **Evaluator deviations (finding 6).**
   - **S-X-4:** a model echoed in the init event that differs from the configured model is `fail(model_mismatch)`, stricter than the plan's "else `observed(not_observable)`". A missing init event or a missing model is still `observed(not_observable)`. The stricter rule is deliberate: a different model than the one consented must not look like evidence.
   - **S-X-5:** rc ≠ 0 is `not_applicable(model_or_effort_rejected)` only when stderr mentions `effort` or `model` (case-insensitive); otherwise it is `fail(rc_nonzero)`, so auth, network and quota failures aren't treated as a rejection. Neither path can give a false `pass`. `PureTest` has a line for each.
7. **Re-evaluation (finding 7).** A dict observation with `value == "hang"` (the `leftover_processes` form) also counts as timed out. In FlowTest, `ev.main` runs under `subprocess.Popen` patched to raise.
8. **Docstring (finding 8).** `run_and_signal` now says it checks pgid/sid only, and that a `setsid()` escapee is not detected. Behaviour is unchanged.
9. **Process (finding 9).** A suite run before each r3b code/test commit; the times are below. Review ranges: bd51e7f (the C1 probe fix) was named to the coordinator when it was committed.

r3b suite (idle, others=0): 451 s before the fix commit, 445 s before the test commit; unit 312, integration 239, recovery 17, all OK.
