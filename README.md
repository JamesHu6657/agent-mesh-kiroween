# Agent Mesh

A local coordinator for CLI coding agents on one Linux machine.

You describe a project, a lead agent breaks it into tasks, and Mesh hands each task to an
executor: either a headless CLI it launches itself (droid, kiro-cli, agy, vibe) or an
agent that polls its inbox (Claude Code, Cursor, Codex, a bot, anything that can run a
shell command). Mesh tracks every task, run, lease and approval as a state machine, gives
each code task its own git worktree, and lets the lead review and merge. Important
decisions (gated actions and permission escalation) wait for the user's
approval.

It is a daemon (`meshd`) with one SQLite database and a CLI (`mesh`) that talks to it
over a Unix socket. There is no network listener and no web UI.

Built for [Kiroween 2026](https://kiroween.devpost.com/) (Frankenstein category).

## Requirements

- **Linux only.** Mesh relies on `/proc`, Unix sockets and process groups. macOS and
  Windows are not supported (WSL2 is untested).
- Python 3.11 or newer. Standard library only: no dependencies.
- git, for repo projects and per-task worktrees.
- Optional: the agent CLIs you want Mesh to launch (droid, kiro-cli, agy, vibe).

## Install

Pick one.

```sh
# A. From a clone (no packaging tools needed)
git clone https://github.com/JamesHu6657/agent-mesh-kiroween.git agent-mesh
cd agent-mesh
sh scripts/install.sh            # links bin/mesh into ~/.local/bin (MESH_BIN_DIR to change)

# B. As a tool (isolated venv)
pipx install git+https://github.com/JamesHu6657/agent-mesh-kiroween.git
#   or: uv tool install git+https://github.com/JamesHu6657/agent-mesh-kiroween.git

# C. Into a venv you manage
python3 -m venv ~/.venvs/mesh && ~/.venvs/mesh/bin/pip install ./agent-mesh
```

Make sure the directory holding `mesh` is on your `PATH`, then run `mesh version`.

## Quick start

```sh
mesh setup
```

`mesh setup` asks for a new passphrase (at least 12 characters, twice), then creates the
home (`~/.agent-mesh`), starts the daemon, creates an inbox lead agent called `lead`,
saves its token to `~/.config/agent-mesh/lead.token` (mode 0600), makes it the default
lead and checks your environment. It is safe to run again: finished steps are skipped.

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
T=~/.config/agent-mesh/lead.token
mesh agent create --user --name worker1 --adapter none --delivery inbox --max-concurrency 1
mesh agent token issue worker1 --user    # save it 0600, as in docs/examples/inbox-agent.sh
mesh intake submit --user --title "Hello" --goal "Write hello.txt" --criterion "hello.txt exists"
mesh inbox list --token-file "$T"                   # the lead sees project.created
mesh task create --project <prj_id> --title "Write hello.txt" --spec "Create hello.txt" \
  --assignee worker1 --token-file "$T"
```

The executor (here an inbox agent `worker1`, see
[`docs/examples/inbox-agent.sh`](docs/examples/inbox-agent.sh)) then runs
`mesh inbox wait`, `mesh task start <tsk>` (prints `run_id`, `fence` and a workspace
directory), does the work there, and `mesh task submit <tsk> --run <run> --fence <n>
--summary …`. The lead finishes:

```sh
mesh task accept <tsk_id> --token-file "$T"     # or: task reject <tsk_id> --reason …
echo '{"criteria":[{"id":"<crit_id>","met":true,"evidence":"hello.txt exists"}]}' \
  | mesh project complete <prj_id> --report - --token-file "$T"
```

Criterion ids are in `mesh project show <prj_id> --user`.

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
  --allowed-model <model-id> --default-model <model-id> --max-concurrency 2
```

Repo projects (`--repo /abs/path --base-branch main --test-command '["make","test"]'` on
`intake submit`) give each code task its own worktree and branch, and the lead merges
accepted work with a test step. Mesh never pushes, rebases or force-updates.

The full workflow (agents, intake, tasks, approvals, merge, deliver) is in
[`docs/usage.md`](docs/usage.md).

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
as one Unix user. Mesh's guarantees are about coordination: authenticated callers,
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

v1, milestones M1 to M3 accepted (gate reports in `docs/`). What works today:

- **Projects and tasks:** intake, leads and handover, dependencies, priorities, per-agent
  and global concurrency, approvals with audit, events and outbox.
- **Execution:** a supervised run wrapper with timeouts, cancel, leftover-process
  cleanup and crash recovery; run tokens bound to the run's session.
- **Git:** per-task worktrees and branches, submission info, a merge pipeline with a test
  step, `project deliver`, workspace cleanup.
- **Lead turns:** inbox leads for any agent; launch-mode leads only for the built-in test
  double (vendor CLIs as launch leads are refused until lead-turn recovery covers them).
- **Adapters:** droid and kiro-cli passed real end-to-end runs; agy and vibe have
  adapters without smoke evidence yet; hermes and openclaw are stubs.

Known limits are listed in `docs/m3-gate-report.md` and `docs/usage.md` §5.

## Future work

- Container-per-agent mode: launch each agent run in its own container with only its
  worktree mounted.
- Vendor CLIs as launch-mode leads.
- Command builders for hermes and openclaw.
- A read-only local status page.

## Tests

```sh
bash scripts/test.sh
```

Runs the unit, integration and recovery tiers offline in under 10 minutes. The default
suite never starts a real vendor CLI; real-CLI probe, smoke and end-to-end tiers are
opt-in and run only with explicit consent (see `tests/smoke/`).

## How it was built (and how Kiro was used)

Agent Mesh was built by a mesh of AI agents coordinated by one person. Kiro wrote and
reviewed plans and specs, reviewed code, and is one of the CLIs Mesh can launch. The
`.kiro/` directory holds the project's specs (`specs/` per milestone), steering files,
agent configs and hooks (acceptance consistency, affected tests, secret scan). Plans,
reviews and gate reports are kept in `docs/`. The private coordination log, handoff
notes and raw agent logs are not part of this public snapshot.

## License

[MIT](LICENSE) © 2026 Shucheng Hu
