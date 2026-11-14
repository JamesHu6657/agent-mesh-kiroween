"""Integration tests for Projects, Intake, Criteria, and Lead Delegation per plan §16 M1.5.

ACC: B11, B12, E1, E2, E3, E4, E5, E6, E7, E10, E12, E13, E14, E18
"""

import concurrent.futures
import json
import os
import time
import unittest
from typing import Any, Dict

import mesh.constants as c
from tests.helpers import MeshHarness, serial


def create_agent(harness: MeshHarness, name: str, adapter: str = "none", delivery: str = "inbox",
                 can_lead: bool = False, capabilities: list = None,
                 allowed_models: list = None, default_model: str = None,
                 max_concurrency: int = 1) -> str:
    """Helper to register an agent and return its issued token."""
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

    rc, resp = harness.cli(*args, user=True)
    assert rc == 0, f"agent create {name} failed: {resp}"

    # Issue token
    rc_tok, tok_resp = harness.cli("agent", "token", "issue", name, user=True)
    assert rc_tok == 0, f"token issue {name} failed: {tok_resp}"
    token = tok_resp["data"]["token"]

    # Save to 0600 token file outside workspaces
    tok_dir = os.path.join(harness.home, "tokens")
    os.makedirs(tok_dir, mode=0o700, exist_ok=True)
    tok_path = os.path.join(tok_dir, f"{name}.tok")
    with open(tok_path, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(tok_path, 0o600)

    return token


class TestProjectsIntegration(unittest.TestCase):
    """Integration test suite for projects and intake lifecycle."""

    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)
        # Create standard test fixture agents
        self.tok_lead1 = create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True, max_concurrency=1)
        self.tok_lead2 = create_agent(self.harness, "lead2", adapter="none", delivery="inbox", can_lead=True, max_concurrency=1)
        self.tok_plain = create_agent(self.harness, "plain", adapter="none", delivery="inbox", capabilities=["intake"], max_concurrency=1)
        self.tok_grok = create_agent(self.harness, "grok", adapter="none", delivery="inbox", capabilities=["intake", "relay"], max_concurrency=1)
        self.tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox", can_lead=False, max_concurrency=1)

        # Set default lead to lead1
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)

    def tearDown(self) -> None:
        self.harness.teardown()

    def test_b11_not_implemented_milestones(self) -> None:
        """ACC: B11 (superseded by K1/K9 in M2) - intake submit --repo is built (m2-r3): a bad repo is rc 2 INVALID_ARGUMENT (K1); task merge is built (m2-r5): it is no longer NOT_IMPLEMENTED_IN_M1; an unknown task is rc 5 NOT_FOUND (merge guards: K9, tests/integration/test_merge.py)."""
        # 1. intake submit --repo (built in M2 r3): a path that is not a repo is rc 2 (K1).
        #    A missing dir under this harness home, so no git process runs against anything else.
        bad_repo = os.path.join(self.harness.home, "not-a-repo")
        rc_repo, resp_repo = self.harness.cli(
            "intake", "submit",
            "--title", "Repo proj",
            "--goal", "Goal",
            "--criterion", "c1",
            "--repo", bad_repo,
            "--base-branch", "main",
            "--test-command", "none",
            token=self.tok_grok,
        )
        self.assertEqual(rc_repo, c.EXIT_USAGE)
        self.assertFalse(resp_repo["ok"])
        self.assertEqual(resp_repo["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_repo["error"]["details"]["param"], "repo")
        self.assertEqual(resp_repo["error"]["details"]["repo"], bad_repo)
        rc_list, resp_list = self.harness.cli("project", "list", user=True)
        self.assertEqual(rc_list, 0, resp_list)
        self.assertNotIn("Repo proj", [p.get("name") for p in resp_list["data"]["items"]])

        # 2. task merge (built in M2 r5; B11 superseded by K9): the daemon serves it, so an
        #    unknown task is rc 5 NOT_FOUND (the first merge guard), not a milestone stub.
        rc_merge, resp_merge = self.harness.cli(
            "task", "merge", "tsk_fake123",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_merge, c.EXIT_NOT_FOUND, resp_merge)
        self.assertFalse(resp_merge["ok"])
        self.assertEqual(resp_merge["error"]["code"], c.NOT_FOUND)
        self.assertNotIn("milestone", resp_merge["error"].get("details", {}))

    def test_e1_intake_default_lead(self) -> None:
        """ACC: E1 - Intake submit without --lead defaults to lead1, created_via token, origin plain, outbox project.created."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E1",
            "--goal", "Goal E1",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(resp["ok"])
        prj_id = resp["data"]["id"]
        self.assertEqual(resp["data"]["lead"], "lead1")
        self.assertEqual(resp["data"]["created_via"], "token")
        self.assertEqual(resp["data"]["origin"], "plain")

        # Check project leads <prj>
        rc_leads, leads_resp = self.harness.cli("project", "leads", prj_id, user=True)
        self.assertEqual(rc_leads, 0)
        self.assertEqual(leads_resp["data"]["current_lead"], "lead1")
        self.assertEqual(len(leads_resp["data"]["items"]), 1)
        self.assertIsNone(leads_resp["data"]["items"][0]["ended_at"])

        # Check lead1 inbox has one project.created
        rc_inbox, inbox_resp = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_inbox, 0)
        items = inbox_resp["data"]["items"]
        created_items = [it for it in items if it["type"] == "project.created" and it["project_id"] == prj_id]
        self.assertEqual(len(created_items), 1)

    def test_e2_intake_override_lead(self) -> None:
        """ACC: E2 - Intake submit with --lead lead2 designates lead2 as current lead."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E2",
            "--goal", "Goal E2",
            "--criterion", "c1",
            "--lead", "lead2",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(resp["ok"])
        prj_id = resp["data"]["id"]
        self.assertEqual(resp["data"]["lead"], "lead2")

        rc_leads, leads_resp = self.harness.cli("project", "leads", prj_id, user=True)
        self.assertEqual(rc_leads, 0)
        self.assertEqual(leads_resp["data"]["current_lead"], "lead2")

    def test_e3_no_lead_error(self) -> None:
        """ACC: E3 - When lead.default_agent is null and --lead is omitted, rc 2 NO_LEAD; no project created."""
        # Set lead.default_agent to null
        rc_cfg, _ = self.harness.cli("config", "set", "lead.default_agent", "null", user=True)
        self.assertEqual(rc_cfg, 0)

        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E3",
            "--goal", "Goal E3",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.NO_LEAD)

        # Verify no project row
        conn = self.harness.db()
        cur = conn.execute("SELECT COUNT(*) as cnt FROM projects WHERE name = 'Proj E3';")
        self.assertEqual(cur.fetchone()["cnt"], 0)
        conn.close()

    def test_e4_lead_not_eligible(self) -> None:
        """ACC: E4 - Submit with --lead ibx (not can_lead) and with a disabled lead -> rc 2 LEAD_NOT_ELIGIBLE."""
        def n_projects() -> int:
            conn = self.harness.db()
            try:
                return conn.execute("SELECT COUNT(*) AS n FROM projects;").fetchone()["n"]
            finally:
                conn.close()

        projects_before = n_projects()
        # 1. ibx agent is not can_lead
        rc_ibx, resp_ibx = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E4",
            "--goal", "Goal E4",
            "--criterion", "c1",
            "--lead", "ibx",
            token=self.tok_plain,
        )
        self.assertEqual(rc_ibx, c.EXIT_USAGE)
        self.assertFalse(resp_ibx["ok"])
        self.assertEqual(resp_ibx["error"]["code"], c.LEAD_NOT_ELIGIBLE)
        self.assertEqual(resp_ibx["error"]["details"]["reason"], "not_can_lead")

        # 2. Disabled lead agent
        rc_dis, _ = self.harness.cli("agent", "disable", "lead2", user=True)
        self.assertEqual(rc_dis, 0)

        rc_dis_lead, resp_dis_lead = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E4 Dis",
            "--goal", "Goal E4 Dis",
            "--criterion", "c1",
            "--lead", "lead2",
            token=self.tok_plain,
        )
        self.assertEqual(rc_dis_lead, c.EXIT_USAGE)
        self.assertFalse(resp_dis_lead["ok"])
        self.assertEqual(resp_dis_lead["error"]["code"], c.LEAD_NOT_ELIGIBLE)
        self.assertEqual(resp_dis_lead["error"]["details"]["reason"], "disabled")
        # No project row from either rejected submit.
        self.assertEqual(n_projects(), projects_before)

    def test_e5_config_set_lead_default_agent_auth(self) -> None:
        """ACC: E5 - config set lead.default_agent requires user factor (rc 4 for agent, rc 0 for user)."""
        rc_agent, resp_agent = self.harness.cli(
            "config", "set", "lead.default_agent", '"lead2"',
            token=self.tok_lead1,
        )
        self.assertEqual(rc_agent, c.EXIT_FORBIDDEN)
        self.assertFalse(resp_agent["ok"])
        self.assertEqual(resp_agent["error"]["code"], c.CAPABILITY_MISSING)

        rc_user, resp_user = self.harness.cli(
            "config", "set", "lead.default_agent", '"lead2"',
            user=True,
        )
        self.assertEqual(rc_user, 0)

        rc_get, resp_get = self.harness.cli("config", "get", "lead.default_agent", user=True)
        self.assertEqual(rc_get, 0)
        self.assertEqual(resp_get["data"]["value"], "lead2")

    def test_e6_intake_submit_auth(self) -> None:
        """ACC: E6 - Intake by [U], by [@grok], by [@plain] succeed; agent without intake capability gets rc 4."""
        # 1. Intake by [U]
        rc_u, resp_u = self.harness.cli(
            "intake", "submit",
            "--title", "Proj U",
            "--goal", "Goal U",
            "--criterion", "c1",
            user=True,
        )
        self.assertEqual(rc_u, 0)

        # 2. Intake by grok
        rc_grok, resp_grok = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Grok",
            "--goal", "Goal Grok",
            "--criterion", "c1",
            token=self.tok_grok,
        )
        self.assertEqual(rc_grok, 0)

        # 3. Intake by plain
        rc_plain, resp_plain = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Plain",
            "--goal", "Goal Plain",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc_plain, 0)

        # 4. Agent without intake capability
        rc_create_no, _ = self.harness.cli(
            "agent", "create",
            "--input", "-",
            stdin=json.dumps({
                "name": "no_intake",
                "adapter": "none",
                "delivery": "inbox",
                "max_concurrency": 1,
                "capabilities": [],
            }),
            user=True,
        )
        self.assertEqual(rc_create_no, 0)
        rc_tok_no, tok_no_resp = self.harness.cli("agent", "token", "issue", "no_intake", user=True)
        tok_no_intake = tok_no_resp["data"]["token"]

        rc_no_intake, resp_no_intake = self.harness.cli(
            "intake", "submit",
            "--title", "Proj No Intake",
            "--goal", "Goal",
            "--criterion", "c1",
            token=tok_no_intake,
        )
        self.assertEqual(rc_no_intake, c.EXIT_FORBIDDEN)
        self.assertFalse(resp_no_intake["ok"])
        self.assertEqual(resp_no_intake["error"]["code"], c.CAPABILITY_MISSING)

    def test_e7_intake_follow_up_message(self) -> None:
        """ACC: E7 - Follow-up message on existing project delivers project.request_received to lead inbox."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E7",
            "--goal", "Goal E7",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        prj_id = resp["data"]["id"]

        # Follow-up message by grok
        rc_msg, resp_msg = self.harness.cli(
            "intake", "submit",
            "--project", prj_id,
            "--message", "also add X",
            token=self.tok_grok,
        )
        self.assertEqual(rc_msg, 0)
        self.assertTrue(resp_msg["ok"])

        # Current lead (lead1) inbox has project.request_received
        rc_inbox, inbox_resp = self.harness.cli("inbox", "list", token=self.tok_lead1)
        self.assertEqual(rc_inbox, 0)
        req_msgs = [m for m in inbox_resp["data"]["items"] if m["type"] == "project.request_received" and m["project_id"] == prj_id]
        self.assertEqual(len(req_msgs), 1)
        self.assertEqual(req_msgs[0]["payload"]["message"], "also add X")

    def test_e10_project_set_lead(self) -> None:
        """ACC: E10 - project set-lead updates history, cancels old lead's pending approval, sends project.handover."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E10",
            "--goal", "Goal E10",
            "--criterion", "c1",
            "--criterion", "c2",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        prj_id = resp["data"]["id"]

        # Insert a task and an approval requested by lead1 directly into DB for test setup
        conn = self.harness.db()
        cur = conn.cursor()
        lead1_id = cur.execute("SELECT id FROM principals WHERE name = 'lead1';").fetchone()["id"]
        conn.close()

        # Connect with writable sqlite connection to seed task and approval for E10 scenario
        import sqlite3
        from mesh.core.events import record_event
        wconn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        wconn.row_factory = sqlite3.Row
        wconn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, created_at, updated_at)
               VALUES ('tsk_e10_1', ?, 'Task 1', 'Spec', 'general', 'assigned', 'edit', 3600, 3, 'lead1', 'now', 'now');""",
            (prj_id,),
        )
        record_event(
            wconn,
            type_="task.assigned",
            actor_id="lead1",
            actor_kind="agent",
            via="token",
            project_id=prj_id,
            task_id="tsk_e10_1",
            to_state="assigned",
        )
        wconn.execute(
            """INSERT INTO approvals (id, project_id, category, op_type, op_json, op_hash, summary, requested_by, requested_at, expires_at, status)
               VALUES ('apr_e10_1', ?, 'project_control', 'project.pause', '{}', 'sha256:fake', 'Pause', ?, 'now', '2099-01-01T00:00:00Z', 'pending');""",
            (prj_id, lead1_id),
        )
        wconn.commit()
        wconn.close()

        crit_ids = [cr["id"] for cr in resp["data"]["criteria"]]
        self.assertEqual(len(crit_ids), 2)
        rconn = self.harness.db()
        seqs_before = [r["seq"] for r in rconn.execute(
            "SELECT seq FROM events WHERE project_id = ? ORDER BY seq;", (prj_id,)).fetchall()]
        rconn.close()
        self.assertGreaterEqual(len(seqs_before), 2)  # project.created ... + the seeded task.assigned

        # Set lead to lead2 by user
        rc_set, resp_set = self.harness.cli(
            "project", "set-lead", prj_id, "lead2",
            "--reason", "Handover to lead2",
            user=True,
        )
        self.assertEqual(rc_set, 0)
        self.assertTrue(resp_set["ok"])

        # project leads shows lead1 ended with reason, lead2 current
        rc_leads, resp_leads = self.harness.cli("project", "leads", prj_id, user=True)
        self.assertEqual(rc_leads, 0)
        self.assertEqual(resp_leads["data"]["current_lead"], "lead2")
        items = resp_leads["data"]["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["agent"], "lead1")
        self.assertIsNotNone(items[0]["ended_at"])
        self.assertEqual(items[0]["ended_reason"], "Handover to lead2")
        self.assertEqual(items[1]["agent"], "lead2")
        self.assertIsNone(items[1]["ended_at"])

        # Verify old lead's approval was cancelled
        rconn = self.harness.db()
        apr_row = rconn.execute("SELECT status FROM approvals WHERE id = 'apr_e10_1';").fetchone()
        self.assertEqual(apr_row["status"], "cancelled")
        rconn.close()

        # Check event list --project $PRJ
        rc_ev, resp_ev = self.harness.cli("event", "list", "--project", prj_id, token=self.tok_lead2)
        self.assertEqual(rc_ev, 0)
        ev_types = [e["type"] for e in resp_ev["data"]["items"]]
        self.assertIn("project.created", ev_types)
        self.assertIn("project.lead_changed", ev_types)
        # Every earlier event is still listed, including the seeded task.assigned.
        listed_seqs = {e["seq"] for e in resp_ev["data"]["items"]}
        self.assertTrue(set(seqs_before) <= listed_seqs, (seqs_before, sorted(listed_seqs)))
        self.assertTrue(any(e["type"] == "task.assigned" and e.get("task_id") == "tsk_e10_1"
                            for e in resp_ev["data"]["items"]))
        # Every earlier task is still listed.
        rc_tl, resp_tl = self.harness.cli("task", "list", "--project", prj_id, token=self.tok_lead2)
        self.assertEqual(rc_tl, 0, resp_tl)
        self.assertIn("tsk_e10_1", [t["id"] for t in resp_tl["data"]["items"]])

        # Check lead2 inbox has project.handover
        rc_inbox, resp_inbox = self.harness.cli("inbox", "list", token=self.tok_lead2)
        self.assertEqual(rc_inbox, 0)
        handover_msgs = [m for m in resp_inbox["data"]["items"] if m["type"] == "project.handover" and m["project_id"] == prj_id]
        self.assertEqual(len(handover_msgs), 1)
        pld = handover_msgs[0]["payload"]
        self.assertEqual(pld["goal"], "Goal E10")
        self.assertEqual(len(pld["criteria"]), 2)
        self.assertEqual(sorted(cr["id"] for cr in pld["criteria"]), sorted(crit_ids))
        self.assertEqual(len(pld["tasks"]), 1)
        self.assertEqual(pld["tasks"][0]["id"], "tsk_e10_1")
        self.assertEqual(pld["tasks"][0]["status"], "assigned")

    def test_e12_project_set_lead_relayed(self) -> None:
        """ACC: E12 - project set-lead via --relayed by grok succeeds (recorded relayed); by plain fails RELAY_NOT_ALLOWED."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E12",
            "--goal", "Goal E12",
            "--criterion", "c1",
            "--lead", "lead2",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        prj_id = resp["data"]["id"]

        # plain has no relay capability -> RELAY_NOT_ALLOWED
        rc_plain, resp_plain = self.harness.cli(
            "project", "set-lead", prj_id, "lead1",
            "--reason", "Switch back",
            "--relayed",
            "--user-statement", "James: switch back",
            token=self.tok_plain,
        )
        self.assertEqual(rc_plain, c.EXIT_FORBIDDEN)
        self.assertFalse(resp_plain["ok"])
        self.assertEqual(resp_plain["error"]["code"], c.RELAY_NOT_ALLOWED)

        # grok has relay capability -> succeeds
        rc_grok, resp_grok = self.harness.cli(
            "project", "set-lead", prj_id, "lead1",
            "--reason", "Switch back",
            "--relayed",
            "--user-statement", "James: switch back",
            token=self.tok_grok,
        )
        self.assertEqual(rc_grok, 0)
        self.assertTrue(resp_grok["ok"])

        # Check event recorded via relayed and relayed_by grok
        rconn = self.harness.db()
        ev_row = rconn.execute(
            "SELECT via, relayed_by FROM events WHERE project_id = ? AND type = 'project.lead_changed';",
            (prj_id,),
        ).fetchone()
        self.assertEqual(ev_row["via"], "relayed")
        self.assertEqual(ev_row["relayed_by"], "grok")
        rconn.close()

    def test_e13_project_set_lead_assignee_guard(self) -> None:
        """ACC: E13 - set-lead to an agent with non-terminal tasks returns rc 6 LEAD_HAS_TASKS."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E13",
            "--goal", "Goal E13",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        prj_id = resp["data"]["id"]

        # Insert a task assigned to lead2
        import sqlite3
        from mesh.core.events import record_event
        wconn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        wconn.row_factory = sqlite3.Row
        lead2_id = wconn.execute("SELECT id FROM principals WHERE name = 'lead2';").fetchone()[0]
        wconn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, assignee_agent_id, created_by, created_at, updated_at)
               VALUES ('tsk_e13_1', ?, 'Task E13', 'Spec', 'general', 'assigned', 'edit', 3600, 3, ?, 'lead1', 'now', 'now');""",
            (prj_id, lead2_id),
        )
        record_event(
            wconn,
            type_="task.assigned",
            actor_id="lead1",
            actor_kind="agent",
            via="token",
            project_id=prj_id,
            task_id="tsk_e13_1",
            to_state="assigned",
        )
        wconn.commit()
        wconn.close()

        # Attempt to set lead to lead2
        rc_set, resp_set = self.harness.cli(
            "project", "set-lead", prj_id, "lead2",
            "--reason", "Promote assignee",
            user=True,
        )
        self.assertEqual(rc_set, c.EXIT_CONFLICT)
        self.assertFalse(resp_set["ok"])
        self.assertEqual(resp_set["error"]["code"], c.LEAD_HAS_TASKS)
        self.assertIn("tsk_e13_1", resp_set["error"]["details"]["tasks"])

        # Verify lead is unchanged (still lead1)
        rc_leads, resp_leads = self.harness.cli("project", "leads", prj_id, user=True)
        self.assertEqual(rc_leads, 0)
        self.assertEqual(resp_leads["data"]["current_lead"], "lead1")

    def test_e14_project_complete_criteria_validation(self) -> None:
        """ACC: E14 - project complete with report that (a) omits c2, (b) met:false, (c) empty evidence -> rc 2 CRITERIA_REPORT_INCOMPLETE."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E14",
            "--goal", "Goal E14",
            "--criterion", "c1",
            "--criterion", "c2",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        prj_id = resp["data"]["id"]
        c1_id = resp["data"]["criteria"][0]["id"]
        c2_id = resp["data"]["criteria"][1]["id"]

        # Case (a): omit criterion c2
        report_a = json.dumps({
            "criteria": [{"id": c1_id, "met": True, "evidence": "Tested ok"}],
            "summary": "Completed",
        })
        rc_a, resp_a = self.harness.cli(
            "project", "complete", prj_id,
            "--report", "-",
            stdin=report_a,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_a, c.EXIT_USAGE)
        self.assertFalse(resp_a["ok"])
        self.assertEqual(resp_a["error"]["code"], c.CRITERIA_REPORT_INCOMPLETE)

        # Case (b): met is False
        report_b = json.dumps({
            "criteria": [
                {"id": c1_id, "met": True, "evidence": "Tested ok"},
                {"id": c2_id, "met": False, "evidence": "Not done"},
            ],
            "summary": "Completed",
        })
        rc_b, resp_b = self.harness.cli(
            "project", "complete", prj_id,
            "--report", "-",
            stdin=report_b,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_b, c.EXIT_USAGE)
        self.assertFalse(resp_b["ok"])
        self.assertEqual(resp_b["error"]["code"], c.CRITERIA_REPORT_INCOMPLETE)

        # Case (c): empty evidence
        report_c = json.dumps({
            "criteria": [
                {"id": c1_id, "met": True, "evidence": "Tested ok"},
                {"id": c2_id, "met": True, "evidence": "   "},
            ],
            "summary": "Completed",
        })
        rc_c, resp_c = self.harness.cli(
            "project", "complete", prj_id,
            "--report", "-",
            stdin=report_c,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_c, c.EXIT_USAGE)
        self.assertFalse(resp_c["ok"])
        self.assertEqual(resp_c["error"]["code"], c.CRITERIA_REPORT_INCOMPLETE)

        # Verify project is still active
        rc_show, resp_show = self.harness.cli("project", "show", prj_id, user=True)
        self.assertEqual(rc_show, 0)
        self.assertEqual(resp_show["data"]["status"], "active")

    def test_e18_project_terminal_no_reopen(self) -> None:
        """ACC: E18 - project complete on completed or cancelled project returns rc 6 PROJECT_NOT_ACTIVE."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj E18",
            "--goal", "Goal E18",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        prj_id = resp["data"]["id"]
        c1_id = resp["data"]["criteria"][0]["id"]

        # Complete project successfully with valid report
        valid_report = json.dumps({
            "criteria": [{"id": c1_id, "met": True, "evidence": "All tests green"}],
            "summary": "Everything finished",
        })
        rc_comp, resp_comp = self.harness.cli(
            "project", "complete", prj_id,
            "--report", "-",
            stdin=valid_report,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_comp, 0)
        self.assertEqual(resp_comp["data"]["status"], "completed")

        # Second complete call returns PROJECT_NOT_ACTIVE
        rc_comp2, resp_comp2 = self.harness.cli(
            "project", "complete", prj_id,
            "--report", "-",
            stdin=valid_report,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_comp2, c.EXIT_CONFLICT)
        self.assertFalse(resp_comp2["ok"])
        self.assertEqual(resp_comp2["error"]["code"], c.PROJECT_NOT_ACTIVE)

        # On cancelled project
        rc_c2, resp_c2 = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Cancelled",
            "--goal", "Goal",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        c2_id = resp_c2["data"]["id"]
        self.harness.cli("project", "cancel", c2_id, user=True)

        rc_c_comp, resp_c_comp = self.harness.cli(
            "project", "complete", c2_id,
            "--report", "-",
            stdin=valid_report,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_c_comp, c.EXIT_CONFLICT)
        self.assertFalse(resp_c_comp["ok"])
        self.assertEqual(resp_c_comp["error"]["code"], c.PROJECT_NOT_ACTIVE)

        # task create on completed project returns PROJECT_NOT_ACTIVE
        rc_tc_comp, resp_tc_comp = self.harness.cli(
            "task", "create",
            "--project", prj_id,
            "--title", "Task on completed project",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_tc_comp, c.EXIT_CONFLICT)
        self.assertFalse(resp_tc_comp["ok"])
        self.assertEqual(resp_tc_comp["error"]["code"], c.PROJECT_NOT_ACTIVE)

        # task create on cancelled project returns PROJECT_NOT_ACTIVE
        rc_tc_canc, resp_tc_canc = self.harness.cli(
            "task", "create",
            "--project", c2_id,
            "--title", "Task on cancelled project",
            "--spec", "Spec",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_tc_canc, c.EXIT_CONFLICT)
        self.assertFalse(resp_tc_canc["ok"])
        self.assertEqual(resp_tc_canc["error"]["code"], c.PROJECT_NOT_ACTIVE)

    def test_b12_project_pause_approval_and_idempotency(self) -> None:
        """ACC: B12 - m project pause $PRJ --idempotency-key k7 [@lead1]; user approves; re-run with --approval; replay; reused key."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj B12",
            "--goal", "Goal B12",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        prj_id = resp["data"]["id"]

        conn = self.harness.db()
        idemp_before = conn.execute("SELECT COUNT(*) as cnt FROM idempotency;").fetchone()["cnt"]
        conn.close()

        # 1. Lead calls project pause without approval -> rc 7 APPROVAL_REQUIRED, idempotency count unchanged
        rc_pause, resp_pause = self.harness.cli(
            "project", "pause", prj_id,
            "--idempotency-key", "k7",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_pause, c.EXIT_APPROVAL_REQUIRED)
        self.assertFalse(resp_pause["ok"])
        self.assertEqual(resp_pause["error"]["code"], c.APPROVAL_REQUIRED)
        apr_id = resp_pause["error"]["details"]["approval_id"]
        op_hash = resp_pause["error"]["details"]["op_hash"]
        self.assertIsNotNone(apr_id)

        conn = self.harness.db()
        idemp_after_gated = conn.execute("SELECT COUNT(*) as cnt FROM idempotency;").fetchone()["cnt"]
        self.assertEqual(idemp_before, idemp_after_gated)
        conn.close()

        # 2. User approves
        rc_appr, resp_appr = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", op_hash,
            user=True,
        )
        self.assertEqual(rc_appr, 0)
        self.assertTrue(resp_appr["ok"])

        # 3. Lead re-runs identical command with --approval $APR -> rc 0, project paused
        rc_run, resp_run = self.harness.cli(
            "project", "pause", prj_id,
            "--idempotency-key", "k7",
            "--approval", apr_id,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_run, 0)
        self.assertTrue(resp_run["ok"])
        self.assertEqual(resp_run["data"]["status"], "paused")

        # 4. Repeat that exact call -> rc 0 with idempotent_replay: true
        rc_replay, resp_replay = self.harness.cli(
            "project", "pause", prj_id,
            "--idempotency-key", "k7",
            "--approval", apr_id,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_replay, 0)
        self.assertTrue(resp_replay["ok"])
        self.assertTrue(resp_replay.get("idempotent_replay", False))

        # 5. Key k7 without --approval -> rc 6 IDEMPOTENCY_KEY_REUSED
        rc_reused, resp_reused = self.harness.cli(
            "project", "pause", prj_id,
            "--idempotency-key", "k7",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_reused, c.EXIT_CONFLICT)
        self.assertFalse(resp_reused["ok"])
        self.assertEqual(resp_reused["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)

    def test_project_lifecycle_and_notes(self) -> None:
        """Integration test for project note, pause, resume, cancel."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Life",
            "--goal", "Goal Life",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        prj_id = resp["data"]["id"]

        # Note by lead
        rc_note, resp_note = self.harness.cli(
            "project", "note", prj_id,
            "--text", "Design review complete",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_note, 0)
        self.assertTrue(resp_note["ok"])

        # Pause by user
        rc_pause, resp_pause = self.harness.cli("project", "pause", prj_id, user=True)
        self.assertEqual(rc_pause, 0)
        self.assertEqual(resp_pause["data"]["status"], "paused")

        # Resume by user
        rc_resume, resp_resume = self.harness.cli("project", "resume", prj_id, user=True)
        self.assertEqual(rc_resume, 0)
        self.assertEqual(resp_resume["data"]["status"], "active")

        # Cancel by user
        rc_cancel, resp_cancel = self.harness.cli("project", "cancel", prj_id, user=True)
        self.assertEqual(rc_cancel, 0)
        self.assertEqual(resp_cancel["data"]["status"], "cancelled")

    @serial
    def test_project_wait_terminal_and_timeout(self) -> None:
        """Verify project wait terminal condition, wake on cancel, and timeout with rc 8."""
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "Proj Wait Test",
            "--goal", "Goal Wait",
            "--criterion", "c1",
            token=self.tok_plain,
        )
        self.assertEqual(rc, 0)
        prj_id = resp["data"]["id"]

        # Missing --timeout on project wait -> rc 2 INVALID_ARGUMENT (param: timeout)
        rc_no_to, resp_no_to = self.harness.cli("project", "wait", prj_id, "--until", "completed", user=True)
        self.assertEqual(rc_no_to, c.EXIT_USAGE)
        self.assertEqual(resp_no_to.get("error", {}).get("code"), c.INVALID_ARGUMENT)
        self.assertEqual(resp_no_to.get("error", {}).get("details", {}).get("param"), "timeout")

        # Dry-run rejected on project wait -> rc 2 INVALID_ARGUMENT (param: dry_run)
        rc_dry, resp_dry = self.harness.cli("project", "wait", prj_id, "--until", "completed", "--timeout", "1s", "--dry-run", user=True)
        self.assertEqual(rc_dry, c.EXIT_USAGE)
        self.assertEqual(resp_dry.get("error", {}).get("code"), c.INVALID_ARGUMENT)
        self.assertEqual(resp_dry.get("error", {}).get("details", {}).get("param"), "dry_run")

        # 1. Timeout waiting for completed (active project) -> rc 8 WAIT_TIMEOUT
        # 1 s (was 2 s, m3-a1-speed): any expired timeout on an active project proves rc 8.
        rc_w1, resp_w1 = self.harness.cli("project", "wait", prj_id, "--until", "completed", "--timeout", "1s", user=True)
        self.assertEqual(rc_w1, c.EXIT_WAIT_TIMEOUT)
        self.assertEqual(resp_w1["error"]["code"], c.WAIT_TIMEOUT)

        # 2. Long-poll wait wakes promptly on terminal condition (cancel)
        def run_wait() -> Any:
            return self.harness.cli("project", "wait", prj_id, "--until", "terminal", "--timeout", "10s", user=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut = executor.submit(run_wait)
            # Give the waiter a moment to connect and enter the long-poll (project waits are
            # not counted in daemon.status active_waits, so poll the wall clock here).
            time.sleep(0.3)

            cancel_start = time.monotonic()
            rc_cn, _ = self.harness.cli("project", "cancel", prj_id, user=True)
            self.assertEqual(rc_cn, 0)

            rc_fut, resp_fut = fut.result(timeout=5.0)
            wait_dur = time.monotonic() - cancel_start
            self.assertEqual(rc_fut, 0)
            self.assertLess(wait_dur, 2.0, f"Project wait took too long to wake: {wait_dur}s")
            self.assertEqual(resp_fut["data"]["status"], "cancelled")

        # 3. Already terminal project returns immediately rc 0
        rc_w2, resp_w2 = self.harness.cli("project", "wait", prj_id, "--until", "terminal", "--timeout", "2s", user=True)
        self.assertEqual(rc_w2, 0)
        self.assertEqual(resp_w2["data"]["status"], "cancelled")

    def test_project_update_scope_change_gating(self) -> None:
        """Sub-step 3b: project update PRJ [--goal] [--criterion]... gated by scope_change."""
        rc_init, resp_init = self.harness.cli(
            "intake", "submit",
            "--title", "Scope Project",
            "--goal", "Initial Goal",
            "--criterion", "Initial Criterion",
            token=self.tok_grok,
        )
        self.assertEqual(rc_init, 0)
        project_id = resp_init["data"]["id"]

        # 1. Lead attempts project update -> rc 7 APPROVAL_REQUIRED (scope_change)
        rc_lead, resp_lead = self.harness.cli(
            "project", "update", project_id,
            "--goal", "Lead Revised Goal",
            "--criterion", "Additional Criterion A",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_lead, c.EXIT_APPROVAL_REQUIRED)
        self.assertFalse(resp_lead["ok"])
        self.assertEqual(resp_lead["error"]["code"], c.APPROVAL_REQUIRED)
        self.assertEqual(resp_lead["error"]["details"]["category"], "scope_change")

        apr_id = resp_lead["error"]["details"]["approval_id"]
        op_hash = resp_lead["error"]["details"]["op_hash"]

        # Verify DB is unchanged
        conn = self.harness.db()
        prow = conn.execute("SELECT goal FROM projects WHERE id = ?;", (project_id,)).fetchone()
        self.assertNotEqual(prow["goal"], "Lead Revised Goal")
        ac_cnt = conn.execute("SELECT COUNT(*) as c FROM acceptance_criteria WHERE project_id = ?;", (project_id,)).fetchone()["c"]
        conn.close()

        # 2. User approves
        rc_app, resp_app = self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc_app, 0)
        self.assertEqual(resp_app["data"]["status"], "approved")

        # 3. Lead re-runs with --approval -> rc 0, goal and criteria updated
        rc_re, resp_re = self.harness.cli(
            "project", "update", project_id,
            "--goal", "Lead Revised Goal",
            "--criterion", "Additional Criterion A",
            "--approval", apr_id,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_re, 0)
        self.assertTrue(resp_re["ok"])
        self.assertEqual(resp_re["data"]["goal"], "Lead Revised Goal")

        conn = self.harness.db()
        prow2 = conn.execute("SELECT goal FROM projects WHERE id = ?;", (project_id,)).fetchone()
        self.assertEqual(prow2["goal"], "Lead Revised Goal")
        ac_cnt2 = conn.execute("SELECT COUNT(*) as c FROM acceptance_criteria WHERE project_id = ?;", (project_id,)).fetchone()["c"]
        self.assertEqual(ac_cnt2, ac_cnt + 1)
        # Verify approval is consumed
        arow = conn.execute("SELECT status, consumed_by FROM approvals WHERE id = ?;", (apr_id,)).fetchone()
        self.assertEqual(arow["status"], "consumed")
        conn.close()

        # 4. Direct user update succeeds without approval
        rc_usr, resp_usr = self.harness.cli(
            "project", "update", project_id,
            "--goal", "Direct User Goal",
            user=True,
        )
        self.assertEqual(rc_usr, 0)
        self.assertEqual(resp_usr["data"]["goal"], "Direct User Goal")

        # 5. Relayer update with --relayed --user-statement succeeds
        rc_rel, resp_rel = self.harness.cli(
            "project", "update", project_id,
            "--goal", "Relayed User Goal",
            "--criterion", "Additional Criterion Relayed",
            "--relayed",
            "--user-statement", "James: update goal and criterion",
            token=self.tok_grok,
        )
        self.assertEqual(rc_rel, 0)
        self.assertEqual(resp_rel["data"]["goal"], "Relayed User Goal")

        # Verify event project.updated
        conn = self.harness.db()
        ev = conn.execute("SELECT * FROM events WHERE type = 'project.updated' ORDER BY seq DESC LIMIT 1;").fetchone()
        conn.close()
        self.assertIsNotNone(ev)
        self.assertEqual(ev["project_id"], project_id)
