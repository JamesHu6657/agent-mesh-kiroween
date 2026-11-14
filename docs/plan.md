# Agent Mesh v1 — Implementation Plan

Status: **plan r3**, written by the planner (Factory droid, Opus 5.5) on 2026-11-06 UTC; revised after review r1 (`docs/review-r1.md`, fixes in `docs/review-r1-fixes.md`) and review r2 (`docs/review-r2.md`, fixes in `docs/review-r2-fixes.md`). Both reviews were VERDICT REVISE; every finding and the section it changed is listed in the matching fixes file.
Next steps: review by Kiro CLI (Opus 5.5), implementation by agy (Gemini 3.8 Flash High), and final acceptance by Worker 2.
Inputs: `docs/requirements.md` (frozen 2026-11-07 Asia/Shanghai), plus a read-only inspection of the CLIs installed on this box (`--help`, `--version`, and model-list commands only; no model sessions were started).

---

## 0. How to read this plan

- **MUST**, **MUST NOT**, and **SHOULD** are normative. Anything not specified here is the implementer's choice, but it must not contradict this plan.
- **UNVERIFIED** marks a fact about an external CLI that was not proven on this box. Each UNVERIFIED item names the test that will settle it (see §14 and §17).
- Deviations from `[SUGGESTED]` requirements are listed in §18. Open questions are in §19.
- Rules for the implementer:
  1. Write fresh code. Do not read, copy, or reference anything in `/workspace/agent-mesh*` or any earlier AgentMesh repository.
  2. Use only the Python standard library, at runtime and in tests. Do not use network installs (`pip install`, etc.).
  3. The default test suite MUST NOT start any real vendor CLI (droid, agy, kiro-cli, vibe, hermes, openclaw). Real-CLI tests are opt-in (§14.10).
  4. Never read credential, token, or cookie files of any vendor (`~/.factory`, `~/.gemini`, `~/.kiro`, `~/.vibe`, etc.). Adapters only invoke CLIs.
  5. Mesh never runs `git push`, never deploys, and never opens a network listener.
  6. Commit in small steps that follow the milestone order (§16). Each commit leaves `scripts/test.sh` green.

---

## 1. Environment facts observed at planning time (2026-11-06)

| Item | Observed | Notes |
|---|---|---|
| OS | Linux 6.12.113+ x86_64, 8 CPUs, user `box` (uid 1000) | single machine |
| yama ptrace restriction | `/proc/sys/kernel/yama/ptrace_scope` **absent** | any `box` process can read another `box` process's `/proc/<pid>/environ` and memory (trust impact in §7.6) |
| Python | 3.13.5, sqlite3 module linked to SQLite **3.46.1** (WAL and `STRICT` tables OK), `hashlib.scrypt` OK, `ctypes` prctl OK | **chosen runtime** |
| pytest | not installed | use `unittest` |
| Node | v26.10.0, `node:sqlite` works (SQLite 3.53.4) | alternative, not chosen |
| Go / cargo / bun | go 1.27.3, cargo, bun present | not chosen |
| `sqlite3` CLI | **not installed** | tests use Python's sqlite3 |
| git | 2.47.3; **no global `user.name`/`user.email`** | merge commits need a repo-local identity (§10.6, §19) |
| tools | `ss`, `ps`, `jq` 1.7, `timeout`, `setsid` present; `make` absent | used by the acceptance checklist |
| droid | `/home/box/.local/bin/droid`, **1.3.0** | help-inspected; fixtures `tests/fixtures/real/droid/1.3.0/` (§14.11) |
| agy | `/home/box/.local/lib/antigravity-cli/agy` (on PATH), **1.5.2** | the requirements say 1.4.8; the binary was updated (mtime 2026-11-06 22:30). Versions MUST NOT be hardcoded. Fixtures `tests/fixtures/real/agy/1.5.2/` |
| kiro-cli | `/home/box/.local/bin/kiro-cli`, **3.1.0** | help and model-list JSON inspected; fixtures `tests/fixtures/real/kiro/3.1.0/` |
| vibe | `/home/box/.local/bin/vibe` → uv tool `mistral-vibe`, **3.1.0** | help-inspected; fixture `tests/fixtures/real/vibe/3.1.0/help.txt` |
| hermes | `command -v hermes` → not found | **not installed** (`tests/fixtures/real/hermes/not-installed.txt`) |
| openclaw | `command -v openclaw` → not found | **not installed** (`tests/fixtures/real/openclaw/not-installed.txt`) |

The real CLI outputs were captured read-only by the coordinator at 2026-11-07T06:47:40Z and committed under `tests/fixtures/real/` (see its `README.md`). §14.11 lists every file with its sha256. Any statement in §14 about a vendor flag cites one of these files; anything not in them is marked UNVERIFIED.

---

## 2. Language and runtime choice

**Decision: Python ≥ 3.11 (developed and tested on the installed 3.13.5), standard library only.**

Why:
1. **It is already installed and complete.** `sqlite3` (WAL, STRICT), `asyncio` (Unix-socket server, subprocesses), `subprocess`/`os` (`start_new_session`, `killpg`), `socket` (`AF_UNIX`, `SO_PEERCRED`), `hashlib.scrypt`, `hmac`, `secrets`, `fcntl.flock`, `ctypes` (for `prctl(PR_SET_CHILD_SUBREAPER)`), `argparse`, `json`, and `unittest` cover every need. Nothing must be downloaded.
2. **No paid services and no network.** Python satisfies both constraints.
3. **Mature SQLite bindings and a predictable process model.** This matters for crash-safety work.
4. **Easy for the implementer model.** Python is easy to generate and review correctly.

Rejected alternatives:
- **Node 26 + `node:sqlite`.** It is viable, but the binding is newer and less battle-tested for crash semantics. Process-group handling would also need extra care.
- **Go 1.27 and Rust.** Both need a third-party SQLite driver fetched from the network.
- **Bun.** It is not needed.

Third-party Python packages MUST NOT be imported. `cryptography` happens to be installed system-wide, but it MUST NOT be used.

---

## 3. Repository layout (to be created by the implementer)

```
bin/mesh                       # POSIX sh shim: exec python3 -P -s -m mesh "$@" with PYTHONPATH=<realpath of install root> (§7.6 mitigation 8)
mesh/__init__.py               # __version__ = "1.0.0.dev0"
mesh/__main__.py               # -> mesh.cli.main()
mesh/cli/main.py               # argparse tree, global flags, envelope, exit codes
mesh/cli/help.py               # layered help text, topics, examples
mesh/cli/commands/*.py         # one module per resource (agent, project, task, ...)
mesh/client.py                 # Unix-socket client (request/response, long-poll)
mesh/errors.py                 # MeshError(code, exit_code, message, details)
mesh/core/db.py                # connection factory, PRAGMAs, tx helper, migrations
mesh/core/schema.py            # DDL (§5.5) as ordered migrations
mesh/core/ids.py, clock.py     # id generation, UTC time, durations
mesh/core/canonical.py         # canonical JSON + op hashing (§7.4)
mesh/core/states.py            # transition tables (§6) — single source of truth
mesh/core/authz.py             # principals, capabilities, authorization matrix (§7.3)
mesh/core/approvals.py         # approval logic, policies, user MAC (§7.4)
mesh/core/policy.py            # model / effort / profile / concurrency validation (§9)
mesh/core/events.py            # append-only event log + hash chain + outbox fan-out (§12)
mesh/core/redact.py            # secret redaction (§15)
mesh/core/workspaces.py        # dirs, worktrees, merge (§10)
mesh/core/gitops.py            # the only module allowed to call git; allow-listed subcommands
mesh/core/prompts.py           # executor / lead prompt rendering, deliverable parsing
mesh/daemon/server.py          # asyncio Unix-socket server, auth, dispatch, dry-run
mesh/daemon/handlers/*.py      # one module per resource; pure tx logic + "effects"
mesh/daemon/scheduler.py       # tick loop: promote, start, timeouts, leases, outbox, expiry
mesh/daemon/supervisor.py      # spawn runwrap, watch, kill, adopt
mesh/daemon/recovery.py        # boot-time recovery (§11.7)
mesh/runwrap.py                # per-run wrapper process (§11.2)
mesh/adapters/base.py          # Adapter interface (§14.1)
mesh/adapters/{droid,agy,kiro,vibe,hermes,openclaw,none,fake}.py
mesh/adapters/registry.py
mesh/testing/fake_agent.py     # the fake CLI used by the fake adapter (§14.9)
tests/unit/ tests/integration/ tests/recovery/   # default, offline, fake-only
tests/probe/                   # opt-in (MESH_PROBE=1): read-only real CLI --help/--version/models
tests/smoke/                   # opt-in (MESH_SMOKE=1): real model sessions, consumes quota
tests/shims/                   # tripwire stubs named droid/agy/kiro-cli/vibe/hermes/openclaw
tests/helpers.py               # MeshHarness (tmp home, daemon, cli())
scripts/test.sh                # runs unit + integration + recovery
docs/                          # plan, checklist, README-style usage (docs/usage.md), docs/adapters.md
```

---

## 4. Architecture overview

```
 agents / user shells                    meshd (one per MESH_HOME)
 ┌───────────────┐   NDJSON over        ┌──────────────────────────────────────────┐
 │ mesh CLI      │── Unix socket ──────▶│ server (auth, authz, idempotency, dryrun)│
 │ (thin client) │◀─ $MESH_HOME/run/    │ handlers → SQLite tx → effects queue     │
 └───────────────┘   mesh.sock (0600)   │ scheduler tick (1 s + on-demand wake)    │
                                        │ supervisor ── spawns ──▶ runwrap ──▶ vendor CLI
                                        │ outbox / inbox long-poll                 │
                                        └───────────────┬──────────────────────────┘
                                                        │ single writer
                                                 $MESH_HOME/mesh.db (WAL)
```

- **meshd** is the only process that writes the DB. The CLI never opens `mesh.db` directly. The exceptions are `mesh init` (it creates the DB before any daemon exists) and `mesh doctor --check-invariants` (it opens the DB read-only when the daemon is down).
- **Single-instance lock.** meshd takes `fcntl.flock(LOCK_EX|LOCK_NB)` on `$MESH_HOME/run/meshd.lock`. If the lock is already held, it exits 1 with `DAEMON_ALREADY_RUNNING`. It writes its pid to `run/meshd.pid`.
- **Boot id.** Every meshd start inserts a `boots` row (`boot_<id>`). It is used for lease holders and recovery.
- **Subreaper.** At start, meshd calls `prctl(PR_SET_CHILD_SUBREAPER, 1)` through ctypes. Descendants that daemonize then reparent to meshd instead of init, so meshd can kill them (§11.4). meshd keeps a registry of every child it spawns and reaps orphans only by explicit pid, never with `waitpid(-1)` (§11.4).
- **Signals.** On SIGTERM or SIGINT, meshd stops accepting connections, finishes the current transaction, and marks its boot row `stopped_at`. It then exits WITHOUT killing runs. Runs keep going and are re-adopted on the next start (§11.7).
- **Request protocol.** One JSON object per line, request then response, on the Unix socket:
  ```json
  {"v":1,"request_id":"req_…","cmd":"task.create","auth":{"type":"token","token":"…"},
   "args":{…},"idempotency_key":"…","dry_run":false}
  ```
  The `auth` field may also be `{"type":"user","passphrase":"…"}` or `{"type":"none"}`. The response is the CLI envelope (§13.1) plus `"exit_code"`.
  Limits: a request is at most 1 MiB, the server allows 64 concurrent connections, and the read timeout is 30 s (except for `*.wait` commands).
  The `auth` field MUST be stripped before anything is logged.
- **Transactions and effects.** Each handler runs inside `BEGIN IMMEDIATE`. It returns a result plus a list of **effects**, such as `spawn_run`, `kill_run`, `git_merge`, `wake_waiters`, or `deliver_lead_wake`.
  Effects run only after `COMMIT`. A dry run executes the handler, then rolls back, and returns `{"would": {"transitions": […], "events": […], "effects": […]}}`.
  A crash between COMMIT and an effect is handled by recovery (§11.7). Every effect is re-derivable from DB state.
- **asyncio.** One event loop serves both the socket server and the scheduler. DB calls are synchronous on the loop thread (they are short). Git, test commands, and probes run as asyncio subprocesses with timeouts.

---

## 5. Storage

### 5.1 Location and permissions
- `MESH_HOME` defaults to `~/.agent-mesh` and can be overridden with the env var `MESH_HOME` or the flag `--home`.
- Layout: `mesh.db` (+`-wal`, `-shm`), `run/` (socket, lock, pid), `runs/<run_id>/`, `workspaces/`, `logs/meshd.log`, `merges/<merge_id>/`.
- The process umask is `0o077` in both the CLI and the daemon.
- At startup meshd checks that `MESH_HOME` is owned by the current uid and that `mode & 0o077 == 0`. If not, it refuses to start (exit 2, `INSECURE_PERMISSIONS`). `mesh doctor --fix-perms` sets 0700 on directories and 0600 on files.
- The Unix socket path must be ≤ 107 bytes. If it is longer, meshd fails with `SOCKET_PATH_TOO_LONG`. Tests use short temp homes (`/tmp/mh-XXXX`).

### 5.2 SQLite settings
On every connection, run:
`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA trusted_schema=OFF;`

Use `sqlite3.connect(path, isolation_level=None)` and start every write with an explicit `BEGIN IMMEDIATE`. `synchronous=FULL` makes committed transactions durable across power loss.

At boot, run `PRAGMA quick_check`. If it does not return `ok`, meshd refuses to start with exit 1 `DB_INTEGRITY_FAILED` (details: the first 20 `quick_check` lines) and prints instructions. Mesh never deletes or recreates an existing DB on its own.

### 5.3 Migrations
- `meta(key='schema_version')` holds the schema version. Migrations are an ordered list in `mesh/core/schema.py`, each applied in its own transaction.
- A DB with a newer schema than the code knows is refused with exit 1 and `SCHEMA_TOO_NEW`.

### 5.4 IDs, time, durations
- **IDs** are `<prefix>_<26 chars>`: 10 hex digits of the Unix millisecond time, then 16 lowercase base32 chars from `secrets`. Prefixes: `prj tsk run apr agt usr cred evt msg req boot lead mrg crit`.
- **Times** are stored as UTC ISO-8601 with milliseconds and a `Z` suffix. In-process timers use `time.monotonic()`. Deadlines are persisted as wall-clock UTC.
- **Durations** are given as `30s`, `15m`, `2h`, `7d`, or plain integer seconds.

### 5.5 Schema (authoritative DDL; all tables `STRICT`)

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL, updated_by TEXT NOT NULL) STRICT;
CREATE TABLE boots (id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at TEXT NOT NULL,
  stopped_at TEXT, crashed INTEGER NOT NULL DEFAULT 0) STRICT;

CREATE TABLE principals (id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('user','agent','system')),
  name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, disabled_at TEXT) STRICT;
-- exactly one 'user' row (name 'user') and one 'system' row (name 'mesh') created by `mesh init`.

CREATE TABLE credentials (id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL REFERENCES principals(id),
  kind TEXT NOT NULL CHECK (kind IN ('passphrase','agent_token','run_token')),
  lookup TEXT UNIQUE,                 -- public token id part; NULL for passphrase
  secret_hash BLOB NOT NULL,          -- sha256(token) | scrypt verifier (32 bytes)
  salt BLOB, kdf_params TEXT,         -- passphrase only
  scope_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT) STRICT;

CREATE TABLE auth_failures (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, at TEXT NOT NULL,
  peer_pid INTEGER) STRICT;

CREATE TABLE agents (id TEXT PRIMARY KEY REFERENCES principals(id),
  adapter TEXT NOT NULL,              -- droid|agy|kiro|vibe|hermes|openclaw|none|fake
  binary_path TEXT,                   -- optional override; otherwise PATH lookup
  delivery TEXT NOT NULL CHECK (delivery IN ('launch','inbox')),
  can_lead INTEGER NOT NULL DEFAULT 0 CHECK (can_lead IN (0,1)),
  capabilities_json TEXT NOT NULL DEFAULT '["intake"]',   -- subset of ["intake","relay"]
  default_model TEXT, allowed_models_json TEXT NOT NULL DEFAULT '[]',
  default_effort TEXT, allowed_efforts_json TEXT NOT NULL DEFAULT '[]',
  max_concurrency INTEGER NOT NULL CHECK (max_concurrency >= 1),
  default_profile TEXT NOT NULL DEFAULT 'edit',
  max_unapproved_profile TEXT NOT NULL DEFAULT 'dev',
  adapter_options_json TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;

CREATE TABLE adapter_probes (adapter TEXT NOT NULL, probed_at TEXT NOT NULL,
  binary_path TEXT, version TEXT, help_sha256 TEXT, flags_json TEXT NOT NULL,
  models_json TEXT, status TEXT NOT NULL, PRIMARY KEY (adapter, probed_at)) STRICT;

CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','paused','completed','cancelled')),
  repo_path TEXT, base_branch TEXT, integration_branch TEXT, base_commit TEXT,
  test_command_json TEXT,             -- JSON argv array | '"none"' | NULL (non-code project)
  test_timeout_s INTEGER NOT NULL DEFAULT 1800,
  origin_principal_id TEXT REFERENCES principals(id), origin_ref TEXT,
  created_by TEXT NOT NULL, created_via TEXT NOT NULL,   -- direct|relayed|token
  completion_report_json TEXT,
  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  completed_at TEXT) STRICT;
CREATE TABLE acceptance_criteria (id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL,
  text TEXT NOT NULL, created_at TEXT NOT NULL, superseded_at TEXT) STRICT;
CREATE TABLE project_deps (project_id TEXT NOT NULL REFERENCES projects(id),
  depends_on_project_id TEXT NOT NULL REFERENCES projects(id),
  PRIMARY KEY (project_id, depends_on_project_id),
  CHECK (project_id <> depends_on_project_id)) STRICT;
CREATE TABLE lead_assignments (id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  assigned_by TEXT NOT NULL, via TEXT NOT NULL, reason TEXT,
  started_at TEXT NOT NULL, ended_at TEXT, ended_reason TEXT,
  session_id TEXT) STRICT;            -- vendor session id of the lead's turns (if resumable)
CREATE UNIQUE INDEX one_current_lead ON lead_assignments(project_id) WHERE ended_at IS NULL;

CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL, spec TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('code','general')),
  status TEXT NOT NULL,               -- §6.2
  priority INTEGER NOT NULL DEFAULT 0,
  assignee_agent_id TEXT REFERENCES agents(id),
  model TEXT, effort TEXT, profile TEXT NOT NULL,
  timeout_s INTEGER NOT NULL, idle_timeout_s INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
  fence_seq INTEGER NOT NULL DEFAULT 0,
  queued_at TEXT, offered_at TEXT,
  workspace_path TEXT, branch TEXT, base_commit TEXT,
  last_session_id TEXT,
  deliverable_json TEXT, failure_json TEXT, merge_json TEXT,
  created_by TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;
CREATE TABLE task_deps (task_id TEXT NOT NULL REFERENCES tasks(id),
  depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on_task_id),
  CHECK (task_id <> depends_on_task_id)) STRICT;

CREATE TABLE runs (id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('task','lead_turn','merge_test','external')),
  task_id TEXT REFERENCES tasks(id), project_id TEXT NOT NULL REFERENCES projects(id),
  agent_id TEXT REFERENCES agents(id), attempt INTEGER, fence INTEGER NOT NULL,
  adapter TEXT NOT NULL, model TEXT, effort TEXT, profile TEXT,
  status TEXT NOT NULL CHECK (status IN ('starting','running','exited','killed','lost','spawn_failed')),
  boot_id TEXT NOT NULL, wrapper_pid INTEGER, pgid INTEGER, proc_starttime INTEGER,
  run_dir TEXT NOT NULL, argv_redacted_json TEXT NOT NULL DEFAULT '[]',
  started_at TEXT, deadline_at TEXT, ended_at TEXT,
  exit_code INTEGER, signal INTEGER, end_reason TEXT,
  session_id TEXT, result_json TEXT, lead_assignment_id TEXT) STRICT;
CREATE UNIQUE INDEX one_active_run_per_task ON runs(task_id)
  WHERE status IN ('starting','running') AND kind IN ('task','external');

CREATE TABLE leases (key TEXT PRIMARY KEY,   -- 'task:<id>' | 'leadturn:<prj>' | 'merge:<prj>'
  run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
  holder_kind TEXT NOT NULL CHECK (holder_kind IN ('meshd','agent')),
  holder TEXT NOT NULL,               -- boot id or agent id
  agent_id TEXT, fence INTEGER NOT NULL,
  slots_in_use_at_acquire INTEGER, limit_at_acquire INTEGER,          -- task leases only (I3)
  total_at_acquire INTEGER, total_limit_at_acquire INTEGER,            -- set only when a global cap applies
  acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL) STRICT;

CREATE TABLE approvals (id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
  category TEXT NOT NULL, op_type TEXT NOT NULL, op_json TEXT NOT NULL, op_hash TEXT NOT NULL,
  summary TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','expired','consumed','cancelled')),
  decided_by TEXT, decided_via TEXT CHECK (decided_via IN ('direct','relayed')),
  relayed_by TEXT, relayer_credential_id TEXT, user_statement TEXT, evidence_ref TEXT,
  decision_note TEXT, decided_at TEXT, user_mac TEXT,
  consumed_at TEXT, consumed_by TEXT, consumed_request_id TEXT) STRICT;
CREATE UNIQUE INDEX one_open_approval_per_op ON approvals(op_hash)
  WHERE status IN ('pending','approved');

CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
  ts TEXT NOT NULL, type TEXT NOT NULL,
  project_id TEXT, task_id TEXT, run_id TEXT, approval_id TEXT,
  actor_id TEXT NOT NULL, actor_kind TEXT NOT NULL,       -- user|agent|system
  via TEXT NOT NULL,                  -- direct|relayed|token|run_token|lead_turn|system
  relayed_by TEXT, credential_id TEXT,
  from_state TEXT, to_state TEXT, reason TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}', request_id TEXT,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL, user_mac TEXT) STRICT;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;

CREATE TABLE outbox (id TEXT PRIMARY KEY, event_seq INTEGER NOT NULL REFERENCES events(seq),
  recipient_id TEXT NOT NULL REFERENCES principals(id),
  channel TEXT NOT NULL CHECK (channel IN ('inbox','lead_wake')),
  dedup_key TEXT NOT NULL UNIQUE, project_id TEXT, type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','delivering','delivered','acked','dead','superseded')),
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
  delivered_at TEXT, acked_at TEXT, delivering_run_id TEXT, last_error TEXT,
  created_at TEXT NOT NULL) STRICT;

CREATE TABLE idempotency (principal_id TEXT NOT NULL, key TEXT NOT NULL,
  cmd TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
  exit_code INTEGER NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (principal_id, key)) STRICT;
```

**Event hash chain.** For each event, `hash = sha256(prev_hash + "\n" + canonical_json(event without hash/user_mac))`. The first event uses `prev_hash = "genesis"`. The chain is tamper-evident against accidental corruption, not against a deliberate attacker (§7.6).

### 5.6 Invariants (checked by `mesh doctor --check-invariants`, by every integration test teardown, and by the crash tests)
- **I1.** `tasks.status='running'` ⇒ there is exactly one `leases` row with key `task:<id>`, and exactly one run in (`starting`,`running`) for that task. Conversely, a `task:<id>` lease ⇒ the task status is `running`, `cancelling`, or `submitted`, and `submitted` is allowed only for a launch-mode task whose run is still active (post-submit grace, T7).
- **I2.** For a task lease, `leases.fence = tasks.fence_seq = runs.fence`.
- **I3.** At acquisition (recorded on the lease): `slots_in_use_at_acquire < limit_at_acquire`, and, if a global cap was set, `total_at_acquire < total_limit_at_acquire`. The current number of task leases of an agent may exceed `agents.max_concurrency` only because the limit was lowered after acquisition (§9.2): at least `count − max_concurrency` of those leases must have `limit_at_acquire > agents.max_concurrency`. The same rule applies to `scheduler.max_total_runs`.
- **I4.** Every change of `tasks.status` or `projects.status` has a matching event with `from_state` and `to_state`. The latest such event's `to_state` equals the current status.
- **I5.** Each project has at most one lead assignment with `ended_at IS NULL`. Active projects have exactly one.
- **I6.** `approvals.status='consumed'` ⇒ `consumed_at`, `consumed_by`, and `decided_via` are non-null.
- **I7.** The event hash chain is continuous.
- **I8.** A launch-mode task in `cancelling` holds a lease (inbox-mode T10 is immediate, so no inbox task stays in `cancelling` after commit). No lease exists without an active run.
- **I9.** For a code task that is `merged`, `merge_json.merge_commit` exists in the repo and is an ancestor of the integration branch.

Unit test `test_invariants_valid_states` builds the DB states produced by F8 (`submitted` with a live run in post-submit grace), T9 (`cancelling` with a live run), and D15 (limit lowered below the running count), and expects zero violations. Corrupted variants (a lease on an `accepted` task; a lease with `slots_in_use_at_acquire >= limit_at_acquire`) must each report the specific invariant.

### 5.7 Settings registry (authoritative)
Settings live in the `settings` table. `mesh config set KEY JSON` is user-only (direct, category `config`). meshd validates every write: an unknown key, a value of the wrong JSON type, or a value outside the range → exit 2 `INVALID_SETTING` (details: `key`, `expected`, `got`). The exception is `scheduler.max_total_runs`, which uses `INVALID_CONCURRENCY` (§9.2). Passphrase-source and authentication checks (exits 2 and 3) run before setting validation. Integers must be JSON integers (not a bool, float, or string). `mesh config list` prints every key below with its value, its default, and whether it was set. Testing mode (§17.1) lowers only the minimums in the "Testing min" column.

| Key | Type | Default | Range | Testing min | Used in |
|---|---|---|---|---|---|
| `lead.default_agent` | agent name or null | null | enabled agent with `can_lead` | — | §8 |
| `lead.wake_debounce_s` | int | 5 | 0..3600 | 0 | §12.4 |
| `lead.profile` | profile name | `readonly` | §9.4 names | — | §12.4 |
| `lead.turn_timeout_s` | int | 1800 | 60..86400 | 1 | §12.4 |
| `scheduler.max_total_runs` | int or null | null | 1..9223372036854775807 | — | §9.2 |
| `workspaces.root` | absolute path | `$MESH_HOME/workspaces` | existing dir owned by the uid, mode 0700 | — | §10.1 |
| `task.default_timeout_s` | int | 3600 | 60..604800 | 1 | T5 deadline when `task create --timeout` is omitted |
| `task.default_max_attempts` | int | 3 | 1..`task.max_attempts_cap` | — | §11.6 |
| `task.max_attempts_cap` | int | 10 | 1..100 | — | §11.6 |
| `inbox.ack_timeout_s` | int | 600 | 10..86400 | 1 | T6 `no_response` |
| `lease.ttl_s` | int | 120 | 10..3600 | 1 | §11.5 |
| `kill.grace_s` | int | 15 | 1..300 | 1 | §11.4 |
| `kill.post_submit_grace_s` | int | 60 | 0..3600 | 0 | T7, §11.3 |
| `outbox.max_attempts` | int | 3 | 1..10 | — | §6.6, §12.4 |
| `approval.ttl_seconds` | int | 86400 | 60..2592000 | 1 | §7.4 |
| `approval.policy.<category>` | string | §7.4 table | §7.4 values | — | §7.4 |
| `notify.command` (M4) | JSON argv array or null | null | non-empty array of strings | — | §12.3 |

**Per-task values** given on `task create` / `task retry` use the same ranges: `--timeout` 60..604800 s (testing min 1; default `task.default_timeout_s`), `--idle-timeout` 0 (off) or 60..604800 s (testing min 1), `--max-attempts` 1..`task.max_attempts_cap`. Out of range → exit 2 `INVALID_ARGUMENT` (details: `param`, `expected`, `got`).

---

## 6. State machines

`src` is the actor that may trigger a transition. "lead" means the project's current lead. "user" means the user principal (direct, or relayed where policy allows, §7.5). The user may perform every lead transition; such events are recorded with `actor_kind=user`. Every transition writes exactly one event with `from_state`, `to_state`, `actor`, `via`, `ts`, `reason`, and `request_id`. Any transition not listed here is rejected with exit 6 `INVALID_TRANSITION`. An op on a project that is not `active` (except P3/P5 on a `paused` project, and read commands) is rejected first with exit 6 `PROJECT_NOT_ACTIVE`; `INVALID_TRANSITION` is used only when the project is active.

### 6.1 Project

| # | From | To | Trigger | Actor | Guards / effects |
|---|---|---|---|---|---|
| P1 | — | active | `intake submit` / `project create` | user, or an agent holding `intake` | A lead is resolved (`--lead`, otherwise `lead.default_agent`, otherwise exit 2 `NO_LEAD`). The lead agent must be enabled and `can_lead` (otherwise exit 2 `LEAD_NOT_ELIGIBLE`, details: `agent`, `reason` = `not_can_lead` or `disabled`; `set-lead` uses the same code). Inserts `lead_assignments` and the criteria. If a repo is given: validate it, create the integration branch and worktree (M2). Effect: wake the lead (`project.created`). |
| P2 | active | paused | `project pause` | user | No new runs start. Running runs continue unless `--cancel-running` is given. |
| P3 | paused | active | `project resume` | user | Scheduler wake. |
| P4 | active | completed | `project complete --report` | lead | (a) every non-cancelled task is `merged` (code) or `accepted` (general); (b) the project has no leases; (c) no `pending` or `approved` approvals reference the project; (d) the report covers **every current criterion id** with `met:true` and non-empty `evidence` (otherwise exit 2 `CRITERIA_REPORT_INCOMPLETE`). If (a), (b), or (c) fails → exit 6 `COMPLETION_BLOCKED` with `details.blocking` listing the task ids, lease keys, and approval ids. (d) is checked first. Effects: notify the user and the origin agent; revoke lead-turn tokens. No user re-confirmation. |
| P5 | active, paused | cancelled | `project cancel` | user | All non-terminal tasks → `cancelled` (running ones via `cancelling`). Pending approvals → `cancelled`. Worktrees are kept. |

`completed` and `cancelled` are terminal in v1 (no reopen).

### 6.2 Task

States: `created, assigned, queued, running, cancelling, submitted, rework, accepted, merging, merge_failed, merged, failed, escalated, cancelled`.
Terminal states: `merged`, `cancelled`, and `accepted` when `kind=general`.

| # | From | To | Trigger | Actor | Guards / effects |
|---|---|---|---|---|---|
| T1 | — | created | `task create` (no `--assignee`) | lead | Project active. Deps exist in the same project and are acyclic (DFS; otherwise exit 2 `DEP_CYCLE`). |
| T2 | — | assigned | `task create --assignee X` | lead | T1 guards plus the assignment guards (AG). |
| T3 | created | assigned | `task assign --to X` | lead | AG |
| T4 | assigned | queued | scheduler | system | Every dep is `accepted` or `merged`, every project dep is `completed`, and the project is `active`. Sets `queued_at`. If the assignee is inbox-mode, emit `task.offered` to the assignee and set `offered_at`. |
| T5 | queued | running | scheduler (launch mode) or `task start` (inbox mode, assignee only) | system / assignee | The agent has a free slot (I3). The model is still allowed. The lease is free. The project is active. In one tx: `fence_seq += 1`, `attempt += 1`, insert run + lease, issue a run token. Effect: spawn (launch mode). |
| T6 | queued | failed | scheduler | system | Reasons: `model_not_allowed`, `adapter_unavailable`, `workspace_error`, `prompt_too_long` (the prompt rendered at T5, including rework feedback, exceeds the limit for an adapter/profile with no fallback, §12.5; details: `bytes`, `limit`, `adapter`, `profile`), or `no_response` (inbox agent did not `task start` within `inbox.ack_timeout_s` of `offered_at`). Notifies the lead. |
| T7 | running | submitted | `task submit` (fenced), or the run exits 0 without a submit (auto-submit, `submitted_by: system`) | executor / system | The fence matches. The deliverable is stored. Launch mode: the lease is held until the §11.4 release condition holds (process group, session, and every process carrying this run's `MESH_RUN_ID` verified gone). After a fenced submit the main process gets at most `kill.post_submit_grace_s`, then §11.4; leftovers found when the wrapper exits are terminated at once (§11.3). Inbox mode releases at once. Notifies the lead. |
| T8 | running | failed | run ended non-zero, `timeout`, `idle_timeout`, `lease_expired` (inbox), `lost_on_restart`, `spawn_failed`, `kill_failed_then_dead`; or `task fail --reason` (fenced) | system / executor | For launch mode, only after the §11.4 release condition holds (leftovers are terminated first, §11.3). Lease released. Notifies the lead. **No automatic retry.** |
| T9 | running; submitted **while the task still holds a lease** (launch-mode post-submit grace) | cancelling | `task cancel` | lead | Effect: terminate the run (§11.4). |
| T10 | cancelling | cancelled | supervisor | system | After the process group is confirmed gone (inbox mode: immediately; the fence is invalidated). |
| T11 | submitted | accepted | `task accept` | lead | No lease on the task (exit 6 `RUN_STILL_ACTIVE`). The lead is not the assignee. |
| T12 | submitted | rework | `task reject --reason R` | lead | Reason non-empty. Stored as rework feedback. No lease on the task (exit 6 `RUN_STILL_ACTIVE`). **Attempt cap:** if `attempt >= max_attempts`, the op is gated like T19: user approval category `retry_over_limit` (exit 7); without it the task stays `submitted`. |
| T13 | rework | queued | scheduler | system | Same guards as T4. A rework run counts as an attempt; the cap was already enforced at T12/T18. |
| T14 | accepted | merging | `task merge` | lead | `kind=code`. The project merge lease is free. M2. |
| T15 | merging | merged | merge pipeline | system | Clean merge, tests passed, commit created (§10.5). |
| T16 | merging | merge_failed | merge pipeline | system | Reasons: `conflict`, `tests_failed`, `worktree_dirty`, `integration_dirty`, `commit_failed`, `interrupted`. The integration branch is left unchanged. Notifies the lead. |
| T17 | merge_failed | merging | `task merge` | lead | Retry after the cause is fixed. |
| T18 | merge_failed | rework | `task reject --reason` | lead | Same attempt cap as T12. |
| T19 | failed, escalated | queued | `task retry [--model M] [--reuse-workspace]` | lead | If `attempt >= max_attempts`, the op is gated: user approval category `retry_over_limit` (exit 7). Workspace rule after `lease_expired`: see below. |
| T20 | assigned, queued, rework, failed, escalated, merge_failed | assigned | `task reassign --to Y --reason` | lead | AG for Y. The attempt rule is the same as T19 when coming from failed or escalated. The branch and worktree are kept and Y continues on the same branch, except after `lease_expired` (fresh workspace, see below; `--reuse-workspace` keeps it). |
| T21 | failed | escalated | `task escalate --reason` | lead | Notifies the user and the origin agent. |
| T22 | created, assigned, queued, rework, submitted, accepted, failed, escalated, merge_failed | cancelled | `task cancel --reason` | lead | The task holds **no lease**. A `submitted` task that still holds a lease goes through T9 (`cancelling`) and T10 instead, so the run is always terminated first. |

**Assignment guards (AG)**, checked in this order; the first failure decides the `error.code`:
1. The agent exists (exit 5 `NOT_FOUND`) and is enabled. It is not the current lead of this project (exit 2 `LEAD_CANNOT_BE_ASSIGNEE`).
2. For `delivery=launch`: the adapter is installed (exit 13 `ADAPTER_NOT_INSTALLED`).
3. For `delivery=launch`: the resolved profile is supported by the adapter (exit 13 `PROFILE_UNSUPPORTED`, §14.2). No approval is ever requested for a profile that can never launch.
4. For `delivery=launch`: the adapter is `launch_enabled` (exit 13 `ADAPTER_UNSUPPORTED`, `details.milestone`; every vendor adapter in M1).
5. The model and effort resolve and are allowed (exit 2, §9.1).
6. If the profile rank exceeds `max_unapproved_profile`, user approval category `permission_escalation` is required (exit 7).
Inbox-mode agents skip steps 2–4; their profile is recorded but emits no flags.

**Fresh workspace after an inbox lease expiry.** If the previous run of a task ended with `lease_expired`, the stale inbox agent may still be editing files (§11.5). The next attempt (T19 retry or T20 reassign) therefore gets a **new** workspace: plain dir `tasks/<tsk>.a<attempt>/`, or for code tasks a new worktree there on a new branch `mesh/<prj>/<tsk>-a<attempt>` created from the old branch's committed tip. `tasks.workspace_path` and `tasks.branch` are updated; the old dir and branch are kept (never deleted automatically). The lead may pass `--reuse-workspace` to keep the old dir and branch; the event records that choice. Residual risk: commits the stale agent makes to the **old** branch after expiry are not merged, but its writes to any other path cannot be prevented.

**Dependent of a cancelled task.** When a dep becomes `cancelled`, every dependent task in `assigned` emits `task.blocked` to the lead. The lead must run `dep remove` or cancel the dependent.

### 6.3 Run
`starting → running → exited | killed | lost`, and `starting → spawn_failed`.
- `exited` has an `exit_code`.
- `killed` has `end_reason` set to `timeout`, `idle_timeout`, `cancelled`, `post_submit_grace`, or `project_cancelled`.
- `lost` means meshd found it dead with no `exit.json` at recovery.
- `external` runs (inbox agents) go `running → exited` (on submit or fail) or `running → lost` (on lease expiry).

### 6.4 Lease
`acquired → renewed* → released`, or `acquired → expired`.
- **meshd-held leases** (launch mode) are renewed every tick while the wrapper is alive. They are released ONLY after the process group is confirmed gone. They never "expire" into a free slot: after a meshd crash, recovery decides (§11.7).
- **Agent-held leases** (inbox mode) are extended by `heartbeat` or `progress`. If `expires_at < now`, the lease expires and the task goes `running → failed(lease_expired)`.

### 6.5 Approval
`pending → approved | rejected | expired | cancelled`; `approved → consumed | expired`.
- Expiry is set by the scheduler when `expires_at < now`.
- `cancelled` is set by the requester, or by the system when the project is cancelled or the lead changes (only for approvals requested by the old lead).

### 6.6 Outbox message
`pending → delivering → delivered → acked`.
- `delivering → pending`: a lead wake failed (`attempts += 1`, backoff 30 s / 120 s / 600 s).
- `pending → dead`: when `attempts ≥ outbox.max_attempts`, the system emits `lead.unreachable`.
- `pending | delivered → superseded`: on lead change, messages for the old lead are re-created for the new lead.
- For inbox messages, `delivered` means "returned by `inbox list` or `inbox wait` at least once". They are redelivered until `acked`, so delivery is at-least-once.

### 6.7 Lead assignment
`current → ended` (on `set-lead`, with `ended_reason`). Rows are never deleted, so history is complete.

---

## 7. Identity and authority

### 7.1 Principals and credentials
| Principal | How created | Credential | How presented |
|---|---|---|---|
| **user** (exactly one) | `mesh init` | **Passphrase** (≥ 12 chars). Stored as `hashlib.scrypt(passphrase, salt=salt16, n=2**15, r=8, p=1, dklen=64, maxmem=64*1024*1024)` (`maxmem` is required: these parameters need about 32 MiB plus overhead, above OpenSSL's default limit; unit test `test_scrypt_params` derives a key with exactly these values); bytes 0–31 are the **verifier** (stored); bytes 32–63 are `K_user`, the MAC key (never stored) | `--user` makes the CLI read the passphrase with `getpass` from **/dev/tty**, or from `--passphrase-fd N`. MUST NOT be read from argv, env, or the JSON stdin body (exit 2 `PASSPHRASE_SOURCE_REJECTED` if a `passphrase` key appears in input). |
| **agent** | `mesh agent create --user` | **Agent token** `mesh_at_<lookup10>_<secret43>` (32 random bytes, base64url). DB stores `lookup` and `sha256(token)`. Shown once by `mesh agent token issue`. | Env `MESH_TOKEN`, or `--token-file PATH` (the file must be mode 0600, else exit 3). Never in argv. |
| **run** (child of an agent) | meshd, at T5 or at lead-turn start | **Run token** `mesh_rt_…` with `scope_json` = `{"role":"executor","run_id","task_id","project_id","fence"}` or `{"role":"lead","run_id","project_id","lead_assignment_id"}`. Expires at the run deadline + 300 s. Revoked when the run ends. | Injected only into the child env as `MESH_TOKEN`, together with `MESH_RUN_ID`, `MESH_FENCE`, `MESH_TASK_ID`, `MESH_PROJECT_ID`, and `MESH_SOCKET`. |
| **system** (`mesh`) | `mesh init` | none | Used for scheduler and supervisor events. |

Authentication rules:
- Token check: look up the row by `lookup`, then `hmac.compare_digest(sha256(token), secret_hash)`. Then check `revoked_at`, `expires_at`, and the principal's `disabled_at`. Failure → exit 3.
- Passphrase check: recompute scrypt and `compare_digest` the verifier. After **5 failures within 5 minutes** from peers outside any run session, user auth is locked for 5 minutes (exit 3 `LOCKED_OUT`). Every failure writes an `auth.failed` event that contains no secret.
- meshd reads `SO_PEERCRED` on each connection and rejects any uid other than its own. The peer pid is recorded in events for audit.
- **Run-session binding (best-effort mitigation, §7.6).** For each connection meshd classifies the peer pid. The peer is **inside run R** if any of these holds: (a) its session id (field 6 of `/proc/<pid>/stat`, parsed as in §11.1) equals the sid of an active run R (the wrapper pid); (b) walking `PPid` upward reaches R's wrapper pid before pid 1 or meshd; (c) `/proc/<pid>/environ` contains `MESH_RUN_ID=<R>` for any **known** run R, active **or ended** (a leftover of an ended run is still classified as inside it; R's token is revoked, so such a peer can authenticate as nothing). Rules (a) and (b) use active runs only, because sids and pids are reused. If the peer's `/proc` entries cannot be read, the request is rejected with exit 4 `CREDENTIAL_NOT_ALLOWED_IN_RUN`.
  - A peer inside run R may authenticate **only** with R's own run token. Any other credential (an agent token, another run's token, a passphrase) is rejected with exit 4 `CREDENTIAL_NOT_ALLOWED_IN_RUN` **before** it is verified. A passphrase rejected this way is not checked and does not count toward the lockout.
  - Each rejection writes an `auth.credential_in_run` event (peer pid, run id, credential kind, token `lookup` if any; never a secret) and one `security.credential_in_run` message per run to the user and the project lead.
  - Limit: a descendant that leaves the session and clears `MESH_RUN_ID` (e.g. `setsid` plus a double fork) is not recognized. This is a mitigation, not a boundary.

### 7.2 Capabilities
- **user**: everything. It is the only principal that can change config, agents, tokens, models, concurrency, or relay grants (category `config`, which is direct-only and cannot be relayed or delegated).
- **agent token**: the capabilities in `agents.capabilities_json`:
  - `intake` is granted by default to every agent. It allows `intake submit`.
  - `relay` is granted only by the user. It allows `--relayed` user-authority actions (§7.5).
  - Lead rights and assignee rights are **derived at request time**. An agent has lead rights on project P iff it holds P's current `lead_assignment`. It has assignee rights on task T iff it is T's assignee and the agent is inbox-mode.
  - Because rights are derived rather than baked into tokens, a lead change revokes lead rights immediately.
- **executor run token**: only `task heartbeat`, `task progress`, `task submit`, and `task fail` on `scope.task_id`, with the fence equal to the current lease fence (else exit 9). It may also read its own task and project (`task show`, `project show`). Nothing else: no intake, no approvals, no relay, no task creation.
- **lead run token**: the lead commands on `scope.project_id`, while `scope.lead_assignment_id` is still current (else exit 9 `LEAD_SUPERSEDED`).

### 7.3 Authorization matrix (enforced in `mesh/core/authz.py`; every handler calls `authorize(ctx, op, resource)`)

| Operation | user | lead (token or lead run token) | assignee (inbox agent token) | executor run token | relayer (`relay` cap) | other agent |
|---|---|---|---|---|---|---|
| config.*, agent.*, agent token.* | ✔ (direct only) | ✘ | ✘ | ✘ | ✘ | ✘ |
| intake submit / project create | ✔ | ✔ (intake cap) | ✔ (intake cap) | ✘ | ✔ | ✔ (intake cap) |
| project pause/resume/cancel/set-lead/update | ✔ | via approval | ✘ | ✘ | ✔ `--relayed` if policy allows | ✘ |
| project complete/note/deliver | ✔ | ✔ | ✘ | ✘ | ✘ | ✘ |
| task create/assign/reassign/accept/reject/retry/escalate/cancel/merge, dep * | ✔ | ✔ | ✘ | ✘ | ✘ | ✘ |
| task start | ✔ | ✘ | ✔ (own task) | ✘ | ✘ | ✘ |
| task heartbeat/progress/submit/fail | ✔ | ✘ | ✔ (fenced) | ✔ (scoped, fenced) | ✘ | ✘ |
| approval request | ✔ | ✔ | ✘ | ✘ | ✔ | ✘ |
| approval approve/reject | ✔ (direct) | ✘ | ✘ | ✘ | ✔ `--relayed`, only if not the requester and not the project lead | ✘ |
| approval check/consume | ✔ | ✔ (requester only) | ✘ | ✘ | requester only | ✘ |
| inbox list/wait/ack | own (`--user`) | own | own | ✘ | own | own |
| read commands (list/show/wait/event/run log) | ✔ | own projects | own tasks | own task | ✔ | own resources |
| daemon stop | ✔ | ✘ | ✘ | ✘ | ✘ | ✘ |
| audit verify | ✔ (direct) | ✘ | ✘ | ✘ | ✘ | ✘ |

**Agent callers on user-only rows.** Any agent credential (with or without `--relayed`, with or without the `relay` capability) calling a `config.*`, `agent.*`, or `agent token.*` op gets exit 4 `CAPABILITY_MISSING`: `config` is never relayable, so `relay` grants nothing there.

**Own resources** (read rows above): for an agent token, the projects where the agent is the current lead, a past lead (read-only history), or the origin principal (`projects.origin_principal_id`), and their tasks, events, and runs; tasks where it is the assignee; approvals it requested; its own inbox. `project wait` and `task wait` are read commands. A relayer reads all projects and approvals (✔).

**Check order for `approval approve/reject`** (one `error.code` per case): (1) an agent caller without `--relayed` → 4 `CAPABILITY_MISSING`; (2) `--relayed` from a principal without `relay`, or from any run token → 4 `RELAY_NOT_ALLOWED`; (3) the relayer is the approval's requester or the current lead of its project → 4 `SELF_APPROVAL`; (4) the category policy is `direct_only` → 12 `CHANNEL_NOT_ALLOWED`; (5) wrong `--op-hash` → 12 `OP_HASH_MISMATCH`; (6) state checks (expired, consumed, conflict). Authentication and run-session binding (§7.1) run before (1).

### 7.4 Approvals

**Categories and the operations Mesh actually gates:**

| Category | Mesh-enforced op(s) | Default policy |
|---|---|---|
| `config` | config set, agent create/update/disable, token issue/revoke | `direct_only` (fixed, not changeable) |
| `lead_change` | `project set-lead` | `direct_or_relayed` |
| `project_control` | `project pause/resume/cancel` | `direct_or_relayed` |
| `scope_change` | `project update` (goal / acceptance criteria) | `direct_or_relayed` |
| `permission_escalation` | assign/reassign/retry with a profile rank above the agent's `max_unapproved_profile`, or a budget option above the agent cap (e.g. vibe `max_price_usd`) | `direct_only` (default; the user may relax it to `direct_or_relayed`). An escalated executor could read other agents' credentials (§7.6), so this needs the user's direct factor. |
| `retry_over_limit` | retry, reassign, or reject (T12/T18) when `attempt >= max_attempts` | `direct_or_relayed` |
| `delete` | `workspace delete` of unmerged work | `direct_or_relayed` |
| `deliver_to_base` | `project deliver` (fast-forward the repo's base branch) | `not_required` (lead authority); the user may tighten it |
| `spend_money`, `deploy`, `delete_external`, `external_other` | **not enforceable by Mesh**: request/check/consume tokens for actions the agent performs itself | `direct_or_relayed` |

The policy is set with `mesh config set approval.policy.<category> '"direct_only"'` (user only). Allowed values are `direct_only` and `direct_or_relayed`, plus `not_required` for `deliver_to_base` only.

**Operation hash.** For a gated op, the server builds:
```json
{"v":1,"op":"<cmd>","requested_by":"<principal id>","project":"<prj|null>","target":"<id|null>",
 "params":{ …server-normalized args with defaults resolved… }}
```
- Excluded keys: `idempotency_key`, `dry_run`, `approval`, `format`, `if_version`.
- Floats are forbidden in params.
- `canonical = json.dumps(op, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode()`
- `op_hash = "sha256:" + sha256(canonical).hexdigest()`

**Flow for a gated op initiated by an agent (e.g. the lead):**
1. The agent runs the op. The server sees that the policy requires approval. It inserts an `approvals` row (`pending`, `expires_at = now + ttl`) or reuses the open row with the same `op_hash` (unique index). It then returns exit 7 `APPROVAL_REQUIRED` with `{approval_id, op_hash, category, summary, expires_at}`.
2. Notifications go to the user inbox and to the project's origin agent inbox (§12.3).
3. The user decides:
   - Directly: `mesh approval approve <apr> --op-hash <hash> --user`.
   - Or a relayer: `mesh approval approve <apr> --op-hash <hash> --relayed --user-statement "<verbatim user words>" [--evidence-ref <chat msg id>]`.
   - The supplied `--op-hash` MUST equal the stored hash, and the server re-hashes the stored `op_json` to detect DB corruption (exit 12 `OP_HASH_MISMATCH`).
4. The requester re-runs the **identical** op with `--approval <apr>`. In one tx the server recomputes `op_hash` from the new args. It requires `status='approved'`, `expires_at > now`, `requested_by` == caller, and the channel allowed by the current policy. It executes the op and marks the approval `consumed`. Any mismatch → exit 12 with no state change: a recomputed `op_hash` that differs (including a different caller, because `requested_by` is hashed, or a different op such as `--profile unrestricted` instead of `elevated`) → `OP_HASH_MISMATCH`; state problems → `APPROVAL_EXPIRED`, `APPROVAL_CONSUMED`, `APPROVAL_REJECTED`, `APPROVAL_CANCELLED`, or `CHANNEL_NOT_ALLOWED`.

**Flow for an external op** (deploy, spend): `approval request --category deploy --summary … --operation '<json>'` → user decision → `approval consume <apr> --op-hash <h>` before acting (exit 0 exactly once; a second consume → 12 `APPROVAL_CONSUMED`). `approval check` is a read-only test.
Honest limit: Mesh cannot see whether the agent actually asked first, or whether it later performed the external action. That is bounded only by the agent's prompt instructions and its CLI permission flags (§7.7).

**Idempotency of decisions.**
- Approving an already-approved request returns the current state (exit 0, `already_decided:true`) with no new event.
- The opposite decision gets exit 6 `DECISION_CONFLICT`.
- A decision after expiry gets exit 12 `APPROVAL_EXPIRED`.
- Normal idempotency keys also apply (§13.4).

**Expiry.**
- `--expires-in` is bounded to [60 s, 30 d]. The default comes from the setting `approval.ttl_seconds` (86400).
- In testing mode (§17.1) the minimum is 1 s.
- An approved but unconsumed approval also expires at `expires_at`.

**Audit and the user MAC (tamper-evidence).**
- A direct decision stores `user_mac = HMAC-SHA256(K_user, "mesh-approval-v1\n" + id + "\n" + op_hash + "\n" + decision + "\n" + decided_at)`.
- Every user-actor event stores `user_mac = HMAC-SHA256(K_user, "mesh-event-v1\n" + event.hash)`.
- `K_user` exists only in memory during the request.
- `mesh audit verify --user` derives `K_user` from the passphrase typed now. It verifies all direct decisions, all user events, and the hash chain. It reports mismatches and exits 12 `AUDIT_MISMATCH` if any are found.
- **What this detects, and what it does not.** It detects forged direct approvals and user events written by a process that edited DB rows but has **not** captured the passphrase or `K_user` and has **not** modified Mesh's code (`bin/mesh`, `mesh/*.py`, or the interpreter it runs). It does **not** detect forgeries by a same-uid process that captured either secret (from CLI or meshd memory, or from a wrapped or shadowed `mesh` during a direct request), or that modified Mesh's code: such a process can compute valid MACs, and a tampered `audit verify` can report a clean result (§7.6 residual risk).
- Relayed decisions carry no user MAC. They are attributable only to the relayer's credential and are labelled `trust:"relayed_unverified"`; direct decisions are `trust:"direct_verified"`. `audit verify` lists relayed decisions grouped by relayer and credential.

### 7.5 Relayed user decisions and their honest trust limits
- A relayer is an agent the user granted `relay` (typically Worker 2). It can:
  - approve or reject approvals with `--relayed`;
  - perform user-authority project ops (`set-lead`, `pause`, `resume`, `cancel`, `update`) with `--relayed --user-statement …`.
  Both require that the category policy is `direct_or_relayed`.
- Constraints:
  - The relayer ≠ the approval's `requested_by`.
  - The relayer is not the current lead of the affected project.
  - Executor run tokens never carry `relay`.
  - `config` is never relayable.
- What is recorded: `decided_via='relayed'`, `relayed_by`, `relayer_credential_id`, the verbatim `user_statement` (required, ≤ 4000 chars, redacted), and `evidence_ref`. `approval show`, `approval check`, `approval consume`, and `event list` display `via: relayed (unverified; by <agent>)` and `trust:"relayed_unverified"`, distinct from `via: direct` and `trust:"direct_verified"`.
- **Trust statement (shown in `mesh help approvals`).** A relayed approval means exactly this: "agent X, holding credential C, asserted that the user said S." Mesh cannot verify that the user said it. A compromised, mistaken, or prompt-injected relayer can forge relayed approvals, and so can any same-uid process that reads the relayer's token file (§7.6). For sensitive categories, the user should set the policy to `direct_only`.

### 7.6 Threat model (honest)
**Mesh is not a security sandbox.** meshd, the CLI, and every agent Mesh launches or talks to run as the same Linux user (`box`). Any process of that uid can read every file the user can read, write every file the user can write (including Mesh's own code, unless the user made it read-only), and (with no yama ptrace restriction on this box) read other processes' `/proc/<pid>/environ` and memory. Without OS-level separation, which v1 does not use, Mesh cannot isolate agents from each other or from itself. In particular, a same-uid executor that ignores the rules can read other agents' Mesh credentials unless OS-level separation is used.

**What meshd enforces** against agents that use the `mesh` CLI or socket with the credentials they were given:
- Every request carries a real credential (passphrase, agent token, or run token) and is authorized by principal, capability, scope, and fence (§7.2, §7.3). Role strings are never trusted.
- A requester cannot approve its own request. A relayer cannot approve for a project it leads. An executor run token cannot approve, relay, create tasks, or act outside its task.
- The text "the user approved" in a deliverable, prompt, or statement is never an approval (§7.4).
- Stale writes are rejected by fencing (§11.5).
- Run-session binding (§7.1, best effort): a process inside a launched run can use only that run's token. An executor that runs `mesh … --token-file <another agent's token>`, or tries passphrases, gets exit 4 and cannot lock the user out.

**Residual risk** (stated in `mesh help auth` and `docs/usage.md`). A same-uid process that ignores the rules (a compromised or prompt-injected executor with shell or file-edit access, or any other program the user runs) can:
- read any file the user can read, including other agents' Mesh token files (e.g. Worker 2's relayer token), and use them from outside a run session (after `setsid` plus a double fork, or with a cleared environment), which defeats run-session binding;
- read run tokens from other processes' environment or memory;
- with a stolen relayer token, forge **relayed** approvals; with a stolen lead token, act as the lead (assign, accept, complete);
- read or modify `mesh.db` directly, kill meshd, or send wrong passphrases from outside a run to trigger the lockout (denial of service);
- copy the scrypt verifier from `mesh.db` and guess the passphrase offline (scrypt makes each guess expensive; a long passphrase is the only defense);
- **modify Mesh's own code** or what it loads: `bin/mesh`, `mesh/*.py`, the Python interpreter or stdlib, or a `mesh` placed earlier on the user's `PATH`. The user's CLI, meshd, and `audit verify` all run that code, so a modified copy can capture the passphrase, compute valid user MACs for forged direct decisions, and make `audit verify` report a clean result. An executor at the default profile `edit` needs only its vendor's file-edit tool, not shell access, to do this if the install is writable and reachable. Mitigations 7 and 8 narrow the accidental and casual paths; they do not prevent a deliberate edit;
- **capture the passphrase or `K_user`** while a direct user request is in flight: read the memory of the `mesh --user` CLI or of meshd (`/proc/<pid>/mem` or ptrace; no yama restriction here, §1), or read what the user types into a wrapped or shadowed `mesh`. With either secret it can forge **direct** approvals and user events that carry valid MACs, without any offline guessing. Python cannot zero secrets reliably (§19 Q11).
Mesh cannot prevent these in v1. After either of the last two, neither the user MAC nor `audit verify` can be trusted.

**Mitigations in v1 (no root, no new users, no containers, no network):**
1. Run tokens are short-lived (run deadline + 300 s), scoped to one run, injected only into that run's environment, never written to disk, argv, prompts, or logs, and revoked when the run ends.
2. Long-lived agent tokens are shown once and stored by Mesh only as hashes. Mesh never writes them into workspaces, run dirs, prompts, or child environments (meshd strips every `MESH_*` variable before adding the run's own). The client refuses a `--token-file` whose realpath is under `workspaces.root`, `$MESH_HOME/runs`, or a registered project repo (exit 3 `TOKEN_FILE_INSECURE`). `mesh doctor` warns about token-like files found there. Docs tell users to keep token files outside every workspace (e.g. `~/.config/mesh/`, mode 0600). This keeps them out of the executor's working tree, not away from a deliberate same-uid reader.
3. The user-approval secret is separate from every agent credential. The passphrase is never stored, logged, or put in env or argv; only its scrypt verifier is stored; `K_user` exists only in memory during a user request. `mesh help auth` and `docs/usage.md` say: never keep the passphrase in a file (the checklist's passphrase file `P` is a test fixture only).
4. Categories that would weaken Mesh's own controls require the user's direct factor by default: `config` (fixed) and `permission_escalation` (§7.4). The user may set any other category to `direct_only`; this is recommended for `spend_money`, `deploy`, and `delete_external` (§19 Q3).
5. Relayed decisions are recorded and shown as `relayed (unverified)` with `trust:"relayed_unverified"`, the relayer, its credential id, and the verbatim statement. `approval check` and `approval consume` return `decided_via` and `trust`, so the requester sees the lower trust.
6. Tamper evidence for **DB-only** edits: direct decisions and user events carry the user MAC. `mesh audit verify --user` detects forged direct approvals and user events made by editing `mesh.db`, provided the forger has not captured the passphrase or `K_user` and has not modified Mesh's code (both are residual risks above). It also lists every relayed decision grouped by relayer for the user to review. The event hash chain detects accidental corruption, not deliberate rewrites.
7. Keep Mesh's own code out of executors' working trees (best effort; mirrors mitigation 2). meshd at start and `mesh doctor` compute the realpath of the install root (the directory that holds `bin/mesh` and the `mesh/` package). If it is equal to or inside `workspaces.root`, `$MESH_HOME/runs`, or a registered project repo, meshd refuses to start and `doctor` reports it: exit 2 `INSTALL_PATH_INSECURE` (details: `install_root`, `conflict`). `config set workspaces.root` and a repo intake (M2) that would create such an overlap are rejected with the same code. A consequence: Mesh cannot manage a project whose repo is its own install; use a separate checkout. `doctor` also warns when install files are writable by the uid. `docs/usage.md` recommends a read-only install (`chmod -R a-w <install root>`) and says that this stops only tools that respect file modes, because the same uid can `chmod` it back.
8. Mesh's own Python processes do not pick up code from executor-writable places by accident. `bin/mesh`, meshd, runwrap, and the fake agent run as `python3 -P -s -m …` with `PYTHONPATH` set to the install root's realpath. `-P` stops `-m` from putting the cwd (a workspace) first on `sys.path`, so a `mesh/` directory written into a workspace cannot shadow Mesh's modules; `-s` ignores user site-packages (`sitecustomize`, `usercustomize`). This closes accidental and casual paths only; it is no defense against a deliberate same-uid attacker.

**Opt-in future hardening (M5, not v1; needs a user decision and sudo):** run meshd under a dedicated uid with the DB, token files, and a root- or meshd-owned read-only install unreadable or unwritable by agents, and run executors under separate uids or containers. Only OS-level separation turns the mitigations above into a boundary. Even then, the account that types the passphrase into `mesh --user` must itself be trusted.

### 7.7 Mesh gates vs. each external CLI's own permissions
- **Mesh gates (enforced):** who may assign, accept, merge, complete, or change config and leads; approvals for the categories in §7.4; which model, effort, and profile an agent is launched with; concurrency; timeouts and kills; fenced writes.
- **External CLI permissions (vendor-enforced, not Mesh):** what a launched CLI may do inside its process (edit files, run shell commands, reach the network). Mesh only **chooses the flags** from the profile mapping (§14.2). It records them in `runs.argv_redacted_json`, and it refuses to launch when the adapter cannot express the requested profile (exit 13 `PROFILE_UNSUPPORTED`). It never silently widens permissions.
- **Vendor project-local config.** A vendor CLI may read configuration from its working directory (for example a `.vibe/`, `.kiro/`, `.factory/`, or agy settings file that an earlier executor wrote into the workspace), especially when the workdir is trusted (vibe `--trust`, §14.6). Such files can change the effective tool permissions. Mesh does not read or inspect them (§0 rule 4), so the profile flags are what Mesh asks for, not a guarantee of what the CLI allows.
- **Mesh does not sandbox external CLIs.** A CLI launched with edit or shell permission can write outside its workspace. The `cwd`/`--cwd`/`--workdir` settings are conventions, not jails. Vendor features such as `agy --sandbox` are vendor features; Mesh passes them when configured but makes no guarantee about them. The sentence "Mesh does not sandbox external CLIs" MUST appear in `mesh help auth` and in `docs/usage.md`.

---

## 8. Lead agent

- **Global default.** `mesh config set lead.default_agent '"<agent name>"' --user`. The agent must be enabled and `can_lead`.
- **Per-project choice at creation.** `--lead <agent>` on `intake submit` or `project create`. When an agent submits the intake, it passes the user's choice. This is recorded with `created_via=token` and `origin_principal_id`.
- **Resolution order:** `--lead`, then `lead.default_agent`, then exit 2 `NO_LEAD`.
- **Mid-project change.** `mesh project set-lead <prj> <agent> --reason R` (user; relayed or via approval per `lead_change`). In one tx:
  1. Close the current `lead_assignments` row (`ended_at`, `ended_reason`) and insert the new one.
  2. Mark the old lead's pending outbox messages for this project `superseded` and re-create them for the new lead.
  3. Cancel pending approvals requested by the old lead (the new lead may re-request).
  4. Emit `project.lead_changed`.
  Effects: an active lead turn of the old lead is terminated (§11.4), and its later actions get exit 9 `LEAD_SUPERSEDED`. The new lead gets a `project.handover` message.
  Guard: the new lead must not be the assignee of a non-terminal task in the project (exit 6, with the list of tasks). The user reassigns those first.
- **History is kept.** Tasks, events, deliverables, notes (`project note`), and all lead assignments stay. `mesh project leads <prj>` lists the full history. The handover message contains the goal, the criteria, a task table with states, the last 20 lead notes, open approvals, and the last 50 events.
- **Lead workspace.** `$MESH_HOME/workspaces/<prj>/lead/<agent_id>/` is separate per lead, so notes and files survive changes. For repo projects it contains `repo/`, a **detached** worktree at the integration branch head, refreshed at the start of each lead turn when it is clean. It is never an executor's worktree.
- **Lead work loop.** Lead-turn mechanics are in §12.4. Inbox-mode leads use the same commands through their own token.
- **Completion.** `project complete --report -` with JSON `{"criteria":[{"id":"crit_…","met":true,"evidence":"…"}],"summary":"…"}` (guards P4). The user is notified. The user does not re-confirm (CONFIRMED requirement). Mesh checks the report's **structure and coverage**, not its truth. Worker 2's final acceptance happens outside Mesh.

---

## 9. Model policy, concurrency, queueing

### 9.1 Models and effort
- Agent fields: `allowed_models` (a list of unique strings, each 1–256 chars, no whitespace or control chars) and `default_model` (which MUST be in `allowed_models`; otherwise exit 2 `DEFAULT_MODEL_NOT_ALLOWED`).
  - `delivery=launch` agents need a non-empty list (otherwise exit 2 `INVALID_ARGUMENT`, details: `param:"allowed_models"`).
  - `adapter=none` agents must have an empty list and no default.
- Only the user can change these. An update that removes the current default without setting a new one → exit 2.
- **Resolving a task's model** at assign, reassign, or retry: use `--model` if given, else the agent default. A model not in the allowed list → exit 2 `MODEL_NOT_ALLOWED` with `details.allowed`. **The lead can only choose within the list.**
- **Re-check at start (T5).** If the allow-list changed and no longer contains the task's model, the task goes `queued → failed(model_not_allowed)` and the lead is notified.
- **Effort** follows the same rules with `allowed_efforts` and `default_effort`. If the adapter has no effort flag (vibe; see §14), any effort gets exit 2 `EFFORT_UNSUPPORTED`. A user-set effort is never dropped silently: if an adapter cannot pass it for a given model (agy model ids that already encode effort, §14.4), the combination is rejected at validation.
- **Effort is not a permission.** Reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`, …) is the model's thinking level, chosen by the user per agent (default and allow-list) and passed only through the adapter's effort flag (droid `--reasoning-effort`, agy `--effort`, kiro `--effort`). It never changes which tools a CLI may use. Tool permissions come only from the permission profile (§9.4). The two are separate fields (`effort` vs `profile`) everywhere: DB, CLI flags, events, and `runs.argv_redacted_json`. Validation rejects a profile name given as an effort and vice versa (exit 2 `EFFORT_NOT_ALLOWED` / `INVALID_ARGUMENT`).
- **Vendor list cross-check.** `agent create/update` calls `adapter.list_models()` (read-only) when available. Unknown ids produce a **warning** in the envelope's `warnings`. With `--strict-models`, they are an error (exit 2). Vendor lists change, so warnings are the default. In testing mode (§17.1) the cross-check is skipped with the warning `models_not_cross_checked`, so the default suite never starts a vendor CLI (tripwire, §14.10).

### 9.2 Concurrency
- `max_concurrency` is required per agent. It applies to task runs (launch and inbox). Lead turns are **not** counted (one per project, §12.4), so a lead never deadlocks behind its own executor runs.
- Optional global `scheduler.max_total_runs` (null by default).
- **Validation (`policy.parse_positive_int`):**
  - CLI string: must match `^[1-9][0-9]*$` and be ≤ 9223372036854775807.
  - JSON input: must be a JSON integer (not a bool, float, or string) in 1..9223372036854775807.
  - Rejected examples: `0`, `-1`, `1.5`, `abc`, ``, `1e3`, `0x10`, `+3`, ` 2`, `9223372036854775808`, JSON `true`, JSON `"2"`, JSON `2.0`.
  - Error: exit 2 `INVALID_CONCURRENCY`, message `max_concurrency must be a positive integer (1..9223372036854775807); got '<value>'`.
- **Lowering the limit** below the current running count does not kill anything. New starts wait until the count drops.

### 9.3 Queueing
- Each scheduler pass promotes `assigned`/`rework` tasks to `queued` (T4/T13). Then, per agent, it starts queued tasks ordered by `priority DESC, queued_at ASC, id ASC` while `running < max_concurrency` (and the global cap allows).
- `task list --status queued` includes `queue_position` (per agent) and `blocked_reason` (`concurrency_limit` | `waiting_for_start` (inbox) | `project_paused`).
- For inbox mode, `task start` when the agent is at its limit → exit 11 `CONCURRENCY_LIMIT`, and the task stays queued.

### 9.4 Permission profiles
Ranks: `readonly(0) < edit(1) < dev(2) < elevated(3) < unrestricted(4)`. (The r1 name `high` was renamed `elevated` so it cannot be confused with reasoning effort `high`, §9.1.)
- A profile is a Mesh name for the CLI tool/autonomy permissions Mesh asks a vendor CLI for. It is independent of model and effort.
- Only `unrestricted` may emit a vendor's trust-all flag (droid `--skip-permissions-unsafe`, agy `--dangerously-skip-permissions`, kiro `--trust-all-tools`, vibe `--auto-approve`). No other profile maps to trust-all, silently or otherwise. If an adapter cannot express a profile with flags that are narrower than the next profile up, that profile is `unsupported` for that adapter (exit 13 `PROFILE_UNSUPPORTED`).
- The task profile defaults to the agent's `default_profile`.
- A profile above `max_unapproved_profile` requires `permission_escalation` approval.
- Per-adapter flag mapping is in §14.2. An unmappable profile gets exit 13 `PROFILE_UNSUPPORTED`. Mesh never substitutes a broader profile.

---

## 10. Workspaces and merging

### 10.1 Layout
```
$MESH_HOME/workspaces/<prj>/
  integration/                 # repo projects only: worktree on branch mesh/<prj>/integration (Mesh-managed)
  lead/<agent_id>/             # lead's own dir (+ repo/ detached worktree for repo projects)
  tasks/<tsk>/                 # executor dir: plain dir (general) or git worktree (code)
```
- The workspace root can be changed with the setting `workspaces.root`.
- A task dir is created lazily at the first T5 and recorded in `tasks.workspace_path`.
- Rework and reassign reuse the same dir and branch, except after an inbox `lease_expired` (§6.2, "Fresh workspace after an inbox lease expiry": `tasks/<tsk>.a<attempt>/`).

### 10.2 Repo projects (M2)
`intake submit --repo PATH --base-branch B --test-command '<json argv>'|none` performs:
1. Validate: `git -C PATH rev-parse --show-toplevel` equals the realpath of PATH, and `git -C PATH rev-parse --verify B^{commit}` succeeds.
2. `test_command` is required for repo projects. The literal `none` means "this project has no tests". That choice is the user's, made at creation, and recorded as `tests: "none"`. The lead cannot change it without `scope_change` approval.
3. `git -C PATH branch mesh/<prj>/integration <B-commit>` (if the branch exists → exit 6).
4. `git -C PATH worktree add <ws>/integration mesh/<prj>/integration`.
5. Store `base_commit`.

### 10.3 Code task worktree
At the first start: `git -C <repo> worktree add -b mesh/<prj>/<tsk> <ws>/tasks/<tsk> mesh/<prj>/integration`, and record `base_commit` and `branch`. The executor prompt tells the agent to commit on this branch and not to push or switch branches.

### 10.4 On submission (code tasks)
Mesh records the following in the deliverable as `git`:
- `git -C wt rev-parse HEAD`
- `git -C wt status --porcelain=v1` (as `dirty: bool`, `dirty_files`)
- `git -C wt diff --stat <base_commit>..HEAD`
- the commit count

Mesh does **not** auto-commit. A dirty tree is reported to the lead, and merge refuses it.

### 10.5 Merge pipeline (`task merge`, lead; serialized per project by lease `merge:<prj>`)
1. Guards: task `accepted`, kind `code`, no task lease. The task worktree must be clean (else T16 `worktree_dirty`, exit 14 `WORKTREE_DIRTY`). The integration worktree must be clean (else exit 14 `INTEGRATION_DIRTY`). Record `pre_head = git -C integ rev-parse HEAD`.
2. `git -C integ merge --no-ff --no-commit mesh/<prj>/<tsk>`.
   - If it reports "Already up to date": finish as `merged` with `noop:true`.
   - Non-zero exit → collect `git -C integ diff --name-only --diff-filter=U`, run `git -C integ merge --abort`, verify HEAD == `pre_head` and the tree is clean, then T16 `conflict` (exit 14 `MERGE_CONFLICT`, `details.files`).
3. Tests: if `test_command` ≠ `"none"`, run it in `integ` as a supervised process (own process group, `test_timeout_s`, output redacted to `merges/<mrg>/test.log`). Non-zero exit or timeout → `git merge --abort`, verify `pre_head`, then T16 `tests_failed` (exit 14 `TESTS_FAILED`, `details.log_path`, last 50 lines).
4. `git -C integ commit --no-edit -m "Merge mesh task <tsk>: <title>"`. Failure (e.g. **no git identity configured**) → abort, then T16 `commit_failed`, with the git stderr in `details`.
5. T15 → `merged`. Record `merge_commit`, `pre_head`, `tests: passed|none`, and the test log path.

Never used: `-X ours/theirs`, `--strategy-option`, `reset --hard` on non-Mesh branches, `checkout -f`, `--force`, `push`, `rebase`.
`gitops.py` allow-lists these subcommands: `rev-parse, status, diff, log, branch (create only), worktree (add/list/remove/prune), merge (--no-ff --no-commit | --abort | --ff-only), commit, update-ref (deliver only), merge-base, show-ref, cat-file`. Any other subcommand raises `AssertionError`.

### 10.6 Deliver to base (`project deliver`, lead, category `deliver_to_base`; M2)
1. Require `git merge-base --is-ancestor <base> <integration>`. Otherwise exit 14 `NOT_FAST_FORWARD`. The lead then creates a task to merge base into integration. Mesh never rebases or forces.
2. If the base branch is checked out in some worktree W (`git worktree list --porcelain`): W must be clean, then run `git -C W merge --ff-only mesh/<prj>/integration`.
3. Otherwise: `git update-ref refs/heads/<base> <new> <old>` (compare-and-swap).

Mesh never sets git identity. Merge commits use the repo's configured identity. This box has no global identity (§1), so the user must configure a repo-local one or merges fail clearly with `commit_failed`.

### 10.7 Cleanup
- `workspace prune` removes worktrees only for tasks that are `merged`, or `cancelled` with no commits beyond `base_commit` (`git worktree remove`, then `git branch -d`, never `-D`). It supports `--dry-run`.
- Removing unmerged work requires `workspace delete <tsk>`, gated by `delete`.
- Nothing is ever deleted automatically.

---

## 11. Process supervision

### 11.1 Spawn sequence (launch mode, T5)
1. **TX A:** acquire the lease `task:<id>` (PK insert), `fence_seq+1`, `attempt+1`, insert the run (`starting`, `deadline_at = now + timeout_s`), issue the run token, set task `running`, write the events. COMMIT.
2. **Effect:** create `runs/<run>/` (0700). Write `prompt.md` and `spec.json`. `spec.json` contains `argv`, `cwd`, `stdin` mode, idle settings, and the names of env vars to redact. It contains **no env values and no secrets**. Then spawn:
   `python3 -P -s -m mesh.runwrap <run_dir>` (with `PYTHONPATH` = the install root's realpath; §7.6 mitigation 8) with `start_new_session=True`, `stdin/stdout/stderr = DEVNULL`, `cwd=<workspace>`, and env = (meshd env minus every `MESH_*` var) + `MESH_TOKEN`, `MESH_RUN_ID`, `MESH_FENCE`, `MESH_TASK_ID`, `MESH_PROJECT_ID`, `MESH_SOCKET`, with `PATH` prefixed by `<repo>/bin` so the agent can call `mesh`.
3. **TX B:** run → `running`, with `wrapper_pid`, `pgid` (= wrapper pid), and `proc_starttime` (field 22 of `/proc/<pid>/stat`). COMMIT.
   **Parsing `/proc/<pid>/stat`:** `comm` (field 2) may contain spaces and parentheses, so split only the text after the **last** `)` on whitespace. That list starts at field 3, so field N is at index N−3: state = index 0, PPid = 1, pgrp = 2, session = 3, starttime = 19. All /proc readers in Mesh use this one helper (`test_proc_identity` covers a `comm` such as `a) (b`).
4. A spawn exception → run `spawn_failed`, task `failed(spawn_failed)`, lease released, lead notified.

### 11.2 runwrap (`mesh/runwrap.py`)
1. Immediately write `wrapper.json` = `{pid, starttime}` atomically (tmp file + fsync + rename).
2. Start the vendor CLI as its child in the **same process group** (no new session). Its stdout and stderr are pipes. Its stdin is DEVNULL or the prompt file, per the adapter.
3. Read both pipes. Redact them line by line (§15.2) using the exact values of `MESH_TOKEN` and of env vars whose **names** match `(?i)(key|token|secret|passw|cookie|credential|auth)`, plus the regex patterns. Write `stdout.log` and `stderr.log` (0600). Each stream is capped at 50 MiB, after which the wrapper writes a marker and keeps draining.
4. Touch `activity` (mtime) at most once per second whenever bytes arrive.
5. On child exit, write `exit.json` = `{exit_code, signal, ended_at}` atomically, then exit.
6. On SIGTERM: forward it to the child (it is in the same group anyway), keep draining, and still write `exit.json`.
7. Because the logs go to files written by the wrapper (not pipes to meshd), **a meshd crash does not kill or block the run.**

### 11.3 Watching (supervisor, per run, every 1 s)
- `exit.json` present → read it and reap the wrapper if it is meshd's child. **Leftovers:** look for processes still in the run's group or session, or carrying this run's `MESH_RUN_ID` (the §11.4 target and escapee sets). If any exist (e.g. a `setsid` grandchild), terminate them through §11.4 at once and emit `run.leftovers_terminated` (pids). The run row stays `running` until the §11.4 release condition holds; then, in one tx, the run is closed (`exited`, exit code from `exit.json`) and the lease is released. If they cannot be killed, §11.4's `run.kill_failed` path applies and the lease is kept. Then call the adapter's `parse_output` and decide:
  - exit 0 with no prior submit → T7 auto-submit (`submitted_by: system`, deliverable from the parsed output). The task may be `submitted` while leftovers are still being terminated; it keeps the lease until then (allowed by I1).
  - non-zero → T8 `exit_nonzero` (`details.exit_code`, last 50 stderr lines).
  - if the task was already `submitted` → only the run is closed (`run.exited_after_submit`).
- Wrapper dead (pid gone, or `starttime` mismatch) with no `exit.json` → `lost` → T8 `lost`.
- `now > deadline_at` → terminate (§11.4), then T8 `timeout`.
- `idle_timeout_s > 0`, the adapter declares `liveness: stream`, and the `activity` mtime is older than `idle_timeout_s` → terminate, then T8 `idle_timeout`. Idle timeout defaults to 0 (off) until smoke tests prove that the adapter streams output during long tool calls (§14).
- After a fenced `task submit`, the process gets `kill.post_submit_grace_s` (default 60 s, §5.7) to exit, then it is terminated with `end_reason=post_submit_grace`.
- The lease is renewed each tick while the wrapper is alive.

### 11.4 Termination (timeout, cancel, post-submit grace, project cancel, lead superseded)
```
targets  = {p in /proc : pgid(p) == run.pgid or sid(p) == run.pgid}    # sid == wrapper pid (session leader)
escapees = {p in /proc : p not in child_registry, p != meshd,
            /proc/p/environ contains MESH_RUN_ID=<this run id>}         # setsid escapees, wherever they reparented
SIGTERM targets ∪ escapees; poll every 200 ms up to kill.grace_s (default 15 s)   # vibe: SIGTERM writes export and exits
SIGKILL remaining; poll up to 10 s
zombies count as gone
if any still alive: emit run.kill_failed, keep the lease, retry every 30 s, show in `mesh status`
else: run → killed(end_reason), release the lease, revoke the run token, then the task transition
```
- **No-duplicate guarantee (release condition).** On every path that releases a launch-mode lease (T7 after a submit or auto-submit, T8 with any reason, T10, the §11.3 exit handling, and recovery §11.7), the lease is released, and the task becomes eligible again, only after the process group, the session, and every process carrying this run's `MESH_RUN_ID` are verified gone. A normal exit 0 therefore does not release the lease while a `setsid` escapee still runs.
- **Child registry.** meshd records every child it spawns (wrappers, git, test commands, probes, `list_models` and session-list calls) in an in-memory registry `{pid: (kind, starttime)}`. A termination never signals a registered pid that belongs to another purpose, and never signals a process whose environ lacks this run's `MESH_RUN_ID` unless it is in the run's group or session. Concurrent git, test, or probe children are therefore never killed by an unrelated cancel. (The old r1 rule "SIGKILL any process whose PPid == meshd pid" is removed.)
- **Reaping.** Because meshd is a subreaper, orphans reparented to it become zombies when they die. Every 5 s, and after each termination, meshd scans `/proc/*/stat` for state `Z` with `PPid == getpid()` and a pid not in the registry, and calls `os.waitpid(pid, os.WNOHANG)` for each. meshd MUST NOT call `waitpid(-1)` or `os.wait()`, because that steals exit statuses from asyncio's child watcher (asyncio would then report returncode 255). Registered children are reaped only by their owner (asyncio `Process.wait()`, or the supervisor's `waitpid(wrapper_pid)`).
  - **Spawn window.** `asyncio.create_subprocess_exec` forks and then awaits, so a new child can exist (and even exit) before its pid is in the registry. Every spawn of a meshd-owned child (asyncio or `subprocess.Popen`) increments a counter `spawns_in_flight` before the call and decrements it in a `finally` after the pid is registered or the spawn failed. The reaper skips its whole scan while `spawns_in_flight > 0` and tries again on the next 5 s tick or after the next termination. Unit test `test_reaper_inflight_spawn`.
- **Residual risk.** A descendant that calls `setsid` **and** clears `MESH_RUN_ID` from its environment cannot be found. For an **adopted** run (§11.7), the new meshd is not an ancestor, so its escapees reparent to init for the rest of the run's life, not only while meshd is down; the environ scan above still finds them unless they cleared the variable. Recorded in §19 Q9.

### 11.5 Leases, fencing, heartbeat
- The fence is `tasks.fence_seq`, incremented on every T5. Every executor write carries the fence: in the run token scope (launch mode) or as `--run --fence` (inbox mode).
- Stale-write codes (one per case, checked in this order, no state change): the task has no lease → exit 9 `LEASE_EXPIRED`; the task has a lease but the presented fence (or run id) differs → exit 9 `FENCE_MISMATCH`.
- After a timeout or cancel, a late `task submit` from the old run is therefore rejected.
- **Inbox mode.** `task start` returns `{run_id, fence, lease_ttl_s, workspace}`. The agent must call `task heartbeat` (or `progress`) at least every `lease.ttl_s` (default 120 s; ttl/3 is recommended). On expiry → T8 `lease_expired`.
  Honest limit: Mesh cannot stop an inbox agent's process. After expiry its writes are rejected, but its file edits cannot be prevented. Inbox mode is therefore for cooperative, already-running agents.

### 11.6 Failure policy (no infinite retry)
- Mesh **never** moves `failed → queued` on its own. Only `task retry` / `reassign` by the lead or the user can, and both are capped by `max_attempts` (default `task.default_max_attempts` = 3; settable per task up to `task.max_attempts_cap` = 10). Going beyond the cap requires `retry_over_limit` user approval.
- Every failure emits `task.failed` with `reason`, `attempt`, `max_attempts`, and the stderr tail. The lead is woken and decides **retry / reassign / escalate / cancel**.
- Lead wake retries are bounded (3; §12.4). After that, `lead.unreachable` goes to the user.
- Kill retries (§11.4) are not work retries. They are surfaced to the user.

### 11.7 Crash recovery (meshd boot)
Steps run in order. Each step is its own tx.
1. flock, `quick_check`, insert the boot row. Mark earlier boot rows with `stopped_at IS NULL` as `crashed=1`.
2. For each run in (`starting`,`running`) whose lease has `holder_kind='meshd'` (join `leases` on `leases.run_id = runs.id`):
   - `wrapper.json` exists, pid alive, `starttime` matches → **adopt**: set `leases.holder` = the current boot id, resume watching, with the deadline unchanged (if it has passed → terminate as `timeout`). Event `run.adopted` (payload: old and new boot id).
   - `exit.json` exists → process the exit normally (§11.3).
   - `wrapper.json` exists but the process is dead or its starttime differs (pid reused; do **not** signal that pid) → `lost` → T8 `lost_on_restart`.
   - No `wrapper.json` (crash before or during spawn): first scan `/proc/*/environ` for `MESH_RUN_ID=<run>`. If a live process carries it (meshd died after `Popen` and before runwrap wrote `wrapper.json`), wait up to 5 s for `wrapper.json`; if it appears, adopt as above; otherwise terminate those processes (§11.4) and wait until they are gone. Only then → `spawn_failed` → T8 `lost_before_spawn`. **No automatic respawn.**
3. Agent-held leases: `expires_at = max(expires_at, now + lease.ttl_s)` (a grace period, because heartbeats failed while meshd was down).
4. Outbox `delivering` rows whose lead-turn run is not adopted → `pending` (`attempts += 1`).
5. Tasks in `merging`: if `MERGE_HEAD` exists in the integration worktree → `git merge --abort` → T16 `interrupted`. If HEAD ≠ `pre_head` and HEAD is a merge commit whose second parent equals the task branch tip and whose message contains the task id → reconcile to `merged`. Otherwise → T16 `interrupted`.
6. Tasks in `cancelling` with no live run → `cancelled`.
7. Expire approvals that are past due. Emit `daemon.recovered` with counts.

All transitions above emit outbox messages with deterministic dedup keys, so a crash during recovery followed by another recovery does not duplicate notifications.

---

## 12. Notifications and work delivery

### 12.1 Event log
The `events` table is append-only (enforced by triggers) and hash-chained. `mesh event list` filters by project, task, type, and `--since SEQ`. `mesh event tail` prints NDJSON live, using the same long-poll mechanism as the inbox.

### 12.2 Outbox fan-out (in the same tx as the event)
| Event | Recipients (channel) | dedup_key |
|---|---|---|
| project.created, project.request_received | lead (lead_wake or inbox) | `<type>:<prj>:<evt>:<lead_asg>` |
| project.handover | new lead | `handover:<lead_asg>` |
| task.offered | inbox assignee | `offered:<tsk>:<fence_next>` |
| task.submitted / task.failed | lead | `<type>:<tsk>:<fence>:<lead_asg>` |
| task.blocked, merge.failed | lead | `<type>:<tsk>:<evt>:<lead_asg>` |
| approval.requested | user (inbox) + origin agent (inbox) | `apr_req:<apr>:<recipient>` |
| approval.decided / approval.expired | requester | `apr_dec:<apr>:<status>` |
| task.escalated, project.completed, lead.unreachable, run.kill_failed | user + origin agent | `<type>:<subject>:<recipient>` |
| project.lead_changed | old lead (inbox, informational) | `lead_changed:<lead_asg>:<old>` |
| security.credential_in_run (§7.1) | user + project lead | `cred_in_run:<run>:<recipient>` |

- Insertion uses `INSERT … ON CONFLICT(dedup_key) DO NOTHING`. A replayed request or a re-run recovery step therefore never creates a second message.
- `inbox ack` is idempotent (acking an already-acked message returns exit 0).
- The "origin agent" is `projects.origin_principal_id`, usually Worker 2. Messages to it marked `for_user:true` are how the user hears about completion, escalations, and approval requests through their chat agent.

### 12.3 How each kind of agent actually receives work (no magic push)
| Agent kind | Mechanism | What wakes it | Model-quota cost while idle |
|---|---|---|---|
| Executor, `delivery=launch` (droid/agy/kiro/vibe headless) | Mesh starts a **new headless process** per attempt. For rework or retry, it **resumes the vendor session** (`droid exec -s`, `agy --conversation`, `kiro-cli chat --resume-id`, `vibe --resume`) **only if** that adapter's resume capability is smoke-verified and a session id was captured. Otherwise it starts a fresh process with the prior deliverable and the feedback in the prompt. | The scheduler (T5) | none |
| Lead, `delivery=launch` | **Lead turn**: a new headless run in the lead dir (resuming the lead session when verified). It is triggered by pending lead messages after a debounce (§12.4). | The scheduler | one run per batch |
| Already-running agent, `delivery=inbox` (Worker 2, an interactive Kiro or droid session the user keeps open, any agent that can run shell commands) | The agent must call **`mesh inbox wait --timeout N`**. This blocks on the Unix socket with no model tokens and returns as soon as a message exists, or with exit 8 on timeout. Alternatively it calls `mesh inbox list` on its own schedule. It then acks with `mesh inbox ack <msg>`. | Only its own next `inbox wait/list` call | none while blocked; whether a vendor harness allows a long blocking tool call is vendor-specific (UNVERIFIED) |
| User | User inbox (`mesh inbox list --user`), plus copies to the origin agent's inbox for relaying. An optional `notify.command` hook (M4) runs a user-supplied argv with the message JSON on stdin. | the user or the origin agent | none |

**What is not possible in v1:** Mesh cannot inject text into a running interactive TUI session. Long-lived bidirectional sessions over ACP or JSON-RPC (`agy acp`, `kiro-cli serve`, `vibe-acp`, `droid exec --input-format stream-jsonrpc`) would allow real push. They are research for M5, not v1.

### 12.4 Lead turns (M2)
- **Trigger.** A lead with `delivery=launch` has pending `lead_wake` messages. The oldest must be ≥ `lead.wake_debounce_s` old (5 s), or ≥ 20 messages must be pending. The lease `leadturn:<prj>` must be free (one turn per project at a time).
- **TX.** Mark up to 50 messages `delivering` (`delivering_run_id`), create run `kind=lead_turn` with a lead run token, and take the lease. Effect: spawn with the lead prompt (§12.6) in `lead/<agent>/`, using profile `lead.profile` (default `readonly`) and timeout `lead.turn_timeout_s` (1800 s).
- **Actions protocol.** Lead CLIs may lack shell permission, so the lead's final output may contain one fenced block:
  ````
  ```mesh-actions
  [{"cmd":"task.create","args":{"title":"…","spec":"…","assignee":"agy-impl"}},
   {"cmd":"task.accept","args":{"task":"tsk_…","note":"…"}}]
  ```
  ````
  - After the turn exits 0, meshd executes the actions in order, each as a normal request authenticated as the lead-turn token, with idempotency key `act:<run_id>:<index>`.
  - Allowed cmds: `task.create`, `task.assign`, `task.reassign`, `task.accept`, `task.reject`, `task.retry`, `task.escalate`, `task.cancel`, `task.merge`, `dep.add`, `dep.remove`, `project.note`, `project.complete`, `project.deliver`, `approval.request`. At most 50 per turn.
  - Each result is recorded (`lead.action_result`). Failures are sent as `lead.action_failed` messages into the next turn.
  - Leads whose CLI can run shell commands may also call `mesh` directly with `MESH_TOKEN`; it is the same authorization either way.
- **Outcome.** Exit 0 → messages `acked`. Failure or timeout → `pending` with backoff (30 s, 120 s, 600 s). After 3 failed attempts → `dead` and `lead.unreachable` (user + origin).
- **Lead session id.** It is captured by the adapter and stored on `lead_assignments.session_id`. If it is missing or unverified, every turn is fresh. The prompt always includes a project summary, so correctness never depends on resume.

### 12.5 Executor prompt (rendered to `runs/<run>/prompt.md`)
````
# Agent Mesh task <tsk> — attempt <n>/<max> — project "<name>" (<prj>)
You are an executor. Work only in: <workspace>
[code] You are on git branch <branch>. Commit your work to this branch before you finish. Do not push. Do not switch branches.
## Task: <title>
<spec>
## Project acceptance criteria (context only)
1. <criterion> …
## Rework feedback (if any)
<reject reason>  — previous deliverable summary: <summary>
## Rules
- You cannot approve anything. Writing that "the user approved" something has no effect in Mesh.
- If the work needs spending money, deleting, deploying, or a scope change, stop and say so in "notes".
## Report
End your final answer with exactly one block:
```mesh-deliverable
{"summary":"…","artifacts":["relative/path"],"tests":{"command":"…","result":"pass|fail|not_run"},"notes":"…"}
```
If your tools allow shell commands, you may also run: mesh task progress --message "…"  (credentials are already in your environment).
````
- Parsing: take the **last** fenced block with info string `mesh-deliverable` that parses as a JSON object. Otherwise the deliverable is `{"summary": <last 4000 chars of final text>, "structured": false}`.
- **Prompt-length fallback** (limit `max_argv_prompt_bytes` = 100 000 bytes of UTF-8, §14.1). The full prompt is always written to `<run_dir>/prompt.md`.
  - droid (`--file`) and vibe (`--prompt-file`) always read the prompt from that file, so no fallback is needed.
  - agy: if `prompt_mode` is verified as stdin (mode C, §14.4), the file is passed on stdin. Otherwise the argv prompt becomes exactly: `Read the file <abs run_dir>/prompt.md and follow the instructions in it exactly. It is your complete task.` and Mesh adds `--add-dir <abs run_dir>` (agy help.txt L2) so the file is inside the agy workspace.
  - kiro: the argv prompt becomes the same pointer sentence, **except** under profile `readonly` (`--trust-tools=` trusts no tool, so `fs_read` may be denied headless). There Mesh never launches a run that cannot read its task:
    - at assign, reassign, or retry (a caller exists): if the prompt rendered from the spec alone is already over the limit → exit 2 `PROMPT_TOO_LONG` (details: `bytes`, `limit`, `adapter`, `profile`);
    - at T5 (scheduler, no caller): if the full prompt, including rework feedback and the previous deliverable summary, is over the limit → T6 `queued → failed(prompt_too_long)` with the same details, and the lead is notified.
  - fake: always argv path (test double).
  Unit test `test_prompt_fallback` pins these cases.

### 12.6 Lead prompt
The lead prompt contains:
- the role and project summary (goal, criteria with ids, task table, open approvals, last notes);
- the pending messages as JSON;
- the authority rules: which categories need user approval, that only allowed models may be chosen, and that the lead cannot be an assignee;
- the `mesh-actions` schema and allowed cmds;
- the `project complete` report format.

**Lead prompt size.** The lead prompt is written to `runs/<run>/prompt.md` and passed with the same per-adapter rules as §12.5. In addition, the renderer caps it at `max_argv_prompt_bytes`: it drops, in this order, the oldest events, then the oldest notes, then terminal tasks from the task table (replaced by per-status counts), then takes fewer pending messages (the rest stay `pending` for the next turn; at least one per turn, its body cut to 4000 chars with a pointer to `mesh inbox list` if needed). The prompt states what was omitted. So the kiro `readonly` refusal never applies to a lead turn. Unit test `test_lead_prompt_cap` (M2).

---

## 13. CLI

### 13.1 Conventions
- **Shape:** `mesh <resource> <verb> [positional] [flags]`. Global flags are accepted anywhere: every subparser inherits a parent parser.
- **Global flags:**
  - `--home DIR`
  - `--format json|text` (default **json**)
  - `--user` and `--passphrase-fd N`
  - `--token-file PATH`
  - `--input FILE|-`
  - `--idempotency-key K` (1–128 chars of `[A-Za-z0-9._:-]`)
  - `--dry-run`
  - `--relayed --user-statement TEXT [--evidence-ref REF]`
  - `--approval APR`
  - `--if-version N` (optimistic concurrency; mismatch → exit 6 `VERSION_MISMATCH`)
  - `--timeout DUR` (client response timeout, default 30 s; `*.wait` commands use their own, and on `task create` / `task retry` it is the run timeout of §5.7, so those verbs use the default client timeout)
  - `-q`
- **JSON envelope.** stdout carries exactly one JSON document (except `event tail`, which is NDJSON):
  ```json
  {"ok":true,"data":{…},"warnings":[],"request_id":"req_…","idempotent_replay":false,"dry_run":false}
  {"ok":false,"error":{"code":"MODEL_NOT_ALLOWED","message":"…","details":{…}},"exit_code":2,"request_id":"req_…"}
  ```
  Diagnostics go to stderr, prefixed `mesh: `. List results are `{"items":[…],"next_cursor":null}`. Text format renders tables but keeps the same exit codes.
- **stdin.** `--input -` reads a JSON object whose keys are the long flag names with `_` (e.g. `{"title":"…","spec":"…","depends_on":["tsk_…"]}`).
  - A key given both as a flag and in the JSON → exit 2 `DUPLICATE_PARAM`.
  - An unknown key → exit 2 `UNKNOWN_PARAM`.
  - Large text fields also accept `-` (`--spec -`, `--report -`) when `--input` is not used.
- **Help, three layers, each with an `Examples:` section:**
  - `mesh --help`: resources, the auth summary, and 3 end-to-end examples.
  - `mesh task --help`: verbs, with one line each and examples.
  - `mesh task create --help`: every flag, its JSON key, the possible exit codes, and examples.
  - Topics: `mesh help auth|approvals|exit-codes|json|workflow|adapters|env|testing`.
  - `mesh help exit-codes` prints the §13.2 table verbatim.
- **Safe defaults.** Default profile `edit`, and `max_unapproved_profile` `dev`. No destructive command runs without its gate. Mutating commands print what they did, and `--dry-run` prints what they would do.

### 13.2 Exit codes
| Code | Name | Typical `error.code`s |
|---|---|---|
| 0 | OK | — |
| 1 | INTERNAL | `INTERNAL_ERROR`, `SCHEMA_TOO_NEW`, `DAEMON_ALREADY_RUNNING`, `DB_INTEGRITY_FAILED` |
| 2 | USAGE / VALIDATION | `INVALID_ARGUMENT`, `UNKNOWN_PARAM`, `DUPLICATE_PARAM`, `INVALID_JSON`, `INVALID_SETTING`, `PROMPT_TOO_LONG`, `NOT_IMPLEMENTED_IN_M1` (details: `milestone`), `INVALID_CONCURRENCY`, `MODEL_NOT_ALLOWED`, `DEFAULT_MODEL_NOT_ALLOWED`, `EFFORT_UNSUPPORTED`, `EFFORT_NOT_ALLOWED`, `NO_LEAD`, `LEAD_NOT_ELIGIBLE`, `DEP_CYCLE`, `LEAD_CANNOT_BE_ASSIGNEE`, `CRITERIA_REPORT_INCOMPLETE`, `PASSPHRASE_SOURCE_REJECTED`, `INSECURE_PERMISSIONS`, `INSTALL_PATH_INSECURE`, `SOCKET_PATH_TOO_LONG` |
| 3 | UNAUTHENTICATED | `TOKEN_MISSING`, `TOKEN_INVALID`, `TOKEN_EXPIRED`, `TOKEN_REVOKED`, `PASSPHRASE_INVALID`, `LOCKED_OUT`, `NO_TTY`, `TOKEN_FILE_INSECURE` |
| 4 | FORBIDDEN | `CAPABILITY_MISSING`, `NOT_PROJECT_LEAD`, `NOT_ASSIGNEE`, `SELF_APPROVAL`, `RELAY_NOT_ALLOWED`, `SCOPE_MISMATCH`, `NOT_REQUESTER`, `CREDENTIAL_NOT_ALLOWED_IN_RUN` |
| 5 | NOT_FOUND | `NOT_FOUND` |
| 6 | CONFLICT | `INVALID_TRANSITION`, `VERSION_MISMATCH`, `IDEMPOTENCY_KEY_REUSED`, `ALREADY_EXISTS`, `RUN_STILL_ACTIVE`, `PROJECT_NOT_ACTIVE`, `DECISION_CONFLICT`, `LEAD_HAS_TASKS`, `COMPLETION_BLOCKED` |
| 7 | APPROVAL_REQUIRED | `APPROVAL_REQUIRED` (details: `approval_id`, `op_hash`, `category`, `expires_at`; also returned when a referenced approval is still pending) |
| 8 | WAIT_TIMEOUT | `WAIT_TIMEOUT` (data still contains `items: []`) |
| 9 | STALE | `FENCE_MISMATCH`, `LEASE_EXPIRED`, `LEAD_SUPERSEDED` |
| 10 | DAEMON_UNAVAILABLE | `DAEMON_UNAVAILABLE` (hint: `mesh daemon start`) |
| 11 | BUSY | `CONCURRENCY_LIMIT` |
| 12 | APPROVAL_INVALID | `APPROVAL_EXPIRED`, `APPROVAL_REJECTED`, `APPROVAL_CONSUMED`, `APPROVAL_CANCELLED`, `OP_HASH_MISMATCH`, `CHANNEL_NOT_ALLOWED`, `AUDIT_MISMATCH` |
| 13 | ADAPTER_UNAVAILABLE | `ADAPTER_NOT_INSTALLED`, `ADAPTER_UNSUPPORTED`, `PROFILE_UNSUPPORTED`, `TEST_DOUBLE_DISABLED` |
| 14 | MERGE_FAILED | `MERGE_CONFLICT`, `TESTS_FAILED`, `WORKTREE_DIRTY`, `INTEGRATION_DIRTY`, `NOT_FAST_FORWARD`, `GIT_COMMIT_FAILED`, `MERGE_HELD` |

argparse usage errors MUST be converted to the JSON envelope with exit 2 (override `ArgumentParser.error`).

- Every `error.code` used anywhere in this plan is listed above; a code missing from the table is a plan bug, and `help exit-codes` prints exactly this table. `error.code` is always UPPER_SNAKE.
- Lowercase words (`timeout`, `lease_expired`, `commit_failed`, …) are **reasons** stored in `failure_json`, `merge_json`, and events, never in `error.code`. T16 reasons map to codes as: `conflict` → `MERGE_CONFLICT`, `tests_failed` → `TESTS_FAILED`, `worktree_dirty` → `WORKTREE_DIRTY`, `integration_dirty` → `INTEGRATION_DIRTY`, `commit_failed` → `GIT_COMMIT_FAILED`. `interrupted` is set only by recovery, which has no caller, so it has no code.

### 13.3 Command tree
| Resource | Verbs (key flags) | Auth |
|---|---|---|
| (top) | `init [--passphrase-fd N]`, `status`, `whoami`, `version`, `help [topic]` | init: none |
| daemon | `start` (detached, waits ≤ 5 s for the socket), `run` (foreground), `stop`, `status` | stop: user |
| config | `list`, `get KEY`, `set KEY JSON` (keys, types, defaults: §5.7) | set: user |
| agent | `create --name --adapter [--delivery launch\|inbox] [--default-model] [--allowed-model]… [--default-effort] [--allowed-effort]… --max-concurrency N [--can-lead] [--capability intake\|relay]… [--default-profile] [--max-unapproved-profile] [--binary PATH] [--option KEY=JSON]… [--strict-models]`, `update NAME [same; list flags replace]`, `list`, `show NAME`, `disable NAME`, `enable NAME`; `agent token issue NAME [--expires-in]`, `agent token revoke CRED`, `agent token list NAME` | user |
| adapter | `list`, `show ID`, `probe [ID]`, `models ID` | any |
| intake | `submit --title --goal --criterion…  [--lead] [--repo --base-branch --test-command JSON\|none] [--depends-on-project PRJ]… [--origin-ref]`; `submit --project PRJ --message TEXT` | intake cap / user |
| project | `create` (= intake new), `list [--status]`, `show PRJ [--history]`, `leads PRJ`, `update PRJ [--goal] [--criterion]…`, `pause`, `resume`, `cancel [--cancel-running]`, `set-lead PRJ AGENT --reason`, `complete PRJ --report FILE\|-`, `note PRJ --text`, `deliver PRJ`, `wait PRJ --until completed\|terminal --timeout` | §7.3 |
| task | `create --project --title --spec TEXT\|- [--kind] [--assignee] [--model] [--effort] [--profile] [--timeout] [--idle-timeout] [--max-attempts] [--priority] [--depends-on TSK]…`, `list [--project] [--status] [--assignee]`, `show TSK`, `log TSK [--run] [--stream stdout\|stderr] [--tail N]`, `assign TSK --to`, `reassign TSK --to --reason [--reuse-workspace]`, `start TSK`, `heartbeat TSK [--run --fence]`, `progress TSK --message [--run --fence]`, `submit TSK [--deliverable FILE\|-] [--summary] [--run --fence]`, `fail TSK --reason [--run --fence]`, `accept TSK [--note]`, `reject TSK --reason`, `retry TSK [--model] [--effort] [--reason] [--reuse-workspace]`, `escalate TSK --reason`, `cancel TSK --reason`, `merge TSK`, `wait TSK --until submitted\|terminal\|state:<s> --timeout` | §7.3 |
| dep | `add TSK --on TSK2`, `remove TSK --on TSK2`, `list TSK` | lead |
| approval | `request --category --summary --operation JSON [--project] [--task] [--expires-in]`, `list [--status] [--project]`, `show APR`, `approve APR --op-hash H [--note]`, `reject APR --op-hash H [--note]`, `cancel APR`, `check APR --op-hash H`, `consume APR --op-hash H` | §7.3 |
| inbox | `list [--all]`, `wait --timeout DUR [--max N]`, `ack MSG…` (each with optional `--user`) | own |
| event | `list [--project] [--task] [--type] [--since SEQ] [--limit N]`, `tail [filters]` | read |
| run | `list [--task] [--status]`, `show RUN`, `log RUN [--stream] [--tail]` | read |
| workspace | `list [--project]`, `show TSK`, `prune [--project]`, `delete TSK` | lead / user (delete gated) |
| doctor | `[--check-invariants] [--fix-perms]` | none (local) |
| audit | `verify` | user (direct) |

### 13.4 Idempotency and dry-run
- **Idempotency.** Every mutating command accepts `--idempotency-key`.
  - Key + same `request_hash` (sha256 of the canonical cmd+args, excluding auth) → the stored response is returned with `idempotent_replay:true` and the same exit code.
  - Key + a different request → exit 6 `IDEMPOTENCY_KEY_REUSED`.
  - Keys are scoped per principal (run tokens use their agent principal) and kept for 7 days.
  - The response is stored in the **same tx** as the mutation.
  - Responses with exit 1, 7, or 10 are not stored. Exits 1 and 10 are transient. Exit 7 is not stored because the approval row it creates is itself idempotent (one open row per `op_hash`), and the requester is expected to re-run the identical op with `--approval`, possibly with the same key.
  - `request_hash` covers cmd + args **including** `approval`, and excludes auth, `idempotency_key`, `dry_run`, and `format`. So: key K → exit 7 (not stored) → the same op with key K and `--approval A` executes and is stored → replaying that exact request returns the stored response → key K without `--approval` afterwards → exit 6 `IDEMPOTENCY_KEY_REUSED`.
- **Dry run.** `--dry-run` runs auth, authz, validation, and the transition checks inside a tx, then rolls back. It returns the same exit code the real call would return. It writes no events, outbox rows, or idempotency rows, and it spawns no processes and runs no git commands. It never creates approval rows: a gated op in dry-run returns exit 7 with `approval_id: null` and the computed `op_hash`.

### 13.5 End-to-end example (user and Worker 2)
```sh
mesh init                                      # prompts for the passphrase on /dev/tty
mesh daemon start
mesh agent create --user --name kiro-lead --adapter kiro --default-model claude-opus-5.5 \
     --allowed-model claude-opus-5.5 --allowed-model claude-sonnet-5.5 --max-concurrency 1 --can-lead
mesh agent create --user --name agy-impl --adapter agy --default-model gemini-3.8-flash-high \
     --allowed-model gemini-3.8-flash-high --allowed-model gemini-3.8-flash-medium --max-concurrency 2
mesh agent create --user --name worker2 --adapter none --delivery inbox --max-concurrency 1 --capability relay
mesh agent token issue worker2 --user | jq -r .data.token > ~/.config/worker2-mesh.token && chmod 600 ~/.config/worker2-mesh.token
# keep token files outside every workspace; any same-uid process can still read them (§7.6)
mesh config set lead.default_agent '"kiro-lead"' --user
# Worker 2 (its own token):
mesh intake submit --token-file ~/.config/worker2-mesh.token --title "Add CSV export" \
     --goal "…" --criterion "export button produces RFC4180 CSV" --repo /path/repo \
     --base-branch main --test-command '["python3","-m","unittest"]'
mesh inbox wait --token-file ~/.config/worker2-mesh.token --timeout 10m   # completion / approvals for the user
```

---

## 14. Adapters

### 14.1 Interface (`mesh/adapters/base.py`)
```python
class Adapter:
    id: str                       # 'droid' | 'agy' | 'kiro' | 'vibe' | 'hermes' | 'openclaw' | 'none' | 'fake'
    kind: str                     # 'vendor' | 'test_double' | 'none'
    binary_names: list[str]       # PATH lookup names; agent.binary_path overrides. No hardcoded home paths.
    planned_version: str | None   # version observed at planning time (§1)
    capabilities: dict[str, Capability]   # name -> (supported: yes|no|unknown, verification: help|smoke|none, note)
    liveness: str                 # 'stream' | 'none' (governs idle timeout)
    max_argv_prompt_bytes: int    # 100_000 for argv-prompt adapters; above that see the §12.5 fallback
    launch_enabled: bool          # False for every vendor adapter until M3; True for fake (testing mode only)
    launch_milestone: str | None  # e.g. "M3" while launch_enabled is False
    def probe(self) -> Probe                      # read-only: --version, --help, model list
    def list_models(self) -> list[str] | None
    def build(self, req: RunRequest) -> CommandSpec   # argv, cwd, stdin ('devnull' | path)
    def parse(self, run_dir) -> RunResult          # final_text, session_id|None, usage|None, vendor_exit_meaning
    def profile_flags(self, profile: str) -> list[str]   # raises ProfileUnsupported
```
- `RunRequest` fields: `prompt_path`, `workspace`, `model`, `effort`, `profile`, `resume_session_id`, `timeout_s`, `options`.
- `build` is a pure function. Golden tests pin its output for every adapter and profile.

**Status words used everywhere** (`mesh adapter list`, docs):
- `verified`: every capability Mesh uses is smoke-verified on the recorded version.
- `partially_verified`: installed and help-verified, but at least one capability is not smoke-verified.
- `not_installed`
- `unsupported`

A capability's verification becomes `stale` if the probed version differs from the version recorded at verification.

`adapter list` and `adapter show` always print `launch_enabled` and `launch_milestone` next to the status. The status word describes **evidence**, not usability: in M1, droid/agy/kiro/vibe show `partially_verified` with `launch_enabled:false, launch_milestone:"M3"`, and launching them returns exit 13 `ADAPTER_UNSUPPORTED`.

### 14.2 Status at planning time and permission-profile mapping

Profiles are tool/autonomy permissions (§9.4), **not** reasoning effort (§9.1). Every flag below is cited from `tests/fixtures/real/` (§14.11). "unsupported" means exit 13 `PROFILE_UNSUPPORTED`. Mesh never substitutes another profile.

| Adapter | Installed | Version | Status now | readonly | edit | dev | elevated | unrestricted (trust-all) |
|---|---|---|---|---|---|---|---|---|
| droid | yes | 1.3.0 | partially_verified (help; one real init event) | *(no autonomy flag; exec-help.txt L56–67 "DEFAULT (no flags) Read-only mode")* | `--auto low` (L69–72) | `--auto medium` (L74–80) | `--auto high` (L82–89; still blocks system-wide changes, unlike trust-all) | `--skip-permissions-unsafe` (L13, L91–96; "Cannot be combined with --auto flags") |
| agy | yes | 1.5.2 | partially_verified (help + `agy models`) | `--mode plan` (help.txt L14; whether it blocks writes is UNVERIFIED) | `--mode accept-edits` (L14) | **unsupported until smoke S-AGY-8** shows `--mode auto` is narrower than `--dangerously-skip-permissions` (L14 gives no semantics for `auto`) | unsupported (no middle tier) | `--dangerously-skip-permissions` (L7) |
| kiro | yes | 3.1.0 | partially_verified (help + model JSON; one real metadata run) | `--trust-tools=` (chat-help.txt L35–37 "trust no tools"; headless behavior on an untrusted tool UNVERIFIED) | `--trust-tools=fs_read,fs_write` (L36) | **unsupported until the shell tool name is verified** | **unsupported** (the only wider flag is trust-all, L32–33) | `--trust-all-tools` (L32–33) |
| vibe | yes | 3.1.0 | partially_verified (help only) | `--agent ask` (help.txt L42–43; headless behavior UNVERIFIED) | `--agent accept-edits` (L43) | `--agent smart-approve` (L43, L48; headless prompting UNVERIFIED) | **unsupported** (no evidence that `--agent auto-approve` differs from `--auto-approve`, L43, L46–47) | `--agent auto-approve --auto-approve` (L43, L46–47) |
| hermes | **no** | — | not_installed | — | — | — | — | — |
| openclaw | **no** | — | not_installed | — | — | — | — | — |
| none | n/a | — | inbox-only agents; cannot launch | — | — | — | — | — |
| fake | n/a | — | **test double, not a vendor**; enabled only in testing mode | `--fake-profile readonly` | `--fake-profile edit` | `--fake-profile dev` | `--fake-profile elevated` | `--fake-profile unrestricted` (no trust-all semantics; the fake only records it) |

Rules enforced by golden tests (`test_adapters_build_*`, G24):
- For every adapter, including `fake`, the flag lists of any two distinct **supported** profiles differ. `fake` supports all five profiles so that D16 and G13 can run; its flags are not vendor flags, and the trust-all rule below does not apply to it.
- A vendor's trust-all flag (column "unrestricted") appears only in the `unrestricted` profile.
- **Only template flags.** For droid, agy, kiro, and vibe, Mesh emits only the flags in that adapter's "argv built" template (§14.3–§14.6) plus its profile flags from this table. Every flag in a "never use" list, and every other flag or subcommand in the help fixtures, is never emitted. `test_adapter_flags_in_fixtures` checks each golden argv against this per-adapter allow-list as well as against the help fixture.
- vibe always gets an explicit `--agent` (its default otherwise comes from the user's vibe config, help.txt L43, which Mesh must not read). kiro always gets an explicit `--trust-tools=…` or `--trust-all-tools`.
- A user-set adapter option that selects a vendor-side permission profile (kiro `--agent <profile>`, agy `--agent <name>`, a custom vibe agent) can change tool permissions in ways Mesh cannot inspect. Only the user can set it (agent config is user-only), and `agent show` warns about it.

Under the default `max_unapproved_profile=dev`, `elevated` and `unrestricted` always need user approval (`permission_escalation`, `direct_only` by default, §7.4).

### 14.3 droid (Factory), 1.3.0 — evidence: `tests/fixtures/real/droid/1.3.0/exec-help.txt` (cited as L<n>), `help.txt`, `exec-init-event-opus55-high.json`
- **Non-interactive:** `droid exec [options] [prompt]` (L1; help.txt L23). The prompt comes from `-f, --file <path>` (L11) or stdin (L320–321 `cat prompt.txt | droid exec`).
- **Output:** `-o, --output-format text|json|stream-json|stream-jsonrpc|acp` (L9).
- **Model:** `-m, --model <id>` (L17, help default `gpt-6.1-sol`). Models are listed under "Available Models:" (L125–185, e.g. `claude-opus-5-5`, `gemini-3.8-flash`, `gpt-6.1-sol`, `glm-5.3`).
- **Effort:** `-r, --reasoning-effort <level>` (L18). Supported values per model are under "Model details" (L187–247; Opus 5.5 at L191: low, medium, high, xhigh, max). One real run with `--model claude-opus-5-5 --reasoning-effort high` emitted an init event echoing `"reasoning_effort": "high"` (`exec-init-event-opus55-high.json`).
- **Resume:** `-s, --session-id <id>` ("requires a prompt", L15) and `--fork <id>` (L16).
- **Permissions:** `--auto low|medium|high` (L12, L56–89), `--skip-permissions-unsafe` (L13, L91–96). Tool controls: `--only-tools`, `--add-tools`, `--remove-tools` (L22–24).
- **Workspace:** `--cwd <path>` (L25). Do **not** use `-w/--worktree` (L27), because Mesh manages worktrees.
- **Traceability:** `--tag <spec>` (L29), `--log-group-id <id>` (L30).
- **Never use:** `--mission`, `--worker-model`, `--worker-reasoning-effort`, `--validator-model`, `--validator-reasoning-effort` (L31–35; multi-agent orchestration inside droid would bypass Mesh scheduling), `--settings` (L26), `--worktree-dir` (L28), `--input-format stream-jsonrpc` (L10, L41–54); from `help.txt`: top-level `-r/--resume` and `--last` (L10–11), and the `resume`, `daemon`, `search`, `update`, `mcp`, `plugin`, and `computer` subcommands (L22, L24–L29). Mesh runs only `droid exec` (and `droid exec --help` for `list_models`).
- **Auth:** the vendor reads `FACTORY_API_KEY` (L259–270) or its own login (the fixture run used an account login, no API key). Mesh passes the env through and never logs it. A wrapper binary (e.g. `droid-with-factory-key`) may be set via `--binary`. Mesh does not read it.
- **argv built:** `droid exec --output-format stream-json --cwd <ws> --model <m> [--reasoning-effort <e>] <profile flags> [--session-id <sid>] --log-group-id <run_id> --file <run_dir>/prompt.md`, with stdin = DEVNULL.
- **Model list:** `list_models()` parses `droid exec --help` (it reads lines under `Available Models:` until a blank line; the first token of each line is the id).
- **Session id:** the first stream-json event of one real run is `{"type":"system","subtype":"init",…,"session_id":"…","model":…,"reasoning_effort":…}` (`exec-init-event-opus55-high.json`). The parser takes `session_id` from that event.
- **UNVERIFIED (smoke S-DROID-*):**
  - the schema of every other stream-json event (only the init event is captured);
  - that `--session-id` combined with `--file` resumes;
  - exit codes on model or auth errors;
  - that SIGTERM stops the run promptly;
  - that `--auto low` allows the `mesh` CLI to run (if not, auto-submit covers it).

### 14.4 agy (Antigravity), 1.5.2 — evidence: `tests/fixtures/real/agy/1.5.2/help.txt` (cited as L<n>) and `models.txt`
The `Usage of agy:` header (L1) suggests Go's `flag` package, where flags must precede positional args. That is an inference, settled by S-AGY-1; Mesh puts all flags first anyway.
- **Non-interactive:** `-p, --print` (L18–19, "Run a single prompt non-interactively and print the response"), alias `--prompt` (L22), and `--print-timeout` (L20, default 0s = wait).
- **Output:** `--output-format text|json|stream-json` (L17). `--input-format text|stream-json` (L11; stream-json needs stream-json output). `--json-schema` (L12) enforces a structured final result.
- **Model:** `--model` (L15). `agy models` (L38) prints `id<TAB>display` lines after a "Fetching available models..." line (`models.txt` L1–7; network fetch). Observed: `gemini-3.8-flash-high|medium|low`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`.
- **Effort:** `--effort low|medium|high|xhigh|max` (L9). How it interacts with effort-suffixed model ids is UNVERIFIED. Rule: a model id ending in `-low`, `-medium`, `-high`, `-xhigh`, or `-max` already encodes effort. If an effort is also resolved for such a model (from `--effort` or the agent's `default_effort`), validation rejects it with exit 2 `EFFORT_NOT_ALLOWED` (details: `reason:"model_encodes_effort"`, `model`, `effort`), at `agent create/update` (when `default_effort` is set and the default model has a suffix) and at assign, reassign, or retry. Mesh never drops a user's effort silently. With an unsuffixed model, `--effort` is passed as given.
- **Resume:** `--conversation <id>` (L6). `-c/--continue` (L4–5) is ambiguous across concurrent tasks and MUST NOT be used.
- **Permissions:** `--mode accept-edits|auto|plan` (L14) and `--dangerously-skip-permissions` (L7). `--sandbox` (L26, "terminal restrictions") is an optional adapter option and a vendor feature, not a Mesh guarantee.
- **Workspace:** there is **no cwd flag**, so Mesh sets the process cwd. `--add-dir` (L2) is used only for the prompt-file fallback (§12.5).
- **Never use:** `--remote-agent` (L24), `--remote-control` (L25), `--new-project` (L16), `--project` (L21), `-i/--prompt-interactive` (L10, L23), `--settings` (L27), `--log-file` (L13), and the `acp`, `remote-control`, `update`, `install`, `plugin`, `mcp`, and `mic-serve` subcommands (L29–42).
- **User-only:** `--agent <name>` (L3, "Agent for the current CLI session") may select a vendor-side agent with different permissions. Like kiro `--agent`, only the user can set it (adapter option `agent`), and `agent show` warns about it.
- **argv built:** `agy --print --output-format stream-json --model <m> [--effort <e>] <profile flags> [--sandbox] [--agent <name>] [--add-dir <run_dir>] [--conversation <id>] --print-timeout <timeout_s>s <prompt>` (`--sandbox` and `--agent` only from user-set adapter options; `--add-dir` only for the §12.5 fallback).
  **How the prompt is passed is UNVERIFIED:**
  - (A) a trailing positional after `--print`;
  - (B) `--print` takes the prompt as its value;
  - (C) stdin.
  Smoke S-AGY-1 tries A, then B, then C, and stores the working mode as adapter option `prompt_mode`. The default is A until verified.
- **Version note:** the requirements recorded 1.4.8; 1.5.2 was observed on 2026-11-06.
- **UNVERIFIED:**
  - the json and stream-json schemas;
  - where the conversation id appears;
  - the semantics of plan mode (does it block writes?);
  - the semantics of `--mode auto` (S-AGY-8: run a task needing a shell command and a file write under `--mode auto`; record whether any prompt or denial occurs; `dev` stays unsupported until it is shown to be narrower than `--dangerously-skip-permissions`);
  - exit codes;
  - SIGTERM behavior.

### 14.5 kiro-cli, 3.1.0 — evidence: `tests/fixtures/real/kiro/3.1.0/chat-help.txt` (cited as L<n>), `help.txt`, `list-models.json`, `effort-high-run-metadata.jsonl`
- **Non-interactive:** `--no-interactive` (L39). `--output-format stream-json` (L67–76, "The run's ACP events as JSON Lines", which implies `--no-interactive`).
- **Prompt:** the positional `[INPUT]` (L3–7).
- **Model:** `--model <MODEL>` (L26). `kiro-cli chat --list-models` (L54) with `-f json` (L57–65) returns (`list-models.json`, pretty-printed) `{"models":[{"model_id":…}],"default_model":"auto"}` (observed: `auto`, `claude-opus-5.5`, `claude-sonnet-5.5`, `claude-haiku-5.5`, `gpt-5.6-sol|terra|luna`, `kimi-k3`, `claude-opus-5`, …, `qwen3-coder-next`; 22 entries, with credit `rate_multiplier`).
- **Effort:** `--effort <EFFORT>` (L29–30, "e.g. low, medium, high, xhigh, max"). One real run with `--model claude-opus-5.5 --effort high` emitted metadata events with `reasoning.effort:"high"` and `effortLevels` (`effort-high-run-metadata.jsonl` L3–5). The fixture README records that an invalid model id combined with `--effort` exits 1 with "failed to set effort … model does not support additional fields"; the adapter maps that stderr to failure reason `model_or_effort_rejected` (details: the stderr line plus the note "README observation: this message was seen with an invalid model id, so check the model id first"; the task fails like any non-zero exit). The reason is not named `effort_rejected` because the observed cause was the model id.
- **Resume:** `--resume-id <SESSION_ID>` (L13). `-r/--resume` (L10) resumes the most recent session in the directory (avoid it). `-l/--list-sessions` (L47) with `-f json` lists sessions **for the current directory**.
- **Permissions:** `-a/--trust-all-tools` (L32–33) and `--trust-tools=<names>` (L35–37; the help example uses `fs_read,fs_write`; other tool names are unknown). `--trust-workspace` (L42) applies to V3 only.
- **Other:**
  - `--agent <AGENT>` (L23, "Context profile to use"), `--agent-engine v1|v2|v3` (L105–108, default v2), `--mode default|spec` (L119–122, V3).
  - `--require-mcp-startup` (L94–95) exits 3 on an MCP failure.
- **Never use:** `--cloud` (L124), `--repo` (L128), `--tui` (L97), `--sessions` (L19), `--resume-picker` (L16), `--legacy-ui` (L100), `-d/--delete-session` (L78, deletes user data), and the `login`, `logout`, `serve`, `update`, and `crew` subcommands (help.txt L8, L9, L20, L18, L15).
- **Workspace:** no cwd flag, so Mesh sets the process cwd. Sessions are per directory, which fits per-task dirs.
- **argv built:** `kiro-cli chat --no-interactive --output-format stream-json --model <m> [--effort <e>] <profile flags> [--resume-id <sid>] [--agent <p>] <prompt>`.
- **Session id:** the real run produced `{"type":"metadata","data":{"sessionId":"…",…}}` lines (`effort-high-run-metadata.jsonl` L1–5); the parser takes `data.sessionId` from the first such line. Whether `--output-format stream-json` emits exactly these lines is confirmed by S-KIRO-2. Fallback: after the run, execute `kiro-cli chat --list-sessions -f json` in the workspace (read-only) and take the newest entry.
- **UNVERIFIED:**
  - the full event schema (only metadata lines are captured);
  - whether a headless run that needs an untrusted tool is denied, fails, or hangs (the wall-clock timeout bounds a hang);
  - the shell tool's name, which unlocks `dev`;
  - exit codes.

### 14.6 Mistral Vibe, 3.1.0 — evidence: `tests/fixtures/real/vibe/3.1.0/help.txt` (cited as L<n>)
- **Headless:** `-p, --prompt [PROMPT]` (L24–25), or `--prompt-file <PATH>` (L36–37, "Run in programmatic mode with the prompt read from PATH").
- **Output:** `--output text|json|streaming` (L26–27). `--output-dir <DIR>` (L40–41) writes `export.json` (version, effective config, token usage, cost, stop reason, outcome) and the session journal on every exit.
- **Model:** `--model <ALIAS>` (L44–45, headless mode only, new sessions only). There is **no model-list command** (commands at L5–8 and L66 are `mcp`, `config`, `help`, `update`), so `list_models()` returns `None` and the allow-list is the user's responsibility. Mesh MUST NOT read `~/.vibe/config.toml`.
- **Effort:** **no flag**, so the capability is unsupported (exit 2 `EFFORT_UNSUPPORTED`).
- **Resume:** `--resume [SESSION_ID]` (L20–21). Without an id it shows an interactive picker, so Mesh MUST always pass an id. `-c/--continue` (L18–19) MUST NOT be used.
- **Permissions:** `--agent ask|accept-edits|smart-approve|auto-approve` (L42–43; default from the user's vibe config, so Mesh always passes it), `--auto-approve` (L46–47, alias `--yolo`), `--smart-approve` (L48–49), `--enabled-tools` (L50–51), `--disabled-tools` (L52–53), and `--trust` (L54–55, "Trust the working directory for this invocation only").
- **`--trust` is UNVERIFIED and off by default.** The help does not say what trusting a workdir unlocks; it may make vibe load project-local config from the workspace, which could change the effective tool permissions (§7.7). Mesh emits `--trust` only when adapter option `trust_workdir` is true, which M3 sets only if smoke S-VIBE-9 shows that headless runs fail or prompt without it. The option is recorded in `runs.argv_redacted_json`, and golden tests pin both values.
- **Limits:** `--max-turns` (L28), `--max-price <dollars>` (L30), `--max-tokens` (L32), and `--time-limit <SECONDS>` (L34–35; exits 3 and writes the export; SIGTERM does the same).
- **Workspace:** `--workdir <CWD>` (L16–17). Do not use `--worktree` (L56–57; Mesh manages worktrees).
- **Never use:** `--setup` (L22), `--check-upgrade` (L60), `--system-prompt-file` (L38), `--add-dir` (L58), and the `update` and `config` commands.
- **argv built:** `vibe --prompt-file <run_dir>/prompt.md --output streaming --workdir <ws> [--model <alias>] <profile flags> [--resume <sid>] [--trust] --time-limit <timeout_s> --output-dir <run_dir>/vibe-out [--max-turns N] [--max-price X] [--max-tokens N]`.
  - With `--resume`, omit `--model`. If the task's model differs from the session's model, start a new session instead.
  - Budget options come from `adapter_options` (`max_turns`, `max_price_usd`, `max_tokens`). A task that asks for more than the agent's cap requires `permission_escalation` approval.
- **Result source:** prefer `vibe-out/export.json` over stdout. Map exit 3 to `timeout`.
- **UNVERIFIED:**
  - that `--prompt-file` alone triggers headless mode;
  - the export.json schema and where the session id appears;
  - how `ask` and `smart-approve` behave headless;
  - whether `--agent auto-approve` alone is narrower than `--auto-approve` (until shown, `elevated` stays unsupported);
  - whether headless runs need `--trust`, and what it changes (S-VIBE-9);
  - the `streaming` format;
  - that model aliases resolve.

### 14.7 Hermes — not installed
`command -v hermes` returned nothing. The adapter module registers `status=not_installed`, with all capabilities `unknown` and **no flags**. `agent create --adapter hermes` → exit 13 `ADAPTER_NOT_INSTALLED`. `adapter probe hermes` re-checks the PATH.

Evidence: `tests/fixtures/real/hermes/not-installed.txt`.

**Interim path (no adapter needed).** Once installed, Hermes can join Mesh at once as an `adapter=none, delivery=inbox` agent, as long as it can run shell commands: it calls `mesh inbox wait`, `mesh task start/heartbeat/submit` with its own token (§12.3). This is **inbox participation, not adapter support**. `adapter list` keeps `hermes` as `not_installed` (or `unsupported`) until the M4 adapter work, and `docs/adapters.md` says so in these words.

When the user installs it and confirms which product "Hermes" means (§19), the path is M4: probe, document the observed flags in `docs/adapters.md`, implement `build`/`parse`, add golden tests, and run opt-in smoke tests. Until then Mesh claims nothing.

### 14.8 OpenClaw — not installed
Same as Hermes (`command -v openclaw` returned nothing; `tests/fixtures/real/openclaw/not-installed.txt`), including the interim inbox-participation path. A directory `/workspace/openclaw-plugins` exists on this box but was not inspected and is not evidence of an installed CLI.

### 14.9 fake (test double) and none
- **`fake`** runs `python3 -P -s -m mesh.testing.fake_agent --fake-profile <profile> <prompt_path>` (§7.6 mitigation 8; the profile flag is from §14.2 and is only recorded, it changes no behavior). It is available **only** when meshd runs with `MESH_TESTING=1`. Otherwise `agent create --adapter fake` → exit 13 `TEST_DOUBLE_DISABLED`, and `adapter list` omits it. When listed, it shows `kind: "test_double", vendor: null, note: "not a vendor integration"`.
  - Behavior is selected by the first prompt line that matches `^FAKE (\{.*\})$` (the task spec is embedded in the prompt). Supported behaviors:
    - `succeed`: print text and a `mesh-deliverable` block, then exit 0. Option `text`.
    - `submit_via_cli`: run `mesh task submit` using the env token, then exit 0.
    - `fail`: exit with `code` (default 1). Option `stderr`.
    - `sleep`: sleep `seconds`. Option `output_every`.
    - `ignore_sigterm`: trap SIGTERM and sleep.
    - `spawn_grandchild`: leave a child that sleeps 1000 s in the same pgid.
    - `spawn_setsid_grandchild`: leave a child that calls `os.setsid()` and sleeps.
    - `write_file`: write `path` relative to cwd with `content`.
    - `git_commit`: write a file, then `git add` and `git commit`. The fixture repo must have a local identity.
    - `print_secrets`: print `$MESH_TOKEN` and `$FAKE_SECRET_API_KEY`.
    - `emit_session`: print `{"session_id": …}`.
    - `record_pid`: append `pid,start_ts,MESH_RUN_ID` to `path`.
    - `try_approve`: attempt `mesh approval approve` and print the exit code.
    - `stale_submit`: sleep `seconds`, then submit.
    - `lead_actions`: print the given `mesh-actions` block.
    - `steal_token`: read the token file at `path` and run `mesh <args…> --token-file <path>` (the credential-theft negative test, §7.1); print the exit code and `error.code`.
    - `bad_passphrase`: run `mesh whoami --user --passphrase-fd 3` `count` times with a wrong passphrase on fd 3; print each exit code and `error.code`.
  - Adapter option `resume_verified` (bool, default false) exists only for `fake` in testing mode. It makes the fake adapter report its `resume` capability as verified, so resume plumbing (L5) can be tested both ways.
  - Behaviors can be combined through a `steps` list.
- **`none`** is for inbox-only agents. It cannot launch, and `delivery` must be `inbox`.

### 14.10 Test tiers (what proves what)
| Tier | Command | Touches real CLIs? | Proves |
|---|---|---|---|
| unit | `python3 -m unittest discover -s tests/unit -t .` | no | argv golden output per adapter and profile; refusal of unsupported profiles; prompt-length fallback; `list_models` parsing against the committed real fixtures `tests/fixtures/real/droid/1.3.0/exec-help.txt`, `tests/fixtures/real/agy/1.5.2/models.txt`, and `tests/fixtures/real/kiro/3.1.0/list-models.json` (§14.11; never invented); session-id parsing against `exec-init-event-opus55-high.json` and `effort-high-run-metadata.jsonl`; fixture hashes; deliverable parsing |
| integration / recovery | `python3 -m unittest discover -s tests/integration -t .` (and `tests/recovery`) | no — **PATH is prefixed with `tests/shims/`**, whose `droid`, `agy`, `kiro-cli`, `vibe`, `hermes`, and `openclaw` stubs append to a tripwire file and exit 99. Teardown asserts the tripwire is empty. | Mesh semantics with the fake adapter only |
| probe (opt-in) | `MESH_PROBE=1 python3 -m unittest discover -s tests/probe -t .` | read-only: `--version`, `--help`, `agy models`, `kiro-cli chat --list-models -f json` (both may contact vendor servers) | the binary resolves; the version is recorded; the live `--help` sha256 is compared with §14.11 (differences are reported as a flag diff); every flag Mesh emits appears in the current `--help`; the model list parses |
| smoke (opt-in, uses quota) | `MESH_SMOKE=1 MESH_SMOKE_ADAPTERS=droid,agy,kiro,vibe python3 -m unittest discover -s tests/smoke -t .` | **yes, real model sessions**, in temp dirs, with profile `readonly` and the cheapest allowed model | see below |

**Smoke cases per adapter X:**
- S-X-1: headless run of "Reply with exactly MESH_SMOKE_OK". Exit 0, the output parses, and the final text contains the token.
- S-X-2: session id captured.
- S-X-3: resume: "What token did you reply with?" returns MESH_SMOKE_OK.
- S-X-4: model flag accepted (and reflected in the output or export, if reported; otherwise recorded as "not observable").
- S-X-5: effort flag accepted (where supported).
- S-X-6: SIGTERM mid-run leaves no process in the group within `kill.grace_s`.
- S-X-7: under readonly, "create file x.txt" is attempted, and whether the file appears is **recorded as an observation**, not asserted as a vendor guarantee.
- S-AGY-1: prompt-passing mode.
- S-AGY-8: `--mode auto` semantics (does it prompt, deny, or auto-approve shell commands and writes?). Until it shows `auto` is narrower than `--dangerously-skip-permissions`, agy `dev` stays unsupported.
- S-KIRO-8: headless behavior with an untrusted tool (deny, fail, or hang).
- S-VIBE-9: S-X-1 in a fresh temp workdir **without** `--trust`. If it completes headless, `trust_workdir` stays false. If it fails or prompts, repeat with `--trust`, record both observations, and set `trust_workdir` to true only then.

**Smoke output** is written to `tests/smoke/results/<adapter>-<version>-<UTC date>.json`. Redacted raw output is saved as new files under `tests/fixtures/real/<adapter>/<version>/` with a `.meta.json` sidecar (§14.11); existing fixture files are never overwritten. Only these real fixtures may be used to write `parse()` tests. Synthetic fixtures MUST be named `synthetic_*.jsonl` and MUST NOT be used to mark any capability as verified. `mesh adapter show X` reads the latest results file and prints per-capability verification, date, and version.

### 14.11 Committed real fixtures (evidence for §14.2–§14.8)
Captured read-only by the coordinator at 2026-11-07T06:47:40Z (see `tests/fixtures/real/README.md`). The README states that only `--help`/model-list commands were run, plus two minimal real runs whose metadata was sanitized. It does not record the exact command line per file, so the "Source" column is inferred from each file's content. These files MUST NOT be edited, regenerated, or extended with invented content. Unit tests read them as they are.

| Path (under `tests/fixtures/real/`) | Source (inferred) | sha256 |
|---|---|---|
| `droid/1.3.0/help.txt` | `droid --help` | `1df2e88e997f1488d22fa59340bc79c13198a143363981ee8825a641260143f3` |
| `droid/1.3.0/exec-help.txt` | `droid exec --help` | `61880b6d4c40e86e497653e95f01beb3a7cdad223a4fd98010d53d6dce30449a` |
| `droid/1.3.0/exec-init-event-opus55-high.json` | first stream-json event of `droid exec --model claude-opus-5-5 --reasoning-effort high` | `e03f9ec3ee4114856c9828a83be463ef8f4d58b41e5a131740d77699d22bd535` |
| `agy/1.5.2/help.txt` | `agy --help` | `e92946f85cec63eca61f93014ca49f53b1e84277eac7954e7fac7d8d712940fd` |
| `agy/1.5.2/models.txt` | `agy models` | `4bdb4f0f081a521804aca90f92132965597e365f10a2afad10f7d5e70d3a4b83` |
| `kiro/3.1.0/help.txt` | `kiro-cli --help` | `353d58ab2ccd44f36372cf16a00a3d051519ccaf4778b5269d6dded4622d17d5` |
| `kiro/3.1.0/chat-help.txt` | `kiro-cli chat --help` | `56bf95560094d7bee17372d18b8fc40ff834ff6b72c338863f91428719b00eeb` |
| `kiro/3.1.0/list-models.json` | `kiro-cli chat --list-models` with a JSON format (22 models, `default_model: "auto"`) | `bdc99135b5a3de929c857c392b25c9c0fe548b56f1bdd7a9eb12dc5a7533dbae` |
| `kiro/3.1.0/effort-high-run-metadata.jsonl` | metadata lines of `kiro-cli chat --no-interactive --model claude-opus-5.5 --effort high` | `41957ed021f3994b1b2c50ba2c293b0a39db505e83ae407063fd043be0ee65fc` |
| `vibe/3.1.0/help.txt` | `vibe --help` | `15d858a3434d0664ee8af445933ef438bc7a6efac9961de31351013c0d2ee158` |
| `hermes/not-installed.txt` | `command -v hermes` (not found) | `2a149e1329f638ffd7e6e5799cffac9ac6f671d92e56b24dff3926739503a154` |
| `openclaw/not-installed.txt` | `command -v openclaw` (not found) | `a102d301e2adc9402c676263c6807ee218d02a667a79f69e0962c985a980775e` |

- `--version` output was not captured as separate files. The versions come from the directory names and the README.
- Unit test `test_fixture_hashes` checks these sha256 values, so a silently edited fixture fails the suite.
- **If `test_fixture_hashes` fails, the implementer stops and reports to the coordinator** (file, expected and actual hash). Neither the fixture files nor this table, nor the test's expected values, may be changed to make the test pass; only the coordinator decides. Planner check (r3, 2026-11-07): `sha256sum` of every file above matched this table.
- Unit test `test_adapter_flags_in_fixtures`: every flag token that any golden argv emits for droid, agy, kiro, or vibe appears in that adapter's help fixture.
- Probe tier (N10): it compares `sha256(<live --help output>)` with this table. If they differ, the probe reports a flag-level diff (flags added or removed) and fails only if a flag Mesh emits is missing.
- New real outputs (M3 smoke) go to new files under `tests/fixtures/real/<adapter>/<version>/`, each with a sidecar `<file>.meta.json` holding `{command, captured_at_utc, sha256}`.

---

## 15. Security

### 15.1 Secrets
- Agent tokens are shown once and stored only as hashes. Run tokens are revoked at run end. The passphrase is never stored; only the scrypt verifier is.
- Secrets never go in argv: tokens use env or a 0600 file, and passphrases use the tty or an fd.
- A `--token-file` under `workspaces.root`, `$MESH_HOME/runs`, or a registered project repo is refused (exit 3 `TOKEN_FILE_INSECURE`, §7.6 mitigation 2). Users are told never to store the passphrase in a file.
- Mesh never reads vendor credential, config, or cookie files. Vendor API keys in meshd's env pass through to children unchanged and are never written by Mesh.
- `spec.json`, events, DB rows, and logs MUST NOT contain secret values. `runs.argv_redacted_json` is redacted.

### 15.2 Redaction (`mesh/core/redact.py`) — applied to run logs (by runwrap), `meshd.log`, event `reason`/`payload`, deliverables, user statements, error messages, and `task log`/`run log` output
1. **Exact values.** Every live token known to the process, plus the env values whose names match `(?i)(key|token|secret|passw|cookie|credential|auth)` and are ≥ 8 chars. They are replaced longest first with `[REDACTED:<name>]`.
2. **Patterns.** Each match is replaced with `[REDACTED]`:
   - `mesh_(at|rt)_[A-Za-z0-9_-]{20,}`
   - `fk-[A-Za-z0-9_-]{16,}`
   - `sk-[A-Za-z0-9_-]{16,}`
   - `AIza[0-9A-Za-z_-]{35}`
   - `gh[pousr]_[A-Za-z0-9]{30,}`
   - `xox[abpr]-[A-Za-z0-9-]{10,}`
   - `(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}=*`
   - `-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----`
   - `(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*\S+`
3. **Never log the `auth` field.** The daemon's request log records only cmd, principal id, request id, exit code, and duration.

### 15.3 File and socket permissions
- umask 077. `MESH_HOME`, `run/`, `runs/`, and `workspaces/` are 0700. The DB, WAL, logs, and token files are 0600. meshd refuses to start on insecure permissions.
- Socket: `$MESH_HOME/run/mesh.sock`, created under umask 077, then `chmod 0600`. Each peer is checked with `SO_PEERCRED` (uid must equal meshd's uid). A stale socket is unlinked only while holding the flock.
- **No network.** The code MUST NOT create `AF_INET`/`AF_INET6` sockets, use `http.server`/`socketserver`, or bind a port. A unit test greps the `mesh/` sources for those names.

### 15.4 Other
- Max request size 1 MiB. Text fields are length-capped: spec 200 KB, reason and statement 4 KB, deliverable 200 KB.
- Prompt injection: specs come from agents. Mesh renders them inside clearly delimited sections and does not interpret them, except for the `FAKE` line in testing mode and `mesh-deliverable`/`mesh-actions` blocks in the **final output of the run that owns them**. Actions are authorized exactly like CLI calls, so injection cannot exceed the lead's own authority.
- `meshd.log` rotates at 10 MiB and keeps 5 files.

---

## 16. Milestones

### M1 — Core (FIRST implementation phase; agy implements this)
**Scope** (build in this order; each step is committed with tests):
1. **M1.1 Skeleton.** `bin/mesh`, the package, the argparse tree for **all** commands in §13.3 (unimplemented verbs return exit 2 `NOT_IMPLEMENTED_IN_M1` with the milestone in details: `"M2"`, `"M3"`, or `"M4"` for verbs scheduled after M1, and the gate that builds the verb, `"M1b"` or `"M1c"`, for M1 verbs not yet built at an intermediate gate), `bin/mesh` with `python3 -P -s` (§7.6 mitigation 8), the envelope, the exit codes, layered help and topics, and `scripts/test.sh`.
2. **M1.2 Storage.** The DDL (§5.5), migrations, ids, the clock, durations, canonical JSON, the event hash chain, and `doctor --check-invariants`.
3. **M1.3 Daemon.** flock, boot, socket with peer-cred check, the protocol, auth (passphrase via tty/fd, agent tokens, run tokens, lockout, run-session classifier), the settings registry (§5.7), idempotency, dry-run, effects, `daemon start/run/stop/status`, permission checks, the install-path check (§7.6 mitigation 7), and `init`.
4. **M1.4 Config, agents, adapters.** Config, agents (models, efforts, profiles, concurrency validation, capabilities), tokens, the adapter registry with read-only `probe`/`models` for the four installed CLIs, `not_installed` for hermes and openclaw, `none`, and `fake` (testing mode only). **No real vendor runs in M1** (`build` exists and is golden-tested, but launching a vendor adapter in M1 returns exit 13 `ADAPTER_UNSUPPORTED` with `details.milestone='M3'`).
5. **M1.5 Projects.** Projects and intake (non-repo projects only; `--repo` returns `NOT_IMPLEMENTED_IN_M1`), criteria, lead default/override/`set-lead` with history and handover message, pause/resume/cancel, complete, and notes.
6. **M1.6 Tasks.** The task state machine (all transitions except T14–T18), deps and project deps, the scheduler (promote, queue, start), events, outbox fan-out, inbox list/wait/ack, `task wait`, `project wait`, and `event list/tail`.
7. **M1.7 Approvals.** All categories, policies, op hashing, direct and relayed decisions, check/consume, expiry, `--approval` re-execution, `audit verify`.
8. **M1.8 Supervision.** runwrap, the supervisor (spawn, watch, timeouts, cancel, kill with the child registry, `MESH_RUN_ID` escapee scan, and reaper of §11.4), leases, fencing, inbox-mode start/heartbeat/submit/fail, auto-submit, the `run list/show/log` and `task log` commands, crash recovery (§11.7 steps 1–4, 6, 7), redaction, plain per-task dirs and lead dirs.
9. **M1.9 Docs.** `docs/usage.md` and `docs/adapters.md` (the status table from §14.2, kept current).

In M1, leads are exercised as **inbox-mode agents** using their tokens. Launched lead turns are M2.

**M1 sub-milestones and gates** (same total scope as M1.1–M1.9; each gate needs a green `scripts/test.sh` with an empty tripwire and a clean `doctor --check-invariants`):
- **M1a — Foundation = M1.1–M1.4.** CLI skeleton and help, storage, invariant checker, daemon, auth (passphrase with `maxmem`, tokens, lockout, the run-session classifier as unit-tested code), settings registry (§5.7), config, agents, tokens, adapter registry with fixture-based golden and parse tests. **Gate:** every checklist item assigned to M1a in checklist §0.1 passes (A8's state-specific clauses apply from M1c; A10 covers those states earlier). Then a **review checkpoint**: the coordinator (or Kiro) reviews the M1a diff and test output before M1b starts.
- **M1b — Coordination = M1.5–M1.7.** Projects, intake, leads and handover, the task state machine up to `queued` (T1–T4, T6 `model_not_allowed`/`adapter_unavailable`, T11–T13 and T19–T22 as far as they need no run), deps, scheduler promotion and queue ordering, events, outbox, inbox, waits, approvals, audit. T5 is not wired yet, so tasks stop at `queued`. **Gate:** all M1a items still pass, plus every item assigned to M1b. Then a review checkpoint.
- **M1c — Execution = M1.8–M1.9.** T5 for launch and inbox mode, runwrap, supervisor, timeouts, kills, child registry and reaper, run-session binding end to end, leases and fencing, auto-submit, crash recovery, redaction, task and lead dirs, and the docs. **Gate:** the M1 exit criteria below.

**M1 exit criteria:** every acceptance-checklist item tagged `M1` passes (that is, every item in the M1a, M1b, and M1c lists of checklist §0.1). `scripts/test.sh` passes offline in < 10 minutes with an empty tripwire. `mesh doctor --check-invariants` is clean after every integration test.

### M2 — Git workspaces, merge, lead turns
Repo projects (§10.2), worktrees, submission git info, the merge pipeline (T14–T18), `project deliver`, `workspace prune/delete`, merge crash recovery (§11.7 step 5), lead turns with debounce, the `mesh-actions` protocol, lead handover prompts, and resume plumbing (exercised with the fake adapter's `emit_session`).

### M3 — Real adapters (droid, agy, kiro, vibe)
Enable `build` for launch. Run the probe tier, then the smoke tier, on this box with the user's consent (it uses quota). Capture real fixtures, write `parse()` from them, set `prompt_mode` for agy, decide the kiro dev mapping, and validate idle-timeout liveness. Update `docs/adapters.md` statuses strictly from the smoke results files.

### M4 — Remaining integrations and polish
Hermes and OpenClaw adapters **if installed** (otherwise they stay `not_installed`, which is a valid outcome). A `custom` user-defined command-template adapter, labelled `custom (user-defined, unverified)`. The `notify.command` hook, `mesh user passwd` (re-MAC with key epochs), and per-project run budgets.

### M5 — Hardening (future; needs user decisions)
Separate OS users for meshd and executors, ACP/JSON-RPC long-lived sessions for real push, and a web UI.
Optional, off by default (approved idea, not built): a Jev decision-model plugin for task routing only (agent / model tier choice; later perhaps review-finding triage and watcher wakes). It never decides approvals, merges or safety gates.

---

## 17. Test plan

### 17.1 Harness (`tests/helpers.py`)
- `MeshHarness` creates `MESH_HOME=/tmp/mh-<rand6>` and runs `mesh init --passphrase-fd` with a test passphrase. It starts `mesh daemon run` as a subprocess with `MESH_TESTING=1` and PATH = `tests/shims:<repo>/bin:$PATH`.
- It provides `cli(*args, token=None, user=False, stdin=None) -> (exit_code, envelope)`, `wait_for(predicate, timeout)`, `kill9_daemon()`, `restart_daemon()`, and `db()` (a read-only connection for assertions).
- Teardown: stop the daemon, run `doctor --check-invariants`, assert the tripwire is empty, and kill any leftover fake processes found by scanning `/proc` for `MESH_RUN_ID` in environ.
- **Testing mode** (`MESH_TESTING=1` in meshd's env only): it enables the `fake` adapter and lowers the minimums marked in §5.7. Testing hooks (`MESH_TEST_CRASH_AT`, `MESH_TEST_STUB_CHILD_S`) are honored only in testing mode. `mesh status` shows `"testing_mode": true`. meshd refuses testing mode if `MESH_HOME` equals the default home.

### 17.2 Unit tests (selection; all MUST exist)
- `test_canonical`: key order, unicode, float rejection, stable op_hash vectors.
- `test_states`: every allowed transition passes, and every other (from, to) pair is rejected. The table is generated from `states.py`, so it is exhaustive.
- `test_policy_concurrency`: all valid and invalid values listed in §9.2.
- `test_policy_models`: default not in list; model not allowed; effort unsupported; agy effort-suffixed model plus an effort → `EFFORT_NOT_ALLOWED` (`model_encodes_effort`); the AG order of §6.2 (e.g. an agy agent with profile `elevated` → `PROFILE_UNSUPPORTED`, not 7 or `ADAPTER_UNSUPPORTED`).
- `test_redact`: each pattern; exact-value replacement; no partial leaks.
- `test_adapters_build_*`: golden argv for each adapter (including `fake`, all five profiles) × profile × resume/non-resume; ProfileUnsupported cases (agy `dev` and `elevated`, kiro `dev` and `elevated`, vibe `elevated`); distinct supported profiles give distinct flags; trust-all flags only under `unrestricted`; only template flags (§14.2); no `-c/--continue` or `--resume` without an id; vibe omits `--model` on resume; vibe with `trust_workdir` false and true.
- `test_adapters_parse_errors`: kiro stderr "failed to set effort … model does not support additional fields" → reason `model_or_effort_rejected` with the README note in details.
- `test_adapters_models_parse`: `tests/fixtures/real/droid/1.3.0/exec-help.txt`, `tests/fixtures/real/agy/1.5.2/models.txt`, and `tests/fixtures/real/kiro/3.1.0/list-models.json` (§14.11).
- `test_authz_matrix`: every row and column of §7.3.
- `test_no_network_code`: grep `mesh/` for `AF_INET`, `AF_INET6`, `http.server`, `socketserver`, `.bind((`.
- `test_stdlib_only`: every import in `mesh/` resolves to `sys.stdlib_module_names` or `mesh`.
- `test_gitops_allowlist`: forbidden subcommands raise.
- `test_proc_identity`: pid + starttime matching; a reused pid is not signalled; `/proc/<pid>/stat` parsing after the last `)`.
- `test_scrypt_params`: derives the passphrase key with exactly the §7.1 parameters (including `maxmem`) without error.
- `test_invariants_valid_states`: §5.6.
- `test_run_session_classifier`: a peer whose sid is an active run's, whose ancestry reaches a wrapper, or whose environ has the `MESH_RUN_ID` of any known run (active or ended) is "inside run"; other peers are not.
- `test_reaper_inflight_spawn`: with `spawns_in_flight > 0` the reaper does not scan; a loop of 200 fast-exiting registered children (`true`) with the reaper forced to run every 10 ms (testing) always yields returncode 0 from asyncio.
- `test_install_path_check`: an install root equal to or under `workspaces.root`, `$MESH_HOME/runs`, or a registered repo → `INSTALL_PATH_INSECURE`; elsewhere → ok.
- `test_prompt_t5_too_long`: a kiro `readonly` task whose spec fits but whose T5 prompt with rework feedback exceeds the limit → T6 reason `prompt_too_long`.
- `test_settings_registry`: every §5.7 key, default, type, and range; unknown key → `INVALID_SETTING`.
- `test_prompt_fallback`, `test_fixture_hashes`, `test_adapter_flags_in_fixtures`: §12.5, §14.11.
- `test_idempotency_approval_rerun`: §13.4.

### 17.3 Integration tests (fake adapter; offline)
- **CLI:**
  - every command's `--help` exits 0 and contains `Examples:`;
  - every error path prints a valid envelope;
  - `--input -` is equivalent to flags;
  - DUPLICATE_PARAM and UNKNOWN_PARAM;
  - an idempotent replay returns the same body, and a reused key gets 6;
  - key K on a gated op → 7 (not stored); the same op with key K and `--approval` → 0; replay → the stored 0; key K without `--approval` → 6;
  - `config set` with an unknown key or a wrong type → 2 `INVALID_SETTING`;
  - dry-run leaves the counts in `events`, `outbox`, `runs`, and `idempotency` unchanged and spawns no process.
- **Lead:**
  - the default lead;
  - the override;
  - NO_LEAD;
  - `set-lead` (history, old token gets 4/9, handover message, approvals cancelled);
  - LEAD_CANNOT_BE_ASSIGNEE.
- **Tasks:**
  - full happy path (create, assign, queued, running, submitted, accepted);
  - reject, then rework, then a second run whose prompt contains the reason;
  - deps gating;
  - DEP_CYCLE;
  - a project dep;
  - intake into an existing project wakes the lead.
- **Concurrency:**
  - max 2 with 5 tasks sleeping 3 s and `record_pid`: the maximum overlap is 2, and all 5 finish;
  - the order follows priority, then FIFO;
  - `queue_position` is reported;
  - the global cap;
  - an inbox-mode `task start` at the limit gets 11.
- **Models:** the default is used (`MESH_MODEL` env given to fake / argv recorded); MODEL_NOT_ALLOWED; the lead changing the allow-list gets 4; an allow-list change while queued → `failed(model_not_allowed)`.
- **Failure:**
  - timeout: the process group is gone before the `failed` event timestamp, and no pid from `record_pid` is alive;
  - a non-zero exit: no new run within 20 s (no auto retry), and exactly one `task.failed` message reaches the lead;
  - retry up to max_attempts, then exit 7;
  - reassign;
  - escalate notifies the user and the origin;
  - cancel with `ignore_sigterm` (the SIGKILL path) and with `spawn_grandchild` and `spawn_setsid_grandchild` (escapee scan, §11.4);
  - `stale_submit` gets 9;
  - `spawn_setsid_grandchild` followed by `succeed` (exit 0): the task is auto-submitted, `run.leftovers_terminated` lists the grandchild, and the lease is released only after no process carries the run's `MESH_RUN_ID` (§11.3, §11.4);
  - `test_python_isolation_flags`: the wrapper and fake cmdlines contain `-P -s -m`; a `mesh/runwrap.py` written into the workspace by attempt 1 is not imported by attempt 2's wrapper;
  - inbox `no_response`;
  - inbox `lease_expired`, then retry: the new attempt's `workspace_path` is `tasks/<tsk>.a2/`; with `--reuse-workspace` it is unchanged;
  - `task reject` at `attempt == max_attempts` → 7 `retry_over_limit`, task stays `submitted`;
  - `task cancel` on a `submitted` task during post-submit grace → `cancelling`, then `cancelled` only after the group is gone;
  - `test_cancel_does_not_kill_unrelated_children`: with testing hook `MESH_TEST_STUB_CHILD_S=30`, meshd starts one registered asyncio child `sleep 30` and writes its pid to `run/stub.pid`; cancelling a `spawn_setsid_grandchild` task leaves the stub alive, it later exits with returncode 0, and no zombie with `PPid == meshd` remains.
- **Approvals (forgery negatives):**
  - A1: an executor run token tries `approval approve` (the fake `try_approve` behavior) → 4.
  - A2: an executor or agent without `relay` uses `--relayed` → 4.
  - A3: the lead approves its own request without `--relayed` → 4 `CAPABILITY_MISSING` (check order, §7.3).
  - A4: a relayer passes the wrong `--op-hash` → 12.
  - A5: a relayer approves its own request → 4.
  - A6: a relayed decision when the policy is `direct_only` → 12 `CHANNEL_NOT_ALLOWED`.
  - A7: a wrong passphrase → 3, and the 6th attempt → `LOCKED_OUT`.
  - A8: `--approval` with changed args → 12 and the target is unchanged.
  - A9: a double consume → 12; a double approve → replay; approve then reject → 6.
  - A10: expired → 12.
  - A11: a deliverable saying "user approved" → the gated op still gets 7.
  - A12: a passphrase in the stdin JSON → 2, and `MESH_PASSPHRASE` in the env is ignored (→ `NO_TTY` exit 3 when there is no tty or fd).
  - A13: a revoked token, and a run token after the run ended → 3.
  - A14: the old lead after `set-lead` → 4 `NOT_PROJECT_LEAD`.
  - A15: consumption by a non-requester → 4.
  - A16: direct DB tampering (the test sets an approval to `approved` with a bogus `user_mac` via sqlite) → `audit verify --user` exits 12 and lists the id. This is a DB-only forgery; code tampering and passphrase capture are not testable defenses (§7.6 residual risk).
  - A17: `approval show` distinguishes `direct` (`trust:"direct_verified"`) from `relayed (unverified; by worker2)` (`trust:"relayed_unverified"`) and includes the statement; `approval check` returns `trust` too.
  - A18: fake `steal_token` inside a launched run uses grok's token file for `approval approve --relayed` and lead1's for `task accept` → each 4 `CREDENTIAL_NOT_ALLOWED_IN_RUN`; the approval stays `pending`; an `auth.credential_in_run` event exists.
  - A19: fake `bad_passphrase` with count 6 inside a run → each 4 `CREDENTIAL_NOT_ALLOWED_IN_RUN`; `auth_failures` gains no rows; the user's next direct command succeeds (no `LOCKED_OUT`).
  - A20: default policy: a relayed approve of a `permission_escalation` approval → 12 `CHANNEL_NOT_ALLOWED`; a direct approve works.
  - A21: `--token-file` under `$H/workspaces/` → 3 `TOKEN_FILE_INSECURE`.
- **Notifications:**
  - dedup (the same submit replayed via idempotency → one message);
  - `inbox wait` returns ≤ 1 s after the event, and gets 8 on timeout;
  - ack is idempotent;
  - messages persist across a daemon restart.
- **Security:**
  - file and socket modes;
  - `ss -ltnupH` shows no meshd entry;
  - `print_secrets`: the logs contain `[REDACTED` and not the token or the fake key; grepping `MESH_HOME` for the test passphrase and the issued tokens finds 0 hits;
  - `/proc/<child>/cmdline` contains no token;
  - a 0755 `MESH_HOME` is refused.

### 17.4 Crash/recovery tests (`tests/recovery`)
- **R1.** kill -9 meshd during a `sleep 8` run, then restart: the run is adopted and finishes, `record_pid` shows exactly 1 pid, and the task is `submitted`.
- **R2.** kill -9 meshd, then kill -9 the wrapper group, then restart: the run is `lost`, the task is `failed(lost_on_restart)`, and the lead has exactly 1 message.
- **R3.** meshd stays down past the deadline: on restart it terminates the run as `timeout`, and the group is verified gone.
- **R4.** kill -9 meshd in the middle of a burst of 200 `task create` calls with idempotency keys, then restart: `PRAGMA integrity_check` is `ok`, the invariants pass, and replaying all 200 keys yields exactly 200 tasks.
- **R5.** A pending outbox message while meshd is down is delivered after the restart, exactly once.
- **R6.** An inbox-mode lease survives a restart through the grace period when heartbeats resume.
- **R7.** kill -9 meshd between TX A and the spawn (testing hook `MESH_TEST_CRASH_AT=after_tx_a`): the run is `spawn_failed`, the task is `failed(lost_before_spawn)`, and there is no auto respawn.
- **R8 (M2).** A crash during a merge → `interrupted` or reconciled, and the integration branch is never left half-merged.
- **R9.** Spawn-window race (testing hook `MESH_TEST_CRASH_AT=after_popen`, which crashes meshd right after `Popen` and makes runwrap delay writing `wrapper.json` by 3 s): on restart, recovery finds the live process by `MESH_RUN_ID` and adopts it once `wrapper.json` appears. Exactly one run and one `record_pid` entry exist.
- **R10.** After R1's adoption, `leases.holder` equals the new boot id.

### 17.5 Probe and smoke tiers
See §14.10. They are never part of `scripts/test.sh`.

---

## 18. Deviations from `[SUGGESTED]` requirements and interpretations

1. **Unix socket only.** There is no loopback TCP at all, which is stricter than "loopback or Unix socket".
2. **The identity boundary is enforced at the Mesh API, not the OS** (all agents share uid `box`; §7.6). Real per-principal credentials, capability checks, fencing, best-effort run-session binding, and a user-only passphrase with after-the-fact MAC verification are delivered. The MAC check detects DB-only forgeries; it does not survive code tampering or capture of the passphrase or `K_user` (§7.4, §7.6). Mesh is **not a security sandbox**: a same-uid process that ignores the rules can read other agents' Mesh credentials and forge relayed approvals or lead actions, and one that modifies Mesh's code or reads the passphrase from memory can forge direct approvals too. Prevention needs OS-level separation, which is opt-in future work (M5). This is stated, not hidden.
3. **The lead cannot be an assignee in its own project.** This is added so that acceptance is never self-review.
4. **Auto-submit on exit 0** when the executor could not call `mesh` (its CLI permissions may block shell commands). It is labelled `submitted_by: system`, and the lead still reviews it.
5. **`deliver_to_base` defaults to lead authority** (ff-only, never force), because the CONFIRMED requirement gives merging to the lead. The user can require approval through the policy setting.
6. **JSON is the default output format.** `--format text` is the human option.
7. **Relayed approvals are allowed by default** for every category except `config` (fixed) and `permission_escalation` (default `direct_only`, changeable by the user), to honor the CONFIRMED "relayed by the originating agent" path. Relayed decisions are labelled `relayed (unverified)`. The user is advised to set `direct_only` for `spend_money`, `deploy`, and `delete_external`.
8. **Permission profile `high` was renamed `elevated`** (r2) so it cannot be confused with reasoning effort `high`.

---

## 19. Open questions and risks

| # | Question / risk | Impact | How it will be resolved |
|---|---|---|---|
| Q1 | Where does Worker 2 run? Can it execute `mesh` on this box? (`/home/box/.local/bin/grok` exists, but whether it is "Worker 2" is unknown.) | If Worker 2 is remote, v1 has no network interface, so intake would need a local bridge (out of scope). | Ask the user before M2. M1 works with any local agent. |
| Q2 | Which products are "Hermes" and "OpenClaw", and will they be installed here? | The adapters stay `not_installed`. | User input. Then M4 probe and smoke. |
| Q3 | Default relayed-approval policy per category (§18.7). r2 makes `permission_escalation` `direct_only` by default; the others stay `direct_or_relayed`. | Security vs. convenience: a stolen relayer token can forge relayed approvals (§7.6). | User decision; a setting, no code change. Recommended: `direct_only` for `spend_money`, `deploy`, `delete_external`. |
| Q4 | Should `deliver_to_base` require user approval (§18.5)? | Policy. | User decision; a setting. |
| Q5 | No global git identity on this box. | Merge commits fail with `commit_failed`. | The user configures a repo-local identity. Mesh never sets identity. |
| Q6 | Vendor quota and cost. Kiro uses credits (`rate_multiplier`), vibe has a dollar `--max-price`, and droid and agy use subscriptions. Routine runs and lead turns consume quota without an approval gate. | Unexpected spend. | Debounced lead turns, max_attempts, vibe budget options; per-project budgets in M4. |
| Q7 | Unverified vendor behaviors: agy prompt passing; agy `--effort` with effort-suffixed model ids (Mesh rejects the combination until then); all output and stream schemas; session-id locations; headless behavior on permission prompts (deny, fail, or hang); kiro tool names; vibe `ask`/`smart-approve` headless; whether vibe needs `--trust` and what it unlocks (S-VIBE-9); what vendor project-local config in a workspace can change (§7.7); droid `-s` with `-f`; SIGTERM handling. | Adapter correctness. | M3 smoke tests (§14.10). Hangs are bounded by wall-clock timeouts. |
| Q8 | Same-uid trust limit (§7.6). **Unresolved in v1.** | A same-uid process that ignores the rules can read other agents' token files, forge relayed approvals, act as a lead, edit `mesh.db`, or trigger the lockout from outside a run. It can also modify Mesh's own code or capture the passphrase or `K_user` from memory during a direct request; then it can forge direct approvals with valid MACs, and `audit verify` cannot be trusted. | Documented in `mesh help auth` and `docs/usage.md`. Mitigations: run-scoped env-only tokens, run-session binding, token files and the install kept out of workspaces (`TOKEN_FILE_INSECURE`, `INSTALL_PATH_INSECURE`), `python3 -P -s`, a read-only install recommendation, direct-only defaults for `config` and `permission_escalation`, `relayed (unverified)` labels, user MAC with `audit verify` (DB-only edits). Prevention needs opt-in OS separation (M5). |
| Q9 | Processes that escape via `setsid` **and** clear `MESH_RUN_ID` are not found. For adopted runs, escapees reparent to init for the rest of the run, not only while meshd is down. | Rare stray processes. | The `MESH_RUN_ID` environ scan finds escapees wherever they reparented, unless they cleared it (§11.4). Documented. A future cgroup option (`systemd-run --user --scope`) is unverified. |
| Q10 | Wall-clock jumps affect persisted deadlines and expiry. | Early or late timeouts. | Monotonic clocks in-process; documented. |
| Q11 | Python cannot reliably zero secrets in memory. | The passphrase and `K_user` stay in memory transiently, where a same-uid process can read them (§7.6 residual risk). | Documented. Minimize their lifetime. |
| Q12 | Whether vendor harnesses allow long blocking tool calls (`mesh inbox wait --timeout 10m`). | Inbox-mode latency and cost. | Per-agent recommendation in `docs/usage.md` after M3 observation; shorter waits in a loop are fine. |
| Q13 | agy changed version between the requirements (1.4.8) and planning (1.5.2). | CLI drift. | Probe-time version recording and `stale` verification marks. |
| Q14 | Mesh checks the structure of the completion report, not its truth. | A lead could mark criteria met falsely. | By design. Worker 2's final acceptance is outside Mesh. |
| Q15 | Socket path limit (108 bytes) with custom `MESH_HOME` paths. | Startup failure. | A clear `SOCKET_PATH_TOO_LONG` error. |
| Q16 | The committed fixtures (§14.11) do not record the exact command per file or separate `--version` outputs. | Weaker provenance for parse tests. | Hashes are pinned in §14.11 and checked by `test_fixture_hashes`. New M3 captures carry `.meta.json` sidecars. |
| Q17 | Profiles that cannot be expressed narrowly: agy `dev` (semantics of `--mode auto` unknown), agy `elevated`, kiro `dev` (shell tool name unknown), kiro `elevated`, vibe `elevated`. | Those adapters refuse those profiles (exit 13). | M3 smoke (S-AGY-8, S-KIRO-8) may enable agy and kiro `dev`. `elevated` stays unsupported unless a vendor adds a middle tier. |
| Q18 | Run-session binding (§7.1) relies on `/proc` heuristics (sid, ancestry, environ). | A false negative lets an escaped process use stolen credentials. A false positive would block a legitimate non-run caller, which is unlikely because only run descendants carry the run sid, ancestry, or `MESH_RUN_ID`. | Unit-tested classifier; residual risk documented (§7.6). |


## Amendment 2026-11-10 (coordinator, PR #12 m3-kiro-real, Linear/Quest reviewed)

§11.3 / T7 "exit 0 → auto-submit" is amended to: **exit 0, no signal, and no vendor failure meaning → auto-submit (T7)**. An exit-0 run whose adapter `parse()` reports a non-empty `vendor_exit_meaning` (errors, refusals, budget/turn/time exhaustion: e.g. kiro auth_failed/run_error/content_filtered, agy run_error, vibe run_error/terminated/turn_limit/limit) goes to **T8 with reason `vendor_error`** (no automatic retry; details carry exit_code, vendor_exit_meaning and stderr tail); the lead decides. Contract and per-adapter table: docs/adapters.md "Run outcome", docs/impl-notes-m3-kiro-real.md. Known limitation: lead turns still judge by rc only.
