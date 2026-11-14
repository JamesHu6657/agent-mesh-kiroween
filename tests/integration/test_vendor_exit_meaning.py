"""m3-kiro-real fix round (Linear review A, MAJOR 1): an exit 0 whose stream reports a vendor
error is T8 ``vendor_error``, never a T7 auto-submit.

Offline: a kiro launch agent whose binary is a test stub. The stub answers the probe from the
real kiro 3.1.0 help/model fixtures and, for a run, prints a synthetic copy of a kiro stream
(tests/fixtures/synthetic/kiro/) and exits 0. Launch evidence is a staged copy of a real kiro
smoke capture under /tmp, handed to meshd only through MESH_TEST_SMOKE_RESULTS_DIR.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

from mesh.adapters import evidence, smoke_schema
from tests import tierlib
from tests.helpers import MeshHarness
from tests.stubs import create_test_stubs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_KIRO = os.path.join(REPO_ROOT, "tests", "fixtures", "real", "kiro", "3.1.0")
SYNTH_KIRO = os.path.join(REPO_ROOT, "tests", "fixtures", "synthetic", "kiro")
AUTH_STREAM = os.path.join(SYNTH_KIRO, "synthetic_run_error_auth.jsonl")
OK_STREAM = os.path.join(SYNTH_KIRO, "synthetic_run_ok.jsonl")
NULL_STREAM = os.path.join(SYNTH_KIRO, "synthetic_run_error_null_fields.jsonl")
MODEL = "claude-haiku-5.5"

STUB = """#!{python}
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
    if {leftover!r}:
        # a setsid grandchild that outlives the leader (keeps MESH_RUN_ID): §11.3 leftovers branch
        import subprocess
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open({stream!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.stdout.flush()
    sys.exit(0)
sys.exit(99)
"""


class VendorExitMeaningLaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)
        self.tmp = tempfile.mkdtemp(prefix="mh-vem-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stage = os.path.join(self.tmp, "S")
        self._stage_evidence()
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
        rc, resp = self.harness.cli("intake", "submit", "--title", "PRJ vem", "--goal", "g", "--criterion", "C1",
                                    "--lead", "lead1", token=self.tok_plain)
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]

    def _stage_evidence(self) -> None:
        cases = []
        for cid in ("S-X-1", "S-X-6"):
            with open(os.path.join(REAL_KIRO, f"smoke-{cid}-stdout.jsonl"), "rb") as f:
                data = f.read()
            name = f"smoke-{cid}-stdout.jsonl"
            tierlib.write_capture(self.stage, f"kiro/3.1.0/{name}", data, ["kiro-cli", "chat"])
            cases.append({"id": cid, "status": "pass", "reason": None, "rc": 0, "duration_s": 1.0,
                          "fixture": smoke_schema.fixture_rel("kiro", "3.1.0", name),
                          "fixture_sha256": tierlib.sha256_bytes(data), "observation": None,
                          "evaluated_from": None, "carried_from": None})
        doc = {"schema": smoke_schema.SCHEMA, "adapter": "kiro", "binary": "/usr/local/bin/kiro-cli",
               "version": "3.1.0", "captured_at_utc": "2026-11-10T01:02:03Z", "mesh_commit": "abc1234",
               "model": MODEL, "effort": None, "profile": "readonly", "timeout_s": 180,
               "consent_ref": "test", "cases": cases}
        name = smoke_schema.results_name("kiro", "3.1.0", "2026-11-10", [])
        tierlib.write_capture(self.stage, f"results/{name}", json.dumps(doc).encode(), ["test"])

    def _agent(self, name: str, args: List[str]) -> str:
        rc, resp = self.harness.setup_cli("agent", "create", "--name", name, "--max-concurrency", "1", *args, user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.setup_cli("agent", "token", "issue", name, user=True)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["token"]

    def _kiro_agent(self, name: str, stream: str, leftover: bool = False) -> None:
        path = os.path.join(self.tmp, f"bin-{name}", "kiro-cli")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(STUB.format(python=sys.executable, help=os.path.join(REAL_KIRO, "help.txt"),
                                chat_help=os.path.join(REAL_KIRO, "chat-help.txt"),
                                models=os.path.join(REAL_KIRO, "list-models.json"), stream=stream,
                                leftover=leftover))
        os.chmod(path, 0o755)
        time.sleep(0.05)  # the probe row must be newer than the binary's mtime
        rc, resp = self.harness.cli("adapter", "probe", "kiro", "--binary", path, user=True)
        self.assertEqual(rc, 0, resp)
        self._agent(name, ["--adapter", "kiro", "--delivery", "launch", "--binary", path,
                           "--allowed-model", MODEL, "--default-model", MODEL])

    def _run_task(self, assignee: str) -> str:
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", "t",
                                    "--spec", "say MESH_SMOKE_OK", "--assignee", assignee,
                                    "--profile", "readonly", token=self.tok_lead)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def _wait_final(self, task_id: str, timeout: float = 30.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
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
        self.fail(f"task {task_id} did not settle: {dict(task)} run={dict(run) if run else None}")

    def test_rc0_kiro_auth_error_stream_fails_task(self) -> None:
        """MAJOR 1: kiro exits 0 but its stream ends in runError (bearer token invalid) -> task failed,
        reason vendor_error, vendor_exit_meaning auth_failed on the event and the run; never submitted."""
        self._kiro_agent("kx", AUTH_STREAM)
        tid = self._run_task("kx")
        got = self._wait_final(tid)
        self.assertEqual(got["task"]["status"], "failed")
        self.assertIsNone(got["task"]["deliverable_json"])
        failure = json.loads(got["task"]["failure_json"])
        self.assertEqual(failure["reason"], "vendor_error")
        self.assertEqual(failure["details"]["exit_code"], 0)
        self.assertEqual(failure["details"]["vendor_exit_meaning"], "auth_failed")
        self.assertEqual(got["run"]["status"], "exited")
        self.assertEqual(got["run"]["exit_code"], 0)
        self.assertEqual(json.loads(got["run"]["result_json"])["vendor_exit_meaning"], "auth_failed")
        types = [e["type"] for e in got["events"]]
        self.assertNotIn("task.submitted", types)
        failed = [e for e in got["events"] if e["type"] == "task.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason"], "vendor_error")
        payload = json.loads(failed[0]["payload_json"])
        self.assertEqual(payload["details"]["vendor_exit_meaning"], "auth_failed")
        self.assertEqual((payload["attempt"], payload["max_attempts"]), (1, 3))
        # §11.6: no automatic retry
        time.sleep(2)
        conn = self.harness.db()
        try:
            n_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?;", (tid,)).fetchone()[0]
            status = conn.execute("SELECT status FROM tasks WHERE id = ?;", (tid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((n_runs, status), (1, "failed"))

    def test_rc0_kiro_normal_stream_auto_submits(self) -> None:
        """Positive control: the same stub with a normal kiro stream (runFinished success) -> T7
        auto-submit unchanged, deliverable from runFinished.finalText, no vendor meaning on the run."""
        self._kiro_agent("ky", OK_STREAM)
        tid = self._run_task("ky")
        got = self._wait_final(tid)
        self.assertEqual(got["task"]["status"], "submitted")
        record = json.loads(got["task"]["deliverable_json"])
        self.assertIs(record["auto"], True)
        self.assertEqual(record["submitted_by"], "system")
        self.assertIn("MESH_SMOKE_OK", json.dumps(record["deliverable"]))
        self.assertNotIn("vendor_exit_meaning", json.loads(got["run"]["result_json"]))
        self.assertNotIn("task.failed", [e["type"] for e in got["events"]])

    def _assert_vendor_failed(self, got: Dict[str, Any], meaning: str) -> None:
        self.assertEqual(got["task"]["status"], "failed")
        self.assertIsNone(got["task"]["deliverable_json"])
        failure = json.loads(got["task"]["failure_json"])
        self.assertEqual((failure["reason"], failure["details"]["exit_code"], failure["details"]["vendor_exit_meaning"]),
                         ("vendor_error", 0, meaning))
        self.assertEqual((got["run"]["status"], got["run"]["exit_code"]), ("exited", 0))
        types = [e["type"] for e in got["events"]]
        self.assertNotIn("task.submitted", types)
        self.assertEqual(types.count("task.failed"), 1)

    def test_rc0_kiro_runerror_null_fields_fails_task(self) -> None:
        """PR #12 round 2 item 2: the auth stream with its runError stage and message set to null
        (synthetic_run_error_null_fields.jsonl), exit 0 -> failed vendor_error/run_error, never submitted."""
        self._kiro_agent("kn", NULL_STREAM)
        got = self._wait_final(self._run_task("kn"))
        self._assert_vendor_failed(got, "run_error")

    def test_rc0_kiro_auth_error_with_leftover_fails_task(self) -> None:
        """PR #12 round 2 item 3: exit 0 + auth runError while a setsid grandchild is still alive ->
        the §11.3 leftovers branch runs (run.leftovers_terminated) and must not auto-submit: the task
        ends failed vendor_error/auth_failed and no task.submitted is ever recorded."""
        self._kiro_agent("kl", AUTH_STREAM, leftover=True)
        got = self._wait_final(self._run_task("kl"), timeout=60.0)
        self.assertIn("run.leftovers_terminated", [e["type"] for e in got["events"]])
        self._assert_vendor_failed(got, "auth_failed")

    def test_rc0_kiro_ok_with_leftover_auto_submits(self) -> None:
        """Control for item 3: a normal stream with the same leftover -> auto-submitted in the leftovers branch."""
        self._kiro_agent("km", OK_STREAM, leftover=True)
        got = self._wait_final(self._run_task("km"), timeout=60.0)
        self.assertIn("run.leftovers_terminated", [e["type"] for e in got["events"]])
        self.assertEqual(got["task"]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
