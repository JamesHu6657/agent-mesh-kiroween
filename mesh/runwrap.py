"""runwrap: wrapper that starts one launched child and captures its streams.

Per plan §11.2:
1. ``wrapper.json`` = ``{pid, starttime}`` written atomically on entry.
2. The child starts in the same process group (no new session), with pipes for
   stdout/stderr and DEVNULL or a prompt file on stdin.
3. Both streams are redacted as a stream (§15.2; secrets spanning lines too) into ``stdout.log`` /
   ``stderr.log`` (0600), each capped at 50 MiB and drained after the cap.
4. ``activity`` (mtime) is touched at most once per second while bytes arrive.
5. On child exit ``exit.json`` = ``{exit_code, signal, ended_at}`` is written
   atomically.
6. SIGTERM is forwarded to the child; the wrapper keeps draining and still
   writes ``exit.json``.

``spec.json`` carries no env values and no secrets: ``argv``, ``cwd``, ``stdin``,
``redact_env_names`` and optional ``idle_timeout_s`` / ``log_cap_bytes`` only.

Merge test step (m2-r6, §10.5 step 3) options:

- ``combined_log``: a file name in the run dir; stderr is merged into stdout and the
  one stream is redacted into that file (``test.log``) instead of the two logs.
- ``restore_pythonpath``: the child gets the caller's own ``PYTHONPATH`` back (from
  ``_MESH_ORIG_PYTHONPATH``; ``__UNSET__`` or absent → unset). The wrapper itself
  needs the install root on ``PYTHONPATH`` to start; the test command must not see it.
"""

import codecs
import errno
import io
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from mesh.core.clock import now_iso_utc
from mesh.core.proc import get_proc_stat
from mesh.core.redact import StreamRedactor, collect_exact_values, redact

DEFAULT_LOG_CAP_BYTES = 50 * 1024 * 1024
ACTIVITY_TOUCH_INTERVAL_S = 1.0
PIPE_DRAIN_GRACE_S = 5.0


def _open_regular_nofollow(path: str, flags: int, mode: int = 0o600, label: str = "file") -> int:
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


def atomic_write_json(path: str, obj: Dict[str, Any]) -> None:
    """Write JSON atomically (mkstemp O_EXCL|O_NOFOLLOW + fsync + rename) with mode 0600."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    base = os.path.basename(path)
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=f".{base}.tmp.", suffix=".json")
    try:
        try:
            os.fchmod(fd, 0o600)
            payload = (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8")
            view = memoryview(payload)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_spec(run_dir: str) -> Dict[str, Any]:
    with open(os.path.join(run_dir, "spec.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def redaction_values(spec: Dict[str, Any], env: Dict[str, str]) -> List[Tuple[str, str]]:
    """Exact values to redact: env-derived secrets plus the spec's named vars."""
    extra = [(name, env[name]) for name in spec.get("redact_env_names", []) if name in env]
    return collect_exact_values(env=env, extra=extra)


READ_CHUNK_BYTES = 64 * 1024


def _utf8_safe_prefix(data: bytes, limit: int) -> bytes:
    """Slice UTF-8 *data* to at most *limit* bytes without splitting a multi-byte codepoint."""
    if limit <= 0:
        return b""
    if len(data) <= limit:
        return data
    end = limit
    pos = end - 1
    while pos >= 0 and (data[pos] & 0xC0) == 0x80 and (end - pos) < 4:
        pos -= 1
    if pos >= 0 and (data[pos] & 0xC0) == 0xC0:
        lead = data[pos]
        if (lead & 0xE0) == 0xC0:
            need = 2
        elif (lead & 0xF0) == 0xE0:
            need = 3
        elif (lead & 0xF8) == 0xF0:
            need = 4
        else:
            need = 1
        if end - pos < need:
            return data[:pos]
    return data[:end]


def _pump_stream(
    pipe: Any,
    log_path: str,
    exact_values: List[Tuple[str, str]],
    activity_path: str,
    cap_bytes: int,
    errors: Optional[List[str]] = None,
    drain_wake_fd: Optional[int] = None,
    drain_deadline: Optional[List[Optional[float]]] = None,
) -> None:
    """Copy one child stream into its log, redacted, capped.

    Reads are non-blocking via ``PollSelector`` with a post-exit drain deadline so
    output without newlines updates activity, escaped descendants holding pipes past
    ``PIPE_DRAIN_GRACE_S`` still flush buffered ``StreamRedactor`` lines before
    ``exit.json`` is written, and capture errors drain safely without deadlocking."""
    written = 0
    truncated = False
    last_touch = 0.0
    redactor = StreamRedactor(exact_values, jsonl=True)  # full review r1 P1-1
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    log_file = None
    try:
        fd = _open_regular_nofollow(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600, label="log file")
        try:
            os.fchmod(fd, 0o600)
            log_file = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
    except OSError as exc:
        if errors is not None:
            errors.append(f"{os.path.basename(log_path)}: {type(exc).__name__}: {exc.strerror or exc}")
        log_file = None

    def write(data: bytes) -> None:
        nonlocal written, truncated, log_file
        if log_file is None or not data:
            return
        if written >= cap_bytes:
            if not truncated:
                data = b"\n[runwrap] log cap reached; output truncated\n"
                truncated = True
            else:
                return
        else:
            room = cap_bytes - written
            if len(data) > room:
                data = _utf8_safe_prefix(data, room) + b"\n[runwrap] log cap reached; output truncated\n"
                truncated = True
        try:
            log_file.write(data)
            log_file.flush()
        except OSError as exc:
            if errors is not None:
                errors.append(f"{os.path.basename(log_path)}: {type(exc).__name__}: {exc.strerror or exc}")
            try:
                log_file.close()
            except OSError:
                pass
            log_file = None
            return
        written += len(data)

    def handle_chunk(chunk: bytes) -> None:
        nonlocal last_touch
        now = time.monotonic()
        if now - last_touch >= ACTIVITY_TOUCH_INTERVAL_S:
            try:
                if os.utime in os.supports_follow_symlinks:
                    os.utime(activity_path, None, follow_symlinks=False)
                else:
                    st_act = os.lstat(activity_path)
                    if stat.S_ISREG(st_act.st_mode):
                        os.utime(activity_path, None)
            except OSError:
                pass
            last_touch = now
        if log_file is None or (truncated and written >= cap_bytes):
            return  # drain only
        write(redactor.feed(decoder.decode(chunk)).encode("utf-8", "replace"))

    def past_deadline() -> bool:
        dl = drain_deadline[0] if drain_deadline else None
        return dl is not None and time.monotonic() >= dl

    sel = None
    try:
        pipe_fd: Optional[int] = None
        if hasattr(pipe, "fileno"):
            try:
                pipe_fd = pipe.fileno()
            except (AttributeError, ValueError, io.UnsupportedOperation, OSError):
                pipe_fd = None

        if pipe_fd is not None and (drain_wake_fd is not None or drain_deadline is not None):
            os.set_blocking(pipe_fd, False)
            sel = selectors.PollSelector()
            sel.register(pipe_fd, selectors.EVENT_READ, data="pipe")
            wake_watched = False
            if drain_wake_fd is not None:
                sel.register(drain_wake_fd, selectors.EVENT_READ, data="wake")
                wake_watched = True

            eof = False
            while not eof:
                dl = drain_deadline[0] if drain_deadline else None
                if dl is not None:
                    if wake_watched:
                        try:
                            sel.unregister(drain_wake_fd)
                        except (KeyError, ValueError):
                            pass
                        wake_watched = False
                    remaining = dl - time.monotonic()
                    if remaining <= 0:
                        break
                    timeout: Optional[float] = remaining
                else:
                    timeout = None

                events = sel.select(timeout)
                if not events:
                    break
                pipe_ready = False
                for key, _ in events:
                    if key.data == "wake":
                        if wake_watched:
                            try:
                                sel.unregister(drain_wake_fd)
                            except (KeyError, ValueError):
                                pass
                            wake_watched = False
                    elif key.data == "pipe":
                        pipe_ready = True
                if not pipe_ready:
                    continue
                while True:
                    try:
                        chunk = os.read(pipe_fd, READ_CHUNK_BYTES)
                    except BlockingIOError:
                        break
                    except InterruptedError:
                        continue
                    except OSError:
                        eof = True
                        break
                    if not chunk:
                        eof = True
                        break
                    handle_chunk(chunk)
                    if past_deadline():
                        eof = True
                        break
        else:
            while True:
                chunk = pipe.read1(READ_CHUNK_BYTES) if hasattr(pipe, "read1") else os.read(pipe.fileno(), READ_CHUNK_BYTES)
                if not chunk:
                    break
                handle_chunk(chunk)

        write(redactor.feed(decoder.decode(b"", final=True)).encode("utf-8", "replace"))
        write(redactor.flush().encode("utf-8", "replace"))
    finally:
        if sel is not None:
            try:
                sel.close()
            except OSError:
                pass
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass


def run(run_dir: str) -> int:
    """Run one wrapper life cycle; returns the process exit code (0 on success)."""
    spec = load_spec(run_dir)
    # Testing only (R9/I10): meshd writes this field only in testing mode with
    # MESH_TEST_CRASH_AT=after_popen, to widen the spawn window (plan risk 6).
    delay = spec.get("testing_wrapper_delay_s")
    if isinstance(delay, (int, float)) and 0 < delay <= 10:
        time.sleep(float(delay))
    activity_path = os.path.join(run_dir, "activity")
    act_fd = _open_regular_nofollow(
        activity_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600, label="activity file"
    )
    os.close(act_fd)

    try:
        stat_info = get_proc_stat(os.getpid())
    except OSError:
        stat_info = None
    atomic_write_json(
        os.path.join(run_dir, "wrapper.json"),
        {
            "pid": os.getpid(),
            "starttime": stat_info["starttime"] if stat_info else 0,
        },
    )

    env = dict(os.environ)
    exact_values = redaction_values(spec, env)
    if spec.get("restore_pythonpath"):
        orig = env.pop("_MESH_ORIG_PYTHONPATH", "__UNSET__")
        if orig == "__UNSET__":
            env.pop("PYTHONPATH", None)
        else:
            env["PYTHONPATH"] = orig
    combined_log = spec.get("combined_log")
    if combined_log is not None and (not isinstance(combined_log, str) or not combined_log
                                     or os.path.basename(combined_log) != combined_log):
        combined_log = "test.log"

    stdin_spec = spec.get("stdin", "devnull")
    stdin_file = None
    if stdin_spec and stdin_spec != "devnull":
        stdin_file = open(stdin_spec, "rb")
        child_stdin: Any = stdin_file
    else:
        child_stdin = subprocess.DEVNULL

    try:
        try:
            child = subprocess.Popen(
                spec["argv"],
                cwd=spec.get("cwd") or None,
                stdin=child_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if combined_log else subprocess.PIPE,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            # The child never started: still leave exit.json so the supervisor sees an end.
            atomic_write_json(
                os.path.join(run_dir, "exit.json"),
                {"exit_code": 127, "signal": None, "ended_at": now_iso_utc(), "error": type(exc).__name__},
            )
            return 0
    finally:
        if stdin_file is not None:
            stdin_file.close()

    def on_signal(signum: int, frame: Any) -> None:
        try:
            child.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    cap_bytes = int(spec.get("log_cap_bytes") or DEFAULT_LOG_CAP_BYTES)
    if combined_log:
        streams = [(child.stdout, combined_log)]
    else:
        streams = [(child.stdout, "stdout.log"), (child.stderr, "stderr.log")]
    capture_errors: List[str] = []
    wake_r, wake_w = os.pipe()
    wake_w_open = True
    drain_deadline: List[Optional[float]] = [None]
    threads = [
        threading.Thread(
            target=_pump_stream,
            args=(pipe, os.path.join(run_dir, name), exact_values, activity_path, cap_bytes,
                  capture_errors, wake_r, drain_deadline),
            daemon=True,
        )
        for pipe, name in streams
    ]
    try:
        for thread in threads:
            thread.start()

        returncode = child.wait()
        # A descendant that escaped may keep the pipes open; let pump threads drain until
        # PIPE_DRAIN_GRACE_S, flush buffered lines and close logs before writing exit.json.
        drain_deadline[0] = time.monotonic() + PIPE_DRAIN_GRACE_S
        try:
            os.close(wake_w)
            wake_w_open = False
        except OSError:
            pass
        join_deadline = drain_deadline[0] + 1.0
        for thread in threads:
            thread.join(max(0.0, join_deadline - time.monotonic()))
    finally:
        if wake_w_open:
            try:
                os.close(wake_w)
            except OSError:
                pass
        try:
            os.close(wake_r)
        except OSError:
            pass
        for (pipe, _), thread in zip(streams, threads):
            if pipe is not None and not thread.is_alive():
                try:
                    pipe.close()
                except OSError:
                    pass

    # Full review r5 P2-6: a pump still running past its deadline did not write the end
    # of the output; the run is not a clean success on its exit code alone.
    hung = [os.path.basename(name) for (_, name), thread in zip(streams, threads) if thread.is_alive()]
    if hung:
        capture_errors.append("output capture did not finish: " + ", ".join(hung))
    exit_code: Optional[int] = returncode if returncode >= 0 else None
    term_signal: Optional[int] = -returncode if returncode < 0 else None
    exit_doc: Dict[str, Any] = {
        "exit_code": exit_code,
        "signal": term_signal,
        "ended_at": now_iso_utc(),
    }
    if capture_errors:
        # Blind review P2: output was lost; the supervisor must not accept the run as a
        # clean success on exit code alone.
        exit_doc["capture_error"] = "; ".join(capture_errors)[:500]
    atomic_write_json(os.path.join(run_dir, "exit.json"), exit_doc)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: python3 -m mesh.runwrap <run_dir>\n")
        return 2
    return run(os.path.abspath(args[0]))


if __name__ == "__main__":
    sys.exit(main())
