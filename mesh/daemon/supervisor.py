"""Launch-mode runs: spawn (§11.1), watching (§11.3) and termination (§11.4).

Spawn is TX A, effect, TX B (or spawn_failed). The watch pass runs every
scheduler tick; terminations (timeout, idle timeout, cancel, post-submit grace,
project cancel, leftovers at exit) are advanced by the watch pass and by
meshd's 200 ms termination loop (``advance_terminations``).
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Set, Tuple

import mesh.constants as c
from mesh.adapters.base import CommandSpec, RunRequest
from mesh.adapters.registry import get_adapter
from mesh.core.auth import generate_run_token
from mesh.core.clock import now_iso_utc
from mesh.core.db import transaction
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.core.leases import get_lease_ttl_s
from mesh.core.paths import get_install_root
from mesh.core.clock import parse_utc
from mesh.core.proc import ScanResult, get_proc_stat, is_tgid_alive
from mesh.core.prompts import (
    PROMPT_LIMIT_BYTES,
    parse_deliverable,
    pointer_prompt,
    prompt_strategy,
    read_rework_feedback,
    render_executor_prompt,
)
from mesh.core.redact import redact_value, register_secret, unregister_secret
from mesh.core.repos import submission_git_info
from mesh.core.workspaces import (
    WorkspaceError,
    plan_code_workspace,
    run_code_workspace_plan,
    compute_task_workspace_path,
    ensure_run_dir,
    ensure_workspace_dir,
    get_workspaces_root,
)
from mesh.daemon import children, terminate

RUN_TOKEN_EXPIRY_GRACE_S = 300
DEFAULT_TIMEOUT_S = 600
STDERR_TAIL_LINES = 50


def _get_current_boot_id(conn: sqlite3.Connection) -> str:
    cur = conn.execute("SELECT id FROM boots WHERE stopped_at IS NULL ORDER BY started_at DESC LIMIT 1;")
    row = cur.fetchone()
    if row:
        return row[0]
    cur2 = conn.execute("SELECT id FROM boots ORDER BY started_at DESC LIMIT 1;")
    row2 = cur2.fetchone()
    if row2:
        return row2[0]
    return generate_id("boot")


def get_run_timeout_s(conn: sqlite3.Connection) -> int:
    """Return the run timeout in seconds (setting ``run.timeout_s``, default 600)."""
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'run.timeout_s';")
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            return int(json.loads(row["value_json"]))
        except Exception:
            return DEFAULT_TIMEOUT_S
    return DEFAULT_TIMEOUT_S


def get_idle_timeout_s(conn: sqlite3.Connection) -> int:
    """Return the idle timeout in seconds (setting ``run.idle_timeout_s``, default 0 = off)."""
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'run.idle_timeout_s';")
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            return int(json.loads(row["value_json"]))
        except Exception:
            return 0
    return 0


def count_active_runs(conn: sqlite3.Connection, agent_id: Optional[str] = None) -> int:
    """Count runs that occupy a concurrency slot (task and external runs)."""
    if agent_id is None:
        cur = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status IN ('starting', 'running') AND kind IN ('task', 'external');"
        )
    else:
        cur = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE agent_id = ? AND status IN ('starting', 'running') "
            "AND kind IN ('task', 'external');",
            (agent_id,),
        )
    return cur.fetchone()[0]


def get_global_run_cap(conn: sqlite3.Connection) -> Optional[int]:
    """Return ``scheduler.max_total_runs`` or None when unset."""
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'scheduler.max_total_runs';")
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            return int(json.loads(row["value_json"]))
        except Exception:
            return None
    return None


def build_child_env(
    base_env: Dict[str, str],
    token: str,
    run_id: str,
    fence: int,
    task_id: str,
    project_id: str,
    socket_path: str,
) -> Dict[str, str]:
    """Build the wrapper environment per plan §11.1 step 2.

    meshd's env minus every ``MESH_*`` var, plus the run credentials, with
    ``PATH`` prefixed by ``<repo>/bin`` so the child can call ``mesh``.
    """
    env = c.build_external_env(base_env)
    env["MESH_TOKEN"] = token
    env["MESH_RUN_ID"] = run_id
    env["MESH_FENCE"] = str(fence)
    env["MESH_TASK_ID"] = task_id
    env["MESH_PROJECT_ID"] = project_id
    env["MESH_SOCKET"] = socket_path
    install_root = get_install_root()
    env["PATH"] = os.path.join(install_root, "bin") + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = install_root
    return env


def build_lead_child_env(
    base_env: Dict[str, str],
    token: str,
    run_id: str,
    project_id: str,
    socket_path: str,
) -> Dict[str, str]:
    """Build a lead turn's wrapper environment (§7.1, §11.1 step 2).

    Same shape as `build_child_env`, without the task-only variables: a lead turn has
    no task and no fence, so `MESH_TASK_ID` and `MESH_FENCE` are not set at all.
    """
    env = c.build_external_env(base_env)
    env["MESH_TOKEN"] = token
    env["MESH_RUN_ID"] = run_id
    env["MESH_PROJECT_ID"] = project_id
    env["MESH_SOCKET"] = socket_path
    install_root = get_install_root()
    env["PATH"] = os.path.join(install_root, "bin") + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = install_root
    return env


def build_spec(
    argv: List[str],
    cwd: str,
    stdin: str,
    redact_env_names: List[str],
    idle_timeout_s: int = 0,
) -> Dict[str, Any]:
    """Build ``spec.json``: no env values and no secrets (plan §11.1, §15.1)."""
    return {
        "argv": list(argv),
        "cwd": cwd,
        "stdin": stdin,
        "redact_env_names": list(redact_env_names),
        "idle_timeout_s": idle_timeout_s,
    }


def _write_text(path: str, text: str, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _write_json(path: str, obj: Any, mode: int = 0o600) -> None:
    _write_text(path, json.dumps(obj, sort_keys=True) + "\n", mode)


def _criteria_for_project(conn: sqlite3.Connection, project_id: str) -> List[str]:
    cur = conn.execute(
        "SELECT text FROM acceptance_criteria WHERE project_id = ? AND superseded_at IS NULL "
        "ORDER BY ordinal ASC, id ASC;",
        (project_id,),
    )
    return [row["text"] for row in cur.fetchall()]


def _fail_task_spawn(
    conn: sqlite3.Connection,
    run_id: str,
    task_id: str,
    project_id: str,
    fence: int,
    attempt: int,
    max_attempts: int,
    message: str,
    after_popen: Optional[Dict[str, Any]] = None,
) -> None:
    """Mark a run ``spawn_failed`` and fail the task (plan §11.1 step 4). ``after_popen``:
    round 7, the process had been created and was killed first (its pid, pgid, starttime
    and any survivors are recorded)."""
    now = now_iso_utc()
    conn.execute("DELETE FROM leases WHERE key = ?;", (f"task:{task_id}",))
    result: Dict[str, Any] = {"reason": "spawn_failed", "message": message}
    if after_popen:
        result["after_popen"] = after_popen
    conn.execute(
        "UPDATE runs SET status = 'spawn_failed', end_reason = 'spawn_failed', ended_at = ?, result_json = ? "
        "WHERE id = ?;",
        (now, json.dumps(redact_value(result)), run_id),
    )
    if after_popen and after_popen.get("pid"):
        conn.execute("UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? WHERE id = ?;",
                     (after_popen["pid"], after_popen["pid"], after_popen.get("starttime"), run_id))
    failure = {"reason": "spawn_failed", "details": {"message": message, "run_id": run_id}}
    conn.execute(
        "UPDATE tasks SET status = 'failed', failure_json = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND status = 'running';",
        (json.dumps(failure), now, task_id),
    )
    record_event(
        conn=conn,
        type_="task.failed",
        actor_id="system",
        actor_kind="system",
        via="system",
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        from_state="running",
        to_state="failed",
        reason="spawn_failed",
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "reason": "spawn_failed",
            "details": {"message": message},
            "fence": fence,
            "attempt": attempt,
            "max_attempts": max_attempts,
        },
    )


def _has_rejection(conn: sqlite3.Connection, task_id: str) -> bool:
    """Keep the latest rejection context across failed rework attempts and retries."""
    row = conn.execute(
        "SELECT 1 FROM events WHERE task_id = ? AND type = 'task.rejected' ORDER BY seq DESC LIMIT 1;",
        (task_id,),
    ).fetchone()
    return row is not None


def build_run_options(agent: Mapping[str, Any], run_id: str) -> Dict[str, Any]:
    """Pass the configured binary, actual run id and adapter options to build()."""
    options: Dict[str, Any] = json.loads(agent["adapter_options_json"] or "{}")
    options["run_id"] = run_id
    if agent["binary_path"]:
        options["binary"] = agent["binary_path"]
    return options


def verified_resume_session(adapter: Any, session_id: Optional[str]) -> Optional[str]:
    """Resume only a captured id on an adapter with smoke-verified resume support."""
    cap = adapter.capabilities.get("resume")
    if cap and cap.supported == "yes" and cap.verification == "smoke":
        return session_id
    return None


def start_launch_run(
    conn: sqlite3.Connection,
    home: str,
    task: sqlite3.Row,
    agent: sqlite3.Row,
    testing_mode: bool = False,
    prepared: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Start one launch-mode run for a queued task (plan §11.1).

    Returns the new run id, or None when the task could not be started (the
    caller's transaction then still commits the failure it recorded).

    `prepared` is a code task's worktree from run_code_workspace_plan (m2-r4,
    §10.3): its path, branch and base commit are written in TX A.
    """
    task_id = task["id"]
    project_id = task["project_id"]
    adapter_id = agent["adapter"]
    profile = task["profile"] or agent["default_profile"] or "dev"
    model = task["model"] or agent["default_model"]
    effort = task["effort"] or agent["default_effort"]

    adapter = get_adapter(adapter_id, testing_mode=testing_mode,
                          options=json.loads(agent["adapter_options_json"] or "{}"))
    workspaces_root = get_workspaces_root(conn, home)
    fence = task["fence_seq"] + 1
    attempt = task["attempt"] + 1
    max_attempts = task["max_attempts"]
    run_id = generate_id("run")
    run_dir = os.path.join(home, "runs", run_id)
    ws_path = task["workspace_path"]
    if not ws_path:
        ws_path = compute_task_workspace_path(workspaces_root, project_id, task_id, attempt=attempt)
    branch = task["branch"]
    base_commit = task["base_commit"]
    if prepared is not None:
        ws_path = prepared["workspace_path"]
        branch = prepared["branch"]
        base_commit = prepared["base_commit"]

    # --- Render the prompt (needs the workspace path, so before TX A) ---
    project_row = conn.execute("SELECT id, name FROM projects WHERE id = ?;", (project_id,)).fetchone()
    rework = read_rework_feedback(conn, task_id) if _has_rejection(conn, task_id) else None
    prompt_text = render_executor_prompt(
        task=task,
        project=project_row,
        criteria=_criteria_for_project(conn, project_id),
        workspace=ws_path,
        attempt=attempt,
        max_attempts=max_attempts,
        rework=rework,
        branch=branch,
    )
    prompt_bytes = len(prompt_text.encode("utf-8"))
    strategy = prompt_strategy(adapter_id, profile, prompt_bytes, PROMPT_LIMIT_BYTES)
    if strategy == "too_long":
        # T6 at T5: no caller exists, so the task fails instead of launching (plan §12.5).
        now = now_iso_utc()
        details = {
            "bytes": prompt_bytes,
            "limit": PROMPT_LIMIT_BYTES,
            "adapter": adapter_id,
            "profile": profile,
        }
        failure = {"reason": "prompt_too_long", "details": details}
        with transaction(conn):
            _fail_prompt_too_long(conn, task, details, failure, now)
        return None

    return _spawn_run(conn, home, task, agent, adapter, adapter_id, profile, model, effort,
                      fence, attempt, max_attempts, run_id, run_dir, ws_path, prompt_text, strategy,
                      branch=branch, base_commit=base_commit)


def _fail_prompt_too_long(conn, task, details, failure, now) -> None:
    """T6 at T5 for an over-long prompt (plan §12.5); caller owns the transaction."""
    task_id = task["id"]
    project_id = task["project_id"]
    max_attempts = task["max_attempts"]
    cur = conn.execute(
        "UPDATE tasks SET status = 'failed', failure_json = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND status = 'queued';",
        (json.dumps(failure), now, task_id),
    )
    if cur.rowcount == 0:
        return
    record_event(
        conn=conn,
        type_="task.failed",
        actor_id="system",
        actor_kind="system",
        via="system",
        project_id=project_id,
        task_id=task_id,
        from_state="queued",
        to_state="failed",
        reason="prompt_too_long",
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "reason": "prompt_too_long",
            "details": details,
            "fence": task["fence_seq"],
            "attempt": task["attempt"],
            "max_attempts": max_attempts,
        },
    )


TESTING_WRAPPER_DELAY_S = 3.0
CRASH_POINTS = ("after_tx_a", "after_popen")


def _crash_at() -> Optional[str]:
    """Testing hook MESH_TEST_CRASH_AT (§17.1, R7/R9): honoured only in testing mode."""
    if not _testing_mode():
        return None
    value = os.environ.get("MESH_TEST_CRASH_AT")
    return value if value in CRASH_POINTS else None


SPAWN_ABORT_WAIT_S = 5.0


class _IdentitySet(set):
    """Cumulative set of authenticated ``(pid, starttime)`` pairs (Round 11).

    Stores ``(int, int)`` pairs so PID reuse across abort passes keeps every
    observed generation. ``pid in s`` also matches by integer PID for callers
    checking whether a PID was ever authenticated.
    """

    def __contains__(self, item: object) -> bool:
        if isinstance(item, int) and not isinstance(item, bool):
            return any(p == item for p, _ in self)
        return super().__contains__(item)


def _persist_abort_members(ab: "_SpawnAbort") -> None:
    """Round 9-11: durably record the cumulative authenticated identity set of a spawn
    abort as a seen.json "unusable" marker (``spawn_abort_pending``, ``prior_members``;
    tmp + fsync + rename + dir fsync), storing every ``(pid, starttime)`` pair without
    collapsing duplicate PIDs across reuse generations. Called before the first signal
    and after every growth. A write failure is logged; memory keeps them."""
    current = set(ab.known)
    if current == ab.persisted or not ab.run_dir:
        return
    pairs = sorted(current)
    path = os.path.join(ab.run_dir, SEEN_FILE)
    doc = {SEEN_MARKER_KEY: "unusable", "run_id": ab.run_id, "reason": "spawn_abort_pending",
           "written_at": now_iso_utc(),
           "prior_members": [{"pid": p, "starttime": s} for p, s in pairs]}
    tmp = f"{path}.abort.{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(ab.run_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        ab.persisted = set(pairs)
    except OSError as exc:
        if ab.home:
            _log_meshd(ab.home, f"run {ab.run_id}: spawn abort identities not persisted: {exc}")


class _SpawnAbort:
    """Round 8-11: a wrapper whose spawn failed after Popen (task run or lead turn).
    ``known`` is the one cumulative set of every ``(pid, starttime)`` identity the abort
    ever authenticated (group-scan members and MESH_RUN_ID carriers). It never shrinks:
    an entry only stops counting once it is confirmed gone; a check error counts as
    alive. While anything survives — or while the closing DB write has not yet committed
    — the run stays open with its lease and the wrapper stays meshd's unreaped child."""

    def __init__(self, proc: "subprocess.Popen[Any]", run_id: str, fail_args: Tuple[Any, ...],
                 run_dir: Optional[str] = None, home: Optional[str] = None,
                 kind: str = "task") -> None:
        self.proc = proc
        self.run_id = run_id
        self.fail_args = fail_args
        self.run_dir = run_dir
        self.home = home
        self.kind = kind
        self.out: Dict[str, Any] = {}
        self.known: _IdentitySet = _IdentitySet()
        self.persisted: Set[Tuple[int, int]] = set()
        self.outside: Set[int] = set()
        self.survivors: Dict[int, int] = {}
        self.wrapper_starttime: Optional[int] = None
        self.kill_done = False
        self.retries = 0


#: run id -> pending spawn abort (round 8). In memory: after a meshd restart the open run
#: (identity in the DB, identities in seen.json, lease held) goes through boot recovery
#: and the close gate.
_SPAWN_ABORTS: Dict[str, _SpawnAbort] = {}


def in_memory_active_runs() -> Dict[str, int]:
    """In-memory ``run_id -> wrapper_pid`` for spawned or aborting wrappers not yet reaped."""
    out: Dict[str, int] = {rid: p.pid for rid, p in _WRAPPERS.items()}
    for rid, ab in _SPAWN_ABORTS.items():
        out[rid] = ab.proc.pid
    return out


def _reap_aborted_wrapper(ab: _SpawnAbort) -> None:
    """Reap and release a spawn-aborted wrapper after its DB close has committed."""
    pid = ab.proc.pid
    try:
        ab.proc.wait()
    finally:
        children.unhold(pid)
        children.unregister_if(pid, "wrapper")
        _SPAWN_ABORTS.pop(ab.run_id, None)


def _close_spawn_abort_db(conn: sqlite3.Connection, ab: _SpawnAbort, details: Dict[str, Any]) -> None:
    """Commit the closing ``spawn_failed`` transition of an aborted spawn."""
    from mesh.daemon import lead_turns

    with transaction(conn):
        conn.execute(
            "UPDATE runs SET wrapper_pid = COALESCE(wrapper_pid, ?), pgid = COALESCE(pgid, ?), "
            "proc_starttime = COALESCE(proc_starttime, ?) WHERE id = ?;",
            (ab.proc.pid, ab.proc.pid, ab.wrapper_starttime, ab.run_id),
        )
        if ab.kind == "lead_turn":
            project_id, message = ab.fail_args
            lead_turns._fail_lead_turn_spawn(
                conn, ab.run_id, project_id, message, after_popen=details or None,
            )
        else:
            task_id, project_id, fence, attempt, max_attempts, message = ab.fail_args
            _fail_task_spawn(
                conn, ab.run_id, task_id, project_id, fence, attempt, max_attempts, message,
                after_popen=details or None,
            )
            revoke_run_token(conn, ab.run_id)


def _abort_collect(ab: _SpawnAbort) -> bool:
    """Add every ``(pid, starttime)`` identity the group scan and the run-id scan
    authenticate to ``ab.known`` (never removes); persist on growth. Returns False when a
    scan failed."""
    pid = ab.proc.pid
    ok = True
    try:
        group_scan = _scan_group(pid)
        if getattr(group_scan, "uncertain", False):
            ok = False
        group = {p: st for p, st in group_scan.items() if p != pid}
    except OSError:
        group, ok = {}, False
    try:
        carrier_scan = terminate.run_targets(ab.run_id, None, None, ab.wrapper_starttime)
        if getattr(carrier_scan, "uncertain", False):
            ok = False
        carriers = {p: st for p, st in carrier_scan.items() if p != pid}
    except OSError:
        carriers, ok = {}, False
    for p, st in list(group.items()) + list(carriers.items()):
        if p <= 2 or known_starttime(st) is None:
            continue
        ab.known.add((int(p), int(st)))
        if p in carriers and p not in group:
            ab.outside.add(int(p))
    _persist_abort_members(ab)
    return ok


def _abort_kill_pass(ab: _SpawnAbort) -> Tuple[bool, Dict[int, int]]:
    """One pass: collect, SIGKILL the wrapper's group (its pgid is reserved by the
    unreaped leader) and every known ``(pid, starttime)`` identity (verifying starttime
    right before each signal), collect again. The leader is never reaped here. Returns
    ``(everything gone, live pid->starttime)``."""
    pid = ab.proc.pid
    ok = _abort_collect(ab)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for p, st in sorted(ab.known):
        terminate.signal_if_same(p, st, signal.SIGKILL)
    ok = _abort_collect(ab) and ok
    live_pairs: Set[Tuple[int, int]] = set()
    for p, st in sorted(ab.known):
        try:
            if _identity_live(p, st):
                live_pairs.add((p, st))
        except OSError:  # uncertain: keep counting it
            live_pairs.add((p, st))
            ok = False
    live: Dict[int, int] = {p: st for p, st in sorted(live_pairs)}
    try:
        leader_gone = _zombie(pid)
    except OSError:
        leader_gone = False
    if not leader_gone:
        live[pid] = int((ab.out or {}).get("starttime") or 0)
    return (ok and leader_gone and not live_pairs and not live), live


def _abort_spawned(ab: _SpawnAbort) -> bool:
    """Round 7-11: kill a wrapper whose spawn failed after Popen, with its group and its
    run-id carriers, for at most SPAWN_ABORT_WAIT_S. Identities are collected and
    persisted before the first signal. Reaping happens only once the closing DB write
    commits so the wrapper's pid/session stays held while the run row is open."""
    proc, run_id, home = ab.proc, ab.run_id, ab.home
    pid = proc.pid
    try:
        info = get_proc_stat(pid)
    except OSError:
        info = None
    st0 = known_starttime(info["starttime"]) if info else ab.wrapper_starttime
    if st0 is not None:
        ab.wrapper_starttime = st0
    ab.out = {"pid": pid, "starttime": st0}
    _abort_collect(ab)  # persisted before the first signal
    deadline = time.monotonic() + SPAWN_ABORT_WAIT_S
    while True:
        done, live = _abort_kill_pass(ab)
        if done or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    ab.survivors = live
    if ab.outside:
        ab.out["escapees"] = sorted(ab.outside)
    if not done:
        ab.out["survivors"] = sorted(live)
    if home:
        _log_meshd(home, f"run {run_id}: spawn failed after Popen; wrapper {pid} and its group killed"
                         + ("" if done else f"; survivors {sorted(live)}, lease kept, retrying"))
    return done


def retry_spawn_aborts(conn: sqlite3.Connection, home: str) -> List[str]:
    """Round 8-11: watch-pass retry of pending spawn aborts (task runs and lead turns).
    Once every known ``(pid, starttime)`` identity is gone AND the closing DB transaction
    commits, the wrapper is reaped and the run closes ``spawn_failed`` (lease released)."""
    closed: List[str] = []
    for run_id, a in list(_SPAWN_ABORTS.items()):
        a.retries += 1
        try:
            done, live = _abort_kill_pass(a)
        except Exception as exc:  # round 9 (P1-c): keep the pending abort, retry later
            _log_meshd(home, f"run {run_id}: spawn abort retry failed: {type(exc).__name__}: {exc}")
            continue
        if not done:
            a.kill_done = False
            a.survivors = live
            try:
                if a.kind == "lead_turn":
                    _renew_lead_turn_lease(conn, {"id": run_id, "project_id": a.fail_args[0]})
                else:
                    _renew_meshd_lease(conn, a.fail_args[0], run_id)
            except Exception:
                pass
            continue
        a.kill_done = True
        out = dict(a.out)
        out.pop("survivors", None)
        out["retries"] = a.retries
        out["survivors_at_abort"] = a.out.get("survivors", [])
        out["authenticated"] = [{"pid": p, "starttime": s} for p, s in sorted(a.known)]
        if a.outside:
            out["escapees"] = sorted(a.outside)
        try:
            _close_spawn_abort_db(conn, a, out)
        except Exception as db_exc:
            _log_meshd(home, f"run {run_id}: spawn abort close deferred: {type(db_exc).__name__}: {db_exc}")
            continue
        _reap_aborted_wrapper(a)
        _log_meshd(home, f"run {run_id}: spawn abort finished after {a.retries} retries; lease released")
        closed.append(run_id)
    return closed


def _zombie(pid: int) -> bool:
    try:
        info = get_proc_stat(pid)
        return info is None or not is_tgid_alive(pid, info)
    except OSError:
        return False


def _crash_now(home: str, point: str, run_id: str) -> None:
    """Simulate `kill -9 meshd` at a spawn crash point (no cleanup, no flush)."""
    _log_meshd(home, f"MESH_TEST_CRASH_AT={point}: killing meshd during run {run_id}")
    os.kill(os.getpid(), signal.SIGKILL)


def handle_spawn_failure(
    conn: sqlite3.Connection,
    home: Optional[str],
    run_id: str,
    run_dir: Optional[str],
    spawned: Optional["subprocess.Popen[Any]"],
    exc: BaseException,
    *,
    kind: str,
    fail_args: Tuple[Any, ...],
) -> None:
    """Shared pre-Popen and post-Popen failure handler for task runs and lead turns.

    Once ``Popen`` has created a wrapper (``spawned is not None``), the failure is always
    handled without raising: the wrapper is held in ``_SPAWN_ABORTS`` before any DB write,
    killed with its group and carriers, and reaped only after the closing DB transaction
    commits. If any DB write fails (e.g. ``SQLITE_BUSY``), ``retry_spawn_aborts`` retries.
    """
    from mesh.daemon import lead_turns

    message = f"{type(exc).__name__}: {exc}"
    if kind == "lead_turn":
        full_fail_args: Tuple[Any, ...] = (fail_args[0], message)
        project_id = fail_args[0]
        task_id = None
    else:
        task_id, project_id, fence, attempt, max_attempts = fail_args
        full_fail_args = (task_id, project_id, fence, attempt, max_attempts, message)

    if spawned is not None:
        ab = _SpawnAbort(spawned, run_id, full_fail_args, run_dir=run_dir, home=home, kind=kind)
        children.hold(spawned.pid)
        _WRAPPERS[run_id] = spawned
        unregister_secret(f"run_token:{run_id}")
        try:
            st0 = known_starttime((get_proc_stat(spawned.pid) or {}).get("starttime"))
        except OSError:
            st0 = None
        ab.wrapper_starttime = st0
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE runs SET wrapper_pid = ?, pgid = ?, "
                    "proc_starttime = COALESCE(proc_starttime, ?) WHERE id = ?;",
                    (spawned.pid, spawned.pid, st0, run_id),
                )
                revoke_run_token(conn, run_id)
        except Exception as db_exc:  # noqa: BLE001 - abort still runs; _WRAPPERS/_SPAWN_ABORTS cover active_runs
            if home:
                _log_meshd(home, f"run {run_id}: identity not recorded before abort: {db_exc}")
        try:
            done = _abort_spawned(ab)
        except Exception as abort_exc:  # round 9 (Linear P1-c): keep it pending, retry
            ab.out.setdefault("pid", spawned.pid)
            ab.out["abort_error"] = f"{type(abort_exc).__name__}: {abort_exc}"
            ab.out.setdefault("survivors", sorted({p for p, _ in ab.known}))
            done = False
        if ab.wrapper_starttime is None:
            ab.wrapper_starttime = known_starttime(ab.out.get("starttime"))
        details = ab.out
        if done:
            try:
                _close_spawn_abort_db(conn, ab, details)
            except Exception as db_exc:  # noqa: BLE001 - keep held in _SPAWN_ABORTS; retry_spawn_aborts closes
                ab.kill_done = True
                _SPAWN_ABORTS[run_id] = ab
                _WRAPPERS.pop(run_id, None)
                if home:
                    _log_meshd(home, f"run {run_id}: spawn abort close deferred: {type(db_exc).__name__}: {db_exc}")
                return None
            _WRAPPERS.pop(run_id, None)
            _reap_aborted_wrapper(ab)
            return None
        _SPAWN_ABORTS[run_id] = ab
        _WRAPPERS.pop(run_id, None)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE runs SET wrapper_pid = ?, pgid = ?, "
                    "proc_starttime = COALESCE(proc_starttime, ?) WHERE id = ?;",
                    (spawned.pid, spawned.pid, ab.wrapper_starttime, run_id),
                )
                revoke_run_token(conn, run_id)
                record_event(
                    conn=conn, type_="run.kill_failed", actor_id="system", actor_kind="system", via="system",
                    project_id=project_id, task_id=task_id, run_id=run_id,
                    payload={"run_id": run_id, "task_id": task_id, "end_reason": "spawn_failed",
                             "pids": details.get("survivors", []), "lease_released": False,
                             "retrying": True, "after_popen": True},
                )
        except Exception as db_exc:  # noqa: BLE001 - keep pending in _SPAWN_ABORTS; retry_spawn_aborts retries
            if home:
                _log_meshd(home, f"run {run_id}: kill_failed event deferred: {type(db_exc).__name__}: {db_exc}")
        return None

    with transaction(conn):
        if kind == "lead_turn":
            lead_turns._fail_lead_turn_spawn(conn, run_id, project_id, message)
        else:
            task_id, project_id, fence, attempt, max_attempts, message = full_fail_args
            _fail_task_spawn(
                conn, run_id, task_id, project_id, fence, attempt, max_attempts, message,
                after_popen=None,
            )
            revoke_run_token(conn, run_id)
    _WRAPPERS.pop(run_id, None)
    unregister_secret(f"run_token:{run_id}")
    return None


def _spawn_run(conn, home, task, agent, adapter, adapter_id, profile, model, effort,
               fence, attempt, max_attempts, run_id, run_dir, ws_path, prompt_text, strategy,
               branch: Optional[str] = None, base_commit: Optional[str] = None) -> Optional[str]:
    """TX A, effect, TX B / spawn_failed (plan §11.1), each transaction committed on its own."""
    task_id = task["id"]
    project_id = task["project_id"]

    # --- TX A: lease, fence, attempt, run row, run token, task running, events ---
    now = now_iso_utc()
    now_ts = time.time()
    lease_ttl_s = get_lease_ttl_s(conn)
    timeout_s = task["timeout_s"] or get_run_timeout_s(conn)
    deadline_at = now_iso_utc(now_ts + timeout_s)
    token_expires_at = now_iso_utc(now_ts + timeout_s + RUN_TOKEN_EXPIRY_GRACE_S)
    boot_id = _get_current_boot_id(conn)

    # The run row goes first: leases.run_id REFERENCES runs(id), and with
    # foreign_keys=ON an earlier lease insert fails (the M1c-run2a3 FK error).
    try:
        with transaction(conn):
            # Pre-public review P2: an agent disabled after selection must not start work.
            dis = conn.execute("SELECT disabled_at FROM principals WHERE id = ?;", (agent["id"],)).fetchone()
            if dis is None or dis["disabled_at"] is not None:
                raise sqlite3.IntegrityError(f"agent {agent['id']} is disabled")
            slots = count_active_runs(conn, agent["id"])
            total_slots = count_active_runs(conn)
            global_cap = get_global_run_cap(conn)

            conn.execute(
                """INSERT INTO runs (
                    id, kind, task_id, project_id, agent_id, attempt, fence,
                    adapter, model, effort, profile, status, boot_id, run_dir,
                    argv_redacted_json, started_at, deadline_at
                ) VALUES (?, 'task', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'starting', ?, ?, '[]', ?, ?);""",
                (
                    run_id,
                    task_id,
                    project_id,
                    agent["id"],
                    attempt,
                    fence,
                    adapter_id,
                    model,
                    effort,
                    profile,
                    boot_id,
                    run_dir,
                    now,
                    deadline_at,
                ),
            )

            conn.execute(
                """INSERT INTO leases (
                    key, run_id, holder_kind, holder, agent_id, fence,
                    slots_in_use_at_acquire, limit_at_acquire, total_at_acquire, total_limit_at_acquire,
                    acquired_at, expires_at, heartbeat_at
                ) VALUES (?, ?, 'meshd', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                (
                    f"task:{task_id}",
                    run_id,
                    boot_id,
                    agent["id"],
                    fence,
                    slots,
                    agent["max_concurrency"] or 1,
                    total_slots,
                    global_cap,
                    now,
                    deadline_at,
                    now,
                ),
            )

            token, lookup, secret_hash = generate_run_token()
            scope = {
                "role": "executor",
                "run_id": run_id,
                "task_id": task_id,
                "project_id": project_id,
                "fence": fence,
            }
            conn.execute(
                """INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at)
                   VALUES (?, ?, 'run_token', ?, ?, ?, ?, ?);""",
                (generate_id("cred"), agent["id"], lookup, secret_hash, json.dumps(scope), now, token_expires_at),
            )
            register_secret(f"run_token:{run_id}", token)

            cur_t = conn.execute(
                "UPDATE tasks SET status = 'running', fence_seq = ?, attempt = ?, workspace_path = ?, "
                "branch = ?, base_commit = ?, "
                "updated_at = ?, version = version + 1 WHERE id = ? AND status = 'queued' AND fence_seq = ?;",
                (fence, attempt, ws_path, branch, base_commit, now, task_id, fence - 1),
            )
            if cur_t.rowcount != 1:
                raise sqlite3.IntegrityError("task is no longer queued at the expected fence")

            record_event(
                conn=conn,
                type_="task.started",
                actor_id="system",
                actor_kind="system",
                via="system",
                project_id=project_id,
                task_id=task_id,
                run_id=run_id,
                from_state="queued",
                to_state="running",
                payload={"run_id": run_id, "fence": fence, "attempt": attempt, "adapter": adapter_id},
            )
    except sqlite3.IntegrityError as exc:
        # Lost a race (lease already held / task no longer queued): nothing was committed.
        unregister_secret(f"run_token:{run_id}")
        raise RuntimeError(f"TX A rejected for task {task_id}: {exc}") from exc

    if _crash_at() == "after_tx_a":
        _crash_now(home, "after_tx_a", run_id)

    # --- Effect: run dir, prompt.md, spec.json, spawn the wrapper ---
    proc: Optional["subprocess.Popen[Any]"] = None
    try:
        ensure_run_dir(run_dir)
        ensure_workspace_dir(ws_path)
        _write_text(os.path.join(run_dir, "prompt.md"), prompt_text)
        if strategy == "pointer":
            argv_prompt = pointer_prompt(run_dir)
        else:
            argv_prompt = prompt_text
        request = RunRequest(
            prompt_path=os.path.join(run_dir, "prompt.md"),
            workspace=ws_path,
            model=model,
            effort=effort,
            profile=profile,
            timeout_s=timeout_s,
            resume_session_id=verified_resume_session(adapter, task["last_session_id"]),
            options=build_run_options(agent, run_id),
        )
        spec_cmd: CommandSpec = adapter.build(request)
        argv = list(spec_cmd.argv)
        if strategy == "pointer":
            argv = [argv_prompt if part == request.prompt_path else part for part in argv]
        spec = build_spec(
            argv=argv,
            cwd=spec_cmd.cwd,
            stdin=spec_cmd.stdin,
            redact_env_names=["MESH_TOKEN"],
            idle_timeout_s=get_idle_timeout_s(conn),
        )
        if _crash_at() == "after_popen":
            # Risk 6: the spawn-window delay reaches runwrap only through this
            # testing-only spec.json field, never through the environment.
            spec["testing_wrapper_delay_s"] = TESTING_WRAPPER_DELAY_S
        _write_json(os.path.join(run_dir, "spec.json"), spec)

        socket_path = os.path.join(home, "run", "mesh.sock")
        child_env = build_child_env(
            base_env=dict(os.environ),
            token=token,
            run_id=run_id,
            fence=fence,
            task_id=task_id,
            project_id=project_id,
            socket_path=socket_path,
        )
        proc = children.popen_registered(
            "wrapper",
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", run_dir],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=ws_path,
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
        _WRAPPERS[run_id] = proc
        if _crash_at() == "after_popen":
            _crash_now(home, "after_popen", run_id)
        # --- TX B: run running with wrapper identity (inside try so any TX B error aborts) ---
        stat_info = get_proc_stat(proc.pid)
        with transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'running', wrapper_pid = ?, pgid = ?, proc_starttime = ?, "
                "argv_redacted_json = ? WHERE id = ?;",
                (
                    proc.pid,
                    proc.pid,
                    stat_info["starttime"] if stat_info else None,
                    json.dumps(redact_value(argv)),
                    run_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - any spawn failure (incl. TX B) is spawn_failed
        spawned = proc if proc is not None else getattr(exc, "proc", None)
        return handle_spawn_failure(
            conn, home, run_id, run_dir, spawned, exc,
            kind="task",
            fail_args=(task_id, project_id, fence, attempt, max_attempts),
        )
    return run_id


def parse_run_output(conn: sqlite3.Connection, run_id: str, run_dir: str, adapter_id: str) -> Dict[str, Any]:
    """Parse a finished run's output into a deliverable (plan §11.3, §12.5)."""
    final_text = ""
    try:
        adapter = get_adapter(adapter_id, testing_mode=_testing_mode())
        result = adapter.parse(run_dir)
        final_text = result.final_text or ""
    except Exception:
        final_text = ""
    if not final_text:
        try:
            with open(os.path.join(run_dir, "stdout.log"), "r", encoding="utf-8", errors="replace") as f:
                final_text = f.read()
        except OSError:
            final_text = ""
    return parse_deliverable(final_text)


#: m3-kiro-real fix round (Linear review A, MAJOR 1): an exit 0 is a success only when the
#: adapter's parse reports no ``vendor_exit_meaning``. Every adapter sets that field only for a
#: run that did not finish normally (kiro auth_failed/run_error/content_filtered, agy run_error,
#: vibe run_error/terminated/turn_limit/limit); droid and fake never set it. Fail closed: any
#: non-empty meaning, including one added later, turns T7 into T8 ``vendor_error``.
VENDOR_FAILURE_REASON = "vendor_error"


#: PR #12 round 2 (perf): one parse per finished run. Key: run_dir; value: (output signature,
#: adapter id, meaning). The signature is (size, mtime_ns) of each output file a parse reads, so a
#: changed file is parsed again. Entries of runs no longer active are dropped with the git cache.
_VENDOR_MEANING: Dict[str, Tuple[Any, Optional[str], Optional[str]]] = {}
_VENDOR_MEANING_MAX = 1024
_VENDOR_OUTPUTS = ("stdout.log", os.path.join("vibe-out", "export.json"))


def _output_signature(run_dir: str) -> Tuple[Any, ...]:
    sig = []
    for name in _VENDOR_OUTPUTS:
        try:
            st = os.stat(os.path.join(run_dir, name))
            sig.append((st.st_size, st.st_mtime_ns))
        except OSError:
            sig.append(None)
    return tuple(sig)


def vendor_failure_meaning(run_dir: str, adapter_id: Optional[str]) -> Optional[str]:
    """The parsed ``vendor_exit_meaning`` of a finished run, or None (normal finish).

    A parse that raises or an unknown adapter is not a vendor failure: the exit code decides,
    as before this rule. Cached per run dir while its outputs are unchanged (two stats per call)."""
    sig = _output_signature(run_dir)
    hit = _VENDOR_MEANING.get(run_dir)
    if hit is not None and hit[0] == sig and hit[1] == adapter_id:
        return hit[2]
    try:
        adapter = get_adapter(adapter_id or "", testing_mode=_testing_mode())
        meaning = adapter.parse(run_dir).vendor_exit_meaning
        meaning = meaning if isinstance(meaning, str) and meaning else None
    except Exception:
        meaning = None
    if len(_VENDOR_MEANING) >= _VENDOR_MEANING_MAX:
        _VENDOR_MEANING.clear()
    _VENDOR_MEANING[run_dir] = (sig, adapter_id, meaning)
    return meaning


def stderr_tail(run_dir: str, lines: int = STDERR_TAIL_LINES) -> List[str]:
    """Return the last ``lines`` lines of the run's stderr log (plan §11.3)."""
    try:
        with open(os.path.join(run_dir, "stderr.log"), "r", encoding="utf-8", errors="replace") as f:
            content = f.read().splitlines()
    except OSError:
        return []
    return content[-lines:]


def revoke_run_token(conn: sqlite3.Connection, run_id: str) -> None:
    """Revoke the run token of a finished run (plan §7.1, §11.4)."""
    conn.execute(
        "UPDATE credentials SET revoked_at = ? WHERE kind = 'run_token' "
        "AND json_extract(scope_json, '$.run_id') = ? AND revoked_at IS NULL;",
        (now_iso_utc(), run_id),
    )
    unregister_secret(f"run_token:{run_id}")


def run_token_scope(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    """Return the stored scope of a run's token (used by tests and the supervisor)."""
    cur = conn.execute(
        "SELECT scope_json FROM credentials WHERE kind = 'run_token' "
        "AND json_extract(scope_json, '$.run_id') = ? LIMIT 1;",
        (run_id,),
    )
    row = cur.fetchone()
    if not row or not row["scope_json"]:
        return None
    try:
        return json.loads(row["scope_json"])
    except (ValueError, TypeError):
        return None


def _log_meshd(home: str, msg: str) -> None:
    from mesh.daemon.scheduler import _log_to_meshd

    _log_to_meshd(home, msg)


# ---------------------------------------------------------------------------
# Code-task worktrees for launch T5 (m2-r4, §10.3; review m2-r3 finding 5).
#
# The tick runs on the event loop, so the git work never runs here: the first
# tick that reaches a queued code task plans it (DB reads) and starts a worker
# thread that runs only git. A later tick (the scheduler loop ticks at least once
# a second) finds the finished result and starts the run in TX A, or records T6
# `workspace_error`. Results are keyed by (workspace_path, branch, fence_seq): a
# task that changed in the meantime is planned again, never started from a stale
# result. The thread's git children are registered (gitops.run_git ->
# children.run_registered), so the reaper never reaps them.
# ---------------------------------------------------------------------------

_WS_PREP: Dict[str, Dict[str, Any]] = {}
#: A finished preparation older than this is not used; the worktree is checked again.
WS_PREP_MAX_AGE_S = 10.0
_WS_PREP_LOCK = threading.Lock()


def _ws_prep_worker(entry: Dict[str, Any], plan: Mapping[str, Any]) -> None:
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    try:
        result = run_code_workspace_plan(plan)
    except WorkspaceError as exc:
        error = dict(exc.details)
        error["message"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - any failure is workspace_error
        error = {"message": f"{type(exc).__name__}: {exc}", "workspace": plan.get("workspace_path"),
                 "step": "prepare"}
    with _WS_PREP_LOCK:
        entry["result"] = result
        entry["error"] = error
        entry["done"] = True
        entry["done_at"] = time.monotonic()


def _code_workspace_state(conn: sqlite3.Connection, home: str, task: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The finished worktree preparation for `task`, or None while it is in flight."""
    plan = plan_code_workspace(conn, home, task)
    if plan is None:
        return {"result": None, "error": None}
    key = (plan["workspace_path"], plan["branch"], plan["fence_seq"])
    task_id = task["id"]
    with _WS_PREP_LOCK:
        entry = _WS_PREP.get(task_id)
        if entry is not None and entry["key"] == key:
            if not entry["done"]:
                return None
            del _WS_PREP[task_id]
            if time.monotonic() - entry.get("done_at", 0.0) <= WS_PREP_MAX_AGE_S:
                return entry
            entry = None  # too old to trust the disk state it saw (finding 5): prepare again
        if entry is not None and not entry["done"]:
            return None  # an older plan is still running; wait for it before planning again
        entry = {"key": key, "done": False, "result": None, "error": None}
        _WS_PREP[task_id] = entry
    threading.Thread(target=_ws_prep_worker, args=(entry, plan), name=f"mesh-ws-{task_id}", daemon=True).start()
    return None


def _evict_ws_prep(candidate_ids: Set[str]) -> None:
    """Drop finished preparations of tasks that are no longer queued candidates (finding 5)."""
    with _WS_PREP_LOCK:
        for tid in [t for t, e in _WS_PREP.items() if e["done"] and t not in candidate_ids]:
            del _WS_PREP[tid]


def code_workspace_prep_in_flight() -> int:
    """Worker threads still preparing a worktree (tests and diagnostics)."""
    with _WS_PREP_LOCK:
        return sum(1 for e in _WS_PREP.values() if not e["done"])


def _fail_workspace_error(conn: sqlite3.Connection, task: Mapping[str, Any], details: Dict[str, Any]) -> None:
    """T6 at T5: the worktree could not be prepared (plan §6.2 `workspace_error`)."""
    from mesh.daemon.scheduler import fail_queued_task_t6

    with transaction(conn):
        current = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task["id"],)).fetchone()
        if current is None or current["fence_seq"] != task["fence_seq"]:
            return
        fail_queued_task_t6(conn, current, "workspace_error", details, assignee_id=current["assignee_agent_id"])


def spawn_launch_runs(
    conn: sqlite3.Connection,
    home: str,
    testing_mode: bool = False,
    max_starts: int = 8,
) -> List[str]:
    """Start queued launch-mode tasks in queue order (plan §9.3, T5).

    Order is ``priority DESC, queued_at ASC, id ASC`` per agent, bounded by the
    agent's ``max_concurrency`` and the global ``scheduler.max_total_runs``.
    """
    started: List[str] = []
    cur = conn.execute(
        """SELECT t.*, a.id as agent_id, a.adapter, a.delivery, a.max_concurrency,
                  a.default_model, a.default_effort, a.default_profile,
                  a.allowed_models_json, a.binary_path
           FROM tasks t
           JOIN agents a ON t.assignee_agent_id = a.id
           JOIN projects p ON t.project_id = p.id
           JOIN principals pr ON pr.id = a.id
           WHERE t.status = 'queued' AND a.delivery = 'launch' AND p.status = 'active'
             AND pr.disabled_at IS NULL
           ORDER BY t.priority DESC, t.queued_at ASC, t.id ASC;"""
    )
    candidates = cur.fetchall()
    # m4-p3-cleanup: never start a task whose adapter is held (evidence.LAUNCH_HOLDS), even
    # if the pre-spawn hold pass could not fail it (that pass logs and goes on per task).
    from mesh.adapters.evidence import held_milestone

    candidates = [t for t in candidates if not held_milestone(t["adapter"], testing_mode)]
    global_cap = get_global_run_cap(conn)
    # Slots held for code tasks whose worktree is being prepared (review m2-r4 finding 2,
    # §9.3/D12): such a task already won its slot in queue order, so later tasks in this
    # tick see the slot as taken and cannot overtake it while its git work runs.
    reserved_agent: Dict[str, int] = {}
    reserved_total = 0
    try:
        for task in candidates:
            if len(started) >= max_starts:
                break
            agent_id = task["agent_id"]
            limit = task["max_concurrency"] or 1
            if count_active_runs(conn, agent_id) + reserved_agent.get(agent_id, 0) >= limit:
                continue
            if global_cap is not None and count_active_runs(conn) + reserved_total >= global_cap:
                break
            outcome = _spawn_one(conn, home, task, testing_mode)
            if outcome == "reserved":
                reserved_agent[agent_id] = reserved_agent.get(agent_id, 0) + 1
                reserved_total += 1
            elif outcome:
                started.append(outcome)
    finally:
        _evict_ws_prep({t["id"] for t in candidates})
    return started


def _spawn_one(conn: sqlite3.Connection, home: str, task: Mapping[str, Any], testing_mode: bool) -> Optional[str]:
    """T5 for one candidate that has a free slot: the run id, "reserved" while its
    worktree is prepared, or None (failed or skipped)."""
    agent_id = task["agent_id"]
    agent = conn.execute(
        "SELECT a.*, p.name FROM agents a JOIN principals p ON a.id = p.id WHERE a.id = ?;",
        (agent_id,),
    ).fetchone()
    prepared = None
    if task["kind"] == "code":
        try:
            state = _code_workspace_state(conn, home, task)
            if state is None:
                return "reserved"  # worktree being prepared in a worker thread; a later tick starts it
            if state.get("error") is not None:
                _fail_workspace_error(conn, task, state["error"])
                return None
            prepared = state["result"]
        except Exception as exc:  # noqa: BLE001
            _log_meshd(home, f"T5 workspace prepare failed for task {task['id']}: {type(exc).__name__}: {exc}")
            return None
    try:
        return start_launch_run(conn, home, task, agent, testing_mode=testing_mode, prepared=prepared)
    except Exception as exc:  # one bad task must not stop the queue
        _log_meshd(home, f"T5 start failed for task {task['id']}: {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Watching and exit handling (plan §11.3) and termination (§11.4).
# ---------------------------------------------------------------------------

DEFAULT_KILL_GRACE_S = 15
DEFAULT_POST_SUBMIT_GRACE_S = 60
#: Terminations started by crash recovery (§11.7) → the status their run closes with.
RECOVERY_END_STATUS = {"lost_on_restart": "lost", "lost_before_spawn": "spawn_failed"}


def _testing_mode() -> bool:
    """meshd's testing mode (MESH_TESTING=1 in meshd's env only, §17.2): the fake
    adapter resolves only then, so vendor output is never parsed as fake (review m1c-run2 #8)."""
    return os.environ.get("MESH_TESTING") == "1"


def _read_json_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def known_starttime(value: Any) -> Optional[int]:
    """Round 8 (Linear P1-1): a usable process starttime (int > 0), else None ("unknown")."""
    if value is None or isinstance(value, bool) or not _is_int(value) or int(value) <= 0:
        return None
    return int(value)


def wrapper_liveness(run: Mapping[str, Any]) -> str:
    """Carbon RO P1-3: tri-state wrapper liveness (``"alive"``, ``"gone"``, ``"uncertain"``).

    Round 8 (Linear P1-1): an unknown recorded starttime (NULL, 0, garbage) returns
    ``"gone"`` (not provably alive), so no seen scan is seeded from that pid and nothing
    is signalled through it; the close gate then holds the run. A non-lookup ``OSError``
    reading ``/proc/<pid>/stat`` or ``/proc/<pid>/task`` returns ``"uncertain"`` so watch
    loops keep a potentially live run untouched without trusting seen scans."""
    pid = run["wrapper_pid"]
    if not pid:
        return "gone"
    expected = known_starttime(run["proc_starttime"])
    if expected is None:
        return "gone"
    try:
        stat_info = get_proc_stat(int(pid))
        if stat_info is None or int(stat_info.get("starttime") or 0) != expected:
            return "gone"
        return "alive" if is_tgid_alive(int(pid), stat_info) else "gone"
    except OSError:
        return "uncertain"


def wrapper_alive(run: Mapping[str, Any]) -> bool:
    """True while the run's wrapper process is provably alive (see :func:`wrapper_liveness`)."""
    return wrapper_liveness(run) == "alive"


_DEFAULT_WRAPPER_ALIVE = wrapper_alive


def _effective_wrapper_liveness(run: Mapping[str, Any]) -> str:
    """Respect monkeypatches on ``supervisor.wrapper_alive`` while preserving tri-state in production."""
    if wrapper_alive is not _DEFAULT_WRAPPER_ALIVE:
        return "alive" if wrapper_alive(run) else "gone"
    return wrapper_liveness(run)


#: Wrapper Popen objects per run id (review m1c-run3 #1). Holding the object keeps
#: CPython from parking it on ``subprocess._active`` (where any later Popen would
#: reap it behind our back), and ``Popen.poll()`` never waits on a pid twice, so a
#: reused pid number can never have its exit status collected here.
_WRAPPERS: Dict[str, "subprocess.Popen[Any]"] = {}


def register_wrapper(run_id: str, proc: "subprocess.Popen[Any]") -> None:
    """Keep a spawned wrapper's Popen object so its exit status is collected here.

    Used by the lead-turn spawn (m2-r9) as well as by `_spawn_run`.
    """
    _WRAPPERS[run_id] = proc


#: N18 (11.4, 11.7): an adopted run's group members are recorded at adoption and
#: every SEEN_SCAN_EVERY_TICKS-th watch tick while the wrapper identity holds.
SEEN_SCAN_EVERY_TICKS = 5
SEEN_FILE = "seen.json"

#: run id -> {pid: starttime} of members seen in the run's group/session while the
#: adopted wrapper's identity held; and the per-run watch-tick counter.
_SEEN: Dict[str, Dict[int, int]] = {}
_SEEN_TICKS: Dict[str, int] = {}


def is_adopted(run: Mapping[str, Any]) -> bool:
    """True for a run whose wrapper this meshd did not spawn (adopted, 11.7).

    Every wrapper spawned by this process (task run or lead turn) is in ``_WRAPPERS``
    until it is reaped; an adopted wrapper is init's child and never is.
    """
    return run["id"] not in _WRAPPERS


def _scan_group(pgid: int) -> ScanResult:
    """Live (non-zombie) processes whose pgrp or session equals ``pgid``: {pid: starttime}.

    ClickUp P1-1: non-lookup ``OSError`` from ``os.listdir('/proc')``, ``get_proc_stat`` or
    ``is_tgid_alive`` marks ``found.uncertain = True`` so callers never read an unreadable
    ``/proc`` entry as "group empty".
    """
    found = ScanResult()
    me = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        found.uncertain = True
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in (1, me):
            continue
        try:
            info = get_proc_stat(pid)
            if info is None or not is_tgid_alive(pid, info):
                continue
        except OSError:
            found.uncertain = True
            continue
        if int(info["pgrp"]) == int(pgid) or int(info["session"]) == int(pgid):
            found[pid] = int(info["starttime"])
    return found


#: r4b: the state of a run's persisted seen map, see :func:`load_seen_state`.
SEEN_ABSENT = "absent"      # no seen.json: no members recorded
SEEN_OK = "ok"              # a valid seen.json (its member list may be empty)
SEEN_UNUSABLE = "unusable"  # seen.json exists but cannot be trusted

#: Runs whose seen.json is unusable (and no in-memory map replaced it yet).
_SEEN_UNUSABLE: Set[str] = set()
#: Runs whose last seen.json write failed: retried on every later tick until it succeeds.
_SEEN_DIRTY: Set[str] = set()
#: r4c: run id -> why its seen.json is unusable (see SEEN_REASONS).
_SEEN_REASON: Dict[str, str] = {}
#: r4c: run id -> (mtime_ns, size) of seen.json when last loaded as unusable (None if
#: the file could not be stat'ed); a change triggers a reload while the run is held.
_SEEN_SIG: Dict[str, Optional[Tuple[int, int]]] = {}
#: r4c: runs loaded from an r4-format seen.json (no ``sid``): members are trusted only
#: with their recorded starttime while still in the run's process group (pgrp == pgid).
_SEEN_LEGACY: Set[str] = set()
#: Round 4: adoption-hold tokens (run_id -> token); see _load_seen / write_unusable_marker.
_SEEN_HOLD_TOKEN: Dict[str, str] = {}
_SEEN_MARKER: Set[str] = set()
SEEN_MARKER_KEY = "mesh_seen_marker"
SEEN_HOLD_TOKEN_KEY = "hold_token"
#: r4c: runs whose ``run.seen_unusable`` event is still to be recorded / already recorded.
#: r4e: a pending entry (run id -> reason) is daemon-level: it is not tied to the run
#: being open (eviction and a forced release leave it), and
#: :func:`flush_pending_seen_events` retries it on every watch tick until the event is
#: stored or found already stored. The event's ids come from the run's DB row.
_SEEN_EVENT_PENDING: Dict[str, str] = {}
_SEEN_EVENT_DONE: Set[str] = set()

#: r4d: run id -> unusable reason of a hold persisted in the DB (meta key
#: ``seen_hold:<run_id>``), as last read from or written to the DB. A run whose
#: seen.json is missing but has a persisted hold stays held (see :func:`seen_for`).
_SEEN_PERSISTED: Dict[str, str] = {}
SEEN_HOLD_META_PREFIX = "seen_hold:"

#: r4c: the ``reason`` values of ``run.seen_unusable``.
SEEN_REASONS = ("missing_fields", "bad_type", "run_id_mismatch", "pgid_mismatch",
                "sid_mismatch", "unreadable", "invalid_json")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _seen_sig(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (int(st.st_mtime_ns), int(st.st_size))


def _load_seen(run: Mapping[str, Any]) -> Tuple[str, Dict[int, int], Optional[str], bool]:
    """(state, members, unusable reason or None, legacy r4 format) - see load_seen_state."""
    path = os.path.join(run["run_dir"] or "", SEEN_FILE)
    try:
        # r4c: open the path itself; only a missing file means "no members recorded".
        # Any other error (a directory, no permission, EIO) makes the state unusable.
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return SEEN_ABSENT, {}, None, False
    except OSError:
        return SEEN_UNUSABLE, {}, "unreadable", False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return SEEN_UNUSABLE, {}, "invalid_json", False
    if not isinstance(data, dict):
        return SEEN_UNUSABLE, {}, "bad_type", False
    if SEEN_MARKER_KEY in data:
        # M3 gate fix round 4: a durable "unusable" marker (adoption pending or its scan
        # untrusted). It has no members/pgid, so it can never pass as seen data.
        # Round 5 (Quest review P1-1): it carries the members recorded before it
        # (``prior_members``, e.g. setsid escapees outside the pgid/session): unusable,
        # but those identities stay termination targets (starttime-checked by the users).
        reason = data.get("reason")
        if data.get("run_id") != run["id"]:
            return SEEN_UNUSABLE, {}, "run_id_mismatch", False
        return (SEEN_UNUSABLE, _prior_members(data.get("prior_members")),
                reason if isinstance(reason, str) and reason else ADOPT_SCAN_UNTRUSTED, False)
    if any(k not in data for k in ("run_id", "pgid", "members")):
        return SEEN_UNUSABLE, {}, "missing_fields", False
    if not isinstance(data["run_id"], str) or not _is_int(data["pgid"]) \
            or ("sid" in data and not _is_int(data["sid"])) or not isinstance(data["members"], list):
        return SEEN_UNUSABLE, {}, "bad_type", False
    if data["run_id"] != run["id"]:
        return SEEN_UNUSABLE, {}, "run_id_mismatch", False
    pgid = run["pgid"]
    if pgid is None or int(data["pgid"]) != int(pgid):
        return SEEN_UNUSABLE, {}, "pgid_mismatch", False
    legacy = "sid" not in data
    if not legacy and int(data["sid"]) != int(pgid):
        return SEEN_UNUSABLE, {}, "sid_mismatch", False
    out: Dict[int, int] = {}
    for m in data["members"]:
        if not isinstance(m, dict) or not _is_int(m.get("pid")) or not _is_int(m.get("starttime")) \
                or m["pid"] <= 1 or m["starttime"] < 0:
            return SEEN_UNUSABLE, {}, "bad_type", False
        out[int(m["pid"])] = int(m["starttime"])
    token = _SEEN_HOLD_TOKEN.get(run["id"])
    if token is not None and data.get(SEEN_HOLD_TOKEN_KEY) != token:
        # Round 4: an adoption hold (persisted row or in memory) wins over any seen.json
        # not written by a scan trusted after that hold (which carries its token), e.g.
        # an old valid file that could not be replaced by the marker.
        # Its members stay termination targets (round 5), the run stays held.
        return SEEN_UNUSABLE, out, _SEEN_PERSISTED.get(run["id"]) or ADOPT_SCAN_UNTRUSTED, False
    return SEEN_OK, out, None, legacy


def _prior_member_pairs(raw: Any) -> Set[Tuple[int, int]]:
    out: Set[Tuple[int, int]] = set()
    if isinstance(raw, list):
        for m in raw:
            if isinstance(m, dict) and _is_int(m.get("pid")) and _is_int(m.get("starttime")) \
                    and m["pid"] > 1 and m["starttime"] >= 0:
                out.add((int(m["pid"]), int(m["starttime"])))
    return out


def _prior_members(raw: Any) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for pid, st in sorted(_prior_member_pairs(raw)):
        if pid not in out or _identity_live(pid, st):
            out[pid] = st
    return out


def _load_prior_member_pairs(run: Mapping[str, Any]) -> Set[Tuple[int, int]]:
    """Read every ``(pid, starttime)`` pair from a marker ``seen.json`` without collapsing
    duplicate PIDs across reuse generations."""
    path = os.path.join(run["run_dir"] or "", SEEN_FILE)
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return set()
    if not isinstance(data, dict) or SEEN_MARKER_KEY not in data or data.get("run_id") != run["id"]:
        return set()
    return _prior_member_pairs(data.get("prior_members"))


def load_seen_state(run: Mapping[str, Any]) -> Tuple[str, Dict[int, int]]:
    """Read ``<run_dir>/seen.json`` and say whether it can be used (r4b, r4c).

    - file missing (FileNotFoundError only) -> (``absent``, {}): no members were recorded;
    - unreadable (any other OSError, e.g. a directory or no permission), invalid JSON,
      not a JSON object, a missing ``run_id``/``pgid``/``members``, wrong types,
      ``members`` not a list, a member not ``{"pid": int > 1, "starttime": int >= 0}``,
      or ``run_id``/``pgid``/``sid`` not equal to the run's id and pgid (the wrapper is
      the session leader, so sid == pgid) -> (``unusable``, {});
    - otherwise (``ok``, {pid: starttime}). An r4-format file (no ``sid``) is ``ok``:
      its sid is unknown and its members are verified by pid + starttime + pgid
      (see :func:`seen_for`).
    Never raises.
    """
    state, out, _reason, _legacy = _load_seen(run)
    return state, (out if state == SEEN_OK else {})


def _write_seen(run: Mapping[str, Any], seen: Dict[int, int]) -> None:
    """Persist the seen map to ``<run_dir>/seen.json`` (tmp file + atomic replace)."""
    doc = {
        "run_id": run["id"],
        "pgid": run["pgid"],
        "sid": run["pgid"],
        "wrapper_pid": run["wrapper_pid"],
        "wrapper_starttime": run["proc_starttime"],
        "updated_at": now_iso_utc(),
        "members": [{"pid": pid, "starttime": st} for pid, st in sorted(seen.items())],
    }
    if run["id"] in _SEEN_HOLD_TOKEN:
        doc[SEEN_HOLD_TOKEN_KEY] = _SEEN_HOLD_TOKEN[run["id"]]
    path = os.path.join(run["run_dir"], SEEN_FILE)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _persist_seen(run: Mapping[str, Any], seen: Dict[int, int], home: Optional[str] = None) -> bool:
    """Write seen.json; on failure log it and mark the run dirty (retried every tick)."""
    try:
        _write_seen(run, seen)
    except Exception as exc:
        first = run["id"] not in _SEEN_DIRTY
        _SEEN_DIRTY.add(run["id"])
        if first:
            _log_seen(home, f"run {run['id']}: seen.json write failed ({type(exc).__name__}: {exc}); "
                            "retrying every tick")
        return False
    if run["id"] in _SEEN_DIRTY:
        _SEEN_DIRTY.discard(run["id"])
        _log_seen(home, f"run {run['id']}: seen.json written after an earlier failure")
    if run["id"] in _SEEN_UNUSABLE:
        _log_seen(home, f"run {run['id']}: seen.json rewritten; seen state ok again")
    if _SEEN_REASON.get(run["id"]) == ADOPT_PENDING and _SEEN_PERSISTED.get(run["id"]) is None \
            and _SEEN_EVENT_PENDING.get(run["id"]) == ADOPT_PENDING:
        # Round 6: an adoption that completed with a trusted scan before its pending
        # state was persisted anywhere: no event.
        _SEEN_EVENT_PENDING.pop(run["id"], None)
    _SEEN_UNUSABLE.discard(run["id"])
    _SEEN_LEGACY.discard(run["id"])
    _SEEN_MARKER.discard(run["id"])
    _SEEN_SIG.pop(run["id"], None)
    return True


def _log_seen(home: Optional[str], msg: str) -> None:
    home = home or os.environ.get("MESH_HOME")
    if not home:
        return
    try:
        _log_meshd(home, msg)
    except Exception:
        pass  # logging must never break supervision


def flush_seen(run: Mapping[str, Any], home: Optional[str] = None) -> None:
    """Retry a failed seen.json write (no-op unless the run is dirty)."""
    if run["id"] in _SEEN_DIRTY and run["id"] in _SEEN:
        _persist_seen(run, dict(_SEEN[run["id"]]), home)


def _mark_unusable(run: Mapping[str, Any], reason: str, home: Optional[str]) -> None:
    run_id = run["id"]
    if run_id not in _SEEN_UNUSABLE:
        _log_seen(home, f"run {run_id}: seen.json unusable (reason={reason}); holding the run "
                        "while its group is occupied")
    _SEEN_UNUSABLE.add(run_id)
    _SEEN_REASON[run_id] = reason
    if run_id not in _SEEN_EVENT_DONE:
        if _SEEN_EVENT_PENDING.get(run_id) == ADOPT_PENDING:
            _SEEN_EVENT_PENDING[run_id] = reason  # round 6: the more specific reason wins
        else:
            _SEEN_EVENT_PENDING.setdefault(run_id, reason)


def _reload_if_changed(run: Mapping[str, Any], home: Optional[str]) -> None:
    """r4c: while a run's seen state is unusable, re-read seen.json when its mtime or size
    changed. A repaired (``ok``) file replaces the map and normal handling resumes; a
    missing file stays unusable (only a valid file lifts the hold)."""
    run_id = run["id"]
    path = os.path.join(run["run_dir"] or "", SEEN_FILE)
    sig = _seen_sig(path)
    if run_id in _SEEN_SIG and sig == _SEEN_SIG[run_id]:
        return
    _SEEN_SIG[run_id] = sig
    state, seen, reason, legacy = _load_seen(run)
    if state == SEEN_OK:
        _SEEN[run_id] = seen
        _SEEN_UNUSABLE.discard(run_id)
        _SEEN_SIG.pop(run_id, None)
        if legacy:
            _SEEN_LEGACY.add(run_id)
        else:
            _SEEN_LEGACY.discard(run_id)
        _log_seen(home, f"run {run_id}: seen.json changed and is valid again; members {sorted(seen)}")
    elif state == SEEN_UNUSABLE and reason:
        _SEEN_REASON[run_id] = reason


def seen_for(run: Mapping[str, Any], home: Optional[str] = None) -> Dict[int, int]:
    """The recorded group members of an adopted run ({} for runs this meshd spawned).

    The in-memory map wins; otherwise seen.json is loaded once. An unusable file is
    logged and the run is marked in ``_SEEN_UNUSABLE`` (see :func:`seen_hold`); while it
    is, the file is re-read whenever it changes (r4c). Members from an r4-format file
    (no ``sid``) are returned only while alive with their starttime and still in the
    run's process group (pgrp == pgid), since the session cannot be checked.
    """
    if not is_adopted(run):
        return {}
    run_id = run["id"]
    seen = _SEEN.get(run_id)
    if seen is None:
        state, seen, reason, legacy = _load_seen(run)
        if state == SEEN_ABSENT and run_id in _SEEN_PERSISTED:
            # r4d: the run was held for unusable seen state before (persisted in the DB);
            # a file deleted since (and a restart) must not turn that into "no members".
            state, reason = SEEN_UNUSABLE, _SEEN_PERSISTED[run_id]
            _log_seen(home, f"run {run_id}: seen.json missing but a persisted seen hold exists "
                            f"(reason={reason}); still held")
        if state == SEEN_UNUSABLE:
            _SEEN_SIG[run_id] = _seen_sig(os.path.join(run["run_dir"] or "", SEEN_FILE))
            _mark_unusable(run, reason or "unreadable", home)
        elif legacy:
            _SEEN_LEGACY.add(run_id)
        _SEEN[run_id] = seen
    elif run_id in _SEEN_UNUSABLE:
        _reload_if_changed(run, home)
        seen = _SEEN.get(run_id, {})
    if run_id in _SEEN_LEGACY and run["pgid"]:
        out: Dict[int, int] = {}
        for pid, st in seen.items():
            try:
                info = get_proc_stat(int(pid))
                if info is not None and is_tgid_alive(int(pid), info) and int(info["starttime"]) == int(st) \
                        and int(info["pgrp"]) == int(run["pgid"]):
                    out[pid] = st
            except OSError:
                continue
        return out
    return dict(seen)


def seen_hold_key(run_id: str) -> str:
    return SEEN_HOLD_META_PREFIX + run_id


def refresh_persisted_holds(conn: sqlite3.Connection, home: Optional[str] = None) -> None:
    """r4d: reload the persisted seen holds (meta ``seen_hold:<run_id>``) and drop the
    rows of runs that are no longer open. Never raises."""
    try:
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE ?;",
                            (SEEN_HOLD_META_PREFIX + "%",)).fetchall()
        found: Dict[str, str] = {}
        for row in rows:
            run_id = row[0][len(SEEN_HOLD_META_PREFIX):]
            try:
                doc = json.loads(row[1]) or {}
                reason = str(doc.get("reason") or "unreadable")
                token = doc.get("token")
            except (ValueError, TypeError, AttributeError):
                reason, token = "unreadable", None
            found[run_id] = reason
            if reason in ADOPT_HOLD_REASONS:
                # Round 4: the hold wins over any seen.json without this token (a row
                # from an older build without a token gets one that no file has).
                _SEEN_HOLD_TOKEN[run_id] = token if isinstance(token, str) and token else _new_hold_token()
        stale = []
        for run_id in found:
            r = conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()
            if r is None or r[0] not in ("starting", "running"):
                stale.append(run_id)
        if stale and not conn.in_transaction:
            with transaction(conn):
                for run_id in stale:
                    conn.execute("DELETE FROM meta WHERE key = ?;", (seen_hold_key(run_id),))
            for run_id in stale:
                found.pop(run_id, None)
        for run_id, reason in found.items():
            if run_id in _SEEN_PERSISTED:
                continue
            # r4e: a persisted hold this meshd did not know (or had forgotten) is restored.
            # A usable map cached meanwhile, e.g. an absent seen.json read while no hold
            # was known (= "no members"), must not outlive it: drop the cache, so the next
            # read applies the persisted reason (a valid file still lifts the hold). A
            # dirty map (members scanned while the wrapper lived, not yet written) is newer
            # than any file and is kept.
            if run_id in _SEEN and run_id not in _SEEN_UNUSABLE and run_id not in _SEEN_DIRTY:
                _SEEN.pop(run_id, None)
                _SEEN_SIG.pop(run_id, None)
                _SEEN_LEGACY.discard(run_id)
                _log_seen(home, f"run {run_id}: persisted seen hold restored (reason={reason}); "
                                "cached seen state dropped and re-read")
        _SEEN_PERSISTED.clear()
        _SEEN_PERSISTED.update(found)
    except Exception as exc:
        _log_seen(home, f"persisted seen holds not refreshed ({type(exc).__name__}: {exc})")


def _sync_hold_row(conn: sqlite3.Connection, run: Mapping[str, Any]) -> None:
    """r4d: keep the DB's persisted hold equal to the in-memory unusable state: written
    while the run is unusable, deleted once a valid file (reload or rewrite) lifted it.
    The in-memory mirror changes only after the commit."""
    run_id = run["id"]
    if run_id in _SEEN_UNUSABLE:
        reason = _SEEN_REASON.get(run_id) or "unreadable"
        if _SEEN_PERSISTED.get(run_id) == reason:
            return
        doc = {"run_id": run_id, "reason": reason, "since": now_iso_utc()}
        if reason in ADOPT_HOLD_REASONS:
            doc["token"] = _SEEN_HOLD_TOKEN.setdefault(run_id, _new_hold_token())
        with transaction(conn):
            # Round 4: the run.seen_unusable event (if still pending) and the hold row
            # commit together; a crash can no longer keep the event and lose the row.
            post_event = _event_in_tx(conn, run_id)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?);",
                         (seen_hold_key(run_id), json.dumps(doc, sort_keys=True)))
        if post_event is not None:
            post_event()
        _SEEN_PERSISTED[run_id] = reason
    elif run_id in _SEEN_PERSISTED and run_id in _SEEN:
        with transaction(conn):
            conn.execute("DELETE FROM meta WHERE key = ?;", (seen_hold_key(run_id),))
        _SEEN_PERSISTED.pop(run_id, None)
        _SEEN_HOLD_TOKEN.pop(run_id, None)


def _event_in_tx(conn: sqlite3.Connection, run_id: str):
    """Insert a pending ``run.seen_unusable`` inside the caller's transaction; returns the
    in-memory bookkeeping to run after the commit (or None if nothing was pending)."""
    if run_id in _SEEN_EVENT_DONE:
        _SEEN_EVENT_PENDING.pop(run_id, None)
        return None
    reason = _SEEN_EVENT_PENDING.get(run_id)
    if reason is None:
        return None
    ids = conn.execute("SELECT project_id, task_id FROM runs WHERE id = ?;", (run_id,)).fetchone()
    if ids is None:
        return None
    if conn.execute("SELECT 1 FROM events WHERE type = 'run.seen_unusable' AND run_id = ? LIMIT 1;",
                    (run_id,)).fetchone() is None:
        record_event(conn=conn, type_="run.seen_unusable", actor_id="system", actor_kind="system",
                     via="system", project_id=ids[0], task_id=ids[1], run_id=run_id,
                     payload={"run_id": run_id, "reason": reason})

    def done() -> None:
        _SEEN_EVENT_PENDING.pop(run_id, None)
        _SEEN_EVENT_DONE.add(run_id)
    return done


def _sync_event(conn: sqlite3.Connection, run: Mapping[str, Any]) -> None:
    _sync_event_id(conn, run["id"])


def _sync_event_id(conn: sqlite3.Connection, run_id: str) -> None:
    if run_id in _SEEN_UNUSABLE and _SEEN_PERSISTED.get(run_id) != (_SEEN_REASON.get(run_id) or "unreadable"):
        # Round 5 (Linear P2): a hold row is due as well: write both in one transaction.
        row = conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        if row is not None:
            _sync_hold_row(conn, row)
            return
    _sync_event_id_only(conn, run_id)


def _sync_event_id_only(conn: sqlite3.Connection, run_id: str) -> None:
    """r4c/r4d: record ``run.seen_unusable {run_id, reason}`` once per run. The run is
    marked done only after the insert committed (or the event already exists). r4e: the
    event's ids are read from the run's row, so this works also after the run closed."""
    if run_id in _SEEN_EVENT_DONE:
        _SEEN_EVENT_PENDING.pop(run_id, None)
        return
    reason = _SEEN_EVENT_PENDING.get(run_id)
    if reason is None:
        return
    ids = conn.execute("SELECT project_id, task_id FROM runs WHERE id = ?;", (run_id,)).fetchone()
    if ids is None:
        _SEEN_EVENT_PENDING.pop(run_id, None)  # no such run in this DB: nothing to record
        return
    with transaction(conn):
        if conn.execute("SELECT 1 FROM events WHERE type = 'run.seen_unusable' AND run_id = ? LIMIT 1;",
                        (run_id,)).fetchone() is None:
            record_event(
                conn=conn,
                type_="run.seen_unusable",
                actor_id="system",
                actor_kind="system",
                via="system",
                project_id=ids[0],
                task_id=ids[1],
                run_id=run_id,
                payload={"run_id": run_id, "reason": reason},
            )
    _SEEN_EVENT_PENDING.pop(run_id, None)
    _SEEN_EVENT_DONE.add(run_id)


def flush_pending_seen_events(conn: sqlite3.Connection, home: Optional[str] = None) -> None:
    """r4e: retry every pending ``run.seen_unusable`` event, whether or not its run is
    still open (a run may close on the tick its insert failed). Own transactions only;
    inside a caller's transaction it does nothing. Never raises."""
    if conn.in_transaction:
        return
    for run_id in list(_SEEN_EVENT_PENDING):
        try:
            _sync_event_id(conn, run_id)
        except Exception as exc:
            _log_seen(home, f"run {run_id}: seen_unusable event not stored ({type(exc).__name__}: {exc}); "
                            "retrying next tick")


def report_seen_unusable(conn: sqlite3.Connection, run: Mapping[str, Any], home: Optional[str] = None) -> None:
    """Persist an adopted run's seen-hold state: the ``run.seen_unusable`` event (once
    per run, also across restarts) and the persisted hold (r4d). Writes only in its own
    transaction: called inside a caller's transaction it does nothing, so an outer
    rollback can never lose a write marked done; everything still pending is retried on
    the next call. A failed write is logged and retried (never raises)."""
    if conn.in_transaction:
        return
    for step in (_sync_hold_row, _sync_event):
        try:
            step(conn, run)
        except Exception as exc:
            _log_seen(home, f"run {run['id']}: seen state not persisted ({type(exc).__name__}: {exc}); "
                            "retrying next tick")
            break  # round 4: event and hold row go together; retry both next tick


def seen_held(run: Mapping[str, Any]) -> bool:
    """True while the run is held only because its seen state is unusable (r4c operator path)."""
    return run["kind"] == "task" and run["status"] in ("starting", "running") and seen_hold(run)


def release_seen_hold(run_id: str) -> Optional[str]:
    """Forget an operator-released run's in-memory seen/termination state; returns the
    unusable reason it was held for."""
    reason = _SEEN_REASON.get(run_id)
    terminate.forget(run_id)
    # r4e: a pending run.seen_unusable event is kept (retried by flush_pending_seen_events).
    for d in (_SEEN, _SEEN_TICKS, _SEEN_REASON, _SEEN_SIG, _SEEN_PERSISTED):
        d.pop(run_id, None)
    for st in (_SEEN_UNUSABLE, _SEEN_DIRTY, _SEEN_LEGACY):
        st.discard(run_id)
    return reason


def finish_forced_release(home: Optional[str], run_id: str, reason: Optional[str],
                          actor: Optional[str]) -> None:
    """r4e: the post-commit part of ``run release --force-seen-unusable``: forget the
    run's in-memory seen/termination state and log the release. Called only after the
    request's transaction committed (server effect ``seen_release_forced``)."""
    release_seen_hold(run_id)
    _log_seen(home, f"run {run_id}: seen hold released by {actor} "
                    f"(--force-seen-unusable, reason={reason}); run lost, no signal")


class CloseHeld(Exception):
    """Round 6: the close-safety gate refused to close a run (see :func:`close_gate`)."""

    def __init__(self, run_id: str, end_reason: str, live: Dict[int, int], occupied: bool,
                 pgid: int, wrapper_starttime: Optional[int]) -> None:
        super().__init__(f"close of run {run_id} held (live recorded {sorted(live)}, occupied={occupied})")
        self.run_id = run_id
        self.end_reason = end_reason
        self.live = live
        self.occupied = occupied
        self.pgid = pgid
        self.wrapper_starttime = wrapper_starttime


def close_identity(run: Mapping[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """Round 6: the run's group identity (pgid, wrapper starttime) from the DB or, when
    the DB has none yet (spawn window: TX B not committed), from wrapper.json."""
    keys = run.keys()
    pgid = run["pgid"] if "pgid" in keys else None
    run_dir = (run["run_dir"] if "run_dir" in keys else "") or ""
    if pgid:
        st = run["proc_starttime"] if "proc_starttime" in keys else None
        if _is_int(st) and int(st) > 0:
            return int(pgid), int(st)
        w = _read_json_file(os.path.join(run_dir, "wrapper.json")) or {}
        wst = w.get("starttime") if _is_int(w.get("pid")) and int(w["pid"]) == int(pgid) else None
        return int(pgid), (int(wst) if _is_int(wst) and int(wst) > 0 else None)
    w = _read_json_file(os.path.join(run_dir, "wrapper.json")) or {}
    if _is_int(w.get("pid")) and int(w["pid"]) > 1:
        st = w.get("starttime")
        # Round 7 (Quest P3-2): a missing, zero or garbage starttime is "unknown".
        return int(w["pid"]), (int(st) if _is_int(st) and int(st) > 0 else None)
    return None, None


def group_still_ours(pgid: int, wrapper_starttime: Optional[int]) -> bool:
    """False only if a live process now has pid == pgid and is not the wrapper. Linux never
    reuses a pid while any live process has it as pgid or sid, so the old group emptied
    before that reuse and what is in the group now is not the run's."""
    try:
        info = get_proc_stat(int(pgid))
    except OSError:
        return True
    if info is None:
        return True
    if wrapper_starttime is None or not _is_int(wrapper_starttime) or int(wrapper_starttime) <= 0:
        # Round 7 (Quest P3-2): with no usable starttime a reuse cannot be proven:
        # fail closed (the group counts as the run's, so an occupied group holds the
        # run; nothing is signalled on this basis).
        return True
    return int(info["starttime"]) == int(wrapper_starttime)


def _identity_live(pid: int, starttime: int) -> bool:
    try:
        info = get_proc_stat(int(pid))
        return info is not None and int(info["starttime"]) == int(starttime) and is_tgid_alive(int(pid), info)
    except OSError:
        # Linear r10 P1-1: unreadable /proc (EACCES/EIO) is unknown, never gone.
        return True


#: Round 7: last logged refused-close state per run (log once per state change).
_CLOSE_REFUSED_LOGGED: Dict[str, Tuple[Any, ...]] = {}


def close_gate(run: Mapping[str, Any], end_reason: str = "") -> None:
    """Round 6 (N18): the one close-safety gate for task runs (called by _close_run).

    1. identity from the DB and the disk (wrapper.json; seen.json incl. a marker's
       ``prior_members`` or a token-mismatched file; the in-memory map);
    2. a recorded identity still alive (pid + starttime) -> refuse: terminate it first;
    3. the run's pgid/session still occupied and the group still the run's -> refuse:
       hold (nothing signalled by pgid alone);
    4. otherwise the close may proceed. Raises :class:`CloseHeld` to refuse."""
    if run["kind"] != "task":
        return
    pgid, wst = close_identity(run)
    if pgid is None:
        return
    view = dict(run)
    view["pgid"] = pgid
    recorded_pairs: Set[Tuple[int, int]] = {
        (int(p), int(s)) for p, s in (_SEEN.get(run["id"]) or {}).items()
    }
    _st, from_file, _r, _l = _load_seen(view)
    recorded_pairs.update((int(p), int(s)) for p, s in from_file.items())
    recorded_pairs.update(_load_prior_member_pairs(view))
    if wst is not None:
        recorded_pairs.add((int(pgid), int(wst)))
    live = {p: s for p, s in sorted(recorded_pairs) if _identity_live(p, s)}
    occupied = False
    if group_still_ours(pgid, wst):
        scanned = _scan_group(pgid)
        occupied = bool(scanned) or getattr(scanned, "uncertain", False)
    if live or occupied:
        raise CloseHeld(run["id"], end_reason, live, occupied, pgid, wst)


def hold_close(conn: sqlite3.Connection, home: Optional[str], run: Mapping[str, Any], held: "CloseHeld") -> None:
    """Round 6: act on a refused close: record the disk identity if the DB has none,
    terminate live recorded identities (two-scan rule of terminate), and hold the run
    while its group/session is occupied (wrapper_gone_group_occupied)."""
    run_id = run["id"]
    if not run["pgid"]:
        with transaction(conn):
            conn.execute("UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? "
                         "WHERE id = ? AND pgid IS NULL;", (held.pgid, held.pgid, held.wrapper_starttime, run_id))
    fresh = conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone() or run
    state = (held.end_reason, tuple(sorted(held.live)), held.occupied)
    if _CLOSE_REFUSED_LOGGED.get(run_id) != state:
        # Round 7 (Quest P3-1): logged once per state change, not on every retry.
        _CLOSE_REFUSED_LOGGED[run_id] = state
        _log_seen(home, f"run {run_id}: close refused by the close-safety gate (end_reason={held.end_reason}, "
                        f"live recorded {sorted(held.live)}, group occupied={held.occupied}); held")
    if held.occupied and (run_id not in _SEEN_UNUSABLE or _SEEN_REASON.get(run_id) not in ADOPT_HOLD_REASONS):
        hold_wrapper_gone(fresh, home)
    if held.occupied:
        report_seen_unusable(conn, fresh, home)
    existing = terminate.get(run_id)
    if existing is not None:
        if held.live:
            for pid, st in held.live.items():
                prev = existing.seen.get(int(pid))
                if prev != int(st):
                    existing.seen[int(pid)] = int(st)
                    existing.signalled_term.discard(int(pid))
                    existing.signalled_kill.discard(int(pid))
            existing.empty_scan_at = None
            if existing.phase == "failed":
                existing.next_retry = 0.0
        return
    if held.live:
        if held.end_reason in GATE_KEEPS_OUTCOME:
            # Round 7 (Linear P2): the run had ended on its own (exit.json, or none:
            # lost). Its live recorded processes are leftovers (H17): terminated, then the
            # run closes with its own outcome (exit 0 -> auto-submit), never as killed.
            if home:
                _begin_termination(conn, home, fresh, "leftovers", leftovers=True, seen=dict(held.live))
            else:
                grace = _setting_int(conn, "kill.grace_s", DEFAULT_KILL_GRACE_S)
                terminate.begin(run_id, held.pgid, fresh["wrapper_pid"], "leftovers", grace,
                                wrapper_starttime=held.wrapper_starttime, seen=dict(held.live))
        else:
            grace = _setting_int(conn, "kill.grace_s", DEFAULT_KILL_GRACE_S)
            terminate.begin(run_id, held.pgid, fresh["wrapper_pid"], held.end_reason or "lost_on_restart", grace,
                            wrapper_starttime=held.wrapper_starttime, seen=dict(held.live))


#: Round 7: end reasons of a run that ended on its own (``_finish_run``); a gate-initiated
#: cleanup of such a run keeps that outcome (leftovers semantics).
GATE_KEEPS_OUTCOME = ("exited", "lost")


def _group_occupied(pgid: Optional[int]) -> bool:
    """Any live (non-zombie) process other than meshd/init whose pgrp or session is ``pgid``
    (or an uncertain scan: never read as empty)."""
    if not pgid:
        return False
    scanned = _scan_group(int(pgid))
    return bool(scanned) or getattr(scanned, "uncertain", False)


class SeenSnapshot(NamedTuple):
    """r4d: one read of a run's seen state per tick, used for both the target scan and
    the hold decision. ``changed``: this read (a flush or a reload of a held run's
    file) changed the recorded membership or the usability; such a tick never closes
    the run (the members are merged and scanned on the next tick)."""
    members: Dict[int, int]
    unusable: bool
    changed: bool


def seen_snapshot(run: Mapping[str, Any], home: Optional[str] = None) -> SeenSnapshot:
    """Flush a failed write, then read the seen state once (see :class:`SeenSnapshot`)."""
    if not is_adopted(run):
        return SeenSnapshot({}, False, False)
    run_id = run["id"]
    loaded = run_id in _SEEN
    before = (run_id in _SEEN_UNUSABLE, dict(_SEEN.get(run_id) or {}))
    flush_seen(run, home)
    members = seen_for(run, home)
    after = (run_id in _SEEN_UNUSABLE, dict(_SEEN.get(run_id) or {}))
    return SeenSnapshot(members, after[0], loaded and before != after)


def snapshot_hold(run: Mapping[str, Any], snap: SeenSnapshot) -> bool:
    """:func:`seen_hold` decided from a snapshot (no second read of seen.json)."""
    if not is_adopted(run) or not snap.unusable:
        return False
    if wrapper_alive(run):
        return False
    return _group_occupied(run["pgid"])


def seen_hold(run: Mapping[str, Any]) -> bool:
    """r4b conservative rule: an adopted run whose seen.json is unusable, whose wrapper
    identity is gone, keeps its run open and lease held while any live process is in
    the run's pgid or session. Such processes are not signalled (their identity cannot
    be proven), so the run waits until they are gone (or a MESH_RUN_ID carrier
    termination ends them)."""
    if not is_adopted(run):
        return False
    seen_for(run)
    if run["id"] not in _SEEN_UNUSABLE:
        return False
    if wrapper_alive(run):
        return False
    return _group_occupied(run["pgid"])


def track_adopted_seen(run: Mapping[str, Any], home: Optional[str] = None) -> Optional[bool]:
    """N18: while an adopted run's wrapper identity holds, record its group members.

    Called on every watch tick with a live wrapper; scans at the first tick (adoption)
    and every ``SEEN_SCAN_EVERY_TICKS``-th tick after. A scan counts only if the
    wrapper identity still holds after it: the wrapper is the session leader, so the
    pgid/sid could not have been reused during the scan. Members that died are
    dropped; the file is rewritten when the map changed, when it does not exist or is
    unusable, and on every tick after a failed write until one succeeds (r4b).

    Returns True when a scan ran and was trusted, False when a scan ran but the
    wrapper identity no longer held after it (the result is discarded: it lost its
    anchor), None when no scan ran on this tick (gate fix round 2).
    """
    if not is_adopted(run) or not run["pgid"]:
        return None
    run_id = run["id"]
    tick = _SEEN_TICKS.get(run_id, 0)
    _SEEN_TICKS[run_id] = tick + 1
    if tick % SEEN_SCAN_EVERY_TICKS != 0:
        flush_seen(run, home)
        return None
    old = seen_for(run, home)
    scanned = _scan_group(int(run["pgid"]))
    liveness = _effective_wrapper_liveness(run)
    if liveness != "alive":
        if liveness == "uncertain":
            _SEEN_TICKS[run_id] = tick
        flush_seen(run, home)
        return False
    if getattr(scanned, "uncertain", False):
        _SEEN_TICKS[run_id] = tick
        flush_seen(run, home)
        return False
    merged: Dict[int, int] = {}
    for pid, st in old.items():
        try:
            info = get_proc_stat(pid)
            if info is not None and int(info["starttime"]) == st and is_tgid_alive(pid, info):
                merged[pid] = st
        except OSError:
            merged[pid] = st
    merged.update(scanned)
    _SEEN[run_id] = merged
    if merged != old or run_id in _SEEN_DIRTY or run_id in _SEEN_UNUSABLE or run_id in _SEEN_LEGACY \
            or run_id in _SEEN_MARKER or run_id in _SEEN_HOLD_TOKEN \
            or not os.path.exists(os.path.join(run["run_dir"], SEEN_FILE)):
        _persist_seen(run, merged, home)
    return True


#: Hold reason when the adoption scan could not be trusted (M3 gate fix round 2).
ADOPT_SCAN_UNTRUSTED = "adopt_scan_untrusted"
#: Marker reason written before an adoption, replaced by the first trusted scan (round 4).
ADOPT_PENDING = "adopt_pending"
#: Round 5 (Linear P1-b): at boot the wrapper is gone but processes remain in the run's
#: pgid/session that no usable seen.json covers: held, never closed as "no members".
WRAPPER_GONE_OCCUPIED = "wrapper_gone_group_occupied"
ADOPT_HOLD_REASONS = frozenset({ADOPT_SCAN_UNTRUSTED, ADOPT_PENDING, WRAPPER_GONE_OCCUPIED})


def hold_wrapper_gone(run: Mapping[str, Any], home: Optional[str] = None) -> None:
    """Round 5: hold a run whose wrapper died while meshd was down and whose group is
    still occupied beyond its recorded members (unusable, token, durable marker)."""
    _SEEN_HOLD_TOKEN.setdefault(run["id"], _new_hold_token())
    _mark_unusable(run, WRAPPER_GONE_OCCUPIED, home)
    write_unusable_marker(run, WRAPPER_GONE_OCCUPIED, home)


def _new_hold_token() -> str:
    return os.urandom(8).hex()


def write_unusable_marker(run: Mapping[str, Any], reason: str, home: Optional[str] = None) -> bool:
    """Round 4: durably replace seen.json with an "unusable" marker (tmp + fsync +
    rename + dir fsync). Every reader treats it as unusable with ``reason``; it has no
    members, so it can never pass as seen data. Only a trusted scan overwrites it. On a
    write failure the caller's in-memory unusable state stands and the persisted hold
    row (with its token) still wins over the old file; nothing falls back to it."""
    run_dir = run["run_dir"] or ""
    path = os.path.join(run_dir, SEEN_FILE)
    tmp = f"{path}.marker.{os.getpid()}"
    # Round 5: keep every identity recorded so far (old file or earlier marker, and
    # memory), so a crash before the next trusted scan cannot forget members outside
    # the pgid/session.
    prior_pairs: Set[Tuple[int, int]] = set(_load_prior_member_pairs(run))
    if run["pgid"]:
        _st, prior_map, _r, _l = _load_seen(run)
        prior_pairs.update((int(p), int(s)) for p, s in prior_map.items())
    prior_pairs.update((int(p), int(s)) for p, s in (_SEEN.get(run["id"]) or {}).items())
    doc = {SEEN_MARKER_KEY: "unusable", "run_id": run["id"], "reason": reason, "written_at": now_iso_utc(),
           "prior_members": [{"pid": p, "starttime": s} for p, s in sorted(prior_pairs)]}
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        _log_seen(home, f"run {run['id']}: seen marker ({reason}) not written ({type(exc).__name__}: "
                        f"{exc}); the run stays held in memory and by its persisted hold")
        return False
    _SEEN_SIG[run["id"]] = _seen_sig(path)
    _SEEN_MARKER.add(run["id"])  # the next trusted scan must replace it
    return True


def prepare_adopt_seen(run: Mapping[str, Any], home: Optional[str] = None) -> None:
    """Round 4: before recovery commits an adoption, keep the current seen map in memory
    and replace seen.json by an ``adopt_pending`` marker. A crash anywhere after this
    leaves a durable unusable marker (the next boot holds the run, never reads it as
    "no members"); the adoption scan, if trusted, overwrites it with real members."""
    run_id = run["id"]
    if not run["run_dir"] or run_id in _WRAPPERS:
        return
    state, seen, _reason, legacy = _load_seen(run) if run["pgid"] else (SEEN_ABSENT, {}, None, False)
    _SEEN[run_id] = dict(seen)  # round 5: also a marker's prior members
    if state == SEEN_OK and legacy:
        _SEEN_LEGACY.add(run_id)
    if _SEEN_PERSISTED.get(run_id) in ADOPT_HOLD_REASONS or run_id in _SEEN_HOLD_TOKEN:
        # An earlier adoption hold is still persisted: stay unusable until a trusted scan.
        _mark_unusable(run, _SEEN_PERSISTED.get(run_id) or ADOPT_SCAN_UNTRUSTED, home)
    else:
        # Round 5 (Linear P1-a): unusable in memory BEFORE the marker is written and
        # before the adopt transaction, so no later failure path (a failed adopt
        # transaction, a skipped scan) can fall back to the cached old members. Quiet:
        # no event unless the run is still pending when a hold is persisted
        # (_sync_hold_row). A trusted adoption scan lifts it.
        # Round 6 (Linear P2): the run.seen_unusable event is queued too (written with the
        # hold row, one transaction); a trusted adoption scan that lifts the pending
        # state before anything was flushed drops it again (_persist_seen).
        _mark_unusable(run, ADOPT_PENDING, home)
    _SEEN_HOLD_TOKEN.setdefault(run_id, _new_hold_token())
    write_unusable_marker(run, ADOPT_PENDING, home)



def establish_adopted_seen(run: Mapping[str, Any], home: Optional[str] = None) -> bool:
    """M3 gate P1-1: the adoption-time scan of an adopted run's group (N18).

    Runs :func:`track_adopted_seen`'s scan at once, from recovery, before the run is
    handed to the watch loop: the scan counts only if the wrapper identity (pid +
    starttime) still holds after it; its members are kept in memory and written to
    seen.json. The tick counter is reset afterwards, so the first watch tick with a
    live wrapper scans again. If the wrapper is already gone at that tick, the watch
    uses these members (seen_snapshot) for the two-scan termination.

    Fails closed (gate fix round 2): if the wrapper identity no longer holds after the
    scan (it exited during or just before it), the scan is not trusted and "untrusted"
    is never read as "no members". The run's seen state is marked unusable with reason
    ``adopt_scan_untrusted``: the r4b hold keeps the run open and its lease held while
    any process is in the run's pgid/session, nothing is signalled by pgid alone,
    ``run.seen_unusable`` is recorded and the hold is persisted (meta
    ``seen_hold:<run_id>``) for an operator (``run release --force-seen-unusable``).
    MESH_RUN_ID carriers are still terminated. Returns True if the seen state was
    established (or is not needed), False if the run is held as uncertain.
    """
    if not is_adopted(run) or not run["pgid"]:
        return True
    run_id = run["id"]
    _SEEN_TICKS.pop(run_id, None)
    prior = dict(_SEEN.get(run_id) or {})
    try:
        trusted = track_adopted_seen(run, home)
    finally:
        _SEEN_TICKS.pop(run_id, None)
    if trusted is False:
        # Round 5 (Quest review P2-1): keep the members known before this scan (not the
        # untrusted scan's): they stay termination targets while the run is held.
        _SEEN[run_id] = prior
        _SEEN_DIRTY.discard(run_id)
        _SEEN_LEGACY.discard(run_id)
        # Gate fix round 3: a valid seen.json from BEFORE the uncertainty (an earlier
        # trusted scan) must not read as a later repair and lift this hold, now or after
        # a restart. Move it aside atomically: with no seen.json, the r4c reload keeps the
        # run unusable and the r4d restart path restores the persisted hold. Only a scan
        # trusted after this point writes a new seen.json (which needs the wrapper alive).
        _SEEN_HOLD_TOKEN.setdefault(run_id, _new_hold_token())
        _mark_unusable(run, ADOPT_SCAN_UNTRUSTED, home)
        write_unusable_marker(run, ADOPT_SCAN_UNTRUSTED, home)
        _log_seen(home, f"run {run_id}: adoption scan not trusted (wrapper identity gone after "
                        "the scan); run held while its group/session is occupied")
        return False
    return True


def _evict_seen(active_run_ids: Set[str]) -> None:
    for run_id in [r for r in _SEEN if r not in active_run_ids]:
        _SEEN.pop(run_id, None)
    for run_id in [r for r in _SEEN_TICKS if r not in active_run_ids]:
        _SEEN_TICKS.pop(run_id, None)
    _SEEN_UNUSABLE.intersection_update(active_run_ids)
    _SEEN_DIRTY.intersection_update(active_run_ids)
    _SEEN_LEGACY.intersection_update(active_run_ids)
    _SEEN_MARKER.intersection_update(active_run_ids)
    # r4e: _SEEN_EVENT_PENDING is not evicted (see flush_pending_seen_events); a done
    # mark may go (the event-exists check keeps it once per run).
    _SEEN_EVENT_DONE.intersection_update(active_run_ids)
    for d in (_SEEN_REASON, _SEEN_SIG, _SEEN_PERSISTED, _SEEN_HOLD_TOKEN):
        for run_id in [r for r in d if r not in active_run_ids]:
            d.pop(run_id, None)


def _reap_wrapper(run: Mapping[str, Any]) -> None:
    """Collect the wrapper's exit status through its own Popen object (§11.4).

    Adopted wrappers (another boot's children) have no Popen here and are not
    meshd's children; nothing is waited on for them.
    """
    proc = _WRAPPERS.get(run["id"])
    if proc is None:
        return
    if proc.poll() is not None:
        _WRAPPERS.pop(run["id"], None)
        children.unregister_if(proc.pid, "wrapper")


def _setting_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?;", (key,)).fetchone()
    if row and row["value_json"] is not None:
        try:
            value = json.loads(row["value_json"])
            if value is not None:
                return int(value)
        except (ValueError, TypeError):
            pass
    return default


def _renew_meshd_lease(conn: sqlite3.Connection, task_id: str, run_id: str) -> None:
    now_ts = time.time()
    ttl = get_lease_ttl_s(conn)
    with transaction(conn):
        conn.execute(
            "UPDATE leases SET heartbeat_at = ?, expires_at = ? "
            "WHERE key = ? AND run_id = ? AND holder_kind = 'meshd';",
            (now_iso_utc(now_ts), now_iso_utc(now_ts + ttl), f"task:{task_id}", run_id),
        )


def _close_run(
    conn: sqlite3.Connection,
    run: Mapping[str, Any],
    status: str,
    end_reason: str,
    exit_code: Optional[int],
    result: Dict[str, Any],
    gate: bool = True,
) -> None:
    """Close the run, release the lease and revoke the run token (caller owns the tx).

    Round 6: every close of a task run passes :func:`close_gate` first; a held close
    raises :class:`CloseHeld`, which rolls the caller's transaction back."""
    if gate:  # only `run release --force-seen-unusable` (operator override) passes gate=False
        close_gate(run, end_reason)
    _CLOSE_REFUSED_LOGGED.pop(run["id"], None)
    now = now_iso_utc()
    session_id = None
    if run["kind"] in ("task", "lead_turn"):
        try:
            parsed = get_adapter(run["adapter"], testing_mode=_testing_mode()).parse(run["run_dir"])
            if isinstance(parsed.session_id, str) and parsed.session_id:
                session_id = parsed.session_id
        except Exception:
            pass  # Unparseable/missing output must not block closing a run.
    closed = conn.execute(
        "UPDATE runs SET status = ?, end_reason = ?, exit_code = ?, ended_at = ?, result_json = ?, session_id = ? "
        "WHERE id = ? AND status IN ('starting', 'running');",
        (status, end_reason, exit_code, now, json.dumps(redact_value(result)), session_id, run["id"]),
    )
    if closed.rowcount and session_id:
        if run["kind"] == "task":
            conn.execute("UPDATE tasks SET last_session_id = ? WHERE id = ?;",
                         (session_id, run["task_id"]))
        elif run["kind"] == "lead_turn":
            conn.execute("UPDATE lead_assignments SET session_id = ? WHERE id = ?;",
                         (session_id, run["lead_assignment_id"]))
    # A lead turn holds `leadturn:<prj>` and has no task (m2-r9); a task run holds
    # `task:<tsk>`. Releasing by run_id alone keeps both correct.
    conn.execute("DELETE FROM leases WHERE run_id = ?;", (run["id"],))
    revoke_run_token(conn, run["id"])


def _task_failed(
    conn: sqlite3.Connection,
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    reason: str,
    details: Dict[str, Any],
    stderr_lines: Optional[List[str]] = None,
    reporter: Optional[Mapping[str, Any]] = None,
) -> None:
    """T8 running → failed (caller owns the tx).

    The actor is the system, except for a deferred `task fail` (`reported`), whose
    event names the principal and channel that reported it (`reporter`).
    """
    now = now_iso_utc()
    details = dict(details)
    details["run_id"] = run["id"]
    if stderr_lines is not None:
        details["stderr_tail"] = stderr_lines
    failure = redact_value({"reason": reason, "details": details})
    cur = conn.execute(
        "UPDATE tasks SET status = 'failed', failure_json = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND status = 'running';",
        (json.dumps(failure), now, task["id"]),
    )
    if cur.rowcount != 1:
        return
    payload: Dict[str, Any] = {
        "task_id": task["id"],
        "project_id": task["project_id"],
        "reason": reason,
        "details": failure["details"],
        "fence": run["fence"],
        "attempt": task["attempt"],
        "max_attempts": task["max_attempts"],
    }
    if stderr_lines is not None:
        payload["stderr_tail"] = failure["details"]["stderr_tail"]
    record_event(
        conn=conn,
        type_="task.failed",
        actor_id=(reporter or {}).get("actor_id") or "system",
        actor_kind=(reporter or {}).get("actor_kind") or "system",
        via=(reporter or {}).get("via") or "system",
        project_id=task["project_id"],
        task_id=task["id"],
        run_id=run["id"],
        from_state="running",
        to_state="failed",
        reason=reason,
        payload=payload,
    )


def is_auto_submit(record: Mapping[str, Any]) -> bool:
    """T7 auto-submits are marked structurally (``auto: true``), never by the
    submitter's name: a user may name a launch agent ``system`` (review m1c-run3 #3)."""
    return record.get("auto") is True


def _task_auto_submit(conn: sqlite3.Connection, task: Mapping[str, Any], run: Mapping[str, Any],
                      git: Optional[Dict[str, Any]] = None) -> None:
    """T7 auto-submit after exit 0 without a prior submit (submitted_by: system).

    `git` is a code task's `deliverable.git` (§10.4, m2-r4), collected off the loop.
    """
    now = now_iso_utc()
    deliverable = redact_value(parse_run_output(conn, run["id"], run["run_dir"], run["adapter"]))
    if git is not None:
        deliverable = dict(deliverable) if isinstance(deliverable, dict) else {"raw": deliverable}
        deliverable["git"] = git
    record = {"deliverable": deliverable, "submitted_by": "system", "submitted_at": now, "run_id": run["id"],
              "auto": True}
    cur = conn.execute(
        "UPDATE tasks SET status = 'submitted', deliverable_json = ?, updated_at = ?, version = version + 1 "
        "WHERE id = ? AND status = 'running';",
        (json.dumps(record), now, task["id"]),
    )
    if cur.rowcount != 1:
        return
    record_event(
        conn=conn,
        type_="task.submitted",
        actor_id="system",
        actor_kind="system",
        via="system",
        project_id=task["project_id"],
        task_id=task["id"],
        run_id=run["id"],
        from_state="running",
        to_state="submitted",
        payload={
            "fence": run["fence"],
            "attempt": task["attempt"],
            "submitted_by": "system",
            "submitted_at": now,
            "deliverable": deliverable,
            "auto": True,
        },
    )


def _finish_run(
    conn: sqlite3.Connection,
    run: Mapping[str, Any],
    exit_info: Optional[Dict[str, Any]],
    git: Optional[Dict[str, Any]] = None,
) -> str:
    """Close a run whose processes are all gone and apply the task transition (§11.3)."""
    task = conn.execute("SELECT * FROM tasks WHERE id = ?;", (run["task_id"],)).fetchone()
    pending = None
    if run["result_json"]:
        try:
            pending = (json.loads(run["result_json"]) or {}).get("pending_failure")
        except (ValueError, TypeError, AttributeError):
            pending = None

    with transaction(conn):
        if exit_info is None:
            _close_run(conn, run, "lost", "lost", None, {"reason": "lost"})
            if task is not None and task["status"] == "running":
                _task_failed(conn, task, run, "lost", {}, stderr_tail(run["run_dir"]))
            return "lost"

        exit_code = exit_info.get("exit_code")
        signo = exit_info.get("signal")
        result = {"exit_code": exit_code, "signal": signo, "ended_at": exit_info.get("ended_at")}
        _close_run(conn, run, "exited", "exited", exit_code, result)
        if signo is not None:
            conn.execute("UPDATE runs SET signal = ? WHERE id = ?;", (signo, run["id"]))
        if task is None:
            return "closed"
        if task["status"] == "submitted" and is_auto_submit(_submitted_record(task)) \
                and _submitted_record(task).get("run_id") == run["id"]:
            # Auto-submitted at exit while leftovers were being terminated (§11.3).
            return "auto_submitted"
        if task["status"] == "submitted":
            record_event(
                conn=conn,
                type_="run.exited_after_submit",
                actor_id="system",
                actor_kind="system",
                via="system",
                project_id=task["project_id"],
                task_id=task["id"],
                run_id=run["id"],
                payload={"run_id": run["id"], "exit_code": exit_code, "signal": signo},
            )
            return "exited_after_submit"
        if task["status"] == "cancelling":
            # T10: the run ended on its own while a cancel was pending.
            _task_cancelled(conn, task, run, "cancelled")
            return "cancelled"
        if task["status"] != "running":
            return "closed"
        if pending:
            # A fenced `task fail` from the launched run; applied once its processes are gone (T8).
            _task_failed(conn, task, run, "reported", {"message": pending.get("message", "")}, reporter=pending)
            return "reported"
        if exit_code == 0 and signo is None:
            meaning = vendor_failure_meaning(run["run_dir"], run["adapter"])
            if meaning is None:
                _task_auto_submit(conn, task, run, git=git)
                return "auto_submitted"
            # §11.3 as ruled in docs/impl-notes-m3-kiro-real.md: exit 0 with an in-stream
            # vendor error is T8, never T7 (no automatic retry, §11.6).
            result["vendor_exit_meaning"] = meaning
            conn.execute("UPDATE runs SET result_json = ? WHERE id = ?;",
                         (json.dumps(redact_value(result)), run["id"]))
            _task_failed(conn, task, run, VENDOR_FAILURE_REASON,
                         {"exit_code": exit_code, "vendor_exit_meaning": meaning},
                         stderr_tail(run["run_dir"]))
            return VENDOR_FAILURE_REASON
        details: Dict[str, Any] = {"exit_code": exit_code}
        if signo is not None:
            details["signal"] = signo
        _task_failed(conn, task, run, "exit_nonzero", details, stderr_tail(run["run_dir"]))
        return "exit_nonzero"


def _task_cancelled(
    conn: sqlite3.Connection,
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    end_reason: str,
) -> None:
    """T10 cancelling → cancelled by the supervisor (caller owns the tx)."""
    now = now_iso_utc()
    cur = conn.execute(
        "UPDATE tasks SET status = 'cancelled', updated_at = ?, version = version + 1 "
        "WHERE id = ? AND status = 'cancelling';",
        (now, task["id"]),
    )
    if cur.rowcount != 1:
        return
    record_event(
        conn=conn,
        type_="task.cancelled",
        actor_id="system",
        actor_kind="system",
        via="system",
        project_id=task["project_id"],
        task_id=task["id"],
        run_id=run["id"],
        from_state="cancelling",
        to_state="cancelled",
        reason=end_reason,
        payload={"task_id": task["id"], "project_id": task["project_id"], "run_id": run["id"], "end_reason": end_reason},
    )
    from mesh.daemon.handlers.task import emit_dependents_blocked

    emit_dependents_blocked(conn, task["id"], None)


def _submitted_record(task: Mapping[str, Any]) -> Dict[str, Any]:
    if not task["deliverable_json"]:
        return {}
    try:
        value = json.loads(task["deliverable_json"])
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _adapter_liveness(adapter_id: Optional[str]) -> str:
    try:
        return str(getattr(get_adapter(adapter_id or "", testing_mode=_testing_mode()), "liveness", "none"))
    except Exception:
        return "none"


def termination_reason(conn: sqlite3.Connection, run: Mapping[str, Any], task: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Why a live run must be terminated now, or None (plan §11.3, §11.4)."""
    if task is None:
        return None
    if task["status"] == "cancelling":
        project = conn.execute("SELECT status FROM projects WHERE id = ?;", (task["project_id"],)).fetchone()
        return "project_cancelled" if project and project["status"] == "cancelled" else "cancelled"
    now = time.time()
    if task["status"] == "submitted":
        record = _submitted_record(task)
        if not is_auto_submit(record) and record.get("submitted_at"):
            grace = _setting_int(conn, "kill.post_submit_grace_s", DEFAULT_POST_SUBMIT_GRACE_S)
            if now >= parse_utc(record["submitted_at"]).timestamp() + grace:
                return "post_submit_grace"
    if run["deadline_at"] and now > parse_utc(run["deadline_at"]).timestamp():
        return "timeout"
    idle_s = int(task["idle_timeout_s"] or 0)
    if idle_s > 0 and task["status"] == "running" and _adapter_liveness(run["adapter"]) == "stream":
        try:
            last = os.stat(os.path.join(run["run_dir"], "activity")).st_mtime
        except OSError:
            last = None
        if last is not None and now - last > idle_s:
            return "idle_timeout"
    return None


def _begin_termination(
    conn: sqlite3.Connection,
    home: str,
    run: Mapping[str, Any],
    end_reason: str,
    leftovers: bool = False,
    seen: Optional[Dict[int, int]] = None,
) -> terminate.Termination:
    grace = _setting_int(conn, "kill.grace_s", DEFAULT_KILL_GRACE_S)
    t = terminate.begin(run["id"], run["pgid"], run["wrapper_pid"], end_reason, grace,
                        wrapper_starttime=run["proc_starttime"], seen=seen)
    if leftovers and not t.leftovers_reported:
        t.leftovers_reported = True
        with transaction(conn):
            record_event(
                conn=conn,
                type_="run.leftovers_terminated",
                actor_id="system",
                actor_kind="system",
                via="system",
                project_id=run["project_id"],
                task_id=run["task_id"],
                run_id=run["id"],
                payload={"run_id": run["id"], "pids": t.initial_pids, "end_reason": end_reason},
            )
    _log_meshd(home, f"terminating run {run['id']} ({end_reason}); pids {t.initial_pids}")
    return t


def _finalize_lead_turn_termination(
    conn: sqlite3.Connection,
    home: str,
    run: Mapping[str, Any],
    t: terminate.Termination,
    exit_info: Optional[Dict[str, Any]],
) -> str:
    """Close a terminated lead turn (m2-r9, §11.4, §12.4).

    A terminated turn did not succeed, so its messages go back to `pending` with the
    §12.4 backoff (or `dead` after `outbox.max_attempts`), exactly as a non-zero exit.
    """
    from mesh.daemon import lead_turns

    with transaction(conn):
        result: Dict[str, Any] = {"end_reason": t.end_reason, "kill_failed": t.kill_failed,
                                  "pids": sorted(t.seen)}
        exit_code = None
        if exit_info:
            exit_code = exit_info.get("exit_code")
            result["exit_code"] = exit_code
            result["signal"] = exit_info.get("signal")
        counts = lead_turns._release_messages(conn, run["id"], False, run["project_id"],
                                              _testing_mode())
        result["messages"] = counts
        _close_run(conn, run, RECOVERY_END_STATUS.get(t.end_reason, "killed"), t.end_reason,
                   exit_code, result)
        if exit_info and exit_info.get("signal") is not None:
            conn.execute("UPDATE runs SET signal = ? WHERE id = ?;", (exit_info.get("signal"), run["id"]))
    return "killed"


def _finalize_termination(
    conn: sqlite3.Connection,
    home: str,
    run: Mapping[str, Any],
    t: terminate.Termination,
) -> str:
    """The release condition holds: close the run, release the lease, then the task transition."""
    _reap_wrapper(run)
    exit_info = _read_json_file(os.path.join(run["run_dir"], "exit.json"))
    if run["kind"] == "lead_turn":
        if t.end_reason == "leftovers":
            # Review m2-r9 finding 2: a turn that exited 0 and left an escapee is a
            # success (§11.4 release condition, §12.4 "exit 0 → acked"), exactly as for
            # a task run (H17). Only timeout/recovery reasons take the failure path.
            from mesh.daemon import lead_turns

            with transaction(conn):
                lead_turns.finish_lead_turn(conn, home, run, exit_info, _testing_mode())
            # finish_lead_turn closes the run as "exited", or "lost" with no exit.json.
            return "exited" if exit_info is not None else "lost"
        return _finalize_lead_turn_termination(conn, home, run, t, exit_info)
    if t.end_reason == "leftovers":
        return _finish_run(conn, run, exit_info)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?;", (run["task_id"],)).fetchone()
    pending = None
    if run["result_json"]:
        try:
            pending = (json.loads(run["result_json"]) or {}).get("pending_failure")
        except (ValueError, TypeError, AttributeError):
            pending = None
    with transaction(conn):
        result: Dict[str, Any] = {"end_reason": t.end_reason, "kill_failed": t.kill_failed, "pids": sorted(t.seen)}
        exit_code = None
        if exit_info:
            exit_code = exit_info.get("exit_code")
            result["exit_code"] = exit_code
            result["signal"] = exit_info.get("signal")
        # Recovery terminations (§11.7) close the run as lost / spawn_failed, not killed.
        _close_run(conn, run, RECOVERY_END_STATUS.get(t.end_reason, "killed"), t.end_reason, exit_code, result)
        if exit_info and exit_info.get("signal") is not None:
            conn.execute("UPDATE runs SET signal = ? WHERE id = ?;", (exit_info.get("signal"), run["id"]))
        if task is None:
            return "killed"
        if task["status"] == "cancelling":
            _task_cancelled(conn, task, run, "cancelled" if t.end_reason in RECOVERY_END_STATUS else t.end_reason)
        elif task["status"] == "running":
            if pending:
                _task_failed(conn, task, run, "reported", {"message": pending.get("message", "")}, reporter=pending)
            elif t.kill_failed:
                _task_failed(conn, task, run, "kill_failed_then_dead", {"original_reason": t.end_reason},
                             stderr_tail(run["run_dir"]))
            else:
                _task_failed(conn, task, run, t.end_reason, {}, stderr_tail(run["run_dir"]))
        # submitted (post-submit grace, or a deadline after a submit): only the run closes.
    return "killed"


def _advance(conn: sqlite3.Connection, home: str, run: Mapping[str, Any], t: terminate.Termination,
             hold_tick: bool = False) -> Optional[str]:
    snap: Optional[SeenSnapshot] = None
    if run["kind"] != "lead_turn" and is_adopted(run):
        # r4c: a failed seen.json write is retried on every tick, also while the run is
        # terminating (grace window, kill retries, a seen hold), so a later restart still
        # finds the members. r4d: the seen state is read once per tick (snapshot); the
        # merge below and the hold decision after the step both use that one read.
        snap = seen_snapshot(run, home)
        # A seen.json repaired while the run is held (r4c reload) re-anchors the group:
        # its live members join the termination's seen map, so they are targeted.
        for pid, st in snap.members.items():
            try:
                info = get_proc_stat(int(pid))
                if info is not None and int(info["starttime"]) == int(st) and is_tgid_alive(int(pid), info):
                    t.seen.setdefault(int(pid), int(st))
            except OSError:
                t.seen.setdefault(int(pid), int(st))
        report_seen_unusable(conn, run, home)
    outcome = terminate.step(t)
    if outcome == "done" and (hold_tick or (snap is not None and (snap.changed or snapshot_hold(run, snap)))):
        # r4b: the release condition cannot be proven (unusable seen.json, group occupied).
        # r4d: nor on a tick whose read changed the seen state: the next tick merges and
        # scans the new members first.
        outcome = "pending"
    if outcome == "done":
        try:
            res = _finalize_termination(conn, home, run, t)
        finally:
            children.reap_orphans()
        terminate.forget(run["id"])
        return res
    if outcome == "kill_failed":
        with transaction(conn):
            record_event(
                conn=conn,
                type_="run.kill_failed",
                actor_id="system",
                actor_kind="system",
                via="system",
                project_id=run["project_id"],
                task_id=run["task_id"],
                run_id=run["id"],
                payload={
                    "run_id": run["id"],
                    "task_id": run["task_id"],
                    "end_reason": t.end_reason,
                    "pids": sorted(t.seen),
                    "retry_every_s": int(terminate.KILL_RETRY_S),
                },
            )
        _log_meshd(home, f"run {run['id']} kill failed; pids {sorted(t.seen)} still alive; lease kept, retrying")
    if run["kind"] == "lead_turn":
        _renew_lead_turn_lease(conn, run)
    else:
        _renew_meshd_lease(conn, run["task_id"], run["id"])
    return None


# Per-run "nothing left" confirmations of exited runs without a termination
# (m3-runtargets-race). One terminate.ClearCheck per run, advanced once per scan by
# that run's watch, evicted with the run; never shared between runs or call sites.
_CLEAR: Dict[str, terminate.ClearCheck] = {}


def _clear_check(run_id: str) -> terminate.ClearCheck:
    chk = _CLEAR.get(run_id)
    if chk is None:
        chk = _CLEAR[run_id] = terminate.ClearCheck()
    return chk


def pending_exit_checks() -> bool:
    """True while an exited run waits for its confirming second empty scan."""
    return any(c.first_empty_at is not None for c in _CLEAR.values())


def next_exit_check_in(now: Optional[float] = None) -> Optional[float]:
    """Seconds until the earliest pending exit confirmation is due (<= 0: due), or None."""
    now = time.monotonic() if now is None else now
    dues = [at for at in (c.due_at() for c in _CLEAR.values()) if at is not None]
    if not dues:
        return None
    return min(dues) - now


def confirm_pending_exits(conn: sqlite3.Connection, home: str) -> List[str]:
    """Take the confirming scan of exited runs once it is due (meshd's 200 ms loop).

    Without this the second empty scan would wait for the next 1 s watch pass; a
    blocking sleep on the loop is not allowed (review #20 L4). Returns closed run ids.
    """
    closed: List[str] = []
    now = time.monotonic()
    for run_id, chk in list(_CLEAR.items()):
        if not chk.due(now):
            continue
        first = chk.first_empty_at
        try:
            if terminate.get(run_id) is not None:
                continue  # the termination loop owns the run; retry after POLL_S
            run = conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
            if run is None or run["status"] not in ("starting", "running"):
                _CLEAR.pop(run_id, None)
                continue
            try:
                if _watch_one(conn, home, run):
                    closed.append(run_id)
            except Exception as exc:
                _log_meshd(home, f"exit confirmation failed for run {run_id}: {type(exc).__name__}: {exc}")
        finally:
            # Review #20c: a due check the attempt did not consume (deliverable.git still
            # collected, a termination owns the run, or the watch raised) is retried no
            # sooner than POLL_S, so the loop never spins on it at its 5 ms floor.
            if _CLEAR.get(run_id) is chk and chk.first_empty_at == first and chk.due(now):
                chk.defer(time.monotonic())
    return closed


def watch_launch_runs(conn: sqlite3.Connection, home: str) -> List[str]:
    """One supervisor pass over active launch-mode runs (plan §11.3, every 1 s).

    Returns the ids of runs closed in this pass.
    """
    from mesh.daemon.lead_turns import recover_queued_lead_merges_once

    try:
        recover_queued_lead_merges_once(conn)
    except Exception as exc:
        try:
            _log_meshd(home, f"queued lead merge recovery failed: {type(exc).__name__}: {exc}")
        except Exception:
            pass  # Even a logging failure must not block exits, leases or deadlines.
    closed: List[str] = []
    refresh_persisted_holds(conn, home)
    try:
        closed.extend(retry_spawn_aborts(conn, home))
    except Exception as exc:
        _log_meshd(home, f"spawn abort retry failed: {type(exc).__name__}: {exc}")
    runs = conn.execute(
        "SELECT * FROM runs WHERE kind IN ('task', 'lead_turn') AND kind <> 'merge_test' "
        "AND status IN ('starting', 'running') "
        "AND wrapper_pid IS NOT NULL ORDER BY started_at ASC, id ASC;"
    ).fetchall()
    for run in runs:
        if run["id"] in _SPAWN_ABORTS:
            continue  # round 8: owned by retry_spawn_aborts until its survivors are gone
        if run["kind"] == "merge_test":
            # The merge pipeline owns its run (no process; m2-r5). Explicit skip (m2-plan risk 5).
            continue
        try:
            outcome = _watch_one(conn, home, run)
        except Exception as exc:  # one run must not stop the others
            _log_meshd(home, f"supervisor watch failed for run {run['id']}: {type(exc).__name__}: {exc}")
            continue
        if outcome:
            closed.append(run["id"])
    active_ids = {r["id"] for r in runs} | set(_SPAWN_ABORTS)
    _evict_git_info(active_ids)
    _evict_seen(active_ids)
    for run_id in [r for r in _CLEAR if r not in active_ids]:
        _CLEAR.pop(run_id, None)
    flush_pending_seen_events(conn, home)
    return closed


def advance_terminations(conn: sqlite3.Connection, home: str) -> List[str]:
    """Advance only the runs being terminated (meshd's 200 ms loop, §11.4)."""
    closed: List[str] = []
    for run_id, t in terminate.active().items():
        run = conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        if run is None or run["status"] not in ("starting", "running"):
            terminate.forget(run_id)
            continue
        if run["kind"] == "merge_test":
            # The merge pipeline drives its own test-step termination (m2-r6); its run
            # never goes through T7/T8.
            continue
        try:
            if _advance(conn, home, run, t):
                closed.append(run_id)
        except CloseHeld as held:
            hold_close(conn, home, run, held)
        except Exception as exc:
            _log_meshd(home, f"termination step failed for run {run_id}: {type(exc).__name__}: {exc}")
    closed.extend(confirm_pending_exits(conn, home))
    return closed


def _watch_lead_turn(conn: sqlite3.Connection, home: str, run: Mapping[str, Any]) -> Optional[str]:
    """One watch pass over a lead turn (m2-r9, §12.4).

    A lead turn has no task, so it never reaches T7/T8: exit handling is
    `lead_turns.finish_lead_turn`. Superseding its lead assignment or reaching the
    deadline terminates a live turn; leftover processes are also terminated at exit.
    """
    from mesh.daemon import lead_turns

    t = terminate.get(run["id"])
    if t is not None:
        return _advance(conn, home, run, t)
    liveness = _effective_wrapper_liveness(run)
    if liveness in ("alive", "uncertain"):
        if liveness == "uncertain":
            _clear_check(run["id"]).invalidate()
        reason = lead_turns.lead_turn_termination_reason(conn, run)
        if reason:
            t = _begin_termination(conn, home, run, reason)
            return _advance(conn, home, run, t)
        _renew_lead_turn_lease(conn, run)
        return None
    exit_info = _read_json_file(os.path.join(run["run_dir"], "exit.json"))
    # Scan before reaping, exactly as for a task run (review m1c-run4 §2).
    leftovers = terminate.run_targets(run["id"], run["pgid"], run["wrapper_pid"], run["proc_starttime"])
    if leftovers:
        _CLEAR.pop(run["id"], None)
        # Hand the identities this scan found to begin() (review #20b L1 handoff): its
        # own re-scan may no longer reach an escapee whose parent exited in between.
        t = _begin_termination(conn, home, run, "leftovers", leftovers=True, seen=dict(leftovers))
        if not getattr(leftovers, "uncertain", False) and not getattr(t, "scan_uncertain", False):
            _reap_wrapper(run)
        return _advance(conn, home, run, t)
    # One empty scan is not "nothing left" (m3-runtargets-race): the run closes on a
    # second empty scan at least POLL_S later, taken on a later tick (no sleeping here;
    # the 200 ms termination loop takes it, see confirm_pending_exits). The wrapper stays
    # unreaped until then, so its pgid stays trusted for that scan.
    if not _clear_check(run["id"]).observe(leftovers):
        _renew_lead_turn_lease(conn, run)
        return None
    _CLEAR.pop(run["id"], None)
    _reap_wrapper(run)
    with transaction(conn):
        return lead_turns.finish_lead_turn(conn, home, run, exit_info, _testing_mode())


def _renew_lead_turn_lease(conn: sqlite3.Connection, run: Mapping[str, Any]) -> None:
    """Keep the `leadturn:<prj>` lease alive while the turn runs (§11.5)."""
    now_ts = time.time()
    ttl = get_lease_ttl_s(conn)
    with transaction(conn):
        conn.execute(
            "UPDATE leases SET heartbeat_at = ?, expires_at = ? "
            "WHERE key = ? AND run_id = ? AND holder_kind = 'meshd';",
            (now_iso_utc(now_ts), now_iso_utc(now_ts + ttl),
             f"leadturn:{run['project_id']}", run["id"]),
        )


def _watch_one(conn: sqlite3.Connection, home: str, run: Mapping[str, Any]) -> Optional[str]:
    """One watch step; a close refused by the close-safety gate turns into a hold."""
    try:
        return _watch_one_inner(conn, home, run)
    except CloseHeld as held:
        hold_close(conn, home, run, held)
        return None


def _watch_one_inner(conn: sqlite3.Connection, home: str, run: Mapping[str, Any]) -> Optional[str]:
    if run["kind"] == "lead_turn":
        return _watch_lead_turn(conn, home, run)
    t = terminate.get(run["id"])
    if t is not None:
        return _advance(conn, home, run, t)
    try:
        return _watch_task_run(conn, home, run)
    finally:
        if is_adopted(run):
            report_seen_unusable(conn, run, home)


def _watch_task_run(conn: sqlite3.Connection, home: str, run: Mapping[str, Any]) -> Optional[str]:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?;", (run["task_id"],)).fetchone()
    exit_path = os.path.join(run["run_dir"], "exit.json")
    liveness = _effective_wrapper_liveness(run)
    if liveness in ("alive", "uncertain"):
        if liveness == "alive":
            track_adopted_seen(run, home)
        else:
            _clear_check(run["id"]).invalidate()
        reason = termination_reason(conn, run, task)
        if reason:
            t = _begin_termination(conn, home, run, reason, seen=seen_for(run, home))
            return _advance(conn, home, run, t)
        _renew_meshd_lease(conn, run["task_id"], run["id"])
        return None
    exit_info = _read_json_file(exit_path)
    git = None
    if _needs_submit_git(run, task, exit_info):
        # §10.4 (m2-r4): a code task about to be auto-submitted gets `deliverable.git`,
        # collected in a worker thread. Until it is ready the run stays open and the
        # wrapper stays unreaped, so the scan-before-reap order below is unchanged.
        state = _submit_git_state(run, task)
        if state is None:
            return None
        git = state["git"]
    # Scan before reaping (review m1c-run4 §2): while meshd's own wrapper is an unreaped
    # zombie its pid, and so the pgid, cannot be reused, so every group/session member
    # belongs to this run, even one exec'd with MESH_RUN_ID stripped from its env.
    # An adopted wrapper is init's child and is reaped at once, so its pgid is trusted
    # after exit only through the members recorded while it lived (N18, seen.json).
    # r4d: one read of the seen state for this tick (target scan and hold decision).
    snap = seen_snapshot(run, home)
    seen = snap.members
    leftovers = terminate.run_targets(run["id"], run["pgid"], run["wrapper_pid"], run["proc_starttime"],
                                      seen)
    if not leftovers:
        if getattr(leftovers, "uncertain", False):
            # ClickUp P1-1: an uncertain scan is never an empty scan; keep the wrapper
            # unreaped so its pgid stays trusted on the next tick.
            _clear_check(run["id"]).invalidate()
            _renew_meshd_lease(conn, run["task_id"], run["id"])
            return None
        if snap.changed or snapshot_hold(run, snap):
            # r4b: seen.json is unusable and the run's group/session is still occupied:
            # keep the run open and the lease held (nothing is signalled) until it empties.
            # r4d: also for one tick after a read changed the seen state, so the next
            # tick scans with the new members before anything closes. Such a tick is
            # never a confirming empty scan (review #20 L3).
            _clear_check(run["id"]).invalidate()
            _reap_wrapper(run)
            _renew_meshd_lease(conn, run["task_id"], run["id"])
            return None
        # One empty scan is not "nothing left" (m3-runtargets-race): exactly one
        # confirmation step per real scan; the second empty scan, at least POLL_S
        # later, comes from a later tick (confirm_pending_exits, 200 ms loop). The
        # wrapper is not reaped before that, so its pgid stays trusted.
        if not _clear_check(run["id"]).observe(leftovers):
            _renew_meshd_lease(conn, run["task_id"], run["id"])
            return None
        _CLEAR.pop(run["id"], None)
        _reap_wrapper(run)
    else:
        _CLEAR.pop(run["id"], None)
    if leftovers:
        # §11.3: processes remain after the wrapper ended (e.g. a setsid escapee):
        # terminate them at once; the run closes when the release condition holds.
        reason = "leftovers"
        if task is not None and task["status"] == "cancelling":
            reason = termination_reason(conn, run, task) or "cancelled"
        # The members recorded for the run plus every identity this scan found (review
        # #20b L1 handoff): begin() re-scans, and an escapee found only through a parent
        # that exited in between would otherwise be lost. begin() keeps only entries
        # still alive with the same starttime.
        handoff = dict(seen)
        handoff.update(leftovers)
        t = _begin_termination(conn, home, run, reason, leftovers=True, seen=handoff)
        # begin() scanned with the zombie still unreaped; its result is t.seen, which
        # keeps the session trusted (terminate._session_anchored) after the reap.
        if not getattr(leftovers, "uncertain", False) and not getattr(t, "scan_uncertain", False):
            _reap_wrapper(run)
        if (
            reason == "leftovers"
            and task is not None
            and task["status"] == "running"
            and exit_info is not None
            and exit_info.get("exit_code") == 0
            and exit_info.get("signal") is None
            and not _pending_failure(run)
            and vendor_failure_meaning(run["run_dir"], run["adapter"]) is None
        ):
            # T7 auto-submit may happen while leftovers are terminated; the lease is kept
            # until the release condition holds (allowed by I1, §11.3).
            with transaction(conn):
                _task_auto_submit(conn, task, run, git=git)
        return _advance(conn, home, run, t, hold_tick=snap.changed)
    return _finish_run(conn, run, exit_info, git=git)


_GIT_INFO: Dict[str, Dict[str, Any]] = {}


def _needs_submit_git(run: Mapping[str, Any], task: Optional[Mapping[str, Any]],
                      exit_info: Optional[Dict[str, Any]]) -> bool:
    """True when this exit will auto-submit a code task (T7 at exit 0, §11.3)."""
    return bool(
        task is not None
        and task["kind"] == "code"
        and task["status"] == "running"
        and exit_info is not None
        and exit_info.get("exit_code") == 0
        and exit_info.get("signal") is None
        and not _pending_failure(run)
        and vendor_failure_meaning(run["run_dir"], run["adapter"]) is None
    )


def _git_info_worker(entry: Dict[str, Any], workspace: str, branch: Optional[str], base: Optional[str]) -> None:
    try:
        git = submission_git_info(workspace, branch, base)
    except Exception as exc:  # noqa: BLE001 - never blocks the submit
        git = {"branch": branch, "base_commit": base,
               "error": {"step": "collect", "reason": "exception", "stderr_tail": f"{type(exc).__name__}: {exc}"}}
    with _WS_PREP_LOCK:
        entry["git"] = git
        entry["done"] = True


def _evict_git_info(active_run_ids: Set[str]) -> None:
    """Drop `deliverable.git` entries of runs that are no longer active (review m2-r4 finding 5),
    and their cached vendor meanings (run dirs end in the run id)."""
    with _WS_PREP_LOCK:
        for rid in [r for r in _GIT_INFO if r not in active_run_ids]:
            del _GIT_INFO[rid]
    for rd in [d for d in _VENDOR_MEANING if os.path.basename(d.rstrip(os.sep)) not in active_run_ids]:
        _VENDOR_MEANING.pop(rd, None)


def _submit_git_state(run: Mapping[str, Any], task: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The finished `deliverable.git` for this run, or None while it is collected."""
    run_id = run["id"]
    with _WS_PREP_LOCK:
        entry = _GIT_INFO.get(run_id)
        if entry is not None:
            if not entry["done"]:
                return None
            # Kept until the run is no longer active (_evict_git_info): the confirming
            # empty scan comes on a later tick and must reuse this result, not start a
            # second collection (review #20c: exactly one per run exit).
            return entry
        entry = {"done": False, "git": None}
        _GIT_INFO[run_id] = entry
    threading.Thread(
        target=_git_info_worker,
        args=(entry, task["workspace_path"], task["branch"], task["base_commit"]),
        name=f"mesh-git-{run_id}",
        daemon=True,
    ).start()
    return None


def _pending_failure(run: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if not run["result_json"]:
        return None
    try:
        value = (json.loads(run["result_json"]) or {}).get("pending_failure")
    except (ValueError, TypeError, AttributeError):
        return None
    return value if isinstance(value, dict) else None
