# M2 r7c: merge recovery minors (ACC: K14)

Branch: `wt/m2-r7c`. Scope: r7b N1, N2, and the per-run exception logging nit.

## Changes

- **N1, current Git evidence.** Without `MERGE_HEAD`, recovery now always reads
  HEAD's parents/message and the full task branch ref. The saved pre-lease probe
  does not gate reconciliation or determine whether the tree was restored.
  A matching merge uses its first parent. Before a merge, or after a previous
  recovery aborted it, current HEAD is the baseline. A differing saved probe is
  retained as `details.recorded_pre_head`; `pre_head` records the current baseline.
  Dirty paths are inspected and recorded even when HEAD moved. If a later Git
  operation fails, recovery keeps the baseline it already observed and reports
  `restored:null`.
- **Existing negative cases remain conservative.** An unmatched commit mentioning
  this task, or containing its current branch tip as a non-first parent, is not
  treated as restored merely because its tree is clean. Its first parent supplies
  the baseline, with `head`, `head_moved:true`, `merge_commit_match:false`, and
  `restored:false`. All existing negative-reconciliation assertions are unchanged.
- **N2, `merge.recovery_blocked`.** A step-2 exception leaving an open merge run
  and a task in `merging` emits an audit event with project/task/run identifiers,
  a `reason`, and payload `{task_id, run_id, reason}`. The reason is the exception
  class plus its redacted message. An immediate transaction checks the current
  task/run state and existing events, then inserts only if that task has never
  reported that redacted reason. Thus A, A, B, A produces two events, including
  across database reopen/daemon restart. Deduplication is per task, not per tick
  or run. The task, version, run, and lease stay unchanged until recovery succeeds.
  No registry change is needed: `record_event` accepts new event types. This is
  an audit event, not a new inbox notification.
- **Exception isolation/logging.** Preparation and closing exceptions are still
  returned for the affected run, without preventing other runs from recovering.
  They are also logged to the database home's `logs/meshd.log`, with both run and
  task identifiers. Termination-step exceptions are logged too, retaining the
  existing `run.kill_failed` behavior. Logging/event-reporting failures cannot
  abort the batch; an event-writing failure is logged when logging is available.

## Tests

New: seven tests in `tests/unit/test_merge_recovery_minors.py`. They reuse the
existing `/tmp/mh-*` Git/database fixtures and add no sleeps or polling windows.
They cover current refs with a valid stale probe, clean/dirty trees and a Git
probe error; recovery repeated after abort; reconciliation even when the saved
probe equals HEAD; persisted step-2/step-5 details and repeat recovery; durable
event deduplication, redaction, event-chain integrity, unchanged blocked state and
eventual successful recovery; preparation/closing failure isolation; and an
unavailable event writer.

Existing checks: all 21 tests in `tests.unit.test_merge` and both real-daemon
SIGKILL/restart tests in `tests.recovery.test_recovery_merge`, unchanged.

Measurements use a fresh Python process around each command, with
`time.monotonic()` wall time. No other tests from this run overlapped a suite.

Commands:

- Reproduction: `python3 -m unittest -v tests.unit.test_merge_recovery_minors`.
- Focused checks: `python3 -m unittest -v tests.unit.test_merge tests.unit.test_merge_recovery_minors tests.recovery.test_recovery_merge`.
- Full suite, once before changes and once at the end: `bash scripts/test.sh`.

| Check | rc | Tests | Wall time |
|---|---:|---:|---:|
| Before implementation: initial five new regression tests | 1 | 5, nine failing subtests/assertions and one error | 0.770 s |
| Initial focused existing/new merge checks | 0 | 28 | 12.470 s |
| Final focused existing/new merge checks | 0 | 30 | 25.789 s |
| Baseline `bash scripts/test.sh` | 0 | 266 unit + 247 integration + 17 recovery = 530 | 1125.009 s |
| Final `bash scripts/test.sh` | 0 | 273 unit + 247 integration + 17 recovery = 537 | 798.673 s |

Both full suites passed all tiers and the runner's tripwire checks. The baseline
already exceeds the 600 s idle-suite budget in this environment; the final suite
also exceeds it. Neither result is an idle-performance claim, and the lower final
time is not attributed to this patch. No suite-speed changes were made.

The first timing launcher could not find
`/usr/bin/time` (rc 127), so it did not start the suite; the measurements above
use Python instead. `git diff --check` also passed. No existing test or assertion
was edited, and no files outside the allowed scope were changed.

Done: N1, N2, exception logging/isolation, regression coverage, and these notes.
Not achieved: the 600 s timing target, already exceeded by the unchanged baseline.
