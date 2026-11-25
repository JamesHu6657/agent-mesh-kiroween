"""Cross-platform process inspection, liveness, ancestry, and crash helpers.

Windows-specific imports (``_win32``, ``_win32_ctypes``) and POSIX-only imports
are guarded so importing this module on either OS never loads the other OS's primitives.
"""

from __future__ import annotations

import signal
import sys
from typing import Any, Dict, List, Optional, Set

SIGKILL = getattr(signal, "SIGKILL", 9)
SIGTERM = getattr(signal, "SIGTERM", 15)

if sys.platform != "win32":
    from mesh.platform import _posix
else:
    _posix: Any = None
    from mesh.platform._win32 import EventHandle, JobHandle  # noqa: F401


def _posix_mod() -> Any:
    """Lazily import ``mesh.platform._posix`` (supports mocked Linux tests on Windows)."""
    global _posix
    if _posix is None:
        from mesh.platform import _posix as posix_mod

        _posix = posix_mod
    return _posix


def get_proc_stat(pid: int) -> Optional[Dict[str, Any]]:
    """Read process stat/identity dict (``starttime``, ``ppid``, ...) or ``None`` if gone."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.get_proc_stat_win(pid)
    return _posix_mod().get_proc_stat(pid)


def is_tgid_alive(pid: int, info: Optional[Dict[str, Any]] = None) -> bool:
    """Return True while any thread in *pid* is alive."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.is_tgid_alive_win(pid, info)
    return _posix_mod().is_tgid_alive(pid, info)


def is_process_alive(pid: int, expected_starttime: Optional[int] = None) -> bool:
    """Return True iff *pid* is live and (if provided) matches *expected_starttime*."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.is_process_alive_win(pid, expected_starttime)
    return _posix_mod().is_process_alive(pid, expected_starttime)


def get_proc_environ(pid: int) -> Optional[Dict[str, str]]:
    """Read and parse *pid*'s environment variables into a dict, or ``None`` if gone."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.get_proc_environ_win(pid)
    return _posix_mod().get_proc_environ(pid)


def get_proc_cmdline(pid: int) -> Optional[List[str]]:
    """Read and parse *pid*'s command-line argv list, or ``None`` if gone."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.get_proc_cmdline_win(pid)
    return _posix_mod().get_proc_cmdline(pid)


def get_proc_ancestry(pid: int, stop_at_pids: Optional[Set[int]] = None) -> List[int]:
    """Walk parent PIDs upward from *pid* until root or any pid in *stop_at_pids*."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.get_proc_ancestry_win(pid, stop_at_pids)
    return _posix_mod().get_proc_ancestry(pid, stop_at_pids)


def find_run_processes(
    run_id: str,
    pgid: Optional[int],
    exclude: Optional[Set[int]] = None,
) -> Any:
    """Find live processes belonging to *run_id* (by ancestry/group or ``MESH_RUN_ID``)."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.find_run_processes_win(run_id, pgid, exclude)
    return _posix_mod().find_run_processes(run_id, pgid, exclude)


def crash_self_now() -> None:
    """Immediately kill the current process without flushing buffers (testing crash hooks)."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        _win32.crash_self_now_win()
        return
    _posix_mod().crash_self_now()


def run_job_name(home: str, run_id: str) -> str:
    """Return the deterministic Win32 Named Job Object path for *home* and *run_id*."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    return _win32.run_job_name_win(home, run_id)


def run_stop_event_name(home: str, run_id: str) -> str:
    """Return the deterministic Win32 Named Stop Event path for *home* and *run_id*."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Named Events are only available on Windows")
    from mesh.platform import _win32

    return _win32.run_stop_event_name_win(home, run_id)


def create_job(*, kill_on_close: bool = True, name: Optional[str] = None) -> Any:
    """Create a Win32 Job Object with breakaway disabled."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    return _win32.create_job_win(kill_on_close=kill_on_close, name=name)


def create_ephemeral_job() -> Any:
    """Create an anonymous Win32 Job Object with ``KILL_ON_JOB_CLOSE`` enabled."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    return _win32.create_ephemeral_job_win()


def create_run_job(home: str, run_id: str, *, kill_on_close: bool = False) -> Any:
    """Create an owner-only Named Run Job Object (``LimitFlags=0`` by default for I8)."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    return _win32.create_run_job_win(home, run_id, kill_on_close=kill_on_close)


def open_run_job(home: str, run_id: str, *, terminate: bool = True) -> Optional[Any]:
    """Re-open an existing Named Run Job Object by name, or ``None`` if not found."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    return _win32.open_run_job_win(home, run_id, terminate=terminate)


def create_stop_event(home: str, run_id: str) -> Any:
    """Create an owner-only manual-reset Named Stop Event for graceful run termination."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Named Events are only available on Windows")
    from mesh.platform import _win32

    return _win32.create_stop_event_win(home, run_id)


def signal_run_stop_event(home: str, run_id: str) -> bool:
    """Signal the Win32 Named Stop Event for (*home*, *run_id*), returning True if set."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Named Events are only available on Windows")
    from mesh.platform import _win32

    return _win32.signal_run_stop_event_win(home, run_id)


def assign_process_to_job(job: Any, proc_or_pid: Any) -> None:
    """Assign *proc_or_pid* (a ``Popen`` instance or PID) to Win32 *job*."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    _win32.assign_process_to_job_win(job, proc_or_pid)


def assign_process_handle_to_job(job: Any, proc_handle: Any) -> None:
    """Assign an explicit Win32 process handle *proc_handle* to *job*."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    _win32.assign_process_handle_to_job_win(job, proc_handle)


def query_job_pids(job: Any) -> List[int]:
    """Return sorted live PIDs assigned to Win32 *job* (including nested child jobs)."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    return _win32.query_job_pids_win(job)


def is_process_in_job(pid_or_proc: Any, job: Any) -> bool:
    """Return True iff *pid_or_proc* is currently associated with Win32 *job*."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    return _win32.is_process_in_job_win(pid_or_proc, job)


def terminate_job(job: Any, exit_code: int = 137) -> None:
    """Terminate all processes associated with Win32 *job*."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    _win32.terminate_job_win(job, exit_code=exit_code)


def resume_process_main_thread(proc_handle: Any, pid: int) -> None:
    """Resume the initial suspended thread of a newly spawned process."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 suspended thread resume is only available on Windows")
    from mesh.platform import _win32

    _win32.resume_process_main_thread_win(proc_handle, pid)


def spawn_in_job(argv: Any, job: Any, **popen_kwargs: Any) -> Any:
    """Spawn *argv* suspended, assign to *job*, resume main thread, and attach ``_mesh_job``."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 Job Objects are only available on Windows")
    from mesh.platform import _win32

    return _win32.spawn_in_job_win(argv, job, **popen_kwargs)


def signal_if_same(
    pid: int,
    starttime: int,
    sig: int,
    run_id: Optional[str] = None,
    home: Optional[str] = None,
) -> bool:
    """Send *sig* to *pid* only if live and its start time matches *starttime*."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.signal_if_same_win(pid, starttime, sig, run_id=run_id, home=home)
    return _posix_mod().signal_if_same(pid, starttime, sig, run_id=run_id, home=home)


def kill_if_same(pid: int, starttime: int) -> bool:
    """Hard-kill *pid* only if live and its start time matches *starttime*."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.kill_if_same_win(pid, starttime)
    return _posix_mod().kill_if_same(pid, starttime)


def is_popen_exited_unreaped(proc: Any) -> bool:
    """Return True once ``proc`` has exited, without closing its wait handle / reaping it."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.is_popen_exited_unreaped_win(proc)
    return _posix_mod().is_popen_exited_unreaped(proc)


def detached_popen_kwargs() -> Dict[str, Any]:
    """Return ``subprocess.Popen`` kwargs for spawning a detached background daemon."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.detached_popen_kwargs_win()
    return _posix_mod().detached_popen_kwargs()


def reexec_clean_env(argv: List[str], env: Dict[str, str]) -> None:
    """Re-execute *argv* with clean *env* (``os.execve`` on POSIX; sync spawn+exit on Windows)."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        _win32.reexec_clean_env_win(argv, env)
        return
    _posix_mod().reexec_clean_env(argv, env)


def find_active_job_for_pid(
    pid: int,
    active_runs: Dict[str, int],
    *,
    active_job_handles: Optional[Dict[str, Any]] = None,
    home: Optional[str] = None,
) -> Optional[str]:
    """Return active ``run_id`` if *pid* belongs to an active run Job Object on Windows."""
    if sys.platform == "win32":
        from mesh.platform import _win32

        return _win32.find_active_job_for_pid_win(
            pid,
            active_runs,
            active_job_handles=active_job_handles,
            home=home,
        )
    return _posix_mod().find_active_job_for_pid(
        pid,
        active_runs,
        active_job_handles=active_job_handles,
        home=home,
    )


def list_toolhelp_processes() -> Dict[int, int]:
    """Return a Win32 Toolhelp snapshot mapping ``{pid: ppid}``."""
    if sys.platform != "win32":
        raise NotImplementedError("Toolhelp process snapshots are only available on Windows")
    from mesh.platform import _win32

    return _win32.list_toolhelp_processes_win()


def get_process_user_sid(pid: int) -> str:
    """Return the Win32 ``TokenUser`` SID string of live process *pid*."""
    if sys.platform != "win32":
        raise NotImplementedError("Win32 process user SID inspection is only available on Windows")
    from mesh.platform import _win32

    return _win32.get_process_user_sid_win(pid)
