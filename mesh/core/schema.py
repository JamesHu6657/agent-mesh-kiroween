"""Authoritative database schema and ordered migrations per plan §5.3 and §5.5.

All tables use STRICT mode.
The schema version is tracked in meta(key='schema_version').
"""

from typing import List, NamedTuple


class Migration(NamedTuple):
    version: int
    name: str
    statements: List[str]


# Authoritative DDL for migration version 1
INITIAL_SCHEMA_STATEMENTS: List[str] = [
    # Meta & Settings & Boots
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;",
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL) STRICT;",
    "CREATE TABLE boots (id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at TEXT NOT NULL, stopped_at TEXT, crashed INTEGER NOT NULL DEFAULT 0) STRICT;",

    # Principals & Credentials & Auth Failures
    "CREATE TABLE principals (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('user','agent','system')), name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, disabled_at TEXT) STRICT;",
    "CREATE TABLE credentials (id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id), kind TEXT NOT NULL CHECK (kind IN ('passphrase','agent_token','run_token')), lookup TEXT UNIQUE, secret_hash BLOB NOT NULL, salt BLOB, kdf_params TEXT, scope_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT) STRICT;",
    "CREATE TABLE auth_failures (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, at TEXT NOT NULL, peer_pid INTEGER) STRICT;",

    # Agents & Adapter Probes
    "CREATE TABLE agents (id TEXT PRIMARY KEY REFERENCES principals(id), adapter TEXT NOT NULL, binary_path TEXT, delivery TEXT NOT NULL CHECK (delivery IN ('launch','inbox')), can_lead INTEGER NOT NULL DEFAULT 0 CHECK (can_lead IN (0,1)), capabilities_json TEXT NOT NULL DEFAULT '[\"intake\"]', default_model TEXT, allowed_models_json TEXT NOT NULL DEFAULT '[]', default_effort TEXT, allowed_efforts_json TEXT NOT NULL DEFAULT '[]', max_concurrency INTEGER NOT NULL CHECK (max_concurrency >= 1), default_profile TEXT NOT NULL DEFAULT 'edit', max_unapproved_profile TEXT NOT NULL DEFAULT 'dev', adapter_options_json TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;",
    "CREATE TABLE adapter_probes (adapter TEXT NOT NULL, probed_at TEXT NOT NULL, binary_path TEXT, version TEXT, help_sha256 TEXT, flags_json TEXT NOT NULL, models_json TEXT, status TEXT NOT NULL, PRIMARY KEY (adapter, probed_at)) STRICT;",

    # Projects, Acceptance Criteria, Project Deps, Lead Assignments
    "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('active','paused','completed','cancelled')), repo_path TEXT, base_branch TEXT, integration_branch TEXT, base_commit TEXT, test_command_json TEXT, test_timeout_s INTEGER NOT NULL DEFAULT 1800, origin_principal_id TEXT REFERENCES principals(id), origin_ref TEXT, created_by TEXT NOT NULL, created_via TEXT NOT NULL, completion_report_json TEXT, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT) STRICT;",
    "CREATE TABLE acceptance_criteria (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, superseded_at TEXT) STRICT;",
    "CREATE TABLE project_deps (project_id TEXT NOT NULL REFERENCES projects(id), depends_on_project_id TEXT NOT NULL REFERENCES projects(id), PRIMARY KEY (project_id, depends_on_project_id), CHECK (project_id <> depends_on_project_id)) STRICT;",
    "CREATE TABLE lead_assignments (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), agent_id TEXT NOT NULL REFERENCES agents(id), assigned_by TEXT NOT NULL, via TEXT NOT NULL, reason TEXT, started_at TEXT NOT NULL, ended_at TEXT, ended_reason TEXT, session_id TEXT) STRICT;",
    "CREATE UNIQUE INDEX one_current_lead ON lead_assignments(project_id) WHERE ended_at IS NULL;",

    # Tasks & Task Deps
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL, spec TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('code','general')), status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, assignee_agent_id TEXT REFERENCES agents(id), model TEXT, effort TEXT, profile TEXT NOT NULL, timeout_s INTEGER NOT NULL, idle_timeout_s INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, fence_seq INTEGER NOT NULL DEFAULT 0, queued_at TEXT, offered_at TEXT, workspace_path TEXT, branch TEXT, base_commit TEXT, last_session_id TEXT, deliverable_json TEXT, failure_json TEXT, merge_json TEXT, created_by TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;",
    "CREATE TABLE task_deps (task_id TEXT NOT NULL REFERENCES tasks(id), depends_on_task_id TEXT NOT NULL REFERENCES tasks(id), PRIMARY KEY (task_id, depends_on_task_id), CHECK (task_id <> depends_on_task_id)) STRICT;",

    # Runs & Leases
    "CREATE TABLE runs (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('task','lead_turn','merge_test','external')), task_id TEXT REFERENCES tasks(id), project_id TEXT NOT NULL REFERENCES projects(id), agent_id TEXT REFERENCES agents(id), attempt INTEGER, fence INTEGER NOT NULL, adapter TEXT NOT NULL, model TEXT, effort TEXT, profile TEXT, status TEXT NOT NULL CHECK (status IN ('starting','running','exited','killed','lost','spawn_failed')), boot_id TEXT NOT NULL, wrapper_pid INTEGER, pgid INTEGER, proc_starttime INTEGER, run_dir TEXT NOT NULL, argv_redacted_json TEXT NOT NULL DEFAULT '[]', started_at TEXT, deadline_at TEXT, ended_at TEXT, exit_code INTEGER, signal INTEGER, end_reason TEXT, session_id TEXT, result_json TEXT, lead_assignment_id TEXT) STRICT;",
    "CREATE UNIQUE INDEX one_active_run_per_task ON runs(task_id) WHERE status IN ('starting','running') AND kind IN ('task','external');",
    "CREATE TABLE leases (key TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id), holder_kind TEXT NOT NULL CHECK (holder_kind IN ('meshd','agent')), holder TEXT NOT NULL, agent_id TEXT, fence INTEGER NOT NULL, slots_in_use_at_acquire INTEGER, limit_at_acquire INTEGER, total_at_acquire INTEGER, total_limit_at_acquire INTEGER, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL) STRICT;",

    # Approvals
    "CREATE TABLE approvals (id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, category TEXT NOT NULL, op_type TEXT NOT NULL, op_json TEXT NOT NULL, op_hash TEXT NOT NULL, summary TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','expired','consumed','cancelled')), decided_by TEXT, decided_via TEXT CHECK (decided_via IN ('direct','relayed')), relayed_by TEXT, relayer_credential_id TEXT, user_statement TEXT, evidence_ref TEXT, decision_note TEXT, decided_at TEXT, user_mac TEXT, consumed_at TEXT, consumed_by TEXT, consumed_request_id TEXT) STRICT;",
    "CREATE UNIQUE INDEX one_open_approval_per_op ON approvals(op_hash) WHERE status IN ('pending','approved');",

    # Events & Append-Only Triggers
    "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL, type TEXT NOT NULL, project_id TEXT, task_id TEXT, run_id TEXT, approval_id TEXT, actor_id TEXT NOT NULL, actor_kind TEXT NOT NULL, via TEXT NOT NULL, relayed_by TEXT, credential_id TEXT, from_state TEXT, to_state TEXT, reason TEXT, payload_json TEXT NOT NULL DEFAULT '{}', request_id TEXT, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, user_mac TEXT) STRICT;",
    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;",

    # Outbox & Idempotency
    "CREATE TABLE outbox (id TEXT PRIMARY KEY, event_seq INTEGER NOT NULL REFERENCES events(seq), recipient_id TEXT NOT NULL REFERENCES principals(id), channel TEXT NOT NULL CHECK (channel IN ('inbox','lead_wake')), dedup_key TEXT NOT NULL UNIQUE, project_id TEXT, type TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending','delivering','delivered','acked','dead','superseded')), attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, delivered_at TEXT, acked_at TEXT, delivering_run_id TEXT, last_error TEXT, created_at TEXT NOT NULL) STRICT;",
    "CREATE TABLE idempotency (principal_id TEXT NOT NULL, key TEXT NOT NULL, cmd TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL, exit_code INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (principal_id, key)) STRICT;",

    # Initial meta row for schema version
    "INSERT INTO meta (key, value) VALUES ('schema_version', '1');",
]

# Migration version 2: Chat-First project conversation, structured plans, and pre-dispatch gate
MIGRATION_V2_STATEMENTS: List[str] = [
    # 1. Project plans
    """CREATE TABLE plans (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        version INTEGER NOT NULL CHECK (version >= 1),
        status TEXT NOT NULL CHECK (status IN (
            'proposed',
            'approved_pending_dispatch',
            'dispatched',
            'revising',
            'superseded',
            'rejected',
            'cancelled'
        )),
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        steps_json TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        proposed_by_agent_id TEXT NOT NULL REFERENCES agents(id),
        proposed_by_run_id TEXT REFERENCES runs(id),
        proposed_event_seq INTEGER NOT NULL,
        approval_id TEXT REFERENCES approvals(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        dispatched_at TEXT,
        UNIQUE(project_id, version)
    ) STRICT;""",
    "CREATE UNIQUE INDEX one_proposed_plan_per_project ON plans(project_id) WHERE status IN ('proposed', 'approved_pending_dispatch');",
    "CREATE INDEX idx_plans_project_created ON plans(project_id, version DESC);",

    # 2. Project chat messages
    """CREATE TABLE chat_messages (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        event_seq INTEGER NOT NULL UNIQUE REFERENCES events(seq),
        role TEXT NOT NULL CHECK (role IN ('user', 'lead', 'system')),
        kind TEXT NOT NULL CHECK (kind IN (
            'text',
            'plan_card',
            'approval_card',
            'escalation_card',
            'dispatch_notice',
            'clarification'
        )),
        actor_id TEXT NOT NULL REFERENCES principals(id),
        actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'agent', 'system')),
        via TEXT NOT NULL,
        run_id TEXT REFERENCES runs(id),
        plan_id TEXT REFERENCES plans(id),
        approval_id TEXT REFERENCES approvals(id),
        task_id TEXT REFERENCES tasks(id),
        body TEXT NOT NULL,
        meta_json TEXT NOT NULL DEFAULT '{}',
        consumed_by_approval_id TEXT REFERENCES approvals(id),
        created_at TEXT NOT NULL
    ) STRICT;""",
    "CREATE INDEX idx_chat_messages_project_seq ON chat_messages(project_id, event_seq ASC);",
    "CREATE UNIQUE INDEX idx_chat_messages_consumed_apr ON chat_messages(consumed_by_approval_id) WHERE consumed_by_approval_id IS NOT NULL;",

    # 3. Persistent narrowly-scoped standing grants (created via card 'always_allow')
    """CREATE TABLE standing_grants (
        id TEXT PRIMARY KEY,
        project_id TEXT REFERENCES projects(id),
        category TEXT NOT NULL CHECK (category IN ('permission_escalation', 'retry_over_limit')),
        scope_agent_id TEXT REFERENCES agents(id),
        scope_profile TEXT CHECK (scope_profile IS NULL OR scope_profile IN ('readonly','edit','dev','elevated','unrestricted')),
        -- Vestigial column kept for additive v2 schema compatibility (always NULL; 'config' standing grants were removed; config writes remain direct-user-only)
        scope_config_key TEXT,
        scope_key TEXT NOT NULL,
        source_approval_id TEXT REFERENCES approvals(id),
        created_by TEXT NOT NULL REFERENCES principals(id),
        created_via TEXT NOT NULL CHECK (created_via = 'direct'),
        created_at TEXT NOT NULL,
        user_mac TEXT NOT NULL,
        revoked_at TEXT,
        revoked_by TEXT REFERENCES principals(id),
        revoked_reason TEXT,
        revoked_user_mac TEXT
    ) STRICT;""",
    "CREATE UNIQUE INDEX one_active_standing_grant_per_scope ON standing_grants(scope_key) WHERE revoked_at IS NULL;",
    "CREATE INDEX idx_standing_grants_project ON standing_grants(project_id, category, revoked_at);",
    "CREATE INDEX idx_events_type_project ON events(type, project_id, seq);",

    # 4. Incremental columns on tasks and projects (existing rows stay backward-compatible)
    "ALTER TABLE tasks ADD COLUMN plan_id TEXT REFERENCES plans(id);",
    "ALTER TABLE tasks ADD COLUMN plan_step_key TEXT;",
    "CREATE INDEX idx_tasks_plan ON tasks(plan_id, plan_step_key);",
    "ALTER TABLE projects ADD COLUMN plan_gate_mode TEXT NOT NULL DEFAULT 'off' CHECK (plan_gate_mode IN ('off', 'require_approval'));",
    "ALTER TABLE projects ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'plan' CHECK (permission_mode IN ('supervised', 'plan', 'auto'));",

    # 4. Backfill v2 default settings for already-initialized v1 databases (fresh init_db leaves settings empty for init_mesh_home)
    """INSERT OR IGNORE INTO settings (key, value_json, updated_at, updated_by)
       SELECT 'approval.policy.plan_dispatch', '"direct_only"',
              COALESCE((SELECT updated_at FROM settings LIMIT 1), '1970-01-01T00:00:00.000Z'), id
       FROM principals WHERE kind = 'user' AND EXISTS (SELECT 1 FROM settings LIMIT 1) LIMIT 1;""",
    """INSERT OR IGNORE INTO settings (key, value_json, updated_at, updated_by)
       SELECT 'approval.policy.step_dispatch', '"direct_only"',
              COALESCE((SELECT updated_at FROM settings LIMIT 1), '1970-01-01T00:00:00.000Z'), id
       FROM principals WHERE kind = 'user' AND EXISTS (SELECT 1 FROM settings LIMIT 1) LIMIT 1;""",
    """INSERT OR IGNORE INTO settings (key, value_json, updated_at, updated_by)
       SELECT 'lead.chat_wake_debounce_s', '0',
              COALESCE((SELECT updated_at FROM settings LIMIT 1), '1970-01-01T00:00:00.000Z'), id
       FROM principals WHERE kind = 'user' AND EXISTS (SELECT 1 FROM settings LIMIT 1) LIMIT 1;""",
    """INSERT OR IGNORE INTO settings (key, value_json, updated_at, updated_by)
       SELECT 'project.default_plan_gate', '"off"',
              COALESCE((SELECT updated_at FROM settings LIMIT 1), '1970-01-01T00:00:00.000Z'), id
       FROM principals WHERE kind = 'user' AND EXISTS (SELECT 1 FROM settings LIMIT 1) LIMIT 1;""",

    # Bump schema version
    "UPDATE meta SET value = '2' WHERE key = 'schema_version';",
]

MIGRATIONS: List[Migration] = [
    Migration(
        version=1,
        name="initial_schema",
        statements=INITIAL_SCHEMA_STATEMENTS,
    ),
    Migration(
        version=2,
        name="chat_and_plans",
        statements=MIGRATION_V2_STATEMENTS,
    ),
]

CURRENT_SCHEMA_VERSION = 2
