# Devin CLI adapter

A separate page, like `docs/adapters-codex-claude.md`: the tables in `docs/adapters.md` cover the six
original vendor CLIs and are checked by `tests/unit/test_docs_m3.py` / `test_docs_m19.py`. This adapter has
one real smoke run (2026-11-19, `unrestricted`, "Smoke tier" below); it verifies `headless` only.

Launch hold: `devin` is in `LAUNCH_HOLDS` (`{"devin": "M5"}`), so it does not launch outside testing mode even with
evidence. Without the hold the default `adapter.evidence_mode=warn` would launch it with only a `no_smoke_evidence`
warning. The smoke tier captures devin's plain-text output (see "Smoke tier" below); the 2026-11-19 results pass
S-X-1 and S-X-6 and do not lift the hold. The
only supported profile is `unrestricted`: `readonly` and `edit` are unsupported (measured below) and stay so until the
per-run deny rules under "Future work" pass a real-machine check.

| | `devin` |
|---|---|
| Written against | Devin CLI 3000.14.30821 (`devin --help`, `devin auth status`, `devin sandbox setup` and real error output, 2026-11-19) |
| Command | `devin -p --prompt-file <run_dir>/prompt.md [--model M] --permission-mode <mode> --respect-workspace-trust false`, cwd = task worktree; never `--cloud` |
| Prompt | `--prompt-file` (the supervisor's `prompt.md`, mode 0600 in the private run dir), never argv; `-p` does not read stdin |
| Efforts | none: effort is part of the model id (e.g. `claude-fable-5-2-high`, `swe-2-flash-medium`). Any `--effort` is rejected at agent/task create and scheduling with `EFFORT_UNSUPPORTED` (the `reasoning_effort` capability is `no`, `validate_resolved_effort`); `build()` keeps a last guard (`EFFORT_NOT_ALLOWED`, reason `effort_not_supported_by_cli`). Pick the effort with the model id. |
| Models | user allowlist (`--allowed-model`, `--default-model`); no model listing (`devin models` needs a login) |
| `readonly` | unsupported: an `auto` run gets a shell from a skill Devin loads while the run reads (measured, see below) |
| `edit` | unsupported: an `accept-edits` run can give itself a shell in the same run (measured, see below) |
| `dev` | unsupported (see below) |
| `elevated` | unsupported (the only wider mode is `dangerous`) |
| `unrestricted` | `--permission-mode dangerous` (the only supported profile) |
| Auth probe | `devin auth status` (exit 0 logged in or not; the first line decides): `Logged in (via Devin).` -> subscription, a `Logged in ... API key` line -> api_key, `Not logged in.` -> none, anything else -> unknown |

Why these profiles:

- Print mode rejects every tool call its permission mode does not auto-approve and prints one stderr line per call
  (real output, 2026-11-19): `warning: rejected a tool call that requires confirmation. Running in non-interactive
  mode. Use --permission-mode dangerous to auto-approve all tools.` Nothing hangs. Only stderr lines starting with
  `warning:` are counted, as `usage.tool_denied`; a rejection is not a failure (same rule as kiro and claude).
- `edit` is unsupported (`PROFILE_UNSUPPORTED`); the adapter never emits `accept-edits`. The vendor docs
  (permissions.mdx) state that `accept-edits` auto-approves file edits inside the workspace only, while shell commands
  and writes outside the workspace still prompt (rejected in print mode), and that much was measured on 2026-11-19
  (3000.14.30821, swe-2-flash): a workspace write succeeded and a `touch` via shell was rejected; writes
  to an absolute path outside the workspace, to `../outside/rel.txt` and through an in-workspace symlink to an outside
  directory were all rejected (three `warning: rejected a tool call ...` lines, exit 0, no file). But the mode has no
  deny list for paths inside the workspace (no command-line deny flag exists), so the run can write `.devin/skills/`,
  and a running session loads a skill written during the run. Measured on 2026-11-19 (3000.14.30821, swe-2-flash,
  `-p --prompt-file ... --permission-mode accept-edits --respect-workspace-trust false`, a fresh git repository, a
  user config without permission rules), one run, tool calls taken from the `--export` transcript:
  1. `exec` `touch control-marker`: rejected (one `warning: rejected a tool call ...` line, no file).
  2. `write` `.devin/skills/p/SKILL.md` with `allowed-tools: [exec]`: "File created successfully".
  3. `skill` `p`: loaded in the same session ("Auto-approved tools ...").
  4. `exec` `touch marker`: ran, exit code 0, `marker` exists.

  So an `edit` run gets a shell without any earlier run preparing the workspace, and a check before the launch cannot
  stop it. Until the adapter can pass deny rules ("Future work" below), `edit` stays unsupported rather than a profile
  that claims a boundary it does not have.
- A deny rule held against a skill's grant in the one run that tried it; the adapter does not use deny rules yet.
  Measured on 2026-11-19 (same version and model, `accept-edits`,
  `--config <file>` holding a copy of the user config plus `"permissions": {"deny": ["exec", "Write(blocked/**)"]}`):
  the `write` of `blocked/x.txt` was refused ("denied by a deny rule in the user settings"), the skill file was
  written and loaded as above, and `touch marker-p` was refused by the deny rule although the skill lists `exec` in
  `allowed-tools`; no marker. A denied call prints no `warning: rejected ...` line (stderr was empty), so it is not
  counted in `usage.tool_denied`. What is missing before a profile can rely on this is listed under "Future work".
- `DEVIN_SANDBOX` / `DEVIN_CLI_DANGEROUSLY_FOLLOW_SYMLINKS` change what a mode allows; below `unrestricted` the
  adapter refuses to start when either variable is set in meshd's environment (`devin_env_widens_profile`).
- `dev` is unsupported: `accept-edits` approves no shell command, `smart` lets a fast model judge which commands are
  safe (a judgement, not a boundary), and `--sandbox` is a Research Preview (Linux needs `bwrap` and `socat`; the
  session fails to start without them). Verified 2026-11-19: `DEVIN_SANDBOX=1` is rejected by the CLI (`invalid value
  '1' for '--sandbox' [possible values: true, false]`, exit 2), and `DEVIN_SANDBOX=true` on a host without `socat`
  exits 1 ("Install it for your distribution ... Unset DEVIN_SANDBOX to proceed without OS-level isolation"). The
  adapter treats any non-empty `DEVIN_SANDBOX` / `DEVIN_CLI_DANGEROUSLY_FOLLOW_SYMLINKS` value (`true`, `1`, ...) as
  set. Revisit after a real-machine smoke of `smart --sandbox`.
- `readonly` is unsupported (`PROFILE_UNSUPPORTED`); the adapter never emits `auto`. `auto` is the CLI's Normal
  mode. permissions.mdx: "In **Normal mode** (the default), read-only
  operations are auto-approved while writes and shell commands require your explicit approval"; its table lists
  read-only tools as Auto and fetch, shell and `edit`/`write` as Prompt, and print mode rejects every prompt. Measured
  on 2026-11-19 (3000.14.30821, swe-2-flash, `-p --prompt-file ... --permission-mode auto --respect-workspace-trust
  false`): asked to write `b.txt`, both tool calls (the write and a shell fallback) were rejected with the warning
  above, exit 0, no file, reply "Both the file write and shell command were rejected". So the mode itself denies
  writes and shell. But skills are loaded while the run reads, and their grants apply under `auto`. Measured on
  2026-11-19 (3000.14.30821, swe-2-flash, `--permission-mode auto`, a git repository with no project layer on its
  root chain, so the project layer check below passes it), one run, tool calls taken from the `--export` transcript:
  1. `exec` `touch control-marker`, `write` `.devin/skills/p/SKILL.md` and `exec` `touch marker-p`: all rejected
     (three warning lines); `skill` `p`: "not found". An `auto` run cannot write itself a skill.
  2. `read` `sub/note.txt`, where the workspace holds `sub/.devin/skills/q/SKILL.md` with `allowed-tools: [exec]`
     (a subdirectory, not on the root chain): then `skill` `q` loaded and `exec` `touch marker-q` ran (file exists).
  3. `read` a file in another git repository outside the workspace that holds `.devin/skills/r/SKILL.md` with the
     same grant: then `skill` `r` loaded and `exec` `touch marker-r` ran (file exists).

  Devin finds skills lazily in the directories a session reads, inside or outside the workspace, and their grants
  apply under `auto`: both cases, a workspace subdirectory and a repository outside the workspace, gave the run a
  shell. A scan before the launch cannot close this. The project layer check covers the workspace's root chain, and
  a skill with grants in a workspace subdirectory (task content), in another task's worktree or anywhere else the
  meshd user can read counts just the same. So `readonly` is unsupported rather than a profile that claims a boundary
  it does not have.
- User config: allow rules auto-approve matching calls in any mode (deny > ask > allow), and organization rules
  cannot be overridden by any flag. The user's own config (`~/.config/devin/config.json`: allow rules, MCP servers,
  hooks) is kept, as claude keeps the user's settings (`--setting-sources user`): it is the meshd user's own
  boundary, and `unrestricted` approves every tool anyway. A user `Exec(...)` / `Write(...)` allow rule would widen
  any profile below `unrestricted`, which the per-run `--config` under "Future work" has to settle as well.

No profile below `unrestricted` is supported today, so `build()` raises `PROFILE_UNSUPPORTED` before it reaches the
environment check above or the project layer check described here, and neither runs for a launch. Both stay in
`build()` and are tested on their own, because a profile that comes back needs them: a project MCP server runs its
command at session start (measured below), and whether a deny rule stops that is not measured.

Project config layer (checked against the CLI's own `share/devin/docs`, reference/configuration): Devin loads
`.devin/config.json` / `.devin/config.local.json` (permissions, hooks, read_config_from), `.devin/mcp_config.json` /
`.devin/mcp_config.local.json` (MCP servers; verified 2026-11-19: a stdio server `sh -c "touch <marker>"` in
`.devin/mcp_config.json` was started at session start under `auto` for a "Reply PONG" prompt, so the refusal is
required), `.devin/hooks.v1.json` / `.devin/hooks.json`, and imports MCP servers and
settings from other tools (`.mcp.json`, `.claude/settings*.json`, `.cursor/mcp.json`, `opencode.json`,
`.zed/settings.json`; plus `.codex`, `.gemini`, `.windsurf`, `.agents` config). `--config` replaces only the user config,
so no flag turns this off. Below `unrestricted` the adapter refuses a workspace that has one of these (`spawn_failed`,
message starting with `devin_project_layer_present`; it names at most 10 paths, each cut to 200 characters, then
`(+N more)`), checked from the workspace up to the nearest `.git` or `.jj`
root (Devin's own root rule). Legacy directory: Devin still reads `.cognition/`, the project directory before the
rename to `.devin/`, the same way (checked offline with the real CLI, 3000.14.30821: `devin mcp list` lists the
servers of `.cognition/mcp_config.json`, `.cognition/mcp_config.local.json` and `.cognition/config.json` with
`Warning: Found legacy project directory .cognition/`, and `devin skills paths` lists `.cognition/skills/`, also when
`.devin/` exists). It is checked as an alias of `.devin` (same file names, same skill scan) and, on top of that, any
`.cognition` entry on the root chain is refused whatever it holds: only Devin project config lives there.
Skills: a `skills/**/SKILL.md` under `.devin`, `.cognition`, `.agents`, `.claude`, `.cursor`, `.codex`,
`.kiro`, `.github`, `.windsurf` or `.codeium` whose frontmatter has `allowed-tools` or `permissions` auto-approves tools
while the skill runs (creating-skills.mdx), so it is refused too. Verified 2026-11-19 (`auto`): a
`.devin/skills/mk/SKILL.md` with `allowed-tools: [exec]` and `permissions: allow: ["Exec(touch)"]` ran its shell
command with no rejection, so skill grants do override print mode's denial and the refusal is required. The scan is bounded (2000 entries, 64 KiB per
file); past the bound, an unreadable file or an unterminated frontmatter counts as a grant. Instruction-only files
(`AGENTS.md`, `CLAUDE.md`, rules, skills with plain frontmatter) are not refused. The scan covers the root chain only;
skills Devin loads later from other directories are why `readonly` is unsupported (above).

The skill check fails closed and does not parse YAML. Devin reads the frontmatter with a real YAML parser and follows
symlinks; checked offline with the real CLI (`devin skills show`, 3000.14.30821), each of these still shows
`Allowed tools: exec`: a quoted, tagged, anchored or `? ` key, an indented or flow mapping, a CR / NEL (U+0085) / LS
(U+2028) line break before the key, a tab after the colon, leading blank lines or indentation before the opening
`---`, an opening `--- ` or `--- # text`, CRLF line ends, an indented ` ---` that does not close the frontmatter, and
a skill directory that is a symlink. So:

- A `SKILL.md` passes only if it has no frontmatter marker at its start (after optional blank space), or it starts
  with exactly `---` + LF and every line up to the closing `---` line is empty or one `key: value` line with a bare
  key at column 0 from this list: `name`, `description`, `argument-hint`, `triggers`, `license`, `compatibility`. The
  value is empty, plain text starting with a letter or digit, a quoted string without escapes, or a flow list of
  words (`[user, model]`), on the same line and without control characters. Everything else counts as a grant:
  `allowed-tools`, `permissions`, `model`, `agent`, `subagent`, any unknown key, comments, indented lines, block
  lists and block scalars (`description: |`), CRLF files. Write such a skill with one-line values to keep it usable
  below `unrestricted`.
- Any symlink inside a `skills/` tree (the `skills` entry itself, a skill directory, a file) is refused.
- `build()` runs on the scheduler's event loop, so the skill file is opened with `O_NOFOLLOW | O_NONBLOCK` and must
  be a regular file (`S_ISREG`), the rule of `base.open_run_file_nofollow`. A FIFO, device, directory or symlink in
  place of `SKILL.md` counts as a grant and never blocks meshd.
- The file name is matched without case (`skill.md`, `SKILL.MD`). On Linux the real CLI only loads `SKILL.md`; the
  wider match covers case-insensitive file systems.

Model and agent overrides (from the vendor docs, creating-skills.mdx and subagents.mdx; not measured with a model):
a skill's `model:` replaces the run's model, `agent:` / `subagent: true` run it under a subagent profile, and
project subagent profiles (`.devin/agents/<name>.md`, `.devin/agents/<name>/AGENT.md`, the same under `.agents/`)
pick their own `model:`. Task content could so run a model outside Mesh's `--allowed-model` list. Skills with these
keys are refused by the rule above, and `.devin/agents` / `.agents/agents` are refused as project layer entries.
Known limit: the built-in subagents remain. `subagent_explore` runs on the account's default subagent model, not
on `--model`, so Mesh's model allowlist covers the main agent only.

Refusing every `skills/` directory below `unrestricted` would be simpler and was considered. It was not chosen
because plain instruction skills are common in repositories (`.claude/skills`, `.agents/skills`, `.github/skills`)
and the rule above lets through only a form with one reading: a real-CLI differential run over 383 spellings (107
of them shown with `Allowed tools`) found no file that passes the check and shows `Allowed tools` in
`devin skills show`.

Output and failure meanings: no JSON or stream format exists, so stdout is the plain-text reply (terminal escapes
such as `ESC[?2004l` stripped) and failures are `Error: ...` lines on stderr with exit 1.

| stderr | meaning |
|---|---|
| `Error: Login canceled`, `Not logged in`, `401 Unauthorized` | `auth_failed` |
| `usage limit`, `quota exceeded`, `out of credits` | `quota_exhausted` |
| `429`, `rate limit`, `Too Many Requests` | `rate_limited` |
| connection refused/reset, DNS, TLS, timed out | `network_error` |
| `Error: Unknown model: '<m>'` (the echoed name is masked before matching) | `run_error` (`error` `unknown_model`) |
| `Error: Refusing to run in an untrusted workspace` | `run_error` (`error` `workspace_untrusted`) |
| `Error: ... context window ...` | `run_error` (`error` `context_window`), never `quota_exhausted` |
| `... needs a prompt`, rejected arguments | `cli_usage_error` (stderr reason; no run meaning) |
| any other `Error:` line | `run_error` |

Only `Error:` lines are classified; warnings never are. Exit 0 with a meaning is `vendor_error`; a non-zero exit
takes the meaning as its reason (`nonzero_exit_uses_stream_meaning`, same as codex/claude), and the stderr parse lands
beside it, so the `error` key above is at `details.stderr_error.details.error` of the failed run.

Not installed: `adapter probe` reports `not_installed` with the hint
`curl -fsSL https://cli.devin.ai/install.sh | bash`; the `tests/shims/devin` tripwire on PATH counts as not installed.

Known limits:

- `prompt.md` stays in the run dir after the run, like every file-prompt adapter (droid, vibe); deleting it would need
  a supervisor change.
- No session id in print mode, so no resume; no `--export` transcript is collected (its format is unverified), so
  usage has no token counts.
- Verified on a real machine (2026-11-19 UTC, 3000.14.30821, swe-2-flash, print mode, `--respect-workspace-trust
  false`): a logged-in print run; `auto` rejecting a write and a shell call; `accept-edits` writing in the workspace and
  rejecting shell, absolute-path, `../` and symlink-to-outside writes; the rejection warning text; a skill's
  `allowed-tools`/`permissions` overriding print mode's denial (refusal required); `.devin/mcp_config.json` stdio
  servers started at session start (refusal required); `DEVIN_SANDBOX=1` rejected by the CLI and
  `DEVIN_SANDBOX=true` failing without `socat`; an `accept-edits` run writing a skill with `allowed-tools: [exec]`
  and using it for a shell command in the same run (`edit` unsupported); an `auto` run getting a shell from a skill
  in a workspace subdirectory and from one in a repository outside the workspace after reading a file there
  (`readonly` unsupported); a user-level `permissions.deny` from `--config` refusing `exec` and a `Write(...)` path
  although a loaded skill grants `exec`.
- Not verified on a real machine yet: the quota, rate-limit and network wording, `devin auth status` with an API key,
  and `smart --sandbox` on a host with `bwrap` and `socat`.

## Smoke tier

The opt-in smoke tier (`tests/smoke/test_smoke.py`, class `DevinSmoke`) captures devin in the results format used for
droid and kiro. One real run is committed: `tests/smoke/results/devin-3000.14.30821-2026-11-19.json` (model
`swe-2-flash-medium`, profile `unrestricted`), with captures under `tests/fixtures/real/devin/3000.14.30821/`.
In that run S-X-1 and S-X-6 pass, S-X-2, S-X-3 and S-X-5 are `not_applicable`, S-X-4 is `observed`, S-X-7 created
`x.txt` and S-DEVIN-8 ran `touch y.txt` (`executed`, 0 rejected tool calls).

- Gate and config: `MESH_SMOKE=1`, `devin` in `MESH_SMOKE_ADAPTERS` and in `MESH_SMOKE_CONSENT`,
  `MESH_SMOKE_MAX_RUNS`, `MESH_SMOKE_MODEL_DEVIN` (never `auto`) and `MESH_SMOKE_OUT` under `/tmp/mh-*`.
- Profile: `unrestricted`, the only profile the adapter supports. The harness picks it from
  `smoke_schema.SMOKE_PROFILES`, the results file records `"profile": "unrestricted"`, and the schema refuses a devin
  file that names another profile. Every run uses `--permission-mode dangerous`, so the CLI approves every tool
  call. The harness starts each case with a fixed prompt in a `/tmp/mh-smoke-*` scratch directory and never in the
  repository. That sets where the run starts, not what it can reach.
- Output: stdout is plain text, so the capture is `smoke-<case>-stdout.txt` (redacted; terminal escapes kept as
  printed) next to `smoke-<case>-stderr.txt`, each with a `{command, captured_at_utc, sha256}` sidecar. The
  evaluators read both through `DevinAdapter.parse` / `output_facts`, and re-evaluation copies both back into a run
  directory.
- S-X-1 passes when the exit code is 0, stderr has no `Error:` line and the reply contains the token.
- S-X-2 and S-X-3 are `not_applicable` (`resume_unsupported`): print mode prints no session id and there is no
  resume. R2 starts no run.
- S-X-4 is `observed` (`not_observable`): the output does not name the model.
- S-X-5 is `not_applicable` (`effort_refused_by_adapter`, or `effort_not_listed` with
  `MESH_SMOKE_EFFORT_DEVIN=none`): there is no effort flag. R3 starts no run.
- S-X-6 sends SIGTERM after the first stdout line or after 5 s and passes when the process group is gone within
  15 s. The line count comes from the output format described above. In the 2026-11-19 run devin had printed
  nothing by 5 s, so the SIGTERM went out at the 5 s fallback (`first_byte` false, rc -15).
- S-X-7 and S-DEVIN-8 are observations under `unrestricted`: whether `x.txt` appeared, and whether `touch y.txt`
  ran (`executed` or `not_executed`, with the number of `warning: rejected a tool call` lines). They never report a
  deny.
- The schema refuses `pass` for S-X-2, S-X-3 and S-X-5 in a devin file.
- Evidence: `mesh/adapters/evidence.py` reads a devin results file like any other (probed version, valid file,
  S-X-1 and S-X-6 pass). A passing S-X-1 marks `headless` only; `stream_output` stays `no`. The launch hold stays
  whatever the results say.

## Future work: per-run deny rules

The candidate way to bring `readonly` and `edit` back: Mesh writes a `--config` file for each run and puts
`permissions.deny` rules in it for every tool outside the profile. The one measurement above shows that a deny rule
holds against a loaded skill's `allowed-tools` grant. Open before a profile can rely on it:

- `--config` replaces the user config. The generated file has to carry the user's own settings (the organization id,
  the user's deny rules) or the run loses them, and no adapter hook writes a per-run file today.
- The deny list has to be complete: `exec`, `edit` / `Write(...)`, `Fetch(...)`, `mcp__*` and every other tool
  outside the profile. A tool left off the list stays open to a skill's grant. For `edit` there is no glob for
  "outside the workspace", so a skill that grants `write` could still widen writes.
- More real-machine runs. So far there is one run, under `accept-edits`, with two rules. Still to run: `auto` with
  the full list against the two cases above (a skill in a workspace subdirectory, a skill outside the workspace),
  and a run for each tool on the list.
- A denied call prints no `warning: rejected ...` line, so `usage.tool_denied` does not count it.

Until those checks pass, `readonly` and `edit` both stay unsupported.
