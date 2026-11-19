"""Repo projects (plan §10.1, §10.2): validation, integration branch/worktree, lead `repo/`.

Every git call goes through `mesh.core.gitops.run_git` (A6) with an explicit `cwd`.
Nothing here deletes a branch or a worktree (§10.7: nothing is removed automatically).
"""

import json
import os
import stat
import subprocess
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

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


def remaining_git_budget(default: float = 30.0) -> float:
    """Remaining seconds in the active ``git_budget``, or ``default`` when none is active."""
    deadline = getattr(_budget, "deadline", None)
    if deadline is None:
        return default
    import time as _time
    return min(default, deadline - _time.monotonic())


def _git(step: str, args: List[str], cwd: str, _timeout_s: float = 30.0, **details: Any) -> Any:
    """run_git, with a timeout (30 s in gitops) mapped to rc 1 `reason:"git_timeout"`.

    `details` (e.g. `branch`, `worktree`) go into the error, so a timeout after the
    branch step still names the branch (Q3; review m2-r3 finding 2).
    """
    try:
        _timeout_s = remaining_git_budget(_timeout_s)
        if _timeout_s <= 0:
            raise subprocess.TimeoutExpired(args, 0)
        return run_git(args, cwd=cwd, timeout_s=_timeout_s)
    except subprocess.TimeoutExpired as e:
        d: Dict[str, Any] = {"reason": "git_timeout", "step": step, "timeout_s": e.timeout}
        d.update(details)
        raise MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                        message=f"git {step} timed out after {e.timeout:g} s", details=d)
    except OSError as e:
        d = {"reason": "git_failed", "step": step, "errno": e.errno,
             "stderr_tail": str(e)[-_STDERR_TAIL:]}
        d.update(details)
        raise MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                        message=f"git {step} failed: {e}", details=d) from e


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
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
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


def _require_trusted_integration_path(wt: str, ws_root: str) -> str:
    """Return ``resolve_trusted_path(wt, root=ws_root)`` or raise INVALID_ARGUMENT."""
    from mesh.core.paths import resolve_trusted_path

    trusted = resolve_trusted_path(wt, root=ws_root)
    if trusted is None:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Integration worktree path '{wt}' is symlinked or outside workspaces.root",
            details={"worktree": wt, "workspaces_root": ws_root, "reason": "symlink_or_outside_root"},
        )
    return trusted


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
    wt = integration_worktree_path(ws_root, project_id)
    trusted = _require_trusted_integration_path(wt, ws_root)
    cp = _git("branch", ["branch", branch, base_commit], repo, _timeout_s=left(), branch=branch, repo=repo)
    if cp.returncode != 0:
        raise _git_failed("branch", cp, branch=branch, repo=repo)
    ensure_workspace_dir(os.path.dirname(trusted))
    trusted = _require_trusted_integration_path(trusted, ws_root)
    cp = _git("worktree add", ["worktree", "add", trusted, branch], repo, _timeout_s=left(),
              branch=branch, repo=repo, worktree=wt)
    if cp.returncode != 0:
        raise _git_failed("worktree add", cp, branch=branch, repo=repo, worktree=wt)
    return {"integration_branch": branch, "integration_path": wt}


def add_lead_repo_worktree(repo: str, project_id: str, ws_root: str, agent_id: str) -> str:
    """The lead's detached `repo/` worktree at the integration branch tip (§10.1, §8).

    Skipped when the path already exists (a previous assignment of the same agent keeps
    its worktree). Returns the path. Raises MeshError when git fails or path is symlinked.
    """
    from mesh.core.paths import resolve_trusted_path

    path = lead_repo_worktree_path(ws_root, project_id, agent_id)
    trusted = resolve_trusted_path(path, root=ws_root)
    if trusted is None:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Lead repo worktree path '{path}' is symlinked or outside workspaces.root",
            details={"worktree": path, "workspaces_root": ws_root, "reason": "symlink_or_outside_root"},
        )
    if os.path.lexists(trusted):
        return path
    ensure_workspace_dir(os.path.dirname(trusted))
    if resolve_trusted_path(trusted, root=ws_root) is None:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Lead repo worktree path '{path}' is symlinked or outside workspaces.root",
            details={"worktree": path, "workspaces_root": ws_root, "reason": "symlink_or_outside_root"},
        )
    branch = integration_branch_name(project_id)
    cp = _git("worktree add --detach", ["worktree", "add", "--detach", trusted, f"refs/heads/{branch}"], repo,
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


def _legacy_worktree_record_ok(rec: List[str]) -> bool:
    """One newline-delimited record in git's field order: worktree, bare | HEAD + branch/detached,
    then optional locked and prunable lines."""
    if not rec or not rec[0].startswith("worktree "):
        return False
    rest, i = rec[1:], 0
    if rest[:1] == ["bare"]:
        i = 1
    else:
        if not (len(rest) > 1 and rest[0].startswith("HEAD ")
                and (rest[1].startswith("branch ") or rest[1] == "detached")):
            return False
        i = 2
    for kw in ("locked", "prunable"):
        if len(rest) > i and (rest[i] == kw or rest[i].startswith(kw + " ")):
            i += 1
    return i == len(rest)


def _legacy_worktree_records(out: str, repo: str) -> List[List[str]]:
    """The records of the newline format: git ends every record with exactly one blank line.
    Fail closed on anything else (a worktree path or lock reason with a newline in it, or a
    broken worktree), so a checked-out base is never mistaken for "no worktree" and no
    record is skipped by the path checks."""
    def bad() -> MeshError:
        return MeshError(
            code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
            message=("git worktree list --porcelain has an unparseable worktree record; possible "
                     "causes: a worktree path or lock reason with a newline in it, or a broken "
                     "worktree (e.g. a bad HEAD). Run `git worktree prune` / `git worktree repair`, "
                     "or upgrade git to 2.36+ for -z"),
            details={"reason": "worktree_list_ambiguous", "repo": repo},
        )

    lines = out.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    recs: List[List[str]] = []
    rec: List[str] = []
    for line in lines:
        if line:
            rec.append(line)
            continue
        if not rec or not _legacy_worktree_record_ok(rec):
            raise bad()
        recs.append(rec)
        rec = []
    if rec:
        raise bad()
    return recs


def _legacy_ambiguous(repo: str, path: str, why: str) -> MeshError:
    return MeshError(
        code=c.WORKTREE_DIRTY, exit_code=c.EXIT_MERGE_FAILED,
        message=(f"git worktree list --porcelain lists a worktree Mesh cannot confirm ({why}); possible "
                 "causes: a worktree path with a newline in it, or a broken or removed worktree. Run "
                 "`git worktree prune` / `git worktree repair`, or upgrade git to 2.36+ for -z"),
        details={"reason": "worktree_list_ambiguous", "repo": repo, "worktree": path, "check": why},
    )


def _verify_legacy_worktree_paths(recs: List[List[str]], repo: str) -> None:
    """Newline format only: a path with a newline can forge whole records, so every listed
    worktree must be this repository's main worktree or a linked worktree registered in its
    common dir, checked from files alone (no git runs inside a listed path)."""
    cp = _git("rev-parse --git-common-dir", ["rev-parse", "--git-common-dir"], repo, repo=repo)
    common = (cp.stdout or "").rstrip("\r\n")
    if cp.returncode != 0 or not common:
        raise _git_failed("rev-parse --git-common-dir", cp, repo=repo)
    repo_common = os.path.realpath(os.path.join(repo, common))
    main_paths = {os.path.realpath(repo), repo_common, os.path.dirname(repo_common)}
    for lines in recs:
        path = lines[0][len("worktree "):]
        if (any(ln == "prunable" or ln.startswith("prunable ") for ln in lines[1:])
                and path and not os.path.lexists(path)):
            continue  # a prunable, removed worktree is never returned as checked out
        if not path or "\n" in path or "\r" in path or not os.path.isabs(path):
            raise _legacy_ambiguous(repo, path, "unusable path")
        if not os.path.isdir(path):
            raise _legacy_ambiguous(repo, path, "not a directory")
        if os.path.realpath(path) in main_paths:
            continue
        rem = remaining_git_budget()
        if rem <= 0:
            raise MeshError(
                code=c.INTERNAL_ERROR,
                exit_code=c.EXIT_INTERNAL,
                message="git verify worktree timed out after 0 s",
                details={"reason": "git_timeout", "step": "verify worktree", "timeout_s": 0,
                         "worktree": path, "repo": repo},
            )
        if not is_registered_worktree(path, repo, timeout_s=rem):
            raise _legacy_ambiguous(repo, path, "not a registered worktree of this repository")


def _worktree_for_branch(repo: str, branch: str) -> Optional[str]:
    """The worktree path that has `refs/heads/<branch>` checked out, or None (§10.6 step 2)."""
    # Blind review P3: NUL-delimited (-z, git 2.36+), so a worktree path with a newline in
    # it is read whole; fields are never stripped (a path may end in a space).
    # Devin full-review P2-3: git < 2.36 rejects -z (usage error, rc 129); fall back to the
    # newline-delimited porcelain format, which has the same fields and blank-line records.
    sep = "\0"
    step = "worktree list --porcelain -z"
    cp = _git(step, ["worktree", "list", "--porcelain", "-z"], repo, repo=repo)
    if cp.returncode == 129:
        sep = "\n"
        step = "worktree list --porcelain"
        cp = _git(step, ["worktree", "list", "--porcelain"], repo, repo=repo)
    if cp.returncode != 0:
        raise _git_failed(step, cp, repo=repo)
    if sep == "\n":
        _verify_legacy_worktree_paths(_legacy_worktree_records(cp.stdout or "", repo), repo)
    target = f"refs/heads/{branch}"
    current: Optional[str] = None
    matched = False
    prunable = False
    for field in (cp.stdout or "").split(sep):
        if field == "":
            if current is not None and matched and not (prunable and not os.path.lexists(current)):
                return current
            current = None
            matched = False
            prunable = False
        elif field.startswith("worktree "):
            current = field[len("worktree "):]
        elif field == f"branch {target}":
            matched = True
        elif field == "prunable" or field.startswith("prunable "):
            prunable = True
    if current is not None and matched and not (prunable and not os.path.lexists(current)):
        return current
    return None


_WORKTREE_IN_PROGRESS_FILES = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_START",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
)


def _worktree_git_in_progress(wt_real: str, worktree: str, repo: str) -> Optional[str]:
    """Return the name of an active git operation in ``wt_real``, or None."""
    for name in _WORKTREE_IN_PROGRESS_FILES:
        cp = _git("rev-parse --git-path", ["rev-parse", "--git-path", name], wt_real,
                  worktree=worktree, repo=repo)
        if cp.returncode != 0:
            raise _git_failed("rev-parse --git-path", cp, worktree=worktree, repo=repo, state=name)
        raw = (cp.stdout or "").rstrip("\r\n")
        if not raw:
            raise _git_failed("rev-parse --git-path", cp, worktree=worktree, repo=repo, state=name)
        path = raw if os.path.isabs(raw) else os.path.join(wt_real, raw)
        if os.path.lexists(path):
            return name
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


def _validate_deliver_worktree(worktree: str, repo: str, base_branch: str) -> str:
    """Verify ``worktree`` is a non-symlinked, registered worktree of ``repo`` before running git in it."""
    from mesh.core.paths import resolve_trusted_path

    def _foreign(message: str) -> MeshError:
        return MeshError(
            code=c.WORKTREE_DIRTY,
            exit_code=c.EXIT_MERGE_FAILED,
            message=message,
            details={"worktree": worktree, "base_branch": base_branch, "repo": repo,
                     "reason": "foreign_repository"},
        )

    wt_real = resolve_trusted_path(worktree)
    if wt_real is None or not os.path.isdir(wt_real):
        raise _foreign(f"Worktree '{worktree}' is missing, symlinked, or invalid")
    dot_git = os.path.join(wt_real, ".git")
    if os.path.islink(dot_git) or not os.path.lexists(dot_git):
        raise _foreign(f"Worktree '{worktree}' is not a registered worktree of '{repo}'")
    if wt_real != os.path.realpath(repo) or os.path.isfile(dot_git):
        rem = remaining_git_budget()
        if rem <= 0:
            raise MeshError(
                code=c.INTERNAL_ERROR,
                exit_code=c.EXIT_INTERNAL,
                message="git verify worktree timed out after 0 s",
                details={"reason": "git_timeout", "step": "verify worktree", "timeout_s": 0,
                         "worktree": worktree, "repo": repo},
            )
        if not is_registered_worktree(wt_real, repo, timeout_s=rem):
            raise _foreign(f"Worktree '{worktree}' is not a registered worktree of '{repo}'")
    top_cp = _git("verify worktree", ["rev-parse", "--show-toplevel"], wt_real, worktree=worktree, repo=repo)
    top = (top_cp.stdout or "").strip()
    if top_cp.returncode != 0 or not top or os.path.realpath(top) != wt_real:
        raise _foreign(f"Worktree '{worktree}' top level does not match '{wt_real}'")
    return wt_real


def deliver_to_base(repo: str, base_branch: str, integration_branch: str,
                    expected: Optional[Tuple[str, str]] = None,
                    before_write: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
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
    if expected is not None and (old, new) != tuple(expected):
        # Full review r1 P1-5: the recorded intent names (old, new); a branch that moved since
        # is not delivered under it. Nothing was written; the deliver can be retried.
        raise MeshError(
            code=c.VERSION_MISMATCH,
            exit_code=c.EXIT_CONFLICT,
            message=f"Base or integration branch moved during the deliver; nothing was changed, retry",
            details={"repo": repo, "base_branch": base_branch, "integration_branch": integration_branch,
                     "reason": "base_moved", "expected_old": expected[0], "actual": old,
                     "expected": expected[1], "new": new},
        )
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
        wt_real = _validate_deliver_worktree(worktree, repo, base_branch)

        cp = _git("status", ["status", "--porcelain=v1", "--untracked-files=all"], wt_real, worktree=worktree)
        if cp.returncode != 0:
            raise _git_failed("status", cp, worktree=worktree)
        dirty = bool((cp.stdout or "").strip())
        if not dirty:
            from mesh.daemon.merge import _read_index_flags

            idx_flags = _read_index_flags(wt_real, "deliver index flags")
            dirty = idx_flags is None or any(au or sw or ita for au, sw, ita in idx_flags.values())
        in_progress = None if dirty else _worktree_git_in_progress(wt_real, worktree, repo)
        if dirty or in_progress is not None:
            details: Dict[str, Any] = {"worktree": worktree, "base_branch": base_branch, "repo": repo}
            if in_progress is not None:
                details["reason"] = "git_in_progress"
                details["state"] = in_progress
            raise MeshError(
                code=c.WORKTREE_DIRTY,
                exit_code=c.EXIT_MERGE_FAILED,
                message=f"Worktree '{worktree}' has '{base_branch}' checked out and is not clean",
                details=details,
            )
        # Linear full review A3: the ff merge moves whatever that worktree has checked out,
        # so confirm, right before it, that this is still the base branch at the expected
        # commit (a concurrent `git switch` there must not advance an unrelated branch).
        _require_worktree_on(wt_real, base_branch, old, repo, new)
        # Full review r7 (R2-P1-1): `status` above never lists ignored files, and an ff merge
        # replaces an ignored, untracked file by default (git's --overwrite-ignore). A
        # delivered commit that adds a path the user ignores locally (`.env`, a local config)
        # would overwrite that file, whose content is in no git object. Refuse before
        # writing (after the base-position check, so a moved base still reports base_moved); `--no-overwrite-ignore` on the merge is the backstop.
        cp = _git("rev-parse base before ff", ["rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"], wt_real,
                  worktree=worktree, base_branch=base_branch)
        if cp.returncode != 0:
            raise _git_failed("rev-parse base before ff", cp, repo=repo, worktree=worktree,
                              base_branch=base_branch)
        actual_before = (cp.stdout or "").strip()
        if actual_before != old:
            raise MeshError(
                code=c.VERSION_MISMATCH,
                exit_code=c.EXIT_CONFLICT,
                message=(f"Base branch '{base_branch}' moved from {old} to {actual_before} during the deliver; "
                         "nothing was changed"),
                details={"repo": repo, "worktree": worktree, "base_branch": base_branch,
                         "reason": "base_moved", "expected_old": old, "expected": new,
                         "actual": actual_before, "new": new},
            )
        clobbered = _ignored_paths_overwritten(wt_real, repo, old, new)
        if clobbered:
            raise MeshError(
                code=c.WORKTREE_DIRTY,
                exit_code=c.EXIT_MERGE_FAILED,
                message=(f"Worktree '{worktree}' has untracked (ignored) files the deliver would "
                         f"overwrite: {', '.join(clobbered[:5])}{' …' if len(clobbered) > 5 else ''}; "
                         "move them away and retry"),
                details={"worktree": worktree, "base_branch": base_branch, "repo": repo,
                         "reason": "ignored_files_would_be_overwritten",
                         "paths": clobbered[:GIT_DIRTY_FILES_CAP],
                         "paths_truncated": len(clobbered) > GIT_DIRTY_FILES_CAP},
            )
        if before_write is not None:
            before_write()
        # The resolved sha, never the branch name: a tag (or any other ref) with the same
        # short name as the integration branch must not be what gets merged (r8 N-1).
        cp = _git("merge --ff-only", ["merge", "--ff-only", "--no-overwrite-ignore", new], wt_real,
                  worktree=worktree, integration_branch=integration_branch)
        if cp.returncode != 0:
            # Devin full-review P3: merge may exit non-zero (e.g. killed by a signal) after the
            # ref moved, so a mismatch here may follow a write: settle/recovery records it.
            _raise_if_base_moved(repo, base_branch, old, new, after_write=True)
            raise _git_failed("merge --ff-only", cp, worktree=worktree, integration_branch=integration_branch)
        cp = _git("rev-parse base after ff", ["rev-parse", "--verify", base_ref], wt_real,
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
                         "actual": landed, "new": new, "after_write": True},
            )
        return {"old": old, "new": new, "method": "merge_ff_only", "worktree": worktree}

    if before_write is not None:
        before_write()
    cp = _git("update-ref", ["update-ref", base_ref, new, old], repo,
              repo=repo, base_branch=base_branch)
    if cp.returncode != 0:
        _raise_if_base_moved(repo, base_branch, old, new)
        raise _git_failed("update-ref", cp, repo=repo, base_branch=base_branch)
    return {"old": old, "new": new, "method": "update_ref", "worktree": None}


def _dir_has_non_deleted_entry(root_dir: str, rel_prefix: str, deleted: Set[str]) -> bool:
    """Return True if ``root_dir`` contains any non-directory or symlink entry outside ``deleted``."""
    stack = [(root_dir, rel_prefix)]
    while stack:
        cur_dir, cur_rel = stack.pop()
        try:
            with os.scandir(cur_dir) as it:
                entries = list(it)
        except OSError:
            return True
        for entry in entries:
            entry_rel = f"{cur_rel}/{entry.name}" if cur_rel else entry.name
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    if entry_rel not in deleted:
                        return True
                else:
                    stack.append((entry.path, entry_rel))
            except OSError:
                return True
    return False


def _ignored_paths_overwritten(worktree: str, repo: str, old: str, new: str) -> List[str]:
    """Full review r7 (R2-P1-1): paths an ff from ``old`` to ``new`` would write over an
    untracked file in ``worktree``. Read-only.

    Only paths the ff *adds* can hit an untracked file (a path tracked at ``old`` is clean
    in the worktree, checked by the caller's `status`). An added path collides when a
    non-deleted file/symlink exists at it, when it replaces a directory containing anything
    outside ``deleted``, or when one of its parent paths is an undeleted symlink or
    non-directory."""
    cp = _git("diff --name-status", ["diff", "--name-status", "-z", "--no-renames", old, new], repo,
              repo=repo)
    if cp.returncode != 0:
        raise _git_failed("diff --name-status", cp, repo=repo)
    fields = (cp.stdout or "").split("\0")
    added: List[str] = []
    deleted: Set[str] = set()
    for status, path in zip(fields[0::2], fields[1::2]):
        if status == "A":
            added.append(path)
        elif status == "D":
            deleted.add(path)
    hits: List[str] = []
    for path in added:
        parts = path.split("/")
        # Inspect ancestors top-down via lstat before touching `full` so a symlink parent
        # never leaks probes outside `worktree`.
        blocked_or_gone = False
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            pfull = os.path.join(worktree, *parts[:i])
            try:
                pst = os.lstat(pfull)
            except FileNotFoundError:
                blocked_or_gone = True
                break
            except OSError:
                hits.append(path)
                blocked_or_gone = True
                break
            if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
                if parent not in deleted:
                    hits.append(path)
                blocked_or_gone = True
                break
        if blocked_or_gone:
            continue
        full = os.path.join(worktree, *parts)
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            continue
        except OSError:
            hits.append(path)
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            hits.append(path)
        elif _dir_has_non_deleted_entry(full, path, deleted):
            hits.append(path)
    return hits


def _raise_if_base_moved(repo: str, base_branch: str, old: str, new: str, after_write: bool = False) -> None:
    """After a failed CAS or merge: rc 6 `VERSION_MISMATCH` (`reason:"base_moved"`) when
    `refs/heads/<base>` is no longer `old`. After a failed merge (``after_write``) the ref may
    already be at `new`; a failed update-ref CAS wrote nothing unless the ref is at `new`
    (git exited non-zero after the ref transaction), which is reported as ``after_write`` too."""
    cp = _git("rev-parse base after cas", ["rev-parse", "--verify", "--quiet", f"refs/heads/{base_branch}^{{commit}}"],
              repo, repo=repo, base_branch=base_branch)
    actual = (cp.stdout or "").strip() if cp.returncode == 0 else None
    if actual == old:
        return
    after_write = after_write or actual == new
    raise MeshError(
        code=c.VERSION_MISMATCH,
        exit_code=c.EXIT_CONFLICT,
        message=(f"Base branch '{base_branch}' moved from {old} to {actual} during the deliver; "
                 + ("the delivery may have been written; retry `mesh project deliver` to settle it"
                    if after_write else "nothing was changed")),
        details={"repo": repo, "base_branch": base_branch, "reason": "base_moved",
                 "expected_old": old, "actual": actual, "new": new,
                 **({"after_write": True} if after_write else {})},
    )


def deliver_heads(repo: str, base_branch: str, integration_branch: str) -> Optional[Tuple[str, str]]:
    """Read-only `(base sha, integration sha)` by full refs, or None on any git failure."""
    out = []
    for ref in (f"refs/heads/{base_branch}", f"refs/heads/{integration_branch}"):
        try:
            cp = _git("rev-parse", ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo, repo=repo)
        except MeshError:
            return None
        sha = (cp.stdout or "").strip()
        if cp.returncode != 0 or not sha:
            return None
        out.append(sha)
    return out[0], out[1]


#: Full review r1 P1-5: `project deliver` moves the user's base branch inside the request's
#: SQLite transaction. An intent, fsynced before the git write, lets recovery record the
#: `project.delivered` event (and the approval's consumption) if meshd dies after the
#: base moved but before the transaction committed.
DELIVER_INTENT_DIR = "deliver-intents"


def deliver_intent_path(home: str, project_id: str) -> str:
    return os.path.join(home, DELIVER_INTENT_DIR, f"{project_id}.json")


def write_deliver_intent(home: str, doc: Dict[str, Any]) -> str:
    """Write `doc` durably (temp file, fsync, rename, directory fsync). Raises OSError."""
    import tempfile

    parent = os.path.join(home, DELIVER_INTENT_DIR)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    path = deliver_intent_path(home, doc["project_id"])
    data = (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".intent_")
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return path


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


def is_registered_worktree(
    wt_path: str,
    repo_path: str,
    timeout_s: float = 30.0,
    *,
    root: Optional[str] = None,
) -> bool:
    """Full review r5 P1-3/P2-4 & r6 P1-3: ``wt_path`` is a non-symlinked worktree
    registered in ``repo_path``, checked from files alone before any git runs in it."""
    from mesh.core.paths import resolve_trusted_path

    try:
        wt_real = resolve_trusted_path(wt_path, root=root)
        if wt_real is None or not os.path.isdir(wt_real):
            return False
        dot_git = os.path.join(wt_real, ".git")
        if os.path.islink(dot_git) or not os.path.isfile(dot_git):
            return False
        with open(dot_git, "r", encoding="utf-8") as f:
            raw = f.read(4096).strip()
        if not raw.startswith("gitdir:"):
            return False
        gitdir = os.path.realpath(os.path.join(wt_real, raw[len("gitdir:"):].strip()))
        cp = run_git(["rev-parse", "--git-common-dir"], cwd=repo_path, timeout_s=timeout_s)
        common = (cp.stdout or "").strip()
        if cp.returncode != 0 or not common:
            return False
        repo_common = os.path.realpath(os.path.join(repo_path, common))
        if os.path.dirname(gitdir) != os.path.join(repo_common, "worktrees"):
            return False
        commondir_file = os.path.join(gitdir, "commondir")
        if os.path.lexists(commondir_file):
            if os.path.islink(commondir_file):
                return False
            with open(commondir_file, "r", encoding="utf-8") as f:
                if os.path.realpath(os.path.join(gitdir, f.read(4096).strip())) != repo_common:
                    return False
        back_file = os.path.join(gitdir, "gitdir")
        if os.path.islink(back_file):
            return False
        with open(back_file, "r", encoding="utf-8") as f:
            back = os.path.realpath(os.path.join(gitdir, f.read(4096).strip()))
        return back == dot_git
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def submission_git_info(workspace: str, branch: Optional[str], base_commit: Optional[str],
                        timeout_s: float = 30.0, repo_path: Optional[str] = None) -> Dict[str, Any]:
    """`deliverable.git`, redacted and bounded (see `bound_git_info`)."""
    return bound_git_info(_collect_git_info(workspace, branch, base_commit, timeout_s, repo_path=repo_path))


def _collect_git_info(workspace: str, branch: Optional[str], base_commit: Optional[str],
                      timeout_s: float = 30.0, repo_path: Optional[str] = None) -> Dict[str, Any]:
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

    from mesh.core.paths import resolve_trusted_path

    if not workspace or not isinstance(workspace, str) or not workspace.strip():
        return fail("workspace", reason="workspace_missing", workspace=workspace)
    ws_clean = workspace.rstrip(os.sep)
    ws_real = resolve_trusted_path(workspace)
    if ws_real is None:
        if not ws_clean or not os.path.lexists(ws_clean):
            return fail("workspace", reason="workspace_missing", workspace=workspace)
        return fail("verify worktree", reason="foreign_repository")
    if not os.path.isdir(ws_real):
        return fail("workspace", reason="workspace_missing", workspace=workspace)

    def git(step: str, args: List[str]) -> Optional[Any]:
        try:
            cp = run_git(args, cwd=ws_real, timeout_s=timeout_s)
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

    if repo_path:
        # Full review r4 P2-3 / r5 P2-4 / r6 P1-3: the workspace must still be a
        # non-symlinked registered worktree of the project's repository before git runs.
        if not is_registered_worktree(ws_real, repo_path, timeout_s):
            return fail("verify worktree", reason="foreign_repository")
        cp = git("verify worktree", ["rev-parse", "--show-toplevel"])
        if cp is None:
            return info
        if os.path.realpath((cp.stdout or "").strip()) != ws_real:
            return fail("verify worktree", reason="foreign_repository")
    if branch:
        sym = git("rev-parse HEAD symref", ["rev-parse", "--symbolic-full-name", "HEAD"])
        if sym is None:
            return info
        actual_ref = (sym.stdout or "").strip()
        if actual_ref == "HEAD":
            return fail("rev-parse HEAD symref", reason="detached_head")
        if actual_ref != f"refs/heads/{branch}":
            return fail("rev-parse HEAD symref", reason="wrong_branch", expected=f"refs/heads/{branch}", actual=actual_ref)
    cp = git("rev-parse HEAD", ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"] if branch else ["rev-parse", "HEAD"])
    if cp is None:
        return info
    head_sha = (cp.stdout or "").strip()
    info["head"] = head_sha
    cp = git("status", ["status", "--porcelain=v1", "--untracked-files=all"])
    if cp is None:
        return info
    dirty_files = _porcelain_paths(cp.stdout)
    info["dirty"] = bool(dirty_files)
    info["dirty_files"] = dirty_files
    if not base_commit:
        return fail("base_commit", reason="no_base_commit")
    cp = git("diff --stat", ["diff", "--stat", f"{base_commit}..{head_sha}"])
    if cp is None:
        return info
    info["diff_stat"] = (cp.stdout or "").rstrip("\n")
    cp = git("log", ["log", "--format=%H", f"{base_commit}..{head_sha}"])
    if cp is None:
        return info
    info["commit_count"] = len([ln for ln in (cp.stdout or "").splitlines() if ln.strip()])
    return info
