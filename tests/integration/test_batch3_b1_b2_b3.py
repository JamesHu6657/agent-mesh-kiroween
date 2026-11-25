"""Integration tests over live meshd IPC for Batch 3 items B1 (task.diff/project.diff),
B2 (chat.queue_edit/chat.queue_cancel/lead.interrupt+steer), and B3 (run.activity).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import mesh.constants as c
from mesh.client import MeshClient
from tests.helpers import MeshHarness


def _make_git_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "mesh-test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Mesh Test"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "app.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)
    cp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True)
    return cp.stdout.strip()


class TestBatch3B1B2B3Integration(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)
        self.client = MeshClient(self.harness.home, timeout=30.0)
        self._tmp_dirs: list[tempfile.TemporaryDirectory] = []

    def tearDown(self) -> None:
        self.harness.teardown()
        for td in self._tmp_dirs:
            td.cleanup()

    def _rpc_user(self, cmd: str, args: dict | None = None) -> dict:
        return self.client.send_request(
            {
                "v": 1,
                "cmd": cmd,
                "args": args or {},
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
            }
        )

    def _rpc_token(
        self,
        token: str,
        cmd: str,
        args: dict | None = None,
        *,
        relayed: bool = False,
    ) -> dict:
        req: dict = {
            "v": 1,
            "cmd": cmd,
            "args": args or {},
            "auth": {"type": "token", "token": token},
        }
        if relayed:
            req["relayed"] = True
            req["user_statement"] = "User asked me to do this"
        return self.client.send_request(req)

    def _create_agent(
        self,
        name: str,
        *,
        adapter: str = "fake",
        delivery: str = "inbox",
        can_lead: bool = False,
        capabilities: list[str] | None = None,
        default_profile: str = "edit",
        allowed_models: list[str] | None = None,
    ) -> str:
        res = self._rpc_user(
            "agent.create",
            {
                "name": name,
                "adapter": adapter,
                "delivery": delivery,
                "can_lead": can_lead,
                "max_concurrency": 2,
                "default_profile": default_profile,
                "max_unapproved_profile": "dev",
                "capabilities": capabilities if capabilities is not None else ["intake"],
                "allowed_models": allowed_models if allowed_models is not None else (["fake-model"] if delivery == "launch" else []),
            },
        )
        self.assertTrue(res.get("ok"), f"agent.create {name} failed: {res}")
        tok = self._rpc_user("agent.token.issue", {"name": name, "ttl": "1h"})
        self.assertTrue(tok.get("ok"), f"token issue failed: {tok}")
        return tok["data"]["token"]

    def _verify_audit(self) -> None:
        res = self._rpc_user("audit.verify")
        self.assertTrue(res.get("ok"), f"audit.verify failed: {res}")
        self.assertGreater(res["data"]["events_verified"], 0, f"audit chain empty: {res}")

    def test_b1_task_diff_and_project_diff_over_ipc_with_redaction_and_authz(self) -> None:
        td = tempfile.TemporaryDirectory(prefix="mh-b1-int-")
        self._tmp_dirs.append(td)
        repo_dir = Path(td.name) / "myrepo"
        base_sha = _make_git_repo(repo_dir)

        _lead_tok = self._create_agent("lead-diff", delivery="inbox", can_lead=True, default_profile="readonly")
        worker_tok = self._create_agent("worker-diff", delivery="inbox", can_lead=False, default_profile="edit")
        other_tok = self._create_agent("outsider-diff", delivery="inbox", can_lead=False, default_profile="edit")

        prj_res = self._rpc_user(
            "project.create",
            {
                "title": "Diff Demo",
                "goal": "Test read-only diffs",
                "lead": "lead-diff",
                "repo": str(repo_dir),
                "base_branch": "main",
                "test_command": "none",
                "criterion": ["Diff endpoints return bounded redacted output"],
            },
        )
        self.assertTrue(prj_res.get("ok"), prj_res)
        pid = prj_res["data"]["id"]

        tsk_res = self._rpc_user(
            "task.create",
            {
                "project": pid,
                "title": "Update greeting",
                "spec": "Edit app.py",
                "kind": "code",
            },
        )
        self.assertTrue(tsk_res.get("ok"), tsk_res)
        tid = tsk_res["data"]["id"]

        assign_res = self._rpc_user("task.assign", {"task_id": tid, "agent": "worker-diff"})
        self.assertTrue(assign_res.get("ok"), assign_res)

        start_res = self._rpc_token(worker_tok, "task.start", {"task_id": tid})
        self.assertTrue(start_res.get("ok"), start_res)
        fence = start_res["data"]["fence"]
        run_id = start_res["data"]["run_id"]
        ws_path = Path(start_res["data"]["workspace"])

        # Edit a tracked file with a secret and add an untracked file inside the task worktree
        secret = "sk-ant-api03-abcdef1234567890abcdef1234567890"
        (ws_path / "app.py").write_text(f"def greet():\n    return 'hello world {secret}'\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=ws_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "update greeting"], cwd=ws_path, check=True, capture_output=True)
        (ws_path / "scratch.txt").write_text("untracked note\n", encoding="utf-8")

        # 1. Direct user calls task.diff -> bounded, redacted, includes both committed and untracked
        tdiff = self._rpc_user("task.diff", {"task_id": tid})
        self.assertTrue(tdiff.get("ok"), tdiff)
        data = tdiff["data"]
        self.assertTrue(data["available"])
        self.assertTrue(data["dirty"])
        by_path = {f["path"]: f for f in data["files"]}
        self.assertIn("app.py", by_path)
        self.assertIn("scratch.txt", by_path)
        self.assertNotIn(secret, json.dumps(data))
        self.assertIn("[REDACTED", by_path["app.py"]["patch"])

        # P3-10: include_untracked rejects non-boolean strings like "false" and honors False
        bad_bool = self._rpc_user("task.diff", {"task_id": tid, "include_untracked": "false"})
        self.assertFalse(bad_bool.get("ok"))
        self.assertEqual(bad_bool["error"]["code"], c.INVALID_ARGUMENT)
        no_untracked = self._rpc_user("task.diff", {"task_id": tid, "include_untracked": False})
        self.assertTrue(no_untracked.get("ok"), no_untracked)
        self.assertNotIn("scratch.txt", {f["path"] for f in no_untracked["data"]["files"]})

        # 2. Outsider agent cannot read task.diff or project.diff
        for cmd, args in (("task.diff", {"task_id": tid}), ("project.diff", {"project_id": pid})):
            denied = self._rpc_token(other_tok, cmd, args)
            self.assertFalse(denied.get("ok"))
            self.assertEqual(denied["error"]["code"], c.SCOPE_MISMATCH)

        # 3. Clean scratch, submit, accept, merge -> project.diff shows integration branch vs main
        (ws_path / "scratch.txt").unlink()
        sub = self._rpc_token(worker_tok, "task.submit", {"task_id": tid, "run": run_id, "fence": fence, "summary": "done"})
        self.assertTrue(sub.get("ok"), sub)
        self.assertTrue(self._rpc_user("task.accept", {"task_id": tid}).get("ok"))
        mres = self._rpc_user("task.merge", {"task_id": tid})
        self.assertTrue(mres.get("ok"), mres)

        # Wait briefly for async merge to finish
        deadline = time.time() + 15.0
        while time.time() < deadline:
            tshow = self._rpc_user("task.show", {"task_id": tid})
            if tshow["data"]["status"] == "merged":
                break
            time.sleep(0.15)
        self.assertEqual(self._rpc_user("task.show", {"task_id": tid})["data"]["status"], "merged")

        pdiff = self._rpc_user("project.diff", {"project_id": pid})
        self.assertTrue(pdiff.get("ok"), pdiff)
        pdata = pdiff["data"]
        self.assertTrue(pdata["available"])
        self.assertEqual(pdata["commits_ahead"], 2)  # task commit + --no-ff merge commit
        self.assertEqual(pdata["stat"]["files_changed"], 1)
        self.assertEqual(pdata["files"][0]["path"], "app.py")
        # Base branch untouched
        main_after = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(main_after, base_sha)
        self._verify_audit()

    def test_b2_queue_edit_cancel_and_authz_and_consumed_protection(self) -> None:
        lead_tok = self._create_agent("lead-q", delivery="inbox", can_lead=True, default_profile="readonly")
        relay_tok = self._create_agent("relayer-q", delivery="inbox", can_lead=False, capabilities=["relay"])

        send1 = self._rpc_user("chat.send", {"title": "Queue Demo", "lead": "lead-q", "text": "Initial goal"})
        self.assertTrue(send1.get("ok"), send1)
        pid = send1["data"]["project_id"]
        msg1_id = send1["data"]["message"]["id"]

        # Unconsumed queued message appears in chat.list.queued_message_ids
        cl = self._rpc_user("chat.list", {"project_id": pid})
        self.assertTrue(cl.get("ok"), cl)
        self.assertIn(msg1_id, cl["data"]["queued_message_ids"])

        # Non-direct callers cannot edit or cancel queued messages
        for tok, relayed in ((lead_tok, False), (relay_tok, True)):
            r1 = self._rpc_token(tok, "chat.queue_edit", {"project_id": pid, "message_id": msg1_id, "text": "hijack"}, relayed=relayed)
            self.assertFalse(r1.get("ok"))
            r2 = self._rpc_token(tok, "chat.queue_cancel", {"project_id": pid, "message_id": msg1_id}, relayed=relayed)
            self.assertFalse(r2.get("ok"))

        # Edit unconsumed queued message -> updates chat_messages & outbox payload, records chat.edited
        ed = self._rpc_user("chat.queue_edit", {"project_id": pid, "message_id": msg1_id, "text": "Updated goal v2"})
        self.assertTrue(ed.get("ok"), ed)
        self.assertEqual(ed["data"]["message"]["body"], "Updated goal v2")
        self.assertEqual(ed["data"]["message"]["meta"]["edit_count"], 1)

        # Send a second queued message and cancel it -> outbox superseded, excluded from queued_message_ids
        send2 = self._rpc_user("chat.send", {"project_id": pid, "text": "Cancel me please"})
        self.assertTrue(send2.get("ok"), send2)
        msg2_id = send2["data"]["message"]["id"]

        can = self._rpc_user("chat.queue_cancel", {"project_id": pid, "message_id": msg2_id})
        self.assertTrue(can.get("ok"), can)
        self.assertTrue(can["data"]["cancelled"])
        self.assertTrue(can["data"]["message"]["meta"]["queued_cancelled"])

        # Double-cancel and edit-after-cancel refused
        self.assertEqual(
            self._rpc_user("chat.queue_cancel", {"project_id": pid, "message_id": msg2_id})["error"]["code"],
            c.INVALID_TRANSITION,
        )
        self.assertEqual(
            self._rpc_user("chat.queue_edit", {"project_id": pid, "message_id": msg2_id, "text": "too late"})["error"]["code"],
            c.INVALID_TRANSITION,
        )

        # Lead acks msg1's inbox message -> now consumed, so edit/cancel MUST fail
        inbox_res = self._rpc_token(lead_tok, "inbox.list", {})
        self.assertTrue(inbox_res.get("ok"), inbox_res)
        chat_obs = [m for m in inbox_res["data"]["items"] if m["type"] == "chat.message"]
        self.assertEqual(len(chat_obs), 1)
        self.assertEqual(chat_obs[0]["payload"]["body"], "Updated goal v2")
        ack_res = self._rpc_token(lead_tok, "inbox.ack", {"messages": [chat_obs[0]["id"]]})
        self.assertTrue(ack_res.get("ok"), ack_res)

        consumed_edit = self._rpc_user("chat.queue_edit", {"project_id": pid, "message_id": msg1_id, "text": "mutate consumed"})
        self.assertFalse(consumed_edit.get("ok"))
        self.assertEqual(consumed_edit["error"]["code"], c.INVALID_TRANSITION)
        self.assertEqual(consumed_edit["error"]["details"]["reason"], "message_already_consumed")

        consumed_cancel = self._rpc_user("chat.queue_cancel", {"project_id": pid, "message_id": msg1_id})
        self.assertFalse(consumed_cancel.get("ok"))
        self.assertEqual(consumed_cancel["error"]["code"], c.INVALID_TRANSITION)
        self._verify_audit()

    def test_b2_lead_interrupt_and_steer_live_turn_and_b3_run_activity(self) -> None:
        # Configure a launch-mode fake Lead that blocks on wait_release so we can interrupt & steer it live
        td = tempfile.TemporaryDirectory(prefix="mh-b2-int-")
        self._tmp_dirs.append(td)
        release_path = Path(td.name) / "release.flag"

        _lead_tok = self._create_agent(
            "lead-launch",
            adapter="fake",
            delivery="launch",
            can_lead=True,
            default_profile="readonly",
            allowed_models=["fake-model"],
        )
        fake_spec = json.dumps({"wait_release": {"path": str(release_path), "timeout_s": 30}})
        send_res = self._rpc_user(
            "chat.send",
            {
                "title": "Steer Demo",
                "lead": "lead-launch",
                "text": f"Start slow turn\nFAKE {fake_spec}",
            },
        )
        self.assertTrue(send_res.get("ok"), send_res)
        pid = send_res["data"]["project_id"]

        # Wait until the launch lead_turn run is active
        first_run_id = None
        deadline = time.time() + 15.0
        while time.time() < deadline:
            rlist = self._rpc_user("run.list", {})
            active = [r for r in rlist["data"]["runs"] if r["project_id"] == pid and r["kind"] == "lead_turn" and r["status"] in ("starting", "running")]
            if active:
                first_run_id = active[0]["id"]
                break
            time.sleep(0.15)
        self.assertIsNotNone(first_run_id, "Expected lead_turn run to start")

        # Interrupt the running lead turn AND steer with a new message (P3-7: secret in reason is redacted consistently)
        reason_secret = "sk-ant-api03-reasonsecret1234567890abcdef123456"
        int_res = self._rpc_user(
            "lead.interrupt",
            {
                "project_id": pid,
                "run_id": first_run_id,
                "steer_text": "Change course immediately",
                "reason": f"stop due to {reason_secret}",
            },
        )
        self.assertTrue(int_res.get("ok"), int_res)
        self.assertTrue(int_res["data"]["interrupted"])
        self.assertEqual(int_res["data"]["run_id"], first_run_id)
        self.assertEqual(int_res["data"]["message"]["body"], "Change course immediately")
        self.assertNotIn(reason_secret, int_res["data"]["reason"])
        self.assertIn("[REDACTED", int_res["data"]["reason"])

        # Verify event row reason & payload_json are also redacted (P3-7)
        ev_conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        ev_conn.row_factory = sqlite3.Row
        try:
            int_ev = ev_conn.execute(
                "SELECT reason, payload_json FROM events WHERE type = 'lead.interrupted' AND run_id = ?;",
                (first_run_id,),
            ).fetchone()
            self.assertIsNotNone(int_ev)
            self.assertNotIn(reason_secret, int_ev["reason"] or "")
            self.assertNotIn(reason_secret, int_ev["payload_json"] or "")
            self.assertIn("[REDACTED", int_ev["reason"] or "")
            self.assertIn("[REDACTED", int_ev["payload_json"] or "")
        finally:
            ev_conn.close()

        # Wait for the first run to close as killed/user_interrupted, then release the steered follow-up turn
        second_run = None
        deadline = time.time() + 20.0
        while time.time() < deadline:
            rlist = self._rpc_user("run.list", {})
            runs = [r for r in rlist["data"]["runs"] if r["project_id"] == pid and r["kind"] == "lead_turn"]
            r1 = next((r for r in runs if r["id"] == first_run_id), None)
            if r1 and r1["status"] == "killed" and not release_path.exists():
                release_path.write_text("go\n", encoding="utf-8")
            r2 = next((r for r in runs if r["id"] != first_run_id and r["status"] == "exited"), None)
            if r1 and r1["status"] == "killed" and r2 is not None:
                second_run = r2
                break
            time.sleep(0.15)

        r1_final = self._rpc_user("run.show", {"run_id": first_run_id})["data"]
        self.assertEqual(r1_final["status"], "killed")
        self.assertEqual(r1_final["end_reason"], "user_interrupted")
        self.assertIsNotNone(second_run, "Expected steered follow-up lead turn to execute and finish")

        # B3: Seed structured Claude/Codex JSONL + secret into second_run's stdout.log and verify run.activity RPC
        run_dir = Path(second_run["run_dir"])
        secret = "sk-proj-activity9876543210abcdef9876543210"
        (run_dir / "stdout.log").write_text(
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "u1", "name": "Read", "input": {"file_path": "mesh/core/diffs.py"}},
                        {"type": "tool_use", "id": "u2", "name": "Bash", "input": {"command": f"echo {secret}"}},
                    ]
                },
            }) + "\n",
            encoding="utf-8",
        )
        act_res = self._rpc_user("run.activity", {"run_id": second_run["id"]})
        self.assertTrue(act_res.get("ok"), act_res)
        adata = act_res["data"]
        self.assertTrue(adata["available"])
        self.assertEqual(adata["source"], "structured")
        self.assertEqual(adata["total_entries"], 2)
        self.assertEqual(adata["counts"]["read"], 1)
        self.assertEqual(adata["counts"]["command"], 1)
        self.assertNotIn(secret, json.dumps(adata))
        self.assertIn("[REDACTED", json.dumps(adata))

        # Verify lead.interrupt refuses a non-lead_turn run (e.g. a task run)
        # Check no dangling leadturn lease
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"))
        try:
            lease = conn.execute("SELECT * FROM leases WHERE key = ?;", (f"leadturn:{pid}",)).fetchone()
            self.assertIsNone(lease, "leadturn lease must be released cleanly after interrupt + steer")
        finally:
            conn.close()

        self._verify_audit()

    def test_p3_5_audit_verify_catches_chat_cancel_and_dangling_edit_tampering(self) -> None:
        """P3-5: audit.verify replays chat.cancelled into meta_json/outbox and flags dangling chat.edited."""
        from mesh.core.auth import verify_user_passphrase
        from mesh.core.events import record_event

        _lead_tok = self._create_agent("lead-aud", delivery="inbox", can_lead=True, default_profile="readonly")
        s1 = self._rpc_user("chat.send", {"title": "Audit Replay", "lead": "lead-aud", "text": "Message 1"})
        self.assertTrue(s1.get("ok"), s1)
        pid = s1["data"]["project_id"]
        msg1_id = s1["data"]["message"]["id"]

        s2 = self._rpc_user("chat.send", {"project_id": pid, "text": "Message 2 to cancel"})
        self.assertTrue(s2.get("ok"), s2)
        msg2_id = s2["data"]["message"]["id"]

        can = self._rpc_user("chat.queue_cancel", {"project_id": pid, "message_id": msg2_id})
        self.assertTrue(can.get("ok"), can)
        self._verify_audit()

        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # 1. Tamper msg1 meta_json.queued_cancelled = true without chat.cancelled event -> must FAIL
            orig_m1_meta = conn.execute("SELECT meta_json FROM chat_messages WHERE id = ?;", (msg1_id,)).fetchone()["meta_json"]
            conn.execute("UPDATE chat_messages SET meta_json = ? WHERE id = ?;", (json.dumps({"queued_cancelled": True}), msg1_id))
            conn.commit()
            res1 = self._rpc_user("audit.verify")
            self.assertFalse(res1.get("ok"), "Tampered queued_cancelled without event must fail audit.verify")
            self.assertEqual(res1["error"]["code"], c.AUDIT_MISMATCH)
            conn.execute("UPDATE chat_messages SET meta_json = ? WHERE id = ?;", (orig_m1_meta, msg1_id))
            conn.commit()
            self._verify_audit()

            # 2. Tamper msg1 outbox.status = 'superseded' without chat.cancelled -> must FAIL
            m1_seq = conn.execute("SELECT event_seq FROM chat_messages WHERE id = ?;", (msg1_id,)).fetchone()["event_seq"]
            conn.execute("UPDATE outbox SET status = 'superseded' WHERE project_id = ? AND event_seq = ?;", (pid, m1_seq))
            conn.commit()
            res2 = self._rpc_user("audit.verify")
            self.assertFalse(res2.get("ok"), "Tampered outbox status='superseded' without event must fail audit.verify")
            self.assertEqual(res2["error"]["code"], c.AUDIT_MISMATCH)
            conn.execute("UPDATE outbox SET status = 'pending' WHERE project_id = ? AND event_seq = ?;", (pid, m1_seq))
            conn.commit()
            self._verify_audit()

            # 3. Tamper cancelled msg2 meta_json back to {} while chat.cancelled event exists -> must FAIL
            orig_m2_meta = conn.execute("SELECT meta_json FROM chat_messages WHERE id = ?;", (msg2_id,)).fetchone()["meta_json"]
            conn.execute("UPDATE chat_messages SET meta_json = '{}' WHERE id = ?;", (msg2_id,))
            conn.commit()
            res3 = self._rpc_user("audit.verify")
            self.assertFalse(res3.get("ok"), "Clearing queued_cancelled on a cancelled message must fail audit.verify")
            self.assertEqual(res3["error"]["code"], c.AUDIT_MISMATCH)
            conn.execute("UPDATE chat_messages SET meta_json = ? WHERE id = ?;", (orig_m2_meta, msg2_id))
            conn.commit()
            self._verify_audit()

            # 4. Append a signed direct-user chat.edited event referencing a nonexistent message_id -> must FAIL
            cred = conn.execute("SELECT salt, kdf_params, secret_hash FROM credentials WHERE kind = 'passphrase' LIMIT 1;").fetchone()
            ok_pass, k_user = verify_user_passphrase(
                self.harness.passphrase,
                cred["salt"],
                cred["kdf_params"],
                cred["secret_hash"],
                testing_mode=True,
            )
            self.assertTrue(ok_pass and k_user is not None)
            user_row = conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;").fetchone()
            record_event(
                conn=conn,
                type_="chat.edited",
                actor_id=user_row["id"],
                actor_kind="user",
                via="direct",
                project_id=pid,
                payload={"message_id": "cmsg_nonexistent_ghost", "project_id": pid, "body": "ghost"},
                k_user=k_user,
            )
            conn.commit()
            res4 = self._rpc_user("audit.verify")
            self.assertFalse(res4.get("ok"), "Dangling chat.edited event must fail audit.verify")
            self.assertEqual(res4["error"]["code"], c.AUDIT_MISMATCH)
        finally:
            conn.close()

    def test_b2_relayed_and_approval_bound_messages_excluded_from_queue_edit_cancel(self) -> None:
        """PR #69/#70 contract sync: relayed chat messages and approval/plan cards are never mutable via queue edit/cancel."""
        self._create_agent("lead-rel", adapter="none", delivery="inbox", can_lead=True)
        self._create_agent("worker-rel", adapter="none", delivery="inbox", can_lead=False)
        self._create_agent("relayer-bot", adapter="none", delivery="inbox", capabilities=["relay", "intake"])
        self.assertTrue(self._rpc_user("config.set", {"key": "lead.default_agent", "value": "lead-rel"}).get("ok"))

        tok_rel = self._rpc_user("agent.token.issue", {"name": "relayer-bot", "ttl": "1h"})["data"]["token"]
        tok_lead = self._rpc_user("agent.token.issue", {"name": "lead-rel", "ttl": "1h"})["data"]["token"]

        # Relayer originates the project (required by #69 R2 relayed chat.send scope)
        intake = self._rpc_token(
            tok_rel,
            "intake.submit",
            {
                "title": "Relayed Queue Guard",
                "goal": "Verify relayed chat immutability",
                "lead": "lead-rel",
                "plan_gate_mode": "require_approval",
                "criteria": ["Relayed messages remain immutable"],
            },
            relayed=True,
        )
        self.assertTrue(intake.get("ok"), intake)
        pid = intake["data"]["id"]

        # Direct user sends a normal queued message + relayer sends a relayed chat message
        dir_send = self._rpc_user("chat.send", {"project_id": pid, "text": "Direct queued text"})
        self.assertTrue(dir_send.get("ok"), dir_send)
        dir_mid = dir_send["data"]["message"]["id"]

        rel_send = self._rpc_token(
            tok_rel,
            "chat.send",
            {"project_id": pid, "text": "Relayed user text"},
            relayed=True,
        )
        self.assertTrue(rel_send.get("ok"), rel_send)
        rel_msg = rel_send["data"]["message"]
        self.assertTrue(rel_msg["relayed"])
        self.assertEqual(rel_msg["relayed_by_name"], "relayer-bot")
        rel_mid = rel_msg["id"]

        # Lead proposes a plan -> emits plan_card bound to plan_id & approval_id
        prop = self._rpc_token(
            tok_lead,
            "plan.propose",
            {
                "project_id": pid,
                "title": "Guarded Plan",
                "summary": "Check approval-bound chat record protection",
                "steps": [{"key": "s1", "title": "Step 1", "spec": "Do step", "assignee": "worker-rel"}],
            },
        )
        self.assertTrue(prop.get("ok"), prop)

        clist = self._rpc_user("chat.list", {"project_id": pid})
        self.assertTrue(clist.get("ok"), clist)
        qids = clist["data"]["queued_message_ids"]
        self.assertIn(dir_mid, qids)
        self.assertNotIn(rel_mid, qids)

        plan_card_msgs = [it for it in clist["data"]["items"] if it["kind"] == "plan_card"]
        self.assertEqual(len(plan_card_msgs), 1)
        plan_card_mid = plan_card_msgs[0]["id"]
        self.assertNotIn(plan_card_mid, qids)

        # Attempting queue_edit or queue_cancel on relayed message must fail cleanly
        for cmd, extra in (("chat.queue_edit", {"text": "Tampered relayed"}), ("chat.queue_cancel", {})):
            r = self._rpc_user(cmd, {"project_id": pid, "message_id": rel_mid, **extra})
            self.assertFalse(r.get("ok"), r)
            self.assertEqual(r["error"]["code"], c.INVALID_TRANSITION)
            self.assertEqual(r["error"]["details"]["reason"], "relayed_message_immutable")

        # Attempting queue_edit or queue_cancel on approval/plan-bound card message must fail
        for cmd, extra in (("chat.queue_edit", {"text": "Tampered card"}), ("chat.queue_cancel", {})):
            r = self._rpc_user(cmd, {"project_id": pid, "message_id": plan_card_mid, **extra})
            self.assertFalse(r.get("ok"), r)
            self.assertEqual(r["error"]["code"], c.INVALID_TRANSITION)

        # Audit verify remains completely clean
        self._verify_audit()

    def test_round3_p2_4_project_diff_authz_before_existence_and_p3_2_chat_list_superseded(self) -> None:
        """Round 3 P2-4 & P3-2: project.diff authorizes before existence lookup; chat.list excludes superseded outbox rows."""
        from datetime import datetime, timedelta, timezone
        from mesh.core.auth import generate_run_token
        from mesh.core.clock import now_iso_utc
        from mesh.core.ids import generate_id

        _tok_lead1 = self._create_agent("lead-r3-1", delivery="inbox", can_lead=True, default_profile="readonly")
        _tok_lead2 = self._create_agent("lead-r3-2", delivery="inbox", can_lead=True, default_profile="readonly")
        tok_foreign = self._create_agent("agent-r3-foreign", delivery="inbox", can_lead=False, default_profile="readonly")

        s1 = self._rpc_user("chat.send", {"title": "R3 Authz & Superseded", "lead": "lead-r3-1", "text": "Initial queued msg"})
        self.assertTrue(s1.get("ok"), s1)
        pid = s1["data"]["project_id"]
        msg_id = s1["data"]["message"]["id"]

        # Create a second project + mint a scoped lead run_token bound to pid2
        s2 = self._rpc_user("chat.send", {"title": "R3 Scoped Project", "lead": "lead-r3-1", "text": "Other project"})
        self.assertTrue(s2.get("ok"), s2)
        pid2 = s2["data"]["project_id"]

        db_path = os.path.join(self.harness.home, "mesh.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            lead_row = conn.execute("SELECT id FROM principals WHERE name = 'lead-r3-1';").fetchone()
            self.assertIsNotNone(lead_row)
            la_row = conn.execute(
                "SELECT id FROM lead_assignments WHERE project_id = ? AND ended_at IS NULL;",
                (pid2,),
            ).fetchone()
            self.assertIsNotNone(la_row)
            run_id = generate_id("run")
            now = now_iso_utc()
            exp = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
            conn.execute(
                """INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, profile, status, boot_id, run_dir, started_at)
                   VALUES (?, 'lead_turn', ?, ?, 1, 'fake', 'readonly', 'running', 'boot_r3', ?, ?);""",
                (run_id, pid2, lead_row["id"], os.path.join(self.harness.home, "runs", run_id), now),
            )
            scoped_tok, lookup, scoped_hash = generate_run_token()
            scoped_cred_id = generate_id("cred")
            conn.execute(
                """INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at)
                   VALUES (?, ?, 'run_token', ?, ?, ?, ?, ?);""",
                (
                    scoped_cred_id,
                    lead_row["id"],
                    lookup,
                    scoped_hash,
                    json.dumps({"role": "lead", "project_id": pid2, "run_id": run_id, "lead_assignment_id": la_row["id"]}),
                    now,
                    exp,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        nonexistent_pid = "prj_does_not_exist_r3"

        # P2-4: Foreign agent token receives SCOPE_MISMATCH (rc 4) for BOTH existing foreign and nonexistent project_id
        for target_pid in (pid, nonexistent_pid):
            r_for = self._rpc_token(tok_foreign, "project.diff", {"project_id": target_pid})
            self.assertFalse(r_for.get("ok"), r_for)
            self.assertEqual(r_for["error"]["code"], c.SCOPE_MISMATCH, f"foreign token project.diff({target_pid}): {r_for}")
            self.assertEqual(r_for["exit_code"], c.EXIT_FORBIDDEN)

        # P2-4: Scoped lead_run token bound to pid2 receives SCOPE_MISMATCH (rc 4) for BOTH pid and nonexistent_pid
        for target_pid in (pid, nonexistent_pid):
            r_sc = self._rpc_token(scoped_tok, "project.diff", {"project_id": target_pid})
            self.assertFalse(r_sc.get("ok"), r_sc)
            self.assertEqual(r_sc["error"]["code"], c.SCOPE_MISMATCH, f"scoped token project.diff({target_pid}): {r_sc}")
            self.assertEqual(r_sc["exit_code"], c.EXIT_FORBIDDEN)

        # Remove synthetic fixture rows before audit.verify
        conn_clean = sqlite3.connect(db_path)
        try:
            conn_clean.execute("DELETE FROM credentials WHERE id = ?;", (scoped_cred_id,))
            conn_clean.execute("DELETE FROM runs WHERE id = ?;", (run_id,))
            conn_clean.commit()
        finally:
            conn_clean.close()

        # Direct user still receives NOT_FOUND (rc 3) for nonexistent project_id
        r_user_nf = self._rpc_user("project.diff", {"project_id": nonexistent_pid})
        self.assertFalse(r_user_nf.get("ok"), r_user_nf)
        self.assertEqual(r_user_nf["error"]["code"], c.NOT_FOUND)
        self.assertEqual(r_user_nf["exit_code"], c.EXIT_NOT_FOUND)

        # P3-2: Before project.set_lead, msg_id is in queued_message_ids
        cl_before = self._rpc_user("chat.list", {"project_id": pid})
        self.assertTrue(cl_before.get("ok"), cl_before)
        self.assertIn(msg_id, cl_before["data"]["queued_message_ids"])

        # Switch project lead -> supersedes old lead's outbox row and creates a new pending row for lead-r3-2
        set_lead = self._rpc_user("project.set_lead", {"project_id": pid, "agent": "lead-r3-2", "reason": "switch lead"})
        self.assertTrue(set_lead.get("ok"), set_lead)

        # chat.list MUST NOT advertise msg_id in queued_message_ids because _load_mutable_queued_message rejects it
        cl_after = self._rpc_user("chat.list", {"project_id": pid})
        self.assertTrue(cl_after.get("ok"), cl_after)
        self.assertNotIn(msg_id, cl_after["data"]["queued_message_ids"])

        # Confirm enforcement matches advertised mutability
        for cmd, extra in (("chat.queue_edit", {"text": "After lead switch"}), ("chat.queue_cancel", {})):
            r_mut = self._rpc_user(cmd, {"project_id": pid, "message_id": msg_id, **extra})
            self.assertFalse(r_mut.get("ok"), r_mut)
            self.assertEqual(r_mut["error"]["code"], c.INVALID_TRANSITION)
            self.assertEqual(r_mut["error"]["details"]["reason"], "message_already_consumed")

        self._verify_audit()


if __name__ == "__main__":
    unittest.main()
