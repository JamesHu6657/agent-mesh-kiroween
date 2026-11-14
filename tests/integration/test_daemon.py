"""Integration tests for Daemon lifecycle, auth, settings, permissions, and socket checks."""

import contextlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness, idle_bound, serial


class TestDaemonIntegration(unittest.TestCase):
    """Test daemon boot, lock, socket, peer-cred, auth, settings, and permissions."""

    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)

    def tearDown(self) -> None:
        self.harness.teardown()

    def test_daemon_status_and_stop(self) -> None:
        """ACC: B8 - daemon status, daemon stop [U], and status when daemon down."""
        # Check status while running
        rc, env_resp = self.harness.cli("status")
        self.assertEqual(rc, 0)
        self.assertTrue(env_resp["ok"])
        self.assertEqual(env_resp["data"]["status"], "running")
        self.assertTrue(env_resp["data"]["testing_mode"])

        # Stop daemon
        rc_stop, stop_resp = self.harness.cli("daemon", "stop", user=True)
        self.assertEqual(rc_stop, 0)
        self.assertTrue(stop_resp["ok"])

        # Now status must return rc 10 DAEMON_UNAVAILABLE with hint
        rc_down, down_resp = self.harness.cli("status")
        self.assertEqual(rc_down, c.EXIT_DAEMON_UNAVAILABLE)
        self.assertFalse(down_resp["ok"])
        self.assertEqual(down_resp["error"]["code"], c.DAEMON_UNAVAILABLE)
        self.assertIn("mesh daemon start", down_resp["error"]["message"])

    def test_daemon_socket_and_no_tcp(self) -> None:
        """ACC: C1 - Daemon opens Unix socket at $H/run/mesh.sock and no TCP ports."""
        sock_path = os.path.join(self.harness.home, "run", "mesh.sock")
        self.assertTrue(os.path.exists(sock_path))
        st = os.stat(sock_path)
        self.assertTrue(stat.S_ISSOCK(st.st_mode))

        # Check PID file
        pid_path = os.path.join(self.harness.home, "run", "meshd.pid")
        self.assertTrue(os.path.exists(pid_path))
        with open(pid_path, "r") as f:
            pid = int(f.read().strip())

        self.assertEqual(pid, self.harness.meshd_pid(), "pid file must name this home's meshd")

        # 1) /proc: resolve every socket fd of meshd against /proc/net/{tcp,tcp6,udp,udp6,unix}.
        fd_dir = f"/proc/{pid}/fd"
        self.assertTrue(os.path.isdir(fd_dir), "/proc/<pid>/fd must exist on Linux")
        sock_inodes = set()
        for fd in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:["):
                sock_inodes.add(target[len("socket:["):-1])
        self.assertTrue(sock_inodes, "meshd must hold at least its listening socket")

        inet_inodes = set()
        for name in ("tcp", "tcp6", "udp", "udp6", "udplite", "udplite6", "raw", "raw6"):
            path = f"/proc/net/{name}"
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()[1:]
            for line in lines:
                cols = line.split()
                if len(cols) > 9:
                    inet_inodes.add(cols[9])
        owned_inet = sock_inodes & inet_inodes
        self.assertEqual(owned_inet, set(), f"meshd owns inet socket inode(s) {owned_inet}")

        listening_unix = []
        with open("/proc/net/unix", "r", encoding="utf-8") as f:
            for line in f.read().splitlines()[1:]:
                cols = line.split()
                # Num RefCount Protocol Flags Type St Inode [Path]; Flags 00010000 = __SO_ACCEPTCON (listening)
                if len(cols) >= 7 and cols[6] in sock_inodes and int(cols[3], 16) & 0x10000:
                    listening_unix.append(cols[7] if len(cols) > 7 else "")
        self.assertEqual(listening_unix, [sock_path], "exactly one listening Unix socket, at $H/run/mesh.sock")

        # 2) ss (present on the test box; its absence is a failure, not a skip).
        ss = shutil.which("ss")
        self.assertIsNotNone(ss, "ss must be installed for C1")
        p = subprocess.run([ss, "-ltnupH"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(f"pid={pid},", p.stdout)
        p_all = subprocess.run([ss, "-tuanpH"], capture_output=True, text=True)
        self.assertEqual(p_all.returncode, 0, p_all.stderr)
        self.assertNotIn(f"pid={pid},", p_all.stdout, "no TCP/UDP socket in any state owned by meshd")
        p_x = subprocess.run([ss, "-xlpH"], capture_output=True, text=True)
        self.assertEqual(p_x.returncode, 0, p_x.stderr)
        mine = [ln for ln in p_x.stdout.splitlines() if f"pid={pid}," in ln]
        self.assertEqual(len(mine), 1, f"expected one listening unix socket for meshd, got {mine}")
        self.assertIn(sock_path, mine[0].split())

    def test_file_permissions(self) -> None:
        """ACC: C3 - Dirs 700; DB, log, socket 600."""
        home = self.harness.home
        # Make sure the request log exists (it is written per request).
        rc, _ = self.harness.cli("status")
        self.assertEqual(rc, 0)
        # Dirs 0700
        for d in [home, os.path.join(home, "run"), os.path.join(home, "runs"), os.path.join(home, "workspaces")]:
            self.assertTrue(os.path.isdir(d), f"{d} must exist")
            mode = stat.S_IMODE(os.stat(d).st_mode)
            self.assertEqual(mode, 0o700, f"Dir {d} has mode {oct(mode)}, expected 0700")

        # Files 0600
        db_path = os.path.join(home, "mesh.db")
        sock_path = os.path.join(home, "run", "mesh.sock")
        log_path = os.path.join(home, "logs", "meshd.log")
        self.assertTrue(stat.S_ISSOCK(os.stat(sock_path).st_mode))
        for f in [db_path, sock_path, log_path]:
            self.assertTrue(os.path.exists(f), f"{f} must exist")
            mode = stat.S_IMODE(os.stat(f).st_mode)
            self.assertEqual(mode, 0o600, f"File {f} has mode {oct(mode)}, expected 0600")

    def test_insecure_home_permissions(self) -> None:
        """ACC: C4 - Stop meshd, chmod 755 $H, mesh daemon run -> rc 2 INSECURE_PERMISSIONS; doctor --fix-perms restores 0700."""
        self.harness.stop_daemon()
        os.chmod(self.harness.home, 0o755)
        try:
            sock_path = os.path.join(self.harness.home, "run", "mesh.sock")
            self.assertFalse(os.path.exists(sock_path), "socket must be gone after stop")
            rc, resp = self.harness.cli("daemon", "run")
            self.assertEqual(rc, c.EXIT_USAGE)
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["code"], c.INSECURE_PERMISSIONS)
            self.assertFalse(os.path.lexists(sock_path), "rejected start must not create the socket")

            # Now run doctor --fix-perms
            rc_fix, resp_fix = self.harness.cli("doctor", "--fix-perms")
            self.assertEqual(rc_fix, 0)
            self.assertTrue(resp_fix["ok"])

            # Verify mode is 0700
            mode = stat.S_IMODE(os.stat(self.harness.home).st_mode)
            self.assertEqual(mode, 0o700)

            # Start daemon again -> succeeds
            self.harness.start_daemon()
            rc_status, resp_status = self.harness.cli("status")
            self.assertEqual(rc_status, 0)
        finally:
            os.chmod(self.harness.home, 0o700)

    def test_install_path_check_in_workspace(self) -> None:
        """ACC: C14 - Copy install to $H/workspaces/inst/, verify daemon run and doctor fail with INSTALL_PATH_INSECURE."""
        self.harness.stop_daemon()
        inst_dir = os.path.join(self.harness.home, "workspaces", "inst")
        from mesh.core.paths import get_install_root
        repo_root = get_install_root()
        # Copy bin/ and mesh/ to $H/workspaces/inst/
        shutil.copytree(os.path.join(repo_root, "bin"), os.path.join(inst_dir, "bin"))
        shutil.copytree(os.path.join(repo_root, "mesh"), os.path.join(inst_dir, "mesh"))
        # Ensure bin/mesh is executable
        os.chmod(os.path.join(inst_dir, "bin", "mesh"), 0o755)

        inst_mesh = os.path.join(inst_dir, "bin", "mesh")
        env = dict(self.harness.get_env())

        # Test daemon run
        sock_path = os.path.join(self.harness.home, "run", "mesh.sock")
        self.assertFalse(os.path.lexists(sock_path), "socket must be gone after stop")
        proc = subprocess.run([inst_mesh, "--home", self.harness.home, "daemon", "run"], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, c.EXIT_USAGE)
        envelope = json.loads(proc.stdout)
        self.assertEqual(envelope["error"]["code"], c.INSTALL_PATH_INSECURE)
        self.assertEqual(envelope["error"]["details"]["install_root"], os.path.realpath(inst_dir))
        self.assertEqual(
            envelope["error"]["details"]["conflict"],
            os.path.realpath(os.path.join(self.harness.home, "workspaces")),
        )
        self.assertFalse(os.path.lexists(sock_path), "INSTALL_PATH_INSECURE start must not create the socket")

        # Test doctor
        proc_doc = subprocess.run([inst_mesh, "--home", self.harness.home, "doctor"], capture_output=True, text=True, env=env)
        self.assertEqual(proc_doc.returncode, c.EXIT_USAGE)
        envelope_doc = json.loads(proc_doc.stdout)
        self.assertEqual(envelope_doc["error"]["code"], c.INSTALL_PATH_INSECURE)
        self.assertEqual(envelope_doc["error"]["details"]["install_root"], os.path.realpath(inst_dir))
        self.assertIn("conflict", envelope_doc["error"]["details"])
        self.assertFalse(os.path.lexists(sock_path))

    def test_daemon_already_running(self) -> None:
        """ACC: C5 - Start second daemon on the same home -> rc 1 DAEMON_ALREADY_RUNNING."""
        # Daemon is already running in self.harness
        pid_before = self.harness.meshd_pid()
        self.assertIsNotNone(pid_before)
        rc, resp = self.harness.cli("daemon", "run")
        self.assertEqual(rc, c.EXIT_INTERNAL)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.DAEMON_ALREADY_RUNNING)
        # The first daemon keeps serving, unchanged.
        rc_st, resp_st = self.harness.cli("daemon", "status")
        self.assertEqual(rc_st, 0, resp_st)
        rc_s, resp_s = self.harness.cli("status")
        self.assertEqual(rc_s, 0, resp_s)
        self.assertEqual(resp_s["data"]["status"], "running")
        self.assertEqual(self.harness.meshd_pid(), pid_before, "meshd pid must be unchanged")
        os.kill(pid_before, 0)

    def test_audit_log_never_contains_auth(self) -> None:
        """ACC: C11 - grep -c '"auth"' $H/logs/meshd.log after requests must be 0."""
        # Agent with a token (setup, unchecked).
        rc, resp = self.harness.setup_cli("agent", "create", "--name", "aud1", "--adapter", "none",
                                          "--delivery", "inbox", "--max-concurrency", "1", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.setup_cli("agent", "token", "issue", "aud1", user=True)
        self.assertEqual(rc, 0, resp)
        agent_token = resp["data"]["token"]
        _db = self.harness.db()
        try:
            agent_id = _db.execute("SELECT id FROM principals WHERE name = 'aud1';").fetchone()[0]
        finally:
            _db.close()
        # Perform user request
        rc, _ = self.harness.cli("config", "list", user=True)
        self.assertEqual(rc, 0)
        rc, _ = self.harness.cli("whoami", user=True)
        self.assertEqual(rc, 0)
        # Token request
        rc, who = self.harness.cli("whoami", token=agent_token)
        self.assertEqual(rc, 0, who)
        # A failing token request
        rc_bad, _ = self.harness.cli("whoami", token="mtk_" + "0" * 40)
        self.assertEqual(rc_bad, c.EXIT_UNAUTHENTICATED)
        # Perform status request
        self.harness.cli("status")

        log_path = os.path.join(self.harness.home, "logs", "meshd.log")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertNotIn('"auth"', content)
        # Verify log line format: req=... cmd=... principal=... rc=... dur_ms=...
        self.assertIn("cmd=config.list", content)
        self.assertIn("cmd=whoami", content)

        import re as _re
        # No secrets anywhere in the log.
        self.assertNotIn(agent_token, content)
        self.assertNotIn(self.harness.passphrase, content)
        self.assertNotIn("0" * 40, content)
        self.assertNotIn("passphrase", content.lower())
        self.assertEqual(content.count('"auth"'), 0)
        # Request lines carry exactly: timestamp, request id, cmd, principal, rc, duration.
        line_re = _re.compile(
            r"^\S+ req=(req_[0-9a-z]+) cmd=([a-z0-9_.-]+) principal=(\S+) rc=(-?\d+) dur_ms=(\d+\.\d{2})$"
        )
        req_lines = [ln for ln in content.splitlines() if " req=" in ln or " cmd=" in ln]
        self.assertGreaterEqual(len(req_lines), 5)
        principals = set()
        for ln in req_lines:
            m = line_re.match(ln)
            self.assertIsNotNone(m, f"request log line has unexpected fields: {ln!r}")
            keys = [kv.split("=", 1)[0] for kv in ln.split()[1:]]
            self.assertEqual(keys, ["req", "cmd", "principal", "rc", "dur_ms"])
            principals.add(m.group(3))
        whoami_lines = [ln for ln in req_lines if " cmd=whoami " in ln]
        self.assertGreaterEqual(len(whoami_lines), 3)
        self.assertTrue(agent_id)
        if True:
            self.assertIn(agent_id, principals, "the agent-token request must be logged with its principal")
        self.assertTrue(any(" rc=3 " in ln for ln in whoami_lines), "failed token request is logged with rc 3")

    def test_config_crud_and_validation(self) -> None:
        """ACC: B13 - config list, get, set with validation and permissions."""
        # config list [U]
        rc, resp = self.harness.cli("config", "list", user=True)
        self.assertEqual(rc, 0)
        self.assertTrue(resp["ok"])
        settings_items = resp["data"]["items"]
        keys = {item["key"] for item in settings_items}
        self.assertIn("task.default_timeout_s", keys)
        self.assertIn("lead.profile", keys)

        # Every plan §5.7 key with its exact default (hard-coded from the plan table and the §7.4 policies).
        expected_defaults = {
            "lead.default_agent": None,
            "lead.wake_debounce_s": 5,
            "lead.profile": "readonly",
            "lead.turn_timeout_s": 1800,
            "scheduler.max_total_runs": None,
            "workspaces.root": os.path.join(self.harness.home, "workspaces"),
            "task.default_timeout_s": 3600,
            "task.default_max_attempts": 3,
            "task.max_attempts_cap": 10,
            "inbox.ack_timeout_s": 600,
            "lease.ttl_s": 120,
            "kill.grace_s": 15,
            "kill.post_submit_grace_s": 60,
            "outbox.max_attempts": 3,
            "approval.ttl_seconds": 86400,
            "approval.policy.config": "direct_only",
            "approval.policy.lead_change": "direct_or_relayed",
            "approval.policy.project_control": "direct_or_relayed",
            "approval.policy.scope_change": "direct_or_relayed",
            "approval.policy.permission_escalation": "direct_only",
            "approval.policy.retry_over_limit": "direct_or_relayed",
            "approval.policy.delete": "direct_or_relayed",
            "approval.policy.deliver_to_base": "not_required",
            "approval.policy.spend_money": "direct_or_relayed",
            "approval.policy.deploy": "direct_or_relayed",
            "approval.policy.delete_external": "direct_or_relayed",
            "approval.policy.external_other": "direct_or_relayed",
            "notify.command": None,
        }
        by_key = {item["key"]: item for item in settings_items}
        self.assertEqual(len(by_key), len(settings_items), "config list repeats a key")
        for key, default in expected_defaults.items():
            with self.subTest(key=key):
                self.assertIn(key, by_key, f"config list is missing plan §5.7 key {key}")
                self.assertIn("default", by_key[key])
                self.assertEqual(by_key[key]["default"], default)
                self.assertEqual(by_key[key]["value"], default, "fresh home: value equals default")
                self.assertIn("set", by_key[key])

        # config set nosuch.key 1 -> INVALID_SETTING
        rc, resp = self.harness.cli("config", "set", "nosuch.key", "1", user=True)
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)
        self.assertEqual(resp["error"]["details"]["key"], "nosuch.key")

        # config set task.default_timeout_s '"600"' -> INVALID_SETTING (wrong type: string)
        rc, resp = self.harness.cli("config", "set", "task.default_timeout_s", '"600"', user=True)
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)
        self.assertEqual(resp["error"]["details"]["key"], "task.default_timeout_s")

        # config set task.default_timeout_s 0 -> INVALID_SETTING (out of range)
        rc, resp = self.harness.cli("config", "set", "task.default_timeout_s", "0", user=True)
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)
        self.assertEqual(resp["error"]["details"]["key"], "task.default_timeout_s")

        # config set task.default_timeout_s 600 -> rc 0
        rc, resp = self.harness.cli("config", "set", "task.default_timeout_s", "600", user=True)
        self.assertEqual(rc, 0)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["data"]["value"], 600)

        # Verify get returns updated value
        rc, resp = self.harness.cli("config", "get", "task.default_timeout_s", user=True)
        self.assertEqual(rc, 0)
        self.assertEqual(resp["data"]["value"], 600)

    def test_token_file_mode_0644_rejected(self) -> None:
        """ACC: C10 - m whoami --token-file f with f mode 0644 -> rc 3 TOKEN_FILE_INSECURE."""
        rc, resp = self.harness.setup_cli("agent", "create", "--name", "tf1", "--adapter", "none",
                                          "--delivery", "inbox", "--max-concurrency", "1", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.setup_cli("agent", "token", "issue", "tf1", user=True)
        self.assertEqual(rc, 0, resp)
        token = resp["data"]["token"]
        f = os.path.join(self.harness.fixtures_dir, "f.tok")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.chmod(f, 0o600)
        rc_ok, resp_ok = self.harness.cli("whoami", "--token-file", f)
        self.assertEqual(rc_ok, 0, resp_ok)  # control: the same file works at 0600
        os.chmod(f, 0o644)
        rc, resp = self.harness.cli("whoami", "--token-file", f)
        self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, resp)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.TOKEN_FILE_INSECURE)
        self.assertNotIn(token, json.dumps(resp))

    def test_token_file_in_workspaces_rejected(self) -> None:
        """ACC: C12 - grok's token copied to $H/workspaces/g.tok (0600) -> whoami rc 3 TOKEN_FILE_INSECURE."""
        rc, resp = self.harness.setup_cli("agent", "create", "--name", "grok", "--adapter", "none",
                                          "--delivery", "inbox", "--max-concurrency", "1",
                                          "--capability", "intake", "--capability", "relay", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.setup_cli("agent", "token", "issue", "grok", user=True)
        self.assertEqual(rc, 0, resp)
        token = resp["data"]["token"]
        orig = os.path.join(self.harness.fixtures_dir, "grok.tok")
        with open(orig, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.chmod(orig, 0o600)
        rc_ok, resp_ok = self.harness.cli("whoami", "--token-file", orig)
        self.assertEqual(rc_ok, 0, resp_ok)  # control: grok's own token file works
        g = os.path.join(self.harness.home, "workspaces", "g.tok")
        shutil.copyfile(orig, g)
        os.chmod(g, 0o600)
        try:
            self.assertEqual(stat.S_IMODE(os.stat(g).st_mode), 0o600)
            rc, resp = self.harness.cli("whoami", "--token-file", g)
            self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, resp)
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["code"], c.TOKEN_FILE_INSECURE)
            self.assertNotIn(token, json.dumps(resp))
        finally:
            os.remove(g)

    # Parallel phase since M1c run 5 (A1 serial cut): the lockout state is home-local
    # (auth_failures of this home's daemon); the 30 s window keeps the five failures
    # inside it on a loaded box, and load can only lengthen the lapse wait.
    @idle_bound
    def test_passphrase_lockout(self) -> None:
        """ACC: G12 - 5 wrong passphrases -> rc 3 PASSPHRASE_INVALID; 6th -> rc 3 LOCKED_OUT."""
        # Set the testing-mode lockout window (the seeded default is 300 s; this test sets
        # 30 s since the M1c run 5 parallel phase) so the five failing attempts cannot age
        # out of the window while the machine is loaded, and the lapse wait stays bounded.
        rc_cfg, _ = self.harness.cli("config", "set", "auth.lockout_window_s", "30", user=True)
        self.assertEqual(rc_cfg, 0)

        # Create a bad passphrase file
        bad_pass_file = os.path.join(self.harness.home, ".bad_pass")
        with open(bad_pass_file, "w") as f:
            f.write("wrong-password-123456")
        os.chmod(bad_pass_file, 0o600)

        bad_fd = os.open(bad_pass_file, os.O_RDONLY)
        try:
            # 5 failures with wrong passphrase
            for i in range(5):
                rc, resp = self.harness.cli("whoami", "--user", "--passphrase-fd", str(bad_fd))
                self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, f"Attempt {i+1} got rc {rc}: {resp}")
                self.assertEqual(resp["error"]["code"], c.PASSPHRASE_INVALID)
        finally:
            os.close(bad_fd)

        # 6th attempt with correct passphrase during lockout window -> LOCKED_OUT
        rc, resp = self.harness.cli("whoami", user=True)
        self.assertEqual(rc, c.EXIT_UNAUTHENTICATED)
        self.assertEqual(resp["error"]["code"], c.LOCKED_OUT)

        # Check auth.failed events exist and contain no passphrase text (ACC: G12)
        conn = self.harness.db()
        events = conn.execute("SELECT payload_json FROM events WHERE type = 'auth.failed';").fetchall()
        conn.close()
        self.assertGreaterEqual(len(events), 5)
        for ev in events:
            self.assertNotIn("wrong-password-123456", ev["payload_json"])

        # ACC: G12 - After lockout ends: m event list --type auth.failed [U] -> rc 0, at least 5 items, no passphrase text in CLI output
        # The lockout window is 30 s here, so wait for it to lapse. Polling the
        # daemon keeps the wait honest without a fixed sleep.
        def _lockout_lapsed() -> bool:
            rc_probe, resp_probe = self.harness.cli("whoami", user=True)
            return rc_probe == 0 and resp_probe.get("ok") is True

        self.assertTrue(
            self.harness.wait_for(_lockout_lapsed, timeout=75.0, step=0.5),
            "Lockout window never lapsed",
        )

        rc_evt, resp_evt = self.harness.cli("event", "list", "--type", "auth.failed", user=True)
        self.assertEqual(rc_evt, 0)
        self.assertTrue(resp_evt["ok"])
        items = resp_evt["data"]["items"]
        self.assertGreaterEqual(len(items), 5)
        self.assertNotIn("wrong-password-123456", json.dumps(resp_evt))

    def test_passphrase_sources(self) -> None:
        """ACC: G17 - Passphrase in stdin JSON rejected; no source -> NO_TTY; env/file sources are opt-in."""
        # Stdin with passphrase key
        stdin_json = json.dumps({"passphrase": "some-passphrase", "key": "task.default_timeout_s", "value": 600})
        rc, resp = self.harness.cli("config", "set", "task.default_timeout_s", "600", "--input", "-", user=True, stdin=stdin_json)
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertEqual(resp["error"]["code"], c.PASSPHRASE_SOURCE_REJECTED)

        # No fd, no file, no env and no tty -> NO_TTY exit 3 (G17: setsid -w ... </dev/null)
        def run(extra_env=None, *extra):
            cmd = ["setsid", "-w", self.harness.bin_mesh, "--home", self.harness.home, "whoami", "--user", *extra]
            env = {k: v for k, v in self.harness.get_env().items() if not k.startswith("MESH_PASSPHRASE")}
            env.update(extra_env or {})
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env)
            return proc.returncode, json.loads(proc.stdout)

        rc, resp = run()
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.NO_TTY))
        # Opt-in sources (productize): MESH_PASSPHRASE, MESH_PASSPHRASE_FILE, --passphrase-file.
        rc, resp = run({"MESH_PASSPHRASE": "wrong-passphrase-123456"})
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_UNAUTHENTICATED, c.PASSPHRASE_INVALID))
        rc, resp = run({"MESH_PASSPHRASE": self.harness.passphrase})
        self.assertEqual(rc, 0, resp)
        rc, resp = run({"MESH_PASSPHRASE_FILE": self.harness.passphrase_file})
        self.assertEqual(rc, 0, resp)
        rc, resp = run(None, "--passphrase-file", self.harness.passphrase_file)
        self.assertEqual(rc, 0, resp)
        loose = os.path.join(self.harness.fixtures_dir, "loose-pass")
        with open(loose, "w", encoding="utf-8") as f:
            f.write(self.harness.passphrase)
        os.chmod(loose, 0o644)
        rc, resp = run(None, "--passphrase-file", loose)
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_USAGE, c.INSECURE_PERMISSIONS))

    def test_idempotency_and_dry_run(self) -> None:
        """ACC: B5, B6 - Idempotency key replay and dry-run."""
        # 1. Config set with idempotency key k1
        rc1, resp1 = self.harness.cli(
            "config", "set", "task.default_timeout_s", "700",
            "--idempotency-key", "k1",
            user=True,
        )
        self.assertEqual(rc1, 0)
        self.assertFalse(resp1.get("idempotent_replay", False))

        # Replay with same key k1 and same value 700 -> same data, idempotent_replay: true
        rc2, resp2 = self.harness.cli(
            "config", "set", "task.default_timeout_s", "700",
            "--idempotency-key", "k1",
            user=True,
        )
        self.assertEqual(rc2, 0)
        self.assertTrue(resp2.get("idempotent_replay", False))
        self.assertEqual(resp1["data"]["value"], resp2["data"]["value"])

        # Replay with key k1 but different value 800 -> rc 6 IDEMPOTENCY_KEY_REUSED
        rc3, resp3 = self.harness.cli(
            "config", "set", "task.default_timeout_s", "800",
            "--idempotency-key", "k1",
            user=True,
        )
        self.assertEqual(rc3, c.EXIT_CONFLICT)
        self.assertEqual(resp3["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)

        # 2. Dry run: dry_run: true and counts unchanged
        db = self.harness.db()
        events_before = db.execute("SELECT COUNT(*) FROM events;").fetchone()[0]
        db.close()

        rc_dry, resp_dry = self.harness.cli(
            "config", "set", "task.default_timeout_s", "900",
            "--dry-run",
            user=True,
        )
        self.assertEqual(rc_dry, 0)
        self.assertTrue(resp_dry.get("dry_run", False))
        self.assertIn("would", resp_dry["data"])

        # Value in DB should still be 700
        rc_get, resp_get = self.harness.cli("config", "get", "task.default_timeout_s", user=True)
        self.assertEqual(resp_get["data"]["value"], 700)

        db = self.harness.db()
        events_after = db.execute("SELECT COUNT(*) FROM events;").fetchone()[0]
        db.close()
        self.assertEqual(events_before, events_after)

    def test_schema_too_new(self) -> None:
        """ACC: I9 - Database with schema_version newer than supported raises SCHEMA_TOO_NEW."""
        self.harness.stop_daemon()
        # Directly modify meta table schema_version
        db_path = os.path.join(self.harness.home, "mesh.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version';")
        conn.commit()
        conn.close()

        import hashlib

        def snapshot():
            st = os.stat(db_path)
            with open(db_path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            side = {s_: os.path.exists(db_path + s_) for s_ in ("-wal", "-shm", "-journal")}
            return (st.st_ino, st.st_size, st.st_mtime_ns, digest, side)

        before = snapshot()
        try:
            rc, resp = self.harness.cli("daemon", "run")
            self.assertEqual(rc, c.EXIT_INTERNAL)
            self.assertEqual(resp["error"]["code"], c.SCHEMA_TOO_NEW)
            after = snapshot()
            self.assertEqual(after[0], before[0], "mesh.db was recreated (inode changed)")
            self.assertEqual(after[3], before[3], "mesh.db content changed")
            self.assertEqual(after[1:3], before[1:3], "mesh.db size/mtime changed")
            self.assertFalse(os.path.exists(os.path.join(self.harness.home, "run", "mesh.sock")))
        finally:
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version';")
            conn.commit()
            conn.close()

    def test_daemon_start_user_passphrase_fd(self) -> None:
        """F1 regression: daemon start --user reads fd once, status poll doesn't fail auth, auth.failed count is 0."""
        fresh = MeshHarness(init_db=True, start_daemon=False)
        try:
            p_file = os.path.join(fresh.home, ".pass_test")
            with open(p_file, "w") as f:
                f.write(fresh.passphrase)
            os.chmod(p_file, 0o600)
            fd = os.open(p_file, os.O_RDONLY)
            try:
                # Real CLI invocation: bin/mesh --home <fresh.home> --user --passphrase-fd <fd> daemon start
                cmd = [fresh.bin_mesh, "--home", fresh.home, "--user", "--passphrase-fd", str(fd), "daemon", "start"]
                proc = subprocess.run(
                    cmd,
                    pass_fds=(fd,),
                    capture_output=True,
                    text=True,
                    env=fresh.get_env(),
                    timeout=10,
                )
                self.assertEqual(proc.returncode, 0, f"daemon start failed: {proc.stdout} {proc.stderr}")
                data = json.loads(proc.stdout)
                self.assertTrue(data.get("ok"))
                self.assertEqual(data.get("data", {}).get("status"), "started")
            finally:
                os.close(fd)

            # Check whoami --user succeeds with rc 0
            rc_who, resp_who = fresh.cli("whoami", user=True)
            self.assertEqual(rc_who, 0, f"whoami failed: {resp_who}")
            self.assertTrue(resp_who.get("ok"))
            self.assertEqual(resp_who.get("data", {}).get("name"), "user")

            # Check auth.failed count is 0
            db = fresh.db()
            fail_count = db.execute("SELECT COUNT(*) FROM events WHERE type = 'auth.failed'").fetchone()[0]
            db.close()
            self.assertEqual(fail_count, 0, f"Expected 0 auth.failed events, found {fail_count}")
        finally:
            fresh.teardown()

    def test_daemon_start_daemon_stopped_by_teardown(self) -> None:
        """r7d3 F1 regression: a meshd started via `mesh daemon start` (detached, not
        spawned by the harness) is stopped by teardown; no process for the home remains."""
        fresh = MeshHarness(init_db=True, start_daemon=False)
        home = fresh.home
        try:
            rc, resp = fresh.cli("daemon", "start", user=True)
            self.assertEqual(rc, 0, f"daemon start failed: {resp}")
            pid_file = os.path.join(home, "run", "meshd.pid")
            self.assertTrue(fresh.wait_for(lambda: os.path.exists(pid_file), timeout=10.0))
            with open(pid_file, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            self.assertTrue(os.path.exists(f"/proc/{pid}"))
            self.assertIsNone(fresh.daemon_proc)
            # r7d4: the detached daemon inherits the harness token (its credential).
            with open(f"/proc/{pid}/environ", "rb") as f:
                environ = f.read().split(b"\0")
            self.assertIn(b"MESH_TEST_HARNESS_TOKEN=" + fresh._harness_token.encode(), environ)
            # Linear r7d5 MINOR: no explicit stop here; teardown alone must stop it.
        finally:
            fresh.teardown()

        def procs_for_home() -> list:
            found = []
            want = home.encode()
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as f:
                        argv = f.read().split(b"\0")
                    with open(f"/proc/{entry}/stat", "r") as f:
                        state = f.read().rsplit(")", 1)[1].split()[0]
                except OSError:
                    continue
                if state != "Z" and want in argv:
                    found.append(int(entry))
            return found

        deadline = time.monotonic() + 10.0
        while procs_for_home() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(procs_for_home(), [], f"processes still running for {home}")
        self.assertFalse(os.path.exists(home))

    def test_foreign_home_daemon_survives_other_harness_cleanup(self) -> None:
        """Linear r7d6 MINOR: a REAL daemon for home B, started by `mesh daemon start
        --home B` with harness A's MESH_HOME and token in its env, is refused by A's
        cleanup even when A's pidfile names it, and B keeps serving afterwards."""
        a = MeshHarness(init_db=True, start_daemon=False)
        b = MeshHarness(init_db=True, start_daemon=False)
        pid = None
        try:
            env_a = {"MESH_HOME": a.home, "MESH_TEST_HARNESS_TOKEN": a._harness_token}
            rc, resp = b.cli("daemon", "start", user=True, extra_env=env_a, timeout=40.0)
            self.assertEqual(rc, 0, f"daemon start failed: {resp}")
            pid_file_b = os.path.join(b.home, "run", "meshd.pid")
            self.assertTrue(b.wait_for(lambda: os.path.exists(pid_file_b), timeout=10.0))
            with open(pid_file_b, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            with open(f"/proc/{pid}/environ", "rb") as f:
                environ = f.read().split(b"\0")
            self.assertIn(b"MESH_HOME=" + a.home.encode(), environ)
            self.assertIn(b"MESH_TEST_HARNESS_TOKEN=" + a._harness_token.encode(), environ)
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                self.assertEqual(f.read().split(b"\0")[:9],
                                 [b"python3", b"-P", b"-s", b"-m", b"mesh", b"--home",
                                  b.home.encode(), b"daemon", b"run"])
            os.makedirs(os.path.join(a.home, "run"), exist_ok=True)
            with open(os.path.join(a.home, "run", "meshd.pid"), "w", encoding="utf-8") as f:
                f.write(f"{pid}\n")
            self.assertEqual(a.stop_pidfile_daemon(), "home_mismatch")
            a.stop_daemon()  # the whole teardown stop path refuses it too
            time.sleep(0.5)
            with open(f"/proc/{pid}/stat", "r") as f:
                self.assertNotIn(f.read().rsplit(")", 1)[1].split()[0], ("Z", "X"))
            rc, resp = b.cli("daemon", "status", user=True)
            self.assertEqual(rc, 0, f"daemon B stopped answering: {resp}")
            self.assertEqual(resp.get("data", {}).get("pid"), pid, resp)
            rc, resp = b.cli("daemon", "stop", user=True)
            self.assertEqual(rc, 0, f"daemon stop failed: {resp}")
            self.assertTrue(b.wait_for(lambda: not os.path.exists(f"/proc/{pid}")
                                       or open(f"/proc/{pid}/stat").read()
                                       .rsplit(")", 1)[1].split()[0] in ("Z", "X"),
                                       timeout=10.0))
            pid = None
        finally:
            if pid is not None:  # failure path: B carries A's token, stop it directly
                with contextlib.suppress(ProcessLookupError):
                    fd = os.pidfd_open(pid)
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    finally:
                        os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(a.home, "run", "meshd.pid"))
            a.teardown()
            b.teardown()

    def test_empty_passphrase_rejected_client_side(self) -> None:
        """F1 regression: empty passphrase rejected client-side without hitting daemon."""
        fresh = MeshHarness(init_db=True, start_daemon=True)
        try:
            empty_file = os.path.join(fresh.home, ".empty_pass")
            with open(empty_file, "w") as f:
                f.write("")
            os.chmod(empty_file, 0o600)

            fd = os.open(empty_file, os.O_RDONLY)
            try:
                cmd = [fresh.bin_mesh, "--home", fresh.home, "--user", "--passphrase-fd", str(fd), "whoami"]
                proc = subprocess.run(
                    cmd,
                    pass_fds=(fd,),
                    capture_output=True,
                    text=True,
                    env=fresh.get_env(),
                    timeout=5,
                )
                self.assertIn(proc.returncode, (c.EXIT_USAGE, c.EXIT_UNAUTHENTICATED))
                data = json.loads(proc.stdout)
                self.assertFalse(data.get("ok"))
                self.assertIn(data["error"]["code"], (c.PASSPHRASE_INVALID, c.INVALID_ARGUMENT))
            finally:
                os.close(fd)

            # Check no auth.failed was written
            db = fresh.db()
            fail_count = db.execute("SELECT COUNT(*) FROM events WHERE type = 'auth.failed'").fetchone()[0]
            db.close()
            self.assertEqual(fail_count, 0)
        finally:
            fresh.teardown()

    def test_f4_authz_matrix_reads(self) -> None:
        """F4 regression: config.list/get and agent.list/show require direct user auth (unauth -> rc 3, agent -> rc 4)."""
        # Create an agent and token to test agent callers
        rc_c, _ = self.harness.cli(
            "agent", "create",
            "--name", "read_test_agent",
            "--adapter", "none",
            "--delivery", "inbox",
            "--max-concurrency", "1",
            user=True,
        )
        self.assertEqual(rc_c, 0)
        _, tok_resp = self.harness.cli("agent", "token", "issue", "read_test_agent", user=True)
        agent_tok = tok_resp["data"]["token"]

        # 1. Unauthenticated calls -> rc 3 TOKEN_MISSING
        for cmd in (["config", "list"], ["config", "get", "task.default_timeout_s"], ["agent", "list"], ["agent", "show", "read_test_agent"]):
            rc, resp = self.harness.cli(*cmd)
            self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, f"Unauthenticated {cmd} expected rc 3, got {rc}: {resp}")
            self.assertEqual(resp["error"]["code"], c.TOKEN_MISSING)

        # 2. Agent token calls -> rc 4 CAPABILITY_MISSING (plan §7.3 L489, L504)
        for cmd in (["config", "list"], ["config", "get", "task.default_timeout_s"], ["agent", "list"], ["agent", "show", "read_test_agent"]):
            rc, resp = self.harness.cli(*cmd, token=agent_tok)
            self.assertEqual(rc, c.EXIT_FORBIDDEN, f"Agent token {cmd} expected rc 4, got {rc}: {resp}")
            self.assertEqual(resp["error"]["code"], c.CAPABILITY_MISSING)

        # 3. Direct user calls -> rc 0
        for cmd in (["config", "list"], ["config", "get", "task.default_timeout_s"], ["agent", "list"], ["agent", "show", "read_test_agent"]):
            rc, resp = self.harness.cli(*cmd, user=True)
            self.assertEqual(rc, 0, f"Direct user {cmd} expected rc 0, got {rc}: {resp}")
            self.assertTrue(resp["ok"])

    def test_f5_config_validation(self) -> None:
        """F5 regression: config set integer settings float/NaN/Infinity -> rc 2 INVALID_SETTING; lead.default_agent validation."""
        # 1. Integer settings with floats/NaN/Infinity -> rc 2 INVALID_SETTING (never INTERNAL_ERROR)
        bad_values = ["600.0", "1e3", "NaN", "Infinity", "not json"]
        for val in bad_values:
            rc, resp = self.harness.cli("config", "set", "task.default_timeout_s", val, user=True)
            self.assertEqual(rc, c.EXIT_USAGE, f"config set task.default_timeout_s {val} should be rc 2, got {rc}: {resp}")
            self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)
            self.assertEqual(resp["error"]["details"].get("key"), "task.default_timeout_s")

        # Negative infinity passed with '--' separator to prevent argparse flag parsing
        rc_neg, resp_neg = self.harness.cli("config", "set", "task.default_timeout_s", "--", "-Infinity", user=True)
        self.assertEqual(rc_neg, c.EXIT_USAGE)
        self.assertEqual(resp_neg["error"]["code"], c.INVALID_SETTING)
        self.assertEqual(resp_neg["error"]["details"].get("key"), "task.default_timeout_s")

        # 2. lead.default_agent: non-existent -> rc 2 INVALID_SETTING
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"nobody"', user=True)
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)
        self.assertEqual(resp["error"]["details"].get("key"), "lead.default_agent")

        # Create agent without can_lead
        self.harness.cli("agent", "create", "--name", "worker_nolead", "--adapter", "none", "--delivery", "inbox", "--max-concurrency", "1", user=True)
        rc_no, resp_no = self.harness.cli("config", "set", "lead.default_agent", '"worker_nolead"', user=True)
        self.assertEqual(rc_no, c.EXIT_USAGE)
        self.assertEqual(resp_no["error"]["code"], c.INVALID_SETTING)
        self.assertEqual(resp_no["error"]["details"].get("key"), "lead.default_agent")

        # Create agent with can_lead -> rc 0
        self.harness.cli("agent", "create", "--name", "lead_ok", "--adapter", "none", "--delivery", "inbox", "--max-concurrency", "1", "--can-lead", user=True)
        rc_ok, resp_ok = self.harness.cli("config", "set", "lead.default_agent", '"lead_ok"', user=True)
        self.assertEqual(rc_ok, 0)
        self.assertTrue(resp_ok["ok"])

    def test_f6_caller_pythonpath_isolated(self) -> None:
        """F6 regression: caller PYTHONPATH with shadow argparse.py is not executed by bin/mesh."""
        shadow_dir = tempfile.mkdtemp(prefix="mh-shadow-")
        try:
            marker = os.path.join(shadow_dir, "SHADOW_HIT")
            shadow_argparse = os.path.join(shadow_dir, "argparse.py")
            with open(shadow_argparse, "w") as f:
                f.write(f"with open({marker!r}, 'w') as mf: mf.write('HIT')\nimport sys; sys.exit(99)\n")

            env = self.harness.get_env()
            env["PYTHONPATH"] = shadow_dir

            cmd = [self.harness.bin_mesh, "--home", self.harness.home, "version"]
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, f"Shadow argparse was executed! stdout={proc.stdout}, stderr={proc.stderr}")
            self.assertFalse(os.path.exists(marker), "Shadow argparse was executed!")
        finally:
            shutil.rmtree(shadow_dir, ignore_errors=True)

    def test_f7a_request_size_limits(self) -> None:
        """F7(a) regression: requests > 64 KiB up to 1 MiB handled; > 1 MiB returns structured rc 2 error, never INTERNAL_ERROR."""
        # 1. Request between 64 KiB and 1 MiB (200 KB) via --input file
        big_str = "x" * 200000
        input_file = os.path.join(self.harness.home, "big_input.json")
        with open(input_file, "w") as f:
            json.dump({"default_profile": big_str}, f)

        rc, resp = self.harness.cli(
            "agent", "create", "--name", "big_agent", "--adapter", "none",
            "--delivery", "inbox", "--max-concurrency", "1",
            "--input", input_file, user=True
        )
        # Should be parsed by daemon and rejected by validator (rc 2 INVALID_ARGUMENT), NOT crash or INTERNAL_ERROR
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)
        self.assertIn("default_profile", resp["error"]["message"])

        # Daemon should still be alive
        rc_stat, resp_stat = self.harness.cli("status")
        self.assertEqual(rc_stat, 0)
        self.assertTrue(resp_stat["ok"])

        # 2. Request exceeding 1 MiB (1.2 MiB) directly via socket
        sock_path = os.path.join(self.harness.home, "run", "mesh.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
        oversized = "y" * (1200 * 1024)
        req = json.dumps({"v": 1, "cmd": "status", "auth": {"type": "none"}, "args": {"big": oversized}}) + "\n"
        s.sendall(req.encode("utf-8"))
        raw_resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            raw_resp += chunk
        s.close()

        resp_over = json.loads(raw_resp.decode("utf-8"))
        self.assertFalse(resp_over["ok"])
        self.assertEqual(resp_over["exit_code"], c.EXIT_USAGE)
        self.assertEqual(resp_over["error"]["code"], c.INVALID_ARGUMENT)
        self.assertIn("Request payload exceeds 1 MiB limit", resp_over["error"]["message"])

        # Daemon should survive
        rc_after, resp_after = self.harness.cli("status")
        self.assertEqual(rc_after, 0)
        self.assertTrue(resp_after["ok"])

    def test_f7b_unknown_command_classification(self) -> None:
        """F7(b) regression: unknown daemon commands return rc 2 INVALID_ARGUMENT; planned verbs return NOT_IMPLEMENTED_IN_M1."""
        sock_path = os.path.join(self.harness.home, "run", "mesh.sock")

        def send_raw(cmd_name: str) -> dict:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            req = json.dumps({
                "v": 1,
                "cmd": cmd_name,
                "auth": {"type": "user", "passphrase": self.harness.passphrase},
                "args": {},
            }) + "\n"
            s.sendall(req.encode("utf-8"))
            raw = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                raw += chunk
            s.close()
            return json.loads(raw.decode("utf-8"))

        # Unknown command -> INVALID_ARGUMENT (rc 2), NOT NOT_IMPLEMENTED_IN_M1
        resp_unknown = send_raw("completely.bogus.command")
        self.assertFalse(resp_unknown["ok"])
        self.assertEqual(resp_unknown["exit_code"], c.EXIT_USAGE)
        self.assertEqual(resp_unknown["error"]["code"], c.INVALID_ARGUMENT)
        self.assertIn("Unknown command", resp_unknown["error"]["message"])
        self.assertNotIn("milestone", resp_unknown["error"].get("details", {}))

        # M1c read verbs are implemented since m1c run 2 (sub-step 5).
        resp_run_list = send_raw("run.list")
        self.assertTrue(resp_run_list["ok"], resp_run_list)
        self.assertEqual(resp_run_list["data"]["runs"], [])

        resp_ws_list = send_raw("workspace.list")
        self.assertTrue(resp_ws_list["ok"], resp_ws_list)

        resp_ws_show = send_raw("workspace.show")
        self.assertEqual(resp_ws_show["error"]["code"], c.INVALID_ARGUMENT)

        # Planned verbs return NOT_IMPLEMENTED_IN_M1 with correct milestones

        # task.merge is built in M2 r5 (B11 superseded by K9): a known command, so a
        # request without task_id is a validation error, not a milestone stub.
        resp_m2 = send_raw("task.merge")
        self.assertFalse(resp_m2["ok"])
        self.assertEqual(resp_m2["exit_code"], c.EXIT_USAGE)
        self.assertEqual(resp_m2["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_m2["error"]["details"].get("param"), "task_id")
        self.assertNotIn("milestone", resp_m2["error"]["details"])

        # workspace.prune / workspace.delete are built in M2 r8 (K12): known commands, so
        # they are served (prune with no candidates is rc 0) or validated, never a stub.
        resp_ws_prune = send_raw("workspace.prune")
        self.assertTrue(resp_ws_prune["ok"], resp_ws_prune)
        self.assertEqual(resp_ws_prune["data"]["removed"], [])
        self.assertEqual(resp_ws_prune["data"]["skipped"], [])

        resp_ws_delete = send_raw("workspace.delete")
        self.assertFalse(resp_ws_delete["ok"])
        self.assertEqual(resp_ws_delete["exit_code"], c.EXIT_USAGE)
        self.assertEqual(resp_ws_delete["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_ws_delete["error"]["details"].get("param"), "task_id")
        self.assertNotIn("milestone", resp_ws_delete["error"]["details"])

        resp_top = send_raw("top")
        self.assertEqual(resp_top["error"]["code"], c.NOT_IMPLEMENTED_IN_M1)
        self.assertIsNone(resp_top["error"]["details"].get("milestone"))

    def test_f7c_doctor_schema_too_new(self) -> None:
        """F7(c) regression: doctor --check-invariants on DB with future schema_version returns rc 1 SCHEMA_TOO_NEW without modifying DB."""
        import hashlib
        db_path = os.path.join(self.harness.home, "mesh.db")
        self.harness.stop_daemon()

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version';")
        conn.commit()
        conn.close()

        with open(db_path, "rb") as f:
            h_before = hashlib.sha256(f.read()).hexdigest()
        mt_before = os.stat(db_path).st_mtime_ns

        rc, resp = self.harness.cli("doctor", "--check-invariants")
        self.assertEqual(rc, c.EXIT_INTERNAL)
        self.assertEqual(resp["error"]["code"], c.SCHEMA_TOO_NEW)
        self.assertEqual(resp["error"]["details"].get("current_version"), 999)

        # Verify DB is completely unmodified
        with open(db_path, "rb") as f:
            h_after = hashlib.sha256(f.read()).hexdigest()
        mt_after = os.stat(db_path).st_mtime_ns
        self.assertEqual(h_before, h_after)
        self.assertEqual(mt_before, mt_after)

        # Restore version so teardown invariant check passes
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version';")
        conn.commit()
        conn.close()

    def test_f7d_daemon_startup_honors_configured_workspaces_root(self) -> None:
        """F7(d) regression: daemon startup install-path check honours configured workspaces.root."""
        from mesh.core.paths import get_install_root
        fresh = MeshHarness(init_db=True, start_daemon=False)
        db_path = os.path.join(fresh.home, "mesh.db")
        try:
            install_root = get_install_root()
            # Set workspaces.root in DB to install root
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) VALUES ('workspaces.root', ?, datetime('now'), 'usr_init');", (json.dumps(install_root),))
            conn.commit()
            conn.close()

            # Attempting to run daemon should fail with INSTALL_PATH_INSECURE (rc 2)
            cmd = [fresh.bin_mesh, "--home", fresh.home, "daemon", "run"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=fresh.get_env())
            self.assertEqual(proc.returncode, c.EXIT_USAGE)
            data = json.loads(proc.stdout)
            self.assertEqual(data["error"]["code"], c.INSTALL_PATH_INSECURE)
        finally:
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM settings WHERE key = 'workspaces.root';")
            conn.commit()
            conn.close()
            fresh.teardown()


if __name__ == "__main__":
    unittest.main()
