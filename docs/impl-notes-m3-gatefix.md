# Implementation notes: m3-gatefix (M3 gate review, Linear REVISE on main 8602d33)

Coder: Quest coder, 2026-11-11. Base: main `8602d33`.

## P1-1 recovery adopt (N18, A8)
`recovery._adopt()` now calls `supervisor.establish_adopted_seen(fresh, home)` before returning. That runs the `track_adopted_seen` scan at once: the scan counts only if the wrapper pid + starttime still hold after it; members go to `_SEEN` and `seen.json`. The tick counter is reset, so the first watch tick with a live wrapper scans again. If the wrapper exited between adopt and the first watch, `seen_snapshot` returns the adopt-time members, `run_targets` takes them (confirmed identity, `_session_anchored` keeps the group trusted) and the two-scan termination runs. The past-deadline adopt path passes `seen_for()` to `_begin_termination`.
Round 2 (Linear re-review P1 and Quest review, not accepted as a residual): the untrusted window runs from `_identity_alive(wrapper)` in `_recover_run`, through the adopt transaction, event and log, to the end of the adoption scan. A wrapper that exits anywhere in it makes the scan untrusted (`wrapper_alive` false after the scan); that was read as "no members" and the run closed `lost` with the lease released and a same-group env={} member alive. Now it fails closed: `track_adopted_seen` returns True/False/None (trusted / scan lost its anchor / no scan this tick); `establish_adopted_seen` on False sets the in-memory seen to empty **and** marks the seen state unusable (reason `adopt_scan_untrusted`), so the r4b hold applies: run open and `task:<id>` held while any process is in the pgid/session, nothing signalled by pgid alone (MESH_RUN_ID carriers still are), operator release via `run release --force-seen-unusable`, or the run closes once the group is empty. Recovery calls `report_seen_unusable` at once (persisted `seen_hold:<run_id>` + `run.seen_unusable`) and returns `adopted_uncertain` (counted in `runs_adopted` and in the new `daemon.recovered` field `runs_adopted_uncertain`). A valid seen.json written later (only by a trusted scan) lifts the hold as for other r4b holds. No attempt is made to prove members from pgid/sid + starttime alone: after the wrapper is gone the pgid can be reused, so the hold is the conservative answer.

## P1-2 merge test step (N26, A8)
`run_test_step()` detects the wrapper's exit with `os.waitid(P_PID, pid, WEXITED|WNOHANG|WNOWAIT)` (`_exited_unreaped`, `_wait_exit_unreaped`) instead of `proc.poll()`/`proc.wait()`. The zombie wrapper (meshd's own child, `ppid == getpid()`) keeps `_wrapper_identity_alive` true, so the leftover scan (`run_targets_or_recheck`) and the termination see the whole group; the wrapper is reaped only in the `finally` after them. Existing semantics kept: leftovers found and terminated are recorded (`leftovers`, `termination`) and the step still passes (H17: exit 0 plus a terminated escapee is success); only `leftovers_kill_failed` (a leftover that survives SIGKILL) makes it not pass. Other reap sites: `supervisor._reap_wrapper` callers already scan first (task/lead-turn watch, finalize); recovery paths do not reap (adopted wrappers are init's children).

## Tests (`tests/unit/test_m3_gatefix.py`, real processes)
- `test_wrapper_exits_between_adopt_and_first_watch`: real temp DB (`init_db`), a wrapper that is not the test's child, env={} member in its group; `_recover_run` adopts, the wrapper exits before any watch; real `_watch_one` ticks: member terminated, run open and lease held while it lives, one `run.leftovers_terminated` listing it.
- `test_adopt_records_member_without_any_watch`: adoption alone writes the member to `seen.json`; first watch rescans.
- `test_wrapper_exits_during_adoption_scan_fails_closed` (round 2): real wrapper/member; `_scan_group` wrapped by a scheduling hook that runs the real scan, then makes the wrapper exit and waits until it is gone. `adopted_uncertain`, no seen.json, hold row + one `run.seen_unusable` (`adopt_scan_untrusted`); 3 s of real watch ticks: member alive and unsignalled, run open, lease held; after the test kills the member the run closes and the lease is released.
- `test_wrapper_exits_after_identity_check_before_adoption_scan` (round 2): wrapper exits just before `establish_adopted_seen` (the wider window) → `adopted_uncertain`, hold persisted, member alive, run open and lease held over real watch ticks.
- `test_adopt_past_deadline_hands_adoption_members_to_termination`: past deadline, wrapper exits right before `_begin_termination` → the env-less member is in `initial_pids` (pins the `seen_for` handoff).
- `test_same_group_envless_background_process_terminated`: zero mocks, `run_test_step()` with real `mesh.runwrap`, `env -i sleep 60 </dev/null >/dev/null 2>&1 &; exit 0` → `leftovers 1`, terminated, wrapper reaped by the step itself (no longer in `children.registered()`; Popen.__del__ would hide a missing reap).
- `test_wait_exit_unreaped_times_out_and_does_not_reap`: False after the timeout on a live process; True after it exits, with the zombie still unreaped (`/proc` state Z, `returncode` None).

## Mutations (all killed)
1. revert recovery.py (no adopt scan) → window test FAIL + seen test ERROR.
2. revert merge.py (reap before scan) → merge test FAIL.
3. no tick reset after the adopt scan → seen test FAIL.
4. `proc.wait()` reinserted before the scan → merge test FAIL.
5. `establish_adopted_seen` without the scan → FAIL + ERROR.
6. (r2) establish ignores an untrusted scan → FAIL.
7. (r2) recovery ignores the result (returns adopted) → FAIL.
8. (r2) trust the anchorless scan (drop the post-scan identity check) → FAIL.
9. (r2) hold not persisted at recovery → FAIL.
10. (r2) untrusted read as no members (no unusable mark) → FAIL.
11. (Quest review) adopt-past-deadline `seen=None` → FAIL.
12. (Quest review) post-scan identity recheck removed → FAIL (2 tests).
13. (Quest review) reap + unregister in `finally` removed → FAIL.
14. (Quest review) `_wait_exit_unreaped` True on timeout → FAIL.

## Runs (base)
Targeted 3x (recovery, merge, termination, race/runtargets, seen-hold, reap, 18 modules, 240 tests): rc 0 each, 266/268/266 s.
Full `bash scripts/test.sh` once, idle base (load 0.00): rc 0, 707/282/49, 69+370+37 s, **477 s** (< 560 s).

Round 2 (head after this commit): targeted 3x (same 18 modules, 244 tests) rc 0: 274/273/272 s. Full `bash scripts/test.sh` once, idle base: rc 0, 711/282/49, 74+357+36 s, **468 s**.

## Round 3 (Linear REVISE on 3e7b949; Quest review of 3e7b949)
- **Old valid seen.json bypassed the hold (Linear P1).** A valid seen.json from an earlier trusted scan stayed in place; the r4c reload read it as a repair and lifted the hold, and the r4d restart path restores a persisted hold only when the file is missing. Fix (superseded in round 4 by the durable marker): when the adoption scan is untrusted, `establish_adopted_seen` moved seen.json aside atomically (`os.replace` to `seen.json.untrusted-<ms>`, kept for forensics; unlink if the rename fails) before marking the run unusable. With no seen.json the reload keeps the run unusable and every restart restores the persisted hold. Only a scan trusted after this point can write a new seen.json, and that needs the wrapper alive, so in practice the hold ends when the group/session is empty or with `run release --force-seen-unusable`.
- **Documented behaviour (Quest review):**
  - An uncertain adopt past its deadline returns `adopted_uncertain` before the deadline path: the wrapper is already gone, so there is no timeout termination; the run is held like any other uncertain adopt (MESH_RUN_ID carriers are still terminated).
  - A hold can linger if the run's pgid/session number is reused by an unrelated process after the original group emptied; the run then stays held until an operator releases it (`run release --force-seen-unusable`). Fail-closed by design.
  - ~~Accepted residual~~ (withdrawn in round 4: the Quest review and Linear reproduced it; see Round 4).
- **Tests (real processes):** `test_old_valid_seen_file_does_not_lift_untrusted_hold` (Linear's repro: a valid wrapper-only seen.json, then recovery's real scan finds the member and the wrapper exits before the re-check; old file moved aside; hold row stays over real watch ticks, member alive, run open, lease held); `test_untrusted_hold_survives_immediate_restart` (same, then memory cleared and `refresh_persisted_holds`); `test_untrusted_hold_full_recover_counts_and_second_restart` (full `recover()`: `daemon.recovered` has `runs_adopted=1`, `runs_adopted_uncertain=1`, `runs_lost=0`; a second full `recover()` after clearing memory keeps the run held).
- **Mutations (killed):** R1 no invalidation of the old file (3 tests fail); R2 restart ignores the persisted hold when the file is absent (2); R3 copy instead of move (2); C1 `runs_adopted` omits uncertain (1); C2 `runs_adopted_uncertain` always 0 (1). All earlier mutations (M1–M10, Q1–Q4) re-run and still killed.

Round 3 runs: targeted 3x (18 modules, 247 tests) rc 0: 280/281/280 s. Full `bash scripts/test.sh` once, idle base: rc 0, 714/282/49, 81+356+36 s, **473 s**.

## Round 4 (Quest review of 79a9479 REVISE; Linear REVISE on 79a9479)
Both reproduced lost-with-member-alive after a crash between the move-aside and the hold-row write, and Linear also with rename+unlink both failing (the old valid file then won after a restart) and with the event committed but the hold row not. The round-3 move-aside is replaced:

- **Durable marker instead of move-aside.** `write_unusable_marker()` replaces seen.json with `{"mesh_seen_marker": "unusable", "run_id", "reason", "written_at"}` via tmp + fsync + `os.replace` + directory fsync. `_load_seen` returns it as unusable with its reason; it has no `members`/`pgid`, so any reader (also an older build) gets `missing_fields` and can never take it as seen data. A trusted scan overwrites it (`_SEEN_MARKER` forces the rewrite).
- **Marker before anything is committed.** `recovery._adopt` calls `supervisor.prepare_adopt_seen()` before the adopt transaction: it keeps the current seen map in memory and writes an `adopt_pending` marker. The trusted adoption scan replaces it with real members; an untrusted scan rewrites it as `adopt_scan_untrusted`. Crash points:
  1. before the marker: nothing of this adoption is committed (lease holder unchanged, old file untouched): the next boot is the same boot again;
  2. after the marker, before/after the adopt commit, before the scan: the restart reads the marker (wrapper dead → `_lost` → unusable → held; `report_seen_unusable` there persists the hold row on boot; wrapper alive → adopted again, trusted scan replaces the marker);
  3. after the untrusted marker, before the hold row: same, reason `adopt_scan_untrusted`;
  4. event without hold row: impossible now, see below.
- **The hold row wins over any seen.json.** Adoption holds (`adopt_pending`, `adopt_scan_untrusted`) carry a random token, in memory from the moment of the mark and in the hold row (`meta seen_hold:<run_id>` → `token`). `_load_seen` treats any otherwise valid seen.json whose `hold_token` differs as unusable (reason = the hold's); only a scan trusted after the hold writes the token into seen.json. `refresh_persisted_holds` restores tokens on boot (a token-less adoption row from an older build gets a fresh token, so no file lifts it). Other r4b/r4c holds (corrupt file, operator repair) are unchanged.
- **Failed marker write fails closed.** The run stays unusable in memory, the old file is ignored in this process (`_SEEN_SIG` unchanged → no reload) and, once the hold row is written, after any restart (token). Nothing falls back to the old file.
- **Event and hold row in one transaction.** `_sync_hold_row` inserts a pending `run.seen_unusable` (`_event_in_tx`) and the hold row in the same transaction; `report_seen_unusable` runs the hold step first and stops on a failure, so both are retried together.
- Residual (double fault): the marker write fails **and** the process dies before the hold row commits → the next boot sees the old file and no hold. Needs a failing filesystem write plus a crash in the same few ms; logged.
- **Tests (real processes, real DB):** `test_r4_marker_write_fails_old_file_never_wins` (PermissionError injected on the marker rename and the tmp unlink; old file stays; hold row with token; restart → held); `test_r4_crash_after_marker_before_hold_row` (hold/event persistence skipped = crash; restart → held, row persisted on boot with reason `adopt_scan_untrusted`); `test_r4_crash_after_adopt_commit_before_scan` (crash after the commit, wrapper exits; marker `adopt_pending`; restart → held); `test_r4_hold_row_write_fails_event_not_committed_alone` (real SQLite trigger aborts the hold-row insert: no event either; restart → both written, held); `test_r4_crash_before_marker_is_a_clean_restart` (crash at `prepare_adopt_seen`: lease unchanged, no file; next boot adopts, trusted scan records the member, member terminated when the wrapper exits, run closes); `test_r4_event_failure_does_not_commit_hold_row_alone` (event insert fails → no hold row either; next report writes both); `test_r4_trusted_rescan_after_hold_carries_token` (re-adopt with the wrapper alive under a persisted adoption hold: the trusted file carries the hold token and is accepted after a restart); `test_r4_absent_seen_file_never_lifts_hold` (P3: seen.json deleted → held live and after a restart). The round-2/3 tests now assert the marker instead of a missing file. Every restart test asserts `runs_lost == 0`, run open, lease held, member alive and unsignalled.
- **Mutations (round 4, all killed):** N1 no `adopt_pending` marker; N1b `prepare_adopt_seen` not called; N2 no token check in `_load_seen`; N3 tokens not restored on boot; N4 event committed separately; N5 no untrusted marker in establish (7 tests); N6 marker branch removed from `_load_seen`; N7 no stop after a failed hold step (r4d test); N8 trusted write without the token; N9 marker never replaces the file (7 + 1 error). Earlier sets re-run: see runs below.

Round 4 runs (rebased on main f66ae8a): mutations above plus re-run of Q1-Q4, M6, M7, M9, M10, C1, C2, R2: all killed. Targeted 3x (18 modules, 255 tests) rc 0: 311/307/305 s. Full `bash scripts/test.sh` once, idle base: rc 0, 722/282/49, 103+376+35 s, **515 s** (< 560; unit +22 s from the new real-process hold tests).

## Round 5 (Linear REVISE on b717e89)
- **P1-a, failed adopt transaction used the cached old members.** `prepare_adopt_seen` now marks the run unusable in memory (`adopt_pending`, quiet, with a hold token) before it writes the marker and before the adopt transaction. If the transaction fails and the daemon goes on, the watch sees an unusable run (the reload also reads the marker) and holds it; the hold row and `run.seen_unusable` are written by the watch. A trusted adoption scan lifts it as before.
- **P1-b, crash between the marker tmp fsync and the rename.** The old valid seen.json remains. New boot rule in `recovery._lost`: when the wrapper is dead and nothing recorded is alive, but the run's pgid/session still holds processes, the run is held (`wrapper_gone_group_occupied`: memory, token, marker, hold row) and nothing is signalled by pgid alone. Exception (`_group_still_ours`): if a live process now has pid == pgid with another starttime than the wrapper, the pid was reused. Linux never reuses a pid while any live process has it as pgid or sid, so the old group emptied first; then §11.7 lost_on_restart applies as before (`test_stale_wrapper_json_identity_never_signalled` unchanged and passing). This also covers the older case of a member that joined after the last trusted scan when meshd was down.
- **P2, watch flush committed the event alone.** `_sync_event_id` now goes through `_sync_hold_row` (event and hold row in one transaction) whenever a hold row is due; only events without a due hold row use the plain path.
- **Tests:** `test_r5_adopt_transaction_fails_cache_not_used` (real SQLite trigger rejects `run.adopted`; recover reports `run_errors` 1; marker `adopt_pending`; wrapper exits; watch ticks hold the run; exactly one `run.seen_unusable`); `test_r5_crash_between_marker_fsync_and_replace` (KeyboardInterrupt at the marker rename; old file intact; wrapper exits; full `recover()` → `runs_lost` 0, hold reason `wrapper_gone_group_occupied`, member alive); `test_r5_flush_path_commits_event_with_hold_row` (hold-row trigger: flush writes neither; after the trigger is dropped both). Hold checks in this module now watch 1.2 s instead of 2.5 s (still more than two 0.2 s supervisor scans), to keep suite time down.
- **Mutations (killed):** O1 prepare does not mark unusable in memory; O2 no boot hold for an occupied group; O3 pid-reuse check removed (§11.7 stale-wrapper integration test); O4 flush writes the event without the hold transaction. O5 (no event for an adoption hold in `_sync_hold_row`) survived: the watch reload already sets the event; the line was redundant and is removed.
- **Quest review of b717e89, P1-1 (regression: the marker forgot escapees).** The `adopt_pending` marker replaced the only durable record of members that had left the pgid/session (setsid escapees). The marker now carries `prior_members` (pid + starttime): the union of the file it replaces (valid file, earlier marker, or a token-mismatched file) and the in-memory map. `_load_seen` still returns a marker as unusable, but with those members, so `seen_for` / `prepare_adopt_seen` seed them into memory. The trusted adoption scan keeps live ones through the existing merge, and `_lost` treats live ones (same starttime) as members to terminate, while the hold still covers the rest. A token-mismatched old file is likewise unusable but keeps its members as targets. `load_seen_state` (public) still returns `{}` for unusable. An older build reads the marker as `missing_fields`.
- **P2-1 (pre-existing since r2).** The untrusted adoption branch no longer sets `_SEEN = {}`. It restores the members known before the scan (not the untrusted scan's own), and the marker records them.
- **Tests (real processes; escapee = `sleep` started with `start_new_session=True`, recorded in the old seen.json, no MESH env):**
  - `test_r5_escapee_crash_after_commit_wrapper_alive` (e1/e6) and `..._wrapper_dead` (e5): crash after the adopt commit; the marker lists the escapee; full `recover()` → `runs_lost` 0, the escapee is terminated, and the lease is held while it lives.
  - `test_r5_escapee_kept_on_untrusted_adopt`: the untrusted adopt keeps the escapee in memory and in the marker, and it is terminated.
  - `test_r5_old_file_members_stay_targets_under_token_hold`: persisted token hold, old file without token, wrapper dead, restart → escapee terminated.
  - `test_r5_trusted_adopt_old_file_equals_scan_replaces_marker` (X9): old file == scan; after the trusted adopt seen.json is a real members file, and after a restart with the wrapper dead the member is terminated and the run closes.
  - The absent-file test now also pins the hold reason after the restart.
- **Mutations (killed):** P1 marker without `prior_members` (3 tests); P2 marker members dropped on load (2); P3 prepare drops marker members; P4 untrusted branch drops prior; P5 token-mismatched file members dropped; R2 (absent file + persisted hold at restart; it had survived the first round-5 run because the occupied-group boot hold masked it, now killed through the hold reason); O1, O2, O4 re-run.
  - X9 (`_SEEN_MARKER` no longer forces the rewrite) is equivalent now: every marker writer also sets a hold token, which forces the rewrite on its own. The condition stays as a guard.
- **Crash-before-marker limit (c0, corrected wording; round 4 said "the same boot again").** A crash before the marker leaves the old seen.json, which is the pre-existing "meshd down when the wrapper exits" case. With the round-5 boot rule, processes still in the pgid/session hold the run, and the members recorded in the old file are terminated. What remains: a process that left the group after the last trusted scan and is recorded nowhere. Nothing can find it without its env (pre-existing, same as before the gate fix).
- **Double fault (c4b, reproduced by the Quest review, small).** The marker write fails and the process dies before the hold row commits. At the next boot the old file is valid. If the wrapper is dead and anything remains in the pgid/session, the run is held by the boot rule; recorded escapees are terminated. An unrecorded escapee is the c0 limit.
- **Suite time:** `_assert_held` counts 12 real watch ticks (8 for in-process checks; the seen scans are tick-based) instead of 2.5 s / 1.5 s windows. The escapee helper stops as soon as the escapee is gone. test_m3_gatefix: 26 tests in about 13 s (round 4: 18 tests, about 33 s).

Round 5 runs: all round-5 mutations killed except X9 (equivalent, see above); re-run of N1-N9, Q1-Q4, M6, M7, M9, M10, C1, C2, R2: all killed; tree clean afterwards. Targeted 3x (18 modules, 263 tests) rc 0: 280/278/277 s. Full `bash scripts/test.sh` once, idle base: rc 0, 730/282/49, 80+360+35 s, **476 s** (round 4: 515 s).

## Round 6 (Linear REVISE on 86a18ca; Quest review of 86a18ca ACCEPT with P3 items)
The guards from rounds 1–5 each sat on one path. Round 6 adds **one close-safety gate** that every close of a task run passes.

- **`supervisor.close_gate(run, end_reason)`**, called at the top of `_close_run` (the only function that closes a task run and releases its lease in meshd):
  1. **Identity:** the DB pgid/starttime or, when the DB has none yet (spawn window, TX B not committed), `wrapper.json` (`close_identity`). Recorded members are the in-memory map plus seen.json, including a marker's `prior_members` or a token-mismatched file, plus the wrapper itself.
  2. **A recorded identity alive** (pid + starttime, not a zombie) → refuse.
  3. **pgid/session still occupied** and `group_still_ours` (no other live process with pid == pgid) → refuse.
  4. **Otherwise** close.
  - A refusal raises `CloseHeld` inside the caller's transaction, so nothing is written.
- **`hold_close`** acts on a refusal. It is used by `_watch_one` (now a wrapper around `_watch_one_inner`) and by recovery's per-run loop. It:
  - writes the disk identity into the DB if missing;
  - holds the run if the group is occupied (`wrapper_gone_group_occupied`: memory, token, marker, hold row + event in one transaction);
  - terminates live recorded identities through `terminate.begin` (the usual two-scan release). The next close attempt passes the gate again.
- **Operator override:** `run release --force-seen-unusable` calls `_close_run(..., gate=False)`. It is the only caller allowed to skip the gate.
- **Recovery `_lost`** also takes the identity from `wrapper.json` when the DB pgid is NULL (Linear P1-b), so the marker / r4b hold applies directly (reason `adopt_pending`). The gate stays the backstop.
- `recovery._group_still_ours` was moved to `supervisor.group_still_ours(pgid, wrapper_starttime)`, shared by recovery and the gate.
- **P2:** `prepare_adopt_seen` queues `run.seen_unusable` (`_mark_unusable(..., adopt_pending)`). A trusted adoption scan drops it again before anything was persisted (`_persist_seen`). A later, more specific reason (`adopt_scan_untrusted`) replaces a still-pending `adopt_pending`. Event and hold row still commit in one transaction whenever a hold row is due. When a hold row is already persisted, a later event-only write is possible (`_sync_event_id_only`).

**Close / lease-release audit** (grep: `SET status`, `DELETE FROM leases`, `_close_run`, `_finish_run`, `_close_without_process`):

| Site | Closes | Through the gate? |
|---|---|---|
| supervisor `_finish_run` (exited/lost; watch and the recovery exit.json → `_watch_one` path) | task run | yes (`_close_run`) |
| supervisor `_finalize_termination` (killed / lost / spawn_failed after a termination) | task run | yes |
| recovery `_close_without_process` (`_lost`) | task run | yes |
| handlers/run.py `handle_run_release` | task run | no, `gate=False`: operator override, by design |
| supervisor.py `_fail_task_spawn` / `handle_spawn_failure` (spawn failure) | task run | no gate, but since round 7 a failure **after** Popen (registration or TX B) first kills the wrapper and its group through the unreaped Popen handle (`_abort_spawned`); only a failure before Popen closes with no process. Round 8: the abort also kills `MESH_RUN_ID` carriers outside the group (setsid escapees, recorded); if anything survives, the run is **not** closed: the lease is kept, the wrapper stays unreaped and the watch pass retries until everything is gone, then closes `spawn_failed`. Round 11: unified via `handle_spawn_failure` across task runs and lead turns, with TX B inside `try:` and closing DB errors keeping `_SPAWN_ABORTS` active |
| handlers/task.py `handle_task_cancel`, `handle_task_submit`, `handle_task_fail` | task run | no: only for leases with `holder_kind != 'meshd'` (inbox mode, no meshd-supervised process); meshd-held runs return early / defer to the watch |
| scheduler.py `sweep_expired_inbox_leases` (lease expiry) | task run | no: `holder_kind = 'agent'` leases only |
| lead_turns.py `finish_lead_turn` (via `_close_run`), `handle_spawn_failure` | lead turn | `finish_lead_turn` has no `close_gate` (scoped to `kind = 'task'`, excluded by seen logic; known gap under **L4** while vendor leads are disabled; must add gate + seen tracking before enabling vendor leads). Since round 11, post-`Popen` / TX B spawn failures in `start_lead_turn` go through `supervisor.handle_spawn_failure`, keeping the wrapper unreaped, `leadturn:<prj>` held, and `outbox` in `'delivering'` until all spawned/escaped `(pid, starttime)` identities are confirmed dead and the closing DB transaction commits |
| merge.py `run_test_step` and the merge pipeline's run close (live test step) | merge_test run | no: guarded by round 1's `waitid(WNOWAIT)` reap rule (group scanned before the wrapper is reaped) |
| merge.py `_close_recovered_merge_run` (boot recovery) | merge_test run | yes since round 7: `merge_close_gate` (identity from the live wrapper, DB or wrapper.json; occupied group still the run's → `MergeRunHeld`: run open, `merge:<prj>` kept, `merge.recovery_blocked`). Exception: a `kill_failed` termination keeps its recorded §11.4 deviation |

**Tests (real processes):**
- `test_r6_exit_json_path_does_not_close_occupied_group` (Linear P1-a):
  - Setup: real `mesh.runwrap` as session leader; the command starts an env-less same-group member and exits 0 on a trigger. The old seen.json lists only the wrapper.
  - Crash at the first marker rename, then the command exits and runwrap writes exit.json.
  - Full `recover()` → `runs_lost` 0, `runs_exited` 0, hold `wrapper_gone_group_occupied`, then held for 12 watch ticks with the member alive.
- `test_r6_spawn_window_marker_with_null_db_identity` (Linear P1-b):
  - Setup: real runwrap; DB wrapper_pid/pgid/starttime NULL; prepare writes `adopt_pending`, then a crash before the adopt transaction.
  - The wrapper is SIGKILLed (no exit.json).
  - Full `recover()` → DB pgid is taken from wrapper.json, hold reason `adopt_pending`, run held.
- `test_r6_close_gate_unit`:
  - The gate refuses while the wrapper lives (live identity, from wrapper.json with the DB empty), then refuses on the occupied group alone.
  - `_close_run` raises and writes nothing; `gate=False` closes.
- `test_r6_pid_reuse_group_not_ours` (Quest Y2): a live process with pid == pgid and another starttime → `group_still_ours` False, and the gate does not hold.
- `test_r6_marker_of_another_run_gives_no_members` (Quest Y9): a marker with a foreign run_id → `run_id_mismatch`, no members.
- The X9 test now also checks that a trusted adoption leaves no `run.seen_unusable`. The spawn-window test pins reason `adopt_pending`, so recovery holds the run itself and not only through the gate.
- **Isolation:** `_forget` also clears the run's pending/done event state. Every test in test_m3_gatefix passes alone (31/31, each in its own process) as well as in the module. `test_r5_adopt_transaction_fails_cache_not_used` failed alone before the P2 fix.

**Mutations (all killed):**
- G1 gate not called; G2 occupied check removed; G3 live-identity check removed; G4 no wrapper.json identity in the gate.
- G5 `_lost` does not fill the identity; it survived until the reason pin.
- G6 prepare does not queue the event; G7 `hold_close` does not hold.
- G8 pending event not dropped on a trusted lift; it survived until the X9 event check.
- G9 / Y2 pid-reuse exception removed: now killed by a unit test as well as by the §11.7 integration test.
- G10 operator release through the gate; Y9 marker run_id check removed.

**Bounded false hold (Quest review b3).** If the run's group emptied, its pgid number was reused by a process that is not a pid == pgid leader, and that process joined the old pgid/session number, the run is held (never signalled) until the occupant exits or `run release --force-seen-unusable`. Linux keeps a pid reserved while any process uses it as pgid/sid, so this needs a chain of reuses. No signal is ever sent to such a process.

Round 6 runs: G1-G10, Y9, P1-P5, R2, O4, N1-N9, Q1-Q4, M6, M7, M9, M10, C1, C2 killed. Equivalent survivors: X9 (as in round 5) and O2 (the boot occupied-group branch in `_lost`: with it removed, the same run reaches `_close_without_process`, the gate refuses and `hold_close` holds it with the same reason; the branch stays as the direct path). Tree clean afterwards. Every test_m3_gatefix test passes alone (31/31). Targeted 3x (18 modules, 268 tests) rc 0: 280/282/284 s. Full `bash scripts/test.sh` once, idle base: rc 0, 735/282/49, 81+368+35 s, **485 s** (round 5: 476 s).

## Round 7 (Linear REVISE on 47ce890; Quest review of 47ce890 ACCEPT with P3s)
- **P1 merge_test restart recovery.** `recover_merge_runs` closed a run whose wrapper was dead after scanning only `MESH_RUN_ID` carriers. Now every recovered close goes through `merge.merge_close_gate`:
  - **Identity:** the live wrapper, else `supervisor.close_identity` (DB, else wrapper.json).
  - **Live wrapper:** terminated first, as before (two-scan rule).
  - **Occupied group, still the run's:** raises `MergeRunHeld`. The run stays open, `merge:<prj>` stays held, and the disk identity is written to the DB. `merge.recovery_blocked` is recorded once per reason, step 5 leaves the task `merging` (`run_open`), and `daemon.recovered` gets `merge_runs_held`.
  - **Re-evaluation:** at every boot. There is no runtime re-check: a held merge run waits for a restart after its occupant ends. Recorded as a limitation; `run release --force-seen-unusable` applies to task runs only.
  - **kill_failed:** keeps its recorded deviation (survivors in `run.kill_failed`, lease released), now pinned by a test.
- **P2 spawn_failed after Popen.**
  - `children.popen_registered` raises `SpawnedThenFailed(proc)` when registration fails after Popen. The TX B identity read moved inside the spawn `try`.
  - When a process exists, `_abort_spawned` sends SIGKILL to its group through the unreaped Popen handle until the group is empty (at most 5 s), then reaps it. This is safe: an unreaped leader keeps its pid and pgid from reuse.
  - Only then does the run close `spawn_failed`. `result_json.after_popen` records pid, starttime and any survivors (survivors also get `run.kill_failed`). A failure before Popen is unchanged.
- **P2 gate cleanup keeps the outcome.** When the gate refuses an own-exit close (`exited`/`lost`: `GATE_KEEPS_OUTCOME`), `hold_close` terminates the live recorded identities as **leftovers** (`run.leftovers_terminated`). The run then closes through `_finish_run` with its own outcome: exit 0 → task auto-submitted, as in H17, never killed or task failed.
- **Quest P3-1:** the "close refused" line is logged once per (end_reason, live set, occupied) state, and reset when the run closes.
- **Quest P3-2:** a missing, zero or garbage wrapper starttime is "unknown". `group_still_ours` then fails closed (cannot prove a reuse), so an occupied pid/group holds the run. Nothing is signalled on that basis.
- **Quest P3-3:** pinned M6 (a zombie is not a live identity) and M7 (the exit path and the per-run `CloseHeld` catch both count `runs_terminating`, hold the run, and record no error).
- **Quest P3-4 / Linear P3:** the lead-turn row now cites L4 and states what must change before vendor leads are enabled.

**Tests (real processes):**
- `test_r7_merge_recovery_holds_occupied_group`: real runwrap, env-less same-group member, exit 0 with exit.json, wrapper reaped, DB identity NULL. Two full `recover()` runs → `merge_runs_held` 1, `merge_runs_lost` 0, `merges_interrupted` 0, lease kept, member alive, one `merge.recovery_blocked`. After the member dies, the next boot closes the run lost and releases the lease.
- `test_r7_merge_gate_pid_reuse_closes`: wrapper.json names another live group leader with a different starttime → closed lost, nothing signalled.
- `test_r7_merge_kill_failed_keeps_recorded_deviation`: kill_failed → killed, lease released, `run.kill_failed` survivors. The same run without kill_failed → `MergeRunHeld`.
- `TestSpawnFailsAfterPopen` (registration EIO, TX B identity EIO): `start_launch_run` with a command that leaves an env-less member. EIO is injected once the member exists. Result: `spawn_failed`, wrapper reaped (no `/proc` entry), group empty, lease released, task failed, wrapper unregistered.
- `test_r7_gate_cleanup_keeps_exit_outcome`: exit 0 with a live recorded member → run `exited` 0, task `submitted`, one `run.leftovers_terminated`, member dead, lease released.
- `test_r7_unknown_starttime_fails_closed` (no / 0 / "abc" starttime), `test_r7_zombie_identity_not_live`, `test_r7_refused_close_logged_once_per_state` (20 refusals → 1 line), `test_r7_recovery_catches_close_held`. The P1-a test now also asserts `runs_terminating` 1.

**Mutations (round 7):**
- **Killed:** H1 merge gate not called; H2 no wrapper.json identity; H3 `group_still_ours` ignored in the merge gate (killed by the pid-reuse test); H4 registration failure not carried; H5 no abort; H6 abort kills the leader only; H7 TX B read outside the try; H8 outcome not kept; H9 / K3 kill_failed exemption removed; K1 log not rate-limited; K2 unknown starttime proves reuse; M6q zombie live; M7a / M7b terminating outcome.
- The first run of H3 and H9 found them **surviving**; the pid-reuse and kill_failed tests were added for them.

Round 7 runs: all 41 test_m3_gatefix tests pass alone, each in its own process. Mutations killed: G1-G10, Y9, H1-H9, K1, K2, M6q, M7a, M7b, P1-P5, R2, O4, N1-N9, Q1-Q4, M6, M7, M9, M10, C1, C2. The first chain aborted on two stale targets (G9, Y9, after the `group_still_ours` rewrite) and ran H3/H9 against the merge test only; all were re-run against the module and killed. O2 is still an equivalent survivor (gate backstop, round 6). Tree clean afterwards. Targeted 3x (18 modules, 278 tests) rc 0: 281/283/283 s. Full `bash scripts/test.sh` once, idle base: rc 0, 745/282/49, 84+365+37 s, **486 s**.

## Round 8 (Linear re-review of 2b913d1: REVISE, three P1s; scope set by James 16:03)
- **P1-1: unknown starttime promoted to trusted.**
  - `supervisor.known_starttime()` treats NULL, 0, garbage and negative values as unknown.
  - `wrapper_alive()` returns False for an unknown recorded starttime ("not provably alive"). So no seen scan is seeded from that pid, no termination targets it, and the close gate holds the run.
  - Recovery stores an unknown wrapper.json starttime as NULL, no longer as 0.
  - `terminate._wrapper_identity_alive` treats a starttime of 0 or less as unknown, so the group is not trusted.
  - **Test** `test_r8_unknown_starttime_never_trusted` (exact repro): DB identity empty, wrapper.json starttime 0 naming a live unrelated process in its own group, deadline passed. Full `recover()` plus 15 watch passes and termination ticks. Nothing is signalled (kill/killpg recorded), the process lives, the run stays open with its lease, the DB starttime stays unknown and no seen members are written.
- **P1-2: setsid escapees.**
  - Each kill pass in `_abort_spawned` also runs the run-id scan (`terminate.run_targets`, environ plus the descent closure). It kills every carrier by pid + starttime, and records the ones outside the wrapper's group as `after_popen.escapees`.
  - **Test** `test_r8_setsid_escapee_killed_and_recorded`: real `start_launch_run`/runwrap, a setsid child with the inherited env, EIO at registration. Result: escapee killed and recorded, run spawn_failed, lease released.
- **P1-3: survivors kept the lease released.**
  - On survivors after `SPAWN_ABORT_WAIT_S`, the run is no longer closed. It stays open with its identity, its lease and a revoked token, and gets `run.kill_failed` with `lease_released: false, retrying: true`.
  - The wrapper is never reaped while anything survives: there is no `proc.poll()`, and the leader check reads /proc state Z, which keeps the pgid reserved.
  - `_SPAWN_ABORTS` plus `retry_spawn_aborts()` run in every watch pass, and the main watch loop skips these runs. Once the group and carriers are gone, the wrapper is reaped and the run closes `spawn_failed` (`after_popen.retries`, `survivors_at_abort`). The lease is released only then (plan §11.4).
  - After a meshd restart the in-memory entry is gone. The open run, with identity and lease, then goes through boot recovery and the close gate.
  - **Test** `test_r8_survivors_keep_lease_and_retry`: EPERM injected on killpg and kill for the wrapper's group. The run stays open, the lease is kept, the wrapper stays unreaped and the group is alive, with one kill_failed event (retrying), and this holds over 3 watch passes. After the injection ends, a watch pass kills the group, reaps the wrapper and closes spawn_failed: lease released, task failed, wrapper unregistered.
- **Quest review P2 (2b913d1), not fixed this round (scope set by James 16:03): known limitation.**
  - A merge_test run held by `merge_close_gate` is re-checked only at boot. `run release --force-seen-unusable` refuses it (`seen_held` needs `kind = 'task'`).
  - So `merge:<prj>` stays blocked until meshd restarts, even after the occupant exits.
  - Workaround: restart meshd after the occupant is gone.
- **P3:** L4 is called a known limitation (not accepted). The audit table cites function names, not line numbers.

Round 8 runs: all 45 test_m3_gatefix tests pass (one module run, 18.6 s).
- Mutations killed: U1 (wrapper_alive trusts an unknown starttime), U2 (recovery stores 0), E1 (carriers not killed), E2 (escapees not recorded), S1 (survivors close anyway), S2 (leader reaped in the kill pass), S3 (no retry in the watch pass).
- Equivalent survivors:
  - U3: terminate trusts starttime 0, but 0 never equals a real starttime.
  - S4: the watch loop does not skip pending aborts, but the close gate holds the occupied group.
- Targeted once (18 modules + test_execution_launch + test_execution_launch_fences, 296 tests): rc 0, 307 s.
- Full `bash scripts/test.sh` once, idle base: rc 0, 749/282/49, 85+369+35 s, **490 s**.

## Round 9 (Linear re-review of ac7e5a7: REVISE, three P1s plus a reaper safety item; P1 only)
- **P1-a: authenticated escapees were lost.**
  - Each kill pass keeps every `MESH_RUN_ID` carrier it authenticated (pid + starttime) in the pending abort. Before, they were dropped (`ab.escapees = {}`).
  - Later passes kill them by pid + starttime and count them as live, with no env rescan. So a member that execs with a cleared env (same pid/starttime) is still targeted.
  - The identities are persisted durably as a seen.json `unusable` marker (`reason: spawn_abort_pending`, `prior_members`; tmp + fsync + rename + dir fsync). It is rewritten when the known set grows. After a restart, boot recovery and the close gate target them.
  - **Tests (real runwrap, EPERM on the first kills, escapee execs `sleep` with `{}` env):**
    - `test_r9_authenticated_escapee_retried_by_identity`: escapee killed before the run closes spawn_failed.
    - `test_r9_authenticated_escapee_after_restart`: memory dropped and the wrapper reaped, then `recover()` plus watch passes. The escapee is terminated, and the run never closes while it lives.
- **P1-b: live merge_test step with an unknown first starttime.**
  - The identity is filled from wrapper.json (pid must match, starttime > 0, up to 2 s).
  - If it is still unknown, the step fails closed (`error: wrapper_identity_unknown`, never `passed`). Its group members are terminated by pid + starttime: the wrapper is still the unreaped child, so the pgid is reserved.
  - **Tests:** `test_r9_merge_step_fills_identity_from_wrapper_json` and `test_r9_merge_step_unknown_identity_fails_closed`. Both use a real runwrap with an env-less same-group member, and in both the member is terminated.
- **P1-c: an OSError inside the abort escaped.**
  - The `_identity_live` checks in the kill pass are guarded; an error counts as "alive".
  - An exception from `_abort_spawned` keeps a pending abort with the wrapper identity and the lease.
  - `retry_spawn_aborts` catches errors per run and retries.
  - **Test:** `test_r9_abort_error_keeps_pending_abort`.
- **Reaper:** `children.hold()`/`unhold()`. `reap_orphans` never reaps a pending abort's wrapper, so its pid keeps the pgid reserved. Asserted in `test_r8_survivors_keep_lease_and_retry`.
- **Known limitations, documented only:**
  - Quest P2: `task cancel` during a pending spawn abort leaves the task `cancelling` until the abort finishes and closes it `spawn_failed`.
  - Quest round-8 P3s.
- **Test-harness incident:** a first draft of the survivors test left `children.get_proc_stat` patched while it called `reap_orphans()`. The patch recorded pid 2 (the container's entrypoint) as a test group, and the cleanup SIGKILLed it, which restarted the base container twice (/tmp wiped). Fixes:
  - the patch now fails only the first read;
  - `_cleanup` never touches pgid ≤ 2 or the test's own group;
  - new-test runs used a kill guard (sitecustomize refusing pids/pgids of tini, entrypoint, sshd and ttyd), with no hits after the fix.
  - Product code was not involved.
- **Mutations killed:** A1 (escapees not retried by identity), A2 (pending abort drops escapees), A3 (not persisted), B1 (no wrapper.json fill), B2 (unknown identity passes), C1 (abort exception not caught), H1 (reaper ignores held pids).

Round 9 runs: test_m3_gatefix 52/52 OK (kill guard on). Targeted once (18 modules + test_execution_launch + test_execution_launch_fences, 301 tests): rc 0, 314 s. Full `bash scripts/test.sh` once, base: rc 0, 754/282/49, 93+392+41 s, **527 s** (slower than round 8 at 490 s; the container had just restarted).

## Round 10 (Linear re-review of 4c92374: REVISE, four P1s; root cause of 1-3 fixed once)
- **One cumulative identity set (`_SpawnAbort.known`).**
  - Every identity the abort ever authenticates goes in: group-scan members and `MESH_RUN_ID` carriers, pid + starttime, pid > 2, known starttime only. (In round 10 this was `dict[pid, starttime]` via `setdefault`; round 11 upgrades it and `prior_members` to `set[(pid, starttime)]` so PID reuse never drops or overwrites a generation.)
  - It never shrinks. An entry stops counting only when confirmed gone. A recheck error means "uncertain" and keeps the entry live, and the abort cannot finish while any entry is uncertain.
  - Every pass: collect, SIGKILL the group, SIGKILL every known identity by pid + starttime, collect again.
  - **Persistence:** `_persist_spawn_abort_known` attempts to write the set durably (`spawn_abort_pending` marker, `prior_members`) **before the first signal** (`_abort_spawned` collects first) and after every growth, inside the first ≤5 s pass as well as on retries. If the marker write fails, the error is logged and signalling still proceeds (nothing withholds `SIGKILL` on a disk failure; `_SpawnAbort.known` stays in memory and the DB identity row is already written).
  - The run row gets `wrapper_pid`/`pgid`/`proc_starttime` **before** the abort starts. So a crash at any point leaves a run that boot recovery and the close gate can see. The old handoff from bare pids to `{pid: 0}`, which filtered entries out, is gone.
- **(1) Repro** `test_r10_group_member_leaves_group_after_refused_kill`: an env-less member that is only in the group (its intermediate sh exits, so it is linked to the run by neither env nor PPid chain). Group SIGKILL and its own kill are refused (EPERM), then it calls setsid(). The watch kills it by identity; the run closes spawn_failed only after it is dead.
- **(2)** `test_r10_recheck_error_keeps_identity`: once the wrapper is dead, the member's recheck raises EIO and its kill is refused. The run stays open; it closes only after the member is dead.
- **(3) Repro** `test_r10_crash_after_first_pass_then_recovery`: meshd "dies" (BaseException) right after the first abort pass, before any pending abort exists. The wrapper is reaped as by init and memory is dropped. Full `recover()` plus watch passes: the persisted member is terminated, and the run never closes while it lives. The test also asserts the DB identity and the marker before the crash.
- **(4) Merge step registration failure.**
  - `children.popen_registered` now holds the pid from the orphan reaper inside the spawn window before raising `SpawnedThenFailed`.
  - `merge.run_test_step` catches it and runs the normal step on the unreaped handle: exit wait, leftover scan and termination before the reap, then the reap and unhold. Before, it became `git_failed` with the wrapper and member left alive. `out.registration_error` records the failure.
  - **Test** `test_r10_merge_registration_failure_runs_cleanup`: member terminated, wrapper reaped, not held.
- **Mutations killed:**
  - V1: group-scan identities not kept.
  - V2: recheck error counts as gone.
  - V3: identities not persisted.
  - V4: no DB identity before the abort.
  - V5: merge `SpawnedThenFailed` not handled.
  - V6: known identities not killed by pid.
- New tests ran under the kill guard (refuses tini, entrypoint, sshd and ttyd); 0 hits.

Round 10 runs: test_m3_gatefix 54/54 OK (kill guard on). Targeted once (20 modules, 305 tests): rc 0, 315 s. Full `bash scripts/test.sh` once, base: rc 0, 758/282/49, 93+355+35 s, **483 s**.

## Round 11 (Carbon + Linear reviews on `fe28f6f`; branch `wt/m3-gf11`)

Commits on `wt/m3-gf11` (above `fe28f6f`):
- `fa082d3`: `fix(m3-gatefix r11): thread-group liveness for leader-zombies and EACCES/EIO as unknown (ACC: N18, A8)`
- `6fb43ae`: `fix(m3-gatefix r11): unified post-Popen spawn abort, cumulative (pid, starttime) set, and termination retry on close errors (ACC: N18, A8)`
- `97d6167`: `fix(m3-gatefix r11): enforce adapter.* authz, user-only --binary, off-loop probes, and dry_run (ACC: N1, N2, N7, N10, N18, A8)`
- `4010827`: `test(m3-gatefix r11): pass authenticated user context in test_adapters unit handler call (ACC: N7)`

### Item 1 — Multi-threaded leader-zombie thread groups (`ACC: N18, A8`)
- **Root cause:** When a multi-threaded Linux process's thread-group leader calls `pthread_exit` without `exit_group`, `/proc/<pid>/stat` reports `state == 'Z'` and `/proc/<pid>/environ` becomes empty while sibling threads under `/proc/<pid>/task/<tid>` stay alive. Raw `state == 'Z'` / `!= 'Z'` checks treated the thread group as dead/exited, letting runs close, leases release, wrappers reap prematurely, or `MESH_RUN_ID` scans miss live thread groups.
- **Shared fix (`mesh/core/proc.py` + callers):**
  - `parse_proc_stat` exposes `num_threads`; `is_tgid_alive(pid, info=None)` checks `/proc/<pid>/task/<tid>/stat` whenever the leader state is `'Z'`/`'X'` (returning `True` if any sibling thread is not `'Z'`/`'X'`, and failing closed as `True` if `/proc/<pid>/task` raises `OSError` while `/proc/<pid>` still exists).
  - `get_proc_environ(pid)` falls back to a live sibling thread's `/proc/<pid>/task/<tid>/environ` when the leader is `'Z'`/`'X'`.
  - All process liveness sites across `mesh/core/proc.py`, `mesh/core/auth.py`, `mesh/runwrap.py`, `mesh/daemon/children.py`, `mesh/daemon/terminate.py`, `mesh/daemon/supervisor.py`, `mesh/daemon/recovery.py`, `mesh/daemon/merge.py`, `tests/smoke/harness.py`, and `scripts/coord/reap_daemons.py` route through thread-group liveness.
- **Audit of intentional remaining `'Z'` checks (`grep -rn "'Z'\|\"Z\"" mesh scripts tests/smoke`):**
  - `mesh/core/proc.py`: `is_tgid_alive` (leader fast-path and per-TID sibling thread check), `get_proc_environ` (leader fast-path before live-TID environ fallback), and docstring explanation.
  - `scripts/coord/reap_daemons.py`: `_tgid_has_live_sibling` and `process_stat` (which promotes `'Z'`/`'X'` with a live sibling thread to `'S'`, so `'Z'` reaches `adhoc_alive`, `decision`, `stop_verified`, and `stop_verified_token` only after the whole thread group has exited), plus `scan_untracked_daemons` (which explicitly checks `_tgid_has_live_sibling`).
  - `mesh/adapters/evidence.py` and `mesh/core/clock.py`: UTC ISO-8601 `'Z'` timestamp suffix, not `/proc` state.
  - `tests/smoke/`: none.

### Item 4 — `EACCES`/`EIO`/malformed `/proc/<pid>/stat` and `/proc/<pid>/environ` (`ACC: N18, A8`)
- **Root cause:** `get_proc_stat` and `get_proc_environ` caught `(OSError, ValueError)` broadly and returned `None`, collapsing transient/permission `/proc` failures (`EACCES`, `EIO`, malformed `stat`) into `ESRCH` ("confirmed dead").
- **Shared fix:** `get_proc_stat` and `get_proc_environ` return `None` only on `(FileNotFoundError, ProcessLookupError)` (`ESRCH`) and propagate non-lookup `OSError` (`PermissionError`, `EIO`; malformed `stat` raises `OSError(errno.EIO, ...)`). Callers in `proc.py`, `auth.py`, `children.py`, `terminate.py`, `supervisor.py`, `recovery.py`, `merge.py`, and `reap_daemons.py` treat `OSError` as unknown/alive (never "gone", never blind signal).
- **TDD evidence (`TestRound11LeaderZombieAndProcUnknown` in `tests/unit/test_m3_gatefix.py`, real processes via `ctypes` `pthread_create`/`pthread_exit`):**
  - `test_r11_mt_leader_zombie_proc_liveness_and_environ`
  - `test_r11_mt_leader_zombie_wrapper_and_leftovers`
  - `test_r11_mt_leader_zombie_reap_daemons_and_smoke_harness`
  - `test_r11_proc_eacces_and_eio_are_unknown_not_gone`
  - **Fail-before:** `Ran 4 tests in 0.421s — FAILED (failures=4)`.
  - **Pass-after:** `Ran 4 tests in 0.399s — OK`.

### Item 5 — Cumulative `(pid, starttime)` set in `_SpawnAbort` and `prior_members` (`ACC: N18, A8`)
- **Root cause:** `_SpawnAbort.known: Dict[int, int]` (`setdefault`) and `prior_members: Dict[int, int]` were keyed by bare `pid`, so PID reuse during an abort or across markers either dropped the newer `(pid, new_starttime)` generation or overwrote the older generation.
- **Shared fix:** `_SpawnAbort.known` is now `Set[Tuple[int, int]]`, and `_prior_member_pairs` / `_load_prior_member_pairs` serialize and reload `prior_members` as `[pid, starttime]` pairs (remaining backward-compatible with legacy dict files) across `_abort_spawned`, `seen.json` markers, `close_gate`, and `recovery._lost`.

### Item 2 — Unified post-`Popen` & TX B spawn failure handling across task runs and lead turns (`ACC: N18, A8`)
- **Root cause:** (a) `lead_turns.start_lead_turn` lacked post-`Popen` spawn abort, releasing `leadturn:<prj>` and failing the turn while wrapper/escaped processes stayed alive; (b) TX B sat outside `try:` in both `supervisor.start_launch_run` and `lead_turns.start_lead_turn`, so TX B DB failures orphaned live processes; (c) closing DB failures after a clean kill dropped `_SPAWN_ABORTS` or left unclosed runs; (d) `supervisor.in_memory_active_runs()` checked only `_WRAPPERS`, omitting `_SPAWN_ABORTS` from `DaemonServer.active_runs`.
- **Shared fix:** Extracted `supervisor.handle_spawn_failure(...)` shared by `start_launch_run` and `start_lead_turn`; moved TX B inside `try:`; kept wrapper un-reaped (`children.hold`), lease held (`task:<id>` / `leadturn:<prj>`), and lead `outbox` rows in `'delivering'` until every spawned/escaped `(pid, starttime)` identity is confirmed dead **and** the closing DB transaction commits via `retry_spawn_aborts`; included `_SPAWN_ABORTS` in `in_memory_active_runs()`.

### Item 6a — `advance_terminations` preserves active record on `CloseHeld` / DB error (`ACC: N18, A8`)
- **Root cause:** `terminate.advance_all` removed `t` from `_ACTIVE` before `supervisor.advance_terminations` called `_finish_terminated`, so `CloseHeld` or a transient DB error dropped the termination record.
- **Shared fix:** Split `terminate.advance_one(t)` from `terminate.forget(run_id)` so `supervisor.advance_terminations` forgets `t` only after `_finish_terminated` succeeds cleanly.
- **TDD evidence for Items 5, 2, 6a (`TestRound11SpawnAbortAndPostPopen` in `tests/unit/test_m3_gatefix.py`):**
  - `test_r11_spawn_abort_cumulative_pid_starttime_pairs_and_prior_members`
  - `test_r11_lead_turn_post_popen_and_tx_b_failure_kills_wrapper_holds_lease_and_outbox`
  - `test_r11_task_spawn_tx_b_failure_and_locked_close_kills_wrapper_and_retries`
  - `test_r11_advance_preserves_termination_end_reason_on_close_held_and_db_error`
  - **Fail-before:** `Ran 4 tests in 0.414s — FAILED (failures=2, errors=2)`.
  - **Pass-after:** `Ran 4 tests in 1.077s — OK`.

### Item 3 — `adapter.*` authorization, user-only `--binary`, off-loop probes, and `dry_run` (`ACC: N1, N2, N7, N10`)
- **Root cause:** (a) `DaemonServer._authenticate_request` permitted `auth.type == "none"` on `adapter.*` and `DaemonServer` did not invoke `check_authz` centrally before idempotency replay or `adapter.*` dispatch; (b) `adapter.show`, `adapter.models`, and `adapter.probe` accepted caller-supplied `binary`/`binary_path` from non-user callers; (c) `adapter.*` ran subprocess probes synchronously on the asyncio event loop inside `BEGIN IMMEDIATE`, stalling `daemon.status` and holding the SQLite write lock; (d) `--dry-run` on `adapter.*` still spawned probe/model subprocesses before rolling back.
- **Shared fix & design decisions:**
  - Narrowed `UNAUTH_CMDS` to `{"daemon.status", "version"}` and called `check_authz(..., resource_resolved=False)` centrally in `DaemonServer` before canonical hashing, `idempotency` lookup/replay, long-poll/stream/merge dispatch, worker-thread `prepare_*` hooks, and `BEGIN IMMEDIATE` (preventing run tokens that share `principal_id` with their agent from replaying cached `adapter.*` responses). Full credential matrix on `adapter.*`: direct user and direct agent token allowed; anonymous `rc=3 TOKEN_MISSING`; active executor/lead run token `rc=4 CAPABILITY_MISSING`; superseded lead run token `rc=9 LEAD_SUPERSEDED`; `--relayed` `rc=4 RELAY_NOT_ALLOWED`.
  - Kept `--binary` on `adapter probe` for direct user only (`actor_kind == "user" and via == "direct"`, `rc=4 CAPABILITY_MISSING` otherwise, validated before touching any path) per M3 plan & probe/smoke suites; removed `--binary` from `adapter models` CLI parser and rejected `binary`/`binary_path` on `adapter.list`, `adapter.show`, and `adapter.models` with `rc=2 INVALID_ARGUMENT`.
  - Added `prepare_adapter_command(ctx, testing_mode)` running `a.probe(..., conn=None)` / `a.list_models()` via `asyncio.to_thread(...)` outside any DB transaction, then persisting `record_adapter_probe(ctx.conn, ...)` inside the short `BEGIN IMMEDIATE` block.
  - On `dry_run`, `prepare_adapter_command` and `handle_adapter_*` validate authz + arguments and return dry-run metadata (`{"would": ...}`) without spawning threads/subprocesses or writing `adapter_probes` / `idempotency`.
- **Existing unauthenticated `adapter.*` test callers updated to pass user credentials:**
  - `tests/unit/test_probe_offline.py`: `TestDaemonProbeBinaryPath.test_daemon_probe_custom_binary_persists_path` (2 `h.cli("adapter", "probe", ..., user=True)` calls).
  - `tests/probe/test_probe.py`: `_daemon_probe` (1 `harness.cli("adapter", "probe", ..., user=True, timeout=120.0)` call).
  - `tests/integration/test_adapter_evidence.py`: `test_override_enables_droid_and_dry_run_create`, `test_corrupt_results_file_blocks_launch_with_warning` (4 `h.cli("adapter", ..., user=True)` calls).
  - `tests/integration/test_adapters.py`: `test_adapter_list_testing_vs_normal`, `test_adapter_probe_and_show_not_installed`, `test_adapter_models`, `test_adapter_probe_cli_live_and_stale`, `test_adapter_probe_error_and_timeout`, `test_adapter_probes_db_persistence` (17 `cli("adapter", ..., user=True)` calls).
  - `tests/unit/test_adapters.py`: `TestAdapterGoldens.test_models_read_no_vendor_config` (2 direct `handle_adapter_models` unit calls updated from bare `SimpleNamespace(args=...)` to explicit direct-user context).
- **TDD evidence (`TestRound11AdapterAuthzBinaryAsyncDryRun` in `tests/unit/test_m3_gatefix.py`):**
  - `test_r11_adapter_authz_matrix_across_all_credentials`
  - `test_r11_adapter_binary_flag_user_only_on_probe_and_rejected_elsewhere`
  - `test_r11_adapter_probe_runs_off_event_loop_and_outside_db_tx`
  - `test_r11_adapter_dry_run_spawns_nothing_and_writes_no_rows`
  - **Fail-before:** `Ran 4 tests in 4.350s — FAILED (failures=4)`.
  - **Pass-after:** `Ran 4 tests in 5.462s — OK`.

### Item 6 — Known Limitations (documented, out of Round 11 fix scope)
- **Carbon P2-2 (`agent update` ignored flags / `--strict-models`):** `mesh agent update` silently ignores unimplemented update flags and `--strict-models` is not yet implemented. Deferred outside M3 gatefix scope.
- **Carbon P3 (`kiro` / `agy` prompt separator):** `kiro` and `agy` adapter command builders pass the positional prompt argument without a `--` end-of-options separator (prompts starting with `-` could be parsed as flags; adding `--` requires live vendor CLI smoke verification to ensure neither vendor CLI treats `--` literally). Deferred as a known limitation.

### Round 11 Targeted Runs (sequential, `PYTHONPATH=/home/node/gf-guard`, `timeout 600`)
- **Unit (26 modules, 401 tests, 1 skipped — all `OK`):** `test_m3_gatefix` (66), `test_probe_offline` (7), `test_projects` (10), `test_skeleton` (14), `test_adapters` (31), `test_auth` (5), `test_doctor_fail_closed` (21), `test_fake_agent_behaviors` (6), `test_home_resolution_r3` (13), `test_lead_supersede` (1), `test_lead_turns` (2), `test_merge` (21), `test_merge_recovery_minors` (7), `test_proc_probe` (3), `test_reap_daemons` (53), `test_recovery_units` (7), `test_runtargets_race` (43), `test_runwrap` (3), `test_seen_hold_r4d` (4), `test_seen_hold_r4e` (3), `test_settings` (5), `test_smoke_cases` (15), `test_smoke_harness` (14), `test_termination_units` (13), `test_test_kdf` (27, 1 skipped), `test_vendor_failure_meaning` (7).
- **Integration (19 modules, 176 tests — all `OK`):** `test_adapters` (6), `test_adapter_evidence` (3), `test_daemon` (25), `test_execution_acc_run2` (17), `test_execution_launch` (5), `test_execution_launch_fences` (9), `test_execution_termination` (16), `test_lead_actions` (5), `test_lead_turns` (8), `test_merge_tests` (5), `test_read_verbs` (4), `test_run_release_seen` (3), `test_test_kdf` (2), `test_vendor_lead_gate` (3), `test_projects` (17), `test_agents` (11), `test_approvals` (22), `test_vendor_exit_meaning` (5), `test_merge` (10).
- **Recovery (4 modules, 49 tests — all `OK`):** `test_recovery` (15), `test_recovery_adopt_seen` (16), `test_recovery_lead_merge` (16), `test_recovery_merge` (2).
- **Total:** 49 targeted modules, 626 tests (`0` failures, `0` errors, `1` skipped), `0 BLOCKED` in `guard.log`. Full `scripts/test.sh` and A1 timing intentionally not run per Round 11 coordinator rules.


### Round 11 coordinator follow-up (Quest coder, on base after Carbon finished)
- Carbon's notes named tests that do not exist; the names above are corrected to the real ones.
- Added `test_r11_linear_eacces_on_final_recheck_is_unknown`: the exact Linear r10 P1-1 repro at run level (env-less member, kills refused, one injected `PermissionError(EACCES)` from `get_proc_stat` on the member's recheck after the wrapper died). The run stays open with its lease and closes `spawn_failed` only after the member is dead.
- Added `test_r11_merge_step_leader_zombie_member_is_leftover`: Carbon P1-1 repro (b) end to end. `merge.run_test_step` with an env-less same-group member whose main thread called `SYS_exit` while a worker thread writes. Leftover found and terminated, worker stops writing.
- Commit authorship rewritten to `Quest (coder)` (Carbon's tool policy blocked `-c user.*`, so its commits carried the base's default identity).
- **Mutations (each run against `tests.unit.test_m3_gatefix`, kill guard on), all killed:**
  - W1: EACCES read as gone.
  - W2: a leader-zombie with live threads counted dead.
  - W3: no central `check_authz`.
  - W4: `--binary` allowed for any caller.
  - W5: known set keyed by pid only.
  - W2b: W2 against the new merge test alone.

Round 11 runs (base, idle, Carbon not running): targeted 28 modules, 400 tests, OK, 428 s (kill guard on, 0 blocks); full `bash scripts/test.sh` once: rc 0, see log `~/gf-logs/full11.log`, **507 s**.


## ClickUp P1-1 (scan errors read as empty)

- **Fix & Tests Commit:** `c75ef1b`

### Root Cause
- `supervisor._scan_group` swallowed `OSError` from `os.listdir("/proc")`, `get_proc_stat(pid)`, and `is_tgid_alive(pid, info)`, returning `{}` ("nobody").
- `terminate.run_targets` likewise swallowed `OSError` from `os.listdir("/proc")`, `get_proc_stat(pid)` / `is_tgid_alive(pid, info)` for pids not already in `seen`, `_wrapper_identity_alive`, `_session_anchored`, `_descends_from`, and hard `/proc/<pid>/environ` read errors (`EIO`/`EMFILE`/`ENFILE`/`ENOMEM`), returning `{}` when no other target was readable.
- `merge._live_wrapper` caught `OSError` and returned `(None, None)`.
- As a result, when a wrapper exited and was reaped while an unrecorded env-less same-group member (not in `seen.json`) hit `EACCES`, `EIO`, a malformed `/proc/<pid>/stat` (`OSError(EIO)`), or `EMFILE` on `os.listdir("/proc")`, `close_gate`, `ClearCheck.observe`, `terminate.step`, `recovery._lost`, `merge_close_gate`, `recover_merge_runs`, and `merge.run_test_step` treated the failed scan as an empty process group — closing the run, releasing `task:<id>` or `merge:<prj>`, trusting a partial `seen.json`, or passing a merge test step while the member remained alive. (Live `task.merge` `_final_tx` holding `merge:<prj>` on uncertain scans, same-uid `EACCES`/`EPERM` on `/proc/<pid>/environ`, and transient `OSError` in `wrapper_alive` / `recovery._identity_alive` were closed subsequently in `## Carbon RO P1-1..P1-3 (round p1b)` below.)

### Fix & Every Changed Caller
- **`mesh/core/proc.py`:**
  - Added `ScanResult(dict)` carrying `uncertain: bool = False`. Empty `ScanResult(uncertain=True)` preserves `isinstance(..., dict)`, `bool(res) == False`, and `res == {}` for plain dict consumers while exposing `res.uncertain` to scan-aware callers. `find_run_processes` is left untouched (only used in unit tests).
- **`mesh/daemon/supervisor.py`:**
  - `_scan_group`: returns `ScanResult()`; sets `found.uncertain = True` on `os.listdir("/proc")` `OSError` and on per-pid `get_proc_stat(pid)` / `is_tgid_alive(pid, info)` `OSError` while continuing to scan remaining pids.
  - `close_gate`: when `group_still_ours(pgid, wst)`, inspects `scanned = _scan_group(pgid)` and sets `occupied = bool(scanned) or getattr(scanned, "uncertain", False)`, raising `CloseHeld(..., occupied=True)` so `_close_run` holds the run open and keeps the lease for retry on the next tick (zero blind signals).
  - `_group_occupied`: returns `bool(scanned) or getattr(scanned, "uncertain", False)`, so `snapshot_hold` and `seen_hold` never release on an uncertain scan.
  - `track_adopted_seen`: when `getattr(scanned, "uncertain", False)`, resets `_SEEN_TICKS[run_id] = tick` (immediate retry on the next watch tick), calls `flush_seen(run, home)`, and returns `False`, so `establish_adopted_seen` writes the `unusable` (`adopt_scan_untrusted`) marker and returns `adopted_uncertain`, and periodic rescans never overwrite a marker or drop live members on a partial scan.
  - `_abort_collect`: marks `ok = False` when `_scan_group` or `terminate.run_targets` reports `uncertain=True`, so `_abort_kill_pass` keeps retrying instead of declaring a failed spawn cleaned up.
  - `_watch_task_run` & `_watch_lead_turn`: when `leftovers` is empty and `leftovers.uncertain` is `True` in `_watch_task_run`, invalidates `_clear_check`, renews `task:<id>`, and returns `None` before `snapshot_hold` or `_reap_wrapper` (keeping the zombie wrapper unreaped so its pgid stays trusted on the next tick, and returning `"exit_deferred"` during `recovery._recover_run`). When `leftovers` is non-empty, skips immediate `_reap_wrapper` while `leftovers.uncertain` or `t.scan_uncertain` holds (deferring reap to `_finalize_termination`).
- **`mesh/daemon/recovery.py`:**
  - `_lost`: checks `if scanned or getattr(scanned, "uncertain", False)` on the `group_still_ours` scan (holding via `hold_wrapper_gone` + `_terminate_group(..., {})` -> `"terminating"`, with zero blind signals), and checks `if carriers or getattr(found, "uncertain", False) or getattr(env_scan, "uncertain", False)` before `_close_without_process` (entering `_terminate_carriers` -> `"terminating"` instead of closing `lost` and releasing the lease).
- **`mesh/daemon/terminate.py`:**
  - `Termination`: added `scan_uncertain: bool = False`.
  - `ClearCheck.observe`: resets `self.first_empty_at = None` and returns `False` when `found or getattr(found, "uncertain", False)`.
  - `_wrapper_identity_alive`, `_session_anchored`, `_descends_from`: accept `errors: Optional[List[bool]] = None`, recording `OSError` while still returning `False` so unverified group/ancestry is never blind-signalled.
  - `run_targets`: returns `ScanResult()`; sets `found.uncertain = True` on `os.listdir("/proc")` `OSError`, non-excluded per-pid `get_proc_stat` / `is_tgid_alive` `OSError`, `get_proc_environ` `OSError` with `errno` in `(EIO, EMFILE, ENFILE, ENOMEM)`, or helper `errors`.
  - `run_targets_or_recheck`: preserves uncertainty (`ScanResult(second, uncertain=True)`) if the first scan was empty+uncertain and the recheck was empty, preventing a single clean scan after an uncertain scan from counting as two clean scans.
  - `begin` & `step`: records `t.scan_uncertain = getattr(live, "uncertain", False)`; in `step(t)`, only enters the two-empty-scan `"done"` path when `not live and not uncertain`, otherwise resetting `t.empty_scan_at = None` and advancing deadlines without blind-signalling.
- **`mesh/daemon/merge.py`:**
  - `merge_close_gate`: checks `scanned = supervisor._scan_group(int(pgid))` and returns only when `not scanned and not getattr(scanned, "uncertain", False)`, otherwise raising `MergeRunHeld` (holding the `merge_test` run and `merge:<prj>` lease).
  - `run_test_step`: when `leftovers or getattr(leftovers, "uncertain", False)`, drives `_terminate_test` before reaping `proc` (so a transient scan error still terminates same-group leftovers while the wrapper pgid remains trusted) and sets `out["error"] = "leftover_scan_uncertain"` (`out["passed"] == False`); retries `_scan_group` once on uncertainty in the `starttime is None` fallback. (Note: live `run_task_merge` `_final_tx` holding `merge:<prj>` on `scan_uncertain` / `leftover_scan_uncertain` and direct `proc` kill on timeout under `/proc` failure were added in round `p1b` below.)
  - `_live_wrapper`: removed `except OSError: return None, None` so `OSError` propagates to `recover_merge_runs` (blocking premature closure and recording `merge.recovery_blocked`).
  - `recover_merge_runs`, `_step_terminations`, `_close_recovered_merge_run`: raises `OSError(EIO, ...)` when `pid is None` and `carriers` is empty with `carriers.uncertain=True`; propagates `t.scan_uncertain` into `out[rid]["scan_uncertain"]` on `kill_failed`; and refuses `_close_recovered_merge_run` when `termination.get("scan_uncertain")` (invoking `merge_close_gate` / raising `OSError(EIO, ...)` instead of releasing `merge:<prj>`).

### Tests (`tests/unit/test_m3_gatefix.py` — `TestClickUpP1ScanUncertain`, ACC: N18, A8)
- `TestClickUpP1ScanUncertain.test_task_close_gate_eacces_holds_until_member_gone`
- `TestClickUpP1ScanUncertain.test_task_close_gate_eio_holds_until_member_gone`
- `TestClickUpP1ScanUncertain.test_task_close_gate_malformed_stat_holds_until_member_gone`
- `TestClickUpP1ScanUncertain.test_task_close_gate_emfile_listdir_holds_until_member_gone`
- `TestClickUpP1ScanUncertain.test_adopt_and_recovery_lost_scan_error_never_trusts_seen_or_loses_run`
- `TestClickUpP1ScanUncertain.test_run_targets_scan_error_uncertain_holds_exit_and_termination`
- `TestClickUpP1ScanUncertain.test_merge_close_gate_scan_error_holds_run_and_lease`
- `TestClickUpP1ScanUncertain.test_merge_test_step_leftover_scan_error_not_passed`

### Fail-Before / Pass-After & Targeted Verification
- **Fail-before (`TestClickUpP1ScanUncertain` against unedited `mesh/`):**
  - `Ran 8 tests in 2.323s — FAILED (failures=8)`:
    - `test_task_close_gate_eacces_holds_until_member_gone`: `AssertionError: 'exited' != 'running' : run closed while scan uncertain and member alive`
    - `test_task_close_gate_eio_holds_until_member_gone`: `AssertionError: 'exited' != 'running' : run closed while scan uncertain and member alive`
    - `test_task_close_gate_malformed_stat_holds_until_member_gone`: `AssertionError: 'exited' != 'running' : run closed while scan uncertain and member alive`
    - `test_task_close_gate_emfile_listdir_holds_until_member_gone`: `AssertionError: 'exited' != 'running' : run closed while scan uncertain and member alive`
    - `test_adopt_and_recovery_lost_scan_error_never_trusts_seen_or_loses_run`: `AssertionError: 'adopted' != 'adopted_uncertain'`
    - `test_run_targets_scan_error_uncertain_holds_exit_and_termination`: `AssertionError: False is not true : run_targets did not report uncertainty on EACCES`
    - `test_merge_close_gate_scan_error_holds_run_and_lease`: `AssertionError: 1 != 0` (`merge_runs_lost: 1`, `merge_runs_held: 0`)
    - `test_merge_test_step_leftover_scan_error_not_passed`: `AssertionError: True is not false : merge test step passed despite leftover scan EIO`
- **Pass-after (`TestClickUpP1ScanUncertain`):**
  - `Ran 8 tests in 6.938s — OK`
- **Sequential Targeted Runs (`PYTHONPATH=/home/node/gf-guard`, `timeout 600`, one module at a time):**
  1. `tests.unit.test_m3_gatefix` — 76 tests (`41.476s`) `OK`
  2. `tests.unit.test_runtargets_race` — 43 tests (`6.595s`) `OK`
  3. `tests.unit.test_termination_units` — 13 tests (`1.977s`) `OK`
  4. `tests.unit.test_merge` — 21 tests (`2.294s`) `OK`
  5. `tests.unit.test_seen_hold_r4d` — 4 tests (`2.702s`) `OK`
  6. `tests.unit.test_seen_hold_r4e` — 3 tests (`0.628s`) `OK`
  7. `tests.integration.test_execution_termination` — 16 tests (`91.523s`) `OK`
  8. `tests.integration.test_run_release_seen` — 3 tests (`12.746s`) `OK`
  9. `tests.integration.test_merge_tests` — 5 tests (`7.217s`) `OK`
  10. `tests.recovery.test_recovery_adopt_seen` — 16 tests (`16.555s`) `OK`
  11. `tests.recovery.test_recovery_merge` — 2 tests (`7.157s`) `OK`
  - **Total:** 11 targeted modules, 202 tests (`0` failures, `0` errors), `0 BLOCKED` in `guard.log`.

### ClickUp P1-1: Quest verification

Mutation check of `TestClickUpP1ScanUncertain`, run by the Quest coder on base with the kill guard on. Each line names the mutation and the tests that failed:
- **M1**, `close_gate` ignores uncertain: killed by the 4 `test_task_close_gate_*` tests.
- **M3**, `merge_close_gate` ignores uncertain: killed by `test_merge_close_gate_scan_error_holds_run_and_lease`.
- **M4**, `ClearCheck.observe` ignores uncertain: killed by `test_run_targets_scan_error_uncertain_holds_exit_and_termination`.
- **M5**, merge-step leftover uncertain branch removed: killed by `test_merge_test_step_leftover_scan_error_not_passed`.
- **M6**, `run_targets` drops `scan_uncertain`: killed by `test_merge_test_step_leftover_scan_error_not_passed`.
- **M2**, `_group_occupied` ignores uncertain: survived. The Quest coder added `TestClickUpP1SeenHoldUncertain.test_seen_hold_uncertain_scan_counts_as_occupied` (`snapshot_hold` and `_group_occupied` with an uncertain scan). The test fails on M2 and passes on the fix.

### ClickUp recheck (ACCEPT on `66b0417`): pre-merge batch (Quest coder)

- **P2, merge step with unknown wrapper identity:** when both `_scan_group` calls came back empty and uncertain, the merge step got `members={}` and ran no termination, and `_final_tx` released `merge:<prj>`. Now the scan is retried every `POLL_S` while it stays uncertain, bounded by the kill grace (minimum 1 s). A certain scan terminates the members it finds. A scan that is still uncertain runs `_terminate_test(seen={})` and sets `leftover_scan_uncertain`, and the termination is marked `kill_failed` + `scan_uncertain` (never a pass; in `5b5b11e` `_final_tx` still released `merge:<prj>` under L12/D25, superseded in round `p1b` below so uncertain scans hold `merge_test` open and keep `merge:<prj>`).
  - New tests in `TestClickUpRecheckMergeUnknownIdentityUncertain`:
    - `test_merge_step_unknown_identity_transient_emfile_rescans_and_terminates`
    - `test_merge_step_unknown_identity_persistent_emfile_is_kill_failed_uncertain`
  - EMFILE is injected only inside the identity-unknown group scan. Both tests fail on `66b0417` and pass with the change.
- **P3:** `mesh/core/proc.py` `find_run_processes`, used only by tests and the smoke harness, now returns `ScanPids` (a list with `uncertain`) instead of swallowing errors as empty.
- Docs: L12 extended (subsequently narrowed in `p1b` to confirmed survivors only), L14 (narrowed in `p1b` via start-time-scoped same-uid check), L15 (hidepid), the Quest review P3s, and the §5a ClickUp entry. §5a now comes before §5b.


## Carbon RO P1-1..P1-3 (round p1b)

- **Review addressed:** `/home/node/workspace/review-inbox/m3-clickup-p1-carbon.md` (Carbon adversarial read-only review on `66b0417`, `REVISE`: P1-1, P1-2, P1-3; P2-1 docs; P3-1).

### Root Cause
- **P1-1 (`mesh/daemon/merge.py` — live `task.merge` `run_test_step` -> `_pipeline_and_finish` / `_final_tx`):**
  - **Subcase 2A (`run_targets_or_recheck` uncertain on `EACCES`/`EIO`/`EMFILE`):** unlike `_close_recovered_merge_run` (which blocked close on `termination.get("scan_uncertain")` and ran `merge_close_gate`), live `_final_tx` ignored `scan_uncertain` and `leftover_scan_uncertain`, skipped `merge_close_gate`, marked the `merge_test` run `'exited'` (`tests_failed`), emitted `run.kill_failed` (`pids: []`, `lease_released: true`), and deleted `merge:<prj>` after `run_test_step` reaped `proc` while an unsignalled same-group `env={}` leftover remained alive.
  - **Subcase 2B (`starttime is None` fallback + uncertain group scan):** `members = {p: st for p, st in group_scan.items() ...}` stripped `group_scan.uncertain`, and live `_final_tx` closed `merge_failed` and deleted `merge:<prj>` despite `leftover_scan_uncertain: True` / `scan_uncertain: True`.
  - **Subcase 2C (`test_timeout_s` expiry under `/proc` `EMFILE`):** when `os.listdir("/proc")` raised `EMFILE` on timeout, `_terminate_test` sent zero signals even to `proc.pid` (`mesh.runwrap`), `finally:` only called non-blocking `proc.poll()`, and `_final_tx` closed the run `'killed'` (`timeout`) and deleted `merge:<prj>` while both `mesh.runwrap` and the foreground test child continued actively running in the integration worktree.
- **P1-2 (`mesh/daemon/terminate.py` — `run_targets` & `recovery._env_carriers`):**
  - **Subcases 3A & 3B (same-uid `EACCES`/`EPERM` on `get_proc_environ`):** `run_targets` only set `found.uncertain = True` for `errno in (EIO, EMFILE, ENFILE, ENOMEM)` on `get_proc_environ(pid)`. Both a transient `PermissionError(EACCES)` on a same-uid `setsid` `MESH_RUN_ID` carrier and a zero-mock real-kernel `setsid()` + `prctl(PR_SET_DUMPABLE, 0)` escapee (`st_uid == geteuid()`, `get_proc_environ` -> `PermissionError(13, EACCES)` on `linux 4.19.0-gvisor`) returned `{}` with `uncertain=False`, closing the run `'exited'` and deleting `task:<id>` while the carrier lived.
  - **Subcase 3C (`_same_uid`, `_is_kthread`, `_now_ticks` swallowing `OSError`):** `_same_uid(pid)`, `_is_kthread(pid, info)`, and `_now_ticks()` caught `OSError` (`EMFILE`/`EIO`/`EACCES` on `os.stat("/proc/<pid>")`, `get_proc_stat`, or `/proc/uptime`) and returned `False` / `None` without recording uncertainty, disabling `_environ_unknown` during the gVisor `environ == {}` exec-lag window with `uncertain=False`.
- **P1-3 (`mesh/daemon/supervisor.py` & `mesh/daemon/recovery.py` — transient `OSError` on wrapper stat):**
  - **Subcase 4A (`supervisor.wrapper_alive`):** `except OSError: return False` collapsed a single transient `EIO`/`EACCES`/`EMFILE` on `get_proc_stat(wrapper_pid)` or `is_tgid_alive` into `"wrapper is dead"`, causing `_watch_task_run` and `_watch_lead_turn` to treat the live running wrapper + child as `"leftovers"`, `SIGTERM`/`SIGKILL` the healthy run, and fail the task / turn.
  - **Subcase 4B (`recovery._identity_alive`):** `except OSError: return False` at boot recovery caused `_recover_run` to skip `_adopt`, fall through to `_lost(..., "lost_on_restart")`, terminate the live wrapper + child, and close the run `lost`.

### Fix & Every Changed Caller
- **`mesh/daemon/merge.py`:**
  - `_kill_test_proc_direct(proc, grace_s, force_group_kill=False)`: directly signals `os.killpg(proc.pid, SIGTERM)` -> wait `grace_s` -> `os.killpg(proc.pid, SIGKILL)` -> wait `KILL_WAIT_S` on `meshd`'s own unreaped `start_new_session=True` child (`proc.returncode is None`, `proc.pid > 2`, `proc.pid != os.getpgrp()`) when `/proc` fails on timeout.
  - `run_test_step`:
    - On timeout: runs `_terminate_test` first, records `out["leftover_scan_uncertain"] = True` when `scan_uncertain`, and calls `_kill_test_proc_direct(proc, grace, force_group_kill=bool(scan_uncertain))` when `not _exited_unreaped(proc)` or `scan_uncertain` (plus direct-kill fallback before setting `out["wrapper_alive"] = True`).
    - On normal-exit leftovers: sets `out["leftover_scan_uncertain"] = True` when `out["termination"].get("scan_uncertain")` holds (so transient initial leftover-scan errors resolved cleanly by `_terminate_test` still fail the step via `out["error"] = "leftover_scan_uncertain"` without wedging `merge:<prj>`).
    - On `starttime is None`: preserves `ScanResult(..., uncertain=bool(getattr(group_scan, "uncertain", False)))` and marks `leftover_scan_uncertain`, `kill_failed`, and `scan_uncertain` when uncertain.
  - `run_merge_pipeline`: copies `"leftover_scan_uncertain"` into failure `details`.
  - `merge_close_gate`: made transaction-aware (`if conn.in_transaction:` vs `with transaction(conn):`).
  - `_check_live_merge_close(conn, plan, result)`: raises `MergeRunHeld` if `termination.get("scan_uncertain")` or `test.get("leftover_scan_uncertain")` or `details.get("leftover_scan_uncertain")`, and calls `merge_close_gate(conn, ...)` unless confirmed survivors (`kill_failed` and `not scan_uncertain`) already trigger D25.
  - `_pipeline_and_finish` & `_final_tx`: calls `_check_live_merge_close` before closing; on `(MergeRunHeld, OSError)`, records `merge.recovery_blocked`, keeps `tasks.status = 'merging'`, `runs.status = 'running'`, `runs.pgid = wpid`, and `merge:<prj>` held, and raises `MeshError(INTERNAL_ERROR, ...)`. Restricts `run.kill_failed` D25 lease-release event in `_final_tx` to `kill_failed and not uncertain`.
- **`mesh/daemon/terminate.py`:**
  - Added `_SCAN_ERRORS: contextvars.ContextVar[Optional[List[bool]]]` and `_record_scan_error(errors, exc)` so `_now_ticks()`, `_young_from(wrapper_starttime)`, `_is_kthread(pid, info)`, `_same_uid(pid)`, and `_environ_unknown(env, pid, info, young_from)` preserve their exact call signatures while recording non-`ENOENT`/`ESRCH` `OSError` into `run_targets`'s `errors` list (`found.uncertain = True`).
  - `run_targets` / `_run_targets_inner`: computes `eacces_from = int(wrapper_starttime) if wrapper_starttime is not None and int(wrapper_starttime) > 0 else young_from`; when `get_proc_environ(pid)` raises `EACCES`/`EPERM` on a non-excluded process with `_same_uid(pid)` and `not _is_kthread(pid, info)` and (`eacces_from is None or st >= eacces_from`), sets `found.uncertain = True` while ignoring older host bystanders (`st < eacces_from`).
- **`mesh/daemon/supervisor.py`:**
  - `wrapper_liveness(run) -> str`: returns `"alive"`, `"gone"` (`pid`/`starttime` missing or `<= 0`, `ENOENT`/`ESRCH`, starttime mismatch, or `is_tgid_alive == False`), or `"uncertain"` (non-lookup `OSError` from `get_proc_stat` / `is_tgid_alive`). `wrapper_alive(run)` returns `wrapper_liveness(run) == "alive"`, and `_effective_wrapper_liveness(run)` honors unit-test monkeypatches of `supervisor.wrapper_alive` while using tri-state `wrapper_liveness(run)` in production.
  - `track_adopted_seen`: checks `_effective_wrapper_liveness(fresh) != "alive"`, resetting `_SEEN_TICKS[run_id] = tick`, flushing seen, and returning `False` on `"uncertain"` (never trusting an unverified anchor).
  - `_watch_task_run` & `_watch_lead_turn`: on `liveness == "uncertain"`, invalidates `_clear_check(run["id"])`, skips leftover scans / reap / close, still enforces `termination_reason` / `lead_turn_termination_reason` (deadlines and cancellations), renews the lease, and returns `None`.
  - `_abort_collect`: passes `ab.wrapper_starttime` into `terminate.run_targets(ab.run_id, None, None, ab.wrapper_starttime)`.
- **`mesh/daemon/recovery.py`:**
  - `_identity_alive(pid, starttime)`: requires `pid > 1 and starttime > 0` and removes `except OSError: return False`, letting non-lookup `OSError` propagate to `_step_runs` (`run_errors += 1`, run row and lease untouched).

### Tests (`tests/unit/test_m3_gatefix.py` — `TestCarbonRoP1b`, ACC: N18, A8)
- `TestCarbonRoP1b.test_p1_1_2a_live_merge_eacces_holds_run_and_merge_lease`
- `TestCarbonRoP1b.test_p1_1_2b_live_merge_unknown_starttime_scan_error_holds_lease`
- `TestCarbonRoP1b.test_p1_1_2c_live_merge_timeout_under_emfile_kills_wrapper_and_child_and_holds`
- `TestCarbonRoP1b.test_p1_2_3a_same_uid_environ_eacces_marks_uncertain_and_holds`
- `TestCarbonRoP1b.test_p1_2_3b_zeromock_prctl_nondumpable_escapee_held_and_old_bystander_ignored`
- `TestCarbonRoP1b.test_p1_2_3c_same_uid_and_now_ticks_oserror_mark_uncertain`
- `TestCarbonRoP1b.test_p1_3_watch_transient_wrapper_stat_eio_preserves_live_run_and_enforces_deadline`
- `TestCarbonRoP1b.test_p1_3_recovery_transient_wrapper_stat_eio_records_error_without_losing_run`
- `TestCarbonRoP1b.test_p1_3_watch_lead_turn_transient_wrapper_stat_eio_preserves_live_turn`

### Fail-Before / Pass-After & Targeted Verification
- **Kernel probe (`prctl(PR_SET_DUMPABLE, 0)` on `linux 4.19.0-gvisor`):** `libc.prctl(4, 0, 0, 0, 0)` returned `0`, `get_proc_stat(pid)` succeeded (`uid == geteuid()`), and unmocked `get_proc_environ(pid)` raised `PermissionError(13, EACCES)`.
- **Fail-before (`TestCarbonRoP1b` initial 8 tests against unmodified `5b5b11e` `mesh/`):**
  - `Ran 8 tests in 38.610s — FAILED (failures=8)`:
    - `test_p1_1_2a_live_merge_eacces_holds_run_and_merge_lease`: `AssertionError: 'TESTS_FAILED' != 'INTERNAL_ERROR'` (closed `merge_failed` and released `merge:prj_m` despite `scan_uncertain: True`)
    - `test_p1_1_2b_live_merge_unknown_starttime_scan_error_holds_lease`: `AssertionError: 'TESTS_FAILED' != 'INTERNAL_ERROR'` (`wrapper_identity_unknown` + `scan_uncertain: True` closed `merge_failed` and released `merge:prj_m`)
    - `test_p1_1_2c_live_merge_timeout_under_emfile_kills_wrapper_and_child_and_holds`: `AssertionError: True is not false : mesh.runwrap survived timeout under /proc EMFILE!`
    - `test_p1_2_3a_same_uid_environ_eacces_marks_uncertain_and_holds`: `AssertionError: False is not true : run_targets ignored EACCES on young same-uid carrier!`
    - `test_p1_2_3b_zeromock_prctl_nondumpable_escapee_held_and_old_bystander_ignored`: `AssertionError: False is not true : zero-mock prctl(PR_SET_DUMPABLE, 0) escapee was NOT marked uncertain!`
    - `test_p1_2_3c_same_uid_and_now_ticks_oserror_mark_uncertain`: `AssertionError: False is not true : _same_uid EMFILE swallowed without setting ScanResult.uncertain!`
    - `test_p1_3_watch_transient_wrapper_stat_eio_preserves_live_run_and_enforces_deadline`: `AssertionError: Termination(..., end_reason='leftovers', ...) is not None : transient wrapper stat EIO started termination on live run!`
    - `test_p1_3_recovery_transient_wrapper_stat_eio_records_error_without_losing_run`: `AssertionError: 0 != 1 : {..., 'runs_terminating': 1, 'run_errors': 0, ...}`
  - Plus 9th test `test_p1_3_watch_lead_turn_transient_wrapper_stat_eio_preserves_live_turn` added to cover `_watch_lead_turn` directly alongside `_watch_task_run`.
- **Pass-after (`TestCarbonRoP1b` on final sources):**
  - `Ran 9 tests in 30.922s — OK`
- **Sequential Targeted Runs (`PYTHONPATH=/home/node/gf-guard`, `timeout 600`, one module at a time):**
  1. `tests.unit.test_runtargets_race` — 43 tests (`6.568s`) `OK`
  2. `tests.unit.test_m3_gatefix` — 88 tests (`78.313s`) `OK`
  3. `tests.unit.test_termination_units` — 13 tests (`2.087s`) `OK`
  4. `tests.unit.test_merge` — 21 tests (`2.301s`) `OK`
  5. `tests.unit.test_merge_recovery_minors` — 7 tests (`1.827s`) `OK`
  6. `tests.unit.test_seen_hold_r4d` — 4 tests (`2.699s`) `OK`
  7. `tests.unit.test_seen_hold_r4e` — 3 tests (`0.637s`) `OK`
  8. `tests.unit.test_recovery_units` — 7 tests (`1.163s`) `OK`
  9. `tests.integration.test_execution_termination` — 16 tests (`91.307s`) `OK`
  10. `tests.integration.test_run_release_seen` — 3 tests (`12.235s`) `OK`
  11. `tests.integration.test_merge_tests` — 5 tests (`7.575s`) `OK`
  12. `tests.recovery.test_recovery_adopt_seen` — 16 tests (`16.483s`) `OK`
  13. `tests.recovery.test_recovery_merge` — 2 tests (`6.676s`) `OK`
  14. `tests.recovery.test_recovery_lead_merge` — 16 tests (`0.115s`) `OK`
  15. `tests.recovery.test_recovery` — 15 tests (`90.607s`) `OK`
  16. `tests.unit.test_docs_m3` — 8 tests (`0.003s`) `OK`
  17. `tests.unit.test_docs_m19` — 6 tests (`0.270s`) `OK`
  - **Total:** 17 targeted modules, 273 tests (`0` failures, `0` errors), `0 BLOCKED` in `~/gf-logs/guard.log`. Full `scripts/test.sh` intentionally not run per coordinator test rules.

### Round p1b: Quest verification

Mutation check of `TestCarbonRoP1b` (9 tests), run by the Quest coder on base with the kill guard on. Each line names the mutation and what it showed:
- **N1**, `_final_tx` uncertain gate off: killed by 2a, 2b and 2c.
- **N3**, EACCES scope `if True` (old bystanders hold): killed by 3b (old bystander).
- **N3b**, `if False`: killed by 3a and 3b.
- **N4**, `_record_scan_error` no-op: killed by 3c.
- **N5**, wrapper `uncertain` read as `gone`: killed by the P1-3 task watch and lead-turn watch tests.
- **N6**, `recovery._identity_alive` swallows OSError again: killed by the P1-3 recovery test.
- **N2 / N2b** (only the SIGTERM or only the SIGKILL of `_kill_test_proc_direct` removed): each survives, because the other signal still ends the group (redundant escalation).
- **N2c** (both removed): killed by 2c.
- Commit `086ac48` (Carbon) was reviewed. The old "L13/D25" merge comment is gone (rewritten as D25, confirmed survivors only).

### Round p1b: Quest review follow-ups (tests and docs only)

- Suite margin: `_setup_live_merge(kill_wait_s=...)` is an opt-in `terminate.KILL_WAIT_S` patch, used by 2c (1 s). `kill.grace_s` is already 1 in that setup, and the 10 s `KILL_WAIT_S` was the real cost. 2a keeps the full wait, because its recovery assertions (`merge_runs_held == 1`) depend on it. `TestCarbonRoP1b` went from 34 s to 22 s.
- `TestCarbonRoP1bCoverage.test_live_merge_close_leftover_scan_uncertain_alone_holds` kills mutant M2: `_check_live_merge_close` ignoring `leftover_scan_uncertain`.
- `TestCarbonRoP1bCoverage.test_is_kthread_stat_error_is_recorded_as_scan_error` kills mutant M4: the `_is_kthread` error is not recorded. ENOENT is checked as a lookup miss.
- Both fail on their mutant and pass on the code.
- Docs: L13 extended (one transient error holds `merge:<prj>` until restart), plus the P3s (MergeRunHeld vs OSError, ECHILD force path, 2x grace).


## Round p1c

- **Review addressed:** `/home/node/workspace/review-inbox/m3-clickup-p1b-carbon.md` (Carbon adversarial read-only review on `08810a1`, `REVISE`: P1-1, P2-1, P3-1; coordinator handles P2-2 docs).

### Fix
- **P1-1 (`mesh/daemon/recovery.py`, `mesh/daemon/merge.py`, `mesh/daemon/supervisor.py`):**
  - `supervisor.close_identity(run)` guards `Mapping` lookups (`"pgid"` / `"proc_starttime"` / `"run_dir"`) and falls back to `wrapper.json` `starttime` when `run["pgid"]` is set with `proc_starttime=None` and matching `wrapper.json` `pid`.
  - `recovery._env_carriers(run_id, wrapper_starttime=None)` forwards `wrapper_starttime` to `terminate.run_targets_or_recheck(run_id, None, None, wrapper_starttime)`.
  - `recovery._terminate_carriers(..., wrapper_starttime=None)` forwards `wrapper_starttime` to `terminate.begin(...)`.
  - `recovery._lost` resolves `wst = supervisor.close_identity(run)[1]` and passes `wst` to both `_env_carriers(run["id"], wst)` and `_terminate_carriers(..., wrapper_starttime=wst)`.
  - `merge.recover_merge_runs` resolves `known_wst = starttime if pid is not None else supervisor.close_identity(run)[1]` and passes `known_wst` to both `terminate.run_targets_or_recheck(run_id, None, None, known_wst)` (when `pid is None`) and `terminate.begin(..., wrapper_starttime=known_wst, seen=carriers)`.
- **P2-1 (`mesh/core/proc.py` — `_sibling_thread_environ` & `get_proc_environ`):**
  - Removed `os.path.exists(spath)` and broad `except OSError: continue` in the gVisor `/proc/<cand>/status` fallback so only `(FileNotFoundError, ProcessLookupError)` is skipped while `EMFILE`/`ENFILE`/`EIO`/`EACCES`/`EPERM` propagate to callers (`terminate.run_targets` marks `found.uncertain = True`).
  - Wrapped `open(f"/proc/{cand}/environ", "rb")` in `try ... except (FileNotFoundError, ProcessLookupError): continue` so a sibling thread exiting mid-scan does not abort scanning remaining threads.
  - Tracked `saw_empty` across sibling threads in both `/proc/<pid>/task` and gVisor fallback loops so an exiting thread returning `b""` does not shadow a live sibling thread with non-empty `environ` (and in `get_proc_environ`, `leader_err` still raises when only empty sibling reads were seen on a live tgid).
- **P3-1 (`mesh/core/proc.py` — `find_run_processes`):**
  - Non-lookup `OSError` from `get_proc_environ(pid)` now sets `uncertain = True` (`EACCES`/`EPERM` scoped to same-uid non-kthread processes so foreign-uid host processes like `pid=10` `uid=0` do not taint clean scans; all other non-`ENOENT`/`ESRCH` `OSError` such as `EMFILE`/`EIO` unconditionally set `uncertain = True`).

### Tests (`tests/unit/test_m3_gatefix.py` — `TestCarbonRoP1c`, ACC: N18, A8)
- `TestCarbonRoP1c.test_p1_1_1a_task_and_lead_crash_recovery_after_young_s_holds_nondumpable_escapee`
- `TestCarbonRoP1c.test_p1_1_1b_merge_test_crash_recovery_after_young_s_holds_nondumpable_escapee`
- `TestCarbonRoP1c.test_p1_1_1c_immediate_restart_then_advance_after_young_s_holds_nondumpable_escapee`
- `TestCarbonRoP1c.test_p2_1a_sibling_thread_status_oserror_propagates_and_marks_uncertain`
- `TestCarbonRoP1c.test_p2_1b_sibling_thread_environ_enoent_race_and_empty_skips_to_live_thread`
- `TestCarbonRoP1c.test_p3_1_find_run_processes_environ_oserror_sets_uncertain`

### Fail-Before / Pass-After & Targeted Verification
- **Fail-before (`TestCarbonRoP1c` on unmodified `08810a1` `mesh/`):** `Ran 6 tests in 3.507s — FAILED (failures=5, errors=1)` (1a closed both `task` and `lead_turn` `'lost'` with `runs_lost=2`; 1b closed `merge_test` `'lost'` with `merge_runs_lost=1`; 1c closed `'lost'` in `advance_terminations` after `YOUNG_S`; 2a swallowed `EMFILE` on `/proc/<cand>/status`; 2b raised unhandled `FileNotFoundError` on `/proc/<s1>/environ`; 3-1 left `ScanPids.uncertain == False` on `EMFILE`).
- **Pass-after (`TestCarbonRoP1c` on round `p1c` sources):** `Ran 6 tests in 5.016s — OK`.
- **Sequential targeted modules (`PYTHONPATH=/home/node/gf-guard`, `timeout 600`):**
  1. `tests.unit.test_m3_gatefix` — 96 tests (`74.887s`) `OK`
  2. `tests.recovery.test_recovery_adopt_seen` — 16 tests (`16.842s`) `OK`
  3. `tests.recovery.test_recovery_merge` — 2 tests (`7.065s`) `OK`
  4. `tests.recovery.test_recovery` — 15 tests (`90.597s`) `OK`
  5. `tests.unit.test_runtargets_race` — 43 tests (`6.548s`) `OK`
  - Plus `tests.unit.test_merge` (21), `tests.unit.test_merge_recovery_minors` (7), `tests.unit.test_proc_probe` (3), `tests.unit.test_docs_m3` (8) — all `OK`.



## m3-p1c-final (coordinator, 2026-11-12)

- run_targets: EACCES on environ from a same-uid non-kthread newer than the wrapper no longer marks the scan uncertain when the process is exiting (no VmSize in /proc/<pid>/status, mm released), a zombie, gone, or pid-reused (_exiting_or_gone). Live non-dumpable escapees always have an mm, so Carbon RO P1-2 stays closed (mutant always-skip: 5 failures in test_m3_gatefix). Seen on base: exiting test processes (empty cmdline) made recovery scans uncertain.
- 38e04df (find_run_processes EACCES skip) touches the test/smoke-harness helper only; no daemon path calls it.
- Recovery leftovers test accepts the L14 fail-safe hold (sshd login started after the wrapper).

## m3-p1c-final: Carbon RO p1c fixes and Kimi RO recheck (2026-11-12)

- Carbon RO p1c (of 835b136): REVISE, P1-1 leader-zombie with a live worker thread treated as exiting on mainline Linux; P2-1 NULL proc_starttime not backfilled in recovery; P3-1 stale docstring.
- Fixes: a154f2f (sibling-thread mm), b57fec2 (Z/X with a live thread returns False; backfill proc_starttime from a matching wrapper.json in `_recover_run` and `_lost`; `_terminate_group` uses `close_identity`; docstring), c2547d4 (unit test, Z/X mutant killed), c1a7e03 (`_has_mm`: gVisor shows `VmSize: 0 kB` for zombies, so 0 counts as no mm; this removed the recovery-tier flakes seen on base: 4/4 clean afterwards).
- Suite on c1a7e03: rc0, 544 s (idle base, no SSH during the run).
- Kimi K3.1 Pro RO recheck of 835b136..c1a7e03: Carbon P1-1, P2-1, P3-1 confirmed fixed by probe (gVisor; mainline only by unit test). Its two new "P1"s need a tampered/invalid `wrapper.json`: a too-new starttime hides an older carrier, and an invalid/mismatched one falls back to the YOUNG_S window. Coordinator ruling: P2, documented as L14 (a) widened and L14 (c); `wrapper.json` is written only by `mesh.runwrap` and same-uid tampering is outside the threat model.

