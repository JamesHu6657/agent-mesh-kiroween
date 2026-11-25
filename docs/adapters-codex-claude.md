# Codex CLI and Claude Code adapters

A separate page on purpose: the tables in `docs/adapters.md` cover the six original vendor CLIs and
are checked by `tests/unit/test_docs_m3.py` / `test_docs_m19.py`. Each adapter has one real smoke
run (2026-11-19, "Smoke tier" at the end of this page); everything else here is from the vendor
documentation and synthetic fixtures.

| | `codex` | `claude` |
|---|---|---|
| Written against | Codex CLI 0.166.0 (`rust-v0.166.0`, 2026-10-29) | Claude Code 2.1.325 (CHANGELOG, 2026-11-16) |
| Command | `codex [-a never] exec --json --skip-git-repo-check --cd <worktree> [--model M] [--config model_reasoning_effort=E] <profile> [resume SID] -` | `claude -p --output-format stream-json --verbose [--model M] [--effort E] [--resume SID] <profile>` |
| Prompt | stdin (`-`), never argv | stdin, never argv |
| Efforts | low, medium, high, xhigh, max, ultra | low, medium, high, xhigh, max, ultracode |
| Models | user allowlist (`--allowed-model`, `--default-model`); no model listing | same (aliases such as `opus` or full ids) |
| `readonly` | `--sandbox read-only` | `--setting-sources user --strict-mcp-config --permission-mode dontAsk --disallowedTools Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch` |
| `edit` | unsupported | `--setting-sources user --strict-mcp-config`, `dontAsk`, `--disallowedTools Bash,WebFetch,WebSearch,Edit(./.git/**),Edit(./.claude/**)`, `--allowedTools Edit(./**)` (writes scoped to the workspace; an Edit rule covers Edit/Write/NotebookEdit) |
| `dev` | `--sandbox workspace-write` | unsupported (a bare `Bash` allow runs any shell command unsandboxed) |
| `elevated` | unsupported | unsupported |
| `unrestricted` | `--dangerously-bypass-approvals-and-sandbox` | `--permission-mode bypassPermissions` |
| Auth probe | `codex login status` -> chatgpt / api_key / none / unknown | `claude auth status` (`authMethod`) -> subscription / api_key / third_party / none |

Below `unrestricted` neither adapter skips approval checks. For claude, the listed denies win over allow rules in any settings file, no MCP server loads (`--strict-mcp-config`), and only user settings load, so a repo's `.claude/settings*.json`, `.mcp.json` or hooks cannot widen a profile. Other tools the user pre-approves in their own `~/.claude` (e.g. skills) are not blocked; claude `readonly` is a permission rule set, not an OS sandbox. Codex `--sandbox` covers codex's own shell and file tools only, not MCP servers or hooks: below `unrestricted` codex refuses a workspace when `.codex/` or `mcp.json` exists in it or in any directory up to the nearest `.git` root (Codex loads project layers from that root down to the cwd; the user's own `CODEX_HOME` is not a project layer) (the run fails as `spawn_failed`, its message starting with `codex_project_layer_present`), and MCP servers in the user's own `~/.codex/config.toml` are outside the guarantee, the same boundary as claude's `~/.claude`. A denied tool is recorded as
`usage.tool_denied` / `usage.denied_tools` and is not a failure (same rule as kiro).

Failure meanings: `auth_failed`, `quota_exhausted`, `rate_limited`, `network_error`, `run_error`,
`output_truncated`, `incomplete`; claude also `turn_limit`, `budget_exceeded`. Exit 0 with a meaning is
`vendor_error`; a non-zero exit takes the meaning as its reason (opt-in switch
`nonzero_exit_uses_stream_meaning`, not for `incomplete`).

Not installed: `adapter probe` reports `not_installed` with an install hint; a `tests/shims/`
tripwire on PATH counts as not installed.

Effort values: codex accepts `low|medium|high|xhigh|max|ultra`, claude `low|medium|high|xhigh|max|ultracode`. Other values are rejected at agent create/update, task create and scheduling (`EFFORT_NOT_ALLOWED`, reason `effort_not_supported_by_cli`), not only at spawn.

Smoke tier: both adapters have opt-in smoke cases (`tests/smoke/test_smoke.py`, classes `CodexSmoke` and `ClaudeSmoke`;
S-X-1..7 plus S-CODEX-8 / S-CLAUDE-8) under the `readonly` profile, with `MESH_SMOKE_MODEL_CODEX` /
`MESH_SMOKE_MODEL_CLAUDE` (never `auto`) and `MESH_SMOKE_EFFORT_CODEX` / `MESH_SMOKE_EFFORT_CLAUDE` (default `low`).
The harness feeds the prompt on stdin, as meshd does. `docs/adapters.md` ("Smoke tier for codex, claude and devin")
lists what each case can show.

Real runs (2026-11-19), with captures under `tests/fixtures/real/codex/0.166.0/` and
`tests/fixtures/real/claude/2.1.325/`:

- `tests/smoke/results/codex-0.166.0-2026-11-19.json` (model `gpt-5.6-luna`): S-X-1, S-X-2, S-X-3 and S-X-6 pass;
  S-X-4 and S-X-5 are `observed` (the stream names neither the model nor the effort). S-X-7 left no `x.txt` and
  stderr has `patch rejected: writing is blocked by read-only sandbox`. S-CODEX-8 left no `y.txt`; the stream has
  no command item (`commands` 0, `failed_commands` 0), only the model's message that the file system is read-only.
- `tests/smoke/results/claude-2.1.325-2026-11-19.json` (model `ds-flash`: DeepSeek through an Anthropic-compatible
  proxy, not an Anthropic model; stderr has `[claude-code:unrecognized_model]`): S-X-1, S-X-2, S-X-3, S-X-4 and
  S-X-6 pass; S-X-5 is `not_applicable`. S-X-7 and S-CLAUDE-8 left no `x.txt` / `y.txt` with 0
  `permission_denials`: the readonly deny list removes Bash, Edit and Write from the session, so there was no tool
  call to deny. In both captures the model handed the task to an `Agent` subagent, which did not create the file
  either. `Agent` is not on the readonly deny list.

With these files both adapters pass the strict launch rule (S-X-1 and S-X-6 pass at the probed version).
