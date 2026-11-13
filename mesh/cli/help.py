"""Help topics and layered help text for Agent Mesh CLI."""

import mesh.constants as c

TOP_HELP_EPILOG = """New here? Run `mesh help quickstart` for a short walkthrough.

Authentication Summary:
  User authentication:
    --user --passphrase-fd <fd>   Provide passphrase via file descriptor (or prompt on /dev/tty).
  Agent authentication:
    --token-file <path>           Authenticate as agent using token file (0600 mode).
  Run authentication:
    Run tokens are automatically provisioned in MESH_TOKEN env var inside run containers.

Examples:
  1. Initialize mesh and start daemon:
     mesh init
     mesh daemon start

  2. Create an agent and configure lead:
     mesh agent create --name agy-impl --adapter agy --default-model gemini-3.8-flash-high --max-concurrency 2
     mesh config set lead.default_agent '"agy-impl"' --user

  3. Submit a new task intake:
     mesh intake submit --title "Add CSV export" --goal "Export reports as CSV" --criterion "RFC4180 compliant"
"""

HELP_TOPICS = {
    "quickstart": """Agent Mesh Quickstart

One machine, one Unix user. The daemon (meshd) schedules tasks; agents talk to it
with the `mesh` CLI. Commands marked --user ask for your passphrase.

  1. Set up (init the home, start the daemon, create an inbox lead with a token file):
     mesh setup

  2. Create an inbox worker agent, its 0600 token file and a cheat-sheet in one step:
     mesh agent onboard worker1 --out ~/.config/agent-mesh/worker1.token --user
     (email-driven or slow agents: add --ack-timeout 2h; the default window is 10 min)

  3. Submit a project for a git repository (the lead plans it). A repo project needs
     its base branch and the merge-gate test command (a JSON argv, or none):
     mesh intake submit --title "Add CSV export" --goal "Export reports as CSV" \
         --criterion "Tests pass" --repo ~/src/myapp --base-branch main \
         --test-command '["python3", "-m", "pytest", "-q"]' \
         --token-file ~/.config/agent-mesh/lead.token

  4. The lead creates a task for the worker ('-' reads the spec from stdin):
     mesh task create --project prj_123 --title "CSV export" --assignee worker1 --spec - \
         --token-file ~/.config/agent-mesh/lead.token < spec.md

  5. The worker picks it up, keeps its lease alive, and submits:
     W=~/.config/agent-mesh/worker1.token
     mesh inbox list --token-file $W
     mesh task start tsk_456 --token-file $W        # prints run id, fence, workspace, lease, session
     export MESH_SESSION=ses_0123456789abcdef       # the session it printed (run writes must send it)
     mesh task keepalive tsk_456 --run run_789 --fence 1 --token-file $W > keepalive.log 2>&1 &
     mesh task submit tsk_456 --run run_789 --fence 1 --summary "Done" \
         --deliverable '{"tests": "pass"}' --token-file $W

  6. The lead reviews, accepts (or rejects with feedback) and merges:
     mesh task accept tsk_456 --token-file ~/.config/agent-mesh/lead.token
     mesh task merge tsk_456 --token-file ~/.config/agent-mesh/lead.token

  7. Deliver the integration branch to the base branch:
     mesh project deliver prj_123 --user

Examples:
  mesh help quickstart
  mesh help workflow
  mesh doctor
""",
    "auth": f"""Agent Mesh Authentication & Threat Model

Authentication Mechanisms:
  - User: Authenticates with a high-entropy passphrase via /dev/tty or --passphrase-fd.
    Passphrase derives K_user using scrypt (N=32768, r=8, p=1, maxmem=64MB).
  - Agent tokens: Bearer tokens issued via 'mesh agent token issue <name>'. Stored as hashes only.
  - Run tokens: Ephemeral tokens scoped strictly to a specific run session; revoked when the run ends.

Security Boundaries and Same-UID Limitations:
  Mesh is not a security sandbox. Mesh does not sandbox external CLIs.
  On Linux without OS user separation, all processes run under the same OS user.
  A same-uid process can read other agents' Mesh credentials (and forge relayed approvals) unless OS-level separation is used.
  Never keep the passphrase in a file and keep token files outside every workspace (refused with TOKEN_FILE_INSECURE).
  A same-uid process can modify Mesh's own code or read the passphrase or K_user from memory during a direct request, after which forged direct approvals carry valid MACs and audit verify cannot be trusted.
  Mesh recommends a read-only install outside every workspace (chmod -R a-w <install_dir>).
  Mesh does not claim that executors cannot steal credentials, and does not claim that audit verify detects forgeries by a process that modified Mesh's code or captured the passphrase.
  Audit verify detects DB-only tampering when code and passphrase are uncompromised.

Examples:
  # Authenticate as user with passphrase on fd 3
  mesh config set lead.default_agent '"lead1"' --user --passphrase-fd 3 3<"$P"

  # Authenticate as agent with token file
  mesh intake submit --token-file ~/.config/agent.tok --title "New task"
""",
    "approvals": """Agent Mesh Approvals & Human-in-the-Loop Policies

Approval Categories:
  - project_control: pause, resume, cancel projects (default: direct_or_relayed)
  - permission_escalation: agent requesting elevated/unrestricted profile (default: direct_only)
  - retry_over_limit: exceeding max task retry attempts (default: direct_or_relayed)
  - spend_money: external API or service expenditure (advises direct_only)
  - deploy: production release actions (advises direct_only)
  - delete_external: deleting external resources or databases (advises direct_only)
  - config: modifying system settings (always direct_only, never relayable)

Channels:
  - direct: User approves directly with passphrase. Cryptographically signed with user MAC.
  - relayed: An agent with 'relay' capability relays an approval with --user-statement.
    Mesh cannot verify a relayed statement and advises direct_only for sensitive categories (spend_money, deploy, delete_external, permission_escalation).
  - delegated (opt-in: mesh config set approval.mode '"delegated"' --user): the project's current
    inbox lead lists `mesh approval pending`, asks the human, then decides with its own token and
    --human-confirmed "<the human's words>". Trusts the lead; recorded unverified with those words.
    direct_only categories (config, permission_escalation by default) still need the passphrase.

Examples:
  # User directly approves an operation:
  mesh approval approve apr_12345 --op-hash sha256:... --user --passphrase-fd 3 3<"$P"

  # Agent relays user approval:
  mesh approval approve apr_12345 --op-hash sha256:... --relayed --user-statement "Alice approved via chat" --token-file agent.tok

  # Delegated mode: the lead approves on the human's quoted reply:
  mesh approval approve apr_12345 --op-hash sha256:... --human-confirmed "Alice 09:12: yes, deploy" --token-file lead.tok
""",
    "exit-codes": f"""Exit Codes:

{c.EXIT_CODES_TABLE_MD}

Examples:
  # USAGE / VALIDATION error (exit code 2)
  mesh task create --bogus

  # NOT_FOUND error (exit code 5)
  mesh agent show no-such-agent --user
""",
    "json": """JSON Envelope and Input Conventions

JSON Envelope:
  Success format (exit code 0):
    {"ok": true, "data": {...}, "warnings": [], "request_id": "req_...", "idempotent_replay": false, "dry_run": false}

  Error format (exit code > 0):
    {"ok": false, "error": {"code": "CODE", "message": "...", "details": {...}}, "exit_code": N, "request_id": "req_..."}

Standard Input:
  Pass '--input -' to read parameters as a JSON object on stdin.
  Keys in stdin JSON correspond to command flag names with underscores (e.g. {"title": "x", "assignee": "fx"}).
  Providing the same parameter on both argv and stdin results in exit 2 DUPLICATE_PARAM.
  Unrecognized keys in stdin JSON result in exit 2 UNKNOWN_PARAM.

Examples:
  # Passing arguments via stdin JSON:
  echo '{"title": "Task 1", "spec": "Spec details", "project": "prj_1"}' | mesh task create --input -

  # Getting JSON output (default):
  mesh agent list
""",
    "workflow": """Agent Mesh End-to-End Workflow

Lifecycle:
  1. Intake: Any authorized agent submits a project goal and criteria via 'mesh intake submit'.
  2. Lead Assignment: Project lead breaks down the goal into tasks with dependencies ('mesh task create').
  3. Scheduling & Execution:
     - Tasks queue and promote based on priority and concurrency limits.
     - Executors (launched CLI processes or inbox long-pollers) work in isolated workspaces.
  4. Review:
     - Executor submits deliverables ('mesh task submit').
     - Lead reviews and accepts ('mesh task accept') or rejects with rework feedback ('mesh task reject').
  5. Completion: Lead verifies all criteria with evidence and completes the project ('mesh project complete').

Examples:
  # Submit project
  mesh intake submit --title "Feature X" --goal "Implement feature X" --criterion "Passes tests"

  # Lead creates task
  mesh task create --project prj_123 --title "Write unit tests" --spec "Cover core logic" --assignee fx
""",
    "adapters": """Agent Mesh Adapters

Supported CLI Adapters:
  - droid: Factory droid CLI
  - agy: Antigravity CLI
  - kiro: Kiro CLI
  - vibe: Mistral vibe CLI
  - hermes: Hermes agent (no launch support yet; use an inbox agent)
  - openclaw: OpenClaw agent (no launch support yet; use an inbox agent)
  Whether a vendor adapter may launch depends on its probe and smoke evidence and
  the adapter.evidence_mode setting; `mesh adapter list` shows launch_enabled and,
  when it is off, launch_reason.
  - none: Inbox-only agent delivery (no CLI launch)
  - fake: In-memory test double (enabled only when MESH_TESTING=1)

Permission Profiles vs Reasoning Effort:
  Permission profiles (readonly, edit, dev, elevated, unrestricted) govern tool permissions and autonomy.
  Reasoning effort (low, medium, high, max) selects model thinking depth.
  Mesh strictly separates profiles from effort and never conflates them.

Examples:
  # List adapters and probe evidence
  mesh adapter list

  # Inspect adapter capabilities
  mesh adapter show droid
""",
    "env": """Agent Mesh Environment Variables

Configuration Variables:
  MESH_HOME: Path to Mesh storage directory (default: ~/.agent-mesh). An empty
             MESH_HOME (MESH_HOME="") counts as unset and uses the default;
             an empty --home is refused.
  MESH_TESTING: Set to '1' to enable testing mode (enables fake adapter and lower minimum timeouts).
  MESH_TOKEN: Set by runwrap inside executor child environments with the run token.

Local Unix Socket Architecture:
  Mesh communicates exclusively via a local Unix domain socket ($MESH_HOME/run/mesh.sock).
  There are no network ports, no TCP/IP listeners, no HTTP servers, and no remote endpoints.
  Local Unix domain socket only; no remote network endpoint options exist.

Examples:
  # Use custom home directory
  export MESH_HOME=/tmp/mh-custom
  mesh init

  # Enable testing mode for automated test suites
  MESH_TESTING=1 mesh daemon run
""",
    "testing": """Agent Mesh Testing Architecture

Testing Principles:
  - Standard library only: Tests run with standard python3 -m unittest. No pip packages or network calls.
  - Isolated temporary homes: Tests use /tmp/mh-XXXXXX (mode 0700) and isolated Unix sockets.
  - Test double: Tests run offline using the 'fake' adapter with testing hooks.
  - Tripwire protection: Shims in tests/shims/ detect and block accidental calls to real vendor CLIs.

Examples:
  # Run full test suite
  bash scripts/test.sh

  # Run only unit tests
  python3 -m unittest discover -s tests/unit -t .
""",
}


def get_topic_help(topic: str) -> str:
    """Return help text for a specific topic."""
    if topic in HELP_TOPICS:
        return HELP_TOPICS[topic]
    topics_list = ", ".join(sorted(HELP_TOPICS.keys()))
    return f"Unknown help topic '{topic}'. Available topics: {topics_list}\n\nExamples:\n  mesh help exit-codes\n  mesh help auth\n"
