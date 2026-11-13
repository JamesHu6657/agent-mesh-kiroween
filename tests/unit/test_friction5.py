"""Unit tests for dogfood friction batch 5: session rules, scheduler error counting,
the `task list` text table and the doctor scheduler check."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.cli.commands.doctor_env import _scheduler_errors_check
from mesh.cli.commands.task import new_session_id, task_list_table
from mesh.core import leases
from mesh.daemon import scheduler
from mesh.errors import MeshError


class TestSessionRules(unittest.TestCase):
    def check(self, recorded, sent):
        ev = None if recorded is False else {"session": recorded, "ts": "2026-01-01T00:00:00Z"}
        with mock.patch.object(leases, "_run_event", return_value=ev):
            leases.check_run_session(None, "tsk_1", "run_1", sent)

    def test_old_client_runs_are_never_checked(self) -> None:
        for sent in (None, "", "ses_x"):
            self.check(None, sent)    # started without a session
            self.check(False, sent)   # no task.started event found

    def test_matching_session_passes(self) -> None:
        self.check("ses_a", "ses_a")

    def test_missing_and_mismatched_sessions_refused(self) -> None:
        for sent, reason in ((None, "session_missing"), ("", "session_missing"), ("ses_b", "session_mismatch")):
            with self.assertRaises(MeshError) as cm:
                self.check("ses_a", sent)
            self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.FENCE_MISMATCH, c.EXIT_STALE))
            self.assertEqual(cm.exception.details["reason"], reason)

    def test_new_session_ids_are_unique(self) -> None:
        ids = {new_session_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(i.startswith("ses_") and len(i) == 20 for i in ids))


class TestSchedulerErrors(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-f5-")
        self.addCleanup(shutil.rmtree, self.home, True)
        scheduler.reset_scheduler_errors(self.home)
        self.addCleanup(scheduler.reset_scheduler_errors)

    def test_raising_step_is_counted_logged_and_others_still_run(self) -> None:
        calls = []

        def ok(name):
            def f(**kw):
                calls.append(name)
                return [name]
            return f

        def boom(**kw):
            raise ValueError("bad row")

        names = ["sweep_expired_approvals", "sweep_expired_inbox_leases", "watch_launch_runs_tick",
                 "check_queued_tasks_t6", "promote_tasks", "start_queued_launch_tasks", "start_lead_turns_tick"]
        patches = {n: ok(n) for n in names}
        patches["sweep_expired_approvals"] = boom
        with mock.patch.multiple(scheduler, **patches):
            out = scheduler.run_scheduler_tick(None, self.home)
        self.assertEqual(calls, names[1:4] + ["promote_tasks", "check_queued_tasks_t6"] + names[5:])
        self.assertNotIn("sweep_expired_approvals", out)
        view = scheduler.scheduler_errors_view()
        self.assertEqual(view["sweep_errors"], 1)
        self.assertEqual(view["last_sweep_error"]["step"], "approval_expiry")
        self.assertEqual(view["last_sweep_error"]["error"], "ValueError: bad row")
        with open(os.path.join(self.home, "logs", "meshd.log")) as f:
            log = f.read()
        self.assertIn("Traceback (most recent call last)", log)
        self.assertIn("ValueError: bad row", log)
        with open(os.path.join(self.home, "run", "scheduler_errors.json")) as f:
            saved = json.load(f)
        self.assertEqual((saved["count"], saved["by_step"]), (1, {"approval_expiry": 1}))
        chk = _scheduler_errors_check(self.home)
        self.assertFalse(chk["ok"])
        self.assertIn("1 since meshd started; last: approval_expiry", chk["detail"])
        # reset at the next meshd start
        scheduler.reset_scheduler_errors(self.home)
        self.assertEqual(scheduler.scheduler_errors_view()["sweep_errors"], 0)
        self.assertTrue(_scheduler_errors_check(self.home)["ok"])

    def test_item_errors_carry_the_item_and_never_raise(self) -> None:
        scheduler._sweep_error(self.home, "promote", RuntimeError("x"), "tsk_9")
        scheduler._sweep_error("", "promote", RuntimeError("y"))  # no home: memory only
        v = scheduler.scheduler_errors_view()
        self.assertEqual((v["sweep_errors"], v["sweep_errors_by_step"]), (2, {"promote": 2}))
        self.assertIsNone(v["last_sweep_error"]["item"])
        with open(os.path.join(self.home, "logs", "meshd.log")) as f:
            self.assertIn("Scheduler step promote failed on tsk_9", f.read())

    def test_injection_hook_only_in_testing_mode(self) -> None:
        os.makedirs(os.path.join(self.home, "run"), exist_ok=True)
        with open(os.path.join(self.home, "run", "scheduler_fail_step"), "w") as f:
            f.write("promote\n")
        scheduler._injected_failure(self.home, "promote", False)  # ignored
        scheduler._injected_failure(self.home, "t6_check", True)  # other step
        with self.assertRaises(RuntimeError):
            scheduler._injected_failure(self.home, "promote", True)

    def test_doctor_check_tolerates_a_bad_file(self) -> None:
        os.makedirs(os.path.join(self.home, "run"), exist_ok=True)
        with open(os.path.join(self.home, "run", "scheduler_errors.json"), "w") as f:
            f.write("{nope")
        self.assertTrue(_scheduler_errors_check(self.home)["ok"])


class TestTaskListTable(unittest.TestCase):
    def test_columns_and_ages(self) -> None:
        from mesh.core.clock import parse_utc
        now = parse_utc("2026-01-02T00:00:00Z").timestamp()
        items = [{"id": "tsk_1", "status": "accepted", "assignee": "ibx", "created_at": "2026-01-01T00:00:00Z",
                  "updated_at": "2026-01-01T23:30:00Z", "attempt": 1, "max_attempts": 3, "title": "A  title"},
                 {"id": "tsk_2", "status": "queued", "assignee": None, "created_at": "2026-01-01T23:59:15Z",
                  "updated_at": None, "attempt": 0, "max_attempts": None, "title": "x" * 80}]
        lines = task_list_table(items, now=now).splitlines()
        self.assertEqual(lines[0].split(), ["ID", "STATUS", "ASSIGNEE", "AGE", "IN_STATE", "ATTEMPT", "TITLE"])
        self.assertEqual(lines[1].split(), ["tsk_1", "accepted", "ibx", "1d", "30m", "1/3", "A", "title"])
        self.assertEqual(lines[2].split()[:6], ["tsk_2", "queued", "-", "45s", "-", "0/-"])
        self.assertTrue(lines[2].endswith("..."))
        self.assertEqual(task_list_table([]), "(none)")


if __name__ == "__main__":
    unittest.main()
