"""Resume plumbing through real CLI calls and fake-only runs (plan §12.3–§12.4)."""

import json
import os
import sys
import unittest

from tests.helpers import MeshHarness


def fake_steps(session_id, finish):
    return "FAKE " + json.dumps({"steps": [
        {"emit_session": {"session_id": session_id}}, finish,
    ]})


class TestResume(unittest.TestCase):
    def setUp(self):
        self.h = MeshHarness(init_db=True, start_daemon=True)
        self.addCleanup(self.h.teardown)
        self.binary = os.path.join(self.h.home, "fake-python")
        os.symlink(sys.executable, self.binary)
        for name, adapter, delivery in (("lead", "none", "inbox"),
                                         ("verified", "fake", "launch"),
                                         ("fresh", "fake", "launch")):
            args = ["agent", "create", "--name", name, "--adapter", adapter,
                    "--delivery", delivery, "--can-lead", "--max-concurrency", "1"]
            if delivery == "launch":
                args += ["--allowed-model", "m-a", "--default-model", "m-a", "--binary", self.binary]
            if name == "verified":
                args += ["--option", "resume_verified=true"]
            rc, result = self.h.setup_cli(*args, user=True)
            self.assertEqual(rc, 0, result)
        for key, value in (("lead.wake_debounce_s", "0"), ("lead.wake_backoff_s", "[600,600,600]")):
            rc, result = self.h.setup_cli("config", "set", key, value, user=True)
            self.assertEqual(rc, 0, result)

    def rows(self, sql, params=()):
        conn = self.h.db()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def call(self, *args):
        rc, result = self.h.cli(*args, user=True)
        self.assertEqual(rc, 0, result)
        return result["data"]

    def project(self, lead, goal):
        return self.call("project", "create", "--title", "Resume project",
                         "--goal", goal, "--criterion", "C1", "--lead", lead)["id"]

    def runs(self, kind, entity):
        column = "task_id" if kind == "task" else "project_id"
        return self.rows(f"SELECT * FROM runs WHERE kind = ? AND {column} = ? "
                         "ORDER BY started_at, id;", (kind, entity))

    def wait_closed(self, kind, entity, count):
        def done():
            runs = self.runs(kind, entity)
            return len(runs) == count and all(r["status"] not in ("starting", "running") for r in runs)
        self.assertTrue(self.h.wait_for(done, timeout=45), (kind, entity, count))
        return self.runs(kind, entity)

    def assert_argv(self, run, session_id):
        argv = json.loads(run["argv_redacted_json"])
        self.assertEqual(argv[0], self.binary)
        if session_id:
            self.assertEqual(argv[argv.index("--fake-resume") + 1], session_id)
            with open(os.path.join(run["run_dir"], "stdout.log"), encoding="utf-8") as f:
                self.assertIn(json.dumps({"fake_resume": session_id}), f.read())
        else:
            self.assertNotIn("--fake-resume", argv)
        with open(os.path.join(run["run_dir"], "prompt.md"), encoding="utf-8") as f:
            return f.read()

    def test_l5_executor_rework_retry_and_lead_turns(self):
        """ACC: L5 - capture executor/lead sessions with and without verification;
        rework/retry/next turns resume only verified ids and always get full prompts."""
        project = self.project("lead", "Executor sessions")
        for agent in ("verified", "fresh"):
            with self.subTest(agent=agent):
                session = f"executor-{agent}"
                spec = fake_steps(session, {"succeed": {"text": "Original task output"}})
                task = self.call("task", "create", "--project", project, "--title", "Session task",
                                 "--spec", spec, "--assignee", agent)["id"]
                first = self.wait_closed("task", task, 1)[0]
                self.assertEqual(first["exit_code"], 0)
                self.assertEqual(first["session_id"], session)
                self.assert_argv(first, None)
                self.assertEqual(self.rows("SELECT last_session_id FROM tasks WHERE id = ?;",
                                           (task,))[0]["last_session_id"], session)
                feedback = f"Please add the missing {agent} regression tests"
                self.call("task", "reject", task, "--reason", feedback)
                second = self.wait_closed("task", task, 2)[1]
                self.assertEqual(second["session_id"], session)
                prompt = self.assert_argv(second, session if agent == "verified" else None)
                for text in (feedback, spec, "## Project acceptance criteria", "C1", "Task completed."):
                    self.assertIn(text, prompt)

                failed_session = f"failed-{agent}"
                failed = self.call("task", "create", "--project", project, "--title", "Retry session",
                                   "--spec", fake_steps(failed_session, {"fail": {}}),
                                   "--assignee", agent)["id"]
                failed_first = self.wait_closed("task", failed, 1)[0]
                self.assertEqual(failed_first["exit_code"], 1)
                self.assertEqual(failed_first["session_id"], failed_session)
                self.call("task", "retry", failed)
                failed_second = self.wait_closed("task", failed, 2)[1]
                self.assertEqual(failed_second["session_id"], failed_session)
                self.assert_argv(failed_second, failed_session if agent == "verified" else None)

                lead_session = f"lead-{agent}"
                goal = fake_steps(lead_session, {"lead_actions": {"actions": []}})
                lead_project = self.project(agent, goal)
                lead_first = self.wait_closed("lead_turn", lead_project, 1)[0]
                self.assertEqual(lead_first["session_id"], lead_session)
                self.assert_argv(lead_first, None)
                assignment = lead_first["lead_assignment_id"]
                self.assertEqual(self.rows("SELECT session_id FROM lead_assignments WHERE id = ?;",
                                           (assignment,))[0]["session_id"], lead_session)
                self.h.hold_launch_starts()
                next_session = f"next-lead-{agent}"
                next_goal = fake_steps(next_session, {"lead_actions": {"actions": []}})
                self.call("project", "update", lead_project, "--goal", next_goal)
                self.call("intake", "submit", "--project", lead_project, "--message", feedback)
                self.h.release_launch_starts()
                lead_second = self.wait_closed("lead_turn", lead_project, 2)[1]
                self.assertEqual(lead_second["session_id"], next_session)
                prompt = self.assert_argv(lead_second, lead_session if agent == "verified" else None)
                for text in (next_goal, feedback, "C1", "## Authority rules"):
                    self.assertIn(text, prompt)
                self.assertEqual(self.rows("SELECT session_id FROM lead_assignments WHERE id = ?;",
                                           (assignment,))[0]["session_id"], next_session)
