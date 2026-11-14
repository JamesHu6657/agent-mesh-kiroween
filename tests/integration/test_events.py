"""Integration tests for Event log, Event tail, and Outbox fan-out per plan §12, §16 M1.6.

ACC: J8
"""

import concurrent.futures
import json
import os
import shutil
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
    os.makedirs(tok_dir, mode=0o700, exist_ok=True)
    tok_path = os.path.join(tok_dir, f"{name}.tok")
    with open(tok_path, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(tok_path, 0o600)

    return token


class TestEventsIntegration(unittest.TestCase):
    """Integration test suite for events and outbox fan-out."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

        self.tok_lead1 = create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        self.tok_lead2 = create_agent(self.harness, "lead2", adapter="none", delivery="inbox", can_lead=True)
        self.tok_plain = create_agent(self.harness, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
        self.tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")

        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def test_event_list_filters(self) -> None:
        """Verify event list filtering by project, task, type, and since."""
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Events",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        prj_id = resp_p["data"]["id"]

        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", prj_id,
            "--title", "Task Events",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk_id = resp_t["data"]["id"]

        # 1. List events by project
        rc_l1, resp_l1 = self.harness.cli("event", "list", "--project", prj_id, user=True)
        self.assertEqual(rc_l1, 0)
        items1 = resp_l1["data"]["items"]
        self.assertTrue(len(items1) >= 2)
        for item in items1:
            self.assertEqual(item["project_id"], prj_id)
            self.assertIn("hash", item)
            self.assertIn("prev_hash", item)
            self.assertIn("seq", item)

        # 2. List events by task
        rc_l2, resp_l2 = self.harness.cli("event", "list", "--task", tsk_id, user=True)
        self.assertEqual(rc_l2, 0)
        items2 = resp_l2["data"]["items"]
        self.assertTrue(len(items2) >= 1)
        for item in items2:
            self.assertEqual(item["task_id"], tsk_id)

        # 3. List events by type
        rc_l3, resp_l3 = self.harness.cli("event", "list", "--type", "task.assigned", user=True)
        self.assertEqual(rc_l3, 0)
        for item in resp_l3["data"]["items"]:
            self.assertEqual(item["type"], "task.assigned")

        # 4. List events since seq
        mid_seq = items1[0]["seq"]
        rc_l4, resp_l4 = self.harness.cli("event", "list", "--project", prj_id, "--since", str(mid_seq), user=True)
        self.assertEqual(rc_l4, 0)
        for item in resp_l4["data"]["items"]:
            self.assertTrue(item["seq"] > mid_seq)

    def test_event_tail_ndjson(self) -> None:
        """Verify event tail streams NDJSON in strict seq order for prior events."""
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Tail",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        prj_id = resp_p["data"]["id"]

        # Add another event to have multiple events
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", prj_id,
            "--title", "Tail Task",
            "--spec", "Spec",
            user=True,
        )
        self.assertEqual(rc_t, 0)

        # Reject --dry-run on event tail -> rc 2 INVALID_ARGUMENT (param: dry_run)
        rc_dry, resp_dry = self.harness.cli("event", "tail", "--project", prj_id, "--timeout", "1s", "--dry-run", user=True)
        self.assertEqual(rc_dry, c.EXIT_USAGE)
        self.assertEqual(resp_dry.get("error", {}).get("code"), c.INVALID_ARGUMENT)
        self.assertEqual(resp_dry.get("error", {}).get("details", {}).get("param"), "dry_run")

        # Call event tail with explicit --since 0 and --timeout 1s
        rc_tail, resp_tail = self.harness.cli("event", "tail", "--project", prj_id, "--since", "0", "--timeout", "1s", user=True)
        self.assertEqual(rc_tail, 0)

        # Harness envelope contains raw_stdout because event tail emits NDJSON lines
        raw_stdout = resp_tail.get("raw_stdout", "")
        lines = [line.strip() for line in raw_stdout.strip().split("\n") if line.strip()]
        self.assertTrue(len(lines) >= 2, f"Expected at least 2 events in tail, got {len(lines)}")

        seqs = []
        for line in lines:
            event = json.loads(line)
            self.assertIn("id", event)
            self.assertIn("type", event)
            self.assertIn("seq", event)
            self.assertEqual(event["project_id"], prj_id)
            seqs.append(event["seq"])

        # Check strict sequential ascending order
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        for i in range(len(seqs) - 1):
            self.assertLess(seqs[i], seqs[i + 1])

    def test_event_tail_live_background(self) -> None:
        """Verify event tail live stream in background captures newly created events."""
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Live Tail",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        prj_id = resp_p["data"]["id"]

        def run_tail() -> Any:
            # Starts from latest seq (since omitted), with 4s timeout
            return self.harness.cli("event", "tail", "--project", prj_id, "--timeout", "4s", user=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut = executor.submit(run_tail)
            time.sleep(0.3)

            # Create an event while tail is running
            rc_note, _ = self.harness.cli("project", "note", prj_id, "--text", "Live tail note", token=self.tok_lead1)
            self.assertEqual(rc_note, 0)

            rc_tail, resp_tail = fut.result(timeout=6.0)
            self.assertEqual(rc_tail, 0)

            # If harness cli received multiple lines, raw_stdout is set. If single line, it parsed it directly.
            if "raw_stdout" in resp_tail:
                lines = [line.strip() for line in resp_tail["raw_stdout"].strip().split("\n") if line.strip()]
                events = [json.loads(line) for line in lines]
            else:
                events = [resp_tail]

            self.assertTrue(len(events) >= 1, f"Expected at least 1 live event, got {len(events)}")
            note_events = [e for e in events if e.get("type") == "project.note"]
            self.assertTrue(len(note_events) >= 1)
            self.assertEqual(note_events[0]["payload"].get("text"), "Live tail note")

    def test_event_tail_unrelated_agent_scope_mismatch(self) -> None:
        """An unrelated agent receives rc 4 SCOPE_MISMATCH on event tail --project."""
        tok_unrelated = create_agent(self.harness, "unrelated_tail", adapter="none", delivery="inbox")
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Secret",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        prj_id = resp_p["data"]["id"]

        rc, resp = self.harness.cli("event", "tail", "--project", prj_id, "--timeout", "1s", token=tok_unrelated)
        self.assertEqual(rc, c.EXIT_FORBIDDEN)
        self.assertEqual(resp.get("error", {}).get("code"), c.SCOPE_MISMATCH)

    def test_j8_outbox_handover_superseded(self) -> None:
        """ACC: J8 - Pending unacked messages for lead1, then set-lead to lead2 -> old messages superseded, re-created for lead2."""
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Proj J8",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        prj_id = resp_p["data"]["id"]

        # lead1 runs inbox list to receive message -> marks row 'delivered'
        rc_inbox, resp_inbox = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_inbox, 0)
        items_lead1 = [it for it in resp_inbox["data"]["items"] if it.get("project_id") == prj_id]
        self.assertTrue(len(items_lead1) >= 1)
        created_item = [it for it in items_lead1 if it["type"] == "project.created"][0]
        self.assertIn("criteria", created_item["payload"])
        crit_list = created_item["payload"]["criteria"]
        self.assertEqual(len(crit_list), 1)
        self.assertEqual(crit_list[0]["text"], "c1")
        self.assertTrue(crit_list[0]["id"].startswith("crit"))

        conn = self.harness.db()
        lead1_id = conn.execute("SELECT id FROM principals WHERE name = 'lead1';").fetchone()["id"]
        lead2_id = conn.execute("SELECT id FROM principals WHERE name = 'lead2';").fetchone()["id"]
        m_before = conn.execute(
            "SELECT id, status, type, payload_json FROM outbox WHERE recipient_id = ? AND project_id = ?;",
            (lead1_id, prj_id),
        ).fetchall()
        conn.close()
        self.assertTrue(len(m_before) >= 1)
        # Message was listed so status is now 'delivered'
        self.assertEqual(m_before[0]["status"], "delivered")
        old_payload_json = m_before[0]["payload_json"]
        old_type = m_before[0]["type"]

        # Set lead to lead2
        rc_sl, resp_sl = self.harness.cli(
            "project", "set-lead", prj_id, "lead2",
            "--reason", "Handover to lead2",
            user=True,
        )
        self.assertEqual(rc_sl, 0)

        # Verify old lead1 message is marked 'superseded'
        conn = self.harness.db()
        m_lead1_after = conn.execute(
            "SELECT id, status, type, payload_json FROM outbox WHERE recipient_id = ? AND project_id = ? AND type = ?;",
            (lead1_id, prj_id, old_type),
        ).fetchall()
        self.assertTrue(len(m_lead1_after) >= 1)
        self.assertEqual(m_lead1_after[0]["status"], "superseded")

        # Verify new lead2 has the re-created message plus project.handover
        m_lead2_after = conn.execute(
            "SELECT id, status, type, payload_json FROM outbox WHERE recipient_id = ? AND project_id = ?;",
            (lead2_id, prj_id),
        ).fetchall()
        conn.close()
        types_lead2 = [r["type"] for r in m_lead2_after]
        self.assertIn("project.created", types_lead2)
        self.assertIn("project.handover", types_lead2)

        # Assert payload_json is identical on re-created message
        recreated = [r for r in m_lead2_after if r["type"] == old_type]
        self.assertTrue(len(recreated) >= 1)
        self.assertEqual(recreated[0]["payload_json"], old_payload_json)
        self.assertEqual(recreated[0]["status"], "pending")

    @serial
    def test_event_tail_disconnect_releases_stream(self) -> None:
        """Verify daemon terminates tail coroutine and active_streams returns to 0 when client disconnects without --timeout."""
        import subprocess
        import sys

        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Disconnect Tail",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        prj_id = resp_p["data"]["id"]

        tok_path = os.path.join(self.harness.home, "tokens", "lead1.tok")
        proc = subprocess.Popen(
            [
                self.harness.bin_mesh,
                "--home", self.harness.home,
                "--token-file", tok_path,
                "event", "tail",
                "--project", prj_id,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Wait briefly for daemon to accept connection and increment active_streams
            start = time.monotonic()
            seen_active = False
            while time.monotonic() - start < 3.0:
                rc_st, resp_st = self.harness.cli("status")
                self.assertEqual(rc_st, 0)
                if resp_st["data"].get("active_streams", 0) >= 1:
                    seen_active = True
                    break
                time.sleep(0.1)

            self.assertTrue(seen_active, "Daemon never reported active_streams >= 1")

            # Terminate client process (SIGTERM)
            proc.terminate()
            proc.wait(timeout=3.0)

            # Within 2 s, daemon should detect disconnect and active_streams return to 0
            start_term = time.monotonic()
            released = False
            while time.monotonic() - start_term < 2.5:
                rc_st2, resp_st2 = self.harness.cli("status")
                self.assertEqual(rc_st2, 0)
                if resp_st2["data"].get("active_streams", 0) == 0:
                    released = True
                    break
                time.sleep(0.1)

            self.assertTrue(released, f"active_streams did not return to 0 after disconnect: {resp_st2['data'].get('active_streams')}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
