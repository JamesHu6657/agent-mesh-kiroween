"""Integration tests for Inbox subsystem: list, wait, ack per plan §12.2, §12.3, §13.
Checklist items: ACC: J2, ACC: J4.
"""

import concurrent.futures
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

import mesh.constants as c
from tests.helpers import MeshHarness, serial
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
    os.makedirs(tok_dir, exist_ok=True)
    tok_path = os.path.join(tok_dir, f"{name}.token")
    with open(tok_path, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(tok_path, 0o600)
    return token


class TestInbox(unittest.TestCase):
    """Integration tests for inbox wait, list, and ack."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

        # Independent agents, created concurrently (m3-a1-speed; same CLI calls and agents).
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            f_lead1 = pool.submit(create_agent, self.harness, "lead1", delivery="inbox", can_lead=True)
            f_lead2 = pool.submit(create_agent, self.harness, "lead2", delivery="inbox", can_lead=True)
            f_plain = pool.submit(create_agent, self.harness, "plain", delivery="inbox", can_lead=False,
                                  capabilities=["intake"])
            self.tok_lead1 = f_lead1.result()
            self.tok_lead2 = f_lead2.result()
            self.tok_plain = f_plain.result()

        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    @serial
    def test_j2_inbox_wait_timeout_empty(self) -> None:
        """ACC: J2 - m inbox wait --timeout 2s [@plain] with nothing pending -> rc 8 WAIT_TIMEOUT, data.items == []."""
        start = time.monotonic()
        rc, resp = self.harness.cli("inbox", "wait", "--timeout", "2s", token=self.tok_plain)
        dur = time.monotonic() - start

        self.assertEqual(rc, c.EXIT_WAIT_TIMEOUT)
        self.assertEqual(resp.get("exit_code"), c.EXIT_WAIT_TIMEOUT)
        self.assertEqual(resp.get("error", {}).get("code"), c.WAIT_TIMEOUT)
        self.assertEqual(resp.get("data", {}).get("items"), [])
        self.assertGreaterEqual(dur, 1.8)
        self.assertLess(dur, 5.0)

    @serial
    def test_j2_inbox_wait_returns_promptly_on_message(self) -> None:
        """ACC: J2 - inbox wait blocks and returns promptly when an event arrives from another request."""
        def run_wait() -> Any:
            return self.harness.cli("inbox", "wait", "--timeout", "10s", token=self.tok_lead1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(run_wait)
            # Wait until the long-poll is actually registered with the daemon
            self.assertTrue(
                self.harness.wait_for_active_waits(1),
                "inbox wait never registered as an active wait",
            )

            # Trigger an intake submit which fans out project.created to lead1's inbox
            intake_start = time.monotonic()
            rc_p, resp_p = self.harness.cli(
                "intake", "submit",
                "--title", "Proj Wait J2",
                "--goal", "Goal",
                "--criterion", "c1",
                token=self.tok_plain,
            )
            self.assertEqual(rc_p, 0)
            prj_id = resp_p["data"]["id"]

            # Waiter should wake up and complete promptly
            rc_wait, resp_wait = future.result(timeout=5.0)
            wait_dur = time.monotonic() - intake_start

            self.assertEqual(rc_wait, 0)
            self.assertLess(wait_dur, 2.0, f"Wait took too long to return after message arrival: {wait_dur}s")
            items = resp_wait.get("data", {}).get("items", [])
            self.assertTrue(len(items) >= 1)
            self.assertEqual(items[0]["type"], "project.created")
            self.assertEqual(items[0]["project_id"], prj_id)

    def test_j4_inbox_list_and_ack_lifecycle(self) -> None:
        """ACC: J4 - inbox list twice without ack (at-least-once redelivery); ack twice (idempotent); list again."""
        # Submit project to generate project.created in lead1's inbox
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Proj J4",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        prj_id = resp_p["data"]["id"]

        # 1st inbox list: returns message and transitions pending -> delivered
        rc_list1, resp_list1 = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_list1, 0)
        items1 = [it for it in resp_list1.get("data", {}).get("items", []) if it.get("project_id") == prj_id]
        self.assertEqual(len(items1), 1)
        msg_id = items1[0]["id"]

        # 2nd inbox list without ack: message is redelivered (at-least-once delivery)
        rc_list2, resp_list2 = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_list2, 0)
        items2 = [it for it in resp_list2.get("data", {}).get("items", []) if it.get("project_id") == prj_id]
        self.assertEqual(len(items2), 1)
        self.assertEqual(items2[0]["id"], msg_id)
        self.assertEqual(items2[0]["status"], "delivered")

        # 1st ack: marks message acked
        rc_ack1, resp_ack1 = self.harness.cli("inbox", "ack", msg_id, token=self.tok_lead1)
        self.assertEqual(rc_ack1, 0)
        self.assertEqual(resp_ack1.get("data", {}).get("acked"), 1)

        # 2nd ack: idempotent -> rc 0
        rc_ack2, resp_ack2 = self.harness.cli("inbox", "ack", msg_id, token=self.tok_lead1)
        self.assertEqual(rc_ack2, 0)
        self.assertEqual(resp_ack2.get("data", {}).get("acked"), 1)

        # 3rd inbox list: message no longer appears in unacked list
        rc_list3, resp_list3 = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_list3, 0)
        items3 = [it for it in resp_list3.get("data", {}).get("items", []) if it.get("project_id") == prj_id]
        self.assertEqual(len(items3), 0)

        # But inbox list --all shows it as acked
        rc_all, resp_all = self.harness.cli("inbox", "list", "--all", token=self.tok_lead1)
        self.assertEqual(rc_all, 0)
        all_items = [it for it in resp_all.get("data", {}).get("items", []) if it.get("id") == msg_id]
        self.assertEqual(len(all_items), 1)
        self.assertEqual(all_items[0]["status"], "acked")

    def test_inbox_ack_other_agent_message_not_found(self) -> None:
        """Acking someone else's message returns rc 5 NOT_FOUND without leaking existence."""
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Isolation",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)

        rc_list, resp_list = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_list, 0)
        items = resp_list.get("data", {}).get("items", [])
        self.assertTrue(len(items) >= 1)
        lead1_msg_id = items[0]["id"]

        # lead2 tries to ack lead1's message
        rc_ack, resp_ack = self.harness.cli("inbox", "ack", lead1_msg_id, token=self.tok_lead2)
        self.assertEqual(rc_ack, c.EXIT_NOT_FOUND)
        self.assertEqual(resp_ack.get("error", {}).get("code"), c.NOT_FOUND)

    @serial
    def test_inbox_wait_does_not_block_concurrent_requests(self) -> None:
        """A pending inbox wait long-poll does not hold the DB write lock or block concurrent requests."""
        def run_wait() -> Any:
            # 3 s (was 5 s, m3-a1-speed): still longer than the 2 s bound on the concurrent
            # request below, and the wait is asserted to be pending when that request ends.
            return self.harness.cli("inbox", "wait", "--timeout", "3s", token=self.tok_plain)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(run_wait)
            self.assertTrue(
                self.harness.wait_for_active_waits(1),
                "inbox wait never registered as an active wait",
            )

            # Concurrent intake submit must execute without being blocked
            start = time.monotonic()
            rc_p, resp_p = self.harness.cli(
                "intake", "submit",
                "--title", "Concurrent Proj",
                "--goal", "Goal",
                "--criterion", "c1",
                token=self.tok_plain,
            )
            dur = time.monotonic() - start
            self.assertEqual(rc_p, 0)
            self.assertLess(dur, 2.0, f"Concurrent request blocked for {dur}s")
            self.assertFalse(future.done(), "inbox wait ended before the concurrent request")

            # Waiter finishes after timeout (or if plain had no messages)
            rc_wait, _ = future.result(timeout=7.0)
            self.assertIn(rc_wait, (0, c.EXIT_WAIT_TIMEOUT))

    def test_inbox_agent_user_flag_rejected(self) -> None:
        """CLI rejects conflicting --user and --token-file (rc 2); daemon rejects user flag with agent auth (rc 4)."""
        # 1. CLI rejects conflicting credentials: --user together with --token-file
        rc, resp = self.harness.cli("inbox", "list", "--user", token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertEqual(resp.get("error", {}).get("code"), c.INVALID_ARGUMENT)
        self.assertEqual(resp.get("error", {}).get("details", {}).get("param"), "user")

        # 2. Daemon trust boundary: agent credential sending args.user=True gets rc 4 CAPABILITY_MISSING
        import socket
        sock_path = os.path.join(self.harness.home, "run", "mesh.sock")

        def send_agent_req(cmd: str, args: dict) -> dict:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            req = json.dumps({
                "v": 1,
                "cmd": cmd,
                "auth": {"type": "token", "token": self.tok_lead1},
                "args": args,
            }) + "\n"
            s.sendall(req.encode("utf-8"))
            f = s.makefile("r", encoding="utf-8")
            line = f.readline()
            s.close()
            return json.loads(line)

        resp_list = send_agent_req("inbox.list", {"user": True})
        self.assertFalse(resp_list.get("ok"))
        self.assertEqual(resp_list.get("exit_code"), c.EXIT_FORBIDDEN)
        self.assertEqual(resp_list.get("error", {}).get("code"), c.CAPABILITY_MISSING)

        resp_wait = send_agent_req("inbox.wait", {"timeout": "1s", "user": True})
        self.assertFalse(resp_wait.get("ok"))
        self.assertEqual(resp_wait.get("exit_code"), c.EXIT_FORBIDDEN)
        self.assertEqual(resp_wait.get("error", {}).get("code"), c.CAPABILITY_MISSING)

        resp_ack = send_agent_req("inbox.ack", {"messages": ["msg_123"], "user": True})
        self.assertFalse(resp_ack.get("ok"))
        self.assertEqual(resp_ack.get("exit_code"), c.EXIT_FORBIDDEN)
        self.assertEqual(resp_ack.get("error", {}).get("code"), c.CAPABILITY_MISSING)

    def test_inbox_wait_missing_timeout_and_dry_run_rejected(self) -> None:
        """Wait commands require --timeout and reject --dry-run (rc 2 INVALID_ARGUMENT)."""
        # Missing --timeout
        rc_no_to, resp_no_to = self.harness.cli("inbox", "wait", token=self.tok_lead1)
        self.assertEqual(rc_no_to, c.EXIT_USAGE)
        self.assertEqual(resp_no_to.get("error", {}).get("code"), c.INVALID_ARGUMENT)
        self.assertEqual(resp_no_to.get("error", {}).get("details", {}).get("param"), "timeout")

        # Dry-run rejected
        rc_dry, resp_dry = self.harness.cli("inbox", "wait", "--timeout", "1s", "--dry-run", token=self.tok_lead1)
        self.assertEqual(rc_dry, c.EXIT_USAGE)
        self.assertEqual(resp_dry.get("error", {}).get("code"), c.INVALID_ARGUMENT)
        self.assertEqual(resp_dry.get("error", {}).get("details", {}).get("param"), "dry_run")

    def test_user_inbox_list_and_ack(self) -> None:
        """User can list and acknowledge messages in their user inbox with --user."""
        # Seed an inbox message for the user through the fan-out path
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        from mesh.core.events import record_event
        user_row = conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;").fetchone()
        user_id = user_row["id"]
        record_event(
            conn=conn,
            event_type="approval.requested",
            actor_id=user_id,
            actor_kind="user",
            via="direct",
            approval_id="apr_test_user_ibx",
            payload={"summary": "Test user approval", "category": "project_control"},
        )
        conn.commit()
        conn.close()

        # User lists their inbox with --user
        rc_list, resp_list = self.harness.cli("inbox", "list", user=True)
        self.assertEqual(rc_list, 0)
        items = resp_list.get("data", {}).get("items", [])
        self.assertTrue(len(items) >= 1)
        user_msg = items[0]
        self.assertEqual(user_msg["type"], "approval.requested")
        msg_id = user_msg["id"]

        # User acks the message with --user
        rc_ack, resp_ack = self.harness.cli("inbox", "ack", msg_id, user=True)
        self.assertEqual(rc_ack, 0)
        self.assertEqual(resp_ack.get("data", {}).get("acked"), 1)

        # Message is no longer pending in unacked list
        rc_list2, resp_list2 = self.harness.cli("inbox", "list", user=True)
        self.assertEqual(rc_list2, 0)
        remaining = [m for m in resp_list2.get("data", {}).get("items", []) if m["id"] == msg_id]
        self.assertEqual(len(remaining), 0)

        # Message is visible with status 'acked' when passing --all
        rc_all, resp_all = self.harness.cli("inbox", "list", "--all", user=True)
        self.assertEqual(rc_all, 0)
        all_items = [m for m in resp_all.get("data", {}).get("items", []) if m["id"] == msg_id]
        self.assertEqual(len(all_items), 1)
        self.assertEqual(all_items[0]["status"], "acked")

    @serial
    def test_inbox_wait_disconnect_leaves_outbox_pending(self) -> None:
        """Terminated inbox wait does not mark subsequently generated messages as delivered."""
        import subprocess

        tok_ibx = create_agent(self.harness, "ibx", delivery="inbox")
        tok_path = os.path.join(self.harness.home, "tokens", "ibx.token")
        proc = subprocess.Popen(
            [
                self.harness.bin_mesh,
                "--home", self.harness.home,
                "--token-file", tok_path,
                "inbox", "wait",
                "--timeout", "30s",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Poll daemon status until waiter is confirmed active
            seen_waiter = False
            for _ in range(50):
                time.sleep(0.1)
                rc_st, resp_st = self.harness.cli("daemon", "status")
                if rc_st == 0 and resp_st.get("data", {}).get("active_waits", 0) >= 1:
                    seen_waiter = True
                    break
            self.assertTrue(seen_waiter, "Daemon never reported active_waits >= 1 for inbox wait")

            # Terminate client process
            proc.terminate()
            proc.wait(timeout=3.0)

            # Wait until daemon releases the waiter and active_waits drops to 0
            released = False
            for _ in range(50):
                time.sleep(0.1)
                rc_st, resp_st = self.harness.cli("daemon", "status")
                if rc_st == 0 and resp_st.get("data", {}).get("active_waits", 0) == 0:
                    released = True
                    break
            self.assertTrue(released, "Daemon active_waits did not return to 0 after terminating waiter")

            rc_p, resp_p = self.harness.cli(
                "intake", "submit",
                "--title", "Proj Dead Waiter",
                "--goal", "Goal",
                "--criterion", "c1",
                token=self.tok_plain,
            )
            self.assertEqual(rc_p, 0)
            prj_id = resp_p["data"]["id"]

            # Create a task assigned to ibx so an outbox message (task.offered) is produced
            rc_t, resp_t = self.harness.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Task For Dead Waiter",
                "--spec", "Spec",
                "--assignee", "ibx",
                token=self.tok_lead1,
            )
            self.assertEqual(rc_t, 0)
            tsk_id = resp_t["data"]["id"]

            # Wait for task to be promoted to queued (emits task.offered to outbox)
            rc_w, _ = self.harness.cli("task", "wait", tsk_id, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
            self.assertEqual(rc_w, 0)

            # The dead waiter must not consume the message. This is a deliberate fixed
            # wait (not a poll): it gives a zombie client the chance to misbehave if it
            # were still alive, so the outbox row must still be 'pending' afterwards.
            time.sleep(1.0)

            # Assert outbox row is still 'pending', not 'delivered'
            conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT o.status, o.id
                   FROM outbox o
                   JOIN principals p ON o.recipient_id = p.id
                   WHERE p.name = 'ibx' AND o.type = 'task.offered'
                   ORDER BY o.event_seq DESC
                   LIMIT 1;"""
            ).fetchall()
            conn.close()

            self.assertTrue(len(rows) >= 1)
            self.assertEqual(rows[0]["status"], "pending")

            # Control case: a live inbox wait connects, returns the message, and marks it delivered
            rc_live, resp_live = self.harness.cli(
                "inbox", "wait",
                "--timeout", "5s",
                token=tok_ibx,
            )
            self.assertEqual(rc_live, 0)
            self.assertTrue(resp_live["ok"])
            items = resp_live["data"]["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], rows[0]["id"])

            conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
            conn.row_factory = sqlite3.Row
            row_ctrl = conn.execute("SELECT status FROM outbox WHERE id = ?;", (rows[0]["id"],)).fetchone()
            conn.close()
            self.assertEqual(row_ctrl["status"], "delivered")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_inbox_offer_superseded_on_t6_and_t22(self) -> None:
        """Carry-over 6h-2: Queue an inbox task; T6 failure supersedes task.offered (not in inbox list); T22 cancel supersedes task.offered."""
        # 1. Setup project via intake
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "T6 T22 Project",
            "--goal", "Goal",
            "--criterion", "Crit",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        project_id = resp_p["data"]["id"]

        # Create inbox agent ibx2 with allowed-model m-a
        tok_ibx2 = create_agent(self.harness, "ibx2", adapter="fake", delivery="inbox", allowed_models=["m-a"], default_model="m-a")

        # 2. Lead creates task assigned to ibx2 with model m-a
        rc_t1, resp_t1 = self.harness.cli(
            "task", "create",
            "--project", project_id,
            "--title", "T6 Test Task",
            "--spec", "Spec",
            "--assignee", "ibx2",
            "--model", "m-a",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t1, 0)
        tsk1_id = resp_t1["data"]["id"]

        # Wait until task is queued and task.offered appears in ibx2's inbox
        rc_w, _ = self.harness.cli("task", "wait", tsk1_id, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        rc_l1, resp_l1 = self.harness.cli("inbox", "list", token=tok_ibx2)
        self.assertEqual(rc_l1, 0)
        offered_msgs1 = [m for m in resp_l1["data"]["items"] if m.get("type") == "task.offered" and m.get("payload", {}).get("task_id") == tsk1_id]
        self.assertEqual(len(offered_msgs1), 1)

        # Update agent ibx2 allowed models to m-b, triggering T6 on next scheduler pass
        rc_upd, _ = self.harness.cli("agent", "update", "ibx2", "--allowed-model", "m-b", user=True)
        self.assertEqual(rc_upd, 0)

        # Wait until task transitions to failed via T6
        self.assertTrue(
            self.harness.wait_for(
                lambda: self.harness.cli("task", "show", tsk1_id, token=self.tok_lead1)[1].get("data", {}).get("status") == "failed",
                timeout=5.0,
                step=0.2,
            )
        )

        # Assert no task.offered for tsk1 remains in ibx2's inbox list
        rc_l1_after, resp_l1_after = self.harness.cli("inbox", "list", token=tok_ibx2)
        self.assertEqual(rc_l1_after, 0)
        offered_after = [m for m in resp_l1_after["data"]["items"] if m.get("type") == "task.offered" and m.get("payload", {}).get("task_id") == tsk1_id]
        self.assertEqual(len(offered_after), 0)

        # Verify DB outbox row status is superseded
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        row_ob1 = conn.execute(
            "SELECT status FROM outbox WHERE id = ?;", (offered_msgs1[0]["id"],)
        ).fetchone()
        self.assertEqual(row_ob1[0], "superseded")
        conn.close()

        # 3. T22: Create second task assigned to ibx2 with model m-b
        rc_t2, resp_t2 = self.harness.cli(
            "task", "create",
            "--project", project_id,
            "--title", "T22 Test Task",
            "--spec", "Spec",
            "--assignee", "ibx2",
            "--model", "m-b",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t2, 0)
        tsk2_id = resp_t2["data"]["id"]

        rc_w2, _ = self.harness.cli("task", "wait", tsk2_id, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2, 0)

        rc_l2, resp_l2 = self.harness.cli("inbox", "list", token=tok_ibx2)
        self.assertEqual(rc_l2, 0)
        offered_msgs2 = [m for m in resp_l2["data"]["items"] if m.get("type") == "task.offered" and m.get("payload", {}).get("task_id") == tsk2_id]
        self.assertEqual(len(offered_msgs2), 1)

        # Lead cancels queued task (T22)
        rc_c, _ = self.harness.cli("task", "cancel", tsk2_id, "--reason", "Not needed", token=self.tok_lead1)
        self.assertEqual(rc_c, 0)

        # Assert no task.offered for tsk2 remains in ibx2's inbox list
        rc_l2_after, resp_l2_after = self.harness.cli("inbox", "list", token=tok_ibx2)
        self.assertEqual(rc_l2_after, 0)
        offered2_after = [m for m in resp_l2_after["data"]["items"] if m.get("type") == "task.offered" and m.get("payload", {}).get("task_id") == tsk2_id]
        self.assertEqual(len(offered2_after), 0)

        # Verify DB outbox row status is superseded
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        row_ob2 = conn.execute(
            "SELECT status FROM outbox WHERE id = ?;", (offered_msgs2[0]["id"],)
        ).fetchone()
        self.assertEqual(row_ob2[0], "superseded")
        conn.close()



if __name__ == "__main__":
    unittest.main()
