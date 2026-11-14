"""Integration tests for Approvals subsystem per plan §7.4 and §16 M1.7.

Checklist items: ACC: G1, G2, G3, G7, G8, G9, G10, G11, G13, G14, G15, G19, G21, G28.
"""

import json
import os
import sqlite3
import shutil
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


class TestApprovalsIntegration(unittest.TestCase):
    """Integration test suite for approval requests, decisions, policies, and audit."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

        # Standard fixture F1 setup
        self.tok_lead1 = create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        self.tok_lead2 = create_agent(self.harness, "lead2", adapter="none", delivery="inbox", can_lead=True)
        self.tok_fx = create_agent(self.harness, "fx", adapter="fake", delivery="launch", max_concurrency=2)
        self.tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        self.tok_grok = create_agent(self.harness, "grok", adapter="none", delivery="inbox", capabilities=["intake", "relay"])
        self.tok_plain = create_agent(self.harness, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
        self.tok_rlead = create_agent(self.harness, "rlead", adapter="none", delivery="inbox", can_lead=True, capabilities=["intake", "relay"])

        # Default lead configuration
        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)

        # Standard project PRJ via intake by grok
        rc_p, resp_p = self.harness.cli(
            "intake", "submit",
            "--title", "t",
            "--goal", "g",
            "--criterion", "c1",
            "--criterion", "c2",
            token=self.tok_grok,
        )
        self.assertEqual(rc_p, 0)
        self.project_id = resp_p["data"]["id"]

        # M1c run 2 wires launch T5. These M1b tests assert on the queued state of
        # launch tasks, so they hold launch starts (testing hook, plan §9.3).
        self.harness.hold_launch_starts()

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def test_g1_project_pause_approval_gate_and_notifications(self) -> None:
        """ACC: G1 - m project pause $PRJ [@lead1], run twice -> rc 7 APPROVAL_REQUIRED with approval_id, op_hash (sha256:…), category:"project_control", expires_at; the 2nd call returns the same approval_id; user inbox and grok inbox each get one approval.requested; project still active."""
        prj = self.project_id

        # First pause call
        rc1, resp1 = self.harness.cli("project", "pause", prj, token=self.tok_lead1)
        self.assertEqual(rc1, c.EXIT_APPROVAL_REQUIRED)
        self.assertFalse(resp1["ok"])
        self.assertEqual(resp1["error"]["code"], c.APPROVAL_REQUIRED)

        details1 = resp1["error"]["details"]
        apr_id1 = details1.get("approval_id")
        op_hash1 = details1.get("op_hash")
        cat1 = details1.get("category")
        exp1 = details1.get("expires_at")

        self.assertIsNotNone(apr_id1)
        self.assertTrue(op_hash1.startswith("sha256:"))
        self.assertEqual(cat1, "project_control")
        self.assertIsNotNone(exp1)

        # Second pause call (reuse open approval)
        rc2, resp2 = self.harness.cli("project", "pause", prj, token=self.tok_lead1)
        self.assertEqual(rc2, c.EXIT_APPROVAL_REQUIRED)
        self.assertFalse(resp2["ok"])
        self.assertEqual(resp2["error"]["code"], c.APPROVAL_REQUIRED)

        details2 = resp2["error"]["details"]
        apr_id2 = details2.get("approval_id")
        op_hash2 = details2.get("op_hash")

        self.assertEqual(apr_id1, apr_id2)
        self.assertEqual(op_hash1, op_hash2)

        # Project still active
        rc_p, resp_p = self.harness.cli("project", "show", prj, user=True)
        self.assertEqual(rc_p, 0)
        self.assertEqual(resp_p["data"]["status"], "active")

        # Check inboxes: user inbox has exactly 1 approval.requested
        rc_ui, resp_ui = self.harness.cli("inbox", "list", user=True)
        self.assertEqual(rc_ui, 0)
        u_msgs = [m for m in resp_ui["data"]["items"] if m.get("type") == "approval.requested"]
        self.assertEqual(len(u_msgs), 1)
        self.assertEqual(u_msgs[0]["payload"]["approval_id"], apr_id1)

        # grok (origin agent) inbox has exactly 1 approval.requested
        rc_gi, resp_gi = self.harness.cli("inbox", "list", token=self.tok_grok)
        self.assertEqual(rc_gi, 0)
        g_msgs = [m for m in resp_gi["data"]["items"] if m.get("type") == "approval.requested"]
        self.assertEqual(len(g_msgs), 1)
        self.assertEqual(g_msgs[0]["payload"]["approval_id"], apr_id1)

    def test_approval_requested_event_has_matching_request_id(self) -> None:
        """Review 7b N2: approval.requested event has request_id matching response request_id."""
        rc, resp = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED)
        req_id = resp.get("request_id")
        self.assertIsNotNone(req_id)
        apr_id = resp["error"]["details"]["approval_id"]

        rc_ev, resp_ev = self.harness.cli("event", "list", "--type", "approval.requested", user=True)
        self.assertEqual(rc_ev, 0)
        matching = [e for e in resp_ev["data"]["items"] if e["payload"].get("approval_id") == apr_id]
        self.assertTrue(len(matching) > 0)
        self.assertEqual(matching[0]["request_id"], req_id)

    def test_approval_request_lifecycle_and_validation(self) -> None:
        """Test approval request flags, canonical JSON float rejection, duration bounds, and authority."""
        # 1. Float rejection in operation (canonical JSON rule)
        rc, resp = self.harness.cli(
            "approval", "request",
            "--category", "spend_money",
            "--summary", "Invalid float amount",
            "--operation", '{"usd": 10.50}',
            user=True,
        )
        self.assertEqual(rc, 2)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)

        # 2. Unknown category rejection
        rc, resp = self.harness.cli(
            "approval", "request",
            "--category", "nonexistent_category",
            "--summary", "Unknown category",
            "--operation", '{"usd": 10}',
            user=True,
        )
        self.assertEqual(rc, 2)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)

        # Duration bounds rejection (< 1s or > 30d) -> rc 2 INVALID_ARGUMENT
        rc_low, r_low = self.harness.cli(
            "approval", "request",
            "--category", "deploy",
            "--summary", "Low duration",
            "--operation", '{"usd": 10}',
            "--expires-in", "0s",
            user=True,
        )
        self.assertEqual(rc_low, 2)
        self.assertEqual(r_low["error"]["code"], c.INVALID_ARGUMENT)

        rc_high, r_high = self.harness.cli(
            "approval", "request",
            "--category", "deploy",
            "--summary", "High duration",
            "--operation", '{"usd": 10}',
            "--expires-in", "31d",
            user=True,
        )
        self.assertEqual(rc_high, 2)
        self.assertEqual(r_high["error"]["code"], c.INVALID_ARGUMENT)

        # 3. Agent authority: plain agent without relay and not lead of project cannot request
        rc, resp = self.harness.cli(
            "approval", "request",
            "--category", "deploy",
            "--summary", "Deploy by plain",
            "--operation", '{"service": "web"}',
            token=self.tok_plain,
        )
        self.assertEqual(rc, 4)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.CAPABILITY_MISSING)

        # 4. Valid request by grok (relayer agent holding relay cap, without project) -> rc 0
        rc, resp = self.harness.cli(
            "approval", "request",
            "--category", "deploy",
            "--summary", "Deploy v1",
            "--operation", '{"service": "worker", "version": 1}',
            "--expires-in", "1h",
            token=self.tok_grok,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(resp["ok"])
        apr_data = resp["data"]
        self.assertIn("approval_id", apr_data)
        self.assertEqual(apr_data["category"], "deploy")
        self.assertEqual(apr_data["status"], "pending")
        self.assertTrue(apr_data["op_hash"].startswith("sha256:"))

        apr_id = apr_data["approval_id"]

        # 5. approval show displays proper fields
        rc_show, resp_show = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(rc_show, 0)
        show_data = resp_show["data"]
        self.assertEqual(show_data["id"], apr_id)
        self.assertEqual(show_data["status"], "pending")
        self.assertIsNone(show_data["decided_via"])
        self.assertIsNone(show_data["trust"])
        self.assertIsNone(show_data["via"])
        self.assertIn("op", show_data)
        self.assertEqual(show_data["op"]["params"]["service"], "worker")

        # 6. Read scope check: plain agent cannot show grok's approval (not requester, not project member)
        rc_plain_show, resp_plain_show = self.harness.cli("approval", "show", apr_id, token=self.tok_plain)
        self.assertEqual(rc_plain_show, 4)
        self.assertEqual(resp_plain_show["error"]["code"], c.SCOPE_MISMATCH)

        # 7. Relayer can show grok's approval
        rc_grok_show, _ = self.harness.cli("approval", "show", apr_id, token=self.tok_grok)
        self.assertEqual(rc_grok_show, 0)

    def test_g3_and_g2_direct_and_relayed_decisions(self) -> None:
        """ACC: G3 - Relayed approval by grok and direct user approval; show and check trust levels.
        ACC: G2 - Direct user approval verification."""
        # 1. Lead creates a pending pause approval
        rc_p, resp_p = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_p, c.EXIT_APPROVAL_REQUIRED)
        apr_id = resp_p["error"]["details"]["approval_id"]
        op_hash = resp_p["error"]["details"]["op_hash"]

        # 2. Relayed approve by grok (ACC: G3)
        rc_app, resp_app = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", op_hash,
            "--relayed",
            "--user-statement", "James: ok",
            "--evidence-ref", "msg-1",
            token=self.tok_grok,
        )
        self.assertEqual(rc_app, 0)
        self.assertEqual(resp_app["data"]["status"], "approved")
        self.assertEqual(resp_app["data"]["decided_via"], "relayed")
        self.assertEqual(resp_app["data"]["trust"], "relayed_unverified")
        self.assertIsNone(resp_app["data"]["user_mac"])

        # 3. User verifies approval show (ACC: G3)
        rc_show, resp_show = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(rc_show, 0)
        show_data = resp_show["data"]
        self.assertEqual(show_data["via"], "relayed (unverified; by grok)")
        self.assertEqual(show_data["trust"], "relayed_unverified")
        self.assertEqual(show_data["user_statement"], "James: ok")
        self.assertEqual(show_data["evidence_ref"], "msg-1")
        self.assertIsNotNone(show_data["relayer_credential_id"])
        self.assertIsNone(show_data["user_mac"])

        # 4. Lead runs approval check (ACC: G3)
        rc_chk, resp_chk = self.harness.cli(
            "approval", "check", apr_id,
            "--op-hash", op_hash,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_chk, 0)
        chk_data = resp_chk["data"]
        self.assertEqual(chk_data["decided_via"], "relayed")
        self.assertEqual(chk_data["trust"], "relayed_unverified")
        self.assertEqual(chk_data["status"], "approved")

        # 5. Direct user approval on a separate approval (ACC: G2, G3 direct verification clause)
        rc_req2, resp_req2 = self.harness.cli(
            "approval", "request",
            "--category", "deploy",
            "--summary", "Deploy prod",
            "--operation", '{"target": "cluster"}',
            token=self.tok_grok,
        )
        self.assertEqual(rc_req2, 0)
        apr2 = resp_req2["data"]["approval_id"]
        hash2 = resp_req2["data"]["op_hash"]

        rc_dir, resp_dir = self.harness.cli(
            "approval", "approve", apr2,
            "--op-hash", hash2,
            user=True,
        )
        self.assertEqual(rc_dir, 0)
        self.assertEqual(resp_dir["data"]["status"], "approved")
        self.assertEqual(resp_dir["data"]["decided_via"], "direct")
        self.assertEqual(resp_dir["data"]["trust"], "direct_verified")
        self.assertIsNotNone(resp_dir["data"]["user_mac"])

        rc_show2, resp_show2 = self.harness.cli("approval", "show", apr2, user=True)
        self.assertEqual(rc_show2, 0)
        self.assertEqual(resp_show2["data"]["via"], "direct")
        self.assertEqual(resp_show2["data"]["trust"], "direct_verified")
        self.assertIsNotNone(resp_show2["data"]["user_mac"])

    def test_g7_g8_agent_decision_negatives(self) -> None:
        """ACC: G7 - Agent caller without relay capability -> rc 4 RELAY_NOT_ALLOWED.
        ACC: G8 - Lead caller without --relayed -> rc 4 CAPABILITY_MISSING."""
        rc_p, resp_p = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_p, c.EXIT_APPROVAL_REQUIRED)
        apr_id = resp_p["error"]["details"]["approval_id"]
        op_hash = resp_p["error"]["details"]["op_hash"]

        # ACC: G7 - plain agent with --relayed (lacks relay cap) -> rc 4 RELAY_NOT_ALLOWED; still pending
        rc_plain, resp_plain = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", op_hash,
            "--relayed",
            "--user-statement", "x",
            token=self.tok_plain,
        )
        self.assertEqual(rc_plain, 4)
        self.assertEqual(resp_plain["error"]["code"], c.RELAY_NOT_ALLOWED)

        rc_s1, resp_s1 = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(resp_s1["data"]["status"], "pending")

        # ACC: G7 - ibx agent with --relayed -> rc 4 RELAY_NOT_ALLOWED; still pending
        rc_ibx, resp_ibx = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", op_hash,
            "--relayed",
            "--user-statement", "x",
            token=self.tok_ibx,
        )
        self.assertEqual(rc_ibx, 4)
        self.assertEqual(resp_ibx["error"]["code"], c.RELAY_NOT_ALLOWED)

        rc_s2, resp_s2 = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(resp_s2["data"]["status"], "pending")

        # ACC: G8 - lead1 without --relayed -> rc 4 CAPABILITY_MISSING (plan §7.3 check order); still pending
        rc_lead, resp_lead = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", op_hash,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_lead, 4)
        self.assertEqual(resp_lead["error"]["code"], c.CAPABILITY_MISSING)

        rc_s3, resp_s3 = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(resp_s3["data"]["status"], "pending")

    def test_g9_op_hash_mismatch(self) -> None:
        """ACC: G9 - Approve with a wrong --op-hash (direct and relayed) -> rc 12 OP_HASH_MISMATCH; still pending."""
        rc_p, resp_p = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_p, c.EXIT_APPROVAL_REQUIRED)
        apr_id = resp_p["error"]["details"]["approval_id"]
        wrong_hash = "sha256:" + "0" * 64

        # Direct approve with wrong hash
        rc_dir, resp_dir = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", wrong_hash,
            user=True,
        )
        self.assertEqual(rc_dir, 12)
        self.assertEqual(resp_dir["error"]["code"], c.OP_HASH_MISMATCH)

        rc_s1, resp_s1 = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(resp_s1["data"]["status"], "pending")

        # Relayed approve with wrong hash
        rc_rel, resp_rel = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", wrong_hash,
            "--relayed",
            "--user-statement", "James: ok",
            token=self.tok_grok,
        )
        self.assertEqual(rc_rel, 12)
        self.assertEqual(resp_rel["error"]["code"], c.OP_HASH_MISMATCH)

        rc_s2, resp_s2 = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(resp_s2["data"]["status"], "pending")

    def test_g10_self_approval_negatives(self) -> None:
        """ACC: G10 - Requester approves its own request; relayer that leads project approves request for that project -> rc 4 SELF_APPROVAL; still pending."""
        # Case 1: grok requests an approval, then tries to approve it via --relayed
        rc_req1, resp_req1 = self.harness.cli(
            "approval", "request",
            "--category", "deploy",
            "--summary", "Self deploy",
            "--operation", '{"t": 1}',
            token=self.tok_grok,
        )
        self.assertEqual(rc_req1, 0)
        apr1 = resp_req1["data"]["approval_id"]
        hash1 = resp_req1["data"]["op_hash"]

        rc_app1, resp_app1 = self.harness.cli(
            "approval", "approve", apr1,
            "--op-hash", hash1,
            "--relayed",
            "--user-statement", "self ok",
            token=self.tok_grok,
        )
        self.assertEqual(rc_app1, 4)
        self.assertEqual(resp_app1["error"]["code"], c.SELF_APPROVAL)

        rc_s1, resp_s1 = self.harness.cli("approval", "show", apr1, user=True)
        self.assertEqual(resp_s1["data"]["status"], "pending")

        # Case 2: PRJ2 created with lead rlead (relayer that is also lead)
        rc_p2, resp_p2 = self.harness.cli(
            "intake", "submit",
            "--title", "t2",
            "--goal", "g2",
            "--criterion", "c",
            "--lead", "rlead",
            token=self.tok_grok,
        )
        self.assertEqual(rc_p2, 0)
        prj2 = resp_p2["data"]["id"]

        # grok requests an approval for PRJ2
        rc_req2, resp_req2 = self.harness.cli(
            "approval", "request",
            "--project", prj2,
            "--category", "deploy",
            "--summary", "Deploy prj2",
            "--operation", '{"t": 2}',
            token=self.tok_grok,
        )
        self.assertEqual(rc_req2, 0)
        apr2 = resp_req2["data"]["approval_id"]
        hash2 = resp_req2["data"]["op_hash"]

        # rlead (leads PRJ2) attempts to approve via --relayed -> rc 4 SELF_APPROVAL
        rc_app2, resp_app2 = self.harness.cli(
            "approval", "approve", apr2,
            "--op-hash", hash2,
            "--relayed",
            "--user-statement", "lead approves",
            token=self.tok_rlead,
        )
        self.assertEqual(rc_app2, 4)
        self.assertEqual(resp_app2["error"]["code"], c.SELF_APPROVAL)

        rc_s2, resp_s2 = self.harness.cli("approval", "show", apr2, user=True)
        self.assertEqual(resp_s2["data"]["status"], "pending")

    def test_g11_channel_not_allowed(self) -> None:
        """ACC: G11 - Set approval.policy.project_control to direct_only; relayed approve -> rc 12 CHANNEL_NOT_ALLOWED; direct approve works."""
        rc_set, _ = self.harness.cli("config", "set", "approval.policy.project_control", '"direct_only"', user=True)
        self.assertEqual(rc_set, 0)

        rc_p, resp_p = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_p, c.EXIT_APPROVAL_REQUIRED)
        apr_id = resp_p["error"]["details"]["approval_id"]
        op_hash = resp_p["error"]["details"]["op_hash"]

        # Relayed approve disallowed by direct_only policy
        rc_rel, resp_rel = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", op_hash,
            "--relayed",
            "--user-statement", "James: ok",
            token=self.tok_grok,
        )
        self.assertEqual(rc_rel, 12)
        self.assertEqual(resp_rel["error"]["code"], c.CHANNEL_NOT_ALLOWED)

        rc_s, resp_s = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(resp_s["data"]["status"], "pending")

        # Direct approve still works
        rc_dir, resp_dir = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", op_hash,
            user=True,
        )
        self.assertEqual(rc_dir, 0)
        self.assertEqual(resp_dir["data"]["status"], "approved")

    def test_relayed_project_ops_respect_channel_policy(self) -> None:
        """Finding 1: Relayed project ops require policy direct_or_relayed; direct_only rejects with rc 12 CHANNEL_NOT_ALLOWED."""
        # 1. Relayed pause with project_control=direct_only -> rc 12, project stays active
        rc_set, _ = self.harness.cli("config", "set", "approval.policy.project_control", '"direct_only"', user=True)
        self.assertEqual(rc_set, 0)

        rc_pause, resp_pause = self.harness.cli(
            "project", "pause", self.project_id,
            "--relayed",
            "--user-statement", "Pause project",
            token=self.tok_grok,
        )
        self.assertEqual(rc_pause, 12)
        self.assertEqual(resp_pause["error"]["code"], c.CHANNEL_NOT_ALLOWED)

        conn = self.harness.db()
        prow = conn.execute("SELECT status FROM projects WHERE id = ?;", (self.project_id,)).fetchone()
        conn.close()
        self.assertEqual(prow["status"], "active")

        # 2. Relayed update with scope_change=direct_only -> rc 12, goal unchanged
        rc_set2, _ = self.harness.cli("config", "set", "approval.policy.scope_change", '"direct_only"', user=True)
        self.assertEqual(rc_set2, 0)

        rc_upd, resp_upd = self.harness.cli(
            "project", "update", self.project_id,
            "--goal", "Tampered Goal",
            "--relayed",
            "--user-statement", "Change goal",
            token=self.tok_grok,
        )
        self.assertEqual(rc_upd, 12)
        self.assertEqual(resp_upd["error"]["code"], c.CHANNEL_NOT_ALLOWED)

        conn = self.harness.db()
        prow2 = conn.execute("SELECT goal FROM projects WHERE id = ?;", (self.project_id,)).fetchone()
        conn.close()
        self.assertEqual(prow2["goal"], "g")

        # 3. Relayed set-lead with lead_change=direct_only -> rc 12 CHANNEL_NOT_ALLOWED, lead unchanged
        rc_set3, _ = self.harness.cli("config", "set", "approval.policy.lead_change", '"direct_only"', user=True)
        self.assertEqual(rc_set3, 0)

        rc_lead, resp_lead = self.harness.cli(
            "project", "set-lead", self.project_id, "lead2",
            "--reason", "Change lead to lead2",
            "--relayed",
            "--user-statement", "Change lead to lead2",
            token=self.tok_grok,
        )
        self.assertEqual(rc_lead, 12)
        self.assertEqual(resp_lead["error"]["code"], c.CHANNEL_NOT_ALLOWED)

        rc_leads, resp_leads = self.harness.cli("project", "leads", self.project_id, user=True)
        self.assertEqual(rc_leads, 0)
        self.assertEqual(resp_leads["data"]["current_lead"], "lead1")

    def test_g14_idempotency_conflict_and_cancel(self) -> None:
        """ACC: G14 - 2nd approve rc 0 already_decided:true with no new event; reject after approve rc 6 DECISION_CONFLICT; cancel pending approval."""
        rc_p, resp_p = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_p, c.EXIT_APPROVAL_REQUIRED)
        apr_id = resp_p["error"]["details"]["approval_id"]
        op_hash = resp_p["error"]["details"]["op_hash"]

        # 1. First approve -> rc 0
        rc1, resp1 = self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc1, 0)
        self.assertEqual(resp1["data"]["status"], "approved")

        conn = self.harness.db()
        events_cnt1 = conn.execute("SELECT COUNT(*) as c FROM events WHERE approval_id = ? AND type = 'approval.decided';", (apr_id,)).fetchone()["c"]
        conn.close()

        # 2. Second approve -> rc 0, already_decided: true, no new event
        rc2, resp2 = self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc2, 0)
        self.assertTrue(resp2["data"].get("already_decided"))

        conn = self.harness.db()
        events_cnt2 = conn.execute("SELECT COUNT(*) as c FROM events WHERE approval_id = ? AND type = 'approval.decided';", (apr_id,)).fetchone()["c"]
        conn.close()
        self.assertEqual(events_cnt1, events_cnt2)

        # 3. Reject after approve -> rc 6 DECISION_CONFLICT
        rc3, resp3 = self.harness.cli("approval", "reject", apr_id, "--op-hash", op_hash, "--note", "conflict note", user=True)
        self.assertEqual(rc3, 6)
        self.assertEqual(resp3["error"]["code"], c.DECISION_CONFLICT)

        # 4. Cancel pending approval (requester only)
        rc_req, resp_req = self.harness.cli(
            "approval", "request",
            "--category", "deploy",
            "--summary", "Cancel test",
            "--operation", '{"t": "cancel"}',
            token=self.tok_grok,
        )
        self.assertEqual(rc_req, 0)
        apr_c = resp_req["data"]["approval_id"]

        # Cancel by non-requester (plain) -> rc 4 NOT_REQUESTER
        rc_c_bad, resp_c_bad = self.harness.cli("approval", "cancel", apr_c, token=self.tok_plain)
        self.assertEqual(rc_c_bad, 4)
        self.assertEqual(resp_c_bad["error"]["code"], c.NOT_REQUESTER)

        # Cancel by requester (grok) -> rc 0
        rc_c_ok, resp_c_ok = self.harness.cli("approval", "cancel", apr_c, token=self.tok_grok)
        self.assertEqual(rc_c_ok, 0)
        self.assertEqual(resp_c_ok["data"]["status"], "cancelled")
        # Release follow-up (c): the cancelled event payload names the approval and its kind.
        conn = self.harness.db()
        ev = conn.execute("SELECT payload_json FROM events WHERE type = 'approval.cancelled' AND approval_id = ?;",
                          (apr_c,)).fetchone()
        conn.close()
        cpay = json.loads(ev["payload_json"])
        self.assertEqual(cpay["approval_id"], apr_c)
        self.assertTrue(cpay["category"])
        self.assertTrue(cpay["op"])

        # Cancel already cancelled approval -> rc 6 INVALID_TRANSITION
        rc_c_again, resp_c_again = self.harness.cli("approval", "cancel", apr_c, token=self.tok_grok)
        self.assertEqual(rc_c_again, 6)
        self.assertEqual(resp_c_again["error"]["code"], c.INVALID_TRANSITION)

    def test_g28_permission_escalation_default_policy(self) -> None:
        """ACC: G28 - Default policy permission_escalation: approve --relayed -> rc 12 CHANNEL_NOT_ALLOWED; then direct approve -> rc 0 trust:direct_verified."""
        # Create task requiring elevated profile (above fx max_unapproved_profile: dev)
        # First ensure fx exists with max_unapproved_profile dev
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "Escalated task",
            "--spec", "Elevated spec",
            "--assignee", "fx",
            "--profile", "elevated",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, c.EXIT_APPROVAL_REQUIRED)
        apr_id = resp_t["error"]["details"]["approval_id"]
        op_hash = resp_t["error"]["details"]["op_hash"]

        # Relayed approve -> rc 12 CHANNEL_NOT_ALLOWED (default policy for permission_escalation is direct_only)
        rc_rel, resp_rel = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", op_hash,
            "--relayed",
            "--user-statement", "James: allow escalation",
            token=self.tok_grok,
        )
        self.assertEqual(rc_rel, 12)
        self.assertEqual(resp_rel["error"]["code"], c.CHANNEL_NOT_ALLOWED)

        rc_s, resp_s = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(resp_s["data"]["status"], "pending")

        # Direct approve -> rc 0, trust: direct_verified
        rc_dir, resp_dir = self.harness.cli(
            "approval", "approve", apr_id,
            "--op-hash", op_hash,
            user=True,
        )
        self.assertEqual(rc_dir, 0)
        self.assertEqual(resp_dir["data"]["status"], "approved")
        self.assertEqual(resp_dir["data"]["trust"], "direct_verified")
        self.assertIsNotNone(resp_dir["data"]["user_mac"])

    def test_g2_rerun_and_g13_op_hash_mismatch(self) -> None:
        """ACC: G2 - Gated pause re-run consumes approval.
        ACC: G13 - Op-hash mismatch on cancel with pause apr and profile escalation mismatch."""
        rc_p, resp_p = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_p, c.EXIT_APPROVAL_REQUIRED)
        apr_id = resp_p["error"]["details"]["approval_id"]
        op_hash = resp_p["error"]["details"]["op_hash"]

        # Approve directly
        rc_app, _ = self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc_app, 0)

        # G13: Try project cancel with pause approval -> rc 12 OP_HASH_MISMATCH
        rc_c, resp_c = self.harness.cli("project", "cancel", self.project_id, "--approval", apr_id, token=self.tok_lead1)
        self.assertEqual(rc_c, 12)
        self.assertEqual(resp_c["error"]["code"], c.OP_HASH_MISMATCH)

        # Verify project is still active and approval is still approved
        rc_pshow, resp_pshow = self.harness.cli("project", "show", self.project_id, user=True)
        self.assertEqual(resp_pshow["data"]["status"], "active")
        rc_ashow, resp_ashow = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(resp_ashow["data"]["status"], "approved")

        # G13 clause 2: Permission escalation approval for profile elevated re-run with --profile unrestricted -> rc 12 OP_HASH_MISMATCH
        rc_t, resp_t = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "G13 escalation task",
            "--spec", "spec",
            "--assignee", "fx",
            "--profile", "elevated",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_t, c.EXIT_APPROVAL_REQUIRED)
        t_apr = resp_t["error"]["details"]["approval_id"]
        t_hash = resp_t["error"]["details"]["op_hash"]

        rc_app_t, _ = self.harness.cli("approval", "approve", t_apr, "--op-hash", t_hash, user=True)
        self.assertEqual(rc_app_t, 0)

        rc_re_bad, resp_re_bad = self.harness.cli(
            "task", "create",
            "--project", self.project_id,
            "--title", "G13 escalation task",
            "--spec", "spec",
            "--assignee", "fx",
            "--profile", "unrestricted",
            "--approval", t_apr,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_re_bad, 12)
        self.assertEqual(resp_re_bad["error"]["code"], c.OP_HASH_MISMATCH)

        # G13 clause 2: target unchanged (no task created) and approval remains approved
        conn = self.harness.db()
        cnt_task = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE title = 'G13 escalation task';").fetchone()["c"]
        arow_t = conn.execute("SELECT status FROM approvals WHERE id = ?;", (t_apr,)).fetchone()
        conn.close()
        self.assertEqual(cnt_task, 0)
        self.assertEqual(arow_t["status"], "approved")

        # G2: Re-run pause with --approval APR succeeds, project transitions to paused, approval is consumed
        rc_re, resp_re = self.harness.cli("project", "pause", self.project_id, "--approval", apr_id, token=self.tok_lead1)
        self.assertEqual(rc_re, 0)
        self.assertTrue(resp_re["ok"])
        self.assertEqual(resp_re["data"]["status"], "paused")

        conn = self.harness.db()
        arow = conn.execute("SELECT status, consumed_by, consumed_request_id FROM approvals WHERE id = ?;", (apr_id,)).fetchone()
        lead1_id = conn.execute("SELECT id FROM principals WHERE name = 'lead1';").fetchone()["id"]
        conn.close()
        self.assertEqual(arow["status"], "consumed")
        self.assertEqual(arow["consumed_by"], lead1_id)
        self.assertIsNotNone(arow["consumed_request_id"])

        # Resume project for further tests
        rc_res, _ = self.harness.cli("project", "resume", self.project_id, user=True)
        self.assertEqual(rc_res, 0)

    def test_g14_consume_twice_and_g19_not_requester(self) -> None:
        """ACC: G14 - Standalone approval consume twice: second -> rc 12 APPROVAL_CONSUMED.
        ACC: G19 - Standalone approval consume by non-requester -> rc 4 NOT_REQUESTER."""
        rc_req, resp_req = self.harness.cli(
            "approval", "request",
            "--category", "spend_money",
            "--summary", "API Credits",
            "--operation", '{"usd": 20}',
            token=self.tok_lead1,
        )
        self.assertEqual(rc_req, 0)
        apr_id = resp_req["data"]["approval_id"]
        op_hash = resp_req["data"]["op_hash"]

        # Approve by user
        rc_app, _ = self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc_app, 0)

        # G19: Consume by non-requester (plain) -> rc 4 NOT_REQUESTER
        rc_bad, resp_bad = self.harness.cli("approval", "consume", apr_id, "--op-hash", op_hash, token=self.tok_plain)
        self.assertEqual(rc_bad, 4)
        self.assertEqual(resp_bad["error"]["code"], c.NOT_REQUESTER)

        # Approval stays approved
        rc_s, resp_s = self.harness.cli("approval", "show", apr_id, user=True)
        self.assertEqual(resp_s["data"]["status"], "approved")

        # G14: First consume by requester (lead1) -> rc 0
        rc_c1, resp_c1 = self.harness.cli("approval", "consume", apr_id, "--op-hash", op_hash, token=self.tok_lead1)
        self.assertEqual(rc_c1, 0)
        self.assertEqual(resp_c1["data"]["status"], "consumed")

        # G14: Second consume by requester (lead1) -> rc 12 APPROVAL_CONSUMED
        rc_c2, resp_c2 = self.harness.cli("approval", "consume", apr_id, "--op-hash", op_hash, token=self.tok_lead1)
        self.assertEqual(rc_c2, 12)
        self.assertEqual(resp_c2["error"]["code"], c.APPROVAL_CONSUMED)

    @serial
    def test_g15_testing_mode_expiry(self) -> None:
        """ACC: G15 - Testing mode: request with --expires-in 1s, wait for expiry -> expired; approve one, let it expire, then use --approval -> rc 12 APPROVAL_EXPIRED."""

        rc_req, resp_req = self.harness.cli(
            "approval", "request",
            "--category", "deploy",
            "--summary", "Fast expiry",
            "--operation", '{"t": 1}',
            "--expires-in", "1s",
            token=self.tok_grok,
        )
        self.assertEqual(rc_req, 0)
        apr1 = resp_req["data"]["approval_id"]
        hash1 = resp_req["data"]["op_hash"]

        # Wait until the 1s TTL has lapsed (poll the daemon instead of a fixed sleep)
        self.assertTrue(
            self.harness.wait_for(
                lambda: self.harness.cli("approval", "show", apr1, user=True)[1].get("data", {}).get("status") == "expired",
                timeout=10.0,
                step=0.1,
            ),
            "approval never expired",
        )

        # Attempt to approve expired approval -> rc 12 APPROVAL_EXPIRED
        rc_app, resp_app = self.harness.cli("approval", "approve", apr1, "--op-hash", hash1, user=True)
        self.assertEqual(rc_app, 12)
        self.assertEqual(resp_app["error"]["code"], c.APPROVAL_EXPIRED)

        # Wait until scheduler sweeps and marks status expired (ACC: G15)
        self.assertTrue(
            self.harness.wait_for(
                lambda: self.harness.cli("approval", "show", apr1, user=True)[1].get("data", {}).get("status") == "expired",
                timeout=5.0,
                step=0.2,
            )
        )
        rc_s1, resp_s1 = self.harness.cli("approval", "show", apr1, user=True)
        self.assertEqual(resp_s1["data"]["status"], "expired")

        # Assert one approval.expired event and one requester outbox row with dedup apr_dec:<apr>:expired
        conn = self.harness.db()
        ev1 = conn.execute("SELECT * FROM events WHERE approval_id = ? AND type = 'approval.expired';", (apr1,)).fetchall()
        self.assertEqual(len(ev1), 1)
        ob1 = conn.execute("SELECT * FROM outbox WHERE dedup_key = ?;", (f"apr_dec:{apr1}:expired",)).fetchall()
        self.assertEqual(len(ob1), 1)
        conn.close()

        # Clause 2: Approve one, let it expire, then use --approval
        rc_cfg, _ = self.harness.cli("config", "set", "approval.ttl_seconds", "2", user=True)
        self.assertEqual(rc_cfg, 0)

        rc_p, resp_p = self.harness.cli(
            "project", "pause", self.project_id,
            token=self.tok_lead1,
        )
        self.assertEqual(rc_p, c.EXIT_APPROVAL_REQUIRED)
        apr2 = resp_p["error"]["details"]["approval_id"]
        hash2 = resp_p["error"]["details"]["op_hash"]

        # Approve immediately
        rc_app2, _ = self.harness.cli("approval", "approve", apr2, "--op-hash", hash2, user=True)
        self.assertEqual(rc_app2, 0)

        # Wait until the 2s TTL has lapsed (poll the daemon instead of a fixed sleep)
        self.assertTrue(
            self.harness.wait_for(
                lambda: self.harness.cli("approval", "show", apr2, user=True)[1].get("data", {}).get("status") == "expired",
                timeout=10.0,
                step=0.1,
            ),
            "approval never expired",
        )

        # Re-run pause with expired approval -> rc 12 APPROVAL_EXPIRED
        rc_re, resp_re = self.harness.cli("project", "pause", self.project_id, "--approval", apr2, token=self.tok_lead1)
        self.assertEqual(rc_re, 12)
        self.assertEqual(resp_re["error"]["code"], c.APPROVAL_EXPIRED)

        # Wait until scheduler sweeps and marks status expired
        self.assertTrue(
            self.harness.wait_for(
                lambda: self.harness.cli("approval", "show", apr2, user=True)[1].get("data", {}).get("status") == "expired",
                timeout=5.0,
                step=0.2,
            )
        )
        rc_s2, resp_s2 = self.harness.cli("approval", "show", apr2, user=True)
        self.assertEqual(resp_s2["data"]["status"], "expired")

        # Assert one approval.expired event and outbox row with dedup apr_dec:<apr2>:expired for lead1
        conn = self.harness.db()
        ev2 = conn.execute("SELECT * FROM events WHERE approval_id = ? AND type = 'approval.expired';", (apr2,)).fetchall()
        self.assertEqual(len(ev2), 1)
        ob2 = conn.execute("SELECT * FROM outbox WHERE dedup_key = ?;", (f"apr_dec:{apr2}:expired",)).fetchall()
        self.assertEqual(len(ob2), 1)
        conn.close()

        # Project remains active
        rc_pshow, resp_pshow = self.harness.cli("project", "show", self.project_id, user=True)
        self.assertEqual(resp_pshow["data"]["status"], "active")

        # Next identical gated call returns a new approval_id
        rc_p3, resp_p3 = self.harness.cli("project", "pause", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc_p3, c.EXIT_APPROVAL_REQUIRED)
        apr3 = resp_p3["error"]["details"]["approval_id"]
        self.assertNotEqual(apr3, apr2)

        # Reset ttl_seconds back to default
        self.harness.cli("config", "set", "approval.ttl_seconds", "86400", user=True)

    def test_g21_spend_money_flow_and_authority(self) -> None:
        """ACC: G21 - m approval request --category spend_money --summary s --operation '{"usd":5}' [@lead1]; user approves; check rc 0; consume twice; unprivileged agent fails request."""
        # 1. Lead1 requests spend_money approval
        rc_req, resp_req = self.harness.cli(
            "approval", "request",
            "--category", "spend_money",
            "--summary", "API Spend",
            "--operation", '{"usd": 5}',
            token=self.tok_lead1,
        )
        self.assertEqual(rc_req, 0)
        apr_id = resp_req["data"]["approval_id"]
        op_hash = resp_req["data"]["op_hash"]

        # 2. User approves
        rc_app, resp_app = self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc_app, 0)
        self.assertEqual(resp_app["data"]["status"], "approved")

        # 3. Check approval (read-only, no state change)
        rc_chk, resp_chk = self.harness.cli("approval", "check", apr_id, "--op-hash", op_hash, token=self.tok_lead1)
        self.assertEqual(rc_chk, 0)
        self.assertEqual(resp_chk["data"]["status"], "approved")
        self.assertEqual(resp_chk["data"]["trust"], "direct_verified")

        # 4. First consume -> rc 0
        rc_c1, resp_c1 = self.harness.cli("approval", "consume", apr_id, "--op-hash", op_hash, token=self.tok_lead1)
        self.assertEqual(rc_c1, 0)
        self.assertEqual(resp_c1["data"]["status"], "consumed")

        # 5. Second consume -> rc 12 APPROVAL_CONSUMED
        rc_c2, resp_c2 = self.harness.cli("approval", "consume", apr_id, "--op-hash", op_hash, token=self.tok_lead1)
        self.assertEqual(rc_c2, 12)
        self.assertEqual(resp_c2["error"]["code"], c.APPROVAL_CONSUMED)

        # 6. Plain agent (lacks relay, does not lead any project) attempts approval request without project -> rc 4 CAPABILITY_MISSING
        rc_plain_req, resp_plain_req = self.harness.cli(
            "approval", "request",
            "--category", "spend_money",
            "--summary", "Plain spend",
            "--operation", '{"usd": 1}',
            token=self.tok_plain,
        )
        self.assertEqual(rc_plain_req, 4)
        self.assertEqual(resp_plain_req["error"]["code"], c.CAPABILITY_MISSING)

    def test_f3_audit_verify_hash_chain_and_append_only_triggers(self) -> None:
        """ACC: F3 - db (writable connection) UPDATE events SET reason='x' and DELETE FROM events abort with 'events are append-only'; then m audit verify [U] on an untampered home passes rc 0; tampered home fails rc 12 AUDIT_MISMATCH."""
        import sqlite3
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        try:
            # 1. Attempt UPDATE on events -> must abort with 'events are append-only'
            with self.assertRaises(sqlite3.DatabaseError) as cm_upd:
                conn.execute("UPDATE events SET reason = 'tampered';")
            self.assertIn("events are append-only", str(cm_upd.exception))

            # 2. Attempt DELETE on events -> must abort with 'events are append-only'
            with self.assertRaises(sqlite3.DatabaseError) as cm_del:
                conn.execute("DELETE FROM events;")
            self.assertIn("events are append-only", str(cm_del.exception))
        finally:
            conn.close()

        # 3. Create tasks, direct approval, and relayed approval so home has all artifact types (Finding 7)
        rc_t, _ = self.harness.cli("task", "create", "--project", self.project_id, "--title", "F3 Task", "--spec", "Spec", token=self.tok_lead1)
        self.assertEqual(rc_t, 0)

        rc_r1, resp_r1 = self.harness.cli("approval", "request", "--category", "spend_money", "--summary", "F3 Direct", "--operation", '{"usd": 1}', token=self.tok_lead1)
        self.assertEqual(rc_r1, 0)
        rc_a1, _ = self.harness.cli("approval", "approve", resp_r1["data"]["approval_id"], "--op-hash", resp_r1["data"]["op_hash"], user=True)
        self.assertEqual(rc_a1, 0)

        rc_r2, resp_r2 = self.harness.cli("approval", "request", "--category", "spend_money", "--summary", "F3 Relayed", "--operation", '{"usd": 2}', token=self.tok_lead1)
        self.assertEqual(rc_r2, 0)
        rc_a2, _ = self.harness.cli("approval", "approve", resp_r2["data"]["approval_id"], "--op-hash", resp_r2["data"]["op_hash"], "--relayed", "--user-statement", "James: OK", token=self.tok_grok)
        self.assertEqual(rc_a2, 0)

        # Audit verify on untampered home -> rc 0, chain continuous, relayed_summary populated
        rc, resp = self.harness.cli("audit", "verify", user=True)
        self.assertEqual(rc, 0)
        self.assertTrue(resp["ok"])
        self.assertGreater(resp["data"]["events_verified"], 0)
        self.assertGreaterEqual(resp["data"]["direct_decisions"], 1)
        self.assertGreaterEqual(resp["data"]["relayed_decisions"], 1)
        rel_summary = resp["data"]["relayed_summary"]
        self.assertGreaterEqual(len(rel_summary), 1)

        conn = sqlite3.connect(db_path)
        grok_id = conn.execute("SELECT id FROM principals WHERE name = 'grok';").fetchone()[0]
        conn.close()
        self.assertTrue(any(s["relayed_by"] == grok_id and s["count"] >= 1 and s.get("credential_id") for s in rel_summary))

        # 4. Tamper by dropping trigger and altering prev_hash
        conn = sqlite3.connect(db_path)
        try:
            orig_prev_hash = conn.execute("SELECT prev_hash FROM events WHERE seq = 2;").fetchone()[0]
            conn.execute("DROP TRIGGER events_no_update;")
            conn.execute("UPDATE events SET prev_hash = 'tampered_hash' WHERE seq = 2;")
            conn.commit()
        finally:
            conn.close()

        # 5. Audit verify on tampered home -> rc 12 AUDIT_MISMATCH
        rc_tampered, resp_tampered = self.harness.cli("audit", "verify", user=True)
        self.assertEqual(rc_tampered, 12)
        self.assertFalse(resp_tampered["ok"])
        self.assertEqual(resp_tampered["error"]["code"], c.AUDIT_MISMATCH)
        self.assertIn(2, resp_tampered["error"]["details"]["broken_event_seqs"])

        # 6. Restore original hash and re-create trigger so teardown invariant check passes
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE events SET prev_hash = ? WHERE seq = 2;", (orig_prev_hash,))
            conn.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;")
            conn.commit()
        finally:
            conn.close()

    def test_g20_audit_verify_bogus_user_mac(self) -> None:
        """ACC: G20 - db (writable): set an approval to approved, decided_via='direct', bogus user_mac; m audit verify [U] returns rc 12 AUDIT_MISMATCH; details lists that approval id."""
        import sqlite3
        # 1. Create and directly approve an approval
        rc_req, resp_req = self.harness.cli(
            "approval", "request",
            "--category", "lead_change",
            "--summary", "Change lead to lead2",
            "--operation", json.dumps({"project_id": self.project_id, "agent_id": "lead2"}),
            token=self.tok_lead1,
        )
        self.assertEqual(rc_req, 0)
        apr_id = resp_req["data"]["approval_id"]
        op_hash = resp_req["data"]["op_hash"]

        rc_app, resp_app = self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, "--note", "approved by user", user=True)
        self.assertEqual(rc_app, 0)
        self.assertEqual(resp_app["data"]["status"], "approved")

        # 2. Audit verify before tampering -> rc 0, 1 direct decision verified
        rc_audit0, resp_audit0 = self.harness.cli("audit", "verify", user=True)
        self.assertEqual(rc_audit0, 0)
        self.assertTrue(resp_audit0["ok"])
        self.assertEqual(resp_audit0["data"]["direct_decisions"], 1)

        # 3. Tamper with SQLite: set bogus user_mac on the approval
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        try:
            bogus_mac = "deadbeef" * 8
            conn.execute("UPDATE approvals SET user_mac = ? WHERE id = ?;", (bogus_mac, apr_id))
            conn.commit()
        finally:
            conn.close()

        # 4. Audit verify after tampering -> rc 12 AUDIT_MISMATCH with apr_id in broken_approval_ids
        rc_audit, resp_audit = self.harness.cli("audit", "verify", user=True)
        self.assertEqual(rc_audit, 12)
        self.assertFalse(resp_audit["ok"])
        self.assertEqual(resp_audit["error"]["code"], c.AUDIT_MISMATCH)
        self.assertIn(apr_id, resp_audit["error"]["details"]["broken_approval_ids"])

    def test_audit_verify_consumed_tampered_and_null_decided_via(self) -> None:
        """Finding 2: audit verify covers consumed direct approvals; flags rows with NULL decided_via; consume rejects NULL decided_via."""
        # 1. Consumed direct approval with tampered user_mac -> audit verify rc 12 AUDIT_MISMATCH listing id
        rc_req, resp_req = self.harness.cli(
            "approval", "request",
            "--category", "lead_change",
            "--summary", "Change lead to lead2",
            "--operation", json.dumps({"project_id": self.project_id, "agent_id": "lead2"}),
            token=self.tok_lead1,
        )
        self.assertEqual(rc_req, 0)
        apr_id = resp_req["data"]["approval_id"]
        op_hash = resp_req["data"]["op_hash"]

        # Direct approve
        rc_app, _ = self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc_app, 0)

        # Consume via approval consume
        rc_c, _ = self.harness.cli("approval", "consume", apr_id, "--op-hash", op_hash, token=self.tok_lead1)
        self.assertEqual(rc_c, 0)

        # Tamper user_mac on the consumed approval
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE approvals SET user_mac = 'tampered_consumed_mac' WHERE id = ?;", (apr_id,))
            conn.commit()
        finally:
            conn.close()

        # Audit verify must fail rc 12 listing apr_id in broken_approval_ids
        rc_audit1, resp_audit1 = self.harness.cli("audit", "verify", user=True)
        self.assertEqual(rc_audit1, 12)
        self.assertEqual(resp_audit1["error"]["code"], c.AUDIT_MISMATCH)
        self.assertIn(apr_id, resp_audit1["error"]["details"]["broken_approval_ids"])

        # Clean up tampered row so we can isolate case 2
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM approvals WHERE id = ?;", (apr_id,))
            conn.commit()
        finally:
            conn.close()

        # 2. DB-inserted approved row with decided_via NULL -> audit rc 12, consume rc 12
        forged_id = "apr_forged1"
        forged_op = json.dumps({"project_id": self.project_id, "agent_id": "lead2"})
        from mesh.core.canonical import compute_op_hash
        conn = sqlite3.connect(db_path)
        lead1_id = conn.execute("SELECT id FROM principals WHERE name = 'lead1';").fetchone()[0]
        conn.close()

        _, forged_hash = compute_op_hash("external.lead_change", lead1_id, json.loads(forged_op), category="lead_change")

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO approvals (id, category, op_type, summary, requested_by, requested_at, project_id, op_json, op_hash, status, decided_via, expires_at)
                   VALUES (?, 'lead_change', 'external.lead_change', 'summary', ?, '2026-01-01T00:00:00Z', ?, ?, ?, 'approved', NULL, '2099-01-01T00:00:00Z');""",
                (forged_id, lead1_id, self.project_id, forged_op, forged_hash),
            )
            conn.commit()
        finally:
            conn.close()

        # Audit verify fails rc 12 with forged_id in broken_approval_ids
        rc_audit2, resp_audit2 = self.harness.cli("audit", "verify", user=True)
        self.assertEqual(rc_audit2, 12)
        self.assertEqual(resp_audit2["error"]["code"], c.AUDIT_MISMATCH)
        self.assertIn(forged_id, resp_audit2["error"]["details"]["broken_approval_ids"])

        # Attempt to consume forged approval -> rc 12 APPROVAL_INVALID
        rc_c2, resp_c2 = self.harness.cli("approval", "consume", forged_id, "--op-hash", forged_hash, token=self.tok_lead1)
        self.assertEqual(rc_c2, 12)
        self.assertEqual(resp_c2["error"]["code"], c.APPROVAL_INVALID)

        # Clean up forged approval
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM approvals WHERE id = ?;", (forged_id,))
            conn.commit()
        finally:
            conn.close()

    def test_audit_verify_and_consume_reject_null_decided_at_or_mac(self) -> None:
        """ACC: G20 - Finding 1: DB-inserted approved row with decided_via='direct', decided_at=NULL, user_mac=NULL.
        Assert audit verify [U] rc 12 with id in broken_approval_ids;
        approval consume [@lead1] rc 12 APPROVAL_INVALID;
        and gated re-run with --approval rc 12 with target unchanged and row still approved."""
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        lead1_id = conn.execute("SELECT id FROM principals WHERE name = 'lead1';").fetchone()[0]
        conn.close()

        forged_id = "apr_forged_null_at_mac"
        op_payload = {"project_id": self.project_id}
        from mesh.core.canonical import compute_op_hash
        _, op_hash = compute_op_hash(
            "project.pause",
            lead1_id,
            op_payload,
            project_id=self.project_id,
            target_id=self.project_id,
            category="project_control",
        )

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO approvals (id, category, op_type, summary, requested_by, requested_at, project_id, op_json, op_hash, status, decided_via, decided_at, decided_by, user_mac, expires_at)
                   VALUES (?, 'project_control', 'project.pause', 'Pause prj', ?, '2026-01-01T00:00:00Z', ?, ?, ?, 'approved', 'direct', NULL, ?, NULL, '2099-01-01T00:00:00Z');""",
                (forged_id, lead1_id, self.project_id, json.dumps(op_payload), op_hash, lead1_id),
            )
            conn.commit()
        finally:
            conn.close()

        try:
            # 1. Audit verify [U] -> rc 12 AUDIT_MISMATCH with forged_id in broken_approval_ids
            rc_audit, resp_audit = self.harness.cli("audit", "verify", user=True)
            self.assertEqual(rc_audit, 12)
            self.assertEqual(resp_audit["error"]["code"], c.AUDIT_MISMATCH)
            self.assertIn(forged_id, resp_audit["error"]["details"]["broken_approval_ids"])

            # 2. approval consume [@lead1] -> rc 12 APPROVAL_INVALID
            rc_c, resp_c = self.harness.cli("approval", "consume", forged_id, "--op-hash", op_hash, token=self.tok_lead1)
            self.assertEqual(rc_c, 12)
            self.assertEqual(resp_c["error"]["code"], c.APPROVAL_INVALID)

            # 3. Gated re-run with --approval -> rc 12 APPROVAL_INVALID
            rc_pause, resp_pause = self.harness.cli("project", "pause", self.project_id, "--approval", forged_id, token=self.tok_lead1)
            self.assertEqual(rc_pause, 12)
            self.assertEqual(resp_pause["error"]["code"], c.APPROVAL_INVALID)

            # Target project unchanged (still active)
            rc_show, resp_show = self.harness.cli("project", "show", self.project_id, token=self.tok_lead1)
            self.assertEqual(rc_show, 0)
            self.assertEqual(resp_show["data"]["status"], "active")

            # Row still approved
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT status FROM approvals WHERE id = ?;", (forged_id,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "approved")
            finally:
                conn.close()
        finally:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DELETE FROM approvals WHERE id = ?;", (forged_id,))
                conn.commit()
            finally:
                conn.close()

    def test_approval_check_restricted_to_requester_and_user(self) -> None:
        """Item 7: approval check is restricted to user and requester; relayer or other agent gets rc 4 NOT_REQUESTER."""
        rc_req, resp_req = self.harness.cli(
            "approval", "request",
            "--category", "deploy",
            "--summary", "Check restriction",
            "--operation", '{"env": "prod"}',
            token=self.tok_lead1,
        )
        self.assertEqual(rc_req, 0)
        apr_id = resp_req["data"]["approval_id"]
        op_hash = resp_req["data"]["op_hash"]

        # 1. User checks -> rc 0
        rc_u, resp_u = self.harness.cli("approval", "check", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc_u, 0)

        # 2. Requester (lead1) checks -> rc 0
        rc_req_chk, resp_req_chk = self.harness.cli("approval", "check", apr_id, "--op-hash", op_hash, token=self.tok_lead1)
        self.assertEqual(rc_req_chk, 0)

        # 3. Relayer (grok, not requester) checks -> rc 4 NOT_REQUESTER
        rc_rel, resp_rel = self.harness.cli("approval", "check", apr_id, "--op-hash", op_hash, token=self.tok_grok)
        self.assertEqual(rc_rel, 4)
        self.assertEqual(resp_rel["error"]["code"], c.NOT_REQUESTER)

        # 4. Plain agent checks -> rc 4 NOT_REQUESTER
        rc_pl, resp_pl = self.harness.cli("approval", "check", apr_id, "--op-hash", op_hash, token=self.tok_plain)
        self.assertEqual(rc_pl, 4)
        self.assertEqual(resp_pl["error"]["code"], c.NOT_REQUESTER)

    def test_g5_agent_callers_on_user_only_rows(self) -> None:
        """ACC: G5 - m config set …, m agent create …, m agent token issue … with [@lead1], and with --relayed … [@grok] return rc 4 CAPABILITY_MISSING each (plan §7.3 'Agent callers on user-only rows'): config is direct-only, never relayable."""
        # 1. Config set with @lead1 -> rc 4 CAPABILITY_MISSING
        rc1, r1 = self.harness.cli("config", "set", "task.default_timeout_s", "600", token=self.tok_lead1)
        self.assertEqual(rc1, 4)
        self.assertEqual(r1["error"]["code"], c.CAPABILITY_MISSING)

        # 2. Config set with --relayed @grok -> rc 4 CAPABILITY_MISSING
        rc2, r2 = self.harness.cli(
            "config", "set", "task.default_timeout_s", "600",
            "--relayed", "--user-statement", "approved config change",
            token=self.tok_grok,
        )
        self.assertEqual(rc2, 4)
        self.assertEqual(r2["error"]["code"], c.CAPABILITY_MISSING)

        # 3. Agent create with @lead1 -> rc 4 CAPABILITY_MISSING
        rc3, r3 = self.harness.cli("agent", "create", "--name", "subagent", "--adapter", "none", "--delivery", "inbox", "--max-concurrency", "1", token=self.tok_lead1)
        self.assertEqual(rc3, 4)
        self.assertEqual(r3["error"]["code"], c.CAPABILITY_MISSING)

        # 4. Agent create with --relayed @grok -> rc 4 CAPABILITY_MISSING
        rc4, r4 = self.harness.cli(
            "agent", "create", "--name", "subagent2", "--adapter", "none", "--delivery", "inbox", "--max-concurrency", "1",
            "--relayed", "--user-statement", "approved create agent",
            token=self.tok_grok,
        )
        self.assertEqual(rc4, 4)
        self.assertEqual(r4["error"]["code"], c.CAPABILITY_MISSING)

        # 5. Agent token issue with @lead1 -> rc 4 CAPABILITY_MISSING
        rc5, r5 = self.harness.cli("agent", "token", "issue", "lead1", token=self.tok_lead1)
        self.assertEqual(rc5, 4)
        self.assertEqual(r5["error"]["code"], c.CAPABILITY_MISSING)

        # 6. Agent token issue with --relayed @grok -> rc 4 CAPABILITY_MISSING
        rc6, r6 = self.harness.cli(
            "agent", "token", "issue", "lead1",
            "--relayed", "--user-statement", "approved issue token",
            token=self.tok_grok,
        )
        self.assertEqual(rc6, 4)
        self.assertEqual(r6["error"]["code"], c.CAPABILITY_MISSING)

        # 7. Audit verify with @lead1 -> rc 4 CAPABILITY_MISSING
        rc7, r7 = self.harness.cli("audit", "verify", token=self.tok_lead1)
        self.assertEqual(rc7, 4)
        self.assertEqual(r7["error"]["code"], c.CAPABILITY_MISSING)

        # 8. Audit verify with --relayed @grok -> rc 4 CAPABILITY_MISSING
        rc8, r8 = self.harness.cli(
            "audit", "verify",
            "--relayed", "--user-statement", "approved verify",
            token=self.tok_grok,
        )
        self.assertEqual(rc8, 4)
        self.assertEqual(r8["error"]["code"], c.CAPABILITY_MISSING)

        # 9. Config set with --relayed @lead1 (no relay cap) -> rc 4 CAPABILITY_MISSING (user-only check before relay check)
        rc9, r9 = self.harness.cli(
            "config", "set", "task.default_timeout_s", "600",
            "--relayed", "--user-statement", "approved config change",
            token=self.tok_lead1,
        )
        self.assertEqual(rc9, 4)
        self.assertEqual(r9["error"]["code"], c.CAPABILITY_MISSING)

        # 10. Agent token issue with --relayed @plain (no relay cap) -> rc 4 CAPABILITY_MISSING
        rc10, r10 = self.harness.cli(
            "agent", "token", "issue", "lead1",
            "--relayed", "--user-statement", "approved issue token",
            token=self.tok_plain,
        )
        self.assertEqual(rc10, 4)
        self.assertEqual(r10["error"]["code"], c.CAPABILITY_MISSING)

    def test_audit_verify_relayed_null_decided_at_and_null_decided_by(self) -> None:
        """Review 7c minor 1:
        (a) DB-insert an approved row with decided_via='relayed', decided_at NULL and correct op_hash:
            audit verify [U] -> rc 12 AUDIT_MISMATCH, id in details.broken_approval_ids, row still counted in relayed_summary.
        (b) DB-insert a decision row with decided_by NULL:
            audit verify [U] -> rc 12, id in details.broken_approval_ids.
        """
        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        lead1_id = conn.execute("SELECT id FROM principals WHERE name = 'lead1';").fetchone()[0]
        grok_id = conn.execute("SELECT id FROM principals WHERE name = 'grok';").fetchone()[0]
        conn.close()

        forged_relayed_id = "apr_forged_relayed_null_at"
        op_payload = {"project_id": self.project_id}
        from mesh.core.canonical import compute_op_hash
        _, op_hash = compute_op_hash(
            "project.pause",
            lead1_id,
            op_payload,
            project_id=self.project_id,
            target_id=self.project_id,
            category="project_control",
        )

        # Part (a): decided_via='relayed', decided_at=NULL, decided_by=lead1_id, relayed_by=grok_id, relayer_credential_id='cred_grok'
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO approvals (id, category, op_type, summary, requested_by, requested_at, project_id, op_json, op_hash, status, decided_via, decided_at, decided_by, relayed_by, relayer_credential_id, expires_at)
                   VALUES (?, 'project_control', 'project.pause', 'Pause prj', ?, '2026-01-01T00:00:00Z', ?, ?, ?, 'approved', 'relayed', NULL, ?, ?, 'cred_grok', '2099-01-01T00:00:00Z');""",
                (forged_relayed_id, lead1_id, self.project_id, json.dumps(op_payload), op_hash, lead1_id, grok_id),
            )
            conn.commit()
        finally:
            conn.close()

        try:
            rc_audit, resp_audit = self.harness.cli("audit", "verify", user=True)
            self.assertEqual(rc_audit, 12)
            self.assertEqual(resp_audit["error"]["code"], c.AUDIT_MISMATCH)
            details = resp_audit["error"]["details"]
            self.assertIn(forged_relayed_id, details["broken_approval_ids"])
            self.assertIn("relayed_summary", details)
            # Row must still be counted in relayed_summary
            summary_entry = [e for e in details["relayed_summary"] if e.get("relayed_by") == grok_id and e.get("credential_id") == "cred_grok"]
            self.assertTrue(len(summary_entry) > 0)
            self.assertGreaterEqual(summary_entry[0]["count"], 1)
        finally:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DELETE FROM approvals WHERE id = ?;", (forged_relayed_id,))
                conn.commit()
            finally:
                conn.close()

        # Part (b): decision row with decided_by NULL
        forged_null_by_id = "apr_forged_null_decided_by"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO approvals (id, category, op_type, summary, requested_by, requested_at, project_id, op_json, op_hash, status, decided_via, decided_at, decided_by, relayed_by, relayer_credential_id, expires_at)
                   VALUES (?, 'project_control', 'project.pause', 'Pause prj', ?, '2026-01-01T00:00:00Z', ?, ?, ?, 'approved', 'relayed', '2026-01-01T01:00:00Z', NULL, ?, 'cred_grok', '2099-01-01T00:00:00Z');""",
                (forged_null_by_id, lead1_id, self.project_id, json.dumps(op_payload), op_hash, grok_id),
            )
            conn.commit()
        finally:
            conn.close()

        try:
            rc_audit, resp_audit = self.harness.cli("audit", "verify", user=True)
            self.assertEqual(rc_audit, 12)
            self.assertEqual(resp_audit["error"]["code"], c.AUDIT_MISMATCH)
            details = resp_audit["error"]["details"]
            self.assertIn(forged_null_by_id, details["broken_approval_ids"])
        finally:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DELETE FROM approvals WHERE id = ?;", (forged_null_by_id,))
                conn.commit()
            finally:
                conn.close()


