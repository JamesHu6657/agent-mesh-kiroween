"""Acceptance reconciliation tests (audit gap closure).

ACC: N16 (T5 prompt with rework feedback over the limit -> T6 failed(prompt_too_long)).
"""

import json
import os
import shutil
import tempfile
import unittest

from tests.helpers import MeshHarness
from tests.integration.test_execution_launch import create_agent
from tests.stubs import create_test_stubs


PROMPT_LIMIT = 100_000


class TestPromptT5TooLongWithRework(unittest.TestCase):
    """ACC: N16 - spec fits; the T5 prompt for the rework attempt (spec + feedback) does not."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_plain = create_agent(self.harness, "plain", capabilities=["intake"])
        create_agent(self.harness, "fx", adapter="fake", delivery="launch",
                     allowed_models=["m-a"], default_model="m-a", max_concurrency=1)
        rc, resp = self.harness.setup_cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.setup_cli("intake", "submit", "--title", "PRJ", "--goal", "N16",
                                          "--criterion", "c1", "--lead", "lead1", token=self.tok_plain)
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def _row(self, sql, args=()):
        conn = self.harness.db()
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()

    def _status(self, task_id: str) -> str:
        return self._row("SELECT status FROM tasks WHERE id = ?;", (task_id,))["status"]

    def _wait(self, task_id: str, status: str, timeout: float = 30.0) -> None:
        ok = self.harness.wait_for(lambda: self._status(task_id) == status, timeout=timeout)
        self.assertTrue(ok, f"{task_id} is {self._status(task_id)}, never reached {status}")

    def test_prompt_t5_too_long_after_rework(self) -> None:
        """ACC: N16 - first attempt launches (spec fits); rework feedback pushes T5 over -> T6 prompt_too_long."""
        # The spec alone fits comfortably under the limit (the rendered prompt adds a few KB of template).
        spec = 'FAKE {"succeed": {}}\n' + ("s" * 96_000)
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", "N16",
                                    "--spec", spec, "--assignee", "fx", "--profile", "readonly",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        task_id = resp["data"]["id"]

        # Attempt 1 is launched (proves the spec-only prompt fits) and submits.
        self._wait(task_id, "submitted")
        runs = self._row("SELECT COUNT(*) AS n FROM runs WHERE task_id = ?;", (task_id,))["n"]
        self.assertEqual(runs, 1)
        run1 = self._row("SELECT run_dir FROM runs WHERE task_id = ?;", (task_id,))
        first_prompt_bytes = os.path.getsize(os.path.join(run1["run_dir"], "prompt.md"))
        self.assertLessEqual(first_prompt_bytes, PROMPT_LIMIT, "attempt-1 prompt (spec only) must fit")
        self.assertTrue(self.harness.wait_for(
            lambda: self._row("SELECT COUNT(*) AS n FROM leases WHERE key = ?;", (f"task:{task_id}",))["n"] == 0,
            timeout=20.0,
        ))

        # Rework feedback that, with the spec, exceeds the limit (each alone is under it).
        feedback = "r" * 4_000  # T12 caps --reason at 4096 bytes
        self.assertLess(len(feedback), PROMPT_LIMIT)
        rc, resp = self.harness.cli("task", "reject", task_id, "--reason", feedback, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)

        self._wait(task_id, "failed")
        rc, resp = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        failure = resp["data"]["failure"]
        self.assertEqual(failure["reason"], "prompt_too_long")
        details = failure["details"]
        self.assertEqual(details["limit"], PROMPT_LIMIT)
        self.assertEqual(details["adapter"], "fake")
        self.assertEqual(details["profile"], "readonly")
        self.assertGreater(details["bytes"], PROMPT_LIMIT)
        self.assertGreaterEqual(details["bytes"], first_prompt_bytes + len(feedback))
        # No second run was started for the rework attempt.
        runs = self._row("SELECT COUNT(*) AS n FROM runs WHERE task_id = ?;", (task_id,))["n"]
        self.assertEqual(runs, 1)
        # The T6 event: rework -> queued (T13) -> failed with reason prompt_too_long and the same details.
        ev = self._row(
            "SELECT * FROM events WHERE task_id = ? AND type = 'task.failed' ORDER BY seq DESC LIMIT 1;",
            (task_id,),
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev["from_state"], "queued")
        self.assertEqual(ev["to_state"], "failed")
        self.assertEqual(ev["reason"], "prompt_too_long")
        payload = json.loads(ev["payload_json"])
        self.assertEqual(payload["details"], details)


if __name__ == "__main__":
    unittest.main()
