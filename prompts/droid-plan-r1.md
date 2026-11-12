You are the PLANNER for a brand-new project "Agent Mesh v1" in the current directory (a fresh git repo).
Read docs/requirements.md carefully. Also inspect the real CLIs installed on this Linux box (read-only: run their --help / version / model-list commands only; do NOT start model sessions, log in, or change config): `droid exec --help`, `agy --help`, `agy models`, `/home/box/.local/bin/kiro-cli chat --help`, `kiro-cli chat --list-models -f json`, `vibe --help`, and check whether `hermes` / `openclaw` exist. Do not read any credential/token/cookie files.

Write exactly two files (and nothing else, no code):
1. docs/plan.md — an implementation plan detailed enough that a separate implementer agent can build v1 without guessing. Must cover:
   - Language/runtime choice and justification (prefer what is already installed on the box; no paid services; no public ports — loopback/Unix socket only).
   - Architecture: data model (projects, tasks, dependencies, agents, adapters, approvals, events, leases), storage (durable, crash-safe, e.g. SQLite WAL), state machines with every transition.
   - CLI command tree (`resource verb`), JSON output, stdin, exit-code table, idempotency keys, --dry-run, layered --help with examples.
   - Identity & authority model: how user, lead agent, executor agents, and relaying agents are distinguished with real credentials/capabilities (not a role string); approval binding to a specific operation hash, expiry, idempotency, audit; how relayed user approvals are recorded and their honest trust limits; what Mesh gates vs what each external CLI's own permission flags do (no claim of sandboxing external CLIs).
   - Lead agent assignment, global default, per-project override, mid-project change with history kept.
   - Model policy: per-agent default model + allow-list enforcement; concurrency limit (any positive integer, invalid values rejected), queueing.
   - Workspaces: per-task dir, git worktree+branch for code tasks, lead's own dir, merge flow (tests first, conflicts never silently overwritten).
   - Process supervision: spawn in process group, timeouts, cancellation, leases with fencing tokens, heartbeat, crash recovery on restart, no duplicate runs, no infinite retry; lead decides retry/reassign/escalate.
   - Notifications: event log + outbox with de-dup; concretely how each agent kind actually receives work (new headless run / session resume / blocking `wait` command / inbox), no assumption of magical push.
   - Adapters for all six (Kiro CLI, agy, droid, Hermes, OpenClaw, Mistral Vibe): per adapter the exact real flags observed on this box, verified/unverified/not-installed status, and what tests prove it. Real-CLI smoke tests must be opt-in and clearly separate from fake-adapter unit tests; never present a fake as vendor support.
   - Security: secrets never logged, log redaction, file permissions, socket permissions.
   - Phased milestones (M1, M2, ...) with clear scope for the FIRST implementation phase, plus a test plan (unit, integration, crash/recovery, concurrency, approval-forgery negative tests).
   - Open questions / risks.
2. docs/acceptance-checklist.md — a numbered, objectively checkable acceptance checklist (each item: what to run / observe, expected result), mapped to requirement sections.

Be concrete and honest; where something is unknown, say so and state how it will be verified. Write in English.
