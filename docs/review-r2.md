VERDICT: REVISE

# Review r2 of `docs/plan.md` (r2) and `docs/acceptance-checklist.md` (r2)

Reviewer: Kiro CLI (Opus 5.5), independent review, round 2, 2026-11-07.
Inputs read: `docs/requirements.md`, `docs/plan.md`, `docs/acceptance-checklist.md`, `docs/review-r1.md`, `docs/review-r1-fixes.md`, and every file under `tests/fixtures/real/`.

**Reviewer limitation.** Shell was not available, so I could not compute the sha256 values in plan §14.11. I checked the flag citations in §14.2–§14.6 line by line against the fixture files instead. Every line number I checked was correct, including droid `exec-help.txt` L9–L35, L56–L96, L125–L185 and L191; agy `help.txt` L2–L27 and L38; kiro `chat-help.txt` L3–L128 and `help.txt` L8–L20; and vibe `help.txt` L16–L66. The model ids in the examples (`claude-opus-5.5` for kiro, `claude-opus-5-5` for droid, `gemini-3.8-flash-high` for agy) match `list-models.json`, `exec-help.txt` and `models.txt`.

**Overall.** All 18 r1 findings are fixed in the text. The plan now separates reasoning effort from permission profiles, maps trust-all flags only to `unrestricted`, keeps the interface to a Unix socket, and splits M1 into three gated sub-milestones. One MAJOR honesty gap remains in the same-uid threat model (new finding 1). It needs only text and one cheap path check. The other new findings are MINOR.

---

## Part 1 — Status of r1 findings

| # | r1 finding | Status | Evidence (plan / checklist section + short quote) |
|---|---|---|---|
| 1 | Executors can steal same-uid credentials; §7.6 overclaimed | **RESOLVED** (a–d). New finding 1 covers a remaining gap in the residual-risk list. | §7.1 "Run-session binding (best-effort mitigation, §7.6) … rejected with exit 4 `CREDENTIAL_NOT_ALLOWED_IN_RUN` **before** it is verified … does not count toward the lockout". §7.6 "**Mesh is not a security sandbox.**" plus a residual-risk list ("with a stolen relayer token, forge **relayed** approvals"). §7.6 mitigation 3 "never keep the passphrase in a file". Checklist G25–G27, G29, C12. |
| 2 | Invariants I1/I3/I8 contradict valid states | **RESOLVED** | §5.6 I1 "a `task:<id>` lease ⇒ the task status is `running`, `cancelling`, or `submitted`". I3 "At acquisition (recorded on the lease): `slots_in_use_at_acquire < limit_at_acquire`". §5.5 adds the `*_at_acquire` lease columns. `test_invariants_valid_states`; checklist A10. |
| 3 | Subreaper sweep kills meshd's own children; reaping unspecified | **RESOLVED** (new finding 5 covers a small race) | §11.4 "escapees = {p … p not in child_registry … environ contains MESH_RUN_ID=<this run id>}". §11.4 "meshd MUST NOT call `waitpid(-1)` or `os.wait()`". Checklist H13. |
| 4 | Profile `high` = `unrestricted` for kiro/vibe | **RESOLVED** | §9.4 "Only `unrestricted` may emit a vendor's trust-all flag". §14.2 kiro `elevated` "**unsupported** (the only wider flag is trust-all, L32–33)", vibe `elevated` "**unsupported**". Checklist G24 "distinct supported profiles give distinct flag lists". |
| 5 | No settings registry | **RESOLVED** | §5.7 table with `task.default_timeout_s` 3600, `inbox.ack_timeout_s` 600, `outbox.max_attempts` 3, `kill.post_submit_grace_s` 60, and "exit 2 `INVALID_SETTING`". Checklist B13. |
| 6 | Real help fixtures did not exist | **RESOLVED** (hashes not independently verified, see limitation) | `tests/fixtures/real/` exists. §14.11 lists every file with its sha256. §14.3–§14.6 cite file and line, and the citations are correct. `test_fixture_hashes`, `test_adapter_flags_in_fixtures`; checklist N6, N10, N15. |
| 7 | M1 too large, no gates | **RESOLVED** (new finding 2 covers gate mis-assignments) | §16 "M1a — Foundation = M1.1–M1.4 … **review checkpoint**"; M1b, M1c. Checklist §0.1 assigns every M1 item to one gate. I counted all of them, and none is missing or duplicated. |
| 8 | Spawn-window race; `/proc/<pid>/stat` parsing | **RESOLVED** | §11.7 step 2 "first scan `/proc/*/environ` for `MESH_RUN_ID=<run>` … wait up to 5 s for `wrapper.json`". §11.1 "split only the text after the **last** `)`". Checklist I10. |
| 9 | scrypt `maxmem` | **RESOLVED** | §7.1 "`maxmem=64*1024*1024` (`maxmem` is required …)". Checklist C13. |
| 10 | Rework not capped | **RESOLVED** | §6.2 T12 "**Attempt cap:** if `attempt >= max_attempts`, the op is gated like T19". T18 "Same attempt cap". Checklist H14. |
| 11 | Stale inbox agent shares the workspace | **RESOLVED** | §6.2 "Fresh workspace after an inbox lease expiry … `tasks/<tsk>.a<attempt>/` … `--reuse-workspace`", with the residual risk stated. Checklist H15, F10. |
| 12 | Cancel from `submitted` skips `cancelling` | **RESOLVED** | §6.2 T9 "submitted **while the task still holds a lease**". T22 "The task holds **no lease**". Checklist H16. |
| 13 | Prompt fallback undefined | **RESOLVED** (new finding 6 covers the T5 path and lead prompts) | §12.5 gives the exact pointer sentence; "kiro … under profile `readonly` … refused … exit 2 `PROMPT_TOO_LONG`". Checklist N16. |
| 14 | Exit-code table incomplete | **RESOLVED** (new finding 9 covers small leftovers) | §13.2 now lists `INVALID_SETTING`, `PROMPT_TOO_LONG`, `NOT_IMPLEMENTED_IN_M1`, `CREDENTIAL_NOT_ALLOWED_IN_RUN` and `COMPLETION_BLOCKED`, plus "T16 reasons map to codes as …". |
| 15 | Checklist items not objectively checkable | **RESOLVED** | G8 "rc 4 `CAPABILITY_MISSING`". H4 "rc 9 `FENCE_MISMATCH`". E15 "rc 6 `COMPLETION_BLOCKED`". A9 names the first, middle and last commits. F1 adds `rlead`. §14.9 adds `resume_verified`. §7.3 defines "**Own resources**". |
| 16 | Recovery details inaccurate | **RESOLVED** | §11.7 step 2 "(join `leases` on `leases.run_id = runs.id`) … set `leases.holder` = the current boot id". §11.4 residual risk covers adopted runs. Q9; checklist I11. |
| 17 | Idempotency vs. approval re-run | **RESOLVED** | §13.4 "Responses with exit 1, 7, or 10 are not stored … `request_hash` covers cmd + args **including** `approval`". Checklist B12. |
| 18 | Hermes/OpenClaw interim path; `partially_verified` read as usable | **RESOLVED** | §14.7 "**inbox participation, not adapter support**". §14.1 "`launch_enabled:false, launch_milestone:"M3"`". Checklist N1, N17. |

### Checks requested for this round

| Check | Result |
|---|---|
| Same-uid threat model is honest; Mesh is not a sandbox; residual risk stated | Mostly. §7.6, §7.7, §18.2 and Q8 are clear. One guarantee is still overclaimed (new finding 1). |
| Reasoning effort is separate from permission profiles | Yes. §9.1 "**Effort is not a permission.**" §9.4 renames `high` to `elevated`. G24 "no profile is passed as an effort value or vice versa". This matches the fixture README note. |
| No profile silently maps to trust-all | Yes. §9.4, the §14.2 rules and G24. Mesh passes vibe `--trust` under every profile, and what that flag unlocks is not stated (new finding 8). |
| Loopback/Unix socket only | Yes. §15.3 "MUST NOT create `AF_INET`/`AF_INET6` sockets", §18.1, C1 and C2. |
| No root, deploy, account change or data deletion required | Yes. M5 (sudo) is opt-in future work. kiro `-d/--delete-session`, `login` and `logout` are listed under "never use". `workspace delete` is gated, and §10.7 says "Nothing is ever deleted automatically". |
| M1 split into gated sub-milestones | Yes. A few items sit in a gate that cannot pass them (new finding 2). |
| Checklist items objectively checkable | Yes, apart from the ordering conflict in new finding 3. |

---

## Part 2 — New findings

### 1. MAJOR — The user-MAC guarantee is overclaimed. Same-uid code tampering and memory capture are missing from the residual-risk list.

**Evidence.**
- §7.4: "This detects forged direct approvals even when an attacker has rewritten DB rows, because the attacker does not know the real passphrase."
- §7.6 mitigation 6: "`mesh audit verify --user` detects forged direct approvals and user events even after DB edits".
- §1: "`/proc/sys/kernel/yama/ptrace_scope` **absent** | any `box` process can read another `box` process's … memory".
- §11.1: executors get "`PATH` prefixed by `<repo>/bin`". The Mesh source tree is owned by `box`, and the default profile `edit` grants file writes (kiro `--trust-tools=fs_read,fs_write`, droid `--auto low`).
- The §7.6 residual-risk list covers token files, run tokens in memory, `mesh.db` edits, killing meshd, lockout and offline guessing. It does not cover (a) a same-uid process modifying `bin/mesh` or `mesh/*.py`, which the user, meshd and `audit verify` all execute, or (b) reading the passphrase or `K_user` from the CLI or meshd memory during the next direct user request.

Either path defeats the MAC and makes `audit verify` report a clean result. A prompt-injected executor at the default `edit` profile needs only its vendor's file-edit tool, not shell, to modify Mesh's own code if the code is reachable. Run-session binding does not cover this path. The general sentence "Only OS-level separation turns the mitigations above into a boundary" does not repair the specific "detects … even after DB edits" claim.

**Required change.**
- (a) In §7.4 and §7.6 mitigation 6, qualify the claim: the MAC detects DB-only forgeries made by a process that has not captured the passphrase or `K_user` and has not modified Mesh's code.
- (b) Add both paths to the §7.6 residual-risk list, to `mesh help auth`, and to `docs/usage.md`.
- (c) Add one realistic non-root mitigation that mirrors §7.6 mitigation 2. meshd and `doctor` refuse to run (or warn) if the realpath of the Mesh install (`bin/`, `mesh/`) is under `workspaces.root`, `$MESH_HOME/runs`, or a registered project repo. The docs also recommend a read-only install (`chmod -R a-w`), noting that this stops only tools that respect file modes.
- (d) Extend checklist G29 to require the new residual-risk text. Add a C-section item for the install-path check.

### 2. MINOR — Some M1a gate items depend on M1b features. The milestone value at intermediate gates is undefined.

**Evidence.**
- Checklist §0.1 puts B2, B11 and G12 in **M1a** (M1.1–M1.4).
- B2 expects `m project show prj_000… [@grok]` → rc 5 `NOT_FOUND`, but projects are built in M1.5. In M1a, plan §16 M1.1 says "unimplemented verbs return exit 2 `NOT_IMPLEMENTED_IN_M1`".
- B11 expects `m intake submit --repo …` → `details.milestone:"M2"`, but intake itself does not exist until M1.5.
- G12 needs `m event list --type auth.failed`, which is built in M1.6.
- A8's Expect text names post-submit grace, `cancelling` and D15 states, which can only be reached in M1c.
- J6 (M1b) uses `m run list`, but no M1.x step builds the `run` resource.

**Required change.** Move B2, B11 and G12 to M1b, or change the checks so they work in M1a. Say that A8's state-specific clauses apply from M1c, and that A10 covers them earlier. Assign `run list/show/log` to an M1.x step. Define the `details.milestone` value for verbs that are scheduled in M1 but not yet built at an intermediate gate (e.g. `"M1b"`).

### 3. MINOR — The order of the assignment-guard checks is unspecified, which makes N4/N5 conflict. The fake adapter has no profile mapping.

**Evidence.**
- N4 (M1): a task on an agy launch agent → rc 13 `ADAPTER_UNSUPPORTED`.
- N5 (M1): a task on a kiro agent with `--profile dev` → rc 13 `PROFILE_UNSUPPORTED`. For agy and vibe `elevated`, the check is "(after approval)".
- Both cases use a vendor launch agent in M1, so the expected `error.code` depends on a check order that §6.2 AG does not define.
- "(after approval)" means the user is asked to approve an escalation for a profile that can never launch.
- §14.2 gives fake profiles as "n/a". D16 and G13 still need fake agent `fx` to accept `elevated` and `unrestricted`, and G24 runs "over every adapter × profile". If fake emits no flags for any profile, G24's distinct-flags rule fails. If fake supports no profiles, D16 and G13 fail.

**Required change.** Fix the AG order in §6.2. A suggested order: adapter installed → profile supported by the adapter (13 `PROFILE_UNSUPPORTED`) → `launch_enabled` (13 `ADAPTER_UNSUPPORTED`) → model and effort → `permission_escalation` (7). Update N5 to drop "after approval". Give `fake` an explicit mapping, for example a distinct `--fake-profile <name>` argument for each of the five profiles with no trust-all semantics, and say whether G24 covers fake.

### 4. MINOR — On a normal exit, the lease can be released while a setsid escapee is still running.

**Evidence.**
- §6.2 T7: "The lease is held until the process group is gone (launch mode …)".
- The stronger release condition "the process group, the session, and every process carrying this run's `MESH_RUN_ID` are verified gone" appears only in §11.4, which covers terminations.
- §7.1 run-session binding classifies peers against an **active** run R only.

After an exit-0 auto-submit, a `setsid` grandchild that keeps `MESH_RUN_ID` can outlive the lease. It can still edit the workspace after the lead accepts, which is the exact case requirement §6 [SUGGESTED] warns about. It is also no longer classified as "inside a run".

**Required change.** Use the §11.4 release condition on every path (T7, T8 `exit_nonzero`, and recovery). Terminate any leftover `MESH_RUN_ID` carriers through §11.4 before releasing the lease. Classify a peer that carries the `MESH_RUN_ID` of any known run, active or ended, as inside that run. Add an H-section item that combines `succeed` with `spawn_setsid_grandchild`.

### 5. MINOR — The reaper can reap a child that has been spawned but not yet registered.

**Evidence.** §11.4 reaps zombies "with `PPid == getpid()` and a pid not in the registry". `asyncio.create_subprocess_exec` forks and then awaits, so the 5 s reaper task can run before the new pid is registered. A fast `git rev-parse` that exits inside that window would be reaped by the scanner, and asyncio would then see ECHILD and return returncode 255. That is the failure r1 finding 3 was about.

**Required change.** Register each child before yielding to the event loop, or have the reaper skip its scan while any spawn is in flight (an in-flight counter). Say so in §11.4.

### 6. MINOR — `PROMPT_TOO_LONG` at T5 has no caller to receive it, and lead prompts have no fallback.

**Evidence.**
- §12.5 says the kiro `readonly` refusal happens "at assign/start". The full prompt, including the rework feedback, exists only at T5.
- T5 is triggered by the scheduler, so there is no caller to receive exit 2.
- T6 reasons ("`model_not_allowed`, `adapter_unavailable`, `workspace_error`, or `no_response`") do not include it.
- §12.4 runs lead turns with `lead.profile` = `readonly` by default. The §12.6 lead prompt (task table, up to 50 messages, 50 events) has no length rule, so a kiro lead hits the same problem in M2.

**Required change.** Add T6 reason `prompt_too_long`, which notifies the lead. Keep the exit-2 check at assign for a spec that is already over the limit. Apply the §12.5 rule to lead prompts, or cap the size of the lead prompt.

### 7. MINOR — Two effort-handling rules can hide the user's choice or mislabel a failure.

**Evidence.**
- §14.4 agy: "Default: do not pass `--effort` when the model id has an effort suffix." A user-configured effort would then be dropped without notice. §9.1 says effort is "chosen by the user per agent".
- §14.5 maps kiro's "failed to set effort … model does not support additional fields" to reason `effort_rejected`. The fixture README records this error for an **invalid model id**, so a lead would be pointed at the wrong cause.

**Required change.** For agy, reject the combination at validation (exit 2 `EFFORT_NOT_ALLOWED`, with details saying the model id already encodes effort), or pass it through and record it. Never drop it silently. Name the kiro reason `model_or_effort_rejected`, and cite the README observation in the details.

### 8. MINOR — The "never use" lists are incomplete, and vibe `--trust` is emitted under every profile without stating what it unlocks.

**Evidence.**
- The droid list in §14.3 omits the subcommands `update`, `daemon`, `computer`, `plugin` and `mcp`, and top-level `-r/--resume` (`droid/1.3.0/help.txt` L10, L22–L31). The kiro and agy lists do exclude their equivalents.
- The agy list omits `--agent` (`agy/1.5.2/help.txt` L3). For kiro, the plan treats `--agent` as a user-only option that may change permissions.
- §14.6 argv always includes `--trust` ("Trust the working directory for this invocation only", vibe `help.txt` L54–55), even under `readonly`. The plan does not say whether a trusted workdir loads project-local vibe config that could change the effective tool permissions.

**Required change.**
- Add the missing items to the lists, or state the rule "only flags in the §14.x argv template are ever emitted", which `test_adapter_flags_in_fixtures` can enforce.
- Treat agy `--agent` like kiro `--agent`: user-only, with a warning.
- Mark vibe `--trust` UNVERIFIED, with a smoke observation that settles it, and emit it only if headless runs need it.
- Add to §7.7 that vendor project-local config in a trusted workdir may change the effective permissions.

### 9. MINOR — Some error codes and ranges are still unspecified.

**Evidence.**
- §5.2: if `quick_check` fails, "meshd refuses to start and prints instructions". No `error.code` is given, yet §13.2 says "a code missing from the table is a plan bug".
- The range of per-task `--timeout` and `--idle-timeout` is undefined (§5.7 covers only the default). H1 uses `--timeout 2s`.
- Several checklist items give only an rc: E4, D2 (second case), D5, F4 (empty reason), G5, G13, G18, H9 (`--max-attempts 11`). They can still be checked by exit code alone.

**Required change.** Add a code such as `DB_INTEGRITY_FAILED` (exit 1). Give per-task timeouts the §5.7 `task.default_timeout_s` range and testing minimum. Pin the `error.code` in the listed items where it is cheap to do so.

### 10. MINOR — There is no rule for what happens when a fixture hash fails to match.

**Evidence.** §14.11 says the fixtures "MUST NOT be edited, regenerated, or extended", and `test_fixture_hashes` pins the values. I could not verify the hashes myself. If any value in the table is wrong, the implementer's easiest fix is to edit the expected hash, which would silently weaken the provenance check.

**Required change.** Add to §14.11: if `test_fixture_hashes` fails, the implementer stops and reports to the coordinator. Neither the fixture files nor the hash table may be changed to make the test pass.

---

## What PASS requires

Resolve new finding 1 (MAJOR, a text change plus one path check). The MINOR findings should be fixed in the same revision where cheap. None of them requires scope beyond `docs/requirements.md`.
