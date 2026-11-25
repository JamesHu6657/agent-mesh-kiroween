#!/usr/bin/env python3
"""Cross-platform Agent Mesh offline test runner (mirrors scripts/test.sh).

Runs unit, integration, and recovery tiers sequentially via ``tests/run_tier.py``
with isolated tripwire logs, suite-lock daemon reaping, and PATH shim prefixing
across Linux and Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import signal
import subprocess
import sys
import time
from typing import Dict, Iterable, Mapping, Optional, Sequence, TextIO, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mesh.platform import locks as plat_locks, paths as plat_paths, perms as plat_perms
from scripts.coord import reap_daemons

TIERS: Tuple[Tuple[str, str], ...] = (
    ("Unit", os.path.join("tests", "unit")),
    ("Integration", os.path.join("tests", "integration")),
    ("Recovery", os.path.join("tests", "recovery")),
)


def tripwire_paths(repo_root: str = REPO_ROOT, temp_dir: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(suite_tripwire_file, default_tripwire_file)`` keyed by *repo_root*."""
    base = temp_dir if temp_dir is not None else plat_paths.temp_root()
    repo_key = hashlib.sha256(os.path.abspath(repo_root).encode("utf-8")).hexdigest()[:12]
    return (
        os.path.join(base, f"mesh-tripwire-suite-{repo_key}.log"),
        os.path.join(base, "mesh-tripwire-hit.log"),
    )


def clear_tripwire_files(paths: Iterable[str]) -> None:
    """Remove any leftover tripwire logs before running the suite."""
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def verify_tripwire_clean(
    suite_log: str,
    default_log: str,
    *,
    out: Optional[TextIO] = None,
) -> bool:
    """Return True if neither tripwire file exists with non-zero contents."""
    stream = out if out is not None else sys.stdout
    for label, path in (("Suite", suite_log), ("Default", default_log)):
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                print(f"FAIL: {label} tripwire was triggered during test suite run!", file=stream, flush=True)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    print(content, file=stream, end="" if content.endswith("\n") else "\n", flush=True)
                except OSError:
                    pass
                return False
        except OSError:
            continue
    return True


def _acquire_suite_lock(
    registry: str,
    suite_id: str,
    *,
    stderr: Optional[TextIO] = None,
) -> Tuple[Optional[int], Optional[str]]:
    """Create and exclusively lock ``suite-<id>.lock`` inside *registry*, or return ``(None, None)``."""
    err_stream = stderr if stderr is not None else sys.stderr
    lock_path = os.path.join(registry, f"suite-{suite_id}.lock")
    fd: Optional[int] = None
    try:
        reap_daemons.prepare_registry(registry)
        fd = plat_locks.open_lock_file(lock_path, mode=0o600)
        if sys.platform == "win32":
            plat_perms.set_private_file_acl(fd)
        plat_locks.acquire_lock(fd, exclusive=True, blocking=False)
        return fd, lock_path
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(lock_path)
            except OSError:
                pass
        print(
            "reap-daemons: suite lock unavailable; using adhoc process ownership",
            file=err_stream,
            flush=True,
        )
        return None, None


def _run_reap(repo_root: str, env: Mapping[str, str], python_exe: str = sys.executable) -> None:
    """Best-effort invocation of ``scripts/coord/reap_daemons.py`` bounded by 30s."""
    reap_script = os.path.join(repo_root, "scripts", "coord", "reap_daemons.py")
    try:
        subprocess.run(
            [python_exe, reap_script],
            cwd=repo_root,
            env=dict(env),
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _release_and_cleanup_suite(
    lock_fd: Optional[int],
    lock_path: Optional[str],
    repo_root: str,
    env: Mapping[str, str],
    *,
    python_exe: str = sys.executable,
) -> None:
    """Release the suite lock, reap leftover daemons, and remove the suite lock file."""
    if lock_fd is not None:
        try:
            plat_locks.release_lock(lock_fd)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass
    _run_reap(repo_root, env, python_exe=python_exe)
    if lock_path:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def build_suite_env(
    base_env: Mapping[str, str],
    repo_root: str,
    tripwire_file: str,
    registry: str,
    suite_id: Optional[str],
) -> Dict[str, str]:
    """Construct environment dictionary for child test tiers with prefixed shim/bin PATH."""
    env = dict(base_env)
    env["_TRIPWIRE_FILE"] = tripwire_file
    env["_SUITE_TRIPWIRE_FILE"] = tripwire_file
    env["MESH_TEST_DAEMON_REGISTRY"] = registry
    if suite_id:
        env["MESH_TEST_SUITE_ID"] = suite_id
    else:
        env.pop("MESH_TEST_SUITE_ID", None)
    shims_dir = os.path.join(repo_root, "tests", "shims")
    bin_dir = os.path.join(repo_root, "bin")
    orig_path = env.get("PATH", "")
    prefix = os.pathsep.join((shims_dir, bin_dir))
    env["PATH"] = f"{prefix}{os.pathsep}{orig_path}" if orig_path else prefix
    return env


def run_suite(
    repo_root: str = REPO_ROOT,
    *,
    tiers: Sequence[Tuple[str, str]] = TIERS,
    check_only: bool = False,
    python_exe: str = sys.executable,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    """Run offline Agent Mesh test tiers sequentially and enforce clean tripwires."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    if check_only:
        print("scripts/run_suite.py: syntax and runner OK", file=out, flush=True)
        return 0

    start = time.monotonic()
    suite_tw, default_tw = tripwire_paths(repo_root)
    clear_tripwire_files((suite_tw, default_tw))

    suite_id = secrets.token_hex(16)
    registry = os.environ.get("MESH_TEST_DAEMON_REGISTRY") or os.path.join(
        plat_paths.temp_root(), "mesh-test-daemons"
    )
    lock_fd, lock_path = _acquire_suite_lock(registry, suite_id, stderr=err)
    held_id = suite_id if lock_fd is not None else None
    env = build_suite_env(os.environ, repo_root, suite_tw, registry, held_id)
    reap_env = dict(env)
    reap_env["PATH"] = os.environ.get("PATH", "")

    installed_handlers: list[tuple[int, object]] = []

    def _make_signal_handler(exit_code: int):
        def _handler(_signum: int, _frame: object) -> None:
            raise SystemExit(exit_code)
        return _handler

    for sig_name, exit_code in (("SIGTERM", 143), ("SIGBREAK", 130)):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            prev_handler = signal.getsignal(sig)
            signal.signal(sig, _make_signal_handler(exit_code))
            installed_handlers.append((sig, prev_handler))
        except (ValueError, OSError):
            continue

    try:
        _run_reap(repo_root, reap_env, python_exe=python_exe)
        runner = os.path.join(repo_root, "tests", "run_tier.py")
        for label, tier_rel in tiers:
            print(f"=== Running {label} Tests ===", file=out, flush=True)
            proc = subprocess.run([python_exe, runner, tier_rel], cwd=repo_root, env=env)
            if proc.returncode != 0:
                return int(proc.returncode)

        if not verify_tripwire_clean(suite_tw, default_tw, out=out):
            return 1

        elapsed = int(time.monotonic() - start)
        print("=== All test tiers passed successfully ===", file=out, flush=True)
        print(f"Total wall time: {elapsed}s", file=out, flush=True)
        return 0
    finally:
        _release_and_cleanup_suite(lock_fd, lock_path, repo_root, reap_env, python_exe=python_exe)
        for sig, prev_handler in reversed(installed_handlers):
            if prev_handler is not None:
                try:
                    signal.signal(sig, prev_handler)
                except (ValueError, OSError):
                    pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-platform Agent Mesh test suite runner.")
    parser.add_argument("--check", action="store_true", help="Verify runner syntax and exit 0.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    return run_suite(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
