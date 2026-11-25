# Changelog

All notable changes to Agent Mesh are documented in this file.

## Unreleased — Desktop Simplification (S1 + S3) & Native Windows 10/11 Port

### Added
- **Desktop auto-start & connection diagnostics (`mesh/desktop/`)**:
  - Automatic single-flight `mesh daemon start` when `$MESH_HOME` is initialized and `meshd` is unreachable (`DAEMON_UNAVAILABLE`), with crash-loop rate limiting (3 attempts per 5 minutes), persistent onboarding (`mesh setup`), draining, user-stopped, and error cards, plus an in-memory diagnostics modal.
  - Explicit stop-daemon suppression tracks the stopped daemon's PID/boot ID so late probes from a shutting-down `meshd` never trigger an unintended restart, and clears `daemon_stopping` once `meshd.lock` is released.
- **Adapter profile & model preset metadata (`mesh/adapters/`, `mesh/daemon/handlers/adapter.py`)**:
  - Exposed per-adapter `supported_profiles`, `profile_flags`, and `model_presets` via `adapter.list`, `adapter.show`, and `adapter.probe`.
  - Updated desktop Create/Edit Agent modals to filter profiles to `supported_profiles`, render preset model chips from backend `model_presets`, group unavailable/held/stub adapters (`devin`, `hermes`, `openclaw`) behind an opt-in advanced group, and require an explicit confirmation dialog naming the vendor CLI flag before saving `default_profile` above `max_unapproved_profile`.
- **Cross-platform OS abstraction layer (`mesh/platform/`)**:
  - `mesh/platform/paths.py`, `fs.py`: OS temp root discovery, case-insensitive drive/UNC normalization, reparse/junction detection, and atomic file replacement.
  - `mesh/platform/locks.py`, `perms.py`, `doctor_perms_win.py`: Win32 `LockFileEx`/`UnlockFileEx` advisory byte-0 range locks, owner-only NTFS DACLs (`CurrentUserSID`, `SYSTEM`, `BUILTIN\Administrators`), and handle-pinned `mesh doctor --fix-perms` on Windows.
  - `mesh/platform/proc.py`, `_win32.py`, `_win32_ctypes.py`: `CreationTime`-pinned process liveness, Toolhelp32 upward ancestry validation, PEB `MESH_RUN_ID` environment inspection, anonymous `KILL_ON_JOB_CLOSE` ephemeral jobs, and owner-only Named Run Job Objects (`Local\mesh-job-...`) + Named Stop Events (`Local\mesh-stop-...`) with `LimitFlags=0` so runs survive `meshd` restarts.
  - `mesh/platform/pipe_pump.py`: Overlapped Win32 Named Pipe stdout/stderr drainer for `mesh.runwrap` and bounded adapter probes without hanging on escaped grandchildren.
  - `mesh/platform/ipc.py`: `ProactorEventLoop` overlapped Named Pipe server (`\\.\pipe\LOCAL\agent-mesh-...`) with `PIPE_REJECT_REMOTE_CLIENTS`, `GetNamedPipeClientProcessId` + `TokenUser` SID verification, and overlapped client `PipeClientStream`.
- **Cross-platform offline test runner (`scripts/run_suite.py`)** and tripwire `.cmd` shims (`tests/shims/*.cmd`), running `unit`, `integration`, and `recovery` tiers on both Linux and Windows (`windows-latest` CI).
- **Packaging and documentation**: updated `pyproject.toml` Windows/Python 3.12 classifiers and documented Windows installation, usage, and platform limitations in `README.md`, `docs/usage.md`, and `docs/restrictions.md`.

### Changed
- **Adapter-aware `agent.create` safe `default_profile`**: when `default_profile` is omitted, `meshd` now selects the adapter's first supported safe profile from `("edit", "dev", "readonly")` (so `codex` defaults to `dev` instead of unsupported `edit`, while never auto-defaulting to `elevated` or `unrestricted`). The CLI (`mesh agent create` and `mesh agent onboard`) omits `default_profile` when not passed so `meshd` remains the single source of truth.
- **Portable Windows instructions**: updated `docs/desktop.md` and `scripts/build_windows_portable.py` (`README.txt`) to recommend `mesh.exe setup` instead of `mesh.exe init`.
- **Zero Linux behavioral regressions**: all Linux `/proc`, `AF_UNIX`, `SO_PEERCRED`, `fcntl.flock`, and process-group code paths remain unchanged.
- **`tests/recovery` and `tests/unit` portability**: adapted recovery tests for Job Object/Toolhelp inspection and Win32 crash codes, removed centralized `tests/win_skips.py`, and replaced genuine Linux-only / POSIX shell-script test skips with explicit `@unittest.skipUnless` decorators.
