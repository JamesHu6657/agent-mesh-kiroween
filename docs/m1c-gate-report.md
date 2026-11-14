# Milestone Gate M1c Verification Report

Gate: **M1c Execution** (Plan §16 steps M1.8–M1.9; Checklist §0.1 M1c items), with the full M1 reconciliation (M1a + M1b + M1c)  
Verifier: Quest (implementer self-check, run 5); gate decision by the coordinator and Kiro. **This report does not mark the gate passed.**  
Date: 2026-11-09  
Branch: `wt/m1c-run5` (from 0637609); tested HEAD: 6118183

---

## 1. Executive Summary

M1c adds execution to Agent Mesh: launch-mode runs through `runwrap` and the supervisor (spawn, watch, exit.json as the source of truth), run tokens and executor authz, redaction, read verbs (`run list/show`, `task log`, `workspace list/show`), termination and cancel (T9/T10 through `cancelling`, project cancel), the child registry and reaper, inbox-mode execution with leases and fencing, failure policy (retry caps, reassign, escalate, no_response), crash recovery on every boot (adopt, lost runs, spawn-window hooks), and the M1.9 docs (usage.md, adapters.md).

| Gate | Items | PASS | FAIL | NOT-COVERED | coordinator-verified |
|---|---:|---:|---:|---:|---:|
| M1a | 46 | 45 | 0 | 0 | 1 (A9) |
| M1b | 47 | 47 | 0 | 0 | 0 |
| M1c | 70 | 70 | 0 | 0 | 0 |
| **M1 total** | **163** | **162** | **0** | **0** | **1** |

Open for the gate decision: A9 (scratch-clone checkouts, §5), the D8 JSON message reading (§7.3), and the adopted-wrapper residual from review m1c-run4 (§7.4).

---

## 2. NOT-COVERED Items

**None.** The reconciliation pass (§4 note) found 29 rows whose tests asserted only part of the Expect; all 29 were strengthened in 3e58d00 (M1b/M1c clauses) and 6118183 (M1a clauses). A1, A3 and C1 also have the recorded evidence in §3 and §6.

---

## 3. Final Suite (A1, A3)

```
$ time MESH_SUITE_TRIPWIRE_FILE=/tmp/r5tw.log bash scripts/test.sh     # HEAD 6118183, no other suite on the box
Unit:        Ran 172 tests in 12.806s    OK
Integration: Ran 205 tests in 411.390s   OK
Recovery:    Ran 15 tests in 33.893s     OK
real 7m38.481s
RC=0
$ test ! -s /tmp/r5tw.log && echo tripwire-empty     # file never created
tripwire-empty
```

- **A1:** rc 0, 392 tests, 458 s wall (budget 600 s), offline, no TCP.
- **A3:** the suite tripwire file was never written; every integration test's teardown also asserts its own tripwire empty.
- Slowest: J6 idle 72 s, G12 lockout 33 s, cancel-spares-unrelated-children 31 s, H8 25 s; recovery: I7 crash-after-TX-A 26 s.
- Under heavy load (three suites at once) the main branch failed E16 and E17; both were made load-proof in 04d96e2 (§7.2). An overlapping run in this session (two copies of this suite plus other worktrees, 28–29 min) passed once and failed once on that same E16 timeout, before the fix.

---

## 4. Gate Verification Table

Test names are full dotted paths, except these short forms: `fp.` = `tests.integration.test_execution_failure_policy.`, `inbox.` = `tests.integration.test_execution_inbox.`, `run2.` = `tests.integration.test_execution_acc_run2.`, `term.` = `tests.integration.test_execution_termination.`, `rec.` = `tests.recovery.test_recovery.`, `docs.` = `tests.unit.test_docs_m19.`. The Check column is shortened; the full Check/Expect is in `docs/acceptance-checklist.md`.

### M1a (46 items)

| ID | Check (short) | Status | Test(s) |
|---|---|:---:|---|
| A1 | `scripts/test.sh` on this box, offline | PASS | `bash scripts/test.sh` on HEAD 6118183, box otherwise idle: rc 0, 392 tests (172 unit + 205 integration + 15 recovery), wall 7m38s (§3); tests.unit.test_skeleton.TestSkeleton.test_test_runner_script |
| A2 | `test_stdlib_only`; `grep -rnE '^\s*(import\|from) (cryptography\|requests\|pytest)' mesh tests` | PASS | tests.unit.test_skeleton.TestSkeleton.test_stdlib_only; tests.unit.test_paths.TestPaths.test_stdlib_only |
| A3 | After A1, read the tripwire file written by `tests/shims/*` | PASS | suite run with `MESH_SUITE_TRIPWIRE_FILE=/tmp/r5tw.log`: file never created (§3); tests.unit.test_skeleton.TestSkeleton.test_tripwire_shims; harness teardown `assert_tripwire_empty` |
| A4 | `grep -rn "agent-mesh" mesh tests bin scripts` | PASS | tests.unit.test_skeleton.TestSkeleton.test_no_workspace_agent_mesh_references |
| A5 | `m --help`; `m help env` | PASS | tests.unit.test_skeleton.TestSkeleton.test_no_forbidden_host_port_flags |
| A7 | `grep -rnE '\.factory\|\.gemini\|\.kiro\|\.vibe' mesh` | PASS | tests.unit.test_skeleton.TestSkeleton.test_no_vendor_credential_paths |
| A8 | `m doctor --check-invariants` on a fresh home and after every integration test (harness teardown) | PASS | tests.unit.test_doctor.TestDoctorCLI.test_doctor_check_invariants_fresh_home / _initialized_clean_db / _corrupted_db; tests.integration.test_execution_inbox.TestExecutionInbox.test_a8_doctor_check_invariants_while_running; tests.integration.test_execution_termination.TestTermination.test_h2_cancel_ignore_sigterm, test_h16_cancel_during_post_submit_grace + test_f8_accept_blocked_until_post_submit_grace_kill; tests.integration.test_execution_acc_run2.TestRun2Execution.test_d15_lowered_limit_no_kill; harness teardown |
| A10 | `test_invariants_valid_states` | PASS | tests.unit.test_invariants.TestInvariants.test_invariants_valid_states, test_corrupted_i1_lease_on_accepted_task … test_corrupted_i6_consumed_approval, test_corrupted_i8_cancelling_task_without_lease |
| B1 | `m --help`; `m <resource> --help` for every resource; `m task create --help`; `m help … | PASS | tests.unit.test_skeleton.TestSkeleton.test_help_layers_and_examples |
| B2 | `m agent show no-such-agent [U]`; `m task create --bogus [@lead1]` | PASS | tests.unit.test_skeleton.TestSkeleton.test_argparse_errors_envelope; tests.integration.test_agents.TestAgentsIntegration.test_agent_show_not_found |
| B7 | Same failing command with `--format text` and with no format flag | PASS | tests.unit.test_skeleton.TestSkeleton.test_format_flag_identical_rc |
| B8 | `m daemon stop [U]`, then `m status` | PASS | tests.integration.test_daemon.TestDaemonIntegration.test_daemon_status_and_stop |
| B9 | `m agent update fx --if-version 999 --max-concurrency 3 [U]` | PASS | tests.integration.test_agents.TestAgentsIntegration.test_agent_update_version_mismatch |
| B10 | `m agent show fx` (created without profile flags) | PASS | tests.integration.test_agents.TestAgentsIntegration.test_agent_default_profiles |
| B13 | `m config list [U]`; `m config set nosuch.key 1 [U]`; `m config set task.default_timeout_s '"600"' [U]`; … | PASS | tests.unit.test_settings.TestSettingsRegistry.test_settings_registry; tests.integration.test_daemon.TestDaemonIntegration.test_config_crud_and_validation |
| C1 | With meshd running: `ss -ltnupH`; `ss -xlp` | PASS | tests.integration.test_daemon.TestDaemonIntegration.test_daemon_socket_and_no_tcp (runs the Check: /proc/net cross-check, `ss -ltnupH`, `ss -tuanpH`, `ss -xlpH`) |
| C2 | `test_no_network_code` | PASS | tests.unit.test_skeleton.TestSkeleton.test_no_network_code; tests.unit.test_paths.TestPaths.test_no_network_code |
| C3 | `stat -c '%a %n' $H $H/run $H/runs $H/workspaces $H/mesh.db $H/run/mesh.sock $H/logs/meshd.log` | PASS | tests.integration.test_daemon.TestDaemonIntegration.test_file_permissions |
| C4 | Stop meshd; `chmod 755 $H`; `m daemon run`; then `m doctor --fix-perms` and start again | PASS | tests.unit.test_paths.TestPaths.test_home_permissions; tests.integration.test_daemon.TestDaemonIntegration.test_insecure_home_permissions |
| C5 | Start a second daemon on the same home | PASS | tests.integration.test_daemon.TestDaemonIntegration.test_daemon_already_running |
| C6 | Unit test of the peer check with a different `SO_PEERCRED` uid (or a real second uid if available) | PASS | tests.unit.test_auth.TestAuth.test_peer_cred_check_mismatched_uid |
| C9 | `m agent token issue plain [U]`, then `m agent token list plain [U]`; db `credentials` row | PASS | tests.unit.test_auth.TestAuth.test_token_generation_and_verification; tests.integration.test_agents.TestAgentsIntegration.test_agent_tokens |
| C10 | `m whoami --token-file f` with `f` mode 0644 | PASS | tests.unit.test_paths.TestPaths.test_token_file_secure; tests.integration.test_daemon.TestDaemonIntegration.test_token_file_mode_0644_rejected |
| C11 | `grep -c '"auth"' $H/logs/meshd.log` after a session of user and token requests | PASS | tests.integration.test_daemon.TestDaemonIntegration.test_audit_log_never_contains_auth |
| C12 | Copy grok's token file to `$H/workspaces/g.tok` (0600); `m whoami --token-file $H/workspaces/g.tok` | PASS | tests.integration.test_daemon.TestDaemonIntegration.test_token_file_in_workspaces_rejected; tests.unit.test_paths.TestPaths.test_token_file_secure |
| C13 | `test_scrypt_params` | PASS | tests.unit.test_auth.TestAuth.test_scrypt_params |
| C14 | `test_install_path_check`; copy the install (`bin/`, `mesh/`) to `$H/workspaces/inst/`, then … | PASS | tests.unit.test_paths.TestPaths.test_install_path_check; tests.integration.test_daemon.TestDaemonIntegration.test_install_path_check_in_workspace |
| D1 | `m agent create --name a1 --adapter fake --allowed-model m-a --allowed-model m-b --default-model m-a … | PASS | tests.integration.test_agents.TestAgentsIntegration.test_agent_create_and_show |
| D2 | Same as D1 with `--default-model m-z`; and a launch agent with no allowed models | PASS | tests.integration.test_agents.TestAgentsIntegration.test_agent_create_invalid_models |
| D5 | `m agent update fx --allowed-model m-z [@lead1]`; same with `[@grok]` | PASS | tests.integration.test_agents.TestAgentsIntegration.test_agent_update_permissions |
| D8 | For each value `0`, `-1`, `1.5`, `abc`, `''`, `1e3`, `0x10`, `+3`, `' 2'`, `9223372036854775808`: `m agent … | PASS | tests.unit.test_settings.TestSettingsRegistry.test_policy_concurrency; tests.integration.test_agents.TestAgentsIntegration.test_agent_concurrency_validation, test_agent_concurrency_json_validation |
| D9 | Values `1`, `7`, `9223372036854775807` by flag and as JSON integers | PASS | same three tests |
| D10 | `m agent update fx --max-concurrency 0 [U]`; `m config set scheduler.max_total_runs 0 [U]` | PASS | tests.unit.test_settings.TestSettingsRegistry.test_policy_concurrency; tests.integration.test_agents.TestAgentsIntegration.test_agent_concurrency_validation |
| D17 | `m agent create --name ag1 --adapter agy --allowed-model gemini-3.8-flash-high --default-model … | PASS | tests.integration.test_agents.TestAgentsIntegration.test_agent_create_agy_model_effort_conflict |
| G17 | `echo '{"passphrase":"…"}' \| m config set task.default_timeout_s 600 --user --input -`; … | PASS | tests.integration.test_daemon.TestDaemonIntegration.test_passphrase_sources |
| G22 | `test_canonical` | PASS | tests.unit.test_canonical.TestCanonical.* |
| G24 | Unit test over every adapter × profile | PASS | tests.unit.test_adapters.TestAdapters.test_profile_flags_and_separation |
| I9 | db: set `meta.schema_version` to a future value; start meshd | PASS | tests.unit.test_schema.TestSchema.test_schema_too_new; tests.integration.test_daemon.TestDaemonIntegration.test_schema_too_new |
| N1 | Daemon without `MESH_TESTING`: `m adapter list` | PASS | tests.integration.test_adapters.TestAdaptersIntegration.test_adapter_list_without_and_with_testing_mode |
| N2 | `m agent create --name h --adapter hermes … [U]`; same for `openclaw`; `m adapter probe hermes`; `m … | PASS | tests.integration.test_agents.TestAgentsIntegration.test_agent_create_not_installed_adapter; tests.integration.test_adapters.TestAdaptersIntegration.test_adapter_probe_and_show_not_installed |
| N3 | Without testing mode: `m agent create --adapter fake …`; with testing mode: `m adapter list` | PASS | tests.integration.test_adapters.TestAdaptersIntegration.test_adapter_list_without_and_with_testing_mode |
| N6 | `test_adapters_build_*`, `test_adapters_models_parse`; `ls tests/fixtures/real` | PASS | tests.unit.test_adapters.TestAdapters.test_adapters_build_droid/agy/kiro/vibe, test_adapters_models_parse, test_adapters_parse_errors |
| N7 | `m adapter models kiro`; `m adapter models vibe` | PASS | tests.integration.test_adapters.TestAdaptersIntegration.test_adapter_models; tests.unit.test_adapters.TestAdapters.test_adapters_models_parse |
| N8 | Unit test: probe returns a version different from the one recorded at verification | PASS | tests.unit.test_adapters.TestAdapters.test_adapter_stale_version |
| N15 | `test_fixture_hashes`; `test_adapter_flags_in_fixtures` | PASS | tests.unit.test_fixture_hashes.TestFixtureHashes.test_fixture_hashes; tests.unit.test_adapters.TestAdapters.test_adapter_flags_in_fixtures |
| N16 | `test_prompt_fallback` (unit; launch is disabled in M1) | PASS | tests.unit.test_adapters.TestAdapters.test_prompt_fallback, test_prompt_t5_too_long; tests.integration.test_execution_launch.*.test_t5_prompt_too_long_fails_task; tests.integration.test_m1a_reconciliation.TestPromptT5TooLongWithRework.test_prompt_t5_too_long_after_rework |

### M1b (47 items)

| ID | Check (short) | Status | Test(s) |
|---|---|:---:|---|
| B3 | Create one task with flags and one with the same values via `--input -` (JSON on stdin) | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_b3_input_stdin_json` |
| B4 | `--title y` plus stdin `{"title":"x"}`; stdin `{"titel":"x"}` | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_b4_duplicate_and_unknown_params` |
| B5 | `m task create … --idempotency-key k1 [@lead1]` twice, then key `k1` with a different title | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_b5_idempotency_and_reused_key` (now `resp2.data == resp1.data`); `tests.integration.test_daemon.TestDaemonIntegration.test_idempotency_and_dry_run` |
| B6 | Count rows of `events`, `outbox`, `runs`, `idempotency` (db) before and after `task create --dry-run`, … | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_b6_dry_run_counts_unchanged` |
| B11 | In the M1 build: `m task merge <tsk> [@lead1]`; `m intake submit --repo … [@grok]` | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_b11_not_implemented_milestones`; `tests.unit.test_skeleton.TestSkeleton.test_not_implemented_stubs` |
| B12 | `m project pause $PRJ --idempotency-key k7 [@lead1]`; user approves; `m project pause $PRJ … | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_b12_project_pause_approval_and_idempotency` |
| D3 | `m task create --project $PRJ --title t --spec s --assignee fx --model m-z [@lead1]` | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_d3_model_not_allowed` |
| D7 | Effort not in `allowed_efforts` on a fake agent; any `--effort` on an agent with `--adapter vibe` | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_d7_effort_validation` (vibe clause on an inbox vibe agent, no task row; reading in §7.3) |
| D16 | `m task create … --assignee fx --profile elevated [@lead1]` | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_d16_permission_escalation_approval` |
| E1 | `m intake submit [@plain] --title t --goal g --criterion c1` (no `--lead`); `m project leads <prj> [U]` | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e1_intake_default_lead` |
| E2 | Same with `--lead lead2` | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e2_intake_override_lead` |
| E3 | `m config set lead.default_agent null [U]`; submit without `--lead` | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e3_no_lead_error` |
| E4 | Submit with `--lead ibx` (not `can_lead`) and with a disabled lead | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e4_lead_not_eligible` (project count unchanged) |
| E5 | `m config set lead.default_agent '"lead2"' [@lead1]`; then with `[U]` | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e5_config_set_lead_default_agent_auth` |
| E6 | Intake by `[U]`, by `[@grok]`, by `[@plain]`; by an agent created with no `intake` capability; by an … | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e6_intake_submit_auth`; `tests.unit.test_projects.TestAuthzMatrix.test_authz_matrix_rules` (run-token intake → 4) |
| E7 | `m intake submit --project $PRJ --message "also add X" [@grok]` | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e7_intake_follow_up_message` |
| E8 | `m task create --project $PRJ --title t --spec s --assignee fx [@lead1]`; `m task assign <tsk> --to ibx … | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_e8_lead_assigns_non_lead_rejected` |
| E9 | `m task create --project $PRJ … --assignee lead1 [@lead1]` | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_e9_lead_cannot_be_assignee` |
| E10 | With tasks and a pending approval requested by `lead1`: `m project set-lead $PRJ lead2 --reason r [U]` | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e10_project_set_lead` (earlier events/tasks still listed; handover criteria ids and task table) |
| E12 | `m project set-lead $PRJ lead1 --reason r --relayed --user-statement "James: switch back" [@grok]`; same … | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e12_project_set_lead_relayed` |
| E13 | `set-lead` to an agent that is the assignee of a non-terminal task in the project | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e13_project_set_lead_assignee_guard` |
| E14 | `m project complete $PRJ --report - [@lead1]` with reports that (a) omit criterion c2, (b) have … | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e14_project_complete_criteria_validation` |
| E18 | `m project complete` or `task create` on a `completed` or `cancelled` project | PASS | `tests.integration.test_projects.TestProjectsIntegration.test_e18_project_terminal_no_reopen` |
| F3 | db (writable connection) `UPDATE events SET reason='x'` and `DELETE FROM events`; then `m audit verify … | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_f3_audit_verify_hash_chain_and_append_only_triggers`; `tests.unit.test_schema.TestSchema.test_events_append_only_triggers`; `tests.unit.test_events.TestEvents.test_tamper_detection` |
| F6 | Project B with `--depends-on-project $PRJ`; assign a task in B | PASS | `tests.integration.test_scheduler.TestSchedulerIntegration.test_f6_project_dependencies_promotion` |
| G1 | `m project pause $PRJ [@lead1]`, run twice | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g1_project_pause_approval_gate_and_notifications` |
| G2 | `m approval approve $APR --op-hash $HASH [U]`; then `m project pause $PRJ --approval $APR [@lead1]` | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g2_rerun_and_g13_op_hash_mismatch`; `tests.integration.test_approvals.TestApprovalsIntegration.test_g3_and_g2_direct_and_relayed_decisions` |
| G3 | New approval; `m approval approve $APR --op-hash $HASH --relayed --user-statement "James: ok" … | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g3_and_g2_direct_and_relayed_decisions` |
| G5 | `m config set …`, `m agent create …`, `m agent token issue …` with `[@lead1]`, and with `--relayed … [@grok]` | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g5_agent_callers_on_user_only_rows` |
| G7 | `m approval approve $APR --op-hash $HASH --relayed --user-statement x [@plain]`; same from an `ibx` inbox … | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g7_g8_agent_decision_negatives` |
| G8 | `m approval approve $APR --op-hash $HASH [@lead1]` (lead approving its own request) | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g7_g8_agent_decision_negatives` |
| G9 | Approve with a wrong `--op-hash` (direct and relayed) | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g9_op_hash_mismatch` |
| G10 | `m approval request --category deploy --summary s --operation '{"t":1}' [@grok]`, then grok approves it … | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g10_self_approval_negatives` |
| G11 | `m config set approval.policy.project_control '"direct_only"' [U]`; relayed approve of a `project_control` … | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g11_channel_not_allowed` |
| G12 | 5 user commands with a wrong passphrase, then one with the right one; `m event list --type auth.failed … | PASS | `tests.integration.test_daemon.TestDaemonIntegration.test_passphrase_lockout` (parallel phase, 30 s testing window, lapse polled up to 75 s); `tests.unit.test_settings.TestSettingsRegistry.test_lockout_window_setting_is_testing_only` |
| G13 | Approve the G1 pause request, then run `m project cancel $PRJ --approval $APR [@lead1]`; for a … | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g2_rerun_and_g13_op_hash_mismatch` |
| G14 | Consume twice; approve twice; approve then reject | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g14_idempotency_conflict_and_cancel`; `tests.integration.test_approvals.TestApprovalsIntegration.test_g14_consume_twice_and_g19_not_requester` |
| G15 | Testing mode: request with `--expires-in 1s`, wait 2 s, approve; also approve one, let it expire, then use … | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g15_testing_mode_expiry` |
| G19 | `m approval consume $APR --op-hash $HASH [@plain]` on grok's request | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g14_consume_twice_and_g19_not_requester` |
| G20 | db (writable): set an approval to `approved`, `decided_via='direct'`, bogus `user_mac`; `m audit verify [U]` | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g20_audit_verify_bogus_user_mac`; `tests.integration.test_approvals.TestApprovalsIntegration.test_audit_verify_and_consume_reject_null_decided_at_or_mac` |
| G21 | `m approval request --category spend_money --summary s --operation '{"usd":5}' [@lead1]`; user approves; … | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g21_spend_money_flow_and_authority` |
| G28 | Default policy. The `permission_escalation` approval from D16: approve `--relayed --user-statement x … | PASS | `tests.integration.test_approvals.TestApprovalsIntegration.test_g28_permission_escalation_default_policy` |
| J2 | `m inbox wait --timeout 2s [@plain]` with nothing pending | PASS | `tests.integration.test_inbox.TestInbox.test_j2_inbox_wait_timeout_empty` |
| J4 | `m inbox list [@lead1]` twice without ack; `m inbox ack <msg> [@lead1]` twice; list again | PASS | `tests.integration.test_inbox.TestInbox.test_j4_inbox_list_and_ack_lifecycle` |
| J8 | Pending unacked messages for `lead1`, then `set-lead` to `lead2` | PASS | `tests.integration.test_events.TestEventsIntegration.test_j8_outbox_handover_superseded` |
| N4 | Launch agent on adapter `agy`; `m task create … --assignee <it> [@lead1]` | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_n4_adapter_launch_disabled` |
| N5 | Kiro agent: tasks with `--profile dev` and `--profile elevated`; agy agent: `dev` and `elevated`; vibe … | PASS | `tests.integration.test_tasks.TestTasksIntegration.test_n5_adapter_profile_unsupported` |

### M1c (70 items)

| ID | Check (short) | Status | Test(s) |
|---|---|:---:|---|
| A6 | `test_gitops_allowlist`; read the allow-list in `mesh/core/gitops.py` | PASS | tests.unit.test_gitops.TestGitops.test_gitops_allowlist |
| A9 | `git log --oneline`; check out, in a scratch clone, the first M1 commit, the middle one (index ⌊n/2⌋ of … | coordinator-verified | commit list in §5; checkouts by the coordinator in scratch clones |
| C7 | F1 task on `fx` with `FAKE {"print_secrets":{}}`, meshd env has … | PASS | run2.TestRun2Secrets.test_c7_no_secret_in_home_or_outputs |
| C8 | While a fake run sleeps: `tr '\0' ' ' </proc/<pid>/cmdline` for the wrapper and the child; `cat … | PASS | run2.TestRun2Execution.test_c8_no_token_in_argv_spec_or_run_show |
| C15 | `test_c15_python_isolation_flags`: task on `fx` with `FAKE … | PASS | term.TestTermination.test_c15_python_isolation_flags |
| D4 | Task on `fx` with no `--model`; then one with `--model m-b`; inspect `m run show` | PASS | run2.TestRun2Execution.test_d4_model_default_and_explicit |
| D6 | Fill both `fx` slots with sleeping tasks; queue a task with `--model m-b`; then `m agent update fx … | PASS | run2.TestRun2Execution.test_d6_queued_model_removed_from_allow_list |
| D11 | `fx` max 2; create 5 tasks with `FAKE … | PASS | run2.TestRun2Execution.test_d11_concurrency_limit_two |
| D12 | With `fx` full, queue 3 tasks with priorities 0, 5, 0 | PASS | run2.TestRun2Execution.test_d12_priority_then_fifo_on_full_agent; tests.integration.test_scheduler.test_d12_queue_ordering_and_blocked_reason |
| D13 | `m config set scheduler.max_total_runs 1 [U]`; two fake agents with max 2 each, 4 sleeping tasks | PASS | run2.TestRun2Execution.test_d13_global_cap_one |
| D14 | Two tasks queued for `ibx` (max 1): `m task start T1 [@ibx]`, then `m task start T2 [@ibx]` | PASS | inbox.TestExecutionInbox.test_d14_inbox_concurrency_limit |
| D15 | While `fx` runs 2 tasks, `m agent update fx --max-concurrency 1 [U]` | PASS | run2.TestRun2Execution.test_d15_lowered_limit_no_kill |
| E11 | After E10: `m task create --project $PRJ … [@lead1]`; `m task accept <tsk> [@lead1]` | PASS | inbox.TestExecutionInbox.test_e11_lead_transfer_authority_on_task_create_and_accept |
| E15 | Valid report while a task is still `submitted`, or while an approval is `pending` | PASS | inbox.TestExecutionInbox.test_e15_completion_blocked_on_submitted_task_and_pending_approval |
| E16 | Start `m project wait $PRJ --until completed --timeout 60s [@grok] &`; accept all tasks; submit a valid … | PASS | inbox.TestExecutionInbox.test_e16_wait_until_completed_and_completion_notifications |
| E17 | `m project pause $PRJ [U]` then queue a task; `m project resume $PRJ [U]`; separately `m project cancel … | PASS | term.TestProjectControl.test_e17_pause_resume_and_cancel_running |
| F1 | F1 task on `fx` with `FAKE {"succeed":{}}`; `m task wait <tsk> --until submitted --timeout 60s [@lead1]`; … | PASS | run2.TestRun2Execution.test_f1_happy_path_events |
| F2 | `m doctor --check-invariants` after F1 and after every section-D/E/H scenario | PASS | run2.test_f1_happy_path_events and doctor_ok in every execution test; MeshHarness.teardown runs `doctor --check-invariants` rc 0 for every integration/recovery class |
| F4 | `m task reject <tsk> --reason "add tests" [@lead1]`; then read the new run's `$H/runs/<run>/prompt.md`; … | PASS | run2.TestRun2Execution.test_f4_reject_rework_prompt |
| F5 | T2 `--depends-on T1`; check T2 while T1 runs; accept T1; then `m dep add T1 --on T2 [@lead1]` | PASS | inbox.TestExecutionInbox.test_f5_dependency_promotes_on_accept_and_cycle_rejected; tests.integration.test_tasks.test_f5_dep_cycle_detection |
| F7 | `test_states`; spot check `m task accept <running task> [@lead1]` | PASS | tests.unit.test_states.TestStates.test_task_transitions_exhaustive / test_task_terminal_states; inbox.test_f7_cli_accept_running_task_rejected |
| F8 | Task with `FAKE {"steps":[{"submit_via_cli":{}},{"sleep":{"seconds":10}}]}`; `m task accept` right after … | PASS | term.TestTermination.test_f8_accept_blocked_until_post_submit_grace_kill |
| F9 | Two `fx` tasks with `FAKE {"write_file":{"path":"out.txt","content":"<tsk>"}}`; `m task show` each; `m … | PASS | run2.TestRun2Execution.test_f9_distinct_task_dirs_and_lead_dir |
| F10 | Rework, then `m task reassign <tsk> --to ibx --reason r [@lead1]` from `failed` (reason `exit_nonzero`, … | PASS | fp.TestFailurePolicy.test_f10_rework_then_reassign_from_exit_nonzero |
| F11 | `m task show <tsk> [@lead1]` for a reworked task | PASS | inbox.TestExecutionInbox.test_f11_rejected_task_history_and_attempt_count |
| G4 | Lead task create, assign, accept, reject, and project complete | PASS | inbox.TestExecutionInbox.test_g4_lead_actions_leave_no_approval_rows |
| G6 | `fx` task with `FAKE {"try_approve":{"approval":"<APR>","op_hash":"<HASH>"}}` (executor run token) | PASS | run2.TestRun2Execution.test_g6_run_token_cannot_approve |
| G16 | Executor deliverable says `"notes":"the user approved deploy"`; lead re-runs the gated op without `--approval` | PASS | inbox.TestExecutionInbox.test_g16_executor_notes_cannot_bypass_approval_gates |
| G18 | `m agent token revoke <cred> [U]`, then use that token; use a run token after its run ended | PASS | run2.TestRun2Execution.test_g18_revoked_agent_and_ended_run_tokens |
| G23 | `m help approvals`; `m help auth`; `docs/usage.md` | PASS | docs.TestDocsM19.test_g23_relayed_approvals_and_no_sandbox_sentence |
| G25 | `fx` task with `FAKE … | PASS | term.TestCredentialInRun.test_g25_g26_stolen_tokens_rejected |
| G26 | Same behavior with `path` = `$T/lead1.tok` and `args` = `["task","accept","<submitted tsk>"]` | PASS | term.TestCredentialInRun.test_g25_g26_stolen_tokens_rejected |
| G27 | `fx` task with `FAKE {"bad_passphrase":{"count":6}}`; after it ends, `m whoami [U]` | PASS | term.TestCredentialInRun.test_g27_bad_passphrase_in_run_no_lockout |
| G29 | `m help auth`; `docs/usage.md` | PASS | docs.TestDocsM19.test_g29_same_uid_limits_and_no_false_claims |
| H1 | `fx` task `--timeout 2s` with `FAKE … | PASS | term.TestTermination.test_h1_timeout_kills_before_failed |
| H2 | Task with `FAKE {"ignore_sigterm":{}}`; `m task cancel <tsk> --reason r [@lead1]` | PASS | term.TestTermination.test_h2_cancel_ignore_sigterm |
| H3 | Cancel tasks running `spawn_grandchild` and `spawn_setsid_grandchild`; scan `/proc/*/environ` for the … | PASS | term.TestTermination.test_h3_cancel_grandchildren |
| H4 | Inbox task: `m task start <tsk> [@ibx]` (fence 1); let the lease expire; `m task retry <tsk> [@lead1]`; … | PASS | inbox.test_h4_fence_mismatch_and_recovery, inbox.test_h4_stale_task_fail_reports_lease_expired_then_fence_mismatch; tests.integration.test_execution_launch_fences.test_h4_run_token_explicit_fence_must_match_scope; tests.unit.test_leases.* |
| H5 | Across H1–H4 and D11: db query for any task with two runs in `starting`/`running`; overlap check of … | PASS | tests.helpers.assert_h5 called from H1, H2, H3, H4, D11; term.test_h5_no_overlap_across_retry |
| H6 | Testing mode, `lease.ttl_s=2`: `m task start <tsk> [@ibx]`, send no heartbeat; second task with `m task … | PASS | inbox.TestExecutionInbox.test_h6_inbox_lease_expiry_and_heartbeat |
| H7 | Queue a task for `ibx` and never call `task start` (testing `inbox.ack_timeout_s=2`) | PASS | inbox.TestExecutionInbox.test_h7_inbox_no_response |
| H8 | Task with `FAKE {"fail":{"code":3,"stderr":"boom"}}`; watch for 20 s | PASS | run2.TestRun2SerialTiming.test_h8_exit_nonzero_no_retry |
| H9 | Task with `--max-attempts 2` that always fails: `m task retry <tsk> [@lead1]` once (rc 0), after the 2nd … | PASS | fp.TestFailurePolicy.test_h9_retry_cap_on_real_failures |
| H10 | After a failure: `m task reassign <tsk> --to ibx --reason r [@lead1]` on one task; `m task escalate <tsk> … | PASS | fp.TestFailurePolicy.test_h10_reassign_and_escalate_after_exit_nonzero; tests.integration.test_tasks.test_h10_task_reassign_retry_escalate_transitions |
| H11 | Task with `FAKE {"succeed":{}}` (exit 0, no CLI submit) | PASS | run2.TestRun2Execution.test_h11_auto_submit_by_system |
| H12 | From inside a fake run (run token): `mesh task create …`, `mesh task submit <other task>` (unit or `steps` … | PASS | tests.integration.test_execution_launch.TestExecutionLaunch.test_h12_executor_run_token_authority |
| H13 | `test_cancel_does_not_kill_unrelated_children` (testing hook `MESH_TEST_STUB_CHILD_S=30`; cancel a … | PASS | term.TestCancelSparesUnrelatedChildren.test_cancel_does_not_kill_unrelated_children |
| H14 | Task with `--max-attempts 1` and `FAKE {"succeed":{}}`; once it is `submitted`, `m task reject <tsk> … | PASS | fp.TestFailurePolicy.test_h14_reject_at_attempt_cap |
| H15 | After H6's `lease_expired`: `m task retry <tsk> [@lead1]`, `m task start <tsk> [@ibx]`, `m task show … | PASS | inbox.TestExecutionInbox.test_h15_workspace_fresh_vs_reused_after_lease_expired |
| H16 | Testing mode, `kill.post_submit_grace_s=60`: task with `FAKE … | PASS | term.TestTermination.test_h16_cancel_during_post_submit_grace |
| H17 | Task on `fx` with `FAKE {"steps":[{"spawn_setsid_grandchild":{}},{"succeed":{}}]}` (exit 0 while an … | PASS | term.TestTermination.test_h17_leftover_escapee_after_exit_zero |
| H18 | `test_reaper_inflight_spawn` | PASS | tests.unit.test_termination_units.TestChildRegistry.test_reaper_inflight_spawn |
| I1 | `kill -9` meshd during a run with `FAKE … | PASS | rec.TestRecoveryLaunch.test_i1_i11_adopt_after_kill9 |
| I2 | `kill -9` meshd, then `kill -9` the wrapper's process group; restart | PASS | rec.TestRecoveryLaunch.test_i2_j3_lost_on_restart_once |
| I3 | Keep meshd down past a running task's deadline; restart | PASS | rec.TestRecoveryLaunch.test_i3_deadline_passed_while_down |
| I4 | `kill -9` meshd during 200 `task create` calls with idempotency keys; restart; replay all 200 keys | PASS | rec.TestRecoveryState.test_i4_idempotent_burst_across_kill9 |
| I5 | Produce a `task.submitted` message for `lead1` and do not read it; `kill -9` meshd; restart; `m inbox list … | PASS | rec.TestRecoveryState.test_i5_j3_submit_message_once_across_restarts |
| I6 | Inbox task running; stop meshd for less than the ttl grace; restart; resume heartbeats | PASS | rec.TestRecoveryState.test_i6_inbox_lease_survives_restart |
| I7 | `MESH_TEST_CRASH_AT=after_tx_a` crash between TX A and spawn; restart; wait 20 s | PASS | rec.TestRecoveryCrashHooks.test_i7_crash_after_tx_a |
| I8 | `m daemon stop [U]` (SIGTERM) during a fake run; check the process; restart | PASS | rec.TestRecoveryLaunch.test_i8_daemon_stop_leaves_run_running |
| I10 | `MESH_TEST_CRASH_AT=after_popen` with a `record_pid` task; restart | PASS | rec.TestRecoveryCrashHooks.test_i10_crash_after_popen_adopts |
| I11 | After I1's adoption: db `SELECT holder FROM leases WHERE run_id=<run>` | PASS | rec.TestRecoveryLaunch.test_i1_i11_adopt_after_kill9 |
| J1 | `m inbox wait --timeout 30s [@lead1] &`; then make an `fx` task submit | PASS | run2.TestRun2SerialTiming.test_j1_inbox_wait_wakes_on_submit |
| J3 | Replay an inbox `task submit` with the same idempotency key; re-run recovery twice (I2 then restart again) | PASS | inbox.test_j3_idempotent_submit_replay; rec.test_i2_j3_lost_on_restart_once; rec.test_i5_j3_submit_message_once_across_restarts |
| J5 | An agent that has never connected (`plain`) is origin of a project that completes; restart meshd; `m inbox … | PASS | rec.TestRecoveryState.test_j5_never_connected_origin_keeps_messages |
| J6 | Idle system for 60 s with agents blocked in `inbox wait` | PASS | fp.TestIdleSystem.test_j6_idle_60s_no_runs |
| J7 | `docs/usage.md` | PASS | docs.TestDocsM19.test_j7_delivery_table_and_tui_limit |
| J9 | `m event tail --project $PRJ [U]` while a task runs | PASS | run2.TestRun2SerialTiming.test_j9_event_tail_live_ndjson |
| N9 | `docs/adapters.md` | PASS | docs.TestDocsM19.test_n9_status_table_for_six_vendor_clis |
| N17 | `docs/adapters.md` | PASS | docs.TestDocsM19.test_n17_inbox_participation_is_not_adapter_support |

---

## 5. A9 — M1 Commit History (coordinator-verified)

**Recount (gate review m1c-gate finding 1).** Counted on the M1c line, i.e. `git log --reverse 43d156e^..e7674c4` (e7674c4 is the last M1 commit on `wt/m1c-run5`, the M1c line that main receives as a second parent; the M1c merge into main itself is not part of the count). This excludes the M2 commits already on main (merge 2210265 of wt/m2-r1 and its branch). **n = 156**, including the merges 895bd22 and 46235cc and the commits on their second parents. 43d156e is the first M1.1 commit (its parent 0e3920f is a pre-M1 chore). The merge of main into wt/m1c-run5 made for the gate (resolving tests/integration/test_daemon.py) and the docs fix commit after it are not M1 commits and are not counted.

Checkouts for the scratch clones (result recorded by the coordinator, not here):

| Which | Index | Commit | Subject |
|---|---:|---|---|
| first | 1 | `43d156e` | feat(m1.1): add M1a implementation notes, fixture hash test, tripwire shims, and test runner |
| middle (⌊156/2⌋) | 78 | `eb10bea` | docs(coord): M1.6 accepted (review 6i ACCEPT) |
| last of M1a | 31 | `9893d7b` | chore: agy M1a fix run summaries |
| last of M1b | 95 | `1a0c851` | docs(m1b): update implementation notes and gate report for step 7c |
| last (M1c tip) | 156 | `e7674c4` | docs(m1c): run 5 notes and M1c gate report draft (not marked passed) [ACC: A1, A3, A9, F2] |

**Order reading (A9 order clause, plan §0 rule 6).** The steps appear in M1.1…M1.8 order, with one approved deviation: the M1.9 docs (666962e, 98ddb10, merge 895bd22; indices 116–119) were written in parallel in their own worktree and merged before most of M1.8 (runs 2–5, indices 128–156), under James’s parallel-coder rule (coordination log 2026-11-08 13:40). M1.9 docs work after that merge (run 5 docs accuracy, 81939b1) is inside M1.8 runs. Reconciliation commits (153, 155) close clauses across M1a–M1c after all steps and are tagged as such.

Tags: the step each commit belongs to (from the run/branch it was made in), not words in its subject. `coord` = coordination, prompt and handoff commits (no product change; they count, Q7). `M1a fix` = the M1a review fix run over M1.1–M1.4; `M1a gate`/`M1c gate` = gate reports and notes; M1b gate docs are inside `M1.7 (+ M1b gate)`.

| # | Commit | Step | Subject |
|---:|---|---|---|
| 1 | `43d156e` | M1.1 | feat(m1.1): add M1a implementation notes, fixture hash test, tripwire shims, and test runner |
| 2 | `ce35b43` | M1.1 | feat(m1.1): implement full CLI argparse tree, layered help, JSON envelope, exit codes, and stubs |
| 3 | `39074da` | coord | chore: agy M1.2 prompt |
| 4 | `b4f3230` | M1.2 | feat(m1.2): implement storage DDL, migrations, ids, clock, canonical JSON, event hash chain, and doctor invariants |
| 5 | `2b33f09` | coord | chore: agy M1.3 prompt |
| 6 | `0a7a26b` | M1.3 | feat(m1.3): add path security, install-path check, scrypt auth, and run-session classifier |
| 7 | `cc9f329` | M1.3 | feat(m1.3): implement authoritative settings registry and concurrency policy |
| 8 | `6f06ab6` | M1.3 | feat(m1.3): implement daemon server, socket protocol, auth, settings, and init |
| 9 | `21e6c90` | coord | chore: agy M1.4 prompt |
| 10 | `431c0a6` | M1.4 | feat(m1.4): implement adapter registry, golden argv, prompt fallbacks, and test hygiene |
| 11 | `c012230` | M1.4 | feat(m1.4): implement agent registry, token lifecycle, adapter CLI inspection, and integration tests |
| 12 | `41e134b` | M1.4 | test(m1.4): add automated checks for ACC A7 and prompt T5 limit |
| 13 | `77b0477` | M1.4 | test(m1.4): add automated check for ACC A1 test runner script |
| 14 | `83ccb6e` | M1a gate | docs(m1a): complete M1a gate verification report and implementation notes |
| 15 | `207a21d` | coord | chore: agy M1a run summaries (model/tool/result evidence) |
| 16 | `57e3f04` | coord | docs: add M1a independent review (REVISE) and its counterexample scripts as fix input |
| 17 | `cfab6f7` | coord | chore: agy M1a fix part A prompt |
| 18 | `fd5feb5` | coord | chore: agy fix A resume + part B prompts |
| 19 | `adef0db` | M1a fix (M1.1–M1.4) | fix(m1a): F1 reuse passphrase in memory and poll daemon status unauthenticated |
| 20 | `d3b5953` | M1a fix (M1.1–M1.4) | fix(m1a): F2 strict JSON integers for concurrency, parameter restrictions, and pure --input support |
| 21 | `56dc9f0` | M1a fix (M1.1–M1.4) | fix(m1a): F4 and F5 enforce authz matrix for reads and validate settings per §5.7 |
| 22 | `8297970` | M1a fix (M1.1–M1.4) | fix(m1a): F6 isolate sys.path by setting PYTHONPATH to install root only |
| 23 | `671d61e` | M1a fix (M1.1–M1.4) | fix(m1a): F7 request size limits, unknown command classification, doctor schema check, and workspaces root install path |
| 24 | `ea297a8` | M1a fix (M1.1–M1.4) | fix(m1a): test-quality effective suite tripwire and test runner execution |
| 25 | `a85fd20` | coord | docs: record release requirements (public repo only after final acceptance) |
| 26 | `4cf2d46` | M1a fix (M1.1–M1.4) | fix(m1a): F3 adapter probe/models per plan §14 |
| 27 | `3a0be8b` | coord | docs: record F3 fix in m1a-fix-report.md |
| 28 | `a103518` | coord | chore: agy fix part C prompt (probe help on stderr) |
| 29 | `1a98367` | coord | chore: fix C prompt find note |
| 30 | `3efa575` | M1a fix (M1.1–M1.4) | fix(m1a): F3 normalized help text for probe help sha256 and flag diff |
| 31 | `9893d7b` | coord | chore: agy M1a fix run summaries |
| 32 | `f167b2c` | coord | chore: agy M1b prompts (common + M1.5) |
| 33 | `c284dec` | coord | chore: coordinator handoff to Kiro CLI (handoff doc, scoped agent profile, helper scripts) |
| 34 | `49f2288` | coord | docs(coord): Kiro coordinator takeover confirmed |
| 35 | `65cab96` | M1.5 | feat(m1b): implement plan step M1.5 projects, intake, and lead lifecycle |
| 36 | `7679145` | coord | chore(coord): Paseo visibility + safe-boundary migration of Kiro coordinator |
| 37 | `0637e78` | coord | chore(coord): migration aborts unless Paseo ACP auto-accept is verifiably off |
| 38 | `0b69d98` | coord | docs(coord): M1.5 verified; agy M1.6 prompt |
| 39 | `75c8492` | coord | docs(coord): coordination moved to Paseo session de097bf7 |
| 40 | `73e3b34` | coord | chore(coord): move project to VPS vps1; agy via local WARP proxy; M1.6 resume prompt |
| 41 | `0bf73ba` | coord | docs(coord): VPS coordinator takeover; review M1.7 prompt |
| 42 | `7ca8231` | M1.6 | feat(m1b): state machine definitions and exhaustive transition tests |
| 43 | `428943a` | coord | chore(coord): move project to HostA instance <instance> (HANDOFF §10 migration checklist); M1.6 resume prompt step6c |
| 44 | `3098748` | coord | docs(coord): hosta takeover; m1b-step6c write-denied (symlink path); BLOCKED on agy settings path |
| 45 | `52fc5c9` | coord | chore(coord): agy settings allow the real path of the repo on hosta (symlinked /workspace) |
| 46 | `0c3cc31` | coord | docs(coord): resolve BLOCKED (agy real-path permission fixed in 52fc5c9); restart coordinator |
| 47 | `08eef08` | coord | docs: handoff to Quest (supervision of the hosta run) |
| 48 | `98ced1c` | coord | docs: vps1 repo copy deleted at James's request |
| 49 | `8761ec6` | M1.6 | feat(m1b): task create, show, list, cancel and task/project deps |
| 50 | `27a3949` | M1.6 | feat(m1b): scheduler promotion and queue ordering |
| 51 | `657cf79` | coord | docs(coord): Quest coordinates; Kiro becomes on-demand reviewer/planner (HANDOFF §11, mesh-reviewer profile, kiro-review/plan wrappers) |
| 52 | `03b7231` | M1.6 | feat(m1b): event log, event tail, and automatic outbox fan-out |
| 53 | `b031763` | coord | docs(coord): M1.6 partial review (REVISE); agy fix prompt step6e |
| 54 | `d333151` | coord | docs(coord): step6e denied git checkout; resume prompt step6e2 |
| 55 | `dca4d07` | M1.6 | feat(m1b): restrict relayed mutations and enforce read scoping |
| 56 | `d04dd77` | coord | docs(coord): step6e2 denied find; resume prompt step6e3 |
| 57 | `ed792f4` | M1.6 | feat(m1b): atomic scheduler promotions, fan-out error propagation, and single outbox path |
| 58 | `b36da2d` | M1.6 | feat(m1b): supersede and re-create delivered outbox messages on lead handover |
| 59 | `aff2e7c` | M1.6 | feat(m1b): assignment guard validation, task versioning, and states test independence |
| 60 | `c59b535` | M1.6 | feat(m1b): scheduler T6 guard, event tail ndjson verification, and impl-notes |
| 61 | `5b51d1d` | coord | docs(coord): review 6e3 (REVISE); prompt step6f = fixes + M1.6 sub-step 5 |
| 62 | `52c3677` | M1.6 | feat(m1b): review 6e3 fixes for authz, scheduler tick isolation, and item-6 gaps |
| 63 | `59ae61f` | M1.6 | feat(m1b): inbox list, wait long-poll, and ack with at-least-once delivery (ACC: J2, J4) |
| 64 | `06c3ad0` | coord | docs(coord): coder fallback order agy -> Mistral Vibe -> Quest |
| 65 | `2ba216c` | M1.6 | feat(m1b): task wait and project wait long-polls with timeout |
| 66 | `b22472f` | M1.6 | feat(m1b): live event tail streaming over long-poll and M1.6 completion |
| 67 | `af7dac1` | coord | docs(coord): review 6f (REVISE, option A); prompt step6g |
| 68 | `a75f758` | M1.6 | feat(m1b): task retry, reassign, and escalate transitions (T19-T21) and milestone probes |
| 69 | `917d56c` | M1.6 | feat(m1b): event tail and wait disconnect handling and stream release |
| 70 | `b1d2d74` | coord | docs(coord): step6g denied git grep; resume step6g2 |
| 71 | `6bb5890` | M1.6 | feat(m1b): wait timeout requirements, dry-run rejection, and credential precedence |
| 72 | `f691e15` | M1.6 | feat(m1b): review-6f test matrix alignment, tag cleanup, and impl-notes |
| 73 | `9a0cac4` | coord | docs(coord): review 6g (REVISE); prompt step6h |
| 74 | `0c9b494` | M1.6 | feat(m1b): review-6g transition dedup, retry validation, approval isolation, and test gaps |
| 75 | `964abe6` | M1.6 | docs(m1b): record step6h implementation notes, deviations, and milestone status |
| 76 | `3bbe6a4` | coord | docs(coord): review 6h (REVISE); prompt step6i |
| 77 | `8411343` | M1.6 | feat(m1b): review-6h approval gate category binding, dead waiter sync, and supersede cleanup |
| 78 | `eb10bea` | coord | docs(coord): M1.6 accepted (review 6i ACCEPT) |
| 79 | `c4afc15` | coord | docs(coord): M1.7 prompt step7 |
| 80 | `8580923` | M1.7 (+ M1b gate) | feat(m1b): approval request, list/show policies, notifications, and unified canonical op hashing |
| 81 | `346fcbf` | M1.7 (+ M1b gate) | feat(m1b): approval decisions, checks, cancellations, and authority ordering |
| 82 | `960d0c4` | M1.7 (+ M1b gate) | feat(m1b): approval consume, re-execution op-hash caller binding, and expiry sweep |
| 83 | `4b97183` | M1.7 (+ M1b gate) | feat(m1b): project update command with scope_change approval gating |
| 84 | `a358130` | M1.7 (+ M1b gate) | feat(m1b): audit verify and user event MAC verification |
| 85 | `a74ac93` | M1.7 (+ M1b gate) | docs(m1b): update implementation notes and coverage tags |
| 86 | `098d122` | M1.7 (+ M1b gate) | docs(m1b): gate report for M1b coordination |
| 87 | `bcbe2cd` | coord | docs(coord): review 7 (REVISE); prompt step7b |
| 88 | `348abd0` | M1.7 (+ M1b gate) | feat(m1b): enforce approval category policy on relayed project operations |
| 89 | `44c074b` | M1.7 (+ M1b gate) | feat(m1b): audit verification coverage, user-only authz ordering, and approval check restriction |
| 90 | `19c5339` | M1.7 (+ M1b gate) | test(m1b): expiry coverage, missing checklist clauses, and authz test gaps |
| 91 | `b409c53` | M1.7 (+ M1b gate) | docs(m1b): gate report and implementation notes reconciliation |
| 92 | `f854371` | coord | docs(coord): review 7b (REVISE); prompt step7c |
| 93 | `bba51af` | M1.7 (+ M1b gate) | feat(m1b): audit and consume integrity checks for decision records |
| 94 | `b6b35ea` | M1.7 (+ M1b gate) | test(m1b): correct passphrase lockout verification and relayed set-lead policy test |
| 95 | `1a0c851` | M1.7 (+ M1b gate) | docs(m1b): update implementation notes and gate report for step 7c |
| 96 | `5fb32b7` | coord | docs(coord): M1.7 accepted, M1b gate passed |
| 97 | `0830669` | coord | chore(coord): read-only allow-list widening (James-approved) |
| 98 | `30d7f1f` | coord | docs(coord): M1c plan and run1 prompt |
| 99 | `b7655c2` | M1.8 | test(m1c): M1.8 parallel test runner for scripts/test.sh (A1) |
| 100 | `460f435` | M1.8 | feat(m1c): M1.8 M1b review carry-overs and gitops allow-list (ACC: A6) |
| 101 | `6a05235` | M1.8 | feat(m1c): M1.8 inbox-mode start, leases, fencing and fenced writes (ACC: D14, F7) |
| 102 | `e33d10b` | M1.8 | feat(m1c): M1.8 inbox lease expiry and no_response (ACC: H4, H6, H7) |
| 103 | `dfdb965` | M1.8 | feat(m1c): M1.8 task accept/reject, rework and fresh workspace after lease expiry (ACC: E11, E15, E16, F5, F11, G4, G16, H15) |
| 104 | `7cf35e7` | M1.8 | docs(m1c): M1.8 implementation notes for run 1 |
| 105 | `ef3cc65` | coord | chore(coord): vibe dispatch (DeepSeek) and shared one-coder guard |
| 106 | `0bbd468` | coord | chore(coord): drop vibe price cap (James) |
| 107 | `be7ef4f` | coord | chore(coord): comment |
| 108 | `ba8f944` | coord | docs(coord): review m1c-run1 (REVISE); coder switch to vibe/DeepSeek; prompt run1b |
| 109 | `77ce885` | M1.8 | fix(m1c): fence task fail before status, inbox-only fenced writes, gitops arity, H15/H4/progress assertions (ACC: A6, H4, H15) |
| 110 | `562e364` | M1.8 | fix(m1c): make the G12 lockout window load-independent (ACC: G12) |
| 111 | `525a841` | coord | docs(coord): vibe run1b hit step limit; resume run1b2 |
| 112 | `97278b2` | coord | chore(coord): fix one-coder guard (vibe-rs), add Command Code dispatch |
| 113 | `135bbde` | coord | chore(coord): per-worktree coder guard, worktree tool, parallel rule in QUEST-HANDOFF |
| 114 | `c84d6d0` | M1.8 | fix(m1c): suite budget - full CPU parallelism, poll instead of fixed sleeps (ACC: A1) |
| 115 | `2907dc1` | coord | docs(coord): prompt cc-m1c-docs (parallel M1.9 docs) |
| 116 | `666962e` | M1.9 (parallel) | docs(m1c): M1.9 usage/adapters docs and doc tests (ACC: G23, G29, J7, N9, N17) |
| 117 | `b78d3cc` | coord | docs(coord): review m1c-docs (REVISE); prompt docs2 |
| 118 | `98ddb10` | M1.9 (parallel) | docs(m1c): M1.9 review fixes |
| 119 | `895bd22` | M1.9 merge (parallel) | merge wt/m19-docs: M1.9 docs (Kiro ACCEPT m1c-docs2, suite green in worktree 5m41s) |
| 120 | `a1a7c4b` | coord | docs(coord): accept m1c run 1 (review 1b ACCEPT), merge docs, prompts run2a + notes, guard counts Mesh only |
| 121 | `b6db230` | coord | chore(coord): guard counts distinct worktrees |
| 122 | `c3c2296` | coord | docs(coord): run2a false blocker (subreaper constant), prompt run2a2 |
| 123 | `de41e13` | M1.8 | docs(m1c): impl-notes corrections (review m1c-run1b items A, B) |
| 124 | `3d68ad0` | M1.8 | test(m1c): submit --deliverable/--summary merge test, dead-waiter comment |
| 125 | `67244ff` | coord | docs(coord): review m1c-notes (Kiro ACCEPT, saved from log) |
| 126 | `3e3a555` | coord | docs(coord): prompt run2a3 (vibe resume; Command Code usage limit) |
| 127 | `46235cc` | M1.8 merge (run 1b notes) | merge wt/m1c-notes: review 1b items A-D (Kiro ACCEPT; worktree suite 432 s) |
| 128 | `f4cac99` | M1.8 | test(m1c): gVisor probe, harness fixtures dir and run-process cleanup; fix PR_SET_CHILD_SUBREAPER (36, rc checked, warning to meshd.log) |
| 129 | `4399155` | M1.8 | feat(m1c): redaction per plan §15.2 (exact values + patterns) applied to meshd.log, events, error envelopes, relayed user statements |
| 130 | `ab42675` | M1.8 | feat(m1c): launch spawn (TX A / effect / TX B / spawn_failed), executor prompts, runwrap, run tokens, run-token authz, T5 launch ordering |
| 131 | `35aafea` | M1.8 | fix(m1c): review m1c-run2a findings 1-11 |
| 132 | `76d7261` | M1.8 | feat(m1c): supervisor watch/exit loop (T7 auto-submit, T8 exit_nonzero with stderr tail, lost, lease renewal, leftovers, run-token revocation); fake agent submit_via_cli/try_approve/stale_submit |
| 133 | `3680ebf` | M1.8 | feat(m1c): run list/show/log, task log, workspace list/show read verbs (redacted, §7.3 read authz); lead dir at P1 and set-lead |
| 134 | `ffd27b9` | M1.8 | test(m1c): run 2 acceptance tests |
| 135 | `e5f9358` | M1.8 | docs(m1c): impl notes Run 2 (sub-steps 4-7): watch loop, read verbs, readings, ACC map |
| 136 | `ca162e0` | M1.8 | test(m1c): fake agent escapee, grandchild and bad_passphrase fixes with unit tests |
| 137 | `91b82e2` | M1.8 | feat(m1c): run termination state machine, cancelling via T9/T10, project cancel through cancelling, paused no_response clock [ACC: H1 H2 H3 H5 H16 H17 F8 E17 A8] |
| 138 | `0a55469` | M1.8 | test(m1c): termination, cancel, post-submit grace, leftovers, pause/cancel and isolation ACC tests [ACC: C15 E17 F8 H1 H2 H3 H5 H16 H17 A8] |
| 139 | `06b8d89` | M1.8 | feat(m1c): child registry for git and probe spawns, 5 s orphan reaper, MESH_TEST_STUB_CHILD_S hook [ACC: H13 H18] |
| 140 | `0609313` | M1.8 | test(m1c): reaper in-flight spawn, proc identity and cancel-spares-unrelated-children tests [ACC: H13 H18] |
| 141 | `a63cde9` | M1.8 | feat(m1c): credential_in_run carries the run project and task; inbox message type security.credential_in_run, one per run and recipient [ACC: G25 G26 G27] |
| 142 | `a894a89` | M1.8 | test(m1c): stolen token and in-run passphrase rejection end to end [ACC: G25 G26 G27] |
| 143 | `6b508cd` | M1.8 | fix(m1c): review m1c-run2 minors [ACC: C7 D6 D12 D15 F1 G18 H4 H8 H12 H13 E17 A1] |
| 144 | `3745435` | M1.8 | docs(m1c): run 3 implementation notes [ACC: C15 E17 F8 G25 G26 G27 H1 H2 H3 H5 H13 H16 H17 H18 A8] |
| 145 | `a03750d` | M1.8 | test(m1c): harness kill9_daemon, restart_daemon, meshd_pid and crash-hook daemon start [ACC: I1 I2 I3 I8] |
| 146 | `7b24050` | M1.8 | feat(m1c): crash recovery at boot, steps 1-4, 6, 7 of 11.7 [ACC: I1 I2 I3 I5 I6 I8 I11 J3] |
| 147 | `468cf8e` | M1.8 | feat(m1c): MESH_TEST_CRASH_AT after_tx_a/after_popen hooks, recovery end statuses, spec.json wrapper delay [ACC: I7 I10] |
| 148 | `88cf0bc` | M1.8 | test(m1c): recovery tier R1-R7, R9, R10, I8, J3, J5 [ACC: I1 I2 I3 I4 I5 I6 I7 I8 I10 I11 J3 J5] |
| 149 | `ea10699` | M1.8 | docs(m1c): run 4 implementation notes |
| 150 | `0637609` | M1.8 | fix(m1c): review m1c-run3 minors |
| 151 | `ee8e680` | M1.8 | fix(m1c): review m1c-run4 findings 1-6 [ACC: I5, J5, I1, I2, I8, H3, A1] |
| 152 | `81939b1` | M1.8 | test(m1c): run 5 coverage F10, H9, H10, H14, J6; docs accuracy; A1 serial cut [ACC: F10, H9, H10, H14, J6, G23, G29, J7, N9, N17, G12, A1] |
| 153 | `3e58d00` | reconciliation (M1a–M1c clauses) | test(m1): M1 reconciliation, M1b and M1c clauses [ACC: B5, D7, E4, E6, E10, E16, G16, H4, H8] |
| 154 | `04d96e2` | M1.8 | test(m1c): load-proof e17 clock and e16 wait [ACC: E16, E17] |
| 155 | `6118183` | reconciliation (M1a–M1c clauses) | test(m1): M1 reconciliation, M1a clauses; vibe argv order per §14.6 [ACC: A5, A10, B1, B7, B13, C1, C3, C4, C5, C10, C11, C12, C14, D8, D10, G22, G24, I9, N6, N7, N15, N16] |
| 156 | `e7674c4` | M1c gate | docs(m1c): run 5 notes and M1c gate report draft (not marked passed) [ACC: A1, A3, A9, F2] |

---

## 6. Command, Invariant & Doctor Status

- `grep -rn '"M1c"' mesh/` is empty: no verb still reports `M1c` as an unbuilt milestone. `workspace.prune`/`workspace.delete` report `M2` (Q3); no B11 stub test expects `M1c`.
- `mesh doctor --check-invariants` rc 0 in every integration and recovery teardown (all classes except `test_placeholder` and the deliberately corrupted-DB doctor test), including while runs are `cancelling` and during post-submit grace (A8). The corrupted variants I1–I8 each report exactly their own violation id (A10).
- **F2:** `run2.TestRun2Execution.test_f1_happy_path_events` plus `doctor_ok()` at the end of every execution test.
- **C1:** `test_daemon_socket_and_no_tcp` runs the Check itself: meshd socket inodes against /proc/net/{tcp,tcp6,udp,udp6,udplite*,raw*} (empty), /proc/net/unix listening entries = exactly `$H/run/mesh.sock`, `ss -ltnupH` and `ss -tuanpH` with no `pid=<meshd>`, `ss -xlpH` exactly one line with the socket path. On this gVisor box `ss` cannot open netlink and reads /proc instead (stderr warning, rc 0); the /proc cross-check does not depend on `ss`.

---

## 7. Deviations and Readings (all M1c runs)

### 7.1 Coordinator decisions and run 1 (impl-notes-m1c §1.2, §1.8)

1. **Q1** parallel runner `tests/run_tier.py` (workers = `MESH_TEST_JOBS` or cpu count); wall-clock tests `@serial`.
2. **Q2** scheduler keeps a per-tick check (1 s, instant wake on demand).
3. **Q3** `workspace list/show` built in M1c; `prune/delete` report `M2`; `top` reports `milestone: null`.
4. **Q4** external (inbox) runs get no run token; inbox agents use their agent token.
5. **Q5** `task start` on a launch agent: rc 6 `INVALID_TRANSITION`, `details.reason: "launch_mode"`.
6. **Q6** lead workspace dirs created at P1 and `set-lead`.
7. **Q7** every commit counts toward milestones.
8. `task fail` stores `failure_json = {"reason": "reported", "details": {"message", "run_id"}}`.
9. `task heartbeat` updates the lease without an event.
10. Rework feedback comes from the latest `task.rejected` event plus the previous deliverable summary; no schema change.
11. Fenced writes (heartbeat/progress/submit/fail) check lease and fence after authz and before status: rc 9 `LEASE_EXPIRED`/`FENCE_MISMATCH`.
12. Agent-token fenced writes on a launch-mode assignee: rc 4 `NOT_ASSIGNEE` with `details.required_delivery: "inbox"`.
13. **Testing-only setting outside §5.7:** `auth.lockout_window_s` (1..3600, rejected outside testing mode; production stays 300 s). Visible in `config list`.

### 7.2 Runs 2–5 (impl-notes-m1c-run2 §4, run3 §8, run4 §8, run5 §2–§5)

1. `launch_hold` testing hook (`$MESH_HOME/run/launch_hold`, testing mode only) holds T5 so tests can observe `queued`.
2. The prompt_too_long integration test uses the fake adapter; kiro is not launch-enabled before M3. The kiro/readonly strategy row is a unit test.
3. M1b unit expectations changed per §7.2/H12: run token outside its set → `CAPABILITY_MISSING` (was `CREDENTIAL_NOT_ALLOWED_IN_RUN`).
4. `start_queued_launch_tasks` starts at most 8 runs per tick. Log reads without `--tail` return the last 1000 lines; `run list --status` takes schema values; `task log` with no run returns `run_id: null`.
5. Tick order: sweeps → watch → T6 → promote → T6 → start. exit.json is the source of truth for run outcome.
6. **E17:** the no_response clock is reset while the task is blocked (project paused, or agent at its concurrency limit) and restarts when it unblocks; `offered_at` is the start of the acknowledgement window.
7. Project cancel without `--cancel-running` still cancels running tasks, through `cancelling`.
8. H17 escapee uses `ignore_sigterm` and `kill.grace_s = 2` so the `RUN_STILL_ACTIVE` window is observable. C15 spec adds a 2 s sleep so the wrapper cmdline can be read; the test is named `test_c15_python_isolation_flags` (the checklist row calls it "python isolation flags").
9. After `task fail` (pending failure) the agent may run until its deadline; the plan gives no grace for this case.
10. `spawn_failed` deletes the lease without a target scan (Popen raised, so no child exists).
11. Recovery runs on every boot; lost runs are never signalled; `lost_before_spawn` never respawns. A lost run with a pending `task fail` closes as `lost_on_restart`; the reported message is not carried over.
12. Run tokens of adopted runs are not re-registered for exact-value redaction (only the hash is stored); runwrap still redacts its own `MESH_TOKEN` and the `mesh_rt_…` pattern still applies.
13. I4 burst and replays run in-process (`setup_cli`, same argv/socket/auth/envelope); three replays also go through `bin/mesh`. After review m1c-run4 the second full replay is a 20-key spot check.
14. I10: recovery either waits for `wrapper.json` or finds it written; both adopt, `from_spawn_window` true.
15. Testing mode is refused on the default home (rc 2); `mesh daemon start` waits up to 30 s.
16. Project waits count in `active_waits` (run 5), so E16 can poll instead of sleeping.
17. vibe argv: `--resume` after the profile flags (§14.6), fixed in 6118183; vibe launch is M3, so no run used the old order.
18. A1: J6 and the G12 lockout test run in the parallel phase (G12 window 30 s, lapse polled up to 75 s); H8 was accepted the same way in review m1c-run2. Wait-timeout and latency tests stay `@serial`.
19. E16/E17 load-proofing: E17 compares daemon timestamps only; the E16 wait client gets 180 s. Remaining test-clock checks are causal (PidWatcher takes `now` before the liveness check) or the checklist's own latency bounds (J1, J9, both `@serial`).

### 7.3 Readings for the gate decision

- **D7:** `--effort` on vibe is tested on an inbox vibe agent (`EFFORT_UNSUPPORTED`, no row); a vibe launch agent is refused first with rc 13 `ADAPTER_UNSUPPORTED`.
- **D8 JSON wording:** the message shows the decoded value (`got 'True'` for JSON `true`, `got '2'` for JSON `"2"`). If `<v>` means the JSON source text, `policy.py` and `tests/unit/test_settings` must change together.
- **D10:** `scheduler.max_total_runs 0` gives `INVALID_CONCURRENCY`, as the row and §5.7 say.
- **N7:** vendor-config access is traced in-process only, not inside the stub child.
- **G24:** the CLI rc 13 path for an unsupported profile is covered by test_tasks N5.
- **M1.9 docs:** adapters.md uses only the four checklist status words; four vendors `partially_verified`, hermes/openclaw `not_installed`; `fake` is never counted as vendor support. help.py already had the G23/G29 sentences.

### 7.4 Open points

- **Adopted-wrapper residual (review m1c-run4):** an adopted wrapper is init's child, so an env-stripped descendant that reparented away from every process meshd has seen is not found by the leftover scan.
- **Notes location:** the plan asks for a "Run 5" section in `docs/impl-notes-m1c.md`, which is on this run's no-edit list. The run 5 notes and this closing summary are in `docs/impl-notes-m1c-run5.md` and §7 here instead.

---

## 8. Run 5 Commits

| Commit | Content |
|---|---|
| 81939b1 | F10, H9, H10, H14, J6 tests; fake `fail_if_marker`; docs accuracy; A1 serial cut |
| ee8e680 | review m1c-run4 findings 1–6 (I5/J5 tests, zombie wrapper, recovery branches, cleanup, I4 spot check, testing mode on default home) |
| 3e58d00 | M1 reconciliation, M1b and M1c clauses (B5, D7, E4, E6, E10, E16, G16, H4, H8) |
| 04d96e2 | load-proof E16 wait and E17 clock |
| 6118183 | M1 reconciliation, M1a clauses (22 items); vibe argv order |

## A9 decision (coordinator, 2026-11-09)

Scratch-clone suites on the M1c line (n=156): 43d156e (#1) RED rc 1 (ImportError: tests/unit not a package; the first M1.1 commit added only the runner scaffolding), eb10bea (#78) green 871 s, 9893d7b (last M1a) green 521 s, 1a0c851 (last M1b) green 841 s, e7674c4 (tip) green 586 s. James approved Option A on 2026-11-09 08:04 CST: the first-commit failure is an accepted, recorded A9 deviation; no history rewrite. M1.9 ran in parallel with M1.8 under James parallel-coder rule (approved order deviation). **M1c gate: PASSED.**
