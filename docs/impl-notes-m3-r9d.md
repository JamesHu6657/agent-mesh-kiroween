# Implementation notes: m3-r9d (ACC: K4, E17)

Branch `wt/m3-r9d`, starting at `8255315`. Follow-ups to Linear PR #4's
non-blocking post-ff error-classification finding and the main25 E17 test flake.

## Post-ff Git read failure

- Root cause: after a successful fast-forward, `deliver_to_base` converted a
  failed `rev-parse` to `landed = None`. Comparing that with the pinned integration
  SHA incorrectly produced rc 6 `VERSION_MISMATCH`, `reason: base_moved`, and
  dropped Git's actual return code and stderr.
- Fix: check the post-ff read's return code first and use the existing
  `_git_failed` helper. The result is rc 1 `INTERNAL_ERROR`, `reason: git_failed`,
  with `step`, `git_rc`, `stderr_tail`, repo, worktree, and base branch.
  Successful reads whose SHA differs from the pinned SHA still report
  `base_moved`. No other delivery paths changed.
- New unit test:
  `TestDeliverCas.test_post_ff_rev_parse_failure_reports_git_error` patches
  `repos.run_git` only for the exact post-ff `rev-parse --verify refs/heads/main`
  call. All other Git commands run against the real isolated fixture repository.
  It injects rc 128 and a config-error stderr, checks the error code/family and
  retained diagnostics, rejects `base_moved`, and confirms HEAD and main really
  landed on the pinned integration commit.
- Red/green evidence: the new test failed before the product change with
  `VERSION_MISMATCH != INTERNAL_ERROR` and `actual: None`. Afterwards,
  `python3 -m unittest -v tests.unit.test_repos_cas tests.unit.test_repos`
  passed all 20 tests in 2.635 s, including the existing descendant-base
  `base_moved` regression.

## E17 inbox concurrency-limit timing contract

- Existing test:
  `TestProjectControl.test_e17_inbox_no_response_clock_paused_at_concurrency_limit`
  in `tests/integration/test_execution_termination.py`.
- Root cause: the test shortened `inbox.ack_timeout_s` to 2 s before creating
  its first task. That task initially has a free slot, so its no-response clock
  correctly runs. CLI latency under contention can exceed the window, letting
  the scheduler fail it before the test's start request arrives. A start then
  correctly returns rc 6 `INVALID_TRANSITION` from `failed`.
  This is a test setup race, not a product bug.
- Fix: retain the isolated fixture's default 600 s ack window until the first
  task has successfully started and holds the slot. Only then set the original
  2 s window, before creating the second task. Replace the fixed 3.5 s sleep
  with a bounded wait for the second task's persisted `offered_at` to advance
  at least 3.5 s beyond its independent `task.queued` event timestamp.
  This observes the scheduler actually refreshing the blocked clock, rather
  than merely assuming a scheduler tick happened during a sleep.
- Product assertions remain: second task is queued with `concurrency_limit`,
  remains queued after more than its full 2 s window while blocked, fails
  specifically `no_response` after the slot frees, and its daemon failure
  timestamp is at least 1 s after the first task's `task.submitted` event.
  Doctor invariants still run. No retries, skips, serial marker, or inbox
  product-code changes were added.
- Deterministic reproduction: an in-memory test-only wrapper delayed the first
  `MeshHarness.cli("task", "start", ...)` call by 3.5 s. Before the fix, a
  read-only DB observation found `failed`, reason `no_response`, timeout 2;
  the existing assertion failed with rc 6 `INVALID_TRANSITION` (6.695 s).
  With exactly the same injected delay after the fix, the observation found
  `queued`, no failure, and the whole test passed (12.487 s). This diagnostic
  used inline Python, not a repository helper file.

### Three-way load evidence

Both phases use three concurrent Python processes, each looping 20 times over
the exact existing test ID. Each iteration uses the existing
`tests.run_tier.run_single_test(test_id, 120)` runner, which launches a fresh
`python3 -m unittest <test_id>` process and uses its exit status. Failures are
printed rather than retried. Thus each phase has 60 executions, not just 20.

| Phase | Lane 1 | Lane 2 | Lane 3 | Total |
| --- | --- | --- | --- | --- |
| Before | 20/20 (191.282 s) | 20/20 (191.810 s) | 20/20 (191.727 s) | 60/60 |
| After | 20/20 (205.887 s) | 20/20 (205.860 s) | 20/20 (205.180 s) | 60/60 |

The stochastic failure did not recur in this baseline load run. The controlled
delay above independently reproduces the precise reported failure. Both stress
phases completed before the final full suite; these are loaded, concurrent-loop
measurements, not idle per-test benchmarks.

## Final validation

- Ran `bash scripts/test.sh` exactly once, after the final product/test edits:
  **rc 0, wall 828 s (13 min 48 s)**, also measured as 828 s by the outer command.
  Existing tiers: **358 unit + 273 integration + 49 recovery = 680 tests**, all
  passed. Tier times: 33.228 s, 683.695 s, 110.350 s, respectively.
  The runner's tripwire checks passed. Log: `/tmp/mesh-m3-r9d-final-suite.log`.
- `git diff --check` passed. Final status showed only the four intended files.
- Expected pre-fix diagnostic failures are documented above; no unresolved
  validation failures or skipped requested checks remain. Work is complete.

## Scope and credits

- Changed: `mesh/core/repos.py`, `tests/unit/test_repos_cas.py`,
  `tests/integration/test_execution_termination.py`, and this notes file.
- The Git fault-injection test is new. The E17 test, repo unit tests, tier
  subprocess runner, and full-suite runner already existed.
- No protected coordination, requirements, plan, checklist, prompt, or Kiro
  files changed. No temporary scripts or helper files were created.
- Credit: Linear identified the Git classification issue; the main25 report
  identified the latency race. Implementation and validation: Factory Droid
  (GPT-6.1 Sol). Session credit usage is not exposed by this environment.
