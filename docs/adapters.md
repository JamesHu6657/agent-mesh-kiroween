# Agent Mesh — Adapter behavior and evidence

Mesh can launch executors through droid, kiro-cli, codex, claude, agy, vibe, and devin
(currently on a launch hold), or accept an already-running inbox agent through the `none`
adapter. Hermes and OpenClaw have stub adapters, not launch integrations. Mesh does not
sandbox any of these agents.

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
not waived in warn mode. `devin` is on an explicit hold until M5 in this checkout (see
[adapters-devin.md](adapters-devin.md)); no other adapter is.

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
| Antigravity (`agy`) | yes | 1.5.12 | partially_verified | false | launch_cases_failed | tests/smoke/results/agy-1.5.12-2026-11-19.json | Allowed with evidence warning and supported profile |
| Kiro CLI (`kiro`) | yes | 3.1.0 | partially_verified | true | — | `tests/smoke/results/kiro-3.1.0-2026-11-10-2.json` | Allowed when installed and profile supported |
| Mistral Vibe (`vibe`) | yes | 3.1.0 | partially_verified | false | launch_cases_failed | tests/smoke/results/vibe-3.1.0-2026-11-19.json | Allowed with evidence warning and supported profile |
| Hermes (`hermes`) | no | — | not_installed | false | no_smoke_evidence | not run | Cannot launch: stub adapter |
| OpenClaw (`openclaw`) | no | — | not_installed | false | no_smoke_evidence | not run | Cannot launch: stub adapter |

Smoke runs on 2026-11-19: agy 1.5.12 passed headless, session id, resume and model selection
(S-X-1 to S-X-4) with the prompt directly after `--print`, and S-X-6 is recorded as a failure because
agy exits 1 with `error: interrupted` on SIGTERM (its process group was gone in 0.2 s). A first
attempt the same day, with the earlier argv, exited before any model call in every case; it is the
reason for the argv change (see "Antigravity prompt argument (agy 1.5.12)" below). vibe 3.1.0 passed
headless, session id and resume, and S-X-6 is recorded as a failure because vibe exits 3 on SIGTERM
(its process group was gone in 0.2 s). Codex 0.166.0, Claude Code 2.1.325 and Devin 3000.14.30821
results are in `tests/smoke/results/`; see the README status table.

Strict results require a matching successful local probe and supported profile.
The table reproduces the recorded evidence reason for stubs; implemented launch
support is separately required in both modes. `—` denotes a null reason.

Check your own `adapter show`/`probe` output. Missing binaries remain unavailable in
both modes. `adapter models` may contact vendor servers; it starts no inference but
can require the vendor's login. Mesh does not query a live model list for Vibe, Codex,
Claude, or Devin (Vibe and Claude have no read-only model-list command; Codex and
Devin have `codex debug models` / `devin models`, which Mesh does not call), so their
model allowlists are supplied by the user (curated UI presets are exposed on
`adapter.list` / `adapter.show` via `model_presets`).

### Supported permission profiles and safe defaults

Each adapter declares its exact `supported_profiles` (matching `profile_flags()` in
canonical order `readonly`, `edit`, `dev`, `elevated`, `unrestricted`) and exposes
`supported_profiles`, `profile_flags`, and `model_presets` in `adapter.list`,
`adapter.show`, and `adapter.probe`:

| Adapter | `supported_profiles` | Safe default (`agent create` without `--default-profile`) |
|---|---|---|
| `droid`, `none`, `fake` | `readonly`, `edit`, `dev`, `elevated`, `unrestricted` | `edit` |
| `vibe` | `readonly`, `edit`, `dev`, `unrestricted` | `edit` |
| `agy`, `kiro`, `claude` | `readonly`, `edit`, `unrestricted` | `edit` |
| `codex` | `readonly`, `dev`, `unrestricted` | `dev` (`--sandbox workspace-write`) |
| `devin` | `unrestricted` | `edit` (never auto-defaults to `unrestricted`; on M5 launch hold) |
| `hermes`, `openclaw` | *(none — stub)* | `edit` |

When `default_profile` is omitted from `agent.create`, `meshd` picks the first
supported safe profile from `("edit", "dev", "readonly")` (so `codex` defaults to
`dev` instead of unsupported `edit`), falling back to `"edit"` when an adapter has no
safe profile (`devin`, `hermes`, `openclaw`). Mesh never auto-defaults to or silently
broadens to `elevated` or `unrestricted`, and an explicit `--default-profile edit` on
`codex` is stored verbatim rather than widened.

### Capability rows (committed evidence)

| Adapter | headless | stream_output | model_selection | reasoning_effort | resume |
|---|---|---|---|---|---|
| droid 1.6.0 | yes / smoke | yes / smoke | yes / smoke | yes / smoke | unknown / smoke |
| agy 1.5.12 | yes / smoke | yes / smoke | yes / smoke | yes / stale | unknown / smoke |
| kiro 3.1.0 | yes / smoke | yes / smoke | yes / help | yes / help | unknown / smoke |
| vibe 3.1.0 | yes / help | yes / help | yes / unverified | no / help | unknown / help |
| hermes | unknown / none | unknown / none | unknown / none | unknown / none | unknown / none |
| openclaw | unknown / none | unknown / none | unknown / none | unknown / none | unknown / none |

Each cell is `supported / verification`; these are recorded versions, not a live inventory.
The agy row is not copied from a live `adapter show`: it is what the probe and evidence code
give offline for the committed 1.5.12 help capture (`tests/fixtures/real/agy/1.5.12/help.txt`)
and results file. `smoke` comes from the passing S-X-1 (headless, stream output), S-X-2 and
S-X-3 (resume) and S-X-4 (model selection). Reasoning effort stays `stale`: S-X-5 was
`not_applicable`, and the adapter's help-level verification was recorded at 1.5.2. Resume stays
`unknown` although its cases passed, so Mesh still does not resume agy sessions (see "Limits
common to adapters").

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
Its smoke evidence is the 1.5.12 run of 2026-11-19,
`tests/smoke/results/agy-1.5.12-2026-11-19.json` (case table below). The 1.5.2, 1.5.3 and
1.5.12 help texts are byte-identical, so no flag was added or removed between them. Vibe's
3.1.0 rows are help-verified except model selection (`yes` / `unverified`); reasoning effort
is `no` / `help`; its smoke evidence is `tests/smoke/results/vibe-3.1.0-2026-11-19.json`.
Both results pass S-X-1 and fail S-X-6, so warn-mode launch is possible (with an evidence
warning) and strict-mode launch is not.

#### Antigravity prompt argument (agy 1.5.12)

Mesh builds `agy --print <prompt> --output-format stream-json --model <m> [--effort <e>]
<profile flags> [--sandbox] [--agent <name>] [--add-dir <run_dir>] [--conversation <id>]
--print-timeout <n>s`. The prompt is its own argv item directly after `--print`; every
other flag follows it. For a prompt over 100 000 bytes that item is the sentence pointing
at the prompt file, and `--add-dir <run_dir>` is added as before.

Why this form:

- agy 1.5.12, first smoke attempt of 2026-11-19 (captured 11:20:59Z at Mesh commit `94436d1`,
  before this change): `--print` takes the next argv item as the prompt. The earlier form,
  `--print --output-format stream-json ... <prompt last>`, exited 2 in every run before any
  model call with `--print took "--output-format" as its prompt`. That attempt is the reason
  for this change. Its earlier results file and captures were replaced, under the same file
  name, by the rerun in the next item.
- agy 1.5.12, rerun with this form (captured 14:08:47Z the same day at Mesh commit `d7467e2`,
  the commit that changed the argv): S-X-1 to S-X-4 pass. The sidecar of each capture under
  `tests/fixtures/real/agy/1.5.12/` records the command, for example `agy --print "Reply with
  exactly MESH_SMOKE_OK" --output-format stream-json --model gemini-3.8-flash-low --mode plan
  --print-timeout 180s`; the resume run adds `--conversation <id>` before `--print-timeout`.
- agy 1.5.3: the sidecar of each real stream capture under `tests/fixtures/real/agy/1.5.3/`
  records `agy -p <prompt> --model <m> --output-format stream-json` (`-p` is the help's
  alias of `--print`). Those runs printed stream-json, so 1.5.3 accepted the prompt directly
  after the flag with other flags behind it.
- `--help` does not show the change: the 1.5.12 help text
  (`tests/fixtures/real/agy/1.5.12/help.txt`) is byte-identical to the 1.5.2 and 1.5.3
  fixtures (all three have sha256
  `e92946f85cec63eca61f93014ca49f53b1e84277eac7954e7fac7d8d712940fd`), so the probe's help
  hash and flag diff stay unchanged and cannot detect it.
- `--print=<prompt>`, which the 1.5.12 message suggests, is not used: nothing recorded
  shows 1.5.3 or 1.5.4 accept it. The earlier trailing-prompt form has no recorded success
  on any agy version.

This form has been run against real agy 1.5.12: the rerun is the committed results file
`tests/smoke/results/agy-1.5.12-2026-11-19.json`, promoted with its captures. It was captured
at `d7467e2`. The later review commit `7be01ec` only narrowed which stderr text counts as
`cli_usage_error`; it did not change the argv, and a unit test rebuilds every recorded command
with the current `build()`. No recorded run on 1.5.3 or 1.5.4 used this form with `--print`
spelled out (the 1.5.3 captures used `-p`). If agy rejects the argv with the 1.5.12 message
again, the run fails with reason `cli_usage_error` (details: the stderr, the flag, the item it
consumed) instead of `exit_nonzero`.

#### Antigravity smoke results (agy 1.5.12)

`tests/smoke/results/agy-1.5.12-2026-11-19.json`: model `gemini-3.8-flash-low`, profile
`readonly` (`--mode plan`), five runs on 2026-11-19 (S-X-1, S-X-2 and S-X-4 read the same run).

| Case | Result | Recorded | Fixture |
|---|---|---|---|
| S-X-1 headless + stream | pass | rc 0; the reply token is in the stream and in the parsed final text | `tests/fixtures/real/agy/1.5.12/smoke-S-X-1-stdout.jsonl` |
| S-X-2 session id | pass | the `init` event's `conversation_id`; `init.cwd` is the smoke workspace | `tests/fixtures/real/agy/1.5.12/smoke-S-X-1-stdout.jsonl` |
| S-X-3 resume | pass | rc 0; `--conversation <id>` continued the S-X-1 session; the reply names the token | `tests/fixtures/real/agy/1.5.12/smoke-S-X-3-stdout.jsonl` |
| S-X-4 model selection | pass | `init.model` is `gemini-3.8-flash-low` | `tests/fixtures/real/agy/1.5.12/smoke-S-X-1-stdout.jsonl` |
| S-X-5 reasoning effort | not_applicable | `effort_not_listed`: no effort run was made | — |
| S-X-6 SIGTERM | fail | `exited_with_error`: rc 1, stderr `error: interrupted`; alive at the SIGTERM, process group gone 0.208 s later | `tests/fixtures/real/agy/1.5.12/smoke-S-X-6-stdout.jsonl` |
| S-X-7 readonly write attempt | observed | rc 0, no `x.txt` | `tests/fixtures/real/agy/1.5.12/smoke-S-X-7-stdout.jsonl` |
| S-AGY-8 readonly shell attempt | observed | outcome `deny`: rc 0, no `y.txt`, 0 tool errors, no `denied_actions` | `tests/fixtures/real/agy/1.5.12/smoke-S-AGY-8-stdout.jsonl` |

S-X-6 fails as a termination case, not as a launch. The SIGTERM went out at the 5 s fallback
(5.012 s), when agy had printed 2 stdout lines and no model output. agy was alive, wrote a
`result` event with status `ERROR` and error `interrupted`, printed `error: interrupted` and
exited 1; its process group was gone 0.208 s after the signal. The evaluator passes S-X-6 only
when the exit is the signal's own (rc -15 or 143), or when the process exits by itself with no
error result in its stream and no stderr at a non-zero rc. agy's orderly exit has both, so it
is recorded as `exited_with_error`, like vibe's exit 3 with stderr. That rule is a known
evaluator issue. Until it is settled, S-X-6 keeps agy at `launch_cases_failed` in strict mode.

S-X-7 and S-AGY-8 are observations, not permission guarantees. In both runs the stream shows
one tool step, `write_file`, which wrote agy's own plan file under
`~/.gemini/antigravity-cli/brain/<conversation id>/`, outside the workspace, and the reply
asked for the plan to be reviewed. No tool step ended in state `ERROR`. The outcome `deny`
therefore means only that `y.txt` was absent at rc 0; no denial was recorded.

## Smoke tier for codex, claude and devin

The opt-in smoke tier (`tests/smoke/test_smoke.py`) has cases for `codex`, `claude` and
`devin` in the results format used for droid and kiro: one
`<adapter>-<version>-<date>.json` file with a sidecar, and captures under
`<adapter>/<version>/` in the real fixture tree. Each of the three has one real run, made on
2026-11-19 and committed with its captures:

| Adapter | Results file | Model | Profile | Launch cases | Other cases | Strict launch |
|---|---|---|---|---|---|---|
| codex 0.166.0 | `tests/smoke/results/codex-0.166.0-2026-11-19.json` | `gpt-5.6-luna` | `readonly` | S-X-1 and S-X-6 pass | S-X-2 and S-X-3 pass; S-X-4 and S-X-5 `observed`; S-X-7: no `x.txt`, the patch was rejected by the read-only sandbox; S-CODEX-8: no `y.txt`, no command item in the stream | enabled |
| claude 2.1.325 | `tests/smoke/results/claude-2.1.325-2026-11-19.json` | `ds-flash` (DeepSeek through an Anthropic-compatible proxy, not an Anthropic model) | `readonly` | S-X-1 and S-X-6 pass | S-X-2, S-X-3 and S-X-4 pass; S-X-5 `not_applicable`; S-X-7 and S-CLAUDE-8: no `x.txt` / `y.txt`, 0 `permission_denials` (the deny list removes Bash, Edit and Write, so no tool call was there to deny) | enabled |
| devin 3000.14.30821 | `tests/smoke/results/devin-3000.14.30821-2026-11-19.json` | `swe-2-flash-medium` | `unrestricted` | S-X-1 and S-X-6 pass | S-X-2, S-X-3 and S-X-5 `not_applicable`; S-X-4 `observed`; S-X-7 created `x.txt`; S-DEVIN-8 ran `touch y.txt` | no: launch hold (M5) |

The offline tests drive these cases with stub binaries and synthetic fixtures; they do not
read the real results. Their own pages
([adapters-codex-claude.md](adapters-codex-claude.md), [adapters-devin.md](adapters-devin.md))
describe the adapters.

| Adapter | Smoke profile | stdout capture | Session id and resume | Effort | Model | Shell case |
|---|---|---|---|---|---|---|
| codex | `readonly` (`--sandbox read-only`) | `smoke-<case>-stdout.jsonl` | `thread.started` id, `exec resume <id>` | `--config model_reasoning_effort=<e>`; the stream does not echo it, so `observed` at best | not named in the stream: `observed` | S-CODEX-8 records the command items and how many failed |
| claude | `readonly` (`--permission-mode dontAsk` and the readonly deny list) | `smoke-<case>-stdout.jsonl` | init `session_id`, `--resume <id>` | `--effort <e>`; the stream does not echo it, so `observed` at best | the init event's `model` must equal `MESH_SMOKE_MODEL_CLAUDE` (name the full id, not an alias) | S-CLAUDE-8 records the result's `permission_denials` |
| devin | `unrestricted` (`--permission-mode dangerous`) | `smoke-<case>-stdout.txt` (plain text) | none: S-X-2 and S-X-3 are `not_applicable` | none: S-X-5 is `not_applicable` | not printed: `observed` | S-DEVIN-8 records what happened, never a deny |

Smoke runs use the `readonly` profile for every adapter except devin. The devin adapter
supports only `unrestricted`, so the harness picks that profile for it
(`smoke_schema.SMOKE_PROFILES`) and the results file records `"profile": "unrestricted"`.
A devin smoke run approves every tool call. The harness still starts each case with a
fixed prompt in a `/tmp/mh-smoke-*` scratch directory, never in the repository; that sets
where the run starts, not what it can reach. S-X-7 and S-DEVIN-8 therefore record what
happened under `unrestricted` (file created, command executed or not executed) instead of
a readonly denial.

The results schema (`mesh/adapters/smoke_schema.py`) checks three things per adapter: the
profile above, the stdout capture name above, and that a case the adapter cannot support
(devin S-X-2, S-X-3 and S-X-5; vibe S-X-5) is never `pass`. A file that breaks one of them
is `evidence_invalid`.

Evidence for these adapters is read like droid's and kiro's: the latest results file at the
probed version must validate and its S-X-1 and S-X-6 must pass. For devin, S-X-1 means exit
code 0, no `Error:` line on stderr and the token in the plain-text reply; it marks
`headless` only, and `stream_output` stays `no`. Passing devin results do not lift its
launch hold.

Environment: `MESH_SMOKE_MODEL_CODEX`, `MESH_SMOKE_MODEL_CLAUDE` and
`MESH_SMOKE_MODEL_DEVIN` are required and never `auto`. `MESH_SMOKE_EFFORT_CODEX`,
`MESH_SMOKE_EFFORT_CLAUDE` and `MESH_SMOKE_EFFORT_DEVIN` default to `low`; devin has no
effort flag, so its S-X-5 is `not_applicable` whatever the value. The stdout line counts
S-X-6 waits for (codex 3, claude 2, devin 1) come from each adapter's documented output
format. In the 2026-11-19 runs claude had 7 lines when the SIGTERM went out at 1.2 s; codex
had 2 lines and devin none by the 5 s fallback, so their SIGTERM went out at 5 s, before
any model output.

## Deferred

- kiro, agy and vibe smoke coverage is incomplete: the agy 1.5.12 and vibe 3.1.0 results
  (2026-11-19) fail a launch case, S-X-6: both pass S-X-1, and each CLI exits non-zero on
  SIGTERM (agy 1, vibe 3) with its process group gone in 0.2 s. No agy effort run was made
  (S-X-5 `not_applicable`). Kiro cases S-X-3, S-X-5, S-X-7 and S-KIRO-8 were not run.
- codex, claude and devin each have one real run (2026-11-19, table above). The claude run
  used a DeepSeek model through a proxy, not an Anthropic model. devin stays on its launch
  hold whatever its results say.
- kiro and agy `dev` profiles are unsupported.
- Idle timeout has no vendor-output validation.
- Vendor lead turns: refused by default (`vendor_lead_m3_lite`), opt-in behind
  `mesh config set lead.vendor_leads true --user` (follows `adapter.evidence_mode` via
  `effective_launch` just like vendor workers: `"warn"` launches with `evidence_warning`,
  `"strict"` requires valid probe + smoke evidence; `lead.profile` default `readonly`,
  `devin` launch hold and `hermes`/`openclaw` stubs still refused; toggling off refuses new
  turns without stopping an already-running turn).
- Resume. No vendor resume capability is both supported and smoke-verified.
- hermes and openclaw lack implemented launch adapters.

## Limits common to adapters

- No vendor can be a launch-mode lead by default. Attempts fail with rc 13
  `ADAPTER_UNSUPPORTED` or emit `lead.turn_refused` with legacy reason
  `vendor_lead_m3_lite` regardless of executor evidence; use an inbox lead, or explicitly
  opt in with `mesh config set lead.vendor_leads true --user` (follows `adapter.evidence_mode`
  via `effective_launch` identically to vendor workers: `"warn"` launches with an
  `evidence_warning`, while `"strict"` requires valid matching probe + smoke evidence;
  `devin` stays held, `hermes`/`openclaw` stubs stay disabled, and `lead.profile` defaults
  to `readonly`).
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
- Kiro receives the prompt as the last positional argument, Antigravity as the argument
  directly after `--print`. A prompt beginning with `-` gets a leading newline so it is
  not treated as an option.

## Opt-in testing without unexpected spending

Probe commands read versions/help/models; model listing can contact vendor servers.
Smoke runs spend vendor quota and need explicit consent, adapter selection, a named
model, a run cap and an output directory under `/tmp/mh-*`. They run with the `readonly`
profile, except devin, which runs `unrestricted` (see the smoke tier section above). They
are not part of the default offline suite. The [usage guide](usage.md#10-vendor-evidence-probe-smoke-and-launch)
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
