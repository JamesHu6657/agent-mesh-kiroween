"""Integration regression tests for batch7 PR #46 round-2 review findings and same-class bugs."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import sqlite3
import unittest

import mesh.constants as c
from mesh.core.invariants import check_invariants
from scripts.coord import reap_daemons
from tests.helpers import MeshHarness


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN_MESH = os.path.join(REPO_ROOT, "bin", "mesh")


def _raw_socket_call(sock_path: str, payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10.0)
        s.connect(sock_path)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8").splitlines()[0])


class TestBatch7Pr46R3AdapterProbeBinary(unittest.TestCase):
    """PR46 r3 P3: adapter.probe must reject option-like / control-char binaries like agent.create does."""

    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)
        self.addCleanup(self.harness.teardown)

    def _probe_rows(self) -> int:
        conn = self.harness.db()
        try:
            return conn.execute("SELECT COUNT(*) FROM adapter_probes;").fetchone()[0]
        finally:
            conn.close()

    def test_probe_rejects_dash_and_control_char_binary(self) -> None:
        before = self._probe_rows()
        cases = (
            (("--binary=--skip",), None),
            (("--binary", " -x"), None),
            (("--input", "-"), json.dumps({"binary": "\t\n/bin/sh"})),
            (("--input", "-"), json.dumps({"binary": "/bin/sh\x7f"})),
        )
        for extra, stdin_text in cases:
            rc, resp = self.harness.cli("adapter", "probe", "droid", *extra, stdin=stdin_text, user=True)
            self.assertEqual(rc, c.EXIT_USAGE, (extra, stdin_text, resp))
            self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT, resp)
            self.assertEqual(resp["error"]["details"].get("param"), "binary", resp)
        self.assertEqual(self._probe_rows(), before)


class TestBatch7Pr46R2SetupIntegration(unittest.TestCase):
    """P2 Finding #1 (`mesh setup --input -` lead_name null / bad token_dir): zero side effects."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46r2-setup-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.tok_dir = os.path.join(self.tmp, "tok")
        os.makedirs(self.tok_dir, mode=0o700)
        self.pfile = os.path.join(self.tmp, "passphrase")
        with open(self.pfile, "w", encoding="utf-8") as f:
            f.write("test-setup-passphrase-16chars\n")
        os.chmod(self.pfile, 0o600)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        self.env["MESH_TESTING"] = "1"
        self.env["MESH_PASSPHRASE_FILE"] = self.pfile
        self.addCleanup(self._cleanup_leaked_daemon)

    def _cleanup_leaked_daemon(self) -> None:
        if os.path.exists(os.path.join(self.home, "run", "mesh.sock")):
            subprocess.run(
                [BIN_MESH, "--home", self.home, "daemon", "stop", "--user"],
                env=self.env, capture_output=True, text=True, timeout=15,
            )
            reap_daemons.kill_home_daemons(self.home)

    def _run_setup(self, *args: str, stdin_text: str = None):
        proc = subprocess.run(
            [BIN_MESH, "--home", self.home, "setup", *args],
            env=self.env,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=60,
        )
        env = json.loads(proc.stdout) if proc.stdout.strip() else {"raw_stderr": proc.stderr}
        return proc.returncode, env

    def test_setup_null_lead_name_and_empty_token_dir_exit_usage_with_no_side_effects(self) -> None:
        rc, resp = self._run_setup("--token-dir", self.tok_dir, "--input", "-", stdin_text='{"lead_name": null}')
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)
        self.assertFalse(os.path.exists(os.path.join(self.home, "mesh.db")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "run", "mesh.sock")))

        rc_td, resp_td = self._run_setup("--token-dir", "")
        self.assertEqual(rc_td, c.EXIT_USAGE, resp_td)
        self.assertEqual(resp_td["error"]["code"], c.INVALID_ARGUMENT)
        self.assertFalse(os.path.exists(os.path.join(self.home, "mesh.db")))

        rc_nl, resp_nl = self._run_setup(
            "--no-lead", "--input", "-", stdin_text=json.dumps({"token_dir": "bad\x00dir"}),
        )
        self.assertEqual(rc_nl, c.EXIT_USAGE, resp_nl)
        self.assertEqual(resp_nl["error"]["code"], c.INVALID_ARGUMENT)
        self.assertFalse(os.path.exists(os.path.join(self.home, "mesh.db")))

        rc_am, resp_am = self._run_setup(
            "--no-lead", "--input", "-", stdin_text=json.dumps({"approval_mode": "bogus"}),
        )
        self.assertEqual(rc_am, c.EXIT_USAGE, resp_am)
        self.assertEqual(resp_am["error"]["code"], c.INVALID_ARGUMENT)
        self.assertFalse(os.path.exists(os.path.join(self.home, "mesh.db")))


class TestBatch7Pr46R2DaemonIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)
        self.addCleanup(self.harness.teardown)
        self.tok_dir = tempfile.mkdtemp(prefix="mh-pr46r2-tok-")
        self.addCleanup(shutil.rmtree, self.tok_dir, True)
        os.chmod(self.tok_dir, 0o700)
        self.sock = os.path.join(self.harness.home, "run", "mesh.sock")
        self.user_auth = {"type": "user", "passphrase": self.harness.passphrase}

    def test_f1_task_and_project_wait_null_and_non_str_until_exit_usage(self) -> None:
        rc_a, _ = self.harness.setup_cli(
            "agent", "create", "--name", "lead1", "--adapter", "none", "--delivery", "inbox", "--can-lead", user=True,
        )
        self.assertEqual(rc_a, 0)
        rc_t, tresp = self.harness.setup_cli("agent", "token", "issue", "lead1", user=True)
        self.assertEqual(rc_t, 0)
        tok_lead = tresp["data"]["token"]

        rc_p, presp = self.harness.setup_cli(
            "intake", "submit", "--title", "P1", "--goal", "G1", "--criterion", "C1", "--lead", "lead1", user=True,
        )
        self.assertEqual(rc_p, 0, presp)
        pid = presp["data"]["id"]

        rc_tk, tkresp = self.harness.setup_cli(
            "task", "create", "--project", pid, "--title", "T1", "--spec", "S1", token=tok_lead,
        )
        self.assertEqual(rc_tk, 0, tkresp)
        tid = tkresp["data"]["id"]

        # CLI --input null until -> exit 2
        rc_w, resp_w = self.harness.cli(
            "task", "wait", tid, "--input", "-",
            stdin='{"until": null, "timeout": "1s"}', user=True,
        )
        self.assertEqual(rc_w, c.EXIT_USAGE, resp_w)
        self.assertEqual(resp_w["error"]["code"], c.INVALID_ARGUMENT)

        rc_pw, resp_pw = self.harness.cli(
            "project", "wait", pid, "--input", "-",
            stdin='{"until": null, "timeout": "1s"}', user=True,
        )
        self.assertEqual(rc_pw, c.EXIT_USAGE, resp_pw)
        self.assertEqual(resp_pw["error"]["code"], c.INVALID_ARGUMENT)

        # Direct socket task.wait & project.wait with null or non-str until -> exit 2 (never exit 1)
        for bad_until in (None, 123, ["submitted"]):
            rt = _raw_socket_call(self.sock, {
                "v": 1, "cmd": "task.wait", "auth": self.user_auth,
                "args": {"task_id": tid, "until": bad_until, "timeout": "1s"},
            })
            self.assertEqual(rt["exit_code"], c.EXIT_USAGE, rt)
            self.assertEqual(rt["error"]["code"], c.INVALID_ARGUMENT)

            rp = _raw_socket_call(self.sock, {
                "v": 1, "cmd": "project.wait", "auth": self.user_auth,
                "args": {"project_id": pid, "until": bad_until, "timeout": "1s"},
            })
            self.assertEqual(rp["exit_code"], c.EXIT_USAGE, rp)
            self.assertEqual(rp["error"]["code"], c.INVALID_ARGUMENT)

    def test_f2_agent_onboard_bad_expires_in_leaves_no_orphan_and_allows_retry(self) -> None:
        out_tok = os.path.join(self.tok_dir, "orphan_w.tok")
        bad_invocations = [
            (["--expires-in", "bogus"], None),
            (["--expires-in", "0s"], None),
            (["--expires-in", "-5s"], None),
            (["--expires-in", ""], None),
            (["--input", "-"], '{"expires_in": ""}'),
            (["--input", "-"], '{"expires_in": 123}'),
        ]
        for extra_args, stdin_text in bad_invocations:
            rc, resp = self.harness.cli(
                "agent", "onboard", "orphan_w", "--out", out_tok, *extra_args,
                stdin=stdin_text, user=True,
            )
            self.assertEqual(rc, c.EXIT_USAGE, (extra_args, stdin_text, resp))
            self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)
            self.assertFalse(os.path.exists(out_tok))

        # max_concurrency: null on onboard still returns INVALID_CONCURRENCY and leaves no DB rows
        rc_mc, resp_mc = self.harness.cli(
            "agent", "onboard", "orphan_w", "--out", out_tok, "--input", "-",
            stdin='{"max_concurrency": null}', user=True,
        )
        self.assertEqual(rc_mc, c.EXIT_USAGE, resp_mc)
        self.assertEqual(resp_mc["error"]["code"], c.INVALID_CONCURRENCY)

        conn = self.harness.db()
        try:
            self.assertIsNone(conn.execute("SELECT id FROM principals WHERE name = 'orphan_w';").fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM credentials WHERE kind = 'agent_token';").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type IN ('agent.created', 'agent.token_issued');").fetchone()[0], 0)
        finally:
            conn.close()

        # Subsequent valid onboard with the same name must succeed (no ALREADY_EXISTS)
        rc_ok, resp_ok = self.harness.cli(
            "agent", "onboard", "orphan_w", "--out", out_tok, "--expires-in", "7d", user=True,
        )
        self.assertEqual(rc_ok, 0, resp_ok)
        self.assertTrue(os.path.exists(out_tok))

    def test_f3_adapter_probe_empty_binary_and_empty_adapter_id(self) -> None:
        w_tok_path = os.path.join(self.tok_dir, "w_probe.tok")
        rc_o, resp_o = self.harness.setup_cli(
            "agent", "onboard", "w_probe", "--out", w_tok_path, user=True,
        )
        self.assertEqual(rc_o, 0, resp_o)
        with open(w_tok_path, "r", encoding="utf-8") as f:
            w_tok = f.read().strip()

        # Worker & user without adapter_id + --binary "" -> exit 2 INVALID_ARGUMENT (never probes PATH)
        for is_user, tok in ((False, w_tok), (True, None)):
            rc, resp = self.harness.cli("adapter", "probe", "--binary", "", user=is_user, token=tok)
            self.assertEqual(rc, c.EXIT_USAGE, resp)
            self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(resp["error"]["details"].get("param"), "adapter_id")

        # Worker WITH adapter_id + --binary "" -> server enforces direct user rule (exit 4 CAPABILITY_MISSING)
        rc_w4, resp_w4 = self.harness.cli("adapter", "probe", "agy", "--binary", "", token=w_tok)
        self.assertEqual(rc_w4, c.EXIT_FORBIDDEN, resp_w4)
        self.assertEqual(resp_w4["error"]["code"], c.CAPABILITY_MISSING)

        # Direct user WITH adapter_id + --binary "" -> server rejects empty binary (exit 2 INVALID_ARGUMENT)
        rc_u2, resp_u2 = self.harness.cli("adapter", "probe", "agy", "--binary", "", user=True)
        self.assertEqual(rc_u2, c.EXIT_USAGE, resp_u2)
        self.assertEqual(resp_u2["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_u2["error"]["details"].get("param"), "binary")

        # Empty positional adapter_id "" must reject exit 2 instead of probing all 8 adapters
        rc_ea, resp_ea = self.harness.cli("adapter", "probe", "", user=True)
        self.assertEqual(rc_ea, c.EXIT_USAGE, resp_ea)
        self.assertEqual(resp_ea["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_ea["error"]["details"].get("param"), "adapter_id")

        # Direct socket parity checks
        r_sock = _raw_socket_call(self.sock, {"v": 1, "cmd": "adapter.probe", "auth": self.user_auth, "args": {"adapter_id": False}})
        self.assertEqual(r_sock["exit_code"], c.EXIT_USAGE, r_sock)
        self.assertEqual(r_sock["error"]["code"], c.INVALID_ARGUMENT)

    def test_f4_and_same_class_direct_socket_and_cli_validation(self) -> None:
        rc_c, resp_c = self.harness.setup_cli(
            "agent", "create", "--name", "upd_w", "--adapter", "none", "--delivery", "inbox", user=True,
        )
        self.assertEqual(rc_c, 0, resp_c)

        # F4: direct socket agent.update bad types must return exit 2 INVALID_ARGUMENT / INVALID_CONCURRENCY and leave row untouched
        for bad_args, exp_code in (
            ({"name": "upd_w", "capabilities": False}, c.INVALID_ARGUMENT),
            ({"name": "upd_w", "capabilities": [{}]}, c.INVALID_ARGUMENT),
            ({"name": "upd_w", "capabilities": {"intake": False}}, c.INVALID_ARGUMENT),
            ({"name": "upd_w", "default_profile": {}}, c.INVALID_ARGUMENT),
            ({"name": "upd_w", "max_unapproved_profile": []}, c.INVALID_ARGUMENT),
            ({"name": "upd_w", "if_version": True}, c.INVALID_ARGUMENT),
            ({"name": "upd_w", "if_version": 1.5}, c.INVALID_ARGUMENT),
            ({"name": "upd_w", "default_model": 123}, c.INVALID_ARGUMENT),
            ({"name": "upd_w", "max_concurrency": None}, c.INVALID_CONCURRENCY),
        ):
            r = _raw_socket_call(self.sock, {"v": 1, "cmd": "agent.update", "auth": self.user_auth, "args": bad_args})
            self.assertEqual(r["exit_code"], c.EXIT_USAGE, (bad_args, r))
            self.assertEqual(r["error"]["code"], exp_code, (bad_args, r))

        # CLI agent update --input '{"max_concurrency": null}' -> INVALID_CONCURRENCY exit 2
        rc_umc, resp_umc = self.harness.cli(
            "agent", "update", "upd_w", "--input", "-", stdin='{"max_concurrency": null}', user=True,
        )
        self.assertEqual(rc_umc, c.EXIT_USAGE, resp_umc)
        self.assertEqual(resp_umc["error"]["code"], c.INVALID_CONCURRENCY)

        # Verify upd_w untouched
        rc_s, resp_s = self.harness.cli("agent", "show", "upd_w", user=True)
        self.assertEqual(rc_s, 0, resp_s)
        self.assertEqual(resp_s["data"]["version"], 1)
        self.assertEqual(resp_s["data"]["capabilities"], ["intake"])
        self.assertEqual(resp_s["data"]["default_profile"], "edit")
        self.assertEqual(resp_s["data"]["max_unapproved_profile"], "dev")
        self.assertEqual(resp_s["data"]["max_concurrency"], 1)

        # Same-class agent create CLI cases: --default-profile "", --binary "--skip", --input binary "a\x00b"
        for extra, stdin_text in (
            (("--default-profile", ""), None),
            (("--max-unapproved-profile", ""), None),
            (("--binary", "--skip"), None),
            (("--input", "-"), json.dumps({"binary": "a\x00b"})),
        ):
            rc_ac, resp_ac = self.harness.cli(
                "agent", "create", "--name", "bad_c", "--adapter", "fake", "--delivery", "launch",
                "--allowed-model", "m-a", "--default-model", "m-a", *extra, stdin=stdin_text, user=True,
            )
            self.assertEqual(rc_ac, c.EXIT_USAGE, (extra, stdin_text, resp_ac))
            self.assertEqual(resp_ac["error"]["code"], c.INVALID_ARGUMENT)

        # Same-class approval.request empty / falsy expires_in
        rc_ap1, resp_ap1 = self.harness.cli(
            "approval", "request", "--category", "spend_money", "--summary", "S",
            "--operation", '{"k": 1}', "--expires-in", "", user=True,
        )
        self.assertEqual(rc_ap1, c.EXIT_USAGE, resp_ap1)
        self.assertEqual(resp_ap1["error"]["code"], c.INVALID_ARGUMENT)
        for bad_exp in (0, False):
            r_ap = _raw_socket_call(self.sock, {
                "v": 1, "cmd": "approval.request", "auth": self.user_auth,
                "args": {"category": "spend_money", "summary": "S", "operation": {"k": 1}, "expires_in": bad_exp},
            })
            self.assertEqual(r_ap["exit_code"], c.EXIT_USAGE, (bad_exp, r_ap))
            self.assertEqual(r_ap["error"]["code"], c.INVALID_ARGUMENT)

        # Same-class project & task direct RPC checks + task merge --project ""
        rc_l, _ = self.harness.setup_cli(
            "agent", "create", "--name", "lead_sc", "--adapter", "none", "--delivery", "inbox", "--can-lead", user=True,
        )
        self.assertEqual(rc_l, 0)
        rc_pr, pr_resp = self.harness.setup_cli(
            "intake", "submit", "--title", "P_SC", "--goal", "G", "--criterion", "C1", "--lead", "lead_sc", user=True,
        )
        self.assertEqual(rc_pr, 0, pr_resp)
        pid = pr_resp["data"]["id"]

        for bad_cmd, bad_args in (
            ("task.create", {"project": pid, "title": "T", "spec": "S", "depends_on": 123}),
            ("task.create", {"project": pid, "title": "T", "spec": "S", "depends_on": False}),
            ("project.create", {"title": "P2", "goal": "G", "criterion": ["C"], "lead": "lead_sc", "depends_on_project": False}),
            ("project.update", {"project_id": pid, "goal": "G2", "criterion": False}),
        ):
            r_sc = _raw_socket_call(self.sock, {"v": 1, "cmd": bad_cmd, "auth": self.user_auth, "args": bad_args})
            self.assertEqual(r_sc["exit_code"], c.EXIT_USAGE, (bad_cmd, bad_args, r_sc))
            self.assertEqual(r_sc["error"]["code"], c.INVALID_ARGUMENT)

        rc_m, resp_m = self.harness.cli("task", "merge", "tsk_1", "--project", "", user=True)
        self.assertEqual(rc_m, c.EXIT_USAGE, resp_m)
        self.assertEqual(resp_m["error"]["code"], c.INVALID_ARGUMENT)

        conn = self.harness.db()
        try:
            self.assertEqual(check_invariants(conn), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
