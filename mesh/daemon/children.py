"""meshd child registry, spawn-window counter and orphan reaper (plan §11.4).

Every child meshd spawns (run wrappers, git, test commands, probes, list_models
and session-list calls, the testing stub child) is recorded here as
``{pid: (kind, starttime)}``. Termination uses the registry to never signal a
registered child that belongs to another purpose; the reaper uses it to never
reap a child whose exit status belongs to its owner.

Reaping rules (§11.4): meshd is a subreaper, so orphans of runs reparent to it
and become zombies when they die. The reaper waits only for zombies with
``PPid == getpid()`` that are *not* registered, one pid at a time with
``WNOHANG``. It never calls ``waitpid(-1)`` or ``os.wait()`` (that would steal
exit statuses from asyncio's child watcher, which then reports 255). While a
spawn is in flight (``spawns_in_flight > 0``) the child may exist before its
pid is registered, so the whole scan is skipped.
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from mesh.core.proc import get_proc_stat, is_tgid_alive
from mesh.platform import proc as _plat_proc

_lock = threading.Lock()
_registry: Dict[int, Tuple[str, Optional[int]]] = {}
_spawns_in_flight = 0

# Test instrumentation (read by unit tests; never changes behaviour).
stats: Dict[str, int] = {"scans": 0, "scans_skipped_in_flight": 0, "scans_aborted_in_flight": 0, "reaped": 0}


def register(pid: int, kind: str) -> None:
    """Record a meshd child with its /proc starttime."""
    info = get_proc_stat(pid)
    with _lock:
        _registry[pid] = (kind, info["starttime"] if info else None)


def unregister(pid: Optional[int]) -> None:
    if not pid:
        return
    with _lock:
        _registry.pop(int(pid), None)


def unregister_if(pid: Optional[int], kind: str) -> None:
    """Unregister pid only if it is still registered with ``kind``."""
    if not pid:
        return
    with _lock:
        entry = _registry.get(int(pid))
        if entry is not None and entry[0] == kind:
            _registry.pop(int(pid), None)


def registered() -> Dict[int, Tuple[str, Optional[int]]]:
    with _lock:
        return dict(_registry)


def registered_pids() -> Set[int]:
    with _lock:
        return set(_registry)


def spawns_in_flight() -> int:
    with _lock:
        return _spawns_in_flight


@contextlib.contextmanager
def spawn_window() -> Iterator[None]:
    """Count a spawn as in flight until the caller has registered the pid (or failed)."""
    global _spawns_in_flight
    with _lock:
        _spawns_in_flight += 1
    try:
        yield
    finally:
        with _lock:
            _spawns_in_flight -= 1


def popen_registered(kind: str, argv: List[str], **kwargs: Any) -> "subprocess.Popen[Any]":
    """``subprocess.Popen`` with registration inside the spawn window.

    On Windows, supplying ``job=`` assigns the child to that Job Object and
    grants timeout/failure kill-scope over it.
    """
    job = kwargs.pop("job", None)
    if sys.platform != "win32":
        with spawn_window():
            proc = subprocess.Popen(argv, **kwargs)
            try:
                register(proc.pid, kind)
            except BaseException as exc:
                # Round 7 (Linear P2): the process exists; the caller must not treat this
                # as "nothing was spawned". The Popen handle travels with the error.
                # Round 10: held from the orphan reaper inside the spawn window (no race).
                hold(proc.pid)
                raise SpawnedThenFailed(proc, exc) from exc
        return proc

    own_group = bool(kwargs.get("start_new_session", False))
    created_job = False
    if job is None and own_group:
        kill_on_close = kind not in ("wrapper", "merge_test")
        job = _plat_proc.create_job(kill_on_close=kill_on_close)
        created_job = True
    with spawn_window():
        try:
            if job is not None:
                proc = _plat_proc.spawn_in_job(
                    argv, job, popen_fn=subprocess.Popen, **kwargs
                )
            else:
                kwargs.pop("start_new_session", None)
                proc = subprocess.Popen(argv, **kwargs)
        except BaseException:
            if created_job and job is not None:
                try:
                    job.close()
                except OSError:
                    pass
            raise
        if job is not None:
            proc._mesh_job_owned = created_job  # type: ignore[attr-defined]
        try:
            register(proc.pid, kind)
        except BaseException as exc:
            hold(proc.pid)
            raise SpawnedThenFailed(proc, exc) from exc
    return proc


class SpawnedThenFailed(Exception):
    """Popen succeeded, then a later step of the spawn failed: ``proc`` is the live
    (unreaped) child that the caller must terminate before it closes anything."""

    def __init__(self, proc: "subprocess.Popen[Any]", cause: BaseException) -> None:
        super().__init__(f"after Popen (pid {proc.pid}): {type(cause).__name__}: {cause}")
        self.proc = proc


#: After a timeout kill, how long `run_registered` still reads the child's output.
POST_KILL_DRAIN_S = 5.0


def _close_owned_proc_job(proc: Any) -> None:
    """Close an internally-created Job handle attached to *proc*, if any."""
    if not getattr(proc, "_mesh_job_owned", False):
        return
    job = getattr(proc, "_mesh_job", None)
    if job is not None:
        try:
            job.close()
        except OSError:
            pass


def _kill_after_timeout(proc: "subprocess.Popen[Any]", own_group: bool) -> None:
    """SIGKILL / TerminateJobObject the timed-out child and its descendants."""
    if sys.platform == "win32":
        job = getattr(proc, "_mesh_job", None)
        if job is not None:
            try:
                _plat_proc.terminate_job(job, 137)
            except OSError:
                pass
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        return
    import signal as _signal
    if own_group:
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_registered(kind: str, argv: List[str], timeout: Optional[float] = None, **kwargs: Any) -> "subprocess.CompletedProcess[Any]":
    """Blocking ``subprocess.run`` equivalent for meshd-owned children (git, probes).

    On Windows, supplying ``job=`` grants timeout/failure kill-scope over that Job.
    """
    input_data = kwargs.pop("input", None)
    check = kwargs.pop("check", False)
    if input_data is not None and "stdin" not in kwargs:
        kwargs["stdin"] = subprocess.PIPE
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    try:
        proc = popen_registered(kind, argv, **kwargs)
    except SpawnedThenFailed as exc:
        # Blind review P2: the child exists but was never registered; stop it (and its
        # group when it leads one) instead of leaving it running, held and unbounded.
        try:
            _kill_after_timeout(exc.proc, kwargs.get("start_new_session", False))
            try:
                exc.proc.communicate(timeout=POST_KILL_DRAIN_S)
            except Exception:
                pass
        finally:
            if sys.platform == "win32":
                _close_owned_proc_job(exc.proc)
            unhold(exc.proc.pid)
        raise
    try:
        try:
            out, err = proc.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_after_timeout(proc, kwargs.get("start_new_session", False))
            out, err = None, None
            try:
                out, err = proc.communicate(timeout=POST_KILL_DRAIN_S)
            except subprocess.TimeoutExpired:
                # Linear full review B4: a descendant still holds the output pipes; stop
                # reading (the child itself is dead) instead of waiting for it forever.
                # On Windows, _communicate's daemon _readerthread holds BufferedReader's
                # lock inside blocking ReadFile, so closing stdout/stderr here would block
                # until the descendant exits. Close stdin only and wait for the killed child.
                to_close = (proc.stdin,) if sys.platform == "win32" else (proc.stdout, proc.stderr, proc.stdin)
                for f in to_close:
                    try:
                        if f is not None:
                            f.close()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=POST_KILL_DRAIN_S)
                except subprocess.TimeoutExpired:
                    pass
            raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    finally:
        if sys.platform == "win32":
            # Deliberate cross-platform divergence: closing an owned ephemeral
            # KILL_ON_JOB_CLOSE job terminates any surviving descendants on return.
            _close_owned_proc_job(proc)
        unregister(proc.pid)
    result = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    if check:
        result.check_returncode()
    return result


def run_registered_bounded(
    kind: str,
    argv: List[str],
    *,
    max_stdout_bytes: int,
    max_stderr_bytes: int = 64 * 1024,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> Tuple[int, bytes, bytes, bool]:
    """Run a registered child process while bounding captured stdout/stderr in memory.

    Reads stdout incrementally up to ``max_stdout_bytes`` bytes; as soon as stdout
    exceeds ``max_stdout_bytes``, terminates the child (and its process group / Win32 Job)
    so an oversized output (such as a huge ``git diff``) cannot exhaust daemon RAM.

    Returns ``(returncode, stdout_bytes, stderr_bytes, stdout_truncated)``.
    Raises ``subprocess.TimeoutExpired`` if *timeout* elapses before completion.
    """
    kwargs.pop("capture_output", None)
    kwargs.pop("text", None)
    kwargs.pop("check", None)
    kwargs.pop("input", None)
    kwargs["stdout"] = subprocess.PIPE
    kwargs["stderr"] = subprocess.PIPE
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    own_group = bool(kwargs.get("start_new_session", False))
    cap = max(0, int(max_stdout_bytes))
    err_cap = max(0, int(max_stderr_bytes))

    try:
        proc = popen_registered(kind, argv, **kwargs)
    except SpawnedThenFailed as exc:
        try:
            _kill_after_timeout(exc.proc, own_group)
            try:
                exc.proc.communicate(timeout=POST_KILL_DRAIN_S)
            except Exception:
                pass
        finally:
            if sys.platform == "win32":
                _close_owned_proc_job(exc.proc)
            unhold(exc.proc.pid)
        raise

    out_chunks: List[bytes] = []
    err_chunks: List[bytes] = []
    cap_hit = threading.Event()
    out_done = threading.Event()

    def _read_stdout() -> None:
        total = 0
        stream = proc.stdout
        try:
            if stream is None:
                return
            while True:
                chunk_size = min(16384, (cap + 1) - total)
                if chunk_size <= 0:
                    cap_hit.set()
                    break
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                if total + len(chunk) > cap:
                    take = cap - total
                    if take > 0:
                        out_chunks.append(chunk[:take])
                    cap_hit.set()
                    break
                out_chunks.append(chunk)
                total += len(chunk)
        except OSError:
            pass
        finally:
            out_done.set()

    def _read_stderr() -> None:
        total = 0
        stream = proc.stderr
        try:
            if stream is None:
                return
            while True:
                chunk = stream.read(16384)
                if not chunk:
                    break
                if total < err_cap:
                    take = min(len(chunk), err_cap - total)
                    err_chunks.append(chunk[:take])
                    total += take
        except OSError:
            pass

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    deadline = (time.monotonic() + timeout) if timeout is not None else None
    try:
        wait_s = None if deadline is None else max(0.0, deadline - time.monotonic())
        finished_out = out_done.wait(timeout=wait_s)
        if cap_hit.is_set():
            _kill_after_timeout(proc, own_group)
            try:
                proc.wait(timeout=POST_KILL_DRAIN_S)
            except subprocess.TimeoutExpired:
                pass
            if sys.platform != "win32":
                for f in (proc.stdout, proc.stderr):
                    try:
                        if f is not None:
                            f.close()
                    except OSError:
                        pass
            t_out.join(timeout=POST_KILL_DRAIN_S)
            t_err.join(timeout=POST_KILL_DRAIN_S)
            rc = proc.returncode if proc.returncode is not None else -9
            return rc, b"".join(out_chunks), b"".join(err_chunks), True

        if not finished_out:
            _kill_after_timeout(proc, own_group)
            try:
                proc.wait(timeout=POST_KILL_DRAIN_S)
            except subprocess.TimeoutExpired:
                pass
            if sys.platform != "win32":
                for f in (proc.stdout, proc.stderr):
                    try:
                        if f is not None:
                            f.close()
                    except OSError:
                        pass
            t_out.join(timeout=POST_KILL_DRAIN_S)
            t_err.join(timeout=POST_KILL_DRAIN_S)
            raise subprocess.TimeoutExpired(argv, timeout, output=b"".join(out_chunks), stderr=b"".join(err_chunks))

        rem = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=rem)
        except subprocess.TimeoutExpired:
            _kill_after_timeout(proc, own_group)
            try:
                proc.wait(timeout=POST_KILL_DRAIN_S)
            except subprocess.TimeoutExpired:
                pass
            if sys.platform != "win32":
                for f in (proc.stdout, proc.stderr):
                    try:
                        if f is not None:
                            f.close()
                    except OSError:
                        pass
            t_out.join(timeout=POST_KILL_DRAIN_S)
            t_err.join(timeout=POST_KILL_DRAIN_S)
            raise subprocess.TimeoutExpired(argv, timeout, output=b"".join(out_chunks), stderr=b"".join(err_chunks))

        t_out.join(timeout=POST_KILL_DRAIN_S)
        t_err.join(timeout=POST_KILL_DRAIN_S)
        return int(proc.returncode or 0), b"".join(out_chunks), b"".join(err_chunks), False
    finally:
        if sys.platform != "win32" or (not t_out.is_alive() and not t_err.is_alive()):
            for f in (proc.stdout, proc.stderr):
                try:
                    if f is not None:
                        f.close()
                except OSError:
                    pass
        if sys.platform == "win32":
            _close_owned_proc_job(proc)
        unregister(proc.pid)


async def create_subprocess_exec_registered(kind: str, *argv: str, **kwargs: Any) -> "asyncio.subprocess.Process":
    """``asyncio.create_subprocess_exec`` with registration inside the spawn window.

    The fork happens before the await returns, so the child may already exist
    (or even have exited) while the pid is unknown; the window covers that gap.
    On Windows, supplying ``job=`` assigns the child to that Job Object and
    grants setup-failure kill-scope over it.
    """
    job = kwargs.pop("job", None)
    if sys.platform != "win32":
        with spawn_window():
            proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
            try:
                register(proc.pid, kind)
            except BaseException:
                import signal as _signal

                if kwargs.get("start_new_session"):
                    try:
                        os.killpg(proc.pid, _signal.SIGKILL)
                    except OSError:
                        pass
                try:
                    proc.kill()
                except OSError:
                    pass
                await proc.wait()
                raise
        return proc

    own_group = bool(kwargs.pop("start_new_session", False))
    created_job = False
    if job is None and own_group:
        job = _plat_proc.create_job(kill_on_close=(kind not in ("wrapper", "merge_test")))
        created_job = True
    with spawn_window():
        if job is not None:
            from mesh.platform import _win32_ctypes as w32

            flags = int(kwargs.pop("creationflags", 0) or 0)
            flags |= w32.CREATE_SUSPENDED | w32.CREATE_NEW_PROCESS_GROUP
            if not (flags & (w32.DETACHED_PROCESS | w32.CREATE_NEW_CONSOLE)):
                flags |= w32.CREATE_NO_WINDOW
            kwargs["creationflags"] = flags
        try:
            proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        except BaseException:
            if created_job and job is not None:
                try:
                    job.close()
                except OSError:
                    pass
            raise
        try:
            if job is not None:
                transport = getattr(proc, "_transport", None)
                raw_proc = getattr(transport, "_proc", None) if transport is not None else None
                proc_handle = getattr(raw_proc, "_handle", None)
                if proc_handle is None:
                    proc_handle = getattr(proc, "_handle", None)
                if proc_handle is not None:
                    raw_h = int(proc_handle)
                    _plat_proc.assign_process_handle_to_job(job, raw_h)
                    _plat_proc.resume_process_main_thread(raw_h, proc.pid)
                else:
                    _plat_proc.assign_process_to_job(job, proc.pid)
                    _plat_proc.resume_process_main_thread(None, proc.pid)
                proc._mesh_job = job  # type: ignore[attr-defined]
                proc._mesh_job_handle = job.handle  # type: ignore[attr-defined]
                proc._mesh_job_owned = created_job  # type: ignore[attr-defined]
            register(proc.pid, kind)
        except BaseException:
            if job is not None:
                try:
                    _plat_proc.terminate_job(job, 137)
                except OSError:
                    pass
            try:
                proc.kill()
            except OSError:
                pass
            await proc.wait()
            if created_job and job is not None:
                try:
                    job.close()
                except OSError:
                    pass
            raise
    return proc


#: Round 9: pids meshd keeps unreaped on purpose although they are not registered (a
#: pending spawn abort's wrapper, whose pid keeps its pgid reserved). Never reaped here.
_held: set = set()


def hold(pid: int) -> None:
    with _lock:
        _held.add(int(pid))


def unhold(pid: int) -> None:
    with _lock:
        _held.discard(int(pid))


def reap_orphans() -> List[int]:
    """Reap unregistered zombie children of meshd; skip entirely while a spawn is in flight."""
    me = os.getpid()
    with _lock:
        if _spawns_in_flight > 0:
            stats["scans_skipped_in_flight"] += 1
            return []
        stats["scans"] += 1
    if sys.platform == "win32":
        # NT does not leave unreaped zombie PIDs in a global process table.
        return []
    reaped: List[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return reaped
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            info = get_proc_stat(pid)
            if info is None or is_tgid_alive(pid, info) or info["ppid"] != me:
                continue
        except OSError:
            continue
        with _lock:
            if _spawns_in_flight > 0:
                # A spawn started during the scan; its child may be this zombie.
                stats["scans_aborted_in_flight"] += 1
                return reaped
            if pid in _registry or pid in _held:
                continue
        try:
            done_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if done_pid == pid:
            reaped.append(pid)
            stats["reaped"] += 1
    return reaped
