"""runwrap: wrapper that starts one launched child and captures its streams.

Per plan §11.2:
1. ``wrapper.json`` = ``{pid, starttime}`` written atomically on entry.
2. The child starts in the same process group (no new session), with pipes for
   stdout/stderr and DEVNULL or a prompt file on stdin.
3. Both streams are redacted line by line (§15.2) into ``stdout.log`` /
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

import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from mesh.core.clock import now_iso_utc
from mesh.core.proc import get_proc_stat
from mesh.core.redact import collect_exact_values, redact

DEFAULT_LOG_CAP_BYTES = 50 * 1024 * 1024
ACTIVITY_TOUCH_INTERVAL_S = 1.0
PIPE_DRAIN_GRACE_S = 5.0


def atomic_write_json(path: str, obj: Dict[str, Any]) -> None:
    """Write JSON atomically (tmp + fsync + rename) with mode 0600."""
    tmp_path = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_spec(run_dir: str) -> Dict[str, Any]:
    with open(os.path.join(run_dir, "spec.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def redaction_values(spec: Dict[str, Any], env: Dict[str, str]) -> List[Tuple[str, str]]:
    """Exact values to redact: env-derived secrets plus the spec's named vars."""
    extra = [(name, env[name]) for name in spec.get("redact_env_names", []) if name in env]
    return collect_exact_values(env=env, extra=extra)


def _pump_stream(
    pipe: Any,
    log_path: str,
    exact_values: List[Tuple[str, str]],
    activity_path: str,
    cap_bytes: int,
) -> None:
    """Copy one child stream into its log, redacted line by line, capped."""
    written = 0
    truncated = False
    last_touch = 0.0
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as log_file:
            while True:
                chunk = pipe.readline()
                if not chunk:
                    break
                now = time.monotonic()
                if now - last_touch >= ACTIVITY_TOUCH_INTERVAL_S:
                    try:
                        os.utime(activity_path, None)
                    except OSError:
                        pass
                    last_touch = now
                text = chunk.decode("utf-8", errors="replace")
                data = redact(text, exact_values=exact_values).encode("utf-8")
                if written >= cap_bytes:
                    if not truncated:
                        log_file.write(b"\n[runwrap] log cap reached; output truncated\n")
                        log_file.flush()
                        truncated = True
                    continue
                room = cap_bytes - written
                if len(data) > room:
                    data = data[:room] + b"\n[runwrap] log cap reached; output truncated\n"
                    truncated = True
                log_file.write(data)
                log_file.flush()
                written += len(data)
    finally:
        try:
            os.close(fd)
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
    open(activity_path, "a", encoding="utf-8").close()

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
    threads = [
        threading.Thread(
            target=_pump_stream,
            args=(pipe, os.path.join(run_dir, name), exact_values, activity_path, cap_bytes),
            daemon=True,
        )
        for pipe, name in streams
    ]
    for thread in threads:
        thread.start()

    returncode = child.wait()
    # A descendant that escaped may keep the pipes open; do not let it hold back
    # exit.json (the supervisor deals with leftovers, plan §11.3).
    join_deadline = time.monotonic() + PIPE_DRAIN_GRACE_S
    for thread in threads:
        thread.join(max(0.0, join_deadline - time.monotonic()))

    exit_code: Optional[int] = returncode if returncode >= 0 else None
    term_signal: Optional[int] = -returncode if returncode < 0 else None
    atomic_write_json(
        os.path.join(run_dir, "exit.json"),
        {
            "exit_code": exit_code,
            "signal": term_signal,
            "ended_at": now_iso_utc(),
        },
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: python3 -m mesh.runwrap <run_dir>\n")
        return 2
    return run(os.path.abspath(args[0]))


if __name__ == "__main__":
    sys.exit(main())
