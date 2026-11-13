"""Authoritative settings registry and validation per plan §5.7."""

import os
import stat
from typing import Any, Dict, List, Optional, Set, Tuple

import mesh.constants as c
from mesh.errors import MeshError
from mesh.core.paths import check_install_path_secure, get_install_root
from mesh.core.policy import parse_positive_int

PROFILES = {"readonly", "edit", "dev", "elevated", "unrestricted"}

#: The lead-turn retry backoff of plan §12.4 (30 s, 120 s, 600 s), one entry per
#: failed attempt. `lead.wake_backoff_s` may override it in testing mode only.
LEAD_WAKE_BACKOFF_S = (30, 120, 600)

APPROVAL_CATEGORIES = {
    "config": "direct_only",  # fixed
    "lead_change": "direct_or_relayed",
    "project_control": "direct_or_relayed",
    "scope_change": "direct_or_relayed",
    "permission_escalation": "direct_only",
    "retry_over_limit": "direct_or_relayed",
    "delete": "direct_or_relayed",
    "deliver_to_base": "not_required",
    "spend_money": "direct_or_relayed",
    "deploy": "direct_or_relayed",
    "delete_external": "direct_or_relayed",
    "external_other": "direct_or_relayed",
}


def is_testing_mode() -> bool:
    """Check if MESH_TESTING=1 is enabled."""
    return os.environ.get("MESH_TESTING") == "1"


def get_default_settings(home: str) -> Dict[str, Any]:
    """Return all default settings per plan §5.7 table."""
    defaults: Dict[str, Any] = {
        "lead.default_agent": None,
        "lead.wake_debounce_s": 5,
        "lead.profile": "readonly",
        "lead.turn_timeout_s": 1800,
        "scheduler.max_total_runs": None,
        "workspaces.root": os.path.join(home, "workspaces"),
        "task.default_timeout_s": 3600,
        "task.default_max_attempts": 3,
        "task.max_attempts_cap": 10,
        "inbox.ack_timeout_s": 600,
        "lease.ttl_s": 120,
        "kill.grace_s": 15,
        "kill.post_submit_grace_s": 60,
        "outbox.max_attempts": 3,
        "approval.ttl_seconds": 86400,
        # "passphrase": only the user (passphrase) or a relayer outside the project
        # decides approvals. "delegated": the project's current inbox lead asks the human
        # and records the decision with the human's own words (--human-confirmed).
        "approval.mode": "passphrase",
        "notify.command": None,
        # Productize: "warn" lets a vendor adapter launch without matching committed smoke
        # evidence (the gap is reported by probe/doctor and in the assign response);
        # "strict" is the M3 evidence gate. Testing mode defaults to strict so the
        # acceptance suite keeps exercising the gate; tests opt into warn explicitly.
        "adapter.evidence_mode": "strict" if is_testing_mode() else "warn",
        # Testing-only knob (see validate_setting): the passphrase lockout window.
        "auth.lockout_window_s": 300,
        # Testing-only knob (see validate_setting): the lead-turn retry backoff, which
        # plan §12.4 fixes at 30/120/600 s. Production always uses LEAD_WAKE_BACKOFF_S.
        "lead.wake_backoff_s": list(LEAD_WAKE_BACKOFF_S),
    }
    for cat, default_policy in APPROVAL_CATEGORIES.items():
        defaults[f"approval.policy.{cat}"] = default_policy
    return defaults


#: Settings whose value is a plain string (or null). `mesh config set` accepts a bare
#: word for them (`lead.default_agent lead1`), see `string_setting`.
STRING_SETTING_KEYS = frozenset({
    "lead.default_agent", "lead.profile", "workspaces.root", "adapter.evidence_mode", "approval.mode",
})


def string_setting(key: str) -> bool:
    return key in STRING_SETTING_KEYS or key.startswith("approval.policy.")


#: Per-agent inbox ack window (task.offered -> `task start`). Stored as a settings row
#: keyed by agent id (``agent.<agt_id>.inbox.ack_timeout_s``) so the schema does not
#: change; it is written only by `agent create/update --ack-timeout` and is not a
#: `config` key. Absent -> the global ``inbox.ack_timeout_s``.
AGENT_ACK_TIMEOUT_KEY = "agent.{agent_id}.inbox.ack_timeout_s"


def agent_ack_timeout_key(agent_id: str) -> str:
    return AGENT_ACK_TIMEOUT_KEY.format(agent_id=agent_id)


def ack_timeout_range() -> Tuple[int, int]:
    """Same range as the global inbox.ack_timeout_s."""
    return (1 if is_testing_mode() else 10, 86400)


def validate_ack_timeout(val: Any) -> int:
    """`--ack-timeout` value (a duration such as 30m / 2h or seconds) -> seconds."""
    from mesh.core.clock import parse_duration

    lo, hi = ack_timeout_range()
    try:
        secs = parse_duration(val)
    except Exception:
        secs = None
    if isinstance(val, bool) or secs is None or not (lo <= secs <= hi):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"--ack-timeout must be a duration between {lo}s and {hi}s (e.g. 30m, 2h), or 'default'",
            details={"param": "ack_timeout", "expected": f"{lo}..{hi} seconds", "got": val},
        )
    return int(secs)


def get_agent_ack_timeout_s(conn: Any, agent_id: str) -> Optional[int]:
    """The agent's own ack window in seconds, or None when it uses the global one."""
    import json as _json

    row = conn.execute("SELECT value_json FROM settings WHERE key = ?;",
                       (agent_ack_timeout_key(agent_id),)).fetchone()
    if not row:
        return None
    try:
        val = _json.loads(row["value_json"])
    except Exception:
        return None
    return int(val) if isinstance(val, int) and not isinstance(val, bool) else None


def set_agent_ack_timeout_s(conn: Any, agent_id: str, secs: Optional[int], now: str, by: str) -> None:
    """Store (or with None, clear) the agent's ack window. Caller owns the transaction."""
    import json as _json

    key = agent_ack_timeout_key(agent_id)
    if secs is None:
        conn.execute("DELETE FROM settings WHERE key = ?;", (key,))
        return
    conn.execute(
        """INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
               updated_at = excluded.updated_at, updated_by = excluded.updated_by;""",
        (key, _json.dumps(int(secs)), now, by),
    )


def validate_setting(
    key: str,
    val: Any,
    home: str,
    current_settings: Optional[Dict[str, Any]] = None,
    install_root: Optional[str] = None,
    db_conn: Optional[Any] = None,
) -> Any:
    """Validate a setting key and value per plan §5.7.

    Raises:
        MeshError(INVALID_CONCURRENCY) for scheduler.max_total_runs
        MeshError(INSTALL_PATH_INSECURE) for workspaces.root overlap
        MeshError(INVALID_SETTING) for any other validation failure
    """
    testing = is_testing_mode()
    defaults = get_default_settings(home)

    if key not in defaults:
        raise MeshError(
            code=c.INVALID_SETTING,
            exit_code=c.EXIT_USAGE,
            message=f"Unknown setting key: '{key}'",
            details={"key": key, "expected": "known setting key", "got": key},
        )

    # 1. scheduler.max_total_runs (int or null, 1..9223372036854775807)
    if key == "scheduler.max_total_runs":
        if val is None:
            return None
        return parse_positive_int(val, param_name="scheduler.max_total_runs")

    # 2. lead.default_agent (agent name or null)
    if key == "lead.default_agent":
        if val is None:
            return None
        if not isinstance(val, str) or not val.strip():
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' must be an agent name string or null",
                details={"key": key, "expected": "agent name or null", "got": val},
            )
        agent_name = val.strip()
        if db_conn is not None:
            cur = db_conn.execute(
                """SELECT p.disabled_at, a.can_lead
                   FROM principals p
                   JOIN agents a ON p.id = a.id
                   WHERE p.name = ?;""",
                (agent_name,),
            )
            row = cur.fetchone()
            if not row:
                raise MeshError(
                    code=c.INVALID_SETTING,
                    exit_code=c.EXIT_USAGE,
                    message=f"Agent '{agent_name}' for setting '{key}' does not exist",
                    details={"key": key, "expected": "enabled agent with can_lead", "got": agent_name},
                )
            if row["disabled_at"] is not None:
                raise MeshError(
                    code=c.INVALID_SETTING,
                    exit_code=c.EXIT_USAGE,
                    message=f"Agent '{agent_name}' for setting '{key}' is disabled",
                    details={"key": key, "expected": "enabled agent with can_lead", "got": agent_name},
                )
            if not row["can_lead"]:
                raise MeshError(
                    code=c.INVALID_SETTING,
                    exit_code=c.EXIT_USAGE,
                    message=f"Agent '{agent_name}' for setting '{key}' does not have can_lead capability",
                    details={"key": key, "expected": "enabled agent with can_lead", "got": agent_name},
                )
        return agent_name

    # 3. lead.profile (profile name)
    if key == "lead.profile":
        if not isinstance(val, str) or val not in PROFILES:
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' must be one of {sorted(PROFILES)}",
                details={"key": key, "expected": sorted(PROFILES), "got": val},
            )
        return val

    # 4. workspaces.root (absolute path, existing dir owned by current uid, mode 0700)
    if key == "workspaces.root":
        if not isinstance(val, str) or not os.path.isabs(val):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' must be an absolute path",
                details={"key": key, "expected": "absolute path", "got": val},
            )
        # Check bidirectional install-path check (review-r3 minor 4)
        if install_root is None:
            install_root = get_install_root()
        check_install_path_secure(
            install_root=install_root,
            home=home,
            workspaces_root=val,
        )

        if not os.path.exists(val):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Directory for setting '{key}' does not exist: '{val}'",
                details={"key": key, "expected": "existing directory", "got": val},
            )
        st = os.stat(val)
        if not stat.S_ISDIR(st.st_mode):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Path for setting '{key}' is not a directory: '{val}'",
                details={"key": key, "expected": "existing directory", "got": val},
            )
        if st.st_uid != os.getuid():
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Directory for setting '{key}' is not owned by current uid",
                details={"key": key, "expected": f"owner uid {os.getuid()}", "got": st.st_uid},
            )
        if (st.st_mode & 0o077) != 0:
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Directory for setting '{key}' must have mode 0700",
                details={"key": key, "expected": "0700", "got": oct(stat.S_IMODE(st.st_mode))},
            )
        return val

    # 5. notify.command (JSON argv array of strings or null)
    if key == "notify.command":
        if val is None:
            return None
        if not isinstance(val, list) or len(val) == 0 or not all(isinstance(x, str) for x in val):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' must be a non-empty array of strings or null",
                details={"key": key, "expected": "non-empty array of strings or null", "got": val},
            )
        return val

    # 5b. adapter.evidence_mode
    if key == "adapter.evidence_mode":
        if val not in ("warn", "strict"):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' must be \"warn\" or \"strict\"",
                details={"key": key, "expected": ["warn", "strict"], "got": val},
            )
        return val

    # 5c. approval.mode
    if key == "approval.mode":
        if val not in ("passphrase", "delegated"):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' must be \"passphrase\" or \"delegated\"",
                details={"key": key, "expected": ["passphrase", "delegated"], "got": val},
            )
        return val

    # 6. approval.policy.<category>
    if key.startswith("approval.policy."):
        cat = key.split(".", 2)[2]
        if cat == "config":
            if val != "direct_only":
                raise MeshError(
                    code=c.INVALID_SETTING,
                    exit_code=c.EXIT_USAGE,
                    message="Policy for 'config' is fixed to 'direct_only' and cannot be changed",
                    details={"key": key, "expected": "direct_only", "got": val},
                )
            return "direct_only"

        allowed = ["direct_only", "direct_or_relayed"]
        if cat == "deliver_to_base":
            allowed.append("not_required")

        if not isinstance(val, str) or val not in allowed:
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Policy for '{cat}' must be one of {allowed}",
                details={"key": key, "expected": allowed, "got": val},
            )
        return val

    # 7. lead.wake_backoff_s (testing-only: a JSON array of 3 ints, each 0..600).
    # Plan §12.4 fixes the backoff at 30/120/600 s; outside testing mode the value
    # cannot be changed, exactly like auth.lockout_window_s.
    if key == "lead.wake_backoff_s":
        if not testing:
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' can only be changed in testing mode",
                details={"key": key, "expected": "testing mode (MESH_TESTING=1)", "got": val},
            )
        if (not isinstance(val, list) or len(val) != len(LEAD_WAKE_BACKOFF_S)
                or any(isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= 600) for v in val)):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' must be an array of {len(LEAD_WAKE_BACKOFF_S)} integers in 0..600",
                details={"key": key, "expected": "array of 3 integers in 0..600", "got": val},
            )
        return list(val)

    # 8. Integer settings
    # Strict integer check: reject bool, float, str, etc.
    if isinstance(val, bool) or not isinstance(val, int) or isinstance(val, float):
        raise MeshError(
            code=c.INVALID_SETTING,
            exit_code=c.EXIT_USAGE,
            message=f"Setting '{key}' must be an integer, got {type(val).__name__}",
            details={"key": key, "expected": "integer", "got": val},
        )

    # auth.lockout_window_s is a testing-only knob: outside testing mode it is
    # fixed at the plan §7.1 value of 300 s and cannot be changed.
    if key == "auth.lockout_window_s":
        if not testing:
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' can only be changed in testing mode",
                details={"key": key, "expected": "testing mode (MESH_TESTING=1)", "got": val},
            )
        if not (1 <= val <= 3600):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' out of range: got {val}",
                details={"key": key, "expected": "1..3600", "got": val},
            )
        return val

    int_ranges: Dict[str, Tuple[int, int]] = {
        "lead.wake_debounce_s": (0 if testing else 0, 3600),
        "lead.turn_timeout_s": (1 if testing else 60, 86400),
        "task.default_timeout_s": (1 if testing else 60, 604800),
        "task.max_attempts_cap": (1, 100),
        "inbox.ack_timeout_s": (1 if testing else 10, 86400),
        "lease.ttl_s": (1 if testing else 10, 3600),
        "kill.grace_s": (1 if testing else 1, 300),
        "kill.post_submit_grace_s": (0 if testing else 0, 3600),
        "outbox.max_attempts": (1, 10),
        "approval.ttl_seconds": (1 if testing else 60, 2592000),
    }

    if key in int_ranges:
        min_v, max_v = int_ranges[key]
        if not (min_v <= val <= max_v):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' out of range: got {val}",
                details={"key": key, "expected": f"{min_v}..{max_v}", "got": val},
            )
        return val

    if key == "task.default_max_attempts":
        # Upper bound is task.max_attempts_cap
        max_cap = 10
        if current_settings and "task.max_attempts_cap" in current_settings:
            max_cap = current_settings["task.max_attempts_cap"]
        if not (1 <= val <= max_cap):
            raise MeshError(
                code=c.INVALID_SETTING,
                exit_code=c.EXIT_USAGE,
                message=f"Setting '{key}' out of range: got {val}",
                details={"key": key, "expected": f"1..{max_cap}", "got": val},
            )
        return val

    # Should not reach here
    return val
