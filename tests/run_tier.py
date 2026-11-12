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
import json
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple


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


def run_single_test(test_id: str, timeout_s: int) -> Tuple[str, str, float, str]:
    """Run a single test ID as an isolated subprocess.

    Returns:
        (test_id, status ['ok', 'FAIL', 'ERROR'], duration_secs, stdout_output)
    """
    cmd = [sys.executable, "-m", "unittest", test_id]
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=dict(os.environ),
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
    timeout_s = int(os.environ.get("MESH_TEST_TIMEOUT_S", "600"))

    # Discover tests
    loader = unittest.TestLoader()
    suite = loader.discover(test_dir, top_level_dir=".")
    all_cases = extract_tests(suite)

    if not all_cases:
        print(f"ERROR: No tests discovered in {test_dir}", file=sys.stderr)
        return 1

    parallel_ids: List[str] = []
    serial_ids: List[str] = []
    idle_ids: List[str] = []

    for case in all_cases:
        t_id = case.id()
        if is_serial_test(case):
            serial_ids.append(t_id)
        elif is_idle_bound_test(case):
            idle_ids.append(t_id)
        else:
            parallel_ids.append(t_id)

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
    failing = [r for r in all_results if r[1] != "ok"]
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
        print(f"  {dur:7.3f}s  {status:5s}  {t_id}")

    # Exactly: Ran N tests in X.XXXs
    print(f"\nRan {len(all_results)} tests in {wall_duration:.3f}s\n")

    failures = sum(1 for r in all_results if r[1] == "FAIL")
    errors = sum(1 for r in all_results if r[1] == "ERROR")

    if failures == 0 and errors == 0 and len(all_results) > 0:
        print("OK")
        return 0
    else:
        parts = []
        if failures > 0:
            parts.append(f"failures={failures}")
        if errors > 0:
            parts.append(f"errors={errors}")
        print(f"FAILED ({', '.join(parts)})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
