"""Unit tests for the fake agent behaviors that M1c termination tests rely on (plan §14.9).

ACC: H3 H13 H17 G27 (test-tool premises: a setsid escapee really escapes and keeps
MESH_RUN_ID; spawn_grandchild keeps the process group; bad_passphrase sends a
fresh wrong passphrase on every call).
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from typing import List

from mesh.core.proc import get_proc_environ, get_proc_stat
from mesh.platform import proc as plat_proc
from tests.stubs import _write_python_stub

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _gone(pid: int) -> bool:
    info = get_proc_stat(pid)
    return info is None or info["state"] == "Z"


class TestFakeAgentBehaviors(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-fake-")
        self.pids: List[int] = []

    def tearDown(self) -> None:
        for pid in self.pids:
            if sys.platform == "win32":
                try:
                    info = get_proc_stat(pid)
                    if info is not None and int(info.get("starttime", 0)) > 0:
                        plat_proc.kill_if_same(pid, int(info["starttime"]))
                except OSError:
                    pass
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all(_gone(p) for p in self.pids):
            time.sleep(0.05)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_fake(self, spec: str, env_extra: dict, new_session: bool = False) -> subprocess.CompletedProcess:
        prompt = os.path.join(self.tmp, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write(f"FAKE {spec}\n")
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT
        env.update(env_extra)
        popen_kwargs: dict = {
            "capture_output": True,
            "text": True,
            "env": env,
            "timeout": 60,
        }
        if new_session:
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
        return subprocess.run(
            [sys.executable, "-m", "mesh.testing.fake_agent", prompt],
            **popen_kwargs,
        )

    @staticmethod
    def _grandchild_pid(stdout: str) -> int:
        for line in stdout.splitlines():
            if line.startswith("grandchild_pid="):
                return int(line.split("=", 1)[1])
        raise AssertionError(f"no grandchild_pid in {stdout!r}")

    def test_spawn_setsid_grandchild_escapes_and_keeps_run_id(self) -> None:
        """The escapee has its own session and group, outlives the agent, and keeps MESH_RUN_ID."""
        res = self._run_fake('{"spawn_setsid_grandchild": {"seconds": 30}}', {"MESH_RUN_ID": f"run_unitescape_{os.getpid()}"})
        self.assertEqual(res.returncode, 0, res.stderr)
        pid = self._grandchild_pid(res.stdout)
        self.pids.append(pid)
        info = get_proc_stat(pid)
        self.assertIsNotNone(info, "escapee did not survive the fake agent")
        self.assertNotEqual(info["state"], "Z")
        if sys.platform == "win32":
            self.assertGreater(int(info["starttime"]), 0)
        else:
            self.assertEqual(info["session"], pid)
            self.assertEqual(info["pgrp"], pid)
            self.assertNotEqual(info["session"], os.getsid(0))
        env = get_proc_environ(pid)
        self.assertIsNotNone(env)
        self.assertEqual(env.get("MESH_RUN_ID"), f"run_unitescape_{os.getpid()}")

    def test_spawn_grandchild_keeps_process_group(self) -> None:
        """The grandchild stays in the agent's process group and session."""
        res = self._run_fake('{"spawn_grandchild": {"seconds": 30}}', {"MESH_RUN_ID": f"run_unitgroup_{os.getpid()}"},
                             new_session=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        pid = self._grandchild_pid(res.stdout)
        self.pids.append(pid)
        info = get_proc_stat(pid)
        self.assertIsNotNone(info)
        if sys.platform == "win32":
            self.assertGreater(int(info["starttime"]), 0)
        else:
            # The agent ran as a session leader, so its pid is the group and session id.
            self.assertEqual(info["pgrp"], info["session"])
            self.assertNotEqual(info["session"], os.getsid(0))
            self.assertNotEqual(info["pgrp"], pid)
        self.assertEqual(get_proc_environ(pid).get("MESH_RUN_ID"), f"run_unitgroup_{os.getpid()}")

    def test_bad_passphrase_fresh_pipe_each_call(self) -> None:
        """Every call reads a full wrong passphrase from its own pipe (or file on Windows) and prints rc and code."""
        log = os.path.join(self.tmp, "reads.log")
        if sys.platform == "win32":
            stub = _write_python_stub(
                self.tmp,
                "mesh",
                (
                    "#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "path = sys.argv[sys.argv.index('--passphrase-file') + 1]\n"
                    "data = open(path, 'r', encoding='utf-8').read().rstrip('\\r\\n')\n"
                    f"open({log!r}, 'a', encoding='utf-8').write(data + '\\n')\n"
                    "print(json.dumps({'ok': False, 'error': {'code': 'CREDENTIAL_NOT_ALLOWED_IN_RUN'}}))\n"
                    "sys.exit(4)\n"
                ),
            )
        else:
            stub = _write_python_stub(
                self.tmp,
                "mesh",
                (
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "fd = int(sys.argv[sys.argv.index('--passphrase-fd') + 1])\n"
                    "data = os.read(fd, 4096).decode().rstrip('\\n')\n"
                    f"open({log!r}, 'a').write(data + '\\n')\n"
                    "print(json.dumps({'ok': False, 'error': {'code': 'CREDENTIAL_NOT_ALLOWED_IN_RUN'}}))\n"
                    "sys.exit(4)\n"
                ),
            )
        res = self._run_fake('{"bad_passphrase": {"count": 3}}', {"MESH_BIN": stub})
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(log, "r", encoding="utf-8") as f:
            reads = f.read().splitlines()
        self.assertEqual(len(reads), 3)
        for value in reads:
            self.assertTrue(value.startswith("wrong-passphrase"), value)
        self.assertEqual(res.stdout.count("rc=4"), 3)
        self.assertEqual(res.stdout.count("error_code=CREDENTIAL_NOT_ALLOWED_IN_RUN"), 3)

    def test_steal_token_passes_token_file_and_prints_result(self) -> None:
        """steal_token calls mesh with the args and --token-file and prints rc/error_code, never the token."""
        log = os.path.join(self.tmp, "argv.json")
        stub = _write_python_stub(
            self.tmp,
            "mesh",
            (
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"json.dump(sys.argv[1:], open({log!r}, 'w'))\n"
                "print(json.dumps({'ok': False, 'error': {'code': 'CREDENTIAL_NOT_ALLOWED_IN_RUN'}}))\n"
                "sys.exit(4)\n"
            ),
        )
        spec = json.dumps({"steal_token": {"path": "/x/grok.tok", "args": ["task", "accept", "tsk_1"]}})
        res = self._run_fake(spec, {"MESH_BIN": stub})
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(log, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), ["task", "accept", "tsk_1", "--token-file", "/x/grok.tok"])
        self.assertIn("rc=4", res.stdout)
        self.assertIn("error_code=CREDENTIAL_NOT_ALLOWED_IN_RUN", res.stdout)

    def test_fail_if_marker_first_run_succeeds_then_fails(self) -> None:
        """F10/H10 premise: the first run succeeds and creates the marker; later runs fail."""
        marker = os.path.join(self.tmp, "marker")
        spec = json.dumps({"steps": [{"fail_if_marker": {"path": marker, "code": 3}}, {"succeed": {}}]})
        first = self._run_fake(spec, {"MESH_RUN_ID": "run_a"})
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("mesh-deliverable", first.stdout)
        with open(marker, encoding="utf-8") as f:
            self.assertEqual(f.read(), "run_a")
        second = self._run_fake(spec, {"MESH_RUN_ID": "run_b"})
        self.assertEqual(second.returncode, 3)
        self.assertNotIn("mesh-deliverable", second.stdout)

    def test_lead_actions_prints_one_parsable_block(self) -> None:
        """ACC: L2 (test-tool premise) - lead_actions exits 0 with the given actions as
        one mesh-actions block that the daemon's parser reads back unchanged."""
        from mesh.core.lead_actions import parse_actions

        actions = [{"cmd": "task.create", "args": {"title": "t", "assignee": "fx"}},
                   {"cmd": "config.set", "args": {"key": "k", "value": "v"}}]
        res = self._run_fake(json.dumps({"lead_actions": {"actions": actions}}), {})
        self.assertEqual(res.returncode, 0, res.stderr)
        parsed = parse_actions(res.stdout)
        self.assertTrue(parsed.block_found)
        self.assertIsNone(parsed.error)
        self.assertEqual([(a.index, a.cmd, a.args) for a in parsed.actions],
                         [(0, "task.create", actions[0]["args"]), (1, "config.set", actions[1]["args"])])
        self.assertIsNone(parsed.actions[0].error)
        self.assertEqual(parsed.actions[1].error, "cmd_not_allowed")

    def test_win_stop_watcher_symlink_home_and_stdio_flush(self) -> None:
        import threading
        from unittest import mock
        from mesh.platform import _win32_ctypes as w32
        from mesh.testing import fake_agent

        real_home = os.path.join(self.tmp, "real_home")
        run_dir = os.path.join(real_home, "runs", "run_sym")
        os.makedirs(run_dir)
        link_home = os.path.join(self.tmp, "link_home")
        try:
            os.symlink(real_home, link_home, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        prompt_via_link = os.path.join(link_home, "runs", "run_sym", "prompt.md")
        with open(prompt_via_link, "w", encoding="utf-8") as f:
            f.write("FAKE {}\n")

        events: list = []
        done = threading.Event()

        class _FakeK32:
            def OpenEventW(self, access: int, inherit: bool, name: str) -> int:
                events.append(("OpenEventW", name))
                return 0x4444

            def WaitForSingleObject(self, handle: int, timeout: int) -> int:
                return w32.WAIT_OBJECT_0

            def CloseHandle(self, handle: int) -> int:
                events.append(("CloseHandle", handle))
                return 1

        def _fake_exit(code: int) -> None:
            events.append(("os._exit", code))
            done.set()

        seen_homes: list = []

        def _fake_ev_name(home: str, run_id: str) -> str:
            seen_homes.append((home, run_id))
            return f"Local\\mesh-stop-fake-{run_id}"

        orig_ignore = fake_agent._IGNORE_STOP_SIGNAL
        fake_agent._IGNORE_STOP_SIGNAL = False
        try:
            env = {k: v for k, v in os.environ.items() if k != "MESH_HOME"}
            env["MESH_RUN_ID"] = "run_sym"
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(sys, "platform", "win32"), \
                    mock.patch.object(plat_proc, "run_stop_event_name", side_effect=_fake_ev_name), \
                    mock.patch.object(w32, "get_kernel32", return_value=_FakeK32()), \
                    mock.patch.object(sys.stdout, "flush", side_effect=lambda: events.append(("flush", "stdout"))), \
                    mock.patch.object(sys.stderr, "flush", side_effect=lambda: events.append(("flush", "stderr"))), \
                    mock.patch.object(os, "_exit", side_effect=_fake_exit):
                fake_agent._start_win_stop_watcher(prompt_via_link, None)
                self.assertTrue(done.wait(timeout=5.0), f"watcher did not fire: {events}")
        finally:
            fake_agent._IGNORE_STOP_SIGNAL = orig_ignore

        self.assertEqual(seen_homes, [(os.path.realpath(real_home), "run_sym")])
        exit_idx = events.index(("os._exit", 143))
        self.assertLess(events.index(("flush", "stdout")), exit_idx)
        self.assertLess(events.index(("flush", "stderr")), exit_idx)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only Named Stop Event watcher test")
    def test_win32_stop_watcher_infers_home_and_exits_143(self) -> None:
        real_home = os.path.join(self.tmp, "real_home_win")
        run_id = "run_win_watch"
        run_dir = os.path.join(real_home, "runs", run_id)
        os.makedirs(run_dir)
        link_home = os.path.join(self.tmp, "link_home_win")
        try:
            os.symlink(real_home, link_home, target_is_directory=True)
            prompt = os.path.join(link_home, "runs", run_id, "prompt.md")
        except OSError:
            prompt = os.path.join(run_dir, "prompt.md")

        with open(prompt, "w", encoding="utf-8") as f:
            f.write('FAKE {"steps": [{"sleep": {"seconds": 0.2, "output_every": 0.2}}, {"sleep": {"seconds": 30}}]}\n')

        stop_ev = plat_proc.create_stop_event(real_home, run_id)
        self.addCleanup(stop_ev.close)
        env = {k: v for k, v in os.environ.items() if k != "MESH_HOME"}
        env["PYTHONPATH"] = REPO_ROOT
        env["MESH_RUN_ID"] = run_id
        proc = subprocess.Popen(
            [sys.executable, "-m", "mesh.testing.fake_agent", prompt],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert proc.stdout is not None
            ready_line = proc.stdout.readline()
            self.assertIn(b"Slept", ready_line)
            stop_ev.set()
            self.assertEqual(proc.wait(timeout=10.0), 143)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
