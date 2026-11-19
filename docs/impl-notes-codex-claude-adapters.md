# Codex CLI and Claude Code adapters (feat/codex-claude-adapters)

Draft PR description. Branch from main `c86d703`.

## Files

| File | Change |
|---|---|
| `mesh/adapters/vendor_stream.py` | new: shared JSONL reading, log-cap / cut-line detection, error-text classification, shim-aware PATH lookup |
| `mesh/adapters/codex.py` | new: `CodexAdapter` |
| `mesh/adapters/claude.py` | new: `ClaudeAdapter` |
| `mesh/adapters/registry.py` | register `codex`, `claude` after `vibe` |
| `mesh/daemon/supervisor.py` | core change, opt-in switch (below); lands via PR #48 into this branch |
| `tests/shims/codex`, `tests/shims/claude` | tripwires, same content as `tests/shims/droid` |
| `tests/fixtures/synthetic/{codex,claude}/*.jsonl` | hand-written streams |
| `tests/unit/test_adapters_codex.py`, `test_adapters_claude.py` | adapter unit tests |
| `tests/unit/test_nonzero_stream_meaning.py` | switch tests |
| `tests/integration/test_codex_claude_launch.py` | meshd end to end with stub binaries |
| `tests/integration/test_adapters.py` | `expected_ids` gains codex, claude; both `not_installed` when no binary resolves (PR #48) |
| `docs/adapters-codex-claude.md` | separate doc page |

## Core change and reason

`_vendor_nonzero_failure` read only stderr. Codex and Claude Code print auth / quota / network
failures in their stdout stream and exit 1, so without a change those runs failed as
`exit_nonzero` with no usable reason. The change: when the adapter has
`nonzero_exit_uses_stream_meaning = True` (codex and claude only), a non-zero exit takes the parsed
`vendor_exit_meaning` as its reason if it is in `_NONZERO_STREAM_MEANINGS`
(`incomplete` is not, so stopped or killed runs keep `exit_nonzero`). The stderr refinement still
runs and lands in `stderr_error`. No other adapter sets the attribute, so their behaviour and
tests are unchanged; `test_nonzero_stream_meaning.py` checks both sides.

## Models and parameters

Both: model from the agent allowlist (`--allowed-model`, `--default-model`), no model list command.
Efforts and profiles: see `docs/adapters-codex-claude.md`. No `--option` keys; `--binary` works.

## Sources (checked 2026-11-16)

Codex 0.166.0: developers.openai.com/codex/cli/reference, non-interactive mode docs,
codex-rs/exec/src/cli.rs, config reference (`model_reasoning_effort`), openai/codex#22570
(a real no-credential stream). Claude Code 2.1.325: code.claude.com cli-reference, headless,
permission-modes, Agent SDK message types, CHANGELOG (npm `latest` still showed 2.1.260).

## Tested only with fake data, needs a real machine

- Codex run with ChatGPT login; API-key run; exact `codex login status` wording.
- Codex usage-limit and network wording; `--ask-for-approval never` placed before `exec`.
- Claude: prompt from stdin without a positional query; `dontAsk` denials in `-p` mode; the exact
  not-logged-in / limit result text; `apiKeySource` values.
- SIGTERM / process-group stop with a real CLI (terminate.run_targets is unchanged and generic).
- Real smoke captures (S-X-1, S-X-6) and help fixtures: none yet.

## Known limitations

- Prompts over 100 KB fail as `prompt_too_long` (not in `FILE_PROMPT_ADAPTERS`), even though both
  read stdin. Kept as agreed.
- codex: `edit` and `elevated` unsupported; claude: `dev` and `elevated` unsupported (dev needs a scoped Bash rule set first).
- No smoke evidence, so a strict (testing) daemon cannot launch them without staged evidence; warn
  mode launches with an evidence warning. `tests/smoke` harness dispatch is not wired yet.
- Resume: flags emitted, capability `unknown` until verified.
- `help.py` / `agent` CLI help strings not updated.
- Shims pushed through the API are mode 644; `resolve_on_path` hides the real binary anyway, but
  run `git update-index --chmod=+x tests/shims/codex tests/shims/claude` so they also act as
  tripwires.

## Review round 1 fixes (PR #49)

- claude `dev` refused (bare `Bash` allow = unsandboxed shell); readonly/edit add explicit `--disallowedTools` and `--setting-sources user` so settings files and project hooks cannot widen them.
- Failure meaning: codex no longer falls back to a recovered `Reconnecting...` notice; claude uses only the last main-conversation assistant error, treats a retry as network only when no clean reply followed, and no longer classifies `result.errors[]` (kept in usage).
- Error-text markers tightened: no bare `429`, `unauthorized`, `timed out`, `transport`.
- `stream_facts` splits on `\n` only (U+2028 inside JSON); claude `logged_in` false when the method is unknown; `bounded()` redacts before cutting.

## Review round 2 fixes (PR #49)

- claude readonly/edit also pass `--strict-mcp-config` and deny `WebSearch`; docs say plainly which tools the denies cover.
- claude: any clean main-conversation assistant event (not only text) clears earlier retries and assistant errors.
- A half-written last line alone is `incomplete` (killed mid-write), not `output_truncated`; only runwrap's log cap is truncation.
- codex r1 regression tests moved above `unittest.main()` and clean their temp dirs.

## Review round 3 fixes (PR #49)

- codex fails closed below `unrestricted` when the workspace has `.codex/` or `mcp.json` (a trusted repo's project layer can start MCP servers/hooks outside `--sandbox`). Long-term option after a real-machine check: per-run `projects."<root>".trust_level="untrusted"` override or an auth-only `CODEX_HOME`.
- Comments now say only runwrap's log cap is `output_truncated`; codex `--help` timeout reports `timeout`.

## Gemini blind review fixes (PR #49, head 8fe755b)

- claude `edit`: allow is path-scoped `Edit(./**)` (cwd = task workspace) instead of bare Edit/Write/NotebookEdit; `.git/` and `.claude/` writes denied. Rule syntax per Claude Code permission docs; real-machine check still pending.
- codex: project-layer check walks from the workspace up to the nearest `.git` root (skipping CODEX_HOME), so a parent trusted repo's `.codex/` is caught.
- Effort: `Adapter.supported_efforts` (codex/claude) checked by `policy.check_adapter_effort_values` at agent create/update, AG step 5 and T6; `ultra`/`ultracode` added to the profile/effort swap hints.
- claude stderr: only commander usage errors are `cli_usage_error` (not any "error: invalid").
