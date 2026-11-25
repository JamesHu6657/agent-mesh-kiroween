"""Workspace path resolution and directory creation per plan §10.1, §11.1."""

import errno
import json
import os
import re
import sys
import threading
import sqlite3
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple


from mesh.core.token_roots import PROTECTED_PATH_META_PREFIX
from mesh.platform import paths as _plat_paths


WS_RES_PREFIX = "ws_res:"
WS_PRUNE_ALL_KEY = "ws_res:prune_all"


def ws_delete_reservation_key(task_id: str) -> str:
    return f"ws_res:task:{task_id}"


def ws_prune_project_reservation_key(project_id: str) -> str:
    return f"ws_res:prune_proj:{project_id}"


def load_ws_reservations(conn: sqlite3.Connection) -> List[Tuple[str, Dict[str, Any]]]:
    """Load all active workspace delete/prune reservations from ``meta``."""
    try:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key ASC;",
            (WS_RES_PREFIX + "%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: List[Tuple[str, Dict[str, Any]]] = []
    for row in rows:
        key = row["key"]
        raw = row["value"]
        try:
            doc = json.loads(raw) if raw else {}
        except Exception:
            doc = {}
        if isinstance(doc, dict):
            out.append((key, doc))
    return out


def _reservation_touches_project(key: str, doc: Mapping[str, Any], project_id: str) -> bool:
    if doc.get("project_id") == project_id:
        return True
    projects = doc.get("projects")
    if isinstance(projects, list) and project_id in projects:
        return True
    if doc.get("kind") == "prune" and (key == WS_PRUNE_ALL_KEY or doc.get("project_id") is None):
        return True
    return False


def has_task_ws_reservation(
    conn: sqlite3.Connection,
    task_id: Optional[str],
    project_id: Optional[str] = None,
) -> Optional[str]:
    """Return matching reservation key if a delete/prune reservation covers this task."""
    for key, doc in load_ws_reservations(conn):
        kind = doc.get("kind")
        if kind == "delete":
            if task_id and (key == ws_delete_reservation_key(task_id) or doc.get("task_id") == task_id):
                return key
        elif kind == "prune":
            if key == WS_PRUNE_ALL_KEY or doc.get("project_id") is None:
                return key
            if project_id and _reservation_touches_project(key, doc, project_id):
                return key
            task_ids = doc.get("task_ids")
            if task_id and isinstance(task_ids, list) and task_id in task_ids:
                return key
        elif task_id and key == ws_delete_reservation_key(task_id):
            return key
    return None


def has_project_any_ws_reservation(conn: sqlite3.Connection, project_id: str) -> Optional[str]:
    """Return matching reservation key if any workspace delete/prune reservation touches ``project_id``."""
    for key, doc in load_ws_reservations(conn):
        if _reservation_touches_project(key, doc, project_id):
            return key
    return None


def list_project_ws_reservation_keys(conn: sqlite3.Connection, project_id: str) -> List[str]:
    """Return all workspace reservation keys affecting ``project_id``."""
    return [
        key
        for key, doc in load_ws_reservations(conn)
        if _reservation_touches_project(key, doc, project_id)
    ]


def pinned_approval_ids(conn: sqlite3.Connection) -> Set[str]:
    """Return approval IDs pinned by active workspace reservations."""
    ids: Set[str] = set()
    for _key, doc in load_ws_reservations(conn):
        apr_id = doc.get("approval_id")
        if isinstance(apr_id, str) and apr_id:
            ids.add(apr_id)
    return ids


def is_approval_pinned_by_ws_reservation(conn: sqlite3.Connection, approval_id: Optional[str]) -> bool:
    """True when ``approval_id`` is currently pinned by an active workspace reservation."""
    if not approval_id:
        return False
    return approval_id in pinned_approval_ids(conn)


def find_ws_reserved_idempotency(
    conn: sqlite3.Connection,
    principal_id: str,
    key: str,
) -> Optional[Dict[str, Any]]:
    """Return active reservation idempotency metadata matching ``(principal_id, key)``, if any."""
    if not key:
        return None
    for res_key, doc in load_ws_reservations(conn):
        idem = doc.get("idempotency")
        if (
            isinstance(idem, dict)
            and idem.get("principal_id") == principal_id
            and idem.get("key") == key
        ):
            out = dict(idem)
            out["reservation_key"] = res_key
            return out
    return None


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


def allowed_workspace_roots(conn: sqlite3.Connection, home: str) -> List[str]:
    """Return current and historically recorded ``workspaces.root`` / protected workspace paths."""
    roots: List[str] = []
    seen: set[str] = set()

    def _add(raw: Optional[str]) -> None:
        if not isinstance(raw, str) or not raw.strip():
            return
        norm = os.path.abspath(raw.strip())
        if norm not in seen:
            seen.add(norm)
            roots.append(norm)

    _add(get_workspaces_root(conn, home))
    _add(os.path.join(home, "workspaces"))
    try:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta';"
        ).fetchone()
        if has_meta:
            for (val,) in conn.execute(
                "SELECT value FROM meta WHERE key LIKE ?;",
                (PROTECTED_PATH_META_PREFIX + "%",),
            ).fetchall():
                _add(val)
    except Exception:
        pass
    try:
        for (pj,) in conn.execute(
            "SELECT payload_json FROM events WHERE type = 'config.updated';"
        ).fetchall():
            try:
                pl = json.loads(pj or "{}")
            except Exception:
                continue
            if isinstance(pl, dict) and pl.get("key") == "workspaces.root":
                v = pl.get("value")
                if isinstance(v, str) and "[REDACTED]" not in v:
                    _add(v)
    except Exception:
        pass
    return roots


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
    if not ws_dir.endswith(os.sep):
        ws_dir += os.sep
    return ws_dir


def compute_lead_workspace_path(workspaces_root: str, project_id: str, agent_id: str) -> str:
    """Lead dir per plan §10.1: <workspaces.root>/<project_id>/lead/<agent_id>/."""
    lead_dir = os.path.join(workspaces_root, project_id, "lead", agent_id)
    if not lead_dir.endswith(os.sep):
        lead_dir += os.sep
    return lead_dir


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


def _ensure_private_dir(path: str, label: str) -> None:
    from mesh.core.paths import resolve_trusted_path
    from mesh.platform import perms

    if resolve_trusted_path(path) is None:
        raise OSError(errno.ELOOP, f"{label} path contains a symlink or is invalid", path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if resolve_trusted_path(path) is None:
        raise OSError(errno.ELOOP, f"{label} path contains a symlink or is invalid", path)
    try:
        if sys.platform == "win32":
            perms.set_private_dir_acl(path, 0o700)
        elif hasattr(os, "fchmod"):
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
            fd = os.open(path, flags)
            try:
                perms.fchmod_private(fd, 0o700)
            finally:
                os.close(fd)
        else:
            perms.set_private_dir_acl(path, 0o700)
    except Exception:
        pass


def ensure_workspace_dir(path: str) -> None:
    """Ensure workspace directory exists with 0700 permissions."""
    _ensure_private_dir(path, "Workspace")


def require_task_worktree(path: str) -> None:
    """Full review r2 P1-C: a code task's workspace is a git worktree prepared by
    `run_code_workspace_plan`; it is never created here. A slot deleted after it was
    prepared (`workspace delete` racing a start) stays absent, so the next attempt
    re-attaches the kept branch instead of finding a plain empty directory there
    (`not_a_worktree` for good)."""
    if not os.path.lexists(os.path.join(_plat_paths.strip_trailing_seps(path), ".git")):
        raise WorkspaceError(f"Task worktree {path} is missing",
                             {"workspace": path, "step": "start", "reason": "workspace_missing"})


def ensure_run_dir(path: str) -> None:
    """Ensure run directory exists with 0700 permissions."""
    _ensure_private_dir(path, "Run")


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


def _common_dir(plan: Mapping[str, Any], path: str) -> Optional[str]:
    """The canonical git common directory of the repo or worktree at ``path``."""
    cp = _git(plan, "rev-parse --git-common-dir", ["rev-parse", "--git-common-dir"], path)
    out = (cp.stdout or "").strip()
    if cp.returncode != 0 or not out:
        return None
    return os.path.realpath(os.path.join(path, out))


def _reuse(plan: Mapping[str, Any], path: str, branch: str, alt_branch: Optional[str] = None) -> str:
    """Reuse an existing worktree only if it is a registered worktree of the repo on `branch`
    (or on `alt_branch` when given). Returns the branch it is on."""
    from mesh.core.paths import resolve_trusted_path
    from mesh.core.repos import is_registered_worktree

    ws_real = resolve_trusted_path(path)
    if ws_real is None:
        raise _fail("workspace path is a symlink or untrusted", plan, step="reuse",
                    reason="foreign_repository", branch=branch)
    if not os.path.lexists(os.path.join(ws_real, ".git")):
        raise _fail("workspace exists but is not a git worktree", plan, step="reuse",
                    reason="not_a_worktree", branch=branch)
    repo = plan.get("repo")
    if repo:
        # Full review r6 P1-3: validate worktree registration from files BEFORE running git in ws_real.
        if not is_registered_worktree(ws_real, repo):
            raise _fail("workspace exists but belongs to a different repository", plan, step="reuse",
                        reason="foreign_repository", branch=branch)
    top = _git(plan, "rev-parse --show-toplevel", ["rev-parse", "--show-toplevel"], ws_real)
    if not _plat_paths.same_path((top.stdout or "").strip(), ws_real):
        raise _fail("workspace exists but is not a git worktree", plan, step="reuse", reason="not_a_worktree",
                    branch=branch)
    current = _existing_worktree_branch(plan, ws_real)
    if current == branch or (alt_branch is not None and current == alt_branch):
        return current
    raise _fail(f"workspace is on '{current}', expected '{branch}'", plan, step="reuse",
                reason="wrong_branch", branch=branch, current_branch=current)


def _fresh_branch(plan: Mapping[str, Any], path: str) -> Optional[str]:
    """`mesh/<prj>/<tsk>-a<n>` for an `<tsk>.a<n>` workspace dir, else None."""
    m = _ATTEMPT_SUFFIX.search(_plat_paths.strip_trailing_seps(path))
    if not m:
        return None
    return f"{task_branch_name(plan['project_id'], plan['task_id'])}-a{m.group(1)}"


def _add_worktree(plan: Mapping[str, Any], repo: str, path: str, new_branch: str, start: str,
                  **details: Any) -> bool:
    """`worktree add -b`. If it fails because a concurrent prepare of the same task just
    created this worktree on `new_branch` (review m2-r4 finding 6), return False (reuse);
    any other failure is the original WorkspaceError."""
    clean_path = _plat_paths.strip_trailing_seps(path)
    try:
        _git(plan, "worktree add", ["worktree", "add", "-b", new_branch, clean_path, start], repo,
             branch=new_branch, **details)
        return True
    except WorkspaceError:
        if os.path.isdir(clean_path):
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
    clean_path = _plat_paths.strip_trailing_seps(path)
    branch = plan.get("branch")
    task_id = plan["task_id"]
    project_id = plan["project_id"]

    if not branch:
        # First start: `worktree add -b mesh/<prj>/<tsk> <ws>/tasks/<tsk> <integration tip>`.
        branch = task_branch_name(project_id, task_id)
        integration = plan["integration"]
        if os.path.lexists(clean_path) or os.path.isdir(clean_path):
            # A previous prepare created it, but its T5 transaction did not commit
            # (lost race or daemon stop). Reuse it; the base is where it forked.
            _reuse(plan, path, branch)
            return {"workspace_path": path, "branch": branch, "base_commit": _fork_point(plan, repo, branch, integration),
                    "action": "reused"}
        cp = _git(plan, "rev-parse integration", ["rev-parse", "--verify", f"refs/heads/{integration}"], repo,
                  branch=integration)
        base = (cp.stdout or "").strip()
        if _branch_exists(plan, repo, branch):
            base = _fork_point(plan, repo, branch, integration)
            _makedirs_parent(plan, path)
            _git(plan, "worktree prune", ["worktree", "prune"], repo, branch=branch)
            _git(plan, "worktree reattach", ["worktree", "add", clean_path, branch], repo, branch=branch)
            _reuse(plan, path, branch)
            return {"workspace_path": path, "branch": branch, "base_commit": base,
                    "action": "reused", "reattached": True}
        _makedirs_parent(plan, path)
        if not _add_worktree(plan, repo, path, branch, base):
            return {"workspace_path": path, "branch": branch, "base_commit": _fork_point(plan, repo, branch, integration),
                    "action": "reused"}
        return {"workspace_path": path, "branch": branch, "base_commit": base, "action": "created"}

    fresh_branch = _fresh_branch(plan, path)
    if fresh_branch == branch:
        fresh_branch = None  # the -a<n> branch is already the task's branch (T5 committed)
    if os.path.lexists(clean_path) or os.path.isdir(clean_path):
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
        # Full review r1 P1-4: `workspace delete` removes the directory but keeps the
        # branch (Q7). A later retry/rework re-attaches that branch (its work, as is)
        # instead of failing on workspace_missing forever.
        if not _branch_exists(plan, repo, branch) or os.path.lexists(clean_path):
            raise _fail("task workspace is missing", plan, step="reuse", reason="workspace_missing", branch=branch)
        _makedirs_parent(plan, path)
        _git(plan, "worktree prune", ["worktree", "prune"], repo, branch=branch)
        _git(plan, "worktree reattach", ["worktree", "add", clean_path, branch], repo, branch=branch)
        _reuse(plan, path, branch)
        return {"workspace_path": path, "branch": branch, "base_commit": plan.get("base_commit"),
                "action": "reused", "reattached": True}
    _makedirs_parent(plan, path)
    _add_worktree(plan, repo, path, fresh_branch, f"refs/heads/{branch}", from_branch=branch)
    return {"workspace_path": path, "branch": fresh_branch, "base_commit": plan.get("base_commit"),
            "action": "fresh", "previous_branch": branch}


def _branch_exists(plan: Mapping[str, Any], repo: str, branch: str) -> bool:
    """`refs/heads/<branch>` resolves to a commit (a git error is a WorkspaceError)."""
    from mesh.core.gitops import run_git

    try:
        cp = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], cwd=repo)
    except subprocess.TimeoutExpired as e:
        raise _fail(f"git rev-parse task branch timed out after {e.timeout:g} s", plan,
                    step="rev-parse task branch", timeout_s=e.timeout, branch=branch)
    except OSError as e:
        raise _fail(f"git rev-parse task branch could not run: {e}", plan,
                    step="rev-parse task branch", error=str(e), branch=branch)
    return cp.returncode == 0


def _fork_point(plan: Mapping[str, Any], repo: str, branch: str, integration: str) -> str:
    """Where `branch` forked from `integration`, by full refs: a tag with either short name
    would otherwise be what git resolves."""
    cp = _git(plan, "merge-base", ["merge-base", f"refs/heads/{branch}", f"refs/heads/{integration}"], repo,
              branch=branch)
    return (cp.stdout or "").strip()


def _makedirs_parent(plan: Mapping[str, Any], path: str) -> None:
    from mesh.core.paths import resolve_trusted_path

    clean_path = _plat_paths.strip_trailing_seps(path)
    if resolve_trusted_path(clean_path) is None:
        raise _fail(f"workspace path '{path}' contains a symlink", plan, step="mkdir", reason="symlink")
    parent = os.path.dirname(clean_path)
    try:
        ensure_workspace_dir(parent)
    except OSError as e:
        raise _fail(f"cannot create {parent}: {e}", plan, step="mkdir", error=str(e))
    if resolve_trusted_path(clean_path) is None:
        raise _fail(f"workspace path '{path}' contains a symlink", plan, step="mkdir", reason="symlink")


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
