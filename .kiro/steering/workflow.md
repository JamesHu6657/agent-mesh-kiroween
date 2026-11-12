---
inclusion: always
---

> **Public snapshot note (2026-11-13).** This file records how the project was built. It cites private build records (`docs/HANDOFF.md`, `docs/QUEST-HANDOFF.md`, `docs/coordination/`, `logs/`) that are not included in the public repository. Final status: M1, M2 and M3-lite were accepted, and M4 (polish: Kiro launch enabled, N13 real end-to-end run, README, MIT license) is done. The M3 gate was closed by a full external review (ACCEPT, 2026-11-12) instead of the Linear Loop gate planned below, and a pre-public review (2026-11-12) led to five P2 fixes merged before publication. Unchecked items below were either completed under a different route or deferred.


# Workflow rules (Agent Mesh v1 build)

Consolidated on 2026-11-09 from docs/HANDOFF.md (§5, §6, §11), docs/QUEST-HANDOFF.md (all dated sections), docs/coordination/log.md, docs/coordination/plans/{m1c-plan,m2-plan,m3-plan}.md (common blocks), scripts/coord/{merge.sh,push-review.sh,sync-github.sh,session-budget.txt}, scripts/test.sh and .gitignore. Where this page and those files differ, the files win (QUEST-HANDOFF header: HANDOFF.md is authoritative).

## 1. Safety gates: stop and ask James
James approves, and no agent may proceed on another agent's say-so (HANDOFF §5, QUEST-HANDOFF §1 "Not delegated"):
- spending money, purchases, top-ups or plan changes; real model quota for M3 smoke and e2e runs (consent is recorded per step in m3-plan decision D2);
- deleting user data; deploying;
- pushes and remotes outside the standing flows below; making the GitHub repo public (QUEST-HANDOFF "Auto-sync", "Publication checklist" item 3);
- account or credential changes; permission widening of any agent (log 2026-11-08 09:15: the allow-list widening was approved by James);
- major scope changes;
- history rewrites. The M1c A9 first-commit deviation was settled without rewriting history, and only after James chose an option (log 2026-11-09 03:45 and 08:05).

Standing James-approved flows, nothing beyond them:
- `bash scripts/coord/sync-github.sh` after a green merge into main: fast-forward only, main only, no force, gitleaks plus pattern scan of the new commits (QUEST-HANDOFF "Auto-sync", 2026-11-09 19:46).
- `bash scripts/coord/push-review.sh <run>` pushes `review/<run>` for a PR review, with the same scans and no force (QUEST-HANDOFF "PR review flow", 2026-11-09 20:23).

## 2. Who may commit
- **Mesh itself never auto-commits** in task worktrees; it creates only the merge commit (plan §10.4, §10.5; m3-plan D3).
- Kiro profiles never commit, push or dispatch: `mesh-reviewer` writes only `docs/coordination/{reviews,plans}/**` (HANDOFF §11.1); `mesh-specwriter` writes only `.kiro/specs/**`, `.kiro/steering/**`, `.kiro/hooks/*.kiro.hook` and `docs/kiro-usage-log.md` (`fs_write.allowedPaths` in `.kiro/agents/mesh-specwriter.json`).
- Hook scripts (`.kiro/hooks/scripts/`) and agent configs (`.kiro/agents/`) change only by the coordinator, with review. Agents never edit their own permissions (`fs_write.deniedPaths` in `.kiro/agents/mesh-specwriter.json`; review docs/coordination/reviews/linear-kiro-scaffold.md, MAJOR on mesh-specwriter.json; fix commit 54f68e5).
- Coders commit only in their own worktree `mesh-wt-<run>` on branch `wt/<run>`, one commit per sub-step (m2-plan and m3-plan common blocks). Quest alone creates worktrees and merges (QUEST-HANDOFF, 2026-11-08 13:29).

## 3. Review routing v2 (QUEST-HANDOFF, 2026-11-09 21:51)
- **Per run:** every run goes through a PR (`push-review.sh`). The **Linear Agent** chat review is the review of record, saved verbatim as `docs/coordination/reviews/linear-<run>.md` with a first line `VERDICT: ACCEPT|REVISE`. Quest may add a sanity check (suite result, mutation check); that is not the review of record.
- **Gates (M2, M3, final):** a **Linear Loop gate review** (JAM issue plus the `gate-review` label; each label add is one paid run, so it is used only at gates) **and** **Kiro execution checks**: A1 suite time, A3 tripwires, merged-tree integration. Both must ACCEPT; files `<ms>-gate-linear.md` and `<ms>-gate-kiro.md`.
- Design plans are drafted in ChatPRD and reviewed by the Linear Agent chat; Kiro is used only for gate execution checks (QUEST-HANDOFF, 2026-11-09 22:35).

## 4. Merging (QUEST-HANDOFF "Merge hard check", 2026-11-09 21:40)
- Merge into main only with `bash scripts/coord/merge.sh <run> <review-name> "<msg>"`. It refuses unless `docs/coordination/reviews/<review-name>.md` is committed and its first line is `VERDICT: ACCEPT`.
- After each merge: full `bash scripts/test.sh` on main, then `sync-github.sh`, then log the sha in docs/coordination/log.md.
- One branch at a time; at most 2 concurrent coders, one per worktree, only for disjoint files (QUEST-HANDOFF, 2026-11-08 13:29; `scripts/coord/coder-guard.sh`).
- Recorded slip: m3-r9 was merged (0a11c6c) before its review; a post-merge Linear review was requested (QUEST-HANDOFF).

## 5. Short coder sessions (QUEST-HANDOFF, 2026-11-09 18:49; scripts/coord/session-budget.txt)
- Size each session to about 100–200 turns. Near about 150 turns, stop at a clean green point, commit, append `## Session handoff` (Done with shas / Remaining / Gotchas) to the run notes file, and print `HANDOFF WRITTEN`.
- The next session (`<run>-s2`, …) gets the original prompt plus "continue from the Session handoff section".
- `dispatch-vibe.sh` caps vibe at `--max-turns 200` and `--time-limit 5400`; `dispatch-droid.sh` uses a 5400 s timeout.

## 6. Secret hygiene
- Run gitleaks before any push: `sync-github.sh` and `push-review.sh` refuse on any hit. The baseline `scripts/coord/gitleaks-baseline.json` allowlists only the known fake test fixtures (QUEST-HANDOFF "Auto-sync").
- Before a commit: `bash .kiro/hooks/scripts/secret-scan.sh` (staged changes). For the Kiro config: `bash .kiro/hooks/scripts/secret-scan.sh --dir .kiro`.
- Nothing under `.kiro/` is git-ignored (Kiroween rule): `.gitignore` ends with `!.kiro/` and `!.kiro/**`, which override the generic rules such as `**/settings.local.json` and `id_*`. So never store a secret anywhere under `.kiro/`, including `.kiro/settings/`, and do not rely on ignore rules to keep one out of git. Other agent settings dirs (`.claude/`, `.vibe/`, `.factory/`, …) stay ignored (.gitignore; QUEST-HANDOFF "Publication checklist" item 2).
- Never print, log or commit tokens, passphrases or API keys (HANDOFF §5). Never read vendor credential dirs (plan §0 rule 4).
- Test secrets are fakes, e.g. `FAKE_SECRET_API_KEY` in checklist C7 and the fake agent's `print_secrets` behaviour (plan §14.9).

## 7. Test discipline
- `bash scripts/test.sh` must exit 0 with both tripwire files empty before every commit that touches code or tests (m2/m3 common blocks; checklist A3).
- A1: the suite runs in under 600 s. Measure it on an idle box (review m1c-run2 finding 6). The M3 plan books a speed pass (m3-a1) if an idle suite exceeds 560 s.
- Tests use `/tmp/mh-*` homes and Unix sockets only, and clean up only what they created. Never kill by name or pattern (m1c-plan common block; plan §5.1, §17.1).
- No fixed sleeps: use testing-mode settings and poll with `wait_for`. Mark `@serial` only what truly depends on wall-clock timing (m2-plan, m3-plan common blocks).
- **Never weaken or delete an assertion.** The only assertions a run may change are the supersessions its plan section names, each listed with its reason in the run notes (m2-plan, m3-plan common blocks).
- New tests must fail on the old code. Reviews check this ("Fails on old code" column in review m3-r5b; prompts/droid-m2-r11c.md item 7; review m1b-step6i).
- Every covered checklist item gets a test with `ACC: <ID>` that drives the real `bin/mesh` path where the item is about CLI behaviour (common blocks).
- The default suite never starts a real vendor CLI. Probe and smoke tiers are opt-in and coordinator-only (plan §14.10; m3-plan common block rule 4).
- If `test_fixture_hashes` fails, STOP and report. Never edit fixtures or expected hashes (plan §14.11).

## 8. Never-edit list (coder prompts; m3-plan common block rule 1)
`docs/requirements.md`, `docs/plan.md`, `docs/acceptance-checklist.md`, `docs/review-*.md`, `docs/reviews/**`, `docs/coordination/**`, `prompts/**`, `scripts/coord/**`, `.kiro/**`, `docs/HANDOFF.md`, `docs/QUEST-HANDOFF.md`, `docs/release-requirements.md`, `docs/m1*-*.md`, `docs/impl-notes-m1*.md`, `logs/**`, `tests/fixtures/real/**`; M3 additions: `docs/impl-notes-m2*.md`, `docs/m2-gate-report.md`, `tests/smoke/results/**`. Only the coordinator adds files under `tests/fixtures/real/` and `tests/smoke/results/`; existing files there are never modified.

The Kiro spec writer additionally never edits product code, tests, `prompts/`, `scripts/` or anything under `docs/coordination/` (its agent prompt in `.kiro/agents/mesh-specwriter.json`).

## Agent permissions are soft guards
Kiro agent profiles (`.kiro/agents/*.json`: allowed paths, shell allowlists, denied commands) limit mistakes; they are not a sandbox. An agent that can write tests and run them can execute anything it writes. Never treat a profile as a security boundary. The real boundaries are: the coordinator reads the diff and makes every commit; the Linear review of record on the PR; `scripts/coord/merge.sh` and a green full suite on main. Never hand sensitive credentials (secrets, money, deploy or live-trading keys) to agent sessions; profiles do not guarantee credential isolation.
