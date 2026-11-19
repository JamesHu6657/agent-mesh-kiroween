# Batch 7 PR #46 Round-2 Implementation Notes

This note records verification against commit `fa89b4f` (`cloud/pr46r2-base`), documentation (`docs/requirements.md`, `docs/usage.md`, `docs/plan.md` §13), root-cause fixes, same-class audit findings, design decisions made without user prompting, and verification runs for branch `pr46-r2`.

All four round-2 review findings (3x P2, 1x P3) and several same-class truthiness, null-handling, NUL-byte path, and direct-RPC validation bugs were verified as real against `fa89b4f`, reproduced with failing regression tests in [tests/unit/test_batch7_pr46_r2.py](tests/unit/test_batch7_pr46_r2.py) and [tests/integration/test_batch7_pr46_r2.py](tests/integration/test_batch7_pr46_r2.py), and resolved with minimal root-cause changes without breaking existing valid callers.

## Verified Round-2 Findings and Fixes

### 1. [P2] `--input` JSON `null` overwriting non-`None` defaults + `setup` & `task.wait` exit-1 crashes

- **Doc & Code Verification:** Confirmed real. `docs/plan.md` §13.1–§13.2 requires all CLI argument and JSON `--input` validation errors to return exit `2` (`EXIT_USAGE`), never exit `1` (`INTERNAL_ERROR`). In `fa89b4f`, `parse_and_apply_input_file` ([mesh/cli/main.py](mesh/cli/main.py)) unconditionally wrote `setattr(args, key, val)` (overwriting non-`None` parser defaults with `None`) and then skipped all validation whenever `val is None`:
  1. `mesh setup --input -` with `{"lead_name": null}` overwrote `--lead-name` (default `"lead"`) with `None`; `handle_setup` ([mesh/cli/commands/setup.py](mesh/cli/commands/setup.py)) checked `if lead_name is not None ...`, ran `init` and `daemon start`, and then crashed with `TypeError: expected str, bytes or os.PathLike object, not NoneType` (exit `1`).
  2. `mesh task wait <tid> --input -` with `{"until": null}` overwrote `--until` (default `"submitted"`) with `None` and sent it to the daemon, where `handle_task_wait` ([mesh/daemon/handlers/task.py](mesh/daemon/handlers/task.py)) crashed calling `until.startswith("state:")` (`AttributeError`, exit `1`). Direct Unix socket calls with `{"until": 123}` crashed identically.
- **Root Cause & Fix:**
  - [mesh/cli/main.py](mesh/cli/main.py) (`parse_and_apply_input_file`): When `val is None` for a JSON-supplied argument whose argparse `act.default not in (None, argparse.SUPPRESS)`, reject with `MeshError(INVALID_ARGUMENT, EXIT_USAGE, details={"param": canonical_key, "got": "NoneType"})`, except for explicitly documented nullable fields (`config set` `value`, `task submit` `deliverable` which `task.py` validates as JSON object, `agent update` `default_model`/`default_effort` clearing, and `agent create|update|onboard` `max_concurrency` which forwards `null` so the daemon reports `INVALID_CONCURRENCY` exit `2`).
  - [mesh/cli/commands/setup.py](mesh/cli/commands/setup.py) (`handle_setup`): Hardened pre-side-effect checks so `not args.no_lead` requires `isinstance(lead_name, str) and _LEAD_NAME_RE.match(lead_name)` before `init` / `daemon start` / passphrase prompting.
  - [mesh/daemon/handlers/task.py](mesh/daemon/handlers/task.py) (`handle_task_wait`): Validated `isinstance(until, str)` before checking `"submitted"` / `"terminal"` / `until.startswith("state:")`, returning `INVALID_ARGUMENT` exit `2` on non-string values (`None`, ints, lists, bools).

### 2. [P2] `agent onboard` pre-validating `--expires-in` before `agent.create`

- **Doc & Code Verification:** Confirmed real. `mesh agent onboard` combines `agent.create` + `agent.token.issue` + `--out` writing. `handle_agent_onboard` ([mesh/cli/commands/agent.py](mesh/cli/commands/agent.py)) already pre-checked `--out` and `--max-concurrency` before any RPC, but `expires_in` was only validated server-side in `handle_agent_token_issue`. Passing `--expires-in bogus`, `--expires-in 0s`, `--expires-in -5s`, or JSON `{"expires_in": ""}` committed the agent row (`principals` + `agents` + audit event) in step 1 before failing step 2 (`agent.token.issue`), leaving an orphan agent and causing corrected retries (without `--idempotency-key`) to fail with `ALREADY_EXISTS` (exit `6`).
- **Root Cause & Fix:**
  - [mesh/cli/commands/agent.py](mesh/cli/commands/agent.py): Extracted `_validate_expires_in(expires_in)` (requiring non-empty `str` and `parse_duration(expires_in) > 0`, raising `MeshError(INVALID_ARGUMENT, EXIT_USAGE, details={"param": "expires_in", ...})`) and called it pre-RPC in both `handle_agent_onboard` and `handle_agent_token_issue`. Kept the server-side validation in `handle_agent_token_issue` ([mesh/daemon/handlers/agent.py](mesh/daemon/handlers/agent.py)) unchanged for direct-RPC parity.

### 3. [P2] `adapter probe` client truthiness dropping `--binary ""` and empty `adapter_id`

- **Doc & Code Verification:** Confirmed real. Per [mesh/daemon/handlers/adapter.py](mesh/daemon/handlers/adapter.py) and `docs/plan.md` §13.3, specifying `--binary` requires `adapter_id` (exit `2`), requires direct user authentication (`CAPABILITY_MISSING`, exit `4`), and requires a non-empty `binary` path (exit `2`). In `fa89b4f`, `handle_adapter_probe` ([mesh/cli/commands/adapter.py](mesh/cli/commands/adapter.py)) checked `if raw_bin:` and `if args.adapter_id:`, silently stripping `"binary": ""` and `"adapter_id": ""` from the RPC payload. As a result, `mesh adapter probe --binary ""` (even with a worker token and no `adapter_id`) bypassed all three rules and probed all adapters on `PATH` with exit `0`.
- **Root Cause & Fix:**
  - [mesh/cli/commands/adapter.py](mesh/cli/commands/adapter.py) (`handle_adapter_probe`): Replaced truthiness checks with `if raw_bin is not None:` and `if args.adapter_id is not None:`. Client still requires non-empty `args.adapter_id` whenever `raw_bin is not None`, and only calls `os.path.abspath(os.path.expanduser(...))` when `raw_bin` is a valid non-empty non-NUL string not starting with `-`, preserving empty/invalid values so the daemon enforces direct-user authz (`CAPABILITY_MISSING` exit `4`) before rejecting empty/invalid `binary` (`INVALID_ARGUMENT` exit `2`).
  - [mesh/daemon/handlers/adapter.py](mesh/daemon/handlers/adapter.py) (`_extract_adapter_id`): Stopped using truthiness `or` across `("adapter_id", "id", "adapter")`; iterates keys checking `is not None` and validates `isinstance(adapter_id, str) and adapter_id.strip()`, so empty `""` or `False` raises `INVALID_ARGUMENT` exit `2` rather than being treated as omitted (`probe all`).

### 4. [P3] `handle_agent_update` missing server-side type checks on `capabilities`, profiles, `if_version`, and sibling fields

- **Doc & Code Verification:** Confirmed real. While `handle_agent_create` ([mesh/daemon/handlers/agent.py](mesh/daemon/handlers/agent.py)) gained `isinstance` checks in PR46 R1, `handle_agent_update` in the same file still lacked them:
  - Direct-socket `capabilities: false` / `123` crashed with `TypeError` (exit `1`), `[{}]` crashed on unhashable `in VALID_CAPABILITIES` (exit `1`), and `{"intake": false}` iterated dict keys to enable `["intake"]` (exit `0`).
  - Non-string `default_profile` / `max_unapproved_profile` (e.g. `{}`, `[]`) crashed on set membership checks (exit `1`).
  - `if_version: true` was coerced by `int(True) == 1` instead of rejected.
- **Root Cause & Fix:**
  - [mesh/daemon/handlers/agent.py](mesh/daemon/handlers/agent.py): Extracted shared validators `_validate_default_profile`, `_validate_max_unapproved_profile`, and `_parse_capabilities_arg`, and reused them in both `handle_agent_create` and `handle_agent_update` (supporting `"capability"` alias consistently with `handle_agent_create`).
  - Hardened remaining `handle_agent_update` direct-RPC inputs: required `name` to be a non-empty `str`; rejected `bool`/`float` in `if_version` before `int()`; checked `"max_concurrency" in ctx.args` (matching `agent.create`) instead of skipping `None`; and verified `val is not None and not isinstance(val, str)` before clearing `default_model`/`default_effort` (so `123` or `{}` returns `INVALID_ARGUMENT` exit `2` even on `adapter="none"` agents).
  - [mesh/daemon/server.py](mesh/daemon/server.py): Updated `agent.update` pre-validation from `if args.get("max_concurrency") is not None:` to `if "max_concurrency" in args:`, matching `agent.create`.

## Same-Class Audit: Additional Fixes and Non-Issues

### Additional Real Same-Class Bugs Fixed

1. **`mesh setup` early `approval_mode` & `--no-lead` `token_dir` validation** ([mesh/cli/commands/setup.py](mesh/cli/commands/setup.py)):
   - `handle_setup` now validates `approval_mode in ("passphrase", "delegated")` (when `not None`) and validates `token_dir` (non-empty `str` without `\x00`) even when `--no-lead` is set, before passphrase prompts, `mesh.db` initialization, or daemon startup.
2. **`mesh agent create` CLI truthiness & `binary` normalization** ([mesh/cli/commands/agent.py](mesh/cli/commands/agent.py)):
   - Used `_from_json` tracking so explicit `""` or JSON `null` for `default_profile` / `max_unapproved_profile` is preserved (letting the daemon reject empty/null profiles instead of silently defaulting to `"edit"`/`"dev"`), while omitted CLI flags still default to `"edit"`/`"dev"`.
   - Guarded `os.path.abspath(os.path.expanduser(...))` on `binary` so blank, NUL-containing, or flag-like (`--...`) values are passed untouched for server validation rather than hitting `ValueError` or getting a `cwd/` prefix that masks leading `-`.
3. **`mesh task merge --project ""` truthiness** ([mesh/cli/commands/task.py](mesh/cli/commands/task.py)):
   - Changed `if getattr(args, "project", None):` in `handle_task_merge` to `is not None` so passing `--project ""` without `--all-accepted` exits `2` (`INVALID_ARGUMENT`) rather than being silently ignored.
4. **Embedded NUL-byte (`\x00`) path guards returning exit `2` instead of exit `1`** (`os.path.*` raising `ValueError: embedded null byte`):
   - [mesh/core/repos.py](mesh/core/repos.py) (`validate_repo`): added `"\x00" in path` check before `os.path.realpath`.
   - [mesh/cli/commands/intake.py](mesh/cli/commands/intake.py) (`handle_intake_submit`) & [mesh/cli/commands/project.py](mesh/cli/commands/project.py) (`_abs_repo`): skipped `os.path.abspath(repo)` on blank or NUL-containing strings so daemon `validate_repo` handles them cleanly.
   - [mesh/cli/commands/agent.py](mesh/cli/commands/agent.py) (`_check_out_path`) & [mesh/cli/commands/task.py](mesh/cli/commands/task.py) (`handle_task_submit` on `--deliverable`): rejected `"\x00"` with `MeshError(INVALID_ARGUMENT, EXIT_USAGE)` prior to `os.path.lexists` / `os.path.exists`.
5. **Sibling daemon wait handler (`project.wait`)** ([mesh/daemon/handlers/project.py](mesh/daemon/handlers/project.py)):
   - Added `isinstance(until, str)` validation to `handle_project_wait`, matching `handle_task_wait` so direct socket calls with `{"until": null | 123 | []}` raise `INVALID_ARGUMENT` exit `2`.
6. **Direct-RPC truthiness `or []` & type bypasses in daemon handlers**:
   - [mesh/daemon/handlers/task.py](mesh/daemon/handlers/task.py) (`handle_task_create`): replaced `ctx.args.get("depends_on") or []` with explicit `"depends_on" in ctx.args and ctx.args["depends_on"] is not None` and strict list-of-nonempty-strings check (so `depends_on: false` / `""` / `123` / `[""]` / `[{}]` fail exit `2`).
   - [mesh/daemon/handlers/project.py](mesh/daemon/handlers/project.py): replaced `ctx.args.get("depends_on_project") or []` in `_handle_intake_submit` (`project.create` / `intake.submit`) with explicit key/`None` checks; replaced `if criterion:` in `handle_project_update` with `has_criterion = "criterion" in ctx.args or "criteria" in ctx.args` so falsy non-list values (`False`, `0`) under `goal` cannot bypass criterion validation.
   - [mesh/daemon/handlers/approval.py](mesh/daemon/handlers/approval.py) (`handle_approval_request`): required `isinstance(category, str)` before `category in DEFAULT_POLICIES` (preventing `TypeError: unhashable type` on `dict`/`list`), and changed `if expires_in:` to `if expires_in is not None:` requiring a non-empty `str` (so `""`, `0`, `False`, `123` return exit `2` instead of falling back to default TTL).
   - [mesh/daemon/handlers/inbox.py](mesh/daemon/handlers/inbox.py) (`handle_inbox_ack`): replaced `ctx.args.get("messages") or []` with explicit key/`None` check, `isinstance(messages, list)`, and non-empty string element validation (preventing `TypeError` on direct RPC `{"messages": 123}` or unhashable items).

### Audited Items Confirmed Not Bugs

- **`handle_task_merge_all` (`mesh/cli/commands/task.py`)**: `if not getattr(args, "project", None):` enforces that `--project` is non-empty; passing `--project ""` with `--all-accepted` already exits `2`.
- **`setup --approval-mode` (`mesh/cli/commands/setup.py`)**: `default=None` is intentional so re-running `mesh setup` without `--approval-mode` preserves the existing configured mode; invalid non-`None` values are now rejected upfront.
- **`approval approve` / `reject` `--evidence-ref` (`mesh/cli/commands/approval.py`)**: `evidence_ref` is an optional string flag (`default=argparse.SUPPRESS`), and `_redact_opt` ([mesh/daemon/handlers/approval.py](mesh/daemon/handlers/approval.py)) handles `str` and non-string/falsy values safely without raising.
- **`task wait` / `project wait` / `inbox wait` `--timeout`**: `if not timeout_val:` rejects missing, `""`, `0`, and `None` with `INVALID_ARGUMENT` exit `2`.
- **`agent update` `allowed_model`/`allowed_effort`/`can_lead`/`option`/`strict_models` & `agent *` `capability`**: these intentionally use `default=None` because `None` in update requests means "leave field unchanged" (unlike `agent create` options with `default=[]`/`False`, where `null` is now rejected exit `2`), and `agent create` `--capability` with `None` maps to `DEFAULT_CAPABILITIES`.

## Decisions Made Without User Prompting

1. **Exempted JSON-nullable `--input` list in `mesh/cli/main.py`:** Kept `allow_null_val` strictly scoped to the four places where `null` has an explicit contract (`config set` `value`, `task submit` `deliverable` checked by `task.py`, `agent update` `default_model`/`default_effort` clearing, and `agent create|update|onboard` `max_concurrency` delegated to server-side `INVALID_CONCURRENCY` exit `2`), rejecting JSON `null` for all other arguments whose argparse default is not in `(None, argparse.SUPPRESS)`.
2. **Pre-RPC vs daemon ordering for `adapter probe --binary ""`:** Left empty `--binary ""` rejection in the daemon when `adapter_id` is supplied (while enforcing `adapter_id` presence client-side) so worker-token calls like `mesh adapter probe agy --binary "" --token-file worker.tok` still fail with `CAPABILITY_MISSING` exit `4` before argument-value validation (`INVALID_ARGUMENT` exit `2`), matching the server rule in `mesh/daemon/handlers/adapter.py`.
3. **Skip `abspath` on malformed CLI path arguments:** Rather than raising in CLI helper wrappers with bespoke error messages or silently prefixing `cwd` onto invalid strings (turning `--skip` into `/cwd/--skip`), `agent create`, `adapter probe`, `intake submit`, and `project create` leave blank, NUL-containing, or leading-`-` paths unexpanded when needed so server validators see the original invalid input, while `_check_out_path` and `--deliverable` fail early in the CLI before local file opens/stat calls.
4. **Portable NUL-byte test delivery:** Because POSIX `execve` rejects embedded `\x00` bytes inside command-line `argv` before Python starts, integration tests exercise NUL-byte paths via `--input -` (`json.dumps(...)`) while unit tests exercise both `--input` JSON and direct Python helper/validator paths (`validate_repo`, `_check_out_path`, `_abs_repo`, `handle_task_submit`, `handle_setup`).

## Verification

### Pre-fix baseline failure confirmation (on `fa89b4f` prior to production fixes)

- `umask 0022 && python3 -m unittest -v tests/unit/test_batch7_pr46_r2.py tests/integration/test_batch7_pr46_r2.py`: **Ran 17 tests in 3.849s — FAILED (failures=16, errors=3)** across Findings #1–#4 and same-class cases (`tests` were subsequently expanded from 17 to 18 tests with NUL-byte and `approval_mode` coverage, and mock adjustments for `agent onboard`).

### Post-fix verification runs (with `umask 0022`)

1. **New regression tests (3 consecutive runs, including minimal `PATH=/usr/bin:/bin`):**
   - Run 1: `umask 0022 && python3 -m unittest tests/unit/test_batch7_pr46_r2.py tests/integration/test_batch7_pr46_r2.py` -> **Ran 18 tests in 5.094s — OK** (13 unit + 5 integration).
   - Run 2: `umask 0022 && python3 -m unittest tests/unit/test_batch7_pr46_r2.py tests/integration/test_batch7_pr46_r2.py` -> **Ran 18 tests in 5.093s — OK**.
   - Run 3: `umask 0022 && PATH=/usr/bin:/bin python3 -m unittest tests/unit/test_batch7_pr46_r2.py tests/integration/test_batch7_pr46_r2.py` -> **Ran 18 tests in 5.166s — OK**.
2. **Related unit suites (2 batches):**
   - `umask 0022 && python3 -m unittest tests/unit/test_batch7_pr46_r2.py tests/unit/test_batch7_pr46_r1.py tests/unit/test_batch7_g5.py tests/unit/test_batch7_pr45_r1.py tests/unit/test_batch7_pr45_r2.py tests/unit/test_batch7_pr44_r1.py tests/unit/test_batch7_pr43_r2.py tests/unit/test_batch7_pr43_r3.py tests/unit/test_cli_usability.py tests/unit/test_adapters.py tests/unit/test_paths.py tests/unit/test_doctor.py tests/unit/test_linear_full_cli.py tests/unit/test_pr37_fixes.py tests/unit/test_pr39_r1.py tests/unit/test_pr39_r2.py tests/unit/test_repos.py tests/unit/test_repos_cas.py tests/unit/test_projects.py tests/unit/test_events.py tests/unit/test_settings.py tests/unit/test_probe_offline.py` -> **Ran 240 tests in 54.227s — OK**.
   - `umask 0022 && python3 -m unittest tests/unit/test_invariants.py tests/unit/test_helpdiff.py tests/unit/test_docs_m3.py tests/unit/test_docs_m19.py tests/unit/test_adapter_evidence.py tests/unit/test_vendor_failure_meaning.py tests/unit/test_redact.py tests/unit/test_worker_friction.py tests/unit/test_inbox_worker_kit.py tests/unit/test_batch7_g2.py tests/unit/test_batch7_g3.py tests/unit/test_batch7_g4.py tests/unit/test_full_review_fixes.py tests/unit/test_m4_p3_cleanup.py` -> **Ran 164 tests in 35.283s — OK**.
3. **Related integration suites (2 batches):**
   - `umask 0022 && python3 -m unittest tests/integration/test_batch7_pr46_r2.py tests/integration/test_batch7_pr46_r1.py tests/integration/test_batch7_g5.py tests/integration/test_batch7_pr45_r1.py tests/integration/test_batch7_pr45_r2.py tests/integration/test_batch7_pr44_r1.py tests/integration/test_batch7_pr43_r2.py tests/integration/test_batch7_pr43_r3.py tests/integration/test_setup.py tests/integration/test_agents.py tests/integration/test_adapters.py tests/integration/test_cli_usability.py tests/integration/test_linear_full_cli.py tests/integration/test_inbox.py tests/integration/test_events.py tests/integration/test_repo_projects.py tests/integration/test_pr39_r1.py tests/integration/test_submit_cli.py` -> **Ran 98 tests in 159.924s — OK**.
   - `umask 0022 && python3 -m unittest tests/integration/test_tasks.py tests/integration/test_projects.py tests/integration/test_approvals.py tests/integration/test_daemon.py` -> **Ran 89 tests in 199.187s — OK**.

## Round 3 (Gemini PR46 R3, 1 x P3)

- P3 adapter.probe binary validation: daemon _extract_binary_arg now rejects option-like (leading "-") and control-character (ord < 32 or 127, incl. NUL) binary paths with INVALID_ARGUMENT exit 2, matching agent.create / validate_adapter_options; the CLI client no longer abspath-expands control-char paths so the server sees the raw value. Regression: tests/integration/test_batch7_pr46_r2.py::TestBatch7Pr46R3AdapterProbeBinary (fails on ebc4faf: --binary=--skip was stored as <daemon cwd>/--skip with exit 0; adapter_probes row count must stay unchanged).
