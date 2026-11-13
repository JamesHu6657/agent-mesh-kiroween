"""`task merge` (plan §10.5, §6.2 T14–T17): the merge pipeline (m2-r5) with its test step (m2-r6).

Flow (one request, held open until the pipeline ends):

1. A read-only git probe of the integration HEAD (worker thread, no transaction).
2. **TX1** (``BEGIN IMMEDIATE``): the checks, in this order — task exists (rc 5),
   authz (rc 4), ``kind=code`` (rc 2 ``param:"kind"``), status ``accepted`` or
   ``merge_failed`` (T14/T17, else rc 6 ``INVALID_TRANSITION``), no task lease (rc 6
   ``RUN_STILL_ACTIVE``), ``merge:<prj>`` free (rc 11 ``CONCURRENCY_LIMIT``,
   ``details.lease``). Then T14/T17 →
   ``merging``, a ``merge_test`` run (``run_dir=$MESH_HOME/merges/<mrg>/``, status
   ``running``, no process), the ``merge:<prj>`` lease (holder meshd), ``merge_json``
   and the ``task.merging`` event. COMMIT.
3. **Pipeline**: git only, in a worker thread, outside any transaction, while the
   lease serializes merges per project. The asyncio task is shielded, so a client
   disconnect does not cancel it. When the project has a ``test_command`` the
   merged tree is tested between ``merge --no-commit`` and ``commit`` (step 3, see
   ``run_test_step``): the command runs through runwrap as the ``merge_test`` run,
   output redacted into ``merges/<mrg>/test.log``; failure or timeout aborts the
   merge (T16 ``tests_failed``). The supervisor's watch and termination loops skip
   ``merge_test`` runs: their exit goes to this pipeline, never to T7/T8.
4. **Final TX**: T15 ``merged`` or T16 ``merge_failed`` (+ ``merge.failed`` event,
   fanned out to the lead), ``merge_json`` completed, the run closed, the lease
   released.

T16 reasons map to codes per §13.2. A git command that fails where the pipeline
does not expect it (it cannot even read HEAD or status) is T16 ``git_failed`` with
rc 1 ``INTERNAL_ERROR`` (same rule as m2-r3). Nothing here deletes a branch or a
worktree (§10.7), sets git identity, or uses a non-allow-listed git form (A6).
"""

import asyncio
import errno
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

import mesh.constants as c
from mesh.core.authz import check_authz
from mesh.core.clock import format_utc, now_iso_utc, parse_utc
from mesh.core.db import transaction
from mesh.core.events import record_event
from mesh.core.gitops import run_git
from mesh.core.paths import get_install_root
from mesh.core.proc import ScanResult, get_proc_stat, is_tgid_alive
from mesh.core.ids import generate_id
from mesh.core.redact import redact, redact_value
from mesh.core.repos import integration_worktree_path, tests_view
from mesh.core.states import check_task_transition
from mesh.core.workspaces import get_workspaces_root
from mesh.daemon import children, terminate
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.errors import MeshError

GIT_TIMEOUT_S = 30.0
GIT_MERGE_TIMEOUT_S = 300.0
STDERR_LIMIT = 4096
FILES_LIMIT = 200
TEST_LOG_NAME = "test.log"
TEST_LOG_TAIL_LINES = 50
TEST_POLL_S = 0.2
#: After the wrapper's exit.json, how long to wait for the wrapper process itself.
WRAPPER_EXIT_WAIT_S = 10.0
DEFAULT_KILL_GRACE_S = 15  # the settings-registry default of kill.grace_s
LOG_TAIL_MAX_BYTES = 16 * 1024
LOG_TAIL_LINE_MAX_BYTES = 1024

# §13.2: T16 reason → error.code. `git_failed` is not a T16 reason of the plan's table;
# it is the internal failure path (rc 1), see the module docstring.
REASON_CODES = {
    "conflict": c.MERGE_CONFLICT,
    "tests_failed": c.TESTS_FAILED,
    "worktree_dirty": c.WORKTREE_DIRTY,
    "integration_dirty": c.INTEGRATION_DIRTY,
    "commit_failed": c.GIT_COMMIT_FAILED,
}


def merge_lease_key(project_id: str) -> str:
    return f"merge:{project_id}"


def merge_in_progress_for_repo(conn: sqlite3.Connection, repo_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Carbon P2-7: is a merge running against ``repo_path`` (compared by realpath)?

    A merge is in progress when any project on the same repository (this one or another
    project sharing the repo) holds the ``merge:<prj>`` lease or has a task in ``merging``.
    Returns ``{project_id, task_id, run_id, lease, reason}`` for the first such merge
    (ordered by project, then task id), or None. Read-only; callers raise or skip.
    """
    if not repo_path:
        return None
    try:
        target = os.path.realpath(repo_path)
    except (OSError, ValueError):
        return None
    for prj in conn.execute("SELECT id, repo_path FROM projects WHERE repo_path IS NOT NULL "
                            "AND repo_path != '' ORDER BY id;").fetchall():
        try:
            if os.path.realpath(prj["repo_path"]) != target:
                continue
        except (OSError, ValueError):
            continue
        key = merge_lease_key(prj["id"])
        lease = conn.execute("SELECT run_id FROM leases WHERE key = ?;", (key,)).fetchone()
        task = conn.execute("SELECT id FROM tasks WHERE project_id = ? AND status = 'merging' "
                            "ORDER BY id LIMIT 1;", (prj["id"],)).fetchone()
        if lease is None and task is None:
            continue
        task_id = task["id"] if task is not None else None
        run_id = lease["run_id"] if lease is not None else None
        if task_id is None and run_id is not None:
            holder = conn.execute("SELECT task_id FROM runs WHERE id = ?;", (run_id,)).fetchone()
            task_id = holder["task_id"] if holder is not None else None
        return {"project_id": prj["id"], "task_id": task_id, "run_id": run_id,
                "lease": key if lease is not None else None, "reason": "merge_in_progress"}
    return None


def _load(conn: sqlite3.Connection, task_id: str) -> Tuple[Optional[sqlite3.Row], Optional[sqlite3.Row]]:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
    if not task:
        return None, None
    project = conn.execute("SELECT * FROM projects WHERE id = ?;", (task["project_id"],)).fetchone()
    return task, project


def _plus_seconds(iso: str, seconds: int) -> str:
    return format_utc(parse_utc(iso) + timedelta(seconds=seconds))


def _boot_id(conn: sqlite3.Connection) -> str:
    from mesh.daemon.supervisor import _get_current_boot_id

    return _get_current_boot_id(conn)


# ---------------------------------------------------------------------------
# Checks + TX1
# ---------------------------------------------------------------------------

def check_merge_request(ctx: HandlerContext, task_id: Any) -> Tuple[sqlite3.Row, sqlite3.Row]:
    """The §10.5 guards in the r5 order. Runs inside TX1; raises MeshError."""
    if not task_id or not isinstance(task_id, str):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Task ID is required",
                        details={"param": "task_id"})
    task, project = _load(ctx.conn, task_id)
    if task is None:
        raise MeshError(code=c.NOT_FOUND, exit_code=c.EXIT_NOT_FOUND, message=f"Task '{task_id}' not found",
                        details={"task_id": task_id})
    project_id = task["project_id"]
    check_authz(ctx.conn, "task.merge", ctx.principal, ctx.actor_kind, ctx.via,
                project_id=project_id, task_id=task_id)
    if task["kind"] != "code":
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message=f"Task '{task_id}' is kind '{task['kind']}'; only code tasks are merged",
                        details={"param": "kind", "kind": task["kind"], "task_id": task_id})
    # T14 (accepted) / T17 (merge_failed) → merging; anything else is rc 6 INVALID_TRANSITION.
    check_task_transition(task["status"], "merging")
    if ctx.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)).fetchone():
        raise MeshError(code=c.RUN_STILL_ACTIVE, exit_code=c.EXIT_CONFLICT,
                        message=f"Cannot merge task '{task_id}': active lease still held",
                        details={"task_id": task_id})
    key = merge_lease_key(project_id)
    held = ctx.conn.execute("SELECT key, run_id FROM leases WHERE key = ?;", (key,)).fetchone()
    if held:
        holder_task = ctx.conn.execute("SELECT task_id FROM runs WHERE id = ?;", (held["run_id"],)).fetchone()
        raise MeshError(code=c.CONCURRENCY_LIMIT, exit_code=c.EXIT_BUSY,
                        message=f"Another merge is running in project '{project_id}'",
                        details={"lease": key, "run_id": held["run_id"],
                                 "task_id": holder_task["task_id"] if holder_task else None})
    if project is None or not project["repo_path"] or not task["branch"] or not task["workspace_path"]:
        # Defensive: a code task in a repo project always has these once it ran (Q13, r4).
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                        message=f"Task '{task_id}' has no branch/worktree to merge",
                        details={"param": "kind", "reason": "no_repo" if (project is None or not project["repo_path"])
                                 else "no_branch", "task_id": task_id})
    return task, project


def _kill_grace_s(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value_json FROM settings WHERE key = 'kill.grace_s';").fetchone()
    if row is not None and row["value_json"]:
        try:
            return int(json.loads(row["value_json"]))
        except (TypeError, ValueError):
            pass
    return DEFAULT_KILL_GRACE_S


def _tx1(ctx: HandlerContext, task: sqlite3.Row, project: sqlite3.Row, pre_head: Optional[str]) -> Dict[str, Any]:
    """T14/T17 → merging, the merge_test run, the merge lease, merge_json, the event (no COMMIT here)."""
    conn = ctx.conn
    now = now_iso_utc()
    task_id = task["id"]
    project_id = task["project_id"]
    from_state = task["status"]
    transition = "T14" if from_state == "accepted" else "T17"
    mrg = generate_id("mrg")
    run_id = generate_id("run")
    run_dir = os.path.join(ctx.home, "merges", mrg)
    boot_id = _boot_id(conn)
    test_timeout_s = int(project["test_timeout_s"] or 1800)
    lease_s = test_timeout_s + 120
    tests = tests_view(project["test_command_json"])
    test_argv = list(tests) if isinstance(tests, list) and tests else None
    merge_json = {"mrg": mrg, "pre_head": pre_head, "started_at": now, "run_id": run_id}

    conn.execute(
        "UPDATE tasks SET status = 'merging', merge_json = ?, version = version + 1, updated_at = ? WHERE id = ?;",
        (json.dumps(merge_json), now, task_id),
    )
    conn.execute(
        """INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status,
                             boot_id, run_dir, argv_redacted_json, started_at)
           VALUES (?, 'merge_test', ?, ?, NULL, ?, ?, 'mesh', 'running', ?, ?, '[]', ?);""",
        (run_id, task_id, project_id, task["attempt"], task["fence_seq"], boot_id, run_dir, now),
    )
    conn.execute(
        """INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, expires_at, heartbeat_at)
           VALUES (?, ?, 'meshd', ?, NULL, ?, ?, ?, ?);""",
        (merge_lease_key(project_id), run_id, boot_id, task["fence_seq"], now, _plus_seconds(now, lease_s), now),
    )
    evt = record_event(
        conn=conn, type_="task.merging", actor_id=ctx.principal["id"], actor_kind=ctx.actor_kind, via=ctx.via,
        project_id=project_id, task_id=task_id, run_id=run_id, from_state=from_state, to_state="merging",
        payload={"merge_id": mrg, "run_id": run_id, "transition": transition, "pre_head": pre_head},
        request_id=ctx.request_id, relayed_by=ctx.relayed_by, credential_id=ctx.credential_id, k_user=ctx.k_user,
    )
    ws_root = get_workspaces_root(conn, ctx.home)
    return {
        "task_id": task_id,
        "project_id": project_id,
        "title": task["title"],
        "branch": task["branch"],
        "worktree": task["workspace_path"],
        "integration": integration_worktree_path(ws_root, project_id),
        "integration_branch": project["integration_branch"],
        "merge_id": mrg,
        "run_id": run_id,
        "run_dir": run_dir,
        "pre_head": pre_head,
        "started_at": now,
        "test_argv": test_argv,
        "test_timeout_s": test_timeout_s,
        "kill_grace_s": _kill_grace_s(conn),
        "from_state": from_state,
        "transition": transition,
        "event": evt,
    }


# ---------------------------------------------------------------------------
# Pipeline (git only; runs in a worker thread)
# ---------------------------------------------------------------------------

class _GitFailed(Exception):
    def __init__(self, step: str, cp: Any = None, exc: Optional[BaseException] = None):
        super().__init__(step)
        self.step = step
        self.details: Dict[str, Any] = {"step": step}
        if cp is not None:
            self.details["returncode"] = cp.returncode
            self.details["stderr"] = _clip(cp.stderr)
        if exc is not None:
            self.details["error"] = f"{type(exc).__name__}: {exc}"


def _clip(text: Any) -> str:
    s = redact(text or "") if isinstance(text, str) else ""
    s = s.strip()
    if len(s) > STDERR_LIMIT:
        s = s[-STDERR_LIMIT:]
    return s


def _git(step: str, args: List[str], cwd: str, timeout_s: float = GIT_TIMEOUT_S) -> Any:
    try:
        return run_git(args, cwd=cwd, timeout_s=timeout_s)
    except Exception as exc:  # timeout, missing cwd, …
        raise _GitFailed(step, exc=exc)


def _head(cwd: str, step: str) -> str:
    cp = _git(step, ["rev-parse", "--verify", "HEAD"], cwd)
    if cp.returncode != 0:
        raise _GitFailed(step, cp)
    return cp.stdout.strip()


def _dirty_paths(cwd: str, step: str) -> List[str]:
    cp = _git(step, ["status", "--porcelain=v1", "--untracked-files=all"], cwd)
    if cp.returncode != 0:
        raise _GitFailed(step, cp)
    from mesh.core.repos import _porcelain_paths

    return _porcelain_paths(cp.stdout)


def _merge_head_present(cwd: str) -> bool:
    cp = _git("verify MERGE_HEAD", ["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd)
    return cp.returncode == 0


_IDENTITY_MARKERS = (
    "please tell me who you are",
    "identity unknown",
    "unable to auto-detect email address",
    "no email was given",
    "empty ident name",
)
#: Locale-independent markers: git's identity hint quotes these config commands
#: verbatim in every translation (review m2-r4b-r5 finding 2).
_IDENTITY_CONFIG_MARKERS = ("user.email", "user.name")


def _identity_error(stderr: str) -> bool:
    low = (stderr or "").lower()
    if any(m in low for m in _IDENTITY_MARKERS):
        return True
    return all(m in low for m in _IDENTITY_CONFIG_MARKERS)


def classify_merge_failure(unmerged: List[str], stderr: str) -> str:
    """Reason for a failed `merge --no-ff --no-commit` (§10.5 step 2).

    Unmerged paths → ``conflict``. With none, it is never a conflict: an identity
    failure (K8, git refuses the merge itself) → ``commit_failed``; anything else →
    ``git_failed`` (rc 1).
    """
    if unmerged:
        return "conflict"
    if _identity_error(stderr):
        return "commit_failed"
    return "git_failed"


def _capped(details: Dict[str, Any], name: str, items: List[str]) -> Dict[str, Any]:
    """Put a path list into details, capped at FILES_LIMIT with count and truncated flag."""
    details[name] = list(items[:FILES_LIMIT])
    details[f"{name}_count"] = len(items)
    details[f"{name}_truncated"] = len(items) > FILES_LIMIT
    return details


def probe_integration_head(integration: str) -> Optional[str]:
    """Read-only probe used before TX1 (None when it cannot be read)."""
    try:
        return _head(integration, "rev-parse integration HEAD")
    except _GitFailed:
        return None


def _abort_and_verify(integ: str, pre_head: str, abort: bool = True) -> Dict[str, Any]:
    """`merge --abort`, then HEAD == pre_head, clean tree, no MERGE_HEAD (§10.5 step 2).

    ``abort=False`` (a noop merge, nothing in progress) only verifies."""
    out: Dict[str, Any] = {}
    cp = _git("merge --abort", ["merge", "--abort"], integ) if abort else None
    if cp is not None and cp.returncode != 0:
        out["abort_stderr"] = _clip(cp.stderr)
    try:
        head = _head(integ, "verify HEAD")
        dirty = _dirty_paths(integ, "verify status")
        merge_head = _merge_head_present(integ)
    except _GitFailed as gf:
        out.update({"restored": False, "verify_error": gf.details})
        return out
    out["restored"] = head == pre_head and not dirty and not merge_head
    if not out["restored"]:
        out.update({"head": head, "merge_head": merge_head})
        _capped(out, "dirty_files", dirty)
    return out


# ---------------------------------------------------------------------------
# Test step (§10.5 step 3; runs in the pipeline's worker thread)
# ---------------------------------------------------------------------------

def _test_env(run_id: str) -> Dict[str, str]:
    """meshd env minus MESH_*, plus MESH_RUN_ID; no run token, no socket.

    The wrapper needs the install root on PYTHONPATH to start; it hands the test
    command the caller's own PYTHONPATH back (spec ``restore_pythonpath``).
    """
    base = dict(os.environ)
    orig = base.get("_MESH_ORIG_PYTHONPATH")
    env = c.build_external_env(base)
    if orig is None:
        orig = env.get("PYTHONPATH", "__UNSET__")
    env["_MESH_ORIG_PYTHONPATH"] = orig
    env["MESH_RUN_ID"] = run_id
    install_root = get_install_root()
    env["PYTHONPATH"] = install_root if orig == "__UNSET__" else install_root + os.pathsep + orig
    return env


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _clip_utf8(text: str, limit: int) -> Tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


def _log_tail(path: str, lines: int = TEST_LOG_TAIL_LINES) -> List[str]:
    return log_tail_capped(path, lines)[0]


def log_tail_capped(path: str, lines: int = TEST_LOG_TAIL_LINES) -> Tuple[List[str], bool]:
    """Last ``lines`` lines, each at most 1 KiB and all together at most 16 KiB of UTF-8
    (review m2-r5b-r6 finding 3). Returns ``(lines, truncated)``; the newest lines win."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read()
    except OSError:
        return [], False
    tail = data.decode("utf-8", errors="replace").splitlines()[-lines:]
    truncated = False
    out: List[str] = []
    total = 0
    for line in reversed(tail):
        line, cut = _clip_utf8(line, LOG_TAIL_LINE_MAX_BYTES)
        truncated = truncated or cut
        n = len(line.encode("utf-8")) + 1
        if total + n > LOG_TAIL_MAX_BYTES:
            truncated = True
            break
        out.append(line)
        total += n
    out.reverse()
    return out, truncated


def _survivors(t: Any) -> List[int]:
    """The pids still targeted at the moment of kill_failed (not every pid ever seen)."""
    try:
        live = terminate.run_targets(t.run_id, t.pgid, t.wrapper_pid, t.wrapper_starttime, t.seen)
    except Exception:
        live = {}
    return sorted(live) or sorted(t.seen)


def _step_terminations(terms: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Drive several §11.4 terminations together until each is done or kill_failed, so the
    total wait is one grace + KILL_WAIT_S, not one per run (review m2-r7 finding 4).
    Returns run_id -> {"targets", ["kill_failed", "pids"]}; every termination is forgotten."""
    out: Dict[str, Dict[str, Any]] = {rid: {"targets": len(t.initial_pids)} for rid, t in terms.items()}
    pending = dict(terms)
    try:
        while pending:
            for rid, t in list(pending.items()):
                try:
                    state = terminate.step(t)
                except Exception as exc:
                    out[rid]["error"] = f"{type(exc).__name__}: {exc}"
                    out[rid]["kill_failed"] = True
                    out[rid]["pids"] = sorted(t.seen)
                    del pending[rid]
                    continue
                if state == "done":
                    del pending[rid]
                elif state == "kill_failed" or t.kill_failed:
                    out[rid]["kill_failed"] = True
                    out[rid]["pids"] = _survivors(t)
                    if getattr(t, "scan_uncertain", False):
                        out[rid]["scan_uncertain"] = True
                    del pending[rid]
            if pending:
                time.sleep(terminate.POLL_S)
    finally:
        for rid in terms:
            terminate.forget(rid)
    return out


def _terminate_test(run_id: str, pid: Optional[int], starttime: Optional[int], end_reason: str,
                    grace_s: float, seen: Optional[Dict[int, int]] = None) -> Dict[str, Any]:
    """§11.4 termination of the test run's group and MESH_RUN_ID carriers, driven here.

    ``seen`` hands identities an earlier scan found to begin() (review #20b L1 handoff).
    """
    try:
        t = terminate.begin(run_id, pid, pid, end_reason, grace_s, wrapper_starttime=starttime,
                            seen=seen)
    except BaseException:
        terminate.forget(run_id)
        raise
    return _step_terminations({run_id: t})[run_id]


def _exited_unreaped(proc: "subprocess.Popen[Any]") -> bool:
    """True once ``proc`` has exited, without reaping it (``waitid`` with WNOWAIT).

    An already-reaped child (``returncode`` set, or ECHILD) also counts as exited.
    """
    if proc.returncode is not None:
        return True
    try:
        res = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return res is not None and res.si_pid == proc.pid


def _wait_exit_unreaped(proc: "subprocess.Popen[Any]", timeout_s: float) -> bool:
    """Wait up to ``timeout_s`` for ``proc`` to exit, leaving it unreaped (a zombie).

    Returns True if it exited (see :func:`_exited_unreaped`).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if _exited_unreaped(proc):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(TEST_POLL_S, 0.05))


def _kill_test_proc_direct(proc: "subprocess.Popen[Any]", grace_s: float,
                           force_group_kill: bool = False) -> None:
    """Carbon RO P1-1 (2C): directly terminate meshd's own unreaped ``merge_test`` Popen
    session/group when ``/proc`` failures keep :func:`_terminate_test` from enumerating it.

    ``proc`` was started with ``start_new_session=True`` and has not been reaped yet
    (``proc.returncode is None``), so ``pgid == proc.pid`` is kernel-reserved and cannot
    have been reused by an unrelated process.
    """
    if proc.returncode is not None or proc.pid <= 2 or proc.pid == os.getpgrp():
        return
    if not _exited_unreaped(proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
        if _wait_exit_unreaped(proc, max(0.0, grace_s)) and not force_group_kill:
            return
    elif not force_group_kill:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    _wait_exit_unreaped(proc, terminate.KILL_WAIT_S)


def run_test_step(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Run the project's test command in the integration worktree (§10.5 step 3).

    The command runs under runwrap (own session/process group, registered child),
    cwd = integration worktree, env = meshd env minus MESH_* plus MESH_RUN_ID, no
    run token; deadline ``test_timeout_s``; stdout+stderr redacted into
    ``<run_dir>/test.log``. On timeout the §11.4 termination runs (end_reason
    ``timeout``). Leftover MESH_RUN_ID carriers after a normal exit are terminated
    too. Returns ``{"passed", "exit_code", "signal", "timeout", "log_path",
    "wrapper": {pid, starttime}, ...}``; never raises for a test failure.
    """
    run_id = plan["run_id"]
    run_dir = plan["run_dir"]
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    log_path = os.path.join(run_dir, TEST_LOG_NAME)
    exit_path = os.path.join(run_dir, "exit.json")
    spec = {
        "argv": list(plan["test_argv"]),
        "cwd": plan["integration"],
        "stdin": "devnull",
        "redact_env_names": [],
        "idle_timeout_s": 0,
        "combined_log": TEST_LOG_NAME,
        "restore_pythonpath": True,
    }
    tmp = os.path.join(run_dir, "spec.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(spec, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, os.path.join(run_dir, "spec.json"))

    timeout_s = float(plan["test_timeout_s"])
    started = time.monotonic()
    registration_error: Optional[str] = None
    try:
        proc = children.popen_registered(
            "merge_test",
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", run_dir],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=plan["integration"], env=_test_env(run_id), start_new_session=True, close_fds=True,
        )
    except children.SpawnedThenFailed as exc:
        # Round 10 (Linear P1-4): Popen succeeded but registration failed. The wrapper is
        # still our unreaped child: it is held from the orphan reaper and goes through the
        # normal step (exit wait, leftover scan and termination before the reap), so the
        # wrapper and its group are never left running with the run closed.
        proc = exc.proc
        children.hold(proc.pid)
        registration_error = f"{type(exc.exc).__name__}: {exc.exc}" if getattr(exc, "exc", None) else str(exc)
    try:
        info = get_proc_stat(proc.pid)
    except OSError:
        info = None
    starttime = int(info["starttime"]) if info else None
    if starttime is None:
        # Round 9 (Linear P1-b): fill the identity from wrapper.json (written by runwrap).
        deadline_fill = time.monotonic() + 2.0
        while starttime is None and time.monotonic() < deadline_fill:
            w = _read_json(os.path.join(run_dir, "wrapper.json")) or {}
            if w.get("pid") == proc.pid and isinstance(w.get("starttime"), int) and w["starttime"] > 0:
                starttime = int(w["starttime"])
                break
            time.sleep(0.02)
    out: Dict[str, Any] = {"log_path": log_path, "timeout": False, "timeout_s": int(timeout_s),
                           "wrapper": {"pid": proc.pid, "starttime": starttime}}
    if registration_error:
        out["registration_error"] = registration_error
    try:
        exit_info: Optional[Dict[str, Any]] = None
        while True:
            exit_info = _read_json(exit_path)
            if exit_info is not None:
                break
            if _exited_unreaped(proc):
                exit_info = _read_json(exit_path)  # written just before the wrapper exits
                break
            if time.monotonic() - started >= timeout_s:
                grace = float(plan.get("kill_grace_s") or DEFAULT_KILL_GRACE_S)
                out["timeout"] = True
                out["termination"] = _terminate_test(run_id, proc.pid, starttime, "timeout", grace)
                if out["termination"].get("scan_uncertain"):
                    out["leftover_scan_uncertain"] = True
                if not _exited_unreaped(proc) or out["termination"].get("scan_uncertain"):
                    _kill_test_proc_direct(
                        proc, grace, force_group_kill=bool(out["termination"].get("scan_uncertain"))
                    )
                exit_info = _read_json(exit_path)
                break
            time.sleep(TEST_POLL_S)
        # M3 gate P1-2: wait for the wrapper's exit WITHOUT reaping it. Until meshd
        # reaps it, the zombie wrapper's pid (= the run's pgid/sid) cannot be reused, so
        # its group stays trusted (terminate._wrapper_identity_alive) for the leftover
        # scans and the termination below; reaping first lost every same-group member
        # without MESH_RUN_ID (e.g. env={} with stdio on /dev/null).
        if not _wait_exit_unreaped(proc, WRAPPER_EXIT_WAIT_S):
            if out["timeout"]:
                _kill_test_proc_direct(proc, 0.0, force_group_kill=True)
            if not _exited_unreaped(proc):
                out["wrapper_alive"] = True
        if not out["timeout"]:
            # Leftovers (§11.3): anything still carrying MESH_RUN_ID or in the group.
            # Scanned before the reap; the identities found are handed to begin().
            leftovers = terminate.run_targets_or_recheck(run_id, proc.pid, proc.pid, starttime)
            if leftovers or getattr(leftovers, "uncertain", False):
                if leftovers:
                    out["leftovers"] = len(leftovers)
                out["termination"] = _terminate_test(run_id, proc.pid, starttime, "leftovers",
                                                     float(plan.get("kill_grace_s") or DEFAULT_KILL_GRACE_S),
                                                     seen=leftovers)
                if out["termination"].get("scan_uncertain"):
                    out["leftover_scan_uncertain"] = True
                if out["termination"].get("kill_failed"):
                    # A leftover that survives SIGKILL (or an uncertain scan that never cleared)
                    # is not a pass (review m2-r5b-r6 finding 1; ClickUp P1-1).
                    out["error"] = (
                        "leftover_scan_uncertain"
                        if out["termination"].get("scan_uncertain") and not leftovers
                        else "leftovers_kill_failed"
                    )
                elif getattr(leftovers, "uncertain", False):
                    # ClickUp P1-1: an uncertain leftover scan is never read as "no leftovers",
                    # though _terminate_test subsequently confirmed clean so close is not wedged.
                    out["error"] = "leftover_scan_uncertain"
        if starttime is None:
            # Round 9 (Linear P1-b): unknown wrapper identity: fail closed. The wrapper is
            # still our unreaped child, so its pgid is reserved and its group members are
            # the run's: they are terminated by pid + starttime; never a pass.
            from mesh.daemon import supervisor

            out["error"] = "wrapper_identity_unknown"
            grace = float(plan.get("kill_grace_s") or DEFAULT_KILL_GRACE_S)
            group_scan = supervisor._scan_group(proc.pid)
            # ClickUp recheck P2: an empty but uncertain scan (EMFILE, EIO) is never read as
            # "no members". Rescan until certain, bounded by the kill grace; the wrapper is
            # still our unreaped child, so its pgid stays reserved meanwhile.
            deadline = time.monotonic() + max(grace, 1.0)
            while getattr(group_scan, "uncertain", False) and time.monotonic() < deadline:
                time.sleep(terminate.POLL_S)
                group_scan = supervisor._scan_group(proc.pid)
            members = ScanResult(
                {p: st for p, st in group_scan.items() if p != proc.pid},
                uncertain=bool(getattr(group_scan, "uncertain", False)),
            )
            if members.uncertain:
                out["leftover_scan_uncertain"] = True
            if members or members.uncertain:
                if members:
                    out["leftovers"] = len(members)
                out["termination"] = _terminate_test(run_id, proc.pid, None, "leftovers", grace,
                                                     seen=members)
                if members.uncertain:
                    # Survival unknown: reported as kill_failed + scan_uncertain (never a pass);
                    # live and recovered close gates hold merge_test open and keep merge:<prj>
                    # (Carbon RO P1-1; D25 applies only to confirmed survivors).
                    out["termination"]["kill_failed"] = True
                    out["termination"]["scan_uncertain"] = True
    finally:
        # Reaped only now, after the leftover scan and any termination (P1-2).
        if proc.poll() is not None:
            children.unregister(proc.pid)
            children.unhold(proc.pid)
    out["duration_s"] = round(time.monotonic() - started, 3)
    if exit_info is None:
        out.update({"exit_code": None, "signal": None})
        if not out["timeout"]:
            out["error"] = "no_exit_json"
    else:
        out.update({"exit_code": exit_info.get("exit_code"), "signal": exit_info.get("signal")})
        if exit_info.get("error"):
            out["error"] = exit_info["error"]
    out["passed"] = (not out["timeout"]) and out["exit_code"] == 0 and "error" not in out
    return out


def run_merge_pipeline(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """§10.5 steps 1–4 (the test step since r6). Returns the outcome; never raises.

    Failure details are redacted (§15.2) before they reach merge_json, events, the
    outbox or the error envelope (review m2-r4b-r5 finding 5).
    """
    out = _run_merge_pipeline(plan)
    if out.get("details"):
        out["details"] = redact_value(out["details"])
    return out


def _integrating_commit(integ: str, tip: str) -> str:
    """The commit of ``integ``'s first-parent history that brought ``tip`` in (Carbon P2-5).

    The oldest first-parent commit of HEAD that has ``tip`` as an ancestor: the merge commit
    that merged the task earlier, or the first commit on top of a branch that is simply
    contained in integration. ``tip`` itself when nothing sits on top of it (HEAD is ``tip``)."""
    cp = _git("log integrating commit",
              ["log", "--first-parent", "--ancestry-path", "--format=%H", f"{tip}..HEAD"], integ)
    if cp.returncode != 0:
        raise _GitFailed("log integrating commit", cp)
    shas = [ln.strip() for ln in (cp.stdout or "").splitlines() if ln.strip()]
    return shas[-1] if shas else tip


def _run_noop_merge(plan: Mapping[str, Any], integ: str, tip: str, pre_head: str) -> Dict[str, Any]:
    """Carbon P2-5: the task branch is already contained in integration (a zero-commit task,
    or a merge commit that exists but was never recorded). Nothing is merged, but the gate
    is not skipped: ``test_command`` still runs on the integration tree, and the recorded
    ``merge_commit`` is the commit that really carries the task, never the task's tip."""
    tests: Dict[str, Any] = {"tests": "none", "test_log": None}
    if plan.get("test_argv"):
        test = run_test_step(plan)
        tests = {"tests": "passed" if test["passed"] else "failed", "test_log": test["log_path"],
                 "test": {k: v for k, v in test.items() if k != "log_path"}}
        if not test["passed"]:
            verify = _abort_and_verify(integ, pre_head, abort=False)
            return {"outcome": "failed", "reason": "tests_failed", "pre_head": pre_head,
                    "details": _test_failure_details(test, verify), **tests}
    return {"outcome": "merged", "noop": True, "pre_head": pre_head,
            "merge_commit": _integrating_commit(integ, tip), **tests}


def _test_failure_details(test: Mapping[str, Any], verify: Mapping[str, Any]) -> Dict[str, Any]:
    tail, tail_cut = log_tail_capped(test["log_path"])
    details = {"log_path": test["log_path"], "log_tail": tail, "log_tail_truncated": tail_cut,
               "timeout": test["timeout"], "timeout_s": test["timeout_s"],
               "exit_code": test["exit_code"], "signal": test["signal"]}
    for k in ("error", "termination", "leftovers", "leftover_scan_uncertain"):
        if k in test:
            details[k] = test[k]
    if (test.get("termination") or {}).get("kill_failed"):
        details["kill_failed"] = True
    details.update(verify)
    return details


def _run_merge_pipeline(plan: Mapping[str, Any]) -> Dict[str, Any]:
    integ = plan["integration"]
    wt = plan["worktree"]
    branch = plan["branch"]
    pre_head: Optional[str] = plan.get("pre_head")
    try:
        # The lease is held now, so this read is the authoritative pre_head.
        pre_head = _head(integ, "rev-parse integration HEAD")
        dirty = _dirty_paths(wt, "status task worktree")
        if dirty:
            return {"outcome": "failed", "reason": "worktree_dirty", "pre_head": pre_head,
                    "details": _capped({"worktree": wt}, "dirty_files", dirty)}
        dirty = _dirty_paths(integ, "status integration")
        if dirty:
            return {"outcome": "failed", "reason": "integration_dirty", "pre_head": pre_head,
                    "details": _capped({"integration": integ}, "dirty_files", dirty)}
        # The task branch is resolved once, by its full ref, to a sha: a tag named like the
        # branch must never be merged instead (review m2-r7 finding 6, as r8 N-1 for deliver).
        cp = _git("rev-parse branch", ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], integ)
        if cp.returncode != 0:
            raise _GitFailed("rev-parse branch", cp)
        tip = (cp.stdout or "").strip()
        cp = _git("merge-base --is-ancestor", ["merge-base", "--is-ancestor", tip, "HEAD"], integ)
        if cp.returncode not in (0, 1):
            raise _GitFailed("merge-base --is-ancestor", cp)
        if cp.returncode == 0:
            return _run_noop_merge(plan, integ, tip, pre_head)

        cp = _git("merge", ["merge", "--no-ff", "--no-commit", tip], integ, GIT_MERGE_TIMEOUT_S)
        if cp.returncode != 0:
            files_cp = _git("diff unmerged", ["diff", "--name-only", "--diff-filter=U"], integ)
            files = [f for f in (files_cp.stdout or "").splitlines() if f.strip()] if files_cp.returncode == 0 else []
            stderr = _clip(cp.stderr) or _clip(cp.stdout)
            verify = _abort_and_verify(integ, pre_head)
            reason = classify_merge_failure(files, stderr)
            if reason == "conflict":
                details = _capped({"stderr": stderr}, "files", files)
            else:
                # commit_failed: git refuses `merge --no-commit` itself when no identity is
                # configured (it needs a committer for the reflog), so K8's failure surfaces
                # here, not at `commit`. git_failed: no unmerged path and no identity
                # message — never reported as a conflict (review m2-r4b-r5 finding 2).
                details = {"stderr": stderr, "returncode": cp.returncode, "step": "merge"}
            details.update(verify)
            return {"outcome": "failed", "reason": reason, "pre_head": pre_head, "details": details}

        tests: Dict[str, Any] = {"tests": "none", "test_log": None}
        if plan.get("test_argv"):
            test = run_test_step(plan)
            tests = {"tests": "passed" if test["passed"] else "failed", "test_log": test["log_path"],
                     "test": {k: v for k, v in test.items() if k != "log_path"}}
            if not test["passed"]:
                verify = _abort_and_verify(integ, pre_head)
                return {"outcome": "failed", "reason": "tests_failed", "pre_head": pre_head,
                        "details": _test_failure_details(test, verify), **tests}

        msg = f"Merge mesh task {plan['task_id']}: {plan['title']}"
        cp = _git("commit", ["commit", "--no-edit", "-m", msg], integ, GIT_MERGE_TIMEOUT_S)
        if cp.returncode != 0:
            stderr = _clip(cp.stderr) or _clip(cp.stdout)
            verify = _abort_and_verify(integ, pre_head)
            details = {"stderr": stderr, "returncode": cp.returncode}
            details.update(verify)
            return {"outcome": "failed", "reason": "commit_failed", "pre_head": pre_head, "details": details,
                    **tests}
        merge_commit = _head(integ, "rev-parse merge commit")
        _maybe_crash_after_commit(plan)
        return {"outcome": "merged", "noop": False, "pre_head": pre_head, "merge_commit": merge_commit, **tests}
    except _GitFailed as gf:
        out: Dict[str, Any] = {"outcome": "failed", "reason": "git_failed", "pre_head": pre_head,
                               "details": dict(gf.details)}
        if pre_head:
            try:
                if _merge_head_present(integ):
                    out["details"].update(_abort_and_verify(integ, pre_head))
            except _GitFailed:
                pass
        return out
    except Exception as exc:  # never leave the task in `merging` because of a bug here
        return {"outcome": "failed", "reason": "git_failed", "pre_head": pre_head,
                "details": {"error": f"{type(exc).__name__}: {exc}"}}


# ---------------------------------------------------------------------------
# Testing-only crash hook (§17.1; m2-r7)
# ---------------------------------------------------------------------------

CRASH_AFTER_MERGE_COMMIT = "after_merge_commit"


def _maybe_crash_after_commit(plan: Mapping[str, Any]) -> None:
    """``MESH_TEST_CRASH_AT=after_merge_commit`` (testing mode only): SIGKILL meshd right
    after the merge commit, before T15, like `kill -9` (no cleanup, no flush)."""
    from mesh.daemon import supervisor

    if not supervisor._testing_mode() or os.environ.get("MESH_TEST_CRASH_AT") != CRASH_AFTER_MERGE_COMMIT:
        return
    home = os.path.dirname(os.path.dirname(plan["run_dir"].rstrip(os.sep)))
    supervisor._log_meshd(home, f"MESH_TEST_CRASH_AT={CRASH_AFTER_MERGE_COMMIT}: killing meshd during "
                                f"merge {plan['merge_id']} of task {plan['task_id']}")
    os.kill(os.getpid(), signal.SIGKILL)


# ---------------------------------------------------------------------------
# Crash recovery (§11.7 step 2 for merge_test runs, step 5; m2-r7)
# ---------------------------------------------------------------------------

def _live_wrapper(run: Mapping[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    wrapper = _read_json(os.path.join(run["run_dir"] or "", "wrapper.json")) or {}
    try:
        wpid = int(wrapper.get("pid") or 0)
        wst = int(wrapper.get("starttime") or 0)
    except (TypeError, ValueError):
        wpid, wst = 0, 0
    if wpid > 1:
        info = get_proc_stat(wpid)
        if info is not None and int(info.get("starttime") or 0) == wst and is_tgid_alive(wpid, info):
            return wpid, wst
    return None, None


def _log_recovery_error(conn: sqlite3.Connection, run: Mapping[str, Any], reason: str) -> None:
    from mesh.daemon import supervisor

    try:
        # The existing callers pass only the connection and runs, not MESH_HOME.
        db = next(row for row in conn.execute("PRAGMA database_list;") if row["name"] == "main")
        if db["file"]:
            supervisor._log_meshd(os.path.dirname(db["file"]),
                                  f"recovery: merge run {run['id']} task {run['task_id']} failed: {reason}")
    except Exception:
        pass  # A logging failure must not stop the remaining runs.


def _report_recovery_blocked(conn: sqlite3.Connection, run: Mapping[str, Any], exc: Exception) -> None:
    reason = redact(f"{type(exc).__name__}: {exc}")
    _log_recovery_error(conn, run, reason)
    try:
        with transaction(conn):
            blocked = conn.execute(
                "SELECT 1 FROM tasks t JOIN runs r ON r.task_id = t.id "
                "WHERE t.id = ? AND r.id = ? AND t.status = 'merging' "
                "AND r.status IN ('starting', 'running');", (run["task_id"], run["id"]),
            ).fetchone()
            if not blocked or conn.execute(
                "SELECT 1 FROM events WHERE type = 'merge.recovery_blocked' AND task_id = ? AND reason = ? LIMIT 1;",
                (run["task_id"], reason),
            ).fetchone():
                return
            record_event(
                conn=conn, type_="merge.recovery_blocked", actor_id="system", actor_kind="system", via="system",
                project_id=run["project_id"], task_id=run["task_id"], run_id=run["id"], reason=reason,
                payload={"task_id": run["task_id"], "run_id": run["id"], "reason": reason},
            )
    except Exception as report_exc:
        _log_recovery_error(conn, run, redact(f"recording merge.recovery_blocked failed: "
                                             f"{type(report_exc).__name__}: {report_exc}"))


def recover_merge_runs(conn: sqlite3.Connection, runs: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """§11.7 step 2 for open ``merge_test`` runs: never adopted. For each run, a live wrapper
    (``wrapper.json`` pid alive with the same starttime) or any ``MESH_RUN_ID`` carrier is
    terminated (§11.4, end_reason ``interrupted``). All terminations are begun first and
    stepped together, so boot waits at most one grace + KILL_WAIT_S in total (review m2-r7
    finding 4). Each run is then closed ``killed`` (something was terminated) or ``lost``,
    and ``merge:<prj>`` is released, unless the close gate (round 7, :func:`merge_close_gate`)
    finds the group still occupied: then the run stays open and held (:class:`MergeRunHeld`).
    A ``kill_failed`` termination emits ``run.kill_failed``
    with the survivors (finding 1). The task stays ``merging``; step 5 decides it.

    Returns run_id -> status string, or the exception raised for that run (the run is then
    left open, and step 5 skips its task; finding 5). Exceptions are logged with run/task
    ids; a blocked task gets one durable merge.recovery_blocked event per distinct error."""
    from mesh.daemon import supervisor

    grace = _kill_grace_s(conn)
    out: Dict[str, Any] = {}
    prepared: Dict[str, Dict[str, Any]] = {}
    terms: Dict[str, Any] = {}
    for run in runs:
        run_id = run["id"]
        try:
            pid, starttime = _live_wrapper(run)
            known_wst = starttime if pid is not None else supervisor.close_identity(run)[1]
            result: Dict[str, Any] = {"reason": "interrupted", "recovered": True}
            if pid is not None:
                result["wrapper"] = {"pid": pid, "starttime": starttime}
            carriers = {} if pid is not None else terminate.run_targets_or_recheck(run_id, None, None, known_wst)
            if pid is None and not carriers and getattr(carriers, "uncertain", False):
                raise OSError(errno.EIO, f"merge test run {run_id}: carrier scan uncertain")
            if pid is not None or carriers:
                try:
                    terms[run_id] = terminate.begin(run_id, pid, pid, "interrupted", grace,
                                                    wrapper_starttime=known_wst, seen=carriers)
                except BaseException:
                    terminate.forget(run_id)
                    raise
            prepared[run_id] = {"run": run, "pid": pid, "starttime": starttime, "result": result}
        except Exception as exc:
            out[run_id] = exc
    stepped = _step_terminations(terms) if terms else {}
    for run_id, prep in prepared.items():
        try:
            if run_id in stepped:
                prep["result"]["termination"] = stepped[run_id]
                if stepped[run_id].get("error"):
                    _log_recovery_error(conn, prep["run"], redact(stepped[run_id]["error"]))
            out[run_id] = _close_recovered_merge_run(conn, prep["run"], prep["pid"], prep["starttime"],
                                                     prep["result"])
        except Exception as exc:
            out[run_id] = exc
    for run in runs:
        error = out.get(run["id"])
        if isinstance(error, Exception):
            _report_recovery_blocked(conn, run, error)
    return out


def recover_merge_run(conn: sqlite3.Connection, run: Mapping[str, Any]) -> str:
    """One run of :func:`recover_merge_runs`; raises what that run raised."""
    status = recover_merge_runs(conn, [run])[run["id"]]
    if isinstance(status, BaseException):
        raise status
    return status


class MergeRunHeld(Exception):
    """Round 7 (Linear P1): the close-safety gate refused to close a recovered
    ``merge_test`` run: its group/session is still occupied (by processes that are not
    recorded, e.g. env-less test children) and still the run's. The run stays open and
    ``merge:<prj>`` stays held; the task is not decided (step 5 skips it) and a
    ``merge.recovery_blocked`` event records the reason. Re-evaluated at every boot."""

    def __init__(self, run_id: str, pgid: int) -> None:
        super().__init__(f"merge test run {run_id}: process group {pgid} still occupied "
                         f"(wrapper_gone_group_occupied); run and merge lease held")
        self.run_id = run_id
        self.pgid = pgid


def merge_close_gate(conn: sqlite3.Connection, run: Mapping[str, Any], pid: Optional[int],
                     starttime: Optional[int]) -> None:
    """Round 7 / Carbon RO P1-1: the close-safety gate for a merge_test run (live or
    recovered). Identity: the live-wrapper check's pid, else the DB or wrapper.json
    (``close_identity``). A live wrapper was already terminated (two-scan rule) before
    this point; if the group is occupied or scan-uncertain and still the run's, the close
    is refused (:class:`MergeRunHeld`) and the disk identity is recorded in the DB."""
    from mesh.daemon import supervisor

    if pid is not None:
        pgid, wst = pid, starttime
    else:
        pgid, wst = supervisor.close_identity(run)
    if not pgid or not supervisor.group_still_ours(pgid, wst):
        return
    scanned = supervisor._scan_group(int(pgid))
    if not scanned and not getattr(scanned, "uncertain", False):
        return
    sql = ("UPDATE runs SET wrapper_pid = COALESCE(wrapper_pid, ?), pgid = COALESCE(pgid, ?), "
           "proc_starttime = COALESCE(proc_starttime, ?) WHERE id = ?;")
    if conn.in_transaction:
        conn.execute(sql, (pgid, pgid, wst, run["id"]))
    else:
        with transaction(conn):
            conn.execute(sql, (pgid, pgid, wst, run["id"]))
    raise MergeRunHeld(run["id"], int(pgid))


def _check_live_merge_close(conn: sqlite3.Connection, plan: Mapping[str, Any],
                            result: Mapping[str, Any]) -> None:
    """Carbon RO P1-1: enforce the same scan-uncertainty and group-occupancy close gate on
    the live ``task.merge`` path as on :func:`_close_recovered_merge_run`.

    D25 (releasing ``merge:<prj>`` with ``run.kill_failed``) applies only when confirmed
    survivors remain after ``kill_failed`` (``not uncertain``); uncertain scans keep the
    ``merge_test`` run open and ``merge:<prj>`` held.
    """
    from mesh.daemon import supervisor

    run_id = plan.get("run_id")
    if not run_id:
        return
    run = conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
    if run is None or run["status"] not in ("starting", "running"):
        return
    test_info = result.get("test") or {}
    termination = test_info.get("termination") or {}
    wrapper = test_info.get("wrapper") or {}
    wpid = int(wrapper["pid"]) if wrapper.get("pid") else None
    wst = supervisor.known_starttime(wrapper.get("starttime"))
    if wpid is not None:
        sql = ("UPDATE runs SET wrapper_pid = COALESCE(wrapper_pid, ?), pgid = COALESCE(pgid, ?), "
               "proc_starttime = COALESCE(proc_starttime, ?) WHERE id = ?;")
        if conn.in_transaction:
            conn.execute(sql, (wpid, wpid, wst, run_id))
        else:
            with transaction(conn):
                conn.execute(sql, (wpid, wpid, wst, run_id))
        run = conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
        if run is None or run["status"] not in ("starting", "running"):
            return
    uncertain = bool(termination.get("scan_uncertain") or test_info.get("leftover_scan_uncertain"))
    if uncertain:
        merge_close_gate(conn, run, wpid, wst)
        raise OSError(errno.EIO, f"merge test run {run_id}: termination scan uncertain")
    if not termination.get("kill_failed"):
        merge_close_gate(conn, run, wpid, wst)


def _close_recovered_merge_run(conn: sqlite3.Connection, run: Mapping[str, Any], pid: Optional[int],
                               starttime: Optional[int], result: Dict[str, Any]) -> str:
    run_id = run["id"]
    termination = result.get("termination") or {}
    status = "killed" if "termination" in result else "lost"
    if termination.get("scan_uncertain"):
        merge_close_gate(conn, run, pid, starttime)
        raise OSError(errno.EIO, f"merge test run {run_id}: termination scan uncertain")
    if not termination.get("kill_failed"):
        # kill_failed keeps its recorded §11.4 deviation (survivors reported, lease released).
        merge_close_gate(conn, run, pid, starttime)
    with transaction(conn):
        if termination.get("kill_failed"):
            # Deviation from §11.4 (recorded, as on the live path): the lease is released
            # and the survivors are reported, here and in the T16 details (step 5).
            cur = conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()
            if cur is not None and cur["status"] in ("starting", "running"):
                record_event(
                    conn=conn, type_="run.kill_failed", actor_id="system", actor_kind="system", via="system",
                    project_id=run["project_id"], task_id=run["task_id"], run_id=run_id,
                    payload={"run_id": run_id, "task_id": run["task_id"], "end_reason": "interrupted",
                             "pids": termination.get("pids") or [], "lease_released": True,
                             "kind": "merge_test", "recovered": True},
                )
        conn.execute(
            "UPDATE runs SET status = ?, end_reason = 'interrupted', ended_at = ?, result_json = ? "
            "WHERE id = ? AND status IN ('starting', 'running');",
            (status, now_iso_utc(), json.dumps(redact_value(result)), run_id),
        )
        if pid is not None:
            conn.execute("UPDATE runs SET wrapper_pid = COALESCE(wrapper_pid, ?), pgid = COALESCE(pgid, ?), "
                         "proc_starttime = COALESCE(proc_starttime, ?) WHERE id = ?;",
                         (pid, pid, starttime, run_id))
        conn.execute("DELETE FROM leases WHERE key = ? AND run_id = ?;",
                     (merge_lease_key(run["project_id"]), run_id))
    return status


def _inspect_head(integ: str, head: str) -> Tuple[List[str], str]:
    cp = _git("inspect HEAD", ["log", "-1", "--format=%P%n%B", head], integ)
    if cp.returncode != 0:
        raise _GitFailed("inspect HEAD", cp)
    first, _, body = (cp.stdout or "").partition("\n")
    return first.split(), body


def _branch_tip(integ: str, branch: Optional[str]) -> Optional[str]:
    if not branch:
        return None
    cp = _git("rev-parse task branch", ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], integ)
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip() or None


def decide_recovery(integ: str, task_id: str, branch: Optional[str], pre_head: Optional[str]) -> Dict[str, Any]:
    """§11.7 step 5 git decision for a task left in ``merging``.

    - ``MERGE_HEAD`` exists → ``merge --abort`` (then verify) → ``interrupted``;
    - HEAD is a merge commit whose second parent is the task branch tip
      and whose message contains the task id → reconcile ``merged``;
    - otherwise → ``interrupted``.

    ``pre_head`` is the probe stored by TX1, read before the merge lease was held, so it can
    be stale (review m2-r7 finding 2). The authoritative value is taken from git instead:
    during a ``--no-commit`` merge HEAD has not moved, so HEAD read before ``merge --abort``
    is the true pre_head; for a reconciled merge commit it is its first parent. Without
    MERGE_HEAD, inspect the current HEAD and task tip regardless of the probe. An unmatched
    task-related commit remains unrestored (its first parent is the baseline); otherwise
    current HEAD is the baseline, as before a merge or after an earlier recovery's abort.
    A differing probe is kept in ``details.recorded_pre_head``.
    """
    details: Dict[str, Any] = {}
    current_pre = pre_head
    try:
        if _merge_head_present(integ):
            details["merge_head"] = True
            true_pre = _head(integ, "rev-parse HEAD before abort")
            current_pre = true_pre
            if pre_head and pre_head != true_pre:
                details["recorded_pre_head"] = pre_head
            details.update(_abort_and_verify(integ, true_pre))
            return {"outcome": "failed", "reason": "interrupted", "pre_head": true_pre, "details": details}
        head = _head(integ, "rev-parse HEAD")
        current_pre = head
        parents, body = _inspect_head(integ, head)
        tip = _branch_tip(integ, branch)
        if len(parents) >= 2 and tip is not None and parents[1] == tip and task_id in body:
            return {"outcome": "merged", "noop": False, "pre_head": parents[0],
                    "merge_commit": head,
                    **({"recorded_pre_head": pre_head} if pre_head and pre_head != parents[0] else {})}
        true_pre = head
        if parents and (task_id in body or (tip is not None and tip in parents[1:])):
            # Do not claim a mismatched task commit was restored just because its tree
            # is clean. Use its current parent, not the pre-lease probe, as the baseline.
            true_pre = parents[0]
            details.update({"head": head, "head_moved": True, "merge_commit_match": False})
        if pre_head and pre_head != true_pre:
            details["recorded_pre_head"] = pre_head
        current_pre = true_pre
        dirty = _dirty_paths(integ, "verify status")
        if dirty:
            _capped(details, "dirty_files", dirty)
        details["restored"] = head == true_pre and not dirty
        return {"outcome": "failed", "reason": "interrupted", "pre_head": true_pre, "details": details}
    except _GitFailed as gf:
        details.update({"git_error": gf.details, "restored": None})
        if pre_head and pre_head != current_pre:
            details["recorded_pre_head"] = pre_head
        return {"outcome": "failed", "reason": "interrupted", "pre_head": current_pre, "details": details}
    except Exception as exc:
        details.update({"error": f"{type(exc).__name__}: {exc}", "restored": None})
        if pre_head and pre_head != current_pre:
            details["recorded_pre_head"] = pre_head
        return {"outcome": "failed", "reason": "interrupted", "pre_head": current_pre, "details": details}


def recover_merging_task(conn: sqlite3.Connection, home: str, task: Mapping[str, Any]) -> str:
    """§11.7 step 5 for one task in ``merging``: T16 ``interrupted`` or reconciled T15 in
    its own transaction (the final TX of the live path, with ``recovered``/``reconciled``).
    Re-run safe: a task already moved on is skipped by ``_final_tx``'s status check."""
    try:
        mj = json.loads(task["merge_json"] or "{}")
    except (TypeError, ValueError):
        mj = {}
    run_id = mj.get("run_id")
    run = conn.execute("SELECT status, result_json FROM runs WHERE id = ?;", (run_id,)).fetchone() if run_id else None
    if run is not None and run["status"] in ("starting", "running"):
        # Step 2 could not close this task's merge_test run (it raised): its processes may
        # still be live, so the task stays `merging` with its lease, and the next boot
        # retries (review m2-r7 finding 5).
        return "run_open"
    run_termination: Dict[str, Any] = {}
    if run is not None:
        try:
            run_termination = (json.loads(run["result_json"] or "{}") or {}).get("termination") or {}
        except (TypeError, ValueError, AttributeError):
            run_termination = {}
    project = conn.execute("SELECT * FROM projects WHERE id = ?;", (task["project_id"],)).fetchone()
    integ = integration_worktree_path(get_workspaces_root(conn, home), task["project_id"])
    pre_head = mj.get("pre_head")
    if os.path.isdir(integ):
        result = decide_recovery(integ, task["id"], task["branch"], pre_head)
    else:
        result = {"outcome": "failed", "reason": "interrupted", "pre_head": pre_head,
                  "details": {"integration_missing": True, "restored": None}}
    reconciled = result["outcome"] == "merged"
    if run_termination.get("kill_failed") and not reconciled:
        # Survivors of step 2's termination (review m2-r7 finding 1).
        result.setdefault("details", {})
        result["details"]["kill_failed"] = True
        result["details"]["kill_failed_pids"] = run_termination.get("pids") or []
    if reconciled:
        # The commit happens only after the test step passed (§10.5), so a reconciled
        # merge commit implies the tests passed (or there were none).
        tests = tests_view(project["test_command_json"]) if project is not None else None
        has_tests = isinstance(tests, list) and bool(tests)
        log = os.path.join(home, "merges", mj["mrg"], TEST_LOG_NAME) if mj.get("mrg") else None
        result["tests"] = "passed" if has_tests else "none"
        result["test_log"] = log if has_tests and log and os.path.isfile(log) else None
    if result.get("details"):
        result["details"] = redact_value(result["details"])
    plan = {"task_id": task["id"], "project_id": task["project_id"], "merge_id": mj.get("mrg"),
            "run_id": mj.get("run_id"), "pre_head": pre_head,
            "started_at": mj.get("started_at") or task["updated_at"]}
    extra: Dict[str, Any] = {"recovered": True, "reconciled": reconciled}
    if reconciled and result.get("recorded_pre_head"):
        extra["recorded_pre_head"] = result["recorded_pre_head"]
    if reconciled and run_termination.get("kill_failed"):
        extra["kill_failed"] = True
        extra["kill_failed_pids"] = run_termination.get("pids") or []
    with transaction(conn):
        fin = _final_tx(conn, plan, result, extra=extra)
    if fin.get("left_merging"):
        return "skipped"
    return "reconciled" if reconciled else "interrupted"


# ---------------------------------------------------------------------------
# Final TX
# ---------------------------------------------------------------------------

def _final_tx(conn: sqlite3.Connection, plan: Mapping[str, Any], result: Mapping[str, Any],
              extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """T15/T16, merge_json, run closed, lease released (+ merge.failed). Caller owns BEGIN/COMMIT.

    ``extra`` (recovery, m2-r7) is added to merge_json and the event payload. The run is
    only closed while still open: recovery closes it first (``killed``/``lost``).
    """
    now = now_iso_utc()
    task_id = plan["task_id"]
    merged = result["outcome"] == "merged"
    to_state = "merged" if merged else "merge_failed"
    merge_json: Dict[str, Any] = {
        "mrg": plan["merge_id"],
        "run_id": plan["run_id"],
        "pre_head": result.get("pre_head") or plan.get("pre_head"),
        "started_at": plan["started_at"],
        "ended_at": now,
        "tests": result.get("tests") or "none",
        "test_log": result.get("test_log"),
    }
    if result.get("test") is not None:
        merge_json["test"] = result["test"]
    if merged:
        merge_json["merge_commit"] = result["merge_commit"]
        merge_json["noop"] = bool(result.get("noop"))
    else:
        merge_json["reason"] = result["reason"]
        merge_json["details"] = result.get("details") or {}
    if extra:
        merge_json.update(extra)

    if not extra:
        _check_live_merge_close(conn, plan, result)

    task = conn.execute("SELECT status, merge_json FROM tasks WHERE id = ?;", (task_id,)).fetchone()
    if task is None or task["status"] != "merging":
        # The task left `merging` while the pipeline ran (e.g. P5 project cancel). The
        # task status is left alone, but the run is closed and the merge lease released
        # in this same transaction, keeping the pipeline result (and any merge_commit)
        # on the run (review m2-r4b-r5 finding 3). The caller returns rc 1.
        kept = dict(merge_json)
        kept["pipeline"] = {k: v for k, v in result.items() if k != "details"}
        kept["pipeline"]["details"] = result.get("details") or {}
        conn.execute(
            "UPDATE runs SET status = 'exited', ended_at = ?, exit_code = 1, end_reason = 'task_left_merging', "
            "result_json = ? WHERE id = ? AND status IN ('starting', 'running');",
            (now, json.dumps(kept), plan["run_id"]),
        )
        conn.execute("DELETE FROM leases WHERE key = ? AND run_id = ?;",
                     (merge_lease_key(plan["project_id"]), plan["run_id"]))
        return {"merge_json": kept, "event": None, "to_state": None, "left_merging": True,
                "task_status": task["status"] if task is not None else None}
    conn.execute(
        "UPDATE tasks SET status = ?, merge_json = ?, version = version + 1, updated_at = ? WHERE id = ?;",
        (to_state, json.dumps(merge_json), now, task_id),
    )
    test_info = result.get("test") or {}
    termination = test_info.get("termination") or {}
    run_status, end_reason = "exited", ("merged" if merged else result["reason"])
    if test_info.get("timeout"):
        run_status, end_reason = "killed", "timeout"  # §11.4 (review m2-r5b-r6 finding 1)
    elif termination.get("kill_failed") and test_info.get("leftovers"):
        run_status, end_reason = "killed", "leftovers"
    conn.execute(
        "UPDATE runs SET status = ?, ended_at = ?, exit_code = ?, end_reason = ?, result_json = ? "
        "WHERE id = ? AND status IN ('starting', 'running');",
        (run_status, now, 0 if merged else 1, end_reason, json.dumps(merge_json), plan["run_id"]),
    )
    if termination.get("kill_failed"):
        # Deviation from §11.4 (recorded): the merge test step does not keep `merge:<prj>`
        # and retry; it reports the survivors, aborts, and releases the lease. Recovery's
        # MESH_RUN_ID scan does not see a closed run, so the pids are in the event.
        record_event(
            conn=conn, type_="run.kill_failed", actor_id="system", actor_kind="system", via="system",
            project_id=plan["project_id"], task_id=task_id, run_id=plan["run_id"],
            payload={"run_id": plan["run_id"], "task_id": task_id, "end_reason": end_reason,
                     "pids": termination.get("pids") or [], "lease_released": True, "kind": "merge_test"},
        )
    wrapper = (result.get("test") or {}).get("wrapper") or {}
    if wrapper.get("pid"):
        conn.execute("UPDATE runs SET wrapper_pid = ?, pgid = ?, proc_starttime = ? WHERE id = ?;",
                     (wrapper["pid"], wrapper["pid"], wrapper.get("starttime"), plan["run_id"]))
    conn.execute("DELETE FROM leases WHERE key = ? AND run_id = ?;",
                 (merge_lease_key(plan["project_id"]), plan["run_id"]))
    if merged:
        evt = record_event(
            conn=conn, type_="task.merged", actor_id="system", actor_kind="system", project_id=plan["project_id"], task_id=task_id,
            run_id=plan["run_id"], from_state="merging", to_state="merged",
            payload={"merge_id": plan["merge_id"], "merge_commit": merge_json["merge_commit"],
                     "pre_head": merge_json["pre_head"], "noop": merge_json["noop"], "tests": merge_json["tests"],
                     "test_log": merge_json["test_log"], **dict(extra or {})},
        )
    else:
        evt = record_event(
            conn=conn, type_="merge.failed", actor_id="system", actor_kind="system", project_id=plan["project_id"], task_id=task_id,
            run_id=plan["run_id"], from_state="merging", to_state="merge_failed", reason=result["reason"],
            payload={"merge_id": plan["merge_id"], "reason": result["reason"], "pre_head": merge_json["pre_head"],
                     "details": merge_json["details"], **dict(extra or {})},
        )
    return {"merge_json": merge_json, "event": evt, "to_state": to_state, "left_merging": False}


def merge_response(plan: Mapping[str, Any], result: Mapping[str, Any], fin: Mapping[str, Any]) -> Any:
    """The request's outcome: a HandlerResult (rc 0) or the MeshError to raise (rc 14 / 1)."""
    if fin.get("left_merging"):
        return MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                         message=f"Task '{plan['task_id']}' left 'merging' during its merge",
                         details={"task_id": plan["task_id"], "merge_id": plan["merge_id"],
                                  "reason": "task_left_merging", "status": fin.get("task_status"),
                                  "merge_commit": result.get("merge_commit")})
    merge_json = fin["merge_json"]
    if result["outcome"] == "merged":
        return HandlerResult(
            data={"task_id": plan["task_id"], "status": "merged", "merge_id": plan["merge_id"],
                  "merge_commit": merge_json["merge_commit"], "pre_head": merge_json["pre_head"],
                  "noop": merge_json["noop"], "tests": merge_json["tests"], "test_log": merge_json["test_log"],
                  "branch": plan["branch"],
                  "integration_branch": plan["integration_branch"]},
            transitions=[{"task_id": plan["task_id"], "from": plan["from_state"], "to": "merging"},
                         {"task_id": plan["task_id"], "from": "merging", "to": "merged"}],
            events=[plan["event"], fin["event"]],
        )
    reason = result["reason"]
    details = {"task_id": plan["task_id"], "reason": reason, "merge_id": plan["merge_id"],
               "pre_head": merge_json["pre_head"], "status": "merge_failed"}
    details.update(result.get("details") or {})
    if reason == "git_failed":
        return MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                         message=f"Merge of task '{plan['task_id']}' failed: a git command failed", details=details)
    return MeshError(code=REASON_CODES[reason], exit_code=c.EXIT_MERGE_FAILED,
                     message=f"Merge of task '{plan['task_id']}' failed: {reason}", details=details)


def response_envelope(response: Any, request_id: str) -> Dict[str, Any]:
    """The exact envelope the client receives (server.py builds the same for rc 0)."""
    if isinstance(response, MeshError):
        return response.to_envelope(request_id=request_id)
    return {"ok": True, "data": response.data, "warnings": response.warnings, "request_id": request_id,
            "idempotent_replay": False, "dry_run": False, "exit_code": response.exit_code}


def store_idempotency(conn: sqlite3.Connection, idem: Optional[Mapping[str, Any]], response: Any,
                      request_id: str) -> Optional[bool]:
    """§13.4 inside the final TX: rc 0 and rc 14 are stored; rc 1 (git_failed, left merging) never.

    ``INSERT OR IGNORE``: a key already taken by a concurrent request of the same principal
    (it passed the server's lookup too) must not roll back the merge's mutation (review
    m2-r5b-r6 finding 2). Returns False when the key was taken, None when nothing is stored.
    """
    if not idem or not idem.get("key"):
        return None
    envelope = response_envelope(response, request_id)
    exit_code = envelope["exit_code"]
    if exit_code not in (0, c.EXIT_MERGE_FAILED):
        return None
    from mesh.daemon.server import _idempotency_record

    cur = conn.execute(
        """INSERT OR IGNORE INTO idempotency (principal_id, key, cmd, request_hash, response_json, exit_code, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?);""",
        (idem["principal_id"], idem["key"], "task.merge", idem["request_hash"],
         json.dumps(_idempotency_record("task.merge", envelope)), exit_code, now_iso_utc()),
    )
    return cur.rowcount == 1


# ---------------------------------------------------------------------------
# Request entry point
# ---------------------------------------------------------------------------

async def run_task_merge(server: Any, ctx: HandlerContext) -> HandlerResult:
    """Serve `task.merge`. Returns the rc 0 result or raises MeshError (rc 14 / 1 after the pipeline)."""
    conn = ctx.conn
    task_id = ctx.args.get("task_id")

    # 1. Read-only probe (no transaction, git off the loop). TX1 re-runs every check.
    # No git on --dry-run (§13.4) and none before authz (review m2-r4b-r5 finding 4); an
    # authz failure is still raised by TX1, in the r5 check order.
    pre_head: Optional[str] = None
    if isinstance(task_id, str) and task_id and not ctx.dry_run:
        task, project = _load(conn, task_id)
        if task is not None and project is not None and project["repo_path"] and task["kind"] == "code":
            try:
                check_authz(conn, "task.merge", ctx.principal, ctx.actor_kind, ctx.via,
                            project_id=project["id"], task_id=task_id)
                allowed = True
            except MeshError:
                allowed = False
            if allowed:
                integ = integration_worktree_path(get_workspaces_root(conn, ctx.home), project["id"])
                pre_head = await asyncio.to_thread(probe_integration_head, integ)

    # 2. TX1
    conn.execute("BEGIN IMMEDIATE;")
    task, project = check_merge_request(ctx, task_id)
    plan = _tx1(ctx, task, project, pre_head)
    if ctx.dry_run:
        conn.execute("ROLLBACK;")
        return HandlerResult(
            data={"would": {"transitions": [{"task_id": plan["task_id"], "from": plan["from_state"],
                                             "to": "merging", "transition": plan["transition"]}],
                            "events": [plan["event"]], "effects": ["merge_pipeline"]}},
            transitions=[{"task_id": plan["task_id"], "from": plan["from_state"], "to": "merging"}],
            events=[plan["event"]],
        )
    conn.execute("COMMIT;")
    try:
        os.makedirs(plan["run_dir"], mode=0o700, exist_ok=True)
    except OSError:
        pass  # the run dir only holds the r6 test log
    try:
        await server.notify_waiters()
    except Exception:
        pass

    # 3 + 4. Pipeline and final TX as one shielded task: a client disconnect (or a
    # cancelled request coroutine) does not cancel the merge.
    idem = getattr(ctx, "idempotency", None)

    async def _pipeline_and_finish() -> Dict[str, Any]:
        result = await asyncio.to_thread(run_merge_pipeline, plan)
        try:
            _check_live_merge_close(conn, plan, result)
        except (MergeRunHeld, OSError) as exc:
            run_row = conn.execute("SELECT * FROM runs WHERE id = ?;", (plan["run_id"],)).fetchone()
            if run_row is not None:
                _report_recovery_blocked(conn, run_row, exc)
            raise
        conn.execute("BEGIN IMMEDIATE;")
        try:
            fin = _final_tx(conn, plan, result)
            response = merge_response(plan, result, fin)
            # §13.4: the response is stored in the same transaction as the mutation
            # (review m2-r4b-r5 finding 1).
            stored = store_idempotency(conn, idem, response, ctx.request_id)
            conn.execute("COMMIT;")
            if stored is False:
                # The real outcome is returned; the key keeps the other request's response.
                try:
                    server.log_error(f"task merge {plan['task_id']}: idempotency key already used by another "
                                     f"request of the same principal; response not stored")
                except Exception:
                    pass
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        return {"result": result, "final": fin, "response": response}

    job = asyncio.ensure_future(_pipeline_and_finish())
    try:
        done = await asyncio.shield(job)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            server.log_error(f"task merge {plan['task_id']} final transaction failed: {type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                        message=f"Merge of task '{plan['task_id']}' could not be recorded",
                        details={"task_id": plan["task_id"], "merge_id": plan["merge_id"], "reason": "internal"})
    try:
        await server.notify_waiters()
        server.wake_scheduler()
    except Exception:
        pass

    response = done["response"]
    if isinstance(response, MeshError):
        if done["final"].get("left_merging"):
            try:
                server.log_error(f"task merge {plan['task_id']}: task left 'merging' during the merge; "
                                 f"run {plan['run_id']} closed, lease released")
            except Exception:
                pass
        raise response
    return response
