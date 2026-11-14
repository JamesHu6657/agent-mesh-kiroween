# Design Document: M3-lite Real adapters

Consolidated on 2026-11-09 from docs/plan.md §14, docs/coordination/plans/m3-plan.md (planner notes, runs, Parts 2–5), docs/coordination/m3-acceptance-addendum.md, docs/impl-notes-m3-r{1,2,3,4,5,9}.md, docs/coordination/log.md and the files on main (853548f; merge state rechecked against `git log main` at b8d102c); the milestone was originally planned in docs/plan.md §16 M3, docs/coordination/plans/m3-plan-draft.md and docs/coordination/plans/m3-plan.md, and built by the coder rotation recorded in docs/coordination/log.md.

A summary with pointers; m3-plan.md has the full run prompts. Files marked "(planned)" do not exist on main yet.

## Overview
Launch for a vendor adapter is decided by committed evidence, never by a hand-set attribute: a read-only probe row for the agent's binary, a results file for the same version, validated real fixtures, and passing launch cases S-X-1 and S-X-6 (m3-plan Q2; addendum N19). The class attribute `launch_enabled` stays `False` on every vendor adapter (m3-plan "State today"). Evidence is produced by two opt-in tiers that only the coordinator runs: the probe tier (no model quota) and the smoke tier (real quota, consent per step).

## Architecture
```
C1  probe tier  (tests/probe/test_probe.py, MESH_PROBE=1)  -> staging /tmp/mh-probe-*  -> tierlib promote -> tests/fixtures/real/<a>/<v>/ + .meta.json
C2  smoke tier  (tests/smoke/test_smoke.py via harness.py) -> staging /tmp/mh-smoke-*  -> tierlib promote -> tests/fixtures/real/droid/1.6.0/ + tests/smoke/results/
C3  reevaluate  (tests/smoke/reevaluate.py, offline)       -> new results file with evaluated_from / carried_from
runtime         mesh/adapters/evidence.py effective_launch(conn, adapter, testing_mode, binary_path)
                  <- latest adapter_probes row for the binary, latest results doc, smoke_schema.validate()
                -> AG step 4 in mesh/daemon/handlers/task.py; adapter list/show/probe output
C4  e2e         (tests/smoke/test_e2e.py) (planned)        -> one real Mesh task on droid readonly
```

## Components
| Component | Files | Run | State on main |
|---|---|---|---|
| Help flag diff | `mesh/adapters/helpdiff.py` | m3-r1 | merged (de8b6b9) |
| Capture writer and promotion | `tests/tierlib.py` | m3-r1 | merged |
| Probe tier, shim-free harness option | `tests/probe/test_probe.py`, `tests/helpers.py` (`use_shims=False`) | m3-r1 | merged |
| Results schema, case-to-capability map | `mesh/adapters/smoke_schema.py` (`SCHEMA = "mesh.smoke.v1"`, `CASE_CAPS`, `LAUNCH_CASES`) | m3-r2 | merged |
| Smoke harness | `tests/smoke/harness.py` | m3-r2 | merged |
| Droid cases, re-evaluation | `tests/smoke/test_smoke.py`, `tests/smoke/reevaluate.py` | m3-r3 | merged |
| Evidence gate and evidence output | `mesh/adapters/evidence.py`, `mesh/adapters/base.py`, `mesh/daemon/handlers/adapter.py`, `mesh/cli/commands/adapter.py`, `mesh/daemon/handlers/task.py` (AG step 4) | m3-r5 | merged |
| `refs/heads/<branch>` in merge and repo validation | `mesh/daemon/merge.py`, `mesh/core/repos.py` | m3-r9, r9b, r9c | merged (0a11c6c); r9b merged 1fbfe9a; r9c merged b8d102c |
| Adopted-wrapper seen map, `RunRequest.options`, vendor lead gate | `mesh/daemon/supervisor.py`, `mesh/daemon/terminate.py`, `mesh/daemon/lead_turns.py`, `mesh/testing/fake_agent.py` | m3-r4 (r4..r4e) | merged (bd0929e) |
| Real droid `parse()`; `reasoning_effort` demoted to `help` | `mesh/adapters/droid.py`, `mesh/adapters/kiro.py` line 43 | m3-r6 | open |
| Real e2e and optional code-task demo | `tests/smoke/test_e2e.py` (planned) | m3-r7a | open |
| Docs from results | `docs/adapters.md`, `docs/usage.md`, `tests/unit/test_docs_m3.py` (planned) | m3-r7b | open |
| Gate report | `docs/m3-gate-report.md`, `docs/impl-notes-m3.md` (planned) | m3-r8 | open |

## Data model
- **`adapter_probes`** (plan §5.5): one row per live probe, PK `(adapter, probed_at)`. Evidence reads the newest row whose `binary_path` equals the agent's binary (m3-plan Q10).
- **Results document** `tests/smoke/results/<adapter>-<version>-<UTC date>.json` (collisions `-2`, `-3`; written with O_EXCL): `{schema, adapter, binary, version, captured_at_utc, mesh_commit, model, effort, profile:"readonly", timeout_s, consent_ref, cases:[{id, status, reason, rc, duration_s, fixture, fixture_sha256, observation, evaluated_from, carried_from}]}`; statuses `pass`, `fail`, `observed`, `not_applicable`, `not_run`, `blocked` (m3-plan m3-r2).
- **Sidecar** `<file>.meta.json` with exactly `{command, captured_at_utc, sha256}` for every new real fixture (plan §14.11; addendum N21).
- **`CASE_CAPS`**: S-X-1 → `headless`, `stream_output`; S-X-2 and S-X-3 → `resume`; S-X-4 → `model_selection`; S-X-5 → `reasoning_effort`; S-X-6 is a launch gate; S-X-7 and S-DROID-8 are observations (m3-plan m3-r2).
- **`seen.json`** in the run dir: members of an adopted run's group and session as (pid, starttime), written by m3-r4 (format in docs/impl-notes-m3-r4.md, on main since bd0929e).

## Error handling
- Evidence failures return rc 13 `ADAPTER_UNSUPPORTED` with `details.milestone:"M3"` and one `details.reason` (addendum N19). Evaluation never executes a binary.
- A task queued before its evidence went stale can still start, because T5 does not re-check (m3-plan m3-r5 sub-step 1, documented limit).
- The harness kills a hanging case: SIGTERM to the group, `grace_s`, SIGKILL, `observation:"hang"`; it polls and never sleeps a fixed time (m3-plan m3-r2).
- No single-case retries: a re-capture is a full new C2 in a fresh staging dir, and promotion is one-shot per version (log 2026-11-09 16:45; review m3-r2b-r3 finding 2).
- If no final text can be parsed, S-X-1 stays `fail`, droid stays launch-disabled, N13 is deferred and the gate report says so (m3-plan risk 2).

## Recorded decisions and deviations
- D5: runtime reads `tests/smoke/results` and hashes `tests/fixtures/real` for launch evidence (James, 2026-11-09 12:44).
- D6: the class-declared `reasoning_effort` verification `smoke` is demoted to `help` until an S-X-5 result exists (m3-r6, open).
- The evidence gate also applies at `task reject` → rework for M3-lite; moving it to T13/T5 is M4 work (log 2026-11-09 19:10).
- `set-lead` to a vendor lead returns rc 13 `vendor_lead_m3_lite`; the vendor rule runs first in `lead_launch_check` (log 2026-11-09 20:52, 20:58).

## C2 result on record (log 2026-11-09 23:00; commit 853548f)
droid 1.6.0, model `claude-sonnet-5-5`, readonly. S-X-1 `fail(parse_pending)`, expected until m3-r6; S-X-2 through S-X-6 pass; S-X-7 observed (no file, rc 1); S-DROID-8 observed (fail, rc 1). R1 cost 13,621 Factory credits; R2–R6 about 18k in total. gitleaks was clean.

## Testing strategy
- Offline tests only in the default suite: stub binaries from `tests/stubs.py` under `/tmp/mh-*`, explicit `env` dicts, committed real fixtures read-only (m3-plan common block rule 4).
- Test files by item: N10/N22 `tests/unit/test_helpdiff.py`, `tests/unit/test_probe_offline.py`, `tests/probe/test_probe.py`; N21 `tests/unit/test_tierlib.py`; N11/N12/N25 `tests/unit/test_smoke_harness.py`, `tests/unit/test_smoke_cases.py`, `tests/smoke/test_smoke.py`; N19/N20/N23 `tests/unit/test_adapter_evidence.py`, `tests/integration/test_adapter_evidence.py`; N1 `tests/integration/test_adapters.py`; N4 `test_n4_adapter_launch_disabled` in `tests/integration/test_tasks.py`; K4 regression `tests/integration/test_merge_refs.py`.
- m3-r4 regression tests (on main since bd0929e): N18 `tests/recovery/test_recovery_adopt_seen.py`; vendor lead gate `tests/integration/test_vendor_lead_gate.py`.
- Planned (not on main): `tests/unit/test_adapters_droid_real.py` (planned; m3-r6), `tests/smoke/test_e2e.py` (planned; m3-r7a), `tests/unit/test_docs_m3.py` (planned; m3-r7b).
- Every C step that commits evidence is followed by a full `bash scripts/test.sh` on main (m3-plan merge-order rule 4), because committed evidence changes what the non-testing N1 daemon sees.
