# Agent Mesh v1 — Usage Guide

Status: updated for gate M2 (plan §16). It describes what Mesh does **today**
on this branch, including what is left for later milestones. Where this guide and the code
disagree, the code and `mesh help <topic>` are authoritative.

Agent Mesh runs on a single Linux machine under one Unix user. It is a local scheduler:
a daemon (`meshd`) owns one SQLite database, and the `mesh` CLI talks to it over a Unix
domain socket in `$MESH_HOME/run/mesh.sock`. There is no network listener, no web UI,
and no remote endpoint.

---

## 1. Workflow

The normal project loop is:

1. **Init and start.** `mesh init` (passphrase on `/dev/tty`), then `mesh daemon start`.
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
   executor calls `mesh task start`, then `mesh task heartbeat`/`progress`, then
   `mesh task submit` (or `mesh task fail`).
7. **Review.** The lead accepts (`mesh task accept`) or rejects with a reason
   (`mesh task reject --reason …`). Rejection returns the task to `rework` and then
   `queued`; after an inbox `lease_expired` the next attempt gets a fresh workspace.
8. **Completion.** The lead submits a report covering every acceptance criterion with
   `mesh project complete <prj> --report -`. The user is notified; the user does not
   re-confirm completion.

**Launch execution for vendor CLIs.** M1c built the run wrapper (`runwrap`), the
supervisor (exit handling, timeouts, cancel, post-submit grace, leftover termination), and
crash recovery (meshd adopts live runs after a restart and closes lost ones). Whether a
vendor adapter may launch depends on `adapter.evidence_mode` (section 10):

- `warn` (the default): a vendor CLI launches even without smoke evidence that matches the
  installed version; `mesh adapter show <id>` reports `evidence_warning` with the reason (for
  example `no_smoke_evidence` or `evidence_stale`), and `mesh doctor` shows the active mode.
- `strict` (opt-in, `mesh config set adapter.evidence_mode '"strict"' --user`): a vendor
  adapter launches only when its committed smoke evidence passes the evidence rule;
  otherwise assignment returns rc 13 `ADAPTER_UNSUPPORTED`.

Committed evidence passes for two adapters. Today that is `droid` 1.6.0 and `kiro` (kiro-cli 3.1.0),
which both passed
the real end-to-end run N13; `agy` and `vibe` have no smoke results, so they launch with a
warning in `warn` mode and are refused in `strict` mode. Launch holds (L11) apply in both
modes. `hermes` and `openclaw` are stubs and never launch. The `fake` test double launches
only when meshd runs with `MESH_TESTING=1`. Inbox-mode execution (leases, fencing,
heartbeat, submit) is available for any agent. `docs/adapters.md` has the per-adapter
status and case results.

Commands that are scheduled for later milestones return rc 2 `NOT_IMPLEMENTED_IN_M1`
with `details.milestone` set to the milestone that builds them.

---

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

Testing homes (m3-test-kdf): `mesh init` with `MESH_TESTING=1` on a home other than
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

`mesh agent token issue <name> --user` prints `mesh_at_<lookup>_<secret>` **once**. The
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
   id, hash, category, and expiry.
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
4. With your answer, the lead decides with its own agent token and quotes you:

   ```sh
   mesh approval approve apr_123 --op-hash sha256:... \
     --human-confirmed "James 08:03: yes, deploy it" [--evidence-ref msg-id] --token-file lead.tok
   mesh approval reject  apr_123 --op-hash sha256:... --human-confirmed "James: no" --token-file lead.tok
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
| **launch** executor (headless CLI: droid/agy/kiro/vibe; in M1 only the `fake` test double under `MESH_TESTING=1`) | Mesh starts a **new headless run for every attempt**. For rework or retry it resumes the vendor session only if that adapter's resume capability is smoke-verified and a session id was captured; otherwise it starts a fresh process with the previous deliverable and the feedback in the prompt. | The scheduler (T5) | Available for vendor CLIs: with the default `adapter.evidence_mode = "warn"` any installed, non-held vendor adapter launches (a warning names missing or stale evidence); with `strict` only those whose committed evidence passes (droid 1.6.0 and kiro-cli 3.1.0 today). The `fake` test double needs `MESH_TESTING=1` |
| **launch** lead | A lead turn: a new headless run in the lead's own directory, again resuming only if verified. | The scheduler, after pending lead messages pass the debounce | Available in M2 for the `fake` test double only (`MESH_TESTING=1`). Vendor adapters cannot be launch-mode leads until M3 evidence enables them (rc 13 `ADAPTER_UNSUPPORTED`; existing turns refused with `lead.turn_refused`); until then use a `delivery=inbox` lead for vendors |
| **inbox** agent (an already-running process: a chat bot, an interactive CLI session the user keeps open, any agent that can run shell commands) | The agent calls **`mesh inbox wait --timeout N`**, which blocks on the Unix socket with no model tokens, or calls **`mesh inbox list`** on its own schedule. It then acknowledges with **`mesh inbox ack <msg>`**. | Only its own next `inbox wait`/`list` call | Available now |
| **user** | The user inbox (`mesh inbox list --user`), plus copies of user-facing messages in the **origin agent's** inbox for relaying. An optional `notify.command` hook is planned for M4. | The user or the origin agent | Available now |

**Mesh cannot push into an interactive TUI.** Injecting text into a running
interactive session (ACP or JSON-RPC push) is research for a later milestone, not v1.

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

- **Never keep the passphrase in a file and keep token files outside every workspace.**
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
- Default output is one JSON envelope. Use `--format text` for tables.
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

## 7. Repo projects and code tasks

Both `intake submit` and `project create` accept `--repo PATH --base-branch B
--test-command JSON_OR_NONE`. PATH must name the repo's top-level directory, and B
must be a local branch. For example (append your normal agent or user authentication):

```sh
mesh intake submit --title "Calculator" --goal "Fix addition" --criterion "Tests pass" --lead lead1 --repo /absolute/path/to/repo --base-branch main --test-command '["python3","-m","unittest"]'
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
mesh task accept <tsk>
mesh task merge <tsk>
mesh project deliver <prj>
mesh workspace prune --project <prj> --dry-run
mesh workspace prune --project <prj>
mesh workspace delete <tsk>
```

These examples require the current lead's credential or user authentication.
`task merge` accepts an accepted code task, or retries `merge_failed` (T17), with no
task lease. A `merge:<prj>` lease serializes merges; another merge is rc 11
`CONCURRENCY_LIMIT`. A submitted task is rc 6 `INVALID_TRANSITION`.
Dirty task/integration worktrees are refused (rc 14 `WORKTREE_DIRTY` /
`INTEGRATION_DIRTY`). Conflicts list the paths (`MERGE_CONFLICT`); Mesh aborts and
verifies restoration rather than overwriting conflict content.

The pipeline prepares a non-fast-forward merge, runs the project's tests in the
integration worktree under supervision, then commits. Tests failing or timing out
give rc 14 `TESTS_FAILED`, a redacted log at `$MESH_HOME/merges/<mrg>/test.log`,
and a log tail. A successful merge records its commit and `tests:"passed"` or
`"none"`; a branch already integrated is a no-op and skips tests. Failures notify
the lead. A client disconnect does not cancel a merge. Boot recovery aborts an
unfinished merge or reconciles an already-created matching merge commit.
`project cancel` during a merge is rc 6 with reason `merge_in_progress`.

Test commands must leave no untracked, non-ignored artifacts in the integration
worktree, or later merges report `INTEGRATION_DIRTY`. Mesh does not clean those
files for you. When testing Mesh itself, use only `/tmp/mh-*` homes and fixture
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
and with no change; retry after the merge finishes.

Nothing is deleted automatically. Prune removes only merged code worktrees, or
cancelled ones with no commits beyond their base. It uses non-forced
`worktree remove` and `branch -d`, never `-D`; dirty/unsafe candidates are skipped, and so is everything while a merge runs on the same
repository (`skipped[].reason: "merge_in_progress"`).
Agents must name `--project` and lead that project; only the user may prune unscoped.
`workspace delete` requires the `delete` approval for agents (rc 7), or direct user
authentication. A live task lease blocks it. After checking containment under
`workspaces.root`, it removes the directory and prunes git's worktree metadata,
but **keeps the branch, including unmerged commits** (Q7).

## 9. Launch-mode lead turns

M2 exercises this path only with `fake` in testing mode, not real inference.
Vendor launch leads remain disabled until M3 evidence enables them. Creation or
set-lead refuses them with rc 13; the scheduler refuses an existing disabled
launch lead once per assignment (`lead.turn_refused`), leaving messages pending.
**In M3-lite every vendor launch lead is refused, even with launch evidence**
(decision D4): no vendor lead turn starts, its messages stay pending, and meshd logs
`lead.turn_refused` with reason `vendor_lead_m3_lite` once per assignment. droid and
kiro can be launch executors but not launch leads; use a `delivery=inbox` lead.

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
capability. M2 tests this only with the fake's `resume_verified=true` option, never
as evidence for a vendor. Without verification the next run is fresh. Every run
still gets the full prompt. After rejection, subsequent attempts include the latest
rejection reason and previous deliverable summary, even after a rework attempt fails
or times out and is retried. Correctness never depends on resume.

## 10. Vendor evidence: probe, smoke and launch (M3)

**Evidence mode.** Everything below describes `adapter.evidence_mode = "strict"`. In the
default `warn` mode the same evaluation runs, but a failing result (other than a launch hold
or a stub adapter) is reported as a warning instead of refusing the launch.

**Evidence-driven launch.** meshd decides `launch_enabled` per vendor at runtime; nobody
sets it by hand. It is true only if the latest probe row for the agent's binary has
version V, the latest results file for the adapter in `tests/smoke/results/` has version
V, that file validates, and its S-X-1 and S-X-6 cases pass. Otherwise launch commands
return rc 13 `ADAPTER_UNSUPPORTED` with `details.reason` (`no_probe_version`,
`no_smoke_evidence`, `evidence_stale`, `evidence_invalid`, `launch_cases_failed`, or
`launch_held` for an adapter held back by milestone; none is held today, kiro was held
during M3, L11).
meshd reads this evidence from the checkout's `tests/` tree at runtime (`tests/smoke/results/`,
and it hashes `tests/fixtures/real/`); this is an accepted deviation for a local v1
(D5). Upgrading a vendor CLI makes its evidence `evidence_stale` until new results for
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
- Vendor launch leads stay refused in M3-lite (D4, section 9).
- The idle timeout stays off: `run.idle_timeout_s` defaults to `0`, and it is not
  validated against vendor output.
- Resume is used only for a resume capability that is `supported: "yes"` and smoke
  verified; every vendor still reports `supported: "unknown"`, so vendor runs start
  fresh with the full prompt.
- **droid `dev` is the profile that commits locally.** In the N26 demo, droid `dev`
  (`--auto medium`) edited and committed in its own task worktree; Mesh never
  auto-commits, then merged the task branch into integration with a two-parent merge
  commit, with no remote and nothing pushed. kiro and agy `dev` stay
  `PROFILE_UNSUPPORTED`.
