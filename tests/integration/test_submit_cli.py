"""Integration tests for the `task submit` CLI deliverable/summary merge.

Not an ACC item (G16 is tested in test_execution_inbox). The merge is how an executor's
deliverable reaches the lead, so the merge path is exercised through the real CLI.
"""

import json
import os
import shutil
import tempfile
import unittest

from tests.helpers import MeshHarness
from tests.stubs import create_test_stubs


def create_agent(
    harness: MeshHarness,
    name: str,
    adapter: str = "none",
    delivery: str = "inbox",
    can_lead: bool = False,
    capabilities=None,
    max_concurrency: int = 1,
) -> str:
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

    rc, resp = harness.cli(*args, user=True)
    assert rc == 0, f"agent create {name} failed: {resp}"

    rc_tok, tok_resp = harness.cli("agent", "token", "issue", name, user=True)
    assert rc_tok == 0, f"token issue {name} failed: {tok_resp}"
    return tok_resp["data"]["token"]


class TestSubmitCliDeliverableSummary(unittest.TestCase):
    """`task submit --deliverable <file> --summary S` merges S into the deliverable."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

        # Standard Fixture F1 (inbox agents only; no launch execution needed)
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tok_ibx = create_agent(self.harness, "ibx", max_concurrency=1)

        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)

        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "PRJ Submit CLI",
            "--goal", "Test the submit CLI merge",
            "--criterion", "C1",
            "--lead", "lead1",
            token=self.tok_grok,
        )
        self.assertEqual(rc_p, 0)
        self.project_id = resp_p["data"]["id"]

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def _start_task(self, title: str):
        """Create a task for ibx, wait until queued, start it, return (task_id, run_id, fence)."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", title,
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli(
            "task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1
        )
        self.assertEqual(rc_w, 0)

        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        return tsk, resp_st["data"]["run_id"], resp_st["data"]["fence"]

    def test_g16_submit_deliverable_file_merges_summary(self) -> None:
        """Deliverable merge - `task submit --deliverable <file> --summary S` sets data.deliverable.summary == S."""
        tsk, run_id, fence = self._start_task("Merge Summary Task")

        deliverable_file = os.path.join(self.harness.home, "deliv-merge.json")
        deliv_obj = {"result": "ok", "items_processed": 7}
        with open(deliverable_file, "w", encoding="utf-8") as fp:
            json.dump(deliv_obj, fp)

        rc_sub, resp_sub = self.harness.cli(
            "task", "submit", tsk,
            "--deliverable", deliverable_file,
            "--summary", "Implemented export",
            "--run", run_id,
            "--fence", str(fence),
            token=self.tok_ibx,
        )
        self.assertEqual(rc_sub, 0)
        data = resp_sub["data"]
        self.assertEqual(data["status"], "submitted")
        # The file's keys survive and the summary is merged in as `summary`.
        self.assertEqual(data["deliverable"]["result"], "ok")
        self.assertEqual(data["deliverable"]["items_processed"], 7)
        self.assertEqual(data["deliverable"]["summary"], "Implemented export")

        # The stored deliverable (task show unwraps the wrapper) carries the same merge.
        rc_show, resp_show = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_show, 0)
        self.assertEqual(resp_show["data"]["deliverable"]["summary"], "Implemented export")

    def test_g16_submit_explicit_json_summary_wins(self) -> None:
        """Deliverable merge - an explicit `summary` key in the deliverable JSON wins over --summary."""
        tsk, run_id, fence = self._start_task("Explicit Summary Task")

        deliverable_file = os.path.join(self.harness.home, "deliv-explicit.json")
        deliv_obj = {"result": "ok", "summary": "summary from the JSON"}
        with open(deliverable_file, "w", encoding="utf-8") as fp:
            json.dump(deliv_obj, fp)

        rc_sub, resp_sub = self.harness.cli(
            "task", "submit", tsk,
            "--deliverable", deliverable_file,
            "--summary", "summary from the flag",
            "--run", run_id,
            "--fence", str(fence),
            token=self.tok_ibx,
        )
        self.assertEqual(rc_sub, 0)
        data = resp_sub["data"]
        self.assertEqual(data["status"], "submitted")
        self.assertEqual(data["deliverable"]["summary"], "summary from the JSON")
        self.assertEqual(data["deliverable"]["result"], "ok")

        rc_show, resp_show = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_show, 0)
        self.assertEqual(resp_show["data"]["deliverable"]["summary"], "summary from the JSON")


if __name__ == "__main__":
    unittest.main()
