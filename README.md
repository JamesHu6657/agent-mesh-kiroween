# Agent Mesh

A local coordinator for CLI coding agents on one Linux machine.

You describe a project, a lead agent breaks it into tasks, and Mesh hands each task to an
executor: either a headless CLI it launches itself (droid, kiro-cli, agy, vibe) or an
agent that polls its inbox. Mesh tracks every task, run, lease and approval as a state
machine, gives each code task its own git worktree, and lets the lead review and merge.
Important decisions (spending, deleting, deploying, permission escalation) wait for the
user's approval.

It is a daemon (`meshd`) with one SQLite database and a CLI (`bin/mesh`) that talks to it
over a Unix socket. There is no network listener and no web UI.

Built for [Kiroween 2026](https://kiroween.devpost.com/) (Frankenstein category).

## Status

v1, milestones M1 to M3 accepted (gate reports in `docs/`). What works today:

- **Projects and tasks:** intake, leads and handover, dependencies, priorities, per-agent
  and global concurrency, approvals with audit, events and outbox.
- **Execution:** a supervised run wrapper with timeouts, cancel, leftover-process
  cleanup and crash recovery; run tokens bound to the run's session.
- **Git:** per-task worktrees and branches, submission info, a merge pipeline with a test
  step, `project deliver`, workspace cleanup. Mesh never pushes, rebases or force-updates.
- **Lead turns:** launch-mode leads woken by pending messages, with a `mesh-actions`
  protocol (vendor CLIs as launch leads are refused in this version; use an inbox lead).
- **Real adapters:** a vendor CLI may launch only when committed smoke evidence matches
  the installed version. Current status per adapter is in `docs/adapters.md`.

Known limits are listed honestly in `docs/m3-gate-report.md` and `docs/usage.md` §5.
**Mesh is not a security sandbox and does not sandbox external CLIs:** everything runs as
one Unix user.

## Requirements

- Linux (uses `/proc`, Unix sockets, process groups)
- Python 3.11 or newer, standard library only (tested on 3.11 and 3.13)
- git, for repo projects
- Optional: the vendor CLIs you want to launch (droid, kiro-cli, agy, vibe)

## Quick start

```sh
# from a clone of this repository
bin/mesh init              # asks for a passphrase on /dev/tty (or pass --passphrase-fd N)
bin/mesh daemon start
bin/mesh agent create --user --name lead1 --adapter none --delivery inbox --can-lead --max-concurrency 1
bin/mesh --help            # resources and commands; `bin/mesh help <topic>` for topics (auth, workflow, ...)
bin/mesh daemon stop --user
```

Commands that take `--user` ask for the passphrase the same way.

The full workflow (agents, intake, tasks, approvals, merge) is in
[`docs/usage.md`](docs/usage.md). Adapter details and evidence rules are in
[`docs/adapters.md`](docs/adapters.md).

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
