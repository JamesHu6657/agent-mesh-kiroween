# Design Document: M1 Core

Consolidated on 2026-11-09 from docs/plan.md (§3–§17), docs/impl-notes-m1a.md, docs/impl-notes-m1b.md, docs/impl-notes-m1c*.md, docs/m1c-gate-report.md and the module list on main (853548f); the milestone was originally planned in docs/plan.md §16 and docs/coordination/plans/m1c-plan.md, and built by the coder rotation recorded in docs/coordination/log.md.

This is a summary with pointers. The normative text is docs/plan.md; where code and plan differ, the recorded readings are in the impl-notes files and the gate reports.

## Overview
One daemon (`meshd`) per `MESH_HOME` is the only writer of `mesh.db`. A thin CLI talks to it over a Unix socket. Handlers run inside `BEGIN IMMEDIATE` transactions and return effects (spawn, kill, wake), which run after commit and can be re-derived from DB state after a crash (plan §4).

## Architecture
```
mesh CLI (bin/mesh -> mesh.cli.main) --NDJSON over $MESH_HOME/run/mesh.sock--> meshd
meshd: server (auth, authz, idempotency, dry-run) -> handlers -> SQLite tx -> effects
       scheduler tick (promote, start, timeouts, leases, outbox, expiry)
       supervisor -> runwrap -> agent CLI (fake in M1)
       recovery at boot
```
- Single-instance flock on `run/meshd.lock`; a `boots` row per start; `PR_SET_CHILD_SUBREAPER` so escaped descendants reparent to meshd (plan §4). The constant was wrong (34 instead of 36) until M1c run 2a fixed it (log 2026-11-08 16:30).
- On SIGTERM meshd stops without killing runs; the next boot adopts them (plan §4, §11.7; checklist I8).

## Components
| Area | Modules | Plan |
|---|---|---|
| CLI and help | `mesh/cli/main.py`, `mesh/cli/help.py`, `mesh/cli/commands/*.py`, `mesh/client.py`, `mesh/errors.py`, `mesh/constants.py` | §13 |
| Storage | `mesh/core/db.py`, `schema.py`, `ids.py`, `clock.py`, `canonical.py`, `init.py` | §5 |
| States and invariants | `mesh/core/states.py`, `mesh/core/invariants.py` | §5.6, §6 |
| Identity and authority | `mesh/core/auth.py`, `authz.py`, `approvals.py`, `policy.py`, `settings.py`, `paths.py` | §7, §9, §5.7 |
| Events and notifications | `mesh/core/events.py`, `mesh/daemon/handlers/{event,inbox}.py` | §12 |
| Daemon | `mesh/daemon/server.py`, `mesh/daemon/handlers/*.py`, `mesh/daemon/scheduler.py` | §4, §9.3 |
| Supervision | `mesh/daemon/supervisor.py`, `mesh/runwrap.py`, `mesh/daemon/terminate.py`, `mesh/daemon/children.py`, `mesh/core/proc.py`, `mesh/core/leases.py` | §11.1–§11.5 |
| Recovery | `mesh/daemon/recovery.py` | §11.7 |
| Prompts, redaction, workspaces | `mesh/core/prompts.py`, `mesh/core/redact.py`, `mesh/core/workspaces.py`, `mesh/core/gitops.py` | §12.5, §15.2, §10.1, §10.5 |
| Adapters | `mesh/adapters/base.py`, `registry.py`, vendor modules, `none_adapter.py`, `fake.py`, `mesh/testing/fake_agent.py` | §14 |

## Data model
The authoritative DDL is plan §5.5 (all tables `STRICT`): `principals`, `credentials`, `agents`, `adapter_probes`, `projects`, `acceptance_criteria`, `project_deps`, `lead_assignments`, `tasks`, `task_deps`, `runs`, `leases`, `approvals`, `events` (append-only triggers, hash chain), `outbox` (unique `dedup_key`), `idempotency`, `settings`, `boots`, `meta`. Invariants I1–I8 are checked by `mesh doctor --check-invariants` and by every integration teardown (plan §5.6); I9 arrives with M2.

Recorded deviation: outbox dedup keys carry a version discriminator so repeated T19–T21 transitions are not dropped (log 2026-11-08 02:55, deviation from plan §12.2).

## Error handling
- Every error is a `MeshError` with an UPPER_SNAKE `error.code` and one exit code from plan §13.2; lowercase words (`timeout`, `lease_expired`, …) are failure reasons, never codes.
- Stale writes: no lease means rc 9 `LEASE_EXPIRED`; a lease with another fence or run means rc 9 `FENCE_MISMATCH` (plan §11.5; `mesh/core/leases.py`).
- Termination keeps the lease until the process group, the session and every `MESH_RUN_ID` carrier are gone; failures emit `run.kill_failed` and retry (plan §11.4).
- Recovery steps run one transaction each with deterministic dedup keys, so a second recovery sends no duplicate message (plan §11.7; checklist J3).

## Testing strategy
- Default suite `bash scripts/test.sh`: unit (`python3 -m unittest discover -s tests/unit -t .`), integration and recovery via the parallel runner `tests/run_tier.py` (added in M1c run 1 for the A1 budget, m1c-plan Q1), with tripwire shims in `tests/shims/`.
- `tests/helpers.py` `MeshHarness`: `/tmp/mh-*` home, testing-mode daemon, `cli()`, `wait_for`, `kill9_daemon()`, `restart_daemon()`, teardown with `doctor --check-invariants` and cleanup of processes carrying this home's run ids (plan §17.1; m1c-plan runs 2 and 4).
- Every checklist item has a test tagged `ACC: <ID>`; the M1c gate report lists them per item (docs/m1c-gate-report.md, 163 items, 162 PASS plus A9 coordinator-verified).
- Representative tests: `test_invariants_valid_states` (A10), `test_gitops_allowlist` (A6), `test_fixture_hashes` (N15), `test_adapter_flags_in_fixtures` (N15), `test_prompt_fallback` (N16), `test_stdlib_only` (A2), `test_no_network_code` (C2), `test_test_runner_script` (A1).
- Measured at the M1c gate: 172 unit, 205 integration, 15 recovery tests, wall 7m38s on HEAD 6118183 (docs/m1c-gate-report.md §3).
