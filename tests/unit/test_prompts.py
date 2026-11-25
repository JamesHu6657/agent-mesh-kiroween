"""Unit tests for mesh/core/prompts.py per plan §12.5.

ACC: F4 (partial: prompt rendering and rework feedback; the CLI reject → new
prompt.md test is the F4 integration test)
"""

import json
import sqlite3
import unittest

from mesh.core.prompts import (
    PROMPT_LIMIT_BYTES,
    parse_deliverable,
    pointer_prompt,
    prompt_strategy,
    read_rework_feedback,
    render_executor_prompt,
)


def _task(**overrides):
    task = {
        "id": "tsk_abc",
        "title": "Add tests",
        "spec": "Cover the parser.",
        "kind": "general",
        "branch": None,
    }
    task.update(overrides)
    return task


PROJECT = {"id": "prj_abc", "name": "Mesh"}


class TestRenderExecutorPrompt(unittest.TestCase):
    def test_header_workspace_criteria_and_rules(self) -> None:
        prompt = render_executor_prompt(
            _task(),
            PROJECT,
            ["All tests pass", "Docs updated"],
            "/tmp/ws/tasks/tsk_abc/",
            attempt=2,
            max_attempts=3,
        )
        self.assertIn(
            '# Agent Mesh task tsk_abc — attempt 2/3 — project "Mesh" (prj_abc)',
            prompt,
        )
        self.assertIn("You are an executor. Work only in: /tmp/ws/tasks/tsk_abc/", prompt)
        self.assertIn("## Task: Add tests", prompt)
        self.assertIn("Cover the parser.", prompt)
        self.assertIn("## Project acceptance criteria (context only)", prompt)
        self.assertIn("1. All tests pass", prompt)
        self.assertIn("2. Docs updated", prompt)
        self.assertIn("## Rework feedback (if any)", prompt)
        self.assertIn("(none)", prompt)
        self.assertNotIn("git branch", prompt)
        self.assertIn("You cannot approve anything.", prompt)
        self.assertIn("```mesh-deliverable", prompt)
        self.assertIn("mesh task progress", prompt)

    def test_code_task_names_branch(self) -> None:
        prompt = render_executor_prompt(
            _task(kind="code", branch="task/tsk_abc"),
            PROJECT,
            [],
            "/tmp/ws/",
            attempt=1,
            max_attempts=2,
        )
        self.assertIn(
            "You are on git branch task/tsk_abc. Commit your work to this branch before you "
            "finish. Do not push. Do not switch branches.",
            prompt,
        )

    def test_rework_feedback_rendered(self) -> None:
        prompt = render_executor_prompt(
            _task(),
            PROJECT,
            [],
            "/tmp/ws/",
            attempt=2,
            max_attempts=3,
            rework={"reason": "add tests", "previous_summary": "did the work"},
        )
        self.assertIn("add tests  — previous deliverable summary: did the work", prompt)


class TestParseDeliverable(unittest.TestCase):
    def test_last_valid_block_wins(self) -> None:
        text = (
            "first\n```mesh-deliverable\n{\"summary\": \"one\"}\n```\n"
            "second\n```mesh-deliverable\n{\"summary\": \"two\", \"artifacts\": []}\n```\n"
        )
        self.assertEqual(parse_deliverable(text), {"summary": "two", "artifacts": []})

    def test_invalid_json_block_falls_back(self) -> None:
        text = "done\n```mesh-deliverable\nnot json\n```\n"
        result = parse_deliverable(text)
        self.assertTrue(result["structured"] is False)
        self.assertIn("not json", result["summary"])

    def test_no_block_uses_last_4000_chars(self) -> None:
        text = "x" * 5000
        result = parse_deliverable(text)
        self.assertEqual(result["summary"], "x" * 4000)
        self.assertFalse(result["structured"])

    def test_non_object_block_falls_back(self) -> None:
        text = "```mesh-deliverable\n[1, 2]\n```\n"
        result = parse_deliverable(text)
        self.assertFalse(result["structured"])


class TestPromptStrategy(unittest.TestCase):
    def test_strategy_table(self) -> None:
        limit = PROMPT_LIMIT_BYTES
        self.assertEqual(prompt_strategy("droid", "edit", limit * 10), "file")
        self.assertEqual(prompt_strategy("vibe", "edit", limit * 10), "file")
        self.assertEqual(prompt_strategy("fake", "edit", limit), "argv")
        self.assertEqual(prompt_strategy("agy", "edit", limit + 1), "pointer")
        self.assertEqual(prompt_strategy("kiro", "edit", limit + 1), "pointer")
        self.assertEqual(prompt_strategy("kiro", "readonly", limit + 1), "too_long")
        self.assertEqual(prompt_strategy("fake", "edit", limit + 1), "too_long")

    def test_pointer_prompt_names_absolute_file(self) -> None:
        sentence = pointer_prompt("/tmp/mh-x/runs/run_1/")
        self.assertIn("Read the file /tmp/mh-x/runs/run_1/prompt.md", sentence)
        self.assertIn("It is your complete task.", sentence)


class TestReadReworkFeedback(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE events (seq INTEGER PRIMARY KEY, task_id TEXT, type TEXT, reason TEXT, payload_json TEXT);"
        )
        self.conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, deliverable_json TEXT);")

    def tearDown(self) -> None:
        self.conn.close()

    def test_reads_latest_reject_and_stored_wrapper_summary(self) -> None:
        self.conn.execute(
            "INSERT INTO events (task_id, type, reason, payload_json) VALUES (?, 'task.rejected', ?, '{}');",
            ("tsk_1", "first try"),
        )
        self.conn.execute(
            "INSERT INTO events (task_id, type, reason, payload_json) VALUES (?, 'task.rejected', ?, '{}');",
            ("tsk_1", "add tests"),
        )
        wrapper = {
            "deliverable": {"summary": "previous attempt summary"},
            "submitted_by": "ibx",
            "submitted_at": "2026-01-01T00:00:00Z",
        }
        self.conn.execute(
            "INSERT INTO tasks (id, deliverable_json) VALUES (?, ?);",
            ("tsk_1", json.dumps(wrapper)),
        )
        feedback = read_rework_feedback(self.conn, "tsk_1")
        assert feedback is not None
        self.assertEqual(feedback["reason"], "add tests")
        self.assertEqual(feedback["previous_summary"], "previous attempt summary")

    def test_none_when_no_rejection(self) -> None:
        self.conn.execute("INSERT INTO tasks (id, deliverable_json) VALUES ('tsk_2', NULL);")
        self.assertIsNone(read_rework_feedback(self.conn, "tsk_2"))


if __name__ == "__main__":
    unittest.main()
