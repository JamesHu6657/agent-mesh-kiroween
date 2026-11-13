# m3-runtargets-race: env-read race in run_targets (Worker 2, 2026-11-10)

## The race

A reviewer measured on box2 (gVisor) that reading `/proc/<pid>/environ`
right after `Popen` returned missed `MESH_RUN_ID` in **92/200** cases.
`get_proc_environ` then returns `{}`, so "not visible yet" looked like
"readable, no MESH_RUN_ID" and `run_targets` skipped the process.

## Fix round 1 (review #20: Linear REVISE, Quest REVISE)

Head before the round: 09f0a1e. Main (with #12, fdd550d) merged in first.

### UNKNOWN, without sleeping (L4, Q2, Q4)

- An environ is **UNKNOWN** when it is empty or unreadable **and** the process is
  young (starttime at/after the run's wrapper starttime, or within `YOUNG_S` = 5 s
  of now), has meshd's own uid, and is not a kernel thread (pid 2, PPid 2, or
  `PF_KTHREAD` in `/proc/<pid>/stat` field 9). The cmdline is no longer consulted:
  in most real misses it is empty too (Q4).
- Other-user processes (environ unreadable forever; 29 on box2) and old
  processes are never UNKNOWN, so they cost one stat + one environ read per scan,
  as before the PR (Q2).
- `run_targets` makes **one** pass over /proc and never sleeps. The 30 ms in-call
  re-reads are gone; an UNKNOWN pid is simply read again on the next scan.
- UNKNOWN never makes a process a target. It only stops a scan from concluding
  "no MESH_RUN_ID"; the process is taken only through a confirmed identity, a
  trusted run group, or PPid descent.

### Confirmed identity and descendant closure (L1, L2)

- A live pid whose starttime equals the one in `seen` (this termination's seen map,
  which also holds everything it found) is a target on its own, independent of
  group/session membership and ancestry (L1). That keeps an UNKNOWN escapee found
  once through its parent after the parent exits and it is re-parented.
- Trusted-group members (UNKNOWN or not) are taken in the scan itself, so they are
  anchors before descent is checked (L2). Descent is then a fixed-point closure:
  a candidate taken by descent becomes an anchor for the others in the same scan,
  so the result does not depend on /proc order (tested with three orders).
  (`_descends_from` walks the whole PPid chain, so the closure step is defensive.)
- Candidates for descent are, as before, untrusted run-group members, plus
  UNKNOWN processes. A readable environ without `MESH_RUN_ID` outside the group
  is still never a target.

### Confirmation state per run, once per scan (L3, Q1, Q5)

- The module-global `_empty_scans` / `confirmed_clear` are removed.
- `terminate.ClearCheck` is a per-run object (`first_empty_at`) with
  `observe(found)` (called exactly once per real scan; True only on an empty scan
  at least `POLL_S` after the first; a hit resets; a premature scan does not renew
  the first) and `invalidate()`.
- The supervisor keeps one `ClearCheck` per exited run in `supervisor._CLEAR`
  (evicted with the run like the seen caches). `_watch_task_run` and
  `_watch_lead_turn` call `observe` once per tick. A hold or changed-seen tick
  calls `invalidate()` and never confirms.
- The wrapper is not reaped until the confirming scan, so meshd's unreaped zombie
  keeps the pgid trusted for that scan (unchanged rule from m1c-run4 §2).
- Terminating runs keep using `Termination.empty_scan_at` in `step()` (already
  per run, one step per scan); `_advance` turning a hold tick's "done" into
  "pending" already resets it.

### Latency without blocking (L4, A1)

The second scan would otherwise wait for the next 1 s watch pass. meshd's 200 ms
termination loop now also runs while an exited run waits for confirmation
(`supervisor.pending_exit_checks`, one-line guard change in `server.py`) and takes
the due scan through `supervisor.confirm_pending_exits` (`_watch_one` on that run).
A clean exit therefore closes about one poll after the first empty scan, without
any sleep on the asyncio loop.

`run_targets_or_recheck` (one scan, and one more after `POLL_S` if empty) is kept
only for one-shot checks with no next tick, all off the loop: the merge test step
(runs in `asyncio.to_thread`), `recovery._env_carriers` and merge recovery at
startup.

Startup recovery's `exit.json` path (`recovery.py`, one `_watch_one`) has no next
tick of its own: if that call left a pending first empty scan, it takes the
confirming scan once, one poll later, in the same call (startup, not the loop), so
a clean exit is still closed from exit.json (`test_run_exited_while_meshd_down`).

## Why unrelated processes stay safe

- A readable environ without `MESH_RUN_ID` is never an env-carrier, including a
  process in a different session (tested, real processes).
- Other-user, kernel-thread, zombie and old processes are never targeted, even
  under an anchor or in the run group (tested, fake /proc; zombie also real).
- An UNKNOWN process in a different session with no descent and no trusted group,
  and a raw pgid match after the wrapper is gone, are left alone (tested).
- meshd, pid 1 and registered children stay excluded; signals still need a
  starttime match; a reused pid (other starttime) is not a confirmed identity.

## Tests

`tests/unit/test_runtargets_race.py`:
- `ClearCheck`: first empty scan is never clear; too-soon scan neither clears nor
  renews; a hit resets; `invalidate` resets; no module state.
- Fake /proc: UNKNOWN escapee via parent anchor; confirmed identity after the
  parent exits (and not for a reused pid); UNKNOWN trusted-group member as anchor
  in any /proc order; empty cmdline still UNKNOWN; safety cases; no sleeping.
- Real processes: full loop UNKNOWN, found, parent exits and child re-parented,
  SIGTERM ignored, SIGKILL, dead, two empty scans, done; readable-without-id not
  targeted; real zombie; recovery env-only path after one empty read.
- Through the real `_watch_task_run` tick path (Q5): first empty scan keeps the
  run open and the wrapper unreaped; a changed-seen tick invalidates; a hit
  resets; empty ticks a poll apart close on the second; `confirm_pending_exits`
  takes the due scan.
- `test_seen_hold_r4e`: the seeded `_empty_scans` entry is replaced by a second
  real `_watch` tick one `POLL_S` later.

Mutations (each run against the new file): clear on first scan, drop UNKNOWN
escapee descent, no reset on a hit, no confirmed identity, no uid/kthread filter,
no young filter: all killed. Dropping the closure's anchor update survives
(redundant with the full-chain walk, see above).

No vendor CLI was run; no secrets.

## Existing tests changed in the first round (kept)

- `tests/unit/test_seen_hold_r4d.py`: waits for the second empty `step()` scan.
- `tests/integration/test_execution_termination.py` (`test_kill_failed_retry_then_dead`):
  one `_advance` is the first empty scan, a second after `POLL_S` on the mocked
  clock is the close.

## Round 2 (review #20b: Linear REVISE + Quest REVISE on bf78bec), Quest coder

Taken over from Worker 2; branch merged with main `adebc01` (clean).

Two-scan rule kept: no single-scan "done" (both reviews, with evidence: pid hopper
311/400 empty single scans with no UNKNOWN pid; exec storm 22/81).

**L1 handoff window (Linear MAJOR).** A scan that finds leftovers now hands every
identity it found (pid + starttime) to `begin()` as `seen`, so `begin()`'s own scan
keeps an escapee whose only link (its parent) exited in between. `begin()` still
keeps only entries alive with the same starttime. Sites:
- `supervisor._watch_lead_turn` leftovers: `seen=dict(leftovers)`.
- `supervisor._watch_task_run` leftovers: recorded members ∪ this scan's targets.
- `merge.run_test_step` leftovers → `_terminate_test(..., seen=leftovers)` (new param).
- `merge.recover_merge_runs` env carriers → `begin(..., seen=carriers)`.
- `recovery._lost` env carriers → `_terminate_carriers(..., carriers)` → `begin(seen=)`.

**Tests added** (`tests/unit/test_runtargets_race.py`):
- `TestHandoffWindow` (real processes, lead / task / merge test step): P carries
  MESH_RUN_ID, C is P's setsid child with an empty environ; the first scan is real,
  then P is killed and C re-parented before `begin()` scans; `begin()` must target
  and SIGTERM C. Each fails if its site drops the handoff.
- M15 `step()`: empty, hit, empty 0.3 s after the first → pending; done only on the
  next empty scan ≥ POLL_S later (scripted scans and clock).
- M14 lead turn: first and too-soon empty scans keep the turn open (lease renewed,
  wrapper unreaped, `finish_lead_turn` not called); closes on the scan ≥ POLL_S later.
- M13 recovery: `_recover_run` of an exit.json run takes the confirming scan itself
  (`exited`, two watches), not `exit_deferred`.
- M17 server loop: `_termination_loop` calls `advance_terminations` with no active
  termination and one pending exit check (fails by timeout otherwise).
- M18 merge test step: first scan empty (the race), setsid MESH_RUN_ID escapee still
  reported as a leftover and terminated.
- Deep ancestry: 150-deep chain of UNKNOWN setsid descendants, both /proc orders, all
  found; this makes the closure's anchor update (M6) non-equivalent: removing it fails.
- Trusted-group boundaries: other-uid and old processes in a trusted group / session
  are group members; other-uid and old children outside the group are not UNKNOWN and
  not taken; the same group untrusted is left alone.
- Full-loop test: parent ignores SIGTERM and exits on stdin; the stdin write tolerates
  BrokenPipe; readiness reads use the bounded `_read_line_or_fail` helper (#18).
- `test_seen_hold_r4d`: no seeded `empty_scan_at`; a second real tick one POLL_S later.

**P3 cleanups.** `run_targets` reads environ of a confirmed-identity pid only when the
caller passes `unknown`.

**Known limits (documented, not fixed here).**
- A pid that vanishes between `listdir("/proc")` and its stat read is skipped; a
  pid-hopping member (fork, parent exits) can make single scans empty. Two scans make
  this much rarer, not impossible.
- An escapee with a readable environ lacking MESH_RUN_ID (`setsid env -i FOO=1 …`)
  outside the run group is not UNKNOWN and is not taken by descent; once its parent
  is gone, nothing links it to the run.
- `confirm_pending_exits` runs the full `_watch_one` (seen read, and for adopted runs
  `report_seen_unusable`) for a due run, at most every 200 ms while pending.

**A1.** meshd's termination loop now sleeps until the earliest pending exit
confirmation is due (`supervisor.next_exit_check_in`), not a fixed 200 ms beat, so a
clean exit closes POLL_S after the first empty scan instead of up to 2 x POLL_S.
New handoff/merge tests patch `terminate.POLL_S` down where they wait on it.
Full `scripts/test.sh` on base (HostA, same window, box slower than when main
measured 446 s): this branch 477 s (unit 61.9 / integration 378.2 / recovery 36.5;
634 / 280 / 49 tests); main adebc01 right after it 515 s (56.9 / 419.8 / 38.0);
m3-r7d (main + small) shortly before 475 s. The PR's cost is within the box's
run-to-run noise; the new unit module adds ~3 s.
