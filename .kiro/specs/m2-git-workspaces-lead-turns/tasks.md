# Implementation Plan: M2 Git workspaces, merge, lead turns

> **Public snapshot note (2026-11-13).** This file records how the project was built. It cites private build records (`docs/HANDOFF.md`, `docs/QUEST-HANDOFF.md`, `docs/coordination/`, `logs/`) that are not included in the public repository. Final status: M1, M2 and M3-lite were accepted, and M4 (polish: Kiro launch enabled, N13 real end-to-end run, README, MIT license) is done. The M3 gate was closed by a full external review (ACCEPT, 2026-11-12) instead of the Linear Loop gate planned below, and a pre-public review (2026-11-12) led to five P2 fixes merged before publication. Unchecked items below were either completed under a different route or deferred.

Consolidated on 2026-11-09 from docs/coordination/log.md, `git log` on main (853548f, merge commits listed with `git log --merges`; rechecked at b8d102c, which includes the m2-r12 merge cb278d2) and docs/coordination/reviews/; the milestone was originally planned in docs/plan.md §16 M2 and docs/coordination/plans/m2-plan.md (runs r1–r12), and built by the coder rotation recorded in docs/coordination/log.md.

Run tasks: `[x]` = implementation merged on main, `[ ]` = not on main. Gate tasks (11.x): `[x]` = that gate check returned ACCEPT; task 11 is checked only when the gate has passed (both reviews ACCEPT), independent of the implementation merge state. Coders and commits are given where the log or the merge commit names them. Requirement numbers refer to `requirements.md` in this folder.

- [x] 1. m2-r1: K0 git fixture and invariant I9
  - vibe ds-flash r1 (6e1f37d, dbc3310, 8f2ea15); Kiro m2-r1 REVISE, r1b (vibe), Kiro m2-r1b REVISE, r1c; Quest review m2-r1c ACCEPT; merged 2210265.
  - _Requirements: 9.2 (ACC: A8)_

- [x] 2. m2-r2: `mesh-actions` parser and lead prompt renderer with size cap
  - vibe ds-flash r2 (7c3a77e, 5ecf0ce, 0238c3d); Kiro m2-r2 REVISE; fix rounds r2b2 (24e48ed, 215958d, a4f374f) to r2f, Quest reviews m2-r2b … m2-r2e REVISE, m2-r2f ACCEPT; merged b1e5646 (log 2026-11-09 06:15: "M2 r2 complete after 6 rounds").
  - _Requirements: 6.3, 7.1 (ACC: L6, L2)_

- [x] 3. m2-r3: repo projects, integration branch, lead `repo/` worktree
  - Kiro m2-r3 REVISE (failed intake left the branch and worktree behind); r3b by the Quest coder; Kiro m2-r3b ACCEPT; merged 55a9274 (log 2026-11-09 03:45, 05:15).
  - _Requirements: 1.1–1.4, 9.3 (ACC: K1, K2, K13, B11)_

- [x] 4. m2-r4, r5, r6: code-task worktrees, merge pipeline, merge test step
  - Quest coder (log 2026-11-09 05:15, 06:15). Kiro m2-r4 REVISE, m2-r4b-r5 REVISE, m2-r5b-r6 ACCEPT; merged 2dc2daf (log 2026-11-09 09:55).
  - [x] 4.1 r4 (+r4b): worktree per code task, fresh `-a<n>` branch after `lease_expired`, submission git facts
    - _Requirements: 2.1, 2.2 (ACC: K3, K4)_
  - [x] 4.2 r5 (+r5b): `task merge` guards, merge lease, conflict abort, dirty checks, noop, commit, T14–T17
    - _Requirements: 3.2, 3.3, 3.5, 3.6, 9.3 (ACC: K4, K6, K8, K9, B11)_
  - [x] 4.3 r6: supervised `merge_test` run, redacted `test.log`, timeout
    - _Requirements: 3.1, 3.4 (ACC: K5, K7)_

- [x] 5. m2-r7: merge crash recovery
  - merged 1b2b9bd "M2 r7 merge crash recovery + r6 minors + r8 N-1 + A1 speed pass (Kiro ACCEPT m2-r7)"; r7b merged 0d3a87f (Quest reviewer ACCEPT m2-r7b); r7c (droid gpt-6.1-sol) merged 6307eac (log 2026-11-09 15:35).
  - _Requirements: 4.1 (ACC: K14)_

- [x] 6. m2-r8: `project deliver`, `workspace prune/delete`, `branch -d`
  - vibe ds-flash r8, Quest reviewer REVISE (unscoped prune authz); r8b, Quest reviewer ACCEPT; merged b850295 (log 2026-11-09 10:15).
  - _Requirements: 5.1–5.4 (ACC: K10, K11, K12, A6)_

- [x] 7. m2-r9: lead run tokens, lead-turn scheduler, debounce, backoff, `lead.unreachable`
  - Kiro m2-r9 REVISE; r9b and r9c (Quest reviews m2-r9b, m2-r9c); r9d (droid gpt-6.1-sol, 1172 s) Quest reviewer ACCEPT; merged a8e1028 (log 2026-11-09 14:15).
  - _Requirements: 6.1–6.3, 1.4 (ACC: L1, L3, L6, K13)_

- [x] 8. m2-r10: lead actions, set-lead terminates the old turn
  - droid claude-opus-5-5 (2001 s, head 3973bf1); Kiro m2-r10 REVISE (durable supersede); decisions: `task.merge` action runs async after the closing tx, approval key on action items (log 2026-11-09 15:20).
  - r10b (droid claude-opus-5-5; d2a7ead, b9699f2) Quest reviewer ACCEPT; merged 3a36085 (log 2026-11-09 16:45).
  - _Requirements: 7.1, 7.2 (ACC: L2, L4)_

- [x] 9. m2-r11: session ids and resume plumbing
  - droid gpt-6.1-sol attempt 2 (four commits to 8a9d4d4); r11b (d3a55ae) Quest reviewer ACCEPT; merged 83f95e1 (log 2026-11-09 18:45, 19:25).
  - Linear Agent review of the pre-fix range: REVISE, 4 MAJOR + 3 MINOR (docs/coordination/reviews/linear-m2-r11.md); r11c (droid gpt-6.1-sol, f670c4e) Quest reviewer ACCEPT; merged 5a24082 (log 2026-11-09 20:20, 21:15).
  - _Requirements: 8.1 (ACC: L5)_

- [x] 10. m2-r12: usage docs, reconciliation, `docs/m2-gate-report.md`
  - droid gpt-6.1-sol on `wt/m2-r12`: 505fca5, 40e72d0, 5217ec8 (merged with r12b/r12c at cb278d2).
  - Linear Agent review PR #2: REVISE (docs/coordination/reviews/linear-m2-r12.md: rework-feedback guarantee in usage.md, lead `project.complete` blocked by its own lease, approval decisions and `task.merged` not waking the lead, three gate-report evidence fixes).
  - [x] 10.1 r12b fix round (droid gpt-6.1-sol; 5e715c3, e1b6d36; 442,311 credits; log 2026-11-09 22:55): Linear REVISE (linear-m2-r12b.md, MINORs only)
  - [x] 10.1b r12c fix round (droid gpt-6.1-sol; d2be66a, 1b52d17; 180,407 credits): Linear ACCEPT (linear-m2-r12c.md)
  - [x] 10.2 merged at cb278d2 via `scripts/coord/merge.sh` after a Linear ACCEPT, full suite on main, `sync-github.sh`
  - _Requirements: 9.1–9.3 (ACC: K1–K14, L1–L6, A1, A3, A6, A8)_

- [ ] 11. M2 gate (review routing v2, docs/QUEST-HANDOFF.md 2026-11-09 21:51); both reviews must ACCEPT
  - [ ] 11.1 Linear Loop gate review (JAM-60 with the `gate-review` label) on main ff00f77: REVISE (docs/coordination/reviews/m2-gate-linear.md). MAJOR-1: the async merge extension test in `tests/integration/test_lead_actions.py` fails under a race (2 of 5 repeats). MINOR-1: `docs/m2-gate-report.md` and `docs/impl-notes-m2.md` miss the r12b/r12c re-reviews.
    - Fixes: the lead-actions flake fix landed with m3-r9c (merge b8d102c, Linear ACCEPT linear-m3-r9c.md); the gate-report docs fix m2-r12d (d72d087) is in review as PR #6. A new gate review is still needed.
  - [x] 11.2 Kiro execution checks: ACCEPT (docs/coordination/reviews/m2-gate-kiro.md). Idle A1 556 s and 555 s, 678 tests (unit 356, integration 273, recovery 49), rc 0, both tripwires absent. Finding 1: `docs/m2-gate-report.md` still says A1 FAIL and must be updated.
  - _Requirements: 9.1, 9.2 (ACC: A1, A3, A8)_
