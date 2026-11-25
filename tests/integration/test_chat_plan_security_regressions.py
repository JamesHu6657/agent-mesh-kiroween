"""Security regression tests for card-based approvals, permission modes, standing grants, and migration.

Covers: actor forgery (lead, relayer, run-token, delegated), single-use / replay,
plan swap / plan tamper / TOCTOU, plan freeze, LEAD_CANNOT_BE_ASSIGNEE, no profile broadening,
grant & permission_mode tampering caught by audit.verify, and migration lock + backup.
"""

import json
import os
import sqlite3
import subprocess
import time
import unittest

import mesh.constants as c
from mesh.client import MeshClient
from mesh.core.db import connect, get_schema_version, transaction
from mesh.core.schema import INITIAL_SCHEMA_STATEMENTS
from tests.helpers import MeshHarness


class TestApprovalSecurityRegressions(unittest.TestCase):
    """End-to-end daemon security regression suite."""

    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)
        self.client = MeshClient(self.harness.home, timeout=30.0)

    def tearDown(self) -> None:
        self.harness.teardown()

    def u(self, cmd: str, args: dict = None, *, approval: str = None) -> dict:
        req = {
            "v": 1,
            "cmd": cmd,
            "args": args or {},
            "auth": {"type": "user", "passphrase": self.harness.passphrase},
        }
        if approval:
            req["approval"] = approval
        return self.client.send_request(req)

    def t(
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

    def agent(
        self,
        name: str,
        *,
        adapter: str = "fake",
        delivery: str = "launch",
        can_lead: bool = False,
        caps: list = None,
        default_profile: str = "edit",
        max_unapproved: str = "dev",
        models: list = None,
    ) -> str:
        r = self.u("agent.create", {
            "name": name,
            "adapter": adapter,
            "delivery": delivery,
            "can_lead": can_lead,
            "max_concurrency": 2,
            "default_profile": default_profile,
            "max_unapproved_profile": max_unapproved,
            "capabilities": caps if caps is not None else ["intake"],
            "allowed_models": models if models is not None else (["fake-model"] if delivery == "launch" else []),
        })
        self.assertTrue(r.get("ok"), r)
        return self.u("agent.token.issue", {"name": name, "ttl": "1h"})["data"]["token"]

    def project(self, lead: str, mode: str = "require_approval", perm: str = "plan", **kw) -> str:
        args = {"title": "P", "goal": "G", "lead": lead, "plan_gate_mode": mode, "permission_mode": perm}
        if mode == "off":
            args["criterion"] = ["done"]
        args.update(kw)
        r = self.u("intake.submit", args)
        self.assertTrue(r.get("ok"), r)
        return r["data"]["id"]

    def verify_audit(self) -> dict:
        r = self.u("audit.verify")
        self.assertTrue(r.get("ok"), r)
        return r["data"]

    # ------------------------------------------------------------------
    # 1. Actor forgery: lead, relayer, worker, delegated NEVER approve plans,
    #    call approval.decide, change permission_mode, or list/revoke grants
    # ------------------------------------------------------------------
    def test_actor_forgery_non_user_cannot_decide_change_mode_or_manage_grants(self) -> None:
        lead_tok = self.agent("lead-af", adapter="none", delivery="inbox", can_lead=True)
        relay_tok = self.agent("relayer-af", adapter="none", delivery="inbox", caps=["relay", "intake"])
        worker_tok = self.agent("w-af", adapter="none", delivery="inbox")
        prj = self.project("lead-af")

        pr = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "T", "summary": "S",
            "steps": [{"key": "s1", "title": "Step", "spec": "Work", "assignee": "w-af"}],
        })["data"]
        plan = pr["plan"]
        op_hash = pr["approval"]["op_hash"]

        # Lead, worker, and relayer CANNOT call approval.decide on plan card
        for tok, relayed in ((lead_tok, False), (worker_tok, False), (relay_tok, False), (relay_tok, True)):
            r = self.t(tok, "approval.decide", {
                "approval_id": plan["approval_id"], "action": "allow", "plan_hash": plan["plan_hash"],
            }, relayed=relayed)
            self.assertFalse(r.get("ok"), f"Actor forgery succeeded: relayed={relayed}: {r}")
            self.assertIn(r["error"]["code"], (c.CAPABILITY_MISSING, c.RELAY_NOT_ALLOWED))

        # Relayer / delegated lead CANNOT use approval.approve on plan_dispatch
        r_rel = self.t(relay_tok, "approval.approve", {
            "approval_id": plan["approval_id"], "op_hash": op_hash, "plan_hash": plan["plan_hash"],
        }, relayed=True)
        self.assertFalse(r_rel.get("ok"))
        self.assertEqual(r_rel["error"]["code"], c.CHANNEL_NOT_ALLOWED)

        self.u("config.set", {"key": "approval.mode", "value": "delegated"})
        r_del = self.t(lead_tok, "approval.approve", {
            "approval_id": plan["approval_id"], "op_hash": op_hash, "plan_hash": plan["plan_hash"],
            "human_confirmed": "user said ok",
        })
        self.assertFalse(r_del.get("ok"))
        self.assertEqual(r_del["error"]["code"], c.CHANNEL_NOT_ALLOWED)
        self.u("config.set", {"key": "approval.mode", "value": "passphrase"})

        # Non-user CANNOT change permission_mode or create an auto/off sibling project
        for tok, relayed in ((lead_tok, False), (worker_tok, False), (relay_tok, True)):
            pm = self.t(tok, "project.set_permission_mode", {
                "project_id": prj, "permission_mode": "auto",
            }, relayed=relayed)
            self.assertFalse(pm.get("ok"))
            self.assertIn(pm["error"]["code"], (c.CAPABILITY_MISSING, c.RELAY_NOT_ALLOWED))

        bad_auto_prj = self.t(lead_tok, "intake.submit", {
            "title": "AutoBypass", "goal": "G", "lead": "lead-af", "permission_mode": "auto", "criterion": ["x"],
        })
        self.assertFalse(bad_auto_prj.get("ok"))
        self.assertEqual(bad_auto_prj["error"]["code"], c.CAPABILITY_MISSING)

        bad_off_prj = self.t(lead_tok, "intake.submit", {
            "title": "OffBypass", "goal": "G", "lead": "lead-af", "plan_gate_mode": "off", "criterion": ["x"],
        })
        self.assertFalse(bad_off_prj.get("ok"))
        self.assertEqual(bad_off_prj["error"]["code"], c.CAPABILITY_MISSING)

        # Non-user CANNOT call grant.list or grant.revoke
        for tok, relayed in ((lead_tok, False), (relay_tok, True)):
            gl = self.t(tok, "grant.list", {"project_id": prj}, relayed=relayed)
            self.assertFalse(gl.get("ok"))
            self.assertIn(gl["error"]["code"], (c.CAPABILITY_MISSING, c.RELAY_NOT_ALLOWED))
            gr = self.t(tok, "grant.revoke", {"grant_id": "grt_0000000000aaaaaaaaaaaaaaaa"}, relayed=relayed)
            self.assertFalse(gr.get("ok"))
            self.assertIn(gr["error"]["code"], (c.CAPABILITY_MISSING, c.RELAY_NOT_ALLOWED))

        # Run token (both executor and lead role) CANNOT call approval.decide, approval.approve,
        # project.set_permission_mode, grant.list, or grant.revoke
        from mesh.core.authz import check_authz
        from mesh.errors import MeshError

        conn = self.harness.db()
        try:
            lead_row = conn.execute(
                "SELECT la.id AS asg_id, la.agent_id, p.name FROM lead_assignments la JOIN principals p ON p.id = la.agent_id "
                "WHERE la.project_id = ? AND la.ended_at IS NULL LIMIT 1;",
                (prj,),
            ).fetchone()
            lead_run_principal = {
                "id": lead_row["agent_id"],
                "name": lead_row["name"],
                "kind": "agent",
                "scope": {"role": "lead", "project_id": prj, "lead_assignment_id": lead_row["asg_id"], "run_id": "run_fake1"},
            }
            exec_run_principal = {
                "id": "agt_exec",
                "name": "w-af",
                "kind": "agent",
                "scope": {"role": "executor", "project_id": prj, "task_id": "tsk_fake1", "run_id": "run_fake2"},
            }
            for rt_principal in (lead_run_principal, exec_run_principal):
                for forbidden_cmd in ("approval.decide", "approval.approve", "approval.reject", "project.set_permission_mode", "grant.list", "grant.revoke"):
                    with self.assertRaises(MeshError) as mctx:
                        check_authz(conn, forbidden_cmd, rt_principal, "agent", "run_token", project_id=prj)
                    self.assertEqual(mctx.exception.code, c.CAPABILITY_MISSING)
        finally:
            conn.close()

        # Plan remained proposed throughout
        self.assertEqual(self.u("plan.show", {"plan_id": plan["id"]})["data"]["status"], "proposed")
        self.verify_audit()

    # ------------------------------------------------------------------
    # 2. Replay, single-use, plan swap, plan hash mismatch, expiry TOCTOU
    # ------------------------------------------------------------------
    def test_plan_swap_toctou_replay_single_use_and_expiry(self) -> None:
        lead_tok = self.agent("lead-rep", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-rep")
        self.harness.hold_launch_starts()
        prj = self.project("lead-rep")

        p1 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Benign v1", "summary": "S1",
            "steps": [{"key": "s1", "title": "Fix typo", "spec": "typo", "assignee": "w-rep"}],
        })["data"]["plan"]
        p2 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Different v2", "summary": "S2",
            "steps": [{"key": "s1", "title": "Rewrite auth", "spec": "rewrite", "assignee": "w-rep", "profile": "dev"}],
        })["data"]["plan"]

        # Clicking superseded v1 card -> APPROVAL_CANCELLED
        c_v1 = self.u("approval.decide", {
            "approval_id": p1["approval_id"], "action": "allow", "plan_hash": p1["plan_hash"],
        })
        self.assertFalse(c_v1.get("ok"))
        self.assertEqual(c_v1["error"]["code"], c.APPROVAL_CANCELLED)

        # Clicking v2 card with v1's plan_hash -> OP_HASH_MISMATCH!
        swap = self.u("approval.decide", {
            "approval_id": p2["approval_id"], "action": "allow", "plan_hash": p1["plan_hash"],
        })
        self.assertFalse(swap.get("ok"))
        self.assertEqual(swap["error"]["code"], c.OP_HASH_MISMATCH)
        self.assertEqual(self.u("plan.show", {"plan_id": p2["id"]})["data"]["status"], "proposed")

        # Clicking v2 with exact plan_hash succeeds; dispatch consumes single-use approval
        ok_v2 = self.u("approval.decide", {
            "approval_id": p2["approval_id"], "action": "allow", "plan_hash": p2["plan_hash"],
        })
        self.assertTrue(ok_v2.get("ok"), ok_v2)

        # Upgrading an unconsumed approved plan card to always_allow still requires confirm_auto_mode=true
        ask_always = self.u("approval.decide", {
            "approval_id": p2["approval_id"], "action": "always_allow", "plan_hash": p2["plan_hash"],
        })
        self.assertTrue(ask_always.get("ok"), ask_always)
        self.assertTrue(ask_always["data"]["confirmation_required"])
        self.assertEqual(self.u("project.show", {"project_id": prj})["data"]["permission_mode"], "plan")

        disp = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": p2["id"]}, approval=p2["approval_id"])
        self.assertTrue(disp.get("ok"), disp)

        # Replay plan.dispatch -> INVALID_TRANSITION; replay allow or always_allow -> APPROVAL_CONSUMED
        disp2 = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": p2["id"]}, approval=p2["approval_id"])
        self.assertFalse(disp2.get("ok"))
        self.assertEqual(disp2["error"]["code"], c.INVALID_TRANSITION)

        dec_consumed = self.u("approval.decide", {
            "approval_id": p2["approval_id"], "action": "allow", "plan_hash": p2["plan_hash"],
        })
        self.assertFalse(dec_consumed.get("ok"))
        self.assertEqual(dec_consumed["error"]["code"], c.APPROVAL_CONSUMED)

        always_consumed = self.u("approval.decide", {
            "approval_id": p2["approval_id"], "action": "always_allow", "plan_hash": p2["plan_hash"], "confirm_auto_mode": True,
        })
        self.assertFalse(always_consumed.get("ok"))
        self.assertEqual(always_consumed["error"]["code"], c.APPROVAL_CONSUMED)

        # TTL expiry syncs plan to cancelled and refuses approval.decide with APPROVAL_EXPIRED
        self.u("config.set", {"key": "approval.ttl_seconds", "value": 1})
        p3 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Expiring", "summary": "S3",
            "steps": [{"key": "s1", "title": "x", "spec": "y", "assignee": "w-rep"}],
        })["data"]["plan"]
        time.sleep(2.1)
        exp = self.u("approval.decide", {
            "approval_id": p3["approval_id"], "action": "allow", "plan_hash": p3["plan_hash"],
        })
        self.assertFalse(exp.get("ok"))
        self.assertEqual(exp["error"]["code"], c.APPROVAL_EXPIRED)
        ps3 = self.u("plan.show", {"plan_id": p3["id"]})["data"]
        self.assertEqual(ps3["status"], "cancelled")
        self.assertEqual(ps3["approval"]["status"], "expired")

        self.verify_audit()

    # ------------------------------------------------------------------
    # 3. Plan freeze & LEAD_CANNOT_BE_ASSIGNEE
    # ------------------------------------------------------------------
    def test_plan_freeze_and_lead_cannot_be_assignee(self) -> None:
        lead_tok = self.agent("lead-frz", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-safe", default_profile="readonly", max_unapproved="dev", models=["fake-model"])
        self.agent("w-other", default_profile="edit", max_unapproved="dev", models=["fake-model", "other-model"])
        self.harness.hold_launch_starts()
        prj = self.project("lead-frz")

        # LEAD_CANNOT_BE_ASSIGNEE enforced at plan.propose
        self_assign = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Self", "summary": "S",
            "steps": [{"key": "s1", "title": "Self", "spec": "x", "assignee": "lead-frz"}],
        })
        self.assertFalse(self_assign.get("ok"))
        self.assertEqual(self_assign["error"]["code"], c.LEAD_CANNOT_BE_ASSIGNEE)

        p = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Frozen", "summary": "S",
            "steps": [
                {"key": "s1", "title": "Audit", "spec": "x", "assignee": "w-safe", "profile": "readonly"},
                {"key": "s2", "title": "Next", "spec": "y", "assignee": "w-safe", "profile": "readonly", "depends_on": ["s1"]},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p["approval_id"], "action": "allow", "plan_hash": p["plan_hash"],
        }).get("ok"))
        d = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": p["id"]}, approval=p["approval_id"])["data"]
        t1 = next(t for t in d["tasks"] if t["plan_step_key"] == "s1")
        t2 = next(t for t in d["tasks"] if t["plan_step_key"] == "s2")

        # Lead cannot rewrite assignee, profile, deps, or cancel dispatched plan tasks
        self.assertEqual(self.t(lead_tok, "task.reassign", {
            "task_id": t1["id"], "to": "w-other", "profile": "dev", "reason": "change",
        })["error"]["code"], c.PLAN_APPROVAL_REQUIRED)
        self.assertEqual(self.t(lead_tok, "dep.remove", {
            "task_id": t2["id"], "depends_on": t1["id"],
        })["error"]["code"], c.PLAN_APPROVAL_REQUIRED)
        self.assertEqual(self.t(lead_tok, "task.cancel", {
            "task_id": t2["id"], "reason": "skip",
        })["error"]["code"], c.PLAN_APPROVAL_REQUIRED)

        # Direct user CAN cancel
        self.assertTrue(self.u("task.cancel", {"task_id": t2["id"], "reason": "user skip"}).get("ok"))
        self.verify_audit()

    # ------------------------------------------------------------------
    # 4. Tampering detection: plan_steps, approvals.user_mac, standing_grants,
    #    projects.permission_mode, chat_messages, tasks.plan_id all caught!
    # ------------------------------------------------------------------
    def test_tampering_caught_by_runtime_and_audit_verify(self) -> None:
        for target in (
            "plan_steps_before_dispatch",
            "approval_user_mac",
            "grant_profile_widen",
            "project_perm_mode",
            "project_plan_gate_mode",
            "project_plan_gate_mode_tamper_and_restore",
            "auto_dispatch_then_restore_mode",
            "chat_body",
        ):
            with self.subTest(tamper=target):
                h = MeshHarness(init_db=True, start_daemon=True, check_invariants_on_teardown=False)
                try:
                    cl = MeshClient(h.home, timeout=30.0)

                    def uc(cmd, args=None):
                        return cl.send_request({
                            "v": 1, "cmd": cmd, "args": args or {},
                            "auth": {"type": "user", "passphrase": h.passphrase},
                        })

                    def tc(tok, cmd, args=None, approval=None):
                        req = {"v": 1, "cmd": cmd, "args": args or {}, "auth": {"type": "token", "token": tok}}
                        if approval:
                            req["approval"] = approval
                        return cl.send_request(req)

                    h.hold_launch_starts()
                    uc("agent.create", {"name": "l1", "adapter": "none", "delivery": "inbox", "can_lead": True, "max_concurrency": 1})
                    uc("agent.create", {"name": "w1", "adapter": "fake", "delivery": "launch", "can_lead": False, "max_concurrency": 1, "allowed_models": ["fake-model"], "max_unapproved_profile": "dev"})
                    ltok = uc("agent.token.issue", {"name": "l1", "ttl": "1h"})["data"]["token"]
                    prj = uc("intake.submit", {"title": "T", "goal": "G", "lead": "l1", "plan_gate_mode": "require_approval"})["data"]["id"]
                    p = tc(ltok, "plan.propose", {
                        "project_id": prj, "title": "P", "summary": "S",
                        "steps": [{"key": "s1", "title": "a", "spec": "bbbb", "assignee": "w1", "profile": "elevated"}],
                    })["data"]["plan"]
                    if target != "auto_dispatch_then_restore_mode":
                        uc("approval.decide", {"approval_id": p["approval_id"], "action": "allow", "plan_hash": p["plan_hash"]})

                    db_path = os.path.join(h.home, "mesh.db")
                    if target == "plan_steps_before_dispatch":
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE plans SET steps_json = replace(steps_json, 'bbbb', 'evil') WHERE id = ?;", (p["id"],))
                        conn.commit()
                        conn.close()
                        # Runtime plan.dispatch MUST reject tampered steps (TOCTOU)!
                        d_bad = tc(ltok, "plan.dispatch", {"project_id": prj, "plan_id": p["id"]}, approval=p["approval_id"])
                        self.assertFalse(d_bad.get("ok"))
                        self.assertEqual(d_bad["error"]["code"], c.OP_HASH_MISMATCH)
                    elif target == "approval_user_mac":
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE approvals SET user_mac = '0000deadbeef' WHERE id = ?;", (p["approval_id"],))
                        conn.commit()
                        conn.close()
                    elif target == "grant_profile_widen":
                        disp = tc(ltok, "plan.dispatch", {"project_id": prj, "plan_id": p["id"]}, approval=p["approval_id"])["data"]
                        esc = disp["escalated_approvals"][0]
                        g = uc("approval.decide", {
                            "approval_id": esc["approval_id"], "action": "always_allow", "op_hash": esc["op_hash"],
                        })["data"]["grant"]
                        self.assertTrue(uc("audit.verify").get("ok"))
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE standing_grants SET scope_profile = 'unrestricted' WHERE id = ?;", (g["id"],))
                        conn.commit()
                        conn.close()
                    elif target == "project_perm_mode":
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE projects SET permission_mode = 'auto' WHERE id = ?;", (prj,))
                        conn.commit()
                        conn.close()
                    elif target == "project_plan_gate_mode":
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE projects SET plan_gate_mode = 'off' WHERE id = ?;", (prj,))
                        conn.commit()
                        conn.close()
                    elif target == "project_plan_gate_mode_tamper_and_restore":
                        # Flip plan_gate_mode -> 'off', let lead bypass gate with raw task.create, then restore -> audit.verify still catches it!
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE projects SET plan_gate_mode = 'off' WHERE id = ?;", (prj,))
                        conn.commit()
                        conn.close()
                        raw_t = tc(ltok, "task.create", {
                            "project": prj, "title": "Sneaky", "spec": "s", "kind": "general", "assignee": "w1",
                        })
                        self.assertTrue(raw_t.get("ok"), raw_t)
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE projects SET plan_gate_mode = 'require_approval' WHERE id = ?;", (prj,))
                        conn.commit()
                        conn.close()
                    elif target == "auto_dispatch_then_restore_mode":
                        # Flip permission_mode -> 'auto', auto-dispatch unapproved proposed plan, then restore -> 'plan' -> audit.verify MUST catch it!
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE projects SET permission_mode = 'auto' WHERE id = ?;", (prj,))
                        conn.commit()
                        conn.close()
                        auto_d = tc(ltok, "plan.dispatch", {"project_id": prj, "plan_id": p["id"]})
                        self.assertTrue(auto_d.get("ok"), auto_d)
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE projects SET permission_mode = 'plan' WHERE id = ?;", (prj,))
                        conn.commit()
                        conn.close()
                    elif target == "chat_body":
                        cm = uc("chat.send", {"project_id": prj, "text": "hello"})["data"]["message"]
                        conn = sqlite3.connect(db_path)
                        conn.execute("UPDATE chat_messages SET body = 'forged' WHERE id = ?;", (cm["id"],))
                        conn.commit()
                        conn.close()

                    bad = uc("audit.verify")
                    self.assertFalse(bad.get("ok"), f"audit.verify missed tampering of {target}")
                    self.assertEqual(bad["error"]["code"], c.AUDIT_MISMATCH)
                finally:
                    h.teardown()

    # ------------------------------------------------------------------
    # 5. P1: Injected params.gates via approval.request CANNOT mint hidden grants
    # ------------------------------------------------------------------
    def test_p1_injected_gates_cannot_mint_hidden_grants(self) -> None:
        lead_tok = self.agent("lead-g1", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-g1", adapter="none", delivery="inbox", max_unapproved="dev")
        prj = self.project("lead-g1", mode="off")

        # Attack 1: permission_escalation with injected gates=["config", "retry_over_limit"]
        req1 = self.t(lead_tok, "approval.request", {
            "category": "permission_escalation",
            "project_id": prj,
            "summary": "Innocent escalation",
            "operation": {
                "to": "w-g1",
                "profile": "elevated",
                "key": "approval.mode",
                "gates": ["config", "retry_over_limit"],
            },
        })
        self.assertTrue(req1.get("ok"), req1)
        aid1 = req1["data"]["approval_id"]
        oh1 = req1["data"]["op_hash"]

        # chat.list refuses to advertise always_allow and returns always_allow_effect=None
        clist = self.u("chat.list", {"project_id": prj})["data"]
        self.assertEqual(clist["approvals_by_id"][aid1]["available_actions"], ["allow", "reject"])
        self.assertIsNone(clist["approvals_by_id"][aid1]["always_allow_effect"])

        # Clicking always_allow MUST FAIL and mint ZERO standing grants
        dec1 = self.u("approval.decide", {"approval_id": aid1, "action": "always_allow", "op_hash": oh1})
        self.assertFalse(dec1.get("ok"))
        self.assertEqual(dec1["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(self.u("grant.list", {"project_id": prj})["data"]["items"], [])

        # Attack 2: external retry_over_limit with injected gates=["permission_escalation"]
        req2 = self.t(lead_tok, "approval.request", {
            "category": "retry_over_limit",
            "project_id": prj,
            "summary": "Fake compound",
            "operation": {"to": "w-g1", "profile": "elevated", "gates": ["permission_escalation"]},
        })
        self.assertTrue(req2.get("ok"), req2)
        aid2 = req2["data"]["approval_id"]
        oh2 = req2["data"]["op_hash"]
        dec2 = self.u("approval.decide", {"approval_id": aid2, "action": "always_allow", "op_hash": oh2})
        self.assertFalse(dec2.get("ok"))
        self.assertEqual(dec2["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(self.u("grant.list", {"project_id": prj})["data"]["items"], [])

        self.verify_audit()

    # ------------------------------------------------------------------
    # 6. P2: Agents/leads CANNOT retract user-signed approved plans or approvals
    # ------------------------------------------------------------------
    def test_p2_agents_cannot_retract_user_signed_approvals_or_approved_plans(self) -> None:
        lead_tok = self.agent("lead-nr", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-nr", adapter="fake", delivery="launch", max_unapproved="dev")
        self.harness.hold_launch_starts()
        prj = self.project("lead-nr", mode="require_approval", perm="supervised")

        # Lead proposes p1; while pending, lead CAN cancel p1
        p1 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Pending P1", "summary": "S",
            "steps": [{"key": "s1", "title": "A", "spec": "X", "assignee": "w-nr"}],
        })["data"]["plan"]
        self.assertTrue(self.t(lead_tok, "plan.cancel", {"project_id": prj, "plan_id": p1["id"]}).get("ok"))

        # Lead proposes p2 -> direct user approves plan card (approved_pending_dispatch)
        p2 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Approved P2", "summary": "S",
            "steps": [
                {"key": "s1", "title": "Normal", "spec": "X", "assignee": "w-nr", "profile": "edit"},
                {"key": "s2", "title": "Elevated", "spec": "Y", "assignee": "w-nr", "profile": "elevated"},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p2["approval_id"], "action": "allow", "plan_hash": p2["plan_hash"],
        }).get("ok"))

        # Lead CANNOT cancel the approved plan via plan.cancel
        c_plan = self.t(lead_tok, "plan.cancel", {"project_id": prj, "plan_id": p2["id"], "reason": "changed mind"})
        self.assertFalse(c_plan.get("ok"))
        self.assertEqual(c_plan["error"]["code"], c.CAPABILITY_MISSING)

        # Lead CANNOT cancel the approved plan_dispatch approval via approval.cancel
        c_apr = self.t(lead_tok, "approval.cancel", {"approval_id": p2["approval_id"]})
        self.assertFalse(c_apr.get("ok"))
        self.assertEqual(c_apr["error"]["code"], c.CAPABILITY_MISSING)

        # Lead CANNOT supersede the approved plan by calling plan.propose
        sup = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Override P3", "summary": "S",
            "steps": [{"key": "s1", "title": "Other", "spec": "Z", "assignee": "w-nr"}],
        })
        self.assertFalse(sup.get("ok"))
        self.assertEqual(sup["error"]["code"], c.INVALID_TRANSITION)

        # Dispatch p2 -> creates step_dispatch (s1) and permission_escalation (s2) cards
        disp = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": p2["id"]}, approval=p2["approval_id"])
        self.assertTrue(disp.get("ok"), disp)
        s1_apr = next(sa for sa in disp["data"]["step_approvals"] if sa["step_key"] == "s1")
        s2_apr = next(sa for sa in disp["data"]["step_approvals"] if sa["step_key"] == "s2")
        self.assertEqual(s1_apr["category"], "step_dispatch")
        self.assertEqual(s2_apr["category"], "permission_escalation")

        # User approves both cards
        self.assertTrue(self.u("approval.decide", {
            "approval_id": s1_apr["approval_id"], "action": "allow", "op_hash": s1_apr["op_hash"],
        }).get("ok"))
        self.assertTrue(self.u("approval.decide", {
            "approval_id": s2_apr["approval_id"], "action": "allow", "op_hash": s2_apr["op_hash"],
        }).get("ok"))

        # Lead/requester CANNOT retract either user-signed approved card!
        for aid in (s1_apr["approval_id"], s2_apr["approval_id"]):
            c_step = self.t(lead_tok, "approval.cancel", {"approval_id": aid})
            self.assertFalse(c_step.get("ok"), f"Lead retracted approved {aid}")
            self.assertEqual(c_step["error"]["code"], c.CAPABILITY_MISSING)

        # Direct user CAN cancel an unconsumed approved card AND an approved plan
        self.assertTrue(self.u("approval.cancel", {"approval_id": s1_apr["approval_id"]}).get("ok"))
        p3 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "User Cancel P3", "summary": "S",
            "steps": [{"key": "s1", "title": "A", "spec": "X", "assignee": "w-nr"}],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p3["approval_id"], "action": "allow", "plan_hash": p3["plan_hash"],
        }).get("ok"))
        self.assertTrue(self.u("plan.cancel", {"project_id": prj, "plan_id": p3["id"], "reason": "user retract"}).get("ok"))

        self.verify_audit()

    # ------------------------------------------------------------------
    # 7. P3: Relayed chat.send scoped to relayer's own originated projects and visibly marked
    # ------------------------------------------------------------------
    def test_p3_relayed_chat_send_scoped_to_origin_and_visibly_marked(self) -> None:
        self.agent("lead-rc", adapter="none", delivery="inbox", can_lead=True)
        relay_a = self.agent("relayer-a", adapter="none", delivery="inbox", caps=["relay", "intake"])
        relay_b = self.agent("relayer-b", adapter="none", delivery="inbox", caps=["relay", "intake"])

        # User-originated auto-mode project
        prj_user = self.project("lead-rc", mode="require_approval", perm="auto")

        # Relayer A originates its own project via relayed intake.submit
        prj_a_resp = self.t(relay_a, "intake.submit", {
            "title": "Relayer A Project", "goal": "From A", "lead": "lead-rc",
            "plan_gate_mode": "require_approval",
        }, relayed=True, user_statement="Start project from A")
        self.assertTrue(prj_a_resp.get("ok"), prj_a_resp)
        prj_a = prj_a_resp["data"]["id"]

        # Relayer A CANNOT inject relayed chat into user's auto project -> SCOPE_MISMATCH!
        inj_user = self.t(relay_a, "chat.send", {
            "project_id": prj_user, "text": "Injected prompt into auto project",
        }, relayed=True)
        self.assertFalse(inj_user.get("ok"))
        self.assertEqual(inj_user["error"]["code"], c.SCOPE_MISMATCH)

        # Relayer B CANNOT inject relayed chat into Relayer A's project -> SCOPE_MISMATCH!
        inj_other = self.t(relay_b, "chat.send", {
            "project_id": prj_a, "text": "Cross-relayer injection",
        }, relayed=True)
        self.assertFalse(inj_other.get("ok"))
        self.assertEqual(inj_other["error"]["code"], c.SCOPE_MISMATCH)

        # Relayer A CAN send relayed chat into its own originated project, and it is visibly marked!
        ok_send = self.t(relay_a, "chat.send", {
            "project_id": prj_a, "text": "Follow-up from external DM",
        }, relayed=True)
        self.assertTrue(ok_send.get("ok"), ok_send)
        msg = ok_send["data"]["message"]
        self.assertTrue(msg["relayed"])
        self.assertEqual(msg["via"], "relayed")
        self.assertEqual(msg["relayed_by_name"], "relayer-a")

        clist = self.u("chat.list", {"project_id": prj_a})["data"]["items"]
        matched = next(it for it in clist if it["id"] == msg["id"])
        self.assertTrue(matched["relayed"])
        self.assertEqual(matched["via"], "relayed")
        self.assertEqual(matched["relayed_by_name"], "relayer-a")

        # Lead prompt renders the relayed message with explicit [RELAYED via relayer-a] marker
        from mesh.core.lead_prompt import render_lead_prompt
        from mesh.daemon.lead_turns import build_prompt_input

        conn = self.harness.db()
        try:
            pinp = build_prompt_input(conn, prj_a, [], limit_bytes=64_000, testing_mode=True)
            rendered = render_lead_prompt(pinp)
            self.assertIn("user [RELAYED via relayer-a]", rendered.text)
        finally:
            conn.close()

        self.verify_audit()

    # ------------------------------------------------------------------
    # 8. R2 P3: Terminal & inactive project always_allow rejected with PROJECT_NOT_ACTIVE
    # ------------------------------------------------------------------
    def test_r2_terminal_and_inactive_project_always_allow_rejected(self) -> None:
        lead_tok = self.agent("lead-term", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-term", adapter="none", delivery="inbox", max_unapproved="dev")

        # (a) Completed project rejects allow & always_allow with PROJECT_NOT_ACTIVE
        prj_done = self.project("lead-term", mode="require_approval", perm="plan")
        crits = self.u("project.show", {"project_id": prj_done})["data"].get("criteria", [])
        comp = self.u("project.complete", {
            "project_id": prj_done,
            "report": {
                "summary": "All done",
                "criteria": [{"id": cr["id"], "met": True, "evidence": "verified"} for cr in crits],
            },
        })
        self.assertTrue(comp.get("ok"), comp)

        esc_req = self.u("approval.request", {
            "category": "permission_escalation",
            "project_id": prj_done,
            "summary": "Esc on completed project",
            "operation": {"to": "w-term", "profile": "elevated"},
        })["data"]
        retry_req = self.u("approval.request", {
            "category": "retry_over_limit",
            "project_id": prj_done,
            "summary": "Retry on completed project",
            "operation": {"task_id": "tsk_dummy"},
        })["data"]
        for req_card in (esc_req, retry_req):
            for action in ("allow", "always_allow"):
                r = self.u("approval.decide", {
                    "approval_id": req_card["approval_id"],
                    "action": action,
                    "op_hash": req_card["op_hash"],
                })
                self.assertFalse(r.get("ok"), r)
                self.assertEqual(r["error"]["code"], c.PROJECT_NOT_ACTIVE)
                self.assertEqual(r["exit_code"], c.EXIT_CONFLICT)

        self.assertEqual(self.u("project.show", {"project_id": prj_done})["data"]["permission_mode"], "plan")
        self.assertEqual(self.u("grant.list", {"project_id": prj_done})["data"]["items"], [])

        # (b) Cancelled project also rejects allow & always_allow with PROJECT_NOT_ACTIVE
        prj_can = self.project("lead-term", mode="require_approval", perm="plan")
        self.assertTrue(self.u("project.cancel", {"project_id": prj_can, "reason": "cancel test"}).get("ok"))
        can_esc = self.u("approval.request", {
            "category": "permission_escalation",
            "project_id": prj_can,
            "summary": "Esc on cancelled project",
            "operation": {"to": "w-term", "profile": "elevated"},
        })["data"]
        for action in ("allow", "always_allow"):
            r_can_esc = self.u("approval.decide", {
                "approval_id": can_esc["approval_id"],
                "action": action,
                "op_hash": can_esc["op_hash"],
            })
            self.assertFalse(r_can_esc.get("ok"))
            self.assertEqual(r_can_esc["error"]["code"], c.PROJECT_NOT_ACTIVE)
        self.assertEqual(self.u("project.show", {"project_id": prj_can})["data"]["permission_mode"], "plan")
        self.assertEqual(self.u("grant.list", {"project_id": prj_can})["data"]["items"], [])

        # (c) Paused project with real internal plan_dispatch, step_dispatch, & permission_escalation cards
        #     rejects always_allow (mode switch + standing grants) with PROJECT_NOT_ACTIVE
        prj_paused_sup = self.project("lead-term", mode="require_approval", perm="supervised")
        p_sup = self.t(lead_tok, "plan.propose", {
            "project_id": prj_paused_sup, "title": "Sup Paused", "summary": "S",
            "steps": [
                {"key": "s1", "title": "Norm", "spec": "X", "assignee": "w-term", "profile": "edit"},
                {"key": "s2", "title": "Elev", "spec": "Y", "assignee": "w-term", "profile": "elevated"},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p_sup["approval_id"], "action": "allow", "plan_hash": p_sup["plan_hash"],
        }).get("ok"))
        d_sup = self.t(lead_tok, "plan.dispatch", {"project_id": prj_paused_sup, "plan_id": p_sup["id"]}, approval=p_sup["approval_id"])["data"]
        step_p = next(sa for sa in d_sup["step_approvals"] if sa["category"] == "step_dispatch")
        esc_p = next(sa for sa in d_sup["step_approvals"] if sa["category"] == "permission_escalation")
        self.assertTrue(self.u("project.pause", {"project_id": prj_paused_sup, "reason": "hold"}).get("ok"))
        for confirm in (False, True):
            r_st = self.u("approval.decide", {
                "approval_id": step_p["approval_id"], "action": "always_allow",
                "op_hash": step_p["op_hash"], "confirm_auto_mode": confirm,
            })
            self.assertFalse(r_st.get("ok"), r_st)
            self.assertEqual(r_st["error"]["code"], c.PROJECT_NOT_ACTIVE)
        r_ep = self.u("approval.decide", {
            "approval_id": esc_p["approval_id"], "action": "always_allow", "op_hash": esc_p["op_hash"],
        })
        self.assertFalse(r_ep.get("ok"), r_ep)
        self.assertEqual(r_ep["error"]["code"], c.PROJECT_NOT_ACTIVE)
        self.assertEqual(self.u("project.show", {"project_id": prj_paused_sup})["data"]["permission_mode"], "supervised")
        self.assertEqual(self.u("grant.list", {"project_id": prj_paused_sup})["data"]["items"], [])

        prj_paused = self.project("lead-term", mode="require_approval", perm="plan")
        p_paused = self.t(lead_tok, "plan.propose", {
            "project_id": prj_paused, "title": "P Paused", "summary": "S",
            "steps": [{"key": "s1", "title": "A", "spec": "X", "assignee": "w-term"}],
        })["data"]["plan"]
        self.assertTrue(self.u("project.pause", {"project_id": prj_paused, "reason": "hold"}).get("ok"))
        r_pause = self.u("approval.decide", {
            "approval_id": p_paused["approval_id"],
            "action": "always_allow",
            "plan_hash": p_paused["plan_hash"],
            "confirm_auto_mode": True,
        })
        self.assertFalse(r_pause.get("ok"))
        self.assertEqual(r_pause["error"]["code"], c.PROJECT_NOT_ACTIVE)
        self.assertEqual(self.u("project.show", {"project_id": prj_paused})["data"]["permission_mode"], "plan")

        self.verify_audit()

    # ------------------------------------------------------------------
    # 9. R2 P3: Decide on expired pending approval persists expired + plan.cancelled (without sweep)
    # ------------------------------------------------------------------
    def test_r2_decide_on_expired_persists_expired_and_plan_cancelled(self) -> None:
        lead_tok = self.agent("lead-exp", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-exp", adapter="none", delivery="inbox", max_unapproved="dev")
        prj = self.project("lead-exp", mode="require_approval", perm="plan")

        fail_step_path = os.path.join(self.harness.home, "run", "scheduler_fail_step")
        with open(fail_step_path, "w", encoding="utf-8") as f:
            f.write("approval_expiry\n")
        try:
            self.assertTrue(self.u("config.set", {"key": "approval.ttl_seconds", "value": 1}).get("ok"))
            p = self.t(lead_tok, "plan.propose", {
                "project_id": prj, "title": "Expire on decide", "summary": "S",
                "steps": [{"key": "s1", "title": "A", "spec": "X", "assignee": "w-exp"}],
            })["data"]["plan"]
            time.sleep(1.6)

            # Prove background sweep did NOT expire it
            conn = self.harness.db()
            try:
                apr_before = conn.execute("SELECT status FROM approvals WHERE id = ?;", (p["approval_id"],)).fetchone()
                pln_before = conn.execute("SELECT status FROM plans WHERE id = ?;", (p["id"],)).fetchone()
                self.assertEqual(apr_before["status"], "pending")
                self.assertEqual(pln_before["status"], "proposed")
            finally:
                conn.close()

            # approval.decide detects expiry, persists expired + plan.cancelled, and raises APPROVAL_EXPIRED
            dec = self.u("approval.decide", {
                "approval_id": p["approval_id"], "action": "allow", "plan_hash": p["plan_hash"],
            })
            self.assertFalse(dec.get("ok"))
            self.assertEqual(dec["error"]["code"], c.APPROVAL_EXPIRED)
            self.assertEqual(dec["exit_code"], c.EXIT_APPROVAL_INVALID)

            # Immediately persisted even while approval_expiry sweep is still disabled!
            ps = self.u("plan.show", {"plan_id": p["id"]})["data"]
            self.assertEqual(ps["status"], "cancelled")
            self.assertEqual(ps["approval"]["status"], "expired")

            conn = self.harness.db()
            try:
                exp_evs = conn.execute(
                    "SELECT payload_json FROM events WHERE approval_id = ? AND type = 'approval.expired';",
                    (p["approval_id"],),
                ).fetchall()
                self.assertEqual(len(exp_evs), 1)
                self.assertEqual(json.loads(exp_evs[0]["payload_json"]).get("expired_on"), "decide")
                can_evs = conn.execute(
                    "SELECT payload_json FROM events WHERE type = 'plan.cancelled' AND json_extract(payload_json, '$.plan_id') = ?;",
                    (p["id"],),
                ).fetchall()
                self.assertEqual(len(can_evs), 1)
            finally:
                conn.close()

            # Second decide on already-expired approval still returns APPROVAL_EXPIRED without duplicate event
            dec2 = self.u("approval.decide", {
                "approval_id": p["approval_id"], "action": "allow", "plan_hash": p["plan_hash"],
            })
            self.assertFalse(dec2.get("ok"))
            self.assertEqual(dec2["error"]["code"], c.APPROVAL_EXPIRED)
            conn = self.harness.db()
            try:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE approval_id = ? AND type = 'approval.expired';",
                    (p["approval_id"],),
                ).fetchone()[0]
                self.assertEqual(cnt, 1)
            finally:
                conn.close()
        finally:
            try:
                os.remove(fail_step_path)
            except OSError:
                pass
            self.u("config.set", {"key": "approval.ttl_seconds", "value": 86400})

        self.verify_audit()

    # ------------------------------------------------------------------
    # 10. R2 P3: Relayed chat.send requires a CURRENT relationship in addition to origin
    # ------------------------------------------------------------------
    def test_r2_relayed_chat_send_requires_current_relationship(self) -> None:
        former_lead_tok = self.agent("former-lead", adapter="none", delivery="inbox", can_lead=True, caps=["relay", "intake"])
        self.agent("new-lead", adapter="none", delivery="inbox", can_lead=True)
        relayer_tok = self.agent("relayer-cur", adapter="none", delivery="inbox", caps=["relay", "intake"])
        self.agent("temp-lead", adapter="none", delivery="inbox", can_lead=True)

        # (a) Agent originates a project with itself as lead, then is removed as lead -> loses relayed chat rights
        prj1_resp = self.t(former_lead_tok, "intake.submit", {
            "title": "Handover Project", "goal": "G", "lead": "former-lead",
            "plan_gate_mode": "require_approval",
        })
        self.assertTrue(prj1_resp.get("ok"), prj1_resp)
        prj1 = prj1_resp["data"]["id"]

        # While active lead: SELF_APPROVAL blocks relay
        self_r = self.t(former_lead_tok, "chat.send", {"project_id": prj1, "text": "as lead"}, relayed=True)
        self.assertFalse(self_r.get("ok"))
        self.assertEqual(self_r["error"]["code"], c.SELF_APPROVAL)

        # User replaces lead -> former-lead is removed as lead and loses relayed chat rights despite still being origin
        set_l = self.u("project.set_lead", {"project_id": prj1, "agent": "new-lead", "reason": "handover"})
        self.assertTrue(set_l.get("ok"), set_l)
        after_remove = self.t(former_lead_tok, "chat.send", {"project_id": prj1, "text": "removed lead"}, relayed=True)
        self.assertFalse(after_remove.get("ok"))
        self.assertEqual(after_remove["error"]["code"], c.SCOPE_MISMATCH)

        # (b) Relayer-originated project loses active enabled lead -> relayed chat.send refused
        prj2_resp = self.t(relayer_tok, "intake.submit", {
            "title": "Relayer Project", "goal": "G", "lead": "temp-lead",
            "plan_gate_mode": "require_approval",
        }, relayed=True, user_statement="Create project")
        self.assertTrue(prj2_resp.get("ok"), prj2_resp)
        prj2 = prj2_resp["data"]["id"]

        ok_before = self.t(relayer_tok, "chat.send", {"project_id": prj2, "text": "hello"}, relayed=True)
        self.assertTrue(ok_before.get("ok"), ok_before)

        self.assertTrue(self.u("agent.disable", {"name": "temp-lead"}).get("ok"))
        no_lead = self.t(relayer_tok, "chat.send", {"project_id": prj2, "text": "after disable"}, relayed=True)
        self.assertFalse(no_lead.get("ok"))
        self.assertEqual(no_lead["error"]["code"], c.SCOPE_MISMATCH)

        self.verify_audit()

    # ------------------------------------------------------------------
    # 11. R2 P3: Mode change away from supervised sweep-cancels pending step_dispatch cards (audited)
    # ------------------------------------------------------------------
    def test_r2_mode_change_away_from_supervised_sweep_cancels_step_dispatch_cards(self) -> None:
        lead_tok = self.agent("lead-swp", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-swp", adapter="fake", delivery="launch", max_unapproved="dev")
        self.harness.hold_launch_starts()

        # (a) project.set_permission_mode: supervised -> plan cancels pending step_dispatch, preserves escalation
        prj1 = self.project("lead-swp", mode="require_approval", perm="supervised")
        p1 = self.t(lead_tok, "plan.propose", {
            "project_id": prj1, "title": "Sweep P1", "summary": "S",
            "steps": [
                {"key": "s1", "title": "Step 1", "spec": "X", "assignee": "w-swp", "profile": "edit"},
                {"key": "s2", "title": "Step 2", "spec": "Y", "assignee": "w-swp", "profile": "edit"},
                {"key": "s3", "title": "Step 3 Esc", "spec": "Z", "assignee": "w-swp", "profile": "elevated"},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p1["approval_id"], "action": "allow", "plan_hash": p1["plan_hash"],
        }).get("ok"))
        d1 = self.t(lead_tok, "plan.dispatch", {"project_id": prj1, "plan_id": p1["id"]}, approval=p1["approval_id"])["data"]
        s1_apr = next(sa for sa in d1["step_approvals"] if sa["step_key"] == "s1")
        s2_apr = next(sa for sa in d1["step_approvals"] if sa["step_key"] == "s2")
        s3_apr = next(sa for sa in d1["step_approvals"] if sa["step_key"] == "s3")
        t1_id = next(t["id"] for t in d1["tasks"] if t["plan_step_key"] == "s1")
        t3_id = next(t["id"] for t in d1["tasks"] if t["plan_step_key"] == "s3")
        self.assertEqual(s1_apr["category"], "step_dispatch")
        self.assertEqual(s2_apr["category"], "step_dispatch")
        self.assertEqual(s3_apr["category"], "permission_escalation")

        sw = self.u("project.set_permission_mode", {"project_id": prj1, "permission_mode": "plan"})
        self.assertTrue(sw.get("ok"), sw)
        self.assertEqual(set(sw["data"]["cancelled_step_approvals"]), {s1_apr["approval_id"], s2_apr["approval_id"]})
        self.assertEqual(self.u("approval.show", {"approval_id": s1_apr["approval_id"]})["data"]["status"], "cancelled")
        self.assertEqual(self.u("approval.show", {"approval_id": s2_apr["approval_id"]})["data"]["status"], "cancelled")
        # permission_escalation card MUST remain pending
        self.assertEqual(self.u("approval.show", {"approval_id": s3_apr["approval_id"]})["data"]["status"], "pending")

        # Forward-only: s1 now assigns ungated in 'plan' mode, while s3 still requires escalation approval
        asg1 = self.t(lead_tok, "task.assign", {"task_id": t1_id, "to": "w-swp", "profile": "edit"})
        self.assertTrue(asg1.get("ok"), asg1)
        asg3 = self.t(lead_tok, "task.assign", {"task_id": t3_id, "to": "w-swp", "profile": "elevated"})
        self.assertFalse(asg3.get("ok"))
        self.assertEqual(asg3["error"]["code"], c.APPROVAL_REQUIRED)

        # Forward-only upgrade (plan -> supervised) does NOT retroactively revert already-assigned t1
        self.assertTrue(self.u("project.set_permission_mode", {"project_id": prj1, "permission_mode": "supervised"}).get("ok"))
        self.assertIn(self.u("task.show", {"task_id": t1_id})["data"]["status"], ("assigned", "queued"))

        # (b) approval.decide always_allow + confirm_auto_mode (supervised -> auto) sweep-cancels OTHER step_dispatch cards
        prj2 = self.project("lead-swp", mode="require_approval", perm="supervised")
        p2 = self.t(lead_tok, "plan.propose", {
            "project_id": prj2, "title": "Sweep P2", "summary": "S",
            "steps": [
                {"key": "b1", "title": "B1", "spec": "X", "assignee": "w-swp", "profile": "edit"},
                {"key": "b2", "title": "B2", "spec": "Y", "assignee": "w-swp", "profile": "edit"},
                {"key": "b3", "title": "B3 Esc", "spec": "Z", "assignee": "w-swp", "profile": "elevated"},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p2["approval_id"], "action": "allow", "plan_hash": p2["plan_hash"],
        }).get("ok"))
        d2 = self.t(lead_tok, "plan.dispatch", {"project_id": prj2, "plan_id": p2["id"]}, approval=p2["approval_id"])["data"]
        b1_apr = next(sa for sa in d2["step_approvals"] if sa["step_key"] == "b1")
        b2_apr = next(sa for sa in d2["step_approvals"] if sa["step_key"] == "b2")
        b3_apr = next(sa for sa in d2["step_approvals"] if sa["step_key"] == "b3")

        conf = self.u("approval.decide", {
            "approval_id": b1_apr["approval_id"],
            "action": "always_allow",
            "op_hash": b1_apr["op_hash"],
            "confirm_auto_mode": True,
        })
        self.assertTrue(conf.get("ok"), conf)
        self.assertEqual(conf["data"]["permission_mode"], "auto")
        self.assertEqual(conf["data"]["cancelled_step_approvals"], [b2_apr["approval_id"]])
        self.assertEqual(self.u("approval.show", {"approval_id": b1_apr["approval_id"]})["data"]["status"], "approved")
        self.assertEqual(self.u("approval.show", {"approval_id": b2_apr["approval_id"]})["data"]["status"], "cancelled")
        self.assertEqual(self.u("approval.show", {"approval_id": b3_apr["approval_id"]})["data"]["status"], "pending")

        self.verify_audit()

    # ------------------------------------------------------------------
    # 12. R2 P3: chat.reply rejects direct user callers (lead-only; desktop uses chat.send)
    # ------------------------------------------------------------------
    def test_r2_chat_reply_rejects_direct_user(self) -> None:
        from mesh.desktop.bridge import ALLOWED_RPC_METHODS

        self.assertNotIn("chat.reply", ALLOWED_RPC_METHODS)
        self.assertIn("chat.send", ALLOWED_RPC_METHODS)

        lead_tok = self.agent("lead-cr", adapter="none", delivery="inbox", can_lead=True)
        prj = self.project("lead-cr", mode="require_approval", perm="plan")

        bad = self.u("chat.reply", {"project_id": prj, "text": "spoofed lead"})
        self.assertFalse(bad.get("ok"))
        self.assertEqual(bad["error"]["code"], c.CAPABILITY_MISSING)
        self.assertEqual(bad["exit_code"], c.EXIT_FORBIDDEN)
        self.assertEqual(bad["error"]["details"].get("use"), "chat.send")

        # No lead message was recorded
        items_before = self.u("chat.list", {"project_id": prj})["data"]["items"]
        self.assertFalse(any(m.get("body") == "spoofed lead" for m in items_before))

        # Acting lead can still use chat.reply (role='lead'), direct user uses chat.send (role='user')
        ok_lead = self.t(lead_tok, "chat.reply", {"project_id": prj, "text": "genuine lead"})
        self.assertTrue(ok_lead.get("ok"), ok_lead)
        self.assertEqual(ok_lead["data"]["message"]["role"], "lead")

        ok_user = self.u("chat.send", {"project_id": prj, "text": "genuine user"})
        self.assertTrue(ok_user.get("ok"), ok_user)
        self.assertEqual(ok_user["data"]["message"]["role"], "user")

        self.verify_audit()

    # ------------------------------------------------------------------
    # 13. R2 Test Gap: permission_escalation standing grant matches after agent rename
    # ------------------------------------------------------------------
    def test_r2_escalation_grant_matches_after_agent_rename(self) -> None:
        lead_tok = self.agent("lead-ren", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-orig", adapter="fake", delivery="launch", max_unapproved="dev")
        self.harness.hold_launch_starts()
        prj = self.project("lead-ren", mode="off", perm="plan")

        # 1. Request escalation for worker by name 'w-orig' -> always_allow mints grant bound to agent_id
        t1_req = self.t(lead_tok, "task.create", {
            "project": prj, "title": "Esc 1", "spec": "S1", "assignee": "w-orig", "profile": "elevated",
        })
        self.assertFalse(t1_req.get("ok"))
        self.assertEqual(t1_req["error"]["code"], c.APPROVAL_REQUIRED)
        apr_id = t1_req["error"]["details"]["approval_id"]
        op_hash = t1_req["error"]["details"]["op_hash"]

        dec = self.u("approval.decide", {"approval_id": apr_id, "action": "always_allow", "op_hash": op_hash})
        self.assertTrue(dec.get("ok"), dec)
        grant = dec["data"]["grant"]
        orig_agent_id = grant["scope_agent_id"]

        # 2. Rename principal 'w-orig' -> 'w-renamed', then create a brand new worker reusing 'w-orig'
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        try:
            conn.execute("UPDATE principals SET name = 'w-renamed' WHERE id = ?;", (orig_agent_id,))
            conn.commit()
        finally:
            conn.close()
        self.agent("w-orig", adapter="fake", delivery="launch", max_unapproved="dev")

        # grant.list dynamically shows the updated name for the immutable agent_id
        glist = self.u("grant.list", {"project_id": prj})["data"]["items"]
        self.assertEqual(len(glist), 1)
        self.assertEqual(glist[0]["scope_agent_id"], orig_agent_id)
        self.assertEqual(glist[0]["scope_agent_name"], "w-renamed")

        # 3. Escalation targeting 'w-renamed' STILL matches the standing grant (no new approval needed!)
        t2_ok = self.t(lead_tok, "task.create", {
            "project": prj, "title": "Esc 2", "spec": "S2", "assignee": "w-renamed", "profile": "elevated",
        })
        self.assertTrue(t2_ok.get("ok"), t2_ok)

        # 4. Escalation targeting the new worker that recycled 'w-orig' MUST NOT match the grant!
        t3_req = self.t(lead_tok, "task.create", {
            "project": prj, "title": "Esc 3", "spec": "S3", "assignee": "w-orig", "profile": "elevated",
        })
        self.assertFalse(t3_req.get("ok"))
        self.assertEqual(t3_req["error"]["code"], c.APPROVAL_REQUIRED)

        self.verify_audit()

    # ------------------------------------------------------------------
    # 14. R3 P3: chat.list & plan.show suppress actions on inactive-project & expired unswept cards
    # ------------------------------------------------------------------
    def test_r3_chat_list_and_plan_summary_suppress_actions_on_inactive_and_expired_cards(self) -> None:
        lead_tok = self.agent("lead-ui", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-ui", adapter="none", delivery="inbox", max_unapproved="dev")

        # (a) Paused project suppresses actions in BOTH chat.list and plan.show; resuming restores them
        prj1 = self.project("lead-ui", mode="require_approval", perm="plan")
        p1 = self.t(lead_tok, "plan.propose", {
            "project_id": prj1, "title": "Pause UI Plan", "summary": "S",
            "steps": [{"key": "s1", "title": "A", "spec": "X", "assignee": "w-ui"}],
        })["data"]["plan"]

        cl_active = self.u("chat.list", {"project_id": prj1})["data"]
        self.assertEqual(cl_active["project_status"], "active")
        a_active = cl_active["approvals_by_id"][p1["approval_id"]]
        self.assertEqual(a_active["available_actions"], ["allow", "always_allow", "reject"])
        self.assertTrue(a_active["decidable"])
        self.assertFalse(a_active["inactive_project"])
        self.assertFalse(a_active["expired"])
        self.assertIsNotNone(a_active["always_allow_effect"])

        self.assertTrue(self.u("project.pause", {"project_id": prj1, "reason": "hold"}).get("ok"))
        cl_paused = self.u("chat.list", {"project_id": prj1})["data"]
        self.assertEqual(cl_paused["project_status"], "paused")
        a_paused = cl_paused["approvals_by_id"][p1["approval_id"]]
        self.assertEqual(a_paused["available_actions"], ["reject"])
        self.assertIsNone(a_paused["always_allow_effect"])
        self.assertTrue(a_paused["decidable"])
        self.assertFalse(a_paused["approvable"])
        self.assertTrue(a_paused["dismiss_only"])
        self.assertTrue(a_paused["inactive_project"])
        self.assertIsNone(a_paused["undecidable_reason"])
        self.assertEqual(a_paused["unapprovable_reason"], "project_not_active")
        pl_paused_chat = cl_paused["plans_by_id"][p1["id"]]["approval"]
        self.assertEqual(pl_paused_chat["available_actions"], ["reject"])
        self.assertTrue(pl_paused_chat["decidable"])
        self.assertFalse(pl_paused_chat["approvable"])
        self.assertTrue(pl_paused_chat["dismiss_only"])
        self.assertTrue(pl_paused_chat["inactive_project"])

        ps_paused = self.u("plan.show", {"plan_id": p1["id"]})["data"]["approval"]
        self.assertEqual(ps_paused["available_actions"], ["reject"])
        self.assertIsNone(ps_paused["always_allow_effect"])
        self.assertTrue(ps_paused["decidable"])
        self.assertFalse(ps_paused["approvable"])
        self.assertTrue(ps_paused["dismiss_only"])
        self.assertTrue(ps_paused["inactive_project"])
        self.assertIsNone(ps_paused["undecidable_reason"])
        self.assertEqual(ps_paused["unapprovable_reason"], "project_not_active")

        # Resuming restores decidability
        self.assertTrue(self.u("project.resume", {"project_id": prj1}).get("ok"))
        cl_resumed = self.u("chat.list", {"project_id": prj1})["data"]
        a_resumed = cl_resumed["approvals_by_id"][p1["approval_id"]]
        self.assertEqual(a_resumed["available_actions"], ["allow", "always_allow", "reject"])
        self.assertTrue(a_resumed["decidable"])
        self.assertFalse(a_resumed["inactive_project"])

        # (b) Past-TTL unswept pending card suppresses actions in BOTH chat.list and plan.show
        prj2 = self.project("lead-ui", mode="require_approval", perm="plan")
        fail_step_path = os.path.join(self.harness.home, "run", "scheduler_fail_step")
        with open(fail_step_path, "w", encoding="utf-8") as f:
            f.write("approval_expiry\n")
        try:
            self.assertTrue(self.u("config.set", {"key": "approval.ttl_seconds", "value": 1}).get("ok"))
            p2 = self.t(lead_tok, "plan.propose", {
                "project_id": prj2, "title": "Expire UI Plan", "summary": "S",
                "steps": [{"key": "s1", "title": "A", "spec": "X", "assignee": "w-ui"}],
            })["data"]["plan"]
            time.sleep(1.6)

            # Confirm still pending unswept in DB, yet chat.list & plan.show suppress all actions
            cl_exp = self.u("chat.list", {"project_id": prj2})["data"]
            a_exp = cl_exp["approvals_by_id"][p2["approval_id"]]
            self.assertEqual(a_exp["status"], "pending")
            self.assertEqual(a_exp["available_actions"], [])
            self.assertIsNone(a_exp["always_allow_effect"])
            self.assertFalse(a_exp["decidable"])
            self.assertTrue(a_exp["expired"])
            self.assertEqual(a_exp["undecidable_reason"], "expired")

            ps_exp = self.u("plan.show", {"plan_id": p2["id"]})["data"]["approval"]
            self.assertEqual(ps_exp["status"], "pending")
            self.assertEqual(ps_exp["available_actions"], [])
            self.assertIsNone(ps_exp["always_allow_effect"])
            self.assertFalse(ps_exp["decidable"])
            self.assertTrue(ps_exp["expired"])
            self.assertEqual(ps_exp["undecidable_reason"], "expired")
        finally:
            try:
                os.remove(fail_step_path)
            except OSError:
                pass
            self.u("config.set", {"key": "approval.ttl_seconds", "value": 86400})

        self.verify_audit()

    # ------------------------------------------------------------------
    # 15. R3 P3: Entering auto mode sweep-cancels pending plan_dispatch cards (plan stays dispatchable)
    # ------------------------------------------------------------------
    def test_r3_entering_auto_mode_sweeps_pending_plan_dispatch_and_keeps_plan_dispatchable(self) -> None:
        lead_tok = self.agent("lead-auto-swp", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-auto-swp", adapter="fake", delivery="launch", max_unapproved="dev")
        self.harness.hold_launch_starts()

        # (a) project.set_permission_mode (plan -> auto) sweep-cancels pending plan_dispatch card without cancelling plan
        prj1 = self.project("lead-auto-swp", mode="require_approval", perm="plan")
        p1 = self.t(lead_tok, "plan.propose", {
            "project_id": prj1, "title": "Auto Sweep P1", "summary": "S",
            "steps": [{"key": "s1", "title": "Step 1", "spec": "X", "assignee": "w-auto-swp", "profile": "edit"}],
        })["data"]["plan"]

        sw = self.u("project.set_permission_mode", {"project_id": prj1, "permission_mode": "auto"})
        self.assertTrue(sw.get("ok"), sw)
        self.assertEqual(sw["data"]["cancelled_plan_approvals"], [p1["approval_id"]])
        self.assertEqual(sw["data"]["cancelled_step_approvals"], [])

        # Card is cancelled in DB and non-decidable in chat.list & plan.show
        self.assertEqual(self.u("approval.show", {"approval_id": p1["approval_id"]})["data"]["status"], "cancelled")
        cl1 = self.u("chat.list", {"project_id": prj1})["data"]
        a1 = cl1["approvals_by_id"][p1["approval_id"]]
        self.assertEqual(a1["status"], "cancelled")
        self.assertEqual(a1["available_actions"], [])
        self.assertIsNone(a1["always_allow_effect"])
        self.assertFalse(a1["decidable"])

        # Attempting to decide the swept plan_dispatch card fails with APPROVAL_CANCELLED
        stale_dec = self.u("approval.decide", {
            "approval_id": p1["approval_id"], "action": "allow", "plan_hash": p1["plan_hash"],
        })
        self.assertFalse(stale_dec.get("ok"))
        self.assertEqual(stale_dec["error"]["code"], c.APPROVAL_CANCELLED)

        # Plan itself stays 'proposed' and dispatches directly in auto mode without approval!
        self.assertEqual(self.u("plan.show", {"plan_id": p1["id"]})["data"]["status"], "proposed")
        disp1 = self.t(lead_tok, "plan.dispatch", {"project_id": prj1, "plan_id": p1["id"]})
        self.assertTrue(disp1.get("ok"), disp1)
        self.assertEqual(disp1["data"]["plan"]["status"], "dispatched")
        self.assertEqual(len(disp1["data"]["tasks"]), 1)

        # (b) approval.decide always_allow + confirm_auto_mode on a step_dispatch card sweeps BOTH step & pending plan cards
        prj2 = self.project("lead-auto-swp", mode="require_approval", perm="supervised")
        p2a = self.t(lead_tok, "plan.propose", {
            "project_id": prj2, "title": "P2a", "summary": "S",
            "steps": [
                {"key": "a1", "title": "A1", "spec": "X", "assignee": "w-auto-swp", "profile": "edit"},
                {"key": "a2", "title": "A2", "spec": "Y", "assignee": "w-auto-swp", "profile": "edit"},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p2a["approval_id"], "action": "allow", "plan_hash": p2a["plan_hash"],
        }).get("ok"))
        d2a = self.t(lead_tok, "plan.dispatch", {"project_id": prj2, "plan_id": p2a["id"]}, approval=p2a["approval_id"])["data"]
        a1_apr = next(sa for sa in d2a["step_approvals"] if sa["step_key"] == "a1")
        a2_apr = next(sa for sa in d2a["step_approvals"] if sa["step_key"] == "a2")

        # Propose a second plan while supervised so there is also a pending plan_dispatch card
        p2b = self.t(lead_tok, "plan.propose", {
            "project_id": prj2, "title": "P2b", "summary": "S2",
            "steps": [{"key": "b1", "title": "B1", "spec": "Z", "assignee": "w-auto-swp", "profile": "edit"}],
        })["data"]["plan"]

        conf = self.u("approval.decide", {
            "approval_id": a1_apr["approval_id"],
            "action": "always_allow",
            "op_hash": a1_apr["op_hash"],
            "confirm_auto_mode": True,
        })
        self.assertTrue(conf.get("ok"), conf)
        self.assertEqual(conf["data"]["cancelled_step_approvals"], [a2_apr["approval_id"]])
        self.assertEqual(conf["data"]["cancelled_plan_approvals"], [p2b["approval_id"]])
        self.assertEqual(self.u("approval.show", {"approval_id": p2b["approval_id"]})["data"]["status"], "cancelled")
        self.assertEqual(self.u("plan.show", {"plan_id": p2b["id"]})["data"]["status"], "proposed")

        # (c) Clicking always_allow + confirm_auto_mode on a plan_dispatch card itself keeps THAT card approved
        #     and suppresses further always_allow actions now that the project is already in auto mode
        prj3 = self.project("lead-auto-swp", mode="require_approval", perm="plan")
        p3 = self.t(lead_tok, "plan.propose", {
            "project_id": prj3, "title": "P3", "summary": "S",
            "steps": [{"key": "c1", "title": "C1", "spec": "W", "assignee": "w-auto-swp", "profile": "edit"}],
        })["data"]["plan"]
        conf3 = self.u("approval.decide", {
            "approval_id": p3["approval_id"],
            "action": "always_allow",
            "plan_hash": p3["plan_hash"],
            "confirm_auto_mode": True,
        })
        self.assertTrue(conf3.get("ok"), conf3)
        self.assertEqual(conf3["data"]["cancelled_plan_approvals"], [])
        self.assertEqual(self.u("approval.show", {"approval_id": p3["approval_id"]})["data"]["status"], "approved")
        cl3 = self.u("chat.list", {"project_id": prj3})["data"]
        a3 = cl3["approvals_by_id"][p3["approval_id"]]
        self.assertEqual(a3["available_actions"], [])
        self.assertIsNone(a3["always_allow_effect"])
        self.assertFalse(a3["decidable"])
        self.assertEqual(a3["undecidable_reason"], "already_auto_mode")

        self.verify_audit()

    # ------------------------------------------------------------------
    # 16. R3 P3: Dead-task step_dispatch & permission_escalation cards refused & suppressed in UI
    # ------------------------------------------------------------------
    def test_r3_dead_task_step_and_escalation_cards_refused_and_suppressed(self) -> None:
        lead_tok = self.agent("lead-dead", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-dead", adapter="fake", delivery="launch", max_unapproved="dev")
        self.harness.hold_launch_starts()

        prj = self.project("lead-dead", mode="require_approval", perm="supervised")
        p = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Dead Task Plan", "summary": "S",
            "steps": [
                {"key": "s1", "title": "Step 1", "spec": "X", "assignee": "w-dead", "profile": "edit"},
                {"key": "s2", "title": "Step 2 Esc", "spec": "Y", "assignee": "w-dead", "profile": "elevated"},
                {"key": "s3", "title": "Step 3 Approved", "spec": "Z", "assignee": "w-dead", "profile": "edit"},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p["approval_id"], "action": "allow", "plan_hash": p["plan_hash"],
        }).get("ok"))
        disp = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": p["id"]}, approval=p["approval_id"])["data"]
        s1_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s1")
        s2_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s2")
        s3_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s3")
        t1_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s1")
        t2_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s2")
        t3_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s3")

        # Approve s3 BEFORE cancelling t3 so we also cover approved -> always_allow upgrade on a dead task
        self.assertTrue(self.u("approval.decide", {
            "approval_id": s3_apr["approval_id"], "action": "allow", "op_hash": s3_apr["op_hash"],
        }).get("ok"))

        # Before cancel: cards show task_status='created' and are decidable
        cl_before = self.u("chat.list", {"project_id": prj})["data"]
        self.assertEqual(cl_before["approvals_by_id"][s1_apr["approval_id"]]["task_status"], "created")
        self.assertEqual(cl_before["approvals_by_id"][s1_apr["approval_id"]]["available_actions"], ["allow", "always_allow", "reject"])
        self.assertEqual(cl_before["approvals_by_id"][s3_apr["approval_id"]]["available_actions"], ["always_allow"])

        # Cancel all three tasks as direct user
        for tid in (t1_id, t2_id, t3_id):
            self.assertTrue(self.u("task.cancel", {"task_id": tid, "reason": "obsolete"}).get("ok"))

        # After cancel: chat.list exposes task_status='cancelled', suppresses approval actions,
        # retains reject-only dismissal on pending cards (s1/s2) and [] on already-approved card (s3)
        cl_after = self.u("chat.list", {"project_id": prj})["data"]
        for apr in (s1_apr, s2_apr):
            entry = cl_after["approvals_by_id"][apr["approval_id"]]
            self.assertEqual(entry["task_status"], "cancelled")
            self.assertTrue(entry["task_terminal"])
            self.assertEqual(entry["available_actions"], ["reject"])
            self.assertIsNone(entry["always_allow_effect"])
            self.assertTrue(entry["decidable"])
            self.assertFalse(entry["approvable"])
            self.assertTrue(entry["dismiss_only"])
            self.assertIsNone(entry["undecidable_reason"])
            self.assertEqual(entry["unapprovable_reason"], "task_terminal")
        entry_s3 = cl_after["approvals_by_id"][s3_apr["approval_id"]]
        self.assertEqual(entry_s3["task_status"], "cancelled")
        self.assertTrue(entry_s3["task_terminal"])
        self.assertEqual(entry_s3["available_actions"], [])
        self.assertIsNone(entry_s3["always_allow_effect"])
        self.assertFalse(entry_s3["decidable"])
        self.assertFalse(entry_s3["approvable"])
        self.assertFalse(entry_s3["dismiss_only"])
        self.assertEqual(entry_s3["undecidable_reason"], "task_terminal")
        self.assertEqual(entry_s3["unapprovable_reason"], "task_terminal")

        # Server refuses always_allow (with or without confirm_auto_mode) and allow on dead-task cards
        for apr, action, extra in (
            (s1_apr, "always_allow", {"confirm_auto_mode": False}),
            (s1_apr, "always_allow", {"confirm_auto_mode": True}),
            (s1_apr, "allow", {}),
            (s2_apr, "always_allow", {}),
            (s2_apr, "allow", {}),
            (s3_apr, "always_allow", {"confirm_auto_mode": True}),
        ):
            r = self.u("approval.decide", {
                "approval_id": apr["approval_id"],
                "action": action,
                "op_hash": apr["op_hash"],
                **extra,
            })
            self.assertFalse(r.get("ok"), r)
            self.assertEqual(r["error"]["code"], c.INVALID_TRANSITION)
            self.assertEqual(r["exit_code"], c.EXIT_CONFLICT)
            self.assertEqual(r["error"]["details"].get("reason"), "task_terminal")
            self.assertEqual(r["error"]["details"].get("task_status"), "cancelled")

        # No mode switch and zero standing grants minted
        self.assertEqual(self.u("project.show", {"project_id": prj})["data"]["permission_mode"], "supervised")
        self.assertEqual(self.u("grant.list", {"project_id": prj})["data"]["items"], [])

        self.verify_audit()

    # ------------------------------------------------------------------
    # 17. R4 P3: Progressed (non-terminal) bound tasks suppress & refuse stale cards
    # ------------------------------------------------------------------
    def test_r4_progressed_task_cards_suppressed_and_refused(self) -> None:
        lead_tok = self.agent("lead-prog", adapter="none", delivery="inbox", can_lead=True)
        w_tok = self.agent("w-prog", adapter="none", delivery="inbox", max_unapproved="dev")

        # (a) Supervised step_dispatch & permission_escalation cards whose tasks are assigned directly by user
        prj = self.project("lead-prog", mode="require_approval", perm="supervised")
        p = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Progressed Task Plan", "summary": "S",
            "steps": [
                {"key": "s1", "title": "Step 1", "spec": "X", "assignee": "w-prog", "profile": "edit"},
                {"key": "s2", "title": "Step 2 Esc", "spec": "Y", "assignee": "w-prog", "profile": "elevated"},
                {"key": "s3", "title": "Step 3 Approved", "spec": "Z", "assignee": "w-prog", "profile": "edit"},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p["approval_id"], "action": "allow", "plan_hash": p["plan_hash"],
        }).get("ok"))
        disp = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": p["id"]}, approval=p["approval_id"])["data"]
        s1_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s1")
        s2_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s2")
        s3_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s3")
        t1_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s1")
        t2_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s2")
        t3_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s3")

        # Approve s3 BEFORE direct user assigns t3 so approved->always_allow upgrade is also covered
        self.assertTrue(self.u("approval.decide", {
            "approval_id": s3_apr["approval_id"], "action": "allow", "op_hash": s3_apr["op_hash"],
        }).get("ok"))

        # Direct user assigns all three tasks directly (bypasses approval gates -> non-terminal assigned/queued)
        for tid in (t1_id, t2_id, t3_id):
            self.assertTrue(self.u("task.assign", {"task_id": tid, "to": "w-prog"}).get("ok"))

        cl_after = self.u("chat.list", {"project_id": prj})["data"]
        cards_by_apr = {it["approval_id"]: it for it in cl_after["items"] if it.get("approval_id")}
        for apr in (s1_apr, s2_apr):
            aid = apr["approval_id"]
            entry = cl_after["approvals_by_id"][aid]
            self.assertIn(entry["task_status"], ("assigned", "queued"))
            self.assertTrue(entry["task_progressed"])
            self.assertEqual(entry["available_actions"], ["reject"])
            self.assertIsNone(entry["always_allow_effect"])
            self.assertTrue(entry["decidable"])
            self.assertFalse(entry["approvable"])
            self.assertTrue(entry["dismiss_only"])
            self.assertIsNone(entry["undecidable_reason"])
            self.assertEqual(entry["unapprovable_reason"], "task_progressed")
            self.assertEqual(cards_by_apr[aid]["meta"]["actions"], ["reject"])
        entry_s3 = cl_after["approvals_by_id"][s3_apr["approval_id"]]
        self.assertIn(entry_s3["task_status"], ("assigned", "queued"))
        self.assertTrue(entry_s3["task_progressed"])
        self.assertEqual(entry_s3["available_actions"], [])
        self.assertIsNone(entry_s3["always_allow_effect"])
        self.assertFalse(entry_s3["decidable"])
        self.assertFalse(entry_s3["approvable"])
        self.assertFalse(entry_s3["dismiss_only"])
        self.assertEqual(entry_s3["undecidable_reason"], "task_progressed")
        self.assertEqual(entry_s3["unapprovable_reason"], "task_progressed")
        self.assertEqual(cards_by_apr[s3_apr["approval_id"]]["meta"]["actions"], [])

        for apr, action, extra in (
            (s1_apr, "allow", {}),
            (s1_apr, "always_allow", {"confirm_auto_mode": False}),
            (s1_apr, "always_allow", {"confirm_auto_mode": True}),
            (s2_apr, "allow", {}),
            (s2_apr, "always_allow", {}),
            (s3_apr, "always_allow", {"confirm_auto_mode": True}),
        ):
            r = self.u("approval.decide", {
                "approval_id": apr["approval_id"], "action": action, "op_hash": apr["op_hash"], **extra,
            })
            self.assertFalse(r.get("ok"), r)
            self.assertEqual(r["error"]["code"], c.INVALID_TRANSITION)
            self.assertEqual(r["exit_code"], c.EXIT_CONFLICT)
            self.assertEqual(r["error"]["details"].get("reason"), "task_progressed")
            self.assertEqual(r["error"]["details"].get("expected_task_statuses"), ["created"])

        self.assertEqual(self.u("project.show", {"project_id": prj})["data"]["permission_mode"], "supervised")
        self.assertEqual(self.u("grant.list", {"project_id": prj})["data"]["items"], [])

        # (b) retry_over_limit card whose failed task was retried directly by user (leaves 'failed' -> 'queued')
        prj_r = self.project("lead-prog", mode="off", perm="plan")
        tr_id = self.t(lead_tok, "task.create", {
            "project": prj_r, "title": "Retry Prog", "spec": "S", "assignee": "w-prog", "max_attempts": 1,
        })["data"]["id"]
        self.assertTrue(self.harness.wait_for(
            lambda: self.u("task.show", {"task_id": tr_id})["data"]["status"] == "queued", timeout=5.0
        ))
        st = self.t(w_tok, "task.start", {"task_id": tr_id})["data"]
        self.assertTrue(self.t(w_tok, "task.fail", {
            "task_id": tr_id, "run": st["run_id"], "fence": st["fence"], "reason": "boom",
        }).get("ok"))

        req_retry = self.t(lead_tok, "task.retry", {"task_id": tr_id, "reason": "try again"})
        self.assertFalse(req_retry.get("ok"))
        self.assertEqual(req_retry["error"]["code"], c.APPROVAL_REQUIRED)
        r_apr_id = req_retry["error"]["details"]["approval_id"]
        r_hash = req_retry["error"]["details"]["op_hash"]

        # Direct user retries task directly -> leaves 'failed' without consuming r_apr_id
        self.assertTrue(self.u("task.retry", {"task_id": tr_id, "reason": "user retry"}).get("ok"))

        cl_r = self.u("chat.list", {"project_id": prj_r})["data"]
        r_entry = cl_r["approvals_by_id"][r_apr_id]
        self.assertEqual(r_entry["task_status"], "queued")
        self.assertTrue(r_entry["task_progressed"])
        self.assertEqual(r_entry["available_actions"], ["reject"])
        self.assertIsNone(r_entry["always_allow_effect"])
        self.assertTrue(r_entry["decidable"])
        self.assertFalse(r_entry["approvable"])
        self.assertTrue(r_entry["dismiss_only"])
        self.assertIsNone(r_entry["undecidable_reason"])
        self.assertEqual(r_entry["unapprovable_reason"], "task_progressed")
        r_card = next(it for it in cl_r["items"] if it.get("approval_id") == r_apr_id)
        self.assertEqual(r_card["meta"]["actions"], ["reject"])

        for action in ("allow", "always_allow"):
            res = self.u("approval.decide", {
                "approval_id": r_apr_id, "action": action, "op_hash": r_hash,
            })
            self.assertFalse(res.get("ok"), res)
            self.assertEqual(res["error"]["code"], c.INVALID_TRANSITION)
            self.assertEqual(res["error"]["details"].get("reason"), "task_progressed")
            self.assertEqual(res["error"]["details"].get("expected_task_statuses"), ["failed", "escalated"])

        self.assertEqual(self.u("grant.list", {"project_id": prj_r})["data"]["items"], [])
        self.verify_audit()

    # ------------------------------------------------------------------
    # 18. R4 P3: Card message meta.actions hydrated from compute_approval_card_actions (never diverges)
    # ------------------------------------------------------------------
    def test_r4_chat_card_meta_actions_hydrated_and_consistent_with_available_actions(self) -> None:
        lead_tok = self.agent("lead-meta", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-meta", adapter="none", delivery="inbox", max_unapproved="dev")

        prj = self.project("lead-meta", mode="require_approval", perm="supervised")
        p1 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Meta Plan", "summary": "S",
            "steps": [
                {"key": "s1", "title": "Normal", "spec": "X", "assignee": "w-meta", "profile": "edit"},
                {"key": "s2", "title": "Esc", "spec": "Y", "assignee": "w-meta", "profile": "elevated"},
            ],
        })["data"]["plan"]

        # Raw chat_messages.meta_json in SQLite does NOT freeze static 'actions'
        conn = self.harness.db()
        try:
            row = conn.execute(
                "SELECT meta_json FROM chat_messages WHERE approval_id = ?;",
                (p1["approval_id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertNotIn("actions", json.loads(row["meta_json"]))
        finally:
            conn.close()

        def _assert_all_cards_consistent(cl_data: dict) -> None:
            for it in cl_data["items"]:
                if it.get("kind") in ("plan_card", "approval_card", "escalation_card"):
                    aid = it.get("approval_id")
                    expected = cl_data["approvals_by_id"][aid]["available_actions"] if aid else []
                    self.assertEqual(it["meta"]["actions"], expected, f"Mismatch on card {it['id']} (aid={aid})")

        # 1. Pending active plan card -> meta.actions == ['allow', 'always_allow', 'reject']
        cl1 = self.u("chat.list", {"project_id": prj})["data"]
        _assert_all_cards_consistent(cl1)
        p1_card = next(it for it in cl1["items"] if it.get("approval_id") == p1["approval_id"])
        self.assertEqual(p1_card["meta"]["actions"], ["allow", "always_allow", "reject"])

        # 2. Paused project -> pending card meta.actions becomes ['reject'] (dismiss-only, matching available_actions)
        self.assertTrue(self.u("project.pause", {"project_id": prj, "reason": "pause"}).get("ok"))
        cl2 = self.u("chat.list", {"project_id": prj})["data"]
        _assert_all_cards_consistent(cl2)
        p1_card_paused = next(it for it in cl2["items"] if it.get("approval_id") == p1["approval_id"])
        self.assertEqual(p1_card_paused["meta"]["actions"], ["reject"])

        # 3. Resume, approve, dispatch -> plan card consumed (meta.actions == []), step & esc cards pending
        self.assertTrue(self.u("project.resume", {"project_id": prj}).get("ok"))
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p1["approval_id"], "action": "allow", "plan_hash": p1["plan_hash"],
        }).get("ok"))
        disp = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": p1["id"]}, approval=p1["approval_id"])["data"]
        s1_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s1")
        s2_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s2")

        # Also emit a config card (never offers always_allow) and verify its meta.actions == ['allow', 'reject']
        cfg = self.t(lead_tok, "approval.request", {
            "category": "config", "project_id": prj, "summary": "Cfg change",
            "operation": {"key": "lead.profile", "value": "dev"},
        })["data"]

        cl3 = self.u("chat.list", {"project_id": prj})["data"]
        _assert_all_cards_consistent(cl3)
        cards3 = {it["approval_id"]: it for it in cl3["items"] if it.get("approval_id")}
        self.assertEqual(cards3[p1["approval_id"]]["meta"]["actions"], [])
        self.assertEqual(cards3[s1_apr["approval_id"]]["meta"]["actions"], ["allow", "always_allow", "reject"])
        self.assertEqual(cards3[s2_apr["approval_id"]]["meta"]["actions"], ["allow", "always_allow", "reject"])
        self.assertEqual(cards3[cfg["approval_id"]]["meta"]["actions"], ["allow", "reject"])

        # 4. Switch to auto -> step card cancelled (meta.actions == []), auto-proposed plan card has meta.actions == []
        self.assertTrue(self.u("project.set_permission_mode", {"project_id": prj, "permission_mode": "auto"}).get("ok"))
        p2 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Auto Plan", "summary": "S2",
            "steps": [{"key": "a1", "title": "Auto Step", "spec": "Z", "assignee": "w-meta", "profile": "edit"}],
        })["data"]["plan"]
        self.assertIsNone(p2["approval_id"])

        cl4 = self.u("chat.list", {"project_id": prj})["data"]
        _assert_all_cards_consistent(cl4)
        p2_card = next(it for it in cl4["items"] if it.get("plan_id") == p2["id"] and it["kind"] == "plan_card")
        self.assertEqual(p2_card["meta"]["actions"], [])

        # 5. Even if an older DB row had stale 'actions' in meta_json, chat.list overwrites it truthfully
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        try:
            conn.execute(
                "UPDATE chat_messages SET meta_json = ? WHERE approval_id = ?;",
                (json.dumps({"category": "step_dispatch", "actions": ["allow", "always_allow", "reject"]}), s1_apr["approval_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        cl5 = self.u("chat.list", {"project_id": prj})["data"]
        _assert_all_cards_consistent(cl5)
        s1_card_stale = next(it for it in cl5["items"] if it.get("approval_id") == s1_apr["approval_id"])
        self.assertEqual(s1_card_stale["meta"]["actions"], [])

        self.verify_audit()

    # ------------------------------------------------------------------
    # 19. R4 P3 + Nit: always_allow when already in 'auto' mode emits truthful copy & plan summary consistency
    # ------------------------------------------------------------------
    def test_r4_always_allow_when_already_auto_is_truthful_and_plan_summary_consistent(self) -> None:
        lead_tok = self.agent("lead-aauto", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-aauto", adapter="none", delivery="inbox", max_unapproved="dev")

        # (a) Approved plan_dispatch card, project switched to 'auto' -> RPC always_allow (both confirm=False & True)
        prj1 = self.project("lead-aauto", mode="require_approval", perm="plan")
        p1 = self.t(lead_tok, "plan.propose", {
            "project_id": prj1, "title": "Already Auto Plan", "summary": "S",
            "steps": [{"key": "s1", "title": "A", "spec": "X", "assignee": "w-aauto", "profile": "edit"}],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p1["approval_id"], "action": "allow", "plan_hash": p1["plan_hash"],
        }).get("ok"))
        self.assertTrue(self.u("project.set_permission_mode", {"project_id": prj1, "permission_mode": "auto"}).get("ok"))

        # plan.list, plan.show, and chat.list all agree: available_actions == [], already_auto_mode
        pl_list = self.u("plan.list", {"project_id": prj1})["data"]["items"][0]["approval"]
        pl_show = self.u("plan.show", {"plan_id": p1["id"]})["data"]["approval"]
        cl_apr = self.u("chat.list", {"project_id": prj1})["data"]["approvals_by_id"][p1["approval_id"]]
        for view in (pl_list, pl_show, cl_apr):
            self.assertEqual(view["available_actions"], [])
            self.assertIsNone(view["always_allow_effect"])
            self.assertFalse(view["decidable"])
            self.assertEqual(view["undecidable_reason"], "already_auto_mode")

        # Direct RPC always_allow with confirm_auto_mode=False AND confirm_auto_mode=True
        for confirm in (False, True):
            r = self.u("approval.decide", {
                "approval_id": p1["approval_id"],
                "action": "always_allow",
                "plan_hash": p1["plan_hash"],
                "confirm_auto_mode": confirm,
            })
            self.assertTrue(r.get("ok"), r)
            self.assertFalse(r["data"]["confirmation_required"])
            self.assertEqual(r["data"]["permission_mode"], "auto")
            self.assertEqual(r["data"]["previous_permission_mode"], "auto")
            self.assertFalse(r["data"]["mode_changed"])
            self.assertTrue(r["data"]["already_auto"])
            self.assertTrue(r["data"]["already_decided"])
            self.assertIn("已处于「全自动 (auto)」模式", r["data"]["message"])
            self.assertNotIn("将项目权限模式切换为", r["data"]["message"])

        # Only the single explicit project.set_permission_mode event exists (none from the already-auto calls)
        conn = self.harness.db()
        try:
            cnt_mode = conn.execute(
                "SELECT COUNT(*) FROM events WHERE project_id = ? AND type = 'project.permission_mode_changed';",
                (prj1,),
            ).fetchone()[0]
            self.assertEqual(cnt_mode, 1)
            cnt_dec = conn.execute(
                "SELECT COUNT(*) FROM events WHERE approval_id = ? AND type = 'approval.decided';",
                (p1["approval_id"],),
            ).fetchone()[0]
            self.assertEqual(cnt_dec, 1)
        finally:
            conn.close()

        # (b) Already-approved step_dispatch card after switching supervised -> auto
        prj2 = self.project("lead-aauto", mode="require_approval", perm="supervised")
        p2 = self.t(lead_tok, "plan.propose", {
            "project_id": prj2, "title": "Already Auto Step", "summary": "S",
            "steps": [{"key": "s1", "title": "Step 1", "spec": "X", "assignee": "w-aauto", "profile": "edit"}],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p2["approval_id"], "action": "allow", "plan_hash": p2["plan_hash"],
        }).get("ok"))
        d2 = self.t(lead_tok, "plan.dispatch", {"project_id": prj2, "plan_id": p2["id"]}, approval=p2["approval_id"])["data"]
        s_apr = d2["step_approvals"][0]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": s_apr["approval_id"], "action": "allow", "op_hash": s_apr["op_hash"],
        }).get("ok"))
        self.assertTrue(self.u("project.set_permission_mode", {"project_id": prj2, "permission_mode": "auto"}).get("ok"))

        r_step = self.u("approval.decide", {
            "approval_id": s_apr["approval_id"],
            "action": "always_allow",
            "op_hash": s_apr["op_hash"],
            "confirm_auto_mode": False,
        })
        self.assertTrue(r_step.get("ok"), r_step)
        self.assertFalse(r_step["data"]["confirmation_required"])
        self.assertEqual(r_step["data"]["permission_mode"], "auto")
        self.assertEqual(r_step["data"]["previous_permission_mode"], "auto")
        self.assertFalse(r_step["data"]["mode_changed"])
        self.assertTrue(r_step["data"]["already_auto"])
        self.assertIn("已处于「全自动 (auto)」模式", r_step["data"]["message"])
        self.assertNotIn("将项目权限模式切换为", r_step["data"]["message"])

        self.verify_audit()

    # ------------------------------------------------------------------
    # 20. R5 P3 + Nit: Legacy approval.approve/reject liveness & relayed-rejected -> direct upgrade
    # ------------------------------------------------------------------
    def test_r5_legacy_approve_refused_on_dead_cards_and_relayed_reject_upgrades(self) -> None:
        lead_tok = self.agent("lead-r5-leg", adapter="none", delivery="inbox", can_lead=True)
        w_tok = self.agent("w-r5-leg", adapter="none", delivery="inbox", max_unapproved="dev")
        rel_tok = self.agent("rel-r5-leg", adapter="none", delivery="inbox", caps=["intake", "relay"])

        # (a) Supervised step_dispatch & permission_escalation cards: progressed, terminal, and paused project
        prj = self.project("lead-r5-leg", mode="require_approval", perm="supervised")
        p = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "R5 Legacy Liveness Plan", "summary": "S",
            "steps": [
                {"key": "s1", "title": "Progressed Step", "spec": "X", "assignee": "w-r5-leg", "profile": "edit"},
                {"key": "s2", "title": "Terminal Esc", "spec": "Y", "assignee": "w-r5-leg", "profile": "elevated"},
                {"key": "s3", "title": "Paused Step", "spec": "Z", "assignee": "w-r5-leg", "profile": "edit"},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p["approval_id"], "action": "allow", "plan_hash": p["plan_hash"],
        }).get("ok"))
        disp = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": p["id"]}, approval=p["approval_id"])["data"]
        s1_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s1")
        s2_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s2")
        s3_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s3")
        t1_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s1")
        t2_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s2")

        # Progress t1 directly (created -> assigned/queued), cancel t2 (terminal)
        self.assertTrue(self.u("task.assign", {"task_id": t1_id, "to": "w-r5-leg"}).get("ok"))
        self.assertTrue(self.u("task.cancel", {"task_id": t2_id, "reason": "cancel"}).get("ok"))

        # Legacy approval.approve MUST refuse progressed task & terminal task
        r_prog = self.u("approval.approve", {"approval_id": s1_apr["approval_id"], "op_hash": s1_apr["op_hash"]})
        self.assertFalse(r_prog.get("ok"), r_prog)
        self.assertEqual(r_prog["error"]["code"], c.INVALID_TRANSITION)
        self.assertEqual(r_prog["error"]["details"].get("reason"), "task_progressed")

        r_term = self.u("approval.approve", {"approval_id": s2_apr["approval_id"], "op_hash": s2_apr["op_hash"]})
        self.assertFalse(r_term.get("ok"), r_term)
        self.assertEqual(r_term["error"]["code"], c.INVALID_TRANSITION)
        self.assertEqual(r_term["error"]["details"].get("reason"), "task_terminal")

        # Pause project -> legacy approval.approve MUST refuse s3 with PROJECT_NOT_ACTIVE
        self.assertTrue(self.u("project.pause", {"project_id": prj, "reason": "hold"}).get("ok"))
        r_paused = self.u("approval.approve", {"approval_id": s3_apr["approval_id"], "op_hash": s3_apr["op_hash"]})
        self.assertFalse(r_paused.get("ok"), r_paused)
        self.assertEqual(r_paused["error"]["code"], c.PROJECT_NOT_ACTIVE)

        # Legacy approval.reject still works on dead pending cards (dismisses them)
        rej_s1 = self.u("approval.reject", {"approval_id": s1_apr["approval_id"], "op_hash": s1_apr["op_hash"]})
        self.assertTrue(rej_s1.get("ok"), rej_s1)
        self.assertEqual(rej_s1["data"]["status"], "rejected")
        rej_s2 = self.u("approval.reject", {"approval_id": s2_apr["approval_id"], "op_hash": s2_apr["op_hash"]})
        self.assertTrue(rej_s2.get("ok"), rej_s2)
        self.assertEqual(rej_s2["data"]["status"], "rejected")
        rej_s3 = self.u("approval.reject", {"approval_id": s3_apr["approval_id"], "op_hash": s3_apr["op_hash"]})
        self.assertTrue(rej_s3.get("ok"), rej_s3)
        self.assertEqual(rej_s3["data"]["status"], "rejected")

        # (b) retry_over_limit (direct_or_relayed): BOTH direct user AND relayer refused on dead cards,
        #     and relayed-rejected upgrades to direct_verified via both approval.decide & approval.reject!
        prj_r = self.project("lead-r5-leg", mode="off", perm="plan")

        def _make_failed_retry_card(title: str):
            tid = self.t(lead_tok, "task.create", {
                "project": prj_r, "title": title, "spec": "S", "assignee": "w-r5-leg", "max_attempts": 1,
            })["data"]["id"]
            self.assertTrue(self.harness.wait_for(
                lambda: self.u("task.show", {"task_id": tid})["data"]["status"] == "queued", timeout=5.0
            ))
            st = self.t(w_tok, "task.start", {"task_id": tid})["data"]
            self.assertTrue(self.t(w_tok, "task.fail", {
                "task_id": tid, "run": st["run_id"], "fence": st["fence"], "reason": "boom",
            }).get("ok"))
            req = self.t(lead_tok, "task.retry", {"task_id": tid, "reason": "retry"})
            self.assertFalse(req.get("ok"))
            self.assertEqual(req["error"]["code"], c.APPROVAL_REQUIRED)
            return tid, req["error"]["details"]["approval_id"], req["error"]["details"]["op_hash"]

        tr1, r1_id, r1_hash = _make_failed_retry_card("Retry Prog Relayer")
        tr2, r2_id, r2_hash = _make_failed_retry_card("Retry Relayed Reject Decide")
        tr3, r3_id, r3_hash = _make_failed_retry_card("Retry Relayed Reject Legacy")

        # Progress tr1 directly -> relayer --relayed AND direct user approval.approve BOTH refused
        self.assertTrue(self.u("task.retry", {"task_id": tr1, "reason": "direct retry"}).get("ok"))
        rel_prog = self.t(rel_tok, "approval.approve", {
            "approval_id": r1_id, "op_hash": r1_hash,
        }, relayed=True, user_statement="approve")
        self.assertFalse(rel_prog.get("ok"), rel_prog)
        self.assertEqual(rel_prog["error"]["code"], c.INVALID_TRANSITION)
        self.assertEqual(rel_prog["error"]["details"].get("reason"), "task_progressed")

        usr_prog = self.u("approval.approve", {"approval_id": r1_id, "op_hash": r1_hash})
        self.assertFalse(usr_prog.get("ok"), usr_prog)
        self.assertEqual(usr_prog["error"]["code"], c.INVALID_TRANSITION)
        self.assertEqual(usr_prog["error"]["details"].get("reason"), "task_progressed")

        # Terminal (cancelled) project -> relayer --relayed AND direct user approval.approve on retry card
        # MUST be refused with PROJECT_NOT_ACTIVE (use a separate cancelled project with an orphaned pending retry card)
        prj_term = self.project("lead-r5-leg", mode="off", perm="plan")
        tid_term = self.t(lead_tok, "task.create", {
            "project": prj_term, "title": "Retry Terminal Proj", "spec": "S", "assignee": "w-r5-leg", "max_attempts": 1,
        })["data"]["id"]
        self.assertTrue(self.harness.wait_for(
            lambda: self.u("task.show", {"task_id": tid_term})["data"]["status"] == "queued", timeout=5.0
        ))
        st_term = self.t(w_tok, "task.start", {"task_id": tid_term})["data"]
        self.assertTrue(self.t(w_tok, "task.fail", {
            "task_id": tid_term, "run": st_term["run_id"], "fence": st_term["fence"], "reason": "boom",
        }).get("ok"))
        req_term = self.t(lead_tok, "task.retry", {"task_id": tid_term, "reason": "retry"})
        self.assertFalse(req_term.get("ok"))
        rt_id = req_term["error"]["details"]["approval_id"]
        rt_hash = req_term["error"]["details"]["op_hash"]
        # Mark project cancelled in DB without sweeping rt_id to verify legacy approval.approve's project gate directly
        conn_t = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        try:
            conn_t.execute("UPDATE projects SET status = 'cancelled' WHERE id = ?;", (prj_term,))
            conn_t.commit()
        finally:
            conn_t.close()
        rel_term = self.t(rel_tok, "approval.approve", {
            "approval_id": rt_id, "op_hash": rt_hash,
        }, relayed=True, user_statement="approve")
        self.assertFalse(rel_term.get("ok"), rel_term)
        self.assertEqual(rel_term["error"]["code"], c.PROJECT_NOT_ACTIVE)
        usr_term = self.u("approval.approve", {"approval_id": rt_id, "op_hash": rt_hash})
        self.assertFalse(usr_term.get("ok"), usr_term)
        self.assertEqual(usr_term["error"]["code"], c.PROJECT_NOT_ACTIVE)
        # Restore project status so audit.verify stays clean (no unrecorded direct SQLite status mutation)
        conn_t = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        try:
            conn_t.execute("UPDATE projects SET status = 'active' WHERE id = ?;", (prj_term,))
            conn_t.commit()
        finally:
            conn_t.close()

        # Relayer rejects r2 and r3 via --relayed -> chat.list exposes ['reject'] to upgrade to direct
        for rid, rhash in ((r2_id, r2_hash), (r3_id, r3_hash)):
            r_rel_rej = self.t(rel_tok, "approval.reject", {
                "approval_id": rid, "op_hash": rhash,
            }, relayed=True, user_statement="user said reject")
            self.assertTrue(r_rel_rej.get("ok"), r_rel_rej)
            self.assertEqual(r_rel_rej["data"]["status"], "rejected")
            self.assertEqual(r_rel_rej["data"]["trust"], "relayed_unverified")

        cl_before_up = self.u("chat.list", {"project_id": prj_r})["data"]
        self.assertEqual(cl_before_up["approvals_by_id"][r2_id]["available_actions"], ["reject"])
        self.assertTrue(cl_before_up["approvals_by_id"][r2_id]["decidable"])
        self.assertFalse(cl_before_up["approvals_by_id"][r2_id]["approvable"])

        # Direct user clicks reject via approval.decide on r2 -> upgrades relayed -> direct_verified
        up_dec = self.u("approval.decide", {"approval_id": r2_id, "action": "reject", "op_hash": r2_hash})
        self.assertTrue(up_dec.get("ok"), up_dec)
        self.assertEqual(up_dec["data"]["status"], "rejected")
        self.assertEqual(up_dec["data"]["decided_via"], "direct")
        self.assertEqual(up_dec["data"]["trust"], "direct_verified")
        self.assertEqual(up_dec["data"]["upgraded_from"], "relayed")
        self.assertTrue(up_dec["data"]["user_mac"])

        # Re-clicking reject is now idempotent (no duplicate direct event)
        up_idem = self.u("approval.decide", {"approval_id": r2_id, "action": "reject", "op_hash": r2_hash})
        self.assertTrue(up_idem.get("ok"), up_idem)
        self.assertTrue(up_idem["data"]["already_decided"])
        self.assertEqual(up_idem["data"]["trust"], "direct_verified")

        # Direct user upgrades r3 via legacy approval.reject -> direct_verified
        up_leg = self.u("approval.reject", {"approval_id": r3_id, "op_hash": r3_hash})
        self.assertTrue(up_leg.get("ok"), up_leg)
        self.assertEqual(up_leg["data"]["status"], "rejected")
        self.assertEqual(up_leg["data"]["decided_via"], "direct")
        self.assertEqual(up_leg["data"]["trust"], "direct_verified")
        self.assertEqual(up_leg["data"]["upgraded_from"], "relayed")
        self.assertTrue(up_leg["data"]["user_mac"])

        cl_after_up = self.u("chat.list", {"project_id": prj_r})["data"]
        for rid in (r2_id, r3_id):
            ent = cl_after_up["approvals_by_id"][rid]
            self.assertEqual(ent["available_actions"], [])
            self.assertFalse(ent["decidable"])
            self.assertEqual(ent["undecidable_reason"], "rejected")

        self.verify_audit()

    # ------------------------------------------------------------------
    # 21. R5 P3 + Nit: Dead-card reject dismissal & plan.show/chat.list serializer parity
    # ------------------------------------------------------------------
    def test_r5_dead_card_dismissal_via_reject_and_plan_summary_field_parity(self) -> None:
        lead_tok = self.agent("lead-r5-dis", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-r5-dis", adapter="none", delivery="inbox", max_unapproved="dev")

        # (a) Plan card serializer parity between chat.list and plan.show (active AND paused)
        prj1 = self.project("lead-r5-dis", mode="require_approval", perm="supervised")
        p1 = self.t(lead_tok, "plan.propose", {
            "project_id": prj1, "title": "Parity Plan", "summary": "S",
            "steps": [
                {"key": "s1", "title": "S1", "spec": "X", "assignee": "w-r5-dis", "profile": "edit"},
                {"key": "s2", "title": "S2", "spec": "Y", "assignee": "w-r5-dis", "profile": "edit"},
                {"key": "s3", "title": "S3", "spec": "Z", "assignee": "w-r5-dis", "profile": "edit"},
            ],
        })["data"]["plan"]

        cl_p1 = self.u("chat.list", {"project_id": prj1})["data"]["approvals_by_id"][p1["approval_id"]]
        ps_p1 = self.u("plan.show", {"plan_id": p1["id"]})["data"]["approval"]
        cl_p1_comparable = {k: v for k, v in cl_p1.items() if k not in ("plan_id", "plan_version", "plan_hash")}
        self.assertEqual(set(ps_p1.keys()), set(cl_p1_comparable.keys()))
        self.assertEqual(ps_p1, cl_p1_comparable)
        self.assertIn("task_status", ps_p1)
        self.assertIn("task_terminal", ps_p1)
        self.assertIn("task_progressed", ps_p1)

        # Approve & dispatch p1 so we have 3 step cards
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p1["approval_id"], "action": "allow", "plan_hash": p1["plan_hash"],
        }).get("ok"))
        disp = self.t(lead_tok, "plan.dispatch", {"project_id": prj1, "plan_id": p1["id"]}, approval=p1["approval_id"])["data"]
        s1_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s1")
        s2_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s2")
        s3_apr = next(sa for sa in disp["step_approvals"] if sa["step_key"] == "s3")
        t1_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s1")
        t2_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s2")
        t3_id = next(t["id"] for t in disp["tasks"] if t["plan_step_key"] == "s3")

        # Make s1 terminal-task dead card, s2 progressed-task dead card, and approve s3 BEFORE cancelling t3
        self.assertTrue(self.u("task.cancel", {"task_id": t1_id, "reason": "dead"}).get("ok"))
        self.assertTrue(self.u("task.assign", {"task_id": t2_id, "to": "w-r5-dis"}).get("ok"))
        self.assertTrue(self.u("approval.decide", {
            "approval_id": s3_apr["approval_id"], "action": "allow", "op_hash": s3_apr["op_hash"],
        }).get("ok"))
        self.assertTrue(self.u("task.cancel", {"task_id": t3_id, "reason": "dead after approve"}).get("ok"))

        cl_dead = self.u("chat.list", {"project_id": prj1})["data"]
        cards_dead = {it["approval_id"]: it for it in cl_dead["items"] if it.get("approval_id")}

        # Pending dead cards s1 & s2 expose reject-only dismissal; already-approved dead card s3 exposes []
        for apr, expected_reason in ((s1_apr, "task_terminal"), (s2_apr, "task_progressed")):
            aid = apr["approval_id"]
            ent = cl_dead["approvals_by_id"][aid]
            self.assertEqual(ent["available_actions"], ["reject"])
            self.assertEqual(cards_dead[aid]["meta"]["actions"], ["reject"])
            self.assertTrue(ent["decidable"])
            self.assertFalse(ent["approvable"])
            self.assertTrue(ent["dismiss_only"])
            self.assertIsNone(ent["undecidable_reason"])
            self.assertEqual(ent["unapprovable_reason"], expected_reason)

            # Dismiss through approval.decide(action="reject") -> succeeds cleanly
            dis = self.u("approval.decide", {
                "approval_id": aid, "action": "reject", "op_hash": apr["op_hash"], "reason": "dismiss stale",
            })
            self.assertTrue(dis.get("ok"), dis)
            self.assertEqual(dis["data"]["status"], "rejected")
            self.assertEqual(dis["data"]["decided_via"], "direct")
            self.assertTrue(dis["data"]["user_mac"])

        # Already-approved dead card s3 exposes [] and server refuses reject with DECISION_CONFLICT
        ent_s3 = cl_dead["approvals_by_id"][s3_apr["approval_id"]]
        self.assertEqual(ent_s3["available_actions"], [])
        self.assertEqual(cards_dead[s3_apr["approval_id"]]["meta"]["actions"], [])
        self.assertFalse(ent_s3["decidable"])
        self.assertFalse(ent_s3["dismiss_only"])
        self.assertEqual(ent_s3["undecidable_reason"], "task_terminal")
        r_s3_rej = self.u("approval.decide", {
            "approval_id": s3_apr["approval_id"], "action": "reject", "op_hash": s3_apr["op_hash"],
        })
        self.assertFalse(r_s3_rej.get("ok"), r_s3_rej)
        self.assertEqual(r_s3_rej["error"]["code"], c.DECISION_CONFLICT)

        # After dismissing s1 and s2, chat.list clears their available_actions & meta.actions to []
        cl_cleared = self.u("chat.list", {"project_id": prj1})["data"]
        cards_cleared = {it["approval_id"]: it for it in cl_cleared["items"] if it.get("approval_id")}
        for apr in (s1_apr, s2_apr):
            aid = apr["approval_id"]
            self.assertEqual(cl_cleared["approvals_by_id"][aid]["available_actions"], [])
            self.assertEqual(cards_cleared[aid]["meta"]["actions"], [])
            self.assertFalse(cl_cleared["approvals_by_id"][aid]["decidable"])
            self.assertEqual(cl_cleared["approvals_by_id"][aid]["undecidable_reason"], "rejected")

        # (b) Paused project pending plan card can be dismissed via reject (marks plan rejected too)
        prj2 = self.project("lead-r5-dis", mode="require_approval", perm="plan")
        p2 = self.t(lead_tok, "plan.propose", {
            "project_id": prj2, "title": "Paused Dismiss Plan", "summary": "S",
            "steps": [{"key": "a1", "title": "A1", "spec": "X", "assignee": "w-r5-dis"}],
        })["data"]["plan"]
        self.assertTrue(self.u("project.pause", {"project_id": prj2, "reason": "pause"}).get("ok"))
        cl_p2 = self.u("chat.list", {"project_id": prj2})["data"]
        ent_p2 = cl_p2["approvals_by_id"][p2["approval_id"]]
        self.assertEqual(ent_p2["available_actions"], ["reject"])
        self.assertTrue(ent_p2["dismiss_only"])
        self.assertEqual(ent_p2["unapprovable_reason"], "project_not_active")

        dis_p2 = self.u("approval.decide", {
            "approval_id": p2["approval_id"], "action": "reject", "plan_hash": p2["plan_hash"],
        })
        self.assertTrue(dis_p2.get("ok"), dis_p2)
        self.assertEqual(dis_p2["data"]["status"], "rejected")
        self.assertEqual(dis_p2["data"]["plan_status"], "rejected")

        cl_p2_after = self.u("chat.list", {"project_id": prj2})["data"]
        self.assertEqual(cl_p2_after["approvals_by_id"][p2["approval_id"]]["available_actions"], [])
        self.assertFalse(cl_p2_after["approvals_by_id"][p2["approval_id"]]["decidable"])

        self.verify_audit()

    # ------------------------------------------------------------------
    # 19. R6 P2-1: Agent-forged external.* cards CANNOT mint grants or flip to auto
    #     (PoCs A1, A2, I from REVIEW-FINAL.md)
    # ------------------------------------------------------------------
    def test_r6_p2_1_external_cards_cannot_mint_grants_or_flip_auto(self) -> None:
        lead_tok = self.agent("lead-r6-p21", adapter="none", delivery="inbox", can_lead=True)
        relay_tok = self.agent("relay-r6-p21", adapter="none", delivery="inbox", caps=["relay", "intake"])
        self.agent("w-r6-p21", adapter="none", delivery="inbox", max_unapproved="dev")
        prj = self.project("lead-r6-p21", mode="require_approval", perm="plan")

        # 1. Internal-only categories (step_dispatch, plan_dispatch) refused on approval.request for both lead & relayer
        for who, tok in (("lead", lead_tok), ("relayer", relay_tok)):
            for cat in ("step_dispatch", "plan_dispatch"):
                r = self.t(tok, "approval.request", {
                    "category": cat,
                    "project_id": prj,
                    "summary": f"forged {cat} by {who}",
                    "operation": {"x": who},
                })
                self.assertFalse(r.get("ok"), f"{who} requested internal category {cat}: {r}")
                self.assertEqual(r["error"]["code"], c.INVALID_ARGUMENT)

        # 2. External permission_escalation & retry_over_limit cards NEVER offer always_allow and NEVER mint grants
        for who, tok in (("relayer", relay_tok), ("lead", lead_tok)):
            r_esc = self.t(tok, "approval.request", {
                "category": "permission_escalation",
                "project_id": prj,
                "summary": f"Allow reading docs folder ({who})",
                "operation": {"to": "w-r6-p21", "profile": "unrestricted", "who": who},
            })
            self.assertTrue(r_esc.get("ok"), r_esc)
            aid_esc, oh_esc = r_esc["data"]["approval_id"], r_esc["data"]["op_hash"]

            r_ret = self.t(tok, "approval.request", {
                "category": "retry_over_limit",
                "project_id": prj,
                "summary": f"one more try ({who})",
                "operation": {"n": who},
            })
            self.assertTrue(r_ret.get("ok"), r_ret)
            aid_ret, oh_ret = r_ret["data"]["approval_id"], r_ret["data"]["op_hash"]

            cl = self.u("chat.list", {"project_id": prj})["data"]
            cards_by_aid = {it["approval_id"]: it for it in cl["items"] if it.get("approval_id")}
            for aid, oh in ((aid_esc, oh_esc), (aid_ret, oh_ret)):
                ent = cl["approvals_by_id"][aid]
                self.assertEqual(ent["available_actions"], ["allow", "reject"])
                self.assertIsNone(ent["always_allow_effect"])
                self.assertEqual(cards_by_aid[aid]["meta"]["actions"], ["allow", "reject"])
                self.assertNotIn("始终允许", cards_by_aid[aid]["body"])

                dec = self.u("approval.decide", {
                    "approval_id": aid,
                    "action": "always_allow",
                    "op_hash": oh,
                    "confirm_auto_mode": True,
                })
                self.assertFalse(dec.get("ok"), dec)
                self.assertEqual(dec["error"]["code"], c.INVALID_ARGUMENT)

        self.assertEqual(self.u("project.show", {"project_id": prj})["data"]["permission_mode"], "plan")
        self.assertEqual(self.u("grant.list", {"project_id": prj})["data"]["items"], [])

        # Lead's next plan with an unrestricted step STILL requires an internal escalation card
        p = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "T", "summary": "S",
            "steps": [{"key": "s1", "title": "A", "spec": "X", "assignee": "w-r6-p21", "profile": "unrestricted"}],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": p["approval_id"], "action": "allow", "plan_hash": p["plan_hash"],
        }).get("ok"))
        dsp = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": p["id"]}, approval=p["approval_id"])["data"]
        self.assertEqual(dsp["tasks"][0]["status"], "created")
        self.assertEqual(len(dsp["escalated_approvals"]), 1)

        self.verify_audit()

    # ------------------------------------------------------------------
    # 20. R6 P3-1: supervised escalated step rejects relayed & delegated decisions
    #     even when approval.policy.permission_escalation == direct_or_relayed (PoC E)
    # ------------------------------------------------------------------
    def test_r6_p3_1_supervised_escalated_step_rejects_relayed_and_delegated(self) -> None:
        lead_tok = self.agent("lead-r6-e", adapter="none", delivery="inbox", can_lead=True)
        relay_tok = self.agent("relay-r6-e", adapter="none", delivery="inbox", caps=["relay", "intake"])
        self.agent("w-r6-e", adapter="none", delivery="inbox", max_unapproved="dev")

        # Explicitly relax permission_escalation policy and enable delegated mode
        self.assertTrue(self.u("config.set", {
            "key": "approval.policy.permission_escalation", "value": "direct_or_relayed",
        }).get("ok"))
        self.assertTrue(self.u("config.set", {
            "key": "approval.mode", "value": "delegated",
        }).get("ok"))

        prj = self.project("lead-r6-e", mode="require_approval", perm="supervised")
        plan = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Sup Esc", "summary": "S",
            "steps": [
                {"key": "s1", "title": "Esc", "spec": "X", "assignee": "w-r6-e", "profile": "elevated"},
                {"key": "s2", "title": "Norm", "spec": "Y", "assignee": "w-r6-e", "profile": "edit"},
            ],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": plan["approval_id"], "action": "allow", "plan_hash": plan["plan_hash"],
        }).get("ok"))
        dsp = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": plan["id"]}, approval=plan["approval_id"])["data"]

        # Both the escalated step card AND the normal step card refuse relayed and delegated approval!
        for sa in dsp["step_approvals"]:
            ap_rel = self.t(relay_tok, "approval.approve", {
                "approval_id": sa["approval_id"], "op_hash": sa["op_hash"],
            }, relayed=True)
            self.assertFalse(ap_rel.get("ok"), f"Relayer approved {sa['category']} in supervised mode: {ap_rel}")
            self.assertEqual(ap_rel["error"]["code"], c.CHANNEL_NOT_ALLOWED)

            ap_del = self.t(lead_tok, "approval.approve", {
                "approval_id": sa["approval_id"], "op_hash": sa["op_hash"],
                "human_confirmed": "yes run it",
            })
            self.assertFalse(ap_del.get("ok"), f"Delegated lead approved {sa['category']} in supervised mode: {ap_del}")
            self.assertEqual(ap_del["error"]["code"], c.CHANNEL_NOT_ALLOWED)

        # Also test consume-time defense: card relayed-approved while in 'plan' mode cannot be consumed
        # after switching project to 'supervised' mode!
        prj2 = self.project("lead-r6-e", mode="require_approval", perm="plan")
        plan2 = self.t(lead_tok, "plan.propose", {
            "project_id": prj2, "title": "SwitchToSup", "summary": "S",
            "steps": [{"key": "s1", "title": "Esc", "spec": "X", "assignee": "w-r6-e", "profile": "elevated"}],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": plan2["approval_id"], "action": "allow", "plan_hash": plan2["plan_hash"],
        }).get("ok"))
        dsp2 = self.t(lead_tok, "plan.dispatch", {"project_id": prj2, "plan_id": plan2["id"]}, approval=plan2["approval_id"])["data"]
        esc2 = dsp2["escalated_approvals"][0]
        # Under 'plan' mode with direct_or_relayed, relayer approves the escalation card
        self.assertTrue(self.t(relay_tok, "approval.approve", {
            "approval_id": esc2["approval_id"], "op_hash": esc2["op_hash"],
        }, relayed=True).get("ok"))
        # User switches project to 'supervised' before task.assign -> consume MUST refuse relayed approval!
        self.assertTrue(self.u("project.set_permission_mode", {
            "project_id": prj2, "permission_mode": "supervised",
        }).get("ok"))
        assign_bad = self.t(lead_tok, "task.assign", {
            "task_id": esc2["task_id"], "to": "w-r6-e",
        }, approval=esc2["approval_id"])
        self.assertFalse(assign_bad.get("ok"), assign_bad)
        self.assertEqual(assign_bad["error"]["code"], c.CHANNEL_NOT_ALLOWED)

        # Direct user upgrading the card to direct_verified lets task.assign succeed
        self.assertTrue(self.u("approval.decide", {
            "approval_id": esc2["approval_id"], "action": "allow", "op_hash": esc2["op_hash"],
        }).get("ok"))
        assign_ok = self.t(lead_tok, "task.assign", {
            "task_id": esc2["task_id"], "to": "w-r6-e",
        }, approval=esc2["approval_id"])
        self.assertTrue(assign_ok.get("ok"), assign_ok)
        self.assertEqual(assign_ok["data"]["status"], "assigned")

        self.verify_audit()

    # ------------------------------------------------------------------
    # 21. R6 P3-2: supervised always_allow on escalation card consumes approval cleanly
    #     AND subsequent step_dispatch card with pre-existing grant still consumes cleanly (PoC B)
    # ------------------------------------------------------------------
    def test_r6_p3_2_supervised_escalation_always_allow_consumes_approval_cleanly(self) -> None:
        lead_tok = self.agent("lead-r6-b", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-r6-b", adapter="none", delivery="inbox", max_unapproved="dev")
        prj = self.project("lead-r6-b", mode="require_approval", perm="supervised")

        plan1 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Sup Esc Always", "summary": "S",
            "steps": [{"key": "s1", "title": "A", "spec": "X", "assignee": "w-r6-b", "profile": "elevated"}],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": plan1["approval_id"], "action": "allow", "plan_hash": plan1["plan_hash"],
        }).get("ok"))
        dsp1 = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": plan1["id"]}, approval=plan1["approval_id"])["data"]
        sa1 = dsp1["step_approvals"][0]
        self.assertEqual(sa1["category"], "permission_escalation")

        # User clicks always_allow on the escalation card -> mints standing grant AND approves card
        dec1 = self.u("approval.decide", {
            "approval_id": sa1["approval_id"], "action": "always_allow", "op_hash": sa1["op_hash"],
        })
        self.assertTrue(dec1.get("ok"), dec1)
        self.assertTrue(dec1["data"]["grant_created"])

        # Lead executes task.assign with that exact approval -> MUST SUCCEED (no OP_HASH_MISMATCH, no 2nd card!)
        a1 = self.t(lead_tok, "task.assign", {
            "task_id": sa1["task_id"], "to": "w-r6-b",
        }, approval=sa1["approval_id"])
        self.assertTrue(a1.get("ok"), a1)
        self.assertEqual(a1["data"]["status"], "assigned")
        self.assertEqual(
            self.u("approval.show", {"approval_id": sa1["approval_id"]})["data"]["status"],
            "consumed",
        )

        # Second plan in same supervised project (standing grant now pre-exists):
        # plan.dispatch emits a step_dispatch card, user clicks allow, task.assign consumes step_dispatch + records grant.used!
        plan2 = self.t(lead_tok, "plan.propose", {
            "project_id": prj, "title": "Sup Esc Second", "summary": "S2",
            "steps": [{"key": "s2", "title": "B", "spec": "Y", "assignee": "w-r6-b", "profile": "elevated"}],
        })["data"]["plan"]
        self.assertTrue(self.u("approval.decide", {
            "approval_id": plan2["approval_id"], "action": "allow", "plan_hash": plan2["plan_hash"],
        }).get("ok"))
        dsp2 = self.t(lead_tok, "plan.dispatch", {"project_id": prj, "plan_id": plan2["id"]}, approval=plan2["approval_id"])["data"]
        sa2 = dsp2["step_approvals"][0]
        self.assertEqual(sa2["category"], "step_dispatch")
        self.assertEqual(dsp2["escalated_approvals"], [])
        self.assertTrue(self.u("approval.decide", {
            "approval_id": sa2["approval_id"], "action": "allow", "op_hash": sa2["op_hash"],
        }).get("ok"))
        a2 = self.t(lead_tok, "task.assign", {
            "task_id": sa2["task_id"], "to": "w-r6-b",
        }, approval=sa2["approval_id"])
        self.assertTrue(a2.get("ok"), a2)
        self.assertEqual(a2["data"]["status"], "assigned")

        self.verify_audit()

    # ------------------------------------------------------------------
    # 22. R6 P3-4: plan.propose rejects secret-bearing title, summary, or step fields (PoC F)
    # ------------------------------------------------------------------
    def test_r6_p3_4_plan_propose_rejects_secret_bearing_text(self) -> None:
        lead_tok = self.agent("lead-r6-f", adapter="none", delivery="inbox", can_lead=True)
        self.agent("w-r6-f", adapter="none", delivery="inbox")
        prj = self.project("lead-r6-f", mode="require_approval", perm="plan")
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

        cases = (
            ("steps", {
                "project_id": prj, "title": "Clean", "summary": "Clean",
                "steps": [{"key": "s1", "title": "A", "spec": f"curl -H 'Authorization: token {secret}' https://example.com", "assignee": "w-r6-f"}],
            }),
            ("title", {
                "project_id": prj, "title": f"Deploy with {secret}", "summary": "Clean",
                "steps": [{"key": "s1", "title": "A", "spec": "clean spec", "assignee": "w-r6-f"}],
            }),
            ("summary", {
                "project_id": prj, "title": "Clean", "summary": f"Uses token {secret}",
                "steps": [{"key": "s1", "title": "A", "spec": "clean spec", "assignee": "w-r6-f"}],
            }),
        )
        for expected_param, args in cases:
            with self.subTest(param=expected_param):
                res = self.t(lead_tok, "plan.propose", args)
                self.assertFalse(res.get("ok"), res)
                self.assertEqual(res["error"]["code"], c.INVALID_ARGUMENT)
                self.assertEqual(res["error"]["details"].get("param"), expected_param)

        self.assertEqual(self.u("plan.list", {"project_id": prj})["data"]["items"], [])
        self.verify_audit()

    # ------------------------------------------------------------------
    # 23. R6 P3-5: Non-lead agent with intake cannot create ungated sibling project
    #     assigned to a lead that leads an active require_approval project (PoC H)
    # ------------------------------------------------------------------
    def test_r6_p3_5_non_lead_intake_inherits_require_approval_for_gated_lead(self) -> None:
        lead_tok = self.agent("lead-r6-h", adapter="none", delivery="inbox", can_lead=True)
        worker_tok = self.agent("w-r6-h", adapter="none", delivery="inbox")
        self.agent("w2-r6-h", adapter="none", delivery="inbox")

        # Lead leads an active gated project
        self.project("lead-r6-h", mode="require_approval", perm="plan")

        # Worker (has default `intake` cap) creates sibling project led by lead-r6-h -> MUST be gated!
        sib = self.t(worker_tok, "intake.submit", {
            "title": "Sibling", "goal": "G", "criterion": ["done"], "lead": "lead-r6-h",
        })
        self.assertTrue(sib.get("ok"), sib)
        self.assertEqual(sib["data"]["plan_gate_mode"], "require_approval")

        # Lead's raw task.create in that sibling project MUST be blocked by PLAN_APPROVAL_REQUIRED
        raw = self.t(lead_tok, "task.create", {
            "project": sib["data"]["id"], "title": "Sneaky", "spec": "s", "kind": "general", "assignee": "w2-r6-h",
        })
        self.assertFalse(raw.get("ok"), raw)
        self.assertEqual(raw["error"]["code"], c.PLAN_APPROVAL_REQUIRED)

        self.verify_audit()


class TestMigrationLockAndBackup(unittest.TestCase):
    """Migration acquires single-instance lock BEFORE migrating and writes mesh.db.v1.bak."""

    def test_second_daemon_does_not_migrate_locked_v1_db_and_upgrade_creates_backup(self) -> None:
        h = MeshHarness(init_db=False, start_daemon=False, check_invariants_on_teardown=False)
        try:
            os.makedirs(os.path.join(h.home, "run"), mode=0o700, exist_ok=True)
            db_path = os.path.join(h.home, "mesh.db")
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            with transaction(conn):
                for stmt in INITIAL_SCHEMA_STATEMENTS:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO principals (id, kind, name, created_at) "
                    "VALUES ('principal_system', 'system', 'system', '2026-01-01T00:00:00.000Z');"
                )
                conn.execute(
                    "INSERT INTO principals (id, kind, name, created_at) "
                    "VALUES ('usr_1', 'user', 'user', '2026-01-01T00:00:00.000Z');"
                )
            conn.close()
            os.chmod(db_path, 0o600)

            from mesh.platform import locks

            lock_path = os.path.join(h.home, "run", "meshd.lock")
            lock_fd = locks.open_lock_file(lock_path, mode=0o600)
            locks.acquire_lock(lock_fd, exclusive=True, blocking=False)
            try:
                p = subprocess.run(
                    [h.bin_mesh, "--home", h.home, "daemon", "run"],
                    env=h.get_env(),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(p.returncode, 0)
                c_check = connect(db_path, read_only=True)
                try:
                    self.assertEqual(get_schema_version(c_check), 1)
                finally:
                    c_check.close()
                self.assertFalse(os.path.exists(os.path.join(h.home, "mesh.db.v1.bak")))
            finally:
                os.close(lock_fd)

            from mesh.daemon.server import DaemonServer

            srv = DaemonServer(h.home, testing_mode=True)
            srv._defer_migration = True
            srv._pending_storage_open = False
            try:
                srv.setup_storage()
                srv.acquire_lock()
                srv._complete_storage_after_lock()
                self.assertEqual(get_schema_version(srv.db_conn), 2)
                # standing_grants and projects.permission_mode exist after upgrade
                self.assertIsNotNone(srv.db_conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='standing_grants';"
                ).fetchone())
            finally:
                if srv.db_conn is not None:
                    srv.db_conn.close()
                srv.release_lock()

            bak_path = os.path.join(h.home, "mesh.db.v1.bak")
            self.assertTrue(os.path.exists(bak_path))
            c_bak = connect(bak_path, read_only=True)
            try:
                self.assertEqual(get_schema_version(c_bak), 1)
            finally:
                c_bak.close()
        finally:
            h.teardown()

    def test_corrupt_v1_db_rejected_before_backup_or_migration(self) -> None:
        """P3-3: PRAGMA quick_check gates startup BEFORE any backup or schema migration."""
        import struct
        from mesh.errors import DbIntegrityError
        from mesh.daemon.server import DaemonServer

        for defer in (True, False):
            with self.subTest(defer_migration=defer):
                h = MeshHarness(init_db=False, start_daemon=False, check_invariants_on_teardown=False)
                try:
                    os.makedirs(os.path.join(h.home, "run"), mode=0o700, exist_ok=True)
                    db_path = os.path.join(h.home, "mesh.db")
                    conn = sqlite3.connect(db_path)
                    conn.execute("PRAGMA journal_mode=WAL;")
                    with transaction(conn):
                        for stmt in INITIAL_SCHEMA_STATEMENTS:
                            conn.execute(stmt)
                        conn.execute(
                            "INSERT INTO principals (id, kind, name, created_at) "
                            "VALUES ('principal_system', 'system', 'system', '2026-01-01T00:00:00.000Z');"
                        )
                        conn.execute(
                            "INSERT INTO principals (id, kind, name, created_at) "
                            "VALUES ('usr_1', 'user', 'user', '2026-01-01T00:00:00.000Z');"
                        )
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    conn.close()
                    os.chmod(db_path, 0o600)

                    # Corrupt header freelist count (DB stays readable for meta queries, only quick_check fails)
                    with open(db_path, "r+b") as f:
                        f.seek(32)
                        self.assertEqual(f.read(8), b"\0" * 8)
                        f.seek(36)
                        f.write(struct.pack(">I", 7))

                    srv = DaemonServer(h.home, testing_mode=True)
                    srv._defer_migration = defer
                    srv._pending_storage_open = False
                    try:
                        with self.assertRaises(DbIntegrityError):
                            srv.setup_storage()
                            if defer:
                                srv.acquire_lock()
                                srv._complete_storage_after_lock()
                    finally:
                        if srv.db_conn is not None:
                            srv.db_conn.close()
                        srv.release_lock()

                    # Corrupt DB MUST NOT have been backed up or migrated to v2
                    self.assertFalse(os.path.exists(os.path.join(h.home, "mesh.db.v1.bak")))
                    c_check = connect(db_path, read_only=True)
                    try:
                        self.assertEqual(get_schema_version(c_check), 1)
                    finally:
                        c_check.close()
                finally:
                    h.teardown()


if __name__ == "__main__":
    unittest.main()
