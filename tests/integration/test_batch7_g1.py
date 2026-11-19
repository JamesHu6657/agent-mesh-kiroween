"""Regression tests for batch7 g1 findings (authz, approvals, agent & task handlers)."""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness
from tests.integration.test_execution_launch_fences import get_proc_environ, run_cli_as
from tests.integration.test_tasks import create_agent
from tests.stubs import create_test_stubs


class TestBatch7G1(unittest.TestCase):
    """Focused regression tests for all 9 batch7 g1 findings."""

    def test_f1_direct_user_task_fail_on_running_launch_task_preserves_audit_verify(self) -> None:
        """[P1] Direct --user `task fail` on a running launch task signs `task.fail_requested`
        and emits system `task.failed` on exit so `audit verify --user` succeeds."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            create_agent(h, "fx", adapter="fake", delivery="launch")
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            rc_p, resp_p = h.cli("intake", "submit", "--title", "P", "--goal", "G", "--criterion", "c1", token=tok_intake)
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            gate = os.path.join(h.fixtures_dir, "f1-fail-gate")
            rc_t, resp_t = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Launch fail user",
                "--spec", f'FAKE {{"wait_release": {{"path": "{gate}"}}}}',
                "--assignee", "fx",
                token=tok_lead,
            )
            self.assertEqual(rc_t, 0, resp_t)
            tsk_id = resp_t["data"]["id"]

            def _running_run():
                conn = h.db()
                try:
                    return conn.execute(
                        "SELECT r.id, r.fence, r.wrapper_pid FROM runs r JOIN tasks t ON t.id = r.task_id "
                        "WHERE r.task_id = ? AND t.status = 'running' AND r.status = 'running' AND r.wrapper_pid IS NOT NULL;",
                        (tsk_id,),
                    ).fetchone()
                finally:
                    conn.close()

            self.assertTrue(h.wait_for(lambda: _running_run() is not None, timeout=15.0))
            run_row = _running_run()
            assert run_row is not None

            # Direct --user task fail while launch run is still active
            rc_f, resp_f = h.cli(
                "task", "fail", tsk_id,
                "--run", run_row["id"],
                "--fence", str(run_row["fence"]),
                "--reason", "abort",
                user=True,
            )
            self.assertEqual(rc_f, 0, resp_f)
            self.assertTrue(resp_f["data"]["failure_pending"])

            # Verify K_user-signed task.fail_requested was recorded immediately
            conn = h.db()
            try:
                req_evt = conn.execute(
                    "SELECT actor_kind, via, user_mac, to_state FROM events "
                    "WHERE task_id = ? AND type = 'task.fail_requested';",
                    (tsk_id,),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(req_evt)
            self.assertEqual((req_evt["actor_kind"], req_evt["via"]), ("user", "direct"))
            self.assertTrue(req_evt["user_mac"])
            self.assertIsNone(req_evt["to_state"])

            # Release run and wait for supervisor to finalize task.failed
            open(gate, "w").close()
            rc_w, _ = h.cli("task", "wait", tsk_id, "--until", "state:failed", "--timeout", "8s", user=True)
            self.assertEqual(rc_w, 0)

            conn = h.db()
            try:
                fail_evt = conn.execute(
                    "SELECT actor_id, actor_kind, via, user_mac FROM events "
                    "WHERE task_id = ? AND type = 'task.failed';",
                    (tsk_id,),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(fail_evt)
            self.assertEqual((fail_evt["actor_id"], fail_evt["actor_kind"], fail_evt["via"]), ("system", "system", "system"))

            rc_aud, resp_aud = h.cli("audit", "verify", user=True)
            self.assertEqual(rc_aud, 0, resp_aud)
            self.assertGreater(resp_aud["data"]["events_verified"], 0)
        finally:
            h.teardown()

    def test_f2_delegated_approval_rechecks_mode_self_approve_and_lead_at_consume(self) -> None:
        """[P1] Delegated approvals enforce approval.mode='delegated', delegated_self_approve,
        and active project lead at consume time (both internal and `approval consume`)."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead1 = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_lead2 = create_agent(h, "lead2", adapter="none", delivery="inbox", can_lead=True)
            tok_plain = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            rc_p, resp_p = h.cli("intake", "submit", "--title", "P", "--goal", "G", "--criterion", "c1", token=tok_plain)
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            def set_mode(m: str) -> None:
                self.assertEqual(h.cli("config", "set", "approval.mode", json.dumps(m), user=True)[0], 0)

            def req_ext(tok: str, summary: str):
                rc_r, resp_r = h.cli(
                    "approval", "request",
                    "--category", "deploy",
                    "--project", prj_id,
                    "--summary", summary,
                    "--op", json.dumps({"target": summary}),
                    token=tok,
                )
                self.assertEqual(rc_r, 0, resp_r)
                return resp_r["data"]["approval_id"], resp_r["data"]["op_hash"]

            # --- Case A: revert approval.mode to 'passphrase' before consuming ---
            set_mode("delegated")
            rc_pause1, resp_pause1 = h.cli("project", "pause", prj_id, token=tok_lead1)
            self.assertEqual(rc_pause1, c.EXIT_APPROVAL_REQUIRED)
            apr_a = resp_pause1["error"]["details"]["approval_id"]
            hash_a = resp_pause1["error"]["details"]["op_hash"]
            self.assertEqual(
                h.cli("approval", "approve", apr_a, "--op-hash", hash_a, "--human-confirmed", "James: yes", token=tok_lead1)[0],
                0,
            )

            apr_ext_a, hash_ext_a = req_ext(tok_lead1, "deploy-a")
            self.assertEqual(
                h.cli("approval", "approve", apr_ext_a, "--op-hash", hash_ext_a, "--human-confirmed", "James: ok", token=tok_lead1)[0],
                0,
            )

            # Audit verify exposes delegated_decisions count
            rc_aud, resp_aud = h.cli("audit", "verify", user=True)
            self.assertEqual(rc_aud, 0, resp_aud)
            self.assertEqual(resp_aud["data"]["delegated_decisions"], 2)

            set_mode("passphrase")
            rc_ca1, resp_ca1 = h.cli("project", "pause", prj_id, "--approval", apr_a, token=tok_lead1)
            self.assertEqual(rc_ca1, c.EXIT_APPROVAL_INVALID, resp_ca1)
            self.assertEqual(resp_ca1["error"]["code"], c.CHANNEL_NOT_ALLOWED)

            rc_ca2, resp_ca2 = h.cli("approval", "consume", apr_ext_a, "--op-hash", hash_ext_a, token=tok_lead1)
            self.assertEqual(rc_ca2, c.EXIT_APPROVAL_INVALID, resp_ca2)
            self.assertEqual(resp_ca2["error"]["code"], c.CHANNEL_NOT_ALLOWED)

            # --- Case B: disable delegated_self_approve before consuming ---
            set_mode("delegated")
            self.assertEqual(h.cli("config", "set", "approval.delegated_self_approve", "false", user=True)[0], 0)
            rc_cb1, resp_cb1 = h.cli("project", "pause", prj_id, "--approval", apr_a, token=tok_lead1)
            self.assertEqual(rc_cb1, c.EXIT_APPROVAL_INVALID, resp_cb1)
            self.assertEqual(resp_cb1["error"]["code"], c.CHANNEL_NOT_ALLOWED)

            rc_cb2, resp_cb2 = h.cli("approval", "consume", apr_ext_a, "--op-hash", hash_ext_a, token=tok_lead1)
            self.assertEqual(rc_cb2, c.EXIT_APPROVAL_INVALID, resp_cb2)
            self.assertEqual(resp_cb2["error"]["code"], c.CHANNEL_NOT_ALLOWED)

            # --- Case C: supersede lead1 via project set-lead before consuming ---
            self.assertEqual(h.cli("config", "set", "approval.delegated_self_approve", "true", user=True)[0], 0)
            # Have another agent (with relay capability) request an external approval on prj_id,
            # so requested_by != lead1; lead1 approves via delegation, then set-lead replaces lead1.
            tok_worker = create_agent(h, "w_ext", adapter="none", delivery="inbox", capabilities=["relay"])
            rc_t, resp_t = h.cli("task", "create", "--project", prj_id, "--title", "T", "--spec", "S", "--assignee", "w_ext", token=tok_lead1)
            self.assertEqual(rc_t, 0, resp_t)
            apr_ext_c, hash_ext_c = req_ext(tok_worker, "deploy-worker")
            self.assertEqual(
                h.cli("approval", "approve", apr_ext_c, "--op-hash", hash_ext_c, "--human-confirmed", "James: ship", token=tok_lead1)[0],
                0,
            )

            # Disabling lead1 blocks worker consumption of delegated approval
            self.assertEqual(h.cli("agent", "disable", "lead1", user=True)[0], 0)
            rc_dis, resp_dis = h.cli("approval", "consume", apr_ext_c, "--op-hash", hash_ext_c, token=tok_worker)
            self.assertEqual(rc_dis, c.EXIT_APPROVAL_INVALID, resp_dis)
            self.assertEqual(resp_dis["error"]["code"], c.CHANNEL_NOT_ALLOWED)
            self.assertEqual(h.cli("agent", "enable", "lead1", user=True)[0], 0)

            rc_sl, resp_sl = h.cli("project", "set-lead", prj_id, "lead2", "--reason", "rotate", user=True)
            self.assertEqual(rc_sl, 0, resp_sl)

            # Both unconsumed delegated approvals decided by lead1 were cancelled on set-lead
            for aid in (apr_a, apr_ext_a, apr_ext_c):
                rc_sh, resp_sh = h.cli("approval", "show", aid, user=True)
                self.assertEqual(rc_sh, 0)
                self.assertEqual(resp_sh["data"]["status"], "cancelled")

            rc_cc, resp_cc = h.cli("approval", "consume", apr_ext_c, "--op-hash", hash_ext_c, token=tok_worker)
            self.assertEqual(rc_cc, c.EXIT_APPROVAL_INVALID, resp_cc)
            self.assertEqual(resp_cc["error"]["code"], c.APPROVAL_CANCELLED)

            rc_aud2, resp_aud2 = h.cli("audit", "verify", user=True)
            self.assertEqual(rc_aud2, 0, resp_aud2)
            self.assertEqual(resp_aud2["data"]["delegated_decisions"], 3)
        finally:
            h.teardown()

    def test_f3_reassign_and_retry_compound_retry_over_limit_and_permission_escalation(self) -> None:
        """[P1] `task reassign` (and `task retry`) with both `retry_over_limit` and
        `permission_escalation` succeeds with a single direct approval and enforces direct_only."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            tok_grok = create_agent(h, "grok", adapter="none", delivery="inbox", capabilities=["intake", "relay"])
            create_agent(h, "w1", adapter="fake", delivery="launch", default_profile="edit", max_unapproved_profile="edit")
            create_agent(h, "w2", adapter="fake", delivery="launch", default_profile="edit", max_unapproved_profile="edit")
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            prj_id = h.cli("intake", "submit", "--title", "P", "--goal", "G", "--criterion", "c1", token=tok_intake)[1]["data"]["id"]

            # Create an elevated task with max_attempts=1 (--user bypasses initial gate) and let it fail at 1/1
            rc_t, resp_t = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Compound gate task",
                "--spec", 'FAKE {"fail": {"code": 1}}',
                "--assignee", "w1",
                "--profile", "elevated",
                "--max-attempts", "1",
                user=True,
            )
            self.assertEqual(rc_t, 0, resp_t)
            tsk_id = resp_t["data"]["id"]
            self.assertEqual(h.cli("task", "wait", tsk_id, "--until", "state:failed", "--timeout", "8s", user=True)[0], 0)

            # Pause project so reassignment stays in `assigned` cleanly
            self.assertEqual(h.cli("project", "pause", prj_id, user=True)[0], 0)
            self.assertEqual(h.cli("project", "resume", prj_id, user=True)[0], 0)
            self.assertEqual(h.cli("project", "pause", prj_id, user=True)[0], 0)

            # 1. Lead requests reassignment to w2 (both retry_over_limit and permission_escalation apply)
            rc_r1, resp_r1 = h.cli("task", "reassign", tsk_id, "--to", "w2", "--reason", "switch", token=tok_lead)
            self.assertEqual(rc_r1, c.EXIT_APPROVAL_REQUIRED, resp_r1)
            apr_1 = resp_r1["error"]["details"]["approval_id"]
            hash_1 = resp_r1["error"]["details"]["op_hash"]

            # Relayed decision must be rejected because permission_escalation is direct_only!
            rc_rel, resp_rel = h.cli(
                "approval", "approve", apr_1, "--op-hash", hash_1,
                "--relayed", "--user-statement", "James: ok", token=tok_grok,
            )
            self.assertEqual(rc_rel, c.EXIT_APPROVAL_INVALID, resp_rel)
            self.assertEqual(resp_rel["error"]["code"], c.CHANNEL_NOT_ALLOWED)

            # Direct user approval + lead re-run with --approval succeeds without OP_HASH_MISMATCH!
            self.assertEqual(h.cli("approval", "approve", apr_1, "--op-hash", hash_1, user=True)[0], 0)
            rc_r2, resp_r2 = h.cli(
                "task", "reassign", tsk_id, "--to", "w2", "--reason", "switch", "--approval", apr_1, token=tok_lead
            )
            self.assertEqual(rc_r2, 0, resp_r2)
            self.assertEqual(resp_r2["data"]["status"], "assigned")
            self.assertEqual(resp_r2["data"]["assignee"], "w2")
            self.assertEqual(resp_r2["data"]["profile"], "elevated")

            # Resume so w2 runs attempt 2/1 and fails again, then test compound `task retry`
            self.assertEqual(h.cli("project", "resume", prj_id, user=True)[0], 0)
            self.assertEqual(
                h.wait_for(lambda: h.cli("task", "show", tsk_id, user=True)[1]["data"]["attempt"] == 2
                           and h.cli("task", "show", tsk_id, user=True)[1]["data"]["status"] == "failed", timeout=15.0),
                True,
            )
            rc_ret1, resp_ret1 = h.cli("task", "retry", tsk_id, "--reason", "try3", token=tok_lead)
            self.assertEqual(rc_ret1, c.EXIT_APPROVAL_REQUIRED, resp_ret1)
            apr_2 = resp_ret1["error"]["details"]["approval_id"]
            hash_2 = resp_ret1["error"]["details"]["op_hash"]

            rc_rel2, resp_rel2 = h.cli(
                "approval", "approve", apr_2, "--op-hash", hash_2,
                "--relayed", "--user-statement", "James: ok", token=tok_grok,
            )
            self.assertEqual(rc_rel2, c.EXIT_APPROVAL_INVALID, resp_rel2)
            self.assertEqual(resp_rel2["error"]["code"], c.CHANNEL_NOT_ALLOWED)

            self.assertEqual(h.cli("approval", "approve", apr_2, "--op-hash", hash_2, user=True)[0], 0)
            rc_ret2, resp_ret2 = h.cli("task", "retry", tsk_id, "--reason", "try3", "--approval", apr_2, token=tok_lead)
            self.assertEqual(rc_ret2, 0, resp_ret2)
            self.assertEqual(h.cli("task", "wait", tsk_id, "--until", "state:failed", "--timeout", "8s", user=True)[0], 0)
        finally:
            h.teardown()

    def test_f4_retry_permission_escalation_and_disabled_agent_guards(self) -> None:
        """[P1] `task retry` enforces `permission_escalation` below max_attempts, and
        `task retry`, `task reject`, and `task start` reject disabled assignees."""
        repo_dir = tempfile.mkdtemp(prefix="mh-b7-repo-")
        self.addCleanup(shutil.rmtree, repo_dir, True)
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            # Initialize tiny git repo for code-task start check
            import subprocess
            subprocess.run(["git", "init", "-b", "main", repo_dir], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", repo_dir, "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Test"], check=True)
            with open(os.path.join(repo_dir, "README.md"), "w", encoding="utf-8") as f:
                f.write("hi\n")
            subprocess.run(["git", "-C", repo_dir, "add", "."], check=True)
            subprocess.run(["git", "-C", repo_dir, "commit", "-m", "init"], check=True, stdout=subprocess.DEVNULL)

            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            create_agent(h, "fx_esc", adapter="fake", delivery="launch", default_profile="edit", max_unapproved_profile="dev")
            tok_ibx = create_agent(h, "ibx_w", adapter="none", delivery="inbox")
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            rc_p, resp_p = h.cli(
                "intake", "submit", "--title", "P", "--goal", "G", "--criterion", "c1",
                "--repo", repo_dir, "--base-branch", "main", "--test-command", "none", token=tok_intake,
            )
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            # (1) Elevated task fails at attempt 1 < max_attempts (3): lead retry requires permission_escalation
            rc_t, resp_t = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Elevated retry",
                "--spec", 'FAKE {"fail": {"code": 1}}',
                "--kind", "general",
                "--assignee", "fx_esc",
                "--profile", "elevated",
                "--max-attempts", "3",
                user=True,
            )
            self.assertEqual(rc_t, 0, resp_t)
            tsk_esc = resp_t["data"]["id"]
            self.assertEqual(h.cli("task", "wait", tsk_esc, "--until", "state:failed", "--timeout", "8s", user=True)[0], 0)

            rc_ret, resp_ret = h.cli("task", "retry", tsk_esc, token=tok_lead)
            self.assertEqual(rc_ret, c.EXIT_APPROVAL_REQUIRED, resp_ret)
            self.assertEqual(resp_ret["error"]["details"]["category"], "permission_escalation")
            apr_id = resp_ret["error"]["details"]["approval_id"]
            op_hash = resp_ret["error"]["details"]["op_hash"]
            self.assertEqual(h.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)[0], 0)

            # (2a) Disable fx_esc: task retry fails with rc 2 INVALID_ARGUMENT (reason: disabled)
            self.assertEqual(h.cli("agent", "disable", "fx_esc", user=True)[0], 0)
            rc_ret_dis, resp_ret_dis = h.cli("task", "retry", tsk_esc, "--approval", apr_id, token=tok_lead)
            self.assertEqual(rc_ret_dis, c.EXIT_USAGE, resp_ret_dis)
            self.assertEqual(resp_ret_dis["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(resp_ret_dis["error"]["details"]["reason"], "disabled")

            # (2b) Submitted task + queued code task on ibx_w, then disable ibx_w
            rc_sub_t, resp_sub_t = h.cli(
                "task", "create", "--project", prj_id, "--title", "Sub task", "--spec", "S",
                "--kind", "general", "--assignee", "ibx_w", token=tok_lead,
            )
            self.assertEqual(rc_sub_t, 0, resp_sub_t)
            tsk_sub = resp_sub_t["data"]["id"]
            self.assertEqual(h.cli("task", "wait", tsk_sub, "--until", "state:queued", "--timeout", "10s", user=True)[0], 0)
            st = h.cli("task", "start", tsk_sub, token=tok_ibx)[1]
            self.assertEqual(
                h.cli("task", "submit", tsk_sub, "--run", st["data"]["run_id"], "--fence", str(st["data"]["fence"]),
                      "--summary", "done", token=tok_ibx)[0],
                0,
            )

            rc_code_t, resp_code_t = h.cli(
                "task", "create", "--project", prj_id, "--title", "Code start task", "--spec", "S",
                "--kind", "code", "--assignee", "ibx_w", token=tok_lead,
            )
            self.assertEqual(rc_code_t, 0, resp_code_t)
            tsk_code = resp_code_t["data"]["id"]
            self.assertEqual(h.cli("task", "wait", tsk_code, "--until", "state:queued", "--timeout", "8s", user=True)[0], 0)
            ws_expected = resp_code_t["data"].get("workspace_path") or os.path.join(h.home, "workspaces", prj_id, "tasks", tsk_code)

            self.assertEqual(h.cli("agent", "disable", "ibx_w", user=True)[0], 0)

            rc_rej, resp_rej = h.cli("task", "reject", tsk_sub, "--reason", "rework", token=tok_lead)
            self.assertEqual(rc_rej, c.EXIT_USAGE, resp_rej)
            self.assertEqual(resp_rej["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(resp_rej["error"]["details"]["reason"], "disabled")

            rc_st_dis, resp_st_dis = h.cli("task", "start", tsk_code, user=True)
            self.assertEqual(rc_st_dis, c.EXIT_USAGE, resp_st_dis)
            self.assertEqual(resp_st_dis["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(resp_st_dis["error"]["details"]["reason"], "disabled")
            self.assertFalse(os.path.exists(ws_expected))
        finally:
            h.teardown()

    def test_f5_accept_and_reject_do_not_pollute_scheduler_errors(self) -> None:
        """[P2] `task accept` and `task reject` (including `--dry-run`) do not call
        `promote_tasks` inside `BEGIN IMMEDIATE` or increment `sweep_errors`."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            tok_ibx = create_agent(h, "ibx", adapter="none", delivery="inbox")
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            prj_id = h.cli("intake", "submit", "--title", "P", "--goal", "G", "--criterion", "c1", token=tok_intake)[1]["data"]["id"]

            t1 = h.cli("task", "create", "--project", prj_id, "--title", "T1", "--spec", "S1", "--assignee", "ibx", token=tok_lead)[1]["data"]["id"]
            t2 = h.cli("task", "create", "--project", prj_id, "--title", "T2", "--spec", "S2", "--assignee", "ibx", "--depends-on", t1, token=tok_lead)[1]["data"]["id"]

            self.assertEqual(h.cli("task", "wait", t1, "--until", "state:queued", "--timeout", "10s", user=True)[0], 0)
            st1 = h.cli("task", "start", t1, token=tok_ibx)[1]["data"]
            self.assertEqual(h.cli("task", "submit", t1, "--run", st1["run_id"], "--fence", str(st1["fence"]), "--summary", "s1", token=tok_ibx)[0], 0)

            # 1. task reject --dry-run
            rc_dr, resp_dr = h.cli("task", "reject", t1, "--reason", "dry fix", "--dry-run", token=tok_lead)
            self.assertEqual(rc_dr, 0, resp_dr)
            self.assertEqual(h.cli("status")[1]["data"]["scheduler"]["sweep_errors"], 0)

            # 2. real task reject -> scheduler promotes rework -> queued cleanly
            rc_rj, resp_rj = h.cli("task", "reject", t1, "--reason", "real fix", token=tok_lead)
            self.assertEqual(rc_rj, 0, resp_rj)
            self.assertEqual(h.cli("task", "wait", t1, "--until", "state:queued", "--timeout", "8s", user=True)[0], 0)
            self.assertEqual(h.cli("status")[1]["data"]["scheduler"]["sweep_errors"], 0)

            # Re-submit t1 and accept it -> scheduler promotes dependent t2 cleanly
            st2 = h.cli("task", "start", t1, token=tok_ibx)[1]["data"]
            self.assertEqual(h.cli("task", "submit", t1, "--run", st2["run_id"], "--fence", str(st2["fence"]), "--summary", "s2", token=tok_ibx)[0], 0)

            rc_ac_dr, _ = h.cli("task", "accept", t1, "--dry-run", token=tok_lead)
            self.assertEqual(rc_ac_dr, 0)
            self.assertEqual(h.cli("status")[1]["data"]["scheduler"]["sweep_errors"], 0)

            rc_ac, resp_ac = h.cli("task", "accept", t1, token=tok_lead)
            self.assertEqual(rc_ac, 0, resp_ac)
            self.assertEqual(h.cli("task", "wait", t2, "--until", "state:queued", "--timeout", "8s", user=True)[0], 0)
            self.assertEqual(h.cli("status")[1]["data"]["scheduler"]["sweep_errors"], 0)
        finally:
            h.teardown()

    def test_f6_custom_binary_off_path_and_requeue_install_check(self) -> None:
        """[P2] `--binary` and `--option binary=...` off `PATH` pass AG installation check,
        and `_requeue_launch_guard` rejects uninstalled binaries in `warn` mode."""
        off_path_dir = tempfile.mkdtemp(prefix="mh-b7-offpath-")
        self.addCleanup(shutil.rmtree, off_path_dir, True)
        create_test_stubs(off_path_dir)
        droid_bin = os.path.join(off_path_dir, "droid")

        # Empty PATH dir so default `droid` is NOT on PATH
        empty_stubs = tempfile.mkdtemp(prefix="mh-b7-empty-")
        self.addCleanup(shutil.rmtree, empty_stubs, True)

        h = MeshHarness(init_db=True, start_daemon=False, extra_path=empty_stubs)
        try:
            # Remove tests/shims from daemon PATH if needed so `droid` is strictly absent from PATH
            env = h.get_env()
            clean_parts = [
                p for p in env.get("PATH", "").split(os.pathsep)
                if p and not os.path.exists(os.path.join(p, "droid"))
            ]
            h.start_daemon(extra_env={"PATH": os.pathsep.join(clean_parts)})

            self.assertEqual(h.cli("config", "set", "adapter.evidence_mode", '"warn"', user=True)[0], 0)
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            prj_id = h.cli("intake", "submit", "--title", "P", "--goal", "G", "--criterion", "c1", token=tok_intake)[1]["data"]["id"]
            self.assertEqual(h.cli("project", "pause", prj_id, user=True)[0], 0)

            # 1. Create launch agents with --binary and --option binary=... when `droid` is absent from PATH
            rc_b1, resp_b1 = h.cli(
                "agent", "create", "--name", "ag_bin", "--adapter", "droid",
                "--binary", droid_bin, "--allowed-model", "claude-sonnet-4.5", user=True,
            )
            self.assertEqual(rc_b1, 0, resp_b1)

            rc_b2, resp_b2 = h.cli(
                "agent", "create", "--name", "ag_opt", "--adapter", "droid",
                "--option", f"binary={droid_bin}", "--allowed-model", "claude-sonnet-4.5", user=True,
            )
            self.assertEqual(rc_b2, 0, resp_b2)

            conn = h.db()
            try:
                bp1 = conn.execute("SELECT binary_path FROM agents WHERE id = ?;", (resp_b1["data"]["id"],)).fetchone()[0]
                bp2 = conn.execute("SELECT binary_path FROM agents WHERE id = ?;", (resp_b2["data"]["id"],)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(bp1, droid_bin)
            self.assertEqual(bp2, droid_bin)

            # Both agents pass AG step 2 (`adapter.is_installed(binary_path=...)`)
            rc_t1, resp_t1 = h.cli(
                "task", "create", "--project", prj_id, "--title", "T1", "--spec", "S1", "--assignee", "ag_bin", token=tok_lead
            )
            self.assertEqual(rc_t1, 0, resp_t1)
            rc_t2, resp_t2 = h.cli(
                "task", "create", "--project", prj_id, "--title", "T2", "--spec", "S2", "--assignee", "ag_opt", token=tok_lead
            )
            self.assertEqual(rc_t2, 0, resp_t2)

            # 2. Have an inbox worker submit/fail two tasks, reassign them to ag_bin? Wait:
            # retry is from failed, reject is from submitted! Let's run/fail and run/submit on inbox worker?
            # Even better: seed failed/submitted on ag_bin and restore before teardown (like test_adapter_evidence.py),
            # OR remove droid_bin and verify both retry and reject fail with ADAPTER_NOT_INSTALLED!
            t_ret = resp_t1["data"]["id"]
            t_rej = resp_t2["data"]["id"]
            w = sqlite3.connect(os.path.join(h.home, "mesh.db"))
            try:
                w.execute("UPDATE tasks SET status = 'failed', attempt = 1 WHERE id = ?;", (t_ret,))
                w.execute("UPDATE tasks SET status = 'submitted', attempt = 1 WHERE id = ?;", (t_rej,))
                w.commit()
            finally:
                w.close()

            os.remove(droid_bin)
            self.assertEqual(h.cli("project", "resume", prj_id, user=True)[0], 0)

            rc_retry, resp_retry = h.cli("task", "retry", t_ret, token=tok_lead)
            self.assertEqual(rc_retry, c.EXIT_ADAPTER_UNAVAILABLE, resp_retry)
            self.assertEqual(resp_retry["error"]["code"], c.ADAPTER_NOT_INSTALLED)

            rc_reject, resp_reject = h.cli("task", "reject", t_rej, "--reason", "redo", token=tok_lead)
            self.assertEqual(rc_reject, c.EXIT_ADAPTER_UNAVAILABLE, resp_reject)
            self.assertEqual(resp_reject["error"]["code"], c.ADAPTER_NOT_INSTALLED)

            # Restore seeded task status to 'assigned' so teardown I4 invariant check passes cleanly
            w = sqlite3.connect(os.path.join(h.home, "mesh.db"))
            try:
                w.execute("UPDATE tasks SET status = 'assigned', attempt = 0 WHERE id IN (?, ?);", (t_ret, t_rej))
                w.commit()
            finally:
                w.close()
        finally:
            h.teardown()

    def test_f7_reassign_cross_adapter_profile_override_and_default_fallback(self) -> None:
        """[P2] `task reassign` accepts `--profile`, `--model`, `--effort` and falls back
        to target `default_profile` when moving a `dev` task to `kiro` or `agy`."""
        stubs_dir = tempfile.mkdtemp(prefix="mh-b7-stubs7-")
        self.addCleanup(shutil.rmtree, stubs_dir, True)
        create_test_stubs(stubs_dir)

        h = MeshHarness(init_db=True, start_daemon=True, extra_path=stubs_dir)
        try:
            self.assertEqual(h.cli("config", "set", "adapter.evidence_mode", '"warn"', user=True)[0], 0)
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            prj_id = h.cli("intake", "submit", "--title", "P", "--goal", "G", "--criterion", "c1", token=tok_intake)[1]["data"]["id"]
            self.assertEqual(h.cli("project", "pause", prj_id, user=True)[0], 0)

            self.assertEqual(h.cli(
                "agent", "create", "--name", "ag_droid", "--adapter", "droid",
                "--default-profile", "dev", "--allowed-model", "claude-sonnet-4.5", user=True,
            )[0], 0)
            self.assertEqual(h.cli(
                "agent", "create", "--name", "ag_kiro", "--adapter", "kiro",
                "--default-profile", "edit", "--allowed-model", "claude-sonnet-4.5",
                "--allowed-model", "claude-haiku-4.5", "--allowed-effort", "low", "--allowed-effort", "high", user=True,
            )[0], 0)
            self.assertEqual(h.cli(
                "agent", "create", "--name", "ag_agy", "--adapter", "agy",
                "--default-profile", "readonly", "--allowed-model", "gemini-2.5-pro", user=True,
            )[0], 0)

            rc_t, resp_t = h.cli(
                "task", "create", "--project", prj_id, "--title", "Dev task", "--spec", "S",
                "--assignee", "ag_droid", "--profile", "dev", token=tok_lead,
            )
            self.assertEqual(rc_t, 0, resp_t)
            tsk_id = resp_t["data"]["id"]
            self.assertEqual(resp_t["data"]["profile"], "dev")

            # 1. Reassign dev task to kiro WITHOUT --profile -> falls back to ag_kiro's default_profile ('edit')
            rc_r1, resp_r1 = h.cli("task", "reassign", tsk_id, "--to", "ag_kiro", "--reason", "switch to kiro", token=tok_lead)
            self.assertEqual(rc_r1, 0, resp_r1)
            self.assertEqual(resp_r1["data"]["profile"], "edit")
            self.assertEqual(resp_r1["data"]["model"], "claude-sonnet-4.5")

            # 2. Explicit unsupported --profile dev on kiro still fails with rc 13 PROFILE_UNSUPPORTED
            rc_bad, resp_bad = h.cli(
                "task", "reassign", tsk_id, "--to", "ag_kiro", "--profile", "dev", "--reason", "bad", token=tok_lead
            )
            self.assertEqual(rc_bad, c.EXIT_ADAPTER_UNAVAILABLE, resp_bad)
            self.assertEqual(resp_bad["error"]["code"], c.PROFILE_UNSUPPORTED)

            # 3. Explicit --profile, --model, --effort overrides on task reassign
            rc_r2, resp_r2 = h.cli(
                "task", "reassign", tsk_id,
                "--to", "ag_kiro",
                "--profile", "readonly",
                "--model", "claude-haiku-4.5",
                "--effort", "high",
                "--reason", "tune flags",
                token=tok_lead,
            )
            self.assertEqual(rc_r2, 0, resp_r2)
            self.assertEqual(resp_r2["data"]["profile"], "readonly")
            self.assertEqual(resp_r2["data"]["model"], "claude-haiku-4.5")
            self.assertEqual(resp_r2["data"]["effort"], "high")

            # 4. Reassign from droid dev task to agy without --profile -> falls back to ag_agy's default ('readonly')
            self.assertEqual(
                h.cli("task", "reassign", tsk_id, "--to", "ag_droid", "--profile", "dev", "--reason", "back", token=tok_lead)[0],
                0,
            )
            rc_r3, resp_r3 = h.cli("task", "reassign", tsk_id, "--to", "ag_agy", "--reason", "switch to agy", token=tok_lead)
            self.assertEqual(rc_r3, 0, resp_r3)
            self.assertEqual(resp_r3["data"]["profile"], "readonly")
            self.assertEqual(resp_r3["data"]["model"], "gemini-2.5-pro")
        finally:
            h.teardown()

    def test_f8_normalized_op_hash_across_channels_and_validation_before_approval(self) -> None:
        """[P2] Approval `op_hash` uses normalized canonical params + `target_id=task_id` across
        `mesh-actions` and CLI, and `retry`/`reassign` validate inputs before creating approvals."""
        from mesh.client import MeshClient

        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            create_agent(h, "w1", adapter="fake", delivery="launch", default_profile="edit", max_unapproved_profile="edit")
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            prj_id = h.cli("intake", "submit", "--title", "P", "--goal", "G", "--criterion", "c1", token=tok_intake)[1]["data"]["id"]
            self.assertEqual(h.cli("project", "pause", prj_id, user=True)[0], 0)

            # Part 1: Request approval with mesh-actions-shaped args (`task` + `agent` aliases, no None keys),
            # then consume via CLI (`--agent w1` + extra None keys in payload).
            from mesh.core.lead_actions import action_request_args

            tsk_1 = h.cli("task", "create", "--project", prj_id, "--title", "Unassigned", "--spec", "S", token=tok_lead)[1]["data"]["id"]
            client = MeshClient(home=h.home)
            raw_resp = client.send_request({
                "v": 1,
                "cmd": "task.assign",
                "auth": {"type": "token", "token": tok_lead},
                "args": action_request_args("task.assign", {"task": tsk_1, "agent": "w1", "profile": "elevated"}, prj_id),
            })
            self.assertFalse(raw_resp["ok"], raw_resp)
            self.assertEqual(raw_resp["exit_code"], c.EXIT_APPROVAL_REQUIRED)
            apr_id = raw_resp["error"]["details"]["approval_id"]
            op_hash = raw_resp["error"]["details"]["op_hash"]

            # Verify target and task_id are populated in op_json / approvals table
            conn = h.db()
            try:
                arow = conn.execute("SELECT task_id, op_json FROM approvals WHERE id = ?;", (apr_id,)).fetchone()
            finally:
                conn.close()
            self.assertEqual(arow["task_id"], tsk_1)
            op_doc = json.loads(arow["op_json"])
            self.assertEqual(op_doc["target"], tsk_1)

            self.assertEqual(h.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)[0], 0)
            rc_cli, resp_cli = h.cli(
                "task", "assign", tsk_1, "--agent", "w1", "--profile", "elevated", "--approval", apr_id, token=tok_lead
            )
            self.assertEqual(rc_cli, 0, resp_cli)
            self.assertEqual(resp_cli["data"]["assignee"], "w1")

            # Part 2: At attempt cap (1/1), typos in --model or --to fail BEFORE stranding approvals
            self.assertEqual(h.cli("project", "resume", prj_id, user=True)[0], 0)
            tsk_fail = h.cli(
                "task", "create", "--project", prj_id, "--title", "Fail1", "--spec", 'FAKE {"fail": {"code": 1}}',
                "--assignee", "w1", "--max-attempts", "1", token=tok_lead,
            )[1]["data"]["id"]
            self.assertEqual(h.cli("task", "wait", tsk_fail, "--until", "state:failed", "--timeout", "8s", user=True)[0], 0)

            rc_bad_m, resp_bad_m = h.cli("task", "retry", tsk_fail, "--model", "m-typo", token=tok_lead)
            self.assertEqual(rc_bad_m, c.EXIT_USAGE, resp_bad_m)
            self.assertEqual(resp_bad_m["error"]["code"], c.MODEL_NOT_ALLOWED)

            rc_bad_to, resp_bad_to = h.cli("task", "reassign", tsk_fail, "--to", "no-such-worker", "--reason", "r", token=tok_lead)
            self.assertEqual(rc_bad_to, c.EXIT_NOT_FOUND, resp_bad_to)
            self.assertEqual(resp_bad_to["error"]["code"], c.NOT_FOUND)

            conn = h.db()
            try:
                stranded = conn.execute("SELECT COUNT(*) FROM approvals WHERE task_id = ?;", (tsk_fail,)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(stranded, 0)
        finally:
            h.teardown()

    def test_f9_agy_effort_suffix_kiro_prompt_too_long_and_agent_update_validators(self) -> None:
        """[P2] `agy` `model_encodes_effort` and `max` effort, `kiro` `readonly` `PROMPT_TOO_LONG`
        at create/assign/reassign/retry, and `agent update` model/effort validators."""
        stubs_dir = tempfile.mkdtemp(prefix="mh-b7-stubs9-")
        self.addCleanup(shutil.rmtree, stubs_dir, True)
        create_test_stubs(stubs_dir)

        h = MeshHarness(init_db=True, start_daemon=True, extra_path=stubs_dir)
        try:
            self.assertEqual(h.cli("config", "set", "adapter.evidence_mode", '"warn"', user=True)[0], 0)
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            prj_id = h.cli("intake", "submit", "--title", "P", "--goal", "G", "--criterion", "c1", token=tok_intake)[1]["data"]["id"]
            self.assertEqual(h.cli("project", "pause", prj_id, user=True)[0], 0)

            # --- (1) agy model_encodes_effort and 'max' effort ---
            self.assertEqual(h.cli(
                "agent", "create", "--name", "ag_agy", "--adapter", "agy",
                "--allowed-model", "gemini-2.5-pro",
                "--allowed-model", "gemini-3.8-flash-high",
                "--default-model", "gemini-2.5-pro",
                "--allowed-effort", "high",
                "--allowed-effort", "max",
                "--default-effort", "high",
                user=True,
            )[0], 0)

            rc_agy_bad, resp_agy_bad = h.cli(
                "task", "create", "--project", prj_id, "--title", "AGY conflict", "--spec", "S",
                "--assignee", "ag_agy", "--model", "gemini-3.8-flash-high", token=tok_lead,
            )
            self.assertEqual(rc_agy_bad, c.EXIT_USAGE, resp_agy_bad)
            self.assertEqual(resp_agy_bad["error"]["code"], c.EFFORT_NOT_ALLOWED)
            self.assertEqual(resp_agy_bad["error"]["details"]["reason"], "model_encodes_effort")

            # 'max' is recognized as an effort (rejected if swapped into --profile, accepted in --effort)
            rc_swap, resp_swap = h.cli(
                "task", "create", "--project", prj_id, "--title", "Swap max", "--spec", "S",
                "--profile", "max", token=tok_lead,
            )
            self.assertEqual(rc_swap, c.EXIT_USAGE, resp_swap)
            self.assertEqual(resp_swap["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(resp_swap["error"]["details"]["param"], "profile")

            rc_max_ok, resp_max_ok = h.cli(
                "task", "create", "--project", prj_id, "--title", "AGY max effort", "--spec", "S",
                "--assignee", "ag_agy", "--model", "gemini-2.5-pro", "--effort", "max", token=tok_lead,
            )
            self.assertEqual(rc_max_ok, 0, resp_max_ok)

            # Check unconditional model_encodes_effort on `task retry` (even without overrides when agent updated, or with --model)
            tsk_agy_ret = resp_max_ok["data"]["id"]
            w = sqlite3.connect(os.path.join(h.home, "mesh.db"))
            try:
                w.execute("UPDATE tasks SET status = 'failed', attempt = 1 WHERE id = ?;", (tsk_agy_ret,))
                w.commit()
            finally:
                w.close()
            rc_ret_agy, resp_ret_agy = h.cli("task", "retry", tsk_agy_ret, "--model", "gemini-3.8-flash-high", token=tok_lead)
            self.assertEqual(rc_ret_agy, c.EXIT_USAGE, resp_ret_agy)
            self.assertEqual(resp_ret_agy["error"]["code"], c.EFFORT_NOT_ALLOWED)
            self.assertEqual(resp_ret_agy["error"]["details"]["reason"], "model_encodes_effort")
            w = sqlite3.connect(os.path.join(h.home, "mesh.db"))
            try:
                w.execute("UPDATE tasks SET status = 'assigned', attempt = 0 WHERE id = ?;", (tsk_agy_ret,))
                w.commit()
            finally:
                w.close()

            # --- (2) kiro readonly PROMPT_TOO_LONG (>100 KB) at create / assign / reassign / retry ---
            self.assertEqual(h.cli(
                "agent", "create", "--name", "ag_kiro", "--adapter", "kiro",
                "--default-profile", "readonly", "--allowed-model", "claude-sonnet-4.5", user=True,
            )[0], 0)
            big_spec = "X" * 105_000

            # create --assignee with readonly -> PROMPT_TOO_LONG
            rc_k_cr, resp_k_cr = h.cli(
                "task", "create", "--project", prj_id, "--title", "Big Kiro", "--spec", "-",
                "--assignee", "ag_kiro", "--profile", "readonly", stdin=big_spec, token=tok_lead,
            )
            self.assertEqual(rc_k_cr, c.EXIT_USAGE, resp_k_cr)
            self.assertEqual(resp_k_cr["error"]["code"], c.PROMPT_TOO_LONG)
            self.assertEqual(resp_k_cr["error"]["details"]["adapter"], "kiro")
            self.assertEqual(resp_k_cr["error"]["details"]["profile"], "readonly")

            # create unassigned, then assign to kiro readonly -> PROMPT_TOO_LONG
            rc_u_big, resp_u_big = h.cli(
                "task", "create", "--project", prj_id, "--title", "Big Unassigned", "--spec", "-",
                stdin=big_spec, token=tok_lead,
            )
            self.assertEqual(rc_u_big, 0, resp_u_big)
            tsk_big = resp_u_big["data"]["id"]

            rc_k_as, resp_k_as = h.cli("task", "assign", tsk_big, "--to", "ag_kiro", "--profile", "readonly", token=tok_lead)
            self.assertEqual(rc_k_as, c.EXIT_USAGE, resp_k_as)
            self.assertEqual(resp_k_as["error"]["code"], c.PROMPT_TOO_LONG)

            # assign with --profile edit (file_pointer strategy) -> succeeds!
            rc_k_edit, resp_k_edit = h.cli("task", "assign", tsk_big, "--to", "ag_kiro", "--profile", "edit", token=tok_lead)
            self.assertEqual(rc_k_edit, 0, resp_k_edit)

            # reassign back to readonly -> PROMPT_TOO_LONG
            rc_k_re, resp_k_re = h.cli(
                "task", "reassign", tsk_big, "--to", "ag_kiro", "--profile", "readonly", "--reason", "ro", token=tok_lead
            )
            self.assertEqual(rc_k_re, c.EXIT_USAGE, resp_k_re)
            self.assertEqual(resp_k_re["error"]["code"], c.PROMPT_TOO_LONG)

            # retry when task has profile='readonly' and >100 KB spec -> PROMPT_TOO_LONG
            w = sqlite3.connect(os.path.join(h.home, "mesh.db"))
            try:
                w.execute("UPDATE tasks SET status = 'failed', attempt = 1, profile = 'readonly' WHERE id = ?;", (tsk_big,))
                w.commit()
            finally:
                w.close()
            rc_k_ret, resp_k_ret = h.cli("task", "retry", tsk_big, token=tok_lead)
            self.assertEqual(rc_k_ret, c.EXIT_USAGE, resp_k_ret)
            self.assertEqual(resp_k_ret["error"]["code"], c.PROMPT_TOO_LONG)
            w = sqlite3.connect(os.path.join(h.home, "mesh.db"))
            try:
                w.execute("UPDATE tasks SET status = 'assigned', attempt = 0, profile = 'edit' WHERE id = ?;", (tsk_big,))
                w.commit()
            finally:
                w.close()

            # --- (3) agent update model & effort validators ---
            self.assertEqual(h.cli(
                "agent", "create", "--name", "ag_vibe", "--adapter", "vibe",
                "--allowed-model", "devstral-small", user=True,
            )[0], 0)
            rc_vu1, resp_vu1 = h.cli(
                "agent", "update", "ag_vibe", "--allowed-effort", "high", "--default-effort", "high", user=True
            )
            self.assertEqual(rc_vu1, c.EXIT_USAGE, resp_vu1)
            self.assertEqual(resp_vu1["error"]["code"], c.EFFORT_UNSUPPORTED)

            rc_vu2, resp_vu2 = h.cli("agent", "update", "ag_agy", "--allowed-effort", "edit", user=True)
            self.assertEqual(rc_vu2, c.EXIT_USAGE, resp_vu2)
            self.assertEqual(resp_vu2["error"]["code"], c.EFFORT_NOT_ALLOWED)

            rc_vu3, resp_vu3 = h.cli("agent", "update", "ag_agy", "--default-profile", "max", user=True)
            self.assertEqual(rc_vu3, c.EXIT_USAGE, resp_vu3)
            self.assertEqual(resp_vu3["error"]["code"], c.INVALID_ARGUMENT)

            rc_vu4, resp_vu4 = h.cli("agent", "update", "ag_agy", "--default-model", "bad model", user=True)
            self.assertEqual(rc_vu4, c.EXIT_USAGE, resp_vu4)
            self.assertEqual(resp_vu4["error"]["code"], c.INVALID_ARGUMENT)

            self.assertEqual(h.cli(
                "agent", "create", "--name", "ag_ibx_kiro", "--adapter", "kiro", "--delivery", "inbox", user=True
            )[0], 0)
            rc_vu5, resp_vu5 = h.cli(
                "agent", "update", "ag_ibx_kiro", "--allowed-model", "m1", "--default-model", "m2", user=True
            )
            self.assertEqual(rc_vu5, c.EXIT_USAGE, resp_vu5)
            self.assertEqual(resp_vu5["error"]["code"], c.DEFAULT_MODEL_NOT_ALLOWED)
        finally:
            h.teardown()


if __name__ == "__main__":
    unittest.main()
