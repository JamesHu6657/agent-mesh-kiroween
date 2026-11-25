Spec for m3-r6, written before implementation (2026-11-10), from m3-plan.md run m3-r6.

# Requirements Document: m3-r6 (m3-droidparse), real droid `parse()`

Sources (authoritative; this spec only restates them): docs/coordination/plans/m3-plan.md ("Run m3-r6 (m3-droidparse)", decisions G and D6, risk 2 "Output-schema surprises", the FILES row for m3-r6 in the parallel map, the common block for every M3 run); docs/acceptance-checklist.md N6, N12, N15, N16; docs/coordination/m3-acceptance-addendum.md N21, N24; the C2 capture under tests/fixtures/real/droid/1.6.0/ and tests/smoke/results/droid-1.6.0-2026-11-09{,-2}.json; docs/coordination/log.md 2026-11-09 23:00 (C2 done); mesh/adapters/{droid,base,evidence,kiro}.py; tests/smoke/reevaluate.py, tests/tierlib.py, tests/unit/test_smoke_cases.py, tests/unit/test_adapters.py.

`[ACC: …]` names rows of the checklist or the addendum. "The parser" means `DroidAdapter.parse()` in mesh/adapters/droid.py plus its private helpers.

## Introduction
Decision G makes droid 1.6.0 the M3-lite gate adapter and assigns "m3-r6 = real droid `parse()`". Today `parse()` (droid.py:85–112) returns only the session id of the first `system/init` event and `final_text=""`. Because of that, C2 recorded S-X-1 as `fail(parse_pending)` with `raw_token_seen: true` (results file droid-1.6.0-2026-11-09-2.json; log 2026-11-09 23:00: "expected until m3-r6"). S-X-1 is a launch case (`LAUNCH_CASES = ("S-X-1", "S-X-6")`, Q2), so droid stays launch-disabled until this run lands and C3 re-evaluates the committed results offline.

Decision D6 demotes the class-declared `reasoning_effort` verification from `smoke` to `help` in droid.py:43 and kiro.py:43, because one fixture is not a smoke result (m3-plan Part 2).

Risk 2 sets the rules: write `parse()` only from real C2 fixtures, ignore unknown events, add a synthetic robustness test, and if no final text can be derived, keep S-X-1 `fail` and droid launch-disabled.

## Requirements

### Requirement 1: Final text from the real `completion` event
**User Story:** As a lead agent, I want a droid run's deliverable to come from the CLI's own final answer, so that I review what the model said instead of a raw event dump.

#### Acceptance Criteria
1. WHEN `stdout.log` in the run dir contains a JSON object with `"type":"completion"` and a string `finalText`, THE parser SHALL return that string unchanged as `RunResult.final_text`. If there are several such events, the last one wins. [ACC: N24, N6]
2. WHEN the real S-X-1 fixture `tests/fixtures/real/droid/1.6.0/smoke-S-X-1-stdout.jsonl` is parsed THE parser SHALL return `final_text == "MESH_SMOKE_OK"`. WHEN the real S-X-3 fixture is parsed, `final_text` SHALL contain `MESH_SMOKE_OK`. [ACC: N24]
3. THE parser SHALL NOT take `final_text` from `message` events (any role), from `tool_call`/`tool_result`/`error` events, or from the `{"type":"result","final_text":…}` shape that appears only in synthetic m3-r2 data and in the test stubs. [ACC: N24, N12]
4. IF the stream has no `completion` event (cut short by SIGTERM as in S-X-6, or ended by an `error` event as in S-X-7 and S-DROID-8), or its `finalText` is not a string, THEN THE parser SHALL return `final_text == ""`. `supervisor.parse_run_output` then falls back to the raw `stdout.log` text, as it does today (mesh/daemon/supervisor.py:616–630). [ACC: N24]

### Requirement 2: Session id, unchanged rule
**User Story:** As the supervisor, I want the session id taken exactly as before, so that resume and `tasks.last_session_id` keep working.

#### Acceptance Criteria
1. THE parser SHALL return as `session_id` the `session_id` of the first `{"type":"system","subtype":"init"}` event in `stdout.log`, as now (m3-plan r6 sub-step 1). It SHALL NOT fall back to `session_id` fields of other events. [ACC: N24]
2. WHEN `stdout.log` yields no session id THE parser SHALL keep the existing fallback: read `exec-init-event-opus55-high.json` from the same run dir, for the session id only. [ACC: N24]
3. WHEN the real S-X-1 fixture is parsed `session_id` SHALL equal its init event's `9e176d53-937b-458d-997f-71b470bb2b9c`. WHEN `tests/fixtures/real/droid/1.3.0/exec-init-event-opus55-high.json` is parsed it SHALL still return `d72c2698-16db-41f0-aa24-9b3090171deb`. [ACC: N24]

### Requirement 3: Usage and exit meaning
**User Story:** As the coordinator, I want the per-run Factory usage reported by droid kept with the parse result, so that cost is visible without parsing logs by hand.

#### Acceptance Criteria
1. WHEN the chosen `completion` event (Requirement 1.1) has a `usage` object THE parser SHALL return a shallow copy of it as `RunResult.usage`, keys unchanged (including `factory_credits`). Otherwise `usage` SHALL be `None`. [ACC: N24]
2. THE parser SHALL leave `RunResult.vendor_exit_meaning` as `None`. The plan maps an exit-code meaning only "for model/auth errors if C2 shows one"; C2 shows none. The permission `error` events in S-X-7 and S-DROID-8 (rc 1) remain observations only. [ACC: N24]

### Requirement 4: Robustness without evidence
**User Story:** As the supervisor and the C3 re-evaluation, I want `parse()` never to raise on vendor output, so that one bad line cannot block closing a run or re-evaluating results.

#### Acceptance Criteria
1. THE parser SHALL ignore events whose `type` it does not use, and JSON lines that are not objects (arrays, strings, numbers, `null`). [ACC: N24]
2. WHEN a non-empty line is not valid JSON (including a truncated last line) THE parser SHALL skip it and count it in the private scan result's `malformed` counter. Blank lines are neither parsed nor counted. [ACC: N24]
3. THE parser SHALL NOT raise for any `stdout.log` content. A missing or unreadable file gives an empty `RunResult`. Undecodable bytes SHALL be replaced on read, so a single bad byte does not discard the session id. [ACC: N24]
4. THE synthetic robustness fixtures under `tests/fixtures/synthetic/droid/` SHALL be named `synthetic_*`, SHALL say "SYNTHETIC" in their init event, SHALL NOT contain `MESH_SMOKE_OK`, and SHALL NOT change any capability's `verification` when parsed. Their tests SHALL say "robustness only, never evidence". [ACC: N12, N24]

### Requirement 5: Decision D6, class-declared `reasoning_effort` demoted
**User Story:** As the user, I want a capability to read `smoke` only when a smoke result proves it, so that status words stay evidence.

#### Acceptance Criteria
1. THE `DroidAdapter` and `KiroAdapter` SHALL declare `reasoning_effort` with `supported="yes"` and `verification="help"`, with the note text unchanged (droid.py:43, kiro.py:43). [ACC: N12]
2. WHEN committed results at the probed version contain a passing S-X-5 THE evidence layer SHALL still report `reasoning_effort` as `smoke` through `evidence.apply_to_probe`. r6 does not change mesh/adapters/evidence.py. [ACC: N12]
3. IF an existing assertion pins the class value `smoke`, THEN it SHALL be changed only as the named supersession from m3-plan common block rule 2, and only in tests/unit/test_adapters.py. On the spec base, `grep` finds no such pin. [ACC: N12]

### Requirement 6: Unchanged surfaces
**User Story:** As a reviewer, I want r6 to touch only the parser and D6, so that the golden argv and fixture evidence stay trustworthy.

#### Acceptance Criteria
1. THE `DroidAdapter.build()` and every golden argv test SHALL stay unchanged and green (`test_adapters_build_golden_matrix`, `test_prompt_fallback`). [ACC: N6, N16]
2. THE fixture hash test `test_fixture_hashes` and its expected values SHALL stay unchanged and passing. If it fails, the implementer stops and reports. [ACC: N15]
3. THE run SHALL add, edit or delete nothing under `tests/fixtures/real/` or `tests/smoke/results/`. [ACC: N21]

### Requirement 7: C3 readiness, offline
**User Story:** As the coordinator, I want proof before C3 that the new parser flips S-X-1 on the committed capture, so that C3 is a formality.

#### Acceptance Criteria
1. WHEN `tests.smoke.reevaluate.reevaluate_doc` runs on the committed `tests/smoke/results/droid-1.6.0-2026-11-09-2.json` with fixture root `tests/fixtures/real`, THE result SHALL have S-X-1 `pass` with `evaluated_from` set. S-X-2, S-X-4 and S-X-5 SHALL be `pass`, and S-X-3, S-X-6, S-X-7 and S-DROID-8 SHALL be carried unchanged. No process SHALL start. [ACC: N12, N24]

### Requirement 8: Offline discipline
#### Acceptance Criteria
1. THE default suite SHALL start no vendor process (no `droid`, `kiro-cli`, `agy`, `vibe`), SHALL NOT set `MESH_SMOKE*`/`MESH_PROBE*` in `os.environ`, and SHALL write temp files only under `/tmp/mh-*`. [ACC: A3]
2. WHEN the run is done `bash scripts/test.sh` SHALL exit 0 with both tripwire files empty and an idle wall time under 600 s, reported before and after. [ACC: A1, A3]
