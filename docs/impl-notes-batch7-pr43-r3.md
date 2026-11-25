# Batch 7 PR #43 Round-3 Review Notes

Both P2 findings reported in `review.md` (mirroring the Drive round-3 review) were verified as real bugs against code and documentation (`docs/plan.md` §9.1 and §14.4 D17) and fixed with regression tests in `tests/unit/test_batch7_pr43_r3.py` and `tests/integration/test_batch7_pr43_r3.py`.

## Findings Verified and Fixed

1. **P2-1 — Inbox-mode `agent create` / `agent update` allowed non-empty `default_model` when `allowed_models == []` and could not clear `default_model` to `null`/`""`:**
   - **Verification:** Confirmed against `docs/plan.md` §9.1 (`default_model` must be in `allowed_models` or return exit 2 `DEFAULT_MODEL_NOT_ALLOWED`; narrowing `allowed_models` without clearing/updating `default_model` must fail exit 2) and `mesh/daemon/handlers/task.py` (`handle_task_start`), which treats `allowed_models == []` as allowing no non-empty model. Both `handle_agent_create` and `handle_agent_update` in `mesh/daemon/handlers/agent.py` gated the inbox check behind `and allowed_models`, while `mesh/cli/commands/agent.py` always sent `"default_model": args.default_model` (`None` when omitted) and `handle_agent_update` ignored `None` (`if "default_model" in ctx.args and ctx.args["default_model"] is not None:`). As a result, inbox agents with empty `allowed_models` accepted `default_model`, and clearing `allowed_models` left stale non-null `default_model` values un-clearable and caused subsequent task starts to fail `MODEL_NOT_ALLOWED`.
   - **Root-cause fix:**
     - `mesh/cli/commands/agent.py` (`handle_agent_update`): Mirror `default_effort` handling by deleting `"default_model"` from the RPC payload when `args.default_model is None` and `"default_model"` was not explicitly supplied in `--input` JSON (`args._from_json`), so explicit `"default_model": null` in JSON reaches the daemon while omitted CLI flags preserve the existing column.
     - `mesh/daemon/handlers/agent.py` (`_validate_model_name`, `handle_agent_create`, `handle_agent_update`):
       - Guard `_validate_model_name` with `not isinstance(model, str)` before `len()` / character iteration so malformed JSON types fail with `INVALID_ARGUMENT` rather than `TypeError`.
       - Normalize `default_model == ""` to `None` in `handle_agent_create` for non-`none` inbox agents and validate `default_model in allowed_models` without `and allowed_models` gating.
       - In `handle_agent_update`, interpret explicit `ctx.args["default_model"] in (None, "")` as `default_model = None` (skipping `_validate_model_name` when `None`, while launch-mode agents still fail `DEFAULT_MODEL_NOT_ALLOWED` since `None` and `""` are never in `allowed_models`), and remove `and allowed_models` from the inbox membership check.

2. **P2-2 — `scheduler.check_queued_tasks_t6` and `handle_task_start` missed AGY's model-suffix effort rule (D17), duplicated resolution/validation, and `handle_task_start` dropped resolved `default_effort` from `runs.effort`:**
   - **Verification:** Confirmed against `docs/plan.md` §9.1 & §14.4 (D17) and `mesh/adapters/agy.py` (`AgyAdapter.build`). Neither `scheduler.check_queued_tasks_t6` nor `handle_task_start` checked AGY's `model_encodes_effort` rule across resolved `(task.model or agent.default_model, task.effort or agent.default_effort)`, so updating an AGY agent to add `default_effort` allowed queued suffixed-model tasks to enter `supervisor` (consuming an `attempt` and failing as `spawn_failed`) or start in inbox mode with exit 0. Additionally, `handle_task_start` validated resolved `task_effort` (`task["effort"] or agent["default_effort"]`) but inserted raw `task["effort"]` (`NULL`) into `runs.effort`.
   - **Design decisions:**
     - Placed single-source helpers in `mesh/core/policy.py` (`AGY_EFFORT_SUFFIXES`, `parse_json_str_list`, `resolve_model_and_effort`, `check_agy_model_effort_conflict`, `validate_resolved_effort`) with lazy exception imports inside helper bodies so `mesh.core` introduces no import-time cycle with `mesh.adapters.__init__` (which eagerly imports `mesh.adapters.agy`).
     - Preserved module-level `get_adapter` mockability in both `mesh/daemon/scheduler.py` (`scheduler.get_adapter`) and `mesh/daemon/handlers/task.py` (`task.get_adapter`) by passing the resolved `adapter` instance (or explicit `None` using an `_UNSET` sentinel when `get_adapter` errors in `T6`) into `validate_resolved_effort`.
     - Kept `check_resolved_model=False` on `create`/`assign`/`reassign`/`retry`/`reject` calls to `_validate_model_and_effort` (matching existing `tests/integration/test_tasks.py` expectations that `task retry` without `--model` succeeds and transitions through `T6`), while `handle_task_start` passes `check_resolved_model=True`.
     - Kept `T6` effort/AGY suffix validation scoped to `delivery == "launch"`, preserving synchronous `EFFORT_NOT_ALLOWED` errors on `mesh task start` / `mesh task reject` for `delivery == "inbox"`.
   - **Root-cause fix:**
     - `mesh/core/policy.py`: Added `AGY_EFFORT_SUFFIXES`, `parse_json_str_list`, `resolve_model_and_effort`, `check_agy_model_effort_conflict`, and `validate_resolved_effort`.
     - `mesh/adapters/agy.py` & `mesh/daemon/handlers/agent.py`: Reused `AGY_EFFORT_SUFFIXES` and `check_agy_model_effort_conflict`.
     - `mesh/daemon/supervisor.py`: Delegated `start_launch_run` `(model, effort)` resolution to `resolve_model_and_effort`.
     - `mesh/daemon/scheduler.py` (`check_queued_tasks_t6`): Selected `a.default_model`, resolved `(resolved_model, resolved_effort)` via `resolve_model_and_effort`, checked `resolved_model not in allowed_models`, and called `validate_resolved_effort` for `delivery == "launch"` tasks (mapping `EFFORT_UNSUPPORTED` to `effort_unsupported` and `EFFORT_NOT_ALLOWED` / `model_encodes_effort` to `effort_not_allowed` with preserved `exc.details`).
     - `mesh/daemon/handlers/task.py`: Added `check_resolved_model: bool = False` to `_validate_model_and_effort` delegating to `mesh.core.policy`; replaced inline model/effort checks in `handle_task_start` with `_validate_model_and_effort(..., check_resolved_model=True)`; and persisted resolved `task_effort` instead of raw `task["effort"]` into `runs.effort`.

## Decisions & Test Design

- **`noexec /tmp` compatibility in integration tests:** `tests/integration/test_batch7_pr43_r3.py` avoids writing executable stubs under `/tmp` and instead relies on `MeshHarness`'s repository-local `tests/shims/agy` and `h.tripwire_file` (with `adapter.evidence_mode="warn"`), proving both `noexec /tmp` compatibility and zero invocation of `agy` during `T6` / `task.start` validation.
- **Pre-existing queued task `model` snapshot semantics:** `task create` eagerly records `agent.default_model` onto `tasks.model` when omitted. Accordingly, clearing `allowed_models: []` and `default_model: null` causes an already-queued task with captured `tasks.model` to fail `T6` `model_not_allowed`, whereas any task created after the update inherits `model = None` and starts cleanly (`test_p2_1_inbox_agent_create_update_default_model_and_clear` tests both behaviors).

## Verification

- Bidirectional import check (`mesh.core.policy` before `mesh.adapters.agy` and vice versa): exit `0`.
- `tests.unit.test_batch7_pr43_r3` + `tests.integration.test_batch7_pr43_r3`: `8` tests passed across `3` consecutive runs (`OK` in `7.395s`, `7.312s`, `7.540s`).
- Related unit tests (`test_batch7_pr43_r3`, `test_batch7_pr43_r2`, `test_batch7_pr43_r1`, `test_batch7_g2`, `test_adapters`, `test_adapters_agy_real`, `test_scheduler`, `test_launch_hold_queue`, `test_settings`, `test_blind_review_unit`, `test_blind_review_b34`): `178` tests `OK` (`32.509s`).
- Related integration tests:
  - `test_tasks`, `test_inbox`, `test_execution_launch`, `test_execution_inbox`: `61` tests `OK` (`141.568s`).
  - `test_batch7_pr43_r3`, `test_batch7_pr43_r2`, `test_batch7_pr43_r1`, `test_batch7_g1`, `test_batch7_g2`, `test_agents`, `test_scheduler`, `test_launch_hold_queue`, `test_blind_review`: `53` tests `OK` (`140.727s`, run after all `mesh/` changes).

