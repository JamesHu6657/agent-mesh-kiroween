# m3-kiro-real: implementation notes (Quest coder, 2026-11-10)

Scope (Quest coordinator task, James 2026-11-10 09:16 "Sol work goes to Quest coder"):
real Kiro parse with real fixtures, kiro smoke cases (S-X-1..7, S-KIRO-8), `MESH_SMOKE_E2E=kiro`
support in tests/smoke/test_e2e.py, the evidence path, and docs/adapters.md. **No Kiro model
run was made** (credits 1987.78/3000; a consented smoke is a separate step).

## What changed

| File | Change |
|---|---|
| `mesh/adapters/kiro.py` | `parse()` (finalText / chunk fallback, session id, usage, vendor_exit_meaning); `stream_facts()`; `parse_stderr_error` adds `tool_denied`; `DENIED_MARKER`; `_scan_lines` / `_scan_stream`. Profiles, build, probe unchanged. |
| `tests/fixtures/real/kiro/3.1.0/` | 5 new files + sidecars (below), promoted with `python3 -m tests.tierlib promote`. |
| `tests/smoke/reevaluate.py` | `stream_facts(stdout, adapter_id)`; evaluators take `adapter_id` (default `droid`, droid behaviour unchanged); S-X-1 fails `vendor_error` when the parse reports a vendor error (kiro exits 0 after a runError). |
| `tests/smoke/harness.py` | `run_and_signal(min_lines=2)` (additive; kiro passes 4). |
| `tests/smoke/test_smoke.py` | `_VendorSmoke` base; `DroidSmoke`, `KiroSmoke`; `KIRO_CASES`, `ADAPTER_CASES`, `case8()`; R6 records `S-KIRO-8` with a `denied_lines` count; R1 does not hand a vendor-error session to R2; deferred list is now agy, vibe. |
| `tests/smoke/test_e2e.py` | `_VendorE2E` base; `DroidE2E` (checks unchanged), `KiroE2E` (`chat --no-interactive --output-format stream-json`, `--trust-tools=`, never trust-all); `required_vars(adapter)`; kiro skips N26 (no `dev`). |
| tests | `test_adapters_kiro_real.py` (12), `test_smoke_kiro.py` (6, /tmp/mh-* stub), `test_smoke_e2e_gate.py` (+6). |
| `docs/adapters.md` | Kiro section. |

## Fixtures (provenance)

All are kiro-cli 3.1.0 output captured on base by earlier coordinator runs and kept in the
gitignored `logs/raw/`. `captured_at_utc` = the log file mtime (UTC). The prompt is not
recorded in the logs, so each sidecar `command` ends in the placeholder `<prompt>`.

| Fixture | Source (logs/raw) | Notes |
|---|---|---|
| `stream-effort-high-ok.jsonl` | `kiro-efforttest.jsonl` (verbatim) | finalText `KIRO_OK`, 0.054589 credits. Same session as the older `effort-high-run-metadata.jsonl` (that file is its metadata lines only). Command per the README: `--model claude-opus-5.5 --effort high`. |
| `stream-shell-forbidden-refusal.jsonl` | `kiro-permtest.jsonl` (verbatim) | Coordinator permission test (2026-11-07): one `CONTENT_FILTERED` (CYBER) refusal mid-run, a `shell` call rejected ("arguments supplied are forbidden"), then `status success`. The only capture with `metadata.modelId`. The agent profile used is not recorded; the sidecar command omits `--agent`. |
| `stream-shell-forbidden-refusal-stderr.txt` | `kiro-permtest.err` | The `[warn] Kiro refused ... (CYBER)` line. |
| `stream-run-error-auth.jsonl` | `kiro-plan-m1b-step7-check-20261107-133728.jsonl` (**excerpt**) | `runError` "bearer token ... invalid", no runFinished. **Excerpted**: the `tool_call` / `tool_call_update` / `agent_thought_chunk` lines were dropped (they held repo file dumps and e-mail addresses / IPs from the box); every kept line is byte-identical and in order. Command reconstructed from `scripts/coord/kiro-run-common.sh` (`--agent mesh-reviewer`). |
| `stderr-tool-denied.txt` | `kiro-review-m2-r9-20261109-020923.err` (verbatim) | 2 headless `[denied]` lines from a `kiro-run-common.sh` reviewer run. |

`.gitignore`d raw logs were checked for secrets before staging (gitleaks runs again in
`push-review.sh`).

## Decisions and deviations

1. **`dev` stays `PROFILE_UNSUPPORTED`** (plan §14.2 and the hard-coded golden table).
   Evidence found: the kiro-cli 3.1.0 binary's built-in docs index says the shell tool is
   `shell`, "also known as execute_bash (legacy alias)", and the stream reports
   `toolName: "shell"`; the binary carries a `--trust-tools: custom tool '...'` error
   string, so an unknown name probably fails loudly (not verified by a run). A candidate `dev` = `--trust-tools=fs_read,fs_write,execute_bash` needs a plan
   change and a smoke run; it is not built.
2. **finalText vs chunks.** 3.1.0's `finalText` equals the `agent_message_chunk` texts with
   one `\n` added between messages separated by tool calls; the fallback rebuilds exactly
   that (checked on all 4 stream logs in logs/raw). Thought chunks are excluded.
3. **vendor_exit_meaning** was informational here; superseded by the fix-round ruling below (rc 0 + a meaning = T8 `vendor_error`). A
   refusal followed by `status success` is *not* `content_filtered`.
4. **Usage keys** are kiro's own (`kiro_credits`, `turn_duration_ms`, `turns`,
   `context_usage_pct`, ...), not droid's token keys; kiro does not stream tokens.
5. **S-X-6 for kiro** signals after 4 stdout lines: `runStarted` and two `metadata` lines
   are written before the model call, so 2 lines would test termination before any model work.
6. **S-X-4 for kiro** will usually be `observed(not_observable)`: 3.1.0 sent `modelId` in 1
   of the 4 captured stream logs.

## Not done (needs James)

- The consented kiro smoke: `MESH_SMOKE=1 MESH_SMOKE_ADAPTERS=kiro MESH_SMOKE_CONSENT=kiro
  MESH_SMOKE_MAX_RUNS=1 MESH_SMOKE_MODEL_KIRO=<cheap model> MESH_SMOKE_OUT=/tmp/mh-kiro-smoke
  MESH_SMOKE_CASES=R1 python3 -m unittest tests.smoke.test_smoke.KiroSmoke -v`, then R4
  (S-X-6) the same way; `/usage` before and after each; ask James before the total passes
  ~2800 credits. Promotion of the results opens kiro launch (S-X-1 + S-X-6 pass).
- The kiro N13 e2e (`MESH_SMOKE_E2E=kiro`) only after that promotion.

## Smoke run (2026-11-10, James consent 09:46; model per James 09:49)

Run from wt/m3-kiro-real, readonly, no effort flag, MAX_RUNS=1 per stage, no retries.

- **Committed evidence: `claude-haiku-5.5`** (James 09:49; Kiro rate 0.06x). R1: S-X-1 pass,
  S-X-2 pass, S-X-4 observed(not_observable). R4: S-X-6 pass (SIGTERM at 3.2 s after 73
  lines, rc -15, group gone in 0.05 s). R1 metered 0.0019 credits.
- Earlier the same morning, before James's 09:49 switch, R1 + R4 were run once with
  `deepseek-3.2` (his 09:46 "Mistral or DeepSeek"; Kiro has no Mistral model). Same result
  (all pass; R1 0.0688 credits). That evidence was not committed.
- `/usage` kiroween bonus: 1987.78 before and after all four runs (the display rounds).
- Promoted with `python3 -m tests.tierlib promote`; the evidence gate now opens kiro 3.1.0.

## Fix round (Linear review A of PR #12, head 778611e, REVISE)

Merged main (db8ab40: agy/vibe parse PRs #13-#16) first; it merged clean (no conflicts), droid/agy/vibe code untouched.

**Ruling on plan §11.3 (supersedes decision 3 above).** §11.3/T7 say "exit 0 with no prior
submit → auto-submit" and T8 lists "run ended non-zero". kiro-cli 3.1.0 exits 0 after a
stream `runError` (real `stream-run-error-auth.jsonl`), so exit code alone cannot mean
success. Ruling: **exit 0 is a success only when `adapter.parse(run_dir).vendor_exit_meaning`
is empty.** Any non-empty meaning makes the run T8 with reason `vendor_error`
(`details.exit_code: 0`, `details.vendor_exit_meaning`, last 50 stderr lines), the same path as
`exit_nonzero`: lease released after the §11.4 release condition, `task.failed` to the lead,
**no automatic retry** (§11.6; `task retry` stays capped by `max_attempts`). The run itself stays
`status/end_reason exited`, `exit_code 0`, and its `result_json` gains `vendor_exit_meaning`.
Fail closed: the rule is "any non-empty meaning", not a list, because every adapter sets the
field only for a run that did not finish normally:

| adapter | meanings that now fail an rc-0 run | why |
|---|---|---|
| kiro | `auth_failed`, `run_error`, `content_filtered` | runError / refusal without a successful finish; no deliverable |
| agy | `run_error` (no result event, status != SUCCESS, result.error) | agy not launch-enabled yet; when it is, an rc-0 run without a SUCCESS result is not a delivery |
| vibe | `run_error`, `terminated`, `turn_limit`, `limit` | not launch-enabled yet; budget/turn stops and interrupts are not completed work. Partial work stays in the workspace; the lead can retry/reassign |
| droid | none (parse never sets it) | **unchanged** |
| fake / none / hermes / openclaw | none | unchanged |

A parse that raises or an unknown adapter counts as no meaning (exit code decides, as before).
Applied at all three exit-0 points in `mesh/daemon/supervisor.py`: `_finish_run` (normal exit
and recovery), the leftovers branch (no T7 while leftovers are terminated; `_finish_run` then
records T8), and `_needs_submit_git` (no `deliverable.git` collection for a run that will
fail). Not changed: lead turns (`lead_turns.py`) — out of this PR's scope; an rc-0 kiro lead
turn with a runError yields no actions block and is handled as an empty turn.

| finding | change | tests |
|---|---|---|
| MAJOR 1 rc-0 runError auto-submitted | `supervisor.vendor_failure_meaning()` + T8 `vendor_error` (above) | `tests/integration/test_vendor_exit_meaning.py`: `test_rc0_kiro_auth_error_stream_fails_task` (real kiro launch through the evidence gate with a stub `kiro-cli` that prints `tests/fixtures/synthetic/kiro/synthetic_run_error_auth.jsonl` and exits 0 → task `failed`/`vendor_error`/`auth_failed`, run `exited` rc 0 with the meaning, no `task.submitted`, one run after 2 s), `test_rc0_kiro_normal_stream_auto_submits` (same stub, `synthetic_run_ok.jsonl` → auto-submitted unchanged). Verified the failure test fails on the pre-fix supervisor (`'submitted' != 'failed'`). `tests/unit/test_vendor_failure_meaning.py` (kiro/agy/vibe/droid/fake/unknown on real fixtures, literal results) |
| MAJOR 2 S-X-6 own rc-0 error counted as SIGTERM pass | S-X-6 verdict moved to pure `reevaluate.eval_sx6`: own error exit = not a signal death (rc ≥ 0, not 130/143) and (rc > 0 with stderr, or an in-stream vendor error). rc −15 is never an own error | `test_smoke_kiro.KiroFixRoundTest.test_sx6_rc0_runerror_on_sigterm_fails` (stub writes runError on SIGTERM and exits 0 → fail `exited_with_error`), `test_sx6_eval_counterexamples`, `test_sx6_committed_r4_evidence_still_passes` (committed kiro R4, rc −15 → pass) |
| MINOR 1 staged resume of a failed R1 | `eval_sx2` fails `vendor_error` on an rc-0 vendor error; `run_r1` returns the id only if S-X-2 passed; `staged_session_id` reads only the latest staged R1 and requires S-X-2 pass + S-X-1 from the same capture (fixture + sha256) with rc 0 and not a vendor_error/timeout failure (S-X-1 `parse_pending` still allowed: droid rule unchanged, `test_smoke_cases` two-stage flow) | `test_staged_resume_refuses_failed_r1` (stage 1 runError, stage 2 R2 → `blocked(no_session_id)`, 1 model run), `test_staged_resume_uses_good_r1`, `test_staged_session_requires_sx1_pass_same_capture`, `test_sx2_sx4_rc0_vendor_error_fail` |
| MINOR 2 S-X-5 verifies effort on auth failure | `eval_sx5` (and `eval_sx4` for the same reason) fail `vendor_error` on an rc-0 in-stream vendor error | `test_sx5_rc0_vendor_error_fails` (real auth stream echoes effort=high → fail; real effort-high-ok → pass), `test_sx5_run_r3_stub_run_error` |

**Re-evaluation of committed evidence.** `python3 -m tests.smoke.reevaluate` over all five
committed droid/kiro results files gives case-for-case identical output with the fix and with
the pre-fix code (ab2a285). Kiro: S-X-1/S-X-2 pass, S-X-4 observed(not_observable), S-X-6 pass
(carried). Pre-existing, not from this round: re-evaluating droid `-09`/`-09-2` turns S-X-1
`fail(parse_pending)` into `pass` (droid parse landed after that capture), and S-X-2 drops the
capture-only `workspace`/`init_cwd` observation keys.

## Fix round 2 (Linear r2 + Quest review on 81e2836, both REVISE)

**Cross-vendor run-outcome contract (coordinator ruling, 2026-11-10).** Plan §11.3 / T7
"exit 0 with no prior submit → auto-submit" is amended to **"exit 0, no signal, and no vendor
failure meaning → auto-submit"**; T8 gains reason `vendor_error`. A vendor failure meaning is
any non-empty `RunResult.vendor_exit_meaning`. Every adapter sets it only when the run did not
finish its task: errors (kiro `auth_failed`/`run_error`, agy `run_error`, vibe `run_error`),
refusals (kiro `content_filtered`), **and budget / turn / time exhaustion or interruption**
(vibe `turn_limit`, `limit`, `terminated`; any adapter that reports one later). All of these
are T8 `vendor_error` (details: `exit_code`, `vendor_exit_meaning`, stderr tail), never an
auto-submit; partial work stays in the workspace and the lead decides retry / reassign /
escalate from the failure details (§11.6, no automatic retry). droid sets no meaning (unchanged).

| item | change | tests |
|---|---|---|
| 1 S-X-6 any rc<0 passed | `eval_sx6`: pass only for `SX6_TERM_RCS = (-15, 143)` (the harness sends SIGTERM in the window; 143 = shell-mapped SIGTERM). The harness SIGKILLs only after the window, and then `group_gone_after_term` is False, so -9/137 in the window is never its doing. Other rc<0 or rc>128 (incl. 130 SIGINT, which the harness never sends) → `fail(unexpected_signal)` with `signal`; rc None → `fail(no_rc)`; rc 0..128 with an explicit stream error (kiro runError, new `stream_error` fact) → `fail(exited_with_error)`; rc>0 + stderr → fail as before | `test_smoke_kiro.KiroRound2Test.test_sx6_only_harness_sigterm_passes`, `test_sx6_stub_ignores_sigterm_then_aborts` (stub ignores SIGTERM then `os.abort()` → rc -6 → unexpected_signal); committed R4 (rc -15) still passes: `KiroFixRoundTest.test_sx6_committed_r4_evidence_still_passes` |
| 2 kiro runError shape | `_scan_lines`: any `runError` event counts (null/missing stage, message or data) → `auth_failed` if the message names the bearer token/login, else `run_error`, also after `runFinished(success)`. **Success needs a `runFinished` with status `success`**: none (truncated / killed / empty stream) or another status → `run_error` (same idea as agy's no-result rule); a refusal without it stays `content_filtered`. `stream_facts` adds `stream_error` (runError only) and `run_finished` | `KiroRound2Test.test_any_runerror_shape_is_an_error`, `test_no_successful_runfinished_is_run_error`, `test_real_fixtures_unchanged`; `test_adapters_kiro_real.test_empty_missing_and_malformed` now expects `run_error`; integration `test_rc0_kiro_runerror_null_fields_fails_task` (full launch, `synthetic_run_error_null_fields.jsonl` → failed/vendor_error/run_error, never submitted) |
| 3 untested exit-0 points | leftovers branch: integration `test_rc0_kiro_auth_error_with_leftover_fails_task` (stub leaves a setsid grandchild; asserts `run.leftovers_terminated` and failed, no `task.submitted`) + control `test_rc0_kiro_ok_with_leftover_auto_submits`. `_needs_submit_git`: `test_vendor_failure_meaning.VendorMeaningCacheAndGitTest.test_needs_submit_git_skips_vendor_failure` (direct; its only effect is a git-info thread, not observable from the CLI) | mutation results below |
| 4 per-tick re-parse | `vendor_failure_meaning` caches per run dir keyed by (size, mtime_ns) of `stdout.log` and `vibe-out/export.json`; changed output is parsed again; entries dropped with the git cache when the run is no longer active (cap 1024) | `test_one_parse_per_run_until_output_changes` |
| 5 policy | contract above; `docs/adapters.md` Run outcome | — |

Hot path (base, Python 3.11): a 100 MiB kiro stdout parses once in 0.96 s on the exit tick;
every later call is two `stat`s, ~6 µs. Auto-submit still parses once more for the
deliverable (`parse_run_output`), as before this PR.

Mutation check (each mutation applied alone, targeted modules, then restored):
M1 `_finish_run` check removed: killed (3) · M2 leftovers check removed: killed (1, the leftover test) ·
M3 `_needs_submit_git` check removed: killed (1) · M4 cache bypassed: killed · M5 cache ignores
output change: killed · M6 kiro runError needs stage/message: killed · M7 no-runFinished not an
error: killed (3) · M8 eval_sx6 any rc<0 passes: killed (2) · M9 rc None passes: killed ·
M10 eval_sx6 ignores stream_error: killed (3).

**Known limitation (P3).** Lead turns (`lead_turns.py`) still judge by rc only: an rc-0 kiro
lead turn ending in runError is recorded ok, its final text is the pre-error chunks (usually no
actions block, so nothing is applied).
