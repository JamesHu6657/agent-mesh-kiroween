"""meshd.log writer (blind review P3): one place that redacts, escapes and rotates.

Every line is redacted (recognized secrets), control characters in it are escaped (no
caller-supplied newline can forge an extra log line), and the file rotates at
``ROTATE_BYTES`` keeping ``KEEP`` old files (meshd.log.1 .. meshd.log.KEEP)."""

import os
import threading

from mesh.core.redact import redact

ROTATE_BYTES = 10 * 1024 * 1024
KEEP = 3
_lock = threading.Lock()


def escape_field(value: object) -> str:
    """``value`` as one log-safe token: redacted, control characters as \\xNN."""
    text = redact(str(value))
    return "".join(ch if (ch >= " " and ch != "\x7f") else f"\\x{ord(ch):02x}" for ch in text)


def _rotate(path: str) -> None:
    for i in range(KEEP, 0, -1):
        src = path if i == 1 else f"{path}.{i - 1}"
        dst = f"{path}.{i}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass


def append_line(home: str, text: str, name: str = "meshd.log") -> None:
    """Append one line (``text`` without its newline) to $home/logs/<name>; never raises."""
    try:
        logs_dir = os.path.join(home, "logs")
        os.makedirs(logs_dir, mode=0o700, exist_ok=True)
        path = os.path.join(logs_dir, name)
        line = escape_field(text.rstrip("\n")) + "\n"
        with _lock:
            try:
                if os.path.getsize(path) >= ROTATE_BYTES:
                    _rotate(path)
            except OSError:
                pass
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8", errors="replace"))
            finally:
                os.close(fd)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except Exception:
        pass
