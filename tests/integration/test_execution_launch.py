"""Integration tests for launch-mode execution: T5 spawn, run tokens, executor authz.

Checklist items: ACC: H12 (executor run-token authority), plus the spawn, prompt,
spec and run-token clauses of plan §11.1, §12.5 and §7.1 that run 2 sub-step 3
builds. The watch loop, exit handling and termination are later sub-steps.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

import mesh.constants as c
from mesh.core.proc import get_proc_environ
from tests.helpers import MeshHarness
from tests.stubs import create_test_stubs


def create_agent(
    harness: MeshHarness,
    name: str,
    adapter: str = "none",
    delivery: str = "inbox",
    can_lead: bool = False,
    capabilities: Optional[List[str]] = None,
    allowed_models: Optional[List[str]] = None,
    default_model: Optional[str] = None,
    max_concurrency: int = 1,
) -> str:
    if delivery == "launch":
        if allowed_models is None:
            allowed_models = ["fake-model"]
        if default_model is None:
            default_model = "fake-model"

    args = [
        "agent", "create",
        "--name", name,
        "--adapter", adapter,
        "--delivery", delivery,
        "--max-concurrency", str(max_concurrency),
    ]
    if can_lead:
        args.append("--can-lead")
    if capabilities:
        for cap in capabilities:
            args.extend(["--capability", cap])
    if allowed_models:
        for m in allowed_models:
            args.extend(["--allowed-model", m])
    if default_model:
        args.extend(["--default-model", default_model])

    rc, resp = harness.setup_cli(*args, user=True)
    assert rc == 0, f"agent create {name} failed: {resp}"

    rc_tok, tok_resp = harness.setup_cli("agent", "token", "issue", name, user=True)
    assert rc_tok == 0, f"token issue {name} failed: {tok_resp}"
    return tok_resp["data"]["token"]


class TestExecutionLaunch(unittest.TestCase):
    """Integration tests for launch-mode T5 spawn and executor run tokens."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

        self.tok_lead1 = create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        self.tok_plain = create_agent(self.harness, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
        self.tok_fx = create_agent(self.harness, "fx", adapter="fake", delivery="launch", max_concurrency=1)

        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)

        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "PRJ Launch",
            "--goal", "Test launch execution",
            "--criterion", "C1",
            "--lead", "lead1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        self.project_id = resp_p["data"]["id"]

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def _create_task(self, spec: str, title: str = "Launch task", assignee: str = "fx") -> str:
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", title,
            "--spec", spec,
            "--assignee", assignee,
            token=self.tok_lead1,
        )
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def _run_row(self, task_id: str) -> Optional[sqlite3.Row]:
        conn = self.harness.db()
        try:
            cur = conn.execute(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT 1;", (task_id,)
            )
            return cur.fetchone()
        finally:
            conn.close()

    def _wait_run_started(self, task_id: str, timeout: float = 15.0) -> sqlite3.Row:
        deadline = time.monotonic() + timeout
        row = None
        while time.monotonic() < deadline:
            row = self._run_row(task_id)
            if row is not None and row["status"] in ("running", "exited", "killed", "lost", "spawn_failed"):
                return row
            time.sleep(0.1)
        conn = self.harness.db()
        try:
            tasks = [dict(r) for r in conn.execute("SELECT id, status, assignee_agent_id FROM tasks;")]
            runs = [dict(r) for r in conn.execute("SELECT id, task_id, status FROM runs;")]
        finally:
            conn.close()
        log_path = os.path.join(self.harness.home, "logs", "meshd.log")
        tail = ""
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                tail = f.read()[-3000:]
        self.fail(
            f"no run row for task {task_id} within {timeout}s (last={dict(row) if row else None}) "
            f"tasks={tasks} runs={runs} log={tail}"
        )

    def test_t5_spawn_creates_run_prompt_spec_and_run_token(self) -> None:
        """T5 spawn: run row, run dir, prompt.md, spec.json, run token scope (plan §11.1, §7.1)."""
        task_id = self._create_task('FAKE {"sleep": {"seconds": 120}}')
        run = self._wait_run_started(task_id)

        self.assertEqual(run["kind"], "task")
        self.assertEqual(run["status"], "running")
        self.assertIsNotNone(run["wrapper_pid"])
        self.assertEqual(run["pgid"], run["wrapper_pid"])
        self.assertIsNotNone(run["proc_starttime"])
        self.assertIsNotNone(run["deadline_at"])

        run_dir = run["run_dir"]
        self.assertTrue(os.path.isdir(run_dir))
        self.assertEqual(os.stat(run_dir).st_mode & 0o777, 0o700)

        prompt_path = os.path.join(run_dir, "prompt.md")
        self.assertTrue(os.path.isfile(prompt_path))
        with open(prompt_path, "r", encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn(f"# Agent Mesh task {task_id} — attempt 1/3", prompt)
        self.assertIn("## Task: Launch task", prompt)
        self.assertIn("## Project acceptance criteria (context only)", prompt)
        self.assertIn("1. C1", prompt)
        self.assertIn("## Rework feedback (if any)", prompt)
        self.assertIn("```mesh-deliverable", prompt)

        spec_path = os.path.join(run_dir, "spec.json")
        self.assertTrue(os.path.isfile(spec_path))
        self.assertEqual(os.stat(spec_path).st_mode & 0o777, 0o600)
        with open(spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        self.assertIn("argv", spec)
        self.assertIn("cwd", spec)
        self.assertIn("stdin", spec)
        self.assertIn("redact_env_names", spec)
        self.assertIn("MESH_TOKEN", spec["redact_env_names"])
        # No env values and no secrets in spec.json (plan §11.1, §15.1).
        self.assertNotIn("env", spec)
        self.assertNotIn("MESH_TOKEN", json.dumps(spec["argv"]))

        # Run token: scope per §7.1, never in argv.
        conn = self.harness.db()
        try:
            cur = conn.execute(
                "SELECT scope_json, expires_at, revoked_at FROM credentials "
                "WHERE kind = 'run_token' AND json_extract(scope_json, '$.run_id') = ?;",
                (run["id"],),
            )
            cred = cur.fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(cred)
        scope = json.loads(cred["scope_json"])
        self.assertEqual(scope["role"], "executor")
        self.assertEqual(scope["run_id"], run["id"])
        self.assertEqual(scope["task_id"], task_id)
        self.assertEqual(scope["project_id"], self.project_id)
        self.assertEqual(scope["fence"], run["fence"])
        self.assertIsNone(cred["revoked_at"])

        # The task is running with the fence and attempt of the run.
        rc_s, resp_s = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc_s, 0)
        self.assertEqual(resp_s["data"]["status"], "running")
        self.assertEqual(resp_s["data"]["attempt"], 1)

        # The wrapper's env carries the run credentials and no MESH_HOME.
        wrapper_env = get_proc_environ(run["wrapper_pid"])
        self.assertIsNotNone(wrapper_env)
        self.assertEqual(wrapper_env.get("MESH_RUN_ID"), run["id"])
        self.assertEqual(wrapper_env.get("MESH_TASK_ID"), task_id)
        self.assertEqual(wrapper_env.get("MESH_PROJECT_ID"), self.project_id)
        self.assertEqual(wrapper_env.get("MESH_FENCE"), str(run["fence"]))
        self.assertTrue(wrapper_env.get("MESH_TOKEN", "").startswith("mesh_rt_"))
        self.assertEqual(wrapper_env.get("MESH_SOCKET"), os.path.join(self.harness.home, "run", "mesh.sock"))
        self.assertNotIn("MESH_HOME", wrapper_env)

    def test_h12_executor_run_token_authority(self) -> None:
        """ACC: H12 - From inside a fake run: task create -> rc 4 CAPABILITY_MISSING; task submit <other task> -> rc 4 SCOPE_MISMATCH."""
        # A second task on an inbox agent, so the run token has a foreign task to target.
        create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        rc_o, resp_o = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Other task",
            "--spec", "Other spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_o, 0, resp_o)
        other_task = resp_o["data"]["id"]

        task_id = self._create_task('FAKE {"sleep": {"seconds": 120}}')
        run = self._wait_run_started(task_id)

        # Drive the CLI with the run's own environment (the executor's view).
        wrapper_env = get_proc_environ(run["wrapper_pid"])
        self.assertIsNotNone(wrapper_env)

        def as_run(*args: str) -> tuple:
            res = subprocess.run(["mesh", *args], capture_output=True, text=True, env=dict(wrapper_env), timeout=60)
            try:
                env = json.loads(res.stdout)
            except ValueError:
                env = {}
            code = (env.get("error") or {}).get("code") if isinstance(env, dict) else None
            return res.returncode, code

        project_id = wrapper_env["MESH_PROJECT_ID"]
        own_task = wrapper_env["MESH_TASK_ID"]
        self.assertEqual(as_run("task", "create", "--project", project_id, "--title", "x", "--spec", "y"), (4, c.CAPABILITY_MISSING))
        self.assertEqual(as_run("task", "submit", other_task, "--summary", "s"), (4, c.SCOPE_MISMATCH))
        self.assertEqual(as_run("task", "show", other_task), (4, c.SCOPE_MISMATCH))
        self.assertEqual(as_run("inbox", "list"), (4, c.CAPABILITY_MISSING))
        # Its own task and project are readable.
        self.assertEqual(as_run("task", "show", own_task), (0, None))
        self.assertEqual(as_run("project", "show", project_id), (0, None))

    def test_t5_concurrency_limit_starts_one_run(self) -> None:
        """T5 respects per-agent max_concurrency: a second queued task waits (plan §9.2, §9.3)."""
        first = self._create_task('FAKE {"sleep": {"seconds": 120}}', title="First")
        second = self._create_task('FAKE {"sleep": {"seconds": 120}}', title="Second")

        run = self._wait_run_started(first)
        self.assertEqual(run["status"], "running")

        # fx has max_concurrency 1, so the second task stays queued.
        rc_s, resp_s = self.harness.cli("task", "show", second, token=self.tok_lead1)
        self.assertEqual(rc_s, 0)
        self.assertEqual(resp_s["data"]["status"], "queued")
        self.assertIsNone(self._run_row(second))

    def test_t5_queue_order_priority_then_fifo(self) -> None:
        """T5 starts queued tasks in priority DESC, queued_at ASC, id ASC order (plan §9.3)."""
        # Hold T5 so both tasks are queued before the scheduler picks one.
        self.harness.hold_launch_starts()
        low = self._create_task('FAKE {"sleep": {"seconds": 120}}', title="Low")
        rc_h, resp_h = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "High",
            "--spec", 'FAKE {"sleep": {"seconds": 120}}',
            "--assignee", "fx",
            "--priority", "9",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_h, 0, resp_h)
        high = resp_h["data"]["id"]
        self.assertIsNone(self._run_row(low))
        self.harness.release_launch_starts()
        rc_d, _ = self.harness.cli("daemon", "status")  # any request runs a scheduler tick
        self.assertEqual(rc_d, 0)

        run = self._wait_run_started(high)
        self.assertEqual(run["task_id"], high)
        self.assertIsNone(self._run_row(low))

    def test_t5_prompt_too_long_fails_task(self) -> None:
        """T5 over the prompt limit on an adapter that cannot read prompt.md -> T6 failed(prompt_too_long) (plan §12.5).

        kiro is not launch-enabled before M3, so the fake adapter (argv-only, no
        file or pointer strategy) stands in for "cannot read the file".
        """
        big_spec = "x" * 120_000
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Huge",
            "--spec", big_spec,
            "--assignee", "fx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0, resp_t)
        task_id = resp_t["data"]["id"]

        def failed() -> bool:
            rc, resp = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
            return rc == 0 and resp["data"]["status"] == "failed"

        self.assertTrue(self.harness.wait_for(failed, timeout=20.0))
        rc_s, resp_s = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc_s, 0)
        failure = resp_s["data"]["failure"]
        self.assertEqual(failure["reason"], "prompt_too_long")
        self.assertEqual(failure["details"]["limit"], 100_000)
        self.assertEqual(failure["details"]["adapter"], "fake")
        self.assertGreater(failure["details"]["bytes"], 100_000)
        self.assertIsNone(self._run_row(task_id))


if __name__ == "__main__":
    unittest.main()
