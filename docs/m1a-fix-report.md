# M1a Fix Report

## F1 — `mesh daemon start --user` locks the user out on a fresh home

- **Finding ID**: F1
- **Root Cause**:
  1. `mesh/cli/commands/daemon.py:70-74,100-102` called `execute_cmd` with authentication on every 0.1s poll during daemon start readiness checks.
  2. `mesh/client.py:58-63` dup'd the passphrase fd and read it to EOF on each call without caching in memory. After the first read, subsequent polls sent an empty passphrase.
  3. Empty passphrases were forwarded to the daemon and verified against scrypt salt/verifier, failing auth 5 times and triggering an account lockout (`LOCKED_OUT`).
- **Fix with file:line**:
  - `mesh/client.py:52-70,111-116`: Cached resolved auth in memory (`_cached_auth` on `MeshClient` and `_resolved_auth` on `args`), rewound seekable fds, and rejected empty passphrases client-side with `PASSPHRASE_INVALID` (rc 3) without hitting the daemon.
  - `mesh/cli/commands/daemon.py:71-74,76-85,113-133`: Cached user auth once if `--user` is provided, and polled `daemon.status` with `auth_override={"type": "none"}` so readiness polling cannot fail authentication or count toward lockout.
- **Regression test name**:
  - `TestDaemonIntegration.test_daemon_start_user_passphrase_fd` in `tests/integration/test_daemon.py:324-362`
  - `TestDaemonIntegration.test_empty_passphrase_rejected_client_side` in `tests/integration/test_daemon.py:364-398`
- **Exact counterexample command and result**:
  - Command: `python3 /tmp/m1a-review/t1.py`
  - Result:
    - `init 0`
    - `start 0 {"ok": true, "data": {"status": "started", ...}}`
    - `status 0 {"ok": true, "data": {"status": "running", ...}}`
    - User account remains unlocked, 0 `auth.failed` events.

## F2 — JSON input path for concurrency, parameter restrictions, and pure `--input -`

- **Finding ID**: F2
- **Root Cause**:
  1. `mesh/core/policy.py:51-74` accepted strings like `"2"` in `parse_positive_int`, not distinguishing between CLI flag strings and strict JSON types.
  2. The canonical JSON serialization in `mesh/daemon/server.py:540` executed before validation, converting floats (e.g. `2.0`, `NaN`) into unhandled `ValueError` resulting in rc 1 `INTERNAL_ERROR`.
  3. Argparse required flags blocked pure `--input -` JSON bodies before input parsing could populate them.
  4. `--input` permitted global/internal flags like `token_file`, `home`, and `func` because the allow-list was `vars(args)`.
  5. `tests/unit/test_settings.py:125` erroneously asserted that JSON strings `"1"`, `"7"`, `"9223372036854775807"` were valid concurrency values.
- **Fix with file:line**:
  - `mesh/core/policy.py:12-88`: Updated `parse_positive_int` to strictly enforce integer JSON values (rejecting bool, float, None, and str with rc 2 `INVALID_CONCURRENCY`), and added `parse_cli_positive_int` to parse decimal integers from CLI flag strings.
  - `mesh/cli/commands/agent.py:155-235`: Tracked provenance (`_from_json`) so flag strings are parsed via `parse_cli_positive_int` while JSON values are forwarded raw for strict JSON integer validation in daemon.
  - `mesh/cli/main.py:194-285,342-348,394-398`: Relaxed required actions when `--input` is on argv; restricted `--input` keys to active subcommand's documented parameters and synonyms, rejecting global/internal options like `token_file` with `UNKNOWN_PARAM` (rc 2); tracked `_from_json`.
  - `mesh/daemon/server.py:540-585`: Pre-validated `agent.create`/`agent.update` and `config.set` arguments before canonical hashing/idempotency check, and caught `ValueError` from `canonical_json` to map float errors to rc 2 (`INVALID_CONCURRENCY`, `INVALID_SETTING`, or `INVALID_ARGUMENT`) instead of `INTERNAL_ERROR`.
  - `tests/unit/test_settings.py:111-128`: Corrected test assertions to verify that string concurrency values are rejected as invalid, and only strict JSON integers are accepted per plan §9.2 / §5.7.
- **Regression test name**:
  - `TestAgentsIntegration.test_agent_concurrency_json_validation` in `tests/integration/test_agents.py:195-255`
  - `TestSettingsUnit.test_concurrency_validation_d8_d9` in `tests/unit/test_settings.py:90-130`
- **Exact counterexample command and result**:
  - Command: `python3 /tmp/m1a-review/t2.py`
  - Result:
    - `PASS D8 flag values`
    - `PASS D8 JSON values (pure --input -)`
    - `PASS D8 JSON values (flags + --input max_concurrency)`
    - `PASS D8 no rows created`
    - `PASS D9 values flag+JSON`
    - `PASS D10 update 0`
    - `PASS D10 config 0`
    - `PASS max_total_runs bool rejected`
    - `PASS max_total_runs null accepted`
    - `PASS C9 db only lookup+32-byte hash`

## F4 — Authz matrix deviation on read commands (`config.list/get`, `agent.list/show`)

- **Finding ID**: F4
- **Root Cause**:
  `mesh/daemon/server.py:447-463` included `config.list`, `config.get`, `agent.list`, and `agent.show` in `unauth_cmds`, allowing anonymous and agent callers. Plan §7.3 L489 establishes that `config.*`, `agent.*`, and `agent token.*` require direct human user authentication (`✔ (direct only)`), with agent callers receiving rc 4 `CAPABILITY_MISSING` (plan §7.3 L504) and unauthenticated callers receiving rc 3 `TOKEN_MISSING`.
- **Fix with file:line**:
  - `mesh/daemon/server.py:449-462`: Removed `config.list`, `config.get`, `agent.list`, and `agent.show` from `unauth_cmds`, returning rc 3 `TOKEN_MISSING` on unauthenticated requests.
  - `mesh/daemon/handlers/config.py:16-21,46-51`: Added direct user enforcement (`ctx.actor_kind != "user" or ctx.via != "direct"`) in `handle_config_list` and `handle_config_get` returning rc 4 `CAPABILITY_MISSING`.
  - `mesh/daemon/handlers/agent.py:537,587`: Added `_check_direct_user_only` in `handle_agent_show` and `handle_agent_list` returning rc 4 `CAPABILITY_MISSING`.
  - `mesh/daemon/server.py:559-566`: Enforced direct user check for `agent.*` in pre-dispatch validation.
  - `tests/integration/test_agents.py:43,191,330,348` and `tests/integration/test_daemon.py:212,301`: Updated test callers of `agent show` and `config get` to specify `user=True`.
- **Regression test name**:
  - `TestDaemonIntegration.test_f4_authz_matrix_reads` in `tests/integration/test_daemon.py:400-432`
- **Exact counterexample command and result**:
  - Command: `python3 /tmp/m1a-review/t2.py`
  - Result:
    - `PASS config.list by agent token: plan §7.3 says user-only (rc4)`
    - `PASS config.list anonymous should not succeed (plan §7.3 user-only)`
    - `PASS agent.list anonymous should not succeed (plan §7.3)`

## F5 — Config validation gaps (§5.7)

- **Finding ID**: F5
- **Root Cause**:
  1. `mesh/core/settings.py:91-105` checked only string type for `lead.default_agent`, not verifying whether the agent exists, is enabled, and has `can_lead=True` per plan §5.7 L346.
  2. Setting values of `600.0`, `1e3`, `NaN`, `Infinity` triggered `ValueError` in `canonical_json` before validation, returning rc 1 `INTERNAL_ERROR`.
  3. `mesh/cli/commands/config.py:75-80` raised `INVALID_JSON` rather than `INVALID_SETTING` with `details={"key": args.key}` when a setting value was unparseable.
- **Fix with file:line**:
  - `mesh/core/settings.py:60-66,91-135`: Added `db_conn` validation in `validate_setting` for `lead.default_agent`, ensuring the agent exists, is not disabled (`disabled_at IS NULL`), and has `can_lead=1` (else rc 2 `INVALID_SETTING`).
  - `mesh/daemon/handlers/config.py:84-90`: Passed `db_conn=ctx.conn` to `validate_setting`.
  - `mesh/cli/commands/config.py:73-81`: Raised `INVALID_SETTING` with `details={"key": args.key}` on invalid JSON.
  - `mesh/daemon/server.py:540-560`: Pre-validated `config.set` before canonical hashing, rejecting floats/NaN/Infinity with rc 2 `INVALID_SETTING`.
- **Regression test name**:
  - `TestDaemonIntegration.test_f5_config_validation` in `tests/integration/test_daemon.py:434-468`
- **Exact counterexample command and result**:
  - Command: `python3 /tmp/m1a-review/t2.py`
  - Result:
    - `PASS B13 config set task.default_timeout_s=600.0 -> rc2 INVALID_SETTING details.key`
    - `PASS B13 config set task.default_timeout_s=NaN -> rc2 INVALID_SETTING details.key`
    - `PASS B13 config set task.default_timeout_s=1e3 -> rc2 INVALID_SETTING details.key`
    - `PASS B13 config set lead.default_agent="fx" -> rc2 INVALID_SETTING details.key`
    - `PASS B13 config set lead.default_agent="nobody" -> rc2 INVALID_SETTING details.key`
    - `PASS config lead.default_agent lead1 ok`

## F6 — Mitigation 8: caller PYTHONPATH was prepended

- **Finding ID**: F6
- **Root Cause**:
  `bin/mesh:14-18` set `PYTHONPATH="$INSTALL_ROOT:$PYTHONPATH"`, allowing caller's `PYTHONPATH` to be on Mesh's `sys.path`. If a caller had `PYTHONPATH=/tmp/evil` containing an `argparse.py` or other module, Mesh executed the shadow file rather than the stdlib.
- **Fix with file:line**:
  - `bin/mesh:13-14`: Set `export PYTHONPATH="$INSTALL_ROOT"` only. Caller's original `PYTHONPATH` remains captured in `_MESH_ORIG_PYTHONPATH` for restoring into external child envs via `build_external_env`.
- **Regression test name**:
  - `TestDaemonIntegration.test_f6_caller_pythonpath_isolated` in `tests/integration/test_daemon.py:465-485`
- **Exact counterexample command and result**:
  - Command: `python3 /tmp/m1a-review/t4.py`
  - Result:
    - `PASS mitigation 8: caller PYTHONPATH cannot shadow stdlib/mesh in bin/mesh`
    - `PASS mitigation 8: cwd mesh/ and json.py do not shadow (-P)`

## F7 — Request size limits, unknown command classification, doctor schema check, workspaces.root install path

- **Finding ID**: F7
- **Root Cause**:
  1. `mesh/daemon/server.py:823` used asyncio's default 64 KiB buffer limit in `start_unix_server`. Requests over 64 KiB threw an unhandled `ValueError` / `LimitOverrunError` inside `reader.readline()`, causing an unexpected exception and returning rc 1 `INTERNAL_ERROR`.
  2. `mesh/daemon/server.py:699-705` had a blanket `else:` dispatching all unrecognized commands as `NOT_IMPLEMENTED_IN_M1` with `milestone: "M1b"`, even for completely unknown commands.
  3. `mesh/cli/commands/task.py:28` required `--spec` on the CLI parser, blocking `task create` from reaching the `NOT_IMPLEMENTED_IN_M1` handler; `mesh/cli/commands/dep.py:27` required `--on` blocking positional `dep add a b`; and `mesh/cli/commands/top.py` lacked a subparser for `top`.
  4. `mesh/cli/commands/doctor.py:54-65` checked invariants without verifying `schema_version` against `CURRENT_SCHEMA_VERSION`, returning rc 0 on a DB with future schema version (e.g. 999).
  5. `mesh/daemon/server.py:116` and `mesh/cli/commands/doctor.py:34` called `check_install_path_secure` with `workspaces_root=None`, defaulting to `$MESH_HOME/workspaces` and ignoring any custom `workspaces.root` stored in the `settings` table.
- **Fix with file:line**:
  - `mesh/daemon/server.py:63-112,850`: Added `PLANNED_COMMAND_MILESTONES` mapping real planned verbs to their milestones (`M1b`, `M1c`, `M2`, `None`), raising rc 2 `NOT_IMPLEMENTED_IN_M1` with milestone for planned verbs, and rc 2 `INVALID_ARGUMENT` ("Unknown command: '<cmd>'") for truly unknown commands. Passed `limit=MAX_REQUEST_BYTES + 4096` to `asyncio.start_unix_server`.
  - `mesh/daemon/server.py:521-536`: Caught `(ValueError, asyncio.LimitOverrunError)` in `handle_client`, drained remaining data, and raised `MeshError(c.INVALID_ARGUMENT, c.EXIT_USAGE, "Request payload exceeds 1 MiB limit")`.
  - `mesh/daemon/server.py:128-142`: Read configured `workspaces.root` from `settings` table after opening DB and verified it with `check_install_path_secure`.
  - `mesh/cli/commands/doctor.py:10-40,75-85`: Inspected `workspaces.root` in `settings` table during `doctor` health checks; in `--check-invariants`, read `schema_version` from `meta` and raised `SchemaTooNewError` (rc 1) if `current_ver > CURRENT_SCHEMA_VERSION`.
  - `mesh/cli/commands/task.py:26-28`: Relaxed CLI parser required flags (`--project`, `--title`, `--spec` default to `None`) so `task create` reaches `handle_task` to return `NOT_IMPLEMENTED_IN_M1`.
  - `mesh/cli/commands/dep.py:26-28,38-40`: Added optional positional `on_arg` so `dep add a b` and `dep add a --on b` both reach `handle_dep`.
  - `mesh/cli/commands/top.py:11,73-86`: Added `top` parser returning `NOT_IMPLEMENTED_IN_M1` with `milestone=None`.
  - `mesh/errors.py:42-48`: Made `milestone` optional in `NotImplementedInM1Error`.
- **Regression test name**:
  - `TestDaemonIntegration.test_f7a_request_size_limits` in `tests/integration/test_daemon.py:488-535`
  - `TestDaemonIntegration.test_f7b_unknown_command_classification` in `tests/integration/test_daemon.py:537-580`
  - `TestDaemonIntegration.test_f7c_doctor_schema_too_new` in `tests/integration/test_daemon.py:582-618`
  - `TestDaemonIntegration.test_f7d_daemon_startup_honors_configured_workspaces_root` in `tests/integration/test_daemon.py:620-647`
- **Exact counterexample command and result**:
  - Command: `python3 /tmp/m1a-review/t2.py`
  - Result:
    - `PASS NOT_IMPLEMENTED_IN_M1 with correct milestone`
    - `SUMMARY 79 pass / 79`
  - Command: `python3 /tmp/m1a-review/t3.py`
  - Result:
    - `PASS oversized request gets structured rc2 error (not hang/internal)`
    - `PASS daemon alive after oversized request`
    - `doctor on too-new schema: 1 SCHEMA_TOO_NEW`
    - `SUMMARY 13 pass / 13`

## Test-Quality — Suite-level tripwire effectiveness and meaningful test runner execution

- **Finding ID**: Test-Quality
- **Root Cause**:
  1. `MeshHarness` overrode `MESH_TRIPWIRE_FILE` per-harness (`tests/helpers.py:33,52`), redirecting tripwire logs away from `scripts/test.sh`'s suite-level file (`/tmp/mesh-tripwire-suite.log`) and deleting them on teardown.
  2. `A1` test (`test_skeleton.py:144-153`) only grepped `scripts/test.sh` file content without executing the test runner.
- **Fix with file:line**:
  - `scripts/test.sh:7-16`: Added `--check` flag for non-recursive execution verification; exported `MESH_SUITE_TRIPWIRE_FILE` alongside `MESH_TRIPWIRE_FILE`.
  - `tests/shims/agy`, `droid`, `hermes`, `kiro-cli`, `openclaw`, `vibe`: Updated shims to append triggers to `MESH_SUITE_TRIPWIRE_FILE` if set, ensuring suite-level tripwire logs cannot be silenced.
  - `tests/helpers.py:32,53,205-212,230-239`: Tracked `self.suite_tripwire_file`, passed it into test subprocess environments, and appended any tripwire hit from `self.tripwire_file` into `suite_tripwire_file` during both `assert_tripwire_empty()` and `teardown()`.
  - `tests/unit/test_skeleton.py:155-164,295-325`: Updated `test_test_runner_script` (A1) to execute `sh -n scripts/test.sh` and `scripts/test.sh --check`; added `test_suite_tripwire_propagation` testing that shim execution propagates to suite-level tripwire files.
- **Regression test name**:
  - `TestSkeleton.test_test_runner_script` in `tests/unit/test_skeleton.py:144-165`
  - `TestSkeleton.test_suite_tripwire_propagation` in `tests/unit/test_skeleton.py:295-325`
- **Exact counterexample command and result**:
  - Command: `bash scripts/test.sh`
  - Result:
    - 70 unit tests passed
    - 36 integration tests passed
    - 1 recovery test passed
    - Step 4 verified tripwire empty; rc=0 exit.

## F3 — Adapter probe and model inspection (§14, gates N1, N7, N8, N10)

- **Finding ID**: F3
- **Root Cause**:
  1. `mesh/adapters/base.py`, `agy.py`, `kiro.py`, and `vibe.py` returned hard-coded constant `Probe` objects and never invoked the underlying binaries.
  2. `adapter list` in non-testing mode failed gate N1 because it returned static constants rather than dynamically probing resolved binaries (`droid 1.3.0`, `agy None`, `kiro None`, `vibe None`).
  3. `stale` detection (gates N8/N10) could never trigger in production because probes did not capture live `--version` or compute `--help` sha256 against plan commitments.
  4. `list_models` dynamically read fixture files under `tests/fixtures/real/...` at runtime, violating runtime isolation from the test suite and failing if tests are not deployed.
  5. Missing binaries or probe failures did not set `not_installed`, `error`, or `unverified` statuses with diagnostic details, and probe results were not persisted to the database.
- **Fix with file:line**:
  - `mesh/adapters/base.py:27-46,55-125,145-212`:
    - Embedded `EXPECTED_HELP_HASHES` as constants citing plan §14.11 (runtime never imports or reads `tests/`).
    - Implemented `parse_version_output` to extract version strings from `--version` outputs.
    - Implemented `run_readonly_cmd` using `subprocess.run` with `stdin=subprocess.DEVNULL`, `build_external_env()`, strict timeout, output caps (`MAX_PROBE_OUTPUT_BYTES = 256 * 1024`), no `shell=True`, and SHA-256 calculation.
    - Added `record_adapter_probe` to persist probe records (probed version, help sha256, flag diff, models, error details, timestamp) into the `adapter_probes` SQLite table.
    - Added `resolve_binary` and `is_installed` to `Adapter` class, resolving against PATH or `binary_path` while ignoring `tests/shims/` for hermes and openclaw.
  - `mesh/adapters/droid.py:10-145`:
    - Removed runtime reads from `tests/fixtures`.
    - Executed read-only `[bin, "exec", "--help"]` to parse models via `parse_models_from_help()`.
    - Executed read-only `[bin, "--version"]`, `[bin, "--help"]`, and `[bin, "exec", "--help"]` during probe; compared help SHA-256 against `EXPECTED_HELP_HASHES`; set stale and flag diff; recorded probe to `adapter_probes`.
  - `mesh/adapters/agy.py:10-150`:
    - Removed runtime reads from `tests/fixtures`.
    - Executed read-only `[bin, "models"]` to parse models via `parse_models_from_output()`.
    - Executed read-only `[bin, "--version"]`, `[bin, "--help"]`, and `[bin, "models"]` during probe; compared help SHA-256 against `EXPECTED_HELP_HASHES`; set stale and flag diff; recorded probe to `adapter_probes`.
  - `mesh/adapters/kiro.py:10-175`:
    - Removed runtime reads from `tests/fixtures`.
    - Executed read-only `[bin, "chat", "--list-models", "-f", "json"]` to parse JSON model list.
    - Executed read-only `[bin, "--version"]`, `[bin, "--help"]`, `[bin, "chat", "--help"]`, and `[bin, "chat", "--list-models", "-f", "json"]` during probe; compared help SHA-256 against `EXPECTED_HELP_HASHES`; set stale and flag diff; recorded probe to `adapter_probes`.
  - `mesh/adapters/vibe.py:10-135`:
    - Executed read-only `[bin, "--version"]` and `[bin, "--help"]` during probe; compared help SHA-256 against `EXPECTED_HELP_HASHES`; reported `models=None` with capability `verification="unverified"` per plan §14.6/N7; recorded probe to `adapter_probes`.
  - `mesh/adapters/hermes.py:10-75` and `mesh/adapters/openclaw.py:10-75`:
    - Probed PATH for binaries; when absent (ignoring `tests/shims/`), returned status `not_installed` with `version=None`. Recorded probe to `adapter_probes`.
  - `mesh/adapters/none_adapter.py:10-60` and `mesh/adapters/fake.py:10-65`:
    - Updated probe implementations and recorded probes to `adapter_probes`.
  - `mesh/daemon/handlers/adapter.py:28-115`:
    - Passed DB connection `conn=ctx.conn` and `binary_path` across `handle_adapter_list`, `handle_adapter_show`, `handle_adapter_probe`, and `handle_adapter_models`.
  - `mesh/daemon/handlers/agent.py:125-135`:
    - Used `adapter.is_installed(binary_path=binary_override)` instead of running a full probe subprocess during agent creation.
  - `mesh/cli/commands/adapter.py:22-38,62-75`:
    - Added `--binary` CLI option to `adapter probe` and `adapter models`.
  - `tests/stubs.py:1-125`:
    - Added test stubs library (`create_test_stubs`, `create_failing_stub`, `create_hanging_stub`, `create_garbage_stub`, `create_stale_stub`, `create_modified_help_stub`) reproducing committed fixture outputs on read-only calls and triggering tripwire on unauthorized calls.
- **Regression test name**:
  - `TestAdaptersUnit.test_expected_help_hashes_constants` in `tests/unit/test_adapters.py:21-36`
  - `TestAdaptersUnit.test_version_output_parser` in `tests/unit/test_adapters.py:38-51`
  - `TestAdaptersUnit.test_probe_error_handling` in `tests/unit/test_adapters.py:53-76`
  - `TestAdaptersUnit.test_probe_unverified_handling` in `tests/unit/test_adapters.py:78-95`
  - `TestAdaptersUnit.test_probe_timeout` in `tests/unit/test_adapters.py:97-111`
  - `TestAdaptersUnit.test_probe_flag_diff_and_stale` in `tests/unit/test_adapters.py:113-138`
  - `TestAdaptersUnit.test_record_probe_to_database` in `tests/unit/test_adapters.py:140-165`
  - `TestAdaptersIntegration.test_adapter_list_probes_versions` in `tests/integration/test_adapters.py:65-98`
  - `TestAdaptersIntegration.test_adapter_probe_and_stale` in `tests/integration/test_adapters.py:100-142`
  - `TestAdaptersIntegration.test_adapter_probe_error_and_timeout` in `tests/integration/test_adapters.py:144-180`
  - `TestAdaptersIntegration.test_adapter_models_probe` in `tests/integration/test_adapters.py:182-205`
  - `TestAdaptersIntegration.test_adapter_probe_persisted_to_db` in `tests/integration/test_adapters.py:207-225`
- **Exact counterexample command and result**:
  - Command: `python3 /tmp/m1a-review/t1.py`
  - Result:
    - `init 0`
    - `start 0`
    - `status 0`
    - `adapter list 0` (probes resolved binaries, reports live versions and statuses; shims logged `--version` into tripwire)
    - `models kiro 0` (probes `kiro-cli chat --list-models -f json`)
    - `models vibe 0` (returns `models: null` with unverified status)
    - `fake non-testing 13 TEST_DOUBLE_DISABLED`
    - `hermes 13 ADAPTER_NOT_INSTALLED`
  - Command: `python3 /tmp/m1a-review/t2.py`
  - Result:
    - `PASS N2 hermes ADAPTER_NOT_INSTALLED rc13`
    - `PASS N2 openclaw ADAPTER_NOT_INSTALLED rc13`
    - `SUMMARY 79 pass / 79`
  - Command: `bash scripts/test.sh`
  - Result:
    - 76 unit tests passed
    - 39 integration tests passed
    - 1 recovery test passed
    - 0 tripwire violations, rc 0

## F3 Follow-Up — Normalized help text (stdout+stderr, ANSI stripping, locale warning filtering) for probe help hash & flag diff

- **Finding ID**: F3 follow-up
- **Root Cause**:
  1. `mesh/adapters/base.py`'s `run_readonly_cmd` computed `sha256_hash` solely from raw `proc.stdout`, returning only stdout as the command output string.
  2. Real CLI binaries like `agy 1.5.3` print their `--help` output to STDERR with empty STDOUT, exiting 0.
  3. Because STDOUT was empty, `help_sha256` evaluated to the hash of empty string `e3b0c44...`, causing `h_sha256 != expected_help` to trigger. When checking flags against `h_out == ""`, `flag_diff` falsely reported every flag (`--print`, `--model`, ...) as missing.
  4. Real fixture capture rules (documented in `tests/fixtures/real/README.md`) captured help as `cmd 2>&1` with ANSI escapes stripped and locale-warning lines (`warning: setlocale`) removed.
- **Fix with file:line**:
  - `mesh/adapters/base.py:43-125`:
    - Implemented `normalize_help_text(stdout, stderr)` which merges stdout and stderr (`cmd 2>&1`), strips ANSI escape sequences via `ANSI_ESCAPE_RE`, and drops lines containing `"warning: setlocale"`.
    - Implemented `is_flag_present(flag, help_text)` and `find_missing_flags(flags, help_text)` using regex boundary checks (`(?:^|[\s,\[(])<flag>(?:[\s=,\]):]|$)`) so that prefix flags (e.g. `--print` vs `--print-timeout`) and trailing-equals flags (e.g. `--trust-tools=`) are accurately evaluated, reporting only flags genuinely absent.
    - Updated `ReadonlyCmdResult` as a tuple subclass preserving 4-tuple unpacking `(returncode, stdout, stderr, help_sha256)` while exposing `.help_text`, `.help_sha256`, `.stdout`, `.stderr`, and `.returncode`.
    - Updated `run_readonly_cmd` to compute `help_text = normalize_help_text(stdout_str, stderr_str)` and `help_sha256 = hashlib.sha256(help_text.encode("utf-8")).hexdigest()`.
  - `mesh/adapters/agy.py:205-273`:
    - Switched help check to `res_h.help_sha256` and flag difference check to `find_missing_flags(flags, res_h.help_text)`.
    - Preserved N8 stale verification if `v != self.verified_version` or `help_sha256` mismatches.
    - Used `res_m.stdout` for model parsing.
  - `mesh/adapters/droid.py:113-125,208-278`:
    - Used `res_eh.help_text` in `list_models` and `parse_models_from_help`.
    - Used `find_missing_flags(flags, res_eh.help_text)` for `flag_diff` on `droid_exec` help mismatch.
  - `mesh/adapters/kiro.py:145-156,235-310`:
    - Used `res_ch.help_text` and `find_missing_flags(flags, res_ch.help_text)` for `kiro_chat` help diff.
  - `mesh/adapters/vibe.py:175-245`:
    - Used `res_h.help_text` and `find_missing_flags(flags, res_h.help_text)` for vibe help diff.
  - `mesh/adapters/hermes.py:80-110` and `mesh/adapters/openclaw.py:80-110`:
    - Used `res_h.help_sha256` and `find_missing_flags(flags, res_h.help_text)`.
  - `tests/stubs.py:220-305`:
    - Added `create_stderr_help_stub` generating a stub binary that writes `--help` text to STDERR only (exit 0) with ANSI escapes and locale warnings.
    - Added `create_missing_flag_stub` generating a stub binary that omits a specific flag.
- **Regression test name**:
  - `TestAdaptersUnit.test_normalize_help_text` in `tests/unit/test_adapters.py:760-776`
  - `TestAdaptersUnit.test_find_missing_flags` in `tests/unit/test_adapters.py:778-787`
  - `TestAdaptersUnit.test_probe_help_on_stderr_only` in `tests/unit/test_adapters.py:789-803`
  - `TestAdaptersUnit.test_probe_help_missing_flag` in `tests/unit/test_adapters.py:805-824`
  - `TestAdaptersIntegration.test_adapter_probe_and_stale` in `tests/integration/test_adapters.py:204-245`
- **Exact counterexample command and result**:
  - Command: `python3 /tmp/m1a-review/t1.py`
  - Result:
    - `init 0`
    - `start 0`
    - `status 0`
    - `adapter list 0` (probes resolved binaries, reports live versions and statuses)
    - `models kiro 0`
    - `models vibe 0`
    - `fake non-testing 13 TEST_DOUBLE_DISABLED`
    - `hermes 13 ADAPTER_NOT_INSTALLED`
  - Command: `python3 /tmp/m1a-review/t2.py`
  - Result:
    - `SUMMARY 79 pass / 79`
  - Command: `bash scripts/test.sh`
  - Result:
    - 80 unit tests passed
    - 39 integration tests passed
    - 1 recovery test passed
    - 0 tripwire violations, rc 0

