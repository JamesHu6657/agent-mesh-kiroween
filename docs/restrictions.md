# Restrictions: removed, configurable, kept

Agent Mesh was first built for one person's machine during a hackathon, with strict
gates while it was under review. For general use some of those gates were removed or
made configurable. This page lists each one and why. Supports Linux and native
Windows 10/11 (Python 3.12+); macOS is not supported.

## Removed

| Restriction | Before | Now | Why |
|---|---|---|---|
| Passphrase only from `/dev/tty` or `--passphrase-fd` | Scripts had to open a pipe fd | Also `--passphrase-file PATH`, `MESH_PASSPHRASE_FILE`, `MESH_PASSPHRASE` (first match: fd, file flag, file env, env, tty) | Unattended setup (CI, systemd, containers). Files must be owned by the user and mode 0600, else `INSECURE_PERMISSIONS`. The env variable is the weakest source (same-user processes can read it); meshd strips `MESH_*` from every launched agent. A passphrase inside `--input` JSON is still rejected. Note that a launched agent runs as the same user and can read a passphrase file, so with a file source an agent could also make `--user` calls such as approving its own gated actions; keep the file only where every agent is trusted, or use the terminal prompt when approvals matter. |
| Only the user (passphrase) or an outside relayer can decide approvals; a lead never decides for its own project | Every approval needed the human at a terminal | `approval.mode = delegated` (opt-in; default `passphrase`): the project's current inbox lead decides with `--human-confirmed "<the human's words>"` | The human answers in chat instead of typing a passphrase. Kept: only the current lead, own agent token, inbox delivery; never workers, other or former leads, relayers or run tokens; `direct_only` categories (`config`, `permission_escalation` by default) still need the passphrase; decisions are recorded unverified with the human's words in the approval and the audit event. Trusts the lead and its token file. |
| Manual multi-step first run | `init`, `daemon start`, `agent create`, `token issue`, `config set lead.default_agent` by hand | `mesh setup` does all of it, idempotently | First-run friction. |
| Running from a clone only | `bin/mesh` in a checkout | `scripts/install.sh`, `pip`/`pipx`/`uv tool install` (console script `mesh`) | Installation for other people. A package install writes a small launcher in `~/.cache/agent-mesh/bin` so launched runs can find `mesh`. |
| Evidence files only next to the code | `tests/smoke/results` in the checkout | `MESH_EVIDENCE_ROOT` points at any checkout | Package installs do not ship the evidence. |

## Made configurable

| Setting | Default | Strict value | Why |
|---|---|---|---|
| `adapter.evidence_mode` | `"warn"` (strict when meshd runs with `MESH_TESTING=1`) | `"strict"` | Strict mode launches a vendor CLI only when the committed smoke evidence matches the installed version exactly. Other people's CLI versions will rarely match, so by default a failing evidence check is waived and reported as `evidence_warning` in `adapter show`/`probe` and in `mesh doctor`. Not waived in warn mode: adapters on an explicit launch hold, and the hermes/openclaw stubs (no command builder). |
| `lead.vendor_leads` | `false` (vendor launch leads refused with `vendor_lead_m3_lite`) | `false` (default) | Kept off by default so operators opt in deliberately (`mesh config set lead.vendor_leads true --user`). Once enabled, vendor launch leads follow the same `adapter.evidence_mode` policy as launch workers (`warn` launches with an evidence warning surfaced on `lead.turn_started` and assignment responses; `strict` requires valid matching probe + smoke evidence); launch holds (`devin`), `hermes`/`openclaw` stubs, and `lead.profile` default `readonly` stay enforced. Lead turns do not count against `max_concurrency` or `scheduler.max_total_runs`; at most one turn per project runs at a time via `leadturn:<prj>`. Turning it back to `false` refuses new turns/assignments without stopping an already-running turn. |
| `mesh doctor` environment checks | — | — | Advisory: platform, Python ≥ 3.11, git, home, daemon, agent CLIs on `PATH`, evidence mode. They add `checks` and `problems` with hints; they never change the `healthy` status. |

## Kept (not changed)

| Restriction | Why it stays |
|---|---|
| Task/run/lease state machine, fencing, idempotency keys bound to the caller | Correctness: duplicate or stale writes would corrupt projects. |
| Per-task git worktrees; Mesh never pushes, rebases or force-updates | Mesh must never lose or rewrite the user's history. Delivery stays a user action. |
| Scoped run tokens, central authorization, token redaction in captured output | A launched agent can only act on its own task. |
| Approvals for gated actions (see `docs/usage.md` §3) and the review/merge gate | The human decides; `config`/`agent`/token commands stay direct-user only. |
| Passphrase lockout window (`auth.lockout_window_s`, 300 s) and lead wake backoff (`lead.wake_backoff_s`, 30/120/600 s) | Changeable only in testing mode. Lockout is brute-force protection; the backoff stops a failing lead from burning model calls. |
| Vendor CLIs as launch-mode leads refused by default (`vendor_lead_m3_lite`) | Off unless explicitly opted in via `mesh config set lead.vendor_leads true --user` (passphrase required; direct-user only). Once enabled, launch leads follow `adapter.evidence_mode` via `evidence.effective_launch` identically to workers (`warn` launches with an evidence warning surfaced on `lead.turn_started` and assignment responses; `strict` requires valid probe + smoke evidence; launch holds like `devin` and `hermes`/`openclaw` stubs still refused) with full process-group seen tracking and close-safety gating; lead turns do not count against `max_concurrency` or `scheduler.max_total_runs` and stay at most one per project via `leadturn:<prj>`. Toggling off refuses new vendor turns without stopping a turn already running. |
| hermes and openclaw cannot launch | Their adapters are stubs (no command builder); `agent create` is refused while the CLI is not installed and launch work is refused otherwise. Connect them as inbox agents. |
| Unimplemented commands return legacy `NOT_IMPLEMENTED_IN_M1` | The operation is unavailable; internal milestone metadata is diagnostic only. |
| `fake` adapter only with `MESH_TESTING=1` | It is a test double, not an agent. |
| Per-agent model/effort allowlists and concurrency caps | User configuration, set per agent at `agent create` (and optional global `scheduler.max_total_runs`); lead turns do not count against `max_concurrency` or `scheduler.max_total_runs` (at most one per project via `leadturn:<prj>`). |
| Linux and Windows 10/11 only | Linux uses `/proc`, process groups, `flock`, and Unix sockets; native Windows uses Named Pipes (`\\.\pipe\LOCAL\agent-mesh-...`), Job Objects, Toolhelp/PEB inspection, `LockFileEx`, and NTFS owner-only DACLs. macOS is not supported. |

## Not a sandbox

Mesh runs meshd and every agent as one OS user (a Unix user on Linux; a local user account on Windows). It is not a security sandbox: a
launched agent can read and change anything that user can. To run agents you do not
trust, run Mesh inside a container or VM (see the README, "Running untrusted agents").
Container-per-agent isolation is future work.
