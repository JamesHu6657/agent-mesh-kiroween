"""Workspace path resolution and directory creation per plan §10.1, §11.1."""

import json
import os
import re
import threading
import sqlite3
import subprocess
from typing import Any, Dict, List, Mapping, Optional


def get_workspaces_root(conn: sqlite3.Connection, home: str) -> str:
    """Retrieve workspaces.root setting from database, falling back to $MESH_HOME/workspaces."""
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'workspaces.root';")
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            val = json.loads(row["value_json"])
            if isinstance(val, str) and val.strip():
                return os.path.abspath(val.strip())
        except Exception:
            pass
    return os.path.abspath(os.path.join(home, "workspaces"))


def compute_task_workspace_path(
    workspaces_root: str,
    project_id: str,
    task_id: str,
    attempt: int = 1,
    reuse_workspace: bool = False,
    existing_path: Optional[str] = None,
) -> str:
    """Compute workspace path for a task attempt.

    Attempt 1: <workspaces.root>/<project_id>/tasks/<task_id>/
    Attempt > 1: <workspaces.root>/<project_id>/tasks/<task_id>.a<attempt>/ (unless reuse_workspace=True)
    """
    if reuse_workspace and existing_path:
        return existing_path

    if attempt <= 1:
        dir_name = task_id
    else:
        dir_name = f"{task_id}.a{attempt}"

    ws_dir = os.path.join(workspaces_root, project_id, "tasks", dir_name)
    if not ws_dir.endswith("/"):
        ws_dir += "/"
    return ws_dir


def compute_lead_workspace_path(workspaces_root: str, project_id: str, agent_id: str) -> str:
    """Lead dir per plan §10.1: <workspaces.root>/<project_id>/lead/<agent_id>/."""
    return os.path.join(workspaces_root, project_id, "lead", agent_id) + "/"


def ensure_lead_dir(conn: sqlite3.Connection, home: str, project_id: str, agent_id: str) -> Optional[str]:
    """Create the lead dir (0700) at P1 and at set-lead (plan §10.1, m1c Q6).

    Idempotent; a failure is returned as None so the caller's state change is not
    blocked by a filesystem problem (the dir is re-created on the next set-lead).
    """
    path = compute_lead_workspace_path(get_workspaces_root(conn, home), project_id, agent_id)
    try:
        ensure_workspace_dir(path)
    except OSError:
        return None
    return path


def ensure_workspace_dir(path: str) -> None:
    """Ensure workspace directory exists with 0700 permissions."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except Exception:
        pass


def ensure_run_dir(path: str) -> None:
    """Ensure run directory exists with 0700 permissions."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Code-task worktrees (plan §10.3, §6.2 T5/T6, fresh-workspace rule).
#
# Two halves so the git work never runs on the event loop or inside the T5
# transaction (review m2-r3 finding 5):
#   - plan_code_workspace(conn, home, task): DB reads only, on the loop thread;
#   - run_code_workspace_plan(plan): git only (no DB), run in a worker thread.
# prepare_code_workspace(conn, task, project) runs both in the calling thread.
# Every git call goes through gitops.run_git (A6), which registers the child
# (children.run_registered: thread-safe, inside the spawn window).
# Nothing here removes a branch or a worktree (§10.7).
# ---------------------------------------------------------------------------

_STDERR_TAIL = 2000
_ATTEMPT_SUFFIX = re.compile(r"\.a([0-9]+)$")


class WorkspaceError(Exception):
    """A code-task worktree could not be prepared (T6 `workspace_error`)."""

    def __init__(self, message: str, details: Dict[str, Any]):
        super().__init__(message)
        self.details = details


def task_branch_name(project_id: str, task_id: str) -> str:
    """`mesh/<prj>/<tsk>` (§10.3)."""
    return f"mesh/{project_id}/{task_id}"


def plan_code_workspace(
    conn: sqlite3.Connection, home: str, task: Mapping[str, Any], project: Optional[Mapping[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """What T5 needs for a code task, from the DB only. None for a general task.

    A code task on a project without a repo (only possible for rows created before
    the Q13 create check) is planned with `repo: None`; running the plan then fails
    with `workspace_error` instead of silently using a plain directory.
    """
    if task["kind"] != "code":
        return None
    project_id = task["project_id"]
    if project is None or "repo_path" not in _keys(project):
        project = conn.execute("SELECT id, repo_path FROM projects WHERE id = ?;", (project_id,)).fetchone()
    repo = project["repo_path"] if project else None
    ws_path = task["workspace_path"]
    if not ws_path:
        ws_path = compute_task_workspace_path(get_workspaces_root(conn, home), project_id, task["id"], attempt=1)
    return {
        "task_id": task["id"],
        "project_id": project_id,
        "repo": repo,
        "integration": f"mesh/{project_id}/integration",
        "workspace_path": ws_path,
        "branch": task["branch"],
        "base_commit": task["base_commit"],
        "fence_seq": task["fence_seq"],
    }


def _keys(row: Any) -> List[str]:
    try:
        return list(row.keys())
    except AttributeError:
        return list(row)


def _fail(message: str, plan: Mapping[str, Any], **details: Any) -> WorkspaceError:
    d: Dict[str, Any] = {"workspace": plan.get("workspace_path"), "repo": plan.get("repo")}
    d.update(details)
    return WorkspaceError(message, d)


def _git(plan: Mapping[str, Any], step: str, args: List[str], cwd: str, **details: Any) -> Any:
    """run_git; a timeout, OSError or non-zero rc is a WorkspaceError naming the step."""
    from mesh.core.gitops import run_git

    try:
        cp = run_git(args, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        raise _fail(f"git {step} timed out after {e.timeout:g} s", plan, step=step, timeout_s=e.timeout, **details)
    except OSError as e:
        raise _fail(f"git {step} could not run: {e}", plan, step=step, error=str(e), **details)
    if cp.returncode != 0:
        raise _fail(f"git {step} failed (rc {cp.returncode})", plan, step=step, git_rc=cp.returncode,
                    stderr_tail=(cp.stderr or "")[-_STDERR_TAIL:], **details)
    return cp


def _existing_worktree_branch(plan: Mapping[str, Any], path: str) -> Optional[str]:
    """The branch checked out in an existing worktree dir (`HEAD` when detached), or None if
    the dir is absent.

    Read as the full ref: `--abbrev-ref` gives `heads/<branch>` when a tag has the same short
    name. (`symbolic-ref` is not on the gitops allow-list; `--symbolic-full-name` is rev-parse.)
    """
    if not os.path.isdir(path):
        return None
    cp = _git(plan, "rev-parse --symbolic-full-name HEAD", ["rev-parse", "--symbolic-full-name", "HEAD"], path)
    full = (cp.stdout or "").strip()
    return full[len("refs/heads/"):] if full.startswith("refs/heads/") else full


def _reuse(plan: Mapping[str, Any], path: str, branch: str, alt_branch: Optional[str] = None) -> str:
    """Reuse an existing worktree only if it is a worktree of the repo on `branch`
    (or on `alt_branch` when given). Returns the branch it is on."""
    top = _git(plan, "rev-parse --show-toplevel", ["rev-parse", "--show-toplevel"], path)
    if os.path.realpath((top.stdout or "").strip()) != os.path.realpath(path):
        raise _fail("workspace exists but is not a git worktree", plan, step="reuse", reason="not_a_worktree",
                    branch=branch)
    current = _existing_worktree_branch(plan, path)
    if current == branch or (alt_branch is not None and current == alt_branch):
        return current
    raise _fail(f"workspace is on '{current}', expected '{branch}'", plan, step="reuse",
                reason="wrong_branch", branch=branch, current_branch=current)


def _fresh_branch(plan: Mapping[str, Any], path: str) -> Optional[str]:
    """`mesh/<prj>/<tsk>-a<n>` for an `<tsk>.a<n>` workspace dir, else None."""
    m = _ATTEMPT_SUFFIX.search(path.rstrip("/"))
    if not m:
        return None
    return f"{task_branch_name(plan['project_id'], plan['task_id'])}-a{m.group(1)}"


def _add_worktree(plan: Mapping[str, Any], repo: str, path: str, new_branch: str, start: str,
                  **details: Any) -> bool:
    """`worktree add -b`. If it fails because a concurrent prepare of the same task just
    created this worktree on `new_branch` (review m2-r4 finding 6), return False (reuse);
    any other failure is the original WorkspaceError."""
    try:
        _git(plan, "worktree add", ["worktree", "add", "-b", new_branch, path.rstrip("/"), start], repo,
             branch=new_branch, **details)
        return True
    except WorkspaceError:
        if os.path.isdir(path):
            try:
                _reuse(plan, path, new_branch)
                return False
            except WorkspaceError:
                pass
        raise


_TASK_LOCKS: Dict[str, threading.Lock] = {}
_TASK_LOCKS_GUARD = threading.Lock()


def _task_lock(task_id: str) -> threading.Lock:
    with _TASK_LOCKS_GUARD:
        lock = _TASK_LOCKS.get(task_id)
        if lock is None:
            lock = _TASK_LOCKS[task_id] = threading.Lock()
        return lock


def run_code_workspace_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Create or reuse the task worktree (§10.3). Git only; safe in a worker thread.

    Returns `{"workspace_path", "branch", "base_commit", "action"}`, with `action` one
    of `created`, `reused`, `fresh`. Raises WorkspaceError on any failure.

    Serialized per task (review m2-r4 finding 6): the launch worker and the inbox
    prepare share this lock, so a second prepare of the same task sees the first one's
    worktree and reuses it instead of racing `worktree add -b`.
    """
    with _task_lock(plan["task_id"]):
        return _run_code_workspace_plan(plan)


def _run_code_workspace_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    repo = plan.get("repo")
    if not repo:
        raise _fail("code task on a project without a repo", plan, step="plan", reason="no_repo")
    if not os.path.isdir(repo):
        raise _fail("project repo is missing", plan, step="plan", reason="repo_missing")
    path = plan["workspace_path"]
    branch = plan.get("branch")
    task_id = plan["task_id"]
    project_id = plan["project_id"]

    if not branch:
        # First start: `worktree add -b mesh/<prj>/<tsk> <ws>/tasks/<tsk> <integration tip>`.
        branch = task_branch_name(project_id, task_id)
        integration = plan["integration"]
        if os.path.isdir(path):
            # A previous prepare created it, but its T5 transaction did not commit
            # (lost race or daemon stop). Reuse it; the base is where it forked.
            _reuse(plan, path, branch)
            return {"workspace_path": path, "branch": branch, "base_commit": _fork_point(plan, repo, branch, integration),
                    "action": "reused"}
        cp = _git(plan, "rev-parse integration", ["rev-parse", "--verify", f"refs/heads/{integration}"], repo,
                  branch=integration)
        base = (cp.stdout or "").strip()
        _makedirs_parent(plan, path)
        if not _add_worktree(plan, repo, path, branch, base):
            return {"workspace_path": path, "branch": branch, "base_commit": _fork_point(plan, repo, branch, integration),
                    "action": "reused"}
        return {"workspace_path": path, "branch": branch, "base_commit": base, "action": "created"}

    fresh_branch = _fresh_branch(plan, path)
    if fresh_branch == branch:
        fresh_branch = None  # the -a<n> branch is already the task's branch (T5 committed)
    if os.path.isdir(path):
        # Rework, retry/reassign with --reuse-workspace or after a non-lease end: same dir and branch.
        # A fresh `<tsk>.a<n>` dir already on `<tsk>-a<n>` was forked by an earlier prepare whose
        # T5 did not commit (refused start, lost race): it is still the fresh workspace
        # (review m2-r4 finding 1).
        current = _reuse(plan, path, branch, alt_branch=fresh_branch)
        if fresh_branch is not None and current == fresh_branch:
            return {"workspace_path": path, "branch": fresh_branch, "base_commit": plan.get("base_commit"),
                    "action": "fresh", "previous_branch": branch}
        return {"workspace_path": path, "branch": branch, "base_commit": plan.get("base_commit"), "action": "reused"}

    # Fresh workspace after lease_expired (§6.2): workspace_path was moved to
    # `<tsk>.a<n>`; fork `mesh/<prj>/<tsk>-a<n>` from the old branch tip.
    if fresh_branch is None:
        raise _fail("task workspace is missing", plan, step="reuse", reason="workspace_missing", branch=branch)
    _makedirs_parent(plan, path)
    _add_worktree(plan, repo, path, fresh_branch, f"refs/heads/{branch}", from_branch=branch)
    return {"workspace_path": path, "branch": fresh_branch, "base_commit": plan.get("base_commit"),
            "action": "fresh", "previous_branch": branch}


def _fork_point(plan: Mapping[str, Any], repo: str, branch: str, integration: str) -> str:
    """Where `branch` forked from `integration`, by full refs: a tag with either short name
    would otherwise be what git resolves."""
    cp = _git(plan, "merge-base", ["merge-base", f"refs/heads/{branch}", f"refs/heads/{integration}"], repo,
              branch=branch)
    return (cp.stdout or "").strip()


def _makedirs_parent(plan: Mapping[str, Any], path: str) -> None:
    parent = os.path.dirname(path.rstrip("/"))
    try:
        ensure_workspace_dir(parent)
    except OSError as e:
        raise _fail(f"cannot create {parent}: {e}", plan, step="mkdir", error=str(e))


def prepare_code_workspace(
    conn: sqlite3.Connection, task: Mapping[str, Any], project: Mapping[str, Any], home: str = ""
) -> Optional[Dict[str, Any]]:
    """Plan + run in the calling thread (tests and callers already off the loop).

    None for a general task. Raises WorkspaceError for a code task that cannot get
    its worktree. The daemon paths split this: plan on the loop, run in a thread.
    """
    plan = plan_code_workspace(conn, home, task, project)
    if plan is None:
        return None
    return run_code_workspace_plan(plan)
