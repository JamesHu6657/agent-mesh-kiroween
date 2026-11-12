# Planner prompt for ChatPRD (Opus 5.5) — Agent Mesh v1
(Self-contained: ChatPRD cannot run commands on the box, so observed CLI facts are inlined below. Full requirements follow at the end.)

You are the PLANNER for a brand-new project "Agent Mesh v1". Produce two Markdown documents:
1. `plan.md` — an implementation plan detailed enough that a separate implementer agent (agy / Gemini 3.8 Flash High) can build v1 without guessing. Cover: language/runtime (Python 3.13 stdlib + SQLite 3.46 WAL is available on the box; no paid services; loopback/Unix socket only); data model (projects, tasks, dependencies, agents, adapters, approvals, events, leases); durable crash-safe storage; state machines with every transition; CLI command tree (`resource verb`), JSON output, stdin, exit-code table, idempotency keys, --dry-run, layered --help with examples; identity & authority model with real credentials/capabilities (not a role string), approval bound to operation hash, expiry, idempotency, audit, relayed user approvals and honest trust limits, Mesh gates vs external CLI permission flags (no claim of sandboxing external CLIs); lead agent assignment/global default/override/mid-project change; per-agent default model + allow-list, concurrency limit (any positive integer, invalid rejected), queueing; per-task workdir, git worktree+branch for code tasks, lead's own dir, merge flow (tests first, conflicts never silently overwritten); process supervision (process groups, timeouts, cancellation, leases with fencing tokens, heartbeat, crash recovery, no duplicate runs, no infinite retry; lead decides retry/reassign/escalate); notifications (event log + outbox with de-dup; concretely how each agent kind receives work — new headless run / session resume / blocking `wait`/inbox command; no magic push); adapters for all six with real flags below and verified/unverified/not-installed status; opt-in real-CLI smoke tests separate from fake-adapter unit tests (never present a fake as vendor support); security (no secrets in logs, redaction, file/socket permissions); phased milestones with a clearly sized FIRST phase; test plan (unit, integration, crash/recovery, concurrency, approval-forgery negative tests); open questions/risks.
2. `acceptance-checklist.md` — numbered, objectively checkable items (what to run/observe, expected result), mapped to requirement sections.
Be concrete and honest; where something is unknown say so and how it will be verified. English.

## Observed CLI facts on the box (2026-11-07)
- Factory droid 1.3.0: `droid exec [-f file] -m <model> -r <low|medium|high|xhigh|max> --auto <low|medium|high> --output-format text|json|stream-json --cwd <dir> -s <session-id>`; default read-only; `--skip-permissions-unsafe` exists; stream-json first event `system/init` echoes `model` and `reasoning_effort`. Model id `claude-opus-5-5`. Auth via FACTORY_API_KEY env or login session.
- Kiro CLI 3.1.0 (`$HOME/.local/bin/kiro-cli`): `kiro-cli chat --no-interactive --model <id> --effort <low..max> --trust-tools=<list> | -a --trust-all-tools --output-format stream-json --resume-id <id> --list-models -f json`; stream-json emits ACP events incl. `metadata.reasoning.effort`. Model id `claude-opus-5.5`. Invalid model → exit 1.
- Antigravity agy (1.4.8, droid observed 1.5.2 later): `agy -p "<prompt>" --model <id> --effort <...> --output-format text|json|stream-json --mode accept-edits|auto|plan --conversation <id> --dangerously-skip-permissions --sandbox --print-timeout`; `agy models` lists `gemini-3.8-flash-high`.
- Mistral Vibe 3.1.0 installed (`vibe`); flags to be verified by implementer via `vibe --help`.
- Hermes and OpenClaw: NOT installed on the box → status "not installed / unverified"; adapter interface only, no claimed support.

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
