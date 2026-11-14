"""Repo projects (plan §10.1, §10.2): validation, integration branch/worktree, lead `repo/`.

Every git call goes through `mesh.core.gitops.run_git` (A6) with an explicit `cwd`.
Nothing here deletes a branch or a worktree (§10.7: nothing is removed automatically).
"""

import json
import os
import subprocess
from typing import Any, Dict, List, Optional, Union

import mesh.constants as c
from mesh.core.gitops import run_git
from mesh.core.workspaces import compute_lead_workspace_path, ensure_workspace_dir
from mesh.errors import MeshError

TESTS_NONE = "none"
LEAD_REPO_DIRNAME = "repo"
_STDERR_TAIL = 2000
#: Caps on `deliverable.git` (review m2-r4 finding 3): an untracked node_modules/ must
#: not copy thousands of paths into deliverable_json, events and the lead's outbox.
GIT_DIRTY_FILES_CAP = 200
GIT_DIFF_STAT_CAP = 16 * 1024


def integration_branch_name(project_id: str) -> str:
    """`mesh/<prj>/integration` (§10.2 step 3)."""
    return f"mesh/{project_id}/integration"


def integration_worktree_path(ws_root: str, project_id: str) -> str:
    """`<workspaces.root>/<prj>/integration` (§10.1)."""
    return os.path.join(ws_root, project_id, "integration")


def lead_repo_worktree_path(ws_root: str, project_id: str, agent_id: str) -> str:
    """`<workspaces.root>/<prj>/lead/<agent_id>/repo` (§10.1, §8)."""
    return os.path.join(compute_lead_workspace_path(ws_root, project_id, agent_id), LEAD_REPO_DIRNAME)


def _usage(param: str, message: str, **details: Any) -> MeshError:
    d: Dict[str, Any] = {"param": param}
    d.update(details)
    return MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message=message, details=d)


def _git_failed(step: str, cp: Any, **details: Any) -> MeshError:
    d: Dict[str, Any] = {"reason": "git_failed", "step": step, "git_rc": cp.returncode,
                         "stderr_tail": (cp.stderr or "")[-_STDERR_TAIL:]}
    d.update(details)
    return MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                     message=f"git {step} failed (rc {cp.returncode})", details=d)


#: Request-wide bound on the git work of one project create (PR #39 review, B5): the calls
#: run inside the request transaction, so their total, not each call, is capped.
SETUP_INTEGRATION_BUDGET_S = 30.0


_budget = __import__("threading").local()


class git_budget:
    """Request-wide git deadline (PR #39 review round 2, B5): every `_git` call made while
    it is active gets at most the time left, and none starts once it is spent. Nested
    budgets keep the earlier deadline."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def __enter__(self) -> "git_budget":
        import time as _time
        self._prev = getattr(_budget, "deadline", None)
        mine = _time.monotonic() + self.seconds
        _budget.deadline = mine if self._prev is None else min(self._prev, mine)
        return self

    def __exit__(self, *exc: Any) -> None:
        _budget.deadline = self._prev


def _git(step: str, args: List[str], cwd: str, _timeout_s: float = 30.0, **details: Any) -> Any:
    """run_git, with a timeout (30 s in gitops) mapped to rc 1 `reason:"git_timeout"`.

    `details` (e.g. `branch`, `worktree`) go into the error, so a timeout after the
    branch step still names the branch (Q3; review m2-r3 finding 2).
    """
    try:
        deadline = getattr(_budget, "deadline", None)
        if deadline is not None:
            import time as _time
            _timeout_s = min(_timeout_s, deadline - _time.monotonic())
        if _timeout_s <= 0:
            raise subprocess.TimeoutExpired(args, 0)
        return run_git(args, cwd=cwd, timeout_s=_timeout_s)
    except subprocess.TimeoutExpired as e:
        d: Dict[str, Any] = {"reason": "git_timeout", "step": step, "timeout_s": e.timeout}
        d.update(details)
        raise MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                        message=f"git {step} timed out after {e.timeout:g} s", details=d)


def _check_ref_name(value: Any, param: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _usage(param, f"'{param}' is required for a repo project")
    if value.startswith("-") or any(ch.isspace() for ch in value) or "\x00" in value:
        raise _usage(param, f"Invalid {param} '{value}'", value=value)
    return value


def validate_repo(path: Any, base_branch: Any) -> Dict[str, str]:
    """§10.2 step 1. Returns `{"repo": <realpath>, "base_branch": B, "base_commit": <sha>}`.

    - PATH must be an existing directory whose `rev-parse --show-toplevel` equals its
      realpath (a subdirectory or a non-repo is rc 2 `details.param:"repo"`).
    - `refs/heads/B` must exist and `rev-parse --verify refs/heads/B^{commit}` must
      succeed (else rc 2 `details.param:"base_branch"`; `reason:"not_a_branch"` for
      a tag, sha or HEAD). A same-name tag never selects the base commit.
    - A git timeout is rc 1 `reason:"git_timeout"` (see `_git`).
    """
    if not isinstance(path, str) or not path.strip():
        raise _usage("repo", "'repo' must be a path")
    if not os.path.isabs(path):
        raise _usage("repo", f"Repo path must be absolute: '{path}'", repo=path)
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise _usage("repo", f"Repo path is not a directory: '{path}'", repo=path, reason="not_a_directory")
    cp = _git("rev-parse --show-toplevel", ["rev-parse", "--show-toplevel"], real, repo=path)
    top = (cp.stdout or "").strip()
    if cp.returncode != 0 or not top:
        raise _usage("repo", f"'{path}' is not a git repository", repo=path, reason="not_a_repo")
    if os.path.realpath(top) != real:
        raise _usage("repo", f"'{path}' is not the top level of its git repository ('{top}')",
                     repo=path, reason="not_toplevel", toplevel=os.path.realpath(top))
    branch = _check_ref_name(base_branch, "base_branch")
    # Only a local branch (review m2-r3 finding 3): `project deliver` advances
    # refs/heads/<base> (§10.6), so a tag, a sha or HEAD could never be delivered.
    cp = _git("show-ref --verify", ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], real,
              repo=path, base_branch=branch)
    if cp.returncode != 0:
        # The bare name is only used to word the error: it never selects the base commit.
        cp = _git("rev-parse --verify", ["rev-parse", "--verify", "--quiet", branch], real,
                  repo=path, base_branch=branch)
        if cp.returncode == 0 and (cp.stdout or "").strip():
            raise _usage("base_branch", f"Base branch '{branch}' is not a local branch in '{path}'",
                         base_branch=branch, repo=path, reason="not_a_branch")
        raise _usage("base_branch", f"Base branch '{branch}' does not resolve to a commit in '{path}'",
                     base_branch=branch, repo=path)
    # base_commit by the full ref only (review m2-r3b finding 2, m3-r9 review): a bare `B`
    # resolves a tag named B before refs/heads/B, and a tag at a tree or blob would fail.
    cp = _git("rev-parse --verify refs/heads", ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
              real, repo=path, base_branch=branch)
    sha = (cp.stdout or "").strip()
    if cp.returncode != 0 or not sha:
        raise _usage("base_branch", f"Base branch '{branch}' does not resolve to a commit in '{path}'",
                     base_branch=branch, repo=path)
    return {"repo": real, "base_branch": branch, "base_commit": sha}


def parse_test_command(value: Any) -> Union[str, List[str]]:
    """§10.2 step 2: a JSON argv array of non-empty strings, or the literal `none`.

    Returns `"none"` or the argv list. Anything else is rc 2 `details.param:"test_command"`.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise _usage("test_command", "--test-command is required for a repo project "
                     "(a JSON argv array, or 'none' for a project without tests)")
    if isinstance(value, list):
        argv: Any = value
    elif isinstance(value, str):
        if value == TESTS_NONE:
            return TESTS_NONE
        try:
            argv = json.loads(value)
        except ValueError:
            raise _usage("test_command", "--test-command must be a JSON argv array or 'none'", value=value)
    else:
        raise _usage("test_command", "--test-command must be a JSON argv array or 'none'")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a != "" for a in argv):
        raise _usage("test_command", "--test-command must be a non-empty JSON array of non-empty strings, or 'none'")
    return list(argv)


def encode_test_command(parsed: Union[str, List[str]]) -> str:
    """Stored form in `projects.test_command_json`: the argv JSON, or `"none"` (a JSON string)."""
    return json.dumps(parsed)


def tests_view(test_command_json_value: Optional[str]) -> Any:
    """`project show` form: `"none"` or the argv list; None for a non-repo project."""
    if test_command_json_value is None:
        return None
    try:
        return json.loads(test_command_json_value)
    except ValueError:
        return test_command_json_value


def setup_integration(repo: str, project_id: str, base_commit: str, ws_root: str) -> Dict[str, str]:
    """§10.2 steps 3–4: the integration branch at the base commit and its worktree.

    An existing `mesh/<prj>/integration` branch is rc 6 `ALREADY_EXISTS` (nothing created).
    If the worktree step fails after the branch was created, the branch stays (§10.7: Mesh
    deletes nothing automatically) and the error names it in `details.branch`.
    """
    import time as _time
    deadline = _time.monotonic() + SETUP_INTEGRATION_BUDGET_S

    def left() -> float:
        return deadline - _time.monotonic()

    branch = integration_branch_name(project_id)
    cp = _git("show-ref", ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], repo,
              _timeout_s=left(), branch=branch, repo=repo)
    if cp.returncode == 0:
        raise MeshError(
            code=c.ALREADY_EXISTS,
            exit_code=c.EXIT_CONFLICT,
            message=f"Branch '{branch}' already exists in '{repo}'",
            details={"branch": branch, "repo": repo},
        )
    cp = _git("branch", ["branch", branch, base_commit], repo, _timeout_s=left(), branch=branch, repo=repo)
    if cp.returncode != 0:
        raise _git_failed("branch", cp, branch=branch, repo=repo)
    wt = integration_worktree_path(ws_root, project_id)
    ensure_workspace_dir(os.path.dirname(wt))
    cp = _git("worktree add", ["worktree", "add", wt, branch], repo, _timeout_s=left(),
              branch=branch, repo=repo, worktree=wt)
    if cp.returncode != 0:
        raise _git_failed("worktree add", cp, branch=branch, repo=repo, worktree=wt)
    return {"integration_branch": branch, "integration_path": wt}


def add_lead_repo_worktree(repo: str, project_id: str, ws_root: str, agent_id: str) -> str:
    """The lead's detached `repo/` worktree at the integration branch tip (§10.1, §8).

    Skipped when the path already exists (a previous assignment of the same agent keeps
    its worktree). Returns the path. Raises MeshError when git fails.
    """
    path = lead_repo_worktree_path(ws_root, project_id, agent_id)
    if os.path.lexists(path):
        return path
    ensure_workspace_dir(os.path.dirname(path))
    branch = integration_branch_name(project_id)
    cp = _git("worktree add --detach", ["worktree", "add", "--detach", path, f"refs/heads/{branch}"], repo,
              branch=branch, repo=repo, worktree=path)
    if cp.returncode != 0:
        raise _git_failed("worktree add --detach", cp, branch=branch, repo=repo, worktree=path)
    return path


def project_repo_paths(conn: Any) -> List[str]:
    """Repo paths of all projects (for the boot and doctor install-path checks).

    Returns [] when the projects table does not exist yet (a DB older than the schema).
    """
    try:
        rows = conn.execute("SELECT DISTINCT repo_path FROM projects WHERE repo_path IS NOT NULL;").fetchall()
    except Exception:
        return []
    return [r[0] for r in rows if r[0]]


def _worktree_for_branch(repo: str, branch: str) -> Optional[str]:
    """The worktree path that has `refs/heads/<branch>` checked out, or None (§10.6 step 2)."""
    # Blind review P3: NUL-delimited (-z, git 2.36+), so a worktree path with a newline in
    # it is read whole; fields are never stripped (a path may end in a space).
    cp = _git("worktree list --porcelain -z", ["worktree", "list", "--porcelain", "-z"], repo, repo=repo)
    if cp.returncode != 0:
        raise _git_failed("worktree list --porcelain -z", cp, repo=repo)
    current: Optional[str] = None
    for field in (cp.stdout or "").split("\0"):
        if field.startswith("worktree "):
            current = field[len("worktree "):]
        elif field == f"branch refs/heads/{branch}":
            return current
    return None


def _require_worktree_on(worktree: str, base_branch: str, old: str, repo: str, new: str) -> None:
    """Raise rc 6 VERSION_MISMATCH (reason worktree_switched; nothing changed) unless
    ``worktree`` still has ``refs/heads/<base_branch>`` checked out. A base that moved
    while checked out is left to the ff/CAS checks, which report it as base_moved."""
    cp = _git("rev-parse HEAD ref", ["rev-parse", "--symbolic-full-name", "HEAD"], worktree, worktree=worktree)
    head_ref = (cp.stdout or "").strip() if cp.returncode == 0 else ""
    if head_ref == f"refs/heads/{base_branch}":
        return
    if head_ref == "HEAD":
        head_ref = ""  # detached
    cp = _git("rev-parse HEAD", ["rev-parse", "--verify", "HEAD"], worktree, worktree=worktree)
    head = (cp.stdout or "").strip() if cp.returncode == 0 else ""
    raise MeshError(
        code=c.VERSION_MISMATCH,
        exit_code=c.EXIT_CONFLICT,
        message=(f"Worktree '{worktree}' no longer has '{base_branch}' checked out (it has "
                 f"{head_ref or 'a detached HEAD'} at {head or '?'}); nothing was changed, retry the deliver"),
        details={"repo": repo, "worktree": worktree, "base_branch": base_branch,
                 "reason": "worktree_switched", "expected_ref": f"refs/heads/{base_branch}",
                 "actual_ref": head_ref or None, "expected_old": old, "actual": head or None, "new": new},
    )


def deliver_to_base(repo: str, base_branch: str, integration_branch: str) -> Dict[str, Any]:
    """§10.6: fast-forward `refs/heads/<base>` to `refs/heads/<integration>`.

    Both commits are pinned via their qualified heads refs. Never pushes or forces.

    Returns `{"old", "new", "method", "worktree"}` with `method` `"merge_ff_only"` (the
    base is checked out in a worktree, which must be clean), `"update_ref"` (a
    compare-and-swap on the ref) or `"noop"` (the base already is the integration tip, so
    nothing is written). Raises MeshError:

    - rc 14 `NOT_FAST_FORWARD` when `<base>` is not an ancestor of `<integration>`
      (nothing changed; the lead then creates a task to merge base into integration);
    - rc 14 `WORKTREE_DIRTY` with `details.worktree` when the base's worktree is dirty;
    - rc 6 `VERSION_MISMATCH` with `reason:"base_moved"` if a concurrent base move
      defeats the CAS or the post-merge base differs from the pinned integration SHA
      (`expected_old`/`actual` for CAS, `expected`/`actual` after the merge);
    - rc 1 `reason:"git_failed"` for any other git failure.
    """
    # Full ref names, never short names: a tag (or any other ref) named like the base branch
    # would otherwise shadow it and make the ancestry test answer for the wrong object.
    base_ref = f"refs/heads/{base_branch}"
    integration_ref = f"refs/heads/{integration_branch}"
    # Both shas are pinned once; the ancestry test and the compare-and-swap use exactly these,
    # so a base advanced by someone else in between makes the CAS fail instead of rolling it back.
    cp = _git("rev-parse base", ["rev-parse", "--verify", f"{base_ref}^{{commit}}"], repo,
              repo=repo, base_branch=base_branch)
    old = (cp.stdout or "").strip()
    if cp.returncode != 0 or not old:
        raise _git_failed("rev-parse base", cp, repo=repo, base_branch=base_branch)
    cp = _git("rev-parse integration", ["rev-parse", "--verify", f"{integration_ref}^{{commit}}"], repo,
              repo=repo, integration_branch=integration_branch)
    new = (cp.stdout or "").strip()
    if cp.returncode != 0 or not new:
        raise _git_failed("rev-parse integration", cp, repo=repo, integration_branch=integration_branch)
    cp = _git("merge-base --is-ancestor", ["merge-base", "--is-ancestor", old, new],
              repo, repo=repo, base_branch=base_branch, integration_branch=integration_branch)
    if cp.returncode != 0:
        raise MeshError(
            code=c.NOT_FAST_FORWARD,
            exit_code=c.EXIT_MERGE_FAILED,
            message=(f"Base branch '{base_branch}' is not an ancestor of '{integration_branch}'; "
                     "Mesh never rebases or forces"),
            details={"repo": repo, "base_branch": base_branch, "integration_branch": integration_branch},
        )

    # Already delivered: nothing to write, and no event to record (review m2-r8 MINOR-2).
    if old == new:
        return {"old": old, "new": new, "method": "noop", "worktree": None}

    worktree = _worktree_for_branch(repo, base_branch)
    if worktree is not None:
        cp = _git("status", ["status", "--porcelain=v1", "--untracked-files=all"], worktree, worktree=worktree)
        if cp.returncode != 0:
            raise _git_failed("status", cp, worktree=worktree)
        if (cp.stdout or "").strip():
            raise MeshError(
                code=c.WORKTREE_DIRTY,
                exit_code=c.EXIT_MERGE_FAILED,
                message=f"Worktree '{worktree}' has '{base_branch}' checked out and is not clean",
                details={"worktree": worktree, "base_branch": base_branch, "repo": repo},
            )
        # Linear full review A3: the ff merge moves whatever that worktree has checked out,
        # so confirm, right before it, that this is still the base branch at the expected
        # commit (a concurrent `git switch` there must not advance an unrelated branch).
        _require_worktree_on(worktree, base_branch, old, repo, new)
        # The resolved sha, never the branch name: a tag (or any other ref) with the same
        # short name as the integration branch must not be what gets merged (r8 N-1).
        cp = _git("merge --ff-only", ["merge", "--ff-only", new], worktree,
                  worktree=worktree, integration_branch=integration_branch)
        if cp.returncode != 0:
            _raise_if_base_moved(repo, base_branch, old, new)
            raise _git_failed("merge --ff-only", cp, worktree=worktree, integration_branch=integration_branch)
        cp = _git("rev-parse base after ff", ["rev-parse", "--verify", base_ref], worktree,
                  worktree=worktree, base_branch=base_branch)
        if cp.returncode != 0:
            raise _git_failed("rev-parse base after ff", cp, repo=repo, worktree=worktree,
                              base_branch=base_branch)
        landed = (cp.stdout or "").strip()
        if landed != new:
            raise MeshError(
                code=c.VERSION_MISMATCH,
                exit_code=c.EXIT_CONFLICT,
                message=(f"Base branch '{base_branch}' is at {landed} after the deliver; "
                         f"expected {new}"),
                details={"repo": repo, "worktree": worktree, "base_branch": base_branch,
                         "reason": "base_moved", "expected_old": old, "expected": new,
                         "actual": landed, "new": new},
            )
        return {"old": old, "new": new, "method": "merge_ff_only", "worktree": worktree}

    cp = _git("update-ref", ["update-ref", base_ref, new, old], repo,
              repo=repo, base_branch=base_branch)
    if cp.returncode != 0:
        _raise_if_base_moved(repo, base_branch, old, new)
        raise _git_failed("update-ref", cp, repo=repo, base_branch=base_branch)
    return {"old": old, "new": new, "method": "update_ref", "worktree": None}


def _raise_if_base_moved(repo: str, base_branch: str, old: str, new: str) -> None:
    """After a failed CAS: rc 6 `VERSION_MISMATCH` (`reason:"base_moved"`) when
    `refs/heads/<base>` is no longer `old`. Nothing was written; the deliver can be retried."""
    cp = _git("rev-parse base after cas", ["rev-parse", "--verify", "--quiet", f"refs/heads/{base_branch}^{{commit}}"],
              repo, repo=repo, base_branch=base_branch)
    actual = (cp.stdout or "").strip() if cp.returncode == 0 else None
    if actual == old:
        return
    raise MeshError(
        code=c.VERSION_MISMATCH,
        exit_code=c.EXIT_CONFLICT,
        message=(f"Base branch '{base_branch}' moved from {old} to {actual} during the deliver; "
                 "nothing was changed"),
        details={"repo": repo, "base_branch": base_branch, "reason": "base_moved",
                 "expected_old": old, "actual": actual, "new": new},
    )


def deliver_plan(repo: str, base_branch: str, integration_branch: str) -> Dict[str, Any]:
    """Read-only plan for `project deliver --dry-run` (§10.6): `{"fast_forward": bool}`.

    `fast_forward` is True when `refs/heads/<base>` is an ancestor of
    `refs/heads/<integration>` (the deliver would succeed) and False otherwise. Nothing is
    written; a git failure is reported as `fast_forward: False` rather than raised, because a
    dry run must not fail on the state it is describing.
    """
    cp = _git("merge-base --is-ancestor",
              ["merge-base", "--is-ancestor", f"refs/heads/{base_branch}", f"refs/heads/{integration_branch}"],
              repo, repo=repo, base_branch=base_branch, integration_branch=integration_branch)
    return {"fast_forward": cp.returncode == 0}


def _porcelain_paths(text: str) -> List[str]:
    """Paths from `status --porcelain=v1` (a rename `R  a -> b` lists `b`)."""
    paths: List[str] = []
    for line in (text or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if line[0] in ("R", "C") and " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def bound_git_info(info: Dict[str, Any]) -> Dict[str, Any]:
    """Redact (§15.2) and size-bound a `deliverable.git` block.

    `dirty_files` keeps the first GIT_DIRTY_FILES_CAP paths, with `dirty_count` (always
    set when `dirty_files` is) and `dirty_files_truncated`; `diff_stat` keeps the first
    GIT_DIFF_STAT_CAP bytes (UTF-8, cut on a character boundary), with `diff_stat_truncated`.
    """
    from mesh.core.redact import redact_value

    out = dict(info)
    files = out.get("dirty_files")
    if isinstance(files, list):
        out["dirty_count"] = len(files)
        out["dirty_files_truncated"] = len(files) > GIT_DIRTY_FILES_CAP
        out["dirty_files"] = files[:GIT_DIRTY_FILES_CAP]
    stat = out.get("diff_stat")
    if isinstance(stat, str):
        raw = stat.encode("utf-8")
        out["diff_stat_truncated"] = len(raw) > GIT_DIFF_STAT_CAP
        if out["diff_stat_truncated"]:
            out["diff_stat"] = raw[:GIT_DIFF_STAT_CAP].decode("utf-8", errors="ignore")
    return redact_value(out)


def submission_git_info(workspace: str, branch: Optional[str], base_commit: Optional[str],
                        timeout_s: float = 30.0) -> Dict[str, Any]:
    """`deliverable.git`, redacted and bounded (see `bound_git_info`)."""
    return bound_git_info(_collect_git_info(workspace, branch, base_commit, timeout_s))


def _collect_git_info(workspace: str, branch: Optional[str], base_commit: Optional[str],
                      timeout_s: float = 30.0) -> Dict[str, Any]:
    """`deliverable.git` for a code task at submit (§10.4). Read-only; no auto-commit.

    `{head, branch, base_commit, dirty, dirty_files, diff_stat, commit_count}`.
    Any git failure (or a missing workspace/base) gives `error: {step, git_rc|timeout_s,
    stderr_tail}` next to what was collected; it never blocks the submit (m2-r4).
    Safe in a worker thread: git only, through run_git (A6, registered children).
    """
    info: Dict[str, Any] = {"branch": branch, "base_commit": base_commit}

    def fail(step: str, **d: Any) -> Dict[str, Any]:
        err: Dict[str, Any] = {"step": step}
        err.update(d)
        info["error"] = err
        return info

    if not workspace or not os.path.isdir(workspace):
        return fail("workspace", reason="workspace_missing", workspace=workspace)

    def git(step: str, args: List[str]) -> Optional[Any]:
        try:
            cp = run_git(args, cwd=workspace, timeout_s=timeout_s)
        except subprocess.TimeoutExpired as e:
            fail(step, reason="git_timeout", timeout_s=e.timeout)
            return None
        except OSError as e:
            fail(step, reason="git_unavailable", stderr_tail=str(e)[-_STDERR_TAIL:])
            return None
        if cp.returncode != 0:
            fail(step, reason="git_failed", git_rc=cp.returncode, stderr_tail=(cp.stderr or "")[-_STDERR_TAIL:])
            return None
        return cp

    cp = git("rev-parse HEAD", ["rev-parse", "HEAD"])
    if cp is None:
        return info
    info["head"] = (cp.stdout or "").strip()
    cp = git("status", ["status", "--porcelain=v1", "--untracked-files=all"])
    if cp is None:
        return info
    dirty_files = _porcelain_paths(cp.stdout)
    info["dirty"] = bool(dirty_files)
    info["dirty_files"] = dirty_files
    if not base_commit:
        return fail("base_commit", reason="no_base_commit")
    cp = git("diff --stat", ["diff", "--stat", f"{base_commit}..HEAD"])
    if cp is None:
        return info
    info["diff_stat"] = (cp.stdout or "").rstrip("\n")
    cp = git("log", ["log", "--format=%H", f"{base_commit}..HEAD"])
    if cp is None:
        return info
    info["commit_count"] = len([ln for ln in (cp.stdout or "").splitlines() if ln.strip()])
    return info
