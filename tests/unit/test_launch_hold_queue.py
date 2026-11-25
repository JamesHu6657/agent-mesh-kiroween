"""M3 gate re-review P2 (L11 kiro hold): the scheduler re-checks ``evidence.LAUNCH_HOLDS`` at
T6 and right before T5, so a launch task on a held adapter that was assigned or queued before
the hold (AG step 4 never saw it) fails with ``launch_held`` and starts no run.

ACC: N19

Also pins P3-1: docs/adapters.md no longer claims that ``agent create`` returns rc 13 for
missing launch evidence.
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from unittest.mock import MagicMock

from mesh.adapters import evidence
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.daemon import scheduler
from mesh.daemon.scheduler import check_queued_tasks_t6, start_queued_launch_tasks

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HELD_DETAILS = {"adapter": "kiro", "reason": "launch_held", "milestone": "M4"}


def _installed_adapter(installed: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.kind = "vendor"
    adapter.is_installed.return_value = installed
    return adapter


class TestLaunchHoldQueue(unittest.TestCase):
    def setUp(self) -> None:
        self.assertNotIn(evidence.UNHOLD_ENV, os.environ)
        # M4: the production table is empty (kiro N13 passed); hold kiro here to test the tool.
        holds = mock.patch.dict(evidence.LAUNCH_HOLDS, {"kiro": "M4"})
        holds.start()
        self.addCleanup(holds.stop)
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-lhq-unit-")
        self.conn = init_db(os.path.join(self.temp_dir.name, "mesh.db"))
        now = now_iso_utc()
        with transaction(self.conn):
            ex = self.conn.execute
            ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr-1', 'user', 'testuser', ?);", (now,))
            for aid, name, adapter, delivery, lead in (
                ("agt-lead", "lead1", "none", "inbox", 1),
                ("agt-kiro", "kx", "kiro", "launch", 0),
                ("agt-droid", "dx", "droid", "launch", 0),
            ):
                ex("INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, ?);", (aid, name, now))
                ex("""INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at)
                      VALUES (?, ?, ?, ?, 1, ?, ?);""", (aid, adapter, delivery, lead, now, now))
            ex("""INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at)
                  VALUES ('prj-1', 'Proj 1', 'Goal', 'active', 'usr-1', 'direct', ?, ?);""", (now, now))
            ex("""INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
                  VALUES ('la-1', 'prj-1', 'agt-lead', 'usr-1', 'direct', ?);""", (now,))
            # Both queued as if assigned before the hold landed (no AG step 4 on the way here).
            for tid, agent in (("tsk-kiro", "agt-kiro"), ("tsk-droid", "agt-droid")):
                ex("""INSERT INTO tasks (
                        id, project_id, title, spec, kind, status, priority,
                        assignee_agent_id, profile, timeout_s, max_attempts, attempt, fence_seq,
                        created_by, version, queued_at, created_at, updated_at
                      ) VALUES (?, 'prj-1', 't', 's', 'general', 'queued', 0,
                                ?, 'edit', 3600, 2, 0, 0, 'usr-1', 1, ?, ?, ?);""",
                   (tid, agent, now, now, now))

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _task(self, tid: str):
        return self.conn.execute("SELECT status, version, failure_json FROM tasks WHERE id = ?;", (tid,)).fetchone()

    def _assert_held_failed(self, tid: str = "tsk-kiro") -> None:
        row = self._task(tid)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(json.loads(row["failure_json"]), {"reason": "launch_held", "details": HELD_DETAILS})
        events = self.conn.execute(
            "SELECT reason FROM events WHERE task_id = ? AND type = 'task.failed';", (tid,)).fetchall()
        self.assertEqual([e["reason"] for e in events], ["launch_held"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?;", (tid,)).fetchone()[0], 0)

    def test_held_milestone_table_and_unhold(self):
        """ACC: N19 - kiro is held (M4) outside testing mode even with the unhold env; testing
        mode lifts it only through MESH_TEST_LAUNCH_UNHOLD; droid is never held."""
        self.assertEqual(evidence.held_milestone("kiro", False, {}), "M4")
        self.assertEqual(evidence.held_milestone("kiro", False, {evidence.UNHOLD_ENV: "kiro"}), "M4")
        self.assertEqual(evidence.held_milestone("kiro", True, {}), "M4")
        self.assertEqual(evidence.held_milestone("kiro", True, {evidence.UNHOLD_ENV: "droid"}), "M4")
        self.assertIsNone(evidence.held_milestone("kiro", True, {evidence.UNHOLD_ENV: " droid , kiro "}))
        self.assertIsNone(evidence.held_milestone("droid", False, {}))

    def test_t6_fails_queued_held_task_droid_untouched(self):
        """ACC: N19 - T6 fails the queued kiro launch task with launch_held (milestone M4, lead
        notified by task.failed); the queued droid task stays queued."""
        with mock.patch("mesh.daemon.scheduler.get_adapter", return_value=_installed_adapter(True)):
            failed = check_queued_tasks_t6(self.conn, testing_mode=False)
        self.assertEqual(failed, ["tsk-kiro"])
        self._assert_held_failed()
        self.assertEqual(self._task("tsk-droid")["status"], "queued")
        ob = self.conn.execute("SELECT type, payload_json FROM outbox WHERE recipient_id = 'agt-lead';").fetchall()
        self.assertEqual([r["type"] for r in ob], ["task.failed"])
        self.assertEqual(json.loads(ob[0]["payload_json"])["reason"], "launch_held")

    def test_t6_hold_checked_before_install(self):
        """ACC: N19 - the hold needs no binary: with the adapter not installed, kiro still fails
        launch_held (not adapter_unavailable), droid fails adapter_unavailable as before."""
        with mock.patch("mesh.daemon.scheduler.get_adapter", return_value=_installed_adapter(False)):
            failed = check_queued_tasks_t6(self.conn, testing_mode=False)
        self.assertEqual(sorted(failed), ["tsk-droid", "tsk-kiro"])
        self._assert_held_failed()
        self.assertEqual(json.loads(self._task("tsk-droid")["failure_json"])["reason"], "adapter_unavailable")

    def test_t6_unhold_env_only_in_testing_mode(self):
        """ACC: N19 - MESH_TEST_LAUNCH_UNHOLD=kiro lifts the hold in testing mode (task stays
        queued) and is ignored outside testing mode (task fails launch_held)."""
        with mock.patch.dict(os.environ, {evidence.UNHOLD_ENV: "kiro"}), \
                mock.patch("mesh.daemon.scheduler.get_adapter", return_value=_installed_adapter(True)):
            self.assertEqual(check_queued_tasks_t6(self.conn, testing_mode=True), [])
            self.assertEqual(self._task("tsk-kiro")["status"], "queued")
            self.assertEqual(check_queued_tasks_t6(self.conn, testing_mode=False), ["tsk-kiro"])
        self._assert_held_failed()

    def test_t5_start_fails_held_task_before_spawn(self):
        """ACC: N19 - T5 alone (no T6 pass first): start_queued_launch_tasks fails the held task
        before spawn_launch_runs runs, so the spawner never sees a queued kiro task."""
        seen = {}

        def fake_spawn(conn, home, testing_mode=False):
            seen["statuses"] = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM tasks;")}
            return []

        with mock.patch("mesh.daemon.supervisor.spawn_launch_runs", side_effect=fake_spawn) as spawn:
            self.assertEqual(start_queued_launch_tasks(self.conn, home=self.temp_dir.name, testing_mode=False), [])
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(seen["statuses"], {"tsk-kiro": "failed", "tsk-droid": "queued"})
        self._assert_held_failed()

    def test_t5_hold_check_error_starts_nothing(self):
        """ACC: N19 - if the hold check fails, T5 starts no launch run on this tick (fail closed)."""
        with mock.patch.object(scheduler, "fail_held_launch_tasks", side_effect=RuntimeError("injected")), \
                mock.patch("mesh.daemon.supervisor.spawn_launch_runs") as spawn:
            self.assertEqual(start_queued_launch_tasks(self.conn, home=self.temp_dir.name, testing_mode=False), [])
        spawn.assert_not_called()
        self.assertEqual(self._task("tsk-kiro")["status"], "queued")

    def test_t5_unheld_in_testing_mode_reaches_spawn(self):
        """ACC: N19 - with the hold lifted in testing mode, T5 hands the queued kiro task to the
        spawner unchanged (the existing kiro launch tests keep working)."""
        with mock.patch.dict(os.environ, {evidence.UNHOLD_ENV: "kiro"}), \
                mock.patch("mesh.daemon.supervisor.spawn_launch_runs", return_value=[]) as spawn:
            start_queued_launch_tasks(self.conn, home=self.temp_dir.name, testing_mode=True)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(self._task("tsk-kiro")["status"], "queued")


class TestAdaptersDocAgentCreate(unittest.TestCase):
    def test_agent_create_does_not_claim_rc13_for_evidence(self):
        """ACC: N19, N27 (P3-1) - docs/adapters.md lists the assignment paths that return rc 13
        and says agent create does not evaluate launch evidence; the queued-before-hold rule is
        documented."""
        with open(os.path.join(ROOT, "docs", "adapters.md"), encoding="utf-8") as fh:
            doc = " ".join(fh.read().split())
        self.assertNotIn("`agent create`/`task assign`/`reassign`/`retry` returns rc 13", doc)
        self.assertIn("`agent create` does not evaluate launch evidence", doc)
        self.assertIn("`task create --assignee`, `task assign`, `task reassign`, `task retry`", doc)
        self.assertIn("fails it with reason `launch_held`", doc)


if __name__ == "__main__":
    unittest.main()
