# Agent Mesh v1 — Requirements

Status: requirements frozen for planning (2026-11-07, Asia/Shanghai).
This is a **fresh implementation**. Do not reuse, copy, or reference code from any earlier AgentMesh repository or from `/workspace/agent-mesh*`.

Legend: **[CONFIRMED]** = explicitly decided by the user (James). **[SUGGESTED]** = proposed by the coordinating assistant; planners may refine but must state deviations.

## 0. Scope & environment
- [CONFIRMED] Runs on a single Linux machine only (no cross-machine collaboration in v1).
- [CONFIRMED] v1 surface = CLI + local programmatic interface for agents. No web UI in v1 (later).
- [SUGGESTED] Local interface only via loopback (127.0.0.1) or a Unix domain socket; never bind a public port. No paid services.
- [SUGGESTED] No cloud repo / push / deploy is part of v1 development.

## 1. Task intake and the lead agent
- [CONFIRMED] The user hands project work to the project's **lead agent** (主 agent) through a helper agent or any other agent. Mesh must expose one unified task-intake entry usable by any agent.
- [CONFIRMED] The lead agent decomposes work and **directly assigns** executors (not a free-for-all claim market), collects deliverables, accepts / rejects (sends back for rework), and drives the project to completion, then notifies the user.
- [CONFIRMED] Each project has a lead agent chosen by the user at creation; a configurable **global default** lead is used when none is given; the lead can be changed mid-project. Task history and context records persist across lead changes.
- [CONFIRMED] Project completion: lead agent confirms completion against the project's acceptance criteria and then notifies the user. The user does not re-confirm completion.
- [SUGGESTED] Full traceability: project/task dependencies, task state machine, every transition with actor, time, reason.

## 2. Agents and execution modes
- [CONFIRMED] Two ways for work to start: (a) notify an already-running agent of a task; (b) Mesh launches a local CLI agent process.
- [CONFIRMED] Integration scope (all six): Kiro CLI, Antigravity (`agy`), Factory (`droid`), Hermes, OpenClaw, Mistral Vibe.
- [SUGGESTED, mandatory honesty rule] Each adapter's real capabilities (non-interactive mode, model flag, effort flag, output format, session resume, permission flags) must be verified against the real installed CLI. A simulator/fake must never be presented as support for a vendor. Phased delivery allowed; the plan must state per-adapter status: verified / partially verified / not installed / unsupported. (Observed on this box at planning time: droid 1.3.0, agy 1.4.8, kiro-cli 3.1.0, vibe 3.1.0 installed; Hermes and OpenClaw NOT installed.)

## 3. Approvals and authority
- [CONFIRMED] Important decisions (spending money, deleting, deploying, major scope change, etc.) require the **user's** approval. Routine assignment, acceptance, and project completion are approved by the lead agent.
- [CONFIRMED] User approval can arrive two ways: relayed by the originating agent, or directly via a Mesh CLI command.
- [SUGGESTED] Each approval binds to one specific pending operation (id + content hash), is idempotent, audited, expires, and can't be satisfied by an executor agent self-reporting "user approved". Real identity / capability boundaries must exist (e.g. per-principal credentials/capability tokens, separate user-approval channel), not just a string `role=user`. Relayed approvals must record the relaying agent and be distinguishable from direct ones; plan must state the trust model honestly.
- [SUGGESTED] Distinguish Mesh scheduling-layer gates from each external CLI's own permissions. Do not claim Mesh sandboxes arbitrary external CLIs.

## 4. Models and concurrency
- [CONFIRMED] The user configures each agent's default model and an allowed-model list; the lead agent may only pick within that list.
- [CONFIRMED] Concurrency limit is any user-defined positive integer. Invalid values must fail clearly. Busy/queued behaviour must be testable.

## 5. Workspaces
- [CONFIRMED] Every task gets its own working directory; code tasks get their own git branch (worktree). The lead agent merges after acceptance.
- [SUGGESTED] Merge only after tests pass; conflicts are never silently overwritten. Lead agent's own workdir is separate from executor workdirs.

## 6. Failure handling
- [CONFIRMED] Failures / no-response timeouts are recorded and the lead agent is notified; the lead decides retry / reassign / escalate to user. No automatic infinite retry.
- [SUGGESTED] Concrete design for subprocess management, cancellation (process groups), leases with fencing tokens, crash recovery, so a "timed-out" task isn't still editing files or running twice.

## 7. Notifications and persistence
- [SUGGESTED] Event-driven notifications rather than frequent model polling (save quota). Durable persistence: pending work survives offline agents and Mesh restarts; notifications are de-duplicated.
- [SUGGESTED] CLI agents cannot magically receive push: the plan must state concretely how a notification reaches/wakes each agent session (e.g. launching a new headless run, resuming a session id, a blocking `mesh wait`/inbox command, etc.).

## 8. CLI design (agent-friendly)
- [SUGGESTED] Non-interactive first; layered `--help` with examples; JSON output; stdin input; clear exit codes; idempotency keys; `--dry-run`; safe defaults; consistent `resource verb` structure. Logs must contain no secrets.

## 9. Pipeline for building this project
- [CONFIRMED] Plan written by Factory droid (Opus 5.5); reviewed by Kiro CLI (Opus 5.5); implemented by agy (Gemini 3.8 Flash High); final acceptance by a helper agent.
