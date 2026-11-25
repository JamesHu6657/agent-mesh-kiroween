# M1c run 5 — implementation notes (coverage, reconciliation, gate report)

Branch `wt/m1c-run5`, created from 0637609 (run 4 head). Run 4's review fixes
(review m1c-run4, REVISE) are folded into this run as one commit, as the coordinator asked.

**Where these notes live.** The m1c plan asks for a "Run 5" section in
`docs/impl-notes-m1c.md`. That file is on this run's no-edit list, so the Run 5 section
and the closing summary of every M1c deviation are here instead, and in §7 of
`docs/m1c-gate-report.md`. Flagged for the coordinator.

## 1. Commits

| Commit | Content | ACC |
|---|---|---|
| 81939b1 | Coverage: `test_execution_failure_policy.py` (F10, H9, H10, H14, J6); fake `fail_if_marker`; docs accuracy (usage.md, adapters.md: launch execution is built for `fake`); A1 serial cut (J6 and G12 lockout moved to the parallel phase) | F10, H9, H10, H14, J6, A1 |
| ee8e680 | Review m1c-run4 findings 1–6 (see run4 notes §10) | I5, J5, I1, I2, I8, H3, A1 |
| 3e58d00 | M1 reconciliation, M1b and M1c clauses | B5, D7, E4, E6, E10, E16, G16, H4, H8 |
| 04d96e2 | Load-proofing asked for by the coordinator after the main-branch run failed under heavy load | E16, E17 |
| 6118183 | M1 reconciliation, M1a clauses; vibe argv order per plan §14.6 | A5, A10, B1, B7, B13, C1, C3, C4, C5, C10, C11, C12, C14, D8, D10, G22, G24, I9, N6, N7, N15, N16 |

G23, G29, J7, N9 and N17 were already covered by `tests/unit/test_docs_m19.py` (M1.9 docs
branch). `mesh/cli/help.py` already carries the G23/G29 sentences; no edit.

## 2. Run 5 sub-steps

- **F10** uses the fake's new `fail_if_marker` (fail when a marker file exists in the
  workspace), so attempt 1 fails `exit_nonzero` and the reassign-from-failed path is real.
- **H9, H10, H14** run against real failures (fake exits non-zero), not hand-written rows.
- **J6** idles 60 s with agents blocked in `inbox wait` and asserts the waits stay registered and no run
  row is created (`run list` and the runs table unchanged, none starting/running). It is not `@serial`: it has no tight timing bound, only
  "nothing happens", and running it in the parallel phase takes 60 s off the serial tail.
  Same reasoning that review m1c-run2 (speedup 4) accepted for H8.
- **G12 `test_passphrase_lockout`** is also moved to the parallel phase. It sets the
  testing-only `auth.lockout_window_s` = 30 (was 10) and polls up to 75 s for the lapse, so
  load cannot age the five failures out of one window.
- The wait-timeout tests (J1, J9 latency bounds, G15) stay `@serial`.

## 3. Product changes in run 5

1. **Project waits count in `active_waits`** (`handle_project_wait`, try/finally). Inbox
   waits already did. Without it E16 could only `sleep()` before triggering completion; now
   it polls `wait_for_active_waits`. No behaviour change for clients.
2. **vibe argv order** (`mesh/adapters/vibe.py`): `--resume <sid>` now comes after the
   profile flags, as in §14.6 "argv built": `[--model] <profile flags> [--resume <sid>]
   [--trust]`. The new golden-argv matrix (N6) caught it. `--model` is still dropped with
   `--resume`. vibe launch is not enabled before M3, so nothing ran with the old order.
3. Review m1c-run4 product fixes (zombie wrapper identity, `_session_anchored`, leftover scan
   before reap, testing mode refused on the default home, `daemon start` waits up to 30 s):
   see run4 notes §10.

## 4. Readings

- **D7:** the Check's `--effort` on a vibe agent is tested on an **inbox** vibe agent
  (EFFORT_UNSUPPORTED, no task row). A vibe **launch** agent is refused first with rc 13
  ADAPTER_UNSUPPORTED (launch for vibe is M3), so effort is never reached there.
- **D8 JSON message:** for `--input -` the product renders the decoded value: JSON `true`
  gives `got 'True'`, JSON `"2"` gives `got '2'`. The Expect's `<v>` is read as the value
  Mesh received. If `<v>` is meant as the JSON source text (`'true'`, `'"2"'`), that is a
  product change in `policy.py` together with `tests/unit/test_settings` (which pins
  `got 'True'`). Left for the gate decision.
- **D10:** `config set scheduler.max_total_runs 0` returns `INVALID_CONCURRENCY`, as the
  checklist row and §5.7 say (not `INVALID_SETTING`).
- **B13:** `config list` has one key beyond §5.7, the testing-only `auth.lockout_window_s`
  (recorded deviation, impl-notes-m1c §1.8). The row asks for a superset; it passes.
- **C1 on gVisor:** `ss` cannot open netlink here and falls back to /proc (warning on
  stderr, rc 0). The test also cross-checks meshd's socket inodes against
  /proc/net/{tcp,tcp6,udp,udp6,...} and /proc/net/unix, so it does not rely on `ss` alone.
- **N7** traces file access in-process only (patched `open`/`os.open`/`os.listdir`/
  `os.scandir`/`os.stat`); the stub child process is not traced.
- **G24:** the CLI rc 13 path for an unsupported profile is the existing N5 test in
  `test_tasks.py`; the new test covers the adapter layer.
- **C15:** the checklist names `test_python_isolation_flags`; the test is
  `test_c15_python_isolation_flags`.
- **"F7 (PEM redaction) still open"** in run3 notes §8 was a mislabel. PEM private-key
  redaction is in `mesh/core/redact.py` and tested in `tests/unit/test_redact.py`. The F7
  row (state machine, `task accept` on a running task) is covered.

## 5. Load-proofing (04d96e2)

The main-branch suite ran under very heavy load (three suites at once, 28 min) and failed
two tests. Fixes:

- **E17 (both branches):** compared a test-side `time.time()` taken after the CLI returned
  with the daemon's `task.failed` ts; failed by 18 ms. Now both sides are daemon
  timestamps: `project.resumed` (pause branch) and `task.submitted` of the slot holder
  (concurrency branch) against `task.failed`, still `>= +1.0 s` with a 2 s window.
- **E16:** the `project wait --timeout 60s` client used the harness default 10 s
  subprocess timeout path under load; it now gets 180 s.
- **Scan for other test-clock vs daemon-clock comparisons:**
  - `PidWatcher` (termination tests) takes `now` *before* the liveness check, so
    "last seen alive < event ts" is causal and safe on one host clock.
  - J1 (`returned_at - task.submitted ts <= 1.0`) and J9 (`arrival - event ts < 2.0`) are
    upper-bound latency checks that the checklist itself asks for. Both are `@serial`; left
    as is.
  - recovery tests compute wait durations from daemon deadlines (no assertion on the test
    clock); `test_recovery.py:645` allows 50 ms and compares a run's `ended_at` with a
    stamp written by the child itself, before it is killed. No change.

## 6. Reconciliation

Every M1a, M1b and M1c row (46 + 47 + 70 = 163) was re-read against its tests. Before the
fixes: 130 covered, 29 partial (a clause of the Expect not asserted), 0 not covered,
4 manual (A1, A3, C1, A9). After 3e58d00 and 6118183 every partial row asserts its full
Expect; A1, A3 and C1 have recorded evidence in the gate report; A9 is coordinator-verified.

- `grep -rn '"M1c"' mesh/` is empty: no verb reports `M1c` as its milestone any more.
- No B11 stub test expects `M1c`.
- Harness teardown runs `doctor --check-invariants` (rc 0) for every integration and
  recovery class, except `test_placeholder` and the deliberately corrupted-DB doctor test.

## 7. Final suite (A1)

`bash scripts/test.sh` on HEAD 6118183 with `MESH_SUITE_TRIPWIRE_FILE=/tmp/r5tw.log`, no
other suite on the box: **rc 0**, unit 172 in 12.8 s, integration 205 in 411.4 s,
recovery 15 in 33.9 s, **wall 7m38s** (458 s, under 600 s). The tripwire file was never
created. Slowest: J6 72 s, G12 lockout 33 s, cancel-spares-children 31 s, H8 25 s.

An earlier run in this session overlapped a second copy of the suite (my mistake: a
background launch I believed killed was still running) plus other worktrees' suites; one
copy passed (rc 0, 29 min) and the other hit the same E16 timeout fixed in 04d96e2.

## 8. Open points

- **Adopted-wrapper residual (review m1c-run4):** an adopted wrapper is init's child, so an
  env-stripped descendant that has reparented away from every process meshd has seen is not
  found by the leftover scan. Recorded, not fixed.
- **A9** needs scratch-clone checkouts of real history; the commit list is in the gate
  report §5. The coordinator verifies.
- **D8** JSON wording (§4) is a reading for the gate decision.
