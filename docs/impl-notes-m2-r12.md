# M2 r12: usage, reconciliation and gate report

Branch: `wt/m2-r12`. Starting HEAD: `83f95e1`.
Coder: Factory Droid, GPT-6.1 Sol.

## Readings and scope

- Read the M2 planner notes and r12 section; requirements; checklist §0 notation,
  K0/K1–K14, L1–L6, A1/A3/A6/A8; plan §10 and §12.4–12.6.
- Consulted M1a/M1b/M1c notes and M1c run 2, all earlier M2 run notes
  (including r2b–r2f, r7c and the appended r1b/c, r3b, r4b, r5b, r7b,
  r8b, r9b/c/d, r10b and r11b sections).
- Read the M1b gate-report format, usage/adapters docs, docs tests, runner,
  K/L scenarios and git author history. Used the coordination log read-only
  to correct default-author metadata when assigning split-review ownership.
- No prohibited file or product code changed. N1 is not changed by M2;
  its later supersession is not an M2 gate claim.

## Baseline

`bash scripts/test.sh` with tool timeout 900 s: **TIMEOUT, no exit code**,
900 s elapsed (901.5 s for the whole observation including process checks).
Unit tier: **283 tests, rc 0, 95.605 s**. Integration tier did not finish;
recovery and final tripwire checks were not reached. No fixture-hash failure.
This is not a green baseline, nor an A1 pass.

Before/after checks invoked `pgrep -fa run_tier.py` through an allowed
`python3 -c` subprocess. Excluding the check's own command-line matches:
- Before: PID 33622, integration suite in `mesh-wt-m3-r5`.
- After: PID 33622 and PID 39054, the latter in `mesh-fresh-v1`.
- Their working directories were read from `/proc/<pid>/cwd`. No other
  worktree or process was modified or stopped. No suite-speed changes made.

## Sub-step 1: usage

- `docs/usage.md`: built repo/task/merge/deliver/prune/delete workflows;
  explicit `tests:"none"`, identity failure, no push/config, branch retention;
  fake-only lead turns, vendor refusal, debounce/leases, full allowed action
  list, action approvals/async merge recovery, backoff, scoped credentials
  and verified-only resume. Test commands require `/tmp/mh-*` homes/repos
  and must not leave untracked checkout or integration artifacts.
- `docs/adapters.md`: one line stating fake-only M2 resume evidence.
- Existing J7 assertions need no removal: the row still contains M2 and
  `delivery=inbox`, now correctly scoped to vendors (coordinator addition A).
  A later additive reconciliation check can pin that distinction.

## Readings / deviations

- Runtime Git instructions prohibit author/committer overrides. Use the
  configured identity, not the requested `--author`, with the required
  Factory Droid co-author trailer. Actual coder attribution is recorded here.
- A1 remains a hard **<600 s** requirement. Contention is disclosed, not used
  to convert a slow or incomplete run to PASS.
- The consolidated index/report distinguish superseded intermediate readings
  from shipped behavior and correct old claims that 636/852 s were under
  ten minutes. No existing acceptance threshold is relaxed.

## Changed assertions

None in sub-step 1. Existing documentation/security assertions and ACC tags
are retained.

## Validation and remaining work

Baseline and final suite outcomes, reconciliation coverage, commands and
commit identifiers are recorded below as each sub-step completes.

### Sub-step 1 result

Commit `505fca5`. Existing check
`python3 -m unittest tests.unit.test_docs_m19 -v`: rc 0, 6 tests,
0.896 s. `git diff --check`: rc 0.

### Sub-step 2: additive reconciliation

Compared all 20 K/L Check/Expect rows with their existing scenario assertions.
No missing product behavior was found. Added checks to existing scenarios,
without adding harnesses, test methods, sleeps, hooks or product changes:

- K11: `test_merge.make_project` and `test_deliver_prune.make_project` now
  snapshot every repo's remote refs **before intake**, not just after project
  creation. Their existing `assert_remote_unchanged` assertions remain, with
  an additional comparison for every created repo (including K8's no-identity
  repo). K8/K9 and the tested-project guard scenario now invoke that check,
  as does the two-project prune-scope scenario.
- L3: `test_l3_three_attempts_then_dead` additionally checks each actual
  start follows the prior end by at least the configured 1 s backoff.
- L6: `test_lead_prompt_cap` now writes the capped 50-message/200-event/
  100-note/300-task prompt under `/tmp/mh-*` and builds Kiro readonly argv.
  It checks the complete prompt is passed, cwd/stdin, and narrow trust flag.
  No vendor binary is resolved or launched. Existing multibyte/minimum-budget
  property checks remain unchanged.
- J7: six additive row assertions pin availability for fake only, M3 vendor
  restriction, rc 13, `lead.turn_refused`, and the vendor-only inbox advice.
  The original M2/inbox assertions, every other assertion and every ACC tag
  are unchanged. There are **no replaced/weakened/deleted assertions**.

Focused command:
`python3 -m unittest tests.unit.test_docs_m19 tests.unit.test_lead_prompt tests.integration.test_merge tests.integration.test_merge_tests tests.integration.test_deliver_prune tests.integration.test_lead_turns.TestLeadTurnBackoff -v`
returned **rc 0, 51 tests, 137.078 s**.

Coverage is full for K1–K14/L1–L6 using the combined named tests in the
gate-report table, subject to final suite verification. K1's collision remains
the coordinator-approved core test (Q2); L6 combines the readonly unit build
with the fake real-turn pending-row scenario. A8 includes I9 plus existing M1
state-specific checks; A6 retains its existing allow-list and spawn scan.

Read-only hygiene checks so far: literal `"M2"` search in `mesh/` found no
matches; B11's retained method explicitly says superseded by K1/K9; the
submitted-task rc 6 case is in `test_merge.test_k9_guards` (B11 itself keeps
the previously approved unknown-task rc 5 probe). `git branch --list 'mesh/*'`
returned no branches. No temporary iteration helper files were created.

### Final suite result (after last test change)

`bash scripts/test.sh` (tool timeout 2400 s) completed **rc 0**:
unit **283 / 28.012 s**; integration **253 / 570.163 s**; recovery
**28 / 95.624 s**. Total **564 tests**, runner wall time **694 s**.
Whole observation including before/after checks: **695.1 s**.
**A1 FAIL**: 694 is not strictly less than 600. No performance changes,
extra benchmark loops or attempt to discount concurrent time.

`pgrep -fa run_tier.py` observations, via `python3 -c` subprocess:
- Before: PID 26081, recovery, `/home/node/workspace/mesh-fresh-v1`.
- After: PID 48876, integration, `/home/node/workspace/mesh-wt-m3-r5`.
- No check-command matches included. No processes stopped.

Both exact runner tripwire paths are absent:
`/tmp/mesh-tripwire-suite-3396540402.log` (key from `cksum` of the checkout
path **without newline**) and `/tmp/mesh-tripwire-hit.log`. The runner's
own end-of-suite checks also passed. An earlier follow-up computed a key
with an extra escaped newline; the exact-key check above supersedes it.
Fixture hashes, A6 scan/allow-list, I9 and doctor teardown all passed.
Literal `ACC: <ID>` searches with word boundaries found existing passing
tests for every K/L/A ID in scope; A1's structural test does not prove its
timing threshold. `grep -rn '"M2"' mesh/`: rc 1, no matches.
`git branch --list 'mesh/*'`: rc 0, empty.

Files in sub-step 2: this note, `tests/unit/test_docs_m19.py`,
`tests/unit/test_lead_prompt.py`, `tests/integration/test_merge.py`,
`tests/integration/test_deliver_prune.py`, `tests/integration/test_lead_turns.py`.
All other files from sub-step 1 are unchanged since its focused docs check.
All K1–K14/L1–L6 are covered by the combined full scenarios in the gate
report, with the Q2/core and readonly-build qualifications above; A3/A6/A8
pass. A1 is the only unmet numeric gate. Final M2 acceptance remains blocked
on A1 and both independent reviewer verdicts, not on a test failure.

### Sub-step 3: consolidated documentation

Sub-step 2 committed as `40e72d0` after the completed rc-0 suite above.
Added `docs/impl-notes-m2.md`, linking all 18 run-note files/review rounds,
and `docs/m2-gate-report.md`, with:
- all 20 K/L and four A rows, actual-coder Author columns, named scenarios
  and real CLI mechanisms;
- per-author commit ranges corrected using run notes/coordination mapping;
- M1 regression, exact tripwire/process observations, cross-cutting ownership;
- 50 consolidated D records covering the Q rulings and later corrections,
  including P5, scoped/global approval distinctions, async/durable merge queue,
  RECOVERY_FAILED isolation, readonly prompt, fake golden resume, end reasons,
  branch -d allowance, historical speed pass and overridden author request;
- A1 explicitly FAIL and both split gate verdicts pending.

After these documentation-only files, reran existing
`python3 -m unittest tests.unit.test_docs_m19 -v`: **rc 0, 6 tests, 0.250 s**.
This does not replace or change the final 564-test result. No code/test
changed after that full suite. The report/index are new documentation, not new
test harnesses. No helper scripts/files were left behind. No public platform
write, push, author override, fixture/hash change or forbidden file edit.

Final delivered files across the three sub-steps: `docs/usage.md`,
`docs/adapters.md`, this note, `docs/impl-notes-m2.md`,
`docs/m2-gate-report.md`, `tests/unit/test_docs_m19.py`,
`tests/unit/test_lead_prompt.py`, `tests/integration/test_merge.py`,
`tests/integration/test_deliver_prune.py`, `tests/integration/test_lead_turns.py`.
Nothing in the requested implementation/doc reconciliation is unfinished.
**Outstanding gate work:** A1 remains unmet and needs coordinator follow-up;
Quest reviewer/Kiro independent split reviews remain pending. No acceptance
decision or idle-host timing is invented.

Final read-only documentation audit (inline Python, no helper file) returned
rc 0: 24 unique authored K/L/A rows; all 50 D records in index and report;
local Markdown links, referenced test paths/methods and 42 Git commit
references resolve. Final whitespace and fixture-branch checks returned rc 0;
no `mesh/*` branch. Sub-step 3 is documentation-only, so the completed final
suite above remains the last full run.

The initial staged report whitespace check returned rc 2 for three Markdown
hard-break lines with trailing spaces (untracked files had not been included
in the earlier unstaged check). Removed those spaces, reran staged and
whole-r12 whitespace checks, and amended the local documentation commit.

## r12b

Coder: Factory Droid (GPT-6.1 Sol). Starting HEAD `d88aa84`, branch
`wt/m2-r12`. Read the full Linear r12 review and the runtime callers/tests.
The merged tree contains r11c `f670c4e`, the M3 chain and m3-r9. r11c is M2
recovery evidence; M3 work is mentioned only as out of M2 scope.

### Runtime changes and regression evidence

1. **Q14/L5 feedback:** supervisor checks task rejection history rather than
   the latest queued transition's source. The existing feedback reader selects
   the most recent rejection and stored previous deliverable summary. Failed
   or timed-out rework followed by retry therefore keeps both in the feedback
   section of the full prompt; tasks without rejection keep no feedback.
   `test_rework_feedback_retry.py::test_rework_failure_retry_keeps_latest_feedback`
   drives four attempts through the CLI and fake for both nonzero exit and
   timeout: an older reject, another submission, latest reject, failed rework,
   explicit retry. It checks latest feedback/summary in the feedback section
   (not merely the spec, which also contains the summary), criteria/spec/attempt,
   and absence of older feedback.
2. **L2 completion:** a run-token caller can ignore only its verified own
   open lead_turn run's exact leadturn lease. Match run/project/agent/assignment;
   normal authz has already checked the current assignment. Other task, merge
   and leadturn leases, plus all task/approval guards, remain blockers.
   `test_lead_wakes_m2.py::test_complete_ignores_only_verified_own_turn_lease`
   executes fake actions and CLI completion. A real other project's live
   lead-turn lease is temporarily re-keyed for each negative fixture, restored
   before teardown; each foreign-lease action and user CLI call remains rc 6.
   Only the verified own-turn action completes the project; close releases its
   lease and emits exactly one completed event.
3. **L1/L2 approval wakes:** keep requester inbox delivery and add a distinct
   deduplicated lead_wake when the requester is the project's current launch
   lead (also applies to expiry). Existing scheduler debounce is unchanged.
   `::test_approval_decision_alone_wakes_lead_and_retries_action` first observes
   an action's rc 7/pending approval, prepares the fake's approved retry without
   a wake, and approves through the user CLI. Only the approval and prior
   action-failure messages reach the next turn; the retry succeeds, consumes
   approval, and the turn starts ≥2 s after the wake. Inbox delivery remains.
4. **L1/L2/K10 merge wakes:** task.merged now shares the existing merge.failed
   lead fan-out. Failure already woke launch leads and is retained.
   `::test_async_merge_success_wakes_lead_to_deliver_and_complete` uses a K0
   fixture, inbox code executor and a gated fake lead turn. Its accept/merge
   actions queue an async merge; before exit, the next goal is set to
   deliver/complete without intake. Success alone starts the next debounced
   turn; both actions succeed, base equals integration, project is completed,
   and exactly the task.merged wake is carried/acked.

Four new runtime test methods, all demonstrated red against unchanged starting
runtime before any product edit:

- Combined regression run: **rc 1, 4 methods, 58.337 s**; both feedback
  subtests lacked latest feedback, merge and approval had no wake.
- Completion's corrected fixture was rerun separately: **rc 1, 1 method,
  20.282 s**, all foreign-lease negatives passed and own-lease completion
  returned rc 6 instead of 0. Earlier exploratory runs had test-fixture errors
  (read-only DB connection, report JSON used where CLI needs a file); those
  errors are not claimed as runtime reproductions.
- Initial fixed run: **rc 0, 4 methods, 67.445 s**. Strengthened the summary
  check to inspect the feedback section and verify the stored CLI summary.
- Final focused command:
  `python3 -m unittest tests.integration.test_rework_feedback_retry tests.integration.test_lead_wakes_m2 tests.integration.test_lead_actions tests.integration.test_resume tests.unit.test_events tests.unit.test_projects -v`
  returned **rc 0, 23 tests, 130.566 s**. Four are new; the other 19 are
  existing action/scope/approval/resume/event/project checks.
- No existing test file/assertion was edited or weakened. All new tests use
  `/tmp/mh-*` harness homes and event/file gates, no fixed sleeps.

### Documentation and review routing

- Usage now states the exact feedback retry guarantee, own-turn completion
  exception and debounced approval/merge wakes.
- Gate report K5 cites the real integration two-parent/message assertions;
  K9 explicitly cites the direct core busy-lease guard with mocked authz;
  K13 says “after the integration tip is updated.”
- Gate report/index include r11c's hardening and named L5 recovery tests;
  M3 chain/m3-r9 remain outside M2 scope. Actual coders: r11c Droid Sol,
  r12/r12b Droid (configured Git identity, no override).
- Linked Linear Agent r11/r12 review records and historical Quest/Kiro review
  links. Review routing v2 (James 2026-11-09 21:51) replaces the former split:
  Linear Loop gate review pending; Kiro A1/A3/merged-tree execution checks pending.
  A1 stays FAIL until <600 s idle; speed belongs to a separate run. No test
  infrastructure, fixture, protected review/coordination file or M3 code edited.

### Full-suite validation and delivery

Full `bash scripts/test.sh`, after the final runtime/test edit, completed
**rc 0, 641 tests, 845.995 s measured wall** (runner: **846 s**).

| Tier | Count | Wall |
|---|---:|---:|
| Unit | 346 | 30.377 s |
| Integration | 262 | 741.534 s |
| Recovery | 33 | 73.596 s |

Four methods are new r12b regressions; the other **637 tests already existed**
in the starting merged tree (including out-of-scope M3 checks). All four new
regressions passed in the full suite. No code/test changed after this run.

`pgrep -fa run_tier.py` observations (excluding observation-command matches):
- Before: no other suite observed.
- After: PID 7172, integration,
  `/home/node/workspace/mesh-wt-m3-r9b`.
- This is endpoint evidence only, not an idle-host claim or a timing discount.
  No other suite or worktree was stopped or modified.

Runner tripwire end checks passed. Independent exact-path checks found both
`/tmp/mesh-tripwire-suite-3396540402.log` and `/tmp/mesh-tripwire-hit.log`
**absent**. Full suite ran before the runtime/test commit.
`git diff --check` and `git branch --list 'mesh/*'`: rc 0, no fixture branches.
**A1 FAIL:** 845.995 s exceeds the unchanged <600 s limit. No speed or test
infrastructure changes were attempted.

Requested items 1–6 are implemented, including all four runtime fixes and
documentation/evidence/author/reviewer corrections. Nothing in this fix run is
blocked. Outstanding gate work remains A1 and the two routed independent
review/execution verdicts; those are not claimed as completed by this coder.

Runtime/tests/usage committed as **`5e715c3`**:
`fix(m2): close lead progress gaps (ACC: L1, L2, L5, K10)`.
The full suite above preceded this code commit. Final existing documentation
check `python3 -m unittest tests.unit.test_docs_m19 -v`: **rc 0, 6 tests,
0.667 s** after the suite evidence update. Subsequent edits are documentation
only (commit reference and validation record), not runtime/tests.

Delivered files: `mesh/daemon/supervisor.py`,
`mesh/daemon/handlers/project.py`, `mesh/core/events.py`,
`tests/integration/test_lead_wakes_m2.py`,
`tests/integration/test_rework_feedback_retry.py`, `docs/usage.md`,
`docs/m2-gate-report.md`, `docs/impl-notes-m2.md`, this run note.
Only these nine allowed files changed; no existing test file changed.
No push/fetch/branch/worktree creation, identity override, vendor inference or
outside-platform write. No temporary iteration scripts/helper files remain.
An inline documentation consistency check (new, no helper file) passed:
37 local links resolve, both indexes contain the same 54 D records in order,
and both end with the exact requested review-routing lines.

## r12c

Coder: Factory Droid (GPT-6.1 Sol). Starting HEAD `e1b6d36`, branch
`wt/m2-r12`. Addressed all three supplied Linear r12b review findings.
The named review file is absent from this checkout; the task's quoted findings
provided the scope. No product code or prohibited file changed.

### Review fixes

1. **Debounce baseline:** reproduced the original approval-wake assertion
   failure with the existing single-test command: **rc 1, 1 test, 13.894 s**,
   `1.009 not greater than or equal to 2`. The assertion now measures turn
   start minus the **oldest carried/coalesced message's timestamp**, matching
   `mesh/daemon/lead_turns.py`'s documented trigger. It still requires the full
   **2 s**, with no tolerance, sleep, or reduced threshold. Existing carried
   message types/statuses and approval/retry assertions remain. This corrects
   r12b's wording above: the coalesced batch, not each later wake, must age 2 s.
2. **K4 evidence:** gate-report row now names
   `tests/integration/test_merge_refs.py::test_merge_ignores_tag_named_like_task_branch`
   (lines **26–41**) and `::test_base_commit_ignores_tag_named_like_base`
   (lines **50–63**). Checked the current names/ranges and ran both tests.
3. **Latest deliverable:** strengthened the existing feedback retry test with
   distinct original/latest CLI-submitted summaries. Only the fake script's
   task spec is updated as an explicit SQLite fixture between closed attempts;
   neither deliverable nor rejection history is injected. Both stored summaries
   are checked after their respective CLI submissions. For both nonzero-exit
   and timeout rework failures, the retry prompt's feedback section must contain
   the latest summary and rejection reason, and exclude the original summary.
   Existing absence-of-older-feedback and full prompt checks remain.

Test changes committed as **`d2be66a`**:
`test(m2): correct coalesced wake timing and latest feedback checks`.

### Validation

- All test methods already existed. No new test methods/harnesses were added;
  this run corrected the timing baseline and added distinct-summary assertions.
- Focused existing checks:
  `python3 -m unittest tests.integration.test_lead_wakes_m2 tests.integration.test_rework_feedback_retry tests.integration.test_merge_refs tests.unit.test_docs_m19 -v`
  returned **rc 0, 12 tests, 84.033 s**.
- Required full `bash scripts/test.sh`, after the final test changes and before
  both commits: **rc 0, 641 tests, 865.520 s measured wall** (runner: **866 s**).
  Unit: **346 / 64.191 s**; integration: **262 / 763.997 s**;
  recovery: **33 / 36.478 s**. Both revised regressions and K4 ref tests passed.
- `pgrep -fa run_tier.py` before: PID **21735**, integration,
  `/home/node/workspace/mesh-fresh-v1`. After: **no actual suite process**.
  The observation's own supervisor/watcher command matched at both endpoints
  (PID 28327); it is not another suite. No other process/worktree was changed
  or stopped. These are endpoint observations, not an idle-host claim.
- Runner tripwire checks passed; independent exact-path checks found
  `/tmp/mesh-tripwire-suite-3396540402.log` and `/tmp/mesh-tripwire-hit.log`
  both **absent**. **A1 remains FAIL:** 865.520 s exceeds the unchanged <600 s
  gate. Performance work is outside this small fix run.
- Whitespace checks passed. No tests or product code changed after the full
  run. Only the K4 citation and this validation note are documentation changes.
  The temporary suite log was removed after recording its results; no helper
  scripts/files remain. The existing untracked `prompts/droid-m2-r12c.md` was
  left untouched. No push, fetch, branch, author override or external write.

Delivered files: `tests/integration/test_lead_wakes_m2.py`,
`tests/integration/test_rework_feedback_retry.py`, `docs/m2-gate-report.md`,
and this note. All three requested review items are done.

## r12d

Docs-only gate reconciliation in `wt/m2-r12d`, based on merged `b8d102c`;
scope: [run prompt](../prompts/droid-m2-r12d.md). Only
`docs/m2-gate-report.md`, `docs/impl-notes-m2.md` and this note are changed.
This section supersedes the historical A1 FAIL / pending-review statements
above; it does not change their measurements or claim a new full-suite run.

### Review and commit record

- r12b runtime/tests/usage: `5e715c3`; documentation: `e1b6d36`.
  [Linear r12b re-review](coordination/reviews/linear-m2-r12b.md) was
  **REVISE**, with no MAJOR, two MINORs (debounce baseline and K4 citations)
  and one NIT (indistinguishable summaries).
- r12c tests: `d2be66a`; documentation: `1b52d17`
  ([coordination log](coordination/log.md)). The [r12c section](#r12c)
  records the fixes and historical 641-test / 865.520 s suite.
  [Linear r12c re-review](coordination/reviews/linear-m2-r12c.md) was
  **ACCEPT**, with 20 focused tests plus the delayed-approval check passing;
  full suite/A1 were not independently verified in that re-review.
  r12/r12b/r12c merged as `cb278d2`
  ([run prompt](../prompts/droid-m2-r12d.md)); per-run ACCEPT is not gate ACCEPT.
- [Linear gate review](coordination/reviews/m2-gate-linear.md) was
  **REVISE on `ff00f77`**. MINOR-1's missing r12b/r12c record is corrected
  in the report and index. MAJOR-1's L2 async-merge test race was fixed by
  m3-r9c `e7021a8`, merged `b8d102c`
  ([run prompt](../prompts/droid-m2-r12d.md)).
  The fixed
  `tests/integration/test_lead_actions.py::TestLeadMergeAction.test_merge_action_queued_then_merged_and_other_project_refused`
  selects the sole winner from `task.merging.request_id` and correlates the
  other request's exact refusal/index/failure notification, without assuming
  preflight completion order or weakening the safety assertions.
  Author stress improved **1/20 → 0/20** failed repetitions
  ([m3-r9c notes](impl-notes-m3-r9.md));
  [Linear m3-r9c ACCEPT](coordination/reviews/linear-m3-r9c.md) independently
  observed 0/20 failures on the fixed test, using a different baseline stress.
  Full M2 gate re-review on the fixed tree remains pending.

### Current execution evidence and follow-up

[Kiro execution checks](coordination/reviews/m2-gate-kiro.md): **ACCEPT**.
Two back-to-back idle `bash scripts/test.sh` runs in
`/home/node/workspace/mesh-fresh-v1` measured **556 s / 555 s**, each
**678 tests, rc 0**: unit **356**, integration **273**, recovery **49**.
No other suite was running. Both the default `/tmp/mesh-tripwire-hit.log`
and checkout suite-keyed log were absent after each run (no
`/tmp/mesh-tripwire*` files); runner end checks passed.
Run 1 was on `ff00f77`; run 2 included docs-only `dbcaac4`, with unchanged
code/tests (code equals `bd0929e`). These are the execution record's measured
trees, not a new timing claim for the later m3-r9c merge.

**A1 now PASS** under the unchanged strict <600 s gate. The 37 additional
tests since r12b's 641 come from the later merged M3 chain, outside M2 scope,
per Kiro's record. The report retains the historical contended r12 694 s,
r12b 846 s and r12c 866 s results above; none is discounted or called an A1 pass.

Known limitation / nonblocking follow-up from Kiro finding 2:
`tests/run_tier.py::run_single_test` reports any rc-0 subprocess as `ok`,
so a future skipped test would look like a pass. No skips exist in today's
unit/integration/recovery tiers; smoke/probe skip sites are outside the suite.
Distinct skip reporting needs a future runner change, not this docs-only run.

### Documentation validation

- Existing `python3 -m unittest tests.unit.test_docs_m19`: **rc 0, 6 tests**.
- Optional `python3 .kiro/hooks/scripts/acceptance-check.py docs/m2-gate-report.md`:
  **skipped**, `.kiro/hooks/scripts/acceptance-check.py` is absent.
- New inline documentation audit (no helper/test file): checked allowed-file
  scope, local links/anchors, all 24 gate rows, matching D01–D54 records,
  exact verdict endings, required evidence against the cited repo records
  and the fixed test's request-ID correlation. **PASS**.
- `git diff --check`: **rc 0**. No code/test changes, new full-suite run
  or temporary scripts/helper files in this docs-only run.

Kiro execution checks: ACCEPT (docs/coordination/reviews/m2-gate-kiro.md)

Linear gate review: REVISE on ff00f77 (docs/coordination/reviews/m2-gate-linear.md); re-review pending on the fixed tree
