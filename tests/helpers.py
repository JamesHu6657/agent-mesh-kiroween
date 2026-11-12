"""Test harness helper for Agent Mesh (plan §17.1)."""

import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import stat
import sqlite3
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from mesh.cli.main import main as mesh_cli_main
from mesh.client import MeshClient
from mesh.core.proc import get_proc_environ, get_proc_stat
from mesh.errors import MeshError
from scripts.coord import reap_daemons

_INPROC_LOCK = threading.Lock()  # redirect_stdout is process-wide

T = TypeVar("T")


def serial(obj: T) -> T:
    """Mark a test method or test class to be run serially by run_tier.py."""
    setattr(obj, "mesh_serial", True)
    return obj


def idle_bound(obj: T) -> T:
    """Mark a parallel test whose wall time is mostly a required idle wait (a fixed
    observation window, a lockout lapse, a stub child's lifetime). run_tier.py runs these
    in an extra pool beside the CPU-bound workers, so the wait does not hold a worker slot
    (A1 speed pass, m2-r7). The test itself is unchanged and still runs alongside other
    tests, as before."""
    setattr(obj, "mesh_idle_bound", True)
    return obj


class MeshHarness:
    """Manages an isolated MESH_HOME (/tmp/mh-XXXXXX, mode 0700) for testing."""

    def __init__(
        self,
        init_db: bool = False,
        start_daemon: bool = False,
        check_invariants_on_teardown: bool = True,
        extra_path: Optional[str] = None,
        use_shims: bool = True,
    ):
        self.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.bin_mesh = os.path.join(self.repo_root, "bin", "mesh")
        self.rand_id = secrets.token_hex(4)
        self.home = f"/tmp/mh-{self.rand_id}"
        # Random per-harness credential placed in the environment of every process
        # this harness launches (get_env); the teardown fallback signals a daemon it
        # did not Popen only if that daemon's environ carries it (r7d4).
        self._harness_token = secrets.token_hex(16)
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        os.chmod(self.home, 0o700)
        self.check_invariants_on_teardown = check_invariants_on_teardown
        self.extra_path = extra_path
        # use_shims=False (m3-r1, probe tier only): tests/shims stays off PATH and
        # MESH_TESTING is not set, so meshd runs the real binaries it is pointed at.
        self.use_shims = use_shims

        # Secrets the harness writes (passphrase, token files) live in a sibling
        # fixtures directory, not in $MESH_HOME, so a C7 grep over $H sees only
        # data Mesh itself wrote.
        self.fixtures_dir = f"{self.home}-fx"
        os.makedirs(self.fixtures_dir, mode=0o700, exist_ok=True)
        os.chmod(self.fixtures_dir, 0o700)

        self.passphrase = "test-passphrase-at-least-16-chars-long"
        self.passphrase_file = os.path.join(self.fixtures_dir, ".test_passphrase")
        with open(self.passphrase_file, "w", encoding="utf-8") as f:
            f.write(self.passphrase)
        os.chmod(self.passphrase_file, 0o600)

        self.daemon_proc: Optional[subprocess.Popen] = None
        self._daemon_record = None
        self._daemon_identity = None
        self.suite_tripwire_file = os.environ.get("MESH_SUITE_TRIPWIRE_FILE") or os.environ.get("MESH_TRIPWIRE_FILE")
        self.tripwire_file = f"/tmp/mesh-tripwire-{self.rand_id}.log"
        if os.path.exists(self.tripwire_file):
            os.remove(self.tripwire_file)

        if init_db:
            self.init()
        if start_daemon:
            self.start_daemon()

    def get_env(self) -> Dict[str, str]:
        """Build test environment with shims on PATH."""
        env = dict(os.environ)
        shims_dir = os.path.join(self.repo_root, "tests", "shims")
        bin_dir = os.path.join(self.repo_root, "bin")
        if not self.use_shims:
            real_shims = os.path.realpath(shims_dir)
            rest = [p for p in env.get("PATH", "").split(os.pathsep) if p and os.path.realpath(p) != real_shims]
            head = [self.extra_path] if self.extra_path else []
            env["PATH"] = os.pathsep.join(head + [bin_dir] + rest)
        elif self.extra_path:
            env["PATH"] = f"{self.extra_path}:{shims_dir}:{bin_dir}:{env.get('PATH', '')}"
        else:
            env["PATH"] = f"{shims_dir}:{bin_dir}:{env.get('PATH', '')}"
        env["MESH_HOME"] = self.home
        token = getattr(self, "_harness_token", None)
        if token:
            env[reap_daemons.HARNESS_TOKEN_VAR] = token
        else:
            env.pop(reap_daemons.HARNESS_TOKEN_VAR, None)
        if self.use_shims:
            env["MESH_TESTING"] = "1"
        else:
            env.pop("MESH_TESTING", None)
        env["MESH_TRIPWIRE_FILE"] = self.tripwire_file
        if self.suite_tripwire_file:
            env["MESH_SUITE_TRIPWIRE_FILE"] = self.suite_tripwire_file
        return env

    def cli(
        self,
        *args: str,
        token: Optional[str] = None,
        user: bool = False,
        stdin: Optional[str] = None,
        timeout: float = 10.0,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Execute bin/mesh with args and return (exit_code, envelope_dict)."""
        passphrase_fd = None
        pass_cmd: List[str] = [self.bin_mesh, "--home", self.home]
        pass_fds: Tuple[int, ...] = ()

        has_user_in_args = "--user" in args
        explicit_fd = None
        for i, a in enumerate(args):
            if a == "--passphrase-fd" and i + 1 < len(args):
                try:
                    explicit_fd = int(args[i + 1])
                except ValueError:
                    pass
            elif a.startswith("--passphrase-fd="):
                try:
                    explicit_fd = int(a.split("=", 1)[1])
                except ValueError:
                    pass

        try:
            if explicit_fd is not None:
                pass_fds = (explicit_fd,)
            elif user:
                passphrase_fd = os.open(self.passphrase_file, os.O_RDONLY)
                pass_fds = (passphrase_fd,)
                if not has_user_in_args:
                    pass_cmd.append("--user")
                pass_cmd.extend(["--passphrase-fd", str(passphrase_fd)])
            elif token:
                tok_path = os.path.join(self.fixtures_dir, f"tok-{secrets.token_hex(4)}.tok")
                with open(tok_path, "w", encoding="utf-8") as f:
                    f.write(token)
                os.chmod(tok_path, 0o600)
                pass_cmd.extend(["--token-file", tok_path])

            pass_cmd.extend(args)

            env = self.get_env()
            if extra_env:
                env.update(extra_env)

            proc = subprocess.run(
                pass_cmd,
                input=stdin,
                text=True,
                capture_output=True,
                env=env,
                timeout=timeout,
                pass_fds=pass_fds,
            )
        finally:
            if passphrase_fd is not None:
                os.close(passphrase_fd)

        raw_stdout = proc.stdout.strip()
        try:
            envelope = json.loads(raw_stdout)
        except json.JSONDecodeError:
            envelope = {
                "ok": proc.returncode == 0,
                "raw_stdout": proc.stdout,
                "raw_stderr": proc.stderr,
                "exit_code": proc.returncode,
            }

        return proc.returncode, envelope

    def setup_cli(self, *args: str, token: Optional[str] = None, user: bool = False) -> Tuple[int, Dict[str, Any]]:
        """Run a fixture-setup CLI call in this process (review m1c-run2 §2 speedup 1).

        Same argv, auth and envelope as ``cli`` but without a Python start per call.
        Use it only for setup that no ACC item checks; checked calls keep ``cli``.
        """
        argv: List[str] = ["--home", self.home]
        fd = None
        tok_path = None
        try:
            if user:
                fd = os.open(self.passphrase_file, os.O_RDONLY)
                argv.extend(["--user", "--passphrase-fd", str(fd)])
            elif token:
                tok_path = os.path.join(self.fixtures_dir, f"tok-{secrets.token_hex(4)}.tok")
                with open(tok_path, "w", encoding="utf-8") as f:
                    f.write(token)
                os.chmod(tok_path, 0o600)
                argv.extend(["--token-file", tok_path])
            argv.extend(args)
            out = io.StringIO()
            with _INPROC_LOCK, contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = mesh_cli_main(argv)
        finally:
            if fd is not None:
                os.close(fd)
        try:
            envelope = json.loads(out.getvalue().strip())
        except json.JSONDecodeError:
            envelope = {"ok": rc == 0, "raw_stdout": out.getvalue(), "exit_code": rc}
        return int(rc or 0), envelope

    def init(self) -> Tuple[int, Dict[str, Any]]:
        """Run mesh init."""
        return self.cli("init", user=True)

    def start_daemon(self, extra_env: Optional[Dict[str, str]] = None, ready_timeout: float = 5.0,
                     wait_ready: bool = True) -> None:
        """Start mesh daemon in background.

        ``extra_env`` is added to meshd's environment only (e.g. the testing hook
        ``MESH_TEST_CRASH_AT``). With ``wait_ready=False`` the call returns right
        after the spawn (a crash-hook daemon may die before it is ever asked).
        """
        if self.daemon_proc and self.daemon_proc.poll() is None:
            return
        reap_daemons.unregister_daemon(self._daemon_record)
        self._daemon_record = None
        cmd = [self.bin_mesh, "--home", self.home, "daemon", "run"]
        env = self.get_env()
        if extra_env:
            env.update(extra_env)
        self.daemon_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        # Identity (pid, starttime) of the daemon we spawned, kept after unregistering
        # so the pid-file fallback in stop_daemon can never signal a reused PID.
        try:
            self._daemon_identity = (self.daemon_proc.pid,
                                     reap_daemons.process_stat(self.daemon_proc.pid)[1])
        except (OSError, ValueError, IndexError):
            self._daemon_identity = None
        self._daemon_record = reap_daemons.register_daemon(self.daemon_proc.pid, self.home)
        # Wait for the socket in-process (review m1c-run2 §2 speedup 3): one
        # daemon.status request per poll instead of a bin/mesh subprocess.
        if not wait_ready:
            return
        client = MeshClient(self.home, timeout=2.0)
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if self.daemon_proc.poll() is not None:
                raise RuntimeError(f"meshd exited with {self.daemon_proc.returncode} before it was ready")
            try:
                resp = client.send_request({"v": 1, "cmd": "daemon.status", "auth": {"type": "none"}})
                if resp.get("ok"):
                    return
            except (MeshError, OSError, ValueError):
                pass
            time.sleep(0.05)
        raise TimeoutError(f"meshd failed to become ready within {ready_timeout} seconds")

    def stop_daemon(self) -> None:
        """Stop mesh daemon."""
        registered_proc = self.daemon_proc
        if self.daemon_proc and self.daemon_proc.poll() is None:
            try:
                self.cli("daemon", "stop", user=True, timeout=3.0)
            except Exception:
                pass
            try:
                self.daemon_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.daemon_proc.terminate()
                try:
                    self.daemon_proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.daemon_proc.kill()
                    self.daemon_proc.wait()
            self.daemon_proc = None

        if registered_proc is not None and registered_proc.poll() is not None:
            reap_daemons.unregister_daemon(self._daemon_record)
            self._daemon_record = None

        # Pid-file fallback for a daemon that outlived the steps above. Signal it
        # only if it is the exact process we spawned (pid + starttime) and runs for
        # this home, through a pidfd (bounded TERM, then KILL). Anything else: skip.
        self.stop_pidfile_daemon()

    def stop_pidfile_daemon(self) -> Optional[str]:
        identity = getattr(self, "_daemon_identity", None)
        pid_file = os.path.join(self.home, "run", "meshd.pid")
        try:
            with open(pid_file, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return None
        if pid <= 1 or pid == os.getpid():
            return "unknown_pid"
        try:
            if identity is not None and pid == identity[0]:
                return reap_daemons.stop_verified(pid, identity[1], self.home)
            # A daemon this harness did not Popen (``mesh daemon start``, detached):
            # stop it only if it runs for exactly this home and its environ carries
            # this harness's token, all through a pidfd. Never a bare kill.
            token = getattr(self, "_harness_token", None)
            if token is None:
                return "unknown_pid"
            return reap_daemons.stop_verified_token(pid, token, self.home)
        except Exception:
            return "error"

    def meshd_pid(self) -> Optional[int]:
        """meshd's pid from run/meshd.pid, only if that process belongs to this home.

        The pid is trusted only if ``/proc/<pid>/environ`` carries this home's
        ``MESH_HOME`` (plan §17.1), so a stale pid file never makes the harness
        signal an unrelated process.
        """
        pid_file = os.path.join(self.home, "run", "meshd.pid")
        try:
            with open(pid_file, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return None
        if pid <= 1 or pid == os.getpid():
            return None
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                entries = f.read().split(b"\0")
        except OSError:
            return None
        if f"MESH_HOME={self.home}".encode() not in entries:
            return None
        return pid

    def wait_daemon_exit(self, timeout: float = 20.0) -> int:
        """Wait for the meshd subprocess to exit (e.g. a crash hook); returns its returncode."""
        if self.daemon_proc is None:
            raise AssertionError("no meshd subprocess")
        rc = self.daemon_proc.wait(timeout=timeout)
        reap_daemons.unregister_daemon(self._daemon_record)
        self._daemon_record = None
        self.daemon_proc = None
        return rc

    def kill9_daemon(self) -> None:
        """`kill -9` meshd (plan §17.1) and wait until it is gone. Runs are left alone."""
        pid = self.meshd_pid()
        if pid is None and self.daemon_proc is not None and self.daemon_proc.poll() is None:
            pid = self.daemon_proc.pid
        if pid is None:
            raise AssertionError("meshd is not running for this home")
        os.kill(pid, signal.SIGKILL)
        if self.daemon_proc is not None and self.daemon_proc.pid == pid:
            self.daemon_proc.wait(timeout=10.0)
            reap_daemons.unregister_daemon(self._daemon_record)
            self._daemon_record = None
            self.daemon_proc = None
        else:
            deadline = time.monotonic() + 10.0
            while os.path.exists(f"/proc/{pid}") and time.monotonic() < deadline:
                try:
                    with open(f"/proc/{pid}/stat", "r") as f:
                        if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                            break
                except OSError:
                    break
                time.sleep(0.05)

    def restart_daemon(self, extra_env: Optional[Dict[str, str]] = None, ready_timeout: float = 30.0) -> None:
        """Start meshd again and wait for its socket (recovery runs before it opens)."""
        if self.daemon_proc is not None and self.daemon_proc.poll() is not None:
            self.daemon_proc = None
        self.start_daemon(extra_env=extra_env, ready_timeout=ready_timeout)

    def db(self) -> sqlite3.Connection:
        """Open a read-only SQLite connection to mesh.db."""
        db_path = os.path.join(self.home, "mesh.db")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 10.0, step: float = 0.1) -> bool:
        """Poll predicate until True or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(step)
        return False

    def active_waits(self) -> int:
        """Return the daemon's current active_waits counter (inbox, task and project waits)."""
        rc, resp = self.cli("daemon", "status")
        if rc != 0:
            return -1
        return int(resp.get("data", {}).get("active_waits", 0))

    def wait_for_active_waits(self, minimum: int, timeout: float = 10.0, step: float = 0.05) -> bool:
        """Wait until the daemon reports at least `minimum` active long-poll waits.

        Only inbox.wait and task.wait increment the counter (project.wait does not), so
        callers must use this only for those two verbs.
        """
        return self.wait_for(lambda: self.active_waits() >= minimum, timeout=timeout, step=step)

    def hold_launch_starts(self) -> None:
        """Stop the scheduler from starting launch-mode runs (testing hook).

        M1b tests that assert on the queued state of a launch task call this in
        their setup. The daemon honours ``$MESH_HOME/run/launch_hold`` only in
        testing mode; it is removed with the home at teardown.
        """
        run_dir = os.path.join(self.home, "run")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        hold_path = os.path.join(run_dir, "launch_hold")
        with open(hold_path, "w", encoding="utf-8") as f:
            f.write("1\n")
        os.chmod(hold_path, 0o600)

    def release_launch_starts(self) -> None:
        """Remove the launch-start hold created by ``hold_launch_starts``."""
        hold_path = os.path.join(self.home, "run", "launch_hold")
        if os.path.exists(hold_path):
            os.remove(hold_path)

    def run_ids_in_db(self) -> set:
        """Return every run id recorded in this home's database."""
        db_path = os.path.join(self.home, "mesh.db")
        if not os.path.exists(db_path):
            return set()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cur = conn.execute("SELECT id FROM runs;")
                return {row[0] for row in cur.fetchall()}
            finally:
                conn.close()
        except sqlite3.Error:
            return set()

    def kill_leftover_run_processes(self) -> List[int]:
        """Kill processes carrying the MESH_RUN_ID of a run in this home.

        Only pids whose /proc/<pid>/environ names one of this home's run ids are
        signalled; nothing is matched by name or pattern (plan §17.1, risk 4).
        """
        run_ids = self.run_ids_in_db()
        if not run_ids:
            return []
        victims: List[int] = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == os.getpid():
                continue
            try:
                env = get_proc_environ(pid)
            except OSError:
                continue
            if env and env.get("MESH_RUN_ID") in run_ids:
                victims.append(pid)
        for pid in victims:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            alive = False
            for pid in victims:
                try:
                    stat_info = get_proc_stat(pid)
                except OSError:
                    continue
                if stat_info is not None and stat_info["state"] != "Z":
                    alive = True
                    break
            if not alive:
                break
            time.sleep(0.05)
        for pid in victims:
            try:
                stat_info = get_proc_stat(pid)
            except OSError:
                continue
            if stat_info is not None and stat_info["state"] != "Z":
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        return victims

    def assert_tripwire_empty(self) -> None:
        """Assert that no real vendor CLI stub was triggered."""
        if os.path.exists(self.tripwire_file):
            with open(self.tripwire_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                if self.suite_tripwire_file and self.suite_tripwire_file != self.tripwire_file:
                    try:
                        with open(self.suite_tripwire_file, "a", encoding="utf-8") as sf:
                            sf.write(content + "\n")
                    except Exception:
                        pass
                raise AssertionError(f"Tripwire triggered! Vendor CLI stub was executed:\n{content}")

    def teardown(self) -> None:
        """Stop daemons, verify tripwire, and clean up temporary home."""
        err: Optional[Exception] = None
        try:
            self.stop_daemon()

            # Leftover run processes of runs recorded in THIS home only (plan §17.1).
            self.kill_leftover_run_processes()

            if self.check_invariants_on_teardown:
                db_path = os.path.join(self.home, "mesh.db")
                if os.path.exists(db_path):
                    rc, env_resp = self.cli("doctor", "--check-invariants")
                    if rc != 0:
                        raise AssertionError(f"Invariants check failed in teardown: {env_resp}")

            self.assert_tripwire_empty()
        except Exception as e:
            err = e
        finally:
            # Clean up tripwire file (propagating any hit to suite log if set)
            if os.path.exists(self.tripwire_file):
                try:
                    with open(self.tripwire_file, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if content and self.suite_tripwire_file and self.suite_tripwire_file != self.tripwire_file:
                        with open(self.suite_tripwire_file, "a", encoding="utf-8") as sf:
                            sf.write(content + "\n")
                except Exception:
                    pass
                try:
                    os.remove(self.tripwire_file)
                except Exception:
                    pass

            # Clean up temporary home
            if os.path.exists(self.home) and self.home.startswith("/tmp/mh-"):
                # Round 6: iterative, so a very deep tree left by a test (1500 levels in
                # test_doctor_fix_perms_r5) cannot raise RecursionError on Python 3.11.
                rmtree_deep(self.home)

            # Clean up the sibling fixtures directory (passphrase/token files)
            if os.path.exists(self.fixtures_dir) and self.fixtures_dir.startswith("/tmp/mh-"):
                rmtree_deep(self.fixtures_dir)

            if err is not None:
                raise err


def assert_h5(tc: Any, harness: "MeshHarness") -> None:
    """ACC: H5 - never two active runs per task; every task lease's fence equals the
    task's fence_seq and its run's fence, and that run is active."""
    conn = harness.db()
    try:
        dup = conn.execute("SELECT task_id, COUNT(*) AS n FROM runs WHERE status IN ('starting', 'running') "
                           "AND task_id IS NOT NULL GROUP BY task_id HAVING n > 1;").fetchall()
        tc.assertEqual([dict(r) for r in dup], [])
        for lease in conn.execute("SELECT * FROM leases WHERE key LIKE 'task:%';").fetchall():
            task = conn.execute("SELECT fence_seq FROM tasks WHERE id = ?;", (lease["key"][5:],)).fetchone()
            run = conn.execute("SELECT fence, status FROM runs WHERE id = ?;", (lease["run_id"],)).fetchone()
            tc.assertEqual(lease["fence"], task["fence_seq"])
            tc.assertEqual(run["fence"], task["fence_seq"])
            tc.assertIn(run["status"], ("starting", "running"))
    finally:
        conn.close()


def rmtree_deep(path: str) -> None:
    """Remove a directory tree of any depth, best effort (m3-test-kdf round 6).

    shutil.rmtree recurses once per level in Python 3.11 (fd-based _rmtree_safe_fd),
    so a 1500-level tree raises RecursionError, which ignore_errors does not catch.
    This walks iteratively with one directory fd held at a time (descend by fd, climb
    with openat(".."), so neither recursion depth, PATH_MAX nor RLIMIT_NOFILE limit
    it), makes each directory 0700 first (a 000 directory can be emptied) and never
    follows symlinks. Errors are ignored, like rmtree(ignore_errors=True).
    """
    try:
        st = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode):
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    try:
        cur = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    names: List[str] = []  # name of each directory below `path` on the way down
    try:
        while True:
            descended = False
            try:
                entries = os.listdir(cur)
            except OSError:
                entries = []
            for name in entries:
                try:
                    est = os.stat(name, dir_fd=cur, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(est.st_mode):
                    try:
                        os.chmod(name, 0o700, dir_fd=cur)
                        nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
                    except OSError:
                        continue
                    os.close(cur)
                    cur = nxt
                    names.append(name)
                    descended = True
                    break
                try:
                    os.unlink(name, dir_fd=cur)
                except OSError:
                    pass
            if descended:
                continue
            if not names:
                break
            up = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
            os.close(cur)
            cur = up
            name = names.pop()
            try:
                os.rmdir(name, dir_fd=cur)
            except OSError:
                # Not emptied (an entry could not be removed): stop, so the loop
                # cannot re-enter the same directory forever.
                break
    finally:
        os.close(cur)
    try:
        os.rmdir(path)
    except OSError:
        pass
