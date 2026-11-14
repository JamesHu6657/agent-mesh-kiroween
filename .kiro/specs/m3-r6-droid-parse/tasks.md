Spec for m3-r6, written before implementation (2026-11-10), from m3-plan.md run m3-r6.

# Implementation Plan: m3-r6 (m3-droidparse), real droid `parse()`

Worktree `~/workspace/mesh-wt-m3-r6`, branch `wt/m3-r6`. Precondition: C2 is on main (log 2026-11-09 23:00; files under tests/fixtures/real/droid/1.6.0/ and tests/smoke/results/droid-1.6.0-2026-11-09{,-2}.json). m2-r11 is merged (log 2026-11-09 19:25), so tests/unit/test_adapters.py is not owned by an open run. Items: N24, N12, N6. Decision: D6. Estimate 60–75 min, one session of about 100–200 turns (workflow §5).

"Req" numbers refer to requirements.md; design sections refer to design.md. Each task is one commit `<type>(m3): <what> (ACC: <IDs>)` with `--author "<your coder string>"`. Run `bash scripts/test.sh` (rc 0, both tripwires empty) before every commit that touches code or tests.

## FILES (exact; m3-plan parallel map, row m3-r6)
- `mesh/adapters/droid.py`: only `parse()`, its private helpers and line 43. `build()` and its golden tests must not change.
- `mesh/adapters/kiro.py` (line 43 only, D6)
- `tests/unit/test_adapters_droid_real.py` (new) (planned)
- `tests/fixtures/synthetic/droid/*` (new; `synthetic_truncated.jsonl`, `synthetic_unknown_event.jsonl`)
- `tests/unit/test_adapters.py` (only the named `reasoning_effort` `smoke` pin, if one exists; none at base)
- `docs/impl-notes-m3-r6.md` (new)

## Never edit (m3-plan common block rule 1, verbatim, plus the M3 additions)
`docs/requirements.md, docs/plan.md, docs/acceptance-checklist.md, docs/review-*.md, docs/reviews/**, docs/coordination/**, prompts/**, scripts/coord/**, .kiro/**, docs/HANDOFF.md, docs/QUEST-HANDOFF.md, docs/release-requirements.md, docs/m1*-*.md, docs/impl-notes-m1*.md, logs/**, tests/fixtures/real/**`. M3 additions: `docs/impl-notes-m2*.md`, `docs/m2-gate-report.md`, `tests/smoke/results/**`. Only the coordinator adds files under `tests/fixtures/real/` and `tests/smoke/results/`; existing files there are never modified. Anything not in FILES is also out of bounds for r6, in particular `tests/unit/test_smoke_cases.py`, `tests/unit/test_fixture_hashes.py`, `tests/smoke/**`, `mesh/adapters/{base,evidence,smoke_schema}.py` and `mesh/daemon/**`.

## Rules carried from the common block
- Never weaken or delete an assertion. The only assertion r6 may change is a `reasoning_effort` `smoke` pin (D6), listed with its reason in the notes.
- Default suite only. Never run `droid`, `agy`, `kiro-cli`, `vibe`, `hermes` or `openclaw` (not even `--help`), never run the `MESH_PROBE=1`/`MESH_SMOKE=1` tiers, and never write `MESH_SMOKE*`/`MESH_PROBE*` into `os.environ`. Fixtures are read-only; temp files go only under `/tmp/mh-*`, created and removed by test code.
- If `test_fixture_hashes` fails, STOP and report the file, expected and actual hash.
- Shell: the m3-plan common block allow-list applies (one simple command per call; `bash scripts/test.sh` with a 900 s+ timeout).

## Tasks

- [x] 0. Baseline (no commit)
  - Run `bash scripts/test.sh` before any change and record rc, unit/integration/recovery counts and wall time (common block rule 6).
  - Confirm on base: `grep -rn "reasoning_effort" tests/` shows no pin of the class value `smoke` (design §8). `test_fixture_hashes` passes.
  - Recommended: write the test file from task 3 first, run `python3 -m unittest tests.unit.test_adapters_droid_real` (planned) on the old code, and save which assertions fail for the notes (fails-on-old evidence). Do not commit it yet.
  - _Requirements: 6.2, 8.2 (ACC: N15, A1)_

- [x] 1. `feat(m3): droid parse() from real stream-json (ACC: N24, N12)`
  - In `mesh/adapters/droid.py`, replace the body of `parse()` and add the private helper `_scan_stream(path) -> dict` exactly as in design §3: `final_text` and `usage` from the last `completion` event (`finalText`, `usage`); `session_id` from the first `system/init` event; the existing `exec-init-event-opus55-high.json` fallback, for the session id only; `vendor_exit_meaning=None`.
  - Add a comment in `parse()` that cites the event lines it relies on: `smoke-S-X-1-stdout.jsonl:1` (init), `:3` (assistant message, not used), `:4` (completion); `smoke-S-X-7-stdout.jsonl:6` (error, not used).
  - Read with `encoding="utf-8", errors="replace"`. Skip and count non-JSON lines (`malformed`); ignore non-object JSON and unknown `type`s; never raise (Req 4.1–4.3).
  - Do not read `message`, `result`/`final_text`, `tool_*` or `error` for `final_text` (Req 1.3). This keeps tests/unit/test_smoke_cases.py:154 and :194 (`parse_pending` with the stub) green. If either fails, STOP: the file is outside FILES.
  - Proof: task 3 tests; the full suite, including `test_smoke_cases`, `test_smoke_harness` and `test_adapters_build_golden_matrix`, stays green.
  - _Requirements: 1.1–1.4, 2.1–2.2, 3.1–3.2, 4.1–4.3, 6.1 (ACC: N24, N12, N6)_

- [x] 2. `fix(m3): demote class-declared reasoning_effort to help (D6)`
  - Change `verification="smoke"` to `verification="help"` on droid.py:43 and kiro.py:43, and nothing else on those lines (the note text stays).
  - If the suite shows an assertion pinning `smoke` for these, change it only if it is in tests/unit/test_adapters.py, and record it in the notes as the named D6 supersession. Anywhere else, STOP.
  - Proof: `test_reasoning_effort_class_value_is_help` (planned; task 3). Existing `ShowRowsTest` in tests/unit/test_adapter_evidence.py still gets `smoke` from S-X-5 evidence (Req 5.2).
  - _Requirements: 5.1–5.3 (ACC: N12)_

- [x] 3. `test(m3): droid parse on real fixtures (ACC: N24, N12, N6)`
  - Create the two synthetic fixtures with the exact content of design §9 (file tool only, no executable).
  - New tests/unit/test_adapters_droid_real.py (planned). Use a module helper `real_fixture(name)` that asserts the file's sha256 equals its sidecar's `sha256` (`tests.tierlib.read_sidecar`) before returning the path. Each test copies its input into `tempfile.mkdtemp(prefix="mh-droidparse-", dir="/tmp")` as `stdout.log`, asserts the path starts with `/tmp/mh-`, and cleans up with `addCleanup`. Every docstring carries `ACC:`. Expected values are in design §4.
    - `test_droid_parse_real_sx1_final_text_session_usage` (planned): S-X-1 gives `final_text == "MESH_SMOKE_OK"`, the init session id, `usage == ` the S-X-1:4 `usage` dict (`factory_credits` 13621), `vendor_exit_meaning is None`. Same check for S-X-5 as a subTest. Fails on old code: `final_text == ""`. ACC: N24, N6. Req 1.1, 1.2, 2.3, 3.1, 3.2.
    - `test_droid_parse_real_sx3_resumed_token` (planned): `MESH_SMOKE_OK` in `final_text`; session id `9e176d53-…`. Fails on old code: empty text. ACC: N24. Req 1.2.
    - `test_droid_parse_real_streams_without_completion` (planned): S-X-6, S-X-7, S-DROID-8 give `final_text == ""`, their init session ids, `usage is None`, and `_scan_stream(...)["malformed"] == 0`. Fails on old code: no `_scan_stream`. ACC: N24, N6. Req 1.3, 1.4, 3.2.
    - `test_droid_parse_init_fixture_1_3_0` (planned): the 1.3.0 init event, both as `stdout.log` and under its own name in an empty run dir, gives `d72c2698-16db-41f0-aa24-9b3090171deb` and `final_text == ""`. This is a regression guard ("still gives its session id"), expected to pass on old code too. ACC: N24. Req 2.2, 2.3.
    - `test_droid_parse_synthetic_robustness` (planned): docstring "robustness only, never evidence". Covers both synthetic files, the `\xff\xfe` run dir and an empty run dir, with design §4 values and no exception. `DroidAdapter().capabilities` `verification` values are identical before and after parsing. `MESH_SMOKE_OK` does not occur in the synthetic files. Fails on old code: `SYNTHETIC_FINAL` expected. ACC: N24, N12. Req 4.1–4.4.
    - `test_reasoning_effort_class_value_is_help` (planned): `DroidAdapter()` and `KiroAdapter()` `reasoning_effort` has `supported == "yes"` and `verification == "help"`, and the notes are unchanged (`exec-init-event-opus55-high.json`, `effort-high-run-metadata.jsonl`). Fails on old code: `smoke`. ACC: N12. Req 5.1.
    - `test_reevaluate_committed_droid_results_offline` (planned): load tests/smoke/results/droid-1.6.0-2026-11-09-2.json read-only. Under `mock.patch("subprocess.Popen", side_effect=AssertionError)`, call `tests.smoke.reevaluate.reevaluate_doc(doc, name, <repo>/tests/fixtures/real)`. Expect S-X-1 `pass` with `evaluated_from` set; S-X-2, S-X-4, S-X-5 `pass`; S-X-3, S-X-6, S-X-7, S-DROID-8 statuses unchanged with `carried_from`. Write nothing outside `/tmp/mh-*` and never call `reevaluate()`/`main()`. Fails on old code: S-X-1 `fail(parse_pending)`. ACC: N12, N24. Req 7.1.
  - Then run `python3 -m unittest tests.unit.test_adapters_droid_real` (planned), followed by the full suite.
  - _Requirements: 1–5, 7.1, 8.1 (ACC: N24, N12, N6)_

- [x] 4. `docs(m3): notes for r6`
  - New `docs/impl-notes-m3-r6.md` with: what was built (`droid.py:parse`, `droid.py:_scan_stream`; D6 lines); the event shapes used (design §2); the assertion changed (expected: none) and why; the fails-on-old evidence per new test; suite wall times before and after; facts found in code; any STOP.
  - _Requirements: 8.2 (ACC: A1)_

## Done criteria
- [ ] One commit per task 1–4, each preceded by a green `bash scripts/test.sh`.
- [ ] Final `bash scripts/test.sh` rc 0, both tripwire files empty, idle wall time under 600 s (unticked by the coordinator after the Linear review: rc 0 and tripwires verified, but there is no idle A1 evidence for this run; idle A1 is measured at the M3 gate) (before/after in the notes). (ACC: A1, A3)
- [x] Offline only: no vendor process started, no probe or smoke tier run, `os.environ` never given `MESH_SMOKE*`/`MESH_PROBE*`, every temp path under `/tmp/mh-`.
- [x] `grep -rn "ACC: N24" tests/` finds the passing tests in tests/unit/test_adapters_droid_real.py (planned). N12 and N6 are covered there too.
- [x] `test_fixture_hashes` unchanged and green; `git diff --name-status <base>..HEAD -- tests/fixtures/real tests/smoke/results` is empty. (ACC: N15, N21)
- [x] `build()` untouched: `git diff <base>..HEAD -- mesh/adapters/droid.py` shows only `parse()`, helpers and line 43. kiro.py shows line 43 only.
- [x] No assertion weakened. The only allowed change is a named D6 pin, if any.
- [ ] `git branch --list 'mesh/*'` in the checkout prints nothing.
- [x] `docs/impl-notes-m3-r6.md` exists.
- Final print (common block): commits (sha + subject); files changed; test commands with exit codes; counts and wall times before/after; ACC IDs (full/partial); deviations including tripwire state; anything not done, with STOPs.

## STOP conditions
- `test_fixture_hashes` fails.
- A needed change falls outside FILES, including any red assertion in tests/unit/test_smoke_cases.py or tests/unit/test_smoke_harness.py.
- A real fixture's sha256 differs from its sidecar.
- The C2 files named above are missing on the base.
