# Agent Mesh v1 — Adapter Status

Status: updated for gate M3-lite and kept current through M4. The adapter facts come from
the code (`mesh/adapters/`, `mesh/adapters/evidence.py`), live
`bin/mesh adapter show <id>` against a non-testing daemon (first captured in run m3-r7b),
and the committed results under
`tests/smoke/results/` and fixtures under `tests/fixtures/real/`. It describes only what
the code really does. Where an answer is not verified, this page says so.

Prompt position (kiro, agy): the prompt is the last positional argv item. A prompt that
starts with "-" gets one leading newline so the CLI cannot read it as an option
(m4-p3-cleanup); Mesh's own prompts never start with "-".

## Status words

Mesh uses exactly four status words for adapters, and they describe **evidence, not
usability** (plan §14.1):

- `verified` — every capability Mesh uses was smoke-verified on the recorded version.
- `partially_verified` — installed and help-verified, but at least one capability is not
  smoke-verified.
- `not_installed` — the binary was not found on `PATH`.
- `unsupported` — Mesh cannot support the adapter as configured.

These four are the adapter statuses the evidence rules use. A probe that fails before
it can describe the CLI records one of two more words. For the four implemented
adapters (`droid.py`, `agy.py`, `kiro.py`, `vibe.py` probe paths): `error` when
`--version` or `--help` exits non-zero or times out (`details.error` is
`version_check_failed`, `timeout` or `help_check_failed`), and `unverified` when
`--version` succeeds but prints no version Mesh can parse (`details.error`
`unparseable_version`). The hermes and openclaw stubs (M4) differ: `error` only for a
failed `--version` (`version_check_failed`), `unverified` with no `details.error` when no
version parses; `--help` still runs, but a failed `--help` does not change the status
(it only leaves `help_sha256` empty). Neither is a success status: the
launch-evidence rule reads only `verified` / `partially_verified` probe rows
(`mesh/adapters/evidence.py` `latest_probe`), so such a row never opens launch.

Per capability, `adapter show` prints `verification` (`smoke`, `help`, `none`, `stale`,
or `unverified`; `unverified` is outside the addendum N20 set and is what vibe 3.1.0
prints for `model_selection`),
`verified_at`, `verified_version` and `results_file`. A capability's verification becomes
`stale` when the probed version differs from the version recorded at verification. The
status is `verified` only if every `supported: "yes"` capability is `smoke`.
`launch_enabled`, `launch_milestone` and `launch_reason` are printed next to the status
and are independent of it: launch is decided by the evidence rule below, not by the
status word.

## Status of the six vendor CLIs

| CLI (adapter id) | Installed | Version (live probe) | Status | Launch enabled | Launch reason | Results file | Launch milestone |
|---|---|---|---|---|---|---|---|
| Factory droid (`droid`) | yes | 1.6.0 | verified | true | — | `tests/smoke/results/droid-1.6.0-2026-11-09-3.json` | — |
| Antigravity (`agy`) | yes | 1.5.4 | partially_verified | false | no_smoke_evidence | not run | M3 |
| Kiro CLI (`kiro`) | yes | 3.1.0 | partially_verified | true | — | `tests/smoke/results/kiro-3.1.0-2026-11-10-2.json` | — |
| Mistral Vibe (`vibe`) | yes | 3.1.0 | partially_verified | false | no_smoke_evidence | not run | M3 |
| Hermes (`hermes`) | no | — | not_installed | false | no_smoke_evidence | not run | M4 |
| OpenClaw (`openclaw`) | no | — | not_installed | false | no_smoke_evidence | not run | M4 |

`—` means `adapter show` printed `null`: an adapter with launch enabled has no launch
milestone and no launch reason.

### Evidence-driven launch (N19)

Effective `launch_enabled` is true only if the latest successful probe row (status
`verified` or `partially_verified`) for the agent's binary
has version V, the latest results file for the adapter has version V, the results file
validates, and S-X-1 (headless run) and S-X-6 (SIGTERM) pass. Otherwise assigning launch
work to an agent on that adapter (`task create --assignee`, `task assign`, `task reassign`,
`task retry`, or `task reject` back to rework) returns rc 13 `ADAPTER_UNSUPPORTED` with
`details.milestone: "M3"` and `details.reason` one of `no_probe_version`,
`no_smoke_evidence`, `evidence_stale`, `evidence_invalid`, `launch_cases_failed`, or
`launch_held` (passing evidence, but the adapter is held back by `evidence.LAUNCH_HOLDS`;
then `details.milestone` is the hold's milestone. No adapter is held today; kiro was held
during M3 until its N13 passed in M4, L11).
`agent create` does not evaluate launch evidence: it creates a launch agent on such an
adapter (rc 13 there only for `ADAPTER_NOT_INSTALLED` or `TEST_DOUBLE_DISABLED`), and the
refusal comes when work is assigned to it. Evidence is not re-checked when a queued task
starts (T5, L3), with one exception: for a task queued on a held adapter
(`evidence.LAUNCH_HOLDS`), e.g. one assigned before the hold, the scheduler fails it with
reason `launch_held` (T6, `failure.details.milestone` from the hold) and starts no run.
Evaluation starts no subprocess. Nobody sets `launch_enabled` by hand; the class
attribute stays `false`. meshd reads the evidence at runtime from the checkout's
`tests/smoke/results/` and hashes `tests/fixtures/real/` (accepted deviation D5; a local
v1 has no import command yet). A new vendor version therefore turns launch off
(`evidence_stale`) until the coordinator captures and promotes new results for it.

### Capability rows (live `adapter show`)

| Adapter | headless | stream_output | model_selection | reasoning_effort | resume |
|---|---|---|---|---|---|
| droid 1.6.0 | yes / smoke | yes / smoke | yes / smoke | yes / smoke | unknown / smoke |
| agy 1.5.4 | yes / stale | yes / stale | yes / stale | yes / stale | unknown / stale |
| kiro 3.1.0 | yes / smoke | yes / smoke | yes / help | yes / help | unknown / smoke |
| vibe 3.1.0 | yes / help | yes / help | yes / unverified | no / help | unknown / help |
| hermes | unknown / none | unknown / none | unknown / none | unknown / none | unknown / none |
| openclaw | unknown / none | unknown / none | unknown / none | unknown / none | unknown / none |

Each cell is `supported / verification`. droid `smoke` rows cite
`tests/smoke/results/droid-1.6.0-2026-11-09-3.json` (verified_version 1.6.0, verified_at
2026-11-09T14:55:28Z); kiro `smoke` rows cite
`tests/smoke/results/kiro-3.1.0-2026-11-10-2.json` (verified_version 3.1.0, verified_at
2026-11-10T01:59:30Z). The resume row is `smoke` because S-X-2 maps to resume, but its
`supported` value stays `unknown`, so Mesh does not resume vendor sessions (see "Deferred").
vibe's `model_selection` prints `unverified` (no read-only model-list command; the user
supplies allowed models).

## Factory droid (`droid`): the M3-lite gate adapter

- **Status `verified`, launch enabled** for droid 1.6.0. All four `supported: "yes"`
  capabilities are smoke-verified, so the evidence rule prints `verified`. The plan
  (Part 2) expected `partially_verified` because edit/dev profiles have no smoke case;
  the status rule does not grade permission profiles, so the live status is `verified`.
  It is still evidence about the readonly smoke cases, not a permission guarantee.
- **Smoke results** (`tests/smoke/results/droid-1.6.0-2026-11-09-3.json`, model
  `claude-sonnet-5-5`, profile `readonly`, consent James 2026-11-09 12:44):

  | Case | Result | Fixture |
  |---|---|---|
  | S-X-1 headless + stream | pass | `tests/fixtures/real/droid/1.6.0/smoke-S-X-1-stdout.jsonl` |
  | S-X-2 session id | pass | `tests/fixtures/real/droid/1.6.0/smoke-S-X-1-stdout.jsonl` |
  | S-X-3 resume | pass | `tests/fixtures/real/droid/1.6.0/smoke-S-X-3-stdout.jsonl` |
  | S-X-4 model selection | pass | `tests/fixtures/real/droid/1.6.0/smoke-S-X-1-stdout.jsonl` |
  | S-X-5 reasoning effort | pass | `tests/fixtures/real/droid/1.6.0/smoke-S-X-5-stdout.jsonl` |
  | S-X-6 SIGTERM | pass | `tests/fixtures/real/droid/1.6.0/smoke-S-X-6-stdout.jsonl` |
  | S-X-7 readonly write attempt | observed | `tests/fixtures/real/droid/1.6.0/smoke-S-X-7-stdout.jsonl` |
  | S-DROID-8 readonly shell attempt | observed | `tests/fixtures/real/droid/1.6.0/smoke-S-DROID-8-stdout.jsonl` |

  S-X-7 and S-DROID-8 are observations, not permission guarantees. The earlier files
  `droid-1.6.0-2026-11-09.json` and `-2.json` are kept as history (S-X-1 failed there);
  the latest file decides.
- **End to end.** N13 (readonly task through Mesh) passed:
  `tests/smoke/results/e2e-N13-droid-1.6.0-run_a1c225d817a3u3i4jeczj2hino.json`.
  N26 (code task) passed:
  `tests/smoke/results/e2e-N26-droid-1.6.0-run_a1c22d87f2h2ejkayt5nhhgojm.json` — droid
  `dev` (`--auto medium`) edited and committed in its task worktree, Mesh merged it into
  integration with a two-parent merge commit, no remote, nothing pushed.
- **Probe and flag diff (C1).** Help fixtures for 1.6.0 under
  `tests/fixtures/real/droid/1.6.0/`. Flag diff 1.3.0 → 1.6.0 for `help.txt` and
  `exec-help.txt`: no flags added, none removed. Every flag Mesh emits for droid
  (`--auto`, `--cwd`, `--file`, `--log-group-id`, `--model`, `--output-format`,
  `--reasoning-effort`, `--session-id`, `--skip-permissions-unsafe`) is in the 1.6.0 help.

## Antigravity (`agy`) and Mistral Vibe (`vibe`): probe only

- **agy** is `partially_verified`, launch off (`no_smoke_evidence`). The live probe sees
  agy **1.5.4**; committed fixtures exist for 1.5.2 and 1.5.3 only (C1 captured 1.5.3;
  flag diff 1.5.2 → 1.5.3: no flags added, none removed). With no capture at 1.5.4, all
  five capability rows print `stale`. A 1.5.4 probe capture is coordinator work. Real
  parse fixtures (`stream-*` under `tests/fixtures/real/agy/1.5.3/`) back `parse()` only;
  they are not smoke results.
- **vibe** 3.1.0 is `partially_verified`, launch off (`no_smoke_evidence`); its rows
  are help-verified except `model_selection`, which prints `yes` / `unverified`. A smoke harness exists (`tests/smoke/test_smoke.py`) but no vibe results
  file has been promoted.

## Hermes (`hermes`) and OpenClaw (`openclaw`)

Not installed. Their adapters report all capabilities `unknown` / `none`, no flags, and
`launch_milestone: "M4"`. `agent create --adapter hermes` or `--adapter openclaw`
returns rc 13 `ADAPTER_NOT_INSTALLED`; `adapter probe` re-checks `PATH` and keeps
`not_installed`.

## What works today

- **Vendor launch works for droid 1.6.0 and kiro-cli 3.1.0** because their evidence passes
  the rule above, and both passed the real N13 end-to-end run. agy and vibe launch tasks return rc 13 `ADAPTER_UNSUPPORTED`
  (`no_smoke_evidence`), or `PROFILE_UNSUPPORTED` first if the profile is unsupported by
  that adapter. hermes/openclaw agents cannot be created (`ADAPTER_NOT_INSTALLED`).
- **Vendor launch leads are refused in M3-lite** (decision D4), whatever the evidence:
  a vendor lead turn never starts, its messages stay pending, and meshd logs
  `lead.turn_refused` with reason `vendor_lead_m3_lite` once per assignment. Use a
  `delivery=inbox` lead for vendors.
- **Inbox participation works** with the `none` adapter: an already-running agent that
  can run shell commands calls `mesh inbox wait`/`list`, then `mesh task start`,
  `heartbeat`/`progress`, and `submit`/`fail` with its own agent token.
- The run wrapper, supervisor and crash recovery from M1c apply to vendor runs as to the
  `fake` test double. Mesh does not sandbox external CLIs.
- `adapter list` in a daemon without `MESH_TESTING` lists droid, agy, kiro, vibe, hermes,
  openclaw, and none. `fake` is absent.

## Deferred (M3-lite scope, plan Part 2)

- **kiro, agy and vibe smoke.** agy and vibe have no smoke results. kiro has only the
  launch cases (S-X-1, S-X-2, S-X-4 observed, S-X-6); its S-X-3, S-X-5, S-X-7 and
  S-KIRO-8 were not run.
- **kiro and agy `dev`** stay `PROFILE_UNSUPPORTED`.
- **agy `prompt_mode`** validation.
- **Idle timeout** stays off (`0`); it is not validated against vendor output.
- **Vendor lead turns** are code-gated off (D4).
- **Resume.** Mesh resumes only when the resume capability is `supported: "yes"` and
  `smoke` (`supervisor.py:verified_resume_session`). droid's S-X-3 passed, but every
  vendor's resume row still says `supported: "unknown"`, so no vendor run resumes today;
  each attempt is a fresh run with the full prompt. kiro's S-X-3 was not run.
- **hermes and openclaw** adapters (M4).
- **N11** (smoke tier) is a scoped pass on droid.

## Kiro (`kiro`): parse ready, launch open by smoke evidence, N13 passed (M4)

- **Parse.** `KiroAdapter.parse()` reads kiro-cli 3.1.0 `--output-format stream-json`
  (`runStarted`, `metadata`, `sessionUpdate`, `runFinished`, `runError`):
  `final_text` = `runFinished.finalText`, else the `agent_message_chunk` texts (thought
  chunks never count); `session_id` = the first `sessionId`; `usage` = summed
  `meteringUsage` credits (`kiro_credits`), `turnDurationMs`, last context %, effort,
  `modelId` when sent, status / stopReason, and a `CONTENT_FILTERED` refusal category.
  `vendor_exit_meaning`: `auth_failed` (runError naming the bearer token / login),
  `run_error` (any other runError), `content_filtered` (refusal with no successful finish),
  and no `runFinished` with status success (truncated/killed/empty) → `run_error`; any
  `runError` counts whatever its fields. **Run outcome (all adapters):** plan §11.3/T7 is
  amended to "exit 0 and no vendor failure meaning → auto-submit". Any `vendor_exit_meaning`
  (errors, refusals, and budget/turn/time exhaustion such as vibe `turn_limit`/`limit`/
  `terminated`) is T8 `vendor_error`; the lead decides from the failure details. droid never
  sets one. Contract in `docs/impl-notes-m3-kiro-real.md`. Real fixtures: `tests/fixtures/real/kiro/3.1.0/stream-*.jsonl`,
  `stderr-tool-denied.txt` (sidecar sha256 each; provenance in
  `docs/impl-notes-m3-kiro-real.md`).
- **Stderr.** `[denied] tool permission approval is not supported in non-interactive mode`
  maps to `tool_denied` (with a count): an untrusted tool under `--no-interactive` is denied,
  not prompted, and the run continues.
- **Smoke.** `tests/smoke/test_smoke.py` `KiroSmoke` runs S-X-1..7 and S-KIRO-8 behind
  `MESH_SMOKE=1`, `MESH_SMOKE_ADAPTERS`/`MESH_SMOKE_CONSENT` containing `kiro`,
  `MESH_SMOKE_MODEL_KIRO` (name a cheap model) and `MESH_SMOKE_MAX_RUNS` (use 1 per stage;
  record `/usage` before and after). Profile readonly = `--trust-tools=`. S-X-4 is usually
  `observed(not_observable)`: 3.1.0 rarely streams `modelId`.
- **E2E.** `tests/smoke/test_e2e.py` `KiroE2E` with `MESH_SMOKE_E2E=kiro` runs the N13
  readonly task. The code demo (N26) is skipped for kiro: it needs `dev`.
- **End to end.** N13 passed in M4 (James consent 2026-11-12 21:04, `claude-haiku-5.5`, one run):
  task `submitted`, deliverable summary contains `MESH_SMOKE_OK`, `--model claude-haiku-5.5`
  in the recorded argv, profile readonly. Record:
  `tests/smoke/results/e2e-N13-kiro-3.1.0-run_a1cfe019a5dgqkf4tu7kwjs7sf.json`.
- **Launch.** Open for kiro-cli 3.1.0 by smoke evidence. During M3 it was held (L11, James
  sign-off 2026-11-12 19:32, reason `launch_held`) because N13 had not run; M4 removed the
  `LAUNCH_HOLDS` entry and then ran N13. `KiroE2E` skips while an adapter is held (the e2e
  daemon runs without `MESH_TESTING`, so the testing-only hold envs do not apply). The evidence (James consent 2026-11-10 09:46/09:49,
  model `claude-haiku-5.5`, MAX_RUNS=1 per stage): `tests/smoke/results/kiro-3.1.0-2026-11-10-2.json`
  has S-X-1, S-X-2 pass, S-X-4 observed(not_observable), S-X-6 pass; S-X-3, S-X-5, S-X-7 and
  S-KIRO-8 not run. Readonly and edit profiles only.
- **`dev` stays unsupported** (plan §14.2). kiro-cli 3.1.0's built-in docs name the shell
  tool `shell` ("also known as execute_bash (legacy alias)"); a candidate mapping
  `--trust-tools=fs_read,fs_write,execute_bash` is noted for a later plan change, not built.

## Unsupported profiles

A profile that an adapter cannot express with narrower flags is `unsupported` for that
adapter (rc 13 `PROFILE_UNSUPPORTED`). Mesh never substitutes a broader profile.
`unrestricted` is the only profile allowed to emit a vendor trust-all flag. In practice,
`agy` `dev`/`elevated`, `kiro` `dev`/`elevated`, and `vibe` `elevated` stay unsupported
until smoke evidence exists. This section is the plan's mapping; the golden tests are
authoritative for the flags actually built.

## Hermes and OpenClaw: inbox participation, not adapter support

Once Hermes or OpenClaw is installed, it can join Mesh immediately as an
`adapter=none, delivery=inbox` agent, provided it can run shell commands: it then uses
`mesh inbox wait`/`list`, `mesh task start`, `heartbeat`, and `submit`/`fail` with its
own token. That is **inbox participation, not adapter support**: it uses the `none`
adapter and Mesh starts no process for it.

Their adapter status stays `not_installed` until the M4 adapter work supplies a probe,
observed flags, `build`/`parse`, golden tests, and opt-in smoke evidence. Installing the
binary alone does not change the status.

## The test double is not vendor support

`fake` is a **test double**, not a vendor integration: `kind: "test_double"`,
`vendor: null`, note `not a vendor integration`. It is available only when meshd runs
with `MESH_TESTING=1`; otherwise `agent create --adapter fake` returns rc 13
`TEST_DOUBLE_DISABLED` and `adapter list` omits it. It is **never counted as vendor
support**, and its `--fake-profile` flags are recorded only — they grant no real
permissions.

`none` is the inbox-only adapter (`kind: "none"`); it cannot launch a process.
