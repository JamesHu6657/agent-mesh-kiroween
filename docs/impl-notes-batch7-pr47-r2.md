# Batch 7 PR #47 Round-2 Implementation Notes

This note documents verification against repository code and `review.md` Repros, root-cause fixes, decisions made without user prompting, and test verification for the three round-2 review findings on branch `pr47-r2` (based on `cloud/pr47r2-base`, commit `86d46c2`).

All three findings (`2x P2`, `1x P3`) were confirmed real on unedited `86d46c2`, reproduced with failing-first regression tests in [tests/unit/test_batch7_pr47_r2.py](../tests/unit/test_batch7_pr47_r2.py), and resolved with minimal root-cause fixes.

---

## Verified Findings and Fixes

### 1. [P2] `tests/run_tier.py` lifts `top_level_dir` blindly and crashes on discovery `ImportError`

- **Verification against Code & `review.md` Repro:** Confirmed real on `86d46c2` at `tests/run_tier.py:251-258`. Running `python3 tests/run_tier.py ext_tier` from a temporary working directory containing a non-package `ext_tier` subdirectory failed at `loader.discover(test_dir, top_level_dir=top_level)` with uncaught `ImportError: Start directory is not importable: '.../ext_tier'` (`rc=1`) because `os.path.commonpath([cwd_abs, dir_abs]) == cwd_abs` unconditionally lifted `top_level` to `cwd_abs` without verifying `__init__.py` package continuity. The same crash affected non-package or partial-package subdirectories under `REPO_ROOT`, as well as missing/unimportable discovery targets. Round-1 `TestFinding6RunTierDiscoveryAndSkip` in [tests/unit/test_batch7_pr47_r1.py](../tests/unit/test_batch7_pr47_r1.py) missed this because its subtests ran an `/tmp` external dir from `cwd=ROOT` (taking the `else: top_level = dir_abs` fallback) and gave `fakerepo/tests/unit` full `__init__.py` markers.
- **Root Cause & Fix:**
  - [tests/run_tier.py](../tests/run_tier.py):
    - Added `_is_importable_under(start_abs, root_abs)` validating `os.path.commonpath([root, cur]) == root` (handling `ValueError`) and checking `os.path.isfile(os.path.join(cur, "__init__.py"))` on every directory from `start_abs` up to (excluding) `root_abs`. When `start_abs == root_abs`, it returns `True` because stdlib `discover(start_dir, top_level_dir=start_dir)` succeeds directly.
    - Updated `main()` to set `top_level = REPO_ROOT` or `top_level = cwd_abs` only when `_is_importable_under` is true, otherwise falling back to `top_level = dir_abs` (`_build_child_pythonpath` still includes both `top_level` and `REPO_ROOT`, keeping sibling helpers and `mesh.*` importable).
    - Wrapped `loader.discover(test_dir, top_level_dir=top_level)` in `try ... except ImportError:` capturing `traceback.format_exc()` as `("discovery_error", "ERROR", 0.0, ...)`, and updated the empty check to `if not all_cases and not loader.errors and not discovery_results:` so any discovery `ImportError` (missing path, invalid package identifier containing `__init__.py`, mid-scan shadowed module) reports a structured `ERROR discovery_error` / `FAILED (errors=...)` result alongside any pre-existing `loader.errors` rather than crashing with an unhandled traceback.
- **Decision:** Placed all new round-2 regression tests in [tests/unit/test_batch7_pr47_r2.py](../tests/unit/test_batch7_pr47_r2.py) (`TestFinding1RunTierTopLevelAndImportError`) per `TASK.md.txt` instead of modifying `tests/unit/test_batch7_pr47_r1.py`.

### 2. [P2] `scripts/coord/secret-pattern.sh` quote/operator regex causes empty-literal/Python false positives and misses f-strings & double-escaped JSON

- **Verification against Code & `review.md` Repro:** Confirmed real on `86d46c2` at `scripts/coord/secret-pattern.sh:11`:
  1. **False positives:** Value-side `(\\?['"]){1,3}` matched two-quote empty literals (`""`, `''`, `\"\"`) whenever followed immediately by non-space code such as `,`, `;`, `}`, or `#` (e.g., empty keyword arg followed by `,timeout=30`, dict entry before `,**extra}`, shell export after `;`). Additionally, allowing `[[:space:]]*[:=][[:space:]]*` with optional value quotes caused ordinary unquoted Python assignments and type annotations (`os.environ.get(...)`, `Optional[str] = None`) to false-match. Scanning all tracked files in `86d46c2` showed existing false positives at `tests/integration/test_execution_acc_run2.py:489` and `tests/unit/test_smoke_harness.py:104`.
  2. **False negatives:** The Python prefix group `([rRbBuU]|[rR][bB]|[bB][rR])` omitted Python 3 f-string prefixes (`f`, `F`, `fr`, `rf`, and mixed-case variants), and `\\?['"]` only handled up to 1 backslash, missing double-escaped JSON-in-JSON strings (`\\\"`).
- **Root Cause & Fix:**
  - [scripts/coord/secret-pattern.sh](../scripts/coord/secret-pattern.sh):
    - Replaced `{1,3}` with `((\\*['"]){3}|\\*['"])` on both key and value sides so quotes match only 3-quote delimiters or 1-quote delimiters (never 2-quote empty literals) with any number of preceding JSON escape backslashes (`\\*`), keeping empty strings (`""`, `''`, `""""""`, `\"\"`, `\\\"\\\"`) excluded at the first character class `[^$` '"\\]`.
    - Extended Python string prefixes with `[fF]` (`([rRbBuUfF]|[rR][bBfF]|[bBfF][rR])`).
    - Split the key-value delimiter/value expression into two branches: (a) `[[:space:]]*[:=][[:space:]]*` when the value has a required opening quote wrapper `((\\*['"]){3}|\\*['"])`, and (b) immediate `=[^$` '"\\][^` '"\\(]{5,}` when unquoted.
- **Decisions & Repo Hygiene Check:**
  - Kept the split immediate-`=` unquoted branch (also excluding `(` within the minimum 6-character span) per review analysis rather than removing unquoted detection altogether, preserving detection of unquoted env assignments such as `KEY=real_val_123` while eliminating Python statement/annotation false positives and `KEY=get(x)`.
  - Verified against all `git ls-files`: eliminated the two false positives at `tests/integration/test_execution_acc_run2.py:489` and `tests/unit/test_smoke_harness.py:104`, added zero new false positives across the repository, and confirmed zero self-matches on `scripts/coord/secret-pattern.sh`, `.kiro/hooks/scripts/secret-scan.sh`, `scripts/coord/push-review.sh`, `scripts/coord/sync-github.sh`, `tests/unit/test_batch7_pr47_r2.py`, and this note.

### 3. [P3] `scripts/inbox-worker-example.sh` signals already-reaped PID and empty/released PGID

- **Verification against Code & `review.md` Repro:** Confirmed real on `86d46c2` at lines 71, 175–176, 289–324:
  1. After `wait "$cmd_pid"` (`reaped=1`) and `! _pgid_has_live "$cmd_pid"` confirmed the process group empty and exited the grace loop, line 319 still unconditionally ran `kill -KILL -- "-$cmd_pid"`. Likewise, if normal exit reaping (`reaped=1`) coincided with `$lost` / `$stopping` on an empty PGID, line 293 unconditionally ran `kill -TERM -- "-$cmd_pid"`.
  2. During cleanup/grace after `reaped=1` while `cmd_pid` remained non-empty, `USR1`/`INT`/`TERM`/`EXIT` traps ran `kill -TERM -- "$cmd_pid" "-$cmd_pid"`, signaling the already-reaped positive PID `"$cmd_pid"`.
  3. Line 323 restored `trap 'exit 130' INT TERM` *before* line 324 set `cmd_pid=""`, leaving a window where a signal firing `EXIT` cleanup operated on stale `cmd_pid`.
- **Root Cause & Fix:**
  - [scripts/inbox-worker-example.sh](../scripts/inbox-worker-example.sh):
    - Initialized `hb_pid=""; cmd_pid=""; reaped=0; cmd_dead=0; pgid_gone=0` together and moved `_pgid_has_live` above `cleanup()`.
    - Added `_signal_cmd <SIG>` and routed `cleanup()`, `USR1`/`INT`/`TERM` traps, grace `SIGTERM`, and fallback `SIGKILL` through it: returns immediately when `cmd_pid` is empty or `pgid_gone == 1`; signals `"$cmd_pid" "-$cmd_pid"` only while `reaped == 0 && cmd_dead == 0`; otherwise probes `_pgid_has_live "$cmd_pid"` and signals only `"-$cmd_pid"` (setting `pgid_gone=1` if empty).
    - Marked `[ -r "/proc/$$/stat" ] && cmd_dead=1` right before `wait "$cmd_pid"` once `/proc/$cmd_pid/stat` shows `""`/`Z`, closing the trap window right around `wait`.
    - Updated `pgid_gone=1` immediately when `! _pgid_has_live "$cmd_pid"` returns empty after reap or in the grace loop; gated grace-block entry on `[ "$reaped" -eq 0 ] || { [ "$pgid_gone" -eq 0 ] && _pgid_has_live "$cmd_pid"; }`; added a final `/proc/$cmd_pid/stat` check after the grace loop sleep before `SIGKILL`; guarded post-grace `else` `SIGKILL` with `elif _pgid_has_live "$cmd_pid"; then ...`; and set `pgid_gone=1; cmd_pid=""` *before* reinstalling `trap 'exit 130' INT TERM`.
- **Decisions & Container Notes:**
  - In containers where PID 1 does not immediately reap reparented exited children, `kill -0 -- "-$cmd_pid"` can still return `0` briefly on `<defunct>` (`Z`) zombies while `_pgid_has_live` inspects `/proc/[0-9]*/stat`, detects no live processes, and breaks cleanly with zero `SIGKILL` and sub-grace elapsed time. Regression tests in `TestFinding3InboxWorkerNoPostReapSignaling` account for this and use exported bash function hooks (`kill`, `trap`, `wait`) to trigger exact race windows deterministically with zero edits to production behavior.

---

## Verification

### Pre-fix Baseline Failure Confirmation (on unedited `cloud/pr47r2-base` @ `86d46c2`)

- `python3 -m unittest -v tests/unit/test_batch7_pr47_r2.py` against a clean detached worktree at `86d46c2`: **Ran 10 tests — FAILED (failures=9)** (every bug regression test failed cleanly with the exact crash / false-positive / false-negative / post-reap `TERM` & `KILL` / stale-`cmd_pid` failure; only the positive control `test_valid_package_chain_under_cwd_lifts_top_level` passed).

### Post-fix Verification Runs

1. **New regression tests (3 consecutive runs):**
   - `python3 -m unittest tests/unit/test_batch7_pr47_r2.py`: **Ran 10 tests, OK** x3 consecutive runs (`2.250s`, `2.263s`, `2.244s`).
2. **Related unit suites (full suite `scripts/test.sh` omitted per task instructions):**
   - `python3 -m unittest tests/unit/test_batch7_pr47_r2.py tests/unit/test_batch7_pr47_r1.py tests/unit/test_batch7_g6.py tests/unit/test_inbox_worker_kit.py tests/unit/test_sakana_core.py tests/unit/test_linear_full_scripts.py tests/unit/test_tierlib.py tests/unit/test_batch6_shutdown.py tests/unit/test_pr39_r1.py tests/unit/test_pr39_r2.py`: **Ran 140 tests in 86.990s — OK**.
3. **Syntax, permissions, and scanner hygiene:**
   - `bash -n scripts/inbox-worker-example.sh scripts/coord/secret-pattern.sh`: pass (`rc=0`).
   - `test -x scripts/inbox-worker-example.sh`: executable bit preserved.
   - `bash scripts/test.sh --check`: `scripts/test.sh: syntax and runner OK`.
   - `grep -E "$P"` self-scan across `secret-pattern.sh`, `secret-scan.sh`, `push-review.sh`, `sync-github.sh`, `tests/unit/test_batch7_pr47_r2.py`, and `docs/impl-notes-batch7-pr47-r2.md`: 0 hits.
