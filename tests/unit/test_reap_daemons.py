"""Registry policy fixtures and pidfd tests, isolated from host/suite processes."""
import contextlib
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from scripts.coord import reap_daemons as reap
from tests.helpers import MeshHarness

ROOT = Path(__file__).resolve().parents[2]


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.record = {"pid": 123, "starttime": 456, "home": "/tmp/mh-fixture",
                       "suite_id": "fixture"}
        self.current = reap.parse_stat("123 (name with ) spaces) S " + "0 " * 18 + "456 0")

    def test_stat_field_22_and_record(self):
        self.assertEqual(self.current, ("S", 456))
        self.assertEqual(reap.validate_record(self.record), self.record)

    def test_home_gone_and_dead_suite(self):
        self.assertEqual(reap.decision(self.record, self.current, False, False), "home_gone")
        self.assertEqual(reap.decision(self.record, self.current, True, False), "dead_suite")

    def test_live_suite_kept_even_with_missing_home_or_arbitrary_age(self):
        for home_exists in (True, False):
            self.assertEqual(reap.decision(self.record, self.current, home_exists, True),
                             "skipped_live")
        # Starttime is an identity, not an age; even the oldest possible owner is kept.
        self.record["starttime"] = 1
        self.assertEqual(reap.decision(self.record, ("S", 1), True, True), "skipped_live")

    def test_pid_reuse_gone_and_zombie(self):
        for state in ("S", "Z"):
            self.assertEqual(reap.decision(self.record, (state, 457), False, False),
                             "pid_reused")
        self.assertEqual(reap.decision(self.record, None, True, True), "gone")
        self.assertEqual(reap.decision(self.record, ("Z", 456), True, True), "gone")

    def test_adhoc_owner_requires_pid_starttime_and_non_zombie(self):
        record = dict(self.record, suite_id="adhoc-123", suite_starttime=789)
        self.assertTrue(reap.adhoc_alive(record, ("S", 789)))
        for owner in (None, ("S", 790), ("Z", 789)):
            self.assertFalse(reap.adhoc_alive(record, owner))

    def test_malformed_record_fields(self):
        for update in ({"pid": True}, {"pid": 1}, {"starttime": "456"},
                       {"home": "/tmp/mh-a/../../var/mh-b"},
                       {"home": "/tmp/mh-a/nested"}, {"home": "/var/tmp/mh-a"},
                       {"home": "/tmp/mh-"}, {"suite_id": "../escape"},
                       {"suite_id": "adhoc-123"}, {"suite_id": ""}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                reap.validate_record(dict(self.record, **update))


class RegistryFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mesh-reap-registry-")
        self.addCleanup(self.directory.cleanup)
        self.registry = Path(self.directory.name) / "records"
        self.home_dir = tempfile.TemporaryDirectory(prefix="mh-reap-", dir="/tmp")
        self.addCleanup(self.home_dir.cleanup)
        self.home = self.home_dir.name
        env = dict(os.environ, MESH_TEST_DAEMON_REGISTRY=str(self.registry))
        env.pop("MESH_TEST_SUITE_ID", None)
        self.environment = mock.patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def fake_mesh_root(self):
        """A dir holding a stand-in ``mesh`` package (prints ready, sleeps), so a
        fixture has the real launcher argv shape ``python3 -P -s -m mesh ...``."""
        root = getattr(self, "_fake_mesh_root", None)
        if root is None:
            holder = tempfile.TemporaryDirectory(prefix="mesh-reap-fake-")
            self.addCleanup(holder.cleanup)
            os.mkdir(os.path.join(holder.name, "mesh"))
            for name, body in (("__init__.py", ""), ("__main__.py",
                               "import time\nprint('ready', flush=True)\ntime.sleep(60)\n")):
                with open(os.path.join(holder.name, "mesh", name), "w") as stream:
                    stream.write(body)
            root = self._fake_mesh_root = holder.name
        return root

    def daemon_child(self, home):
        """Stand-in daemon with the real launcher argv: python3 -P -s -m mesh --home H daemon run."""
        child = subprocess.Popen(["python3", "-P", "-s", "-m", "mesh", "--home", home, "daemon", "run"],
                                 env=dict(os.environ, PYTHONPATH=self.fake_mesh_root()),
                                 stdout=subprocess.PIPE, text=True)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
        self.addCleanup(cleanup)
        ready, _, _ = select.select([child.stdout], [], [], 5)
        self.assertTrue(ready, "child did not initialize within five seconds")
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child

    def child(self, ignore_term=False, argv=()):
        program = ("import signal, time; "
                   + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
                   + "print('ready', flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-c", program, *argv],
                                 stdout=subprocess.PIPE, text=True)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
        self.addCleanup(cleanup)
        ready, _, _ = select.select([child.stdout], [], [], 5)
        self.assertTrue(ready, "child did not initialize within five seconds")
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child

    def register(self, child, suite="dead-fixture"):
        with mock.patch.dict(os.environ, {"MESH_TEST_SUITE_ID": suite}):
            handle = reap.register_daemon(child.pid, self.home)
        self.assertIsNotNone(handle)
        return handle

    def run_reaper(self, *extra, use_env=False):
        output = io.StringIO()
        args = [] if use_env else ["--registry", str(self.registry)]
        with contextlib.redirect_stderr(output):
            self.assertEqual(reap.main([*args, *extra]), 0)
        return output.getvalue()


class TestRegistry(RegistryFixture):
    def test_atomic_registration_permissions_and_removal(self):
        child = self.child()
        original_replace = os.replace
        observed = []

        def replace(source, destination):
            self.assertFalse(Path(destination).exists())
            self.assertEqual(Path(source).parent, self.registry)
            observed.append(json.loads(Path(source).read_text()))
            original_replace(source, destination)

        with mock.patch.object(reap.os, "replace", side_effect=replace):
            handle = reap.register_daemon(child.pid, self.home)
        self.assertIsNotNone(handle)
        record = json.loads(Path(handle[0]).read_text())
        self.assertEqual(observed, [record])
        self.assertEqual(record["pid"], child.pid)
        self.assertEqual(record["starttime"], reap.process_stat(child.pid)[1])
        self.assertEqual(record["home"], os.path.realpath(self.home))
        self.assertEqual(record["suite_id"], f"adhoc-{os.getpid()}")
        self.assertEqual(record["suite_starttime"], reap.process_stat(os.getpid())[1])
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(handle[0]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.registry.iterdir()), [Path(handle[0])])
        reap.unregister_daemon(handle)
        self.assertFalse(Path(handle[0]).exists())

    def test_registration_rejects_symlink_nested_non_tmp_and_prefix_collision(self):
        child = self.child()
        link = self.home + "-link"
        os.symlink(self.home, link)
        self.addCleanup(os.unlink, link)
        nested = Path(self.home) / "mh-nested"
        nested.mkdir()
        outside = Path(self.directory.name) / "mh-outside"
        outside.mkdir()
        for home in (link, str(nested), str(outside), "/tmp/not-mh",
                     self.home + "/../" + Path(self.home).name, "/tmp/mh-"):
            with self.subTest(home=home), contextlib.redirect_stderr(io.StringIO()) as log:
                self.assertIsNone(reap.register_daemon(child.pid, home))
                self.assertIn("registration_failed", log.getvalue())
        self.assertFalse(self.registry.exists())
        self.assertIsNone(child.poll())

    def test_registry_write_and_removal_failures_are_best_effort(self):
        child = self.child()
        with mock.patch.object(reap.os, "replace", side_effect=OSError("fixture failure")), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertIsNone(reap.register_daemon(child.pid, self.home))
        self.assertIn("registration_failed", log.getvalue())
        self.assertEqual(list(self.registry.iterdir()), [])
        handle = self.register(child)
        with mock.patch.object(reap.os, "unlink", side_effect=OSError("fixture failure")), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            reap.unregister_daemon(handle)
        self.assertIn("unregistration_failed", log.getvalue())
        self.assertTrue(Path(handle[0]).exists())

    def test_registry_symlink_rejected(self):
        self.registry.symlink_to(self.directory.name, target_is_directory=True)
        with contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertIsNone(reap.register_daemon(self.child().pid, self.home))
        self.assertIn("registration_failed", log.getvalue())
        self.assertIn("registry_error", self.run_reaper())

    def test_home_gone_is_terminated_via_pidfd(self):
        child = self.child()
        path, _ = self.register(child)
        os.rmdir(self.home)
        with mock.patch.object(reap.signal, "pidfd_send_signal",
                               wraps=signal.pidfd_send_signal) as send:
            output = self.run_reaper(use_env=True)
        self.assertIn(f"terminated pid={child.pid}", output)
        self.assertIn("reason=home_gone", output)
        self.assertEqual([call.args[1] for call in send.call_args_list], [signal.SIGTERM])
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        self.assertFalse(Path(path).exists())

    def test_dead_suite_reaped_with_home_present(self):
        child = self.child()
        path, _ = self.register(child)
        # An unlocked lock file is dead, just like a missing lock file.
        (self.registry / "suite-dead-fixture.lock").touch()
        output = self.run_reaper()
        self.assertIn(f"terminated pid={child.pid}", output)
        self.assertIn("reason=dead_suite", output)
        self.assertTrue(Path(self.home).is_dir())
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        self.assertFalse(Path(path).exists())

    def test_live_suite_kept_including_own_suite_and_missing_home(self):
        child = self.child()
        path, _ = self.register(child, suite="live-fixture")
        lock = self.registry / "suite-live-fixture.lock"
        with lock.open("w") as stream, \
                mock.patch.dict(os.environ, {"MESH_TEST_SUITE_ID": "live-fixture"}), \
                mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for remove_home in (False, True):
                if remove_home:
                    os.rmdir(self.home)
                self.assertIn(f"skipped_live pid={child.pid}", self.run_reaper())
                self.assertIsNone(child.poll())
                self.assertTrue(Path(path).exists())
            send.assert_not_called()

    def test_adhoc_live_and_dead_owner_starttime(self):
        child = self.child()
        path, record = reap.register_daemon(child.pid, self.home)
        self.assertIn(f"skipped_live pid={child.pid}", self.run_reaper())
        self.assertIsNone(child.poll())
        # A recycled test-process PID is dead even though that PID exists.
        record["suite_starttime"] += 1
        Path(path).write_text(json.dumps(record))
        self.assertIn(f"terminated pid={child.pid}", self.run_reaper())
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        self.assertFalse(Path(path).exists())

    def test_adhoc_owner_exited(self):
        child, owner = self.child(), self.child()
        path, record = self.register(child)
        record.update(suite_id=f"adhoc-{owner.pid}",
                      suite_starttime=reap.process_stat(owner.pid)[1])
        Path(path).write_text(json.dumps(record))
        owner.terminate()
        owner.wait(timeout=5)
        self.assertIn(f"terminated pid={child.pid}", self.run_reaper())
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)

    def test_pid_reused_drops_record_without_signal(self):
        child = self.child()
        old_path, record = self.register(child)
        record["starttime"] += 1
        os.unlink(old_path)
        path = self.registry / reap.record_name(record)
        path.write_text(json.dumps(record))
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            self.assertIn(f"pid_reused pid={child.pid}", self.run_reaper())
            send.assert_not_called()
        self.assertIsNone(child.poll())
        self.assertFalse(Path(path).exists())

    def test_ignoring_sigterm_escalates_via_same_pidfd(self):
        child = self.child(ignore_term=True)
        path, _ = self.register(child)
        with mock.patch.object(reap.signal, "pidfd_send_signal",
                               wraps=signal.pidfd_send_signal) as send:
            output = self.run_reaper("--term-timeout", "0.05")
        self.assertIn(f"killed pid={child.pid}", output)
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(send.call_args_list[0].args[0], send.call_args_list[1].args[0])
        self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        self.assertFalse(Path(path).exists())

    def test_already_gone_record_removed(self):
        child = self.child()
        path, _ = self.register(child)
        child.terminate()
        child.wait(timeout=5)
        self.assertIn(f"gone pid={child.pid}", self.run_reaper())
        self.assertFalse(Path(path).exists())

    def test_malformed_records_logged_and_retained(self):
        self.registry.mkdir()
        for name, body in (("broken.json", "{"), ("123.json", "[]"),
                           ("124.json", '{"pid":124}')):
            (self.registry / name).write_text(body)
        with mock.patch.object(reap.os, "pidfd_open") as open_pid:
            output = self.run_reaper()
            open_pid.assert_not_called()
        self.assertEqual(output.count("malformed record="), 3)
        self.assertEqual(len(list(self.registry.iterdir())), 3)

    def test_unregistered_spoofed_argv_never_touched(self):
        children = [self.child(argv=("-m", "mesh", "--home", self.home, "daemon", "run")),
                    self.child(argv=(f"-m mesh --home {self.home} daemon run",))]
        with mock.patch.object(reap.os, "pidfd_open") as open_pid:
            self.assertIn("checked records=0", self.run_reaper())
            open_pid.assert_not_called()
        for child in children:
            self.assertIsNone(child.poll())

    def test_dry_run_retains_process_and_record(self):
        child = self.child()
        path, _ = self.register(child)
        self.assertIn(f"would_reap pid={child.pid}", self.run_reaper("--dry-run"))
        self.assertIsNone(child.poll())
        self.assertTrue(Path(path).exists())

    def test_signal_failure_is_not_logged_as_success_or_deleted(self):
        child = self.child()
        path, _ = self.register(child)
        with mock.patch.object(reap.signal, "pidfd_send_signal",
                               side_effect=PermissionError("fixture denial")):
            output = self.run_reaper()
        self.assertIn(f"error pid={child.pid}", output)
        self.assertNotIn("terminated", output)
        self.assertNotIn("killed", output)
        self.assertTrue(Path(path).exists())
        self.assertIsNone(child.poll())

    def test_harness_hooks_start_stop_and_crash_restart(self):
        # Only spawn our sleepers, but exercise the actual MeshHarness hook paths.
        harness = MeshHarness.__new__(MeshHarness)
        harness.home, harness.bin_mesh = self.home, "unused-fixture-command"
        harness.daemon_proc = harness._daemon_record = None
        harness.get_env = lambda: dict(os.environ)
        for crash in (False, True):
            child = self.child()
            with mock.patch("tests.helpers.subprocess.Popen", return_value=child):
                harness.start_daemon(wait_ready=False)
            path, _ = harness._daemon_record
            self.assertTrue(Path(path).exists())
            child.terminate()
            child.wait(timeout=5)
            if crash:
                harness.wait_daemon_exit()
            else:
                harness.stop_daemon()
            self.assertFalse(Path(path).exists())
            self.assertIsNone(harness._daemon_record)

    def cleanup_snippet(self):
        script = (ROOT / "scripts" / "test.sh").read_text()
        return script.split("# BEGIN SUITE CLEANUP", 1)[1].split("\n", 1)[1].split(
            "# END SUITE CLEANUP", 1)[0]

    def test_exit_trap_releases_lock_reaps_and_preserves_failing_exit(self):
        child = self.child()
        path, _ = self.register(child, suite="trap-fixture")
        env = dict(os.environ, REPO_ROOT=str(ROOT),
                   SUITE_LOCK=str(self.registry / "suite-trap-fixture.lock"))
        script = ("set -e\nSUITE_LOCK_HELD=1\nexec 9>\"$SUITE_LOCK\"\nflock -n 9\n"
                  + self.cleanup_snippet() + "\npython3 -c 'raise SystemExit(7)'\n"
                  + "echo should-not-run\n")
        result = subprocess.run(["/bin/sh", "-c", script], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertNotIn("should-not-run", result.stdout)
        self.assertIn(f"terminated pid={child.pid}", result.stderr)
        self.assertFalse(Path(path).exists())
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        # The lock was released (child reaped) and its file removed at exit.
        self.assertFalse(Path(env["SUITE_LOCK"]).exists())

    @staticmethod
    def ignored_on_entry(sig):
        """True if sig is ignored in this process (inherited by /bin/sh, where a
        signal ignored on entry cannot be trapped, so the trap test would hang)."""
        with open("/proc/self/status", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("SigIgn:"):
                    return bool(int(line.split()[1], 16) & (1 << (sig - 1)))
        return False

    def test_signal_traps_run_cleanup_under_dash(self):
        for sig, status in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(sig=sig):
                if self.ignored_on_entry(sig):
                    self.skipTest(f"{sig.name} is ignored on entry (SigIgn); /bin/sh "
                                  "cannot trap it - launch the suite with it at default")
                child = self.child()
                path, _ = self.register(child, suite="trap-fixture")
                env = dict(os.environ, REPO_ROOT=str(ROOT),
                           SUITE_LOCK=str(self.registry / "suite-trap-fixture.lock"))
                script = ("set -e\nSUITE_LOCK_HELD=1\nexec 9>\"$SUITE_LOCK\"\nflock -n 9\n"
                          + self.cleanup_snippet()
                          + "\necho armed\nwhile :; do sleep 0.05; done\n")
                # Own process group, so the fixture loop and its sleeps are always
                # killed below even if the trap never fires (no leftovers on failure).
                shell = subprocess.Popen(["/bin/sh", "-c", script], env=env, text=True,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         start_new_session=True)
                # Exit is observed through a pidfd WITHOUT reaping: the unreaped shell
                # (group leader) keeps its pgid reserved, so the killpg below can never
                # hit a reused group; only after it is the shell reaped (Quest r7d6 P3).
                shell_fd = os.pidfd_open(shell.pid)
                try:
                    ready, _, _ = select.select([shell.stdout], [], [], 10)
                    self.assertTrue(ready, "trap fixture did not arm within ten seconds")
                    self.assertEqual(shell.stdout.readline().strip(), "armed")
                    shell.send_signal(sig)
                    self.assertTrue(reap.wait_pidfd(shell_fd, 20), "trap fixture did not exit")
                finally:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(shell.pid, signal.SIGKILL)
                    os.close(shell_fd)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        shell.wait(timeout=5)
                    err = shell.stderr.read()
                    for stream in (shell.stdout, shell.stderr):
                        stream.close()
                self.assertEqual(shell.returncode, status, err)
                self.assertIn(f"terminated pid={child.pid}", err)
                self.assertFalse(Path(path).exists())
                self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)

    def test_exit_trap_preserves_status_when_cleanup_times_out(self):
        for status in (0, 7):
            script = ("set -e\nSUITE_LOCK_HELD=0\ntimeout() { return 124; }\n"
                      + self.cleanup_snippet() + f"\nexit {status}\n")
            result = subprocess.run(["/bin/sh", "-c", script],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, status, result.stderr)


class TestGenerationsAndEdges(RegistryFixture):
    """Item 2/3 regressions; inherits the isolated registry fixture only."""

    def test_record_name_is_pid_starttime_generation(self):
        child = self.child()
        path, record = self.register(child)
        self.assertEqual(Path(path).name, f"{child.pid}-{record['starttime']}.json")

    def test_stale_generation_cleanup_keeps_new_generation_of_same_pid(self):
        child = self.child()
        path, record = reap.register_daemon(child.pid, self.home)  # live adhoc owner
        stale = dict(record, starttime=record["starttime"] - 1)
        stale_path = self.registry / f"{child.pid}-{stale['starttime']}.json"
        stale_path.write_text(json.dumps(stale))
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            output = self.run_reaper()
            send.assert_not_called()
        self.assertIn(f"pid_reused pid={child.pid}", output)
        self.assertIn(f"skipped_live pid={child.pid}", output)
        self.assertFalse(stale_path.exists())
        self.assertTrue(Path(path).exists())
        self.assertIsNone(child.poll())

    def test_removal_never_deletes_a_replaced_record(self):
        child = self.child()
        path, record = self.register(child)
        Path(path).write_text(json.dumps(dict(record, suite_id="other-suite")))
        reap.unregister_daemon((path, record))
        reap.remove_record(path, record)
        self.assertEqual(json.loads(Path(path).read_text())["suite_id"], "other-suite")

    def test_legacy_pid_only_name_is_malformed(self):
        child = self.child()
        path, record = self.register(child)
        os.rename(path, self.registry / f"{child.pid}.json")
        with mock.patch.object(reap.os, "pidfd_open") as open_pid:
            self.assertIn("malformed record=", self.run_reaper())
            open_pid.assert_not_called()
        self.assertIsNone(child.poll())

    def test_pid_now_a_thread_is_dropped_as_reused(self):
        program = ("import threading, time; e = threading.Event(); "
                   "t = threading.Thread(target=e.wait, daemon=True); t.start(); "
                   "print(t.native_id, flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-c", program],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.wait(timeout=5), child.stdout.close()))
        tid = int(child.stdout.readline())
        self.assertNotEqual(tid, child.pid)
        self.registry.mkdir()
        record = {"pid": tid, "starttime": 1, "home": self.home, "suite_id": "dead-fixture"}
        path = self.registry / reap.record_name(record)
        path.write_text(json.dumps(record))
        real_open = os.pidfd_open

        def pidfd_open(pid, *a):
            if pid == tid:  # kernels with PIDFD_THREAD accept TIDs; pin the old behavior
                raise OSError(errno.EINVAL, "Invalid argument")
            return real_open(pid, *a)
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send, \
                mock.patch.object(reap.os, "pidfd_open", side_effect=pidfd_open):
            output = self.run_reaper()
            send.assert_not_called()
        self.assertIn(f"pid_reused pid={tid}", output)
        self.assertFalse(path.exists())
        self.assertIsNone(child.poll())

    def test_pidfd_open_einval_real_kernel(self):
        # Without mocks the outcome is kernel-specific: EINVAL (pre-6.9) or a
        # mismatched starttime -> pid_reused; sandboxes hiding /proc/<tid> -> gone.
        # Either way: never a signal, and the record is dropped.
        program = ("import threading, time; e = threading.Event(); "
                   "t = threading.Thread(target=e.wait, daemon=True); t.start(); "
                   "print(t.native_id, flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-c", program],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.wait(timeout=5), child.stdout.close()))
        tid = int(child.stdout.readline())
        self.registry.mkdir()
        record = {"pid": tid, "starttime": 1, "home": self.home, "suite_id": "dead-fixture"}
        path = self.registry / reap.record_name(record)
        path.write_text(json.dumps(record))
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            output = self.run_reaper()
            send.assert_not_called()
        self.assertRegex(output, rf"(pid_reused|gone) pid={tid} ")
        self.assertFalse(path.exists())
        self.assertIsNone(child.poll())

    def test_still_running_after_kill_keeps_record(self):
        child = self.child(ignore_term=True)
        path, _ = self.register(child)
        real_send = signal.pidfd_send_signal

        def send(fd, sig, *a):
            if sig != signal.SIGKILL:  # swallow KILL so the process survives
                real_send(fd, sig, *a)
        with mock.patch.object(reap.signal, "pidfd_send_signal", side_effect=send):
            output = self.run_reaper("--term-timeout", "0.05", "--kill-timeout", "0.05")
        self.assertIn(f"still_running pid={child.pid}", output)
        self.assertTrue(Path(path).exists())
        self.assertIsNone(child.poll())

    def test_stale_lock_pruning(self):
        self.registry.mkdir()
        old = time.time() - reap.STALE_LOCK_SECONDS - 60
        stale = self.registry / "suite-stale.lock"
        held = self.registry / "suite-held.lock"
        fresh = self.registry / "suite-fresh.lock"
        target = self.registry / "target"
        link = self.registry / "suite-link.lock"
        for p in (stale, held, fresh, target):
            p.touch()
        link.symlink_to(target)
        for p in (stale, held, target):
            os.utime(p, (old, old))
        os.utime(link, (old, old), follow_symlinks=False)
        with held.open("w") as stream:
            os.utime(held, (old, old))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            output = self.run_reaper()
            self.assertTrue(held.exists())
        self.assertIn("pruned_lock suite-stale.lock", output)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(link.is_symlink() and target.exists())
        # Dry runs never prune.
        os.utime(fresh, (old, old))
        self.run_reaper("--dry-run")
        self.assertTrue(fresh.exists())


class TestHarnessPidFileFallback(RegistryFixture):
    """Linear MAJOR: the meshd.pid fallback only signals our exact daemon via pidfd."""

    def harness_with(self, child):
        harness = MeshHarness.__new__(MeshHarness)
        harness.home, harness.bin_mesh = self.home, "unused-fixture-command"
        harness.daemon_proc = harness._daemon_record = None
        harness._daemon_identity = None
        harness.get_env = lambda: dict(os.environ)
        with mock.patch("tests.helpers.subprocess.Popen", return_value=child):
            harness.start_daemon(wait_ready=False)
        # Pretend the managed handle is gone (as after an orphaning restart).
        reap.unregister_daemon(harness._daemon_record)
        harness._daemon_record = harness.daemon_proc = None
        return harness

    def write_pidfile(self, pid):
        run = Path(self.home) / "run"
        run.mkdir(exist_ok=True)
        (run / "meshd.pid").write_text(f"{pid}\n")

    def test_identity_recorded_and_matching_daemon_stopped_via_pidfd(self):
        child = self.daemon_child(self.home)
        harness = self.harness_with(child)
        self.assertEqual(harness._daemon_identity,
                         (child.pid, reap.process_stat(child.pid)[1]))
        self.write_pidfile(child.pid)
        with mock.patch.object(reap.signal, "pidfd_send_signal",
                               wraps=signal.pidfd_send_signal) as send, \
                mock.patch.object(reap.os, "kill") as bare_kill, \
                mock.patch("tests.helpers.os.kill") as helper_kill:
            harness.stop_daemon()
            bare_kill.assert_not_called()
            helper_kill.assert_not_called()
        self.assertEqual([c.args[1] for c in send.call_args_list],
                         [signal.SIGTERM])
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)

    def test_pid_reuse_simulated_by_mismatched_starttime_is_skipped(self):
        child = self.daemon_child(self.home)
        harness = self.harness_with(child)
        pid, start = harness._daemon_identity
        harness._daemon_identity = (pid, start + 1)  # same number, different process
        self.write_pidfile(child.pid)
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send, \
                mock.patch("tests.helpers.os.kill") as helper_kill:
            harness.stop_daemon()
            self.assertEqual(harness.stop_pidfile_daemon(), "pid_reused")
            send.assert_not_called()
            helper_kill.assert_not_called()
        self.assertIsNone(child.poll())

    def test_unknown_pid_and_foreign_home_are_skipped(self):
        ours = self.daemon_child(self.home)
        harness = self.harness_with(ours)
        stranger = self.daemon_child(self.home)
        self.write_pidfile(stranger.pid)
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            self.assertEqual(harness.stop_pidfile_daemon(), "unknown_pid")
            send.assert_not_called()
        # Right pid and starttime, but the process is not running for this home.
        other_home = self.daemon_child("/tmp/mh-someone-else")
        harness2 = self.harness_with(other_home)
        self.write_pidfile(other_home.pid)
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            self.assertEqual(harness2.stop_pidfile_daemon(), "home_mismatch")
            send.assert_not_called()
        for child in (ours, stranger, other_home):
            self.assertIsNone(child.poll())

    def test_exited_daemon_pidfile_is_gone_and_no_identity_means_skip(self):
        child = self.daemon_child(self.home)
        harness = self.harness_with(child)
        self.write_pidfile(child.pid)
        child.kill()
        child.wait(timeout=5)
        self.assertEqual(harness.stop_pidfile_daemon(), "gone")
        harness._daemon_identity = None
        self.assertEqual(harness.stop_pidfile_daemon(), "unknown_pid")



class TestPidFileDaemonWithoutIdentity(RegistryFixture):
    """r7d3 F1 / r7d4: daemons from ``mesh daemon start`` (no Popen identity) are
    stopped via pidfd only if they run for exactly this (absolute) home and carry
    this harness's token in their environ."""

    TOKEN = "ab" * 16
    OTHER = "cd" * 16

    def write_pidfile(self, pid):
        run = Path(self.home) / "run"
        run.mkdir(exist_ok=True)
        (run / "meshd.pid").write_text(f"{pid}\n")

    def bare_harness(self, token=TOKEN):
        harness = MeshHarness.__new__(MeshHarness)
        harness.home = self.home
        harness.daemon_proc = harness._daemon_record = harness._daemon_identity = None
        harness._harness_token = token
        return harness

    def spawn(self, argv=(), env=None, cwd=None, token=TOKEN, raw=None):
        """Launcher-shaped fixture: ``python3 -P -s -m mesh daemon run <argv>``, or
        ``<sys.executable> <raw...>`` when raw (interpreter args) is given."""
        environment = dict(os.environ)
        environment.pop("MESH_HOME", None)
        environment.pop("MESH_TEST_HARNESS_TOKEN", None)
        environment["PYTHONPATH"] = self.fake_mesh_root()
        if token is not None:
            environment["MESH_TEST_HARNESS_TOKEN"] = token
        environment.update(env or {})
        if raw is None:
            raw = ("-P", "-s", "-m", "mesh", "daemon", "run", *argv)
        child = subprocess.Popen(["python3", *raw], env=environment, cwd=cwd,
                                 stdout=subprocess.PIPE, text=True)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
        self.addCleanup(cleanup)
        ready, _, _ = select.select([child.stdout], [], [], 5)
        self.assertTrue(ready, "child did not initialize within five seconds")
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child

    def assert_skipped(self, child, expected):
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send, \
                mock.patch.object(reap.os, "kill") as bare_kill:
            self.assertEqual(reap.stop_verified_token(child.pid, self.TOKEN, self.home),
                             expected)
            send.assert_not_called()
            bare_kill.assert_not_called()
        self.assertIsNone(child.poll())

    def test_prefix_home_never_matches_in_any_form(self):
        other = self.home + "-other"
        for kwargs in ({"argv": ("--home", other)}, {"argv": ("--home=" + other,)},
                       {"env": {"MESH_HOME": other}}):
            child = self.spawn(**kwargs)
            self.assertFalse(reap.home_matches(child.pid, self.home), kwargs)
            self.assert_skipped(child, "home_mismatch")

    def test_exact_and_canonical_home_forms_match(self):
        for kwargs in ({"argv": ("--home", self.home)}, {"argv": ("--home", self.home + "/.")},
                       {"argv": ("--home=" + self.home,)}, {"env": {"MESH_HOME": self.home}}):
            child = self.spawn(**kwargs)
            self.assertTrue(reap.home_matches(child.pid, self.home), kwargs)

    def test_relative_home_never_matches_even_if_it_resolves_here(self):
        # Linear r7d4 MAJOR: the target runs in another cwd where "./<name>" is a
        # different directory; resolving it in our cwd (/tmp) would name our home.
        parent, name = os.path.split(self.home)
        elsewhere = tempfile.TemporaryDirectory(prefix="mesh-reap-cwd-")
        self.addCleanup(elsewhere.cleanup)
        os.mkdir(os.path.join(elsewhere.name, name))
        previous = os.getcwd()
        os.chdir(parent)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(os.path.realpath(name), os.path.realpath(self.home))
        for kwargs in ({"argv": ("--home", name)}, {"argv": ("--home=" + name,)},
                       {"env": {"MESH_HOME": name}}, {"env": {"MESH_HOME": "./" + name}}):
            child = self.spawn(cwd=elsewhere.name, **kwargs)
            self.assertFalse(reap.home_matches(child.pid, self.home), kwargs)
            self.assert_skipped(child, "home_mismatch")

    def test_explicit_home_overrides_inherited_mesh_home(self):
        # Linear r7d5 MAJOR: harness A runs `daemon start --home B`; the daemon
        # inherits MESH_HOME=A and token A but serves B, so A must not stop it.
        other = self.home + "-b"
        child = self.spawn(argv=("--home", other), env={"MESH_HOME": self.home})
        self.assertFalse(reap.home_matches(child.pid, self.home))
        self.assertTrue(reap.home_matches(child.pid, other))
        self.assert_skipped(child, "home_mismatch")
        child = self.spawn(argv=("--home=" + other,), env={"MESH_HOME": self.home})
        self.assert_skipped(child, "home_mismatch")

    def test_relative_or_ambiguous_explicit_home_never_falls_back_to_mesh_home(self):
        name = os.path.basename(self.home)
        for argv in (("--home", name), ("--home=./" + name,),
                     ("--home", self.home, "--home", self.home + "-b"),
                     ("--hom", self.home), ("--ho=" + self.home,), ("--home",)):
            child = self.spawn(argv=argv, env={"MESH_HOME": self.home}, cwd="/")
            self.assertFalse(reap.home_matches(child.pid, self.home), argv)
            self.assert_skipped(child, "home_mismatch")
        # Repeating the same home is not ambiguous.
        child = self.spawn(argv=("--home", self.home, "--home=" + self.home + "/."),
                           env={"MESH_HOME": "/tmp/elsewhere"})
        self.assertTrue(reap.home_matches(child.pid, self.home))

    def test_effective_home_table(self):
        cases = [([b"python3", b"-P", b"-s", b"-m", b"mesh", *args, b""], env, want)
                 for args, env, want in (
                     ([b"--home", b"/tmp", b"daemon", b"run"], [b"MESH_HOME=/var"], b"/tmp"),
                     ([b"--home=/tmp", b"daemon", b"run"], [], b"/tmp"),
                     ([b"daemon", b"run"], [b"MESH_HOME=/tmp"], b"/tmp"),
                     ([b"daemon", b"run"], [b"MESH_HOME="], None),
                     ([b"daemon", b"run"], [], None),
                     ([b"daemon", b"run"], [b"MESH_HOME=/tmp", b"MESH_HOME=/var"], None),
                     ([b"--home", b"rel", b"daemon", b"run"], [b"MESH_HOME=/tmp"], None),
                     ([b"--home", b"/tmp", b"--home", b"/var", b"daemon", b"run"], [], None),
                     ([b"--hom", b"/tmp", b"daemon", b"run"], [b"MESH_HOME=/tmp"], None),
                     ([b"daemon", b"run", b"--home"], [b"MESH_HOME=/tmp"], None),
                 )]
        for argv, environ, expected in cases:
            self.assertEqual(reap.effective_home(argv, environ), expected, (argv, environ))

    def test_launcher_shape_table(self):
        # Linear r7d6 MAJOR: only `<python> [value-less flags] -m mesh` + mesh args
        # `daemon run` (plus --home) is ever a daemon; anything else is no match.
        env = [b"MESH_HOME=/var"]
        match = [
            [b"python3", b"-P", b"-s", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run", b""],
            [b"/usr/bin/python3.11", b"-m", b"mesh", b"daemon", b"run", b"--home=/tmp"],
            [b"python", b"-u", b"-E", b"-I", b"-B", b"-m", b"mesh", b"--home", b"/tmp",
             b"daemon", b"run"],
            # --home is accepted on the root parser and on every subparser.
            [b"python3", b"-P", b"-s", b"-m", b"mesh", b"daemon", b"run", b"--home", b"/tmp"],
            [b"python3", b"-P", b"-s", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run",
             b"--home=/tmp/."],
        ]
        for argv in match:
            self.assertEqual(reap.effective_home(argv, env), b"/tmp", argv)
        self.assertEqual(reap.effective_home(
            [b"python3", b"-P", b"-s", b"-m", b"mesh", b"daemon", b"run"], env), b"/var")
        no_match = [
            [b"python3", b"-X", b"--home=/tmp", b"-m", b"mesh", b"daemon", b"run"],
            [b"python3", b"-X", b"dev", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-W", b"--home=/tmp", b"-m", b"mesh", b"daemon", b"run"],
            # -X/-W take the next element as their value: here "-m" is that value and
            # "mesh" would be run as a script, so a value-taking flag is never skipped.
            [b"python3", b"-X", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-W", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-W", b"ignore", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-c", b"pass", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-O", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-Ps", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-q", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-P", b"-s", b"--home=/tmp", b"daemon", b"run"],
            [b"python3", b"-mmesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-m", b"mesh.runwrap", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-m", b"meshx", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-m"],
            [b"python3", b"/x/mesh/__main__.py", b"--home", b"/tmp", b"daemon", b"run"],
            [b"sh", b"-P", b"-s", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"pythonw", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3x", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"bin/mesh", b"--home", b"/tmp", b"daemon", b"run"],
            [b"python3", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"start"],
            [b"python3", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"status"],
            [b"python3", b"-m", b"mesh", b"--home", b"/tmp", b"daemon"],
            [b"python3", b"-m", b"mesh", b"--home", b"/tmp", b"task", b"run"],
            [b"python3", b"-m", b"mesh", b"--home", b"/tmp", b"run", b"daemon"],
            [b"python3", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run", b"--user"],
            [b"python3", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run", b"--", b"x"],
            [b"python3", b"-P", b"-s", b"-m", b"mesh", b"-q", b"daemon", b"run", b"--home", b"/tmp"],
            [b"python3", b"-P", b"-s", b"-m", b"mesh", b"daemon", b"run", b"extra"],
            [b"python3", b"-m", b"mesh", b"--home", b"daemon", b"run"],
            [b"python3", b"-m", b"mesh"],
            # root vs sub-level --home disagree: which one argparse keeps is not ours to guess.
            [b"python3", b"-P", b"-s", b"-m", b"mesh", b"--home", b"/tmp", b"daemon", b"run",
             b"--home", b"/var"],
            [b"python3", b"-P", b"-s", b"-m", b"mesh", b"daemon", b"run", b"--home=/var",
             b"--home", b"/tmp"],
            [b""],
            [],
        ]
        for argv in no_match:
            self.assertIsNone(reap.effective_home(argv, env), argv)

    def test_interpreter_option_value_is_not_a_mesh_home(self):
        # The r7d6 repro: `MESH_HOME=B python3 -X --home=A -m mesh daemon run` serves B.
        other = self.home + "-b"
        child = self.spawn(raw=("-P", "-X", "--home=" + self.home, "-m", "mesh", "daemon", "run"),
                           env={"MESH_HOME": other})
        self.assertFalse(reap.home_matches(child.pid, self.home))
        self.assert_skipped(child, "home_mismatch")
        for raw in (("-P", "-W", "--home=" + self.home, "-m", "mesh", "daemon", "run"),
                    ("-P", "-s", "-m", "mesh", "--home", self.home, "daemon", "status")):
            child = self.spawn(raw=raw)
            self.assertFalse(reap.home_matches(child.pid, self.home), raw)
            self.assert_skipped(child, "home_mismatch")
        child = self.spawn(argv=("--home", self.home))
        self.assertTrue(reap.home_matches(child.pid, self.home))

    def test_matching_home_without_or_with_wrong_token_is_skipped(self):
        for token in (None, self.OTHER, self.TOKEN[:-1], self.TOKEN + "ff"):
            child = self.spawn(argv=("--home", self.home), token=token)
            self.assert_skipped(child, "token_mismatch")
        # Only the exact variable name counts, not one ending in it.
        child = self.spawn(argv=("--home", self.home), token=None,
                           env={"X_MESH_TEST_HARNESS_TOKEN": self.TOKEN})
        self.assert_skipped(child, "token_mismatch")
        # The token in argv instead of environ is not a credential (and the extra
        # mesh arg already makes it not a `daemon run` argv: r7d6).
        child = self.spawn(argv=("--home", self.home,
                                 "MESH_TEST_HARNESS_TOKEN=" + self.TOKEN), token=None)
        self.assertFalse(reap.token_matches(child.pid, self.TOKEN))
        self.assert_skipped(child, "home_mismatch")

    def test_zombie_is_gone_without_signal(self):
        child = subprocess.Popen([sys.executable, "-c", "pass", "--home", self.home],
                                 env=dict(os.environ, MESH_TEST_HARNESS_TOKEN=self.TOKEN))
        self.addCleanup(child.wait)
        deadline = time.time() + 5
        while reap.process_stat(child.pid)[0] != "Z" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(reap.process_stat(child.pid)[0], "Z")
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            self.assertEqual(reap.stop_verified_token(child.pid, self.TOKEN, self.home), "gone")
            send.assert_not_called()

    def test_invalid_home_or_token_refused(self):
        child = self.spawn(argv=("--home", "/tmp/nested/mh-x"))
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            self.assertEqual(reap.stop_verified_token(child.pid, self.TOKEN, "/tmp/nested/mh-x"),
                             "unknown_pid")
            for token in ("", None, "XYZ", self.TOKEN.upper(), self.TOKEN + "0"):
                self.assertEqual(reap.stop_verified_token(child.pid, token, self.home),
                                 "unknown_pid")
            send.assert_not_called()
        self.assertEqual(self.bare_harness(token=None).stop_pidfile_daemon(), None)
        self.write_pidfile(child.pid)
        self.assertEqual(self.bare_harness(token=None).stop_pidfile_daemon(), "unknown_pid")

    def test_harness_stops_unspawned_daemon_with_token_via_pidfd(self):
        harness = self.bare_harness()
        child = self.spawn(argv=("--home", self.home))
        self.write_pidfile(child.pid)
        with mock.patch.object(reap.signal, "pidfd_send_signal",
                               wraps=signal.pidfd_send_signal) as send, \
                mock.patch.object(reap.os, "kill") as bare_kill, \
                mock.patch("tests.helpers.os.kill") as helper_kill:
            self.assertEqual(harness.stop_pidfile_daemon(), "terminated")
            bare_kill.assert_not_called()
            helper_kill.assert_not_called()
        self.assertEqual([c.args[1] for c in send.call_args_list], [signal.SIGTERM])
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)

    def test_harness_skips_untokened_or_foreign_unspawned_process(self):
        harness = self.bare_harness()
        untokened = self.spawn(argv=("--home", self.home), token=None)
        self.write_pidfile(untokened.pid)
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            self.assertEqual(harness.stop_pidfile_daemon(), "token_mismatch")
            send.assert_not_called()
        foreign = self.spawn(argv=("--home", self.home + "-other"))
        self.write_pidfile(foreign.pid)
        with mock.patch.object(reap.signal, "pidfd_send_signal") as send:
            self.assertEqual(harness.stop_pidfile_daemon(), "home_mismatch")
            send.assert_not_called()
        for child in (untokened, foreign):
            self.assertIsNone(child.poll())

    def test_real_harness_env_carries_its_token(self):
        harness = MeshHarness.__new__(MeshHarness)
        harness.home, harness.use_shims, harness.extra_path = self.home, True, None
        harness.repo_root = str(ROOT)
        harness.tripwire_file, harness.suite_tripwire_file = "/dev/null", None
        harness._harness_token = "ab" * 16
        self.assertEqual(harness.get_env()["MESH_TEST_HARNESS_TOKEN"], "ab" * 16)

    def test_get_env_drops_or_replaces_stale_inherited_token(self):
        harness = MeshHarness.__new__(MeshHarness)
        harness.home, harness.use_shims, harness.extra_path = self.home, True, None
        harness.repo_root = str(ROOT)
        harness.tripwire_file, harness.suite_tripwire_file = "/dev/null", None
        with mock.patch.dict(os.environ, {"MESH_TEST_HARNESS_TOKEN": "ef" * 16}):
            harness._harness_token = None
            self.assertNotIn("MESH_TEST_HARNESS_TOKEN", harness.get_env())
            harness._harness_token = ""
            self.assertNotIn("MESH_TEST_HARNESS_TOKEN", harness.get_env())
            harness._harness_token = "ab" * 16
            self.assertEqual(harness.get_env()["MESH_TEST_HARNESS_TOKEN"], "ab" * 16)
            self.assertEqual(os.environ["MESH_TEST_HARNESS_TOKEN"], "ef" * 16)


if __name__ == "__main__":
    unittest.main()
