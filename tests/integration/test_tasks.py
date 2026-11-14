"""Integration tests for Task creation, lifecycle, deps, and validation per plan §16 M1.6.

ACC: B3, B4, B5, B6, D3, D7, D16, E8, E9, F5, N4, N5
"""

import concurrent.futures
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

import mesh.constants as c
from tests.helpers import MeshHarness, serial
from tests.stubs import create_test_stubs


def create_agent(
    harness: MeshHarness,
    name: str,
    adapter: str = "none",
    delivery: str = "inbox",
    can_lead: bool = False,
    capabilities: Optional[List[str]] = None,
    allowed_models: Optional[List[str]] = None,
    default_model: Optional[str] = None,
    allowed_efforts: Optional[List[str]] = None,
    default_effort: Optional[str] = None,
    default_profile: Optional[str] = None,
    max_unapproved_profile: Optional[str] = None,
    max_concurrency: int = 1,
) -> str:
    """Helper to register an agent and return its issued token."""
    if delivery == "launch":
        if allowed_models is None:
            allowed_models = ["m-a", "m-b"]
        if default_model is None:
            default_model = "m-a"

    args = [
        "agent", "create",
        "--name", name,
        "--adapter", adapter,
        "--delivery", delivery,
        "--max-concurrency", str(max_concurrency),
    ]
    if can_lead:
        args.append("--can-lead")
    if capabilities:
        for cap in capabilities:
            args.extend(["--capability", cap])
    if allowed_models:
        for m in allowed_models:
            args.extend(["--allowed-model", m])
    if default_model:
        args.extend(["--default-model", default_model])
    if allowed_efforts:
        for ef in allowed_efforts:
            args.extend(["--allowed-effort", ef])
    if default_effort:
        args.extend(["--default-effort", default_effort])
    if default_profile:
        args.extend(["--default-profile", default_profile])
    if max_unapproved_profile:
        args.extend(["--max-unapproved-profile", max_unapproved_profile])

    rc, resp = harness.cli(*args, user=True)
    assert rc == 0, f"agent create {name} failed: {resp}"

    rc_tok, tok_resp = harness.cli("agent", "token", "issue", name, user=True)
    assert rc_tok == 0, f"token issue {name} failed: {tok_resp}"
    token = tok_resp["data"]["token"]

    tok_dir = os.path.join(harness.home, "tokens")
    os.makedirs(tok_dir, mode=0o700, exist_ok=True)
    tok_path = os.path.join(tok_dir, f"{name}.tok")
    with open(tok_path, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(tok_path, 0o600)

    return token


class TestTasksIntegration(unittest.TestCase):
    """Integration test suite for tasks and dependencies."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

        # Standard test agents
        self.tok_lead1 = create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        self.tok_lead2 = create_agent(self.harness, "lead2", adapter="none", delivery="inbox", can_lead=True)
        self.tok_plain = create_agent(self.harness, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
        self.tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")

        # Set default lead
        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)

        # Create active test project
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "Main Project",
            "--goal", "Build the task system",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_p, 0)
        self.project_id = resp_p["data"]["id"]

        # M1c run 2 wires launch T5. These M1b tests assert on the queued state of
        # launch tasks, so they hold launch starts (testing hook, plan §9.3).
        self.harness.hold_launch_starts()

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def test_b3_input_stdin_json(self) -> None:
        """ACC: B3 - Create one task with flags and one with the same values via --input - (JSON on stdin)."""
        # 1. Create task with CLI flags
        rc_flag, resp_flag = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Flag task",
            "--spec", "Spec content",
            "--priority", "5",
            "--timeout", "120s",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_flag, 0)
        self.assertTrue(resp_flag["ok"])
        data_flag = resp_flag["data"]

        # 2. Create task with identical parameters via JSON stdin
        stdin_json = json.dumps({
            "project": self.project_id,
            "title": "Flag task",
            "spec": "Spec content",
            "priority": 5,
            "timeout": 120,
        })
        rc_stdin, resp_stdin = self.harness.cli(
            "task", "create",
            "--input", "-",
            stdin=stdin_json,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_stdin, 0)
        self.assertTrue(resp_stdin["ok"])
        data_stdin = resp_stdin["data"]

        # Compare fields except id and timestamps
        ignore_keys = {"id", "created_at", "updated_at", "workspace_path"}
        for k in data_flag:
            if k not in ignore_keys:
                self.assertEqual(data_flag[k], data_stdin[k], f"Mismatch for field '{k}'")

    def test_b4_duplicate_and_unknown_params(self) -> None:
        """ACC: B4 - --title y plus stdin {"title":"x"}; stdin {"titel":"x"} -> DUPLICATE_PARAM; UNKNOWN_PARAM."""
        # 1. Duplicate param: flag and stdin define title
        rc_dup, resp_dup = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--spec", "Spec",
            "--title", "y",
            "--input", "-",
            stdin=json.dumps({"title": "x"}),
            token=self.tok_lead1,
        )
        self.assertEqual(rc_dup, c.EXIT_USAGE)
        self.assertFalse(resp_dup["ok"])
        self.assertEqual(resp_dup["error"]["code"], c.DUPLICATE_PARAM)

        # 2. Unknown param in stdin JSON
        rc_unk, resp_unk = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--spec", "Spec",
            "--input", "-",
            stdin=json.dumps({"titel": "x"}),
            token=self.tok_lead1,
        )
        self.assertEqual(rc_unk, c.EXIT_USAGE)
        self.assertFalse(resp_unk["ok"])
        self.assertEqual(resp_unk["error"]["code"], c.UNKNOWN_PARAM)

    def test_b5_idempotency_and_reused_key(self) -> None:
        """ACC: B5 - m task create … --idempotency-key k1 [@lead1] twice, then key k1 with a different title."""
        # 1. First call with key k1
        rc1, resp1 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task B5",
            "--spec", "Spec B5",
            "--idempotency-key", "k1",
            token=self.tok_lead1,
        )
        self.assertEqual(rc1, 0)
        self.assertTrue(resp1["ok"])
        self.assertFalse(resp1.get("idempotent_replay", False))
        task_id = resp1["data"]["id"]

        # 2. Replay with identical params and same key k1
        rc2, resp2 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task B5",
            "--spec", "Spec B5",
            "--idempotency-key", "k1",
            token=self.tok_lead1,
        )
        self.assertEqual(rc2, 0)
        self.assertTrue(resp2["ok"])
        self.assertTrue(resp2.get("idempotent_replay", False))
        self.assertEqual(resp2["data"]["id"], task_id)
        # "same data": the replay returns the stored response's data unchanged.
        self.assertEqual(resp2["data"], resp1["data"])

        # Verify only one task row in database
        conn = self.harness.db()
        cnt = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE project_id = ?;", (self.project_id,)).fetchone()["c"]
        conn.close()
        self.assertEqual(cnt, 1)

        # 3. Third call: key k1 reused with different title
        rc3, resp3 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Different title",
            "--spec", "Spec B5",
            "--idempotency-key", "k1",
            token=self.tok_lead1,
        )
        self.assertEqual(rc3, c.EXIT_CONFLICT)
        self.assertFalse(resp3["ok"])
        self.assertEqual(resp3["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)

    def test_b6_dry_run_counts_unchanged(self) -> None:
        """ACC: B6 - Count rows of events, outbox, runs, idempotency before and after task create/assign/gated --dry-run."""
        # Create a helper agent fx for assignment and gated profile
        tok_fx = create_agent(
            self.harness,
            "fx",
            adapter="fake",
            delivery="launch",
            allowed_models=["m-a", "m-b"],
            default_model="m-a",
            default_profile="edit",
            max_unapproved_profile="dev",
        )

        # Create a real task to test task assign --dry-run
        rc_real, resp_real = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Real unassigned task",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_real, 0)
        real_task_id = resp_real["data"]["id"]

        def get_db_counts() -> Dict[str, int]:
            conn = self.harness.db()
            counts = {
                "events": conn.execute("SELECT COUNT(*) as c FROM events;").fetchone()["c"],
                "outbox": conn.execute("SELECT COUNT(*) as c FROM outbox;").fetchone()["c"],
                "runs": conn.execute("SELECT COUNT(*) as c FROM runs;").fetchone()["c"],
                "idempotency": conn.execute("SELECT COUNT(*) as c FROM idempotency;").fetchone()["c"],
                "tasks": conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"],
            }
            conn.close()
            return counts

        counts_before = get_db_counts()

        # 1. task create --dry-run
        rc_tc, resp_tc = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Dry run task",
            "--spec", "Spec",
            "--dry-run",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_tc, 0)
        self.assertTrue(resp_tc["ok"])
        self.assertTrue(resp_tc.get("dry_run", False))
        self.assertIn("would", resp_tc.get("data", {}))
        self.assertEqual(get_db_counts(), counts_before)

        # 2. task assign --dry-run
        rc_ta, resp_ta = self.harness.cli(
            "task", "assign", real_task_id,
            "--to", "fx",
            "--dry-run",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_ta, 0)
        self.assertTrue(resp_ta["ok"])
        self.assertTrue(resp_ta.get("dry_run", False))
        self.assertIn("would", resp_ta.get("data", {}))
        self.assertEqual(get_db_counts(), counts_before)

        # 3. Gated op with --dry-run (permission escalation to elevated profile)
        rc_gated, resp_gated = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Gated dry run task",
            "--spec", "Spec",
            "--assignee", "fx",
            "--profile", "elevated",
            "--dry-run",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_gated, c.EXIT_APPROVAL_REQUIRED)
        self.assertFalse(resp_gated["ok"])
        self.assertEqual(resp_gated["error"]["code"], c.APPROVAL_REQUIRED)
        details = resp_gated["error"]["details"]
        self.assertIsNone(details["approval_id"])
        self.assertIsNotNone(details["op_hash"])
        self.assertEqual(details["category"], "permission_escalation")
        self.assertEqual(get_db_counts(), counts_before)

    def test_d3_model_not_allowed(self) -> None:
        """ACC: D3 - m task create --project $PRJ --title t --spec s --assignee fx --model m-z [@lead1] -> rc 2 MODEL_NOT_ALLOWED."""
        create_agent(
            self.harness,
            "fx_d3",
            adapter="fake",
            delivery="launch",
            allowed_models=["m-a", "m-b"],
            default_model="m-a",
        )
        conn = self.harness.db()
        tasks_before = conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"]
        conn.close()

        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D3",
            "--spec", "Spec",
            "--assignee", "fx_d3",
            "--model", "m-z",
            token=self.tok_lead1,
        )
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.MODEL_NOT_ALLOWED)
        self.assertEqual(resp["error"]["details"]["allowed"], ["m-a", "m-b"])

        # Model on agent with empty allowed_models list (ibx) -> MODEL_NOT_ALLOWED
        rc_ibx, resp_ibx = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D3 Empty",
            "--spec", "Spec",
            "--assignee", "ibx",
            "--model", "m-any",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_ibx, c.EXIT_USAGE)
        self.assertEqual(resp_ibx["error"]["code"], c.MODEL_NOT_ALLOWED)

        # Disabled assignee -> INVALID_ARGUMENT with details.reason: disabled
        self.harness.cli("agent", "disable", "fx_d3", user=True)
        rc_dis, resp_dis = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D3 Disabled",
            "--spec", "Spec",
            "--assignee", "fx_d3",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_dis, c.EXIT_USAGE)
        self.assertEqual(resp_dis["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_dis["error"]["details"]["reason"], "disabled")

        conn = self.harness.db()
        tasks_after = conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"]
        conn.close()
        self.assertEqual(tasks_before, tasks_after)

    def test_d7_effort_validation(self) -> None:
        """ACC: D7 - Effort not in allowed_efforts on fake agent; any --effort on agent with --adapter vibe."""
        # 1. Fake agent with allowed_efforts ["low", "medium"]
        create_agent(
            self.harness,
            "fx_d7",
            adapter="fake",
            delivery="launch",
            allowed_efforts=["low", "medium"],
            default_effort="low",
        )
        rc1, resp1 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D7 Fake",
            "--spec", "Spec",
            "--assignee", "fx_d7",
            "--effort", "high",
            token=self.tok_lead1,
        )
        self.assertEqual(rc1, c.EXIT_USAGE)
        self.assertFalse(resp1["ok"])
        self.assertEqual(resp1["error"]["code"], c.EFFORT_NOT_ALLOWED)
        self.assertEqual(resp1["error"]["details"]["allowed"], ["low", "medium"])

        # 2. Vibe agent: rejects effort at agent create (vibe adapter does not support effort per plan §9.1)
        rc2, resp2 = self.harness.cli(
            "agent", "create",
            "--name", "ag_vibe_launch",
            "--adapter", "vibe",
            "--delivery", "launch",
            "--allowed-model", "mistral-large",
            "--default-effort", "high",
            "--max-concurrency", "1",
            user=True,
        )
        self.assertEqual(rc2, c.EXIT_USAGE)
        self.assertFalse(resp2["ok"])
        self.assertEqual(resp2["error"]["code"], c.EFFORT_UNSUPPORTED)

        # 2b. Any --effort on a task for a vibe agent: rc 2 EFFORT_UNSUPPORTED, no task row.
        rc_va, resp_va = self.harness.cli(
            "agent", "create",
            "--name", "ag_vibe_d7",
            "--adapter", "vibe",
            # inbox delivery: a vibe launch agent is refused earlier (13 ADAPTER_UNSUPPORTED,
            # vibe launching is M3), which would hide the effort check.
            "--delivery", "inbox",
            "--allowed-model", "mistral-large",
            "--max-concurrency", "1",
            user=True,
        )
        self.assertEqual(rc_va, 0, resp_va)
        conn = self.harness.db()
        n_before = conn.execute("SELECT COUNT(*) AS n FROM tasks;").fetchone()["n"]
        conn.close()
        for effort in ("high", "low"):
            rc_ve, resp_ve = self.harness.cli(
                "task", "create",
                "--project", self.project_id,
                "--title", f"Task D7 Vibe {effort}",
                "--spec", "Spec",
                "--assignee", "ag_vibe_d7",
                "--effort", effort,
                token=self.tok_lead1,
            )
            self.assertEqual(rc_ve, c.EXIT_USAGE, resp_ve)
            self.assertEqual(resp_ve["error"]["code"], c.EFFORT_UNSUPPORTED, resp_ve)
        conn = self.harness.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM tasks;").fetchone()["n"], n_before)
        conn.close()

        # 3. Profile/effort swap rejection (§9.1)
        rc3, resp3 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D7 Swap",
            "--spec", "Spec",
            "--assignee", "fx_d7",
            "--profile", "high",
            token=self.tok_lead1,
        )
        self.assertEqual(rc3, c.EXIT_USAGE)
        self.assertFalse(resp3["ok"])
        self.assertEqual(resp3["error"]["code"], c.INVALID_ARGUMENT)

        rc4, resp4 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D7 Swap 2",
            "--spec", "Spec",
            "--assignee", "fx_d7",
            "--effort", "dev",
            token=self.tok_lead1,
        )
        self.assertEqual(rc4, c.EXIT_USAGE)
        self.assertFalse(resp4["ok"])
        self.assertEqual(resp4["error"]["code"], c.INVALID_ARGUMENT)

        # 4. Effort on empty-allow-list agent (ibx) -> EFFORT_NOT_ALLOWED
        rc5, resp5 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D7 Empty",
            "--spec", "Spec",
            "--assignee", "ibx",
            "--effort", "low",
            token=self.tok_lead1,
        )
        self.assertEqual(rc5, c.EXIT_USAGE)
        self.assertEqual(resp5["error"]["code"], c.EFFORT_NOT_ALLOWED)

        # 5. Profile bogus -> INVALID_ARGUMENT with details.param: profile
        rc6, resp6 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D7 Bogus Profile",
            "--spec", "Spec",
            "--profile", "bogus",
            token=self.tok_lead1,
        )
        self.assertEqual(rc6, c.EXIT_USAGE)
        self.assertEqual(resp6["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp6["error"]["details"]["param"], "profile")

    def test_d16_permission_escalation_approval(self) -> None:
        """ACC: D16 - m task create … --assignee fx --profile elevated [@lead1] -> rc 7 APPROVAL_REQUIRED."""
        create_agent(
            self.harness,
            "fx_d16",
            adapter="fake",
            delivery="launch",
            default_profile="edit",
            max_unapproved_profile="dev",
        )
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D16",
            "--spec", "Spec",
            "--assignee", "fx_d16",
            "--profile", "elevated",
            token=self.tok_lead1,
        )
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.APPROVAL_REQUIRED)
        self.assertEqual(resp["error"]["details"]["category"], "permission_escalation")

        # Verify no task was created
        conn = self.harness.db()
        trow = conn.execute("SELECT * FROM tasks WHERE title = 'Task D16';").fetchone()
        conn.close()
        self.assertIsNone(trow)

        # ACC: D16 clause: "no task created until approved and re-run with --approval"
        d16_apr_id = resp["error"]["details"]["approval_id"]
        d16_op_hash = resp["error"]["details"]["op_hash"]
        rc_app, resp_app = self.harness.cli("approval", "approve", d16_apr_id, "--op-hash", d16_op_hash, user=True)
        self.assertEqual(rc_app, 0)

        rc_re, resp_re = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D16",
            "--spec", "Spec",
            "--assignee", "fx_d16",
            "--profile", "elevated",
            "--approval", d16_apr_id,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_re, 0)
        self.assertTrue(resp_re["ok"])

        conn = self.harness.db()
        trow = conn.execute("SELECT * FROM tasks WHERE title = 'Task D16';").fetchone()
        self.assertIsNotNone(trow)
        arow = conn.execute("SELECT status, consumed_by FROM approvals WHERE id = ?;", (d16_apr_id,)).fetchone()
        conn.close()
        self.assertEqual(arow["status"], "consumed")

        # task assign escalation records op task.assign
        rc_u, resp_u = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task D16 Unassigned",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_u, 0)
        unassigned_tsk_id = resp_u["data"]["id"]

        rc_asg, resp_asg = self.harness.cli(
            "task", "assign",
            unassigned_tsk_id,
            "--to", "fx_d16",
            "--profile", "elevated",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_asg, c.EXIT_APPROVAL_REQUIRED)
        self.assertEqual(resp_asg["error"]["code"], c.APPROVAL_REQUIRED)
        apr_id = resp_asg["error"]["details"]["approval_id"]
        self.assertIsNotNone(apr_id)

        # Verify op_json in approvals table has op="task.assign"
        conn = self.harness.db()
        apr_row = conn.execute("SELECT op_type, op_json FROM approvals WHERE id = ?;", (apr_id,)).fetchone()
        conn.close()
        self.assertIsNotNone(apr_row)
        self.assertEqual(apr_row["op_type"], "task.assign")
        op_data = json.loads(apr_row["op_json"])
        self.assertEqual(op_data["op"], "task.assign")

    def test_unrelated_agent_task_show_forbidden(self) -> None:
        """Unrelated agent token on task show <tsk> returns rc 4 SCOPE_MISMATCH."""
        tok_other = create_agent(
            self.harness,
            "fx_other_agent",
            adapter="fake",
            delivery="launch",
        )

        # Create a task under self.project_id
        rc_c, resp_c = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task Show Secret",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_c, 0)
        tsk_id = resp_c["data"]["id"]

        # Other agent attempts to task show
        rc_s, resp_s = self.harness.cli("task", "show", tsk_id, token=tok_other)
        self.assertEqual(rc_s, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_s["error"]["code"], c.SCOPE_MISMATCH)

    def test_e8_lead_assigns_non_lead_rejected(self) -> None:
        """ACC: E8 - Lead assigns directly; non-lead returns rc 4 NOT_PROJECT_LEAD; no claim verb."""
        # 1. Lead creates assigned task directly -> rc 0
        rc1, resp1 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task E8",
            "--spec", "Spec",
            "--assignee", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc1, 0)
        self.assertTrue(resp1["ok"])
        self.assertEqual(resp1["data"]["status"], "assigned")
        self.assertEqual(resp1["data"]["assignee"], "ibx")

        # 2. Unassigned task assigned by non-lead [@plain] -> rc 4 NOT_PROJECT_LEAD
        rc2, resp2 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task E8 Unassigned",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc2, 0)
        unassigned_id = resp2["data"]["id"]

        rc_plain, resp_plain = self.harness.cli(
            "task", "assign", unassigned_id,
            "--to", "ibx",
            token=self.tok_plain,
        )
        self.assertEqual(rc_plain, c.EXIT_FORBIDDEN)
        self.assertFalse(resp_plain["ok"])
        self.assertEqual(resp_plain["error"]["code"], c.NOT_PROJECT_LEAD)

        # 3. Check m task --help does not have claim verb
        rc_help, resp_help = self.harness.cli("task", "--help")
        self.assertEqual(rc_help, 0)
        raw_text = json.dumps(resp_help)
        self.assertNotIn("claim", raw_text)

    def test_e9_lead_cannot_be_assignee(self) -> None:
        """ACC: E9 - m task create --project $PRJ … --assignee lead1 [@lead1] -> rc 2 LEAD_CANNOT_BE_ASSIGNEE."""
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task E9",
            "--spec", "Spec",
            "--assignee", "lead1",
            token=self.tok_lead1,
        )
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.LEAD_CANNOT_BE_ASSIGNEE)

    def test_dep_add_list_remove(self) -> None:
        """Verify m dep add tsk_b --on tsk_a; m dep list tsk_b; m dep remove tsk_b --on tsk_a."""
        rc_a, resp_a = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task A",
            "--spec", "Spec A",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_a, 0)
        tsk_a = resp_a["data"]["id"]

        rc_b, resp_b = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task B",
            "--spec", "Spec B",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_b, 0)
        tsk_b = resp_b["data"]["id"]

        # 1. Add dep: B depends on A
        rc_add, resp_add = self.harness.cli(
            "dep", "add", tsk_b,
            "--on", tsk_a,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_add, 0)
        self.assertTrue(resp_add["ok"])
        self.assertEqual(resp_add["data"]["task_id"], tsk_b)
        self.assertEqual(resp_add["data"]["depends_on"], tsk_a)

        # 2. List deps of B
        rc_list, resp_list = self.harness.cli(
            "dep", "list", tsk_b,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_list, 0)
        self.assertTrue(resp_list["ok"])
        self.assertEqual(resp_list["data"]["depends_on"], [tsk_a])

        # Verify task show tsk_b reflects depends_on
        rc_show, resp_show = self.harness.cli("task", "show", tsk_b, token=self.tok_lead1)
        self.assertEqual(rc_show, 0)
        self.assertEqual(resp_show["data"]["depends_on"], [tsk_a])

        # 3. Remove dep
        rc_rm, resp_rm = self.harness.cli(
            "dep", "remove", tsk_b,
            "--on", tsk_a,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_rm, 0)
        self.assertTrue(resp_rm["ok"])

        # Check list again: empty
        rc_list2, resp_list2 = self.harness.cli("dep", "list", tsk_b, token=self.tok_lead1)
        self.assertEqual(rc_list2, 0)
        self.assertEqual(resp_list2["data"]["depends_on"], [])

    def test_f5_dep_cycle_detection(self) -> None:
        """ACC: F5 - Cycle detection in dep add: m dep add tsk_a --on tsk_b when b depends on a returns rc 2 DEP_CYCLE."""
        rc_a, resp_a = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task F6 A",
            "--spec", "Spec A",
            token=self.tok_lead1,
        )
        tsk_a = resp_a["data"]["id"]

        rc_b, resp_b = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task F6 B",
            "--spec", "Spec B",
            token=self.tok_lead1,
        )
        tsk_b = resp_b["data"]["id"]

        # B depends on A
        rc_add, _ = self.harness.cli("dep", "add", tsk_b, "--on", tsk_a, token=self.tok_lead1)
        self.assertEqual(rc_add, 0)

        # Attempt A depends on B -> cycle
        rc_cyc, resp_cyc = self.harness.cli("dep", "add", tsk_a, "--on", tsk_b, token=self.tok_lead1)
        self.assertEqual(rc_cyc, c.EXIT_USAGE)
        self.assertFalse(resp_cyc["ok"])
        self.assertEqual(resp_cyc["error"]["code"], c.DEP_CYCLE)

        # Attempt self-cycle A depends on A
        rc_self, resp_self = self.harness.cli("dep", "add", tsk_a, "--on", tsk_a, token=self.tok_lead1)
        self.assertEqual(rc_self, c.EXIT_USAGE)
        self.assertFalse(resp_self["ok"])
        self.assertEqual(resp_self["error"]["code"], c.DEP_CYCLE)

    def test_n4_adapter_launch_disabled(self) -> None:
        """ACC: N4, N23 - Launch agent on adapter agy; m task create … --assignee <it> [@lead1] -> rc 13 ADAPTER_UNSUPPORTED M3, details.reason no_smoke_evidence (no agy results file)."""
        from mesh.adapters import evidence, smoke_schema

        # Precondition (N23): no agy results file where this testing-mode daemon could look.
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)
        self.assertIsNone(evidence.results_dir(True, {}))
        self.assertIsNone(smoke_schema.load_latest(str(evidence.results_dir(False)), "agy"))
        create_agent(
            self.harness,
            "ag_agy",
            adapter="agy",
            delivery="launch",
            default_profile="edit",
        )
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task N4",
            "--spec", "Spec",
            "--assignee", "ag_agy",
            token=self.tok_lead1,
        )
        self.assertEqual(rc, c.EXIT_ADAPTER_UNAVAILABLE)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.ADAPTER_UNSUPPORTED)
        self.assertEqual(resp["error"]["details"]["milestone"], "M3")
        self.assertEqual(resp["error"]["details"]["reason"], "no_smoke_evidence")

    def test_n5_adapter_profile_unsupported(self) -> None:
        """ACC: N5 - Kiro: dev & elevated; agy: dev & elevated; vibe: elevated -> rc 13 PROFILE_UNSUPPORTED, no approval row."""
        create_agent(self.harness, "ag_kiro", adapter="kiro", delivery="launch")
        create_agent(self.harness, "ag_agy_n5", adapter="agy", delivery="launch")
        create_agent(self.harness, "ag_vibe_n5", adapter="vibe", delivery="launch")

        conn = self.harness.db()
        approvals_before = conn.execute("SELECT COUNT(*) as c FROM approvals;").fetchone()["c"]
        conn.close()

        # Kiro: dev
        rc1, r1 = self.harness.cli("task", "create", "--project", self.project_id, "--title", "Kiro dev", "--spec", "s", "--assignee", "ag_kiro", "--profile", "dev", token=self.tok_lead1)
        self.assertEqual(rc1, c.EXIT_ADAPTER_UNAVAILABLE)
        self.assertEqual(r1["error"]["code"], c.PROFILE_UNSUPPORTED)

        # Kiro: elevated
        rc2, r2 = self.harness.cli("task", "create", "--project", self.project_id, "--title", "Kiro elevated", "--spec", "s", "--assignee", "ag_kiro", "--profile", "elevated", token=self.tok_lead1)
        self.assertEqual(rc2, c.EXIT_ADAPTER_UNAVAILABLE)
        self.assertEqual(r2["error"]["code"], c.PROFILE_UNSUPPORTED)

        # Agy: dev
        rc3, r3 = self.harness.cli("task", "create", "--project", self.project_id, "--title", "Agy dev", "--spec", "s", "--assignee", "ag_agy_n5", "--profile", "dev", token=self.tok_lead1)
        self.assertEqual(rc3, c.EXIT_ADAPTER_UNAVAILABLE)
        self.assertEqual(r3["error"]["code"], c.PROFILE_UNSUPPORTED)

        # Agy: elevated
        rc4, r4 = self.harness.cli("task", "create", "--project", self.project_id, "--title", "Agy elevated", "--spec", "s", "--assignee", "ag_agy_n5", "--profile", "elevated", token=self.tok_lead1)
        self.assertEqual(rc4, c.EXIT_ADAPTER_UNAVAILABLE)
        self.assertEqual(r4["error"]["code"], c.PROFILE_UNSUPPORTED)

        # Vibe: elevated
        rc5, r5 = self.harness.cli("task", "create", "--project", self.project_id, "--title", "Vibe elevated", "--spec", "s", "--assignee", "ag_vibe_n5", "--profile", "elevated", token=self.tok_lead1)
        self.assertEqual(rc5, c.EXIT_ADAPTER_UNAVAILABLE)
        self.assertEqual(r5["error"]["code"], c.PROFILE_UNSUPPORTED)

        # Verify no approval row was created
        conn = self.harness.db()
        approvals_after = conn.execute("SELECT COUNT(*) as c FROM approvals;").fetchone()["c"]
        conn.close()
        self.assertEqual(approvals_before, approvals_after)

    def test_task_cancel_and_show_list(self) -> None:
        """Verify task cancellation (T22) requires reason and emits task.cancelled."""
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task to cancel",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        task_id = resp["data"]["id"]

        # Cancel without reason -> rc 2
        rc_no_r, resp_no_r = self.harness.cli("task", "cancel", task_id, token=self.tok_lead1)
        self.assertEqual(rc_no_r, c.EXIT_USAGE)

        # Cancel with reason -> rc 0
        rc_c, resp_c = self.harness.cli("task", "cancel", task_id, "--reason", "No longer needed", token=self.tok_lead1)
        self.assertEqual(rc_c, 0)
        self.assertTrue(resp_c["ok"])
        self.assertEqual(resp_c["data"]["status"], "cancelled")

        # Task show reflects cancelled
        rc_s, resp_s = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc_s, 0)
        self.assertEqual(resp_s["data"]["status"], "cancelled")

        # Task list filtering
        rc_l, resp_l = self.harness.cli("task", "list", "--project", self.project_id, "--status", "cancelled", token=self.tok_lead1)
        self.assertEqual(rc_l, 0)
        ids = [item["id"] for item in resp_l["data"]["items"]]
        self.assertIn(task_id, ids)

    def test_relayed_mutations_forbidden(self) -> None:
        """Finding 1: task and dep mutations with --relayed return rc 4 RELAY_NOT_ALLOWED."""
        tok_grok = create_agent(self.harness, "grok", adapter="none", delivery="inbox", capabilities=["relay"])

        conn = self.harness.db()
        init_tasks = conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"]
        init_deps = conn.execute("SELECT COUNT(*) as c FROM task_deps;").fetchone()["c"]
        conn.close()

        # 1. task create with --relayed
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Relayed Task",
            "--spec", "Spec",
            "--relayed",
            "--user-statement", "Create this task",
            token=tok_grok,
        )
        self.assertEqual(rc, c.EXIT_FORBIDDEN)
        self.assertEqual(resp["error"]["code"], c.RELAY_NOT_ALLOWED)

        # Create a task legitimately with lead1
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Legit Task 1",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc, 0)
        task1_id = resp["data"]["id"]

        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Legit Task 2",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc, 0)
        task2_id = resp["data"]["id"]

        conn = self.harness.db()
        cur_tasks = conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"]
        cur_deps = conn.execute("SELECT COUNT(*) as c FROM task_deps;").fetchone()["c"]
        conn.close()
        self.assertEqual(cur_tasks, init_tasks + 2)

        # 2. task assign with --relayed
        rc, resp = self.harness.cli(
            "task", "assign",
            task1_id,
            "--to", "ibx",
            "--relayed",
            "--user-statement", "Assign task",
            token=tok_grok,
        )
        self.assertEqual(rc, c.EXIT_FORBIDDEN)
        self.assertEqual(resp["error"]["code"], c.RELAY_NOT_ALLOWED)

        # 3. task cancel with --relayed
        rc, resp = self.harness.cli(
            "task", "cancel",
            task1_id,
            "--reason", "cancel it",
            "--relayed",
            "--user-statement", "Cancel task",
            token=tok_grok,
        )
        self.assertEqual(rc, c.EXIT_FORBIDDEN)
        self.assertEqual(resp["error"]["code"], c.RELAY_NOT_ALLOWED)

        # 4. dep add with --relayed
        rc, resp = self.harness.cli(
            "dep", "add",
            task2_id,
            "--on", task1_id,
            "--relayed",
            "--user-statement", "Add dep",
            token=tok_grok,
        )
        self.assertEqual(rc, c.EXIT_FORBIDDEN)
        self.assertEqual(resp["error"]["code"], c.RELAY_NOT_ALLOWED)

        # Legit dep add with lead1
        rc, resp = self.harness.cli("dep", "add", task2_id, "--on", task1_id, token=self.tok_lead1)
        self.assertEqual(rc, 0)

        # 5. dep remove with --relayed
        rc, resp = self.harness.cli(
            "dep", "remove",
            task2_id,
            "--on", task1_id,
            "--relayed",
            "--user-statement", "Remove dep",
            token=tok_grok,
        )
        self.assertEqual(rc, c.EXIT_FORBIDDEN)
        self.assertEqual(resp["error"]["code"], c.RELAY_NOT_ALLOWED)

        # Verify task and dep counts
        conn = self.harness.db()
        final_tasks = conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"]
        final_deps = conn.execute("SELECT COUNT(*) as c FROM task_deps;").fetchone()["c"]
        conn.close()
        self.assertEqual(final_tasks, init_tasks + 2)
        self.assertEqual(final_deps, cur_deps + 1)

    def test_task_version_increments_and_terminal_accepted(self) -> None:
        """Version increments on assign, promotion, and cancel, and general accepted task cannot be cancelled."""
        # 1. Create unassigned task (initial version = 1)
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Version Test",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc, 0)
        task_id = resp["data"]["id"]
        self.assertEqual(resp["data"]["version"], 1)

        # 2. Assign task -> assign bumps version 1 -> 2, then scheduler post-commit promotion bumps 2 -> 3
        rc_a, resp_a = self.harness.cli(
            "task", "assign", task_id,
            "--to", "ibx",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_a, 0)
        self.assertEqual(resp_a["data"]["version"], 2)

        rc_s, resp_s = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc_s, 0)
        self.assertEqual(resp_s["data"]["status"], "queued")
        self.assertEqual(resp_s["data"]["version"], 3)

        # 3. Cancel task -> version becomes 4
        rc_c, resp_c = self.harness.cli(
            "task", "cancel", task_id,
            "--reason", "Version test cancel",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_c, 0)
        self.assertEqual(resp_c["data"]["version"], 4)

        # 4. Accepted general task cannot be cancelled (terminal state per §6.2)
        rc_g, resp_g = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "General Accepted",
            "--spec", "Spec",
            "--kind", "general",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_g, 0)
        g_id = resp_g["data"]["id"]

        from mesh.core.db import connect
        from mesh.core.events import record_event
        wconn = connect(os.path.join(self.harness.home, "mesh.db"))
        wconn.execute("UPDATE tasks SET status = 'accepted' WHERE id = ?;", (g_id,))
        lead1_id = wconn.execute("SELECT id FROM principals WHERE name = 'lead1';").fetchone()["id"]
        record_event(
            wconn,
            type_="task.accepted",
            actor_id=lead1_id,
            actor_kind="agent",
            via="direct",
            project_id=self.project_id,
            task_id=g_id,
            from_state="created",
            to_state="accepted",
        )
        wconn.close()

        rc_gc, resp_gc = self.harness.cli(
            "task", "cancel", g_id,
            "--reason", "Try cancel accepted general",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_gc, c.EXIT_CONFLICT)
        self.assertFalse(resp_gc["ok"])
        self.assertEqual(resp_gc["error"]["code"], c.INVALID_TRANSITION)

    def test_depends_on_checked_before_ag_approval(self) -> None:
        """Finding 6: Invalid --depends-on must fail before creating a permission_escalation approval."""
        create_agent(
            self.harness,
            "ag_order_test",
            adapter="fake",
            delivery="launch",
            default_profile="edit",
            max_unapproved_profile="dev",
        )
        conn = self.harness.db()
        apr_count_before = conn.execute("SELECT COUNT(*) as c FROM approvals;").fetchone()["c"]
        conn.close()

        # Try create with invalid dep and elevated profile (which would trigger approval if AG ran first)
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Invalid Dep Task",
            "--spec", "Spec",
            "--assignee", "ag_order_test",
            "--profile", "elevated",
            "--depends-on", "tsk_nonexistent_123",
            token=self.tok_lead1,
        )
        self.assertEqual(rc, c.EXIT_NOT_FOUND)
        self.assertEqual(resp["error"]["code"], c.NOT_FOUND)

        # Verify NO approval row was created
        conn = self.harness.db()
        apr_count_after = conn.execute("SELECT COUNT(*) as c FROM approvals;").fetchone()["c"]
        conn.close()
        self.assertEqual(apr_count_before, apr_count_after)

    @serial
    def test_task_wait_state_reached_and_timeout(self) -> None:
        """Verify task wait reaches target state, wakes on transition, and times out with rc 8."""
        rc_c, resp_c = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Wait Target Task",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_c, 0)
        tsk_id = resp_c["data"]["id"]

        # 1. State already matches state:created -> returns immediately rc 0
        rc_w1, resp_w1 = self.harness.cli("task", "wait", tsk_id, "--until", "state:created", "--timeout", "2s", token=self.tok_lead1)
        self.assertEqual(rc_w1, 0)
        self.assertEqual(resp_w1["data"]["id"], tsk_id)
        self.assertEqual(resp_w1["data"]["status"], "created")

        # Missing --timeout on task wait -> rc 2 INVALID_ARGUMENT (param: timeout)
        rc_no_to, resp_no_to = self.harness.cli("task", "wait", tsk_id, "--until", "submitted", token=self.tok_lead1)
        self.assertEqual(rc_no_to, c.EXIT_USAGE)
        self.assertEqual(resp_no_to.get("error", {}).get("code"), c.INVALID_ARGUMENT)
        self.assertEqual(resp_no_to.get("error", {}).get("details", {}).get("param"), "timeout")

        # Dry-run rejected on task wait -> rc 2 INVALID_ARGUMENT (param: dry_run)
        rc_dry, resp_dry = self.harness.cli("task", "wait", tsk_id, "--until", "submitted", "--timeout", "1s", "--dry-run", token=self.tok_lead1)
        self.assertEqual(rc_dry, c.EXIT_USAGE)
        self.assertEqual(resp_dry.get("error", {}).get("code"), c.INVALID_ARGUMENT)
        self.assertEqual(resp_dry.get("error", {}).get("details", {}).get("param"), "dry_run")

        # 2. Timeout waiting for state:running (which won't happen in M1b) -> rc 8 WAIT_TIMEOUT
        # 1 s (was 2 s, m3-a1-speed): any expired timeout proves rc 8; step 3 already uses 1 s.
        rc_w2, resp_w2 = self.harness.cli("task", "wait", tsk_id, "--until", "state:running", "--timeout", "1s", token=self.tok_lead1)
        self.assertEqual(rc_w2, c.EXIT_WAIT_TIMEOUT)
        self.assertEqual(resp_w2["error"]["code"], c.WAIT_TIMEOUT)

        # 3. Accepted value 'submitted' times out in M1b with rc 8
        rc_sub, resp_sub = self.harness.cli("task", "wait", tsk_id, "--until", "submitted", "--timeout", "1s", token=self.tok_lead1)
        self.assertEqual(rc_sub, c.EXIT_WAIT_TIMEOUT)
        self.assertEqual(resp_sub["error"]["code"], c.WAIT_TIMEOUT)

        # 4. Long-poll wait wakes promptly on transition (assigning task -> promoted to queued)
        def run_wait() -> Any:
            return self.harness.cli("task", "wait", tsk_id, "--until", "state:queued", "--timeout", "10s", token=self.tok_lead1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut = executor.submit(run_wait)
            self.assertTrue(
                self.harness.wait_for_active_waits(1),
                "task wait never registered as an active wait",
            )

            assign_start = time.monotonic()
            rc_asg, resp_asg = self.harness.cli("task", "assign", tsk_id, "--to", "ibx", token=self.tok_lead1)
            self.assertEqual(rc_asg, 0)

            rc_fut, resp_fut = fut.result(timeout=5.0)
            wait_dur = time.monotonic() - assign_start
            self.assertEqual(rc_fut, 0)
            self.assertLess(wait_dur, 2.0, f"Task wait took too long to wake: {wait_dur}s")
            self.assertEqual(resp_fut["data"]["status"], "queued")

        # 5. Long-poll wait wakes promptly on terminal condition (cancel)
        def run_wait_terminal() -> Any:
            return self.harness.cli("task", "wait", tsk_id, "--until", "terminal", "--timeout", "10s", token=self.tok_lead1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_term = executor.submit(run_wait_terminal)
            self.assertTrue(
                self.harness.wait_for_active_waits(1),
                "terminal task wait never registered as an active wait",
            )

            cancel_start = time.monotonic()
            rc_cn, _ = self.harness.cli("task", "cancel", tsk_id, "--reason", "Test done", token=self.tok_lead1)
            self.assertEqual(rc_cn, 0)

            rc_term, resp_term = fut_term.result(timeout=5.0)
            term_dur = time.monotonic() - cancel_start
            self.assertEqual(rc_term, 0)
            self.assertLess(term_dur, 2.0, f"Task wait took too long to wake on terminal: {term_dur}s")
            self.assertEqual(resp_term["data"]["status"], "cancelled")

    def test_h10_task_reassign_retry_escalate_transitions(self) -> None:
        """ACC: H10 - After a failure: reassign to ibx; escalate to user/grok inboxes; retry to queued; and reassign from queued."""
        tok_grok = create_agent(self.harness, "grok", adapter="none", delivery="inbox", capabilities=["intake"])
        tok_fx = create_agent(self.harness, "fx", adapter="fake", delivery="launch", default_profile="dev")

        # 1. Create project with lead lead1 and origin grok
        rc_p, resp_p = self.harness.cli(
            "project", "create",
            "--title", "Test H10",
            "--goal", "Test goal",
            "--criterion", "c1",
            "--lead", "lead1",
            token=tok_grok,
        )
        self.assertEqual(rc_p, 0)
        prj_id = resp_p["data"]["id"]

        # Create agent with a specific allowed model
        create_agent(
            self.harness,
            "agt_t6",
            adapter="fake",
            delivery="launch",
            default_profile="dev",
            allowed_models=["m-orig"],
            default_model="m-orig",
        )

        # 2. Create tasks assigned to agt_t6 with model m-orig
        rc_t1, resp_t1 = self.harness.cli(
            "task", "create",
            "--project", prj_id,
            "--title", "T6 Task 1",
            "--spec", "Spec 1",
            "--assignee", "agt_t6",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t1, 0)
        tsk1 = resp_t1["data"]["id"]

        rc_t2, resp_t2 = self.harness.cli(
            "task", "create",
            "--project", prj_id,
            "--title", "T6 Task 2",
            "--spec", "Spec 2",
            "--assignee", "agt_t6",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t2, 0)
        tsk2 = resp_t2["data"]["id"]

        rc_t3, resp_t3 = self.harness.cli(
            "task", "create",
            "--project", prj_id,
            "--title", "T6 Task 3",
            "--spec", "Spec 3",
            "--assignee", "agt_t6",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t3, 0)
        tsk3 = resp_t3["data"]["id"]

        # Wait for scheduler to promote all tasks to queued
        rc_w1, _ = self.harness.cli("task", "wait", tsk1, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w1, 0)
        rc_w2, _ = self.harness.cli("task", "wait", tsk2, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2, 0)
        rc_w3, _ = self.harness.cli("task", "wait", tsk3, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w3, 0)

        # Change agt_t6's allowed models so tasks' model (m-orig) becomes disallowed
        rc_upd, _ = self.harness.cli(
            "agent", "update", "agt_t6",
            "--allowed-model", "m-new",
            "--default-model", "m-new",
            user=True,
        )
        self.assertEqual(rc_upd, 0)

        # Wait for scheduler to detect disallowed model and fail tasks (T6)
        rc_w_fail1, resp_w_fail1 = self.harness.cli("task", "wait", tsk1, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w_fail1, 0)
        self.assertEqual(resp_w_fail1["data"]["status"], "failed")

        rc_w_fail2, resp_w_fail2 = self.harness.cli("task", "wait", tsk2, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w_fail2, 0)
        self.assertEqual(resp_w_fail2["data"]["status"], "failed")

        rc_w_fail3, resp_w_fail3 = self.harness.cli("task", "wait", tsk3, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w_fail3, 0)
        self.assertEqual(resp_w_fail3["data"]["status"], "failed")

        # Test T20: task reassign on tsk1 from failed -> assigned
        rc_reasg, resp_reasg = self.harness.cli(
            "task", "reassign", tsk1,
            "--to", "ibx",
            "--reason", "Reassigned to specialist",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_reasg, 0)
        self.assertEqual(resp_reasg["data"]["status"], "assigned")
        self.assertEqual(resp_reasg["data"]["assignee"], "ibx")

        # Wait for scheduler promotion assigned -> queued
        rc_w_q, _ = self.harness.cli("task", "wait", tsk1, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w_q, 0)

        # Test T20: reassign from queued
        rc_reasg_q, resp_reasg_q = self.harness.cli(
            "task", "reassign", tsk1,
            "--to", "fx",
            "--reason", "Switching to fx while queued",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_reasg_q, 0)
        self.assertEqual(resp_reasg_q["data"]["status"], "assigned")
        self.assertEqual(resp_reasg_q["data"]["assignee"], "fx")

        # Test T19: task retry on tsk2 with updated model m-new -> queued
        rc_retry, resp_retry = self.harness.cli(
            "task", "retry", tsk2,
            "--model", "m-new",
            "--reason", "Retry with new allowed model",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_retry, 0)
        self.assertEqual(resp_retry["data"]["status"], "queued")
        self.assertEqual(resp_retry["data"]["model"], "m-new")

        # Test T21: task escalate on tsk3 -> escalated
        rc_esc, resp_esc = self.harness.cli(
            "task", "escalate", tsk3,
            "--reason", "Blocked by dependency failure",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_esc, 0)
        self.assertEqual(resp_esc["data"]["status"], "escalated")

        # Verify task.escalated message delivered to user inbox and grok inbox
        rc_ibx_grok, resp_ibx_grok = self.harness.cli("inbox", "list", token=tok_grok)
        self.assertEqual(rc_ibx_grok, 0)
        esc_grok = [m for m in resp_ibx_grok["data"]["items"] if m["type"] == "task.escalated"]
        self.assertGreaterEqual(len(esc_grok), 1)
        self.assertIn("Blocked by dependency failure", esc_grok[0]["payload"]["reason"])

        # Check user inbox via --user
        rc_ibx_user, resp_ibx_user = self.harness.cli("inbox", "list", user=True)
        self.assertEqual(rc_ibx_user, 0)
        esc_user = [m for m in resp_ibx_user["data"]["items"] if m["type"] == "task.escalated"]
        self.assertGreaterEqual(len(esc_user), 1)
        self.assertIn("Blocked by dependency failure", esc_user[0]["payload"]["reason"])

    def test_repeated_task_transitions_outbox_dedup_and_inbox_delivery(self) -> None:
        """Finding 1: Repeated task transitions do not drop outbox notifications due to dedup keys.

        1. Queued inbox A reassigned to inbox B: B gets task.offered, A's offer is superseded.
        2. T6-failed task retried without model change fails again: lead inbox holds two task.failed messages.
        3. Escalate -> retry -> fail -> escalate: user inbox holds two task.escalated messages.
        """
        tok_ibx_a = create_agent(self.harness, "ibx_a", delivery="inbox")
        tok_ibx_b = create_agent(self.harness, "ibx_b", delivery="inbox")

        # 1. Queued inbox A -> reassign to inbox B
        rc_t1, resp_t1 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task Dedup 1",
            "--spec", "Spec 1",
            "--assignee", "ibx_a",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t1, 0)
        tsk1 = resp_t1["data"]["id"]

        # Wait for promotion to queued
        rc_w1, _ = self.harness.cli("task", "wait", tsk1, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w1, 0)

        # Check ibx_a inbox has task.offered
        rc_list_a, resp_list_a = self.harness.cli("inbox", "list", token=tok_ibx_a)
        self.assertEqual(rc_list_a, 0)
        offers_a = [m for m in resp_list_a["data"]["items"] if m["type"] == "task.offered" and m["payload"].get("task_id") == tsk1]
        self.assertEqual(len(offers_a), 1)

        # Reassign tsk1 from queued to ibx_b
        rc_reasg, resp_reasg = self.harness.cli(
            "task", "reassign", tsk1,
            "--to", "ibx_b",
            "--reason", "Handing off from A to B",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_reasg, 0)

        # Wait for promotion to queued for ibx_b
        rc_w1_b, _ = self.harness.cli("task", "wait", tsk1, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w1_b, 0)

        # Assert B's inbox has task.offered
        rc_list_b, resp_list_b = self.harness.cli("inbox", "list", token=tok_ibx_b)
        self.assertEqual(rc_list_b, 0)
        offers_b = [m for m in resp_list_b["data"]["items"] if m["type"] == "task.offered" and m["payload"].get("task_id") == tsk1]
        self.assertEqual(len(offers_b), 1)

        # Assert A's offer is gone from A's unacked inbox list
        rc_list_a2, resp_list_a2 = self.harness.cli("inbox", "list", token=tok_ibx_a)
        self.assertEqual(rc_list_a2, 0)
        offers_a2 = [m for m in resp_list_a2["data"]["items"] if m["type"] == "task.offered" and m["payload"].get("task_id") == tsk1]
        self.assertEqual(len(offers_a2), 0)

        # 2. T6-failed task retried without model change fails again -> lead inbox holds two task.failed messages
        create_agent(self.harness, "agt_fail2", delivery="launch", adapter="fake", allowed_models=["m-x"], default_model="m-x")
        rc_t2, resp_t2 = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task Dedup 2",
            "--spec", "Spec 2",
            "--assignee", "agt_fail2",
            "--model", "m-x",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t2, 0)
        tsk2 = resp_t2["data"]["id"]

        rc_w2_q, _ = self.harness.cli("task", "wait", tsk2, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2_q, 0)

        # Disallow m-x
        rc_upd, _ = self.harness.cli("agent", "update", "agt_fail2", "--allowed-model", "m-y", "--default-model", "m-y", user=True)
        self.assertEqual(rc_upd, 0)

        # First T6 failure
        rc_w2_f1, _ = self.harness.cli("task", "wait", tsk2, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2_f1, 0)

        # Retry without changing model
        rc_ret1, _ = self.harness.cli("task", "retry", tsk2, "--reason", "Retry without changing model", token=self.tok_lead1)
        self.assertEqual(rc_ret1, 0)

        # Second T6 failure
        rc_w2_f2, _ = self.harness.cli("task", "wait", tsk2, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2_f2, 0)

        # Check lead's inbox has two task.failed messages for tsk2
        rc_lead_ibx, resp_lead_ibx = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_lead_ibx, 0)
        failed_msgs = [m for m in resp_lead_ibx["data"]["items"] if m["type"] == "task.failed" and m["payload"].get("task_id") == tsk2]
        self.assertEqual(len(failed_msgs), 2)

        # 3. Escalate -> retry -> fail -> escalate -> user inbox holds two task.escalated messages
        rc_esc1, _ = self.harness.cli("task", "escalate", tsk2, "--reason", "First escalation", token=self.tok_lead1)
        self.assertEqual(rc_esc1, 0)

        # Retry from escalated
        rc_ret2, _ = self.harness.cli("task", "retry", tsk2, "--reason", "Retry after escalation", token=self.tok_lead1)
        self.assertEqual(rc_ret2, 0)

        # Third T6 failure
        rc_w2_f3, _ = self.harness.cli("task", "wait", tsk2, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w2_f3, 0)

        # Second escalation
        rc_esc2, _ = self.harness.cli("task", "escalate", tsk2, "--reason", "Second escalation", token=self.tok_lead1)
        self.assertEqual(rc_esc2, 0)

        # User inbox holds two task.escalated messages for tsk2
        rc_user_ibx, resp_user_ibx = self.harness.cli("inbox", "list", user=True)
        self.assertEqual(rc_user_ibx, 0)
        esc_msgs = [m for m in resp_user_ibx["data"]["items"] if m["type"] == "task.escalated" and m["payload"].get("task_id") == tsk2]
        self.assertEqual(len(esc_msgs), 2)

    def test_task_retry_effort_unsupported_on_vibe_adapter(self) -> None:
        """Finding 2: task retry --effort on a vibe-adapter task returns EFFORT_UNSUPPORTED (D7)."""
        create_agent(
            self.harness,
            "ag_vibe_retry",
            adapter="vibe",
            delivery="inbox",
            allowed_models=["m-orig"],
            default_model="m-orig",
            max_concurrency=1,
        )
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task Vibe Retry",
            "--spec", "Spec",
            "--assignee", "ag_vibe_retry",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        # Wait until queued
        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # Change allowed model to trigger T6 failure
        rc_upd, _ = self.harness.cli("agent", "update", "ag_vibe_retry", "--allowed-model", "m-other", "--default-model", "m-other", user=True)
        self.assertEqual(rc_upd, 0)

        # Wait until failed
        rc_wf, _ = self.harness.cli("task", "wait", tsk, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_wf, 0)

        # task retry --effort low must fail with EFFORT_UNSUPPORTED
        rc_retry, resp_retry = self.harness.cli(
            "task", "retry", tsk,
            "--effort", "low",
            "--reason", "Attempting effort override",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_retry, c.EXIT_USAGE)
        self.assertFalse(resp_retry["ok"])
        self.assertEqual(resp_retry["error"]["code"], c.EFFORT_UNSUPPORTED)
        self.assertEqual(resp_retry["error"]["details"]["adapter"], "vibe")

    def test_task_retry_over_limit_approval_gate_and_user_direct(self) -> None:
        """Finding 4: Seeded attempt = max_attempts on failed task gates lead retry and allows user retry."""
        create_agent(
            self.harness,
            "ag_retry_cap",
            adapter="fake",
            delivery="launch",
            allowed_models=["m-cap"],
            default_model="m-cap",
            max_concurrency=1,
        )
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task Retry Cap",
            "--spec", "Spec",
            "--assignee", "ag_retry_cap",
            "--model", "m-cap",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        # Wait until queued
        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # Trigger T6 failure by updating allowed model
        rc_upd, _ = self.harness.cli("agent", "update", "ag_retry_cap", "--allowed-model", "m-other", "--default-model", "m-other", user=True)
        self.assertEqual(rc_upd, 0)

        # Wait until state:failed
        rc_wf, _ = self.harness.cli("task", "wait", tsk, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_wf, 0)

        # Seed task attempt = max_attempts (3)
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE tasks SET attempt = max_attempts WHERE id = ?;", (tsk,))
        conn.commit()

        # Lead retry requires approval (rc 7)
        rc_lead, resp_lead = self.harness.cli(
            "task", "retry", tsk,
            "--model", "m-other",
            "--reason", "Lead retry at cap",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_lead, c.EXIT_APPROVAL_REQUIRED)
        self.assertFalse(resp_lead["ok"])
        self.assertEqual(resp_lead["error"]["code"], c.APPROVAL_REQUIRED)
        self.assertEqual(resp_lead["error"]["details"]["category"], "retry_over_limit")

        apr_id = resp_lead["error"]["details"]["approval_id"]
        op_hash = resp_lead["error"]["details"]["op_hash"]
        apr_row = conn.execute("SELECT op_type FROM approvals WHERE id = ?;", (apr_id,)).fetchone()
        conn.close()
        self.assertIsNotNone(apr_row)
        self.assertEqual(apr_row["op_type"], "task.retry")

        # Review 7b N2: approval.requested event has request_id matching response request_id
        req_id = resp_lead.get("request_id")
        self.assertIsNotNone(req_id)
        rc_ev, resp_ev = self.harness.cli("event", "list", "--type", "approval.requested", user=True)
        self.assertEqual(rc_ev, 0)
        matching_evs = [e for e in resp_ev["data"]["items"] if e["payload"].get("approval_id") == apr_id]
        self.assertTrue(len(matching_evs) > 0)
        self.assertEqual(matching_evs[0]["request_id"], req_id)

        # Verify task stays in 'failed' status
        rc_show, resp_show = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_show, 0)
        self.assertEqual(resp_show["data"]["status"], "failed")

        # 6h-6 / D7: User direct approve, then lead retries with --approval and succeeds
        rc_app, resp_app = self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc_app, 0)
        self.assertTrue(resp_app["ok"])

        rc_re, resp_re = self.harness.cli(
            "task", "retry", tsk,
            "--model", "m-other",
            "--reason", "Lead retry at cap",
            "--approval", apr_id,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_re, 0)
        self.assertTrue(resp_re["ok"])
        self.assertEqual(resp_re["data"]["status"], "queued")

        conn = self.harness.db()
        arow = conn.execute("SELECT status, consumed_by FROM approvals WHERE id = ?;", (apr_id,)).fetchone()
        conn.close()
        self.assertEqual(arow["status"], "consumed")

        # Seed again to verify user-direct retry also succeeds without approval
        wconn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        wconn.execute("UPDATE tasks SET status = 'failed', attempt = max_attempts WHERE id = ?;", (tsk,))
        wconn.commit()
        wconn.close()

        # User-direct retry succeeds (rc 0)
        rc_user, resp_user = self.harness.cli(
            "task", "retry", tsk,
            "--model", "m-other",
            "--reason", "User direct retry at cap",
            user=True,
        )
        self.assertEqual(rc_user, 0)
        self.assertTrue(resp_user["ok"])
        self.assertEqual(resp_user["data"]["status"], "queued")

    def test_task_mutations_negative_clauses(self) -> None:
        """Finding 6: Negative CLI clauses for task reassign, retry, and escalate."""
        create_agent(
            self.harness,
            "agt_neg",
            adapter="fake",
            delivery="launch",
            allowed_models=["m-neg"],
            default_model="m-neg",
            max_concurrency=1,
        )
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task Neg Clauses",
            "--spec", "Spec",
            "--assignee", "agt_neg",
            "--model", "m-neg",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        # Wait until queued
        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # 1. Non-lead callers get rc 4 NOT_PROJECT_LEAD
        rc_nl_reasg, resp_nl_reasg = self.harness.cli("task", "reassign", tsk, "--to", "ibx", "--reason", "r", token=self.tok_plain)
        self.assertEqual(rc_nl_reasg, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_nl_reasg["error"]["code"], c.NOT_PROJECT_LEAD)

        rc_nl_retry, resp_nl_retry = self.harness.cli("task", "retry", tsk, "--reason", "r", token=self.tok_plain)
        self.assertEqual(rc_nl_retry, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_nl_retry["error"]["code"], c.NOT_PROJECT_LEAD)

        rc_nl_esc, resp_nl_esc = self.harness.cli("task", "escalate", tsk, "--reason", "r", token=self.tok_plain)
        self.assertEqual(rc_nl_esc, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_nl_esc["error"]["code"], c.NOT_PROJECT_LEAD)

        # 2. Empty --reason gets rc 2 INVALID_ARGUMENT
        rc_emp_reasg, resp_emp_reasg = self.harness.cli("task", "reassign", tsk, "--to", "ibx", "--reason", "", token=self.tok_lead1)
        self.assertEqual(rc_emp_reasg, c.EXIT_USAGE)
        self.assertEqual(resp_emp_reasg["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_emp_reasg["error"]["details"]["param"], "reason")

        rc_emp_esc, resp_emp_esc = self.harness.cli("task", "escalate", tsk, "--reason", "", token=self.tok_lead1)
        self.assertEqual(rc_emp_esc, c.EXIT_USAGE)
        self.assertEqual(resp_emp_esc["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_emp_esc["error"]["details"]["param"], "reason")

        # 3. Escalate on non-failed task gets rc 6 INVALID_TRANSITION
        rc_esc_nf, resp_esc_nf = self.harness.cli("task", "escalate", tsk, "--reason", "Escalating while queued", token=self.tok_lead1)
        self.assertEqual(rc_esc_nf, c.EXIT_CONFLICT)
        self.assertEqual(resp_esc_nf["error"]["code"], c.INVALID_TRANSITION)

        # 4. Retry from escalated succeeds -> queued
        rc_upd, _ = self.harness.cli("agent", "update", "agt_neg", "--allowed-model", "m-other", "--default-model", "m-other", user=True)
        self.assertEqual(rc_upd, 0)

        rc_wf, _ = self.harness.cli("task", "wait", tsk, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_wf, 0)

        rc_esc_ok, _ = self.harness.cli("task", "escalate", tsk, "--reason", "Valid escalation from failed", token=self.tok_lead1)
        self.assertEqual(rc_esc_ok, 0)

        rc_ret_esc, resp_ret_esc = self.harness.cli("task", "retry", tsk, "--model", "m-other", "--reason", "Retry from escalated", token=self.tok_lead1)
        self.assertEqual(rc_ret_esc, 0)
        self.assertEqual(resp_ret_esc["data"]["status"], "queued")

    def test_approval_consumption_bound_to_gate_category(self) -> None:
        """Finding 1 (b): Approval consumption category binding prevents cross-category authorization."""
        create_agent(
            self.harness,
            "ag_cat_src",
            adapter="fake",
            delivery="launch",
            allowed_models=["m-cat"],
            default_model="m-cat",
            max_concurrency=1,
        )
        create_agent(
            self.harness,
            "ag_cat_dest",
            adapter="fake",
            delivery="launch",
            allowed_models=["m-cat", "m-other"],
            default_model="m-cat",
            default_profile="edit",
            max_unapproved_profile="edit",
            max_concurrency=1,
        )

        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Task Category Binding",
            "--spec", "Spec",
            "--assignee", "ag_cat_src",
            "--model", "m-cat",
            "--profile", "dev",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, 0)
        tsk = resp_t["data"]["id"]

        # Wait until queued
        rc_w, _ = self.harness.cli("task", "wait", tsk, "--until", "state:queued", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_w, 0)

        # Trigger failure on src agent
        rc_upd, _ = self.harness.cli("agent", "update", "ag_cat_src", "--allowed-model", "m-other", "--default-model", "m-other", user=True)
        self.assertEqual(rc_upd, 0)

        # Wait until state:failed
        rc_wf, _ = self.harness.cli("task", "wait", tsk, "--until", "state:failed", "--timeout", "5s", token=self.tok_lead1)
        self.assertEqual(rc_wf, 0)

        # Seed task attempt = max_attempts
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE tasks SET attempt = max_attempts WHERE id = ?;", (tsk,))
        conn.commit()

        # Step 1: Lead runs task reassign -> rc 7 retry_over_limit apr1
        rc_re, resp_re = self.harness.cli(
            "task", "reassign", tsk,
            "--to", "ag_cat_dest",
            "--reason", "Reassign at attempt cap",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_re, c.EXIT_APPROVAL_REQUIRED)
        self.assertFalse(resp_re["ok"])
        self.assertEqual(resp_re["error"]["code"], c.APPROVAL_REQUIRED)
        self.assertEqual(resp_re["error"]["details"]["category"], "retry_over_limit")

        apr1_id = resp_re["error"]["details"]["approval_id"]
        apr1_hash = resp_re["error"]["details"]["op_hash"]
        self.assertIsNotNone(apr1_id)

        # Step 2: User approves apr1 directly
        rc_ap, resp_ap = self.harness.cli(
            "approval", "approve", apr1_id,
            "--op-hash", apr1_hash,
            user=True,
        )
        self.assertEqual(rc_ap, 0)
        self.assertTrue(resp_ap["ok"])

        # Step 3: User retries task directly -> queued (attempt unchanged, still at cap)
        rc_ret, resp_ret = self.harness.cli(
            "task", "retry", tsk,
            "--model", "m-other",
            "--reason", "User direct retry",
            user=True,
        )
        self.assertEqual(rc_ret, 0)
        self.assertTrue(resp_ret["ok"])
        self.assertEqual(resp_ret["data"]["status"], "queued")

        # Step 4: Lead re-runs identical reassign with --approval apr1
        # Gate 1 (retry_over_limit) is skipped because status is queued.
        # Gate 2 (AG step 6: permission_escalation) executes because task profile 'dev' > 'edit'.
        # Consumption with category="permission_escalation" must fail with OP_HASH_MISMATCH (rc 12).
        rc_lead2, resp_lead2 = self.harness.cli(
            "task", "reassign", tsk,
            "--to", "ag_cat_dest",
            "--reason", "Reassign at attempt cap",
            "--approval", apr1_id,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_lead2, c.EXIT_APPROVAL_INVALID)
        self.assertFalse(resp_lead2["ok"])
        self.assertEqual(resp_lead2["error"]["code"], c.OP_HASH_MISMATCH)

        # apr1 stays approved
        apr_row = conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr1_id,)).fetchone()
        self.assertEqual(apr_row["status"], "approved")

        # Task remains queued with old assignee ag_cat_src
        rc_sh, resp_sh = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc_sh, 0)
        self.assertEqual(resp_sh["data"]["status"], "queued")
        self.assertEqual(resp_sh["data"]["assignee"], "ag_cat_src")
        conn.close()



if __name__ == "__main__":
    unittest.main()
