---
inclusion: fileMatch
fileMatchPattern: "**/*.py"
---

# Repository structure

Consolidated on 2026-11-09 from docs/plan.md §3 and the files present on main (853548f). Plan §3 is the intended layout; the list below is what exists.

## Product code (`mesh/`)
| Path | Role |
|---|---|
| `bin/mesh` | POSIX sh shim: `python3 -P -s -m mesh` |
| `mesh/cli/main.py`, `mesh/cli/help.py`, `mesh/cli/commands/*.py` | argparse tree, envelope, layered help; one module per resource |
| `mesh/client.py` | Unix-socket client |
| `mesh/errors.py`, `mesh/constants.py` | `MeshError(code, exit_code, …)`, shared constants |
| `mesh/core/db.py`, `schema.py`, `ids.py`, `clock.py`, `canonical.py` | storage, migrations, ids, time, canonical JSON and op hashes |
| `mesh/core/states.py` | transition tables (single source of truth, plan §6) |
| `mesh/core/auth.py`, `authz.py`, `approvals.py`, `policy.py`, `settings.py` | credentials, authorization matrix, approvals, model/concurrency policy, settings registry |
| `mesh/core/events.py`, `invariants.py`, `redact.py`, `proc.py`, `paths.py`, `init.py` | event log and outbox, I1–I9, redaction, `/proc` helpers, install-path checks, `mesh init` |
| `mesh/core/gitops.py`, `repos.py`, `workspaces.py`, `leases.py` | the only git caller, repo/integration setup, task dirs and worktrees, fencing |
| `mesh/core/prompts.py`, `lead_prompt.py`, `lead_actions.py` | executor prompt and deliverable parsing, lead prompt with size cap, `mesh-actions` parser |
| `mesh/daemon/server.py`, `handlers/*.py` | socket server, auth, dispatch; one handler module per resource |
| `mesh/daemon/scheduler.py`, `supervisor.py`, `terminate.py`, `children.py`, `recovery.py` | tick loop, spawn/watch, §11.4 termination, child registry and reaper, boot recovery |
| `mesh/daemon/merge.py`, `lead_turns.py` | merge pipeline (M2), lead turns (M2) |
| `mesh/runwrap.py` | per-run wrapper process |
| `mesh/adapters/base.py`, `registry.py`, `{droid,agy,kiro,vibe,hermes,openclaw}.py`, `none_adapter.py`, `fake.py` | adapter interface and implementations |
| `mesh/adapters/helpdiff.py`, `smoke_schema.py`, `evidence.py` | M3 help diff, smoke results schema, evidence-driven launch |
| `mesh/testing/fake_agent.py` | the fake CLI behind the `fake` adapter (test double) |

## Tests (`tests/`)
- `tests/unit/` (pure), `tests/integration/` (harness + daemon), `tests/recovery/` (kill -9 and restart) form the default suite run by `scripts/test.sh`.
- `tests/probe/`, `tests/smoke/` are opt-in tiers; `tests/smoke/harness.py`, `tests/smoke/reevaluate.py`, `tests/tierlib.py` support them.
- `tests/helpers.py` (`MeshHarness`, `serial`), `tests/gitfixture.py`, `tests/stubs.py`, `tests/run_tier.py`, `tests/shims/` (tripwire stubs named after the vendor CLIs).
- `tests/fixtures/real/` holds real CLI captures (coordinator-only, never edited); `tests/smoke/results/` holds smoke results (coordinator-only).

## Docs and coordination
- Authoritative, never edited by coders: `docs/requirements.md`, `docs/plan.md`, `docs/acceptance-checklist.md`, `docs/coordination/m3-acceptance-addendum.md`.
- Gate reports `docs/m1a-gate-report.md`, `docs/m1b-gate-report.md`, `docs/m1c-gate-report.md`, `docs/m2-gate-report.md` (on main since cb278d2); per-run notes `docs/impl-notes-*.md`; user docs `docs/usage.md`, `docs/adapters.md`.
- `docs/coordination/`: `log.md` (decisions, UTC+8), `plans/`, `reviews/` (review of record per run), coordinator-owned.
- `scripts/test.sh`; coordinator tooling in `scripts/coord/` (dispatch, worktree, merge, push-review, sync-github, gitleaks baseline).

## Where new code goes
Follow the run plan's FILES list for the run you are in (docs/coordination/plans/*.md). A file outside that list, or on the never-edit list in `.kiro/steering/workflow.md`, means STOP and report.
