"""Dogfood friction batch 3, end to end: inbox list --peek, the reject reason for the
assignee, agent create's max-concurrency default, and ack windows fixed at offer time.
(Merge TESTS_FAILED details and the text-mode merge note are in test_merge_tests.py.)"""

import json
import os
import shutil
import tempfile
import time
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness, serial
from tests.integration.test_execution_inbox import create_agent
from tests.stubs import create_test_stubs


class TestFriction3(unittest.TestCase):
    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tok_w1 = create_agent(self.harness, "w1")
        rc, resp = self.harness.cli("intake", "submit", "--title", "PRJ", "--goal", "G", "--criterion", "C1",
                                    "--lead", "lead1", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def new_task(self, assignee="w1", title="T"):
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", title,
                                    "--spec", "S", "--assignee", assignee, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        tsk = resp["data"]["id"]
        rc, resp = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "10s",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return tsk

    def inbox(self, *extra, token=None):
        rc, resp = self.harness.cli("inbox", "list", *extra, token=token or self.tok_w1)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["items"]

    # 1. --peek
    def test_inbox_list_peek_does_not_mark_delivered(self) -> None:
        tsk = self.new_task()
        for _ in range(2):
            items = self.inbox("--peek")
            offer = [m for m in items if m["type"] == "task.offered" and m["payload"]["task_id"] == tsk]
            self.assertEqual(len(offer), 1, items)
            self.assertEqual(offer[0]["status"], "pending")
            self.assertIsNone(offer[0]["delivered_at"])
        items = self.inbox()  # the worker's own list marks it delivered
        self.assertEqual([m["status"] for m in items if m["type"] == "task.offered"], ["pending"])
        items = self.inbox("--peek")
        self.assertEqual([m["status"] for m in items if m["type"] == "task.offered"], ["delivered"])

    # 2. reject reason
    def test_reject_reason_reaches_the_assignee(self) -> None:
        tsk = self.new_task()
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_w1)
        self.assertEqual(rc, 0, st)
        run, fence = st["data"]["run_id"], st["data"]["fence"]
        rc, resp = self.harness.cli("task", "submit", tsk, "--run", run, "--fence", str(fence),
                                    "--summary", "done", token=self.tok_w1)
        self.assertEqual(rc, 0, resp)
        self.harness.cli("inbox", "ack", *[m["id"] for m in self.inbox()], token=self.tok_w1)
        rc, resp = self.harness.cli("task", "reject", tsk, "--reason", "add tests for the CSV header",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "10s",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        for tok in (self.tok_w1, self.tok_lead1):
            rc, sh = self.harness.cli("task", "show", tsk, token=tok)
            self.assertEqual(rc, 0, sh)
            lr = sh["data"]["last_rejection"]
            self.assertEqual(lr["reason"], "add tests for the CSV header")
            self.assertEqual(lr["rejected_by"], "lead1")
            self.assertTrue(lr["rejected_at"])
        items = self.inbox()
        rej = [m for m in items if m["type"] == "task.rejected"]
        self.assertEqual(len(rej), 1, items)
        self.assertEqual(rej[0]["payload"]["reason"], "add tests for the CSV header")
        self.assertEqual(rej[0]["payload"]["rejected_by"], "lead1")
        offers = [m for m in items if m["type"] == "task.offered"]
        self.assertEqual(len(offers), 1, items)
        self.assertEqual(offers[0]["payload"]["last_rejection"]["reason"], "add tests for the CSV header")
        # text mode shows it too
        rc, out = self.harness.cli("--format", "text", "task", "show", tsk, token=self.tok_w1)
        self.assertIn("add tests for the CSV header", out["raw_stdout"])

    def test_never_rejected_has_null_last_rejection(self) -> None:
        tsk = self.new_task()
        rc, sh = self.harness.cli("task", "show", tsk, token=self.tok_w1)
        self.assertIsNone(sh["data"]["last_rejection"])
        offer = [m for m in self.inbox("--peek") if m["type"] == "task.offered"][0]
        self.assertIsNone(offer["payload"]["last_rejection"])
        self.assertEqual(offer["payload"]["ack_timeout_s"], 600)

    # 3. max-concurrency default
    def test_agent_create_defaults_max_concurrency_to_1(self) -> None:
        rc, resp = self.harness.cli("agent", "create", "--name", "w2", "--adapter", "none", "--delivery", "inbox",
                                    user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["max_concurrency"], 1)
        rc, resp = self.harness.cli("agent", "create", "--name", "w3", "--adapter", "none", "--delivery", "inbox",
                                    "--max-concurrency", "0", user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)

    # 6. ack window fixed at offer time
    @serial
    def test_lowering_global_ack_timeout_is_not_retroactive(self) -> None:
        tsk = self.new_task()  # offered with the default 600 s window
        time.sleep(1.5)
        rc, resp = self.harness.cli("config", "set", "inbox.ack_timeout_s", "1", user=True)
        self.assertEqual(rc, 0, resp)
        kept = resp["data"]["offers_keeping_window"]
        self.assertEqual([(k["task_id"], k["ack_timeout_s"], k["kept"]) for k in kept], [(tsk, 600, True)])
        self.assertTrue(any(tsk in w and "new offers only" in w for w in resp["warnings"]), resp)
        time.sleep(3)
        rc, sh = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(sh["data"]["status"], "queued", sh)
        # A new offer gets the new, short window.
        t2 = self.new_task(title="second")
        rc, resp = self.harness.cli("task", "wait", t2, "--until", "state:failed", "--timeout", "15s",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        # Raising the window extends offers already out.
        rc, resp = self.harness.cli("config", "set", "inbox.ack_timeout_s", "900", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["warnings"], [])

    @serial
    def test_raising_window_extends_existing_offer(self) -> None:
        rc, _ = self.harness.cli("config", "set", "inbox.ack_timeout_s", "3", user=True)
        tsk = self.new_task()
        rc, resp = self.harness.cli("agent", "update", "w1", "--ack-timeout", "1h", user=True)
        self.assertEqual(rc, 0, resp)
        time.sleep(4.5)
        rc, sh = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(sh["data"]["status"], "queued", sh)


if __name__ == "__main__":
    unittest.main()
