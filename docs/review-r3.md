VERDICT: PASS

# Review r3 of `docs/plan.md` (r3) and `docs/acceptance-checklist.md` (r3)

Reviewer: Kiro CLI (Opus 5.5), independent review, round 3 (final), 2026-11-07.
Inputs read: `docs/requirements.md`, `docs/plan.md`, `docs/acceptance-checklist.md`, `docs/review-r1.md`, `docs/review-r2.md`, `docs/review-r1-fixes.md`, `docs/review-r2-fixes.md`, and `tests/fixtures/real/` (README plus the droid, agy, kiro and vibe help files and the kiro metadata run).

**Reviewer limitation.** Shell was not available, so I could not recompute the §14.11 sha256 values. The planner's statement that `sha256sum` matched all 12 files remains unverified by me. N15 and the new §14.11 stop-and-report rule cover this. I checked every flag line number that r3 added against the fixtures, and all of them are correct:
- droid `help.txt`: L10–11 (`-r/--resume`, `--last`), L22 and L24–L29 (`resume`, `daemon`, `search`, `update`, `mcp`, `plugin`, `computer`).
- agy `help.txt`: L3 `--agent`, L13 `--log-file`, L29–42 subcommands.
- kiro `help.txt`: L8 `login`, L9 `logout`, L15 `crew`, L18 `update`, L20 `serve`.
- vibe `help.txt`: L54–55 `--trust`.

The kiro `chat-help.txt` citations (L23, L26, L29–30, L32–37, L39, L42, L78) are also correct.

**Overall.** All ten r2 findings are resolved in the text. Spot checks of the r1 fixes found no regressions. The same-uid threat model is now honest. Reasoning effort and permission profiles are kept apart, and no profile silently maps to trust-all. The interface is Unix-socket only, and M1 is gated into M1a, M1b and M1c. The new findings below are all MINOR. None is a BLOCKER or MAJOR, so the verdict is PASS. They should be fixed during implementation or in a small follow-up edit.

---

## Part 1 — Status of r2 findings

| # | r2 finding | Status | Evidence (section + short quote) |
|---|---|---|---|
| 1 | MAJOR: user-MAC guarantee overclaimed; code tampering and memory capture missing | **RESOLVED** | §7.4 "**What this detects, and what it does not.** It detects forged direct approvals … written by a process that edited DB rows but has **not** captured the passphrase or `K_user` and has **not** modified Mesh's code". §7.6 residual risk adds "**modify Mesh's own code** … `audit verify` report a clean result" and "**capture the passphrase or `K_user`** … `/proc/<pid>/mem` or ptrace". It ends with "Mesh cannot prevent these in v1." Mitigation 6 is now "Tamper evidence for **DB-only** edits". Mitigation 7 adds `INSTALL_PATH_INSECURE`, a read-only install, and "the same uid can `chmod` it back". Mitigation 8 adds `python3 -P -s`. §18.2, §19 Q8 and Q11 are updated. Checklist: C14, C15, the G-section preamble, G20 "(a DB-only forgery …)", and G29 now requires the new residual-risk text. One gap in mitigation 7 remains (new finding 2, MINOR). |
| 2 | M1a items depend on M1b; the milestone value at intermediate gates was undefined | **RESOLVED** | Checklist §0.1: B11 and G12 are in M1b, and J6 is in M1c. B2 now uses "`m agent show no-such-agent [U]`". The gate notes say "A8's state-specific clauses … apply from M1c". §16 M1.1 says "`"M1b"` or `"M1c"`, for M1 verbs not yet built at an intermediate gate". M1.8 builds "the `run list/show/log` and `task log` commands". I re-counted §0.1: every M1 item from A to N appears in exactly one gate. |
| 3 | AG check order unspecified; fake adapter had no profile mapping | **RESOLVED** | §6.2 "**Assignment guards (AG)**, checked in this order", steps 1–6: installed → profile supported (`PROFILE_UNSUPPORTED`) → `launch_enabled` (`ADAPTER_UNSUPPORTED`) → model/effort → `permission_escalation`. It also says "No approval is ever requested for a profile that can never launch." In §14.2 the fake row is `--fake-profile <name>` for all five profiles, and "For every adapter, including `fake` …". N5 now reads "with no approval row created (AG step 3 runs before …)". G24 includes fake. |
| 4 | Lease released on normal exit while a setsid escapee runs | **RESOLVED** (wording leftovers in new finding 3) | §11.4 "**No-duplicate guarantee (release condition).** On every path … (T7 …, T8 …, T10, the §11.3 exit handling, and recovery §11.7)". §11.3 "**Leftovers:** … terminate them through §11.4 at once and emit `run.leftovers_terminated`". T7 and T8 cite the condition. §7.1 (c) says "for any **known** run R, active **or ended**". Checklist H17 is new. |
| 5 | Reaper could reap a spawned but unregistered child | **RESOLVED** | §11.4 "**Spawn window.** … `spawns_in_flight` … The reaper skips its whole scan while `spawns_in_flight > 0`". `test_reaper_inflight_spawn` is in §17.2, and checklist H18 is new. |
| 6 | `PROMPT_TOO_LONG` at T5 had no caller; lead prompts had no rule | **RESOLVED** | T6 now lists "`prompt_too_long` … details: `bytes`, `limit`, `adapter`, `profile`". §12.5 says "at assign, reassign, or retry (a caller exists) … exit 2" versus "at T5 … T6 `queued → failed(prompt_too_long)`". §12.6 "**Lead prompt size.** … caps it at `max_argv_prompt_bytes` … the kiro `readonly` refusal never applies to a lead turn". Checklist N16 and L6 cover it. |
| 7 | agy effort dropped silently; kiro reason mislabelled | **RESOLVED** | §14.4 "validation rejects it with exit 2 `EFFORT_NOT_ALLOWED` (details: `reason:"model_encodes_effort"` …) … Mesh never drops a user's effort silently". §9.1 "A user-set effort is never dropped silently". §14.5 reason `model_or_effort_rejected` with the README note, matching the fixture README's "invalid model id". Checklist D17 and N6 cover it. |
| 8 | "Never use" lists incomplete; vibe `--trust` always emitted | **RESOLVED** | §14.2 "**Only template flags.** … `test_adapter_flags_in_fixtures` checks each golden argv against this per-adapter allow-list". The §14.3 droid list now includes `-r/--resume`, `--last` and the seven subcommands. §14.4 "**User-only:** `--agent <name>` (L3)". §14.6 "**`--trust` is UNVERIFIED and off by default** … only when adapter option `trust_workdir` is true … S-VIBE-9". §7.7 "**Vendor project-local config.**" The kiro fixture's `--trust-workspace` text ("Hooks, agents, MCP servers and workflows from this folder load without asking") supports this. Checklist N15 covers it. |
| 9 | Error codes and ranges unspecified | **RESOLVED** | §5.2 "exit 1 `DB_INTEGRITY_FAILED`". §5.7 "**Per-task values** … `--timeout` 60..604800 s (testing min 1 …) … `INVALID_ARGUMENT`". §13.2 lists `DB_INTEGRITY_FAILED`, `LEAD_NOT_ELIGIBLE` and `INSTALL_PATH_INSECURE`. The checklist now pins one code each in D2 (`INVALID_ARGUMENT`), D5 (`CAPABILITY_MISSING`), E4 (`LEAD_NOT_ELIGIBLE`), F4, G5, G13 (`OP_HASH_MISMATCH`), G18 (`TOKEN_REVOKED`) and H9. I found no `error.code` in the plan or checklist that is missing from §13.2. |
| 10 | No rule for a fixture hash mismatch | **RESOLVED** | §14.11 "**If `test_fixture_hashes` fails, the implementer stops and reports to the coordinator** … Neither the fixture files nor this table, nor the test's expected values, may be changed". N15 adds "editing the fixtures, the plan table, or the test's expected values fails this item". |

### Spot check: r1 fixes (no regressions found)

| r1 # | Still in place | Evidence |
|---|---|---|
| 1 | yes | §7.1 run-session binding, "rejected … **before** it is verified"; §7.6 "**Mesh is not a security sandbox.**"; G25–G27 |
| 2 | yes | §5.6 I1 lists `running`, `cancelling`, `submitted`; I3 is checked at acquisition; A10 |
| 3 | yes | §11.4 "meshd MUST NOT call `waitpid(-1)`"; H13 |
| 4 | yes | §9.4 "Only `unrestricted` may emit a vendor's trust-all flag"; kiro and vibe `elevated` are unsupported |
| 5, 9 | yes | §5.7 table; §7.1 `maxmem=64*1024*1024`; C13 |
| 7 | yes | §16 M1a/M1b/M1c gates with review checkpoints; §0.1 |
| 8, 16 | yes | §11.7 step 2 environ scan, and "set `leases.holder` = the current boot id" |
| 10, 12 | yes | T12 attempt cap; T9/T22 lease split; H14, H16 |
| 11 | yes | §6.2 "Fresh workspace after an inbox lease expiry"; H15 |
| 13 | yes, refined | §12.5 now splits the check into assign-time exit 2 and a T5 failure |
| 14 | yes | §13.2 rule "a code missing from the table is a plan bug" |
| 17, 18 | yes | §13.4 "Responses with exit 1, 7, or 10 are not stored"; §14.1 `launch_enabled:false, launch_milestone:"M3"`; N1, N17 |

### Requested checks

| Check | Result |
|---|---|
| Same-uid threat model honest; Mesh not a sandbox; residual risk stated | **Yes.** §7.6 opens with "**Mesh is not a security sandbox.**" and lists code tampering and secret capture as residual risks. Mitigations 7 and 8 are marked as covering "accidental and casual paths only". §7.6 adds that "the account that types the passphrase … must itself be trusted". §18.2 and Q8 agree, and G29 checks the docs. No remaining overclaim found. A grep for "cannot forge/steal/escape", "guarantee" and "prevent" found only qualified statements. |
| User reasoning effort separate from permission profiles | **Yes.** §9.1 "**Effort is not a permission.** … separate fields (`effort` vs `profile`) everywhere". §9.4 renames `high` to `elevated`. G24 "no profile is passed as an effort value or vice versa". This matches the fixture README note. |
| No profile silently maps to trust-all/unrestricted | **Yes.** In §14.2 trust-all flags appear only in the `unrestricted` column. droid `elevated` = `--auto high`, which the help says "still blocks system-wide changes". agy `dev`/`elevated`, kiro `dev`/`elevated` and vibe `elevated` are unsupported (exit 13). vibe `--trust` is now off by default. |
| Loopback/Unix socket only | **Yes.** §15.3 "MUST NOT create `AF_INET`/`AF_INET6` sockets"; §18.1; C1, C2. |
| No root, deploy, account change or data deletion required | **Yes.** kiro `login`/`logout`/`-d`, droid `update`, agy `update`/`install` and vibe `--setup`/`update`/`config` are never used. The read-only install is a recommendation the user applies. M5 (sudo) is opt-in. "Nothing is ever deleted automatically" (§10.7) is unchanged. |
| M1 split into gated sub-milestones | **Yes.** §16 and checklist §0.1. The gate assignments are now consistent with the build steps (finding 2 checked item by item). |
| Checklist items objectively checkable | **Mostly.** Four small exceptions are listed in new finding 5. |

---

## Part 2 — New findings

### 1. MINOR — The install-root `PYTHONPATH` leaks into vendor CLIs, executor commands and the M2 merge test command.

**Evidence.**
- §3: `bin/mesh` is a "shim: exec python3 -P -s -m mesh "$@" with PYTHONPATH=<realpath of install root>".
- meshd is started through `bin/mesh`, so meshd's own environment contains that `PYTHONPATH`.
- §11.1 step 2 builds the run environment as "(meshd env minus every `MESH_*` var) + …". `PYTHONPATH` therefore reaches runwrap, the vendor CLI, and every Python command the executor runs.
- §10.5 (M2) runs the project's `--test-command`, for example K0's `python3 -m unittest`, presumably with the same inheritance.
- The install root contains top-level `mesh/`, `tests/` and `scripts/` directories (§3). A project whose own `tests` directory is a namespace package (no `__init__.py`), or a project that imports a top-level `mesh` module, would resolve to Mesh's packages. Python prefers a regular package found later on `sys.path` over a namespace portion. The merge gate ("merge only after tests pass") could then run the wrong tests. vibe is itself a Python tool and also inherits the path.

**Required change.** Only Mesh's own processes may receive the install-root `PYTHONPATH`. runwrap must start the vendor CLI with the caller's original `PYTHONPATH`, or with it unset. The same applies to gitops/test-command children. One way to do this: the shim records the original value in a non-`MESH_*` variable, or passes the install root without using `PYTHONPATH` at all. Add a unit test showing that the vendor child's environment does not contain the install root on `PYTHONPATH`.

### 2. MINOR — The install-path check (mitigation 7) only checks one direction.

**Evidence.** §7.6 mitigation 7 refuses to start "If it [the install root] is equal to or inside `workspaces.root`, `$MESH_HOME/runs`, or a registered project repo". The reverse case is not covered: `MESH_HOME` or `workspaces.root` inside the install tree, for example `mesh --home ./h` run from the checkout. Executor working directories would then sit inside Mesh's own git checkout. `../../mesh/core/approvals.py` is one relative edit away, and vendor CLIs that treat the enclosing git root as the project root would see Mesh's source as the project.

**Required change.** Reject any overlap in both directions (install ⊆ X or X ⊆ install). Extend `test_install_path_check` and C14 with a `--home` inside the install root.

### 3. MINOR — Lease-release wording in §6.4, T10 and §11.7 step 6 is stale.

**Evidence.**
- §6.4: "They are released ONLY after the process group is confirmed gone."
- T10: "After the process group is confirmed gone".
- §11.7 step 6: "Tasks in `cancelling` with no live run → `cancelled`".
- §11.4 now says the stronger release condition (group, session, and every `MESH_RUN_ID` carrier gone) applies "On every path", including T10 and recovery.

The condition in §11.4 is the one that applies, but an implementer working from the §6 tables could implement the weaker condition.

**Required change.** Replace these three phrases with a reference to "the §11.4 release condition".

### 4. MINOR — The error code for a `workspaces.root` overlap contradicts the §5.7 rule.

**Evidence.**
- §5.7: an invalid value gets "exit 2 `INVALID_SETTING` … The exception is `scheduler.max_total_runs`".
- §7.6 mitigation 7: "`config set workspaces.root` … that would create such an overlap [is] rejected with the same code" (`INSTALL_PATH_INSECURE`).

**Required change.** Add `workspaces.root` → `INSTALL_PATH_INSECURE` (for an overlap) to the §5.7 exception sentence.

### 5. MINOR — Checklist checkability issues.

**Evidence and required change.**
- **A4.** The check `grep -rn "agent-mesh" mesh tests bin scripts` also matches the default home `~/.agent-mesh` (§5.1), which correct code must contain. The item would fail on a correct build. Change the grep to `/workspace/agent-mesh`, or rename the default home.
- **H17.** The clause "a `m task accept` … while the lease is still held gets rc 6" depends on a window that may last only milliseconds, because the setsid grandchild dies at the first SIGTERM. The clause cannot be checked reliably. Give `spawn_setsid_grandchild` an `ignore_sigterm` option and use it here, so the window lasts at least `kill.grace_s`. Otherwise drop the clause.
- **C14.** "doctor reports `INSTALL_PATH_INSECURE`" gives no exit code for `doctor`. State the expected rc (e.g. rc 2 with that `error.code`, or rc 0 with a named finding).
- **C15.** `/tmp/acc-shadow` is a fixed path, so a leftover from an earlier run would fail the item. Add "`rm -f /tmp/acc-shadow` first", or use a path under `$H`.

### 6. MINOR — The release condition will terminate vendor background daemons started during a run. This is not yet recorded as an M3 observation.

**Evidence.** The fixtures show long-lived vendor services: kiro `serve` ("persistent WebSocket server", `help.txt` L20), droid `daemon` (`help.txt` L24) and agy `remote-control` ("background daemon", `help.txt` L41). If a headless run auto-starts such a service, the service inherits `MESH_RUN_ID`. §11.3 and §11.4 then terminate it at run end and hold the lease until it is gone. That behavior is correct for Mesh. It may still disrupt the user's own interactive vendor sessions if they share the daemon. S-X-6 checks only the process group.

**Required change.** No M1 design change. Add to §14.10 S-X-6, or as a new smoke case, that after a normal exit the test records any process outside the group that carries `MESH_RUN_ID`. Add the item to §19 Q7 and record the result in `docs/adapters.md`.

---

## Summary

All r2 findings 1–10 are resolved, and the r1 fixes still hold. The six new findings are MINOR text or test fixes. None of them requires scope beyond `docs/requirements.md`. Findings 1 and 2 matter most and are cheap to fix during M1a. Finding 1 must be fixed before M2 relies on the merge test gate.
