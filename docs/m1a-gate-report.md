# Milestone Gate M1a Verification Report

Status: **PASS**  
Gate: **M1a Foundation** (Plan §16 steps M1.1–M1.4; Checklist §0.1 M1a items)  
Verifier: agy (Gemini 3.8 Flash High)  
Date: 2026-11-07  

---

## 1. Executive Summary

Milestone gate M1a covers the core foundational layer of Agent Mesh v1: CLI entry point, exit code and JSON envelope conventions, storage layer (SQLite schemas, STRICT tables, migrations, invariants, event hash chain), daemon lifecycle (Unix domain socket, single-instance flock, peer credentials, session classification, user auth, scrypt, settings, path guards), agent and token registry, and adapter registry with read-only inspection for all six CLIs plus fake test double and inbox-only adapters.

Every checklist item assigned to gate M1a in `docs/acceptance-checklist.md` §0.1 has been implemented and verified with automated tests carrying `ACC: <ID>` docstrings. The test suite passes 100% offline without live vendor calls or network access, and leaves no temporary directories behind.

- **Total M1a items:** 46
- **PASS:** 46
- **FAIL:** 0
- **NOT-COVERED:** 0

---

## 2. Gate Verification Table

| ID | Checklist Summary | Status | Test Name(s) |
|---|---|:---:|---|
| **A1** | `scripts/test.sh` offline, exit 0 under 10 min | **PASS** | `tests/unit/test_skeleton.py::test_test_runner_script` |
| **A2** | Python stdlib only; no third-party imports | **PASS** | `tests/unit/test_skeleton.py::test_stdlib_only`<br>`tests/unit/test_paths.py::test_stdlib_only` |
| **A3** | No real vendor CLI invoked (tripwire clean) | **PASS** | `tests/unit/test_skeleton.py::test_tripwire_shims` |
| **A4** | No references to `/workspace/agent-mesh*` | **PASS** | `tests/unit/test_skeleton.py::test_no_workspace_agent_mesh_references` |
| **A5** | Linux local only; no `--host`, `--port`, `--url` | **PASS** | `tests/unit/test_skeleton.py::test_no_forbidden_host_port_flags` |
| **A7** | No code in `mesh/` opens vendor credential/config paths | **PASS** | `tests/unit/test_skeleton.py::test_no_vendor_credential_paths` |
| **A8** | `mesh doctor --check-invariants` exit 0 on clean DB | **PASS** | `tests/unit/test_invariants.py::test_invariants_valid_states`<br>`tests/unit/test_doctor.py::test_doctor_check_invariants_fresh_home`<br>`tests/unit/test_doctor.py::test_doctor_check_invariants_initialized_clean_db`<br>`tests/unit/test_doctor.py::test_doctor_check_invariants_corrupted_db` |
| **A10** | Database invariants check detects corrupted states (I1–I8) | **PASS** | `tests/unit/test_invariants.py::test_invariants_valid_states`<br>`tests/unit/test_invariants.py::test_corrupted_i1_lease_on_accepted_task`<br>`tests/unit/test_invariants.py::test_corrupted_i2_fence_mismatch`<br>`tests/unit/test_invariants.py::test_corrupted_i3_slots_ge_limit`<br>`tests/unit/test_invariants.py::test_corrupted_i4_status_mismatch`<br>`tests/unit/test_invariants.py::test_corrupted_i5_lead_assignments`<br>`tests/unit/test_invariants.py::test_corrupted_i6_consumed_approval`<br>`tests/unit/test_invariants.py::test_corrupted_i8_cancelling_task_without_lease` |
| **B1** | Layered `--help` and help topics contain `Examples:` | **PASS** | `tests/unit/test_skeleton.py::test_help_layers_and_examples` |
| **B2** | Standard JSON envelope on error (`ok:false`, `error.code`) | **PASS** | `tests/integration/test_agents.py::test_agent_show_not_found`<br>`tests/unit/test_skeleton.py::test_argparse_errors_envelope` |
| **B7** | `--format text` and default JSON return identical rc | **PASS** | `tests/unit/test_skeleton.py::test_format_flag_identical_rc` |
| **B8** | `mesh status` when daemon stopped returns rc 10 `DAEMON_UNAVAILABLE` | **PASS** | `tests/integration/test_daemon.py::test_daemon_status_and_stop` |
| **B9** | Optimistic concurrency mismatch returns rc 6 `VERSION_MISMATCH` | **PASS** | `tests/integration/test_agents.py::test_agent_update_version_mismatch` |
| **B10** | Agent created without profile defaults to edit / dev | **PASS** | `tests/integration/test_agents.py::test_agent_default_profiles` |
| **B13** | Config CRUD and validation per plan §5.7 | **PASS** | `tests/integration/test_daemon.py::test_config_crud_and_validation`<br>`tests/unit/test_settings.py::test_settings_registry` |
| **C1** | Unix domain socket only at `$H/run/mesh.sock`; no TCP/UDP | **PASS** | `tests/integration/test_daemon.py::test_daemon_socket_and_no_tcp` |
| **C2** | No network socket code in `mesh/` | **PASS** | `tests/unit/test_skeleton.py::test_no_network_code`<br>`tests/unit/test_paths.py::test_no_network_code` |
| **C3** | Filesystem permissions: dirs 0700, db/log/sock 0600 | **PASS** | `tests/integration/test_daemon.py::test_file_permissions` |
| **C4** | Insecure home permissions reject start; doctor fixes | **PASS** | `tests/integration/test_daemon.py::test_insecure_home_permissions`<br>`tests/unit/test_paths.py::test_home_permissions` |
| **C5** | Second daemon on same home returns rc 1 `DAEMON_ALREADY_RUNNING` | **PASS** | `tests/integration/test_daemon.py::test_daemon_already_running` |
| **C6** | `SO_PEERCRED` uid mismatch rejected before dispatch | **PASS** | `tests/unit/test_auth.py::test_peer_cred_check_mismatched_uid` |
| **C9** | Agent token issue/list stores only hash in DB; secret shown once | **PASS** | `tests/integration/test_agents.py::test_agent_tokens`<br>`tests/unit/test_auth.py::test_token_generation_and_verification` |
| **C10** | Token file with permissions > 0600 returns rc 3 `TOKEN_FILE_INSECURE` | **PASS** | `tests/unit/test_paths.py::test_token_file_secure` |
| **C11** | Daemon request audit log never records `"auth"` secrets | **PASS** | `tests/integration/test_daemon.py::test_audit_log_never_contains_auth` |
| **C12** | Token file located inside workspaces returns rc 3 `TOKEN_FILE_INSECURE` | **PASS** | `tests/unit/test_paths.py::test_token_file_secure` |
| **C13** | Scrypt parameters match plan §7.1 (`n=2**15, r=8, p=1, dklen=64`) | **PASS** | `tests/unit/test_auth.py::test_scrypt_params` |
| **C14** | Install path inside workspaces or vice versa rejected bidirectionally | **PASS** | `tests/integration/test_daemon.py::test_install_path_check_in_workspace`<br>`tests/unit/test_paths.py::test_install_path_check` |
| **D1** | Agent create stores default model and allowed models list | **PASS** | `tests/integration/test_agents.py::test_agent_create_and_show` |
| **D2** | Default model not in allowed list rejected; launch agent requires models | **PASS** | `tests/integration/test_agents.py::test_agent_create_invalid_models` |
| **D5** | Agent callers cannot change agent models (rc 4 `CAPABILITY_MISSING`) | **PASS** | `tests/integration/test_agents.py::test_agent_update_permissions` |
| **D8** | Concurrency validation rejects non-positive-integers (rc 2) | **PASS** | `tests/integration/test_agents.py::test_agent_concurrency_validation`<br>`tests/unit/test_settings.py::test_policy_concurrency` |
| **D9** | Valid positive integer concurrency accepted (1, 7, 2^63-1) | **PASS** | `tests/integration/test_agents.py::test_agent_concurrency_validation`<br>`tests/unit/test_settings.py::test_policy_concurrency` |
| **D10** | Setting concurrency to 0 via update or config set rejected (rc 2) | **PASS** | `tests/integration/test_agents.py::test_agent_concurrency_validation`<br>`tests/unit/test_settings.py::test_policy_concurrency` |
| **D17** | AGY model encoding effort with explicit `--effort` rejected | **PASS** | `tests/integration/test_agents.py::test_agent_create_agy_model_effort_conflict` |
| **G17** | Passphrase from JSON stdin rejected; non-tty returns rc 3 `NO_TTY` | **PASS** | `tests/integration/test_daemon.py::test_passphrase_sources` |
| **G22** | Canonical JSON op hash stable vectors; key order independent; floats rejected | **PASS** | `tests/unit/test_canonical.py::test_key_order_independence`<br>`tests/unit/test_canonical.py::test_unicode_preservation`<br>`tests/unit/test_canonical.py::test_compact_separators`<br>`tests/unit/test_canonical.py::test_float_rejection`<br>`tests/unit/test_canonical.py::test_stable_op_hash_vectors` |
| **G24** | Profile flags unique; trust-all flag restricted to unrestricted | **PASS** | `tests/unit/test_adapters.py::test_profile_flags_and_separation` |
| **I9** | Schema version newer than binary raises rc 1 `SCHEMA_TOO_NEW` | **PASS** | `tests/integration/test_daemon.py::test_schema_too_new`<br>`tests/unit/test_schema.py::test_schema_too_new` |
| **N1** | Adapter list without testing mode lists 6 CLIs + none; fake absent | **PASS** | `tests/integration/test_adapters.py::test_adapter_list_without_and_with_testing_mode` |
| **N2** | Hermes / OpenClaw return rc 13 `ADAPTER_NOT_INSTALLED`; probe reports `not_installed` | **PASS** | `tests/integration/test_agents.py::test_agent_create_not_installed_adapter`<br>`tests/integration/test_adapters.py::test_adapter_probe_and_show_not_installed` |
| **N3** | Fake adapter disabled outside testing mode (`TEST_DOUBLE_DISABLED`) | **PASS** | `tests/integration/test_adapters.py::test_adapter_list_without_and_with_testing_mode` |
| **N6** | Golden argv per adapter × profile × resume pass; kiro error mapping | **PASS** | `tests/unit/test_adapters.py::test_adapters_build_droid`<br>`tests/unit/test_adapters.py::test_adapters_build_agy`<br>`tests/unit/test_adapters.py::test_adapters_build_kiro`<br>`tests/unit/test_adapters.py::test_adapters_build_vibe`<br>`tests/unit/test_adapters.py::test_adapters_models_parse`<br>`tests/unit/test_adapters.py::test_adapters_parse_errors` |
| **N7** | `mesh adapter models <name>` reads real fixtures or returns null | **PASS** | `tests/integration/test_adapters.py::test_adapter_models`<br>`tests/unit/test_adapters.py::test_adapters_models_parse` |
| **N8** | Adapter probe detecting version difference marks capabilities `stale` | **PASS** | `tests/unit/test_adapters.py::test_adapter_stale_version` |
| **N15** | Real fixture hashes match plan §14.11 sha256; emitted flags in fixtures | **PASS** | `tests/unit/test_fixture_hashes.py::test_fixture_hashes`<br>`tests/unit/test_adapters.py::test_adapter_flags_in_fixtures` |
| **N16** | Prompt fallback per adapter; kiro readonly raises `PROMPT_TOO_LONG` | **PASS** | `tests/unit/test_adapters.py::test_prompt_fallback`<br>`tests/unit/test_adapters.py::test_prompt_t5_too_long` |

---

## 3. Review-r3 Findings Status for M1a

- **Finding 1 (PYTHONPATH isolation):** Handled and verified. `bin/mesh` saves original `PYTHONPATH` in `_MESH_ORIG_PYTHONPATH`; `build_external_env()` restores or clears it.
- **Finding 2 (Bidirectional install-path check):** Handled and verified. Install root inside workspace/runs/repo and workspace/runs/repo inside install root are both rejected.
- **Finding 3 (Lease-release wording):** Deferred to M1c per instructions.
- **Finding 4 (workspaces.root overlap error code):** Handled and verified. Setting `workspaces.root` to overlap install root returns exit 2 `INSTALL_PATH_INSECURE`.
- **Finding 5 (Checklist checkability):** Handled and verified. All M1a items have objective, executable automated checks.
- **Finding 6 (Vendor background daemons):** Deferred to M3 per instructions.

---

## 4. Test Suite Execution & Hygiene Evidence

```
$ bash scripts/test.sh
=== Running Unit Tests ===
Ran 69 tests in 4.604s
OK
=== Running Integration Tests ===
Ran 26 tests in 29.978s
OK
=== Running Recovery Tests ===
Ran 1 test in 0.000s
OK
=== All test tiers passed successfully ===
```

- **Tripwire:** Verified `/tmp/mesh-tripwire-suite.log` was empty / not triggered.
- **Temp Homes Cleanup:** Verified zero `/tmp/mh-*` directories remain after test suite execution (`glob.glob('/tmp/mh-*') == []`).
- **Invariants Check:** Fresh home and teardown checks run `mesh doctor --check-invariants` returning exit code 0 and empty violations list.
