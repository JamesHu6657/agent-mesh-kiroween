"""Doctor resource command: diagnostics, invariant checks, and permission repairs."""

import argparse
import json
import os
import stat
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

import mesh.constants as c
from mesh.errors import MeshError, SchemaTooNewError
from mesh.core.auth import parse_kdf_params
from mesh.core.db import connect
from mesh.core.schema import CURRENT_SCHEMA_VERSION
from mesh.core.invariants import check_invariants
from mesh.core.paths import (
    check_install_path_secure,
    get_install_root,
    refuse_testing_on_default_home,
    resolve_home,
)

# What `mesh init` creates next to mesh.db: if any of these is in the home, the home
# looks initialised and a missing mesh.db is a failure, not a fresh home (round 3).
_INIT_MARKERS = ("run", "runs", "workspaces", "logs", "merges", "mesh.db-wal", "mesh.db-shm")

# What `mesh init` creates in the home; --fix-perms touches only these (round 4).
_KNOWN_DIRS = ("run", "runs", "workspaces", "logs", "merges")
_KNOWN_FILES = ("mesh.db", "mesh.db-wal", "mesh.db-shm", "mesh.db-journal")
# Round 5: files under these keep the owner's bits (a checked-out script stays +x);
# only group/other bits are removed. Elsewhere regular files get 0600 (as on main).
_KEEP_OWNER_BITS_DIRS = ("workspaces",)

# Test hook (round 5): called as _RACE_HOOK(stage, rel_path) between opening an entry
# and acting on it, so a test can swap a component for a symlink mid-walk. None in
# production. Stages: "before-open-home" (round 6), "before-chmod", "before-open-dir",
# "before-verify" (round 6).
_RACE_HOOK: Optional[Callable[[str, str], None]] = None

_O_PATH = getattr(os, "O_PATH", 0)

# Round 6: the one reason given for an entry that could only be chmodded by name.
_NO_FD_CHMOD = "no fd-bound chmod"


def _hook(stage: str, rel: str) -> None:
    if _RACE_HOOK is not None:
        _RACE_HOOK(stage, rel)


def _ident(st: os.stat_result) -> Tuple[int, int]:
    return (st.st_dev, st.st_ino)


class _FixReport:
    def __init__(self) -> None:
        self.skipped: List[Dict[str, str]] = []
        # Round 6: rel -> ((dev, ino), mode) of every entry this run chmodded (or found
        # already right), re-verified after the walk.
        self.repaired: Dict[str, Tuple[Tuple[int, int], int]] = {}
        # Round 6: repaired entries whose path no longer names the repaired inode with
        # the target mode after the walk (the path is unsafe right now).
        self.unsafe: List[Dict[str, str]] = []

    def skip(self, rel: str, reason: str) -> None:
        self.skipped.append({"path": rel, "reason": reason})

    def done(self, rel: str, st: os.stat_result, mode: int) -> None:
        self.repaired[rel] = (_ident(st), mode)


def _chmod_fd(fd: int, mode: int) -> bool:
    """chmod the inode ``fd`` refers to, never a path that could have been swapped.

    ``fd`` is an O_PATH descriptor (opened with O_NOFOLLOW), so it works on a mode 000
    file or directory, which cannot be opened for reading. os.fchmod() does not accept
    an O_PATH fd on Linux, so the chmod goes through /proc/self/fd/N, which names the
    already-open inode, not the directory entry. Round 6: there is no by-name fallback
    any more (fchmodat(parent_fd, name) is not bound to the checked inode); without
    /proc this returns False and the caller skips the entry ("no fd-bound chmod").
    """
    try:
        os.fchmod(fd, mode)
        return True
    except OSError:
        pass
    try:
        os.chmod(f"/proc/self/fd/{fd}", mode)
        return True
    except OSError:
        return False


def _open_entry(parent_fd: int, name: str) -> Optional[int]:
    """O_PATH|O_NOFOLLOW open of ``name`` relative to ``parent_fd``; None if absent."""
    try:
        return os.open(name, _O_PATH | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return None


def _open_dir_for_listing(pfd: int, parent_fd: Optional[int], name: str) -> Optional[int]:
    """A readable fd on the SAME directory inode as the O_PATH fd ``pfd`` (checked by
    dev/ino), opened without following symlinks; None if it changed meanwhile.
    Round 6: the readable fd is closed if fstat raises (no leak on the error path)."""
    want = os.fstat(pfd)
    try:
        dfd = os.open(f"/proc/self/fd/{pfd}", os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        if parent_fd is None:
            raise
        dfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        got = os.fstat(dfd)
    except BaseException:
        os.close(dfd)
        raise
    if _ident(got) != _ident(want):
        os.close(dfd)
        return None
    return dfd


def _file_mode(st: os.stat_result, keep_owner: bool) -> int:
    if keep_owner:
        # Owner bits kept (x included), group/other removed, owner rw ensured.
        return (stat.S_IMODE(st.st_mode) & 0o700) | 0o600
    return 0o600


def _fix_entry(parent_fd: int, name: str, rel: str, uid: int, keep_owner: bool,
               report: _FixReport) -> Optional[int]:
    """Repair one entry by fd. Returns a readable fd on it if it is a directory the
    walk should descend into (the caller closes it), else None."""
    try:
        fd = _open_entry(parent_fd, name)
    except OSError as e:
        report.skip(rel, f"cannot open ({type(e).__name__})")
        return None
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode):
            report.skip(rel, "symlink, not followed")
            return None
        if st.st_uid != uid:
            report.skip(rel, f"owned by uid {st.st_uid}, not {uid}")
            return None
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            # Round 6: another name (possibly outside the home) shares this inode, so
            # chmodding it would change that file too.
            report.skip(rel, "hardlinked")
            return None
        _hook("before-chmod", rel)
        if stat.S_ISDIR(st.st_mode):
            # 0700 before listing, so a 000 directory can be walked.
            if not _chmod_fd(fd, 0o700):
                report.skip(rel, _NO_FD_CHMOD)
                return None
            report.done(rel, st, 0o700)
            _hook("before-open-dir", rel)
            dfd = _open_dir_for_listing(fd, parent_fd, name)
            if dfd is None:
                report.skip(rel, "directory changed during the walk, not descended")
            return dfd
        if stat.S_ISREG(st.st_mode):
            mode = _file_mode(st, keep_owner)
            if not _chmod_fd(fd, mode):
                report.skip(rel, _NO_FD_CHMOD)
            else:
                report.done(rel, st, mode)
            return None
        # Sockets (run/mesh.sock), FIFOs and device nodes are left alone, as on main.
        return None
    except OSError as e:
        report.skip(rel, f"cannot repair ({type(e).__name__})")
        return None
    finally:
        os.close(fd)


def _walk(root_fd: int, root_rel: str, visit: Callable[[int, str, str], Optional[int]],
          report: _FixReport) -> None:
    """Iterative, fd-based walk below the directory ``root_fd`` (owned by this call:
    it is closed on every path, error paths included).

    ``visit(parent_fd, name, rel)`` handles one entry and returns a readable fd on a
    directory to descend into (ownership passes to the walk), or None.

    Only one directory fd is held at a time, so depth is unbounded (no recursion, no
    fd per level): the walk descends into a child by fd and climbs back with
    openat(cur, "..") and checks the parent's dev/ino against what it recorded, so a
    directory moved mid-walk stops the walk instead of chmodding something else.
    """
    cur = root_fd
    try:
        try:
            frames = [(_ident(os.fstat(cur)), root_rel, sorted(os.listdir(cur), reverse=True))]
        except OSError as e:
            report.skip(root_rel, f"cannot list ({type(e).__name__})")
            return
        while frames:
            _, rel, names = frames[-1]
            if not names:
                frames.pop()
                if not frames:
                    break
                up = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
                os.close(cur)
                cur = up
                if _ident(os.fstat(cur)) != frames[-1][0]:
                    report.skip(frames[-1][1], "directory moved during the walk, rest not walked")
                    return
                continue
            name = names.pop()
            child_rel = f"{rel}/{name}"
            child = visit(cur, name, child_rel)
            if child is None:
                continue
            # Round 6: the child fd is closed if fstat / listdir raises.
            try:
                child_frame = (_ident(os.fstat(child)), child_rel, sorted(os.listdir(child), reverse=True))
            except OSError as e:
                os.close(child)
                report.skip(child_rel, f"cannot list ({type(e).__name__})")
                continue
            except BaseException:
                os.close(child)
                raise
            frames.append(child_frame)
            os.close(cur)
            cur = child
    except OSError as e:
        report.skip(root_rel, f"walk stopped ({type(e).__name__})")
    finally:
        os.close(cur)


def _open_home_nofollow(home_dir: str) -> int:
    """O_PATH fd on the home, opened from "/" one component at a time with O_NOFOLLOW
    (round 6): a symlinked ANCESTOR fails (ENOTDIR / ELOOP) instead of being followed.
    ``home_dir`` is resolve_home()'d (realpath), so no legitimate component is a link."""
    flags = _O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY
    cur = os.open("/", _O_PATH | os.O_DIRECTORY)
    try:
        for part in [p for p in home_dir.split("/") if p]:
            nxt = os.open(part, flags, dir_fd=cur)
            os.close(cur)
            cur = nxt
    except BaseException:
        os.close(cur)
        raise
    return cur


def _verify_entry(report: _FixReport, pending: Dict[str, Tuple[Tuple[int, int], int]]):
    """Visitor for the post-repair walk: an entry this run repaired must still be the
    repaired inode (by lstat-equivalent fstat of an O_NOFOLLOW open) with the target
    mode; it descends only into repaired directories."""
    def visit(parent_fd: int, name: str, rel: str) -> Optional[int]:
        want = pending.pop(rel, None)
        if want is None:
            return None
        (ident, mode) = want
        try:
            fd = _open_entry(parent_fd, name)
        except OSError:
            fd = None
        if fd is None:
            report.unsafe.append({"path": rel, "reason": "changed after repair (gone or not openable)"})
            return None
        try:
            st = os.fstat(fd)
            if _ident(st) != ident or stat.S_ISLNK(st.st_mode):
                report.unsafe.append({"path": rel, "reason": "changed after repair (another inode at this path)"})
                return None
            if stat.S_IMODE(st.st_mode) != mode:
                report.unsafe.append({"path": rel, "reason": f"changed after repair (mode {stat.S_IMODE(st.st_mode):04o}, want {mode:04o})"})
                return None
            if stat.S_ISDIR(st.st_mode):
                dfd = _open_dir_for_listing(fd, parent_fd, name)
                if dfd is None:
                    report.unsafe.append({"path": rel, "reason": "changed after repair (another inode at this path)"})
                return dfd
            return None
        except OSError as e:
            report.unsafe.append({"path": rel, "reason": f"cannot re-verify ({type(e).__name__})"})
            return None
        finally:
            os.close(fd)
    return visit


def _verify_repaired(home_dir: str, report: _FixReport) -> None:
    """Round 6: after the walk, every repaired path must still name the repaired
    inode with the target mode; otherwise it is reported unsafe (doctor fails)."""
    if not report.repaired:
        return
    _hook("before-verify", ".")
    pending = dict(report.repaired)
    home_want = pending.pop(".", None)
    try:
        hfd = _open_home_nofollow(home_dir)
    except OSError as e:
        report.unsafe.append({"path": ".", "reason": f"changed after repair ({type(e).__name__})"})
        return
    dfd = None
    try:
        st = os.fstat(hfd)
        if home_want is not None and (_ident(st) != home_want[0] or stat.S_IMODE(st.st_mode) != home_want[1]):
            report.unsafe.append({"path": ".", "reason": "changed after repair"})
            return
        dfd = _open_dir_for_listing(hfd, None, home_dir)
    except OSError as e:
        report.unsafe.append({"path": ".", "reason": f"cannot re-verify ({type(e).__name__})"})
        return
    finally:
        os.close(hfd)
    if dfd is None:
        report.unsafe.append({"path": ".", "reason": "changed after repair"})
        return
    visit = _verify_entry(report, pending)
    try:
        for name in _KNOWN_FILES + _KNOWN_DIRS:
            sub = visit(dfd, name, name)
            if sub is not None:
                _walk(sub, name, visit, report)
    finally:
        os.close(dfd)
    # Repaired entries the walk did not meet again (moved away with a parent, or a
    # parent failed verification) are unsafe too, unless a parent is already listed.
    listed = [u["path"] for u in report.unsafe]
    for rel in sorted(pending):
        if not any(rel == p or rel.startswith(p + "/") for p in listed):
            report.unsafe.append({"path": rel, "reason": "changed after repair (not found again)"})


def _fix_perms(home_dir: str, expected: Optional[Tuple[int, int]] = None) -> _FixReport:
    """mesh doctor --fix-perms (plan §5.1): 0700 on directories, 0600 on files.

    Only the home itself and the files/directories `mesh init` creates in it
    (recursively under the known directories) are touched, only if owned by the
    current uid. Round 5: fd-based, so no path is re-resolved between the check and
    the chmod: every entry is opened O_PATH|O_NOFOLLOW relative to its parent's fd,
    checked with fstat and chmodded on that fd; a leaf or an ancestor swapped for a
    symlink mid-walk cannot redirect a chmod outside the home.

    Round 6: the home itself is opened component by component from "/" with
    O_NOFOLLOW and must be the inode the precheck recorded (``expected``, from
    handle_doctor's stat right after resolve_home); hardlinked regular files are
    skipped; there is no by-name chmod fallback; every repaired path is re-verified
    after the walk (report.unsafe). Returns the report.
    """
    uid = os.getuid()
    report = _FixReport()
    _hook("before-open-home", ".")
    try:
        hfd = _open_home_nofollow(home_dir)
    except FileNotFoundError:
        if expected is not None:
            report.skip(".", "home changed since the precheck (now absent)")
        return report
    except OSError as e:
        report.skip(".", f"home cannot be opened as a directory without following symlinks ({type(e).__name__})")
        return report
    dfd = None
    try:
        st = os.fstat(hfd)
        if expected is None or _ident(st) != expected:
            report.skip(".", "home is not the directory the precheck saw, not repaired")
            return report
        if st.st_uid != uid:
            report.skip(".", f"owned by uid {st.st_uid}, not {uid}")
            return report
        _hook("before-chmod", ".")
        if not _chmod_fd(hfd, 0o700):
            report.skip(".", _NO_FD_CHMOD)
            return report
        report.done(".", st, 0o700)
        dfd = _open_dir_for_listing(hfd, None, home_dir)
    except OSError as e:
        report.skip(".", f"cannot repair ({type(e).__name__})")
        return report
    finally:
        os.close(hfd)
    if dfd is None:
        report.skip(".", "home changed during the walk")
        return report
    try:
        for name in _KNOWN_FILES:
            _fix_entry(dfd, name, name, uid, False, report)
        for name in _KNOWN_DIRS:
            keep = name in _KEEP_OWNER_BITS_DIRS
            sub = _fix_entry(dfd, name, name, uid, keep, report)
            if sub is not None:
                def visit(pfd: int, n: str, rel: str, _keep: bool = keep) -> Optional[int]:
                    return _fix_entry(pfd, n, rel, uid, _keep, report)
                _walk(sub, name, visit, report)
    finally:
        os.close(dfd)
    _verify_repaired(home_dir, report)
    return report


def _home_identity(home_dir: str) -> Optional[Tuple[int, int]]:
    """(dev, ino) of the resolved home at precheck time (round 6); None if absent."""
    try:
        return _ident(os.lstat(home_dir))
    except OSError:
        return None


def register_doctor_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    doctor_parser = subparsers.add_parser(
        "doctor",
        parents=[parent_parser],
        help="Diagnose environment health, verify database invariants, and repair file modes",
        description="Diagnose environment health, verify database invariants, and repair file modes.",
        epilog="Examples:\n  mesh doctor\n  mesh doctor --check-invariants\n  mesh doctor --fix-perms\n",
    )
    doctor_parser.add_argument("--check-invariants", action="store_true", default=False, help="Verify DB invariants I1-I8 while daemon is stopped")
    doctor_parser.add_argument("--fix-perms", action="store_true", default=False, help="Restore 0700/0600 filesystem permissions across MESH_HOME")
    doctor_parser.set_defaults(func=handle_doctor)


def _db_error(db_path: str, reason: str, exc: Any = None) -> MeshError:
    details: Dict[str, Any] = {"db": db_path, "reason": reason}
    if exc is not None:
        details["error"] = str(exc)
    return MeshError(
        code=c.DB_INTEGRITY_FAILED,
        exit_code=c.EXIT_INTERNAL,
        message=f"mesh doctor cannot verify this home's database ({reason}); it is not healthy.",
        details=details,
    )


def _present(path: str, db_path: str) -> bool:
    """True if ``path`` exists (a dangling symlink counts), False only if really absent.

    m3-test-kdf round 3: os.path.lexists()/exists() return False on *any* OSError,
    so an unreadable home (chmod 000) looked uninitialised and doctor said healthy.
    Here only FileNotFoundError means absent; PermissionError or any other OSError
    (e.g. a path component that is not a directory) fails with rc 1.
    """
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        raise _db_error(db_path, f"cannot access {path} ({type(e).__name__})", e) from e


def _strict_schema_version(conn: sqlite3.Connection, db_path: str) -> int:
    """Schema version, strictly (round 3): what meshd requires at startup.

    Unlike the lenient migration reader (which treats a missing meta table / row as
    version 0), a missing meta table, a missing schema_version row or a value that is
    not an integer is DB_INTEGRITY_FAILED (rc 1); a version newer than supported is
    SCHEMA_TOO_NEW.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta';").fetchone() is None:
        raise _db_error(db_path, "meta table missing")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version';").fetchone()
    if row is None:
        raise _db_error(db_path, "schema_version missing in meta table")
    raw = row["value"]
    if isinstance(raw, bool) or not (isinstance(raw, int) or (isinstance(raw, str) and raw.isascii() and raw.isdigit())):
        raise _db_error(db_path, f"schema_version is not an integer: {raw!r}")
    version = int(raw)
    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaTooNewError(current_version=version, supported_version=CURRENT_SCHEMA_VERSION)
    return version


def _read_home_state(db_path: str) -> Dict[str, Any]:
    """Read what doctor checks from mesh.db, fail closed (m3-test-kdf round 2).

    Any open / query / decode error, a corrupt file, a missing table or column, or a
    missing user passphrase credential row raises DB_INTEGRITY_FAILED (rc 1). Nothing
    here is swallowed: a home doctor cannot read is never reported as healthy.
    SCHEMA_TOO_NEW is raised before any schema-dependent query.
    """
    try:
        conn = connect(db_path, read_only=True)
    except sqlite3.Error as e:
        raise _db_error(db_path, "cannot open database", e) from e
    try:
        try:
            qc = conn.execute("PRAGMA quick_check;").fetchall()
            if [tuple(r) for r in qc] != [("ok",)]:
                raise _db_error(db_path, "quick_check failed: " + "; ".join(str(r[0]) for r in qc[:5]))
            _strict_schema_version(conn, db_path)
            krow = conn.execute(
                "SELECT c.kdf_params FROM credentials c JOIN principals p ON c.principal_id = p.id "
                "WHERE p.kind = 'user' AND c.kind = 'passphrase' LIMIT 1;"
            ).fetchone()
            if krow is None:
                raise _db_error(db_path, "no user passphrase credential row")
            # Raw stored value; parsed by parse_kdf_params (fail closed) by the caller.
            kdf_raw = krow["kdf_params"]
            row = conn.execute("SELECT value_json FROM settings WHERE key = 'workspaces.root';").fetchone()
            configured_ws = None
            if row is not None and row["value_json"]:
                try:
                    configured_ws = json.loads(row["value_json"])
                except ValueError as e:
                    raise _db_error(db_path, "settings workspaces.root is not valid JSON", e) from e
                if configured_ws is not None and not isinstance(configured_ws, str):
                    raise _db_error(db_path, "settings workspaces.root is not a path string")
            repo_rows = conn.execute(
                "SELECT DISTINCT repo_path FROM projects WHERE repo_path IS NOT NULL;"
            ).fetchall()
            repo_paths = [r[0] for r in repo_rows if r[0]]
        except sqlite3.Error as e:
            raise _db_error(db_path, "database query failed", e) from e
    finally:
        conn.close()
    return {"kdf_raw": kdf_raw, "workspaces_root": configured_ws, "repo_paths": repo_paths}


def _refuse_dry_run(args: argparse.Namespace, what: str) -> None:
    """Linear full review C1: local commands that write cannot simulate; refuse rather
    than perform the change under --dry-run."""
    if getattr(args, "dry_run", False):
        import mesh.constants as _c
        from mesh.errors import MeshError as _ME
        raise _ME(_c.INVALID_ARGUMENT, exit_code=_c.EXIT_USAGE,
                  message=f"{what} does not support --dry-run (it would change local state); run it without --dry-run",
                  details={"param": "dry_run"})


def handle_doctor(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "fix_perms", False):
        _refuse_dry_run(args, "mesh doctor --fix-perms")
    # Round 4: only a missing home falls back to MESH_HOME / the default; an empty
    # --home is passed on so resolve_home refuses it. An EMPTY MESH_HOME (MESH_HOME="")
    # counts as unset and falls back to the default ~/.agent-mesh, as in mesh/cli/main.py
    # (round 5, documented in `mesh help env`).
    home_dir = getattr(args, "home", None)
    if home_dir is None:
        home_dir = os.environ.get("MESH_HOME") or os.path.expanduser("~/.agent-mesh")
    # Round 3: resolve once (relative to the cwd, symlinks before ".."), as init and meshd do.
    home_dir = resolve_home(home_dir)
    install_root = get_install_root()
    # m3-test-kdf fix round (mode authority): doctor decides its testing mode once,
    # here, by the same rule meshd uses when started without an explicit argument.
    testing_mode = os.environ.get("MESH_TESTING") == "1"
    # Round 2: the same default-home guard as `mesh init` and meshd, so testing mode
    # on the default home (or any alias of it) never accepts weak params here.
    refuse_testing_on_default_home(home_dir, testing_mode)
    # Round 5 (order): every path / overlap / refusal check that does not need the DB
    # runs BEFORE --fix-perms writes anything: the home, $home/runs and the default
    # $home/workspaces against the install root (plan §7.6 mitigation 7, C14). An
    # install dir used as the home is refused (rc 2) with its modes untouched.
    check_install_path_secure(install_root=install_root, home=home_dir)

    fix_perms = bool(getattr(args, "fix_perms", False))
    skipped: List[Dict[str, str]] = []
    if fix_perms:
        # Round 6: the identity of the home the prechecks above saw; --fix-perms only
        # repairs a home whose no-follow open yields this same inode.
        home_ident = _home_identity(home_dir)
        # Round 4: repair modes before the fail-closed DB/KDF checks below, so a chmod
        # 000 home or mesh.db is repaired (rc 0) as on main (plan §5.1). Without
        # --fix-perms an unreadable home still fails (rc 1).
        report = _fix_perms(home_dir, home_ident)
        if report.unsafe:
            # Round 6: a repaired path that no longer names the repaired inode with
            # the target mode is unsafe right now: fixed:false and doctor fails.
            raise MeshError(
                code=c.INSECURE_PERMISSIONS,
                exit_code=c.EXIT_USAGE,
                message=f"mesh doctor --fix-perms: {len(report.unsafe)} repaired path(s) changed during the repair and are not safe.",
                details={"fixed": False, "home": home_dir, "unsafe": report.unsafe, "skipped": report.skipped},
            )
        skipped = report.skipped

    db_path = os.path.join(home_dir, "mesh.db")
    configured_ws = None
    repo_paths: List[str] = []
    db_present = _present(db_path, db_path)
    if not db_present:
        # Round 3: no mesh.db is healthy only for a home that is really uninitialised.
        for marker in _INIT_MARKERS:
            if _present(os.path.join(home_dir, marker), db_path):
                raise _db_error(db_path, f"mesh.db missing but the home looks initialised ({marker} exists)")
    if db_present:
        state = _read_home_state(db_path)
        configured_ws = state["workspaces_root"]
        repo_paths = state["repo_paths"]
        # m3-test-kdf guard b: the same parse+validate helper as meshd's unlock, fail
        # closed. Garbled JSON, SQL NULL, empty, JSON null or out-of-range params give
        # KDF_PARAMS_INVALID; outside testing mode a weak / testing-marked home gives
        # KDF_TOO_WEAK (both rc 3) instead of being reported as healthy.
        parse_kdf_params(state["kdf_raw"], testing_mode=testing_mode)

    # Plan §7.6 mitigation 7 & checklist C14: again with the DB's workspaces.root and
    # project repo paths, which can only be read after the repair.
    check_install_path_secure(install_root=install_root, home=home_dir, workspaces_root=configured_ws,
                              repo_paths=repo_paths)

    if fix_perms:
        # Round 5: fixed is False (with each skipped entry and why) when something
        # was left alone because it is a symlink, owned by another uid, or could not
        # be chmodded safely; the rc stays 0.
        if skipped:
            return {"fixed": False, "home": home_dir, "skipped": skipped,
                    "reason": f"{len(skipped)} entr{'y' if len(skipped) == 1 else 'ies'} not repaired"}
        return {"fixed": True, "home": home_dir}

    if getattr(args, "check_invariants", False):
        if not db_present:
            # Really uninitialised home (checked strictly above) -> 0 violations
            return {"violations": []}

        try:
            conn = connect(db_path, read_only=True)
            try:
                _strict_schema_version(conn, db_path)
                violations = check_invariants(conn)
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise MeshError(
                code=c.DB_INTEGRITY_FAILED,
                exit_code=c.EXIT_INTERNAL,
                message=f"Failed to inspect database for invariant verification: {e}",
                details={"error": str(e)},
            ) from e

        if violations:
            raise MeshError(
                code=c.INTERNAL_ERROR,
                exit_code=c.EXIT_INTERNAL,
                message=f"Found {len(violations)} database invariant violation(s)",
                details={"violations": [v.to_dict() for v in violations]},
            )

        return {"violations": []}

    # Default doctor health check, plus cheap environment checks (productize): platform,
    # python, git, init state, daemon, agent CLIs on PATH and the evidence mode. They are
    # advisory: "status" stays about the home itself; failing checks carry a hint.
    from mesh.cli.commands.doctor_env import environment_checks

    checks = environment_checks(home_dir, db_present, testing_mode=testing_mode)
    out: Dict[str, Any] = {
        # An uninitialised home (no mesh.db and no init marker, checked strictly above)
        # is not "healthy": nothing can run there until it is set up.
        "status": "healthy" if db_present else "not_initialized",
        "install_root": install_root,
        "home": home_dir,
        "checks": checks,
        "problems": [f"{x['name']}: {x['detail']} -> {x.get('hint', '')}" for x in checks if not x["ok"]],
    }
    if not db_present:
        out["hint"] = f"Mesh is not set up in {home_dir} yet; run `mesh setup` (or `mesh init`)"
    return out
