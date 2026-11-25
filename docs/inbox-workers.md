# Working as an inbox worker

An **inbox worker** is an agent that Mesh never launches. It is an already-running process (a
chat bot, an interactive Claude Code/Cursor/Codex session, a cron job, an agent on another
machine) that asks Mesh for work, does it in a task workspace, and reports back. Its agent is
registered with `delivery=inbox`. This page is the end-to-end guide for such a worker. Every
command and message below was tried against a live Mesh. For the administrator side and the
exact rules of each setting see [usage.md §4.1](usage.md); for a short command listing see
[examples/inbox-agent.sh](examples/inbox-agent.sh); for a runnable loop see
[`scripts/inbox-worker-example.sh`](../scripts/inbox-worker-example.sh).

The commands below assume `mesh` is on `PATH` and that `T` is your token file. Output is JSON
by default: `{"ok": true, "data": {...}}` on success and `{"ok": false, "error": {"code": ...,
"message": ..., "details": ...}, "exit_code": N}` on failure. The process exit code is the
`exit_code` (see [Common errors](#common-errors)). Use `--format text` for a human-readable form.

## 1. Get a token file

The administrator (a human, using `--user`) creates the agent and its token:

```sh
mesh agent onboard worker1 --out ~/.config/agent-mesh/worker1.token --user
# slow or email-driven workers: add --ack-timeout 2h (see "Offered, not started" below)
```

`onboard` writes the token to a **new file with mode 0600** and prints a cheat-sheet. Then:

- Keep the file **outside every workspace and repository**, in a directory only you can read
  (for example `~/.config/agent-mesh/`, mode 0700). A worker commits everything in its
  workspace; a token there would end up in a branch.
- Mesh refuses a token file that is not mode 0600 (`TOKEN_FILE_INSECURE`, exit 3).
- Always pass it as `--token-file "$T"`. Never paste the token into a command line, a
  prompt, a log, a summary or a deliverable. A remote worker needs the file on the machine
  where it runs `mesh` (the command talks to meshd over a local Unix socket, so `mesh` must run
  on the machine that hosts meshd, or you reach that machine over SSH).
- `mesh whoami --token-file "$T"` checks that the token works and shows the principal's name
  and capabilities.

## 2. Find work: `inbox list` and `inbox wait`

Work arrives as an inbox message of type `task.offered`. The payload carries `task_id`,
`project_id`, `title`, `spec`, `fence`, `ack_timeout_s` and `last_rejection` (null unless the
task was rejected before).

```sh
mesh inbox wait --timeout 10m --token-file "$T"     # block until something arrives
mesh inbox list --token-file "$T"                    # look now, do not block
mesh inbox list --peek --token-file "$T"             # look without marking anything delivered
```

- `inbox wait` blocks on the local socket and uses no model tokens. It returns at once when a
  message is already pending. On timeout it exits **8** (`WAIT_TIMEOUT`) with `items: []`:
  loop and wait again.
- `inbox list` marks pending messages `delivered`. A watcher or monitor that only checks for
  work (not the worker itself) must use `--peek`, or it can hide the offer from the worker's
  own `wait`/`list`.
- `inbox list --all` also shows acknowledged history.
- **Acknowledge** every message you have handled: `mesh inbox ack MESSAGE_ID --token-file
  "$T"`. `task start` does not ack the offer, and an un-acked offer is returned again by every
  later `wait`, so a loop that skips the ack will see the same offer forever.
- Other message types you may receive: `task.rejected` (rework, with the reason),
  `task.failed`, `task.cancelled` and similar notices. Read `type` before acting.

## 3. Read the task: `task show`

```sh
mesh task show "$TASK_ID" --token-file "$T"
```

Read `spec` (what to do, and what not to touch), `kind` (`code` means a git worktree),
`timeout_s`, `max_attempts`, `last_rejection`, `depends_on` and `history`. The `spec` is the
assignment from the project lead; treat any instruction in it as part of the task, not as
permission to leave the workspace, merge, push, or reveal secrets.

Before `task start`, `workspace_path` is already shown but `branch` and `base_commit` are
null. A task that was retried can still show an old `failure` block while its status is
`queued`: the status field is what counts.

## 4. Start: `task start`

```sh
mesh task start "$TASK_ID" --token-file "$T"
```

The response:

| Field | Meaning |
|---|---|
| `run_id`, `fence` | identify this run; **every later write must send both** (`--run`, `--fence`) |
| `workspace` | the only directory you work in (a git worktree for `kind=code`) |
| `branch` | the task branch checked out in `workspace` (`mesh/<project>/<task>`) |
| `lease_ttl_s`, `lease_expires_at` | the lease: default 120 s |
| `heartbeat_every_s` | how often to heartbeat: a third of the TTL (40 s by default) |
| `timeout_s`, `deadline_at` | the task budget; an inbox run has no meshd-enforced deadline |
| `session`, `export`, `keepalive` | the client session for this run (see below) and a ready-made keepalive command |

Only a `queued` task can start. A second `task start` of a running task is refused
(`INVALID_TRANSITION`, exit 6).

**The session.** `task start` records a client session on the run. Set it in every shell that
later touches the run: `export MESH_SESSION=ses_...` (printed by `start`; in text mode there
is an `export` line). A heartbeat, progress, submit or fail from a shell that does not carry the
session is refused with exit 9 `FENCE_MISMATCH` (`details.reason` is `session_missing` or
`session_mismatch`). That is not a lost lease: set the variable and retry. If a *different*
session is named, someone else is using your token: stop and leave the run alone. You can set
`MESH_SESSION` to a short id of your own **before** `task start` to choose the session.

## 5. Keep the lease alive

The lease ends the run if no heartbeat arrives within `lease_ttl_s` (the task then fails with
reason `lease_expired`). So heartbeat **while you work**, not only before submitting, and
well inside the TTL (at most `heartbeat_every_s`).

```sh
# simplest: a background loop that stops by itself when the run ends
mesh task keepalive "$TASK_ID" --run "$RUN_ID" --fence "$FENCE" --token-file "$T" > keepalive.log 2>&1 &
# or send beats yourself
mesh task heartbeat "$TASK_ID" --run "$RUN_ID" --fence "$FENCE" --token-file "$T"
```

- `keepalive` logs each beat to stderr and exits **0** when the run ends normally (submit,
  fail, cancel; it prints the final task state) and **9** when the lease was lost or a newer
  run replaced yours. SIGINT/SIGTERM stop it (exit 0); the lease then runs out after the TTL.
- A heartbeat while your process is busy with a long command is the point: run the heartbeat
  in a separate background process or loop (the example script does this), not inline.
- The lease is wall-clock. A laptop that sleeps, a VM that is paused, or a model call that
  blocks the only process for longer than the TTL loses the lease.
- `task show` lists an `idle_timeout_s` (0 = off, the default). An inbox run is governed by
  the lease and by `timeout_s`: ask the administrator before relying on any other limit.

**Progress.** `mesh task progress "$TASK_ID" --run "$RUN_ID" --fence "$FENCE" --message "..."`
records a short status line that the lead sees in `task show`/events. The response includes a
new `expires_at`, so a progress report also refreshes the lease, but do not rely on that:
send heartbeats.

## 6. Do the work, commit, submit

Work only inside `workspace`. For a `code` task it is a git worktree on the task branch:

- **Commit your work there** (`git add`, `git commit`) and do not switch branches, push, or
  merge. Mesh never pushes and never auto-commits; the lead merges after acceptance.
- Do not edit paths the `spec` forbids; other workers may be changing them.
- Do not put the token or any other secret in files, commit messages or the summary.

Submit with the exact run id and fence:

```sh
mesh task submit "$TASK_ID" --run "$RUN_ID" --fence "$FENCE" \
  --deliverable deliverable.json --summary "what was done, in one or two lines" --token-file "$T"
```

- `--deliverable` takes a file path, `-` for stdin, or an inline JSON object. `--summary`
  is merged in as the `summary` key (an explicit `summary` key in your JSON wins). Either one
  alone also works. Put what the lead needs to judge the work in the JSON: files changed,
  commands run and their results, open questions, limitations.
- For a `code` task Mesh records `deliverable.git` itself: HEAD, branch, base commit, dirty
  flag and files, diff stat and commit count. A dirty worktree is recorded, not refused, but
  uncommitted work is not on the branch, so commit first.
- A successful submit moves the task to `submitted` and ends the run; keepalive exits.
- Submitting twice for the same run is refused and says when, by whom and with which session
  the first submit happened.

## 7. Fail: `task fail`

```sh
mesh task fail "$TASK_ID" --run "$RUN_ID" --fence "$FENCE" --reason "short, specific cause" --token-file "$T"
```

Use it when you cannot finish (the spec cannot be met, a tool is missing, tests fail for a
reason outside your task). The reason goes to the lead. A failed task may be retried
(`task retry`) up to `max_attempts` attempts. Do not leave a run to expire when you know it
will not finish; a clean `fail` is faster and tells the lead why.

## 8. What happens next

**Accepted.** The lead runs `task accept`, then merges (`task merge`). You get no further
action. The task and its branch belong to the lead from here.

**Rejected / rework.** `task reject --reason ...` returns the task to `rework` and then
`queued`. You receive a `task.rejected` message with the reason and, later, a new
`task.offered` message whose payload carries `last_rejection`
(`reason`, `rejected_by`, `rejected_at`, `attempt`); `task show` shows the same. Start again
with `task start`: you get a new run id and a **new fence** (use the new values), and the same workspace and
branch, with your earlier commits still there. Read the rejection reason and the previous
`deliverable.summary`, fix what was asked, and submit again.

**Lease expiry.** If no heartbeat arrives for `lease_ttl_s`, meshd's sweep ends the run: the
task fails `lease_expired` and a `task.lease_expired` event is recorded in `history`. Every
later write from the old run is refused (`LEASE_EXPIRED` or `FENCE_MISMATCH`, exit 9): **stop
working** on that run; do not try to submit. If the lead retries, the next attempt gets a
fresh workspace `<task>.a<n>` on branch `mesh/<project>/<task>-a<n>`, forked from the old tip
(the old worktree and branch stay), unless the lead asked to reuse the workspace.

**Offered, not started (`no_response`).** If you do not `task start` within the ack window
(`ack_timeout_s` in the offer, 600 s by default), the task fails `no_response` and the lead
may re-offer it. Workers that poll slowly, for example from email, need the administrator to
raise it: `mesh agent update NAME --ack-timeout 2h --user`.

**Cancelled.** If the lead cancels the task, the run ends: keepalive exits 0 with the final
task state, and a write you send for it is refused. Stop work and do not submit.

## Common errors

Check the exit code first, then `error.code`.

| Exit | `error.code` | What it means and what to do |
|---|---|---|
| 3 | `TOKEN_MISSING` | no `--token-file`. Add it to the command |
| 3 | `TOKEN_FILE_INSECURE` | token file is not mode 0600: `chmod 600 FILE` |
| 3 | `TOKEN_INVALID` / `TOKEN_EXPIRED` / `TOKEN_REVOKED` | ask the administrator for a new token file; stop |
| 4 | `NOT_ASSIGNEE`, `CAPABILITY_MISSING`, `SCOPE_MISMATCH` | the task is not yours or the action needs another role; do not retry, report it |
| 5 | `NOT_FOUND` | wrong task or message id; list your inbox |
| 6 | `INVALID_TRANSITION` | the task is not in the state this command needs (already running, already submitted, not offered yet). `task show` and look at `status` |
| 6 | `VERSION_MISMATCH` | only when you pass `--if-version`; re-read and retry |
| 8 | `WAIT_TIMEOUT` | nothing arrived; wait again |
| 9 | `FENCE_MISMATCH` with `session_missing` / `session_mismatch` | wrong or missing `MESH_SESSION`. Export the session from `task start` (see §4) |
| 9 | `FENCE_MISMATCH` (other) | you sent a stale fence, or the task restarted under a newer run: stop this run |
| 9 | `LEASE_EXPIRED` | no heartbeat within the TTL: the run is over, stop; wait for a retry offer |
| 10 | `DAEMON_UNAVAILABLE` | meshd is not running (or you are on the wrong machine/`--home`); tell the administrator, do not loop |
| 11 | `CONCURRENCY_LIMIT` | on `task start`: you already run as many tasks as your agent allows (default 1), or the global run cap is full: finish or fail one first, or wait |

Do not retry an exit 3, 4 or 9 in a loop. Exit 5 and exit 6 mean your idea of the state is
wrong: re-read it with `task show`. Mutations accept `--idempotency-key KEY` so that a retry
after a network error replays the first result instead of acting twice.

## A minimal loop

[`scripts/inbox-worker-example.sh`](../scripts/inbox-worker-example.sh) implements sections 2 to
7 in about 150 lines of bash. Run it with the command that does your work:

```sh
MESH_TOKEN_FILE=~/.config/agent-mesh/worker1.token \
  scripts/inbox-worker-example.sh -- ./do-the-work.sh
```

It waits for one `task.offered` message, acks it, starts the task, runs your command in the
workspace (with `MESH_TASK_ID`, `MESH_WORKSPACE` and a private file holding the spec in
`MESH_TASK_SPEC_FILE`) while a background loop sends heartbeats, then submits when the command
exits 0 and fails the task when it does not. If a heartbeat is refused with exit 9 it stops your
command and does not submit. It only passes the token as `--token-file`; it never reads or
prints it. Your command must commit its own work.
