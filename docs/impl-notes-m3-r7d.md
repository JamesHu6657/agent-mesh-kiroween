# m3-r7d round 2: registered test-daemon cleanup (ACC: N25)

## Scope and design

Replaces round 1's `/proc` command-line scan rather than repairing its pattern.
Only `MeshHarness.start_daemon` opts processes into cleanup. There is no age rule,
command-line inference, process-group signal, or global process enumeration.

- The harness publishes `<pid>.json` using a mode-0600 temporary file and
  `os.replace` in a mode-0700 registry, default `/tmp/mesh-test-daemons`.
  `MESH_TEST_DAEMON_REGISTRY` overrides it for isolated tests.
- Records contain `pid`, Linux stat field-22 `starttime`, canonical `home`, and
  `suite_id`. Registration requires an existing, non-symlink `/tmp/mh-*` direct
  child. Nested homes, path traversal, empty prefixes and other locations fail
  closed. Registry errors are logged to stderr and do not fail harness calls.
- A suite exports a fresh random `MESH_TEST_SUITE_ID` and holds an exclusive
  flock on its `suite-<id>.lock`. An unlocked/missing lock means dead ownership.
  Without suite ownership, a record uses `adhoc-<test-process-pid>` plus
  `suite_starttime`; both the PID and starttime must still match a non-zombie.
  Runner lock setup failure falls back to this adhoc ownership.
- A **live suite always wins**, including this runner's own suite and a suite
  whose home disappeared. This is the explicit "never reap live suites"
  interpretation of the approved design's otherwise overlapping conditions.
  A dead owner's records can be reclaimed whether their home exists or not.
- Cleanup first opens a pidfd, then checks the current stat identity. Reused
  PIDs only lose the stale record. All signals use that same pidfd: SIGTERM,
  at most five seconds of polling, then SIGKILL and at most five more seconds.
  No bare-PID signal or fallback is used if pidfd support fails.
- Logs distinguish `terminated`, `killed`, `gone`, `pid_reused`, `skipped_live`,
  and errors/still-running processes. Confirmed exits and stale identities
  remove their records; malformed records and failed cleanup remain visible.
- Harness stop, crash-wait, and confirmed kill paths remove their records only
  after waiting for exit. Restart discards the previous exited daemon's record.
- The POSIX runner installs an EXIT trap, saves the original status, unlocks
  ownership, runs cleanup with `timeout 30`, and exits with that same status.
  Startup cleanup is also bounded to 30 seconds. `set -e` and both existing
  tripwire checks are unchanged. Lock files are retained, avoiding unlink/relock
  races; the small unlocked files are not evidence of a live suite.

## Linear finding -> fix

Source: `docs/coordination/reviews/linear-m3-r7d.md` (REVISE).

| Finding | Fix / answer | Evidence |
| --- | --- | --- |
| MAJOR 1: flattened argv permits spoofed module calls or single-argument matches | Delete argv discovery completely. Only atomically registered process identities are eligible. | Both spoofed-argv forms remain alive with no record; an empty isolated registry never opens a pidfd. |
| MAJOR 2: textual `/tmp/mh-` prefix, symlinks, traversal and ordinary user homes | Registration requires the exact canonical, direct-child path, rejects symlinks, and records the process explicitly. Reaping validates the home and record again. Registries, records and suite locks also reject symlinks. | Registration rejection tests cover symlink, nested, non-matching and traversal paths. Pure fixtures also reject `/var/tmp/mh-*`. Registry-symlink rejection is exercised on this host. |
| MAJOR 3: bare PID and scan-to-signal PID reuse races | Open a pidfd before checking stat starttime, then signal/poll only that fd. Mismatches drop the record without signalling. | Real spawned child with mismatched starttime survives; signal spy verifies no call. TERM/KILL escalation uses the identical pidfd. |
| MAJOR 4: unit tests globally scan/kill concurrent daemons with a lower age threshold | All reaper invocations use a temporary registry, either an explicit argument or the injected environment. Every kill target is a subprocess spawned by that test. Policy uses fixture records/stat text. | Rewritten isolated test module; no global `/proc` enumeration and no age-threshold API. |
| MINOR 1: trailing cleanup misses failures/tripwire exits, lacks timeout | EXIT trap releases ownership and runs bounded best-effort cleanup while preserving the incoming status. | Tests extract the real trap body: a `set -e` failure still reaps its registered child, releases its flock, skips subsequent commands, and returns 7. Simulated timeout returns preserve both 0 and 7. |
| MINOR 2: TERM-only, no confirmation/escalation, misleading kill log | Poll pidfd for confirmed exit; escalate TERM-resistant children via the same fd; report actual outcomes and keep records on error. | TERM child exits with `-SIGTERM`; ignoring child exits with `-SIGKILL`; permission-error fixture stays alive with its record and an error, not a success log. Signals intentionally target only the registered daemon, not an unverified subtree. Existing harness run-process teardown is unchanged. |
| Additional age/coverage discussion: long-running live smoke/e2e suites could be killed | Remove the three-hour rule entirely. Locks/adhoc process identity, not elapsed time, determine ownership. | Oldest-starttime policy fixture, live-suite lock test (including own suite and missing home), adhoc owner exit/reuse fixtures. The old four tests were rewritten, preserving their relevant safety assertions and replacing the unsafe age expectation with the required live-owner guarantee. |

## Verification

New checks live alongside the existing unit tests in
`tests/unit/test_reap_daemons.py`; no temporary test scripts were added.

- `python3 -m unittest tests.unit.test_reap_daemons -v`: **25 passed**, 0.486 s
  before the full-suite run. The first run had one failing registry-symlink
  assertion: this gVisor host did not reject that directory using `O_NOFOLLOW`
  alone. Added an explicit `lstat` type check; rerun passed without weakening
  the assertion. Records and suite locks have equivalent explicit checks.
- Existing shell check `sh -n scripts/test.sh`: passed.
- Existing patch formatting check `git diff --check`: passed before full suite.
- Existing full runner `bash scripts/test.sh`: **passed, exit 0**, one actual
  full run. **716 tests passed** (394 unit / 273 integration / 49 recovery).
  Tier timings: 34.330 s / 473.544 s / 44.723 s.
  Fresh-process wall time including EXIT cleanup: **553.228 s**; the runner
  printed `Total wall time: 553s`.
  `/usr/bin/time` is unavailable here, so a Python `time.monotonic()` wrapper
  measured the real runner instead. The failed timing-tool launch never
  started the suite.
- Full-run reaper output, exactly two lines, at startup and EXIT respectively:

  ```text
  reap-daemons: checked records=0
  reap-daemons: checked records=0
  ```

  There were no registration/reaper errors or records needing cleanup at
  either boundary. This does not claim that unregistered historical daemons
  were discovered; deliberately only registry records are inspected.

The reaper deliberately cannot discover pre-registry leaks or other launchers'
unregistered processes. Malformed records are retained for diagnosis. It does
not weaken daemon/run assertions or attempt unrelated PID-file cleanup changes.

## Round 3 fixes (review r7d3)

- **F1 (P1) `mesh daemon start` daemons leaked after teardown.** `MeshHarness`
  now records `_created_ticks` (boot clock ticks, `/proc/uptime` floored, taken
  *before* the home directory is created). `stop_pidfile_daemon()` keeps the
  exact-identity path for daemons it spawned; for any other pid in
  `run/meshd.pid` it calls `reap_daemons.stop_verified_since(pid, created, home)`:
  pidfd opened first, stat read after pinning, the process must have started at
  or after `_created_ticks` (else `pid_reused`, no signal), must not be a zombie,
  and must run for exactly this home (canonical `--home <home>` / `--home=<home>`
  argv element or `MESH_HOME=<home>` environ entry); TERM then KILL go through
  the same fd. Never a bare kill. The home must pass `valid_home()`.
  Regression: `tests.integration.test_daemon...test_daemon_start_daemon_stopped_by_teardown`
  (real `mesh daemon start`, teardown, then a /proc scan asserts no non-zombie
  process has the home in its argv). Unit: `TestPidFileDaemonWithoutIdentity`.
- **F3 (P3) prefix home.** `home_matches` compares whole argv/environ elements
  after `realpath`; `test_prefix_home_never_matches` covers `/tmp/mh-x-other`
  vs `/tmp/mh-x` for both argv and `MESH_HOME`.
- **F2 (P3) legacy records.** Registry files named `<pid>.json` (pre-generation
  format) are logged as `malformed` by `reap_daemons.py` and **kept** on purpose
  (never signalled, never deleted). To clean them by hand: check the pid is not
  a meshd you still need (`cat /tmp/mesh-test-daemons/<pid>.json`, compare
  `/proc/<pid>/cmdline`), stop it yourself if it is a leftover, then
  `rm /tmp/mesh-test-daemons/<pid>.json`. New records are `<pid>-<starttime>.json`.
- **F4 (P3) signal traps.** Under dash, the HUP/INT/TERM traps in
  `scripts/test.sh` run only after the current tier's foreground command
  exits (the shell waits on the foreground child). Cleanup still runs and the
  exit code is 129/130/143. Deferred, not changed: running tiers in the
  background + `wait` would change output ordering and `set -e` semantics; a
  second signal during `cleanup_suite` may skip the lock `rm`; the stale-lock
  pruning (unheld and > 1 h old) removes such leftovers later.

## Round 4 fixes (Linear r7d4 + Quest r7d4)

- **MAJOR: relative home resolved against the reaper's cwd.** `home_matches`
  canonicalised `--home h`, `--home=h` and `MESH_HOME=h` with `realpath()` in the
  reaper's own cwd, so a daemon of another home (same relative string, other cwd)
  could match. Now any relative value (not starting with `/`) never matches, in
  all three forms; harness homes are always absolute. Tests run real children in
  another cwd (`test_relative_home_never_matches_*`).
- **MINOR: 1 s start-time slack.** Replaced by a credential: each `MeshHarness`
  generates `secrets.token_hex(16)` and `get_env()` puts it in
  `MESH_TEST_HARNESS_TOKEN` for every process it launches (Popen'd meshd and all
  CLI calls, so `mesh daemon start`'s detached daemon inherits it;
  `mesh/cli/commands/daemon.py` passes `dict(os.environ)`, verified with the real
  CLI). The fallback is now `reap_daemons.stop_verified_token(pid, token, home)`:
  pidfd pinned first, then non-zombie, exact absolute home, and an exact
  `MESH_TEST_HARNESS_TOKEN=<token>` environ entry, all read after pinning;
  otherwise no signal (`home_mismatch` / `token_mismatch`). The start-time marker
  (`boot_ticks`, `_created_ticks`, `stop_verified_since`) is removed: it needed
  slack on gVisor and added nothing once the token is required. Processes that
  inherited the token (the harness's own descendants for this home) may be
  stopped at teardown; that is intended.
- **Quest P3s.** Negative prefix tests for `--home=` and `MESH_HOME`; a zombie
  test (no signal, `gone`); the docstring no longer claims a reused PID "can
  never match" by time; `scripts/test.sh` documents that signals ignored on
  entry (`nohup … &`, or any `… &` from a non-interactive shell) cannot be
  trapped; `trap - INT` in such a shell does not undo it (seen on base: a
  backgrounded full run failed `test_signal_traps_run_cleanup_under_dash` for
  SIGINT). Run the suite in the foreground or a tmux pane. Legacy `<pid>.json` records: see F2
  above for manual cleanup (still kept, never signalled).
