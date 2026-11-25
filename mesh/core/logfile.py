"""meshd.log writer (blind review P3): one place that redacts, escapes and rotates.

Every line is redacted (recognized secrets), control characters in it are escaped (no
caller-supplied newline can forge an extra log line), and the file rotates at
``ROTATE_BYTES`` keeping ``KEEP`` old files (meshd.log.1 .. meshd.log.KEEP)."""

import os
import stat
import sys
import threading

from mesh.core.redact import redact
from mesh.platform import perms
from mesh.platform.fs import atomic_replace, open_regular_nofollow
from mesh.platform.paths import is_reparse_stat

ROTATE_BYTES = 10 * 1024 * 1024
KEEP = 4
_lock = threading.Lock()


def escape_field(value: object) -> str:
    """``value`` as one log-safe token: redacted, whitespace/=/control characters as \\xNN."""
    text = redact(str(value))
    return "".join(
        ch if (ch > " " and ch != "=" and not ("\x7f" <= ch <= "\x9f")) else f"\\x{ord(ch):02x}"
        for ch in text
    )


def _escape_line(value: str) -> str:
    """Redact and escape control characters across a formatted log line (preserving spaces)."""
    text = redact(value)
    return "".join(
        ch if (ch >= " " and not ("\x7f" <= ch <= "\x9f")) else f"\\x{ord(ch):02x}"
        for ch in text
    )


def _rotate(path: str) -> None:
    for i in range(KEEP, 0, -1):
        src = path if i == 1 else f"{path}.{i - 1}"
        dst = f"{path}.{i}"
        try:
            st = os.lstat(src)
        except OSError:
            continue
        if is_reparse_stat(st):
            continue
        try:
            atomic_replace(src, dst)
        except OSError:
            pass


def append_line(home: str, text: str, name: str = "meshd.log") -> None:
    """Append one line (``text`` without its newline) to $home/logs/<name>; never raises."""
    try:
        if (
            not isinstance(name, str)
            or not name
            or "\x00" in name
            or os.path.basename(name) != name
            or name in (".", "..")
        ):
            return
        line = _escape_line(text.rstrip("\n")) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        with _lock:
            st_home = os.lstat(home)
            if is_reparse_stat(st_home) or not stat.S_ISDIR(st_home.st_mode):
                return
            logs_dir = os.path.join(home, "logs")
            if os.path.lexists(logs_dir):
                st_logs = os.lstat(logs_dir)
                if is_reparse_stat(st_logs) or not stat.S_ISDIR(st_logs.st_mode):
                    return
            else:
                os.makedirs(logs_dir, mode=0o700, exist_ok=True)
                if sys.platform == "win32":
                    perms.set_private_dir_acl(logs_dir, 0o700)
            path = os.path.join(logs_dir, name)
            try:
                st = os.lstat(path)
                if not is_reparse_stat(st) and st.st_size >= ROTATE_BYTES:
                    _rotate(path)
            except OSError:
                pass
            if sys.platform == "win32":
                fd = open_regular_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600, label="log file")
            else:
                fd = os.open(path, flags, 0o600)
            try:
                perms.fchmod_private(fd, 0o600)
                os.write(fd, line.encode("utf-8", errors="replace"))
            finally:
                os.close(fd)
    except Exception:
        pass
