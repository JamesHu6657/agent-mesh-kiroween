"""Read-only bounded & redacted Git diff helpers for ``task.diff`` and ``project.diff`` (Batch 3 B1).

Security & governance invariants:
- Strictly read-only Git invocations via :func:`mesh.core.gitops.run_git`
  (``rev-parse``, ``show-ref``, ``merge-base``, ``status``, ``diff``, ``log``).
  Never calls push, rebase, reset, fetch, checkout, merge, commit, or update-ref.
- Verifies every worktree path with :func:`mesh.core.paths.resolve_trusted_path`
  and :func:`mesh.core.repos.is_registered_worktree` before executing Git.
- Never follows symlinks when previewing untracked files.
- Enforces strict file-count, per-file patch, and total payload byte caps.
- Redacts all paths, patches, and error text via :func:`mesh.core.redact.redact`
  *before* truncating so secret patterns are never sliced in half.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from mesh.core.gitops import run_git, run_git_bounded
from mesh.core.paths import resolve_trusted_path
from mesh.core.redact import redact, redact_value
from mesh.core.repos import integration_branch_name, is_registered_worktree
from mesh.platform import fs as _plat_fs
from mesh.platform import paths as _plat_paths

DEFAULT_MAX_DIFF_BYTES: int = 512 * 1024  # 512 KiB
MAX_DIFF_BYTES: int = 512 * 1024
MAX_FILE_PATCH_BYTES: int = 64 * 1024
MAX_DIFF_FILES: int = 200
MAX_UNTRACKED_FILE_BYTES: int = 32 * 1024
MAX_DIFF_METADATA_BYTES: int = 1024 * 1024  # 1 MiB cap for status/name-status/numstat/log
REDACT_LOOKAHEAD_BYTES: int = 4096  # Extra bytes read past cap so secrets straddling cap redact cleanly
DIFF_GIT_TIMEOUT_S: float = 15.0
DIFF_TOTAL_TIMEOUT_S: float = 20.0  # Cumulative wall-clock budget (< bridge 30s RPC timeout)


def _empty_stat() -> Dict[str, int]:
    return {"files_changed": 0, "additions": 0, "deletions": 0}


def _remaining_timeout(deadline: Optional[float], per_cmd_cap: float = DIFF_GIT_TIMEOUT_S) -> float:
    if deadline is None:
        return per_cmd_cap
    rem = deadline - time.monotonic()
    if rem <= 0.0:
        return 0.0
    return min(per_cmd_cap, rem)


def _safe_git(
    args: List[str],
    cwd: str,
    timeout_s: float = DIFF_GIT_TIMEOUT_S,
    *,
    deadline: Optional[float] = None,
) -> Optional[subprocess.CompletedProcess]:
    eff_timeout = _remaining_timeout(deadline, timeout_s)
    if eff_timeout <= 0.0:
        return None
    try:
        return run_git(args, cwd=cwd, timeout_s=eff_timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _safe_git_bounded(
    args: List[str],
    cwd: str,
    *,
    max_output_bytes: int,
    timeout_s: float = DIFF_GIT_TIMEOUT_S,
    deadline: Optional[float] = None,
) -> Tuple[bool, str, bool]:
    """Run read-only git with a strict stdout byte cap; return ``(ok, stdout, capped)``."""
    eff_timeout = _remaining_timeout(deadline, timeout_s)
    if eff_timeout <= 0.0:
        return False, "", False
    try:
        rc, out, capped = run_git_bounded(
            args, cwd=cwd, max_output_bytes=max_output_bytes, timeout_s=eff_timeout
        )
    except (subprocess.TimeoutExpired, OSError, AssertionError):
        return False, "", False
    return (rc == 0 or capped), out, capped


def _trim_to_last_nul(text: str, capped: bool) -> str:
    """When NUL-delimited git output was capped mid-record, drop the trailing partial record."""
    if not capped:
        return text
    idx = text.rfind("\0")
    return text[: idx + 1] if idx >= 0 else ""


def _clip_utf8_redacted(text: str, max_bytes: int) -> Tuple[str, bool]:
    """Redact *text* first, then bound to *max_bytes* UTF-8 bytes."""
    cleaned = redact(text)
    raw = cleaned.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return cleaned, False
    if max_bytes <= 0:
        return "", True
    clipped = raw[:max_bytes].decode("utf-8", errors="ignore")
    return clipped + "\n...[truncated]", True


def _parse_numstat(stdout: str) -> Dict[str, Tuple[int, int, bool]]:
    """Parse ``git diff --numstat -z`` output into ``path -> (add, del, is_binary)``."""
    out: Dict[str, Tuple[int, int, bool]] = {}
    tokens = stdout.split("\0")
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        parts = tok.split("\t", 2)
        if len(parts) < 3:
            continue
        add_s, del_s, path_field = parts
        is_bin = (add_s == "-" and del_s == "-")
        add_n = 0 if is_bin else (int(add_s) if add_s.isdigit() else 0)
        del_n = 0 if is_bin else (int(del_s) if del_s.isdigit() else 0)
        if path_field == "":
            # Rename/copy in -z format: empty path followed by old_path\0new_path\0
            if i + 1 < n:
                _old_p = tokens[i]
                new_p = tokens[i + 1]
                i += 2
                out[new_p] = (add_n, del_n, is_bin)
        else:
            out[path_field] = (add_n, del_n, is_bin)
    return out


def _parse_name_status(stdout: str) -> List[Tuple[str, str, Optional[str]]]:
    """Parse ``git diff --name-status -z`` output into ``[(status, path, old_path), ...]``."""
    out: List[Tuple[str, str, Optional[str]]] = []
    tokens = stdout.split("\0")
    i = 0
    n = len(tokens)
    while i < n:
        st_tok = tokens[i]
        i += 1
        if not st_tok:
            continue
        code = st_tok[0]
        if code in ("R", "C"):
            if i + 1 < n:
                old_p = tokens[i]
                new_p = tokens[i + 1]
                i += 2
                out.append((code, new_p, old_p))
        else:
            if i < n:
                p = tokens[i]
                i += 1
                out.append((code, p, None))
    return out


def _parse_porcelain_status(stdout: str) -> Tuple[bool, List[str]]:
    """Return ``(dirty, untracked_paths)`` from ``git status --porcelain=v1 -z -uall``."""
    dirty = False
    untracked: List[str] = []
    tokens = stdout.split("\0")
    i = 0
    n = len(tokens)
    while i < n:
        entry = tokens[i]
        i += 1
        if not entry or len(entry) < 3:
            continue
        xy = entry[:2]
        path = entry[3:]
        dirty = True
        if xy == "??":
            if path and not path.endswith("/"):
                untracked.append(path)
        elif xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            i += 1  # skip original path token
    return dirty, untracked


def _open_regular_beneath_nofollow(root_real: str, rel_path: str) -> Optional[int]:
    """Open *rel_path* beneath *root_real* without following any ancestor or leaf symlink.

    On POSIX, anchors at *root_real* and walks every path component with
    ``O_DIRECTORY | O_NOFOLLOW`` + ``dir_fd`` so an attacker racing inside a worktree
    cannot swap an ancestor directory for a symlink between validation and open.
    On Windows (``win32``), Python stdlib lacks ``dir_fd`` / relative-handle opens, so
    each ancestor is checked via ``is_reparse_link`` before opening the leaf via
    ``open_regular_nofollow``; this leaves a residual same-actor ancestor TOCTOU
    micro-window on Windows only (mitigated by read-only bounded <=32 KiB redacted output).
    """
    if not rel_path or "\x00" in rel_path:
        return None
    norm = rel_path.replace("\\", "/") if sys.platform == "win32" else rel_path
    if norm.startswith("/") or (sys.platform == "win32" and len(norm) >= 2 and norm[1] == ":"):
        return None
    parts = [p for p in norm.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    if sys.platform != "win32" and "\\" in rel_path:
        # Backslash is a valid filename char on POSIX; keep as single component if not path sep
        parts = [p for p in rel_path.split("/") if p]
        if not parts or any(p in (".", "..") for p in parts):
            return None

    full_path = os.path.join(root_real, *parts)
    trusted = resolve_trusted_path(full_path, root=root_real)
    if trusted is None:
        return None

    if (
        sys.platform != "win32"
        and os.open in getattr(os, "supports_dir_fd", set())
        and hasattr(os, "O_DIRECTORY")
    ):
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        cur_fd: Optional[int] = None
        try:
            cur_fd = os.open(root_real, dir_flags)
            for comp in parts[:-1]:
                next_fd = os.open(comp, dir_flags, dir_fd=cur_fd)
                os.close(cur_fd)
                cur_fd = next_fd
            fd = os.open(parts[-1], file_flags, dir_fd=cur_fd)
        except OSError:
            return None
        finally:
            if cur_fd is not None:
                try:
                    os.close(cur_fd)
                except OSError:
                    pass
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                return None
            nonblock = getattr(os, "O_NONBLOCK", 0)
            if nonblock:
                try:
                    import fcntl

                    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                    if fl & nonblock:
                        fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~nonblock)
                except OSError:
                    pass
            return fd
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return None

    # Windows fallback: check every ancestor component for reparse points + leaf open_regular_nofollow.
    # Security note (residual): win32 lacks dir_fd relative open in stdlib, leaving a narrow same-actor
    # ancestor TOCTOU micro-window; output remains strictly read-only, <=32 KiB, and redacted.
    cur = root_real
    for comp in parts:
        cur = os.path.join(cur, comp)
        if _plat_paths.is_reparse_link(cur):
            return None
    try:
        return _plat_fs.open_regular_nofollow(trusted, os.O_RDONLY, label="untracked file")
    except OSError:
        return None


def _read_untracked_preview(worktree_real: str, rel_path: str, byte_budget: int) -> Tuple[str, int, bool, bool]:
    """Read an untracked file safely (dir_fd/nofollow, inside worktree) and synthesize an added diff."""
    read_cap = min(MAX_UNTRACKED_FILE_BYTES, max(0, byte_budget))
    if read_cap <= 0:
        return "", 0, False, True

    fd = _open_regular_beneath_nofollow(worktree_real, rel_path)
    if fd is None:
        return "", 0, False, False
    try:
        with os.fdopen(fd, "rb") as bf:
            raw = bf.read(read_cap + 1)
    except OSError:
        return "", 0, False, False

    was_truncated = len(raw) > read_cap
    if was_truncated:
        raw = raw[:read_cap]
    if b"\x00" in raw:
        return "Binary file (untracked)", 0, True, False

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    additions = len(lines)
    header = f"--- /dev/null\n+++ b/{rel_path}\n@@ -0,0 +1,{additions} @@\n"
    body = "".join(f"+{ln}\n" for ln in lines)
    patch, clipped = _clip_utf8_redacted(header + body, min(MAX_FILE_PATCH_BYTES, byte_budget))
    return patch, additions, False, was_truncated or clipped


def _collect_diff_files(
    cwd: str,
    diff_spec_args: List[str],
    *,
    worktree_real: Optional[str],
    untracked_paths: List[str],
    max_bytes: int,
    deadline: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], bool, int, Optional[str]]:
    """Run bounded read-only git diff commands and assemble redacted per-file diff items."""
    base_flags = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-M"]

    ns_ok, ns_out, ns_capped = _safe_git_bounded(
        base_flags + ["--name-status", "-z"] + diff_spec_args,
        cwd,
        max_output_bytes=MAX_DIFF_METADATA_BYTES,
        deadline=deadline,
    )
    if not ns_ok:
        meta_error = (
            "diff_timeout"
            if (deadline is not None and _remaining_timeout(deadline) <= 0.0)
            else "git_diff_failed"
        )
        return [], _empty_stat(), True, 0, meta_error

    num_ok, num_out, num_capped = _safe_git_bounded(
        base_flags + ["--numstat", "-z"] + diff_spec_args,
        cwd,
        max_output_bytes=MAX_DIFF_METADATA_BYTES,
        deadline=deadline,
    )

    ns_items = _parse_name_status(_trim_to_last_nul(ns_out, ns_capped))
    num_map = _parse_numstat(_trim_to_last_nul(num_out, num_capped)) if num_ok else {}

    files: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()
    total_add = 0
    total_del = 0
    total_bytes = 0
    truncated = bool(ns_capped or num_capped or not num_ok)

    for status_code, path, old_path in ns_items:
        if len(files) >= MAX_DIFF_FILES:
            truncated = True
            break
        seen_paths.add(path)
        add_n, del_n, is_bin = num_map.get(path, (0, 0, False))
        total_add += add_n
        total_del += del_n

        file_truncated = False
        file_error: Optional[str] = None
        patch_text = ""
        if is_bin:
            patch_text = "Binary file changed"
        else:
            remaining = max(0, max_bytes - total_bytes)
            if remaining <= 0:
                file_truncated = True
                truncated = True
            elif deadline is not None and _remaining_timeout(deadline) <= 0.0:
                file_truncated = True
                truncated = True
                file_error = "timeout"
            else:
                cap = min(MAX_FILE_PATCH_BYTES, remaining)
                pathspecs = (
                    [f":(literal){old_path}", f":(literal){path}"]
                    if old_path
                    else [f":(literal){path}"]
                )
                p_ok, raw_patch, p_capped = _safe_git_bounded(
                    base_flags + ["--unified=3"] + diff_spec_args + ["--"] + pathspecs,
                    cwd,
                    max_output_bytes=cap + REDACT_LOOKAHEAD_BYTES,
                    deadline=deadline,
                )
                if p_ok and raw_patch:
                    patch_text, clipped = _clip_utf8_redacted(raw_patch, cap)
                    if p_capped and not clipped:
                        patch_text = patch_text + "\n...[truncated]"
                        clipped = True
                    if clipped or p_capped:
                        file_truncated = True
                        truncated = True
                    total_bytes += len(patch_text.encode("utf-8", errors="replace"))
                elif p_capped:
                    file_truncated = True
                    truncated = True
                elif not p_ok:
                    file_truncated = True
                    truncated = True
                    file_error = (
                        "timeout"
                        if (deadline is not None and _remaining_timeout(deadline) <= 0.0)
                        else "git_diff_failed"
                    )

        file_item: Dict[str, Any] = {
            "path": redact(path),
            "old_path": redact(old_path) if old_path else None,
            "status": status_code,
            "additions": add_n,
            "deletions": del_n,
            "binary": is_bin,
            "untracked": False,
            "truncated": file_truncated,
            "patch": patch_text,
        }
        if file_error is not None:
            file_item["error"] = file_error
        files.append(file_item)

    # Append untracked working-tree files (for live task worktree inspection)
    if worktree_real and untracked_paths:
        for rel_p in untracked_paths:
            if rel_p in seen_paths:
                continue
            if len(files) >= MAX_DIFF_FILES:
                truncated = True
                break
            u_error: Optional[str] = None
            if deadline is not None and _remaining_timeout(deadline) <= 0.0:
                patch_text, add_n, is_bin, f_trunc = "", 0, False, True
                u_error = "timeout"
            else:
                remaining = max(0, max_bytes - total_bytes)
                patch_text, add_n, is_bin, f_trunc = _read_untracked_preview(worktree_real, rel_p, remaining)
            total_add += add_n
            if patch_text:
                total_bytes += len(patch_text.encode("utf-8", errors="replace"))
            if f_trunc:
                truncated = True
            u_item: Dict[str, Any] = {
                "path": redact(rel_p),
                "old_path": None,
                "status": "?",
                "additions": add_n,
                "deletions": 0,
                "binary": is_bin,
                "untracked": True,
                "truncated": f_trunc,
                "patch": patch_text,
            }
            if u_error is not None:
                u_item["error"] = u_error
            files.append(u_item)

    stat = {
        "files_changed": len(files),
        "additions": total_add,
        "deletions": total_del,
    }
    return files, stat, truncated, total_bytes, None


def compute_task_diff(
    *,
    task_id: str,
    project_id: str,
    task_kind: str,
    workspace_path: Optional[str],
    workspace_real: Optional[str],
    branch: Optional[str],
    base_commit: Optional[str],
    repo_path: Optional[str],
    integ_wt: Optional[str],
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    include_untracked: bool = True,
    total_timeout_s: float = DIFF_TOTAL_TIMEOUT_S,
) -> Dict[str, Any]:
    """Compute a read-only, bounded, redacted diff for a code task."""
    budget = max(1024, min(int(max_bytes), MAX_DIFF_BYTES))
    deadline = time.monotonic() + max(0.0, float(total_timeout_s))
    base_result: Dict[str, Any] = {
        "available": False,
        "scope": "task",
        "task_id": task_id,
        "project_id": project_id,
        "branch": branch,
        "base_ref": base_commit,
        "base_commit": base_commit,
        "head_commit": None,
        "worktree_path": workspace_path,
        "worktree_exists": False,
        "dirty": False,
        "truncated": False,
        "total_bytes": 0,
        "stat": _empty_stat(),
        "files": [],
        "reason": None,
    }

    if task_kind != "code":
        base_result["reason"] = "not_code_task"
        return redact_value(base_result)
    if not repo_path:
        base_result["reason"] = "no_repo"
        return redact_value(base_result)

    trusted_repo = resolve_trusted_path(repo_path)
    if not trusted_repo or not os.path.isdir(trusted_repo):
        base_result["reason"] = "repo_missing"
        return redact_value(base_result)

    # Prefer live task worktree when present and verified as registered worktree of repo
    use_worktree = False
    if workspace_real and os.path.isdir(workspace_real) and os.path.lexists(os.path.join(workspace_real, ".git")):
        if is_registered_worktree(workspace_real, trusted_repo):
            use_worktree = True
        else:
            base_result["reason"] = "untrusted_worktree"
            return redact_value(base_result)

    if use_worktree and workspace_real:
        cwd = workspace_real
        base_result["worktree_exists"] = True
        head_cp = _safe_git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd, deadline=deadline)
        head_sha = (head_cp.stdout or "").strip() if (head_cp and head_cp.returncode == 0) else None
        base_result["head_commit"] = head_sha

        eff_base = base_commit
        if eff_base:
            ver_cp = _safe_git(
                ["rev-parse", "--verify", "--quiet", f"{eff_base}^{{commit}}"], cwd, deadline=deadline
            )
            if not (ver_cp and ver_cp.returncode == 0):
                eff_base = None
        if not eff_base:
            integ_ref = f"refs/heads/{integration_branch_name(project_id)}"
            mb_cp = _safe_git(["merge-base", "HEAD", integ_ref], cwd, deadline=deadline)
            if mb_cp and mb_cp.returncode == 0 and (mb_cp.stdout or "").strip():
                eff_base = (mb_cp.stdout or "").strip()
            else:
                eff_base = head_sha
        if not eff_base:
            base_result["reason"] = "diff_timeout" if _remaining_timeout(deadline) <= 0.0 else "no_base_commit"
            return redact_value(base_result)

        base_result["base_commit"] = eff_base
        base_result["base_ref"] = eff_base

        st_ok, st_out, st_capped = _safe_git_bounded(
            ["status", "--porcelain=v1", "-z", "-uall"],
            cwd,
            max_output_bytes=MAX_DIFF_METADATA_BYTES,
            deadline=deadline,
        )
        dirty, untracked = (
            _parse_porcelain_status(_trim_to_last_nul(st_out, st_capped)) if st_ok else (False, [])
        )
        base_result["dirty"] = dirty or st_capped

        files, stat, truncated, total_bytes, meta_error = _collect_diff_files(
            cwd,
            [eff_base],
            worktree_real=workspace_real if include_untracked else None,
            untracked_paths=untracked if include_untracked else [],
            max_bytes=budget,
            deadline=deadline,
        )
        if meta_error is not None:
            base_result.update(
                {
                    "available": False,
                    "truncated": True,
                    "total_bytes": 0,
                    "stat": _empty_stat(),
                    "files": [],
                    "reason": meta_error,
                }
            )
            return redact_value(base_result)
        base_result.update(
            {
                "available": True,
                "truncated": truncated or st_capped or not st_ok,
                "total_bytes": total_bytes,
                "stat": stat,
                "files": files,
                "reason": None,
            }
        )
        return redact_value(base_result)

    # Fallback when task worktree was pruned/not yet created: compare branch ref against base_commit
    fallback_cwd = integ_wt if (integ_wt and os.path.isdir(integ_wt)) else trusted_repo
    if not branch or not base_commit:
        base_result["reason"] = "workspace_missing"
        return redact_value(base_result)

    ref = f"refs/heads/{branch}"
    ref_cp = _safe_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], fallback_cwd, deadline=deadline)
    if not (ref_cp and ref_cp.returncode == 0 and (ref_cp.stdout or "").strip()):
        base_result["reason"] = "diff_timeout" if _remaining_timeout(deadline) <= 0.0 else "workspace_missing"
        return redact_value(base_result)

    head_sha = (ref_cp.stdout or "").strip()
    base_result["head_commit"] = head_sha
    ver_base = _safe_git(
        ["rev-parse", "--verify", "--quiet", f"{base_commit}^{{commit}}"], fallback_cwd, deadline=deadline
    )
    if not (ver_base and ver_base.returncode == 0):
        base_result["reason"] = "diff_timeout" if _remaining_timeout(deadline) <= 0.0 else "no_base_commit"
        return redact_value(base_result)

    files, stat, truncated, total_bytes, meta_error = _collect_diff_files(
        fallback_cwd,
        [f"{base_commit}..{ref}"],
        worktree_real=None,
        untracked_paths=[],
        max_bytes=budget,
        deadline=deadline,
    )
    if meta_error is not None:
        base_result.update(
            {
                "available": False,
                "truncated": True,
                "total_bytes": 0,
                "stat": _empty_stat(),
                "files": [],
                "reason": meta_error,
            }
        )
        return redact_value(base_result)
    base_result.update(
        {
            "available": True,
            "truncated": truncated,
            "total_bytes": total_bytes,
            "stat": stat,
            "files": files,
            "reason": None,
        }
    )
    return redact_value(base_result)


def compute_project_diff(
    *,
    project_id: str,
    repo_path: Optional[str],
    base_branch: Optional[str],
    base_commit: Optional[str],
    integration_branch: Optional[str],
    integ_wt: Optional[str],
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    total_timeout_s: float = DIFF_TOTAL_TIMEOUT_S,
) -> Dict[str, Any]:
    """Compute a read-only, bounded, redacted diff for a project's integration branch vs base branch."""
    budget = max(1024, min(int(max_bytes), MAX_DIFF_BYTES))
    deadline = time.monotonic() + max(0.0, float(total_timeout_s))
    integ_name = integration_branch or integration_branch_name(project_id)
    base_result: Dict[str, Any] = {
        "available": False,
        "scope": "project",
        "project_id": project_id,
        "branch": integ_name,
        "base_ref": base_branch or base_commit,
        "base_commit": base_commit,
        "head_commit": None,
        "worktree_path": integ_wt or repo_path,
        "worktree_exists": False,
        "commits_ahead": 0,
        "dirty": False,
        "truncated": False,
        "total_bytes": 0,
        "stat": _empty_stat(),
        "files": [],
        "reason": None,
    }

    if not repo_path:
        base_result["reason"] = "no_repo"
        return redact_value(base_result)

    trusted_repo = resolve_trusted_path(repo_path)
    if not trusted_repo or not os.path.isdir(trusted_repo):
        base_result["reason"] = "repo_missing"
        return redact_value(base_result)

    cwd = trusted_repo
    if integ_wt and os.path.isdir(integ_wt) and os.path.lexists(os.path.join(integ_wt, ".git")):
        if is_registered_worktree(integ_wt, trusted_repo):
            cwd = integ_wt
            base_result["worktree_exists"] = True

    integ_ref = f"refs/heads/{integ_name}"
    head_cp = _safe_git(["rev-parse", "--verify", "--quiet", f"{integ_ref}^{{commit}}"], cwd, deadline=deadline)
    if not (head_cp and head_cp.returncode == 0 and (head_cp.stdout or "").strip()):
        base_result["reason"] = "diff_timeout" if _remaining_timeout(deadline) <= 0.0 else "integration_branch_missing"
        return redact_value(base_result)
    head_sha = (head_cp.stdout or "").strip()
    base_result["head_commit"] = head_sha

    base_ref_spec: Optional[str] = None
    if base_branch:
        bref = f"refs/heads/{base_branch}"
        b_cp = _safe_git(["rev-parse", "--verify", "--quiet", f"{bref}^{{commit}}"], cwd, deadline=deadline)
        if b_cp and b_cp.returncode == 0 and (b_cp.stdout or "").strip():
            base_ref_spec = bref
            base_result["base_commit"] = (b_cp.stdout or "").strip()
            base_result["base_ref"] = base_branch
    if not base_ref_spec and base_commit:
        b_cp = _safe_git(["rev-parse", "--verify", "--quiet", f"{base_commit}^{{commit}}"], cwd, deadline=deadline)
        if b_cp and b_cp.returncode == 0:
            base_ref_spec = base_commit
            base_result["base_commit"] = base_commit
            base_result["base_ref"] = base_commit
    if not base_ref_spec:
        base_result["reason"] = "diff_timeout" if _remaining_timeout(deadline) <= 0.0 else "base_branch_missing"
        return redact_value(base_result)

    log_ok, log_out, log_capped = _safe_git_bounded(
        ["log", "--format=%H", f"{base_ref_spec}..{integ_ref}"],
        cwd,
        max_output_bytes=MAX_DIFF_METADATA_BYTES,
        deadline=deadline,
    )
    commits_ahead = (
        len([ln for ln in log_out.splitlines() if ln.strip()])
        if log_ok
        else 0
    )
    base_result["commits_ahead"] = commits_ahead

    files, stat, truncated, total_bytes, meta_error = _collect_diff_files(
        cwd,
        [f"{base_ref_spec}..{integ_ref}"],
        worktree_real=None,
        untracked_paths=[],
        max_bytes=budget,
        deadline=deadline,
    )
    if meta_error is not None:
        base_result.update(
            {
                "available": False,
                "truncated": True,
                "total_bytes": 0,
                "stat": _empty_stat(),
                "files": [],
                "reason": meta_error,
            }
        )
        return redact_value(base_result)
    base_result.update(
        {
            "available": True,
            "truncated": truncated or log_capped or not log_ok,
            "total_bytes": total_bytes,
            "stat": stat,
            "files": files,
            "reason": None,
        }
    )
    return redact_value(base_result)
