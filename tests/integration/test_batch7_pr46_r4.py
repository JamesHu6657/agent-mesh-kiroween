"""Integration regression tests for batch7 PR #46 round-4 review findings."""

import json
import os
import subprocess
import unittest

import mesh.constants as c
from tests.integration import test_merge as tm


class TestBatch7Pr46R4Integration(unittest.TestCase):
    """End-to-end regressions for PR46 R4 findings (P2 merge --all-accepted pipe fd, P3 JSON synonym dupes)."""

    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project
    show = tm.TestMerge.show
    start_task = tm.TestMerge.start_task
    commit_file = tm.TestMerge.commit_file
    submit_accept = tm.TestMerge.submit_accept
    accepted_task = tm.TestMerge.accepted_task
    integ_wt = tm.TestMerge.integ_wt
    q = tm.TestMerge.q

    def _run_with_pipe_passphrase(self, *cli_args: str) -> subprocess.CompletedProcess:
        r_fd, w_fd = os.pipe()
        try:
            os.write(w_fd, (self.harness.passphrase + "\n").encode("utf-8"))
        finally:
            os.close(w_fd)
        env = {
            k: v
            for k, v in self.harness.get_env().items()
            if not k.startswith("MESH_PASSPHRASE")
        }
        try:
            return subprocess.run(
                [
                    self.harness.bin_mesh,
                    "--home",
                    self.harness.home,
                    *cli_args,
                    "--user",
                    "--passphrase-fd",
                    str(r_fd),
                ],
                env=env,
                pass_fds=(r_fd,),
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            os.close(r_fd)

    def test_merge_all_accepted_pipe_passphrase_fd_nonempty_queue(self) -> None:
        t1, _ = self.accepted_task(self.prj, "T1", "f1.txt", "one\n")
        t2, _ = self.accepted_task(self.prj, "T2", "f2.txt", "two\n")

        proc = self._run_with_pipe_passphrase("task", "merge", "--all-accepted", "--project", self.prj)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        env = json.loads(proc.stdout)
        self.assertTrue(env["ok"], env)
        self.assertEqual([m["task_id"] for m in env["data"]["merged"]], [t1, t2])
        self.assertEqual(self.show(t1)["status"], "merged")
        self.assertEqual(self.show(t2)["status"], "merged")

        # Text mode (--format text spawns _merge_progress_note) must also consume pipe fd once.
        t3, _ = self.accepted_task(self.prj, "T3", "f3.txt", "three\n")
        proc_txt = self._run_with_pipe_passphrase(
            "--format", "text", "task", "merge", "--all-accepted", "--project", self.prj
        )
        self.assertEqual(proc_txt.returncode, 0, f"stdout={proc_txt.stdout!r} stderr={proc_txt.stderr!r}")
        self.assertIn(f"merged {t3}", proc_txt.stderr)
        self.assertIn("merged 1 accepted task(s)", proc_txt.stdout)
        self.assertIn(t3, proc_txt.stdout)
        self.assertEqual(self.show(t3)["status"], "merged")

        # Stdin pipe (--passphrase-fd 0) must also succeed on a non-empty queue.
        t4, _ = self.accepted_task(self.prj, "T4", "f4.txt", "four\n")
        t5, _ = self.accepted_task(self.prj, "T5", "f5.txt", "five\n")
        env = {
            k: v
            for k, v in self.harness.get_env().items()
            if not k.startswith("MESH_PASSPHRASE")
        }
        proc_stdin = subprocess.run(
            [
                self.harness.bin_mesh,
                "--home",
                self.harness.home,
                "task",
                "merge",
                "--all-accepted",
                "--project",
                self.prj,
                "--user",
                "--passphrase-fd",
                "0",
            ],
            input=self.harness.passphrase + "\n",
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc_stdin.returncode, 0, f"stdout={proc_stdin.stdout!r} stderr={proc_stdin.stderr!r}")
        env_stdin = json.loads(proc_stdin.stdout)
        self.assertTrue(env_stdin["ok"], env_stdin)
        self.assertEqual([m["task_id"] for m in env_stdin["data"]["merged"]], [t4, t5])
        self.assertEqual(self.show(t4)["status"], "merged")
        self.assertEqual(self.show(t5)["status"], "merged")

    def test_input_duplicate_synonym_and_canonical_end_to_end(self) -> None:
        # Repro 1: intake submit with invalid-type synonym + valid canonical key -> DUPLICATE_PARAM (exit 2)
        projects_before = len(self.q("SELECT id FROM projects;"))
        rc1, resp1 = self.harness.cli(
            "intake",
            "submit",
            "--input",
            "-",
            stdin='{"title": "T", "goal": "G", "lead": "lead1", "criteria": 123, "criterion": ["C1"]}',
            user=True,
        )
        self.assertEqual(rc1, c.EXIT_USAGE, resp1)
        self.assertEqual(resp1["error"]["code"], c.DUPLICATE_PARAM, resp1)
        self.assertEqual(len(self.q("SELECT id FROM projects;")), projects_before)

        # Repro 2: agent create with allowed_models + allowed_model -> DUPLICATE_PARAM (exit 2),
        # not DEFAULT_MODEL_NOT_ALLOWED from sending unvalidated allowed_models=["m1"].
        agents_before = len(self.q("SELECT id FROM agents;"))
        rc2, resp2 = self.harness.cli(
            "agent",
            "create",
            "--input",
            "-",
            stdin=(
                '{"name": "a_syn", "adapter": "fake", "delivery": "launch", '
                '"allowed_models": ["m1"], "allowed_model": ["m2"], "default_model": "m2"}'
            ),
            user=True,
        )
        self.assertEqual(rc2, c.EXIT_USAGE, resp2)
        self.assertEqual(resp2["error"]["code"], c.DUPLICATE_PARAM, resp2)
        self.assertEqual(len(self.q("SELECT id FROM agents;")), agents_before)


if __name__ == "__main__":
    unittest.main()
