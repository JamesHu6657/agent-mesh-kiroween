"""Unit tests for the fake agent behaviors that M1c termination tests rely on (plan §14.9).

ACC: H3 H13 H17 G27 (test-tool premises: a setsid escapee really escapes and keeps
MESH_RUN_ID; spawn_grandchild keeps the process group; bad_passphrase sends a
fresh wrong passphrase on every call).
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from typing import List

from mesh.core.proc import get_proc_environ, get_proc_stat

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
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all(_gone(p) for p in self.pids):
            time.sleep(0.05)
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def _run_fake(self, spec: str, env_extra: dict, new_session: bool = False) -> subprocess.CompletedProcess:
        prompt = os.path.join(self.tmp, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write(f"FAKE {spec}\n")
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "mesh.testing.fake_agent", prompt],
            capture_output=True, text=True, env=env, timeout=60, start_new_session=new_session,
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
        # The agent ran as a session leader, so its pid is the group and session id.
        self.assertEqual(info["pgrp"], info["session"])
        self.assertNotEqual(info["session"], os.getsid(0))
        self.assertNotEqual(info["pgrp"], pid)
        self.assertEqual(get_proc_environ(pid).get("MESH_RUN_ID"), f"run_unitgroup_{os.getpid()}")

    def test_bad_passphrase_fresh_pipe_each_call(self) -> None:
        """Every call reads a full wrong passphrase from its own pipe and prints rc and code."""
        log = os.path.join(self.tmp, "reads.log")
        stub = os.path.join(self.tmp, "mesh")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "fd = int(sys.argv[sys.argv.index('--passphrase-fd') + 1])\n"
                "data = os.read(fd, 4096).decode().rstrip('\\n')\n"
                f"open({log!r}, 'a').write(data + '\\n')\n"
                "print(json.dumps({'ok': False, 'error': {'code': 'CREDENTIAL_NOT_ALLOWED_IN_RUN'}}))\n"
                "sys.exit(4)\n"
            )
        os.chmod(stub, 0o700)
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
        stub = os.path.join(self.tmp, "mesh")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"json.dump(sys.argv[1:], open({log!r}, 'w'))\n"
                "print(json.dumps({'ok': False, 'error': {'code': 'CREDENTIAL_NOT_ALLOWED_IN_RUN'}}))\n"
                "sys.exit(4)\n"
            )
        os.chmod(stub, 0o700)
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


if __name__ == "__main__":
    unittest.main()
