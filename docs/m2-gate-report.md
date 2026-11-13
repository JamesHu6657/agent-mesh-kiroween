# Milestone Gate M2 Verification Report

Gate: **M2 Git workspaces, merge, lead turns** (plan §16; checklist §0/K/L).

Verifier: **implementer self-check; gate decision by the coordinator**.

Coder of this report: Factory Droid, runs r12/r12b/r12c, `wt/m2-r12`;
docs-only r12d reconciliation in `wt/m2-r12d`
([run notes](impl-notes-m2-r12.md#r12d)).

Starting merged tree: `83f95e1`; r12 usage `505fca5`, reconciliation `40e72d0`.
r12b starts at `d88aa84`, including r11c `f670c4e` and the merged M3 chain/m3-r9.
M3 work is outside this M2 gate's implementation and acceptance scope.

## Gate decision (2026-11-10 02:31 CST): M2 PASSED on `6b6e420`

Both gate reviews ACCEPT under review routing v2:
- Linear gate review: Loop JAM-60 REVISE on `ff00f77` ([m2-gate-linear.md](coordination/reviews/m2-gate-linear.md)), free-chat re-review REVISE on `6b6e420` ([m2-gate-linear-b.md](coordination/reviews/m2-gate-linear-b.md)), round 3 **ACCEPT** on `6b6e420` ([m2-gate-linear-c.md](coordination/reviews/m2-gate-linear-c.md)).
- Kiro execution checks: **ACCEPT** on `ff00f77` ([m2-gate-kiro.md](coordination/reviews/m2-gate-kiro.md)) and **ACCEPT** on `6b6e420` ([m2-gate-kiro-b.md](coordination/reviews/m2-gate-kiro-b.md)): A1 554 s and 560 s, 679 tests (unit 357, integration 273, recovery 49), rc 0, no other suite running when sampled before, between and after the runs; A3 tripwires absent.

The sections below keep the pre-decision record; where their numbers (556/555 s, 678 tests, +37) differ, the `6b6e420` figures above (+38: the M3 chain plus the m3-r9c K4 unit test) are the gated ones. Full renumbering is folded into the M3 docs.

## 1. Outcome

**A1 PASS; final M2 gate acceptance awaits Linear re-review.**
[Kiro execution checks](coordination/reviews/m2-gate-kiro.md) ACCEPT: two
back-to-back idle suites on `mesh-fresh-v1` took **556 s** and **555 s**,
each **678 tests, rc 0** (unit **356**, integration **273**, recovery **49**),
with no other suite running and both tripwire logs absent. Both satisfy the
unchanged strict **<600 s** limit. Run 1 was on `ff00f77`; run 2 included
docs-only `dbcaac4`, with unchanged code/tests (code equals `bd0929e`).
The **+37 tests** since r12b come from the subsequently merged M3 chain,
outside M2 implementation/acceptance scope, per the same execution record.

Historical contended results remain: r12 **564 tests / 694 s**, r12b
**641 tests / 845.995 s** (runner **846 s**), both rc 0 but not A1 passes
([r12/r12b notes](impl-notes-m2-r12.md)). Contention is not a waiver.
No test infrastructure or speed changes were made in r12/r12b.
[Linear gate review](coordination/reviews/m2-gate-linear.md) is **REVISE on
`ff00f77`**. Its L2 async-merge test race is fixed by m3-r9c and its missing
r12b/r12c review record is corrected below; re-review on the fixed tree is pending.

All launches are fake under testing mode; inbox agents use none. No real
vendor inference/login, network, TCP listener, push or identity/config change.
Kiro readonly argv construction is tested without launching Kiro.
The authoritative requirements, plan, checklist, old notes and real fixtures
were not edited. **N1 is not changed in M2.**

## 2. Commands, counts and regression evidence

Historical r12/r12b/r12c commands ran from
`/home/user/workspace/mesh-wt-m2-r12` ([run notes](impl-notes-m2-r12.md)).
Kiro's gate commands ran from `/home/user/workspace/mesh-fresh-v1`
([execution record](coordination/reviews/m2-gate-kiro.md)).

| Check | Command | Exit/result |
|---|---|---|
| Baseline, before first edit | `bash scripts/test.sh` (900 s tool limit) | TIMEOUT, no exit code; unit 283/95.605 s OK; integration incomplete, recovery not reached; 901.5 s whole observation |
| Existing docs check | `python3 -m unittest tests.unit.test_docs_m19 -v` | rc 0; 6 tests, 0.896 s |
| Final docs-only recheck | `python3 -m unittest tests.unit.test_docs_m19 -v` | rc 0; 6 tests, 0.250 s |
| Focused reconciliation | `python3 -m unittest tests.unit.test_docs_m19 tests.unit.test_lead_prompt tests.integration.test_merge tests.integration.test_merge_tests tests.integration.test_deliver_prune tests.integration.test_lead_turns.TestLeadTurnBackoff -v` | rc 0; 51 tests, 137.078 s |
| Historical r12 final suite | `bash scripts/test.sh` (2400 s tool limit) | **rc 0; 564 tests; 694 s runner wall time**; 695.1 s whole observation |
| r12b new regressions, unchanged runtime | `python3 -m unittest tests.integration.test_rework_feedback_retry tests.integration.test_lead_wakes_m2 -v` | rc 1; four new methods fail (retry has both exit/timeout subtests); completion fixture corrected and rerun separately, rc 1 on own-lease completion |
| r12b focused, new and existing checks | `python3 -m unittest tests.integration.test_rework_feedback_retry tests.integration.test_lead_wakes_m2 tests.integration.test_lead_actions tests.integration.test_resume tests.unit.test_events tests.unit.test_projects -v` | rc 0; 23 tests, 130.566 s |
| r12b full merged-tree suite, after final runtime/test edit | `bash scripts/test.sh` (1600 s tool limit) | **rc 0; 641 tests; 845.995 s measured wall; 846 s runner wall** |
| r12b final docs-only recheck | `python3 -m unittest tests.unit.test_docs_m19 -v` | rc 0; 6 existing tests, 0.667 s |
| Historical r12c suite, after final test changes | `bash scripts/test.sh` | rc 0; 641 tests; 865.520 s measured wall, 866 s runner wall; contended, not A1 evidence ([notes](impl-notes-m2-r12.md#r12c)) |
| Kiro idle gate suite, run 1 | `bash scripts/test.sh` | **rc 0; 678 tests; 556 s runner wall**; unit 356 / 33.431 s, integration 273 / 476.182 s, recovery 49 / 46.125 s |
| Kiro idle gate suite, run 2 | `bash scripts/test.sh` | **rc 0; 678 tests; 555 s runner wall**; unit 356 / 32.408 s, integration 273 / 477.383 s, recovery 49 / 44.639 s |
| M2 stubs | `grep -rn '"M2"' mesh/` | rc 1, no matches |
| Fixture branch hygiene | `git branch --list 'mesh/*'` | rc 0, empty |
| Whitespace | `git diff --check` | rc 0 |

Historical r12 tiers: unit **283 / 28.012 s**, integration **253 / 570.163 s**,
recovery **28 / 95.624 s**, all OK. Counts include existing M1 regression tests.
No fixture-hash failure. M1's accepted historical exceptions remain historical,
not the evidence for the current A1 pass. B11 is superseded by K1/K9; its retained method
documents that and keeps invalid-repo/unknown-task probes. Submitted-task rc 6
is exercised by K9. No existing assertion or ACC tag was removed/weakened.
The original r12 additions strengthened existing tests, **zero new test methods**:
K11 pre-intake/all-repo remote snapshots; L3 elapsed backoff; L6 readonly
argv build; six J7 launch-row assertions. r12b adds four methods in
`integration/test_lead_wakes_m2.py` and `integration/test_rework_feedback_retry.py`;
all failed before runtime edits, and no existing assertions changed.
Details: [r12/r12b notes](impl-notes-m2-r12.md).

r12b tiers: unit **346 / 30.377 s**, integration **262 / 741.534 s**,
recovery **33 / 73.596 s**, all OK. Of the 641 checks, **4 are new r12b
regression methods and 637 already existed** in the merged starting tree
(including M3, outside M2 scope). Full suite ran before the runtime/test commit.

Current gate-level counts are **356 + 273 + 49 = 678** in each Kiro run;
the **37** additional checks over r12b's 641 are from the M3 chain merged
after r12b, not new M2 tests
([execution record](coordination/reviews/m2-gate-kiro.md)).

The historical r12 runner's end checks passed. Both exact tripwire paths were also checked:
`/tmp/mesh-tripwire-suite-3396540402.log` and `/tmp/mesh-tripwire-hit.log`,
both **absent** (suite key is cksum of checkout path without newline).
Invariant checks run in harness teardown, with explicit running/grace/cancelling
and lowered-concurrency regression scenarios plus I9 positive/negative tests.
A6 tests retain the actual `test_git_never_spawned_outside_gitops` spawn scan
and strict allow-list test. No Mesh checkout was a fixture repo.

### Concurrent suites

`pgrep -fa run_tier.py` ran before and after, through an allowed `python3 -c`
subprocess; `/proc/<pid>/cwd` identified other worktrees. Check-command matches
are excluded. No other process/worktree was stopped or changed.

| Observation | Other suite(s) |
|---|---|
| Before baseline | PID 33622 integration, `mesh-wt-m3-r5` |
| After baseline timeout | PID 33622 as above; PID 39054 integration, `mesh-fresh-v1` |
| Before final | PID 26081 recovery, `mesh-fresh-v1` |
| After final | PID 48876 integration, `mesh-wt-m3-r5` |
| Before r12b full suite | No other suite observed (excluding the observation command itself) |
| After r12b full suite | PID 7172 integration, `mesh-wt-m3-r9b` |
| Kiro before run 1, between runs and after run 2 | No other suite running; exact `tests/run_tier` and `unittest` process checks empty ([execution record](coordination/reviews/m2-gate-kiro.md)) |

The historical r12/r12b endpoint observations establish contention, not continuous
sampling or the amount of slowdown. They do not establish an idle-host A1 result.
r12b independently checked `/tmp/mesh-tripwire-suite-3396540402.log` and
`/tmp/mesh-tripwire-hit.log`: both absent; runner end checks passed too.

Kiro's independent back-to-back gate runs establish the later idle A1 pass.
The broad `run_tier` match found only the reviewer's prompt-bearing processes,
not test runners; exact suite checks were empty. After each `mesh-fresh-v1`
run the default `/tmp/mesh-tripwire-hit.log` and checkout's suite-keyed log
were absent (no `/tmp/mesh-tripwire*` files); both runner end checks passed.
See [Kiro's execution record](coordination/reviews/m2-gate-kiro.md), not the
historical r12 checkout key, for current A3 evidence.

### Post-r12 evidence and review record

- **r11c, M2/L5:** `f670c4e` hardens queued lead-merge recovery. Existing
  `recovery/test_recovery_lead_merge.py` now checks the final complete legacy
  block and all stored task aliases, identifiable versus unrelated corrupt JSON,
  durable skip markers, exact NULL-request source matching, detached missing-run
  failures, once-per-boot behavior and the real committed close/action path.
  Evidence includes `::test_legacy_final_block_never_falls_back_to_an_earlier_task`,
  `::test_legacy_stdout_task_must_agree_with_every_stored_task`,
  `::test_unidentifiable_invalid_json_is_skipped_and_logged_once`,
  `::test_null_request_failures_match_only_the_source_event`,
  `::test_missing_run_records_detached_terminal_failure_once`,
  `::test_committed_queue_survives_connection_and_memory_loss` and
  `::test_malformed_payload_missing_index_and_truncated_stdout_fail_once`;
  `unit/test_lead_actions.py::test_recovery_parser_requires_the_final_complete_actions_block`.
  These remain direct recovery-boundary tests, not a SIGKILL-at-queue-boundary claim.
- **r12b, M2:** the four new runtime scenarios below close feedback retention,
  verified own-turn completion, approval wakes and async merge-success wakes.
  Existing `merge.failed` fan-out already wakes launch leads and is retained.
- **M3 chain and m3-r9:** present in this merged checkout, but outside M2 scope.
  Their vendor/evidence/fixture changes are not attributed to M2 or re-accepted here.
- **Linear Agent:** historical REVISE reviews
  [r11](coordination/reviews/linear-m2-r11.md) and
  [r12](coordination/reviews/linear-m2-r12.md). r11 findings informed r11b/r11c;
  r12 findings were addressed in r12b (`5e715c3`, `e1b6d36`).
  [r12b re-review](coordination/reviews/linear-m2-r12b.md) was **REVISE**,
  with no MAJOR, two MINORs and one NIT. r12c (`d2be66a`, `1b52d17`)
  corrected coalesced debounce timing, K4 citations and distinct-summary
  assertions; [r12c re-review](coordination/reviews/linear-m2-r12c.md)
  was **ACCEPT**, merged as `cb278d2`
  ([run notes](impl-notes-m2-r12.md#r12c),
  [coordination log](coordination/log.md), [r12d scope](../prompts/droid-m2-r12d.md)).
  Per-run ACCEPT does not imply full M2 gate or A1 acceptance.
- **Linear Loop gate:** [REVISE on `ff00f77`](coordination/reviews/m2-gate-linear.md).
  MAJOR-1 was the L2 async-merge regression's fixed winner-order assumption,
  not a demonstrated runtime safety defect. m3-r9c `e7021a8`, merged `b8d102c`
  ([r12d scope](../prompts/droid-m2-r12d.md)), fixes
  `tests/integration/test_lead_actions.py::TestLeadMergeAction.test_merge_action_queued_then_merged_and_other_project_refused`:
  identify the sole winner via `task.merging.request_id` and correlate the other
  request's index/request ID/refusal/failure notification, retaining the existing
  safety assertions. Author stress: **1/20 → 0/20** failed repetitions
  ([m3-r9c notes](impl-notes-m3-r9.md)).
  [Linear m3-r9c ACCEPT](coordination/reviews/linear-m3-r9c.md) independently
  observed **0/20** failures on the fixed test; its different baseline stress
  is not confirmation of the author's 1/20 frequency. Gate re-review remains pending.
  MINOR-1's missing r12b/r12c record is corrected here and in the notes index.
- **Quest reviewer:** historical per-run reviews include
  [r11b ACCEPT](coordination/reviews/m2-r11b.md) and
  [r11c ACCEPT](coordination/reviews/m2-r11c.md); these are not a final M2 gate review.
- **Kiro:** historical reviews include
  [r4b/r5](coordination/reviews/m2-r4b-r5.md),
  [r5b/r6](coordination/reviews/m2-r5b-r6.md) and
  [r10](coordination/reviews/m2-r10.md), with findings handled in later fix rounds.
  Routing v2 (James, 2026-11-09 21:51) assigns the gate review to Linear Loop
  and A1 timing/A3 tripwires/merged-tree execution checks to Kiro.
  [Kiro execution checks](coordination/reviews/m2-gate-kiro.md) are now **ACCEPT**,
  with the A1/A3/678-test evidence above.

## 3. Verification table

Test paths below are relative to `tests/`; `::method` identifies the actual
unittest method (class omitted for readability). Commands show the real CLI
entry point; `…`, REPO, PRJ and TSK denote scenario flags/fixture IDs, not
standalone copy/paste recipes. Full setup/auth/negative cases live in the named
tests. Every K/L/A ID has an existing literal `ACC: <ID>` test tag.
PASS means the **combined** listed checks, not that each named method alone
covers the whole item. Q2/core collision and direct timeout/build exceptions
are explicit below and in D05/D21/D38.

| ID | Status | Author (actual coder) | CLI / mechanism | Passing test names and complete scenario coverage |
|---|---|---|---|---|
| K1 | PASS | Quest coder | `mesh intake submit … --repo REPO --base-branch main --test-command none`; `project show PRJ` | `integration/test_repo_projects.py::test_repo_intake_integration_and_lead_worktrees`, `::test_bad_repo_and_base_branch`; `unit/test_repos.py::test_existing_branch_rc6` (Q2 core collision). Valid integration branch/worktree/base, invalid repo/base, rc 6 collision. |
| K2 | PASS | Quest coder | Same intake with argv / `none` / missing test command | `integration/test_repo_projects.py::test_repo_intake_integration_and_lead_worktrees`; `unit/test_repos.py::test_parse_test_command`. Missing/invalid rc 2, argv and exact none storage/show. |
| K3 | PASS | Quest coder; Droid GPT-6.1 Sol | `mesh task create … --kind code`; `task start TSK` | `integration/test_code_tasks.py::test_k3_two_code_tasks_each_in_own_worktree_and_branch`, `::test_fresh_branch_after_lease_expiry_keeps_old_worktree`, `::test_rework_reuses_worktree_and_branch`, `::test_workspace_error_inbox_and_launch`, `::test_code_task_in_prep_holds_its_slot`. Separate/fresh/reused branches/worktrees, version/fence and safe failures. |
| K4 | PASS | Quest coder; Droid GPT-6.1 Sol | `mesh task submit TSK …`; `task merge TSK`; reject/rework/accept/retry | `integration/test_code_tasks.py::test_k4_inbox_code_task_dirty_deliverable`; `integration/test_merge.py::test_k4_dirty_worktree_then_t17_retry`; `integration/test_merge_refs.py::test_merge_ignores_tag_named_like_task_branch` (lines 26–41), `::test_base_commit_ignores_tag_named_like_base` (lines 50–63). Git metadata, dirty refusal, unchanged integration then repaired retry; tag shadowing cannot replace task/base branch refs. |
| K5 | PASS | Quest coder | `mesh task merge TSK` with test argv | `integration/test_merge_tests.py::test_k5_tests_pass_then_merged`; `integration/test_merge.py::test_clean_merge_records_merge_commit` (lines 175–179 explicitly assert both parents and message). Test log/result, merge run, two-parent commit/message, task/event/lease state. |
| K6 | PASS | Quest coder | `mesh task merge TSK` with conflicting edits | `integration/test_merge.py::test_k6_conflict_restores_integration`. rc 14 conflict, failed task/notification, clean original integration head and no lease. |
| K7 | PASS | Quest coder | `mesh task merge TSK` with failing tests; direct test-step timeout | `integration/test_merge_tests.py::test_k7_failing_tests_abort_the_merge`, `::test_timeout_terminates_group_and_escapee`. Failure rollback/log tail, no commit; timeout kills descendants/escapee. No CLI timeout flag (D21). |
| K8 | PASS | Quest coder; Droid GPT-6.1 Sol | `mesh task merge TSK` in no-identity fixture | `integration/test_merge.py::test_k8_no_identity_commit_failed`. rc 14 commit_failed even at merge preparation, no committed merge/config identity, rollback/lease/remote checks. |
| K9 | PASS | Quest coder; Droid GPT-6.1 Sol | `mesh task merge TSK` submitted / other lead; direct busy-lease guard | `integration/test_merge.py::test_k9_guards`, `::test_general_task_and_test_command_guards`; `unit/test_merge.py::test_merge_lease_held_is_rc11` (lines 120–132, direct core guard with mocked authz boundary). CLI rc 6 invalid transition/rc 4 not lead; core rc 11 busy merge lease with no mutation; general-kind/unknown-task guards. |
| K10 | PASS | vibe; Quest coder | `mesh project deliver PRJ` | `integration/test_deliver_prune.py::test_k10_deliver_fast_forwards_then_refuses_non_ff`, `::test_k10_deliver_through_a_worktree_holding_the_base`, `::test_k10_tag_shadowing_the_integration_branch`. Fast-forward, non-FF refusal, both base paths, no integration change/push. |
| K11 | PASS | vibe; Quest coder; Droid GPT-6.1 Sol | intake / submit / merge / deliver / prune/delete | `integration/test_deliver_prune.py::test_k11_no_push_across_intake_merge_deliver_prune`; remote assertions in merge/deliver fixtures and code-task scenarios. r12 snapshots before intake and compares every created repo, including no-identity and multi-project cases. |
| K12 | PASS | vibe; Droid GPT-6.1 Sol | `mesh workspace prune --project PRJ`; `workspace delete TSK` | `integration/test_deliver_prune.py::test_k12_prune_and_delete`, `::test_k12_delete_refuses_a_task_with_a_lease`, `::test_k12_delete_refuses_a_path_outside_the_workspaces_root`, `::test_k12_unscoped_prune_is_refused_for_agents`. Eligibility, dry run, branches/dirs, approval, retention, lease/containment/scope. |
| K13 | PASS | vibe; Quest coder | Repo intake and fake lead turn after the integration tip is updated | `integration/test_lead_turns.py::test_k13_lead_repo_detached_during_turn`; repo intake scenario. Separate detached lead repo, current integration at next turn, task workspace remains separate. |
| K14 | PASS | Quest coder; Droid GPT-6.1 Sol | Merge, kill meshd, `mesh daemon start`; repeat boot | `recovery/test_recovery_merge.py::test_k14_kill9_during_test_step_interrupted`, `::test_k14_crash_after_merge_commit_reconciled`; `unit/test_merge.py`, `unit/test_merge_recovery_minors.py`. Terminate interrupted tests/abort, reconcile matching committed merge, repeat-safe recovery; P5 and isolated-block failure checks. |
| L1 | PASS | vibe; Droid claude-sonnet-5-5; Droid GPT-6.1 Sol; Droid (r12b) | `mesh intake submit … --lead fx`; intake/approval/merge wakes | `integration/test_lead_turns.py::test_l1_one_turn_per_debounce_window`, `::test_l1_no_overlapping_turns`, `::test_l1_lead_token_confined_to_its_project`, `::test_l1_leftover_after_exit_zero_acks`; `integration/test_lead_wakes_m2.py::test_approval_decision_alone_wakes_lead_and_retries_action`, `::test_async_merge_success_wakes_lead_to_deliver_and_complete`. Debounce/coalescing ≥2 s from the oldest carried message ([r12c review](coordination/reviews/linear-m2-r12c.md)), lease/no overlap, scope/credential lifetime, leftovers/ack. |
| L2 | PASS (fixed; gate re-review pending) | vibe; Droid claude-opus-5-5; Droid GPT-6.1 Sol; Droid (r12b) | Fake stdout mesh-actions → internal real handlers; CLI intake/inbox/approval | `integration/test_lead_actions.py::test_l2_actions_executed_as_lead_and_failures_reach_next_turn`, `::test_approval_gated_action_needs_a_matching_approval`, `::test_merge_action_queued_then_merged_and_other_project_refused`; `unit/test_lead_actions.py`; `integration/test_lead_wakes_m2.py::test_complete_ignores_only_verified_own_turn_lease`, `::test_approval_decision_alone_wakes_lead_and_retries_action`, `::test_async_merge_success_wakes_lead_to_deliver_and_complete`. Action/authz/approval/hash/scope and durable recovery (D38); verified own-turn completion, foreign task/merge/leadturn leases still rc 6; approval alone retries; merge success alone starts deliver/complete. Gate MAJOR-1's async-merge test race is fixed by m3-r9c `e7021a8` (merged `b8d102c`): winner/refusal correlated by `request_id`, not preflight order; stress 1/20 → 0/20 ([notes](impl-notes-m3-r9.md), [Linear ACCEPT](coordination/reviews/linear-m3-r9c.md)). |
| L3 | PASS | vibe; Droid GPT-6.1 Sol | Fake failing turns; testing backoff configuration | `integration/test_lead_turns.py::test_l3_three_attempts_then_dead`, `::test_l3_second_episode_reaches_the_user_again`; `unit/test_settings.py`. Three attempts, measured ≥1 s adjacent backoffs, dead rows/one unreachable per episode, production read-path policy. |
| L4 | PASS | Droid claude-opus-5-5 | `mesh project set-lead PRJ … --user` during a turn | `integration/test_lead_actions.py::test_l4_set_lead_terminates_turn_and_supersedes_token`; `unit/test_lead_supersede.py`. Immediate termination, supersede/recreate delivering messages, old token rejected, durable reason backstop. |
| L5 | PASS | Droid GPT-6.1 Sol (including r11c); Droid (r12b) | Fake executor reject/retry and repeated lead turns; direct recovery boundary | `integration/test_resume.py::test_l5_executor_rework_retry_and_lead_turns`; `unit/test_adapters.py`; `recovery/test_recovery_lead_merge.py::test_l5_all_close_paths_capture_sessions_without_erasing_last_id` and r11c evidence listed above; `integration/test_rework_feedback_retry.py::test_rework_failure_retry_keeps_latest_feedback`. Verified-only resume/fresh paths, captured IDs, options/configured binary, ordinary/killed/lost closes; latest rejection and previous summary in the feedback section after nonzero-exit/timeout rework→retry; hardened recovery provenance/isolation. |
| L6 | PASS | vibe; Droid GPT-6.1 Sol | Capped prompt → Kiro readonly build (no launch); fake real turn | `unit/test_lead_prompt.py::test_lead_prompt_cap` and multibyte/fixed-section properties; `integration/test_lead_turns.py::test_l6_overflow_messages_stay_pending`. 50 messages/200 events/100 notes/300 tasks, complete fixed contract/byte cap/omitted counts, readonly argv; only included messages delivered/acked. |
| A1 | **PASS** | Quest coder (runner); Kiro (gate measurement) | `bash scripts/test.sh`, two back-to-back idle runs | **556 s and 555 s**, each **678 tests, rc 0**, no other suite running; unchanged strict **<600 s** limit ([Kiro execution checks](coordination/reviews/m2-gate-kiro.md)). Historical contended 694 s / 846 s runs remain failures, not waivers. Structural skeleton test is not timing evidence. |
| A3 | PASS | Quest coder; Droid GPT-6.1 Sol; Kiro (gate verification) | Full offline suite and final tripwire checks | Both `mesh-fresh-v1` gate runs: default `/tmp/mesh-tripwire-hit.log` and checkout suite-keyed log absent (no `/tmp/mesh-tripwire*` files); rc 0 and runner end checks passed ([Kiro execution checks](coordination/reviews/m2-gate-kiro.md)). Inherited harness tripwire checks retained. |
| A6 | PASS | vibe; Quest coder | Product git wrapper/allow-list and source scan | `unit/test_gitops.py::test_gitops_allowlist`, `::test_git_never_spawned_outside_gitops`, `::test_run_git_ignores_inherited_repo_location_env`. Includes exact safe branch-delete addition. |
| A8 | PASS | vibe; Quest coder; Droid GPT-6.1 Sol | `mesh doctor --check-invariants --user` in harness | `unit/test_invariants.py::TestInvariantI9`; `integration/test_execution_inbox.py::test_a8_doctor_check_invariants_while_running`; `test_execution_termination.py::test_h16_cancel_during_post_submit_grace`, `::test_f8_accept_blocked_until_post_submit_grace_kill`; `test_execution_acc_run2.py::test_d15_lowered_limit_no_kill`; teardown. I9 full commits/ancestry, existing running/grace/cancelling states. |

NOT-COVERED K/L items: **none**, with documented test-level exceptions above.
No unmet numeric gate remains in this table. Final independent gate acceptance
is not inferred from PASS rows: Linear re-review on the fixed tree is pending.

## 4. Per-author implementation ranges

Ranges are inclusive **within the named runs**, not an assertion that every
intervening merged commit has that author. Cross-check notes and read-only
coordination mapping, not only `%an`: several Droid commits misleadingly say
`Kiro CLI coordinator (claude-opus-5.5 high)`.

| Actual coder | Run(s) | Commit range / commits |
|---|---|---|
| vibe DeepSeek-v4.1-flash | r1/r1b/c | `6e1f37d` … `94fce31` |
| vibe DeepSeek-v4.1-flash | r2–r2f | `7c3a77e` … `30f6917` |
| vibe DeepSeek-v4.1-flash | r8/r8b | `1b6f467` … `11610f3` |
| vibe DeepSeek-v4.1-flash | r9/r9b | `156286e` … `7d0f9b4` |
| Quest coder | r3/r3b | `f8fbe85` … `597b593` |
| Quest coder | r4/r4b | `8add1f2` … `0e0fa98` |
| Quest coder | r5/r5b | `195219d` … `166be53` (also `59869f2`, `9f417c0`) |
| Quest coder | r6 | `1f46429` … `9f895c0` |
| Quest coder | r7/r7b | `438713a`, `11e5bcf`, `9f3cd41`, `943b91c`, `b319d5b`, `bdb5b96`, `d43b53f`, `0eef3d5` |
| Droid claude-sonnet-5-5 | r9c | `bd59b02` |
| Droid GPT-6.1 Sol | r9d; r7c | `ebc2355`; `0a3ef3d` |
| Droid claude-opus-5-5 | r10 | `e48c2fc`, `f0bd25c`, `3973bf1` |
| Droid claude-opus-5-5 | r10b | `d2a7ead`, `b9699f2` (wrong default metadata) |
| Droid GPT-6.1 Sol | r11/r11b | `a02bf5c` … `d3a55ae` (includes `093592c`, `ff5f737`, `8a9d4d4`; wrong default metadata) |
| Droid GPT-6.1 Sol | r11c | `f670c4e` (recovery hardening, including L5 tests) |
| Droid | r12 | `505fca5`, `40e72d0`, `5217ec8` (wrong default metadata) |
| Droid | r12b | `5e715c3` runtime/tests/usage on `d88aa84`; `e1b6d36` documentation ([notes](impl-notes-m2-r12.md), [review](coordination/reviews/linear-m2-r12b.md)) |
| Droid GPT-6.1 Sol | r12c | `d2be66a` tests; `1b52d17` documentation ([notes](impl-notes-m2-r12.md#r12c), [coordination log](coordination/log.md)) |

No agy-coded M2 range is inferred from the requested commit-author string.

### Cross-cutting items

- **A1:** historical Quest runner changes plus Kiro's merged-tree idle
  measurements, 556 s / 555 s: PASS
  ([execution checks](coordination/reviews/m2-gate-kiro.md)).
- **A3:** all authors' launched code and tests share tripwire/harness guarantees.
- **Merged-tree integration:** historical r12 ran 564 tests against r1–r11
  plus r12 docs/assertions, including M1 regression and I9. r12b passes 641
  tests on the merged tree including r11c and M3. Kiro's gate runs each pass
  678 tests; the +37 are later M3-chain additions, outside M2 scope
  ([execution checks](coordination/reviews/m2-gate-kiro.md)).
- **Spanning authors:** K3/K4/K8/K9/K10/K11/K13/K14 and L1/L2/L3/L5/L6;
  implementation, parser, supervisor, approval, recovery and prompt evidence
  must be read together. A6/A8 likewise span gitops/workspace/invariant owners.

## 5. Consolidated rulings and deviations

The D identifiers match the expanded list in [implementation index](impl-notes-m2.md).
That index links every earlier run note and appended review round. The
following is the consolidated reading used for this gate, including supersessions.

| Record | Ruling / deviation and source |
|---|---|
| D01 | Inherited accepted M1 scaffold/A9 exception is not reopened. N1 unchanged in M2; its later N23 supersession belongs to M3. |
| D02 | Q15 actual coder comes from run notes/coordination when Git defaults are wrong. Runtime prohibits requested agy author override; r11/r12 use configured identity plus Factory co-author. |
| D03 | Q1: report host-dependent times without discount. Historical 636/852 s “under ten minutes” claims are wrong. Baseline timeout is not green; historical r12 final 694 s fails strict <600. No r12 speed changes. Later idle 556 s / 555 s pass ([Kiro checks](coordination/reviews/m2-gate-kiro.md)). |
| D04 | r1: K0 ignores `__pycache__/`; fixture drops inherited GIT env and permits its harness home. Product strips repo-location env, never sets identity/config. I9 requires full SHA-1/SHA-256 commit and integration ancestry; git errors differ from non-ancestor. |
| D05 | Q2/r3: generated branch collision tested at core (rc 6); bad repo/base through real CLI. No fixture Git points at the Mesh checkout. |
| D06 | Q3/r3b supersedes original git-first reading: setup last after DB inserts/event before commit; rows roll back on failure, successful branch remains on worktree failure/timeout and is named in git_failed/git_timeout details. |
| D07 | r3: absolute top-level repo, local base branch (not SHA/tag/HEAD); invalid flag combinations/follow-up repo flags rc 2. Unknown dependency retains rc 1; non-repo output unchanged. Test argv / exact input literal none stored as JSON. |
| D08 | r3: synchronous registered intake git in request transaction, possible 30 s timeout/hook blocking; no pre-transaction async phase. |
| D09 | r3: lead detached repo post-commit/best effort/logged/recreated on set-lead. Dry run creates nothing. Install-root wiring is core/mocked, never CLI against the checkout. |
| D10 | Q13/r4: code requires repo; general unchanged. Exact integration SHA start point; fresh expired-lease worktree/branch preserves original base_commit. |
| D11 | r4: required server hook outside initial file list; off-loop prep, reservations/per-task lock, 10 s result TTL, inbox auth/delivery/project/status recheck, up to one extra launch tick. |
| D12 | r4: auth/fence before git metadata; failures nonblocking; Mesh overrides supplied git key. All untracked files included; dirty paths capped 200/diff stat 16 KiB/redacted. |
| D13 | Q4/r5–6: merge lease busy rc 11; disconnect-shielded pipeline. Client hardcoded 1800+120 s since project timeout has no setter/CLI flag. |
| D14 | Q5/r5: merge_test run even without tests/process, meshd-owned lease, merges/<mrg> run dir; no run token to test process. |
| D15 | r5b/r7b: full refs/resolved SHA avoid tag shadowing; authoritative pre-head read while lease held supersedes initial probe. |
| D16 | r5b/r6: noop via ancestry, not localized stdout; no new commit, test step skipped, tests:none. |
| D17 | r5/b: no identity may fail merge preparation; marker-based commit_failed (rc 14). Unexpected Git error/timeout becomes git_failed (rc 1), with abort/verify where needed; not every failure is conflict. |
| D18 | r5b/r6: rc 0 and rc 14 idempotency replies stored atomically in final TX; conflict-safe INSERT OR IGNORE. Original rc-0-only note superseded. |
| D19 | r5b: task that left merging still closes run/releases lease; new diagnostic/end_reason task_left_merging, rc 1. Merged runs use merged; failure runs use T16 reason. |
| D20 | r6: test env has MESH_RUN_ID only, no token; restore PYTHONPATH. Registered combined/redacted 0600 logs; last 50 lines capped 1 KiB each/16 KiB total. |
| D21 | r6: direct 1 s timeout/escapee test because no CLI timeout flag. Timeout/leftovers run status killed, end_reason timeout/leftovers, not ordinary exited failure. |
| D22 | Q6/r7/b: merge_test never adopted, terminate/interrupted. Current Git evidence/first parent beats stale saved head; missing head null when unverifiable, nonmatching commits conservative. Noop crash retries; state re-read makes repeated recovery event/message-safe. |
| D23 | r7: testing-only after_merge_commit hook. Reconciled tests passed iff configured, otherwise none; retain test_log when present. |
| D24 | P5/r7: project cancel refused rc 6 INVALID_TRANSITION reason merge_in_progress before approval, with task/run IDs and no mutation. |
| D25 | r7/b/c: recorded §11.4 deviation, merge test kill_failed releases lease and reports surviving PIDs (run.kill_failed live/recovery), rather than holding/retrying. Different open-run step-2 error retains task/lease for later boot, durable redacted per-task/reason merge.recovery_blocked, isolated/logging-safe. |
| D26 | Q7/r8: contained rmtree + metadata prune for explicit delete, branch kept, never -D. Safe prune exact branch -d exception to create-only allowance; dirty/unsafe/leased/committed-unmerged skipped, already-pruned omitted. No workspace => deleted:false + event. Dry run only planned transitions. |
| D27 | r8b supersedes permissive unscoped-prune reading: current lead/user, only user unscoped; no new capability. Deliver completed allowed/cancelled refused/noop no event. Both ff-only checked-out-base and CAS update-ref paths tested with full refs/base revalidation; never push. |
| D28 | Q8/r9–10: lead.action_failed included but cannot trigger alone, preventing failure loops. |
| D29 | Q9/r9b: testing-only 3-int backoff 0..600; production read path fixed 30/120/600. Default 3 attempts consume only first 2 delays. |
| D30 | r9/b/c: unreachable dedup per episode/run, second episode can notify. Spawn failure backs off; release lease by run ID; ack retains delivering_run_id; lead.turn_started audit event. Exit-zero leftovers take lead finish/ack path. |
| D31 | Q11/r9–10: goal on its own line for FAKE. Vendor launch disabled, rc 13/lead.turn_refused once per assignment until M3, inbox advice retained. Dirty lead repo retained/logged; refresh off-loop/best effort. |
| D32 | r9/b/d: lead token current-project lists/ID reads, no new-project intake; own follow-up allowed. Other-project and global/projectless approvals denied even with relay/requester/both-project leadership. Executor authorization unchanged. |
| D33 | r9b: lead run-token inbox excludes lead_wake; pointer cannot acknowledge pending turn rows. |
| D34 | Q10/r10: dispatch before revoke/close with handler via=run_token to preserve real authorization, action result via=lead_turn. Request IDs correlate results/events. User-only config/agent/audit rejection before supersede check. |
| D35 | r10b: optional action-level approval string (empty => none) bound to normal ctx/hash/requester/project/channel. Missing approval rc 7, not erroneous task-text 10. approval.request auto-own-project only for action; direct projectless request denied. |
| D36 | r10b supersedes not_internal merge refusal: read-only guards in rolled-back savepoint, earlier actions visible; queue answer not merge outcome/idempotency. Post-commit async pipeline preserves principal/request ID. Duplicate/start failure emits phase merge_start. Initial other queued effects only logged (no current non-merge handlers need them). |
| D37 | r11 supersedes r10b queue-loss limitation: durable queued action events requeue once per boot, request-ID de-dup, skip task.merging/merge_start, legacy stdout fallback, preserve normalized/original args/principal; TX1 current guards still apply. |
| D38 | r11b: per-row RECOVERY_FAILED terminal results, continue other rows; finally marks boot, supervisor/logging failure isolation. Coordinator approved direct unit-style connection/queue-loss boundary tests, not an actual SIGKILL-at-this-boundary scenario. |
| D39 | r10/b L4: supersede/recreate all old-lead delivering rows; terminate post-commit, durable assignment mismatch after restart; new end_reason lead_superseded. Test grace increased 5→10 s, assertions retained. |
| D40 | r2/r2b/c/r10b parser: last valid fenced JSON array, LF/CRLF only, matching backtick run; >50 rejects whole block, per-item errors independent, missing args {}, no valid block => no actions. Optional approval preserved. |
| D41 | r2/e: whole UTF-8 prompt cap including immutable rules/schema/actions, no executable example mesh-actions fence. Deep-copy before trimming; fixed structure remains. |
| D42 | r2d/e: strings 60/list 10, authority categories 600-byte and models 1500-byte budgets. Minimum raised to 16384 because fixed multibyte skeleton cannot fit 4096; no production caller below 100000. |
| D43 | r2/b/c: oldest events/notes, terminal tasks all-at-once with per-status counts, title/summary truncation, newest messages/tasks/approvals dropped to keep oldest; quarter batching, exact counters, messages_cut distinct from drops. |
| D44 | r2/e: optional goal/criteria text may truncate/drop; immutable authority removed from variable guard and fixed headings retained. |
| D45 | r2f: measure envelope/remaining byte room, monotonic recut max 8, UTF-8 tiny-body check; pathological only message may drop/stay pending rather than always keep one, clear cut counter. Category “N more” exact even outside byte-budget path. |
| D46 | Q14/r11: always complete prompts; resume only supported=yes + smoke + ID. Capture last stdout JSON independently of exit/resume, including killed/lost; malformed/no ID keeps last, reclose cannot overwrite. Stored options plus authoritative binary/run ID; vendor resume_verified cannot enable capability. Fake-only evidence. |
| D47 | r11 coordinator-approved golden change: resumed fake cases in all 5 profiles expect --fake-resume sess_1; fresh/no-ID/matrix/cwd/stdin/forbidden checks unchanged. No real fixture/hash edits; installed agy 1.5.3 vs 1.5.2 remains stale. |
| D48 | Historical process deviations: r10 shared-file substeps 1+2 combined; r2d/e untracked prompt files left uncommitted. Loaded-host H4 intermediate flake passed isolated/final, no assertion removed. |
| D49 | r7 coordinator A1 speed pass outside initial file scope: lazy daemon imports; ≤4 idle-bound workers beside 4 normal workers; serial phase/windows unchanged. Historical idle 605→453 s, not a current A1 proof. |
| D50 | r12/Q12: B11 explicitly superseded K1/K9, retained unknown-task rc 5 rather than rewriting to submitted rc 6 (covered K9). Additive K11/L3/L6/J7 checks only, no weakened assertions, extra test methods, speed or product changes. |
| D51 | r11c/L5: require final complete legacy block and consistent task IDs; classify identifiable corrupt merge rows only, durably skip others; NULL requests match exact source_seq; missing runs produce detached terminals. Once-per-boot and real committed action-path coverage added, retaining D38's boundary qualification. |
| D52 | r12b/Q14: search task rejection history, not just the latest queued source. The most recent rejection reason and stored previous deliverable summary survive failed/timeout rework and explicit retry; first attempts without rejection do not acquire feedback. |
| D53 | r12b/L2: completion disregards only the authenticated current lead run token's own open lead_turn run lease, matched by project/agent/assignment/run ID and exact leadturn key. Foreign task/merge/leadturn leases and all task/approval guards retain rc 6. |
| D54 | r12b/L1/L2: approval decisions/expiry retain requester inbox delivery and add a separately deduplicated lead_wake only for the project's current launch lead requester; normal debounce applies. task.merged now shares merge.failed's lead fan-out; async success can drive deliver/complete without intake. |

## 6. Remaining gate work

- A1/A3 execution work is complete: Kiro ACCEPT, idle 556 s / 555 s,
  678 tests per run, both logs absent
  ([execution record](coordination/reviews/m2-gate-kiro.md)). No limit was relaxed.
- Obtain Linear gate re-review on the fixed tree. The `ff00f77` REVISE
  remains the gate verdict; m3-r9c fixes MAJOR-1 and this documentation
  fixes MINOR-1 ([gate review](coordination/reviews/m2-gate-linear.md),
  [m3-r9c ACCEPT](coordination/reviews/linear-m3-r9c.md)).
  Per-run r12c/m3-r9c ACCEPT does not close the M2 gate.
- Known limitation / nonblocking follow-up: `tests/run_tier.py::run_single_test`
  reports any rc-0 subprocess as `ok`, including a future skipped test.
  No skips exist in the unit/integration/recovery tiers today; smoke/probe
  skip sites are outside `scripts/test.sh`. Add distinct skip reporting in a
  future runner change, not this docs-only run
  ([Kiro finding 2](coordination/reviews/m2-gate-kiro.md)).

Kiro execution checks: ACCEPT (docs/coordination/reviews/m2-gate-kiro.md)

Linear gate review: REVISE on ff00f77 (docs/coordination/reviews/m2-gate-linear.md); re-review pending on the fixed tree
