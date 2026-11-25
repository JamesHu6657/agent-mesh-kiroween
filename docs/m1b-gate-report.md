# Milestone Gate M1b Verification Report

Gate: **M1b Coordination** (Plan §16 steps M1.5–M1.7; Checklist §0.1 M1b items)  
Verifier: agy (implementer self-check); gate decision by the coordinator  
Date: 2026-11-07  

---

## 1. Executive Summary

Milestone gate M1b establishes the coordination and multi-agent governance foundation of Agent Mesh v1:
- **M1.5 (Projects)**: Project creation, intake processing (non-repo projects), criteria management, lead default/override assignment, lead handover and invalidation of pending requests, project control (`pause`, `resume`, `cancel`), project completion reports, and notes.
- **M1.6 (Tasks)**: Task creation with full Assignment Guard evaluation (AG 1–6), dependency management with DAG validation and cycle detection, status transitions (T1–T4, T13, T19–T22), scheduler promotion, outbox fan-out per Table 12.2, inbox delivery and acknowledgement, non-blocking asynchronous waits (`inbox wait`, `task wait`, `project wait`), and live NDJSON event streaming (`event tail`).
- **M1.7 (Approvals)**: Generic and gated approval requests, canonical JSON op hashing with category isolation, approval policies for all 12 plan §7.4 categories, direct user decisions with HMAC-SHA256 signatures, relayed decisions with audited statements and credentials, decision conflict/idempotency, caller-bound approval consumption, background expiry sweep, project update gated by `scope_change`, and direct user `audit verify` verifying event hash continuity and approval signatures.

Every checklist item assigned to gate M1b in `docs/acceptance-checklist.md` §0.1 has been implemented and verified with automated CLI path tests carrying `ACC: <ID>` docstrings. The test suite passes 100% offline without live vendor calls, network sockets, or dirty workspace state.

- **Total M1b items:** 47
- **PASS:** 47
- **FAIL:** 0
- **NOT-COVERED:** 0

---

## 2. NOT-COVERED Items and Justification

**None (0 items).** All 47 checklist items assigned to gate M1b are fully covered and pass their complete checklist `Expect` clauses under automated tests.

---

## 3. Baseline & Invariant Integrity

- **All M1a items still pass:** All 46 M1a gate items remain green with zero regressions across unit, integration, and recovery tiers (matching HANDOFF §2 "46/46").
- **Harness invariant and tripwire verification:** The integration test harness teardown explicitly checks the tripwire log (verifying 0 unauthorized vendor CLI executions) and runs `mesh doctor --check-invariants` (verifying all database invariants I1–I7 with 0 violations) after every single integration test.

---

## 4. Gate Verification Table

| ID | Checklist Summary | Status | CLI Command / Mechanism | Test Name(s) |
|---|---|:---:|---|---|
| **B3** | Create one task with flags and one with the same values via `--input -` (JSON on stdin) | **PASS** | `mesh task create ...` vs `mesh task create --input -` | `tests/integration/test_tasks.py::test_b3_input_stdin_json` |
| **B4** | Conflicting duplicate CLI flag and JSON key; unknown param in JSON | **PASS** | `mesh task create --title y --input -` | `tests/integration/test_tasks.py::test_b4_duplicate_and_unknown_params` |
| **B5** | Idempotency key replay returns identical data; key reuse conflict | **PASS** | `mesh task create ... --idempotency-key k1` | `tests/integration/test_tasks.py::test_b5_idempotency_and_reused_key` |
| **B6** | Dry-run preserves database counts across `task create`, `task assign`, gated op | **PASS** | `mesh task create --dry-run`, `mesh project pause --dry-run` | `tests/integration/test_tasks.py::test_b6_dry_run_counts_unchanged` |
| **B11** | M2 verbs return exit 2 `NOT_IMPLEMENTED_IN_M1` with `milestone: "M2"` | **PASS** | `mesh task merge <tsk>`, `mesh intake submit --repo <repo>` | `tests/integration/test_projects.py::test_b11_not_implemented_milestones`<br>`tests/unit/test_skeleton.py::test_not_implemented_stubs` |
| **B12** | Gated op idempotency flow with `--approval` | **PASS** | `mesh project pause $PRJ --idempotency-key k7 --approval $APR` | `tests/integration/test_projects.py::test_b12_project_pause_approval_and_idempotency` |
| **D3** | Model not in assignee's allowed models rejected (exit 2 `MODEL_NOT_ALLOWED`) | **PASS** | `mesh task create ... --model m-z` | `tests/integration/test_tasks.py::test_d3_model_not_allowed` |
| **D7** | Effort not in allowed list or unsupported on adapter rejected (exit 2) | **PASS** | `mesh task create ... --effort high` | `tests/integration/test_tasks.py::test_d7_effort_validation` |
| **D16** | Elevated profile requires approval (`permission_escalation`); created on re-run | **PASS** | `mesh task create ... --profile elevated` | `tests/integration/test_tasks.py::test_d16_permission_escalation_approval` |
| **E1** | Intake submit creates project and criteria with default lead (`lead1`) | **PASS** | `mesh intake submit ...` | `tests/integration/test_projects.py::test_e1_intake_default_lead` |
| **E2** | Intake submit with explicit `--lead` assigns designated lead | **PASS** | `mesh intake submit ... --lead lead2` | `tests/integration/test_projects.py::test_e2_intake_override_lead` |
| **E3** | Intake submit without `--lead` and null default returns exit 2 `NO_LEAD` | **PASS** | `mesh intake submit ...` | `tests/integration/test_projects.py::test_e3_no_lead_error` |
| **E4** | Ineligible lead (`not_can_lead` or disabled) returns exit 2 `LEAD_NOT_ELIGIBLE` | **PASS** | `mesh intake submit ... --lead ibx` | `tests/integration/test_projects.py::test_e4_lead_not_eligible` |
| **E5** | Setting `lead.default_agent` requires direct user authentication | **PASS** | `mesh config set lead.default_agent '"lead2"' --user` | `tests/integration/test_projects.py::test_e5_config_set_lead_default_agent_auth` |
| **E6** | Intake submit capability enforcement (user, grok, plain; unprivileged agent blocked) | **PASS** | `mesh intake submit ...` | `tests/integration/test_projects.py::test_e6_intake_submit_auth` |
| **E7** | Intake follow-up `--message` records request and notifies active lead | **PASS** | `mesh intake submit --project $PRJ --message "..."` | `tests/integration/test_projects.py::test_e7_intake_follow_up_message` |
| **E8** | Lead assigns task directly; unprivileged agent receives exit 4 `NOT_PROJECT_LEAD` | **PASS** | `mesh task create ...`, `mesh task assign ...` | `tests/integration/test_tasks.py::test_e8_lead_assigns_non_lead_rejected` |
| **E9** | Current lead cannot be assigned to task (exit 2 `LEAD_CANNOT_BE_ASSIGNEE`) | **PASS** | `mesh task create ... --assignee lead1` | `tests/integration/test_tasks.py::test_e9_lead_cannot_be_assignee` |
| **E10** | `project set-lead` handover, records lead history, and cancels old lead approvals | **PASS** | `mesh project set-lead $PRJ lead2 --reason r --user` | `tests/integration/test_projects.py::test_e10_project_set_lead` |
| **E12** | Relayed `project set-lead` with `--user-statement` by relayer; unprivileged agent blocked | **PASS** | `mesh project set-lead $PRJ lead1 --relayed --user-statement "..."` | `tests/integration/test_projects.py::test_e12_project_set_lead_relayed` |
| **E13** | Setting lead to agent with incomplete tasks returns exit 6 `LEAD_HAS_TASKS` | **PASS** | `mesh project set-lead $PRJ worker --user` | `tests/integration/test_projects.py::test_e13_project_set_lead_assignee_guard` |
| **E14** | `project complete` validates acceptance criteria reports | **PASS** | `mesh project complete $PRJ --report -` | `tests/integration/test_projects.py::test_e14_project_complete_criteria_validation` |
| **E18** | Complete project or create task on completed/cancelled project exits 6 `PROJECT_NOT_ACTIVE` (no reopen) | **PASS** | `mesh project complete ...`, `mesh task create ...` | `tests/integration/test_projects.py::test_e18_project_terminal_no_reopen` |
| **F3** | Append-only triggers prevent SQLite update/delete; untampered DB passes `audit verify` | **PASS** | `mesh audit verify --user` | `tests/integration/test_approvals.py::test_f3_audit_verify_hash_chain_and_append_only_triggers`<br>`tests/unit/test_schema.py::test_events_append_only_triggers`<br>`tests/unit/test_events.py::test_tamper_detection` |
| **F6** | Project dependency gating: task in project B depends on project A completing | **PASS** | `mesh scheduler tick` | `tests/integration/test_scheduler.py::test_f6_project_dependencies_promotion` |
| **G1** | Gated op (`project pause`) generates pending approval and deduplicated outbox messages | **PASS** | `mesh project pause $PRJ` | `tests/integration/test_approvals.py::test_g1_project_pause_approval_gate_and_notifications` |
| **G2** | Direct user approval and command re-execution with `--approval` | **PASS** | `mesh approval approve $APR`, `mesh project pause $PRJ --approval $APR` | `tests/integration/test_approvals.py::test_g2_rerun_and_g13_op_hash_mismatch`<br>`tests/integration/test_approvals.py::test_g3_and_g2_direct_and_relayed_decisions` |
| **G3** | Relayed approval by relayer (`trust: "relayed_unverified"`) vs direct user (`trust: "direct_verified"`) | **PASS** | `mesh approval approve $APR --relayed ...` | `tests/integration/test_approvals.py::test_g3_and_g2_direct_and_relayed_decisions` |
| **G5** | Agent callers on user-only rows (`config.*`, `agent.*`, `audit.*`) receive exit 4 `CAPABILITY_MISSING` | **PASS** | `mesh config set ...`, `mesh agent create ...`, `mesh audit verify` | `tests/integration/test_approvals.py::test_g5_agent_callers_on_user_only_rows` |
| **G7** | Agent without relay capability calling approve with `--relayed` receives exit 4 `RELAY_NOT_ALLOWED` | **PASS** | `mesh approval approve $APR --relayed ...` | `tests/integration/test_approvals.py::test_g7_g8_agent_decision_negatives` |
| **G8** | Lead caller attempting approval without `--relayed` receives exit 4 `CAPABILITY_MISSING` | **PASS** | `mesh approval approve $APR` | `tests/integration/test_approvals.py::test_g7_g8_agent_decision_negatives` |
| **G9** | Approval decision with mismatched `--op-hash` returns exit 12 `OP_HASH_MISMATCH` | **PASS** | `mesh approval approve $APR --op-hash bogus` | `tests/integration/test_approvals.py::test_g9_op_hash_mismatch` |
| **G10** | Requester or project lead self-approval rejected (exit 4 `SELF_APPROVAL`) | **PASS** | `mesh approval approve $APR --relayed ...` | `tests/integration/test_approvals.py::test_g10_self_approval_negatives` |
| **G11** | Policy change to `direct_only` rejects relayed approvals (exit 12 `CHANNEL_NOT_ALLOWED`) | **PASS** | `mesh approval approve $APR --relayed ...` | `tests/integration/test_approvals.py::test_g11_channel_not_allowed` |
| **G12** | Passphrase lockout: 5 failures -> `PASSPHRASE_INVALID`, 6th -> `LOCKED_OUT`; logs `auth.failed` without secrets | **PASS** | `mesh whoami --user --passphrase-fd <fd>`, `mesh event list --type auth.failed --user` | `tests/integration/test_daemon.py::test_passphrase_lockout` |
| **G13** | Op-hash mismatch: re-run different command (`project cancel`) with pause approval, and profile escalation mismatch (`--profile unrestricted`) | **PASS** | `mesh project cancel $PRJ --approval $PAUSE_APR`, `mesh task create ... --profile unrestricted --approval $APR` | `tests/integration/test_approvals.py::test_g2_rerun_and_g13_op_hash_mismatch` |
| **G14** | Decision idempotency (`already_decided: true`), decision conflict (exit 6), and double consume (exit 12) | **PASS** | `mesh approval approve ...`, `mesh approval reject ...`, `mesh approval consume ...` | `tests/integration/test_approvals.py::test_g14_idempotency_conflict_and_cancel`<br>`tests/integration/test_approvals.py::test_g14_consume_twice_and_g19_not_requester` |
| **G15** | Testing mode expiry sweep: expired approvals reject consumption with exit 12 `APPROVAL_EXPIRED` | **PASS** | `mesh project pause $PRJ --approval $EXPIRED_APR` | `tests/integration/test_approvals.py::test_g15_testing_mode_expiry` |
| **G19** | Approval consume by non-requester rejected with exit 4 `NOT_REQUESTER` | **PASS** | `mesh approval consume $APR --op-hash $H` | `tests/integration/test_approvals.py::test_g14_consume_twice_and_g19_not_requester` |
| **G20** | Tampered approval `user_mac` detected by `audit verify` (exit 12 `AUDIT_MISMATCH`, lists approval ID) | **PASS** | `mesh audit verify --user` | `tests/integration/test_approvals.py::test_g20_audit_verify_bogus_user_mac` |
| **G21** | Generic `approval request` lifecycle for `spend_money`; unprivileged agent blocked | **PASS** | `mesh approval request --category spend_money ...` | `tests/integration/test_approvals.py::test_g21_spend_money_flow_and_authority` |
| **G28** | Permission escalation default policy `direct_only`: relayed rejected, direct approved | **PASS** | `mesh approval approve $APR --relayed ...` vs `mesh approval approve $APR --user` | `tests/integration/test_approvals.py::test_g28_permission_escalation_default_policy` |
| **J2** | `inbox wait` with nothing pending times out returning exit 8 `WAIT_TIMEOUT` with `items: []` | **PASS** | `mesh inbox wait --timeout 2s` | `tests/integration/test_inbox.py::test_j2_inbox_wait_timeout_empty`<br>`tests/integration/test_inbox.py::test_j2_inbox_wait_returns_promptly_on_message` |
| **J4** | `inbox list` at-least-once redelivery until acknowledged; idempotent `inbox ack` | **PASS** | `mesh inbox list`, `mesh inbox ack <msg>` | `tests/integration/test_inbox.py::test_j4_inbox_list_and_ack_lifecycle` |
| **J8** | Pending unacked messages for lead1 superseded on handover and re-created for new lead | **PASS** | `mesh project set-lead $PRJ lead2 ...` | `tests/integration/test_events.py::test_j8_outbox_handover_superseded` |
| **N4** | Launch agent with `agy` adapter returns exit 13 `ADAPTER_UNSUPPORTED` (`milestone: "M3"`) | **PASS** | `mesh task create ... --assignee agy-agent` | `tests/integration/test_tasks.py::test_n4_adapter_launch_disabled` |
| **N5** | Profile unsupported (`dev`/`elevated` on kiro/agy/vibe) rejected at AG Step 3 before launch enabled | **PASS** | `mesh task create ... --profile dev` | `tests/integration/test_tasks.py::test_n5_adapter_profile_unsupported` |

---

## 5. Command & Milestone Status

### 5.1 Verbs Reporting "M1b"
**NONE**. All commands planned for gate M1b have been implemented:
- `project.update`: Implemented with `scope_change` approval gating and policy enforcement.
- `approval.consume`: Implemented with requester and caller binding.
- `audit.verify`: Implemented with event chain continuity from genesis, direct user event MAC verification, direct user decision MAC verification across approved/consumed/expired rows, and strict detection of forged decision rows (requiring valid `decided_via IN ('direct','relayed')`, non-NULL `decided_at` and `decided_by`, and valid `user_mac` on direct decisions).

### 5.2 Unimplemented Verbs and Reporting Milestones
All unimplemented commands correctly return exit code 2 `NOT_IMPLEMENTED_IN_M1`:
- **Milestone M1c**: `task.start`, `task.log`, `task.heartbeat`, `task.progress`, `task.submit`, `task.fail`, `task.accept`, `task.reject`, `run.list`, `run.show`, `run.log`.
- **Milestone M2**: `task.merge`, `intake.submit --repo <path>`, `project.deliver`.
- **Milestone Unassigned / null (`milestone: null`)**: `workspace.list`, `workspace.show`, `workspace.prune`, `workspace.delete`, `top`. (Plan §16 M2 builds only `workspace prune/delete`; `workspace list/show` and `top` do not appear in the plan, and all five report `milestone: null` in code).

### 5.3 Deviations Recorded in M1b
- **Testing mode lockout window (§17.1, §7.1, G12)**: In testing mode (`lockout_window` in `DaemonServer.authenticate_and_authorize` (mesh/daemon/server.py)), the lockout window is 300 s unless a test sets the testing-only setting `auth.lockout_window_s`; the 2 s fallback applies only to homes without that settings row. This is required to observe the "after the lockout ends" clause of G12 in automated tests. Production lockout window is unchanged (300 seconds).
- **Op document canonical format (§7.4)**: `category` is embedded as a reserved top-level key in the canonical op document to prevent collision across distinct categories.

---

## 6. Invariant & Doctor Status

`mesh doctor --check-invariants` exit code: **0**.  
Database invariant checker verifies all active invariants (I1–I7) with 0 violations across unit and integration teardowns.

---

## 7. Test Suite Execution & Hygiene Evidence

```
$ bash scripts/test.sh
=== Running Unit Tests ===
...................................................................................................
----------------------------------------------------------------------
Ran 99 tests in 7.753s

OK
=== Running Integration Tests ===
.........................................................................................................................
----------------------------------------------------------------------
Ran 121 tests in 416.762s

OK
=== Running Recovery Tests ===
.
----------------------------------------------------------------------
Ran 1 test in 0.000s

OK
=== All test tiers passed successfully ===
```

- **Exit code:** 0
- **Unit test count:** 99 tests
- **Integration test count:** 121 tests
- **Recovery test count:** 1 test
- **Total tests:** 221 tests
- **Tripwire log status:** Clean (0 bytes, no unauthorized vendor CLIs spawned)
