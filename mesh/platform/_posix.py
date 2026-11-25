"""POSIX/Linux implementations of Agent Mesh platform primitives.

All functions in this module preserve exact Linux behavior byte-for-byte.
"""

from __future__ import annotations

import errno
import os
import signal
import stat
from typing import Any, Dict, List, Optional, Set


def is_fs_root(path: str) -> bool:
    """Return True if *path* is the POSIX filesystem root ``/``."""
    return path == "/"


def norm_cmp(path: str) -> str:
    """Identity path comparison normalization on POSIX (case-sensitive)."""
    return path


def same_path(a: str, b: str) -> bool:
    """Return True if *a* and *b* resolve to the same canonical path on POSIX."""
    return os.path.realpath(a) == os.path.realpath(b)


def is_path_equal_or_parent(parent: str, child: str) -> bool:
    """Return True if *child* is equal to *parent* or located inside *parent*."""
    parent_real = os.path.realpath(parent)
    child_real = os.path.realpath(child)
    if parent_real == child_real:
        return True
    try:
        common = os.path.commonpath([parent_real, child_real])
        return common == parent_real
    except ValueError:
        return False


def strip_trailing_seps(path: str) -> str:
    """Strip trailing POSIX ``/`` separators, preserving ``/``."""
    if not path:
        return path
    return path.rstrip(os.sep) or os.sep


def is_reparse_link(path: str) -> bool:
    """Return True if *path* is a symbolic link."""
    return os.path.islink(strip_trailing_seps(path))


def is_reparse_stat(st: os.stat_result) -> bool:
    """Return True if *st* represents a symbolic link."""
    return stat.S_ISLNK(st.st_mode)


def temp_root() -> str:
    """Return the fixed POSIX temp root ``/tmp``."""
    return "/tmp"


def split_path_segments(path: str) -> list[str]:
    """Split *path* on POSIX directory separators, dropping empty segments."""
    return [seg for seg in path.split("/") if seg]


def has_traversal_segment(path: str) -> bool:
    """Return True if *path* contains any ``..`` traversal segment."""
    return any(seg == ".." for seg in split_path_segments(path))


def atomic_replace(src: str, dst: str) -> None:
    """Atomically replace *dst* with *src* via ``os.replace``."""
    os.replace(src, dst)


def unlink_nonregular(path: str) -> None:
    """Unlink a file or POSIX symlink."""
    os.unlink(path)


def read_bytes_shared(path: str) -> bytes:
    """Read *path* in binary mode."""
    with open(path, "rb") as f:
        return f.read()


def fsync_dir(dir_path: str) -> None:
    """Fsync directory metadata after an atomic rename on POSIX."""
    dfd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def open_regular_nofollow(path: str, flags: int, mode: int = 0o600, label: str = "file") -> int:
    """Open a regular file without following symlinks or blocking on FIFOs."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags | nofollow | nonblock, mode)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, f"Refusing non-regular {label}", path)
        if nonblock:
            try:
                import fcntl

                fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                if fl & nonblock:
                    fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~nonblock)
            except OSError:
                pass
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_lock_file(path: str, *, mode: int = 0o600) -> int:
    """Open or create a lock file with inheritable=False on POSIX."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, mode)
    try:
        os.set_inheritable(fd, False)
        return fd
    except BaseException:
        os.close(fd)
        raise


def acquire_lock(fd: int, *, exclusive: bool = True, blocking: bool = True) -> None:
    """Acquire an exclusive or shared flock on *fd* on POSIX."""
    import fcntl

    op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        op |= fcntl.LOCK_NB
    fcntl.flock(fd, op)


def release_lock(fd: int) -> None:
    """Release an flock on *fd* on POSIX."""
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def is_exclusive_lock_held(path: str) -> bool:
    """Return True if an exclusive flock is currently held on *path*."""
    import fcntl

    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return True
    finally:
        os.close(fd)
    return False


def set_private_dir_acl(path_or_fd: str | int, mode: int = 0o700) -> None:
    """Set private directory permissions (0o700) on POSIX."""
    if isinstance(path_or_fd, int) and not isinstance(path_or_fd, bool):
        os.fchmod(path_or_fd, mode)
    else:
        os.chmod(path_or_fd, mode)


def set_private_file_acl(path_or_fd: str | int, mode: int = 0o600) -> None:
    """Set private file permissions (0o600) on POSIX."""
    if isinstance(path_or_fd, int) and not isinstance(path_or_fd, bool):
        os.fchmod(path_or_fd, mode)
    else:
        os.chmod(path_or_fd, mode)


def fchmod_private(fd: int, mode: int = 0o600) -> None:
    """Call ``os.fchmod(fd, mode)`` on POSIX."""
    os.fchmod(fd, mode)


def chmod_private(path: str, mode: int = 0o600) -> None:
    """Call ``os.chmod(path, mode)`` on POSIX."""
    os.chmod(path, mode)


def verify_private_owner_and_perms(
    fd_or_path: str | int,
    *,
    is_dir: bool | None = None,
    label: str | None = None,
    error_code: str | None = None,
    exit_code: int | None = None,
) -> None:
    """Verify owner == getuid() and mode & 0o077 == 0 on POSIX."""
    import mesh.constants as c
    from mesh.errors import MeshError

    code = error_code or c.INSECURE_PERMISSIONS
    rc = c.EXIT_USAGE if exit_code is None else exit_code
    is_fd = isinstance(fd_or_path, int) and not isinstance(fd_or_path, bool)
    st = os.fstat(fd_or_path) if is_fd else os.stat(fd_or_path)
    desc = label or (f"fd {fd_or_path}" if is_fd else str(fd_or_path))
    if is_dir is True and not stat.S_ISDIR(st.st_mode):
        raise OSError(errno.ENOTDIR, "Expected directory", str(fd_or_path))
    if is_dir is False and not stat.S_ISREG(st.st_mode):
        raise OSError(errno.EINVAL, "Expected regular file", str(fd_or_path))
    uid = os.getuid()
    if st.st_uid != uid:
        raise MeshError(
            code=code,
            exit_code=rc,
            message=f"Path '{desc}' is not owned by current uid {uid}",
            details={"path": desc, "owner_uid": st.st_uid, "current_uid": uid},
        )
    if (st.st_mode & 0o077) != 0:
        mode_octal = oct(stat.S_IMODE(st.st_mode))
        expected = "0700" if stat.S_ISDIR(st.st_mode) else "0600"
        raise MeshError(
            code=code,
            exit_code=rc,
            message=f"Path '{desc}' has insecure permissions {mode_octal} (must be mode {expected})",
            details={"path": desc, "mode": mode_octal},
        )


def verify_dir_not_other_writable(
    path: str,
    *,
    label: str | None = None,
    error_code: str | None = None,
    exit_code: int | None = None,
) -> None:
    """Verify directory is owned by current euid/root and not writable by group/other (unless sticky)."""
    import mesh.constants as c
    from mesh.errors import MeshError

    code = error_code or c.INSECURE_PERMISSIONS
    rc = c.EXIT_USAGE if exit_code is None else exit_code
    st = os.stat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(errno.ENOTDIR, "Expected directory", path)
    desc = label or str(path)
    if hasattr(os, "geteuid") and st.st_uid not in (os.geteuid(), 0):
        raise MeshError(
            code=code,
            exit_code=rc,
            message=f"Directory '{desc}' is owned by another user",
            details={"path": desc, "owner_uid": st.st_uid, "current_uid": os.geteuid()},
        )
    if (st.st_mode & 0o022) and not (st.st_mode & stat.S_ISVTX):
        mode_octal = oct(stat.S_IMODE(st.st_mode))
        raise MeshError(
            code=code,
            exit_code=rc,
            message=f"Directory '{desc}' is writable by other users (mode {mode_octal})",
            details={"path": desc, "mode": mode_octal},
        )


def is_owned_by_current_user(path: str) -> bool:
    """Return True if *path* is owned by the current effective uid (or root when euid checked)."""
    st = os.lstat(path)
    if hasattr(os, "geteuid"):
        return st.st_uid in (os.geteuid(), 0)
    return st.st_uid == os.getuid()


def ensure_owned_dir_writable(
    path: str,
    *,
    st: os.stat_result | None = None,
    uid: int | None = None,
) -> bool:
    """Ensure owned directory *path* has ``0o700`` bits set on POSIX; return True if modified."""
    cur_st = os.lstat(path) if st is None else st
    cur_uid = os.getuid() if uid is None else uid
    if cur_st.st_uid == cur_uid and (cur_st.st_mode & 0o700) != 0o700:
        os.chmod(path, (cur_st.st_mode & 0o7777) | 0o700)
        return True
    return False


def get_proc_stat(pid: int) -> Optional[Dict[str, Any]]:
    """Delegate POSIX ``get_proc_stat`` to ``mesh.core.proc`` unchanged."""
    from mesh.core import proc as _core_proc

    return _core_proc.get_proc_stat(pid)


def is_tgid_alive(pid: int, info: Optional[Dict[str, Any]] = None) -> bool:
    """Delegate POSIX ``is_tgid_alive`` to ``mesh.core.proc`` unchanged."""
    from mesh.core import proc as _core_proc

    return _core_proc.is_tgid_alive(pid, info)


def is_process_alive(pid: int, expected_starttime: Optional[int] = None) -> bool:
    """Return True if POSIX *pid* is alive and (when provided) matches *expected_starttime*."""
    from mesh.core import proc as _core_proc

    exp_st: Optional[int] = None
    if expected_starttime is not None:
        try:
            exp_st = int(expected_starttime)
        except (TypeError, ValueError):
            return False
        if exp_st <= 0:
            return False
    info = _core_proc.get_proc_stat(pid)
    if info is None:
        return False
    if exp_st is not None and int(info["starttime"]) != exp_st:
        return False
    return _core_proc.is_tgid_alive(pid, info)


def get_proc_environ(pid: int) -> Optional[Dict[str, str]]:
    """Delegate POSIX ``get_proc_environ`` to ``mesh.core.proc`` unchanged."""
    from mesh.core import proc as _core_proc

    return _core_proc.get_proc_environ(pid)


def get_proc_cmdline(pid: int) -> Optional[List[str]]:
    """Read and parse ``/proc/<pid>/cmdline`` on POSIX, returning ``None`` if gone."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except (FileNotFoundError, ProcessLookupError):
        return None
    if not raw:
        return []
    parts = raw.split(b"\x00")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return [os.fsdecode(p) for p in parts]


def get_proc_ancestry(pid: int, stop_at_pids: Optional[Set[int]] = None) -> List[int]:
    """Delegate POSIX ``get_proc_ancestry`` to ``mesh.core.proc`` unchanged."""
    from mesh.core import proc as _core_proc

    return _core_proc.get_proc_ancestry(pid, stop_at_pids)


def find_run_processes(
    run_id: str,
    pgid: Optional[int],
    exclude: Optional[Set[int]] = None,
) -> Any:
    """Delegate POSIX ``find_run_processes`` to ``mesh.core.proc`` unchanged."""
    from mesh.core import proc as _core_proc

    return _core_proc.find_run_processes(run_id, pgid, exclude)


def crash_self_now() -> None:
    """Immediately kill the current process with SIGKILL on POSIX."""
    os.kill(os.getpid(), signal.SIGKILL)


def signal_if_same(
    pid: int,
    starttime: int,
    sig: int,
    run_id: Optional[str] = None,
    home: Optional[str] = None,
) -> bool:
    """Send *sig* to *pid* on POSIX only if it is live and matches *starttime*."""
    _ = (run_id, home)
    try:
        target_pid = int(pid)
        exp_st = int(starttime)
        sig_num = int(sig)
    except (TypeError, ValueError):
        return False
    if target_pid <= 1 or target_pid == os.getpid() or exp_st <= 0:
        return False
    try:
        info = get_proc_stat(target_pid)
        if info is None or int(info["starttime"]) != exp_st or not is_tgid_alive(target_pid, info):
            return False
    except OSError:
        return False
    try:
        os.kill(target_pid, sig_num)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def kill_if_same(pid: int, starttime: int) -> bool:
    """Send SIGKILL to *pid* on POSIX only if it is live and matches *starttime*."""
    return signal_if_same(pid, starttime, signal.SIGKILL)


def is_popen_exited_unreaped(proc: Any) -> bool:
    """True once ``proc`` has exited, without reaping it (``waitid`` with ``WNOWAIT``)."""
    if getattr(proc, "returncode", None) is not None:
        return True
    try:
        res = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return res is not None and res.si_pid == proc.pid


def detached_popen_kwargs() -> Dict[str, Any]:
    """Return ``subprocess.Popen`` kwargs to detach a background daemon on POSIX."""
    return {"start_new_session": True}


def reexec_clean_env(argv: List[str], env: Dict[str, str]) -> None:
    """Replace the current process image on POSIX via ``os.execve``."""
    os.execve(argv[0], argv, env)


def find_active_job_for_pid(
    pid: int,
    active_runs: Dict[str, int],
    *,
    active_job_handles: Optional[Dict[str, Any]] = None,
    home: Optional[str] = None,
) -> Optional[str]:
    """No-op on POSIX (active run sessions use POSIX sid/ancestry/environ)."""
    _ = (pid, active_runs, active_job_handles, home)
    return None


def daemon_endpoint_path(home: str) -> str:
    """Return the POSIX Unix domain socket path ``$MESH_HOME/run/mesh.sock``."""
    return os.path.join(home, "run", "mesh.sock")


def get_peer_credentials(sock: Any) -> tuple[int, int, int]:
    """Read ``(pid, uid, gid)`` from a connected Unix domain socket via ``SO_PEERCRED``."""
    import socket
    import struct

    ucred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", ucred)
    return pid, uid, gid


async def start_daemon_server(
    client_connected_cb: Any,
    path: str,
    *,
    backlog: int = 64,
    limit: int = 65536,
) -> Any:
    """Start the POSIX ``asyncio.start_unix_server`` listener with mode ``0o600``."""
    import asyncio

    server = await asyncio.start_unix_server(
        client_connected_cb,
        path=path,
        backlog=backlog,
        limit=limit,
    )
    os.chmod(path, 0o600)
    return server


def connect_endpoint(endpoint: str, timeout: Optional[float] = 30.0) -> Any:
    """Connect to a POSIX ``AF_UNIX`` socket *endpoint* with *timeout*."""
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(endpoint)
        return sock
    except OSError:
        sock.close()
        raise


def probe_daemon_endpoint(home_dir: str, timeout: float = 1.0) -> bool:
    """Return True if a daemon accepts connections on ``$MESH_HOME/run/mesh.sock``."""
    import socket

    sock_path = daemon_endpoint_path(home_dir)
    if not os.path.exists(sock_path):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        s.close()
