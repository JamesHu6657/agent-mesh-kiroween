# Requirements Document: M1 Core

Consolidated on 2026-11-09 from docs/requirements.md, docs/plan.md (§5–§17, §16 M1), docs/acceptance-checklist.md (§0.1 and sections A–J, N), docs/m1a-gate-report.md, docs/m1b-gate-report.md, docs/m1c-gate-report.md and docs/HANDOFF.md; the milestone was originally planned in docs/plan.md §16 (M1.1–M1.9, gates M1a/M1b/M1c), docs/acceptance-checklist.md §0.1, docs/coordination/plans/m1b-step7-check.md and docs/coordination/plans/m1c-plan.md, and built by the coder rotation recorded in docs/coordination/log.md (M1a, which predates the log, by agy per docs/HANDOFF.md §2–§3).

This file restates, in EARS form, what the checklist already requires. The checklist row is authoritative for every criterion; `[ACC: …]` names the row(s). P1–P3 are pipeline items outside the gates (checklist §0.1) and are not restated.

## Introduction
M1 is the first implementation phase: CLI, storage, daemon, identity and approvals, projects and leads, the task state machine, supervision and recovery, and docs, using only the `fake` and `none` adapters (plan §16 M1). Leads are exercised as inbox-mode agents; launched lead turns are M2.

## Requirements

### Requirement 1: Agent-friendly CLI (plan M1.1, §13)
**User Story:** As an agent or user driving Mesh from a shell, I want predictable, JSON-first commands, so that I can script them without parsing prose.

#### Acceptance Criteria
1. WHEN `mesh --help`, `mesh <resource> --help`, `mesh task create --help` or `mesh help <topic>` runs THE SYSTEM SHALL exit 0 and print an `Examples:` section, and `mesh help exit-codes` SHALL list exit codes 0–14 exactly as plan §13.2. [ACC: B1]
2. WHEN a command fails, including an argparse usage error, THE SYSTEM SHALL print exactly one JSON envelope with `ok:false`, `error.code` and `exit_code`, and SHALL use the same exit code with `--format text`. [ACC: B2, B7]
3. WHEN parameters arrive as JSON through `--input -` THE SYSTEM SHALL treat them as the equivalent flags, and SHALL fail rc 2 `DUPLICATE_PARAM` for a key also given as a flag and rc 2 `UNKNOWN_PARAM` for an unknown key. [ACC: B3, B4]
4. WHEN a mutating command is repeated with the same `--idempotency-key` and the same request THE SYSTEM SHALL return the stored response with `idempotent_replay:true`; WHEN the key is reused for a different request THE SYSTEM SHALL fail rc 6 `IDEMPOTENCY_KEY_REUSED`; a gated call answered with rc 7 SHALL NOT store the key. [ACC: B5, B12]
5. WHEN `--dry-run` is given THE SYSTEM SHALL return `dry_run:true` and `data.would`, leave the `events`, `outbox`, `runs` and `idempotency` row counts unchanged and spawn no process; a gated op SHALL return rc 7 with `approval_id:null` and an `op_hash`. [ACC: B6]
6. WHEN the daemon is not running THE SYSTEM SHALL fail rc 10 `DAEMON_UNAVAILABLE` with a hint to run `mesh daemon start`. [ACC: B8]
7. WHEN `--if-version` does not match THE SYSTEM SHALL fail rc 6 `VERSION_MISMATCH` and change nothing. [ACC: B9]
8. WHEN an agent is created without profile flags THE SYSTEM SHALL store `default_profile:"edit"` and `max_unapproved_profile:"dev"`. [ACC: B10]
9. WHEN a verb scheduled for a later milestone is called THE SYSTEM SHALL fail rc 2 `NOT_IMPLEMENTED_IN_M1` with `details.milestone`. [ACC: B11]
10. WHEN `config set` gets an unknown key, a wrong JSON type or an out-of-range value THE SYSTEM SHALL fail rc 2 `INVALID_SETTING` with `details.key`, and `config list` SHALL show every plan §5.7 key with its default. [ACC: B13]
11. THE SYSTEM SHALL offer no `--host`, `--port` or `--url` option anywhere. [ACC: A5]

### Requirement 2: Durable, traceable storage (plan M1.2, §5–§6)
**User Story:** As the user, I want every state change recorded and checkable, so that I can trust the history after crashes.

#### Acceptance Criteria
1. WHEN a task or project changes status THE SYSTEM SHALL write one event with `actor_id`, `actor_kind`, `via`, `ts`, `from_state`, `to_state` and `request_id`. [ACC: F1, F2]
2. WHEN `mesh doctor --check-invariants` runs on a fresh home or after any integration test THE SYSTEM SHALL exit 0 with an empty violation list, including during post-submit grace, during `cancelling` and after a limit was lowered below the running count. [ACC: A8, F2]
3. WHEN the invariant unit test builds the F8, T9 and D15 states THE SYSTEM SHALL report zero violations, and SHALL report the specific invariant id for each corrupted variant. [ACC: A10]
4. WHEN any process issues `UPDATE` or `DELETE` on `events` THE SYSTEM SHALL abort with `events are append-only`, and `mesh audit verify` on an untampered home SHALL exit 0. [ACC: F3]
5. WHEN a transition is not in the plan §6 tables THE SYSTEM SHALL fail rc 6 `INVALID_TRANSITION`. [ACC: F7]
6. WHEN `meta.schema_version` is newer than the code THE SYSTEM SHALL refuse to start with rc 1 `SCHEMA_TOO_NEW` and SHALL NOT modify or recreate the DB. [ACC: I9]

### Requirement 3: Local-only daemon and real identities (plan M1.3, §4, §7.1, §15)
**User Story:** As the user, I want Mesh reachable only locally and every caller authenticated, so that a role string can never grant authority.

#### Acceptance Criteria
1. WHILE meshd runs THE SYSTEM SHALL own no TCP or UDP socket and SHALL listen on one Unix socket `$H/run/mesh.sock`, and the `mesh/` sources SHALL contain no network server code. [ACC: C1, C2]
2. THE SYSTEM SHALL keep `MESH_HOME` directories at mode 700 and the DB, log and socket at 600, and WHEN `MESH_HOME` is 755 THE SYSTEM SHALL refuse to start with rc 2 `INSECURE_PERMISSIONS` until `mesh doctor --fix-perms` runs. [ACC: C3, C4]
3. WHEN a second daemon starts on the same home THE SYSTEM SHALL fail rc 1 `DAEMON_ALREADY_RUNNING` and the first SHALL keep serving. [ACC: C5]
4. WHEN a peer with another uid connects THE SYSTEM SHALL reject it before dispatch. [ACC: C6]
5. WHEN an agent token is issued THE SYSTEM SHALL print it once and store only its lookup and a 32-byte hash; WHEN a token file is not 0600, or lies under a workspace, THE SYSTEM SHALL fail rc 3 `TOKEN_FILE_INSECURE`. [ACC: C9, C10, C12]
6. THE SYSTEM SHALL never write the request `auth` field to `meshd.log`. [ACC: C11]
7. THE SYSTEM SHALL derive the passphrase key with scrypt `n=2**15, r=8, p=1, dklen=64, maxmem=64*1024*1024`. [ACC: C13]
8. WHEN the install root lies inside a workspace root, the runs dir or a registered repo THE SYSTEM SHALL refuse to start with rc 2 `INSTALL_PATH_INSECURE`. [ACC: C14]
9. WHEN five user commands carry a wrong passphrase THE SYSTEM SHALL fail each rc 3 `PASSPHRASE_INVALID`, the sixth rc 3 `LOCKED_OUT`, and SHALL record `auth.failed` events without the passphrase text. [ACC: G12]
10. WHEN a passphrase appears in stdin JSON THE SYSTEM SHALL fail rc 2 `PASSPHRASE_SOURCE_REJECTED`, and WHEN only `MESH_PASSPHRASE` is set without a tty or fd THE SYSTEM SHALL ignore it and fail rc 3 `NO_TTY`. [ACC: G17]
11. WHEN a revoked agent token, or a run token after its run ended, is used THE SYSTEM SHALL fail rc 3 `TOKEN_REVOKED`. [ACC: G18]

### Requirement 4: Agents, models, concurrency and honest adapters (plan M1.4, §9, §14)
**User Story:** As the user, I want to set each agent's models and limits and see each CLI's real status, so that the lead works only inside my policy.

#### Acceptance Criteria
1. WHEN an agent is created THE SYSTEM SHALL store the default model and allowed list as given, and SHALL fail rc 2 `DEFAULT_MODEL_NOT_ALLOWED` when the default is not allowed and rc 2 `INVALID_ARGUMENT` (`details.param:"allowed_models"`) for a launch agent without allowed models. [ACC: D1, D2]
2. WHEN a task names a model outside the agent's list THE SYSTEM SHALL fail rc 2 `MODEL_NOT_ALLOWED` with `details.allowed`. [ACC: D3]
3. WHEN an agent token tries to change an agent's models THE SYSTEM SHALL fail rc 4 `CAPABILITY_MISSING`. [ACC: D5]
4. WHEN an effort is not allowed THE SYSTEM SHALL fail rc 2 `EFFORT_NOT_ALLOWED`; for vibe any effort SHALL fail rc 2 `EFFORT_UNSUPPORTED`; for an agy model id that encodes effort plus an effort THE SYSTEM SHALL fail rc 2 `EFFORT_NOT_ALLOWED` with `details.reason:"model_encodes_effort"`. [ACC: D7, D17]
5. WHEN `max_concurrency` or `scheduler.max_total_runs` is not an integer in 1..9223372036854775807 THE SYSTEM SHALL fail rc 2 `INVALID_CONCURRENCY` with the plan §9.2 message and keep the previous value; valid values SHALL be stored as given. [ACC: D8, D9, D10]
6. WHEN `mesh adapter list` runs without testing mode THE SYSTEM SHALL list droid, agy, kiro and vibe as `partially_verified` with `launch_enabled:false` and `launch_milestone:"M3"`, hermes and openclaw as `not_installed`, and SHALL omit `fake`; creating a `hermes`/`openclaw` agent SHALL fail rc 13 `ADAPTER_NOT_INSTALLED`, and a `fake` agent outside testing mode rc 13 `TEST_DOUBLE_DISABLED`. [ACC: N1, N2, N3]
7. WHEN a task is assigned to a launch agent on a vendor adapter in M1 THE SYSTEM SHALL fail rc 13 `ADAPTER_UNSUPPORTED` with `details.milestone:"M3"` and start no process. [ACC: N4]
8. WHEN a task asks for a profile the adapter cannot express THE SYSTEM SHALL fail rc 13 `PROFILE_UNSUPPORTED` without creating an approval and without substituting another profile. [ACC: N5, G24]
9. THE SYSTEM SHALL build golden argv per adapter and profile from the committed real fixtures only, keep every emitted flag inside the help fixtures, pin the fixture sha256 values, apply the plan §12.5 prompt fallback, and mark a capability `stale` when the probed version differs. [ACC: N6, N7, N8, N15, N16]
10. THE SYSTEM SHALL import only the standard library, never read vendor credential or config paths, never reference earlier AgentMesh code, and the default suite SHALL leave the tripwire empty. [ACC: A2, A3, A4, A7]

### Requirement 5: Intake, projects and leads (plan M1.5, §6.1, §8)
**User Story:** As the user, I want any agent to hand work to a project's lead and to change that lead, so that work flows without me routing it.

#### Acceptance Criteria
1. WHEN an agent with the `intake` capability submits work without `--lead` THE SYSTEM SHALL create an active project led by `lead.default_agent`, record `created_via:"token"` and the origin, and send the lead one `project.created`; an agent without `intake` SHALL get rc 4 `CAPABILITY_MISSING`. [ACC: E1, E6]
2. WHEN `--lead` is given THE SYSTEM SHALL use it; WHEN no lead resolves THE SYSTEM SHALL fail rc 2 `NO_LEAD`; WHEN the lead is not `can_lead` or is disabled THE SYSTEM SHALL fail rc 2 `LEAD_NOT_ELIGIBLE`. [ACC: E2, E3, E4]
3. WHEN an agent tries to set `lead.default_agent` THE SYSTEM SHALL fail rc 4, and the user SHALL succeed. [ACC: E5]
4. WHEN work is submitted into an existing project THE SYSTEM SHALL send the current lead exactly one `project.request_received`. [ACC: E7]
5. WHEN the lead changes THE SYSTEM SHALL keep the full lead history, send the new lead a `project.handover`, cancel the old lead's pending approvals, supersede and re-create its messages, and reject the old lead with rc 4 `NOT_PROJECT_LEAD`; a relayed change SHALL be recorded `via:"relayed"`; a new lead that is assignee of a non-terminal task SHALL be refused rc 6 `LEAD_HAS_TASKS`. [ACC: E10, E11, E12, E13, J8]
6. WHEN a completion report misses a criterion, has `met:false` or empty evidence THE SYSTEM SHALL fail rc 2 `CRITERIA_REPORT_INCOMPLETE`; WHEN tasks, leases or approvals still block THE SYSTEM SHALL fail rc 6 `COMPLETION_BLOCKED` with `details.blocking`; a valid report by the lead SHALL complete the project with no user command and notify the user and the origin agent. [ACC: E14, E15, E16]
7. WHEN the user pauses, resumes or cancels a project THE SYSTEM SHALL hold queued tasks with `blocked_reason:"project_paused"`, restart them on resume, and on cancel end every non-terminal task and pending approval as `cancelled`. [ACC: E17]
8. WHEN an op targets a `completed` or `cancelled` project THE SYSTEM SHALL fail rc 6 `PROJECT_NOT_ACTIVE`. [ACC: E18]

### Requirement 6: Tasks, scheduling and notifications (plan M1.6, §6.2, §9.3, §12)
**User Story:** As a lead, I want to assign tasks directly and be notified of results, so that I never poll or race other agents.

#### Acceptance Criteria
1. WHEN the lead creates or assigns a task THE SYSTEM SHALL accept it, a non-lead SHALL get rc 4 `NOT_PROJECT_LEAD`, no `claim` verb SHALL exist, and assigning the lead itself SHALL fail rc 2 `LEAD_CANNOT_BE_ASSIGNEE`. [ACC: E8, E9]
2. WHEN a task depends on unfinished tasks or an incomplete project THE SYSTEM SHALL keep it `assigned`, and a dependency cycle SHALL fail rc 2 `DEP_CYCLE`. [ACC: F5, F6]
3. WHILE an agent is at its limit THE SYSTEM SHALL keep tasks `queued` with `queue_position` and `blocked_reason`, and start them by priority, then FIFO. [ACC: D11, D12]
4. WHEN an event for an agent occurs THE SYSTEM SHALL return it to a blocked `mesh inbox wait` within 1 s, return rc 8 `WAIT_TIMEOUT` with `items:[]` when nothing arrives, redeliver until acked, accept repeated acks, and deliver one message per event. [ACC: J1, J2, J3, J4]
5. THE SYSTEM SHALL keep messages across daemon restarts, start no model session while idle, and stream `mesh event tail` as live NDJSON in `seq` order. [ACC: J5, J6, J9]

### Requirement 7: Approvals and audit (plan M1.7, §7.3–§7.5)
**User Story:** As the user, I want important operations to wait for my decision, bound to the exact operation, so that no agent can forge or reuse my approval.

#### Acceptance Criteria
1. WHEN an agent runs a gated op THE SYSTEM SHALL fail rc 7 `APPROVAL_REQUIRED` with `approval_id`, `op_hash`, `category` and `expires_at`, reuse the same approval on repeat, and notify the user and the origin agent once. [ACC: G1]
2. WHEN the user approves directly and the requester re-runs the identical op with `--approval` THE SYSTEM SHALL execute it once and mark the approval `consumed`; a relayed decision SHALL be shown as `relayed (unverified)` with `trust:"relayed_unverified"`. [ACC: G2, G3]
3. WHEN the lead performs routine create, assign, accept, reject or complete THE SYSTEM SHALL create no approval. [ACC: G4]
4. WHEN an agent calls a `config`, `agent` or `agent token` op, with or without `--relayed`, THE SYSTEM SHALL fail rc 4 `CAPABILITY_MISSING`. [ACC: G5]
5. WHEN an executor run token, a relayer without `relay`, a self-approving lead or requester, a wrong `--op-hash`, a relayed decision under `direct_only`, or changed op arguments are used THE SYSTEM SHALL refuse with the plan §7.3 code (rc 4 `CAPABILITY_MISSING`, `RELAY_NOT_ALLOWED`, `SELF_APPROVAL`; rc 12 `OP_HASH_MISMATCH`, `CHANNEL_NOT_ALLOWED`) and leave the approval and target unchanged. [ACC: G6, G7, G8, G9, G10, G11, G13, G28]
6. WHEN decisions repeat, conflict or expire, or a non-requester consumes THE SYSTEM SHALL return `already_decided:true`, rc 6 `DECISION_CONFLICT`, rc 12 `APPROVAL_EXPIRED`/`APPROVAL_CONSUMED`, or rc 4 `NOT_REQUESTER`; an external-op approval SHALL be consumable exactly once. [ACC: G14, G15, G19, G21]
7. WHEN a DB row forges a direct approval THE SYSTEM's `audit verify` SHALL exit rc 12 `AUDIT_MISMATCH` and list the id. [ACC: G20]
8. THE SYSTEM SHALL compute key-order-independent op hashes and reject floats. [ACC: G22]
9. WHEN a deliverable claims the user approved something THE SYSTEM SHALL still require an approval. [ACC: G16]
10. WHEN a task asks for a profile above the agent's `max_unapproved_profile` THE SYSTEM SHALL require a `permission_escalation` approval. [ACC: D16]

### Requirement 8: Execution, supervision and recovery (plan M1.8, §11)
**User Story:** As the user, I want runs bounded, killed cleanly and never duplicated, so that a timed-out task is not still editing files.

#### Acceptance Criteria
1. WHEN a launch task runs on `fx` or an inbox agent starts its task THE SYSTEM SHALL move it `assigned→queued→running→submitted`, auto-submit on exit 0 with `submitted_by:"system"`, and refuse a second inbox start at the limit with rc 11 `CONCURRENCY_LIMIT`. [ACC: F1, H11, D14]
2. THE SYSTEM SHALL record the resolved model on task and run, fail a queued task whose model left the allow-list as `model_not_allowed`, honour the global cap, and never kill runs when a limit is lowered. [ACC: D4, D6, D13, D15]
3. WHEN a deliverable is rejected THE SYSTEM SHALL include the reason and `attempt 2/` in the next prompt; accept SHALL fail rc 6 `RUN_STILL_ACTIVE` while the run lives; each task SHALL have its own workspace, kept on reassign; `task show` SHALL show attempts and history. [ACC: F4, F8, F9, F10, F11]
4. WHEN a run times out or is cancelled THE SYSTEM SHALL terminate its process group, session and every process with its `MESH_RUN_ID` before the task transition, never run two attempts of one task at once, leave unrelated children alive, and reap without stealing exit statuses. [ACC: H1, H2, H3, H5, H13, H16, H17, H18]
5. WHEN a stale run writes THE SYSTEM SHALL fail rc 9 `FENCE_MISMATCH` or `LEASE_EXPIRED`; an inbox lease without heartbeat SHALL fail the task `lease_expired`; an unstarted inbox task SHALL fail `no_response`; the next attempt after `lease_expired` SHALL get a fresh workspace unless `--reuse-workspace`. [ACC: H4, H6, H7, H15]
6. WHEN a run fails THE SYSTEM SHALL notify the lead once and never retry automatically; retries and rejects at `max_attempts` SHALL need `retry_over_limit` approval; reassign and escalate SHALL work from `failed`. [ACC: H8, H9, H10, H14]
7. WHEN an executor run token calls anything outside its own task's heartbeat, progress, submit or fail THE SYSTEM SHALL fail rc 4. [ACC: H12]
8. THE SYSTEM SHALL keep tokens, keys and the passphrase out of logs, argv, `spec.json` and `argv_redacted_json`, and SHALL run Mesh's own processes with `-P -s -m`. [ACC: C7, C8, C15]
9. WHEN a process inside a run presents another credential or a passphrase THE SYSTEM SHALL fail rc 4 `CREDENTIAL_NOT_ALLOWED_IN_RUN` before verifying it, without counting toward lockout. [ACC: G25, G26, G27]
10. WHEN meshd crashes or stops and restarts THE SYSTEM SHALL adopt live runs, mark dead ones `lost_on_restart`, terminate overdue ones, never respawn automatically, keep inbox leases through a grace period, and deliver pending messages exactly once. [ACC: I1, I2, I3, I4, I5, I6, I7, I8, I10, I11]
11. THE SYSTEM SHALL run git only through an allow-list that excludes push, rebase, reset and checkout. [ACC: A6]

### Requirement 9: Docs and gate hygiene (plan M1.9)
**User Story:** As the user, I want the docs to state the real limits, so that I do not over-trust Mesh.

#### Acceptance Criteria
1. THE SYSTEM's `mesh help auth`, `mesh help approvals` and `docs/usage.md` SHALL state that relayed statements cannot be verified, that "Mesh does not sandbox external CLIs", that "Mesh is not a security sandbox", and the same-uid residual risks of plan §7.6. [ACC: G23, G29]
2. THE SYSTEM's `docs/usage.md` SHALL include the launch / inbox / user delivery table and say Mesh cannot push into an interactive TUI. [ACC: J7]
3. THE SYSTEM's `docs/adapters.md` SHALL give each of the six CLIs one status word, never count `fake` as vendor support, and describe Hermes/OpenClaw inbox participation as "inbox participation, not adapter support". [ACC: N9, N17]
4. THE SYSTEM's `scripts/test.sh` SHALL pass offline in under 10 minutes, and the M1 commits SHALL follow the M1.1…M1.9 order with green checkpoints. [ACC: A1, A9]
