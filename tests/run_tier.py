#!/usr/bin/env python3
"""Parallel and serial tier test runner for Agent Mesh.

Usage:
    python3 tests/run_tier.py <test_directory>

Discovers tests in <test_directory>, partitions them into parallel and serial sets
(based on the @serial decorator / mesh_serial attribute), runs parallel tests with
workers = MESH_TEST_JOBS or os.cpu_count(), then runs serial tests sequentially.

Parallel tests are independent by construction: every test gets its own /tmp/mh-*
home, its own daemon on its own Unix socket, and its own subprocesses, so the worker
count is bounded only by the CPU count (gate item A1: keep the suite under 10 min).
"""

import concurrent.futures
import fcntl
import tempfile
import json
import hashlib
import keyword
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import unittest
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _is_importable_under(start_abs: str, root_abs: str) -> bool:
    cur = os.path.realpath(start_abs)
    root = os.path.realpath(root_abs)
    try:
        if os.path.commonpath([root, cur]) != root:
            return False
    except ValueError:
        return False
    while cur != root:
        pkg = os.path.basename(cur)
        if not pkg.isidentifier() or keyword.iskeyword(pkg):
            return False
        if not os.path.isfile(os.path.join(cur, "__init__.py")):
            return False
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent
    return True


def _build_child_pythonpath(top_level: str, base_pythonpath: Optional[str] = None) -> str:
    entries: List[str] = []
    for p in (os.path.realpath(top_level), REPO_ROOT):
        if p and p not in entries:
            entries.append(p)
    if base_pythonpath:
        for p in base_pythonpath.split(os.pathsep):
            if p and p not in entries:
                entries.append(p)
    return os.pathsep.join(entries)


_CHILD_PYTHONPATH: str = _build_child_pythonpath(REPO_ROOT, os.environ.get("PYTHONPATH"))


def extract_tests(suite: unittest.TestSuite) -> List[unittest.TestCase]:
    """Recursively extract all TestCase instances from a TestSuite."""
    tests: List[unittest.TestCase] = []
    stack = [suite]
    while stack:
        current = stack.pop(0)
        for item in current:
            if isinstance(item, unittest.TestSuite):
                stack.append(item)
            elif isinstance(item, unittest.TestCase):
                tests.append(item)
    return tests


def is_serial_test(test: unittest.TestCase) -> bool:
    """Check if test case class or test method has mesh_serial == True."""
    # Check class
    if getattr(test.__class__, "mesh_serial", False):
        return True
    # Check method
    method_name = getattr(test, "_testMethodName", None)
    if method_name:
        method = getattr(test, method_name, None)
        if method and getattr(method, "mesh_serial", False):
            return True
    return False


def is_idle_bound_test(test: unittest.TestCase) -> bool:
    """Check if test case class or test method has mesh_idle_bound == True."""
    if getattr(test.__class__, "mesh_idle_bound", False):
        return True
    method = getattr(test, getattr(test, "_testMethodName", "") or "", None)
    return bool(method is not None and getattr(method, "mesh_idle_bound", False))


IDLE_POOL_MAX = 4


# Host-wide /proc lock (dogfood: N concurrent tier runs on one host). Tests that scan
# the real /proc see every same-uid process on the host, including another runner's
# non-dumpable escapees (EACCES environ -> scan "uncertain"). mesh_serial only orders
# tests inside one runner, so these tests also take an flock shared by all runners of
# this uid: tests that spawn non-dumpable escapees hold it EXCLUSIVE, tests that scan
# the real /proc hold it SHARED. Scanners still run concurrently with each other; no
# scanner runs while any runner has a non-dumpable escapee alive. Production scan
# semantics are untouched. Marking: ``mesh_proc_lock = "exclusive" | "shared"`` on a
# test method or class, or ``MESH_PROC_LOCK = ...`` at module level.
#
# Integration and recovery tiers default to SHARED (batch 6 audit): every test there runs
# a real meshd whose run closure / termination / merge leftover scans read the real
# /proc, so another runner's EXCLUSIVE escapee test could make them "uncertain"
# (test_kill_failed_retry_then_dead, merge leftover_scan_uncertain under load).
#
# Writer preference: shared holders are many and overlap across runners, so a plain
# flock could starve an EXCLUSIVE test. Every acquirer first takes a turnstile lock
# (EX) for the moment of its own acquisition; an exclusive waiter holds the turnstile
# while it waits, so new shared holders queue behind it and the current ones drain.
PROC_LOCK_PATH = os.environ.get("MESH_TEST_PROC_LOCK") or os.path.join(
    tempfile.gettempdir(), f"agent-mesh-proc-tests-{os.getuid()}.lock")
PROC_TURNSTILE_PATH = PROC_LOCK_PATH + ".turnstile"
SHARED_BY_DEFAULT_TIERS = ("integration", "recovery")
DEFAULT_PROC_LOCK: Optional[str] = None


def proc_lock_mode(test: unittest.TestCase) -> Optional[str]:
    """``"exclusive"``, ``"shared"`` or ``None`` for a test (method > class > module)."""
    method = getattr(test, getattr(test, "_testMethodName", "") or "", None)
    for holder in (method, test.__class__):
        mode = getattr(holder, "mesh_proc_lock", None) if holder is not None else None
        if mode:
            return mode
    module = sys.modules.get(test.__class__.__module__)
    mode = getattr(module, "MESH_PROC_LOCK", None) if module is not None else None
    return mode or DEFAULT_PROC_LOCK


class _ProcLock:
    def __init__(self, mode: Optional[str]) -> None:
        self.mode = mode
        self.fd: Optional[int] = None

    def __enter__(self) -> "_ProcLock":
        if self.mode in ("exclusive", "shared"):
            self.fd = os.open(PROC_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
            turn = os.open(PROC_TURNSTILE_PATH, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(turn, fcntl.LOCK_EX)
                fcntl.flock(self.fd, fcntl.LOCK_EX if self.mode == "exclusive" else fcntl.LOCK_SH)
            finally:
                os.close(turn)  # releases the turnstile
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


PROC_LOCK_MODES: Dict[str, Optional[str]] = {}


def run_single_test(
    test_id: str, timeout_s: int, pythonpath: Optional[str] = None
) -> Tuple[str, str, float, str]:
    """Run a single test ID as an isolated subprocess (under its host-wide /proc lock)."""
    with _ProcLock(PROC_LOCK_MODES.get(test_id)):
        return _run_single_test(test_id, timeout_s, pythonpath=pythonpath)


def _run_single_test(
    test_id: str, timeout_s: int, pythonpath: Optional[str] = None
) -> Tuple[str, str, float, str]:
    """Run a single test ID as an isolated subprocess.

    Returns:
        (test_id, status ['ok', 'skipped', 'FAIL', 'ERROR'], duration_secs, stdout_output)
    """
    cmd = [sys.executable, "-m", "unittest", test_id]
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = pythonpath if pythonpath is not None else _CHILD_PYTHONPATH
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=child_env,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - t0
            # Kill process group
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
            proc.communicate()
            return test_id, "ERROR", duration, f"ERROR: timeout ({timeout_s}s expired)\n"

        duration = time.monotonic() - t0
        rc = proc.returncode
        if rc == 0:
            if re.search(r"(?m)^OK \((?:[^)]*\b)?skipped=\d+", stdout):
                status = "skipped"
            else:
                status = "ok"
        else:
            if "\nFAIL: " in stdout or stdout.startswith("FAIL: ") or "FAILED (failures=" in stdout:
                status = "FAIL"
            else:
                status = "ERROR"
        return test_id, status, duration, stdout
    except Exception as exc:
        duration = time.monotonic() - t0
        return test_id, "ERROR", duration, f"ERROR: runner exception: {exc}\n"


def _load_durations(path: str) -> Dict[str, float]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_durations(path: str, fresh: Dict[str, float], previous: Dict[str, float]) -> None:
    merged = dict(previous)
    merged.update(fresh)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <test_directory>", file=sys.stderr)
        return 1

    test_dir = sys.argv[1]
    global DEFAULT_PROC_LOCK, _CHILD_PYTHONPATH
    if os.path.basename(os.path.normpath(test_dir)) in SHARED_BY_DEFAULT_TIERS:
        DEFAULT_PROC_LOCK = "shared"
    timeout_s = int(os.environ.get("MESH_TEST_TIMEOUT_S", "600"))

    # Discover tests
    loader = unittest.TestLoader()
    cwd_abs = os.path.realpath(".")
    dir_abs = os.path.realpath(test_dir)
    if _is_importable_under(dir_abs, REPO_ROOT):
        top_level = REPO_ROOT
    elif _is_importable_under(dir_abs, cwd_abs):
        top_level = cwd_abs
    else:
        top_level = dir_abs
    _CHILD_PYTHONPATH = _build_child_pythonpath(top_level, os.environ.get("PYTHONPATH"))
    discover_target = dir_abs if os.path.isdir(dir_abs) else test_dir
    all_cases: List[unittest.TestCase] = []
    discovery_results: List[Tuple[str, str, float, str]] = []
    try:
        suite = loader.discover(discover_target, top_level_dir=top_level)
        all_cases = extract_tests(suite)
    except ImportError:
        discovery_results.append(("discovery_error", "ERROR", 0.0, traceback.format_exc()))

    if not all_cases and not loader.errors and not discovery_results:
        print(f"ERROR: No tests discovered in {test_dir}", file=sys.stderr)
        return 1

    parallel_ids: List[str] = []
    serial_ids: List[str] = []
    idle_ids: List[str] = []
    failed_cls = getattr(unittest.loader, "_FailedTest", None)
    mod_skipped_classes = tuple(
        cls for cls in (
            getattr(unittest.loader, "ModuleSkipped", None),
            getattr(unittest.loader, "_ModuleSkipped", None),
        )
        if cls is not None
    )

    for case in all_cases:
        t_id = case.id()
        if (mod_skipped_classes and isinstance(case, mod_skipped_classes)) or type(case).__name__ in (
            "ModuleSkipped",
            "_ModuleSkipped",
        ):
            skip_res = unittest.TestResult()
            case.run(skip_res)
            reason = skip_res.skipped[0][1] if skip_res.skipped else ""
            discovery_results.append((t_id, "skipped", 0.0, reason))
            continue
        exc = getattr(case, "_exception", None)
        if exc is not None or (failed_cls is not None and isinstance(case, failed_cls)):
            discovery_results.append((t_id, "ERROR", 0.0, str(exc) if exc is not None else repr(case)))
            continue
        PROC_LOCK_MODES[t_id] = proc_lock_mode(case)
        if is_serial_test(case):
            serial_ids.append(t_id)
        elif is_idle_bound_test(case):
            idle_ids.append(t_id)
        else:
            parallel_ids.append(t_id)
    seen_errors = {out for _, status, _, out in discovery_results if status == "ERROR"}
    if loader.errors:
        for idx, err in enumerate(loader.errors, 1):
            if err not in seen_errors and not any(err in prev for prev in seen_errors):
                discovery_results.append((f"discovery_error_{idx}", "ERROR", 0.0, err))
                seen_errors.add(err)

    # Longest-first (review m1c-run2 §2 speedup 5): durations of the previous run,
    # unknown tests first (they may be long), then discovery order as the tie-break.
    durations_path = os.environ.get(
        "MESH_TEST_DURATIONS",
        # Keyed by repo root: other worktrees' suites keep their own ordering hints.
        f"/tmp/mesh-test-durations-{os.getuid()}-"
        f"{hashlib.sha256(os.path.realpath(test_dir).encode()).hexdigest()[:12]}-"
        f"{os.path.basename(os.path.normpath(test_dir))}.json"
    )
    previous = _load_durations(durations_path)
    order = {tid: i for i, tid in enumerate(parallel_ids)}
    parallel_ids.sort(key=lambda tid: (-previous.get(tid, float("inf")), order[tid]))

    total_count = len(all_cases)
    jobs_env = os.environ.get("MESH_TEST_JOBS")
    if jobs_env and jobs_env.isdigit() and int(jobs_env) > 0:
        workers = int(jobs_env)
    else:
        cpu_cnt = os.cpu_count() or 2
        workers = max(1, cpu_cnt)

    wall_start = time.monotonic()
    all_results: List[Tuple[str, str, float, str]] = []
    print_lock = threading.Lock()

    def record_and_print(res: Tuple[str, str, float, str]) -> None:
        t_id, status, dur, _ = res
        with print_lock:
            all_results.append(res)
            print(f"{status} {t_id} {dur:.3f}s", flush=True)

    for res in discovery_results:
        record_and_print(res)

    # 1. Run parallel tests. Idle-bound tests (@idle_bound: a required idle wait, little
    # CPU) get their own pool beside the workers, started first (A1 speed pass, m2-r7).
    idle_pool = None
    idle_futures: List[Any] = []
    if idle_ids:
        idle_pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(IDLE_POOL_MAX, len(idle_ids)))
        idle_futures = [idle_pool.submit(run_single_test, tid, timeout_s) for tid in idle_ids]
    if workers > 1 and len(parallel_ids) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(run_single_test, tid, timeout_s) for tid in parallel_ids]
            for fut in concurrent.futures.as_completed(futures):
                record_and_print(fut.result())
    else:
        for tid in parallel_ids:
            record_and_print(run_single_test(tid, timeout_s))

    for fut in idle_futures:
        record_and_print(fut.result())
    if idle_pool is not None:
        idle_pool.shutdown(wait=True)

    # 2. Run serial tests sequentially
    for tid in serial_ids:
        record_and_print(run_single_test(tid, timeout_s))

    _save_durations(durations_path, {t_id: dur for t_id, status, dur, _ in all_results if status == "ok"}, previous)

    wall_end = time.monotonic()
    wall_duration = wall_end - wall_start

    # Output full trace for any failing tests
    failing = [r for r in all_results if r[1] in ("FAIL", "ERROR")]
    if failing:
        print("\n" + "=" * 70, file=sys.stderr)
        print("FAILURES AND ERRORS:", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        for t_id, status, dur, out in failing:
            print(f"\n[{status}] {t_id} ({dur:.3f}s):", file=sys.stderr)
            print("-" * 70, file=sys.stderr)
            print(out.strip(), file=sys.stderr)
            print("-" * 70, file=sys.stderr)

    # 10 slowest tests
    print("\n" + "=" * 70)
    print("10 SLOWEST TESTS:")
    print("=" * 70)
    slowest = sorted(all_results, key=lambda r: r[2], reverse=True)[:10]
    for t_id, status, dur, _ in slowest:
        print(f"  {dur:7.3f}s  {status:7s}  {t_id}")

    # Exactly: Ran N tests in X.XXXs
    print(f"\nRan {len(all_results)} tests in {wall_duration:.3f}s\n")

    failures = sum(1 for r in all_results if r[1] == "FAIL")
    errors = sum(1 for r in all_results if r[1] == "ERROR")
    skipped = sum(1 for r in all_results if r[1] == "skipped")

    if failures == 0 and errors == 0 and len(all_results) > 0:
        if skipped > 0:
            print(f"OK (skipped={skipped})")
        else:
            print("OK")
        return 0
    else:
        parts = []
        if failures > 0:
            parts.append(f"failures={failures}")
        if errors > 0:
            parts.append(f"errors={errors}")
        if skipped > 0:
            parts.append(f"skipped={skipped}")
        print(f"FAILED ({', '.join(parts)})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
