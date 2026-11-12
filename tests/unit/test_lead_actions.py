"""Unit tests for the `mesh-actions` parser and allowed command list.

ACC: L2 (parser part; execution is covered by the lead-turn integration test)
"""

import json
import unittest

from mesh.core.lead_actions import (
    ALLOWED_ACTION_CMDS,
    ERR_CMD_NOT_ALLOWED,
    ERR_INVALID_ITEM,
    ERR_TOO_MANY_ACTIONS,
    MAX_ACTIONS,
    _iter_blocks,
    action_idempotency_key,
    parse_actions,
)


def block(items, info="mesh-actions"):
    """Render a fenced block around a JSON payload."""
    return "```%s\n%s\n```" % (info, json.dumps(items))


class TestLeadActions(unittest.TestCase):
    """Parser and allowed-command list for lead turns. ACC: L2 (parser part)"""

    def test_recovery_parser_requires_the_final_complete_actions_block(self):
        from mesh.core.lead_actions import parse_final_actions_block

        first = block([{"cmd": "task.merge", "args": {"task": "first"}}])
        final = block([{"cmd": "task.merge", "args": {"task": "final"}}])
        for text in (final, first + "\n" + final,
                     (first + "\n" + final).replace("\n", "\r\n")):
            with self.subTest(valid=text):
                parsed = parse_final_actions_block(text)
                self.assertTrue(parsed.block_found)
                self.assertIsNone(parsed.error)
                self.assertEqual(parsed.actions[0].args, {"task": "final"})
        for suffix in ('```mesh-actions\n[{"cmd":', '```mesh-actions\n[]',
                       '````mesh-actions\n[]\n```', '```mesh-actions\ninvalid\n```',
                       '```mesh-actions\n{}\n```', '```json\n{}\n```'):
            with self.subTest(invalid=suffix):
                parsed = parse_final_actions_block(first + "\n" + suffix)
                self.assertEqual(parsed.actions, [])
                # Normal lead turns still deliberately use last-valid parsing.
                self.assertEqual(parse_actions(first + "\n" + suffix).actions[0].args,
                                 {"task": "first"})
        self.assertEqual(parse_final_actions_block("no blocks").actions, [])
        self.assertEqual(parse_final_actions_block(None).actions, [])
        oversized = parse_final_actions_block(block(
            [{"cmd": "task.merge"}] * (MAX_ACTIONS + 1)))
        self.assertEqual(oversized.error, ERR_TOO_MANY_ACTIONS)
        self.assertEqual(oversized.actions, [])

    def test_allowed_cmds_are_exactly_the_plan_list(self):
        """The allowed list is the 15 cmds of plan §12.4. ACC: L2"""
        self.assertEqual(
            ALLOWED_ACTION_CMDS,
            [
                "task.create",
                "task.assign",
                "task.reassign",
                "task.accept",
                "task.reject",
                "task.retry",
                "task.escalate",
                "task.cancel",
                "task.merge",
                "dep.add",
                "dep.remove",
                "project.note",
                "project.complete",
                "project.deliver",
                "approval.request",
            ],
        )
        self.assertEqual(len(ALLOWED_ACTION_CMDS), 15)
        self.assertEqual(MAX_ACTIONS, 50)

    def test_optional_approval_key(self):
        """An item's optional top-level `approval` is kept as the approval id; a
        non-string one is an invalid item; an empty one is no approval. ACC: L2"""
        parsed = parse_actions(block([
            {"cmd": "task.retry", "args": {"task": "tsk_1"}, "approval": "apr_1"},
            {"cmd": "task.retry", "args": {"task": "tsk_1"}, "approval": 7},
            {"cmd": "task.retry", "args": {"task": "tsk_1"}, "approval": ""},
            {"cmd": "task.retry", "args": {"task": "tsk_1"}},
        ]))
        self.assertEqual([a.approval for a in parsed.actions], ["apr_1", None, None, None])
        self.assertEqual([a.error for a in parsed.actions], [None, ERR_INVALID_ITEM, None, None])
        self.assertEqual(parsed.actions[0].to_dict()["approval"], "apr_1")

    def test_last_valid_block_wins(self):
        """The last valid mesh-actions block overrides an earlier one. ACC: L2"""
        text = "\n".join(
            [
                "Some prose.",
                block([{"cmd": "task.create", "args": {"title": "first"}}]),
                "More prose.",
                block([{"cmd": "project.note", "args": {"text": "second"}}]),
                "Done.",
            ]
        )
        parsed = parse_actions(text)
        self.assertTrue(parsed.block_found)
        self.assertIsNone(parsed.error)
        self.assertEqual(len(parsed.actions), 1)
        self.assertEqual(parsed.actions[0].cmd, "project.note")
        self.assertEqual(parsed.actions[0].args, {"text": "second"})
        self.assertEqual(parsed.actions[0].index, 0)
        self.assertIsNone(parsed.actions[0].error)

    def test_invalid_json_last_block_falls_back(self):
        """A last block that is not JSON falls back to the previous valid one. ACC: L2"""
        text = "\n".join(
            [
                block([{"cmd": "task.accept", "args": {"task": "tsk_1"}}]),
                "```mesh-actions\n[{\"cmd\": \"task.accept\", oops\n```",
            ]
        )
        parsed = parse_actions(text)
        self.assertTrue(parsed.block_found)
        self.assertIsNone(parsed.error)
        self.assertEqual([a.cmd for a in parsed.actions], ["task.accept"])

    def test_non_array_block_is_ignored(self):
        """A block whose body is not a JSON array is skipped. ACC: L2"""
        text = "\n".join(
            [
                block([{"cmd": "task.create", "args": {}}]),
                block({"cmd": "task.create", "args": {}}),
            ]
        )
        parsed = parse_actions(text)
        self.assertTrue(parsed.block_found)
        self.assertEqual(len(parsed.actions), 1)
        self.assertEqual(parsed.actions[0].cmd, "task.create")

        only_object = parse_actions(block({"cmd": "task.create", "args": {}}))
        self.assertFalse(only_object.block_found)
        self.assertEqual(only_object.actions, [])
        self.assertIsNone(only_object.error)

    def test_other_info_strings_are_ignored(self):
        """Only the exact info string `mesh-actions` counts. ACC: L2"""
        text = block([{"cmd": "task.create", "args": {}}], info="mesh-deliverable")
        parsed = parse_actions(text)
        self.assertFalse(parsed.block_found)
        self.assertEqual(parsed.actions, [])

    def test_cmd_not_allowed_is_an_item_error(self):
        """`config.set` is rejected while the valid item survives. ACC: L2"""
        text = block(
            [
                {"cmd": "task.create", "args": {"title": "t", "spec": "s", "assignee": "fx"}},
                {"cmd": "config.set", "args": {"key": "lead.default_agent", "value": "x"}},
            ]
        )
        parsed = parse_actions(text)
        self.assertTrue(parsed.block_found)
        self.assertIsNone(parsed.error)
        self.assertEqual(len(parsed.actions), 2)

        first, second = parsed.actions
        self.assertEqual(first.index, 0)
        self.assertEqual(first.cmd, "task.create")
        self.assertIsNone(first.error)
        self.assertEqual(first.args["assignee"], "fx")

        self.assertEqual(second.index, 1)
        self.assertEqual(second.cmd, "config.set")
        self.assertEqual(second.error, ERR_CMD_NOT_ALLOWED)

    def test_invalid_items_are_item_errors(self):
        """Non-object items, missing/non-string cmd and non-object args. ACC: L2"""
        text = block(
            [
                "not-an-object",
                {"args": {}},
                {"cmd": 7, "args": {}},
                {"cmd": "task.create", "args": ["nope"]},
                {"cmd": "task.create"},
            ]
        )
        parsed = parse_actions(text)
        self.assertIsNone(parsed.error)
        self.assertEqual(len(parsed.actions), 5)
        self.assertEqual([a.error for a in parsed.actions[:4]], [ERR_INVALID_ITEM] * 4)
        # A missing `args` defaults to {} and stays valid.
        self.assertIsNone(parsed.actions[4].error)
        self.assertEqual(parsed.actions[4].args, {})
        self.assertEqual([a.index for a in parsed.actions], [0, 1, 2, 3, 4])

    def test_too_many_actions_refuses_the_whole_block(self):
        """51 items refuse the block entirely. ACC: L2"""
        items = [{"cmd": "project.note", "args": {"text": "n%d" % i}} for i in range(MAX_ACTIONS + 1)]
        parsed = parse_actions(block(items))
        self.assertTrue(parsed.block_found)
        self.assertEqual(parsed.error, ERR_TOO_MANY_ACTIONS)
        self.assertEqual(parsed.actions, [])

        at_limit = parse_actions(block(items[:MAX_ACTIONS]))
        self.assertIsNone(at_limit.error)
        self.assertEqual(len(at_limit.actions), MAX_ACTIONS)

    def test_idempotency_key_format_and_charset(self):
        """Keys are `act:<run_id>:<index>` and fit the §13.1 charset. ACC: L2"""
        key = action_idempotency_key("run_01H8XYZ", 3)
        self.assertEqual(key, "act:run_01H8XYZ:3")
        # The charset is asserted literally, not through the module's own regex.
        self.assertRegex(key, r"^[A-Za-z0-9._:-]{1,128}$")
        self.assertLessEqual(len(key), 128)
        self.assertEqual(action_idempotency_key("run_01H8XYZ", 0), "act:run_01H8XYZ:0")

        # A run id with characters outside the charset is refused, not silently mangled.
        with self.assertRaises(ValueError):
            action_idempotency_key("run with space", 0)
        with self.assertRaises(ValueError):
            action_idempotency_key("run/with/slash", 0)
        # A run id long enough to push the key past 128 chars is refused too.
        with self.assertRaises(ValueError):
            action_idempotency_key("r" * 200, 0)

    def test_no_block_found(self):
        """Text without a mesh-actions block is not an error. ACC: L2"""
        parsed = parse_actions("I did some work but emitted no actions.")
        self.assertFalse(parsed.block_found)
        self.assertEqual(parsed.actions, [])
        self.assertIsNone(parsed.error)

        self.assertFalse(parse_actions("").block_found)

    def test_crlf_line_ends_are_accepted(self):
        """A block with CRLF line ends parses like an LF one. ACC: L2"""
        text = "Prose.\r\n```mesh-actions\r\n%s\r\n```\r\nDone.\r\n" % (
            json.dumps([{"cmd": "project.note", "args": {"text": "crlf"}}]),
        )
        parsed = parse_actions(text)
        self.assertTrue(parsed.block_found)
        self.assertIsNone(parsed.error)
        self.assertEqual([a.cmd for a in parsed.actions], ["project.note"])
        self.assertEqual(parsed.actions[0].args, {"text": "crlf"})

    def test_only_lf_and_crlf_end_lines(self):
        """A bare CR or U+2028 inside a JSON string does not end a line. ACC: L2"""
        # A bare CR is not a line end: the block still parses and the argument
        # keeps its CR instead of being split into two lines.
        items = [{"cmd": "project.note", "args": {"text": "a\rb"}}]
        text = "```mesh-actions\n%s\n```" % (json.dumps(items),)
        parsed = parse_actions(text)
        self.assertTrue(parsed.block_found)
        self.assertEqual(parsed.actions[0].args, {"text": "a\rb"})

        # U+2028 is a Unicode line separator; `str.splitlines` would split on
        # it and break the JSON body. It must survive unchanged.
        items = [{"cmd": "project.note", "args": {"text": "a\u2028b"}}]
        text = "```mesh-actions\n%s\n```" % (json.dumps(items),)
        parsed = parse_actions(text)
        self.assertTrue(parsed.block_found)
        self.assertEqual(parsed.actions[0].args, {"text": "a\u2028b"})

        # A bare CR between two fence lines is not a line end either, so the
        # block is not found: the opening line is ```` ```mesh-actions\r``` ````.
        not_a_block = "```mesh-actions\r```"
        self.assertFalse(parse_actions(not_a_block).block_found)
        self.assertEqual(list(_iter_blocks(not_a_block)), [])

    def test_longer_opening_fence_needs_a_long_enough_closing_fence(self):
        """A 4-backtick block closes on 4+ backticks and may hold 3-backtick lines. ACC: L2"""
        items = [{"cmd": "task.accept", "args": {"task": "tsk_1"}}]
        text = "````mesh-actions\n%s\n````" % (json.dumps(items),)
        parsed = parse_actions(text)
        self.assertTrue(parsed.block_found)
        self.assertEqual([a.cmd for a in parsed.actions], ["task.accept"])

        # A body line with three backticks does not close a four-backtick block:
        # the block runs to the four-backtick line, so its body is not JSON and
        # the block is skipped rather than truncated at the inner fence.
        body = json.dumps(items)
        text_with_inner = "````mesh-actions\n```\n%s\n````" % (body,)
        self.assertFalse(parse_actions(text_with_inner).block_found)
        self.assertEqual(
            list(_iter_blocks(text_with_inner)),
            [("mesh-actions", "```\n%s" % (body,))],
        )

        # A three-backtick closing fence is too short for a four-backtick opening.
        too_short = "````mesh-actions\n%s\n```" % (body,)
        self.assertFalse(parse_actions(too_short).block_found)


if __name__ == "__main__":
    unittest.main()
