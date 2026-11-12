# Implementation Plan: M1 Core

Consolidated on 2026-11-09 from docs/HANDOFF.md §2, docs/coordination/log.md, `git log` on main (853548f), docs/m1a-gate-report.md, docs/m1b-gate-report.md and docs/m1c-gate-report.md; the milestone was originally planned in docs/plan.md §16 (M1.1–M1.9), docs/coordination/plans/m1b-step7-check.md and docs/coordination/plans/m1c-plan.md, and built by the coder rotation recorded in docs/coordination/log.md.

All M1 steps are merged on main. Run ids and commits come from the log and `git log`; where the log does not name a commit, none is given. Requirement numbers refer to `requirements.md` in this folder.

- [x] 1. Gate M1a Foundation (plan M1.1–M1.4)
  - Built by agy before the coordination log starts; accepted at 9893d7b, 46/46 gate items, independent review F1–F7 fixed (docs/HANDOFF.md §2; docs/m1a-gate-report.md; docs/m1a-fix-report.md).
  - [x] 1.1 M1.1 CLI skeleton, envelope, exit codes, layered help, `scripts/test.sh`
    - _Requirements: 1.1, 1.2, 1.6–1.8, 1.10, 1.11, 9.4 (ACC: A1, B1, B2, B7, B8, B9, B10, B13, A5)_
  - [x] 1.2 M1.2 storage, migrations, ids, canonical JSON, hash chain, `doctor --check-invariants`
    - _Requirements: 2.2, 2.3, 2.6 (ACC: A8, A10, I9)_
  - [x] 1.3 M1.3 daemon, socket, auth, settings registry, idempotency, dry-run, `init`
    - _Requirements: 3.1–3.8, 3.10 (ACC: C1–C6, C9–C14, G17)_
  - [x] 1.4 M1.4 config, agents, tokens, adapter registry with fixture-based golden and parse tests
    - _Requirements: 4.1, 4.3–4.6, 4.8–4.10, 7.8 (ACC: D1, D2, D5, D8, D9, D10, D17, N1, N2, N3, N6, N7, N8, N15, N16, G22, G24, A2, A3, A4, A7)_

- [x] 2. Gate M1b Coordination (plan M1.5–M1.7); gate passed at 1a0c851 (log 2026-11-08 09:05; docs/m1b-gate-report.md)
  - [x] 2.1 M1.5 projects, intake, lead lifecycle: run m1b-step5 (agy), commit 65cab96, verified as progress (log 2026-11-07 ~19:22)
    - _Requirements: 1.4, 1.9, 5.1–5.6, 5.8 (ACC: E1–E7, E10, E12, E13, E14, E18, B11, B12)_
  - [x] 2.2 M1.6 tasks, deps, scheduler, events, outbox, inbox, waits: runs m1b-step6 … m1b-step6i (agy); sub-step (1) 7ca8231; accepted at 8411343 after Kiro review m1b-step6i ACCEPT (log 2026-11-08 04:20)
    - Runs 6, 6b, 6c were cut off by network drops, machine moves and a permission bug (docs/QUEST-HANDOFF.md §3); 6d–6h were REVISE rounds (log 2026-11-07 22:28 to 2026-11-08 03:45).
    - _Requirements: 1.3–1.5, 2.4, 2.5, 4.2, 4.4, 4.7, 4.8, 5.5, 6.1–6.4 (ACC: B3, B4, B5, B6, D3, D7, D12, E8, E9, F3, F5, F6, F7, J2, J4, J8, N4, N5)_
  - [x] 2.3 M1.7 approvals, policies, op hashing, audit: runs m1b-step7, 7b, 7c (agy); 7c commits bba51af, b6b35ea, 1a0c851; Kiro review m1b-step7c ACCEPT (log 2026-11-08 09:05)
    - _Requirements: 3.9, 7.1, 7.2, 7.4–7.7, 7.10 (ACC: G1, G2, G3, G5, G7–G15, G19, G20, G21, G28, D16)_

- [x] 3. Gate M1c Execution (plan M1.8–M1.9); plan docs/coordination/plans/m1c-plan.md (Kiro, 2026-11-08)
  - [x] 3.1 Run 1: parallel runner, review carry-overs, gitops allow-list, inbox execution, accept/reject
    - m1c-run1 (agy): b7655c2, 460f435, 6a05235, e33d10b, dfdb965, 7cf35e7 (log 2026-11-08 11:20); Kiro m1c-run1 REVISE.
    - m1c-run1b and m1c-run1b2 (vibe, DeepSeek): 77ce885, 562e364, c84d6d0; Kiro re-review m1c-run1b ACCEPT (log 2026-11-08 15:50).
    - _Requirements: 2.5, 5.5, 5.6, 6.2, 6.4, 7.3, 7.9, 8.1, 8.3, 8.5, 8.11 (ACC: A6, D14, E11, E15, E16, F5, F7, F11, G4, G16, H4, H6, H7, H15, J3)_
  - [x] 3.2 M1.9 docs (`docs/usage.md`, `docs/adapters.md`)
    - wt/m19-docs (Command Code): 666962e, 98ddb10; Kiro m1c-docs REVISE, m1c-docs2 ACCEPT; merged 895bd22 (log 2026-11-08 15:50).
    - Follow-ups: wt/m1c-notes (vibe) merged 46235cc; wt/m1c-notes2 (vibe, 679b037) merged 17fe2d4 (log 2026-11-08 17:10, 18:00).
    - _Requirements: 9.1–9.3 (ACC: G23, G29, J7, N9, N17)_
  - [x] 3.3 Run 2: harness, redaction, prompts, runwrap, supervisor spawn/watch, run tokens, launch T5, read verbs
    - Run 2a: Command Code and vibe attempts, finished by the Quest coder (f4cac99, 4399155, ab42675); the subreaper constant fix (34 → 36) came in this run (log 2026-11-08 16:30–20:35).
    - Run 2b (Quest coder); Kiro m1c-run2a REVISE, m1c-run2 ACCEPT; merged e8a4d6c (log 2026-11-08 22:35).
    - _Requirements: 2.1, 2.2, 3.11, 6.3–6.5, 7.5, 8.1–8.3, 8.6–8.8 (ACC: C7, C8, D4, D6, D11, D12, D13, D15, F1, F2, F4, F9, G6, G18, H8, H11, H12, J1, J9)_
  - [x] 3.4 Run 3: termination, child registry, reaper, run-session binding, python isolation
    - Quest coder; Kiro m1c-run3 ACCEPT; merged 528f192 (log 2026-11-08 23:45).
    - _Requirements: 8.3, 8.4, 8.8, 8.9, 5.7 (ACC: C15, E17, F8, G25, G26, G27, H1, H2, H3, H5, H13, H16, H17, H18)_
  - [x] 3.5 Runs 4 and 5: crash recovery and recovery tier; remaining failure-policy tests, reconciliation, M1c gate report
    - Quest coder; Kiro m1c-run4 REVISE (folded into run 5); merged 6543281 after Kiro m1c-gate ACCEPT (log 2026-11-09 00:45, 03:45).
    - _Requirements: 6.4, 6.5, 8.3, 8.6, 8.10, 9.4 (ACC: I1–I8, I10, I11, J3, J5, F10, H9, H10, H14, J6, A1)_
  - [x] 3.6 A9 scratch-clone checkouts and gate decision
    - 43d156e was red (ImportError: tests/unit not a package at the first M1.1 commit); eb10bea, 9893d7b, 1a0c851 and e7674c4 were green (log 2026-11-09 03:45).
    - James chose Option A: the first-commit deviation is accepted and recorded, no history rewrite; **M1c gate passed** (log 2026-11-09 08:05).
    - _Requirements: 9.4 (ACC: A9)_
