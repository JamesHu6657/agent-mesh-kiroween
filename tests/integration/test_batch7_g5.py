"""Focused integration regression tests for batch7 g5 findings (adapters and CLI)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import mesh.constants as c
from mesh.core.paths import mesh_cli_argv
from mesh.platform import ipc as plat_ipc
from tests.helpers import MeshHarness
from tests.stubs import _write_python_stub


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _raw_socket_call(sock_path: str, payload: bytes) -> dict:
    with plat_ipc.connect_endpoint(sock_path, timeout=10.0) as s:
        s.settimeout(10.0)
        s.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8").splitlines()[0])


class TestBatch7G5Integration(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)
        self.addCleanup(self.harness.teardown)
        self.tok_dir = tempfile.mkdtemp(prefix="mh-g5-tok-")
        self.addCleanup(shutil.rmtree, self.tok_dir, True)

    def _create_agent(self, name: str, *, can_lead: bool = False) -> str:
        args = ["agent", "create", "--name", name, "--adapter", "none", "--delivery", "inbox", "--max-concurrency", "1"]
        if can_lead:
            args.append("--can-lead")
        rc, resp = self.harness.cli(*args, user=True)
        self.assertEqual(rc, 0, resp)
        rc, tresp = self.harness.cli("agent", "token", "issue", name, user=True)
        self.assertEqual(rc, 0, tresp)
        return tresp["data"]["token"]

    def _run_setup_with_passphrase(self, home: str, passphrase: str, *extra: str) -> tuple:
        if sys.platform == "win32":
            env = self.harness._with_direct_pythonpath(self.harness.get_env())
            cmd = mesh_cli_argv(
                "--home", home, "--format", "json", "setup", "--passphrase-fd", "0", *extra,
            )
            proc = subprocess.run(
                cmd, cwd=REPO_ROOT, input=passphrase, capture_output=True, text=True, timeout=60, env=env,
            )
        else:
            r_fd, w_fd = os.pipe()
            os.write(w_fd, passphrase.encode("utf-8"))
            os.close(w_fd)
            cmd = [
                sys.executable, "-m", "mesh", "--home", home, "--format", "json",
                "setup", "--passphrase-fd", str(r_fd), *extra,
            ]
            try:
                proc = subprocess.run(
                    cmd, cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    pass_fds=(r_fd,), timeout=60, env=self.harness.get_env(),
                )
            finally:
                os.close(r_fd)
        try:
            body = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            body = {"raw_stdout": proc.stdout, "raw_stderr": proc.stderr}
        return proc.returncode, body

    # F2: task keepalive preserves session_missing and session_mismatch -----------
    def test_f2_keepalive_preserves_session_missing_and_mismatch(self) -> None:
        tok_lead = self._create_agent("lead1", can_lead=True)
        tok_ibx = self._create_agent("ibx1")
        rc, prj = self.harness.cli(
            "intake", "submit", "--title", "P-F2", "--goal", "g", "--criterion", "c1", "--lead", "lead1", user=True
        )
        self.assertEqual(rc, 0, prj)
        pid = prj["data"]["id"]
        rc, tsk = self.harness.cli(
            "task", "create", "--project", pid, "--title", "T1", "--spec", "s", "--assignee", "ibx1", token=tok_lead
        )
        self.assertEqual(rc, 0, tsk)
        tid = tsk["data"]["id"]
        rc, st = self.harness.cli("task", "start", tid, token=tok_ibx)
        self.assertEqual(rc, 0, st)
        run_id = st["data"]["run_id"]
        fence = st["data"]["fence"]

        # Without MESH_SESSION -> session_missing must NOT be rewritten to lost-lease stop message
        rc1, resp1 = self.harness.cli(
            "task", "keepalive", tid, "--run", run_id, "--fence", str(fence),
            token=tok_ibx, extra_env={"MESH_SESSION": ""},
        )
        self.assertEqual(rc1, c.EXIT_STALE, resp1)
        self.assertEqual(resp1["error"]["code"], c.FENCE_MISMATCH)
        self.assertEqual(resp1["error"]["details"]["reason"], "session_missing")
        self.assertIn("MESH_SESSION", resp1["error"]["message"])
        self.assertNotIn("no longer holds task", resp1["error"]["message"])

        # With stale/wrong MESH_SESSION -> session_mismatch preserved too
        rc2, resp2 = self.harness.cli(
            "task", "keepalive", tid, "--run", run_id, "--fence", str(fence),
            token=tok_ibx, extra_env={"MESH_SESSION": "sess_stale_wrong"},
        )
        self.assertEqual(rc2, c.EXIT_STALE, resp2)
        self.assertEqual(resp2["error"]["code"], c.FENCE_MISMATCH)
        self.assertEqual(resp2["error"]["details"]["reason"], "session_mismatch")
        self.assertNotIn("Stop working on this run", resp2["error"]["message"])

    # F1: Live CLI rejects bad option types and leading '-' values ---------------
    def test_f1_cli_rejects_bad_adapter_options_and_models(self) -> None:
        cases = [
            ["agent", "create", "--name", "k_bad1", "--adapter", "kiro", "--option", "agent=true",
             "--allowed-model", "m1", "--default-model", "m1"],
            ["agent", "create", "--name", "k_bad2", "--adapter", "kiro", "--option", "agent=--trust-all-tools",
             "--allowed-model", "m1", "--default-model", "m1"],
            ["agent", "create", "--name", "d_bad", "--adapter", "droid",
             "--allowed-model=--skip-permissions-unsafe", "--default-model=--skip-permissions-unsafe"],
            ["agent", "create", "--name", "v_bad", "--adapter", "vibe", "--option", "max_turns=null",
             "--option", "max_tokens=--auto-approve", "--allowed-model", "m1", "--default-model", "m1"],
        ]
        for argv in cases:
            rc, resp = self.harness.cli(*argv, user=True)
            self.assertEqual(rc, c.EXIT_USAGE, (argv, resp))
            self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)
            if "d_bad" in argv:
                self.assertEqual(resp["error"]["details"].get("param"), "allowed_models", resp)

    # F3: adapter probe requires adapter_id with --binary & normalizes relative path
    def test_f3_cli_probe_binary_validation_and_normalization(self) -> None:
        bin_dir = os.path.join(self.tok_dir, "bins")
        os.makedirs(bin_dir)
        droid_path = _write_python_stub(
            bin_dir,
            "droid",
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if sys.argv[1:2] == ['--version']:\n"
            "    print('droid 1.6.0')\n"
            "    sys.exit(0)\n"
            "print('Usage: droid')\n",
        )

        rc_bad, resp_bad = self.harness.cli("adapter", "probe", "--binary", droid_path, user=True)
        self.assertEqual(rc_bad, c.EXIT_USAGE, resp_bad)
        self.assertEqual(resp_bad["error"]["code"], c.INVALID_ARGUMENT)

        # Invoke CLI from parent dir with relative ./bins/droid (or ./bins/droid.cmd on Windows)
        rel_droid = "./bins/" + os.path.basename(droid_path)
        if sys.platform == "win32":
            proc = subprocess.run(
                mesh_cli_argv(
                    "--home", self.harness.home, "--format", "json",
                    "adapter", "probe", "droid", "--binary", rel_droid,
                    "--user", "--passphrase-file", self.harness.passphrase_file,
                ),
                cwd=self.tok_dir, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=30, env={**self.harness.get_env(), "PYTHONPATH": REPO_ROOT},
            )
        else:
            pass_fd = os.open(self.harness.passphrase_file, os.O_RDONLY)
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "mesh", "--home", self.harness.home, "--format", "json",
                     "adapter", "probe", "droid", "--binary", rel_droid, "--user", "--passphrase-fd", str(pass_fd)],
                    cwd=self.tok_dir, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    pass_fds=(pass_fd,), timeout=30, env={**self.harness.get_env(), "PYTHONPATH": REPO_ROOT},
                )
            finally:
                os.close(pass_fd)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["data"]["binary_path"], os.path.abspath(droid_path))

    # F6: setup wrong passphrase burns 1 attempt, preserves default_agent, --no-start works
    def test_f6_setup_wrong_passphrase_no_start_and_default_agent(self) -> None:
        self._create_agent("lead2", can_lead=True)
        rc, set_resp = self.harness.cli("config", "set", "lead.default_agent", '"lead2"', user=True)
        self.assertEqual(rc, 0, set_resp)

        # Wrong passphrase on setup must fail at agent show without also calling agent create (1 failure, not 2)
        rc_w, resp_w = self._run_setup_with_passphrase(
            self.harness.home, "wrong-passphrase-xyz-99", "--token-dir", self.tok_dir
        )
        self.assertNotEqual(rc_w, 0)
        self.assertEqual(resp_w["error"]["code"], c.PASSPHRASE_INVALID)
        conn = self.harness.db()
        try:
            row = conn.execute("SELECT COUNT(*) FROM auth_failures;").fetchone()
            self.assertEqual(row[0], 1)
        finally:
            conn.close()

        # Setup with correct passphrase must NOT overwrite lead.default_agent='lead2'
        rc_ok, resp_ok = self._run_setup_with_passphrase(
            self.harness.home, self.harness.passphrase, "--token-dir", self.tok_dir
        )
        self.assertEqual(rc_ok, 0, resp_ok)
        rc_g, resp_g = self.harness.cli("config", "get", "lead.default_agent", user=True)
        self.assertEqual(rc_g, 0, resp_g)
        self.assertEqual(resp_g["data"]["value"], "lead2")

        # Fresh home with --no-start must succeed without crashing with DAEMON_UNAVAILABLE
        fresh_home = os.path.join(self.tok_dir, "fresh-home")
        fresh_tok = os.path.join(self.tok_dir, "fresh-tok")
        rc_ns, resp_ns = self._run_setup_with_passphrase(
            fresh_home, self.harness.passphrase, "--no-start", "--token-dir", fresh_tok
        )
        self.assertEqual(rc_ns, 0, resp_ns)
        self.assertTrue(os.path.exists(os.path.join(fresh_home, "mesh.db")))

    # F8: server socket envelope validation & CLI config set --input -------------
    def test_f8_daemon_malformed_envelopes_and_cli_input(self) -> None:
        sock_path = plat_ipc.daemon_endpoint_path(self.harness.home)
        payloads = [
            b"\xff\xfe\n",
            b"{not valid json\n",
            b"[]\n",
            b'{"cmd": 123}\n',
            b'{"cmd": "status", "auth": "bad", "args": {}}\n',
            b'{"cmd": "status", "auth": {"type": 123}, "args": {}}\n',
            b'{"cmd": "status", "auth": {"type": "user"}, "args": "bad"}\n',
            b'{"cmd": "status", "auth": {"type": "user", "passphrase": 123}, "args": {}}\n',
            b'{"cmd": "status", "auth": {"type": "token", "token": 123}, "args": {}}\n',
            b'{"cmd": "status", "auth": {"type": "user", "passphrase": "' + self.harness.passphrase.encode() + b'"}, "args": {}, "idempotency_key": "bad key!"}\n',
        ]
        for raw in payloads:
            resp = _raw_socket_call(sock_path, raw)
            self.assertFalse(resp["ok"], raw)
            self.assertEqual(resp["exit_code"], c.EXIT_USAGE, (raw, resp))
            self.assertIn(resp["error"]["code"], (c.INVALID_JSON, c.INVALID_ARGUMENT), (raw, resp))

        rc_empty, resp_empty = self.harness.cli("config", "set", "--input", "-", user=True, stdin="{}")
        self.assertEqual(rc_empty, c.EXIT_USAGE, resp_empty)
        self.assertEqual(resp_empty["error"]["code"], c.INVALID_ARGUMENT)

        rc_int, resp_int = self.harness.cli(
            "config", "set", "--input", "-", user=True, stdin='{"key": "lease.ttl_s", "value": 90}'
        )
        self.assertEqual(rc_int, 0, resp_int)
        self.assertEqual(resp_int["data"]["value"], 90)

    # F11: idempotent agent onboard / token issue replay without --force ----------
    def test_f11_idempotent_onboard_and_token_issue_replay(self) -> None:
        tok_out = os.path.join(self.tok_dir, "w1.tok")
        rc1, resp1 = self.harness.cli(
            "--idempotency-key", "k-onboard-g5",
            "agent", "onboard", "w1", "--out", tok_out, "--adapter", "none", user=True,
        )
        self.assertEqual(rc1, 0, resp1)
        self.assertTrue(os.path.exists(tok_out))
        self.assertEqual(resp1["data"]["token_file"], tok_out)

        # Identical retry WITHOUT --force must succeed via replay instead of pre-flight failure
        rc2, resp2 = self.harness.cli(
            "--idempotency-key", "k-onboard-g5",
            "agent", "onboard", "w1", "--out", tok_out, "--adapter", "none", user=True,
        )
        self.assertEqual(rc2, 0, resp2)
        self.assertTrue(resp2["data"]["replayed"])
        self.assertFalse(resp2["data"]["written"])

        # Same key with different args -> IDEMPOTENCY_KEY_REUSED (exit 6)
        rc3, resp3 = self.harness.cli(
            "--idempotency-key", "k-onboard-g5",
            "agent", "onboard", "w1", "--out", tok_out, "--adapter", "none", "--max-concurrency", "2", user=True,
        )
        self.assertEqual(rc3, c.EXIT_CONFLICT, resp3)
        self.assertEqual(resp3["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)

        # Same for agent token issue --idempotency-key --out: retry without --force reaches
        # daemon replay and returns IDEMPOTENCY_KEY_REUSED (replay=True), not pre-flight EXIT_USAGE
        tok_out2 = os.path.join(self.tok_dir, "w1_second.tok")
        rc4, resp4 = self.harness.cli(
            "--idempotency-key", "k-tok-g5",
            "agent", "token", "issue", "w1", "--out", tok_out2, user=True,
        )
        self.assertEqual(rc4, 0, resp4)
        self.assertTrue(os.path.exists(tok_out2))
        rc5, resp5 = self.harness.cli(
            "--idempotency-key", "k-tok-g5",
            "agent", "token", "issue", "w1", "--out", tok_out2, user=True,
        )
        self.assertEqual(rc5, c.EXIT_CONFLICT, resp5)
        self.assertEqual(resp5["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)
        self.assertTrue(resp5["error"]["details"]["replay"])
        self.assertFalse(resp5["error"]["details"]["written"])

    # F12 & F13: merge --all-accepted empty project/auth & missing subcommand JSON
    def test_f12_and_f13_merge_all_empty_checks_and_missing_subcommand_json(self) -> None:
        self._create_agent("lead_m", can_lead=True)
        tok_ibx = self._create_agent("ibx_m")

        # F12: nonexistent project returns NOT_FOUND (exit 5)
        rc_nf, resp_nf = self.harness.cli(
            "task", "merge", "--all-accepted", "--project", "prj_does_not_exist", user=True
        )
        self.assertEqual(rc_nf, c.EXIT_NOT_FOUND, resp_nf)
        self.assertEqual(resp_nf["error"]["code"], c.NOT_FOUND)

        # F12: empty existing project called by non-lead worker returns NOT_PROJECT_LEAD (exit 4)
        rc_p, prj = self.harness.cli(
            "intake", "submit", "--title", "EmptyP", "--goal", "g", "--criterion", "c1", "--lead", "lead_m", user=True
        )
        self.assertEqual(rc_p, 0, prj)
        rc_ua, resp_ua = self.harness.cli(
            "task", "merge", "--all-accepted", "--project", prj["data"]["id"], token=tok_ibx
        )
        self.assertEqual(rc_ua, c.EXIT_FORBIDDEN, resp_ua)
        self.assertEqual(resp_ua["error"]["code"], c.NOT_PROJECT_LEAD)

        # F13: missing subcommand returns exit 2 with JSON error envelope
        rc_sub, resp_sub = self.harness.cli("task")
        self.assertEqual(rc_sub, c.EXIT_USAGE, resp_sub)
        self.assertFalse(resp_sub["ok"])
        self.assertEqual(resp_sub["error"]["code"], c.INVALID_ARGUMENT)


if __name__ == "__main__":
    unittest.main()
