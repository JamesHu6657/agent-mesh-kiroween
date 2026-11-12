# Implementation Plan: M3-lite Real adapters

> **Public snapshot note (2026-11-13).** This file records how the project was built. It cites private build records (`docs/HANDOFF.md`, `docs/QUEST-HANDOFF.md`, `docs/coordination/`, `logs/`) that are not included in the public repository. Final status: M1, M2 and M3-lite were accepted, and M4 (polish: Kiro launch enabled, N13 real end-to-end run, README, MIT license) is done. The M3 gate was closed by a full external review (ACCEPT, 2026-11-12) instead of the Linear Loop gate planned below, and a pre-public review (2026-11-12) led to five P2 fixes merged before publication. Unchecked items below were either completed under a different route or deferred.

Consolidated on 2026-11-09 from docs/coordination/log.md, `git log` on main (853548f; rechecked at b8d102c, which includes the m3-r9b merge 1fbfe9a, the m3-r4 merge bd0929e and the m3-r9c merge b8d102c) and docs/coordination/reviews/; the milestone was originally planned in docs/plan.md §16 M3, docs/coordination/plans/m3-plan-draft.md and docs/coordination/plans/m3-plan.md (11 coder runs, 2 optional, 7 coordinator steps C0–C6), and built by the coder rotation recorded in docs/coordination/log.md.

`[x]` = merged or committed on main. `[ ]` = open. `[COORD]` = Quest only; `[COORD+CONSENT]` = Quest only, real model quota. Requirement numbers refer to `requirements.md` in this folder.

Merge-order note: m3-plan rule 1 says m3-r5 merges only after m3-r4. The git log shows m3-r5 merged at de8b6b9 while m3-r4 was still open on its branch (log 2026-11-09 20:58); m3-r4 merged later at bd0929e. This is recorded, not resolved, here.

- [x] 1. C0 [COORD]: James's decisions G and D1–D7 logged, addendum N18–N27 committed, M3 never-edit additions (log 2026-11-09 12:44, 13:00)
  - _Requirements: scope in the Introduction (the addendum defines ACC: N18–N27)_

- [x] 2. m3-r1: help flag diff, capture writer with sidecars, opt-in probe tier
  - Quest coder; Kiro m3-r1 REVISE; r1b with r2 under Kiro m3-r1b-r2 ACCEPT (log 2026-11-09 14:50); merged in de8b6b9 "M3 r1-r3, r5 (+fix rounds)".
  - _Requirements: 1.1–1.3 (ACC: N10, N21, N22)_

- [x] 3. C1 [COORD]: probe run, promote droid 1.6.0 help/exec-help and agy 1.5.3 help/models
  - Staged at `/tmp/mh-probe-1794212343`: no flag diffs, no missing emitted flags, kiro list-models same-version drift only (log 2026-11-09 16:45); committed ac8a421.
  - Before that, attempt 1 failed on a probe-tier Row-vs-tuple assertion bug, fixed in bd51e7f (log 2026-11-09 16:20).
  - _Requirements: 1.1–1.3 (ACC: N10, N21, N22)_

- [x] 4. m3-r2: results schema and smoke harness core
  - Quest coder; Kiro m3-r1b-r2 ACCEPT; r2b reviewed with r3; merged in de8b6b9.
  - _Requirements: 2.1–2.3; results schema for 3.1, 3.2 (ACC: N11, N21, N25, N12)_

- [x] 5. m3-r3: droid cases S-X-1…7 and S-DROID-8, offline re-evaluation
  - Quest coder; Kiro m3-r2b-r3 REVISE (2 MAJOR: S-X-6 passed if droid exited before SIGTERM; single-case retries not promotable); ruling: no single-case retries (log 2026-11-09 16:45); r3b reviewed with r5; merged in de8b6b9.
  - _Requirements: 2.1, 3.1, 3.2 (ACC: N11, N12, N25)_

- [x] 6. m3-r5: evidence-driven launch and evidence output, N1/N4 supersession
  - Quest coder; Kiro m3-r3b-r5 REVISE (task retry requeued a launch task without an evidence check); r5b in two Quest coder sessions (a757f26 handoff; 8e2eac2, 83e82b6, 244e87c); Kiro m3-r5b ACCEPT; merged in de8b6b9 (log 2026-11-09 18:45–20:58).
  - _Requirements: 3.2, 4.1–4.4 (ACC: N19, N20, N23, N1, N4, N12)_

- [x] 7. C2 [COORD+CONSENT]: droid 1.6.0 smoke capture, `claude-sonnet-5-5`, readonly
  - Consent James 2026-11-09 12:44; staged R1 then R2–R6; committed 853548f (log 2026-11-09 23:00; results in design.md).
  - _Requirements: 3.1 (ACC: N11)_

- [x] 8. m3-r9 (optional): `refs/heads/<branch>` in merge and repo validation
  - Quest coder (9143c74 notes); merged 0a11c6c before its review, recorded as a slip (docs/QUEST-HANDOFF.md "Merge hard check").
  - _Requirements: 9.1; K4 is an M2 item (M2 requirements 2.2, 3.2) (ACC: K4)_

- [x] 9. m3-r9b fix round after the post-merge Linear review
  - Linear Agent post-merge review: REVISE (docs/coordination/reviews/linear-m3-r9.md).
  - droid claude-opus-5-5: 04b9a77, 0e314d4, 57a30b7, 807,585 credits (log 2026-11-09 22:55); Linear ACCEPT (linear-m3-r9b.md); merged at 1fbfe9a (log 2026-11-09 23:40).
  - [x] 9.1 m3-r9c follow-up: `base_moved` when the base moved to a descendant, lead-actions flake fix
    - droid gpt-6.1-sol, e7021a8, 2490 s, 115 turns, 319,131 credits, flake 1/20 → 0/20 (log 2026-11-10 00:22); Linear ACCEPT, PR #4 (docs/coordination/reviews/linear-m3-r9c.md); merged b8d102c.
  - _Requirements: 9.1; K4 is an M2 item (M2 requirements 2.2, 3.2) (ACC: K4)_

- [x] 10. m3-r4: adopted-wrapper seen map, `RunRequest` options, vendor lead-turn code gate
  - Quest coder: 5f9ac08, 012c7fb, 1658399, 7666005 (log 2026-11-09 20:52), then fix rounds r4b–r4e below; all merged at bd0929e.
  - Linear reviews PR #1: m3-r4 REVISE, m3-r4b REVISE, m3-r4c REVISE (docs/coordination/reviews/linear-m3-r4*.md).
  - [x] 10.1 r4b fix round (f65c7ef, 46ca4c4, a4009fa) and r4c fix round (09b5f48, 3d20b85): Linear REVISE each round
  - [x] 10.2 r4d fix round (2f71e09, 4dc4daa, fcf00e6; idle suite 505 s): Linear REVISE (linear-m3-r4d.md)
  - [x] 10.2b r4e fix round (6f50951, 2a19fe9; idle suite 538 s): Linear ACCEPT (linear-m3-r4e.md). Known limitation: pending run.seen_unusable events are memory-only until flushed.
  - [x] 10.3 merged at bd0929e via `scripts/coord/merge.sh` with a Linear ACCEPT on record, full suite on main
  - _Requirements: 5.1, 5.2 (ACC: N18, N19, A8)_

- [ ] 11. m3-r6: real droid `parse()` from the C2 fixtures; demote class-declared `reasoning_effort` to `help` (D6)
  - Needs C2 on main (done). New test `tests/unit/test_adapters_droid_real.py` (planned).
  - _Requirements: 6.1 (ACC: N24, N12, N6)_

- [ ] 12. C3 [COORD]: offline re-evaluation of the droid results with the real parser; promote; full suite on main
  - Needs m3-r4 (done, bd0929e), m3-r5 (done, de8b6b9) and m3-r6 on main. Expect `launch_enabled:true` for droid only if S-X-1 and S-X-6 pass.
  - _Requirements: 3.2, 4.1, 4.2 (ACC: N12, N19, N20)_

- [ ] 13. m3-r7a: opt-in real e2e test (N13) and code-task demo test (N26)
  - New file `tests/smoke/test_e2e.py` (planned). Needs C3 committed and m3-r4 merged (done, bd0929e).
  - _Requirements: 7.1, 7.2 (ACC: N13, N26)_

- [ ] 14. C4 [COORD+CONSENT]: one real Mesh e2e run on droid readonly (consent D2); at most one retry
  - _Requirements: 7.1 (ACC: N13)_

- [ ] 15. C5 [COORD+CONSENT] (optional, D3): droid `dev` code-task demo in a no-remote fixture repo, after a Factory quota check
  - _Requirements: 7.2 (ACC: N26)_

- [ ] 16. m3-r7b: `docs/adapters.md` and `docs/usage.md` from the results files
  - New test `tests/unit/test_docs_m3.py` (planned). Needs m2-r12 merged (done, cb278d2) and C4.
  - _Requirements: 8.1, 8.2 (ACC: N9, N12, N27)_

- [ ] 17. m3-r8: `docs/m3-gate-report.md`, `docs/impl-notes-m3.md`, reconciliation, idle A1
  - _Requirements: 8.2, 9.1, 9.2; reconciles Requirements 1–7 (ACC: N27, N10, N11, N12, N13, N18, N19, N20, N21, N22, N23, N24, N25, N26, A1, A3, A8)_

- [ ] 18. C6 [COORD]: M3 gate per review routing v2: Linear Loop gate review plus Kiro execution checks (A1, A3, merged tree); both must ACCEPT
  - _Requirements: 9.1, 9.2 (ACC: A1, A3, A8)_

- [ ] 19. Optional runs booked in m3-plan
  - [ ] 19.1 m3-a1: A1 speed pass, dispatched only if an idle suite exceeds 560 s
  - [ ] 19.2 m3-r6v: vibe `parse()`, only after C4 and a separate C2v consent (D7)
  - _Requirements: 9.1, 6.1 (ACC: A1, N24)_
