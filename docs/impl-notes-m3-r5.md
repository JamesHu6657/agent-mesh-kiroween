# Implementation notes: m3-r5 (evidence-driven launch, results-aware adapter show)

Branch wt/m3-r5 is based on wt/m3-r3 at bd51e7f, which includes the C1 probe fix.

## Files
- **`mesh/adapters/evidence.py` (new).**
  - `results_dir(testing_mode, env=None)` and `fixture_root(testing_mode, env=None)` follow Q3.
    - Outside testing mode they use `<repo>/tests/smoke/results` and `<repo>/tests/fixtures/real`, and the override is ignored.
    - In testing mode only `MESH_TEST_SMOKE_RESULTS_DIR` counts, with fixtures in its parent directory (the smoke staging layout `<S>/results` + `<S>/<adapter>/<version>/`). Without the override, both return None, so there is no evidence.
  - `effective_launch(conn, adapter, testing_mode, binary_path=None, env=None) -> Evidence(ok, reason, results_path, cases, probed_version, results_version, captured_at_utc, errors)`.
  - `launch_allowed(conn, adapter_id, testing_mode, binary_path=None)` takes an adapter id instead of an adapter instance. It is the clean entry point for `lead_launch_allowed` (lead_turns.py) once r10b and r5 are both merged. Nothing in lead_turns.py or server.py was touched.
  - `apply_to_probe(data, adapter, ev)` adds the N20 fields to `Probe.to_dict()`.
- **`mesh/daemon/handlers/task.py`:** AG step 4 hunk only. It calls `effective_launch(ctx.conn, adapter, ctx.testing_mode, agent["binary_path"])` and on failure raises `AdapterUnsupported(…, milestone=adapter.launch_milestone or "M3", reason=ev.reason)`. The import is local to the hunk.
- **`mesh/adapters/base.py`:**
  - `AdapterUnsupported(message, milestone=None, reason=None)` puts `details.reason` in when a reason is given.
  - The class `launch_enabled` stays False.
  - The comment at :17–19 now records the accepted D5 deviation.
- **`mesh/daemon/handlers/adapter.py`:** list, show, probe (single and all) run the live probe first, then evidence for the same binary, then `apply_to_probe`.
  - list items gain `launch_reason` and `results_file`, and their `status`, `launch_enabled` and `launch_milestone` are now the effective values.
- **`mesh/cli/commands/adapter.py`:** unchanged. The CLI returns the daemon's `data` as-is, so the new fields print without changes.

## Rules as implemented
1. **No results file for the adapter → `no_smoke_evidence`.** This is checked first, so an adapter with no evidence at all reports that whether or not it was probed. N4 (agy, never probed in that test) needs exactly this, and so does N23.
2. **Probe version.** The newest `adapter_probes` row (`ORDER BY probed_at DESC`) whose `binary_path` equals the agent's binary is used. When the agent has no binary set, the adapter's `resolve_binary(None)` is used instead: a PATH lookup that is also how `probe()` records the row. If there is no such row, or the row has no version → `no_probe_version`.
3. **Latest file (Q4) at another version → `evidence_stale`.**
4. **Validity.** `validate(doc, repo, fixture_root)` errors, a bad or missing results sidecar, an adapter mismatch, or a doc version that differs from the file name version → `evidence_invalid`, with the problems listed in `errors`.
5. **Launch cases.** If S-X-1 or S-X-6 is not `pass` → `launch_cases_failed`. Otherwise ok.
6. **Non-vendor kinds** (none, fake) return their class `launch_enabled`, with no reason. Only files and the DB are read; no process is started (unit test with `Popen` and `os.execv` patched to raise). hermes and openclaw are `kind = "vendor"`, so they go through the evidence rule (`no_smoke_evidence` today).

**Show rows (N20).** These rules apply per capability that has mapped cases (CASE_CAPS):
- **`smoke`:** when a mapped case passed in the latest doc at the probed version. `verified_at` is the doc's `captured_at_utc`; `verified_version` and `results_file` are also set. `results_file` is repo-relative for committed results, and the path as read otherwise.
- **`stale`:** when the doc is at another version and contains a mapped case.
- **Class value:** otherwise.
- **Status:** `verified` only when every `supported:"yes"` capability is `smoke`; otherwise `partially_verified`. Only a successful live probe gets this status; `not_installed`, `error` and `unverified` are left unchanged. The first suite run caught this in `test_adapter_probe_error_and_timeout`.
- **Launch fields:** `launch_milestone` is null when launch is enabled, and the class value (M3) when it isn't. `launch_reason` is set as well.

**Where evidence is checked:** at AG step 4 (create, assign, reassign) and, since r5b, in `task retry` (T19) and `task reject` (T12 → rework, which the scheduler requeues without AG). Both use `_requeue_launch_guard` in task.py: a launch-delivery assignee without evidence gets rc 13 `ADAPTER_UNSUPPORTED` with `details.reason`, before any write or approval use. **Not re-checked at T5:** a task already queued before its evidence went stale can still start; evidence is not checked again when the run is spawned.

## Tests
- **`tests/unit/test_adapter_evidence.py` (new, 7 tests).**
  - The trees are built in `/tmp/mh-ev-*` from copies of the real droid init-event fixture, with sidecars written by `tierlib.write_capture`.
  - The DB is in-memory, using the real `adapter_probes` DDL and `sqlite3.Row` like the daemon.
  - Coverage: ok; results location in testing and non-testing mode; every reason; Q4 ordering (date, then -N, and an older date never wins); synthetic refusal; version mismatch → stale rows; binary mismatch → `no_probe_version`; no spawn; non-vendor kinds; `launch_allowed`; `AdapterUnsupported.details`; the show rows and status.
- **`tests/integration/test_adapter_evidence.py` (new, 2 tests).**
  - Setup: one testing-mode daemon with the stubs as `extra_path`. The stub reports droid 1.3.0, so the probe row comes from the live probe and nothing is seeded. The override is set in meshd's env only.
  - With the override: `adapter show droid` has smoke rows and `launch_enabled:true`; list shows agy as `no_smoke_evidence`; `task create --assignee <droid agent> --dry-run` returns rc 0 with `would`, no run row, and an empty tripwire.
  - Same home, daemon restarted without the override: rc 13 `ADAPTER_UNSUPPORTED`, `details == {"milestone":"M3","reason":"no_smoke_evidence"}`.
  - On a non-testing daemon the override is ignored: the fields equal `effective_launch` over the committed directory.
- **`tests/integration/test_adapters.py`, N1 assertion only.** For each vendor, `launch_enabled == effective_launch(...)` over the daemon's results directory, using the binary from the daemon's newest probe row. `launch_milestone == "M3"` when false. All other assertions are unchanged.
- **`tests/integration/test_tasks.py` `test_n4_adapter_launch_disabled`.** All assertions are kept. It adds `details.reason == "no_smoke_evidence"` and a precondition that no agy results file exists (no override in os.environ, testing mode has no dir, no committed agy file).

## Expected after C3 (N23)
- **N1 in test_adapters.** This runs a non-testing daemon that reads the committed `tests/smoke/results`. droid becomes `true` there only if the committed results version equals the version the test's droid probe reports.
  - The stubs from `tests.stubs.create_test_stubs` report **1.3.0**. With C2/C3 results at 1.6.0, the N1 run gives droid `launch_enabled:false, reason:evidence_stale`. The assertion still holds, because it compares against `effective_launch`.
  - To see `true` there, the droid stub has to report 1.6.0. tests/stubs.py is outside r5's FILES.
  - Also in the N1 test: the `status == "partially_verified"` assertion stays. With 1.3.0 stubs, the rows are `stale` and the status stays partially_verified, so it holds.
- **Who sees the committed evidence.** Once committed droid 1.6.0 evidence exists, a non-testing daemon on a machine with real droid 1.6.0 that has been probed will report `launch_enabled:true`. Testing-mode suites never see the committed evidence (Q3).

## Suite
- Before r5 (wt/m3-r3 + r2b), idle: 451 s.
- First r5 run: 1 failure (status re-grade on error probes), fixed.
- After r5: rc 0 in 631 s (unit 315, integration 241, recovery 17); m2-r11 and main suites started near the end, so not fully idle.

## r5b: Kiro's r3b+r5 review (logs/raw/m3-r3b-r5.md)

- **Finding 1 (MAJOR), retry gate:** implemented, not recorded as a deviation. `handle_task_retry` checks right after the transition check. `handle_task_reject` is gated the same way, because rework is the other requeue path (scheduler T13, no AG). Integration test `test_retry_and_reject_gated`: failed → retry is rc 13 `{M3, no_smoke_evidence}`, submitted → reject is rc 13; both tasks keep their status, there is no run row, and the tripwire is empty.
- **Finding 2:** `Evidence.__bool__` returns `ok`, with a unit assertion that the result is falsy without evidence. When the lead path is wired in, `lead_launch_check` must call `effective_launch(conn, adapter, testing_mode, lead_agent["binary_path"])` and raise `AdapterUnsupported(..., reason=ev.reason)`; D4's vendor code gate still applies on top.
- **Finding 3:** only probe rows with status `partially_verified`/`verified` count. A stat-only freshness check: binary `st_mtime` later than the row's `probed_at` gives `evidence_stale` (error "binary modified after its last probe"), and capability rows show `stale`. A new good probe clears it. Unit test `test_failed_probe_rows_and_changed_binary`, with Popen patched to raise.
- **Finding 5:** the integration scenario now has an older-dated second file (2026-11-19, S-X-4 fail). Through `bin/mesh adapter show droid`, `results_file` and `verified_at` come from the 2026-11-20 file, and `model_selection` is `smoke`.
- **Finding 6:** the hermes/openclaw sentence and the retry sentence are corrected above.
- **Finding 10 (for the gate report):** N1/N23 equality holds; droid stays false in N1 because the stubs report 1.3.0. Getting `true` there needs an approved tests/stubs.py change in a later run.
- **Findings 8 and 9:** coordinator's.
- **Suite:** 9704a83 (fix) committed after a run that was green except two new, not yet committed tests (fixed). The run before the test commit was idle and rc 0: 463 s, unit 320, integration 242, recovery 17.

## Session handoff
**Done:** 9704a83 (fix), fff9c2f (test), f8aadad (docs) on wt/m3-r5. These cover findings 1, 2, 3, 5, 6 and 10. The tree is green: an idle run took 463 s.

**Remaining: findings 4 and 7, which are r3 files.** Drafts are written and untested. They are on the dev box at /tmp/r5b-B/{harness.py,test_smoke.py,test_smoke_cases.py}, which are full replacements for tests/smoke/harness.py, tests/smoke/test_smoke.py and tests/unit/test_smoke_cases.py. A copy is also at /tmp/quest/agent/mesh/s/m3r5b/.
- **Finding 4:**
  - `run_and_signal` now signals after the second stdout line (`_line_count`), at the deadline, or when the leader exits, and records `lines_at_term`.
  - `run_r4` gives `fail(exited_with_error)` via `_error_exit` (rc > 0, not 130/143, stderr non-empty).
  - New stub mode `term_then_err` and test `test_sx6_exited_with_error`. The pass test asserts `lines_at_term >= 2`.
- **Finding 7:** `_raise_interrupt` sets SIGTERM and SIGHUP to SIG_IGN before raising. `test_sigterm_to_harness_kills_vendor` patches `harness.signal_session` to send a second SIGTERM during cleanup.
- **Steps:**
  1. Diff the drafts against HEAD, copy them in, and run `python3 -m unittest tests.unit.test_smoke_cases tests.unit.test_smoke_harness`.
  2. Run the suite (/tmp/seqm35.sh writes to /tmp/m3r5.log), then commit `fix(m3): … (ACC: N11, N25)` with harness.py and test_smoke.py.
  3. Run the suite again, then commit `test(m3): … (ACC: N11, N25)` with test_smoke_cases.py.
  4. Add r5b lines for findings 4 and 7 to docs/impl-notes-m3-r3.md and commit them.

**Gotchas:**
- In the `count_one` exited-before-signal test, the one-line stub could now be seen as "first byte, not 2 lines". The loop still exits on `poll()`, so it stays `exited_before_signal`. Check that this holds.
- `term_then_err` sleeps 1 s in its SIGTERM handler, so `gone_within_s` has to be at least 5.
- Never put MESH_SMOKE* into os.environ.
- Author every commit with `--author "Quest <coder@example.invalid>"`. The worktree identity is wrong (finding 8).

## r5b-s2 (findings 4 and 7)
- **Commits:** 8e2eac2 fix (tests/smoke/harness.py, tests/smoke/test_smoke.py), 83e82b6 test (tests/unit/test_smoke_cases.py). Drafts from /tmp/r5b-B/ taken unchanged after review.
- **Finding 4:** `run_and_signal` sends SIGTERM once stdout has >= 2 lines, at `wait_first_byte_s`, or on leader exit; `signal_info.lines_at_term` recorded. `run_r4` reports `fail(exited_with_error)` when the group is gone but rc > 0 (not 130/143) with non-empty stderr (`stderr_excerpt` added). New stub mode `term_then_err` + `test_sx6_exited_with_error`; pass test asserts `lines_at_term >= 2`. The `count_one` gotcha holds: the loop exits on `poll()`, case stays `exited_before_signal` (test green).
- **Finding 7:** `_raise_interrupt` sets SIGTERM/SIGHUP to SIG_IGN before raising; `install/restore_interrupt_handlers` restore the previous ones. `test_sigterm_to_harness_kills_vendor` patches `harness.signal_session` to deliver a second SIGTERM during cleanup. Mutation check: removing the two SIG_IGN lines makes this test fail.
- **Verification:** offline `tests.unit.test_smoke_cases tests.unit.test_smoke_harness` 29 OK. Full `bash scripts/test.sh` on the final tree: RC=0, 321 unit / 242 integration / 17 recovery OK, tripwires empty, 871 s. Full suite on the fix-only tree (before the test commit): RC=0, 320/242/17, 1256 s. Both runs not idle (another worktree's suite, mesh-wt-m2-r12, was running).
