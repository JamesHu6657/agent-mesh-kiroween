# Batch 7 PR #46 Round-4 Implementation Notes

This note records verification against commit `10dc64f` (`cloud/pr46r4-base`), documentation (`docs/plan.md` §7.1, §13.1–§13.2, `docs/usage.md`), root-cause fixes, same-class audit findings, design decisions made without user prompting, and verification runs for branch `pr46-r4`.

Both round-4 review findings (1x P2, 1x P3) in `review.md` were verified as real against `10dc64f`, reproduced with failing regression tests in [tests/unit/test_batch7_pr46_r4.py](tests/unit/test_batch7_pr46_r4.py) and [tests/integration/test_batch7_pr46_r4.py](tests/integration/test_batch7_pr46_r4.py), and resolved with minimal root-cause changes without altering existing callers' behavior or exit codes.

## Verified Round-4 Findings and Fixes

### 1. [P2] `handle_task_merge_all` re-reading passphrase on copied per-RPC `Namespace`s (`PASSPHRASE_INVALID` exit 3 on pipe `--passphrase-fd`, `1 + N` TTY prompts)

- **Doc & Code Verification:** Confirmed real. `docs/plan.md` §7.1 and [mesh/core/passphrase.py](mesh/core/passphrase.py) specify `--passphrase-fd` (pipe or stdin fd) and single interactive `/dev/tty` prompts cached on `args._resolved_auth` (`MeshClient.resolve_auth` in [mesh/client.py](mesh/client.py)). In `10dc64f`, `handle_task_merge_all` ([mesh/cli/commands/task.py](mesh/cli/commands/task.py)) constructed `lister = MeshClient(home=args.home)` and called `lister.execute_cmd(list_args, "task.list", ...)` on a copy `list_args = _rpc_args()` created before `args._resolved_auth` existed, without propagating `_resolved_auth` back to `args`. Subsequent per-task merges created fresh `sub = _rpc_args(tsk)` copies and new `MeshClient` instances inside `_merge_one(sub)`, causing `read_passphrase` to run `1 + N` times:
  1. End-to-end with `--user --passphrase-fd <pipe_fd>` on a 2-task accepted queue, `task.list` drained the non-seekable pipe and the first `_merge_one` failed with exit `3` (`PASSPHRASE_INVALID`: `"Passphrase cannot be empty"`).
  2. Unit-level with 2 accepted tasks (both JSON and `--format text`), `read_passphrase` was called `3` times (`1 + 2`) instead of `1`.
- **Root Cause & Fix:**
  - [mesh/cli/commands/task.py](mesh/cli/commands/task.py) (`handle_task_merge_all`): Called `lister.resolve_auth(args)` right after `lister = MeshClient(home=args.home)` and before building `list_args = _rpc_args()` (or `auth_args` / `sub`). Because `resolve_auth(args)` populates `args._resolved_auth`, `list_args`, `auth_args` (empty queue `verify_merge_auth`), every `sub = _rpc_args(tsk)`, and `_merge_progress_note`'s `show_args` (via `copy.copy(sub)` in `--format text`) all inherit `_resolved_auth` and call `read_passphrase` exactly once.
- **Same-Class Audit:**
  - `agent onboard` ([mesh/cli/commands/agent.py](mesh/cli/commands/agent.py)) already propagates `_resolved_auth` via `_sync_req_meta(create_args, args)` before copying `issue_args`.
  - `_merge_one` ([mesh/cli/commands/task.py](mesh/cli/commands/task.py)) already calls `client.resolve_auth(args)` before spawning `_merge_progress_note`.
  - `task keepalive` ([mesh/cli/commands/task.py](mesh/cli/commands/task.py)) reuses a single `MeshClient` instance whose `self._cached_auth` caches credentials after the first call.

### 2. [P3] `parse_and_apply_input_file` allowing synonym + canonical key in the same `--input` JSON object

- **Doc & Code Verification:** Confirmed real. `docs/plan.md` §13.1–§13.2 requires invalid/conflicting CLI & `--input` JSON parameters to fail with exit `2` (`EXIT_USAGE`), and [mesh/cli/main.py](mesh/cli/main.py) defines `SYNONYMS` (`allowed_models`→`allowed_model`, `allowed_efforts`→`allowed_effort`, `capabilities`→`capability`, `options`→`option`, `depends_on`→`on`, `criteria`→`criterion`, `depends_on_projects`→`depends_on_project`). In `10dc64f`, `parse_and_apply_input_file` rejected CLI-vs-JSON synonym duplicates via `canonical_key in cli_specified_flags`, but did not reject a synonym and its canonical key (or two synonyms of the same parameter) appearing in the same JSON object. When a synonym appeared before its canonical key:
  1. `mesh intake submit --input -` with `{"title": "T", "goal": "G", "lead": "lead1", "criteria": 123, "criterion": ["C1"]}` overwrote `args.criterion` with `["C1"]`, skipped validating `args.criteria = 123`, and created the project with exit `0` instead of rejecting `DUPLICATE_PARAM` (exit `2`).
  2. `mesh agent create --input -` with `{"name": "a_syn", "adapter": "fake", "delivery": "launch", "allowed_models": ["m1"], "allowed_model": ["m2"], "default_model": "m2"}` validated `args.allowed_model = ["m2"]` in `main.py` while `handle_agent_create` ([mesh/cli/commands/agent.py](mesh/cli/commands/agent.py)) read and sent the unvalidated `args.allowed_models = ["m1"]`, failing in the daemon with `DEFAULT_MODEL_NOT_ALLOWED` instead of `DUPLICATE_PARAM` (exit `2`).
- **Root Cause & Fix:**
  - [mesh/cli/main.py](mesh/cli/main.py) (`parse_and_apply_input_file`): Added `if canonical_key in from_json or key in from_json:` right after the `cli_specified_flags` check, raising `MeshError(code=c.DUPLICATE_PARAM, exit_code=c.EXIT_USAGE, details={"param": key})`. Because `parse_and_apply_input_file` mirrors every single synonym onto both `args.<key>` and `args.<canonical_key>`, rejecting in-JSON duplicates ensures both attributes always hold the same value, so the validation loop always validates the exact value sent by the command handler. `task create --input` with `depends_on` continues to work cleanly because `"on"` is not in `task create`'s `valid_params` and `depends_on` is checked directly against `action_by_dest["depends_on"]`.

## Decisions Made Without User Prompting

1. **Pre-RPC `lister.resolve_auth(args)` vs post-RPC sync in `handle_task_merge_all`:** Chose calling `lister.resolve_auth(args)` upfront before `_rpc_args()` rather than copying `_resolved_auth` back from `list_args` after `task.list`. Both invoke `read_passphrase` once and fail identically on invalid auth flags/sources (since `MeshClient.execute_cmd` already calls `resolve_auth` before `send_request`), while upfront resolution avoids relying on mutable post-call back-syncing.
2. **Error `details` for JSON synonym duplicates:** Used `details={"param": key}` on `DUPLICATE_PARAM` (`EXIT_USAGE`), matching the existing CLI-vs-JSON `DUPLICATE_PARAM` check immediately above it in `parse_and_apply_input_file`.
3. **Module lock marker in `tests/unit/test_batch7_pr46_r4.py`:** Added `MESH_PROC_LOCK = "shared"` at module level per `tests/run_tier.py` and the task instructions, matching other unit regression modules (`test_batch7_g2.py`, etc.).

## Verification

### Pre-fix baseline failure confirmation (on `10dc64f` prior to production fixes)

- `python3 -m unittest -v tests/unit/test_batch7_pr46_r4.py tests/integration/test_batch7_pr46_r4.py`: **Ran 7 tests — FAILED (failures=7)** (every unit and integration test failed reproducing Finding #1 `read_passphrase.call_count == 3` / `PASSPHRASE_INVALID` exit `3` and Finding #2 unrejected synonym+canonical JSON inputs).

### Post-fix verification runs (with `umask 0022`)

1. **New regression tests (3 consecutive runs, including minimal `PATH=/usr/bin:/bin`):**
   - Run 1 (`umask 0022`): `python3 -m unittest -v tests/unit/test_batch7_pr46_r4.py tests/integration/test_batch7_pr46_r4.py` -> **Ran 7 tests in 5.978s — OK**.
   - Run 2 (`umask 0022`): `python3 -m unittest tests/unit/test_batch7_pr46_r4.py tests/integration/test_batch7_pr46_r4.py` -> **Ran 7 tests in 5.989s — OK**.
   - Run 3 (`umask 0022`, `PATH=/usr/bin:/bin`): `PATH=/usr/bin:/bin python3 -m unittest tests/unit/test_batch7_pr46_r4.py tests/integration/test_batch7_pr46_r4.py` -> **Ran 7 tests in 6.010s — OK**.
2. **Related unit suites:**
   - `umask 0022 && python3 -m unittest tests/unit/test_batch7_pr46_r4.py tests/unit/test_batch7_pr46_r2.py tests/unit/test_batch7_pr46_r1.py tests/unit/test_batch7_g5.py tests/unit/test_batch7_g3.py tests/unit/test_batch7_pr44_r1.py tests/unit/test_cli_usability.py tests/unit/test_full_review_fixes.py tests/unit/test_linear_full_cli.py tests/unit/test_home_resolution_r3.py tests/unit/test_productize_round3.py tests/unit/test_skeleton.py` -> **Ran 195 tests in 17.696s — OK**.
3. **Related integration suites:**
   - `umask 0022 && python3 -m unittest tests/integration/test_batch7_pr46_r4.py tests/integration/test_batch7_pr46_r2.py tests/integration/test_batch7_pr46_r1.py tests/integration/test_batch7_g5.py tests/integration/test_friction5.py tests/integration/test_sakana_cli.py tests/integration/test_linear_full_cli.py tests/integration/test_pr39_r1.py tests/integration/test_cli_usability.py tests/integration/test_merge.py` -> **Ran 59 tests in 99.693s — OK**.
   - `umask 0022 && python3 -m unittest tests/integration/test_agents.py tests/integration/test_projects.py tests/integration/test_tasks.py` -> **Ran 53 tests in 105.564s — OK**.
