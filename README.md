# Agent Mesh

A local coordinator for CLI coding agents on one Linux or Windows machine.

You describe a project, a lead agent breaks it into tasks, and Mesh hands each task to an
executor: either a headless CLI it launches itself (droid, kiro-cli, codex, claude, agy,
vibe) or an agent that polls its inbox (Claude Code, Cursor, Codex, a bot, anything that
can run a shell command). Mesh tracks every task, run, lease and approval as a state
machine, gives each code task its own git worktree, and lets the lead review and merge.
Important decisions (gated actions and permission escalation) wait for the user's
approval.

It is a daemon (`meshd`) with one SQLite database and a CLI (`mesh`) — plus an optional
local-IPC desktop client (`mesh-desktop` / `pip install "agent-mesh[desktop]"`) — that talks
to `meshd` over a Unix domain socket on Linux or an owner-only Named Pipe
(`\\.\pipe\LOCAL\agent-mesh-...`) on Windows. There is no network listener and no hosted web UI.

Built for [Kiroween 2026](https://kiroween.devpost.com/) (Frankenstein category).

## For Kiroween judges

Mesh runs locally; there is no hosted app to log into. The shortest path through it, on a
Linux machine with Python 3.11+ and git:

```sh
git clone https://github.com/JamesHu6657/agent-mesh-kiroween.git agent-mesh
cd agent-mesh
sh scripts/install.sh      # puts mesh in ~/.local/bin
mesh setup                 # passphrase, daemon, lead agent, environment check
mesh doctor
bash scripts/test.sh       # unit, integration and recovery tiers, offline
```

Then follow [Your first project](#your-first-project-inbox-lead): it uses an inbox worker,
so it needs no vendor CLI, API key or model account. Where to look next:

- Kiro: `.kiro/` (specs, steering, agents, hooks) and [`docs/kiro-usage-log.md`](docs/kiro-usage-log.md).
- Milestone gates and reviews: `docs/m*-gate-report.md`, `docs/review-r*.md`, `docs/reviews/`.
- Adapter evidence: the table under [Status](#status), records in `tests/smoke/results/`.

## Requirements

- **Linux and native Windows 10/11.** Linux relies on `/proc`, Unix sockets, `flock`, and
  process groups; native Windows relies on Named Pipes (`\\.\pipe\LOCAL\agent-mesh-...`),
  Job Objects, Toolhelp/PEB inspection, `LockFileEx`, and NTFS owner-only DACLs. macOS is not supported.
- **Python 3.11 or newer on Linux; Python 3.12 or newer on Windows.** Standard library only: no dependencies.
- git, for repo projects and per-task worktrees. git 2.36 or newer is recommended;
  git 2.31 to 2.35 works through a fallback and `mesh doctor` warns about it. git older
  than 2.31 is not supported: a removed worktree that was not pruned blocks
  `mesh project deliver` there until you run `git worktree prune`.
  On Windows, configure `git config --global core.longpaths true` and
  `git config --global core.autocrlf false`.
- Optional: the agent CLIs you want Mesh to launch (droid, kiro-cli, agy, vibe, codex, claude, devin).
  devin is on a launch hold for now and does not launch outside testing mode; see
  [docs/adapters-devin.md](docs/adapters-devin.md).

## Install

Pick one installation method. Run the venv method from inside the clone. These
examples install the CLI but do not start a daemon or a model run.

```sh
# A. From a clone (no packaging tools needed)
git clone https://github.com/JamesHu6657/agent-mesh-kiroween.git agent-mesh
cd agent-mesh
sh scripts/install.sh            # links bin/mesh into ~/.local/bin (MESH_BIN_DIR to change)

# B. As a tool (isolated venv)
pipx install git+https://github.com/JamesHu6657/agent-mesh-kiroween.git
#   or: uv tool install git+https://github.com/JamesHu6657/agent-mesh-kiroween.git

# C. Into a venv you manage
python3 -m venv ~/.venvs/mesh
~/.venvs/mesh/bin/pip install .   # run inside the cloned agent-mesh directory
```

Make sure the directory holding `mesh` is on your `PATH`, then run `mesh version`.

### Windows (PowerShell, Python 3.12+)

```powershell
git config --global core.longpaths true
git config --global core.autocrlf false
git clone https://github.com/JamesHu6657/agent-mesh-kiroween.git agent-mesh
cd agent-mesh
python -m pip install .          # or add %CD%\bin (containing mesh.cmd) to PATH
mesh setup                       # passphrase (via CONIN$), daemon, lead agent, doctor
mesh doctor
python scripts/run_suite.py      # unit, integration and recovery tiers, offline
```

Non-interactive setups on Windows should use `--passphrase-file` (or `MESH_PASSPHRASE_FILE`)
stored in an owner-only NTFS directory outside every workspace (`mesh doctor --fix-perms`
repairs `$MESH_HOME` DACLs; `--passphrase-fd` is Linux/POSIX-only).

### Windows platform notes & limitations

- Requires Python **3.12+** (`mesh doctor` enforces `sys.version_info >= (3, 12)` on Windows).
- Passphrase input uses `--passphrase-file`, `MESH_PASSPHRASE_FILE`, `MESH_PASSPHRASE`, or `CONIN$`; POSIX `--passphrase-fd` is unavailable.
- Symlink/junction checks (`FILE_FLAG_OPEN_REPARSE_POINT`) refuse reparse points inside `$MESH_HOME`, workspaces, and token paths; creating filesystem symlinks requires Windows Developer Mode or elevated privileges.
- `.cmd`/`.bat` vendor CLI shims force pointer/file prompt delivery when prompts contain newlines because `cmd.exe` splits arguments on newlines.
- Real-CLI `tests/smoke/` (`SmokeSession`) and POSIX helper scripts (`scripts/test.sh`, `scripts/coord/*.sh`, `scripts/inbox-worker-example.sh`) remain Linux/POSIX-only.
- Windows Named Run Job Objects (`Local\mesh-job-...`) exist while at least one handle (`meshd` or `runwrap`) remains open; if both are hard-killed simultaneously with no `seen.json`, Windows has no persistent kernel PGID/SID integer outside Job/PEB tracking.

## Quick start

### Desktop client (chat-first, recommended on Windows)

Install the optional desktop extra (`pip install "agent-mesh[desktop]"`) or install/unzip
the Windows release (per-user Inno Setup installer or portable zip with pinned bundled
MinGit) and launch `Agent Mesh.exe` (`mesh-desktop`). On first run, the built-in graphic
wizard sets your local passphrase (held in memory by default), starts `meshd`, scans
installed local CLIs (`claude`, `codex`, `agy`, `droid`, `kiro`, `vibe`), lets you pick
**one** Lead, automatically pairs a separate Worker (preserving `LEAD_CANNOT_BE_ASSIGNEE`),
and lands directly in the project chat. Use the native folder picker to attach a local Git
repository (auto-detecting branches and test commands), review the Lead's inline plan card,
click its buttons (允许 / 始终允许 / 拒绝) to approve or reject (chat only asks for revisions), and watch nested tasks run in real time. Closing
the window hides to the Windows system tray while keeping `meshd` running; quitting stops
nothing unless you explicitly choose to stop `meshd`. See [`docs/desktop.md`](docs/desktop.md)
for security guarantees, installer build steps, and details.

### CLI setup

```sh
mesh setup
```

`mesh setup` asks for a new passphrase (at least 12 characters, twice), then creates the
home (`~/.agent-mesh`), starts the daemon, creates an inbox lead agent called `lead`,
saves its token to `~/.config/agent-mesh/lead.token` (mode 0600), makes it the default
lead and checks your environment. With a non-default `MESH_HOME`/`--home`, the token
filename includes a home-specific suffix; use the `token_file` returned by setup,
not an assumed `lead.token` path. It is safe to run again: finished steps are skipped.

Non-interactive (scripts, CI): put the passphrase in a 0600 file and pass it on.

```sh
mkdir -p -m 700 ~/.config/agent-mesh
(umask 077; printf '%s\n' 'a long passphrase here' > ~/.config/agent-mesh/passphrase)
mesh setup --passphrase-file ~/.config/agent-mesh/passphrase
export MESH_PASSPHRASE_FILE=~/.config/agent-mesh/passphrase   # for later --user commands
```

Passphrase sources, first match wins: `--passphrase-fd N`, `--passphrase-file PATH`,
`MESH_PASSPHRASE_FILE`, `MESH_PASSPHRASE`, then a prompt on the terminal. Prefer the
file: an environment variable is readable by other processes of the same user. Mesh
strips every `MESH_*` variable from the agents it launches.
Launched agents run as your user, so they can read a passphrase file too: use the
file only when every agent is trusted, and the terminal prompt when approvals matter.

Rather not type the passphrase for every approval? `mesh setup --approval-mode delegated`
(or later `mesh config set approval.mode '"delegated"' --user`) lets your inbox lead ask
you in chat and approve with your quoted reply (`--human-confirmed`), recorded in the
audit log. It trusts the lead; `config` and `permission_escalation` still need the
passphrase. Details: docs/usage.md §3.4.

### Your first project (inbox lead)

```sh
T=~/.config/agent-mesh/lead.token  # use setup's token_file for a non-default home
mesh agent create --user --name worker1 --adapter none --delivery inbox --max-concurrency 1
mesh agent token issue worker1 --out ~/.config/agent-mesh/worker1.token --user   # 0600 file
mesh intake submit --user --title "Hello" --goal "Write hello.txt" --criterion "hello.txt exists"
mesh inbox list --token-file "$T"                   # the lead sees project.created
mesh task create --project "$PROJECT_ID" --title "Write hello.txt" --spec "Create hello.txt" \
  --assignee worker1 --token-file "$T"
```

Set `PROJECT_ID` to the `id` in the intake response (`data.id`, a `prj_...` value). Variables such as
`TASK_ID`, `RUN_ID`, `FENCE` and `CRITERION_ID` below stand for values returned by
Mesh; do not paste angle-bracket placeholders into a shell. Agent and user commands
have different authority: only the human runs the `--user` setup commands.

The executor (here an inbox agent `worker1`, see
[`docs/examples/inbox-agent.sh`](docs/examples/inbox-agent.sh)) then runs
`mesh inbox wait`, `mesh task start <tsk>` (prints `run_id`, `fence`, `session` and a
workspace directory), then exports the session before any other command for that run:

```sh
export MESH_SESSION="$SESSION"   # the `session` value from task start
```

Without it `heartbeat` and `submit` fail with `FENCE_MISMATCH` (`session_missing`). It then
does the work there while sending a heartbeat well before the returned lease expires
(for the default 120-second lease, every 40 seconds or less), and submits with
`mesh task submit "$TASK_ID" --run "$RUN_ID" --fence "$FENCE" --summary "Created hello.txt"`. The lead finishes:

```sh
mesh task accept "$TASK_ID" --token-file "$T"
python3 -c 'import json,sys; print(json.dumps({"criteria":[{"id":sys.argv[1],"met":True,"evidence":"hello.txt exists"}]}))' "$CRITERION_ID" \
  | mesh project complete "$PROJECT_ID" --report - --token-file "$T"
```

Task ids are returned by `task create`; criterion ids are in
`mesh project show "$PROJECT_ID" --token-file "$T"`. For a fully isolated,
copyable walkthrough, see [the example audit](docs/examples/docs-audit.md).

The lead is whatever agent holds that token: give the token path and the loop in
[`docs/examples/inbox-agent.sh`](docs/examples/inbox-agent.sh) to Claude Code, Cursor or
any agent that can run shell commands, and it can plan, assign, review (`mesh task
accept` / `reject`) and complete the project (`mesh project complete`).

### Adding an agent CLI

Examples for each supported CLI are in [`docs/examples/`](docs/examples/): an inbox agent,
droid, kiro, agy and vibe. Typical launch agent:

```sh
mesh adapter probe kiro --user
mesh adapter models kiro --user
mesh agent create --user --name kiro1 --adapter kiro \
  --allowed-model "$MODEL_ID" --default-model "$MODEL_ID" --max-concurrency 2
```

Set `MODEL_ID` to an id returned by `adapter models`; listing models may contact
the vendor, but does not start inference. Creating an agent does not launch it.

codex and claude have no example script yet and no model listing: pass the model ids you
want with `--allowed-model` and `--default-model` (claude also accepts aliases such as
`opus`). Commands, permission profiles and limits are in
[`docs/adapters-codex-claude.md`](docs/adapters-codex-claude.md).

Repo projects (`--repo /abs/path --base-branch main --test-command '["make","test"]'` on
`intake submit`) give each code task its own worktree and branch, and the lead merges
accepted work with a test step. Mesh never pushes, rebases or force-updates.

The full workflow (agents, intake, tasks, approvals, merge, deliver) is in
[`docs/usage.md`](docs/usage.md).

- [`docs/inbox-workers.md`](docs/inbox-workers.md): how an external or remote agent works as an inbox worker (token, inbox, start, heartbeat, submit, fail, rework, lease expiry, errors).
- [`scripts/inbox-worker-example.sh`](scripts/inbox-worker-example.sh): a small bash loop that waits for an offered task, runs your command in its workspace with a heartbeat, then submits or fails.

### Checking things

- `mesh doctor`: platform, Python, git, home, daemon, agent CLIs on PATH, evidence mode,
  each problem with a hint. `mesh doctor --check-invariants` (daemon stopped) verifies
  the database; `mesh doctor --fix-perms` restores file modes.
- `mesh status`, `mesh event tail --user`, `mesh help <topic>`.

### Launch evidence

The repository carries smoke-test evidence for the CLI versions it was tested with
(`tests/smoke/results/`). By default (`adapter.evidence_mode = "warn"`) any installed
supported CLI can be launched; `mesh adapter show <id>` and `mesh doctor` tell you when
your version has no matching evidence. To launch only evidence-matched versions:

```sh
mesh config set adapter.evidence_mode '"strict"' --user
```

With a package install (pipx, uv, pip) the evidence files are not installed; point
`MESH_EVIDENCE_ROOT` at a clone if you use strict mode.

## Security model

**Mesh is not a security sandbox.** Everything, meshd and every agent it launches, runs
as one OS user (a Unix user on Linux; a local user account on Windows). Mesh's guarantees are about coordination: authenticated callers,
scoped run tokens, idempotent writes, an audited approval flow, redaction of tokens from
captured output, per-task worktrees, and process cleanup. They do not stop a launched
agent from reading or changing anything your user can.

### Running untrusted agents

If you would not run an agent CLI directly in your shell, do not run it under Mesh
either. Run the whole Mesh setup in a container or VM instead:

- a throwaway VM or a container (Docker/Podman with a dedicated volume for
  `~/.agent-mesh` and your repos), with no host home directory mounted;
- no long-lived cloud credentials inside it; give agents scoped, revocable tokens only;
- a separate Unix user at minimum, if a VM is not possible.

Container-per-agent isolation inside Mesh is future work (below), not a feature today.

What each restriction protects, and which ones were removed or made configurable for
general use, is in [`docs/restrictions.md`](docs/restrictions.md).

## Upgrade and uninstall

- **Upgrade:** stop the daemon (`mesh daemon stop --user`), then `git pull` in the clone,
  or `pipx upgrade agent-mesh` / `uv tool upgrade agent-mesh`, then `mesh daemon start`.
  The database schema is checked at start; a newer schema than the code is refused
  rather than guessed at.
- **Uninstall:** `mesh daemon stop --user`; then `sh scripts/install.sh --uninstall`
  (clone), `pipx uninstall agent-mesh` or `uv tool uninstall agent-mesh`. Your data stays
  in `~/.agent-mesh` (database, runs, workspaces) and `~/.config/agent-mesh` (tokens);
  delete them yourself if you want. A package install may also leave a small launcher in
  `~/.cache/agent-mesh/bin/<install-id>` (one per installation).

## Status

Current functionality:

- **Projects and tasks:** intake, leads and handover, dependencies, priorities, per-agent
  and global concurrency, approvals with audit, events and outbox.
- **Execution:** a supervised run wrapper with timeouts, cancel, leftover-process
  cleanup and crash recovery; run tokens bound to the run's session.
- **Git:** per-task worktrees and branches, submission info, a merge pipeline with a test
  step, `project deliver`, workspace cleanup.
- **Lead turns:** inbox leads for any agent; launch-mode leads for the built-in test
  double (`MESH_TESTING=1`) and, behind explicit direct-user opt-in
  (`mesh config set lead.vendor_leads true --user`, default `false`), vendor CLIs under the
  same `adapter.evidence_mode` launch-evidence policy as tasks (`warn` launches with an
  evidence warning surfaced on the assignment response/event; `strict` requires valid
  probe + smoke evidence; `lead.profile` defaults to `readonly`; `devin`'s launch hold and
  `hermes`/`openclaw` stubs remain unsupported). Lead turns use the same process-group
  seen tracking, close-safety gate, and `run release --force-seen-unusable` recovery path as tasks.
- **Adapters:** seven vendor CLIs have launch adapters, with the evidence below.

| CLI (adapter) | Version | Evidence in this repository | Launch (default warn mode) |
|---|---|---|---|
| Factory Droid (`droid`) | 1.6.0 | smoke cases and two real end-to-end runs (readonly, code task) | yes |
| Kiro CLI (`kiro`) | 3.1.0 | smoke cases (headless, session id, SIGTERM) and a readonly end-to-end run | yes |
| Codex CLI (`codex`) | 0.166.0 | smoke cases on a real machine (readonly): headless, session id, resume, SIGTERM pass; file write rejected by the read-only sandbox, `touch y.txt` left no file | yes |
| Claude Code (`claude`) | 2.1.325 | smoke cases on a real machine (readonly, DeepSeek through an Anthropic-compatible proxy): headless, session id, resume, model, SIGTERM pass; `touch y.txt` left no file (no shell tool in the session, 0 permission denials recorded) | yes |
| Antigravity (`agy`) | 1.5.12 | smoke cases (readonly, prompt passed directly after `--print`): headless, session id, resume, model pass; SIGTERM recorded as fail (agy exits 1 with `error: interrupted`, the process group was gone in 0.2 s); the file-write and `touch y.txt` prompts left no file; real stream fixtures from 1.5.3 for parsing | yes, with an evidence warning |
| Mistral Vibe (`vibe`) | 3.1.0 | smoke cases: headless, session id, resume pass; SIGTERM recorded as fail (vibe exits 3, the process group was gone in 0.2 s) | yes, with an evidence warning |
| Devin CLI (`devin`) | 3000.14.30821 | smoke cases (`unrestricted`, plain-text output): headless and SIGTERM pass; no session id, so no resume | no: launch hold, see [docs/adapters-devin.md](docs/adapters-devin.md) |
| Hermes, OpenClaw | | stub adapters | no; they can join as inbox agents |

In strict mode droid, kiro, codex and claude launch. Per-capability detail is in
[`docs/adapters.md`](docs/adapters.md) and
[`docs/adapters-codex-claude.md`](docs/adapters-codex-claude.md).

Known security and operational limits are described in [the usage guide](docs/usage.md#5-security-model-and-honest-limits).

## Future work

- Container-per-agent mode: launch each agent run in its own container with only its
  worktree mounted.
- Real vendor smoke verification for launch-mode lead workflows (beyond single-turn/process-gate coverage).
- Command builders for hermes and openclaw.
- A read-only local status page.
- Optional decision-model plugin (approved idea, not built): Jev, TypeSafe AI's "System One"
  decision model, for task routing only (which coder agent or model tier gets a task;
  later perhaps review-finding triage and watcher wake decisions). Off by default. It never
  decides approvals, merges or any safety gate; those stay with the user, the lead and the
  existing gates.

## Tests

```sh
python scripts/run_suite.py   # cross-platform (Linux and Windows)
bash scripts/test.sh          # Linux/POSIX shell equivalent
```

Runs the unit, integration and recovery tiers offline in under 10 minutes. The default
suite never starts a real vendor CLI; real-CLI probe, smoke and end-to-end tiers are
opt-in and run only with explicit consent (see `tests/smoke/`).

## How it was built (and how Kiro was used)

Agent Mesh was built by a mesh of AI agents coordinated by one person. Kiro wrote and
reviewed plans and specs, reviewed code, and is one of the CLIs Mesh can launch. The
`.kiro/` directory holds the project's specs (`specs/`), steering files,
agent configs and hooks (acceptance consistency, affected tests, secret scan). Plans,
reviews and acceptance reports are kept in `docs/`; how Kiro was used, session by
session, is in [`docs/kiro-usage-log.md`](docs/kiro-usage-log.md). The private
coordination log, handoff notes and raw agent logs are not part of this public snapshot.

## License

[MIT](LICENSE) © 2026 Shucheng Hu
