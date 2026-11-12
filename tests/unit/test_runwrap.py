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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET_TOKEN = "mesh_rt_" + "Q" * 43
FAKE_KEY = "sk-acc0123456789abcdef"


class TestRunwrap(unittest.TestCase):
    def setUp(self) -> None:
        self.run_dir = tempfile.mkdtemp(prefix="mh-rw-", dir="/tmp")
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = REPO_ROOT
        self.env["MESH_TOKEN"] = SECRET_TOKEN
        self.env["FAKE_SECRET_API_KEY"] = FAKE_KEY

    def tearDown(self) -> None:
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def _write_spec(self, script: str, **extra: object) -> None:
        spec = {
            "argv": [sys.executable, "-c", script],
            "cwd": self.run_dir,
            "stdin": "devnull",
            "redact_env_names": ["FAKE_SECRET_API_KEY"],
        }
        spec.update(extra)
        with open(os.path.join(self.run_dir, "spec.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f)

    def _run_wrapper(self, timeout: float = 30.0) -> int:
        proc = subprocess.run(
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", self.run_dir],
            cwd=REPO_ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode

    def _read(self, name: str) -> str:
        with open(os.path.join(self.run_dir, name), "r", encoding="utf-8") as f:
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
            mode = stat.S_IMODE(os.stat(os.path.join(self.run_dir, name)).st_mode)
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

    def test_sigterm_forwarded_and_exit_json_written(self) -> None:
        script = (
            "import signal, sys, time\n"
            "def on_term(signum, frame):\n"
            "    print('got sigterm')\n"
            "    sys.stdout.flush()\n"
            "    sys.exit(7)\n"
            "signal.signal(signal.SIGTERM, on_term)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        self._write_spec(script)
        proc = subprocess.Popen(
            [sys.executable, "-P", "-s", "-m", "mesh.runwrap", self.run_dir],
            cwd=REPO_ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wrapper_path = os.path.join(self.run_dir, "wrapper.json")
            stdout_path = os.path.join(self.run_dir, "stdout.log")
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if os.path.exists(wrapper_path) and os.path.exists(stdout_path):
                    if "ready" in self._read("stdout.log"):
                        break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(wrapper_path), "wrapper.json must appear")
            self.assertIn("ready", self._read("stdout.log"), "child must install its SIGTERM handler")
            with open(wrapper_path, "r", encoding="utf-8") as f:
                wrapper = json.load(f)
            os.kill(int(wrapper["pid"]), signal.SIGTERM)
            proc.wait(timeout=15.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)

        exit_path = os.path.join(self.run_dir, "exit.json")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not os.path.exists(exit_path):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(exit_path), "exit.json must be written after SIGTERM")
        exit_info = json.loads(self._read("exit.json"))
        self.assertEqual(exit_info["exit_code"], 7)
        self.assertIn("got sigterm", self._read("stdout.log"))


if __name__ == "__main__":
    unittest.main()
