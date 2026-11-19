# Codex CLI and Claude Code adapters

A separate page on purpose: the tables in `docs/adapters.md` cover the six original vendor CLIs and
are checked by `tests/unit/test_docs_m3.py` / `test_docs_m19.py`. These two adapters have no real
fixtures or smoke evidence yet, so nothing here counts as verified.

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
