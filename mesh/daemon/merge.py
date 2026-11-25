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
import contextvars
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

import mesh.constants as c
from mesh.core.authz import check_authz
from mesh.core.clock import format_utc, now_iso_utc, parse_utc
from mesh.core.db import transaction
from mesh.core.events import record_event
from mesh.core.gitops import run_git
from mesh.core.paths import (
    get_install_root,
    is_site_packages_dir,
    make_owned_ancestors_writable,
    make_owned_dirs_writable,
    rmtree_allow_readonly,
)
from mesh.platform import fs as _plat_fs
from mesh.platform import paths as _plat_paths
from mesh.platform import perms as _plat_perms
from mesh.platform import proc as _plat_proc
from mesh.platform.fs import fsync_dir
from mesh.platform.proc import crash_self_now
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


#: R1 P1-1: run ids of merges whose pipeline job is still running in this meshd. Only
#: touched on the event loop: added right after TX1 commits (no await in between),
#: removed when the pipeline-and-final-TX job is done (including a held close gate).
_ACTIVE_MERGE_RUNS: Set[str] = set()


def is_merge_run_active(run_id: str) -> bool:
    """True while the merge pipeline of ``run_id`` still runs in this process."""
    with _ACTIVE_TEST_LOCK:
        return run_id in _ACTIVE_MERGE_RUNS


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
            if not _plat_paths.same_path(prj["repo_path"], target):
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
    from mesh.core.workspaces import has_project_any_ws_reservation, has_task_ws_reservation
    res_key = has_project_any_ws_reservation(ctx.conn, project_id) or has_task_ws_reservation(
        ctx.conn, task_id, project_id
    )
    if res_key:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot merge task '{task_id}': workspace operation '{res_key}' is in progress",
            details={"reservation": res_key, "task_id": task_id, "project_id": project_id},
        )
    key = merge_lease_key(project_id)
    held = ctx.conn.execute("SELECT key, run_id FROM leases WHERE key = ?;", (key,)).fetchone()
    if held:
        holder_task = ctx.conn.execute("SELECT task_id FROM runs WHERE id = ?;", (held["run_id"],)).fetchone()
        raise MeshError(code=c.CONCURRENCY_LIMIT, exit_code=c.EXIT_BUSY,
                        message=f"Another merge is running in project '{project_id}'",
                        details={"lease": key, "run_id": held["run_id"],
                                 "task_id": holder_task["task_id"] if holder_task else None})
    # R1 P1-1: a task left `merging` without the lease (e.g. a forced release whose
    # follow-up recovery failed) still owns the integration tree until it is decided.
    unsettled = ctx.conn.execute(
        "SELECT id FROM tasks WHERE project_id = ? AND status = 'merging' AND id != ? ORDER BY id LIMIT 1;",
        (project_id, task_id),
    ).fetchone()
    if unsettled is not None:
        raise MeshError(code=c.CONCURRENCY_LIMIT, exit_code=c.EXIT_BUSY,
                        message=f"Another merge is still unsettled in project '{project_id}'",
                        details={"lease": None, "run_id": None, "task_id": unsettled["id"],
                                 "reason": "merge_unsettled"})
    if project is not None and project["repo_path"]:
        busy = merge_in_progress_for_repo(ctx.conn, project["repo_path"])
        if busy is not None:
            raise MeshError(
                code=c.CONCURRENCY_LIMIT,
                exit_code=c.EXIT_BUSY,
                message=f"Another merge is running on repository '{project['repo_path']}' (project '{busy['project_id']}')",
                details={
                    "lease": busy["lease"],
                    "run_id": busy["run_id"],
                    "task_id": busy["task_id"],
                    "merge_project_id": busy["project_id"],
                    "reason": "merge_in_progress",
                },
            )
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


def reviewed_head(deliverable_json: Optional[str]) -> Optional[str]:
    """Full review r1 P0-1: the commit Mesh recorded at submit (`deliverable.git.head`),
    i.e. the work that was reviewed and accepted; None when it was not recorded."""
    if not deliverable_json:
        return None
    try:
        rec = json.loads(deliverable_json)
        git = ((rec.get("deliverable") or {}).get("git") or {})
        head = git.get("head")
    except (TypeError, ValueError, AttributeError):
        return None
    if isinstance(head, str) and len(head) in (40, 64) and all(ch in "0123456789abcdef" for ch in head):
        return head
    return None


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
        "expected_head": reviewed_head(task["deliverable_json"]),
        "worktree": task["workspace_path"],
        "repo": project["repo_path"],
        "ws_root": ws_root,
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


def _git_path(cwd: str, name: str, step: str = "rev-parse git-path") -> Optional[str]:
    try:
        cp = _git(step, ["rev-parse", "--git-path", name], cwd)
    except _GitFailed:
        return None
    if cp.returncode != 0:
        return None
    raw = (cp.stdout or "").strip()
    if not raw:
        return None
    return raw if os.path.isabs(raw) else os.path.join(cwd, raw)


def _head_symref(cwd: str, step: str = "rev-parse HEAD symref") -> Optional[str]:
    try:
        cp = _git(step, ["rev-parse", "--symbolic-full-name", "HEAD"], cwd)
    except _GitFailed:
        return None
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip() or None


_CE_VALID = 0x8000
_CE_EXTENDED = 0x4000
_CE_SKIP_WORKTREE = 0x4000
_CE_INTENT_TO_ADD = 0x2000


def _unlink_nonregular(path: str) -> None:
    """Unlink a file, POSIX symlink, or Windows symlink/NTFS directory junction."""
    _plat_fs.unlink_nonregular(path)


def _atomic_write_bytes(path: str, data: bytes, mode: int = 0o600) -> None:
    parent = os.path.dirname(path)
    if not parent or not os.path.isdir(parent):
        return
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{os.path.basename(path)}_")
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        if not (sys.platform == "win32" and (mode & 0o077) == 0):
            os.chmod(tmp, mode)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    if sys.platform == "win32" and (mode & 0o077) == 0:
        _plat_perms.set_private_file_acl(tmp, mode)
    _plat_fs.atomic_replace(tmp, path)


_INDEX_LINK_EXT = b"link"


def _scan_index_bytes(
    data: bytes, *, clear_hide_flags: bool = False
) -> Optional[Dict[str, Any]]:
    """Walk a DIRC v2/v3/v4 index: entries, then the extension area, fail closed.

    Returns ``None`` on any framing/checksum error, otherwise a dict with
    ``flags`` (path -> (assume_unchanged, skip_worktree, intent_to_add)),
    ``unnamed`` / ``unnamed_hidden`` (split-index replacement entries carry an
    empty name), ``exts`` (list of ``(sig, payload)``), ``hash_len`` and, when
    ``clear_hide_flags`` changed anything, the rewritten ``buf``.
    """
    if len(data) < 12 or data[:4] != b"DIRC":
        return None
    ver, num_entries = struct.unpack_from(">II", data, 4)
    if ver not in (2, 3, 4):
        return None
    if len(data) >= 32 and hashlib.sha1(data[:-20]).digest() == data[-20:]:
        hash_len = 20
    elif len(data) >= 44 and hashlib.sha256(data[:-32]).digest() == data[-32:]:
        hash_len = 32
    else:
        return None
    body_end = len(data) - hash_len
    pos = 12
    prev_name = b""
    out: Dict[str, Tuple[bool, bool, bool]] = {}
    buf: Optional[bytearray] = None
    modified = False
    unnamed = False
    unnamed_hidden = False
    entries: List[Tuple[bytes, bool, bool, bool]] = []
    for _ in range(num_entries):
        flags_off = pos + 40 + hash_len
        if flags_off + 2 > body_end:
            return None
        flags = struct.unpack_from(">H", data, flags_off)[0]
        has_ext = ver >= 3 and bool(flags & _CE_EXTENDED)
        if has_ext:
            ext_off = flags_off + 2
            if ext_off + 2 > body_end:
                return None
            ext_flags = struct.unpack_from(">H", data, ext_off)[0]
            name_start = ext_off + 2
        else:
            ext_off = None
            ext_flags = 0
            name_start = flags_off + 2
        if ver < 4:
            nul = data.find(b"\x00", name_start, body_end)
            if nul < 0:
                return None
            name_bytes = bytes(data[name_start:nul])
            entry_len = nul - pos + 1
            pos += (entry_len + 7) & ~7
        else:
            p = name_start
            if p >= body_end:
                return None
            b = data[p]
            p += 1
            strip_len = b & 0x7F
            while b & 0x80:
                if p >= body_end:
                    return None
                strip_len += 1
                b = data[p]
                p += 1
                strip_len = (strip_len << 7) | (b & 0x7F)
            if strip_len > len(prev_name):
                return None
            nul = data.find(b"\x00", p, body_end)
            if nul < 0:
                return None
            keep_len = len(prev_name) - strip_len
            name_bytes = (prev_name[:keep_len] if keep_len else b"") + bytes(data[p:nul])
            prev_name = name_bytes
            pos = nul + 1
        if pos > body_end:
            return None
        au = bool(flags & _CE_VALID)
        sw = bool(ext_flags & _CE_SKIP_WORKTREE)
        ita = bool(ext_flags & _CE_INTENT_TO_ADD)
        entries.append((name_bytes, au, sw, ita))
        if not name_bytes:
            # Split-index replacement entry: its path lives in sharedindex.<sha>.
            unnamed = True
            if au or sw or ita:
                unnamed_hidden = True
            continue
        if au or sw or ita:
            rel = name_bytes.decode("utf-8", errors="surrogateescape")
            prev_vals = out.get(rel)
            if prev_vals is None:
                out[rel] = (au, sw, ita)
            else:
                out[rel] = (prev_vals[0] or au, prev_vals[1] or sw, prev_vals[2] or ita)
        if clear_hide_flags and (au or sw):
            if buf is None:
                buf = bytearray(data)
            if au:
                struct.pack_into(">H", buf, flags_off, flags & ~_CE_VALID)
            if sw and ext_off is not None:
                struct.pack_into(">H", buf, ext_off, ext_flags & ~_CE_SKIP_WORKTREE)
            modified = True
    # Extension area: <4-byte sig><4-byte size><payload>, exactly up to the trailer.
    exts: List[Tuple[bytes, bytes]] = []
    while pos < body_end:
        if pos + 8 > body_end:
            return None
        sig = bytes(data[pos:pos + 4])
        ext_size = struct.unpack_from(">I", data, pos + 4)[0]
        if pos + 8 + ext_size > body_end:
            return None
        exts.append((sig, bytes(data[pos + 8:pos + 8 + ext_size])))
        pos += 8 + ext_size
    if pos != body_end:
        return None
    return {
        "flags": out,
        "modified": modified,
        "buf": buf,
        "hash_len": hash_len,
        "body_end": body_end,
        "exts": exts,
        "unnamed": unnamed,
        "unnamed_hidden": unnamed_hidden,
        "entries": entries,
    }


def _ewah_decode(data: bytes, pos: int) -> Optional[Tuple[Set[int], int]]:
    """Decode one git EWAH bitmap at ``pos``; return ``(set_bits, next_pos)`` or ``None``."""
    if pos + 8 > len(data):
        return None
    bit_size, word_count = struct.unpack_from(">II", data, pos)
    pos += 8
    if pos + 8 * word_count + 4 > len(data):
        return None
    words = struct.unpack_from(f">{word_count}Q", data, pos) if word_count else ()
    pos += 8 * word_count + 4  # words, then the RLW position
    bits: Set[int] = set()
    bit = 0
    i = 0
    while i < word_count:
        rlw = words[i]
        running_bit = rlw & 1
        run_len = (rlw >> 1) & 0xFFFFFFFF
        lit_count = (rlw >> 33) & 0x7FFFFFFF
        if running_bit:
            if run_len * 64 > bit_size + 64:
                return None
            bits.update(range(bit, bit + run_len * 64))
        bit += run_len * 64
        if i + 1 + lit_count > word_count:
            return None
        for j in range(lit_count):
            w = words[i + 1 + j]
            while w:
                low = w & -w
                bits.add(bit + low.bit_length() - 1)
                w ^= low
            bit += 64
        i += 1 + lit_count
    return {b for b in bits if b < bit_size}, pos


def _is_optional_index_ext(sig: bytes) -> bool:
    return len(sig) == 4 and 0x41 <= sig[0] <= 0x5A


def _index_link_shared_name(index_path: str) -> Optional[str]:
    """``sharedindex.<hex>`` named by ``index_path``'s split-index ``link`` extension, if any."""
    try:
        with open(index_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return _index_bytes_shared_name(data)


def _index_bytes_shared_name(data: Optional[bytes]) -> Optional[str]:
    """``sharedindex.<hex>`` named by index bytes' ``link`` extension (None if unsplit/corrupt)."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    scan = _scan_index_bytes(bytes(data))
    if scan is None:
        return None
    for sig, payload in scan["exts"]:
        if sig == _INDEX_LINK_EXT and len(payload) >= scan["hash_len"]:
            return "sharedindex." + payload[: scan["hash_len"]].hex()
    return None


def _parse_index_flags(
    index_path: str, *, clear_hide_flags: bool = False
) -> Tuple[Optional[Dict[str, Tuple[bool, bool, bool]]], bool]:
    """Parse DIRC v2/v3/v4 index entry hide/intent flags, optionally clearing hide flags in place.

    Returns ``(flags_by_path, modified)`` where each value is
    ``(assume_unchanged, skip_worktree, intent_to_add)``, or ``(None, False)`` on
    unreadable/corrupt/unsupported index files.

    The extension area is walked too (review PR45 R5 P1): any non-optional
    extension other than split-index ``link`` (e.g. sparse-index ``sdir``), bad
    extension framing, or a nameless entry outside a split index fails closed.
    For ``link`` the flags are resolved against ``sharedindex.<sha>`` (delete and
    replace EWAH bitmaps applied); a missing/corrupt shared index fails closed, and
    so does a clear request — the shared file's name is its checksum, so callers
    restore ``pre_merge.index`` instead of rewriting it.
    """
    if not index_path or not os.path.lexists(index_path):
        return {}, False
    if _plat_paths.is_reparse_link(index_path) or not os.path.isfile(index_path):
        return None, False
    try:
        st_mode = (os.lstat(index_path).st_mode & 0o777) or 0o644
        data = _plat_fs.read_bytes_shared(index_path)
    except OSError:
        return None, False
    scan = _scan_index_bytes(data, clear_hide_flags=clear_hide_flags)
    if scan is None:
        return None, False
    link_payload: Optional[bytes] = None
    for sig, payload in scan["exts"]:
        if _is_optional_index_ext(sig):
            continue
        if sig == _INDEX_LINK_EXT and link_payload is None:
            link_payload = payload
            continue
        return None, False
    out: Dict[str, Tuple[bool, bool, bool]] = dict(scan["flags"])
    if link_payload is None:
        if scan["unnamed"]:
            return None, False
    else:
        merged = None if clear_hide_flags else _split_index_flags(index_path, scan, link_payload)
        if merged is None:
            return None, False
        return merged, False
    buf = scan["buf"]
    modified = bool(scan["modified"])
    if clear_hide_flags and modified and buf is not None:
        body_end = scan["body_end"]
        digest = (
            hashlib.sha1(buf[:body_end]).digest()
            if scan["hash_len"] == 20
            else hashlib.sha256(buf[:body_end]).digest()
        )
        buf[body_end:] = digest
        try:
            _atomic_write_bytes(index_path, bytes(buf), mode=st_mode)
        except OSError:
            return None, False
    return out, modified


def _split_index_flags(
    index_path: str, scan: Dict[str, Any], link_payload: bytes
) -> Optional[Dict[str, Tuple[bool, bool, bool]]]:
    """Resolve hide flags of a split index: ``sharedindex.<sha>`` minus the ``link``
    delete bitmap, with replace-bitmap entries taking the main index's (nameless)
    replacement flags, plus the main index's own new entries. Fails closed."""
    hash_len = scan["hash_len"]
    if len(link_payload) < hash_len:
        return None
    shared_hex = link_payload[:hash_len].hex()
    deleted: Set[int] = set()
    replaced: List[int] = []
    if len(link_payload) > hash_len:
        dec = _ewah_decode(link_payload, hash_len)
        if dec is None:
            return None
        deleted, nxt = dec
        dec = _ewah_decode(link_payload, nxt)
        if dec is None or dec[1] != len(link_payload):
            return None
        replaced = sorted(dec[0])
    unnamed = [e for e in scan["entries"] if not e[0]]
    if len(unnamed) != len(replaced):
        return None
    out: Dict[str, Tuple[bool, bool, bool]] = {}

    def _add(name: bytes, au: bool, sw: bool, ita: bool) -> None:
        if not (au or sw or ita):
            return
        rel = name.decode("utf-8", errors="surrogateescape")
        prev = out.get(rel)
        out[rel] = (au, sw, ita) if prev is None else (prev[0] or au, prev[1] or sw, prev[2] or ita)

    if not shared_hex.strip("0"):
        if replaced or deleted:
            return None
    else:
        shared_path = os.path.join(os.path.dirname(index_path), "sharedindex." + shared_hex)
        if _plat_paths.is_reparse_link(shared_path) or not os.path.isfile(shared_path):
            return None
        try:
            sdata = _plat_fs.read_bytes_shared(shared_path)
        except OSError:
            return None
        sscan = _scan_index_bytes(sdata)
        if sscan is None or sscan["unnamed"] or sdata[-hash_len:].hex() != shared_hex:
            return None
        if any(not _is_optional_index_ext(sig) for sig, _p in sscan["exts"]):
            return None
        shared = sscan["entries"]
        if any(i >= len(shared) for i in deleted) or any(i >= len(shared) for i in replaced):
            return None
        repl_by_pos = dict(zip(replaced, unnamed))
        for i, (name, au, sw, ita) in enumerate(shared):
            if i in deleted:
                continue
            r = repl_by_pos.get(i)
            if r is not None:
                _add(name, r[1], r[2], r[3])
            else:
                _add(name, au, sw, ita)
    for name, au, sw, ita in scan["entries"]:
        if name:
            _add(name, au, sw, ita)
    return out


def _cleanup_stray_shared_indexes(integ: str) -> None:
    """Remove ``sharedindex.*`` files the current index does not reference (review PR45 R5 P1).

    A test may split the index; after ``pre_merge.index`` is restored the shared
    files it wrote are stale. The one the restored index links to (a repo that
    legitimately runs ``core.splitIndex``) is kept.
    """
    idx_path = _git_path(integ, "index", "rev-parse index path for sharedindex cleanup")
    if not idx_path:
        return
    gitdir = os.path.dirname(idx_path)
    keep = _index_link_shared_name(idx_path) if os.path.isfile(idx_path) else None
    try:
        names = os.listdir(gitdir)
    except OSError:
        return
    for name in names:
        if not name.startswith("sharedindex.") or name == keep:
            continue
        path = os.path.join(gitdir, name)
        try:
            if _plat_paths.is_reparse_link(path) or os.path.isfile(path):
                _unlink_nonregular(path)
        except OSError:
            pass


def _clear_merge_state_files(integ: str) -> None:
    """Drop MERGE_HEAD/MERGE_MSG/MERGE_MODE/AUTO_MERGE when `merge --abort` cannot (review PR45 R5 P1)."""
    for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "AUTO_MERGE"):
        path = _git_path(integ, name, f"rev-parse {name} path")
        if not path:
            continue
        try:
            if _plat_paths.is_reparse_link(path) or os.path.isfile(path):
                _unlink_nonregular(path)
        except OSError:
            pass


def _read_index_flags(
    cwd: str, step: str = "read index flags"
) -> Optional[Dict[str, Tuple[bool, bool, bool]]]:
    idx_path = _git_path(cwd, "index", step)
    if not idx_path:
        return None
    flags, _modified = _parse_index_flags(idx_path, clear_hide_flags=False)
    return flags


def _clear_index_hide_flags(cwd: str, step: str = "clear index hide flags") -> bool:
    idx_path = _git_path(cwd, "index", step)
    if not idx_path:
        return False
    flags, _modified = _parse_index_flags(idx_path, clear_hide_flags=True)
    return flags is not None


def _dirty_paths(cwd: str, step: str) -> List[str]:
    idx_flags = _read_index_flags(cwd, step)
    flag_dirty = (
        [".git/index"]
        if idx_flags is None
        else [rel for rel, (au, sw, ita) in sorted(idx_flags.items()) if au or sw or ita]
    )
    cp = _git(step, ["status", "--porcelain=v1", "--untracked-files=all"], cwd)
    if cp.returncode != 0:
        tampered = _tampered_gitlink_paths(cwd, _gitlink_paths(cwd, refs=("HEAD",), include_index=True))
        if tampered:
            return sorted(set(tampered) | set(_fallback_diff_paths(cwd)) | set(flag_dirty))
        raise _GitFailed(step, cp)
    from mesh.core.repos import _porcelain_paths

    paths = _porcelain_paths(cp.stdout)
    seen = set(paths)
    for rel in flag_dirty:
        if rel not in seen:
            paths.append(rel)
            seen.add(rel)
    return paths


_PROBE_EXPECTED: contextvars.ContextVar[Optional[Tuple[Optional[str], Optional[str]]]] = (
    contextvars.ContextVar("_PROBE_EXPECTED", default=None)
)
_RECOVERY_EXPECTED: contextvars.ContextVar[Optional[Tuple[Optional[str], Optional[str]]]] = (
    contextvars.ContextVar("_RECOVERY_EXPECTED", default=None)
)


def _verify_integration_worktree(
    integ: str,
    repo: Optional[str],
    ws_root: Optional[str] = None,
) -> bool:
    """Verify ``integ`` is a trusted, non-symlinked registered worktree of ``repo``."""
    from mesh.core.paths import resolve_trusted_path
    from mesh.core.repos import is_registered_worktree

    integ_real = resolve_trusted_path(integ, root=ws_root)
    if integ_real is None or not os.path.isdir(integ_real):
        return False
    return not repo or is_registered_worktree(integ_real, repo, root=ws_root)


def _merge_head_present(cwd: str) -> bool:
    cp = _git("verify MERGE_HEAD", ["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd)
    return cp.returncode == 0


def _merge_head_exists(cwd: str) -> bool:
    if _merge_head_present(cwd):
        return True
    mh_path = _git_path(cwd, "MERGE_HEAD", "rev-parse MERGE_HEAD path")
    return bool(mh_path and os.path.lexists(mh_path))


def _merge_head_matches_tip(cwd: str, expected_tip: str) -> bool:
    """Verify MERGE_HEAD is a regular file containing exclusively ``expected_tip``."""
    if not expected_tip:
        return False
    mh_path = _git_path(cwd, "MERGE_HEAD", "rev-parse MERGE_HEAD path")
    if not mh_path or _plat_paths.is_reparse_link(mh_path) or not os.path.isfile(mh_path):
        return False
    try:
        fd = _plat_fs.open_regular_nofollow(mh_path, os.O_RDONLY, label="MERGE_HEAD")
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False
        chunks: List[bytes] = []
        total = 0
        while total <= 4096:
            chunk = os.read(fd, 4097 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > 4096:
            return False
        raw = b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    finally:
        os.close(fd)
    shas = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return shas == [expected_tip] and _merge_head_present(cwd)


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
    expected = _PROBE_EXPECTED.get()
    if expected is not None:
        repo, ws_root = expected
        if not _verify_integration_worktree(integration, repo, ws_root):
            return None
    else:
        from mesh.core.paths import resolve_trusted_path

        integ_real = resolve_trusted_path(integration)
        if integ_real is None or not os.path.isdir(integ_real):
            return None
    try:
        return _head(integration, "rev-parse integration HEAD")
    except _GitFailed:
        return None


def _untracked_paths(cwd: str, step: str = "untracked status") -> List[str]:
    """Return unignored untracked relative paths inside ``cwd`` via allow-listed git status."""
    cp = _git(step, ["status", "--porcelain=v1", "--untracked-files=all", "-z"], cwd)
    if cp.returncode != 0:
        raise _GitFailed(step, cp)
    entries = (cp.stdout or "").split("\0")
    out: List[str] = []
    i = 0
    while i < len(entries):
        rec = entries[i]
        i += 1
        if not rec or len(rec) < 4:
            continue
        code = rec[:2]
        rel = rec[3:]
        if code[0] in ("R", "C"):
            i += 1
        if code == "??":
            out.append(rel)
    return out


def _safe_unlink_untracked(
    integ: str,
    rel: str,
    protected_dirs: Optional[Set[str]] = None,
) -> None:
    """Remove a single untracked entry created inside ``integ`` without following symlinks."""
    if sys.platform == "win32":
        import ntpath

        rel_clean = (rel or "").rstrip("\\/")
        if (
            not rel_clean
            or rel_clean.startswith(("/", "\\"))
            or ntpath.isabs(rel_clean)
            or ntpath.splitdrive(rel_clean)[0]
        ):
            return
        parts = _plat_paths.split_path_segments(rel_clean)
    else:
        rel_clean = (rel or "").rstrip("/")
        if not rel_clean or os.path.isabs(rel_clean):
            return
        parts = [p for p in rel_clean.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return
    integ_real = os.path.realpath(integ)
    cur = integ
    for part in parts[:-1]:
        cur = os.path.join(cur, part)
        if _plat_paths.is_reparse_link(cur):
            return
        if not _plat_paths.is_path_equal_or_parent(integ_real, cur):
            return
    target = os.path.join(cur, parts[-1])
    if not _plat_paths.is_path_equal_or_parent(integ_real, os.path.dirname(target)):
        return
    if _plat_paths.is_reparse_link(target) or os.path.isfile(target):
        try:
            _unlink_nonregular(target)
        except PermissionError:
            if not make_owned_ancestors_writable(target, integ_real):
                raise
            _unlink_nonregular(target)
    elif os.path.isdir(target):
        try:
            rmtree_allow_readonly(target, boundary_real=integ_real)
        except PermissionError:
            if not make_owned_ancestors_writable(target, integ_real):
                raise
            rmtree_allow_readonly(target, boundary_real=integ_real)
    elif os.path.lexists(target):
        try:
            _unlink_nonregular(target)
        except PermissionError:
            if not make_owned_ancestors_writable(target, integ_real):
                raise
            _unlink_nonregular(target)
    else:
        return
    protected_norm = {p.strip("/") for p in (protected_dirs or ()) if p and p.strip("/")}
    parent = os.path.dirname(target)
    while parent and not _plat_paths.same_path(parent, integ):
        if (
            _plat_paths.same_path(parent, integ_real)
            or not _plat_paths.is_path_equal_or_parent(integ_real, parent)
            or _plat_paths.is_reparse_link(parent)
        ):
            break
        rel_parent = os.path.relpath(parent, integ).replace(os.sep, "/").strip("/")
        if rel_parent in protected_norm:
            break
        try:
            os.rmdir(parent)
        except OSError:
            break
        parent = os.path.dirname(parent)


PRE_UNTRACKED_NAME = "pre_untracked.json"
PRE_EMPTY_DIRS_NAME = "pre_empty_dirs.json"
PRE_MERGE_NAME = "pre_merge.json"
PRE_MERGE_INDEX_NAME = "pre_merge.index"
#: Copy of the ``sharedindex.<sha>`` the pre-merge index links to (split index;
#: review PR45 R5 second review P1): git may expire the shared file during the
#: test, and restoring ``pre_merge.index`` alone would leave a dangling link.
PRE_MERGE_SHARED_NAME = "pre_merge.sharedindex"
COMMIT_READY_NAME = "commit_ready.json"


def _pre_untracked_path(run_dir: Optional[str]) -> Optional[str]:
    if not run_dir or not isinstance(run_dir, str):
        return None
    return os.path.join(run_dir, PRE_UNTRACKED_NAME)


def _pre_empty_dirs_path(run_dir: Optional[str]) -> Optional[str]:
    if not run_dir or not isinstance(run_dir, str):
        return None
    return os.path.join(run_dir, PRE_EMPTY_DIRS_NAME)


def _run_dir_file(run_dir: Optional[str], filename: str) -> Optional[str]:
    if not run_dir or not isinstance(run_dir, str):
        return None
    return os.path.join(run_dir, filename)


def _atomic_write_json(path: Optional[str], payload: Any, mode: int = 0o600) -> None:
    if not path or not os.path.isdir(os.path.dirname(path)):
        return
    try:
        data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write_bytes(path, data, mode=mode)
    except OSError:
        pass


def _save_pre_merge_state(
    run_dir: Optional[str],
    *,
    task_id: str,
    pre_head: str,
    tip: str,
    symref: Optional[str],
    head_raw: Optional[str],
    index_bytes: Optional[bytes],
    shared_bytes: Optional[bytes] = None,
) -> None:
    _atomic_write_json(
        _run_dir_file(run_dir, PRE_MERGE_NAME),
        {
            "task_id": task_id,
            "pre_head": pre_head,
            "tip": tip,
            "symref": symref,
            "head_raw": head_raw,
        },
    )
    idx_path = _run_dir_file(run_dir, PRE_MERGE_INDEX_NAME)
    if idx_path and isinstance(index_bytes, (bytes, bytearray)) and os.path.isdir(os.path.dirname(idx_path)):
        try:
            _atomic_write_bytes(idx_path, bytes(index_bytes), mode=0o600)
        except OSError:
            pass
    sh_path = _run_dir_file(run_dir, PRE_MERGE_SHARED_NAME)
    if sh_path and isinstance(shared_bytes, (bytes, bytearray)) and os.path.isdir(os.path.dirname(sh_path)):
        try:
            _atomic_write_bytes(sh_path, bytes(shared_bytes), mode=0o600)
        except OSError:
            pass


def _load_pre_merge_shared(run_dir: Optional[str]) -> Optional[bytes]:
    path = _run_dir_file(run_dir, PRE_MERGE_SHARED_NAME)
    if not path or not os.path.isfile(path):
        return None
    try:
        return _plat_fs.read_bytes_shared(path)
    except OSError:
        return None


def _load_pre_merge_state(run_dir: Optional[str]) -> Optional[Dict[str, Any]]:
    path = _run_dir_file(run_dir, PRE_MERGE_NAME)
    return _read_json(path) if path and os.path.isfile(path) else None


def _load_pre_merge_index(run_dir: Optional[str]) -> Optional[bytes]:
    path = _run_dir_file(run_dir, PRE_MERGE_INDEX_NAME)
    if not path or not os.path.isfile(path):
        return None
    try:
        return _plat_fs.read_bytes_shared(path)
    except OSError:
        return None


def _save_commit_ready(
    run_dir: Optional[str],
    *,
    task_id: str,
    pre_head: str,
    tip: str,
    symref: Optional[str],
    message: str,
) -> None:
    """Carbon P1-3: durably record ``(pre_head, tip)`` before the merge commit is made.

    Temp file, fsync, rename, directory fsync: a crash right after ``git commit`` finds it,
    and recovery reconciles the commit by its parents. Raises OSError when the record cannot
    be made durable (or there is no run dir); the caller then aborts instead of committing.
    """
    path = _run_dir_file(run_dir, COMMIT_READY_NAME)
    if not path or not os.path.isdir(os.path.dirname(path)):
        raise OSError(f"no run directory for {COMMIT_READY_NAME}")
    parent = os.path.dirname(path)
    data = (json.dumps({
        "task_id": task_id,
        "pre_head": pre_head,
        "tip": tip,
        "symref": symref,
        "message": message,
    }, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{COMMIT_READY_NAME}_")
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            if sys.platform != "win32" and hasattr(os, "fchmod"):
                _plat_perms.fchmod_private(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        if sys.platform == "win32" or not hasattr(os, "fchmod"):
            _plat_perms.set_private_file_acl(tmp, 0o600)
        _plat_fs.atomic_replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(parent)


def _load_commit_ready(run_dir: Optional[str]) -> Optional[Dict[str, Any]]:
    path = _run_dir_file(run_dir, COMMIT_READY_NAME)
    return _read_json(path) if path and os.path.isfile(path) else None


def _unlink_commit_ready(run_dir: Optional[str]) -> None:
    path = _run_dir_file(run_dir, COMMIT_READY_NAME)
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _has_pipeline_snapshot(run_dir: Optional[str]) -> bool:
    for fname in (PRE_MERGE_NAME, PRE_MERGE_INDEX_NAME, PRE_UNTRACKED_NAME, PRE_EMPTY_DIRS_NAME):
        p = _run_dir_file(run_dir, fname)
        if p and os.path.isfile(p):
            return True
    return False


def _save_pre_untracked(run_dir: Optional[str], pre_untracked: Set[str]) -> None:
    path = _pre_untracked_path(run_dir)
    if not path or not os.path.isdir(os.path.dirname(path)):
        return
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".pre_untracked_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(sorted(pre_untracked), f)
            if sys.platform == "win32":
                _plat_perms.set_private_file_acl(tmp, 0o600)
            _plat_fs.atomic_replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass


def _load_pre_untracked(run_dir: Optional[str]) -> Optional[Set[str]]:
    path = _pre_untracked_path(run_dir)
    if not path or not os.path.isfile(path):
        return None
    try:
        data = json.loads(_plat_fs.read_bytes_shared(path).decode("utf-8"))
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return set(data)
    except (OSError, ValueError, TypeError):
        pass
    return None


def _snapshot_empty_dirs(integ: str) -> Set[str]:
    empty: Set[str] = set()
    if not os.path.isdir(integ):
        return empty
    integ_real = os.path.realpath(integ)
    for dirpath, dirnames, filenames in os.walk(integ, topdown=True, followlinks=False):
        has_entries = bool(dirnames or filenames)
        dirnames[:] = sorted(
            d for d in dirnames
            if d != ".git" and not _plat_paths.is_reparse_link(os.path.join(dirpath, d))
        )
        if _plat_paths.same_path(dirpath, integ) or has_entries:
            continue
        if _plat_paths.is_reparse_link(dirpath):
            continue
        if not _plat_paths.same_path(dirpath, integ_real) and _plat_paths.is_path_equal_or_parent(integ_real, dirpath):
            empty.add(os.path.relpath(dirpath, integ).replace(os.sep, "/"))
    return empty


def _save_pre_empty_dirs(run_dir: Optional[str], pre_empty_dirs: Set[str]) -> None:
    path = _pre_empty_dirs_path(run_dir)
    if not path or not os.path.isdir(os.path.dirname(path)):
        return
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".pre_empty_dirs_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(sorted(pre_empty_dirs), f)
            if sys.platform == "win32":
                _plat_perms.set_private_file_acl(tmp, 0o600)
            _plat_fs.atomic_replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass


def _load_pre_empty_dirs(run_dir: Optional[str]) -> Optional[Set[str]]:
    path = _pre_empty_dirs_path(run_dir)
    if not path or not os.path.isfile(path):
        return None
    try:
        data = json.loads(_plat_fs.read_bytes_shared(path).decode("utf-8"))
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return set(data)
    except (OSError, ValueError, TypeError):
        pass
    return None


_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _parse_diff_raw_z_full(raw: str) -> List[Tuple[str, str, str, str, str, str]]:
    """Parse ``git diff --raw --no-renames -z`` output into
    ``(old_mode, new_mode, old_oid, new_oid, status, path)``.
    """
    tokens = (raw or "").split("\0")
    out: List[Tuple[str, str, str, str, str, str]] = []
    i = 0
    while i < len(tokens):
        header = tokens[i]
        i += 1
        if not header or not header.startswith(":"):
            continue
        parts = header[1:].split()
        if len(parts) < 5:
            continue
        old_mode = parts[0]
        new_mode = parts[1]
        old_oid = parts[2]
        new_oid = parts[3]
        status = parts[4]
        if i >= len(tokens):
            break
        rel = tokens[i]
        i += 1
        if status and status[0] in ("R", "C") and i < len(tokens):
            i += 1
        if rel:
            out.append((old_mode, new_mode, old_oid, new_oid, status, rel))
    return out


def _parse_diff_raw_z(raw: str) -> List[Tuple[str, str, str]]:
    """Parse ``git diff --raw --no-renames -z`` output into ``(old_mode, status, path)``."""
    return [
        (old_mode, status, rel)
        for old_mode, _new_mode, _old_oid, _new_oid, status, rel in _parse_diff_raw_z_full(raw)
    ]


def _gitlink_paths(
    integ: str,
    refs: Tuple[Optional[str], ...] = ("HEAD",),
    include_index: bool = True,
) -> Set[str]:
    """Return normalized paths of tracked 160000 gitlinks in ``refs`` and/or the index."""
    out: Set[str] = set()
    if not integ or not os.path.isdir(integ):
        return out
    seen_refs: Set[str] = set()
    for ref in refs:
        if not ref or ref in seen_refs:
            continue
        seen_refs.add(ref)
        try:
            cp = run_git(["diff", "--raw", "--no-renames", "-z", _EMPTY_TREE_SHA, ref], cwd=integ)
        except Exception:
            continue
        if cp.returncode != 0:
            continue
        for old_mode, new_mode, _oo, _no, _st, rel in _parse_diff_raw_z_full(cp.stdout or ""):
            if old_mode == "160000" or new_mode == "160000":
                clean = rel.strip("/")
                if clean:
                    out.add(clean)
    if include_index:
        try:
            cp = run_git(["diff", "--cached", "--raw", "--no-renames", "-z", _EMPTY_TREE_SHA], cwd=integ)
        except Exception:
            cp = None
        if cp is not None and cp.returncode == 0:
            for old_mode, new_mode, _oo, _no, _st, rel in _parse_diff_raw_z_full(cp.stdout or ""):
                if old_mode == "160000" or new_mode == "160000":
                    clean = rel.strip("/")
                    if clean:
                        out.add(clean)
    return out


def _is_under_gitlink(rel: str, gitlinks: Set[str]) -> bool:
    norm = (rel or "").strip("/")
    if not norm or not gitlinks:
        return False
    return norm in gitlinks or any(norm.startswith(g + "/") for g in gitlinks)


def _gitlink_ancestors(gitlinks: Set[str]) -> Set[str]:
    anc: Set[str] = set()
    for g in gitlinks:
        parts = [p for p in g.strip("/").split("/") if p]
        for i in range(1, len(parts)):
            anc.add("/".join(parts[:i]))
    return anc


def _is_safe_gitlink_dir(integ: str, rel: str) -> bool:
    """True iff ``rel`` and all its ancestors inside ``integ`` are non-symlink dirs under ``integ``."""
    if not integ or not os.path.isdir(integ):
        return False
    integ_real = os.path.realpath(integ)
    parts = [p for p in (rel or "").strip("/").split("/") if p]
    if not parts:
        return False
    cur = integ
    for part in parts:
        cur = os.path.join(cur, part)
        if _plat_paths.is_reparse_link(cur) or not os.path.isdir(cur):
            return False
        if not _plat_paths.is_path_equal_or_parent(integ_real, cur):
            return False
    return True


def _tampered_gitlink_paths(integ: str, gitlinks: Set[str]) -> Set[str]:
    """Return tracked 160000 gitlink paths whose path or ancestor became a symlink or non-dir."""
    tampered: Set[str] = set()
    if not integ or not os.path.isdir(integ) or not gitlinks:
        return tampered
    for g in gitlinks:
        parts = [p for p in g.strip("/").split("/") if p]
        if not parts:
            continue
        cur = integ
        for part in parts:
            cur = os.path.join(cur, part)
            if _plat_paths.is_reparse_link(cur):
                tampered.add(g)
                break
            if os.path.lexists(cur) and not os.path.isdir(cur):
                tampered.add(g)
                break
            if not os.path.lexists(cur):
                break
    return tampered


def _open_gitlink_ancestors(integ: str, gitlinks: Set[str]) -> None:
    """Give owned, non-symlink ancestors of each gitlink back their search bit (review PR45 R5 P2).

    A test may ``chmod 000`` the parent of a nested gitlink (``deps`` for
    ``deps/nested_sub``); ``islink``/``isdir`` then swallow ``EACCES`` and the
    gitlink looks unsafe, so it would neither be skipped (an initialized submodule
    got its read-only dirs chmod'ed) nor cleaned (an empty one kept test litter).
    Only strict ancestors inside ``integ`` are touched, never the gitlink itself.
    """
    if not integ or not gitlinks or not os.path.isdir(integ):
        return
    integ_real = os.path.realpath(integ)
    for g in sorted(gitlinks):
        parts = [p for p in g.strip("/").split("/") if p]
        if len(parts) < 2:
            continue
        try:
            make_owned_ancestors_writable(os.path.join(integ, *parts), integ_real)
        except OSError:
            continue


def _gitlink_skip_paths_real(integ: str, gitlinks: Set[str]) -> Set[str]:
    out: Set[str] = set()
    if not integ or not gitlinks:
        return out
    _open_gitlink_ancestors(integ, gitlinks)
    for g in gitlinks:
        if _is_safe_gitlink_dir(integ, g):
            parts = [p for p in g.strip("/").split("/") if p]
            if parts:
                out.add(os.path.realpath(os.path.join(integ, *parts)))
    return out


def _cleanable_and_exempt_gitlinks(
    integ: str,
    gitlinks: Set[str],
    empty_snapshot: Optional[Set[str]],
) -> Tuple[Set[str], Set[str]]:
    """Split tracked 160000 gitlinks into cleanable (empty in snapshot) vs. exempt."""
    cleanable: Set[str] = set()
    if empty_snapshot is not None:
        _open_gitlink_ancestors(integ, gitlinks)
        for g in gitlinks:
            if g in empty_snapshot and _is_safe_gitlink_dir(integ, g):
                cleanable.add(g)
    exempt = gitlinks - cleanable
    return cleanable, exempt


def _cleanup_inside_empty_gitlinks(integ: str, cleanable_gitlinks: Set[str]) -> None:
    """Remove artifacts written inside uninitialized gitlink dirs that were empty in snapshot."""
    if not integ or not os.path.isdir(integ) or not cleanable_gitlinks:
        return
    integ_real = os.path.realpath(integ)
    for g in sorted(cleanable_gitlinks):
        if not _is_safe_gitlink_dir(integ, g):
            continue
        g_path = os.path.join(integ, *[p for p in g.strip("/").split("/") if p])
        g_real = os.path.realpath(g_path)
        try:
            entries = os.listdir(g_path)
        except PermissionError:
            if not make_owned_ancestors_writable(g_path, integ_real):
                continue
            try:
                os.chmod(g_path, 0o755)
                entries = os.listdir(g_path)
            except OSError:
                continue
        except OSError:
            continue
        if not entries:
            continue
        make_owned_dirs_writable(g_path, boundary_real=integ_real)
        for name in entries:
            child = os.path.join(g_path, name)
            try:
                if _plat_paths.is_reparse_link(child) or not os.path.isdir(child):
                    try:
                        _unlink_nonregular(child)
                    except PermissionError:
                        make_owned_ancestors_writable(child, integ_real)
                        _unlink_nonregular(child)
                else:
                    rmtree_allow_readonly(child, boundary_real=g_real)
            except OSError:
                pass


def _non_empty_cleanable_gitlinks(integ: str, cleanable_gitlinks: Set[str]) -> List[str]:
    dirty: List[str] = []
    if not integ or not os.path.isdir(integ) or not cleanable_gitlinks:
        return dirty
    for g in sorted(cleanable_gitlinks):
        if not _is_safe_gitlink_dir(integ, g):
            continue
        g_path = os.path.join(integ, *[p for p in g.strip("/").split("/") if p])
        try:
            if os.listdir(g_path):
                dirty.append(g)
        except OSError:
            dirty.append(g)
    return dirty


def _fallback_diff_paths(cwd: str, ref: Optional[str] = "HEAD") -> Set[str]:
    """Collect tracked dirty paths via ``git diff --raw`` when ``git status`` fails."""
    out: Set[str] = set()
    try:
        args = ["diff", "--raw", "--no-renames", "-z"] + ([ref] if ref else [])
        cp = run_git(args, cwd=cwd)
        if cp.returncode == 0:
            for _om, _st, rel in _parse_diff_raw_z(cp.stdout or ""):
                if rel:
                    out.add(rel)
    except Exception:
        pass
    if ref:
        try:
            cp_i = run_git(["diff", "--raw", "--no-renames", "-z"], cwd=cwd)
            if cp_i.returncode == 0:
                for _om, _st, rel in _parse_diff_raw_z(cp_i.stdout or ""):
                    if rel:
                        out.add(rel)
        except Exception:
            pass
    return out


def _restore_index_bytes(
    integ: str, index_bytes: Optional[bytes], shared_bytes: Optional[bytes] = None
) -> bool:
    if not isinstance(index_bytes, (bytes, bytearray)):
        return False
    idx_path = _git_path(integ, "index", "rev-parse index path")
    if not idx_path:
        return False
    shared_name = _index_bytes_shared_name(index_bytes)
    if shared_name:
        # Put the linked shared index back first if the test expired/removed it,
        # so the restored index never points at a missing file.
        sh_path = os.path.join(os.path.dirname(idx_path), shared_name)
        if not os.path.isfile(sh_path) or _plat_paths.is_reparse_link(sh_path):
            if not isinstance(shared_bytes, (bytes, bytearray)):
                return False
            try:
                if _plat_paths.is_reparse_link(sh_path):
                    _unlink_nonregular(sh_path)
                _atomic_write_bytes(sh_path, bytes(shared_bytes), mode=0o644)
            except OSError:
                return False
    try:
        st_mode = (
            (os.lstat(idx_path).st_mode & 0o777)
            if os.path.lexists(idx_path) and not _plat_paths.is_reparse_link(idx_path)
            else 0o644
        )
        if _plat_paths.is_reparse_link(idx_path):
            _unlink_nonregular(idx_path)
        _atomic_write_bytes(idx_path, bytes(index_bytes), mode=st_mode or 0o644)
        return True
    except OSError:
        return False


def _restore_head_and_ref(
    integ: str,
    pre_head: str,
    *,
    pre_symref: Optional[str] = None,
    pre_head_raw: Optional[str] = None,
) -> bool:
    """Restore ``HEAD`` attachment/target and integration branch ref back to ``pre_head``."""
    head_path = _git_path(integ, "HEAD", "rev-parse HEAD path")
    target_ref = pre_symref if (pre_symref and pre_symref.startswith("refs/heads/")) else None
    cur_symref = _head_symref(integ, "rev-parse current HEAD symref")
    if (
        target_ref is None
        and cur_symref
        and cur_symref.startswith("refs/heads/")
        and not (isinstance(pre_head_raw, str) and pre_head_raw.strip() == pre_head)
    ):
        target_ref = cur_symref

    desired_raw: Optional[str] = None
    if target_ref:
        desired_raw = f"ref: {target_ref}\n"
    elif isinstance(pre_head_raw, str) and pre_head_raw.strip():
        desired_raw = pre_head_raw if pre_head_raw.endswith("\n") else pre_head_raw + "\n"

    if desired_raw and head_path:
        try:
            cur_raw = None
            if os.path.lexists(head_path) and not _plat_paths.is_reparse_link(head_path) and os.path.isfile(head_path):
                if sys.platform == "win32":
                    cur_raw = _plat_fs.read_bytes_shared(head_path).decode("utf-8")
                else:
                    with open(head_path, "r", encoding="utf-8") as f:
                        cur_raw = f.read()
            if cur_raw != desired_raw:
                if _plat_paths.is_reparse_link(head_path):
                    _unlink_nonregular(head_path)
                _atomic_write_bytes(head_path, desired_raw.encode("utf-8"), mode=0o644)
        except (OSError, UnicodeDecodeError):
            return False

    try:
        cur_head = _head(integ, "check current HEAD")
    except _GitFailed:
        cur_head = None
    cur_sym_after = _head_symref(integ, "check current HEAD symref")
    if cur_head == pre_head and (target_ref is None or cur_sym_after == target_ref):
        return True
    # Compare-and-swap only (gitops allow-list, Carbon review): the ref moves back to
    # pre_head only from the value read here; a concurrent writer makes it fail closed.
    zero = "0" * len(pre_head)
    if target_ref:
        cp = _git("read branch ref", ["rev-parse", "-q", "--verify", f"{target_ref}^{{commit}}"], integ)
        old = (cp.stdout or "").strip() if cp.returncode == 0 else ""
        cp = _git("restore branch ref", ["update-ref", target_ref, pre_head, old or zero], integ)
        return cp.returncode == 0
    if cur_head is None:
        return False
    cp = _git("restore detached HEAD", ["update-ref", "--no-deref", "HEAD", pre_head, cur_head], integ)
    return cp.returncode == 0


def _staged_index_snapshot(
    integ: str, step: str = "staged index snapshot"
) -> Dict[str, Tuple[str, str, str, str, str]]:
    """Snapshot staged index entries vs HEAD keyed by relative path."""
    cp = _git(step, ["diff", "--cached", "--raw", "--full-index", "--no-renames", "-z", "HEAD"], integ)
    if cp.returncode != 0:
        raise _GitFailed(step, cp)
    snap: Dict[str, Tuple[str, str, str, str, str]] = {}
    for old_mode, new_mode, old_oid, new_oid, status, rel in _parse_diff_raw_z_full(cp.stdout or ""):
        snap[rel] = (old_mode, new_mode, old_oid, new_oid, status)
    return snap


def _cleanup_empty_dirs(
    integ: str,
    pre_empty_dirs: Set[str],
    protected_refs: Tuple[Optional[str], ...] = ("HEAD",),
    include_index: bool = True,
) -> None:
    if not os.path.isdir(integ):
        return
    integ_real = os.path.realpath(integ)
    gitlinks = _gitlink_paths(integ, refs=protected_refs, include_index=include_index)
    _cleanable_gl, exempt_gl = _cleanable_and_exempt_gitlinks(integ, gitlinks, pre_empty_dirs)
    gitlink_ancs = _gitlink_ancestors(gitlinks)
    make_owned_dirs_writable(
        integ,
        boundary_real=integ_real,
        skip_names={".git"},
        skip_paths_real=_gitlink_skip_paths_real(integ, exempt_gl),
    )
    dirs: List[Tuple[str, str]] = []
    for dirpath, dirnames, _filenames in os.walk(integ, topdown=True, followlinks=False):
        rel_cur = (
            ""
            if dirpath == integ
            else os.path.relpath(dirpath, integ).replace(os.sep, "/").strip("/")
        )
        kept_dirnames: List[str] = []
        for d in sorted(dirnames):
            if d == ".git" or _plat_paths.is_reparse_link(os.path.join(dirpath, d)):
                continue
            child_rel = f"{rel_cur}/{d}" if rel_cur else d
            if _is_under_gitlink(child_rel, gitlinks):
                continue
            kept_dirnames.append(d)
        dirnames[:] = kept_dirnames
        if _plat_paths.same_path(dirpath, integ) or _plat_paths.is_reparse_link(dirpath):
            continue
        if _plat_paths.same_path(dirpath, integ_real) or not _plat_paths.is_path_equal_or_parent(integ_real, dirpath):
            continue
        if _is_under_gitlink(rel_cur, gitlinks) or rel_cur in gitlink_ancs:
            continue
        dirs.append((dirpath, rel_cur))
    for dirpath, rel in reversed(dirs):
        if rel in pre_empty_dirs:
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def _post_test_dirty_paths(
    integ: str,
    pre_untracked: Optional[Set[str]] = None,
    pre_empty_dirs: Optional[Set[str]] = None,
    pre_staged: Optional[Dict[str, Tuple[str, str, str, str, str]]] = None,
    pre_head: Optional[str] = None,
    pre_flags: Optional[Dict[str, Tuple[bool, bool, bool]]] = None,
    pre_symref: Optional[str] = None,
) -> List[str]:
    """Return paths left dirty in ``integ`` after tests (untracked, staged/HEAD, hide flags, empty dirs, or edits)."""
    gitlinks = _gitlink_paths(integ, refs=(), include_index=True)
    cleanable_gl, _exempt_gl = _cleanable_and_exempt_gitlinks(integ, gitlinks, pre_empty_dirs)
    tampered_gl = _tampered_gitlink_paths(integ, gitlinks)
    non_empty_gl = _non_empty_cleanable_gitlinks(integ, cleanable_gl)

    cur_flags = _read_index_flags(integ, "verify post-test index flags")
    flag_dirty: List[str] = []
    if cur_flags is None:
        flag_dirty.append(".git/index")
    else:
        base_flags = pre_flags or {}
        for rel, (au, sw, _ita) in sorted(cur_flags.items()):
            pre_au, pre_sw, _pre_ita = base_flags.get(rel, (False, False, False))
            if (au and not pre_au) or (sw and not pre_sw) or (pre_flags is None and (au or sw)):
                flag_dirty.append(rel)

    cp = _git("verify post-test status", ["status", "--porcelain=v1", "--untracked-files=all", "-z"], integ)
    dirty: List[str] = []
    seen_dirty: Set[str] = set()
    if cp.returncode != 0:
        fallback = set(tampered_gl) | set(non_empty_gl) | set(flag_dirty) | _fallback_diff_paths(integ, ref=None)
        if pre_head is not None:
            try:
                post_head = _head(integ, "verify post-test HEAD")
                if post_head != pre_head:
                    dcp = _git("diff post-test HEAD", ["diff", "--raw", "--no-renames", "-z", pre_head, post_head], integ)
                    changed = False
                    if dcp.returncode == 0:
                        for _om, _st, rel in _parse_diff_raw_z(dcp.stdout or ""):
                            changed = True
                            fallback.add(rel)
                    if not changed:
                        fallback.add(".git/HEAD")
            except _GitFailed:
                fallback.add(".git/HEAD")
        if pre_symref and pre_symref.startswith("refs/heads/"):
            if _head_symref(integ, "verify post-test symref") != pre_symref:
                fallback.add(".git/HEAD")
        if pre_staged is not None:
            try:
                cur_staged = _staged_index_snapshot(integ, "verify post-test staged index")
                for rel in sorted(set(cur_staged) | set(pre_staged)):
                    if cur_staged.get(rel) != pre_staged.get(rel):
                        fallback.add(rel)
            except _GitFailed:
                pass
        if pre_empty_dirs is not None:
            for d in sorted(_snapshot_empty_dirs(integ)):
                if d not in pre_empty_dirs and not _is_under_gitlink(d, gitlinks):
                    fallback.add(d)
        if fallback:
            return sorted(fallback)
        raise _GitFailed("verify post-test status", cp)

    entries = (cp.stdout or "").split("\0")
    i = 0
    while i < len(entries):
        rec = entries[i]
        i += 1
        if not rec or len(rec) < 4:
            continue
        code = rec[:2]
        rel = rec[3:]
        if code[0] in ("R", "C"):
            i += 1
        if code == "??":
            if (pre_untracked is None or rel not in pre_untracked) and rel not in seen_dirty:
                dirty.append(rel)
                seen_dirty.add(rel)
        elif code[1] != " ":
            if rel not in seen_dirty:
                dirty.append(rel)
                seen_dirty.add(rel)
    for rel in flag_dirty + sorted(tampered_gl) + non_empty_gl:
        if rel not in seen_dirty:
            dirty.append(rel)
            seen_dirty.add(rel)
    if pre_head is not None:
        post_head = _head(integ, "verify post-test HEAD")
        if post_head != pre_head:
            dcp = _git("diff post-test HEAD", ["diff", "--raw", "--no-renames", "-z", pre_head, post_head], integ)
            changed = False
            if dcp.returncode == 0:
                for _om, _st, rel in _parse_diff_raw_z(dcp.stdout or ""):
                    changed = True
                    if rel not in seen_dirty:
                        dirty.append(rel)
                        seen_dirty.add(rel)
            if not changed and ".git/HEAD" not in seen_dirty:
                dirty.append(".git/HEAD")
                seen_dirty.add(".git/HEAD")
    if pre_symref and pre_symref.startswith("refs/heads/"):
        if _head_symref(integ, "verify post-test symref") != pre_symref and ".git/HEAD" not in seen_dirty:
            dirty.append(".git/HEAD")
            seen_dirty.add(".git/HEAD")
    if pre_staged is not None:
        cur_staged = _staged_index_snapshot(integ, "verify post-test staged index")
        for rel in sorted(set(cur_staged) | set(pre_staged)):
            if cur_staged.get(rel) != pre_staged.get(rel) and rel not in seen_dirty:
                dirty.append(rel)
                seen_dirty.add(rel)
    if pre_empty_dirs is not None:
        for d in sorted(_snapshot_empty_dirs(integ)):
            if d not in pre_empty_dirs and not _is_under_gitlink(d, gitlinks) and d not in seen_dirty:
                dirty.append(d)
                seen_dirty.add(d)
    return dirty


def _unlink_new_reparse_ancestor(
    integ: str,
    integ_real: str,
    rel: str,
    pre_untracked: Set[str],
    protected_dirs: Set[str],
) -> bool:
    """Handle a newly created junction/symlink reported via a nested child path.

    On Windows ``git status -uall`` descends into NTFS junctions and reports
    ``junction/child.txt``. Never traverse inside the link; unlink the new
    junction itself (unless protected/pre-existing) so external targets stay safe.
    Returns True if a reparse ancestor handled/blocked ``rel``.
    """
    parts = [p for p in (rel or "").replace("\\", "/").strip("/").split("/") if p]
    if len(parts) < 2:
        return False
    cur = integ
    for idx, part in enumerate(parts[:-1]):
        if part in (".", "..", ".git"):
            return True
        cur = os.path.join(cur, part)
        if _plat_paths.is_reparse_link(cur):
            prefix = "/".join(parts[: idx + 1])
            if (
                prefix in protected_dirs
                or prefix in pre_untracked
                or any(p.startswith(prefix + "/") for p in pre_untracked)
            ):
                return True
            if not _plat_paths.is_path_equal_or_parent(integ_real, os.path.dirname(cur)):
                return True
            try:
                _unlink_nonregular(cur)
            except PermissionError:
                make_owned_ancestors_writable(cur, integ_real)
                _unlink_nonregular(cur)
            return True
        if not os.path.isdir(cur):
            return False
    return False


def _cleanup_new_untracked(
    integ: str,
    pre_untracked: Optional[Set[str]],
    pre_empty_dirs: Optional[Set[str]] = None,
    protected_refs: Tuple[Optional[str], ...] = ("HEAD",),
    include_index: bool = True,
) -> None:
    """Remove untracked files and empty dirs created after snapshot (e.g. by test_command)."""
    if pre_untracked is None:
        return
    gitlinks = _gitlink_paths(integ, refs=protected_refs, include_index=include_index)
    cleanable_gl, exempt_gl = _cleanable_and_exempt_gitlinks(integ, gitlinks, pre_empty_dirs)
    integ_real = os.path.realpath(integ) if os.path.isdir(integ) else None
    if integ_real is not None:
        make_owned_dirs_writable(
            integ,
            boundary_real=integ_real,
            skip_names={".git"},
            skip_paths_real=_gitlink_skip_paths_real(integ, exempt_gl),
        )
    _cleanup_inside_empty_gitlinks(integ, cleanable_gl)
    try:
        current = _untracked_paths(integ, "cleanup untracked")
    except Exception:
        return
    protected_dirs = set(pre_empty_dirs or ()) | gitlinks | _gitlink_ancestors(gitlinks)
    for rel in current:
        if rel in pre_untracked or _is_under_gitlink(rel, gitlinks):
            continue
        try:
            if integ_real is not None and _unlink_new_reparse_ancestor(
                integ, integ_real, rel, pre_untracked, protected_dirs
            ):
                continue
            _safe_unlink_untracked(integ, rel, protected_dirs=protected_dirs)
        except OSError:
            pass
    if pre_empty_dirs is not None:
        _cleanup_empty_dirs(
            integ,
            pre_empty_dirs,
            protected_refs=protected_refs,
            include_index=include_index,
        )


def _restore_single_tracked_path(integ: str, integ_real: str, ref: Optional[str], rel: str, old_mode: str) -> None:
    """Safely restore one tracked worktree entry inside ``integ`` from ``ref`` (or index ``:0:``)."""
    rel_clean = (rel or "").strip("/")
    if not rel_clean or os.path.isabs(rel_clean):
        return
    parts = [p for p in rel_clean.split("/") if p]
    if not parts or any(p in (".", "..", ".git") for p in parts):
        return
    if old_mode == "000000":
        cur = integ
        for part in parts[:-1]:
            cur = os.path.join(cur, part)
            if _plat_paths.is_reparse_link(cur) or not os.path.isdir(cur):
                return
            if _plat_paths.same_path(cur, integ_real) or not _plat_paths.is_path_equal_or_parent(integ_real, cur):
                return
        target = os.path.join(cur, parts[-1])
        if not _plat_paths.is_path_equal_or_parent(integ_real, os.path.dirname(target)):
            return
        try:
            if os.path.lexists(target):
                if _plat_paths.is_reparse_link(target) or not os.path.isdir(target):
                    _unlink_nonregular(target)
                else:
                    rmtree_allow_readonly(target, boundary_real=integ_real)
        except PermissionError:
            make_owned_ancestors_writable(target, integ_real)
            if os.path.lexists(target):
                if _plat_paths.is_reparse_link(target) or not os.path.isdir(target):
                    _unlink_nonregular(target)
                else:
                    rmtree_allow_readonly(target, boundary_real=integ_real)
        return
    cur = integ
    for part in parts[:-1]:
        cur = os.path.join(cur, part)
        if _plat_paths.is_reparse_link(cur):
            _unlink_nonregular(cur)
        elif os.path.lexists(cur) and not os.path.isdir(cur):
            _unlink_nonregular(cur)
        if not os.path.exists(cur):
            try:
                os.makedirs(cur, mode=0o755, exist_ok=True)
            except PermissionError:
                make_owned_ancestors_writable(cur, integ_real)
                os.makedirs(cur, mode=0o755, exist_ok=True)
        if _plat_paths.same_path(cur, integ_real) or not _plat_paths.is_path_equal_or_parent(integ_real, cur):
            return
    target = os.path.join(cur, parts[-1])
    if not _plat_paths.is_path_equal_or_parent(integ_real, os.path.dirname(target)):
        return
    if old_mode == "160000" and not _plat_paths.is_reparse_link(target) and os.path.isdir(target):
        return
    try:
        if os.path.lexists(target):
            if _plat_paths.is_reparse_link(target) or not os.path.isdir(target):
                _unlink_nonregular(target)
            else:
                rmtree_allow_readonly(target, boundary_real=integ_real)
    except PermissionError:
        make_owned_ancestors_writable(target, integ_real)
        if os.path.lexists(target):
            if _plat_paths.is_reparse_link(target) or not os.path.isdir(target):
                _unlink_nonregular(target)
            else:
                rmtree_allow_readonly(target, boundary_real=integ_real)

    if old_mode == "160000":
        os.makedirs(target, mode=0o755, exist_ok=True)
        return
    obj_spec = f"{ref}:{rel}" if ref else f":0:{rel}"
    blob_cp = run_git(["cat-file", "blob", obj_spec], cwd=integ, text=False)
    if blob_cp.returncode != 0:
        return
    data = blob_cp.stdout if isinstance(blob_cp.stdout, bytes) else (blob_cp.stdout or "").encode("utf-8")
    if old_mode == "120000":
        if sys.platform == "win32":
            link_target = data.decode("utf-8", errors="surrogateescape")
            resolved_target = (
                link_target
                if os.path.isabs(link_target)
                else os.path.join(os.path.dirname(target), link_target)
            )
            target_is_dir = os.path.isdir(resolved_target)
            try:
                os.symlink(link_target, target, target_is_directory=target_is_dir)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    raise OSError(
                        exc.errno or errno.EACCES,
                        "Cannot restore tracked git symlink without Windows SeCreateSymbolicLinkPrivilege "
                        "(WinError 1314; enable Developer Mode or grant SeCreateSymbolicLinkPrivilege)",
                        target,
                    ) from exc
                raise
        else:
            os.symlink(data, target)
        return
    mode = 0o755 if old_mode == "100755" else 0o644
    try:
        fd = _plat_fs.open_regular_nofollow(
            target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode, label="tracked worktree file"
        )
    except PermissionError:
        make_owned_ancestors_writable(target, integ_real)
        if os.path.lexists(target) and not _plat_paths.is_reparse_link(target):
            try:
                os.chmod(target, 0o600 if sys.platform == "win32" else mode)
            except OSError:
                pass
        fd = _plat_fs.open_regular_nofollow(
            target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode, label="tracked worktree file"
        )
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
    finally:
        os.close(fd)
    os.chmod(target, mode)


def _restore_tracked_from_ref(integ: str, ref: Optional[str]) -> None:
    """Restore dirty tracked worktree files in ``integ`` from ``ref`` (commit) or index (None)."""
    if not os.path.isdir(integ):
        return
    integ_real = os.path.realpath(integ)
    gitlinks = _gitlink_paths(integ, refs=(ref,) if ref else (), include_index=True)
    make_owned_dirs_writable(
        integ,
        boundary_real=integ_real,
        skip_names={".git"},
        skip_paths_real=_gitlink_skip_paths_real(integ, gitlinks),
    )
    args = ["diff", "--raw", "--no-renames", "-z"] + ([ref] if ref else [])
    cp = _git("diff dirty tracked", args, integ)
    if cp.returncode != 0:
        return
    entries = _parse_diff_raw_z(cp.stdout or "")
    for old_mode, status, rel in entries:
        if status.startswith("U"):
            continue
        if ref is None and old_mode == "000000":
            continue
        try:
            _restore_single_tracked_path(integ, integ_real, ref, rel, old_mode)
        except OSError:
            pass
    if entries:
        try:
            _git("refresh status", ["status", "--porcelain=v1"], integ)
        except _GitFailed:
            pass


def _abort_and_verify(
    integ: str,
    pre_head: str,
    pre_untracked: Optional[Set[str]] = None,
    pre_empty_dirs: Optional[Set[str]] = None,
    run_dir: Optional[str] = None,
    *,
    pre_index_bytes: Optional[bytes] = None,
    pre_symref: Optional[str] = None,
    pre_head_raw: Optional[str] = None,
    noop: bool = False,
    repo: Optional[str] = None,
    ws_root: Optional[str] = None,
) -> Dict[str, Any]:
    """`merge --abort`, then restore HEAD/branch/index/tracked/untracked, and verify clean (§10.5 step 2).

    ``noop=True`` (Carbon P2-5: a noop merge whose test step failed) has no merge of its own
    to abort: unless the test left a MERGE_HEAD, `merge --abort` is skipped and only the
    restore and verification run."""
    if not _verify_integration_worktree(integ, repo, ws_root):
        return {"restored": False, "integration": integ, "foreign_repository": True}
    out: Dict[str, Any] = {}
    pre_shared_bytes = _load_pre_merge_shared(run_dir) if run_dir else None
    if run_dir:
        if pre_index_bytes is None:
            pre_index_bytes = _load_pre_merge_index(run_dir)
        if pre_symref is None or pre_head_raw is None:
            pm = _load_pre_merge_state(run_dir)
            if pm:
                if pre_symref is None:
                    pre_symref = pm.get("symref")
                if pre_head_raw is None:
                    pre_head_raw = pm.get("head_raw")

    integ_real = os.path.realpath(integ) if os.path.isdir(integ) else None
    gitlinks = _gitlink_paths(integ, refs=("HEAD", pre_head), include_index=True)
    cleanable_gl, exempt_gl = _cleanable_and_exempt_gitlinks(integ, gitlinks, pre_empty_dirs)
    skip_reals = _gitlink_skip_paths_real(integ, exempt_gl)
    if pre_untracked is not None and integ_real:
        make_owned_dirs_writable(
            integ,
            boundary_real=integ_real,
            skip_names={".git"},
            skip_paths_real=skip_reals,
        )
    if noop and not _merge_head_exists(integ):
        pass
    elif (cp := _git("merge --abort", ["merge", "--abort"], integ)).returncode == 0:
        _maybe_crash_after_merge_abort(run_dir)
    else:
        if integ_real:
            make_owned_dirs_writable(
                integ,
                boundary_real=integ_real,
                skip_names={".git"},
                skip_paths_real=skip_reals,
            )
        _clear_index_hide_flags(integ, "clear hide flags on abort retry")
        try:
            if _merge_head_present(integ):
                _restore_tracked_from_ref(integ, ref=None)
                if pre_untracked is not None:
                    _cleanup_new_untracked(
                        integ,
                        pre_untracked,
                        pre_empty_dirs=pre_empty_dirs,
                        protected_refs=("HEAD", pre_head),
                        include_index=True,
                    )
                cp2 = _git("merge --abort", ["merge", "--abort"], integ)
                if cp2.returncode == 0:
                    _maybe_crash_after_merge_abort(run_dir)
                else:
                    out["abort_stderr"] = _clip(cp2.stderr)
            else:
                out["abort_stderr"] = _clip(cp.stderr)
        except Exception:
            out["abort_stderr"] = _clip(cp.stderr)
        # Review PR45 R5 P1: a corrupt/split/hide-flagged index can make every
        # `merge --abort` fail and leave MERGE_HEAD behind. With the pre-merge index
        # snapshot in hand we do not need git's abort: drop the merge state files and
        # fall through to the full HEAD/index/worktree restore below.
        if pre_index_bytes is not None:
            try:
                still_merging = _merge_head_exists(integ)
            except _GitFailed:
                still_merging = True
            if still_merging:
                _clear_merge_state_files(integ)
                out["forced_abort"] = True
    try:
        if not _merge_head_present(integ):
            _restore_head_and_ref(
                integ, pre_head, pre_symref=pre_symref, pre_head_raw=pre_head_raw
            )
            if pre_index_bytes is not None:
                _restore_index_bytes(integ, pre_index_bytes, pre_shared_bytes)
            _cleanup_stray_shared_indexes(integ)
            _clear_index_hide_flags(integ, "clear hide flags before restore")
            post_gl = _gitlink_paths(integ, refs=(pre_head,), include_index=True)
            post_cleanable_gl, post_exempt_gl = _cleanable_and_exempt_gitlinks(
                integ, post_gl, pre_empty_dirs
            )
            if integ_real:
                make_owned_dirs_writable(
                    integ,
                    boundary_real=integ_real,
                    skip_names={".git"},
                    skip_paths_real=_gitlink_skip_paths_real(integ, post_exempt_gl),
                )
            _restore_tracked_from_ref(integ, ref=pre_head)
            _cleanup_inside_empty_gitlinks(integ, post_cleanable_gl)
            _cleanup_new_untracked(
                integ,
                pre_untracked,
                pre_empty_dirs=pre_empty_dirs,
                protected_refs=(pre_head,),
                include_index=True,
            )
        head = _head(integ, "verify HEAD")
        symref_ok = True
        if pre_symref and pre_symref.startswith("refs/heads/"):
            symref_ok = _head_symref(integ, "verify symref") == pre_symref
        dirty = _dirty_paths(integ, "verify status")
        post_gitlinks = _gitlink_paths(integ, refs=("HEAD", pre_head), include_index=True)
        post_cleanable, _post_exempt = _cleanable_and_exempt_gitlinks(integ, post_gitlinks, pre_empty_dirs)
        seen_dirty = set(dirty)
        for rel in _non_empty_cleanable_gitlinks(integ, post_cleanable):
            if rel not in seen_dirty:
                dirty.append(rel)
                seen_dirty.add(rel)
        if pre_empty_dirs is not None:
            for d in sorted(_snapshot_empty_dirs(integ)):
                if d not in pre_empty_dirs and not _is_under_gitlink(d, post_gitlinks) and d not in seen_dirty:
                    dirty.append(d)
                    seen_dirty.add(d)
        merge_head = _merge_head_exists(integ)
    except _GitFailed as gf:
        out.update({"restored": False, "verify_error": gf.details})
        return out
    out["restored"] = (head == pre_head) and symref_ok and (not dirty) and (not merge_head)
    if not out["restored"]:
        out.update({"head": head, "merge_head": merge_head})
        if not symref_ok:
            out["symref"] = _head_symref(integ, "verify symref on mismatch")
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
    if is_site_packages_dir(install_root):
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = install_root
    return env


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(_plat_fs.read_bytes_shared(path).decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _clip_utf8(text: str, limit: int) -> Tuple[str, bool]:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


FAILING_TESTS_LIMIT = 20
FAILING_TESTS_SCAN_BYTES = 8 * 1024 * 1024
_UNITTEST_FAIL_RE = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)\s]+)\)")
_PYTEST_FAIL_RE = re.compile(r"^(FAILED|ERROR) (\S+::\S+)")


def failing_tests(path: str, limit: int = FAILING_TESTS_LIMIT) -> Tuple[List[Dict[str, str]], int]:
    """Failing test ids in a test log: unittest ``FAIL:/ERROR: name (id)`` lines and pytest
    ``FAILED path::name`` lines, first seen first, de-duplicated. Returns at most
    ``limit`` entries and the total found (in the last 8 MiB of the log)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - FAILING_TESTS_SCAN_BYTES))
            data = f.read()
    except OSError:
        return [], 0
    seen: Dict[str, Dict[str, str]] = {}
    for line in data.decode("utf-8", errors="replace").splitlines():
        m = _UNITTEST_FAIL_RE.match(line)
        if m:
            kind, name, ident = m.group(1), m.group(2), m.group(3)
            # Python >= 3.11 prints the full id; older versions print the class only.
            tid = ident if ident.endswith("." + name) else f"{ident}.{name}"
        else:
            m = _PYTEST_FAIL_RE.match(line)
            if not m:
                continue
            kind, tid = ("FAIL" if m.group(1) == "FAILED" else "ERROR"), m.group(2)
        tid = _clip_utf8(tid, 512)[0]
        if tid not in seen:
            seen[tid] = {"id": tid, "kind": kind}
    found = list(seen.values())
    return found[:limit], len(found)


def tests_failed_message(task_id: str, details: Mapping[str, Any]) -> str:
    """One readable message for TESTS_FAILED: the failing ids and the log path."""
    log = details.get("log_path")
    total = details.get("failing_tests_total") or 0
    ids = details.get("failing_tests") or []
    if details.get("timeout"):
        head = f"Merge of task '{task_id}' failed: tests_failed (timed out after {details.get('timeout_s')}s)"
    elif total:
        head = f"Merge of task '{task_id}' failed: tests_failed ({total} failing test{'s' if total != 1 else ''})"
    elif details.get("exit_code") == 0 and details.get("error"):
        # The tests passed but the post-test process check did not (e.g. a leftover-process
        # scan that could not be completed): say that rather than "no FAIL lines".
        head = (f"Merge of task '{task_id}' failed: tests_failed (tests exited 0, but the post-test "
                f"check failed: {details.get('error')}; retrying the merge is safe)")
    else:
        head = (f"Merge of task '{task_id}' failed: tests_failed (exit code {details.get('exit_code')}; "
                "no FAIL:/ERROR: lines found in the log)")
    lines = [head]
    lines.extend(f"  {t['kind']}: {t['id']}" for t in ids)
    if total > len(ids):
        lines.append(f"  ... and {total - len(ids)} more")
    if log:
        lines.append(f"  log: {log}")
    return "\n".join(lines)


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
    if tail:
        # Full review r3 P2-1: redacted again as one JSONL text (key-block state across
        # lines, JSON lines kept valid) BEFORE the per-line clip, so a value newly known
        # is never cut into an unmatched prefix.
        from mesh.core.redact import redact

        tail = redact("\n".join(tail), jsonl=True).split("\n")
    truncated = False
    out: List[str] = []
    total = 0
    for line in reversed(tail):
        line, cut = _clip_utf8(line, LOG_TAIL_LINE_MAX_BYTES)
        truncated = truncated or cut
        n = len(line.encode("utf-8", "replace")) + 1
        if total + n > LOG_TAIL_MAX_BYTES:
            truncated = True
            break
        out.append(line)
        total += n
    out.reverse()
    return out, truncated


def redact_merge_value(value: Any) -> Any:
    """``redact_value`` for merge results; a ``log_tail`` (test.log lines, already JSONL-
    redacted) is redacted as one JSONL text, not line by line with the plain rules
    (r3 P2-1: that broke JSON lines and dropped the key-block state)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "log_tail" and isinstance(v, list) and all(isinstance(x, str) for x in v):
                from mesh.core.redact import redact

                out[k] = redact("\n".join(v), jsonl=True).split("\n") if v else []
            else:
                out[redact_value(k) if isinstance(k, str) else k] = redact_merge_value(v)
        return out
    if isinstance(value, list):
        return [redact_merge_value(x) for x in value]
    return redact_value(value)


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
    """True once ``proc`` has exited, without reaping it (``waitid`` with WNOWAIT on POSIX;
    ``WaitForSingleObject(proc._handle, 0) == WAIT_OBJECT_0`` without closing handle on Win32).

    An already-reaped child (``returncode`` set, or ECHILD) also counts as exited.
    """
    return _plat_proc.is_popen_exited_unreaped(proc)


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


def _kill_test_proc_direct(
    proc: "subprocess.Popen[Any]",
    grace_s: float,
    force_group_kill: bool = False,
    *,
    run_id: Optional[str] = None,
    home: Optional[str] = None,
    wrapper_starttime: Optional[int] = None,
) -> None:
    """Carbon RO P1-1 (2C): directly terminate meshd's own unreaped ``merge_test`` Popen
    session/group when ``/proc`` failures keep :func:`_terminate_test` from enumerating it.

    ``proc`` was started with ``start_new_session=True`` (or inside a Named Run Job on Win32)
    and has not been reaped yet (``proc.returncode is None``), so ``pgid == proc.pid`` is
    kernel-reserved on POSIX and handle-pinned / Job-bound on Windows.
    """
    if sys.platform == "win32":
        from mesh.daemon import supervisor as _sup

        pid = int(getattr(proc, "pid", 0) or 0)
        if getattr(proc, "returncode", None) is not None or pid <= 1 or pid == os.getpid():
            return
        eff_wst = wrapper_starttime
        if eff_wst is None and not _exited_unreaped(proc):
            try:
                info = get_proc_stat(pid)
                if info is not None and int(info.get("starttime") or 0) > 0:
                    eff_wst = int(info["starttime"])
            except OSError:
                eff_wst = None
        job = getattr(proc, "_mesh_job", None)
        should_close = False
        if job is None and run_id:
            try:
                job, should_close = _sup.resolve_run_job(run_id, home=home, terminate=True)
            except OSError:
                job, should_close = None, False
        try:
            if not _exited_unreaped(proc):
                signalled = False
                if eff_wst:
                    try:
                        signalled = _plat_proc.signal_if_same(
                            pid,
                            int(eff_wst),
                            _plat_proc.SIGTERM,
                            run_id=run_id,
                            home=home,
                        )
                    except OSError:
                        signalled = False
                elif run_id and home:
                    try:
                        signalled = _plat_proc.signal_run_stop_event(home, run_id)
                    except OSError:
                        signalled = False
                if signalled and _wait_exit_unreaped(proc, max(0.0, grace_s)) and not force_group_kill:
                    return
            elif not force_group_kill:
                return
            if job is not None:
                try:
                    _plat_proc.terminate_job(job, 137)
                except OSError:
                    pass
            if eff_wst:
                try:
                    _plat_proc.kill_if_same(pid, int(eff_wst))
                except OSError:
                    pass
            if not _exited_unreaped(proc):
                try:
                    proc.kill()
                except OSError:
                    pass
            _wait_exit_unreaped(proc, terminate.KILL_WAIT_S)
        finally:
            if should_close and job is not None:
                try:
                    job.close()
                except Exception:
                    pass
        return

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


#: Full review r2 P1-F: `run_id -> wrapper pid` of merge_test wrappers while their step
#: runs. The runs row gets its wrapper_pid only at TX2, so without this the run-session
#: binding (session / parent walk, `classify_peer_run_session`) did not see the test
#: command at all and a child that dropped MESH_RUN_ID passed as outside any run.
_ACTIVE_TEST_WRAPPERS: Dict[str, int] = {}
_ACTIVE_TEST_JOB_RUNS: Set[str] = set()
_ACTIVE_TEST_LOCK = threading.Lock()


def in_memory_active_test_runs() -> Dict[str, int]:
    with _ACTIVE_TEST_LOCK:
        return dict(_ACTIVE_TEST_WRAPPERS)


def in_memory_active_job_runs() -> Set[str]:
    """Return merge run_ids currently owning or about to own a Win32 Job handle."""
    with _ACTIVE_TEST_LOCK:
        return set(_ACTIVE_MERGE_RUNS) | set(_ACTIVE_TEST_JOB_RUNS)


def run_test_step(plan: Mapping[str, Any]) -> Dict[str, Any]:
    win_job_tracked = sys.platform == "win32"
    if win_job_tracked:
        with _ACTIVE_TEST_LOCK:
            _ACTIVE_TEST_JOB_RUNS.add(plan["run_id"])
    try:
        return _run_test_step(plan)
    finally:
        with _ACTIVE_TEST_LOCK:
            _ACTIVE_TEST_WRAPPERS.pop(plan["run_id"], None)
            if win_job_tracked:
                _ACTIVE_TEST_JOB_RUNS.discard(plan["run_id"])


def _run_test_step(plan: Mapping[str, Any]) -> Dict[str, Any]:
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
    home = os.path.dirname(os.path.dirname(_plat_paths.strip_trailing_seps(run_dir)))
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    if sys.platform == "win32":
        _plat_perms.set_private_dir_acl(run_dir, 0o700)
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
    if sys.platform == "win32":
        spec["home"] = home
        spec["run_id"] = run_id
    tmp = os.path.join(run_dir, "spec.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(spec, sort_keys=True) + "\n").encode("utf-8"))
        if sys.platform != "win32" and hasattr(os, "fchmod"):
            _plat_perms.fchmod_private(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    if sys.platform == "win32" or not hasattr(os, "fchmod"):
        _plat_perms.set_private_file_acl(tmp, 0o600)
    _plat_fs.atomic_replace(tmp, os.path.join(run_dir, "spec.json"))

    timeout_s = float(plan["test_timeout_s"])
    started = time.monotonic()
    registration_error: Optional[str] = None
    run_job: Optional[Any] = None
    popen_extra: Dict[str, Any] = {}
    if sys.platform == "win32":
        from mesh.daemon import supervisor as _sup

        run_job = _plat_proc.create_run_job(home, run_id, kill_on_close=False)
        _sup.register_run_job(run_id, run_job, home=home)
        popen_extra["job"] = run_job
    try:
        proc = children.popen_registered(
            "merge_test",
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", run_dir],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=plan["integration"], env=_test_env(run_id), start_new_session=True, close_fds=True,
            **popen_extra,
        )
    except children.SpawnedThenFailed as exc:
        # Round 10 (Linear P1-4): Popen succeeded but registration failed. The wrapper is
        # still our unreaped child: it is held from the orphan reaper and goes through the
        # normal step (exit wait, leftover scan and termination before the reap), so the
        # wrapper and its group are never left running with the run closed.
        proc = exc.proc
        children.hold(proc.pid)
        registration_error = f"{type(exc.exc).__name__}: {exc.exc}" if getattr(exc, "exc", None) else str(exc)
    except BaseException:
        if sys.platform == "win32":
            from mesh.daemon import supervisor as _sup

            _sup.close_run_job(run_id)
        raise
    with _ACTIVE_TEST_LOCK:
        _ACTIVE_TEST_WRAPPERS[run_id] = proc.pid
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
    if sys.platform == "win32":
        from mesh.daemon import supervisor as _sup

        _sup.register_run_job(run_id, run_job, pgid=proc.pid, wrapper_starttime=starttime, home=home)
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
                        proc,
                        grace,
                        force_group_kill=bool(out["termination"].get("scan_uncertain")),
                        run_id=run_id,
                        home=home,
                        wrapper_starttime=starttime,
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
                _kill_test_proc_direct(
                    proc,
                    0.0,
                    force_group_kill=True,
                    run_id=run_id,
                    home=home,
                    wrapper_starttime=starttime,
                )
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
            scan_tok = (
                supervisor._SCAN_RUN_CTX.set((run_id, proc.pid, None, home))
                if sys.platform == "win32"
                else None
            )
            try:
                group_scan = supervisor._scan_group(proc.pid)
                # ClickUp recheck P2: an empty but uncertain scan (EMFILE, EIO) is never read as
                # "no members". Rescan until certain, bounded by the kill grace; the wrapper is
                # still our unreaped child, so its pgid stays reserved meanwhile.
                deadline = time.monotonic() + max(grace, 1.0)
                while getattr(group_scan, "uncertain", False) and time.monotonic() < deadline:
                    time.sleep(terminate.POLL_S)
                    group_scan = supervisor._scan_group(proc.pid)
            finally:
                if scan_tok is not None:
                    supervisor._SCAN_RUN_CTX.reset(scan_tok)
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
        from mesh.daemon import supervisor as _sup

        exit_code, signo = _sup.normalize_exit_info(exit_info)
        out.update({"exit_code": exit_code, "signal": signo})
        if exit_info.get("error"):
            out["error"] = exit_info["error"]
        elif exit_info.get("capture_error"):
            # Blind review P2: the test log could not be written; don't pass blind.
            out["error"] = "capture_error: " + str(exit_info["capture_error"])
    out["passed"] = (not out["timeout"]) and out["exit_code"] == 0 and "error" not in out
    return out


def run_merge_pipeline(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """§10.5 steps 1–4 (the test step since r6). Returns the outcome; never raises.

    Failure details are redacted (§15.2) before they reach merge_json, events, the
    outbox or the error envelope (review m2-r4b-r5 finding 5).
    """
    out = _run_merge_pipeline(plan)
    if out.get("details"):
        out["details"] = redact_merge_value(out["details"])
    return out


def _integrating_commit(integ: str, tip: str) -> str:
    """The commit of ``integ``'s first-parent history that brought ``tip`` in (Carbon P2-5).

    The oldest first-parent commit of HEAD that has ``tip`` as an ancestor: the merge commit
    that merged the task earlier, or the first commit on top of a branch that is simply
    contained in integration. ``tip`` itself when nothing sits on top of it (HEAD is ``tip``)
    or when ``tip`` lies on the first-parent line (nothing merged it)."""
    cp = _git("log integrating commit",
              ["log", "--first-parent", "--ancestry-path", "--format=%H", f"{tip}..HEAD"], integ)
    if cp.returncode != 0:
        raise _GitFailed("log integrating commit", cp)
    shas = [ln.strip() for ln in (cp.stdout or "").splitlines() if ln.strip()]
    if not shas:
        return tip
    # When ``tip`` is itself on the first-parent line (a zero-commit task: its tip is the base
    # it started from), nothing merged it: the oldest commit above it is just the next
    # integration commit (another task's merge), so the tip is recorded, as before P2-5.
    cp = _git("log integrating parents", ["log", "-1", "--format=%P", shas[-1]], integ)
    if cp.returncode != 0:
        raise _GitFailed("log integrating parents", cp)
    parents = (cp.stdout or "").split()
    return tip if parents and parents[0] == tip else shas[-1]


def _run_merge_pipeline(plan: Mapping[str, Any]) -> Dict[str, Any]:
    integ = plan["integration"]
    wt = plan["worktree"]
    branch = plan["branch"]
    repo = plan.get("repo")
    ws_root = plan.get("ws_root")
    run_dir = plan.get("run_dir")
    pre_head: Optional[str] = plan.get("pre_head")
    pre_untracked: Optional[Set[str]] = None
    pre_empty_dirs: Optional[Set[str]] = None
    pre_symref: Optional[str] = None
    pre_head_raw: Optional[str] = None
    pre_index_bytes: Optional[bytes] = None
    committed = False
    noop = False
    try:
        if not _verify_integration_worktree(integ, repo, ws_root):
            return {
                "outcome": "failed",
                "reason": "integration_dirty",
                "pre_head": pre_head,
                "details": {"integration": integ, "foreign_repository": True},
            }
        # Blind review P2: the integration worktree must be on the project's integration
        # branch; on any other branch the merge would advance the wrong ref.
        want = plan.get("integration_branch")
        if want:
            cp = _git("rev-parse integration branch", ["rev-parse", "--symbolic-full-name", "HEAD"], integ)
            if cp.returncode != 0:
                raise _GitFailed("rev-parse integration branch", cp)
            on = (cp.stdout or "").strip()
            if on != f"refs/heads/{want}":
                return {"outcome": "failed", "reason": "integration_dirty", "pre_head": pre_head,
                        "details": {"integration": integ, "wrong_branch": True, "expected_branch": want,
                                    "current": on[len("refs/heads/"):] if on.startswith("refs/heads/") else on}}
        # The lease is held now, so this read is the authoritative pre_head.
        pre_head = _head(integ, "rev-parse integration HEAD")
        # Full review r1 P1-4: `workspace delete` removes the worktree but keeps the branch
        # (Q7); the branch is what is merged, so a deleted worktree is not an error. The
        # tip is still bound to the reviewed head below.
        wt_clean = _plat_paths.strip_trailing_seps(wt) if isinstance(wt, str) else ""
        if _plat_paths.is_fs_root(wt_clean):
            wt_clean = ""
        if wt_clean and os.path.lexists(wt_clean):
            from mesh.core.paths import resolve_trusted_path
            from mesh.core.repos import is_registered_worktree
            wt_real = resolve_trusted_path(wt_clean)
            if wt_real is None or (
                not _plat_paths.same_path(wt_real, integ)
                and not is_registered_worktree(wt_real, integ)
            ):  # integ: a meshd-owned worktree of the repo
                # Full review r5 P1-3: never run git in a worktree the task re-pointed.
                return {"outcome": "failed", "reason": "worktree_dirty", "pre_head": pre_head,
                        "details": {"worktree": wt, "foreign_repository": True}}
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
        # Full review r1 P0-1: merge exactly the reviewed work. A branch moved after submit
        # (commits added after accept, or work committed on a detached HEAD/another branch so
        # the branch never got it) is refused; changing the work goes through reject/rework.
        if "expected_head" in plan and plan.get("expected_head") != tip:
            return {"outcome": "failed", "reason": "worktree_dirty", "pre_head": pre_head,
                    "details": {"branch": branch, "head_mismatch": True,
                                "expected_head": plan.get("expected_head"), "actual_head": tip}}
        cp = _git("merge-base --is-ancestor", ["merge-base", "--is-ancestor", tip, "HEAD"], integ)
        if cp.returncode not in (0, 1):
            raise _GitFailed("merge-base --is-ancestor", cp)
        # Carbon P2-5: a task branch already contained in integration (a zero-commit task, or
        # a merge commit that exists but was never recorded) merges nothing, but the gate is
        # not skipped: with a test_command it runs below, on the integration tree, under the
        # same snapshot/restore and git-state guards as a real merge, and no commit is made.
        # The recorded merge_commit is the commit that really carries the task.
        noop = cp.returncode == 0
        if noop and not plan.get("test_argv"):
            return {"outcome": "merged", "noop": True, "pre_head": pre_head, "tip": tip,
                    "merge_commit": _integrating_commit(integ, tip), "tests": "none", "test_log": None}

        pre_symref = _head_symref(integ, "rev-parse pre-merge HEAD symref")
        head_path = _git_path(integ, "HEAD", "rev-parse pre-merge HEAD path")
        if head_path and not _plat_paths.is_reparse_link(head_path) and os.path.isfile(head_path):
            try:
                pre_head_raw = _plat_fs.read_bytes_shared(head_path).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                pre_head_raw = None
        idx_path = _git_path(integ, "index", "rev-parse pre-merge index path")
        if idx_path and not _plat_paths.is_reparse_link(idx_path) and os.path.isfile(idx_path):
            try:
                pre_index_bytes = _plat_fs.read_bytes_shared(idx_path)
            except OSError:
                pre_index_bytes = None
        pre_shared_bytes = None
        sh_name = _index_bytes_shared_name(pre_index_bytes)
        if idx_path and sh_name:
            sh_path = os.path.join(os.path.dirname(idx_path), sh_name)
            if os.path.isfile(sh_path) and not _plat_paths.is_reparse_link(sh_path):
                try:
                    pre_shared_bytes = _plat_fs.read_bytes_shared(sh_path)
                except OSError:
                    pre_shared_bytes = None

        pre_untracked = set(_untracked_paths(integ, "snapshot pre-merge untracked"))
        _save_pre_untracked(run_dir, pre_untracked)
        pre_empty_dirs = _snapshot_empty_dirs(integ)
        _save_pre_empty_dirs(run_dir, pre_empty_dirs)
        _save_pre_merge_state(
            run_dir,
            task_id=plan["task_id"],
            pre_head=pre_head,
            tip=tip,
            symref=pre_symref,
            head_raw=pre_head_raw,
            index_bytes=pre_index_bytes,
            shared_bytes=pre_shared_bytes,
        )

        if not noop:
            cp = _git("merge", ["merge", "--no-ff", "--no-commit", tip], integ, GIT_MERGE_TIMEOUT_S)
            if cp.returncode != 0:
                files_cp = _git("diff unmerged", ["diff", "--name-only", "--diff-filter=U"], integ)
                files = [f for f in (files_cp.stdout or "").splitlines() if f.strip()] if files_cp.returncode == 0 else []
                stderr = _clip(cp.stderr) or _clip(cp.stdout)
                verify = _abort_and_verify(
                    integ,
                    pre_head,
                    pre_untracked=pre_untracked,
                    pre_empty_dirs=pre_empty_dirs,
                    run_dir=run_dir,
                    pre_index_bytes=pre_index_bytes,
                    pre_symref=pre_symref,
                    pre_head_raw=pre_head_raw,
                    noop=noop,
                    repo=repo,
                    ws_root=ws_root,
                )
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
        pre_test_staged: Optional[Dict[str, Tuple[str, str, str, str, str]]] = None
        pre_test_empty_dirs: Optional[Set[str]] = None
        pre_test_flags: Optional[Dict[str, Tuple[bool, bool, bool]]] = None
        if plan.get("test_argv"):
            pre_test_untracked = set(_untracked_paths(integ, "snapshot post-merge untracked"))
            pre_test_empty_dirs = _snapshot_empty_dirs(integ)
            pre_test_staged = _staged_index_snapshot(integ, "snapshot post-merge staged index")
            pre_test_flags = _read_index_flags(integ, "snapshot post-merge index flags")
            try:
                test = run_test_step(plan)
            finally:
                if _verify_integration_worktree(integ, repo, ws_root):
                    _cleanup_new_untracked(
                        integ,
                        pre_test_untracked,
                        pre_empty_dirs=pre_test_empty_dirs,
                        protected_refs=(),
                        include_index=True,
                    )
            tests = {"tests": "passed" if test["passed"] else "failed", "test_log": test["log_path"],
                     "test": {k: v for k, v in test.items() if k != "log_path"}}
            if not test["passed"]:
                verify = _abort_and_verify(
                    integ,
                    pre_head,
                    pre_untracked=pre_untracked,
                    pre_empty_dirs=pre_empty_dirs,
                    run_dir=run_dir,
                    pre_index_bytes=pre_index_bytes,
                    pre_symref=pre_symref,
                    pre_head_raw=pre_head_raw,
                    noop=noop,
                    repo=repo,
                    ws_root=ws_root,
                )
                tail, tail_cut = log_tail_capped(test["log_path"])
                details = {"log_path": test["log_path"], "log_tail": tail, "log_tail_truncated": tail_cut,
                           "timeout": test["timeout"], "timeout_s": test["timeout_s"],
                           "exit_code": test["exit_code"], "signal": test["signal"]}
                fails, n_fails = failing_tests(test["log_path"])
                details.update({"failing_tests": fails, "failing_tests_total": n_fails,
                                "failing_tests_truncated": n_fails > len(fails)})
                for k in ("error", "termination", "leftovers", "leftover_scan_uncertain"):
                    if k in test:
                        details[k] = test[k]
                if (test.get("termination") or {}).get("kill_failed"):
                    details["kill_failed"] = True
                details.update(verify)
                return {"outcome": "failed", "reason": "tests_failed", "pre_head": pre_head,
                        "details": details, **tests}
            if not _verify_integration_worktree(integ, repo, ws_root):
                details = _capped(
                    {
                        "integration": integ,
                        "foreign_repository": True,
                        "restored": False,
                        "error": "dirty_after_tests",
                        "log_path": test["log_path"],
                        "exit_code": test["exit_code"],
                    },
                    "dirty_files",
                    [".git"],
                )
                return {
                    "outcome": "failed",
                    "reason": "integration_dirty",
                    "pre_head": pre_head,
                    "details": details,
                    **tests,
                }
            post_dirty = _post_test_dirty_paths(
                integ,
                pre_test_untracked,
                pre_empty_dirs=pre_test_empty_dirs,
                pre_staged=pre_test_staged,
                pre_head=pre_head,
                pre_flags=pre_test_flags,
                pre_symref=pre_symref,
            )
            if post_dirty:
                verify = _abort_and_verify(
                    integ,
                    pre_head,
                    pre_untracked=pre_untracked,
                    pre_empty_dirs=pre_empty_dirs,
                    run_dir=run_dir,
                    pre_index_bytes=pre_index_bytes,
                    pre_symref=pre_symref,
                    pre_head_raw=pre_head_raw,
                    noop=noop,
                    repo=repo,
                    ws_root=ws_root,
                )
                details = _capped(
                    {
                        "integration": integ,
                        "error": "dirty_after_tests",
                        "log_path": test["log_path"],
                        "exit_code": test["exit_code"],
                    },
                    "dirty_files",
                    post_dirty,
                )
                details.update(verify)
                return {"outcome": "failed", "reason": "integration_dirty", "pre_head": pre_head,
                        "details": details, **tests}

        if plan.get("test_argv"):
            guard_dirty: List[str] = []
            guard_seen: Set[str] = set()

            def _add_guard(path: str) -> None:
                if path not in guard_seen:
                    guard_dirty.append(path)
                    guard_seen.add(path)

            cur_head = _head(integ, "verify pre-commit HEAD")
            if pre_head and cur_head != pre_head:
                dcp = _git("diff rogue commits", ["diff", "--raw", "--no-renames", "-z", pre_head, cur_head], integ)
                changed = False
                if dcp.returncode == 0:
                    for _om, _st, rel in _parse_diff_raw_z(dcp.stdout or ""):
                        changed = True
                        _add_guard(rel)
                if not changed:
                    _add_guard(".git/HEAD")
            if pre_symref and pre_symref.startswith("refs/heads/"):
                if _head_symref(integ, "verify pre-commit symref") != pre_symref:
                    _add_guard(".git/HEAD")
            # A noop merge has no MERGE_HEAD of its own: one now is the test's `git merge`.
            merge_head_bad = _merge_head_exists(integ) if noop else not _merge_head_matches_tip(integ, tip)
            if merge_head_bad:
                _add_guard(".git/MERGE_HEAD")
            elif not (pre_head and cur_head != pre_head):
                cur_staged = _staged_index_snapshot(integ, "verify pre-commit staged index")
                if cur_staged != pre_test_staged:
                    for p in sorted(set(cur_staged) | set(pre_test_staged or {})):
                        if cur_staged.get(p) != (pre_test_staged or {}).get(p):
                            _add_guard(p)
            cur_flags = _read_index_flags(integ, "verify pre-commit index flags")
            if cur_flags is None:
                _add_guard(".git/index")
            else:
                base_flags = pre_test_flags or {}
                for p in sorted(cur_flags):
                    au, sw, _ita = cur_flags[p]
                    pre_au, pre_sw, _pre_ita = base_flags.get(p, (False, False, False))
                    if (au and not pre_au) or (sw and not pre_sw) or (pre_test_flags is None and (au or sw)):
                        _add_guard(p)
            guard_gl = _gitlink_paths(integ, refs=(), include_index=True)
            guard_cleanable_gl, _ = _cleanable_and_exempt_gitlinks(integ, guard_gl, pre_test_empty_dirs)
            for p in sorted(_tampered_gitlink_paths(integ, guard_gl)):
                _add_guard(p)
            for p in _non_empty_cleanable_gitlinks(integ, guard_cleanable_gl):
                _add_guard(p)
            if guard_dirty:
                verify = _abort_and_verify(
                    integ,
                    pre_head,
                    pre_untracked=pre_untracked,
                    pre_empty_dirs=pre_empty_dirs,
                    run_dir=run_dir,
                    pre_index_bytes=pre_index_bytes,
                    pre_symref=pre_symref,
                    pre_head_raw=pre_head_raw,
                    noop=noop,
                    repo=repo,
                    ws_root=ws_root,
                )
                details = _capped(
                    {
                        "integration": integ,
                        "error": "dirty_after_tests",
                        "log_path": tests.get("test_log"),
                        "exit_code": (tests.get("test") or {}).get("exit_code"),
                    },
                    "dirty_files",
                    guard_dirty,
                )
                details.update(verify)
                return {"outcome": "failed", "reason": "integration_dirty", "pre_head": pre_head,
                        "details": details, **tests}

        if noop:
            return {"outcome": "merged", "noop": True, "pre_head": pre_head, "tip": tip,
                    "merge_commit": _integrating_commit(integ, tip), **tests}

        msg = f"Merge mesh task {plan['task_id']}: {plan['title']}"
        try:
            _save_commit_ready(
                run_dir,
                task_id=plan["task_id"],
                pre_head=pre_head,
                tip=tip,
                symref=pre_symref,
                message=msg,
            )
        except OSError as exc:
            # Carbon P1-3: never commit without a durable intent; a crash after the commit
            # could not be recognised. The merge is aborted and reported as a git step failure.
            _unlink_commit_ready(run_dir)
            verify = _abort_and_verify(
                integ,
                pre_head,
                pre_untracked=pre_untracked,
                pre_empty_dirs=pre_empty_dirs,
                run_dir=run_dir,
                pre_index_bytes=pre_index_bytes,
                pre_symref=pre_symref,
                pre_head_raw=pre_head_raw,
                repo=repo,
                ws_root=ws_root,
            )
            details = {"step": "write commit_ready", "error": f"{type(exc).__name__}: {exc}"}
            details.update(verify)
            return {"outcome": "failed", "reason": "git_failed", "pre_head": pre_head, "details": details,
                    **tests}
        cp = _git("commit", ["commit", "--no-edit", "--cleanup=verbatim", "-m", msg], integ, GIT_MERGE_TIMEOUT_S)
        if cp.returncode != 0:
            _unlink_commit_ready(run_dir)
            stderr = _clip(cp.stderr) or _clip(cp.stdout)
            verify = _abort_and_verify(
                integ,
                pre_head,
                pre_untracked=pre_untracked,
                pre_empty_dirs=pre_empty_dirs,
                run_dir=run_dir,
                pre_index_bytes=pre_index_bytes,
                pre_symref=pre_symref,
                pre_head_raw=pre_head_raw,
                noop=noop,
                repo=repo,
                ws_root=ws_root,
            )
            details = {"stderr": stderr, "returncode": cp.returncode}
            details.update(verify)
            return {"outcome": "failed", "reason": "commit_failed", "pre_head": pre_head, "details": details,
                    **tests}
        committed = True
        merge_commit = _head(integ, "rev-parse merge commit")
        _maybe_crash_after_commit(plan)
        return {"outcome": "merged", "noop": False, "pre_head": pre_head, "merge_commit": merge_commit, **tests}
    except _GitFailed as gf:
        out: Dict[str, Any] = {"outcome": "failed", "reason": "git_failed", "pre_head": pre_head,
                               "details": dict(gf.details)}
        if pre_head and not committed:
            _unlink_commit_ready(run_dir)
            try:
                if not _verify_integration_worktree(integ, repo, ws_root):
                    out["details"].update({"restored": False, "foreign_repository": True})
                elif _merge_head_present(integ) or pre_untracked is not None:
                    out["details"].update(_abort_and_verify(
                        integ,
                        pre_head,
                        pre_untracked=pre_untracked,
                        pre_empty_dirs=pre_empty_dirs,
                        run_dir=run_dir,
                        pre_index_bytes=pre_index_bytes,
                        pre_symref=pre_symref,
                        pre_head_raw=pre_head_raw,
                        noop=noop,
                        repo=repo,
                        ws_root=ws_root,
                    ))
            except _GitFailed:
                pass
        return out
    except Exception as exc:  # never leave the task in `merging` because of a bug here
        out = {"outcome": "failed", "reason": "git_failed", "pre_head": pre_head,
               "details": {"error": f"{type(exc).__name__}: {exc}"}}
        # Linear full review B2: an unexpected error after `merge --no-commit` (e.g. the
        # test wrapper failing to spawn) must not leave MERGE_HEAD and staged changes
        # behind, or every later merge fails INTEGRATION_DIRTY. Same cleanup as git errors.
        if pre_head and not committed:
            _unlink_commit_ready(run_dir)
            try:
                if not _verify_integration_worktree(integ, repo, ws_root):
                    out["details"].update({"restored": False, "foreign_repository": True})
                elif _merge_head_present(integ) or pre_untracked is not None:
                    out["details"].update(_abort_and_verify(
                        integ,
                        pre_head,
                        pre_untracked=pre_untracked,
                        pre_empty_dirs=pre_empty_dirs,
                        run_dir=run_dir,
                        pre_index_bytes=pre_index_bytes,
                        pre_symref=pre_symref,
                        pre_head_raw=pre_head_raw,
                        noop=noop,
                        repo=repo,
                        ws_root=ws_root,
                    ))
            except Exception as cleanup_exc:
                out["details"]["cleanup_error"] = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        return out


# ---------------------------------------------------------------------------
# Testing-only crash hook (§17.1; m2-r7)
# ---------------------------------------------------------------------------

CRASH_AFTER_MERGE_COMMIT = "after_merge_commit"
CRASH_AFTER_MERGE_ABORT = "after_merge_abort"


def _maybe_crash_after_commit(plan: Mapping[str, Any]) -> None:
    """``MESH_TEST_CRASH_AT=after_merge_commit`` (testing mode only): SIGKILL meshd right
    after the merge commit, before T15, like `kill -9` (no cleanup, no flush)."""
    from mesh.daemon import supervisor

    if not supervisor._testing_mode() or os.environ.get("MESH_TEST_CRASH_AT") != CRASH_AFTER_MERGE_COMMIT:
        return
    home = os.path.dirname(os.path.dirname(_plat_paths.strip_trailing_seps(plan["run_dir"])))
    supervisor._log_meshd(home, f"MESH_TEST_CRASH_AT={CRASH_AFTER_MERGE_COMMIT}: killing meshd during "
                                f"merge {plan['merge_id']} of task {plan['task_id']}")
    crash_self_now()


def _maybe_crash_after_merge_abort(run_dir: Optional[str]) -> None:
    """``MESH_TEST_CRASH_AT=after_merge_abort`` (testing mode only): SIGKILL meshd right
    after ``git merge --abort`` clears MERGE_HEAD, before post-abort restore/cleanup."""
    from mesh.daemon import supervisor

    if not supervisor._testing_mode() or os.environ.get("MESH_TEST_CRASH_AT") != CRASH_AFTER_MERGE_ABORT:
        return
    if run_dir and isinstance(run_dir, str):
        home = os.path.dirname(os.path.dirname(_plat_paths.strip_trailing_seps(run_dir)))
        if home:
            try:
                supervisor._log_meshd(
                    home,
                    f"MESH_TEST_CRASH_AT={CRASH_AFTER_MERGE_ABORT}: killing meshd after merge --abort",
                )
            except Exception:
                pass
    crash_self_now()


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


def _merge_run_home(conn: sqlite3.Connection, run: Mapping[str, Any]) -> Optional[str]:
    run_dir = run["run_dir"] if "run_dir" in run.keys() else None
    if isinstance(run_dir, str) and run_dir.strip():
        parent = os.path.dirname(os.path.dirname(_plat_paths.strip_trailing_seps(run_dir)))
        if parent:
            return parent
    try:
        db = next((row for row in conn.execute("PRAGMA database_list;") if row["name"] == "main"), None)
        if db is not None and db["file"]:
            return os.path.dirname(db["file"])
    except Exception:
        pass
    return None


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
            id_pid, id_wst = supervisor.close_identity(run)
            known_pid = pid if pid is not None else id_pid
            known_wst = starttime if pid is not None else id_wst
            win_exit_members: Dict[int, int] = {}
            if sys.platform == "win32":
                run_home = _merge_run_home(conn, run)
                if run_home:
                    supervisor.record_run_home(run_id, run_home)
                    supervisor.register_pgid_run(run_id, known_pid, known_wst, run_home)
                    try:
                        rjob = _plat_proc.open_run_job(run_home, run_id, terminate=True)
                    except (OSError, ValueError) as job_exc:
                        _log_recovery_error(conn, run, redact(f"open Win32 Named Run Job failed: {job_exc}"))
                        rjob = None
                    if rjob is not None:
                        supervisor.register_run_job(
                            run_id,
                            rjob,
                            pgid=known_pid,
                            wrapper_starttime=known_wst,
                            home=run_home,
                        )
                for mpid, mst in supervisor._win_exit_job_member_pairs(run):
                    if supervisor._identity_live(mpid, mst):
                        win_exit_members[int(mpid)] = int(mst)
            result: Dict[str, Any] = {"reason": "interrupted", "recovered": True}
            if pid is not None:
                result["wrapper"] = {"pid": pid, "starttime": starttime}
            carriers = {} if pid is not None else terminate.run_targets_or_recheck(run_id, None, None, known_wst)
            if pid is None and win_exit_members:
                merged_carriers = ScanResult(
                    dict(carriers or {}),
                    uncertain=bool(getattr(carriers, "uncertain", False)),
                )
                merged_carriers.update(win_exit_members)
                carriers = merged_carriers
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


class LiveMergeHeld(Exception):
    """The live ``task merge`` close gate kept the ``merge_test`` run open (uncertain
    process scan or occupied group). This is a defined state, not an internal error
    (dogfood): the run and ``merge:<prj>`` stay held, the task stays ``merging``, a
    ``merge.recovery_blocked`` event is recorded, and the next meshd boot re-checks the
    run (§11.7 step 2) and decides the task (step 5). The caller gets exit 14
    ``MERGE_HELD`` with the test outcome and the recovery steps."""

    def __init__(self, plan: Mapping[str, Any], result: Mapping[str, Any], cause: BaseException) -> None:
        self.plan = plan
        self.result = result
        self.cause_text = redact(f"{type(cause).__name__}: {cause}")
        super().__init__(self.cause_text)

    def to_mesh_error(self) -> MeshError:
        test = self.result.get("test") or {}
        merge_commit = self.result.get("merge_commit")
        committed = self.result.get("outcome") == "merged" and merge_commit and not self.result.get("noop")
        if committed:
            # PR #37 review #8: held after the pipeline committed the merge.
            merged_text = (f"The merge commit {str(merge_commit)[:12]} is already on the integration branch, "
                           f"but the task stays 'merging'")
        else:
            merged_text = "Nothing was merged; the task stays 'merging'"
        log_path = self.result.get("test_log") or test.get("log_path")  # review #9
        return MeshError(
            code=c.MERGE_HELD,
            exit_code=c.EXIT_MERGE_FAILED,
            message=(f"Merge of task '{self.plan['task_id']}' is held: the merge test run "
                     f"{self.plan.get('run_id')} ended (exit code {test.get('exit_code')}) but it could not "
                     f"be closed safely ({self.cause_text}). {merged_text} "
                     f"and the project merge lock stays held. Stop any leftover processes "
                     f"of that run, then restart meshd (mesh daemon stop --user; mesh daemon start): "
                     f"boot recovery re-checks the run and decides the task."),
            details={"task_id": self.plan["task_id"], "merge_id": self.plan.get("merge_id"),
                     "run_id": self.plan.get("run_id"), "reason": "close_held",
                     "cause": self.cause_text, "test_exit_code": test.get("exit_code"),
                     "test_passed": test.get("passed"), "log_path": log_path,
                     "merge_commit": merge_commit if committed else None},
        )


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


def _win_job_live_pids(run_id: str, home: Optional[str]) -> Tuple[Set[int], bool]:
    """Return ``(pids, uncertain)`` for any live processes inside Win32 Named Run Job *run_id*."""
    from mesh.daemon import supervisor

    try:
        job, should_close = supervisor.resolve_run_job(run_id, home=home, terminate=False)
    except OSError:
        return set(), True
    if job is None:
        return set(), False
    try:
        try:
            return {int(p) for p in _plat_proc.query_job_pids(job) if int(p) > 0}, False
        except OSError:
            return set(), True
    finally:
        if should_close:
            try:
                job.close()
            except Exception:
                pass


def merge_close_gate(conn: sqlite3.Connection, run: Mapping[str, Any], pid: Optional[int],
                     starttime: Optional[int]) -> None:
    """Round 7 / Carbon RO P1-1: the close-safety gate for a merge_test run (live or
    recovered). Identity: the live-wrapper check's pid, else the DB or wrapper.json
    (``close_identity``). A live wrapper was already terminated (two-scan rule) before
    this point; if the group is occupied or scan-uncertain and still the run's, the close
    is refused (:class:`MergeRunHeld`) and the disk identity is recorded in the DB."""
    from mesh.daemon import supervisor

    if pid is not None:
        wrapper_pgid, wrapper_wst = pid, starttime
    else:
        wrapper_pgid, wrapper_wst = supervisor.close_identity(run)
    eff_home: Optional[str] = None
    occupied = False
    held_pid: Optional[int] = int(wrapper_pgid) if wrapper_pgid else None
    if sys.platform == "win32":
        eff_home = _merge_run_home(conn, run) or supervisor.get_run_home(run["id"])
        if eff_home:
            supervisor.record_run_home(run["id"], eff_home)
        for mpid, mst in supervisor._win_exit_job_member_pairs(run):
            if supervisor._identity_live(mpid, mst):
                occupied = True
                if not held_pid:
                    held_pid = int(mpid)
                break
    if not occupied and wrapper_pgid and supervisor.group_still_ours(wrapper_pgid, wrapper_wst):
        if sys.platform == "win32":
            supervisor.register_pgid_run(run["id"], wrapper_pgid, wrapper_wst, eff_home)
            scan_tok = supervisor._SCAN_RUN_CTX.set((run["id"], int(wrapper_pgid), wrapper_wst, eff_home))
            try:
                scanned = supervisor._scan_group(int(wrapper_pgid))
            finally:
                supervisor._SCAN_RUN_CTX.reset(scan_tok)
        else:
            scanned = supervisor._scan_group(int(wrapper_pgid))
        occupied = bool(scanned) or getattr(scanned, "uncertain", False)
    if not occupied and sys.platform == "win32":
        job_pids, job_uncertain = _win_job_live_pids(run["id"], eff_home)
        if job_pids or job_uncertain:
            occupied = True
            if not held_pid and job_pids:
                held_pid = next(iter(sorted(job_pids)))
    if not occupied:
        return
    if wrapper_pgid is not None:
        sql = ("UPDATE runs SET wrapper_pid = COALESCE(wrapper_pid, ?), pgid = COALESCE(pgid, ?), "
               "proc_starttime = COALESCE(proc_starttime, ?) WHERE id = ?;")
        if conn.in_transaction:
            conn.execute(sql, (wrapper_pgid, wrapper_pgid, wrapper_wst, run["id"]))
        else:
            with transaction(conn):
                conn.execute(sql, (wrapper_pgid, wrapper_pgid, wrapper_wst, run["id"]))
    raise MergeRunHeld(run["id"], int(held_pid or 0))


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
            (status, now_iso_utc(), json.dumps(redact_merge_value(result)), run_id),
        )
        if pid is not None:
            conn.execute("UPDATE runs SET wrapper_pid = COALESCE(wrapper_pid, ?), pgid = COALESCE(pgid, ?), "
                         "proc_starttime = COALESCE(proc_starttime, ?) WHERE id = ?;",
                         (pid, pid, starttime, run_id))
        conn.execute("DELETE FROM leases WHERE key = ? AND run_id = ?;",
                     (merge_lease_key(run["project_id"]), run_id))
    if sys.platform == "win32":
        from mesh.daemon import supervisor

        supervisor.close_run_job(run_id)
    return status


def _norm_commit_msg(msg: str) -> str:
    """Line-ending-insensitive form for comparing a commit body with ``commit_ready``.

    The commit is written with ``--cleanup=verbatim`` (review PR45 R5 second review
    P2), but git output is read in text mode, which turns CR/CRLF into LF.
    """
    return msg.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


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


def decide_recovery(
    integ: str,
    task_id: str,
    branch: Optional[str],
    pre_head: Optional[str],
    run_dir: Optional[str] = None,
    pre_untracked: Optional[Set[str]] = None,
    *,
    repo: Optional[str] = None,
    ws_root: Optional[str] = None,
) -> Dict[str, Any]:
    """§11.7 step 5 git decision for a task left in ``merging``.

    - ``MERGE_HEAD`` exists → ``merge --abort`` (then verify) → ``interrupted``;
    - HEAD is a merge commit whose parents are exactly ``[pre_head, tip]`` of the durable
      ``commit_ready.json`` written (fsynced) right before ``git commit``, whose message is
      the recorded one, on the recorded HEAD symref, with the task branch still at ``tip``
      → reconcile ``merged`` (``matched_by: commit_ready``). Only for a run without any
      pipeline snapshot (made before snapshots existed) the old rule applies: HEAD is a merge
      commit whose second parent is the task branch tip and whose message contains the task
      id (``matched_by: message``);
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
    if repo is None and ws_root is None:
        expected = _RECOVERY_EXPECTED.get()
        if expected is not None:
            repo, ws_root = expected
    if not _verify_integration_worktree(integ, repo, ws_root):
        return {
            "outcome": "failed",
            "reason": "interrupted",
            "pre_head": pre_head,
            "details": {"integration": integ, "foreign_repository": True, "restored": False},
        }
    details: Dict[str, Any] = {}
    current_pre = pre_head
    try:
        pre_merge_meta = _load_pre_merge_state(run_dir)
        pre_merge_idx = _load_pre_merge_index(run_dir)
        commit_ready = _load_commit_ready(run_dir)
        has_pipeline_snapshot = (
            pre_merge_meta is not None
            or pre_merge_idx is not None
            or _has_pipeline_snapshot(run_dir)
        )
        loaded_pre_untracked = (
            pre_untracked if pre_untracked is not None else _load_pre_untracked(run_dir)
        )
        loaded_pre_empty = _load_pre_empty_dirs(run_dir)

        if _merge_head_present(integ):
            details["merge_head"] = True
            true_pre = (
                (pre_merge_meta or {}).get("pre_head")
                or _head(integ, "rev-parse HEAD before abort")
            )
            current_pre = true_pre
            if pre_head and pre_head != true_pre:
                details["recorded_pre_head"] = pre_head
            effective_pre = loaded_pre_untracked if loaded_pre_untracked is not None else set()
            details.update(_abort_and_verify(
                integ,
                true_pre,
                pre_untracked=effective_pre,
                pre_empty_dirs=loaded_pre_empty,
                run_dir=run_dir,
                pre_index_bytes=pre_merge_idx,
                pre_symref=(pre_merge_meta or {}).get("symref"),
                pre_head_raw=(pre_merge_meta or {}).get("head_raw"),
                repo=repo,
                ws_root=ws_root,
            ))
            return {"outcome": "failed", "reason": "interrupted", "pre_head": true_pre, "details": details}

        head = _head(integ, "rev-parse HEAD")
        current_pre = head
        parents, body = _inspect_head(integ, head)
        tip = _branch_tip(integ, branch)

        reconcile_ok = False
        matched_by = None
        if commit_ready is not None:
            cr_task = commit_ready.get("task_id")
            cr_pre = commit_ready.get("pre_head")
            cr_tip = commit_ready.get("tip")
            cr_sym = commit_ready.get("symref")
            cr_msg = commit_ready.get("message")
            cur_symref = _head_symref(integ, "verify recovered HEAD symref")
            # Port note (Carbon P1-3 vs batch7): the parents must be exactly the durable
            # (pre_head, tip). The private repo keeps its stricter batch7 rule on top: the
            # message and the current branch tip must match too. A commit that fails any check
            # (a hook-rewritten message, a branch that moved on) is not reconciled: it is rolled
            # back to pre_head and the task ends merge_failed/interrupted, fail closed (no
            # unrecorded work stays in integration; the task branch keeps its commits).
            if (
                cr_task == task_id
                and len(parents) == 2
                and bool(cr_pre)
                and bool(cr_tip)
                and head != cr_pre
                and parents[0] == cr_pre
                and parents[1] == cr_tip
                and tip is not None
                and tip == cr_tip
                and body.startswith(f"Merge mesh task {task_id}:")
                and (not cr_msg or _norm_commit_msg(body) == _norm_commit_msg(str(cr_msg)))
                and (not cr_sym or cur_symref == cr_sym)
            ):
                reconcile_ok = True
                matched_by = "commit_ready"
        elif not has_pipeline_snapshot:
            if len(parents) >= 2 and tip is not None and parents[1] == tip and task_id in body:
                reconcile_ok = True
                matched_by = "message"

        if reconcile_ok:
            return {"outcome": "merged", "noop": False, "pre_head": parents[0],
                    "merge_commit": head, "matched_by": matched_by,
                    **({"recorded_pre_head": pre_head} if pre_head and pre_head != parents[0] else {})}

        if pre_merge_meta is not None or pre_merge_idx is not None:
            true_pre = (pre_merge_meta or {}).get("pre_head") or pre_head or head
            current_pre = true_pre
            if pre_head and pre_head != true_pre:
                details["recorded_pre_head"] = pre_head
            if head != true_pre:
                details.update({"head": head, "head_moved": True, "merge_commit_match": False})
            effective_pre = loaded_pre_untracked if loaded_pre_untracked is not None else set()
            details.update(_abort_and_verify(
                integ,
                true_pre,
                pre_untracked=effective_pre,
                pre_empty_dirs=loaded_pre_empty,
                run_dir=run_dir,
                pre_index_bytes=pre_merge_idx,
                pre_symref=(pre_merge_meta or {}).get("symref"),
                pre_head_raw=(pre_merge_meta or {}).get("head_raw"),
                repo=repo,
                ws_root=ws_root,
            ))
            return {"outcome": "failed", "reason": "interrupted", "pre_head": true_pre, "details": details}

        true_pre = head
        head_moved = False
        if parents and (task_id in body or (tip is not None and tip in parents[1:])):
            # Do not claim a mismatched task commit was restored just because its tree
            # is clean. Use its current parent, not the pre-lease probe, as the baseline.
            true_pre = parents[0]
            head_moved = True
            details.update({"head": head, "head_moved": True, "merge_commit_match": False})
        if pre_head and pre_head != true_pre:
            details["recorded_pre_head"] = pre_head
        current_pre = true_pre
        pre_check_dirty = _dirty_paths(integ, "verify status")
        gitlinks_pre = _gitlink_paths(integ, refs=(true_pre,), include_index=True)
        cleanable_pre, exempt_pre = _cleanable_and_exempt_gitlinks(integ, gitlinks_pre, loaded_pre_empty)
        non_empty_gl_pre = _non_empty_cleanable_gitlinks(integ, cleanable_pre)
        if (
            head == true_pre
            and not head_moved
            and (loaded_pre_untracked is not None or bool(pre_check_dirty) or bool(non_empty_gl_pre))
        ):
            integ_real = os.path.realpath(integ) if os.path.isdir(integ) else None
            skip_reals = _gitlink_skip_paths_real(integ, exempt_pre)
            if loaded_pre_untracked is not None and integ_real:
                make_owned_dirs_writable(
                    integ,
                    boundary_real=integ_real,
                    skip_names={".git"},
                    skip_paths_real=skip_reals,
                )
            _clear_index_hide_flags(integ, "clear hide flags in no-merge-head recovery")
            _restore_tracked_from_ref(integ, ref=true_pre)
            _cleanup_inside_empty_gitlinks(integ, cleanable_pre)
            if loaded_pre_untracked is not None:
                _cleanup_new_untracked(
                    integ,
                    loaded_pre_untracked,
                    pre_empty_dirs=loaded_pre_empty,
                    protected_refs=(true_pre,),
                    include_index=True,
                )
            dirty = _dirty_paths(integ, "verify status")
        else:
            dirty = list(pre_check_dirty)
        if head == true_pre:
            gitlinks = gitlinks_pre
            cleanable_gl, _exempt_gl = _cleanable_and_exempt_gitlinks(integ, gitlinks, loaded_pre_empty)
            seen_dirty = set(dirty)
            for rel in _non_empty_cleanable_gitlinks(integ, cleanable_gl):
                if rel not in seen_dirty:
                    dirty.append(rel)
                    seen_dirty.add(rel)
            if loaded_pre_empty is not None:
                for d in sorted(_snapshot_empty_dirs(integ)):
                    if d not in loaded_pre_empty and not _is_under_gitlink(d, gitlinks) and d not in seen_dirty:
                        dirty.append(d)
                        seen_dirty.add(d)
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


def _prepare_merging_recovery(conn: sqlite3.Connection, home: str, task: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Load recovery context for a task stuck in ``merging``, or None if its run is still open."""
    try:
        mj = json.loads(task["merge_json"] or "{}")
    except (TypeError, ValueError):
        mj = {}
    run_id = mj.get("run_id")
    run = conn.execute("SELECT status, result_json, run_dir FROM runs WHERE id = ?;", (run_id,)).fetchone() if run_id else None
    if run is not None and run["status"] in ("starting", "running"):
        # Step 2 could not close this task's merge_test run (it raised): its processes may
        # still be live, so the task stays `merging` with its lease, and the next boot
        # retries (review m2-r7 finding 5).
        return None
    run_termination: Dict[str, Any] = {}
    if run is not None:
        try:
            run_termination = (json.loads(run["result_json"] or "{}") or {}).get("termination") or {}
        except (TypeError, ValueError, AttributeError):
            run_termination = {}
    project = conn.execute("SELECT * FROM projects WHERE id = ?;", (task["project_id"],)).fetchone()
    ws_root = get_workspaces_root(conn, home)
    integ = integration_worktree_path(ws_root, task["project_id"])
    return {
        "task": task,
        "mj": mj,
        "project": project,
        "repo": project["repo_path"] if project is not None else None,
        "ws_root": ws_root,
        "integ": integ,
        "pre_head": mj.get("pre_head"),
        "run_dir": run["run_dir"] if run is not None else None,
        "run_termination": run_termination,
    }


def _finish_merging_recovery(
    conn: sqlite3.Connection,
    home: str,
    rec_ctx: Dict[str, Any],
    result: Dict[str, Any],
) -> str:
    """Commit the recovered merge outcome via ``_final_tx`` and return its status string."""
    task = rec_ctx["task"]
    mj = rec_ctx["mj"]
    project = rec_ctx["project"]
    pre_head = rec_ctx["pre_head"]
    run_termination = rec_ctx["run_termination"]
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
        result["details"] = redact_merge_value(result["details"])
    plan = {"task_id": task["id"], "project_id": task["project_id"], "merge_id": mj.get("mrg"),
            "run_id": mj.get("run_id"), "pre_head": pre_head,
            "started_at": mj.get("started_at") or task["updated_at"]}
    extra: Dict[str, Any] = {"recovered": True, "reconciled": reconciled}
    if reconciled and result.get("recorded_pre_head"):
        extra["recorded_pre_head"] = result["recorded_pre_head"]
    if reconciled and result.get("matched_by"):
        extra["matched_by"] = result["matched_by"]
    if reconciled and run_termination.get("kill_failed"):
        extra["kill_failed"] = True
        extra["kill_failed_pids"] = run_termination.get("pids") or []
    with transaction(conn):
        fin = _final_tx(conn, plan, result, extra=extra)
    if fin.get("left_merging"):
        return "skipped"
    return "reconciled" if reconciled else "interrupted"


def recover_merging_task(conn: sqlite3.Connection, home: str, task: Mapping[str, Any]) -> str:
    """§11.7 step 5 for one task in ``merging``: T16 ``interrupted`` or reconciled T15 in
    its own transaction (the final TX of the live path, with ``recovered``/``reconciled``).
    Re-run safe: a task already moved on is skipped by ``_final_tx``'s status check."""
    rec_ctx = _prepare_merging_recovery(conn, home, task)
    if rec_ctx is None:
        return "run_open"
    integ = rec_ctx["integ"]
    pre_head = rec_ctx["pre_head"]
    if _plat_paths.is_reparse_link(_plat_paths.strip_trailing_seps(integ)):
        result = {"outcome": "failed", "reason": "interrupted", "pre_head": pre_head,
                  "details": {"integration": integ, "foreign_repository": True, "restored": False}}
    elif os.path.isdir(integ):
        token = _RECOVERY_EXPECTED.set((rec_ctx.get("repo"), rec_ctx.get("ws_root")))
        try:
            result = decide_recovery(integ, task["id"], task["branch"], pre_head, run_dir=rec_ctx["run_dir"])
        finally:
            _RECOVERY_EXPECTED.reset(token)
    else:
        result = {"outcome": "failed", "reason": "interrupted", "pre_head": pre_head,
                  "details": {"integration_missing": True, "restored": None}}
    return _finish_merging_recovery(conn, home, rec_ctx, result)


async def recover_merging_task_async(conn: sqlite3.Connection, home: str, task: Mapping[str, Any]) -> str:
    """Async counterpart of ``recover_merging_task`` running ``decide_recovery`` in a worker thread."""
    rec_ctx = _prepare_merging_recovery(conn, home, task)
    if rec_ctx is None:
        return "run_open"
    integ = rec_ctx["integ"]
    pre_head = rec_ctx["pre_head"]
    if _plat_paths.is_reparse_link(_plat_paths.strip_trailing_seps(integ)):
        result = {"outcome": "failed", "reason": "interrupted", "pre_head": pre_head,
                  "details": {"integration": integ, "foreign_repository": True, "restored": False}}
    elif os.path.isdir(integ):
        token = _RECOVERY_EXPECTED.set((rec_ctx.get("repo"), rec_ctx.get("ws_root")))
        try:
            result = await asyncio.to_thread(
                decide_recovery, integ, task["id"], task["branch"], pre_head, run_dir=rec_ctx["run_dir"]
            )
        finally:
            _RECOVERY_EXPECTED.reset(token)
    else:
        result = {"outcome": "failed", "reason": "interrupted", "pre_head": pre_head,
                  "details": {"integration_missing": True, "restored": None}}
    return _finish_merging_recovery(conn, home, rec_ctx, result)


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
        if result.get("noop") and result.get("tip"):
            merge_json["tip"] = result["tip"]  # R3 P2-1: lets I9 check what the no-op recorded
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
        if sys.platform == "win32" and plan.get("run_id"):
            from mesh.daemon import supervisor

            supervisor.close_run_job(plan["run_id"])
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
    if sys.platform == "win32" and plan.get("run_id"):
        from mesh.daemon import supervisor

        supervisor.close_run_job(plan["run_id"])
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
    message = f"Merge of task '{plan['task_id']}' failed: {reason}"
    if reason == "tests_failed":
        message = tests_failed_message(plan["task_id"], details)
    return MeshError(code=REASON_CODES[reason], exit_code=c.EXIT_MERGE_FAILED, message=message, details=details)


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
    from mesh.daemon.server import _idempotency_record, _store_idempotency_row

    return _store_idempotency_row(
        conn,
        idem["principal_id"],
        idem["key"],
        "task.merge",
        idem["request_hash"],
        json.dumps(_idempotency_record("task.merge", envelope)),
        exit_code,
        ignore_conflict=True,
    )


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
                ws_root = get_workspaces_root(conn, ctx.home)
                integ = integration_worktree_path(ws_root, project["id"])
                token = _PROBE_EXPECTED.set((project["repo_path"], ws_root))
                try:
                    pre_head = await asyncio.to_thread(probe_integration_head, integ)
                finally:
                    _PROBE_EXPECTED.reset(token)

    # 2. TX1
    idem = getattr(ctx, "idempotency", None)
    conn.execute("BEGIN IMMEDIATE;")
    try:
        from mesh.daemon.handlers.inbox import recheck_tx_credential_and_authz

        recheck_tx_credential_and_authz(ctx, cmd="task.merge")
        if not ctx.dry_run:
            from mesh.daemon.handlers.run import _check_tx1_idempotency

            replay = _check_tx1_idempotency(conn, idem, "task.merge", ctx=ctx)
            if replay is not None:
                conn.execute("ROLLBACK;")
                return replay
        task, project = check_merge_request(ctx, task_id)
        plan = _tx1(ctx, task, project, pre_head)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise
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
    with _ACTIVE_TEST_LOCK:
        _ACTIVE_MERGE_RUNS.add(plan["run_id"])
    try:
        os.makedirs(plan["run_dir"], mode=0o700, exist_ok=True)
        if sys.platform == "win32":
            _plat_perms.set_private_dir_acl(plan["run_dir"], 0o700)
    except OSError:
        pass  # the run dir only holds the r6 test log

    # 3 + 4. Pipeline and final TX as one shielded task: a client disconnect (or a
    # cancelled request coroutine) does not cancel the merge. Notify waiters only
    # inside the shielded job so no cancellation window sits between COMMIT and shield.
    idem = getattr(ctx, "idempotency", None)

    async def _pipeline_and_finish() -> Dict[str, Any]:
        try:
            await server.notify_waiters()
        except Exception:
            pass
        result = await asyncio.to_thread(run_merge_pipeline, plan)
        try:
            _check_live_merge_close(conn, plan, result)
        except (MergeRunHeld, OSError) as exc:
            run_row = conn.execute("SELECT * FROM runs WHERE id = ?;", (plan["run_id"],)).fetchone()
            if run_row is not None:
                _report_recovery_blocked(conn, run_row, exc)
            raise LiveMergeHeld(plan, result, exc) from exc
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
        try:
            await server.notify_waiters()
            server.wake_scheduler()
        except Exception:
            pass
        return {"result": result, "final": fin, "response": response}

    def _on_job_done(f: "asyncio.Future", rid: str = plan["run_id"]) -> None:
        with _ACTIVE_TEST_LOCK:
            _ACTIVE_MERGE_RUNS.discard(rid)
        if not f.cancelled():
            f.exception()

    try:
        job = asyncio.ensure_future(_pipeline_and_finish())
    except BaseException:
        with _ACTIVE_TEST_LOCK:
            _ACTIVE_MERGE_RUNS.discard(plan["run_id"])
        raise
    job.add_done_callback(_on_job_done)
    # Full review P1: meshd shutdown awaits the job before it releases the lock / DB.
    track = getattr(server, "track_merge_job", None)
    if callable(track):
        track(job)
    try:
        done = await asyncio.shield(job)
    except asyncio.CancelledError:
        raise
    except LiveMergeHeld as held:
        try:
            server.log_error(f"task merge {plan['task_id']} held: {held.cause_text}")
        except Exception:
            pass
        raise held.to_mesh_error()
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
