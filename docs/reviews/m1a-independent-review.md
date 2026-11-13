# M1a independent acceptance: /workspace/mesh-fresh-v1

Reviewer: Worker 2 (independent executor subagent). Time: 2026-11-07, 16:50–17:05 (UTC+8). HEAD reviewed: `207a21d`.
Scope: I read the code and docs and ran isolated tests only. I modified nothing in the repo, invoked no real vendor CLI (the shims stayed on PATH and the tripwire stayed empty), and did not start M1b. All my temp homes were under `/tmp/mr*`. Each was cleaned up, and no meshd processes are left running.

## Verdict: **REVISE**
The report's claim of 46/46 PASS does not hold. At least 3 gate items fail (**D8, D9, N1**). There is also one MAJOR usability/security bug outside the checklist: `daemon start --user` locks the user out of their own account. Most other items do hold up under black-box testing (see the list at the end).

## Reproducibility (verified)
- `bash scripts/test.sh` ran twice, with rc=0 both times. Results: unit 69, integration 26, recovery 1, for a total of **96** (2 of them are placeholders). Wall time was about 34 s.
- After the runs, `/tmp/mh-*` and `/tmp/mesh-tripwire*` were absent, no `-m mesh` processes were left, and `git status` was clean.
- Fixtures: I recomputed the `sha256sum` of all 12 files against the **plan §14.11 table itself**, not the test's copy. All 12 match.
- `git diff 581ec51..HEAD -- docs/plan.md docs/acceptance-checklist.md docs/requirements.md docs/review-r*.md tests/fixtures` is empty, so the protected plan and fixtures are unchanged since review r3.

## Findings (blocking first)

**F1 MAJOR — `mesh daemon start --user` locks the user out on a fresh home.**
- Cause 1: `mesh/cli/commands/daemon.py:70-74,100-102` calls `execute_cmd` (with auth) on every 0.1 s poll.
- Cause 2: `mesh/client.py:58-63` `os.dup`s the passphrase fd and reads it to EOF. After the first read, every request sends an empty passphrase.
- Result: 5 `auth.failed` events within about 0.7 s, then `LOCKED_OUT` for 5 minutes. The start command also wrongly returns rc 10 even though the daemon is running.
- Repro: `init` followed by `bin/mesh --home $H --user --passphrase-fd 3 daemon start 3<$P` gives rc=10 ("locked out"). A following `whoami --user` gives rc=3 `LOCKED_OUT`.
- Why the tests miss it: the harness (`tests/helpers.py:142-160`) only uses `daemon run`.
- Related risk: on the tty path this would prompt repeatedly. An empty passphrase also counts toward the lockout.

**F2 FAIL D8/D9 — JSON input path for concurrency.**
- `echo '{"max_concurrency":"2"}' | mesh agent create --name k1 --adapter fake ... --input - [U]` gives **rc 0, agent created**. D8 requires rc 2 `INVALID_CONCURRENCY` and no row. Cause: `mesh/core/policy.py:51-74` accepts any `str`, and nothing tracks whether a value came from a flag or from JSON.
- JSON `2.0` gives **rc 1 `INTERNAL_ERROR`** ("Floats are forbidden in canonical JSON"). The idempotency hash `mesh/daemon/server.py:540` runs before validation.
- A pure `--input -` body (name, adapter, … in JSON) gives rc 2 `INVALID_ARGUMENT` "required: --name, --adapter". Argparse's required flags (`mesh/cli/main.py:195-252`) block it, so D8's JSON cases and D9's "as JSON integers" cannot be exercised at all.
- Why this looks like a pass: the tests only use the flag path (`tests/integration/test_agents.py:146-192`). `tests/unit/test_settings.py:125` even asserts that the string `"7"` is valid. The docstring at `policy.py:18` claims JSON `"2"` is rejected, but the code does not do that.

**F3 FAIL N1 (risk for N7/N8) — the probe is hard-coded and never inspects the binary.**
- Plan §14.1 L1033 says probe is "read-only: --version, --help, model list". Instead, `mesh/adapters/base.py:179-197`, `agy.py:131-162`, `kiro.py:158-186` and `vibe.py:102-130` return constants.
- `adapter list` in non-testing mode shows versions `droid 1.3.0` (planned constant), `agy None`, `kiro None`, `vibe None`. N1 requires probe versions, e.g. agy 1.5.2.
- N8 `stale` can never trigger in production because `probed_version` is never supplied. Only the unit test passes a value.
- `list_models` reads `tests/fixtures/...` at runtime (`kiro.py:140-147`, `droid.py:108-116`). This is acceptable for M1 offline work, but it is not a CLI probe and breaks if the tests are not shipped.

**F4 MEDIUM — authz matrix deviation.**
- `mesh/daemon/server.py:447-463` allows **unauthenticated** `config.list/get` and `agent.list/show`, and agent tokens can call `config list`.
- Plan §7.3 L489 says `config.*`, `agent.*` are user-only (direct), and agent callers get rc 4.
- Repro: `bin/mesh --home $H config list` gives rc 0, and `agent list` gives rc 0 with full agent configs.
- The mutating paths are correctly denied, which I verified: agent tokens get rc 4 on update/create/token issue/list/disable/config set/daemon stop, including with `--relayed`.

**F5 MEDIUM — config validation gaps (§5.7).**
- `config set task.default_timeout_s 600.0`, `1e3`, and `NaN` give rc 1 `INTERNAL_ERROR` instead of 2 `INVALID_SETTING` (same root cause as `server.py:540`).
- `lead.default_agent '"nobody"'` and `'"fx"'` (fx has no `can_lead`) give rc 0. §5.7 requires an "enabled agent with can_lead", but `mesh/core/settings.py:91-105` checks only the type.
- B13's literal checks pass.

**F6 MINOR — mitigation 8: caller PYTHONPATH is prepended.**
- `bin/mesh:14-18` sets `PYTHONPATH=<root>:$PYTHONPATH`, but plan L70 and §7.6(8) say PYTHONPATH should be the install root only.
- A caller `PYTHONPATH=/tmp/evil` containing `argparse.py` is executed by `bin/mesh` (shadow hit confirmed).
- What works: a cwd `mesh/` or `json.py` is correctly ignored thanks to `-P`, and `build_external_env` is correct.
- What's untested: `build_external_env` has no production caller in M1a. meshd's env had `PYTHONPATH=<root>:<root>` (duplicated, harmless).

**F7 MINOR (others)**
- Requests over 64 KiB give rc 1 `INTERNAL_ERROR`, because the asyncio readline limit is hit and the 1 MiB check at `server.py:514` is unreachable. The daemon survives.
- Unknown daemon commands always report `milestone:"M1b"` (`server.py:639-646`).
- `doctor --check-invariants` returns rc 0 on a DB with schema_version 999.
- The startup install-path check (`server.py:114`) ignores a configured `workspaces.root`.
- `--input` accepts `{"token_file":…}` because the allow-list is `vars(args)` (`main.py:228`).

**Test-quality gaps (labels without proof)**
- A1 `test_test_runner_script` (`test_skeleton.py:144-153`) only greps the script text. A3 `test_tripwire_shims` (`:264`) only checks that the shims fire.
- The suite-level tripwire in `scripts/test.sh` is effectively unused, because `MeshHarness` overrides `MESH_TRIPWIRE_FILE` per harness (`helpers.py:33,52`). The per-test teardown assert does hold.
- The probes never execute a binary, so an "empty tripwire" proves little.
- A10 has no corrupted-I7 unit variant. I verified I7 by black box: a tampered payload, a tampered middle field and a deleted middle row are all detected, after I dropped the append-only triggers in a temp copy. Truncating the tail is not detected, as expected for a hash chain alone.

## Independently verified as holding (black-box, my own /tmp scripts)
- **C1:** no TCP/UDP sockets for the meshd pid (`ss -tunap`), and a Unix socket at `$H/run/mesh.sock`.
- **C3:** permissions are 700/600.
- **C4:** a 755 home gives rc 2 `INSECURE_PERMISSIONS` with no socket, and `--fix-perms` restores it.
- **C5:** a second `daemon run` gives rc 1 `DAEMON_ALREADY_RUNNING` and the first daemon keeps serving.
- **C6:** the unit test drives the real `handle_client` with a mismatched uid; the connection is closed and readline is never called.
- **C9:** the DB holds only the lookup plus a 32-byte hash, and `token list` shows no secret.
- **C10/C12:** a 0644 file, a file under workspaces, a file under runs, and a symlink to a workspace token all give rc 3 `TOKEN_FILE_INSECURE`.
- **C11:** `"auth"` does not appear in the log. The passphrase and both tokens (full and secret part) appear nowhere under `$H`.
- **C14 forward:** install under `$H/workspaces/inst` gives rc 2 with `install_root`/`conflict` and no socket, and doctor reports it.
- **C14 reverse:** a home inside the install is rejected (init and run). `workspaces.root` inside the install, or containing it, gives rc 2 `INSTALL_PATH_INSECURE`.
- **B2, B7, B8, B9, B10:** pass.
- **D1, D2, D5, D10, D17:** pass.
- **G17:** stdin passphrase gives rc 2. `MESH_PASSPHRASE` with `setsid` and no tty gives rc 3 `NO_TTY`.
- **I9:** rc 1, and the DB bytes and mtime are unchanged.
- **N2/N3:** pass.
- **Stubs:** verbs not yet built return rc 2 `NOT_IMPLEMENTED_IN_M1` with the correct M1b/M1c/M2 milestones.
- **Lockout:** after 5 bad passphrases, even the correct one gets `LOCKED_OUT`.
- **Concurrency:** 12 parallel same-name creates give exactly 1 success (others rc 6). 10 parallel `--if-version` updates give exactly 1 winner. 10 parallel config sets are all rc 0.
- **Restart:** a restart after SIGKILL works.
- **Static checks:** the greps for A2, A4, A5, A7 and C2 are clean.

## Needed before re-review
1. Fix F1.
2. Fix F2: distinguish JSON from flag strings, validate before canonical hashing or map floats to rc 2, and make pure `--input -` work.
3. Fix F3: make the probe run `--version`/`--help` read-only, testable via shims or `binary_path`.
4. Fix F4 and F5.
5. Add regression tests that drive the real CLI path for each of the above.
F6/F7 can be tracked separately.
Repro scripts: `/tmp/m1a-review/{bb,t2,t3,t4,t5}.py`. These are temporary and safe to delete.
