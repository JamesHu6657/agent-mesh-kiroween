# m3-a1-speed: integration-tier speedups (test-only)

Branch `grok/m3-a1-speed` from `origin/main` at `dcfeb80` (includes #19). Test files only,
plus these notes. Nothing under `mesh/`, `scripts/`, `tests/helpers.py`, `tests/smoke/**` or
`tests/fixtures/real/**` changed, and no knob was added.

## Files that were off limits

`git diff --name-only origin/main...<branch>` on 2026-11-10:

- PR #12 `review/m3-kiro-real`: test files `tests/integration/test_vendor_exit_meaning.py`,
  `tests/unit/test_adapters_kiro_real.py`, `tests/unit/test_smoke_e2e_gate.py`,
  `tests/unit/test_smoke_kiro.py`, `tests/unit/test_vendor_failure_meaning.py`, plus
  `tests/smoke/**`, `tests/fixtures/real/kiro/**`, `tests/fixtures/synthetic/kiro/*`.
- PR #17 `grok/m3-smoke-agy-vibe`: the #12 set plus `tests/unit/test_adapters_agy_real.py`,
  `tests/unit/test_smoke_agy.py`, `tests/unit/test_smoke_cases.py`, `tests/unit/test_smoke_vibe.py`.
- PR #20 `grok/m3-runtargets-race`: `tests/integration/test_execution_termination.py`,
  `tests/unit/test_runtargets_race.py`, `tests/unit/test_seen_hold_r4d.py`,
  `tests/unit/test_seen_hold_r4e.py`.

None of them is touched here. That rules out `test_cancel_does_not_kill_unrelated_children`
(31 s, in `test_execution_termination.py`).

## Profile (idle box, 8 vCPU, `origin/main`)

Two full `scripts/test.sh` runs on `origin/main` on this box gave integration 180.3 s and
183.4 s (unit 27 s, recovery 24 s, total 232 s and 234 s). This box is faster than the one
that measured 457 s, but the shape is what matters.

How `run_tier.py` spends the integration wall time:

| lane | tests | summed test time | wall share |
|---|---|---|---|
| parallel pool (8 workers) | 254 | 841 to 851 s | about 106 s (sum / 8) |
| idle-bound pool (beside it) | 4 | 155 s | hidden behind the pool (longest 69 s) |
| serial, one at a time | 15 | 73 to 75 s | 73 to 75 s, all of it |

So wall time is roughly (parallel sum / 8) + (serial sum). Findings:

1. **Each test is its own `python -m unittest <id>` process**, so `setUpClass` sharing a
   daemon per class saves nothing under `run_tier.py`: the class is set up again in every
   process. The daemon-per-class idea needs a runner change (see proposals).
2. **Per-test fixed cost dominates.** The median integration test is 2.5 s, and 233 of 273
   tests are under 5 s (542 s of the 851 s). Harness start is about 0.3 s and teardown about
   0.2 to 0.3 s; the rest of a short test is CLI calls. A token CLI call costs about 0.06 s,
   a `user=True` call about 0.13 to 0.16 s, because each one derives K_user with scrypt
   (N=2^15, about 66 ms CPU here). A typical setUp is 12 to 14 CLI calls (1.1 to 2.0 s).
3. **The parallel pool is throughput-bound.** Taking the idle waits out of parallel tests
   makes them shorter, but the freed worker slot is filled at once, the other tests run
   slower under the extra load, and the summed time barely moves (see the first after-run
   below: changed tests -51 s, all other tests +75 s). Wait cuts in the pool help a box with
   spare cores, not this one.
4. **The serial lane is pure latency** with the rest of the box idle, so every second cut
   there is a second of wall time. Its biggest costs are setUp (about 27 s of the 73 s) and
   the lease, ack and wait windows.
5. The four idle-bound tests (`test_j6_idle_60s_no_runs` 69 s, `test_passphrase_lockout`
   31 s, `test_cancel_does_not_kill_unrelated_children` 31 s, `test_h8_exit_nonzero_no_retry`
   24 s) run beside the pool and finish before it, so shrinking them would not change wall
   time today. They are left alone: the 60 s idle window and the lockout window are the
   assertions themselves, and the third is in a forbidden file.

## Changes

Every assertion is kept. Where a test now holds a run with a release file, it also asserts
the run is still `running` right before the release, which is stricter than before.

| test | change | why it still proves the property |
|---|---|---|
| `test_lead_turns.TestLeadRunTokenScope.test_l1_lead_token_confined_to_its_project` | turn held by `wait_release` instead of a 30 s sleep; released after the last scope check | the turn is asserted `running` when the checks end, so every check ran with a live turn token; the turn then exits 0 as before |
| `test_lead_turns.TestLeadRepoWorktree.test_k13_lead_repo_detached_during_turn` | first turn held by `wait_release` instead of a 4 s sleep | the worktree checks run while the turn is asserted `running`; the second turn finds the file and exits at once, as the old 4 s sleep did, before the post-turn checks |
| `test_lead_turns.TestLeadTurnDebounce.test_l1_no_overlapping_turns` | first turn held by `wait_release` instead of a 3 s sleep; fix round: `lead.wake_debounce_s` 0 for this test, and the 3.5 s no-second-turn window starts only once the second batch is due | see "Fix round" below: the window now covers at least three scheduler ticks with the second batch eligible, and a lease-guard mutation fails the test; the no-overlap span check is unchanged |
| `test_lead_wakes_m2.TestLeadWakesM2.test_complete_ignores_only_verified_own_turn_lease` | `lead.wake_debounce_s` 1 instead of 2 (existing setting, testing range 0..3600) | five sequential turns; each follow-up is sent after the previous turn closed, and nothing in this test checks debounce timing (the tests that do keep 2 s) |
| `test_execution_acc_run2.TestRun2Execution.test_d11_concurrency_limit_two` | fake runs sleep 2 s instead of 3 s; fix round: the queue is watched from a thread started before the first create | five runs at max 2 keep tasks queued for about 4 s, and the watcher sees the whole span; every queued item seen must still carry `queue_position` and `concurrency_limit`; the overlap, pid-count, H5 and doctor checks are unchanged |
| `test_inbox.TestInbox.test_inbox_wait_does_not_block_concurrent_requests` (serial) | waiter `--timeout 3s` instead of 5 s; new assertion that the wait is still pending when the concurrent request ends | the request bound stays 2 s, and the new assertion proves the wait was open the whole time, which the old test only implied |
| `test_tasks.TestTasksIntegration.test_task_wait_state_reached_and_timeout` (serial) | the `state:running` wait times out after 1 s instead of 2 s | any expired timeout proves rc 8 WAIT_TIMEOUT; step 3 of the same test already uses 1 s |
| `test_projects.TestProjectsIntegration.test_project_wait_terminal_and_timeout` (serial) | the `completed` wait times out after 1 s instead of 2 s | same as above, for project wait |
| `test_execution_inbox.TestExecutionInbox.test_h7_inbox_no_response` (serial) | `inbox.ack_timeout_s` 1 instead of 2 (existing setting, testing minimum 1); fix round: the live `task wait --until state:queued` is replaced by an event-log check | the log must show `task.queued` -> `task.offered` -> `task.failed` (from `queued`), with at least 1 s between offer and failure; nothing transient has to be caught live, so a slow client cannot miss it |
| `test_execution_inbox.TestExecutionInbox.test_h6_inbox_lease_expiry_and_heartbeat` (serial) | Task B heartbeats for 4 s instead of 5 s | 4 s is twice the 2 s ttl, so without the heartbeats the lease would have lapsed twice, as Task A in the same test shows |
| `TestExecutionInbox.setUp` (21 tests: 5 serial, 16 parallel) and `TestInbox.setUp` (10 tests: 4 serial, 6 parallel) | the independent agents are created concurrently with the same `create_agent` CLI calls | same agents, capabilities and tokens; only the order of `agent.created` events and agent ids differs, and no test in these files reads either. setUp went from 2.0 s to 1.4 s and from 1.1 s to 0.9 s: about 12.6 s summed over TestExecutionInbox (3.0 s of it in the serial lane) and 2.0 s over TestInbox (0.8 s serial) |

Lease ttl stays at 2 s in H4, H6 and H15: after the expiry, each of them runs several CLI
calls inside the next attempt's lease, and 1 s leaves too little margin. G15 keeps its 2 s
approval ttl for the same reason (approve has to land before expiry). J2's 2 s timeout has
duration bounds of its own and stays.

## Evidence

### Changed tests, alone on the idle box

Before is `origin/main` run once, after is this branch run three times, one test at a time.
These after numbers were taken before the concurrent setUp change, so H6, H7 and the inbox
test are now a little faster than shown. All runs were rc 0, `Ran 1 test`, with the same
assertions.

| test | before (s) | after, 3 runs (s) |
|---|---|---|
| `test_lead_turns.TestLeadRunTokenScope.test_l1_lead_token_confined_to_its_project` | 34.76 | 5.49, 5.53, 5.51 |
| `test_lead_turns.TestLeadRepoWorktree.test_k13_lead_repo_detached_during_turn` | 18.06 | 9.94, 9.89, 9.95 |
| `test_lead_turns.TestLeadTurnDebounce.test_l1_no_overlapping_turns` | 10.49 | 7.58, 7.45, 7.45 |
| `test_inbox.TestInbox.test_inbox_wait_does_not_block_concurrent_requests` | 6.59 | 4.49, 4.59, 4.54 |
| `test_tasks.TestTasksIntegration.test_task_wait_state_reached_and_timeout` | 5.37 | 4.43, 4.66, 4.49 |
| `test_projects.TestProjectsIntegration.test_project_wait_terminal_and_timeout` | 4.86 | 4.03, 3.85, 4.00 |
| `test_execution_inbox.TestExecutionInbox.test_h7_inbox_no_response` | 4.61 | 3.65, 3.60, 3.72 |
| `test_execution_inbox.TestExecutionInbox.test_h6_inbox_lease_expiry_and_heartbeat` | 10.29 | 9.78, 9.68, 9.91 |
| `test_lead_wakes_m2.TestLeadWakesM2.test_complete_ignores_only_verified_own_turn_lease` | 20.70 | 14.97, 14.72, 14.81 |
| `test_execution_acc_run2.TestRun2Execution.test_d11_concurrency_limit_two` | 13.55 | 10.39, 10.44, 10.42 |
| total | 129.3 | 74.7 (mean) |

### Full `scripts/test.sh`, same idle box

| run | unit | integration | recovery | total wall | counts |
|---|---|---|---|---|---|
| before 1 (`origin/main`) | 27.0 s | 180.3 s | 23.6 s | 232 s | 440 / 273 / 49, OK |
| before 2 (`origin/main`) | 26.6 s | 183.4 s | 23.5 s | 234 s | 440 / 273 / 49, OK |
| after, lead and wait changes only | 26.9 s | 179.6 s | 23.8 s | 231 s | 440 / 273 / 49, OK |
| after, final branch | 26.8 s | 164.8 s | 23.7 s | 216 s | 440 / 273 / 49, OK |

Final branch against the mean of the two before runs: integration -17 s (-9 %), serial lane
74 s to 63 s, parallel sum 846 s to 810 s. The middle row shows finding 3: pool-only cuts did
not move the wall time; the serial-lane and setUp cuts did.

### Stability: the seven changed modules, three times

`test_lead_turns`, `test_inbox`, `test_tasks`, `test_projects`, `test_execution_inbox`,
`test_lead_wakes_m2`, `test_execution_acc_run2` (101 tests), run with `run_tier.py`'s
`run_single_test` (8 workers, then the serial tests one by one):

| run | result | wall |
|---|---|---|
| `origin/main` | 101 / 101 ok | 150.3 s |
| after 1 | 101 / 101 ok | 120.0 s |
| after 2 | 101 / 101 ok | 122.4 s |
| after 3 | 101 / 101 ok | 124.0 s |

## Fix round (Quest ACCEPT, Linear REVISE on 7b49faa)

Current `origin/main` (`be54a3c`, includes #17) merged into the branch first: merge commit
`e5122b2`, no textual conflicts. Still test files and these notes only.

1. **L1 no-overlap window (Linear MAJOR).** `test_l1_no_overlapping_turns` now sets
   `lead.wake_debounce_s` to 0 (existing setting, testing range 0..3600), records the
   messages the first turn carries, sends the second batch, waits until that batch is due
   (pending with `next_attempt_at <= now`, or already taken, which the next checks reject),
   and only then observes for 3.5 s: at least three scheduler ticks with the batch eligible
   while turn 1 is held by its release file. It then asserts one turn only, still `running`,
   the `leadturn:<prj>` lease held by turn 1, and the second batch still `pending`; only then
   is turn 1 released. The two-turn and no-overlap span checks after the release are
   unchanged.

   **Mutation** (applied to `mesh/daemon/lead_turns.py`, never committed, then restored):
   `should_start_turn` lost its `_lease_free` / `_active_lead_turn` early returns, the
   in-transaction `_lease_free` check in `commit_lead_turn` was removed, and the lease insert
   became `INSERT OR REPLACE`, so a second turn can start and take the lease while the first
   runs. Result: the new test fails at once, `AssertionError: True is not false : a second
   lead turn started while the first was running` (1.5 s). Restored: 3/3 OK (6.5 to 6.7 s).
   For comparison, the previous version of the test under the same mutation failed 3/3 too,
   but only on the later lease-owner check (`run_... != run_...`): the second turn started
   about 2 s after the message (the debounce), just past its 2 s window, so the window itself
   proved nothing. The new window is what fails now.
2. **H7 queued observation (Linear MINOR).** The live `task wait --until state:queued` is
   gone. After the `no_response` failure the test reads the event log: it must hold
   `task.queued` -> `task.offered` -> `task.failed` (from `queued`), with at least 1 s
   (the ack timeout) between offer and failure. Note: `test_h7_inbox_no_response`
   (`tests/integration/test_execution_inbox.py`) checks `failed.ts - offered.ts >= 1.0` from
   the `task.offered` event's timestamp, whereas the ack timeout actually starts at the
   earlier `tasks.offered_at` (`failure.details.ack_window_start`, stamped before
   `task.queued` and `task.offered` are appended). Because `offered.ts >= offered_at`,
   `failed.ts - offered.ts` is a conservative lower bound on elapsed time since `offered_at`
   (and an approximate check of the ack window: a pause between setting `offered_at` and
   recording `task.offered` could leave `failed.ts - offered.ts < 1.0` even after the full
   timeout from `offered_at` elapsed). The log keeps every transition, so a slow client
   cannot miss the transient state; `inbox.ack_timeout_s` stays 1 s. 3/3 OK alone.
3. **D11 queue observation (Linear MINOR).** The queue watcher now runs in a thread started
   before the first of the five creates (15 s bound, 0.1 s step), so it sees the whole
   queued span instead of what remains after five CLI creates. Every queued item it sees
   must have `queue_position` and `blocked_reason == "concurrency_limit"` (fx is a launch
   agent, so that is the only reason it can report). Runs stay 2 s. 3/3 OK alone.
4. **Counts.** `TestExecutionInbox` is 21 tests (5 serial, 16 parallel) and `TestInbox` 10
   (4 serial, 6 parallel); the setUp row above is corrected, with the savings (about 12.6 s
   summed, 3.0 s serial for TestExecutionInbox; 2.0 s summed, 0.8 s serial for TestInbox).
5. **Quest's measurement on the A1 target box (base):** this branch merged with main
   `fdd550d`: 485 / 278 / 49 tests, unit 34 s + integration 441 s + recovery 43 s = 518 s,
   against main 543 s there. Main `be54a3c` (after #17) is 559 s on that box.

### Full `scripts/test.sh` after the fix round (grokbot-box, idle, nothing else running)

Branch at the fix-round commit (main `be54a3c` merged; unit count is higher than Quest's
485 because #17 added unit tests). These two full-suite runs (219 s and 232 s) are on
`grokbot-box`, not `base`; the `base` number (518 s vs 543 s on `fdd550d`, and 559 s on
`be54a3c` after #17; fix-round head not re-measured on `base`) is the one that counts for
the A1 target (full suite `< 600 s` with rc 0 on `base`).

| run | unit | integration | recovery | total wall | counts |
|---|---|---|---|---|---|
| fix round 1 | 27.8 s | 167.4 s | 23.6 s | 219 s | 503 / 278 / 49, OK |
| fix round 2 | 28.7 s | 178.9 s | 24.3 s | 232 s | 503 / 278 / 49, OK |

## Target

On `grokbot-box` the integration tier was already under 380 s: 164.8 s (216 s total) before
merging `be54a3c`, and 167.4 to 178.9 s (219 to 232 s total) after the fix round above. On
`base` (which counts for the `< 600 s` full-suite A1 target: 518 s total with 441 s
integration vs 543 s on `fdd550d` and 559 s on `be54a3c`, down from an earlier 457 s
integration baseline), expect a smaller relative gain from test-only changes: the bigger
costs there are the per-test fixed overhead and the serial lane, and the large levers need
code this task may not touch.

## Proposals (not done here)

1. **Runner, `tests/run_tier.py` / `scripts/test.sh`: run the serial lane beside the pool's
   tail, or give serial tests their own lane that starts once the pool drops below N busy
   workers.** The serial lane is 63 s of wall time on an otherwise idle box. Serial tests only
   need not to compete with CPU-heavy tests, not to run after all of them.
2. **Runner: one process per test class instead of per test, for classes marked as sharing a
   daemon.** This is what makes `setUpClass` worth doing. At about 1.5 to 2.5 s of fixed cost per
   test, 254 parallel tests carry roughly 400 to 600 s of summed setUp and teardown.
3. **Product, `mesh/core/auth.py`: a cheaper scrypt cost in testing mode** (for example
   N=2^10 when `MESH_TESTING=1`, never in production). Every `user=True` CLI call pays about
   66 ms of scrypt CPU, the most common setUp call. This is a product change, so it is only
   listed here. Note: this unguarded "`N=2^10` when `MESH_TESTING=1`" form as written here
   was judged unsafe by both reviewers (Quest and Linear, because `kdf_params` are stored at
   `mesh init` and reused for the life of the home); it is now implemented as the separately
   guarded task `m3-test-kdf` (see `docs/impl-notes-m3-test-kdf.md`).
4. **Runner: start the idle-bound pool first** (it already does) and keep it at four; if more
   idle-bound tests are added, the 69 s J6 test will become the critical path on a fast box.

No merge, deploy, vendor runs, or secrets were involved.
