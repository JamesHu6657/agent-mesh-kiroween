"""Coordinator-only real vendor e2e (m3-r7a droid; m3-kiro-real kiro). Never run by scripts/test.sh.

With the smoke/E2E gate closed, setUp skips before resolving a binary, creating a
home or spawning. An open gate also requires a positive run cap and explicit model.
MESH_SMOKE_E2E=<adapter> picks the class (DroidE2E or KiroE2E); the other skips.
MESH_SMOKE_E2E_CODE=1 selects the optional code demo instead of the readonly run,
so the C4 and C5 commands each spend at most one model run.

Kiro: the executor launch needs committed kiro smoke evidence (tests/smoke/results with
S-X-1 and S-X-6 pass for the probed version; mesh/adapters/evidence.py); until then
``task create`` is refused (ADAPTER_UNSUPPORTED) and no model run is spent. The code demo (N26) needs the
``dev`` profile, which kiro does not map (plan §14.2), so KiroE2E skips it.
Use ``MESH_SMOKE_MODEL_KIRO`` (a cheap model) and MESH_SMOKE_MAX_RUNS=1.
"""

import json
import os
import re
import shutil
import unittest

from mesh.adapters.registry import ADAPTER_CLASSES
from mesh.core import redact
from tests import tierlib
from tests.helpers import MeshHarness
from tests.smoke import harness

_RUNS_USED = 0


def required_vars(adapter: str) -> tuple:
    return ("MESH_SMOKE_MAX_RUNS", f"MESH_SMOKE_MODEL_{adapter.upper()}", "MESH_SMOKE_OUT")


REQUIRED_VARS = required_vars("droid")
# Adapters whose plan §14.2 mapping has no ``dev`` profile: no N26 code demo.
NO_DEV_PROFILE = ("kiro",)


class _VendorE2E(unittest.TestCase):
    ADAPTER = ""
    BINARY = ""
    LABEL = ""

    def setUp(self) -> None:
        global _RUNS_USED
        if not self.ADAPTER:
            self.skipTest("base class")
        env = dict(os.environ)
        reason = harness.gate(env, self.ADAPTER)
        if reason:
            self.skipTest(f"e2e tier closed: {reason}")
        if env.get("MESH_SMOKE_E2E") != self.ADAPTER:
            self.skipTest(f"MESH_SMOKE_E2E!={self.ADAPTER}")
        self.code_demo = self._testMethodName == "test_n26_code_task"
        if self.code_demo != (env.get("MESH_SMOKE_E2E_CODE") == "1"):
            self.skipTest("code demo not selected" if self.code_demo else "C5 selects only the code demo")
        if self.code_demo and self.ADAPTER in NO_DEV_PROFILE:
            self.skipTest(f"{self.ADAPTER}: no dev profile (plan §14.2), no code demo")
        # Gates open but required config absent: skip, not ERROR. Present-but-invalid values still raise in load_config.
        missing = [v for v in required_vars(self.ADAPTER) if not env.get(v)]
        if missing:
            self.skipTest("e2e config missing: " + ", ".join(missing))
        found = shutil.which(self.BINARY, path=harness.shim_free_path(env))
        self.config = harness.load_config(env, self.ADAPTER, os.path.abspath(found) if found else "")
        if _RUNS_USED >= self.config.max_runs:
            self.skipTest("run_cap")
        # Reserve before any setup subprocess; never refund on a failed attempt.
        _RUNS_USED += 1
        self.env = env

    def start_mesh(self, mesh: MeshHarness) -> None:
        from mesh.adapters.evidence import LAUNCH_HOLDS
        if self.ADAPTER in LAUNCH_HOLDS:  # L11: e.g. kiro N13 runs in M4 after its hold is removed
            mesh.teardown()
            self.skipTest(f"{self.ADAPTER} launch held until {LAUNCH_HOLDS[self.ADAPTER]} (L11)")
        self.mesh = mesh
        self.addCleanup(mesh.teardown)
        self.assertTrue(mesh.home.startswith("/tmp/mh-"))
        self.assertFalse(mesh.use_shims)
        self.assertNotIn("MESH_TESTING", mesh.get_env())
        self.assertNotIn(os.path.realpath(os.path.join(harness.REPO_ROOT, "tests", "shims")),
                         [os.path.realpath(p) for p in mesh.get_env()["PATH"].split(os.pathsep)])
        self.assertEqual(mesh.init()[0], 0)
        mesh.start_daemon()
        probe = self.cli("adapter", "probe", self.ADAPTER, "--binary", self.config.binary, user=True)
        self.version = probe["version"]
        self.cli("agent", "create", "--name", "e2e-lead", "--adapter", "none",
                 "--delivery", "inbox", "--can-lead", "--max-concurrency", "1", user=True)
        self.lead_token = self.cli("agent", "token", "issue", "e2e-lead", user=True)["token"]
        self.cli("agent", "create", "--name", "e2e-executor", "--adapter", self.ADAPTER,
                 "--delivery", "launch", "--binary", self.config.binary,
                 "--allowed-model", self.config.model, "--default-model", self.config.model,
                 "--default-profile", "readonly", "--max-unapproved-profile", "dev",
                 "--max-concurrency", "1", user=True)

    def cli(self, *args: str, **kwargs):
        rc, response = self.mesh.cli(*args, **kwargs)
        self.assertEqual(rc, 0, response)
        return response["data"]

    def project(self, *extra: str) -> str:
        return self.cli("project", "create", "--title", f"{self.LABEL} e2e",
                        "--goal", f"Exercise one real {self.ADAPTER} task", "--criterion", "Task succeeds",
                        "--lead", "e2e-lead", *extra, user=True)["id"]

    def submit_task(self, project: str, spec: str, profile: str, timeout_s: int):
        task_id = self.cli("task", "create", "--project", project, "--title", f"{self.LABEL} e2e",
                           "--spec", spec, "--assignee", "e2e-executor",
                           "--model", self.config.model, "--profile", profile,
                           "--timeout", f"{timeout_s}s", "--max-attempts", "1",
                           token=self.lead_token)["id"]
        latest = {}

        def finished() -> bool:
            latest["task"] = self.cli("task", "show", task_id, token=self.lead_token)
            return latest["task"]["status"] in ("submitted", "failed", "cancelled", "escalated")

        self.assertTrue(self.mesh.wait_for(finished, timeout=timeout_s + 45, step=0.5),
                        f"{task_id} did not finish within its timeout")
        task = latest["task"]
        self.assertEqual(task["status"], "submitted", task)
        conn = self.mesh.db()
        try:
            rows = conn.execute("SELECT * FROM runs WHERE task_id = ? AND kind = 'task';",
                                (task_id,)).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        run = dict(rows[0])
        argv = json.loads(run["argv_redacted_json"])
        self.assertEqual(argv[0], self.config.binary)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], self.config.model)
        self.check_argv(argv, run, profile)
        self.assertTrue(os.path.realpath(task["workspace_path"]).startswith("/tmp/mh-"))
        return task, run

    def check_argv(self, argv, run, profile: str) -> None:
        raise NotImplementedError

    def stage_record(self, case: str, task, run, **extra) -> None:
        """Keep only redacted, sidecar-backed evidence for coordinator promotion."""
        exact = redact.collect_exact_values(self.env, extra=[
            ("lead_token", self.lead_token), ("passphrase", self.mesh.passphrase)])
        command = ["python3", "-m", "unittest", "tests.smoke.test_e2e", "-v"]
        fixtures = {}
        for stream in ("stdout", "stderr"):
            with open(os.path.join(run["run_dir"], f"{stream}.log"), encoding="utf-8",
                      errors="replace") as f:
                data = redact.redact(f.read(), exact).encode("utf-8")
            rel = f"{self.ADAPTER}/{self.version}/e2e-{case}-{run['id']}-{stream}.log"
            tierlib.write_capture(self.config.out, rel, data, command)
            fixtures[stream] = {"fixture": f"tests/fixtures/real/{rel}",
                                "sha256": tierlib.sha256_bytes(data)}
        record = redact.redact_value({
            "case": case, "status": "pass", "adapter": self.ADAPTER, "version": self.version,
            "model": self.config.model, "consent_ref": self.config.consent_ref,
            "task": task, "run": run, "fixtures": fixtures, **extra,
        }, exact)
        # E2E records are not adapter capability results and must not supersede C3.
        path = tierlib.write_capture(
            self.config.out, f"results/e2e-{case}-{self.ADAPTER}-{self.version}-{run['id']}.json",
            (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8"), command)
        print(f"e2e record: {path}")

    def test_n13_readonly(self) -> None:
        """ACC: N13, N25 - gated readonly launch submits parsed text and records the model."""
        self.start_mesh(MeshHarness(use_shims=False))
        task, run = self.submit_task(self.project(), "Reply with exactly MESH_SMOKE_OK",
                                     "readonly", 300)
        deliverable = task["deliverable"]
        self.assertIn("MESH_SMOKE_OK", json.dumps(deliverable))
        if deliverable.get("structured") is False:
            self.assertIsNone(re.search(r'^\s*\{\s*"type"\s*:', deliverable["summary"], re.M))
        parsed = ADAPTER_CLASSES[self.ADAPTER]().parse(run["run_dir"])
        self.assertIn("MESH_SMOKE_OK", parsed.final_text)
        self.cli("doctor", "--check-invariants", user=True)
        self.stage_record("N13", task, run)

    def test_n26_code_task(self) -> None:
        """ACC: N26, N25 - opt-in dev task commits itself, then passes tests and merges."""
        import subprocess

        from tests.gitfixture import GitFixture, GitMeshHarness

        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        # Do not use make_k0(): it deliberately creates a bare origin.
        repo = fixture.path("repo")
        fixture.git(fixture.base, "init", "-b", "main", repo)
        fixture.git(repo, "config", "user.name", "t")
        fixture.git(repo, "config", "user.email", "t@example.invalid")
        with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
            f.write(f"{self.LABEL} code-task demo. No remote, no push.\n")
        fixture.git(repo, "add", "README.md")
        fixture.git(repo, "commit", "-m", "Initialize code-task demo")
        self.assertEqual(fixture.git(repo, "remote").stdout.strip(), "")
        self.assertEqual(fixture.git(repo, "for-each-ref", "refs/remotes/").stdout.strip(), "")
        main_before = fixture.head(repo, "refs/heads/main")
        self.start_mesh(GitMeshHarness(fixture, use_shims=False))
        tests = json.dumps([
            "python3", "-c",
            "from pathlib import Path; "
            "assert Path('hello.txt').read_text().strip() == 'MESH_DEMO_OK'",
        ])
        project = self.project("--repo", repo, "--base-branch", "main", "--test-command", tests)
        integration = f"refs/heads/mesh/{project}/integration"
        pre_head = fixture.head(repo, integration)
        task, run = self.submit_task(
            project,
            "Create hello.txt containing MESH_DEMO_OK and commit it with git in this worktree. Do not push.",
            "dev", 600)

        def lease_released() -> bool:
            conn = self.mesh.db()
            try:
                return conn.execute("SELECT 1 FROM leases WHERE key = ?;",
                                    (f"task:{task['id']}",)).fetchone() is None
            finally:
                conn.close()

        self.assertTrue(self.mesh.wait_for(lease_released, timeout=45),
                        "submitted task still has an active run lease")
        self.assertEqual(task["kind"], "code")
        git = task["deliverable"]["git"]
        self.assertNotIn("error", git)
        self.assertIs(git["dirty"], False)
        self.assertGreaterEqual(git["commit_count"], 1)
        branch = f"refs/heads/mesh/{project}/{task['id']}"
        task_tip = fixture.head(repo, branch)
        self.assertEqual(git["head"], task_tip)
        author = fixture.git(repo, "show", "-s", "--format=%an <%ae>", task_tip).stdout.strip()
        self.assertTrue(author)
        self.assertNotIn("mesh", author.lower())
        self.assertEqual(fixture.head(repo, integration), pre_head)
        self.cli("task", "accept", task["id"], token=self.lead_token)
        merged = self.cli("task", "merge", task["id"], token=self.lead_token, timeout=120.0)
        self.assertEqual(merged["status"], "merged")
        self.assertIs(merged["noop"], False)
        self.assertEqual(merged["tests"], "passed")
        final = self.cli("task", "show", task["id"], token=self.lead_token)
        self.assertEqual(final["status"], "merged")
        self.assertEqual(final["merge"]["tests"], "passed")
        merge_tip = fixture.head(repo, integration)
        self.assertEqual(merged["merge_commit"], merge_tip)
        parents = fixture.git(repo, "rev-list", "--parents", "-n", "1", merge_tip).stdout.split()
        self.assertEqual(parents, [merge_tip, pre_head, task_tip])
        self.assertEqual(fixture.git(repo, "show", f"{integration}:hello.txt").stdout.strip(),
                         "MESH_DEMO_OK")
        self.assertEqual(fixture.head(repo, "refs/heads/main"), main_before)
        self.assertEqual(fixture.git(repo, "remote").stdout.strip(), "")
        self.assertEqual(fixture.git(repo, "for-each-ref", "refs/remotes/").stdout.strip(), "")
        # The plan explicitly requires this read-only check of the Mesh checkout.
        checkout = subprocess.run(["git", "-C", harness.REPO_ROOT, "branch", "--list", "mesh/*"],
                                  env=fixture.env(), capture_output=True, text=True, check=True)
        self.assertEqual(checkout.stdout.strip(), "")
        self.cli("doctor", "--check-invariants", user=True)
        self.stage_record("N26", task, run, merge=final["merge"], task_author=author,
                          parents=parents[1:], no_remote=True, main_unchanged=True)


class DroidE2E(_VendorE2E):
    ADAPTER = "droid"
    BINARY = "droid"
    LABEL = "Droid"

    def check_argv(self, argv, run, profile: str) -> None:
        self.assertIn("--log-group-id", argv)
        self.assertEqual(argv[argv.index("--log-group-id") + 1], run["id"])
        if profile == "readonly":
            self.assertNotIn("--auto", argv)
        else:
            self.assertIn("--auto", argv)
            self.assertEqual(argv[argv.index("--auto") + 1], "medium")
        self.assertNotIn("--skip-permissions-unsafe", argv)


class KiroE2E(_VendorE2E):
    ADAPTER = "kiro"
    BINARY = "kiro-cli"
    LABEL = "Kiro"

    def check_argv(self, argv, run, profile: str) -> None:
        self.assertEqual(argv[1:5], ["chat", "--no-interactive", "--output-format", "stream-json"])
        self.assertEqual(profile, "readonly")
        self.assertIn("--trust-tools=", argv)
        self.assertNotIn("--trust-all-tools", argv)
        self.assertNotIn("-a", argv)
