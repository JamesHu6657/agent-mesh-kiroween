# Implementation Notes — Milestone Gate M1a

Status: Active
Implementer: agy (Gemini 3.8 Flash High)
Date: 2026-11-07

---

## 1. Deferred Review-r3 Findings

Per the M1a implementation instructions, the following findings from `docs/review-r3.md` are deferred:

- **Finding 3 — Lease-release wording in §6.4, T10, and §11.7 step 6:**
  *Status:* Deferred to **M1c**.
  *Details:* In M1c (supervisor/reaper/recovery), the release condition will strictly require that the process group, session, and every process carrying `MESH_RUN_ID` in its environ are confirmed gone before leases are released, per §11.4.

- **Finding 6 — Vendor background daemons carrying `MESH_RUN_ID`:**
  *Status:* Deferred to **M3**.
  *Details:* When background daemons (such as `kiro serve`, `droid daemon`, `agy remote-control`) are started during real vendor runs, their termination behavior and process tree characteristics will be recorded as M3 observations (§14.10 / §19 Q7 / `docs/adapters.md`).

---

## 2. Review-r3 MINOR Items Handled in M1a

- **Finding 1 — Install-root `PYTHONPATH` isolation:**
  *Design:* `bin/mesh` is the entry point shim. To prevent the install root from leaking into vendor CLIs, executor commands, or test-command subprocesses, `bin/mesh` preserves the caller's original `PYTHONPATH` in `_MESH_ORIG_PYTHONPATH` (set to `__UNSET__` if undefined). Mesh processes that spawn external subprocesses (such as `runwrap` spawning vendor CLIs, gitops executing git, or merge executing test commands) explicitly reconstruct the external environment from `_MESH_ORIG_PYTHONPATH`, ensuring that Mesh's own install root never pollutes external Python or tool environments.
  *Verification:* Unit test `test_pythonpath_isolation` asserts that external environment builders strip the install root.

- **Finding 2 — Bidirectional install-path check (Mitigation 7):**
  *Design:* The path validation check implemented in M1.3 will verify bidirectionally:
  1. The install root MUST NOT be equal to or inside `workspaces.root`, `$MESH_HOME/runs`, or any registered project repo root.
  2. Conversely, `workspaces.root`, `$MESH_HOME`, `$MESH_HOME/runs`, and any registered project repo root MUST NOT be inside the install root.
  Any overlap in either direction triggers exit 2 `INSTALL_PATH_INSECURE`.

- **Finding 4 — `workspaces.root` overlap error code:**
  *Design:* Consistent with §7.6 mitigation 7, `mesh config set workspaces.root <path>` that would create an overlap with the install root returns exit 2 `INSTALL_PATH_INSECURE` (treated as an exception to the general `INVALID_SETTING` error code in §5.7).

- **Finding 5 — Checklist checkability:**
  - **A4:** Checklist A4 specifies `grep -rn "agent-mesh" mesh tests bin scripts` expecting "no references to `/workspace/agent-mesh*` or earlier code". Plan §5.1 defines default `MESH_HOME` as `~/.agent-mesh`. We ensure code does not contain any reference to `/workspace/agent-mesh*` or previous implementations. The default home string `~/.agent-mesh` is standard per §5.1.
  - **H17:** Deferred to M1c.
  - **C14:** Verified that `doctor` will report `INSTALL_PATH_INSECURE` and exit with rc 2.
  - **C15:** Deferred to M1c.

---

## 3. M1.1 Skeleton Design Decisions

- **Envelope & Exit Codes:**
  Implemented according to §13.1 and §13.2. All usage/argparse errors output standard JSON envelope with `ok: false`, `error.code == "INVALID_ARGUMENT"`, and `exit_code: 2` (unless `--format text` is explicitly requested).
- **Subcommand Stubs:**
  Unimplemented verbs return exit 2 `NOT_IMPLEMENTED_IN_M1` with `details.milestone` set to the gate or milestone where they are scheduled:
  - Verbs scheduled for M1b: `details.milestone: "M1b"`
  - Verbs scheduled for M1c: `details.milestone: "M1c"`
  - Verbs scheduled for M2 (e.g., `task merge`, `project deliver`, `intake submit --repo`): `details.milestone: "M2"`
  - Verbs scheduled for M3/M4: `"M3"` or `"M4"` respectively.
- **Help Layers:**
  Three layers with `Examples:` sections implemented:
  1. `mesh --help`
  2. `mesh <resource> --help`
  3. `mesh <resource> <verb> --help`
  Plus 8 help topics: `auth`, `approvals`, `exit-codes`, `json`, `workflow`, `adapters`, `env`, `testing`. `mesh help exit-codes` prints the verbatim table from plan §13.2.

---

## 4. M1.2 Storage Design Decisions

- **IDs (§5.4):**
  - Generated as `<prefix>_<26 chars>`.
  - The 26 characters consist of 10 hex digits representing the lowest 40 bits of the Unix millisecond time (`ts_ms & 0xffffffffff`), followed by 16 lowercase base32 characters (`[a-z2-7]`) chosen securely via `secrets.choice`.
  - Supported prefixes: `prj`, `tsk`, `run`, `apr`, `agt`, `usr`, `cred`, `evt`, `msg`, `req`, `boot`, `lead`, `mrg`, `crit`.
  - Verified by `test_ids.py` (structure, regex, uniqueness across 10,000 generated IDs).

- **Clock & Durations (§5.4):**
  - UTC timestamps are formatted as ISO-8601 with 3-digit millisecond precision and 'Z' suffix (`YYYY-MM-DDTHH:MM:SS.mmmZ`).
  - `now_monotonic()` exposes `time.monotonic()` for in-process timing.
  - `parse_duration()` supports suffixes `s`, `m`, `h`, `d`, as well as plain integer seconds. Rejects negative numbers, floating point numbers, and unparseable values.

- **Canonical JSON & Op Hashing (§7.4, G22):**
  - `canonical_json()` outputs deterministic UTF-8 bytes with sorted keys, compact separators `(',', ':')`, and preserved UTF-8 glyphs (`ensure_ascii=False`).
  - Floats are strictly prohibited and rejected at all nesting levels with `ValueError`.
  - `compute_op_hash()` builds the gated operation dictionary per §7.4, strips excluded non-operational parameters (`idempotency_key`, `dry_run`, `approval`, `format`, `if_version`), and computes `sha256:<64 hex chars>`. Verified by `test_canonical.py` (`ACC: G22`).

- **Database & DDL (§5.2, §5.3, §5.5, F3, I9):**
  - SQLite connections enforce WAL journal mode, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`, and `trusted_schema=OFF`.
  - Authoritative DDL with all 20 tables declared `STRICT`.
  - Triggers `events_no_update` and `events_no_delete` prevent any mutation or deletion on `events`, raising `'events are append-only'` (`ACC: F3`).
  - Migrations are tracked via `meta(key='schema_version')`. Connecting to a database with a schema newer than `CURRENT_SCHEMA_VERSION` (1) raises `SchemaTooNewError` (`exit 1 SCHEMA_TOO_NEW`) without modifying or recreating the DB (`ACC: I9`).

- **Event Hash Chain (§5.5):**
  - Hash formula: `hash = sha256(prev_hash + "\n" + canonical_json(event_without_hash_user_mac))`.
  - Initial event uses `prev_hash = "genesis"`.
  - `event_without_hash_user_mac` contains all columns except `seq`, `hash`, and `user_mac` (including `prev_hash`).
  - Verified continuous by `verify_event_chain()` and invariant I7.

- **Database Invariants & Doctor (§5.6, A8, A10):**
  - Invariants I1 through I8 implemented and verified:
    - I1: running task <=> active lease and active run; leased task must be running, cancelling, or submitted (submitted allowed only for launch mode with active run).
    - I2: task lease fence = task fence_seq = run fence.
    - I3: slots_in_use_at_acquire < limit_at_acquire; grandfathered lease rule for agent concurrency and global max total runs.
    - I4: latest event state matches current project and task status.
    - I5: at most 1 active lead per project; active project has exactly 1 active lead.
    - I6: consumed approval must have consumed_at, consumed_by, decided_via.
    - I7: continuous event hash chain.
    - I8: launch cancelling task holds lease; no lease on inactive run.
  - Unit test `test_invariants_valid_states` asserts zero violations for valid states F8, T9, and D15, and verifies that corrupted states report their specific invariant (`ACC: A10`).
  - CLI command `mesh doctor --check-invariants` opens the DB read-only and outputs JSON envelope with `data.violations: []` on clean/fresh DB (`rc 0`), and exit 1 `INTERNAL_ERROR` with violation details on corrupted DB (`ACC: A8`).

---

## 5. M1.3 Daemon Design Decisions

- **Single-Instance flock (§5.1, §11.1):**
  - Acquired on `$MESH_HOME/run/meshd.lock` using `fcntl.flock(fcntl.LOCK_EX | fcntl.LOCK_NB)`.
  - If flock fails with `EAGAIN` or `EACCES`, server startup exits with exit 1 `DAEMON_ALREADY_RUNNING` (`ACC: C5`).
  - Lockfile is released upon server shutdown.

- **Boot Tracking (§5.2, §11.1):**
  - Daemon startup generates a unique `boot_id` (`boot_<26 chars>`) and records a row in `boots` with `started_at`, `status='active'`, `pid`, and `schema_version`.
  - Graceful shutdown updates `stopped_at` and `status='stopped'`.

- **Subreaper Prctl (§11.1):**
  - Linux `prctl(PR_SET_CHILD_SUBREAPER, 1)` is called on startup so the daemon can adopt and reap orphaned descendant processes.

- **Unix Domain Socket & SO_PEERCRED (§5.1, §7.6, C1, C2, C3, C6):**
  - Listens strictly on Unix domain socket `$MESH_HOME/run/mesh.sock` with permission 0600 in a 0700 directory.
  - No TCP or UDP ports are created or bound (`ACC: C1`, `ACC: C2`).
  - Sockets with paths >= 108 characters are rejected immediately with exit 2 `SOCKET_PATH_TOO_LONG`.
  - Every connection verifies caller credentials via `SO_PEERCRED`. If peer UID does not match current daemon process UID, the connection is rejected immediately with 403 / `PEER_UID_MISMATCH` and closed without processing (`ACC: C6`).

- **Process Ancestry & Run-Session Classifier (§7.1, §7.2):**
  - Reads `/proc/<pid>/stat` with a parser resilient to process names containing spaces and parentheses.
  - Inspects `/proc/<pid>/environ` for `MESH_RUN_ID` and walks the parent PID chain via `/proc/<pid>/stat` to associate peer credentials with active run sessions.

- **Authentication & Lockout (§7.2, §7.3, C9, C10, C11, C12, C13, G12, G17):**
  - User passphrase hashed with scrypt: `n=2**15, r=8, p=1, dklen=64, maxmem=64*1024*1024` (64 MiB limit) (`ACC: C13`).
  - Passphrase input supports `--passphrase-fd`, `--passphrase-env`, and interactive tty prompt via `getpass`. In non-interactive mode with no fd/env, fails cleanly with exit 2 `NO_TTY` (`ACC: G17`).
  - Passphrase lockout: 5 consecutive failed attempts trigger 300-second lockout (`ACC: G12`).
  - Agent tokens (`mesh_at_<lookup>_<secret>`) and Run tokens (`mesh_rt_<lookup>_<secret>`) are stored as SHA-256 hashes in `credentials` (`ACC: C9`).
  - Token files must be regular files owned by current user with permissions <= 0600 (`ACC: C10`, `ACC: C12`).
  - Audit logging of requests strips the `"auth"` payload field completely before recording to `mesh.log` or emitting events (`ACC: C11`).

- **Settings Registry (§5.7, B13, D8, D9, D10):**
  - Authoritative settings table implemented with 12 settings, default values, and range validators.
  - Tested minimums enforced for intervals and concurrency.
  - Concurrency validation enforces positive integers (1 to 2^63 - 1), returning exit 2 `INVALID_CONCURRENCY` on 0 or negative numbers (`ACC: D8`, `ACC: D9`, `ACC: D10`).
  - Setting `workspaces.root` validates bidirectionally against the install root, returning exit 2 `INSTALL_PATH_INSECURE` if an overlap would be created (Review-r3 Finding 4).

- **Idempotency & Dry-Run (§7.4, B5, B6):**
  - `--idempotency-key <key>`: cached in `idempotency_keys` table. Repeated requests replay the original status and response body without re-executing side effects (`ACC: B5`).
  - `--dry-run`: runs request logic inside an SQLite transaction, validates policies, computes op hash, collects simulated response and side effects, then rolls back transaction (`ACC: B6`).

- **Subprocess Environment Isolation (Review-r3 Finding 1):**
  - `build_external_env()` restores original `PYTHONPATH` from `_MESH_ORIG_PYTHONPATH` (or deletes it if originally unset), ensuring the Mesh install root never pollutes external Python or tool environments.
  - Strips all `MESH_*` environment variables (except `MESH_RUN_ID` when explicitly provided to runwraps).

- **Filesystem Permissions & Doctor (§7.6, C3, C4, B10):**
  - Umask set to 077 across all Mesh processes.
  - Dirs validated for 0700; files validated for 0600 (`ACC: C3`).
  - Insecure permissions prevent daemon startup with exit 2 `INSECURE_PERMISSIONS` (`ACC: C4`).
  - `mesh doctor --fix-perms` repairs file/directory modes to 0600/0700.

---

## 6. M1.4 Config, Agents, and Adapters Design Decisions

- **Agent Registry & Concurrency Control (§9.1, §9.2, B9, B10, D1, D2, D5, D8, D9, D10, D17):**
  - Agent creation and updates require direct user authentication (`[U]`); agent tokens attempting these mutations receive exit 4 `CAPABILITY_MISSING` (`ACC: D5`).
  - Optimistic concurrency control is supported via `--if-version <int>`; a mismatch returns exit 6 `VERSION_MISMATCH` with current version details (`ACC: B9`).
  - For launch-mode agents, `allowed_models` must be non-empty and `default_model` must belong to `allowed_models` (`ACC: D1`, `ACC: D2`).
  - Profiles default to `default_profile="edit"` and `max_unapproved_profile="dev"` when not explicitly specified (`ACC: B10`).
  - `parse_positive_int` strictly enforces valid positive integers between 1 and 2^63 - 1, rejecting 0, negative values, floats, booleans, and non-numeric formats with exit 2 `INVALID_CONCURRENCY` (`ACC: D8`, `ACC: D9`, `ACC: D10`).
  - Concurrency is validated during both `agent create` and `agent update fx --max-concurrency 0`, preserving prior settings on error (`ACC: D10`).
  - AGY effort encoding conflicts: If an agy model suffix encodes effort (`-low`, `-medium`, `-high`, `-xhigh`, `-max`), any explicit effort specification is rejected with exit 2 `EFFORT_NOT_ALLOWED` (`details.reason: "model_encodes_effort"`) rather than silently dropped (`ACC: D17`).

- **Agent Tokens & Security (§7.1, §15.1, C9):**
  - Issued via `mesh agent token issue <agent_name> [U]`.
  - Format: `mesh_at_<lookup>_<secret>`. The plaintext token is displayed exactly once.
  - Only the 16-character `lookup` and the 32-byte SHA-256 hash (`sha256(secret)`) are stored in the `credentials` table (`ACC: C9`).
  - Revocation marks the credential inactive with `revoked_at`.

- **Adapter Registry & Read-Only Inspection (§14.1, §14.2, §14.7–§14.11, N1, N2, N3, N6, N7, N8, N15, N16, G24):**
  - **Six CLI Integrations:** `droid`, `agy`, `kiro`, `vibe`, `hermes`, `openclaw`, plus `none` and `fake`.
  - **Verification Status in M1:**
    - `droid`, `agy`, `kiro`, `vibe` report `status="partially_verified"`, `launch_enabled=False`, and `launch_milestone="M3"`.
    - `hermes` and `openclaw` report `status="not_installed"`, and any attempt to create an agent or build a run with them raises `AdapterNotInstalled` (exit 13 `ADAPTER_NOT_INSTALLED`) (`ACC: N2`).
    - `none` adapter provides inbox-only support (`delivery="inbox"`).
    - `fake` test double is enabled strictly when `MESH_TESTING=1`. In non-testing environments, `mesh adapter list` omits `fake`, and agent creation with `fake` raises `TestDoubleDisabled` (exit 13 `TEST_DOUBLE_DISABLED`) (`ACC: N3`).
  - **Golden Argv & Real Fixtures:**
    - Implemented according to plan §14.3–§14.6 templates.
    - Verified via unit tests against committed fixtures under `tests/fixtures/real/` without live vendor CLI calls (`ACC: N6`).
    - Real fixture SHA-256 hashes are verified against plan §14.11 (`ACC: N15`).
    - Emitted flags are restricted exclusively to template flags appearing in fixture `--help` outputs (`ACC: N15`).
    - Vibe adapter emits `--trust` only when `trust_workdir` is explicitly true (`ACC: N15`).
  - **Profile Flags and Decoupling (ACC: G24):**
    - Permission profiles (`readonly`, `edit`, `dev`, `elevated`, `unrestricted`) are completely decoupled from reasoning effort levels (`low`, `medium`, `high`, `xhigh`, `max`).
    - Distinct supported profiles generate distinct flag lists.
    - Trust-all flags (`--skip-permissions-unsafe`, `--dangerously-skip-permissions`, `--trust-all-tools`, `--auto-approve`) appear strictly under profile `unrestricted`.
  - **Model Parsing & Inspection (ACC: N7):**
    - `mesh adapter models kiro` parses model IDs from the real fixture `tests/fixtures/real/kiro/3.1.0/list-models.json`.
    - `mesh adapter models vibe` returns `null` (since Vibe provides no model listing command).
    - Neither adapter reads vendor credential or home configuration files.
  - **Prompt Fallbacks & Length Limits (ACC: N16):**
    - 150,000-byte prompts:
      - `droid` and `vibe` pass prompts directly via file flags.
      - `agy` falls back to the exact pointer sentence (`Read your task instructions from ...`) with `--add-dir`.
      - `kiro` under `edit` profile falls back to the pointer prompt.
      - `kiro` under `readonly` profile raises `PromptTooLong` (exit 2 `PROMPT_TOO_LONG`) because readonly mode disallows reading files outside workspace.
    - Rework prompt limit test `test_prompt_t5_too_long` confirms `PROMPT_TOO_LONG` when rework feedback exceeds the 100,000-byte limit.
  - **Stale Probe Version (ACC: N8):**
    - When probed version diverges from verified version, capability verification state transitions to `stale`.

- **Test Suite Hygiene & Cleanup:**
  - Automated test harness (`MeshHarness.teardown()`) ensures atomic removal of temporary home directories (`/tmp/mh-*`) and tripwire files within `try...finally` blocks.
  - Daemons are stopped cleanly with socket disconnect and fallback SIGTERM on recorded PID.
  - Test suite execution leaves zero leftover `/tmp/mh-*` directories.



