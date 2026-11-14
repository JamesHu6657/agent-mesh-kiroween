# Agent Mesh v1 — Usage Guide

This guide describes the current implementation and its limits. Where this guide and the code
disagree, the code and `mesh help <topic>` are authoritative. For a short end-to-end
walkthrough run `mesh help quickstart`.

Agent Mesh runs on a single Linux machine under one Unix user. It is a local scheduler:
a daemon (`meshd`) owns one SQLite database, and the `mesh` CLI talks to it over a Unix
domain socket in `$MESH_HOME/run/mesh.sock`. There is no network listener, no web UI,
and no remote endpoint.

---

## 1. Workflow

The normal project loop is:

1. **Set up.** `mesh setup` initializes the home, starts the daemon and creates an inbox
   lead. For the separate steps use `mesh init` followed by `mesh daemon start`.
2. **Agents.** The user creates agents with `mesh agent create --user`. An agent is
   either `delivery=launch` (Mesh starts a headless CLI process per attempt) or
   `delivery=inbox` (an already-running process polls for work).
3. **Intake.** Any agent holding the `intake` capability, or the user, submits a project
   with `mesh intake submit --title … --goal … --criterion …`. The project gets a lead
   agent (`--lead`, else `lead.default_agent`, else rc 2 `NO_LEAD`).
4. **Lead plans.** The lead creates tasks with `mesh task create`, supplies a `spec`,
   assigns an executor, and sets dependencies (`--depends-on`).
5. **Scheduling.** Tasks move `assigned → queued → running` subject to dependencies,
   project state, priority, and per-agent plus global concurrency limits.
6. **Execution.** A launch executor runs in its own per-task directory under a wrapper
   that meshd supervises (timeout, cancel, crash recovery); an inbox
   executor calls `mesh task start`, keeps the lease alive with `mesh task keepalive`
   (or its own `mesh task heartbeat` calls), reports `progress`, then
   `mesh task submit` (or `mesh task fail`).
7. **Review.** The lead accepts (`mesh task accept`) or rejects with a reason
   (`mesh task reject --reason …`). Rejection returns the task to `rework` and then
   `queued`; after an inbox `lease_expired` the next attempt gets a fresh workspace.
8. **Completion.** The lead submits a report covering every acceptance criterion with
   `mesh project complete <prj> --report -`. The user is notified; the user does not
   re-confirm completion.

**Launch execution for vendor CLIs.** The run wrapper (`runwrap`), the
supervisor (exit handling, timeouts, cancel, post-submit grace, leftover termination), and
crash recovery (meshd adopts live runs after a restart and closes lost ones) supervise
each launched attempt. Whether a vendor adapter may launch depends on `adapter.evidence_mode` (section 10):

- `warn` (the default): a vendor CLI launches even without smoke evidence that matches the
  installed version; `mesh adapter show <id>` reports `evidence_warning` with the reason (for
  example `no_smoke_evidence` or `evidence_stale`), and `mesh doctor` shows the active mode.
- `strict` (opt-in, `mesh config set adapter.evidence_mode '"strict"' --user`): a vendor
  adapter launches only when its committed smoke evidence passes the evidence rule;
  otherwise assignment returns rc 13 `ADAPTER_UNSUPPORTED`.

Committed evidence passes for two adapters. Today that is `droid` 1.6.0 and `kiro` (kiro-cli 3.1.0),
which both passed
real end-to-end readonly tasks; `agy` and `vibe` have no smoke results, so they launch with a
warning in `warn` mode and are refused in `strict` mode. Explicit launch holds apply in both
modes. `hermes` and `openclaw` are stubs and never launch. The `fake` test double launches
only when meshd runs with `MESH_TESTING=1`. Inbox-mode execution (leases, fencing,
heartbeat, submit) is available for any agent. `docs/adapters.md` has the per-adapter
status and case results.

Unimplemented commands return rc 2 with the legacy error code
`NOT_IMPLEMENTED_IN_M1`; this means the operation is unavailable, not that an earlier
release must be installed. Internal milestone fields are diagnostic metadata.

---

### Reading and trying the examples

Commands with `$PROJECT_ID`, `$TASK_ID`, `$RUN_ID`, `$FENCE`, `$APPROVAL_ID` or
`$OP_HASH` require values returned by the preceding operation. Replace symbolic
paths and model ids before running them. Angle brackets in prose are placeholders,
not shell syntax. The human uses `--user`; an agent appends its own
`--token-file "$TOKEN_FILE"`. Never substitute another agent's credential.

Use a throwaway home to try administrative examples without changing a working Mesh:

```sh
DEMO_ROOT=$(mktemp -d /tmp/mh-demo-XXXXXX)
export MESH_HOME="$DEMO_ROOT/mesh"
mkdir -m 700 "$DEMO_ROOT/tokens"
(umask 077; printf '%s\n' 'documentation-only-passphrase' > "$DEMO_ROOT/passphrase")
export MESH_PASSPHRASE_FILE="$DEMO_ROOT/passphrase"
mesh --home "$MESH_HOME" setup --token-dir "$DEMO_ROOT/tokens" \
  --passphrase-file "$MESH_PASSPHRASE_FILE"
# Use the token_file in this response; a custom home adds a filename suffix.
```

The daemon's Unix socket, database, runs and workspaces live under this temporary
home; no TCP port is opened. Do not point a demo project at your Mesh installation
or real repository. After the demo, stop only this daemon and remove its fixtures:

```sh
mesh --home "$MESH_HOME" daemon stop --user
rm -rf -- "$DEMO_ROOT"
unset MESH_HOME MESH_PASSPHRASE_FILE DEMO_ROOT
```

## 2. Authentication

There are three kinds of credential.

### 2.1 User passphrase

`mesh init` sets one user passphrase (at least 12 characters). It is stored only as a
`scrypt` verifier (`N=2**15, r=8, p=1, dklen=64`); `K_user` (the MAC key) is derived at
request time and never stored. User commands read the passphrase from the first of:
`--passphrase-fd N`, `--passphrase-file PATH`, `MESH_PASSPHRASE_FILE`,
`MESH_PASSPHRASE`, then `getpass` on `/dev/tty`. A passphrase file must be owned by
the user and mode 0600 (else `INSECURE_PERMISSIONS`). `MESH_PASSPHRASE` is readable by
other processes of the same user, so prefer the file; meshd strips every `MESH_*`
variable from launched agents. A passphrase in argv or in the JSON stdin body is
rejected (`PASSPHRASE_SOURCE_REJECTED`). `mesh setup` runs init, daemon start and the
first lead agent in one step (README, Quick start). Note that a launched agent runs as the same user and can read a passphrase file, so with a file source an agent could also make `--user` calls such as approving its own gated actions; keep the file only where every agent is trusted, or use the terminal prompt when approvals matter.

After five failures within five minutes from peers outside a run session, user auth is
locked for five minutes (`LOCKED_OUT`). Failures are recorded as `auth.failed` events
that contain no secret.

Testing homes: `mesh init` with `MESH_TESTING=1` on a home other than
`~/.agent-mesh` stores a cheap verifier (`N=2**10`) marked `"testing": true`, so the
offline test suite does not spend minutes in scrypt. `mesh init`, meshd and `mesh doctor` refuse
testing mode on the default home, including any second path to it they can see (symlink,
or the same directory by device and inode, such as a bind mount). meshd decides its testing mode once
at startup (an explicit setting wins over `MESH_TESTING`), and outside testing mode
meshd and `mesh doctor` refuse a home whose KDF is below `N=2**15` or carries the testing
marker (`KDF_TOO_WEAK`, rc 3). In every mode they refuse stored KDF parameters that are
not exactly what `mesh init` writes (missing, garbled, out of range: `KDF_PARAMS_INVALID`,
rc 3) before running scrypt. Both checks come before the lockout check and never count
as a failed passphrase. `mesh doctor` is never healthy on a mesh.db it cannot fully
read: an open or query error, a corrupt file, a missing table or column, or no user
passphrase credential gives `DB_INTEGRITY_FAILED` (rc 1). Home paths may contain `?`, `#`,
`%` and spaces. Never use a testing home for real work; create a new one with
`mesh init` outside testing mode.

### 2.2 Agent tokens

`mesh agent token issue <name> --user` prints `mesh_at_<lookup>_<secret>` **once**.
`mesh agent token issue <name> --out FILE --user` writes it to a new mode 0600 file
instead (refused if FILE exists unless `--force`, or if it is in a place the token-file
checks below reject) and leaves it out of the output. The
database stores only the lookup id and `sha256(token)`. An agent presents the token with
`--token-file PATH` (mode 0600 required) or in `MESH_TOKEN`. A token never appears in
argv, prompts, run directories, or child environments.

`--token-file` is refused with `TOKEN_FILE_INSECURE` when its realpath is under
`workspaces.root`, `$MESH_HOME/runs`, or a registered project repo. **Keep token files
outside every workspace**, for example under `~/.config/mesh/` with mode 0600. This
keeps them out of a working tree; it does not hide them from another process of the
same Unix user.

### 2.3 Run tokens

meshd injects a short-lived **run token** into every launched agent run's environment
(`MESH_TOKEN`), together with `MESH_RUN_ID`, `MESH_PROJECT_ID`, and `MESH_SOCKET`.
Executors also receive `MESH_FENCE` and `MESH_TASK_ID`; their token is scoped to that
task and fence. Lead tokens instead bind the project and current lead assignment.
Merge-test processes receive no token. Agent run tokens expire at the run deadline
plus 300 s and are revoked when the run ends. A token is never
written to disk, argv, prompts, or logs (the wrapper redacts it from captured output).
Inbox-mode (`external`) runs have no run token; the inbox agent keeps using its own agent
token.

Authentication failures return exit 3. A credential presented from inside a run session
that is not that run's own token is rejected with exit 4
`CREDENTIAL_NOT_ALLOWED_IN_RUN`, before the credential is checked.

---

## 3. Approvals

### 3.1 What is gated

Mesh gates the operations it controls:

| Category | Gated Mesh operation | Default policy |
|---|---|---|
| `config` | config set, agent create/update/disable, token issue/revoke | `direct_only` (fixed) |
| `lead_change` | `project set-lead` | `direct_or_relayed` |
| `project_control` | `project pause`/`resume`/`cancel` | `direct_or_relayed` |
| `scope_change` | `project update` (goal / criteria) | `direct_or_relayed` |
| `permission_escalation` | assign/reassign/retry above `max_unapproved_profile`, or above a budget cap | `direct_only` by default |
| `retry_over_limit` | retry/reassign/reject when `attempt >= max_attempts` | `direct_or_relayed` |
| `delete` | `workspace delete` | `direct_or_relayed` |
| `deliver_to_base` | `project deliver` | `not_required` |
| `spend_money`, `deploy`, `delete_external`, `external_other` | external actions Mesh cannot enforce: request/check/consume only | `direct_or_relayed` |

### 3.2 Flow

1. An agent runs a gated op. meshd creates (or reuses) a `pending` approval bound to
   the op's canonical `op_hash` and returns rc 7 `APPROVAL_REQUIRED` with the approval
   id, hash, category, and expiry. An open approval is reused only while it is within
   its TTL; one past `expires_at` (not yet swept) is marked `expired` (event
   `approval.expired`, `expired_on: new_request`) and a fresh approval is created.
2. The requester re-runs the **identical** op with `--approval <apr>`. Any change in the
   operation, caller, or parameters changes `op_hash` and yields rc 12
   `OP_HASH_MISMATCH` with no state change.
3. Direct decisions are signed with `user_mac` (HMAC-SHA256 under `K_user`) and are
   shown as `trust:"direct_verified"`. Relayed decisions have no user MAC and are shown
   as `via: relayed (unverified; by <agent>)`, `trust:"relayed_unverified"`.

### 3.3 Direct, relayed and delegated

- **Direct:** `mesh approval approve <apr> --op-hash <h> --user`. The user types the
  passphrase.
- **Relayed:** an agent holding the `relay` capability runs `--relayed
  --user-statement "<verbatim words>" [--evidence-ref …]`. Mesh records the relayer and
  its credential. **Mesh cannot verify a relayed statement and advises `direct_only` for
  sensitive categories** (`spend_money`, `deploy`, `delete_external`,
  `permission_escalation`). A relayer may not approve its own request or a project it
  leads. `config` is never relayable.
- **Delegated:** the project's own lead asks you and decides with `--human-confirmed`;
  off unless `approval.mode` is `delegated`. See 3.4.

### 3.4 Delegated mode: the lead asks you

If you would rather answer your lead in chat than type a passphrase, turn on delegated
mode once:

```sh
mesh config set approval.mode '"delegated"' --user      # or: mesh setup --approval-mode delegated
```

Then:

1. A gated op creates a pending approval as usual. In delegated mode the project's inbox
   lead also gets the `approval.requested` message.
2. The lead runs `mesh approval pending --token-file lead.tok`. Each item shows what is
   asked (category, summary, operation, expiry), its `decision_channel` (`delegated`,
   or `user_only` when only you can decide it) and ready approve/reject commands with
   the caller's `--token-file`/`--home`.
3. The lead asks you through whatever channel it uses with you (chat, email, Telegram).
4. With your actual answer, the lead sets `HUMAN_REPLY` to your words and decides
   with its own token. Set `APPROVAL_ID` and `OP_HASH` from the pending response,
   and `LEAD_TOKEN` to that lead's token path. Optional evidence is supplied with
   `--evidence-ref "$MESSAGE_ID"`; square-bracket syntax is not a CLI argument:

   ```sh
   mesh approval approve "$APPROVAL_ID" --op-hash "$OP_HASH" \
     --human-confirmed "$HUMAN_REPLY" --token-file "$LEAD_TOKEN"
   # Alternative decision, not a second command to run for the same approval:
   mesh approval reject "$APPROVAL_ID" --op-hash "$OP_HASH" \
     --human-confirmed "$HUMAN_REPLY" --token-file "$LEAD_TOKEN"
   ```

What Mesh enforces: only the **current lead of the approval's project** can do this, with
its own agent token and an inbox delivery (a launch-mode lead has no channel to you).
Workers, other leads, former leads, relayers and every run token are refused, and a
handover cancels the old lead's open requests. `--human-confirmed` is required and
non-empty (max 4000 chars, redacted like every other text); it is stored as the
approval's `user_statement` and in the `approval.decided` event (`via: "delegated"`,
payload `human_confirmed`). `mesh approval show <apr>` shows who decided, when, the
operation and the quoted words; `mesh event list` has the event; `mesh audit verify`
counts delegated decisions with the relayed ones (per lead and credential). Categories whose policy is `direct_only`
(`config` always, `permission_escalation` by default) still need your passphrase; set
others to `direct_only` to keep them for yourself. Your own `--user` approvals keep
working in delegated mode.

**The lead can approve its own requests.** When the lead itself ran the gated op (for
example it raised a `spend_money` request), it may still decide that approval with
`--human-confirmed`: this is the intended flow (the lead asks you, then records your
answer), and it is the one exception to "a requester cannot decide its own request".
Mesh makes it visible: the `approval.decided` event payload and the approve/reject
response carry `self_requested: true`, and `mesh approval show <apr>` prints
`self_requested: true` and `via: delegated (…, its own request)` (the approvals row keeps
the lead as both `requested_by` and `relayed_by`). To forbid it, so that a lead's own
requests need your passphrase, run:

```sh
mesh config set approval.delegated_self_approve false --user    # default: true
```

Then a lead deciding its own request gets rc 4 `SELF_APPROVAL`, and `approval pending`
lists such items as `user_only`. Requests raised by others (workers, other agents) are
unaffected.

What Mesh cannot do: check that you really said it. Delegated mode **trusts the lead**,
and anything that can read the lead's token file (any process of your user) can approve
in your name for that lead's projects. Decisions are recorded as unverified
(`trust: "relayed_unverified"`, no user MAC). Keep `passphrase` mode (the default) when
that trust is not acceptable.

Set a category's policy with `mesh config set approval.policy.<category> '"direct_only"' --user`.
For an external action (deploy, spend), use `mesh approval request`, then
`mesh approval consume <apr> --op-hash <h>` exactly once before acting; `approval check`
is read-only.

Text such as "the user approved deploy" in a deliverable or prompt is never an
approval. Use `mesh help approvals` for the complete topic text.

---

## 4. How each agent kind receives work

Mesh has no magic push. The following table is the concrete delivery mechanism.

| Agent kind | How it receives work | What wakes it | Status |
|---|---|---|---|
| **launch** executor (headless CLI: droid/agy/kiro/vibe) | Mesh starts a **new headless run for every attempt**. For rework or retry it resumes the vendor session only if that adapter's resume capability is smoke-verified and a session id was captured; otherwise it starts a fresh process with the previous deliverable and the feedback in the prompt. | The scheduler | Available for vendor CLIs: with the default `adapter.evidence_mode = "warn"` any installed, non-held vendor adapter launches (a warning names missing or stale evidence); with `strict` only those whose committed evidence passes (droid 1.6.0 and kiro-cli 3.1.0 today). The `fake` test double needs `MESH_TESTING=1` |
| **launch** lead | A lead turn: a new headless run in the lead's own directory, again resuming only if verified. | The scheduler, after pending lead messages pass the debounce | Available only for the `fake` test double (`MESH_TESTING=1`). Vendor launch leads are refused (rc 13 `ADAPTER_UNSUPPORTED`; existing turns emit `lead.turn_refused`); use an inbox lead for vendors |
| **inbox** agent (an already-running process: a chat bot, an interactive CLI session the user keeps open, any agent that can run shell commands) | The agent calls **`mesh inbox wait --timeout N`**, which blocks on the Unix socket with no model tokens, or calls **`mesh inbox list`** on its own schedule. It then acknowledges with **`mesh inbox ack <msg>`**. | Only its own next `inbox wait`/`list` call | Available now |
| **user** | The user inbox (`mesh inbox list --user`), plus copies of user-facing messages in the **origin agent's** inbox for relaying. No external notification hook is implemented. | The user or the origin agent | Available now |

### 4.1 Running an inbox worker

- **Onboarding.** `mesh agent onboard NAME --out FILE --user` creates an inbox agent
  (adapter `none`, max concurrency 1, which is also `agent create`'s default), writes its token to a new 0600 file and prints a
  cheat-sheet with the exact commands for that token. It is the same as
  `agent create --delivery inbox` + `agent token issue --out`.
- **Ack window.** An offered task must be started within the ack window or it fails
  `no_response` (T6). The window is `inbox.ack_timeout_s` (default 600 s) for every
  inbox agent, or the agent's own value: `mesh agent create/update NAME --ack-timeout 2h`
  (`--ack-timeout default` goes back to the global setting; `agent show` prints
  `ack_timeout_s` and where it comes from). Use it for email-driven or other slow
  agents. The failure details and the lead's `task.failed` notification name the
  window, the setting that set it, and the retry / update commands.
  The window is fixed when the task is offered (recorded as `ack_timeout_s` in the
  `task.offered` message). Lowering `inbox.ack_timeout_s` or `--ack-timeout` applies to
  new offers only; raising it also extends offers already out. `config set` and
  `agent update` list (`offers_keeping_window` + a warning) the offers that keep
  their longer window. Offers made before this was recorded use the current value;
  the warning names those too.
- **Watching an inbox.** `mesh inbox list` marks pending messages `delivered`.
  A watcher or cron job that only checks for work should use `mesh inbox list --peek`
  (read-only), so the worker's own `inbox list`/`inbox wait` still sees them pending.
- **Rework.** After `task reject --reason`, `task show` (assignee and lead) has
  `last_rejection` {reason, rejected_by, rejected_at, attempt}. An inbox assignee also
  gets a `task.rejected` message with the reason, and the next `task.offered`
  message carries `last_rejection`.
- **Lease.** `task start` prints `lease_ttl_s`, `lease_expires_at`,
  `heartbeat_every_s` (a third of the TTL), `timeout_s` and `deadline_at`; the
  assignee sees the same under `live_run` in `task show`. An inbox run has no
  meshd-enforced deadline (`deadline_at` is null, `deadline_enforced` false): it ends
  when the worker submits or fails it, or when no heartbeat arrives within
  `lease.ttl_s` (the task fails `lease_expired`). `timeout_s` is the task's budget.
  Every lease the sweep expires records a `task.lease_expired` event (run, fence,
  holder, task status) shown in `task show` history; the lead is told by the
  `task.failed` message, or by `task.lease_expired` itself when the task was no
  longer running and so did not fail.
- **One session per run (on by default).** Several worker processes may share an
  agent token. `task start` records a client session on the run: `MESH_SESSION` if set
  (any short id), otherwise a generated `ses_...` id. The response carries `session`,
  an `export MESH_SESSION=...` line (printed in text mode) and a `keepalive` command that
  already includes it. Heartbeat / keepalive / progress / submit / fail of that run
  from a different session is refused (exit 9 `FENCE_MISMATCH`, `details.reason:
  "session_mismatch"`), and so is one naming no session (`"session_missing"`; the error
  says which `export` to run). Runs started without a session (older clients, launch
  runs) are not checked.
  A submit or heartbeat for a run that was already submitted says when, by whom,
  with which request (and session), so a second session sees what happened.
- **Lists and batches.** `mesh task list --status submitted,accepted` takes a comma
  list; its text form shows id, status, assignee, age, time in the current status,
  attempt and title. `mesh task merge --all-accepted --project P` (lead) merges the
  project's accepted code tasks one at a time in creation order, each an ordinary
  `task merge` with its full gate, stops at the first failure and reports merged /
  failed / not attempted plus `next_step` (in text mode, one stderr line per task). A
  failed merge (`TESTS_FAILED`, `MERGE_CONFLICT`) leaves the integration worktree clean
  and the project merge lock free, so the next run continues with the rest;
  `MERGE_HELD` keeps the lock until the held merge test run is resolved (see its message).
- **Scheduler errors.** A failing scheduler step is logged with its traceback in
  `logs/meshd.log`, counted, and does not stop the other steps; `mesh daemon status`
  (`scheduler.sweep_errors`, `last_sweep_error`, per step) and `mesh doctor`
  (`scheduler_errors` warning) show them since meshd started.
- **Keepalive.** `mesh task keepalive TSK --run R --fence F [--every N]` heartbeats in a
  loop (default every `heartbeat_every_s`) and logs each beat to stderr. Run it in the
  background after `task start`. It exits 0 with the final task state when the run
  ends normally (submit, fail, cancel) and 9 when the lease was lost (`LEASE_EXPIRED`)
  or the task was restarted with a newer fence (`FENCE_MISMATCH`). SIGINT/SIGTERM stop
  it at once, also while a heartbeat is waiting on meshd (exit 0, `ended: stopped`, no
  traceback); the lease then runs out after `lease.ttl_s`. Each daemon call it makes
  times out after 10 s and counts as a failed beat, retried until the lease could expire.
- **Roster for leads.** `mesh agent list` / `mesh agent show NAME` work with an agent
  token for an enabled agent with `can_lead` or the current lead of an active project.
  Leads get the roster view: id, name, adapter, delivery, can_lead, max concurrency,
  disabled, models and efforts, ack window. No credentials, adapter options or binary
  paths. Every other agent keeps the user-only rule, and all agent changes stay user-only.
- **Capabilities.** An agent gets `["intake"]` when `agent create` names no
  `--capability`. Each `--capability` replaces that default (a warning says so when
  intake is dropped); `--no-intake` drops it on purpose.
- **Ack window warning.** `agent create`/`onboard` of an inbox agent without
  `--ack-timeout` warns when the global `inbox.ack_timeout_s` is under 1 h.
- **Deliverables.** `task submit --deliverable` takes a file path, `-` for stdin, or
  inline JSON. A value that is neither an existing file nor JSON is reported as
  `file not found: PATH (and not valid inline JSON)`.

**Mesh cannot push into an interactive TUI.** Injecting text into a running
interactive session (ACP or JSON-RPC push) is not implemented.

---

## 5. Security model and honest limits

Read this section before relying on Mesh for isolation.

**Mesh is not a security sandbox. Mesh does not sandbox external CLIs.** meshd, the
CLI, and every agent Mesh launches or talks to run as the same Linux user. Any process
of that uid can read every file the user can read, write every file the user can write
(including Mesh's own code unless the user made it read-only), and, on a box with no
yama ptrace restriction, read other processes' `/proc/<pid>/environ` and memory.
Without OS-level separation, which v1 does not use, Mesh cannot isolate agents from each
other or from itself. Vendor cwd/workdir flags are conventions, not jails: a CLI
launched with edit or shell permission can write outside its workspace, and vendor
sandbox flags are vendor features that Mesh does not guarantee.

For real work: Never keep the passphrase in a file and keep token files outside every workspace.
The throwaway examples above use only a disposable dummy passphrase, not a real account credential.

**Same-uid limits.** These are properties of the operating system, not bugs:

- **A same-uid process can read other agents' Mesh credentials (and forge relayed
  approvals) unless OS-level separation is used.** That includes another agent's token
  file (for example a relayer's token) and run tokens in another process's environment
  or memory. `setsid` plus a double fork, or a cleared environment, defeats the
  best-effort run-session binding.
- A same-uid process can read or modify `mesh.db` directly, kill meshd, or send wrong
  passphrases from outside a run to trigger the lockout (denial of service).
- A same-uid process can copy the scrypt verifier and guess the passphrase offline;
  scrypt makes each guess expensive, and a long passphrase is the only defense.
- **A same-uid process can modify Mesh's own code or read the passphrase or K_user from
  memory during a direct request, after which forged direct approvals carry valid MACs
  and audit verify cannot be trusted.**
- In particular, a **launched executor cannot be prevented from stealing credentials**;
  it can read any file or process the user can. Run-session binding stops the casual
  case (an executor using another agent's token through the `mesh` CLI), not a
  deliberate same-uid attacker.

What meshd **does** enforce against a caller that uses the `mesh` CLI or socket with the
credential it was given: every request carries a real credential; authorization is by
principal, capability, scope, and fence; a requester cannot approve its own request; a
relayer cannot approve for a project it leads; an executor run token cannot approve,
relay, create tasks, or act outside its task; stale writes are rejected by fencing; and
a process inside a run may use only that run's token.

**Known limitation: idempotent replay and changed authority.** A request repeated
with the same idempotency key returns the stored response before authorization is
checked again. If the caller's authority changed in between (for example project
leadership moved to another agent), the replay still returns the old success with
`idempotent_replay: true`. Nothing is executed again; only the earlier result is
returned. Re-checking authority before a replay is planned.

**What `mesh audit verify` detects.** It detects DB-only tampering: forged direct
approvals and user events written by editing `mesh.db`, *provided* the forger has not
captured the passphrase or `K_user` and has not modified Mesh's code. It also checks the
event hash chain (which detects accidental corruption, not deliberate rewrites) and
lists relayed decisions for review.

**Mesh does not claim that executors cannot steal credentials, and does not claim that
audit verify detects forgeries by a process that modified Mesh's code or captured the
passphrase.** It cannot detect either.

**Recommendations:**

- **Keep token files outside every workspace; prefer the terminal prompt for real approvals.**
  The checklist's passphrase file `P` is a test fixture only. Real users type the
  passphrase on `/dev/tty`; use mode 0600 token files under `~/.config/mesh/`.
- **Mesh recommends a read-only install outside every workspace**
  (`chmod -R a-w <install_dir>`), and it refuses to run when the install root is inside
  `workspaces.root`, `$MESH_HOME/runs`, or a project repo (`INSTALL_PATH_INSECURE`).
  This stops only tools that respect file modes; the same uid can `chmod` it back.
- Set sensitive categories to `direct_only`, and keep the event log for review.

---

## 6. Practical notes

- Exit codes are stable: 0 OK, 2 usage/validation, 3 unauthenticated, 4 forbidden,
  5 not found, 6 conflict, 7 approval required, 8 wait timeout, 9 stale, 10 daemon
  unavailable, 11 busy, 12 approval invalid, 13 adapter unavailable, 14 merge failed.
  `mesh help exit-codes` prints the full table.
- Default output is one JSON envelope. Use `--format text` for tables; columns are cut and
  padded by display width, so CJK (East Asian wide) text stays aligned.
- `mesh config set KEY VALUE` takes VALUE as JSON (`600`, `null`, `'"text"'`,
  `'["a"]'`). String settings (`lead.default_agent`, `lead.profile`, `workspaces.root`,
  `adapter.evidence_mode`, `approval.mode`, `approval.policy.*`) also take a bare word
  (`config set lead.default_agent lead1`); for other settings a value that is not JSON
  is refused with the quoted form to use.
- Every mutating command accepts `--idempotency-key` and `--dry-run`.
- Use `mesh help auth`, `mesh help approvals`, `mesh help workflow`, and
  `mesh help adapters` for the CLI's own topic text.
- A run adopted after a meshd restart is held (run open, lease kept, nothing
  signalled) when its recorded group members (`seen.json` in the run directory) are
  unusable and its process group or session is still occupied; meshd logs this and
  records one `run.seen_unusable {run_id, reason}` event. The hold is also stored in
  the database, so deleting `seen.json` (even across a meshd restart) does not lift
  it. Writing a valid file lifts the hold on its own. Otherwise, once you have checked the leftover processes yourself,
  `mesh run release <run-id> --force-seen-unusable --user` closes the run as `lost` and
  releases its lease without signalling any process (event `run.seen_release_forced`).
  It is user-only and refuses runs that are not held this way.
- A `merge_test` run that the merge close gate keeps open (its group is still occupied
  after the test step) holds `merge:<prj>` and leaves the task `merging` until the next
  boot. Once you have checked the leftover processes yourself,
  `mesh run release <run-id> --force-merge-held --user` closes that run as `lost`,
  releases `merge:<prj>` and records `run.merge_release_forced`; the task is then decided
  as boot recovery would (reconciled `merged` if its merge commit is in integration,
  otherwise `merge_failed` with reason `interrupted`). It is user-only, only for a
  `merge_test` run that is still open (otherwise rc 6 `not_merge_test` / `run_not_open`),
  and refused (rc 6 `RUN_STILL_ACTIVE`, reason `wrapper_alive`) while the run's wrapper
  process is alive. No process is signalled.

---

Inbox workers should send a heartbeat while the model or tests are working, not only
once before submission. `task start` returns `lease_ttl_s`; use an interval well
inside it (at most 40 seconds for the default 120-second lease). Every write uses
the exact `run_id` and `fence` from that start. Stop heartbeats after submit/fail or
a stale/terminal response. See [inbox-agent.sh](examples/inbox-agent.sh).

## 7. Repo projects and code tasks

Both `intake submit` and `project create` accept `--repo PATH --base-branch B
--test-command JSON_OR_NONE`. PATH must name the repo's top-level directory, and B
must be a local branch. Set `REPO` to a separate, clean test repository and use
`lead` only if that is your configured inbox lead. For example:

```sh
mesh intake submit --title "Calculator" --goal "Fix addition" --criterion "Tests pass" --lead lead --repo "$REPO" --base-branch main --test-command '["python3","-m","unittest"]' --token-file "$LEAD_TOKEN"
```

`--test-command` is required. Use the literal `none` to explicitly record
`tests:"none"`; otherwise supply a JSON argv array, not a shell command string.
`project show` reports the repo, base branch/commit, integration branch and tests.
Mesh creates `mesh/<prj>/integration` at the base commit and checks it out under
`<workspaces.root>/<prj>/integration/`. An existing integration branch is rc 6.
A dry run validates without creating git state. If worktree creation fails after
branch creation, the error names the branch and Mesh keeps it.

**Configure a repo-local git identity before merging.** Mesh never sets git identity
or config and never pushes. Merge commits use the repo's configured `user.name` and
`user.email`. Missing identity produces rc 14 `GIT_COMMIT_FAILED`, with reason
`commit_failed` and git stderr, without changing integration HEAD. Git may refuse
identity at the merge preparation step, before the commit command itself.

A repo project's tasks default to `kind=code`; use `--kind general` for a plain
directory task. `--kind code` on a non-repo project is rc 2. At first start, each code
task gets its own worktree at `<workspaces.root>/<prj>/tasks/<tsk>/`, on
`mesh/<prj>/<tsk>`, forked from integration. Commit on that branch, do not push or
switch branches. Rework/retry/reassign normally reuse it. After an inbox lease
expires, retry without `--reuse-workspace` creates `<tsk>.a<n>/` on
`mesh/<prj>/<tsk>-a<n>` from the old tip; the old worktree and branch stay.

On submit Mesh records `deliverable.git`: HEAD, branch, base commit, dirty flag,
dirty files, diff stat and commit count. It does not auto-commit. Collection errors
are reported in `git.error` without blocking submission. Large lists/stat output
are bounded and marked as truncated.

## 8. Merge, deliver and cleanup

```sh
mesh task accept "$TASK_ID" --token-file "$LEAD_TOKEN"
mesh task merge "$TASK_ID" --token-file "$LEAD_TOKEN"
mesh project deliver "$PROJECT_ID" --token-file "$LEAD_TOKEN"
mesh workspace prune --project "$PROJECT_ID" --dry-run --token-file "$LEAD_TOKEN"
mesh workspace prune --project "$PROJECT_ID" --token-file "$LEAD_TOKEN"
# Accepted work that landed through another task's merge: close it as superseded.
mesh task close "$TASK_ID" --superseded-by "$OTHER_TASK_ID" --reason "landed in $OTHER_TASK_ID" --token-file "$LEAD_TOKEN"
# Alternative to prune: delete one inactive workspace, retaining its branch.
# Agents must first obtain the delete approval, then add --approval "$APPROVAL_ID".
mesh workspace delete "$TASK_ID" --token-file "$LEAD_TOKEN" --approval "$APPROVAL_ID"
```

These are lead operations, not worker instructions. `project deliver` changes the
local base branch; run it only when authorized, never merely to inspect a task.
Prune and delete remove workspaces; use throwaway fixtures when trying them.
`task merge` accepts an accepted code task, or retries `merge_failed`, with no
task lease. A `merge:<prj>` lease serializes merges; another merge is rc 11
`CONCURRENCY_LIMIT`. A submitted task is rc 6 `INVALID_TRANSITION`.
Dirty task/integration worktrees are refused (rc 14 `WORKTREE_DIRTY` /
`INTEGRATION_DIRTY`). Conflicts list the paths (`MERGE_CONFLICT`); Mesh aborts and
verifies restoration rather than overwriting conflict content.

The pipeline prepares a non-fast-forward merge, runs the project's tests in the
integration worktree under supervision, then commits. Tests failing or timing out
give rc 14 `TESTS_FAILED`, a redacted log at `$MESH_HOME/merges/<mrg>/test.log`,
and a log tail. The message and details (`failing_tests`, `failing_tests_total`,
`failing_tests_truncated`, `log_path`) list up to 20 failing test ids, parsed from
unittest `FAIL:`/`ERROR:` and pytest `FAILED` lines. `task merge` waits for the tests;
in `--format text` it prints `mesh: merging TSK: running the project's tests if any
(log: PATH)...` to stderr once the merge has started. (`--no-wait` was left out: the
merge result, idempotency record and lead notification are tied to the request; a
second terminal can follow `task show` (status `merging`, `merge.mrg`) and the log.) A successful merge records its commit and `tests:"passed"` or
`"none"`; a branch already integrated is a no-op that still runs the tests (failing tests
make it `merge_failed`) and records the commit that really merged it as `merge_commit`
(never the task's tip). Failures notify
the lead. A client disconnect does not cancel a merge. Boot recovery aborts an
unfinished merge or reconciles an already-created matching merge commit. Before the
merge commit is made, `(pre_head, tip)` is written to `$MESH_HOME/merges/<mrg>/merge_intent.json`
and fsynced (no intent, no commit); recovery recognises the merge commit by those
parents (a commit above `pre_head` with parents exactly `[pre_head, tip]`), not by its
message, so a `commit-msg` hook or a task branch that moved on cannot hide it. A run
without an intent file (made before intents existed) falls back to the old rule: a merge
commit on HEAD whose second parent is the current branch tip and whose message names the
task. `doctor --check-invariants` (I9) also reports the reverse case: a task that is not
`merged` (for example cancelled after an unrecognised merge) whose work is already in the
integration branch, because `project deliver` would hand it to the base branch.
`project cancel` during a merge is rc 6 with reason `merge_in_progress`.

`task close TSK --superseded-by TSK2` (lead or user) is for an `accepted` or
`merge_failed` code task whose work reached integration through TSK2's merge. TSK2
must be `merged` and in the same project, and TSK must have no open dependents
(re-point them with `dep remove`/`dep add`). TSK ends in the terminal `cancelled`
state with a `task.closed` event (`superseded_by`, `from_state`, optional `--reason`);
`task show` prints `superseded_by`. The branch and worktree are kept. No new task
state is added, so project completion, deliver, doctor and prune treat it like any
cancelled task.


The test command runs in the integration worktree. HEAD, `MERGE_HEAD` and the index are
compared before and after it: a test that stages anything (`git add`, `git add -N`),
commits or aborts the merge fails as `TESTS_FAILED` whatever its exit code
(`details.git_changes`), so nothing it staged enters the merge commit. Afterwards Mesh
puts the worktree back to the index: untracked, non-ignored files (coverage, junit,
build output) are removed and tracked files the test rewrote are restored
(`details.cleanup`), so a test never leaves the project `INTEGRATION_DIRTY`. Ignored
files are left alone. Mesh never resets or rewinds the integration branch: a test that
committed is reported (`head_moved`, `restored: false`), and until that work is dealt
with `project deliver` refuses (below). When testing Mesh itself, use only `/tmp/mh-*` homes and fixture
repos, never a home or repo inside the Mesh checkout; test commands must not leave
untracked files in the checkout. `bash scripts/test.sh` uses isolated fixtures.

`project deliver` fast-forwards the base branch only if it is an ancestor of
integration. A checked-out base must have a clean worktree; otherwise Mesh uses a
compare-and-swap ref update. Divergence is rc 14 `NOT_FAST_FORWARD`, with no base
change. It never pushes, rebases or forces. The `deliver_to_base` policy defaults to
`not_required`; if tightened, the lead needs approval and must re-run with
`--approval <apr>`. While a merge is running on the same repository (the project's own
`merge:<prj>` lease or a `merging` task, or the same for any other project sharing the
repo path, compared by realpath), `project deliver` is rc 11 `MERGE_IN_PROGRESS`
(`details.reason: "merge_in_progress"`, with the merging task and run), before approval
and with no change; retry after the merge finishes. If the integration branch carries
work of a code task of the project that is not `merged` (the reverse I9 case above, or a
test command that committed during its merge), `project deliver` is rc 6
`INVALID_TRANSITION` (`details.reason: "integrated_unmerged_work"`, with the tasks and
merge commits), before approval and with no change: merge that task again (a no-op merge
that re-runs the tests) or repair the integration branch yourself first.

Nothing is deleted automatically. Prune removes only merged code worktrees, or
cancelled ones with no commits beyond their base. It uses non-forced
`worktree remove` and `branch -d`, never `-D`; dirty/unsafe candidates are skipped, and so is everything while a merge runs on the same
repository (`skipped[].reason: "merge_in_progress"`).
Agents must name `--project` and lead that project; only the user may prune unscoped.
`workspace delete` requires the `delete` approval for agents (rc 7), or direct user
authentication. A live task lease blocks it. After checking containment under
`workspaces.root`, it removes the directory and prunes git's worktree metadata,
but **keeps the branch, including unmerged commits**.

## 9. Launch-mode lead turns

Only the `fake` test double can be a launch-mode lead, and only in testing mode.
All vendor launch leads are refused, even with executor launch evidence. Creation or
set-lead returns rc 13; an existing disabled launch lead keeps its messages pending
and emits `lead.turn_refused` with legacy reason `vendor_lead_m3_lite` once per
assignment. Use `delivery=inbox` for a real lead. No vendor lead inference starts.

For an active project, pending `lead_wake` messages trigger a turn after
`lead.wake_debounce_s` (default 5 s), or when at least 20 are pending. The
`leadturn:<prj>` lease prevents overlapping turns for the project. Up to 50
messages fit in a turn; omitted messages stay pending. The prompt names omitted
context and retains the authority rules and completion format. The lead runs in
`lead/<agent_id>/`, using `lead.profile` (default `readonly`) and
`lead.turn_timeout_s` (default 1800 s). A repo project's separate `repo/` is
detached and refreshed to integration when clean, never an executor worktree.
Approval decisions addressed to the current launch-mode lead also enqueue a wake,
while retaining inbox delivery. Async merge success (`task.merged`) and failure
(`merge.failed`) wake the lead too. These wakes use the same debounce and lease guards.

A lead run token authorizes only its own project while its assignment is current.
It cannot approve anything, read global approvals, or act on another project,
even if the agent has relay capability or leads both projects. It may request
approval for its own project. `set-lead` terminates the old turn and transfers its
unacked messages; during termination its token gets rc 9 `LEAD_SUPERSEDED`, then
rc 3 after revocation. This supersede check also survives daemon restart.

The final output may contain a fenced `mesh-actions` JSON array:

````text
```mesh-actions
[{"cmd":"task.create","args":{"title":"Fix addition","spec":"Commit the fix","assignee":"ibx"}},
 {"cmd":"task.retry","args":{"task":"tsk_…"},"approval":"apr_…"}]
```
````

Allowed cmds: `task.create`, `task.assign`, `task.reassign`, `task.accept`,
`task.reject`, `task.retry`, `task.escalate`, `task.cancel`, `task.merge`,
`dep.add`, `dep.remove`, `project.note`, `project.complete`, `project.deliver`,
`approval.request`. The last valid array block wins. More than 50 actions refuses
the whole block; invalid items fail individually. Optional top-level `approval`
is the action equivalent of `--approval`, not an approval bypass.

After exit 0, meshd executes actions in order under the credential-backed lead
principal before revoking the token, with keys `act:<run>:<index>`. Handlers see
`via:"run_token"`; `lead.action_result` events use `via:"lead_turn"` and correlate
dispatched request ids. `task.merge` first reports `status:"queued"` and starts
asynchronously after the turn-close transaction commits; merge events give its
actual outcome. Boot re-queues committed requests that never reached merge start;
unrecoverable historical rows get a terminal `RECOVERY_FAILED` action result.
`project.complete` may ignore only the verified calling lead turn's own
`leadturn:<prj>` lease. Other leases, non-terminal tasks and open approvals still
block completion with rc 6, including completion requested by a different caller.

Exit 0 acknowledges the carried messages. Non-zero exit, timeout or spawn failure
returns them to pending with backoff (30, 120, 600 s); after
`outbox.max_attempts` (default 3) they become dead and `lead.unreachable` reaches
the user and origin agent. With three attempts only the first two delays apply.
Action failures enter the next prompt as `lead.action_failed`, but do not trigger
a turn by themselves, avoiding an automatic failure loop.

Session ids are captured for executor runs and lead assignments. Resume is used
only when an id exists and the adapter reports a supported, smoke-verified resume
capability. Tests exercise this with the fake's `resume_verified=true` option; that is not
evidence for a vendor. Without verification the next run is fresh. Every run
still gets the full prompt. After rejection, subsequent attempts include the latest
rejection reason and previous deliverable summary, even after a rework attempt fails
or times out and is retried. Correctness never depends on resume.

## 10. Vendor evidence: probe, smoke and launch

**Evidence mode.** Everything below describes `adapter.evidence_mode = "strict"`. In the
default `warn` mode the same evaluation runs, but a failing result (other than a launch hold
or a stub adapter) is reported as a warning instead of refusing the launch.

**Evidence-driven launch.** For leads, every vendor launch lead is refused, even with launch evidence;
its legacy machine reason remains `vendor_lead_m3_lite`.

**Strict executor evidence.** meshd decides `launch_enabled` per vendor at runtime; nobody
sets it by hand. It is true only if the latest probe row for the agent's binary has
version V, the latest results file for the adapter in `tests/smoke/results/` has version
V, that file validates, and its S-X-1 and S-X-6 cases pass. Otherwise launch commands
return rc 13 `ADAPTER_UNSUPPORTED` with `details.reason` (`no_probe_version`,
`no_smoke_evidence`, `evidence_stale`, `evidence_invalid`, `launch_cases_failed`, or
`launch_held` for an adapter on an explicit hold).
meshd reads this evidence from the checkout's `tests/` tree at runtime (`tests/smoke/results/`,
and it hashes `tests/fixtures/real/`); package installs therefore need `MESH_EVIDENCE_ROOT` pointing at a checkout for
strict evidence checking. Upgrading a vendor CLI makes its evidence `evidence_stale` until new results for
the new version are promoted. `mesh adapter show <id>` prints the status,
`launch_enabled`, `launch_milestone`, `launch_reason`, `results_file`, and per-capability
`verification`, `verified_at` and `verified_version`.

**Probe tier (read-only).** `MESH_PROBE=1 python3 -m unittest tests.probe.test_probe`
runs `--version`, `--help` and model-list commands (`MESH_PROBE_ADAPTERS` limits the
adapters) and stages captures for review (`MESH_PROBE_CAPTURE`). It starts no model
run, but `agy models` and `kiro-cli chat --list-models` contact vendor servers.

**Smoke tier (spends vendor quota; needs consent).** Smoke tests skip and spawn nothing
unless all of these are set: `MESH_SMOKE=1`; the adapter in both `MESH_SMOKE_ADAPTERS`
and `MESH_SMOKE_CONSENT` (optionally `MESH_SMOKE_CONSENT_REF` naming who agreed and when; it is recorded in the results file);
`MESH_SMOKE_MAX_RUNS` (required, enforced); an explicit model
`MESH_SMOKE_MODEL_<ADAPTER>` (`auto` is refused); and `MESH_SMOKE_OUT` under
`/tmp/mh-*`, the only place results are written. `MESH_SMOKE_CASES` and
`MESH_SMOKE_TIMEOUT_S` narrow a run. Stage it: run the first case alone, check the
vendor's usage before and after, then run the rest. The real end-to-end tests
(`tests.smoke.test_e2e`) need `MESH_SMOKE_E2E=<adapter>`, and `MESH_SMOKE_E2E_CODE=1` selects the code-task demo
instead of the readonly run. Neither tier is part of `bash scripts/test.sh`; the default
suite stays vendor-free.

**Promotion is coordinator-only.** Staged captures and results become evidence only
when the coordinator promotes them: `python3 -m tests.tierlib promote --from <staging>
--to <repo>`. Promotion writes new files with a `{command, captured_at_utc, sha256}`
sidecar and refuses existing paths and `synthetic_*` names. Committed results and real
fixtures are never edited. `python3 -m tests.smoke.reevaluate` re-grades a committed
results file offline into a new staging directory.

**What the evidence does not change.**

- Mesh does not sandbox external CLIs. A passing smoke case is evidence about a readonly
  run, not a permission guarantee.
- Vendor launch leads are refused (section 9).
- The idle timeout stays off: `run.idle_timeout_s` defaults to `0`, and it is not
  validated against vendor output.
- Resume is used only for a resume capability that is `supported: "yes"` and smoke
  verified; every vendor still reports `supported: "unknown"`, so vendor runs start
  fresh with the full prompt.
- **droid `dev` is the profile that commits locally.** In the recorded code-task demo, droid `dev`
  (`--auto medium`) edited and committed in its own task worktree; Mesh never
  auto-commits, then merged the task branch into integration with a two-parent merge
  commit, with no remote and nothing pushed. kiro and agy `dev` stay
  `PROFILE_UNSUPPORTED`.
