# M2 implementation notes index

Scope: Git workspaces, merge and lead turns, checklist K1–K14/L1–L6.
This is an index and consolidated record, not a requirements amendment.
See [gate report](m2-gate-report.md) for current evidence and gate review routing.
**A1 PASS:** [Kiro execution checks](coordination/reviews/m2-gate-kiro.md)
ACCEPT, two back-to-back idle runs of **556 s / 555 s**, each **678 tests,
rc 0** (356 unit, 273 integration, 49 recovery), no other suite running,
both tripwire logs absent. The **+37 tests** since r12b belong to the later
M3 chain, outside M2 scope. Linear gate re-review remains pending.
Historical r12 suite: 564 tests pass, 694 s, failed the strict <600 s gate.
r12b closes the four Linear review runtime gaps; its historical validation
is recorded in the [r12b section](impl-notes-m2-r12.md#r12b).
Historical full r12b suite: **rc 0, 641 tests, 845.995 s wall** (runner 846 s);
346 unit, 262 integration, 33 recovery; both tripwire paths absent.
Four methods are new; 637 checks already existed in the merged starting tree.
That run failed A1, with another suite observed afterward; contention is not a waiver.
The merged M3 chain and m3-r9 are outside M2 scope.

## Sources and readings

Requirements, plan §10/§11.7/§12.3–§12.6/§16 and checklist §0/K/L/A govern
this work. M1a/M1b/M1c remain accepted; consult their implementation notes,
M1c run-2 notes and gate reports for inherited decisions. The planner's Q
decisions and read-only coordination mapping establish scope and actual coders.
Later review-round corrections below supersede contradictory early statements.
N1 is **not changed in M2**; N23 supersession belongs to the M3 report.

| Notes (includes appended review rounds unless separate) | Actual coder | Scope |
|---|---|---|
| [r1](impl-notes-m2-r1.md), r1b/c | vibe DeepSeek-v4.1-flash | K0 fixture, I9, git env isolation |
| [r2](impl-notes-m2-r2.md) | vibe DeepSeek-v4.1-flash | Actions parser and lead prompt |
| [r2b](impl-notes-m2-r2b.md) | vibe DeepSeek-v4.1-flash | Prompt trimming/parser fixes |
| [r2c](impl-notes-m2-r2c.md) | vibe DeepSeek-v4.1-flash | Copying, drop order, batching |
| [r2d](impl-notes-m2-r2d.md) | vibe DeepSeek-v4.1-flash | Multibyte fixed-section caps |
| [r2e](impl-notes-m2-r2e.md) | vibe DeepSeek-v4.1-flash | Immutable authority, byte room |
| [r2f](impl-notes-m2-r2f.md) | vibe DeepSeek-v4.1-flash | Bounded recutting, omitted counts |
| [r3](impl-notes-m2-r3.md), r3b | Quest coder | Repo intake, Q3 order, B11 |
| [r4](impl-notes-m2-r4.md), r4b | Quest coder | Code worktrees and submit metadata |
| [r5](impl-notes-m2-r5.md), r5b | Quest coder | Merge pipeline and failure atomicity |
| [r6](impl-notes-m2-r6.md) | Quest coder | Merge test step and termination |
| [r7](impl-notes-m2-r7.md), r7b | Quest coder | Recovery, P5, kill failures, A1 speed pass |
| [r7c](impl-notes-m2-r7c.md) | Droid GPT-6.1 Sol | Isolated recovery failures |
| [r8](impl-notes-m2-r8.md), r8b | vibe DeepSeek-v4.1-flash | Deliver/prune/delete and scope |
| [r9](impl-notes-m2-r9.md), r9b/c/d | vibe; Droid claude-sonnet-5-5 (r9c); Droid GPT-6.1 Sol (r9d) | Lead turns, backoff, run-token scope |
| [r10](impl-notes-m2-r10.md), r10b | Droid claude-opus-5-5 | Actions, approvals, supersede, async merge |
| [r11](impl-notes-m2-r11.md), r11b/c | Droid GPT-6.1 Sol | Resume, durable queue, failure isolation and r11c recovery hardening |
| [r12](impl-notes-m2-r12.md), r12b/r12c/r12d | Droid | Usage, reconciliation, gate report; r12b runtime/docs `5e715c3`, `e1b6d36`; r12c tests/docs `d2be66a`, `1b52d17`; r12d docs-only gate reconciliation ([coordination log](coordination/log.md), [run notes](impl-notes-m2-r12.md)) |

## Post-r12 evidence and reviews

- r11c `f670c4e` adds L5 recovery hardening evidence in
  `tests/recovery/test_recovery_lead_merge.py`: final legacy block/task-ID
  consistency, identifiable corrupt JSON/durable skips, exact NULL-request
  source matching, missing-run detached failure, once-per-boot and real
  committed close/action-path recovery. The strict parser test is in
  `tests/unit/test_lead_actions.py`. These are direct boundary tests, not a
  SIGKILL-at-queue-boundary claim.
- r12b adds four CLI/fake regression methods in
  `tests/integration/test_rework_feedback_retry.py` and
  `tests/integration/test_lead_wakes_m2.py`. They prove latest feedback/summary
  after failed and timed-out rework→retry; completion ignores only the verified
  own-turn lease; approval and async merge success alone start debounced turns,
  reaching retry or deliver/complete. Foreign leases remain rc 6.
  [r12c](impl-notes-m2-r12.md#r12c) corrects the debounce baseline to the oldest
  carried message (still ≥2 s), adds K4 references and distinct-summary checks.
  Its historical full suite passed 641 tests, rc 0, 865.520 s (runner 866 s),
  with contention, not an idle A1 pass.
- M3 chain/m3-r9 are present in the merged validation tree only. Vendor
  evidence/fixtures and M3 acceptance belong to M3, not this report.
- Linear Agent's [r11](coordination/reviews/linear-m2-r11.md) and
  [r12](coordination/reviews/linear-m2-r12.md) reviews were REVISE; their M2
  findings drove r11b/c and r12b.
  [r12b re-review](coordination/reviews/linear-m2-r12b.md) was **REVISE**
  (no MAJOR; two MINORs and one NIT);
  [r12c re-review](coordination/reviews/linear-m2-r12c.md) was **ACCEPT**,
  merged as `cb278d2` ([r12d scope](../prompts/droid-m2-r12d.md)).
  These per-run verdicts do not establish M2 gate or A1 acceptance.
  Quest reviewer accepted [r11b](coordination/reviews/m2-r11b.md)
  and [r11c](coordination/reviews/m2-r11c.md). Historical Kiro reviews include
  [r4b/r5](coordination/reviews/m2-r4b-r5.md),
  [r5b/r6](coordination/reviews/m2-r5b-r6.md) and
  [r10](coordination/reviews/m2-r10.md), followed by fix rounds. These per-run
  records are not final gate acceptance.
- Routing v2 (James, 2026-11-09 21:51): Linear Loop performs the gate review;
  Kiro performs A1 timing, A3 tripwire and merged-tree execution checks.
  [Kiro execution checks](coordination/reviews/m2-gate-kiro.md) are ACCEPT.
  [Linear gate review](coordination/reviews/m2-gate-linear.md) remains REVISE
  on `ff00f77`, pending re-review on the fixed tree.
- Gate MAJOR-1's L2 async-merge test race was fixed by m3-r9c `e7021a8`,
  merged `b8d102c` ([r12d scope](../prompts/droid-m2-r12d.md)).
  `tests/integration/test_lead_actions.py::TestLeadMergeAction.test_merge_action_queued_then_merged_and_other_project_refused`
  derives the winner from `task.merging.request_id`, correlating the other
  request's refusal without assuming FIFO. Author stress **1/20 → 0/20**
  ([notes](impl-notes-m3-r9.md)); independent
  [Linear m3-r9c ACCEPT](coordination/reviews/linear-m3-r9c.md) confirms 0/20
  on the fixed test, using a different baseline stress. This M2-relevant test
  fix does not expand the gate to M3 acceptance.
- Known nonblocking runner limitation: `tests/run_tier.py` reports rc-0
  skips as `ok`. No skips exist in the three tiers today; distinct skip
  reporting is a follow-up ([Kiro finding 2](coordination/reviews/m2-gate-kiro.md)).

## Consolidated rulings, readings and deviations

The D numbers are shared with the gate report. Historical intermediate
results are not current acceptance evidence; no threshold is waived here.

1. **D01, inherited gates:** M1's accepted first-scaffold/A9 red-commit exception
   is historical and not reopened. M2 does not change N1.
2. **D02, Q15 attribution:** run notes override wrong default Git author labels.
   Droid runtime forbids identity overrides; r11/r12 use configured identity
   and the Factory co-author trailer, not the prompt's requested agy identity.
3. **D03, Q1/A1:** host contention is disclosed, never subtracted. Older 636/852 s
   “under ten minutes” claims are incorrect. r12 baseline timed out at 900 s;
   historical final 694 s fails <600 s. No r12 speed changes. Later idle
   556 s / 555 s pass ([Kiro checks](coordination/reviews/m2-gate-kiro.md)).
4. **D04, K0/I9:** K0 adds `__pycache__/` ignore for clean test runs. Fixture
   env drops all inherited `GIT_*`; safe roots include its harness home.
   Product git strips repo-location env, not identity/config. I9 validates
   full SHA-1/SHA-256 commits and integration ancestry, distinguishing git errors.
5. **D05, Q2:** generated integration-branch collision is exercised at core
   level (rc 6), without overriding CLI IDs; invalid repo/base use the real CLI.
6. **D06, Q3 final order:** read-only repo validation follows auth; setup is the
   last new-project handler statement after inserts/event, before commit.
   Failure rolls back rows; a created branch stays if worktree add fails/times
   out, named in the rc 1 `git_failed`/`git_timeout` details.
7. **D07, intake inputs:** absolute repo top level; base must be a local branch,
   not SHA/tag/HEAD. Flags without repo or on follow-up are rc 2. Non-repo
   output stays unchanged. Unknown dependency retains existing rc 1. Storage
   is JSON argv or JSON string `"none"`; input literal must be exactly `none`.
8. **D08, synchronous intake:** registered git calls remain inside the request
   transaction, potentially blocking up to the 30 s git timeout/hook duration;
   no async intake phase was introduced.
9. **D09, lead repo/install check:** detached lead repo setup is post-commit,
   best effort, logged and retried on set-lead. Dry run writes no git state.
   Install-root wiring uses mocked core checks, never a fixture in the checkout.
10. **D10, Q13/worktrees:** code requires repo; general tasks are unchanged.
    First start forks the exact observed integration SHA; expired leases
    allocate a fresh branch/worktree but retain original base_commit.
11. **D11, preparation:** off-loop git needs a server pre-transaction hook
    outside the original file list. Reservations/per-task lock and 10 s result
    TTL fence preparation; inbox repeats auth/delivery/project/status checks.
    Launch can take an extra tick.
12. **D12, submit metadata:** authorize/fence before git; failures are nonblocking.
    Mesh replaces caller git metadata. All untracked paths count; dirty path
    output is capped at 200, diff stat at 16 KiB, with redaction.
13. **D13, Q4:** busy merge lease is rc 11 with lease detail. Client waits
    hardcoded 1800+120 s (no settable timeout); disconnect cannot cancel merge.
14. **D14, Q5:** even `none` creates a merge_test run under `merges/<mrg>/`;
    meshd owns the merge lease. No fake test process/token is invented.
15. **D15, merge refs:** use full refs/resolved SHAs against tag shadowing.
    Re-read authoritative pre-head while holding lease, not only the TX1 probe.
16. **D16, noop:** ancestor detection, not localized merge text; skip tests and
    report `tests:"none"` when the task branch is already integrated.
17. **D17, K8/git errors:** missing identity can fail merge preparation, not
    commit; identity markers classify `commit_failed`. Unexpected failures
    become rc 1 `git_failed`, with abort/verification where needed.
18. **D18, idempotency:** final transaction stores rc 0 **and rc 14** replies.
    Conflict-safe INSERT OR IGNORE prevents replay conflicts rolling back merge.
19. **D19, moved task:** finalization closes a run/releases lease even if task
    left merging; diagnostic `task_left_merging`, rc 1, avoids stranded runs.
20. **D20, test execution:** only MESH_RUN_ID, no run token; restore PYTHONPATH.
    Registered wrapper combines/redacts streams in 0600 logs. Failure tails
    keep last 50 lines, ≤1 KiB/line and ≤16 KiB total.
21. **D21, timeout:** no CLI timeout flag; direct test-step check uses 1 s and
    an escapee. Timeout/leftovers end runs killed with `timeout`/`leftovers`.
22. **D22, Q6 recovery:** merge_test is never adopted; terminate as `interrupted`.
    Current Git state/first parent supersede stale saved pre-head. Missing
    pre-head yields null restoration where unverifiable; nonmatching commits
    remain conservative. Noop interruption retries. Repeat recovery emits no duplicates.
23. **D23, recovery hook:** `after_merge_commit` is testing-only. Reconciled
    tests are passed/none, with test_log if present.
24. **D24, P5:** project cancel during merge returns rc 6 INVALID_TRANSITION,
    reason `merge_in_progress`, **before approval**, with no mutation.
25. **D25, kill_failed deviation:** merge test termination releases lease and
    reports survivors instead of §11.4 retry/hold; live/recovery emit
    `run.kill_failed`. Distinct open-run step-2 errors retain task/lease until
    later boot; `merge.recovery_blocked` is durable, redacted, reason-deduped,
    isolated per task, and logging failures cannot halt other recovery.
26. **D26, Q7:** explicit delete uses contained rmtree then metadata prune;
    **branch kept**, never force-delete. Safe prune adds exact `branch -d`
    allowance despite create-only plan wording. Keep dirty/unsafe/leased/
    committed-unmerged candidates; omit already-pruned tasks. No workspace
    means `deleted:false` plus event; dry run only reports planned transitions.
27. **D27, workspace/deliver scope:** current lead/user, no new capability.
    Only user may prune unscoped (r8's initial permissive reading corrected).
    Completed delivery allowed; cancelled refused; noop has no event.
    Checked-out base uses ff-only merge, otherwise checked CAS update-ref;
    full refs, pre/post base verification, never push. Both paths are tested.
28. **D28, Q8:** action-failure messages enter prompts but cannot alone trigger
    a new turn, preventing a self-sustaining failure loop.
29. **D29, Q9:** testing-only backoff is exactly 3 integers 0..600; production
    read path forces 30/120/600 even with injected DB values. Three default
    attempts use only the first two delays.
30. **D30, lead lifecycle:** unreachable dedup is per run/failure episode;
    a second episode notifies again. Spawn failure backs off; release by run ID.
    Acked messages retain delivering_run_id for audit; `lead.turn_started` added.
31. **D31, Q11/vendor gate:** FAKE goal on its own line; no vendor launch.
    Vendor rc 13/`lead.turn_refused` once per assignment until M3. Retain vendor
    inbox advice. Dirty lead repo retained/logged; refresh off-loop, best effort.
32. **D32, scope:** lead token lists/ID reads confined to its project/current
    assignment; deny other-project **and global/projectless approvals** even
    with relay, shared requester or leadership of both projects (r9d).
    New intake prohibited; scoped follow-up allowed. Executor rules unchanged.
33. **D33, inbox:** lead run token cannot list/ack lead_wake rows; a pending
    pointer cannot bypass the turn delivery/ack lifecycle.
34. **D34, Q10 actions:** run before revoke/close under credential-backed lead;
    handler `via='run_token'`, result `via='lead_turn'`, shared request ID.
    User-only config/agent/audit denial precedes supersede checking.
35. **D35, action approval:** optional top-level approval string becomes
    ctx.approval (empty is none). Normal hash/requester/project/channel checks
    apply. Missing approval is rc **7**, not task wording's 10. Action
    approval.request defaults to own project; direct projectless call denied.
36. **D36, async merge:** supersedes initial `not_internal` refusal. Read-only
    guards in rollback savepoint see earlier actions; queued result isn't the
    merge's idempotency result. Start post-commit with original principal and
    request ID; failed/duplicate starts emit phase `merge_start`. Other unused
    handler effects were logged rather than run inside close transaction.
37. **D37, durable queue:** r10b's in-memory-loss limitation is superseded by
    r11 event-based once-per-boot requeue/de-dup. Original normalized request/
    action persisted; legacy stdout fallback; TX1 rechecks current guards.
38. **D38, r11b recovery:** per-row `RECOVERY_FAILED` terminals and isolation,
    boot marker in finally, logger-safe supervisor. Coordinator approved direct
    unit-style boundary recovery tests, **not actual SIGKILL at this boundary**.
39. **D39, L4:** all old-lead delivering rows superseded/recreated; terminate
    after set-lead commit, durable assignment mismatch backstop across restart.
    New end_reason `lead_superseded`; test kill grace 5→10 s.
40. **D40, parser:** last valid fenced array; LF/CRLF only, matching fence
    length; >50 refuses whole block, independent item errors; missing args `{}`.
    No valid block means no actions; no Unicode line-separator interpretation.
41. **D41, prompt contract:** cap whole UTF-8 prompt; plain schema/list/example,
    no executable mesh-actions fence. Immutable rules/actions/fixed structure
    survive trimming; copy inputs, do not mutate caller dictionaries.
42. **D42, prompt budgets:** strings 60 chars/list 10; categories 600 bytes,
    models 1500. Minimum raised to 16384 (4096 cannot hold fixed multibyte
    structure); no production caller below 100000.
43. **D43, trimming:** oldest events/notes first, terminal tasks all-at-once
    with per-status counts; truncate titles/summaries before later drops.
    Drop newest messages/tasks/approvals to retain oldest. Quarter-size batches,
    exact omission counters; `messages_cut` separate from dropped messages.
44. **D44, last resort:** optional goal/criteria text may truncate/drop while
    fixed headings/rules survive. Authority excluded from variable-section guard.
45. **D45, bounded recut:** measure actual envelope/remaining byte room, shrink
    monotonically, max 8 passes, UTF-8 tiny-body check. Pathological sole message
    may drop (stays pending), contrary to at-least-one recommendation; clear cut
    counter on drop. Hidden categories report exact “N more” even if byte room exists.
46. **D46, Q14 resume:** always full prompt. Enable only supported=yes,
    verification=smoke and captured ID. Capture last stdout JSON session even on
    failure/killed/lost; missing/malformed keeps last, reclose cannot overwrite.
    Stored options plus authoritative real run ID/configured binary; vendor
    resume_verified option cannot grant capability. Fake only demonstrated.
47. **D47, fake golden:** coordinator approved `--fake-resume sess_1` expectation
    for all five resumed fake profiles; fresh/no-ID and all other assertions
    retained. No real fixture/hash change; installed agy 1.5.3 vs fixture 1.5.2
    remains honestly stale.
48. **D48, historical process:** r10 substeps 1+2 combined because shared files.
    r2d/e untracked prompt files were left uncommitted. Intermediate loaded-host
    H4 timing flake passed isolated/final; no removed timing assertion.
49. **D49, historical A1 speed pass:** coordinator-authorized lazy daemon imports
    and separate ≤4 idle-bound workers beside 4 workers; serial phase and required
    observation windows unchanged. Historical idle 605→453 s is not r12 evidence.
50. **D50, r12/Q12:** B11 retained as superseded by K1/K9: invalid repo rc 2,
    unknown task rc 5; submitted-task rc 6 in K9. Only additive K11/L3/L6/J7
    assertions; no weakened checks, new test methods, product or speed changes.
51. **D51, r11c/L5:** final complete legacy block and all stored task aliases
    must agree; identifiable corrupt merges fail once, unrelated corrupt rows
    get durable skips. NULL requests match exact source_seq; missing runs get
    detached terminal events. Added boot-local and real committed-path checks.
52. **D52, r12b/Q14:** rejection history supplies the latest reason and stored
    previous deliverable summary even after failed/timeout rework→retry, not
    just when the latest queued source is rework. No rejection means no feedback.
53. **D53, r12b/L2:** completion may disregard only the verified current lead
    run token's own open lead_turn lease, matched by project, agent, assignment,
    run and exact leadturn key. All other leases/task/approval guards retain rc 6.
54. **D54, r12b/L1/L2:** approval decisions/expiry retain requester inbox
    delivery and add a deduplicated wake only for the project's current launch
    lead requester. task.merged now wakes the lead, as merge.failed already did.
    Normal debounce/lease guards still apply; async success can drive delivery
    and completion without an intake follow-up.

Kiro execution checks: ACCEPT (docs/coordination/reviews/m2-gate-kiro.md)

Linear gate review: REVISE on ff00f77 (docs/coordination/reviews/m2-gate-linear.md); re-review pending on the fixed tree
