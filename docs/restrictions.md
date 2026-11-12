# Restrictions: removed, configurable, kept

Agent Mesh was first built for one person's machine during a hackathon, with strict
gates while it was under review. For general use some of those gates were removed or
made configurable. This page lists each one and why. Linux only throughout: macOS and
Windows are not supported.

## Removed

| Restriction | Before | Now | Why |
|---|---|---|---|
| Passphrase only from `/dev/tty` or `--passphrase-fd` | Scripts had to open a pipe fd | Also `--passphrase-file PATH`, `MESH_PASSPHRASE_FILE`, `MESH_PASSPHRASE` (first match: fd, file flag, file env, env, tty) | Unattended setup (CI, systemd, containers). Files must be owned by the user and mode 0600, else `INSECURE_PERMISSIONS`. The env variable is the weakest source (same-user processes can read it); meshd strips `MESH_*` from every launched agent. A passphrase inside `--input` JSON is still rejected. Note that a launched agent runs as the same user and can read a passphrase file, so with a file source an agent could also make `--user` calls such as approving its own gated actions; keep the file only where every agent is trusted, or use the terminal prompt when approvals matter. |
| Manual multi-step first run | `init`, `daemon start`, `agent create`, `token issue`, `config set lead.default_agent` by hand | `mesh setup` does all of it, idempotently | First-run friction. |
| Running from a clone only | `bin/mesh` in a checkout | `scripts/install.sh`, `pip`/`pipx`/`uv tool install` (console script `mesh`) | Installation for other people. A package install writes a small launcher in `~/.cache/agent-mesh/bin` so launched runs can find `mesh`. |
| Evidence files only next to the code | `tests/smoke/results` in the checkout | `MESH_EVIDENCE_ROOT` points at any checkout | Package installs do not ship the evidence. |

## Made configurable

| Setting | Default | Strict value | Why |
|---|---|---|---|
| `adapter.evidence_mode` | `"warn"` (strict when meshd runs with `MESH_TESTING=1`) | `"strict"` | Strict mode launches a vendor CLI only when the committed smoke evidence matches the installed version exactly. Other people's CLI versions will rarely match, so by default a failing evidence check is waived and reported as `evidence_warning` in `adapter show`/`probe` and in `mesh doctor`. Not waived in warn mode: adapters on an explicit launch hold, and the hermes/openclaw stubs (no command builder). |
| `mesh doctor` environment checks | — | — | Advisory: platform, Python ≥ 3.11, git, home, daemon, agent CLIs on `PATH`, evidence mode. They add `checks` and `problems` with hints; they never change the `healthy` status. |

## Kept (not changed)

| Restriction | Why it stays |
|---|---|
| Task/run/lease state machine, fencing, idempotency keys bound to the caller | Correctness: duplicate or stale writes would corrupt projects. |
| Per-task git worktrees; Mesh never pushes, rebases or force-updates | Mesh must never lose or rewrite the user's history. Delivery stays a user action. |
| Scoped run tokens, central authorization, token redaction in captured output | A launched agent can only act on its own task. |
| Approvals for gated actions (see `docs/usage.md` §3) and the review/merge gate | The human decides; `config`/`agent`/token commands stay direct-user only. |
| Passphrase lockout window (`auth.lockout_window_s`, 300 s) and lead wake backoff (`lead.wake_backoff_s`, 30/120/600 s) | Changeable only in testing mode. Lockout is brute-force protection; the backoff stops a failing lead from burning model calls. |
| Vendor CLIs as launch-mode leads refused (`vendor_lead_m3_lite`) | Lead-turn crash recovery does not yet track the lead's process group (known limitation L4). Use an inbox lead for vendor agents; the test double can still be a launch lead in testing mode. |
| hermes and openclaw cannot launch | Their adapters are stubs (no command builder); `agent create` is refused while the CLI is not installed and launch work is refused otherwise. Connect them as inbox agents. |
| Commands planned for later milestones return `NOT_IMPLEMENTED_IN_M1` | They are not built; the error names the milestone. |
| `fake` adapter only with `MESH_TESTING=1` | It is a test double, not an agent. |
| Per-agent model/effort allowlists and concurrency caps | User configuration, set per agent at `agent create`; no global limit is imposed. |
| Linux only | Process supervision reads `/proc` and uses process groups and Unix sockets. |

## Not a sandbox

Mesh runs meshd and every agent as one Unix user. It is not a security sandbox: a
launched agent can read and change anything that user can. To run agents you do not
trust, run Mesh inside a container or VM (see the README, "Running untrusted agents").
Container-per-agent isolation is future work.
