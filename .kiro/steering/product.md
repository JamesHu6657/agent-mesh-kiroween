---
inclusion: always
---

# Product: Agent Mesh v1

Consolidated on 2026-11-09 from docs/requirements.md, docs/plan.md, docs/HANDOFF.md, docs/QUEST-HANDOFF.md and docs/coordination/log.md. Those files are authoritative; this page only summarises them.

## What it is
Agent Mesh is a local coordinator for CLI coding agents on one Linux machine (requirements §0). The v1 surface is a CLI (`bin/mesh`) plus a local programmatic interface over a Unix socket; there is no web UI and no network listener (requirements §0, plan §4, §18.1). It is a fresh implementation: no code from earlier AgentMesh repositories (requirements header, plan §0 rule 1).

## Who uses it
- **The user (James)** holds the only passphrase and approves important decisions: spending, deleting, deploying, major scope changes (requirements §3, plan §7.4).
- **A lead agent per project** decomposes work, assigns executors directly (no claim market), accepts or rejects deliverables, merges, and completes the project against its acceptance criteria (requirements §1, plan §6, §8).
- **Executors** are either launched by Mesh as headless CLI processes or notified through their inbox (requirements §2, plan §12.3).
- **A relayer** (a helper agent in the plan examples) may carry relayed user decisions, which Mesh labels `relayed (unverified)` (plan §7.5).

## Core behaviour
- Projects, tasks, runs, leases, approvals and the outbox are state machines with one event per transition (plan §5–§6).
- Approvals bind to one operation hash, expire, and are audited; an executor saying "the user approved" never counts (plan §7.4).
- Models, efforts, permission profiles and concurrency are user policy; the lead chooses only within the allowed lists (requirements §4, plan §9).
- Every task has its own directory; code tasks get their own git worktree and branch; the lead merges after acceptance and tests (requirements §5, plan §10). Mesh never auto-commits, pushes, rebases or forces (plan §10.4–§10.6).
- Failures are recorded and the lead decides retry, reassign or escalate; there is no automatic retry (requirements §6, plan §11.6).

## Honesty rules
- Adapter status words are evidence, not marketing: `verified`, `partially_verified`, `not_installed`, `unsupported` (requirements §2, plan §14.1).
- "Mesh is not a security sandbox" and "Mesh does not sandbox external CLIs" (plan §7.6, §7.7).
- The `fake` adapter is a test double, never vendor support (plan §14.9).

## Milestones (plan §16) and state on 2026-11-09 (docs/coordination/log.md; merge state rechecked against `git log main` at b8d102c)
- **M1 Core** (gates M1a, M1b, M1c): passed; M1c gate passed 2026-11-09 08:05 UTC+8 after James accepted the A9 first-commit deviation.
- **M2 Git workspaces, merge, lead turns**: runs r1–r12 merged on main (m2-r12 at cb278d2). M2 gate open: Kiro execution checks ACCEPT (docs/coordination/reviews/m2-gate-kiro.md), Linear Loop gate review REVISE (docs/coordination/reviews/m2-gate-linear.md).
- **M3 Real adapters**, cut to **M3-lite** with droid 1.6.0 as the gate adapter (docs/coordination/plans/m3-plan.md, decisions of 2026-11-09 12:44/12:45): probe, smoke harness, evidence gate, the C2 droid capture, m3-r4 (bd0929e) and m3-r9/r9b/r9c (0a11c6c, 1fbfe9a, b8d102c) are on main; m3-r6 onward is open.
- **M4** (Hermes/OpenClaw if installed, custom adapter, notify hook) and **M5** (OS-level hardening, ACP sessions, web UI) are later.

Specs: `.kiro/specs/m1-core/`, `.kiro/specs/m2-git-workspaces-lead-turns/`, `.kiro/specs/m3-lite-real-adapters/`.
