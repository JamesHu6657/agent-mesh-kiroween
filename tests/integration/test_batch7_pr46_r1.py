"""Integration regression tests for batch7 PR #46 round-1 review findings."""

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

import mesh.constants as c
from mesh.adapters import evidence, smoke_schema
from mesh.core.invariants import check_invariants
from tests import tierlib
from tests.helpers import MeshHarness
from tests.stubs import create_test_stubs


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_KIRO = os.path.join(REPO_ROOT, "tests", "fixtures", "real", "kiro", "3.1.0")
KIRO_MODEL = "claude-haiku-5.5"

_KIRO_DENIED_EXIT1_STUB = """#!{python}
import sys
args = sys.argv[1:]
FILES = {{("--help",): {help!r}, ("chat", "--help"): {chat_help!r},
          ("chat", "--list-models", "-f", "json"): {models!r}}}
if args == ["--version"]:
    print("kiro-cli 3.1.0")
    sys.exit(0)
if tuple(args) in FILES:
    with open(FILES[tuple(args)], "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
if args[:2] == ["chat", "--no-interactive"]:
    sys.stderr.write("[denied] tool permission approval is not supported in non-interactive mode\\n")
    for i in range(3000):
        sys.stderr.write(f"trace line {{i:05d}} continuing non-interactive execution\\n")
    sys.stderr.write("Error: network reset leak=" + {fake_sk!r} + " " + {fake_mesh!r} + "\\n")
    sys.stderr.flush()
    sys.exit(1)
sys.exit(99)
"""


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


class TestBatch7Pr46R1Integration(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)
        self.addCleanup(self.harness.teardown)
        self.tok_dir = tempfile.mkdtemp(prefix="mh-pr46-tok-")
        self.addCleanup(shutil.rmtree, self.tok_dir, True)
        os.chmod(self.tok_dir, 0o700)

    def test_f1_keyed_onboard_and_token_issue_refuse_existing_out_before_db_writes(self) -> None:
        existing = os.path.join(self.tok_dir, "existing.tok")
        with open(existing, "w", encoding="utf-8") as f:
            f.write("PREEXISTING_TOKEN\n")
        os.chmod(existing, 0o600)

        # 1. Fresh keyed onboard without --force must exit 2 and leave DB clean
        for attempt in (1, 2):
            rc, resp = self.harness.cli(
                "--idempotency-key", "fresh-k1",
                "agent", "onboard", "ghost_w", "--out", existing, "--adapter", "none",
                user=True,
            )
            self.assertEqual(rc, c.EXIT_USAGE, f"attempt {attempt}: {resp}")
            self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)

        # Dry-run with idem key and existing --out must refuse exit 2 too
        rc_dry, resp_dry = self.harness.cli(
            "--dry-run", "--idempotency-key", "fresh-k-dry",
            "agent", "onboard", "ghost_w", "--out", existing, "--adapter", "none",
            user=True,
        )
        self.assertEqual(rc_dry, c.EXIT_USAGE, resp_dry)
        self.assertEqual(resp_dry["error"]["code"], c.INVALID_ARGUMENT)

        with open(existing, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "PREEXISTING_TOKEN\n")

        conn = self.harness.db()
        try:
            self.assertIsNone(
                conn.execute("SELECT id FROM principals WHERE name = 'ghost_w';").fetchone()
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM credentials WHERE kind = 'agent_token';").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM idempotency WHERE key IN ('fresh-k1', 'fresh-k-dry');").fetchone()[0], 0
            )
        finally:
            conn.close()

        # 2. Fresh keyed `agent token issue` without --force must exit 2 before minting credential
        rc_c, resp_c = self.harness.cli(
            "agent", "create", "--name", "real_w", "--adapter", "none", "--delivery", "inbox", user=True,
        )
        self.assertEqual(rc_c, 0, resp_c)

        rc_ti, resp_ti = self.harness.cli(
            "--idempotency-key", "fresh-tk1",
            "agent", "token", "issue", "real_w", "--out", existing,
            user=True,
        )
        self.assertEqual(rc_ti, c.EXIT_USAGE, resp_ti)
        self.assertEqual(resp_ti["error"]["code"], c.INVALID_ARGUMENT)

        rc_ti_dry, resp_ti_dry = self.harness.cli(
            "--dry-run", "--idempotency-key", "fresh-tk-dry",
            "agent", "token", "issue", "real_w", "--out", existing,
            user=True,
        )
        self.assertEqual(rc_ti_dry, c.EXIT_USAGE, resp_ti_dry)
        self.assertEqual(resp_ti_dry["error"]["code"], c.INVALID_ARGUMENT)

        with open(existing, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "PREEXISTING_TOKEN\n")

        conn = self.harness.db()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM credentials WHERE kind = 'agent_token';").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events WHERE type = 'agent.token_issued';").fetchone()[0], 0
            )
        finally:
            conn.close()

        # 3. Direct daemon protocol checks for replay_only envelope rules
        sock = os.path.join(self.harness.home, "run", "mesh.sock")
        auth = {"type": "user", "passphrase": self.harness.passphrase}
        for bad_req in (
            {"v": 1, "cmd": "agent.create", "auth": auth, "args": {"name": "s1", "adapter": "none", "delivery": "inbox"}, "replay_only": True},
            {"v": 1, "cmd": "agent.create", "auth": auth, "args": {"name": "s2", "adapter": "none", "delivery": "inbox"}, "idempotency_key": "k-s2", "replay_only": "yes"},
            {"v": 1, "cmd": "agent.create", "auth": auth, "args": {"name": "s3", "adapter": "none", "delivery": "inbox"}, "idempotency_key": "k-s3", "dry_run": True, "replay_only": True},
        ):
            r = _raw_socket_call(sock, bad_req)
            self.assertEqual(r["exit_code"], c.EXIT_USAGE, r)
            self.assertEqual(r["error"]["code"], c.INVALID_ARGUMENT)

        r_miss = _raw_socket_call(sock, {
            "v": 1,
            "cmd": "agent.create",
            "auth": auth,
            "args": {"name": "ghost_sock", "adapter": "none", "delivery": "inbox"},
            "idempotency_key": "sock-miss",
            "replay_only": True,
        })
        self.assertEqual(r_miss["exit_code"], c.EXIT_USAGE, r_miss)
        self.assertEqual(r_miss["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(r_miss["error"]["details"].get("reason"), "replay_only_miss")

        # 4. Legitimate idempotent onboard replay (where first call created the file) still succeeds!
        ok_out = os.path.join(self.tok_dir, "good_onboard.tok")
        rc_ok1, resp_ok1 = self.harness.cli(
            "--idempotency-key", "good-ob-1",
            "agent", "onboard", "good_w", "--out", ok_out, "--adapter", "none", user=True,
        )
        self.assertEqual(rc_ok1, 0, resp_ok1)
        rc_ok2, resp_ok2 = self.harness.cli(
            "--idempotency-key", "good-ob-1",
            "agent", "onboard", "good_w", "--out", ok_out, "--adapter", "none", user=True,
        )
        self.assertEqual(rc_ok2, 0, resp_ok2)
        self.assertTrue(resp_ok2["data"]["replayed"])
        self.assertFalse(resp_ok2["data"]["written"])

        # 5. True token issue replay without --force returns IDEMPOTENCY_KEY_REUSED (exit 6)
        tok2_out = os.path.join(self.tok_dir, "tok2.tok")
        rc_t1, resp_t1 = self.harness.cli(
            "--idempotency-key", "good-ti-1",
            "agent", "token", "issue", "real_w", "--out", tok2_out, user=True,
        )
        self.assertEqual(rc_t1, 0, resp_t1)
        rc_t2, resp_t2 = self.harness.cli(
            "--idempotency-key", "good-ti-1",
            "agent", "token", "issue", "real_w", "--out", tok2_out, user=True,
        )
        self.assertEqual(rc_t2, c.EXIT_CONFLICT, resp_t2)
        self.assertEqual(resp_t2["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)

    def test_f2_cli_empty_input_and_json_arg_type_errors_return_usage(self) -> None:
        # Empty --input must return exit 2 (not exit 1 or 0)
        for cmd in (
            ("config", "set", "--input", ""),
            ("status", "--input", ""),
        ):
            rc, resp = self.harness.cli(*cmd, user=True)
            self.assertEqual(rc, c.EXIT_USAGE, (cmd, resp))
            self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)

        # Create lead & project for task/inbox calls
        rc, _ = self.harness.cli(
            "agent", "create", "--name", "lead1", "--adapter", "none", "--delivery", "inbox", "--can-lead", user=True,
        )
        self.assertEqual(rc, 0)
        rc, tresp = self.harness.cli("agent", "token", "issue", "lead1", user=True)
        self.assertEqual(rc, 0)
        tok_lead = tresp["data"]["token"]

        bad_cli_cases = [
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "binary": 123}', None),
            (["setup", "--input", "-"], '{"lead_name": 123}', None),
            (["task", "create", "--input", "-"], '{"project": ["a"], "title": "t", "spec": "s"}', tok_lead),
            (["inbox", "ack", "--input", "-"], '{"messages": 123}', tok_lead),
            (["inbox", "ack", "--input", "-"], '{"messages": []}', tok_lead),
            (["inbox", "ack", "--input", "-"], '{"messages": [123]}', tok_lead),
            (["task", "close", "tsk_1", "--input", "-"], '{"superseded_by": "tsk_2", "reason": 123}', tok_lead),
            (["agent", "token", "issue", "lead1", "--expires-in", "bogus"], None, None),
        ]
        for argv, stdin_text, tok in bad_cli_cases:
            rc, resp = self.harness.cli(*argv, stdin=stdin_text, token=tok, user=(tok is None))
            self.assertEqual(rc, c.EXIT_USAGE, (argv, stdin_text, resp))
            self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT, (argv, stdin_text, resp))

    def test_f6_and_f7_server_model_validation_reaches_daemon_without_droid_and_falsy_nonlists_rejected(self) -> None:
        # F6: reaches server-side _validate_model_name even when droid CLI is not in PATH
        rc, resp = self.harness.cli(
            "agent", "create", "--name", "d_bad_srv", "--adapter", "droid",
            "--allowed-model=--skip-permissions-unsafe",
            "--default-model=--skip-permissions-unsafe",
            user=True,
            extra_env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp["error"]["details"].get("param"), "allowed_models")

        # F7: falsy non-lists passed in --input JSON must fail INVALID_ARGUMENT (exit 2)
        for bad_json in (
            '{"name": "ibx_b1", "adapter": "fake", "delivery": "inbox", "allowed_models": false}',
            '{"name": "ibx_b2", "adapter": "fake", "delivery": "inbox", "allowed_efforts": 0}',
            '{"name": "ibx_b3", "adapter": "fake", "delivery": "inbox", "capabilities": {}}',
        ):
            rc_b, resp_b = self.harness.cli("agent", "create", "--input", "-", stdin=bad_json, user=True)
            self.assertEqual(rc_b, c.EXIT_USAGE, (bad_json, resp_b))
            self.assertEqual(resp_b["error"]["code"], c.INVALID_ARGUMENT)

        conn = self.harness.db()
        try:
            self.assertEqual(check_invariants(conn), [])
        finally:
            conn.close()


class TestBatch7Pr46R1VendorIntegration(unittest.TestCase):
    """End-to-end daemon launch test for Finding #5."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46-f5e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stage = os.path.join(self.tmp, "S")
        cases = []
        for cid in ("S-X-1", "S-X-6"):
            with open(os.path.join(REAL_KIRO, f"smoke-{cid}-stdout.jsonl"), "rb") as f:
                data = f.read()
            name = f"smoke-{cid}-stdout.jsonl"
            tierlib.write_capture(self.stage, f"kiro/3.1.0/{name}", data, ["kiro-cli", "chat"])
            cases.append({
                "id": cid, "status": "pass", "reason": None, "rc": 0, "duration_s": 1.0,
                "fixture": smoke_schema.fixture_rel("kiro", "3.1.0", name),
                "fixture_sha256": tierlib.sha256_bytes(data), "observation": None,
                "evaluated_from": None, "carried_from": None,
            })
        doc = {
            "schema": smoke_schema.SCHEMA, "adapter": "kiro", "binary": "/usr/local/bin/kiro-cli",
            "version": "3.1.0", "captured_at_utc": "2026-11-10T01:02:03Z", "mesh_commit": "abc1234",
            "model": KIRO_MODEL, "effort": None, "profile": "readonly", "timeout_s": 180,
            "consent_ref": "test", "cases": cases,
        }
        rname = smoke_schema.results_name("kiro", "3.1.0", "2026-11-10", [])
        tierlib.write_capture(self.stage, f"results/{rname}", json.dumps(doc).encode(), ["test"])

        self.stubs_dir = tempfile.mkdtemp(prefix="mh-pr46-stubs-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=False, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)
        self.harness.start_daemon(extra_env={evidence.OVERRIDE_ENV: os.path.join(self.stage, "results")})

    def test_f5_task_failed_with_tool_denied_keeps_exit_nonzero_and_bounds_redacts_db(self) -> None:
        fake_sk = "sk-" + "Z" * 24
        fake_mesh = "mesh_at_" + "Y" * 24

        rc, resp = self.harness.setup_cli(
            "agent", "create", "--name", "lead_k", "--adapter", "none", "--delivery", "inbox", "--can-lead", user=True
        )
        self.assertEqual(rc, 0, resp)
        rc, tok_r = self.harness.setup_cli("agent", "token", "issue", "lead_k", user=True)
        self.assertEqual(rc, 0, tok_r)
        tok_lead = tok_r["data"]["token"]

        kiro_bin = os.path.join(self.tmp, "bin-kiro", "kiro-cli")
        os.makedirs(os.path.dirname(kiro_bin))
        with open(kiro_bin, "w", encoding="utf-8") as f:
            f.write(_KIRO_DENIED_EXIT1_STUB.format(
                python=sys.executable,
                help=os.path.join(REAL_KIRO, "help.txt"),
                chat_help=os.path.join(REAL_KIRO, "chat-help.txt"),
                models=os.path.join(REAL_KIRO, "list-models.json"),
                fake_sk=fake_sk,
                fake_mesh=fake_mesh,
            ))
        os.chmod(kiro_bin, 0o755)
        time.sleep(0.05)

        rc_p, resp_p = self.harness.cli("adapter", "probe", "kiro", "--binary", kiro_bin, user=True)
        self.assertEqual(rc_p, 0, resp_p)
        rc_a, resp_a = self.harness.setup_cli(
            "agent", "create", "--name", "kiro_w", "--adapter", "kiro", "--delivery", "launch",
            "--binary", kiro_bin, "--allowed-model", KIRO_MODEL, "--default-model", KIRO_MODEL, user=True,
        )
        self.assertEqual(rc_a, 0, resp_a)

        rc_prj, resp_prj = self.harness.cli(
            "intake", "submit", "--title", "PRJ F5", "--goal", "g", "--criterion", "C1", "--lead", "lead_k", user=True
        )
        self.assertEqual(rc_prj, 0, resp_prj)
        pid = resp_prj["data"]["id"]
        rc_tsk, resp_tsk = self.harness.cli(
            "task", "create", "--project", pid, "--title", "TF5", "--spec", "say hi",
            "--assignee", "kiro_w", "--profile", "readonly", token=tok_lead,
        )
        self.assertEqual(rc_tsk, 0, resp_tsk)
        tid = resp_tsk["data"]["id"]

        deadline = time.monotonic() + 30.0
        task = run = lease = None
        events = []
        while time.monotonic() < deadline:
            conn = self.harness.db()
            try:
                task = conn.execute("SELECT * FROM tasks WHERE id = ?;", (tid,)).fetchone()
                run = conn.execute("SELECT * FROM runs WHERE task_id = ?;", (tid,)).fetchone()
                lease = conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{tid}",)).fetchone()
                events = [dict(r) for r in conn.execute(
                    "SELECT type, reason, payload_json FROM events WHERE task_id = ? ORDER BY rowid;", (tid,)
                )]
            finally:
                conn.close()
            if task and task["status"] in ("submitted", "failed") and run is not None and run["status"] != "running" and lease is None:
                break
            time.sleep(0.15)
        else:
            self.fail(f"task {tid} did not settle: {dict(task) if task else None} run={dict(run) if run else None}")

        self.assertEqual(task["status"], "failed")
        failure = json.loads(task["failure_json"])
        self.assertEqual(failure["reason"], "exit_nonzero")
        self.assertIn("stderr_error", failure["details"])
        self.assertEqual(failure["details"]["stderr_error"]["reason"], "tool_denied")
        self.assertLessEqual(len(failure["details"]["stderr_error"]["details"]["stderr"]), 2000)

        failed_evts = [e for e in events if e["type"] == "task.failed"]
        self.assertEqual(len(failed_evts), 1)
        self.assertEqual(failed_evts[0]["reason"], "exit_nonzero")
        self.assertLess(len(task["failure_json"]), 10000)
        self.assertLess(len(failed_evts[0]["payload_json"]), 10000)
        self.assertNotIn(fake_sk, task["failure_json"])
        self.assertNotIn(fake_sk, failed_evts[0]["payload_json"])
        self.assertNotIn(fake_mesh, task["failure_json"])
        self.assertNotIn(fake_mesh, failed_evts[0]["payload_json"])

        conn = self.harness.db()
        try:
            self.assertEqual(check_invariants(conn), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
