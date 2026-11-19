"""Unit/DB/daemon regression tests for batch7 PR #43 round-3 review findings."""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest import mock

import mesh.constants as c
from mesh.cli.commands import agent as cli_agent
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core import policy
from mesh.daemon import scheduler
from mesh.daemon.handlers.agent import handle_agent_create, handle_agent_update
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.task import handle_task_start
from mesh.errors import MeshError


def _make_ctx(
    conn: sqlite3.Connection,
    home: str,
    cmd: str,
    args: Dict[str, Any],
    *,
    principal: Optional[Dict[str, Any]] = None,
    actor_kind: str = "user",
    via: str = "direct",
    request_id: str = "req_r3",
) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal=principal or {"id": "usr_1", "kind": "user", "name": "user"},
        actor_kind=actor_kind,
        via=via,
        request_id=request_id,
        home=home,
        cmd=cmd,
        args=args,
        testing_mode=True,
    )


class TestBatch7PR43R3Unit(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-pr43r3-u-")
        for d in ("db", "run", "logs", "workspaces", "runs"):
            os.makedirs(os.path.join(self.home, d), exist_ok=True)
        self.db_path = os.path.join(self.home, "db", "mesh.db")
        self.conn = init_db(self.db_path)
        self.now = now_iso_utc()

        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
                (self.now,),
            )
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);",
                (self.now,),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, default_profile, max_unapproved_profile, "
                "max_concurrency, created_at, updated_at) "
                "VALUES ('agt_lead', 'none', 'inbox', 1, 'edit', 'dev', 1, ?, ?);",
                (self.now, self.now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P', 'G', 'active', 'usr_1', 'direct', ?, ?);",
                (self.now, self.now),
            )
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES ('la_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
                (self.now,),
            )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.home, ignore_errors=True)

    # ------------------------------------------------------------------
    # P2-1: inbox agent create/update allowed_models == [] and clear default_model
    # ------------------------------------------------------------------

    def test_p2_1_inbox_agent_create_rejects_default_model_when_allowed_models_empty(self) -> None:
        """P2-1: inbox agent create with empty allowed_models rejects non-empty default_model."""
        ctx_bad = _make_ctx(
            self.conn,
            self.home,
            "agent.create",
            {
                "name": "ibx_bad",
                "adapter": "fake",
                "delivery": "inbox",
                "allowed_models": [],
                "default_model": "m1",
                "max_concurrency": 1,
            },
        )
        with self.assertRaises(MeshError) as err_bad:
            handle_agent_create(ctx_bad)
        self.assertEqual(err_bad.exception.code, c.DEFAULT_MODEL_NOT_ALLOWED)

        # None default_model with empty allowed_models succeeds cleanly.
        ctx_ok = _make_ctx(
            self.conn,
            self.home,
            "agent.create",
            {
                "name": "ibx_empty_ok",
                "adapter": "fake",
                "delivery": "inbox",
                "allowed_models": [],
                "default_model": None,
                "max_concurrency": 1,
            },
        )
        with transaction(self.conn):
            res_ok = handle_agent_create(ctx_ok)
        self.assertIsNone(res_ok.data["default_model"])
        self.assertEqual(res_ok.data["allowed_models"], [])

        # Non-string JSON default_model must raise MeshError INVALID_ARGUMENT, not TypeError.
        ctx_bad_type = _make_ctx(
            self.conn,
            self.home,
            "agent.create",
            {
                "name": "ibx_bad_type",
                "adapter": "fake",
                "delivery": "inbox",
                "allowed_models": ["m1"],
                "default_model": 123,
                "max_concurrency": 1,
            },
        )
        with self.assertRaises(MeshError) as err_type:
            handle_agent_create(ctx_bad_type)
        self.assertEqual(err_type.exception.code, c.INVALID_ARGUMENT)

    def test_p2_1_inbox_agent_update_empty_allowed_models_and_clear_default_model(self) -> None:
        """P2-1: inbox agent update mirrors allowed_efforts/default_effort rules and launch keeps requiring allowed."""
        with transaction(self.conn):
            handle_agent_create(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.create",
                    {
                        "name": "ibx_m",
                        "adapter": "fake",
                        "delivery": "inbox",
                        "allowed_models": [],
                        "default_model": None,
                        "max_concurrency": 1,
                    },
                )
            )
            handle_agent_create(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.create",
                    {
                        "name": "ln_m",
                        "adapter": "fake",
                        "delivery": "launch",
                        "allowed_models": ["m1"],
                        "default_model": "m1",
                        "max_concurrency": 1,
                    },
                )
            )

        # Non-string JSON default_model must raise MeshError INVALID_ARGUMENT, not TypeError.
        with self.assertRaises(MeshError) as err_upd_type:
            handle_agent_update(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.update",
                    {"name": "ibx_m", "default_model": 123},
                )
            )
        self.assertEqual(err_upd_type.exception.code, c.INVALID_ARGUMENT)

        # Setting default_model on inbox agent with empty allowed_models fails DEFAULT_MODEL_NOT_ALLOWED.
        with self.assertRaises(MeshError) as err_set:
            handle_agent_update(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.update",
                    {"name": "ibx_m", "default_model": "m1"},
                )
            )
        self.assertEqual(err_set.exception.code, c.DEFAULT_MODEL_NOT_ALLOWED)

        # Configure allowed_models + default_model, then clear allowed_models alone -> rejected.
        with transaction(self.conn):
            handle_agent_update(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.update",
                    {"name": "ibx_m", "allowed_models": ["m1", "m2"], "default_model": "m1"},
                )
            )
        with self.assertRaises(MeshError) as err_empty_alone:
            handle_agent_update(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.update",
                    {"name": "ibx_m", "allowed_models": []},
                )
            )
        self.assertEqual(err_empty_alone.exception.code, c.DEFAULT_MODEL_NOT_ALLOWED)

        # Explicit None clears default_model cleanly alongside allowed_models=[].
        with transaction(self.conn):
            res_clr_null = handle_agent_update(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.update",
                    {"name": "ibx_m", "allowed_models": [], "default_model": None},
                )
            )
        self.assertIsNone(res_clr_null.data["default_model"])
        self.assertEqual(res_clr_null.data["allowed_models"], [])

        # Re-populate, then clear via empty string "" and also clear only default_model.
        with transaction(self.conn):
            handle_agent_update(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.update",
                    {"name": "ibx_m", "allowed_models": ["m1"], "default_model": "m1"},
                )
            )
        with transaction(self.conn):
            res_clr_empty_str = handle_agent_update(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.update",
                    {"name": "ibx_m", "default_model": ""},
                )
            )
        self.assertIsNone(res_clr_empty_str.data["default_model"])
        self.assertEqual(res_clr_empty_str.data["allowed_models"], ["m1"])

        # Re-set default_model and confirm unrelated update preserves it.
        with transaction(self.conn):
            handle_agent_update(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.update",
                    {"name": "ibx_m", "default_model": "m1"},
                )
            )
        with transaction(self.conn):
            res_keep = handle_agent_update(
                _make_ctx(
                    self.conn,
                    self.home,
                    "agent.update",
                    {"name": "ibx_m", "max_concurrency": 3},
                )
            )
        self.assertEqual(res_keep.data["default_model"], "m1")

        # Launch agent still rejects clearing default_model to None or "".
        for cleared in (None, ""):
            with self.assertRaises(MeshError) as err_ln:
                handle_agent_update(
                    _make_ctx(
                        self.conn,
                        self.home,
                        "agent.update",
                        {"name": "ln_m", "default_model": cleared},
                    )
                )
            self.assertEqual(err_ln.exception.code, c.DEFAULT_MODEL_NOT_ALLOWED)

    def test_p2_1_cli_agent_update_payload_omits_unset_default_model_keeps_json_null(self) -> None:
        """P2-1: CLI handle_agent_update omits default_model unless provided on CLI or in _from_json."""
        sent_payloads = []

        class _FakeClient:
            def __init__(self, home: str) -> None:
                pass

            def execute_cmd(self, args: Any, cmd: str, payload: Dict[str, Any]) -> Dict[str, Any]:
                sent_payloads.append(dict(payload))
                return {"data": {}}

        def _base_ns(**overrides: Any) -> SimpleNamespace:
            data = {
                "home": self.home,
                "name": "ag1",
                "adapter": None,
                "delivery": None,
                "default_model": None,
                "allowed_models": None,
                "allowed_model": None,
                "default_effort": None,
                "allowed_efforts": None,
                "allowed_effort": None,
                "max_concurrency": None,
                "can_lead": None,
                "capabilities": None,
                "capability": None,
                "default_profile": None,
                "max_unapproved_profile": None,
                "binary": None,
                "options": None,
                "option": None,
                "strict_models": None,
                "if_version": None,
                "no_intake": False,
                "ack_timeout": None,
                "_from_json": set(),
            }
            data.update(overrides)
            return SimpleNamespace(**data)

        with mock.patch.object(cli_agent, "MeshClient", _FakeClient):
            cli_agent.handle_agent_update(_base_ns(max_concurrency="2"))
            cli_agent.handle_agent_update(_base_ns(_from_json={"default_model"}))
            cli_agent.handle_agent_update(_base_ns(default_model=""))

        self.assertNotIn("default_model", sent_payloads[0])
        self.assertIn("default_model", sent_payloads[1])
        self.assertIsNone(sent_payloads[1]["default_model"])
        self.assertEqual(sent_payloads[2]["default_model"], "")

    # ------------------------------------------------------------------
    # P2-2: T6 + task start AGY model_encodes_effort + runs.effort persistence
    # ------------------------------------------------------------------

    def _insert_agent_and_task(
        self,
        *,
        agent_id: str,
        agent_name: str,
        adapter: str,
        delivery: str,
        allowed_models: list[str],
        default_model: Optional[str],
        allowed_efforts: list[str],
        default_effort: Optional[str],
        task_id: str,
        task_model: Optional[str],
        task_effort: Optional[str],
    ) -> None:
        with transaction(self.conn):
            if not self.conn.execute("SELECT 1 FROM principals WHERE id = ?;", (agent_id,)).fetchone():
                self.conn.execute(
                    "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, ?);",
                    (agent_id, agent_name, self.now),
                )
                self.conn.execute(
                    """INSERT INTO agents (
                           id, adapter, delivery, can_lead, default_profile, max_unapproved_profile,
                           default_model, allowed_models_json, default_effort, allowed_efforts_json,
                           max_concurrency, created_at, updated_at
                       ) VALUES (?, ?, ?, 0, 'edit', 'edit', ?, ?, ?, ?, 1, ?, ?);""",
                    (
                        agent_id,
                        adapter,
                        delivery,
                        default_model,
                        json.dumps(allowed_models),
                        default_effort,
                        json.dumps(allowed_efforts),
                        self.now,
                        self.now,
                    ),
                )
            else:
                self.conn.execute(
                    """UPDATE agents
                       SET default_model = ?, allowed_models_json = ?, default_effort = ?, allowed_efforts_json = ?
                       WHERE id = ?;""",
                    (
                        default_model,
                        json.dumps(allowed_models),
                        default_effort,
                        json.dumps(allowed_efforts),
                        agent_id,
                    ),
                )
            self.conn.execute(
                """INSERT INTO tasks (
                       id, project_id, title, spec, kind, status, assignee_agent_id,
                       model, effort, profile, timeout_s, max_attempts, attempt, fence_seq,
                       queued_at, offered_at, created_by, created_at, updated_at
                   ) VALUES (?, 'prj_1', 'T', 'S', 'general', 'queued', ?, ?, ?, 'edit', 60, 3, 0, 0,
                             ?, ?, 'agt_lead', ?, ?);""",
                (
                    task_id,
                    agent_id,
                    task_model,
                    task_effort,
                    self.now,
                    self.now if delivery == "inbox" else None,
                    self.now,
                    self.now,
                ),
            )

    def test_p2_2_t6_checks_agy_model_encodes_effort_for_launch_tasks(self) -> None:
        """P2-2: scheduler T6 rejects suffixed AGY model + resolved effort with effort_not_allowed before spawning."""
        all_models = [
            "gemini-3.8-flash",
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-xhigh",
            "gemini-3.8-flash-max",
        ]
        all_efforts = ["low", "medium", "high", "xhigh", "max"]

        # 1. Task specifies suffixed model (effort=None), agent has default_effort='high'.
        for idx, suffix in enumerate(("low", "medium", "high", "xhigh", "max")):
            tid = f"tsk_agy_suf_{idx}"
            suffixed_model = f"gemini-3.8-flash-{suffix}"
            self._insert_agent_and_task(
                agent_id="agt_agy_ln",
                agent_name="agy_ln",
                adapter="agy",
                delivery="launch",
                allowed_models=all_models,
                default_model="gemini-3.8-flash",
                allowed_efforts=all_efforts,
                default_effort="high",
                task_id=tid,
                task_model=suffixed_model,
                task_effort=None,
            )

        # 2. Task specifies model=None + effort='high', while agent default_model carries suffix.
        self._insert_agent_and_task(
            agent_id="agt_agy_ln_dm",
            agent_name="agy_ln_dm",
            adapter="agy",
            delivery="launch",
            allowed_models=all_models,
            default_model="gemini-3.8-flash-high",
            allowed_efforts=all_efforts,
            default_effort=None,
            task_id="tsk_agy_defm",
            task_model=None,
            task_effort="high",
        )

        # 3. Control: unsuffixed AGY task with default_effort='high' stays queued in T6.
        self._insert_agent_and_task(
            agent_id="agt_agy_ln_ok",
            agent_name="agy_ln_ok",
            adapter="agy",
            delivery="launch",
            allowed_models=all_models,
            default_model="gemini-3.8-flash",
            allowed_efforts=all_efforts,
            default_effort="high",
            task_id="tsk_agy_ok",
            task_model=None,
            task_effort=None,
        )

        # 4. Control: inbox AGY task stays queued in T6 (handled synchronously by task.start).
        self._insert_agent_and_task(
            agent_id="agt_agy_ibx",
            agent_name="agy_ibx",
            adapter="agy",
            delivery="inbox",
            allowed_models=all_models,
            default_model="gemini-3.8-flash",
            allowed_efforts=all_efforts,
            default_effort="high",
            task_id="tsk_agy_ibx",
            task_model="gemini-3.8-flash-high",
            task_effort=None,
        )

        fake_agy = mock.MagicMock()
        fake_agy.kind = "vendor"
        fake_agy.capabilities = {}
        fake_agy.is_installed.return_value = True

        with mock.patch("mesh.daemon.scheduler.get_adapter", return_value=fake_agy):
            failed = scheduler.check_queued_tasks_t6(self.conn, home=self.home, testing_mode=True)

        expected_failed = {f"tsk_agy_suf_{i}" for i in range(5)} | {"tsk_agy_defm"}
        self.assertEqual(set(failed), expected_failed)

        for idx, suffix in enumerate(("low", "medium", "high", "xhigh", "max")):
            tid = f"tsk_agy_suf_{idx}"
            row = self.conn.execute("SELECT status, attempt, failure_json FROM tasks WHERE id = ?;", (tid,)).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempt"], 0)
            self.assertEqual(
                json.loads(row["failure_json"]),
                {
                    "reason": "effort_not_allowed",
                    "details": {
                        "reason": "model_encodes_effort",
                        "model": f"gemini-3.8-flash-{suffix}",
                        "effort": "high",
                    },
                },
            )
            self.assertEqual(
                self.conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?;", (tid,)).fetchone()[0],
                0,
            )

        defm_row = self.conn.execute("SELECT status, failure_json FROM tasks WHERE id = 'tsk_agy_defm';").fetchone()
        self.assertEqual(defm_row["status"], "failed")
        self.assertEqual(json.loads(defm_row["failure_json"])["details"]["reason"], "model_encodes_effort")

        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_agy_ok';").fetchone()["status"], "queued")
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id = 'tsk_agy_ibx';").fetchone()["status"], "queued")

    def test_p2_2_handle_task_start_rejects_agy_conflict_and_persists_resolved_effort(self) -> None:
        """P2-2: handle_task_start enforces AGY model_encodes_effort and writes resolved task_effort to runs.effort."""
        # Case A: suffixed task.model + agent.default_effort -> EFFORT_NOT_ALLOWED (model_encodes_effort)
        self._insert_agent_and_task(
            agent_id="agt_agy_ibx",
            agent_name="agy_ibx",
            adapter="agy",
            delivery="inbox",
            allowed_models=["gemini-3.8-flash", "gemini-3.8-flash-high"],
            default_model="gemini-3.8-flash",
            allowed_efforts=["low", "high"],
            default_effort="high",
            task_id="tsk_ibx_bad",
            task_model="gemini-3.8-flash-high",
            task_effort=None,
        )
        ctx_bad = _make_ctx(
            self.conn,
            self.home,
            "task.start",
            {"task_id": "tsk_ibx_bad"},
            principal={"id": "agt_agy_ibx", "kind": "agent", "name": "agy_ibx"},
            actor_kind="agent",
            via="token",
        )
        with self.assertRaises(MeshError) as err_bad:
            handle_task_start(ctx_bad)
        self.assertEqual(err_bad.exception.code, c.EFFORT_NOT_ALLOWED)
        self.assertEqual(
            err_bad.exception.details,
            {"reason": "model_encodes_effort", "model": "gemini-3.8-flash-high", "effort": "high"},
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = 'tsk_ibx_bad';").fetchone()[0],
            0,
        )

        # Case B: task.model=None + task.effort='high' + suffixed agent.default_model -> also EFFORT_NOT_ALLOWED
        self._insert_agent_and_task(
            agent_id="agt_agy_ibx_dm",
            agent_name="agy_ibx_dm",
            adapter="agy",
            delivery="inbox",
            allowed_models=["gemini-3.8-flash", "gemini-3.8-flash-high"],
            default_model="gemini-3.8-flash-high",
            allowed_efforts=["high"],
            default_effort=None,
            task_id="tsk_ibx_dm_bad",
            task_model=None,
            task_effort="high",
        )
        ctx_dm_bad = _make_ctx(
            self.conn,
            self.home,
            "task.start",
            {"task_id": "tsk_ibx_dm_bad"},
            principal={"id": "agt_agy_ibx_dm", "kind": "agent", "name": "agy_ibx_dm"},
            actor_kind="agent",
            via="token",
        )
        with self.assertRaises(MeshError) as err_dm_bad:
            handle_task_start(ctx_dm_bad)
        self.assertEqual(err_dm_bad.exception.code, c.EFFORT_NOT_ALLOWED)
        self.assertEqual(err_dm_bad.exception.details["reason"], "model_encodes_effort")

        # Case C: unsuffixed AGY default_model + agent default_effort -> succeeds and persists runs.effort!
        self._insert_agent_and_task(
            agent_id="agt_agy_ibx_ok",
            agent_name="agy_ibx_ok",
            adapter="agy",
            delivery="inbox",
            allowed_models=["gemini-3.8-flash", "gemini-3.8-flash-high"],
            default_model="gemini-3.8-flash",
            allowed_efforts=["low", "high"],
            default_effort="high",
            task_id="tsk_ibx_ok",
            task_model=None,
            task_effort=None,
        )
        ctx_ok = _make_ctx(
            self.conn,
            self.home,
            "task.start",
            {"task_id": "tsk_ibx_ok"},
            principal={"id": "agt_agy_ibx_ok", "kind": "agent", "name": "agy_ibx_ok"},
            actor_kind="agent",
            via="token",
        )
        with transaction(self.conn):
            res_ok = handle_task_start(ctx_ok)
        run_row = self.conn.execute(
            "SELECT model, effort FROM runs WHERE id = ?;",
            (res_ok.data["run_id"],),
        ).fetchone()
        self.assertEqual(run_row["model"], "gemini-3.8-flash")
        self.assertEqual(run_row["effort"], "high")
        self.assertEqual(
            policy.resolve_model_and_effort(model=None, default_model="gemini-3.8-flash", effort=None, default_effort="high"),
            ("gemini-3.8-flash", "high"),
        )


if __name__ == "__main__":
    unittest.main()
