"""Where the user passphrase comes from (CLI side only; meshd never reads it from env).

Order, first match wins:

1. ``--passphrase-fd N``: read from an open file descriptor;
2. ``--passphrase-file PATH``: read from a file that must be owned by the caller and not
   readable or writable by group/other (mode 0600 or stricter);
3. ``MESH_PASSPHRASE_FILE``: the same, from the environment;
4. ``MESH_PASSPHRASE``: the passphrase itself, from the environment;
5. an interactive prompt on the terminal (``/dev/tty``).

Environment variables are convenient for scripts and CI but are visible to every process
of the same user that can read ``/proc/<pid>/environ``. Mesh strips every ``MESH_*``
variable from the environment of the agents it launches, so a launched agent does not
inherit them, but prefer a 0600 file (or the fd) on shared machines.

Earlier versions ignored ``MESH_PASSPHRASE`` on purpose (checklist G17); it is now an
explicit, documented opt-in source. A passphrase in stdin JSON is still rejected.
"""

import errno
import getpass
import os
import stat
import sys
from typing import Any, Optional

from mesh import constants as c
from mesh.errors import MeshError
from mesh.platform import perms
from mesh.platform.fs import open_regular_nofollow

ENV_PASSPHRASE = "MESH_PASSPHRASE"
ENV_PASSPHRASE_FILE = "MESH_PASSPHRASE_FILE"
MAX_PASSPHRASE_BYTES = 16384

_NO_SOURCE = ("No passphrase source: no terminal for a prompt and none of --passphrase-fd, "
              "--passphrase-file, MESH_PASSPHRASE_FILE or MESH_PASSPHRASE is set")


def _read_fd(fd: int) -> str:
    try:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            pass
        dup_fd = os.dup(fd)
        with os.fdopen(dup_fd, "r", encoding="utf-8") as f:
            content = f.read(MAX_PASSPHRASE_BYTES + 1)
        if len(content) > MAX_PASSPHRASE_BYTES:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Passphrase from fd {fd} exceeds maximum length ({MAX_PASSPHRASE_BYTES} characters)",
            )
        return content.rstrip("\r\n")
    except MeshError:
        raise
    except Exception as exc:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Failed to read passphrase from fd {fd}: {exc}",
        )


def _read_file(path: str, source: str) -> str:
    path = os.path.expanduser(path)
    if sys.platform == "win32":
        try:
            fd = open_regular_nofollow(path, os.O_RDONLY, label="passphrase file")
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                msg = f"Passphrase file {path} ({source}) cannot be a symlink"
            elif exc.errno in (errno.EINVAL, errno.EISDIR):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Passphrase file {path} ({source}) is not a regular file",
                    details={"path": path, "source": source},
                ) from exc
            else:
                msg = f"Cannot read passphrase file {path} ({source}): {exc.strerror or exc}"
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=msg,
                details={"path": path, "source": source},
            ) from exc
        try:
            try:
                perms.verify_private_owner_and_perms(
                    fd,
                    is_dir=False,
                    label=path,
                    error_code=c.INSECURE_PERMISSIONS,
                    exit_code=c.EXIT_USAGE,
                )
            except OSError as exc:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Passphrase file {path} ({source}) is not a regular file",
                    details={"path": path, "source": source},
                ) from exc
        except BaseException:
            os.close(fd)
            raise
    else:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            msg = (
                f"Passphrase file {path} ({source}) cannot be a symlink"
                if exc.errno == errno.ELOOP
                else f"Cannot read passphrase file {path} ({source}): {exc.strerror or exc}"
            )
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=msg,
                details={"path": path, "source": source},
            ) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Passphrase file {path} ({source}) is not a regular file",
                    details={"path": path, "source": source},
                )
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise MeshError(
                    code=c.INSECURE_PERMISSIONS,
                    exit_code=c.EXIT_USAGE,
                    message=(f"Passphrase file {path} ({source}) must be owned by you with mode 0600 "
                             f"(run: chmod 600 {path})"),
                    details={"path": path, "source": source, "mode": oct(stat.S_IMODE(st.st_mode))},
                )
        except BaseException:
            os.close(fd)
            raise
    with os.fdopen(fd, "r", encoding="utf-8", errors="strict") as f:
        try:
            content = f.read(MAX_PASSPHRASE_BYTES + 1)
        except (OSError, UnicodeDecodeError) as exc:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Cannot read passphrase file {path} ({source}): {exc}",
                details={"path": path, "source": source},
            ) from exc
    if len(content) > MAX_PASSPHRASE_BYTES:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Passphrase file {path} ({source}) exceeds maximum length ({MAX_PASSPHRASE_BYTES} characters)",
            details={"path": path, "source": source},
        )
    return content.rstrip("\r\n")


def _win_stdin_is_console() -> bool:
    """Return True iff fd 0 on Windows is backed by a real console input buffer."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.argtypes = [wintypes.DWORD]
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetConsoleMode.restype = wintypes.BOOL
        h_in = k32.GetStdHandle(wintypes.DWORD(-10))
        if not h_in or h_in == wintypes.HANDLE(-1).value:
            return False
        mode = wintypes.DWORD(0)
        return bool(k32.GetConsoleMode(h_in, ctypes.byref(mode)))
    except Exception:
        return False


def _prompt(prompt: str) -> str:
    try:
        if os.isatty(0) and _win_stdin_is_console():
            return getpass.getpass(prompt)
        tty_path = "CONIN$" if sys.platform == "win32" else "/dev/tty"
        with open(tty_path, "r"):
            pass
        return getpass.getpass(prompt, stream=sys.stderr)
    except Exception:
        raise MeshError(code=c.NO_TTY, exit_code=c.EXIT_UNAUTHENTICATED, message=_NO_SOURCE)


def read_passphrase(args: Any, prompt: str = "Enter user passphrase: ",
                    env: Optional[dict] = None) -> str:
    env = os.environ if env is None else env
    fd = getattr(args, "passphrase_fd", None)
    if fd is not None:
        return _read_fd(fd)
    path = getattr(args, "passphrase_file", None)
    if path is not None:
        if not path:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Passphrase file path (--passphrase-file) cannot be empty",
                details={"source": "--passphrase-file"},
            )
        return _read_file(path, "--passphrase-file")
    if ENV_PASSPHRASE_FILE in env:
        env_path = env[ENV_PASSPHRASE_FILE]
        if not env_path:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"{ENV_PASSPHRASE_FILE} cannot be empty",
                details={"source": ENV_PASSPHRASE_FILE},
            )
        return _read_file(env_path, ENV_PASSPHRASE_FILE)
    if ENV_PASSPHRASE in env:
        val = env[ENV_PASSPHRASE].rstrip("\r\n")
        if not val:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"{ENV_PASSPHRASE} cannot be empty",
                details={"source": ENV_PASSPHRASE},
            )
        if len(val) > MAX_PASSPHRASE_BYTES:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"{ENV_PASSPHRASE} exceeds maximum length ({MAX_PASSPHRASE_BYTES} characters)",
                details={"source": ENV_PASSPHRASE},
            )
        return val
    return _prompt(prompt)
