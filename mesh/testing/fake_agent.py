"""Fake agent runner for offline testing mode per plan §14.9."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional


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


def _run_mesh(args: List[str]) -> "subprocess.CompletedProcess[str]":
    bin_mesh = os.environ.get("MESH_BIN", "mesh")
    return subprocess.run([bin_mesh, *args], capture_output=True, text=True)


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
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
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
            code = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); " + code
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
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
        code = ("import os, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "os.write(1, b'ready\\n'); r, w = os.pipe(); os.read(r, 1)")
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
        path = params.get("path", "/tmp/acc-pids")
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
        # Each call gets a fresh pipe holding a wrong passphrase (G27).
        count = int(params.get("count", 1))
        bin_mesh = os.environ.get("MESH_BIN", "mesh")
        for _ in range(count):
            r, w = os.pipe()
            try:
                os.write(w, b"wrong-passphrase-0000\n")
                os.close(w)
                cmd = [bin_mesh, "whoami", "--user", "--passphrase-fd", str(r)]
                res = subprocess.run(cmd, capture_output=True, text=True, pass_fds=(r,))
            finally:
                os.close(r)
            _print_cli_result(res)

def main() -> None:
    parser = argparse.ArgumentParser(description="Fake agent runner for offline test mode")
    parser.add_argument("--fake-profile", help="Fake permission profile level")
    parser.add_argument("--fake-resume", help="Session id supplied by Mesh")
    parser.add_argument("prompt_path", help="Path to prompt markdown file")
    args = parser.parse_args()
    if args.fake_resume:
        print(json.dumps({"fake_resume": args.fake_resume}), flush=True)

    prompt_content = ""
    if os.path.exists(args.prompt_path):
        with open(args.prompt_path, "r", encoding="utf-8") as f:
            prompt_content = f.read()

    spec = parse_fake_spec(prompt_content)
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
