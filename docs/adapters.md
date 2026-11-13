# Agent Mesh — Adapter behavior and evidence

Mesh can launch executors through droid, kiro-cli, agy and vibe, or accept an
already-running inbox agent through the `none` adapter. Hermes and OpenClaw have
stub adapters, not launch integrations. Mesh does not sandbox any of these agents.

## Evidence is not permission

An adapter's `status` describes recorded evidence, not whether a model is safe or
whether launching is currently allowed:

- `verified`: all capabilities marked `supported: "yes"` have matching smoke evidence.
- `partially_verified`: installed and help-verified, but not every supported capability
  has matching smoke evidence.
- `not_installed`: no binary was found on `PATH`.
- `unsupported`: the configured adapter cannot be supported.

A failed version/help probe reports `error`; an unparseable successful version probe
reports `unverified`. Stub probes differ: failed help leaves `help_sha256` empty but
does not change an otherwise successful version probe's status.

Per-capability output includes `supported`, `verification`, `verified_at`,
`verified_version` and `results_file`. Verification can be `smoke`, `help`, `none`,
`stale` or `unverified`. A different installed version makes previous verification
stale. Status does not grade permission profiles or guarantee read-only enforcement.

## Warn versus strict launch policy

`adapter.evidence_mode` defaults to `"warn"` outside testing mode. Missing, invalid or
stale smoke evidence produces an `evidence_warning` rather than blocking an installed,
implemented adapter. Testing daemons default to `"strict"`.

In strict mode, launch requires a successful latest probe (`verified` or
`partially_verified`) at version V, valid latest committed results also at V, and
passing headless/stream and SIGTERM cases (fixture ids `S-X-1` and `S-X-6`). Reasons
include `no_probe_version`, `no_smoke_evidence`, `evidence_stale`, `evidence_invalid`,
`launch_cases_failed` and `launch_held`. Stub adapters and explicit launch holds are
not waived in warn mode. No adapter has an explicit hold in this checkout.

`mesh adapter show "$ADAPTER" --token-file "$TOKEN_FILE"` reports the effective
`launch_enabled`, `launch_reason`, warnings and capability evidence. Internal fields
such as `launch_milestone` are diagnostic labels, not additional installation steps.
`agent create` does not evaluate launch evidence; assignment does.
The assignment paths are `task create --assignee`, `task assign`, `task reassign`, `task retry`
and `task reject` back to rework; strict-mode evidence refusal returns rc 13
`ADAPTER_UNSUPPORTED`.
Queued tasks are not generally re-evaluated, except that when a queued task’s adapter is newly held,
the scheduler fails it with reason `launch_held` without starting a run.

Results are read from `tests/smoke/results/`, with hashed fixtures under
`tests/fixtures/real/`. Package installs do not carry those files; strict checking
needs `MESH_EVIDENCE_ROOT` pointing at a checkout. Updating a vendor CLI can make
strict evidence stale until a coordinator captures and promotes matching results.

## Status of the six vendor CLIs

Recorded versions below are not a live installation inventory.

| CLI (adapter id) | Recorded installed | Recorded version | Status | Strict launch enabled | Strict launch reason | Results file | Warn executor launch |
|---|---|---|---|---|---|---|---|
| Factory droid (`droid`) | yes | 1.6.0 | verified | true | — | `tests/smoke/results/droid-1.6.0-2026-11-09-3.json` | Allowed when installed and profile supported |
| Antigravity (`agy`) | yes | 1.5.4 | partially_verified | false | no_smoke_evidence | not run | Allowed with evidence warning and supported profile |
| Kiro CLI (`kiro`) | yes | 3.1.0 | partially_verified | true | — | `tests/smoke/results/kiro-3.1.0-2026-11-10-2.json` | Allowed when installed and profile supported |
| Mistral Vibe (`vibe`) | yes | 3.1.0 | partially_verified | false | no_smoke_evidence | not run | Allowed with evidence warning and supported profile |
| Hermes (`hermes`) | no | — | not_installed | false | no_smoke_evidence | not run | Cannot launch: stub adapter |
| OpenClaw (`openclaw`) | no | — | not_installed | false | no_smoke_evidence | not run | Cannot launch: stub adapter |

Strict results require a matching successful local probe and supported profile.
The table reproduces the recorded evidence reason for stubs; implemented launch
support is separately required in both modes. `—` denotes a null reason.

Check your own `adapter show`/`probe` output. Missing binaries remain unavailable in
both modes. `adapter models` may contact vendor servers; it starts no inference but
can require the vendor's login. Vibe has no read-only model-list command, so its model
allowlist is supplied by the user.

### Capability rows (committed evidence)

| Adapter | headless | stream_output | model_selection | reasoning_effort | resume |
|---|---|---|---|---|---|
| droid 1.6.0 | yes / smoke | yes / smoke | yes / smoke | yes / smoke | unknown / smoke |
| agy 1.5.4 | yes / stale | yes / stale | yes / stale | yes / stale | unknown / stale |
| kiro 3.1.0 | yes / smoke | yes / smoke | yes / help | yes / help | unknown / smoke |
| vibe 3.1.0 | yes / help | yes / help | yes / unverified | no / help | unknown / help |
| hermes | unknown / none | unknown / none | unknown / none | unknown / none | unknown / none |
| openclaw | unknown / none | unknown / none | unknown / none | unknown / none | unknown / none |

Each cell is `supported / verification`; these are recorded versions, not a live inventory.

## Factory droid (`droid`)

The latest committed readonly smoke record is
`tests/smoke/results/droid-1.6.0-2026-11-09-3.json`. Headless/stream, session id,
resume observation, model selection, effort and SIGTERM passed. Readonly write/shell
attempts are observations, not permission guarantees. Earlier failed result files
are retained as history; the latest record decides launch.

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

Readonly and local code-task end-to-end records are respectively
`tests/smoke/results/e2e-N13-droid-1.6.0-run_a1c225d817a3u3i4jeczj2hino.json` and
`tests/smoke/results/e2e-N26-droid-1.6.0-run_a1c22d87f2h2ejkayt5nhhgojm.json`.
The code-task demo used `dev` (`--auto medium`): droid edited and committed in its
worktree, then Mesh made a two-parent integration merge, with no remote or push.
Mesh never auto-commits executor changes. The 1.6.0 help fixtures cover all emitted
flags; their diff from 1.3.0 added or removed no flags used by Mesh.

### Kiro

Kiro parses 3.1.0 `--output-format stream-json`: final text comes from
`runFinished.finalText`, falling back to agent message chunks (not thoughts). It
captures the first session id and metering credits, duration, last context percentage,
effort and model id when present. A run error, a content-filter refusal without a
successful finish, or a missing successful finish is a vendor failure even at exit 0.
Across adapters, exit 0 auto-submits only when there is no vendor failure meaning;
errors/refusals/budget or turn exhaustion become `vendor_error` for lead review.

A noninteractive tool-denial message maps to `tool_denied`; the tool is denied rather
than prompting, and the run can continue. Readonly maps to `--trust-tools=`.
The latest smoke file is `tests/smoke/results/kiro-3.1.0-2026-11-10-2.json`: headless,
session id and SIGTERM passed, model selection was observed but not observable from
the stream, and resume/effort/write/shell cases were not run. The readonly end-to-end
record is `tests/smoke/results/e2e-N13-kiro-3.1.0-run_a1cfe019a5dgqkf4tu7kwjs7sf.json`.
Readonly and edit profiles are available; `dev`/`elevated` remain unsupported. A shell
alias appearing in vendor help is not a supported profile mapping by itself.

### Antigravity and Vibe

Antigravity's 1.5.3 real stream fixtures exercise parsing, not smoke launch behavior.
The recorded 1.5.4 probe has stale capability evidence; the 1.5.2 to 1.5.3 help diff
added or removed no flags. Vibe's 3.1.0 rows are help-verified except model selection
(`yes` / `unverified`); reasoning effort is `no` / `help`. Neither has a promoted smoke
result. Warn-mode launch is possible; strict-mode launch is not.

## Deferred

- kiro, agy and vibe smoke coverage is incomplete: agy/vibe have no promoted results;
  Kiro cases S-X-3, S-X-5, S-X-7 and S-KIRO-8 were not run.
- kiro and agy `dev` profiles are unsupported.
- Idle timeout has no vendor-output validation.
- Vendor lead turns cannot launch.
- Resume. No vendor resume capability is both supported and smoke-verified.
- hermes and openclaw lack implemented launch adapters.

## Limits common to adapters

- No real vendor can be a launch-mode lead. Use an inbox lead; attempts return
  `ADAPTER_UNSUPPORTED` or emit `lead.turn_refused` with the legacy reason
  `vendor_lead_m3_lite`. Executor evidence does not remove this restriction.
- Unsupported profiles return `PROFILE_UNSUPPORTED`; Mesh never broadens permissions
  to make a command work. Only `unrestricted` may emit trust-all flags. Antigravity
  `dev`/`elevated`, Kiro `dev`/`elevated`, and Vibe `elevated` are unsupported.
- Resume requires both `supported: "yes"` and smoke verification. Every vendor resume
  row currently remains `supported: "unknown"`, including droid's passing observation,
  so no vendor run resumes today; each attempt starts fresh with the full prompt and prior feedback.
- Idle timeout defaults to `0` and has not been validated against vendor output.
- Antigravity `prompt_mode` validation and Hermes/OpenClaw command builders are not
  implemented. Installing a stub's binary may improve probe status but does not add
  build/parse support or make launch available.
- Kiro and Antigravity receive prompts as the last positional argument. A prompt
  beginning with `-` gets a leading newline so it is not treated as an option.

## Opt-in testing without unexpected spending

Probe commands read versions/help/models; model listing can contact vendor servers.
Smoke runs spend vendor quota and need explicit consent, adapter selection, a named
model, a run cap and an output directory under `/tmp/mh-*`. They are not part of the
default offline suite. The [usage guide](usage.md#10-vendor-evidence-probe-smoke-and-launch)
describes the environment switches, staged first run and coordinator-only promotion.
Committed evidence/fixtures must not be edited in place.

## Inbox agents and the test double

Hermes and OpenClaw adapter status stays `not_installed` in the recorded environment;
installing a binary permits a fresh probe but does not implement a launch adapter.

Any already-running agent that can execute shell commands can join as
`adapter=none, delivery=inbox`, including Hermes/OpenClaw. It uses its own token with
`inbox wait`/`list`, `task start`, recurring heartbeat and `submit`/`fail`. This is inbox
participation, not adapter support; Mesh starts no process for it. See
[the inbox example](examples/inbox-agent.sh).

`fake` is a test double, not a vendor integration. It is never counted as vendor support. It is absent from `adapter list` and
refused with `TEST_DOUBLE_DISABLED` unless the daemon runs with `MESH_TESTING=1`.
Its recorded fake permission flags grant no real permissions. `none` is inbox-only
and cannot launch a process.
