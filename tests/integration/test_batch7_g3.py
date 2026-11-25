"""Integration regression tests for batch7 g3 findings against a live meshd."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict

import mesh.constants as c
from mesh.core.auth import generate_run_token
from mesh.core.clock import now_iso_utc
from mesh.core.ids import new_id
from mesh.platform import ipc as plat_ipc, perms as plat_perms, pipe_pump
from tests.helpers import MeshHarness, cli_popen_argv
from tests.integration.test_lead_actions import _fake_goal
from tests.integration.test_lead_turns import LeadTurnCase, create_agent
from tests.stubs import create_test_stubs


def _tok(prefix: str = "sk-", char: str = "z", length: int = 24) -> str:
    return prefix + (char * length)


class TestBatch7G3DaemonFlows(unittest.TestCase):
    """End-to-end daemon tests for redaction, MESH_SOCKET, auth event wakeup, and inbox wait supersede."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-g3-int-stubs-")
        create_test_stubs(self.stubs_dir)
        self.h = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)

    def tearDown(self) -> None:
        self.h.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def test_f1_set_lead_and_complete_redact_end_to_end(self) -> None:
        tok_orig = create_agent(self.h, "orig", adapter="none", delivery="inbox", capabilities=["intake"])
        tok_l1 = create_agent(self.h, "lead1", adapter="none", delivery="inbox", can_lead=True)
        tok_l2 = create_agent(self.h, "lead2", adapter="none", delivery="inbox", can_lead=True)

        goal_sec = "goalsk991" + ("x" * 16)
        crit_sec = "critpw882" + ("y" * 16)
        reason_sec = "reasonsk773" + ("z" * 16)
        rep_sec = "reportsk664" + ("w" * 16)

        rc, prj_resp = self.h.cli(
            "intake", "submit",
            "--title", "Redact Prj",
            "--goal", f"Goal api_key={goal_sec}",
            "--criterion", f"C1 password={crit_sec}",
            "--lead", "lead1",
            token=tok_orig,
        )
        self.assertEqual(rc, 0, prj_resp)
        prj_id = prj_resp["data"]["id"]
        rc_c, crit_resp = self.h.cli("project", "show", prj_id, user=True)
        self.assertEqual(rc_c, 0, crit_resp)
        crit_id = crit_resp["data"]["criteria"][0]["id"]

        rc, sl_resp = self.h.cli(
            "project", "set-lead", prj_id, "lead2",
            "--reason", f"api_key={reason_sec}",
            user=True,
        )
        self.assertEqual(rc, 0, sl_resp)

        # Inbox read for lead2 must never expose secrets in project.handover.
        rc, ibx_resp = self.h.cli("inbox", "list", "--peek", token=tok_l2)
        self.assertEqual(rc, 0, ibx_resp)
        dumped_ibx = json.dumps(ibx_resp["data"]["items"])
        self.assertIn("project.handover", dumped_ibx)
        self.assertIn("[REDACTED]", dumped_ibx)
        self.assertNotIn(goal_sec, dumped_ibx)
        self.assertNotIn(crit_sec, dumped_ibx)
        self.assertNotIn(reason_sec, dumped_ibx)

        # Raw DB outbox rows for handover and lead_changed must already be redacted.
        conn = self.h.db()
        try:
            out_rows = conn.execute(
                "SELECT type, payload_json FROM outbox WHERE project_id = ? "
                "AND type IN ('project.handover', 'project.lead_changed');",
                (prj_id,),
            ).fetchall()
            self.assertGreaterEqual(len(out_rows), 2)
            for row in out_rows:
                self.assertIn("[REDACTED]", row["payload_json"], row["type"])
                self.assertNotIn(goal_sec, row["payload_json"], row["type"])
                self.assertNotIn(reason_sec, row["payload_json"], row["type"])
        finally:
            conn.close()

        # Event list redacts reason on project.lead_changed.
        rc, ev_resp = self.h.cli("event", "list", "--project", prj_id, user=True)
        self.assertEqual(rc, 0, ev_resp)
        dumped_ev = json.dumps(ev_resp["data"]["items"])
        self.assertIn("[REDACTED]", dumped_ev)
        self.assertNotIn(reason_sec, dumped_ev)

        # Complete project with secret inside completion report -> persisted & returned redacted.
        report_payload = {
            "summary": f"Completed api_key={rep_sec}",
            "criteria": [{"id": crit_id, "met": True, "evidence": f"password={rep_sec}"}],
        }
        rc, comp_resp = self.h.cli(
            "--input", "-", "project", "complete", prj_id,
            stdin=json.dumps({"report": report_payload}),
            user=True,
        )
        self.assertEqual(rc, 0, comp_resp)
        conn = self.h.db()
        try:
            raw_comp = conn.execute(
                "SELECT completion_report_json FROM projects WHERE id = ?;", (prj_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIn("[REDACTED]", raw_comp)
        self.assertNotIn(rep_sec, raw_comp)
        self.assertGreater(len(tok_l1), 0)

    def test_f3_mesh_socket_client_rejects_target_workspace_token_file(self) -> None:
        tok_w = create_agent(self.h, "w1", adapter="none", delivery="inbox")
        ws_tok = os.path.join(self.h.home, "workspaces", "prj_fake", "w.tok")
        os.makedirs(os.path.dirname(ws_tok), mode=0o700, exist_ok=True)
        with open(ws_tok, "w", encoding="utf-8") as f:
            f.write(tok_w + "\n")
        plat_perms.set_private_file_acl(ws_tok)

        fake_user_home = os.path.join(self.h.fixtures_dir, "empty_user_home")
        os.makedirs(fake_user_home, mode=0o700, exist_ok=True)
        plat_perms.set_private_dir_acl(fake_user_home)
        env = self.h.get_env()
        env.pop("MESH_HOME", None)
        env["HOME"] = fake_user_home
        env["USERPROFILE"] = fake_user_home
        env["MESH_SOCKET"] = plat_ipc.daemon_endpoint_path(self.h.home)

        cmd = pipe_pump.prepare_win_cmd_args([self.h.bin_mesh, "--token-file", ws_tok, "whoami"], env=env)
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, c.EXIT_UNAUTHENTICATED, proc.stdout + proc.stderr)
        resp = json.loads(proc.stdout)
        self.assertEqual(resp["error"]["code"], c.TOKEN_FILE_INSECURE)

    def test_f6_f7_auth_failed_wakes_tail_and_superseded_inbox_wait(self) -> None:
        # 1. auth.failed wakes a streaming event tail before timeout.
        cmd_tail, env_tail = cli_popen_argv(
            self.h, "--user", "--passphrase-file", self.h.passphrase_file,
            "event", "tail", "--since", "0", "--timeout", "10s",
        )
        tail_proc = subprocess.Popen(
            cmd_tail,
            env=env_tail,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(1.0)
            bad_pw = os.path.join(self.h.fixtures_dir, "bad.pw")
            with open(bad_pw, "w", encoding="utf-8") as f:
                f.write("definitely-wrong-passphrase-123\n")
            plat_perms.set_private_file_acl(bad_pw)
            rc_bad, _ = self.h.cli("--user", "--passphrase-file", bad_pw, "whoami")
            self.assertEqual(rc_bad, c.EXIT_UNAUTHENTICATED)

            deadline = time.monotonic() + 5.0
            seen_auth_failed = False
            assert tail_proc.stdout is not None
            while time.monotonic() < deadline:
                line = tail_proc.stdout.readline()
                if not line:
                    break
                if '"auth.failed"' in line:
                    seen_auth_failed = True
                    break
            self.assertTrue(seen_auth_failed, "event tail did not wake for auth.failed")
        finally:
            if tail_proc.poll() is None:
                tail_proc.terminate()
                try:
                    tail_proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    tail_proc.kill()
                    tail_proc.wait()
            if tail_proc.stdout:
                tail_proc.stdout.close()
            if tail_proc.stderr:
                tail_proc.stderr.close()

        # 2. Superseded lead in active inbox wait gets LEAD_SUPERSEDED on wakeup and steals nothing.
        tok_l1 = create_agent(self.h, "slead1", adapter="none", delivery="inbox", can_lead=True, capabilities=["intake"])
        _tok_l2 = create_agent(self.h, "slead2", adapter="none", delivery="inbox", can_lead=True)
        rc, prj_resp = self.h.cli(
            "intake", "submit", "--title", "Supersede Wait", "--goal", "G", "--criterion", "C1",
            "--lead", "slead1", token=tok_l1,
        )
        self.assertEqual(rc, 0, prj_resp)
        prj_id = prj_resp["data"]["id"]
        rc_ibx, ibx_init = self.h.cli("inbox", "list", token=tok_l1)
        self.assertEqual(rc_ibx, 0, ibx_init)
        msg_ids = [m["id"] for m in ibx_init["data"]["items"]]
        if msg_ids:
            rc_ack, resp_ack = self.h.cli("inbox", "ack", *msg_ids, token=tok_l1)
            self.assertEqual(rc_ack, 0, resp_ack)

        conn = self.h.db()
        try:
            lead1_id = conn.execute("SELECT id FROM principals WHERE name = 'slead1';").fetchone()[0]
            asg_id = conn.execute(
                "SELECT id FROM lead_assignments WHERE project_id = ? AND ended_at IS NULL;", (prj_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        lead_run_tok, lookup, secret_hash = generate_run_token()
        scope = {
            "role": "lead",
            "project_id": prj_id,
            "lead_assignment_id": asg_id,
            "run_id": "run_sup_wait_1",
        }
        conn_rw = sqlite3.connect(os.path.join(self.h.home, "mesh.db"), timeout=10.0)
        try:
            conn_rw.execute(
                "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
                "VALUES (?, ?, 'run_token', ?, ?, ?, ?, ?);",
                (new_id("cred"), lead1_id, lookup, secret_hash, json.dumps(scope),
                 now_iso_utc(), now_iso_utc(time.time() + 600)),
            )
            conn_rw.commit()
        finally:
            conn_rw.close()

        wait_result: Dict[str, Any] = {}

        def _do_wait() -> None:
            wait_result["res"] = self.h.cli("inbox", "wait", "--timeout", "15s", token=lead_run_tok, timeout=25.0)

        th = threading.Thread(target=_do_wait, daemon=True)
        th.start()
        self.assertTrue(self.h.wait_for_active_waits(1, timeout=10.0), "inbox wait never became active")

        # set-lead to slead2 supersedes asg_id AND fans out project.lead_changed to origin slead1.
        rc_sl, resp_sl = self.h.cli("project", "set-lead", prj_id, "slead2", "--reason", "rotate", user=True)
        self.assertEqual(rc_sl, 0, resp_sl)

        th.join(10.0)
        self.assertFalse(th.is_alive(), "superseded inbox wait hung instead of stopping")
        rc_w, resp_w = wait_result["res"]
        self.assertEqual(rc_w, c.EXIT_STALE, resp_w)
        self.assertEqual(resp_w["error"]["code"], c.LEAD_SUPERSEDED, resp_w)

        conn = self.h.db()
        try:
            row = conn.execute(
                "SELECT status FROM outbox WHERE project_id = ? AND recipient_id = ? AND type = 'project.lead_changed';",
                (prj_id, lead1_id),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "pending")
        finally:
            conn.close()


class TestBatch7G3CredentialInRunAndWake(LeadTurnCase):
    """Finding 6 live test: security.credential_in_run wakes event tail and launch lead via lead_wake."""

    def test_f6_credential_in_run_routes_lead_wake_and_wakes_tail(self) -> None:
        create_agent(self.harness, "fw", adapter="fake", delivery="launch")
        rc, _ = self.harness.cli("config", "set", "lead.wake_debounce_s", "1", user=True)
        self.assertEqual(rc, 0)
        self._set_backoff([0, 0, 0])

        # Write grok's token outside workspaces so fake_agent steal_token hits CREDENTIAL_NOT_ALLOWED_IN_RUN.
        ext_tok = os.path.join(self.stubs_dir, "grok.tok")
        with open(ext_tok, "w", encoding="utf-8") as f:
            f.write(self.tok_grok + "\n")
        plat_perms.set_private_file_acl(ext_tok)

        prj_id = self._create_project(_fake_goal({"lead_actions": {"actions": []}}))
        self._wait_turn_closed(prj_id, count=1)

        cmd_tail, env_tail = cli_popen_argv(
            self.harness, "--user", "--passphrase-file", self.harness.passphrase_file,
            "event", "tail", "--since", "0", "--timeout", "15s",
        )
        tail_proc = subprocess.Popen(
            cmd_tail,
            env=env_tail,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(1.0)
            spec = {"steps": [{"steal_token": {"path": ext_tok, "args": ["whoami"]}}, {"succeed": {}}]}
            rc, tr = self.harness.cli(
                "task", "create", "--project", prj_id,
                "--title", "Cred in run",
                "--spec", _fake_goal(spec),
                "--assignee", "fw",
                user=True,
            )
            self.assertEqual(rc, 0, tr)

            # Wait for security.credential_in_run event + launch lead_wake outbox row.
            def _has_cred_wake() -> bool:
                conn = self.harness.db()
                try:
                    row = conn.execute(
                        "SELECT o.channel, p.name FROM outbox o JOIN principals p ON p.id = o.recipient_id "
                        "WHERE o.project_id = ? AND o.type = 'security.credential_in_run' AND p.name = 'flead';",
                        (prj_id,),
                    ).fetchone()
                    return row is not None and row["channel"] == "lead_wake"
                finally:
                    conn.close()

            self.assertTrue(self.harness.wait_for(_has_cred_wake, timeout=20.0, step=0.1))

            assert tail_proc.stdout is not None
            seen_types = set()
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and "auth.credential_in_run" not in seen_types:
                line = tail_proc.stdout.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                    seen_types.add(ev.get("type"))
                except json.JSONDecodeError:
                    pass
            self.assertIn("auth.credential_in_run", seen_types)
            # Ensure launched wake turn completes cleanly before teardown.
            self._wait_turn_closed(prj_id, count=2, timeout=30.0)
        finally:
            if tail_proc.poll() is None:
                tail_proc.terminate()
                try:
                    tail_proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    tail_proc.kill()
                    tail_proc.wait()
            if tail_proc.stdout:
                tail_proc.stdout.close()
            if tail_proc.stderr:
                tail_proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
