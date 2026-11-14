"""Dogfood friction batch 5, end to end: `task merge --all-accepted`, `task list` text
columns and comma status lists, and scheduler error counts in `daemon status`/`doctor`."""

import os
import re
import unittest

import mesh.constants as c
from tests.integration import test_merge as tm


class TestBatch5(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project
    show = tm.TestMerge.show
    start_task = tm.TestMerge.start_task
    commit_file = tm.TestMerge.commit_file
    submit_accept = tm.TestMerge.submit_accept
    accepted_task = tm.TestMerge.accepted_task
    merge = tm.TestMerge.merge
    q = tm.TestMerge.q

    def merge_all(self, *extra, token=None):
        return self.harness.cli("task", "merge", "--all-accepted", "--project", self.prj, *extra,
                                token=token or self.tok_lead1, timeout=300.0)

    def test_merge_all_accepted_in_order_and_stops_at_first_failure(self) -> None:
        a, _ = self.accepted_task(self.prj, "A", "a.txt", "a\n")
        b, _ = self.accepted_task(self.prj, "B", "same.txt", "b\n")
        cc, _ = self.accepted_task(self.prj, "C", "same.txt", "c\n")  # conflicts with B
        d, _ = self.accepted_task(self.prj, "D", "d.txt", "d\n")
        # Guards: not lead (refused by the first merge's own authz), id + flag, no project.
        rc, resp = self.merge_all(token=self.tok_ibx)
        self.assertNotEqual(rc, 0, resp)
        self.assertEqual(resp["error"]["details"]["merged"], [])
        self.assertEqual(resp["error"]["details"]["failed"]["task_id"], a)
        self.assertEqual(self.show(a)["status"], "accepted")
        rc, resp = self.harness.cli("task", "merge", a, "--all-accepted", "--project", self.prj, token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        rc, resp = self.harness.cli("task", "merge", "--all-accepted", token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        rc, resp = self.harness.cli("task", "merge", token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_USAGE, resp)

        rc, resp = self.merge_all()
        self.assertNotEqual(rc, 0, resp)
        det = resp["error"]["details"]
        self.assertEqual([m["task_id"] for m in det["merged"]], [a, b])
        self.assertEqual(det["failed"]["task_id"], cc)
        self.assertEqual(det["not_attempted"], [d])
        self.assertIn("stopped at", resp["error"]["message"])
        self.assertEqual([self.show(t)["status"] for t in (a, b, d)], ["merged", "merged", "accepted"])
        self.assertNotEqual(self.show(cc)["status"], "merged")
        # The rest goes through on a second call (C is no longer accepted).
        rc, resp = self.merge_all()
        if self.show(cc)["status"] == "accepted":  # a conflict may leave C accepted: close it
            self.assertNotEqual(rc, 0, resp)
            rc, resp = self.harness.cli("task", "close", cc, "--superseded-by", b, token=self.tok_lead1)
            self.assertEqual(rc, 0, resp)
            rc, resp = self.merge_all()
        self.assertEqual(rc, 0, resp)
        self.assertEqual([m["task_id"] for m in resp["data"]["merged"]], [d])
        self.assertEqual(self.show(d)["status"], "merged")
        rc, out = self.harness.cli("--format", "text", "task", "merge", "--all-accepted", "--project", self.prj,
                                   token=self.tok_lead1)
        self.assertEqual(rc, 0, out)
        self.assertIn("no accepted code tasks", out["raw_stdout"])

    def test_task_list_text_columns_and_status_lists(self) -> None:
        a, _ = self.accepted_task(self.prj, "Alpha", "a.txt", "a\n")
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", "Beta", "--spec", "S",
                                    "--assignee", "ibx", token=self.tok_lead1)
        b = resp["data"]["id"]
        rc, resp = self.harness.cli("task", "list", "--project", self.prj, "--status", "accepted,queued",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(sorted(t["id"] for t in resp["data"]["items"]), sorted([a, b]))
        rc, resp = self.harness.cli("task", "list", "--project", self.prj, "--status", " accepted ,",
                                    token=self.tok_lead1)
        self.assertEqual([t["id"] for t in resp["data"]["items"]], [a])
        rc, resp = self.harness.cli("task", "list", "--status", "accepted,bogus", token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["details"]["unknown"], ["bogus"])
        rc, out = self.harness.cli("--format", "text", "task", "list", "--project", self.prj, token=self.tok_lead1)
        self.assertEqual(rc, 0, out)
        lines = out["raw_stdout"].splitlines()
        self.assertEqual(lines[0].split(), ["ID", "STATUS", "ASSIGNEE", "AGE", "IN_STATE", "ATTEMPT", "TITLE"])
        row = next(l for l in lines if l.startswith(a))
        self.assertIn(" ibx ", row)
        self.assertRegex(row, r" \d+[smhd] +\d+[smhd] ")
        self.assertTrue(row.rstrip().endswith("Alpha"))

    def test_status_and_doctor_show_scheduler_errors(self) -> None:
        rc, resp = self.harness.cli("daemon", "status", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["scheduler"]["sweep_errors"], 0)
        self.assertIsNone(resp["data"]["scheduler"]["last_sweep_error"])
        rc, resp = self.harness.cli("doctor")
        self.assertEqual(rc, 0, resp)
        chk = {x["name"]: x for x in resp["data"]["checks"]}["scheduler_errors"]
        self.assertTrue(chk["ok"], chk)

        # A raising first step (testing hook): counted, logged with traceback, and the
        # later steps still run (a new task is promoted to queued).
        with open(os.path.join(self.harness.home, "run", "scheduler_fail_step"), "w") as f:
            f.write("approval_expiry\n")
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", "T", "--spec", "S",
                                    "--assignee", "ibx", token=self.tok_lead1)
        tsk = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(lambda: self.show(tsk)["status"] == "queued", timeout=15.0))
        self.assertTrue(self.harness.wait_for(
            lambda: self.harness.cli("daemon", "status", user=True)[1]["data"]["scheduler"]["sweep_errors"] > 0,
            timeout=15.0))
        sch = self.harness.cli("daemon", "status", user=True)[1]["data"]["scheduler"]
        self.assertEqual(sch["last_sweep_error"]["step"], "approval_expiry")
        self.assertIn("injected failure", sch["last_sweep_error"]["error"])
        self.assertEqual(set(sch["sweep_errors_by_step"]), {"approval_expiry"})
        with open(os.path.join(self.harness.home, "logs", "meshd.log")) as f:
            log = f.read()
        self.assertIn("Scheduler step approval_expiry failed", log)
        self.assertIn("Traceback (most recent call last)", log)
        rc, resp = self.harness.cli("doctor")
        self.assertEqual(rc, 0, resp)
        chk = {x["name"]: x for x in resp["data"]["checks"]}["scheduler_errors"]
        self.assertFalse(chk["ok"], chk)
        self.assertEqual(chk["level"], "warning")
        self.assertIn("approval_expiry", chk["detail"])
        self.assertTrue(any(p.startswith("scheduler_errors:") for p in resp["data"]["problems"]))
        os.unlink(os.path.join(self.harness.home, "run", "scheduler_fail_step"))


if __name__ == "__main__":
    unittest.main()
