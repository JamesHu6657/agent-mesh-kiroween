# Implementation notes: m3-r6 (m3-droidparse), real droid `parse()`

Spec: `.kiro/specs/m3-r6-droid-parse/` (requirements, design, tasks). Plan: docs/coordination/plans/m3-plan.md "Run m3-r6", decision D6, risk 2. Base: `3bb4a6b` on `wt/m3-r6`. Items: N24, N12, N6.

**Not committed.** tasks.md asks for one commit per task. The run prompt from James/Quest said "Do not commit", which overrides tasks.md, so all changes are left in the working tree for the coordinator to commit.

## What changed
| File:line | Change |
|---|---|
| `mesh/adapters/droid.py:43` | D6: `reasoning_effort` `verification="smoke"` → `"help"`; note unchanged. |
| `mesh/adapters/droid.py:85–104` (`DroidAdapter.parse`) | Body replaced: uses `_scan_stream` on `stdout.log`, keeps the `exec-init-event-opus55-high.json` fallback for the session id only, returns `final_text`, `session_id`, `usage`, `vendor_exit_meaning=None`. The comment cites the fixture lines the rule relies on. The dead `os.path.isabs` branch was dropped (design §3 allows this). |
| `mesh/adapters/droid.py:275–309` (`_scan_stream`, module-level, private) | New single-pass scanner, returns a plain dict `{session_id, final_text, usage, malformed}`. |
| `mesh/adapters/kiro.py:43` | D6: same one-word change; note unchanged. |
| `tests/unit/test_adapters_droid_real.py` | New, 7 tests (see below). |
| `tests/fixtures/synthetic/droid/synthetic_truncated.jsonl` | New, exact content of design §9 (3 lines, no trailing newline). |
| `tests/fixtures/synthetic/droid/synthetic_unknown_event.jsonl` | New, exact content of design §9 (8 lines). |
| `.kiro/specs/m3-r6-droid-parse/tasks.md` | Ticks only. |

`build()`, `profile_flags()`, `probe()`, `list_models()`, imports, `base.py`, `evidence.py` and everything under `tests/fixtures/real/` and `tests/smoke/results/` are untouched. `tests/unit/test_adapters.py` is untouched.

## Parse rule (droid 1.6.0 stream-json, from the C2 fixtures)
- `session_id`: the non-empty string `session_id` of the **first** `{"type":"system","subtype":"init"}` event (smoke-S-X-1-stdout.jsonl:1). No fallback to `session_id` fields on other events. If `stdout.log` gives none, the same scan runs on `exec-init-event-opus55-high.json` in the run dir, and only its session id is used.
- `final_text`: `finalText` of the **last** `{"type":"completion"}` event (S-X-1:4, S-X-3:4, S-X-5:4) if it is a string, else `""`.
- `usage`: a shallow copy of that same completion's `usage` dict (keys unchanged, including `factory_credits`), else `None`. A later completion without a dict `usage` resets it to `None`.
- Not used for `final_text`: `message` events of any role (S-X-1:3), `tool_call`/`tool_result`, `error` (S-X-7:6, S-DROID-8:6), and the synthetic `{"type":"result","final_text":…}` shape.
- `vendor_exit_meaning`: always `None`. C2 shows no model/auth exit code; the rc 1 permission `error` events in S-X-7/S-DROID-8 stay observations.
- Robustness: read with `encoding="utf-8", errors="replace"`. Blank lines are skipped and not counted. Non-JSON lines are counted in `malformed`; `RecursionError` from deeply nested input is counted there too, so no input can raise. Non-object JSON and unknown types are ignored. `OSError` (missing file, a directory, unreadable) gives an all-`None` result with `malformed == 0`.

## Assertions changed (supersessions)
None. D6 allowed changing a `reasoning_effort` `smoke` pin in `tests/unit/test_adapters.py` if one existed. On base, `grep -rn reasoning_effort tests/` and a grep for `verification…"smoke"` found no pin of the droid/kiro class value. The only `smoke` assertion on `reasoning_effort` (tests/unit/test_adapter_evidence.py:264, `ShowRowsTest`) checks the evidence layer raising it from a passing S-X-5, and it stays green unchanged (Req 5.2). No assertion was weakened or deleted.

## Test evidence
New tests in `tests/unit/test_adapters_droid_real.py`. Every docstring has `ACC:`; every run dir is `tempfile.mkdtemp(prefix="mh-droidparse-", dir="/tmp")` with a `/tmp/mh-` assertion and `addCleanup`. `real_fixture()` checks each real file's sha256 against its sidecar before use.

Fails-on-old evidence: the test file was written first and run against the unchanged base code with `python3 -m unittest tests.unit.test_adapters_droid_real`: 7 tests, failures=8, errors=5 (sub-tests counted separately).

| Test | ACC | On old code | On new code |
|---|---|---|---|
| `test_droid_parse_real_sx1_final_text_session_usage` | N24, N6 | FAIL `'' != 'MESH_SMOKE_OK'` (S-X-1, S-X-5); ERROR `usage` is None | OK |
| `test_droid_parse_real_sx3_resumed_token` | N24 | FAIL token not in `''` | OK |
| `test_droid_parse_real_streams_without_completion` | N24, N6 | ERROR no `_scan_stream` (S-X-6, S-X-7, S-DROID-8) | OK |
| `test_droid_parse_init_fixture_1_3_0` | N24 | OK (regression guard, expected) | OK |
| `test_droid_parse_synthetic_robustness` ("robustness only, never evidence") | N24, N12 | FAIL `'' != 'SYNTHETIC_FINAL'`; ERROR no `_scan_stream`; FAIL session id `None` for the `\xff\xfe` run dir (old strict UTF-8 read lost it) | OK |
| `test_reasoning_effort_class_value_is_help` | N12 | FAIL `'smoke'` for droid and kiro | OK |
| `test_reevaluate_committed_droid_results_offline` | N12, N24 | FAIL S-X-1 `('fail', 'parse_pending')` | OK: S-X-1 `pass` with `evaluated_from`; S-X-2/4/5 `pass`; S-X-3/6/7/S-DROID-8 carried unchanged |

The re-evaluation test calls only `reevaluate_doc` (never `reevaluate()`/`main()`), with `subprocess.Popen` patched to raise. It pins `tempfile.tempdir` to `/tmp` and checks that every temp dir `reevaluate_doc` makes starts with `/tmp/mh-`, and that the committed results file is unchanged afterwards.

Commands (all exit 0 after the change):
- `python3 -m unittest tests.unit.test_adapters_droid_real tests.unit.test_adapters`: 38 tests OK.
- `python3 -m unittest tests.unit.test_smoke_cases tests.unit.test_smoke_harness tests.unit.test_adapter_evidence tests.unit.test_fixture_hashes tests.unit.test_tierlib`: 48 tests OK. The stub `parse_pending` assertions (test_smoke_cases.py:154, :194) and `test_fixture_hashes` are green.
- `bash scripts/test.sh`, before and after:

| | rc | unit | integration | recovery | wall time | tripwires |
|---|---|---|---|---|---|---|
| before (base 3bb4a6b) | 0 | 357 OK | 273 OK | 49 OK | 923 s | none present after the run |
| after | 0 | 364 OK (+7) | 273 OK | 49 OK | 532 s | none present after the run |

The 923 s baseline exceeds the 600 s A1 limit. It was not an idle measurement: the integration tier alone took 839 s, against 454 s in the after run with no code difference in that tier. Other suites were likely running on the box (the run prompt warned about this). `/proc/loadavg` reads all zeros in this container, so load could not be confirmed. The after run is under 600 s, and A1 should be re-measured on an idle box by the coordinator as usual.

## Facts found in code
- `supervisor.verified_resume_session` (mesh/daemon/supervisor.py:279–284) checks `verification == "smoke"`, but only for `resume`, so D6 does not change it.
- `reevaluate_doc` (tests/smoke/reevaluate.py:189) calls `tempfile.mkdtemp(prefix="mh-reeval-")` without `dir=`, so its temp dir lands under `/tmp` only when `TMPDIR` is unset. The r6 test pins `tempfile.tempdir`. C3 runs `reevaluate` directly, so with a non-default `TMPDIR` the staging temp dir would sit outside `/tmp/mh-*`. This is outside r6 FILES, so it is only noted here.
- `tests/fixtures/real/droid/1.3.0/exec-init-event-opus55-high.json` has no `.meta.json` sidecar. For it, `real_fixture()` checks the sha256 against `FIXTURE_HASHES` in tests/unit/test_fixture_hashes.py (imported read-only).

## Deviations and STOPs
- Snapshot recorded by Kiro at the end of its session (2026-11-10 ~03:35 CST), before the coordinator committed the work as ede5999, ccba298 and ab1b3d7: no commits (see the top of this file). Otherwise none: no file outside FILES was edited, no STOP condition was hit, no vendor process was started, no probe or smoke tier was run, and `os.environ` was never given `MESH_SMOKE*`/`MESH_PROBE*`.
- Not run: the done-criteria check `git branch --list 'mesh/*'`. This session's shell allow-list rejected it, both with and without the pattern. No git command that creates branches was run, and `git log -1` was still the base `3bb4a6b` at that snapshot. That criterion is left unticked in tasks.md for the coordinator.

## Permission model (added by the coordinator after the Linear review, docs/coordination/reviews/linear-m3-r6.md)
- The `.kiro/agents/mesh-coder-r6.json` permissions (write only the r6 FILES, offline, no git writes) are a **soft guard**, not a sandbox. The agent can write tests and also run them (`python3 -m unittest`, `bash scripts/test.sh`), so code it writes runs with the same user, network and filesystem access as the session. Real sandboxing is out of scope for M3-lite.
- The real boundaries, in order: the coordinator reads the diff and makes every commit; the Linear review of record on the PR returns ACCEPT; `scripts/coord/merge.sh` merges into main; the full suite on main is green; only then `sync-github.sh` publishes it.
- The `fs_write` grant for `mesh/adapters/kiro.py` is file-wide in the profile, but this run may change line 43 only (D6). `git diff ccbfa24..HEAD -- mesh/adapters/kiro.py` is exactly that one line.
- No-network check (coordinator, 2026-11-10): `env -i PATH=/usr/bin:/bin HOME=/tmp LANG=C.UTF-8 unshare -rn python3 -m unittest tests.unit.test_adapters_droid_real tests.unit.test_adapters` (new user and network namespace with no interfaces up, environment cleared of proxy and credential variables): 38 tests OK, rc 0. The r6 tests need no network and no credentials.
