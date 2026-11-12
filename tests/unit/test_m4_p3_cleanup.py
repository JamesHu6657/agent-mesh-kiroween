"""m4-p3-cleanup: the five P3 items James's M4 polish scope carried over from the M3 gate.

1. kiro/agy: a prompt that starts with "-" stays positional INPUT (Carbon r10 P3).
2. F7: the droid stub probes 1.6.0 (tested in tests/integration/test_adapters.py N1).
3. L4 guard + C-5: every vendor lead is refused with vendor_lead_m3_lite even with passing
   evidence; non-vendor leads go through evidence.effective_launch with the lead's binary.
4. fail_held_launch_tasks logs a failing task and goes on; the task that errored still
   never reaches a spawn (spawn_launch_runs skips held adapters).

ACC: N6, N19
"""

import os
import tempfile
import unittest
from unittest import mock

from mesh.adapters import evidence
from mesh.adapters.agy import AgyAdapter
from mesh.adapters.base import RunRequest, positional_prompt
from mesh.adapters.evidence import Evidence
from mesh.adapters.kiro import KiroAdapter
from mesh.adapters.registry import get_adapter
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.daemon import lead_turns, scheduler, supervisor
from mesh.errors import MeshError

VENDORS = ("droid", "agy", "kiro", "vibe", "hermes", "openclaw")


class TestPositionalPrompt(unittest.TestCase):
    def _last_arg(self, adapter, text: str, profile: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prompt.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            req = RunRequest(prompt_path=path, workspace="/ws/t", model="m", effort=None, profile=profile)
            return adapter.build(req).argv[-1]

    def test_helper(self):
        self.assertEqual(positional_prompt("-rf x"), "\n-rf x")
        self.assertEqual(positional_prompt("--help"), "\n--help")
        self.assertEqual(positional_prompt("# Agent Mesh task"), "# Agent Mesh task")
        self.assertEqual(positional_prompt(""), "")

    def test_kiro_and_agy_leading_dash_prompt_is_not_a_flag(self):
        """ACC: N6 - the last argv item never starts with "-"; normal prompts are unchanged."""
        for adapter in (KiroAdapter(), AgyAdapter()):
            with self.subTest(adapter=adapter.id):
                last = self._last_arg(adapter, "--trust-all-tools do it", "readonly")
                self.assertEqual(last, "\n--trust-all-tools do it")
                self.assertFalse(last.startswith("-"))
                self.assertEqual(self._last_arg(adapter, "# Agent Mesh task t", "readonly"),
                                 "# Agent Mesh task t")


class TestLeadGateGuard(unittest.TestCase):
    def test_every_vendor_lead_refused_even_with_passing_evidence(self):
        """ACC: N19 (L4 guard) - with effective_launch forced to pass, every vendor adapter is
        still refused as vendor_lead_m3_lite, in and out of testing mode."""
        ok = Evidence(ok=True)
        with mock.patch.object(evidence, "effective_launch", return_value=ok) as ev:
            for aid in VENDORS:
                for testing in (False, True):
                    with self.subTest(adapter=aid, testing=testing):
                        adapter = get_adapter(aid, testing_mode=testing)
                        self.assertEqual(adapter.kind, "vendor")
                        with self.assertRaises(MeshError) as cm:
                            lead_turns.lead_launch_check(None, adapter, testing, binary_path="/x")
                        self.assertEqual(cm.exception.details.get("reason"), "vendor_lead_m3_lite")
        ev.assert_not_called()

    def test_non_vendor_lead_goes_through_effective_launch(self):
        """ACC: N19 (C-5) - a non-vendor lead is gated by effective_launch with its binary_path;
        a refusal keeps the evidence reason."""
        adapter = get_adapter("none", testing_mode=False)
        with mock.patch.object(evidence, "effective_launch",
                               return_value=Evidence(ok=False, reason="no_evidence")) as ev:
            with self.assertRaises(MeshError) as cm:
                lead_turns.lead_launch_check(None, adapter, False, binary_path="/bin/x")
        ev.assert_called_once_with(None, adapter, False, "/bin/x")
        self.assertEqual(cm.exception.details.get("reason"), "no_evidence")
        self.assertEqual(cm.exception.details.get("milestone"), adapter.launch_milestone or "M3")
        # Unpatched: the class value decides for non-vendors, as before.
        self.assertEqual(lead_turns.lead_launch_allowed(None, adapter, False), bool(adapter.launch_enabled))


class TestHoldPassContinues(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in [k for k in os.environ if k.startswith("MESH_TEST_LAUNCH")]:
            del os.environ[key]
        self.holds = mock.patch.dict(evidence.LAUNCH_HOLDS, {"kiro": "M4"}, clear=True)
        self.holds.start()
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-m4p3-")
        self.conn = init_db(os.path.join(self.tmp.name, "mesh.db"))
        now = now_iso_utc()
        with transaction(self.conn):
            ex = self.conn.execute
            ex("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr-1', 'user', 'u', ?);", (now,))
            for aid, name, adapter, delivery, lead in (
                ("agt-lead", "lead1", "none", "inbox", 1),
                ("agt-kiro", "kx", "kiro", "launch", 0),
                ("agt-droid", "dx", "droid", "launch", 0),
            ):
                ex("INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, ?);", (aid, name, now))
                ex("""INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at)
                      VALUES (?, ?, ?, ?, 4, ?, ?);""", (aid, adapter, delivery, lead, now, now))
            ex("""INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at)
                  VALUES ('prj-1', 'P', 'G', 'active', 'usr-1', 'direct', ?, ?);""", (now, now))
            ex("""INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
                  VALUES ('la-1', 'prj-1', 'agt-lead', 'usr-1', 'direct', ?);""", (now,))
            for tid, agent in (("tsk-kiro-a", "agt-kiro"), ("tsk-kiro-b", "agt-kiro"), ("tsk-droid", "agt-droid")):
                ex("""INSERT INTO tasks (
                        id, project_id, title, spec, kind, status, priority,
                        assignee_agent_id, profile, timeout_s, max_attempts, attempt, fence_seq,
                        created_by, version, queued_at, created_at, updated_at
                      ) VALUES (?, 'prj-1', 't', 's', 'general', 'queued', 0,
                                ?, 'edit', 3600, 2, 0, 0, 'usr-1', 1, ?, ?, ?);""",
                   (tid, agent, now, now, now))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()
        self.holds.stop()
        self.env.stop()

    def _status(self, tid):
        return self.conn.execute("SELECT status FROM tasks WHERE id = ?;", (tid,)).fetchone()[0]

    def test_one_failing_task_does_not_stop_the_pass_or_reach_spawn(self):
        """ACC: N19 - an error failing tsk-kiro-a is logged and the pass goes on (tsk-kiro-b
        fails launch_held); T5 still runs, and the spawner never gets either kiro task."""
        real = scheduler.fail_queued_task_t6

        def flaky(conn, task, *a, **k):
            if task["id"] == "tsk-kiro-a":
                raise RuntimeError("injected")
            return real(conn, task, *a, **k)

        spawned = []

        def fake_spawn_one(conn, home, task, testing_mode):
            spawned.append(task["id"])
            return None

        with mock.patch.object(scheduler, "fail_queued_task_t6", side_effect=flaky), \
                mock.patch.object(scheduler, "_log_to_meshd") as log, \
                mock.patch.object(supervisor, "_spawn_one", side_effect=fake_spawn_one):
            self.assertEqual(scheduler.start_queued_launch_tasks(self.conn, home=self.tmp.name,
                                                                 testing_mode=False), [])
        self.assertEqual(self._status("tsk-kiro-a"), "queued")
        self.assertEqual(self._status("tsk-kiro-b"), "failed")
        self.assertEqual(spawned, ["tsk-droid"])
        self.assertTrue(any("tsk-kiro-a" in str(c) and "injected" in str(c) for c in log.call_args_list))

    def test_spawn_skips_held_adapter_without_the_hold_pass(self):
        """ACC: N19 - spawn_launch_runs alone never starts a held adapter's task."""
        spawned = []
        with mock.patch.object(supervisor, "_spawn_one",
                               side_effect=lambda c, h, t, tm: spawned.append(t["id"])):
            supervisor.spawn_launch_runs(self.conn, self.tmp.name, testing_mode=False)
        self.assertEqual(spawned, ["tsk-droid"])


if __name__ == "__main__":
    unittest.main()
