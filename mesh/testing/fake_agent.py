"""Fake agent runner for offline testing mode per plan §14.9."""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from mesh.core.paths import mesh_cli_argv
from mesh.platform import perms, pipe_pump, proc as plat_proc
from mesh.platform.fs import open_regular_nofollow
from mesh.platform.paths import temp_root

_IGNORE_STOP_SIGNAL = False


def parse_fake_spec(prompt_text: str) -> Optional[Dict[str, Any]]:
    """Extract first line matching ^FAKE ({.*})$."""
    for line in prompt_text.splitlines():
        line = line.strip()
        m = re.match(r"^FAKE\s+(\{.*\})$", line)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    return None


def _mesh_base_argv() -> List[str]:
    bin_mesh = os.environ.get("MESH_BIN")
    if bin_mesh:
        return [bin_mesh]
    if sys.platform == "win32":
        return mesh_cli_argv()
    return ["mesh"]


def _run_mesh(args: List[str]) -> "subprocess.CompletedProcess[str]":
    cmd = [*_mesh_base_argv(), *args]
    if sys.platform == "win32":
        cmd = pipe_pump.prepare_win_cmd_args(cmd)
    return subprocess.run(cmd, capture_output=True, text=True)


def _print_cli_result(res: "subprocess.CompletedProcess[str]") -> None:
    """Print rc and error.code of a mesh CLI call (never the token)."""
    print(f"rc={res.returncode}")
    try:
        env = json.loads(res.stdout)
    except Exception:
        return
    if isinstance(env, dict) and not env.get("ok"):
        print(f"error_code={(env.get('error') or {}).get('code')}")
    sys.stdout.flush()


def execute_behavior(behavior: str, params: Dict[str, Any]) -> None:
    """Execute a single fake behavior."""
    if behavior == "succeed":
        text = params.get("text", "Task completed successfully.")
        print(text)
        deliverable = {
            "summary": "Task completed.",
            "artifacts": [],
            "tests": {"command": "none", "result": "not_run"},
            "notes": "",
        }
        print("```mesh-deliverable")
        print(json.dumps(deliverable))
        print("```")
        sys.exit(0)

    elif behavior == "fail":
        code = int(params.get("code", 1))
        stderr_msg = params.get("stderr", "Simulated failure.")
        if stderr_msg:
            sys.stderr.write(stderr_msg + "\n")
            sys.stderr.flush()
        sys.exit(code)

    elif behavior == "fail_if_marker":
        # Attempt-dependent behaviour for rework/retry tests: the first run creates the
        # marker and continues; every later run (marker present) fails with `code`.
        path = str(params.get("path", ""))
        if path and os.path.exists(path):
            sys.stderr.write(str(params.get("stderr", "Simulated failure on a later attempt.")) + "\n")
            sys.stderr.flush()
            sys.exit(int(params.get("code", 1)))
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(os.environ.get("MESH_RUN_ID", ""))

    elif behavior == "sleep":
        seconds = float(params.get("seconds", 1))
        output_every = float(params.get("output_every", 0))
        if output_every > 0:
            elapsed = 0.0
            while elapsed < seconds:
                step = min(output_every, seconds - elapsed)
                time.sleep(step)
                elapsed += step
                print(f"Slept {elapsed:.1f}s")
                sys.stdout.flush()
        else:
            time.sleep(seconds)

    elif behavior == "wait_release":
        # Block until the test creates `path` (or `timeout_s` passes): keeps the run
        # alive for exactly as long as the test needs, independent of host load.
        path = str(params["path"])
        deadline = time.monotonic() + float(params.get("timeout_s", 120))
        while not os.path.exists(path) and time.monotonic() < deadline:
            time.sleep(0.05)

    elif behavior == "ignore_sigterm":
        global _IGNORE_STOP_SIGNAL
        _IGNORE_STOP_SIGNAL = True
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        while True:
            time.sleep(1)

    elif behavior == "spawn_grandchild":
        # A child in the same process group (and session) that outlives this step.
        # Its stdio is /dev/null so the wrapper's log pipes still close when the agent exits.
        seconds = float(params.get("seconds", 1000))
        proc = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({seconds})"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"grandchild_pid={proc.pid}")
        sys.stdout.flush()

    elif behavior == "spawn_setsid_grandchild":
        # An escapee: new session and process group (start_new_session=True runs
        # setsid() in the child), but it keeps the inherited MESH_RUN_ID (§11.4).
        # With "ignore_sigterm": true it only dies on SIGKILL (keeps a kill.grace_s window).
        seconds = float(params.get("seconds", 1000))
        code = f"import time; time.sleep({seconds})"
        if params.get("ignore_sigterm"):
            code = (
                "import signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "sys.platform == 'win32' and hasattr(signal, 'SIGBREAK') and signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
                + code
            )
        # "env": {...} replaces the environment (a sanitized child without MESH_RUN_ID).
        env = {str(k): str(v) for k, v in params["env"].items()} if isinstance(params.get("env"), dict) else None
        popen_kw: Dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": env,
        }
        if sys.platform == "win32":
            popen_kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kw["start_new_session"] = True
        proc = subprocess.Popen([sys.executable, "-c", code], **popen_kw)
        if params.get("path"):
            with open(str(params["path"]), "w", encoding="utf-8") as f:
                f.write(f"{proc.pid}\n")
        print(f"grandchild_pid={proc.pid}")
        sys.stdout.flush()

    elif behavior == "spawn_envless_member":
        # N18: a member of the run's process group and session (no setsid) with an
        # empty environment (so no MESH_RUN_ID) that ignores SIGTERM and blocks on a
        # read of its own pipe (no sleep): only SIGKILL ends it.
        # Readiness handshake (r4b): the member writes "ready" to its stdout pipe only
        # after SIG_IGN is installed; this step blocks on that line before it records
        # the pid. `path` then gets two lines, "<pid>" and "ready", so whoever reads the
        # pid file may SIGTERM the member at once. With `trigger`, this step then waits
        # for that file to appear, so the test decides when the agent (and its wrapper)
        # exit.
        code = (
            "import os, signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "sys.platform == 'win32' and hasattr(signal, 'SIGBREAK') and signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
            "os.write(1, b'ready\\n'); r, w = os.pipe(); os.read(r, 1)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={},
        )
        line = proc.stdout.readline() if proc.stdout is not None else b""
        if proc.stdout is not None:
            proc.stdout.close()
        if line.strip() != b"ready":
            print(f"member_not_ready pid={proc.pid}")
            sys.stdout.flush()
            sys.exit(3)
        path = params.get("path")
        if path:
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(f"{proc.pid}\nready\n")
            os.replace(tmp, str(path))
        print(f"member_pid={proc.pid}")
        sys.stdout.flush()
        trigger = params.get("trigger")
        if trigger:
            deadline = time.monotonic() + float(params.get("timeout_s", 120))
            while not os.path.exists(str(trigger)) and time.monotonic() < deadline:
                time.sleep(0.05)

    elif behavior == "write_file":
        rel_path = params.get("path", "output.txt")
        content = params.get("content", "")
        parent = os.path.dirname(rel_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(rel_path, "w", encoding="utf-8") as f:
            f.write(content)

    elif behavior == "git_commit":
        rel_path = params.get("path", "file.txt")
        content = params.get("content", "commit content")
        with open(rel_path, "w", encoding="utf-8") as f:
            f.write(content)
        subprocess.run(["git", "add", rel_path], check=True)
        subprocess.run(["git", "commit", "-m", params.get("message", "fake commit")], check=True)

    elif behavior == "print_secrets":
        mesh_token = os.environ.get("MESH_TOKEN", "")
        fake_api_key = os.environ.get("FAKE_SECRET_API_KEY", "")
        print(f"Token: {mesh_token}")
        print(f"ApiKey: {fake_api_key}")

    elif behavior == "emit_session":
        session_id = params.get("session_id", "session_fake_123")
        print(json.dumps({"session_id": session_id}))

    elif behavior == "lead_actions":
        # A lead turn's final output (§12.4): the given actions as one mesh-actions block.
        print("```mesh-actions")
        print(json.dumps(params.get("actions", [])))
        print("```")
        sys.stdout.flush()
        sys.exit(0)

    elif behavior == "record_pid":
        path = params.get("path", os.path.join(temp_root(), "acc-pids"))
        run_id = os.environ.get("MESH_RUN_ID", "no_run_id")
        line = f"{os.getpid()},{time.time()},{run_id}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    elif behavior == "submit_via_cli":
        # Run and fence come from MESH_RUN_ID / MESH_FENCE via the CLI; the token from MESH_TOKEN.
        summary = params.get("summary", "Submitted via CLI.")
        _print_cli_result(_run_mesh(["task", "submit", os.environ.get("MESH_TASK_ID", ""), "--summary", summary]))

    elif behavior == "try_approve":
        # The executor run token tries to decide an approval (§7.2, G6): must be rc 4.
        args = ["approval", "approve", str(params.get("approval", "")), "--op-hash", str(params.get("op_hash", ""))]
        if params.get("relayed"):
            args += ["--relayed", "--user-statement", str(params.get("user_statement", "approved"))]
        _print_cli_result(_run_mesh(args))

    elif behavior == "stale_submit":
        # Sleep, then submit with the run's own (possibly stale) run id and fence.
        time.sleep(float(params.get("seconds", 1)))
        _print_cli_result(_run_mesh(["task", "submit", os.environ.get("MESH_TASK_ID", ""), "--summary", "stale"]))

    elif behavior == "steal_token":
        # Use another principal's token file from inside a run (G25/G26).
        tok_path = str(params.get("path", ""))
        args = [str(a) for a in params.get("args", [])]
        _print_cli_result(_run_mesh([*args, "--token-file", tok_path]))

    elif behavior == "bad_passphrase":
        # Each call gets a fresh pipe (or private file on Windows) holding a wrong passphrase (G27).
        count = int(params.get("count", 1))
        base_cmd = _mesh_base_argv()
        if sys.platform == "win32":
            for _ in range(count):
                pp_dir = tempfile.mkdtemp(prefix="mesh-fake-pp-", dir=temp_root())
                try:
                    perms.set_private_dir_acl(pp_dir, 0o700)
                    pp_file = os.path.join(pp_dir, "passphrase")
                    fd = open_regular_nofollow(
                        pp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, label="passphrase file"
                    )
                    try:
                        perms.fchmod_private(fd, 0o600)
                        data = b"wrong-passphrase-0000\n"
                        view = memoryview(data)
                        while view:
                            n = os.write(fd, view)
                            view = view[n:]
                    finally:
                        os.close(fd)
                    cmd = pipe_pump.prepare_win_cmd_args([*base_cmd, "whoami", "--user", "--passphrase-file", pp_file])
                    res = subprocess.run(cmd, capture_output=True, text=True)
                finally:
                    shutil.rmtree(pp_dir, ignore_errors=True)
                _print_cli_result(res)
        else:
            for _ in range(count):
                r, w = os.pipe()
                try:
                    os.write(w, b"wrong-passphrase-0000\n")
                    os.close(w)
                    cmd = [*base_cmd, "whoami", "--user", "--passphrase-fd", str(r)]
                    res = subprocess.run(cmd, capture_output=True, text=True, pass_fds=(r,))
                finally:
                    os.close(r)
                _print_cli_result(res)


def _spec_has_ignore_sigterm(spec: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(spec, dict):
        return False
    if "ignore_sigterm" in spec:
        return True
    steps = spec.get("steps")
    if isinstance(steps, list):
        return any(isinstance(s, dict) and "ignore_sigterm" in s for s in steps)
    return False


def _start_win_stop_watcher(prompt_path: str, spec: Optional[Dict[str, Any]]) -> None:
    """Exit gracefully when the run's Windows Named Stop Event signals (unless ignoring stop)."""
    global _IGNORE_STOP_SIGNAL
    if sys.platform != "win32" or _spec_has_ignore_sigterm(spec):
        if _spec_has_ignore_sigterm(spec):
            _IGNORE_STOP_SIGNAL = True
        return
    run_id = os.environ.get("MESH_RUN_ID")
    if not run_id:
        return
    home = os.environ.get("MESH_HOME")
    if not home:
        run_dir = os.path.realpath(os.path.dirname(os.path.abspath(prompt_path)))
        parent = os.path.dirname(run_dir)
        if os.path.basename(parent) in ("runs", "merges"):
            home = os.path.dirname(parent)
    if not home:
        return
    try:
        ev_name = plat_proc.run_stop_event_name(home, run_id)
    except ValueError:
        return

    from mesh.platform import _win32_ctypes as w32

    k32 = w32.get_kernel32()
    h_ev = k32.OpenEventW(w32.SYNCHRONIZE, False, ev_name)
    if w32.is_null_or_invalid_handle(h_ev):
        return

    def _watch() -> None:
        try:
            rc = int(k32.WaitForSingleObject(h_ev, w32.INFINITE))
            if rc == w32.WAIT_OBJECT_0 and not _IGNORE_STOP_SIGNAL:
                # Allow SIGBREAK handler a short window if console delivery succeeded first.
                time.sleep(0.05)
                if not _IGNORE_STOP_SIGNAL:
                    for stream in (sys.stdout, sys.stderr):
                        if stream is not None:
                            try:
                                stream.flush()
                            except Exception:
                                pass
                    os._exit(143)
        finally:
            k32.CloseHandle(h_ev)

    threading.Thread(target=_watch, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake agent runner for offline test mode")
    parser.add_argument("--fake-profile", help="Fake permission profile level")
    parser.add_argument("--fake-resume", help="Session id supplied by Mesh")
    parser.add_argument("prompt_path", help="Path to prompt markdown file")
    args = parser.parse_args()
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda _s, _f: sys.exit(143))
    if args.fake_resume:
        print(json.dumps({"fake_resume": args.fake_resume}), flush=True)

    prompt_content = ""
    if os.path.exists(args.prompt_path):
        with open(args.prompt_path, "r", encoding="utf-8") as f:
            prompt_content = f.read()

    spec = parse_fake_spec(prompt_content)
    if sys.platform == "win32":
        _start_win_stop_watcher(args.prompt_path, spec)
    if not spec:
        # Default behavior: succeed
        print("Default fake agent execution: succeed.")
        deliverable = {
            "summary": "Completed default task.",
            "artifacts": [],
            "tests": {"command": "none", "result": "not_run"},
            "notes": "",
        }
        print("```mesh-deliverable")
        print(json.dumps(deliverable))
        print("```")
        sys.exit(0)

    if "steps" in spec and isinstance(spec["steps"], list):
        for step in spec["steps"]:
            if isinstance(step, dict):
                for b_name, b_params in step.items():
                    execute_behavior(b_name, b_params if isinstance(b_params, dict) else {})
    else:
        for b_name, b_params in spec.items():
            execute_behavior(b_name, b_params if isinstance(b_params, dict) else {})


if __name__ == "__main__":
    main()
