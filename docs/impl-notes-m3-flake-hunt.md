# m3-flake-hunt: implementation notes (a helper agent, 2026-11-10)

Test-only fix for two load-sensitive unit modules. Branch `grok/m3-flake-hunt` from
`origin/main` (256bc6e). Changed: `tests/unit/test_termination_units.py`,
`tests/unit/test_proc_probe.py`, this file. No product code, `tests/helpers.py` or `scripts/*`
touched. No assertion was loosened and no retries were added.

## How it was measured

Each module was run as `python3 -m unittest tests.unit.<module>` in a loop on helper-box
(8 vCPU, gVisor), 30 runs per row:

* **solo**: nothing else running.
* **load**: while `bash scripts/test.sh` ran in a loop from another worktree in a separate tmux
  session, plus 8 CPU busy loops (`stress-ng` is not installed, so 8 `python3` loops; load
  average 11-14).
* **parallel**: two loops of the same module at the same time, under the same load (this is
  the "two suites in parallel" case).

## Before / after

| Module | Mode | Before (fails) | After (fails) |
|---|---|---|---|
| test_termination_units | solo | 0/30 | 0/30 |
| test_termination_units | load | 0/30 | 0/30 |
| test_termination_units | parallel (2 loops x 30) | 1/60 | 0/60 |
| test_proc_probe | solo | 2/30 | 0/30 |
| test_proc_probe | load | 3/30 | 0/30 |
| test_proc_probe | parallel | not run | 0/30 |

The "after" load and parallel rows ran at the same time (four loops at once, plus the load).
The load suite itself (old code, other worktree) also failed once in `test_proc_probe` during
the "before" phase.

## Root causes and fixes

### test_termination_units: shared run ids across processes

Failure (parallel, 1/60):

```
FAIL: test_env_stripped_member_found_while_wrapper_unreaped
  File "tests/unit/test_termination_units.py", line 323
    self.assertEqual(terminate.run_targets("run_zomb", leader.pid, leader.pid, st), {})
AssertionError: {637821: 1059876} != {}
```

`terminate.run_targets()` treats every process in `/proc` whose `MESH_RUN_ID` equals the run id
as a target (by design: that is how escapees are found). The tests used fixed run ids
(`run_zomb`, `run_gone`, `run_stale`, `run_unit_esc`), so with two suites running, one test
found the other suite's leader (`MESH_RUN_ID=run_zomb`) and the "nothing is trusted" assertion
saw a foreign pid. Fix: `_run_id(tag)` gives `<tag>_<pid>_<uuid8>` for every run id that reaches
`run_targets()` / `terminate.begin()`. The assertions are unchanged.

Also: the wait-for-condition deadlines (`_wait`, the zombie wait, the `_reap_wrapper` poll and
`Popen.wait`) use one `WAIT_S = 30` instead of 5 / 8 / 10 s. These polls return as soon as the
condition holds, so this only changes how long a real failure takes to report. None of them
failed in the runs above; it is headroom for a loaded box, not a fix for an observed failure.

### test_proc_probe: /proc/<pid>/environ read right after exec

Failure (solo 2/30, load 3/30):

```
FAIL: test_proc_stat_environ_and_subreaper
  File "tests/unit/test_proc_probe.py", line 67
    self.assertEqual(child_env.get("MESH_PROBE_TOKEN"), "probe-value-123456")
AssertionError: None != 'probe-value-123456'
```

`Popen()` returns once the child has exec'd, but on this gVisor host `/proc/<pid>/environ` of a
just-exec'd process can read back **empty** for a few ms (a probe loop: 109 of 200 reads taken
right after `Popen()` were empty while `/proc/<pid>/cmdline` already showed the new program; a
second read a moment later had the full 4 KB env; longest empty window seen 6.7 ms). The test
read it immediately. Fix: the child prints `ready` once its interpreter runs and the test reads
`/proc` only after that line (stdout is a pipe now). 300 reads after the ready line: 0 empty.
The stat / environ / prctl assertions are unchanged. The orphan test's 5 s deadlines use
`WAIT_S` too (no failure seen there).

Bounded readiness wait (Linear review of #18, MAJOR): a bare `proc.stdout.readline()` blocks
forever if the child is alive but never prints, and the test would never reach its cleanup.
Both line reads in this file (the `ready` line and the orphan test's grandchild pid line) now
go through `_read_line_or_fail(proc, timeout_s=WAIT_S)`: it polls the (binary) stdout pipe
with `select` against a monotonic deadline; on timeout or EOF before a full line it kills and
reaps the child and raises `AssertionError` with the pid, reason and partial output. No retry.
`test_silent_child_fails_within_deadline` pins it: a child that sleeps instead of printing,
with a 0.5 s deadline override, must fail with "no line within" well before its 30 s sleep and
be reaped (`returncode` set).

## Product observation (not changed, for the reviewer)

The empty-environ window also applies to product code: `mesh.core.proc.get_proc_environ()`
returns `{}` for such a process, so a `terminate.run_targets()` scan that lands in the first
few ms after a run's child exec's would not match it by `MESH_RUN_ID` in that scan (it can
still match by pgid/session or descent, and the next scan sees the env). I did not change
product code; flagging it in case it matters for escapee detection.

## Tests

`bash scripts/test.sh` (all three tiers) on helper-box; counts in the PR.
