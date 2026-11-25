"""Unit tests for mesh/runwrap.py per plan §11.2.

These exercise the real wrapper process (``python3 -P -s -m mesh.runwrap``): the
spec file, the atomic ``wrapper.json``, redacted 0600 logs, ``activity``, and the
atomic ``exit.json``. They support C8 (no secrets in spec/argv) and the H8/H11
exit paths but are not themselves acceptance-checklist items.
"""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from unittest import mock

from mesh import runwrap
from mesh.core import proc as core_proc
from mesh.platform import perms, proc as plat_proc
from mesh.platform.paths import temp_root

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET_TOKEN = "mesh_rt_" + "Q" * 43
FAKE_KEY = "sk-acc0123456789abcdef"


class TestRunwrap(unittest.TestCase):
    def setUp(self) -> None:
        self.run_dir = tempfile.mkdtemp(prefix="mh-rw-", dir=temp_root())
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = REPO_ROOT
        self.env["MESH_TOKEN"] = SECRET_TOKEN
        self.env["FAKE_SECRET_API_KEY"] = FAKE_KEY

    def tearDown(self) -> None:
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def _write_spec(self, script: str, run_dir: str = "", **extra: object) -> None:
        target_dir = run_dir or self.run_dir
        spec = {
            "argv": [sys.executable, "-c", script],
            "cwd": target_dir,
            "stdin": "devnull",
            "redact_env_names": ["FAKE_SECRET_API_KEY"],
        }
        spec.update(extra)
        with open(os.path.join(target_dir, "spec.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f)

    def _run_wrapper(self, timeout: float = 30.0, run_dir: str = "") -> int:
        target_dir = run_dir or self.run_dir
        proc = subprocess.run(
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", target_dir],
            cwd=REPO_ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode

    def _read(self, name: str, run_dir: str = "") -> str:
        target_dir = run_dir or self.run_dir
        with open(os.path.join(target_dir, name), "r", encoding="utf-8") as f:
            return f.read()

    def test_wrapper_json_logs_and_exit_json(self) -> None:
        script = (
            "import os, sys\n"
            "print('token ' + os.environ['MESH_TOKEN'])\n"
            "print('key ' + os.environ['FAKE_SECRET_API_KEY'])\n"
            "sys.stderr.write('boom ' + os.environ['FAKE_SECRET_API_KEY'] + '\\n')\n"
            "sys.stderr.flush()\n"
            "sys.exit(3)\n"
        )
        self._write_spec(script)
        self.assertEqual(self._run_wrapper(), 0)

        wrapper = json.loads(self._read("wrapper.json"))
        self.assertGreater(int(wrapper["pid"]), 0)
        self.assertGreater(int(wrapper["starttime"]), 0)

        exit_info = json.loads(self._read("exit.json"))
        self.assertEqual(exit_info["exit_code"], 3)
        self.assertIsNone(exit_info["signal"])
        self.assertTrue(exit_info["ended_at"])

        stdout = self._read("stdout.log")
        stderr = self._read("stderr.log")
        self.assertIn("[REDACTED", stdout)
        self.assertIn("[REDACTED", stderr)
        self.assertNotIn(SECRET_TOKEN, stdout)
        self.assertNotIn(FAKE_KEY, stdout + stderr)
        self.assertIn("boom", stderr)

        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "activity")))

        for name in ("wrapper.json", "exit.json", "stdout.log", "stderr.log"):
            fpath = os.path.join(self.run_dir, name)
            if sys.platform == "win32":
                perms.verify_private_owner_and_perms(fpath, is_dir=False, label=name)
            else:
                mode = stat.S_IMODE(os.stat(fpath).st_mode)
                self.assertEqual(mode, 0o600, f"{name} must be 0600, got {oct(mode)}")

        # spec.json itself never carries the values
        self.assertNotIn(SECRET_TOKEN, self._read("spec.json"))
        self.assertNotIn(FAKE_KEY, self._read("spec.json"))

    def test_log_cap_writes_marker_and_keeps_draining(self) -> None:
        script = "import sys; sys.stdout.write('x' * 5000); sys.stdout.flush()\n"
        self._write_spec(script, log_cap_bytes=200)
        self.assertEqual(self._run_wrapper(), 0)
        stdout = self._read("stdout.log")
        self.assertIn("[runwrap] log cap reached", stdout)
        self.assertLess(len(stdout.encode("utf-8")), 1000)
        # exit.json is still written after the cap was hit
        self.assertEqual(json.loads(self._read("exit.json"))["exit_code"], 0)

    @staticmethod
    def _sigterm_child_script() -> str:
        sig_expr = "getattr(signal, 'SIGBREAK', signal.SIGTERM)" if sys.platform == "win32" else "signal.SIGTERM"
        # CPython's time.sleep() on Windows only wakes on _PyOS_SigintEvent() (SIGINT), not
        # SIGBREAK, so poll in short slices so the SIGBREAK Python handler runs promptly.
        wait_stmt = (
            "deadline = time.monotonic() + 30.0\n"
            "while time.monotonic() < deadline:\n"
            "    time.sleep(0.05)\n"
            if sys.platform == "win32"
            else "time.sleep(30)\n"
        )
        return (
            "import signal, sys, time\n"
            "def on_term(signum, frame):\n"
            "    print('got sigterm')\n"
            "    sys.stdout.flush()\n"
            "    sys.exit(7)\n"
            f"signal.signal({sig_expr}, on_term)\n"
            "print('ready', flush=True)\n"
            + wait_stmt
        )

    def test_sigterm_forwarded_and_exit_json_written(self) -> None:
        script = self._sigterm_child_script()
        if sys.platform == "win32":
            home = os.path.join(self.run_dir, "home")
            run_id = "run_rw_sig"
            sig_dir = os.path.join(home, "runs", run_id)
            os.makedirs(sig_dir, exist_ok=True)
            self._write_spec(script, run_dir=sig_dir)
        else:
            home = ""
            run_id = ""
            sig_dir = self.run_dir
            self._write_spec(script)

        proc = subprocess.Popen(
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", sig_dir],
            cwd=REPO_ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wrapper_path = os.path.join(sig_dir, "wrapper.json")
            stdout_path = os.path.join(sig_dir, "stdout.log")
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if os.path.exists(wrapper_path) and os.path.exists(stdout_path):
                    if "ready" in self._read("stdout.log", run_dir=sig_dir):
                        break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(wrapper_path), "wrapper.json must appear")
            self.assertIn("ready", self._read("stdout.log", run_dir=sig_dir), "child must install its signal handler")
            with open(wrapper_path, "r", encoding="utf-8") as f:
                wrapper = json.load(f)
            if sys.platform == "win32":
                self.assertTrue(
                    plat_proc.signal_if_same(
                        int(wrapper["pid"]),
                        int(wrapper["starttime"]),
                        plat_proc.SIGTERM,
                        run_id=run_id,
                        home=home,
                    )
                )
            else:
                os.kill(int(wrapper["pid"]), signal.SIGTERM)
            proc.wait(timeout=15.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)

        exit_path = os.path.join(sig_dir, "exit.json")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not os.path.exists(exit_path):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(exit_path), "exit.json must be written after stop signal")
        exit_info = json.loads(self._read("exit.json", run_dir=sig_dir))
        self.assertEqual(exit_info["exit_code"], 7)
        self.assertIn("got sigterm", self._read("stdout.log", run_dir=sig_dir))

    def test_partial_line_updates_activity_and_flushes(self) -> None:
        script = "import sys; sys.stdout.write('partial_no_newline'); sys.stdout.flush()\n"
        self._write_spec(script)
        self.assertEqual(self._run_wrapper(), 0)
        self.assertEqual(self._read("stdout.log"), "partial_no_newline")
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "activity")))

    def test_escaped_pipe_holder_flushes_and_exits_after_grace(self) -> None:
        pid_file = os.path.join(self.run_dir, "holder.pid")
        script = (
            "import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'])\n"
            f"with open({pid_file!r}, 'w', encoding='utf-8') as f:\n"
            "    f.write(str(p.pid))\n"
            "sys.stdout.write('child_done\\npartial-tail')\n"
            "sys.stdout.flush()\n"
        )
        self._write_spec(script)

        def _cleanup_holder() -> None:
            if os.path.exists(pid_file):
                try:
                    with open(pid_file, "r", encoding="utf-8") as f:
                        pid = int(f.read().strip())
                except (OSError, ValueError):
                    return
                if sys.platform == "win32":
                    info = core_proc.get_proc_stat(pid)
                    if info is not None and int(info.get("starttime", 0)) > 0:
                        plat_proc.kill_if_same(pid, int(info["starttime"]))
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass

        self.addCleanup(_cleanup_holder)
        saved_sigs = {}
        for s_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, s_name, None)
            if sig is not None:
                saved_sigs[sig] = signal.getsignal(sig)
        t0 = time.monotonic()
        try:
            with mock.patch.dict(os.environ, self.env), mock.patch.object(runwrap, "PIPE_DRAIN_GRACE_S", 0.3):
                rc = runwrap.run(self.run_dir)
        finally:
            for sig, handler in saved_sigs.items():
                signal.signal(sig, handler)
        elapsed = time.monotonic() - t0
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, 4.0)
        exit_info = json.loads(self._read("exit.json"))
        self.assertEqual(exit_info["exit_code"], 0)
        self.assertNotIn("capture_error", exit_info)
        stdout = self._read("stdout.log")
        self.assertIn("child_done", stdout)
        self.assertIn("partial-tail", stdout)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only Named Run Job retention test")
    def test_win32_records_job_members_at_exit(self) -> None:
        home = os.path.join(self.run_dir, "home")
        run_id = "run_rw_job"
        job_dir = os.path.join(home, "runs", run_id)
        os.makedirs(job_dir, exist_ok=True)
        holder_pid_file = os.path.join(job_dir, "sleeper.pid")
        run_job = plat_proc.create_run_job(home, run_id, kill_on_close=False)

        def _cleanup_job() -> None:
            try:
                plat_proc.terminate_job(run_job, 137)
            finally:
                run_job.close()

        self.addCleanup(_cleanup_job)
        script = (
            "import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({holder_pid_file!r}, 'w').write(str(p.pid))\n"
            "print('spawned', flush=True)\n"
        )
        self._write_spec(script, run_dir=job_dir)
        proc = plat_proc.spawn_in_job(
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", job_dir],
            run_job,
            cwd=REPO_ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=15.0)
        exit_info = json.loads(self._read("exit.json", run_dir=job_dir))
        self.assertEqual(exit_info["exit_code"], 0)
        with open(holder_pid_file, "r", encoding="utf-8") as pf:
            holder_pid = int(pf.read().strip())
        members = exit_info.get("job_members_at_exit", [])
        self.assertTrue(any(int(m["pid"]) == holder_pid and int(m["starttime"]) > 0 for m in members), members)

    def _spawn_and_verify_win32_sigterm(self, run_id: str, creationflags: int) -> None:
        home = os.path.join(self.run_dir, f"home_{run_id}")
        sig_dir = os.path.join(home, "runs", run_id)
        os.makedirs(sig_dir, exist_ok=True)
        script = self._sigterm_child_script()
        self._write_spec(script, run_dir=sig_dir)
        proc = subprocess.Popen(
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", sig_dir],
            cwd=REPO_ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        try:
            wrapper_path = os.path.join(sig_dir, "wrapper.json")
            stdout_path = os.path.join(sig_dir, "stdout.log")
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if os.path.exists(wrapper_path) and os.path.exists(stdout_path):
                    if "ready" in self._read("stdout.log", run_dir=sig_dir):
                        break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(wrapper_path), "wrapper.json must appear")
            self.assertIn("ready", self._read("stdout.log", run_dir=sig_dir))
            with open(wrapper_path, "r", encoding="utf-8") as f:
                wrapper = json.load(f)
            self.assertTrue(
                plat_proc.signal_if_same(
                    int(wrapper["pid"]),
                    int(wrapper["starttime"]),
                    plat_proc.SIGTERM,
                    run_id=run_id,
                    home=home,
                )
            )
            self.assertEqual(proc.wait(timeout=15.0), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)
        exit_info = json.loads(self._read("exit.json", run_dir=sig_dir))
        self.assertEqual(exit_info["exit_code"], 7)
        self.assertIn("got sigterm", self._read("stdout.log", run_dir=sig_dir))

    @unittest.skipUnless(sys.platform == "win32", "Windows-only console-less runwrap CTRL_BREAK test")
    def test_win32_sigterm_forwarded_when_runwrap_lacks_console(self) -> None:
        self._spawn_and_verify_win32_sigterm("run_detached", subprocess.DETACHED_PROCESS)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only CREATE_NO_WINDOW runwrap CTRL_BREAK test")
    def test_win32_sigterm_forwarded_under_create_no_window(self) -> None:
        self._spawn_and_verify_win32_sigterm(
            "run_nowin", subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    def test_ensure_console_win_allocates_and_restores_std_handles(self) -> None:
        from mesh.platform import _win32_ctypes as w32

        class _FakeK32:
            def __init__(self, count: int, alloc_ok: int = 1, hwnd: int = 0xDEAD) -> None:
                self.count = count
                self.alloc_ok = alloc_ok
                self.hwnd = hwnd
                self.alloc_calls = 0
                self.restored: list = []
                self.std = {
                    w32.STD_INPUT_HANDLE: 0x101,
                    w32.STD_OUTPUT_HANDLE: 0x102,
                    w32.STD_ERROR_HANDLE: -1,  # INVALID_HANDLE_VALUE must not be restored
                }

            def GetConsoleProcessList(self, buf: object, cap: int) -> int:
                return self.count

            def GetStdHandle(self, std_id: int) -> int:
                return self.std[std_id]

            def AllocConsole(self) -> int:
                self.alloc_calls += 1
                return self.alloc_ok

            def SetStdHandle(self, std_id: int, handle: int) -> int:
                self.restored.append((std_id, handle))
                return 1

            def GetConsoleWindow(self) -> int:
                return self.hwnd

        class _FakeU32:
            def __init__(self) -> None:
                self.shown: list = []

            def ShowWindow(self, hwnd: int, cmd: int) -> int:
                self.shown.append((hwnd, cmd))
                return 1

        k32_missing = _FakeK32(count=0)
        u32 = _FakeU32()
        self.assertTrue(runwrap._ensure_console_win(k32=k32_missing, user32=u32))
        self.assertEqual(k32_missing.alloc_calls, 1)
        self.assertEqual(
            k32_missing.restored,
            [(w32.STD_INPUT_HANDLE, 0x101), (w32.STD_OUTPUT_HANDLE, 0x102)],
        )
        self.assertEqual(u32.shown, [(0xDEAD, w32.SW_HIDE)])

        k32_present = _FakeK32(count=1)
        self.assertFalse(runwrap._ensure_console_win(k32=k32_present, user32=u32))
        self.assertEqual(k32_present.alloc_calls, 0)

        k32_fail = _FakeK32(count=0, alloc_ok=0)
        self.assertFalse(runwrap._ensure_console_win(k32=k32_fail, user32=u32))
        self.assertEqual(k32_fail.alloc_calls, 1)
        self.assertEqual(k32_fail.restored, [])

    def test_win32_unsafe_cmd_arg_writes_exit_json_127(self) -> None:
        from mesh.platform import pipe_pump

        closed_readers: list = []

        class _StubReader:
            def close(self) -> None:
                closed_readers.append(self)

        pipe_pairs = []

        def _fake_pair() -> tuple:
            r_fd, w_fd = os.pipe()
            os.close(r_fd)
            rdr = _StubReader()
            pipe_pairs.append((rdr, w_fd))
            return rdr, w_fd

        cmd_path = os.path.join(self.run_dir, "agent.cmd")
        with open(cmd_path, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
        spec = {
            "argv": [cmd_path, "--pattern=%MESH_TOKEN%"],
            "cwd": self.run_dir,
            "stdin": "devnull",
        }
        with mock.patch.object(runwrap, "_ensure_console_win", return_value=False) as ensure_mock, \
                mock.patch.object(plat_proc, "open_run_job", return_value=None), \
                mock.patch.object(plat_proc, "create_stop_event", return_value=None), \
                mock.patch.object(pipe_pump, "create_pipe_pair", side_effect=_fake_pair):
            rc = runwrap._run_win32(
                self.run_dir,
                spec,
                dict(self.env),
                [],
                os.path.join(self.run_dir, "activity"),
                None,
                "devnull",
                runwrap.DEFAULT_LOG_CAP_BYTES,
            )
        self.assertEqual(rc, 0)
        ensure_mock.assert_called_once()
        self.assertEqual(len(closed_readers), 2)
        for _rdr, w_fd in pipe_pairs:
            with self.assertRaises(OSError):
                os.fstat(w_fd)
        exit_info = json.loads(self._read("exit.json"))
        self.assertEqual(exit_info["exit_code"], 127)
        self.assertEqual(exit_info["error"], "ValueError")


if __name__ == "__main__":
    unittest.main()
