"""Unit tests for the M1.1 skeleton: CLI tree, help, exit codes, envelope, stubs.

Covers checklist items: A1, A2, A3, A4, A5, B1, B2, B7, B11.
"""

import ast
import json
import os
import re
import subprocess
import sys
import unittest

from tests.helpers import MeshHarness
import mesh.constants as c


class TestSkeleton(unittest.TestCase):
    """Test suite for Milestone M1.1 Skeleton."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def setUp(self):
        self.harness = MeshHarness()

    def tearDown(self):
        self.harness.teardown()

    def test_stdlib_only(self):
        """Verify only Python standard library and mesh modules are imported. ACC: A2"""
        stdlib = sys.stdlib_module_names if hasattr(sys, "stdlib_module_names") else set()
        mesh_dir = os.path.join(self.repo_root, "mesh")

        forbidden_pkgs = {"cryptography", "requests", "pytest"}

        for root, _, files in os.walk(mesh_dir):
            for file in files:
                if not file.endswith(".py"):
                    continue
                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            top_name = alias.name.split(".")[0]
                            self.assertNotIn(top_name, forbidden_pkgs, f"Forbidden import {top_name} in {path}")
                            self.assertTrue(
                                top_name == "mesh" or top_name in stdlib or top_name in sys.builtin_module_names,
                                f"Non-stdlib import {top_name} in {path}",
                            )
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            top_name = node.module.split(".")[0]
                            self.assertNotIn(top_name, forbidden_pkgs, f"Forbidden import {top_name} in {path}")
                            self.assertTrue(
                                top_name == "mesh" or top_name in stdlib or top_name in sys.builtin_module_names,
                                f"Non-stdlib import {top_name} in {path}",
                            )

    def test_no_network_code(self):
        """Verify no AF_INET, AF_INET6, or HTTP servers exist in mesh/. ACC: C2"""
        mesh_dir = os.path.join(self.repo_root, "mesh")
        forbidden_patterns = [
            re.compile(r"AF_INET\b"),
            re.compile(r"AF_INET6\b"),
            re.compile(r"http\.server\b"),
            re.compile(r"socketserver\b"),
            re.compile(r"\.bind\(\("),
        ]

        for root, _, files in os.walk(mesh_dir):
            for file in files:
                if not file.endswith(".py"):
                    continue
                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                for pattern in forbidden_patterns:
                    match = pattern.search(content)
                    self.assertIsNone(match, f"Forbidden pattern '{pattern.pattern}' found in {path}")

    def test_no_forbidden_host_port_flags(self):
        """Verify no --host, --port, or --url options exist in help or CLI. ACC: A5"""
        forbidden = ("--host", "--port", "--url")
        rc, out = self.harness.cli("--help")
        self.assertEqual(rc, 0, "m --help must exit 0")
        raw_text = out.get("raw_stdout", "")
        self.assertNotIn("--host", raw_text)
        self.assertNotIn("--port", raw_text)
        self.assertNotIn("--url", raw_text)

        rc, out = self.harness.cli("help", "env")
        self.assertEqual(rc, 0, "m help env must exit 0")
        text = out.get("data", {}).get("text", "")
        self.assertTrue(text, "help env must return text")
        self.assertNotIn("--host", text)
        self.assertNotIn("--port", text)
        self.assertNotIn("--url", text)

        # Walk the whole argparse tree: every (sub)parser's --help text and every option string.
        import argparse
        from mesh.cli.main import build_parser

        visited = 0
        stack = [("mesh", build_parser())]
        while stack:
            path, parser = stack.pop()
            visited += 1
            help_text = parser.format_help()
            for flag in forbidden:
                self.assertNotIn(flag, help_text, f"'{path} --help' mentions {flag}")
            for action in parser._actions:
                for opt in action.option_strings:
                    for flag in forbidden:
                        self.assertFalse(
                            opt == flag or opt.startswith(flag + "="),
                            f"'{path}' defines forbidden option {opt}",
                        )
                if isinstance(action, argparse._SubParsersAction):
                    for name, sub in action.choices.items():
                        stack.append((f"{path} {name}", sub))
        self.assertGreater(visited, 50, "parser tree walk visited too few parsers")

    def test_no_workspace_agent_mesh_references(self):
        """Verify no references to earlier /workspace/agent-mesh code exist. ACC: A4"""
        target = "/workspace/" + "agent-mesh"
        dirs_to_check = ["mesh", "tests", "bin", "scripts"]
        text_exts = (".py", ".sh", ".md", ".txt", ".json")
        for d in dirs_to_check:
            dir_path = os.path.join(self.repo_root, d)
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                if "__pycache__" in root:
                    continue
                for file in files:
                    if not file.endswith(text_exts):
                        continue
                    file_path = os.path.join(root, file)
                    if file_path == os.path.abspath(__file__):
                        continue
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    self.assertNotIn(
                        target,
                        content,
                        f"Found forbidden reference in {file_path}",
                    )

    def test_no_vendor_credential_paths(self):
        """Verify no code in mesh/ opens vendor credential or config paths. ACC: A7"""
        vendor_path_pattern = re.compile(r"open\([\"'][^\"']*(\.factory|\.gemini|\.kiro|\.vibe)")
        mesh_dir = os.path.join(self.repo_root, "mesh")
        for root, _, files in os.walk(mesh_dir):
            if "__pycache__" in root:
                continue
            for file in files:
                if not file.endswith(".py"):
                    continue
                file_path = os.path.join(root, file)
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                m = vendor_path_pattern.search(content)
                self.assertIsNone(
                    m,
                    f"Found forbidden vendor config open in {file_path}: {m.group(0) if m else ''}",
                )

    def test_test_runner_script(self):
        """Verify test runner script exists, is executable, and runs offline test tiers. ACC: A1"""
        script_path = os.path.join(self.repo_root, "scripts", "test.sh")
        self.assertTrue(os.path.exists(script_path), "scripts/test.sh must exist")
        self.assertTrue(os.access(script_path, os.X_OK), "scripts/test.sh must be executable")
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("discover -s tests/unit", content)
        self.assertIn("discover -s tests/integration", content)
        self.assertIn("discover -s tests/recovery", content)

        # Meaningful execution: test syntax validation via sh -n
        proc_sh = subprocess.run(["sh", "-n", script_path], capture_output=True, text=True)
        self.assertEqual(proc_sh.returncode, 0, f"sh -n scripts/test.sh failed: {proc_sh.stderr}")

        # Meaningful execution: execute scripts/test.sh in non-recursive check mode
        proc = subprocess.run([script_path, "--check"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"scripts/test.sh --check failed: {proc.stderr}")
        self.assertIn("syntax and runner OK", proc.stdout)

    def test_help_layers_and_examples(self):
        """Verify layered --help and topics all exit 0 and contain Examples:. ACC: B1"""
        # Layer 1: top-level --help
        rc, out = self.harness.cli("--help")
        self.assertEqual(rc, 0)
        top_text = out.get("raw_stdout", "")
        self.assertIn("Examples:", top_text)

        # Layer 2: every resource --help
        resources = [
            "init", "status", "whoami", "version", "help",
            "daemon", "config", "agent", "adapter", "intake",
            "project", "task", "dep", "approval", "inbox",
            "event", "run", "workspace", "doctor", "audit",
        ]
        for res in resources:
            rc, out = self.harness.cli(res, "--help")
            self.assertEqual(rc, 0, f"Resource '{res} --help' failed with rc {rc}")
            text = out.get("raw_stdout", "")
            self.assertIn("Examples:", text, f"Resource '{res} --help' missing 'Examples:' section")

        # Layer 3: task create --help
        rc, out = self.harness.cli("task", "create", "--help")
        self.assertEqual(rc, 0)
        tc_text = out.get("raw_stdout", "")
        self.assertIn("Examples:", tc_text)
        self.assertIn("--project", tc_text)
        self.assertIn("--title", tc_text)
        self.assertIn("--spec", tc_text)

        # Topics: auth, approvals, exit-codes, json, workflow, adapters, env, testing
        topics = ["auth", "approvals", "exit-codes", "json", "workflow", "adapters", "env", "testing"]
        for topic in topics:
            rc, out = self.harness.cli("help", topic)
            self.assertEqual(rc, 0, f"Topic '{topic}' failed with rc {rc}")
            data = out.get("data", {})
            text = data.get("text", "")
            self.assertIn("Examples:", text, f"Topic '{topic}' missing 'Examples:'")

        # Specific check for exit-codes table verbatim matching plan §13.2
        rc, out = self.harness.cli("help", "exit-codes")
        self.assertEqual(rc, 0)
        text = out.get("data", {}).get("text", "")
        for code in range(15):
            self.assertIn(f"| {code} |", text, f"Exit code {code} missing from exit-codes help table")

        # Row-by-row comparison with plan §13.2 (code and name hard-coded; meaning read from the plan).
        expected_names = {
            0: "OK", 1: "INTERNAL", 2: "USAGE / VALIDATION", 3: "UNAUTHENTICATED", 4: "FORBIDDEN",
            5: "NOT_FOUND", 6: "CONFLICT", 7: "APPROVAL_REQUIRED", 8: "WAIT_TIMEOUT", 9: "STALE",
            10: "DAEMON_UNAVAILABLE", 11: "BUSY", 12: "APPROVAL_INVALID", 13: "ADAPTER_UNAVAILABLE",
            14: "MERGE_FAILED",
        }
        row_re = re.compile(r"^\| (\d+) \| ([^|]+?) \| (.+) \|$")

        def rows_of(md):
            rows = {}
            for line in md.splitlines():
                m = row_re.match(line.strip())
                if m:
                    code = int(m.group(1))
                    self.assertNotIn(code, rows, f"exit code {code} listed twice")
                    rows[code] = (m.group(2).strip(), m.group(3).strip())
            return rows

        with open(os.path.join(self.repo_root, "docs", "plan.md"), "r", encoding="utf-8") as f:
            plan = f.read()
        start = plan.index("### 13.2 Exit codes")
        end = plan.index("### 13.3", start)
        plan_rows = rows_of(plan[start:end])
        help_rows = rows_of(text)
        self.assertEqual(sorted(plan_rows), list(range(15)), "plan §13.2 table parse sanity")
        self.assertEqual(sorted(help_rows), list(range(15)), "help exit-codes must list exactly codes 0-14")
        self.assertFalse([c_ for c_ in help_rows if c_ >= 15], "no exit code >= 15 may be listed")
        self.assertFalse(re.search(r"^\|\s*(1[5-9]|[2-9]\d|\d{3,})\s*\|", text, re.M), "code >= 15 listed")
        for code in range(15):
            name, meaning = help_rows[code]
            self.assertEqual(name, expected_names[code], f"exit code {code} name")
            self.assertEqual(plan_rows[code][0], expected_names[code], f"plan exit code {code} name")
            self.assertEqual(meaning, plan_rows[code][1], f"exit code {code} meaning differs from plan §13.2")
        self.assertIn("INTERNAL_ERROR", text)
        self.assertIn("NOT_IMPLEMENTED_IN_M1", text)
        self.assertIn("MERGE_CONFLICT", text)

    def test_argparse_errors_envelope(self):
        """Verify argparse usage errors return rc 2 in JSON envelope. ACC: B2"""
        # Missing required args
        rc, out = self.harness.cli("task", "create", "--bogus")
        self.assertEqual(rc, 2)
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("exit_code"), 2)
        self.assertEqual(out.get("error", {}).get("code"), c.INVALID_ARGUMENT)

        # Unrecognized argument when required args are present
        rc, out = self.harness.cli("task", "create", "--project", "p", "--title", "t", "--spec", "s", "--bogus")
        self.assertEqual(rc, 2)
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("exit_code"), 2)
        self.assertEqual(out.get("error", {}).get("code"), c.INVALID_ARGUMENT)

    def test_format_flag_identical_rc(self):
        """Verify failing commands produce identical exit code under --format text and default JSON. ACC: B7"""
        rc_json, out_json = self.harness.cli("task", "create", "--bogus")
        rc_text, out_text = self.harness.cli("task", "create", "--bogus", "--format", "text")
        self.assertEqual(rc_json, 2)
        self.assertEqual(rc_text, 2)
        self.assertIn("ok", out_json)

        # Default output must itself be JSON (parsed directly, no harness fallback envelope).
        env = self.harness.get_env()
        base = [self.harness.bin_mesh, "--home", self.harness.home, "task", "create", "--bogus"]
        p_def = subprocess.run(base, env=env, capture_output=True, text=True, timeout=30)
        p_txt = subprocess.run(base + ["--format", "text"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p_def.returncode, p_txt.returncode, "rc must be identical with --format text and default")
        self.assertEqual(p_def.returncode, 2)
        doc = json.loads(p_def.stdout)
        self.assertIsInstance(doc, dict)
        self.assertIn("ok", doc)
        self.assertIs(doc["ok"], False)
        self.assertEqual(doc.get("error", {}).get("code"), c.INVALID_ARGUMENT)

    def test_not_implemented_stubs(self):
        """Verify unimplemented verbs return rc 2 NOT_IMPLEMENTED_IN_M1 with milestone details. ACC: B11 (superseded by K1/K9/K10/K12 in M2: intake --repo, task merge, project deliver and workspace prune/delete are built)"""
        # M2 verbs per checklist B11: `task merge` is built in M2 r5 (B11 superseded by K9);
        # it is checked below against a running daemon (rc 5 NOT_FOUND for an unknown task).

        # intake submit --repo is built in M2 r3 (B11 superseded by K1): a missing repo dir is
        # rc 2 INVALID_ARGUMENT details.param "repo", no longer NOT_IMPLEMENTED_IN_M1.
        # The check is daemon-side now, so this clause needs its own running daemon.
        repo_harness = MeshHarness(init_db=True, start_daemon=True)
        try:
            bad_repo = os.path.join(repo_harness.home, "not-a-repo")
            rc, out = repo_harness.cli("intake", "submit", "--title", "t", "--goal", "g", "--criterion", "c",
                                       "--repo", bad_repo, "--base-branch", "main",
                                       "--test-command", "none", user=True)
            self.assertEqual(rc, 2)
            self.assertFalse(out.get("ok"))
            self.assertEqual(out.get("error", {}).get("code"), c.INVALID_ARGUMENT)
            self.assertEqual(out.get("error", {}).get("details", {}).get("param"), "repo")
            # task merge (m2-r5): served by the daemon, no longer a NOT_IMPLEMENTED_IN_M1 stub.
            rc, out = repo_harness.cli("task", "merge", "tsk_123", user=True)
            self.assertEqual(rc, c.EXIT_NOT_FOUND, out)
            self.assertFalse(out.get("ok"))
            self.assertEqual(out.get("error", {}).get("code"), c.NOT_FOUND)
            self.assertNotIn("milestone", out.get("error", {}).get("details", {}))
            # project deliver is built in M2 r8 (B11 superseded by K10): served by the
            # daemon, so an unknown project is rc 5 NOT_FOUND, not a milestone stub.
            rc, out = repo_harness.cli("project", "deliver", "prj_123", user=True)
            self.assertEqual(rc, c.EXIT_NOT_FOUND, out)
            self.assertEqual(out.get("error", {}).get("code"), c.NOT_FOUND)
            self.assertNotIn("milestone", out.get("error", {}).get("details", {}))
            # Workspace verbs (Review 7b N3) are built in M2 r8 (K12): served by the daemon.
            rc, out = repo_harness.cli("workspace", "prune", user=True)
            self.assertNotEqual(out.get("error", {}).get("code"), c.NOT_IMPLEMENTED_IN_M1, out)
            self.assertNotIn("milestone", out.get("error", {}).get("details", {}))
            rc, out = repo_harness.cli("workspace", "delete", "tsk_123", user=True)
            self.assertEqual(rc, c.EXIT_NOT_FOUND, out)
            self.assertEqual(out.get("error", {}).get("code"), c.NOT_FOUND)
            self.assertNotIn("milestone", out.get("error", {}).get("details", {}))
        finally:
            repo_harness.teardown()

        # M1c verbs are built in m1c run 2 (sub-step 5): no longer NOT_IMPLEMENTED_IN_M1.
        for argv in (("run", "list"), ("workspace", "list"), ("workspace", "show", "tsk_123"), ("task", "log", "tsk_123")):
            rc, out = self.harness.cli(*argv)
            self.assertNotEqual(out.get("error", {}).get("code"), c.NOT_IMPLEMENTED_IN_M1, argv)

        # Top command
        rc, out = self.harness.cli("top")
        self.assertEqual(rc, 2)
        self.assertEqual(out.get("error", {}).get("code"), c.NOT_IMPLEMENTED_IN_M1)
        self.assertIsNone(out.get("error", {}).get("details", {}).get("milestone"))

    def test_version_command(self):
        """Verify mesh version returns rc 0 and correct version string."""
        rc, out = self.harness.cli("version")
        self.assertEqual(rc, 0)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("data", {}).get("version"), "1.0.0.dev0")

    def test_tripwire_shims(self):
        """Verify shims in tests/shims trigger tripwire and exit 99 when invoked. ACC: A3"""
        shims = ["droid", "agy", "kiro-cli", "vibe", "hermes", "openclaw"]
        tripwire = f"/tmp/tripwire-test-{os.getpid()}.log"
        if os.path.exists(tripwire):
            os.remove(tripwire)

        try:
            env = dict(os.environ)
            env["MESH_TRIPWIRE_FILE"] = tripwire
            env["MESH_SUITE_TRIPWIRE_FILE"] = tripwire
            for shim in shims:
                shim_path = os.path.join(self.repo_root, "tests", "shims", shim)
                proc = subprocess.run([shim_path, "--test-flag"], env=env, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 99)

            self.assertTrue(os.path.exists(tripwire))
            with open(tripwire, "r", encoding="utf-8") as f:
                content = f.read()
            for shim in shims:
                self.assertIn(shim, content)
        finally:
            if os.path.exists(tripwire):
                os.remove(tripwire)

    def test_suite_tripwire_propagation(self):
        """Verify shim execution propagates to suite-level tripwire file when MESH_SUITE_TRIPWIRE_FILE is set."""
        shim_tripwire = f"/tmp/tripwire-per-harness-{os.getpid()}.log"
        suite_tripwire = f"/tmp/tripwire-suite-test-{os.getpid()}.log"
        for p in (shim_tripwire, suite_tripwire):
            if os.path.exists(p):
                os.remove(p)

        try:
            env = dict(os.environ)
            env["MESH_TRIPWIRE_FILE"] = shim_tripwire
            env["MESH_SUITE_TRIPWIRE_FILE"] = suite_tripwire

            droid_shim = os.path.join(self.repo_root, "tests", "shims", "droid")
            proc = subprocess.run([droid_shim, "--version"], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 99)

            self.assertTrue(os.path.exists(shim_tripwire))
            self.assertTrue(os.path.exists(suite_tripwire))
            with open(suite_tripwire, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("droid called with: --version", content)
        finally:
            for p in (shim_tripwire, suite_tripwire):
                if os.path.exists(p):
                    os.remove(p)

    def test_pythonpath_isolation(self):
        """Verify bin/mesh records _MESH_ORIG_PYTHONPATH and build_external_env isolates child envs. Review-r3 Finding 1."""
        # 1. Test when caller had a custom PYTHONPATH
        env1 = {
            "PYTHONPATH": f"{self.repo_root}:/custom/vendor/lib",
            "_MESH_ORIG_PYTHONPATH": "/custom/vendor/lib",
            "MESH_HOME": "/tmp/mh-test",
            "MESH_TOKEN": "secret-run-token",
            "OTHER_VAR": "keep-me",
        }
        clean_env1 = c.build_external_env(env1)
        self.assertEqual(clean_env1.get("PYTHONPATH"), "/custom/vendor/lib")
        self.assertNotIn("MESH_HOME", clean_env1)
        self.assertNotIn("MESH_TOKEN", clean_env1)
        self.assertNotIn("_MESH_ORIG_PYTHONPATH", clean_env1)
        self.assertEqual(clean_env1.get("OTHER_VAR"), "keep-me")

        # 2. Test when caller had NO PYTHONPATH (unset)
        env2 = {
            "PYTHONPATH": f"{self.repo_root}",
            "_MESH_ORIG_PYTHONPATH": "__UNSET__",
            "MESH_HOME": "/tmp/mh-test",
            "USER": "box",
        }
        clean_env2 = c.build_external_env(env2)
        self.assertNotIn("PYTHONPATH", clean_env2)
        self.assertNotIn("MESH_HOME", clean_env2)
        self.assertNotIn("_MESH_ORIG_PYTHONPATH", clean_env2)
        self.assertEqual(clean_env2.get("USER"), "box")

        # 3. Test running bin/mesh preserves _MESH_ORIG_PYTHONPATH in process
        proc = subprocess.run(
            [self.harness.bin_mesh, "version"],
            env=dict(os.environ, PYTHONPATH="/some/orig/path"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
