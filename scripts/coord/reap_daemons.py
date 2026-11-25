#!/usr/bin/env python3
"""Best-effort cleanup of registered test daemons, never a global process scan.

Harnesses register process identities atomically. Suite flock ownership (or an
adhoc test process's PID/starttime) protects live suites, regardless of age.
"""
import argparse
import errno
import json
import ntpath
import os
import re
import signal
import stat
import sys
import tempfile
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

if sys.platform != "win32":
    import fcntl
    import select
else:
    fcntl = None  # type: ignore[assignment]
    select = None  # type: ignore[assignment]

from mesh.platform import fs as plat_fs
from mesh.platform import locks as plat_locks
from mesh.platform import paths as plat_paths
from mesh.platform import perms as plat_perms
from mesh.platform import proc as plat_proc

DEFAULT_REGISTRY = (
    "/tmp/mesh-test-daemons"
    if sys.platform != "win32"
    else os.path.join(plat_paths.temp_root(), "mesh-test-daemons")
)
# Unlocked suite lock files older than this are leftovers of finished suites.
STALE_LOCK_SECONDS = 3600


def log(message):
    print(f"reap-daemons: {message}", file=sys.stderr, flush=True)


def registry_dir():
    default = (
        DEFAULT_REGISTRY
        if sys.platform != "win32"
        else os.path.join(plat_paths.temp_root(), "mesh-test-daemons")
    )
    return os.environ.get("MESH_TEST_DAEMON_REGISTRY", default)


def prepare_registry(directory):
    if sys.platform == "win32":
        if plat_paths.is_reparse_link(directory):
            raise ValueError("registry must be a non-symlink directory")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            plat_perms.verify_owner_and_set_private_dir_acl_win(directory, 0o700)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError("registry must be a non-symlink directory") from exc
            raise
        except ValueError as exc:
            if "not owned" in str(exc):
                raise ValueError("registry is not owned by this user") from exc
            raise
        return
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        raise ValueError("registry must be a non-symlink directory")
    with_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(with_fd).st_uid != os.getuid():
            raise ValueError("registry is not owned by this user")
        os.fchmod(with_fd, 0o700)
    finally:
        os.close(with_fd)


def parse_stat(data):
    """Return state and field 22, allowing spaces and ')' in comm (field 2)."""
    fields = data.rsplit(")", 1)[1].split()
    return fields[0], int(fields[19])


def _tgid_has_live_sibling(pid, raw_stat):
    fields = raw_stat.rsplit(")", 1)[1].split()
    if len(fields) > 17 and int(fields[17]) > 1:
        return True
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except (FileNotFoundError, ProcessLookupError):
        return False
    for tid in tids:
        if not tid.isdigit() or int(tid) == int(pid):
            continue
        try:
            with open(f"/proc/{pid}/task/{tid}/stat", encoding="utf-8") as stream:
                tstate, _ = parse_stat(stream.read())
            if tstate not in ("Z", "X"):
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
    return False


def process_stat(pid):
    if sys.platform == "win32":
        info = plat_proc.get_proc_stat(pid)
        if info is None:
            raise FileNotFoundError(errno.ESRCH, f"No such process: {pid}")
        return str(info.get("state") or "R"), int(info["starttime"])
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
        raw = stream.read()
    state, starttime = parse_stat(raw)
    if state in ("Z", "X") and _tgid_has_live_sibling(pid, raw):
        return "S", starttime
    return state, starttime


def valid_home(home):
    if sys.platform == "win32":
        if not isinstance(home, str) or not home or "\x00" in home:
            return False
        if "/" in home or plat_paths.has_traversal_segment(home):
            return False
        try:
            real = os.path.realpath(home)
        except OSError:
            return False
        tmp_root = plat_paths.temp_root()
        base = ntpath.basename(real)
        return (
            plat_paths.norm_cmp(home) == plat_paths.norm_cmp(real)
            and plat_paths.norm_cmp(ntpath.dirname(real)) == plat_paths.norm_cmp(tmp_root)
            and base.startswith("mh-")
            and len(base) > 3
            and not plat_paths.is_reparse_link(home)
        )
    return (isinstance(home, str) and home == os.path.realpath(home)
            and os.path.dirname(home) == "/tmp"
            and os.path.basename(home).startswith("mh-")
            and len(os.path.basename(home)) > 3 and not os.path.islink(home))


def validate_record(record):
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    for key in ("pid", "starttime"):
        if type(record.get(key)) is not int or record[key] <= (1 if key == "pid" else 0):
            raise ValueError(f"invalid {key}")
    if not valid_home(record.get("home")):
        raise ValueError("home must be a canonical, non-symlink /tmp/mh-* child")
    suite = record.get("suite_id")
    if not isinstance(suite, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", suite):
        raise ValueError("invalid suite_id")
    if suite.startswith("adhoc-"):
        if not re.fullmatch(r"adhoc-[1-9][0-9]*", suite):
            raise ValueError("invalid adhoc owner")
        if type(record.get("suite_starttime")) is not int or record["suite_starttime"] <= 0:
            raise ValueError("invalid adhoc owner starttime")
    return record


def record_name(record):
    """Generation file name: a reused PID never shares (or replaces) a record file."""
    return f"{record['pid']}-{record['starttime']}.json"


def register_daemon(pid, home):
    """Return a removal handle; registration failures must not fail tests."""
    temporary = None
    try:
        if not valid_home(home) or not os.path.isdir(home):
            raise ValueError("refusing non-canonical or non-/tmp/mh-* home")
        record = {"pid": pid, "starttime": process_stat(pid)[1],
                  "home": os.path.realpath(home),
                  "suite_id": os.environ.get("MESH_TEST_SUITE_ID") or f"adhoc-{os.getpid()}"}
        if record["suite_id"].startswith("adhoc-"):
            record["suite_starttime"] = process_stat(os.getpid())[1]
        validate_record(record)
        directory = registry_dir()
        prepare_registry(directory)
        fd, temporary = tempfile.mkstemp(prefix=".daemon-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        path = os.path.join(directory, record_name(record))
        if sys.platform == "win32":
            plat_perms.set_private_file_acl(temporary, 0o600)
            plat_fs.atomic_replace_win(temporary, path, replace_fn=os.replace)
        else:
            os.replace(temporary, path)
        return path, record
    except Exception as exc:
        log(f"registration_failed pid={pid}: {exc}")
        return None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log(f"temporary_cleanup_failed: {exc}")


def unregister_daemon(handle):
    if handle is None:
        return
    path, record = handle
    try:
        remove_record(path, record)
    except Exception as exc:
        log(f"unregistration_failed pid={record['pid']}: {exc}")


def adhoc_alive(record, owner_stat):
    return (owner_stat is not None and owner_stat[0] not in ("Z", "X")
            and owner_stat[1] == record["suite_starttime"])


def suite_alive(record, directory):
    suite = record["suite_id"]
    if suite.startswith("adhoc-"):
        try:
            return adhoc_alive(record, process_stat(int(suite[6:])))
        except (FileNotFoundError, ProcessLookupError):
            return False
        except OSError:
            return True
    if sys.platform == "win32":
        path = os.path.join(directory, f"suite-{suite}.lock")
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        if plat_paths.is_reparse_stat(st) or not stat.S_ISREG(st.st_mode):
            raise ValueError("suite lock must be a non-symlink regular file")
        try:
            fd = plat_fs.open_regular_nofollow(path, os.O_RDWR, label="suite lock")
        except FileNotFoundError:
            return False
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EINVAL):
                raise ValueError("suite lock must be a non-symlink regular file") from exc
            raise
        acquired = False
        try:
            try:
                plat_locks.acquire_lock(fd, exclusive=True, blocking=False)
                acquired = True
            except BlockingIOError:
                return True
            return False
        finally:
            if acquired:
                try:
                    plat_locks.release_lock(fd)
                except OSError:
                    pass
            os.close(fd)
    try:
        path = os.path.join(directory, f"suite-{suite}.lock")
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise ValueError("suite lock must be a non-symlink regular file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def decision(record, current_stat, home_exists, owner_alive):
    """Pure policy: no age cutoff; live owners override missing homes."""
    if current_stat is None:
        return "gone"
    if current_stat[1] != record["starttime"]:
        return "pid_reused"
    if current_stat[0] in ("Z", "X"):
        return "gone"
    if owner_alive:
        return "skipped_live"
    return "dead_suite" if home_exists else "home_gone"


def _open_pidfd_win(pid, *, k32=None, get_last_error_fn=None):
    """Open a handle-pinned Win32 process handle and read CreationTime after pinning."""
    from mesh.platform import _win32, _win32_ctypes as w32

    if int(pid) <= 0:
        return None, None
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    access = w32.SYNCHRONIZE | w32.PROCESS_TERMINATE | w32.PROCESS_QUERY_LIMITED_INFORMATION
    handle = dll_k32.OpenProcess(access, False, int(pid))
    if w32.is_null_or_invalid_handle(handle):
        err = err_fn()
        if err == w32.ERROR_INVALID_PARAMETER:
            return None, None
        raise _win32._win_proc_error(err)
    try:
        if not _win32._is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            dll_k32.CloseHandle(handle)
            return None, None
        ct = _win32._get_handle_creation_time_win(handle, k32=dll_k32, err_fn=err_fn)
        if not _win32._is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            dll_k32.CloseHandle(handle)
            return None, None
        return int(handle), ("R", ct)
    except BaseException:
        dll_k32.CloseHandle(handle)
        raise


def _wait_pidfd_win(handle, seconds, *, k32=None, get_last_error_fn=None):
    from mesh.platform import _win32, _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    timeout_ms = min(0xFFFFFFFE, max(0, int(seconds * 1000)))
    rc = int(dll_k32.WaitForSingleObject(handle, timeout_ms))
    if rc == w32.WAIT_OBJECT_0:
        return True
    if rc == w32.WAIT_TIMEOUT:
        return False
    if rc == w32.WAIT_FAILED:
        raise _win32._win_proc_error(err_fn())
    raise OSError(errno.EIO, f"WaitForSingleObject returned unexpected status {rc:#x}")


def _stop_pidfd_win(handle, term_timeout, kill_timeout, *, k32=None, get_last_error_fn=None):
    from mesh.platform import _win32, _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    if _wait_pidfd_win(handle, 0, k32=dll_k32, get_last_error_fn=err_fn):
        return "gone"
    if not dll_k32.TerminateProcess(handle, 15):
        err = err_fn()
        if _wait_pidfd_win(handle, 0, k32=dll_k32, get_last_error_fn=err_fn):
            return "gone"
        raise _win32._win_proc_error(err)
    if _wait_pidfd_win(handle, term_timeout, k32=dll_k32, get_last_error_fn=err_fn):
        return "terminated"
    if not dll_k32.TerminateProcess(handle, 137):
        err = err_fn()
        if _wait_pidfd_win(handle, 0, k32=dll_k32, get_last_error_fn=err_fn):
            return "killed"
        raise _win32._win_proc_error(err)
    return "killed" if _wait_pidfd_win(handle, kill_timeout, k32=dll_k32, get_last_error_fn=err_fn) else "still_running"


def _close_pidfd_win(handle, *, k32=None):
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_k32.CloseHandle(handle)


def _close_pidfd(fd):
    if sys.platform == "win32":
        _close_pidfd_win(fd)
        return
    os.close(fd)


def wait_pidfd(fd, seconds):
    if sys.platform == "win32":
        return _wait_pidfd_win(fd, seconds)
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(max(0, int(seconds * 1000))))


def stop_pidfd(fd, term_timeout, kill_timeout):
    """All signals and exit checks stay bound to the originally opened process."""
    if sys.platform == "win32":
        return _stop_pidfd_win(fd, term_timeout, kill_timeout)
    try:
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if wait_pidfd(fd, term_timeout):
            return "terminated"
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        return "killed" if wait_pidfd(fd, kill_timeout) else "still_running"
    except ProcessLookupError:
        return "gone"


def open_pidfd(pid):
    """Return (fd, stat) with stat read after the fd is pinned; (None, None) if gone.

    EINVAL means the number now names a non-leader thread: not our daemon, so it
    is reported as a reused identity (stat ("?", 0) never matches a starttime).
    """
    if sys.platform == "win32":
        return _open_pidfd_win(pid)
    fd = None
    try:
        fd = os.pidfd_open(pid)
        return fd, process_stat(pid)
    except (FileNotFoundError, ProcessLookupError):
        pass
    except OSError as exc:
        if exc.errno != errno.EINVAL:
            if fd is not None:
                os.close(fd)
            raise
        if fd is not None:
            os.close(fd)
        return None, ("?", 0)
    if fd is not None:
        os.close(fd)
    return None, None


def _canonical(raw):
    """Canonical form of an absolute home path from argv/environ bytes, else None.

    A relative value is never resolved: realpath() would use *our* cwd, not the
    target's, so the same relative string could name a different home there
    (Linear r7d4 MAJOR). Harness homes are always absolute, so relative = no match.
    """
    if sys.platform == "win32":
        try:
            decoded = os.fsdecode(raw)
        except ValueError:
            return None
        drive, rest = ntpath.splitdrive(decoded)
        if not drive or not (
            rest.startswith("\\")
            or rest.startswith("/")
            or drive.startswith("\\\\")
            or drive.startswith("//")
        ):
            return None
        try:
            return os.fsencode(plat_paths.norm_cmp_win(os.path.realpath(decoded)))
        except (ValueError, OSError):
            return None
    if not raw.startswith(b"/"):
        return None
    try:
        return os.fsencode(os.path.realpath(os.fsdecode(raw)))
    except (ValueError, OSError):
        return None


_HOME_ABBREVS = (b"--ho", b"--hom")  # argparse allow_abbrev: unique prefixes of --home

# Value-less interpreter flags allowed before ``-m mesh``. bin/mesh execs
# ``python3 -P -s -m mesh ...``; both MeshHarness.start_daemon and ``mesh daemon
# start`` launch the daemon through bin/mesh (argv checked on base, r7d6);
# -u/-E/-I/-B take no value and do not change which module or args mesh sees.
# Anything else (-X/-W/-c/-O/combined/unknown) makes the argv boundary uncertain.
_INTERPRETER_FLAGS = frozenset((b"-P", b"-s", b"-u", b"-E", b"-I", b"-B"))
_INTERPRETER_NAME = re.compile(rb"python(3(\.[0-9]+)?)?\Z")
_WIN_INTERPRETER_NAME = re.compile(rb"(?i:python(3(\.[0-9]+)?)?(?:\.exe)?)\Z")


def mesh_args(argv):
    """The argv elements mesh itself parses, or None unless argv is exactly
    ``<python> [allowed flags] -m mesh <args>`` (Linear r7d6 MAJOR: interpreter
    option values such as ``-X --home=A`` must never be read as mesh options)."""
    if argv and argv[-1] == b"":
        argv = argv[:-1]  # /proc cmdline ends with a NUL
    if sys.platform == "win32":
        if not argv or not _WIN_INTERPRETER_NAME.match(ntpath.basename(argv[0])):
            return None
    elif not argv or not _INTERPRETER_NAME.match(os.path.basename(argv[0])):
        return None
    index = 1
    while index < len(argv) and argv[index] in _INTERPRETER_FLAGS:
        index += 1
    if argv[index:index + 2] != [b"-m", b"mesh"]:
        return None
    return argv[index + 2:]


def effective_home(argv, environ):
    """The home a ``mesh daemon run`` process actually serves, canonical bytes, or
    None if unsure.

    argv must be the launcher shape ``<python> [-P -s ...] -m mesh ...`` (see
    mesh_args) and, apart from ``--home`` options, its mesh args must be exactly
    ``daemon run``: only daemons are ever stopped. Within the mesh args this
    mirrors the mesh CLI (mesh/cli/main.py): an explicit ``--home`` overrides
    MESH_HOME entirely; only without one does MESH_HOME apply, and an empty
    MESH_HOME means the default ~/.agent-mesh (never a harness home). Linear r7d5
    MAJOR: matching ``--home`` OR MESH_HOME let a daemon started with ``--home B``
    under an inherited MESH_HOME=A count as A's.

    Conservative where the CLI's choice is not certain: argparse abbreviations
    (``--ho``/``--hom``), several ``--home`` values that disagree (including a
    root-level ``--home`` vs a sub-level ``daemon run --home``), a dangling
    ``--home``, a relative value, or MESH_HOME set more than once to different
    values all yield None (no match).
    """
    args = mesh_args(argv)
    if args is None:
        return None
    values, rest = [], []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == b"--home":
            if index + 1 >= len(args):
                return None
            values.append(args[index + 1])
            index += 2
            continue
        if arg.startswith(b"--home="):
            values.append(arg[7:])
        elif arg in _HOME_ABBREVS or arg.startswith(tuple(a + b"=" for a in _HOME_ABBREVS)):
            return None
        else:
            rest.append(arg)
        index += 1
    if rest != [b"daemon", b"run"]:
        return None
    if values:
        canon = {_canonical(value) for value in values}
        return canon.pop() if len(canon) == 1 else None
    if sys.platform == "win32":
        env_values = set()
        for entry in environ:
            key, sep, val = entry.partition(b"=")
            if sep and key.upper() == b"MESH_HOME":
                env_values.add(val)
    else:
        env_values = {entry[10:] for entry in environ if entry.startswith(b"MESH_HOME=")}
    if len(env_values) != 1:
        return None
    return _canonical(env_values.pop())


def _read_proc_file(pid, filename):
    leader_err = None
    try:
        with open(f"/proc/{pid}/{filename}", "rb") as stream:
            data = stream.read()
        if data:
            return data
    except OSError as exc:
        leader_err = exc

    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
            raw = stream.read()
        state, _ = parse_stat(raw)
        live_leader_zombie = state in ("Z", "X") and _tgid_has_live_sibling(pid, raw)
    except OSError:
        live_leader_zombie = False

    if not live_leader_zombie:
        if leader_err is not None:
            raise leader_err
        return b""

    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        tids = None
    if tids is not None:
        for tid in tids:
            if not tid.isdigit() or int(tid) == int(pid):
                continue
            try:
                with open(f"/proc/{pid}/task/{tid}/{filename}", "rb") as stream:
                    data = stream.read()
                if data:
                    return data
            except OSError:
                continue
    else:
        try:
            with open("/proc/sys/kernel/pid_max", encoding="ascii") as pf:
                pmax = max(int(pf.read().strip()), 2)
        except (OSError, ValueError):
            pmax = 65536
        needle = f"\nTgid:\t{pid}\n"
        for off in range(1, pmax):
            cand = (pid + off - 1) % pmax + 1
            if cand <= 1 or cand == pid:
                continue
            spath = f"/proc/{cand}/status"
            if not os.path.exists(spath):
                continue
            try:
                with open(spath, encoding="utf-8", errors="replace") as sf:
                    if needle not in ("\n" + sf.read()):
                        continue
                with open(f"/proc/{cand}/{filename}", "rb") as stream:
                    data = stream.read()
                if data:
                    return data
            except OSError:
                continue
    if leader_err is not None:
        raise leader_err
    return b""


def home_matches(pid, home):
    """True if pid is a ``mesh daemon run`` whose effective home is exactly home.

    Whole values are compared after canonicalisation, so /tmp/mh-abc-other never
    matches /tmp/mh-abc. Relative or ambiguous values never match.
    """
    if sys.platform == "win32":
        try:
            argv_strs = plat_proc.get_proc_cmdline(pid)
            env_dict = plat_proc.get_proc_environ(pid)
            if argv_strs is None or env_dict is None:
                return False
            argv = [os.fsencode(a) for a in argv_strs]
            environ = [os.fsencode(f"{k}={v}") for k, v in env_dict.items()]
            want = os.fsencode(plat_paths.norm_cmp(os.path.realpath(home)))
            got = effective_home(argv, environ)
            return got is not None and got == want
        except OSError:
            return False
    try:
        argv = _read_proc_file(pid, "cmdline").split(b"\0")
        environ = _read_proc_file(pid, "environ").split(b"\0")
        want = os.fsencode(os.path.realpath(home))
        got = effective_home(argv, environ)
        return got is not None and got == want
    except OSError:
        return False


HARNESS_TOKEN_VAR = "MESH_TEST_HARNESS_TOKEN"


def valid_token(token):
    return (type(token) is str and len(token) == 32
            and all(c in "0123456789abcdef" for c in token))


def token_matches(pid, token):
    """True if pid's environ holds exactly ``MESH_TEST_HARNESS_TOKEN=<token>``."""
    if sys.platform == "win32":
        try:
            env_dict = plat_proc.get_proc_environ(pid)
            if env_dict is None:
                return False
            for k, v in env_dict.items():
                if k.upper() == HARNESS_TOKEN_VAR and v == token:
                    return True
            return False
        except OSError:
            return False
    want = f"{HARNESS_TOKEN_VAR}={token}".encode()
    try:
        return want in _read_proc_file(pid, "environ").split(b"\0")
    except OSError:
        return False


def stop_verified(pid, starttime, home, term_timeout=5.0, kill_timeout=5.0):
    """Stop pid only if it is still the process (pid, starttime) running for home.

    Shared by the harness teardown fallback: the fd is opened first, the identity is
    checked against it, and every signal goes through that same fd.
    """
    fd, current = open_pidfd(pid)
    try:
        if current is None:
            return "gone"
        if current[1] != starttime:
            return "pid_reused"
        if current[0] in ("Z", "X"):
            return "gone"
        if not home_matches(pid, home):
            return "home_mismatch"
        return stop_pidfd(fd, term_timeout, kill_timeout)
    finally:
        if fd is not None:
            _close_pidfd(fd)


def stop_verified_token(pid, token, home, term_timeout=5.0, kill_timeout=5.0):
    """Stop a daemon the harness did not Popen itself (e.g. ``mesh daemon start``).

    Signals only if, checked after a pidfd is pinned: the process is not a zombie,
    is a ``python3 [-P -s] -m mesh ... daemon run`` serving exactly ``home``
    (see effective_home), and
    its environ carries this harness's random 128-bit token
    (MESH_TEST_HARNESS_TOKEN), which the harness puts only in the environment of
    processes it launches. A process from before the harness, or any process the
    harness did not launch (directly or through a descendant that inherited its
    environment), cannot carry the token. The /proc reads happen after the fd is
    pinned; if the pid is reused between pidfd_open and the reads, the fd still
    names the old process, so a signal can only reach it (or fail with ESRCH).
    There is no start-time check: on gVisor /proc/uptime and stat starttime drift
    by a tick, and the token is the stronger credential.
    """
    if not valid_home(home) or not valid_token(token):
        return "unknown_pid"
    fd, current = open_pidfd(pid)
    try:
        if current is None:
            return "gone"
        if current[0] in ("Z", "X"):
            return "gone"
        if current[0] == "?":
            return "pid_reused"
        if not home_matches(pid, home):
            return "home_mismatch"
        if not token_matches(pid, token):
            return "token_mismatch"
        return stop_pidfd(fd, term_timeout, kill_timeout)
    finally:
        if fd is not None:
            _close_pidfd(fd)


def remove_record(path, record):
    """Unlink path only if it still holds exactly the record that was validated."""
    try:
        with open(path, encoding="utf-8") as stream:
            if json.load(stream) != record:
                return
        os.unlink(path)
    except FileNotFoundError:
        pass


def reap_record(path, record, directory, term_timeout, kill_timeout, dry_run):
    fd = None
    try:
        # Open first, then verify stat: a subsequent PID reuse cannot retarget fd.
        fd, current_stat = open_pidfd(record["pid"])
        why = decision(record, current_stat, os.path.isdir(record["home"]),
                       suite_alive(record, directory))
        outcome = why
        if why in ("home_gone", "dead_suite"):
            outcome = "would_reap" if dry_run else stop_pidfd(fd, term_timeout, kill_timeout)
        log(f"{outcome} pid={record['pid']} home={record['home']} reason={why}")
        if not dry_run and outcome in ("terminated", "killed", "gone", "pid_reused"):
            remove_record(path, record)
    finally:
        if fd is not None:
            _close_pidfd(fd)


def prune_stale_locks(directory, max_age=None):
    """Remove suite locks nobody holds and that are old; never a held or fresh one.

    The age gate covers the window between test.sh creating its lock file and
    flocking it. The unlink happens while we hold the lock, and only if the path
    still names the inode we locked.
    """
    max_age = STALE_LOCK_SECONDS if max_age is None else max_age
    for name in sorted(os.listdir(directory)):
        if not (name.startswith("suite-") and name.endswith(".lock")):
            continue
        path = os.path.join(directory, name)
        if sys.platform == "win32":
            try:
                info = os.lstat(path)
                if (
                    plat_paths.is_reparse_stat(info)
                    or not stat.S_ISREG(info.st_mode)
                    or time.time() - info.st_mtime < max_age
                ):
                    continue
                fd = plat_fs.open_regular_nofollow(path, os.O_RDWR, label="suite lock")
            except OSError:
                continue
            acquired = False
            try:
                try:
                    plat_locks.acquire_lock(fd, exclusive=True, blocking=False)
                    acquired = True
                except BlockingIOError:
                    continue
                held = os.fstat(fd)
                current = os.lstat(path)
                if (
                    not plat_paths.is_reparse_stat(current)
                    and (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)
                ):
                    os.unlink(path)
                    log(f"pruned_lock {name}")
            except OSError as exc:
                log(f"lock_prune_failed {name}: {exc}")
            finally:
                if acquired:
                    try:
                        plat_locks.release_lock(fd)
                    except OSError:
                        pass
                os.close(fd)
            continue
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or time.time() - info.st_mtime < max_age:
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            held = os.fstat(fd)
            current = os.lstat(path)
            if (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino):
                os.unlink(path)
                log(f"pruned_lock {name}")
        except OSError as exc:
            log(f"lock_prune_failed {name}: {exc}")
        finally:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=registry_dir())
    parser.add_argument("--prepare-registry", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--term-timeout", type=float, default=5.0)
    parser.add_argument("--kill-timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    try:
        if args.prepare_registry:
            prepare_registry(args.registry)
            return 0
        # Reject symlink/foreign registries before reading any records.
        prepare_registry(args.registry)
        paths = sorted(name for name in os.listdir(args.registry) if name.endswith(".json"))
        for name in paths:
            path = os.path.join(args.registry, name)
            try:
                st = os.lstat(path)
                if plat_paths.is_reparse_stat(st) or not stat.S_ISREG(st.st_mode):
                    raise ValueError("record must be a non-symlink regular file")
                if sys.platform == "win32":
                    fd = plat_fs.open_regular_nofollow(path, os.O_RDONLY, label="record file")
                else:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, encoding="utf-8") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("record is not a regular file")
                    record = validate_record(json.load(stream))
                if name != record_name(record):
                    raise ValueError("record filename does not match pid-starttime")
            except Exception as exc:
                log(f"malformed record={name}: {exc}")
                continue
            try:
                reap_record(path, record, args.registry, args.term_timeout,
                            args.kill_timeout, args.dry_run)
            except Exception as exc:
                log(f"error pid={record['pid']}: {exc}")
        log(f"checked records={len(paths)}")
        if not args.dry_run:
            prune_stale_locks(args.registry)
    except Exception as exc:
        log(f"registry_error: {exc}")
        return 1 if args.prepare_registry else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
