"""Full-codebase review (Carbon via agy, Nov 14) fixes: core/cli unit coverage."""
import argparse
import sqlite3
import unittest

from mesh.core.redact import redact, redact_value
from mesh.core.clock import parse_duration, now_iso_utc, format_duration
from mesh.core.prompts import parse_deliverable
from mesh.core.db import transaction
from mesh.cli.main import is_text_format

F = "`" * 3
SECRET = "sk-" + "0123456789abcdef" + "0123456789"  # built at runtime: keeps scanners quiet


class TestRedactKeys(unittest.TestCase):
    def test_dict_keys_are_redacted(self):
        out = redact_value({SECRET: 1, "ok": {"inner " + SECRET: "x"}})
        self.assertNotIn(SECRET, repr(out))
        self.assertEqual(out["ok"], {redact("inner " + SECRET): "x"})
        self.assertNotEqual(redact_value({SECRET: 1}), {SECRET: 1})

    def test_plain_keys_unchanged(self):
        self.assertEqual(redact_value({"a": "b", 1: "c"}), {"a": "b", 1: "c"})


class TestFenceNotArmed(unittest.TestCase):
    def test_bearer_suffix_keeps_actions_fence(self):
        text = "the standard-bearer\n" + F + "mesh-actions\n[]\n" + F
        self.assertIn(F + "mesh-actions", redact(text))

    def test_redacted_bearer_tail_keeps_deliverable_fence(self):
        text = "Authorization: Bearer abcdefghijklmnop_bearer\n" + F + "mesh-deliverable\n{}\n" + F
        out = redact(text)
        self.assertIn(F + "mesh-deliverable", out)
        self.assertNotIn("abcdefghijklmnop", out)

    def test_armed_value_still_redacted(self):
        self.assertNotIn("hunter2value", redact("password:\nhunter2value"))


class TestClockBools(unittest.TestCase):
    def test_bools_rejected(self):
        for fn in (parse_duration, now_iso_utc, format_duration):
            with self.assertRaises(ValueError):
                fn(True)
        self.assertEqual(parse_duration(5), 5)


class TestDeliverableFence(unittest.TestCase):
    def test_inline_backticks_in_json_string(self):
        body = '{"summary": "Fixed ' + F + 'mesh-actions' + F + ' parsing", "artifacts": ["a.py"]}'
        out = parse_deliverable("done\n" + F + "mesh-deliverable\n" + body + "\n" + F + "\n")
        self.assertEqual(out.get("artifacts"), ["a.py"])

    def test_plain_block_still_parses(self):
        out = parse_deliverable(F + "mesh-deliverable\n{\"summary\": \"s\"}\n" + F)
        self.assertEqual(out, {"summary": "s"})


class TestPr41R1(unittest.TestCase):
    def test_last_closed_block_wins_over_regex_only(self):
        draft = F + "mesh-deliverable\n{\"summary\": \"draft\"}\n" + F + "\n"
        final = F + "mesh-deliverable\n{\"summary\": \"final\"}\n" + F + "\n"
        self.assertEqual(parse_deliverable(draft + "x\n" + final), {"summary": "final"})

    def test_input_json_deliverable_is_sent(self):
        from unittest import mock
        from mesh.cli.commands import task as task_cmd
        sent = {}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def execute_cmd(self, args, cmd, payload, **k):
                sent.update(payload)
                return {"data": {}}

        args = argparse.Namespace(task_id="tsk_1", run="run_1", fence=1, deliverable={"summary": "done", "artifacts": ["a.py"]},
                                  summary=None, home="/tmp/x", _from_json={"deliverable", "run", "fence"})
        with mock.patch.object(task_cmd, "MeshClient", FakeClient):
            task_cmd.handle_task_submit(args)
        self.assertEqual(sent.get("deliverable"), {"summary": "done", "artifacts": ["a.py"]})


class TestTransactionRollback(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.execute("CREATE TABLE t (x INTEGER);")

    def test_base_exception_rolls_back(self):
        with self.assertRaises(KeyboardInterrupt):
            with transaction(self.conn):
                self.conn.execute("INSERT INTO t VALUES (1);")
                raise KeyboardInterrupt
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM t;").fetchone()[0], 0)

    def test_already_rolled_back_keeps_real_error(self):
        class Boom(Exception):
            pass
        with self.assertRaises(Boom):
            with transaction(self.conn):
                self.conn.execute("ROLLBACK;")
                raise Boom


class TestTextFormat(unittest.TestCase):
    def test_last_format_wins_and_double_dash(self):
        self.assertFalse(is_text_format(["--format", "text", "--format", "json"]))
        self.assertTrue(is_text_format(["--format=json", "--format", "text"]))
        self.assertFalse(is_text_format(["inbox", "ack", "--", "--format", "text"]))


class TestDepConflict(unittest.TestCase):
    def test_conflicting_on_values(self):
        from mesh.cli.commands.dep import _on_value
        from mesh.errors import MeshError
        with self.assertRaises(MeshError) as cm:
            _on_value(argparse.Namespace(on="tsk_3", on_arg="tsk_1"))
        self.assertEqual(cm.exception.code, "DUPLICATE_PARAM")
        self.assertEqual(_on_value(argparse.Namespace(on=None, on_arg="tsk_1")), "tsk_1")
        self.assertEqual(_on_value(argparse.Namespace(on="tsk_1", on_arg="tsk_1")), "tsk_1")


class TestEventIntArg(unittest.TestCase):
    def test_limit_validation(self):
        from mesh.daemon.handlers.event import _int_arg
        from mesh.errors import MeshError
        for bad in (-1, 0, "abc", True, 10001):
            with self.assertRaises(MeshError):
                _int_arg(bad, "limit", 1, 10000)
        self.assertEqual(_int_arg("20", "limit", 1, 10000), 20)
        self.assertIsNone(_int_arg(None, "limit", 1, 10000))


if __name__ == "__main__":
    unittest.main()
