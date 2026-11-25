"""Rejection context survives a failed rework attempt and an explicit CLI retry."""

import json
import os
import sqlite3

from tests.integration.test_execution_failure_policy import FailurePolicyBase


class TestReworkFeedbackRetry(FailurePolicyBase):
    def test_rework_failure_retry_keeps_latest_feedback(self) -> None:
        """ACC: L5 - full prompts after both nonzero-exit and timeout rework failures."""
        for failure in ("exit_nonzero", "timeout"):
            with self.subTest(failure=failure):
                marker = self.gate(failure)
                if failure == "timeout":
                    self.release(marker)
                    step = {"wait_release": {"path": marker}}
                else:
                    step = {"fail_if_marker": {"path": marker, "code": 3}}
                older_summary = f"Original deliverable before {failure}"
                summary = f"Latest deliverable before {failure}"

                def fake_spec(deliverable_summary):
                    return "FAKE " + json.dumps({"steps": [
                        step, {"submit_via_cli": {"summary": deliverable_summary}}, {"succeed": {}},
                    ]})

                spec = fake_spec(summary)
                task = self.create_task(fake_spec(older_summary), "--max-attempts", "5", "--timeout", "8s")

                def wait_closed(count):
                    self.assertTrue(self.harness.wait_for(
                        lambda: len(self.runs_of(task)) == count
                        and all(r["status"] not in ("starting", "running") for r in self.runs_of(task)),
                        timeout=45))
                    return self.runs_of(task)[-1]

                wait_closed(1)
                self.assertEqual(self.status(task), "submitted")
                self.assertEqual(json.loads(self.task_row(task)["deliverable_json"])["deliverable"]["summary"],
                                 older_summary)
                # Change only the fake's script between closed attempts; both
                # distinct deliverables still come from real CLI submissions.
                conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10)
                try:
                    conn.execute("UPDATE tasks SET spec = ? WHERE id = ?;", (spec, task))
                    conn.commit()
                finally:
                    conn.close()
                # An older rejection must not win over the next one.
                if failure == "exit_nonzero":
                    os.unlink(marker)
                rc, resp = self.harness.cli("task", "reject", task, "--reason", "Older feedback",
                                            token=self.tok_lead1)
                self.assertEqual(rc, 0, resp)
                wait_closed(2)
                self.assertEqual(self.status(task), "submitted")
                self.assertEqual(json.loads(self.task_row(task)["deliverable_json"])["deliverable"]["summary"],
                                 summary)
                if failure == "timeout":
                    os.unlink(marker)
                feedback = f"Latest rejection: add the missing {failure} tests"
                rc, resp = self.harness.cli("task", "reject", task, "--reason", feedback,
                                            token=self.tok_lead1)
                self.assertEqual(rc, 0, resp)
                failed = wait_closed(3)
                self.assertEqual(self.status(task), "failed")
                self.assertEqual(self.failure_reason(task), failure, dict(failed))
                if failure == "exit_nonzero":
                    os.unlink(marker)
                else:
                    self.release(marker)
                rc, resp = self.harness.cli("task", "retry", task, token=self.tok_lead1)
                self.assertEqual(rc, 0, resp)
                retried = wait_closed(4)
                self.assertEqual(self.status(task), "submitted")
                with open(os.path.join(retried["run_dir"], "prompt.md"), encoding="utf-8") as f:
                    prompt = f.read()
                rework = prompt.split("## Rework feedback (if any)\n", 1)[1].split("## Rules", 1)[0]
                for text in (feedback, summary):
                    self.assertIn(text, rework)
                self.assertNotIn(older_summary, rework)
                for text in (spec, "c1", "c2", "4/5"):
                    self.assertIn(text, prompt)
                self.assertNotIn("Older feedback", prompt)
                self.doctor_ok()
