# Implementation notes: m3-r2 (m3-smokecore): results schema and smoke harness core

Coder: Quest. Branch `wt/m3-r2`, from wt/m3-r1 d20c1d6. r1b (fc5ddc2..3bc2799) was merged in at 66135d5 after Kiro's REVISE on r1. Items: N11 (build), N21, N25, N12 (schema). Decisions: Q2, Q4, Q13.

Suite wall time:
- Before: 8m32s (512 s, the r1 head).
- After the r2 code: 7m34s (454 s), rc 0, idle, tripwires empty.
- Final, after merging r1b (66135d5 plus notes): rc 0 twice, tripwires empty, 292/239/17 tests OK. Both runs overlapped other suites that started after mine: m2-r9 on the first (13m50s), m2-r10 and the main checkout on the second (14m58s). The idle-equivalent time is the 454 s measured above; the coordinator should rerun once the box is idle.

## What was built

### `mesh/adapters/smoke_schema.py` (pure)
- `SCHEMA = "mesh.smoke.v1"`; `STATUSES` lists the six statuses.
- `CASE_CAPS` uses existing capability keys only. A test checks every mapped key against `DroidAdapter`/`KiroAdapter().capabilities`.
- `LAUNCH_CASES = ("S-X-1", "S-X-6")`.
- `capabilities_used(adapter)` returns the keys with `supported == "yes"`. For droid that is headless, model_selection, reasoning_effort and stream_output, and each has a mapped case. `resume` is `unknown`, so it is not "used".
- `cases_for_capability(cap)`.
- `fixture_rel(a, v, name)` returns `tests/fixtures/real/<a>/<v>/<name>`, the path a results doc records. It stays valid before and after promotion.
- `validate(doc, repo_root, fixture_root=None)` returns a list of errors. It checks:
  - exact doc and case keys;
  - schema, adapter, numeric version, absolute binary, model ≠ `auto`, timestamp format, `profile == "readonly"`, positive timeout;
  - case ids are known and unique; status is valid; any status other than `pass` or `observed` needs a reason; types of rc and duration;
  - `fixture` is exactly `tests/fixtures/real/<adapter>/<version>/<name>` with no traversal;
  - `synthetic_*` names are refused (also in `evaluated_from` and `carried_from`);
  - the file exists under `fixture_root` (staging) or `<repo>/tests/fixtures/real`, and has an exact-key sidecar whose hash matches;
  - `fixture_sha256` matches the file;
  - a `pass` on S-X-1 has a fixture.
- `results_name` follows Q4 (`-2`, `-3`, …). `parse_results_name`. `load_latest(dir, adapter)` takes the highest (date, suffix) and ignores sidecars, other adapters and an invalid `-1` suffix.

### `tests/smoke/harness.py` (coordinator tier; `tests/smoke/__init__.py`)
- `gate(env, adapter)`: `MESH_SMOKE=1`, and the adapter is in both `MESH_SMOKE_ADAPTERS` and `MESH_SMOKE_CONSENT`. Otherwise it returns a reason.
- `load_config(env, adapter, binary)`:
  - The gate runs first and raises `SmokeSkip`. Config problems raise `SmokeConfigError`. Nothing spawns in either case.
  - The binary must be absolute, an executable file, and not under tests/shims.
  - `MESH_SMOKE_MAX_RUNS` must be an int ≥ 1.
  - `MESH_SMOKE_MODEL_<ADAPTER>` is required; `auto` is refused in any case.
  - `MESH_SMOKE_TIMEOUT_S` defaults to 180.
  - `MESH_SMOKE_OUT` must start with `/tmp/mh-` and its realpath must stay under `/tmp/mh-*`; it may not be inside the repo.
  - `MESH_SMOKE_CONSENT_REF` is optional.
- `SmokeSession(env, adapter, binary, *, grace_s=15, version=None, mesh_commit, effort)`:
  - `version` defaults to `adapter.probe(binary_path=binary).version`, a read-only call.
  - The workspace is `mkdtemp(prefix="mh-smoke-", dir="/tmp")`; one is refused if it is inside the repo.
  - `run(case_id, prompt, effort=, resume_session_id=, timeout_s=)` returns `None` at the cap without spawning.
    - It writes the prompt into a private run dir under `$OUT/.runs`.
    - The argv comes from `adapter.build(RunRequest(..., profile="readonly", options={"binary", "run_id"}))`. It asserts `argv[0]` is the absolute binary and the cwd is outside the repo.
    - `Popen` gets `start_new_session=True`, stdin DEVNULL, and env = `env` minus `MESH_*`.
    - On wall timeout, `terminate_group` sends SIGTERM to the group, polls up to `grace_s`, sends SIGKILL to the group, then waits until the group is gone. The returned outcome has `rc=None` and `timed_out=True`; `record` turns that into `observation: "hang"`.
    - After a normal exit the group is SIGKILLed too, so no member outlives the run.
    - "Group gone" scans `/proc` with `mesh/core/proc.get_proc_stat`, and zombies don't count. The first version used `killpg(0)` and flaked under suite load.
    - Output is redacted with `redact(text, collect_exact_values(env))`, which uses exact env secrets plus patterns.
    - Raw files are deleted, and a redacted `stdout.log` is kept so `adapter.parse()` can read it.
    - `tierlib.write_capture` writes `<adapter>/<version>/smoke-<case>-stdout.jsonl` and `-stderr.txt`. The sidecar `command` is the redacted argv with the binary basename.
  - `record`, `run_or_cap` (records `not_run(run_cap)`), `document`.
  - `finish()` validates against the staging tree and writes `results/<Q4 name>` with a sidecar, using `O_EXCL`. It then removes the workspace and `.runs`.
- `main` (`python3 -m tests.smoke.harness <adapter> --prompt …`) is the only reader of `os.environ`. It finds the binary with `shutil.which` on a PATH without shims. It does one gated run, as a coordinator tool; the case suite is r3.

### `tests/fixtures/synthetic/smoke/synthetic_droid_stream.jsonl`
Three hand-written lines (init, message, result). Its note field says it is SYNTHETIC.

## Tests: `tests/unit/test_smoke_harness.py` (8 tests, about 2.4 s)
Every test asserts that `os.environ` has no `MESH_SMOKE*`/`MESH_PROBE*` in both setUp and tearDown. The stub path is checked to start with `/tmp/mh-`, and every env is a dict.

- **Gate (plan cases a, b):** an empty env, missing consent, `MESH_SMOKE=0`, and an unlisted adapter all raise `SmokeSkip`, with 0 spawns and no OUT dir.
- **Config (plan case i, and more):** 14 refusals, all with 0 spawns: relative binary, bare name, MAX_RUNS missing/0/garbage, model missing, `auto`/`AUTO`, OUT missing, OUT in `/tmp/` but not `mh-`, OUT in the repo, OUT with traversal, timeout 0, a missing binary. A default timeout of 180 is also checked.
- **Run (plan cases c, d, e, f):**
  - argv uses the absolute binary and the readonly profile, with no `--auto`.
  - The child cwd is the `/tmp/mh-smoke-*` workspace, and the child sees no `MESH_*` variables.
  - `parse()` reads the redacted `stdout.log`.
  - A cap of 1 makes the second case `not_run(run_cap)` with no extra spawn.
  - OUT holds exactly 6 files, and the planted `fk-…` secret appears in none of them. `[REDACTED` is present.
  - Sidecars have exactly the three keys and a matching hash.
  - The doc validates against staging and against a temp repo after `tierlib.promote`.
  - No new files under the checkout's `tests/` (excluding `__pycache__`).
- **Hang (plan case h):** the stub ignores SIGTERM and has a SIGTERM-ignoring child. With a 1 s timeout and `grace_s=1`: `timed_out`, `rc` None, `group_gone`, and it takes between 1.9 and 10 s. Both pids are gone or zombies, and the observation is `hang`.
- **Names:** two `finish()` calls on the same day give `-2`, and `load_latest` picks it.
- **Schema:**
  - CASE_CAPS keys are real; launch cases, statuses and `capabilities_used` are checked.
  - **validate (plan case g):** 22 bad mutations fail, including synthetic path, synthetic name and synthetic `evaluated_from`. A missing sidecar and a tampered file also fail.
  - Q4 names and `load_latest` ordering.

## Deviations / readings
- **Stub:** it is written by the test, under `/tmp/mh-*`, not taken from `tests/stubs.py`. The stubs.py droid stub trips on `exec`, and stubs.py is outside r2's FILES.
- **Probe calls in run counts:** the session's version probe runs `--version`/`--help` on the stub. These are read-only and are not counted as model runs. Tests count only `exec … --file` calls.
- **mesh_commit:** it is a parameter (default `unknown`), because the harness doesn't run git against the checkout. r3 or C2 passes it in.
- **Commit timing:** sub-steps 1–3 were committed after one full suite run on the combined r2 tree, then r1b was merged. The final suite run covers the merge.
- No vendor binary was run. MESH_SMOKE/MESH_PROBE were never set. No assertions changed.

## r2b: Kiro's r1b+r2 minors (review-m3-r1b-r2.md, ACCEPT + 7 minors)

Suite wall time: before r2b, 454 s (r2 code, idle); after r2b, see below.

1. **Interrupted run (finding 1).**
   - `SmokeSession.run` wraps the spawn and wait in `except BaseException: terminate_group(proc, grace_s); raise`.
   - A `finally` deletes `raw-stdout`/`raw-stderr` whether the run succeeds or fails.
   - Test: patched `Popen.wait` raises KeyboardInterrupt. The stub and its child are gone, no `raw-*` file is left, and the interrupt propagates.
2. **Session-wide kill (finding 2).**
   - `_group_alive` counts a process if `pgrp == pgid or session == pgid`. `session_pgids` and `signal_session` signal every distinct pgid in the session.
   - Used by `terminate_group` (SIGTERM and SIGKILL) and by the normal-exit path (SIGKILL, then a 10 s poll).
   - `record()` adds `leftover_processes: true` to the observation when `outcome.group_gone` is False. A non-dict observation is wrapped as `{"value": …}`.
   - Tests: a `setpgid(0,0)` escapee and a child left behind by a normally exiting leader are both killed, with `group_gone` True. `record` is also checked with a forced `group_gone=False`.
3. **Duplicate case (finding 3).** `refuse_existing_capture(case_id)` runs before the run is counted. It raises `SmokeConfigError` if any of the case's four capture paths exists. Test: one model run, and a second session on the same OUT is refused too.
4. **Staged C2 (finding 4, coordinator decision: carry).**
   - `SmokeSession.carry_staged()` is called by `finish()`. It loads the latest results doc in `OUT/results` for this adapter and version (sidecar and `validate` checked).
   - It copies every case this session didn't record, with `carried_from = tests/smoke/results/<that file>`. A case carried earlier keeps its original source.
   - Cases are sorted in `CASE_IDS` order.
   - The latest doc is therefore complete: S-X-1 from stage 1 and S-X-6 from stage 2. Test: three stages.
5. **validate strictness (finding 5).**
   - (a) `fixture` basename must match `smoke-S-[A-Z0-9-]+-stdout.jsonl`.
   - (b) `_check_sidecar` checks the command list and timestamp format, like `tierlib.read_sidecar`.
   - (c) `evaluated_from`/`carried_from` are string or null only.
   - (d) `smoke_schema.version_ok`, checked in `SmokeSession.__init__` before any spawn.
   - One test mutation per item.
6. **Test hygiene (finding 6).**
   - Every session in test_smoke_harness is registered with `addCleanup(s.cleanup)`.
   - New `main()` closed-gate subprocess test with an explicit env: rc 0, `skipped`, nothing spawned.
   - The dead `all_outside` helper is removed from test_tierlib.
7. **Docs (finding 7).**
   - The test_probe.py docstring sentence is fixed.
   - Process: the r2b fix and test commits follow one suite run on the combined tree, as before. The merge commit author was the worktree default; r2b's commits use the Quest author.

No assertions changed. Existing tests are untouched except for the added `addCleanup` lines and the new mutations.
