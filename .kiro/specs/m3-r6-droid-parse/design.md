Spec for m3-r6, written before implementation (2026-11-10), from m3-plan.md run m3-r6.

# Design Document: m3-r6, real droid `parse()`

The design is the approved m3-plan run m3-r6 (sub-steps 1–4, decision D6, risk 2). This page adds only what the plan left to the fixtures: the exact droid 1.6.0 stream-json shapes and the parse rule for each. Requirement numbers refer to requirements.md in this folder.

## 1. Scope
- `mesh/adapters/droid.py`: change `parse()` and add private helpers only. Line 43 becomes `verification="help"`. `build()`, `profile_flags()`, `probe()` and `list_models()` stay unchanged. No import changes are needed, because `json` and `os` are already imported.
- `mesh/adapters/kiro.py`: line 43 only (`verification="help"`).
- New `tests/unit/test_adapters_droid_real.py` (planned) and new `tests/fixtures/synthetic/droid/synthetic_*.jsonl`.
- No change to `mesh/adapters/base.py`. `RunResult` (base.py:313–318) already has `final_text`, `session_id`, `usage` and `vendor_exit_meaning`.

## 2. droid 1.6.0 stream-json events in the C2 fixtures
Fixtures: `tests/fixtures/real/droid/1.6.0/smoke-<case>-stdout.jsonl`. Each has one JSON object per line, written by `droid exec --output-format stream-json` (sidecar `command`). All stderr fixtures are empty (0 bytes).

| `type` (+ discriminator) | Fields seen | Where (file:line) | Parser use |
|---|---|---|---|
| `system`, `subtype:"init"` | `cwd`, `session_id`, `tools[]`, `model`, `reasoning_effort` | every file :1; 1.3.0 `exec-init-event-opus55-high.json`:1 (no `tools`) | `session_id` (first init only) |
| `message`, `role:"user"` | `id`, `text`, `timestamp`, `session_id` | S-X-1:2, S-X-3:2, S-X-5:2, S-X-6:2, S-X-7:2, S-DROID-8:2 | ignored |
| `message`, `role:"assistant"` | `id`, `text`, `timestamp`, `session_id` | S-X-1:3, S-X-3:3, S-X-5:3, S-X-7:5, S-DROID-8:5 | ignored (Req 1.3) |
| `tool_call` | `id`, `messageId`, `toolId`, `toolName`, `parameters{summary,command,riskLevel}`, `timestamp`, `session_id` | S-X-6:3, S-X-7:3, S-DROID-8:3 | ignored |
| `tool_result` | `id`, `messageId`, `toolId`, `isError`, `value` (ok) or `error{type,message}` (error), `timestamp`, `session_id` | S-X-6:4 (`isError:false`, 12 KB value), S-X-7:4, S-DROID-8:4 (`isError:true`) | ignored |
| `completion` | `finalText`, `numTurns`, `durationMs`, `session_id`, `timestamp`, `usage{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, factory_credits, thinking_tokens, ttft_ms}` | S-X-1:4, S-X-3:4, S-X-5:4 (always the last line) | `final_text` ← `finalText`; `usage` ← `usage` |
| `error`, `source:"cli"` | `message`, `timestamp`, `session_id` | S-X-7:6, S-DROID-8:6 (always the last line; rc 1) | ignored |

There is no separate cost field. Cost appears only as `usage.factory_credits` (S-X-1: 13621; S-X-3: 17120; S-X-5: 1029). These match the credits in the C2 log entry (R1 13,621).

## 3. Parse rule
```
parse(run_dir):
  scan = _scan_stream(run_dir/"stdout.log")            # dict, see below; {} if absent/unreadable
  session_id = scan.session_id
  if not session_id:                                   # existing fallback, kept
      session_id = _scan_stream(run_dir/"exec-init-event-opus55-high.json").session_id
  return RunResult(final_text=scan.final_text or "", session_id=session_id,
                   usage=scan.usage, vendor_exit_meaning=None)

_scan_stream(path) -> {"session_id", "final_text", "usage", "malformed"}:
  open(path, encoding="utf-8", errors="replace"); OSError -> all None, malformed 0
  for each line: strip; skip blank
      try json.loads except ValueError: malformed += 1; continue
      not a dict -> continue
      type "system" + subtype "init" and session_id not yet set -> session_id = ev.session_id if non-empty str
      type "completion" -> final_text = finalText if str else None
                           usage = dict(ev.usage) if dict else None      # last completion wins
      anything else -> continue
```
- `final_text` and `usage` come from the same (last) `completion` event, so a later completion without `usage` resets `usage` to `None`.
- The fallback scan reads only `session_id` from the 1.3.0 file name, as the current loop does. The current dead `os.path.isabs` branch (droid.py:91–93) may be dropped.
- `_scan_stream` returns a plain dict, so no new import is needed. Tests read `malformed` from it directly.

## 4. Behaviour by input (all assertions in tests/unit/test_adapters_droid_real.py (planned))
| Input (copied into a `/tmp/mh-*` run dir as `stdout.log`) | `final_text` | `session_id` | `usage` | `malformed` |
|---|---|---|---|---|
| real S-X-1 | `MESH_SMOKE_OK` | `9e176d53-937b-458d-997f-71b470bb2b9c` | S-X-1:4 `usage` dict | 0 |
| real S-X-3 (resumed R1 session) | ``I replied with `MESH_SMOKE_OK`.`` | `9e176d53-…` (same session) | S-X-3:4 `usage` | 0 |
| real S-X-5 | `MESH_SMOKE_OK` | `c09dcfd8-ee77-4d2d-bdb9-e97fd15d3dd4` | dict | 0 |
| real S-X-6 (SIGTERM, rc 143, no completion) | `""` | `6dbacd6c-a492-4be7-a65c-700ebc7799cf` | `None` | 0 |
| real S-X-7 (`error`, rc 1) | `""` | `d71ac66d-f418-479d-a2ee-a26fc67e5097` | `None` | 0 |
| real S-DROID-8 (`error`, rc 1) | `""` | `ce360ce6-07f0-43fd-a3bf-6f5ce0cf1079` | `None` | 0 |
| 1.3.0 init event, as `stdout.log` and under its own name | `""` | `d72c2698-16db-41f0-aa24-9b3090171deb` | `None` | 0 |
| `synthetic_truncated.jsonl` | `""` | `synthetic-droid-0001` | `None` | 1 |
| `synthetic_unknown_event.jsonl` | `SYNTHETIC_FINAL` | `synthetic-droid-0002` | `None` | 1 |
| no `stdout.log` | `""` | `None` | `None` | – |

## 5. Edge cases (requirements 1.4, 3.2 and 4)
- **Unknown events:** any `type` other than `system`/`completion` is skipped, as are non-object JSON lines. New droid event types therefore cannot break parsing.
- **Truncated stream:** a cut-off last line fails `json.loads` and is counted in `malformed`. Without a complete `completion`, `final_text` is `""`. S-X-6 is the real case, a stream stopped by SIGTERM before any completion.
- **Non-zero exit:** `parse()` never sees the exit code. Run status, T6 reasons and rc handling stay in the supervisor, which r6 does not touch. A `completion` in a stream that exited non-zero is still returned. `vendor_exit_meaning` stays `None` (Req 3.2).
- **Missing final text:** `final_text == ""`, so `parse_run_output` (supervisor.py:616–630) uses the raw `stdout.log` text, which also carries any `error.message`. `eval_sx1` (tests/smoke/reevaluate.py:76–86) then reports `fail(parse_pending)`, so launch stays off (risk 2).
- **Exceptions:** none escape `parse()`. The supervisor swallows them anyway (supervisor.py:619–624 and :1500–1504), but `reevaluate_doc` (reevaluate.py:208) does not, so a raise would abort C3.

## 6. Interactions that must not change
- **The stub tests pin `parse_pending`.** The stub droid in tests/unit/test_smoke_cases.py emits `message` (assistant) and `result`/`final_text` lines but no `completion`. The test file asserts S-X-1 `fail(parse_pending)` at capture (tests/unit/test_smoke_cases.py:154) and after re-evaluation (:194). That file is outside r6's FILES. The completion-only rule keeps both assertions true. Reading `message` or `result` would break them, which would be a STOP.
- **tests/unit/test_smoke_harness.py:196** checks only `session_id == "synthetic-session-0001"` on `synthetic_droid_stream.jsonl` (init first). That still holds.
- **Evidence:** the committed results still say S-X-1 `fail`. `effective_launch` therefore still returns `launch_cases_failed` for droid 1.6.0 until C3 adds a re-evaluated results file. r6 changes no evidence.
- **Lead turns:** `lead_turn_final_text` (lead_turns.py:853–857) would now see droid text, but vendor lead turns are code-gated off in M3-lite (D4, m3-r4).
- **Session id at close:** `_close_run` (supervisor.py:1500–1504) uses `parse().session_id`. The rule is unchanged.

## 7. How C3 re-evaluates (tests/smoke/reevaluate.py, tests/tierlib.py)
`reevaluate_doc` validates the doc, then handles each output-derived case (S-X-1, S-X-2, S-X-4, S-X-5): it checks the fixture's sidecar sha256 (`tierlib.read_sidecar`), copies the fixture as `stdout.log` into a `mh-reeval-*` temp dir, calls `adapter.parse()` and re-applies `eval_sx1`/`eval_sx2`/`eval_sx4`/`eval_sx5` with the recorded rc, setting `evaluated_from`. S-X-3, S-X-6, S-X-7 and S-DROID-8 are copied unchanged with `carried_from`. With the r6 parser, S-X-1 on the committed doc becomes `pass` (rc 0, token in `finalText`). The r6 test proves this in memory through `reevaluate_doc` only. It never calls `reevaluate()`/`main()`, which write files, and it never promotes. C3 itself stays a coordinator step.

## 8. Decision D6
`Capability("reasoning_effort", supported="yes", verification="help", note=<unchanged>)` in droid.py:43 and kiro.py:43. `probe()` copies the class value and turns it `stale` on drift (droid.py:242–246). `evidence.apply_to_probe` raises it to `smoke` only from a passing S-X-5 at the probed version, so an honest `smoke` is still reachable after C3. At base no test pins the class value (`grep` for `reasoning_effort` in tests/ finds only evidence and schema tests), so tests/unit/test_adapters.py is expected to stay untouched.

## 9. Synthetic fixtures (exact content; robustness only, never evidence)
`tests/fixtures/synthetic/droid/synthetic_truncated.jsonl` (3 lines, no trailing newline):
```
{"type":"system","subtype":"init","session_id":"synthetic-droid-0001","model":"synthetic-model","note":"SYNTHETIC: hand-written for m3-r6 parse robustness tests; not captured from droid"}
{"type":"message","role":"assistant","text":"SYNTHETIC_PARTIAL"}
{"type":"completion","finalText":"SYNTHETIC_FI
```
`tests/fixtures/synthetic/droid/synthetic_unknown_event.jsonl` (8 lines):
```
{"type":"system","subtype":"init","session_id":"synthetic-droid-0002","model":"synthetic-model","note":"SYNTHETIC: hand-written for m3-r6 parse robustness tests; not captured from droid"}
{"type":"future_event","payload":{"x":1},"session_id":"synthetic-droid-0002"}
[]
null
"plain string"
{not json
{"type":"message","role":"assistant","text":"SYNTHETIC_ASSISTANT_TEXT"}
{"type":"completion","finalText":"SYNTHETIC_FINAL","usage":"not-a-dict","session_id":"synthetic-droid-0002"}
```
The test also writes a `/tmp/mh-*` run dir whose `stdout.log` starts with the bytes `b"\xff\xfe\n"` followed by the S-X-1 init line. Expected: the session id is kept and no exception is raised.
