# Implementation notes: m3-r4 (m3-wrapfix)

Branch `wt/m3-r4` from main `d1a0809`. Items: N18, A8, N19 (lead clause).

## Sub-step 1: seen map for adopted runs (N18)

- `mesh/daemon/supervisor.py`
  - `is_adopted(run)`: a run whose wrapper is not in `_WRAPPERS`. Every wrapper this meshd
    spawned (task run via `_spawn_run`, lead turn via `register_wrapper`) is there until it
    is reaped; an adopted wrapper (another boot's, now init's child) never is.
  - `track_adopted_seen(run)`: called from `_watch_one` on every tick with a live wrapper.
    For adopted runs with a pgid it scans at the first tick (adoption) and every
    `SEEN_SCAN_EVERY_TICKS = 5`th tick. A scan only counts if `wrapper_alive(run)` still
    holds after it (the wrapper is the session leader, so its sid/pgid could not be reused
    during the scan). Members that died or whose starttime changed are dropped; new ones
    are merged in. The file is rewritten only when the map changed (or does not exist yet).
  - `seen_for(run)`: in-memory map, loaded from `seen.json` on first use (so a second
    restart after the wrapper exited still has it); `{}` for runs this meshd spawned, so
    non-adopted runs behave exactly as before.
  - `_watch_one`: passes `seen_for(run)` to `terminate.run_targets` (the leftovers scan
    after the wrapper ended) and to `_begin_termination` (both the leftovers path and the
    live-wrapper termination path). `_evict_seen` drops state for runs no longer active.
- `mesh/daemon/terminate.py`
  - `begin(..., seen=None)`: pre-seeds `Termination.seen` with the entries still alive at
    the same starttime.
  - `_session_anchored`: a seen member with matching starttime anchors the group if its
    `session == pgid` **or** `pgrp == pgid` (a pgid cannot be recycled while a member of
    the group lives). `group_trusted` uses it unchanged; `run_targets` already accepted a
    `seen` argument.
- Lead turns (`_watch_lead_turn`) are not changed: the plan names `_watch_one` only.
  Adopted lead turns therefore still have the pre-r4 behaviour (follow-up if wanted).

### `seen.json` format

`<run_dir>/seen.json`, mode 0600, written to `seen.json.tmp.<meshd pid>`, fsynced, then
`os.replace`d:

```json
{"members": [{"pid": 4242, "starttime": 123456}], "pgid": 4240, "run_id": "run_...",
 "updated_at": "2026-11-09T12:00:00Z", "wrapper_pid": 4240, "wrapper_starttime": 123400}
```

Only `members` is read back; the other keys are diagnostics.

### Scan cost

One `os.listdir("/proc")` plus one `/proc/<pid>/stat` read per process, per adopted run,
every 5 watch ticks (the watch loop runs about once per second), so about one /proc walk
per adopted run every 5 s, and no `environ` reads. Runs spawned by the current meshd never
scan. Adopted runs exist only after a meshd crash/restart, so the steady-state cost is zero.

## Sub-step 2: RunRequest options (plan 14.3) - skipped

Already done by m2-r11/r11b: `supervisor.build_run_options(agent, run_id)` builds
`{**adapter_options, "run_id": run_id, "binary": binary_path (if set)}`, so run id and the
configured binary win over stored adapter options. It is used at T5
(`supervisor._spawn_run`, `RunRequest(options=build_run_options(agent, run_id))`) and at
lead-turn start (`lead_turns.commit_lead_turn`). Covered by
`tests/unit/test_adapters.py::test_daemon_run_options_and_resume_verification` (droid argv[0]
is the agent binary, `--log-group-id <run id>`) and
`TestDaemonRunOptionGuards.test_adapter_options_cannot_override_run_id_or_configured_binary`.
No gap found; `tests/unit/test_runrequest_options.py` not added.

## Sub-step 3: vendor lead refusal (N19 lead clause)

- `mesh/daemon/lead_turns.py::lead_launch_check`: after the m2-r10 checks (test double
  outside testing mode, `launch_enabled`), an adapter with `kind == "vendor"` raises
  `vendor_lead_refused(adapter)`: rc 13 `ADAPTER_UNSUPPORTED`, `details`
  `{"milestone": "M3", "reason": "vendor_lead_m3_lite"}`. `lead_launch_allowed` inherits it.
- `_record_turn_refused` uses `details.reason` when set, else the old code map, so the
  event is `lead.turn_refused {reason:"vendor_lead_m3_lite"}`, still once per assignment;
  messages stay `pending` (no turn planned).
- Ordering: the vendor rule runs after the launch gate, so a vendor adapter that is not
  launch-enabled keeps the m2-r10 reason `adapter_not_launch_enabled` and
  `test_lead_actions.py::test_vendor_launch_lead_refused` is untouched. Either way no turn.
- Side effect: `handlers/project.py` calls `lead_launch_check` for `--lead`/`set-lead`, so a
  launch-mode vendor lead that becomes launch-enabled (after m3-r5's evidence gate) is also
  rc 13 there, with `details.reason:"vendor_lead_m3_lite"`. Consistent with M3-lite; not
  reachable on this branch (no vendor adapter is launch-enabled here).
- Test: `tests/integration/test_vendor_lead_gate.py`. Evidence is simulated by a droid
  subclass with `launch_enabled = True` patched into `lead_turns.get_adapter`, driving
  `plan_lead_turn` against a harness DB (inbox lead, so meshd's tick never races it). The
  plan's daemon-level variant with `MESH_TEST_SMOKE_RESULTS_DIR` needs m3-r5's
  `mesh/adapters/evidence.py`, which is not on main; deferred to the m3-r5 merge.

## Sub-step 4: tests (N18, A8)

- `mesh/testing/fake_agent.py`: behaviour `spawn_envless_member {path, trigger, timeout_s}`:
  `python3` child (`sys.executable`), `env={}`, same group and session, SIGTERM ignored,
  blocks on `os.read` of its own pipe; pid written to `path`; then the step waits for
  `trigger` (50 ms poll, like `wait_release`).
- `tests/recovery/test_recovery_adopt_seen.py`:
  - `test_adopted_wrapper_exit_with_envless_member` (ACC: N18, A8): grace 2 s, kill -9,
    restart, adopted; `seen.json` lists the member; trigger -> fake and wrapper exit; while
    the member lives (sampled every 0.1 s, including after `run.leftovers_terminated` = the
    SIGTERM/grace window) the run stays open and `task:<id>` is held; the member dies
    (SIGKILL), lease released, run closed, `doctor` ok (plus harness teardown A8).
  - `test_adopted_wrapper_exit_without_member_closes_normally` (control).
  - Not `@serial`.
- Fails on old code: with HEAD `supervisor.py`/`terminate.py` (seen check bypassed) the
  member is never killed: the run closed and the lease was released while it lived.
  `test_vendor_lead_gate` fails on HEAD `lead_turns.py` (2 failures).

## Assertions changed

None.

## Suite runs (`bash scripts/test.sh`, rc 0, both tripwire files absent each time)

| Before commit | unit | integration | recovery | wall | box |
|---|---|---|---|---|---|
| 5f9ac08 (N18 fix) | 283 OK | 253 OK | 28 OK | 489 s | idle at start (another worktree's suite started during the run) |
| 012c7fb (N19) | 283 OK | 255 OK | 28 OK | 485 s | idle |
| test commit (N18, A8) | 283 OK | 255 OK | 30 OK | 492 s | idle |

Baseline before changes was not timed separately; main's suite is the first row minus the
new code (same test count).

## r4b (fixes for the external review of 7666005)

### Item 1 (MAJOR): the `lost` path honours seen.json

`recovery._lost` (wrapper.json present but the wrapper dead without exit.json, or no
wrapper identity) now loads the run's persisted members (`supervisor.seen_for`) for task
runs with a pgid. If any recorded member is alive with its recorded starttime, recovery
starts `terminate.begin(run, pgid, wrapper_pid, end_reason, grace, wrapper_starttime,
seen=<live members>)` (`_terminate_group`) instead of closing: `_session_anchored` keeps
the group trusted, the termination targets the members (and the rest of the anchored
group plus any MESH_RUN_ID carriers), the run stays open and the lease is held (re-owned by
the new boot id) until the release condition holds; then the supervisor closes it `lost`
(`lost_on_restart`) as for any recovery termination. Before r4b only MESH_RUN_ID carriers
were looked at, so an env-less member was orphaned and the run/lease closed at once.
Lead turns are unchanged (no seen tracking for them, as in r4).

Test: `test_second_restart_wrapper_lost_member_alive` (ACC: N18, A8): adopt after restart 1,
seen.json lists the member, kill -9 meshd, SIGKILL the wrapper and the fake agent (no
exit.json, no carrier), restart 2: not re-adopted; while the member lives the run is open
and `task:<id>` held; meshd SIGKILLs it after `kill.grace_s=2`; lease released, run `lost`,
`doctor` ok.

### Item 2 (MAJOR): failed seen.json writes are retried

`_persist_seen` wraps `_write_seen`; a failure is logged once to meshd.log ("seen.json write
failed ...; retrying every tick") and puts the run in `_SEEN_DIRTY`. `flush_seen` retries
on every watch tick (non-scan ticks of `track_adopted_seen`, a scan whose wrapper died
mid-scan, and the wrapper-exit path of `_watch_one`) until a write succeeds (logged, flag
cleared), with no membership change needed. Test: `TestSeenState.test_failed_write_is_retried_without_membership_change`.

### Item 3 (MINOR): seen.json validation and the exact rule

`supervisor.load_seen_state(run)` returns one of:

- `absent` (no file): **no members recorded** - behaves exactly as r4 (empty map; trust only
  from a live wrapper identity or MESH_RUN_ID).
- `ok`: the file is a JSON object with `run_id == run.id`, `pgid` and `sid` ints equal to the
  run's pgid (the wrapper is session leader, so sid == pgid), and `members` a list of
  `{"pid": int > 1, "starttime": int >= 0}` (bools rejected). An empty list is valid.
- `unusable`: anything else (unreadable, invalid JSON, not an object, `members` not a list
  such as `"members":1`, a bad member, missing/mismatched `run_id`/`pgid`/`sid`). Never raises.

Rule for `unusable` (logged once per run to meshd.log, run kept in `_SEEN_UNUSABLE`):

1. While the wrapper identity holds, the next scan tick rewrites the file from a fresh scan
   (trust comes from the live wrapper; nothing is dropped) and the run leaves the set.
2. Once the wrapper identity is gone (`seen_hold`): while **any** live non-zombie process
   (other than meshd and pid 1) has `pgrp == pgid` or `session == pgid`, the run stays open
   and its lease is held and renewed: `_watch_one`'s wrapper-exit path returns without
   closing, `_advance` treats a `done` termination as pending, and `recovery._lost` starts a
   (carrier-only) termination instead of closing. Those processes are **not** signalled,
   because their identity cannot be proven (the r1c "never trust a pid number" rule).
   MESH_RUN_ID carriers are still terminated. When the group/session is empty the run closes
   normally.
   Cost of being conservative: if every real member died and an unrelated process later got
   the same number as its pgid/sid, the run is held until that process ends (visible as an
   open run with a held lease; never a wrong signal).

The in-memory map of the current meshd always wins over the file. `seen.json` now also
carries `"sid"`; an r4-format file (no `sid`) is `unusable` and is rewritten on the next scan
tick while the wrapper lives.

Tests (`TestSeenState`): missing -> absent; valid -> ok; 13 unusable sub-cases (corrupt,
non-object, members int/str/non-dict, pid as str/bool, missing starttime, run_id/pgid/sid
mismatch, sid missing, pgid wrong type); `"members":1` through `seen_for` does not raise and
is logged; hold while the group is occupied, release once empty, a missing file never holds;
an unusable file is rewritten while the wrapper lives.

### Item 4 (MINOR): vendor rule first

`lead_launch_check` order is now: vendor (`vendor_lead_m3_lite`, rc 13, `details.milestone:"M3"`)
-> test double outside testing mode -> `launch_enabled` (`adapter_not_launch_enabled`). Per the
coordinator ruling.

**Assertion changed (allowed by the ruling):** `tests/integration/test_lead_actions.py::test_vendor_launch_lead_refused`
(ACC: N4): expected `lead.turn_refused` reason `adapter_not_launch_enabled` ->
`vendor_lead_m3_lite`. Only that string; every other assertion and ACC tag kept.

`test_vendor_lead_gate.py`: r4's `test_vendor_not_launch_enabled_keeps_m2_reason` asserted the
old order and is replaced by `test_vendor_rule_runs_first` (droid with and without launch
evidence, testing mode on/off -> `vendor_lead_m3_lite`; a non-vendor adapter not launch-enabled
-> rc 13 without `details.reason`) and `test_non_vendor_not_launch_enabled_refused_as_m2`
(planner level: test double with `launch_enabled=False` -> one `lead.turn_refused
{reason: adapter_not_launch_enabled}`, no turn; non-evidenced droid -> `vendor_lead_m3_lite`).
`handlers/project.py` (set-lead / --lead) inherits the order: a vendor launch lead is rc 13
with `details.reason:"vendor_lead_m3_lite"` (milestone M3 as before).

### Item 5 (risk): readiness handshake

`spawn_envless_member`: the member installs `SIG_IGN` for SIGTERM, then writes `ready` to its
stdout pipe; the fake blocks on that line (`readline`, no sleep), exits 3 if it never comes,
and only then writes the pid file atomically (tmp + `os.replace`) as two lines `<pid>`, `ready`.
Tests read the pid file, so they can no longer signal before the handler exists. Test:
`TestEnvlessMemberReady.test_member_ready_before_pid_recorded` (pid file has `ready`, SigIgn has
SIGTERM right after the step returns, immediate SIGTERM leaves it alive).

### How the new tests were shown to fail on 7666005

Source files (`supervisor.py`, `recovery.py`, `lead_turns.py`, `fake_agent.py`) were restored
with `git checkout 7666005 -- <files>` while keeping the new tests, then
`python3 -m unittest -v tests.recovery.test_recovery_adopt_seen tests.integration.test_vendor_lead_gate`
was run: item 1 FAIL (run closed / lease released while the member lived), item 2 ERROR (the
injected OSError propagates; no retry), item 3 all ERROR (no `load_seen_state`; `"members":1`
-> TypeError), item 4 both new tests FAIL (vendor not launch-enabled -> `adapter_not_launch_enabled`),
item 5 FAIL (no `ready` line). The N4 string change fails on old code by construction. The
fixed files were then copied back; both r4 tests keep passing.

### r4b suite runs (`bash scripts/test.sh`, rc 0, tripwire files absent)

| Tree | unit | integration | recovery | wall | box |
|---|---|---|---|---|---|
| f65c7ef (N19 only) | 283 OK | 256 OK | 30 OK | 498 s | idle at start |
| 46ca4c4 (final code tree) | 283 OK | 256 OK | 39 OK | 702 s | idle at start |

The final-tree run was made on the uncommitted tree before f65c7ef; 46ca4c4's tree is
byte-identical to it. The N18 fix and its tests are one commit, so no commit holds the fix
without its tests or vice versa untested.

## r4c (fixes for the Linear re-review of a4009fa)

### Item 1 (MAJOR): seen.json retried on the termination ticks

`supervisor._advance` (every termination tick: the 1 s watch pass and the 200 ms
`advance_terminations` loop, so also the `kill.grace_s` window, kill retries and a seen
hold) now starts with `flush_seen` for adopted task runs. Before r4c the two early
`_advance` returns in `_watch_one` and every `advance_terminations` call skipped it, so a
run whose write had failed and that was then terminated never persisted its members.
`_write_seen` also removes its tmp file when the replace fails.

Test: `TestRecoveryAdoptSeen.test_seen_write_failure_persisted_while_terminating` (ACC: N18,
A8). A real storage fault, no hook: a non-empty directory sits at `seen.json`, so the
atomic replace fails at the adoption scan (meshd.log "write failed", no file). `task
cancel` starts the termination (member ignores SIGTERM, `kill.grace_s=120`); the
directory is removed and seen.json then lists the member while the run terminates;
`kill.grace_s=2`, kill -9 meshd, restart: while the member lives the run is open and
`task:<id>` held; meshd SIGKILLs it; lease released, run closed, `doctor` ok.

### Item 2 (MINOR a): open, do not `exists()`

`_load_seen` opens the path; only `FileNotFoundError` is `absent`, every other `OSError`
is `unusable` (`reason=unreadable`). Test `test_unreadable_path_is_unusable_not_absent`:
a symlink loop and (non-root) a run_dir without search permission, where
`os.path.exists` is False, are `unusable`; a directory and (non-root) a chmod-000 file too.

### Item 3 (MINOR b): coordinator decisions

Unchanged: nothing is killed by pgid alone and a hold is never released on a timeout.

1. **Migration.** A file without `sid` (r4 format) is `ok`; the run is marked legacy
   (`_SEEN_LEGACY`) and `seen_for` returns only members alive with their recorded
   starttime and `pgrp == pgid` (sid unknown). The next scan tick with a live wrapper
   rewrites the file in the r4b format. Test `test_legacy_file_without_sid_is_usable`.
   **Assertion changed (per the decision):** r4b's `test_load_unusable_cases` sub-case
   `sid_missing` (expected `unusable`) is replaced by `sid_wrong_type` (`"sid": "<str>"`,
   still `unusable`); every other sub-case kept.
2. **Reload on change.** While a run is in `_SEEN_UNUSABLE`, `seen_for` stats the file
   and re-reads it when `(mtime_ns, size)` changed. `ok` replaces the map and clears the
   hold; still-bad updates the reason; a deleted file keeps the hold (only a valid file
   lifts it). `_advance` merges the live members of a reloaded map into the termination's
   seen map, so the anchored group is targeted again. Test `test_held_run_reloads_repaired_file`.
3. **Loud event.** `run.seen_unusable {run_id, reason}` (reasons `missing_fields`,
   `bad_type`, `run_id_mismatch`, `pgid_mismatch`, `sid_mismatch`, `unreadable`,
   `invalid_json`), recorded once per run (in memory, and an existing event for the run
   suppresses another after a restart) from `_watch_one`, `_advance` and `recovery._lost`;
   meshd.log gets `run <id>: seen.json unusable (reason=<r>)`. Tests `test_unusable_reasons`
   and the integration test below (one event after many ticks and a further restart).
4. **Operator path.** `mesh run release <run-id> --force-seen-unusable` (`run.release`,
   `handlers/run.py`, routed in `server.py`). Direct user only (agent and run tokens: rc 4
   CAPABILITY_MISSING). Without the flag: rc 2 usage error naming the flag. A run that is
   not a task run held by `seen_hold` (open, adopted, unusable seen state, wrapper gone,
   group occupied): rc 6 INVALID_TRANSITION. With the flag: in the request transaction the
   run closes `lost` (`end_reason lost_on_restart`, result `reason: seen_release_forced`),
   the lease is released, the task takes the lost-run transition (as recovery's
   `_close_without_process`), `run.seen_release_forced {run_id, actor, reason}` is recorded;
   then the in-memory termination/seen state is dropped. No signal is sent. `--dry-run`
   changes nothing. docs/usage.md §6 has a paragraph.
   Test `tests/integration/test_run_release_seen.py::test_held_run_event_once_and_forced_release`
   (through bin/mesh): held run, one event + log line, refused without the flag, for an
   agent token and for a running run that is not held, works with the flag (run lost,
   lease gone, member still alive, event with actor/reason), refused again afterwards.

### Item 4 (MINOR c)

`TestEnvlessMemberReady` registers the member's SIGKILL cleanup right after reading the
pid file, before any assertion.

### How the new tests were shown to fail on a4009fa

A copy of the worktree (`/tmp/mh-r4c-old`) with the new tests and
`git checkout a4009fa -- mesh/daemon/supervisor.py mesh/daemon/recovery.py mesh/daemon/server.py mesh/daemon/handlers/run.py mesh/cli/commands/run.py`;
`python3 -m unittest tests.integration.test_run_release_seen tests.recovery.test_recovery_adopt_seen`:
item 1 FAIL ("seen.json not written while terminating: []"); item 2 FAIL
(`('absent', {}) != ('unusable', {})`); legacy FAIL (`'unusable' != 'ok'`); reload, reasons
ERROR (no reason tracking; the reload test's final hold check would fail too); release/event
FAIL (no `run.seen_unusable` event). r4b's tests keep passing on the new code.

### r4c suite run (`bash scripts/test.sh`, rc 0, tripwire files absent)

| Tree | unit | integration | recovery | wall | box |
|---|---|---|---|---|---|
| r4c code commit | 283 OK | 257 OK | 44 OK | 937 s | **not idle**: another `scripts/test.sh` (in mesh-fresh-v1) started 30 s before and ran alongside |

The code commit's tree is the tree that was run; the notes commit after it changes docs only.

## r4d (fixes for the Linear Agent re-review of r4c, docs/coordination/reviews/linear-m3-r4c.md)

Code commit `2f71e09` (on 3d20b85). Unchanged: nothing is killed by pgid alone, and a
hold is never released on a timeout.

### Item 1 (MAJOR): one seen-state read per tick

`supervisor.seen_snapshot(run, home)` flushes a failed write and then reads the seen
state once (`seen_for`, including the r4c reload). It returns `SeenSnapshot(members,
unusable, changed)`. `changed` is true when this read changed the recorded membership
or the usability of a run that was already loaded. `snapshot_hold(run, snap)` is the r4b
hold rule (unusable, wrapper gone, group/session occupied), decided from the snapshot
without reading the file again. `_watch_task_run`, `_advance` and `recovery._lost` each
take one snapshot per tick and use it for both the target scan / merge and the hold
decision. A tick whose snapshot has `changed` never closes the run:
`_watch_task_run` keeps the lease and returns, or passes `hold_tick` to `_advance`, and
`_advance` turns `done` into `pending`. The next tick merges and scans the new members.
The standalone `seen_hold(run)` is unchanged; it is only one read.

Tests in `tests/unit/test_seen_hold_r4d.py::TestOneSeenReadPerTick` use a real DB and real
processes. The setup is an adopted run whose session-leader wrapper has exited, an env-less
SIGTERM-ignoring member in its group and session, and a corrupt seen.json. A seam repairs
the file exactly between the old code's two read points:
- `test_repair_between_scan_and_hold_in_watch`: the repair runs right after `terminate.run_targets` in `_watch_task_run`.
- `test_repair_between_step_and_hold_in_advance`: the repair runs right after `terminate.step` returned `done` in `_advance`.
Both tests assert that the run stays open and `task:<id>` stays held on that tick and on
every later tick while the same member (pid + starttime) lives. The member must then be a
termination target and die, and only then does the run close and the lease get released.

### Item 2 (MINOR): event done only after it is stored

`report_seen_unusable(conn, run, home)` now writes only in its own transaction. Called
inside a caller's transaction, it does nothing and everything stays pending. The event
is marked done only after its transaction committed, or when the event already exists. A
failed write is logged (`seen state not persisted ... retrying next tick`) and retried on
the next call; it never raises. Tests in `TestSeenUnusableEventStored`:
- an injected `record_event` failure, then success: exactly one event;
- a call inside an outer transaction that is rolled back, then later calls: exactly one event.

### Item 3 (MINOR): persisted hold

**Decision:** the hold is stored in the existing `meta` table under key
`seen_hold:<run_id>`, with value `{"run_id", "reason", "since"}`. No schema change and no
migration are needed. How it works:
- `report_seen_unusable` writes the row while the run is unusable.
- `refresh_persisted_holds(conn)` reads the rows at recovery and on every watch pass, and drops rows of runs that are no longer open.
- On first load, `seen_for` treats a missing seen.json with a persisted hold as `unusable` (with the persisted reason) and logs `seen.json missing but a persisted seen hold exists`.
- A valid file lifts the hold, through the r4c reload or a rewrite while the wrapper lives. The next sync then deletes the row.
- `mesh run release --force-seen-unusable` deletes the row in its request transaction.
- As in r4b, the hold applies only while the group/session is occupied.
Tests in `tests/integration/test_run_release_seen.py` run through meshd and bin/mesh. Both
start from a held run, delete seen.json, kill -9 meshd and restart. The run must still be
open, the lease held, the member alive and not signalled, the row present, and still only
one event. Then:
- `test_deleted_file_after_restart_stays_held_until_forced`: a forced release closes the run `lost`, releases the lease and clears the row. The row stays cleared after another restart, and `doctor` is ok.
- `test_deleted_file_after_restart_valid_file_lifts_hold`: a valid seen.json listing the member is written. meshd targets and SIGKILLs the member, and only then closes the run and releases the lease. The row is cleared and `doctor` is ok.
docs/usage.md §6 now says that deleting seen.json does not lift the hold.

### Item 4 (MINOR): observation race

In `test_seen_write_failure_persisted_while_terminating`, `_check_held` reads the run and
lease and then re-confirms that the same member (pid + starttime) is alive. A violation
counts only when the member was alive both before and after the read. The observation
window is guaranteed by sync, not timing:
1. The restart after the storage repair keeps `kill.grace_s=120`.
2. The sync point is this boot's `terminating run <id>` line in meshd.log, after the pre-restart offset.
3. The test then takes 5 confirmed observations; meshd cannot SIGKILL the member before 120 s.
4. Only then does it set `kill.grace_s=2`, kill -9 meshd and restart. The member is killed by meshd while the same re-confirmed checks run.

### How the new tests were shown to fail on 3d20b85

`/tmp/mh-r4d-old` is a tar copy of the worktree (no .git) with
`git show 3d20b85:<f>` for `mesh/daemon/{supervisor,recovery}.py` and
`mesh/daemon/handlers/run.py`. The new tests use only APIs that 3d20b85 already has
(`_watch_one`, `_begin_termination`, `report_seen_unusable`, `seen_hold`, the CLI). The
run was `python3 -m unittest tests.unit.test_seen_hold_r4d tests.integration.test_run_release_seen tests.recovery.test_recovery_adopt_seen`
and gave 6 FAIL, 0 ERROR:

| Test | Old-code failure |
|---|---|
| watch seam | `'lost' not in ('starting','running')`: run closed on the tick whose second read saw the repaired file |
| advance seam | `'killed' not in (...)`: same |
| outer rollback | `0 != 1` events |
| write failure + retry | `0 != 1` events: the event is never retried |
| forced | `lease released after the restart: the hold was lost` |
| valid file | `lease released after the restart: the hold was lost` |

The item-4 test change is a robustness fix and passes on both trees.

### r4d suite run (`bash scripts/test.sh`, rc 0, tripwire files absent)

| Tree | unit | integration | recovery | wall | box |
|---|---|---|---|---|---|
| r4d code commit 2f71e09 | 287 OK | 259 OK | 44 OK | 505 s | idle: `pgrep -fa run_tier.py` empty before and after |

The tree that was run is the code commit's tree. The notes commit after it changes docs only.

## r4e (fixes for the Linear Agent re-review of r4d, docs/coordination/reviews/linear-m3-r4d.md)

Branch head before r4e: `deba962` (r4d `fcf00e6` + merge of main). Rulings kept as they
stand: the meta key `seen_hold:<run_id>` is OK; a normal close once the group/session
is confirmed empty is OK. Unchanged rules: nothing is killed by pgid alone, and no hold
is released on a timeout.

### Item 1 (MAJOR): force release changes memory only after the commit

- `mesh/daemon/handlers/run.py` `handle_run_release`: inside the request transaction it
  only does DB work (close `lost`, task transition, `run.seen_release_forced`, DELETE of
  the `seen_hold:<run_id>` row). It no longer calls `release_seen_hold` or logs; it
  appends `Effect("seen_release_forced", {run_id, reason, actor})`.
- `mesh/daemon/server.py` `execute_effects`: the effect calls
  `supervisor.finish_forced_release(home, run_id, reason, actor)`. Effects run only
  after `COMMIT` succeeded (a failed handler, idempotency insert or COMMIT goes to the
  ROLLBACK branch and drops `ctx.effects`). The call is synchronous right after COMMIT,
  so no watch tick runs in between. If it raised, the run is already closed: the watch
  skips closed runs, `advance_terminations` forgets their terminations, eviction drops
  the rest.
- `mesh/daemon/supervisor.py` `finish_forced_release`: `release_seen_hold` (seen maps,
  terminate state, persisted mirror) + the meshd.log line.
- A rolled-back release therefore leaves the effective hold exactly as it was: in-memory
  unusable state, `_SEEN_PERSISTED` mirror, meta row, run row and lease.
- Second guard, `refresh_persisted_holds`: a persisted reason that is new to the mirror
  (restored) drops a cached *usable* seen map for that run (`_SEEN`, `_SEEN_SIG`,
  `_SEEN_LEGACY`), so the next read applies the persisted reason (absent file -> still
  held; a valid file still lifts the hold). Not dropped: an already-unusable entry, and a
  dirty map (members scanned while the wrapper lived, not yet written; newer than any
  file). Logged as `persisted seen hold restored ... cached seen state dropped`.

### Item 2 (MINOR): a pending `run.seen_unusable` survives the run closing

- `_SEEN_EVENT_PENDING` (run id -> reason) is daemon-level: `_evict_seen` and
  `release_seen_hold` no longer drop it.
- `_sync_event_id(conn, run_id)` takes the event's `project_id`/`task_id` from the run's
  DB row (open or closed); a run id unknown to the DB is dropped (nothing to attach to).
  The existing "event already present" check still keeps it at one per run.
- `flush_pending_seen_events(conn, home)` runs at the end of every
  `watch_launch_runs` pass (after eviction), outside any transaction, and retries every
  pending entry until it is stored or found present. Never raises; logs failures.
- `tests/unit/test_seen_hold_r4d.py` `_forget` also clears `_SEEN_EVENT_PENDING` (test
  hygiene only: it is no longer cleared by `_evict_seen(set())`). No assertion changed.

### New tests (`tests/unit/test_seen_hold_r4e.py`, in-process, real DB and processes, /tmp/mh-* homes)

| Test | What |
|---|---|
| `test_rolled_back_release_keeps_hold_then_release_closes` | persisted hold, seen.json deleted; force release with an injected failure after the handler (ROLLBACK), immediate retry also rolled back, 3 watch passes: run open, lease held, member alive (pid+starttime), no `os.kill`/`os.killpg` to member or group, meta row present, no forced event. Then a committed release (+ server `execute_effects`): `lost`, lease and row gone, member untouched, one forced event, memory cleared. |
| `test_absent_file_cached_empty_then_hold_restored` | absent seen.json cached as usable `{}`, then the meta row appears: watch passes keep the run held, nothing signalled; after the member dies it closes. |
| `test_failure_on_close_tick_then_success` | held run, member dies; on the close tick every `run.seen_unusable` insert fails and the run closes; next tick: exactly one event (ids from the run row), nothing pending. |

### How the new tests were shown to fail on fcf00e6

`/tmp/mh-r4e-old` is a tar copy of the worktree (no .git, no `__pycache__`) with
`git show fcf00e6:<f>` for `mesh/daemon/{supervisor,server,recovery}.py` and
`mesh/daemon/handlers/run.py` (`cmp` confirmed identical to fcf00e6). The tests use only
APIs fcf00e6 has (`handle_run_release`, `HandlerContext`, `DaemonServer.execute_effects`,
`watch_launch_runs`, `seen_for`, `seen_hold`, `record_event`). Run:
`python3 -m unittest -v tests.unit.test_seen_hold_r4e` -> 3 FAIL, 0 ERROR:

| Test | Old-code failure |
|---|---|
| rolled-back release | `'lost' not found in ('starting','running') : run closed after a rolled-back force release` |
| restored hold | `'lost' not found in (...) : run closed although a persisted hold exists` |
| close tick | `0 != 1` events: the pending event was evicted with the run |

Either half of item 1 alone makes the first test pass (deferral keeps the mirror;
the refresh guard drops the stale `{}`); both are kept as defence in depth.

### r4e suite run (`bash scripts/test.sh`, rc 0, tripwire files absent)

| Tree | unit | integration | recovery | wall | box |
|---|---|---|---|---|---|
| r4e code commit 6f50951 | 353 OK | 264 OK | 49 OK | 538 s | `pgrep -fa run_tier.py` empty before and after; another worktree's unittest process was seen running on the box ~10 min earlier, none during the run's check points |

The suite ran on the exact tree committed as 6f50951 (working tree before the commit).
