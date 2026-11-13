"""Dogfood friction batch 3 (no daemon): failing-test parsing for TESTS_FAILED, the
inbox --peek flag, agent create's max-concurrency default, the merge progress note."""

import argparse
import io
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from mesh.cli.commands import task as task_cmd
from mesh.cli.main import build_parser
from mesh.daemon import merge


class TestFailingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-ft-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _log(self, text: str) -> str:
        p = os.path.join(self.tmp, "test.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_unittest_new_and_old_formats_and_pytest(self) -> None:
        p = self._log(
            "======================================================================\n"
            "FAIL: test_add (tests.test_calc.TestCalc.test_add)\n"
            "----------------------------------------------------------------------\n"
            "ERROR: test_io (tests.test_io.TestIO)\n"
            "FAIL: test_add (tests.test_calc.TestCalc.test_add)\n"          # duplicate (subTest)
            "FAILED tests/test_x.py::test_y - AssertionError\n"
            "ERROR tests/test_x.py::test_z\n"
            "  FAIL: indented lines are not headers (x.y)\n"
            "Ran 4 tests\nFAILED (failures=1, errors=1)\n")
        found, total = merge.failing_tests(p)
        self.assertEqual(total, 4)
        self.assertEqual([(t["kind"], t["id"]) for t in found], [
            ("FAIL", "tests.test_calc.TestCalc.test_add"),
            ("ERROR", "tests.test_io.TestIO.test_io"),
            ("FAIL", "tests/test_x.py::test_y"),
            ("ERROR", "tests/test_x.py::test_z"),
        ])

    def test_cap_and_message(self) -> None:
        p = self._log("".join(f"FAIL: test_{i} (m.C.test_{i})\n" for i in range(25)))
        found, total = merge.failing_tests(p)
        self.assertEqual((len(found), total), (merge.FAILING_TESTS_LIMIT, 25))
        msg = merge.tests_failed_message("tsk_1", {"log_path": p, "failing_tests": found, "failing_tests_total": total})
        self.assertIn("(25 failing tests)", msg)
        self.assertIn("  FAIL: m.C.test_0", msg)
        self.assertIn("... and 5 more", msg)
        self.assertTrue(msg.endswith(f"  log: {p}"))

    def test_post_test_check_failure_message(self) -> None:
        from mesh.daemon.merge import tests_failed_message
        msg = tests_failed_message("tsk_x", {"exit_code": 0, "error": "leftover_scan_uncertain",
                                             "log_path": "/l/test.log"})
        self.assertIn("tests exited 0", msg)
        self.assertIn("leftover_scan_uncertain", msg)
        self.assertNotIn("no FAIL:/ERROR:", msg)
        self.assertIn("log: /l/test.log", msg)

    def test_no_ids_and_timeout_messages(self) -> None:
        msg = merge.tests_failed_message("tsk_1", {"log_path": "/l", "exit_code": 2, "failing_tests": [],
                                                   "failing_tests_total": 0})
        self.assertIn("exit code 2", msg)
        msg = merge.tests_failed_message("tsk_1", {"log_path": "/l", "timeout": True, "timeout_s": 30})
        self.assertIn("timed out after 30s", msg)
        self.assertEqual(merge.failing_tests(os.path.join(self.tmp, "missing.log")), ([], 0))


class TestCliFlags(unittest.TestCase):
    def test_inbox_list_peek(self) -> None:
        a = build_parser().parse_args(["inbox", "list", "--peek"])
        self.assertTrue(a.peek)
        sent = {}
        client = mock.Mock()
        client.execute_cmd.side_effect = lambda args, cmd, payload=None, **_: sent.update(payload) or {"data": {}}
        from mesh.cli.commands import inbox as inbox_cmd
        a.home = None
        with mock.patch.object(inbox_cmd, "MeshClient", return_value=client):
            inbox_cmd.handle_inbox_list(a)
        self.assertIs(sent["peek"], True)

    def test_agent_create_max_concurrency_optional(self) -> None:
        a = build_parser().parse_args(["agent", "create", "--name", "x", "--adapter", "none"])
        self.assertIsNone(a.max_concurrency)


class TestMergeNote(unittest.TestCase):
    def test_note_names_the_log_once_merging(self) -> None:
        responses = [{"data": {"status": "accepted"}},
                     {"data": {"status": "merging", "merge": {"mrg": "mrg_1"}}}]
        client = mock.Mock()
        client.home = "/h"
        shower = mock.Mock()
        shower.execute_cmd.side_effect = lambda *a, **k: responses.pop(0)
        args = argparse.Namespace(task_id="tsk_1", idempotency_key="k", request_id="r", dry_run=False)
        err = io.StringIO()
        with mock.patch.object(task_cmd, "MeshClient", return_value=shower), mock.patch("sys.stderr", err):
            task_cmd._merge_progress_note(client, args, threading.Event())
        self.assertEqual(err.getvalue(),
                         "mesh: merging tsk_1: running the project's tests if any (log: /h/merges/mrg_1/test.log)...\n")
        self.assertEqual(args.idempotency_key, "k")  # the merge's own args are untouched

    def test_no_note_after_the_merge_returned(self) -> None:
        done = threading.Event()
        done.set()
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            task_cmd._merge_progress_note(mock.Mock(home="/h"), argparse.Namespace(task_id="t"), done)
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
