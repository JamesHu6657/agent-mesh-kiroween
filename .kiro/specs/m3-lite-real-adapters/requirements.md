# Requirements Document: M3-lite Real adapters (gate adapter droid 1.6.0)

Consolidated on 2026-11-09 from docs/requirements.md §2, docs/plan.md (§14, §16 M3), docs/acceptance-checklist.md (N1, N4, N6, N9–N14), docs/coordination/m3-acceptance-addendum.md (N18–N27), docs/coordination/plans/m3-plan.md and docs/coordination/log.md; the milestone was originally planned in docs/plan.md §16 M3, docs/coordination/plans/m3-plan-draft.md (ChatPRD) and docs/coordination/plans/m3-plan.md (Quest revision after Kiro review docs/coordination/reviews/plan-m3.md), and built by the coder rotation recorded in docs/coordination/log.md.

The checklist and the addendum are authoritative; `[ACC: …]` names their rows. N18–N27 exist only in the addendum (coordinator-owned).

## Introduction
Plan §16 M3 enables real launches for droid, agy, kiro and vibe from probe and smoke evidence. James cut it to **M3-lite** on 2026-11-09 12:44/12:45 (m3-plan "Decisions"): droid 1.6.0 is the only gate adapter; kiro and agy are probe-only; vibe is considered only after C4; vendor lead turns stay off; N11 is a scoped pass for droid; the C5/N26 code-task demo is an optional stretch. Hermes and OpenClaw stay `not_installed` (N14 is M4).

## Requirements

### Requirement 1: Read-only probe tier and help drift (m3-r1, C1)
**User Story:** As the coordinator, I want every flag Mesh emits checked against the installed CLI's help, so that a vendor update cannot silently break argv.

#### Acceptance Criteria
1. WHEN the opt-in probe tier runs THE SYSTEM SHALL confirm that every flag Mesh emits for droid, agy, kiro-cli and vibe appears in the current `--help`, store the versions in `adapter_probes`, and report any sha256 difference from plan §14.11 as a flag diff. [ACC: N10]
2. WHEN a probed help differs from the committed fixture THE SYSTEM SHALL record droid 1.6.0 and agy 1.5.3 captures in new version dirs and print the flag diff; old-version rows SHALL read `stale`. [ACC: N22]
3. WHEN a capture is written or promoted THE SYSTEM SHALL write a sidecar with exactly `{command, captured_at_utc, sha256}`, refuse existing paths and `synthetic_*` names, and add files only (no edits) under `tests/fixtures/real/` and `tests/smoke/results/`; `test_fixture_hashes` SHALL stay unchanged and passing. [ACC: N21, N15]

### Requirement 2: Safe smoke harness (m3-r2)
**User Story:** As James, I want real-quota runs to happen only with my explicit consent and a hard run cap, so that tests never spend quota by accident.

#### Acceptance Criteria
1. WHEN `MESH_SMOKE` is missing or not equal to `1`, or the adapter is missing from `MESH_SMOKE_ADAPTERS` or from the consent list `MESH_SMOKE_CONSENT`, THE SYSTEM SHALL skip every smoke test and start no vendor process (gate in `tests/smoke/harness.py`). [ACC: N25]
2. WHEN the smoke harness runs THE SYSTEM SHALL require `MESH_SMOKE_MAX_RUNS`, refuse model `auto` and non-absolute binaries, write only under `MESH_SMOKE_OUT` (`/tmp/mh-*`), never use a cwd in the repo, and redact secrets. [ACC: N25, N21]
3. THE SYSTEM's offline tests SHALL never set `MESH_SMOKE*` in `os.environ`, SHALL use stub binaries under `/tmp/mh-`, and SHALL leave the A3 tripwire empty. [ACC: N25, A3]

### Requirement 3: Droid smoke cases and offline re-evaluation (m3-r3, C2, C3)
**User Story:** As the coordinator, I want the droid capabilities Mesh relies on proven by recorded real runs, so that the status words come from evidence.

#### Acceptance Criteria
1. WHEN the consented C2 capture runs THE SYSTEM SHALL write a results file `tests/smoke/results/<adapter>-<version>-<date>.json` with each case S-X-1…7 and S-DROID-8, and kiro, agy and vibe cases as `not_run(deferred_m3_lite)`. [ACC: N11]
2. WHEN the parser changes THE SYSTEM SHALL re-evaluate output-derived cases offline from the committed fixtures, carry the others unchanged, and never mark a capability verified from a `synthetic_*` fixture. [ACC: N12]

### Requirement 4: Evidence-driven launch (m3-r5)
**User Story:** As the user, I want a vendor adapter to launch only when committed evidence matches the installed binary, so that "launch enabled" is never a hand-set flag.

#### Acceptance Criteria
1. WHEN a task is created, assigned, reassigned or retried on a vendor agent THE SYSTEM SHALL allow launch only if the latest probe row for the agent's binary has version V, the latest results file has version V, `validate()` is clean, and S-X-1 and S-X-6 pass; otherwise it SHALL fail rc 13 `ADAPTER_UNSUPPORTED` with `details.milestone:"M3"` and `details.reason` one of `no_probe_version`, `no_smoke_evidence`, `evidence_stale`, `evidence_invalid`, `launch_cases_failed`, without running any subprocess. [ACC: N19]
2. WHEN `adapter show` or `adapter list` runs THE SYSTEM SHALL print per capability `verification`, `verified_at`, `verified_version` and `results_file` from the latest results file, `stale` on a version mismatch, status `verified` only if every capability with `supported: yes` has verification `smoke` (otherwise `partially_verified`; mesh/adapters/evidence.py), and `launch_enabled`, `launch_milestone`, `launch_reason`. [ACC: N20]
3. THE SYSTEM's N1 test SHALL assert `launch_enabled == effective_launch(...)` over the daemon's results directory, and the N4 test SHALL keep its agy assertions plus `details.reason:"no_smoke_evidence"`; N1 is recorded as superseded by N23. [ACC: N23, N1, N4]
4. WHILE not in testing mode THE SYSTEM SHALL ignore the testing override of the results directory. [ACC: N19]

### Requirement 5: Adopted-wrapper residual, run options, vendor lead gate (m3-r4)
**User Story:** As the user, I want an adopted run's leftover processes tracked after its wrapper exits, so that no vendor launch can leave an unseen process holding a slot.

#### Acceptance Criteria
1. WHEN an adopted run's wrapper exits while a SIGTERM-ignoring, env-less member of its group lives THE SYSTEM SHALL keep the run open and the `task:<id>` lease held, including through the grace window, then SIGKILL the member, release the lease, list it in `seen.json`, and pass `doctor --check-invariants`. [ACC: N18, A8]
2. WHEN a vendor lead is due for a turn in M3-lite THE SYSTEM SHALL start no turn and keep the messages pending. [ACC: N19]

### Requirement 6: Real droid output parsing (m3-r6)
**User Story:** As a lead, I want the droid deliverable taken from droid's real stream-json, so that a submitted task carries the actual final answer.

#### Acceptance Criteria
1. WHEN `parse()` reads the real droid S-X-1 fixture THE SYSTEM SHALL return `final_text` containing `MESH_SMOKE_OK` and the init event's `session_id`; the S-X-3 fixture SHALL contain the token; `exec-init-event-opus55-high.json` SHALL still give its session id; synthetic robustness fixtures SHALL parse without exception and set no verification. [ACC: N24, N6, N12]

### Requirement 7: Real end-to-end launch (m3-r7a, C4, optional C5)
**User Story:** As James, I want one real Mesh task completed by droid, so that launch mode is proven with a vendor CLI and not only with the fake.

#### Acceptance Criteria
1. WHEN the consented C4 run executes a readonly task "Reply with exactly MESH_SMOKE_OK" on droid with the cheapest allowed model THE SYSTEM SHALL reach `submitted` with the token in a parsed deliverable and the model flag in the recorded argv. [ACC: N13]
2. WHERE the optional C5 demo runs, WHEN droid with profile `dev` commits `hello.txt` in a no-remote fixture repo THE SYSTEM SHALL submit with `git.dirty:false`, merge after the test step, produce a two-parent merge commit on integration, and push nothing; if skipped the gate report SHALL say "not run". [ACC: N26]

### Requirement 8: Docs and gate report from evidence (m3-r7b, m3-r8)
**User Story:** As a reader of the docs, I want adapter statuses copied from results files, so that the docs never claim more than was tested.

#### Acceptance Criteria
1. THE SYSTEM's `docs/adapters.md` SHALL give each of the six CLIs one status word, and every `verified` capability SHALL have a passing smoke case and a real fixture. [ACC: N9, N12]
2. THE SYSTEM's `docs/m3-gate-report.md` SHALL list each CLI's status word, `launch_enabled` with reason, results file or "not run", each droid case, the deferred list, the N26 result, and the Factory and Kiro credits spent. [ACC: N27]

### Requirement 9: Regression at the M3 gate
**User Story:** As the coordinator, I want M3 to keep every earlier guarantee, so that real adapters do not weaken the default suite.

#### Acceptance Criteria
1. THE SYSTEM's `bash scripts/test.sh` SHALL stay vendor-free, pass in under 10 minutes with empty tripwires, and never include the probe or smoke tiers. [ACC: A1, A3]
2. THE SYSTEM's `doctor --check-invariants` SHALL stay clean after every integration test. [ACC: A8]
