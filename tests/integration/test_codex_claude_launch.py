"""codex / claude launch agents end to end through meshd, offline.

Each agent's binary is a test stub in a temp dir: it answers the probe (``--version``,
``--help``, ``exec --help`` / ``auth status``) like Codex CLI 0.166.0 / Claude Code 2.1.325
and, for a run, reads the prompt from stdin, prints a synthetic stream from
tests/fixtures/synthetic/{codex,claude}/ and exits with the given code. No real CLI, no
network, no login.

Launch evidence is a test double staged under /tmp and handed to meshd only through
MESH_TEST_SMOKE_RESULTS_DIR (as in test_vendor_exit_meaning.py). There is no real codex /
claude smoke capture yet, so the staged S-X-1 / S-X-6 captures are copies of the synthetic ok
streams; nothing is written under tests/fixtures/real or tests/smoke/results.

Non-zero exits: codex and claude opt in to ``nonzero_exit_uses_stream_meaning``, so a run that
exits 1 after an auth / quota / network failure in its stream fails with that reason
(auth_failed / quota_exhausted / network_error) instead of exit_nonzero.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List

from mesh.adapters import evidence, smoke_schema
from tests import tierlib
from tests.helpers import MeshHarness
from tests.stubs import create_test_stubs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTH = os.path.join(REPO_ROOT, "tests", "fixtures", "synthetic")
VERSIONS = {"codex": "0.166.0", "claude": "2.1.325"}
MODELS = {"codex": "gpt-6.1-sol", "claude": "opus"}

CODEX_STUB = """#!{python}
import sys
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.166.0"); sys.exit(0)
if args == ["--help"]:
    print("Usage: codex [OPTIONS] [PROMPT]\\n  -a, --ask-for-approval <POLICY>\\n  -C, --cd <DIR>\\n  -m, --model <MODEL>")
    sys.exit(0)
if args == ["exec", "--help"]:
    print("Usage: codex exec [OPTIONS] [PROMPT]\\n      --json\\n      --skip-git-repo-check\\n  -C, --cd <DIR>\\n"
          "  -m, --model <MODEL>\\n  -c, --config <key=value>\\n  -s, --sandbox <SANDBOX_MODE>\\n"
          "      --dangerously-bypass-approvals-and-sandbox")
    sys.exit(0)
if args == ["login", "status"]:
    print("Logged in using an API key"); sys.exit(0)
if "exec" in args and "--json" in args and args[-1] == "-":
    prompt = sys.stdin.read()
    if "MESH_SMOKE_OK" not in prompt:
        sys.exit(98)
    with open({stream!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.stdout.flush()
    sys.exit({rc})
sys.exit(99)
"""

CLAUDE_STUB = """#!{python}
import json, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.325 (Claude Code)"); sys.exit(0)
if args == ["--help"]:
    print("Usage: claude [options] [command] [prompt]\\n  -p, --print\\n  --output-format <format>\\n  --verbose\\n"
          "  --model <model>\\n  --effort <level>\\n  --permission-mode <mode>\\n  --allowedTools <tools...>\\n"
          "  -r, --resume [value]")
    sys.exit(0)
if args == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai"}})); sys.exit(0)
if args[:1] == ["-p"] and "stream-json" in args:
    if args[args.index("--permission-mode") + 1] != "dontAsk":
        sys.exit(97)
    prompt = sys.stdin.read()
    if "MESH_SMOKE_OK" not in prompt:
        sys.exit(98)
    with open({stream!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.stdout.flush()
    sys.exit({rc})
sys.exit(99)
"""


class CodexClaudeLaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)
        self.tmp = tempfile.mkdtemp(prefix="mh-ccl-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stage = os.path.join(self.tmp, "S")
        for adapter in ("codex", "claude"):
            self._stage_evidence(adapter)
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=False, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)
        self.harness.start_daemon(extra_env={evidence.OVERRIDE_ENV: os.path.join(self.stage, "results")})
        self.tok_lead = self._agent("lead1", ["--adapter", "none", "--delivery", "inbox", "--can-lead"])
        self.tok_plain = self._agent("plain", ["--adapter", "none", "--delivery", "inbox", "--capability", "intake"])
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("intake", "submit", "--title", "PRJ ccl", "--goal", "g", "--criterion", "C1",
                                    "--lead", "lead1", token=self.tok_plain)
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]

    def _stage_evidence(self, adapter: str) -> None:
        version = VERSIONS[adapter]
        with open(os.path.join(SYNTH, adapter, "synthetic_ok.jsonl"), "rb") as f:
            data = f.read()
        cases = []
        for cid in ("S-X-1", "S-X-6"):
            name = f"smoke-{cid}-stdout.jsonl"
            tierlib.write_capture(self.stage, f"{adapter}/{version}/{name}", data, [adapter])
            cases.append({"id": cid, "status": "pass", "reason": None, "rc": 0, "duration_s": 1.0,
                          "fixture": smoke_schema.fixture_rel(adapter, version, name),
                          "fixture_sha256": tierlib.sha256_bytes(data), "observation": None,
                          "evaluated_from": None, "carried_from": None})
        doc = {"schema": smoke_schema.SCHEMA, "adapter": adapter, "binary": f"/usr/local/bin/{adapter}",
               "version": version, "captured_at_utc": "2026-11-16T01:02:03Z", "mesh_commit": "abc1234",
               "model": MODELS[adapter], "effort": None, "profile": "readonly", "timeout_s": 180,
               "consent_ref": "test", "cases": cases}
        name = smoke_schema.results_name(adapter, version, "2026-11-16", [])
        tierlib.write_capture(self.stage, f"results/{name}", json.dumps(doc).encode(), ["test"])

    def _agent(self, name: str, args: List[str]) -> str:
        rc, resp = self.harness.setup_cli("agent", "create", "--name", name, "--max-concurrency", "1", *args, user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.setup_cli("agent", "token", "issue", name, user=True)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["token"]

    def _vendor_agent(self, adapter: str, name: str, stream: str, rc: int) -> None:
        path = os.path.join(self.tmp, f"bin-{name}", adapter)
        os.makedirs(os.path.dirname(path))
        template = CODEX_STUB if adapter == "codex" else CLAUDE_STUB
        with open(path, "w", encoding="utf-8") as f:
            f.write(template.format(python=sys.executable, stream=os.path.join(SYNTH, adapter, stream), rc=rc))
        os.chmod(path, 0o755)
        time.sleep(0.05)  # the probe row must be newer than the binary's mtime
        rc_p, resp = self.harness.cli("adapter", "probe", adapter, "--binary", path, user=True)
        self.assertEqual(rc_p, 0, resp)
        self.assertEqual((resp["data"]["status"], resp["data"]["version"]), ("partially_verified", VERSIONS[adapter]))
        self._agent(name, ["--adapter", adapter, "--delivery", "launch", "--binary", path,
                           "--allowed-model", MODELS[adapter], "--default-model", MODELS[adapter]])

    def test_agent_create_rejects_cli_unsupported_effort(self) -> None:
        """PR49 gemini P2: a value the CLI does not accept fails at agent create, not at spawn."""
        for adapter, bad in (("claude", "ultra"), ("codex", "ultracode")):
            with self.subTest(adapter=adapter):
                path = os.path.join(self.tmp, f"bin-eff-{adapter}", adapter)
                os.makedirs(os.path.dirname(path))
                template = CODEX_STUB if adapter == "codex" else CLAUDE_STUB
                with open(path, "w", encoding="utf-8") as f:
                    f.write(template.format(python=sys.executable,
                                            stream=os.path.join(SYNTH, adapter, "synthetic_ok.jsonl"), rc=0))
                os.chmod(path, 0o755)
                time.sleep(0.05)
                rc_p, resp = self.harness.cli("adapter", "probe", adapter, "--binary", path, user=True)
                self.assertEqual(rc_p, 0, resp)
                rc, resp = self.harness.setup_cli(
                    "agent", "create", "--name", f"eff_{adapter}", "--adapter", adapter, "--delivery", "launch",
                    "--binary", path, "--allowed-model", MODELS[adapter], "--default-model", MODELS[adapter],
                    "--allowed-effort", bad, "--default-effort", bad, user=True)
                self.assertNotEqual(rc, 0, resp)
                self.assertEqual(resp["error"]["code"], "EFFORT_NOT_ALLOWED", resp)

    def _run_task(self, assignee: str) -> str:
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", "t",
                                    "--spec", "say MESH_SMOKE_OK", "--assignee", assignee,
                                    "--profile", "readonly", token=self.tok_lead)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def _wait_final(self, task_id: str, timeout: float = 30.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        task = run = None
        while time.monotonic() < deadline:
            conn = self.harness.db()
            try:
                task = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
                run = conn.execute("SELECT * FROM runs WHERE task_id = ?;", (task_id,)).fetchone()
                lease = conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)).fetchone()
                events = [dict(r) for r in conn.execute(
                    "SELECT type, reason, payload_json FROM events WHERE task_id = ? ORDER BY rowid;", (task_id,))]
            finally:
                conn.close()
            if task["status"] in ("submitted", "failed") and run is not None and run["status"] != "running" \
                    and lease is None:
                return {"task": dict(task), "run": dict(run), "events": events}
            time.sleep(0.2)
        self.fail(f"task {task_id} did not settle: {dict(task) if task else None} run={dict(run) if run else None}")

    def _assert_submitted(self, got: Dict[str, Any], summary: str) -> None:
        self.assertEqual(got["task"]["status"], "submitted", got["task"].get("failure_json"))
        record = json.loads(got["task"]["deliverable_json"])
        self.assertIs(record["auto"], True)
        self.assertIn(summary, json.dumps(record["deliverable"]))
        self.assertNotIn("vendor_exit_meaning", json.loads(got["run"]["result_json"]))

    def _assert_failed(self, got: Dict[str, Any], reason: str, exit_code: int, meaning: str) -> None:
        self.assertEqual(got["task"]["status"], "failed")
        self.assertIsNone(got["task"]["deliverable_json"])
        failure = json.loads(got["task"]["failure_json"])
        self.assertEqual((failure["reason"], failure["details"]["exit_code"], failure["details"]["vendor_exit_meaning"]),
                         (reason, exit_code, meaning))
        self.assertEqual((got["run"]["status"], got["run"]["exit_code"]), ("exited", exit_code))
        types = [e["type"] for e in got["events"]]
        self.assertNotIn("task.submitted", types)
        self.assertEqual(types.count("task.failed"), 1)

    def test_codex_ok_auto_submits(self) -> None:
        self._vendor_agent("codex", "cx_ok", "synthetic_ok.jsonl", 0)
        got = self._wait_final(self._run_task("cx_ok"))
        self._assert_submitted(got, "MESH_SMOKE_OK codex")

    def test_codex_not_logged_in_exit1_is_auth_failed(self) -> None:
        self._vendor_agent("codex", "cx_auth", "synthetic_not_logged_in.jsonl", 1)
        self._assert_failed(self._wait_final(self._run_task("cx_auth")), "auth_failed", 1, "auth_failed")

    def test_codex_quota_exit1_is_quota_exhausted(self) -> None:
        self._vendor_agent("codex", "cx_quota", "synthetic_quota.jsonl", 1)
        self._assert_failed(self._wait_final(self._run_task("cx_quota")), "quota_exhausted", 1, "quota_exhausted")

    def test_claude_ok_auto_submits(self) -> None:
        self._vendor_agent("claude", "cl_ok", "synthetic_ok.jsonl", 0)
        self._assert_submitted(self._wait_final(self._run_task("cl_ok")), "MESH_SMOKE_OK claude")

    def test_claude_network_exit1_is_network_error(self) -> None:
        self._vendor_agent("claude", "cl_net", "synthetic_network.jsonl", 1)
        self._assert_failed(self._wait_final(self._run_task("cl_net")), "network_error", 1, "network_error")

    def test_claude_auth_exit0_is_vendor_error(self) -> None:
        """Exit 0 with an is_error result: the existing exit-0 rule (T8 vendor_error) applies."""
        self._vendor_agent("claude", "cl_auth0", "synthetic_not_logged_in.jsonl", 0)
        self._assert_failed(self._wait_final(self._run_task("cl_auth0")), "vendor_error", 0, "auth_failed")


if __name__ == "__main__":
    unittest.main()
