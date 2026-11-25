"""Integration test driving DesktopBridge against a real meshd daemon."""

from __future__ import annotations

import os
import sys
import time
import unittest
from typing import Any, Dict, List
from unittest import mock

import mesh.constants as c
from mesh.desktop.bridge import DesktopBridge
from mesh.platform import ipc as plat_ipc, locks as plat_locks
from tests.helpers import MeshHarness


class TestDesktopBridgeIntegration(unittest.TestCase):
    """End-to-end DesktopBridge integration test against a real meshd (MESH_TESTING=1)."""

    def setUp(self) -> None:
        # Runs `mesh init` in an isolated temp MESH_HOME with MESH_TESTING=1 env helpers
        self.harness = MeshHarness(init_db=True, start_daemon=False)

    def tearDown(self) -> None:
        self.harness.teardown()

    def test_bridge_end_to_end_real_meshd_lifecycle_and_event_tail(self) -> None:
        """init -> start_daemon -> set_passphrase -> intake.submit -> event.tail -> stop_daemon."""
        bridge = DesktopBridge(
            home=self.harness.home,
            probe_timeout_s=2.0,
            probe_interval_s=0.25,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        env_patch = self.harness.get_env()
        with mock.patch.dict(os.environ, env_patch, clear=False):
            # 1. Start real meshd via DesktopBridge.start_daemon() (MESH_TESTING=1)
            start_res = bridge.start_daemon()
            self.assertTrue(start_res["ok"], start_res)
            conn_snap = start_res["data"]["connection"]
            self.assertTrue(conn_snap["connected"], conn_snap)
            self.assertIsNotNone(conn_snap["daemon_status"])
            self.assertTrue(conn_snap["daemon_status"]["testing_mode"], conn_snap["daemon_status"])

            # Verify unauthenticated daemon.status & version probes
            st_res = bridge.daemon_status()
            self.assertTrue(st_res["ok"], st_res)
            self.assertTrue(st_res["data"]["daemon_version"])

            # 2. Authenticate user passphrase via whoami & start event.tail stream
            auth_res = bridge.set_passphrase(self.harness.passphrase, verify=True)
            self.assertTrue(auth_res["ok"], auth_res)
            self.assertTrue(auth_res["data"]["auth_verified"], auth_res)

            # Wait until real event.tail stream is active on meshd before submitting
            stream_deadline = time.monotonic() + (15.0 if sys.platform == "win32" else 5.0)
            stream_ready = False
            while time.monotonic() < stream_deadline:
                ds = bridge.call("daemon.status", {})
                if ds["ok"] and int((ds["data"] or {}).get("active_streams") or 0) >= 1:
                    stream_ready = True
                    break
                time.sleep(0.05)
            self.assertTrue(stream_ready, "event.tail stream did not register on meshd")

            # Drain any prior queued items before creating lead + project
            bridge.poll_events()

            # 3. Create an inbox Lead agent & submit a project via intake.submit
            lead_res = bridge.call(
                "agent.create",
                {
                    "name": "lead1",
                    "adapter": "none",
                    "delivery": "inbox",
                    "can_lead": True,
                    "max_concurrency": 1,
                },
            )
            self.assertTrue(lead_res["ok"], lead_res)

            submit_res = bridge.call(
                "intake.submit",
                {
                    "title": "Desktop Real Daemon E2E",
                    "goal": "Verify DesktopBridge IPC and event.tail against real meshd",
                    "criterion": ["project created", "events streamed"],
                    "lead": "lead1",
                },
            )
            self.assertTrue(submit_res["ok"], submit_res)
            project_id = submit_res["data"]["id"]
            self.assertTrue(project_id.startswith("prj_"), submit_res)

            # Follow-up intake.submit (Form 2) emits project.request_received
            followup_res = bridge.call(
                "intake.submit",
                {
                    "project": project_id,
                    "message": "Follow-up request over DesktopBridge",
                },
            )
            self.assertTrue(followup_res["ok"], followup_res)
            self.assertTrue(followup_res["data"].get("message_recorded"), followup_res)

            # 4. Verify real event.tail delivers project.created & project.request_received
            collected: List[Dict[str, Any]] = []
            tail_deadline = time.monotonic() + (15.0 if sys.platform == "win32" else 5.0)
            saw_created = False
            saw_request = False
            while time.monotonic() < tail_deadline:
                polled = bridge.poll_events(limit=200)
                self.assertTrue(polled["ok"], polled)
                collected.extend(polled["data"]["items"])
                proj_types = {
                    ev.get("type")
                    for ev in collected
                    if ev.get("project_id") == project_id
                }
                saw_created = "project.created" in proj_types
                saw_request = "project.request_received" in proj_types
                if saw_created and saw_request:
                    break
                time.sleep(0.05)

            self.assertTrue(saw_created and saw_request, f"Missing event.tail events for {project_id}: {collected}")

            proj_events = [ev for ev in collected if ev.get("project_id") == project_id]
            self.assertTrue(all(ev.get("via") == "direct" for ev in proj_events), proj_events)

            # Verify event.list matches and IPC doctor_report succeeds cleanly
            ev_list_res = bridge.call("event.list", {"project": project_id, "limit": 100})
            self.assertTrue(ev_list_res["ok"], ev_list_res)
            listed_types = {ev.get("type") for ev in ev_list_res["data"]["items"]}
            self.assertIn("project.created", listed_types)
            self.assertIn("project.request_received", listed_types)

            doc_res = bridge.doctor_report()
            self.assertTrue(doc_res["ok"], doc_res)
            self.assertEqual(doc_res["data"]["source"], "ipc")
            self.assertEqual(doc_res["data"]["status"], "healthy")

            # 5. Stop real meshd via DesktopBridge.stop_daemon()
            stop_res = bridge.stop_daemon()
            self.assertTrue(stop_res["ok"], stop_res)
            self.assertFalse(bridge.connection_state()["data"]["connected"])

            # Wait until daemon socket/pipe and lock are released
            lock_path = os.path.join(self.harness.home, "run", "meshd.lock")
            stop_deadline = time.monotonic() + (15.0 if sys.platform == "win32" else 5.0)
            while time.monotonic() < stop_deadline:
                endpoint_up = plat_ipc.probe_daemon_endpoint(self.harness.home, timeout=0.2)
                lock_held = plat_locks.is_exclusive_lock_held(lock_path)
                if not endpoint_up and not lock_held:
                    break
                time.sleep(0.05)
            self.assertFalse(plat_ipc.probe_daemon_endpoint(self.harness.home, timeout=0.2))
            self.assertFalse(plat_locks.is_exclusive_lock_held(lock_path))

            # Subsequent probe confirms DAEMON_UNAVAILABLE
            offline_res = bridge.call("project.list", {})
            self.assertFalse(offline_res["ok"])
            self.assertEqual(offline_res["error"]["code"], c.DAEMON_UNAVAILABLE)

    def test_supervisor_running_auto_starts_and_does_not_restart_after_stop_daemon(self) -> None:
        """F1 + F3 + F9: with supervisor running, real meshd auto-starts and stays stopped after stop_daemon()."""
        bridge = DesktopBridge(
            home=self.harness.home,
            backoff_schedule=(0.05, 0.1, 0.15),
            probe_timeout_s=2.0,
            probe_interval_s=0.1,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        env_patch = self.harness.get_env()
        with mock.patch.dict(os.environ, env_patch, clear=False):
            # 1. Start supervisor; it must auto-start real meshd
            bridge.start()
            connect_deadline = time.monotonic() + (25.0 if sys.platform == "win32" else 15.0)
            while time.monotonic() < connect_deadline:
                st = bridge.connection_state()["data"]
                if st["connected"] and st["daemon_status"] is not None:
                    break
                time.sleep(0.05)
            st_up = bridge.connection_state()["data"]
            self.assertTrue(st_up["connected"], f"Supervisor failed to auto-start real meshd: {st_up}")
            pid1 = st_up["daemon_status"]["pid"]
            self.assertTrue(pid1)

            # 2. Authenticate and explicitly stop real meshd while supervisor is actively probing
            auth_res = bridge.set_passphrase(self.harness.passphrase, verify=True)
            self.assertTrue(auth_res["ok"], auth_res)

            stop_res = bridge.stop_daemon()
            self.assertTrue(stop_res["ok"], stop_res)

            # 3. Wait until real meshd exits, lock is freed, and supervisor clears daemon_stopping
            lock_path = os.path.join(self.harness.home, "run", "meshd.lock")
            stop_deadline = time.monotonic() + (20.0 if sys.platform == "win32" else 10.0)
            while time.monotonic() < stop_deadline:
                st_now = bridge.connection_state()["data"]
                if (
                    not st_now["connected"]
                    and st_now["user_stopped"] is True
                    and st_now["daemon_stopping"] is False
                    and st_now["reconnect_attempt"] >= 3
                    and not plat_locks.is_exclusive_lock_held(lock_path)
                ):
                    break
                time.sleep(0.05)

            st_after = bridge.connection_state()["data"]
            self.assertFalse(st_after["connected"], f"Supervisor restarted meshd after user stop: {st_after}")
            self.assertTrue(st_after["user_stopped"], st_after)
            self.assertFalse(st_after["daemon_stopping"], st_after)
            self.assertFalse(plat_ipc.probe_daemon_endpoint(self.harness.home, timeout=0.2))
            self.assertFalse(plat_locks.is_exclusive_lock_held(lock_path))

    def test_n1_external_cli_stop_and_two_windows_not_undone_by_supervisor(self) -> None:
        """N1: external CLI `mesh daemon stop` and stopping from window A with window B open
        both persist run/autostart.hold so no supervisor restarts real meshd."""
        bridge_a = DesktopBridge(
            home=self.harness.home,
            backoff_schedule=(0.05, 0.1, 0.15),
            probe_timeout_s=2.0,
            probe_interval_s=0.1,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        bridge_b = DesktopBridge(
            home=self.harness.home,
            backoff_schedule=(0.05, 0.1, 0.15),
            probe_timeout_s=2.0,
            probe_interval_s=0.1,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge_a.close)
        self.addCleanup(bridge_b.close)

        env_patch = self.harness.get_env()
        lock_path = os.path.join(self.harness.home, "run", "meshd.lock")
        with mock.patch.dict(os.environ, env_patch, clear=False):
            bridge_a.start()
            bridge_b.start()
            connect_deadline = time.monotonic() + (25.0 if sys.platform == "win32" else 15.0)
            while time.monotonic() < connect_deadline:
                sa = bridge_a.connection_state()["data"]
                sb = bridge_b.connection_state()["data"]
                if sa["connected"] and sb["connected"] and sa["daemon_status"] and sb["daemon_status"]:
                    break
                time.sleep(0.05)
            sa0 = bridge_a.connection_state()["data"]
            sb0 = bridge_b.connection_state()["data"]
            self.assertTrue(sa0["connected"] and sb0["connected"], (sa0, sb0))
            pid1 = sa0["daemon_status"]["pid"]

            # 1. Stop via external CLI `mesh daemon stop` while both windows are open
            rc_stop, env_stop = self.harness.cli("daemon", "stop", user=True, timeout=30.0)
            self.assertEqual(rc_stop, 0, env_stop)

            deadline = time.monotonic() + (20.0 if sys.platform == "win32" else 10.0)
            while time.monotonic() < deadline:
                sa = bridge_a.connection_state()["data"]
                sb = bridge_b.connection_state()["data"]
                if (
                    not sa["connected"]
                    and not sb["connected"]
                    and sa["user_stopped"] is True
                    and sb["user_stopped"] is True
                    and not sa["daemon_stopping"]
                    and not sb["daemon_stopping"]
                    and sa["reconnect_attempt"] >= 3
                    and sb["reconnect_attempt"] >= 3
                    and not plat_locks.is_exclusive_lock_held(lock_path)
                ):
                    break
                time.sleep(0.05)
            sa1 = bridge_a.connection_state()["data"]
            sb1 = bridge_b.connection_state()["data"]
            self.assertFalse(sa1["connected"], f"CLI stop was undone (A): {sa1}")
            self.assertFalse(sb1["connected"], f"CLI stop was undone (B): {sb1}")
            self.assertTrue(sa1["user_stopped"] and sb1["user_stopped"])
            self.assertFalse(plat_ipc.probe_daemon_endpoint(self.harness.home, timeout=0.2))

            # 2. Start externally via CLI -> both windows reconnect to new pid2
            rc_start, env_start = self.harness.cli("daemon", "start", timeout=30.0)
            self.assertEqual(rc_start, 0, env_start)
            reconn_deadline = time.monotonic() + (25.0 if sys.platform == "win32" else 15.0)
            while time.monotonic() < reconn_deadline:
                sa = bridge_a.connection_state()["data"]
                sb = bridge_b.connection_state()["data"]
                if sa["connected"] and sb["connected"] and not sa["user_stopped"] and not sb["user_stopped"]:
                    break
                time.sleep(0.05)
            sa2 = bridge_a.connection_state()["data"]
            sb2 = bridge_b.connection_state()["data"]
            self.assertTrue(sa2["connected"] and sb2["connected"], (sa2, sb2))
            self.assertNotEqual(sa2["daemon_status"]["pid"], pid1)

            # 3. Stop via Window A -> Window B must NOT restart meshd
            self.assertTrue(bridge_a.set_passphrase(self.harness.passphrase, verify=True)["ok"])
            stop_a = bridge_a.stop_daemon()
            self.assertTrue(stop_a["ok"], stop_a)

            deadline2 = time.monotonic() + (20.0 if sys.platform == "win32" else 10.0)
            while time.monotonic() < deadline2:
                sa = bridge_a.connection_state()["data"]
                sb = bridge_b.connection_state()["data"]
                if (
                    not sa["connected"]
                    and not sb["connected"]
                    and sa["user_stopped"] is True
                    and sb["user_stopped"] is True
                    and not sa["daemon_stopping"]
                    and not sb["daemon_stopping"]
                    and sa["reconnect_attempt"] >= 3
                    and sb["reconnect_attempt"] >= 3
                    and not plat_locks.is_exclusive_lock_held(lock_path)
                ):
                    break
                time.sleep(0.05)
            sa3 = bridge_a.connection_state()["data"]
            sb3 = bridge_b.connection_state()["data"]
            self.assertFalse(sa3["connected"], f"Window A stop undone by Window B (A): {sa3}")
            self.assertFalse(sb3["connected"], f"Window A stop undone by Window B (B): {sb3}")
            self.assertTrue(sa3["user_stopped"] and sb3["user_stopped"])
            self.assertFalse(plat_ipc.probe_daemon_endpoint(self.harness.home, timeout=0.2))

    def test_initialize_home_and_chat_plan_rpcs_over_real_meshd(self) -> None:
        """Batch 2.1 E2E: uninitialized home -> initialize_home -> real meshd -> chat.send/list/plan.list."""
        fresh_harness = MeshHarness(init_db=False, start_daemon=False)
        self.addCleanup(fresh_harness.teardown)

        bridge = DesktopBridge(
            home=fresh_harness.home,
            probe_timeout_s=2.0,
            probe_interval_s=0.25,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        env_patch = fresh_harness.get_env()
        with mock.patch.dict(os.environ, env_patch, clear=False):
            hs0 = bridge.home_status()
            self.assertTrue(hs0["ok"], hs0)
            self.assertFalse(hs0["data"]["home_initialized"])

            init_res = bridge.initialize_home(fresh_harness.passphrase)
            self.assertTrue(init_res["ok"], init_res)
            self.assertTrue(init_res["data"]["initialized"])
            self.assertTrue(init_res["data"]["daemon_started"])
            self.assertTrue(init_res["data"]["auth"]["auth_verified"])

            hs1 = bridge.home_status()
            self.assertTrue(hs1["ok"], hs1)
            self.assertTrue(hs1["data"]["home_initialized"])
            self.assertTrue(hs1["data"]["daemon_running"])
            self.assertTrue(hs1["data"]["auth_verified"])

            # Re-initializing an existing home is strictly refused
            dup = bridge.initialize_home("another-passphrase-12345")
            self.assertFalse(dup["ok"])
            self.assertEqual(dup["error"]["code"], c.ALREADY_EXISTS)

            # Create inbox lead & set lead.default_agent, then use chat.send(project_id=None)
            lead_res = bridge.call(
                "agent.create",
                {"name": "mesh-lead", "adapter": "none", "delivery": "inbox", "can_lead": True},
            )
            self.assertTrue(lead_res["ok"], lead_res)
            def_res = bridge.call("config.set", {"key": "lead.default_agent", "value": "mesh-lead"})
            self.assertTrue(def_res["ok"], def_res)

            send_res = bridge.call("chat.send", {"text": "请帮我规划一个日志清理脚本"})
            self.assertTrue(send_res["ok"], send_res)
            pid = send_res["data"]["project_id"]
            self.assertTrue(pid.startswith("prj_"))
            self.assertEqual(send_res["data"]["project"]["plan_gate_mode"], "require_approval")
            send_ls = send_res["data"]["lead_status"]
            self.assertEqual(send_ls["agent_name"], "mesh-lead")
            self.assertEqual(send_ls["delivery"], "inbox")
            self.assertGreaterEqual(send_ls["queued_outbox_count"], 1)
            self.assertIsNone(send_ls["active_run"])

            chat_list = bridge.call("chat.list", {"project_id": pid})
            self.assertTrue(chat_list["ok"], chat_list)
            self.assertEqual(len(chat_list["data"]["items"]), 1)
            self.assertEqual(chat_list["data"]["items"][0]["role"], "user")
            self.assertEqual(chat_list["data"]["lead_status"]["agent_name"], "mesh-lead")
            self.assertEqual(chat_list["data"]["lead_status"]["delivery"], "inbox")
            self.assertGreaterEqual(chat_list["data"]["lead_status"]["queued_outbox_count"], 1)

            plan_list = bridge.call("plan.list", {"project_id": pid})
            self.assertTrue(plan_list["ok"], plan_list)
            self.assertEqual(plan_list["data"]["items"], [])

            # Lead-only internal RPCs stay blocked from desktop bridge
            blocked = bridge.call("plan.dispatch", {"project_id": pid})
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"]["code"], "RPC_NOT_ALLOWED")

            # Verify Issue 2 (clean start meshd.log starts with INFO recovery, not ERROR)
            meshd_log_path = os.path.join(fresh_harness.home, "logs", "meshd.log")
            self.assertTrue(os.path.isfile(meshd_log_path))
            with open(meshd_log_path, encoding="utf-8") as f:
                meshd_lines = [ln.strip() for ln in f if ln.strip()]
            self.assertTrue(meshd_lines)
            self.assertIn(" INFO recovery: ", meshd_lines[0], meshd_lines[0])
            self.assertNotIn(" ERROR recovery: ", meshd_lines[0], meshd_lines[0])

            # Verify Issue 3 (desktop.log exists, records bridge events, never leaks passphrase)
            conn_snap = bridge.connection_state()["data"]
            desktop_log_path = os.path.join(fresh_harness.home, "logs", "desktop.log")
            self.assertEqual(conn_snap["desktop_log_path"], desktop_log_path)
            self.assertTrue(os.path.isfile(desktop_log_path))
            with open(desktop_log_path, encoding="utf-8") as f:
                desktop_log_text = f.read()
            self.assertIn("HOME_INITIALIZED", desktop_log_text)
            self.assertIn("cmd=chat.send", desktop_log_text)
            self.assertNotIn(fresh_harness.passphrase, desktop_log_text)

            # Batch 3 S4: inspect_repo_folder on real git repo -> chat.send auto-creates repo project
            from tests.gitfixture import GitFixture

            fixture = GitFixture()
            self.addCleanup(fixture.cleanup)
            repo = fixture.make_k0("b3-repo")
            insp = bridge.inspect_repo_folder(repo)
            self.assertTrue(insp["ok"], insp)
            self.assertTrue(insp["data"]["is_git_repo"])
            self.assertTrue(insp["data"]["is_toplevel"])
            rec_branch = insp["data"]["recommended_base_branch"]
            self.assertTrue(rec_branch)
            send_repo = bridge.call(
                "chat.send",
                {
                    "text": "请检查这个仓库并规划一个 README 改进",
                    "repo": insp["data"]["toplevel"],
                    "base_branch": rec_branch,
                    "test_command": insp["data"]["default_test_command"],
                },
            )
            self.assertTrue(send_repo["ok"], send_repo)
            self.assertEqual(send_repo["data"]["project"]["base_branch"], rec_branch)

            stop_res = bridge.stop_daemon()
            self.assertTrue(stop_res["ok"], stop_res)

    def test_desktop_bridge_card_approvals_permission_mode_and_standing_grants(self) -> None:
        """DesktopJsApi end-to-end: approval.decide (allow/reject/always_allow+confirm_auto_mode), permission_mode picker, grant.list/revoke, and audit.verify."""
        from mesh.client import MeshClient
        from mesh.desktop.bridge import DesktopJsApi

        bridge = DesktopBridge(
            home=self.harness.home,
            probe_timeout_s=2.0,
            probe_interval_s=0.25,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        js_api = DesktopJsApi(bridge)

        env_patch = self.harness.get_env()
        with mock.patch.dict(os.environ, env_patch, clear=False):
            self.assertTrue(bridge.start_daemon()["ok"])
            self.assertTrue(js_api.set_passphrase(self.harness.passphrase, True)["ok"])

            # Create lead & worker via desktop bridge, issue lead token via MeshClient for plan.propose/task.create
            self.assertTrue(js_api.call("agent.create", {
                "name": "desk-lead", "adapter": "none", "delivery": "inbox", "can_lead": True,
            })["ok"])
            self.assertTrue(js_api.call("agent.create", {
                "name": "desk-worker", "adapter": "none", "delivery": "inbox",
                "can_lead": False, "max_unapproved_profile": "dev",
            })["ok"])
            self.assertTrue(js_api.call("config.set", {"key": "lead.default_agent", "value": "desk-lead"})["ok"])

            direct_client = MeshClient(self.harness.home, timeout=15.0)
            tok_resp = direct_client.send_request({
                "v": 1, "cmd": "agent.token.issue", "args": {"name": "desk-lead", "ttl": "1h"},
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
            })
            self.assertTrue(tok_resp["ok"], tok_resp)
            lead_tok = tok_resp["data"]["token"]

            def lead_rpc(cmd: str, args: dict, approval: str | None = None) -> dict:
                req = {"v": 1, "cmd": cmd, "args": args, "auth": {"type": "token", "token": lead_tok}}
                if approval:
                    req["approval"] = approval
                return direct_client.send_request(req)

            # 1. Start chat project -> default permission_mode='plan'
            chat_res = js_api.call("chat.send", {"text": "规划一个桌面审批卡片演示"})
            self.assertTrue(chat_res["ok"], chat_res)
            pid = chat_res["data"]["project_id"]
            self.assertEqual(chat_res["data"]["project"]["permission_mode"], "plan")

            # 2. Switch permission mode via header/settings RPC: plan -> supervised -> plan
            m_sup = js_api.call("project.set_permission_mode", {
                "project_id": pid, "permission_mode": "supervised", "reason": "test supervised",
            })
            self.assertTrue(m_sup["ok"], m_sup)
            self.assertEqual(m_sup["data"]["permission_mode"], "supervised")
            m_plan = js_api.call("project.set_permission_mode", {
                "project_id": pid, "permission_mode": "plan", "reason": "back to plan",
            })
            self.assertTrue(m_plan["ok"], m_plan)
            self.assertEqual(m_plan["data"]["permission_mode"], "plan")

            # 3. Lead proposes plan -> chat.list hydrates card without short_code
            p1 = lead_rpc("plan.propose", {
                "project_id": pid, "title": "Plan v1", "summary": "Two steps",
                "steps": [
                    {"key": "s1", "title": "Normal step", "spec": "Do 1", "assignee": "desk-worker", "profile": "edit"},
                    {"key": "s2", "title": "Elevated step", "spec": "Do 2", "assignee": "desk-worker", "profile": "elevated", "depends_on": ["s1"]},
                ],
            })["data"]["plan"]
            clist = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist["ok"], clist)
            self.assertEqual(clist["data"]["permission_mode"], "plan")
            apr_view = clist["data"]["approvals_by_id"][p1["approval_id"]]
            self.assertNotIn("short_code", apr_view)
            self.assertEqual(apr_view["available_actions"], ["allow", "always_allow", "reject"])

            # 4. Click 始终允许 on plan card: step 1 returns confirmation_required=True; step 2 confirms auto mode
            ask = js_api.call("approval.decide", {
                "approval_id": p1["approval_id"], "action": "always_allow",
                "plan_hash": p1["plan_hash"], "plan_id": p1["id"], "plan_version": p1["version"],
            })
            self.assertTrue(ask["ok"], ask)
            self.assertTrue(ask["data"]["confirmation_required"])
            conf = js_api.call("approval.decide", {
                "approval_id": p1["approval_id"], "action": "always_allow",
                "plan_hash": p1["plan_hash"], "plan_id": p1["id"], "plan_version": p1["version"],
                "confirm_auto_mode": True,
            })
            self.assertTrue(conf["ok"], conf)
            self.assertFalse(conf["data"]["confirmation_required"])
            self.assertEqual(conf["data"]["permission_mode"], "auto")
            self.assertEqual(conf["data"]["plan_status"], "approved_pending_dispatch")

            # 5. Dispatch plan -> elevated step STILL produces a permission_escalation approval card
            disp = lead_rpc("plan.dispatch", {"project_id": pid, "plan_id": p1["id"]}, approval=p1["approval_id"])
            self.assertTrue(disp["ok"], disp)
            esc_list = disp["data"]["escalated_approvals"]
            self.assertEqual(len(esc_list), 1)
            esc = esc_list[0]

            # 6. Click 始终允许 on permission_escalation card -> creates narrow standing grant
            esc_dec = js_api.call("approval.decide", {
                "approval_id": esc["approval_id"], "action": "always_allow", "op_hash": esc["op_hash"],
            })
            self.assertTrue(esc_dec["ok"], esc_dec)
            self.assertTrue(esc_dec["data"]["grant_created"])
            grant_id = esc_dec["data"]["grant"]["id"]

            # 7. Settings grant.list shows the active grant, grant.revoke revokes it
            glist1 = js_api.call("grant.list", {"include_revoked": False})
            self.assertTrue(glist1["ok"], glist1)
            self.assertTrue(any(g["id"] == grant_id and g["active"] for g in glist1["data"]["items"]))

            rev = js_api.call("grant.revoke", {"grant_id": grant_id, "reason": "Revoked from desktop test"})
            self.assertTrue(rev["ok"], rev)
            self.assertTrue(rev["data"]["revoked"])
            glist2 = js_api.call("grant.list", {"include_revoked": False})
            self.assertFalse(any(g["id"] == grant_id for g in glist2["data"]["items"]))

            # 8. audit.verify passes through desktop bridge
            audit = js_api.call("audit.verify", {})
            self.assertTrue(audit["ok"], audit)

            self.assertTrue(bridge.stop_daemon()["ok"])

    def test_desktop_bridge_r3_contract_sync_auto_sweep_dead_task_and_suppressed_cards(self) -> None:
        """PR #69 round 3 DesktopJsApi E2E: project_status, cancelled_plan_approvals sweep, dead-task refusal/suppression, inactive-project suppression."""
        from mesh.client import MeshClient
        from mesh.desktop.bridge import DesktopJsApi

        bridge = DesktopBridge(
            home=self.harness.home,
            probe_timeout_s=2.0,
            probe_interval_s=0.25,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        js_api = DesktopJsApi(bridge)

        env_patch = self.harness.get_env()
        with mock.patch.dict(os.environ, env_patch, clear=False):
            self.assertTrue(bridge.start_daemon()["ok"])
            self.assertTrue(js_api.set_passphrase(self.harness.passphrase, True)["ok"])

            self.assertTrue(js_api.call("agent.create", {
                "name": "r3-lead", "adapter": "none", "delivery": "inbox", "can_lead": True,
            })["ok"])
            self.assertTrue(js_api.call("agent.create", {
                "name": "r3-worker", "adapter": "none", "delivery": "inbox",
                "can_lead": False, "max_unapproved_profile": "dev",
            })["ok"])
            self.assertTrue(js_api.call("config.set", {"key": "lead.default_agent", "value": "r3-lead"})["ok"])

            direct_client = MeshClient(self.harness.home, timeout=15.0)
            tok_resp = direct_client.send_request({
                "v": 1, "cmd": "agent.token.issue", "args": {"name": "r3-lead", "ttl": "1h"},
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
            })
            self.assertTrue(tok_resp["ok"], tok_resp)
            lead_tok = tok_resp["data"]["token"]

            def lead_rpc(cmd: str, args: dict) -> dict:
                return direct_client.send_request({
                    "v": 1, "cmd": cmd, "args": args, "auth": {"type": "token", "token": lead_tok},
                })

            chat_res = js_api.call("chat.send", {"text": "R3 contract sync test"})
            self.assertTrue(chat_res["ok"], chat_res)
            pid = chat_res["data"]["project_id"]

            # 1. Proposed plan card swept when switching to auto mode via project.set_permission_mode
            p1 = lead_rpc("plan.propose", {
                "project_id": pid,
                "title": "Sweepable Plan",
                "summary": "Will be swept on auto",
                "steps": [
                    {"key": "s1", "title": "Elevated step", "spec": "Do 1", "assignee": "r3-worker", "profile": "elevated"},
                    {"key": "s2", "title": "Second elevated step", "spec": "Do 2", "assignee": "r3-worker", "profile": "elevated"},
                ],
            })["data"]["plan"]
            p1_apr_id = p1["approval_id"]

            clist1 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist1["ok"], clist1)
            self.assertEqual(clist1["data"]["project_status"], "active")
            self.assertTrue(clist1["data"]["approvals_by_id"][p1_apr_id]["decidable"])
            self.assertIsNone(clist1["data"]["approvals_by_id"][p1_apr_id]["undecidable_reason"])

            mode_auto = js_api.call("project.set_permission_mode", {
                "project_id": pid, "permission_mode": "auto", "reason": "test auto sweep",
            })
            self.assertTrue(mode_auto["ok"], mode_auto)
            self.assertIn(p1_apr_id, mode_auto["data"]["cancelled_plan_approvals"])

            # Proposed plan remains proposed and dispatchable directly under auto mode; approval card suppressed
            clist2 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist2["ok"], clist2)
            self.assertEqual(clist2["data"]["plans_by_id"][p1["id"]]["status"], "proposed")
            swept_card = clist2["data"]["approvals_by_id"][p1_apr_id]
            self.assertEqual(swept_card["status"], "cancelled")
            self.assertEqual(swept_card["available_actions"], [])
            self.assertFalse(swept_card["decidable"])

            # 2. Dispatch plan under auto -> emits 2 permission_escalation cards; cancel task 1 -> dead-task card suppressed & refused
            disp = lead_rpc("plan.dispatch", {"project_id": pid, "plan_id": p1["id"]})
            self.assertTrue(disp["ok"], disp)
            esc_cards = disp["data"]["escalated_approvals"]
            self.assertEqual(len(esc_cards), 2)
            dead_esc = esc_cards[0]
            live_esc = esc_cards[1]

            cancel_res = js_api.call("task.cancel", {"task_id": dead_esc["task_id"], "reason": "cancel dead task"})
            self.assertTrue(cancel_res["ok"], cancel_res)

            clist3 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist3["ok"], clist3)
            dead_view = clist3["data"]["approvals_by_id"][dead_esc["approval_id"]]
            self.assertEqual(dead_view["available_actions"], ["reject"])
            self.assertTrue(dead_view["decidable"])
            self.assertFalse(dead_view["approvable"])
            self.assertTrue(dead_view["dismiss_only"])
            self.assertIsNone(dead_view["undecidable_reason"])
            self.assertEqual(dead_view["unapprovable_reason"], "task_terminal")
            self.assertEqual(dead_view["task_status"], "cancelled")

            refused_dead = js_api.call("approval.decide", {
                "approval_id": dead_esc["approval_id"],
                "action": "allow",
                "op_hash": dead_esc["op_hash"],
            })
            self.assertFalse(refused_dead["ok"])
            self.assertEqual(refused_dead["error"]["code"], c.INVALID_TRANSITION)
            self.assertEqual(refused_dead["error"]["details"]["reason"], "task_terminal")

            # Dismiss dead pending card via reject -> clears to status=rejected, available_actions=[]
            dismiss_dead = js_api.call("approval.decide", {
                "approval_id": dead_esc["approval_id"],
                "action": "reject",
                "op_hash": dead_esc["op_hash"],
                "reason": "Dismiss dead terminal task card",
            })
            self.assertTrue(dismiss_dead["ok"], dismiss_dead)
            self.assertEqual(dismiss_dead["data"]["status"], "rejected")

            # 3. Pause project -> live pending card becomes dismiss-only (project_not_active), allow returns PROJECT_NOT_ACTIVE
            pause_res = js_api.call("project.pause", {"project_id": pid, "reason": "pause for check"})
            self.assertTrue(pause_res["ok"], pause_res)

            clist4 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist4["ok"], clist4)
            self.assertEqual(clist4["data"]["project_status"], "paused")
            live_view = clist4["data"]["approvals_by_id"][live_esc["approval_id"]]
            self.assertEqual(live_view["available_actions"], ["reject"])
            self.assertTrue(live_view["decidable"])
            self.assertFalse(live_view["approvable"])
            self.assertTrue(live_view["dismiss_only"])
            self.assertTrue(live_view["inactive_project"])
            self.assertIsNone(live_view["undecidable_reason"])
            self.assertEqual(live_view["unapprovable_reason"], "project_not_active")

            refused_paused = js_api.call("approval.decide", {
                "approval_id": live_esc["approval_id"],
                "action": "allow",
                "op_hash": live_esc["op_hash"],
            })
            self.assertFalse(refused_paused["ok"])
            self.assertEqual(refused_paused["error"]["code"], c.PROJECT_NOT_ACTIVE)

            audit = js_api.call("audit.verify", {})
            self.assertTrue(audit["ok"], audit)
            self.assertTrue(bridge.stop_daemon()["ok"])

    def test_desktop_bridge_r4_progressed_task_meta_actions_and_already_auto(self) -> None:
        """PR #69 round 4 contracts via DesktopJsApi: live meta.actions, progressed task suppression, already_auto response."""
        from mesh.client import MeshClient
        from mesh.desktop.bridge import DesktopJsApi

        bridge = DesktopBridge(
            home=self.harness.home,
            probe_timeout_s=2.0,
            probe_interval_s=0.25,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        js_api = DesktopJsApi(bridge)

        env_patch = self.harness.get_env()
        with mock.patch.dict(os.environ, env_patch, clear=False):
            self.assertTrue(bridge.start_daemon()["ok"])
            self.assertTrue(js_api.set_passphrase(self.harness.passphrase, True)["ok"])

            self.assertTrue(js_api.call("agent.create", {
                "name": "r4-lead", "adapter": "none", "delivery": "inbox", "can_lead": True,
            })["ok"])
            self.assertTrue(js_api.call("agent.create", {
                "name": "r4-worker", "adapter": "none", "delivery": "inbox",
                "can_lead": False, "max_unapproved_profile": "dev",
            })["ok"])
            self.assertTrue(js_api.call("config.set", {"key": "lead.default_agent", "value": "r4-lead"})["ok"])

            direct_client = MeshClient(self.harness.home, timeout=15.0)
            tok_resp = direct_client.send_request({
                "v": 1, "cmd": "agent.token.issue", "args": {"name": "r4-lead", "ttl": "1h"},
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
            })
            self.assertTrue(tok_resp["ok"], tok_resp)
            lead_tok = tok_resp["data"]["token"]

            def lead_rpc(cmd: str, args: dict, approval: str | None = None) -> dict:
                req: Dict[str, Any] = {
                    "v": 1, "cmd": cmd, "args": args, "auth": {"type": "token", "token": lead_tok},
                }
                if approval is not None:
                    req["approval"] = approval
                return direct_client.send_request(req)

            # Create supervised project so dispatched step gets a step_dispatch card
            send = js_api.call("chat.send", {
                "text": "Run supervised plan for R4 contract test",
                "permission_mode": "supervised",
            })
            self.assertTrue(send["ok"], send)
            pid = send["data"]["project_id"]

            prop_resp = lead_rpc("plan.propose", {
                "project_id": pid,
                "title": "R4 Plan",
                "summary": "Single step to test task_progressed and meta.actions",
                "steps": [
                    {
                        "key": "s1",
                        "title": "Step 1",
                        "spec": "Do step 1",
                        "assignee": "r4-worker",
                        "profile": "dev",
                    },
                ],
            })
            self.assertTrue(prop_resp["ok"], prop_resp)
            prop = prop_resp["data"]
            plan_id = prop["plan"]["id"]
            plan_hash = prop["plan"]["plan_hash"]
            plan_apr_id = prop["plan"]["approval_id"]

            # Approve plan and dispatch -> creates step_dispatch card on created task
            dec_plan = js_api.call("approval.decide", {
                "approval_id": plan_apr_id,
                "action": "allow",
                "plan_hash": plan_hash,
                "plan_id": plan_id,
                "plan_version": 1,
            })
            self.assertTrue(dec_plan["ok"], dec_plan)

            disp_resp = lead_rpc(
                "plan.dispatch",
                {"project_id": pid, "plan_id": plan_id},
                approval=plan_apr_id,
            )
            self.assertTrue(disp_resp["ok"], disp_resp)
            disp = disp_resp["data"]
            self.assertEqual(len(disp["step_approvals"]), 1)
            step_apr = disp["step_approvals"][0]
            step_apr_id = step_apr["approval_id"]
            step_op_hash = step_apr["op_hash"]
            step_task_id = step_apr["task_id"]

            # Before task progresses: chat.list meta.actions matches approvals_by_id.available_actions
            clist1 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist1["ok"], clist1)
            step_view1 = clist1["data"]["approvals_by_id"][step_apr_id]
            self.assertEqual(step_view1["available_actions"], ["allow", "always_allow", "reject"])
            self.assertFalse(step_view1["task_progressed"])
            step_cards1 = [it for it in clist1["data"]["items"] if it.get("approval_id") == step_apr_id]
            self.assertEqual(len(step_cards1), 1)
            self.assertEqual(step_cards1[0]["meta"]["actions"], step_view1["available_actions"])

            # Direct user assigns task directly -> task leaves 'created' -> step card is now progressed!
            assign_res = js_api.call("task.assign", {"task_id": step_task_id, "to": "r4-worker"})
            self.assertTrue(assign_res["ok"], assign_res)

            clist2 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist2["ok"], clist2)
            step_view2 = clist2["data"]["approvals_by_id"][step_apr_id]
            self.assertEqual(step_view2["available_actions"], ["reject"])
            self.assertTrue(step_view2["decidable"])
            self.assertFalse(step_view2["approvable"])
            self.assertTrue(step_view2["dismiss_only"])
            self.assertTrue(step_view2["task_progressed"])
            self.assertIsNone(step_view2["undecidable_reason"])
            self.assertEqual(step_view2["unapprovable_reason"], "task_progressed")
            self.assertIn(step_view2["task_status"], ("assigned", "queued"))
            step_cards2 = [it for it in clist2["data"]["items"] if it.get("approval_id") == step_apr_id]
            self.assertEqual(step_cards2[0]["meta"]["actions"], ["reject"])

            refused_prog = js_api.call("approval.decide", {
                "approval_id": step_apr_id,
                "action": "allow",
                "op_hash": step_op_hash,
            })
            self.assertFalse(refused_prog["ok"])
            self.assertEqual(refused_prog["error"]["code"], c.INVALID_TRANSITION)
            self.assertEqual(refused_prog["error"]["details"]["reason"], "task_progressed")
            self.assertIn(refused_prog["error"]["details"]["task_status"], ("assigned", "queued"))
            self.assertEqual(refused_prog["error"]["details"]["expected_task_statuses"], ["created"])

            # Already-auto check: approve plan_dispatch, switch project to auto, then call always_allow
            send2 = js_api.call("chat.send", {
                "text": "Already auto test project",
                "permission_mode": "plan",
            })
            self.assertTrue(send2["ok"], send2)
            pid2 = send2["data"]["project_id"]
            prop2_resp = lead_rpc("plan.propose", {
                "project_id": pid2,
                "title": "Plan 2",
                "summary": "Check already_auto response",
                "steps": [
                    {"key": "a1", "title": "A1", "spec": "Run A1", "assignee": "r4-worker", "profile": "dev"},
                ],
            })
            self.assertTrue(prop2_resp["ok"], prop2_resp)
            prop2 = prop2_resp["data"]
            p2_id = prop2["plan"]["id"]
            p2_hash = prop2["plan"]["plan_hash"]
            p2_apr = prop2["plan"]["approval_id"]
            self.assertTrue(js_api.call("approval.decide", {
                "approval_id": p2_apr,
                "action": "allow",
                "plan_hash": p2_hash,
                "plan_id": p2_id,
                "plan_version": 1,
            })["ok"])
            self.assertTrue(js_api.call("project.set_permission_mode", {
                "project_id": pid2,
                "permission_mode": "auto",
            })["ok"])

            already_auto_res = js_api.call("approval.decide", {
                "approval_id": p2_apr,
                "action": "always_allow",
                "plan_hash": p2_hash,
                "plan_id": p2_id,
                "plan_version": 1,
            })
            self.assertTrue(already_auto_res["ok"], already_auto_res)
            self.assertFalse(already_auto_res["data"]["confirmation_required"])
            self.assertTrue(already_auto_res["data"]["already_auto"])
            self.assertFalse(already_auto_res["data"]["mode_changed"])
            self.assertEqual(already_auto_res["data"]["permission_mode"], "auto")

            audit = js_api.call("audit.verify", {})
            self.assertTrue(audit["ok"], audit)
            self.assertTrue(bridge.stop_daemon()["ok"])

    def test_desktop_bridge_r5_dismiss_dead_pending_card_and_relayed_reject_upgrade(self) -> None:
        """PR #69 round 5 via DesktopJsApi: dismiss dead pending card via reject & upgrade relayed-rejected card via reject."""
        from mesh.client import MeshClient
        from mesh.desktop.bridge import DesktopJsApi

        bridge = DesktopBridge(
            home=self.harness.home,
            probe_timeout_s=2.0,
            probe_interval_s=0.25,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        js_api = DesktopJsApi(bridge)

        env_patch = self.harness.get_env()
        with mock.patch.dict(os.environ, env_patch, clear=False):
            self.assertTrue(bridge.start_daemon()["ok"])
            self.assertTrue(js_api.set_passphrase(self.harness.passphrase, True)["ok"])

            self.assertTrue(js_api.call("agent.create", {
                "name": "r5-lead", "adapter": "none", "delivery": "inbox", "can_lead": True,
            })["ok"])
            self.assertTrue(js_api.call("agent.create", {
                "name": "r5-worker", "adapter": "none", "delivery": "inbox",
                "can_lead": False, "max_unapproved_profile": "dev",
            })["ok"])
            self.assertTrue(js_api.call("config.set", {"key": "lead.default_agent", "value": "r5-lead"})["ok"])
            self.assertTrue(js_api.call("config.set", {
                "key": "approval.policy.permission_escalation", "value": "direct_or_relayed",
            })["ok"])

            direct_client = MeshClient(self.harness.home, timeout=15.0)
            lead_tok = direct_client.send_request({
                "v": 1, "cmd": "agent.token.issue", "args": {"name": "r5-lead", "ttl": "1h"},
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
            })["data"]["token"]
            rel_agent_res = direct_client.send_request({
                "v": 1, "cmd": "agent.create",
                "args": {"name": "r5-relayer", "adapter": "none", "delivery": "inbox", "capabilities": ["intake", "relay"]},
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
            })
            self.assertTrue(rel_agent_res["ok"], rel_agent_res)
            relay_res = direct_client.send_request({
                "v": 1, "cmd": "agent.token.issue", "args": {"name": "r5-relayer", "ttl": "1h"},
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
            })
            self.assertTrue(relay_res["ok"], relay_res)
            relay_tok = relay_res["data"]["token"]

            def lead_rpc(cmd: str, args: dict) -> dict:
                return direct_client.send_request({
                    "v": 1, "cmd": cmd, "args": args, "auth": {"type": "token", "token": lead_tok},
                })

            send = js_api.call("chat.send", {"text": "R5 contract test", "permission_mode": "auto"})
            self.assertTrue(send["ok"], send)
            pid = send["data"]["project_id"]

            p1 = lead_rpc("plan.propose", {
                "project_id": pid,
                "title": "R5 Escalation Plan",
                "summary": "Two elevated steps",
                "steps": [
                    {"key": "s1", "title": "Elevated 1", "spec": "Do 1", "assignee": "r5-worker", "profile": "elevated"},
                    {"key": "s2", "title": "Elevated 2", "spec": "Do 2", "assignee": "r5-worker", "profile": "elevated"},
                ],
            })["data"]["plan"]
            disp = lead_rpc("plan.dispatch", {"project_id": pid, "plan_id": p1["id"]})
            self.assertTrue(disp["ok"], disp)
            esc_cards = disp["data"]["escalated_approvals"]
            self.assertEqual(len(esc_cards), 2)
            esc1, esc2 = esc_cards[0], esc_cards[1]

            # 1. Dead pending card (cancelled task) exposes dismiss-only ["reject"] and can be dismissed via reject
            self.assertTrue(js_api.call("task.cancel", {"task_id": esc1["task_id"], "reason": "cancel"})["ok"])
            clist1 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist1["ok"], clist1)
            dead_v = clist1["data"]["approvals_by_id"][esc1["approval_id"]]
            self.assertEqual(dead_v["available_actions"], ["reject"])
            self.assertTrue(dead_v["dismiss_only"])
            self.assertFalse(dead_v["approvable"])
            self.assertEqual(dead_v["unapprovable_reason"], "task_terminal")
            self.assertIsNone(dead_v["undecidable_reason"])

            dismiss_res = js_api.call("approval.decide", {
                "approval_id": esc1["approval_id"],
                "action": "reject",
                "op_hash": esc1["op_hash"],
                "reason": "Dismissed dead card",
            })
            self.assertTrue(dismiss_res["ok"], dismiss_res)
            self.assertEqual(dismiss_res["data"]["status"], "rejected")

            # 2. Relayed-rejected card exposes ["reject"] (dismiss_only=False) and upgrades to direct_verified
            rel_rej = direct_client.send_request({
                "v": 1,
                "cmd": "approval.reject",
                "args": {
                    "approval_id": esc2["approval_id"],
                    "op_hash": esc2["op_hash"],
                },
                "relayed": True,
                "user_statement": "Reject from relay",
                "auth": {"type": "token", "token": relay_tok},
            })
            self.assertTrue(rel_rej["ok"], rel_rej)

            clist2 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist2["ok"], clist2)
            # Card 1 is now direct-rejected -> []
            self.assertEqual(clist2["data"]["approvals_by_id"][esc1["approval_id"]]["available_actions"], [])
            # Card 2 is relayed-rejected -> ["reject"] upgradeable
            rel_v = clist2["data"]["approvals_by_id"][esc2["approval_id"]]
            self.assertEqual(rel_v["status"], "rejected")
            self.assertEqual(rel_v["decided_via"], "relayed")
            self.assertEqual(rel_v["available_actions"], ["reject"])
            self.assertTrue(rel_v["decidable"])
            self.assertFalse(rel_v["approvable"])
            self.assertFalse(rel_v["dismiss_only"])
            self.assertIsNone(rel_v["undecidable_reason"])
            self.assertEqual(rel_v["unapprovable_reason"], "rejected")

            up_res = js_api.call("approval.decide", {
                "approval_id": esc2["approval_id"],
                "action": "reject",
                "op_hash": esc2["op_hash"],
                "reason": "Direct confirm reject",
            })
            self.assertTrue(up_res["ok"], up_res)
            self.assertEqual(up_res["data"]["status"], "rejected")
            self.assertEqual(up_res["data"]["decided_via"], "direct")
            self.assertEqual(up_res["data"]["trust"], "direct_verified")
            self.assertEqual(up_res["data"]["upgraded_from"], "relayed")
            self.assertTrue(up_res["data"]["user_mac"])

            clist3 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist3["ok"], clist3)
            self.assertEqual(clist3["data"]["approvals_by_id"][esc2["approval_id"]]["available_actions"], [])
            self.assertFalse(clist3["data"]["approvals_by_id"][esc2["approval_id"]]["decidable"])

            audit = js_api.call("audit.verify", {})
            self.assertTrue(audit["ok"], audit)
            self.assertTrue(bridge.stop_daemon()["ok"])

    def test_desktop_bridge_r6_external_cards_supervised_always_allow_and_plan_gate_audit(self) -> None:
        """PR #69 round 6 via DesktopJsApi: external.* cards allow/reject only, supervised escalation always_allow single-step consume, relayed escaping, and plan_gate_mode audit."""
        import sqlite3
        from mesh.client import MeshClient
        from mesh.desktop.bridge import DesktopJsApi

        bridge = DesktopBridge(
            home=self.harness.home,
            probe_timeout_s=2.0,
            probe_interval_s=0.25,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        js_api = DesktopJsApi(bridge)

        env_patch = self.harness.get_env()
        with mock.patch.dict(os.environ, env_patch, clear=False):
            self.assertTrue(bridge.start_daemon()["ok"])
            self.assertTrue(js_api.set_passphrase(self.harness.passphrase, True)["ok"])

            self.assertTrue(js_api.call("agent.create", {
                "name": "r6-lead", "adapter": "none", "delivery": "inbox", "can_lead": True,
            })["ok"])
            self.assertTrue(js_api.call("agent.create", {
                "name": "r6-worker", "adapter": "none", "delivery": "inbox",
                "can_lead": False, "max_unapproved_profile": "dev",
            })["ok"])
            self.assertTrue(js_api.call("config.set", {"key": "lead.default_agent", "value": "r6-lead"})["ok"])

            direct_client = MeshClient(self.harness.home, timeout=15.0)
            lead_tok = direct_client.send_request({
                "v": 1, "cmd": "agent.token.issue", "args": {"name": "r6-lead", "ttl": "1h"},
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
            })["data"]["token"]

            def lead_rpc(cmd: str, args: dict, approval: str | None = None) -> dict:
                req: Dict[str, Any] = {
                    "v": 1, "cmd": cmd, "args": args, "auth": {"type": "token", "token": lead_tok},
                }
                if approval is not None:
                    req["approval"] = approval
                return direct_client.send_request(req)

            # Create supervised project via desktop chat.send
            send = js_api.call("chat.send", {
                "text": "R6 contract sync project",
                "permission_mode": "supervised",
            })
            self.assertTrue(send["ok"], send)
            pid = send["data"]["project_id"]

            # 1. approval.request rejects internal-only categories plan_dispatch / step_dispatch
            for bad_cat in ("plan_dispatch", "step_dispatch"):
                bad_req = lead_rpc("approval.request", {
                    "category": bad_cat,
                    "project_id": pid,
                    "summary": f"Attempt forged {bad_cat}",
                })
                self.assertFalse(bad_req["ok"], bad_req)
                self.assertEqual(bad_req["error"]["code"], c.INVALID_ARGUMENT)

            # 2. External card (external.permission_escalation) exposes ["allow", "reject"], always_allow_effect=None,
            #    distinct external copy, and rejects approval.decide(action="always_allow")
            ext_req = lead_rpc("approval.request", {
                "category": "permission_escalation",
                "project_id": pid,
                "summary": "External API escalation request",
                "operation": {"action": "external_api_call", "to": "r6-worker", "profile": "elevated"},
            })
            self.assertTrue(ext_req["ok"], ext_req)
            ext_apr_id = ext_req["data"]["approval_id"]
            ext_op_hash = ext_req["data"]["op_hash"]

            clist1 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist1["ok"], clist1)
            self.assertEqual(clist1["data"]["plan_gate_mode"], "require_approval")
            self.assertEqual(clist1["data"]["permission_mode"], "supervised")
            ext_view = clist1["data"]["approvals_by_id"][ext_apr_id]
            self.assertEqual(ext_view["op_type"], "external.permission_escalation")
            self.assertEqual(ext_view["available_actions"], ["allow", "reject"])
            self.assertIsNone(ext_view["always_allow_effect"])
            ext_cards = [it for it in clist1["data"]["items"] if it.get("approval_id") == ext_apr_id]
            self.assertEqual(len(ext_cards), 1)
            self.assertEqual(ext_cards[0]["meta"]["actions"], ["allow", "reject"])
            self.assertIn("外部请求确认", ext_cards[0]["body"])

            ext_always = js_api.call("approval.decide", {
                "approval_id": ext_apr_id,
                "action": "always_allow",
                "op_hash": ext_op_hash,
            })
            self.assertFalse(ext_always["ok"])
            self.assertEqual(ext_always["error"]["code"], c.INVALID_ARGUMENT)
            glist_empty = js_api.call("grant.list", {"project_id": pid})
            self.assertTrue(glist_empty["ok"], glist_empty)
            self.assertEqual(glist_empty["data"]["items"], [])

            ext_allow = js_api.call("approval.decide", {
                "approval_id": ext_apr_id,
                "action": "allow",
                "op_hash": ext_op_hash,
            })
            self.assertTrue(ext_allow["ok"], ext_allow)
            self.assertEqual(ext_allow["data"]["status"], "approved")

            # 3. Supervised escalated step: clicking always_allow mints grant AND allows task.assign --approval <apr>
            #    to consume that exact card cleanly in one step
            p1_resp = lead_rpc("plan.propose", {
                "project_id": pid,
                "title": "Supervised Escalation Plan",
                "summary": "Verify single-step consume after always_allow",
                "steps": [
                    {"key": "s1", "title": "Elevated step", "spec": "Run elevated", "assignee": "r6-worker", "profile": "elevated"},
                ],
            })
            self.assertTrue(p1_resp["ok"], p1_resp)
            p1 = p1_resp["data"]["plan"]
            self.assertTrue(js_api.call("approval.decide", {
                "approval_id": p1["approval_id"],
                "action": "allow",
                "plan_hash": p1["plan_hash"],
                "plan_id": p1["id"],
                "plan_version": 1,
            })["ok"])
            disp = lead_rpc("plan.dispatch", {"project_id": pid, "plan_id": p1["id"]}, approval=p1["approval_id"])
            self.assertTrue(disp["ok"], disp)
            self.assertEqual(len(disp["data"]["escalated_approvals"]), 1)
            esc = disp["data"]["escalated_approvals"][0]

            clist2 = js_api.call("chat.list", {"project_id": pid})
            self.assertTrue(clist2["ok"], clist2)
            esc_view = clist2["data"]["approvals_by_id"][esc["approval_id"]]
            self.assertEqual(esc_view["policy"], "direct_only")
            self.assertEqual(esc_view["available_actions"], ["allow", "always_allow", "reject"])
            self.assertEqual(esc_view["always_allow_effect"]["kind"], "standing_grant")

            always_esc = js_api.call("approval.decide", {
                "approval_id": esc["approval_id"],
                "action": "always_allow",
                "op_hash": esc["op_hash"],
            })
            self.assertTrue(always_esc["ok"], always_esc)
            self.assertTrue(always_esc["data"]["grant_created"])

            assign_ok = lead_rpc(
                "task.assign",
                {"task_id": esc["task_id"], "to": "r6-worker", "profile": "elevated"},
                approval=esc["approval_id"],
            )
            self.assertTrue(assign_ok["ok"], assign_ok)

            # 4. audit.verify passes before tamper, and catches projects.plan_gate_mode tamper via DesktopJsApi
            audit_ok = js_api.call("audit.verify", {})
            self.assertTrue(audit_ok["ok"], audit_ok)

            db_path = os.path.join(self.harness.home, "mesh.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("UPDATE projects SET plan_gate_mode = 'off' WHERE id = ?;", (pid,))
                conn.commit()
            finally:
                conn.close()

            audit_bad = js_api.call("audit.verify", {})
            self.assertFalse(audit_bad["ok"], audit_bad)
            self.assertEqual(audit_bad["error"]["code"], c.AUDIT_MISMATCH)
            self.assertIn(f"project_plan_gate:{pid}", audit_bad["error"]["details"]["state_mismatches"])

            self.assertTrue(bridge.stop_daemon()["ok"])

    def test_desktop_bridge_r3_bounded_hydrated_queries_allowlist_and_auto_confirm_project_name(self) -> None:
        """PR #70 final review round 3: bounded approval.list/run.list filters, card hydration, removed legacy RPCs, and auto-mode confirm project name."""
        from mesh.client import MeshClient
        from mesh.desktop.bridge import DesktopJsApi

        bridge = DesktopBridge(
            home=self.harness.home,
            probe_timeout_s=2.0,
            probe_interval_s=0.25,
            stream_poll_timeout_s=0.1,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        js_api = DesktopJsApi(bridge)

        env_patch = self.harness.get_env()
        with mock.patch.dict(os.environ, env_patch, clear=False):
            self.assertTrue(bridge.start_daemon()["ok"])
            self.assertTrue(js_api.set_passphrase(self.harness.passphrase, True)["ok"])

            # 1. Removed legacy methods (approval.approve/reject/cancel, project.note) are refused by DesktopJsApi
            for removed_method in ("approval.approve", "approval.reject", "approval.cancel", "project.note"):
                res_blocked = js_api.call(removed_method, {})
                self.assertFalse(res_blocked["ok"], f"{removed_method} must be blocked")
                self.assertEqual(res_blocked["error"]["code"], "RPC_NOT_ALLOWED")

            self.assertTrue(js_api.call("agent.create", {
                "name": "r3-lead", "adapter": "none", "delivery": "inbox", "can_lead": True,
            })["ok"])
            self.assertTrue(js_api.call("agent.create", {
                "name": "r3-worker", "adapter": "none", "delivery": "inbox",
                "can_lead": False, "max_unapproved_profile": "edit",
            })["ok"])
            self.assertTrue(js_api.call("config.set", {"key": "lead.default_agent", "value": "r3-lead"})["ok"])

            direct_client = MeshClient(self.harness.home, timeout=15.0)
            lead_tok = direct_client.send_request({
                "v": 1, "cmd": "agent.token.issue", "args": {"name": "r3-lead", "ttl": "1h"},
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
            })["data"]["token"]

            # 2. Create two named projects so we can verify project_name in auto-mode confirm and project-scoped run.list
            send_a = js_api.call("chat.send", {"text": "Alpha project task", "lead": "r3-lead"})
            self.assertTrue(send_a["ok"], send_a)
            pid_a = send_a["data"]["project_id"]
            send_b = js_api.call("chat.send", {"text": "Beta project task", "lead": "r3-lead"})
            self.assertTrue(send_b["ok"], send_b)
            pid_b = send_b["data"]["project_id"]

            def lead_rpc(cmd: str, args: dict, *, approval: str | None = None) -> dict:
                req: Dict[str, Any] = {
                    "v": 1, "cmd": cmd, "args": args, "auth": {"type": "token", "token": lead_tok},
                }
                if approval is not None:
                    req["approval"] = approval
                return direct_client.send_request(req)

            # Propose two plans on project A: reject plan 1 (making it historical terminal), keep plan 2 pending
            prop1 = lead_rpc("plan.propose", {
                "project_id": pid_a,
                "title": "Plan 1 (will reject)",
                "summary": "First attempt",
                "steps": [{
                    "key": "s1",
                    "title": "Step 1",
                    "spec": "Detailed spec for step 1",
                    "kind": "general",
                    "assignee": "r3-worker",
                    "profile": "edit",
                }],
            })
            self.assertTrue(prop1["ok"], prop1)
            p1 = prop1["data"]["plan"]
            apr1_id = p1["approval_id"]
            p1_hash = p1["plan_hash"]

            rej1 = js_api.call("approval.decide", {
                "approval_id": apr1_id,
                "action": "reject",
                "plan_hash": p1_hash,
                "reason": "Need revision",
            })
            self.assertTrue(rej1["ok"], rej1)

            prop2 = lead_rpc("plan.propose", {
                "project_id": pid_a,
                "title": "Plan 2 (active)",
                "summary": "Revised plan",
                "steps": [{
                    "key": "s1",
                    "title": "Step 1 v2",
                    "spec": "Detailed spec v2 with safe instructions",
                    "kind": "general",
                    "assignee": "r3-worker",
                    "profile": "edit",
                }],
            })
            self.assertTrue(prop2["ok"], prop2)
            p2 = prop2["data"]["plan"]
            apr2_id = p2["approval_id"]
            p2_hash = p2["plan_hash"]

            # 3. approval.list with actionable_only=True, include_card_fields=True, limit=200 excludes direct-rejected apr1, includes hydrated apr2
            apr_act = js_api.call("approval.list", {
                "actionable_only": True,
                "include_card_fields": True,
                "limit": 200,
            })
            self.assertTrue(apr_act["ok"], apr_act)
            act_items = apr_act["data"]["items"]
            act_by_id = {it["id"]: it for it in act_items}
            self.assertNotIn(apr1_id, act_by_id)
            self.assertIn(apr2_id, act_by_id)
            self.assertEqual(act_by_id[apr2_id]["available_actions"], ["allow", "always_allow", "reject"])
            self.assertEqual(act_by_id[apr2_id]["plan_hash"], p2_hash)
            self.assertEqual(act_by_id[apr2_id]["always_allow_effect"]["kind"], "switch_permission_mode")

            # include_ids re-hydrates historical older-page apr1 in the same bounded call
            apr_with_inc = js_api.call("approval.list", {
                "actionable_only": True,
                "include_card_fields": True,
                "include_ids": [apr1_id],
                "limit": 200,
            })
            self.assertTrue(apr_with_inc["ok"], apr_with_inc)
            inc_by_id = {it["id"]: it for it in apr_with_inc["data"]["items"]}
            self.assertIn(apr1_id, inc_by_id)
            self.assertEqual(inc_by_id[apr1_id]["status"], "rejected")
            self.assertEqual(inc_by_id[apr1_id]["available_actions"], [])
            self.assertFalse(inc_by_id[apr1_id]["decidable"])

            # 4. approval.decide always_allow confirmation_required returns project_name and names the actual project (not 当前项目)
            prj_a_show = js_api.call("project.show", {"project_id": pid_a})
            self.assertTrue(prj_a_show["ok"], prj_a_show)
            prj_a_name = prj_a_show["data"]["name"]

            conf_res = js_api.call("approval.decide", {
                "approval_id": apr2_id,
                "action": "always_allow",
                "plan_hash": p2_hash,
            })
            self.assertTrue(conf_res["ok"], conf_res)
            self.assertTrue(conf_res["data"]["confirmation_required"])
            self.assertEqual(conf_res["data"]["project_id"], pid_a)
            self.assertEqual(conf_res["data"]["project_name"], prj_a_name)
            self.assertIn(prj_a_name, conf_res["data"]["message"])
            self.assertNotIn("当前项目", conf_res["data"]["message"])

            # 5. run.list bounded filters: statuses, project, limit
            runs_active = js_api.call("run.list", {"statuses": ["starting", "running"], "limit": 50})
            self.assertTrue(runs_active["ok"], runs_active)
            for r in runs_active["data"]["runs"]:
                self.assertIn(r["status"], ("starting", "running"))

            runs_b = js_api.call("run.list", {"project": pid_b, "limit": 10})
            self.assertTrue(runs_b["ok"], runs_b)
            for r in runs_b["data"]["runs"]:
                self.assertEqual(r["project_id"], pid_b)

            bad_limit = js_api.call("run.list", {"limit": 0})
            self.assertFalse(bad_limit["ok"])
            self.assertEqual(bad_limit["error"]["code"], c.INVALID_ARGUMENT)

            self.assertTrue(bridge.stop_daemon()["ok"])


if __name__ == "__main__":
    unittest.main()
