"""Integration tests for inbox-mode task execution: start, heartbeat, progress, submit, fail, leases, fencing.
Checklist items: ACC: D14, ACC: J3 (partial), ACC: A8.
"""

import concurrent.futures
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional

import mesh.constants as c
from tests.helpers import MeshHarness, assert_h5, serial
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

    rc, resp = harness.cli(*args, user=True)
    assert rc == 0, f"agent create {name} failed: {resp}"

    rc_tok, tok_resp = harness.cli("agent", "token", "issue", name, user=True)
    assert rc_tok == 0, f"token issue {name} failed: {tok_resp}"
    token = tok_resp["data"]["token"]

    tok_dir = os.path.join(harness.home, "tokens")
    os.makedirs(tok_dir, exist_ok=True)
    tok_path = os.path.join(tok_dir, f"{name}.token")
    with open(tok_path, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(tok_path, 0o600)
    return token


class TestExecutionInbox(unittest.TestCase):
    """Integration tests for inbox-mode task execution and leases."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

        # Standard Fixture F1. The six agents are independent, so they are created
        # concurrently (m3-a1-speed): same CLI calls, same agents and tokens, and the
        # daemon serialises the writes; only the order of the agent.created events and
        # agent ids differs, and no test here reads either.
        specs = {
            "lead1": dict(adapter="none", delivery="inbox", can_lead=True),
            "lead2": dict(adapter="none", delivery="inbox", can_lead=True),
            "grok": dict(adapter="none", delivery="inbox", capabilities=["intake", "relay"]),
            "plain": dict(adapter="none", delivery="inbox", capabilities=["intake"]),
            "ibx": dict(adapter="none", delivery="inbox", max_concurrency=1),
            "fx": dict(adapter="fake", delivery="launch", max_concurrency=1),
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(specs)) as pool:
            futures = {name: pool.submit(create_agent, self.harness, name, **kw) for name, kw in specs.items()}
            toks = {name: fut.result() for name, fut in futures.items()}
        self.tok_lead1 = toks["lead1"]
        self.tok_lead2 = toks["lead2"]
        self.tok_grok = toks["grok"]
        self.tok_plain = toks["plain"]
        self.tok_ibx = toks["ibx"]
        self.tok_fx = toks["fx"]

        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)

        # Create active project PRJ
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "PRJ Execution",
            "--goal", "Test execution",
            "--criterion", "C1",
            "--lead", "lead1",
            token=self.tok_grok,
        )
        self.assertEqual(rc_p, 0)
        self.project_id = resp_p["data"]["id"]
        self.crit_id = resp_p["data"]["criteria"][0]["id"]

        # M1c run 2 wires launch T5. These M1b tests assert on the queued state of
        # launch tasks, so they hold launch starts (testing hook, plan §9.3).
        self.harness.hold_launch_starts()

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def test_d14_inbox_concurrency_limit(self) -> None:
        """ACC: D14 - Two tasks queued for ibx (max 1); task start T1 succeeds; task start T2 gets rc 11 CONCURRENCY_LIMIT; T2 stays queued."""
        # Create Task 1
        rc_t1, resp_t1 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task 1",
            "--spec", "Spec 1",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t1, 0)
        t1 = resp_t1["data"]["id"]

        # Create Task 2
        rc_t2, resp_t2 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task 2",
            "--spec", "Spec 2",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t2, 0)
        t2 = resp_t2["data"]["id"]

        # Wait until queued
        rc_w1, _ = self.harness.cli("task", "wait", t1, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w1, 0)
        rc_w2, _ = self.harness.cli("task", "wait", t2, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2, 0)

        # Before start: ibx has free slot -> T1 and T2 show waiting_for_start
        rc_list0, resp_list0 = self.harness.cli("task", "list", "--status", "queued", token=self.tok_lead1)
        self.assertEqual(rc_list0, 0)
        items0 = {item["id"]: item for item in resp_list0["data"]["items"]}
        self.assertEqual(items0[t1]["blocked_reason"], "waiting_for_start")
        self.assertEqual(items0[t2]["blocked_reason"], "waiting_for_start")

        # Start T1 [@ibx] -> rc 0 with run_id, fence, lease_ttl_s, workspace
        rc_s1, resp_s1 = self.harness.cli("task", "start", t1, token=self.tok_ibx)
        self.assertEqual(rc_s1, 0)
        data_s1 = resp_s1["data"]
        run1 = data_s1["run_id"]
        fence1 = data_s1["fence"]
        ttl1 = data_s1["lease_ttl_s"]
        ws1 = data_s1["workspace"]
        self.assertIsNotNone(run1)
        self.assertEqual(fence1, 1)
        self.assertGreater(ttl1, 0)
        self.assertTrue(os.path.isdir(ws1))

        # Start T2 [@ibx] -> rc 11 CONCURRENCY_LIMIT; T2 stays queued
        rc_s2, resp_s2 = self.harness.cli("task", "start", t2, token=self.tok_ibx)
        self.assertEqual(rc_s2, c.EXIT_BUSY)
        self.assertEqual(resp_s2["error"]["code"], c.CONCURRENCY_LIMIT)

        # Verify T2 is still queued and reports blocked_reason: "concurrency_limit"
        rc_show2, resp_show2 = self.harness.cli("task", "show", t2, token=self.tok_lead1)
        self.assertEqual(rc_show2, 0)
        self.assertEqual(resp_show2["data"]["status"], "queued")
        self.assertEqual(resp_show2["data"]["blocked_reason"], "concurrency_limit")

        # Submit T1 to free slot
        rc_sub1, _ = self.harness.cli("task", "submit", t1, "--summary", "Done T1", "--run", run1, "--fence", str(fence1), token=self.tok_ibx)
        self.assertEqual(rc_sub1, 0)

        # Now ibx has free slot -> T2 reports blocked_reason: "waiting_for_start"
        rc_show2b, resp_show2b = self.harness.cli("task", "show", t2, token=self.tok_lead1)
        self.assertEqual(rc_show2b, 0)
        self.assertEqual(resp_show2b["data"]["blocked_reason"], "waiting_for_start")

        # Now start T2 [@ibx] succeeds -> rc 0
        rc_s2b, resp_s2b = self.harness.cli("task", "start", t2, token=self.tok_ibx)
        self.assertEqual(rc_s2b, 0)
        self.assertEqual(resp_s2b["data"]["fence"], 1)

    def test_inbox_happy_path_execution_and_fencing(self) -> None:
        """Happy path: start, heartbeat extends lease, progress writes event, submit -> submitted, lead notified."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Happy Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        # Wait until queued
        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # 1. Start task
        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        run_id = resp_st["data"]["run_id"]
        fence = resp_st["data"]["fence"]
        ws_path = resp_st["data"]["workspace"]

        # Verify workspace and run dirs exist with 0700 permissions
        self.assertTrue(os.path.isdir(ws_path))
        ws_mode = stat.S_IMODE(os.stat(ws_path).st_mode)
        self.assertEqual(ws_mode, 0o700)
        run_dir = os.path.join(self.harness.home, "runs", run_id)
        self.assertTrue(os.path.isdir(run_dir))
        run_mode = stat.S_IMODE(os.stat(run_dir).st_mode)
        self.assertEqual(run_mode, 0o700)

        # 2. Heartbeat extends expires_at. Let the lease age a little first so the extension
        # is observable (expires_at only moves on heartbeat).
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        cur_l = conn.execute("SELECT expires_at FROM leases WHERE key = ?;", (f"task:{tsk}",))
        initial_expires_at = cur_l.fetchone()[0]
        conn.close()

        time.sleep(1.0)
        rc_hb, resp_hb = self.harness.cli("task", "heartbeat", tsk, "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_hb, 0)
        new_expires_at = resp_hb["data"]["expires_at"]
        self.assertGreater(new_expires_at, initial_expires_at)

        # 3. Progress writes task.progress event
        rc_prg, resp_prg = self.harness.cli("task", "progress", tsk, "--message", "Processing step 1", "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_prg, 0)

        # 3b. Exactly one task.progress event for this task, carrying the message
        rc_pev, resp_pev = self.harness.cli("event", "list", "--task", tsk, "--type", "task.progress", token=self.tok_lead1)
        self.assertEqual(rc_pev, 0)
        progress_events = resp_pev["data"]["items"]
        self.assertEqual(len(progress_events), 1)
        self.assertEqual(progress_events[0]["payload"].get("message"), "Processing step 1")
        self.assertEqual(progress_events[0]["run_id"], run_id)

        # 4. Submit deliverable
        deliverable_file = os.path.join(self.harness.home, "deliv.json")
        deliv_obj = {"result": "ok", "items_processed": 42}
        with open(deliverable_file, "w", encoding="utf-8") as fp:
            json.dump(deliv_obj, fp)

        rc_sub, resp_sub = self.harness.cli("task", "submit", tsk, "--deliverable", deliverable_file, "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_sub, 0)
        self.assertEqual(resp_sub["data"]["status"], "submitted")
        self.assertEqual(resp_sub["data"]["deliverable"], deliv_obj)
        self.assertEqual(resp_sub["data"]["submitted_by"], "ibx")

        # 5. Verify events assigned -> queued -> running -> submitted in order
        rc_ev, resp_ev = self.harness.cli("event", "list", "--project", self.project_id, user=True)
        self.assertEqual(rc_ev, 0)
        task_evs = [e for e in resp_ev["data"]["items"] if e.get("task_id") == tsk and e.get("to_state")]
        states = [e["to_state"] for e in task_evs]
        self.assertEqual(states, ["assigned", "queued", "running", "submitted"])

        # 6. Verify lead1's inbox has exactly one task.submitted message
        rc_inb, resp_inb = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_inb, 0)
        submitted_msgs = [m for m in resp_inb["data"]["items"] if m.get("type") == "task.submitted" and m.get("payload", {}).get("task_id") == tsk or m.get("payload_raw", {}).get("task_id") == tsk]
        # In outbox, type is task.submitted
        sub_msgs = [m for m in resp_inb["data"]["items"] if m.get("type") == "task.submitted"]
        self.assertEqual(len(sub_msgs), 1)

        # 7. Verify lease row is deleted from leases table
        conn = sqlite3.connect(db_path)
        lease_row = conn.execute("SELECT * FROM leases WHERE key = ?;", (f"task:{tsk}",)).fetchone()
        self.assertIsNone(lease_row)

        # 8. Verify run row is status='exited', end_reason='submitted'
        run_row = conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_id,)).fetchone()
        conn.close()
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row[0], "exited")
        self.assertEqual(run_row[1], "submitted")

    def test_j3_idempotent_submit_replay(self) -> None:
        """ACC: J3 - Replay the same task submit with the same --idempotency-key -> idempotent_replay:true; outbox has exactly 1 task.submitted."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Replay Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        run_id = resp_st["data"]["run_id"]
        fence = resp_st["data"]["fence"]

        # First submit with idempotency key
        idem_key = "submit_key_12345"
        rc_sub1, resp_sub1 = self.harness.cli(
            "task", "submit", tsk,
            "--summary", "Completed first try",
            "--run", run_id,
            "--fence", str(fence),
            "--idempotency-key", idem_key,
            token=self.tok_ibx,
        )
        self.assertEqual(rc_sub1, 0)
        self.assertFalse(resp_sub1.get("idempotent_replay", False))

        # Second submit with same idempotency key -> idempotent_replay: true
        rc_sub2, resp_sub2 = self.harness.cli(
            "task", "submit", tsk,
            "--summary", "Completed first try",
            "--run", run_id,
            "--fence", str(fence),
            "--idempotency-key", idem_key,
            token=self.tok_ibx,
        )
        self.assertEqual(rc_sub2, 0)
        self.assertTrue(resp_sub2.get("idempotent_replay", False))

        # Check lead1's inbox: still exactly 1 task.submitted message
        rc_inb, resp_inb = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_inb, 0)
        sub_msgs = [m for m in resp_inb["data"]["items"] if m.get("type") == "task.submitted"]
        self.assertEqual(len(sub_msgs), 1)

    def test_inbox_fail_path(self) -> None:
        """Fail path: task fail -> failed, lead1 gets one task.failed message with reason, attempt and max_attempts."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Fail Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        run_id = resp_st["data"]["run_id"]
        fence = resp_st["data"]["fence"]

        # Call task fail
        fail_msg = "Unrecoverable parse error on line 42"
        rc_fail, resp_fail = self.harness.cli("task", "fail", tsk, "--reason", fail_msg, "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_fail, 0)
        self.assertEqual(resp_fail["data"]["status"], "failed")
        self.assertEqual(resp_fail["data"]["reason"], "reported")

        # Verify task show
        rc_show, resp_show = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_show, 0)
        self.assertEqual(resp_show["data"]["status"], "failed")
        self.assertIsNotNone(resp_show["data"]["failure"])
        self.assertEqual(resp_show["data"]["failure"]["reason"], "reported")
        self.assertEqual(resp_show["data"]["failure"]["details"]["message"], fail_msg)

        # Verify lead1's inbox gets exactly one task.failed message
        rc_inb, resp_inb = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_inb, 0)
        fail_msgs = [m for m in resp_inb["data"]["items"] if m.get("type") == "task.failed"]
        self.assertEqual(len(fail_msgs), 1)
        payload = fail_msgs[0].get("payload", {})
        self.assertEqual(payload.get("reason"), "reported")
        self.assertEqual(payload.get("attempt"), 1)
        self.assertEqual(payload.get("max_attempts"), 3)

        # Lease is deleted and run ended
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        lease_row = conn.execute("SELECT * FROM leases WHERE key = ?;", (f"task:{tsk}",)).fetchone()
        run_row = conn.execute("SELECT status, end_reason FROM runs WHERE id = ?;", (run_id,)).fetchone()
        conn.close()
        self.assertIsNone(lease_row)
        self.assertEqual(run_row[0], "exited")
        self.assertEqual(run_row[1], "failed")

    def test_authority_task_submit_and_start(self) -> None:
        """Authority: task submit by lead1 (not assignee) -> rc 4; task start by plain (not assignee) -> rc 4."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Auth Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # plain attempts task start on ibx's task -> rc 4 NOT_ASSIGNEE
        rc_st_plain, resp_st_plain = self.harness.cli("task", "start", tsk, token=self.tok_plain)
        self.assertEqual(rc_st_plain, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_st_plain["error"]["code"], c.NOT_ASSIGNEE)

        # Start task properly by assignee ibx
        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        run_id = resp_st["data"]["run_id"]
        fence = resp_st["data"]["fence"]

        # lead1 attempts task submit on ibx's task -> rc 4 NOT_ASSIGNEE
        rc_sub_lead, resp_sub_lead = self.harness.cli("task", "submit", tsk, "--summary", "Lead submits", "--run", run_id, "--fence", str(fence), token=self.tok_lead1)
        self.assertEqual(rc_sub_lead, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_sub_lead["error"]["code"], c.NOT_ASSIGNEE)

    def test_task_start_on_launch_mode_agent_rejected(self) -> None:
        """Coordinator decision Q5: Starting a launch-mode agent via task start returns rc 6 INVALID_TRANSITION (reason: launch_mode)."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Launch Task",
            "--spec", "Spec",
            "--assignee", "fx",
            "--model", "fake-model",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # Start via task start -> rc 6 INVALID_TRANSITION, reason: launch_mode
        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_fx)
        self.assertEqual(rc_st, c.EXIT_CONFLICT)
        self.assertEqual(resp_st["error"]["code"], c.INVALID_TRANSITION)
        self.assertEqual(resp_st["error"]["details"]["reason"], "launch_mode")

        # Task must still remain queued
        rc_show, resp_show = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_show, 0)
        self.assertEqual(resp_show["data"]["status"], "queued")

    def test_a8_doctor_check_invariants_while_running(self) -> None:
        """ACC: A8 - doctor --check-invariants returns rc 0 with empty violations list while task is running."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "A8 Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)

        # Run doctor --check-invariants while task is running
        rc_doc, resp_doc = self.harness.cli("doctor", "--check-invariants")
        self.assertEqual(rc_doc, 0)
        self.assertEqual(resp_doc["data"]["violations"], [])

    def test_stale_fence_and_lease_expired_on_heartbeat_and_submit(self) -> None:
        """Fencing checks: wrong fence/run -> rc 9 FENCE_MISMATCH; missing lease -> rc 9 LEASE_EXPIRED."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Fence Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        run_id = resp_st["data"]["run_id"]
        fence = resp_st["data"]["fence"]

        # Wrong fence on heartbeat -> rc 9 FENCE_MISMATCH
        rc_hb_bad, resp_hb_bad = self.harness.cli("task", "heartbeat", tsk, "--run", run_id, "--fence", "999", token=self.tok_ibx)
        self.assertEqual(rc_hb_bad, c.EXIT_STALE)
        self.assertEqual(resp_hb_bad["error"]["code"], c.FENCE_MISMATCH)

        # Wrong run on progress -> rc 9 FENCE_MISMATCH
        rc_prg_bad, resp_prg_bad = self.harness.cli("task", "progress", tsk, "--message", "msg", "--run", "run_wrong", "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_prg_bad, c.EXIT_STALE)
        self.assertEqual(resp_prg_bad["error"]["code"], c.FENCE_MISMATCH)

        # Wrong fence on submit -> rc 9 FENCE_MISMATCH
        rc_sub_bad, resp_sub_bad = self.harness.cli("task", "submit", tsk, "--summary", "Done", "--run", run_id, "--fence", "999", token=self.tok_ibx)
        self.assertEqual(rc_sub_bad, c.EXIT_STALE)
        self.assertEqual(resp_sub_bad["error"]["code"], c.FENCE_MISMATCH)

        # Submit properly
        rc_sub_ok, resp_sub_ok = self.harness.cli("task", "submit", tsk, "--summary", "Done", "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_sub_ok, 0)

        # Submit again after task is already submitted (lease gone) -> rc 9 LEASE_EXPIRED
        rc_sub_post, resp_sub_post = self.harness.cli("task", "submit", tsk, "--summary", "Done", "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_sub_post, c.EXIT_STALE)
        self.assertEqual(resp_sub_post["error"]["code"], c.LEASE_EXPIRED)

        # Heartbeat after task is already submitted (lease gone) -> rc 9 LEASE_EXPIRED
        rc_hb_post, resp_hb_post = self.harness.cli("task", "heartbeat", tsk, "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_hb_post, c.EXIT_STALE)
        self.assertEqual(resp_hb_post["error"]["code"], c.LEASE_EXPIRED)

    def test_f7_cli_accept_running_task_rejected(self) -> None:
        """ACC: F7 - CLI spot check: task accept <running task> [@lead1] -> rc 6 INVALID_TRANSITION."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "F7 Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # Start task -> running
        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)

        # Lead attempts task accept on running task -> rc 6 INVALID_TRANSITION
        rc_acc, resp_acc = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc_acc, c.EXIT_CONFLICT)
        self.assertEqual(resp_acc["error"]["code"], c.INVALID_TRANSITION)

    @serial
    def test_h6_inbox_lease_expiry_and_heartbeat(self) -> None:
        """ACC: H6 - Inbox lease expiry sweep and heartbeat extension."""
        # Set lease.ttl_s to 2s
        rc, _ = self.harness.cli("config", "set", "lease.ttl_s", "2", user=True)
        self.assertEqual(rc, 0)

        # Task A: start, no heartbeat -> failed with lease_expired; later submit -> 9 LEASE_EXPIRED
        rc_ta, resp_ta = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task A",
            "--spec", "Spec A",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_ta, 0)
        tskA = resp_ta["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tskA, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_sta, resp_sta = self.harness.cli("task", "start", tskA, token=self.tok_ibx)
        self.assertEqual(rc_sta, 0)
        run_id_A = resp_sta["data"]["run_id"]
        fence_A = resp_sta["data"]["fence"]

        # Wait for task A to fail by lease expiry (ttl is 2s, wait up to 10s)
        rc_wa, resp_wa = self.harness.cli("task", "wait", tskA, "--until", "state:failed", "--timeout", "10s", token=self.tok_lead1)
        self.assertEqual(rc_wa, 0)
        self.assertEqual(resp_wa["data"]["status"], "failed")

        rc_sha, resp_sha = self.harness.cli("task", "show", tskA, token=self.tok_lead1)
        self.assertEqual(rc_sha, 0)
        self.assertEqual(resp_sha["data"]["failure"]["reason"], "lease_expired")

        # Late submit -> rc 9 LEASE_EXPIRED
        rc_sub, resp_sub = self.harness.cli("task", "submit", tskA, "--summary", "Late", "--run", run_id_A, "--fence", str(fence_A), token=self.tok_ibx)
        self.assertEqual(rc_sub, c.EXIT_STALE)
        self.assertEqual(resp_sub["error"]["code"], c.LEASE_EXPIRED)
        # Dogfood: the remote worker is told why and what to do (code unchanged).
        self.assertIn("lease expired", resp_sub["error"]["message"])
        self.assertIn("Stop work", resp_sub["error"]["message"])
        self.assertEqual(resp_sub["error"]["details"]["run_end_reason"], "lease_expired")
        self.assertEqual(resp_sub["error"]["details"]["task_status"], "failed")
        rc_hbl, resp_hbl = self.harness.cli("task", "heartbeat", tskA, "--run", run_id_A, "--fence", str(fence_A), token=self.tok_ibx)
        self.assertEqual(rc_hbl, c.EXIT_STALE)
        self.assertEqual(resp_hbl["error"]["code"], c.LEASE_EXPIRED)
        self.assertIn("lease expired", resp_hbl["error"]["message"])

        # Task B: heartbeat every 0.6s for 5s -> stays running; then submit -> rc 0
        rc_tb, resp_tb = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task B",
            "--spec", "Spec B",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_tb, 0)
        tskB = resp_tb["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tskB, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_stb, resp_stb = self.harness.cli("task", "start", tskB, token=self.tok_ibx)
        self.assertEqual(rc_stb, 0)
        run_id_B = resp_stb["data"]["run_id"]
        fence_B = resp_stb["data"]["fence"]

        # Heartbeat every 0.6s for 4s (= 2x the 2s ttl; was 5 s, m3-a1-speed): without the
        # heartbeats the lease would have lapsed twice over, as Task A just showed.
        start_t = time.monotonic()
        while time.monotonic() - start_t < 4.0:
            time.sleep(0.6)
            rc_hb, resp_hb = self.harness.cli("task", "heartbeat", tskB, "--run", run_id_B, "--fence", str(fence_B), token=self.tok_ibx)
            self.assertEqual(rc_hb, 0)

        # Still running
        rc_shb, resp_shb = self.harness.cli("task", "show", tskB, token=self.tok_lead1)
        self.assertEqual(rc_shb, 0)
        self.assertEqual(resp_shb["data"]["status"], "running")

        # Submit -> rc 0
        rc_sub_b, resp_sub_b = self.harness.cli("task", "submit", tskB, "--summary", "Done B", "--run", run_id_B, "--fence", str(fence_B), token=self.tok_ibx)
        self.assertEqual(rc_sub_b, 0)

    @serial
    def test_h7_inbox_no_response(self) -> None:
        """ACC: H7 - Inbox ack timeout failure (no_response) and lead notification."""
        # Set inbox.ack_timeout_s to 1s (testing-mode minimum; was 2 s, m3-a1-speed). Nothing
        # after the task is queued is timed: the test only waits for the no_response failure.
        rc, _ = self.harness.cli("config", "set", "inbox.ack_timeout_s", "1", user=True)
        self.assertEqual(rc, 0)

        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task No Response",
            "--spec", "Spec NR",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        # Never start it. Wait until state:failed
        rc_wf, resp_wf = self.harness.cli("task", "wait", tsk, "--until", "state:failed", "--timeout", "10s", token=self.tok_lead1)
        self.assertEqual(rc_wf, 0)
        self.assertEqual(resp_wf["data"]["status"], "failed")

        # The task was queued and offered before it failed. Read from the event log rather
        # than caught live with `task wait --until state:queued`: with a 1 s ack timeout a
        # slow client could miss the transient queued state (m3-a1-speed fix round, Linear
        # MINOR). The log keeps every transition, so this cannot race.
        from mesh.core.clock import parse_utc

        conn = self.harness.db()
        try:
            evs = conn.execute(
                "SELECT seq, ts, type, from_state, to_state FROM events WHERE task_id = ? "
                "AND type IN ('task.queued', 'task.offered', 'task.failed') ORDER BY seq ASC;",
                (tsk,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([e["type"] for e in evs], ["task.queued", "task.offered", "task.failed"],
                         [dict(e) for e in evs])
        self.assertEqual(evs[0]["to_state"], "queued")
        self.assertEqual(evs[2]["from_state"], "queued")
        # It stayed queued for at least the 1 s ack timeout before no_response.
        held = parse_utc(evs[2]["ts"]).timestamp() - parse_utc(evs[1]["ts"]).timestamp()
        self.assertGreaterEqual(held, 1.0, f"failed after only {held:.2f}s in queued")

        rc_sh, resp_sh = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_sh, 0)
        self.assertEqual(resp_sh["data"]["failure"]["reason"], "no_response")

        # Lead1's inbox has exactly one task.failed message
        rc_inbox, resp_inbox = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_inbox, 0)
        failed_msgs = [m for m in resp_inbox["data"]["items"] if m["type"] == "task.failed" and m["payload"].get("task_id") == tsk]
        self.assertEqual(len(failed_msgs), 1)
        self.assertEqual(failed_msgs[0]["payload"]["reason"], "no_response")

    @serial
    def test_h4_fence_mismatch_and_recovery(self) -> None:
        """ACC: H4 - Stale write after recovery rejected with FENCE_MISMATCH, current run unaffected."""
        rc, _ = self.harness.cli("config", "set", "lease.ttl_s", "2", user=True)
        self.assertEqual(rc, 0)

        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task Fence Recovery",
            "--spec", "Spec FR",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # Start attempt 1 (fence 1)
        rc_st1, resp_st1 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st1, 0)
        run_id_1 = resp_st1["data"]["run_id"]
        fence_1 = resp_st1["data"]["fence"]
        self.assertEqual(fence_1, 1)

        # Let the lease expire -> task failed
        rc_wf, _ = self.harness.cli("task", "wait", tsk, "--until", "state:failed", "--timeout", "10s", token=self.tok_lead1)
        self.assertEqual(rc_wf, 0)

        # Lead retries task -> queued
        rc_ret, resp_ret = self.harness.cli("task", "retry", tsk, token=self.tok_lead1)
        self.assertEqual(rc_ret, 0)

        # Start attempt 2 (fence 2)
        rc_st2, resp_st2 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st2, 0)
        run_id_2 = resp_st2["data"]["run_id"]
        fence_2 = resp_st2["data"]["fence"]
        self.assertEqual(fence_2, 2)

        # Stale submit with run 1 / fence 1 -> rc 9 FENCE_MISMATCH
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        lease_before = conn.execute("SELECT * FROM leases WHERE key = ?;", (f"task:{tsk}",)).fetchone()
        conn.close()
        self.assertIsNotNone(lease_before)
        self.assertEqual(lease_before["fence"], 2)
        self.assertEqual(lease_before["run_id"], run_id_2)

        rc_sub_stale, resp_sub_stale = self.harness.cli(
            "task", "submit", tsk,
            "--summary", "Stale submit",
            "--run", run_id_1,
            "--fence", str(fence_1),
            token=self.tok_ibx,
        )
        self.assertEqual(rc_sub_stale, c.EXIT_STALE)
        self.assertEqual(resp_sub_stale["error"]["code"], c.FENCE_MISMATCH)

        # Task stays running, fence-2 lease is unchanged
        rc_sh, resp_sh = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_sh, 0)
        self.assertEqual(resp_sh["data"]["status"], "running")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        lease_after = conn.execute("SELECT * FROM leases WHERE key = ?;", (f"task:{tsk}",)).fetchone()
        conn.close()
        self.assertIsNotNone(lease_after)
        self.assertEqual(lease_after["fence"], 2)
        self.assertEqual(lease_after["run_id"], run_id_2)
        self.assertEqual(lease_after["expires_at"], lease_before["expires_at"])
        self.assertEqual(lease_after["heartbeat_at"], lease_before["heartbeat_at"])

        # Valid submit with run 2 / fence 2 -> rc 0
        rc_sub_valid, resp_sub_valid = self.harness.cli(
            "task", "submit", tsk,
            "--summary", "Valid submit",
            "--run", run_id_2,
            "--fence", str(fence_2),
            token=self.tok_ibx,
        )
        self.assertEqual(rc_sub_valid, 0)

        # Task is now submitted
        rc_sh2, resp_sh2 = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_sh2, 0)
        self.assertEqual(resp_sh2["data"]["status"], "submitted")
        assert_h5(self, self.harness)  # ACC: H5 (review m1c-run3 #6)

    @serial
    def test_h4_stale_task_fail_reports_lease_expired_then_fence_mismatch(self) -> None:
        """ACC: H4 - Stale `task fail` is fenced before the status check: 9 LEASE_EXPIRED after lease expiry, 9 FENCE_MISMATCH after retry."""
        rc, _ = self.harness.cli("config", "set", "lease.ttl_s", "2", user=True)
        self.assertEqual(rc, 0)

        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task Stale Fail",
            "--spec", "Spec SF",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # Attempt 1 (fence 1), then let the lease expire -> task failed
        rc_st1, resp_st1 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st1, 0)
        run_id_1 = resp_st1["data"]["run_id"]
        fence_1 = resp_st1["data"]["fence"]
        self.assertEqual(fence_1, 1)

        rc_wf, _ = self.harness.cli("task", "wait", tsk, "--until", "state:failed", "--timeout", "10s", token=self.tok_lead1)
        self.assertEqual(rc_wf, 0)

        # Stale fail after lease expiry -> 9 LEASE_EXPIRED (not 6 INVALID_TRANSITION)
        rc_fail_stale, resp_fail_stale = self.harness.cli(
            "task", "fail", tsk,
            "--reason", "stale fail after expiry",
            "--run", run_id_1,
            "--fence", str(fence_1),
            token=self.tok_ibx,
        )
        self.assertEqual(rc_fail_stale, c.EXIT_STALE)
        self.assertEqual(resp_fail_stale["error"]["code"], c.LEASE_EXPIRED)

        # Lead retries -> queued, then attempt 2 (fence 2)
        rc_ret, _ = self.harness.cli("task", "retry", tsk, token=self.tok_lead1)
        self.assertEqual(rc_ret, 0)
        rc_wq, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_wq, 0)

        rc_st2, resp_st2 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st2, 0)
        run_id_2 = resp_st2["data"]["run_id"]
        fence_2 = resp_st2["data"]["fence"]
        self.assertEqual(fence_2, 2)

        # Stale fail with run 1 / fence 1 -> 9 FENCE_MISMATCH, task still running
        rc_fail_fence, resp_fail_fence = self.harness.cli(
            "task", "fail", tsk,
            "--reason", "stale fail with old fence",
            "--run", run_id_1,
            "--fence", str(fence_1),
            token=self.tok_ibx,
        )
        self.assertEqual(rc_fail_fence, c.EXIT_STALE)
        self.assertEqual(resp_fail_fence["error"]["code"], c.FENCE_MISMATCH)

        rc_sh, resp_sh = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_sh, 0)
        self.assertEqual(resp_sh["data"]["status"], "running")
        assert_h5(self, self.harness)  # ACC: H5, with the retried run's lease held

        # The current run/fence still works
        rc_fail_ok, resp_fail_ok = self.harness.cli(
            "task", "fail", tsk,
            "--reason", "  real failure  ",
            "--run", run_id_2,
            "--fence", str(fence_2),
            token=self.tok_ibx,
        )
        self.assertEqual(rc_fail_ok, 0)
        self.assertEqual(resp_fail_ok["data"]["status"], "failed")
        # Whitespace-only padding is stripped from the stored reason
        self.assertEqual(resp_fail_ok["data"]["message"], "real failure")
        assert_h5(self, self.harness)  # ACC: H5 (review m1c-run3 #6)

    def test_e11_lead_transfer_authority_on_task_create_and_accept(self) -> None:
        """ACC: E11 - After set-lead to lead2, lead1 cannot create/accept tasks; lead2 can."""
        rc_set, _ = self.harness.cli("project", "set-lead", self.project_id, "lead2", "--reason", "Handover", user=True)
        self.assertEqual(rc_set, 0)

        # Create submitted task by lead2
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "E11 Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead2,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead2)
        self.assertEqual(rc_w, 0)

        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        run_id = resp_st["data"]["run_id"]
        fence = resp_st["data"]["fence"]

        rc_sub, _ = self.harness.cli("task", "submit", tsk, "--summary", "Done", "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_sub, 0)

        # 1. lead1 calls task create -> rc 4 NOT_PROJECT_LEAD
        rc_c1, resp_c1 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Lead1 Task",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_c1, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_c1["error"]["code"], c.NOT_PROJECT_LEAD)

        # 2. lead1 calls task accept -> rc 4 NOT_PROJECT_LEAD
        rc_a1, resp_a1 = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc_a1, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_a1["error"]["code"], c.NOT_PROJECT_LEAD)

        # 3. lead2 calls task create -> rc 0
        rc_c2, resp_c2 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Lead2 Task",
            "--spec", "Spec",
            token=self.tok_lead2,
        )
        self.assertEqual(rc_c2, 0)

        # 4. lead2 calls task accept -> rc 0
        rc_a2, resp_a2 = self.harness.cli("task", "accept", tsk, token=self.tok_lead2)
        self.assertEqual(rc_a2, 0)

    def test_e15_completion_blocked_on_submitted_task_and_pending_approval(self) -> None:
        """ACC: E15 - Valid completion report while task submitted or approval pending returns rc 6 COMPLETION_BLOCKED."""
        report_data = {
            "summary": "Completed PRJ",
            "criteria": [
                {"id": self.crit_id, "met": True, "evidence": "Verified all criteria"},
            ],
        }
        rep_file = os.path.join(self.harness.home, "report.json")
        with open(rep_file, "w", encoding="utf-8") as f:
            json.dump(report_data, f)

        # Create task for ibx, start, submit
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "E15 Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        run_id = resp_st["data"]["run_id"]
        fence = resp_st["data"]["fence"]

        rc_sub, _ = self.harness.cli("task", "submit", tsk, "--summary", "Done", "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_sub, 0)

        # Try to complete while task is submitted -> rc 6 COMPLETION_BLOCKED with task id
        rc_cmp1, resp_cmp1 = self.harness.cli("project", "complete", self.project_id, "--report", rep_file, token=self.tok_lead1)
        self.assertEqual(rc_cmp1, c.EXIT_CONFLICT)
        self.assertEqual(resp_cmp1["error"]["code"], c.COMPLETION_BLOCKED)
        self.assertIn(tsk, resp_cmp1["error"]["details"]["blocking"])

        # Project stays active
        rc_sh1, resp_sh1 = self.harness.cli("project", "show", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_sh1, 0)
        self.assertEqual(resp_sh1["data"]["status"], "active")

        # Accept task
        rc_acc, _ = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc_acc, 0)

        # Trigger gated op to create pending approval
        rc_p, resp_p = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_p, c.EXIT_APPROVAL_REQUIRED)
        apr_id = resp_p["error"]["details"]["approval_id"]

        # Try to complete while approval is pending -> rc 6 COMPLETION_BLOCKED with approval id
        rc_cmp2, resp_cmp2 = self.harness.cli("project", "complete", self.project_id, "--report", rep_file, token=self.tok_lead1)
        self.assertEqual(rc_cmp2, c.EXIT_CONFLICT)
        self.assertEqual(resp_cmp2["error"]["code"], c.COMPLETION_BLOCKED)
        self.assertIn(apr_id, resp_cmp2["error"]["details"]["blocking"])

        # Project stays active
        rc_sh2, resp_sh2 = self.harness.cli("project", "show", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_sh2, 0)
        self.assertEqual(resp_sh2["data"]["status"], "active")

    def test_e16_wait_until_completed_and_completion_notifications(self) -> None:
        """ACC: E16 - Project wait until completed, completion by lead without user command, inbox notifications."""
        report_data = {
            "summary": "Full success",
            "criteria": [
                {"id": self.crit_id, "met": True, "evidence": "Tested thoroughly"},
            ],
        }
        rep_file = os.path.join(self.harness.home, "report_e16.json")
        with open(rep_file, "w", encoding="utf-8") as f:
            json.dump(report_data, f)

        # Report by plain (not lead) -> rc 4 NOT_PROJECT_LEAD
        rc_plain, resp_plain = self.harness.cli("project", "complete", self.project_id, "--report", rep_file, token=self.tok_plain)
        self.assertEqual(rc_plain, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_plain["error"]["code"], c.NOT_PROJECT_LEAD)

        wait_results: Dict[str, Any] = {}

        def _do_wait() -> None:
            rc_w, resp_w = self.harness.cli("project", "wait", self.project_id, "--until", "completed", "--timeout", "60s", token=self.tok_grok,
                                         timeout=180.0)  # the client must outlive the 60 s wait under load
            wait_results["rc"] = rc_w
            wait_results["resp"] = resp_w

        waits_before = self.harness.active_waits()
        t = threading.Thread(target=_do_wait)
        t.start()
        # Proceed only once the project wait is registered in the daemon (active_waits counts
        # project waits since M1c run 5), not after a fixed sleep.
        self.assertTrue(self.harness.wait_for_active_waits(waits_before + 1, timeout=15.0),
                        "project wait did not register")

        # Create task, start, submit, accept
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "E16 Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        run_id = resp_st["data"]["run_id"]
        fence = resp_st["data"]["fence"]

        rc_sub, _ = self.harness.cli("task", "submit", tsk, "--summary", "Done", "--run", run_id, "--fence", str(fence), token=self.tok_ibx)
        self.assertEqual(rc_sub, 0)

        rc_acc, _ = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc_acc, 0)

        # Lead completes project
        rc_cmp, resp_cmp = self.harness.cli("project", "complete", self.project_id, "--report", rep_file, token=self.tok_lead1)
        self.assertEqual(rc_cmp, 0)

        # Wait thread finishes
        t.join(timeout=10.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(wait_results.get("rc"), 0)
        self.assertEqual(wait_results.get("resp", {}).get("data", {}).get("status"), "completed")

        # inbox list [U] has exactly one project.completed with for_user:true
        rc_u, resp_u = self.harness.cli("inbox", "list", user=True)
        self.assertEqual(rc_u, 0)
        u_msgs = [m for m in resp_u["data"]["items"] if m["type"] == "project.completed" and m["project_id"] == self.project_id]
        self.assertEqual(len(u_msgs), 1)
        self.assertTrue(u_msgs[0]["payload"].get("for_user"))

        # inbox list [@grok] has exactly one project.completed with for_user:true
        rc_g, resp_g = self.harness.cli("inbox", "list", token=self.tok_grok)
        self.assertEqual(rc_g, 0)
        g_msgs = [m for m in resp_g["data"]["items"] if m["type"] == "project.completed" and m["project_id"] == self.project_id]
        self.assertEqual(len(g_msgs), 1)
        self.assertTrue(g_msgs[0]["payload"].get("for_user"))

    def test_f5_dependency_promotes_on_accept_and_cycle_rejected(self) -> None:
        """ACC: F5 - T2 depends on T1: while T1 running, T2 stays assigned; accept T1 -> T2 queued; cycle rejected."""
        rc_t1, resp_t1 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task 1",
            "--spec", "Spec 1",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t1, 0)
        t1 = resp_t1["data"]["id"]

        rc_w1, _ = self.harness.cli("task", "wait", t1, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w1, 0)

        rc_t2, resp_t2 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task 2",
            "--spec", "Spec 2",
            "--assignee", "ibx",
            "--depends-on", t1,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t2, 0)
        t2 = resp_t2["data"]["id"]

        # T2 starts assigned because T1 is not accepted
        rc_sh2, resp_sh2 = self.harness.cli("task", "show", t2, token=self.tok_lead1)
        self.assertEqual(rc_sh2, 0)
        self.assertEqual(resp_sh2["data"]["status"], "assigned")

        # ibx starts T1 -> T1 running
        rc_st1, resp_st1 = self.harness.cli("task", "start", t1, token=self.tok_ibx)
        self.assertEqual(rc_st1, 0)
        run_1 = resp_st1["data"]["run_id"]
        fence_1 = resp_st1["data"]["fence"]

        # While T1 is running, T2 stays assigned
        rc_sh2_run, resp_sh2_run = self.harness.cli("task", "show", t2, token=self.tok_lead1)
        self.assertEqual(rc_sh2_run, 0)
        self.assertEqual(resp_sh2_run["data"]["status"], "assigned")

        # Submit T1 -> submitted
        rc_sub1, _ = self.harness.cli("task", "submit", t1, "--summary", "Done T1", "--run", run_1, "--fence", str(fence_1), token=self.tok_ibx)
        self.assertEqual(rc_sub1, 0)

        # Still assigned
        rc_sh2_sub, resp_sh2_sub = self.harness.cli("task", "show", t2, token=self.tok_lead1)
        self.assertEqual(rc_sh2_sub, 0)
        self.assertEqual(resp_sh2_sub["data"]["status"], "assigned")

        # Lead1 accepts T1 -> accepted
        rc_acc, _ = self.harness.cli("task", "accept", t1, token=self.tok_lead1)
        self.assertEqual(rc_acc, 0)

        # T2 now promotes to queued
        rc_w2, resp_w2 = self.harness.cli("task", "wait", t2, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2, 0)
        self.assertEqual(resp_w2["data"]["status"], "queued")

        # Adding dependency T1 depends on T2 creates cycle -> rc 2 DEP_CYCLE
        rc_cycle, resp_cycle = self.harness.cli("dep", "add", t1, "--on", t2, token=self.tok_lead1)
        self.assertEqual(rc_cycle, c.EXIT_USAGE)
        self.assertEqual(resp_cycle["error"]["code"], c.DEP_CYCLE)

    def test_f11_rejected_task_history_and_attempt_count(self) -> None:
        """ACC: F11 - Task rejected once and resubmitted: shows attempt 2, max_attempts, deliverable, rejection history entry."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "F11 Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # Attempt 1: start and submit
        rc_st1, resp_st1 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st1, 0)
        run_id_1 = resp_st1["data"]["run_id"]
        fence_1 = resp_st1["data"]["fence"]

        rc_sub1, _ = self.harness.cli("task", "submit", tsk, "--summary", "Initial draft", "--run", run_id_1, "--fence", str(fence_1), token=self.tok_ibx)
        self.assertEqual(rc_sub1, 0)

        # Lead rejects with reason
        rc_rej, resp_rej = self.harness.cli("task", "reject", tsk, "--reason", "Missing unit tests and comments", token=self.tok_lead1)
        self.assertEqual(rc_rej, 0)

        # Promoted to queued (T13)
        rc_wq, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_wq, 0)

        # Attempt 2: start and submit
        rc_st2, resp_st2 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st2, 0)
        run_id_2 = resp_st2["data"]["run_id"]
        fence_2 = resp_st2["data"]["fence"]

        rc_sub2, _ = self.harness.cli("task", "submit", tsk, "--summary", "Added unit tests and comments", "--run", run_id_2, "--fence", str(fence_2), token=self.tok_ibx)
        self.assertEqual(rc_sub2, 0)

        # task show [@lead1]
        rc_sh, resp_sh = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_sh, 0)
        task_data = resp_sh["data"]
        self.assertEqual(task_data["status"], "submitted")
        self.assertEqual(task_data["attempt"], 2)
        self.assertEqual(task_data["max_attempts"], 3)
        self.assertEqual(task_data["deliverable"], {"summary": "Added unit tests and comments"})

        # History includes rejection entry with reason
        rej_entries = [h for h in task_data.get("history", []) if h["type"] == "task.rejected"]
        self.assertEqual(len(rej_entries), 1)
        self.assertEqual(rej_entries[0]["reason"], "Missing unit tests and comments")

    def test_g4_lead_actions_leave_no_approval_rows(self) -> None:
        """ACC: G4 - Lead runs task create, assign, accept, reject and project complete: DB approvals has 0 rows."""
        # Create dedicated project for G4
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "PRJ G4",
            "--goal", "Test G4 no approvals",
            "--criterion", "C1",
            "--lead", "lead1",
            token=self.tok_grok,
        )
        self.assertEqual(rc_p, 0)
        prj_g4 = resp_p["data"]["id"]
        crit_g4 = resp_p["data"]["criteria"][0]["id"]

        # 1. Lead creates task (unassigned)
        rc_t, resp_t = self.harness.cli("task", "create", "--project", prj_g4, "--title", "G4 Task", "--spec", "Spec", token=self.tok_lead1)
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        # 2. Lead assigns task
        rc_asg, _ = self.harness.cli("task", "assign", tsk, "--to", "ibx", token=self.tok_lead1)
        self.assertEqual(rc_asg, 0)

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # Executor starts & submits
        rc_st1, resp_st1 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st1, 0)
        rc_sub1, _ = self.harness.cli("task", "submit", tsk, "--summary", "V1", "--run", resp_st1["data"]["run_id"], "--fence", str(resp_st1["data"]["fence"]), token=self.tok_ibx)
        self.assertEqual(rc_sub1, 0)

        # 3. Lead rejects
        rc_rej, _ = self.harness.cli("task", "reject", tsk, "--reason", "Please update", token=self.tok_lead1)
        self.assertEqual(rc_rej, 0)

        rc_w2, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2, 0)

        # Executor starts & submits second version
        rc_st2, resp_st2 = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st2, 0)
        rc_sub2, _ = self.harness.cli("task", "submit", tsk, "--summary", "V2", "--run", resp_st2["data"]["run_id"], "--fence", str(resp_st2["data"]["fence"]), token=self.tok_ibx)
        self.assertEqual(rc_sub2, 0)

        # 4. Lead accepts
        rc_acc, _ = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc_acc, 0)

        # 5. Lead completes project
        rep_file = os.path.join(self.harness.home, "report_g4.json")
        with open(rep_file, "w", encoding="utf-8") as f:
            json.dump({"summary": "Done G4", "criteria": [{"id": crit_g4, "met": True, "evidence": "Tested"}]}, f)

        rc_cmp, _ = self.harness.cli("project", "complete", prj_g4, "--report", rep_file, token=self.tok_lead1)
        self.assertEqual(rc_cmp, 0)

        # Assert DB approvals table has 0 rows for this project (and total 0 if no others)
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        cur = conn.execute("SELECT COUNT(*) FROM approvals WHERE project_id = ?;", (prj_g4,))
        count = cur.fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_g16_executor_notes_cannot_bypass_approval_gates(self) -> None:
        """ACC: G16 - Executor notes saying 'approved' cannot satisfy approval gate; op still returns rc 7."""
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "G16 Task",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_st, resp_st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc_st, 0)
        run_id = resp_st["data"]["run_id"]
        fence = resp_st["data"]["fence"]

        def approval_rows() -> Dict[str, str]:
            conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
            try:
                return {r[0]: r[1] for r in conn.execute("SELECT id, status FROM approvals;")}
            finally:
                conn.close()

        approvals_before = approval_rows()
        # Executor submits deliverable claiming user approval
        deliverable = {"notes": "the user approved deploy", "code": 0}
        rc_sub, _ = self.harness.cli(
            "task", "submit", tsk,
            "--deliverable", json.dumps(deliverable),
            "--run", run_id,
            "--fence", str(fence),
            token=self.tok_ibx,
        )
        self.assertEqual(rc_sub, 0)

        # Lead1 runs gated op without --approval -> rc 7 APPROVAL_REQUIRED
        rc_pause, resp_pause = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_pause, c.EXIT_APPROVAL_REQUIRED)
        self.assertEqual(resp_pause["error"]["code"], c.APPROVAL_REQUIRED)

        # No approval changed state: every earlier row is unchanged; the only new row is
        # the pending one the rc 7 call created (A11).
        approvals_after = approval_rows()
        for apr_id, status in approvals_before.items():
            self.assertEqual(approvals_after.get(apr_id), status, apr_id)
        new_rows = {k: v for k, v in approvals_after.items() if k not in approvals_before}
        self.assertEqual(list(new_rows.values()), ["pending"], new_rows)
        self.assertEqual(resp_pause["error"]["details"].get("approval_id"), next(iter(new_rows)), resp_pause)

    @serial
    def test_h15_workspace_fresh_vs_reused_after_lease_expired(self) -> None:
        """ACC: H15 - Fresh workspace vs reuse-workspace on retry after lease expired."""
        rc_cfg, _ = self.harness.cli("config", "set", "lease.ttl_s", "2", user=True)
        self.assertEqual(rc_cfg, 0)

        # Task 1
        rc_t1, resp_t1 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task 1 H15",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t1, 0)
        t1 = resp_t1["data"]["id"]

        rc_w1, _ = self.harness.cli("task", "wait", t1, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w1, 0)

        # Start T1 attempt 1
        rc_st1, resp_st1 = self.harness.cli("task", "start", t1, token=self.tok_ibx)
        self.assertEqual(rc_st1, 0)
        ws_t1_attempt1 = resp_st1["data"]["workspace"]

        # Wait until T1 failed (lease expired)
        rc_wf1, _ = self.harness.cli("task", "wait", t1, "--until", "state:failed", "--timeout", "10s", token=self.tok_lead1)
        self.assertEqual(rc_wf1, 0)

        # Task 2
        rc_t2, resp_t2 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task 2 H15",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t2, 0)
        t2 = resp_t2["data"]["id"]

        rc_w2, _ = self.harness.cli("task", "wait", t2, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2, 0)

        # Start T2 attempt 1
        rc_st2, resp_st2 = self.harness.cli("task", "start", t2, token=self.tok_ibx)
        self.assertEqual(rc_st2, 0)
        ws_t2_attempt1 = resp_st2["data"]["workspace"]

        # Wait until T2 failed (lease expired)
        rc_wf2, _ = self.harness.cli("task", "wait", t2, "--until", "state:failed", "--timeout", "10s", token=self.tok_lead1)
        self.assertEqual(rc_wf2, 0)

        # 1. Lead retries T1 without --reuse-workspace -> fresh workspace
        rc_ret1, _ = self.harness.cli("task", "retry", t1, token=self.tok_lead1)
        self.assertEqual(rc_ret1, 0)

        rc_w1_q, _ = self.harness.cli("task", "wait", t1, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w1_q, 0)

        rc_st1_a2, resp_st1_a2 = self.harness.cli("task", "start", t1, token=self.tok_ibx)
        self.assertEqual(rc_st1_a2, 0)

        rc_sh1, resp_sh1 = self.harness.cli("task", "show", t1, token=self.tok_lead1)
        self.assertEqual(rc_sh1, 0)
        ws_t1_attempt2 = resp_sh1["data"]["workspace_path"]
        self.assertTrue(ws_t1_attempt2.rstrip("/").endswith(f"tasks/{t1}.a2"))
        # Old dir still exists on disk
        self.assertTrue(os.path.isdir(ws_t1_attempt1))

        # 2. Lead retries T2 with --reuse-workspace -> workspace_path unchanged
        rc_ret2, _ = self.harness.cli("task", "retry", t2, "--reuse-workspace", token=self.tok_lead1)
        self.assertEqual(rc_ret2, 0)

        rc_sh2, resp_sh2 = self.harness.cli("task", "show", t2, token=self.tok_lead1)
        self.assertEqual(rc_sh2, 0)
        self.assertEqual(resp_sh2["data"]["workspace_path"], ws_t2_attempt1)

        # 3. Both retry events record the workspace choice (checklist H15 Expect)
        rc_ev1, resp_ev1 = self.harness.cli("event", "list", "--task", t1, "--type", "task.retried", token=self.tok_lead1)
        self.assertEqual(rc_ev1, 0)
        retried_t1 = resp_ev1["data"]["items"]
        self.assertEqual(len(retried_t1), 1)
        self.assertEqual(retried_t1[0]["payload"].get("workspace"), "fresh")
        self.assertTrue(retried_t1[0]["payload"].get("workspace_path", "").rstrip("/").endswith(f"tasks/{t1}.a2"))

        rc_ev2, resp_ev2 = self.harness.cli("event", "list", "--task", t2, "--type", "task.retried", token=self.tok_lead1)
        self.assertEqual(rc_ev2, 0)
        retried_t2 = resp_ev2["data"]["items"]
        self.assertEqual(len(retried_t2), 1)
        self.assertEqual(retried_t2[0]["payload"].get("workspace"), "reused")
        self.assertEqual(retried_t2[0]["payload"].get("workspace_path"), ws_t2_attempt1)


if __name__ == "__main__":
    unittest.main()
