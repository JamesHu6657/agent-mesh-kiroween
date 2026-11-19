"""Integration regression tests for batch7 PR #43 round-3 review findings."""

import json
import os
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness
from tests.integration.test_tasks import create_agent


def _wait_task_status(test: unittest.TestCase, h: MeshHarness, task_id: str, status: str) -> dict:
    test.assertTrue(
        h.wait_for(
            lambda: h.cli("task", "show", task_id, user=True)[1]["data"]["status"] == status,
            timeout=25.0,
            step=0.1,
        ),
        f"task {task_id} did not reach status {status}",
    )
    rc, resp = h.cli("task", "show", task_id, user=True)
    test.assertEqual(rc, 0, resp)
    return resp["data"]


class TestBatch7PR43R3Integration(unittest.TestCase):
    """CLI + daemon integration tests for round-3 P2-1 and P2-2."""

    def test_p2_1_inbox_agent_create_update_default_model_and_clear(self) -> None:
        """P2-1: inbox agent create/update rejects non-empty default_model on [] allowlist and allows null/'' clear."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            rc_p, resp_p = h.cli(
                "intake", "submit", "--title", "P2-1 Inbox Models", "--goal", "G", "--criterion", "c1", user=True
            )
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            # 1. Creating inbox agent with empty allowed_models and --default-model fails DEFAULT_MODEL_NOT_ALLOWED.
            rc_bad_cr, resp_bad_cr = h.cli(
                "agent", "create",
                "--name", "ibx_bad_cr",
                "--adapter", "fake",
                "--delivery", "inbox",
                "--default-model", "m1",
                user=True,
            )
            self.assertEqual(rc_bad_cr, c.EXIT_USAGE, resp_bad_cr)
            self.assertEqual(resp_bad_cr["error"]["code"], c.DEFAULT_MODEL_NOT_ALLOWED)

            # 2. Repro B: inbox agent with empty allowed_models cannot later set --default-model via agent update.
            tok_dm2 = create_agent(
                h, "ibx_dm2", adapter="fake", delivery="inbox", allowed_models=[], default_model=None
            )
            rc_t_b, resp_t_b = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Task B",
                "--spec", "S",
                "--assignee", "ibx_dm2",
                token=tok_lead,
            )
            self.assertEqual(rc_t_b, 0, resp_t_b)
            tsk_b = resp_t_b["data"]["id"]
            _wait_task_status(self, h, tsk_b, "queued")

            rc_upd_b, resp_upd_b = h.cli(
                "agent", "update", "ibx_dm2", "--default-model", "m1", user=True
            )
            self.assertEqual(rc_upd_b, c.EXIT_USAGE, resp_upd_b)
            self.assertEqual(resp_upd_b["error"]["code"], c.DEFAULT_MODEL_NOT_ALLOWED)

            rc_sh_b, resp_sh_b = h.cli("agent", "show", "ibx_dm2", user=True)
            self.assertEqual(rc_sh_b, 0, resp_sh_b)
            self.assertEqual(resp_sh_b["data"]["allowed_models"], [])
            self.assertIsNone(resp_sh_b["data"]["default_model"])

            # Existing queued task can start cleanly with model=None.
            rc_st_b, resp_st_b = h.cli("task", "start", tsk_b, token=tok_dm2)
            self.assertEqual(rc_st_b, 0, resp_st_b)

            # 3. Repro A: agent update clearing allowed_models must fail unless default_model is also cleared.
            tok_m = create_agent(
                h, "ibx_m", adapter="fake", delivery="inbox", allowed_models=["m1"], default_model="m1"
            )
            rc_t_a, resp_t_a = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Task A",
                "--spec", "S",
                "--assignee", "ibx_m",
                token=tok_lead,
            )
            self.assertEqual(rc_t_a, 0, resp_t_a)
            tsk_a = resp_t_a["data"]["id"]
            self.assertEqual(resp_t_a["data"]["model"], "m1")
            _wait_task_status(self, h, tsk_a, "queued")

            # Unrelated update preserves default_model="m1".
            rc_keep, resp_keep = h.cli("agent", "update", "ibx_m", "--max-concurrency", "2", user=True)
            self.assertEqual(rc_keep, 0, resp_keep)
            self.assertEqual(resp_keep["data"]["default_model"], "m1")

            # Clearing allowed_models alone -> DEFAULT_MODEL_NOT_ALLOWED.
            rc_clr_only, resp_clr_only = h.cli(
                "agent", "update", "ibx_m", "--input", "-", stdin=json.dumps({"allowed_models": []}), user=True
            )
            self.assertEqual(rc_clr_only, c.EXIT_USAGE, resp_clr_only)
            self.assertEqual(resp_clr_only["error"]["code"], c.DEFAULT_MODEL_NOT_ALLOWED)

            # Clearing both allowed_models: [] and default_model: null succeeds!
            rc_clr_both, resp_clr_both = h.cli(
                "agent", "update", "ibx_m", "--input", "-",
                stdin=json.dumps({"allowed_models": [], "default_model": None}),
                user=True,
            )
            self.assertEqual(rc_clr_both, 0, resp_clr_both)
            self.assertEqual(resp_clr_both["data"]["allowed_models"], [])
            self.assertIsNone(resp_clr_both["data"]["default_model"])

            # Agent show confirms DB state; prior task with captured model='m1' fails T6 model_not_allowed.
            rc_sh_a, resp_sh_a = h.cli("agent", "show", "ibx_m", user=True)
            self.assertEqual(rc_sh_a, 0, resp_sh_a)
            self.assertEqual(resp_sh_a["data"]["allowed_models"], [])
            self.assertIsNone(resp_sh_a["data"]["default_model"])

            failed_a = _wait_task_status(self, h, tsk_a, "failed")
            self.assertEqual(failed_a["failure"]["reason"], "model_not_allowed")

            # New tasks created after clearing default_model resolve model=None and start cleanly.
            rc_t_a2, resp_t_a2 = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Task A After Clear",
                "--spec", "S",
                "--assignee", "ibx_m",
                token=tok_lead,
            )
            self.assertEqual(rc_t_a2, 0, resp_t_a2)
            self.assertIsNone(resp_t_a2["data"]["model"])
            tsk_a2 = resp_t_a2["data"]["id"]
            _wait_task_status(self, h, tsk_a2, "queued")

            rc_st_a2, resp_st_a2 = h.cli("task", "start", tsk_a2, token=tok_m)
            self.assertEqual(rc_st_a2, 0, resp_st_a2)

            # Empty string "" via JSON input or CLI flag also clears default_model cleanly.
            self.assertEqual(
                h.cli("agent", "update", "ibx_m", "--allowed-model", "m1", "--default-model", "m1", user=True)[0],
                0,
            )
            rc_clr_es1, resp_clr_es1 = h.cli(
                "agent", "update", "ibx_m", "--input", "-",
                stdin=json.dumps({"allowed_models": [], "default_model": ""}),
                user=True,
            )
            self.assertEqual(rc_clr_es1, 0, resp_clr_es1)
            self.assertIsNone(resp_clr_es1["data"]["default_model"])
            self.assertEqual(resp_clr_es1["data"]["allowed_models"], [])

            self.assertEqual(
                h.cli("agent", "update", "ibx_m", "--allowed-model", "m1", "--default-model", "m1", user=True)[0],
                0,
            )
            rc_clr_es2, resp_clr_es2 = h.cli("agent", "update", "ibx_m", "--default-model", "", user=True)
            self.assertEqual(rc_clr_es2, 0, resp_clr_es2)
            self.assertIsNone(resp_clr_es2["data"]["default_model"])
            self.assertEqual(resp_clr_es2["data"]["allowed_models"], ["m1"])
        finally:
            h.teardown()

    def test_p2_2_agy_launch_t6_fails_model_encodes_effort_without_consuming_attempt(self) -> None:
        """P2-2: queued AGY launch task fails at T6 (effort_not_allowed/model_encodes_effort) with 0 runs and attempt=0."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            self.assertEqual(h.cli("config", "set", "adapter.evidence_mode", '"warn"', user=True)[0], 0)
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)

            rc_ag, resp_ag = h.cli(
                "agent", "create",
                "--name", "agy_ln",
                "--adapter", "agy",
                "--delivery", "launch",
                "--allowed-model", "gemini-3.8-flash",
                "--allowed-model", "gemini-3.8-flash-high",
                "--default-model", "gemini-3.8-flash",
                "--allowed-effort", "high",
                "--default-profile", "readonly",
                "--max-unapproved-profile", "readonly",
                "--max-concurrency", "1",
                user=True,
            )
            self.assertEqual(rc_ag, 0, resp_ag)

            rc_p, resp_p = h.cli(
                "intake", "submit", "--title", "AGY T6", "--goal", "G", "--criterion", "c1", user=True
            )
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            # Hold launch starts while queued task with suffixed model waits in queued state.
            h.hold_launch_starts()
            rc_t, resp_t = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "AGY launch conflict",
                "--spec", "S",
                "--assignee", "agy_ln",
                "--model", "gemini-3.8-flash-high",
                "--profile", "readonly",
                token=tok_lead,
            )
            self.assertEqual(rc_t, 0, resp_t)
            tsk_id = resp_t["data"]["id"]
            _wait_task_status(self, h, tsk_id, "queued")

            # Add default_effort='high' (legal because agent default_model='gemini-3.8-flash' is unsuffixed).
            rc_upd, resp_upd = h.cli("agent", "update", "agy_ln", "--default-effort", "high", user=True)
            self.assertEqual(rc_upd, 0, resp_upd)

            # Release launch hold: T6 must fail it before supervisor spawns a run.
            h.release_launch_starts()
            task_doc = _wait_task_status(self, h, tsk_id, "failed")
            self.assertEqual(task_doc["attempt"], 0)
            self.assertEqual(task_doc["failure"]["reason"], "effort_not_allowed")
            self.assertEqual(
                task_doc["failure"]["details"],
                {
                    "reason": "model_encodes_effort",
                    "model": "gemini-3.8-flash-high",
                    "effort": "high",
                },
            )

            conn = h.db()
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?;", (tsk_id,)).fetchone()[0],
                    0,
                )
            finally:
                conn.close()
            self.assertFalse(os.path.exists(h.tripwire_file))
        finally:
            h.teardown()

    def test_p2_2_agy_inbox_task_start_conflict_and_persists_resolved_effort(self) -> None:
        """P2-2: AGY inbox task start rejects model_encodes_effort and persists resolved default_effort to runs.effort."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            self.assertEqual(h.cli("config", "set", "adapter.evidence_mode", '"warn"', user=True)[0], 0)
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)

            rc_ag, resp_ag = h.cli(
                "agent", "create",
                "--name", "agy_ibx",
                "--adapter", "agy",
                "--delivery", "inbox",
                "--allowed-model", "gemini-3.8-flash",
                "--allowed-model", "gemini-3.8-flash-high",
                "--default-model", "gemini-3.8-flash",
                "--allowed-effort", "high",
                "--default-profile", "readonly",
                "--max-concurrency", "2",
                user=True,
            )
            self.assertEqual(rc_ag, 0, resp_ag)
            rc_tok, resp_tok = h.cli("agent", "token", "issue", "agy_ibx", user=True)
            self.assertEqual(rc_tok, 0, resp_tok)
            tok_agy = resp_tok["data"]["token"]

            rc_p, resp_p = h.cli(
                "intake", "submit", "--title", "AGY Inbox", "--goal", "G", "--criterion", "c1", user=True
            )
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            # 1. Queue suffixed-model task (conflict once default_effort='high' is set).
            rc_t1, resp_t1 = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "AGY inbox conflict",
                "--spec", "S",
                "--assignee", "agy_ibx",
                "--model", "gemini-3.8-flash-high",
                token=tok_lead,
            )
            self.assertEqual(rc_t1, 0, resp_t1)
            tsk_bad = resp_t1["data"]["id"]
            _wait_task_status(self, h, tsk_bad, "queued")

            # 2. Queue unsuffixed task without effort override (inherits defaults).
            rc_t2, resp_t2 = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "AGY inbox valid default effort",
                "--spec", "S",
                "--assignee", "agy_ibx",
                token=tok_lead,
            )
            self.assertEqual(rc_t2, 0, resp_t2)
            tsk_ok = resp_t2["data"]["id"]
            _wait_task_status(self, h, tsk_ok, "queued")

            # Add default_effort='high' -> legal for unsuffixed default_model='gemini-3.8-flash'.
            rc_upd, resp_upd = h.cli("agent", "update", "agy_ibx", "--default-effort", "high", user=True)
            self.assertEqual(rc_upd, 0, resp_upd)

            # Task start on tsk_bad must fail with EFFORT_NOT_ALLOWED / model_encodes_effort and create 0 runs.
            rc_st_bad, resp_st_bad = h.cli("task", "start", tsk_bad, token=tok_agy)
            self.assertEqual(rc_st_bad, c.EXIT_USAGE, resp_st_bad)
            self.assertEqual(resp_st_bad["error"]["code"], c.EFFORT_NOT_ALLOWED)
            self.assertEqual(
                resp_st_bad["error"]["details"],
                {"reason": "model_encodes_effort", "model": "gemini-3.8-flash-high", "effort": "high"},
            )
            self.assertEqual(h.cli("task", "show", tsk_bad, user=True)[1]["data"]["status"], "queued")

            # Task start on tsk_ok succeeds and persists resolved model & default_effort in runs table + run show.
            rc_st_ok, resp_st_ok = h.cli("task", "start", tsk_ok, token=tok_agy)
            self.assertEqual(rc_st_ok, 0, resp_st_ok)
            run_id = resp_st_ok["data"]["run_id"]

            conn = h.db()
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?;", (tsk_bad,)).fetchone()[0],
                    0,
                )
                row = conn.execute("SELECT model, effort FROM runs WHERE id = ?;", (run_id,)).fetchone()
                self.assertEqual(row["model"], "gemini-3.8-flash")
                self.assertEqual(row["effort"], "high")
            finally:
                conn.close()

            rc_rs, resp_rs = h.cli("run", "show", run_id, user=True)
            self.assertEqual(rc_rs, 0, resp_rs)
            self.assertEqual(resp_rs["data"]["model"], "gemini-3.8-flash")
            self.assertEqual(resp_rs["data"]["effort"], "high")
            self.assertFalse(os.path.exists(h.tripwire_file))
        finally:
            h.teardown()


if __name__ == "__main__":
    unittest.main()
