"""Integration tests for card-based approvals, permission modes, standing grants, chat/plan flows, and audit.verify."""

import json
import os
import sqlite3
import time
import unittest

import mesh.constants as c
from mesh.client import MeshClient
from mesh.core import gitops
from tests.helpers import MeshHarness


class TestChatPlanAndCardApprovals(unittest.TestCase):
    """End-to-end integration tests over real meshd IPC covering modes, inline cards, standing grants, and audit.verify."""

    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)
        self.client = MeshClient(self.harness.home, timeout=30.0)

    def tearDown(self) -> None:
        self.harness.teardown()

    def _rpc_user(self, cmd: str, args: dict = None, *, approval: str = None) -> dict:
        req = {
            "v": 1,
            "cmd": cmd,
            "args": args or {},
            "auth": {"type": "user", "passphrase": self.harness.passphrase},
        }
        if approval:
            req["approval"] = approval
        return self.client.send_request(req)

    def _rpc_token(
        self,
        token: str,
        cmd: str,
        args: dict = None,
        *,
        approval: str = None,
        relayed: bool = False,
        user_statement: str = None,
    ) -> dict:
        req = {
            "v": 1,
            "cmd": cmd,
            "args": args or {},
            "auth": {"type": "token", "token": token},
        }
        if approval:
            req["approval"] = approval
        if relayed:
            req["relayed"] = True
            req["user_statement"] = user_statement or "User confirmed via relay"
        return self.client.send_request(req)

    def _create_agent_and_token(
        self,
        name: str,
        *,
        adapter: str = "fake",
        delivery: str = "inbox",
        can_lead: bool = False,
        capabilities: list = None,
        default_profile: str = "edit",
        max_unapproved_profile: str = "dev",
        allowed_models: list = None,
    ) -> str:
        args = {
            "name": name,
            "adapter": adapter,
            "delivery": delivery,
            "can_lead": can_lead,
            "max_concurrency": 2,
            "default_profile": default_profile,
            "max_unapproved_profile": max_unapproved_profile,
            "capabilities": capabilities if capabilities is not None else ["intake"],
            "allowed_models": allowed_models if allowed_models is not None else (["fake-model"] if delivery == "launch" else []),
        }
        resp = self._rpc_user("agent.create", args)
        self.assertTrue(resp.get("ok"), f"agent.create {name} failed: {resp}")
        tok_resp = self._rpc_user("agent.token.issue", {"name": name, "ttl": "1h"})
        self.assertTrue(tok_resp.get("ok"), f"agent.token.issue {name} failed: {tok_resp}")
        return tok_resp["data"]["token"]

    def _verify_audit(self) -> dict:
        res = self._rpc_user("audit.verify")
        self.assertTrue(res.get("ok"), f"audit.verify failed: {res}")
        return res["data"]

    def test_plan_mode_card_approval_chat_never_approves_and_audit(self) -> None:
        """Default 'plan' mode: chat text NEVER approves; clicking 允许 on plan card binds plan_hash & runs all steps; 拒绝 rejects."""
        lead_tok = self._create_agent_and_token("chat-lead", adapter="none", delivery="inbox", can_lead=True)
        self._create_agent_and_token("worker-a", adapter="fake", delivery="launch", can_lead=False)
        self._create_agent_and_token("worker-b", adapter="fake", delivery="launch", can_lead=False)
        self.harness.hold_launch_starts()

        prj_resp = self._rpc_user("intake.submit", {
            "title": "Rate Limiter",
            "goal": "Add rate limiting to login",
            "lead": "chat-lead",
            "plan_gate_mode": "require_approval",
        })
        self.assertTrue(prj_resp.get("ok"), prj_resp)
        prj_id = prj_resp["data"]["id"]
        self.assertEqual(prj_resp["data"]["plan_gate_mode"], "require_approval")
        self.assertEqual(prj_resp["data"]["permission_mode"], "plan")

        # Hard pre-dispatch gate: lead cannot call raw task.create in 'plan' mode
        sneak = self._rpc_token(lead_tok, "task.create", {
            "project": prj_id, "title": "Sneaky", "spec": "Bypass", "assignee": "worker-a",
        })
        self.assertFalse(sneak.get("ok"))
        self.assertEqual(sneak["error"]["code"], c.PLAN_APPROVAL_REQUIRED)

        # Lead proposes v1
        p1_resp = self._rpc_token(lead_tok, "plan.propose", {
            "project_id": prj_id,
            "title": "登录限流 v1",
            "summary": "2 steps",
            "steps": [
                {"key": "s1", "title": "实现限流器", "spec": "滑动窗口", "assignee": "worker-a", "profile": "edit"},
                {"key": "s2", "title": "补单测", "spec": "单测", "assignee": "worker-b", "profile": "dev", "depends_on": ["s1"]},
            ],
        })
        self.assertTrue(p1_resp.get("ok"), p1_resp)
        p1 = p1_resp["data"]["plan"]
        self.assertEqual(p1["version"], 1)
        self.assertEqual(p1["status"], "proposed")

        # Chat text NEVER approves anything (even explicit old verbal phrases!)
        for phrase in ("可以", "可以 开干", "approve", "yes go ahead", "确认批准", "拒绝"):
            m = self._rpc_user("chat.send", {"project_id": prj_id, "text": phrase})
            self.assertTrue(m.get("ok"), m)
            self.assertNotIn("matched_gate", m["data"])
            ps = self._rpc_user("plan.show", {"plan_id": p1["id"]})["data"]
            self.assertEqual(ps["status"], "proposed", f"Chat phrase {phrase!r} mutated plan status!")
            self.assertEqual(ps["approval"]["status"], "pending")

        # Lead can ask clarifying question in chat
        clar = self._rpc_token(lead_tok, "chat.reply", {
            "project_id": prj_id,
            "kind": "clarification",
            "text": "请点击上方计划卡片的「允许」或「拒绝」按钮确认。",
            "plan_id": p1["id"],
        })
        self.assertTrue(clar.get("ok"), clar)

        # Click 拒绝 on v1 with optional reason -> v1 rejected
        rej = self._rpc_user("approval.decide", {
            "approval_id": p1["approval_id"],
            "action": "reject",
            "plan_hash": p1["plan_hash"],
            "reason": "Need boundary tests in step 2",
        })
        self.assertTrue(rej.get("ok"), rej)
        self.assertEqual(rej["data"]["status"], "rejected")
        self.assertEqual(rej["data"]["plan_status"], "rejected")
        self.assertTrue(rej["data"]["user_mac"])

        # Lead cannot dispatch rejected v1
        d_rej = self._rpc_token(lead_tok, "plan.dispatch", {
            "project_id": prj_id, "plan_id": p1["id"],
        }, approval=p1["approval_id"])
        self.assertFalse(d_rej.get("ok"))
        self.assertEqual(d_rej["error"]["code"], c.INVALID_TRANSITION)

        # Lead proposes v2
        p2_resp = self._rpc_token(lead_tok, "plan.propose", {
            "project_id": prj_id,
            "title": "登录限流 v2",
            "summary": "2 steps with boundary tests",
            "steps": [
                {"key": "s1", "title": "实现限流器", "spec": "滑动窗口", "assignee": "worker-a", "profile": "edit"},
                {"key": "s2", "title": "边界与并发单测", "spec": "覆盖429与重置", "assignee": "worker-b", "profile": "dev", "depends_on": ["s1"]},
            ],
        })
        self.assertTrue(p2_resp.get("ok"), p2_resp)
        p2 = p2_resp["data"]["plan"]
        self.assertEqual(p2["version"], 2)

        # Click 允许 on v2 with exact plan_hash -> approved_pending_dispatch
        allow = self._rpc_user("approval.decide", {
            "approval_id": p2["approval_id"],
            "action": "allow",
            "plan_hash": p2["plan_hash"],
        })
        self.assertTrue(allow.get("ok"), allow)
        self.assertEqual(allow["data"]["status"], "approved")
        self.assertEqual(allow["data"]["decided_via"], "direct")
        self.assertTrue(allow["data"]["user_mac"])
        self.assertEqual(allow["data"]["plan_status"], "approved_pending_dispatch")

        # Lead dispatches v2 -> both steps materialized as 'assigned'
        disp = self._rpc_token(lead_tok, "plan.dispatch", {
            "project_id": prj_id, "plan_id": p2["id"],
        }, approval=p2["approval_id"])
        self.assertTrue(disp.get("ok"), disp)
        tasks = disp["data"]["tasks"]
        self.assertEqual(len(tasks), 2)
        by_key = {t["plan_step_key"]: t for t in tasks}
        self.assertEqual(by_key["s1"]["status"], "assigned")
        self.assertEqual(by_key["s2"]["status"], "assigned")
        self.assertEqual(by_key["s2"]["depends_on"], [by_key["s1"]["id"]])

        # chat.list hydrates plan_card & approval without short_code
        clist = self._rpc_user("chat.list", {"project_id": prj_id})["data"]
        self.assertEqual(clist["permission_mode"], "plan")
        self.assertIn(p2["id"], clist["plans_by_id"])
        apr_view = clist["approvals_by_id"][p2["approval_id"]]
        self.assertNotIn("short_code", apr_view)
        self.assertEqual(apr_view["plan_hash"], p2["plan_hash"])

        audit = self._verify_audit()
        self.assertGreaterEqual(audit["direct_decisions"], 2)

    def test_supervised_mode_requires_card_approval_per_dispatched_step(self) -> None:
        """'supervised' mode: every dispatched step starts in 'created' with a card approval before running."""
        lead_tok = self._create_agent_and_token("sup-lead", adapter="none", delivery="inbox", can_lead=True)
        self._create_agent_and_token("w-sup", adapter="fake", delivery="launch", can_lead=False)
        self.harness.hold_launch_starts()

        prj_id = self._rpc_user("intake.submit", {
            "title": "Supervised Project",
            "goal": "Step by step",
            "lead": "sup-lead",
            "plan_gate_mode": "require_approval",
            "permission_mode": "supervised",
        })["data"]["id"]

        # Verify project.show reports permission_mode='supervised'
        self.assertEqual(self._rpc_user("project.show", {"project_id": prj_id})["data"]["permission_mode"], "supervised")

        p = self._rpc_token(lead_tok, "plan.propose", {
            "project_id": prj_id,
            "title": "Two Steps",
            "summary": "S",
            "steps": [
                {"key": "s1", "title": "Step 1", "spec": "Do 1", "assignee": "w-sup", "profile": "edit"},
                {"key": "s2", "title": "Step 2", "spec": "Do 2", "assignee": "w-sup", "profile": "dev", "depends_on": ["s1"]},
            ],
        })["data"]["plan"]

        # Approve plan card
        self.assertTrue(self._rpc_user("approval.decide", {
            "approval_id": p["approval_id"], "action": "allow", "plan_hash": p["plan_hash"],
        }).get("ok"))

        # Dispatch plan -> both tasks MUST be in 'created' with step_dispatch approval cards!
        disp = self._rpc_token(lead_tok, "plan.dispatch", {
            "project_id": prj_id, "plan_id": p["id"],
        }, approval=p["approval_id"])
        self.assertTrue(disp.get("ok"), disp)
        tasks = disp["data"]["tasks"]
        step_aprs = disp["data"]["step_approvals"]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(len(step_aprs), 2)
        for t in tasks:
            self.assertEqual(t["status"], "created")
        for sa in step_aprs:
            self.assertEqual(sa["category"], "step_dispatch")

        # Lead trying task.assign on s1 WITHOUT step card approval MUST FAIL!
        s1_apr = next(sa for sa in step_aprs if sa["step_key"] == "s1")
        s2_apr = next(sa for sa in step_aprs if sa["step_key"] == "s2")
        no_apr = self._rpc_token(lead_tok, "task.assign", {
            "task_id": s1_apr["task_id"], "to": "w-sup", "profile": "edit",
        })
        self.assertFalse(no_apr.get("ok"))
        self.assertEqual(no_apr["error"]["code"], c.APPROVAL_REQUIRED)

        # User clicks 允许 on s1 step card -> lead assigns s1 -> assigned!
        ok1 = self._rpc_user("approval.decide", {
            "approval_id": s1_apr["approval_id"], "action": "allow", "op_hash": s1_apr["op_hash"],
        })
        self.assertTrue(ok1.get("ok"), ok1)
        a1 = self._rpc_token(lead_tok, "task.assign", {
            "task_id": s1_apr["task_id"], "to": "w-sup", "profile": "edit",
        }, approval=s1_apr["approval_id"])
        self.assertTrue(a1.get("ok"), a1)
        self.assertEqual(a1["data"]["status"], "assigned")

        # User clicks 拒绝 on s2 step card -> lead assign fails with APPROVAL_REJECTED!
        rej2 = self._rpc_user("approval.decide", {
            "approval_id": s2_apr["approval_id"], "action": "reject", "op_hash": s2_apr["op_hash"], "reason": "Skip step 2",
        })
        self.assertTrue(rej2.get("ok"), rej2)
        a2 = self._rpc_token(lead_tok, "task.assign", {
            "task_id": s2_apr["task_id"], "to": "w-sup", "profile": "dev",
        }, approval=s2_apr["approval_id"])
        self.assertFalse(a2.get("ok"))
        self.assertEqual(a2["error"]["code"], c.APPROVAL_REJECTED)
        self.assertEqual(self._rpc_user("task.show", {"task_id": s2_apr["task_id"]})["data"]["status"], "created")

        self._verify_audit()

    def test_auto_mode_and_plan_dispatch_always_allow_confirmation(self) -> None:
        """plan_dispatch 始终允许 requires confirm_auto_mode=true, switches project to 'auto', skips future plan approval & auto-allows retry_over_limit, but NEVER auto-approves permission_escalation."""
        lead_tok = self._create_agent_and_token("auto-lead", adapter="none", delivery="inbox", can_lead=True)
        worker_tok = self._create_agent_and_token("auto-w", adapter="none", delivery="inbox", can_lead=False, max_unapproved_profile="dev")

        prj_id = self._rpc_user("intake.submit", {
            "title": "Auto Transition",
            "goal": "Test always_allow -> auto",
            "lead": "auto-lead",
            "plan_gate_mode": "require_approval",
        })["data"]["id"]

        p1 = self._rpc_token(lead_tok, "plan.propose", {
            "project_id": prj_id,
            "title": "Plan 1",
            "summary": "S",
            "steps": [{"key": "s1", "title": "First", "spec": "Do 1", "assignee": "auto-w", "profile": "edit", "max_attempts": 1}],
        })["data"]["plan"]

        # 1. Clicking always_allow WITHOUT confirm_auto_mode=true asks for confirmation and mutates nothing!
        ask = self._rpc_user("approval.decide", {
            "approval_id": p1["approval_id"],
            "action": "always_allow",
            "plan_hash": p1["plan_hash"],
        })
        self.assertTrue(ask.get("ok"), ask)
        self.assertTrue(ask["data"]["confirmation_required"])
        self.assertEqual(ask["data"]["effect"], "switch_project_to_auto_mode")
        self.assertEqual(ask["data"]["target_permission_mode"], "auto")
        self.assertEqual(self._rpc_user("project.show", {"project_id": prj_id})["data"]["permission_mode"], "plan")
        self.assertEqual(self._rpc_user("plan.show", {"plan_id": p1["id"]})["data"]["status"], "proposed")

        # 2. Confirming with confirm_auto_mode=true approves plan AND switches project to 'auto'!
        conf = self._rpc_user("approval.decide", {
            "approval_id": p1["approval_id"],
            "action": "always_allow",
            "plan_hash": p1["plan_hash"],
            "confirm_auto_mode": True,
        })
        self.assertTrue(conf.get("ok"), conf)
        self.assertFalse(conf["data"]["confirmation_required"])
        self.assertEqual(conf["data"]["permission_mode"], "auto")
        self.assertTrue(conf["data"]["mode_changed"])
        self.assertEqual(conf["data"]["plan_status"], "approved_pending_dispatch")
        self.assertEqual(self._rpc_user("project.show", {"project_id": prj_id})["data"]["permission_mode"], "auto")

        # Dispatch p1
        d1 = self._rpc_token(lead_tok, "plan.dispatch", {"project_id": prj_id, "plan_id": p1["id"]}, approval=p1["approval_id"])
        self.assertTrue(d1.get("ok"), d1)
        tid = d1["data"]["tasks"][0]["id"]

        # 3. retry_over_limit is AUTO-APPROVED in 'auto' mode!
        self.assertTrue(self.harness.wait_for(
            lambda: self._rpc_user("task.show", {"task_id": tid})["data"]["status"] == "queued", timeout=5.0
        ))
        st = self._rpc_token(worker_tok, "task.start", {"task_id": tid})
        self.assertTrue(st.get("ok"), st)
        self.assertTrue(self._rpc_token(worker_tok, "task.fail", {
            "task_id": tid, "run": st["data"]["run_id"], "fence": st["data"]["fence"], "reason": "flake",
        }).get("ok"))
        auto_retry = self._rpc_token(lead_tok, "task.retry", {"task_id": tid, "reason": "auto retry"})
        self.assertTrue(auto_retry.get("ok"), auto_retry)
        self.assertEqual(auto_retry["data"]["status"], "queued")

        # 4. Subsequent plan in 'auto' mode needs NO plan approval, BUT permission_escalation STILL requires a card!
        p2 = self._rpc_token(lead_tok, "plan.propose", {
            "project_id": prj_id,
            "title": "Auto Plan 2",
            "summary": "One normal + one elevated",
            "steps": [
                {"key": "n1", "title": "Normal", "spec": "ok", "assignee": "auto-w", "profile": "dev"},
                {"key": "e1", "title": "Elevated", "spec": "needs card", "assignee": "auto-w", "profile": "elevated"},
            ],
        })["data"]["plan"]
        self.assertEqual(p2["status"], "approved_pending_dispatch")
        self.assertIsNone(p2["approval_id"])

        d2 = self._rpc_token(lead_tok, "plan.dispatch", {"project_id": prj_id, "plan_id": p2["id"]})
        self.assertTrue(d2.get("ok"), d2)
        by_key = {t["plan_step_key"]: t for t in d2["data"]["tasks"]}
        self.assertEqual(by_key["n1"]["status"], "assigned")
        # permission_escalation NEVER auto-approved by 'auto' mode!
        self.assertEqual(by_key["e1"]["status"], "created")
        self.assertEqual(len(d2["data"]["escalated_approvals"]), 1)

        # 5. Switch back to 'plan' via project.set_permission_mode -> plan approval restored
        sw = self._rpc_user("project.set_permission_mode", {"project_id": prj_id, "permission_mode": "plan", "reason": "back to plan"})
        self.assertTrue(sw.get("ok"), sw)
        self.assertEqual(sw["data"]["permission_mode"], "plan")
        p3 = self._rpc_token(lead_tok, "plan.propose", {
            "project_id": prj_id, "title": "Plan 3", "summary": "S",
            "steps": [{"key": "x1", "title": "X", "spec": "X", "assignee": "auto-w"}],
        })["data"]["plan"]
        self.assertEqual(p3["status"], "proposed")
        self.assertIsNotNone(p3["approval_id"])

        self._verify_audit()

    def test_standing_grants_exact_scoping_use_list_and_revoke(self) -> None:
        """Standing grants via 始终允许: exact narrow scope for permission_escalation & retry_over_limit (config stays user-only); grant.list / grant.revoke; audited."""
        lead_tok = self._create_agent_and_token("gr-lead", adapter="none", delivery="inbox", can_lead=True)
        w1_tok = self._create_agent_and_token("w1", adapter="none", delivery="inbox", max_unapproved_profile="dev")
        self._create_agent_and_token("w2", adapter="none", delivery="inbox", max_unapproved_profile="dev")

        prj1 = self._rpc_user("intake.submit", {
            "title": "Grant Prj 1", "goal": "G1", "lead": "gr-lead", "plan_gate_mode": "off", "criterion": ["ok"],
        })["data"]["id"]
        prj2 = self._rpc_user("intake.submit", {
            "title": "Grant Prj 2", "goal": "G2", "lead": "gr-lead", "plan_gate_mode": "off", "criterion": ["ok"],
        })["data"]["id"]

        # --- 1. permission_escalation standing grant: exact (prj1, w1, elevated) ---
        esc_req = self._rpc_token(lead_tok, "task.create", {
            "project": prj1, "title": "Esc 1", "spec": "S", "assignee": "w1", "profile": "elevated",
        })
        self.assertFalse(esc_req.get("ok"))
        self.assertEqual(esc_req["error"]["code"], c.APPROVAL_REQUIRED)
        esc_apr_id = esc_req["error"]["details"]["approval_id"]
        esc_op_hash = esc_req["error"]["details"]["op_hash"]

        # Click 始终允许 -> approves AND creates narrow standing grant (prj1, w1, elevated)
        dec = self._rpc_user("approval.decide", {
            "approval_id": esc_apr_id, "action": "always_allow", "op_hash": esc_op_hash,
        })
        self.assertTrue(dec.get("ok"), dec)
        self.assertTrue(dec["data"]["grant_created"])
        perm_grant = dec["data"]["grant"]
        self.assertEqual(perm_grant["category"], "permission_escalation")
        self.assertEqual(perm_grant["scope_profile"], "elevated")
        self.assertEqual(perm_grant["scope_agent_name"], "w1")
        self.assertEqual(perm_grant["project_id"], prj1)

        # Future task on SAME (prj1, w1, elevated) succeeds WITHOUT approval card!
        t_same = self._rpc_token(lead_tok, "task.create", {
            "project": prj1, "title": "Esc 2", "spec": "S2", "assignee": "w1", "profile": "elevated",
        })
        self.assertTrue(t_same.get("ok"), t_same)

        # Broader profile (unrestricted) on SAME (prj1, w1) MUST STILL REQUIRE APPROVAL (no profile broadening)!
        t_broader = self._rpc_token(lead_tok, "task.create", {
            "project": prj1, "title": "Esc Unr", "spec": "S3", "assignee": "w1", "profile": "unrestricted",
        })
        self.assertFalse(t_broader.get("ok"))
        self.assertEqual(t_broader["error"]["code"], c.APPROVAL_REQUIRED)

        # Different agent (w2) on SAME (prj1, elevated) MUST STILL REQUIRE APPROVAL!
        t_other_agent = self._rpc_token(lead_tok, "task.create", {
            "project": prj1, "title": "Esc W2", "spec": "S4", "assignee": "w2", "profile": "elevated",
        })
        self.assertFalse(t_other_agent.get("ok"))
        self.assertEqual(t_other_agent["error"]["code"], c.APPROVAL_REQUIRED)

        # Different project (prj2) on SAME (w1, elevated) MUST STILL REQUIRE APPROVAL!
        t_other_prj = self._rpc_token(lead_tok, "task.create", {
            "project": prj2, "title": "Esc P2", "spec": "S5", "assignee": "w1", "profile": "elevated",
        })
        self.assertFalse(t_other_prj.get("ok"))
        self.assertEqual(t_other_prj["error"]["code"], c.APPROVAL_REQUIRED)

        # --- 2. config does NOT support standing grants (direct-user-only; allow/reject only) ---
        cfg1 = self._rpc_token(lead_tok, "approval.request", {
            "category": "config", "project_id": prj1, "summary": "Set lead.profile",
            "operation": {"key": "lead.profile", "value": "readonly"},
        })
        self.assertTrue(cfg1.get("ok"), cfg1)
        self.assertEqual(cfg1["data"]["status"], "pending")
        self.assertFalse(cfg1["data"].get("granted_by_standing_grant", False))
        cfg_apr = cfg1["data"]["approval_id"]
        cfg_hash = cfg1["data"]["op_hash"]

        # chat.list for config card exposes ONLY ['allow', 'reject'] and always_allow_effect=None
        clist_cfg = self._rpc_user("chat.list", {"project_id": prj1})["data"]
        cfg_view = clist_cfg["approvals_by_id"][cfg_apr]
        self.assertEqual(cfg_view["available_actions"], ["allow", "reject"])
        self.assertIsNone(cfg_view["always_allow_effect"])

        # Clicking always_allow on a config card is explicitly refused and creates NO grant
        cfg_always = self._rpc_user("approval.decide", {
            "approval_id": cfg_apr, "action": "always_allow", "op_hash": cfg_hash,
        })
        self.assertFalse(cfg_always.get("ok"))
        self.assertEqual(cfg_always["error"]["code"], c.INVALID_ARGUMENT)

        # grant.list rejects category='config'
        gl_cfg = self._rpc_user("grant.list", {"category": "config"})
        self.assertFalse(gl_cfg.get("ok"))
        self.assertEqual(gl_cfg["error"]["code"], c.INVALID_ARGUMENT)

        # --- 3. retry_over_limit standing grant: scoped to prj1 ---
        rt1 = self._rpc_token(lead_tok, "task.create", {
            "project": prj1, "title": "Flaky 1", "spec": "S", "assignee": "w1", "max_attempts": 1,
        })["data"]["id"]
        self.assertTrue(self.harness.wait_for(
            lambda: self._rpc_user("task.show", {"task_id": rt1})["data"]["status"] == "queued", timeout=5.0
        ))
        st1 = self._rpc_token(w1_tok, "task.start", {"task_id": rt1})["data"]
        self._rpc_token(w1_tok, "task.fail", {"task_id": rt1, "run": st1["run_id"], "fence": st1["fence"], "reason": "boom"})
        rr1 = self._rpc_token(lead_tok, "task.retry", {"task_id": rt1, "reason": "retry"})
        self.assertFalse(rr1.get("ok"))
        self.assertEqual(rr1["error"]["code"], c.APPROVAL_REQUIRED)
        r_apr = rr1["error"]["details"]["approval_id"]
        r_hash = rr1["error"]["details"]["op_hash"]

        r_dec = self._rpc_user("approval.decide", {
            "approval_id": r_apr, "action": "always_allow", "op_hash": r_hash,
        })
        self.assertTrue(r_dec.get("ok"), r_dec)
        retry_grant = r_dec["data"]["grant"]
        self.assertEqual(retry_grant["category"], "retry_over_limit")
        self.assertEqual(retry_grant["project_id"], prj1)

        # Now task.retry on prj1 succeeds automatically via standing grant (no approval needed!)
        rr2 = self._rpc_token(lead_tok, "task.retry", {"task_id": rt1, "reason": "retry via grant"})
        self.assertTrue(rr2.get("ok"), rr2)
        self.assertEqual(rr2["data"]["status"], "queued")

        # --- 4. grant.list & grant.revoke ---
        glist = self._rpc_user("grant.list", {"project_id": prj1})["data"]["items"]
        self.assertEqual(len(glist), 2)

        rev = self._rpc_user("grant.revoke", {"grant_id": perm_grant["id"], "reason": "no longer needed"})
        self.assertTrue(rev.get("ok"), rev)
        self.assertTrue(rev["data"]["revoked"])
        self.assertFalse(rev["data"]["grant"]["active"])

        # After revocation, (prj1, w1, elevated) MUST REQUIRE APPROVAL AGAIN!
        t_after_rev = self._rpc_token(lead_tok, "task.create", {
            "project": prj1, "title": "Esc After Revoke", "spec": "S6", "assignee": "w1", "profile": "elevated",
        })
        self.assertFalse(t_after_rev.get("ok"))
        self.assertEqual(t_after_rev["error"]["code"], c.APPROVAL_REQUIRED)

        # Active list now has 1 grant; include_revoked=True shows both 2
        self.assertEqual(len(self._rpc_user("grant.list", {"project_id": prj1})["data"]["items"]), 1)
        self.assertEqual(len(self._rpc_user("grant.list", {"project_id": prj1, "include_revoked": True})["data"]["items"]), 2)

        # Verify grant.created, grant.used, grant.revoked events exist and audit.verify passes!
        conn = self.harness.db()
        try:
            ev_types = {r["type"] for r in conn.execute("SELECT DISTINCT type FROM events;").fetchall()}
            self.assertIn("grant.created", ev_types)
            self.assertIn("grant.used", ev_types)
            self.assertIn("grant.revoked", ev_types)
        finally:
            conn.close()

        self._verify_audit()

    def test_launch_lead_immediate_wake_mesh_actions_and_legacy_off_project(self) -> None:
        """Real launch lead 0s wake + mesh-actions + legacy plan_gate_mode='off' + git/profile invariants."""
        self._rpc_user("config.set", {"key": "lead.wake_debounce_s", "value": 60})
        self._rpc_user("config.set", {"key": "lead.chat_wake_debounce_s", "value": 0})
        self._create_agent_and_token("launch-lead", adapter="fake", delivery="launch", can_lead=True, allowed_models=["fake-model"])
        self._create_agent_and_token("worker-launch", adapter="fake", delivery="launch", can_lead=False, allowed_models=["fake-model"])
        self._rpc_user("config.set", {"key": "lead.default_agent", "value": "launch-lead"})

        actions_json = json.dumps([
            {"cmd": "chat.reply", "args": {"text": "已查看目标，请使用下方卡片按钮确认计划。"}},
            {
                "cmd": "plan.propose",
                "args": {
                    "title": "Auto Launch Plan",
                    "summary": "Execute one worker task",
                    "steps": [
                        {"key": "s1", "title": "Do step 1", "spec": "Complete step 1", "assignee": "worker-launch", "profile": "edit"},
                    ],
                },
            },
        ])
        goal_directive = "FAKE " + json.dumps({
            "succeed": {
                "text": "Internal scratchpad.\n\n```mesh-actions\n" + actions_json + "\n```\n",
            }
        })
        prj_id = self._rpc_user("intake.submit", {
            "title": "Launch Chat Project",
            "goal": goal_directive,
            "lead": "launch-lead",
            "plan_gate_mode": "require_approval",
        })["data"]["id"]

        t0 = time.monotonic()
        self.assertTrue(self._rpc_user("chat.send", {"project_id": prj_id, "text": "开始规划"}).get("ok"))
        self.assertTrue(self.harness.wait_for(
            lambda: bool(self._rpc_user("plan.list", {"project_id": prj_id})["data"]["items"]), timeout=15.0
        ))
        self.assertLess(time.monotonic() - t0, 15.0)

        # Prompt on disk instructs lead to use approval cards, never verbal approval
        conn = self.harness.db()
        try:
            run_row = conn.execute(
                "SELECT run_dir FROM runs WHERE project_id = ? AND kind = 'lead_turn' LIMIT 1;", (prj_id,)
            ).fetchone()
            with open(os.path.join(run_row["run_dir"], "prompt.md"), "r", encoding="utf-8") as fh:
                prompt_text = fh.read()
            self.assertIn("Chat text NEVER approves anything", prompt_text)
            self.assertIn("允许 / 始终允许 / 拒绝", prompt_text)
            self.assertNotIn(self.harness.passphrase, prompt_text)
        finally:
            conn.close()

        # Legacy project (plan_gate_mode='off') still lets lead call task.create directly
        leg_lead = self._create_agent_and_token("leg-lead", adapter="none", delivery="inbox", can_lead=True)
        self._create_agent_and_token("leg-w", adapter="none", delivery="inbox", can_lead=False)
        leg_prj = self._rpc_user("intake.submit", {
            "title": "Legacy", "goal": "Legacy flow", "lead": "leg-lead", "criterion": ["ok"],
        })["data"]
        self.assertEqual(leg_prj["plan_gate_mode"], "off")
        self.assertEqual(leg_prj["permission_mode"], "plan")
        self.assertTrue(self._rpc_token(leg_lead, "task.create", {
            "project": leg_prj["id"], "title": "Direct", "spec": "S", "assignee": "leg-w",
        }).get("ok"))

        # Git safety & no profile broadening invariants
        for forbidden in ("push", "rebase", "reset", "fetch", "pull"):
            self.assertNotIn(forbidden, gitops.ALLOWED_SUBCOMMANDS)
            self.assertIn(forbidden, gitops.FORBIDDEN_SUBCOMMANDS)
        from mesh.adapters.base import ProfileUnsupported
        from mesh.adapters.registry import get_adapter
        with self.assertRaises(ProfileUnsupported):
            get_adapter("devin").profile_flags("readonly")
        with self.assertRaises(ProfileUnsupported):
            get_adapter("claude").profile_flags("dev")

        self._verify_audit()

    def test_chat_send_and_list_report_inbox_queued_and_active_lead_turn_status(self) -> None:
        """Issue 1 backend contract: chat.send/list expose lead delivery, queued outbox, and active turn state."""
        # 1. Built-in Inbox Lead (mesh-lead): chat.send immediately reports delivery=inbox and queued_outbox_count >= 1
        self._create_agent_and_token("mesh-lead", adapter="none", delivery="inbox", can_lead=True)
        cfg_res = self._rpc_user("config.set", {"key": "lead.default_agent", "value": "mesh-lead"})
        self.assertTrue(cfg_res.get("ok"), cfg_res)

        send_inbox = self._rpc_user("chat.send", {"text": "整理发布说明"})
        self.assertTrue(send_inbox.get("ok"), send_inbox)
        pid_inbox = send_inbox["data"]["project_id"]
        ls_inbox = send_inbox["data"]["lead_status"]
        self.assertEqual(ls_inbox["agent_name"], "mesh-lead")
        self.assertEqual(ls_inbox["delivery"], "inbox")
        self.assertEqual(ls_inbox["adapter"], "none")
        self.assertGreaterEqual(ls_inbox["queued_outbox_count"], 1)
        self.assertIsNone(ls_inbox["active_run"])

        list_inbox = self._rpc_user("chat.list", {"project_id": pid_inbox})
        self.assertTrue(list_inbox.get("ok"), list_inbox)
        self.assertEqual(list_inbox["data"]["lead_status"]["delivery"], "inbox")
        self.assertGreaterEqual(list_inbox["data"]["lead_status"]["queued_outbox_count"], 1)

        # 2. Launch Lead with active lead_turn run: chat.list reports delivery=launch and active_run state
        self._create_agent_and_token(
            "launch-lead",
            adapter="fake",
            delivery="launch",
            can_lead=True,
            default_profile="readonly",
            allowed_models=["fake-model"],
        )
        self.harness.hold_launch_starts()
        prj_launch = self._rpc_user("intake.submit", {
            "title": "Launch Lead Status",
            "goal": "Check active lead_turn reporting",
            "lead": "launch-lead",
            "plan_gate_mode": "require_approval",
        })
        self.assertTrue(prj_launch.get("ok"), prj_launch)
        pid_launch = prj_launch["data"]["id"]

        send_launch = self._rpc_user("chat.send", {"project_id": pid_launch, "text": "给我一个两步计划"})
        self.assertTrue(send_launch.get("ok"), send_launch)
        ls_launch = send_launch["data"]["lead_status"]
        self.assertEqual(ls_launch["agent_name"], "launch-lead")
        self.assertEqual(ls_launch["delivery"], "launch")
        self.assertEqual(ls_launch["adapter"], "fake")
        self.assertGreaterEqual(ls_launch["queued_outbox_count"], 1)

        # Insert an active running lead_turn row and verify chat.list reports it consistently
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        conn.row_factory = sqlite3.Row
        try:
            ag_row = conn.execute("SELECT id FROM principals WHERE name = 'launch-lead';").fetchone()
            conn.execute(
                """INSERT INTO runs (id, kind, task_id, project_id, agent_id, fence, adapter, status, boot_id, run_dir, started_at)
                   VALUES ('run_lead_status_1', 'lead_turn', NULL, ?, ?, 0, 'fake', 'running', 'boot_test', ?, '2026-11-23T14:00:00Z');""",
                (pid_launch, ag_row["id"], os.path.join(self.harness.home, "runs", "run_lead_status_1")),
            )
            conn.commit()
        finally:
            conn.close()

        try:
            list_launch = self._rpc_user("chat.list", {"project_id": pid_launch})
            self.assertTrue(list_launch.get("ok"), list_launch)
            self.assertIsNotNone(list_launch["data"]["active_lead_run"])
            self.assertEqual(list_launch["data"]["active_lead_run"]["status"], "running")
            self.assertEqual(list_launch["data"]["lead_status"]["active_run"]["id"], "run_lead_status_1")
            self.assertEqual(list_launch["data"]["lead_status"]["active_run"]["status"], "running")
        finally:
            conn_clean = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
            try:
                conn_clean.execute("DELETE FROM runs WHERE id = 'run_lead_status_1';")
                conn_clean.commit()
            finally:
                conn_clean.close()

    def test_relayed_to_direct_card_upgrade_and_approved_to_always_allow_upgrade(self) -> None:
        """P2 relayed->direct card upgrade (allow & always_allow) and P3 approved->always_allow upgrade."""
        lead_tok = self._create_agent_and_token("up-lead", adapter="none", delivery="inbox", can_lead=True)
        relay_tok = self._create_agent_and_token("up-relay", adapter="none", delivery="inbox", capabilities=["relay", "intake"])
        w_tok = self._create_agent_and_token("up-w", adapter="none", delivery="inbox", max_unapproved_profile="dev")

        prj_id = self._rpc_user("intake.submit", {
            "title": "Upgrade Project", "goal": "G", "lead": "up-lead", "plan_gate_mode": "off", "criterion": ["ok"],
        })["data"]["id"]

        # 1. Create a task with max_attempts=1, fail it -> task.retry creates retry_over_limit card
        t1 = self._rpc_token(lead_tok, "task.create", {
            "project": prj_id, "title": "Flaky Up1", "spec": "S", "assignee": "up-w", "max_attempts": 1,
        })["data"]["id"]
        self.assertTrue(self.harness.wait_for(
            lambda: self._rpc_user("task.show", {"task_id": t1})["data"]["status"] == "queued", timeout=5.0
        ))
        st1 = self._rpc_token(w_tok, "task.start", {"task_id": t1})["data"]
        self._rpc_token(w_tok, "task.fail", {"task_id": t1, "run": st1["run_id"], "fence": st1["fence"], "reason": "fail1"})
        rr1 = self._rpc_token(lead_tok, "task.retry", {"task_id": t1, "reason": "retry1"})
        self.assertEqual(rr1["error"]["code"], c.APPROVAL_REQUIRED)
        r_apr1 = rr1["error"]["details"]["approval_id"]
        r_hash1 = rr1["error"]["details"]["op_hash"]

        # Relayer approves it first (relayed_unverified)
        rel_ok = self._rpc_token(relay_tok, "approval.approve", {
            "approval_id": r_apr1, "op_hash": r_hash1,
        }, relayed=True, user_statement="User said retry ok")
        self.assertTrue(rel_ok.get("ok"), rel_ok)
        self.assertEqual(rel_ok["data"]["decided_via"], "relayed")
        self.assertEqual(rel_ok["data"]["trust"], "relayed_unverified")

        # chat.list still exposes 'allow' and 'always_allow' on relayed-approved card!
        clist1 = self._rpc_user("chat.list", {"project_id": prj_id})["data"]
        self.assertIn("allow", clist1["approvals_by_id"][r_apr1]["available_actions"])
        self.assertIn("always_allow", clist1["approvals_by_id"][r_apr1]["available_actions"])

        # Direct user clicks 'allow' on the card -> upgrades relayed -> direct with user_mac & upgraded_from!
        up_allow = self._rpc_user("approval.decide", {
            "approval_id": r_apr1, "action": "allow", "op_hash": r_hash1,
        })
        self.assertTrue(up_allow.get("ok"), up_allow)
        self.assertEqual(up_allow["data"]["decided_via"], "direct")
        self.assertEqual(up_allow["data"]["trust"], "direct_verified")
        self.assertEqual(up_allow["data"]["upgraded_from"], "relayed")
        self.assertTrue(up_allow["data"]["user_mac"])
        self.assertFalse(up_allow["data"].get("already_decided", False))

        # Verify event log has approval.decided with upgraded_from='relayed'
        conn = self.harness.db()
        try:
            evs = conn.execute(
                "SELECT via, payload_json FROM events WHERE approval_id = ? AND type = 'approval.decided' ORDER BY seq ASC;",
                (r_apr1,),
            ).fetchall()
            self.assertEqual(len(evs), 2)
            self.assertEqual(evs[0]["via"], "relayed")
            self.assertEqual(evs[1]["via"], "direct")
            self.assertEqual(json.loads(evs[1]["payload_json"]).get("upgraded_from"), "relayed")
        finally:
            conn.close()

        # 2. Direct-approved permission_escalation card upgraded to 'always_allow' (decision stays approved, grant created)
        esc = self._rpc_token(lead_tok, "task.create", {
            "project": prj_id, "title": "Esc Up", "spec": "S", "assignee": "up-w", "profile": "elevated",
        })
        self.assertEqual(esc["error"]["code"], c.APPROVAL_REQUIRED)
        e_apr = esc["error"]["details"]["approval_id"]
        e_hash = esc["error"]["details"]["op_hash"]

        dec_allow = self._rpc_user("approval.decide", {
            "approval_id": e_apr, "action": "allow", "op_hash": e_hash,
        })
        self.assertTrue(dec_allow.get("ok"), dec_allow)
        orig_mac = dec_allow["data"]["user_mac"]

        # chat.list shows 'always_allow' still available on unconsumed approved card
        clist2 = self._rpc_user("chat.list", {"project_id": prj_id})["data"]
        self.assertEqual(clist2["approvals_by_id"][e_apr]["available_actions"], ["always_allow"])

        # Clicking 'always_allow' on the already-approved card keeps decision approved, creates grant, no duplicate approval.decided!
        dec_up = self._rpc_user("approval.decide", {
            "approval_id": e_apr, "action": "always_allow", "op_hash": e_hash,
        })
        self.assertTrue(dec_up.get("ok"), dec_up)
        self.assertEqual(dec_up["data"]["status"], "approved")
        self.assertTrue(dec_up["data"]["upgraded_to_always_allow"])
        self.assertTrue(dec_up["data"]["already_decided"])
        self.assertEqual(dec_up["data"]["user_mac"], orig_mac)
        self.assertTrue(dec_up["data"]["grant_created"])
        self.assertEqual(dec_up["data"]["grant"]["category"], "permission_escalation")

        conn = self.harness.db()
        try:
            dec_cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM events WHERE approval_id = ? AND type = 'approval.decided';",
                (e_apr,),
            ).fetchone()["cnt"]
            self.assertEqual(dec_cnt, 1)
        finally:
            conn.close()

        # Standing grant now authorizes future (prj_id, up-w, elevated) tasks immediately!
        t_after = self._rpc_token(lead_tok, "task.create", {
            "project": prj_id, "title": "Esc After Upgrade", "spec": "S2", "assignee": "up-w", "profile": "elevated",
        })
        self.assertTrue(t_after.get("ok"), t_after)

        # 3. Relayed-approved retry_over_limit card upgraded directly via 'always_allow' -> upgrades to direct AND creates grant!
        self.assertTrue(self._rpc_token(lead_tok, "task.retry", {"task_id": t1, "reason": "retry1"}, approval=r_apr1).get("ok"))
        st2 = self._rpc_token(w_tok, "task.start", {"task_id": t1})["data"]
        self._rpc_token(w_tok, "task.fail", {"task_id": t1, "run": st2["run_id"], "fence": st2["fence"], "reason": "fail2"})
        rr2 = self._rpc_token(lead_tok, "task.retry", {"task_id": t1, "reason": "retry2"})
        self.assertEqual(rr2["error"]["code"], c.APPROVAL_REQUIRED)
        r_apr2 = rr2["error"]["details"]["approval_id"]
        r_hash2 = rr2["error"]["details"]["op_hash"]
        self.assertTrue(self._rpc_token(relay_tok, "approval.approve", {
            "approval_id": r_apr2, "op_hash": r_hash2,
        }, relayed=True, user_statement="ok again").get("ok"))

        up_always = self._rpc_user("approval.decide", {
            "approval_id": r_apr2, "action": "always_allow", "op_hash": r_hash2,
        })
        self.assertTrue(up_always.get("ok"), up_always)
        self.assertEqual(up_always["data"]["decided_via"], "direct")
        self.assertEqual(up_always["data"]["upgraded_from"], "relayed")
        self.assertTrue(up_always["data"]["upgraded_to_always_allow"])
        self.assertTrue(up_always["data"]["grant_created"])
        self.assertEqual(up_always["data"]["grant"]["category"], "retry_over_limit")

        # 4. Compound task.retry (retry_over_limit + permission_escalation) enumerates BOTH scopes in preview & creates both exact grants
        prj_cmp = self._rpc_user("intake.submit", {
            "title": "Compound Prj", "goal": "G", "lead": "up-lead", "plan_gate_mode": "off", "criterion": ["ok"],
        })["data"]["id"]
        t_cmp = self._rpc_user("task.create", {
            "project": prj_cmp, "title": "Elev Retry", "spec": "S", "assignee": "up-w", "profile": "elevated", "max_attempts": 1,
        })["data"]["id"]
        self.assertTrue(self.harness.wait_for(
            lambda: self._rpc_user("task.show", {"task_id": t_cmp})["data"]["status"] == "queued", timeout=5.0
        ))
        st_cmp = self._rpc_token(w_tok, "task.start", {"task_id": t_cmp})["data"]
        self._rpc_token(w_tok, "task.fail", {"task_id": t_cmp, "run": st_cmp["run_id"], "fence": st_cmp["fence"], "reason": "boom"})
        rr_cmp = self._rpc_token(lead_tok, "task.retry", {"task_id": t_cmp, "reason": "retry elevated"})
        self.assertEqual(rr_cmp["error"]["code"], c.APPROVAL_REQUIRED)
        cmp_apr = rr_cmp["error"]["details"]["approval_id"]
        cmp_hash = rr_cmp["error"]["details"]["op_hash"]

        clist_cmp = self._rpc_user("chat.list", {"project_id": prj_cmp})["data"]
        cmp_eff = clist_cmp["approvals_by_id"][cmp_apr]["always_allow_effect"]
        self.assertIsNotNone(cmp_eff)
        self.assertEqual(cmp_eff["kind"], "standing_grant")
        self.assertEqual({s["category"] for s in cmp_eff["scopes"]}, {"retry_over_limit", "permission_escalation"})

        dec_cmp = self._rpc_user("approval.decide", {
            "approval_id": cmp_apr, "action": "always_allow", "op_hash": cmp_hash,
        })
        self.assertTrue(dec_cmp.get("ok"), dec_cmp)
        self.assertEqual(
            {g["scope_key"] for g in dec_cmp["data"]["grants"]},
            {s["scope_key"] for s in cmp_eff["scopes"]},
        )

        self._verify_audit()


if __name__ == "__main__":
    unittest.main()
