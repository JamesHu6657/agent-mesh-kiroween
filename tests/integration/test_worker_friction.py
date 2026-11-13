"""Remote-worker and onboarding friction, end to end against a real meshd:
lease timing in task start/show, task keepalive, per-agent ack window, deliverable
messages, config set bare strings, the lead roster, --no-intake and agent onboard."""

import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness, serial
from tests.integration.test_execution_inbox import create_agent
from tests.stubs import create_test_stubs


class TestWorkerFriction(unittest.TestCase):
    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tok_plain = create_agent(self.harness, "plain")
        rc, resp = self.harness.cli("intake", "submit", "--title", "PRJ", "--goal", "G", "--criterion", "C1",
                                    "--lead", "lead1", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]
        self.tokdir = tempfile.mkdtemp(prefix="mh-tok-")
        os.chmod(self.tokdir, 0o700)

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)
        shutil.rmtree(self.tokdir, ignore_errors=True)

    # helpers
    def onboard(self, name, *extra):
        path = os.path.join(self.tokdir, f"{name}.token")
        rc, resp = self.harness.cli("agent", "onboard", name, "--out", path, *extra, user=True)
        self.assertEqual(rc, 0, resp)
        return path, resp

    def cli_tf(self, tf, *args, timeout=10.0):
        return self.harness.cli(*args, "--token-file", tf, timeout=timeout)

    def new_task(self, assignee, title="T"):
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", title,
                                    "--spec", "S", "--assignee", assignee, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def start(self, tf, tsk):
        rc, resp = self.cli_tf(tf, "task", "wait", tsk, "--until", "state:queued", "--timeout", "10s")
        self.assertEqual(rc, 0, resp)
        rc, resp = self.cli_tf(tf, "task", "start", tsk)
        self.assertEqual(rc, 0, resp)
        return resp["data"]

    def keepalive(self, tf, tsk, run, fence, *extra):
        cmd = [self.harness.bin_mesh, "--home", self.harness.home, "task", "keepalive", tsk,
               "--run", run, "--fence", str(fence), "--token-file", tf, *extra]
        return subprocess.Popen(cmd, env=self.harness.get_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    # 8. onboard
    def test_onboard_writes_0600_token_file_and_cheat_sheet(self) -> None:
        path, resp = self.onboard("w1", "--ack-timeout", "30m")
        st = os.stat(path)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertNotIn("mesh_at_", json.dumps(resp))  # the token never reaches stdout
        d = resp["data"]
        self.assertEqual(d["token_file"], path)
        self.assertEqual(d["agent"]["delivery"], "inbox")
        self.assertEqual(d["agent"]["adapter"], "none")
        self.assertEqual(d["agent"]["max_concurrency"], 1)
        self.assertEqual(d["agent"]["capabilities"], ["intake"])
        self.assertEqual(d["agent"]["ack_timeout_s"], 1800)
        for needle in (f"--token-file {path}", "task keepalive <tsk>", "task submit <tsk>", "within 1800s"):
            self.assertIn(needle, d["cheat_sheet"])
        rc, who = self.cli_tf(path, "whoami")
        self.assertEqual(rc, 0, who)
        self.assertEqual(who["data"]["name"], "w1")
        # text mode prints the cheat-sheet readably
        rc_t = subprocess.run([self.harness.bin_mesh, "--home", self.harness.home, "--format", "text",
                               "agent", "onboard", "w2", "--out", os.path.join(self.tokdir, "w2.token"),
                               "--user", "--passphrase-file", self.harness.passphrase_file],
                              env=self.harness.get_env(), capture_output=True, text=True, timeout=20)
        self.assertEqual(rc_t.returncode, 0, rc_t.stdout + rc_t.stderr)
        self.assertIn("Worker cheat-sheet for w2", rc_t.stdout)
        self.assertIn("task keepalive <tsk>", rc_t.stdout)

    def test_onboard_refuses_existing_file_before_creating_agent(self) -> None:
        path = os.path.join(self.tokdir, "w1.token")
        with open(path, "w") as f:
            f.write("keep")
        rc, resp = self.harness.cli("agent", "onboard", "w1", "--out", path, user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertIn("--force", resp["error"]["message"])
        rc, resp = self.harness.cli("agent", "show", "w1", user=True)
        self.assertEqual(rc, c.EXIT_NOT_FOUND, resp)
        with open(path) as f:
            self.assertEqual(f.read(), "keep")
        # Existing agent name: nothing is written.
        rc, resp = self.harness.cli("agent", "onboard", "plain", "--out", os.path.join(self.tokdir, "p.token"), user=True)
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tokdir, "p.token")))

    def test_onboard_needs_the_user(self) -> None:
        rc, resp = self.harness.cli("agent", "onboard", "w9", "--out", os.path.join(self.tokdir, "w9.token"),
                                    token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        self.assertFalse(os.path.exists(os.path.join(self.tokdir, "w9.token")))

    # 1. lease timing
    def test_start_and_show_report_lease_timing(self) -> None:
        tf, _ = self.onboard("w1")
        tsk = self.new_task("w1")
        d = self.start(tf, tsk)
        self.assertEqual(d["lease_ttl_s"], 120)
        self.assertEqual(d["heartbeat_every_s"], 40)
        self.assertTrue(d["lease_expires_at"])
        self.assertIn("timeout_s", d)
        self.assertIsNone(d["deadline_at"])
        self.assertFalse(d["deadline_enforced"])
        self.assertIn(f"mesh task keepalive {tsk} --run {d['run_id']} --fence {d['fence']}", d["keepalive"])
        rc, sh = self.cli_tf(tf, "task", "show", tsk)
        self.assertEqual(rc, 0, sh)
        lr = sh["data"]["live_run"]
        self.assertEqual(lr["run_id"], d["run_id"])
        self.assertEqual(lr["fence"], d["fence"])
        self.assertEqual(lr["heartbeat_every_s"], 40)
        rc, hb = self.cli_tf(tf, "task", "heartbeat", tsk, "--run", d["run_id"], "--fence", str(d["fence"]))
        self.assertEqual(rc, 0, hb)
        rc, sh2 = self.cli_tf(tf, "task", "show", tsk)
        self.assertGreaterEqual(sh2["data"]["live_run"]["lease_expires_at"], lr["lease_expires_at"])
        # Another agent (the lead) does not get the live run block.
        rc, shl = self.harness.cli("task", "show", tsk, token=self.tok_lead1)
        self.assertEqual(rc, 0)
        self.assertNotIn("live_run", shl["data"])
        # text mode renders the nested block
        rc_t = subprocess.run([self.harness.bin_mesh, "--home", self.harness.home, "--format", "text", "task",
                               "show", tsk, "--token-file", tf], env=self.harness.get_env(),
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(rc_t.returncode, 0, rc_t.stderr)
        self.assertIn("heartbeat_every_s", rc_t.stdout)

    # 2. keepalive
    @serial
    def test_keepalive_holds_lease_and_exits_0_after_submit(self) -> None:
        rc, _ = self.harness.cli("config", "set", "lease.ttl_s", "3", user=True)
        self.assertEqual(rc, 0)
        tf, _ = self.onboard("w1")
        tsk = self.new_task("w1")
        d = self.start(tf, tsk)
        self.assertEqual(d["heartbeat_every_s"], 1)
        proc = self.keepalive(tf, tsk, d["run_id"], d["fence"])
        try:
            time.sleep(6)  # twice the TTL: without heartbeats the lease would be gone
            self.assertIsNone(proc.poll(), proc.stderr.read() if proc.poll() is not None else "")
            rc, sh = self.cli_tf(tf, "task", "show", tsk)
            self.assertEqual(sh["data"]["status"], "running")
            dl = os.path.join(self.tokdir, "d.json")
            with open(dl, "w") as f:
                json.dump({"tests": "pass"}, f)
            rc, sub = self.cli_tf(tf, "task", "submit", tsk, "--run", d["run_id"], "--fence", str(d["fence"]),
                                  "--summary", "done", "--deliverable", dl)
            self.assertEqual(rc, 0, sub)
            out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, out + err)
        res = json.loads(out)["data"]
        self.assertEqual(res["ended"], "run_ended")
        self.assertEqual(res["task_status"], "submitted")
        self.assertGreaterEqual(res["beats"], 3)
        self.assertIn("heartbeat", err)

    @serial
    def test_keepalive_exit_9_when_lease_lost(self) -> None:
        rc, _ = self.harness.cli("config", "set", "lease.ttl_s", "1", user=True)
        self.assertEqual(rc, 0)
        tf, _ = self.onboard("w1")
        tsk = self.new_task("w1")
        d = self.start(tf, tsk)
        rc, resp = self.harness.cli("task", "wait", tsk, "--until", "state:failed", "--timeout", "15s",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        proc = self.keepalive(tf, tsk, d["run_id"], d["fence"])
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, c.EXIT_STALE, out + err)
        env = json.loads(out)
        self.assertEqual(env["error"]["code"], c.LEASE_EXPIRED)
        self.assertIn("expired", env["error"]["message"])

    def test_keepalive_wrong_fence_is_exit_9(self) -> None:
        tf, _ = self.onboard("w1")
        tsk = self.new_task("w1")
        d = self.start(tf, tsk)
        proc = self.keepalive(tf, tsk, d["run_id"], d["fence"] + 5)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, c.EXIT_STALE, out + err)
        self.assertEqual(json.loads(out)["error"]["code"], c.FENCE_MISMATCH)

    def test_keepalive_sigterm_stops_cleanly(self) -> None:
        tf, _ = self.onboard("w1")
        tsk = self.new_task("w1")
        d = self.start(tf, tsk)
        proc = self.keepalive(tf, tsk, d["run_id"], d["fence"], "--every", "1")
        try:
            time.sleep(2.5)
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual(json.loads(out)["data"]["ended"], "stopped")

    # 3. per-agent ack window
    @serial
    def test_per_agent_ack_timeout_overrides_global(self) -> None:
        rc, _ = self.harness.cli("config", "set", "inbox.ack_timeout_s", "1", user=True)
        self.assertEqual(rc, 0)
        self.onboard("slow", "--ack-timeout", "1h")
        self.onboard("fast")
        t_slow = self.new_task("slow", "slow")
        t_fast = self.new_task("fast", "fast")
        rc, resp = self.harness.cli("task", "wait", t_fast, "--until", "state:failed", "--timeout", "15s",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        rc, sh = self.harness.cli("task", "show", t_fast, token=self.tok_lead1)
        fail = sh["data"]["failure"]
        self.assertEqual(fail["reason"], "no_response")
        det = fail["details"]
        self.assertEqual(det["setting"], "inbox.ack_timeout_s")
        self.assertEqual(det["timeout_s"], 1)
        self.assertIn(f"mesh task retry {t_fast}", det["hint"])
        self.assertIn("mesh agent update fast --ack-timeout", det["hint"])
        # The lead's notification carries the same hint.
        rc, inbox = self.harness.cli("inbox", "list", token=self.tok_lead1)
        msgs = [m for m in inbox["data"]["items"] if m["payload"].get("task_id") == t_fast and m["type"] == "task.failed"]
        self.assertEqual(len(msgs), 1, inbox)
        self.assertIn("ack_timeout", json.dumps(msgs[0]))
        # slow is still waiting well past the global 1 s window.
        time.sleep(2)
        rc, sh = self.harness.cli("task", "show", t_slow, token=self.tok_lead1)
        self.assertEqual(sh["data"]["status"], "queued", sh)
        rc, ag = self.harness.cli("agent", "show", "slow", user=True)
        self.assertEqual((ag["data"]["ack_timeout_s"], ag["data"]["ack_timeout_source"]), (3600, "agent"))
        # Back to the global 1 s window: new offers get it, but the task already offered
        # keeps the 1 h window it was offered with (no retroactive no_response).
        rc, up = self.harness.cli("agent", "update", "slow", "--ack-timeout", "default", user=True)
        self.assertEqual(rc, 0, up)
        self.assertEqual(up["data"]["ack_timeout_source"], "inbox.ack_timeout_s")
        self.assertEqual([k["task_id"] for k in up["data"]["offers_keeping_window"]], [t_slow])
        self.assertTrue(up["data"]["offers_keeping_window"][0]["kept"])
        self.assertTrue(any(t_slow in w and "keep the ack window" in w for w in up["warnings"]), up)
        time.sleep(2.5)
        rc, sh = self.harness.cli("task", "show", t_slow, token=self.tok_lead1)
        self.assertEqual(sh["data"]["status"], "queued", sh)
        t_new = self.new_task("slow", "new offer")
        rc, resp = self.harness.cli("task", "wait", t_new, "--until", "state:failed", "--timeout", "15s",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)

    def test_ack_timeout_validation(self) -> None:
        rc, resp = self.harness.cli("agent", "create", "--name", "lx", "--adapter", "fake", "--delivery", "launch",
                                    "--allowed-model", "fake-model", "--default-model", "fake-model",
                                    "--max-concurrency", "1", "--ack-timeout", "1h", user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertIn("inbox", resp["error"]["message"])
        rc, resp = self.harness.cli("agent", "update", "plain", "--ack-timeout", "soon", user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertIn("--ack-timeout", resp["error"]["message"])

    # 4. deliverable
    def test_submit_missing_deliverable_file_message(self) -> None:
        tf, _ = self.onboard("w1")
        tsk = self.new_task("w1")
        d = self.start(tf, tsk)
        rc, resp = self.cli_tf(tf, "task", "submit", tsk, "--run", d["run_id"], "--fence", str(d["fence"]),
                               "--summary", "x", "--deliverable", "out/result.json")
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertIn("file not found: out/result.json (and not valid inline JSON)", resp["error"]["message"])
        rc, sh = self.cli_tf(tf, "task", "show", tsk)
        self.assertEqual(sh["data"]["status"], "running")

    # 5. config set
    def test_config_set_bare_string_and_quoted_hint(self) -> None:
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", "lead1", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("config", "get", "lead.default_agent", user=True)
        self.assertEqual(resp["data"]["value"], "lead1")
        rc, resp = self.harness.cli("config", "set", "lease.ttl_s", "two-minutes", user=True)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertIn("'\"two-minutes\"'", resp["error"]["message"])

    # 6. roster
    def test_lead_gets_read_only_roster(self) -> None:
        self.onboard("w1", "--ack-timeout", "2h")
        rc, resp = self.harness.cli("agent", "list", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        items = resp["data"]["items"]
        names = {a["name"] for a in items}
        self.assertTrue({"lead1", "grok", "plain", "w1"} <= names)
        blob = json.dumps(resp)
        for secret_field in ("options", "binary", "credential", "token"):
            self.assertNotIn(f'"{secret_field}', blob)
        w1 = [a for a in items if a["name"] == "w1"][0]
        self.assertEqual(w1["ack_timeout_s"], 7200)
        rc, resp = self.harness.cli("agent", "show", "w1", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["delivery"], "inbox")
        # Writes stay user-only, and non-leads cannot read.
        rc, resp = self.harness.cli("agent", "update", "w1", "--max-concurrency", "2", token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        rc, resp = self.harness.cli("agent", "list", token=self.tok_plain)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        rc, resp = self.harness.cli("agent", "show", "lead1", token=self.tok_grok)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        # The active lead of an active project keeps access even without can_lead.
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        try:
            conn.execute("UPDATE agents SET can_lead = 0 WHERE id = "
                         "(SELECT id FROM principals WHERE name = 'lead1');")
            conn.commit()
        finally:
            conn.close()
        rc, resp = self.harness.cli("agent", "list", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        # The user still sees the full view.
        rc, resp = self.harness.cli("agent", "show", "w1", user=True)
        self.assertEqual(rc, 0)
        self.assertIn("options", resp["data"])

    # 7. capabilities
    def test_capability_default_and_no_intake(self) -> None:
        rc, resp = self.harness.cli("agent", "create", "--name", "r1", "--adapter", "none", "--delivery", "inbox",
                                    "--max-concurrency", "1", "--capability", "relay", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["capabilities"], ["relay"])
        self.assertTrue(any("intake" in w for w in resp.get("warnings", [])), resp)
        rc, resp = self.harness.cli("agent", "create", "--name", "r2", "--adapter", "none", "--delivery", "inbox",
                                    "--max-concurrency", "1", "--no-intake", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["capabilities"], [])
        self.assertFalse([w for w in resp.get("warnings", []) if "intake" in w], resp)
        rc, resp = self.harness.cli("agent", "create", "--name", "r3", "--adapter", "none", "--delivery", "inbox",
                                    "--max-concurrency", "1", user=True)
        self.assertEqual(resp["data"]["capabilities"], ["intake"])
        rc, resp = self.harness.cli("agent", "update", "r3", "--no-intake", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["capabilities"], [])


if __name__ == "__main__":
    unittest.main()
