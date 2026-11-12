VERDICT: REVISE

# Review r1 of `docs/plan.md` (r1) and `docs/acceptance-checklist.md` (r1)

Reviewer: Kiro CLI (Opus 5.5), independent review, 2026-11-06.
Inputs read: `docs/requirements.md`, `docs/plan.md`, `docs/acceptance-checklist.md`.

**Reviewer limitation (not a plan finding).** In this session every shell command was refused by the harness: `Permission request failed: ... "tool permission approval is not supported in non-interactive mode"`. Reading files outside the repo (e.g. the vibe install under `~/.local/share/uv/tools`) was refused the same way. I therefore could **not** run `droid exec --help`, `agy --help`, `kiro-cli chat --help` or `vibe --help` myself. The help captures in `logs/droid-plan-r1c.sanitized.jsonl` are truncated (`...[+40505 chars]`), so they cannot be used either. The vendor flags in §14.3–§14.6 are therefore reviewed for internal consistency and plausibility only, not against live output. Finding 6 asks for the evidence that would make this check possible.

Overall: the plan is detailed, honest about the same-uid trust limit, and covers every [CONFIRMED] requirement at least at the design level. Several internal contradictions and one security gap must be fixed before an implementer can follow it without guessing.

---

## Findings

### 1. MAJOR — Executors can steal same-uid agent credentials and forge relayed approvals or lead actions through the normal `mesh` CLI. The threat model claims this is prevented.

**Evidence.**
- §7.6: "Agents that misuse the **Mesh API** (confused, over-eager, hallucinating, or prompt-injected agents using the `mesh` CLI) cannot: approve their own requests; act outside their capability or scope ... turn the text 'the user approved' into an approval."
- §13.5 stores the relayer token at `~/.config/helper-mesh.token`, a 0600 file owned by `box`. Every launched executor also runs as `box`.
- §7.1: agent tokens are presented by "Env `MESH_TOKEN`, or `--token-file PATH`". In M1 the leads are inbox agents with such tokens (§16 M1: "leads are exercised as inbox-mode agents using their tokens").
- §14.2: the default profile `edit` grants file reads (kiro `--trust-tools=fs_read,fs_write`, droid `--auto low`). §7.7: "Mesh does not sandbox external CLIs."
- §18.7: relayed approvals are allowed by default for every category except `config`.
- Checklist notation `[U]` = `--passphrase-fd 3 3<"$P"`, which models keeping the passphrase in a file.

A prompt-injected executor with read access can run `mesh approval approve <apr> --op-hash <h> --relayed --user-statement "ok" --token-file ~/.config/helper-mesh.token`, or act as the lead with the lead's token file (accept its own task, complete the project). That is a plain `mesh` CLI call, not a deliberate bypass of Mesh, so the §7.6 claim is false as written. Requirement §3 [SUGGESTED] says approvals "can't be satisfied by an executor agent". The same path lets an executor lock the user out (§7.1: 5 bad passphrases, then a 5-minute lockout), which blocks direct approvals.

**Required change.**
- (a) Add **run-session binding** to the server (§4, §7.1). On each connection, take the `SO_PEERCRED` pid and read its `sid` and its PPid ancestry from `/proc`. If the peer belongs to the session of an active launched run, or descends from a known runwrap pid, accept only that run's own run token. Reject agent tokens and user passphrases with exit 4 `CREDENTIAL_NOT_ALLOWED_IN_RUN`, and do not count the attempt toward user lockout. If `/proc` cannot be read, reject.
- (b) Rewrite §7.6 to say exactly what remains: a process that escapes the run session (setsid plus double fork) or edits `mesh.db` directly can still bypass Mesh.
- (c) Add to `docs/usage.md` and `mesh help auth`: never keep the passphrase in a file, and keep agent token files outside every workspace.
- (d) Add checklist items. A fake behavior `steal_token` that reads a token file and calls `mesh approval approve --relayed --token-file …` expects rc 4. A run that sends 6 wrong passphrases expects no lockout of the real user.

### 2. MAJOR — The invariants contradict valid states, so `doctor --check-invariants` (A8, F2) would fail on correct behaviour.

**Evidence.**
- §5.6 I1: "`tasks.status='running'` ⇔ there is exactly one `leases` row with key `task:<id>`, and exactly one run in (`starting`,`running`)".
- §6.2 T7: "The lease is held until the process group is gone (launch mode), then released." A `submitted` task therefore holds a lease and has a live run, which breaks the ⇐ direction of I1. F8 tests exactly this state.
- §5.6 I8: "No task is in `cancelling` without a lease." A cancelling task holds a lease, which also breaks I1's ⇐ direction.
- §5.6 I3: "the count of task leases is ≤ `agents.max_concurrency`". §9.2: "Lowering the limit below the current running count does not kill anything". Checklist D15 creates exactly this state, and A8 requires doctor to be clean after every integration test.

**Required change.** Restate the invariants:
- I1: `running` ⇒ exactly one lease and exactly one active run. A lease ⇒ the task status is in {`running`, `cancelling`, `submitted`}, and `submitted` is allowed only for launch mode while the run is active.
- I3: the lease count may exceed `max_concurrency` only if the limit was lowered after those leases were acquired. Add a check that no lease was acquired while the count was at or above the limit at that time, e.g. by recording `slots_in_use_at_acquire` on the lease.

Add a unit test that runs doctor against the states created by F8, T9 and D15.

### 3. MAJOR — The subreaper sweep kills meshd's own legitimate children, and child reaping with asyncio is unspecified.

**Evidence.**
- §11.4: "also: SIGKILL any process whose PPid == meshd pid and which is not a known live wrapper".
- §4: "Git, test commands, and probes run as asyncio subprocesses". §9.1: `agent create/update` calls `adapter.list_models()` (in M1). §10.5: merge tests run for up to 1800 s.

Any concurrent `git`, test command, probe or `kiro-cli --list-models` child of meshd would be SIGKILLed by an unrelated cancel. That causes spurious `tests_failed` or probe failures. Once meshd is a subreaper, orphans reparented to it also become zombies that nobody reaps. A naive `waitpid(-1)` loop steals exit statuses from asyncio's child watcher, and asyncio then reports returncode 255. The implementer has to guess a strategy.

**Required change.**
- (a) Keep a registry of every child meshd spawns (wrappers, git, tests, probes). Sweep only processes that are not registered **and** whose `/proc/<pid>/environ` contains `MESH_RUN_ID=<the run being terminated>`. Leave all others alone.
- (b) Specify the reaper. Periodically scan `/proc` for `State: Z` with `PPid == getpid()` and a pid not in the registry, and call `os.waitpid(pid, WNOHANG)` for each. Never call `waitpid(-1)`.
- (c) Add an integration test: a cancel with `spawn_setsid_grandchild` while a long-running `adapter probe`, or a stub child, is in flight must not kill the stub.

### 4. MAJOR — The permission-profile mapping makes `high` equal to `unrestricted` for kiro (and probably vibe). This contradicts §7.7 and checklist G24.

**Evidence.**
- §14.2 kiro row: `high` = `--trust-all-tools`, `unrestricted` = `--trust-all-tools`. Vibe row: `high` = `--agent auto-approve`, `unrestricted` = `--auto-approve`. The plan does not show that these two vibe flags differ.
- §7.7: "It never silently widens permissions." §9.4: "Mesh never substitutes a broader profile."
- G24: "no adapter emits a broader profile's flags".

An implementer who follows §14.2 fails G24. One who follows G24 must guess the mapping.

**Required change.** Mark kiro `high` as `unsupported` (exit 13 `PROFILE_UNSUPPORTED`), as was done for agy. For vibe, either cite help text showing that `--agent auto-approve` differs from `--auto-approve`, or mark `high` unsupported. Add a golden test asserting that, for every adapter, the flags for distinct supported profiles are distinct.

### 5. MAJOR — No settings registry. Required defaults are missing, so the implementer must guess.

**Evidence.**
- §5.5: `timeout_s INTEGER NOT NULL` with no default. No default task timeout is given anywhere (grep for `default_timeout` finds nothing). `task create --timeout` is optional (§13.3).
- §6.2 T6: "`inbox.ack_timeout_s`" has no default value.
- §6.6: "`outbox.max_attempts`" has no value. §12.4 says 3, but the key is not named there.
- `config set KEY JSON` (§13.3) does not say how unknown keys or wrong types are handled. Checklist G17 runs `m config set x 1`.
- Settings are scattered: `lease.ttl_s`, `kill.grace_s`, `post_submit_grace_s` (no namespace), `lead.*`, `scheduler.max_total_runs`, `workspaces.root`, `approval.*`, `task.*`.

**Required change.** Add a §5.7 settings table listing every key with its type, default, allowed range, the testing-mode minimum, and who may set it. Specify that unknown keys and wrong types give exit 2 `INVALID_SETTING`. At minimum define `task.default_timeout_s`, `inbox.ack_timeout_s`, `outbox.max_attempts` (= 3), and `kill.post_submit_grace_s`.

### 6. MAJOR — The "real help fixtures captured at planning time and committed" do not exist, so N6 and the honesty rule cannot be checked.

**Evidence.**
- §14.10 (unit tier): "`list_models` parsing against **help text captured at planning time and committed as fixtures** (these are real `--help` outputs, not invented)". Checklist N6: "model parsing uses the real help/model outputs captured at planning time".
- The repository contains only `docs/`, `logs/`, `prompts/` and `.gitignore`. There are no fixtures. The help text in `logs/droid-plan-r1c.sanitized.jsonl` is truncated (`...[+40505 chars]`).
- The planning-time flag claims in §14.3–§14.6 also have no recorded evidence. I could not re-verify them (see the reviewer limitation).

If the implementer writes these fixtures, it may reconstruct or invent them. That is the fake-as-vendor risk that requirement §2 forbids.

**Required change.** Either commit the raw read-only captures now (`--version`, `--help` of each relevant subcommand, `agy models`, `kiro-cli chat --list-models -f json`), each with the command, UTC time and sha256, under `tests/fixtures/real/<adapter>/<version>/help/`. Or specify a `scripts/capture_help.sh` that the implementer runs once in M1.4, which writes the same metadata. Then add a checklist item: the probe tier (N10) compares the live `--help | sha256sum` with the fixture hash, or lists the differences. In §14.3–§14.6, cite the fixture file and line for each flag Mesh emits.

### 7. MAJOR — M1 is clear but very large, and it has no intermediate acceptance gates.

**Evidence.**
- §16 M1 contains nine sub-steps: the full CLI tree, storage, the daemon with auth, config/agents/adapters, projects, the task state machine and scheduler, approvals with audit MAC, supervision with subreaper and crash recovery, and docs.
- "M1 exit criteria: every acceptance-checklist item tagged `[M1]` passes." That is about 130 checklist items, all tagged only `M1`.
- §0 rule 6 requires small green commits, but no checklist subset is attached to each M1.x step.

For a separate implementer model, a phase with one gate makes progress hard to verify and failures expensive to localise.

**Required change.** Tag each checklist item with its M1.x step (e.g. B1–B11 → M1.1/M1.3, D → M1.4/M1.6, G → M1.7, H/I → M1.8). Define a review checkpoint after M1.4 ("M1a: daemon, auth, config, agents"), with its own exit criteria, before M1.5–M1.9 begin.

### 8. MINOR — Recovery can release the lease of a run that is actually alive (spawn-window race).

**Evidence.** §11.7 step 2: "No `wrapper.json` (crash before or during spawn) → `spawn_failed` → T8 `lost_before_spawn`." runwrap writes `wrapper.json` only after Python starts (§11.2 step 1). If meshd dies after `Popen` and before that write, and recovery runs quickly, a live wrapper could be marked failed and the task retried, giving two runs. Separately, `proc_starttime` "field 22 of `/proc/<pid>/stat`" must be parsed after the last `)`, because `comm` may contain spaces and parentheses.

**Required change.** Before declaring `spawn_failed`, scan `/proc/*/environ` for `MESH_RUN_ID=<run>`, the same technique the harness uses in §17.1. If any match exists, adopt or terminate that process first. State the `/proc/<pid>/stat` parsing rule in §11.1.

### 9. MINOR — The scrypt parameters as written fail with Python's default memory limit.

**Evidence.** §7.1: "`scrypt(passphrase, salt16, n=2^15, r=8, p=1, dklen=64)`". OpenSSL needs about 128·r·(n+2) bytes, roughly 32.0 MiB plus overhead. That exceeds the default 32 MiB limit that `hashlib.scrypt` uses when `maxmem` is 0, so the call raises `ValueError: memory limit exceeded`. §1's "`hashlib.scrypt` OK" does not show the parameters that were tested. I could not run this because shell is unavailable. This is from OpenSSL's documented computation.

**Required change.** Specify `maxmem=64*1024*1024` and add a unit test that derives a key with the exact parameters.

### 10. MINOR — Rework is not capped by `max_attempts`.

**Evidence.** §6.2 T13: "A rework run counts as an attempt; see the T19 attempt rule". T13 itself has no `attempt >= max_attempts` gate; only T19 and T20 do. A lead (in M2, an automated lead turn) can reject indefinitely, and each rejection starts a paid run.

**Required change.** Apply the T19 rule to T12/T13: when `attempt >= max_attempts`, `task reject` returns exit 7 `retry_over_limit`, or the task waits in `rework` with `blocked_reason: attempts_exhausted`. Add a checklist item.

### 11. MINOR — After an inbox lease expires, a reassigned run shares the workspace with a possibly still-active stale agent.

**Evidence.** §11.5: "Mesh cannot stop an inbox agent's process. After expiry its writes are rejected, but its file edits cannot be prevented." §6.2 T20: "The branch and worktree are kept. Y continues on the same branch."

**Required change.** After `lease_expired` or `no_response` on an inbox run, the next attempt gets a fresh task directory or worktree (`tasks/<tsk>/a<attempt>/`) unless the lead passes `--reuse-workspace`. Document the residual risk.

### 12. MINOR — A cancel from `submitted` can skip `cancelling` while the process is still alive.

**Evidence.** T22 allows `submitted → cancelled` directly. T7 keeps the lease and process alive during the post-submit grace. T10 requires "the process group is confirmed gone" only on the `cancelling` path.

**Required change.** If the task holds a lease, T22 must go through `cancelling` and terminate the run (§11.4), as T9 does.

### 13. MINOR — The "file reference" prompt fallback is undefined and conflicts with kiro `readonly`.

**Evidence.** §12.5: "Prompts longer than the adapter's argv limit (§14.1) are passed by file reference." kiro and agy have no prompt-file flag (§14.4, §14.5). Kiro `readonly` = `--trust-tools=`, so `fs_read` is not trusted headless and the file may be unreadable (UNVERIFIED per §14.5).

**Required change.** Define the exact fallback text, e.g. "Read and follow `<run_dir>/prompt.md`". Require stdin for adapters that accept it (agy mode C). For kiro `readonly`, refuse prompts over the limit with exit 2 `PROMPT_TOO_LONG` rather than launching a run that cannot read its task.

### 14. MINOR — The exit-code table is incomplete, but B1 requires an exact match.

**Evidence.** `NOT_IMPLEMENTED_IN_M1` (§16 M1.1, B11), `PROMPT_TOO_LONG` (if adopted), `CREDENTIAL_NOT_ALLOWED_IN_RUN` (finding 1), and `INVALID_SETTING` (finding 5) are not in §13.2. B1: "`help exit-codes` lists codes 0–14 exactly as plan §13.2". The table also mixes `commit_failed` (reason) with `GIT_COMMIT_FAILED` (code) without saying which goes in `error.code`.

**Required change.** Add every `error.code` used anywhere in the plan to §13.2, and state that T16 reasons map to the listed codes.

### 15. MINOR — Some checklist items are not objectively checkable as written.

**Evidence.**
- G8 "rc 4 (`SELF_APPROVAL` or `CAPABILITY_MISSING`)", H4 "`FENCE_MISMATCH` or `LEASE_EXPIRED`", H12 and E18 also allow two codes.
- E15 gives "rc 6" with no code.
- A9 "for 3 sampled commits" does not say how they are sampled.
- G10's second case needs grok to be a project lead, but F1 does not create grok with `--can-lead`.
- L5 depends on "the adapter's resume is marked verified" for `fake`, which has no verification state.
- E16 needs `project wait [@grok]`, but §7.3's "own resources" is undefined (is the origin agent an owner?).

**Required change.** Pin one `error.code` per case. Define the sampling (first, middle and last M1 commit). Add `grok --can-lead` or a separate lead-relayer agent to F1. Give `fake` an explicit test-only `resume_verified` option. Define "own resources" in §7.3: projects where the caller is lead or origin, tasks where it is assignee, its own approvals.

### 16. MINOR — Recovery details are inaccurate or missing.

**Evidence.**
- §11.7 step 2 filters "runs ... with `holder_kind='meshd'`", but `holder_kind` is a column of `leases`, not `runs`.
- Adoption does not say that the lease `holder` must be updated to the new boot id.
- §11.4 "Residual risk" says setsid escapees are lost only "while meshd is down". For **adopted** runs, the new meshd is not their ancestor, so escapees reparent to init for the whole remaining lifetime of the run.

**Required change.** Join `leases` in step 2. Add "set `leases.holder` = current boot" to adoption. Correct the residual-risk text and add it to §19 Q9.

### 17. MINOR — The idempotency and approval re-run interaction is ambiguous.

**Evidence.** §13.4 stores every response except exit 1 and 10, so exit 7 is stored. `request_hash` covers "cmd+args, excluding auth". It is not said whether `approval` is included. A lead that reuses its idempotency key when re-running with `--approval` either replays the stored exit 7 or gets `IDEMPOTENCY_KEY_REUSED`, depending on the implementer's choice.

**Required change.** Specify that exit 7 responses are not stored, or that `approval` is excluded from `request_hash` and a stored 7 is re-evaluated. Add a test.

### 18. MINOR — Hermes and OpenClaw: the honest status is right, but the interim integration path is not stated.

**Evidence.** §14.7/§14.8 report `not_installed`, which is correct and honest, and M4 is conditional. Requirement §2 [CONFIRMED] lists all six as in scope. §12.3 already lets "any agent that can run shell commands" use inbox mode through adapter `none`. Separately, `adapter list` in M1 shows `partially_verified` for the four installed CLIs even though launching them returns exit 13 until M3 (N4). A reader may take `partially_verified` to mean usable.

**Required change.** In §14.7/§14.8 and `docs/adapters.md`, state that once installed, Hermes and OpenClaw can join immediately as `adapter=none, delivery=inbox` agents. Label this inbox participation, not adapter support. Add `launch_enabled: false` (with `milestone`) to `adapter list`/`show` output in M1, and check it in N1.

---

## Summary against the review criteria

| Criterion | Assessment |
|---|---|
| (1) Implementable without guessing | Mostly. Findings 2, 3, 5, 13, 14 and 17 force guesses. |
| (2) Coverage of [CONFIRMED] / [SUGGESTED] | All [CONFIRMED] items are mapped (checklist §Z). Deviations are listed in §18. Real-CLI launch is phased to M3, which is allowed. |
| (3) Security | Unix socket only, `SO_PEERCRED`, op-hash binding, expiry, idempotent decisions, user MAC audit and the no-sandbox statement are all good. Finding 1 (credential theft by executors) is a real gap and an overclaim. |
| (4) Supervision | Fencing, no auto retry, adopt-or-fail recovery and kill-before-release are sound. Findings 2, 3, 8, 11, 12 and 16 need fixes. |
| (5) Adapter honesty | Statuses are honest, fake is clearly a test double, and UNVERIFIED items are explicit. Finding 4 (profile widening) and finding 6 (missing evidence) remain. I could not re-verify the flags live. |
| (6) Checklist objectivity | Mostly concrete commands and expected results. See findings 2 and 15. |
| (7) First phase | Clear scope, but oversized without gates (finding 7). |

PASS requires resolving findings 1–7. The MINOR findings should be addressed in the same revision where cheap.
