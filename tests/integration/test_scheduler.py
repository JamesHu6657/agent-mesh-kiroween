"""Integration tests for Scheduler promotion and queue ordering per plan §16 M1.6.

ACC: F6, D12
"""

import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional

import mesh.constants as c
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
            allowed_models = ["m-a", "m-b"]
        if default_model is None:
            default_model = "m-a"

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

    rc, resp = harness.cli(*args, user=True)
    assert rc == 0, f"agent create {name} failed: {resp}"

    rc_tok, tok_resp = harness.cli("agent", "token", "issue", name, user=True)
    assert rc_tok == 0, f"token issue {name} failed: {tok_resp}"
    token = tok_resp["data"]["token"]

    tok_dir = os.path.join(harness.home, "tokens")
    os.makedirs(tok_dir, mode=0o700, exist_ok=True)
    tok_path = os.path.join(tok_dir, f"{name}.tok")
    with open(tok_path, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(tok_path, 0o600)

    return token


class TestSchedulerIntegration(unittest.TestCase):
    """Integration test suite for scheduler task promotion and queue ordering."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

        self.tok_lead1 = create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        self.tok_plain = create_agent(self.harness, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
        self.tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        self.tok_fx = create_agent(self.harness, "fx", adapter="fake", delivery="launch", max_concurrency=2)

        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)

        # M1c run 2 wires launch T5. These M1b tests assert on the queued state of
        # launch tasks, so they hold launch starts (testing hook, plan §9.3).
        self.harness.hold_launch_starts()

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def test_f6_project_dependencies_promotion(self) -> None:
        """ACC: F6 - Project B with --depends-on-project $PRJ; assign task in B -> task does not queue until $PRJ is completed."""
        # 1. Create project PRJ_A with one criterion
        rc_a, resp_a = self.harness.cli(
            "intake", "submit",
            "--title", "Project A",
            "--goal", "Goal A",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_a, 0)
        prj_a = resp_a["data"]["id"]

        # 2. Create project PRJ_B with --depends-on-project PRJ_A
        rc_b, resp_b = self.harness.cli(
            "intake", "submit",
            "--title", "Project B",
            "--goal", "Goal B",
            "--criterion", "c1",
            "--depends-on-project", prj_a,
            token=self.tok_plain,
        )
        self.assertEqual(rc_b, 0)
        prj_b = resp_b["data"]["id"]

        # 3. Create task in Project B assigned to ibx
        rc_tb, resp_tb = self.harness.cli(
            "task", "create",
            "--project", prj_b,
            "--title", "Task in B",
            "--spec", "Spec B",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_tb, 0)
        tsk_b = resp_tb["data"]["id"]

        # Check task in B: must stay assigned, NOT queued (because PRJ_A is not completed)
        rc_sb, resp_sb = self.harness.cli("task", "show", tsk_b, token=self.tok_lead1)
        self.assertEqual(rc_sb, 0)
        self.assertEqual(resp_sb["data"]["status"], "assigned")

        # 4. Complete Project A with valid criteria report
        c1_id = resp_a["data"]["criteria"][0]["id"]
        report_json = json.dumps({
            "criteria": [{"id": c1_id, "met": True, "evidence": "Verified"}],
            "summary": "Finished A",
        })
        rc_comp, resp_comp = self.harness.cli(
            "project", "complete", prj_a,
            "--report", "-",
            stdin=report_json,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_comp, 0)
        self.assertTrue(resp_comp["ok"])

        # 5. Check task in B: now PRJ_A is completed, scheduler promotes it to 'queued'
        rc_sb2, resp_sb2 = self.harness.cli("task", "show", tsk_b, token=self.tok_lead1)
        self.assertEqual(rc_sb2, 0)
        self.assertEqual(resp_sb2["data"]["status"], "queued")
        self.assertIsNotNone(resp_sb2["data"]["queued_at"])

    def test_d12_queue_ordering_and_blocked_reason(self) -> None:
        """ACC: D12 - Queue 3 tasks with priorities 0, 5, 0 -> priority 5 is position 1, others FIFO."""
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Project D12",
            "--goal", "Goal D12",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        prj = resp_p["data"]["id"]

        # Create Task 1 (priority 0)
        rc1, r1 = self.harness.cli(
            "task", "create",
            "--project", prj,
            "--title", "T1 prio 0",
            "--spec", "s1",
            "--assignee", "ibx",
            "--priority", "0",
            token=self.tok_lead1,
        )
        self.assertEqual(rc1, 0)
        t1_id = r1["data"]["id"]

        # Create Task 2 (priority 5)
        rc2, r2 = self.harness.cli(
            "task", "create",
            "--project", prj,
            "--title", "T2 prio 5",
            "--spec", "s2",
            "--assignee", "ibx",
            "--priority", "5",
            token=self.tok_lead1,
        )
        self.assertEqual(rc2, 0)
        t2_id = r2["data"]["id"]

        # Create Task 3 (priority 0)
        rc3, r3 = self.harness.cli(
            "task", "create",
            "--project", prj,
            "--title", "T3 prio 0",
            "--spec", "s3",
            "--assignee", "ibx",
            "--priority", "0",
            token=self.tok_lead1,
        )
        self.assertEqual(rc3, 0)
        t3_id = r3["data"]["id"]

        # List queued tasks for ibx
        rc_l, resp_l = self.harness.cli(
            "task", "list",
            "--assignee", "ibx",
            "--status", "queued",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_l, 0)
        items = resp_l["data"]["items"]
        self.assertEqual(len(items), 3)

        # Ordering by priority DESC, queued_at ASC, id ASC
        # Items are sorted priority 5 first (T2), then T1, then T3
        by_id = {it["id"]: it for it in items}
        self.assertEqual(by_id[t2_id]["queue_position"], 1)
        self.assertEqual(by_id[t1_id]["queue_position"], 2)
        self.assertEqual(by_id[t3_id]["queue_position"], 3)

        # ibx is inbox mode -> blocked_reason is "waiting_for_start"
        for it in items:
            self.assertEqual(it["blocked_reason"], "waiting_for_start")

        # Now test pause on project: blocked_reason becomes "project_paused"
        rc_pause, _ = self.harness.cli("project", "pause", prj, user=True)
        self.assertEqual(rc_pause, 0)

        rc_l2, resp_l2 = self.harness.cli(
            "task", "list",
            "--assignee", "ibx",
            "--status", "queued",
            token=self.tok_lead1,
        )
        for it in resp_l2["data"]["items"]:
            self.assertEqual(it["blocked_reason"], "project_paused")

    def test_task_dep_removal_promotes_to_queued(self) -> None:
        """Verify removing a blocking dependency promotes task to queued."""
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Project Dep Removal",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        prj = resp_p["data"]["id"]

        # T1 on fx
        rc1, r1 = self.harness.cli(
            "task", "create",
            "--project", prj,
            "--title", "T1",
            "--spec", "s1",
            "--assignee", "fx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc1, 0)
        t1_id = r1["data"]["id"]

        # T2 on ibx, depends on T1
        rc2, r2 = self.harness.cli(
            "task", "create",
            "--project", prj,
            "--title", "T2",
            "--spec", "s2",
            "--assignee", "ibx",
            "--depends-on", t1_id,
            token=self.tok_lead1,
        )
        self.assertEqual(rc2, 0)
        t2_id = r2["data"]["id"]

        # T2 must stay assigned because T1 is not accepted or merged
        rc_s2, resp_s2 = self.harness.cli("task", "show", t2_id, token=self.tok_lead1)
        self.assertEqual(rc_s2, 0)
        self.assertEqual(resp_s2["data"]["status"], "assigned")

        # Remove dependency from T2 on T1
        rc_rm, resp_rm = self.harness.cli(
            "dep", "remove", t2_id,
            "--on", t1_id,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_rm, 0)

        # Now that dependency is removed, T2 is promoted to queued
        rc_s2_after, resp_s2_after = self.harness.cli("task", "show", t2_id, token=self.tok_lead1)
        self.assertEqual(rc_s2_after, 0)
        self.assertEqual(resp_s2_after["data"]["status"], "queued")
        self.assertIsNotNone(resp_s2_after["data"]["queued_at"])

    def test_scheduler_t6_model_not_allowed(self) -> None:
        """T6 - Queued task whose model is no longer allowed for its assignee fails and notifies lead."""
        create_agent(
            self.harness,
            "fx_t6",
            adapter="fake",
            delivery="launch",
            allowed_models=["m-a", "m-b"],
            default_model="m-a",
        )

        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Project T6",
            "--goal", "Goal T6",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        prj = resp_p["data"]["id"]

        # Queue a task with model m-b
        rc_task, resp_task = self.harness.cli(
            "task", "create",
            "--project", prj,
            "--title", "Task T6",
            "--spec", "Spec T6",
            "--assignee", "fx_t6",
            "--model", "m-b",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_task, 0)
        task_id = resp_task["data"]["id"]

        # Task should be promoted to queued
        rc_s, resp_s = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc_s, 0)
        self.assertEqual(resp_s["data"]["status"], "queued")
        v_queued = resp_s["data"]["version"]

        # User updates fx_t6 allowed models to exclude m-b [U]
        rc_u, resp_u = self.harness.cli("agent", "update", "fx_t6", "--allowed-model", "m-a", user=True)
        self.assertEqual(rc_u, 0)

        # Wait for scheduler tick to fail the queued task
        def task_failed() -> bool:
            rc_check, resp_check = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
            return rc_check == 0 and resp_check["data"]["status"] == "failed"

        self.assertTrue(self.harness.wait_for(task_failed, timeout=5.0))

        # Assert status failed + reason + version bump
        rc_final, resp_final = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc_final, 0)
        self.assertEqual(resp_final["data"]["status"], "failed")
        self.assertGreater(resp_final["data"]["version"], v_queued)
        self.assertIsNotNone(resp_final["data"]["failure"])
        self.assertEqual(resp_final["data"]["failure"]["reason"], "model_not_allowed")
        self.assertEqual(resp_final["data"]["failure"]["details"]["model"], "m-b")
        self.assertEqual(resp_final["data"]["failure"]["details"]["allowed"], ["m-a"])

        # Assert lead inbox message
        rc_inbox, resp_inbox = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_inbox, 0)
        items = resp_inbox["data"]["items"]
        failed_items = [it for it in items if it["type"] == "task.failed"]
        self.assertEqual(len(failed_items), 1)
        self.assertEqual(failed_items[0]["payload"]["task_id"], task_id)
        self.assertEqual(failed_items[0]["payload"]["reason"], "model_not_allowed")
        self.assertEqual(failed_items[0]["payload"]["details"]["model"], "m-b")


if __name__ == "__main__":
    unittest.main()
