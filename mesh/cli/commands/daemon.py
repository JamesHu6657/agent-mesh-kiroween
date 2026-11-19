"""Daemon resource commands: start, run, stop, status."""

import argparse
import os
import sys
import subprocess
import time
from typing import Any, Dict

import mesh.constants as c
from mesh.errors import MeshError
from mesh.client import MeshClient
from mesh.core.paths import get_install_root


DAEMON_START_WAIT_S = 30.0


def register_daemon_commands(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    daemon_parser = subparsers.add_parser(
        "daemon",
        parents=[parent_parser],
        help="Manage the background meshd service process",
        description="Manage the background meshd service process.",
        epilog="Examples:\n  mesh daemon start\n  mesh daemon status\n  mesh daemon stop --user\n",
    )
    sub = daemon_parser.add_subparsers(dest="verb", metavar="<verb>")

    # start
    start_parser = sub.add_parser(
        "start",
        parents=[parent_parser],
        help="Start meshd as a detached background service and wait for the socket",
        description="Start meshd as a detached background service and wait for the socket.",
        epilog="Examples:\n  mesh daemon start\n  mesh daemon start --home /tmp/mh\n",
    )
    start_parser.set_defaults(func=handle_daemon_start)

    # run
    run_parser = sub.add_parser(
        "run",
        parents=[parent_parser],
        help="Run meshd in the foreground (used by systemd or test harnesses)",
        description="Run meshd in the foreground (used by systemd or test harnesses).",
        epilog="Examples:\n  mesh daemon run\n  MESH_TESTING=1 mesh daemon run\n",
    )
    run_parser.set_defaults(func=handle_daemon_run)

    # stop
    stop_parser = sub.add_parser(
        "stop",
        parents=[parent_parser],
        help="Stop the running meshd daemon gracefully",
        description="Stop the running meshd daemon gracefully.",
        epilog="Examples:\n  mesh daemon stop --user\n  mesh daemon stop --user --passphrase-fd 3\n",
    )
    stop_parser.set_defaults(func=handle_daemon_stop)

    # status
    status_parser = sub.add_parser(
        "status",
        parents=[parent_parser],
        help="Check whether meshd is running and responsive",
        description="Check whether meshd is running and responsive.",
        epilog="Examples:\n  mesh daemon status\n  mesh daemon status --format json\n",
    )
    status_parser.set_defaults(func=handle_daemon_status)


def _meshd_lock_held(home: str) -> bool:
    """PR42 r1: True while some meshd still holds $MESH_HOME/run/meshd.lock (e.g. one
    that stopped answering but is still draining in-flight merges)."""
    import fcntl
    path = os.path.join(home, "run", "meshd.lock")
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return True
    finally:
        os.close(fd)  # closing the fd also drops a probe lock we got
    return False


def _refuse_dry_run(args: argparse.Namespace, what: str) -> None:
    """Linear full review C1: local commands that write cannot simulate; refuse rather
    than perform the change under --dry-run."""
    if getattr(args, "dry_run", False):
        import mesh.constants as _c
        from mesh.errors import MeshError as _ME
        raise _ME(_c.INVALID_ARGUMENT, exit_code=_c.EXIT_USAGE,
                  message=f"{what} does not support --dry-run (it would change local state); run it without --dry-run",
                  details={"param": "dry_run"})


def handle_daemon_start(args: argparse.Namespace) -> Dict[str, Any]:
    _refuse_dry_run(args, "mesh daemon start")
    """Start meshd as a detached background service and wait up to DAEMON_START_WAIT_S (30 s) for the socket."""
    client = MeshClient(home=args.home, timeout=1.0)
    # If --user was provided, validate / read passphrase client-side exactly once
    if getattr(args, "user", False):
        client.resolve_auth(args)

    # Check if already running using unauthenticated status
    try:
        resp = client.execute_cmd(args, "daemon.status", auth_override={"type": "none"})
        return {
            "status": "already_running",
            "message": "Daemon is already running",
            "pid": resp.get("data", {}).get("pid"),
        }
    except Exception:
        pass

    # PR42 r1: an old meshd that closed its socket but is still finishing merges keeps
    # the lock; a new one would exit at once with an unexplained rc 1.
    if _meshd_lock_held(client.home):
        raise MeshError(
            code=c.DAEMON_ALREADY_RUNNING,
            exit_code=c.EXIT_INTERNAL,
            message="A previous meshd is still stopping (finishing in-flight merges) and holds "
                    "meshd.lock; retry `mesh daemon start` when it has exited",
            details={"status": "stopping"},
        )

    install_root = get_install_root()
    from mesh.core.paths import get_mesh_bin_dir
    bin_mesh = os.path.join(get_mesh_bin_dir(), "mesh")

    env = {k: v for k, v in os.environ.items() if k not in DAEMON_SECRET_ENV}
    # Detached spawn
    proc = subprocess.Popen(
        [bin_mesh, "--home", args.home, "daemon", "run"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )

    # Wait up to 30 s for the socket: boot recovery (§11.7) runs before the socket opens and
    # can wait up to 5 s per run caught in its spawn window (review m1c-run4 §6).
    deadline = time.monotonic() + DAEMON_START_WAIT_S
    last_err = None
    started = False
    status_resp = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise MeshError(
                code=c.INTERNAL_ERROR,
                exit_code=c.EXIT_INTERNAL,
                message=f"Daemon process exited immediately with returncode {proc.returncode}",
            )
        try:
            resp = client.execute_cmd(args, "daemon.status", auth_override={"type": "none"})
            if resp.get("ok"):
                started = True
                status_resp = resp
                break
        except Exception as exc:
            last_err = exc
        time.sleep(0.1)

    if not started:
        raise MeshError(
            code=c.DAEMON_UNAVAILABLE,
            exit_code=c.EXIT_DAEMON_UNAVAILABLE,
            message=f"Timed out waiting {DAEMON_START_WAIT_S:g}s for meshd socket to become available: {last_err}",
        )

    return {
        "status": "started",
        "pid": status_resp.get("data", {}).get("pid", proc.pid),
        "boot_id": status_resp.get("data", {}).get("boot_id"),
    }


#: Full review r3 P1-6: credentials of the CLI caller. meshd never reads them (users
#: authenticate per request), but a process's initial environment stays readable in
#: /proc/<pid>/environ for its whole life, by every same-uid agent and test command.
DAEMON_SECRET_ENV = ("MESH_PASSPHRASE", "MESH_PASSPHRASE_FILE", "MESH_TOKEN")


def _exec_without_secret_env() -> None:
    """Re-exec this `daemon run` with the caller's credentials removed from the
    environment (the kernel keeps the exec-time block; only a new exec drops it)."""
    if not any(k in os.environ for k in DAEMON_SECRET_ENV):
        return
    env = {k: v for k, v in os.environ.items() if k not in DAEMON_SECRET_ENV}
    os.execve(sys.executable, [sys.executable, "-P", "-s", "-m", "mesh", *sys.argv[1:]], env)


def handle_daemon_run(args: argparse.Namespace) -> Dict[str, Any]:
    """Run meshd in the foreground."""
    _refuse_dry_run(args, "mesh daemon run")
    _exec_without_secret_env()
    # Imported here: every other CLI verb skips asyncio and the daemon (startup cost, A1).
    import asyncio

    from mesh.daemon.server import DaemonServer

    server = DaemonServer(home=args.home)
    asyncio.run(server.run_server())
    return {"status": "stopped"}


def handle_daemon_stop(args: argparse.Namespace) -> Dict[str, Any]:
    """Stop the running meshd daemon gracefully."""
    client = MeshClient(home=args.home, timeout=5.0)
    resp = client.execute_cmd(args, "daemon.stop")
    if getattr(args, "dry_run", False):
        # Full review: a dry run stops nothing; report what would happen.
        return resp.get("data", {})

    # Wait (up to 5s) for the socket to stop answering AND for meshd.lock to be free:
    # PR42 r1, meshd closes its socket first and then drains in-flight merges.
    deadline = time.monotonic() + 5.0
    answering = True
    while time.monotonic() < deadline:
        if answering:
            try:
                client.send_request({"v": 1, "cmd": "daemon.status", "auth": {"type": "none"}})
            except Exception:
                answering = False  # socket closed or gone
        if not answering and not _meshd_lock_held(client.home):
            return {"status": "stopped", "message": "Daemon stopped gracefully"}
        time.sleep(0.1)
    if not answering:
        raise MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                        message="meshd stopped accepting requests but is still finishing in-flight "
                                "merges (it holds meshd.lock and exits when they end)",
                        details={"status": "stopping"})

    # Full review: never claim "stopped" while meshd still answers.
    raise MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL,
                    message="Daemon accepted stop but is still running after 5s",
                    details={"status": "stopping"})


def handle_daemon_status(args: argparse.Namespace) -> Dict[str, Any]:
    """Check daemon status."""
    client = MeshClient(home=args.home)
    resp = client.execute_cmd(args, "daemon.status")
    return resp.get("data", {})
