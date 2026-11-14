"""Unit tests for the lead prompt renderer and its §12.6 size cap.

ACC: L6 (the checklist's Check is `test_lead_prompt_cap`; the integration
clause "messages that did not fit stay pending" is covered by the lead-turn
integration test)
"""

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from mesh.adapters.base import RunRequest
from mesh.adapters.kiro import KiroAdapter
from mesh.core import lead_prompt
from mesh.core.lead_actions import ALLOWED_ACTION_CMDS, parse_actions
from mesh.core.lead_prompt import (
    AUTHORITY_CATEGORIES_BUDGET,
    MESSAGE_BODY_CUT_MIN,
    MIN_LIMIT_BYTES,
    LeadPromptInput,
    render_lead_prompt,
)


def make_input(**overrides):
    """A small, complete LeadPromptInput."""
    base = dict(
        project={"id": "prj_1", "name": "Demo", "goal": "Ship the demo."},
        criteria=[{"id": "crit_1", "text": "It works."}],
        tasks=[
            {"id": "tsk_1", "title": "Do it", "status": "running", "assignee": "fx"},
            {"id": "tsk_2", "title": "Done it", "status": "accepted", "assignee": "fx"},
        ],
        approvals=[{"id": "apr_1", "category": "spend_money", "summary": "Buy a thing"}],
        notes=[{"ts": "2026-01-01T00:00:00Z", "text": "first note"}],
        events=[
            {"seq": 1, "ts": "2026-01-01T00:00:00Z", "type": "project.created", "task_id": None, "reason": None}
        ],
        messages=[
            {
                "id": "msg_1",
                "type": "lead_wake",
                "created_at": "2026-01-01T00:00:00Z",
                "payload": {"text": "hello"},
            }
        ],
        authority={
            "approval_categories": ["spend_money", "delete"],
            "allowed_models": {"fx": ["m1", "m2"]},
            "lead_cannot_be_assignee": True,
        },
        limit_bytes=100_000,
    )
    base.update(overrides)
    return LeadPromptInput(**base)


class TestLeadPrompt(unittest.TestCase):
    """Lead prompt rendering and size cap. ACC: L6"""

    def test_lead_prompt_cap(self):
        """A large lead prompt is capped at max_argv_prompt_bytes. ACC: L6"""
        limit = KiroAdapter.max_argv_prompt_bytes
        messages = [
            {
                "id": "msg_%03d" % i,
                "type": "lead_wake",
                "created_at": "2026-01-01T00:00:00Z",
                "payload": {"text": "m%03d " % i + "x" * 1500},
            }
            for i in range(50)
        ]
        events = [
            {"seq": i, "ts": "2026-01-01T00:00:00Z", "type": "task.progress", "task_id": "tsk_1", "reason": None}
            for i in range(200)
        ]
        notes = [{"ts": "2026-01-01T00:00:00Z", "text": "note %03d " % i + "y" * 1500} for i in range(100)]
        tasks = []
        for i in range(300):
            status = "accepted" if i % 2 == 0 else "running"
            tasks.append(
                {
                    "id": "tsk_%03d" % i,
                    "title": "task %03d " % i + "t" * 400,
                    "status": status,
                    "assignee": "fx",
                }
            )

        inp = make_input(
            messages=messages,
            events=events,
            notes=notes,
            tasks=tasks,
            limit_bytes=limit,
        )
        rendered = render_lead_prompt(inp)

        self.assertLessEqual(rendered.bytes, limit)
        self.assertEqual(rendered.bytes, len(rendered.text.encode("utf-8")))

        omitted_line = [ln for ln in rendered.text.splitlines() if ln.startswith("Omitted:")]
        self.assertEqual(len(omitted_line), 1)
        line = omitted_line[0]
        for part in ("events=", "notes=", "terminal_tasks=", "messages="):
            self.assertIn(part, line)

        # The included messages are a non-empty strict prefix of the input order.
        included = rendered.included_message_ids
        self.assertTrue(included)
        self.assertLess(len(included), len(messages))
        self.assertEqual(included, [m["id"] for m in messages[: len(included)]])

        # The renderer does not touch the messages it dropped: they stay pending.
        self.assertEqual(rendered.omitted["messages"], len(messages) - len(included))

        # Exercise the checklist's readonly argv path, without launching a vendor.
        with tempfile.TemporaryDirectory(prefix="mh-lead-cap-", dir="/tmp") as home:
            prompt_path = os.path.join(home, "prompt.md")
            with open(prompt_path, "w", encoding="utf-8") as handle:
                handle.write(rendered.text)
            command = KiroAdapter().build(RunRequest(
                prompt_path=prompt_path, workspace=home, model="m-a", effort=None,
                profile="readonly",
            ))
            self.assertIn("--trust-tools=", command.argv)
            self.assertEqual(command.argv[-1], rendered.text)
            self.assertEqual(command.cwd, home)
            self.assertEqual(command.stdin, "devnull")

    def test_small_input_renders_every_section(self):
        """A small prompt renders all sections and omits nothing. ACC: L6"""
        rendered = render_lead_prompt(make_input())
        text = rendered.text
        headings = (
            "## Goal",
            "## Acceptance criteria",
            "## Tasks",
            "## Open approvals",
            "## Notes",
            "## Recent events",
            "## Pending messages",
            "## Authority rules",
            "## Actions",
            "## Completion report",
        )
        for heading in headings:
            self.assertIn(heading, text)
        # The sections appear in the documented order, each exactly once.
        offsets = [text.index(heading) for heading in headings]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(len(set(offsets)), len(headings))
        self.assertGreater(text.index("Omitted:"), offsets[-1])
        self.assertIn("Omitted: nothing.", text)
        self.assertEqual(rendered.omitted, {})
        self.assertEqual(rendered.included_message_ids, ["msg_1"])
        self.assertIn("crit_1", text)
        self.assertIn("apr_1", text)
        self.assertIn("first note", text)
        self.assertIn("project.created", text)
        self.assertIn("spend_money", text)
        self.assertIn("cannot be an assignee", text)
        # The message payload stays a JSON value, not a re-encoded string.
        payload_lines = [
            json.loads(ln) for ln in text.splitlines() if ln.startswith('{"created_at"')
        ]
        self.assertEqual(len(payload_lines), 1)
        self.assertEqual(payload_lines[0]["payload"], {"text": "hello"})

    def test_single_huge_message_is_cut_with_pointer(self):
        """One 300 KB message is kept, cut to 4000 chars, with the inbox pointer. ACC: L6"""
        limit = KiroAdapter.max_argv_prompt_bytes
        huge = "z" * 300_000
        inp = make_input(
            messages=[
                {
                    "id": "msg_big",
                    "type": "lead_wake",
                    "created_at": "2026-01-01T00:00:00Z",
                    "payload": {"text": huge},
                }
            ],
            limit_bytes=limit,
        )
        rendered = render_lead_prompt(inp)
        self.assertLessEqual(rendered.bytes, limit)
        self.assertEqual(rendered.included_message_ids, ["msg_big"])
        self.assertIn("mesh inbox list", rendered.text)
        self.assertIn("truncated", rendered.text)
        self.assertEqual(rendered.omitted.get("messages_cut"), 1)
        # The cut payload is a marked string, not the original JSON value.
        payload_lines = [
            json.loads(ln) for ln in rendered.text.splitlines() if ln.startswith('{"created_at"')
        ]
        self.assertEqual(len(payload_lines), 1)
        payload = payload_lines[0]["payload"]
        self.assertIsInstance(payload, str)
        self.assertIn('"truncated": true', payload)
        self.assertIn("mesh inbox list", payload)
        # The body really was cut: the prompt cannot contain the whole 300 KB body.
        self.assertNotIn("z" * 5000, rendered.text)

    def test_single_emoji_message_is_recut_not_dropped(self):
        """One emoji x100000 message at the smallest limit is re-cut, not dropped. ACC: L6"""
        # The first cut is a character cut, so a 4-byte-per-character body can
        # still be far too large; step 10 must re-cut the ORIGINAL payload to
        # MESSAGE_BODY_CUT_MIN instead of dropping the message (which would
        # leave it pending forever and waste the remaining budget).
        limit = MIN_LIMIT_BYTES
        inp = make_input(
            messages=[
                {
                    "id": "msg_1",
                    "type": "lead_wake",
                    "created_at": "2026-01-01T00:00:00Z",
                    "payload": {"text": "😀" * 100_000},
                }
            ],
            limit_bytes=limit,
        )
        rendered = render_lead_prompt(inp)
        self.assertLessEqual(rendered.bytes, limit)
        self.assertEqual(rendered.included_message_ids, ["msg_1"])
        self.assertEqual(rendered.omitted.get("messages_cut"), 1)
        self.assertNotIn("messages", rendered.omitted)
        self.assertIn("mesh inbox list", rendered.text)
        payload_lines = [
            json.loads(ln) for ln in rendered.text.splitlines() if ln.startswith('{"created_at"')
        ]
        self.assertEqual(len(payload_lines), 1)
        payload = payload_lines[0]["payload"]
        self.assertIsInstance(payload, str)
        self.assertIn('"truncated": true', payload)
        self.assertIn("mesh inbox list", payload)
        # The re-cut body is small: the prompt is nowhere near the limit.
        self.assertLess(rendered.bytes, limit - 5000)

    def test_task_status_and_assignee_are_cut_at_entry(self):
        """Task status/assignee are cut at entry and named in Omitted. ACC: L6"""
        inp = make_input(
            tasks=[
                {
                    "id": "tsk_1",
                    "title": "Do it",
                    "status": "s" * 50_000,
                    "assignee": "a" * 50_000,
                }
            ],
            limit_bytes=100_000,
        )
        rendered = render_lead_prompt(inp)
        self.assertLessEqual(rendered.bytes, 100_000)
        omitted_line = [ln for ln in rendered.text.splitlines() if ln.startswith("Omitted:")][0]
        self.assertIn("task_fields_cut=", omitted_line)
        self.assertNotIn("s" * 1000, rendered.text)
        self.assertNotIn("a" * 1000, rendered.text)

    def test_goal_is_on_its_own_line(self):
        """The goal line equals the goal text exactly (Q11). ACC: L6"""
        goal = 'FAKE {"succeed":{"text":"done"}}'
        rendered = render_lead_prompt(make_input(project={"id": "prj_1", "name": "Demo", "goal": goal}))
        lines = rendered.text.splitlines()
        self.assertIn(goal, lines)

    def test_actions_section_lists_exactly_the_allowed_cmds(self):
        """The Actions section lists exactly ALLOWED_ACTION_CMDS, unfenced. ACC: L6"""
        rendered = render_lead_prompt(make_input())
        text = rendered.text
        start = text.index("## Actions")
        end = text.index("## Completion report")
        section = text[start:end]
        listed = [cmd for cmd in ALLOWED_ACTION_CMDS if cmd in section]
        self.assertEqual(listed, ALLOWED_ACTION_CMDS)
        # The prompt must not contain a mesh-actions block of its own: a lead
        # that echoed the prompt would otherwise submit the schema as actions.
        self.assertNotIn("```mesh-actions", text)
        self.assertFalse(parse_actions(text).block_found)

    def test_last_resort_cuts_goal_and_criteria(self):
        """If the fixed parts alone exceed the limit, goal and criteria are cut. ACC: L6"""
        inp = make_input(
            project={"id": "prj_1", "name": "Demo", "goal": "g" * 50_000},
            criteria=[{"id": "crit_1", "text": "c" * 50_000}],
            limit_bytes=20_000,
        )
        rendered = render_lead_prompt(inp)
        self.assertLessEqual(rendered.bytes, 20_000)
        self.assertIn("cut to fit the prompt size limit", rendered.text)
        self.assertEqual(rendered.omitted.get("goal_criteria_cut"), 1)
        omitted_line = [ln for ln in rendered.text.splitlines() if ln.startswith("Omitted:")][0]
        self.assertIn("goal_criteria_cut=", omitted_line)

    def test_lead_prompt_cap_unbounded_fields(self):
        """Fields with no per-item bound are cut until the cap really holds. ACC: L6"""
        limit = KiroAdapter.max_argv_prompt_bytes
        tasks = [
            {
                "id": "tsk_%03d" % i,
                "title": "task %03d " % i + "中" * 1000,
                "status": "running",
                "assignee": "fx",
            }
            for i in range(300)
        ]
        approvals = [
            {"id": "apr_%03d" % i, "category": "spend_money", "summary": "s" * 4000}
            for i in range(200)
        ]
        criteria = [{"id": "crit_%03d" % i, "text": "c" * 2000} for i in range(100)]
        goal = "Ship it — ünïcode ✓ " * 200
        inp = make_input(
            project={"id": "prj_1", "name": "Demo", "goal": goal},
            criteria=criteria,
            tasks=tasks,
            approvals=approvals,
            notes=[],
            events=[],
            messages=[],
            limit_bytes=limit,
        )
        rendered = render_lead_prompt(inp)

        self.assertLessEqual(rendered.bytes, limit)
        self.assertEqual(rendered.bytes, len(rendered.text.encode("utf-8")))
        lines = rendered.text.splitlines()
        self.assertIn("## Actions", lines)
        self.assertIn("## Completion report", lines)
        self.assertIn("## Goal", lines)
        # The goal's first line survives under ## Goal even after the cut: the
        # rendered goal line starts with it (the cut only shortens the tail).
        goal_line = goal.split("\n")[0]
        rendered_goal = lines[lines.index("## Goal") + 2]
        self.assertTrue(rendered_goal.startswith(goal_line[:100]))
        self.assertIn("Ship it — ünïcode ✓", rendered_goal)
        omitted_line = [ln for ln in lines if ln.startswith("Omitted:")][0]
        for part in ("task_titles_cut=", "approval_summaries_cut=", "tasks=", "approvals="):
            self.assertIn(part, omitted_line)
        self.assertIn("mesh task list", rendered.text)
        self.assertIn("mesh approval list", rendered.text)

    def test_lead_prompt_cap_property(self):
        """bytes <= limit for every limit and every filler. ACC: L6"""
        tasks = [
            {"id": "tsk_%03d" % i, "title": "中" * 1000, "status": "running", "assignee": "fx"}
            for i in range(300)
        ]
        approvals = [
            {"id": "apr_%03d" % i, "category": "spend_money", "summary": "s" * 4096}
            for i in range(50)
        ]
        criteria = [{"id": "crit_%03d" % i, "text": "c" * 2048} for i in range(100)]
        messages = [
            {
                "id": "msg_%d" % i,
                "type": "lead_wake",
                "created_at": "2026-01-01T00:00:00Z",
                "payload": {"text": "z" * 300_000},
            }
            for i in range(3)
        ]
        # The message envelope is multibyte too: id/type/created_at are cut at
        # MESSAGE_FIELD_CUT characters, so the line's overhead is ~2.4 KB, not
        # the 200 bytes a fixed guess assumed.
        wide_messages = [
            {
                "id": "😀" * 300,
                "type": "中" * 300,
                "created_at": "😀" * 300,
                "payload": {"text": "😀" * 300_000},
            }
            for _ in range(3)
        ]
        # 4096 used to be in this list; it is below the measured floor of the
        # fixed skeleton (MIN_LIMIT_BYTES), where the renderer now raises
        # ValueError by contract, so it is replaced by 20000.
        limits = (
            MIN_LIMIT_BYTES,
            MIN_LIMIT_BYTES + 1,
            20000,
            MIN_LIMIT_BYTES + 4096,
            KiroAdapter.max_argv_prompt_bytes,
            100_000,
        )
        for limit in limits:
            for filler in ("a", "é", "中", "😀"):
                for message_set in (messages, wide_messages):
                    with self.subTest(limit=limit, filler=filler, wide=message_set is wide_messages):
                        inp = LeadPromptInput(
                            project={
                                "id": "prj_1",
                                "name": filler * 200_000,
                                "goal": filler * 5000,
                            },
                            criteria=criteria,
                            tasks=tasks,
                            approvals=approvals,
                            messages=message_set,
                            authority={
                                "approval_categories": ["spend_money"],
                                "allowed_models": {"fx": [filler * 200_000]},
                            },
                            limit_bytes=limit,
                        )
                        rendered = render_lead_prompt(inp)
                        self.assertLessEqual(rendered.bytes, limit)
                        self.assertEqual(rendered.bytes, len(rendered.text.encode("utf-8")))
                        self.assertIn("## Actions", rendered.text)
                        self.assertIn("## Completion report", rendered.text)
                        # The authority rules are never cut, whatever the filler and
                        # the limit: a lead that does not know them can break the
                        # authority model (no assignee, no approval, allowed models).
                        self.assertIn("## Authority rules", rendered.text)
                        self.assertIn("You cannot be an assignee", rendered.text)
                        self.assertIn("You cannot approve anything", rendered.text)
                        self.assertIn("These categories need user approval", rendered.text)
                        omitted_line = [
                            ln for ln in rendered.text.splitlines() if ln.startswith("Omitted:")
                        ]
                        self.assertEqual(len(omitted_line), 1)
                        self.assertNotEqual(omitted_line[0], "Omitted: nothing.")

    def test_lead_prompt_cap_property_big_authority(self):
        """A huge authority section cannot push the fixed tail out. ACC: L6"""
        # 50 categories, 30 agents x 30 models x 300 chars, every filler: the
        # authority section alone would be ~1 MB, so it must be bounded in
        # total and the fixed tail (## Actions, ## Completion report, the
        # Omitted line) must survive at every limit.
        for filler in ("a", "é", "中", "😀"):
            categories = ["cat_%02d_%s" % (i, filler * 300) for i in range(50)]
            allowed_models = {
                "agent_%02d" % a: ["model_%02d_%s" % (m, filler * 300) for m in range(30)]
                for a in range(30)
            }
            tasks = [
                {"id": "tsk_%03d" % i, "title": filler * 1000, "status": "running", "assignee": "fx"}
                for i in range(300)
            ]
            approvals = [
                {"id": "apr_%03d" % i, "category": "spend_money", "summary": filler * 4096}
                for i in range(50)
            ]
            criteria = [{"id": "crit_%03d" % i, "text": filler * 2048} for i in range(100)]
            messages = [
                {
                    "id": "msg_%d" % i,
                    "type": "lead_wake",
                    "created_at": "2026-01-01T00:00:00Z",
                    "payload": {"text": filler * 300_000},
                }
                for i in range(3)
            ]
            for limit in (MIN_LIMIT_BYTES, MIN_LIMIT_BYTES + 1, MIN_LIMIT_BYTES + 4096, 100_000):
                with self.subTest(limit=limit, filler=filler):
                    inp = LeadPromptInput(
                        project={"id": "prj_1", "name": filler * 200_000, "goal": filler * 5000},
                        criteria=criteria,
                        tasks=tasks,
                        approvals=approvals,
                        messages=messages,
                        authority={
                            "approval_categories": categories,
                            "allowed_models": allowed_models,
                        },
                        limit_bytes=limit,
                    )
                    rendered = render_lead_prompt(inp)
                    self.assertLessEqual(rendered.bytes, limit)
                    self.assertEqual(rendered.bytes, len(rendered.text.encode("utf-8")))
                    lines = rendered.text.splitlines()
                    self.assertIn("## Actions", lines)
                    self.assertIn("## Completion report", lines)
                    omitted_line = [ln for ln in lines if ln.startswith("Omitted:")]
                    self.assertEqual(len(omitted_line), 1)
                    self.assertNotEqual(omitted_line[0], "Omitted: nothing.")
                    # The authority section is bounded in total, and the cut is
                    # named in the Omitted line.
                    self.assertIn("authority_cut=", omitted_line[0])
                    self.assertIn("mesh agent show", rendered.text)
                    # The rule lines survive the cut: they are emitted outside
                    # the byte budget, so a huge category list cannot drop them.
                    self.assertIn("## Authority rules", rendered.text)
                    self.assertIn("You cannot be an assignee", rendered.text)
                    self.assertIn("You cannot approve anything", rendered.text)
                    self.assertIn("These categories need user approval", rendered.text)
                    # The final cut never eats the fixed tail: it only cuts
                    # inside the variable sections, so nothing is reported as
                    # "truncated" (that key means a byte-level chop).
                    self.assertNotIn("truncated", rendered.omitted)
                    # The fixed tail is intact, not chopped mid-line.
                    self.assertIn("Omitted: ", rendered.text)
                    self.assertTrue(rendered.text.rstrip().endswith("."))

    def test_render_does_not_mutate_the_input(self):
        """The renderer copies every dict it cuts; two renders agree. ACC: L6"""
        tasks = [
            {"id": "tsk_%03d" % i, "title": "t" * 1000, "status": "running", "assignee": "fx"}
            for i in range(300)
        ]
        approvals = [
            {"id": "apr_%03d" % i, "category": "spend_money", "summary": "s" * 4000}
            for i in range(50)
        ]
        criteria = [{"id": "crit_%03d" % i, "text": "c" * 2000} for i in range(100)]
        messages = [
            {
                "id": "msg_%d" % i,
                "type": "lead_wake",
                "created_at": "2026-01-01T00:00:00Z",
                "payload": {"text": "z" * 300_000},
            }
            for i in range(3)
        ]
        inp = LeadPromptInput(
            project={"id": "prj_1", "name": "n" * 500, "goal": "g" * 5000},
            criteria=criteria,
            tasks=tasks,
            approvals=approvals,
            messages=messages,
            limit_bytes=MIN_LIMIT_BYTES,
        )
        before = copy.deepcopy(inp)
        first = render_lead_prompt(inp)
        self.assertEqual(inp.tasks, before.tasks)
        self.assertEqual(inp.approvals, before.approvals)
        self.assertEqual(inp.criteria, before.criteria)
        self.assertEqual(inp.messages, before.messages)
        self.assertEqual(inp.project, before.project)
        self.assertEqual(inp.authority, before.authority)
        second = render_lead_prompt(inp)
        self.assertEqual(first.text, second.text)
        self.assertEqual(first.omitted, second.omitted)
        self.assertEqual(first.included_message_ids, second.included_message_ids)

    def test_entry_cuts_are_recorded(self):
        """Every entry cut is named in the Omitted line. ACC: L6"""
        inp = LeadPromptInput(
            project={"id": "p" * 200_000, "name": "n" * 200_000, "goal": "g"},
            tasks=[{"id": "t" * 500, "title": "x" * 500, "status": "running", "assignee": "fx"}],
            approvals=[{"id": "a" * 500, "category": "spend_money", "summary": "s" * 5000}],
            criteria=[{"id": "c" * 500, "text": "c" * 5000}],
            messages=[
                {
                    "id": "m" * 500,
                    "type": "lead_wake",
                    "created_at": "2026-01-01T00:00:00Z",
                    "payload": {"text": "hi"},
                }
            ],
            authority={
                "approval_categories": ["spend_money"],
                "allowed_models": {"fx": ["m" * 200_000]},
            },
            limit_bytes=100_000,
        )
        rendered = render_lead_prompt(inp)
        self.assertLessEqual(rendered.bytes, 100_000)
        omitted_line = [ln for ln in rendered.text.splitlines() if ln.startswith("Omitted:")][0]
        for part in (
            "project_name_cut=",
            "project_id_cut=",
            "task_ids_cut=",
            "task_titles_cut=",
            "approval_ids_cut=",
            "approval_summaries_cut=",
            "criterion_ids_cut=",
            "criteria_text_cut=",
            "message_fields_cut=",
            "authority_strings_cut=",
        ):
            self.assertIn(part, omitted_line)

    def test_limit_below_minimum_is_a_caller_error(self):
        """A limit below MIN_LIMIT_BYTES raises ValueError. ACC: L6"""
        with self.assertRaises(ValueError):
            render_lead_prompt(make_input(limit_bytes=MIN_LIMIT_BYTES - 1))

    def test_authority_omission_counts_are_exact(self):
        """One long category reports authority_strings_cut == 1, not one per build. ACC: L6"""
        # The authority section is rendered once per render, not once per
        # build(): a single 61-char category used to be counted 11 times.
        inp = make_input(
            authority={"approval_categories": ["c" * 61], "allowed_models": {}},
            limit_bytes=100_000,
        )
        rendered = render_lead_prompt(inp)
        self.assertEqual(rendered.omitted.get("authority_strings_cut"), 1)
        self.assertNotIn("authority_cut", rendered.omitted)
        self.assertIn("These categories need user approval", rendered.text)
        self.assertIn("mesh agent show", rendered.text)

    def test_authority_categories_line_is_cut_to_its_sub_budget(self):
        """A huge category list is cut to a pointer, rules kept, budget respected. ACC: L6"""
        # 50 categories of 300 emoji each: the categories line alone would be
        # ~60 KB, far past AUTHORITY_BYTE_BUDGET. It must be cut to its own
        # sub-budget and end with the pointer, and the rule lines must survive.
        categories = ["cat_%02d_%s" % (i, "😀" * 300) for i in range(50)]
        inp = make_input(
            authority={"approval_categories": categories, "allowed_models": {}},
            limit_bytes=100_000,
        )
        rendered = render_lead_prompt(inp)
        self.assertLessEqual(rendered.bytes, 100_000)
        lines = rendered.text.splitlines()
        category_lines = [ln for ln in lines if "categories need user approval" in ln]
        self.assertEqual(len(category_lines), 1)
        self.assertLessEqual(len(category_lines[0].encode("utf-8")), AUTHORITY_CATEGORIES_BUDGET)
        self.assertIn("more", category_lines[0])
        self.assertIn("mesh agent show", category_lines[0])
        self.assertIn("You cannot be an assignee", rendered.text)
        self.assertIn("You cannot approve anything", rendered.text)
        self.assertGreater(rendered.omitted.get("authority_categories", 0), 0)

    def test_dropped_message_clears_messages_cut(self):
        """A message that is dropped is counted as dropped, not as cut. ACC: L6"""
        # Step 10 re-cuts the kept message to the byte room that is left; only
        # when even that does not fit is the message dropped. A dropped message
        # stays pending, so it must not be reported as cut.
        inp = make_input(
            project={"id": "prj_1", "name": "Demo", "goal": "g"},
            criteria=[],
            tasks=[],
            approvals=[],
            notes=[],
            events=[],
            messages=[
                {
                    "id": "msg_1",
                    "type": "lead_wake",
                    "created_at": "2026-01-01T00:00:00Z",
                    "payload": {"text": "z" * 300_000},
                }
            ],
            limit_bytes=MIN_LIMIT_BYTES,
        )
        # Force the drop branch: step 4b's cut does not shrink the body, and the
        # room left for the re-cut is always negative.
        real_cut_body = lead_prompt._cut_body
        calls = []

        def cut_body(body, limit=lead_prompt.MESSAGE_BODY_CUT):
            calls.append(limit)
            if len(calls) == 1:
                return real_cut_body(body, 10**9)
            return real_cut_body(body, limit)

        with mock.patch.object(lead_prompt, "_cut_body", cut_body), mock.patch.object(
            lead_prompt, "MESSAGE_LINE_OVERHEAD", 10**9
        ):
            rendered = render_lead_prompt(inp)
        self.assertLessEqual(rendered.bytes, MIN_LIMIT_BYTES)
        self.assertEqual(rendered.included_message_ids, [])
        self.assertEqual(rendered.omitted.get("messages"), 1)
        self.assertNotIn("messages_cut", rendered.omitted)
        omitted_line = [ln for ln in rendered.text.splitlines() if ln.startswith("Omitted:")][0]
        self.assertIn("messages=1", omitted_line)
        self.assertNotIn("messages_cut=", omitted_line)

    def test_kept_message_is_recut_to_the_remaining_room(self):
        """Step 10 re-cuts to the room left instead of dropping the message. ACC: L6"""
        # The message is kept and cut, and the prompt uses the budget it has:
        # the re-cut body is larger than MESSAGE_BODY_CUT_MIN because there is
        # room for it, so the message is not dropped and not wasted.
        inp = make_input(
            project={"id": "prj_1", "name": "Demo", "goal": "g"},
            criteria=[],
            tasks=[],
            approvals=[],
            notes=[],
            events=[],
            messages=[
                {
                    "id": "msg_1",
                    "type": "lead_wake",
                    "created_at": "2026-01-01T00:00:00Z",
                    "payload": {"text": "z" * 300_000},
                }
            ],
            limit_bytes=MIN_LIMIT_BYTES,
        )
        rendered = render_lead_prompt(inp)
        self.assertLessEqual(rendered.bytes, MIN_LIMIT_BYTES)
        self.assertEqual(rendered.included_message_ids, ["msg_1"])
        self.assertEqual(rendered.omitted.get("messages_cut"), 1)
        self.assertNotIn("messages", rendered.omitted)
        payload_lines = [
            json.loads(ln) for ln in rendered.text.splitlines() if ln.startswith('{"created_at"')
        ]
        self.assertEqual(len(payload_lines), 1)
        body = json.loads(payload_lines[0]["payload"])
        self.assertTrue(body["truncated"])
        # The body uses the room that is left, not the 200-char floor.
        self.assertGreater(len(body["text"]), MESSAGE_BODY_CUT_MIN)

    def test_step_10_terminates_on_multibyte_envelope(self):
        """Step 10 always terminates and honours the limit. ACC: L6"""
        # The message envelope (id/type/created_at) is multibyte and up to
        # MESSAGE_FIELD_CUT characters each, so a fixed 200-byte overhead guess
        # left a room that never shrank and the re-cut loop spun forever. Every
        # pass must now make progress: the render returns, the limit holds, and
        # the message is either kept (cut) or dropped (counted, not cut).
        for filler in ("😀", "中"):
            for limit in (MIN_LIMIT_BYTES, MIN_LIMIT_BYTES + 1):
                with self.subTest(filler=filler, limit=limit):
                    inp = LeadPromptInput(
                        project={"id": "p", "name": "n", "goal": filler * 50_000},
                        tasks=[
                            {
                                "id": "t%d" % i,
                                "title": filler * 3000,
                                "status": "running",
                                "assignee": "x",
                            }
                            for i in range(30)
                        ],
                        messages=[
                            {
                                "id": filler * 300,
                                "type": filler * 300,
                                "created_at": filler * 300,
                                "payload": filler * 100_000,
                            }
                        ],
                        limit_bytes=limit,
                    )
                    rendered = render_lead_prompt(inp)
                    self.assertLessEqual(rendered.bytes, limit)
                    self.assertEqual(rendered.bytes, len(rendered.text.encode("utf-8")))
                    self.assertIn("## Actions", rendered.text)
                    self.assertIn("## Completion report", rendered.text)
                    if rendered.included_message_ids:
                        self.assertEqual(rendered.omitted.get("messages_cut"), 1)
                        self.assertNotIn("messages", rendered.omitted)
                    else:
                        self.assertEqual(rendered.omitted.get("messages"), 1)
                        self.assertNotIn("messages_cut", rendered.omitted)

    def test_authority_categories_over_the_list_cap_are_counted(self):
        """Categories over the list cap end with "and N more". ACC: L6"""
        # 15 short categories fit the byte budget but not the list cap of 10:
        # the line must say how many are hidden, not silently stop at c9.
        categories = ["c%d" % i for i in range(15)]
        rendered = render_lead_prompt(
            make_input(authority={"approval_categories": categories, "allowed_models": {}})
        )
        lines = [ln for ln in rendered.text.splitlines() if "need user approval" in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn("and 5 more", lines[0])
        self.assertIn("mesh agent show", lines[0])
        self.assertEqual(rendered.omitted.get("authority_categories"), 5)


if __name__ == "__main__":
    unittest.main()
